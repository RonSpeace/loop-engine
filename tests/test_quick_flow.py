#!/usr/bin/env python3
"""B2 quick 流程：dev → terminal（无方案、无 LLM 评审、无验收），仅限文案/配置/单文件样式类小改。

- 配置：flows.json 含 quick（green），节点 = [dev(code), terminal]；heavy 描述不再含「视觉参考还原」。
- 升级闸：quick 卡 dev 完成时，改动触及调度器模块（RESTART_TRIGGER_FILES）或改动清单取不到
  （fail-closed）→ 快照改写为 standard 并进入其 dev 之后的节点（代码审查），不直接收尾。
- 其它流程不触发改动清单采集（不给存量路径引入 git 调用）。
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

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402
import tests.test_flow_engine as tfe  # noqa: E402  模块引用：避免父类用例被本文件重复收集


class QuickFlowConfig(unittest.TestCase):

    def test_quick_flow_defined(self):
        f = d.flow_by_id("quick")
        self.assertIsNotNone(f)
        self.assertEqual([(n.get("role"), n.get("node_type")) for n in f["nodes"]],
                         [("dev", "code"), (None, "terminal")])
        self.assertEqual(f["color"], "green")
        self.assertTrue(d.flow_enabled("quick"))
        self.assertEqual(ac.FLOW_CONFIG_ERROR, "")

    def test_default_flow_stays_heavy(self):
        """兜底仍走最完整的流程（前置环节没给出合法推荐时 fail-safe）"""
        self.assertEqual(d.default_flow_id(), "heavy")

    def test_descriptions_carry_routing_criteria(self):
        quick, heavy = d.flow_by_id("quick"), d.flow_by_id("heavy")
        self.assertNotIn("视觉参考", heavy["description"])
        self.assertIn("长驻进程", heavy["description"])
        for token in ("文案", "配置"):
            self.assertIn(token, quick["description"])

    def test_catalog_lists_quick_for_analysis(self):
        self.assertIn("quick（", d.pre_slot_contract_text("/tmp/x/result.json"))


class QuickUpgradeGate(tfe.HandleResultFlowTest):

    def setUp(self):
        super().setUp()
        self.paths_calls = []
        self._patches = [
            mock.patch.object(d, "_gen_review_diff", lambda req: None),
            mock.patch.object(d, "dev_branch", lambda req: "f-quick"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    def _quick_req(self):
        nodes = d.flow_by_id("quick")["nodes"]
        return tfe.fake_req(9, "doing", mode=None, flow="quick", snapshot=(nodes, 0))

    def _run_with_paths(self, req, paths):
        def fake_paths(r, branch):
            self.paths_calls.append(branch)
            return paths
        with mock.patch.object(d, "_collect_merge_changed_paths", fake_paths):
            self._run(req, "dev")

    def test_small_change_goes_straight_to_terminal(self):
        req = self._quick_req()
        self._run_with_paths(req, ["docs/guide.html", "README.md"])
        self.assertEqual(self.finalized, ["9"])
        self.assertEqual(self.spawns, [])
        self.assertEqual(req["flow"], "quick")

    def test_touching_dispatcher_module_upgrades_to_standard(self):
        req = self._quick_req()
        self._run_with_paths(req, ["docs/x.md", "dispatcher.py"])
        self.assertEqual(self.finalized, [])
        self.assertEqual(req["flow"], "standard")
        std = d.flow_by_id("standard")["nodes"]
        self.assertEqual(json.loads(req["nodes_json"]), std)
        self.assertEqual(std[req["node_index"]]["role"], "code_review")
        self.assertEqual(self.spawns, [("9", "code_review", False)])
        self.assertTrue(any("升级" in c and "standard" in c for _, c in self.events))

    def test_nested_path_matches_by_basename(self):
        req = self._quick_req()
        self._run_with_paths(req, ["sub/acp_shared.py"])
        self.assertEqual(req["flow"], "standard")

    def test_unknown_change_list_fails_closed(self):
        req = self._quick_req()
        self._run_with_paths(req, None)
        self.assertEqual(self.finalized, [])
        self.assertEqual(req["flow"], "standard")

    # ---- 评审复修：闸门覆盖面 + fail-closed ----
    def test_project_high_risk_paths_upgrade(self):
        """项目清单 high_risk_paths（前缀匹配）与 .loop/ 命中同样升级"""
        req = self._quick_req()
        with mock.patch.object(d, "_quick_high_risk_prefixes", lambda r: ["board.py", "prompts/"]):
            self._run_with_paths(req, ["prompts/dev.md"])
        self.assertEqual(req["flow"], "standard")

    def test_loop_dir_always_high_risk(self):
        req = self._quick_req()
        with mock.patch.object(d, "_quick_high_risk_prefixes", lambda r: []):
            self._run_with_paths(req, [".loop/guides/instance.md"])
        self.assertEqual(req["flow"], "standard")

    def test_prefix_match_is_path_based_not_substring(self):
        req = self._quick_req()
        with mock.patch.object(d, "_quick_high_risk_prefixes", lambda r: ["board.py"]):
            self._run_with_paths(req, ["docs/board.py.md"])
        self.assertEqual(self.finalized, ["9"])

    def test_no_target_flow_fails_closed(self):
        """standard 被禁用/缺失 → 不按 quick 收尾，转暂缓等人工"""
        req = self._quick_req()
        real = d.flow_enabled
        with mock.patch.object(d, "flow_enabled", lambda fid: False if fid == "standard" else real(fid)):
            self._run_with_paths(req, ["dispatcher.py"])
        self.assertEqual(self.finalized, [])
        self.assertEqual(self.spawns, [])
        self.assertIn(("9", {"status": "later"}), self.set_reqs)
        self.assertTrue(any("转暂缓" in c for _, c in self.events))


    def test_other_flows_do_not_collect_paths(self):
        nodes = d.flow_by_id("standard")["nodes"]
        req = tfe.fake_req(9, "doing", mode=None, flow="standard", snapshot=(nodes, 0))
        self._run_with_paths(req, ["dispatcher.py"])
        self.assertEqual(self.paths_calls, [])
        self.assertEqual(self.spawns, [("9", "code_review", False)])


for _name in dir(tfe.HandleResultFlowTest):
    if _name.startswith("test_"):
        setattr(QuickUpgradeGate, _name, None)


if __name__ == "__main__":
    unittest.main()
