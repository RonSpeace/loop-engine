#!/usr/bin/env python3
"""req-386 网页端 merge_conflict 确认按钮（对账式）测试。

改动：reconcile_decisions() 新增 kind=merge 对账分支——扫描 merge_conflicts 表 action 非空记录，
以 pending_confirm（kind=merge）为权威消费（与 on_message 同 state_lock 互斥）：
- retry → 重试合并：成功 cleanup + restart 配方 + _set_done（dispatcher_touched 判定）；仍冲突重挂 pending + 重置按钮
- takeover → 与飞书 n 语义一致：置 later，分支/worktree 保留
- 无 pending（飞书并发消费/幽灵）→ 清表 + 事件留痕
另覆盖 finalize_done 冲突登记 / on_message y、n 的表同步（merge_conflict_write/clear 调用）。

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
    "state", "save_state", "api", "api_board", "fetch_req", "notify", "post_dispatcher_event",
    "set_status", "set_req", "append_summary", "merge_dev_branches", "cleanup_dev_worktrees",
    "_set_done", "run_project_restart", "_touches_dispatcher", "merge_conflict_write",
    "merge_conflict_clear", "finalize_done")}


def fresh_state():
    return {"chat_id": "oc_req386_test", "retries": {}, "pending_confirm": {},
            "pending_route": {}, "pending_review": {}, "sessions": {},
            "worktrees": {}, "flow_review_rounds": {}}


def fake_req(rid, status="doing", mode="auto", project="req-board", phase="dev"):
    return {"id": rid, "title": f"req{rid}", "project": project, "status": status,
            "mode": mode, "phase": phase}


class L0MergeWebDecision(unittest.TestCase):
    """L0 单测：mock 副作用，校验 merge_conflict 对账四分支 + 表同步调用"""

    def setUp(self):
        d.state = fresh_state()
        self.notifies = []        # notify 记录
        self.events = []          # post_dispatcher_event 记录 [(kind, content), ...]
        self.status = []          # set_status 记录 (rid, status)
        self.set_reqs = []        # set_req 记录
        self.apis = []            # api 记录 (method, path, body)
        self.sums = []            # append_summary 记录
        self.dones = []           # _set_done 记录 (req, kwargs)
        self.conflicts = []       # api_board 返回的 merge_conflicts 列表
        self.merge_result = (True, [])   # merge_dev_branches 返回
        self.touched = False      # _touches_dispatcher 返回值
        self.reqs = []            # api GET 列表返回的需求
        d.save_state = lambda: None
        d.api = lambda method, path, body=None: (
            self.apis.append((method, path, body)),
            self.reqs if method == "GET" else {"success": True})[1]
        d.api_board = lambda method, path, body=None: self.conflicts
        d.fetch_req = lambda rid: fake_req(rid)
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.set_status = lambda rid, status: self.status.append((rid, status))
        d.set_req = lambda rid, body: self.set_reqs.append((rid, body))
        d.append_summary = lambda rid, line: self.sums.append((rid, line))
        d.merge_dev_branches = lambda req: self.merge_result
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.run_project_restart = lambda *a, **k: None
        d._set_done = lambda req, **kw: self.dones.append((req["id"], kw))
        d._touches_dispatcher = lambda req, branch: self.touched
        d.merge_conflict_write = lambda rid, action=None: self.apis.append(("MRG_WRITE", rid, action))
        d.merge_conflict_clear = lambda rid, reason="": self.apis.append(("MRG_CLEAR", rid, reason))

    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)

    def _reconcile(self):
        d.reconcile_decisions()

    # ---- retry 成功 ----
    def test_retry_success_done(self):
        """验收：点「重试合并」→ merge 成功 → cleanup + restart + _set_done + 清理表记录+提示已完成"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        self.conflicts = [{"req_id": 90, "action": "retry"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.dones, [(90, {"dispatcher_touched": False, "restart_failed": None,
                                            "restart_ok": False, "restart_warn": ""})])
        self.assertEqual(list(d.state["pending_confirm"]), [])   # pending 已消费
        self.assertIn(("MRG_CLEAR", "90", "reconcile 消费"), self.apis)  # 消费后清表
        self.assertTrue(any("重试合并成功" in t for t in self.notifies))

    def test_retry_success_dispatcher_touched(self):
        """验收：#380——dev 分支改动涉及 dispatcher.py → _set_done(dispatcher_touched=True) 走自动重启"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        d.state["worktrees"]["90"] = {"branch": "f-386"}
        self.touched = True
        self.conflicts = [{"req_id": 90, "action": "retry"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.dones[0][1]["dispatcher_touched"], True)

    # ---- retry 仍冲突 ----
    def test_retry_conflict_repend(self):
        """验收：仍冲突 → 保持 doing、重挂 pending、重置按钮等待再次点击，提示继续处理"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        self.conflicts = [{"req_id": 90, "action": "retry"}]
        self.reqs = [fake_req(90)]
        self.merge_result = (False, ["repo-a"])
        self._reconcile()
        pend = d.state["pending_confirm"].get("90")
        self.assertIsNotNone(pend)                      # pending 重挂
        self.assertEqual(pend["kind"], "merge")
        self.assertTrue(any("仍冲突" in t for t in self.notifies))
        self.assertEqual(self.dones, [])                # 不 done
        # 消费时清表 + 失败后重新登记等待再次点击
        self.assertIn(("MRG_CLEAR", "90", "reconcile 消费"), self.apis)
        self.assertIn(("MRG_WRITE", "90", None), self.apis)

    # ---- takeover ----
    def test_takeover_later(self):
        """验收：点「人工接管」→ 与飞书 n 语义一致：置 later、分支/worktree 保留（cleanup 不执行）"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        self.conflicts = [{"req_id": 90, "action": "takeover"}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.status, [(90, "later")])
        self.assertEqual(list(d.state["pending_confirm"]), [])
        self.assertTrue(any("人工接管" in t for t in self.notifies))
        self.assertTrue(any("人工接管" in s for _, s in self.sums))
        self.assertEqual(self.dones, [])

    # ---- 幽灵决策（无 pending）----
    def test_ghost_decision_clear(self):
        """验收：表有 action 但无 pending（飞书并发消费/已处理）→ 清表 + 事件留痕，不执行任何动作"""
        self.conflicts = [{"req_id": 91, "action": "retry"}]
        self.reqs = [fake_req(91)]
        self._reconcile()
        self.assertEqual(self.dones, [])
        self.assertEqual(self.status, [])
        self.assertEqual(self.notifies, [])
        self.assertTrue(any("无待确认 pending" in c for _, c in self.events))

    # ---- action 为空不消费 ----
    def test_action_null_ignored(self):
        """登记态（action 空）仅作按钮渲染信号：不消费、不发通知"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        self.conflicts = [{"req_id": 90, "action": None}]
        self.reqs = [fake_req(90)]
        self._reconcile()
        self.assertEqual(self.notifies, [])
        self.assertEqual(list(d.state["pending_confirm"]), ["90"])  # pending 保留
        self.assertEqual(self.dones, [])

    # ---- 需求不存在 ----
    def test_req_missing_clear(self):
        """表有 action 但需求已不存在 → 清表（residue 兜底）"""
        d.state["pending_confirm"]["92"] = {"req": None, "question": "Q?", "kind": "merge"}
        self.conflicts = [{"req_id": 92, "action": "retry"}]
        self.reqs = []
        self._reconcile()
        self.assertEqual(list(d.state["pending_confirm"]), [])
        self.assertEqual(self.dones, [])

    # ---- 与飞书双通道并存（on_message 回归不受影响 + 表同步调用）----
    def test_feishu_n_clears_table(self):
        """飞书 n（接管）→ 原语义不变 + 同步清表"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        d.fetch_req = lambda rid: fake_req(rid)
        d.on_message(_fake_msg("n 90 人工处理"))
        self.assertEqual(self.status, [(90, "later")])
        self.assertIn(("MRG_CLEAR", 90, "飞书 n 接管"), self.apis)

    def test_feishu_y_success_clears_table(self):
        """飞书 y 重试成功 → 原语义不变 + 同步清表"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        d.fetch_req = lambda rid: fake_req(rid)
        d.on_message(_fake_msg("y 90"))
        self.assertEqual(self.dones, [(90, {"dispatcher_touched": False, "restart_failed": None,
                                            "restart_ok": False, "restart_warn": ""})])
        self.assertIn(("MRG_CLEAR", 90, "飞书 y 重试成功"), self.apis)

    def test_feishu_y_conflict_reposts(self):
        """飞书 y 仍冲突 → 重挂 pending + 重新登记按钮（语义不变）"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        d.fetch_req = lambda rid: fake_req(rid)
        self.merge_result = (False, ["repo-a"])
        d.on_message(_fake_msg("y 90"))
        self.assertEqual(d.state["pending_confirm"]["90"]["kind"], "merge")
        self.assertIn(("MRG_WRITE", 90, None), self.apis)


def _fake_msg(text):
    import types
    content = json.dumps({"text": text})
    return types.SimpleNamespace(event=types.SimpleNamespace(
        message=types.SimpleNamespace(message_type="text", content=content, chat_id="oc_req386_test")))


if __name__ == "__main__":
    unittest.main()
