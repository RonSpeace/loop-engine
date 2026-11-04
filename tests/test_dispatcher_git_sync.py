#!/usr/bin/env python3
"""dispatcher 项目级 Git 自动拉取/推送（git_sync v1）测试。

覆盖（任务契约）：
1. load_projects 归一：git/git_sync/worktree 解析；枚举值；旧布尔 fetch_before/push_after
   兼容（枚举优先）；非法值忽略+warn 不阻断；未配置 git_sync = None。
2. 未配置 git_sync = 零副作用：_sync_main_worktrees / _push_done_worktrees / ensure /
   finalize 不产生任何 git 远端操作（git_ops 零调用）。
3. _sync_main_worktrees：fetch-only 不动工作区；pull 快进；脏工作区/非主干跳过；
   分叉 overlap-check 空交集自动 merge、非空跳过转人工；ff-only / stop；fetch 失败只
   warn+事件不抛；repo_lock 串行。
4. ensure_dev_worktrees：仅新建 worktree 时同步一次；复用不重复同步；fetch 档不快进。
5. merge_dev_branches 主干守卫：git_sync 启用且主工作区不在主干 → 不合并、不落错分支、
   事件留痕；未配置 git_sync 保持旧行为（照旧合并）。
6. finalize_done：合并前 pull 再同步；on_done=auto 推主干（禁 force、非快进 fetch+ff 重试
   一次，失败不阻断 done）；manual 只统计待推送；off 不碰远端。
7. _set_done：主干推送失败 / 待手动推送三处提示（summary+事件+done 卡片）；仅推分支项目
   #288 f-分支原文案保持不变。

隔离：全部用临时 git 仓库（bare remote + clone）/临时 PROJECTS/临时 state；git_ops.repo_lock
替换为内存锁（不落 ~/.req-board/locks），post_dispatcher_event/notify/save_state 等全部
mock —— 绝不触真实 board.db、真实 projects.json、真实 ~/.req-board/worktrees。

运行：
    .venv/bin/python -m pytest -q tests/test_dispatcher_git_sync.py
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 防误连生产 8899

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_REAL_SET_DONE = d._set_done
_PROJECT = "git-sync-test"


# ---------------- 临时 git 仓库工具 ----------------

def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=check)


def init_repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "tester")
    git(path, "config", "commit.gpgsign", "false")
    return path


def commit_file(repo, name, content, message):
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def head_sha(repo, ref="HEAD"):
    return git(repo, "rev-parse", ref).stdout.strip()


def make_remote_pair(tmp, branch="main"):
    """bare remote + 本地 clone（含一个初始提交并已 push）。返回 (remote, local)。"""
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", branch, str(remote)], check=True)
    local = tmp / "repo"
    subprocess.run(["git", "clone", "-q", str(remote), str(local)], check=True)
    git(local, "config", "user.email", "test@example.com")
    git(local, "config", "user.name", "tester")
    git(local, "config", "commit.gpgsign", "false")
    commit_file(local, "base.txt", "base\n", "init")
    git(local, "push", "-q", "-u", "origin", branch)
    return remote, local


def advance_remote(tmp, remote, filename="remote.txt", content="remote\n", branch="main"):
    """模拟他人推进远端：克隆→改文件→push。返回远端新 sha。"""
    other = tmp / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "config", "user.email", "test@example.com")
    git(other, "config", "user.name", "tester")
    git(other, "config", "commit.gpgsign", "false")
    commit_file(other, filename, content, f"remote advance {filename}")
    git(other, "push", "-q", "origin", branch)
    return head_sha(other)


def sync_cfg(before_card="pull", on_done="off", diverge="overlap-check", timeout=30):
    return {"before_card": before_card, "on_done": on_done,
            "diverge": diverge, "timeout": timeout}


def req(req_id=9100, project=_PROJECT):
    return {"id": req_id, "project": project, "title": "git sync", "status": "doing",
            "created_at": "2026-09-01 00:00:00"}


def project_cfg(paths, sync=None, base_branch="main", company=False, git_cfg=None):
    cfg = {"paths": [str(p) for p in paths], "base_branch": base_branch, "company": company,
           "confirm": False, "restart": None, "auto_restart_after_merge": False,
           "worktree": "per-requirement",
           "git": git_cfg if git_cfg is not None else {"remote": "origin", "url": "",
                                                       "credential": "system", "ssh_key": ""}}
    if sync is not None:
        cfg["git_sync"] = sync
    return cfg


class _GitSyncCase(unittest.TestCase):
    """公共隔离：临时 state、内存 repo_lock、事件/摘要/通知记录器、真实临时 remote+local。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self._real_state = d.state
        d.state = {"chat_id": "oc_git_sync_test", "retries": {}, "pending_confirm": {},
                   "pending_route": {}, "pending_review": {}, "pending_restart": {},
                   "sessions": {}, "worktrees": {}, "req_repos": {}, "req_todos": {},
                   "merge_resolve": {}, "model_fallback": {}, "pending_merge_review": {},
                   "confirm_decisions": {}, "review_escalation": {}, "review_round_points": {},
                   "stale_wt_blocked": {}, "flow_review_rounds": {}, "held_procs": {}}
        self.events, self.summaries, self.cards, self.notifies = [], [], [], []
        self.locks = []

        @contextlib.contextmanager
        def _lock(repo, timeout=90):
            self.locks.append(str(repo))
            yield

        self._lock = _lock
        self._patchers = [
            mock.patch.object(d, "save_state", lambda: None),
            mock.patch.object(d, "set_status", lambda rid, st: None),
            mock.patch.object(d, "append_summary",
                              lambda rid, line: self.summaries.append(line)),
            mock.patch.object(d, "post_dispatcher_event",
                              lambda rid, content, kind="action", session_id=None:
                              self.events.append((kind, content))),
            mock.patch.object(d, "notify_req_card",
                              lambda event, req, **kw: self.cards.append((event, kw))),
            mock.patch.object(d, "notify", lambda text, *a, **k: self.notifies.append(text)),
            mock.patch.object(d, "merge_conflict_write", lambda rid, action=None: None),
            mock.patch.object(d, "spawn_agent", lambda *a, **k: None),
            mock.patch.object(d, "_kickstart_board_if_own", lambda *a, **k: None),
            mock.patch.object(d, "_annotate_extra_repos", lambda *a, **k: None),
            mock.patch.object(d.git_ops, "repo_lock", _lock),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.remote, self.repo = make_remote_pair(self.tmp)
        self.addCleanup(self.tmpdir.cleanup)

    def tearDown(self):
        d.state = self._real_state

    # -- helpers --
    def patch_projects(self, sync=None, **kw):
        cfg = project_cfg([self.repo], sync=sync,
                          git_cfg=kw.pop("git_cfg", None), **kw)
        patcher = mock.patch.object(d, "PROJECTS", {_PROJECT: cfg})
        patcher.start()
        self.addCleanup(patcher.stop)
        return cfg

    def start_sync(self, sync):
        return d._sync_main_worktrees(req(), sync)


# ==================== 1. load_projects 配置归一 ====================

class LoadProjectsGitSyncConfigTest(unittest.TestCase):
    def _load(self, raw):
        fake = mock.Mock()
        fake.read_text.return_value = json.dumps(raw)
        with mock.patch.object(d, "PROJECTS_FILE", fake):
            return d.load_projects()

    def test_unconfigured_is_none(self):
        out = self._load({"a": {"paths": ["/tmp/a"]}})
        self.assertIsNone(out["a"]["git_sync"])
        self.assertIsNone(out["a"]["git"])
        self.assertEqual(out["a"]["worktree"], "per-requirement")

    def test_enums_normalized(self):
        out = self._load({"a": {"paths": ["/tmp/a"],
                                "git_sync": {"before_card": "PULL", "on_done": "Auto",
                                             "diverge": "stop", "timeout": "45"}}})
        self.assertEqual(out["a"]["git_sync"], {"before_card": "pull", "on_done": "auto",
                                                "diverge": "stop", "timeout": 45})

    def test_legacy_booleans_mapped(self):
        """旧布尔 fetch_before=true→pull、push_after=true→auto；缺省字段落默认值。"""
        out = self._load({"a": {"paths": ["/tmp/a"], "fetch_before": True, "push_after": True}})
        self.assertEqual(out["a"]["git_sync"], {"before_card": "pull", "on_done": "auto",
                                                "diverge": "overlap-check", "timeout": 120})

    def test_legacy_false_booleans_do_not_enable(self):
        out = self._load({"a": {"paths": ["/tmp/a"], "fetch_before": False}})
        self.assertIsNone(out["a"]["git_sync"])

    def test_enum_takes_precedence_over_legacy(self):
        out = self._load({"a": {"paths": ["/tmp/a"], "fetch_before": True,
                                "git_sync": {"before_card": "off"}}})
        self.assertEqual(out["a"]["git_sync"]["before_card"], "off")

    def test_none_alias_maps_to_off(self):
        """前端友好别名 none → off（不报错、不阻断）。"""
        out = self._load({"a": {"paths": ["/tmp/a"],
                                "git_sync": {"before_card": "none", "on_done": "none"}}})
        self.assertEqual(out["a"]["git_sync"]["before_card"], "off")
        self.assertEqual(out["a"]["git_sync"]["on_done"], "off")

    def test_invalid_values_warn_and_ignored(self):
        raw = {"a": {"paths": ["/tmp/a"],
                     "worktree": "per-repo",
                     "git": {"remote": "", "credential": "oauth", "ssh_key": 3},
                     "git_sync": {"before_card": "maybe", "on_done": 1,
                                  "diverge": "wild", "timeout": -5}},
               "b": {"paths": ["/tmp/b"], "git_sync": "yes"},
               "c": {"paths": ["/tmp/c"], "git": ["bad"]}}
        with self.assertLogs("dispatcher", level="WARNING") as logs:
            out = self._load(raw)
        text = "\n".join(logs.output)
        for key in ("before_card", "on_done", "diverge", "timeout", "git_sync",
                    "worktree", "remote", "credential", "ssh_key", "git"):
            self.assertIn(key, text)
        self.assertEqual(out["a"]["git_sync"], {"before_card": "off", "on_done": "off",
                                                "diverge": "overlap-check", "timeout": 120})
        self.assertEqual(out["a"]["worktree"], "per-requirement")
        self.assertEqual(out["a"]["git"]["remote"], "origin")
        self.assertEqual(out["a"]["git"]["credential"], "system")
        self.assertEqual(out["a"]["git"]["ssh_key"], "")
        self.assertIsNone(out["b"]["git_sync"])
        self.assertIsNone(out["c"]["git"])

    def test_git_fields_normalized(self):
        out = self._load({"a": {"paths": ["/tmp/a"],
                                "git": {"remote": " upstream ", "url": " https://x/y.git ",
                                        "credential": "SSH", "ssh_key": " ~/.ssh/id "}}})
        self.assertEqual(out["a"]["git"], {"remote": "upstream",
                                           "url": "https://x/y.git",
                                           "credential": "ssh",
                                           "ssh_key": "~/.ssh/id"})

    def test_empty_git_sync_dict_uses_defaults(self):
        out = self._load({"a": {"paths": ["/tmp/a"], "git_sync": {}}})
        self.assertEqual(out["a"]["git_sync"], {"before_card": "off", "on_done": "off",
                                                "diverge": "overlap-check", "timeout": 120})


# ==================== 2. 未配置 git_sync = 零副作用 ====================

class UnconfiguredZeroSideEffectTest(_GitSyncCase):

    def test_sync_and_push_helpers_noop(self):
        with mock.patch.object(d.git_ops, "run_git") as run:
            run.side_effect = AssertionError("未配置 git_sync 不得执行任何 git 远端命令")
            self.assertEqual(d._sync_main_worktrees(req(), None)["mode"], "off")
            self.assertEqual(d._sync_main_worktrees(req(), None)["fetched"], [])
            self.assertEqual(d._push_done_worktrees(req(), None), (set(), {}))
            self.assertEqual(d._push_done_worktrees(req(), {"before_card": "off",
                                                            "on_done": "off",
                                                            "diverge": "overlap-check",
                                                            "timeout": 30}), (set(), {}))
        self.assertEqual(run.call_count, 0)

    def test_ensure_worktrees_without_sync_creates_worktree(self):
        """未配置 git_sync：ensure 照常建 worktree，且不调 git_ops（零同步）。"""
        self.patch_projects(sync=None)
        wt = self.tmp / "worktrees" / "repo"
        with mock.patch.object(d, "dev_worktree_path", lambda r, p: wt), \
             mock.patch.object(d, "dev_workdir_for", lambda r: wt.parent), \
             mock.patch.object(d.git_ops, "run_git") as run:
            run.side_effect = AssertionError("未配置 git_sync 不得同步")
            d.ensure_dev_worktrees(req())
        self.assertTrue((wt / ".git").exists())
        self.assertEqual(run.call_count, 0)

    def test_finalize_without_sync_does_not_push(self):
        self.patch_projects(sync=None)
        d.state["worktrees"]["9100"] = {"project": _PROJECT, "branch": "f-20260901-9100",
                                        "base": {}, "worktrees": []}
        calls = {}

        def _set_done(r, **kw):
            calls.update(kw)
            calls["_req"] = r

        with mock.patch.object(d, "merge_dev_branches", lambda r: (True, [])), \
             mock.patch.object(d, "changed_repos", lambda r, b: set()), \
             mock.patch.object(d, "_touches_dispatcher", lambda r, b: False), \
             mock.patch.object(d, "_maybe_auto_restart_after_merge",
                               lambda *a, **k: (None, False, "")), \
             mock.patch.object(d, "cleanup_dev_worktrees", lambda *a, **k: None), \
             mock.patch.object(d, "_set_done", _set_done), \
             mock.patch.object(d.git_ops, "run_git") as run:
            run.side_effect = AssertionError("未配置 git_sync 不得 push/fetch")
            d.finalize_done(req())
        self.assertNotIn("main_push_failed", calls)
        self.assertNotIn("main_pending_push", calls)
        self.assertEqual(run.call_count, 0)


# ==================== 3. _sync_main_worktrees 行为 ====================

class SyncMainWorktreesTest(_GitSyncCase):

    def test_fetch_only_updates_refs_not_worktree(self):
        self.patch_projects(sync=sync_cfg(before_card="fetch"))
        old = head_sha(self.repo)
        new = advance_remote(self.tmp, self.remote)
        result = self.start_sync(sync_cfg(before_card="fetch"))
        self.assertEqual(head_sha(self.repo), old)          # 工作区未动
        self.assertEqual(head_sha(self.repo, "origin/main"), new)  # 远端引用已更新
        self.assertIn("repo", result["fetched"])
        self.assertEqual(result["fast_forwarded"], [])
        self.assertTrue(self.locks)                        # repo_lock 串行

    def test_pull_fast_forward(self):
        self.patch_projects(sync=sync_cfg())
        new = advance_remote(self.tmp, self.remote)
        result = self.start_sync(sync_cfg())
        self.assertEqual(head_sha(self.repo), new)
        self.assertIn("repo", result["fast_forwarded"])
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "")

    def test_fast_forward_skipped_when_dirty(self):
        self.patch_projects(sync=sync_cfg())
        old = head_sha(self.repo)
        advance_remote(self.tmp, self.remote)
        (self.repo / "base.txt").write_text("local wip\n", encoding="utf-8")
        result = self.start_sync(sync_cfg())
        self.assertIn("repo", result["skipped"])
        self.assertEqual(head_sha(self.repo), old)
        self.assertNotIn("repo", result["fast_forwarded"])
        self.assertEqual(self.events, [])

    def test_fast_forward_skipped_when_on_other_branch(self):
        self.patch_projects(sync=sync_cfg())
        git(self.repo, "checkout", "-q", "-b", "feature-x")
        old_main = head_sha(self.repo, "main")
        advance_remote(self.tmp, self.remote)
        result = self.start_sync(sync_cfg())
        self.assertIn("repo", result["skipped"])
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                         "feature-x")               # 不切分支
        self.assertEqual(head_sha(self.repo, "main"), old_main)

    def test_diverge_overlap_empty_auto_merge(self):
        self.patch_projects(sync=sync_cfg())
        commit_file(self.repo, "local.txt", "local\n", "local advance")
        advance_remote(self.tmp, self.remote, "remote.txt")
        result = self.start_sync(sync_cfg(diverge="overlap-check"))
        self.assertIn("repo", result["merged"])
        self.assertTrue((self.repo / "local.txt").exists())
        self.assertTrue((self.repo / "remote.txt").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "")
        self.assertTrue(any("无重叠" in c for _k, c in self.events))

    def test_diverge_overlap_nonempty_skips(self):
        self.patch_projects(sync=sync_cfg())
        commit_file(self.repo, "shared.txt", "local side\n", "local edits shared")
        local_tip = head_sha(self.repo)
        advance_remote(self.tmp, self.remote, "shared.txt", "remote side\n")
        result = self.start_sync(sync_cfg(diverge="overlap-check"))
        self.assertIn("repo", result["diverged"])
        self.assertEqual(head_sha(self.repo), local_tip)    # 未合并
        self.assertNotEqual(git(self.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD",
                               check=False).returncode, 0)  # 无半自动冲突现场
        self.assertTrue(any("重叠" in c and "转人工" in c for _k, c in self.events))

    def test_diverge_ff_only_skips(self):
        self.patch_projects(sync=sync_cfg())
        commit_file(self.repo, "local.txt", "local\n", "local advance")
        local_tip = head_sha(self.repo)
        advance_remote(self.tmp, self.remote)
        result = self.start_sync(sync_cfg(diverge="ff-only"))
        self.assertIn("repo", result["diverged"])
        self.assertEqual(head_sha(self.repo), local_tip)

    def test_diverge_stop_events(self):
        self.patch_projects(sync=sync_cfg())
        commit_file(self.repo, "local.txt", "local\n", "local advance")
        advance_remote(self.tmp, self.remote)
        result = self.start_sync(sync_cfg(diverge="stop"))
        self.assertIn("repo", result["diverged"])
        self.assertTrue(any("stop" in c for _k, c in self.events))

    def test_fetch_failure_warns_and_events_without_raising(self):
        self.patch_projects(sync=sync_cfg())
        git(self.repo, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        old = head_sha(self.repo)
        result = self.start_sync(sync_cfg())
        self.assertIn("repo", result["failed"])
        self.assertEqual(head_sha(self.repo), old)
        self.assertTrue(any("fetch" in c for _k, c in self.events))
        self.assertTrue(any(k == "merge" for k, _c in self.events))

    def test_local_only_repo_skipped_without_error(self):
        """无任何远端的本地仓库：git_sync 配了也跳过（本地纯项目属正常，不报失败）。"""
        local = init_repo(self.tmp / "standalone")
        commit_file(local, "a.txt", "a\n", "init")
        cfg = project_cfg([local], sync=sync_cfg())
        with mock.patch.object(d, "PROJECTS", {_PROJECT: cfg}):
            result = self.start_sync(sync_cfg())
        self.assertIn("standalone", result["skipped"])
        self.assertEqual(result["failed"], [])


# ==================== 4. ensure_dev_worktrees 同步时机 ====================

class EnsureSyncOnceTest(_GitSyncCase):

    def _ensure(self, cfg_sync, wt):
        self.patch_projects(sync=cfg_sync)
        with mock.patch.object(d, "dev_worktree_path", lambda r, p: wt), \
             mock.patch.object(d, "dev_workdir_for", lambda r: wt.parent):
            return d.ensure_dev_worktrees(req())

    def test_sync_once_when_creating_worktree(self):
        sync = sync_cfg()
        wt = self.tmp / "worktrees" / "repo"
        advance_remote(self.tmp, self.remote)
        real_sync = d._sync_main_worktrees
        calls = []

        def spy(r, s, mode=None):
            calls.append(mode)
            return real_sync(r, s, mode)

        self.patch_projects(sync=sync)
        with mock.patch.object(d, "dev_worktree_path", lambda r, p: wt), \
             mock.patch.object(d, "dev_workdir_for", lambda r: wt.parent), \
             mock.patch.object(d, "_sync_main_worktrees", spy):
            d.ensure_dev_worktrees(req())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], None)  # 不传 mode = 用 before_card=pull
        self.assertEqual(head_sha(wt), head_sha(self.repo, "main"))
        self.assertEqual(head_sha(wt), head_sha(self.repo, "origin/main"))  # 先同步再建 worktree

    def test_no_sync_when_worktree_already_exists(self):
        sync = sync_cfg()
        wt = self.tmp / "worktrees" / "repo"
        self._ensure(sync, wt)
        advance_remote(self.tmp, self.remote)
        with mock.patch.object(d.git_ops, "run_git") as run:
            run.side_effect = AssertionError("复用已有 worktree 不得重复同步")
            with mock.patch.object(d, "dev_worktree_path", lambda r, p: wt), \
                 mock.patch.object(d, "dev_workdir_for", lambda r: wt.parent):
                d.ensure_dev_worktrees(req())
        self.assertEqual(run.call_count, 0)

    def test_fetch_mode_worktree_base_stays_local_main(self):
        sync = sync_cfg(before_card="fetch")
        wt = self.tmp / "worktrees" / "repo"
        old_local = head_sha(self.repo)
        advance_remote(self.tmp, self.remote)
        self._ensure(sync, wt)
        self.assertEqual(head_sha(wt), old_local)  # fetch 档不快进，worktree 基线仍本地主干


