#!/usr/bin/env python3
"""#4/#6 模型调参抽屉前端测试。

D1（静态，模板与 static/board.js/board.css 源码文本）：DOM 骨架/函数接线/端点字符串/ESC 关闭/切项目关闭/事件委托；
D3~D6（node harness，tests/robot_drawer_harness.js）：主表渲染（四档来源标注/清除选项/别名池/
      覆盖不在池/降级手填）、统计渲染（分组/状态分布/空态/口径注记）、转义矩阵。
B1~B6（#6 批量保存）：静态接线（保存全部/串行调用/失败不重拉/无改动不请求）+ harness
      渲染（表头按钮/行内无保存按钮/data-cur/表尾注记原文案）+ 批量文案矩阵。
机器人位（具体项目可点 / 所有项目禁用）在 tests/project_bar_harness.js H3f/H4d（D2）。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_robot_drawer_ui.py -q
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "robot_drawer_harness.js"
sys.path.insert(0, str(REPO))

NEEDED_FUNCS = ("escapeHtml", "escapeAttr", "formatTokens", "robotModelRowsHtml",
                "robotCacheCell", "robotStatsSinceParam", "robotStatsHtml", "robotBatchResultText")


def _board_src():
    return frontend_src()


def _script_block():
    return frontend_js()


class RobotDrawerStaticTest(unittest.TestCase):
    """D1/B1/B4/B5/B6：接线静态断言（源码文本）"""

    @classmethod
    def setUpClass(cls):
        cls.src = _board_src()
        cls.js = _script_block()

    def test_d1_dom_and_functions(self):
        for token in ('id="robotDrawer"', 'id="robotMask"', 'id="robotBody"',
                      'id="robotCloseBtn"', 'id="robotReloadBtn"',
                      "function openRobotDrawer(", "function closeRobotDrawer(",
                      "async function loadRobotDrawer(", "async function saveRobotModel(",
                      "async function saveRobotModelsAll(", "function robotBatchResultText(",
                      "function robotModelRowsHtml(", "function robotStatsHtml("):
            self.assertIn(token, self.src, token)
        # 抽屉为右侧滑出（镜像设置抽屉体系：容器复用 sd-head/sd-body + .robot-drawer.show）
        self.assertIn(".robot-drawer.show", self.src)
        self.assertIn("transform: translateX(calc(100% + 24px))", self.src)

    def test_d1_endpoints_and_close_paths(self):
        self.assertIn("'/models'", self.js)
        self.assertIn("'/model-stats'", self.js)
        self.assertIn("'/api/projects/'", self.js)
        self.assertIn("method: 'PUT'", self.js)
        self.assertIn("if (e.key === 'Escape') closeRobotDrawer();", self.js)
        body = _extract_function(self.js, "refreshProjectBar")
        self.assertIn("robotProject !== sel", body)
        self.assertIn("closeRobotDrawer()", body)
        # 事件委托挂在 #projectBar 容器（innerHTML 重绘不失联）
        self.assertIn("getElementById('projectBar').addEventListener('click'", self.src)
        # #6 D1 迁移（逐行保存按钮移除所致）：「下一张卡生效」由批量结果文案产出，
        # saveRobotModelsAll 走 robotBatchResultText；表尾注记断言（D3d）不动。
        save_all = _extract_function(self.js, "saveRobotModelsAll")
        self.assertIn("robotBatchResultText", save_all)
        self.assertIn("下一张卡生效", _extract_function(self.js, "robotBatchResultText"))

    def test_d1_robot_button_states(self):
        bar = _extract_function(self.js, "projectBarHtml")
        self.assertIn("模型调参需选中具体项目", bar)      # all：禁用 + title 提示
        self.assertIn('class="pb-robot on"', bar)         # 具体项目：可点 button
        self.assertIn("aria-disabled", bar)

    def test_b1_save_all_wiring(self):
        """B1：渲染产出 #robotSaveAllBtn / .rb-save-all；行内 .rb-save 不再渲染；加载后接线"""
        rows = _extract_function(self.js, "robotModelRowsHtml")
        self.assertIn('id="robotSaveAllBtn"', rows)
        self.assertIn("rb-save-all", rows)
        self.assertNotIn('class="btn rb-save"', self.js)   # 精确 token：rb-save-all 不误伤
        loader = _extract_function(self.js, "loadRobotDrawer")
        self.assertIn("robotSaveAllBtn", loader)
        self.assertIn("saveRobotModelsAll(project)", loader)
        self.assertNotIn(".rb-save'", loader)              # 旧逐行绑定移除

    def test_f7_save_all_moved_and_three_columns(self):
        """#50：「保存全部」在分组标题行右端；主表 3 列（无思考深度列），loadRobotDrawer 不再传档位词表。"""
        rows = _extract_function(self.js, "robotModelRowsHtml")
        self.assertIn("角色 × 模型（项目级覆盖）", rows)      # 标题随按钮一起移入本函数
        self.assertIn('<div class="rb-head">', rows)
        self.assertIn('id="robotSaveAllBtn"', rows)
        self.assertNotIn("rb-act", rows)                    # 末列已去掉
        self.assertNotIn("rb-act", self.js)                 # 表头末列样式一并移除
        self.assertNotIn("<th class=\"rb-act\"", rows)
        self.assertIn("<th>角色</th><th>当前生效模型</th><th>项目级覆盖</th>", rows)
        self.assertNotIn("<th>思考深度</th>", rows)
        self.assertNotIn("rb-think", rows)
        self.assertNotIn("thinking_levels", rows)
        loader = _extract_function(self.js, "loadRobotDrawer")
        self.assertNotIn("角色 × 模型（项目级覆盖）", loader)  # 调用侧不再重复渲染标题
        self.assertNotIn("md.thinking_levels", loader)       # 档位词表服务端注入已下线
        self.assertIn("robotModelRowsHtml(md.roles, md.aliases, md.aliases && md.aliases.warning)", loader)

    def test_b4_batch_orchestration(self):
        """B4：串行 await saveRobotModel；全成功才重拉；保存中禁用按钮"""
        body = _extract_function(self.js, "saveRobotModelsAll")
        self.assertIn("await saveRobotModel(project, role)", body)
        self.assertIn("if (!failed) loadRobotDrawer();", body)
        self.assertIn("btn.disabled = true", body)
        self.assertIn("保存中…", body)

    def test_b5_single_role_compat(self):
        """B5：saveRobotModel 仍走单角色 PUT、不自行 toast/重拉（批量汇总方为上层）；
        #50：body 只提交 model，无 thinking 控件与错误槽。"""
        body = _extract_function(self.js, "saveRobotModel")
        self.assertIn("method: 'PUT'", body)
        self.assertIn("payload.model", body)
        self.assertNotIn("payload.thinking", body)
        self.assertNotIn(":thinking", body)
        self.assertNotIn("rb-think", body)
        self.assertNotIn("loadRobotDrawer()", body)
        self.assertNotIn("下一张卡生效", body)   # 文案随迁移上收至 robotBatchResultText

    def test_b6_no_change_no_request(self):
        """B6：无改动点「保存全部」→「没有改动」且不发请求（data-cur 比较先于请求）"""
        body = _extract_function(self.js, "saveRobotModelsAll")
        self.assertIn("data-cur", body)
        self.assertIn("没有改动", body)
        self.assertLess(body.index("没有改动"), body.index("await saveRobotModel"))
        rows = _extract_function(self.js, "robotModelRowsHtml")
        self.assertIn('data-cur="${escapeAttr(override)}"', rows)

    def test_f6_batch_collects_models_only(self):
        """F6（#50）：批量变更收集只含模型控件（.rb-select/.rb-input，不含 .rb-think）；
        主表无思考列/错误槽，body 只提交 model。"""
        body = _extract_function(self.js, "saveRobotModelsAll")
        self.assertIn(".rb-select, .rb-input'", body)
        self.assertNotIn("rb-think", body)
        self.assertIn("roles.indexOf(role) === -1", body)          # 同角色去重合并 PUT
        rows = _extract_function(self.js, "robotModelRowsHtml")
        self.assertIn('class="rb-select" data-role="${escapeAttr(r.role)}" data-cur="${escapeAttr(override)}"', rows)
        self.assertNotIn("<th>思考深度</th>", rows)
        self.assertNotIn('data-msg="${escapeAttr(r.role)}:thinking"', rows)
        loader = _extract_function(self.js, "loadRobotDrawer")
        self.assertNotIn("md.thinking_levels", loader)              # 档位词表服务端注入已下线


def _harness_source():
    js = _script_block()
    parts = [_extract_function(js, name) for name in NEEDED_FUNCS]
    return "\n\n".join(parts) + "\n\n" + HARNESS_JS.read_text(encoding="utf-8")


class RobotDrawerHarnessTest(unittest.TestCase):
    """D3~D6 + B2/B3：抽屉渲染矩阵（node 断言）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 #4 抽屉 JS 断言")

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

    def test_d_matrix_pass(self):
        """D3~D6 + B2/B3：主表/降级/统计/转义/批量渲染/批量文案矩阵全通过"""
        checks = self._run_harness().get("checks", [])
        self.assertGreaterEqual(len(checks), 20, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)


if __name__ == "__main__":
    unittest.main()
