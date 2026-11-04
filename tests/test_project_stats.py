#!/usr/bin/env python3
"""#3 项目信息条统计接口测试：GET /api/project-stats 口径与 fail-safe。

A 组：统计口径（total/doing/done/dropped/success_rate/reject/ctx_tokens）、all 聚合、
      goal/paths 透传、未知项目/空库/DB 异常零值、参数哨兵、打回 JOIN 语义、ctx NULL；
B 组：/api/auto-projects 输出等价 + _auto_projects_config fail-safe。

隔离：临时 DB（沿 tests/test_status_log.py 先例），不碰真实 data/board.db；
      projects.json 读取一律 mock（软链穿透红线，绝不写文件）；用例各自使用独立项目名，
      不依赖执行顺序与其它用例的累计数据。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_project_stats.py -q
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

import board as b  # noqa: E402


class ProjectStatsTest(unittest.TestCase):
    """A1~A12：统计口径与降级（临时 DB 隔离）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-project-stats-"))
        cls._orig_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _insert_req(self, project, status, title="t"):
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO requirements (title, project, status, notes) VALUES (?, ?, ?, '')",
                (title, project, status))
            conn.commit()
            return cur.lastrowid

    def _add_review(self, req_id, verdict):
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO req_reviews (req_id, round, reviewer, verdict, content) "
                "VALUES (?, 1, 'r', ?, 'x')", (req_id, verdict))
            conn.commit()

    def _add_run(self, req_id, context_tokens):
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO agent_runs (req_id, role, context_tokens) VALUES (?, 'dev', ?)",
                (req_id, context_tokens))
            conn.commit()

    def _stats(self, project=None):
        url = "/api/project-stats" + ("" if project is None else "?project=" + project)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_a1_single_project_full_metrics(self):
        """A1：单项目全口径——5 状态计数 + 成功率 + reject JOIN + ctx SUM 精确值"""
        ids = []
        for status in ("later", "open", "doing", "doing", "done", "done", "done", "dropped"):
            ids.append(self._insert_req("pa1", status))
        self._add_review(ids[4], "reject")
        self._add_review(ids[5], "reject")
        self._add_review(ids[6], "pass")          # 非 reject 不计
        self._add_run(ids[4], 1000)
        self._add_run(ids[5], None)               # NULL 按 0（SUM 不炸）
        self._add_run(ids[6], 234)
        with mock.patch.object(b, "_auto_projects_config",
                               return_value={"pa1": {"paths": ["/p"], "goal": "GA"}}):
            d = self._stats("pa1")
        self.assertEqual(d["project"], "pa1")
        self.assertEqual(d["stats"], {"total": 8, "doing": 2, "done": 3, "dropped": 1,
                                      "success_rate": 0.75, "reject": 2, "ctx_tokens": 1234})

    def test_a2_success_rate_zero_denominator(self):
        """A2：无 done/dropped 项目 → success_rate 为 None（前端显示 —）"""
        self._insert_req("pbeta", "later")
        self._insert_req("pbeta", "open")
        with mock.patch.object(b, "_auto_projects_config", return_value={}):
            d = self._stats("pbeta")
        self.assertIsNone(d["stats"]["success_rate"])
        self.assertEqual(d["stats"]["total"], 2)

    def test_a3_dropped_in_denominator(self):
        """A3：已废弃计入分母——done=1,dropped=1 → 0.5"""
        self._insert_req("pgamma", "done")
        self._insert_req("pgamma", "dropped")
        with mock.patch.object(b, "_auto_projects_config", return_value={}):
            d = self._stats("pgamma")
        self.assertEqual(d["stats"]["success_rate"], 0.5)

    def test_a4_all_aggregate(self):
        """A4：all 聚合=全库（含 projects.json 之外项目卡）+ project_count=mock 配置键数；
        聚合值与全库 SQL 直查一致；缺省参数与 all 等价"""
        self._insert_req("zz-ghost-not-in-config", "done")
        self._insert_req("zz-ghost-not-in-config", "later")
        with b.get_db() as conn:
            expected_total = conn.execute("SELECT COUNT(*) FROM requirements").fetchone()[0]
            expected_reject = conn.execute(
                "SELECT COUNT(*) FROM req_reviews rv JOIN requirements r ON r.id = rv.req_id "
                "WHERE rv.verdict = 'reject'").fetchone()[0]
            expected_ctx = conn.execute(
                "SELECT COALESCE(SUM(ar.context_tokens), 0) FROM agent_runs ar "
                "JOIN requirements r ON r.id = ar.req_id").fetchone()[0]
        with mock.patch.object(b, "_auto_projects_config",
                               return_value={"alpha": {}, "beta": {}}):
            d_all = self._stats("all")
            d_default = self._stats()
        self.assertEqual(d_all["project"], "all")
        self.assertEqual(d_all["project_count"], 2)
        self.assertEqual(d_all["paths"], [])
        self.assertEqual(d_all["stats"]["total"], expected_total)
        self.assertGreaterEqual(d_all["stats"]["total"], 2)    # 含配置外项目卡（ghost）
        self.assertEqual(d_all["stats"]["reject"], expected_reject)
        self.assertEqual(d_all["stats"]["ctx_tokens"], int(expected_ctx))
        self.assertEqual(d_all, d_default)

    def test_a5_unknown_project_zero(self):
        """A5：未知项目名 → 零值 + goal="" + paths=[] + project_count=None + 200"""
        with mock.patch.object(b, "_auto_projects_config", return_value={"alpha": {}}):
            d = self._stats("no-such-project")
        self.assertEqual(d, {"project": "no-such-project", "goal": "", "paths": [],
                             "project_count": None, "stats": b._stats_zero()})

    def test_a7_goal_paths_passthrough(self):
        """A7：goal/paths 实时透传；缺 goal → ""；缺 paths → []"""
        cfg = {"alpha": {"paths": ["/p1", "/p2"], "goal": "目标A"},
               "delta": {"paths": [], "goal": ""}}
        with mock.patch.object(b, "_auto_projects_config", return_value=cfg):
            d1 = self._stats("alpha")
            d2 = self._stats("delta")
        self.assertEqual(d1["goal"], "目标A")
        self.assertEqual(d1["paths"], ["/p1", "/p2"])
        self.assertEqual(d2["goal"], "")
        self.assertEqual(d2["paths"], [])

    def test_a8_db_error_zero_fallback(self):
        """A8：DB 异常 → 200 零值（不 500）；projects.json 字段仍透传"""
        with mock.patch.object(b, "_auto_projects_config",
                               return_value={"alpha": {"paths": ["/p"], "goal": "G"}}), \
                mock.patch.object(b, "get_db", side_effect=RuntimeError("db gone")):
            d = self._stats("alpha")
        self.assertEqual(d["stats"], b._stats_zero())
        self.assertEqual(d["goal"], "G")
        self.assertEqual(d["paths"], ["/p"])

    def test_a9_reject_join_semantics(self):
        """A9：孤儿 req_reviews（req 不存在）不计；pass/close 非 reject 不计"""
        rid = self._insert_req("pa9", "open")
        self._add_review(rid, "reject")
        self._add_review(rid, "pass")
        self._add_review(rid, "close")
        with b.get_db() as conn:   # 孤儿评审（req_id=999999 不存在）
            conn.execute("INSERT INTO req_reviews (req_id, round, reviewer, verdict, content) "
                         "VALUES (999999, 1, 'r', 'reject', 'x')")
            conn.commit()
        with mock.patch.object(b, "_auto_projects_config", return_value={}):
            d = self._stats("pa9")
        self.assertEqual(d["stats"]["reject"], 1)

    def test_a10_ctx_null_and_orphan_runs(self):
        """A10：context_tokens 为 NULL 按 0 合计；孤儿 agent_runs 不计入项目口径"""
        rid = self._insert_req("pa10", "open")
        self._add_run(rid, None)
        with b.get_db() as conn:
            conn.execute("INSERT INTO agent_runs (req_id, role, context_tokens) "
                         "VALUES (999999, 'dev', 55555)")
            conn.commit()
        with mock.patch.object(b, "_auto_projects_config", return_value={}):
            d = self._stats("pa10")
        self.assertEqual(d["stats"]["ctx_tokens"], 0)

    def test_a11_param_sentinel(self):
        """A11：缺省/空串 project 与 all 等价（全库聚合）"""
        with mock.patch.object(b, "_auto_projects_config", return_value={"x": {}}):
            d_none, d_empty, d_all = self._stats(), self._stats(""), self._stats("all")
        self.assertEqual(d_none["stats"], d_all["stats"])
        self.assertEqual(d_empty["stats"], d_all["stats"])
        self.assertEqual(d_none["project_count"], 1)

    def test_a12_empty_db_all_zero(self):
        """A6：空库 → 全零 200（fail-safe）"""
        tmp2 = Path(tempfile.mkdtemp(prefix="req-project-stats-empty-"))
        try:
            with mock.patch.object(b, "DB_PATH", tmp2 / "board.db"):
                b.init_db()
                with mock.patch.object(b, "_auto_projects_config", return_value={}):
                    d = self._stats("all")
            self.assertEqual(d["stats"], b._stats_zero())
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)


