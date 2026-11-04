#!/usr/bin/env python3
"""#420 测试套件隔离防线 L0 测试（dispatcher.py 三机制）：

1. 落盘守卫：save_state 在未 load_state 的进程一律拒绝（零文件 IO + 告警含路径）
2. 路径可覆盖：REQ_BOARD_STATE 覆盖 STATE_FILE（未设时默认路径与 Path 类型不变）
3. 测试进程短路：ensure_dev_worktrees 在「测试进程 + 未隔离路径」下不 mkdir / 不跑 git /
   不写 state；已隔离用例（路径被 patch）照走真实逻辑，不被误伤

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest tests.test_state_isolation_guard -v

子进程用例用 sys.executable（= 跑本套件的 venv），import dispatcher 需要
REPO/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防误连生产

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_PY = sys.executable   # 跑本套件的解释器（子进程用例复用，保证依赖一致）
_DEFAULT_STATE = str(REPO / "data" / "dispatcher_state.json")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _env(extra=None):
    """子进程环境：摘掉本进程可能带上的测试钩子，再叠加用例要求。"""
    env = dict(os.environ)
    env.pop("REQ_BOARD_STATE", None)
    env.pop("REQ_BOARD_TEST", None)
    env.update(extra or {})
    return env


def _run_child(code, extra_env=None):
    return subprocess.run([_PY, "-c", code], capture_output=True, text=True,
                          cwd=str(REPO), env=_env(extra_env), timeout=120)


class SaveStateNotLoadedGuard(unittest.TestCase):
    """T1-T3：落盘守卫（真实 save_state，STATE_FILE patch 到临时目录）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req420-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sf = self.tmp / "dispatcher_state.json"
        self._reals = {n: getattr(d, n) for n in ("STATE_FILE", "_state_loaded", "state")}
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._reals.items():
            setattr(d, name, value)

    def test_t1_refuse_save_before_load(self):
        """未 load：文件字节不变、无 .tmp 残留、告警含目标路径"""
        d.STATE_FILE = self.sf
        sentinel = '{"keep": "sentinel"}'
        self.sf.write_text(sentinel)
        d._state_loaded = False
        d.state = {"chat_id": "should-not-land"}
        with self.assertLogs("dispatcher", level="WARNING") as cm:
            d.save_state()
        self.assertEqual(self.sf.read_text(), sentinel)
        self.assertFalse(self.sf.with_name(self.sf.name + ".tmp").exists())
        self.assertTrue(any(str(self.sf) in line for line in cm.output), cm.output)

    def test_t2_load_state_sets_flag_and_normalizes(self):
        """load_state 置位在读文件之前：尾部自身 save_state 不被拒（文件由无到有）"""
        d.STATE_FILE = self.sf
        d._state_loaded = False
        d.state = {}
        self.assertFalse(self.sf.exists())
        d.load_state()
        self.assertIs(d._state_loaded, True)
        self.assertTrue(self.sf.exists())
        saved = json.loads(self.sf.read_text())
        self.assertIn("pending_restart", saved)

    def test_t3_save_after_load_writes(self):
        """load 之后写盘照常（守卫不误伤正常路径）"""
        d.STATE_FILE = self.sf
        d._state_loaded = True
        d.state = {"chat_id": "oc_after_load", "retries": {}}
        d.save_state()
        self.assertEqual(json.loads(self.sf.read_text())["chat_id"], "oc_after_load")


