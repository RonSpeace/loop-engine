#!/usr/bin/env python3
"""req-423 只读角色 Bash 的 cd 前缀剥离 + 评审角色受限 python 自验测试。

四层：
A. strip_cd_prefix 纯函数（剥离语义/越界/畸形 fail-closed）；
B. 只读出口覆盖（dbq / readonly_cmd / pipeline / python A·B / git 不回归）；
C. 两层同跑参数化（放行/拒绝表 × _readonly_bash_ok + _terminal_approve + _auto_approve 预检）；
D. 同源结构断言 + 无 cd 前缀既有行为回归。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_readonly_cd_prefix -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防误连生产

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402


def _stub_session(workdir, artifact=None, role="code_review", allow_git_commit=False,
                  allow_git_merge=False, allow_python_safe=False, python_roots=None,
                  python_compile_roots=None, readonly_roots=None):
    """ACPSession 桩：__new__ 绕过 __init__（不起 kimi 子进程），只初始化审批所需属性"""
    s = d.ACPSession.__new__(d.ACPSession)
    s.req_id = 99999
    s.role = role
    s.readonly = True
    s.workdir = Path(workdir)
    s.artifact_root = Path(artifact) if artifact else Path(workdir) / "art"
    s.project = "req-board"
    s.readonly_roots = [Path(r) for r in (readonly_roots or [s.workdir, s.artifact_root])]
    s.allow_git_commit = allow_git_commit
    s.allow_git_merge = allow_git_merge
    s.allow_python_safe = allow_python_safe
    s.python_roots = [Path(r).resolve() for r in (python_roots or [])] or None
    s.python_compile_roots = [Path(r).resolve() for r in (python_compile_roots or [])] or None
    s._tool_cmds = {}
    s.proc = None
    s._emit = lambda *a, **k: None
    return s


def _perm_params(cmd_text, tool_call_id="t1"):
    """审批参数：展示文本是 kimi 会截断的短文本，完整命令走 _tool_cmds 缓存（生产形态）"""
    return {"options": [{"optionId": "approve_once", "kind": "allow_once"},
                        {"optionId": "reject", "kind": "reject_once"}],
            "toolCall": {"title": "Bash", "toolCallId": tool_call_id,
                         "content": [{"type": "content",
                                      "content": {"type": "text",
                                                  "text": f"Requesting approval to Running: {cmd_text[:60]}"}}]}}


def _approved(outcome):
    return outcome.get("outcome", {}).get("outcome") == "selected"


class AStripCdPrefix(unittest.TestCase):
    """A. strip_cd_prefix 纯函数：剥离语义 + 三态返回 + fail-closed"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req423-cd-")).resolve()
        (self.tmp / "sub").mkdir()
        (self.tmp / "art").mkdir()
        self.W = str(self.tmp)
        self.A = str(self.tmp / "art")
        self.roots = [self.W, self.A]
        self.base = self.W

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ok(self, text, exp_rest, exp_eff):
        st = ac.strip_cd_prefix(text, self.roots, base=self.base)
        self.assertIsNotNone(st, text)
        rest, eff = st
        self.assertEqual(rest, exp_rest, text)
        self.assertEqual(str(eff), exp_eff, text)

    def test_a1_single_quote(self):
        self._ok(f"cd '{self.W}' && ls -la", "ls -la", self.W)

    def test_a2_double_quote_and_bare(self):
        self._ok(f'cd "{self.W}" && date', "date", self.W)
        self._ok(f"cd {self.W} && date", "date", self.W)

    def test_a3_multi_segments(self):
        self._ok(f"cd '{self.W}' && cd {self.W}/sub && ls -la", "ls -la", str(self.tmp / "sub"))

    def test_a4_relative_subdir(self):
        self._ok(f"cd '{self.W}' && cd sub && ls", "ls", str(self.tmp / "sub"))

    def test_a5_no_prefix_passthrough(self):
        for text in ("ls -la", "date", "ls data/; ls data/", ""):
            self.assertEqual(ac.strip_cd_prefix(text, self.roots, base=self.base),
                             (text, Path(self.base).resolve()), text)

    def test_a6_out_of_roots(self):
        for text in ("cd /etc && ls",
                     f"cd '{self.tmp.parent}' && ls",
                     f"cd '{self.W}' && cd ../../.. && ls"):
            self.assertIsNone(ac.strip_cd_prefix(text, self.roots, base=self.base), text)

    def test_a7_later_segment_out_of_roots(self):
        self.assertIsNone(
            ac.strip_cd_prefix(f"cd '{self.W}' && cd /etc && ls", self.roots, base=self.base))

    def test_a8_escape_then_return_fail_closed(self):
        self.assertIsNone(
            ac.strip_cd_prefix(f"cd '{self.W}' && cd ../../x && cd '{self.W}' && ls",
                               self.roots, base=self.base))

    def test_a9_malformed_or_only_cd(self):
        for text in ("cd && ls", "cd '' && ls", f'cd "{self.W} && ls', f"cd '{self.W}'",
                     f"cd '{self.W}' &&"):
            self.assertIsNone(ac.strip_cd_prefix(text, self.roots, base=self.base), text)

    def test_a10_remainder_lossless(self):
        self._ok(f"cd '{self.W}' && cat \"a && b\"", 'cat "a && b"', self.W)

    def test_a11_non_cd_command_word(self):
        """`cdrom` 等不以 cd 为独立命令词 → 不算前缀，原样返回（由白名单二次拒绝）"""
        self.assertEqual(ac.strip_cd_prefix("cdrom && ls", self.roots, base=self.base),
                         ("cdrom && ls", Path(self.base).resolve()))


