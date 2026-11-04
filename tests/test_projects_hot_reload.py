#!/usr/bin/env python3
"""#4 dispatcher projects.json mtime 热重载测试（B 组）。

B1 变化触发重载 / B2 未变化不重载 / B3 失败保留旧值+log.error（失败签名防刷屏）/
B4 首轮基线仅记录 / B5 _main_loop_tick 接线锁定 / B6 文件缺失静默跳过 /
B7 run_agent spawn 传参断言（role_model 收到 project=req["project"]）。

隔离：PROJECTS_FILE 一律 patch 到临时 fixture，用例结束恢复；不写仓库内任何文件。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_projects_hot_reload.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import dispatcher as d  # noqa: E402


class HotReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req4-hot-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pj = self.tmp / "projects.json"
        self._saved = (d.PROJECTS_FILE, d.PROJECTS, d._PROJECTS_SIG)
        d.PROJECTS_FILE = self.pj
        self.addCleanup(self._restore)

    def _restore(self):
        d.PROJECTS_FILE, d.PROJECTS, d._PROJECTS_SIG = self._saved

    def _write(self, data):
        new = self.tmp / f"new-{os.getpid()}.json"
        new.write_text(json.dumps(data), encoding="utf-8")
        os.replace(new, self.pj)   # 原子替换，确保 mtime_ns 变化

    # ---- B1：sig 变化触发重载 ----
    def test_b1_sig_change_reloads(self):
        self._write({"req-board": str(REPO)})
        d.PROJECTS = {"old": {"paths": []}}
        d._PROJECTS_SIG = None
        d._maybe_reload_projects()                       # 首轮：仅建基线
        self.assertEqual(list(d.PROJECTS), ["old"])
        self._write({"req-board": str(REPO), "newproj": {"paths": ["/tmp/x"]}})
        d._maybe_reload_projects()
        self.assertIn("newproj", d.PROJECTS)
        self.assertEqual(d.PROJECTS["newproj"]["paths"], ["/tmp/x"])
        self.assertEqual(d._PROJECTS_SIG, d._projects_file_sig())

    # ---- B2：sig 不变不重载（对象同一性） ----
    def test_b2_same_sig_no_reload(self):
        self._write({"req-board": str(REPO)})
        d.PROJECTS = {"pinned": {"paths": []}}
        d._PROJECTS_SIG = d._projects_file_sig()
        d._maybe_reload_projects()
        self.assertIs(d.PROJECTS, d.PROJECTS)
        self.assertEqual(list(d.PROJECTS), ["pinned"])
        with mock.patch.object(d, "load_projects") as lp:
            d._maybe_reload_projects()
        lp.assert_not_called()

    # ---- B3：重载失败保留旧值 + log.error；失败签名防每 tick 刷屏 ----
    def test_b3_reload_failure_keeps_old(self):
        self._write({"req-board": str(REPO)})
        d._PROJECTS_SIG = None
        d._maybe_reload_projects()                       # 基线
        old = d.PROJECTS
        self.pj.write_text("{bad json", encoding="utf-8")
        with self.assertLogs("dispatcher", level="ERROR") as cm:
            d._maybe_reload_projects()                   # sig 变化（size 变）→ 重载失败
            self.assertIs(d.PROJECTS, old)
            self.assertEqual(sum("热重载失败" in line for line in cm.output), 1)
            d._maybe_reload_projects()                   # 同一失败签名 → 不重试、不再刷屏
            self.assertEqual(sum("热重载失败" in line for line in cm.output), 1)
        self.assertIs(d.PROJECTS, old)

    # ---- B4：首轮基线（_PROJECTS_SIG=None → 仅记录不重载） ----
    def test_b4_first_baseline(self):
        self._write({"newproj": {"paths": []}})
        d.PROJECTS = {"pinned": {"paths": []}}
        d._PROJECTS_SIG = None
        d._maybe_reload_projects()
        self.assertEqual(list(d.PROJECTS), ["pinned"])   # 未重载
        self.assertEqual(d._PROJECTS_SIG, d._projects_file_sig())

    # ---- B5：tick 接线锁定 ----
    def test_b5_tick_wiring(self):
        with mock.patch.object(d, "_maybe_auto_restart"), \
                mock.patch.object(d, "_maybe_reload_projects") as reload_mock, \
                mock.patch.object(d, "_maybe_sweep_empty_sessions"), \
                mock.patch.object(d, "_maybe_sweep_stale_worktrees"), \
                mock.patch.object(d, "reconcile_decisions"), \
                mock.patch.object(d, "reconcile_confirmations"), \
                mock.patch.object(d, "_sweep_dead_running_once"), \
                mock.patch.object(d, "get_open_reqs", return_value=[]), \
                mock.patch.object(d, "write_heartbeat"):
            d._main_loop_tick()
        reload_mock.assert_called_once()

    # ---- B6：文件缺失静默跳过（不抛、不重载、保留旧值） ----
    def test_b6_missing_file_silent(self):
        d.PROJECTS = {"pinned": {"paths": []}}
        d._PROJECTS_SIG = (1, 1)                         # 陈旧基线，文件已不存在
        self.assertFalse(self.pj.exists())
        with mock.patch.object(d.log, "error") as err:
            d._maybe_reload_projects()
        err.assert_not_called()
        self.assertEqual(list(d.PROJECTS), ["pinned"])
        self.assertIsNone(d._PROJECTS_SIG)


class SpawnModelProjectArgTest(unittest.TestCase):
    """B7：run_agent 建会话时 role_model 收到 project=req["project"]（防重构丢参）"""

    def test_b7_spawn_passes_project(self):
        req = {"id": 424, "project": "req-board", "title": "T", "notes": "N"}
        art = Path(tempfile.mkdtemp(prefix="req4-art-")).resolve()
        wd = Path(tempfile.mkdtemp(prefix="req4-wd-")).resolve()
        self.addCleanup(shutil.rmtree, art, True)
        self.addCleanup(shutil.rmtree, wd, True)
        calls = []

        class FakeSession:
            def __init__(self, *a, **kw):
                self.session_id, self.model_name, self.thinking = "s", "m", "low"
                self.proc = mock.Mock(pid=12345, returncode=0)

            def start(self, workdir, resume_session_id=None):
                return False

            def prompt(self, text, timeout):
                return {}

            def collect_usage(self):
                return None

            def cancel(self):
                pass

            def close(self, kill=False):
                pass

            def _flush_message(self):
                pass

        saved = {n: getattr(d, n) for n in (
            "artifact_dir", "dev_workdir_for", "post_event", "post_run_record",
            "update_session_info", "backfill_spawn_session", "kimi_version", "dev_branch",
            "owner_key", "handle_result", "save_state", "append_kill_redline", "role_allowed",
            "sync_agent_session", "role_model", "resolve_role_model")}
        d.artifact_dir = lambda r: art
        d.dev_workdir_for = lambda r: wd
        d.post_event = d.post_run_record = d.update_session_info = d.save_state = lambda *a, **k: None
        d.backfill_spawn_session = d.sync_agent_session = d.handle_result = lambda *a, **k: None
        d.kimi_version = lambda: "test"
        d.dev_branch = lambda r: "f-test"
        d.owner_key = lambda r: "owner"
        d.append_kill_redline = lambda prompt, pid: prompt
        d.role_allowed = lambda r, role: True

        def fake_role_model(role, project=None):
            calls.append((role, project))
            return ""

        def fake_resolve(role, project=None):
            # #11 建会话取模改经 resolve_role_model（其内部再调 role_model_detail）——
            # 断言点不变：project 必须被透传
            calls.append((role, project))
            return {"model": "", "source": "global", "configured": "", "configured_source": "global",
                    "degraded": False, "reason": None}

        d.role_model = fake_role_model
        d.resolve_role_model = fake_resolve
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "sessions": {},
                   "worktrees": {}, "held_procs": {}}
        try:
            with mock.patch.object(d, "ACPSession", FakeSession):
                d.run_agent(req, "dev", "p", "2026-10-03T00:00:00")
        finally:
            for name, val in saved.items():
                setattr(d, name, val)
        self.assertIn(("dev", "req-board"), calls)
        self.assertTrue(all(p == "req-board" for _r, p in calls), calls)


if __name__ == "__main__":
    unittest.main()
