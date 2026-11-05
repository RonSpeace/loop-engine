#!/usr/bin/env python3
"""#15 看板皮肤系统：深空玻璃（默认）/ 浅色双皮肤与设计语言令牌化（源码文本断言，零副作用）。

本用例只解析 templates/board.html（HTML 结构）与 static/board.css、static/board.js（#33 外置的样式/脚本）
文本，不起服务、不建 worktree、不触 build_prompt/spawn_agent。视觉唯一基准 = 素材原型
（materials/index (1).html）：深色 = :root，浅色 = body.light-mode。逐条令牌对照见 docs/design-皮肤系统.md。

    .venv/bin/python -m pytest tests/test_skin_system.py -q
"""
import re
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css, frontend_js, frontend_src, frontend_text

REPO = Path(__file__).resolve().parent.parent
HTML = frontend_text()
SRC = frontend_src()
CSS = re.sub(r"/\*.*?\*/", "", frontend_css(), flags=re.S)

_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _rule(selector, src=CSS):
    for m in _RULE_RE.finditer(src):
        parts = [re.sub(r"\s+", " ", p).strip() for p in m.group(1).split(",")]
        if selector in parts:
            return m.group(2)
    return None


def _token_block(sel):
    """取令牌块全文（:root 或 :root[data-skin="light"]）"""
    return _rule(sel)


ROOT = _token_block(":root") or ""
LIGHT = _token_block(':root[data-skin="light"]') or ""


def _strip_token_blocks(css):
    """剥掉两个令牌块，用于「颜色字面量清零」护栏"""
    def cut(src, sel):
        m = re.search(re.escape(sel) + r"\s*\{", src)
        if not m:
            return src
        depth, k = 1, m.end()
        while k < len(src) and depth:
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
            k += 1
        return src[:m.start()] + src[k:]
    return cut(cut(css, ":root"), ':root[data-skin="light"]')


