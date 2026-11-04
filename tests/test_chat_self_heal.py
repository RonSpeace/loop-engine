#!/usr/bin/env python3
"""建卡会话自愈回归（P0+P1）：进程失效时 prompt/confirm 自动恢复，
线程收尾兜底不残留 running；启动对账把 idle/running 标 interrupted。

运行：
    .venv/bin/python -m unittest tests.test_chat_self_heal -v
"""
import shutil
import sys
import tempfile
import threading
import time
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
    """不发真实 kimi 的会话桩：prompt 立即返回，记录调用。"""

    def __init__(self):
        self.proc = FakeProc()
        self.prompts = []
        self.cancelled = False
        self.closed = False
        self.thinking = "high"  # 模拟 configOptions 的 thought_level 原始值（#118）

    def prompt(self, text, timeout=None):
        self.prompts.append(text)

    def _flush_message(self):
        pass

    def cancel(self):
        self.cancelled = True

    def close(self, kill=False):
        self.closed = True


class ChatSelfHealTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-chat-heal-"))
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            conn.execute("DELETE FROM chat_events")
            conn.execute("DELETE FROM chat_sessions")
            conn.commit()
        with b.chat_lock:
            b.chat_runtime.clear()
            b.chat_booting.clear()
        self.boot_calls = []
        self.sessions = {}
        self._old_paths = b.PROJECT_PATHS
        b.PROJECT_PATHS = {"req-board": ["/tmp/req-board"], "other": ["/tmp/other"]}
        b._chat_boot = self._fake_boot

    def tearDown(self):
        b.PROJECT_PATHS = self._old_paths

    def _fake_boot(self, chat_id, project, resume_session_id=None):
        s = FakeSession()
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": s, "logf": DummyLog()}
        b._chat_set(chat_id, status="idle", session_id="new-session", model="test",
                    thinking=s.thinking, interrupt_source=None, error=None)
        b._chat_add_event(chat_id, "system",
                          "会话已恢复（项目 req-board）" if resume_session_id else "会话已建立（项目 req-board）")
        with b.chat_lock:
            b.chat_booting.discard(chat_id)
        self.boot_calls.append({"chat_id": chat_id, "resume_session_id": resume_session_id})
        self.sessions[chat_id] = s

    def _insert(self, **fields):
        vals = {"project": "req-board", "status": "idle", "session_id": "old-session",
                "draft_title": None, "draft_notes": None,
                "interrupt_source": None,
                "created_at": b.now_str(), "updated_at": b.now_str()}
        vals.update(fields)
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO chat_sessions (project, status, session_id, draft_title, draft_notes, interrupt_source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (vals["project"], vals["status"], vals["session_id"], vals["draft_title"],
                 vals["draft_notes"], vals["interrupt_source"], vals["created_at"], vals["updated_at"]))
            chat_id = cur.lastrowid
            conn.commit()
        return chat_id

    def _status(self, chat_id):
        with b.get_db() as conn:
            row = conn.execute("SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
        return row["status"] if row else None

    def _events(self, chat_id):
        with b.get_db() as conn:
            rows = conn.execute("SELECT content FROM chat_events WHERE chat_id = ? ORDER BY id",
                                (chat_id,)).fetchall()
        return [r["content"] for r in rows]

    def _wait(self, cond, timeout=10):  # 后台线程收尾；全量并发/CI 慢机上 3s 偶发不够，条件满足即返回
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.01)
        return False

    # ---- #118：thinking 采集（建卡会话推理深度落库，暂不展示） ----

    def test_thinking_column_exists_and_historical_null(self):
        # 迁移：chat_sessions 存在 thinking 列
        with b.get_db() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
            self.assertIn("thinking", cols)
        # 历史会话（未带 thinking 字段插入）该列为 NULL
        old_id = self._insert(status="idle")
        with b.get_db() as conn:
            row = conn.execute("SELECT thinking FROM chat_sessions WHERE id = ?", (old_id,)).fetchone()
        self.assertIsNone(row["thinking"])

    def test_boot_persists_thinking_original_value(self):
        # 会话建立（挂载点 _chat_set 补写）后 thinking 落库为 configOptions 原始值
        chat_id = self._insert(status="idle")
        self._fake_boot(chat_id, "req-board")
        with b.get_db() as conn:
            row = conn.execute("SELECT thinking FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
        self.assertEqual(row["thinking"], "high")

    # ---- P0：进程失效自愈 ----

    def test_prompt_idle_without_runtime_auto_resumes(self):
        chat_id = self._insert(status="idle")
        resp, code = b._start_chat_round(chat_id, "你好", event=("user", "你好"))
        self.assertEqual(code, 202)
        # 恢复桩先登记 idle，会话线程随后才提交 prompt；等待真实消息证据，避免调度竞态。
        self.assertTrue(self._wait(lambda: self._status(chat_id) == "idle"
                                   and chat_id in self.sessions and self.sessions[chat_id].prompts))
        # 用旧 session_id 恢复，而非新建
        self.assertEqual(self.boot_calls[-1]["resume_session_id"], "old-session")
        # 用户消息确实送达 kimi（首轮会拼系统提示词）
        self.assertIn("用户消息：\n你好", self.sessions[chat_id].prompts[-1])
        evs = self._events(chat_id)
        self.assertTrue(any("自动恢复" in e for e in evs))
        self.assertTrue(any("会话已恢复" in e for e in evs))

    def test_prompt_orphan_running_auto_recovers(self):
        chat_id = self._insert(status="running")
        resp, code = b._start_chat_round(chat_id, "继续", event=("user", "继续"))
        self.assertEqual(code, 202)
        self.assertTrue(self._wait(lambda: self._status(chat_id) == "idle" and chat_id in self.sessions))
        self.assertEqual(self.boot_calls[-1]["resume_session_id"], "old-session")
        evs = self._events(chat_id)
        self.assertTrue(any("自动恢复" in e for e in evs))

    def test_prompt_running_with_live_runtime_conflict(self):
        chat_id = self._insert(status="running")
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": FakeSession(), "logf": DummyLog()}
        resp, code = b._start_chat_round(chat_id, "x", event=("user", "x"))
        self.assertEqual(code, 409)

    def test_confirm_without_runtime_resumes_and_prompts(self):
        chat_id = self._insert(status="idle", draft_title="测试需求", draft_notes="备注")
        r = self.client.post(f"/api/chat/{chat_id}/confirm")
        self.assertEqual(r.status_code, 202)
        self.assertTrue(self._wait(lambda: self._status(chat_id) == "idle" and chat_id in self.sessions))
        self.assertEqual(self.boot_calls[-1]["resume_session_id"], "old-session")
        self.assertIn("create_requirement", self.sessions[chat_id].prompts[-1])

    def test_resume_allows_stale_idle(self):
        chat_id = self._insert(status="idle")
        r = self.client.post(f"/api/chat/{chat_id}/resume", json={"text": "接着聊"})
        self.assertEqual(r.status_code, 202)
        self.assertTrue(self._wait(lambda: self._status(chat_id) == "idle" and chat_id in self.sessions))
        self.assertEqual(self.boot_calls[-1]["resume_session_id"], "old-session")

    # ---- 项目切换：挂起并恢复 ----

    def test_suspend_idle_preserves_session_and_stops_runtime(self):
        chat_id = self._insert(status="idle", draft_title="保留草稿", draft_notes="详情")
        session = FakeSession()
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": session, "logf": DummyLog()}

        r = self.client.post(f"/api/chat/{chat_id}/suspend")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._status(chat_id), "suspended")
        self.assertTrue(session.closed)
        self.assertNotIn(chat_id, b.chat_runtime)
        with b.get_db() as conn:
            row = conn.execute(
                "SELECT session_id, draft_title, draft_notes FROM chat_sessions WHERE id = ?",
                (chat_id,)).fetchone()
        self.assertEqual(dict(row), {
            "session_id": "old-session", "draft_title": "保留草稿", "draft_notes": "详情"
        })

    def test_suspend_running_cancels_round(self):
        chat_id = self._insert(status="running")
        session = FakeSession()
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": session, "logf": DummyLog()}

        r = self.client.post(f"/api/chat/{chat_id}/suspend")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._status(chat_id), "suspended")
        self.assertTrue(session.cancelled)
        self.assertTrue(session.closed)
        self.assertTrue(any("当前一轮已中断并挂起" in e for e in self._events(chat_id)))

    def test_resume_suspended_uses_previous_session(self):
        chat_id = self._insert(status="suspended", interrupt_source="project_switch")

        r = self.client.post(f"/api/chat/{chat_id}/resume")

        self.assertEqual(r.status_code, 202)
        self.assertTrue(self._wait(lambda: self._status(chat_id) == "idle" and chat_id in self.sessions))
        self.assertEqual(self.boot_calls[-1]["resume_session_id"], "old-session")

    def test_resume_suspended_allows_parallel_other_project(self):
        chat_id = self._insert(status="suspended", interrupt_source="project_switch")
        other_id = self._insert(project="other", status="idle")

        r = self.client.post(f"/api/chat/{chat_id}/resume")

        self.assertEqual(r.status_code, 202)
        self.assertTrue(self._wait(lambda: self._status(chat_id) == "idle" and chat_id in self.sessions))
        self.assertEqual(self.boot_calls[-1]["resume_session_id"], "old-session")

    # ---- 并行会话（每项目独立，全站最多 3 个 running） ----

    def test_chat_new_same_project_conflict(self):
        self._insert(project="req-board", status="idle")
        r = self.client.post("/api/chat", json={"project": "req-board"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["project"], "req-board")

    def test_chat_new_parallel_cap(self):
        for i in range(3):
            self._insert(project=f"p{i}", status="running")
        r = self.client.post("/api/chat", json={"project": "other"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("并行", r.get_json()["error"])

    def test_chat_new_under_cap_allowed(self):
        self._insert(project="p1", status="running")
        self._insert(project="p2", status="running")
        r = self.client.post("/api/chat", json={"project": "other"})
        self.assertEqual(r.status_code, 201)
        chat_id = r.get_json()["id"]
        self.assertTrue(self._wait(lambda: self._status(chat_id) == "idle"))

    def test_prompt_respects_parallel_cap(self):
        for i in range(3):
            self._insert(project=f"p{i}", status="running")
        chat_id = self._insert(status="idle")
        resp, code = b._start_chat_round(chat_id, "hi", event=("user", "hi"))
        self.assertEqual(code, 429)
        self.assertEqual(self._status(chat_id), "idle")

    def test_detach_running_keeps_round_in_background(self):
        chat_id = self._insert(status="running")
        session = FakeSession()
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": session, "logf": DummyLog()}
        r = self.client.post(f"/api/chat/{chat_id}/detach", json={"detached": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._status(chat_id), "running")
        self.assertFalse(session.closed)
        self.assertTrue(b.chat_runtime[chat_id]["detached"])
        self.assertTrue(any("后台继续完成" in e for e in self._events(chat_id)))

    def test_detach_idle_recycles_process(self):
        chat_id = self._insert(status="idle", session_id="old-session")
        session = FakeSession()
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": session, "logf": DummyLog()}
        r = self.client.post(f"/api/chat/{chat_id}/detach", json={"detached": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._status(chat_id), "idle")
        self.assertTrue(session.closed)
        self.assertNotIn(chat_id, b.chat_runtime)

    def test_attach_clears_detached_flag(self):
        chat_id = self._insert(status="running")
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": FakeSession(), "logf": DummyLog(), "detached": True}
        r = self.client.post(f"/api/chat/{chat_id}/detach", json={"detached": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(b.chat_runtime[chat_id]["detached"])

    def test_detached_running_completion_recycles_process(self):
        chat_id = self._insert(status="running")
        s = FakeSession()
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": s, "logf": DummyLog(), "detached": True}
        b._chat_prompt_thread(chat_id, "x")
        self.assertEqual(self._status(chat_id), "idle")
        self.assertTrue(s.closed)
        self.assertNotIn(chat_id, b.chat_runtime)

    # ---- P0：线程收尾兜底 ----

    def test_prompt_thread_early_return_does_not_stick_running(self):
        chat_id = self._insert(status="running")
        b._chat_prompt_thread(chat_id, "x")
        self.assertEqual(self._status(chat_id), "interrupted")
        self.assertTrue(any("本轮未执行" in e for e in self._events(chat_id)))

    def test_prompt_thread_exception_marks_interrupted(self):
        chat_id = self._insert(status="running")
        s = FakeSession()

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        s.prompt = boom
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": s, "logf": DummyLog()}
        b._chat_prompt_thread(chat_id, "x")
        self.assertEqual(self._status(chat_id), "interrupted")
        self.assertTrue(any("本轮异常" in e for e in self._events(chat_id)))

    # ---- P1：启动对账 ----

    def test_recover_marks_idle_and_running_interrupted(self):
        idle_id = self._insert(status="idle")
        running_id = self._insert(status="running")
        b._recover_chat_sessions()
        self.assertEqual(self._status(idle_id), "interrupted")
        self.assertEqual(self._status(running_id), "interrupted")
        with b.get_db() as conn:
            rows = conn.execute(
                "SELECT interrupt_source FROM chat_sessions WHERE id IN (?, ?)",
                (idle_id, running_id)).fetchall()
        self.assertEqual({r["interrupt_source"] for r in rows}, {"restart"})

    def test_auto_resume_latest_recoverable_session(self):
        old_id = self._insert(status="interrupted", interrupt_source="restart")
        latest_id = self._insert(status="interrupted", interrupt_source="process_lost")
        b._auto_resume_chat_session()
        self.assertTrue(self._wait(lambda: latest_id in self.sessions))
        self.assertNotIn(old_id, self.sessions)
        self.assertEqual(self.boot_calls[-1]["chat_id"], latest_id)

    def test_auto_resume_ignores_newer_suspended_session(self):
        interrupted_id = self._insert(status="interrupted", interrupt_source="restart")
        suspended_id = self._insert(status="suspended", interrupt_source="project_switch")
        b._auto_resume_chat_session()
        self.assertTrue(self._wait(lambda: interrupted_id in self.sessions))
        self.assertNotIn(suspended_id, self.sessions)

    def test_auto_resume_does_not_touch_user_cancel(self):
        chat_id = self._insert(status="interrupted", interrupt_source="user_cancel")
        b._auto_resume_chat_session()
        time.sleep(0.05)
        self.assertNotIn(chat_id, self.sessions)

    # ---- 正常路径不回归 ----

    def test_prompt_idle_with_live_runtime_no_reboot(self):
        chat_id = self._insert(status="idle")
        s = FakeSession()
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": s, "logf": DummyLog()}
        resp, code = b._start_chat_round(chat_id, "hi", event=("user", "hi"))
        self.assertEqual(code, 202)
        self.assertTrue(self._wait(lambda: self._status(chat_id) == "idle" and s.prompts))
        self.assertEqual(self.boot_calls, [])

    # ---- 历史已建卡会话分页 ----

    def test_chat_list_created_pagination(self):
        ids = []
        with b.get_db() as conn:
            for i in range(5):
                cur = conn.execute(
                    "INSERT INTO chat_sessions (project, status, req_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("req-board", "created", 1000 + i, b.now_str(), b.now_str()))
                ids.append(cur.lastrowid)
            conn.commit()
        r = self.client.get("/api/chat?project=req-board&status=created&limit=3")
        self.assertEqual(r.status_code, 200)
        first = [x["id"] for x in r.get_json()]
        self.assertEqual(len(first), 3)
        self.assertEqual(first, sorted(first, reverse=True))
        r2 = self.client.get(
            f"/api/chat?project=req-board&status=created&limit=3&before_id={min(first)}")
        self.assertEqual(r2.status_code, 200)
        second = [x["id"] for x in r2.get_json()]
        self.assertEqual(len(second), 2)
        self.assertTrue(all(x < min(first) for x in second))

    def test_chat_list_multi_status(self):
        created_id = self._insert(status="created", req_id=2001)
        closed_id = self._insert(status="closed")
        self._insert(status="idle")
        r = self.client.get("/api/chat?project=req-board&status=created,closed")
        self.assertEqual(r.status_code, 200)
        rows = r.get_json()
        self.assertEqual({x["id"] for x in rows}, {created_id, closed_id})
        self.assertTrue(all(x["status"] in ("created", "closed") for x in rows))

    def test_close_idle_finalizes_then_closes(self):
        chat_id = self._insert(status="idle", session_id="old-session")
        session = FakeSession()
        with b.chat_lock:
            b.chat_runtime[chat_id] = {"session": session, "logf": DummyLog()}
        old_ver = b._kimi_code_version
        old_branch = b._chat_current_branch
        b._kimi_code_version = lambda: "test"
        b._chat_current_branch = lambda project: "main"
        row = None
        try:
            r = self.client.post(f"/api/chat/{chat_id}/close")
            self.assertEqual(r.status_code, 200)
            # #335 close 收尾已后台化：轮询等待后台线程采集完元数据并杀进程
            deadline = time.time() + 5
            while time.time() < deadline:
                if session.closed:
                    with b.get_db() as conn:
                        row = conn.execute(
                            "SELECT finalized, harness, branch FROM chat_sessions WHERE id = ?",
                            (chat_id,)).fetchone()
                    if row and row["finalized"] == 1:
                        break
                time.sleep(0.05)
        finally:
            b._kimi_code_version = old_ver
            b._chat_current_branch = old_branch
        self.assertEqual(self._status(chat_id), "closed")
        self.assertTrue(session.closed)
        if row is None:
            with b.get_db() as conn:
                row = conn.execute(
                    "SELECT finalized, harness, branch FROM chat_sessions WHERE id = ?",
                    (chat_id,)).fetchone()
        self.assertEqual(row["finalized"], 1)
        self.assertEqual(row["harness"], "kimi code test")
        self.assertEqual(row["branch"], "main")
        self.assertTrue(any("会话已关闭" in e for e in self._events(chat_id)))


if __name__ == "__main__":
    unittest.main()
