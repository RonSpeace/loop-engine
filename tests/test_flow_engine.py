#!/usr/bin/env python3
"""#419 流程引擎（配置驱动 + 卡上快照）单测——取代 test_semi_flows.py。

覆盖：配置派生流程声明（flows/节点序列/派生函数）、role_for/role_allowed 快照与懒映射、
输入规则（上游产物清单，无角色名映射/无 prd.md 探测）、前置槽位采纳（推荐流程/兜底留痕/
清单外仓库与 confirm 项目 need_confirm/快照写后回读）、节点推进（artifact/code/review）、
评审 pass/reject 打回指针回退与轮次上限、terminal 收尾三入口、存量卡回填、快照隔离。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_flow_engine -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 项目基线钉到最小清单（只含 1 个项目）：worktree 内这份与 main 的多项目清单语义不同——凡断言
# 「按项目扫描」次数/列表的地方必须自行把 d.PROJECTS 钉死
# （#419 现场：未钉 → worktree 绿、main 7 个项目红）
from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402

_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "set_req", "fetch_req", "append_summary",
    "_spawn_safe", "spawn_agent", "post_dispatcher_event", "notify_req_card",
    "api_board", "artifact_ok", "clear_retries", "clear_flow_rounds", "_branch_base",
    "has_code_changes", "notify", "finalize_done", "PROJECTS", "_register_need_confirm",
    "api", "get_open_reqs", "get_doing_reqs", "_adopted_rids")}


class _MockRestoreMixin:
    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)


def fresh_state():
    return {"chat_id": "oc_test", "retries": {}, "pending_confirm": {},
            "pending_route": {}, "pending_review": {}, "sessions": {}, "worktrees": {},
            "req_repos": {}, "held_procs": {}, "flow_review_rounds": {},
            "merge_resolve": {}, "confirm_rounds": {}, "req_todos": {}}


def fake_req(rid, status="open", mode="semi", phase=None, flow=None, snapshot=None):
    """snapshot=(nodes, index) 时写卡上快照（新语义）；否则按历史 mode/phase 走懒映射。"""
    r = {"id": rid, "title": f"req{rid}", "project": "req-board", "status": status,
         "mode": mode, "phase": phase, "flow": flow, "notes": "测试需求", "summary": "",
         "nodes_json": None, "node_index": None}
    if snapshot is not None:
        nodes, idx = snapshot
        r["nodes_json"] = json.dumps(nodes, ensure_ascii=False)
        r["node_index"] = idx
    return r


NODES_LEAN = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
              {"role": "dev", "node_type": "code"},
              {"role": "accept", "node_type": "artifact", "artifact": "accept.md"},
              {"node_type": "terminal"}]


class FlowsDeclTest(unittest.TestCase):
    """流程声明来自配置（flows.json），派生函数按节点序列计算"""

    def test_flows_from_config(self):
        ids = {f["id"] for f in d.flows()}
        self.assertEqual(ids, {"quick", "standard", "heavy"})
        std = d.flow_by_id("standard")
        self.assertEqual([n.get("role") for n in std["nodes"]], ["dev", "code_review", "accept", None])
        self.assertEqual(std["nodes"][-1]["node_type"], "terminal")
        self.assertEqual(d.default_flow_id(), "heavy")
        self.assertEqual(d.pre_role(), "analysis")
        self.assertIn("terminal", ac.NODE_TYPES)

    def test_disabled_flow_keeps_existing_cards(self):
        """软禁用只作用于「可选面」：定义仍在（flow_by_id 命中、存量卡照旧解析），
        只是不进推荐目录——若实现成过滤 flows()，存量卡会被悄悄改写成默认流程"""
        flows = [dict(f, enabled=False) if f["id"] == "heavy" else f for f in d.flows()]
        with mock.patch.dict(ac.FLOW_CONFIG, {"flows": flows}):
            self.assertTrue(d.flow_by_id("heavy"))
            self.assertFalse(d.flow_enabled("heavy"))
            self.assertNotIn("heavy", [f["id"] for f in d.enabled_flows()])
            req = fake_req(9, status="doing", mode="semi", phase="accept", flow="heavy")
            self.assertEqual(d.flow_of(req), "heavy")         # 不被改写成默认流程
            self.assertEqual(d.role_for(req), "accept")

    def test_flow_of_fallback(self):
        self.assertEqual(d.flow_of({}), "heavy")
        self.assertEqual(d.flow_of({"flow": "xxx"}), "heavy")
        self.assertEqual(d.flow_of({"flow": "lean"}), "heavy")   # 已删流程 id → 兜底默认流程

    def test_node_derived_helpers(self):
        std, heavy = (fake_req(1, flow=f) for f in ("standard", "heavy"))
        self.assertEqual(d.next_stage(std, "dev"), "code_review")
        self.assertEqual(d.next_stage(std, "code_review"), "accept")
        self.assertIsNone(d.next_stage(std, "accept"))
        self.assertEqual(d.next_stage(heavy, "tech"), "tech_review")
        self.assertEqual(d.next_stage(heavy, "code_review"), "accept")
        self.assertEqual(d.stage_of_review(std, "code_review"), "dev")
        self.assertEqual(d.stage_of_review(heavy, "code_review"), "dev")

    def test_role_for_pre_stage_and_legacy(self):
        # 未入流程 = 前置阶段 → pre.role（可配槽位）
        self.assertEqual(d.role_for(fake_req(1, mode=None, phase=None)), "analysis")
        # 历史卡按 mode/phase 懒映射到配置
        self.assertEqual(d.role_for(fake_req(1, mode="semi", phase=None, flow="standard")), "dev")
        self.assertEqual(d.role_for(fake_req(1, mode="semi", phase=None, flow=None)), "tech")
        # auto 流程已删、自动通道已无产生路径（pending_route 无写入方）：mode=auto 卡与
        # flow 缺失同路，落兜底默认流程 heavy 的首节点
        self.assertEqual(d.role_for(fake_req(1, mode="auto")), "tech")
        self.assertEqual(d.role_for(fake_req(1, mode="semi", phase="tech_review", flow="heavy")),
                         "tech_review")

    def test_role_for_snapshot_driven(self):
        self.assertEqual(d.role_for(fake_req(1, mode=None, snapshot=(NODES_LEAN, 0))), "tech")
        self.assertEqual(d.role_for(fake_req(1, mode=None, snapshot=(NODES_LEAN, 2))), "accept")
        # terminal 节点不拉起 agent
        self.assertIsNone(d.role_for(fake_req(1, mode=None, snapshot=(NODES_LEAN, 3))))
        self.assertEqual(d.node_index_of(fake_req(1, mode=None, snapshot=(NODES_LEAN, 2))), 2)

    def test_role_allowed_current_node_only(self):
        # standard 已无 tech 节点：历史 phase="tech" 映射不到 → 落到首节点（dev），"只放行当前节点"语义不变
        legacy = fake_req(1, mode="semi", phase="tech", flow="standard")
        self.assertTrue(d.role_allowed(legacy, "dev"))
        self.assertFalse(d.role_allowed(legacy, "tech"))
        snap = fake_req(2, mode=None, snapshot=(NODES_LEAN, 1))
        self.assertTrue(d.role_allowed(snap, "dev"))
        self.assertFalse(d.role_allowed(snap, "tech"))
        pre = fake_req(3, mode=None)
        self.assertTrue(d.role_allowed(pre, "analysis"))
        self.assertFalse(d.role_allowed(pre, "dev"))

    def test_role_allowed_merge_resolve_gate(self):
        d.state = fresh_state()
        req = fake_req(4, mode=None)
        self.assertFalse(d.role_allowed(req, "merge_resolve"))
        d.state["merge_resolve"]["4"] = {"failed": []}
        self.assertTrue(d.role_allowed(req, "merge_resolve"))

    def test_stage_input_rules(self):
        art = Path(tempfile.mkdtemp())
        with mock.patch.object(d, "artifact_dir", lambda r: art):
            pre = fake_req(5, mode=None)
            self.assertEqual(d.stage_input_for(pre), "需求本身（title + notes）")
            # 流程首节点：输入含前置槽位产物（lean 的 tech 仍见 analysis.md），无 prd.md 探测
            first = fake_req(5, mode=None, snapshot=(NODES_LEAN, 0))
            text = d.stage_input_for(first)
            self.assertIn(str(art / "analysis.md"), text)
            self.assertNotIn("prd.md", text)
            # 第 N 节点：列出全部已完成上游节点产物
            third = fake_req(5, mode=None, snapshot=(NODES_LEAN, 2))
            text3 = d.stage_input_for(third)
            self.assertIn(str(art / "analysis.md"), text3)
            self.assertIn(str(art / "tech.md"), text3)
            self.assertNotIn("self-test.md", text3)  # dev 节点无产物文件名 → 不入清单


class HandleResultFlowTest(_MockRestoreMixin, unittest.TestCase):
    """handle_result 按 node_type 分派推进（mock API/spawn）"""

    def setUp(self):
        d.state = fresh_state()
        self.set_reqs = []
        self.spawns = []
        self.spawn_extras = []
        self.events = []
        self.summaries = []
        self.cards = []
        self.todos_state = None
        self.finalized = []
        self.confirms = []
        d.save_state = lambda: None
        d.set_status = lambda rid, st, **kw: self.set_reqs.append((str(rid), {"status": st}))
        def _set_req(rid, fields, **kw):
            self.set_reqs.append((str(rid), fields))
            if self.cur_req is not None and self.mirror_writes:
                self.cur_req.update(fields)
        d.set_req = _set_req
        self.mirror_writes = True
        d.fetch_req = lambda rid: self.cur_req
        d.append_summary = lambda rid, line, **kw: self.summaries.append((str(rid), line))
        def _spawn_safe(req, role, extra="", resume=False):
            self.spawns.append((str(req["id"]), role, resume))
            self.spawn_extras.append((str(req["id"]), role, resume, extra))
        d._spawn_safe = _spawn_safe
        d.post_dispatcher_event = lambda rid, content, kind="action", **kw: self.events.append((kind, content))
        d.notify_req_card = lambda event, req, **kw: self.cards.append((event, req["id"]))
        d.api_board = lambda method, path, body=None: {"ok": True}
        d.artifact_ok = lambda req, role: (True, "")
        d.clear_retries = lambda rid: d.state.get("retries", {}).pop(str(rid), None)
        d.clear_flow_rounds = lambda rid: d.state.get("flow_review_rounds", {}).pop(str(rid), None)
        d._branch_base = lambda req, p: None
        d.has_code_changes = lambda req, since: True
        d.finalize_done = lambda req, summary=None: self.finalized.append(str(req["id"]))
        d._register_need_confirm = lambda rid, entry, role: (
            d.state["pending_confirm"].__setitem__(str(rid), entry) or 1)
        d.notify = lambda text: None

    def _run(self, req, role, status="done", verdict=None, result_extra=None):
        self.cur_req = req
        result = {"status": status, "summary": "ok"}
        if verdict:
            result["verdict"] = verdict
        if result_extra:
            result.update(result_extra)
        with mock.patch.object(d, "result_file_path", lambda r: _TMP_RESULT), \
             mock.patch.object(d, "artifact_dir", lambda r: _TMP_ART):
            _TMP_RESULT.write_text(json.dumps(result))
            d.handle_result(req, role, 0, False, 0)

    def test_pre_stage_adopts_suggested_flow(self):
        # 用例用 heavy（配置里启用的流程之一；lean 已软禁用，推荐它只会走兜底路径）
        req = fake_req(9, "doing", mode=None)
        self._run(req, "analysis", result_extra={"suggested_flow": "heavy", "repos": []})
        self.assertEqual(req["flow"], "heavy")
        self.assertEqual(req["flow_name"], d.flow_by_id("heavy")["name"])
        self.assertEqual(req["node_index"], 0)
        self.assertEqual(json.loads(req["nodes_json"])[0]["role"], "tech")
        self.assertEqual(self.spawns, [("9", "tech", False)])

    def test_suggested_flow_invalid_falls_back_default_with_event(self):
        """非法流程 id → 兜底默认流程 + flow_fallback 留痕（§6.2 用例 2）"""
        req = fake_req(9, "doing", mode=None)
        self._run(req, "analysis", result_extra={"suggested_flow": "no-such-flow"})
        self.assertEqual(req["flow"], "heavy")
        self.assertTrue(any(k == "flow_fallback" for k, _ in self.events), self.events)
        self.assertEqual(self.spawns, [("9", "tech", False)])

    def test_suggested_flow_valid_id_adopted(self):
        req = fake_req(9, "doing", mode=None)
        self._run(req, "analysis", result_extra={"suggested_flow": "standard"})
        self.assertEqual(req["flow"], "standard")
        self.assertEqual(self.spawns, [("9", "dev", False)])

    def test_suggested_flow_missing_falls_back_default_with_event(self):
        req = fake_req(9, "doing", mode=None)
        self._run(req, "analysis", result_extra={"suggested_mode": "auto"})  # 旧字段 → 非法
        self.assertEqual(req["flow"], "heavy")
        self.assertTrue(any(k == "flow_fallback" for k, _ in self.events), self.events)
        self.assertEqual(self.spawns, [("9", "tech", False)])

    def test_pre_stage_outside_repos_goes_need_confirm(self):
        req = fake_req(9, "doing", mode=None)
        self._run(req, "analysis", result_extra={"suggested_flow": "heavy",
                                                 "repos": ["other-project"]})
        self.assertEqual(self.spawns, [])
        self.assertEqual(req.get("flow"), None)
        pend = d.state["pending_confirm"]["9"]
        self.assertEqual(pend["flow_id"], "heavy")
        self.assertIn("清单外仓库", pend["question"])

    def test_pre_stage_confirm_project_goes_need_confirm(self):
        req = fake_req(9, "doing", mode=None)
        with mock.patch.dict(d.PROJECTS, {"req-board": dict(d.PROJECTS["req-board"], confirm=True)}):
            self._run(req, "analysis", result_extra={"suggested_flow": "heavy"})
        self.assertEqual(self.spawns, [])
        self.assertIn("人工确认", d.state["pending_confirm"]["9"]["question"])

    def test_snapshot_write_readback_fail_pauses(self):
        self.mirror_writes = False  # 看板静默忽略新字段：写后回读不一致
        req = fake_req(9, "doing", mode=None)
        self._run(req, "analysis", result_extra={"suggested_flow": "heavy"})
        self.assertEqual(self.spawns, [])
        self.assertTrue(any("快照写入未生效" in c for _, c in self.events), self.events)

    def test_artifact_node_advances_to_next_node(self):
        req = fake_req(9, "doing", mode=None, snapshot=(NODES_LEAN, 0))
        self._run(req, "tech")
        self.assertEqual(req["node_index"], 1)
        self.assertEqual(self.spawns, [("9", "dev", False)])

    def test_last_agent_node_inline_terminal(self):
        req = fake_req(9, "doing", mode=None, snapshot=(NODES_LEAN, 2))
        self._run(req, "accept")
        self.assertEqual(req["node_index"], 3)
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.finalized, ["9"])          # 内联执行 terminal 收尾
        self.assertTrue(any("收尾" in c for _, c in self.events))

    def test_dev_code_node_advances(self):
        req = fake_req(9, "doing", mode=None, snapshot=(NODES_LEAN, 1))
        self._run(req, "dev")
        self.assertEqual(req["node_index"], 2)
        self.assertEqual(self.spawns, [("9", "accept", False)])

    def test_review_pass_advances(self):
        nodes = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                 {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
                 {"role": "dev", "node_type": "code"},
                 {"node_type": "terminal"}]
        req = fake_req(9, "doing", mode=None, snapshot=(nodes, 1))
        self._run(req, "tech_review", verdict="pass", result_extra={"summary": "覆盖点齐全"})
        self.assertEqual(req["node_index"], 2)
        self.assertEqual(self.spawns, [("9", "dev", False)])
        self.assertEqual(d.state["flow_review_rounds"].get("9", {}), {})
        for _rid, line in self.summaries:               # 评审要点不进 summary（#342）
            self.assertNotIn("覆盖点齐全", line)

    def test_review_reject_bounces_pointer_then_resume(self):
        nodes = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                 {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
                 {"role": "dev", "node_type": "code"},
                 {"node_type": "terminal"}]
        req = fake_req(9, "doing", mode=None, snapshot=(nodes, 1))
        self._run(req, "tech_review", verdict="reject")
        # 指针先回退到前驱，再 resume 前驱（否则 #279 守卫按当前节点拦截）
        self.assertEqual(req["node_index"], 0)
        self.assertEqual(self.spawns, [("9", "tech", True)])
        self.assertEqual(d.state["flow_review_rounds"]["9"], {"1": 1})  # 轮次键 = 评审节点下标
        self.assertTrue(d.role_allowed(req, "tech"))
        self.assertFalse(d.role_allowed(req, "tech_review"))

    def test_review_reject_delivers_feedback_via_extra(self):
        """#419/#342：reject 打回时评审意见与评审产物全文经 extra 注入上游续跑会话
        （resume 路径 build_prompt 被丢弃，不注入则上游看不到审查发现）；评审结论不进 summary。"""
        nodes = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                 {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
                 {"node_type": "terminal"}]
        req = fake_req(9, "doing", mode=None, snapshot=(nodes, 1))
        (_TMP_ART / "tech_review.md").write_text("结论：打回\n- 缺 X 证据，结论不稳")
        try:
            self._run(req, "tech_review", verdict="reject",
                      result_extra={"summary": "缺 X 证据，结论不稳"})
            rid, role, resume, extra = self.spawn_extras[0]
            self.assertEqual((rid, role, resume), ("9", "tech", True))
            self.assertIn("缺 X 证据", extra)               # 评审意见（summary）送达
            self.assertIn("结论：打回", extra)              # 评审产物全文送达
            self.assertIn("tech_review.md", extra)
            self.assertIn("第 1 轮", extra)
            for _rid, line in self.summaries:               # 评审结论只进 req_reviews
                self.assertNotIn("缺 X 证据", line)
                self.assertNotIn("结论：打回", line)
            self.assertTrue(any("未通过，打回" in line for _, line in self.summaries))
        finally:
            (_TMP_ART / "tech_review.md").unlink(missing_ok=True)

    def test_review_verdict_recorded_to_req_reviews(self):
        """§6.2 #14：评审结论由调度器代写 req_reviews（append-only，verdict + 意见 + 产物路径）"""
        posts = []
        d.api_board = lambda method, path, body=None: (posts.append((method, path, body)), {"ok": True})[1]
        nodes = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                 {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
                 {"node_type": "terminal"}]
        req = fake_req(9, "doing", mode=None, snapshot=(nodes, 1))
        self._run(req, "tech_review", verdict="pass", result_extra={"summary": "覆盖点齐全"})
        self.assertEqual(posts[0][0], "POST")
        self.assertEqual(posts[0][1], "/requirements/9/reviews")
        self.assertEqual(posts[0][2]["verdict"], "pass")
        self.assertEqual(posts[0][2]["reviewer"], "agent:tech_review")
        self.assertIn("覆盖点齐全", posts[0][2]["content"])
        self.assertIn("tech_review.md", posts[0][2]["content"])

    def test_review_rounds_cap_forces_need_confirm(self):
        nodes = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                 {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
                 {"node_type": "terminal"}]
        req = fake_req(9, "doing", mode=None, snapshot=(nodes, 1))
        d.state["flow_review_rounds"] = {"9": {"1": d.FLOW_REVIEW_ROUNDS_MAX}}
        self._run(req, "tech_review", verdict="reject")
        self.assertEqual(self.spawns, [])
        pend = d.state["pending_confirm"]["9"]
        self.assertEqual(pend["review_index"], 1)
        self.assertIn("轮评审未通过", pend["question"])

    def test_review_at_cap_boundary_still_bounces(self):
        """上限为闭区间：第 FLOW_REVIEW_ROUNDS_MAX 次拒仍自动打回（cur <= MAX），第 MAX+1 次才转人工。
        与上一条成对，锁死比较符方向（改成 `cur < MAX` 即失败）。"""
        nodes = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                 {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
                 {"node_type": "terminal"}]
        req = fake_req(9, "doing", mode=None, snapshot=(nodes, 1))
        d.state["flow_review_rounds"] = {"9": {"1": d.FLOW_REVIEW_ROUNDS_MAX - 1}}
        self._run(req, "tech_review", verdict="reject")
        self.assertEqual(req["node_index"], 0)                       # 指针回退前驱
        self.assertEqual(self.spawns, [("9", "tech", True)])         # 仍自动打回，未转人工
        self.assertEqual(d.state["flow_review_rounds"]["9"], {"1": d.FLOW_REVIEW_ROUNDS_MAX})
        self.assertNotIn("9", d.state["pending_confirm"])

    def test_review_unknown_verdict_fail_closed(self):
        nodes = [{"role": "dev", "node_type": "code"},
                 {"role": "code_review", "node_type": "review", "artifact": "code_review.md"},
                 {"node_type": "terminal"}]
        req = fake_req(9, "doing", mode=None, snapshot=(nodes, 1))
        self._run(req, "code_review", verdict=None)
        self.assertEqual(req["node_index"], 0)          # 视为 reject → 打回前驱
        self.assertEqual(self.spawns, [("9", "dev", True)])