class BExitCoverage(unittest.TestCase):
    """B. 剥离后的剩余部分覆盖各只读出口（cd 前缀不改变出口判定）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req423-exit-")).resolve()
        (self.tmp / "art").mkdir()
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "test_a.py").write_text("import unittest\n")
        (self.tmp / "x.py").write_text("x = 1\n")
        (self.tmp / ".venv" / "bin").mkdir(parents=True)
        (self.tmp / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        self.W = str(self.tmp)
        self.A = str(self.tmp / "art")
        self.venv_py = f"{self.W}/.venv/bin/python"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _s(self, **kw):
        kw.setdefault("allow_python_safe", True)
        kw.setdefault("python_roots", [self.W])
        kw.setdefault("python_compile_roots", [self.W, self.A])
        return _stub_session(self.W, self.A, **kw)

    def test_b1_pipeline(self):
        self.assertTrue(self._s()._readonly_bash_ok(f"cd '{self.W}' && ls -la | head -5"))

    def test_b2_dbq(self):
        db = str(Path(ac.BOARD_DB).resolve())
        self.assertTrue(self._s()._readonly_bash_ok(
            f"cd '{self.W}' && python3 {ac.DBQ_SCRIPT} {db} \"SELECT 1\""))

    def test_b3_python_form_b_venv_unittest(self):
        self.assertTrue(self._s()._readonly_bash_ok(
            f"cd '{self.W}' && {self.venv_py} -m unittest discover -s tests"))

    def test_b4_python_form_a_py_compile(self):
        self.assertTrue(self._s()._readonly_bash_ok(f"cd '{self.W}' && python3 -m py_compile {self.W}/x.py"))

    def test_b5_git_exit_no_regression(self):
        s = self._s(allow_git_commit=True, allow_git_merge=True)
        self.assertTrue(s._readonly_bash_ok(f"cd {self.W} && git status -s"))

    def test_b6_git_exit_blocked_by_bad_cd(self):
        s = self._s(allow_git_commit=True, allow_git_merge=True)
        self.assertFalse(s._readonly_bash_ok("cd /etc && git status"))

    def test_b7_py_compile_outside_compile_roots(self):
        """形态 A 落点收窄：主工作区根∈python_roots 但∉compile_roots → 拒（防写 __pycache__）"""
        other = Path(tempfile.mkdtemp(prefix="req423-mainws-")).resolve()
        self.addCleanup(shutil.rmtree, other, True)
        s = _stub_session(self.W, self.A, allow_python_safe=True,
                          python_roots=[self.W, str(other)], python_compile_roots=[self.W, self.A])
        self.assertFalse(s._readonly_bash_ok(f"cd '{self.W}' && python3 -m py_compile {other}/x.py"))
        self.assertTrue(s._readonly_bash_ok(f"cd '{self.W}' && python3 -m py_compile {self.W}/x.py"))

    def test_b8_bare_python3_unittest_rejected(self):
        """裸 python3 跑 unittest 不在白名单形态内（必须 venv 解释器精确匹配）"""
        self.assertFalse(self._s()._readonly_bash_ok(
            f"cd '{self.W}' && python3 -m unittest discover -s tests"))


class CTwoLayersSameVerdict(unittest.TestCase):
    """C. 两层同跑参数化：同一脚本在 _readonly_bash_ok / _terminal_approve / _auto_approve
    预检三处结论一致（terminal 层是机制保证；预检 miss 不 reject 属既有语义）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req423-2l-")).resolve()
        (self.tmp / "art").mkdir()
        (self.tmp / "sub").mkdir()
        (self.tmp / "tests").mkdir()
        (self.tmp / ".venv" / "bin").mkdir(parents=True)
        (self.tmp / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (self.tmp / "x.py").write_text("x = 1\n")
        self.W = str(self.tmp)
        self.A = str(self.tmp / "art")
        self.venv_py = f"{self.W}/.venv/bin/python"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self):
        return _stub_session(self.W, self.A, role="code_review", allow_python_safe=True,
                             python_roots=[self.W], python_compile_roots=[self.W, self.A])

    def allow_cases(self):
        return [
            f"cd '{self.W}' && ls -la",
            f"cd '{self.W}' && date",
            f"cd '{self.A}' && ls",
            f"cd '{self.W}' && cd {self.W}/sub && ls -la",
            f"cd '{self.W}' && {self.venv_py} -m unittest discover -s tests",
            f"cd '{self.W}' && ls -la | head -5",
            f"cd '{self.W}' && python3 {ac.DBQ_SCRIPT} {ac.BOARD_DB} \"SELECT 1\"",
        ]

    def deny_cases(self):
        return [
            "cd /etc && cat passwd",
            f"cd '{self.tmp.parent}' && ls",
            f"cd '{self.W}' && rm -rf x",
            f"cd '{self.W}' && cat /etc/passwd",
            f"cd '{self.W}' && ls > /tmp/f",
            f"cd '{self.W}' && python3 -c \"print(1)\"",
            f"cd '{self.W}' && cd ../../.. && ls",
            f"cd {self.W} && git push origin main",
            f"cd '{self.W}' && python3 -m unittest discover -s tests",
        ]

    def _assert_layer_consistency(self, cmd, expect):
        s = self._session()
        s._tool_cmds["t1"] = cmd  # 预检命中缓存（生产：tool_call_update 完整命令）
        self.assertEqual(s._readonly_bash_ok(cmd), expect, cmd)
        if expect:
            s._terminal_approve(cmd)  # 不抛即放行
        else:
            with self.assertRaises(PermissionError, msg=cmd):
                s._terminal_approve(cmd)
        with mock.patch.object(ac.log, "warning") as mw:
            self.assertTrue(_approved(s._auto_approve(_perm_params(cmd))), cmd)  # 预检不阻断
            warned = any("预检未命中" in str(c.args[0]) for c in mw.call_args_list)
        self.assertEqual(warned, not expect, f"预检 warning 与结论不符: {cmd}")

    def test_c1_allow_cases(self):
        for cmd in self.allow_cases():
            self._assert_layer_consistency(cmd, True)

    def test_c2_deny_cases(self):
        for cmd in self.deny_cases():
            self._assert_layer_consistency(cmd, False)


