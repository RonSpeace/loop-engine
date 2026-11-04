#!/usr/bin/env python3
"""#428 残留 worktree 定期对账测试：安全闸（分支未合并 / worktree 有未提交改动一律不放行）、
done/dropped/卡不存在才处理、阻塞告警去重、主循环巡检节流。

背景：#428 人工合并 + 人工置 done 不走调度器收尾，worktree 与分支一直残留到下次重启——原
cleanup_stale_worktrees 只在 main() 启动时跑一次。改为启动 + 主循环定期执行后，必须同时加安全闸：
定期清理不得把「人工置 done 但分支还没合并」的现场强删（会丢内容）。

运行（不跑真实 git，subprocess 全 mock）：
    .venv/bin/python -m unittest tests.test_stale_worktree_cleanup -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore）；本测试不调 load_state，
save_state 由 mock 接管，不碰真实 data/dispatcher_state.json。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import dispatcher as d  # noqa: E402


def _git_run(clean=True, merged=True):
    """假 git：status --porcelain 按 clean 返回；merge-base --is-ancestor 按 merged 返回。"""
    def run(cmd, **kw):
        if list(cmd[:2]) == ["git", "status"]:
            return mock.Mock(returncode=0, stdout="" if clean else " M board.py\n", stderr="")
        if list(cmd[:3]) == ["git", "merge-base", "--is-ancestor"]:
            return mock.Mock(returncode=0 if merged else 1, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    return run


class StaleWorktreeBase(unittest.TestCase):
    """共用隔离：state / api / git / cleanup / save_state / 事件全 mock。"""

    def setUp(self):
        self._state = d.state
        self._projects = d.PROJECTS
        d.state = {"worktrees": {}, "stale_wt_blocked": {}}
        d.PROJECTS = {"req-board": {"paths": [str(REPO)]}}
        self.tmpdir = Path(tempfile.mkdtemp(prefix="req-stale-wt-"))

    def tearDown(self):
        d.state = self._state
        d.PROJECTS = self._projects
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rec(self, wt=None):
        return {"project": "req-board", "branch": "f-20260918-428",
                "base": {}, "worktrees": [(str(REPO), wt or str(self.tmpdir / "wt"))]}

    def _run_cleanup(self, status="done", clean=True, merged=True, include_req=True):
        reqs = [{"id": 428, "status": status}] if include_req else []
        with mock.patch.object(d, "api", return_value=reqs), \
             mock.patch.object(d.subprocess, "run", side_effect=_git_run(clean, merged)), \
             mock.patch.object(d, "_default_branch", return_value=("main", "abc123")), \
             mock.patch.object(d, "cleanup_dev_worktrees") as cleanup, \
             mock.patch.object(d, "save_state"), \
             mock.patch.object(d, "post_dispatcher_event") as event:
            d.cleanup_stale_worktrees()
        return cleanup, event


class BlockerTest(StaleWorktreeBase):
    """安全闸判定（_stale_worktree_blocker）"""

    def test_allows_when_merged_and_no_worktree_dir(self):
        with mock.patch.object(d.subprocess, "run", side_effect=_git_run()), \
             mock.patch.object(d, "_default_branch", return_value=("main", "abc")):
            self.assertEqual(d._stale_worktree_blocker(self._rec()), "")

    def test_blocks_unmerged_branch(self):
        with mock.patch.object(d.subprocess, "run", side_effect=_git_run(merged=False)), \
             mock.patch.object(d, "_default_branch", return_value=("main", "abc")):
            why = d._stale_worktree_blocker(self._rec())
        self.assertIn("未合并入主干", why)
        self.assertIn("f-20260918-428", why)

    def test_blocks_dirty_worktree(self):
        wt = self.tmpdir / "wt"
        wt.mkdir()
        with mock.patch.object(d.subprocess, "run", side_effect=_git_run(clean=False)), \
             mock.patch.object(d, "_default_branch", return_value=("main", "abc")):
            why = d._stale_worktree_blocker(self._rec(str(wt)))
        self.assertIn("未提交改动", why)

    def test_blocks_when_no_branch_recorded(self):
        self.assertIn("缺分支名", d._stale_worktree_blocker({"worktrees": []}))


class CleanupTest(StaleWorktreeBase):
    """cleanup_stale_worktrees 主流程"""

    def test_recovers_done_req_when_safe(self):
        d.state["worktrees"]["428"] = self._rec()
        cleanup, event = self._run_cleanup(status="done")
        cleanup.assert_called_once_with(428, delete_branch=True)
        event.assert_not_called()
        self.assertNotIn("428", d.state.get("stale_wt_blocked") or {})

    def test_recovers_dropped_and_missing_req(self):
        for status, include in (("dropped", True), (None, False)):
            d.state["worktrees"]["428"] = self._rec()
            cleanup, _ = self._run_cleanup(status=status, include_req=include)
            cleanup.assert_called_once_with(428, delete_branch=True)

    def test_skips_active_req(self):
        """doing/open 的卡有在途流程，不得动它的 worktree"""
        for status in ("doing", "open", "later"):
            d.state["worktrees"]["428"] = self._rec()
            cleanup, _ = self._run_cleanup(status=status)
            cleanup.assert_not_called()

    def test_keeps_scene_and_warns_when_unmerged(self):
        """人工置 done 但分支未合并：保留现场 + 一次告警，不删"""
        d.state["worktrees"]["428"] = self._rec()
        cleanup, event = self._run_cleanup(status="done", merged=False)
        cleanup.assert_not_called()
        self.assertIn("未合并入主干", d.state["stale_wt_blocked"]["428"])
        event.assert_called_once()
        self.assertIn("未自动回收", event.call_args[0][1])

    def test_keeps_scene_and_warns_when_dirty(self):
        wt = self.tmpdir / "wt"
        wt.mkdir()
        d.state["worktrees"]["428"] = self._rec(str(wt))
        cleanup, event = self._run_cleanup(status="done", clean=False)
        cleanup.assert_not_called()
        event.assert_called_once()

    def test_warn_deduped_for_same_cause(self):
        """同一阻塞原因只告警一次（定期巡检不刷屏）"""
        d.state["worktrees"]["428"] = self._rec()
        self._run_cleanup(status="done", merged=False)
        _, event2 = self._run_cleanup(status="done", merged=False)
        event2.assert_not_called()

    def test_warn_reset_after_recovery(self):
        """恢复可回收后清标记：先阻塞告警，再可回收，标记清空"""
        d.state["worktrees"]["428"] = self._rec()
        self._run_cleanup(status="done", merged=False)
        self.assertIn("428", d.state["stale_wt_blocked"])
        self._run_cleanup(status="done", merged=True)
        self.assertNotIn("428", d.state["stale_wt_blocked"])

    def test_empty_records_noop(self):
        cleanup, event = self._run_cleanup(status="done")
        cleanup.assert_not_called()
        event.assert_not_called()

    def test_board_unreachable_keeps_scene(self):
        """看板不可达：整体跳过，不猜状态删现场"""
        d.state["worktrees"]["428"] = self._rec()
        with mock.patch.object(d, "api", side_effect=RuntimeError("boom")), \
             mock.patch.object(d, "cleanup_dev_worktrees") as cleanup:
            d.cleanup_stale_worktrees()
        cleanup.assert_not_called()


class SweepIntervalTest(StaleWorktreeBase):
    """主循环巡检节流（_maybe_sweep_stale_worktrees）"""

    def tearDown(self):
        d._last_stale_wt_sweep = None
        super().tearDown()

    def test_sweep_throttled_by_interval(self):
        d._last_stale_wt_sweep = None
        with mock.patch.object(d, "cleanup_stale_worktrees") as sweep, \
             mock.patch.object(d.time, "monotonic", return_value=1000.0):
            d._maybe_sweep_stale_worktrees()          # 首轮：距上次 1000s > 间隔
            sweep.assert_called_once()
            d._maybe_sweep_stale_worktrees()          # 同一时刻再调：未到间隔
            sweep.assert_called_once()

    def test_sweep_runs_again_after_interval(self):
        d._last_stale_wt_sweep = None
        with mock.patch.object(d, "cleanup_stale_worktrees") as sweep, \
             mock.patch.object(d.time, "monotonic", return_value=1000.0):
            d._maybe_sweep_stale_worktrees()
        with mock.patch.object(d, "cleanup_stale_worktrees") as sweep, \
             mock.patch.object(d.time, "monotonic",
                               return_value=1000.0 + d.STALE_WORKTREE_INTERVAL + 1):
            d._maybe_sweep_stale_worktrees()
        sweep.assert_called_once()


class MainLoopWiringTest(unittest.TestCase):
    """主循环接线锁定：_main_loop_tick 每轮调用定期巡检（防重构时把接线删掉）"""

    def test_tick_calls_stale_sweep(self):
        with mock.patch.object(d, "_maybe_auto_restart"), \
             mock.patch.object(d, "_maybe_sweep_empty_sessions"), \
             mock.patch.object(d, "_maybe_sweep_stale_worktrees") as sweep, \
             mock.patch.object(d, "reconcile_decisions"), \
             mock.patch.object(d, "reconcile_confirmations"), \
             mock.patch.object(d, "_sweep_dead_running_once"), \
             mock.patch.object(d, "get_open_reqs", return_value=[]), \
             mock.patch.object(d, "write_heartbeat"):
            d._main_loop_tick()
        sweep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
