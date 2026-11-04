#!/usr/bin/env python3
"""项目级 Git 配置测试（v1）：GET 归一 + POST/PATCH 校验与写回。

覆盖：
- GET /api/projects/<name> 恒返回归一后的 git/worktree/git_sync（含默认值）；
  旧布尔 fetch_before/push_after → before_card/on_done 折算（新枚举优先）；非法存量值回退默认。
- POST /api/projects 接受可选 git/worktree/git_sync：非法 400 零写入；未提交的段不写盘。
- PATCH /api/projects/<name>：仅有提交的键被覆盖；git/git_sync 整对象替换并补默认；
  旧布尔写回统一为新枚举（不落旧键）；未知项目字段（company/models/custom）原样保留；
  归一后无变化 changed=false 且不写盘/.bak/mtime 不动。
- fail-closed 拒绝矩阵（git/worktree/git_sync 各非法形态）零写入。

隔离：临时 DB + 临时 projects.json（b.PROJECTS_FILE 钉一份，结束还原）；不触真实
data/board.db、真实 projects.json（软链穿透红线）。仓库路径用带 .git 的临时目录（只做路径校验）。

运行：
    .venv/bin/python -m pytest tests/test_projects_git_config.py -q
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402

GIT_DEFAULTS = {"remote": "origin", "url": "", "credential": "system", "ssh_key": ""}
SYNC_DEFAULTS = {"before_card": "off", "on_done": "off",
                 "diverge": "overlap-check", "timeout": 120}


class GitConfigBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-gitcfg-"))
        cls._orig_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.pj = self.tmp / f"projects-{self._testMethodName}.json"
        self._orig_pj = b.PROJECTS_FILE
        self._orig_pp = b.PROJECT_PATHS
        b.PROJECTS_FILE = self.pj
        self.addCleanup(self._restore)
        self.repo = self.tmp / f"repo-{self._testMethodName}"
        self.repo.mkdir(exist_ok=True)
        (self.repo / ".git").mkdir(exist_ok=True)
        self._write({"p1": {"paths": [str(self.repo)], "goal": "G1",
                            "company": False, "models": {"dev": "m1"},
                            "x_custom": {"a": [1, 2], "b": None}}})

    def _restore(self):
        b.PROJECTS_FILE = self._orig_pj
        b.PROJECT_PATHS = self._orig_pp

    def _write(self, data):
        self.pj.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        b.PROJECT_PATHS = b._projects_config()

    def _raw(self):
        return json.loads(self.pj.read_text(encoding="utf-8"))

    def _sig(self):
        st = self.pj.stat()
        return (st.st_mtime_ns, st.st_size)

    def _bak(self):
        return self.pj.parent / (self.pj.name + ".bak")

    def _assert_zero_write(self, before_sig, before_bytes):
        self.assertEqual(self.pj.read_bytes(), before_bytes)
        self.assertEqual(self._sig(), before_sig)
        self.assertFalse(self._bak().exists())
        self.assertFalse((self.pj.parent / (self.pj.name + ".tmp")).exists())


class GetNormalizeTest(GitConfigBase):
    def test_g1_get_returns_defaults(self):
        resp = self.client.get("/api/projects/p1")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["git"], GIT_DEFAULTS)
        self.assertEqual(d["worktree"], "per-requirement")
        self.assertEqual(d["git_sync"], SYNC_DEFAULTS)

    def test_g2_get_normalizes_stored_config(self):
        self._write({"p1": {"paths": [str(self.repo)],
                            "git": {"remote": "upstream", "url": "https://example.com/a.git",
                                    "credential": "token", "ssh_key": "/k/id_ed25519"},
                            "worktree": "per-requirement",
                            "git_sync": {"before_card": "fetch", "on_done": "manual",
                                         "diverge": "ff-only", "timeout": 45}}})
        d = self.client.get("/api/projects/p1").get_json()
        self.assertEqual(d["git"], {"remote": "upstream", "url": "https://example.com/a.git",
                                    "credential": "token", "ssh_key": "/k/id_ed25519"})
        self.assertEqual(d["git_sync"], {"before_card": "fetch", "on_done": "manual",
                                         "diverge": "ff-only", "timeout": 45})

    def test_g3_get_legacy_booleans(self):
        self._write({"p1": {"paths": [str(self.repo)],
                            "git_sync": {"fetch_before": True, "push_after": True}}})
        sync = self.client.get("/api/projects/p1").get_json()["git_sync"]
        self.assertEqual(sync["before_card"], "pull")     # 旧布尔 true → pull
        self.assertEqual(sync["on_done"], "auto")
        self._write({"p1": {"paths": [str(self.repo)],
                            "git_sync": {"fetch_before": False, "push_after": False}}})
        sync = self.client.get("/api/projects/p1").get_json()["git_sync"]
        self.assertEqual(sync["before_card"], "off")
        self.assertEqual(sync["on_done"], "off")

    def test_g4_get_enum_wins_over_legacy(self):
        self._write({"p1": {"paths": [str(self.repo)],
                            "git_sync": {"before_card": "fetch", "fetch_before": True,
                                         "on_done": "manual", "push_after": True}}})
        sync = self.client.get("/api/projects/p1").get_json()["git_sync"]
        self.assertEqual(sync["before_card"], "fetch")
        self.assertEqual(sync["on_done"], "manual")

    def test_g5_get_invalid_values_fall_back(self):
        self._write({"p1": {"paths": [str(self.repo)],
                            "git": {"remote": "", "url": 3, "credential": "nope",
                                    "ssh_key": 5},
                            "worktree": "shared",
                            "git_sync": {"before_card": "sometimes", "on_done": "later",
                                         "diverge": "merge", "timeout": 0}}})
        d = self.client.get("/api/projects/p1").get_json()
        self.assertEqual(d["git"], GIT_DEFAULTS)
        self.assertEqual(d["worktree"], "per-requirement")
        self.assertEqual(d["git_sync"], SYNC_DEFAULTS)

    def test_g6_unknown_project_and_bad_file(self):
        self.assertEqual(self.client.get("/api/projects/ghost").status_code, 404)
        self.pj.write_text("{bad", encoding="utf-8")
        self.assertEqual(self.client.get("/api/projects/p1").status_code, 400)
        self.pj.unlink()
        self.assertEqual(self.client.get("/api/projects/p1").status_code, 404)


class PostConfigTest(GitConfigBase):
    def _post(self, name="newp", **kw):
        body = {"name": name, "paths": [str(self.repo)]}
        body.update(kw)
        return self.client.post("/api/projects", json=body)

    def test_p1_post_writes_normalized_config(self):
        resp = self._post(git={"credential": "token", "url": "git@example.com:a.git"},
                          worktree="per-requirement",
                          git_sync={"before_card": "pull", "on_done": "manual"})
        self.assertEqual(resp.status_code, 201)
        entry = self._raw()["newp"]
        self.assertEqual(entry["git"], {"remote": "origin",
                                        "url": "git@example.com:a.git",
                                        "credential": "token", "ssh_key": ""})
        self.assertEqual(entry["worktree"], "per-requirement")
        self.assertEqual(entry["git_sync"], {"before_card": "pull", "on_done": "manual",
                                             "diverge": "overlap-check", "timeout": 120})

    def test_p2_post_no_config_does_not_write_defaults(self):
        self.assertEqual(self._post(name="bare").status_code, 201)
        entry = self._raw()["bare"]
        self.assertEqual(entry, {"paths": [str(self.repo)], "base_branch": "main"})
        # GET 时仍回填默认值
        d = self.client.get("/api/projects/bare").get_json()
        self.assertEqual(d["git"], GIT_DEFAULTS)
        self.assertEqual(d["git_sync"], SYNC_DEFAULTS)

    def test_p3_post_legacy_booleans_write_new_enums(self):
        self.assertEqual(self._post(name="legacy",
                                    git_sync={"fetch_before": True,
                                              "push_after": True}).status_code, 201)
        sync = self._raw()["legacy"]["git_sync"]
        self.assertEqual(sync, {"before_card": "pull", "on_done": "auto",
                                "diverge": "overlap-check", "timeout": 120})
        self.assertNotIn("fetch_before", sync)
        self.assertNotIn("push_after", sync)

    def test_p4_post_preserves_existing_entries(self):
        before = self._raw()["p1"]
        self.assertEqual(self._post(name="fresh", git={"remote": "upstream"}).status_code, 201)
        self.assertEqual(self._raw()["p1"], before)     # 已有条目（含未知字段）原样保留

    def test_p5_post_rejection_matrix_zero_write(self):
        cases = [
            ("git 非对象", dict(git="x")),
            ("git 未知字段", dict(git={"token": "x"})),
            ("git.remote 空", dict(git={"remote": ""})),
            ("git.remote 空白", dict(git={"remote": "a b"})),
            ("git.remote 以 - 开头", dict(git={"remote": "-x"})),
            ("git.remote 超长", dict(git={"remote": "r" * 129})),
            ("git.url 非串", dict(git={"url": 3})),
            ("git.url 超长", dict(git={"url": "u" * 2049})),
            ("git.url 内嵌密码", dict(git={"url": "https://u:secret@example.com/a.git"})),
            ("git.credential 非法", dict(git={"credential": "oauth"})),
            ("git.ssh_key 非串", dict(git={"ssh_key": 3})),
            ("worktree 非串", dict(worktree=3)),
            ("worktree 非 per-requirement", dict(worktree="shared")),
            ("git_sync 非对象", dict(git_sync="x")),
            ("git_sync 未知字段", dict(git_sync={"remote": "origin"})),
            ("before_card 非法", dict(git_sync={"before_card": "always"})),
            ("on_done 非法", dict(git_sync={"on_done": "later"})),
            ("diverge 非法", dict(git_sync={"diverge": "merge"})),
            ("timeout 非整数", dict(git_sync={"timeout": "120"})),
            ("timeout 布尔", dict(git_sync={"timeout": True})),
            ("timeout 过小", dict(git_sync={"timeout": 0})),
            ("timeout 过大", dict(git_sync={"timeout": 3601})),
            ("fetch_before 非布尔", dict(git_sync={"fetch_before": "yes"})),
            ("push_after 非布尔", dict(git_sync={"push_after": 1})),
        ]
        for label, kw in cases:
            before_sig, before_bytes = self._sig(), self.pj.read_bytes()
            resp = self._post(name="bad", **kw)
            self.assertEqual(resp.status_code, 400, label)
            self.assertIn("error", resp.get_json(), label)
            self._assert_zero_write(before_sig, before_bytes)


class PatchConfigTest(GitConfigBase):
    def _patch(self, body):
        return self.client.patch("/api/projects/p1", json=body)

    def test_e1_patch_only_submitted_key_changes(self):
        self._write({"p1": {"paths": [str(self.repo)],
                            "git": {"remote": "upstream", "url": "https://a/b.git",
                                    "credential": "token", "ssh_key": ""},
                            "git_sync": {"before_card": "pull", "on_done": "auto",
                                         "diverge": "stop", "timeout": 90},
                            "company": False, "models": {"dev": "m1"}}})
        resp = self._patch({"goal": "新目标"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["changed"])
        raw = self._raw()["p1"]
        self.assertEqual(raw["goal"], "新目标")
        self.assertEqual(raw["git"]["remote"], "upstream")      # 未提交的段不动
        self.assertEqual(raw["git_sync"]["timeout"], 90)
        self.assertEqual(raw["company"], False)                 # 未知字段保留
        self.assertEqual(raw["models"], {"dev": "m1"})

    def test_e2_patch_git_whole_replace_with_defaults(self):
        resp = self._patch({"git": {"url": "https://example.com/x.git"}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._raw()["p1"]["git"],
                         {"remote": "origin", "url": "https://example.com/x.git",
                          "credential": "system", "ssh_key": ""})

    def test_e3_patch_git_sync_whole_replace_and_legacy_fold(self):
        resp = self._patch({"git_sync": {"before_card": "fetch"}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._raw()["p1"]["git_sync"],
                         {"before_card": "fetch", "on_done": "off",
                          "diverge": "overlap-check", "timeout": 120})
        resp = self._patch({"git_sync": {"fetch_before": True, "push_after": True}})
        self.assertEqual(resp.status_code, 200)
        sync = self._raw()["p1"]["git_sync"]
        self.assertEqual(sync, {"before_card": "pull", "on_done": "auto",
                                "diverge": "overlap-check", "timeout": 120})
        self.assertNotIn("fetch_before", sync)

    def test_e4_patch_worktree_and_idempotent(self):
        resp = self._patch({"worktree": "per-requirement"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._raw()["p1"]["worktree"], "per-requirement")
        self._bak().unlink(missing_ok=True)                     # 首次写入的 .bak 清掉再验幂等
        before_sig, before_bytes = self._sig(), self.pj.read_bytes()
        resp = self._patch({"worktree": "per-requirement"})   # 同值 → 不写盘
        self.assertEqual(resp.get_json()["changed"], False)
        self._assert_zero_write(before_sig, before_bytes)

    def test_e5_patch_preserves_unknown_fields(self):
        before = self._raw()["p1"]
        resp = self._patch({"git": {"credential": "ssh"},
                            "git_sync": {"on_done": "manual"}})
        self.assertEqual(resp.status_code, 200)
        raw = self._raw()["p1"]
        for key in ("company", "models", "x_custom", "goal"):
            self.assertEqual(raw[key], before[key], key)

    def test_e6_patch_rejection_matrix_zero_write(self):
        cases = [
            ("无字段", {}),
            ("仅未知字段", {"name": "x"}),
            ("git 非对象", {"git": []}),
            ("git 未知字段", {"git": {"foo": 1}}),
            ("git.url 内嵌密码", {"git": {"url": "https://u:p@h/r.git"}}),
            ("worktree 非法", {"worktree": "shared"}),
            ("git_sync 非对象", {"git_sync": []}),
            ("git_sync 枚举非法", {"git_sync": {"on_done": "later"}}),
            ("git_sync timeout 越界", {"git_sync": {"timeout": 9999}}),
        ]
        for label, body in cases:
            before_sig, before_bytes = self._sig(), self.pj.read_bytes()
            resp = self._patch(body)
            self.assertEqual(resp.status_code, 400, label)
            self.assertIn("error", resp.get_json(), label)
            self._assert_zero_write(before_sig, before_bytes)

    def test_e7_patch_unknown_project_and_bad_file(self):
        self.assertEqual(self.client.patch("/api/projects/ghost",
                                           json={"git": {"remote": "upstream"}}).status_code, 404)
        self.pj.write_text("{bad", encoding="utf-8")
        before_bytes = self.pj.read_bytes()
        self.assertEqual(self._patch({"git": {"remote": "upstream"}}).status_code, 400)
        self.assertEqual(self.pj.read_bytes(), before_bytes)


if __name__ == "__main__":
    unittest.main()
