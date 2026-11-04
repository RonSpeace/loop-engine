#!/usr/bin/env python3
"""req-94 网页端审核通过/否决后飞书推送确认消息测试。

改动：reconcile_decisions() 识别到看板按钮改判的三个分支补发 notify()：
- route 确认（走自动/走半自动）→ 「已在网页端确认走X通道」
- review 通过（doing，非 accept）→ 「已在网页端审核通过，进入X阶段」
- review 否决（open）→ 「已在网页端否决，退回待开发」
accept 通过不加发（finalize_done → _set_done 已发「已完成」通知，避免一条决策两条消息）；
pending 在 notify 前已 pop，每张卡只推送一次。

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

# 测试会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "api", "fetch_req", "notify", "post_dispatcher_event",
    "_spawn_safe", "set_req", "append_summary", "finalize_done")}


def fresh_state():
    return {"chat_id": "oc_req94_test", "retries": {}, "pending_confirm": {},
            "pending_route": {}, "pending_review": {}, "sessions": {}, "worktrees": {}}


def fake_req(rid, status, mode=None, phase=None, flow=None):
    return {"id": rid, "title": f"req{rid}", "project": "req-board", "status": status,
            "mode": mode, "phase": phase, "flow": flow}


class L0WebReviewNotify(unittest.TestCase):
    """L0 单测：mock 副作用，校验三个推送分支的 notify 文案、accept 不加发、pending 一次性消费"""

    def setUp(self):
        d.state = fresh_state()
        self.notifies = []        # notify 记录
        self.events = []          # post_dispatcher_event 记录 [(kind, content), ...]
        self.spawns = []          # _spawn_safe 记录 (req_id, role)
        self.set_reqs = []        # set_req 记录 (rid, body)
        self.finalized = []       # finalize_done 记录
        self.reqs = []            # api 返回的需求列表
        d.save_state = lambda: None
        d.api = lambda method, path, body=None: self.reqs
        d.fetch_req = lambda rid: fake_req(rid, "doing", mode="semi", phase="dev")
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d._spawn_safe = lambda req, role: self.spawns.append((req["id"], role))
        d.set_req = lambda rid, body: self.set_reqs.append((rid, body))
        d.append_summary = lambda *a, **k: None
        d.finalize_done = lambda req, summary=None: self.finalized.append(req["id"])

    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)

    # ---- route 通道确认（走自动 / 走半自动）----
    def test_route_confirm_auto(self):
        """验收：通道确认按钮（走自动）→ 飞书确认「已在网页端确认走自动通道」

        历史遗留路径（#419 后 pending_route 无写入方）：auto 流程已从配置删除，
        mode=auto 的无快照卡与 flow 缺失同路 → 落兜底默认流程 heavy 的首节点。"""
        d.state["pending_route"]["90"] = {"req": None, "suggested_mode": "auto"}
        self.reqs = [fake_req(90, "open", mode="auto")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, ["需求 #90 已在网页端确认走自动通道。"])
        self.assertEqual(self.events, [("phase", "通道已确认（自动），进入「技术方案」")])
        self.assertEqual(self.spawns, [(90, "tech")])         # 兜底默认流程 heavy 首节点
        self.assertEqual(list(d.state["pending_route"]), [])  # pending 已消费

    def test_route_confirm_semi(self):
        """走半自动（缺省=默认流程 heavy）→ 「已在网页端确认走半自动通道（复杂流程）」，按 phase 路由"""
        d.state["pending_route"]["90"] = {"req": None, "suggested_mode": "semi"}
        self.reqs = [fake_req(90, "open", mode="semi", phase="tech")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, ["需求 #90 已在网页端确认走半自动通道（复杂流程）。"])
        self.assertEqual(self.events, [("phase", "通道已确认（半自动·复杂），进入「技术方案」")])
        self.assertEqual(self.spawns, [(90, "tech")])

    def test_route_confirm_semi_deleted_flow_name_fallback(self):
        """历史卡 flow 指向已删除的流程 id（lean）→ 通知里的流程名按兜底默认流程显示，不炸"""
        d.state["pending_route"]["91"] = {"req": None, "suggested_mode": "semi"}
        self.reqs = [fake_req(91, "open", mode="semi", flow="lean")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, ["需求 #91 已在网页端确认走半自动通道（复杂流程）。"])
        self.assertEqual(self.events, [("phase", "通道已确认（半自动·复杂），进入「技术方案」")])
        self.assertEqual(self.spawns, [(91, "tech")])

    def test_route_confirm_semi_heavy(self):
        """#376：走半自动·复杂（flow=heavy）→ 通知带流程名，按流程首阶段路由"""
        d.state["pending_route"]["92"] = {"req": None, "suggested_mode": "semi"}
        self.reqs = [fake_req(92, "open", mode="semi", flow="heavy")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, ["需求 #92 已在网页端确认走半自动通道（复杂流程）。"])
        self.assertEqual(self.events, [("phase", "通道已确认（半自动·复杂），进入「技术方案」")])
        self.assertEqual(self.spawns, [(92, "tech")])

    def test_route_still_route_no_notify(self):
        """仍为 route：不消费 pending、不发消息（等用户点按钮）"""
        d.state["pending_route"]["90"] = {"req": None, "suggested_mode": "auto"}
        self.reqs = [fake_req(90, "route")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, [])
        self.assertEqual(list(d.state["pending_route"]), ["90"])

    # ---- review 通过（网页点「通过」，非 accept）----
    def test_review_approve_to_next_phase(self):
        """验收：网页点「通过」→ 飞书确认「已在网页端审核通过，进入X阶段」，且只推一次

        历史遗留路径（#419 后 pending_review 无写入方）：卡 flow 缺失 → 兜底默认流程 heavy，
        phase=tech 的下一个节点是方案评审。"""
        d.state["pending_review"]["90"] = {"req": None, "phase": "tech"}
        self.reqs = [fake_req(90, "doing", mode="semi", phase="tech")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, ["需求 #90 已在网页端审核通过，进入 方案评审 阶段。"])
        self.assertEqual(self.set_reqs, [(90, {"status": "doing", "phase": "tech_review"})])
        self.assertEqual(self.spawns, [(90, "tech_review")])
        self.assertEqual(list(d.state["pending_review"]), [])  # pending 已消费，只推一次
        # 同轮重复扫描不会重发（pending 已 pop）
        d.reconcile_decisions()
        self.assertEqual(self.notifies, ["需求 #90 已在网页端审核通过，进入 方案评审 阶段。"])
        self.assertEqual(self.spawns, [(90, "tech_review")])

    def test_review_approve_accept_no_notify(self):
        """accept 通过：不加发「审核通过」（finalize_done 已发「已完成」），防一条决策两条消息"""
        d.state["pending_review"]["90"] = {"req": None, "phase": "accept"}
        self.reqs = [fake_req(90, "doing", mode="semi", phase="accept")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, [])
        self.assertEqual(self.finalized, [90])
        self.assertEqual(list(d.state["pending_review"]), [])

    def test_review_approve_phase_no_next_role(self):
        """phase 无下一角色（正常流程不会出现）：只推进不误发「进入X阶段」"""
        d.state["pending_review"]["90"] = {"req": None, "phase": "unknown"}
        self.reqs = [fake_req(90, "doing", mode="semi", phase="unknown")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, [])
        self.assertEqual(self.set_reqs, [])

    # ---- review 否决（网页点「否决」）----
    def test_review_reject_back_to_open(self):
        """验收：网页点「否决」→ 飞书确认「已在网页端否决，退回待开发」，且补 phase 事件"""
        d.state["pending_review"]["90"] = {"req": None, "phase": "tech"}
        self.reqs = [fake_req(90, "open", mode="semi", phase="tech")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, ["需求 #90 已在网页端否决，退回待开发。"])
        self.assertEqual(self.events, [("phase", "已在网页端否决，退回待开发")])
        self.assertEqual(list(d.state["pending_review"]), [])
        self.assertEqual(self.spawns, [])  # 否决不拉起 agent

    # ---- 其它状态：不消费 / 不推送 ----
    def test_still_review_no_action(self):
        """仍为 review：等用户决策，不消费 pending、不发消息"""
        d.state["pending_review"]["90"] = {"req": None, "phase": "tech"}
        self.reqs = [fake_req(90, "review")]
        d.reconcile_decisions()
        self.assertEqual(self.notifies, [])
        self.assertEqual(list(d.state["pending_review"]), ["90"])

    def test_req_missing_pops_silently(self):
        """看板无该需求：静默消费 pending（既有行为），不发消息"""
        d.state["pending_review"]["90"] = {"req": None, "phase": "tech"}
        d.state["pending_route"]["91"] = {"req": None, "suggested_mode": "auto"}
        self.reqs = []
        d.reconcile_decisions()
        self.assertEqual(self.notifies, [])
        self.assertEqual(list(d.state["pending_review"]), [])
        self.assertEqual(list(d.state["pending_route"]), [])

    # ---- 改判留痕（状态非通过/否决，如误触通过后改回暂缓）----
    def test_review_discard_later_keeps_trace(self):
        """验收：review pending 遇 later 改判 → pending 消费 + 调度事件留痕，不通知不完成"""
        d.state["pending_review"]["284"] = {"req": None, "phase": "accept"}
        self.reqs = [fake_req(284, "later", mode="semi", phase="accept")]
        d.reconcile_decisions()
        self.assertEqual(list(d.state["pending_review"]), [])
        self.assertEqual(self.events,
                         [("phase", "评审 pending 已清除：需求状态改为「later」，未执行通过/否决动作")])
        self.assertEqual(self.notifies, [])   # 不发飞书
        self.assertEqual(self.finalized, [])  # 不执行完成逻辑

    def test_route_discard_later_keeps_trace(self):
        """验收：route pending 遇 later 改判 → pending 消费 + 调度事件留痕，不拉起 agent"""
        d.state["pending_route"]["90"] = {"req": None, "suggested_mode": "auto"}
        self.reqs = [fake_req(90, "later")]
        d.reconcile_decisions()
        self.assertEqual(list(d.state["pending_route"]), [])
        self.assertEqual(self.events,
                         [("phase", "通道 pending 已清除：需求状态改为「later」，未走确认/否决")])
        self.assertEqual(self.notifies, [])
        self.assertEqual(self.spawns, [])


if __name__ == "__main__":
    unittest.main()
