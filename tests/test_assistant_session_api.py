#!/usr/bin/env python3
"""#24 飞书看板助手（board 侧）测试：表结构 / 会话 API / 取消契约 / 历史聚合 /
助手工具面 / ACP 构造档。

覆盖：
1  新表 + 新索引幂等（重复 init_db 零副作用）；旧表只增列不重建、project NOT NULL 不变。
2  旧库迁移：chat_scope='chat'、feishu_chat_id=NULL、存量行其余列逐列不变。
3  部分唯一索引：活跃态（idle/running/cancelling/interrupted）一 chat 一行；closed/failed 释放约束。
4  get-or-create 撞唯一约束回读既有行（不插第二行）。
5  双绑定归属校验：session id + feishu_chat_id 全部端点，不匹配 404。
6  cancel 契约：锁内标 cancelling → session.cancel + 关进程 → 保留 session_id、写 interrupted 事件；
   turn_id 不匹配 409。
7  history 聚合按 (source, id) 复合主键，两表 id 撞号不串会话；before_id 游标。
8  create_requirement：project 必须在清单内、status 仅 later/open 且缺省 later、同轮幂等、
   来源链接写入 notes、未知工具/未知参数 fail-closed。
9  助手工具面固定（无 db_query/read_file/list_dir/git_*）。
10 助手 ACP 构造档：tool_profile='feishu_assistant' + 只注入助手 MCP + 只读 roots 仅临时空目录。

隔离：临时 DB + 临时 PROJECT_PATHS/FEISHU_JSON_PATH/HEARTBEAT_FILE + 打桩 ACP 线程
（不起真实 kimi、不建 worktree）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_assistant_session_api -v
"""
import base64
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402

ASSISTANT_TABLE = "feishu_assistant_sessions"
EVENTS_TABLE = "feishu_assistant_events"
EXPECTED_TOOLS = {"projects_status", "project_find", "req_query", "req_card",
                  "req_timeline", "send_requirement_cards", "sessions_status",
                  "pending_confirmations", "create_requirement",
                  # #26：文件/代码只读分析 3 + 已建卡状态变更 1；#30：需求卡清单卡片 1
                  # #54：项目代码同步 3（status/pull/commit）
                  "file_read", "file_find", "code_search", "set_requirement_status",
                  "project_git_status", "project_git_pull", "project_git_commit"}
FORBIDDEN_TOOLS = {"db_query", "read_file", "list_dir", "git_log", "git_status", "git_diff",
                   "search_text", "Bash", "Read", "Write", "Edit", "mysql_query",
                   "git_push", "git_merge", "git_checkout"}


class _FakeSession:
    """ACPSession 桩（捕获构造 kwargs，记录 cancel/close 调用与逐轮 prompt 参数）。"""

    last_kwargs = None
    last_args = None
    last_instance = None
    built = 0

    def __init__(self, *args, **kwargs):
        _FakeSession.built += 1
        _FakeSession.last_args = args
        _FakeSession.last_kwargs = kwargs
        _FakeSession.last_instance = self
        self.session_id = "sess-fake-1"
        self.model_name = "fake-model"
        self.cancelled = 0
        self.closed = 0
        self.prompts = []
        import types
        self.proc = types.SimpleNamespace(pid=1, poll=lambda: None)

    def start(self, workdir, resume_session_id=None):
        self.resume_session_id = resume_session_id
        return None

    def prompt(self, text, timeout=None, content=None):
        self.prompts.append({"text": text, "timeout": timeout, "content": content})

    def _flush_message(self):
        pass

    def cancel(self):
        self.cancelled += 1

    def close(self, kill=False):
        self.closed += 1


class _Stub:
    def close(self):
        pass

    def shutdown(self):
        pass

    def server_close(self):
        pass


class AssistantBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req24-asst-"))
        cls._old = (b.DB_PATH, b.PROJECT_PATHS, b.FEISHU_JSON_PATH, b.HEARTBEAT_FILE,
                    b.ACPSession)
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        b.PROJECT_PATHS = {"req-board": ["/tmp/req-board"], "other": ["/tmp/other"]}
        b.FEISHU_JSON_PATH = cls.tmp / "feishu.json"     # 不存在 = assistant_model 未配置
        b.HEARTBEAT_FILE = cls.tmp / "hb.json"
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        (b.DB_PATH, b.PROJECT_PATHS, b.FEISHU_JSON_PATH, b.HEARTBEAT_FILE,
         b.ACPSession) = cls._old
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            for t in ("chat_events", "chat_sessions", EVENTS_TABLE, ASSISTANT_TABLE,
                      "requirements", "dispatcher_events", "agent_sessions"):
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
        with b.assistant_lock:
            for rt in list(b.assistant_runtime.values()):
                b._assistant_close_runtime(rt)
            b.assistant_runtime.clear()
            b.assistant_booting.clear()
        self._old_thread = b._assistant_prompt_thread
        b._assistant_prompt_thread = self._record_thread
        self.thread_calls = []

    def tearDown(self):
        b._assistant_prompt_thread = self._old_thread

    def _record_thread(self, sid, text, turn_id, budget, attachments=None):
        self.thread_calls.append((sid, text, turn_id, budget, attachments))

    # ---- helpers ----
    def _create(self, chat="oc_chat_1", model=None):
        body = {"feishu_chat_id": chat}
        if model:
            body["model"] = model
        resp = self.client.post("/api/assistant/sessions", json=body)
        return resp

    def _row(self, sid):
        with b.get_db() as conn:
            return dict(conn.execute(f"SELECT * FROM {ASSISTANT_TABLE} WHERE id = ?",
                                     (sid,)).fetchone())

    def _insert_assistant(self, chat="oc_chat_1", status="idle", row_id=None, **extra):
        cols = {"feishu_chat_id": chat, "status": status, "created_at": b.now_str(),
                "updated_at": b.now_str()}
        cols.update(extra)
        if row_id is not None:
            cols = {"id": row_id, **cols}
        keys = list(cols)
        with b.get_db() as conn:
            cur = conn.execute(
                f"INSERT INTO {ASSISTANT_TABLE} ({', '.join(keys)}) "
                f"VALUES ({', '.join('?' * len(keys))})", tuple(cols[k] for k in keys))
            conn.commit()
            return cur.lastrowid

    def _insert_chat(self, chat_id=None, channel="feishu", project="req-board", **extra):
        cols = {"project": project, "status": "created", "channel": channel,
                "created_at": b.now_str(), "updated_at": b.now_str()}
        cols.update(extra)
        if chat_id is not None:
            cols = {"id": chat_id, **cols}
        keys = list(cols)
        with b.get_db() as conn:
            cur = conn.execute(
                f"INSERT INTO chat_sessions ({', '.join(keys)}) "
                f"VALUES ({', '.join('?' * len(keys))})", tuple(cols[k] for k in keys))
            conn.commit()
            return cur.lastrowid

    def _cols(self, table):
        with b.get_db() as conn:
            return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}


class SchemaTest(AssistantBase):
    def test_01_init_db_idempotent_and_new_tables(self):
        with b.get_db() as conn:
            before = conn.execute(f"SELECT COUNT(*) FROM {ASSISTANT_TABLE}").fetchone()[0]
        b.init_db()   # 重复初始化零副作用
        b.init_db()
        with b.get_db() as conn:
            after = conn.execute(f"SELECT COUNT(*) FROM {ASSISTANT_TABLE}").fetchone()[0]
            idx = {r[1] for r in conn.execute(f"PRAGMA index_list({ASSISTANT_TABLE})")}
            ev_idx = {r[1] for r in conn.execute(f"PRAGMA index_list({EVENTS_TABLE})")}
        self.assertEqual(before, after)
        self.assertIn("idx_feishu_assistant_active", idx)
        self.assertIn("idx_feishu_assistant_sessions_chat", idx)
        self.assertIn("idx_feishu_assistant_events_session", ev_idx)

    def test_02_assistant_table_shape(self):
        cols = self._cols(ASSISTANT_TABLE)
        # 无 project 字段（跨项目助手不污染项目语义）
        self.assertNotIn("project", cols)
        for name in ("id", "feishu_chat_id", "status", "session_id", "turn_id", "model",
                     "assistant_model_source", "awaiting_project", "pending_json",
                     "last_event_id", "error", "created_at", "updated_at"):
            self.assertIn(name, cols, name)
        # feishu_chat_id 显式 NOT NULL（空值不被部分唯一索引拦截）
        self.assertEqual(cols["feishu_chat_id"][3], 1)
        # 状态机不含旧表 created 终态语义
        self.assertNotIn("created", b.ASSISTANT_ACTIVE_STATUSES + b.ASSISTANT_TERMINAL_STATUSES)

    def test_03_legacy_chat_sessions_only_added_columns(self):
        legacy = self.tmp / "legacy24.db"
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "project TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'idle', "
                     "created_at TIMESTAMP, updated_at TIMESTAMP)")
        conn.execute("INSERT INTO chat_sessions (project, status) VALUES ('req-board', 'idle')")
        conn.commit()
        conn.close()
        old_db = b.DB_PATH
        try:
            b.DB_PATH = legacy
            b.init_db()
            b.init_db()   # 幂等：不重复 ALTER、不重建
            with b.get_db() as c:
                cols = {r[1]: r for r in c.execute("PRAGMA table_info(chat_sessions)")}
                row = dict(c.execute("SELECT * FROM chat_sessions WHERE id = 1").fetchone())
        finally:
            b.DB_PATH = old_db
        self.assertIn("chat_scope", cols)
        self.assertIn("feishu_chat_id", cols)
        self.assertEqual(row["chat_scope"], "chat")       # 存量行自动为 chat
        self.assertIsNone(row["feishu_chat_id"])
        self.assertEqual(row["project"], "req-board")     # 其余列逐列不变
        self.assertEqual(row["status"], "idle")
        self.assertEqual(cols["project"][3], 1)           # project NOT NULL 未被放宽

    def test_04_partial_unique_index_semantics(self):
        first = self._insert_assistant(chat="oc_u", status="idle")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_assistant(chat="oc_u", status="running")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_assistant(chat="oc_u", status="cancelling")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_assistant(chat="oc_u", status="interrupted")
        # closed / failed 为终态，释放唯一约束（同 chat 可保留历史多行）
        b._assistant_set(first, status="closed")
        second = self._insert_assistant(chat="oc_u", status="failed")
        third = self._insert_assistant(chat="oc_u", status="idle")
        self.assertEqual(len({first, second, third}), 3)

    def test_05_get_or_create_hits_integrity_error_and_rereads(self):
        existing = self._insert_assistant(chat="oc_race", status="idle")
        real = b._assistant_active_row
        calls = {"n": 0}

        def flaky(chat):
            calls["n"] += 1
            if calls["n"] == 1:
                return None          # 逼真并发：预查落空，插入撞唯一约束
            return real(chat)

        b._assistant_active_row = flaky
        try:
            row, created = b._assistant_get_or_create("oc_race")
        finally:
            b._assistant_active_row = real
        self.assertFalse(created)
        self.assertEqual(row["id"], existing)
        with b.get_db() as conn:
            n = conn.execute(f"SELECT COUNT(*) FROM {ASSISTANT_TABLE} WHERE feishu_chat_id = ?",
                             ("oc_race",)).fetchone()[0]
        self.assertEqual(n, 1)   # 未插第二行


