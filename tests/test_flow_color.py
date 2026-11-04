#!/usr/bin/env python3
"""#428 中间栏流程标签三色区分（简单/标准/复杂 + 无流程快照卡中性灰）测试。

B 组：配色词表（acp_shared.FLOW_COLORS）↔ CSS 规则（static/board.css）双向一致、色值逐条锁定、
      .chip-auto/.chip-semi 删除、前端零手写映射、兜底常量与后端一致；
C 组：flowChipHtml 渲染矩阵（node harness，tests/flow_chip_color_harness.js，C1~C13）。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_flow_color -v
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import backend_text, frontend_css, frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "flow_chip_color_harness.js"

# 色值唯一落点（static/board.css）逐条锁定：三档流程色 + 中性灰
# （fuchsia 这一档 2026-09-19 由玫红 rose #fff1f2/#be123c 换成品红紫 #fdf4ff/#a21caf：
#   原玫红与「评审中」徽标红 #fef2f2/#b91c1c 几乎同色，同屏情绪串台，故与状态色去重；
#   色名同步由 rose 改为 fuchsia，保持名实一致）
# #15：色值收敛为令牌——规则体只引用 var(--cf-<name>-bg/-fg)，取值落 :root / :root[data-skin="light"]
EXPECTED_COLORS = {
    "green": ("--cf-green-bg", "--cf-green-fg"),
    "indigo": ("--cf-indigo-bg", "--cf-indigo-fg"),
    "fuchsia": ("--cf-fuchsia-bg", "--cf-fuchsia-fg"),
    "slate": ("--cf-slate-bg", "--cf-slate-fg"),
}
# 浅色令牌值 == 改造前字面量（表 C「保」）
EXPECTED_LIGHT_VALUES = {
    "green": ("#f0fdf4", "#15803d"),
    "indigo": ("#eef2ff", "#4338ca"),
    "fuchsia": ("#fdf4ff", "#a21caf"),
    "slate": ("#f8fafc", "#64748b"),
}

NEEDED_FUNCS = ("escapeHtml", "flowChipHtml")


class FlowColorCssTest(unittest.TestCase):
    """B1~B5：配色词表 ↔ CSS 规则双向一致（防双源漂移）"""

    def test_b1_css_rules_match_vocabulary(self):
        """B1 规则名集合 == acp_shared.FLOW_COLORS（不缺不多），每个名字恰一条规则"""
        import acp_shared as ac

        names = re.findall(r"\.(?:meta-chip\.)?chip-flow-([a-z]+)\s*\{([^}]*)\}", frontend_css())
        got = {}
        for name, _ in names:
            got[name] = got.get(name, 0) + 1
        self.assertEqual(set(got), set(ac.FLOW_COLORS), got)
        self.assertEqual([n for n, c in got.items() if c != 1], [], got)

    def test_b2_css_values_locked(self):
        """B2 色值逐条锁定（背景/前景），常量键集 == 词表（词表禁 hex，色值只在 CSS 落一次）"""
        import acp_shared as ac

        self.assertEqual(set(EXPECTED_COLORS), set(ac.FLOW_COLORS))
        src = frontend_css()
        light = re.search(r':root\[data-skin="light"\]\s*\{(.*?)\n        \}', src, re.S).group(1)
        for name, body in re.findall(r"\.chip-flow-([a-z]+)\s*\{([^}]*)\}", src):
            bg = re.search(r"background\s*:\s*var\((--[\w-]+)\)", body)
            fg = re.search(r"(?<![\w-])color\s*:\s*var\((--[\w-]+)\)", body)
            self.assertIsNotNone(bg, name)
            self.assertIsNotNone(fg, name)
            self.assertEqual((bg.group(1), fg.group(1)), EXPECTED_COLORS[name], name)
        for name, (bgt, fgt) in EXPECTED_COLORS.items():
            vbg, vfg = EXPECTED_LIGHT_VALUES[name]
            self.assertIn("%s: %s" % (bgt, vbg), light, name)
            self.assertIn("%s: %s" % (fgt, vfg), light, name)
        for token in ac.FLOW_COLORS:
            self.assertNotIn("#", token)   # 词表只存语义色名，禁写 hex

    def test_b3_legacy_chip_classes_removed(self):
        """B3 旧类删除：前端全源（模板 + static/board.js + static/board.css，含注释/JS 字符串）
        与后端源码均不再出现 chip-auto/chip-semi"""
        src = frontend_src() + backend_text()
        self.assertNotIn("chip-auto", src)
        self.assertNotIn("chip-semi", src)

    def test_b4_frontend_zero_handwritten_mapping(self):
        """B4 前端零手写映射：FLOW_COLOR 只由 /api/meta 的 f.color 构建，无字面量副本"""
        src = frontend_js()
        self.assertRegex(src, r"FLOW_COLOR\[f\.id\]\s*=\s*f\.color")
        self.assertNotRegex(src, r"FLOW_COLOR\s*=\s*\{[^}]*:")
        self.assertNotRegex(src, r"'(auto|standard|heavy)'\s*:\s*'(green|indigo|fuchsia|slate)'")

    def test_b5_fallback_matches_backend_default(self):
        """B5 前端兜底常量 == acp_shared.DEFAULT_FLOW_COLOR（同一语义单一源）"""
        import acp_shared as ac

        m = re.search(r"FLOW_COLOR_FALLBACK\s*=\s*'([a-z]+)'", frontend_js())
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), ac.DEFAULT_FLOW_COLOR)
        self.assertIn(ac.DEFAULT_FLOW_COLOR, ac.FLOW_COLORS)


class FlowChipColorHarnessTest(unittest.TestCase):
    """C1~C13：flowChipHtml 渲染矩阵（node 断言）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 #428 流程 chip 配色 JS 断言")

    def _run_harness(self):
        js = frontend_js()
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

    def test_c_chip_color_matrix_pass(self):
        """C1~C13：三档取色/未命中中性灰/锁死不按 mode 取色/降级/边界全部通过"""
        checks = self._run_harness().get("checks", [])
        self.assertGreaterEqual(len(checks), 12, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)


if __name__ == "__main__":
    unittest.main()
