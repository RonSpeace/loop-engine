#!/usr/bin/env python3
"""req-387 需求助手受限 python 自验白名单测试：python_safe_cmd 纯函数层（形态 A/B 正反例）
+ _terminal_approve 会话层（开关/roots/红线联动）。

运行（worktree 内，用 main 工作区的 .venv，含全部依赖）：
    .venv/bin/python -m unittest tests.test_python_safe_cmd -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import acp_shared as ac  # noqa: E402


def _stub_session(workdir=None, allow_python_safe=False, python_roots=None):
    """ACPSession 桩：__new__ 绕过 __init__（不起 kimi 子进程），只初始化审批所需属性"""
    s = ac.ACPSession.__new__(ac.ACPSession)
    s.req_id = 99999
    s.role = "analysis"
    s.workdir = Path(workdir) if workdir else Path(tempfile.gettempdir())
    s.artifact_root = Path(tempfile.gettempdir()) / "art"
    s.project = "req-board"
    s.readonly_roots = [s.workdir]
    s.allow_git_commit = False
    s.allow_git_merge = False
    s.allow_python_safe = bool(allow_python_safe)
    s.python_roots = [Path(r).resolve() for r in (python_roots or [])] or None
    s._tool_cmds = {}
    s.proc = None
    s._emit = lambda *a, **k: None
    return s


class L0PythonSafeCmd(unittest.TestCase):
    """python_safe_cmd 纯函数层：形态 A（py_compile）/形态 B（venv unittest discover）正反例"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req387-py-")).resolve()
        (self.tmp / "app.py").write_text("x = 1\n")
        (self.tmp / "my file.py").write_text("x = 2\n")
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "test_app.py").write_text("import unittest\n")
        self.roots = [self.tmp]
        self.base = self.tmp
        self.venv_py = str(self.tmp / ".venv" / "bin" / "python")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 形态 A 正例 ----
    def test_p_pycompile_abs(self):
        self.assertTrue(ac.python_safe_cmd(f"python3 -m py_compile {self.tmp}/app.py", self.roots, self.base))

    def test_p_pycompile_rel(self):
        self.assertTrue(ac.python_safe_cmd("python3 -m py_compile app.py", self.roots, self.base))

    def test_p_pycompile_multi(self):
        self.assertTrue(ac.python_safe_cmd(
            f"python3 -m py_compile app.py {self.tmp}/tests/test_app.py", self.roots, self.base))

    def test_p_pycompile_quoted_space(self):
        self.assertTrue(ac.python_safe_cmd('python3 -m py_compile "my file.py"', self.roots, self.base))

    # ---- 形态 B 正例 ----
    def test_p_venv_unittest_abs(self):
        self.assertTrue(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest discover -s {self.tmp}/tests -v", self.roots, self.base))

    def test_p_venv_unittest_rel(self):
        self.assertTrue(ac.python_safe_cmd(
            ".venv/bin/python -m unittest discover -s tests -v", self.roots, self.base))

    def test_p_venv_unittest_noflag(self):
        self.assertTrue(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest discover", self.roots, self.base))

    def test_p_venv_unittest_all_flags(self):
        cmd = (f"{self.venv_py} -m unittest discover -s tests -p 'test_*.py' -k Foo "
               f"-t {self.tmp} -q -f -c -b")
        self.assertTrue(ac.python_safe_cmd(cmd, self.roots, self.base))

    def test_p_multi_roots(self):
        tmp2 = Path(tempfile.mkdtemp(prefix="req387-py2-")).resolve()
        try:
            (tmp2 / "tests").mkdir()
            roots = [self.tmp, tmp2]
            self.assertTrue(ac.python_safe_cmd(
                f"python3 -m py_compile {tmp2}/app.py", roots, self.base))
            self.assertTrue(ac.python_safe_cmd(
                f"{tmp2}/.venv/bin/python -m unittest discover -s {tmp2}/tests -v", roots, self.base))
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    # ---- 负例：-c / 任意脚本 / 其它模块 / 裸解释器 unittest ----
    def test_n_c_dash_c(self):
        self.assertFalse(ac.python_safe_cmd('python3 -c "print(1)"', self.roots, self.base))

    def test_n_arbitrary_script(self):
        self.assertFalse(ac.python_safe_cmd(f"python3 {self.tmp}/script.py", self.roots, self.base))

    def test_n_venv_arbitrary_script(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.venv_py} {self.tmp}/script.py", self.roots, self.base))

    def test_n_bare_python3_unittest(self):
        self.assertFalse(ac.python_safe_cmd(
            "python3 -m unittest discover -s tests", self.roots, self.base))

    def test_n_pytest_module(self):
        self.assertFalse(ac.python_safe_cmd("python3 -m pytest tests", self.roots, self.base))

    # ---- 负例：路径越界 / venv 形态越界 ----
    def test_n_path_escape(self):
        self.assertFalse(ac.python_safe_cmd(
            "python3 -m py_compile /etc/passwd", self.roots, self.base))

    def test_n_venv_other_root(self):
        self.assertFalse(ac.python_safe_cmd(
            "/opt/other/.venv/bin/python -m unittest discover -s tests", self.roots, self.base))

    def test_n_venv_opt_path(self):
        self.assertFalse(ac.python_safe_cmd(
            "/opt/venv/bin/python -m unittest discover", self.roots, self.base))

    def test_n_venv_dir_name_venv(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.tmp}/venv/bin/python -m unittest discover", self.roots, self.base))

    # ---- 负例：重定向 / 管道 / 拼接（token 层拒，非 #336 护栏链路） ----
    def test_n_redirect_tmp(self):
        self.assertFalse(ac.python_safe_cmd(
            "python3 -m py_compile x.py > /tmp/x.log", self.roots, self.base))

    def test_n_redirect_main_ws(self):
        self.assertFalse(ac.python_safe_cmd(
            f"python3 -m py_compile x.py > {self.tmp}/x.log", self.roots, self.base))

    def test_n_pipe(self):
        self.assertFalse(ac.python_safe_cmd(
            "python3 -m py_compile x.py | head", self.roots, self.base))

    def test_n_semicolon(self):
        self.assertFalse(ac.python_safe_cmd(
            "python3 -m py_compile a.py; python3 -m py_compile b.py", self.roots, self.base))

    def test_n_andand(self):
        self.assertFalse(ac.python_safe_cmd(
            "python3 -m py_compile a.py && python3 -m py_compile b.py", self.roots, self.base))

    def test_n_tilde(self):
        self.assertFalse(ac.python_safe_cmd("python3 -m py_compile ~/x.py", self.roots, self.base))

    def test_n_dollar_var(self):
        self.assertFalse(ac.python_safe_cmd(
            "python3 -m py_compile $HOME/x.py", self.roots, self.base))

    # ---- 负例：形态 B flag 约束 ----
    def test_n_unittest_no_discover(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest", self.roots, self.base))

    def test_n_unittest_unknown_long_flag(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest discover -s tests --grep x", self.roots, self.base))

    def test_n_unittest_unknown_short_flag(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest discover -s tests -x", self.roots, self.base))

    def test_n_unittest_s_escape(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest discover -s /etc", self.roots, self.base))

    def test_n_unittest_t_escape(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest discover -s tests -t /opt", self.roots, self.base))

    def test_n_unittest_dotdot_escape(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest discover -s {self.tmp}/../outside", self.roots, self.base))

    def test_n_unittest_positional(self):
        self.assertFalse(ac.python_safe_cmd(
            f"{self.venv_py} -m unittest discover tests", self.roots, self.base))

    # ---- 负例：形态 A 约束 ----
    def test_n_pycompile_help(self):
        self.assertFalse(ac.python_safe_cmd("python3 -m py_compile --help", self.roots, self.base))

    def test_n_pycompile_no_path(self):
        self.assertFalse(ac.python_safe_cmd("python3 -m py_compile", self.roots, self.base))

    def test_n_pycompile_case(self):
        self.assertFalse(ac.python_safe_cmd(
            f"python3 -m Py_compile {self.tmp}/app.py", self.roots, self.base))

    def test_n_compileall_module(self):
        self.assertFalse(ac.python_safe_cmd(
            f"python3 -m compileall {self.tmp}", self.roots, self.base))

    # ---- 负例：解释器 / symlink / 红线词 ----
    def test_n_interp_python_python2(self):
        self.assertFalse(ac.python_safe_cmd(
            f"python -m py_compile {self.tmp}/app.py", self.roots, self.base))
        self.assertFalse(ac.python_safe_cmd(
            f"python2 -m py_compile {self.tmp}/app.py", self.roots, self.base))

    def test_n_venv_symlink_escape(self):
        outside = Path(tempfile.mkdtemp(prefix="req387-out-")).resolve()
        try:
            (self.tmp / ".venv").symlink_to(outside)
            self.assertFalse(ac.python_safe_cmd(
                f"{self.tmp}/.venv/bin/python -m unittest discover", self.roots, self.base))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_n_newline_redline(self):
        self.assertFalse(ac.python_safe_cmd(
            "python3 -m py_compile x.py\npkill kimi", self.roots, self.base))

    # ---- #423：compile_roots（形态 A 落点收窄） ----
    def test_p_compile_roots_narrows_form_a(self):
        """目标在 roots 内但不在 compile_roots（如主工作区仓库根）→ 拒（防写 __pycache__）"""
        main_ws = Path(tempfile.mkdtemp(prefix="req423-mainws-")).resolve()
        try:
            (main_ws / "app.py").write_text("x = 1\n")
            roots = [self.tmp, main_ws]
            self.assertFalse(ac.python_safe_cmd(f"python3 -m py_compile {main_ws}/app.py", roots,
                                                self.base, compile_roots=[self.tmp]))
            self.assertTrue(ac.python_safe_cmd(f"python3 -m py_compile {self.tmp}/app.py", roots,
                                               self.base, compile_roots=[self.tmp]))
        finally:
            shutil.rmtree(main_ws, ignore_errors=True)

    def test_p_compile_roots_default_fallback(self):
        """compile_roots 缺省回退 roots（#387 兼容：既有调用方行为零变化）"""
        self.assertTrue(ac.python_safe_cmd(f"python3 -m py_compile {self.tmp}/app.py",
                                           self.roots, self.base, compile_roots=None))
        self.assertTrue(ac.python_safe_cmd(f"python3 -m py_compile {self.tmp}/app.py",
                                           self.roots, self.base, compile_roots=[]))


class L1TerminalApproval(unittest.TestCase):
    """_terminal_approve 会话层：开关/roots/红线联动（stub 会话 readonly=True 走只读分支）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req387-term-")).resolve()
        (self.tmp / "app.py").write_text("x = 1\n")
        self.venv_py = f"{self.tmp}/.venv/bin/python"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_p_pycompile_allow(self):
        s = _stub_session(workdir=self.tmp, allow_python_safe=True, python_roots=[self.tmp])
        s._terminal_approve(f"python3 -m py_compile {self.tmp}/app.py")

    def test_p_venv_unittest_allow(self):
        s = _stub_session(workdir=self.tmp, allow_python_safe=True, python_roots=[self.tmp])
        s._terminal_approve(f"{self.venv_py} -m unittest discover -s {self.tmp}/tests -v")

    def test_n_negative_forms_raise(self):
        s = _stub_session(workdir=self.tmp, allow_python_safe=True, python_roots=[self.tmp])
        for cmd in ('python3 -c "print(1)"',
                    "python3 -m pytest tests",
                    "python3 -m py_compile x.py > /tmp/x.log"):
            with self.assertRaises(PermissionError, msg=cmd):
                s._terminal_approve(cmd)

    def test_p_cd_prefix_forms(self):
        """#423：kimi 的 `cd '<cwd>' && ` 前缀剥离后按同一白名单校验（形态 A/B 会话层）"""
        (self.tmp / "tests").mkdir(exist_ok=True)
        s = _stub_session(workdir=self.tmp, allow_python_safe=True, python_roots=[self.tmp])
        s.readonly_roots = [self.tmp]
        s._terminal_approve(f"cd '{self.tmp}' && python3 -m py_compile {self.tmp}/app.py")
        s._terminal_approve(
            f"cd '{self.tmp}' && {self.venv_py} -m unittest discover -s {self.tmp}/tests -v")
        for bad in (f"cd /etc && python3 -m py_compile {self.tmp}/app.py",
                    f"cd '{self.tmp}' && python3 -m pytest tests",
                    f"cd '{self.tmp}' && {self.venv_py} -m unittest discover -s ../.."):
            with self.assertRaises(PermissionError, msg=bad):
                s._terminal_approve(bad)

    def test_n_redline_prior(self):
        s = _stub_session(workdir=self.tmp, allow_python_safe=True, python_roots=[self.tmp])
        with self.assertRaises(PermissionError) as cm:
            s._terminal_approve("python3 -m py_compile x.py ; pkill -f kimi")
        self.assertIn("红线", str(cm.exception))

    def test_n_switch_off(self):
        s = _stub_session(workdir=self.tmp, allow_python_safe=False, python_roots=[self.tmp])
        with self.assertRaises(PermissionError):
            s._terminal_approve(f"python3 -m py_compile {self.tmp}/app.py")

    def test_n_roots_none(self):
        s = _stub_session(workdir=self.tmp, allow_python_safe=True, python_roots=None)
        with self.assertRaises(PermissionError):
            s._terminal_approve(f"python3 -m py_compile {self.tmp}/app.py")


class L2AutoApprovePrecheck(unittest.TestCase):
    """_auto_approve 只读 Bash 预检 OR 链（#387）：python 白名单命中直接放行；
    未命中仍按现有语义交 terminal 层复核（预检不 reject，行为不变）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req387-auto-")).resolve()
        (self.tmp / "app.py").write_text("x = 1\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _approved(self, outcome):
        return outcome.get("outcome", {}).get("outcome") == "selected"

    def _perm_params(self, text):
        return {"options": [{"optionId": "approve_once", "kind": "allow_once"},
                            {"optionId": "reject", "kind": "reject_once"}],
                "toolCall": {"title": "Bash",
                             "content": [{"type": "content",
                                          "content": {"type": "text", "text": text}}]}}

    def test_pycompile_precheck_approved(self):
        s = _stub_session(workdir=self.tmp, allow_python_safe=True, python_roots=[self.tmp])
        p = self._perm_params(f"python3 -m py_compile {self.tmp}/app.py")
        self.assertTrue(self._approved(s._auto_approve(p)))

    def test_venv_unittest_precheck_approved(self):
        s = _stub_session(workdir=self.tmp, allow_python_safe=True, python_roots=[self.tmp])
        p = self._perm_params(f"{self.tmp}/.venv/bin/python -m unittest discover -s {self.tmp}/tests -v")
        self.assertTrue(self._approved(s._auto_approve(p)))

    def test_switch_off_unchanged(self):
        """开关 False：python 命令预检未命中也不 reject（预检语义），由 terminal 层复核兜底"""
        s = _stub_session(workdir=self.tmp, allow_python_safe=False, python_roots=[self.tmp])
        p = self._perm_params(f"python3 -m py_compile {self.tmp}/app.py")
        self.assertTrue(self._approved(s._auto_approve(p)))


if __name__ == "__main__":
    unittest.main()
