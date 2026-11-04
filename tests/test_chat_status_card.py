#!/usr/bin/env python3
"""#430 三处状态行统一为同一张状态卡片：共享组件接线 + 两个新增状态模型的行为矩阵。

背景：需求助手（#reqChatStatus）与聊天建卡（#chatStatus）原先是「居中小灰字」状态行，
与调度器会话（#agentChatStatus）的活性状态卡片不一致（#421 只把卡片 scope 在 .agent-chat 内）。
本用例锁定统一后的契约：
1. 单一实现——卡片 CSS 规则不带视图前缀（三处共用），三处状态行同一 DOM 结构；
   差异只允许落在各自的状态模型纯函数（agentActivityModel / reqChatActivityModel /
   chatAddActivityModel）。
2. 共享渲染/交互：chatStatusCardHtml / applyChatStatusCard / setStatusNoInput /
   toggleChatStatusDetail / tickChatStatusCard / chatStatusTick / ensureChatStatusTicker。
3. 两个新模型的状态矩阵与输入区显隐（node harness，见 tests/chat_status_card_harness.js）。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_chat_status_card -v
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css, frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
SRC = frontend_src()
HARNESS_JS = Path(__file__).resolve().parent / "chat_status_card_harness.js"

# 共享卡片 + 两个新模型（reqchat / 建卡页）所需函数；escapeHtml 为真实实现
NEEDED_FUNCS = ("escapeHtml", "reqChatEventMs", "reqChatActivityModel", "chatRoundSec",
                "chatAddActivityModel", "chatStatusCardHtml", "applyChatStatusCard",
                "setStatusNoInput", "tickChatStatusCard")


def _script():
    """#33：主脚本已外置 static/board.js，按函数名提取的取源即该文件全文。"""
    return frontend_js()


class ChatStatusCardHarnessTest(unittest.TestCase):
    """共享卡片构造/tick + 需求助手/建卡页状态模型（node 断言 A~E）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过状态卡片 harness")

    def _run_harness(self):
        js = _script()
        source = "\n\n".join(_extract_function(js, name) for name in NEEDED_FUNCS)
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

    def test_all_card_checks_pass(self):
        """A~E 全部断言通过（卡片构造/tick/no-input + 两个状态模型矩阵）"""
        data = self._run_harness()
        checks = data.get("checks", [])
        self.assertGreaterEqual(len(checks), 30, "harness 断言数不足: %d" % len(checks))
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)

    def test_sig_key_single_source(self):
        """data-k → signals 键映射单一源（渲染与 tick 共用，harness 依赖同值）"""
        self.assertIn("const CHAT_STATUS_SIG_KEY = {'事件流': 'event', '模型层': 'model', '判定': 'verdict'};", SRC)

    def test_model_tables_single_source(self):
        """harness 桩的文案/图标表与 static/board.js 逐行同值（防两处漂移，仿 #421 阈值注入做法）"""
        harness = HARNESS_JS.read_text(encoding="utf-8")
        for table in ("CHAT_STATUS_SIG_KEY", "REQ_CHAT_STATUS_ICO", "REQ_CHAT_CARD_LABEL",
                      "CHAT_ADD_STATUS_ICO"):
            body = re.search(r"const %s = \{(.*?)\};" % table, SRC, re.S)
            self.assertIsNotNone(body, "static/board.js 缺少 %s" % table)
            stub = re.search(r"const %s = \{(.*?)\};" % table, harness, re.S)
            self.assertIsNotNone(stub, "harness 缺少 %s 桩" % table)
            norm = lambda s: re.sub(r"\s+", "", s)
            self.assertEqual(norm(body.group(1)), norm(stub.group(1)),
                             "%s 与 harness 桩不一致（两处需同步）" % table)


