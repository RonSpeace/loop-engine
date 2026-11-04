#!/usr/bin/env python3
"""req-400 网页确认交互（确认横幅按钮 + 人工输入接力）L0 测试。

改动：dispatcher.py——
- need_confirm 落表：解析 result.json options（纯对话类 label/hint，未知键丢弃）+ role；
  review_gate/confirm_override 丢弃 options（按钮由调度器按 kind 生成）
- _register_need_confirm：confirm_rounds 单调计数（无任何清零/复位点），N > CONFIRM_ROUNDS_MAX
  仍落表 + entry["over_limit"]=True（不自动转 later，明示出口）
- reconcile_confirmations：GET /api/confirmations 全量 + 本地过滤 action 非空；以 pending_confirm
  为权威消费（与 on_message 同 state_lock 互斥）；kind 不匹配/无 pending = 幽灵决策清表留痕；
  语义与飞书 y/n 对等（on_message 零改动）

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
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

import dispatcher as d  # noqa: E402

_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "STATE_FILE", "_state_loaded", "api", "api_board", "fetch_req", "notify", "post_dispatcher_event",
    "set_status", "set_req", "append_summary", "finalize_done", "clear_retries",
    "confirm_decision_clear", "notify_req_card", "handle_result", "merge_dev_branches",
    "cleanup_dev_worktrees", "_set_done", "_touches_dispatcher", "artifact_dir")}


def fresh_state():
    return {"chat_id": "oc_req400_test", "retries": {}, "pending_confirm": {},
            "pending_route": {}, "pending_review": {}, "sessions": {}, "worktrees": {},
            "confirm_rounds": {}}


def fake_req(rid, status="doing", mode="auto", project="req-board", phase="dev"):
    return {"id": rid, "title": f"req{rid}", "project": project, "status": status,
            "mode": mode, "phase": phase}


class L0ConfirmWebDecision(unittest.TestCase):
    """落表/护栏/对账 L0：mock 副作用，校验 options 解析、单调计数、四类动作消费、幽灵清理"""

    def setUp(self):
        d.state = fresh_state()
        self.notifies = []
        self.events = []          # [(kind, content)]
        self.status = []          # set_status
        self.set_reqs = []        # set_req
        self.sums = []            # append_summary
        self.finalized = []       # finalize_done (req, summary)
        self.confirms = []        # api_board GET /confirmations 返回
        self.cleared = []         # confirm_decision_clear 记录
        self.cards = []           # notify_req_card 记录 (event, req, kw)
        self.reqs = []            # api GET 列表
        # #412：save_state 不再 mock（旧 setUp mock 掩盖 set_status 锁内嵌套→伪绿）；
        # patch STATE_FILE 到临时路径走真实 save_state（消除伪绿，回归锁真实生效）
        # #420：置 _state_loaded=True——落盘守卫只放行 load 过的进程，本类须断言真实落盘结果
        self._state_dir = Path(tempfile.mkdtemp(prefix="req400-state-"))
        d.STATE_FILE = self._state_dir / "dispatcher_state.json"
        d._state_loaded = True
        d.api = lambda method, path, body=None: self.reqs
        d.api_board = lambda method, path, body=None: self.confirms
        d.fetch_req = lambda rid: fake_req(int(rid))
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.set_status = lambda rid, status: self.status.append((rid, status))
        d.set_req = lambda rid, body: self.set_reqs.append((rid, body))
        d.append_summary = lambda rid, line: self.sums.append((rid, line))
        d.finalize_done = lambda req, summary=None: self.finalized.append((req["id"], summary))
        d.clear_retries = lambda rid: None
        d.confirm_decision_clear = lambda rid, reason="": self.cleared.append((rid, reason))
        d.notify_req_card = lambda event, req, **kw: self.cards.append((event, req["id"], kw))
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d._set_done = lambda req, **kw: None
        d._touches_dispatcher = lambda req, branch: False

    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)
        shutil.rmtree(self._state_dir, ignore_errors=True)

    # ---- A. options 解析 ----
    def test_parse_options_valid(self):
        self.assertEqual(d._parse_confirm_options({"options": [
            {"label": "继续修", "hint": "在当前方案上继续修复"},
            {"label": "换方案", "hint": "重新做技术方案"}]}),
            [{"label": "继续修", "hint": "在当前方案上继续修复"}, {"label": "换方案", "hint": "重新做技术方案"}])

    def test_parse_options_drops_action_and_limits(self):
        """fail-closed：action/类型键丢弃；超长 label 截断；超 5 项截断；非法项丢弃"""
        parsed = d._parse_confirm_options({"options": [
            {"label": "继续修", "action": "pass"},          # action 丢弃（仅剩 label）
            {"label": "x" * 50},                             # 截断到 20
            "not-a-dict",                                    # 非法项丢弃
            {"hint": "无 label 丢弃"},
            {"label": "a"}, {"label": "b"}, {"label": "c"}, {"label": "d"}, {"label": "e"},
            {"label": "超限第 6 项丢弃"},
        ]})
        self.assertEqual(len(parsed), 5)
        self.assertNotIn("action", parsed[0])
        self.assertEqual(parsed[1]["label"], "x" * 20)
        self.assertNotIn("hint", parsed[1])

    def test_parse_options_invalid_returns_none(self):
        self.assertIsNone(d._parse_confirm_options({"options": "oops"}))
        self.assertIsNone(d._parse_confirm_options({}))
        self.assertIsNone(d._parse_confirm_options({"options": [{"action": "pass"}]}))

    # ---- A. 落表 + 护栏 ----
    def _handle_need_confirm(self, result, role="dev", rid=90):
        """写真实 result.json（mock artifact_dir 到临时目录）→ 走 handle_result need_confirm 分支"""
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="req400-hr-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)  # 防 $TMPDIR 泄漏
        (tmp / "result.json").write_text(json.dumps(result))
        d.artifact_dir = lambda req: tmp
        d.state["sessions"] = {}
        d.reqs = [fake_req(rid, status="doing")]
        d.handle_result(fake_req(rid, status="doing"), role, 0, False, 0.0)

    def test_register_normal_and_options(self):
        """普通 need_confirm + 合法 options → pending 带 options/role；confirm_rounds=1；无 over_limit"""
        self._handle_need_confirm({"status": "need_confirm", "question": "Q?",
                                   "options": [{"label": "继续修"}]}, role="dev")
        pend = d.state["pending_confirm"]["90"]
        self.assertEqual(pend.get("kind") in (None, "confirm"), True)  # 缺省 kind=confirm（GET 端点归一）
        self.assertEqual(pend["options"], [{"label": "继续修"}])
        self.assertEqual(pend["role"], "dev")
        self.assertEqual(d.state["confirm_rounds"]["90"], 1)
        self.assertFalse(pend.get("over_limit"))
        self.assertEqual(self.cards[0][0], "need_confirm")
        self.assertEqual(self.cards[0][2].get("round_n"), 1)

    def test_register_over_limit_marks_entry(self):
        """第 6 轮起：仍落 pending（不自动转 later、不毒会话），entry over_limit=True，文案升级"""
        for i in range(1, 7):
            self._handle_need_confirm({"status": "need_confirm", "question": f"Q{i}?"}, role="dev")
            pend = d.state["pending_confirm"]["90"]
            self.assertEqual(d.state["confirm_rounds"]["90"], i)
            self.assertFalse(pend.get("over_limit")) if i <= 5 else self.assertTrue(pend.get("over_limit"))
        # 第 6 轮卡片带超限出口文案
        self.assertEqual(self.cards[-1][2].get("round_n"), 6)
        self.assertTrue(self.cards[-1][2].get("over_limit"))

    def test_register_review_need_confirm_uses_plain_confirm(self):
        """#419：review 角色 need_confirm 不再转 review_gate（发现问题走 verdict=reject 自动打回，
        人工通道只剩普通 need_confirm）→ 落普通 confirm、options 按对话类解析"""
        self._handle_need_confirm({"status": "need_confirm", "question": "审查?",
                                   "options": [{"label": "仍合并"}]}, role="review")
        pend = d.state["pending_confirm"]["90"]
        self.assertIsNone(pend.get("kind"))
        self.assertEqual(pend["options"], [{"label": "仍合并"}])

    def test_register_confirm_override_drops_options(self):
        """waive_gate 申报 → kind=confirm_override、options 置空、role 落表、计数递增"""
        d.state["sessions"] = {}
        d.reqs = [fake_req(90, status="doing")]
        # 直接构造豁免申报：done + 收尾校验失败难以纯 mock，走 _register_need_confirm 单元级验证
        rounds = d._register_need_confirm("90", {"req": fake_req(90), "question": "Q?",
                                                 "kind": "confirm_override", "options": None}, "dev")
        self.assertEqual(rounds, 1)
        self.assertEqual(d.state["pending_confirm"]["90"]["role"], "dev")

    # ---- B. reconcile_confirmations 消费 ----
    def _reconcile(self):
        d.reconcile_confirmations()

    def test_reconcile_review_gate_pass(self):
        """review_gate + pass → 等效飞书 y：pop pending_merge_review、红字留档、finalize_done、清表"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "审查发现问题", "kind": "review_gate"}
        d.state["pending_merge_review"] = {"90": {"summary": "dev 摘要"}}
        self.confirms = [{"req_id": 90, "kind": "review_gate", "action": "pass"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.finalized, [(90, "dev 摘要")])
        self.assertTrue(any("人工确认仍合并" in s for _, s in self.sums))
        self.assertIn(("90", "reconcile 消费"), self.cleared)
        self.assertEqual(d.state["confirm_rounds"].get("90"), None)  # 单调计数无清零点

    def test_reconcile_review_gate_reject(self):
        """review_gate + reject → 等效飞书 n：pop、set_status(later)、summary、分支保留"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "审查发现问题", "kind": "review_gate"}
        self.confirms = [{"req_id": 90, "kind": "review_gate", "action": "reject"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.status, [(90, "later")])
        self.assertTrue(any("否决合并" in s for _, s in self.sums))

    def test_reconcile_review_gate_rework(self):
        """#411：review_gate + rework → 等效飞书 n 转开发：pop 标记、保持 doing（无状态变更）、
        resume 原 dev 会话且 extra 含 merge_review.md 审查反馈、summary+事件留痕、清表"""
        import tempfile
        from unittest import mock as _mock
        d.state["pending_confirm"]["90"] = {"req": None, "question": "审查发现问题", "kind": "review_gate"}
        d.state["pending_merge_review"] = {"90": {"req": None, "summary": "dev 摘要"}}
        self.confirms = [{"req_id": 90, "kind": "review_gate", "action": "rework"}]
        self.reqs = [fake_req(90)]
        tmp = Path(tempfile.mkdtemp(prefix="req400-rework-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)  # 防 $TMPDIR 泄漏
        (tmp / "merge_review.md").write_text("审查发现夹带改动 a.py 需拆除")
        d.artifact_dir = lambda req: tmp
        spawned = []
        with _mock.patch.object(d, "_spawn_safe",
                                lambda req, role, **k: spawned.append((role, k))):
            self._reconcile()
        self.assertEqual(spawned[0][0], "dev")
        self.assertTrue(spawned[0][1]["resume"])
        self.assertIn("夹带改动 a.py", spawned[0][1]["extra"])
        self.assertEqual(self.status, [])  # 无 set_status（保持 doing）
        self.assertTrue(any("转开发" in s for _, s in self.sums))
        self.assertIn(("90", "reconcile 消费"), self.cleared)

    def test_reconcile_review_gate_rework_with_reason(self):
        """#415：review_gate + rework + reason → 用户意见原样注入 _review_gate_rework 原因；
        未填 reason 时保持默认「（网页端转开发）」"""
        import tempfile
        from unittest import mock as _mock
        d.state["pending_confirm"]["90"] = {"req": None, "question": "审查发现问题", "kind": "review_gate"}
        d.state["pending_merge_review"] = {"90": {"req": None, "summary": "dev 摘要"}}
        tmp = Path(tempfile.mkdtemp(prefix="req400-rework-reason-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)  # 防 $TMPDIR 泄漏
        (tmp / "merge_review.md").write_text("审查发现夹带改动 a.py 需拆除")
        d.artifact_dir = lambda req: tmp

        # 1) 网页弹窗填写了意见 → 原样注入，不再出现默认文案
        self.confirms = [{"req_id": 90, "kind": "review_gate", "action": "rework",
                          "reason": "请补迁人脸识别→渠道风控规则并补充单测"}]
        self.reqs = [fake_req(90)]
        spawned = []
        with _mock.patch.object(d, "_spawn_safe",
                                lambda req, role, **k: spawned.append((role, k))):
            self._reconcile()
        self.assertIn("请补迁人脸识别→渠道风控规则并补充单测", spawned[0][1]["extra"])
        self.assertNotIn("（网页端转开发）", spawned[0][1]["extra"])
        self.assertIn(("90", "reconcile 消费"), self.cleared)

        # 2) 未填写意见（reason 缺省）→ 保持默认「（网页端转开发）」
        d.state["pending_confirm"]["90"] = {"req": None, "question": "审查发现问题", "kind": "review_gate"}
        d.state["pending_merge_review"] = {"90": {"req": None, "summary": "dev 摘要"}}
        self.confirms = [{"req_id": 90, "kind": "review_gate", "action": "rework", "reason": None}]
        self.reqs = [fake_req(90)]
        spawned2 = []
        with _mock.patch.object(d, "_spawn_safe",
                                lambda req, role, **k: spawned2.append((role, k))):
            self._reconcile()
        self.assertIn("（网页端转开发）", spawned2[0][1]["extra"])

    def test_reconcile_override_pass(self):
        """confirm_override + pass → 等效飞书 y：红字留档、finalize_done(带 summary)"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "校验未过", "kind": "confirm_override",
                                            "summary": "豁免摘要"}
        self.confirms = [{"req_id": 90, "kind": "confirm_override", "action": "pass"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.finalized, [(90, "豁免摘要")])
        self.assertTrue(any("人工确认豁免" in s for _, s in self.sums))

    def test_reconcile_override_reject_and_later(self):
        """confirm_override + reject → 退 open(phase=None)+清 retries；reject_later → 置 later"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "校验未过", "kind": "confirm_override"}
        self.confirms = [{"req_id": 90, "kind": "confirm_override", "action": "reject"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.set_reqs, [(90, {"status": "open", "phase": None})])
        d.state["pending_confirm"]["90"] = {"req": None, "question": "校验未过", "kind": "confirm_override"}
        self.confirms = [{"req_id": 90, "kind": "confirm_override", "action": "reject_later"}]
        self._reconcile()
        self.assertEqual(self.status, [(90, "later")])
        self.assertTrue(any("later" in s for _, s in self.sums))

    def test_reconcile_stop_later(self):
        """confirm + stop_later → 网页新增明示出口语义：set_status(later)+summary 留痕+清表
        （非飞书 n 对等复刻——飞书 n 普通确认=退 open，n+later 前缀仅 confirm_override 存在）"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "继续?",
                                            "kind": "confirm", "over_limit": True}
        self.confirms = [{"req_id": 90, "kind": "confirm", "action": "stop_later"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.status, [(90, "later")])
        self.assertTrue(any("停止追问" in s for _, s in self.sums))
        self.assertIn(("90", "reconcile 消费"), self.cleared)

    def test_reconcile_plain_confirm_proceed(self):
        """confirm + proceed → 等同飞书 y，留档依据并续跑当前角色"""
        d.state["pending_confirm"]["90"] = {
            "req": None,
            "question": "允许共享包吗？",
            "kind": "confirm",
        }
        self.confirms = [{
            "req_id": 90,
            "kind": "confirm",
            "action": "proceed",
            "reason": "packages/auth 与 tenant-db 已合入",
        }]
        self.reqs = [fake_req(90)]
        spawned = []
        d.spawn_agent = lambda req, **kwargs: spawned.append((req["id"], kwargs))
        self._reconcile()
        self.assertEqual(spawned[0][0], 90)
        self.assertTrue(spawned[0][1]["resume"])
        self.assertIn("packages/auth 与 tenant-db 已合入", spawned[0][1]["extra"])
        self.assertTrue(any("packages/auth" in line for _, line in self.sums))
        self.assertIn(("90", "reconcile 消费"), self.cleared)

    def test_reconcile_review_cap_rework(self):
        """confirm + review_index + rework → 回退前驱节点并 resume，原因进入重做提示词"""
        from unittest import mock as _mock
        d.state["pending_confirm"]["90"] = {
            "req": None,
            "question": "方案评审连续 4 轮未通过",
            "kind": "confirm",
            "review_index": 1,
            "role": "tech_review",
        }
        d.state["flow_review_rounds"] = {"90": {"1": 4}}
        self.confirms = [{
            "req_id": 90,
            "kind": "confirm",
            "action": "rework",
            "reason": "修正回滚模板与 ConfigService 注入",
        }]
        self.reqs = [fake_req(90)]
        spawned = []
        review = {"role": "tech_review", "node_type": "review"}
        nodes = [{"role": "tech", "node_type": "artifact"}, review, {"node_type": "terminal"}]
        with _mock.patch.object(d, "current_node", lambda req: review), \
                _mock.patch.object(d, "flow_snapshot", lambda req: ("heavy", nodes, 1)), \
                _mock.patch.object(d, "_spawn_safe",
                                   lambda req, role, **k: spawned.append((role, k))):
            self._reconcile()
        self.assertEqual(self.set_reqs[-1], ("90", {"node_index": 0}))
        self.assertEqual(spawned[0][0], "tech")
        self.assertTrue(spawned[0][1]["resume"])
        self.assertIn("修正回滚模板与 ConfigService 注入", spawned[0][1]["extra"])
        self.assertEqual(self.status, [])
        self.assertTrue(any("人工裁决打回" in s for _, s in self.sums))
        self.assertTrue(any("再修一轮" in c for _, c in self.events))
        self.assertIn(("90", "reconcile 消费"), self.cleared)

    def test_reconcile_review_cap_pass(self):
        """confirm + review_index + pass → 清本轮轮次并推进下一节点"""
        from unittest import mock as _mock
        d.state["pending_confirm"]["90"] = {
            "req": None,
            "question": "方案评审连续 4 轮未通过",
            "kind": "confirm",
            "review_index": 1,
            "role": "tech_review",
        }
        d.state["flow_review_rounds"] = {"90": {"1": 4}}
        self.confirms = [{"req_id": 90, "kind": "confirm", "action": "pass"}]
        self.reqs = [fake_req(90)]
        advanced = []
        review = {"role": "tech_review", "node_type": "review"}
        nodes = [{"role": "tech", "node_type": "artifact"}, review, {"node_type": "terminal"}]
        with _mock.patch.object(d, "current_node", lambda req: review), \
                _mock.patch.object(d, "flow_snapshot", lambda req: ("heavy", nodes, 1)), \
                _mock.patch.object(d, "_clear_review_escalation", lambda *a, **k: None), \
                _mock.patch.object(d, "_advance_after_node",
                                   lambda req, node: advanced.append((req["id"], node["role"]))):
            self._reconcile()
        self.assertEqual(d.state["flow_review_rounds"]["90"], {})
        self.assertEqual(advanced, [(90, "tech_review")])
        self.assertTrue(any("人工裁决放行" in s for _, s in self.sums))
        self.assertIn(("90", "reconcile 消费"), self.cleared)

    def test_reconcile_ghost_decision(self):
        """表有 action 但无 pending（飞书并发消费）→ 清表 + 事件留痕，不执行任何动作"""
        self.confirms = [{"req_id": 91, "kind": "confirm_override", "action": "pass"}]
        self.reqs = [fake_req(91)]
        self._reconcile()
        self.assertEqual(self.finalized, [])
        self.assertEqual(self.status, [])
        self.assertIn(("91", "reconcile 幽灵清理"), self.cleared)  # 幽灵决策：清表 + 事件留痕
        self.assertTrue(any("无待确认 pending" in c for _, c in self.events))

    def test_reconcile_kind_mismatch_ghost(self):
        """表行 kind 与 pending 不匹配 → 按幽灵处理，不执行"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "confirm"}
        self.confirms = [{"req_id": 90, "kind": "review_gate", "action": "pass"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.finalized, [])
        self.assertEqual(list(d.state["pending_confirm"]), ["90"])  # pending 保留（kind 不匹配不消费）

    def test_reconcile_action_null_ignored(self):
        """action 为空（等待用户操作）→ 不消费、pending 保留"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "confirm_override"}
        self.confirms = [{"req_id": 90, "kind": "confirm_override", "action": None}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(list(d.state["pending_confirm"]), ["90"])
        self.assertEqual(self.finalized, [])

    # ---- C. #25 原顺序下终态 action 分流 ----
    def _terminal_reconcile(self, kind, action, status="done", **pend_extra):
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": kind, **pend_extra}
        self.confirms = [{"req_id": 90, "kind": kind, "action": action}]
        d.fetch_req = lambda rid: fake_req(int(rid), status=status)
        self._reconcile()

    def test_reconcile_terminal_reject_clears_without_execute(self):
        """done + review_gate/reject → 不执行（不改 open/later、不 finalize）、走清账单点后跳过"""
        d.state.setdefault("pending_merge_review", {})["90"] = {"req": None, "summary": "dev 摘要"}
        self._terminal_reconcile("review_gate", "reject")
        self.assertEqual(self.finalized, [])
        self.assertEqual(self.status, [])
        self.assertNotIn("90", d.state["pending_confirm"])
        self.assertNotIn("90", d.state["pending_merge_review"])
        self.assertIn(("90", "reconcile 终态残留清理"), self.cleared)

    def test_reconcile_terminal_stop_later_clears_without_execute(self):
        """done + confirm/stop_later → 不置 later，pending 与决策行同步清理"""
        self._terminal_reconcile("confirm", "stop_later", over_limit=True)
        self.assertEqual(self.status, [])
        self.assertNotIn("90", d.state["pending_confirm"])
        self.assertIn(("90", "reconcile 终态残留清理"), self.cleared)

    def test_reconcile_terminal_override_clears_without_execute(self):
        """dropped + confirm_override/pass → 不执行豁免/退回，统一清理"""
        self._terminal_reconcile("confirm_override", "pass", status="dropped")
        self.assertEqual(self.finalized, [])
        self.assertEqual(self.set_reqs, [])
        self.assertIn(("90", "reconcile 终态残留清理"), self.cleared)

    def test_reconcile_non_terminal_action_executes(self):
        """回归：doing 卡 action 仍按原逻辑执行（confirm_override/pass → finalize_done）"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "校验未过", "kind": "confirm_override"}
        self.confirms = [{"req_id": 90, "kind": "confirm_override", "action": "pass"}]
        d.fetch_req = lambda rid: fake_req(int(rid), status="doing")
        self._reconcile()
        self.assertEqual(self.finalized, [(90, None)])

    def test_reconcile_confirmations_order_unchanged(self):
        """#25 收敛口径：主循环不插入 ghost 前移、不新增第二套终态守卫；
        reconcile_decisions → reconcile_confirmations 相对顺序不变"""
        import inspect
        src = inspect.getsource(d._main_loop_tick)
        self.assertNotIn("reconcile_ghost_pending", src)
        self.assertLess(src.index("reconcile_decisions()"), src.index("reconcile_confirmations()"))

    # ---- 护栏单调计数（响应不清零）----
    def test_confirm_rounds_cleared_on_terminal_status(self):
        """#400·v2：终态（done/later）清 confirm_rounds 键——人工重开新周期从头计数，首轮不被误标超限。
        #412：走真实 set_status + 真实 save_state（STATE_FILE 已 patch 临时路径）——修复前锁内
        save_state 二次取非重入 state_lock 恒死锁（本测试挂起=失败）；修复后锁外保存，落盘无键。"""
        d.state["confirm_rounds"]["90"] = 6
        d.set_status = _D_REALS["set_status"]
        d.api = lambda method, path, body=None: {"success": True}
        d.set_status(90, "later")
        self.assertNotIn("90", d.state["confirm_rounds"])
        saved = json.loads(d.STATE_FILE.read_text()) if d.STATE_FILE.exists() else {}
        self.assertNotIn("90", saved.get("confirm_rounds", {}))  # 真实 save_state 落盘无键
        d.state["confirm_rounds"]["90"] = 5
        d.set_status(90, "done")
        self.assertNotIn("90", d.state["confirm_rounds"])
        saved = json.loads(d.STATE_FILE.read_text()) if d.STATE_FILE.exists() else {}
        self.assertNotIn("90", saved.get("confirm_rounds", {}))
        d.state["confirm_rounds"]["90"] = 3
        d.set_status(90, "doing")   # 非终态不清
        self.assertIn("90", d.state["confirm_rounds"])

    def test_reconcile_action_exception_keeps_decision(self):
        """消费动作抛错（如 finalize_done 异常）→ 恢复 pending + 表行未清可重试（fail-closed 不静默丢决策）"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "confirm_override"}
        self.confirms = [{"req_id": 90, "kind": "confirm_override", "action": "pass"}]
        self.reqs = [fake_req(90)]
        d.finalize_done = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        self._reconcile()
        self.assertEqual(list(d.state["pending_confirm"]), ["90"])  # pending 恢复保留
        self.assertEqual(self.cleared, [])                            # 表行未清（可重试）
        self.assertTrue(any("执行异常" in c for _, c in self.events))

    def test_rounds_monotonic_no_reset(self):
        """超限后任一消费动作不清零 confirm_rounds（语义=用户参与下也累计）"""
        for i in range(6):
            self._handle_need_confirm({"status": "need_confirm", "question": f"Q{i}?"}, role="dev")
        self.assertEqual(d.state["confirm_rounds"]["90"], 6)
        d.state["pending_confirm"]["90"]["kind"] = "confirm_override"
        self.confirms = [{"req_id": 90, "kind": "confirm_override", "action": "pass"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(d.state["confirm_rounds"]["90"], 6)  # 不清零


if __name__ == "__main__":
    unittest.main()
