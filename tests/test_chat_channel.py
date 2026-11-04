#!/usr/bin/env python3
"""#22 建卡会话 channel 隔离测试（迁移 / POST channel / GET ?channel= / 分页 / 并行名额）。

对应技术方案用例 21-31：
21 POST {channel:'feishu'} → 201 且行 channel='feishu'；22 缺省 web / 非法 400；
23 ?channel=feishu|web 各自只返回对应通道；24/25 不带 channel 时不含 feishu（fail-closed）；
26 ?channel=all 逃生口；27 ?channel=feishu&limit=2 分页 + limit 越界 clamp；
28 ?project=X 无 limit/status 走原活跃优先固定分支；29 (project,channel) 维度的唯一活跃判定；
30 CHAT_PARALLEL_LIMIT 全局共用；31 旧 schema 迁移出 channel 列且存量行 'web'。

隔离：临时 DB + 临时 PROJECT_PATHS + 打桩 _chat_boot（不起真实 kimi）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_chat_channel -v
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402


class ChatChannelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req22-channel-"))
        cls._old_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._old_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            conn.execute("DELETE FROM chat_events")
            conn.execute("DELETE FROM chat_sessions")
            conn.commit()
        with b.chat_lock:
            b.chat_runtime.clear()
            b.chat_booting.clear()
        self._old_paths = b.PROJECT_PATHS
        b.PROJECT_PATHS = {"req-board": ["/tmp/req-board"], "other": ["/tmp/other"]}
        self._old_boot = b._chat_boot
        b._chat_boot = self._fake_boot

    def tearDown(self):
        b.PROJECT_PATHS = self._old_paths
        b._chat_boot = self._old_boot

    def _fake_boot(self, chat_id, project, resume_session_id=None):
        b._chat_set(chat_id, status="idle", session_id="s-1", model="test",
                    thinking="high", interrupt_source=None, error=None)

    def _insert(self, **fields):
        vals = {"project": "req-board", "status": "idle", "channel": "web",
                "session_id": "s-1", "created_at": b.now_str(), "updated_at": b.now_str(),
                "interrupt_source": None}
        vals.update(fields)
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO chat_sessions (project, status, channel, session_id, "
                "interrupt_source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (vals["project"], vals["status"], vals["channel"], vals["session_id"],
                 vals["interrupt_source"], vals["created_at"], vals["updated_at"]))
            chat_id = cur.lastrowid
            conn.commit()
        return chat_id

    def _row(self, chat_id):
        with b.get_db() as conn:
            return dict(conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone())

    def _list(self, qs=""):
        resp = self.client.get("/api/chat" + (("?" + qs) if qs else ""))
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    # ---- 21/22 POST channel ----

    def test_21_post_feishu_channel(self):
        resp = self.client.post("/api/chat", json={"project": "req-board", "channel": "feishu"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        cid = resp.get_json()["id"]
        self.assertEqual(self._row(cid)["channel"], "feishu")

    def test_22_default_web_and_illegal_rejected(self):
        resp = self.client.post("/api/chat", json={"project": "req-board"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self._row(resp.get_json()["id"])["channel"], "web")
        # 空串缺省 web
        resp2 = self.client.post("/api/chat", json={"project": "other", "channel": ""})
        self.assertEqual(resp2.status_code, 201)
        self.assertEqual(self._row(resp2.get_json()["id"])["channel"], "web")
        # 非法 channel → 400 且不落行
        before = len(self._list("channel=all"))
        resp3 = self.client.post("/api/chat", json={"project": "req-board", "channel": "sms"})
        self.assertEqual(resp3.status_code, 400)
        self.assertEqual(len(self._list("channel=all")), before)

    # ---- 23-26 GET channel 过滤 ----

    def test_23_channel_filter(self):
        w = self._insert(channel="web")
        f = self._insert(channel="feishu")
        self.assertEqual({r["id"] for r in self._list("channel=feishu")}, {f})
        self.assertEqual({r["id"] for r in self._list("channel=web")}, {w})

    def test_24_project_without_channel_excludes_feishu(self):
        w = self._insert(channel="web", project="req-board")
        self._insert(channel="feishu", project="req-board")
        rows = self._list("project=req-board")
        self.assertEqual({r["id"] for r in rows}, {w})

    def test_25_no_params_excludes_feishu(self):
        w = self._insert(channel="web")
        self._insert(channel="feishu")
        self.assertEqual({r["id"] for r in self._list()}, {w})

    def test_26_channel_all_escape_hatch(self):
        w = self._insert(channel="web")
        f = self._insert(channel="feishu")
        self.assertEqual({r["id"] for r in self._list("channel=all")}, {w, f})
        # 未知 channel 值回落 web（fail-closed）
        self.assertEqual({r["id"] for r in self._list("channel=bogus")}, {w})

    # ---- 27 分页 ----

    def test_27_feishu_pagination(self):
        ids = [self._insert(channel="feishu") for _ in range(3)]
        page1 = self._list("channel=feishu&limit=2")
        self.assertEqual([r["id"] for r in page1], [ids[2], ids[1]])
        page2 = self._list(f"channel=feishu&limit=2&before_id={min(r['id'] for r in page1)}")
        self.assertEqual([r["id"] for r in page2], [ids[0]])
        # limit 越界 → clamp（0 → 1；超大 → 50 上限，行数不足时返回全部）
        self.assertEqual(len(self._list("channel=feishu&limit=0")), 1)
        self.assertEqual(len(self._list("channel=feishu&limit=999")), 3)
        # 非数字 → 缺省 20（3 条全返回）
        self.assertEqual(len(self._list("channel=feishu&limit=abc")), 3)

    def test_28_project_regression_active_first(self):
        active = self._insert(project="req-board", status="idle", channel="web")
        for _ in range(25):
            self._insert(project="req-board", status="created", channel="web")
        rows = self._list("project=req-board")
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["id"], active)   # 活跃优先

    # ---- 29/30 唯一活跃判定与并行名额 ----

    def test_29_unique_active_per_project_channel(self):
        self._insert(project="req-board", status="idle", channel="web")
        # 网页会话不妨碍飞书建会话
        resp = self.client.post("/api/chat", json={"project": "req-board", "channel": "feishu"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        # 同通道重复 → 409
        resp2 = self.client.post("/api/chat", json={"project": "req-board", "channel": "feishu"})
        self.assertEqual(resp2.status_code, 409)
        resp3 = self.client.post("/api/chat", json={"project": "req-board", "channel": "web"})
        self.assertEqual(resp3.status_code, 409)

    def test_30_parallel_limit_shared(self):
        for i in range(b.CHAT_PARALLEL_LIMIT):
            self._insert(project=f"p{i}", status="running", channel="web")
        resp = self.client.post("/api/chat", json={"project": "other", "channel": "feishu"})
        self.assertEqual(resp.status_code, 429)
        self.assertIn("并行", resp.get_json()["error"])

    # ---- 32/33 自动恢复按 channel 过滤（只恢复 web 会话，⑦）----

    def test_32_recover_only_web_channel(self):
        web = self._insert(channel="web", status="running")
        feishu = self._insert(channel="feishu", status="running")
        b._recover_chat_sessions()
        self.assertEqual(self._row(web)["status"], "interrupted")
        self.assertEqual(self._row(feishu)["status"], "running")   # 飞书会话不被标中断

    def test_33_auto_resume_skips_feishu(self):
        self._insert(channel="feishu", status="interrupted", session_id="s-f",
                     interrupt_source="restart")
        old = b._spawn_resume_round
        calls = []
        b._spawn_resume_round = lambda *a, **k: calls.append(a)
        try:
            b._auto_resume_chat_session()
            self.assertEqual(calls, [])           # 只有飞书中断行 → 不拉起恢复
            web = self._insert(channel="web", status="interrupted", session_id="s-w",
                               interrupt_source="restart")
            b._auto_resume_chat_session()
        finally:
            b._spawn_resume_round = old
        self.assertEqual([a[0] for a in calls], [web])   # web 中断行照旧被拉起

    # ---- 31 迁移 ----

    def test_31_migration_adds_channel(self):
        legacy = self.tmp / "legacy.db"
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
            with b.get_db() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(chat_sessions)")}
                row = c.execute("SELECT channel FROM chat_sessions WHERE id = 1").fetchone()
                idx = {r[1] for r in c.execute("PRAGMA index_list(chat_sessions)")}
            self.assertIn("channel", cols)
            self.assertEqual(row["channel"], "web")
            self.assertIn("idx_chat_sessions_channel", idx)
        finally:
            b.DB_PATH = old_db


if __name__ == "__main__":
    unittest.main()