class ChatStatusCardWiringTest(unittest.TestCase):
    """三处状态行的接线：同一套渲染/展开/tick/no-input，不再各写一份"""

    def setUp(self):
        self.js = _script()

    def test_three_views_share_card_renderer(self):
        """三处渲染函数都走 applyChatStatusCard（agent 保留自身状态模型与动作）"""
        for fn, needle in (("renderAgentChatStatus", "applyChatStatusCard(el, m, act + newLink);"),
                           ("renderReqChatStatus", "applyChatStatusCard(el, m, act);"),
                           ("renderChatStatus", "applyChatStatusCard(el, m, act);")):
            body = _extract_function(self.js, fn)
            self.assertIn(needle, body, "%s 未走共享卡片渲染" % fn)
            self.assertIn("setStatusNoInput(el, !m.canInput);", body, "%s 未走共享输入区显隐" % fn)

    def test_three_status_rows_same_markup(self):
        """三处状态行 DOM 同形（.chat-status 容器，内容全部由共享渲染写入）"""
        for builder in ("agentSessionHtml", "reqAssistantHtml", "addFormHtml"):
            body = _extract_function(self.js, builder)
            self.assertIn('<div class="chat-status" id="', body, "%s 状态行容器不合规" % builder)

    def test_shared_toggle_dispatch(self):
        """点头部展开只挂 .st-head，且按状态卡 id 分派到三处的手选展开态"""
        head = _extract_function(self.js, "chatStatusCardHtml")
        self.assertIn('onclick="toggleChatStatusDetail(this)"', head)
        self.assertIn("title=\"点击展开 / 收起判定依据\"", head)
        self.assertIn("${open ? '▼' : '▶'}", head)
        toggle = _extract_function(self.js, "toggleChatStatusDetail")
        self.assertIn("head.closest('.chat-status')", toggle)
        for needle in ("agentStatusManualOpen = open;", "reqChatStatusManualOpen = open;",
                       "chatAddStatusManualOpen = open;"):
            self.assertIn(needle, toggle, "toggleChatStatusDetail 缺少 %s" % needle)

    def test_single_ticker(self):
        """三处共用 1 个 1s ticker：按当前挂载的状态卡分派，档位没变只改文本节点"""
        tick = _extract_function(self.js, "chatStatusTick")
        for needle in ("agentStatusPointerDown", "agentChatStatus", "reqChatStatus", "chatStatus"):
            self.assertIn(needle, tick, "chatStatusTick 缺少 %s" % needle)
        self.assertIn("setInterval(() => { if (!document.hidden) chatStatusTick(); }, 1000);", self.js)
        # 旧的两套（agent 专用 ticker / 展开函数）已删除，避免双实现漂移
        self.assertNotIn("ensureAgentStatusTicker", SRC)
        self.assertNotIn("toggleAgentStatusDetail", SRC)
        self.assertNotIn("agentStatusTicker", SRC)

    def test_reqchat_status_recomputed_after_events(self):
        """防复发（#430 浏览器复核实测）：事件并入后必须重算一次需求助手状态卡——计时与「最后事件 Ns 前」
        由事件时间派生，首屏渲染时事件还没并进来；running 态有 1s tick 兜底，非 running 态没有 tick，
        漏这一步就会长期停在占位文案「—（尚无事件时间）」（点一下头部才恢复）。
        首次渲染仍须先于事件渲染（滚底前先定高，避免对话区停在距底几十像素处）。"""
        body = _extract_function(self.js, "refreshReqChat")
        i_events = body.index("renderReqChatEvents();")
        i_after = body.index("renderReqChatStatus();", i_events)
        self.assertGreater(i_after, i_events, "refreshReqChat 必须在 renderReqChatEvents 之后再渲染状态卡")
        self.assertLess(body.index("renderReqChatStatus();"), i_events,
                        "首次状态卡渲染仍须先于事件渲染（保滚动位置）")

    def test_agent_status_recomputed_after_events(self):
        """防复发（与需求助手侧同款缺陷的另一侧）：refreshProcess 拉到事件、更新「事件流」基准后
        必须立即补一次状态卡重算——首屏渲染先于事件并入，判定依据会停在占位「—（未加载执行过程）」；
        不补则自动展开档（红/琥珀）要多等一拍 1s tick，非 running 档没有 ticker 更是要等用户
        点一下头部才恢复（1s tick 只在展开态写判定依据）。

        实现只用文本级 chatStatusTick()：不得整卡重建——事件轮询最密 1s 一次，innerHTML 重建会
        冲掉判定依据的展开态与选中文字（#294 选区守卫同类风险）。"""
        body = _extract_function(self.js, "refreshProcess")
        i_assign = body.index("lastProcEventAtMs = events.reduce")
        # 行首真实调用（行内注释里的同名子串不算数——只看子串会让「注释掉这行」蒙混过关）
        m = re.search(r"(?m)^[ \t]*chatStatusTick\(\);", body)
        self.assertIsNotNone(m, "refreshProcess 未补状态卡重算（chatStatusTick 被删或仅存在于注释里）")
        self.assertGreater(m.start(), i_assign, "必须在更新事件流基准之后再重算状态卡")
        self.assertNotIn("applyChatStatusCard", body, "refreshProcess 不得整卡重建（防冲掉展开态/选区）")