class TerminalTest(_MockRestoreMixin, unittest.TestCase):
    """terminal 收尾三入口（内联 / 启动扫描 / 主循环兜底）与幂等"""

    def setUp(self):
        d.state = fresh_state()
        # PROJECTS 钉成单项目：reconcile_startup/_main_loop 按 PROJECTS 逐项目扫描，而本类的
        # get_open_reqs 是**无视 project 参数**的 mock，不钉则 _run_terminal 调用次数 = 配置项目数
        # （#419 现场：main 7 个项目 → 同一张 terminal 卡被收尾 7 次；tearDown 的 mixin 会还原）
        d.PROJECTS = {"req-board": {"paths": [str(REPO)], "confirm": False}}
        self.finalized = []
        self.events = []
        self.statuss = []
        d.save_state = lambda: None
        d.finalize_done = lambda req, summary=None: self.finalized.append(str(req["id"]))
        d.post_dispatcher_event = lambda rid, content, kind="action", **kw: self.events.append((kind, content))
        d.set_status = lambda rid, st, **kw: self.statuss.append((str(rid), st))
        d.api = lambda method, path, body=None: self.rows_by_status.get(path.split("status=")[-1], [])
        d._adopted_rids = set()

    def test_run_terminal_skips_done_and_runs_otherwise(self):
        req = fake_req(9, "doing", mode=None, snapshot=(NODES_LEAN, 3))
        d.fetch_req = lambda rid: req
        d._run_terminal(req)
        self.assertEqual(self.finalized, ["9"])
        req["status"] = "done"
        d._run_terminal(req)
        self.assertEqual(self.finalized, ["9"])          # 幂等：已 done 跳过

    def test_reconcile_startup_runs_terminal_card(self):
        req = fake_req(9, "open", mode=None, snapshot=(NODES_LEAN, 3))
        self.rows_by_status = {"open,doing,route,review": [req]}
        d.get_open_reqs = lambda project: [req]
        d.get_doing_reqs = lambda project: []
        d.reconcile_startup()
        self.assertEqual(self.finalized, ["9"])
        self.assertEqual(self.statuss, [])               # terminal 卡不退 open

    def test_reconcile_startup_skips_merge_resolve_inflight(self):
        req = fake_req(9, "doing", mode=None, snapshot=(NODES_LEAN, 3))
        d.state["merge_resolve"]["9"] = {"failed": ["req-board"]}
        d.get_open_reqs = lambda project: []
        d.get_doing_reqs = lambda project: [req]
        d.reconcile_startup()
        self.assertEqual(self.finalized, [])
        self.assertEqual(self.statuss, [])               # 也不退 open