class AutoProjectsConfigTest(unittest.TestCase):
    """B 组：/api/auto-projects 输出等价 + _auto_projects_config fail-safe"""

    def test_b1_endpoint_outputs_config_keys(self):
        """B1：端点输出 = _auto_projects_config 键列表（顺序保持）"""
        cfg = {"p1": {"paths": [], "goal": ""}, "p2": {"paths": [], "goal": ""}}
        with mock.patch.object(b, "_auto_projects_config", return_value=cfg):
            resp = b.app.test_client().get("/api/auto-projects")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), ["p1", "p2"])

    def test_b2_endpoint_matches_real_projects_json(self):
        """B2：真实 projects.json 存在时，端点输出 == 文件键列表（重构前后逐字等价）"""
        pj = b.BASE_DIR / "projects.json"
        if not pj.exists():
            self.skipTest("projects.json 不存在（干净 checkout），跳过")
        raw = json.loads(pj.read_text())
        resp = b.app.test_client().get("/api/auto-projects")
        self.assertEqual(resp.get_json(), list(raw.keys()))

    def test_b3_config_fail_safe(self):
        """B3：_auto_projects_config 对 str 简写/非法 JSON/非对象/缺字段全部宽容降级
        （#5 起读 PROJECTS_FILE，隔离面同步钉该属性）"""
        tmp = Path(tempfile.mkdtemp(prefix="req-project-cfg-"))
        try:
            with mock.patch.object(b, "PROJECTS_FILE", tmp / "projects.json"):
                (tmp / "projects.json").write_text(json.dumps({
                    "s1": "/path/one",
                    "d1": {"paths": ["/a", "/b"], "goal": "G"},
                    "d2": {"path": "/legacy"},
                    "bad": 3,
                }))
                cfg = b._auto_projects_config()
                self.assertEqual(cfg["s1"], {"paths": ["/path/one"], "goal": ""})
                self.assertEqual(cfg["d1"], {"paths": ["/a", "/b"], "goal": "G"})
                self.assertEqual(cfg["d2"], {"paths": ["/legacy"], "goal": ""})
                self.assertEqual(cfg["bad"], {"paths": [], "goal": ""})
                (tmp / "projects.json").write_text("{not json")
                self.assertEqual(b._auto_projects_config(), {})
                (tmp / "projects.json").write_text(json.dumps([1, 2]))
                self.assertEqual(b._auto_projects_config(), {})
                (tmp / "projects.json").unlink()
                self.assertEqual(b._auto_projects_config(), {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
