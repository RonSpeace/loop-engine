#!/usr/bin/env python3
"""主 Agent 协作模式：projects.json 项目级 `external_roles` 声明由外部主 Agent 负责的角色。

- spawn_agent 拉起这些角色时不建会话：卡转暂缓并留痕「等待外部主 Agent 产出」，
  外部主 Agent 写好产物后把卡指向下一节点并改回待开发，后续节点照常由调度器跑。
- 评审打回到外部角色同样只停卡，不拉起会话；打回轮次照常累计（第 4 次打回转人工）。
- 未声明 / 非法值 → 行为不变。
同时锁定：评审打回上限恢复为 3（第 4 次转人工）、单次运行默认时长与步数预算调高。
"""
import json
import shutil
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
import tests.test_flow_engine as tfe  # noqa: E402  模块引用：避免父类用例被本文件重复收集


class ProjectExternalRoles(unittest.TestCase):

    def test_reads_list_each_call(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "projects.json"
            p.write_text(json.dumps({"a": {"paths": ["/x"], "external_roles": ["tech", "proposal"]},
                                     "b": {"paths": ["/y"], "external_roles": "tech"},
                                     "c": {"paths": ["/z"], "external_roles": ["tech", 3]},
                                     "d": "/w"}))
            with mock.patch.object(ac, "PROJECTS_FILE", p):
                self.assertEqual(ac.project_external_roles("a"), ["tech", "proposal"])
                self.assertEqual(ac.project_external_roles("b"), [])       # 非列表 → 不生效
                self.assertEqual(ac.project_external_roles("c"), ["tech"])  # 非字符串元素忽略
                self.assertEqual(ac.project_external_roles("d"), [])
                self.assertEqual(ac.project_external_roles(None), [])


class SpawnParksExternalRole(unittest.TestCase):

    def setUp(self):
        self.calls = []
        self.events = []
        self.threads = []
        self._p = [
            mock.patch.object(d, "role_allowed", lambda r, role: True),
            mock.patch.object(d, "set_req", lambda rid, fields, **k: self.calls.append((str(rid), fields))),
            mock.patch.object(d, "set_status", lambda rid, st, **k: self.calls.append((str(rid), {"status": st}))),
            mock.patch.object(d, "post_dispatcher_event",
                              lambda rid, content, kind="action", **k: self.events.append((kind, content))),
            mock.patch.object(d, "append_summary", lambda rid, line, **k: None),
            mock.patch.object(d, "build_prompt", lambda *a, **k: "p"),
            mock.patch.object(d, "_note_review_model", lambda *a, **k: None),
            mock.patch.object(d, "find_resume_sid", lambda *a, **k: None),
            mock.patch.object(d.threading, "Thread",
                              lambda *a, **k: mock.Mock(start=lambda: self.threads.append(1))),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _req(self):
        nodes = d.flow_by_id("heavy")["nodes"]
        return tfe.fake_req(32, "open", mode=None, flow="heavy", snapshot=(nodes, 0))

    def test_external_role_parks_without_session(self):
        with mock.patch.object(d, "project_external_roles", lambda project: ["tech"]):
            d.spawn_agent(self._req(), role="tech")
        self.assertEqual(self.threads, [])
        self.assertIn(("32", {"status": "later"}), self.calls)
        self.assertNotIn(("32", {"status": "doing"}), self.calls)
        self.assertTrue(any("外部主 Agent" in c for _, c in self.events))

    def test_other_roles_unaffected(self):
        with mock.patch.object(d, "project_external_roles", lambda project: ["tech"]):
            d.spawn_agent(self._req(), role="tech_review")
        self.assertEqual(self.threads, [1])
        self.assertIn(("32", {"status": "doing"}), self.calls)

    def test_no_config_unaffected(self):
        with mock.patch.object(d, "project_external_roles", lambda project: []):
            d.spawn_agent(self._req(), role="tech")
        self.assertEqual(self.threads, [1])


class ReviewBounceToExternal(tfe.HandleResultFlowTest):
    """评审打回到外部角色：指针回退到作者节点、经 spawn 入口停卡；轮次照常累计"""

    def test_reject_bounces_to_author_node(self):
        nodes = d.flow_by_id("heavy")["nodes"]
        req = tfe.fake_req(9, "doing", mode=None, flow="heavy", snapshot=(nodes, 1))
        self._run(req, "tech_review", verdict="reject")
        self.assertEqual(req["node_index"], 0)
        self.assertEqual(self.spawns, [("9", "tech", True)])     # 经 _spawn_safe → spawn_agent（外部角色在此停卡）
        self.assertEqual(d.state["flow_review_rounds"]["9"], {"1": 1})


for _name in dir(tfe.HandleResultFlowTest):
    if _name.startswith("test_"):
        setattr(ReviewBounceToExternal, _name, None)


class LimitsRestored(unittest.TestCase):

    def test_review_rounds_back_to_three(self):
        """第 1–3 次打回自动重做，第 4 次转人工（恢复 C3 之前的口径）"""
        self.assertEqual(d.FLOW_REVIEW_ROUNDS_MAX, 3)

    def test_long_running_defaults(self):
        self.assertEqual(d.AGENT_TIMEOUT, 240 * 60)
        self.assertEqual(d.MAX_TIMEOUT_MINUTES, 480)
        self.assertGreaterEqual(ac.DEFAULT_ROLE_MAX_STEPS["dev"], 1000)


if __name__ == "__main__":
    unittest.main()