class BackfillTest(_MockRestoreMixin, unittest.TestCase):
    """存量卡快照回填（§4.2，幂等）"""

    def setUp(self):
        d.state = fresh_state()
        self.set_reqs = []
        self.statuss = []
        self.events = []
        self.rows_by_status = {}
        self.cur_req = None
        d.save_state = lambda: None
        d.post_dispatcher_event = lambda rid, content, kind="action", **kw: self.events.append((kind, content))
        d.set_status = lambda rid, st, **kw: self.statuss.append((str(rid), st))
        def _set_req(rid, fields, **kw):
            self.set_reqs.append((str(rid), fields))
            if self.cur_req is not None:
                self.cur_req.update(fields)
        d.set_req = _set_req
        d.fetch_req = lambda rid: self.cur_req
        d.api = lambda method, path, body=None: self.rows_by_status.get(path.split("status=")[-1], [])

    def test_backfill_semi_phase_maps_to_index(self):
        # phase 用非首节点（code_review 是 standard 第 2 个节点），才验得出「按 phase 定位」而非恒取 0
        req = fake_req(9, "doing", mode="semi", phase="code_review", flow="standard")
        self.cur_req = req
        self.rows_by_status = {"open,doing,route,review": [req]}
        d.backfill_flow_snapshots()
        self.assertEqual(req["flow"], "standard")
        self.assertEqual(req["node_index"], 1)
        self.assertEqual(req["flow_name"], "标准")

    def test_backfill_auto_card_without_auto_flow(self):
        """auto 流程已从配置删除：mode=auto 的无快照卡不再钉到流程节点（回填只冻结展示名）。
        自动通道在 #419 后已无产生路径（pending_route 无写入方），此处锁住「不写快照」这一结果。"""
        req = fake_req(9, "open", mode="auto", phase=None)
        self.cur_req = req
        self.rows_by_status = {"open,doing,route,review": [req]}
        d.backfill_flow_snapshots()
        self.assertIsNone(req.get("flow"))
        self.assertIsNone(req.get("nodes_json"))

    def test_backfill_route_card_back_to_open(self):
        req = fake_req(9, "route", mode="semi", phase="tech", flow="standard")
        self.cur_req = req
        self.rows_by_status = {"open,doing,route,review": [req]}
        d.backfill_flow_snapshots()
        self.assertIn(("9", {"status": "open", "flow": None, "flow_name": None,
                             "nodes_json": None, "node_index": None}), self.set_reqs)

    def test_backfill_review_card_advances_index(self):
        req = fake_req(9, "review", mode="semi", phase="accept", flow="standard")
        self.cur_req = req
        self.rows_by_status = {"open,doing,route,review": [req]}
        d.backfill_flow_snapshots()
        self.assertEqual(req["node_index"], 3)            # accept → terminal
        self.assertEqual(d.role_for(req), None)           # 由启动扫描/主循环兜底收尾
        self.assertIn(("9", "open"), self.statuss)

    def test_backfill_legacy_flow_id_falls_back_default(self):
        req = fake_req(9, "doing", mode="semi", phase="tech", flow="removed-flow")
        self.cur_req = req
        self.rows_by_status = {"open,doing,route,review": [req]}
        d.backfill_flow_snapshots()
        self.assertEqual(req["flow"], "heavy")
        self.assertTrue(any(k == "flow_fallback" for k, _ in self.events), self.events)

    def test_backfill_idempotent_and_freezes_flow_name(self):
        snap = fake_req(9, "doing", mode="semi", phase="dev", flow="standard",
                        snapshot=(NODES_LEAN, 1))
        self.cur_req = snap
        self.rows_by_status = {"doing": [snap]}
        d.backfill_flow_snapshots()
        self.assertEqual(self.set_reqs, [])               # 已有快照 → 不动
        done = fake_req(10, "done", mode="semi", phase=None, flow="standard")
        self.cur_req = done
        self.rows_by_status = {"open,doing,route,review": [done]}
        d.backfill_flow_snapshots()
        self.assertEqual(done.get("flow_name"), "标准")
        self.assertIsNone(done.get("nodes_json"))         # 历史完成卡不建快照


