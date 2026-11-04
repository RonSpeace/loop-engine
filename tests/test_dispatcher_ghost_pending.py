#!/usr/bin/env python3
"""req-25 终态清账单点测试。

改动：需求进入终态（done/dropped）时，`_purge_ghost_confirm` 由「kind 白名单」扩为
「终态卡任意 kind」——同一处清理 pending_confirm、pending_merge_review、confirm_decisions
决策行与相关留痕（#24 现场：done 卡仍挂「待确认」徽标与裁决按钮）。
- `_set_done` 走 terminal=True（不依赖置 done 前的旧快照）；
- 启动对账 `reconcile_ghost_pending` 扫终态卡（覆盖人工置 done/dropped）；
- 非终态卡任意 kind（含 confirm/confirm_override）逐字保留（#394 红线按卡状态豁免）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_ghost_pending -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达（#343）

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

# 各测试类会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "_kickstart_board_if_own", "merge_conflict_clear",
    "confirm_decision_clear", "fetch_req", "api", "api_board")}


def _forbid_real_api(*a, **k):
    """sentinel（#343）：测试期间禁止任何真实看板外呼——未 mock 干净的路径直接 AssertionError"""
    raise AssertionError("测试命中未 mock 的看板外呼路径（api/api_board），禁止真实请求")


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fresh_state():
    return {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "pending_merge_review": {}, "sessions": {}, "worktrees": {}}


def fake_req(rid, status="doing"):
    return {"id": rid, "project": "req-board", "title": "T", "status": status, "mode": "auto"}


class GhostBase(unittest.TestCase):
    """公共 mock：_set_done 外呼路径（状态/通知/事件/表清除/看板重启）全部收进 calls；
    api/api_board 缺省钉 sentinel（#343）——未 mock 干净的真实外呼直接 AssertionError"""

    def setUp(self):
        d.state = fresh_state()
        self.calls = []
        d.save_state = lambda: None
        d.set_status = lambda rid, s: self.calls.append(("set_status", s))
        d.append_summary = lambda *a, **k: self.calls.append(("append_summary", a, k))
        d.post_dispatcher_event = lambda rid, content, kind="action": self.calls.append(
            ("event", (rid, content, kind)))
        d.notify = lambda *a, **k: self.calls.append(("notify", a, k))
        d.notify_req_card = lambda event, req, **k: self.calls.append(("card", (event, req["id"], k)))
        d._kickstart_board_if_own = lambda *a, **k: self.calls.append(("kickstart", a, k))
        d.merge_conflict_clear = lambda rid, reason="": self.calls.append(("merge_clear", (rid, reason)))
        d.confirm_decision_clear = lambda rid, reason="": self.calls.append(("decision_clear", (rid, reason)))
        d.fetch_req = lambda rid: None          # 缺省回读不到（终态路径须显式钉）
        d.api = _forbid_real_api
        d.api_board = _forbid_real_api

    def tearDown(self):
        _restore_d()

    def decision_cleared(self, rid):
        return any(c[0] == "decision_clear" and str(c[1][0]) == str(rid) for c in self.calls)

    def merge_cleared(self, rid):
        return any(c[0] == "merge_clear" and str(c[1][0]) == str(rid) for c in self.calls)


