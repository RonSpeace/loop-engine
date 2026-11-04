#!/usr/bin/env python3
"""req-388 收尾兜底独立化：_sweep_dead_running_once 主循环兜底单测。

现场（#386/#387，2026-08-30）：run_agent 的收尾（sync_agent_session idle / post_run_record /
handle_result）在会话线程 finally 内，调度器被杀即丢；重启后「有持有者键但进程已死」的
running 会话行无人认领（#374 只清无持有者键），中间栏僵尸。

修复（#388，仅 dispatcher.py）：
- _sweep_dead_running_once 每 tick 补扫：running 行 + 有持有者键 + 进程确死
  （active_runs 有记录且 proc.poll() 非 None）→ _finish_dead_session 独立线程补收尾
  （行置 idle + 运行记录落库 + handle_result）；_swept_rows 防 run_agent finally 重复收尾；
- 边界二分：无持有者键 / active_runs 无记录的行归启动序列（#374 清算 / adopt)，运行期不碰。

本测试为 L0 单测（mock 副作用，沿用 tests/test_dispatcher_reconcile_orphan_sessions.py 风格）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_tick_cleanup_sweep -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


class _AliveProc:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def row(rid, role, sid, status="running", db_id=None):
    return {"id": db_id or (rid * 10), "req_id": rid, "role": role,
            "session_id": sid, "status": status}


class L0SweepDeadRunning(unittest.TestCase):
    """L0 单测：mock api_board/active_runs/fetch_req/_finish_dead_session，
    校验收尾触发 / 存活与边界保护 / 降级"""

    # 本类会替换的 dispatcher 模块属性，tearDown 统一恢复（防污染同进程后续测试）
    _MUTATED = ("state", "api_board", "active_runs", "fetch_req", "_finish_dead_session", "log")

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in self._MUTATED}
        d.state = {"sessions": {}}
        d.active_runs = {}
        d.log = mock.Mock()
        self.get_rows = []
        self.finished = []  # _finish_dead_session 调用记录 (row, req, role, rec)

        def fake_board(method, path, body=None):
            return self.get_rows if method == "GET" else {}

        d.api_board = fake_board
        d.fetch_req = lambda rid: {"id": int(rid), "project": "req-board",
                                   "status": "doing", "phase": None, "notes": ""}
        d._finish_dead_session = lambda row, req, role, rec: self.finished.append(
            (row, req, role, rec))

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)
        d._swept_rows.clear()

    def _rows(self, *rows):
        self.get_rows = list(rows)

    def _held_sessions(self, rid, role, sid):
        d.state["sessions"] = {f"{role}:{rid}": {"id": sid}}

    # ---- 验收 3：进程死后 running 行一轮 tick 内被收尾（idle + run 记录由 _finish 落）----
    def test_dead_process_session_swept(self):
        """running 行 + 持有者键 + active_runs proc.poll()=1（进程确死）→ 发现并启动补收尾"""
        self._rows(row(388, "analysis", "session_dead", db_id=901))
        self._held_sessions(388, "analysis", "session_dead")
        d.active_runs["388"] = {"proc": _AliveProc(1), "role": "analysis",
                                "started_at": 1000.0, "session": object()}
        found = d._sweep_dead_running_once()
        self.assertEqual(found, 1)
        self.assertEqual(len(self.finished), 1)
        rrow, req, role, rec = self.finished[0]
        self.assertEqual(rrow["id"], 901)
        self.assertEqual(req["id"], 388)
        self.assertEqual(role, "analysis")
        self.assertEqual(rec["started_at"], 1000.0)

    def test_alive_process_not_swept(self):
        """proc.poll() is None（正常运行中）→ 绝不误动"""
        self._rows(row(388, "analysis", "session_alive", db_id=901))
        self._held_sessions(388, "analysis", "session_alive")
        d.active_runs["388"] = {"proc": _AliveProc(None), "role": "analysis",
                                "started_at": 1000.0, "session": object()}
        found = d._sweep_dead_running_once()
        self.assertEqual(found, 0)
        self.assertEqual(self.finished, [])
        self.assertTrue(901 not in d._swept_rows)

    def test_no_owner_not_swept(self):
        """无持有者键（归 #374 启动清算）→ 运行期 sweep 不碰"""
        self._rows(row(388, "analysis", "session_orphan", db_id=901))
        d.active_runs["388"] = {"proc": _AliveProc(1), "role": "analysis",
                                "started_at": 1000.0, "session": object()}
        found = d._sweep_dead_running_once()
        self.assertEqual(found, 0)
        self.assertEqual(self.finished, [])

    def test_no_active_record_not_swept(self):
        """active_runs 无记录（重启残留，归启动序列 adopt/orphan）→ 运行期不碰"""
        self._rows(row(388, "analysis", "session_zombie", db_id=901))
        self._held_sessions(388, "analysis", "session_zombie")
        found = d._sweep_dead_running_once()
        self.assertEqual(found, 0)
        self.assertEqual(self.finished, [])

    def test_not_running_and_empty_sid_skipped(self):
        """非 running 行 / session_id 空行不参与兜底"""
        self._rows(row(388, "analysis", "sidle", status="idle", db_id=901),
                   row(389, "analysis", None, db_id=902))
        found = d._sweep_dead_running_once()
        self.assertEqual(found, 0)

    def test_already_swept_row_not_repeated(self):
        """_swept_rows 已标记的行（收尾线程/会话 finally 竞态）不重复启动收尾"""
        self._rows(row(388, "analysis", "session_dead", db_id=901))
        self._held_sessions(388, "analysis", "session_dead")
        d.active_runs["388"] = {"proc": _AliveProc(1), "role": "analysis",
                                "started_at": 1000.0, "session": object()}
        self.assertEqual(d._sweep_dead_running_once(), 1)
        self.assertEqual(d._sweep_dead_running_once(), 0)
        self.assertEqual(len(self.finished), 1)

    def test_board_unreachable_returns_zero(self):
        """GET /agent-sessions 失败 → 返回 0 不崩"""
        def boom(method, path, body=None):
            raise RuntimeError("board down")
        d.api_board = boom
        self.assertEqual(d._sweep_dead_running_once(), 0)

    def test_missing_req_skipped(self):
        """fetch_req 返回 None（需求已删）→ 跳过该行（不启动收尾线程；未标记，下轮重扫无妨）"""
        self._rows(row(388, "analysis", "session_dead", db_id=901))
        self._held_sessions(388, "analysis", "session_dead")
        d.active_runs["388"] = {"proc": _AliveProc(1), "role": "analysis",
                                "started_at": 1000.0, "session": object()}
        d.fetch_req = lambda rid: None
        self.assertEqual(d._sweep_dead_running_once(), 0)
        self.assertEqual(self.finished, [])

    def test_row_role_falls_back_to_active_record(self):
        """行 role 为空 → 取 active_runs 的 role（再退 role_for）"""
        self._rows(row(388, "", "session_dead", db_id=901))
        self._held_sessions(388, "", "session_dead")
        d.state["sessions"] = {"388": "session_dead"}  # 旧格式持有者键
        d.active_runs["388"] = {"proc": _AliveProc(1), "role": "dev",
                                "started_at": 1000.0, "session": object()}
        self.assertEqual(d._sweep_dead_running_once(), 1)
        self.assertEqual(self.finished[0][2], "dev")


