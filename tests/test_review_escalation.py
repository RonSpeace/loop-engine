#!/usr/bin/env python3
"""C3 评审打回熔断与升级测试。

同一评审节点连续打回：第 1 至 MAX-1 轮照常打回；第 FLOW_REVIEW_ROUNDS_MAX(=3) 轮打回时作者模型升级为项目
`escalation_model`（未配置/不可用/与作者同模型 → 不升级、留痕后照常打回）；超过上限转 need_confirm，
问题附各轮打回要点对比。升级记录按卡 × 角色存 state["review_escalation"]，spawn_agent 拉起该角色时
自动带上（评审打回、人工 n 打回、失败重试共用），评审通过 / 人工放行 / 新周期清除。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402
import tests.test_flow_engine as tfe  # noqa: E402  模块引用：避免父类被本文件重复收集

fake_req = tfe.fake_req

NODES = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
         {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
         {"node_type": "terminal"}]


class ReviewEscalationFlow(tfe.HandleResultFlowTest):
    """复用 HandleResultFlowTest 的 mock 夹具（不继承其用例）"""

    def setUp(self):
        super().setUp()
        self._esc = mock.patch.object(d, "_escalation_candidate",
                                      lambda req, role: ("claude-opus-5", None))
        self._esc.start()

    def tearDown(self):
        self._esc.stop()
        super().tearDown()

    def _req(self):
        return fake_req(9, "doing", mode=None, snapshot=(NODES, 1))

    def test_c3_cap_is_three(self):
        self.assertEqual(d.FLOW_REVIEW_ROUNDS_MAX, 3)

    def test_c3_first_reject_bounces_without_escalation(self):
        self._run(self._req(), "tech_review", verdict="reject")
        self.assertEqual(self.spawns, [("9", "tech", True)])
        self.assertFalse((d.state.get("review_escalation") or {}).get("9"))

    def test_c3_cap_reject_escalates_author_model(self):
        d.state["flow_review_rounds"] = {"9": {"1": d.FLOW_REVIEW_ROUNDS_MAX - 1}}
        self._run(self._req(), "tech_review", verdict="reject")
        self.assertEqual(self.spawns, [("9", "tech", True)])
        self.assertEqual(d.state["review_escalation"]["9"]["tech"], "claude-opus-5")
        self.assertTrue(any(k == "model" and "升级" in c and "claude-opus-5" in c
                            for k, c in self.events))
        self.assertNotIn("9", d.state["pending_confirm"])

    def test_c3_no_escalation_model_still_bounces_and_notes(self):
        d.state["flow_review_rounds"] = {"9": {"1": d.FLOW_REVIEW_ROUNDS_MAX - 1}}
        with mock.patch.object(d, "_escalation_candidate",
                               lambda req, role: (None, "项目未配置 escalation_model")):
            self._run(self._req(), "tech_review", verdict="reject")
        self.assertEqual(self.spawns, [("9", "tech", True)])
        self.assertFalse((d.state.get("review_escalation") or {}).get("9"))
        self.assertTrue(any("未升级" in c and "escalation_model" in c for _, c in self.events))

    def test_c3_over_cap_goes_human_with_round_comparison(self):
        req = self._req()
        for _ in range(d.FLOW_REVIEW_ROUNDS_MAX):                 # 第 1..MAX 轮自动打回（第 MAX 轮升级）
            self._run(req, "tech_review", verdict="reject")
            req["node_index"] = 1
        self._run(req, "tech_review", verdict="reject")             # 第 MAX+1 轮 → 人工
        self.assertEqual(len(self.spawns), d.FLOW_REVIEW_ROUNDS_MAX)
        pend = d.state["pending_confirm"]["9"]
        self.assertEqual(pend["review_index"], 1)
        q = pend["question"]
        self.assertIn("轮评审未通过", q)
        for n in range(1, d.FLOW_REVIEW_ROUNDS_MAX + 2):
            self.assertIn(f"第 {n} 轮", q)
        self.assertIn("claude-opus-5", q)            # 注明已升级过模型

    def test_c3_pass_clears_escalation_and_points(self):
        d.state["flow_review_rounds"] = {"9": {"1": d.FLOW_REVIEW_ROUNDS_MAX - 1}}
        self._run(self._req(), "tech_review", verdict="reject")     # 第 2 轮 → 升级
        req = self._req()
        self._run(req, "tech_review", verdict="pass")
        self.assertFalse((d.state.get("review_escalation") or {}).get("9", {}).get("tech"))
        self.assertFalse((d.state.get("review_round_points") or {}).get("9", {}).get("1"))


# 父类用例不在本类重复执行（非可调用属性会被 unittest/pytest 收集器跳过）
for _name in dir(tfe.HandleResultFlowTest):
    if _name.startswith("test_"):
        setattr(ReviewEscalationFlow, _name, None)


class EscalationCandidate(unittest.TestCase):

    def _cand(self, configured, current="gpt-5.6-sol", pool=None):
        pool = pool if pool is not None else {
            "claude-opus-5": {"usable": True, "reason": ""},
            "broken": {"usable": False, "reason": "缺 max_context_size"}}
        with mock.patch.object(d, "project_escalation_model", lambda project: configured), \
             mock.patch.object(d, "resolve_role_model", lambda role, project=None: {"model": current}), \
             mock.patch.object(d, "model_alias_pool", lambda: (pool, None, "")):
            return d._escalation_candidate({"id": 9, "project": "loop-engine"}, "tech")

    def test_ok(self):
        self.assertEqual(self._cand("claude-opus-5"), ("claude-opus-5", None))

    def test_not_configured(self):
        m, why = self._cand(None)
        self.assertIsNone(m)
        self.assertIn("escalation_model", why)

    def test_same_as_author(self):
        m, why = self._cand("gpt-5.6-sol", current="gpt-5.6-sol")
        self.assertIsNone(m)
        self.assertIn("已是", why)

    def test_not_in_pool(self):
        m, why = self._cand("nope")
        self.assertIsNone(m)
        self.assertIn("不在别名池", why)

    def test_unusable(self):
        m, why = self._cand("broken")
        self.assertIsNone(m)
        self.assertIn("不可用", why)

    def test_pool_unavailable_fail_open(self):
        with mock.patch.object(d, "project_escalation_model", lambda project: "claude-opus-5"), \
             mock.patch.object(d, "resolve_role_model", lambda role, project=None: {"model": "x"}), \
             mock.patch.object(d, "model_alias_pool", lambda: (None, None, "无配置")):
            self.assertEqual(d._escalation_candidate({"id": 9, "project": "p"}, "tech"),
                             ("claude-opus-5", None))


class ProjectEscalationModel(unittest.TestCase):

    def test_reads_projects_json_each_call(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "projects.json"
            p.write_text(json.dumps({"loop-engine": {"paths": ["/x"], "escalation_model": "claude-opus-5"},
                                     "other": {"paths": ["/y"]}}))
            with mock.patch.object(ac, "PROJECTS_FILE", p):
                self.assertEqual(ac.project_escalation_model("loop-engine"), "claude-opus-5")
                self.assertIsNone(ac.project_escalation_model("other"))
                self.assertIsNone(ac.project_escalation_model(None))
                p.write_text("not json")
                self.assertIsNone(ac.project_escalation_model("loop-engine"))


class OverrideApplication(unittest.TestCase):

    def test_resolved_with_escalation_dict(self):
        base = {"model": "gpt-5.6-sol", "source": "project", "configured": "gpt-5.6-sol", "degraded": False}
        got = d._apply_model_override(base, {"model": "claude-opus-5", "source": "escalation",
                                             "degraded": False, "reason": "升级"})
        self.assertEqual(got["model"], "claude-opus-5")
        self.assertEqual(got["source"], "escalation")
        self.assertEqual(got["configured"], "gpt-5.6-sol")

    def test_resolved_with_legacy_string_unchanged(self):
        got = d._apply_model_override({"model": "a", "source": "project"}, "deepseek/deepseek-flash")
        self.assertEqual(got["model"], "deepseek/deepseek-flash")
        self.assertEqual(got["source"], "global")
        self.assertTrue(got["degraded"])

    def test_none_is_identity(self):
        base = {"model": "a"}
        self.assertEqual(d._apply_model_override(base, None), base)

    def test_spawn_agent_attaches_active_escalation(self):
        saved = d.state
        d.state = {"review_escalation": {"9": {"tech": "claude-opus-5"}}}
        captured = {}

        class FakeThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                captured["kwargs"] = kwargs or {}

            def start(self):
                pass
        req = fake_req(9, "doing", mode=None, snapshot=(NODES, 0))
        try:
            with mock.patch.object(d, "role_allowed", lambda r, role: True), \
                 mock.patch.object(d, "set_req", lambda *a, **k: None), \
                 mock.patch.object(d, "_note_review_model", lambda *a, **k: None), \
                 mock.patch.object(d, "build_prompt", lambda *a, **k: "p"), \
                 mock.patch.object(d, "find_resume_sid", lambda *a, **k: "sid"), \
                 mock.patch.object(d, "post_dispatcher_event", lambda *a, **k: 1), \
                 mock.patch.object(d.threading, "Thread", FakeThread):
                d.spawn_agent(req, role="tech", resume=True)
        finally:
            d.state = saved
        ov = captured["kwargs"].get("model_override")
        self.assertEqual(ov["model"], "claude-opus-5")
        self.assertEqual(ov["source"], "escalation")


if __name__ == "__main__":
    unittest.main()