class DSameSourceAndRegression(unittest.TestCase):
    """D. 两层同源结构断言 + 无 cd 前缀既有行为零变化"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req423-reg-")).resolve()
        (self.tmp / "art").mkdir()
        (self.tmp / "a.txt").write_text("hello\n")
        self.W = str(self.tmp)
        self.A = str(self.tmp / "art")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_d1_both_layers_call_shared_entry(self):
        """预检层与 terminal 层都经 _readonly_bash_ok（同源唯一入口，spy ≥1 次/层）"""
        s = _stub_session(self.W, self.A)
        calls = []
        orig = ac.ACPSession._readonly_bash_ok

        def spy(self, text):
            calls.append(text)
            return orig(self, text)

        with mock.patch.object(ac.ACPSession, "_readonly_bash_ok", spy):
            s._auto_approve(_perm_params("ls -la"))
            s._terminal_approve("ls -la")
        self.assertGreaterEqual(len(calls), 2, calls)

    def test_d2_precheck_roots_uses_readonly_roots(self):
        """预检层 roots 与 terminal 层同取 readonly_roots（D2）：产物目录内路径两层一致放行"""
        art = Path(tempfile.mkdtemp(prefix="req423-sep-art-")).resolve()
        self.addCleanup(shutil.rmtree, art, True)
        s = _stub_session(self.W, art, readonly_roots=[self.W, art])
        cmd = f"cd '{art}' && ls"
        self.assertTrue(s._readonly_bash_ok(cmd))
        s._terminal_approve(cmd)
        s2 = _stub_session(self.W, art, readonly_roots=[self.W])  # 产物目录不在 roots → 拒
        self.assertFalse(s2._readonly_bash_ok(cmd))

    def test_d3_no_prefix_commands_unchanged(self):
        s = _stub_session(self.W, self.A)
        for cmd in ("ls -la", "date", "ls data/; ls data/", "cat AGENTS.md | head -3"):
            self.assertTrue(s._readonly_bash_ok(cmd), cmd)
            s._terminal_approve(cmd)
        for cmd in ("ls && ls", "ls > /tmp/f", "rm -rf /", "git -C /etc status"):
            self.assertFalse(s._readonly_bash_ok(cmd), cmd)
            with self.assertRaises(PermissionError, msg=cmd):
                s._terminal_approve(cmd)

    def test_d4_python_switch_off_unchanged(self):
        """未开 python_safe 的只读会话（analysis 等）：cd 前缀 + venv unittest 仍拒"""
        (self.tmp / ".venv" / "bin").mkdir(parents=True)
        s = _stub_session(self.W, self.A, role="analysis")
        self.assertFalse(s._readonly_bash_ok(
            f"cd '{self.W}' && {self.W}/.venv/bin/python -m unittest discover -s tests"))


if __name__ == "__main__":
    unittest.main()