class SessionApiTest(AssistantBase):
    def test_10_get_or_create_idempotent(self):
        r1 = self._create("oc_a")
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        d1 = r1.get_json()
        self.assertEqual(set(d1), {"id", "feishu_chat_id", "status", "session_id", "turn_id",
                                   "model"})
        self.assertEqual(d1["status"], "idle")
        r2 = self._create("oc_a")
        self.assertEqual(r2.get_json()["id"], d1["id"])
        # 缺 feishu_chat_id → 400
        self.assertEqual(self.client.post("/api/assistant/sessions", json={}).status_code, 400)

    def test_11_ownership_double_binding(self):
        sid = self._create("oc_a").get_json()["id"]
        self.assertEqual(self.client.get(f"/api/assistant/sessions/{sid}"
                                         f"?feishu_chat_id=oc_a").status_code, 200)
        self.assertEqual(self.client.get(f"/api/assistant/sessions/{sid}"
                                         f"?feishu_chat_id=oc_other").status_code, 404)
        self.assertEqual(self.client.get(f"/api/assistant/sessions/{sid}").status_code, 404)
        self.assertEqual(self.client.get("/api/assistant/sessions/99999"
                                         "?feishu_chat_id=oc_a").status_code, 404)
        for path in (f"/api/assistant/sessions/{sid}/events?feishu_chat_id=oc_other",
                     f"/api/assistant/sessions/{sid}/close",
                     f"/api/assistant/sessions/{sid}/cancel"):
            with self.subTest(path=path):
                if path.endswith("/close"):
                    resp = self.client.post(path, json={"feishu_chat_id": "oc_other"})
                elif path.endswith("/cancel"):
                    resp = self.client.post(path, json={"feishu_chat_id": "oc_other",
                                                        "turn_id": 1})
                else:
                    resp = self.client.get(path)
                self.assertEqual(resp.status_code, 404, path)
        resp = self.client.post(f"/api/assistant/sessions/{sid}/prompt",
                                json={"feishu_chat_id": "oc_other", "text": "hi",
                                      "remaining_sec": 30})
        self.assertEqual(resp.status_code, 404)

    def test_12_prompt_round_allocates_turn_and_writes_event(self):
        sid = self._create("oc_a").get_json()["id"]
        resp = self.client.post(f"/api/assistant/sessions/{sid}/prompt",
                                json={"feishu_chat_id": "oc_a", "text": "哪个项目在跑",
                                      "remaining_sec": 42})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        d = resp.get_json()
        self.assertEqual(d, {"id": sid, "turn_id": 1, "status": "running"})
        self.assertEqual(len(self.thread_calls), 1)
        self.assertEqual(self.thread_calls[0][1], "哪个项目在跑")
        self.assertEqual(self.thread_calls[0][3], 42)     # deadline 由 remaining_sec 驱动
        # 已在 running → 409
        resp2 = self.client.post(f"/api/assistant/sessions/{sid}/prompt",
                                 json={"feishu_chat_id": "oc_a", "text": "再来", "remaining_sec": 30})
        self.assertEqual(resp2.status_code, 409)
        # 事件接口：user 事件 + 会话快照
        ev = self.client.get(f"/api/assistant/sessions/{sid}/events?feishu_chat_id=oc_a").get_json()
        self.assertEqual([e["kind"] for e in ev["events"]], ["user"])
        self.assertEqual(ev["session"]["status"], "running")
        self.assertEqual(ev["session"]["turn_id"], 1)

    def test_13_prompt_rejects_empty_and_closed(self):
        sid = self._create("oc_a").get_json()["id"]
        self.assertEqual(self.client.post(f"/api/assistant/sessions/{sid}/prompt",
                                          json={"feishu_chat_id": "oc_a", "text": "  ",
                                                "remaining_sec": 30}).status_code, 400)
        self.client.post(f"/api/assistant/sessions/{sid}/close", json={"feishu_chat_id": "oc_a"})
        resp = self.client.post(f"/api/assistant/sessions/{sid}/prompt",
                                json={"feishu_chat_id": "oc_a", "text": "hi", "remaining_sec": 30})
        self.assertEqual(resp.status_code, 400)

    def test_14_close_terminal_and_releases(self):
        sid = self._create("oc_a").get_json()["id"]
        self.assertEqual(self.client.post(f"/api/assistant/sessions/{sid}/close",
                                          json={"feishu_chat_id": "oc_a"}).status_code, 200)
        self.assertEqual(self._row(sid)["status"], "closed")
        # 终态释放唯一约束：同 chat 可再建活跃会话
        sid2 = self._create("oc_a").get_json()["id"]
        self.assertNotEqual(sid, sid2)

    def test_15_cancel_contract(self):
        sid = self._create("oc_a").get_json()["id"]
        self.client.post(f"/api/assistant/sessions/{sid}/prompt",
                         json={"feishu_chat_id": "oc_a", "text": "x", "remaining_sec": 30})
        b._assistant_set(sid, session_id="sess-keep")
        fake = _FakeSession()
        with b.assistant_lock:
            b.assistant_runtime[sid] = {"session": fake, "logf": _Stub(), "mcp": _Stub()}
        # turn_id 不匹配 → 409，不取消
        self.assertEqual(self.client.post(f"/api/assistant/sessions/{sid}/cancel",
                                          json={"feishu_chat_id": "oc_a",
                                                "turn_id": 999}).status_code, 409)
        self.assertEqual(fake.cancelled, 0)
        # 缺 turn_id → 400
        self.assertEqual(self.client.post(f"/api/assistant/sessions/{sid}/cancel",
                                          json={"feishu_chat_id": "oc_a"}).status_code, 400)
        resp = self.client.post(f"/api/assistant/sessions/{sid}/cancel",
                                json={"feishu_chat_id": "oc_a", "turn_id": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True})
        row = self._row(sid)
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["session_id"], "sess-keep")   # ACP session_id 保留供 resume
        self.assertEqual(fake.closed, 1)
        with b.assistant_lock:
            self.assertNotIn(sid, b.assistant_runtime)
        kinds = [e["kind"] for e in self.client.get(
            f"/api/assistant/sessions/{sid}/events?feishu_chat_id=oc_a").get_json()["events"]]
        self.assertIn("system", kinds)

    def test_16_prompt_after_cancel_is_allowed(self):
        sid = self._create("oc_a").get_json()["id"]
        self.client.post(f"/api/assistant/sessions/{sid}/prompt",
                         json={"feishu_chat_id": "oc_a", "text": "x", "remaining_sec": 30})
        self.client.post(f"/api/assistant/sessions/{sid}/cancel",
                         json={"feishu_chat_id": "oc_a", "turn_id": 1})
        resp = self.client.post(f"/api/assistant/sessions/{sid}/prompt",
                                json={"feishu_chat_id": "oc_a", "text": "y", "remaining_sec": 30})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["turn_id"], 2)


class HistoryTest(AssistantBase):
    def test_20_aggregate_keeps_source_namespaces(self):
        # 两表自增 id 撞号：都显式给 id=1
        self._insert_assistant(chat="oc_h", status="idle", row_id=1)
        b._assistant_add_event(1, "message", "助手回复")
        self._insert_chat(chat_id=1, channel="feishu", project="req-board")
        b._chat_add_event(1, "message", "建卡回复")
        rows = self.client.get("/api/assistant/history").get_json()
        self.assertEqual(len(rows), 2)
        got = {(r["source"], r["id"]) for r in rows}
        self.assertEqual(got, {("assistant", 1), ("chat", 1)})
        by_src = {r["source"]: r for r in rows}
        self.assertEqual(by_src["assistant"]["project"], None)
        self.assertEqual(by_src["assistant"]["feishu_chat_id"], "oc_h")
        self.assertEqual(by_src["assistant"]["last_message"], "助手回复")
        self.assertEqual(by_src["chat"]["project"], "req-board")
        self.assertEqual(by_src["chat"]["last_message"], "建卡回复")

    def test_21_before_id_cursor(self):
        self._insert_assistant(chat="oc_h1", status="idle")
        self._insert_assistant(chat="oc_h2", status="idle")
        rows = self.client.get("/api/assistant/history").get_json()
        self.assertEqual(len(rows), 2)
        nxt = self.client.get(
            f"/api/assistant/history?before_id={rows[0]['source']}:{rows[0]['id']}").get_json()
        self.assertEqual(len(nxt), 1)
        self.assertEqual((nxt[0]["source"], nxt[0]["id"]),
                         (rows[1]["source"], rows[1]["id"]))

    def test_22_only_feishu_chat_rows_are_aggregated(self):
        self._insert_chat(chat_id=None, channel="web", project="req-board")
        rows = self.client.get("/api/assistant/history").get_json()
        self.assertEqual(rows, [])


class ToolFaceTest(AssistantBase):
    def setUp(self):
        super().setUp()
        self.server = b.AssistantMcpServer(1, "oc_t", turn_id=3)

    def tearDown(self):
        self.server.close()
        super().tearDown()

    def _rpc(self, payload):
        req = urllib.request.Request(
            self.server.url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def test_30_tools_list_is_fixed_and_readonly_safe(self):
        out = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in out["result"]["tools"]}
        self.assertEqual(names, EXPECTED_TOOLS)
        for bad in FORBIDDEN_TOOLS:
            self.assertNotIn(bad, names)
        self.assertEqual(set(self.server.ALL_TOOLS), EXPECTED_TOOLS)

    def test_31_unknown_tool_and_param_fail_closed(self):
        with self.assertRaises(ValueError):
            self.server.call("db_query", {})
        with self.assertRaises(ValueError):
            self.server.call("read_file", {"path": "/etc/passwd"})
        with self.assertRaises(ValueError):
            self.server.call("create_requirement",
                             {"project": "req-board", "title": "t", "bogus": 1})
        with self.assertRaises(ValueError):
            self.server.call("req_query", {"nope": 1})

    def test_32_create_requirement_project_status_and_default_later(self):
        with self.assertRaises(ValueError):
            self.server.call("create_requirement", {"project": "nope", "title": "t"})
        with self.assertRaises(ValueError):
            self.server.call("create_requirement",
                             {"project": "req-board", "title": "t", "status": "doing"})
        with self.assertRaises(ValueError):
            self.server.call("create_requirement", {"project": "req-board", "title": ""})
        out = json.loads(self.server.call("create_requirement",
                                          {"project": "req-board", "title": "缺省状态卡"}))
        self.assertTrue(out["created"])
        self.assertEqual(out["status"], "later")     # 缺省 later（不被调度器捞走）
        with b.get_db() as conn:
            row = conn.execute("SELECT status, source FROM requirements WHERE id = ?",
                               (out["id"],)).fetchone()
        self.assertEqual(row["status"], "later")
        self.assertEqual(row["source"], "board-feishu-assistant")

    def test_33_create_requirement_idempotent_same_turn_and_source_url(self):
        out1 = json.loads(self.server.call("create_requirement", {
            "project": "req-board", "title": "带链接卡", "notes": "要做的事",
            "source_url": "https://example.com/issue/1"}))
        out2 = json.loads(self.server.call("create_requirement", {
            "project": "req-board", "title": "带链接卡", "notes": "要做的事"}))
        self.assertFalse(out2["created"])
        self.assertEqual(out1["id"], out2["id"])     # 同轮幂等：不重复落卡
        with b.get_db() as conn:
            row = conn.execute("SELECT notes FROM requirements WHERE id = ?",
                               (out1["id"],)).fetchone()
        self.assertIn("https://example.com/issue/1", row["notes"])
        self.assertIn("要做的事", row["notes"])
        # 另一个轮次（同 chat）可建不同需求的卡（一 chat 多卡）
        other = b.AssistantMcpServer(1, "oc_t", turn_id=4)
        try:
            out3 = json.loads(other.call("create_requirement",
                                         {"project": "req-board", "title": "第 2 张"},
                                         ))
            self.assertTrue(out3["created"])
            self.assertNotEqual(out3["id"], out1["id"])
        finally:
            other.close()

    def test_34_open_only_when_explicit(self):
        out = json.loads(self.server.call("create_requirement",
                                          {"project": "req-board", "title": "要开发",
                                           "status": "open"}))
        self.assertEqual(out["status"], "open")
        with b.get_db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM requirements WHERE id = ?",
                                          (out["id"],)).fetchone()[0], "open")

    def test_35_no_requirement_leakage_of_reviews(self):
        with b.get_db() as conn:
            conn.execute("INSERT INTO req_reviews (req_id, round, reviewer, verdict, content)"
                         " VALUES (1, 1, 'r', 'pass', 'SECRET-REVIEW-BODY')")
            conn.commit()
        req = b.req_add("卡", "req-board", "notes", status="later")
        out = self.server.call("req_card", {"req_id": req["id"]})
        self.assertNotIn("SECRET-REVIEW-BODY", out)
        self.assertNotIn("req_reviews", out)

    def test_36_req_card_returns_current_node_products_and_elapsed(self):
        """#24 复修（TC-10）：「#N 到哪了」必须能答出当前节点、产物清单与耗时。
        节点取自流程快照 nodes_json/node_index（phase 是历史列、进行中的卡常为空）。"""
        nodes = json.dumps([{"role": "dev", "node_type": "code"},
                            {"role": "code_review", "node_type": "review"},
                            {"node_type": "terminal"}])
        req = b.req_add("卡", "req-board", "notes", status="doing")
        with b.get_db() as conn:
            conn.execute("UPDATE requirements SET nodes_json = ?, node_index = ?, "
                         "flow_name = 'heavy', started_at = ? WHERE id = ?",
                         (nodes, 1, "2026-10-12 10:00:00", req["id"]))
            conn.commit()
        art = Path(tempfile.mkdtemp(prefix="req24-art-"))
        d = art / "req-board" / f"req-{req['id']}"
        d.mkdir(parents=True)
        (d / "analysis.md").write_text("x", encoding="utf-8")
        old_art = b.ARTIFACTS_ROOT
        b.ARTIFACTS_ROOT = art
        try:
            out = json.loads(self.server.call("req_card", {"req_id": req["id"]}))
        finally:
            b.ARTIFACTS_ROOT = old_art
        self.assertEqual(out["node_index"], 1)                          # 当前节点取自流程快照
        self.assertEqual([n["node_type"] for n in out["nodes"]], ["code", "review", "terminal"])
        self.assertEqual(out["current_node"], b.role_meta("code_review", "display") or "code_review")
        self.assertIn("analysis.md", [p["filename"] for p in out["products"]])   # 产物清单
        self.assertIsInstance(out["elapsed_sec"], int)
        self.assertGreater(out["elapsed_sec"], 0)                       # started_at → now
        self.assertNotIn("SECRET", json.dumps(out))


