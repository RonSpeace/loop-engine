#!/usr/bin/env python3
"""req-273 人工接管测试：
1. board agent_sessions 注册表 API（upsert/列表/按需求查询）
2. board 新建空会话 / 发消息入队 manual_commands API
3. dispatcher mark_session_poisoned / find_resume_sid 跳过毒会话
4. dispatcher consume_manual_commands 消费/claim/恢复

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_agent_sessions_manual -v
"""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


class BoardAgentSessionsApiTest(unittest.TestCase):
    """agent_sessions / manual_commands 看板 API（临时库）"""

    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b
        cls.tmp = tempfile.mkdtemp(prefix="req273-board-")
        cls._orig_db = b.DB_PATH
        b.DB_PATH = Path(cls.tmp) / "test.db"
        b.init_db()
        b.app.config["TESTING"] = True
        cls.client = b.app.test_client()
        resp = cls.client.post("/api/requirements", json={
            "title": "T", "project": "req-board", "status": "later", "mode": "semi", "phase": "accept"})
        cls.req_id = resp.get_json()["id"]
        # 建卡接口不落 phase（内部流转字段），补 PATCH 使当前阶段为 accept；
        # #419：同时写卡上流程快照——/sessions 允许角色按快照当前节点判定（accept 节点）
        cls.client.patch(f"/api/requirements/{cls.req_id}", json={
            "phase": "accept", "flow": "standard", "node_index": 2, "nodes_json": json.dumps([
                {"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                {"role": "dev", "node_type": "code"},
                {"role": "accept", "node_type": "artifact", "artifact": "accept.md"},
                {"node_type": "terminal"}])})

    @classmethod
    def tearDownClass(cls):
        cls.b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with self.b.get_db() as conn:
            conn.execute("DELETE FROM agent_sessions")
            conn.execute("DELETE FROM manual_commands")
            conn.commit()

    def _upsert(self, **fields):
        return self.client.post("/api/agent-sessions", json=fields)

    def test_upsert_insert_then_update_by_session(self):
        r1 = self._upsert(req_id=self.req_id, role="accept", session_id="s_abc",
                          model="M", status="running")
        self.assertEqual(r1.status_code, 201)
        rid = r1.get_json()["id"]
        r2 = self._upsert(req_id=self.req_id, role="accept", session_id="s_abc",
                          poisoned=1, status="idle")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["id"], rid)
        with self.b.get_db() as conn:
            rows = conn.execute("SELECT * FROM agent_sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["poisoned"], 1)
        self.assertEqual(rows[0]["status"], "idle")

    def test_upsert_by_id(self):
        rid = self._upsert(req_id=self.req_id, role="dev", session_id="s_x").get_json()["id"]
        resp = self._upsert(id=rid, status="idle", context_pct="10%")
        self.assertEqual(resp.status_code, 200)
        with self.b.get_db() as conn:
            row = conn.execute("SELECT * FROM agent_sessions WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["context_pct"], "10%")

    def test_upsert_without_session_rejected(self):
        resp = self._upsert(req_id=self.req_id, role="accept")
        self.assertEqual(resp.status_code, 400)

    def test_list_endpoints(self):
        self._upsert(req_id=self.req_id, role="accept", session_id="s_1")
        self._upsert(req_id=999, role="dev", session_id="s_2")
        all_rows = self.client.get("/api/agent-sessions").get_json()
        self.assertEqual(len(all_rows), 2)
        req_rows = self.client.get(f"/api/requirements/{self.req_id}/sessions").get_json()
        self.assertEqual(len(req_rows), 1)
        self.assertEqual(req_rows[0]["session_id"], "s_1")

    def test_new_session_only_current_role(self):
        """#431：空行只由首条消息端点创建（旧 POST /sessions 已 410），角色口径不变"""
        old = self.client.post(f"/api/requirements/{self.req_id}/sessions", json={"role": "accept"})
        self.assertEqual(old.status_code, 410)
        bad = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                               json={"role": "dev", "text": "x"})
        self.assertEqual(bad.status_code, 400)
        resp = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                                json={"role": "accept", "text": "x"})
        self.assertEqual(resp.status_code, 201)
        row_id = resp.get_json()["id"]
        with self.b.get_db() as conn:
            row = conn.execute("SELECT * FROM agent_sessions WHERE id=?", (row_id,)).fetchone()
        self.assertEqual(row["role"], "accept")
        self.assertEqual(row["status"], "created")
        self.assertIsNone(row["session_id"])

    def test_message_enqueue_and_status_transitions(self):
        new = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                               json={"text": "首条"}).get_json()
        cmds = self.client.get("/api/manual-commands?status=pending").get_json()
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["agent_session_id"], new["id"])
        self.assertEqual(cmds[0]["text"], "首条")
        resp = self.client.post(
            f"/api/requirements/{self.req_id}/sessions/{new['id']}/messages", json={"text": "继续"})
        self.assertEqual(resp.status_code, 202)
        cmds = self.client.get("/api/manual-commands?status=pending").get_json()
        self.assertEqual(len(cmds), 2)
        # claim -> done
        self.client.patch(f"/api/manual-commands/{cmds[0]['id']}", json={"status": "running"})
        self.client.patch(f"/api/manual-commands/{cmds[0]['id']}", json={"status": "done"})
        rest = self.client.get("/api/manual-commands?status=pending").get_json()
        self.assertEqual([c["id"] for c in rest], [cmds[1]["id"]])

    def test_message_while_running_conflict(self):
        new = self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                               json={"text": "首条"}).get_json()
        with self.b.get_db() as conn:
            conn.execute("UPDATE agent_sessions SET status='running' WHERE id=?", (new["id"],))
            conn.commit()
        resp = self.client.post(
            f"/api/requirements/{self.req_id}/sessions/{new['id']}/messages", json={"text": "x"})
        self.assertEqual(resp.status_code, 409)


