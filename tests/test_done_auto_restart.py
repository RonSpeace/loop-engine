#!/usr/bin/env python3
"""req-380 done 流程自动重启调度器（dispatcher.py 改动免手动重启）测试。

改动：_set_done 命中 dispatcher.py（dispatcher_touched=True）时不再只发手动提醒——在函数
末尾登记 state["pending_restart"][req_id] = {at, attempts}（done 通知/合并/看板重启完成后，
#266 时序）；主循环每轮 _maybe_auto_restart 检查：pending 已写满 POLL_INTERVAL + active_runs
空 + 无外部存疑 kimi 进程 → 写 data/dispatcher_restart_source.json（source=auto）→ kill -TERM
自身（KeepAlive 拉起，复用 #373 来源标记）；attempts 超 MAX_AUTO_RESTART_ATTEMPTS 转原手动
提醒兜底；新进程启动 _verify_pending_restart 按心跳 last_shutdown 自证并清标记 + 补发
「调度器已自动重启，dispatcher.py 改动已生效」通知。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899（#343）
# #8 标记隔离：钉「无标记=launchd 旧路径」基线（ambient 标记随进程树进入测试进程；本文件断言
# KeepAlive 拉起文案与 kill 自身路径）。pytest 侧另有 conftest autouse 兜底。
os.environ.pop("REQ_BOARD_SUPERVISOR", None)

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_REAL_SET_DONE = d._set_done

# 各测试类会替换多个 dispatcher 模块级函数/变量；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "_kickstart_board_if_own", "_queue_auto_restart",
    "_stray_acp_pids", "_maybe_auto_restart", "reconcile_decisions", "load_heartbeat",
    "write_heartbeat", "STATE_FILE", "_state_loaded", "RESTART_SOURCE_FILE", "active_runs",
    "_maybe_sweep_empty_sessions")}


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fresh_state():
    return {"chat_id": "oc_req380_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


class SetDoneSchedulesRestartTest(unittest.TestCase):
    """_set_done 命中 dispatcher.py：登记 pending_restart + 文案改为自动重启；未命中不登记"""

    def setUp(self):
        d.state = fresh_state()
        d._set_done = _REAL_SET_DONE     # 恢复真实函数（可能被前面的测试类替换成记录器）
        self.summaries = []
        self.events = []
        self.card_notifies = []
        d.save_state = lambda: None
        d.set_status = lambda rid, st: None
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append(k)
        d._kickstart_board_if_own = lambda *a, **k: None

    def tearDown(self):
        _restore_d()

    def test_t1_hit_registers_pending_and_new_text(self):
        """命中：pending_restart 登记 {at, attempts:0}；done 文案「调度器将自动重启」不再要求手动 kill"""
        d._set_done({"id": 99, "project": "req-board", "title": "T"}, dispatcher_touched=True)
        self.assertIn("99", d.state.get("pending_restart", {}))
        rec = d.state["pending_restart"]["99"]
        self.assertEqual(rec["attempts"], 0)
        self.assertIsInstance(rec["at"], float)
        kw = self.card_notifies[0]
        self.assertIn("dispatcher.py", kw["reminder"])
        self.assertIn("自动重启", kw["reminder"])
        self.assertNotIn("kill <pid>", kw["reminder"])
        self.assertTrue(any("dispatcher.py" in s and "自动重启" in s for s in self.summaries))
        self.assertTrue(any("自动重启" in c for _, c in self.events))

    def test_t2_busy_delayed_text(self):
        """存在并行 agent：done 文案「将在并行 agent 结束后自动重启」"""
        d.active_runs = {"7": {"proc": None, "project": "x"}}
        d._set_done({"id": 99, "project": "req-board", "title": "T"}, dispatcher_touched=True)
        kw = self.card_notifies[0]
        self.assertIn("并行 agent 结束后", kw["reminder"])
        self.assertTrue(any("并行 agent 结束后" in c for _, c in self.events))
        self.assertIn("99", d.state.get("pending_restart", {}))

    def test_t3_no_hit_no_pending(self):
        """未命中：不登记 pending_restart，done 行为保持（无提醒）"""
        d._set_done({"id": 98, "project": "req-board", "title": "T"})
        self.assertEqual(d.state.get("pending_restart", {}), {})
        self.assertEqual(self.card_notifies[0]["reminder"], "")
        self.assertEqual(self.summaries, [])

    def test_t4_pending_last_after_notify_and_kickstart(self):
        """#266/#380 时序：pending_restart 登记必须在 summary/事件/卡片/看板重启之后"""
        order = []
        d.append_summary = lambda rid, line: order.append("summary")
        d.post_dispatcher_event = lambda rid, content, kind="action": order.append("event")
        d.notify_req_card = lambda event, req, **k: order.append("card")
        d._kickstart_board_if_own = lambda project, req_id: order.append("kickstart")
        d._queue_auto_restart = lambda rid: order.append("pending")
        d._set_done({"id": 99, "project": "req-board", "title": "T"}, dispatcher_touched=True)
        self.assertEqual(order, ["summary", "event", "card", "kickstart", "pending"])

    def test_t5_short_circuit_survives_manual_hint_fallback(self):
        """生成兜底文案仍存在（自动重启多次失败时 _fallback_manual_hint 复用）"""
        self.assertIn("kill <pid>", d.DISPATCHER_RESTART_HINT)
        self.assertEqual(d.MAX_AUTO_RESTART_ATTEMPTS, 3)


