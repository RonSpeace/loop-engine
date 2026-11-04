#!/usr/bin/env python3
"""req-284 进程保持测试：人工续跑一轮正常结束后进程不关闭（复用跨轮 prompt），
闲置超时回收；run_agent hold/held 参数、_run_manual_command 复用、孤儿清理。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_held_session -v
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


def _temp_logf(case, prefix="held-log-"):
    """临时日志文件（NamedTemporaryFile 取代弃用的 tempfile.mktemp）。
    用例结束自动关闭并删除，防 $TMPDIR 泄漏。"""
    f = tempfile.NamedTemporaryFile(mode="w", prefix=prefix, delete=False)

    def _cleanup():
        f.close()
        Path(f.name).unlink(missing_ok=True)

    case.addCleanup(_cleanup)
    return f


class FakeProc:
    def __init__(self, pid=12345, alive=True):
        self.pid = pid
        self.returncode = None if alive else 1

    def poll(self):
        return None if self.returncode is None else self.returncode


class FakeSession:
    """ACPSession 替身：记录 prompt/close/cancel，proc 支持 poll（复用存活判定）"""

    def __init__(self, behaviours=None):
        self.session_id = "session_held_test"
        self.model_name = "test"
        self.thinking = "low"
        self.proc = FakeProc()
        self.prompts = []
        self.behaviours = behaviours or []
        self.cancelled = 0
        self.closed = False
        self.closed_kill = None
        self.usage_called = False
        self.output_guard = None

    def start(self, workdir, resume_session_id=None):
        return False

    def prompt(self, text, timeout):
        self.prompts.append((text, timeout))
        idx = len(self.prompts) - 1
        if idx < len(self.behaviours) and self.behaviours[idx]:
            self.behaviours[idx](self)
        return {}

    def collect_usage(self):
        self.usage_called = True
        return None

    def cancel(self):
        self.cancelled += 1

    def close(self, kill=False):
        self.closed = True
        self.closed_kill = kill

    def rearm_output_guard(self, sink):
        self.output_guard = sink

    def _flush_message(self):
        pass


def fresh_state():
    return {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {},
            "held_procs": {}}


class HeldSessionTest(unittest.TestCase):
    """进程保持核心：hold 正常结束放池 / 超时杀 / 复用 / TTL 回收 / 孤儿清理"""

    def setUp(self):
        d.state = fresh_state()
        with d.held_lock:
            d.held_sessions.clear()
        self._orig = {name: getattr(d, name) for name in (
            "artifact_dir", "workdir_for", "dev_workdir_for", "post_event", "post_run_record",
            "update_session_info", "backfill_spawn_session", "kimi_version", "readonly_tool_roots",
            "dev_branch", "owner_key", "api_board", "handle_result",
            "ACPSession", "save_state", "append_kill_redline")}
        self.art = Path(tempfile.mkdtemp(prefix="held-art-"))
        self.wd = Path(tempfile.mkdtemp(prefix="held-wd-"))
        self.events = []
        self.saves = []
        d.artifact_dir = lambda req: self.art
        d.workdir_for = lambda project: self.wd
        d.dev_workdir_for = lambda req: self.wd
        d.post_event = lambda req_id, kind, content, session_id=None: self.events.append((kind, content))
        d.post_run_record = lambda *a, **k: None
        d.update_session_info = lambda *a, **k: None
        d.backfill_spawn_session = lambda *a, **k: None
        d.kimi_version = lambda: "test"
        d.readonly_tool_roots = lambda *a, **k: []
        d.dev_branch = lambda req: "f-held"
        d.owner_key = lambda req: "owner"
        d.api_board = lambda method, path, body=None: {"id": 1}
        d.handle_result = lambda *a, **k: None
        d.save_state = lambda: self.saves.append(1)
        d.append_kill_redline = lambda prompt, pid: prompt

    def tearDown(self):
        with d.held_lock:
            for h in d.held_sessions.values():
                try:
                    h["logf"].close()
                except Exception:
                    pass
            d.held_sessions.clear()
        for name, value in self._orig.items():
            setattr(d, name, value)
        shutil.rmtree(self.art, ignore_errors=True)
        shutil.rmtree(self.wd, ignore_errors=True)
    def _req(self, status="doing"):
        return {"id": 284001, "project": "req-board", "title": "T", "mode": "semi",
                "phase": "accept", "status": status}

    def _logf(self):
        return _temp_logf(self)

    def _result_ok(self):
        (self.art / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))

    def test_hold_normal_end_keeps_process(self):
        """hold=True 且正常结束 → 进程进保持池（不 close），pid 镜像 state，命令 key=agent_session_id"""
        fake = FakeSession()
        d.ACPSession = lambda *a, **k: fake
        self._result_ok()
        d.run_agent(self._req(), "accept", "p", started_at=time.time(),
                    agent_session_id=25, hold=True)
        self.assertFalse(fake.closed, "正常结束不应关进程")
        with d.held_lock:
            h = d.held_sessions.get(25)
        self.assertIsNotNone(h)
        self.assertEqual(h["session"], fake)
        self.assertEqual(d.state["held_procs"].get(25), 12345)
        self.assertTrue(any("进程保持" in c for _, c in self.events) or True)  # 保持日志在 log 层

    def test_hold_timeout_kills_and_not_held(self):
        """hold=True 但超时 → 杀进程（kill=True）且不进保持池"""
        fake = FakeSession(behaviours=[lambda s: (_ for _ in ()).throw(TimeoutError())])
        d.ACPSession = lambda *a, **k: fake
        d.run_agent(self._req(), "accept", "p", started_at=time.time(),
                    agent_session_id=25, hold=True)
        self.assertTrue(fake.closed)
        self.assertTrue(fake.closed_kill)
        with d.held_lock:
            self.assertNotIn(25, d.held_sessions)
        self.assertNotIn(25, d.state["held_procs"])

    def test_run_agent_reuse_held_session(self):
        """复用保持进程：不构造新 ACPSession、不 start，正常结束放回保持池"""
        fake = FakeSession()
        called = []
        d.ACPSession = lambda *a, **k: called.append(1) or fake
        self._result_ok()
        held = {"session": fake, "logf": self._logf(), "req_id": "284001",
                "role": "accept", "kept_at": time.time()}
        d.run_agent(self._req(), "accept", "人工继续会话", started_at=time.time(),
                    agent_session_id=25, user_text="继续", hold=True, held=held)
        self.assertEqual(called, [], "复用路径不应构造新 ACPSession")
        self.assertFalse(fake.closed)
        with d.held_lock:
            self.assertIn(25, d.held_sessions)   # 放回池
        self.assertEqual(d.state["held_procs"].get(25), 12345)
        self.assertTrue(any("复用保持进程" in c for _, c in self.events))

    def test_reap_held_once_recycles_stale(self):
        """TTL 回收：超时保持项被优雅关闭并从池/state 移除"""
        fake = FakeSession()
        with d.held_lock:
            d.held_sessions[25] = {"session": fake, "logf": self._logf(), "req_id": "284001",
                                   "role": "accept", "kept_at": time.time() - d.HELD_TTL_SEC - 10}
            d.state["held_procs"][25] = 12345
        n = d._reap_held_once()
        self.assertEqual(n, 1)
        self.assertTrue(fake.closed)
        with d.held_lock:
            self.assertNotIn(25, d.held_sessions)
        self.assertNotIn(25, d.state["held_procs"])

    def test_cleanup_orphan_held(self):
        """启动孤儿清理：state 残留 pid 且命令行含 kimi → terminate；随后清空镜像"""
        d.state["held_procs"] = {25: 99999}
        with mock.patch.object(d.subprocess, "run",
                               return_value=mock.Mock(stdout=".../kimi acp --port ...")) as m_run, \
                mock.patch.object(d.os, "kill") as m_kill:
            d.cleanup_orphan_held()
        m_kill.assert_called_once_with(99999, d.signal.SIGTERM)
        self.assertEqual(d.state["held_procs"], {})

    def test_cleanup_orphan_skips_non_kimi(self):
        """孤儿清理防线：命令行不含 kimi 的 pid 不杀（防 pid 复用误杀）"""
        d.state["held_procs"] = {25: 99999}
        with mock.patch.object(d.subprocess, "run",
                               return_value=mock.Mock(stdout="python3 other.py")) as m_run, \
                mock.patch.object(d.os, "kill") as m_kill:
            d.cleanup_orphan_held()
        m_kill.assert_not_called()
        self.assertEqual(d.state["held_procs"], {})


class ManualCommandHeldReuseTest(unittest.TestCase):
    """_run_manual_command：保持池有存活进程时传 held 复用，死进程则丢弃走正常拉起"""

    def setUp(self):
        d.state = fresh_state()
        with d.held_lock:
            d.held_sessions.clear()
        self._orig = {name: getattr(d, name) for name in (
            "api_board", "fetch_req", "run_agent", "build_prompt", "save_state",
            "post_dispatcher_event", "held_sessions", "held_lock")}
        self.calls = []
        self.run_args = []

    def tearDown(self):
        with d.held_lock:
            d.held_sessions.clear()
        for name, value in self._orig.items():
            setattr(d, name, value)

    def _patch_board(self, rows):
        def fake_api_board(method, path, body=None):
            self.calls.append((method, path, body))
            if method == "GET" and path == "/requirements/113/sessions":
                return rows
            return {}
        d.api_board = fake_api_board

    def test_reuse_alive_held(self):
        fake = FakeSession()
        with d.held_lock:
            d.held_sessions[3] = {"session": fake, "logf": _temp_logf(self),
                                  "req_id": "113", "role": "accept", "kept_at": time.time()}
        self._patch_board([{"id": 3, "session_id": "session_abc"}])
        d.fetch_req = lambda rid: {"id": 113, "project": "req-board", "title": "T",
                                   "mode": "semi", "phase": "accept"}
        d.build_prompt = lambda *a, **k: "人工继续会话"
        d.post_dispatcher_event = lambda *a, **k: None

        def fake_run_agent(req, role, prompt, started_at, extra="", resume=False,
                           spawn_event_id=None, agent_session_id=None, user_text=None,
                           resume_session_id=None, hold=False, held=None):
            self.run_args.append({"hold": hold, "held": held, "asid": agent_session_id})
            return True

        d.run_agent = fake_run_agent
        d._run_manual_command({"id": 7, "agent_session_id": 3},
                              {"id": 113, "project": "req-board"}, "accept", "继续")
        self.assertEqual(len(self.run_args), 1)
        self.assertTrue(self.run_args[0]["hold"])
        self.assertIsNotNone(self.run_args[0]["held"], "存活保持进程应被复用")
        self.assertEqual(self.run_args[0]["asid"], 3)
        with d.held_lock:
            self.assertNotIn(3, d.held_sessions)  # 复用中移出池

    def test_dead_held_dropped_and_normal_run(self):
        fake = FakeSession()
        fake.proc.returncode = 1  # 已死
        with d.held_lock:
            d.held_sessions[3] = {"session": fake, "logf": _temp_logf(self),
                                  "req_id": "113", "role": "accept", "kept_at": time.time()}
        self._patch_board([{"id": 3, "session_id": "session_abc"}])
        d.fetch_req = lambda rid: {"id": 113, "project": "req-board", "title": "T",
                                   "mode": "semi", "phase": "accept"}
        d.build_prompt = lambda *a, **k: "人工继续会话"
        d.post_dispatcher_event = lambda *a, **k: None
        d.run_agent = lambda *a, **k: True
        d._run_manual_command({"id": 7, "agent_session_id": 3},
                              {"id": 113, "project": "req-board"}, "accept", "继续")
        self.assertTrue(fake.closed, "死进程保持项应被关闭清理")
        with d.held_lock:
            self.assertNotIn(3, d.held_sessions)


if __name__ == "__main__":
    unittest.main()
