#!/usr/bin/env python3
"""req-421 会话活性状态行（B 组）：agent_sessions 活性心跳列 + 注册表/事件接口扩展。

覆盖 8 例：
- B1 迁移幂等：旧 schema 库（无 6 活性列）跑 init_db() → 6 列补齐、存量行保留且新列为 NULL；重复调用不报错
- B2 白名单落库：POST /api/agent-sessions 的 6 个 #421 字段按 id / 按 (req_id, role, session_id) 更新均不被静默丢弃
- B3 数值清洗与局部更新：非法心跳值（"abc"）不 500、落 NULL；只传部分字段不覆盖 session_id/status
- B4 长度截断：turn_step 截 20、turn_step_detail 截 48
- B5 server_now：GET /api/agent-sessions 每行 float、贴近服务端时钟、同一响应多行同值
- B6 req_status：卡 done → 行带 req_status='done'；需求行删除后会话行仍返回（LEFT JOIN）且 req_status=None
- B7 按需求查询：GET /api/requirements/<id>/sessions 同样带 server_now/req_status
- B8 事件 ts_ms：GET /api/requirements/<id>/events 每行 ts_ms 为 int 毫秒、与 ts（北京时间 +08:00）解析一致，历史行同样被补

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_agent_sessions_activity_api -v

隔离（#420）：临时目录 + monkeypatch board.DB_PATH，绝不写生产 data/board.db；本文件不拉起任何进程。
"""
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402

# #421 新增的 6 个活性列（迁移/白名单/截断断言共用）
ACTIVITY_COLS = ('turn_step', 'turn_step_detail', 'last_llm_request_at',
                 'last_llm_response_at', 'last_thought_at', 'last_tool_started_at')


class InitDbActivityMigrationTest(unittest.TestCase):
    """B1：init_db() 对 #421 之前的旧库做 ALTER 补列（数据保护：只加列不删表、存量行保留）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="req421-migrate-")
        self._orig_db = b.DB_PATH
        b.DB_PATH = Path(self.tmp) / "legacy.db"

    def tearDown(self):
        b.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_b1_migration_idempotent_keeps_rows(self):
        # 现场复刻：旧库的 agent_sessions 无 6 活性列，且已有存量行（迁移不得丢数据）
        conn = sqlite3.connect(str(b.DB_PATH))
        conn.execute("""
            CREATE TABLE agent_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                session_id TEXT,
                model TEXT,
                thinking TEXT,
                harness TEXT,
                context_pct TEXT,
                branch TEXT,
                workdir TEXT,
                poisoned INTEGER DEFAULT 0,
                status TEXT DEFAULT 'created',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO agent_sessions (req_id, role, session_id, status)"
                     " VALUES (7, 'dev', 's_legacy', 'idle')")
        conn.commit()
        conn.close()
        # 隔离红线（#420）：先确认临时库生效，避免迁移真跑在生产 data/board.db 上
        self.assertTrue(str(b.DB_PATH).startswith(self.tmp))

        b.init_db()
        b.init_db()   # 幂等：第二次调用不报错、不改写既有数据

        with b.get_db() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(agent_sessions)")}
            row = c.execute("SELECT * FROM agent_sessions WHERE session_id = 's_legacy'").fetchone()
        self.assertTrue(set(ACTIVITY_COLS) <= cols, f"缺列: {sorted(set(ACTIVITY_COLS) - cols)}")
        self.assertIsNotNone(row, "迁移不得丢存量行")
        self.assertEqual(row["req_id"], 7)
        self.assertEqual(row["status"], "idle")
        for col in ACTIVITY_COLS:
            self.assertIsNone(row[col], f"存量行 {col} 应为 NULL（前端 fail-safe：不计时、不判卡死）")


