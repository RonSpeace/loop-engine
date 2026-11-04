#!/usr/bin/env python3
"""#419 流程快照隔离（tech.md §6.2）：进行中卡按卡上快照推进，配置变更（删流程/改名/复用 id）
不影响进行中的卡；历史卡显示流程名快照。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_flow_snapshot_isolation -v
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


NODES_LEAN = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
              {"role": "dev", "node_type": "code"},
              {"role": "accept", "node_type": "artifact", "artifact": "accept.md"},
              {"node_type": "terminal"}]

# 同名 id 的「新配置」：与卡上快照不同的节点序列（模拟删除/改名/复用 id）
NODES_REUSED = [{"role": "dev", "node_type": "code"},
                {"role": "code_review", "node_type": "review", "artifact": "code_review.md"},
                {"node_type": "terminal"}]

NEW_CONFIG_LEAN = {"id": "lean", "name": "精简流程（改名后）", "nodes": NODES_REUSED}

# 模块级函数替换统一恢复（防跨测试文件污染）
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_req", "fetch_req", "_spawn_safe", "post_dispatcher_event", "api")}


class _MockRestoreMixin:
    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)


def snap_card(rid, status="doing", flow="lean", nodes=None, idx=1, **extra):
    r = {"id": rid, "title": f"req{rid}", "project": "req-board", "status": status,
         "mode": None, "phase": None, "flow": flow, "flow_name": "精简",
         "nodes_json": json.dumps(nodes if nodes is not None else NODES_LEAN, ensure_ascii=False),
         "node_index": idx, "notes": "", "summary": ""}
    r.update(extra)
    return r


class SnapshotIsolationTest(_MockRestoreMixin, unittest.TestCase):
    def setUp(self):
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {},
                   "held_procs": {}, "flow_review_rounds": {}, "merge_resolve": {},
                   "confirm_rounds": {}, "req_todos": {}}
        self.spawns = []
        self.set_reqs = []
        self.events = []
        self.cur_req = None
        d.save_state = lambda: None
        d._spawn_safe = lambda req, role, extra="", resume=False: self.spawns.append((str(req["id"]), role, resume))
        def _set_req(rid, fields, **kw):
            self.set_reqs.append((str(rid), fields))
            if self.cur_req is not None:
                self.cur_req.update(fields)
        d.set_req = _set_req
        d.fetch_req = lambda rid: self.cur_req
        d.post_dispatcher_event = lambda rid, content, kind="action", **kw: self.events.append((kind, content))
        # _advance_after_node → append_summary 会真的 PATCH 看板（测试内不该有网络副作用；
        # 早先未打桩时该调用会打到本机看板并 404，用例随环境红绿）
        d.api = lambda *a, **kw: None

    def test_deleted_flow_does_not_affect_inflight(self):
        """配置里没有该 flow id（已删）→ 卡仍按自身快照解析当前节点并推进"""
        req = snap_card(9, flow="deleted-flow", idx=0)
        self.cur_req = req
        self.assertEqual(d.role_for(req), "tech")
        self.assertFalse(d.flow_by_id("deleted-flow"))
        d._advance_after_node(req, d.current_node(req))
        self.assertEqual(req["node_index"], 1)          # 推进只写指针
        self.assertEqual(self.spawns, [("9", "dev", False)])
        self.assertFalse(any(k == "flow_fallback" for k, _ in self.events))

    def test_renamed_flow_keeps_card_flow_name(self):
        """配置里同名流程整条换掉（改名 + 节点序列不同）→ 卡显示名与推进仍按快照"""
        req = snap_card(9, idx=1)
        self.cur_req = req
        with mock.patch.object(d, "flow_by_id", lambda fid: dict(NEW_CONFIG_LEAN) if fid == "lean" else None), \
             mock.patch.object(d, "flows", lambda: [NEW_CONFIG_LEAN]):
            self.assertEqual(req["flow_name"], "精简")   # 进入流程时的名称快照，不随改名变化
            self.assertEqual(d.role_for(req), "dev")     # 快照第 1 节点 = dev（新配置第 1 节点是 review）
            self.assertEqual(d.current_node(req).get("node_type"), "code")
            d._advance_after_node(req, d.current_node(req))
            self.assertEqual(req["node_index"], 2)       # 按快照 4 节点序列推进，而非新配置 3 节点
            self.assertEqual(self.spawns, [("9", "accept", False)])

    def test_reused_flow_id_inflight_uses_snapshot(self):
        """flow id 被复用（指向完全不同的流程）→ 进行中卡不受影响，新行为只对无快照卡生效"""
        inflight = snap_card(9, idx=2)
        fresh = {"id": 10, "title": "new", "project": "req-board", "status": "open",
                 "mode": None, "phase": None, "flow": None, "flow_name": None,
                 "nodes_json": None, "node_index": None, "notes": "", "summary": ""}
        with mock.patch.object(d, "flow_by_id", lambda fid: dict(NEW_CONFIG_LEAN) if fid == "lean" else None), \
             mock.patch.object(d, "flows", lambda: [NEW_CONFIG_LEAN]):
            self.assertEqual(d.role_for(inflight), "accept")     # 快照第 2 节点
            self.assertEqual(d.role_for(fresh), d.pre_role())    # 无快照 → 前置槽位
            self.assertEqual(d.node_display(d.current_node(inflight)), "测试验证")

    def test_history_card_shows_flow_name_snapshot(self):
        """历史完成卡：flow_name 是展示唯一来源；流程改名不影响它"""
        done = snap_card(9, status="done", idx=3, flow_name="精简", nodes_json=None)
        with mock.patch.object(d, "flow_by_id", lambda fid: dict(NEW_CONFIG_LEAN) if fid == "lean" else None), \
             mock.patch.object(d, "flows", lambda: [NEW_CONFIG_LEAN]):
            self.assertEqual(done["flow_name"], "精简")
            self.assertIsNone(d.nodes_of(done))          # 历史卡不建快照，仅冻结展示名
            self.assertEqual(done["flow"], "lean")


if __name__ == "__main__":
    unittest.main()
