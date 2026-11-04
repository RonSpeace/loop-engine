#!/usr/bin/env python3
"""#343 回归：ensure_dev_worktrees 幂等复用不得覆盖既有 base 记录。

现场：dev 在分支 commit 后，need_confirm 恢复/重试/人工续跑再次拉会话进入幂等
复用分支，旧实现无条件用「分支当前 HEAD」覆盖 base，has_code_changes 做
base...branch 三点 diff 恒为空 →「无代码变更」死循环。修复后：
- 既有 base 记录：保持不动（不调 git）；
- base 缺失（旧需求/异常丢失）：补记 merge-base(默认分支, 分支) = 分叉点。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")

# 项目基线钉到最小清单（test 内 mock PROJECTS 自钉 t-proj，此处只为 import dispatcher 可用）
from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

REQ = {"id": 3430, "project": "t-proj", "created_at": "2026-08-17 00:00:00",
       "title": "t", "notes": ""}


class EnsureBaseStabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.wt = Path(self.tmp.name) / "wt"
        (self.wt / ".git").mkdir(parents=True)  # worktree 目录已存在 → 走幂等复用分支
        self._real_state = d.state
        d.state = {"worktrees": {}}
        patcher = mock.patch.multiple(
            d, save_state=lambda: None,
            PROJECTS={"t-proj": {"paths": [str(self.repo)]}},
            dev_worktree_path=lambda req, p: self.wt,
            dev_workdir_for=lambda req: self.wt)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        d.state = self._real_state
        self.tmp.cleanup()

    def test_existing_base_not_overwritten(self):
        """分支 commit 后（HEAD 已前进于 base）再次 ensure：base 保持原记录不变"""
        recorded = "a" * 40
        d.state = {"worktrees": {"3430": {"base": {str(self.repo): recorded}}}}
        with mock.patch.object(d.subprocess, "run") as run:
            run.side_effect = AssertionError(
                f"既有 base 不应触发 git 调用: {run.call_args}")
            wd, repo_map = d.ensure_dev_worktrees(REQ)
        base = d.state["worktrees"]["3430"]["base"][str(self.repo)]
        self.assertEqual(base, recorded)

    def test_missing_base_backfilled_with_merge_base(self):
        """base 缺失时补记：取 merge-base(默认分支, 分支) 结果，而非分支 HEAD"""
        d.state = {"worktrees": {}}
        seen = []

        def fake_run(cmd, **k):
            seen.append(cmd)
            return SimpleNamespace(returncode=0, stdout="mb111\n")

        with mock.patch.object(d.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(d, "_default_branch", return_value=("main", "def222")):
            d.ensure_dev_worktrees(REQ)
        base = d.state["worktrees"]["3430"]["base"][str(self.repo)]
        self.assertEqual(base, "mb111")
        self.assertEqual(seen, [["git", "merge-base", "main", d.dev_branch(REQ)]])

    def test_missing_base_merge_base_fail_fallback_default(self):
        """merge-base 失败（如分支刚建无共同历史）→ 回退默认分支 HEAD，不留空"""
        d.state = {"worktrees": {}}

        def fake_run(cmd, **k):
            return SimpleNamespace(returncode=1, stdout="")

        with mock.patch.object(d.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(d, "_default_branch", return_value=("main", "def222")):
            d.ensure_dev_worktrees(REQ)
        base = d.state["worktrees"]["3430"]["base"][str(self.repo)]
        self.assertEqual(base, "def222")


class TestDefaultBranchConfig(unittest.TestCase):
    """base_branch 项目配置（projects.json 可选）：优先于默认分支探测（master→main→
    origin/HEAD→HEAD）。现场：某项目本地 master 落后开发主线
    f-20260303-bill 144 提交，未配置时 worktree 基线取 master 导致 analysis
    「代码搜不到」误判。"""

    def _make_repo(self):
        tmp = tempfile.TemporaryDirectory()
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "f").write_text("1")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        # develop 领先 master 一个提交：配置场景应取 develop，不取 master
        subprocess.run(["git", "branch", "develop"], cwd=repo, check=True)
        (repo / "f").write_text("2")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "dev"], cwd=repo, check=True)
        return tmp, repo

    def test_config_branch_priority(self):
        """配置 base_branch 存在时返回该分支而非本地 master"""
        tmp, repo = self._make_repo()
        self.addCleanup(tmp.cleanup)
        ref, sha = d._default_branch(repo, {"base_branch": "develop"})
        expect = subprocess.run(["git", "rev-parse", "develop"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(ref, "develop")
        self.assertEqual(sha, expect)

    def test_config_branch_missing_fallback(self):
        """配置的分支不存在 → 回退默认探测（本地 master），不抛异常"""
        tmp, repo = self._make_repo()
        self.addCleanup(tmp.cleanup)
        ref, sha = d._default_branch(repo, {"base_branch": "no-such-branch"})
        expect = subprocess.run(["git", "rev-parse", "master"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(ref, "master")
        self.assertEqual(sha, expect)

    def test_config_branch_dict_form(self):
        """dict 形态 {仓库路径: 分支} 按仓库匹配"""
        tmp, repo = self._make_repo()
        self.addCleanup(tmp.cleanup)
        ref, _ = d._default_branch(repo, {"base_branch": {str(repo): "develop"}})
        self.assertEqual(ref, "develop")
        # 其它仓库路径不命中 → 回退默认探测
        ref2, _ = d._default_branch(repo, {"base_branch": {str(repo) + "x": "develop"}})
        self.assertEqual(ref2, "master")

    def test_load_projects_passthrough_base_branch(self):
        """#369 回归：load_projects 归一化必须透传 base_branch，否则配置永不生效
        （现场：_default_branch 支持 base_branch 但归一化丢字段，worktree 基线
        错拿本地 master）。直接传 dict 给 _default_branch 的用例绕过了归一化，
        测不出该 bug。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pj = Path(tmp.name) / "projects.json"
        pj.write_text(json.dumps({
            "p1": {"paths": ["/tmp/r1"], "company": True,
                   "base_branch": "f-20260303-bill"},
            "p2": {"paths": ["/tmp/r2"],
                   "base_branch": {"/tmp/r2": "develop"}},
            "p3": "/tmp/r3",
        }))
        with mock.patch.object(d, "PROJECTS_FILE", pj):
            out = d.load_projects()
        self.assertEqual(out["p1"]["base_branch"], "f-20260303-bill")
        self.assertEqual(out["p2"]["base_branch"], {"/tmp/r2": "develop"})
        self.assertIsNone(out["p3"]["base_branch"])


if __name__ == "__main__":
    unittest.main()
