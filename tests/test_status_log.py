#!/usr/bin/env python3
"""#375 需求状态变更审计测试：req_status_log 建表 / baseline 幂等快照 / PATCH 写点（old/new 比较）
/ X-Actor actor 判定（dispatcher/manual/agent/api）/ 仅改 notes 不写行 / status-log 查询接口。

运行：
    .venv/bin/python -m unittest tests.test_status_log -v
"""
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402


class StatusLogUpdateTest(unittest.TestCase):
    """PATCH 写点与 actor 判定（临时 DB 隔离，不碰真实 data/board.db）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-status-log-"))
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _log(self, req_id):
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM req_status_log WHERE req_id = ? ORDER BY id", (req_id,)).fetchall()]

    def _events(self, req_id):
        """#410 调度回放事件（kind=status 等）"""
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM dispatcher_events WHERE req_id = ? ORDER BY id", (req_id,)).fetchall()]

    def _create(self, status="later"):
        r = self.client.post("/api/requirements", json={
            "title": "状态审计卡", "project": "req-board", "status": status})
        self.assertEqual(r.status_code, 201)
        return r.get_json()

    def _patch(self, req_id, body, headers=None):
        return self.client.patch(f"/api/requirements/{req_id}", json=body, headers=headers or {})

    def test_table_exists(self):
        with b.get_db() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='req_status_log'").fetchone()
        self.assertIsNotNone(row)

    def test_create_no_log(self):
        """建卡不写 log 行（建卡 status 非变更）"""
        it = self._create()
        self.assertEqual(self._log(it["id"]), [])

    def test_patch_status_no_header_goes_api_with_ua(self):
        """验收 3：无头 PATCH 落 api 桶，detail 含 UA"""
        it = self._create()
        r = self._patch(it["id"], {"status": "open"}, {"User-Agent": "curl/8.6.0"})
        self.assertEqual(r.status_code, 200)
        rows = self._log(it["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "api")
        self.assertEqual(rows[0]["old_status"], "later")
        self.assertEqual(rows[0]["new_status"], "open")
        self.assertIn("curl/8.6.0", rows[0]["detail"])

    def test_patch_dispatcher_header(self):
        """验收 2：X-Actor: dispatcher 落 dispatcher 桶"""
        it = self._create()
        self._patch(it["id"], {"status": "open"}, {"X-Actor": "dispatcher"})
        rows = self._log(it["id"])
        self.assertEqual(rows[-1]["actor"], "dispatcher")
        self.assertIn("X-Actor=dispatcher", rows[-1]["detail"])

    def test_patch_manual_header(self):
        """验收 1：X-Actor: manual:<操作名> 落 manual 桶，detail 含操作名"""
        it = self._create()
        self._patch(it["id"], {"status": "open"}, {"X-Actor": "manual:下拉改状态"})
        rows = self._log(it["id"])
        self.assertEqual(rows[-1]["actor"], "manual")
        self.assertIn("manual:下拉改状态", rows[-1]["detail"])

    def test_patch_percent_encoded_actor(self):
        """浏览器 fetch 头值仅允许 ISO-8859-1：前端中文部分 encodeURIComponent 后发送，
        后端 _header_str 百分号解码还原中文（无 % 的值不受影响）"""
        from urllib.parse import quote
        encoded = f"manual:{quote('下拉改状态')}"
        self.assertNotEqual(encoded, f"manual:下拉改状态")   # 前置：编码后确为纯 ASCII
        it = self._create()
        self._patch(it["id"], {"status": "open"}, {"X-Actor": encoded})
        rows = self._log(it["id"])
        self.assertEqual(rows[-1]["actor"], "manual")
        self.assertIn("manual:下拉改状态", rows[-1]["detail"])

    def test_patch_agent_header(self):
        """验收 5：X-Actor: agent:<来源> 落 agent 桶，detail 含来源标识"""
        it = self._create()
        self._patch(it["id"], {"status": "open"}, {"X-Actor": "agent:req-board:req-375"})
        rows = self._log(it["id"])
        self.assertEqual(rows[-1]["actor"], "agent")
        self.assertIn("agent:req-board:req-375", rows[-1]["detail"])

    def test_patch_same_status_no_log(self):
        """验收 4：status 未变化的 PATCH（仅改 notes/同状态）不产生 log 行"""
        it = self._create()
        self._patch(it["id"], {"status": "open"})
        self.assertEqual(len(self._log(it["id"])), 1)
        self._patch(it["id"], {"notes": "仅改详情，状态不变"})
        self.assertEqual(len(self._log(it["id"])), 1)   # 仅 notes 不写
        self._patch(it["id"], {"status": "open"})
        self.assertEqual(len(self._log(it["id"])), 1)   # 同状态不写
        self._patch(it["id"], {"status": "later"})
        self.assertEqual(len(self._log(it["id"])), 2)   # 实际变化才写

    def test_status_log_api_chain(self):
        """GET status-log 返回全链（升序，含 actor）"""
        it = self._create()
        self._patch(it["id"], {"status": "open"})
        self._patch(it["id"], {"status": "doing"}, {"X-Actor": "dispatcher"})
        r = self.client.get(f"/api/requirements/{it['id']}/status-log")
        self.assertEqual(r.status_code, 200)
        rows = r.get_json()
        self.assertEqual([x["new_status"] for x in rows], ["open", "doing"])
        self.assertEqual(rows[0]["actor"], "api")
        self.assertEqual(rows[1]["actor"], "dispatcher")
        self.assertIn("ts", rows[0])

    def test_patch_nonexistent_404(self):
        r = self._patch(999999, {"status": "open"})
        self.assertEqual(r.status_code, 404)

    # ==================== #410 调度回放 status 事件 ====================

    def test_status_event_written_with_log(self):
        """#410 验收 1：status 变化写审计行的同时写 kind=status 回放事件（content 中文+原文）"""
        it = self._create()
        self._patch(it["id"], {"status": "open"})
        self.assertEqual(len(self._log(it["id"])), 1)
        events = self._events(it["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "status")
        self.assertEqual(events[0]["content"], "状态变更：暂缓(later) → 待开发(open)")

    def test_status_event_all_sources(self):
        """#410 验收 1：dispatcher/manual/agent/api 任一头均写回放事件（单点覆盖）"""
        it = self._create()
        headerses = [{"X-Actor": "dispatcher"}, {"X-Actor": "manual:下拉改状态"},
                     {"X-Actor": "agent:req-board:req-410"}, {}]
        for i, headers in enumerate(headerses):
            self._patch(it["id"], {"status": ["open", "doing", "review", "done"][i]}, headers)
            self.assertEqual(len(self._events(it["id"])), i + 1)

    def test_status_event_no_write_when_unchanged(self):
        """#410：同状态 PATCH 与仅改 notes 不写回放事件"""
        it = self._create()
        self._patch(it["id"], {"status": "open"})
        self._patch(it["id"], {"notes": "仅改详情，状态不变"})
        self._patch(it["id"], {"status": "open"})
        self.assertEqual(len(self._events(it["id"])), 1)

    def test_status_event_order_matches_log(self):
        """#410 验收 2：多次变更回放顺序与审计行一致（done 流程：合并→状态→重启）"""
        it = self._create()
        for status in ("open", "doing", "done"):
            self._patch(it["id"], {"status": status}, {"X-Actor": "dispatcher"})
        events = self._events(it["id"])
        logs = self._log(it["id"])
        self.assertEqual([e["content"] for e in events], [
            "状态变更：暂缓(later) → 待开发(open)",
            "状态变更：待开发(open) → 进行中(doing)",
            "状态变更：进行中(doing) → 已完成(done)"])
        self.assertEqual(len(events), len(logs))   # 每次变更各写一条，数量一致

    def test_status_event_full_label(self):
        """#410：全 7 状态值中文映射（route/review/dropped 不裸显英文）"""
        it = self._create("open")
        for status in ("route", "review", "dropped"):
            self._patch(it["id"], {"status": status})
        contents = [e["content"] for e in self._events(it["id"])]
        self.assertEqual(contents[-1], "状态变更：评审中(review) → 已废弃(dropped)")
        for c in contents:
            self.assertIn("→", c)

    def test_status_event_ts_local(self):
        """#410 修复（审查 P1）：status 事件 ts 为本地时间（now_str），与回放其他事件一致，
        非 SQLite DEFAULT CURRENT_TIMESTAMP（UTC，差 8 小时）"""
        it = self._create()
        self._patch(it["id"], {"status": "open"})
        ev = self._events(it["id"])[0]
        ts = datetime.strptime(ev["ts"], "%Y-%m-%d %H:%M:%S")
        # UTC（CURRENT_TIMESTAMP）会差 8 小时（28800s），本地时间应接近 0
        self.assertLess(abs((datetime.now() - ts).total_seconds()), 90)


class BaselineSnapshotTest(unittest.TestCase):
    """上线快照：存量需求各写一行 baseline，且幂等（重复 init_db 不重写）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req-status-base-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        b.DB_PATH = self.tmp / "board.db"
        b.init_db()
        # 模拟存量需求（baseline 写入前已存在）
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO requirements (title, project, status) VALUES ('存量1', 'req-board', 'later')")
            conn.execute(
                "INSERT INTO requirements (title, project, status) VALUES ('存量2', 'req-board', 'doing')")
            conn.commit()

    def test_baseline_written_once(self):
        b.init_db()  # 触发上线快照
        with b.get_db() as conn:
            rows = conn.execute(
                "SELECT req_id, old_status, new_status, actor FROM req_status_log ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["actor"], "baseline")
            self.assertIsNone(r["old_status"])
        self.assertEqual({r["new_status"] for r in rows}, {"later", "doing"})
        b.init_db()  # 重复启动不重写
        with b.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM req_status_log").fetchone()[0]
        self.assertEqual(n, 2)

    def test_new_req_after_baseline_no_log(self):
        """快照之后新建的卡不产生 baseline 行，从第一次变更起有痕"""
        b.init_db()
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO requirements (title, project, status) VALUES ('新卡', 'req-board', 'open')")
            conn.commit()
            new_id = cur.lastrowid
        with b.get_db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM req_status_log WHERE req_id = ?", (new_id,)).fetchone()[0]
        self.assertEqual(n, 0)


class StatusLogTimestampTest(unittest.TestCase):
    """审计行时间口径（#428 顺带修）：新行走 now_str() 本地时间、存量 UTC 行一次性迁移且幂等。

    历史：建表默认值 CURRENT_TIMESTAMP 落 UTC，与卡面/事件流（北京时间）差 8 小时——#410 修了
    同事务的 dispatcher_events 行，req_status_log 行漏改。独立临时 DB，不碰真实 data/board.db。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req-status-ts-"))
        self._orig_db = b.DB_PATH
        b.DB_PATH = self.tmp / "board.db"
        b.init_db()
        self.client = b.app.test_client()

    def tearDown(self):
        b.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _local_seconds(self, ts):
        return abs((datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds())

    def test_new_row_ts_is_local_time(self):
        """PATCH 变更写出的审计行 = 北京时间（UTC 会差 8 小时）"""
        r = self.client.post("/api/requirements", json={
            "title": "口径卡", "project": "req-board", "status": "later"})
        rid = r.get_json()["id"]
        self.client.patch(f"/api/requirements/{rid}", json={"status": "open"})
        with b.get_db() as conn:
            row = conn.execute("SELECT ts FROM req_status_log WHERE req_id = ?", (rid,)).fetchone()
        self.assertLess(self._local_seconds(row["ts"]), 60, f"审计行 ts 非本地时间: {row['ts']}")

    def test_status_row_matches_event_row_same_txn(self):
        """同一事务里审计行与 dispatcher_events 行同口径（历史 bug：两行差 8 小时）"""
        r = self.client.post("/api/requirements", json={
            "title": "口径卡2", "project": "req-board", "status": "later"})
        rid = r.get_json()["id"]
        self.client.patch(f"/api/requirements/{rid}", json={"status": "open"})
        with b.get_db() as conn:
            log_ts = conn.execute("SELECT ts FROM req_status_log WHERE req_id = ?", (rid,)).fetchone()["ts"]
            ev_ts = conn.execute(
                "SELECT ts FROM dispatcher_events WHERE req_id = ? AND kind = 'status'", (rid,)).fetchone()["ts"]
        self.assertEqual(log_ts[:16], ev_ts[:16])

    def test_baseline_row_ts_is_local_time(self):
        """baseline 上线快照行同样走本地时间"""
        with b.get_db() as conn:
            conn.execute("INSERT INTO requirements (title, project, status) VALUES ('存量卡', 'req-board', 'later')")
            conn.commit()
        b.init_db()   # 触发 baseline 补写
        with b.get_db() as conn:
            row = conn.execute(
                "SELECT ts FROM req_status_log WHERE actor = 'baseline' ORDER BY id DESC").fetchone()
        self.assertLess(self._local_seconds(row["ts"]), 60)

    def test_legacy_utc_rows_migrated_once(self):
        """存量 UTC 行一次性 +8h；user_version 守卫保证不重复迁移"""
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO req_status_log (req_id, old_status, new_status, actor, detail, ts) "
                "VALUES (990001, 'open', 'doing', 'api', 'legacy-utc', datetime('now'))")
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
        b.init_db()
        with b.get_db() as conn:
            migrated = conn.execute(
                "SELECT ts FROM req_status_log WHERE req_id = 990001").fetchone()["ts"]
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(ver, 1)
        delta_h = (datetime.strptime(migrated, "%Y-%m-%d %H:%M:%S")
                   - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() / 3600
        self.assertAlmostEqual(delta_h, 8, delta=0.05, msg=f"迁移后应≈北京时间: {migrated}")
        b.init_db()   # 再启动一次：不得二次 +8h
        with b.get_db() as conn:
            again = conn.execute(
                "SELECT ts FROM req_status_log WHERE req_id = 990001").fetchone()["ts"]
        self.assertEqual(migrated, again)


if __name__ == "__main__":
    unittest.main()
