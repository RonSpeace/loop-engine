#!/usr/bin/env python3
"""req-103 会话运行记录落库测试：dispatcher 收尾写记录（post_run_record）L0 单测 + board runs API 测试。

改动：新增 agent_runs 表（只增不改）与 POST /api/requirements/<id>/runs 端点；
run_agent 收尾统一写运行记录 + 明确结束事件；/usage 的 X/Y/Z 三值解析落库。

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试 setUpModule 自动补一份。
board.py 的 init_db 只在 __main__ 调用，import 不碰真实 data/board.db；测试用临时库。
"""
import json
import os
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

# 测试会替换 dispatcher 模块级函数；保存真实引用，tearDown 恢复防跨文件污染
_REAL_API = d.api
_REAL_POST_EVENT = d.post_event


class L0PostRunRecord(unittest.TestCase):
    """post_run_record：字段完整性、status 映射（done/timed_out/failed）、写库失败不阻断"""

    def setUp(self):
        self.calls = []
        self.events = []
        d.api = lambda method, path, body=None: self.calls.append((method, path, body)) or {"success": True}
        d.post_event = lambda rid, kind, content, session_id=None: self.events.append((kind, content, session_id))

    def tearDown(self):
        d.api = _REAL_API
        d.post_event = _REAL_POST_EVENT

    def _session(self, sid="session_abc"):
        s = mock.Mock()
        s.session_id = sid
        s._tool_calls = {"t1": "Read", "t2": "Bash", "t3": "Write"}
        return s

    def test_t1_done_record_fields_complete(self):
        """验收 1：收尾记录字段完整（起止时间/状态/工具数/token/百分比）"""
        usage = {"context_tokens": 60000, "max_tokens": 600000, "context_pct": "10%"}
        d.post_run_record({"id": 42}, "dev", self._session(), 1000.0, 2000.0, 0, False, usage)
        method, path, body = self.calls[0]
        self.assertEqual((method, path), ("POST", "/42/runs"))
        self.assertEqual(body["role"], "dev")
        self.assertEqual(body["session_id"], "session_abc")
        self.assertEqual(body["started_at"], 1000.0)
        self.assertEqual(body["ended_at"], 2000.0)
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["tool_count"], 3)
        self.assertEqual(body["context_pct"], 10.0)        # '10%' -> 数值 10.0
        self.assertEqual(body["context_tokens"], 60000)
        self.assertEqual(body["max_tokens"], 600000)
        # 结束事件带 session_id 与 status
        self.assertEqual(self.events, [("system", "运行结束（done）", "session_abc")])

    def test_t2_status_mapping(self):
        """超时 -> timed_out；异常/非零退出 -> failed（token 字段置空不阻断）"""
        d.post_run_record({"id": 42}, "dev", self._session(), 1.0, 2.0, 0, True, None)
        self.assertEqual(self.calls[-1][2]["status"], "timed_out")
        self.assertIsNone(self.calls[-1][2]["context_pct"])
        self.assertIsNone(self.calls[-1][2]["context_tokens"])
        self.assertIsNone(self.calls[-1][2]["max_tokens"])
        d.post_run_record({"id": 42}, "dev", self._session(), 1.0, 2.0, 1, False, None)
        self.assertEqual(self.calls[-1][2]["status"], "failed")

    def test_t3_write_failure_does_not_raise(self):
        """验收 2 兜底：写库失败仅警告，不阻断收尾（对齐 post_event 容错）"""
        d.api = mock.Mock(side_effect=RuntimeError("board down"))
        d.post_run_record({"id": 42}, "dev", self._session(), 1.0, 2.0, 0, False,
                          {"context_tokens": 1, "max_tokens": 2, "context_pct": "1%"})
        # 不抛异常即为通过

    def test_t4_no_session_none_fields(self):
        """会话建立前失败：session_id 为 None、工具数 0，仍写记录"""
        d.post_run_record({"id": 42}, "analysis", None, 1.0, 2.0, 1, False, None)
        body = self.calls[-1][2]
        self.assertIsNone(body["session_id"])
        self.assertEqual(body["tool_count"], 0)
        self.assertIsNone(body["context_pct"])