# ==================== 5. merge_dev_branches 主干守卫 ====================

class MergeGuardTest(_GitSyncCase):

    def _repo_with_dev_branch(self):
        """main 初始提交 → dev 分支改文件 → 主工作区切到 other 分支（非主干）。"""
        branch = "f-20260901-9100"
        git(self.repo, "checkout", "-q", "-b", branch)
        commit_file(self.repo, "dev.txt", "dev\n", "dev work")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "checkout", "-q", "-b", "other")
        return branch

    def test_guard_blocks_merge_on_wrong_branch(self):
        branch = self._repo_with_dev_branch()
        cfg = project_cfg([self.repo], sync=sync_cfg(before_card="off"))
        with mock.patch.object(d, "PROJECTS", {_PROJECT: cfg}):
            ok, failed = d.merge_dev_branches(req())
        self.assertFalse(ok)
        self.assertEqual(failed, [str(self.repo)])
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                         "other")                       # 未被切走/未落错分支
        self.assertFalse((self.repo / "dev.txt").exists())  # merge 未执行
        self.assertTrue(any("主干" in c or "自动合并跳过" in c for _k, c in self.events))

    def test_guard_absent_without_git_sync(self):
        """未配置 git_sync = 旧行为：照旧在当前分支合并（兼容性红线）。"""
        branch = self._repo_with_dev_branch()
        cfg = project_cfg([self.repo], sync=None)
        cfg.pop("base_branch", None)
        with mock.patch.object(d, "PROJECTS", {_PROJECT: cfg}):
            ok, failed = d.merge_dev_branches(req())
        self.assertTrue(ok, failed)
        self.assertEqual(failed, [])
        self.assertTrue((self.repo / "dev.txt").exists())
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                         "other")

    def test_guard_allows_merge_on_base(self):
        branch = "f-20260901-9100"
        git(self.repo, "checkout", "-q", "-b", branch)
        commit_file(self.repo, "dev.txt", "dev\n", "dev work")
        git(self.repo, "checkout", "-q", "main")
        cfg = project_cfg([self.repo], sync=sync_cfg(before_card="off"))
        with mock.patch.object(d, "PROJECTS", {_PROJECT: cfg}):
            ok, failed = d.merge_dev_branches(req())
        self.assertTrue(ok, failed)
        self.assertTrue((self.repo / "dev.txt").exists())

    def test_preflight_fail_open_on_unavailable_repo(self):
        """守卫探测异常（仓库路径不存在）→ fail-open 返回 None，不抛、不阻断合并主流程。"""
        cfg = project_cfg([self.tmp / "missing"], sync=sync_cfg())
        with mock.patch.object(d, "PROJECTS", {_PROJECT: cfg}):
            self.assertIsNone(d._merge_preflight(req(), str(self.tmp / "missing")))

    def test_finalize_guard_text_not_dirty(self):
        self._repo_with_dev_branch()
        cfg = project_cfg([self.repo], sync=sync_cfg(before_card="off"))
        cfg["restart"] = None
        d.state["worktrees"]["9100"] = {"project": _PROJECT, "branch": "f-20260901-9100",
                                        "base": {}, "worktrees": []}
        with mock.patch.object(d, "PROJECTS", {_PROJECT: cfg}), \
             mock.patch.object(d, "changed_repos", lambda r, b: {"repo"}), \
             mock.patch.object(d, "_touches_dispatcher", lambda r, b: False):
            d.finalize_done(req())
        pend = d.state["pending_confirm"]["9100"]
        self.assertEqual(pend["kind"], "merge")
        self.assertIn("主干", pend["question"])
        self.assertNotIn("未提交改动", pend["question"])
        self.assertNotIn("工作区脏", pend["question"])
        self.assertTrue(any("主干守卫" in s for s in self.summaries))
        self.assertTrue(any("主干守卫" in c for _k, c in self.events))
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                         "other")