class AgentSessionsActivityApiTest(unittest.TestCase):
    """B2-B8：注册表 / 按需求会话 / 事件三个读接口的 #421 扩展（临时库 + Flask test client）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="req421-activity-")
        cls._orig_db = b.DB_PATH
        b.DB_PATH = Path(cls.tmp) / "test.db"
        b.init_db()
        b.app.config["TESTING"] = True
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            conn.execute("DELETE FROM agent_sessions")
            conn.execute("DELETE FROM agent_events")
            conn.execute("DELETE FROM requirements")
            conn.commit()
        self.req_id = self._new_req()

    # ---- 现场构造 ----
    def _new_req(self, status="later", title="活性测试"):
        resp = self.client.post("/api/requirements", json={
            "title": title, "project": "req-board", "status": status})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()["id"]

    def _new_session(self, req_id=None, role="dev", session_id=None, **fields):
        body = {"req_id": self.req_id if req_id is None else req_id, "role": role,
                "session_id": session_id or f"s_{uuid.uuid4().hex[:8]}"}
        body.update(fields)
        resp = self.client.post("/api/agent-sessions", json=body)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()["id"]

    def _upsert(self, body):
        return self.client.post("/api/agent-sessions", json=body)

    def _row(self, sid):
        with b.get_db() as conn:
            return conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (sid,)).fetchone()

    # ---- B2 ----
    def test_b2_activity_fields_roundtrip(self):
        """白名单（_AGENT_SESSION_FIELDS）漏加 = 心跳被静默丢弃：两条更新路径都要回读一致"""
        sid = self._new_session()
        now = time.time()
        payload = {"turn_step": "llm", "turn_step_detail": "Read board.py",
                   "last_llm_request_at": now, "last_llm_response_at": now - 1,
                   "last_thought_at": now - 2, "last_tool_started_at": now - 3}
        resp = self._upsert(dict(payload, id=sid))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["id"], sid)
        row = self._row(sid)
        for key, value in payload.items():
            self.assertEqual(row[key], value, f"{key} 未按入参落库（白名单缺失？）")
        # 第二条路径：不带 id，按 (req_id, role, session_id) 命中行更新
        resp2 = self._upsert({"req_id": self.req_id, "role": "dev",
                              "session_id": row["session_id"],
                              "turn_step": "tool", "last_tool_started_at": now - 9})
        self.assertEqual(resp2.status_code, 200, resp2.get_data(as_text=True))
        self.assertEqual(resp2.get_json()["id"], sid)
        row2 = self._row(sid)
        self.assertEqual(row2["turn_step"], "tool")
        self.assertEqual(row2["last_tool_started_at"], now - 9)

    # ---- B3 ----
    def test_b3_bad_value_to_null_and_partial_update(self):
        sid = self._new_session(status="running", session_id="s_keep")
        resp = self._upsert({"id": sid, "last_thought_at": "abc"})
        self.assertEqual(resp.status_code, 200, "非法心跳值不得 500")
        self.assertEqual(resp.get_json()["id"], sid)
        row = self._row(sid)
        self.assertIsNone(row["last_thought_at"], "非法值应清洗为 NULL")
        # 局部更新只作用于传入字段：session_id/status 不被清掉
        self.assertEqual(row["session_id"], "s_keep")
        self.assertEqual(row["status"], "running")
        # 空串 / 显式 null 同口径落 NULL
        for bad in ("", None):
            r2 = self._upsert({"id": sid, "last_llm_request_at": bad})
            self.assertEqual(r2.status_code, 200, f"{bad!r} 不得 500")
            self.assertIsNone(self._row(sid)["last_llm_request_at"])

    # ---- B4 ----
    def test_b4_turn_step_truncation(self):
        sid = self._new_session()
        resp = self._upsert({"id": sid, "turn_step": "l" * 25, "turn_step_detail": "d" * 60})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        row = self._row(sid)
        self.assertEqual(row["turn_step"], "l" * 20)
        self.assertEqual(row["turn_step_detail"], "d" * 48)

    # ---- B5 ----
    def test_b5_server_now(self):
        self._new_session(role="dev")
        self._new_session(role="accept")
        rows = self.client.get("/api/agent-sessions").get_json()
        self.assertEqual(len(rows), 2)
        now = time.time()
        for row in rows:
            self.assertIsInstance(row["server_now"], float)
            self.assertLess(abs(row["server_now"] - now), 2, "server_now 应为服务端时钟（epoch 秒）")
        self.assertEqual(rows[0]["server_now"], rows[1]["server_now"], "同一响应内 server_now 同值")

    # ---- B6 ----
    def test_b6_req_status_and_left_join(self):
        # 卡 done + 会话 idle：行附权威 req_status（收口 15s 轮询滞后窗口）
        done_id = self._new_req(status="done", title="已完成卡")
        sid = self._new_session(req_id=done_id)
        rows = self.client.get("/api/agent-sessions").get_json()
        row = [r for r in rows if r["id"] == sid][0]
        self.assertEqual(row["req_status"], "done")
        self.assertEqual(row["status"], "idle")
        # 需求行被删（直删表模拟数据缺失）：LEFT JOIN 不得丢会话行，req_status 为 None
        with b.get_db() as conn:
            conn.execute("DELETE FROM requirements WHERE id = ?", (done_id,))
            conn.commit()
        rows2 = self.client.get("/api/agent-sessions").get_json()
        kept = [r for r in rows2 if r["id"] == sid]
        self.assertEqual(len(kept), 1, "需求行删除后会话行仍应返回")
        self.assertIsNone(kept[0]["req_status"])

    # ---- B7 ----
    def test_b7_req_sessions_meta(self):
        sid = self._new_session()
        rows = self.client.get(f"/api/requirements/{self.req_id}/sessions").get_json()
        self.assertEqual([r["id"] for r in rows], [sid])
        self.assertIsInstance(rows[0]["server_now"], float)
        self.assertLess(abs(rows[0]["server_now"] - time.time()), 2)
        self.assertEqual(rows[0]["req_status"], "later")

    # ---- B8 ----
    def test_b8_events_ts_ms(self):
        # ① 接口写入行（ts = now_str，北京时间）
        resp = self.client.post(f"/api/requirements/{self.req_id}/events",
                                json={"events": [{"kind": "message", "content": "现场"}]})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        # ② 直插历史行（模拟 #421 之前的存量事件：ts 已是北京时间字符串、无 ts_ms 列）
        old_ts = "2026-01-02 03:04:05"
        with b.get_db() as conn:
            conn.execute("INSERT INTO agent_events (req_id, kind, content, ts)"
                         " VALUES (?, 'system', '历史行', ?)", (self.req_id, old_ts))
            conn.commit()
        rows = self.client.get(f"/api/requirements/{self.req_id}/events").get_json()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIsInstance(row["ts_ms"], int, f"{row['content']}: ts_ms 应为 int 毫秒")
            self.assertEqual(row["ts_ms"], b._ts_ms(row["ts"]))
        # 历史行同样被补：03:04:05(+08:00) == 前一天 19:04:05 UTC
        hist = [r for r in rows if r["content"] == "历史行"][0]
        expected = int(datetime(2026, 1, 1, 19, 4, 5, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(hist["ts_ms"], expected)
        # 新鲜行 ts_ms ≈ now（服务端刚写入）
        fresh = [r for r in rows if r["content"] == "现场"][0]
        self.assertLess(abs(fresh["ts_ms"] / 1000.0 - time.time()), 5)


if __name__ == "__main__":
    unittest.main()
