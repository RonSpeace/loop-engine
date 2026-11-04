# -*- coding: utf-8 -*-
"""ACP 请求超时的「睡眠穿透」修复测试（2026-09-17）。

背景：``ACPSession.request`` 原来用 ``threading.Event.wait(timeout)`` 一次等待。macOS 上
``Event.wait`` 走 ``pthread_cond_timedwait``（realtime 语义——本机 CPython 3.11 只链接了
``_pthread_cond_timedwait``，没有 ``relative_np`` 变体），**系统睡眠期间继续计时**：
合盖数小时后唤醒，仍在运行的 agent 会被误判超时 cancel，丢失已完成的工作。

修复：改为「分片等待 + monotonic 记账」——每片最多 ``ACP_WAIT_SLICE`` 秒，外层用
``time.monotonic()`` 累计（睡眠期间不前进），睡眠最多造成单片误差；进程真阻塞时
monotonic 正常前进，超时保护不被削弱。

验证点：
- T1 分片等待：每片 <= ACP_WAIT_SLICE，而非一次等整个 timeout
- T2 睡眠穿透：wait 片被 realtime 提前结束但 monotonic 未耗尽 → 继续等待，不误判超时
- T3 真超时：monotonic 耗尽 → 抛 TimeoutError 并清理 _pending
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import acp_shared as ac  # noqa: E402


def _stub():
    """ACPSession 桩：__new__ 绕过 __init__（不起 kimi 子进程），只备 request 所需状态"""
    s = ac.ACPSession.__new__(ac.ACPSession)
    s._next_id = 0
    s._pending = {}
    s._send = lambda msg: None
    return s


def _responder(session, slices, respond_at):
    """Event 替身工厂：记录每片时长，第 respond_at 片模拟「响应到达」"""
    class _Ev:
        def wait(self, timeout):
            slices.append(timeout)
            if len(slices) >= respond_at:
                session._pending[1]["response"] = {"result": "ok"}
                return True
            return False
    return _Ev


class RequestSliceTest(unittest.TestCase):
    """request 超时的时钟语义（分片 + monotonic）"""

    def test_t1_waits_in_slices(self):
        """T1：分片等待——每片不超过 ACP_WAIT_SLICE（回归：曾是一次 wait(整个 timeout)）"""
        slices = []
        s = _stub()
        with mock.patch.object(ac.threading, "Event", _responder(s, slices, 3)):
            out = s.request("test/m", {}, timeout=60)
        self.assertEqual(out, "ok")
        self.assertGreaterEqual(len(slices), 3, "应分多片等待")
        self.assertTrue(all(t <= ac.ACP_WAIT_SLICE for t in slices),
                        f"每片不得超过 {ac.ACP_WAIT_SLICE}s：{slices}")

    def test_t2_sleep_jump_does_not_timeout(self):
        """T2：睡眠穿透——wait 片被 realtime 提前结束，但 monotonic 未耗尽 → 不得超时。

        模拟合盖唤醒：每次 wait 都因 realtime 到期而返回 False（睡眠吃掉了片超时），
        但活跃时间只走了 1 秒/片；5 片共 5 秒 << 60 秒预算，必须继续等待并最终成功。
        """
        calls = []
        mono = [1000.0]
        s = _stub()

        class _Ev:
            def wait(self, timeout):
                calls.append(timeout)
                mono[0] += 1.0          # 每片只消耗 1 秒「活跃时间」（睡眠不计入）
                if len(calls) >= 5:
                    s._pending[1]["response"] = {"result": "ok"}
                    return True
                return False            # realtime 片提前到期（睡眠穿透）
        with mock.patch.object(ac.threading, "Event", _Ev), \
                mock.patch.object(ac.time, "monotonic", lambda: mono[0]):
            out = s.request("test/m", {}, timeout=60)
        self.assertEqual(out, "ok", "活跃时间 5 秒远小于 60 秒预算，不得误判超时")
        self.assertEqual(len(calls), 5)

    def test_t3_real_timeout_still_raises(self):
        """T3：monotonic 真正耗尽 → 抛 TimeoutError 并清理 _pending（超时保护不被削弱）"""
        mono = [1000.0]
        s = _stub()

        class _Ev:
            def wait(self, timeout):
                mono[0] += ac.ACP_WAIT_SLICE
                return False
        with mock.patch.object(ac.threading, "Event", _Ev), \
                mock.patch.object(ac.time, "monotonic", lambda: mono[0]):
            with self.assertRaises(TimeoutError):
                s.request("test/m", {}, timeout=10)
        self.assertNotIn(1, s._pending, "超时必须清理 _pending（防泄漏）")


if __name__ == "__main__":
    unittest.main()
