#!/usr/bin/env python3
"""req-299 调度历史无痕修复：reconcile_startup 退回 open 补 phase 事件 + 修正日志措辞。

现场（#295）：dev 会话进行中调度器进程被外部信号终止，KeepAlive 拉起新调度器后
reconcile_startup 把 doing 卡退回 open（仅写日志），调度历史回放只有「新会话 → 续跑」
两条 spawn，退回这一段无痕；日志措辞「处于 doing 但无活跃运行」也不准确（重启后内存态
active_runs 必然为空，准确说法是运行记录丢失）。

修复（仅 dispatcher.py 的 reconcile_startup）：
- set_status 退回 open 后补写 phase 调度事件「调度器重启，进行中需求退回 open
  （1 分钟后自动重捞续跑）」，kind=phase（前端回放视图现成支持，board.py 零改动）；
- 事件写入位于 per-project try/except 内（看板不可达跳过不崩）；
- pending_confirm/pending_route/pending_review 保护的 doing 卡不退回、不写事件（现状不变）；
- 日志措辞改为「调度器重启导致运行记录丢失，退回 open 待重捞续跑」。

本测试为 L0 单测（mock 副作用，沿用 tests/test_dispatcher_events_session.py 风格），
覆盖验收三点：退回补 phase 事件 + 新日志措辞 / pending 保护不退回不写事件 /
看板不可达不崩不误写。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_reconcile_startup -v
    # 或与全量一起：
    .venv/bin/python -m unittest discover -s tests -v

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

ROLLBACK_EVENT = "调度器重启，进行中需求退回 open（1 分钟后自动重捞续跑）"


class L0ReconcileStartup(unittest.TestCase):
    """L0 单测：mock 副作用，校验退回补 phase 事件 / pending 保护 / 看板不可达降级"""

    # 本类会替换的 dispatcher 模块属性，tearDown 统一恢复（防污染同进程后续测试）
    _MUTATED = ("state", "PROJECTS", "get_doing_reqs", "get_open_reqs", "set_status",
                "post_dispatcher_event", "log")

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in self._MUTATED}
        d.state = {"pending_confirm": {}, "pending_route": {}, "pending_review": {}}
        d.PROJECTS = {"req-board": str(REPO)}
        d.log = mock.Mock()
        self.patched = []   # set_status 调用记录 (req_id, status)
        self.posted = []    # post_dispatcher_event 调用记录 (req_id, content, kwargs)
        d.set_status = lambda rid, st: self.patched.append((rid, st))
        d.post_dispatcher_event = lambda rid, content, **kw: self.posted.append((rid, content, kw))
        # open 扫描同样走看板 HTTP：不钉住就会真连 127.0.0.1:8899（本机恰好开着看板时才通过）
        d.get_open_reqs = lambda project: []

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)

    def _doing(self, rid, project="req-board"):
        return [{"id": rid, "project": project, "status": "doing", "phase": "dev"}]

    # ---- 验收 1：退回 open 补 phase 事件 + 日志新措辞 ----
    def test_rollback_posts_phase_event_after_set_status(self):
        """doing 卡被退回 open 后，补写 kind=phase 的调度事件；顺序为「先退回后写事件」"""
        d.get_doing_reqs = lambda project: self._doing(295)
        d.reconcile_startup()
        self.assertEqual(self.patched, [(295, "open")])
        self.assertEqual(len(self.posted), 1)
        rid, content, kw = self.posted[0]
        self.assertEqual(rid, 295)
        self.assertEqual(content, ROLLBACK_EVENT)
        self.assertEqual(kw, {"kind": "phase"})

    def test_rollback_log_uses_new_wording(self):
        """日志措辞改为「运行记录丢失」，不再出现误导性「无活跃运行」"""
        d.get_doing_reqs = lambda project: self._doing(295)
        d.reconcile_startup()
        d.log.warning.assert_called_once()
        msg = d.log.warning.call_args[0][0]
        self.assertIn("调度器重启导致运行记录丢失，退回 open 待重捞续跑", msg)
        self.assertNotIn("无活跃运行", msg)

    def test_multiple_doing_cards_each_rollback_and_event(self):
        """同项目多张 doing 卡：每张都退回并各写一条事件"""
        d.get_doing_reqs = lambda project: [{"id": 295, "status": "doing"},
                                            {"id": 296, "status": "doing"}]
        d.reconcile_startup()
        self.assertEqual(self.patched, [(295, "open"), (296, "open")])
        self.assertEqual([c[0] for c in self.posted], [295, 296])
        self.assertTrue(all(kw["kind"] == "phase" for _, _, kw in self.posted))

    # ---- 验收 2：pending 保护的 doing 卡不退回、不写事件 ----
    def test_pending_protected_doing_not_rolled_back(self):
        """pending_confirm/route/review 中的 doing 卡跳过（不退回、不写事件、无退回日志）"""
        for key in ("pending_confirm", "pending_route", "pending_review"):
            d.state[key] = {"295": True}
        d.get_doing_reqs = lambda project: self._doing(295)
        d.reconcile_startup()
        self.assertEqual(self.patched, [])
        self.assertEqual(self.posted, [])
        d.log.warning.assert_not_called()

    def test_pending_protection_only_skips_marked_card(self):
        """同一项目内：pending 保护的卡跳过，未保护的卡正常退回+写事件"""
        d.state["pending_confirm"] = {"295": True}
        d.get_doing_reqs = lambda project: [{"id": 295, "status": "doing"},
                                            {"id": 296, "status": "doing"}]
        d.reconcile_startup()
        self.assertEqual(self.patched, [(296, "open")])
        self.assertEqual([c[0] for c in self.posted], [296])

    # ---- 验收 3：看板不可达时不崩、不误写 ----
    def test_board_unreachable_skips_project_without_crash(self):
        """get_doing_reqs 抛异常（看板不可达）→ 该项目跳过，不写事件不崩；
        其余项目正常处理（per-project try/except 语义）"""
        def flaky_get(project):
            if project == "req-board":
                raise RuntimeError("board down")
            return self._doing(300, project)
        d.PROJECTS = {"req-board": str(REPO), "other": "/tmp/other"}
        d.get_doing_reqs = flaky_get
        d.reconcile_startup()  # 不抛异常即通过
        self.assertEqual(self.patched, [(300, "open")])
        self.assertEqual([c[0] for c in self.posted], [300])
        warn_msgs = [c[0][0] for c in d.log.warning.call_args_list]
        self.assertTrue(any("启动 reconcile 项目 req-board 失败（跳过）" in m for m in warn_msgs))

    def test_set_status_failure_skips_event_write(self):
        """退回动作本身失败 → 事件不写（事件写入位于 set_status 之后、同一 try 内），不误写不崩"""
        d.get_doing_reqs = lambda project: self._doing(295)
        def flaky_set(rid, st):
            raise RuntimeError("board down")
        d.set_status = flaky_set
        d.reconcile_startup()  # 不抛异常即通过
        self.assertEqual(self.posted, [])

    def test_event_post_failure_does_not_crash(self):
        """post_dispatcher_event 内部抛异常（看板瞬时不可达）→ 仅该项目跳过其余卡，不崩"""
        def flaky_post(rid, content, **kw):
            raise RuntimeError("board down")
        d.get_doing_reqs = lambda project: [{"id": 295, "status": "doing"},
                                            {"id": 296, "status": "doing"}]
        d.post_dispatcher_event = flaky_post
        d.reconcile_startup()  # 不抛异常即通过
        self.assertEqual(self.patched, [(295, "open")])  # 第一张已退回；第二张随异常跳过


if __name__ == "__main__":
    unittest.main()
