#!/usr/bin/env python3
"""req-412 调度器死锁防御测试（T1-T13 + T15）。

改动：dispatcher.py——
- S1: set_status 终态清 confirm_rounds 时 save_state 移锁外（旧实现锁内调用二次取非重入
  state_lock 恒死锁，#410 现场根因）
- S1b: save_state 锁内仅序列化、锁外原子写（tmp + os.replace），state_lock 内无文件 IO
- A1: _reap_held_once 锁内快照+pop、_close_held 移锁外（close 阻塞不卡 held_lock）
- A2: _run_manual_command 复用路径进程已死分支同型锁外 close
- B: run_agent 尾部 handle_result 包 try/except + log.exception（异常必留痕）
- D: 主循环看门狗（last_tick + 独立线程检测，只告警不 kill）

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest tests.test_deadlock_defense -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

# setUp/tearDown 恢复用的真实值快照（基类 tearDown 统一恢复）
_REALS = {name: getattr(d, name) for name in (
    "state", "STATE_FILE", "_state_loaded", "api", "save_state", "held_sessions", "subprocess",
    "last_tick", "last_tick_mono", "PROJECTS")}


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
    """ACPSession 替身（同 test_held_session）：proc 支持 poll；_flush_message 可注入阻塞"""

    def __init__(self, block_event=None):
        self.session_id = "session_deadlock_test"
        self.model_name = "test"
        self.thinking = "low"
        self.proc = FakeProc()
        self.prompts = []
        self.block_event = block_event
        self.closed = False
        self.closed_kill = None

    def start(self, workdir, resume_session_id=None):
        return False

    def prompt(self, text, timeout):
        self.prompts.append((text, timeout))
        return {}

    def collect_usage(self):
        return None

    def cancel(self):
        pass

    def close(self, kill=False):
        self.closed = True
        self.closed_kill = kill

    def _flush_message(self):
        if self.block_event is not None:
            self.block_event.wait(5)


def fresh_state(confirm_rounds=None):
    return {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {},
            "held_procs": {}, "confirm_rounds": dict(confirm_rounds or {})}


def _req(rid=412001, status="doing"):
    return {"id": rid, "project": "req-board", "title": "T", "mode": "semi",
            "phase": "accept", "status": status}


class DeadlockBase(unittest.TestCase):
    def tearDown(self):
        for name, value in _REALS.items():
            setattr(d, name, value)
        try:
            d.held_sessions.clear()
        except Exception:
            pass


class SetStatusDeadlockTest(DeadlockBase):
    """T1-T5：S1/S1b 回归锁——真实 save_state（patch STATE_FILE 临时路径），
    修复前锁内嵌套 save_state 二次取非重入 state_lock 恒死锁（线程 join 限时=失败）"""

    def setUp(self):
        d.state = fresh_state(confirm_rounds={"412001": 1})
        self._state_dir = Path(tempfile.mkdtemp(prefix="deadlock-state-"))
        d.STATE_FILE = self._state_dir / "dispatcher_state.json"
        # #420：落盘守卫只放行 load 过的进程；本类 T4/T5 直接调真实 save_state 并断言落盘
        d._state_loaded = True
        self.api_calls = []
        d.api = lambda method, path, body=None: (self.api_calls.append((method, path, body)) or
                                                 {"success": True})

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self._state_dir, ignore_errors=True)

    def test_set_status_done_confirm_rounds_no_deadlock(self):
        """T1：confirm_rounds 有键时 set_status(done) 限时内完成 + PATCH(done) 被调（回归锁）"""
        t = threading.Thread(target=lambda: d.set_status(412001, "done"), daemon=True)
        t.start()
        t.join(2)
        self.assertFalse(t.is_alive(), "set_status(done) 2s 未完成：疑似锁内 save_state 死锁")
        self.assertNotIn("412001", d.state["confirm_rounds"])
        self.assertIn(("PATCH", "/412001", {"status": "done"}), self.api_calls)

    def test_set_status_later_confirm_rounds_clears_key(self):
        """T2：later 清键 + PATCH(later) + 真实 save_state 落盘无键"""
        d.set_status(412001, "later")
        self.assertNotIn("412001", d.state["confirm_rounds"])
        self.assertIn(("PATCH", "/412001", {"status": "later"}), self.api_calls)
        saved = json.loads(d.STATE_FILE.read_text()) if d.STATE_FILE.exists() else {}
        self.assertNotIn("412001", saved.get("confirm_rounds", {}))
        self.assertEqual(saved.get("confirm_rounds"), {})
        # done 路径同样清键
        d.state["confirm_rounds"]["412001"] = 2
        d.set_status(412001, "done")
        self.assertNotIn("412001", d.state["confirm_rounds"])
        saved = json.loads(d.STATE_FILE.read_text())
        self.assertNotIn("412001", saved.get("confirm_rounds", {}))

    def test_set_status_no_confirm_rounds_unchanged(self):
        """T3：无键行为不变（PATCH 发出、save_state 不被调）"""
        d.state["confirm_rounds"].clear()
        with mock.patch.object(d, "save_state") as m_save:
            d.set_status(412001, "done")
        self.assertIn(("PATCH", "/412001", {"status": "done"}), self.api_calls)
        m_save.assert_not_called()

    def test_save_state_no_io_inside_state_lock(self):
        """T4：save_state 执行期间 state_lock 可获取（锁内无文件 IO）；tmp 无残留、文件可解析"""
        d.state["retries"] = {"x": {"last_failure": "测试"}}
        real_replace = d.os.replace
        observed = {}

        def spy_replace(src, dst):
            acquired = d.state_lock.acquire(blocking=False)
            observed["locked_during_write"] = not acquired
            if acquired:
                d.state_lock.release()
            return real_replace(src, dst)

        with mock.patch.object(d.os, "replace", side_effect=spy_replace):
            d.save_state()
        self.assertFalse(observed.get("locked_during_write", True),
                         "save_state 写盘（replace）时 state_lock 仍被持有（锁内 IO）")
        self.assertFalse(d.STATE_FILE.with_name(d.STATE_FILE.name + ".tmp").exists(),
                         "原子写 tmp 有残留")
        saved = json.loads(d.STATE_FILE.read_text())
        self.assertEqual(saved.get("retries"), {"x": {"last_failure": "测试"}})

    def test_save_state_atomic_replace(self):
        """T5：写盘失败（os.replace 抛 OSError）→ 异常抛出但 state_lock 已释放、旧文件不被破坏"""
        d.STATE_FILE.write_text(json.dumps({"keep": 1}))
        with mock.patch.object(d.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                d.save_state()
        self.assertTrue(d.state_lock.acquire(blocking=False), "写盘异常后 state_lock 应已释放")
        d.state_lock.release()
        self.assertEqual(json.loads(d.STATE_FILE.read_text()), {"keep": 1},
                         "旧 STATE_FILE 不应被破坏")


class ReapHeldLockTest(DeadlockBase):
    """T6-T8：_reap_held_once 锁外 close（A1）"""

    def setUp(self):
        d.state = fresh_state()
        d.held_sessions = {}
        d.state["held_procs"] = {}
        d.save_state = lambda: None  # 本组不测 save_state（避免写真实 STATE_FILE）
        self._close_orig = d._close_held

    def tearDown(self):
        d._close_held = self._close_orig
        super().tearDown()

    def _stale_held(self, block_event=None):
        h = {"session": FakeSession(block_event=block_event),
             "logf": _temp_logf(self),
             "req_id": "412001", "role": "accept",
             "kept_at": time.time() - d.HELD_TTL_SEC - 10}
        d.held_sessions[25] = h
        d.state["held_procs"][25] = h["session"].proc.pid

    def test_reap_held_once_close_outside_lock(self):
        """T6：_close_held 阻塞（Event.wait）期间 held_lock 可获取（close 在锁外）"""
        g = threading.Event()
        self._stale_held(block_event=g)
        t = threading.Thread(target=d._reap_held_once, daemon=True)
        t.start()
        time.sleep(0.2)
        self.assertTrue(d.held_lock.acquire(timeout=1),
                        "_close_held 阻塞时 held_lock 应可获取（close 未在锁内）")
        d.held_lock.release()
        g.set()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(d.held_sessions, {}, "回收后池应清空")
        self.assertEqual(d.state["held_procs"], {})

    def test_reap_held_once_close_raises_still_reaps_all(self):
        """T7：_close_held 首项抛异常 → 其余仍回收、计数正确（单项异常不中断循环）"""
        self._stale_held()
        d.held_sessions[26] = {"session": FakeSession(),
                               "logf": _temp_logf(self),
                               "req_id": "412001", "role": "accept",
                               "kept_at": time.time() - d.HELD_TTL_SEC - 10}
        d.state["held_procs"][26] = 999
        calls = {"n": 0}
        orig = d._close_held

        def counting_close(h):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return orig(h)

        d._close_held = counting_close
        n = d._reap_held_once()
        self.assertEqual(n, 2)
        self.assertEqual(calls["n"], 2, "首项异常后应继续回收其余项")
        self.assertEqual(d.held_sessions, {})
        self.assertEqual(d.state["held_procs"], {})
        self.assertTrue(d.held_lock.acquire(blocking=False), "回收后 held_lock 应释放")
        d.held_lock.release()

    def test_reap_held_once_pool_consistent(self):
        """T8：回收后 held_sessions/held_procs 无残留（兼容 test_held_session.py:181-193）"""
        self._stale_held()
        n = d._reap_held_once()
        self.assertEqual(n, 1)
        self.assertEqual(d.held_sessions, {}, "池应清空")
        self.assertEqual(d.state["held_procs"], {}, "pid 镜像应清空")


class ManualReuseLockTest(DeadlockBase):
    """T9：_run_manual_command 复用路径进程已死 → close 锁外（A2）"""

    def setUp(self):
        d.state = fresh_state()
        d.held_sessions = {}
        d.state["held_procs"] = {}
        d.save_state = lambda: None  # 本组不测 save_state（避免写真实 STATE_FILE）
        self._orig = {name: getattr(d, name) for name in (
            "api_board", "fetch_req", "run_agent", "build_prompt",
            "post_dispatcher_event")}

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(d, name, value)
        super().tearDown()

    def test_manual_reuse_close_outside_lock(self):
        gate = threading.Event()
        dead = {"session": FakeSession(block_event=gate), "logf": _temp_logf(self),
                "req_id": "113", "role": "accept", "kept_at": time.time()}
        dead["session"].proc.returncode = 1  # 进程已死
        d.held_sessions[3] = dead
        d.state["held_procs"][3] = 12345
        d.api_board = lambda method, path, body=None: (
            [{"id": 3, "session_id": "session_abc"}] if method == "GET" else {})
        d.fetch_req = lambda rid: _req(113, status="doing")
        d.build_prompt = lambda *a, **k: "人工继续会话"
        d.run_agent = lambda *a, **k: True
        d.post_dispatcher_event = lambda *a, **k: None
        t = threading.Thread(target=d._run_manual_command,
                             args=({"id": 7, "agent_session_id": 3}, _req(113), "accept", "继续"),
                             daemon=True)
        t.start()
        time.sleep(0.2)
        self.assertTrue(d.held_lock.acquire(timeout=1),
                        "死进程 close 阻塞时 held_lock 应可获取（close 未在锁内）")
        d.held_lock.release()
        gate.set()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertNotIn(3, d.held_sessions, "死进程保持项应已清理")
        self.assertNotIn(3, d.state["held_procs"])


class HandleResultExceptionTest(DeadlockBase):
    """T10-T11：run_agent 尾部 handle_result 异常留痕（B）"""

    def setUp(self):
        d.state = fresh_state()
        self.art = Path(tempfile.mkdtemp(prefix="deadlock-art-"))
        self.wd = Path(tempfile.mkdtemp(prefix="deadlock-wd-"))
        self._orig = {name: getattr(d, name) for name in (
            "artifact_dir", "workdir_for", "dev_workdir_for", "post_event",
            "post_run_record", "update_session_info", "backfill_spawn_session",
            "kimi_version", "readonly_tool_roots", "dev_branch",
            "owner_key", "api_board", "handle_result", "ACPSession", "save_state",
            "append_kill_redline")}
        d.artifact_dir = lambda req: self.art
        d.workdir_for = lambda project: self.wd
        d.dev_workdir_for = lambda req: self.wd
        d.post_event = lambda req_id, kind, content, session_id=None: None
        d.post_run_record = lambda *a, **k: None
        d.update_session_info = lambda *a, **k: None
        d.backfill_spawn_session = lambda *a, **k: None
        d.kimi_version = lambda: "test"
        d.readonly_tool_roots = lambda *a, **k: []
        d.dev_branch = lambda req: "f-deadlock"
        d.owner_key = lambda req: "owner"
        d.api_board = lambda method, path, body=None: {"id": 1}
        d.save_state = lambda: None
        d.append_kill_redline = lambda prompt, pid: prompt
        self.fake = FakeSession()
        d.ACPSession = lambda *a, **k: self.fake
        (self.art / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(d, name, value)
        shutil.rmtree(self.art, ignore_errors=True)
        shutil.rmtree(self.wd, ignore_errors=True)
        super().tearDown()

    def test_handle_result_exception_logged(self):
        """T10：handle_result 抛 RuntimeError → run_agent 返回 True 不冒泡 + 日志含 traceback"""
        def boom(*a, **k):
            raise RuntimeError("handle_result boom")
        d.handle_result = boom
        with self.assertLogs("dispatcher", level="ERROR") as cm:
            ret = d.run_agent(_req(), "accept", "p", started_at=time.time(),
                              agent_session_id=25, hold=True)
        self.assertTrue(ret)
        self.assertTrue(any(r.exc_info and "handle_result 异常" in r.getMessage()
                            for r in cm.records), "应记录 handle_result 异常 traceback")

    def test_handle_result_normal_path_unchanged(self):
        """T11：无异常行为不变（返回 True，进程正常保持）"""
        runs = []
        d.handle_result = lambda *a, **k: runs.append(1)
        ret = d.run_agent(_req(), "accept", "p", started_at=time.time(),
                          agent_session_id=25, hold=True)
        self.assertTrue(ret)
        self.assertEqual(len(runs), 1)
        self.assertFalse(self.fake.closed, "正常结束不应关进程")


class WatchdogTest(DeadlockBase):
    """T12：看门狗检测 tick 停滞（D）"""

    def test_watchdog_flags_stalled_tick(self):
        d.last_tick_mono = time.monotonic() - d.TICK_WARN_SEC * 2
        with self.assertLogs("dispatcher", level="WARNING") as cm:
            stalled = d._watchdog_check()
        self.assertTrue(stalled)
        self.assertTrue(any("主循环 tick 停滞" in r.getMessage() for r in cm.records))

    def test_watchdog_normal_no_warning(self):
        """last_tick 正常（无老化）→ 不告警"""
        d.last_tick_mono = time.monotonic()
        self.assertFalse(d._watchdog_check())

    def test_watchdog_zero_last_tick_silent(self):
        """last_tick=0（未初始化）→ 不告警（保护 watch dog 启动窗口）"""
        d.last_tick_mono = 0.0
        self.assertFalse(d._watchdog_check())

    def test_watchdog_ignores_sleep_wall_clock_jump(self):
        """2026-09-17 修复回归：系统睡眠时 wall clock 继续走、monotonic 暂停——
        合盖 8 小时后不得误报 tick 停滞（现场每天 26~115 条误报的根因）。"""
        d.last_tick_mono = time.monotonic()          # monotonic 新鲜（睡眠不消耗）
        d.last_tick = time.time() - 8 * 3600         # wall clock 已老 8 小时
        self.assertFalse(d._watchdog_check(), "睡眠造成的 wall clock 跳变不应触发停滞告警")


class MergeTimeoutAbortTest(DeadlockBase):
    """T13：merge_dev_branches 超时 abort 兜底（C）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deadlock-merge-"))
        d.PROJECTS = {"req-board": {"paths": [str(self.tmp)]}}

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merge_timeout_aborts(self):
        """超时 → 对该仓库调 git merge --abort + 返回 failed"""
        with mock.patch.object(d.subprocess, "run") as m_run:
            m_run.side_effect = [
                subprocess.TimeoutExpired(["git", "merge"], d.GIT_TIMEOUT),
                mock.Mock(returncode=0),  # abort 调用
            ]
            ok, failed = d.merge_dev_branches(_req(412001))
        self.assertFalse(ok)
        self.assertEqual(failed, [str(self.tmp)])
        self.assertEqual(m_run.call_count, 2)
        self.assertEqual(m_run.call_args_list[1][0][0], ["git", "merge", "--abort"])

    def test_merge_conflict_keeps_现场(self):
        """正常冲突（returncode!=0）不 abort（保留冲突现场给 merge_resolve）"""
        with mock.patch.object(d.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=1)
            ok, failed = d.merge_dev_branches(_req(412001))
        self.assertFalse(ok)
        self.assertEqual(failed, [str(self.tmp)])
        self.assertEqual(m_run.call_count, 1, "冲突路径不应调用 abort")
        self.assertEqual(m_run.call_args[0][0][:3], ["git", "merge", "--no-ff"])


class StaticScanTest(DeadlockBase):
    """T15：save_state 调用点不得处于 with state_lock: 块内（S1 同类回归防线）"""

    def test_no_save_state_inside_state_lock(self):
        src = Path(d.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            holds_state_lock = any(
                (isinstance(it.context_expr, ast.Name) and it.context_expr.id == "state_lock")
                or (isinstance(it.context_expr, ast.Call)
                    and isinstance(it.context_expr.func, ast.Name)
                    and it.context_expr.func.id == "state_lock")
                for it in node.items
            )
            if not holds_state_lock:
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == "save_state"):
                    offenders.append(node.lineno)
        self.assertEqual(offenders, [],
                         f"save_state 出现在 with state_lock: 块内（行 {offenders}）——"
                         f"同 #410 S1 死锁形态，禁止锁内调 save_state")


if __name__ == "__main__":
    unittest.main()
