#!/usr/bin/env python3
"""#419 端到端集成（tech.md §6.2 test_flow_e2e）：open 免确认项目连续推进到 done（无人工中间态）、
主循环按快照取角色 / terminal 收尾、重启后新配置对新卡生效、改配置增删节点生效、过渡窗口兼容。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_flow_e2e -v
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
# 「按项目扫描」次数/列表的地方必须自行 mock.patch.object(d, "PROJECTS", ...) 钉死
# （#419 现场：未钉 → worktree 绿、main 7 个项目红）
from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="req419-e2e-"))
ART = TMP / "artifacts"
ART.mkdir(exist_ok=True)
RESULT = ART / "result.json"

# 本文件在 setUp 里整体替换 dispatcher 的模块级函数（闭包捕获 self），必须在 tearDown 还原，
# 否则泄漏给同进程后续测试（#419 现场：泄漏的 fetch_req 让后续收尾用例收到上一张卡 → ['11'] != ['9']）。
# 快照在 import 期取，与本文件用例执行顺序无关。
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "append_summary", "post_dispatcher_event", "notify",
    "notify_req_card", "api_board", "api", "artifact_ok", "has_code_changes",
    "_branch_base", "_gen_review_diff", "clear_retries", "clear_flow_rounds",
    "_register_need_confirm", "finalize_done", "set_status", "set_req", "fetch_req",
    "_spawn_safe", "get_open_reqs", "get_doing_reqs", "PROJECTS", "_adopted_rids")}


class _MockRestoreMixin:
    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)

NODES_LEAN = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
              {"role": "dev", "node_type": "code"},
              {"role": "accept", "node_type": "artifact", "artifact": "accept.md"},
              {"node_type": "terminal"}]
# 快照夹具用的节点序列（与生产配置无关，测试自行声明；卡上 flow=lean 表示「引用了已删流程的存量卡」）


def fresh_state():
    return {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {},
            "held_procs": {}, "flow_review_rounds": {}, "merge_resolve": {},
            "confirm_rounds": {}, "req_todos": {}}


class E2EBase(_MockRestoreMixin, unittest.TestCase):
    def setUp(self):
        d.state = fresh_state()
        self.statuses = []          # (req_id, status) 显式 set_status
        self.set_reqs = []          # (req_id, fields) 字段写入
        self.spawns = []            # (req_id, role, resume)
        self.events = []
        self.finalized = []
        self.depth = 0
        self.suggested_flow = "heavy"
        self.cur_req = None
        d.save_state = lambda: None
        d.append_summary = lambda rid, line, **kw: None
        d.post_dispatcher_event = lambda rid, content, kind="action", **kw: self.events.append((kind, content))
        d.notify = lambda text: None
        d.notify_req_card = lambda *a, **kw: None
        d.api_board = lambda method, path, body=None: {"ok": True}
        d.artifact_ok = lambda req, role: (True, "")
        d.has_code_changes = lambda req, since: True
        d._branch_base = lambda req, p: None
        d._gen_review_diff = lambda req: None
        d.clear_retries = lambda rid: d.state.get("retries", {}).pop(str(rid), None)
        d.clear_flow_rounds = lambda rid: d.state.get("flow_review_rounds", {}).pop(str(rid), None)
        d._register_need_confirm = lambda rid, entry, role: (
            d.state["pending_confirm"].__setitem__(str(rid), entry) or 1)
        d.finalize_done = lambda req, summary=None: self.finalized.append(str(req["id"]))
        d.set_status = lambda rid, st, **kw: self.statuses.append((str(rid), st))

        def _set_req(rid, fields, **kw):
            self.set_reqs.append((str(rid), fields))
            if self.cur_req is not None and str(rid) == str(self.cur_req["id"]):
                self.cur_req.update(fields)
        d.set_req = _set_req
        d.fetch_req = lambda rid: self.cur_req

    # ---- 驱动：模拟 agent 逐节点完成（每次 spawn 同回合回调 handle_result） ----
    def spawn_and_complete(self, req, role, extra="", resume=False):
        self.spawns.append((str(req["id"]), role, resume))
        self.depth += 1
        if self.depth > 12:
            raise AssertionError("推进链未收敛（疑似死循环）")
        node = d.current_node(req) or {}
        result = {"status": "done", "summary": f"{role} 完成"}
        if d.in_pre_stage(req):
            result.update({"suggested_flow": self.suggested_flow, "repos": []})
        if (node.get("node_type") or "") == "review":
            result["verdict"] = "pass"
        RESULT.write_text(json.dumps(result))
        with mock.patch.object(d, "result_file_path", lambda r: RESULT), \
             mock.patch.object(d, "artifact_dir", lambda r: ART):
            d.handle_result(req, role, 0, False, 0)

    def run_flow(self, req):
        """前置环节启动：写入 recommended flow 的 result.json 后交给推进链（后续节点由 spawn 回调驱动）"""
        self.cur_req = req
        d._spawn_safe = self.spawn_and_complete
        RESULT.write_text(json.dumps({"status": "done", "summary": "analysis 完成",
                                      "suggested_flow": self.suggested_flow, "repos": []}))
        with mock.patch.object(d, "result_file_path", lambda r: RESULT), \
             mock.patch.object(d, "artifact_dir", lambda r: ART):
            d.handle_result(req, d.pre_role(), 0, False, 0)

    def assert_no_human_state(self):
        self.assertEqual(d.state["pending_route"], {})
        self.assertEqual(d.state["pending_review"], {})
        self.assertEqual(d.state["pending_confirm"], {})
        self.assertFalse([s for _rid, s in self.statuses if s in ("route", "review")])


def new_card(rid, status="open"):
    return {"id": rid, "title": f"req{rid}", "project": "req-board", "status": status,
            "mode": None, "phase": None, "flow": None, "flow_name": None,
            "nodes_json": None, "node_index": None, "notes": "", "summary": ""}


class EndToEndTest(E2EBase):
    def test_end_to_end_open_to_done_no_human(self):
        """open → 前置（推荐 heavy）→ tech → 方案评审 → dev → 代码审查 → accept → terminal → done，
        全程无人工中间态"""
        req = new_card(9)
        self.run_flow(req)
        self.assertEqual([r for _rid, r, _res in self.spawns],
                         ["tech", "tech_review", "dev", "code_review", "accept"])
        self.assertEqual(self.finalized, ["9"])              # terminal 内联收尾
        self.assertEqual(req["flow"], "heavy")
        self.assertEqual(req["flow_name"], "复杂")
        self.assertEqual(req["node_index"], 5)               # terminal 下标
        self.assert_no_human_state()

    def test_end_to_end_simple_flow_uses_shared_code_review(self):
        """#423 合并：standard（常规改动）与 heavy（复杂）共用评审角色 code_review——
        open → 前置（推荐 standard）→ dev → code_review → accept → terminal → done，全程无人工中间态"""
        self.suggested_flow = "standard"
        req = new_card(10)
        self.run_flow(req)
        self.assertEqual([r for _rid, r, _res in self.spawns], ["dev", "code_review", "accept"])
        self.assertEqual(self.finalized, ["10"])
        self.assertEqual(req["flow"], "standard")
        self.assertEqual(req["flow_name"], "标准")
        self.assertEqual(req["node_index"], 3)               # terminal 下标
        nodes = json.loads(req["nodes_json"])
        self.assertEqual(nodes[1]["role"], "code_review")
        self.assertEqual(nodes[1]["artifact"], "code_review.md")   # 统一产物名
        self.assertEqual(nodes[1], next(n for n in d.flow_by_id("heavy")["nodes"]
                                        if n.get("role") == "code_review"),
                         "两条流程的代码审查节点应同构（同一角色、同一产物）")
        self.assert_no_human_state()

    def test_code_review_prompt_input_follows_flow(self):
        """合并后的关键设计点：同一份 code_review 提示词按卡上快照取上游产物清单（{stage_input}）——
        短流程（standard：无技术方案）只有需求分析产物，长流程（heavy）额外含技术方案与方案评审产物；
        不需要按流程分支"""
        wd = Path("/tmp/req423-wd")

        def _prompt(nodes, idx):
            req = new_card(12)
            req.update({"flow": "x", "nodes_json": json.dumps(nodes), "node_index": idx})
            with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                    mock.patch.object(d, "dev_repo_listing", return_value=""), \
                    mock.patch.object(d, "_gen_review_diff", lambda r: None):
                return d.build_prompt(req, "code_review")

        std_nodes = [{"role": "dev", "node_type": "code"},
                     {"role": "code_review", "node_type": "review", "artifact": "code_review.md"},
                     {"node_type": "terminal"}]
        std_prompt = _prompt(std_nodes, 1)
        self.assertIn("需求分析产物", std_prompt)
        self.assertNotIn("技术方案产物", std_prompt)
        self.assertIn("code_review.md", std_prompt)

        heavy_nodes = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                       {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
                       {"role": "dev", "node_type": "code"},
                       {"role": "code_review", "node_type": "review", "artifact": "code_review.md"},
                       {"node_type": "terminal"}]
        heavy_prompt = _prompt(heavy_nodes, 3)
        self.assertIn("需求分析产物", heavy_prompt)
        self.assertIn("技术方案产物", heavy_prompt)
        self.assertIn("方案评审产物", heavy_prompt)
        self.assertNotIn("{stage_input}", heavy_prompt)      # 占位符已渲染

    def test_main_loop_picks_by_snapshot(self):
        """主循环：有快照按当前节点角色拉起；terminal 节点转收尾入口不拉 agent"""
        node_card = new_card(11)
        node_card.update({"nodes_json": json.dumps(NODES_LEAN), "node_index": 1, "flow": "lean"})
        term_card = new_card(12)
        term_card.update({"nodes_json": json.dumps(NODES_LEAN), "node_index": 3, "flow": "lean"})
        self.cur_req = node_card
        spawned, terminated = [], []
        with mock.patch.object(d, "_maybe_auto_restart", lambda: None), \
             mock.patch.object(d, "_maybe_sweep_empty_sessions", lambda: None), \
             mock.patch.object(d, "reconcile_decisions", lambda: None), \
             mock.patch.object(d, "reconcile_confirmations", lambda: None), \
             mock.patch.object(d, "_sweep_dead_running_once", lambda: None), \
             mock.patch.object(d, "write_heartbeat", lambda *a, **kw: None), \
             mock.patch.object(d, "PROJECTS", {"req-board": {"paths": [str(REPO)], "confirm": False}}), \
             mock.patch.object(d, "get_open_reqs", lambda project: [node_card, term_card]), \
             mock.patch.object(d, "find_resume_sid", lambda req, role: None), \
             mock.patch.object(d, "spawn_agent",
                               lambda req, role=None, extra="", resume=False: spawned.append((str(req["id"]), role))), \
             mock.patch.object(d, "_run_terminal", lambda req: terminated.append(str(req["id"]))), \
             mock.patch.object(d, "active_runs", {}), \
             mock.patch.object(d, "MAX_PARALLEL", 2):
            d._main_loop_tick()
        self.assertEqual(spawned, [("11", "dev")])           # 按快照第 1 节点取角色
        self.assertEqual(terminated, ["12"])                 # terminal 兜底收尾

    def test_restart_picks_up_new_config(self):
        """重启后新配置：新卡按新流程（自定义流程 id）推进，进行中卡不受影响"""
        nodes_custom = [{"role": "proposal", "node_type": "artifact", "artifact": "prd.md"},
                        {"role": "dev", "node_type": "code"},
                        {"node_type": "terminal"}]
        custom = {"id": "fast", "name": "快速", "nodes": nodes_custom}
        inflight = new_card(21)
        inflight.update({"nodes_json": json.dumps(NODES_LEAN), "node_index": 1, "flow": "lean"})
        # 注入的自定义流程同时视为「配置里存在且启用」——推荐值命中启用检查才会被采纳
        with mock.patch.object(d, "flow_by_id",
                               lambda fid: custom if fid == "fast" else _real_flow_by_id(fid)), \
             mock.patch.object(d, "flow_enabled",
                               lambda fid: True if fid == "fast" else _real_flow_enabled(fid)), \
             mock.patch.object(d, "flows", lambda: list(_real_flows()) + [custom]):
            self.suggested_flow = "fast"
            self.run_flow(new_card(20))
            self.assertEqual(self.finalized, ["20"])
            self.assertEqual(d.role_for(inflight), "dev")    # 进行中卡按自身快照，不受新配置影响

    def test_config_node_add_remove_takes_effect(self):
        """同一份配置删掉 accept 节点后，新卡按新序列推进（验收「改配置即可增删节点」）"""
        nodes_no_accept = [n for n in NODES_LEAN if n.get("role") != "accept"]
        lean2 = {"id": "lean", "name": "精简", "enabled": True, "nodes": nodes_no_accept}
        # 注入的 lean2 显式视为启用：生产配置里 lean 已软禁用，推荐它只会走兜底路径；
        # 本用例测的是「改配置增删节点立即生效」，与生产是否启用该流程无关
        with mock.patch.object(d, "flow_by_id",
                               lambda fid: lean2 if fid == "lean" else _real_flow_by_id(fid)), \
             mock.patch.object(d, "flow_enabled",
                               lambda fid: True if fid == "lean" else _real_flow_enabled(fid)):
            self.suggested_flow = "lean"
            req = new_card(30)
            self.run_flow(req)
            self.assertEqual(json.loads(req["nodes_json"]), nodes_no_accept)
            self.assertEqual([r for _rid, r, _res in self.spawns], ["tech", "dev"])
            self.assertEqual(self.finalized, ["30"])

    def test_transition_window_legacy_writes(self):
        """窗口 A：旧调度器写 mode/phase（无快照）→ 卡可被回填并推进；不产生 route/review 中间态"""
        req = new_card(40)
        # 旧调度器写入，且 flow 指向已删除的流程 id → 回填须兜底默认流程并留痕
        req.update({"mode": "semi", "phase": "tech", "flow": "lean"})
        self.cur_req = req
        rows = {}
        d.api = lambda method, path, body=None: rows.get(path.split("status=")[-1], [])
        rows["open,doing,route,review"] = [req]
        d.backfill_flow_snapshots()
        self.assertEqual((req["flow"], req["node_index"]), ("heavy", 0))
        self.assertIsNotNone(req["nodes_json"])
        self.assertEqual(d.role_for(req), "tech")
        self.assert_no_human_state()


_REAL = {"flow_by_id": d.flow_by_id, "flow_enabled": d.flow_enabled, "flows": d.flows}
_real_flow_enabled = _REAL["flow_enabled"]


def _real_flow_by_id(fid):
    return _REAL["flow_by_id"](fid)


def _real_flows():
    return _REAL["flows"]()


if __name__ == "__main__":
    unittest.main()
