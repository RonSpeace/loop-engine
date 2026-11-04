#!/usr/bin/env python3
"""req-431「新建聊天」不再预建空会话行：后端端点与迁移测试。

覆盖：首条消息端点（单事务落行 + 入队命令 / 校验矩阵 / role 口径 / origin 收敛）、
旧 POST /sessions 410、DELETE 空行兜底矩阵与命令历史保留、origin 两列 ALTER 迁移与
upsert 插入路径写来源、GET 列表语义不变。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_req_session_draft -v
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402

NODES = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
         {"role": "dev", "node_type": "code"},
         {"role": "accept", "node_type": "artifact", "artifact": "accept.md"},
         {"node_type": "terminal"}]


class ReqSessionDraftApiTest(unittest.TestCase):
    """首条消息端点 / 旧端点下线 / DELETE 兜底（临时库 + Flask 测试客户端）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="req431-board-")
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
            conn.execute("DELETE FROM manual_commands")
            conn.execute("DELETE FROM requirements")
            conn.commit()
        self.req_id = self._new_req()

    def _new_req(self, nodes=None, node_index=1, status="open"):
        rid = self.client.post("/api/requirements", json={
            "title": "T", "project": "req-board", "status": "later"}).get_json()["id"]
        self.client.patch(f"/api/requirements/{rid}", json={
            "status": status, "flow": "standard", "node_index": node_index,
            "nodes_json": json.dumps(nodes if nodes is not None else NODES)})
        return rid

    def _counts(self):
        with b.get_db() as conn:
            return (conn.execute("SELECT COUNT(*) AS n FROM agent_sessions").fetchone()["n"],
                    conn.execute("SELECT COUNT(*) AS n FROM manual_commands").fetchone()["n"])

    def _rows(self):
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM agent_sessions ORDER BY id")]

    def _cmds(self):
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM manual_commands ORDER BY id")]

    # ---- 用例 1：首条消息落行 + 入队（单事务） ----
    def test_first_message_creates_row_and_command(self):
        with mock.patch.object(b, "_sse_publish") as pub:
            resp = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                                    json={"text": "  首条消息  "},
                                    headers={"X-Actor": "manual:web", "User-Agent": "pytest-UA"})
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(set(data.keys()), {"id", "command_id"})
        rows, cmds = self._rows(), self._cmds()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(cmds), 1)
        r = rows[0]
        self.assertEqual(r["id"], data["id"])
        self.assertEqual(r["req_id"], self.req_id)
        self.assertEqual(r["role"], "dev")          # 快照 node_index=1 → dev
        self.assertIsNone(r["session_id"])
        self.assertEqual(r["status"], "created")
        self.assertEqual(r["poisoned"], 0)
        self.assertEqual(r["origin"], "manual")
        self.assertIn("X-Actor=manual:web", r["origin_detail"])
        self.assertIn("UA=pytest-UA", r["origin_detail"])
        c = cmds[0]
        self.assertEqual(c["id"], data["command_id"])
        self.assertEqual(c["agent_session_id"], data["id"])
        self.assertEqual(c["req_id"], self.req_id)
        self.assertEqual(c["role"], "dev")
        self.assertEqual(c["text"], "首条消息")     # strip 后入库
        self.assertEqual(c["status"], "pending")
        pub.assert_any_call("sessions")

    # ---- 用例 2：role 缺省用允许角色；不符 400 且零写入 ----
    def test_first_message_role_defaults_and_mismatch(self):
        ok = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                              json={"text": "x", "role": "dev"})
        self.assertEqual(ok.status_code, 201)
        bad = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                               json={"text": "x", "role": "accept"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self._counts(), (1, 1))    # 失败例零新增

    # ---- 用例 3：校验矩阵（任一失败零写入，单事务原子性） ----
    def test_first_message_validation_matrix(self):
        cases = [
            ("需求不存在", 999999, {"text": "x"}, 404),
            ("text 缺失", self.req_id, {}, 400),
            ("text 空串", self.req_id, {"text": ""}, 400),
            ("text 全空白", self.req_id, {"text": "  \n\t "}, 400),
        ]
        for name, rid, body, code in cases:
            with self.subTest(name):
                resp = self.client.post(f"/api/requirements/{rid}/sessions/messages", json=body)
                self.assertEqual(resp.status_code, code)
                self.assertEqual(self._counts(), (0, 0))
        done_id = self._new_req(status="done")
        resp = self.client.post(f"/api/requirements/{done_id}/sessions/messages", json={"text": "x"})
        self.assertEqual(resp.status_code, 400)
        term_id = self._new_req(node_index=3)
        resp = self.client.post(f"/api/requirements/{term_id}/sessions/messages", json={"text": "x"})
        self.assertEqual(resp.status_code, 400)
        no_snapshot = self.client.post("/api/requirements", json={
            "title": "前置卡", "project": "req-board", "status": "later"}).get_json()["id"]
        self.client.patch(f"/api/requirements/{no_snapshot}", json={"status": "open"})
        resp = self.client.post(f"/api/requirements/{no_snapshot}/sessions/messages",
                                json={"text": "x"})
        self.assertEqual(resp.status_code, 201)     # 无快照 → 前置槽位角色
        self.assertEqual(self._rows()[0]["role"], b.pre_role())
        self.assertEqual(self._counts(), (1, 1))

    # ---- 用例 4：origin 封闭枚举收敛 ----
    def test_first_message_origin_buckets(self):
        for actor, expect in (("agent:some-agent", "agent"), ("unknown:x", "api"),
                              (None, "api"), ("dispatcher", "dispatcher"),
                              ("manual:网页新建会话", "manual")):
            with self.subTest(actor):
                with b.get_db() as conn:
                    conn.execute("DELETE FROM agent_sessions")
                    conn.execute("DELETE FROM manual_commands")
                    conn.commit()
                headers = {"User-Agent": "UA-X"}
                if actor:
                    headers["X-Actor"] = actor
                resp = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                                        json={"text": "x"}, headers=headers)
                self.assertEqual(resp.status_code, 201)
                row = self._rows()[0]
                self.assertEqual(row["origin"], expect)
                self.assertIn("UA=UA-X", row["origin_detail"])

    # ---- 用例 5：旧端点 410 且零新增 ----
    def test_old_session_endpoint_gone(self):
        resp = self.client.post(f"/api/requirements/{self.req_id}/sessions", json={})
        self.assertEqual(resp.status_code, 410)
        self.assertIn("sessions/messages", resp.get_json()["error"])
        self.assertEqual(self._counts(), (0, 0))

    # ---- 用例 6：空行删除成功 ----
    def test_delete_empty_session_ok(self):
        rid = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                               json={"text": "x"}).get_json()["id"]
        with b.get_db() as conn:      # 命令消费完（done）才允许删
            conn.execute("UPDATE manual_commands SET status='done' WHERE agent_session_id=?", (rid,))
            conn.commit()
        with mock.patch.object(b, "_sse_publish") as pub:
            resp = self.client.delete(f"/api/requirements/{self.req_id}/sessions/{rid}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"deleted": 1})
        self.assertEqual(self._counts()[0], 0)
        pub.assert_any_call("sessions")

    # ---- 用例 7：拒绝矩阵（每例断言行仍在） ----
    def test_delete_rejects_matrix(self):
        def new_row(**fields):
            cols = {"req_id": self.req_id, "role": "dev", "status": "created",
                    "created_at": b.now_str(), "updated_at": b.now_str()}
            cols.update(fields)
            with b.get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO agent_sessions (%s) VALUES (%s)"
                    % (", ".join(cols), ", ".join("?" * len(cols))), tuple(cols.values()))
                conn.commit()
                return cur.lastrowid

        def add_cmd(asid, status):
            with b.get_db() as conn:
                conn.execute("INSERT INTO manual_commands (req_id, agent_session_id, role, text,"
                             " status, created_at, updated_at) VALUES (?, ?, 'dev', 'x', ?, ?, ?)",
                             (self.req_id, asid, status, b.now_str(), b.now_str()))
                conn.commit()

        cases = []
        real = new_row(session_id="session_x")
        cases.append(("真实会话", real, 409))
        pend = new_row()
        add_cmd(pend, "pending")
        cases.append(("pending 命令", pend, 409))
        run_cmd = new_row()
        add_cmd(run_cmd, "running")
        cases.append(("running 命令", run_cmd, 409))
        running = new_row(status="running")
        cases.append(("行 running", running, 409))
        other_req = self._new_req()
        cases.append(("跨需求", real, 404))
        for name, asid, code in cases:
            rid = other_req if name == "跨需求" else self.req_id
            with self.subTest(name):
                resp = self.client.delete(f"/api/requirements/{rid}/sessions/{asid}")
                self.assertEqual(resp.status_code, code)
                self.assertEqual(len(self._rows()), len(cases) - 1)
        resp = self.client.delete(f"/api/requirements/{self.req_id}/sessions/999999")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.delete(f"/api/requirements/999999/sessions/{real}")
        self.assertEqual(resp.status_code, 404)

    # ---- 用例 8：命令历史（done/failed）保留 ----
    def test_delete_keeps_command_history(self):
        rid = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                               json={"text": "x"}).get_json()["id"]
        with b.get_db() as conn:
            conn.execute("UPDATE manual_commands SET status='failed', error='e'")
            conn.commit()
        self.assertEqual(self.client.delete(
            f"/api/requirements/{self.req_id}/sessions/{rid}").status_code, 200)
        cmds = self._cmds()
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["status"], "failed")
        self.assertEqual(cmds[0]["agent_session_id"], rid)

    # ---- 用例 10：upsert 插入写 origin（更新路径不改） ----
    def test_upsert_insert_writes_origin(self):
        r1 = self.client.post("/api/agent-sessions", json={
            "req_id": self.req_id, "role": "dev", "session_id": "s_1"},
            headers={"X-Actor": "dispatcher", "User-Agent": "UA-D"})
        self.assertEqual(r1.status_code, 201)
        row = self._rows()[0]
        self.assertEqual(row["origin"], "dispatcher")
        self.assertIn("UA=UA-D", row["origin_detail"])
        # 按 id 更新不改 origin；body 传 origin 也不生效（不在白名单）
        self.client.post("/api/agent-sessions", json={
            "id": row["id"], "status": "idle", "origin": "manual"})
        after = self._rows()[0]
        self.assertEqual(after["status"], "idle")
        self.assertEqual(after["origin"], "dispatcher")
        # 无头插入 → api 桶
        self.client.post("/api/agent-sessions", json={
            "req_id": self.req_id, "role": "tech", "session_id": "s_2"})
        self.assertEqual([r["origin"] for r in self._rows()], ["dispatcher", "api"])

    # ---- 用例 11：GET 列表语义不变（空行照返回） ----
    def test_list_endpoints_keep_empty_rows(self):
        rid = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                               json={"text": "x"}).get_json()["id"]
        rows = self.client.get(f"/api/requirements/{self.req_id}/sessions").get_json()
        self.assertEqual([r["id"] for r in rows], [rid])
        self.assertIsNone(rows[0]["session_id"])
        all_rows = self.client.get("/api/agent-sessions").get_json()
        self.assertEqual([r["id"] for r in all_rows], [rid])
        self.assertIn("origin", all_rows[0])
        self.assertIn("session_dir", all_rows[0])


class OriginColumnMigrationTest(unittest.TestCase):
    """用例 9：origin/origin_detail 两列 ALTER 迁移（缺列才加、幂等、存量留空）"""

    def test_migration_adds_origin_columns(self):
        tmp = tempfile.mkdtemp(prefix="req431-mig-")
        old_db = Path(tmp) / "old.db"
        conn = sqlite3.connect(old_db)
        conn.execute("""CREATE TABLE agent_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, req_id INTEGER NOT NULL, role TEXT NOT NULL,
            session_id TEXT, status TEXT DEFAULT 'created')""")
        conn.execute("INSERT INTO agent_sessions (req_id, role, session_id, status)"
                     " VALUES (7, 'dev', 's_old', 'idle')")
        conn.commit()
        conn.close()
        with mock.patch.object(b, "DB_PATH", old_db):
            b.init_db()
            b.init_db()      # 幂等：重复启动不报错
            with b.get_db() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(agent_sessions)")}
                self.assertTrue({"origin", "origin_detail"} <= cols)
                row = c.execute("SELECT * FROM agent_sessions WHERE id = 1").fetchone()
                self.assertIsNone(row["origin"])          # 存量行留空不回填
                self.assertIsNone(row["origin_detail"])
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
