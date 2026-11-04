#!/usr/bin/env python3
"""#17 全站点击按钮统一为实心三档（皮肤切换除外）。

源码文本断言（零副作用）：解析 templates/board.html（HTML 结构）与 static/board.css、static/board.js
（#33 外置的样式/脚本）文本，不起服务、不建 worktree、不触 build_prompt/spawn_agent。
方案依据见 artifacts/.../req-17/tech.md。

    .venv/bin/python -m pytest tests/test_btn_solid_tiers.py -q
"""
import re
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css, frontend_src

REPO = Path(__file__).resolve().parent.parent
SRC = frontend_src()
CSS = re.sub(r"/\*.*?\*/", "", frontend_css(), flags=re.S)

_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _rule(selector, src=CSS):
    for m in _RULE_RE.finditer(src):
        parts = [re.sub(r"\s+", " ", p).strip() for p in m.group(1).split(",")]
        if selector in parts:
            return m.group(2)
    return None


def _norm(body):
    return re.sub(r"\s+", " ", body or "").strip()


def _decls(body):
    out = {}
    for d in _norm(body).split(";"):
        d = d.strip()
        if d and ":" in d:
            k, v = d.split(":", 1)
            out[k.strip()] = v.strip()
    return out


ROOT = _rule(":root") or ""
LIGHT = _rule(':root[data-skin="light"]') or ""

# 按钮档位 9 条 :hover 规则（口径边界：只有它们受「禁声明 color」约束）
HOVER_RULES = (".btn:hover", ".btn-primary:hover", ".btn-danger:hover",
               ".btn.rb-save-all:hover", ".btn.ms-del-alias:hover", ".btn.ms-del-prov:hover",
               ".chat-status .st-btn:hover", ".chat-status .st-btn-primary:hover",
               ".chat-attach-btn:hover")
# 主/危险档基态（文字色只在此声明）
ACCENT_BASE_RULES = (".btn-primary", ".btn-danger", ".rb-save-all",
                     ".btn.ms-del-alias", ".btn.ms-del-prov", ".chat-status .st-btn-primary")

SIZE_PROPS = ("padding", "margin", "width", "height", "border-width", "transform")


