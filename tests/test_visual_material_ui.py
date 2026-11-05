#!/usr/bin/env python3
"""#10 看板视觉质感升级 + #15 皮肤系统：源码文本断言（纯 CSS 落在 static/board.css，#33 外置）。

本用例只做源码文本断言（不起服务、不建 worktree、不触 build_prompt/spawn_agent，零副作用），锁定：

- 令牌齐备 + 双皮肤令牌块存在（:root 深色默认 / :root[data-skin="light"] 浅色覆盖）
- 圆角单一源（--radius 面板 / --radius-card 卡片）
- 三区 + 摘要条玻璃：基态实体色降级、@supports 命中才换半透明 + backdrop-filter
- 卡片默认/hover/selected 三态：hover 零位移、selected 语义经令牌表达（#19 起无角标装饰）
- 键盘 focus 可见（:focus-visible 兜底 + 三组输入 :focus 补 outline）
- 语义色（徽标 / 流程 chip / 状态卡片 / 健康角标）规则体引用令牌、值落令牌块
- 既有被逐字锁定的规则文本未动

#15 迁移口径：断言对象从「规则体内字面量」改为「① 规则体引用 `var(--x)` + ② 令牌块内该令牌值」。
六面浅色值按原型 `body.light-mode`；其余面浅色 = 改造前字面量（表 C）。详见 docs/design-皮肤系统.md。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_visual_material_ui.py -q
"""
import re
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css

REPO = Path(__file__).resolve().parent.parent
SRC = frontend_css()
# 去注释后解析规则：避免说明文字被当作选择器（影响精确选择器匹配）或被负向断言误扫
CSS = re.sub(r"/\*.*?\*/", "", SRC, flags=re.S)

_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _rules(src):
    """返回 [(选择器列表, 规则体)]；选择器列表按逗号拆分并归一化空白"""
    out = []
    for m in _RULE_RE.finditer(src):
        parts = [re.sub(r"\s+", " ", p).strip() for p in m.group(1).split(",")]
        out.append((parts, m.group(2)))
    return out


def _rule(selector, src=CSS):
    """取精确选择器（逗号列表中的一项）对应的规则体；缺失返回 None"""
    for parts, body in _rules(src):
        if selector in parts:
            return body
    return None


def _supports_block(src=CSS):
    """截取 @supports 玻璃块的正文（花括号配平）"""
    i = src.find("@supports ((-webkit-backdrop-filter")
    if i < 0:
        return None
    j = src.find("{", i)
    depth, k = 1, j + 1
    while k < len(src) and depth:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
        k += 1
    return src[j + 1:k - 1]


ROOT = _rule(":root")            # 深色默认（表 A/B/C 深色值）
LIGHT = _rule(':root[data-skin="light"]')  # 浅色覆盖