class ACPProfileTest(AssistantBase):
    def test_40_boot_passes_tool_profile_and_isolated_roots(self):
        sid = self._insert_assistant(chat="oc_boot", status="running", turn_id=7, model=None)
        b.ACPSession = _FakeSession
        _FakeSession.built = 0
        try:
            rt = b._assistant_boot(sid, 7)
            self.assertIsNotNone(rt)
            self.assertEqual(_FakeSession.built, 1)
            kw = _FakeSession.last_kwargs
            self.assertEqual(kw["tool_profile"], "feishu_assistant")
            self.assertFalse(kw["allow_fs_write"])
            self.assertEqual(kw["extra_allow_prefixes"],
                             [f"mcp__{b.ASSISTANT_TOOL_SERVER}__"])
            servers = list(kw["extra_mcp_servers"])
            self.assertEqual([s["name"] for s in servers], [b.ASSISTANT_TOOL_SERVER])
            self.assertEqual(servers[0]["type"], "http")
            roots = list(kw["readonly_roots"])
            self.assertEqual(len(roots), 1)
            self.assertIn(str(b.ASSISTANT_TMP_ROOT), roots[0])   # 临时空目录，非仓库/非家目录
            self.assertNotEqual(Path(roots[0]), Path.home())
        finally:
            b.ACPSession = self._old[4]
            with b.assistant_lock:
                rt = b.assistant_runtime.pop(sid, None)
            if rt:
                b._assistant_close_runtime(rt)

    def test_41_boot_sets_session_id_not_closed(self):
        sid = self._insert_assistant(chat="oc_boot2", status="running", turn_id=1)
        b.ACPSession = _FakeSession
        _FakeSession.built = 0
        try:
            self.assertIsNotNone(b._assistant_boot(sid, 1))
            row = self._row(sid)
            self.assertEqual(row["session_id"], "sess-fake-1")
            self.assertEqual(row["status"], "running")   # boot 不把会话置终态
            # 按需 boot：存活会话直接复用，不重复拉起进程（防进程泄漏/孤儿）
            self.assertIsNotNone(b._assistant_boot(sid, 1))
            self.assertEqual(_FakeSession.built, 1)
        finally:
            b.ACPSession = self._old[4]
            with b.assistant_lock:
                rt = b.assistant_runtime.pop(sid, None)
            if rt:
                b._assistant_close_runtime(rt)

    def test_42_reuse_process_then_create_card_lands_new_card(self):
        """#24 复修：复用存活进程时必须把 MCP 的 turn_id 推进到本轮，否则 create_requirement
        幂等键停在首轮，同一 chat 第二次建卡命中旧卡、不落新卡。"""
        sid = self._insert_assistant(chat="oc_reuse", status="running", turn_id=1)
        b.ACPSession = _FakeSession
        _FakeSession.built = 0
        try:
            rt = b._assistant_boot(sid, 1)
            self.assertIsNotNone(rt)
            out1 = json.loads(rt["mcp"].call("create_requirement",
                                             {"project": "req-board", "title": "第 1 张"}))
            self.assertTrue(out1["created"])
            b._assistant_finish(sid, 1, "idle")                 # 正常收尾：进程保留在 runtime
            self.assertEqual(self._row(sid)["status"], "idle")

            b._assistant_set(sid, status="running", turn_id=2)  # 同一 chat 的第 2 轮
            rt2 = b._assistant_boot(sid, 2)
            self.assertIsNotNone(rt2)
            self.assertEqual(_FakeSession.built, 1)             # 复用，未重复拉起进程
            self.assertEqual(rt2["mcp"].turn_id, 2)             # 幂等键随本轮推进（复修点）
            out2 = json.loads(rt2["mcp"].call("create_requirement",
                                              {"project": "req-board", "title": "第 2 张"}))
            self.assertTrue(out2["created"])                    # 落新卡，而非返回旧卡
            self.assertNotEqual(out2["id"], out1["id"])
        finally:
            b.ACPSession = self._old[4]
            with b.assistant_lock:
                rt = b.assistant_runtime.pop(sid, None)
            if rt:
                b._assistant_close_runtime(rt)


