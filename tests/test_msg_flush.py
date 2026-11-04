# -*- coding: utf-8 -*-
"""消息回显时效测试（2026-09-16）：agent_message_chunk 的 flush（上报）策略。

背景：chunk 原来只在「缓冲 > 800 字 / 下一次工具调用 / 回合结束」时上报，实测最近 20 个会话的
末条 message 事件 **100% 与会话结束同秒**——纯文字回复（尤其回合末总结）只能等整轮跑完才出现在
页面，体感「回显不及时」。现改为「停顿即出」：静默 MSG_FLUSH_IDLE_SEC 即上报，或缓冲超过
MSG_FLUSH_CHARS 立即上报（长输出安全阀）。

验证点：
- T1-T2 常量默认值与环境变量覆盖（KIMI_MSG_FLUSH_CHARS / KIMI_MSG_FLUSH_IDLE_SEC）
- T3 短 chunk 不立即上报；静默到期后由 _flush_loop 上报；close 置 _closed 后线程退出
- T4 缓冲超过阈值立即上报
- T5 下一次工具调用到达时 flush（既有行为保留，防回归）
- T6 _capture（/usage 采集）期间 chunk 不进缓冲、不受静默 flush 影响
"""
import os
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import acp_shared as ac  # noqa: E402


def _stub(capture=None):
    """ACPSession 桩：__new__ 绕过 __init__（不起 kimi 子进程），只备 flush 所需状态"""
    s = ac.ACPSession.__new__(ac.ACPSession)
    s.session_id = "session_test"
    s._msg_buf = []
    s._buf_lock = threading.Lock()
    s._last_chunk_at = 0.0
    s._closed = False
    s._capture = capture
    s._tool_calls = {}
    s._tool_posted = set()
    s.events = []
    s._emit = lambda kind, content, session_id=None: s.events.append((kind, content))
    return s


def _chunk(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def _tool_call(tid="t1", title="Running: ls -la"):
    return {"sessionUpdate": "tool_call", "toolCallId": tid, "title": title}


class L0FlushConstants(unittest.TestCase):
    """T1-T2：常量默认值 + env 覆盖（模块加载时读取）"""

    def test_t1_defaults(self):
        self.assertEqual(ac.MSG_FLUSH_CHARS, 400)
        self.assertEqual(ac.MSG_FLUSH_IDLE_SEC, 0.4)
        self.assertLessEqual(ac.MSG_FLUSH_TICK_SEC, ac.MSG_FLUSH_IDLE_SEC)

    def test_t2_env_override(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); import acp_shared as ac; "
                "print(ac.MSG_FLUSH_CHARS, ac.MSG_FLUSH_IDLE_SEC)") % str(REPO)
        env = {k: v for k, v in os.environ.items()
               if k not in ("KIMI_MSG_FLUSH_CHARS", "KIMI_MSG_FLUSH_IDLE_SEC")}
        env.update({"KIMI_MSG_FLUSH_CHARS": "120", "KIMI_MSG_FLUSH_IDLE_SEC": "0.15"})
        out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
        self.assertEqual(out.strip(), "120 0.15")


class L1FlushBehaviour(unittest.TestCase):
    """T3-T6：flush 时机（静默 / 超限 / 工具调用 / 采集隔离）"""

    def test_t3_idle_flush_and_thread_exit(self):
        """短回复不再等回合结束：静默到期由 _flush_loop 上报；close 后线程退出"""
        old_idle, old_tick = ac.MSG_FLUSH_IDLE_SEC, ac.MSG_FLUSH_TICK_SEC
        ac.MSG_FLUSH_IDLE_SEC, ac.MSG_FLUSH_TICK_SEC = 0.05, 0.01
        try:
            s = _stub()
            s._on_update(_chunk("这是一段没有工具调用的短回复"))
            self.assertEqual(s.events, [], "未到静默阈值不应上报（保持流式聚合）")
            t = threading.Thread(target=s._flush_loop, daemon=True)
            t.start()
            for _ in range(100):
                if s.events:
                    break
                time.sleep(0.02)
            self.assertTrue(s.events, "静默到期后应由 _flush_loop 上报")
            self.assertEqual(s.events[0][0], "message")
            self.assertIn("短回复", s.events[0][1])
            s._closed = True
            t.join(timeout=2)
            self.assertFalse(t.is_alive(), "close（_closed）后 flush 线程必须退出")
        finally:
            ac.MSG_FLUSH_IDLE_SEC, ac.MSG_FLUSH_TICK_SEC = old_idle, old_tick

    def test_t4_threshold_flushes_immediately(self):
        """长输出安全阀：缓冲超过 MSG_FLUSH_CHARS 立即上报（不必等静默）"""
        s = _stub()
        s._on_update(_chunk("x" * (ac.MSG_FLUSH_CHARS + 1)))
        self.assertEqual(len(s.events), 1, "超阈值应立即上报")
        self.assertEqual(s._msg_buf, [])

    def test_t5_tool_call_flushes_pending_text(self):
        """既有行为防回归：工具调用到达时先把已攒文字上报，再记录工具调用"""
        s = _stub()
        s._on_update(_chunk("我先看一下代码"))
        self.assertEqual(s.events, [])
        s._on_update(_tool_call())
        self.assertEqual(len(s.events), 1, "工具调用前应 flush 文字")
        self.assertIn("我先看一下代码", s.events[0][1])
        self.assertEqual(s._tool_calls.get("t1"), "Running: ls -la")

    def test_t6_capture_mode_bypasses_buffer(self):
        """/usage 采集（_capture）期间 chunk 进采集缓冲，不污染 _msg_buf、不触发上报"""
        cap = []
        s = _stub(capture=cap)
        s._on_update(_chunk("/usage 输出片段"))
        self.assertEqual(s._msg_buf, [], "采集期间不应写入 _msg_buf")
        self.assertEqual(cap, ["/usage 输出片段"])
        self.assertEqual(s.events, [])
        # 采集期间静默 flush 也应无动作（缓冲为空）
        old_idle, old_tick = ac.MSG_FLUSH_IDLE_SEC, ac.MSG_FLUSH_TICK_SEC
        ac.MSG_FLUSH_IDLE_SEC, ac.MSG_FLUSH_TICK_SEC = 0.02, 0.01
        try:
            s._last_chunk_at = time.time()
            t = threading.Thread(target=s._flush_loop, daemon=True)
            t.start()
            time.sleep(0.1)
            s._closed = True
            t.join(timeout=2)
            self.assertEqual(s.events, [], "采集期间不得上报 message 事件")
        finally:
            ac.MSG_FLUSH_IDLE_SEC, ac.MSG_FLUSH_TICK_SEC = old_idle, old_tick


if __name__ == "__main__":
    unittest.main()
