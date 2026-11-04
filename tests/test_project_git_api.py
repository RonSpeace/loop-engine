#!/usr/bin/env python3
"""项目 Git 操作端点测试（v1）：pick-folder / git-detect / status / pull / push / credential。

覆盖：
- POST /api/system/pick-folder：非 darwin 501、成功 {path}、取消 {cancelled:true}、失败 500
  且不回显内部命令。
- POST /api/system/git-detect：真临时 git 仓库探测（分支/远端/base_branch）、非 git 宽松返回、
  相对/不存在路径逐条 error、请求级 400。
- POST /api/projects/<name>/git/status：只读（不 fetch，远端前进后 behind 不刷新）、ahead/behind、
  dirty 计数、非 git 路径逐条 error、未知项目 404。
- POST .../git/pull：真 bare remote + 克隆（落后快进 pulled / 脏工作区拒绝 / 冲突保留现场）、
  repo 参数非法 400、未知项目 404。
- POST .../git/push：confirm 强制、只推 base_branch（分支不一致拒绝）、脏工作区拒绝、
  pushed/up_to_date/behind 状态、未知项目 404。
- GET/POST .../git/credential：安全视图无密码、save 后自动测试（成功/失败均保留凭据）、
  test/clear、密码不进响应；凭据文件隔离到临时路径（绝不写真实 data/git_credentials.json）。

隔离：临时 DB + 临时 projects.json + 临时 git 仓库（bare remote/克隆）+ 临时凭据文件 + 临时锁目录；
git 远端操作走真实 git（不触网，远端是本地 bare 仓库）；pick-folder 只 mock subprocess。

运行：
    .venv/bin/python -m pytest tests/test_project_git_api.py -q
"""
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402
import git_ops  # noqa: E402
from board_modules.blueprints import projects as proj_bp  # noqa: E402
from board_modules.services.project_git import ProjectGit  # noqa: E402

SECRET_PW = "tok-super-secret-9381"


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


class GitApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="req-gitapi-"))
        cls._orig_db = b.DB_PATH
        b.DB_PATH = cls.root / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="case-", dir=str(self.root)))
        self.projects_file = self.tmp / "projects.json"
        self._orig = (b.PROJECTS_FILE, b.PROJECT_PATHS, git_ops.SECRETS_PATH,
                      git_ops.LOCKS_DIR)
        b.PROJECTS_FILE = self.projects_file
        b.PROJECT_PATHS = {}
        git_ops.SECRETS_PATH = self.tmp / "git_credentials.json"
        git_ops.LOCKS_DIR = self.tmp / "locks"
        self.addCleanup(self._restore)
        self._data = {}

    def _restore(self):
        b.PROJECTS_FILE, b.PROJECT_PATHS = self._orig[0], self._orig[1]
        git_ops.SECRETS_PATH, git_ops.LOCKS_DIR = self._orig[2], self._orig[3]

    def add_project(self, name, paths, **cfg):
        entry = {"paths": [str(p) for p in paths]}
        entry.update(cfg)
        self._data[name] = entry
        self.projects_file.write_text(json.dumps(self._data, ensure_ascii=False),
                                      encoding="utf-8")
        b.PROJECT_PATHS = {k: [str(p) for p in (v.get("paths") or [])]
                           for k, v in self._data.items()}
        return entry

    def make_pair(self, name="repo"):
        remote = self.tmp / f"{name}-remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        work = init_repo(self.tmp / name)
        git(work, "remote", "add", "origin", str(remote))
        git(work, "push", "-q", "-u", "origin", "main")
        other = self.tmp / f"{name}-other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        git(other, "config", "user.email", "t2@example.com")
        git(other, "config", "user.name", "Other")
        return work, other, remote

    def advance_remote(self, other, filename="file.txt", content="remote\n"):
        (other / filename).write_text(content, encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "remote change")
        git(other, "push", "-q", "origin", "main")

    def local_commit(self, work, name="local.txt", content="l\n"):
        (work / name).write_text(content, encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "local change")

    def post(self, url, **kw):
        return self.client.post(url, json=kw or None)


