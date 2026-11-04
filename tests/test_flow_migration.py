#!/usr/bin/env python3
"""#419 存量卡迁移 / 状态收缩（tech.md §6.2 test_flow_migration）：
dispatcher 启动回填（route→open 清快照、review→按已通过推进、mode/phase 映射、旧 flow id 兜底、
幂等）与 board 状态五值 + 过渡窗口 route/review 照存 + 历史事件中文回放。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_flow_migration -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import board as b  # noqa: E402
import dispatcher as d  # noqa: E402

# 模块级函数替换统一恢复（防跨测试文件污染）
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "set_req", "fetch_req", "append_summary",
    "post_dispatcher_event", "api")}


class _MockRestoreMixin:
    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)


def legacy_req(rid, status="doing", mode="semi", phase=None, flow=None):
    """存量卡（无快照，只有历史 mode/phase/flow）"""
    return {"id": rid, "title": f"req{rid}", "project": "req-board", "status": status,
            "mode": mode, "phase": phase, "flow": flow, "notes": "存量", "summary": "",
            "nodes_json": None, "node_index": None}


class BackfillTest(_MockRestoreMixin, unittest.TestCase):
    """dispatcher 启动回填（§4.2，幂等）"""

    def setUp(self):
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {},
                   "held_procs": {}, "flow_review_rounds": {}, "merge_resolve": {},
                   "confirm_rounds": {}, "req_todos": {}}
        self.cur_req = None
        self.statuss = []
        self.set_reqs = []
        self.events = []
        self.rows_by_status = {}
        d.save_state = lambda: None
        d.append_summary = lambda rid, line, **kw: None
        d.post_dispatcher_event = lambda rid, content, kind="action", **kw: self.events.append((kind, content))
        d.set_status = lambda rid, st, **kw: self.statuss.append((str(rid), st))

        def _set_req(rid, fields, **kw):
            self.set_reqs.append((str(rid), fields))
            if self.cur_req is not None:
                self.cur_req.update(fields)
        d.set_req = _set_req
        d.fetch_req = lambda rid: self.cur_req
        d.api = lambda method, path, body=None: self.rows_by_status.get(path.split("status=")[-1], [])

    def _run(self, req, key="open,doing,route,review"):
        self.cur_req = req
        self.rows_by_status = {key: [req]}
        d.backfill_flow_snapshots()
        return req

    def test_backfill_semi_phase_accept(self):
        req = self._run(legacy_req(9, "doing", "semi", "accept", "standard"))
        self.assertEqual(req["flow"], "standard")
        self.assertEqual(req["node_index"], 2)               # standard: tech(0) dev(1) accept(2) terminal(3)
        self.assertEqual(req["flow_name"], "标准")
        self.assertEqual(d.role_for(req), "accept")

    def test_backfill_auto_card(self):
        """auto 流程已从配置删除：mode=auto 的无快照卡不再钉到流程节点。
        自动通道在 #419 后已无产生路径（pending_route 无写入方），此处锁住「不写快照」这一结果。"""
        req = self._run(legacy_req(9, "open", "auto", None))
        self.assertIsNone(req["flow"])
        self.assertIsNone(req["nodes_json"])

    def test_backfill_legacy_flow_id_fallback(self):
        req = self._run(legacy_req(9, "doing", "semi", "tech", "removed-flow"))
        self.assertEqual(req["flow"], d.default_flow_id())
        self.assertEqual(req["node_index"], 0)               # heavy 现为 tech(0) tech_review(1) dev(2) …
        self.assertTrue(any(k == "flow_fallback" for k, _ in self.events), self.events)

    def test_route_card_to_open(self):
        req = self._run(legacy_req(9, "route", None, None, None))   # route 卡真实形态：通道未定，mode/phase 均空
        self.assertIn(("9", {"status": "open", "flow": None, "flow_name": None,
                             "nodes_json": None, "node_index": None}), self.set_reqs)  # 重走前置环节
        self.assertIsNone(req["flow"])
        self.assertIsNone(req["nodes_json"])
        self.assertIsNone(req["node_index"])
        self.assertEqual(d.role_for(req), d.pre_role())

    def test_mode_null_card_stays_pre_stage(self):
        """mode 为空（未入流程）的 open/doing 卡：不写快照、保持前置阶段（评审必改1）"""
        req = self._run(legacy_req(9, "doing", mode=None, phase=None, flow=None))
        self.assertEqual(self.set_reqs, [])
        self.assertIsNone(req["nodes_json"])
        self.assertIsNone(req["node_index"])
        self.assertEqual(d.role_for(req), d.pre_role())

    def test_mode_null_review_card_back_to_open(self):
        """无流程信息的遗留 review 卡：转 open 重走前置环节（与 route 同）"""
        req = self._run(legacy_req(9, "review", mode=None, phase=None, flow=None))
        self.assertIn(("9", {"status": "open", "flow": None, "flow_name": None,
                             "nodes_json": None, "node_index": None}), self.set_reqs)
        self.assertIsNone(req["nodes_json"])

    def test_review_card_advances(self):
        req = self._run(legacy_req(9, "review", "semi", "tech", "heavy"))
        self.assertEqual(self.statuss, [("9", "open")])
        self.assertEqual(req["node_index"], 1)               # tech(0) → tech_review(1)：视为已通过
        self.assertEqual(d.role_for(req), "tech_review")

    def test_backfill_idempotent(self):
        first = self._run(legacy_req(9, "doing", "semi", "dev", "lean"))
        snapshot = (first["flow"], first["node_index"], first["nodes_json"])
        self.rows_by_status = {"doing": [first]}
        d.backfill_flow_snapshots()
        self.assertEqual((first["flow"], first["node_index"], first["nodes_json"]), snapshot)
        self.assertEqual(len(self.statuss), 0)               # 已有快照 → 不再写状态


