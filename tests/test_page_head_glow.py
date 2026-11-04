#!/usr/bin/env python3
"""#20 页头去底色 + 底线来回流动高光：源码文本断言（零副作用）。

只解析 static/board.css 文本（#33 起样式外置），不起服务、不建 worktree。锁定：页头去实心底色、
::after 画 1px 底线（var(--border)）+ 22% 宽浅蓝高光（var(--headline-glow)）来回流动 6s、
reduced-motion 停动画只留静态底线、令牌双皮肤取值。

    .venv/bin/python -m pytest tests/test_page_head_glow.py -q
"""
import re
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css

REPO = Path(__file__).resolve().parent.parent
SRC = frontend_css()
CSS = re.sub(r"/\*.*?\*/", "", SRC, flags=re.S)

_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _rule(selector, src=CSS):
    for m in _RULE_RE.finditer(src):
        parts = [re.sub(r"\s+", " ", p).strip() for p in m.group(1).split(",")]
        if selector in parts:
            return m.group(2)
    return None


class PageHeadGlowTest(unittest.TestCase):
    # ---- 去实心底色（透出 #16 流体背景），sticky/z-index 保留 ----
    def test_head_no_solid_background(self):
        head = _rule(".page-head") or ""
        self.assertIn("background: transparent", head)
        self.assertNotIn("var(--bg)", head, "页头不得再带实心底色")
        self.assertIn("position: sticky", head)
        self.assertIn("z-index: 100", head)

    # ---- 1px 底线 + 22% 宽浅蓝高光，颜色全经令牌 ----
    def test_bottom_line_and_glow_layer(self):
        after = _rule(".page-head::after") or ""
        self.assertIn("position: absolute", after, "伪元素须绝对定位（不占布局高度）")
        self.assertIn("bottom: 0", after)
        self.assertIn("height: 1px", after)
        self.assertIn("background-color: var(--border)", after, "底线须与面板边框同源")
        self.assertIn("linear-gradient(90deg, transparent, var(--headline-glow), transparent)", after)
        self.assertIn("background-size: 22% 100%", after)
        self.assertIn("animation: headline-sweep 6s ease-in-out infinite alternate", after)
        self.assertNotIn("#", after, "规则体不得写颜色字面量")

    # ---- 关键帧来回流动 ----
    def test_keyframes_travel_back_and_forth(self):
        self.assertIn("@keyframes headline-sweep {", SRC)
        kf = SRC[SRC.index("@keyframes headline-sweep {"):]
        kf = kf[:kf.index("\n        }")]
        self.assertIn("background-position: 0% 0", kf)
        self.assertIn("background-position: 100% 0", kf)

    # ---- reduced-motion 停动画、只留静态底线 ----
    def test_reduced_motion_keeps_static_line_only(self):
        self.assertRegex(CSS, r"@media \(prefers-reduced-motion: reduce\) \{\s*"
                              r"\.page-head::after \{ animation: none; background-image: none; \}")

    # ---- 令牌双皮肤取值 ----
    def test_token_two_skins(self):
        root = _rule(":root") or ""
        light = _rule(':root[data-skin="light"]') or ""
        self.assertIn("--headline-glow: rgba(147, 197, 253, 0.85)", root)
        self.assertIn("--headline-glow: rgba(96, 165, 250, 0.85)", light)


if __name__ == "__main__":
    unittest.main()
