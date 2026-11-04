#!/usr/bin/env python3
"""#5 项目管理端点测试（GET/POST/PATCH/DELETE /api/projects[/<name>]）。

A1~A16：新增成功与默认值 / 拒绝矩阵零改动 / 字段保留 / 编辑（goal·paths·base_branch、清空、
幂等）/ 移除（成功·doing 阻断·404）/ GET 预填 / 软链落点 / 原子性 / 并发 / board.db 零写入。

隔离：临时 DB + 临时 projects.json（b.PROJECTS_FILE 钉一份），PROJECT_PATHS 快照 setUp 保存、
cleanup 还原；绝不触真实 data/board.db 与真实 projects.json（软链穿透红线）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_project_admin_api.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402


class AdminApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req5-api-"))
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
        self.repo1 = self._git_dir(f"repo1-{self._testMethodName}")
        self.repo2 = self._git_dir(f"repo2-{self._testMethodName}")
        self._write({"p1": {"paths": [str(self.repo1)], "goal": "G1", "models": {"dev": "m1"}},
                     "toy": str(self.repo2)})
        b.PROJECT_PATHS = b._projects_config()

    def _restore(self):
        b.PROJECTS_FILE = self._orig_pj
        b.PROJECT_PATHS = self._orig_pp

    def _git_dir(self, name):
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        (d / ".git").mkdir(exist_ok=True)
        return d

    def _write(self, data):
        self.pj.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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

    def _req(self, project, status, title="t"):
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO requirements (title, project, status, notes) VALUES (?, ?, ?, '')",
                (title, project, status))
            conn.commit()
            return cur.lastrowid


class PostCreateTest(AdminApiBase):
    def test_a1_post_success(self):
        before = self.pj.read_bytes()
        resp = self.client.post("/api/projects", json={
            "name": "new-proj", "paths": [str(self.repo1), str(self.repo2)],
            "base_branch": "main", "goal": "目标 G"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json(), {"ok": True, "project": "new-proj", "created": True})
        raw = self._raw()
        self.assertEqual(raw["new-proj"], {"paths": [str(self.repo1), str(self.repo2)],
                                          "base_branch": "main", "goal": "目标 G"})
        self.assertEqual(raw["p1"], {"paths": [str(self.repo1)], "goal": "G1", "models": {"dev": "m1"}})
        self.assertEqual(raw["toy"], {"paths": [str(self.repo2)]})        # str 简写归一为 paths（沿 #4 口径）
        self.assertEqual(self._bak().read_bytes(), before)                # .bak = 写前字节
        self.assertFalse((self.pj.parent / (self.pj.name + ".tmp")).exists())
        self.assertIn("new-proj", self.client.get("/api/auto-projects").get_json())
        self.assertIn("new-proj", b.PROJECT_PATHS)                        # 快照刷新（建卡对话即时可用）

    def test_a2_post_defaults(self):
        resp = self.client.post("/api/projects", json={"name": "no-branch", "paths": [str(self.repo1)]})
        self.assertEqual(resp.status_code, 201)
        raw = self._raw()
        self.assertEqual(raw["no-branch"], {"paths": [str(self.repo1)], "base_branch": "main"})
        self.assertNotIn("goal", raw["no-branch"])                        # 缺 goal → 不写键
        self.assertEqual(list(raw)[-1], "no-branch")                      # 新键追加键序尾部
        resp = self.client.post("/api/projects", json={
            "name": "empty-branch", "paths": [str(self.repo1)], "base_branch": "", "goal": ""})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self._raw()["empty-branch"],
                         {"paths": [str(self.repo1)], "base_branch": "main"})

    def test_a3_post_rejection_matrix(self):
        good = {"name": "ok-name", "paths": [str(self.repo1)]}
        nogit = self.tmp / "no-git-dir"
        nogit.mkdir(exist_ok=True)
        cases = [
            ("name 缺失", {k: v for k, v in good.items() if k != "name"}, 400),
            ("name 非字符串", dict(good, name=3), 400),
            ("name 空", dict(good, name="  "), 400),
            ("name 大写", dict(good, name="MyProj"), 400),
            ("name 下划线", dict(good, name="my_proj"), 400),
            ("name 中文", dict(good, name="项目"), 400),
            ("name 超 64", dict(good, name="a" * 65), 400),
            ("重名 409", dict(good, name="p1"), 409),
            ("paths 非列表", dict(good, paths=str(self.repo1)), 400),
            ("paths 空列表", dict(good, paths=[]), 400),
            ("paths 元素非字符串", dict(good, paths=[123]), 400),
            ("paths 元素空串", dict(good, paths=[""]), 400),
            ("paths 相对路径", dict(good, paths=["relative/path"]), 400),
            ("paths 不存在", dict(good, paths=[str(self.tmp / "ghost-dir")]), 400),
            ("paths 指向文件", dict(good, paths=[str(self.pj)]), 400),
            ("paths 无 .git", dict(good, paths=[str(nogit)]), 400),
            ("base_branch 非字符串", dict(good, base_branch={"a": "b"}), 400),
            ("base_branch 空白", dict(good, base_branch="  "), 400),
            ("base_branch 含控制字符", dict(good, base_branch="a\x01b"), 400),
            ("base_branch 超 128", dict(good, base_branch="x" * 129), 400),
            ("goal 非字符串", dict(good, goal=3), 400),
            ("goal 超 500", dict(good, goal="g" * 501), 400),
            ("goal 含控制字符", dict(good, goal="a\x01b"), 400),
        ]
        for label, body, want in cases:
            before, before_bytes = self._sig(), self.pj.read_bytes()
            resp = self.client.post("/api/projects", json=body)
            self.assertEqual(resp.status_code, want, label)
            self.assertIn("error", resp.get_json(), label)
            self._assert_zero_write(before, before_bytes)
        resp = self.client.post("/api/projects", json=["x"])              # body 非对象
        self.assertEqual(resp.status_code, 400)
        self.pj.write_text("{bad", encoding="utf-8")                      # 坏 JSON → 400 零写入
        before_bytes = self.pj.read_bytes()
        self.assertEqual(self.client.post("/api/projects", json=good).status_code, 400)
        self.assertEqual(self.pj.read_bytes(), before_bytes)
        self.pj.unlink()                                                  # 文件缺失 → 404
        self.assertEqual(self.client.post("/api/projects", json=good).status_code, 404)

    def test_a4_field_preservation(self):
        data = {"p1": {"paths": [str(self.repo1)], "goal": "G1", "x_custom": {"a": [1, 2], "b": None}},
                "toy": str(self.repo2), "other": {"paths": [str(self.repo1)], "unknown": "keep"}}
        self._write(data)
        resp = self.client.post("/api/projects", json={"name": "fresh", "paths": [str(self.repo1)]})
        self.assertEqual(resp.status_code, 201)
        raw = self._raw()
        self.assertEqual(raw["p1"], data["p1"])
        self.assertEqual(raw["other"], data["other"])
        self.assertEqual(raw["toy"], {"paths": [str(self.repo2)]})        # str 简写条目归一（沿 #4 口径）
        self.assertEqual(set(raw), set(data) | {"fresh"})


class PatchEditTest(AdminApiBase):
    def test_a5_patch_goal(self):
        before = self.pj.read_bytes()
        resp = self.client.patch("/api/projects/p1", json={"goal": "新目标"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "project": "p1", "changed": True})
        raw = self._raw()
        self.assertEqual(raw["p1"]["goal"], "新目标")
        self.assertEqual(raw["p1"]["paths"], [str(self.repo1)])
        self.assertEqual(raw["p1"]["models"], {"dev": "m1"})
        self.assertEqual(self._bak().read_bytes(), before)                # .bak 生成

    def test_a6_patch_fields_and_goal_clear(self):
        self._write({"p1": {"paths": [str(self.repo1)], "base_branch": "main", "goal": "G",
                            "models": {"dev": "m"}}})
        resp = self.client.patch("/api/projects/p1", json={"paths": [str(self.repo2)], "base_branch": "master"})
        self.assertEqual(resp.status_code, 200)
        raw = self._raw()
        self.assertEqual(raw["p1"], {"paths": [str(self.repo2)], "base_branch": "master",
                                     "goal": "G", "models": {"dev": "m"}})
        resp = self.client.patch("/api/projects/p1", json={"goal": ""})   # 显式空串 = 清空该键
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["changed"])
        raw = self._raw()
        self.assertNotIn("goal", raw["p1"])
        self.assertEqual(raw["p1"]["paths"], [str(self.repo2)])           # 未提交字段不动
        self.assertEqual(raw["p1"]["models"], {"dev": "m"})

    def test_a7_patch_idempotent(self):
        resp = self.client.patch("/api/projects/p1", json={"goal": "G1"})  # 同值 → 无变化
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        before, sig = self.pj.read_bytes(), self._sig()
        self.assertFalse(self._bak().exists())
        # str 简写条目同值 PATCH：归一后比较相等 → 不写盘（不强制归一）
        resp = self.client.patch("/api/projects/toy", json={"paths": [str(self.repo2)]})
        self.assertEqual(resp.get_json()["changed"], False)
        self.assertEqual(self.pj.read_bytes(), before)
        self.assertEqual(self._sig(), sig)
        self.assertFalse(self._bak().exists())

    def test_a8_patch_rejection_matrix(self):
        cases = [
            ("未知项目", "ghost", {"goal": "x"}, 404),
            ("无字段", "p1", {}, 400),
            ("仅未知字段（name 不可改）", "p1", {"name": "x"}, 400),
            ("paths 空列表", "p1", {"paths": []}, 400),
            ("paths 非列表", "p1", {"paths": "x"}, 400),
            ("base_branch 空串", "p1", {"base_branch": ""}, 400),
            ("base_branch 非串", "p1", {"base_branch": 3}, 400),
            ("base_branch dict 映射形态", "p1", {"base_branch": {"a": "b"}}, 400),
            ("goal 非串", "p1", {"goal": 3}, 400),
            ("goal 超 500", "p1", {"goal": "g" * 501}, 400),
            ("body 非对象", "p1", ["x"], 400),
        ]
        for label, project, body, want in cases:
            before, before_bytes = self._sig(), self.pj.read_bytes()
            resp = self.client.patch(f"/api/projects/{project}", json=body)
            self.assertEqual(resp.status_code, want, label)
            self.assertIn("error", resp.get_json(), label)
            self._assert_zero_write(before, before_bytes)
        self.pj.write_text("{bad", encoding="utf-8")
        before_bytes = self.pj.read_bytes()
        self.assertEqual(self.client.patch("/api/projects/p1", json={"goal": "x"}).status_code, 400)
        self.assertEqual(self.pj.read_bytes(), before_bytes)
        self._write({"p1": 3})                                            # 条目结构不符 → 400
        self.assertEqual(self.client.patch("/api/projects/p1", json={"goal": "x"}).status_code, 400)
        self.pj.unlink()
        self.assertEqual(self.client.patch("/api/projects/p1", json={"goal": "x"}).status_code, 404)


class DeleteRemoveTest(AdminApiBase):
    def test_a9_delete_and_reattach(self):
        req_id = self._req("toy", "done")
        before = self.pj.read_bytes()
        resp = self.client.delete("/api/projects/toy")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "project": "toy", "removed": True})
        self.assertNotIn("toy", self._raw())
        self.assertNotIn("toy", self.client.get("/api/auto-projects").get_json())
        self.assertNotIn("toy", b.PROJECT_PATHS)                          # 快照刷新：删除后失效
        self.assertEqual(self._bak().read_bytes(), before)
        with b.get_db() as conn:                                          # 历史卡数据零删除
            row = conn.execute("SELECT status FROM requirements WHERE id = ?", (req_id,)).fetchone()
        self.assertEqual(row["status"], "done")
        resp = self.client.post("/api/projects", json={                   # 重新接入同名 → 完整恢复
            "name": "toy", "paths": [str(self.repo2)], "goal": "回归目标"})
        self.assertEqual(resp.status_code, 201)
        self.assertIn("toy", self.client.get("/api/auto-projects").get_json())
        self.assertIn("toy", b.PROJECT_PATHS)
        with b.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM requirements WHERE project = 'toy'").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_a10_delete_doing_block(self):
        for status in ("open", "later", "done", "dropped"):
            self._req("p1", status)
        self._req("p1", "doing")
        before, before_bytes = self._sig(), self.pj.read_bytes()
        resp = self.client.delete("/api/projects/p1")
        self.assertEqual(resp.status_code, 409)
        d = resp.get_json()
        self.assertEqual(d["doing"], 1)
        self.assertIn("error", d)
        self._assert_zero_write(before, before_bytes)
        with b.get_db() as conn:                                          # doing 清零后放行
            conn.execute("UPDATE requirements SET status='done' WHERE project='p1' AND status='doing'")
            conn.commit()
        self.assertEqual(self.client.delete("/api/projects/p1").status_code, 200)

    def test_a11_delete_errors(self):
        self.assertEqual(self.client.delete("/api/projects/ghost").status_code, 404)
        # 未注册但存在 doing 卡：先判存在性 → 404（不误报 doing 阻断；与其他端点口径一致、零写入）
        self._req("ghost-doing", "doing")
        before_bytes = self.pj.read_bytes()
        self.assertEqual(self.client.delete("/api/projects/ghost-doing").status_code, 404)
        self.assertEqual(self.pj.read_bytes(), before_bytes)
        self.assertEqual(self.client.delete("/api/projects/toy").status_code, 200)
        self.assertEqual(self.client.delete("/api/projects/toy").status_code, 404)   # 重复删除
        self.pj.write_text("{bad", encoding="utf-8")
        before_bytes = self.pj.read_bytes()
        self.assertEqual(self.client.delete("/api/projects/p1").status_code, 400)
        self.assertEqual(self.pj.read_bytes(), before_bytes)
        self.pj.unlink()
        self.assertEqual(self.client.delete("/api/projects/p1").status_code, 404)


class GetDetailTest(AdminApiBase):
    def test_a12_get_prefill(self):
        resp = self.client.get("/api/projects/p1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "project": "p1", "paths": [str(self.repo1)],
            "base_branch": "", "goal": "G1",
            # v1 项目级 Git 配置：GET 恒返回归一后的三段（含默认值）
            "git": {"remote": "origin", "url": "", "credential": "system", "ssh_key": ""},
            "worktree": "per-requirement",
            "git_sync": {"before_card": "off", "on_done": "off",
                         "diverge": "overlap-check", "timeout": 120}})
        d = self.client.get("/api/projects/toy").get_json()               # str 简写归一为 paths
        self.assertEqual((d["paths"], d["base_branch"], d["goal"]), ([str(self.repo2)], "", ""))
        self._write({"p1": {"paths": [str(self.repo1)], "base_branch": {"<repo>": "main"}, "goal": "G"}})
        d = self.client.get("/api/projects/p1").get_json()                # dict 映射形态原样透传
        self.assertEqual(d["base_branch"], {"<repo>": "main"})
        self._write({"p1": {"path": str(self.repo1)}})                    # path 单值形态兼容
        self.assertEqual(self.client.get("/api/projects/p1").get_json()["paths"], [str(self.repo1)])
        self._write({"p1": {"paths": [str(self.repo1)], "goal": 3}})       # goal 非串 → ""
        self.assertEqual(self.client.get("/api/projects/p1").get_json()["goal"], "")
        self.assertEqual(self.client.get("/api/projects/ghost").status_code, 404)
        self.pj.write_text("{bad", encoding="utf-8")
        self.assertEqual(self.client.get("/api/projects/p1").status_code, 400)
        self.pj.write_text(json.dumps({"p1": 3}), encoding="utf-8")       # 条目结构不符 → 400
        self.assertEqual(self.client.get("/api/projects/p1").status_code, 400)
        self.pj.write_text(json.dumps({"p1": None}), encoding="utf-8")
        self.assertEqual(self.client.get("/api/projects/p1").status_code, 400)
        self.pj.unlink()
        self.assertEqual(self.client.get("/api/projects/p1").status_code, 404)


class WritePathSafetyTest(AdminApiBase):
    def test_a13_symlink_target(self):
        real_dir = self.tmp / "a13-real"
        link_dir = self.tmp / "a13-link"
        real_dir.mkdir()
        link_dir.mkdir()
        real = real_dir / "projects.json"
        link = link_dir / "projects.json"
        initial = {"p1": {"paths": [str(self.repo1)], "goal": "G"}}
        real.write_text(json.dumps(initial), encoding="utf-8")
        os.symlink(real, link)
        b.PROJECTS_FILE = link
        resp = self.client.post("/api/projects", json={"name": "via-link", "paths": [str(self.repo1)]})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(os.path.islink(link))                             # 软链保持原样
        self.assertEqual(os.readlink(link), str(real))
        raw = json.loads(real.read_text(encoding="utf-8"))
        self.assertIn("via-link", raw)                                    # 真实文件被写
        self.assertEqual((real_dir / "projects.json.bak").read_text(), json.dumps(initial))
        self.assertEqual(sorted(p.name for p in link_dir.iterdir()), ["projects.json"])

    def test_a14_atomicity(self):
        before = self.pj.read_bytes()
        with mock.patch.object(os, "replace", side_effect=OSError("disk boom")):
            resp = self.client.post("/api/projects", json={"name": "boom", "paths": [str(self.repo1)]})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.pj.read_bytes(), before)                    # 原文件不动
        self.assertFalse((self.pj.parent / (self.pj.name + ".tmp")).exists())

    def test_a15_concurrent_writes(self):
        results = []

        def worker(name, goal):
            with b.app.test_client() as c:
                r = c.post("/api/projects", json={"name": name, "paths": [str(self.repo1)], "goal": goal})
                results.append((name, r.status_code))

        threads = [threading.Thread(target=worker, args=("conc-a", "A")),
                   threading.Thread(target=worker, args=("conc-b", "B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(code for _, code in results), [201, 201])  # 写锁串行：两变更并存
        raw = self._raw()
        self.assertEqual(raw["conc-a"]["goal"], "A")
        self.assertEqual(raw["conc-b"]["goal"], "B")
        results2 = []

        def same_worker():
            with b.app.test_client() as c:
                results2.append(c.post("/api/projects",
                                       json={"name": "same-name",
                                             "paths": [str(self.repo1)]}).status_code)

        threads = [threading.Thread(target=same_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results2), [201, 409])                    # 同名并发恰一成功
        self.assertIn("same-name", self._raw())
        results3 = []

        def patch_worker(field, value):
            with b.app.test_client() as c:
                results3.append(c.patch("/api/projects/p1", json={field: value}).status_code)

        threads = [threading.Thread(target=patch_worker, args=("goal", "PA")),
                   threading.Thread(target=patch_worker, args=("base_branch", "PB"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results3), [200, 200])
        raw = self._raw()
        self.assertEqual(raw["p1"]["goal"], "PA")
        self.assertEqual(raw["p1"]["base_branch"], "PB")

    def test_a16_board_db_zero_write(self):
        def snapshot():
            with b.get_db() as conn:
                counts = {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
                          for t in ("requirements", "req_reviews", "agent_sessions", "chat_sessions")}
                schema = [tuple(r) for r in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()]
            return counts, schema

        self._req("p1", "later")
        before = snapshot()
        self.client.post("/api/projects", json={"name": "dbtest", "paths": [str(self.repo1)]})
        self.client.patch("/api/projects/p1", json={"goal": "x"})
        self.client.get("/api/projects/p1")
        self.client.delete("/api/projects/dbtest")
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
