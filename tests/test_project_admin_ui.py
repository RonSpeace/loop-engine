#!/usr/bin/env python3
"""#5 项目管理前端测试。

C 组（静态，模板与 static/board.js/board.css 源码文本）：结构（nav-head「+」/行内编辑移除按钮/两个模态与字段/checklist
      容器、既有布局契约保留）、接线（navList 委托分支、删除成功 await renderNav(); render();
      编辑保存 refreshProjectBar、行内按钮属性位 escapeAttr）、harness 提取函数禁区零引用；
      C4/C5：项目弹窗 Git 配置结构（选择文件夹/探测/远端/凭据/同步策略/worktree 只读）、项目页状态条容器、
      选择文件夹→自动探测、凭据保存测试、push 二次确认、状态按需加载（切换/首次加载各一次）接线。
H 组（node harness，tests/project_admin_harness.js）：pmNameValid / pmPathsToArray /
      projectStatsText / projectRemoveBlocked / projectChecklistHtml 矩阵；
      H6~H14：路径追加 / git-detect 回填 / git_sync 表单组装 / 凭据 body 与状态文案 /
      仓库状态文案与状态条 HTML / push 确认文案 / 拉取推送结果文案。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_project_admin_ui.py -q
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
HARNESS_JS = Path(__file__).resolve().parent / "project_admin_harness.js"
sys.path.insert(0, str(REPO))

NEEDED_FUNCS = ("escapeHtml", "escapeAttr", "pmNameValid", "pmPathsToArray",
                "projectStatsText", "projectRemoveBlocked", "projectChecklistHtml",
                "pmPathsAppend", "pmDetectFill", "pmSyncForm", "pmCredentialBody",
                "pmCredentialStateText", "gitRepoStatusText", "gitStatusBarHtml",
                "gitPushConfirmText", "gitOpResultText")

# harness 注入的常量块（纯函数依赖；块内不含 ASCII 分号，_js_const 按第一个 ; 收束）
NEEDED_CONSTS = ("PM_STATUS_ORDER", "PM_GIT_SYNC_VALUES", "PM_GIT_SYNC_DEFAULTS")


def _board_src():
    return frontend_src()


def _script_block():
    return frontend_js()


def _js_const(js, name):
    """提取 const <name> = ...; 常量块源码（用于注入 harness）"""
    m = re.search(r"const %s = .*?;" % re.escape(name), js, re.S)
    if not m:
        raise AssertionError("const %s not found in static/board.js" % name)
    return m.group(0)


class ProjectAdminStaticTest(unittest.TestCase):
    """C1~C3：结构与接线静态断言（源码文本）"""

    @classmethod
    def setUpClass(cls):
        cls.src = _board_src()
        cls.js = _script_block()

    def test_c1_structure(self):
        """C1：入口/行内按钮/两个模态与字段/checklist 容器存在；既有布局契约保留"""
        for token in ('id="navProjectAddBtn"', 'class="nav-project-add"',
                      'class="nav-edit-project"', 'class="nav-remove-project"',
                      'id="projectModal"', 'id="projectModalForm"', 'id="projectModalChecklist"',
                      'id="pmName"', 'id="pmPaths"', 'id="pmBase"', 'id="pmGoal"',
                      'id="pmError"', 'id="pmCancel"', 'id="pmSubmit"', 'id="pmBaseHint"',
                      'id="removeProjectModal"', 'id="removeProjectName"', 'id="removeProjectStats"',
                      'id="removeProjectWarn"', 'id="removeProjectOk"'):
            self.assertIn(token, self.src, token)
        for token in ('<div class="nav-items" id="navList"></div>',
                      '<div class="project-bar" id="projectBar"></div>',
                      '<div class="list-col" id="list"></div>',
                      '<div class="detail-col" id="detail"></div>'):
            self.assertIn(token, self.src, token)

    def test_c2_wiring(self):
        """C2：委托分支 / 删除成功 await renderNav(); render(); / 编辑保存 refreshProjectBar / 转义位"""
        nav = _extract_function(self.js, "renderNav")
        self.assertIn("nav-edit-project", nav)
        self.assertIn("nav-remove-project", nav)
        self.assertIn('data-edit-project="${escapeAttr(p)}"', nav)     # 属性位用 escapeAttr
        self.assertIn('data-remove-project="${escapeAttr(p)}"', nav)
        i = self.js.index("document.getElementById('navList').addEventListener('click'")
        deleg = self.js[i:i + 1500]
        self.assertIn("nav-edit-project", deleg)
        self.assertIn("nav-remove-project", deleg)
        self.assertIn("openProjectModal(editBtn.dataset.editProject)", deleg)
        self.assertIn("openRemoveProjectModal(rmBtn.dataset.removeProject)", deleg)
        rm = _extract_function(self.js, "confirmRemoveProject")
        self.assertIn("await renderNav();", rm)
        self.assertIn("render();", rm)
        self.assertLess(rm.index("await renderNav();"), rm.index("render();"))
        render_body = _extract_function(self.js, "render")
        for token in ("renderList()", "renderDetailPane()", "refreshProjectBar()"):
            self.assertIn(token, render_body)      # render 内置三挂点，删除后全量重绘依据
        submit = _extract_function(self.js, "submitProjectModal")
        self.assertIn("refreshProjectBar();", submit)
        checklist = _extract_function(self.js, "projectChecklistHtml")
        self.assertIn("escapeHtml(agentsMd)", checklist)               # 文本位 escapeHtml
        self.assertIn("escapeHtml(cmd)", checklist)

    def test_c3_harness_functions_clean(self):
        """C3：harness 提取函数不引用禁区全局；renderNav 既有回退逻辑保留"""
        for fn in NEEDED_FUNCS[2:]:
            body = _extract_function(self.js, fn)
            for bad in ("pmEditingName", "pmRemovingName", "pmBaseMapping", "autoProjects",
                        "fetch(", "showToast", "renderNav", "pmSetError"):
                self.assertNotIn(bad, body, "%s 不得引用 %s" % (fn, bad))
        nav = _extract_function(self.js, "renderNav")
        self.assertIn("selectedProject = ALL_PROJECTS;", nav)          # 选中项失效回退保留
        for fn in ("openProjectModal", "closeProjectModal", "submitProjectModal",
                   "openRemoveProjectModal", "closeRemoveProjectModal", "confirmRemoveProject"):
            self.assertGreater(len(_extract_function(self.js, fn)), 0, fn)
        for token in ("navProjectAddBtn", "'pmSubmit'", "'pmCancel'", "'removeProjectOk'",
                      "'removeProjectCancel'"):
            self.assertIn(token, self.js)

    def test_c4_git_config_structure(self):
        """C4：项目弹窗 Git 配置结构（选择文件夹/探测/远端/凭据/同步策略/worktree 只读）+ 项目页状态条容器"""
        for token in ('id="pmPickFolder"', 'id="pmDetect"', 'id="pmDetectResult"',
                      'id="pmRemote"', 'id="pmGitUrl"', 'id="pmCredKind"', 'id="pmCredUser"',
                      'id="pmCredToken"', 'id="pmCredSsh"', 'id="pmCredState"',
                      'id="pmCredSaveTest"', 'id="pmSyncBefore"', 'id="pmSyncOnDone"',
                      'id="pmSyncDiverge"', 'id="pmWorktree"', 'id="projectGitBar"',
                      '按需求 worktree（当前唯一支持）'):
            self.assertIn(token, self.src, token)
        self.assertIn('id="pmCredToken" type="password"', self.src)  # 令牌不回显明文
        for option in ('<option value="off">不拉取</option>', '<option value="fetch">只 fetch</option>',
                       '<option value="pull">拉取并快进</option>',
                       '<option value="off">不推送</option>', '<option value="auto">自动推送</option>',
                       '<option value="manual">手动推送</option>',
                       '<option value="ff-only">仅快进</option>',
                       '<option value="overlap-check">文件重叠判定</option>',
                       '<option value="stop">停止并提示</option>'):
            self.assertIn(option, self.src, option)

    def test_c5_git_wiring(self):
        """C5：选择文件夹/探测/凭据保存测试/状态按需加载/push 二次确认接线"""
        for fn in ("pmPickFolder", "pmDetectRepos", "pmSaveCredential", "pmSaveCredentialAndTest",
                   "refreshProjectGitStatus", "runProjectGitOp", "renderProjectGitBar",
                   "pmCredentialBody", "pmSyncForm", "pmDetectFill", "pmPathsAppend"):
            self.assertGreater(len(_extract_function(self.js, fn)), 0, fn)
        for token in ("'/api/system/pick-folder'", "'/api/system/git-detect'",
                      "'/git/status'", "'/git/'", "'/git/credential'",
                      "push ? 'push' : 'pull'"):
            self.assertIn(token, self.js, token)
        push = _extract_function(self.js, "runProjectGitOp")
        self.assertIn("window.confirm(gitPushConfirmText(", push)   # push 必须二次确认
        self.assertIn("confirm: true", push)
        pick = _extract_function(self.js, "pmPickFolder")
        self.assertIn("pmPathsAppend(", pick)
        self.assertIn("pmDetectRepos()", pick)                     # 选完自动探测
        detect = _extract_function(self.js, "pmDetectRepos")
        self.assertIn("pmDetectFill(", detect)
        submit = _extract_function(self.js, "submitProjectModal")
        self.assertIn("pmCollectGitConfig()", submit)
        self.assertIn("pmCollectSync()", submit)
        cred_save = _extract_function(self.js, "pmSaveCredential")
        self.assertIn("pmCredentialBody(", cred_save)
        self.assertEqual(cred_save.count("document.getElementById('pmCredToken').value = ''"), 1)  # 密码不回显
        load = _extract_function(self.js, "load")
        select = _extract_function(self.js, "selectProject")
        for body in (load, select):
            self.assertIn("refreshProjectGitStatus()", body)       # 切换/首次加载各拉一次（不轮询）
        # 静态 JS 先于模板热更新：新节点接线逐个判空 + 模态入口防御，旧模板窗口期不中断整页初始化
        self.assertIn("if (pmPickBtn) pmPickBtn.addEventListener", self.js)
        self.assertIn("if (!document.getElementById('pmRemote'))", self.js)


def _harness_source():
    js = _script_block()
    parts = []
    for name in NEEDED_FUNCS:
        body = _extract_function(js, name)
        parts.append(body)
    for name in NEEDED_CONSTS:
        parts.append(_js_const(js, name))
    return "\n\n".join(parts) + "\n\n" + HARNESS_JS.read_text(encoding="utf-8")


class ProjectAdminHarnessTest(unittest.TestCase):
    """H1~H5：项目管理纯函数矩阵（node 断言）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 #5 项目管理 JS 断言")

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
        """H1~H5：名称/解析/统计/阻断/checklist 矩阵全通过"""
        checks = self._run_harness().get("checks", [])
        self.assertGreaterEqual(len(checks), 8, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)


if __name__ == "__main__":
    unittest.main()