class MaybeAutoRestartTest(unittest.TestCase):
    """_maybe_auto_restart：安全阀（满龄/并行/外部进程）与 kill 发起/计数/转手动兜底"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.src_file = Path(self._tmp.name) / "dispatcher_restart_source.json"
        d.state = fresh_state()
        d.save_state = lambda: None
        self.kills = []
        self.summaries = []
        self.events = []
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        self._patch_src = mock.patch.object(d, "RESTART_SOURCE_FILE", self.src_file)
        self._patch_kill = mock.patch.object(d.os, "kill", side_effect=lambda pid, sig: self.kills.append((pid, sig)))
        self._patch_stray = mock.patch.object(d, "_stray_acp_pids", return_value=[])
        self._patch_src.start()
        self._patch_kill.start()
        self._patch_stray.start()

    def tearDown(self):
        self._patch_stray.stop()
        self._patch_kill.stop()
        self._patch_src.stop()
        self._tmp.cleanup()
        _restore_d()

    def _pending(self, rid, at=None, attempts=0):
        d.state.setdefault("pending_restart", {})[str(rid)] = {
            "at": at if at is not None else time.time() - 100, "attempts": attempts}

    def test_t10_ready_restarts(self):
        """满龄 + 无并行 + 无外部进程 → 写来源标记(schema) + os.kill(自身, SIGTERM) + attempts+1"""
        self._pending(99)
        d._maybe_auto_restart()
        self.assertEqual(self.kills, [(os.getpid(), signal.SIGTERM)])
        src = json.loads(self.src_file.read_text())
        self.assertEqual(src["source"], "auto")
        self.assertEqual(d.state["pending_restart"]["99"]["attempts"], 1)

    def test_t11_fresh_pending_skipped(self):
        """写入未满 POLL_INTERVAL → 不 kill（先写通知/合并，主循环下一轮才触发）"""
        self._pending(99, at=time.time())
        d._maybe_auto_restart()
        self.assertEqual(self.kills, [])
        self.assertFalse(self.src_file.exists())
        self.assertEqual(d.state["pending_restart"]["99"]["attempts"], 0)

    def test_t12_busy_skipped_without_counting(self):
        """有并行 agent → 顺延不 kill 不计数"""
        d.active_runs = {"7": {"proc": None}}
        self._pending(99)
        d._maybe_auto_restart()
        self.assertEqual(self.kills, [])
        self.assertEqual(d.state["pending_restart"]["99"]["attempts"], 0)

    def test_t13_stray_skipped_without_counting(self):
        """外部存疑 kimi 进程 → 本轮跳过不 kill 不计数"""
        with mock.patch.object(d, "_stray_acp_pids", return_value=[9999]):
            self._pending(99)
            d._maybe_auto_restart()
        self.assertEqual(self.kills, [])
        self.assertEqual(d.state["pending_restart"]["99"]["attempts"], 0)

    def test_t14_give_up_after_max_attempts(self):
        """attempts 达上限 → 放弃：清 pending + 转「需手动重启（kill <pid>）」提醒，不再 kill"""
        self._pending(99, attempts=d.MAX_AUTO_RESTART_ATTEMPTS)
        d._maybe_auto_restart()
        self.assertEqual(self.kills, [])
        self.assertEqual(d.state.get("pending_restart", {}), {})
        self.assertTrue(any("kill <pid>" in s for s in self.summaries))
        self.assertTrue(any(k == "merge" and "自动重启未能生效" in c and "kill <pid>" in c
                            for k, c in self.events))

    def test_t15_mixed_giveup_one_kill_other(self):
        """多需求混存：达上限的需求放弃转提醒，未达上限的继续 kill（attempts 各自累计）"""
        self._pending(90, attempts=d.MAX_AUTO_RESTART_ATTEMPTS)
        self._pending(91, attempts=0)
        d._maybe_auto_restart()
        self.assertEqual(self.kills, [(os.getpid(), signal.SIGTERM)])
        self.assertIn("91", d.state["pending_restart"])
        self.assertNotIn("90", d.state["pending_restart"])
        self.assertEqual(d.state["pending_restart"]["91"]["attempts"], 1)
        self.assertTrue(any("kill <pid>" in s for s in self.summaries))

    def test_t16_kill_exception_retains_pending(self):
        """os.kill 抛异常：不崩溃，pending 保留待下轮重试"""
        self._pending(99)
        with mock.patch.object(d.os, "kill", side_effect=OSError("boom")):
            d._maybe_auto_restart()
        self.assertIn("99", d.state["pending_restart"])
        self.assertEqual(d.state["pending_restart"]["99"]["attempts"], 1)


class VerifyPendingRestartTest(unittest.TestCase):
    """_verify_pending_restart：新进程启动按心跳自证——清标记 + 按来源补发通知"""

    def setUp(self):
        d.state = fresh_state()
        d.save_state = lambda: None
        self.summaries = []
        self.events = []
        self.hb_writes = []
        d.append_summary = lambda rid, line: self.summaries.append((rid, line))
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((rid, kind, content))
        d.write_heartbeat = lambda extra=None: self.hb_writes.append(extra)

    def tearDown(self):
        _restore_d()

    def _run(self, hb):
        with mock.patch.object(d, "load_heartbeat", return_value=hb):
            d._verify_pending_restart()

    def test_t20_confirmed_auto_success(self):
        """心跳 last_shutdown.at >= pending.at 且来源 auto → 清标记 + 「调度器已自动重启，已生效」（验收文案）"""
        d.state["pending_restart"] = {"99": {"at": 1000.0, "attempts": 1}}
        self._run({"last_poll_at": 3000.0, "last_shutdown": {"at": 2000.0, "signal": "SIGTERM", "source": "auto"}})
        self.assertEqual(d.state.get("pending_restart"), {})
        ok_text = "调度器已自动重启，调度器模块（dispatcher.py / acp_shared.py）改动已生效"  # #423 触发面扩到 acp_shared.py
        self.assertTrue(any(ok_text in s for _, s in self.summaries))
        self.assertTrue(any(k == "merge" and ok_text in c for _, k, c in self.events))
        # 心跳标记透出到看板角标「自动重启成功」
        self.assertIn({"auto_restart_ok": True}, self.hb_writes)

    def test_t21_confirmed_manual_source(self):
        """期间被人工 script 重启：注明来源仍清标记（配置已生效）；非 auto 来源不写角标成功标记"""
        d.state["pending_restart"] = {"99": {"at": 1000.0, "attempts": 1}}
        self._run({"last_shutdown": {"at": 2500.0, "signal": "SIGTERM", "source": "script"}})
        self.assertEqual(d.state.get("pending_restart"), {})
        self.assertTrue(any("来源: script" in s and "已生效" in s for _, s in self.summaries))
        self.assertEqual(self.hb_writes, [])

    def test_t22_unconfirmed_crash_clears_and_notifies(self):
        """心跳无更新的 last_shutdown（异常终止）：仍清标记（防重启循环）+ 注明 KeepAlive 拉起"""
        d.state["pending_restart"] = {"99": {"at": 1000.0, "attempts": 1}}
        self._run({"last_shutdown": {"at": 500.0, "signal": "SIGTERM", "source": "auto"}})
        self.assertEqual(d.state.get("pending_restart"), {})
        self.assertTrue(any("KeepAlive 拉起" in s for _, s in self.summaries))

    def test_t23_no_pending_noop(self):
        """无 pending_restart：不补发任何通知"""
        self._run(None)
        self.assertEqual(self.summaries, [])
        self.assertEqual(self.events, [])

    def test_t24_multi_req_all_notified(self):
        """多需求一并清标记 + 逐条通知"""
        d.state["pending_restart"] = {"90": {"at": 1000.0, "attempts": 1}, "91": {"at": 1100.0, "attempts": 2}}
        self._run({"last_shutdown": {"at": 2000.0, "signal": "SIGTERM", "source": "auto"}})
        self.assertEqual(d.state.get("pending_restart"), {})
        self.assertEqual({rid for rid, _ in self.summaries}, {"90", "91"})


class StrayAcpPidsTest(unittest.TestCase):
    """_stray_acp_pids：pgrep 兜底——排除 board 助手进程（祖先链）与保持池 pid"""

    def setUp(self):
        d.state = fresh_state()
        d.state["held_procs"] = {"a": 300}

    def tearDown(self):
        _restore_d()

    def _run(self, pids, board_pids, ps_lines):
        """按调用顺序 mock：pgrep kimi acp → pgrep board.py → ps 祖先快照"""
        side_effect = [
            mock.Mock(stdout=" ".join(map(str, pids)) + " ", returncode=0),
            mock.Mock(stdout=" ".join(map(str, board_pids)), returncode=0),
            mock.Mock(stdout="\n".join(ps_lines) + "\n", returncode=0),
        ]
        with mock.patch.object(d.subprocess, "run", side_effect=side_effect):
            return d._stray_acp_pids()

    def test_t30_exclude_board_children_and_held(self):
        """100 为 board 子进程、300 为保持池 → 均排除，仅剩 200（孤儿）"""
        out = self._run([100, 200, 300], [5], ["100 5", "200 1", "300 1", "5 1"])
        self.assertEqual(out, [200])

    def test_t31_none_matched(self):
        """pgrep 无命中 → 空（不拦截）"""
        self.assertEqual(self._run([], [], []), [])

    def test_t32_pgrep_exception_allow(self):
        """pgrep 异常（如不可用）→ 空（best-effort，主安全阀是 active_runs）"""
        with mock.patch.object(d.subprocess, "run", side_effect=OSError("pgrep missing")):
            self.assertEqual(d._stray_acp_pids(), [])


class LoadStateDefaultTest(unittest.TestCase):
    """load_state：旧状态文件无 pending_restart 键 → 补默认空 dict"""

    def tearDown(self):
        # #420：还原放 tearDown（原为用例末行，断言失败即跳过）——真实 load_state 会置
        # _state_loaded=True，泄漏到后续用例会让落盘守卫失效且不报错
        _restore_d()

    def test_t40_old_state_back_compat(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "state.json"
            old.write_text(json.dumps({"chat_id": None, "retries": {}}))
            with mock.patch.object(d, "STATE_FILE", old), mock.patch.object(d, "save_state"):
                d.state = {}
                d.load_state()
        self.assertEqual(d.state["pending_restart"], {})


class MainLoopTickTest(unittest.TestCase):
    """#380 S1 修复回归：自动重启检查必须在 spawn 之前——同轮 spawn 的新 agent 线程
    构造完 ACPSession（50-300ms）后才登记 active_runs，若先 spawn 后检查，会出现
    「检查时 active_runs 恰为空 → kill 自身」把新 agent 孤儿化的竞态。"""

    def setUp(self):
        self.order = []
        # _main_loop_tick 内的 reconcile_confirmations/兜底扫描会调 save_state：不隔离就写真实
        # data/dispatcher_state.json（#419 清理现场：本类 2 例直写真实 state）。tearDown 的
        # _restore_d 会还原 STATE_FILE。
        d.STATE_FILE = Path(tempfile.mkdtemp(prefix="t50-state-")) / "dispatcher_state.json"
        d._state_loaded = True   # #420：守卫只放行 load 过的进程（本类断言 tick 正常走完）
        d._maybe_auto_restart = lambda: self.order.append("restart-check")
        # #431：空会话巡检会真发 HTTP 到活看板（BOARD_ROOT_API 默认 127.0.0.1:8899），必须打桩
        d._maybe_sweep_empty_sessions = lambda: None
        d.reconcile_decisions = lambda: self.order.append("reconcile")
        d.write_heartbeat = lambda extra=None: self.order.append("heartbeat")

    def tearDown(self):
        _restore_d()

    def test_t50_restart_check_before_spawn(self):
        """验收：单轮顺序 = 自动重启检查 → 调度（spawn）→ 心跳"""
        d._main_loop_tick()
        self.assertEqual(self.order, ["restart-check", "reconcile", "heartbeat"])

    def test_t51_restart_check_exception_does_not_block(self):
        """重启检查抛异常：本轮跳过（留待下轮），仍执行调度与心跳"""
        def boom():
            self.order.append("restart-check")
            raise RuntimeError("boom")
        d._maybe_auto_restart = boom
        d._main_loop_tick()
        self.assertEqual(self.order, ["restart-check", "reconcile", "heartbeat"])


if __name__ == "__main__":
    unittest.main()