class SnapshotIsolationTest(_MockRestoreMixin, unittest.TestCase):
    """进行中卡按快照推进：流程删除/改名/复用 id 均不影响"""

    def setUp(self):
        d.state = fresh_state()
        d.save_state = lambda: None

    def test_inflight_uses_snapshot_after_config_change(self):
        # 卡上快照引用已删流程 id（配置里不存在）→ 仍按快照推进
        nodes = [{"role": "dev", "node_type": "code"},
                 {"role": "code_review", "node_type": "review", "artifact": "code_review.md"},
                 {"node_type": "terminal"}]
        req = {"id": 9, "title": "t", "project": "req-board", "status": "doing",
               "mode": None, "phase": None, "flow": "deleted-flow",
               "nodes_json": json.dumps(nodes), "node_index": 1, "notes": "", "summary": ""}
        self.assertEqual(d.role_for(req), "code_review")
        self.assertEqual(d.node_display(d.current_node(req)), d.flow_by_id("standard")["nodes"][1].get("name") or "代码审查")
        self.assertEqual(d.node_index_of(req), 1)

    def test_deleted_role_in_snapshot_degrades_to_raw_id(self):
        """角色被删除后，引用它的历史快照仍可推进（role_for/推进按快照），仅显示名退化为原文 id"""
        nodes = [{"role": "dev", "node_type": "code"},
                 {"role": "gone_role", "node_type": "review", "artifact": "x.md"},
                 {"node_type": "terminal"}]
        req = {"id": 9, "title": "t", "project": "req-board", "status": "doing",
               "mode": None, "phase": None, "flow": "deleted-flow",
               "nodes_json": json.dumps(nodes), "node_index": 1, "notes": "", "summary": ""}
        self.assertIsNone(d.role_meta("gone_role", "display"))
        self.assertEqual(d.role_for(req), "gone_role")
        self.assertEqual(d.node_display(d.current_node(req)), "gone_role")

    def test_flow_name_snapshot_is_display_only(self):
        req = fake_req(9, mode=None, snapshot=(NODES_LEAN, 0))
        req["flow_name"] = "旧流程名"
        self.assertEqual(req["flow_name"], "旧流程名")     # flow_name 仅展示，不参与调度
        self.assertEqual(d.role_for(req), "tech")