class ChatStatusCardCssTest(unittest.TestCase):
    """卡片 CSS 单一实现（不带视图前缀）+ .no-input 只隐藏输入件"""

    def _rule(self, selector):
        m = re.search(r"(?m)^[ \t]*" + re.escape(selector) + r"[ \t]*\{([^}]*)\}", frontend_css())
        return m.group(1) if m else None

    def test_card_rules_unprefixed(self):
        """所有 .st-* 卡片规则一律无视图前缀（三处共用；有前缀即为分叉实现）"""
        for m in re.finditer(r"(?m)^[ \t]*([^\n{]*\.chat-status[^\n{]*)\{", SRC):
            selector = m.group(1).strip()
            self.assertTrue(selector.startswith(".chat-status"),
                            "状态行卡片规则必须无视图前缀（三处共用）: %s" % selector)
            self.assertNotIn(".agent-chat .chat-status", selector)
        self.assertNotIn(".reqchat-pane .chat-status", SRC)
        self.assertNotIn(".add-pane .chat-status", SRC)
        self.assertNotIn(".chat-status.thinking", SRC)   # 呼吸小灰字已被卡片取代

    def test_no_input_hides_only_input_widgets(self):
        """不可输入只隐藏 textarea 与主按钮：直接子代选择器，卡片内动作按钮不受影响"""
        self.assertIn(".no-input .chat-input-row > textarea,\n"
                      "        .no-input .chat-input-row > button { display: none; }", SRC)
        self.assertNotIn(".agent-chat.no-input .chat-input-row textarea", SRC)

    def test_palette_and_detail_intact(self):
        """五档配色与判定依据两列样式在共享作用域内保持不变（#421 视觉不回归）"""
        for sel, needles in (
            (".chat-status", ["border: 1px solid var(--border)", "border-radius: 8px",
                              "font-size: 13px", "text-align: left"]),
            (".chat-status.st-green", ["var(--cs-green-bg)", "var(--cs-green-bd)", "var(--cs-green-fg)"]),
            (".chat-status.st-amber", ["var(--cs-amber-bg)", "var(--cs-amber-bd)", "var(--cs-amber-fg)"]),
            (".chat-status.st-red", ["var(--cs-red-bg)", "var(--cs-red-bd)", "var(--cs-red-fg)"]),
            (".chat-status.st-blue", ["var(--cs-blue-bg)", "var(--cs-blue-bd)", "var(--cs-blue-fg)"]),
            (".chat-status.st-gray", ["var(--cs-gray-bg)"]),
            (".chat-status .st-line", ["display: flex", "padding: 5px 12px"]),
            (".chat-status .st-k", ["width: 56px"]),
            (".chat-status .st-v.ok", ["var(--cs-ok-fg)"]),
            (".chat-status .st-v.warn", ["var(--cs-warn-fg)"]),
            (".chat-status .st-v.bad", ["var(--cs-bad-fg)", "font-weight: 600"]),
            (".chat-status .st-head.st-static", ["cursor: default"]),
        ):
            body = self._rule(sel)
            self.assertIsNotNone(body, "缺少 CSS 规则: %s" % sel)
            for needle in needles:
                self.assertIn(needle, body, "%s 规则缺少 %s" % (sel, needle))


if __name__ == "__main__":
    unittest.main()