class BoardRunsApiTest(unittest.TestCase):
    """board.py runs 端点：建表 + 写入 + 只增不改（临时库，不碰真实 data/board.db）"""

    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b
        cls.tmp = tempfile.mkdtemp(prefix="req103-board-")
        cls._orig_db = b.DB_PATH
        b.DB_PATH = Path(cls.tmp) / "test.db"
        b.init_db()
        b.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        cls.b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 同 class 共享临时库：每个用例前清空 agent_runs，保证断言独立
        with self.b.get_db() as conn:
            conn.execute("DELETE FROM agent_runs")
            conn.commit()

    def _post(self, rid=103, **fields):
        return self.b.app.test_client().post(f"/api/requirements/{rid}/runs", json=fields)

    def _query(self, sql, *args):
        with self.b.get_db() as conn:
            return conn.execute(sql, args).fetchall()

    def test_t1_insert_full_fields(self):
        """验收 1：POST 全字段 -> agent_runs 落一行完整记录"""
        resp = self._post(role="dev", session_id="session_x", started_at=1.5, ended_at=2.5,
                          status="done", tool_count=7, context_pct=10.0,
                          context_tokens=60000, max_tokens=600000)
        self.assertEqual(resp.status_code, 201)
        rows = self._query("SELECT * FROM agent_runs")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["req_id"], 103)
        self.assertEqual(r["role"], "dev")
        self.assertEqual(r["session_id"], "session_x")
        self.assertEqual(r["started_at"], 1.5)
        self.assertEqual(r["ended_at"], 2.5)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["tool_count"], 7)
        self.assertEqual(r["context_pct"], 10.0)
        self.assertEqual(r["context_tokens"], 60000)
        self.assertEqual(r["max_tokens"], 600000)

    def test_t2_missing_fields_null(self):
        """缺字段存 NULL，不报错（超时/异常路径 token 置空）"""
        resp = self._post(role="analysis", status="failed")
        self.assertEqual(resp.status_code, 201)
        r = self._query("SELECT * FROM agent_runs ORDER BY id DESC")[0]
        self.assertEqual(r["role"], "analysis")
        self.assertIsNone(r["session_id"])
        self.assertIsNone(r["started_at"])
        self.assertIsNone(r["ended_at"])
        self.assertIsNone(r["tool_count"])
        self.assertIsNone(r["context_pct"])

    def test_t3_append_only(self):
        """只增不改：多次写入各自成行，不覆盖"""
        self._post(role="dev", status="done", tool_count=1)
        self._post(role="dev", status="timed_out", tool_count=2)
        rows = self._query("SELECT status, tool_count FROM agent_runs ORDER BY id")
        self.assertEqual([(r["status"], r["tool_count"]) for r in rows],
                         [("done", 1), ("timed_out", 2)])

    def test_t4_parse_usage_to_pct_value(self):
        """pct_value：'10%' -> 10.0，空/异常 -> None（post_run_record 落库前置转换）"""
        self.assertEqual(d.pct_value("10%"), 10.0)
        self.assertEqual(d.pct_value("0.5%"), 0.5)
        self.assertIsNone(d.pct_value(""))
        self.assertIsNone(d.pct_value("abc"))
        self.assertIsNone(d.pct_value(None))


class CacheRunsApiTest(BoardRunsApiTest):
    """#42 缓存命中四字段：R1 全量落库读回 / R2 枚举校验 / R3 数值校验 / R4 raw 截断 /
    R5 旧客户端形态（无新字段）→ 201 且新列 NULL"""

    def test_r1_cache_fields_roundtrip(self):
        resp = self._post(role="dev", status="done", cached_tokens=120, input_tokens=1000,
                          cache_state="hit", usage_raw="raw-evidence")
        self.assertEqual(resp.status_code, 201)
        r = self._query("SELECT cached_tokens, input_tokens, cache_state, usage_raw "
                        "FROM agent_runs ORDER BY id DESC")[0]
        self.assertEqual((r["cached_tokens"], r["input_tokens"], r["cache_state"], r["usage_raw"]),
                         (120, 1000, "hit", "raw-evidence"))

    def test_r2_cache_state_enum_enforced(self):
        resp = self._post(role="dev", cache_state="bogus")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(self._query("SELECT id FROM agent_runs")), 0)   # 零写入
        self._post(role="dev", cache_state="unknown")                        # 缺省走正常行
        r = self._query("SELECT cache_state FROM agent_runs ORDER BY id DESC")[0]
        self.assertEqual(r["cache_state"], "unknown")
        self._post(role="dev")                                               # 缺省 → NULL
        self.assertIsNone(self._query("SELECT cache_state FROM agent_runs ORDER BY id DESC")[0]["cache_state"])

    def test_r3_negative_and_non_int_rejected(self):
        for bad in ({"cached_tokens": -1}, {"input_tokens": -5}, {"cached_tokens": 1.5},
                    {"input_tokens": "x"}, {"cached_tokens": True}):
            resp = self._post(role="dev", **bad)
            self.assertEqual(resp.status_code, 400, bad)
        self.assertEqual(len(self._query("SELECT id FROM agent_runs")), 0)   # 全部零写入

    def test_r4_usage_raw_truncated_to_2048(self):
        resp = self._post(role="dev", cache_state="unknown", usage_raw="x" * 3000)
        self.assertEqual(resp.status_code, 201)
        r = self._query("SELECT usage_raw FROM agent_runs")[0]
        self.assertEqual(len(r["usage_raw"]), 2048)

    def test_r5_old_client_without_new_fields(self):
        resp = self._post(role="dev", status="done")
        self.assertEqual(resp.status_code, 201)
        r = self._query("SELECT cached_tokens, input_tokens, cache_state, usage_raw FROM agent_runs")[0]
        self.assertEqual(tuple(r), (None, None, None, None))


if __name__ == "__main__":
    unittest.main()
