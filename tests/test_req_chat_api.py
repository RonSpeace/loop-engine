#!/usr/bin/env python3
"""需求对话（#302）API 状态机：创建/发消息/事件增量/取消/恢复/关闭/并行上限/启动对账。
需求详情页常驻聊天，独立于调度器；任何需求状态（含 later/done）均可对话。

运行：
    .venv/bin/python -m unittest tests.test_req_chat_api -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402


class DummyLog:
    def close(self):
        pass


class FakeProc:
    def __init__(self):
        self.pid = 424242

    def poll(self):
        return None


class FakeSession:
    """不发真实 kimi 的会话桩（仅用于 running 状态与 cancel 的进程句柄）。"""

    def __init__(self):
        self.proc = FakeProc()
        self.closed = False

    def cancel(self):
        pass

    def close(self, kill=False):
        self.closed = True


class ReqChatApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-chat-api-"))
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            conn.execute("DELETE FROM req_chat_events")
            conn.execute("DELETE FROM req_chats")
            conn.execute("DELETE FROM requirements")
            conn.commit()
        with b.reqchat_lock:
            b.reqchat_runtime.clear()
            b.reqchat_booting.clear()
        self.round_calls = []
        b._reqchat_run_round = self._fake_run_round

    def _fake_run_round(self, req_chat_id, text):
        self.round_calls.append((req_chat_id, text))

    def _add_req(self, status="done"):
        r = b.req_add("测试需求", "req-board", notes="n", status=status)
        return r["id"]

    # ---- 基本 CRUD ----
    def test_get_no_chat_returns_null(self):
        rid = self._add_req()
        resp = self.client.get(f"/api/requirements/{rid}/chat")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["chat"])

    def test_get_missing_req_404(self):
        self.assertEqual(self.client.get("/api/requirements/99999/chat").status_code, 404)

    def test_create_chat_and_conflict(self):
        rid = self._add_req()
        resp = self.client.post(f"/api/requirements/{rid}/chat")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["chat"]["status"], "created")
        # 已有活跃对话再建 → 409 返回现有
        resp = self.client.post(f"/api/requirements/{rid}/chat")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["chat"]["id"],
                         self.client.get(f"/api/requirements/{rid}/chat").get_json()["chat"]["id"])

    # ---- 发消息（回合制） ----
    def test_prompt_auto_creates_and_rounds(self):
        rid = self._add_req(status="later")  # 暂缓状态也可对话
        resp = self.client.post(f"/api/requirements/{rid}/chat/prompt", json={"text": "你好"})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(self.round_calls), 1)
        # 用户消息事件已写入，且对话行自动创建
        data = self.client.get(f"/api/requirements/{rid}/chat/events").get_json()
        self.assertIsNotNone(data["chat"])
        self.assertEqual([e["kind"] for e in data["events"]], ["user"])

    def test_prompt_empty_text_400(self):
        rid = self._add_req()
        self.assertEqual(
            self.client.post(f"/api/requirements/{rid}/chat/prompt", json={"text": "  "}).status_code, 400)

    def test_prompt_missing_req_404(self):
        self.assertEqual(
            self.client.post("/api/requirements/99999/chat/prompt", json={"text": "x"}).status_code, 404)

    def test_prompt_running_409(self):
        rid = self._add_req()
        chat_id = b._reqchat_create(rid)
        with b.get_db() as conn:
            conn.execute("UPDATE req_chats SET status='running' WHERE id = ?", (chat_id,))
            conn.commit()
        with b.reqchat_lock:
            b.reqchat_runtime[chat_id] = {"session": FakeSession(), "logf": DummyLog(), "mcp": None}
        try:
            resp = self.client.post(f"/api/requirements/{rid}/chat/prompt", json={"text": "x"})
            self.assertEqual(resp.status_code, 409)
        finally:
            with b.reqchat_lock:
                b.reqchat_runtime.pop(chat_id, None)

    def test_parallel_limit_429(self):
        for _ in range(3):
            rid = self._add_req()
            b._reqchat_create(rid)
        with b.get_db() as conn:
            conn.execute("UPDATE req_chats SET status='running'")
            conn.commit()
        rid4 = self._add_req()
        b._reqchat_create(rid4)
        resp = self.client.post(f"/api/requirements/{rid4}/chat/prompt", json={"text": "x"})
        self.assertEqual(resp.status_code, 429)

    # ---- 事件增量 ----
    def test_events_incremental(self):
        rid = self._add_req()
        chat_id = b._reqchat_create(rid)
        b._reqchat_add_event(chat_id, "system", "e1")
        b._reqchat_add_event(chat_id, "message", "e2")
        data = self.client.get(f"/api/requirements/{rid}/chat/events").get_json()
        self.assertEqual([e["content"] for e in data["events"]], ["e1", "e2"])
        after = data["events"][1]["id"]
        self.assertEqual(self.client.get(f"/api/requirements/{rid}/chat/events?after={after}").get_json()["events"], [])
        b._reqchat_add_event(chat_id, "system", "e3")
        data = self.client.get(f"/api/requirements/{rid}/chat/events?after={after}").get_json()
        self.assertEqual([e["content"] for e in data["events"]], ["e3"])

    # ---- 取消/恢复/关闭 ----
    def test_cancel_resume_close_flow(self):
        rid = self._add_req()
        chat_id = b._reqchat_create(rid)
        # 非 running cancel：直接成功
        self.assertEqual(self.client.post(f"/api/requirements/{rid}/chat/cancel").status_code, 200)
        # running + runtime：cancel 杀进程标 interrupted
        with b.get_db() as conn:
            conn.execute("UPDATE req_chats SET status='running' WHERE id = ?", (chat_id,))
            conn.commit()
        sess = FakeSession()
        with b.reqchat_lock:
            b.reqchat_runtime[chat_id] = {"session": sess, "logf": DummyLog(), "mcp": None}
        resp = self.client.post(f"/api/requirements/{rid}/chat/cancel")
        self.assertEqual(resp.status_code, 200)
        with b.get_db() as conn:
            row = conn.execute("SELECT status FROM req_chats WHERE id = ?", (chat_id,)).fetchone()
        self.assertEqual(row["status"], "interrupted")
        self.assertTrue(sess.closed)
        # resume 带消息：202 驱动一轮（事件含 user）
        resp = self.client.post(f"/api/requirements/{rid}/chat/resume", json={"text": "继续"})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(self.round_calls), 1)
        data = self.client.get(f"/api/requirements/{rid}/chat/events").get_json()
        self.assertEqual(data["events"][-1]["kind"], "user")
        # close → 终态 closed；closed 后可新建
        self.assertEqual(self.client.post(f"/api/requirements/{rid}/chat/close").status_code, 200)
        with b.get_db() as conn:
            row = conn.execute("SELECT status FROM req_chats WHERE id = ?", (chat_id,)).fetchone()
        self.assertEqual(row["status"], "closed")
        resp = self.client.post(f"/api/requirements/{rid}/chat")
        self.assertEqual(resp.status_code, 201)

    def test_resume_failed_400(self):
        rid = self._add_req()
        chat_id = b._reqchat_create(rid)
        with b.get_db() as conn:
            conn.execute("UPDATE req_chats SET status='failed' WHERE id = ?", (chat_id,))
            conn.commit()
        resp = self.client.post(f"/api/requirements/{rid}/chat/resume")
        self.assertEqual(resp.status_code, 400)

    # ---- 启动对账 ----
    def test_recover_marks_running_idle_interrupted(self):
        rid = self._add_req()
        b._reqchat_create(rid)
        with b.get_db() as conn:
            conn.execute("UPDATE req_chats SET status='running' WHERE id = "
                         "(SELECT id FROM req_chats ORDER BY id DESC LIMIT 1)")
            conn.execute("INSERT INTO req_chats (req_id, status) VALUES (?, 'idle')", (rid,))
            conn.execute("INSERT INTO req_chats (req_id, status) VALUES (?, 'created')", (rid,))
            conn.commit()
        b._recover_req_chats()
        with b.get_db() as conn:
            statuses = sorted(r["status"] for r in conn.execute(
                "SELECT status FROM req_chats WHERE req_id = ?", (rid,)).fetchall())
        self.assertEqual(statuses, ["created", "interrupted", "interrupted"])


if __name__ == "__main__":
    unittest.main()
