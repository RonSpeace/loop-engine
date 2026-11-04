"""合并被 main 工作区未提交改动挡住（非代码冲突）的识别与提示 测试。

背景：#44 合并时 main 工作区有未提交改动与 dev 分支改到同一批文件，git 预检拒绝合并
（"Your local changes ... would be overwritten by merge"），调度器却按真冲突拉起
merge_resolve（改 dev 分支，解决不了 main 脏现场），重试 4 次均「仍冲突」且无文件提示。

覆盖：
1. _merge_dirty_blockers 用真实临时 git 仓库区分：已跟踪改动 / 未跟踪文件挡住（无 MERGE_HEAD）
   vs 真冲突（有 MERGE_HEAD）；中文/空格文件名（-z 解析）；探测异常 fail-closed；
2. finalize_done 遇脏现场不拉起 merge_resolve，直接转人工通道并列出文件；
3. 重试失败提示文本：脏现场给文件与处理方式，真冲突沿用原「仍冲突（仓库）」口径。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 防测试误连生产 8899

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "append_summary", "post_dispatcher_event",
    "notify_req_card", "merge_conflict_write", "spawn_agent", "changed_repos",
    "_touches_dispatcher", "_annotate_extra_repos", "PROJECTS")}

BRANCH = "f-20260901-990"


def fake_req():
    return {"id": 990, "project": "dirty-test", "title": "脏工作区合并", "mode": "auto",
            "created_at": "2026-09-01 00:00:00"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


class _RepoCase(unittest.TestCase):
    """临时仓库：main 一个初始提交，dev 分支改 shared.txt 并新增 中文 名.txt"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="merge-dirty-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "shared.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "checkout", "-q", "-b", BRANCH)
        (self.repo / "shared.txt").write_text("dev\n", encoding="utf-8")
        (self.repo / "中文 名.txt").write_text("dev\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "dev")
        git(self.repo, "checkout", "-q", "main")
        d.PROJECTS = {**_D_REALS["PROJECTS"], "dirty-test": {"paths": [str(self.repo)]}}

    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)


class DirtyBlockersTest(_RepoCase):

    def test_tracked_change_blocks(self):
        """已跟踪文件未提交改动与 dev 分支重叠 → 合并失败、无 MERGE_HEAD、列出该文件（不含无关脏文件）"""
        (self.repo / "shared.txt").write_text("local wip\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("unrelated wip\n", encoding="utf-8")
        ok, failed = d.merge_dev_branches(fake_req())
        self.assertFalse(ok)
        self.assertEqual(d._merge_dirty_blockers(fake_req(), failed), {str(self.repo): ["shared.txt"]})
        self.assertEqual((self.repo / "shared.txt").read_text(encoding="utf-8"), "local wip\n")  # 现场未动

    def test_untracked_collision_blocks_with_cjk_space_name(self):
        """未跟踪文件与 dev 分支新增文件同名（中文+空格，-z 不转义）→ 识别为脏现场"""
        (self.repo / "中文 名.txt").write_text("local\n", encoding="utf-8")
        ok, failed = d.merge_dev_branches(fake_req())
        self.assertFalse(ok)
        self.assertEqual(d._merge_dirty_blockers(fake_req(), failed), {str(self.repo): ["中文 名.txt"]})

    def test_real_conflict_not_misclassified(self):
        """main 已提交的改动与 dev 冲突 → 有 MERGE_HEAD，按真冲突处理（返回空）"""
        (self.repo / "shared.txt").write_text("main side\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "main side")
        ok, failed = d.merge_dev_branches(fake_req())
        self.assertFalse(ok)
        try:
            self.assertEqual(d._merge_dirty_blockers(fake_req(), failed), {})
        finally:
            git(self.repo, "merge", "--abort")

    def test_probe_error_fail_closed(self):
        """探测异常（仓库路径不存在）→ 按真冲突处理，不抛异常"""
        self.assertEqual(d._merge_dirty_blockers(fake_req(), [str(self.tmp / "missing")]), {})

    def test_fail_text(self):
        """提示文本：脏现场列文件 + 处理方式（超过上限折叠计数）；真冲突沿用原口径"""
        many = {str(self.repo): [f"f{i}.py" for i in range(7)]}
        text = d._merge_fail_text([str(self.repo)], many)
        self.assertIn("未提交改动挡住", text)
        self.assertIn("repo：f0.py、f1.py、f2.py、f3.py、f4.py 等 7 个", text)
        self.assertIn("stash", text)
        self.assertEqual(d._merge_fail_text(["/x/req-board"], {}), "仍冲突（/x/req-board）")


class FinalizeDoneDirtyTest(_RepoCase):

    def setUp(self):
        super().setUp()
        d.state = {"chat_id": "oc_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {},
                   "held_procs": {}, "pending_restart": {}, "flow_review_rounds": {},
                   "merge_resolve": {}}
        d.state["worktrees"]["990"] = {"project": "dirty-test", "branch": BRANCH, "worktrees": []}
        self.sums, self.events, self.spawns, self.cards, self.writes = [], [], [], [], []
        d.save_state = lambda: None
        d.set_status = lambda *a, **k: None
        d.append_summary = lambda rid, line: self.sums.append(line)
        d.post_dispatcher_event = lambda rid, msg, **k: self.events.append(msg)
        d.notify_req_card = lambda event, req, **k: self.cards.append(event)
        d.merge_conflict_write = lambda rid, action=None: self.writes.append(rid)
        d.spawn_agent = lambda req, role=None, **k: self.spawns.append(role)
        d.changed_repos = lambda req, branch: {"repo"}
        d._touches_dispatcher = lambda req, branch: False
        d._annotate_extra_repos = lambda *a, **k: None

    def test_dirty_skips_merge_resolve(self):
        """脏现场 → 不拉起 merge_resolve，直接 pending_confirm(kind=merge) + 红卡 + 按钮，提示含文件名"""
        (self.repo / "shared.txt").write_text("local wip\n", encoding="utf-8")
        d.finalize_done(fake_req())
        self.assertEqual(self.spawns, [])
        self.assertEqual(d.state["merge_resolve"], {})
        pend = d.state["pending_confirm"]["990"]
        self.assertEqual(pend["kind"], "merge")
        self.assertIn("shared.txt", pend["question"])
        self.assertTrue(any("未提交改动挡住" in s and "shared.txt" in s for s in self.sums))
        self.assertTrue(any("未拉起 merge_resolve" in e for e in self.events))
        self.assertEqual(self.cards, ["merge_conflict"])
        self.assertEqual(self.writes, ["990"])

    def test_real_conflict_still_spawns_merge_resolve(self):
        """真冲突 → 仍走 #396 merge_resolve（行为不变）"""
        (self.repo / "shared.txt").write_text("main side\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "main side")
        try:
            d.finalize_done(fake_req())
            self.assertEqual(self.spawns, ["merge_resolve"])
            self.assertEqual(d.state["pending_confirm"], {})
        finally:
            git(self.repo, "merge", "--abort")


if __name__ == "__main__":
    unittest.main()