class SetDonePurgeTest(GhostBase):
    """终态单点清理：_set_done（terminal=True）清 pending 任意 kind + 决策行"""

    def test_done_purges_pending_and_decision(self):
        req = fake_req(25001)
        d.state["pending_confirm"]["25001"] = {"req": req, "question": "是否继续?", "kind": "confirm"}
        d.state["pending_merge_review"]["25001"] = {"req": req, "summary": "dev 摘要"}
        d._set_done(req)
        self.assertNotIn("25001", d.state["pending_confirm"])
        self.assertNotIn("25001", d.state["pending_merge_review"])
        self.assertTrue(self.decision_cleared("25001"))
        self.assertTrue(self.merge_cleared("25001"))
        self.assertTrue(any(c[0] == "event" and "幽灵待确认" in c[1][1] for c in self.calls))

    def test_all_kinds_purged_on_terminal(self):
        """终态卡任意 kind（普通 confirm/confirm_override/review_gate/merge）均清理"""
        for i, kind in enumerate(("confirm", "confirm_override", "review_gate", "merge")):
            rid = str(25010 + i)
            d.state["pending_confirm"][rid] = {"req": fake_req(int(rid)), "question": "q", "kind": kind}
        d._set_done(fake_req(25010))
        # 仅 25010 走 _set_done；其余三种 kind 走终态 helper 单点
        self.assertNotIn("25010", d.state["pending_confirm"])
        for i, kind in enumerate(("confirm_override", "review_gate", "merge"), start=1):
            rid = str(25010 + i)
            d.fetch_req = lambda r, rid=rid: fake_req(int(rid), status="done")
            d._purge_ghost_confirm(rid, "终态清理")
            self.assertNotIn(rid, d.state["pending_confirm"], kind)

    def test_set_done_force_terminal_does_not_depend_on_stale_snapshot(self):
        """terminal=True 直接采信：置 done 前的 req 快照仍是 doing 也照清（不被旧快照误判）"""
        req = fake_req(25002, status="doing")
        d.state["pending_confirm"]["25002"] = {"req": req, "question": "q", "kind": "confirm_override"}
        d._set_done(req)
        self.assertNotIn("25002", d.state["pending_confirm"])
        self.assertTrue(self.decision_cleared("25002"))

    def test_no_pending_no_side_effect(self):
        """无 pending：_set_done 不产生 merge 清表/幽灵事件（决策清仍幂等执行）"""
        d._set_done(fake_req(25005))
        self.assertEqual(d.state["pending_confirm"], {})
        self.assertFalse(any(c[0] == "merge_clear" for c in self.calls))
        self.assertFalse(any(c[0] == "event" and "幽灵待确认" in c[1][1] for c in self.calls))

    def test_purge_clears_decision_even_if_pending_already_popped(self):
        """pending 已被其它路径 pop、决策行动作残留时，终态单点仍清决策行"""
        d.fetch_req = lambda rid: fake_req(25003, status="done")
        d._purge_ghost_confirm("25003", "终态残留")
        self.assertTrue(self.decision_cleared("25003"))


class NonTerminalKeptTest(GhostBase):
    """#394 红线（按卡状态豁免）：非终态卡任意 kind 逐字保留，不调决策清"""

    def _keep(self, rid, kind, status="doing"):
        d.state["pending_confirm"][str(rid)] = {"req": fake_req(rid, status=status), "question": "q", "kind": kind}
        d.fetch_req = lambda r, rid=rid, status=status: fake_req(int(rid), status=status)
        d._purge_ghost_confirm(str(rid), "非终态")
        self.assertIn(str(rid), d.state["pending_confirm"])
        self.assertFalse(self.decision_cleared(rid))
        self.assertFalse(self.merge_cleared(rid))

    def test_non_terminal_confirm_kept(self):
        self._keep(25101, "confirm")

    def test_non_terminal_confirm_override_kept(self):
        self._keep(25102, "confirm_override")

    def test_non_terminal_merge_and_review_kept(self):
        self._keep(25103, "merge")
        self._keep(25104, "review_gate")

    def test_no_non_terminal_decision_delete(self):
        """doing/open/later 任意 kind 均不调用决策清理"""
        for i, (kind, status) in enumerate((("confirm", "open"), ("merge", "later"),
                                            ("review_gate", "doing"))):
            rid = 25110 + i
            d.state["pending_confirm"][str(rid)] = {"req": fake_req(rid, status), "question": "q", "kind": kind}
            d.fetch_req = lambda r, rid=rid, status=status: fake_req(int(rid), status=status)
            d._purge_ghost_confirm(str(rid), "非终态")
        self.assertFalse(any(c[0] == "decision_clear" for c in self.calls))


class PurgeReadSafetyTest(GhostBase):
    """终态判定回读安全：回读 None/异常/缺 status 一律 no-op，不误清 #394 保护范围"""

    def test_purge_fetch_none_is_safe(self):
        d.state["pending_confirm"]["25201"] = {"req": fake_req(25201), "question": "q", "kind": "confirm"}
        d.fetch_req = lambda rid: None
        self.assertFalse(d._purge_ghost_confirm("25201", "回读不到"))
        self.assertIn("25201", d.state["pending_confirm"])
        self.assertFalse(self.decision_cleared("25201"))

    def test_purge_fetch_exception_is_safe(self):
        d.state["pending_confirm"]["25202"] = {"req": fake_req(25202), "question": "q", "kind": "confirm"}
        d.fetch_req = lambda rid: (_ for _ in ()).throw(RuntimeError("board down"))
        self.assertFalse(d._purge_ghost_confirm("25202", "回读异常"))
        self.assertIn("25202", d.state["pending_confirm"])
        self.assertFalse(self.decision_cleared("25202"))

    def test_purge_missing_status_is_safe(self):
        d.state["pending_confirm"]["25203"] = {"req": fake_req(25203), "question": "q", "kind": "confirm"}
        d.fetch_req = lambda rid: {"id": rid}   # 缺 status
        self.assertFalse(d._purge_ghost_confirm("25203"))
        self.assertIn("25203", d.state["pending_confirm"])

    def test_purge_terminal_flag_skips_read(self):
        """terminal=True 不复用 fetch_req（避免置 done 前旧快照误判）"""
        called = []
        d.fetch_req = lambda rid: called.append(rid) or None
        d.state["pending_confirm"]["25204"] = {"req": fake_req(25204), "question": "q", "kind": "confirm"}
        self.assertTrue(d._purge_ghost_confirm("25204", "done 收尾", terminal=True))
        self.assertEqual(called, [])