class BtnSolidTiersTest(unittest.TestCase):
    # ---- B1 中性档令牌齐备 ----
    def test_B1_neutral_tokens(self):
        for block, name, vals in ((ROOT, "深色", ("#16233a", "#dcecff", "#1e2f4a")),
                                  (LIGHT, "浅色", ("#e5e7eb", "#374151", "#d1d5db"))):
            for tok, val in zip(("--btn-neutral-bg", "--btn-neutral-fg", "--btn-neutral-bg-hover"), vals):
                self.assertIn("%s: %s" % (tok, val), block, "%s %s 取值偏离" % (name, tok))
        self.assertNotIn("--btn-neutral-fg-hover", CSS)
        self.assertNotIn("--btn-neutral-bd", CSS)

    # ---- B2 令牌纯净 ----
    def test_B2_token_purity(self):
        for tok in ("--btn-neutral-bg", "--btn-neutral-fg", "--btn-neutral-bg-hover"):
            self.assertEqual(CSS.count(tok + ":"), 2, "%s 必须恰好在两个令牌块各定义一次" % tok)
        for block in (ROOT, LIGHT):
            for prop in SIZE_PROPS + ("font-size",):
                self.assertNotIn(prop + ":", block, "令牌块不得含尺寸属性 %s" % prop)

    # ---- B3 .btn 实心化 ----
    def test_B3_btn_solid(self):
        body = _norm(_rule(".btn"))
        self.assertIn("background: var(--btn-neutral-bg)", body)
        self.assertIn("border: 1px solid var(--btn-neutral-bg)", body, "描边须与底色同值（同色 1px 保留盒模型）")
        self.assertIn("color: var(--btn-neutral-fg)", body)
        self.assertNotIn("var(--btn-bg)", body)

    # ---- B4 悬浮只改底色/描边 ----
    def test_B4_btn_hover_minimal(self):
        body = _norm(_rule(".btn:hover"))
        self.assertIn("background: var(--btn-neutral-bg-hover)", body)
        self.assertIn("border-color: var(--btn-neutral-bg-hover)", body)
        self.assertNotIn("color", _decls(body), "hover 不得声明文字色")
        for bad in SIZE_PROPS:
            self.assertNotIn(bad, _decls(body), "hover 不得改 %s" % bad)

    # ---- B5 主/危险档令牌化 ----
    def test_B5_primary_danger_tokens(self):
        for sel, token, hover_token in ((".btn-primary", "--accent", "--accent-hover"),
                                        (".btn-danger", "--danger", "--danger-hover")):
            body = _decls(_rule(sel))
            self.assertIn("var(%s)" % token, body.get("background", ""), sel)
            self.assertEqual(body.get("color"), "var(--on-accent)", sel)
            hover = _decls(_rule(sel + ":hover"))
            self.assertIn("var(%s)" % hover_token, hover.get("border-color", ""), sel)
            self.assertNotIn("color", hover, "%s 不得声明文字色" % sel)

    # ---- B6 表格行危险变体 ----
    def test_B6_table_danger_variants(self):
        for sel in (".btn.ms-del-alias", ".btn.ms-del-prov"):
            body = _decls(_rule(sel))
            self.assertIsNotNone(_rule(sel), "缺少 %s" % sel)
            self.assertIn("var(--danger)", body.get("background", ""))
            self.assertEqual(body.get("color"), "var(--on-accent)")
            hover = _decls(_rule(sel + ":hover"))
            self.assertIn("var(--danger-hover)", hover.get("border-color", ""), sel)
            self.assertNotIn("color", hover, sel)

    # ---- B7 保存全部主档 ----
    def test_B7_save_all_primary(self):
        body = _decls(_rule(".rb-save-all"))
        self.assertIn("var(--accent)", body.get("background", ""))
        self.assertEqual(body.get("color"), "var(--on-accent)")
        self.assertEqual(body.get("padding"), "4px 12px")
        self.assertEqual(body.get("font-size"), "12px")
        hover = _decls(_rule(".btn.rb-save-all:hover"))
        self.assertIn("var(--accent-hover)", hover.get("border-color", ""))
        self.assertNotIn("color", hover)

    # ---- B8 附件按钮归并 ----
    def test_B8_attach_button_merged(self):
        self.assertIn('class="btn btn-sm chat-attach-btn"', SRC)
        body = _decls(_rule(".chat-attach-btn"))
        self.assertNotIn("background", body)
        self.assertNotIn("border", body)
        self.assertNotIn("font", body)
        self.assertNotIn("font-size", body)
        hover = _decls(_rule(".chat-attach-btn:hover"))
        self.assertIn("var(--accent)", hover.get("border-color", ""))
        self.assertNotIn("color", hover)

    # ---- B9 状态卡中性档 ----
    def test_B9_status_btn_neutral(self):
        body = _norm(_rule(".chat-status .st-btn"))
        self.assertTrue("border: 1px solid var(--btn-neutral-bg)" in body
                        or ("border-width: 1px" in body and "border-style: solid" in body),
                        "只写 border-color 会让 style/width 回落 UA outset/2px：%s" % body)
        decls = _decls(body)
        self.assertEqual(decls.get("background"), "var(--btn-neutral-bg)")
        self.assertEqual(decls.get("color"), "var(--btn-neutral-fg)")
        self.assertEqual(decls.get("font-size"), "12px")
        self.assertEqual(decls.get("line-height"), "1.4")
        self.assertEqual(decls.get("border-radius"), "6px")
        self.assertEqual(decls.get("cursor"), "pointer")
        self.assertEqual(decls.get("padding"), "4px 12px")
        self.assertNotIn("currentColor", body)
        self.assertNotIn("color: inherit", body)
        self.assertNotIn("font", decls, "不得沿用 font: 简写（会重置字号）")

    # ---- B10 状态卡主档 ----
    def test_B10_status_btn_primary(self):
        body = _decls(_rule(".chat-status .st-btn-primary"))
        self.assertIsNotNone(_rule(".chat-status .st-btn-primary"), "缺少 .chat-status .st-btn-primary")
        self.assertIn("var(--accent)", body.get("background", ""))
        self.assertEqual(body.get("color"), "var(--on-accent)")
        hover = _decls(_rule(".chat-status .st-btn-primary:hover"))
        self.assertIn("var(--accent-hover)", hover.get("border-color", ""))
        self.assertNotIn("color", hover)
        for m in re.finditer(r"(?m)^[ \t]*([^\n{]*\.st-btn[^\n{]*)\{", CSS):
            self.assertTrue(m.group(1).strip().startswith(".chat-status"),
                            "状态卡按钮规则不得带视图前缀：%s" % m.group(1).strip())

    # ---- B11 禁用态不变 ----
    def test_B11_disabled_untouched(self):
        self.assertEqual(
            _norm(_rule(".btn:disabled")),
            "background: var(--bg); border-color: var(--border); color: var(--text-3); "
            "cursor: not-allowed; opacity: .75;")
        disabled = CSS.find(".btn:disabled")
        for sel in (".btn-primary", ".btn-danger", ".btn.ms-del-alias", ".btn.ms-del-prov"):
            self.assertLess(CSS.find(sel), disabled, "%s 必须在禁用态规则之前" % sel)

    # ---- B12 排除项零回归 ----
    def test_B12_exclusions_untouched(self):
        self.assertEqual(
            _norm(_rule(".skin-toggle")),
            "display: inline-flex; align-items: center; gap: 6px; height: 24px; "
            "padding: 0 8px; border: 1px solid var(--glass-border); border-radius: 7px; "
            "font-size: 12px; line-height: 1; cursor: pointer; "
            "background: var(--btn-bg); color: var(--text-2); "
            "transition: background .15s, border-color .15s, color .15s;")
        self.assertEqual(
            _norm(_rule(".skin-toggle:hover")),
            "background: var(--btn-bg-hover); border-color: var(--border-strong); color: var(--text);")
        self.assertIn("--btn-bg: #ffffff", LIGHT)
        self.assertIn("--btn-bg-hover: #f3f4f6", LIGHT)
        self.assertEqual(
            _norm(_rule(".nav-gear")),
            "display: flex; align-items: center; justify-content: center; gap: 6px; "
            "width: 100%; height: 30px; padding: 0; "
            "border: none; border-radius: 7px; background: transparent; "
            "color: var(--text-3); font-size: 12px; cursor: pointer; "
            "transition: background .15s, color .15s;")

    # ---- B13 紧凑档几何 ----
    def test_B13_compact_geometry(self):
        self.assertIn("padding: 4px 12px", _norm(_rule(".btn-sm")))
        self.assertIn("font-size: 12px", _norm(_rule(".btn-sm")))
        for sel in (".chat-status .st-btn", ".rb-save-all"):
            self.assertIn("padding: 4px 12px", _norm(_rule(sel)), sel)
            self.assertIn("font-size: 12px", _norm(_rule(sel)), sel)
        self.assertEqual(frontend_css().count("padding: 8px 18px;"), 1, "常规档几何只允许出现在 .btn 基类")

    # ---- B14 标记最小集 ----
    def test_B14_marker_minimal_set(self):
        self.assertIn("""class="btn btn-sm btn-danger" ${sub('reject')}>退回待开发""", SRC)
        self.assertNotIn("btn btn-sm btn-primary\" onclick=\"decideMerge", SRC)
        self.assertIn("class=\"btn btn-sm\" onclick=\"decideMerge(${i.id},'retry')\"", SRC)
        self.assertNotIn("btn btn-sm btn-primary\" onclick=\"openEdit", SRC)
        self.assertNotIn("btn btn-sm\" onclick=\"delReq", SRC)
        self.assertIn('class="btn btn-danger" onclick="delReq(', SRC)
        self.assertIn("class=\"btn\" onclick=\"decideMerge(${i.id},'retry')\"", SRC)
        self.assertIn('class="st-btn st-btn-primary"', SRC)
        self.assertNotIn('btn btn-sm btn-primary" id="chatResumeBtn"', SRC)
        for locked in ('class="btn ms-del-alias"', 'class="btn ms-add-alias-btn"', 'class="btn rb-save-all"'):
            self.assertIn(locked, SRC, "harness 锁定标记被改：%s" % locked)

    # ---- B15 无新增动画 ----
    def test_B15_no_new_animation(self):
        self.assertEqual(frontend_css().count("animation:"), 6, "基线 6 处 animation 声明不得增减（#20 页头 headline-sweep + reduced-motion 关闭各 +1）")
        self.assertEqual(frontend_css().count("will-change"), 0)

    # ---- B16 悬停文字色不丢（口径限定为按钮档位 9 条） ----
    def test_B16_hover_text_color_intact(self):
        for sel in HOVER_RULES:
            self.assertIsNotNone(_rule(sel), "缺少 %s" % sel)
            self.assertNotIn("color", _decls(_rule(sel)), "%s 不得声明文字色" % sel)
        for sel in ACCENT_BASE_RULES:
            self.assertEqual(_decls(_rule(sel)).get("color"), "var(--on-accent)", sel)
        self.assertIn("color", _decls(_rule(".skin-toggle:hover")), "非按钮 hover 的文字色不得被顺带删掉")

    # ---- B17 紧凑档字号不被简写重置 ----
    def test_B17_compact_font_not_reset(self):
        for sel in (".chat-attach-btn", ".chat-status .st-btn"):
            self.assertNotIn("font", _decls(_rule(sel)), sel)
        self.assertNotIn("font-size", _decls(_rule(".chat-attach-btn")), "字号由 .btn-sm 提供")
        self.assertLess(CSS.find(".btn-sm"), CSS.find(".chat-attach-btn"), ".btn-sm 必须先于附件按钮规则")
        self.assertEqual(_decls(_rule(".chat-status .st-btn")).get("font-size"), "12px")

    # ---- B18 模型设置表内行紧凑档 ----
    def test_B18_model_table_compact(self):
        for sel in (".ms-alias-table .btn", ".ms-add-alias .btn"):
            body = _norm(_rule(sel))
            self.assertIsNotNone(body, "缺少作用域规则 %s" % sel)
            decls = {d.strip() for d in body.split(";") if d.strip()}
            self.assertEqual(decls, {"padding: 4px 12px", "font-size: 12px"},
                             "%s 只允许紧凑几何，不得触碰颜色/描边" % sel)
        for locked in ('class="btn ms-del-alias"', 'class="btn ms-add-alias-btn"'):
            self.assertIn(locked, SRC)


if __name__ == "__main__":
    unittest.main()