class VisualMaterialTest(unittest.TestCase):
    # ---- T1 令牌齐备 ----
    def test_T1_tokens_present(self):
        self.assertIsNotNone(ROOT, "缺少 :root 令牌块")
        for token in ("--glass-nav", "--glass-list", "--glass-detail", "--glass-project",
                      "--glass-nav-solid", "--glass-list-solid", "--glass-detail-solid",
                      "--glass-project-solid", "--glass-border", "--glass-highlight",
                      "--card-bg", "--card-border", "--card-border-hover", "--card-shadow",
                      "--card-shadow-hover", "--card-sel-border", "--card-sel-shadow",
                      "--panel-shadow", "--hover-fill",
                      "--canvas-bg", "--fluid-color-a", "--fluid-color-b", "--fluid-color-c",
                      "--fluid-veil", "--fluid-opacity", "--fluid-fallback", "--glass-filter",
                      "--mono", "--radius-card"):
            self.assertIn(token + ":", ROOT, "缺少令牌 %s" % token)
        self.assertIsNotNone(LIGHT, "缺少浅色令牌块 :root[data-skin=\"light\"]")
        for token in ("--canvas-bg", "--glass-bg", "--glass-border", "--text", "--muted",
                      "--accent", "--cyan", "--success", "--orange", "--danger",
                      "--glass-shadow", "--card-bg", "--card-shadow", "--card-sel-border",
                      "--card-sel-shadow", "--rich-bg", "--rich-bd", "--rich-fg",
                      "--field-bg", "--field-fg", "--btn-bg", "--warn-fg"):
            self.assertIn(token + ":", LIGHT, "浅色块缺少令牌 %s" % token)

    # ---- T2 浅色令牌逐值（表 A/B 六面 = 原型 body.light-mode） ----
    def test_T2_light_token_values(self):
        for needle in ("--canvas-bg: #fafafa", "--glass-bg: #ffffff", "--glass-border: #e5e7eb",
                       "--glass-shadow: 0 1px 2px rgba(17, 24, 39, 0.035)",
                       "--text: #111827", "--muted: #6b7280", "--accent: #2563eb",
                       "--cyan: #2563eb", "--success: #22c55e", "--orange: #b45309",
                       "--danger: #be123c", "--card-bg: #ffffff", "--card-shadow: none",
                       "--card-sel-border: #2563eb", "--card-sel-shadow: 0 0 0 1px rgba(37, 99, 235, 0.08)",
                       "--rich-bg: #f8fafc", "--rich-bd: #e5e7eb", "--rich-fg: #374151",
                       "--text-2: #6b7280", "--text-3: #9ca3af"):
            self.assertIn(needle, LIGHT, "浅色令牌偏离原型：%s" % needle)
        # 深色默认（表 A 原型 :root）
        for needle in ("--canvas-bg: #050d18", "--glass-bg: rgba(5, 22, 48, 0.72)",
                       "--glass-border: rgba(141, 203, 255, 0.16)",
                       "--text: #edf8ff", "--muted: #8da9c5", "--accent: #3b82f6",
                       "--cyan: #22d3ee", "--success: #26d69a", "--orange: #e05e36",
                       "--danger: #ff5b6e", "--radius: 14px"):
            self.assertIn(needle, ROOT, "深色默认偏离原型：%s" % needle)

    # ---- T3 圆角单一源 ----
    def test_T3_radius_single_source(self):
        self.assertIn("--radius: 14px", ROOT)
        self.assertIn("--radius-card: 12px", ROOT)
        self.assertIn("border-radius: var(--radius-card)", _rule(".card"))
        for sel in (".nav-col", ".list-col", ".project-bar", ".detail-pane"):
            body = _rule(sel)
            self.assertIsNotNone(body, "缺少规则 %s" % sel)
            self.assertIn("border-radius: var(--radius)", body, "%s 未用统一圆角令牌" % sel)
        # 列宽钳制数学零变更：.list-col 不加 border/padding（仅既有 padding-right）
        lc = _rule(".list-col")
        self.assertNotIn("border:", lc)
        self.assertNotIn("padding:", lc)

    # ---- T4 玻璃降级（验收 7） ----
    def test_T4_glass_fallback(self):
        for sel in (".nav-col", ".list-col", ".project-bar", ".detail-pane"):
            body = _rule(sel)
            self.assertNotIn("rgba(", body, "%s 基态 background 必须是实体色（降级态）" % sel)
            self.assertIn("var(--glass-", body, "%s 基态须用实体降级令牌" % sel)
        block = _supports_block()
        self.assertIsNotNone(block, "缺少 @supports 玻璃降级块")
        for sel in (".nav-col", ".project-bar", ".detail-pane"):
            b = _rule(sel, block)
            self.assertIsNotNone(b, "玻璃块缺 %s" % sel)
            self.assertIn("backdrop-filter: var(--glass-filter)", b, sel)
            self.assertIn("-webkit-backdrop-filter", b, sel)
        lb = _rule(".list-col", block)
        self.assertIsNotNone(lb, "玻璃块缺 .list-col")
        self.assertIn("var(--glass-list)", lb)
        self.assertNotIn("backdrop-filter", lb, "滚动容器 .list-col 不得加 backdrop-filter")
        self.assertIn("--glass-filter: none", LIGHT, "浅色须关闭模糊")

    # ---- T5 卡片默认态 ----
    def test_T5_card_default(self):
        body = _rule(".card")
        self.assertIn("var(--card-bg)", body)
        self.assertIn("var(--card-border)", body)
        self.assertIn("box-shadow: var(--card-shadow)", body)
        self.assertIn("position: relative", body)
        self.assertNotIn("transform", body)

    # ---- T6 hover 不跳布局（验收 4） ----
    def test_T6_hover_no_layout_shift(self):
        body = _rule(".card:hover")
        self.assertIsNotNone(body)
        self.assertIn("var(--card-bg-hover)", body)
        self.assertIn("var(--card-border-hover)", body)
        self.assertIn("var(--card-shadow-hover)", body)
        for bad in ("padding", "margin", "width", "height", "border-width", "transform"):
            self.assertNotIn(bad, body, "hover 不得改 %s（会跳布局）" % bad)

    # ---- T7 selected 语义（验收 5：#19 去掉朋克角标） ----
    def test_T7_selected_semantics(self):
        sel = _rule(".card.selected")
        self.assertIn("var(--card-sel-border)", sel)
        self.assertIn("var(--card-sel-shadow)", sel)
        self.assertNotIn("background:", sel, "选中态不改卡片底色（两皮肤均由边/环表达）")
        self.assertNotEqual(sel, _rule(".card:hover"), "selected 阴影/语义须明显强于 hover")
        # #19：角标已移除，两条伪元素规则不得再存在（撤销即回归失败）
        self.assertIsNone(_rule(".card.selected::before"), "选中态左上有角标规则残留")
        self.assertIsNone(_rule(".card.selected::after"), "选中态右下有角标规则残留")
        self.assertNotIn("punk", SRC, "punk 令牌/引用须清零")

    # ---- T8 语义色：规则引用令牌 + 令牌块落值 ----
    def test_T8_semantic_colors_tokens(self):
        for sel, (bg, fg, dot) in {
            ".badge.open": ("--st-open-bg", "--st-open-fg", "--st-open-pt"),
            ".badge.later": ("--st-later-bg", "--st-later-fg", "--st-later-pt"),
            ".badge.doing": ("--st-doing-bg", "--st-doing-fg", "--st-doing-pt"),
            ".badge.done": ("--st-done-bg", "--st-done-fg", "--st-done-pt"),
            ".badge.route": ("--st-route-bg", "--st-route-fg", "--st-route-pt"),
            ".badge.review": ("--st-review-bg", "--st-review-fg", "--st-review-pt"),
            ".badge.dropped": ("--st-dropped-bg", "--st-dropped-fg", "--st-dropped-pt"),
        }.items():
            body = _rule(sel)
            self.assertIn("background: var(%s)" % bg, body, sel)
            self.assertIn("color: var(%s)" % fg, body, sel)
            self.assertIn("background: var(%s)" % dot, _rule(sel + "::before"), sel)
        # 其余面浅色 = 改造前字面量（表 C「保」）
        for token, val in (("--st-open-bg", "#fffbeb"), ("--st-later-bg", "#f5f3ff"),
                           ("--st-doing-bg", "#eff6ff"), ("--st-done-bg", "#f0fdf4"),
                           ("--st-route-bg", "#fff7ed"), ("--st-review-bg", "#fef2f2"),
                           ("--st-dropped-bg", "#f3f4f6")):
            self.assertIn("%s: %s" % (token, val), LIGHT, "浅色语义色须保持改造前：%s" % token)
        for sel in (".chat-status.st-green", ".chat-status.st-amber", ".chat-status.st-red",
                    ".chat-status.st-blue", ".chat-status.st-gray"):
            body = _rule(sel)
            self.assertIn("var(--cs-", body, sel)
            self.assertNotIn("#", body, "%s 不得含颜色字面量" % sel)
        self.assertIn("background: var(--cs-gray-bg)", _rule(".chat-status.st-gray"))
        self.assertIn("color: var(--cs-gray-fg)", _rule(".chat-status.st-gray"))
        for token in ("--health-ok-dot", "--health-bad-dot", "--health-warn-dot",
                      "--health-warn-fg"):
            self.assertIn("var(%s)" % token, _rule(".health.ok .dot") + _rule(".health.bad .dot")
                          + _rule(".health.warn .dot") + _rule(".health.warn"), token)
        self.assertIn("--health-ok-dot: #16a34a", LIGHT)
        self.assertIn("--health-bad-dot: #dc2626", LIGHT)
        self.assertIn("--health-warn-dot: #3b82f6", LIGHT)  # 提示档改蓝：橙色只留给「需要你决定」

    # ---- T9 摘要条（验收 3） ----
    def test_T9_project_bar(self):
        body = _rule(".project-bar")
        for needle in ("var(--glass-project-solid)", "var(--glass-border)",
                       "var(--panel-shadow)", "inset 0 1px 0", "var(--panel-grad)",
                       "var(--panel-inset-bottom)"):
            self.assertIn(needle, body, "摘要条缺少 %s" % needle)
        self.assertIn("flex: 1 1 260px", _rule(".pb-left"))
        self.assertIn("margin-left: auto", _rule(".pb-right"))
        self.assertIn("font-family: var(--mono)", _rule(".pb-name"))

    # ---- T10 focus 可见（可用性） ----
    def test_T10_focus_visible(self):
        self.assertIn("outline: 2px solid var(--accent)", _rule(":focus-visible"))
        for sel in (".add-pane input:focus", ".agent-chat .chat-input-row textarea:focus",
                    ".modal input:focus"):
            body = _rule(sel)
            self.assertIsNotNone(body, "缺少规则 %s" % sel)
            self.assertIn("outline: 2px solid var(--accent)", body, "%s 未补 outline" % sel)
        self.assertEqual(SRC.count("outline: none"), 3, "基态 outline 清零声明计数不得变（禁新增）")

    # ---- T11 无新增动画 ----
    def test_T11_no_new_animation(self):
        self.assertEqual(SRC.count("animation:"), 9, "基线 9 处 animation 声明不得增减（#20 页头 headline-sweep + reduced-motion 关闭各 +1；卡片流程进度条当前段呼吸 + 阻塞段关闭 + reduced-motion 关闭 +3）")
        self.assertEqual(SRC.count("will-change"), 0)
        for sel in (".nav-col", ".list-col", ".project-bar", ".detail-pane", ".card",
                    ".card.selected",
                    ":focus-visible"):
            self.assertNotIn("animation", _rule(sel), sel)

    # ---- T12 既有锁定规则文本未动 ----
    def test_T12_locked_rules_untouched(self):
        self.assertIn(".workspace.col-resizing .list-col { transition: none; }", SRC)
        chat = _rule(".chat-status")
        self.assertIn("border: 1px solid var(--border)", chat)
        self.assertIn("border-radius: 8px", chat)
        self.assertIn("overflow: hidden", chat)
        for sel, token in ((".chip-flow-green", "--cf-green-fg"), (".chip-flow-indigo", "--cf-indigo-fg"),
                           (".chip-flow-fuchsia", "--cf-fuchsia-fg"), (".chip-flow-slate", "--cf-slate-fg")):
            self.assertIn("color: var(%s)" % token, _rule(sel), sel)
        for token, val in (("--cf-green-fg", "#15803d"), ("--cf-indigo-fg", "#4338ca"),
                           ("--cf-fuchsia-fg", "#a21caf"), ("--cf-slate-fg", "#64748b")):
            self.assertIn("%s: %s" % (token, val), LIGHT, "浅色 chip 须保持改造前：%s" % token)
        self.assertIn("margin-left: calc(5ch + 10px)", _rule(".ev-confirm-card"))
        self.assertIn("animation: card-flash 2s ease;", _rule(".card.flash"))


if __name__ == "__main__":
    unittest.main()