class SkinTokensTest(unittest.TestCase):
    # ---- S1 令牌表 A：逐值（深色 == 原型 :root；浅色 == 原型 body.light-mode） ----
    #      #16：网格令牌（--grid-minor/--grid-major）随网格底纹整体退场，改锁流体置换令牌
    def test_S1_table_a_values(self):
        for t, v in (("--canvas-bg", "#050d18"),
                     ("--fluid-color-a", "#06203c"), ("--fluid-color-b", "#0b4f7a"),
                     ("--fluid-color-c", "#22d3ee"), ("--fluid-veil", "#050d18"),
                     ("--fluid-opacity", "0.9"),
                     ("--glass-bg", "rgba(5, 22, 48, 0.72)"), ("--glass-strong", "rgba(4, 18, 40, 0.9)"),
                     ("--glass-border", "rgba(141, 203, 255, 0.16)"), ("--glass-blur", "14px"),
                     ("--glass-filter", "blur(14px) saturate(1.2)"),
                     ("--glass-shadow", "0 12px 32px rgba(0, 0, 0, 0.16)"),
                     ("--glass-highlight", "rgba(255, 255, 255, 0.045)"),
                     ("--text", "#edf8ff"), ("--muted", "#8da9c5"), ("--accent", "#3b82f6"),
                     ("--cyan", "#22d3ee"), ("--success", "#26d69a"), ("--orange", "#e05e36"),
                     ("--danger", "#ff5b6e"), ("--radius", "14px")):
            self.assertIn("%s: %s" % (t, v), ROOT, "深色表 A 偏离原型：%s" % t)
        self.assertIn("--fluid-fallback: radial-gradient(", ROOT, "深色缺流体静态渐变底兜底")
        for t, v in (("--canvas-bg", "#fafafa"),
                     ("--fluid-color-a", "#f4f8ff"), ("--fluid-color-b", "#dbeafe"),
                     ("--fluid-color-c", "#bfdbfe"), ("--fluid-veil", "#fafafa"),
                     ("--fluid-opacity", "0.6"),
                     ("--glass-bg", "#ffffff"),
                     ("--glass-strong", "#ffffff"), ("--glass-border", "#e5e7eb"),
                     ("--glass-filter", "none"), ("--text", "#111827"), ("--muted", "#6b7280"),
                     ("--accent", "#2563eb"), ("--cyan", "#2563eb"), ("--success", "#22c55e"),
                     ("--orange", "#b45309"), ("--danger", "#be123c")):
            self.assertIn("%s: %s" % (t, v), LIGHT, "浅色表 A 偏离原型：%s" % t)
        self.assertIn("--fluid-fallback: radial-gradient(", LIGHT, "浅色缺流体静态渐变底兜底")
        # #16 网格令牌整体退场（不留死令牌）
        self.assertNotIn("--grid-minor", SRC)
        self.assertNotIn("--grid-major", SRC)
        # 原型 --display/--body（外链字体）不引入
        self.assertNotIn("--display:", CSS)
        self.assertNotIn("--body:", CSS)

    # ---- S2 令牌表 B/C 逐值 + 口径 ----
    def test_S2_table_b_c_values(self):
        for t, v in (("--glass-shadow", "0 1px 2px rgba(17, 24, 39, 0.035)"),
                     ("--glass-highlight", "transparent"),
                     ("--panel-shadow", "0 4px 14px rgba(17, 24, 39, 0.055)"),
                     ("--panel-inset-top", "rgba(255, 255, 255, 0.9)"),
                     ("--card-bg", "#ffffff"), ("--card-shadow", "none"),
                     ("--card-shadow-hover", "none"), ("--card-sel-border", "#2563eb"),
                     ("--card-sel-shadow", "0 0 0 1px rgba(37, 99, 235, 0.08)"),
                     ("--text-2", "#6b7280"), ("--text-3", "#9ca3af"),
                     ("--rich-bg", "#f8fafc"), ("--rich-bd", "#e5e7eb"), ("--rich-fg", "#374151"),
                     ("--io-bg", "#f8fafc"), ("--io-pre-fg", "#374151"),
                     ("--field-bg", "#ffffff"), ("--field-bd", "#d1d5db"), ("--field-fg", "#111827"),
                     ("--btn-bg", "#ffffff"), ("--btn-bg-hover", "#f3f4f6")):
            self.assertIn("%s: %s" % (t, v), LIGHT, "浅色表 B 偏离：%s" % t)
        # 表 C 浅色 = 改造前字面量（保）
        for t, v in (("--st-open-bg", "#fffbeb"), ("--st-later-bg", "#f5f3ff"),
                     ("--st-doing-bg", "#eff6ff"), ("--st-done-bg", "#f0fdf4"),
                     ("--st-route-bg", "#fff7ed"), ("--st-review-bg", "#fef2f2"),
                     ("--st-dropped-bg", "#f3f4f6"), ("--cs-green-bg", "#f0fdf4"),
                     ("--cs-amber-bg", "#fffbeb"), ("--cs-red-bg", "#fef2f2"),
                     ("--cs-blue-bg", "#eff6ff"), ("--cs-gray-bg", "#f9fafb"),
                     ("--cf-green-bg", "#f0fdf4"), ("--cf-slate-bg", "#f8fafc"),
                     ("--toast-bg", "#ffffff"), ("--warn-fg", "#b45309"),
                     ("--health-ok-dot", "#16a34a"), ("--health-bad-dot", "#dc2626"),
                     ("--health-warn-dot", "#3b82f6")):  # 提示档改蓝：橙色只留给「需要你决定」
            self.assertIn("%s: %s" % (t, v), LIGHT, "浅色表 C 偏离：%s" % t)
        # 两表令牌名集合互不重叠（表 A/B 通用 vs 表 C 语义前缀不冲突）
        self.assertTrue(any(x in ROOT for x in ("--st-open-bg:",)))
        self.assertTrue(any(x in ROOT for x in ("--cs-green-bg:",)))

    # ---- S3 令牌单一源护栏 ----
    def test_S3_single_source_no_literals(self):
        rest = _strip_token_blocks(CSS)
        hexes = [h for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", rest) if re.search(r"[a-fA-F]", h)]
        self.assertEqual(hexes, [], "令牌块外仍有 hex 颜色字面量：%s" % hexes)
        rgbs = re.findall(r"rgba?\([^)]*\)", rest)
        self.assertEqual(rgbs, [], "令牌块外仍有 rgb()/rgba() 字面量：%s" % rgbs)

    # ---- S4 流体背景槽位（#16：替换原 body::before 双层网格底纹） ----
    def test_S4_fluid_slot(self):
        bg = _rule("#fluidBg")
        self.assertIsNotNone(bg, "缺少 #fluidBg 流体背景槽位规则")
        for needle in ("position: fixed", "inset: 0", "pointer-events: none", "z-index: 0",
                       "background-image: var(--fluid-fallback)", "opacity: var(--fluid-opacity)"):
            self.assertIn(needle, bg, "流体槽位缺 %s" % needle)
        self.assertIn("z-index: 1", _rule(".page") or "", "内容层须 z-index: 1 覆盖流体")
        self.assertNotIn("body::before", CSS, "旧网格底纹须整体删除")

    # ---- S5 玻璃降级保留 ----
    def test_S5_glass_fallback(self):
        i = CSS.find("@supports ((-webkit-backdrop-filter")
        self.assertGreater(i, 0, "缺少 @supports 玻璃降级块")
        j = CSS.find("{", i)
        depth, k = 1, j + 1
        while k < len(CSS) and depth:
            if CSS[k] == "{":
                depth += 1
            elif CSS[k] == "}":
                depth -= 1
            k += 1
        block = CSS[j + 1:k - 1]
        for sel in (".nav-col", ".project-bar", ".detail-pane"):
            b = _rule(sel, block)
            self.assertIsNotNone(b, "玻璃块缺 %s" % sel)
            self.assertIn("backdrop-filter: var(--glass-filter)", b, sel)
            self.assertIn("-webkit-backdrop-filter", b, sel)
        self.assertNotIn("backdrop-filter", _rule(".list-col", block))
        self.assertIn("--glass-filter: none", LIGHT)

    # ---- S6 切换入口接线 ----
    def test_S6_switch_wiring(self):
        hr = HTML.index('class="head-right"')
        toggle = HTML.index('id="skinToggle"')
        health = HTML.index('id="healthBadge"')
        self.assertLess(hr, toggle, "#skinToggle 必须在 .head-right 内")
        self.assertLess(toggle, health, "#skinToggle 必须在 #healthBadge 之前")
        self.assertIn("skinToggleLabel", HTML)
        # <head> 内联防闪脚本在样式之前、读 localStorage 键（#33：样式改为 CSS 外链，参照物同步换）
        head_script = HTML.index("localStorage.getItem('reqBoardSkin')")
        css_link = HTML.index('<link rel="stylesheet"')
        self.assertLess(head_script, css_link, "防闪脚本必须在样式之前执行")
        head = HTML[:css_link]
        self.assertIn("document.documentElement.dataset.skin", head,
                      "防闪脚本须在 <head> 内设置 html data-skin")
        script = frontend_js()
        self.assertIn("const SKIN_KEY = 'reqBoardSkin'", script)
        for fn in ("function applySkin(skin)", "function toggleSkin()", "function initSkin()"):
            self.assertIn(fn, script)
        self.assertIn("initSkin();", script, "引导区须调用一次 initSkin()")

    # ---- S7 布局零位移静态护栏 ----
    def test_S7_layout_zero_shift_static(self):
        for block in (ROOT, LIGHT):
            for prop in ("padding", "margin", "width", "height", "border-width", "font-size"):
                self.assertNotIn(prop + ":", block, "令牌块不得含尺寸属性 %s" % prop)
        self.assertIn("width: 172px", _rule(".nav-col"))
        self.assertIn("width: 540px", _rule(".list-col"))
        self.assertIn("padding: 12px 14px", _rule(".card"))
        self.assertIn("height: 24px", _rule(".skin-toggle"), "页头按钮限高 24px 防撑高页头")
        self.assertNotIn("36px", _rule(".skin-toggle"))

    # ---- S8 原生控件 chrome ----
    def test_S8_native_control_chrome(self):
        self.assertIn("color-scheme: dark", ROOT)
        self.assertIn("color-scheme: light", LIGHT)
        self.assertIn("scrollbar-color: var(--scroll-thumb)", _rule("body") or "")
        self.assertNotIn("scrollbar-width", CSS, "禁设 scrollbar-width（会改滚动条占位）")
        self.assertNotIn("::-webkit-scrollbar", CSS, "禁写 ::-webkit-scrollbar 伪元素")
        for sel in (".rb-select",):
            self.assertIn("var(--field-", _rule(sel) or "", "%s 须用令牌声明" % sel)

    # ---- S9 语义色映射 ----
    def test_S9_semantic_token_mapping(self):
        rules = {
            ".badge.open": "--st-open-bg", ".badge.later": "--st-later-bg",
            ".badge.doing": "--st-doing-bg", ".badge.done": "--st-done-bg",
            ".badge.route": "--st-route-bg", ".badge.review": "--st-review-bg",
            ".badge.dropped": "--st-dropped-bg",
            ".chip-flow-green": "--cf-green-bg", ".chip-flow-indigo": "--cf-indigo-bg",
            ".chip-flow-fuchsia": "--cf-fuchsia-bg", ".chip-flow-slate": "--cf-slate-bg",
        }
        for sel, token in rules.items():
            body = _rule(sel) or ""
            self.assertIn("var(%s)" % token, body, sel)
            self.assertNotIn("#", body, "%s 不得含颜色字面量" % sel)
        for sel in (".chat-status.st-green", ".chat-status.st-amber", ".chat-status.st-red",
                    ".chat-status.st-blue", ".chat-status.st-gray"):
            self.assertNotIn("#", _rule(sel) or "#", sel)
            self.assertIn("var(--cs-", _rule(sel) or "", sel)
        for sel in (".health.ok .dot", ".health.bad .dot", ".health.warn .dot"):
            self.assertIn("var(--health-", _rule(sel) or "", sel)

    # ---- S10 状态卡片无前缀 ----
    def test_S10_chat_status_no_prefix(self):
        for m in re.finditer(r"(?m)^[ \t]*([^\n{]*\.chat-status[^\n{]*)\{", SRC):
            self.assertTrue(m.group(1).strip().startswith(".chat-status"),
                            "状态行卡片规则必须无视图前缀：%s" % m.group(1).strip())
        self.assertNotIn(".chat-status", LIGHT, "浅色块不得新增 .chat-status 覆盖（只换令牌取值）")
        self.assertNotIn(".chat-status", ROOT)

    # ---- S11 呼吸还原 ----
    def test_S11_breath_restored(self):
        self.assertIn(
            ".session-footer .sf-id-link.running { animation: sfIdBreath 2.2s ease-in-out infinite; }", SRC)
        self.assertIn("@keyframes sfIdBreath {", SRC)
        kf = SRC[SRC.index("@keyframes sfIdBreath {"):]
        kf = kf[:kf.index("\n        }")]
        self.assertIn("opacity: 0.62", kf)
        self.assertIn("opacity: 1", kf)
        self.assertIn("text-shadow:", kf)
        self.assertNotIn("opacity: 0.5", kf)
        self.assertIn(".session-footer .sf-id-link.running { animation: none; }", SRC)
        self.assertEqual(SRC.count(".sf-id-link.running"),
                         SRC.count(".session-footer .sf-id-link.running"),
                         "呼吸选择器必须全部带 .session-footer 前缀")

    # ---- S12 hover 零位移保留 ----
    def test_S12_hover_no_shift(self):
        body = _rule(".card:hover") or ""
        for bad in ("transform", "padding", "margin", "width", "height", "border-width"):
            self.assertNotIn(bad, body, "hover 不得改 %s" % bad)
        self.assertNotIn("translateY", body, "不得引入卡片位移（#10 零位移硬约束）")
        self.assertNotIn("translateY", _rule(".card") or "", "卡片基态不得有位移")

    # ---- S13 原型排除项未引入 ----
    def test_S13_prototype_exclusions(self):
        for bad in ("fonts.googleapis.com", "fonts.gstatic.com", "Noto Sans SC",
                    "font-family: Inter", "'Inter'", "light-mode"):
            self.assertNotIn(bad, SRC, "不得引入原型外链/类名：%s" % bad)
        self.assertNotIn("prefers-color-scheme", SRC, "不做自动换肤（只手动切换 + localStorage）")
        self.assertNotIn(".theme-toggle", SRC)
        self.assertNotIn("width: 38px", SRC)
        self.assertIn("localStorage.getItem('reqBoardSkin')", SRC)
        self.assertNotIn("loop-engine-theme", SRC, "不得照搬原型 localStorage 键")

    # ---- S14 文档口径断言 ----
    def test_S14_doc_present(self):
        doc = REPO / "docs" / "design-皮肤系统.md"
        self.assertTrue(doc.exists(), "缺少 docs/design-皮肤系统.md")
        text = doc.read_text(encoding="utf-8")
        for label in ("同原型", "保持改造前", "差异"):
            self.assertIn(label, text, "文档缺口径标注：%s" % label)
        self.assertIn("--canvas-bg", text)
        self.assertIn("data-skin", text)
        self.assertIn("reqBoardSkin", text)


if __name__ == "__main__":
    unittest.main()
