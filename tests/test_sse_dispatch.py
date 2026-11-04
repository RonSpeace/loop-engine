#!/usr/bin/env python3
"""req-416 前端 SSE 分发/降级 harness 包装（node 执行 tests/sse_dispatch_harness.js）。

用例见 harness 内 H1-H12：proc 分发与自筛 / sessions poke 差异重绘（真 fetch 链）/ chat 与 reqchat 自筛 /
init 对齐 / 永久失败（CLOSED）立即降级+30s 退避 / 瞬时失败（CONNECTING）×2 降级与 onopen 复位 /
单连接（切换 5 次存活 ≤1）/ 开关 / 后台标签页暂缓与回前台补刷 /
降级瞬间真实 reqchat 轮询链（#418：真 scheduleReqChatPoll 门控 + 定时器到点回调）/
注入 fetch 失败时对齐刷新仍执行且不产生 unhandled rejection（#418）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_sse_dispatch -v
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests._frontend_src import frontend_js  # noqa: E402

import board as b  # noqa: E402


class SseDispatchHarnessTest(unittest.TestCase):
    """渲染后的页面 JS 交 node harness 执行（提取 #416 代码块 + mock 环境断言）。"""

    def _rendered_js(self, path='/', query=''):
        """#33：主脚本已外置 static/board.js——执行对象 = 该文件全文，前置一段从渲染页原样取出的
        配置脚本（window.__REQ_BOARD_CFG__），与浏览器实际执行序一致（配置先于主脚本）。"""
        resp = b.app.test_client().get(path)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        m = re.search(r"<script>(window\.__REQ_BOARD_CFG__=.*?)</script>", html, re.S)
        self.assertIsNotNone(m, "首页应有 window.__REQ_BOARD_CFG__ 配置脚本")
        return m.group(1) + "\n" + (REPO / "static" / "board.js").read_text(encoding="utf-8")

    def test_sse_dispatch_harness(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用，跳过前端 SSE harness")
        harness = REPO / "tests" / "sse_dispatch_harness.js"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8", delete=False) as f:
            f.write(self._rendered_js())
            tmp = f.name
        try:
            proc = subprocess.run([node, str(harness), tmp], capture_output=True, text=True, timeout=120)
        finally:
            Path(tmp).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, f"harness 失败:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("ALL PASS", proc.stdout)

    def test_visibilitychange_wiring(self):
        """回前台补一次 poke 的挂点存在（visibilitychange 处理器内调用 sseAlignView）。"""
        js = self._rendered_js()
        self.assertRegex(js, r"visibilitychange[\s\S]{0,200}ssePendingPoke[\s\S]{0,80}sseAlignView")


class RenderListSignatureTest(unittest.TestCase):
    """#426：注册表稳定键 + renderList HTML 签名短路的静态接线断言（W1-W3）。

    缺陷链：GET /api/agent-sessions 每行附带的 server_now 每请求现取 → 两处 JSON.stringify
    快照比较永久判「有变化」→ renderList 无条件写 #list innerHTML → 卡片节点重建、呼吸动画
    从 0% 重启。防线 A = 稳定键剔除 server_now；防线 B = 签名相同不写 DOM。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = frontend_js()  # #33：主脚本外置在 static/board.js（W1-W3 全为 JS 断言）

    @staticmethod
    def _func_span(src, name):
        """提取函数源码区间（起点，终点）——从 `function <name>(` 起按大括号配平。"""
        start = src.index("function %s(" % name)
        depth = 0
        for i in range(src.index("{", start), len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        raise AssertionError("函数 %s 大括号不配平" % name)

    def test_w1_agent_sessions_stable_key_wiring(self):
        """W1：稳定键纯函数存在且只剔 server_now；SSE poke 与 15 秒轮询两处比较都改用它；
        被 server_now 击穿的 JSON.stringify 直比较形态必须清零。"""
        self.assertIn("function agentSessionsKey(rows) {", self.src)
        self.assertIn("const {server_now, ...rest} = r;", self.src)
        self.assertNotIn("JSON.stringify(agentSessions", self.src)
        self.assertNotIn("JSON.stringify(latestAgentSessions)", self.src)
        body = self.src[self.src.index("async function sseRefreshSessions()"):]
        body = body[:self._func_span(self.src, "sseRefreshSessions")[1]
                    - self.src.index("async function sseRefreshSessions()")]
        self.assertIn("const before = agentSessionsKey(agentSessions);", body)
        self.assertIn("if (agentSessionsKey(agentSessions) === before) return;", body)
        auto = self.src[self.src.index("async function autoRefresh()"):]
        auto = auto[:self._func_span(self.src, "autoRefresh")[1]
                    - self.src.index("async function autoRefresh()")]
        self.assertIn("agentSessionsKey(latestAgentSessions) === agentSessionsKey(agentSessions)", auto)

    def test_w2_render_list_signature_short_circuit(self):
        """W2：#list 的 innerHTML 唯一写入口是 renderList；签名相同早退、empty-main 在早退之前
        （与 DOM 写入解耦），空态以空串为签名（幂等）。"""
        self.assertEqual(self.src.count("document.getElementById('list')"), 1)
        self.assertEqual(self.src.count("list.innerHTML"), 1)
        self.assertIn("let listHtmlSig = null;\n        function renderList() {", self.src)
        start, end = self._func_span(self.src, "renderList")
        body = self.src[start:end]
        self.assertIn("if (html === listHtmlSig) return;", body)
        self.assertIn("listHtmlSig = html;", body)
        i_empty = body.index("empty-main")
        i_early = body.index("if (html === listHtmlSig) return;")
        i_write = body.index("list.innerHTML = html;")
        self.assertLess(i_empty, i_early, "empty-main 的 class 维护必须先于签名早退（与 DOM 写入解耦）")
        self.assertLess(i_early, i_write, "签名早退必须先于 innerHTML 赋值（短路有效性）")
        self.assertEqual(body.count("list.innerHTML"), 1, "唯一写入口必须落在 renderList 内")

    def test_w3_card_html_no_wallclock_inputs(self):
        """W3：短路前提锁定——cardHtml/sessionFooterHtml 源码不含随墙钟变化的输入
        （Date.now/new Date/Math.random），否则签名会在心跳期恒定变化、短路失效。"""
        for name in ("cardHtml", "sessionFooterHtml"):
            start, end = self._func_span(self.src, name)
            body = self.src[start:end]
            for bad in ("Date.now(", "new Date(", "Math.random("):
                self.assertNotIn(bad, body, "%s 不应包含墙钟/随机输入 %s" % (name, bad))


if __name__ == "__main__":
    unittest.main()