# ==================== 6. finalize_done 推送（on_done） ====================

class PushDoneTest(_GitSyncCase):

    def _finalize(self, sync):
        self.patch_projects(sync=sync)
        d.state["worktrees"]["9100"] = {"project": _PROJECT, "branch": "f-20260901-9100",
                                        "base": {}, "worktrees": []}
        calls = []

        def _set_done(r, **kw):
            calls.append(kw)

        with mock.patch.object(d, "merge_dev_branches", lambda r: (True, [])), \
             mock.patch.object(d, "changed_repos", lambda r, b: set()), \
             mock.patch.object(d, "_touches_dispatcher", lambda r, b: False), \
             mock.patch.object(d, "_maybe_auto_restart_after_merge",
                               lambda *a, **k: (None, False, "")), \
             mock.patch.object(d, "cleanup_dev_worktrees", lambda *a, **k: None), \
             mock.patch.object(d, "_set_done", _set_done):
            d.finalize_done(req())
        return calls

    def test_auto_push_success(self):
        self.patch_projects(sync=sync_cfg(on_done="auto"))
        commit_file(self.repo, "merged.txt", "merged\n", "merge result")
        local_tip = head_sha(self.repo)
        failed, pending = d._push_done_worktrees(req(), sync_cfg(on_done="auto"))
        self.assertEqual(failed, set())
        self.assertEqual(pending, {})
        self.assertEqual(head_sha(self.repo, "origin/main"), local_tip)  # 远端已更新
        self.assertTrue(self.locks)  # repo_lock 串行
        self.assertTrue(any("已自动推送" in c for _k, c in self.events))

    def test_auto_push_failure_not_blocking_done(self):
        self.patch_projects(sync=sync_cfg(on_done="auto"))
        commit_file(self.repo, "base.txt", "local side\n", "local edits base")
        advance_remote(self.tmp, self.remote, "base.txt", "remote side\n")  # 非快进且 ff 失败
        failed, pending = d._push_done_worktrees(req(), sync_cfg(on_done="auto"))
        self.assertEqual(failed, {"repo"})
        self.assertEqual(pending, {})
        self.assertTrue(any("push" in c or "推送" in c for _k, c in self.events))
        calls = self._finalize(sync_cfg(on_done="auto"))
        self.assertEqual(len(calls), 1)                    # done 仍走到出口
        self.assertEqual(calls[0].get("main_push_failed"), {"repo"})  # 三处提醒数据已透传

    def test_manual_pending_push_recorded(self):
        self.patch_projects(sync=sync_cfg(on_done="manual"))
        before_remote = head_sha(self.repo, "origin/main")
        commit_file(self.repo, "m1.txt", "1\n", "m1")
        commit_file(self.repo, "m2.txt", "2\n", "m2")
        failed, pending = d._push_done_worktrees(req(), sync_cfg(on_done="manual"))
        self.assertEqual(failed, set())
        self.assertEqual(pending, {"repo": 2})
        self.assertEqual(head_sha(self.repo, "origin/main"), before_remote)  # 未推送
        self.assertTrue(any("待手动推送" in c for _k, c in self.events))
        calls = self._finalize(sync_cfg(on_done="manual"))
        self.assertEqual(calls[0].get("main_pending_push"), {"repo": 2})

    def test_off_does_not_touch_remote(self):
        self.patch_projects(sync=sync_cfg(on_done="off"))
        with mock.patch.object(d.git_ops, "run_git") as run:
            run.side_effect = AssertionError("on_done=off 不得碰远端")
            self.assertEqual(d._push_done_worktrees(req(), sync_cfg(on_done="off")),
                             (set(), {}))
        self.assertEqual(run.call_count, 0)

    def test_auto_never_uses_force(self):
        """只推主干、禁 force：push 命令必须精确为 [push, <remote>, <base>]。"""
        self.patch_projects(sync=sync_cfg(on_done="auto"))
        commit_file(self.repo, "merged.txt", "merged\n", "merge result")
        calls = []
        real_run = d.git_ops.run_git

        def spy(repo, args, **kw):
            calls.append(list(args))
            return real_run(repo, args, **kw)

        with mock.patch.object(d.git_ops, "run_git", side_effect=spy):
            d._push_done_worktrees(req(), sync_cfg(on_done="auto"))
        pushes = [c for c in calls if c and c[0] == "push"]
        self.assertEqual(pushes, [["push", "origin", "main"]])
        self.assertFalse(any("--force" in c or "-f" in c for c in pushes))


