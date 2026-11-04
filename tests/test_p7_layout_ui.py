#!/usr/bin/env python3
"""#7 项目中心 P7/#49：看板布局可拖拽 + 需求卡折叠（前端静态接线 + 纯函数 harness）。

C 组（静态，模板与 static/board.js/board.css 源码文本）：
  C1 骨架 token 逐字保留 + 两处分界插入位置（列的兄弟节点，list 与 detail token 之间）；
  C2 #list 唯一写入口口径（getElementById('list') / list.innerHTML 计数各 1；新代码用 querySelector('.list-col')）；
  C3 renderList 沙箱面（正文零新标识符，前置签名行原样保 sse_dispatch harness 提取）；
  C4 卡片折叠接线（所有状态可折叠；done 默认收缩、其余默认展开；cardCollapsed/toggleCardExpand/
     cardHtml 裁剪与图标/aria、CSS .open 旋转）；
  C5 拖拽接线（常量不变量 / 指针绑定 / 拖动禁过渡 / resize 与折叠收敛 / 初始化挂点）；
  C6 持久化接线（两存储键 + 拖动结束与卡片切换的落盘点 + fail-safe 解析）；
  C7 负向锁（折叠图标仅一处且不受 done 守卫、sessionFooterHtml 零改动、无 dblclick 双击重置）。
H 组（node harness，tests/p7_layout_harness.js）：clampColWidth / colMaxWidth / cardCollapsed /
  parseStoredObject 矩阵（注入 static/board.js 真实函数）。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_p7_layout_ui.py -q
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
HARNESS_JS = Path(__file__).resolve().parent / "p7_layout_harness.js"
sys.path.insert(0, str(REPO))

NEEDED_FUNCS = ("parseStoredObject", "clampColWidth", "colMaxWidth", "cardCollapsed")
NUMERIC_CONSTS = ("NAV_MIN_W", "NAV_DEFAULT_W", "LIST_DEFAULT_W", "LIST_MIN_W",
                  "DETAIL_MIN_W", "MAIN_MIN_W", "COL_GAP_W")


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


class P7LayoutStaticTest(unittest.TestCase):
    """C1~C7：静态接线断言（源码文本，沿 test_project_bar_ui C 组风格）"""

    @classmethod
    def setUpClass(cls):
        cls.src = _board_src()
        cls.js = _script_block()

    def test_c1_skeleton_tokens_and_insert_positions(self):
        """C1：骨架 token 逐字保留；两处分界只作列的兄弟节点插入（位置次序断言）"""
        for token in ('<div class="workspace">', '<div class="nav-col">', '<div class="main-col">',
                      '<div class="main-row">', '<div class="list-col" id="list"></div>',
                      '<div class="detail-col" id="detail"></div>'):
            self.assertIn(token, self.src, token)
        self.assertIn('<div class="col-resizer col-resizer-nav" id="navColResizer"', self.src)
        self.assertIn('<div class="col-resizer col-resizer-list" id="listColResizer"', self.src)
        i_nav = self.src.index('<div class="nav-col">')
        i_nav_res = self.src.index('id="navColResizer"')
        i_main = self.src.index('<div class="main-col">')
        self.assertLess(i_nav, i_nav_res)
        self.assertLess(i_nav_res, i_main, "左栏分界必须在 nav-col 与 main-col 之间")
        i_list = self.src.index('<div class="list-col" id="list"></div>')
        i_list_res = self.src.index('id="listColResizer"')
        i_detail = self.src.index('<div class="detail-col" id="detail"></div>')
        self.assertLess(i_list, i_list_res)
        self.assertLess(i_list_res, i_detail, "卡片流分界必须在 list 与 detail 两列 token 之间")

    def test_c2_list_write_single_entry(self):
        """C2：#list 唯一写入口口径保持；新代码不得新增 getElementById('list') / list.innerHTML"""
        self.assertEqual(self.src.count("document.getElementById('list')"), 1)
        self.assertEqual(self.src.count("list.innerHTML"), 1)
        self.assertIn("document.querySelector('.list-col')", self.js)

    def test_c3_render_list_sandbox_surface(self):
        """C3：renderList 正文零新标识符、前置签名行原样（保护 sse_dispatch harness 提取面）"""
        body = _extract_function(self.js, "renderList")
        for bad in ("layoutPref", "cardExpandState", "applyLayoutWidths", "clampColWidth",
                    "colMaxWidth", "col-resizer", "LAYOUT_KEY", "CARD_EXPAND_KEY",
                    "cardCollapsed", "initLayoutResizers", "bindColResizer"):
            self.assertNotIn(bad, body, "renderList 不得引用 %s" % bad)
        self.assertIn("let listHtmlSig = null;\n        function renderList() {", self.src)

    def test_c4_card_collapse_wiring(self):
        """C4：卡片折叠接线——判定/裁剪/图标（所有状态）/切换路径/CSS 旋转全部落位"""
        body = _extract_function(self.js, "cardHtml")
        self.assertIn("cardCollapsed(i", body)
        self.assertIn("collapsed ? ' collapsed' : ''", body)
        self.assertEqual(body.count("card-collapse"), 1, "折叠图标只允许一处")
        self.assertNotIn("i.status === 'done' ? `<span class=\"card-collapse", body,
                         "折叠图标不得再受 done 守卫（所有状态可折叠）")
        self.assertIn('<span class="card-collapse', body, "折叠图标必须对所有状态渲染")
        self.assertIn("aria-expanded=\"${collapsed ? 'false' : 'true'}\"", body,
                      "折叠图标 aria-expanded 必须跟随折叠态")
        self.assertIn("collapsed ? '展开角色会话' : '收起角色会话'", body)
        self.assertIn("collapsed ? '' : sessionFooterHtml(i)", body)
        self.assertIn("collapsed ? '' : dispRow", body)
        tb = _extract_function(self.js, "toggleCardExpand")
        self.assertIn("= cardCollapsed(", tb, "新状态直接赋 cardCollapsed 返回值（禁止取反形态）")
        self.assertNotIn("!cardCollapsed(", tb, "取反会让未表态卡首点停在收缩态（#390 教训）")
        self.assertIn("render()", tb)
        self.assertNotIn("classList", tb)
        self.assertNotIn("innerHTML", tb)
        self.assertIn(".card-collapse.open svg", self.src)
        self.assertIn("function cardCollapsed(i, expandState)", self.js)

    def test_c5_drag_wiring(self):
        """C5：拖拽接线——常量不变量 / 指针事件 / 拖动禁过渡 / 收敛挂点"""
        for name in ("clampColWidth", "colMaxWidth", "applyLayoutWidths", "bindColResizer",
                     "initLayoutResizers", "parseStoredObject"):
            self.assertIn("function %s(" % name, self.js, name)
        vals = {}
        for name in NUMERIC_CONSTS:
            m = re.search(r"const %s = (\d+);" % name, self.js)
            self.assertIsNotNone(m, "缺少常量 %s" % name)
            vals[name] = int(m.group(1))
        self.assertEqual(vals["NAV_MIN_W"], 172, "左栏下限 = 原写死宽度 172")
        self.assertGreaterEqual(vals["MAIN_MIN_W"],
                                vals["LIST_MIN_W"] + vals["COL_GAP_W"] + vals["DETAIL_MIN_W"],
                                "主区下限必须 ≥ 卡片流下限 + 间距 + 对话区下限（极限下不压没对话区）")
        body = _extract_function(self.js, "bindColResizer")
        self.assertIn("addEventListener('pointerdown'", body)
        i = self.src.index(".workspace.col-resizing .nav-col")
        self.assertIn("transition: none", self.src[i:i + 200], "拖动中列宽过渡必须禁用")
        self.assertIn(".workspace.col-resizing .list-col { transition: none; }", self.src)
        init = _extract_function(self.js, "initLayoutResizers")
        self.assertIn("addEventListener('resize'", init)
        self.assertIn("applyLayoutWidths()", init)
        nav = _extract_function(self.js, "applyNavCollapsed")
        self.assertIn("applyLayoutWidths()", nav, "折叠/展开后必须回到拖动存储宽度")
        self.assertEqual(self.src.count("initLayoutResizers("), 2, "定义 + 初始化区调用各一处")

    def test_c6_persistence_wiring(self):
        """C6：持久化接线——两存储键、拖动结束落盘、卡片手动态落盘、解析 fail-safe"""
        self.assertIn("const LAYOUT_KEY = 'reqBoardLayout';", self.js)
        self.assertIn("const CARD_EXPAND_KEY = 'reqBoardCardExpand';", self.js)
        body = _extract_function(self.js, "parseStoredObject")
        self.assertIn("try {", body)
        self.assertIn("catch", body)
        drag = _extract_function(self.js, "bindColResizer")
        self.assertIn("localStorage.setItem(LAYOUT_KEY, JSON.stringify(layoutPref))", drag)
        tb = _extract_function(self.js, "toggleCardExpand")
        self.assertIn("localStorage.setItem(CARD_EXPAND_KEY, JSON.stringify(cardExpandState))", tb)

    def test_c7_negative_locks(self):
        """C7：负向锁——折叠图标仅一处且不受 done 守卫、sessionFooterHtml 零改动、无双击重置实现"""
        body = _extract_function(self.js, "cardHtml")
        self.assertEqual(body.count("card-collapse"), 1)
        self.assertNotIn("i.status === 'done' ? `<span class=\"card-collapse", body,
                         "折叠图标必须对所有状态可用")
        self.assertIn('<span class="card-collapse', body)
        sf = _extract_function(self.js, "sessionFooterHtml")
        self.assertNotIn("collapse", sf)
        self.assertNotIn("cardExpandState", sf)
        self.assertNotIn("dblclick", self.src)


def _harness_source():
    js = _script_block()
    parts = [_extract_function(js, name) for name in NEEDED_FUNCS]
    parts.append(_js_const(js, "COL_GAP_W"))
    return "\n\n".join(parts) + "\n\n" + HARNESS_JS.read_text(encoding="utf-8")


class P7LayoutHarnessTest(unittest.TestCase):
    """H1~H4：布局纯函数矩阵（node 断言）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 #7 P7 布局 JS 断言")

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
        """H1~H4：clampColWidth / colMaxWidth / cardCollapsed / parseStoredObject 全通过"""
        checks = self._run_harness().get("checks", [])
        self.assertGreaterEqual(len(checks), 12, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)


if __name__ == "__main__":
    unittest.main()
