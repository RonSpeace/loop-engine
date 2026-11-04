#!/usr/bin/env python3
"""req-431 调度器空会话行清理（sweep_empty_sessions）测试。

覆盖：候选筛选与计数、TTL=0 关闭、409/404 跳过计数、看板不可达 warn、启动不限龄清理接线、
节流、主循环接线、created_at 解析失败 fail-closed、L1 集成（真实 Flask 客户端驱动完整删除矩阵）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_empty_session_sweep -v
"""
import contextlib
import json
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


def _mk_row(row_id, req_id=1, session_id=None, status="created", poisoned=0,
            created_at=None, age_sec=None):
    if created_at is None:
        ts = time.time() - (age_sec or 0)
        created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    return {"id": row_id, "req_id": req_id, "role": "dev", "session_id": session_id,
            "status": status, "poisoned": poisoned, "created_at": created_at}


class SweepUnitTest(unittest.TestCase):
    """候选筛选 / 计数 / 关闭 / 降级（api_board 全 mock，不发真请求）"""

    def setUp(self):
        self._orig_ttl = d.EMPTY_SESSION_TTL_SEC
        self._orig_interval = d.EMPTY_SESSION_SWEEP_INTERVAL
        self._orig_last = d._last_empty_session_sweep
        d._last_empty_session_sweep = 0.0

    def tearDown(self):
        d.EMPTY_SESSION_TTL_SEC = self._orig_ttl
        d.EMPTY_SESSION_SWEEP_INTERVAL = self._orig_interval
        d._last_empty_session_sweep = self._orig_last

    # ---- 用例 12：候选筛选与计数（命令条件不在此层，由 L1 用例覆盖） ----
    def test_sweep_candidate_filter_and_counting(self):
        rows = [
            _mk_row(1, age_sec=90000),                                   # 候选：全满足
            _mk_row(2, session_id="s_1", age_sec=90000),                 # 有 session_id
            _mk_row(3, status="idle", age_sec=90000),                    # 非 created
            _mk_row(4, poisoned=1, age_sec=90000),                       # 毒会话
            _mk_row(5, age_sec=10),                                      # 龄未到（TTL=86400）
            _mk_row(6, created_at="not-a-time"),                         # 龄不可解析 → 不删
        ]
        deleted_paths = []

        def fake(method, path, body=None):
            if method == "GET":
                return rows
            deleted_paths.append(path)
            return {"deleted": 1}

        with mock.patch.object(d, "api_board", side_effect=fake):
            d.sweep_empty_sessions(max_age_sec=86400)
        self.assertEqual(deleted_paths, ["/requirements/1/sessions/1"])

    # ---- 用例 14：409/404 跳过计数 + 日志四类计数 ----
    def test_sweep_counts_and_logs_skip_kinds(self):
        rows = [_mk_row(1), _mk_row(2), _mk_row(3), _mk_row(4, session_id="s")]

        def fake(method, path, body=None):
            if method == "GET":
                return rows
            if path.endswith("/1"):
                return {"deleted": 1}
            if path.endswith("/2"):
                raise urllib.error.HTTPError(path, 409, "conflict", None, None)
            raise urllib.error.HTTPError(path, 404, "not found", None, None)

        with mock.patch.object(d, "api_board", side_effect=fake), \
             mock.patch.object(d.log, "info") as log_info:
            d.sweep_empty_sessions(max_age_sec=None)   # 不限龄：4 行全候选
        msg = " ".join(str(c.args[0]) for c in log_info.call_args_list)
        self.assertIn("候选 3", msg)       # 第 4 行有 session_id，不入候选
        self.assertIn("删除 1", msg)
        self.assertIn("409=1", msg)
        self.assertIn("404=1", msg)

    # ---- 用例 15：看板不可达 → 单列 warn，不带删除计数，不抛 ----
    def test_sweep_board_unreachable_logs_warn(self):
        with mock.patch.object(d, "api_board", side_effect=OSError("connection refused")), \
             mock.patch.object(d.log, "warning") as warn, \
             mock.patch.object(d.log, "info") as info:
            d.sweep_empty_sessions(max_age_sec=None)
        self.assertIn("看板不可达", str(warn.call_args_list[0].args[0]))
        self.assertFalse(info.called)

    # ---- 用例 13：TTL=0 关闭定期清理（零请求） ----
    def test_sweep_ttl_zero_disables(self):
        d.EMPTY_SESSION_TTL_SEC = 0
        with mock.patch.object(d, "api_board") as api, \
             mock.patch.object(d.time, "monotonic", return_value=10 ** 9):
            d._maybe_sweep_empty_sessions()
        self.assertFalse(api.called)

    # ---- 用例 17：节流（同 interval 内二次调用零请求） ----
    def test_sweep_throttled(self):
        d.EMPTY_SESSION_SWEEP_INTERVAL = 1800
        base = 10 ** 6
        with mock.patch.object(d, "api_board", return_value=[]) as api, \
             mock.patch.object(d.time, "monotonic", return_value=base):
            d._maybe_sweep_empty_sessions()
        self.assertEqual(api.call_count, 1)
        with mock.patch.object(d, "api_board", return_value=[]) as api2, \
             mock.patch.object(d.time, "monotonic", return_value=base + 10):
            d._maybe_sweep_empty_sessions()
        self.assertEqual(api2.call_count, 0)   # 同 interval 内零请求
        with mock.patch.object(d, "api_board", return_value=[]) as api3, \
             mock.patch.object(d.time, "monotonic",
                               return_value=base + d.EMPTY_SESSION_SWEEP_INTERVAL + 1):
            d._maybe_sweep_empty_sessions()
        self.assertEqual(api3.call_count, 1)   # 到点后再查一次

    # ---- 用例 19：created_at 缺失/非法 → 不删（fail-closed） ----
    def test_sweep_skips_unparsable_created_at(self):
        self.assertIsNone(d._session_age_sec(""))
        self.assertIsNone(d._session_age_sec(None))
        self.assertIsNone(d._session_age_sec("2026-09-19T15:00:00"))
        self.assertIsNotNone(d._session_age_sec("2026-09-19 15:00:00"))
        rows = [_mk_row(1, created_at=None), _mk_row(2, created_at="")]
        for r in rows:
            r["created_at"] = None
        with mock.patch.object(d, "api_board", side_effect=lambda m, p, b=None: rows if m == "GET" else {}) as api:
            d.sweep_empty_sessions(max_age_sec=0)
        self.assertEqual(api.call_count, 1)   # 只有 GET，零 DELETE

    # ---- 用例 16：不限龄（启动存量清理）连新行也删 ----
    def test_sweep_unlimited_age_deletes_fresh_row(self):
        rows = [_mk_row(1, age_sec=5)]
        calls = []

        def fake(method, path, body=None):
            calls.append((method, path))
            return rows if method == "GET" else {"deleted": 1}

        with mock.patch.object(d, "api_board", side_effect=fake):
            d.sweep_empty_sessions(max_age_sec=None)
        self.assertEqual(calls[1], ("DELETE", "/requirements/1/sessions/1"))


