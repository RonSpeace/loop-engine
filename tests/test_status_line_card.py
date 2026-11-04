#!/usr/bin/env python3
"""#430 三处状态行统一为同一张状态卡片：CSS 规格 + 渲染接线锁定（#421 卡片化的延伸）。

背景：#421 把调度器会话的状态行卡片化（底色卡片 + 图标 + 左对齐层级 + 两列判定依据）时，
把整套样式 scope 在 .agent-chat 内，需求助手（#reqChatStatus）与聊天建卡（#chatStatus）仍是
「居中小灰字」——同一产品里两个页面底部状态栏观感/交互不一致（用户走查结论）。#430 把卡片
提升为三处共用的单一实现，本用例锁定：

- 卡片规则**不带视图前缀**（有前缀即为分叉实现）
- 卡片容器/五档配色/图标位/两列判定依据（视觉不回归 #421 走查结论）
- 交互：点头部展开只挂 .st-head；展开态由各视图手选变量记忆；箭头 ▶/▼ 与会话头部折叠同款
- tick：1s 重绘同步图标/主文案/计时，并按 signalSev 重着色值列
- 不可输入只隐藏输入件（直接子代选择器，卡片内动作按钮不受影响）

共享卡片构造与两个新状态模型（reqchat / 建卡页）的行为矩阵见 tests/test_chat_status_card.py。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_status_line_card -v
"""
import re
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css, frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
SRC = frontend_src()
CSS = frontend_css()
CARD = ".chat-status"


def _rule(selector, src=CSS):
    """取 CSS 规则体（精确选择器；缺失返回 None，由调用方给出断言信息）"""
    m = re.search(r"(?m)^[ \t]*" + re.escape(selector) + r"[ \t]*\{([^}]*)\}", src)
    return m.group(1) if m else None


def _script():
    """#33：主脚本已外置 static/board.js，按函数名提取的取源即该文件全文。"""
    return frontend_js()


class StatusLineCardCssTest(unittest.TestCase):
    """#421 交互稿视觉层（卡片容器 / 状态配色 / 图标位 / 两列判定依据）+ #430 单一实现"""

    def _assert_rule(self, selector, needles):
        body = _rule(selector)
        self.assertIsNotNone(body, "缺少 CSS 规则: %s" % selector)
        for needle in needles:
            self.assertIn(needle, body, "%s 规则缺少 %s" % (selector, needle))

    def test_card_container(self):
        """状态行是卡片：边框 + 圆角 + 裁切 + 13px + 左对齐 + 状态切换过渡（交互稿 .statusbar）"""
        self._assert_rule(CARD, ["border: 1px solid var(--border)", "border-radius: 8px",
                                 "overflow: hidden", "font-size: 13px", "text-align: left",
                                 "transition: background"])

    def test_state_palette(self):
        """五档状态 = 底色 + 描边 + 同色系文字（交互稿 .st-green/.st-amber/.st-red/.st-blue/.st-gray）"""
        palette = {
            "st-green": ("var(--cs-green-bg)", "var(--cs-green-bd)", "var(--cs-green-fg)"),
            "st-amber": ("var(--cs-amber-bg)", "var(--cs-amber-bd)", "var(--cs-amber-fg)"),
            "st-red": ("var(--cs-red-bg)", "var(--cs-red-bd)", "var(--cs-red-fg)"),
            "st-blue": ("var(--cs-blue-bg)", "var(--cs-blue-bd)", "var(--cs-blue-fg)"),
            "st-gray": ("var(--cs-gray-bg)", None, None),
        }
        for state, (bg, line, fg) in palette.items():
            body = _rule("%s.%s" % (CARD, state))
            self.assertIsNotNone(body, "缺少状态配色规则: %s.%s" % (CARD, state))
            self.assertIn("background: %s" % bg, body)
            if line:
                self.assertIn("border-color: %s" % line, body)
            if fg:
                self.assertIn("color: %s" % fg, body)
        # #15：值落令牌块（浅色 == 改造前字面量，表 C「保」）
        for token, val in (("--cs-green-bg", "#f0fdf4"), ("--cs-green-bd", "#bbf7d0"),
                           ("--cs-green-fg", "#15803d"), ("--cs-amber-bg", "#fffbeb"),
                           ("--cs-amber-bd", "#fde68a"), ("--cs-amber-fg", "#b45309"),
                           ("--cs-red-bg", "#fef2f2"), ("--cs-red-bd", "#fecaca"),
                           ("--cs-red-fg", "#b91c1c"), ("--cs-blue-bg", "#eff6ff"),
                           ("--cs-blue-bd", "#bfdbfe"), ("--cs-blue-fg", "#1d4ed8"),
                           ("--cs-gray-bg", "#f9fafb"), ("--cs-gray-fg", "#6b7280")):
            self.assertIn("%s: %s" % (token, val), SRC, "浅色令牌缺 %s: %s" % (token, val))

    def test_icon_and_hierarchy(self):
        """信息层级：18px 图标位 + 主文案加粗占满剩余宽度（把计时/箭头推到右侧）"""
        self._assert_rule(CARD + " .st-ico", ["width: 18px", "font-size: 15px"])
        self._assert_rule(CARD + " .st-main", ["flex: 1", "font-weight: 650"])
        self._assert_rule(CARD + " .st-head", ["display: flex", "padding: 9px 12px", "cursor: pointer"])
        # hover 反馈（交互稿 .bar-head:hover）
        self.assertIsNotNone(_rule(CARD + " .st-head:hover"))
        # 无判定依据的卡片（一次性提示/接口错误）：头部不可点击
        self._assert_rule(CARD + " .st-head.st-static", ["cursor: default"])

    def test_detail_two_columns_with_severity(self):
        """判定依据三行 = 键列 + 值列；值列按严重度三色（交互稿 .sig .k/.v.ok/.v.warn/.v.bad）"""
        self._assert_rule(CARD + " .st-line", ["display: flex", "padding: 5px 12px", "font-size: 12.5px"])
        self._assert_rule(CARD + " .st-k", ["width: 56px", "flex: none"])
        self._assert_rule(CARD + " .st-v", ["flex: 1"])
        self._assert_rule(CARD + " .st-v.ok", ["var(--cs-ok-fg)"])
        self._assert_rule(CARD + " .st-v.warn", ["var(--cs-warn-fg)"])
        self._assert_rule(CARD + " .st-v.bad", ["var(--cs-bad-fg)", "font-weight: 600"])
        # 展开面板：分隔线 + 半透明白底（交互稿 .bar-detail；#15 收敛为令牌）
        self._assert_rule(CARD + " .st-detail", ["display: none", "border-top: 1px solid var(--cs-detail-bd)",
                                                 "background: var(--cs-detail-bg)"])
        self._assert_rule(CARD + ".open .st-detail", ["display: block"])

    def test_card_single_source(self):
        """#430 单一实现：卡片规则一律无视图前缀（三处共用）；旧的居中小灰字与作用域隔离已删除"""
        for m in re.finditer(r"(?m)^[ \t]*([^\n{]*\.chat-status[^\n{]*)\{", SRC):
            selector = m.group(1).strip()
            self.assertTrue(selector.startswith(CARD),
                            "状态行卡片规则必须无视图前缀（三处共用同一套）: %s" % selector)
        # 三处状态行不再各带一份卡片覆盖规则
        for scoped in (".agent-chat .chat-status", ".reqchat-pane .chat-status", ".add-pane .chat-status"):
            self.assertNotIn(scoped, SRC, "卡片规则不应再按视图分叉: %s" % scoped)
        # #322 的居中小灰字呼吸已被卡片 + 计时取代（全站只剩会话 id 一处呼吸，见 #426 用例）
        self.assertNotIn(".chat-status.thinking", SRC)
        self.assertNotIn("chatStatusBreath", SRC)

    def test_no_input_hides_only_input_widgets(self):
        """不可输入只隐藏输入件（textarea 与主按钮）：直接子代选择器，卡片内动作按钮不受影响"""
        self.assertIn(".no-input .chat-input-row > textarea,\n"
                      "        .no-input .chat-input-row > button { display: none; }", SRC)
        self.assertNotIn(".agent-chat.no-input .chat-input-row textarea", SRC)
        self.assertNotIn(".agent-chat.no-input #agentChatSend", SRC)