class ReconcileGhostPendingTest(GhostBase):
    """启动存量终态对账：done/dropped 清理（任意 kind），非终态/缺卡/读不到不清"""

    def test_reconcile_ghost_pending_startup_only(self):
        d.state["pending_confirm"]["25301"] = {"req": fake_req(25301), "question": "q1", "kind": "confirm"}
        d.state["pending_confirm"]["25302"] = {"req": fake_req(25302), "question": "q2", "kind": "merge"}
        d.state["pending_confirm"]["25303"] = {"req": fake_req(25303), "question": "q3", "kind": "review_gate"}
        d.api = lambda method, path, body=None: [{"id": 25301, "status": "done"},
                                                 {"id": 25302, "status": "dropped"},
                                                 {"id": 25303, "status": "doing"}]
        d.fetch_req = lambda rid: fake_req(int(rid), status={"25301": "done", "25302": "dropped"}.get(str(rid), "doing"))
        d.reconcile_ghost_pending()
        self.assertNotIn("25301", d.state["pending_confirm"])
        self.assertNotIn("25302", d.state["pending_confirm"])
        self.assertIn("25303", d.state["pending_confirm"])   # doing 保留（#394）

    def test_reconcile_missing_card_kept(self):
        """卡不在全量列表 → 跳过（不当终态清理）"""
        d.state["pending_confirm"]["25310"] = {"req": fake_req(25310), "question": "q", "kind": "confirm"}
        d.api = lambda method, path, body=None: []
        d.reconcile_ghost_pending()
        self.assertIn("25310", d.state["pending_confirm"])
        self.assertFalse(self.decision_cleared("25310"))

    def test_reconcile_read_none_kept(self):
        """列表为 done 但回读 None → helper no-op 保留（读抖动不误清）"""
        d.state["pending_confirm"]["25311"] = {"req": fake_req(25311), "question": "q", "kind": "confirm"}
        d.api = lambda method, path, body=None: [{"id": 25311, "status": "done"}]
        d.fetch_req = lambda rid: None
        d.reconcile_ghost_pending()
        self.assertIn("25311", d.state["pending_confirm"])

    def test_reconcile_read_doing_kept(self):
        """列表为 done 但现取回读 doing（状态已回退）→ 保留"""
        d.state["pending_confirm"]["25312"] = {"req": fake_req(25312), "question": "q", "kind": "confirm"}
        d.api = lambda method, path, body=None: [{"id": 25312, "status": "done"}]
        d.fetch_req = lambda rid: fake_req(int(rid), status="doing")
        d.reconcile_ghost_pending()
        self.assertIn("25312", d.state["pending_confirm"])

    def test_reconcile_single_failure_continues(self):
        """单条回读异常不阻断其它条目清理"""
        d.state["pending_confirm"]["25320"] = {"req": fake_req(25320), "question": "q", "kind": "confirm"}
        d.state["pending_confirm"]["25321"] = {"req": fake_req(25321), "question": "q", "kind": "confirm"}
        d.api = lambda method, path, body=None: [{"id": 25320, "status": "done"},
                                                 {"id": 25321, "status": "done"}]

        def _fetch(rid):
            if int(rid) == 25320:
                raise RuntimeError("boom")
            return fake_req(int(rid), status="done")

        d.fetch_req = _fetch
        d.reconcile_ghost_pending()
        self.assertIn("25320", d.state["pending_confirm"])    # 异常条目保留（helper 内已捕获）
        self.assertNotIn("25321", d.state["pending_confirm"])  # 另一条照清

    def test_reconcile_api_unreachable_skip(self):
        d.state["pending_confirm"]["25330"] = {"req": fake_req(25330), "question": "q", "kind": "confirm"}
        d.api = lambda method, path, body=None: (_ for _ in ()).throw(RuntimeError("board down"))
        d.reconcile_ghost_pending()
        self.assertIn("25330", d.state["pending_confirm"])


if __name__ == "__main__":
    unittest.main()