class StatusShrinkTest(unittest.TestCase):
    """board 状态五值 + 过渡窗口 route/review 照存 + 历史事件中文回放（临时 DB 隔离）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-419-migration-"))
        cls._orig_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_statuses_five_values(self):
        self.assertEqual(b.STATUSES, ('later', 'open', 'doing', 'done', 'dropped'))
        self.assertEqual(b.LEGACY_STATUSES, ('route', 'review'))
        self.assertEqual(b.WRITABLE_STATUSES, b.STATUSES + b.LEGACY_STATUSES)

    def test_route_review_write_tolerated(self):
        """窗口 A：旧调度器写 route/review → 看板照存不转换（由调度器启动回填统一转换）"""
        r = self.client.post("/api/requirements", json={
            "title": "窗口A存量卡", "project": "req-board", "status": "open"})
        self.assertEqual(r.status_code, 201)
        rid = r.get_json()["id"]
        for st in ("route", "review"):
            resp = self.client.patch(f"/api/requirements/{rid}",
                                     json={"status": st, "mode": "semi", "phase": "tech"},
                                     headers={"X-Actor": "agent:test:419"})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            row = self.client.get("/api/requirements?project=req-board").get_json()
            card = [c for c in row if c["id"] == rid][0]
            self.assertEqual(card["status"], st)             # 照存，不转换成 open/doing

    def test_patch_snapshot_validation_mirrors_loader(self):
        """快照字段面元素级校验与加载器同规则：human/非 terminal 缺 role/terminal 非末位或带 role/
        缺 terminal → 400；合法序列 200"""
        r = self.client.post("/api/requirements", json={
            "title": "快照校验卡", "project": "req-board", "status": "open"})
        rid = r.get_json()["id"]
        base = {"flow": "standard", "flow_name": "标准", "node_index": 0}
        bad_cases = [
            [{"role": "tech", "node_type": "human"}, {"node_type": "terminal"}],        # human 未实现
            [{"node_type": "artifact"}, {"node_type": "terminal"}],                     # 非 terminal 缺 role
            [{"node_type": "terminal"}, {"role": "dev", "node_type": "code"}],          # terminal 非末位
            [{"role": "dev", "node_type": "code"}, {"role": "dev", "node_type": "terminal"}],  # terminal 带 role
            [{"role": "tech", "node_type": "artifact"}],                                # 缺 terminal
        ]
        for nodes in bad_cases:
            resp = self.client.patch(f"/api/requirements/{rid}",
                                     json={**base, "nodes_json": json.dumps(nodes)},
                                     headers={"X-Actor": "agent:test:419"})
            self.assertEqual(resp.status_code, 400, nodes)
        ok = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
              {"node_type": "terminal"}]
        resp = self.client.patch(f"/api/requirements/{rid}",
                                 json={**base, "nodes_json": json.dumps(ok)},
                                 headers={"X-Actor": "agent:test:419"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_status_log_replay_legacy_labels(self):
        """历史 route/review 事件中文回放（req_status_log 行不迁移）"""
        self.assertEqual(b._status_label_cn("route"), "通道待确认(route)")
        self.assertEqual(b._status_label_cn("review"), "评审中(review)")
        self.assertEqual(b._status_label_cn("doing"), "进行中(doing)")
        meta = self.client.get("/api/meta").get_json()
        self.assertEqual(meta["legacy_status_labels"], {"route": "通道待确认", "review": "评审中"})
        self.assertEqual(meta["status_labels"]["open"], "待开发")

    def test_meta_status_labels_five_values(self):
        meta = self.client.get("/api/meta").get_json()
        self.assertTrue(set(b.STATUSES) <= set(meta["status_labels"]))   # 5 值 + LEGACY 历史值

    def test_meta_flows_carry_description(self):
        """#419：/api/meta 的每条流程带 description（展示层复用同一份适用场景文案）
        另带 enabled（软禁用标记；展示名映射保留全部流程，历史卡 chip 不退化成原文 id）"""
        import acp_shared as ac

        meta = self.client.get("/api/meta").get_json()
        self.assertEqual([f["id"] for f in meta["flows"]], [f["id"] for f in ac.flows()])
        for got, f in zip(meta["flows"], ac.flows()):
            self.assertEqual(got["description"], f["description"], f["id"])
            self.assertTrue(got["description"].strip(), f["id"])     # 仓库真实配置多条流程 → 必填
            self.assertEqual(got["enabled"], f.get("enabled", True), f["id"])

    def test_meta_flows_carry_color(self):
        """#428：/api/meta 的 flows[] 带 color（枚举 = FLOW_COLORS；前端配色唯一注入通道）"""
        import acp_shared as ac

        meta = self.client.get("/api/meta").get_json()
        for got, f in zip(meta["flows"], ac.flows()):
            self.assertEqual(got["color"], f["color"], f["id"])      # D1 与配置同源
            self.assertIn(got["color"], ac.FLOW_COLORS, f["id"])
        self.assertEqual({f["id"]: f["color"] for f in meta["flows"]},   # D2 内置两流程档位色
                         {"quick": "green", "standard": "indigo", "heavy": "fuchsia"})


if __name__ == "__main__":
    unittest.main()