class FinalizeSyncStageTest(_GitSyncCase):
    """finalize_done：pull 档合并前再同步一次；fetch/off 档不重复快进判定。"""

    def test_pull_syncs_before_merge(self):
        self.patch_projects(sync=sync_cfg(before_card="pull"))
        d.state["worktrees"]["9100"] = {"project": _PROJECT, "branch": "f-20260901-9100",
                                        "base": {}, "worktrees": []}
        order = []

        def fake_sync(r, s, mode=None):
            order.append("sync")
            return {}

        def fake_merge(r):
            order.append("merge")
            return True, []

        with mock.patch.object(d, "_sync_main_worktrees", fake_sync), \
             mock.patch.object(d, "merge_dev_branches", fake_merge), \
             mock.patch.object(d, "changed_repos", lambda r, b: set()), \
             mock.patch.object(d, "_touches_dispatcher", lambda r, b: False), \
             mock.patch.object(d, "_maybe_auto_restart_after_merge",
                               lambda *a, **k: (None, False, "")), \
             mock.patch.object(d, "cleanup_dev_worktrees", lambda *a, **k: None), \
             mock.patch.object(d, "_set_done", lambda r, **k: order.append("done")):
            d.finalize_done(req())
        self.assertEqual(order, ["sync", "merge", "done"])

    def test_off_does_not_sync_before_merge(self):
        self.patch_projects(sync=sync_cfg(before_card="off"))
        d.state["worktrees"]["9100"] = {"project": _PROJECT, "branch": "f-20260901-9100",
                                        "base": {}, "worktrees": []}
        with mock.patch.object(d, "_sync_main_worktrees") as sync_mock, \
             mock.patch.object(d, "merge_dev_branches", lambda r: (True, [])), \
             mock.patch.object(d, "changed_repos", lambda r, b: set()), \
             mock.patch.object(d, "_touches_dispatcher", lambda r, b: False), \
             mock.patch.object(d, "_maybe_auto_restart_after_merge",
                               lambda *a, **k: (None, False, "")), \
             mock.patch.object(d, "cleanup_dev_worktrees", lambda *a, **k: None), \
             mock.patch.object(d, "_set_done", lambda r, **k: None):
            d.finalize_done(req())
        sync_mock.assert_not_called()