class TurnFenceTest(AssistantBase):
    """turn_id 栅栏：旧线程晚到不得改新轮状态；超时文案只发一次。"""

    def test_50_stale_turn_does_not_touch_current_round(self):
        sid = self._insert_assistant(chat="oc_f", status="running", turn_id=2)
        b._assistant_finish(sid, 1, "idle")      # 旧轮次（turn_id=1）晚到
        self.assertEqual(self._row(sid)["status"], "running")
        b._assistant_finish(sid, 2, "idle")
        self.assertEqual(self._row(sid)["status"], "idle")

    def test_51_timeout_writes_fixed_text_once(self):
        sid = self._insert_assistant(chat="oc_t2", status="running", turn_id=5)
        b._assistant_finish(sid, 5, "interrupted", b.ASSISTANT_TIMEOUT_TEXT, kind="message",
                            error="本轮超时")
        b._assistant_finish(sid, 5, "interrupted", b.ASSISTANT_TIMEOUT_TEXT, kind="message",
                            error="本轮超时")
        with b.get_db() as conn:
            n = conn.execute(f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE "
                             "assistant_session_id = ? AND kind = 'message' AND content = ?",
                             (sid, b.ASSISTANT_TIMEOUT_TEXT)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(self._row(sid)["status"], "interrupted")

    def test_52_timeout_text_persisted_before_status_flip(self):
        """#24 复修：收尾必须先写 message 事件、后翻状态。dispatcher 每拍先读 status 再读 events，
        若状态先翻到 interrupted，调用方会看到「非 running」而收尾返回，超时文案永远发不出。"""
        sid = self._insert_assistant(chat="oc_t3", status="running", turn_id=5)
        observed = []
        orig_set = b._assistant_set

        def spy_set(s, **fields):
            if fields.get("status"):
                with b.get_db() as conn:
                    n = conn.execute(f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE "
                                     "assistant_session_id = ? AND kind = 'message' AND content = ?",
                                     (s, b.ASSISTANT_TIMEOUT_TEXT)).fetchone()[0]
                observed.append((fields["status"], n))
            return orig_set(s, **fields)

        b._assistant_set = spy_set
        try:
            b._assistant_finish(sid, 5, "interrupted", b.ASSISTANT_TIMEOUT_TEXT, kind="message",
                                error="本轮超时")
        finally:
            b._assistant_set = orig_set
        self.assertTrue(observed)
        self.assertEqual(observed[-1][0], "interrupted")
        self.assertEqual(observed[-1][1], 1)     # 状态翻转到 interrupted 时文案已落库


class CapabilityTest(AssistantBase):
    """#26 能力一（文件/代码只读分析，跨项目）+ 能力二（暂缓→待开发）。"""

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="req26-cap-"))
        self.pa, self.pb = self.root / "proj_a", self.root / "proj_b"
        for d in (self.pa, self.pb):
            (d / "src").mkdir(parents=True)
        (self.pa / "src" / "a.py").write_text("# alpha\nneedle_here = 1\n", encoding="utf-8")
        (self.pb / "src" / "b.py").write_text("x = 2\nneedle_here = 2\n", encoding="utf-8")
        self.pa.joinpath("many.txt").write_text(
            "".join(f"line{i}\n" for i in range(1, 2101)), encoding="utf-8")
        self._old_paths = b.PROJECT_PATHS
        b.PROJECT_PATHS = {"proj_a": [str(self.pa)], "proj_b": [str(self.pb)]}
        self._caps(file_tools=True, status_write=True)
        self.server = b.AssistantMcpServer(1, "oc_cap", turn_id=7)

    def tearDown(self):
        self.server.close()
        b.PROJECT_PATHS = self._old_paths
        b.FEISHU_JSON_PATH.unlink(missing_ok=True)
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    # ---- helpers ----
    def _caps(self, file_tools=None, status_write=None):
        raw = {}
        if b.FEISHU_JSON_PATH.exists():
            raw = json.loads(b.FEISHU_JSON_PATH.read_text(encoding="utf-8"))
        for k, v in ((b.ASSISTANT_CAP_FILE_TOOLS, file_tools),
                     (b.ASSISTANT_CAP_STATUS_WRITE, status_write)):
            if v is not None:
                raw[k] = v
        b.FEISHU_JSON_PATH.write_text(json.dumps(raw), encoding="utf-8")

    def _req_row(self, rid):
        with b.get_db() as conn:
            return dict(conn.execute("SELECT * FROM requirements WHERE id = ?", (rid,)).fetchone())

    def _log_rows(self, rid):
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM req_status_log WHERE req_id = ? ORDER BY id", (rid,))]

    def _events(self, rid):
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM dispatcher_events WHERE req_id = ? ORDER BY id", (rid,))]

    @staticmethod
    def _scanned(out):
        return int(re.search(r"扫描 (\d+) 个文件", out).group(1))

    def _touch(self, rid, status=None, minutes_ago=0):
        """直改 requirements 的 status/updated_at（构造「久置」现场用，绕过工具路径）。"""
        with b.get_db() as conn:
            conn.execute("UPDATE requirements SET status = COALESCE(?, status), updated_at = ? "
                         "WHERE id = ?",
                         (status, (datetime.now() - timedelta(minutes=minutes_ago))
                          .strftime("%Y-%m-%d %H:%M:%S"), rid))
            conn.commit()

    # ---- A 能力一 ----
    def test_a1_tools_list_is_13(self):
        names = {t["name"] for t in self.server._tool_defs()}
        self.assertEqual(names, EXPECTED_TOOLS)
        self.assertEqual(set(self.server.ALL_TOOLS), EXPECTED_TOOLS)
        for bad in FORBIDDEN_TOOLS:
            self.assertNotIn(bad, names)

    def test_a2_cap_off_refuses_without_field_names(self):
        b.FEISHU_JSON_PATH.unlink()
        for name, args in (("file_read", {"path": "src/a.py", "project": "proj_a"}),
                           ("file_find", {"pattern": "**/*.py"}),
                           ("code_search", {"keyword": "needle_here"}),
                           ("set_requirement_status", {"req_id": 1, "status": "open"})):
            with self.assertRaises(ValueError, msg=name) as cm:
                self.server.call(name, args)
            msg = str(cm.exception)
            self.assertIn("能力未开启", msg)
            self.assertIn("设置 → 通知渠道", msg)
            # #31 回绝语给出可照着走到的具体位置：哪个页面 / 哪个 tab / 哪个分组
            self.assertIn("飞书机器人", msg)
            self.assertIn("看板助手能力", msg)
            self.assertNotIn(b.ASSISTANT_CAP_FILE_TOOLS, msg)
            self.assertNotIn(b.ASSISTANT_CAP_STATUS_WRITE, msg)

    def test_a3_file_read_with_line_numbers(self):
        out = self.server.call("file_read", {"path": "src/a.py", "project": "proj_a"})
        self.assertIn("1\t# alpha", out)
        self.assertIn("2\tneedle_here = 1", out)
        # 断言**不编造行号**：# alpha 只有 2 行 → 恰好 2 行正文、无 3..200 的尾部空号
        body = out.split("\n")[1:]
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0], "1\t# alpha")
        self.assertEqual(body[1], "2\tneedle_here = 1")
        self.assertIn("lines=2", out.split("\n")[0])
        # 单行 offset 分页同样按真实行数截断 + 文件内 1-based 绝对行号
        paged = self.server.call("file_read", {"path": str(self.pa / "src" / "a.py"),
                                               "offset": 1, "max_lines": 50})
        self.assertEqual(paged.split("\n")[1:], ["2\tneedle_here = 1"])
        # offset 越过 EOF → 0 行，不垫空号
        eof = self.server.call("file_read", {"path": "src/a.py", "project": "proj_a",
                                             "offset": 10, "max_lines": 5})
        self.assertEqual(eof.split("\n")[1:], [""])
        self.assertIn("lines=0", eof.split("\n")[0])
        meta = self.server.call("file_read", {"path": "src/a.py", "project": "proj_a", "meta": True})
        self.assertIn("type=text", meta)
        self.assertIn("lines=2", meta)

    def test_a4_path_escape_fail_closed(self):
        for p in ("../outside.txt", "/etc/passwd", str(self.root / "nope.py"),
                  str(self.pb.parent / "proj_c" / "c.py")):
            with self.assertRaises(ValueError, msg=p):
                self.server.call("file_read", {"path": p, "project": "proj_a"})
        with self.assertRaises(ValueError):
            self.server.call("file_read", {"path": "src/a.py"})      # 相对路径须给 project
        with self.assertRaises(ValueError):
            self.server.call("file_find", {"pattern": "/etc/*"})
        with self.assertRaises(ValueError):
            self.server.call("file_find", {"pattern": "../*"})
        with self.assertRaises(ValueError):
            self.server.call("code_search", {"keyword": "x", "path": "src/a.py"})  # 无 project

    def test_a4b_symlink_escape_fail_closed(self):
        """助手入口（非只读面）的 symlink 逃逸：链到 roots 外的文件 / 目录一律拒。"""
        outside = self.root / "outside.txt"
        outside.write_text("outside_secret\n", encoding="utf-8")
        (self.pa / "link_out.txt").symlink_to(outside)
        (self.pa / "link_dir").symlink_to(self.root)
        for p in ("link_out.txt", "link_dir/outside.txt"):
            with self.assertRaises(ValueError, msg=p):
                self.server.call("file_read", {"path": p, "project": "proj_a"})
            with self.assertRaises(ValueError, msg=p):
                self.server.call("file_read", {"path": str(self.pa / p), "project": "proj_a"})
        found = self.server.call("file_find", {"pattern": "**/*", "project": "proj_a"})
        self.assertNotIn("link_out.txt", found)     # 符号链接不进列文件结果
        hits = self.server.call("code_search", {"keyword": "outside_secret", "project": "proj_a"})
        self.assertIn("无匹配", hits)               # 链接目标内容不被检索
        self.assertNotIn("link_out.txt", hits)

    def test_a5_sensitive_files(self):
        (self.pa / ".env").write_text("K=v\nneedle_here = 9\n", encoding="utf-8")
        (self.pa / "id_rsa").write_text("needle_here\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.server.call("file_read", {"path": ".env", "project": "proj_a"})
        with self.assertRaises(ValueError):
            self.server.call("file_read", {"path": "id_rsa", "project": "proj_a"})
        found = self.server.call("file_find", {"pattern": "**/*", "project": "proj_a"})
        self.assertNotIn(".env", found)
        self.assertNotIn("id_rsa", found)
        hits = self.server.call("code_search", {"keyword": "needle_here", "project": "proj_a"})
        self.assertNotIn(".env", hits)
        self.assertNotIn("id_rsa", hits)

    def test_a6_cross_project_scope(self):
        out = self.server.call("code_search", {"keyword": "needle_here"})
        self.assertIn("proj_a/src/a.py:2: needle_here = 1", out)
        self.assertIn("proj_b/src/b.py:2: needle_here = 2", out)
        one = self.server.call("code_search", {"keyword": "needle_here", "project": "proj_a"})
        self.assertIn("proj_a/src/a.py:2", one)
        self.assertNotIn("proj_b", one)
        found = self.server.call("file_find", {"pattern": "**/*.py"})
        self.assertIn("proj_a: src/a.py", found)
        self.assertIn("proj_b: src/b.py", found)
        narrow = self.server.call("code_search", {"keyword": "needle_here", "project": "proj_a",
                                                  "path": "src"})
        self.assertIn("proj_a/src/a.py:2", narrow)

    def test_a7_limits_split(self):
        big = self.pa / "big.txt"
        big.write_text("x" * (b.FILE_MAX_BYTES + 10), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.server.call("file_read", {"path": "big.txt", "project": "proj_a"})
        # 行数类：2000 合法（证明未被 _int_arg 的默认 hi=50 夹取）
        out = self.server.call("file_read", {"path": "many.txt", "project": "proj_a",
                                             "max_lines": b.FILE_MAX_LINES_LIMIT})
        lines = out.split("\n")[1:]
        self.assertIn("继续读用 offset=2000", lines[-1])        # C6：未读完给续读提示
        body = lines[:-1]
        self.assertEqual(len(body), b.FILE_MAX_LINES_LIMIT)
        self.assertTrue(body[-1].startswith("2000\t"))
        for bad in ({"max_lines": 0}, {"max_lines": b.FILE_MAX_LINES_LIMIT + 1},
                    {"max_lines": "10"}, {"offset": -1}, {"offset": "1"}):
            with self.assertRaises(ValueError, msg=bad):
                self.server.call("file_read", {"path": "many.txt", "project": "proj_a", **bad})
        # 条数类：上限内合法、越界拒（不静默夹取）
        self.server.call("code_search", {"keyword": "needle_here",
                                         "max_matches": b.SEARCH_MAX_MATCHES})
        with self.assertRaises(ValueError):
            self.server.call("code_search", {"keyword": "needle_here",
                                             "max_matches": b.SEARCH_MAX_MATCHES + 1})
        self.server.call("file_find", {"pattern": "**/*.py", "limit": b.DIR_MAX_ENTRIES})
        with self.assertRaises(ValueError):
            self.server.call("file_find", {"pattern": "**/*.py", "limit": b.DIR_MAX_ENTRIES + 1})

    def test_a8_cross_project_scan_budget_shared(self):
        """跨项目共用一份全局扫描预算：合计扫描数 ≤ 上限（不是每项目各一份）。"""
        old = b.ASSISTANT_SCAN_MAX_FILES_TOTAL
        b.ASSISTANT_SCAN_MAX_FILES_TOTAL = 3
        try:
            for i in range(4):
                (self.pa / "src" / f"f{i}.txt").write_text(f"budget_probe_{i}\n", encoding="utf-8")
                (self.pb / "src" / f"g{i}.txt").write_text(f"budget_probe_{i}\n", encoding="utf-8")
            out = self.server.call("code_search", {"keyword": "budget_probe"})
            self.assertIn("截断", out)
            self.assertLessEqual(self._scanned(out), 3)
            self.assertIn("proj_a/src/f", out)      # 预算内确实命中
            self.assertNotIn("proj_b", out)          # 预算耗尽后不再扫下一个项目
            found = self.server.call("file_find", {"pattern": "**/f*.txt"})
            self.assertLessEqual(self._scanned(found), 3)
            self.assertIn("截断", found)
        finally:
            b.ASSISTANT_SCAN_MAX_FILES_TOTAL = old

    def test_a6b_multi_root_project_scans_all_paths(self):
        """每个项目的**全部** paths 都进检索（不只首个根）——「数据范围 = 项目清单全部路径」。"""
        r1, r2 = self.root / "c_repo1", self.root / "c_repo2"
        for d in (r1, r2):
            (d / "src").mkdir(parents=True)
        (r1 / "src" / "one.py").write_text("multiroot_token = 1\n", encoding="utf-8")
        (r2 / "src" / "two.py").write_text("multiroot_token = 2\n", encoding="utf-8")
        b.PROJECT_PATHS = dict(b.PROJECT_PATHS, proj_c=[str(r1), str(r2)])
        try:
            hits = self.server.call("code_search", {"keyword": "multiroot_token"})
            self.assertIn("proj_c/c_repo1/src/one.py:1: multiroot_token = 1", hits)
            self.assertIn("proj_c/c_repo2/src/two.py:1: multiroot_token = 2", hits)
            one = self.server.call("code_search", {"keyword": "multiroot_token", "project": "proj_c"})
            self.assertIn("proj_c/c_repo1/src/one.py:1", one)
            self.assertIn("proj_c/c_repo2/src/two.py:1", one)     # 第二个根不被漏掉
            found = self.server.call("file_find", {"pattern": "**/two.py", "project": "proj_c"})
            self.assertIn("proj_c/c_repo2: src/two.py", found)
            read = self.server.call("file_read", {"path": "src/two.py", "project": "proj_c"})
            self.assertIn("1\tmultiroot_token = 2", read)         # 相对路径按根逐个试解析
        finally:
            b.PROJECT_PATHS = {"proj_a": [str(self.pa)], "proj_b": [str(self.pb)]}

    def test_a8b_scan_deadline(self):
        """墙钟时限：到期即停并保留已得结果（不把单轮拖到 90s 被取消）。"""
        real_search, real_mono = b.ro_search_hits, b.time.monotonic
        state = {"expired": False}

        def fake_mono():
            return real_mono() + (9999.0 if state["expired"] else 0.0)

        def wrapped(target, kw, max_matches, budget, deadline=None):
            out = real_search(target, kw, max_matches, budget, deadline)
            state["expired"] = True          # 首个目标扫完后「时限到」
            return out

        b.time.monotonic, b.ro_search_hits = fake_mono, wrapped
        try:
            out = self.server.call("code_search", {"keyword": "needle_here"})
        finally:
            b.time.monotonic, b.ro_search_hits = real_mono, real_search
        self.assertIn("proj_a/src/a.py:2", out)     # 时限内已得结果保留
        self.assertIn("截断", out)
        self.assertNotIn("proj_b", out)             # 到期后不再扫下一个项目
        # 调用前就已超时：如实说「扫描未完成」，不谎报「无匹配」
        old = b.ASSISTANT_SCAN_DEADLINE_SEC
        b.ASSISTANT_SCAN_DEADLINE_SEC = 0
        try:
            none = self.server.call("file_find", {"pattern": "**/*.py"})
        finally:
            b.ASSISTANT_SCAN_DEADLINE_SEC = old
        self.assertIn("扫描未完成", none)

    def test_a9_unknown_params_and_tools(self):
        with self.assertRaises(ValueError):
            self.server.call("file_find", {"pattern": "**/*.py", "bogus": 1})
        with self.assertRaises(ValueError):
            self.server.call("read_file", {"path": str(self.pa / "src" / "a.py")})
        with self.assertRaises(ValueError):
            self.server.call("code_search", {"keyword": ""})

    def test_a10_cap_read_per_call(self):
        self._caps(file_tools=False)
        with self.assertRaises(ValueError):
            self.server.call("file_find", {"pattern": "**/*.py"})
        self._caps(file_tools=True)
        self.assertIn("a.py", self.server.call("file_find", {"pattern": "**/*.py"}))  # 同一实例即时生效

    def test_a11_http_tools_call_end_to_end(self):
        """经真实 MCP HTTP 通道调新工具（不只直调 Python 方法）。"""
        payload = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                   "params": {"name": "file_find", "arguments": {"pattern": "**/*.py"}}}
        req = urllib.request.Request(self.server.url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode())
        text = out["result"]["content"][0]["text"]
        self.assertIn("proj_a: src/a.py", text)
        self.assertIn("proj_b: src/b.py", text)

    # ---- B 能力二 ----
    def test_b1_later_to_open_success_and_audit(self):
        rid = b.req_add("待开发化", "proj_a", "n", status="later")["id"]
        self._touch(rid, minutes_ago=2)          # 久置卡：证明 updated_at 被刷新（非同一秒巧合）
        before = self._req_row(rid)
        out = json.loads(self.server.call("set_requirement_status",
                                          {"req_id": rid, "status": "open"}))
        self.assertTrue(out["changed"])
        self.assertEqual(out["old_status"], "later")
        self.assertEqual(out["status"], "open")
        self.assertIn("自动", out["warning"])
        after = self._req_row(rid)
        self.assertEqual(after["status"], "open")
        self.assertNotEqual(after["updated_at"], before["updated_at"])
        self.assertIsNone(after["started_at"])
        self.assertIsNone(after["completed_at"])
        logs = self._log_rows(rid)
        self.assertEqual(len(logs), 1)
        self.assertEqual((logs[0]["old_status"], logs[0]["new_status"], logs[0]["actor"]),
                         ("later", "open", "agent"))
        self.assertIn("oc_cap", logs[0]["detail"])
        self.assertIn("agent:feishu-assistant", logs[0]["detail"])
        ev = self._events(rid)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "status")

    def test_b2_query_reflects_new_status(self):
        rid = b.req_add("查询回读", "proj_a", "n", status="later")["id"]
        self.server.call("set_requirement_status", {"req_id": rid, "status": "open"})
        card = json.loads(self.server.call("req_card", {"req_id": rid}))
        self.assertEqual(card["status"], "open")
        rows = json.loads(self.server.call("req_query", {"status": "open"}))
        self.assertIn(rid, [r["id"] for r in rows["requirements"]])

    def test_b3_idempotent_keeps_updated_at(self):
        rid = b.req_add("已开放卡", "proj_a", "n", status="open")["id"]
        before = self._req_row(rid)
        out = json.loads(self.server.call("set_requirement_status",
                                          {"req_id": rid, "status": "open"}))
        self.assertFalse(out["changed"])
        self.assertEqual(self._req_row(rid)["updated_at"], before["updated_at"])
        self.assertEqual(self._log_rows(rid), [])
        self.assertEqual(self._events(rid), [])

    def test_b4_illegal_combinations_zero_change(self):
        rid = b.req_add("进行中卡", "proj_a", "n", status="doing")["id"]
        before = self._req_row(rid)
        for args in ({"req_id": rid, "status": "open"},
                     {"req_id": rid, "status": "later"},
                     {"req_id": 999999, "status": "open"},
                     {"req_id": rid, "status": "doing"},
                     {"req_id": rid},
                     {"req_id": rid, "status": "open", "bogus": 1},
                     {"req_id": "1", "status": "open"}):
            with self.assertRaises(ValueError, msg=args):
                self.server.call("set_requirement_status", args)
        self.assertEqual(self._req_row(rid), before)
        self.assertEqual(self._log_rows(rid), [])

    def test_b5_cap_off_refuses_zero_change(self):
        rid = b.req_add("开关卡", "proj_a", "n", status="later")["id"]
        before = self._req_row(rid)
        self._caps(status_write=False)
        with self.assertRaises(ValueError) as cm:
            self.server.call("set_requirement_status", {"req_id": rid, "status": "open"})
        self.assertIn("能力未开启", str(cm.exception))
        self.assertEqual(self._req_row(rid), before)
        self.assertEqual(self._log_rows(rid), [])
        self.assertEqual(self._events(rid), [])

    def test_b6_other_columns_untouched(self):
        rid = b.req_add("字段卡", "proj_a", "notes 原文", status="later")["id"]
        before = self._req_row(rid)
        self.server.call("set_requirement_status", {"req_id": rid, "status": "open"})
        after = self._req_row(rid)
        skip = {"status", "updated_at", "started_at", "completed_at"}
        self.assertEqual({k: v for k, v in before.items() if k not in skip},
                         {k: v for k, v in after.items() if k not in skip})

    def test_b7_quiet_period_recomputed(self):
        """#26：静默期依据 updated_at（对齐 dispatcher.get_open_reqs 的 MIN_AGE_MINUTES=1）。"""
        rid = b.req_add("静默卡", "proj_a", "n", status="later")["id"]
        # 反事实：open 且 updated_at 陈旧 2 分钟 → 会被调度器立即捞取
        self._touch(rid, status="open", minutes_ago=2)
        self.assertTrue(self._would_be_picked(rid))
        # 目标路径：久置 later 卡改 open → 静默期从改状态时刻起算，不会立即被捞取
        self._touch(rid, status="later", minutes_ago=2)
        self.server.call("set_requirement_status", {"req_id": rid, "status": "open"})
        self.assertFalse(self._would_be_picked(rid))

    def _would_be_picked(self, rid):
        row = self._req_row(rid)
        if row["status"] != "open":
            return False
        age = (datetime.now() - datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S"))
        return age.total_seconds() >= 60


class ReqCardsTest(AssistantBase):
    """#30 能力三：需求卡清单卡片（服务端取数/中文映射/分页/开关）。"""

    def setUp(self):
        super().setUp()
        self._set_cap(True)
        self.server = b.AssistantMcpServer(1, "oc_cards", turn_id=5)

    def tearDown(self):
        self.server.close()
        b.FEISHU_JSON_PATH.unlink(missing_ok=True)
        super().tearDown()

    # ---- helpers ----
    def _set_cap(self, on):
        raw = {}
        if b.FEISHU_JSON_PATH.exists():
            raw = json.loads(b.FEISHU_JSON_PATH.read_text(encoding="utf-8"))
        if on is not None:
            raw[b.ASSISTANT_CAP_REQ_CARDS] = on
        b.FEISHU_JSON_PATH.write_text(json.dumps(raw), encoding="utf-8")

    def _mk(self, title, status="later", flow_name=None, project="req-board"):
        rid = b.req_add(title, project, "n", status=status)["id"]
        if flow_name is not None:
            with b.get_db() as conn:
                conn.execute("UPDATE requirements SET flow_name = ? WHERE id = ?", (flow_name, rid))
                conn.commit()
        return rid

    def _pages(self):
        with b.get_db() as conn:
            rows = conn.execute("SELECT id, content FROM feishu_assistant_events "
                                "WHERE assistant_session_id = 1 AND kind = 'card' ORDER BY id").fetchall()
        return [json.loads(r["content"]) for r in rows]

    def _ids(self):
        return [it["id"] for p in self._pages() for it in p["items"]]

    # ---- A 工具面与开关 ----
    def test_c1_tool_in_face_and_readonly(self):
        self.assertIn("send_requirement_cards", self.server.READONLY_TOOLS)
        self.assertNotIn("send_requirement_cards", self.server.WRITE_TOOLS)
        self.assertIn("send_requirement_cards", {t["name"] for t in self.server._tool_defs()})

    def test_c2_cap_off_refuses_zero_side_effect(self):
        b.FEISHU_JSON_PATH.unlink(missing_ok=True)
        self._mk("卡")
        with self.assertRaises(ValueError) as cm:
            self.server.call("send_requirement_cards", {"project": "req-board"})
        msg = str(cm.exception)
        self.assertIn("能力未开启", msg)
        self.assertIn("设置 → 通知渠道", msg)
        self.assertIn("飞书机器人", msg)          # #31 具体位置：tab 名
        self.assertIn("看板助手能力", msg)         # #31 具体位置：分组名
        self.assertNotIn(b.ASSISTANT_CAP_REQ_CARDS, msg)
        self.assertEqual(self._pages(), [])

    def test_c3_cap_read_per_call(self):
        self._mk("卡")
        self._set_cap(False)
        with self.assertRaises(ValueError):
            self.server.call("send_requirement_cards", {"project": "req-board"})
        self._set_cap(True)   # 同一 server 实例内即时生效
        self.assertIn("sent_pages", self.server.call("send_requirement_cards", {"project": "req-board"}))

    def test_c4_bad_args_and_project_rejected(self):
        self._mk("卡")
        for args in ({"project": "req-board", "bogus": 1}, {}, {"project": "no_such_project"},
                     {"project": "req-board", "status": "open,weird"}):
            with self.assertRaises(ValueError, msg=args):
                self.server.call("send_requirement_cards", args)
        self.assertEqual(self._pages(), [])

    def test_c5_http_tools_call_end_to_end(self):
        self._mk("HTTP 卡")
        payload = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                   "params": {"name": "send_requirement_cards",
                              "arguments": {"project": "req-board"}}}
        req = urllib.request.Request(self.server.url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode())
        text = out["result"]["content"][0]["text"]
        self.assertIn("sent_pages", text)
        self.assertEqual(len(self._ids()), 1)

    # ---- B 数据正确性 ----
    def test_c6_ids_and_titles_match_db(self):
        made = [self._mk(f"卡{i}") for i in range(3)]
        self.server.call("send_requirement_cards", {"project": "req-board"})
        rows = {r["id"]: r for r in b.req_list("req-board", None)}
        self.assertEqual(sorted(self._ids()), sorted(made))
        titles = {it["id"]: it["title"] for p in self._pages() for it in p["items"]}
        for rid in made:
            self.assertEqual(titles[rid], rows[rid]["title"])

    def test_c7_status_label_chinese_single_source(self):
        rid = self._mk("进行中卡", status="doing")
        self.server.call("send_requirement_cards", {"project": "req-board"})
        item = next(it for p in self._pages() for it in p["items"] if it["id"] == rid)
        self.assertEqual(item["status"], "doing")     # 英文码仍原样
        self.assertEqual(item["status_label"], b.STATUS_LABEL_CN["doing"])
        self.assertEqual(item["status_label"], "进行中")

    def test_c8_flow_snapshot_and_null_fallback(self):
        r1 = self._mk("有流程", flow_name="复杂")
        r2 = self._mk("存量卡")   # flow_name 为 NULL
        self.server.call("send_requirement_cards", {"project": "req-board"})
        flow = {it["id"]: it["flow"] for p in self._pages() for it in p["items"]}
        self.assertEqual(flow[r1], "复杂")            # 快照原样透传，不做 id→显示名二次映射
        self.assertEqual(flow[r2], b.ASSISTANT_CARD_FLOW_FALLBACK)
        self.assertEqual(flow[r2], "未指定")

    def test_c9_updated_at_from_db(self):
        rid = self._mk("时间卡")
        row = next(r for r in b.req_list("req-board", None) if r["id"] == rid)
        self.server.call("send_requirement_cards", {"project": "req-board"})
        item = next(it for p in self._pages() for it in p["items"] if it["id"] == rid)
        self.assertEqual(item["updated_at"], row["updated_at"])

    # ---- C 分页与不丢卡 ----
    def test_c11_pagination_no_loss(self):
        made = [self._mk(f"卡{i}") for i in range(45)]
        out = json.loads(self.server.call("send_requirement_cards", {"project": "req-board"}))
        self.assertEqual(out["total_items"], 45)
        self.assertEqual(out["sent_pages"], 3)         # ceil(45/20)
        self.assertEqual(sorted(self._ids()), sorted(made))   # 无重无漏
        pages = self._pages()
        self.assertEqual([p["page"] for p in pages], [1, 2, 3])
        self.assertTrue(all(p["pages"] == 3 and p["total_cards"] == 45 for p in pages))

    def test_c12_page_payload_within_budget(self):
        for i in range(30):
            self._mk("长标题" * 20 + str(i))
        self.server.call("send_requirement_cards", {"project": "req-board"})
        for p in self._pages():
            self.assertLessEqual(len(json.dumps(p, ensure_ascii=False)),
                                 b.ASSISTANT_CARD_PAGE_MAX)

    def test_c13_event_content_not_truncated(self):
        for i in range(30):
            self._mk("标题" * 30 + str(i))
        self.server.call("send_requirement_cards", {"project": "req-board"})
        with b.get_db() as conn:
            contents = [r["content"] for r in conn.execute(
                "SELECT content FROM feishu_assistant_events WHERE kind = 'card' ORDER BY id")]
        for c in contents:
            self.assertEqual(c, json.dumps(json.loads(c), ensure_ascii=False))  # 可解析且未被截断

    def test_c14_beyond_page_cap_not_silent(self):
        for i in range(205):
            self._mk(f"卡{i}")
        out = json.loads(self.server.call("send_requirement_cards", {"project": "req-board"}))
        pages = self._pages()
        self.assertEqual(len(pages), b.ASSISTANT_CARD_MAX_PAGES)
        self.assertEqual(out["total_items"], 205)
        self.assertTrue(out["truncated"])
        last = pages[-1]
        self.assertTrue(last["truncated"])
        self.assertEqual(last["remaining"], 205 - sum(len(p["items"]) for p in pages))

    def test_c15_status_and_keyword_filter(self):
        open_ids = [self._mk(f"过滤卡{i}", status="open") for i in range(2)]
        self._mk("过滤卡 later", status="later")
        self._mk("别的标题", status="open")
        out = json.loads(self.server.call("send_requirement_cards",
                                          {"project": "req-board", "status": "open",
                                           "keyword": "过滤卡"}))
        self.assertEqual(sorted(self._ids()), sorted(open_ids))
        self.assertEqual(out["total_items"], 2)

    def test_c16_empty_result_explicit(self):
        out = json.loads(self.server.call("send_requirement_cards", {"project": "other"}))
        pages = self._pages()
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["items"], [])
        self.assertEqual(out["total_items"], 0)
        self.assertIn("没有符合条件", out["note"])

    def test_c17_no_result_max_50_cap(self):
        for i in range(60):
            self._mk(f"卡{i}")
        out = json.loads(self.server.call("send_requirement_cards", {"project": "req-board"}))
        self.assertEqual(out["total_items"], 60)      # 未走 _int_arg / ASSISTANT_RESULT_MAX=50 夹取
        self.assertEqual(len(self._ids()), 60)


