#!/usr/bin/env python3
"""req-421 会话活性状态行回归测试（A 组 + 接线断言）。

从 static/board.js 提取真实纯函数 `agentActivityModel`（连同 escapeHtml）交 node 执行断言（#33 外置）
（tests/session_activity_harness.js，覆盖 A1~A25：十五态矩阵/阈值边界/输入区显隐/自动展开/
手动态优先/服务端时钟基准/历史行 fail-safe/计时格式与基准来源/判定文案/XSS/思考与输出档位推进/
tie-break/优先级 P1~P6/降级口径）；另有 Python 侧断言覆盖接口契约与渲染接线（Jinja 常量注入、
服务端 ts_ms 解析、状态行渲染转义、tick/指针/回前台接线、done 状态行常驻）。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_session_activity -v
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_js
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "session_activity_harness.js"

NEEDED_FUNCS = ("escapeHtml", "agentActivityModel")


def _script_block():
    """#33：主脚本已外置 static/board.js（模板里只剩 skinBoot/配置脚本/外链），直接读该文件；
    返回 (js, js) 保持既有调用形态（src 与 js 在本文件内均只做 JS 断言）。"""
    js = frontend_js()
    return js, js


def _extract_source():
    """提取纯函数源码（与 test_process_markdown_merge 同款）"""
    _, js = _script_block()
    return "\n\n".join(_extract_function(js, name) for name in NEEDED_FUNCS)


class SessionActivityHarnessTest(unittest.TestCase):
    """#421 状态机纯函数（node 断言）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 #421 活性状态机 JS 断言")

    def _run_harness(self):
        source = _extract_source()
        harness = HARNESS_JS.read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(source + "\n\n" + harness)
            path = f.name
        try:
            proc = subprocess.run([self.node, path], capture_output=True, text=True, timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        if proc.returncode != 0:
            self.fail("node harness 失败: %s\n%s" % (proc.stderr, proc.stdout))
        return json.loads(proc.stdout)

    def test_a_all_model_checks_pass(self):
        """A1~A25：状态机纯函数全部断言通过"""
        data = self._run_harness()
        checks = data.get("checks", [])
        self.assertGreaterEqual(len(checks), 25, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)

    def test_a_server_stall_sec_injected(self):
        """阈值单一源：页面渲染时注入服务端 AGENT_STALL_SEC（避免前后端漂移）"""
        import board
        from flask import render_template_string
        with board.app.app_context():
            out = render_template_string(
                board.HTML, sse_enabled=False, agent_stall_sec=board.AGENT_STALL_SEC,
                agent_stall_first_sec=board.AGENT_STALL_FIRST_FRAME_SEC,
                thought_frames_absent=board.THOUGHT_FRAMES_ABSENT)
        # #33：阈值改由页面配置脚本注入（static/board.js 读 window.__REQ_BOARD_CFG__.stallSec）
        self.assertIn("stallSec:%d" % board.AGENT_STALL_SEC, out)
        self.assertIn("const ACTIVITY_WARN_SEC = 60;", frontend_js())
        self.assertEqual(180, board.AGENT_STALL_SEC)

    def test_a_ts_ms_beijing(self):
        """服务端 ts_ms：按 +08:00 解析（机器时区无关），与 ts 一致"""
        import calendar
        import board
        # 2026-09-18 02:00:00 (+08:00) == 2026-09-17 18:00:00 UTC
        expect = calendar.timegm((2026, 9, 17, 18, 0, 0, 0, 0, 0)) * 1000
        self.assertEqual(expect, board._ts_ms("2026-09-18 02:00:00"))
        self.assertIsNone(board._ts_ms(None))
        self.assertIsNone(board._ts_ms("not-a-time"))

    def test_a_render_wiring(self):
        """接线：状态行渲染走共享卡片（转义/结构在共享构造器）+ done 常驻 + tick/指针/回前台 + ts_ms"""
        src, js = _script_block()
        render_fn = _extract_function(js, "renderAgentChatStatus")
        for needle in ("agentActivityModel(", "setStatusNoInput(el, !m.canInput)",
                       "restartDispatcherFromStatus()", "event.stopPropagation()",
                       "newAgentSession(", "applyChatStatusCard(el, m, act + newLink);"):
            self.assertIn(needle, render_fn, "renderAgentChatStatus 缺少 %s" % needle)
        # #430：文本节点级 tick 与判定依据转义在共享实现里（单一源），本视图只负责重算模型与整卡重建
        tick_fn = _extract_function(js, "agentStatusTickOnce")
        for needle in ("agentStatusPointerDown", "agentStatusNowMs()", "tickChatStatusCard(",
                       "renderAgentChatStatus(row, agentChatBusy)"):
            self.assertIn(needle, tick_fn, "agentStatusTickOnce 缺少 %s" % needle)
        # 状态行常驻：done 不再整块置空，只加 no-input
        session_fn = _extract_function(js, "agentSessionHtml")
        self.assertNotIn("i.status === 'done'\n                ? ''", session_fn)
        self.assertIn("agent-chat${(row.req_status || i.status) === 'done' ? ' no-input' : ''}", session_fn)
        # 事件流最后一条（服务端 ts_ms）与指针/回前台接线
        self.assertIn("lastProcEventAtMs = events.reduce((mx, e) => Math.max(mx, e.ts_ms || 0), 0) || null;", src)
        self.assertIn("document.addEventListener('pointerdown'", src)
        self.assertIn("agentStatusTickOnce();", src)
        # 展开态复位（切换会话/全量重建）
        self.assertIn("agentStatusManualOpen = null;", src)
        self.assertIn("lastProcEventAtMs = null;", src)


if __name__ == "__main__":
    unittest.main()
