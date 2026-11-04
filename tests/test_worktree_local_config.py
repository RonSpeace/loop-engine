#!/usr/bin/env python3
"""dev worktree 本地配置同源（软链 projects.json）测试。

worktree 内没有 projects.json 时，50 个测试文件自带的 bootstrap 会写入一份**只有 1 个项目**的
最小清单，于是 PROJECTS 在 worktree=1、main=7，凡遍历 PROJECTS 的次数/列表断言两边结论不同
且不报错（#419 合并带出的现场：worktree 全绿、main 转红）。本测试覆盖
`dispatcher._link_repo_local_config` 的全部分支，以及 `ensure_dev_worktrees` 的接线。

运行：
    .venv/bin/python -m unittest tests.test_worktree_local_config -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


def dst_readlink(p: Path) -> str:
    import os
    return os.readlink(p)


class LinkRepoLocalConfigTest(unittest.TestCase):
    """分支覆盖：建链 / 幂等 / 指向别处重建 / 断链自愈 / 普通文件替换 / 源缺失 / OSError 容错"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wt-localcfg-"))
        self.src = self.tmp / "main-projects.json"
        self.src.write_text('{"req-board": "/x"}', encoding="utf-8")
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self._pj = d.PROJECTS_FILE
        d.PROJECTS_FILE = self.src

    def tearDown(self):
        d.PROJECTS_FILE = self._pj
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def dst(self):
        return self.wt / "projects.json"

    def test_creates_symlink_and_content_matches_source(self):
        d._link_repo_local_config(self.wt)
        self.assertTrue(self.dst.is_symlink())
        self.assertEqual(Path(dst_readlink(self.dst)), self.src)
        # 通过软链读到的是主工作区那份内容（同源，而非最小清单）
        self.assertEqual(json.loads(self.dst.read_text(encoding="utf-8")), {"req-board": "/x"})

    def test_idempotent_skips_existing_correct_link(self):
        d._link_repo_local_config(self.wt)
        ino = self.dst.lstat().st_ino
        calls = []
        real = Path.symlink_to

        def spy(self_, target, **kw):
            calls.append(target)
            return real(self_, target, **kw)

        with mock.patch.object(Path, "symlink_to", spy):
            d._link_repo_local_config(self.wt)
        self.assertEqual(calls, [], "已是正确软链时不得重建")
        self.assertEqual(self.dst.lstat().st_ino, ino)

    def test_relinks_when_pointing_elsewhere(self):
        other = self.tmp / "other.json"
        other.write_text("{}", encoding="utf-8")
        self.dst.symlink_to(other)
        d._link_repo_local_config(self.wt)
        self.assertEqual(Path(dst_readlink(self.dst)), self.src)

    def test_heals_broken_symlink(self):
        """断链软链的 exists() 为 False——存在性判断必须含 is_symlink()，否则无法自愈"""
        self.dst.symlink_to(self.tmp / "gone.json")
        self.assertFalse(self.dst.exists())
        d._link_repo_local_config(self.wt)
        self.assertEqual(Path(dst_readlink(self.dst)), self.src)
        self.assertTrue(self.dst.exists())

    def test_replaces_regular_file_from_bootstrap(self):
        """历史 bootstrap 写入的最小清单（普通文件）→ 替换为主工作区软链"""
        self.dst.write_text(json.dumps({"req-board": str(self.wt)}), encoding="utf-8")
        d._link_repo_local_config(self.wt)
        self.assertTrue(self.dst.is_symlink())
        self.assertEqual(json.loads(self.dst.read_text(encoding="utf-8")), {"req-board": "/x"})

    def test_skips_when_source_missing(self):
        """源不存在：告警跳过，不建任何东西、不抛（正常不会发生：PROJECTS 就是从源读的）"""
        d.PROJECTS_FILE = self.tmp / "not-here.json"
        d._link_repo_local_config(self.wt)
        self.assertFalse(self.dst.exists())
        self.assertFalse(self.dst.is_symlink())

    def test_oserror_does_not_raise(self):
        """建链失败只告警：补本地配置不得阻断 worktree 创建（fail-open）"""
        with mock.patch.object(Path, "symlink_to", side_effect=OSError("read-only fs")):
            d._link_repo_local_config(self.wt)   # 不抛即通过


class EnsureDevWorktreesWiringTest(unittest.TestCase):
    """接线：ensure_dev_worktrees 对**每个仓库** worktree 各调用一次补链（不触真实 worktrees 根）"""

    def test_calls_link_per_repo(self):
        tmp = Path(tempfile.mkdtemp(prefix="wt-wiring-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repos = [tmp / "repoA", tmp / "repoB"]
        for r in repos:
            r.mkdir()
        req = {"id": 999999, "project": "fakeproj", "title": "t", "status": "open", "notes": ""}
        linked = []

        def fake_run(cmd, **kw):
            return mock.Mock(returncode=0, stdout="", stderr=b"")

        with mock.patch.object(d, "WORKTREES_ROOT", tmp / "worktrees"), \
             mock.patch.object(d, "PROJECTS", {"fakeproj": {"paths": [str(r) for r in repos]}}), \
             mock.patch.object(d, "dev_branch", lambda req: "f-test-999999"), \
             mock.patch.object(d, "_default_branch", lambda repo, cfg=None: ("main", "sha1")), \
             mock.patch.object(d, "subprocess", mock.Mock(run=fake_run)), \
             mock.patch.object(d, "state", {"worktrees": {}}), \
             mock.patch.object(d, "save_state", lambda: None), \
             mock.patch.object(d, "dev_workdir_for", lambda r: str(tmp)), \
             mock.patch.object(d, "_link_repo_local_config", lambda wt: linked.append(Path(wt))):
            d.ensure_dev_worktrees(req)

        self.assertEqual(len(linked), 2, f"应为每个仓库补链一次，实际 {linked}")
        self.assertEqual({p.name for p in linked}, {"repoA", "repoB"})
        for p in linked:
            self.assertEqual(p.parent.name, "req-999999")


if __name__ == "__main__":
    unittest.main()