class L0FinishDeadSession(unittest.TestCase):
    """_finish_dead_session：补收尾三件事（行 idle + 运行记录 + handle_result）+ 摘除旧登记，各自降级"""

    _MUTATED = ("sync_agent_session", "post_run_record", "handle_result", "log", "active_runs")

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in self._MUTATED}
        d.log = mock.Mock()
        d.active_runs = {}
        self.calls = {"sync": [], "run": [], "handle": []}
        d.sync_agent_session = lambda req, role, session_id=None, row_id=None, **kw: (
            self.calls["sync"].append((req, role, session_id, row_id, kw)))
        d.post_run_record = lambda *a, **k: self.calls["run"].append((a, k))
        d.handle_result = lambda *a, **k: self.calls["handle"].append((a, k))

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)

    def test_finish_does_three_cleanups(self):
        """行置 idle（带 row_id）+ 运行记录（started_at/returncode=1）+ handle_result(rc=0)"""
        row = {"id": 901, "session_id": "session_dead"}
        req = {"id": 388, "project": "req-board"}
        rec = {"proc": _AliveProc(1), "started_at": 1000.0, "session": object()}
        d._finish_dead_session(row, req, "analysis", rec)
        # #421：行置 idle 改走 _close_session_row（sync_agent_session 形态：带 turn_step='' 清态）
        self.assertEqual(self.calls["sync"],
                         [(req, "analysis", "session_dead", 901, {"status": "idle", "turn_step": ""})])
        run_args, _ = self.calls["run"][0]
        self.assertEqual(run_args[0], req)
        self.assertEqual(run_args[1], "analysis")
        self.assertEqual(run_args[3], 1000.0)
        self.assertEqual(run_args[5], 1)
        self.assertEqual(self.calls["handle"][0][0][0], req)
        self.assertEqual(self.calls["handle"][0][0][1], "analysis")
        self.assertEqual(self.calls["handle"][0][0][2], 0)
        self.assertFalse(self.calls["handle"][0][0][3])

    # ---- #388 审查问题 1/2 修复：handle_result 前摘除旧登记（对象匹配）----
    def test_finish_removes_stale_active_run(self):
        """handle_result 前按对象匹配摘除死进程的旧登记——否则 _spawn_safe 见
        active_runs 含本需求被陈旧记录误拦，下一阶段不被拉起（问题 1 现场）"""
        row = {"id": 901, "session_id": "s1"}
        req = {"id": 388, "project": "req-board"}
        rec = {"proc": _AliveProc(1), "started_at": 1000.0, "session": object()}
        d.active_runs["388"] = rec
        d._finish_dead_session(row, req, "analysis", rec)
        self.assertNotIn("388", d.active_runs)
        self.assertEqual(len(self.calls["handle"]), 1)  # 摘除不影响收尾执行

    def test_finish_keeps_newer_active_run(self):
        """对象不匹配（同需求已有新会话登记）→ 不摘除（防误删失败通道/新会话的登记）"""
        row = {"id": 901, "session_id": "s1"}
        req = {"id": 388, "project": "req-board"}
        rec = {"proc": _AliveProc(1), "started_at": 1000.0, "session": object()}
        newer = {"proc": _AliveProc(None), "role": "dev", "started_at": 2000.0,
                 "session": object()}
        d.active_runs["388"] = newer
        d._finish_dead_session(row, req, "analysis", rec)
        self.assertIs(d.active_runs["388"], newer)

    def test_finish_degrades_step_by_step(self):
        """sync/post_run_record 抛异常 → 各自降级日志，handle_result 仍执行"""
        def boom(req, role, session_id=None, row_id=None, **kw):
            raise RuntimeError("board down")
        d.sync_agent_session = boom
        row = {"id": 901, "session_id": "s1"}
        req = {"id": 388, "project": "req-board"}
        rec = {"proc": _AliveProc(1), "started_at": 1000.0, "session": object()}
        d._finish_dead_session(row, req, "analysis", rec)
        self.assertEqual(len(self.calls["run"]), 1)
        self.assertEqual(len(self.calls["handle"]), 1)


if __name__ == "__main__":
    unittest.main()