class PickFolderTest(GitApiBase):
    def test_p1_non_darwin_501(self):
        with mock.patch.object(proj_bp.platform, "system", return_value="Linux"):
            resp = self.post("/api/system/pick-folder")
        self.assertEqual(resp.status_code, 501)
        text = json.dumps(resp.get_json(), ensure_ascii=False)
        self.assertIn("error", resp.get_json())
        self.assertNotIn("osascript", text)
        self.assertNotIn("choose folder", text)

    def _darwin(self):
        return mock.patch.object(proj_bp.platform, "system", return_value="Darwin")

    def test_p2_success_returns_path(self):
        target = self.tmp / "picked"
        target.mkdir()
        run = mock.Mock(return_value=SimpleNamespace(
            returncode=0, stdout=str(target) + "\n", stderr=""))
        with self._darwin(), mock.patch.object(b.subprocess, "run", run):
            resp = self.post("/api/system/pick-folder")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"path": str(target)})
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "osascript")
        self.assertIn("-e", argv)

    def test_p3_cancelled(self):
        run = mock.Mock(return_value=SimpleNamespace(
            returncode=1, stdout="",
            stderr="execution error: User canceled. (-128)\n"))
        with self._darwin(), mock.patch.object(b.subprocess, "run", run):
            resp = self.post("/api/system/pick-folder")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"cancelled": True})

    def test_p4_failure_hides_command_details(self):
        run = mock.Mock(return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="osascript: boom"))
        with self._darwin(), mock.patch.object(b.subprocess, "run", run):
            resp = self.post("/api/system/pick-folder")
        self.assertEqual(resp.status_code, 500)
        text = json.dumps(resp.get_json(), ensure_ascii=False)
        self.assertIn("error", resp.get_json())
        for frag in ("osascript", "boom", "choose folder"):
            self.assertNotIn(frag, text)

    def test_p5_selected_not_dir(self):
        run = mock.Mock(return_value=SimpleNamespace(
            returncode=0, stdout="/definitely/not/here-xyz\n", stderr=""))
        with self._darwin(), mock.patch.object(b.subprocess, "run", run):
            resp = self.post("/api/system/pick-folder")
        self.assertEqual(resp.status_code, 500)
        self.assertIn("error", resp.get_json())