class StartupWiringTest(unittest.TestCase):
    """启动序列接线：不限龄清理在 recover_manual_commands 之后"""

    def test_startup_purge_unlimited_age(self):
        calls = []
        # 21 个补丁写成单个 with 会超过 CPython 静态嵌套块上限（20），整个文件无法编译——用 ExitStack
        with contextlib.ExitStack() as stack:
            for name in ("load_state", "_verify_pending_restart", "_mount_file_logger", "write_heartbeat",
                         "register_shutdown_handlers", "report_previous_exit", "reconcile_ghost_pending",
                         "backfill_flow_snapshots", "reconcile_adopt_results", "reconcile_startup",
                         "reconcile_orphan_sessions", "cleanup_stale_worktrees", "cleanup_orphan_held"):
                stack.enter_context(mock.patch.object(d, name))
            stack.enter_context(mock.patch.object(d, "recover_manual_commands",
                                                  side_effect=lambda: calls.append("recover")))
            stack.enter_context(mock.patch.object(
                d, "sweep_empty_sessions",
                side_effect=lambda max_age_sec=None: calls.append(("sweep", max_age_sec))))
            stack.enter_context(mock.patch.object(d.threading, "Thread"))
            stack.enter_context(mock.patch.object(d, "NO_FEISHU", True))
            stack.enter_context(mock.patch.multiple(
                d, init_feishu=mock.DEFAULT,
                HEARTBEAT_FILE=Path(tempfile.mkdtemp(prefix="sweep-hb-")) / "hb.json"))
            stack.enter_context(mock.patch.object(d, "_main_loop_tick", side_effect=KeyboardInterrupt))
            stack.enter_context(mock.patch.object(d.time, "sleep"))
            with self.assertRaises(KeyboardInterrupt):
                d.main()
        self.assertEqual(calls, ["recover", ("sweep", None)])

    def test_tick_calls_empty_session_sweep(self):
        with mock.patch.object(d, "_maybe_auto_restart"), \
             mock.patch.object(d, "_maybe_sweep_stale_worktrees"), \
             mock.patch.object(d, "_maybe_sweep_empty_sessions") as sweep, \
             mock.patch.object(d, "reconcile_decisions"), \
             mock.patch.object(d, "reconcile_confirmations"), \
             mock.patch.object(d, "_sweep_dead_running_once"), \
             mock.patch.object(d, "get_open_reqs", return_value=[]), \
             mock.patch.object(d, "write_heartbeat"):
            d._main_loop_tick()
        sweep.assert_called_once()