class StatusLineCardWiringTest(unittest.TestCase):
    """渲染/交互接线：共享构造器 + 单一 ticker + 头部点击分派"""

    def setUp(self):
        self.js = _script()

    def test_head_only_click(self):
        """点击展开只挂头部：点判定依据正文不再收起（正文可选可复制）；三处状态行容器同形"""
        card = _extract_function(self.js, "chatStatusCardHtml")
        self.assertIn('onclick="toggleChatStatusDetail(this)"', card)
        self.assertIn('title="点击展开 / 收起判定依据"', card)
        for builder in ("agentSessionHtml", "reqAssistantHtml", "addFormHtml"):
            body = _extract_function(self.js, builder)
            self.assertIn('<div class="chat-status" id="', body, "%s 状态行容器不合规" % builder)
            self.assertNotIn('id="agentChatStatus" onclick', body)

    def test_arrow_matches_session_header(self):
        """箭头字形与会话头部折叠同款 ▶/▼（交互稿明确要求；▸/▾ 已弃用）"""
        card = _extract_function(self.js, "chatStatusCardHtml")
        self.assertIn("${open ? '▼' : '▶'}", card)
        head = _extract_function(self.js, "sessHeadHtml")
        self.assertIn("${open ? '▼' : '▶'}", head)
        for glyph in ("▸", "▾"):
            self.assertNotIn(glyph, SRC, "状态行不应再使用 %s" % glyph)

    def test_render_structure(self):
        """DOM：图标位 + 主文案 + 计时 + 动作 + 行尾箭头 + 三行键值判定依据"""
        card = _extract_function(self.js, "chatStatusCardHtml")
        for needle in ('class="st-ico"', 'class="st-main"', 'class="st-timer"', 'class="st-act"',
                       'class="st-arw"', 'class="st-k"', 'class="st-v '):
            self.assertIn(needle, card, "状态行 DOM 缺少 %s" % needle)
        for k in ("事件流", "模型层", "判定"):
            self.assertIn("sigRow('%s', sg." % k, card, "判定依据缺少 %s 行" % k)
        # 箭头在动作之后（行尾），不再是行首
        self.assertLess(card.index('class="st-arw"'), card.index('class="st-detail"'))

    def test_tick_updates_text_and_severity(self):
        """1s 重绘：同档位内同步图标/主文案/计时，并按 signalSev 重着色值列"""
        tick = _extract_function(self.js, "tickChatStatusCard")
        for needle in ("setTxt('.st-ico'", "setTxt('.st-main'", "setTxt('.st-timer'",
                       "CHAT_STATUS_SIG_KEY", "v.className = 'st-v '"):
            self.assertIn(needle, tick, "tickChatStatusCard 缺少 %s" % needle)
        # 档位变化整卡重建交给调用方（rebuild 回调），同档位不重建
        self.assertIn("if (el.dataset.kind !== m.kind) { rebuild(); return; }", tick)


if __name__ == "__main__":
    unittest.main()