def _git(repo, *args, check=True):
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {r.stderr}")
    return r


def _init_repo(path, body="base\n"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Tester")
    (path / "file.txt").write_text(body, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    _git(path, "branch", "-M", "main")
    return path


class GitToolsTest(AssistantBase):
    """#54 助手工具面：能力开关 / 候选 / 拉取 / 提交两阶段 / 冲突 payload / 预算。"""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="req54-tools-"))
        self.repo = _init_repo(self.tmp / "proj")
        self._old_paths = b.PROJECT_PATHS
        b.PROJECT_PATHS = {"req-board": ["/tmp/req-board"], "gitproj": [str(self.repo)]}
        self._cap(True)

    def tearDown(self):
        b.PROJECT_PATHS = self._old_paths
        b.FEISHU_JSON_PATH.unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _cap(self, on):
        data = {}
        if b.FEISHU_JSON_PATH.exists():
            data = json.loads(b.FEISHU_JSON_PATH.read_text(encoding="utf-8"))
        data["assistant_git_ops"] = on
        b.FEISHU_JSON_PATH.write_text(json.dumps(data), encoding="utf-8")

    def _server(self, turn_id=1):
        return b.AssistantMcpServer(999, "oc_git", turn_id=turn_id)

    def _pair(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        _git(self.repo, "remote", "add", "origin", str(remote))
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        _git(other, "config", "user.email", "t2@example.com")
        _git(other, "config", "user.name", "Other")
        return other

    def _advance(self, other, content="remote\n"):
        (other / "file.txt").write_text(content, encoding="utf-8")
        _git(other, "commit", "-q", "-am", "remote change")
        _git(other, "push", "-q", "origin", "main")

    def test_d1_tools_list_16_and_no_forbidden(self):
        server = self._server()
        try:
            names = {t["name"] for t in server._tool_defs()}
            self.assertEqual(names, EXPECTED_TOOLS)
            self.assertEqual(set(server.ALL_TOOLS), EXPECTED_TOOLS)
            self.assertFalse(names & FORBIDDEN_TOOLS)
            self.assertFalse({"project_git_status", "project_git_pull",
                              "project_git_commit"} & FORBIDDEN_TOOLS)
        finally:
            server.close()

    def test_d2_cap_off_refuses_without_field_names(self):
        self._cap(False)
        server = self._server()
        try:
            for name in ("project_git_status", "project_git_pull", "project_git_commit"):
                with self.assertRaises(ValueError) as ctx:
                    server.call(name, {"project": "gitproj"})
                msg = str(ctx.exception)
                self.assertIn("能力未开启", msg)
                self.assertIn("看板助手能力", msg)
                self.assertNotIn("assistant_git_ops", msg)
        finally:
            server.close()

    def test_d3_missing_and_unknown_project_list_candidates(self):
        server = self._server()
        try:
            for args in ({}, {"project": ""}):
                with self.assertRaises(ValueError) as ctx:
                    server.call("project_git_status", args)
                self.assertIn("候选项目", str(ctx.exception))
                self.assertIn("gitproj", str(ctx.exception))
            with self.assertRaises(ValueError) as ctx:
                server.call("project_git_status", {"project": "nope"})
            self.assertIn("未知项目", str(ctx.exception))
        finally:
            server.close()

    def test_d4_multi_repo_without_repo_lists_candidates(self):
        b.PROJECT_PATHS = dict(b.PROJECT_PATHS, gitproj=[str(self.repo), str(self.tmp / "r2")])
        server = self._server()
        try:
            with self.assertRaises(ValueError) as ctx:
                server.call("project_git_status", {"project": "gitproj"})
            self.assertIn("候选仓库", str(ctx.exception))
        finally:
            server.close()

    def test_d5_status_and_pull_fast_forward(self):
        other = self._pair()
        before = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self._advance(other)
        server = self._server()
        try:
            st = json.loads(server.call("project_git_status", {"project": "gitproj"}))
            self.assertEqual(st["status"], "ok")
            self.assertEqual(st["branch"], "main")
            self.assertTrue(st["head"]["short"])
            out = json.loads(server.call("project_git_pull", {"project": "gitproj"}))
            self.assertEqual(out["status"], "pulled")
            self.assertEqual(out["before"]["sha"], before)
            self.assertNotEqual(out["after"]["sha"], before)
            self.assertEqual((self.repo / "file.txt").read_text(), "remote\n")
        finally:
            server.close()

    def test_d6_commit_two_phase_and_single_use(self):
        (self.repo / "file.txt").write_text("changed\n", encoding="utf-8")
        server = self._server(turn_id=1)
        try:
            pv = json.loads(server.call("project_git_commit",
                                        {"project": "gitproj", "message": "msg"}))
            self.assertEqual(pv["status"], "preview")
            token = pv["confirm_token"]
            self.assertTrue(token)
            with self.assertRaises(ValueError) as ctx:
                server.call("project_git_commit", {"project": "gitproj", "message": "msg",
                                                   "confirm_token": token})
            self.assertIn("下一轮", str(ctx.exception))
            server.turn_id = 2
            out = json.loads(server.call("project_git_commit",
                                         {"project": "gitproj", "message": "msg",
                                          "confirm_token": token}))
            self.assertEqual(out["status"], "committed")
            self.assertEqual(out["commit"]["subject"], "msg")
            with self.assertRaises(ValueError):
                server.call("project_git_commit", {"project": "gitproj", "message": "msg",
                                                   "confirm_token": token})
        finally:
            server.close()

    def test_d7_token_not_cross_instance(self):
        (self.repo / "file.txt").write_text("changed\n", encoding="utf-8")
        one = self._server(turn_id=1)
        pv = json.loads(one.call("project_git_commit",
                                 {"project": "gitproj", "message": "msg"}))
        one.close()
        two = self._server(turn_id=2)
        try:
            with self.assertRaises(ValueError):
                two.call("project_git_commit", {"project": "gitproj", "message": "msg",
                                                "confirm_token": pv["confirm_token"]})
        finally:
            two.close()

    def test_d7b_branch_switch_after_preview_rejected(self):
        (self.repo / "file.txt").write_text("changed\n", encoding="utf-8")
        server = self._server(turn_id=1)
        try:
            pv = json.loads(server.call("project_git_commit",
                                        {"project": "gitproj", "message": "msg"}))
            _git(self.repo, "checkout", "-q", "-b", "alt")
            server.turn_id = 2
            with self.assertRaises(ValueError) as ctx:
                server.call("project_git_commit", {"project": "gitproj", "message": "msg",
                                                   "confirm_token": pv["confirm_token"]})
            self.assertIn("分支", str(ctx.exception))
            self.assertEqual(_git(self.repo, "rev-parse", "--abbrev-ref",
                                  "HEAD").stdout.strip(), "alt")
            self.assertIn("M file.txt", _git(self.repo, "status", "--porcelain").stdout)
        finally:
            server.close()

    def test_d7c_git_failure_structured_via_mcp(self):
        net = _init_repo(self.tmp / "net")
        _git(net, "remote", "add", "origin",
             "https://user:supersecret@127.0.0.1:1/nope.git")
        b.PROJECT_PATHS = dict(b.PROJECT_PATHS, netproj=[str(net)])
        server = self._server()
        try:
            out = json.loads(server.call("project_git_pull", {"project": "netproj"}))
            self.assertEqual(out["status"], "failed")
            self.assertTrue(out["error"])
            self.assertNotIn("supersecret", json.dumps(out, ensure_ascii=False))
        finally:
            server.close()

    def test_d8_conflict_payload_and_resolve(self):
        other = self._pair()
        (self.repo / "file.txt").write_text("ours\n", encoding="utf-8")
        _git(self.repo, "commit", "-q", "-am", "ours")
        self._advance(other, "theirs\n")
        server = self._server()
        try:
            out = json.loads(server.call("project_git_pull", {"project": "gitproj"}))
            self.assertEqual(out["status"], "conflict")
            self.assertTrue(out["in_progress"])
            self.assertEqual(out["files"][0]["code"], "UU")
            self.assertEqual(len(out["options"]), 4)
            self.assertIn("ours", out["options"][0])
            resolved = json.loads(server.call("project_git_pull",
                                              {"project": "gitproj", "action": "resolve",
                                               "side": "theirs"}))
            self.assertEqual(resolved["status"], "resolved")
            self.assertEqual((self.repo / "file.txt").read_text(), "theirs\n")
            aborted = json.loads(server.call("project_git_pull",
                                             {"project": "gitproj", "action": "abort"}))
            self.assertEqual(aborted["status"], "aborted")
        finally:
            server.close()

    def test_d9_turn_deadline_rejects_low_budget_and_close_cancels(self):
        server = self._server()
        try:
            server.turn_deadline = time.monotonic() + 2
            out = json.loads(server.call("project_git_status", {"project": "gitproj"}))
            self.assertEqual(out["status"], "failed")
            self.assertIn("剩余时间不足", out["error"])
            self.assertFalse(server.cancel_event.is_set())
        finally:
            server.close()
        self.assertTrue(server.cancel_event.is_set())


class PromptHashTest(AssistantBase):
    """#54 prompt_hash：历史 NULL/文件变化 → 重建会话并强制注入；未变 → resume 不重复注入。"""

    def _seed_message(self, sid):
        b._assistant_add_event(sid, "message", "历史回复")

    def _run(self, sid, text="hi"):
        old = b.ACPSession
        _FakeSession.built = 0
        b.ACPSession = _FakeSession
        try:
            self._old_thread(sid, text, 1, 30)
        finally:
            b.ACPSession = old
            with b.assistant_lock:
                rt = b.assistant_runtime.pop(sid, None)
            if rt:
                b._assistant_close_runtime(rt)
        return _FakeSession.last_instance

    def test_70_null_or_changed_hash_rebuilds_and_injects(self):
        sid = self._insert_assistant(chat="oc_hash", status="idle",
                                     session_id="old-sid", prompt_hash=None)
        self._seed_message(sid)
        b._assistant_set(sid, status="running", turn_id=1)
        inst = self._run(sid)
        row = self._row(sid)
        self.assertIsNone(inst.resume_session_id)         # 不 resume 旧上下文
        self.assertEqual(row["session_id"], "sess-fake-1")
        self.assertEqual(row["prompt_hash"], b._assistant_prompt_hash())
        self.assertTrue(inst.prompts[0]["text"].startswith(b._assistant_system_prompt()))

    def test_71_unchanged_hash_resumes_without_reinject(self):
        sid = self._insert_assistant(chat="oc_hash2", status="idle",
                                     session_id="sess-old",
                                     prompt_hash=b._assistant_prompt_hash())
        self._seed_message(sid)
        b._assistant_set(sid, status="running", turn_id=1)
        inst = self._run(sid, "继续")
        self.assertEqual(inst.resume_session_id, "sess-old")
        self.assertEqual(inst.prompts[0]["text"], "继续")


class PromptCapTest(unittest.TestCase):
    """#26/#30 提示词同步（F1）：含新能力边界与中文能力名，不泄配置字段名、不含旧句。"""

    def test_prompt_has_new_caps_without_field_names(self):
        text = b.ASSISTANT_PROMPT_FILE.read_text(encoding="utf-8")
        for must in ("代码只读分析", "状态变更", "需求卡清单", "设置 → 通知渠道", "飞书机器人",
                     "看板助手能力", "暂缓 → 待开发", "行号",
                     # #54
                     "代码拉取与提交", "project_git_status", "project_git_pull",
                     "project_git_commit", "保留我方(ours)", "保留对方(theirs)",
                     "下一轮", "不 push", "冲突文件清单"):
            self.assertIn(must, text, must)
        for bad in ("assistant_file_tools", "assistant_status_write", "assistant_req_cards",
                    "assistant_git_ops", "你没有读写文件"):
            self.assertNotIn(bad, text)
        # 旧口径：禁止调用 git / 固定 13 个
        self.assertNotIn("不要调用未授权工具（通用数据库、执行命令、git 等）", text)


class AssistantAttachTest(AssistantBase):
    """#44 助手附件（图片/文件）：端点契约（校验类出口零落盘）/ 落盘根取值 / prompt 组装 /
    file_read 附件根例外 / user 事件留痕。"""

    def setUp(self):
        super().setUp()
        self._old_materials = b.MATERIALS_ROOT
        b.MATERIALS_ROOT = Path(self.tmp) / "materials"

    def tearDown(self):
        b.MATERIALS_ROOT = self._old_materials
        b.FEISHU_JSON_PATH.unlink(missing_ok=True)
        super().tearDown()

    # ---- helpers ----
    def _b64(self, data):
        return base64.b64encode(data).decode()

    def _att(self, name="a.txt", data=b"hello"):
        return {"name": name, "content_b64": self._b64(data)}

    def _prompt(self, sid, text="看看这个", attachments=None, chat="oc_a", **extra):
        body = {"feishu_chat_id": chat, "text": text, "remaining_sec": 30}
        if attachments is not None:
            body["attachments"] = attachments
        body.update(extra)
        return self.client.post(f"/api/assistant/sessions/{sid}/prompt", json=body)

    def _events(self, sid, chat="oc_a"):
        return self.client.get(
            f"/api/assistant/sessions/{sid}/events?feishu_chat_id={chat}").get_json()["events"]

    def _attach_dir(self, sid):
        return Path(self.tmp) / "materials" / "_assistant" / f"asst-{sid}"

    def _feishu_json(self, **kv):
        raw = {}
        if b.FEISHU_JSON_PATH.exists():
            raw = json.loads(b.FEISHU_JSON_PATH.read_text(encoding="utf-8"))
        raw.update(kv)
        b.FEISHU_JSON_PATH.write_text(json.dumps(raw), encoding="utf-8")

    def _run_real_thread(self, sid, text, items, turn_id=1):
        """跑真实 _assistant_prompt_thread（AssistantBase 只把 board 上的名字换成桩，
        原始函数保存在 self._old_thread），用 _FakeSession 捕获 content blocks。"""
        b._assistant_set(sid, status="running", turn_id=turn_id)     # 轮次栅栏：boot 才认这一轮
        b.ACPSession = _FakeSession
        try:
            self._old_thread(sid, text, turn_id, 30, items)
        finally:
            b.ACPSession = self._old[4]
            with b.assistant_lock:
                rt = b.assistant_runtime.pop(sid, None)
            if rt:
                b._assistant_close_runtime(rt)

    # ---- 端点契约 / 落盘 ----
    def test_24_attachment_round_saves_file_and_event(self):
        sid = self._create("oc_a").get_json()["id"]
        resp = self._prompt(sid, attachments=[self._att("会议纪要.txt", b"hello world")])
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["turn_id"], 1)
        self.assertEqual(len(self.thread_calls), 1)
        atts = self.thread_calls[0][4]
        self.assertEqual([a["filename"] for a in atts], ["会议纪要.txt"])
        self.assertEqual(atts[0]["kind"], "text")
        p = Path(atts[0]["path"])
        self.assertTrue(p.is_file())
        self.assertEqual(p.read_bytes(), b"hello world")
        self.assertTrue(p.resolve().is_relative_to(self._attach_dir(sid).resolve()))
        ev = self._events(sid)
        self.assertEqual([e["kind"] for e in ev], ["user"])
        self.assertEqual(ev[0]["content"], "[附件] 会议纪要.txt（文本）\n\n看看这个")

    def test_24b_plain_round_unchanged(self):
        sid = self._create("oc_a").get_json()["id"]
        self.assertEqual(self._prompt(sid, text="hi").status_code, 200)
        self.assertEqual(self.thread_calls[0][4], None)
        self.assertEqual(self._events(sid)[0]["content"], "hi")
        self.assertFalse(self._attach_dir(sid).exists())

    def test_29_illegal_names_400_zero_side_effect(self):
        sid = self._create("oc_a").get_json()["id"]
        for name in ("../x.txt", "a/b.txt", "x", "x.exe", "a" * 300 + ".txt", "a\x01.txt"):
            with self.subTest(name=name):
                resp = self._prompt(sid, attachments=[self._att(name)])
                self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIsNone(self._row(sid)["turn_id"])
        self.assertEqual(self._events(sid), [])
        self.assertEqual(self.thread_calls, [])
        self.assertFalse(self._attach_dir(sid).exists())

    def test_30_shape_count_b64_size_400(self):
        sid = self._create("oc_a").get_json()["id"]
        cases = [
            ("非数组", {"a": 1}),
            ("元素非对象", ["a.txt"]),
            ("缺 content_b64", [{"name": "a.txt"}]),
            ("content_b64 非字符串", [{"name": "a.txt", "content_b64": 5}]),
            ("非法 base64", [{"name": "a.txt", "content_b64": "!!!"}]),
            ("数量超限", [self._att(f"a{i}.txt") for i in range(6)]),
            ("单文件超限", [self._att("big.txt", b"x" * (20 * 1024 * 1024 + 1))]),
        ]
        for label, atts in cases:
            with self.subTest(label=label):
                resp = self._prompt(sid, attachments=atts)
                self.assertEqual(resp.status_code, 400, f"{label}: {resp.get_data(as_text=True)}")
        self.assertIsNone(self._row(sid)["turn_id"])
        self.assertEqual(self.thread_calls, [])
        self.assertFalse(self._attach_dir(sid).exists())

    def test_31_body_limit_413_early_exit(self):
        sid = self._create("oc_a").get_json()["id"]
        body = b"x" * (b.FEISHU_MATERIAL_BODY_LIMIT + 1)
        resp = self.client.post(f"/api/assistant/sessions/{sid}/prompt", data=body,
                                content_type="application/json")
        self.assertEqual(resp.status_code, 413)
        self.assertIsNone(self._row(sid)["turn_id"])
        self.assertFalse(self._attach_dir(sid).exists())

    def test_34_ownership_404_no_disk(self):
        sid = self._create("oc_a").get_json()["id"]
        resp = self._prompt(sid, chat="oc_other", attachments=[self._att()])
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(self._attach_dir(sid).exists())

    def test_35_running_409_no_disk(self):
        sid = self._create("oc_a").get_json()["id"]
        self._prompt(sid, text="第一轮")
        resp = self._prompt(sid, text="第二轮", attachments=[self._att()])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(self.thread_calls), 1)          # 未起第二轮
        self.assertFalse((self._attach_dir(sid) / "a.txt").exists())

    def test_32_same_name_uploads_do_not_overwrite(self):
        sid = self._create("oc_a").get_json()["id"]
        self._prompt(sid, attachments=[self._att("a.txt", b"first")])
        b._assistant_set(sid, status="idle")                 # 手动收尾，起第二轮
        self._prompt(sid, attachments=[self._att("a.txt", b"second")])
        names = [a["filename"] for a in self.thread_calls[1][4]]
        self.assertEqual(names, ["a-1.txt"])
        self.assertEqual((self._attach_dir(sid) / "a.txt").read_bytes(), b"first")
        self.assertEqual((self._attach_dir(sid) / "a-1.txt").read_bytes(), b"second")

    def test_33_each_round_carries_only_this_round(self):
        sid = self._create("oc_a").get_json()["id"]
        self._prompt(sid, attachments=[self._att("first.txt", b"1")])
        b._assistant_set(sid, status="idle")
        self._prompt(sid, attachments=[self._att("second.txt", b"2")])
        self.assertEqual([a["filename"] for a in self.thread_calls[0][4]], ["first.txt"])
        self.assertEqual([a["filename"] for a in self.thread_calls[1][4]], ["second.txt"])

    def test_77_write_failure_midway_400_keeps_first_file(self):
        import board_modules.blueprints.assistant as bpmod
        sid = self._create("oc_a").get_json()["id"]
        real = bpmod._write_material_to_dir
        calls = {"n": 0}

        def _flaky(d, name, data):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real(d, name, data)

        with mock.patch.object(bpmod, "_write_material_to_dir", _flaky):
            resp = self._prompt(sid, attachments=[self._att("ok.txt", b"1"),
                                                 self._att("bad.txt", b"2")])
        self.assertEqual(resp.status_code, 400)
        row = self._row(sid)
        self.assertIsNone(row["turn_id"])
        self.assertEqual(row["status"], "idle")
        self.assertEqual(self.thread_calls, [])
        self.assertEqual(self._events(sid), [])
        self.assertTrue((self._attach_dir(sid) / "ok.txt").is_file())   # 允许残留、不进 prompt

    # ---- prompt 组装（图片 / 文本 / 仅路径 / 档位） ----
    def test_25_26_28_attach_blocks_by_kind(self):
        sid = self._create("oc_a").get_json()["id"]
        self._feishu_json(assistant_vision=False)              # #52 显式关闭读图，单独覆盖降级档位
        items = b._assistant_save_attachments(sid, [
            {"name": "pic.png", "data": b"\x89PNG-bytes"},
            {"name": "notes.md", "data": b"# title\nbody"},
            {"name": "doc.pdf", "data": b"%PDF-1.4"},
        ])
        self.assertEqual([i["kind"] for i in items], ["image", "text", "path_only"])
        blocks = b._assistant_attach_blocks(sid, items)
        self.assertEqual(len(blocks), 1)                      # 显式关闭：图片不产生 image block
        text = blocks[0]["text"]
        self.assertIn("notes.md", text)
        self.assertIn("# title", text)                        # 正文内联（不依赖代码只读开关）
        self.assertNotIn(base64.b64encode(b"\x89PNG-bytes").decode(), text)
        self.assertIn("doc.pdf", text)
        self.assertIn(items[2]["path"], text)                  # 仅路径：给绝对路径
        self.assertNotIn("%PDF", text)                        # 不内联二进制正文

    def test_27_text_truncated_with_path_hint(self):
        sid = self._create("oc_a").get_json()["id"]
        items = b._assistant_save_attachments(sid, [{"name": "big.txt", "data": b"y" * 25000}])
        text = b._assistant_attach_blocks(sid, items)[0]["text"]
        self.assertIn("已截断，完整文件见", text)
        self.assertIn(items[0]["path"], text)
        self.assertEqual(len(text.split("\n")[1]), b.ASSISTANT_ATTACH_TEXT_MAX)   # 正文按上限截断

    def test_73_vision_switch_default_on_and_explicit_off(self):
        """#52 缺省开启：无 assistant_vision 键 → image block；显式 false → 同一张图走仅路径。"""
        sid = self._create("oc_a").get_json()["id"]
        items = b._assistant_save_attachments(sid, [{"name": "pic.png", "data": b"PNGBYTES"}])
        b.ACPSession = _FakeSession
        b.FEISHU_JSON_PATH.unlink(missing_ok=True)            # 缺键 = 缺省开启
        self._run_real_thread(sid, "看图", items)
        content = _FakeSession.last_instance.prompts[0]["content"]
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["mimeType"], "image/png")
        self.assertEqual(content[1]["data"], self._b64(b"PNGBYTES"))
        self.assertNotIn(self._b64(b"PNGBYTES"), content[0]["text"])
        # 显式 false → 走仅路径（同一张图不再进 image block，保留既有降级文案）
        self._feishu_json(assistant_vision=False)
        self._run_real_thread(sid, "再看", items, turn_id=2)
        content2 = _FakeSession.last_instance.prompts[0]["content"]
        self.assertEqual([c["type"] for c in content2], ["text", "text"])   # 仅路径：无 image block
        self.assertIn(items[0]["path"], content2[1]["text"])
        self.assertIn("助手当前未开启读图", content2[1]["text"])
        self.assertNotIn(self._b64(b"PNGBYTES"), content2[1]["text"])

    def test_74_missing_or_corrupt_defaults_on(self):
        """#52 缺省开启：文件缺失/损坏/非对象均按开启处理，只有显式 false 才关闭。"""
        b.FEISHU_JSON_PATH.unlink(missing_ok=True)
        self.assertTrue(b._assistant_vision_enabled())
        b.FEISHU_JSON_PATH.write_text("{ not json", encoding="utf-8")
        self.assertTrue(b._assistant_vision_enabled())
        b.FEISHU_JSON_PATH.write_text(json.dumps(["not", "object"]), encoding="utf-8")
        self.assertTrue(b._assistant_vision_enabled())
        b.FEISHU_JSON_PATH.write_text(json.dumps({"assistant_vision": False}), encoding="utf-8")
        self.assertFalse(b._assistant_vision_enabled())
        b.FEISHU_JSON_PATH.write_text(json.dumps({"assistant_vision": True}), encoding="utf-8")
        self.assertTrue(b._assistant_vision_enabled())

    def test_61_user_event_has_attachment_list_and_text(self):
        sid = self._create("oc_a").get_json()["id"]
        self._prompt(sid, text="这两份你看看",
                     attachments=[self._att("a.png", b"P"), self._att("b.txt", b"T")])
        content = self._events(sid)[0]["content"]
        self.assertTrue(content.startswith("[附件] a.png（图片）\n[附件] b.txt（文本）\n\n这两份你看看"),
                        content)
        self.assertNotIn(self._b64(b"P"), content)

    # ---- file_read 附件根例外 ----
    def _cap_on(self):
        self._feishu_json(**{b.ASSISTANT_CAP_FILE_TOOLS: True})

    def test_36_37_file_read_attachment_root_without_cap(self):
        sid = self._create("oc_a").get_json()["id"]
        items = b._assistant_save_attachments(sid, [{"name": "log.txt", "data": b"line1\nline2\n"}])
        server = b.AssistantMcpServer(sid, "oc_a", turn_id=1)
        try:
            out = server.tool_file_read({"path": items[0]["path"]})       # 开关关闭也能读
            self.assertIn("line1", out)
            self.assertIn("文件:", out)
            with self.assertRaises(ValueError) as cm:
                server.tool_file_read({"path": "/tmp/whatever.txt"})       # 项目文件仍被开关拦住
            self.assertIn("能力未开启", str(cm.exception))
        finally:
            server.close()

    def test_38_39_40_76_attachment_root_not_widened(self):
        sid = self._create("oc_a").get_json()["id"]
        other = self._insert_assistant(chat="oc_b", status="idle")
        mine = b._assistant_save_attachments(sid, [{"name": "ok.txt", "data": b"x"}])
        theirs = b._assistant_save_attachments(other, [{"name": "se.crt", "data": b"x"}])
        env = self._attach_dir(sid) / ".env"
        env.write_text("SECRET=1\n", encoding="utf-8")
        link = self._attach_dir(sid) / "link.txt"
        link.symlink_to("/etc/hosts")
        self._cap_on()
        server = b.AssistantMcpServer(sid, "oc_a", turn_id=1)
        try:
            # 跨会话（非本会话附件根）→ 越权/非法路径
            with self.assertRaises(ValueError) as cm:
                server.tool_file_read({"path": theirs[0]["path"]})
            self.assertIn("越权/非法路径", str(cm.exception))
            # 符号链接逃逸 → resolve 后不在附件根内 → 越权/非法路径
            with self.assertRaises(ValueError) as cm:
                server.tool_file_read({"path": str(link)})
            self.assertIn("越权/非法路径", str(cm.exception))
            # 附件目录内敏感文件名 → 黑名单拒绝
            with self.assertRaises(ValueError) as cm:
                server.tool_file_read({"path": str(env)})
            self.assertIn("敏感文件", str(cm.exception))
            # 本会话附件根正常读
            self.assertIn("x", server.tool_file_read({"path": mine[0]["path"]}))
            # 项目文件（cap 开）越出项目根 → 拒绝；file_find 不因附件根放开
            with self.assertRaises(ValueError) as cm:
                server.tool_file_read({"path": mine[0]["path"],
                                       "project": "req-board"})
            self.assertIn("越权/非法路径", str(cm.exception))
            found = server.tool_file_find({"pattern": "*.txt"})
            self.assertNotIn("ok.txt", found)
            self.assertNotIn("asst-", found)
        finally:
            server.close()

    def test_41_file_read_size_limit_in_attachment_root(self):
        sid = self._create("oc_a").get_json()["id"]
        items = b._assistant_save_attachments(
            sid, [{"name": "big.bin", "data": b"z" * (b.FILE_MAX_BYTES + 1)}])
        server = b.AssistantMcpServer(sid, "oc_a", turn_id=1)
        try:
            with self.assertRaises(ValueError) as cm:
                server.tool_file_read({"path": items[0]["path"]})
            self.assertIn("上限", str(cm.exception))
        finally:
            server.close()

    def test_66_materials_root_taken_at_call_time(self):
        sid = self._create("oc_a").get_json()["id"]
        items = b._assistant_save_attachments(sid, [{"name": "a.txt", "data": b"1"}])
        self.assertTrue(Path(items[0]["path"]).resolve().is_relative_to(
            Path(b.MATERIALS_ROOT).resolve()))
        self.assertIn("_assistant", items[0]["path"])
        b.MATERIALS_ROOT = Path(self.tmp) / "materials2"
        items2 = b._assistant_save_attachments(sid, [{"name": "b.txt", "data": b"2"}])
        self.assertTrue(Path(items2[0]["path"]).resolve().is_relative_to(
            Path(b.MATERIALS_ROOT).resolve()))                 # 调用期取值，非 import 期常量
        b.MATERIALS_ROOT = Path(self.tmp) / "materials"


if __name__ == "__main__":
    unittest.main()
