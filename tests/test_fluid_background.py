#!/usr/bin/env python3
"""#16 看板背景改为 WebGL2 流体置换效果（深色/浅色配色区分）。

覆盖：
- F1-F10：源码文本断言（templates/board.html 结构 + static/board.css + static/board.js，#33 外置）
- H1-H5：node harness 对 4 个纯函数（parseCssColor / fluidRenderSize / fluidTokenNames /
  fluidReadTokens）做断言，入口 tests/fluid_bg_harness.js

零副作用：只解析上述三份源码文本（HTML 结构 / 外置 CSS / 外置 JS），不起服务、不建 worktree、不触
build_prompt/spawn_agent。

运行（worktree 内用 main 工作区的 .venv）：
    ".venv/bin/python" -m pytest tests/test_fluid_background.py -q
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css, frontend_js, frontend_src, frontend_text
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
HTML = frontend_text()
SRC = frontend_src()
CSS = re.sub(r"/\*.*?\*/", "", frontend_css(), flags=re.S)
SCRIPT = frontend_js()

_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _rule(selector, src=CSS):
    for m in _RULE_RE.finditer(src):
        parts = [re.sub(r"\s+", " ", p).strip() for p in m.group(1).split(",")]
        if selector in parts:
            return m.group(2)
    return None


ROOT = _rule(":root") or ""
LIGHT = _rule(':root[data-skin="light"]') or ""

# 流体模块源码切片：模块分节头 → 下一个分节头（`#7 P7：布局宽度`；两标记都在 JS 内，#33 起在 static/board.js）
_MOD_START = SCRIPT.index("#16 流体置换背景（原生 WebGL2")
_MOD_END = SCRIPT.index("#7 P7：布局宽度（拖拽分界）")
MOD = SCRIPT[_MOD_START:_MOD_END]

NEEDED_FUNCS = ("parseCssColor", "fluidRenderSize", "fluidTokenNames", "fluidReadTokens")
HARNESS_JS = Path(__file__).resolve().parent / "fluid_bg_harness.js"
FLUID_TOKENS = ("--fluid-color-a", "--fluid-color-b", "--fluid-color-c",
                "--fluid-veil", "--fluid-opacity")


class FluidBackgroundSourceTest(unittest.TestCase):
    """F1-F10：源码文本断言"""

    def test_F1_canvas_slot(self):
        self.assertIn('<canvas id="fluidBg" aria-hidden="true">', HTML)
        body = HTML.index("<body>")
        canvas = HTML.index('<canvas id="fluidBg"')
        page = HTML.index('<div class="page">')
        self.assertLess(body, canvas, "canvas 须在 <body> 之后")
        self.assertLess(canvas, page, "canvas 须是 .page 之前的 body 首子元素")

    def test_F2_slot_rule(self):
        bg = _rule("#fluidBg")
        self.assertIsNotNone(bg, "缺少 #fluidBg 槽位规则")
        for needle in ("position: fixed", "inset: 0", "pointer-events: none", "z-index: 0",
                       "background-image: var(--fluid-fallback)", "opacity: var(--fluid-opacity)"):
            self.assertIn(needle, bg, "槽位规则缺 %s" % needle)
        self.assertIn("z-index: 1", _rule(".page") or "", "内容层须 z-index: 1 覆盖流体")

    def test_F3_grid_removed(self):
        self.assertNotIn("body::before", SRC, "旧网格底纹须整体删除")
        self.assertNotIn("--grid-minor", SRC)
        self.assertNotIn("--grid-major", SRC)
        self.assertNotIn("160px 160px, 160px 160px, 32px 32px, 32px 32px", SRC)

    def test_F4_fluid_tokens_both_skins(self):
        for name in FLUID_TOKENS + ("--fluid-fallback",):
            self.assertIn(name + ":", ROOT, "深色缺令牌 %s" % name)
            self.assertIn(name + ":", LIGHT, "浅色缺令牌 %s" % name)

        def val(block, name):
            m = re.search(re.escape(name) + r"\s*:\s*([^;]+);", block)
            return m.group(1).strip() if m else None

        for name in FLUID_TOKENS:
            self.assertNotEqual(val(ROOT, name), val(LIGHT, name),
                                "深浅两皮肤 %s 取值须明显区分" % name)
        # --fluid-veil 必须 = 该档 --canvas-bg（「压底混色 = 画布底」口径；防后续改画布底色
        # 只改一处导致 veil 漂移、可读性口径失效）
        for block in (ROOT, LIGHT):
            self.assertEqual(val(block, "--fluid-veil"), val(block, "--canvas-bg"),
                             "--fluid-veil 须与该皮肤 --canvas-bg 同值")

    def test_F5_no_color_literals(self):
        # CSS 侧：槽位规则零字面量（既有 S3 的流体域补充）
        bg = _rule("#fluidBg") or ""
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", bg), "槽位规则含 hex 字面量")
        self.assertIsNone(re.search(r"rgba?\(", bg), "槽位规则含 rgb()/rgba() 字面量")
        # JS 侧：流体模块无 6 位 hex、无真实颜色调用（`rgb(` 后紧跟数字才算颜色值）
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{6}\b", MOD), "流体模块含 hex 颜色字面量")
        self.assertIsNone(re.search(r"rgba?\(\s*[0-9.]", MOD), "流体模块含 rgb()/rgba() 颜色字面量")

    def test_F6_reduced_motion_guard(self):
        self.assertIn("prefers-reduced-motion", MOD)
        self.assertIn("matchMedia", MOD)
        self.assertIn("if (reduced)", MOD, "reduced-motion 须有显式分支")
        self.assertIn("fluidRenderFrame(fluidTime)", MOD, "reduced-motion 下渲染单帧静态流体")

    def test_F7_webgl2_probe_and_context_loss(self):
        self.assertIn("getContext('webgl2'", MOD)
        self.assertIn("webglcontextlost", MOD)
        self.assertIn("preventDefault", MOD)
        self.assertIn("webglcontextrestored", MOD)
        # 恢复路径必须重建 GL 资源：上下文丢失后旧 program/uniform location 已失效，
        # 直接复用只会产生 INVALID_OPERATION（WebGL 错误不抛 JS 异常，只在 console 告警）
        init_fn = _extract_function(SCRIPT, "initFluidBg")
        restored = init_fn[init_fn.index("webglcontextrestored"):]
        self.assertIn("fluidBuildProgram(fluidGl)", restored, "restored 分支须重建 program/location")
        self.assertIn("fluidBuildProgram(gl)", init_fn, "初始化与恢复须共用同一 builder")
        self.assertNotIn("getUniformLocation", init_fn,
                         "uniform location 取用须集中在 fluidBuildProgram（避免恢复路径漏建）")
        self.assertIn("getUniformLocation", _extract_function(SCRIPT, "fluidBuildProgram"))

    def test_F8_wiring(self):
        skin = _extract_function(SCRIPT, "applySkin")
        self.assertIn("applyFluidTokens()", skin, "applySkin 须刷新流体令牌")
        tokens_fn = _extract_function(SCRIPT, "applyFluidTokens")
        self.assertIn("useProgram(fluidProg)", tokens_fn,
                      "applyFluidTokens 须先 useProgram 再设 uniform（否则 location 无效、静默不生效）")
        self.assertLess(tokens_fn.index("useProgram(fluidProg)"), tokens_fn.index("uniform3f"),
                        "useProgram 须在 uniform 之前")
        # 静态单帧模式（reduced-motion）循环已停，切皮肤必须显式重绘，否则 canvas 停在上一档配色
        self.assertIn("if (!fluidAnimated) fluidRenderFrame(fluidTime)", tokens_fn,
                      "静态单帧模式切皮肤须立即重绘")
        self.assertEqual(SCRIPT.count("initFluidBg();"), 1, "引导区须恰好调用一次 initFluidBg()")
        self.assertIn("fluidSetPaused(document.hidden)", SCRIPT, "visibilitychange 须暂停/恢复流体")

    def test_F9_console_silent(self):
        self.assertNotIn("console.", MOD, "流体模块不得输出控制台日志/报错")

    def test_F10_zero_dependency(self):
        self.assertNotIn("http://", MOD)
        self.assertNotIn("https://", MOD)
        self.assertIsNone(re.search(r"\bimport\s", MOD))
        self.assertIsNone(re.search(r"\brequire\(", MOD))


def _extract_source():
    return "\n\n".join(_extract_function(SCRIPT, name) for name in NEEDED_FUNCS)


class FluidBackgroundHarnessTest(unittest.TestCase):
    """H1-H5：纯函数 node harness"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过流体纯函数 harness")

    def _run(self):
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

    def test_H_all_checks_pass(self):
        checks = self._run().get("checks", [])
        self.assertGreaterEqual(len(checks), 10, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败断言: %s" % failures)

    def test_H5_token_names_match_source(self):
        """防改名漂移：JS 令牌名清单须逐一落在 static/board.css 两个令牌块内（非同源自证）"""
        for name in FLUID_TOKENS:
            self.assertIn(name + ":", ROOT)
            self.assertIn(name + ":", LIGHT)
            self.assertIn("'%s'" % name, SCRIPT)


if __name__ == "__main__":
    unittest.main()