# ==================== 7. _set_done 文案三处提示 ====================

class SetDoneGitSyncHintTest(unittest.TestCase):
    """_set_done(main_push_failed / main_pending_push)：summary + 时间线 + done 卡片三处提示；"""

    def setUp(self):
        self._real_state = d.state
        d.state = {"chat_id": "oc_set_done_test", "retries": {}, "pending_confirm": {},
                   "pending_route": {}, "pending_review": {}, "pending_restart": {},
                   "sessions": {}, "worktrees": {}, "req_repos": {}, "req_todos": {},
                   "merge_resolve": {}, "model_fallback": {}, "pending_merge_review": {},
                   "confirm_decisions": {}, "review_escalation": {}, "review_round_points": {},
                   "stale_wt_blocked": {}}
        self.summaries, self.events, self.card_notifies = [], [], []
        patchers = [
            mock.patch.object(d, "save_state", lambda: None),
            mock.patch.object(d, "set_status", lambda rid, st: None),
            mock.patch.object(d, "append_summary",
                              lambda rid, line: self.summaries.append(line)),
            mock.patch.object(d, "post_dispatcher_event",
                              lambda rid, content, kind="action": self.events.append((kind, content))),
            mock.patch.object(d, "notify_req_card",
                              lambda event, req, **kw: self.card_notifies.append(kw)),
            mock.patch.object(d, "_kickstart_board_if_own", lambda *a, **k: None),
            mock.patch.object(d, "_purge_ghost_confirm", lambda *a, **k: False),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, d, "state", self._real_state)
        self.addCleanup(setattr, d, "_set_done", _REAL_SET_DONE)

    def _run(self, req=None, projects=None, **kwargs):
        req = req or {"id": 99, "project": "p", "title": "T"}
        cfg = projects or {"p": {"paths": ["/tmp/p"], "company": False}}
        with mock.patch.object(d, "PROJECTS", cfg):
            _REAL_SET_DONE(req, **kwargs)

    def _reminder(self):
        self.assertEqual(len(self.card_notifies), 1)
        return self.card_notifies[0]["reminder"]

    def test_main_push_failed_three_places(self):
        self._run(main_push_failed={"repo-a", "repo-b"})
        self.assertTrue(any("主干自动推送失败" in s and "repo-a" in s
                            for s in self.summaries))
        self.assertTrue(any(k == "merge" and "主干" in c for k, c in self.events))
        reminder = self._reminder()
        self.assertIn("主干自动推送失败", reminder)
        self.assertIn("repo-b", reminder)
        self.assertIn("手动推送主干", reminder)

    def test_main_pending_push_three_places(self):
        self._run(main_pending_push={"repo-a": 2, "repo-b": None})
        self.assertTrue(any("待手动推送" in s and "领先 2 个提交" in s
                            for s in self.summaries))
        self.assertTrue(any(k == "merge" and "待手动推送" in c for k, c in self.events))
        reminder = self._reminder()
        self.assertIn("repo-a 领先 2 个提交", reminder)
        self.assertIn("无法统计", reminder)
        self.assertIn("git push", reminder)

    def test_company_f_branch_wording_preserved(self):
        """#288 仅推分支项目 f-分支文案（回归红线）。"""
        self._run(projects={"p": {"paths": ["/tmp/p"], "company": True}},
                  push_failed={"repo-a"})
        reminder = self._reminder()
        self.assertIn("仅推分支项目 f-分支自动 push 失败", reminder)
        self.assertIn("git push origin", reminder)
        self.assertTrue(any("仅推分支项目 f-分支" in c for _k, c in self.events))

    def test_empty_hints_no_reminder(self):
        self._run(main_pending_push={}, main_push_failed=set())
        self.assertEqual(self._reminder(), "")
        self.assertEqual(self.summaries, [])
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