class PreRoleSwappableTest(_MockRestoreMixin, unittest.TestCase):
    """§6.2 #5：前置槽位角色可换——契约段按「卡处于前置阶段」判定（不按角色名），
    组合断言：pre.role=tech 且 lean 首节点同为 tech 时，前置阶段 prompt 含契约、流程内 tech 节点不含"""

    def setUp(self):
        d.state = fresh_state()

    def _prompt(self, req, role):
        wd = Path("/tmp/req419-pre-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=""):
            return d.build_prompt(req, role)

    def test_pre_role_tech_contract_injected_by_slot_only(self):
        with mock.patch.dict(ac.FLOW_CONFIG, {"pre": {"role": "tech"}}):
            self.assertEqual(d.pre_role(), "tech")
            pre = fake_req(9, mode=None, phase=None)
            pre_prompt = self._prompt(pre, "tech")
            self.assertIn("前置环节输出契约", pre_prompt)      # 换角色后契约仍注入
            self.assertIn('"suggested_flow"', pre_prompt)
            self.assertIn('"repos"', pre_prompt)
            inflow = fake_req(9, mode=None, snapshot=(NODES_LEAN, 0))   # lean 首节点同为 tech
            node_prompt = self._prompt(inflow, "tech")
            self.assertNotIn("前置环节输出契约", node_prompt)   # 流程内节点不重复注入
            self.assertNotIn('"suggested_flow"', node_prompt)


class DegradedConfigGuardTest(_MockRestoreMixin, unittest.TestCase):
    """#430 配置降级不静默收尾：收尾判据收窄到 terminal + 空角色 fail-closed 双重守卫。
    降级复现：mock.patch.object(ac, "FLOW_CONFIG", None)——dispatcher 的 pre_role/flow_by_id 为
    import 期绑定同一函数对象，函数体内读 acp_shared 模块全局，patch 生效。"""

    def setUp(self):
        d.state = fresh_state()
        # PROJECTS 钉成单项目：tick 按 PROJECTS 逐项目扫描（get_open_reqs 为无视 project 参数的 mock）
        d.PROJECTS = {"req-board": {"paths": [str(REPO)], "confirm": False}}
        d.save_state = lambda: None
        self.set_reqs = []
        self.events = []
        self.spawns = []
        self.terminated = []
        self.prompts = []
        d.set_req = lambda rid, fields, **kw: self.set_reqs.append((str(rid), fields))
        d.post_dispatcher_event = lambda rid, content, kind="action", **kw: self.events.append((kind, content))
        d._no_role_warned.clear()      # 模块级节流表：跨用例残留会让留痕断言随机失败

    def tearDown(self):
        d._no_role_warned.clear()
        super().tearDown()

    def _tick(self, cards, degraded=True):
        """跑一轮主循环：外部依赖全 mock（不建 worktree、不拉会话、不写盘、不触网）"""
        data = {"_maybe_auto_restart": lambda: None,
                "_maybe_sweep_stale_worktrees": lambda: None,
                "_maybe_sweep_empty_sessions": lambda: None,   # #431：不 mock 会真发 HTTP 到活看板
                "reconcile_decisions": lambda: None,
                "reconcile_confirmations": lambda: None,
                "_sweep_dead_running_once": lambda: None,
                "write_heartbeat": lambda *a, **kw: None,
                "get_open_reqs": lambda project: list(cards),
                "find_resume_sid": lambda req, role: None,
                "active_runs": {}}
        patches = [mock.patch.object(d, k, v) for k, v in data.items()]
        patches.append(mock.patch.object(
            d, "spawn_agent",
            lambda req, role=None, extra="", resume=False: self.spawns.append((str(req["id"]), role))))
        patches.append(mock.patch.object(
            d, "_run_terminal", lambda req: self.terminated.append(str(req["id"]))))
        if degraded:
            patches.append(mock.patch.object(ac, "FLOW_CONFIG", None))
        for p in patches:
            p.start()
        try:
            d._main_loop_tick()
        finally:
            for p in patches:
                p.stop()

    def test_degraded_pre_stage_card_parks(self):
        """① 降级 + 前置卡：不收尾、不拉起、有留痕、卡零写入（原缺陷：被当 terminal 收尾）"""
        req = fake_req(9, "open", mode=None)
        with mock.patch.object(ac, "FLOW_CONFIG", None):
            self.assertFalse(d.at_terminal_node(req))          # 判据不认「无角色」
        self._tick([req])
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.set_reqs, [])                    # 不置 done/doing
        self.assertEqual([k for k, _ in self.events], ["action"])
        self.assertIn("停泊", self.events[0][1])

    def test_degraded_snapshot_mid_card_still_spawns(self):
        """② 降级 + 快照中段卡：照常按快照拉起（快照解析不依赖配置），无停泊留痕"""
        req = fake_req(9, "open", mode=None, snapshot=(NODES_LEAN, 1))
        self._tick([req])
        self.assertEqual(self.spawns, [("9", "dev")])
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.events, [])

    def test_degraded_snapshot_terminal_card_finalizes(self):
        """③ 降级 + 快照 terminal 卡：仍执行收尾（防修过头造成 open+terminal 死锁）"""
        req = fake_req(9, "open", mode=None, snapshot=(NODES_LEAN, 3))
        with mock.patch.object(ac, "FLOW_CONFIG", None):
            self.assertTrue(d.at_terminal_node(req))
        self._tick([req])
        self.assertEqual(self.terminated, ["9"])
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.events, [])

    def test_normal_config_pre_stage_card_spawns_analysis(self):
        """④ 正常配置 + 前置卡：照常拉起 analysis（正向对照，零护栏误伤）"""
        req = fake_req(9, "open", mode=None)
        self._tick([req], degraded=False)
        self.assertEqual(self.spawns, [("9", "analysis")])
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.events, [])

    def test_no_role_warn_throttled_then_after_clear(self):
        """⑤ 停泊留痕节流：同需求最短 10 分钟一条；超间隔/表清空（等价重启）后恢复留痕"""
        req = fake_req(9, "open", mode=None)
        self._tick([req])
        self._tick([req])
        self.assertEqual(len(self.events), 1)                  # 同一秒连跑两 tick 只留一条
        d._no_role_warned["9"] = d.time.time() - d._NO_ROLE_WARN_INTERVAL - 1
        self._tick([req])
        self.assertEqual(len(self.events), 2)                  # 超最短间隔 → 再留一条
        d._no_role_warned.clear()
        self._tick([req])
        self.assertEqual(len(self.events), 3)

    def test_empty_role_fail_closed_guards(self):
        """⑥ 空角色不放行：role_allowed False；spawn_agent 留痕 return，
        先于 set_req(doing) 与 build_prompt（防卡停 doing 主循环捞不到而无法自愈）"""
        req = fake_req(9, "open", mode=None)
        with mock.patch.object(ac, "FLOW_CONFIG", None), \
                mock.patch.object(d, "build_prompt",
                                  lambda r, role, extra="": self.prompts.append(role)):
            self.assertFalse(d.role_allowed(req, ""))
            d.spawn_agent(req, role="")
        self.assertEqual([f for _r, f in self.set_reqs if f.get("status") == "doing"], [])
        self.assertEqual(self.prompts, [])                     # 未进 build_prompt
        self.assertEqual([k for k, _ in self.events], ["action"])
        self.assertIn("无角色可拉起", self.events[0][1])


_TMP = Path(tempfile.mkdtemp(prefix="req419-"))
_TMP_ART = _TMP / "artifacts"
_TMP_ART.mkdir(exist_ok=True)
_TMP_RESULT = _TMP_ART / "result.json"

if __name__ == "__main__":
    unittest.main()
