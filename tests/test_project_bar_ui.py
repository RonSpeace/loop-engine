#!/usr/bin/env python3
"""#3 项目信息条 + 设置抽屉前端测试。

C 组（静态，模板与 static/board.js/board.css 源码文本）：布局结构（main-col/main-row/projectBar，原 #list/#detail 保留）、
      刷新挂点（render/autoRefresh 双挂点）、harness 禁区函数零引用、#list 唯一写入口口径、
      抽屉接线、escapeAttr 用于属性位、#21 指标配色令牌与规则；
H 组（node harness，tests/project_bar_harness.js）：formatTokens/formatSuccessRate 矩阵、
      单项目/all 态渲染、goal 缺失、注入转义、数据缺失降级、机器人禁用位、
      #21 指标数字配色修饰类与零值中性。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_project_bar_ui.py -q
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "project_bar_harness.js"
sys.path.insert(0, str(REPO))

NEEDED_FUNCS = ("escapeHtml", "escapeAttr", "formatTokens", "formatSuccessRate",
                "displayProjectName", "projectBarHtml")


def _board_src():
    return frontend_src()


def _script_block():
    return frontend_js()


def _js_const(js, name):
    """提取 const <name> = ...; 常量块源码（用于注入 harness；块内无 ASCII 分号）"""
    m = re.search(r"const %s = .*?;" % re.escape(name), js, re.S)
    if not m:
        raise AssertionError("const %s not found in static/board.js" % name)
    return m.group(0)


def _css_block(src, selector):
    """取源文本中 selector 之后第一对花括号内的样式块正文"""
    j = src.index("{", src.index(selector))
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j + 1:k]
        k += 1
    raise AssertionError("未闭合的样式块：%s" % selector)


class ProjectBarStaticTest(unittest.TestCase):
    """C1~C7：接线静态断言（源码文本，沿 test_sse_dispatch W 组风格）"""

    @classmethod
    def setUpClass(cls):
        cls.src = _board_src()
        cls.js = _script_block()

    def test_c1_layout_structure(self):
        """C1：新包裹层与信息条存在；原 #list/#detail/#navList 与 .workspace 三栏契约保留"""
        for token in ('<div class="workspace">', '<div class="nav-col">',
                      '<div class="main-col">', '<div class="main-row">',
                      '<div class="project-bar" id="projectBar"></div>',
                      '<div class="nav-items" id="navList"></div>',
                      '<div class="list-col" id="list"></div>',
                      '<div class="detail-col" id="detail"></div>'):
            self.assertIn(token, self.src, token)

    def test_c2_refresh_hooks(self):
        """C2：refreshProjectBar 同时挂在 render() 与 autoRefresh() 函数体内"""
        for fn in ("render", "autoRefresh"):
            body = _extract_function(self.js, fn)
            self.assertIn("refreshProjectBar()", body, fn)
        # 过期响应丢弃 + ALL_PROJECTS→all 映射（评审反馈 5）
        body = _extract_function(self.js, "refreshProjectBar")
        self.assertIn("selectedProject !== sel", body)
        self.assertIn("ALL_PROJECTS ? 'all'", body)
        self.assertIn("/api/project-stats?project=", body)

    def test_c3_forbidden_harness_functions_untouched(self):
        """C3：harness 提取的五个函数体不得引用新全局；renderList 前置签名行不变"""
        for fn in ("renderList", "repaintForAgentSessions", "agentSessionRefreshDecision",
                   "refreshAgentSessions", "scheduleReqChatPoll"):
            body = _extract_function(self.js, fn)
            for bad in ("projectBar", "settingsDrawer", "refreshProjectBar", "PB_STAT_TIP"):
                self.assertNotIn(bad, body, "%s 不得引用 %s" % (fn, bad))
        self.assertIn("let listHtmlSig = null;\n        function renderList() {", self.src)

    def test_c4_list_write_single_entry(self):
        """C4：#list 唯一写入口口径保持（沿 test_sse_dispatch W2 数字口径）"""
        self.assertEqual(self.src.count("document.getElementById('list')"), 1)
        self.assertEqual(self.src.count("list.innerHTML"), 1)

    def test_c5_drawer_wiring(self):
        """C5：齿轮/抽屉 DOM 与函数接线存在；齿轮落在 nav-foot（左下角）"""
        for token in ('id="settingsGearBtn"', 'id="settingsDrawer"', 'id="settingsMask"',
                      'class="drawer-mask"', "function openSettingsDrawer()",
                      "function closeSettingsDrawer()", "function loadSettingsDrawer(",
                      "'/api/settings-drawer'"):
            self.assertIn(token, self.src, token)
        foot = self.src[self.src.index('<div class="nav-foot">'):]
        self.assertIn('id="settingsGearBtn"', foot[:900])
        # ESC 关闭接线
        self.assertIn("closeSettingsDrawer()", self.js)

    def test_c6_escape_attr_used_for_attribute_positions(self):
        """C6：escapeAttr（引号转义）定义且在 projectBarHtml 属性位使用"""
        body = _extract_function(self.js, "escapeAttr")
        self.assertIn("&quot;", body)
        self.assertIn("&#39;", body)
        bar = _extract_function(self.js, "projectBarHtml")
        self.assertIn('title="${escapeAttr(', bar)

    def test_c7_stat_tone_tokens(self):
        """C7（#21）：6 个指标配色令牌深/浅两块齐备；6 条规则体只消费 var(--pb-stat-*) 无字面量"""
        tones = ("total", "doing", "done", "rate", "reject", "tokens")
        for sel in (":root {", ':root[data-skin="light"] {'):
            block = _css_block(self.src, sel)
            for tone in tones:
                self.assertIn("--pb-stat-%s:" % tone, block,
                              "令牌块 %s 缺 --pb-stat-%s" % (sel, tone))
        for tone in tones:
            self.assertIn(".pb-stat-%s b { color: var(--pb-stat-%s); }" % (tone, tone), self.src,
                          "缺指标配色规则 .pb-stat-%s b" % tone)


def _harness_source():
    js = _script_block()
    parts = [_extract_function(js, name) for name in NEEDED_FUNCS]
    parts.append(_js_const(js, "PROJECT_DISPLAY"))
    parts.append(_js_const(js, "PB_STAT_TIP"))
    return "\n\n".join(parts) + "\n\n" + HARNESS_JS.read_text(encoding="utf-8")


class ProjectBarHarnessTest(unittest.TestCase):
    """H1~H10：项目信息条渲染矩阵（node 断言）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 #3 项目信息条 JS 断言")

    def _run_harness(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(_harness_source())
            path = f.name
        try:
            proc = subprocess.run([self.node, path], capture_output=True, text=True, timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        if proc.returncode != 0:
            self.fail("node harness 失败: %s\n%s" % (proc.stderr, proc.stdout))
        return json.loads(proc.stdout)

    def test_h_matrix_pass(self):
        """H1~H10：格式化矩阵 / 单项目与聚合渲染 / goal 缺失 / 注入转义 / 降级 / 机器人位 / 配色修饰类全通过"""
        checks = self._run_harness().get("checks", [])
        self.assertGreaterEqual(len(checks), 8, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)


if __name__ == "__main__":
    unittest.main()