class SweepIntegrationTest(unittest.TestCase):
    """用例 20（L1，命令条件权威用例）：真实 Flask 测试客户端驱动完整删除矩阵"""

    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b
        cls.tmp = tempfile.mkdtemp(prefix="req431-sweep-")
        cls._orig_db = b.DB_PATH
        b.DB_PATH = Path(cls.tmp) / "test.db"
        b.init_db()
        b.app.config["TESTING"] = True
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_sweep_integration_with_board_client(self):
        b = self.b
        req_id = self.client.post("/api/requirements", json={
            "title": "S", "project": "req-board", "status": "later"}).get_json()["id"]
        rows = {}

        def add(role, session_id, status, created_at, poisoned=0):
            with b.get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO agent_sessions (req_id, role, session_id, status, poisoned,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (req_id, role, session_id, status, poisoned, created_at, created_at))
                conn.commit()
                return cur.lastrowid

        def add_cmd(asid, status):
            with b.get_db() as conn:
                conn.execute("INSERT INTO manual_commands (req_id, agent_session_id, role, text,"
                             " status, created_at, updated_at) VALUES (?, ?, 'dev', 'x', ?, ?, ?)",
                             (req_id, asid, status, b.now_str(), b.now_str()))
                conn.commit()

        rows["empty"] = add("dev", None, "created", b.now_str())
        rows["pending"] = add("dev", None, "created", b.now_str())
        add_cmd(rows["pending"], "pending")
        rows["running_cmd"] = add("dev", None, "created", b.now_str())
        add_cmd(rows["running_cmd"], "running")
        rows["done_cmd"] = add("dev", None, "created", b.now_str())
        add_cmd(rows["done_cmd"], "done")
        rows["real"] = add("dev", "session_real", "idle", b.now_str())

        def shim(method, path, body=None):
            resp = self.client.open("/api" + path, method=method,
                                    data=json.dumps(body) if body is not None else None,
                                    content_type="application/json")
            if resp.status_code >= 400:
                raise urllib.error.HTTPError(path, resp.status_code, "err", None, None)
            return resp.get_json()

        with mock.patch.object(d, "api_board", side_effect=shim):
            d.sweep_empty_sessions(max_age_sec=None)
        with b.get_db() as conn:
            left = {r["id"] for r in conn.execute("SELECT id FROM agent_sessions")}
        self.assertEqual(left, {rows["pending"], rows["running_cmd"], rows["real"]})
        # 命令历史（done）随行删除保留留档
        with b.get_db() as conn:
            cmds = [dict(r) for r in conn.execute("SELECT * FROM manual_commands")]
        self.assertEqual(sorted(c["status"] for c in cmds), ["done", "pending", "running"])


if __name__ == "__main__":
    unittest.main()