class StateFileEnvOverrideTest(unittest.TestCase):
    """T4-T6：REQ_BOARD_STATE 覆盖（真实 venv 子进程，避开本进程已 import 的模块态）"""

    _PROBE = f"import sys; sys.path.insert(0, {str(REPO)!r}); import dispatcher as dd;" \
             "print(type(dd.STATE_FILE).__name__); print(dd.STATE_FILE)"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req420-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_t4_env_override_path_and_type(self):
        target = self.tmp / "s.json"
        r = _run_child(self._PROBE, {"REQ_BOARD_STATE": str(target)})
        self.assertEqual(r.returncode, 0, r.stderr)
        kind, value = r.stdout.strip().splitlines()
        self.assertEqual(kind, "PosixPath")
        self.assertEqual(value, str(target))

    def test_t5_env_unset_default_path(self):
        r = _run_child(self._PROBE)
        self.assertEqual(r.returncode, 0, r.stderr)
        kind, value = r.stdout.strip().splitlines()
        self.assertEqual(kind, "PosixPath")
        self.assertEqual(value, _DEFAULT_STATE)

    def test_t6_import_only_process_never_writes(self):
        """只 import 不跑 main 的进程调 save_state：副本字节与 mtime 均不变 + 告警"""
        target = self.tmp / "real_copy.json"
        target.write_text('{"chat_id": "keep", "sessions": {}}')
        before_sha, before_mtime = _sha(target), target.stat().st_mtime_ns
        code = f"import sys; sys.path.insert(0, {str(REPO)!r}); import dispatcher as dd; dd.save_state()"
        r = _run_child(code, {"REQ_BOARD_STATE": str(target)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_sha(target), before_sha)
        self.assertEqual(target.stat().st_mtime_ns, before_mtime)
        self.assertIn("save_state 被拒绝", r.stderr)


class EnsureDevWorktreesShortCircuitTest(unittest.TestCase):
    """T7/T8：测试进程短路（T7）与已隔离用例照走真实逻辑（T8，短路不误伤）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req420-wt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._reals = {n: getattr(d, n) for n in
                       ("PROJECTS", "WORKTREES_ROOT", "state", "save_state")}
        self.addCleanup(self._restore)
        self.req = {"id": 420999, "project": "t-iso-probe", "created_at": "2026-09-18 00:00:00",
                    "title": "t", "notes": ""}

    def _restore(self):
        for name, value in self._reals.items():
            setattr(d, name, value)

    def test_t7_unisolated_call_short_circuits(self):
        """测试进程 + 真实路径（未 patch）：零 subprocess、零 save_state、不建目录，返回形状不变。

        子进程内执行：本用例必须刻意以真实路径调用真实 ensure_dev_worktrees（探针会替换该函数，
        且巡检 A 判据「真实路径调用即视为会真建」会把这一刻意调用误报为未隔离），子进程执行
        保持 scripts/test_isolation_audit.py 的 A 清单 = 目标实际状态。子进程 import unittest
        复刻套件触发条件（与 T9 的口径一致）。"""
        probe_repo = self.tmp / "repoA"
        code = textwrap.dedent(f"""
            import sys, json
            sys.path.insert(0, {str(REPO)!r})
            import unittest                        # 测试进程判定（与套件同款触发）
            from unittest import mock
            import dispatcher as d
            req = {self.req!r}
            d.PROJECTS = {{req["project"]: {{"paths": [{str(probe_repo)!r}]}}}}
            run_mock = mock.Mock(side_effect=AssertionError("短路下不得调用 subprocess.run"))
            save_mock = mock.Mock()
            with mock.patch.object(d, "subprocess", mock.Mock(run=run_mock)), \\
                    mock.patch.object(d, "save_state", save_mock):
                wd, repo_map = d.ensure_dev_worktrees(req)
            print(json.dumps({{"run_called": run_mock.called, "save_called": save_mock.called,
                               "wd": str(wd), "repo_map": {{k: str(v) for k, v in repo_map.items()}}}}))
        """)
        r = _run_child(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertFalse(out["run_called"])
        self.assertFalse(out["save_called"])
        wt_dir = d.WORKTREES_ROOT / self.req["project"] / "dev" / f"req-{self.req['id']}"
        self.addCleanup(shutil.rmtree, wt_dir.parents[1], ignore_errors=True)  # 短路失效时只清本假项目目录
        self.assertFalse(wt_dir.exists(), "短路下不得在真实 worktrees 根建目录")
        self.assertEqual(out["repo_map"], {"repoA": str(wt_dir / "repoA")})
        self.assertEqual(out["wd"], str(wt_dir / "repoA"))

    def test_t8_isolated_call_still_uses_real_logic(self):
        """WORKTREES_ROOT 被 patch（已隔离）→ 判据不成立：照走 git 调用并写 state"""
        repo = self.tmp / "repoA"
        repo.mkdir()
        d.PROJECTS = {self.req["project"]: {"paths": [str(repo)]}}
        d.WORKTREES_ROOT = self.tmp / "worktrees"
        d.state = {"worktrees": {}}
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "--verify" in cmd:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout="sha1")

        saves = []
        with mock.patch.object(d, "subprocess", mock.Mock(run=fake_run)), \
                mock.patch.object(d, "_default_branch", lambda r, cfg=None: ("main", "sha1")), \
                mock.patch.object(d, "save_state", lambda: saves.append(1)), \
                mock.patch.object(d, "_link_repo_local_config", lambda wt: None):
            d.ensure_dev_worktrees(self.req)
        branch = d.dev_branch(self.req)
        wt = d.dev_worktree_path(self.req, str(repo))
        self.assertIn(["git", "worktree", "add", str(wt), "-b", branch, "main"], calls)
        rec = d.state["worktrees"][str(self.req["id"])]
        self.assertEqual(rec["branch"], branch)
        self.assertEqual(rec["base"][str(repo)], "sha1")
        self.assertEqual(saves, [1])
        self.assertTrue(wt.parent.exists(), "已隔离用例应照走真实逻辑（mkdir 生效）")


class TestEnvDetectionTest(unittest.TestCase):
    """T9：in_test_env 判据——测试进程 True；生产进程（venv 子进程不设 env）恒 False"""

    _PROBE = f"import sys; sys.path.insert(0, {str(REPO)!r}); import dispatcher as dd; print(dd.in_test_env())"

    def test_t9_test_env_detection(self):
        self.assertIs(d.in_test_env(), True)
        r = _run_child(self._PROBE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")
        r = _run_child(self._PROBE, {"REQ_BOARD_TEST": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