class DispatcherPoisonAndManualTest(unittest.TestCase):
    """mark_session_poisoned / find_resume_sid / consume_manual_commands"""

    def setUp(self):
        # #420：d.state 是模块级共享对象，tearDown 必须还原（旧实现不还原，测试态泄漏到后续用例/文件）
        self._orig_state = d.state
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}}
        self.api_calls = []
        self._orig_api_board = d.api_board
        d.api_board = lambda method, path, body=None: self.api_calls.append((method, path, body))
        self._orig_save = d.save_state
        d.save_state = lambda: None
        self._orig_extra = {name: getattr(d, name) for name in (
            "fetch_req", "set_req", "post_dispatcher_event", "_run_manual_command")}

    def tearDown(self):
        d.state = self._orig_state
        d.api_board = self._orig_api_board
        d.save_state = self._orig_save
        for name, value in self._orig_extra.items():
            setattr(d, name, value)

    def test_mark_poisoned_sets_flag_and_syncs(self):
        d.state["sessions"]["accept:113"] = {"id": "session_abc", "poisoned": False}
        d.mark_session_poisoned(113, "accept")
        self.assertTrue(d.state["sessions"]["accept:113"]["poisoned"])
        self.assertTrue(any(m == "POST" and p == "/agent-sessions" and b.get("poisoned") == 1
                            for m, p, b in self.api_calls))

    def test_find_resume_skips_poisoned(self):
        d.state["sessions"]["accept:113"] = {"id": "session_abc", "poisoned": True}
        self.assertIsNone(d.find_resume_sid({"id": 113}, "accept"))
        d.state["sessions"]["accept:113"]["poisoned"] = False
        self.assertEqual(d.find_resume_sid({"id": 113}, "accept"), "session_abc")

    def test_consume_manual_command_claims_and_spawns(self):
        cmd = {"id": 7, "req_id": 113, "agent_session_id": 3, "role": "accept", "text": "继续"}
        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), kwargs=None, **extra):
                self.target = target
                self.args = args
                self.kwargs = kwargs or {}

            def start(self):
                started.append((self.target, self.args))

        def fake_api_board(method, path, body=None):
            self.api_calls.append((method, path, body))
            if method == "GET" and path == "/manual-commands?status=pending":
                return [cmd]
            return {}

        d.api_board = fake_api_board
        d.fetch_req = lambda rid: {"id": 113, "project": "req-board", "title": "T",
                                   "mode": "semi", "phase": "dev"}
        d.set_req = lambda rid, fields: None
        d.post_dispatcher_event = lambda *a, **k: None
        with mock.patch.object(d.threading, "Thread", FakeThread), \
                mock.patch.object(d, "_run_manual_command", lambda *a, **k: None):
            d.consume_manual_commands()
        self.assertTrue(any(m == "PATCH" and p == "/manual-commands/7" and b == {"status": "running"}
                            for m, p, b in self.api_calls))
        self.assertEqual(len(started), 1)

    def test_consume_manual_command_records_pending_confirm_answer(self):
        """网页聊天框是普通提问在网页端的唯一答复入口：该卡有待答普通提问时，人工消息同样留档
        summary（口径与飞书 y 一致）；机器生成的两类提问（前置流程确认 / 评审超限裁决）跳过"""
        cmd = {"id": 9, "req_id": 113, "agent_session_id": 3, "role": "dev", "text": "按 B 方案"}
        summaries = []
        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), kwargs=None, **extra):
                pass

            def start(self):
                started.append(1)

        d.api_board = lambda method, path, body=None: [cmd] if (
            method == "GET" and path == "/manual-commands?status=pending") else {}
        d.fetch_req = lambda rid: {"id": 113, "project": "req-board", "title": "T",
                                   "mode": "semi", "phase": "dev"}
        d.set_req = lambda rid, fields: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda rid, line, **kw: summaries.append((rid, line))
        d.state["pending_confirm"]["113"] = {"req": None, "question": "A 方案还是 B 方案？"}
        with mock.patch.object(d.threading, "Thread", FakeThread), \
                mock.patch.object(d, "_run_manual_command", lambda *a, **k: None):
            d.consume_manual_commands()
        self.assertEqual(summaries, [(113, "人工答复（问题：A 方案还是 B 方案？）：按 B 方案。")])
        self.assertEqual(len(started), 1)

        # 机器生成的提问（带 flow_id / review_index）不写这一行
        summaries.clear()
        d.state["pending_confirm"]["113"] = {"req": None, "question": "推荐流程「复杂」…",
                                             "flow_id": "heavy"}
        with mock.patch.object(d.threading, "Thread", FakeThread), \
                mock.patch.object(d, "_run_manual_command", lambda *a, **k: None):
            d.consume_manual_commands()
        self.assertEqual(summaries, [])

    def test_consume_skips_when_active(self):
        cmd = {"id": 8, "req_id": 113, "agent_session_id": 3, "role": "accept", "text": "继续"}
        d.api_board = lambda method, path, body=None: [cmd] if (
            method == "GET" and path == "/manual-commands?status=pending") else {}
        d.fetch_req = lambda rid: {"id": 113, "project": "req-board", "title": "T",
                                   "mode": "semi", "phase": "accept"}
        with d.active_lock:
            d.active_runs["113"] = {"proc": object(), "project": "req-board",
                                    "role": "accept", "owner": "113"}
        d.consume_manual_commands()
        self.assertTrue(not any(m == "PATCH" and "/manual-commands/" in p for m, p, b in self.api_calls))
        with d.active_lock:
            d.active_runs.pop("113", None)

    def test_consume_review_route_keeps_status_no_doing(self):
        """#284 现场：route/review（等待人工决策的中间态）需求人工续跑只执行会话、不置 doing——
        否则决策对账会把「状态变 doing」误判为网页端确认/评审通过（accept 阶段会误置 done）"""
        for status in ("route", "review"):
            with self.subTest(status=status):
                self.api_calls = []
                cmd = {"id": 10, "req_id": 113, "agent_session_id": 3, "role": "accept", "text": "继续"}
                set_calls = []

                def fake_api_board(method, path, body=None):
                    self.api_calls.append((method, path, body))
                    if method == "GET" and path == "/manual-commands?status=pending":
                        return [cmd]
                    return {}

                class FakeThread:
                    def __init__(self, target=None, args=(), kwargs=None, **extra):
                        self.target = target
                        self.args = args
                        self.kwargs = kwargs or {}

                    def start(self):
                        pass

                d.api_board = fake_api_board
                d.fetch_req = lambda rid: {"id": 113, "project": "req-board", "title": "T",
                                           "mode": "semi", "phase": "accept", "status": status}
                d.set_req = lambda rid, fields: set_calls.append((rid, fields))
                d.post_dispatcher_event = lambda *a, **k: None
                with mock.patch.object(d.threading, "Thread", FakeThread), \
                        mock.patch.object(d, "_run_manual_command", lambda *a, **k: None):
                    d.consume_manual_commands()
                self.assertTrue(any(m == "PATCH" and p == "/manual-commands/10"
                                    and b == {"status": "running"} for m, p, b in self.api_calls))
                self.assertEqual(set_calls, [])  # 不置 doing、不改 phase

    def test_recover_running_commands(self):
        calls = []

        def fake_api_board(method, path, body=None):
            calls.append((method, path, body))
            return [{"id": 9}] if method == "GET" and path == "/manual-commands?status=running" else {}

        d.api_board = fake_api_board
        d.recover_manual_commands()
        self.assertTrue(any(m == "PATCH" and p == "/manual-commands/9"
                            and b.get("status") == "failed" for m, p, b in calls))

    def test_manual_run_success_clears_poisoned(self):
        """人工续跑成功（卡片进入 review/done）后清除毒会话标记"""
        d.state["sessions"]["accept:113"] = {"id": "session_abc", "poisoned": True}
        calls = []

        def fake_api_board(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET" and path == "/requirements/113/sessions":
                return [{"id": 3, "session_id": "session_abc"}]
            return {}

        d.api_board = fake_api_board
        d.fetch_req = lambda rid: {"id": 113, "project": "req-board", "status": "review"}
        with mock.patch.object(d, "run_agent", lambda *a, **k: None):
            d._run_manual_command({"id": 5, "agent_session_id": 3},
                                  {"id": 113, "project": "req-board"}, "accept", "继续")
        self.assertTrue(any(m == "POST" and p == "/agent-sessions"
                            and b == {"id": 3, "poisoned": 0} for m, p, b in calls))
        self.assertFalse(d.state["sessions"]["accept:113"]["poisoned"])

    def test_teardown_restores_state(self):
        """#420 回归锁：tearDown 必须把 d.state 还原为 setUp 前对象（旧实现不还原，
        测试态 state 泄漏到同进程后续用例/文件）。自跑一轮 tearDown 后重建现场。"""
        original = self._orig_state
        self.tearDown()
        self.assertIs(d.state, original)
        self.setUp()


if __name__ == "__main__":
    unittest.main()
