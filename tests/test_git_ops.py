#!/usr/bin/env python3
"""git_ops 公共件测试（凭据 0600 / askpass 清理 / 跨进程锁 / 只读探测）。

隔离：凭据文件与锁目录都指到临时目录（patch git_ops.SECRETS_PATH / git_ops.LOCKS_DIR），
绝不写真实 data/git_credentials.json 与 ~/.req-board/locks。
运行：.venv/bin/python -m pytest -q tests/test_git_ops.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import git_ops as g  # noqa: E402


class GitOpsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="git-ops-"))
        self._p1 = mock.patch.object(g, "SECRETS_PATH", self.tmp / "secrets.json")
        self._p2 = mock.patch.object(g, "LOCKS_DIR", self.tmp / "locks")
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _git_repo(self, name="repo"):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
        (d / "a.txt").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=d, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=d, check=True)
        return d


class CredentialTest(GitOpsBase):
    def test_roundtrip_and_no_secret_in_state(self):
        state = g.save_credential("proj", kind="token", url="https://example.com/r.git",
                                  username="alice", password="s3cr3t!")
        self.assertTrue(state["configured"])
        self.assertEqual(state["username"], "alice")
        self.assertNotIn("password", state)
        self.assertNotIn("s3cr3t", repr(state))
        self.assertEqual(g.credential_for("proj")["password"], "s3cr3t!")
        self.assertEqual(g.credential_for("missing"), None)

    def test_secret_file_permissions_0600(self):
        g.save_credential("proj", kind="token", username="u", password="p")
        self.assertEqual(os.stat(g.SECRETS_PATH).st_mode & 0o777, 0o600)

    def test_ssh_requires_existing_key(self):
        with self.assertRaises(ValueError):
            g.save_credential("proj", kind="ssh", ssh_key=str(self.tmp / "nope"))
        key = self.tmp / "id_ed25519"
        key.write_text("dummy", encoding="utf-8")
        state = g.save_credential("proj", kind="ssh", ssh_key=str(key))
        self.assertEqual(state["kind"], "ssh")
        self.assertTrue(state["configured"])

    def test_token_requires_password_and_clear(self):
        with self.assertRaises(ValueError):
            g.save_credential("proj", kind="token")
        g.save_credential("proj", kind="token", username="u", password="p")
        self.assertFalse(g.delete_credential("proj")["configured"])
        self.assertFalse(g.credential_state("proj")["configured"])

    def test_corrupt_file_is_unconfigured(self):
        g.SECRETS_PATH.write_text("{not json", encoding="utf-8")
        self.assertEqual(g.credential_for("proj"), None)
        self.assertFalse(g.credential_state("proj")["configured"])


class GitEnvTest(GitOpsBase):
    def test_system_credential_has_no_askpass(self):
        with g.git_env("proj") as env:
            self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
            self.assertNotIn("GIT_ASKPASS", env)
            self.assertNotIn("GIT_SSH_COMMAND", env)

    def test_token_askpass_exists_during_and_removed_after(self):
        g.save_credential("proj", kind="token", username="u", password="p@ss word")
        ambient = os.environ.get("GIT_ASKPASS")   # IDE/终端可能本来就设了，断言的是「不泄漏」而非「不存在」
        with g.git_env("proj") as env:
            ask = env["GIT_ASKPASS"]
            self.assertTrue(Path(ask).is_file())
            self.assertEqual(os.stat(ask).st_mode & 0o777, 0o700)
            # 密码不写在脚本正文里（脚本只 cat 0600 文件）
            self.assertNotIn("p@ss word", Path(ask).read_text(encoding="utf-8"))
            d = Path(ask).parent
        self.assertFalse(d.exists())
        self.assertEqual(os.environ.get("GIT_ASKPASS"), ambient)

    def test_ssh_env_uses_key(self):
        key = self.tmp / "id_ed25519"
        key.write_text("dummy", encoding="utf-8")
        g.save_credential("proj", kind="ssh", ssh_key=str(key))
        with g.git_env("proj") as env:
            self.assertIn("-i", env["GIT_SSH_COMMAND"])
            self.assertIn(str(key), env["GIT_SSH_COMMAND"])
            self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])


class RepoLockTest(GitOpsBase):
    def test_second_acquire_times_out_while_held(self):
        repo = self._git_repo()
        with g.repo_lock(repo):
            t0 = time.time()
            with self.assertRaises(TimeoutError):
                with g.repo_lock(repo, timeout=0.3):
                    pass
            self.assertLess(time.time() - t0, 5)

    def test_lock_released_after_context(self):
        repo = self._git_repo()
        with g.repo_lock(repo):
            pass
        with g.repo_lock(repo, timeout=1):
            pass


class ReadonlyHelpersTest(GitOpsBase):
    def test_current_branch_remote_and_ahead_behind(self):
        repo = self._git_repo()
        self.assertEqual(g.current_branch(repo), "main")
        self.assertEqual(g.remote_names(repo), [])
        self.assertEqual(g.remote_url(repo), "")
        # 无 remote 引用 → None,None
        self.assertEqual(g.ahead_behind(repo, "main"), (None, None))
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
        self.assertEqual(g.ahead_behind(repo, "main"), (0, 0))
        (repo / "b.txt").write_text("b", encoding="utf-8")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c2"], cwd=repo, check=True)
        self.assertEqual(g.ahead_behind(repo, "main"), (1, 0))

    def test_run_git_does_not_raise_on_bad_repo(self):
        rc, _, err = g.run_git(self.tmp / "not-a-repo", ["status"])
        self.assertNotEqual(rc, 0)
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
