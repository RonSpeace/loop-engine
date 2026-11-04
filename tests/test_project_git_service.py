#!/usr/bin/env python3
"""#54 ProjectGit 服务层测试：真实临时 git 仓库 + bare remote。

覆盖：项目/仓库候选与隔离、状态、拉取（up_to_date/ahead/落后快进/分叉合并/冲突/abort/
resolve/中间态/脏工作区拒绝）、提交两阶段素材（预览/指纹/安全门/include_untracked/
merge 收尾）、预算与取消。

运行：
    .venv/bin/python -m unittest tests.test_project_git_service -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402
from board_modules.services.project_git import ProjectGit  # noqa: E402

SECRET_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
SECRET_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"


def git(repo, *args, check=True):
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {r.stderr}")
    return r


def init_repo(path, name="file.txt", body="base\n"):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "Tester")
    (path / name).write_text(body, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    git(path, "branch", "-M", "main")
    return path


class GitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req54-git-"))
        self.projects_file = self.tmp / "projects.json"
        self._old = (b.PROJECTS_FILE, b.PROJECT_PATHS)
        b.PROJECTS_FILE = self.projects_file
        b.PROJECT_PATHS = {}
        self.git = ProjectGit()

    def tearDown(self):
        b.PROJECTS_FILE, b.PROJECT_PATHS = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_project(self, name, paths, **cfg):
        data = {}
        if self.projects_file.exists():
            data = json.loads(self.projects_file.read_text(encoding="utf-8"))
        entry = {"paths": [str(p) for p in paths]}
        entry.update(cfg)
        data[name] = entry
        self.projects_file.write_text(json.dumps(data), encoding="utf-8")
        b.PROJECT_PATHS = {k: [str(p) for p in (v.get("paths") or [])]
                           for k, v in data.items()}
        return entry

    def make_pair(self, name="repo"):
        remote = self.tmp / f"{name}-remote.git"
        remote.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        work = init_repo(self.tmp / name)
        git(work, "remote", "add", "origin", str(remote))
        git(work, "push", "-q", "-u", "origin", "main")
        other = self.tmp / f"{name}-other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        git(other, "config", "user.email", "t2@example.com")
        git(other, "config", "user.name", "Other")
        return work, other, remote

    def advance_remote(self, other, filename="file.txt", content="remote\n", msg="remote change"):
        (other / filename).write_text(content, encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", msg)
        git(other, "push", "-q", "origin", "main")


class CandidateTest(GitBase):
    def test_01_missing_project_lists_candidates(self):
        self.add_project("alpha", [self.tmp / "a"])
        with self.assertRaises(ValueError) as ctx:
            self.git.status(None)
        self.assertIn("候选项目", str(ctx.exception))
        self.assertIn("alpha", str(ctx.exception))

    def test_02_unknown_project_lists_candidates(self):
        self.add_project("alpha", [self.tmp / "a"])
        with self.assertRaises(ValueError) as ctx:
            self.git.status("nope")
        self.assertIn("未知项目", str(ctx.exception))
        self.assertIn("alpha", str(ctx.exception))

    def test_03_multi_repo_requires_repo_and_lists(self):
        self.add_project("multi", [self.tmp / "r1", self.tmp / "r2"])
        with self.assertRaises(ValueError) as ctx:
            self.git.resolve_repo("multi")
        msg = str(ctx.exception)
        self.assertIn("候选仓库", msg)
        self.assertIn("r1", msg)
        self.assertIn("r2", msg)

    def test_04_same_basename_projects_only_match_key(self):
        one = init_repo(self.tmp / "x" / "Main Repo")
        two = init_repo(self.tmp / "y" / "Main Repo")
        self.add_project("one", [one])
        self.add_project("two", [two])
        self.assertIn("one", str(self.git.project_candidates()))
        with self.assertRaises(ValueError):
            self.git.resolve_repo("one", str(two))
        self.assertEqual(self.git.resolve_repo("one", "Main Repo").resolve(), one.resolve())

    def test_05_repo_unique_name_and_absolute(self):
        work = init_repo(self.tmp / "solo")
        self.add_project("solo", [work])
        self.assertEqual(self.git.resolve_repo("solo").resolve(), work.resolve())
        self.assertEqual(self.git.resolve_repo("solo", "solo").resolve(), work.resolve())
        with self.assertRaises(ValueError):
            self.git.resolve_repo("solo", str(self.tmp))

    def test_06_non_git_path_rejected(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.add_project("plain", [plain])
        with self.assertRaises(ValueError) as ctx:
            self.git.resolve_repo("plain")
        self.assertIn("不是 git 仓库根", str(ctx.exception))


class StatusTest(GitBase):
    def test_07_status_fields_and_no_side_effect(self):
        work = init_repo(self.tmp / "status")
        self.add_project("status", [work], base_branch="main")
        before = git(work, "rev-parse", "HEAD").stdout.strip()
        (work / "file.txt").write_text("changed\n", encoding="utf-8")
        (work / "new.txt").write_text("new\n", encoding="utf-8")
        out = self.git.status("status")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["branch"], "main")
        self.assertTrue(out["head"]["short"])
        self.assertEqual(out["dirty"]["changed"], 1)
        self.assertEqual(out["dirty"]["untracked"], 1)
        self.assertEqual(out["changed_files"][0]["path"], "file.txt")
        self.assertIn("new.txt", out["untracked_files"])
        self.assertFalse(out["unmerged"])
        self.assertEqual(git(work, "rev-parse", "HEAD").stdout.strip(), before)

    def test_08_detached_head_status_error(self):
        work = init_repo(self.tmp / "detached")
        self.add_project("detached", [work])
        sha = git(work, "rev-parse", "HEAD").stdout.strip()
        git(work, "checkout", "-q", sha)
        out = self.git.status("detached")
        self.assertEqual(out["status"], "error")
        self.assertIsNone(out["branch"])
        self.assertIn("detached HEAD", out["error"])

    def test_49_base_branch_display_only_no_switch(self):
        work = init_repo(self.tmp / "base")
        self.add_project("base", [work], base_branch="feature/f-init")
        out = self.git.status("base")
        self.assertEqual(out["base_branch"], "feature/f-init")
        self.assertIn("不一致", out["base_branch_hint"])
        self.assertEqual(out["branch"], "main")

    def test_50_explicit_branch_mismatch_rejected(self):
        work = init_repo(self.tmp / "branch")
        self.add_project("branch", [work])
        with self.assertRaises(ValueError) as ctx:
            self.git.pull("branch", branch="other")
        self.assertIn("不一致", str(ctx.exception))
        self.assertEqual(git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")


class PullTest(GitBase):
    def test_09_up_to_date(self):
        work, _other, _remote = self.make_pair()
        self.add_project("p", [work])
        out = self.git.pull("p")
        self.assertEqual(out["status"], "up_to_date")
        self.assertEqual(out["branch"], "main")

    def test_10_ahead(self):
        work, _other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "local.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "local")
        out = self.git.pull("p")
        self.assertEqual(out["status"], "ahead")

    def test_11_behind_fast_forward_reports_before_after(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        before = git(work, "rev-parse", "HEAD").stdout.strip()
        self.advance_remote(other, content="remote\n")
        out = self.git.pull("p")
        self.assertEqual(out["status"], "pulled")
        self.assertEqual(out["before"]["sha"], before)
        self.assertNotEqual(out["after"]["sha"], before)
        self.assertEqual((work / "file.txt").read_text(), "remote\n")

    def test_12_diverged_merge_commit(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "local.txt").write_text("l\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "local")
        self.advance_remote(other, filename="remote.txt", content="r\n")
        out = self.git.pull("p")
        self.assertEqual(out["status"], "pulled")
        parents = git(work, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
        self.assertEqual(len(parents), 3)      # merge commit 两个父提交

    def test_13_conflict_keeps_scene_and_lists_files(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "file.txt").write_text("ours\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "ours")
        self.advance_remote(other, content="theirs\n")
        local_sha = git(work, "rev-parse", "HEAD").stdout.strip()
        out = self.git.pull("p")
        self.assertEqual(out["status"], "conflict")
        self.assertTrue(out["in_progress"])
        self.assertEqual(out["files"][0]["code"], "UU")
        self.assertIn("ours", out["options"][0])
        self.assertTrue((work / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(git(work, "rev-parse", "HEAD").stdout.strip(), local_sha)

    def test_14_resolve_ours_then_status(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "file.txt").write_text("ours\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "ours")
        self.advance_remote(other, content="theirs\n")
        self.git.pull("p")
        out = self.git.resolve_conflicts("p", side="ours")
        self.assertEqual(out["status"], "resolved")
        self.assertEqual(out["remaining_conflicts"], [])
        self.assertEqual((work / "file.txt").read_text(), "ours\n")
        self.assertTrue((work / ".git" / "MERGE_HEAD").exists())   # 不自动 commit

    def test_15_resolve_theirs(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "file.txt").write_text("ours\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "ours")
        self.advance_remote(other, content="theirs\n")
        self.git.pull("p")
        self.git.resolve_conflicts("p", side="theirs")
        self.assertEqual((work / "file.txt").read_text(), "theirs\n")

    def test_16_delete_modify_manual_required(self):
        work = init_repo(self.tmp / "dm")
        self.add_project("p", [work])
        git(work, "checkout", "-q", "-b", "theirs")
        (work / "file.txt").write_text("modified\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "modify")
        git(work, "checkout", "-q", "main")
        (work / "file.txt").unlink()
        git(work, "commit", "-q", "-am", "delete")
        git(work, "merge", "theirs", check=False)
        out = self.git.resolve_conflicts("p", side="ours")
        self.assertEqual(out["status"], "manual_required")
        self.assertTrue(out["manual"])
        self.assertIn("git", out["manual"][0]["suggestion"])

    def test_17_rename_conflict_manual_required(self):
        work = init_repo(self.tmp / "ren")
        self.add_project("p", [work])
        git(work, "checkout", "-q", "-b", "theirs")
        git(work, "mv", "file.txt", "theirs.txt")
        git(work, "commit", "-q", "-m", "rename theirs")
        git(work, "checkout", "-q", "main")
        git(work, "mv", "file.txt", "ours.txt")
        git(work, "commit", "-q", "-m", "rename ours")
        git(work, "merge", "theirs", check=False)
        out = self.git.resolve_conflicts("p", side="ours")
        self.assertEqual(out["status"], "manual_required")
        self.assertTrue(out["manual"])
        self.assertTrue(out["remaining_conflicts"])

    def test_18_resolve_without_merge_rejected(self):
        work = init_repo(self.tmp / "nomerge")
        self.add_project("p", [work])
        with self.assertRaises(ValueError) as ctx:
            self.git.resolve_conflicts("p", side="ours")
        self.assertIn("没有进行中的 merge", str(ctx.exception))

    def test_19_abort_restores_head(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "file.txt").write_text("ours\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "ours")
        self.advance_remote(other, content="theirs\n")
        before = git(work, "rev-parse", "HEAD").stdout.strip()
        self.git.pull("p")
        out = self.git.abort_merge("p")
        self.assertEqual(out["status"], "aborted")
        self.assertEqual(git(work, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertFalse((work / ".git" / "MERGE_HEAD").exists())

    def test_20_dirty_tracked_worktree_rejected(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "file.txt").write_text("local dirty\n", encoding="utf-8")
        self.advance_remote(other, content="remote\n")
        out = self.git.pull("p")
        self.assertEqual(out["status"], "failed")
        self.assertIn("未提交", out["error"])
        self.assertEqual((work / "file.txt").read_text(), "local dirty\n")

    def test_21_midstate_pull_rejected(self):
        work = init_repo(self.tmp / "mid")
        self.add_project("p", [work])
        (work / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        out = self.git.pull("p")
        self.assertEqual(out["status"], "failed")
        self.assertIn("中间态", out["error"])

    def test_22_fetch_failure_redacts_credentials(self):
        work = init_repo(self.tmp / "net")
        self.add_project("p", [work])
        git(work, "remote", "add", "origin",
            "https://user:supersecret@127.0.0.1:1/nope.git")
        out = self.git.pull("p")
        self.assertEqual(out["status"], "failed")
        self.assertNotIn("supersecret", json.dumps(out, ensure_ascii=False))


class CommitTest(GitBase):
    def preview(self, project="p", **kw):
        return self.git.prepare_commit(project, message=kw.pop("message", "msg"), **kw)

    def apply(self, preview, project="p", **kw):
        return self.git.apply_commit(
            project, message=kw.pop("message", "msg"),
            include_untracked=preview.get("include_untracked"),
            allow_sensitive=preview.get("allowed_sensitive"),
            allow_secret_findings=preview.get("allow_secret_findings", False),
            expected_fingerprint=preview["fingerprint"],
            expected_head=preview["head"]["sha"],
            expected_branch=kw.pop("expected_branch", preview.get("branch")), **kw)

    def test_26_detached_commit_rejected(self):
        work = init_repo(self.tmp / "detach")
        self.add_project("p", [work])
        git(work, "checkout", "-q", "--detach")
        (work / "x.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            self.preview()
        self.assertIn("detached HEAD", str(ctx.exception))

    def test_27_rebase_midstate_commit_rejected(self):
        work = init_repo(self.tmp / "rebase")
        self.add_project("p", [work])
        (work / ".git" / "REBASE_HEAD").write_text("0" * 40 + "\n")
        (work / "x.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            self.preview()
        self.assertIn("rebase", str(ctx.exception))

    def test_28_resolve_then_commit_completes_merge(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "file.txt").write_text("ours\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "ours")
        self.advance_remote(other, content="theirs\n")
        self.git.pull("p")
        self.git.resolve_conflicts("p", side="ours")
        out = self.apply(self.preview())
        self.assertEqual(out["status"], "committed")
        self.assertTrue(out["merge_completed"])
        parents = git(work, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
        self.assertEqual(len(parents), 3)
        self.assertFalse((work / ".git" / "MERGE_HEAD").exists())

    def test_29_merge_head_with_unmerged_commit_rejected(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work])
        (work / "file.txt").write_text("ours\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "ours")
        self.advance_remote(other, content="theirs\n")
        self.git.pull("p")
        with self.assertRaises(ValueError) as ctx:
            self.preview()
        self.assertIn("未解决", str(ctx.exception))

    def test_30_preview_reports_without_commit(self):
        work = init_repo(self.tmp / "preview")
        self.add_project("p", [work])
        (work / "file.txt").write_text("changed\n", encoding="utf-8")
        head = git(work, "rev-parse", "HEAD").stdout.strip()
        out = self.preview()
        self.assertEqual(out["status"], "preview")
        self.assertEqual(out["branch"], "main")
        self.assertEqual(out["tracked_changes"][0]["path"], "file.txt")
        self.assertTrue(out["fingerprint"])
        self.assertEqual(git(work, "rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual(git(work, "status", "--porcelain").stdout.strip(), "M file.txt")

    def test_31_apply_commit_success_and_single_use(self):
        work = init_repo(self.tmp / "apply")
        self.add_project("p", [work])
        (work / "file.txt").write_text("changed\n", encoding="utf-8")
        out = self.apply(self.preview())
        self.assertEqual(out["status"], "committed")
        self.assertEqual(git(work, "status", "--porcelain").stdout.strip(), "")
        self.assertEqual(out["commit"]["subject"], "msg")

    def test_32_fingerprint_change_invalidates(self):
        work = init_repo(self.tmp / "fp")
        self.add_project("p", [work])
        (work / "file.txt").write_text("changed\n", encoding="utf-8")
        pv = self.preview()
        (work / "file.txt").write_text("changed2\n", encoding="utf-8")
        out = self.apply(pv)
        self.assertEqual(out["status"], "failed")
        self.assertIn("预览后发生了变化", out["error"])

    def test_33_untracked_size_mtime_in_fingerprint(self):
        work = init_repo(self.tmp / "fp2")
        self.add_project("p", [work])
        (work / "u.txt").write_text("a\n", encoding="utf-8")
        pv = self.preview(include_untracked=["u.txt"])
        time.sleep(0.01)
        (work / "u.txt").write_text("bb\n", encoding="utf-8")
        out = self.apply(pv)
        self.assertEqual(out["status"], "failed")

    def test_34_head_change_invalidates_token(self):
        work = init_repo(self.tmp / "headmove")
        self.add_project("p", [work])
        (work / "file.txt").write_text("changed\n", encoding="utf-8")
        pv = self.preview()
        git(work, "commit", "-q", "-am", "tmp commit")     # HEAD 变化（工作区变干净）
        out = self.apply(pv)
        self.assertEqual(out["status"], "failed")
        self.assertIn("HEAD", out["error"])

    def test_34b_branch_switch_same_head_invalidates_token(self):
        work = init_repo(self.tmp / "branchmove")
        self.add_project("p", [work])
        (work / "file.txt").write_text("changed\n", encoding="utf-8")
        pv = self.preview()
        git(work, "checkout", "-q", "-b", "alt")           # 同一 commit 的另一分支
        out = self.apply(pv)
        self.assertEqual(out["status"], "failed")
        self.assertIn("分支", out["error"])
        self.assertEqual(git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "alt")

    def test_35_no_changes_blocked(self):
        work = init_repo(self.tmp / "clean")
        self.add_project("p", [work])
        out = self.preview()
        self.assertEqual(out["status"], "blocked")

    def test_36_default_stages_tracked_only(self):
        work = init_repo(self.tmp / "tracked")
        self.add_project("p", [work])
        (work / "file.txt").write_text("changed\n", encoding="utf-8")
        (work / "new.txt").write_text("new\n", encoding="utf-8")
        self.apply(self.preview())
        status = git(work, "status", "--porcelain").stdout
        self.assertIn("?? new.txt", status)

    def test_37_include_untracked_committed(self):
        work = init_repo(self.tmp / "inc")
        self.add_project("p", [work])
        (work / "new.txt").write_text("new\n", encoding="utf-8")
        pv = self.preview(include_untracked=["new.txt"])
        self.assertEqual(pv["include_untracked"], ["new.txt"])
        self.apply(pv)
        self.assertEqual(git(work, "status", "--porcelain").stdout.strip(), "")

    def test_38_pathspec_magic_literal(self):
        work = init_repo(self.tmp / "magic")
        self.add_project("p", [work])
        (work / "a.txt").write_text("a\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.preview(include_untracked=[":(glob)*.txt"])   # 不展开成 a.txt
        (work / ":(glob)note.txt").write_text("x\n", encoding="utf-8")
        pv = self.preview(include_untracked=[":(glob)note.txt"])
        self.apply(pv)
        status = git(work, "status", "--porcelain").stdout
        self.assertNotIn(":(glob)note.txt", status)
        self.assertIn("?? a.txt", status)     # 未列入的文件保持未跟踪

    def test_39_dot_env_blocked(self):
        work = init_repo(self.tmp / "env")
        self.add_project("p", [work])
        (work / ".env").write_text("A=1\n", encoding="utf-8")
        out = self.preview(include_untracked=[".env"])
        self.assertEqual(out["status"], "blocked")
        self.assertIn(".env", out["blocked_files"])
        self.assertIn("?? .env", git(work, "status", "--porcelain").stdout)

    def test_40_dot_env_example_allowed(self):
        work = init_repo(self.tmp / "envex")
        self.add_project("p", [work])
        (work / ".env.example").write_text("A=1\n", encoding="utf-8")
        pv = self.preview(include_untracked=[".env.example"])
        self.assertEqual(pv["status"], "preview")

    def test_41_allow_sensitive_explicit(self):
        work = init_repo(self.tmp / "envallow")
        self.add_project("p", [work])
        (work / ".env").write_text("A=1\n", encoding="utf-8")
        pv = self.preview(include_untracked=[".env"], allow_sensitive=[".env"])
        self.assertEqual(pv["status"], "preview")
        self.assertEqual(pv["allowed_sensitive"], [".env"])

    def test_42_tracked_env_example_and_tokenizer_allowed(self):
        work = init_repo(self.tmp / "trackedenv")
        self.add_project("p", [work])
        (work / ".env.example").write_text("A=1\n", encoding="utf-8")
        (work / "tokenizer.py").write_text("x = 1\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "add tracked")
        (work / ".env.example").write_text("A=2\n", encoding="utf-8")
        (work / "tokenizer.py").write_text("x = 2\n", encoding="utf-8")
        pv = self.preview()
        self.assertEqual(pv["status"], "preview")
        self.apply(pv)

    def test_43_secret_findings_blocked_no_raw(self):
        work = init_repo(self.tmp / "secret")
        self.add_project("p", [work])
        (work / "file.txt").write_text(f"key = {SECRET_AWS}\n", encoding="utf-8")
        out = self.preview()
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(out["reason"], "secret_findings")
        self.assertEqual(out["secret_findings"][0]["line"], 1)
        self.assertNotIn(SECRET_AWS, json.dumps(out, ensure_ascii=False))

    def test_44_allow_secret_findings(self):
        work = init_repo(self.tmp / "secret2")
        self.add_project("p", [work])
        (work / "file.txt").write_text(f"{SECRET_PEM}\n", encoding="utf-8")
        pv = self.preview(allow_secret_findings=True)
        self.assertEqual(pv["status"], "preview")
        self.assertTrue(pv["allow_secret_findings"])

    def test_45_normal_docs_not_blocked(self):
        work = init_repo(self.tmp / "docs")
        self.add_project("p", [work])
        (work / "file.txt").write_text(
            'SECRET = "SECRET-FEISHU-XYZ"\napi_key = "sk-你的key"\nobj_token="OBJTOKEN"\n',
            encoding="utf-8")
        self.assertEqual(self.preview()["status"], "preview")

    def test_46_message_validation(self):
        self.add_project("p", [init_repo(self.tmp / "msg")])
        for bad in ("", "a\nb", "x" * 501, "a\x00b"):
            with self.assertRaises(ValueError):
                self.git.prepare_commit("p", message=bad)

    def test_48_project_a_cannot_use_b_repo(self):
        a = init_repo(self.tmp / "A")
        bb = init_repo(self.tmp / "B")
        self.add_project("a", [a])
        self.add_project("b", [bb])
        with self.assertRaises(ValueError):
            self.git.resolve_repo("a", str(bb))

    def test_51_index_lock_failed(self):
        work = init_repo(self.tmp / "lock")
        self.add_project("p", [work])
        (work / "file.txt").write_text("changed\n", encoding="utf-8")
        pv = self.preview()
        (work / ".git" / "index.lock").write_text("")
        out = self.apply(pv)
        self.assertEqual(out["status"], "failed")
        self.assertIn("index.lock", out["error"])

    def test_52_no_subset_tracked_commit(self):
        work = init_repo(self.tmp / "subset")
        self.add_project("p", [work])
        (work / "file.txt").write_text("a\n", encoding="utf-8")
        (work / "two.txt").write_text("base\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "two")
        (work / "file.txt").write_text("b\n", encoding="utf-8")
        (work / "two.txt").write_text("changed\n", encoding="utf-8")
        self.apply(self.preview())     # 全量 tracked 提交（不支持子集）
        self.assertEqual(git(work, "status", "--porcelain").stdout.strip(), "")


class BudgetCancelTest(GitBase):
    def test_23_low_budget_fails_without_git(self):
        git = ProjectGit(deadline=time.monotonic() + 3)
        called = []
        git._spawn = lambda *a, **k: called.append(1)
        out = git.status("p")
        self.assertEqual(out["status"], "failed")
        self.assertIn("剩余时间不足", out["error"])
        self.assertEqual(called, [])

    def test_24_budget_decreases(self):
        repo = init_repo(self.tmp / "budget")
        self.add_project("p", [repo])
        git = ProjectGit(deadline=time.monotonic() + 20)
        first = git._budget()
        time.sleep(0.02)
        self.assertLess(git._budget(), first)
        self.assertLessEqual(git._budget(), ProjectGit.TOOL_BUDGET_SEC)

    def test_25_cancel_terminates_subprocess(self):
        repo = init_repo(self.tmp / "cancel")
        self.add_project("p", [repo])
        git = ProjectGit()
        real_spawn = git._spawn
        started = subprocess.Popen(["sleep", "30"], start_new_session=True)
        git._spawn = lambda argv, cwd: started
        errors = []

        def worker():
            try:
                git.run(["git", "status"], repo)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.2)
        git.cancel_event.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(type(errors[0]).__name__, "GitCancelled")
        self.assertIsNotNone(started.poll())
        git._spawn = real_spawn

    def test_47_same_repo_lock_serializes(self):
        repo = init_repo(self.tmp / "lockprobe")
        order = []

        def worker(tag):
            with self.git.op_lock(repo):
                order.append(f"{tag}-in")
                time.sleep(0.05)
                order.append(f"{tag}-out")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(order[0].split("-")[0], order[1].split("-")[0])  # 同任务 in/out 相邻
        self.assertEqual(order[1].split("-")[1], "out")


if __name__ == "__main__":
    unittest.main()