class GitDetectTest(GitApiBase):
    def test_d1_detect_mixed_paths(self):
        work, _other, remote = self.make_pair()
        plain = self.tmp / "plain"
        plain.mkdir()
        missing = self.tmp / "ghost"
        resp = self.post("/api/system/git-detect",
                         paths=[str(work), str(plain), "relative/x", str(missing)])
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        rows = data["repos"]
        self.assertEqual([r["path"] for r in rows],
                         [str(work), str(plain), "relative/x", str(missing)])
        row = rows[0]
        self.assertTrue(row["is_git"])
        self.assertEqual(row["name"], work.name)
        self.assertEqual(row["current_branch"], "main")
        self.assertIn("main", row["branches"])
        self.assertEqual(row["remotes"], [{"name": "origin", "url": str(remote)}])
        self.assertEqual(row["base_branch"], "main")
        self.assertIsNone(row["error"])
        self.assertFalse(rows[1]["is_git"])
        self.assertIsNone(rows[1]["error"])                 # 非 git 宽松返回，不报错
        self.assertIn("绝对路径", rows[2]["error"])
        self.assertIn("不存在", rows[3]["error"])
        self.assertEqual(rows[2]["remotes"], [])
        self.assertEqual(rows[2]["branches"], [])

    def test_d2_request_validation(self):
        for body in (["x"], {"paths": "x"}, {"paths": []}, {"paths": [1] * 51}):
            resp = self.client.post("/api/system/git-detect", json=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn("error", resp.get_json())


class GitStatusTest(GitApiBase):
    def test_s1_status_fields_readonly(self):
        work, other, remote = self.make_pair()
        self.local_commit(work)
        (work / "file.txt").write_text("dirty\n", encoding="utf-8")
        (work / "untracked.txt").write_text("u\n", encoding="utf-8")
        self.add_project("p", [work], base_branch="main",
                         git={"remote": "origin", "url": "https://display/x.git"})
        self.advance_remote(other)                          # 远端前进：status 不 fetch 不应看到
        resp = self.post("/api/projects/p/git/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        row = data["repos"][0]
        self.assertEqual(row["name"], work.name)
        self.assertEqual(row["branch"], "main")
        self.assertEqual(row["ahead"], 1)
        self.assertEqual(row["behind"], 0)                  # 未 fetch：origin/main 仍是旧引用
        self.assertEqual(row["dirty"], 2)
        self.assertEqual(row["dirty_detail"], {"changed": 1, "untracked": 1, "deleted": 0})
        self.assertEqual(row["remote"], "origin")
        self.assertEqual(row["url"], str(remote))           # 实际远端优先于展示配置
        self.assertIsNone(row["error"])

    def test_s2_status_non_git_and_unknown_project(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.add_project("p", [plain])
        data = self.post("/api/projects/p/git/status").get_json()
        self.assertFalse(data["ok"])
        self.assertIn("不是 git 仓库", data["repos"][0]["error"])
        self.assertEqual(data["repos"][0]["dirty"], 0)
        self.assertEqual(self.post("/api/projects/ghost/git/status").status_code, 404)


class GitPullTest(GitApiBase):
    def test_u1_pull_fast_forward(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        before = git(work, "rev-parse", "HEAD").stdout.strip()
        self.advance_remote(other)
        data = self.post("/api/projects/p/git/pull").get_json()
        self.assertTrue(data["ok"])
        row = data["repos"][0]
        self.assertEqual(row["status"], "pulled")
        self.assertEqual(row["before"]["sha"], before)
        self.assertNotEqual(row["after"]["sha"], before)
        self.assertTrue(row["message"])
        self.assertEqual((work / "file.txt").read_text(), "remote\n")

    def test_u2_pull_dirty_tracked_rejected(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        (work / "file.txt").write_text("local dirty\n", encoding="utf-8")
        self.advance_remote(other)
        data = self.post("/api/projects/p/git/pull").get_json()
        self.assertFalse(data["ok"])
        row = data["repos"][0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("未提交", row["message"])
        self.assertEqual((work / "file.txt").read_text(), "local dirty\n")

    def test_u3_pull_conflict_keeps_scene(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        (work / "file.txt").write_text("ours\n", encoding="utf-8")
        git(work, "commit", "-q", "-am", "ours")
        self.advance_remote(other, content="theirs\n")
        data = self.post("/api/projects/p/git/pull").get_json()
        self.assertFalse(data["ok"])
        row = data["repos"][0]
        self.assertEqual(row["status"], "conflict")
        self.assertIn("冲突", row["message"])
        self.assertEqual(row["conflicts"][0]["code"], "UU")
        self.assertTrue((work / ".git" / "MERGE_HEAD").exists())

    def test_u4_pull_repo_param_and_errors(self):
        work, _other, _remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        data = self.post("/api/projects/p/git/pull", repo=str(work)).get_json()
        self.assertEqual(data["repos"][0]["status"], "up_to_date")
        self.assertEqual(self.post("/api/projects/p/git/pull", repo="ghost").status_code, 400)
        self.assertEqual(self.post("/api/projects/ghost/git/pull").status_code, 404)


class GitPushTest(GitApiBase):
    def test_h1_push_success_only_base(self):
        work, _other, remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        self.local_commit(work, "local.txt", "x\n")
        data = self.post("/api/projects/p/git/push", confirm=True).get_json()
        self.assertTrue(data["ok"])
        row = data["repos"][0]
        self.assertEqual(row["status"], "pushed")
        self.assertEqual(row["ahead"], 1)
        self.assertIn("已推送", row["message"])
        remote_head = git(remote, "rev-parse", "refs/heads/main").stdout.strip()
        self.assertEqual(remote_head, git(work, "rev-parse", "HEAD").stdout.strip())
        self.assertNotIn("f-", git(remote, "for-each-ref", "--format=%(refname)").stdout)

    def test_h2_push_requires_confirm(self):
        work, _other, remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        self.local_commit(work)
        local_head = git(work, "rev-parse", "HEAD").stdout.strip()
        for body in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}):
            resp = self.client.post("/api/projects/p/git/push", json=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn("confirm", resp.get_json()["error"])
        self.assertNotEqual(git(remote, "rev-parse", "refs/heads/main").stdout.strip(),
                            local_head)                     # 未确认 → 远端未动

    def test_h3_push_up_to_date(self):
        work, _other, _remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        data = self.post("/api/projects/p/git/push", confirm=True).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["repos"][0]["status"], "up_to_date")
        self.assertEqual(data["repos"][0]["ahead"], 0)

    def test_h4_push_dirty_and_wrong_branch_rejected(self):
        work, _other, _remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        (work / "file.txt").write_text("dirty\n", encoding="utf-8")
        data = self.post("/api/projects/p/git/push", confirm=True).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["repos"][0]["status"], "failed")
        self.assertIn("未提交", data["repos"][0]["message"])
        git(work, "checkout", "-q", "--", "file.txt")
        (work / "untracked.txt").write_text("u\n", encoding="utf-8")
        data = self.post("/api/projects/p/git/push", confirm=True).get_json()
        self.assertFalse(data["ok"])
        self.assertIn("未跟踪", data["repos"][0]["message"])     # 工作区必须干净
        (work / "untracked.txt").unlink()
        git(work, "checkout", "-q", "-b", "feature")
        data = self.post("/api/projects/p/git/push", confirm=True).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["repos"][0]["status"], "failed")
        self.assertIn("不一致", data["repos"][0]["message"])

    def test_h5_push_diverged_reports_behind(self):
        work, other, _remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        self.local_commit(work, "mine.txt", "m\n")
        self.advance_remote(other, filename="theirs.txt", content="t\n")
        data = self.post("/api/projects/p/git/push", confirm=True).get_json()
        self.assertFalse(data["ok"])
        row = data["repos"][0]
        self.assertEqual(row["status"], "behind")
        self.assertIn("分叉", row["message"])

    def test_h6_push_multi_repo_repo_param_and_errors(self):
        work1, _o1, _r1 = self.make_pair("r1")
        work2, _o2, _r2 = self.make_pair("r2")
        self.add_project("multi", [work1, work2], base_branch="main")
        data = self.post("/api/projects/multi/git/push", confirm=True).get_json()
        self.assertEqual(len(data["repos"]), 2)
        self.assertEqual({row["status"] for row in data["repos"]}, {"up_to_date"})
        data = self.post("/api/projects/multi/git/push", confirm=True,
                         repo=str(work1)).get_json()
        self.assertEqual(len(data["repos"]), 1)
        self.assertEqual(self.post("/api/projects/multi/git/push", confirm=True,
                                   repo="ghost").status_code, 400)
        self.assertEqual(self.post("/api/projects/ghost/git/push",
                                   confirm=True).status_code, 404)

    def test_h7_push_without_base_branch_rejected(self):
        work, _other, _remote = self.make_pair()
        self.add_project("p", [work])                      # 未配置 base_branch
        data = self.post("/api/projects/p/git/push", confirm=True).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["repos"][0]["status"], "failed")
        self.assertIn("base_branch", data["repos"][0]["message"])


class CredentialEnvInjectionTest(GitApiBase):
    """远端操作必须注入 git_ops.git_env 产出的凭据环境（token/ssh 才拿得到）。"""

    def test_v1_pull_and_push_remote_ops_use_git_env(self):
        work, _other, _remote = self.make_pair()
        self.add_project("p", [work], base_branch="main")
        sentinel = {"SENTINEL_ENV": "1"}
        seen = []
        real_spawn = ProjectGit._spawn

        def spy(_self, argv, cwd, env=None):
            seen.append(env)
            if env is None:
                return real_spawn(_self, argv, cwd)
            return real_spawn(_self, argv, cwd, env=env)

        with mock.patch.object(ProjectGit, "_spawn", spy), \
                mock.patch.object(git_ops, "git_env",
                                  lambda project: contextlib.nullcontext(sentinel)):
            pull = ProjectGit().pull("p")
            push = ProjectGit().push("p")
        self.assertEqual(pull["status"], "up_to_date")
        self.assertEqual(push["status"], "up_to_date")
        self.assertIn(sentinel, seen)          # fetch/push 远端命令带凭据 env
        self.assertIn(None, seen)              # 本地只读命令仍走默认消毒环境

    def test_v2_pull_takes_repo_lock_per_repo(self):
        work1, _o1, _r1 = self.make_pair("k1")
        work2, _o2, _r2 = self.make_pair("k2")
        self.add_project("multi", [work1, work2], base_branch="main")
        locked = []
        real_lock = git_ops.repo_lock

        def spy(repo, *args, **kw):
            locked.append(str(repo))
            return real_lock(repo, *args, **kw)

        with mock.patch.object(git_ops, "repo_lock", spy):
            self.post("/api/projects/multi/git/status")       # 只读不取锁
            self.assertEqual(locked, [])
            data = self.post("/api/projects/multi/git/pull").get_json()
        self.assertEqual(len(data["repos"]), 2)
        self.assertEqual(sorted(Path(x).resolve() for x in locked),
                         sorted([work1.resolve(), work2.resolve()]))


class CredentialTest(GitApiBase):
    URL = "https://example.com/team/repo.git"

    def _post(self, **body):
        return self.client.post("/api/projects/p/git/credential", json=body)

    def test_c1_get_unconfigured_no_password(self):
        self.add_project("p", [self.tmp])
        data = self.client.get("/api/projects/p/git/credential").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["state"], {"configured": False})
        self.assertEqual(self.client.get("/api/projects/ghost/git/credential").status_code, 404)

    def test_c2_save_then_test_and_clear(self):
        self.add_project("p", [self.tmp])
        ok_run = mock.Mock(return_value=(0, "", ""))
        with mock.patch.object(git_ops, "run_git", ok_run):
            resp = self._post(action="save", kind="token", username="alice",
                              password=SECRET_PW, url=self.URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["test"]["ok"])
        self.assertEqual(data["state"]["configured"], True)
        self.assertEqual(data["state"]["kind"], "token")
        self.assertEqual(data["state"]["username"], "alice")
        self.assertEqual(data["state"]["url"], self.URL)
        self.assertNotIn(SECRET_PW, json.dumps(data, ensure_ascii=False))
        secret_file = git_ops.SECRETS_PATH
        self.assertTrue(secret_file.exists())              # 已落隔离临时文件
        self.assertEqual(secret_file.stat().st_mode & 0o777, 0o600)
        self.assertIn(SECRET_PW, secret_file.read_text(encoding="utf-8"))  # 隔离文件内可存
        # GET 安全视图永不含密码
        got = self.client.get("/api/projects/p/git/credential").get_json()
        self.assertNotIn(SECRET_PW, json.dumps(got, ensure_ascii=False))
        self.assertEqual(got["state"]["username"], "alice")
        # test 动作：用当前凭据测连接
        with mock.patch.object(git_ops, "run_git", mock.Mock(return_value=(0, "", ""))):
            data = self._post(action="test", url=self.URL).get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["test"]["ok"])
        # clear：删除凭据
        data = self._post(action="clear").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["state"], {"configured": False})

    def test_c3_save_test_failure_keeps_credential(self):
        self.add_project("p", [self.tmp])
        bad_run = mock.Mock(return_value=(
            1, "", "fatal: Authentication failed for 'https://u:" + SECRET_PW
            + "@example.com/team/repo.git'"))
        with mock.patch.object(git_ops, "run_git", bad_run):
            data = self._post(action="save", kind="token", username="alice",
                              password=SECRET_PW, url=self.URL).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["state"]["configured"], True)   # 凭据保留
        self.assertFalse(data["test"]["ok"])
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(SECRET_PW, text)                    # 密码绝不回显
        self.assertIn("***", data["test"]["message"])        # URL userinfo 已脱敏

    def test_c4_save_rejection_matrix(self):
        self.add_project("p", [self.tmp])
        cases = [
            ("缺 password", dict(action="save", kind="token", url=self.URL)),
            ("ssh 缺 key", dict(action="save", kind="ssh")),
            ("ssh key 不存在", dict(action="save", kind="ssh",
                                  ssh_key=str(self.tmp / "nokey"))),
            ("kind 非法", dict(action="save", kind="oauth", password="x")),
            ("password 非串", dict(action="save", kind="token", password=3)),
            ("action 非法", dict(action="remove")),
            ("url 非串", dict(action="save", kind="token", password="x", url=3)),
            ("url 内嵌密码", dict(action="save", kind="token", password="x",
                                url="https://u:pw@example.com/r.git")),
        ]
        for label, body in cases:
            resp = self._post(**body)
            self.assertEqual(resp.status_code, 400, label)
            self.assertIn("error", resp.get_json(), label)
        self.assertFalse(git_ops.SECRETS_PATH.exists())      # 拒绝 → 零写入

    def test_c5_get_redacts_legacy_url_userinfo(self):
        """存量凭据文件即使 URL 内嵌 userinfo，GET 也只返回脱敏 url（防密码经 URL 回显）。"""
        self.add_project("p", [self.tmp])
        git_ops.SECRETS_PATH.write_text(json.dumps({
            "p": {"kind": "token", "username": "u",
                  "url": "https://u:" + SECRET_PW + "@example.com/r.git",
                  "password": SECRET_PW}}), encoding="utf-8")
        data = self.client.get("/api/projects/p/git/credential").get_json()
        self.assertTrue(data["state"]["configured"])
        self.assertEqual(data["state"]["url"], "https://***@example.com/r.git")
        self.assertNotIn(SECRET_PW, json.dumps(data, ensure_ascii=False))

    def test_c6_get_never_reads_password(self):
        self.add_project("p", [self.tmp])
        with mock.patch.object(git_ops, "run_git", mock.Mock(return_value=(0, "", ""))):
            self._post(action="save", kind="token", password=SECRET_PW,
                       url=self.URL, username="u")
        for _ in range(2):
            got = self.client.get("/api/projects/p/git/credential").get_json()
            self.assertNotIn(SECRET_PW, json.dumps(got, ensure_ascii=False))
            self.assertNotIn("password", json.dumps(got, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
