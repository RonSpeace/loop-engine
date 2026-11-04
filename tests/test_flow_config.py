#!/usr/bin/env python3
"""#419 流程配置加载与校验（flows.json + prompts/*.md）测试。

覆盖 tech.md §6.2 的配置层用例：合法配置规范化、各类非法配置 fail-fast（聚合报错）、
角色键集与能力表双向一致（防未知角色 fail-open）、节点类型/terminal/review 前驱/同流程角色唯一、
提示词文件路径越界，以及「仓库真实 flows.json 与现状零行为变化」的锁定。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_flow_config -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import acp_shared as ac  # noqa: E402


def _base_config():
    """最小合法配置：2 角色 + 1 流程（比真实配置小，只保留校验相关结构）。"""
    return {
        "version": 1,
        "default_flow": "std",
        "pre": {"role": "analysis"},
        "roles": {
            "analysis": {"display": "需求分析", "order": 1, "artifact": "analysis.md",
                         "prompt_file": "prompts/analysis.md"},
            "dev": {"display": "需求开发", "order": 2, "artifact": None,
                    "prompt_file": "prompts/dev.md"},
        },
        "flows": [{"id": "std", "name": "标准", "nodes": [
            {"role": "analysis", "node_type": "artifact"},
            {"role": "dev", "node_type": "code"},
            {"node_type": "terminal"}]}],
    }


class FlowConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "prompts").mkdir()
        for name in ("analysis", "dev", "extra"):
            (self.dir / "prompts" / f"{name}.md").write_text(f"{name} prompt {{notes}}")
        # 校验要求 roles 键集 == 能力表键集，故默认配置按能力表补齐全部 9 个角色（order/display/artifact 对齐）
        roles = {}
        for role in ac.ROLE_CAPABILITIES:
            roles[role] = {"display": ac.role_meta(role, "display"), "order": ac.role_meta(role, "order"),
                           "artifact": ac.role_meta(role, "artifact"), "prompt_file": f"prompts/{role}.md"}
            (self.dir / "prompts" / f"{role}.md").write_text(f"{role} prompt {{notes}}")
        self.cfg = dict(_base_config())
        self.cfg["roles"] = roles

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, cfg=None):
        p = self.dir / "flows.json"
        p.write_text(json.dumps(cfg if cfg is not None else self.cfg))
        return ac.load_flow_config(p, self.dir)

    def errors(self, cfg=None):
        with self.assertRaises(ValueError) as cm:
            self.load(cfg)
        return str(cm.exception)

    # --- 合法配置 ---
    def test_valid_config_normalized(self):
        got = self.load()
        self.assertEqual(got["default_flow"], "std")
        self.assertEqual(got["pre"]["role"], "analysis")
        self.assertEqual(set(got["roles"]), set(ac.ROLE_CAPABILITIES))
        self.assertEqual(got["roles"]["analysis"]["artifact"], "analysis.md")
        self.assertIn("analysis prompt", got["roles"]["analysis"]["prompt"])
        nodes = got["flows"][0]["nodes"]
        self.assertEqual(nodes[-1], {"node_type": "terminal"})
        self.assertEqual(nodes[0], {"role": "analysis", "node_type": "artifact",
                                    "name": None, "artifact": "analysis.md"})
        self.assertEqual(got["flows"][0]["color"], ac.DEFAULT_FLOW_COLOR)   # A1 缺省即中性灰

    def test_repo_config_valid_and_zero_behavior_change(self):
        """仓库真实 flows.json：可加载、角色 display/artifact/order 与能力表逐键对齐（零行为变化）"""
        self.assertEqual(ac.FLOW_CONFIG_ERROR, "", ac.FLOW_CONFIG_ERROR)
        self.assertEqual(set(ac.flow_names()), {"quick", "standard", "heavy"})   # lean/auto 已整条删除
        self.assertEqual(ac.pre_role(), "analysis")
        self.assertEqual(ac.default_flow_id(), "heavy")
        expected = {"analysis": ("需求分析", 1, "analysis.md"), "proposal": ("产品方案", 2, "prd.md"),
                    "tech": ("技术方案", 3, "tech.md"), "tech_review": ("方案评审", 4, "tech_review.md"),
                    "dev": ("需求开发", 5, None), "code_review": ("代码审查", 6, "code_review.md"),
                    "accept": ("测试验证", 7, "accept.md"),
                    "merge_resolve": ("冲突解决", 8, None)}   # review 已合并进 code_review
        self.assertEqual(set(ac.ROLE_CAPABILITIES), set(expected))
        for role, (display, order, artifact) in expected.items():
            # #404 收敛的中文名不因配置化改名；#419 起 display/order/artifact 唯一源 = flows.json
            self.assertEqual(ac.role_meta(role, "display"), display, role)
            self.assertEqual(ac.role_meta(role, "order"), order, role)
            self.assertEqual(ac.role_meta(role, "artifact"), artifact, role)
            self.assertIsNone(ac.ROLE_CAPABILITIES[role].get("display"), f"{role} 能力表不应再存 display")
            self.assertTrue((REPO / ac.role_meta(role, "prompt_file")).is_file(), role)

    def test_node_types_single_source(self):
        self.assertEqual(ac.NODE_TYPES, ("artifact", "code", "review", "terminal", "human"))
        self.assertEqual(set(ac.NODE_TYPE_LABELS), set(ac.NODE_TYPES))

    # --- color（#428 流程档位配色语义名） ---
    def test_flow_colors_vocabulary_single_source(self):
        """A4 词表单一源：四色语义名，默认色在词表内，词表禁写 hex"""
        self.assertEqual(ac.FLOW_COLORS, ("green", "indigo", "fuchsia", "slate"))
        self.assertIn(ac.DEFAULT_FLOW_COLOR, ac.FLOW_COLORS)
        for token in ac.FLOW_COLORS:
            self.assertNotIn("#", token)

    def test_color_legal_value_preserved(self):
        """A2 合法值保留（含首尾空白 strip）"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["color"] = "indigo"
        self.assertEqual(self.load(cfg)["flows"][0]["color"], "indigo")
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["color"] = "  fuchsia  "
        self.assertEqual(self.load(cfg)["flows"][0]["color"], "fuchsia")

    def test_illegal_color_fail_fast(self):
        """A3 非法值加载期报错（大小写不符/hex/空串/数字/数组/未知色名均 fail-fast）"""
        for bad in ("blue", "#ff0000", "", "GREEN", 123, ["green"]):
            cfg = json.loads(json.dumps(self.cfg))
            cfg["flows"][0]["color"] = bad
            msg = self.errors(cfg)
            self.assertIn("color", msg, f"{bad!r}: {msg}")
            self.assertIn("green", msg, f"{bad!r}: {msg}")   # 报错文本带词表全文，便于照抄修正

    def test_repo_config_colors_locked(self):
        """A5 仓库真实配置：两条流程档位色锁定（标准靛蓝/复杂品红紫）"""
        self.assertEqual({f["id"]: f["color"] for f in ac.flows()},
                         {"quick": "green", "standard": "indigo", "heavy": "fuchsia"})
        for f in ac.flows():
            self.assertIn(f["color"], ac.FLOW_COLORS)

    def test_repo_config_every_flow_has_code_review(self):
        """每条启用流程都含 code_review 节点——合并前代码审查是硬约定
        （standard 曾是唯一例外：tech→dev→accept 直接进验收，没有审查；#423 闸门只覆盖 auto）。
        B2 唯一豁免 = quick：限文案/配置类极小改动，触及调度器模块由升级闸改走 standard
        （tests/test_quick_flow.py 锁定），其它流程不得无审查。"""
        for f in ac.enabled_flows():
            if f["id"] == "quick":
                continue
            self.assertTrue(any(n.get("role") == "code_review" for n in f["nodes"]),
                            f"流程 {f['id']} 缺 code_review 节点：{f['nodes']}")

    # --- 文件与顶层结构 ---
    def test_missing_file_fail_fast(self):
        with self.assertRaises(ValueError):
            ac.load_flow_config(self.dir / "nope.json", self.dir)

    def test_invalid_json_fail_fast(self):
        p = self.dir / "flows.json"
        p.write_text("{not json")
        with self.assertRaises(ValueError):
            ac.load_flow_config(p, self.dir)

    def test_empty_flows_and_nodes_fail_fast(self):
        for bad in ([], None):
            cfg = json.loads(json.dumps(self.cfg))
            cfg["flows"] = bad
            self.assertIn("flows", self.errors(cfg))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"] = []
        self.assertIn("nodes", self.errors(cfg))

    def test_duplicate_flow_id_fail_fast(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"].append(dict(cfg["flows"][0]))
        self.assertIn("重复", self.errors(cfg))

    def test_unknown_default_flow_fail_fast(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["default_flow"] = "nope"
        self.assertIn("default_flow", self.errors(cfg))

    # --- enabled 软禁用 ---
    def _two_flows(self):
        """双流程配置（第二条 alt 无 description），用于 enabled / description 必填判定"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"].append({"id": "alt", "name": "备选",
                             "nodes": [{"role": "dev", "node_type": "code"},
                                       {"node_type": "terminal"}]})
        return cfg

    def test_enabled_non_bool_fail_fast(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["enabled"] = "no"
        self.assertIn("enabled 必须是布尔", self.errors(cfg))

    def test_disabled_default_flow_fail_fast(self):
        """禁用默认流程 = 兜底目标消失 → 加载期拦住"""
        cfg = self._two_flows()
        cfg["flows"][0]["enabled"] = False
        self.assertIn("指向已禁用的流程", self.errors(cfg))

    def test_all_flows_disabled_fail_fast(self):
        cfg = self._two_flows()
        for f in cfg["flows"]:
            f["enabled"] = False
        msg = self.errors(cfg)
        self.assertIn("全部被禁用", msg)
        self.assertIn("指向已禁用的流程", msg)

    def test_disabled_flow_description_exempt(self):
        """description 必填按启用条数判定：禁用流程不写适用场景不报错，启用流程仍必填"""
        cfg = self._two_flows()
        cfg["flows"][1]["enabled"] = False
        self.load(cfg)                                        # 禁用那条无 description → 合法
        cfg = self._two_flows()
        self.assertIn("description 必填", self.errors(cfg))    # 两条都启用、alt 无 description → 报错

    def test_enabled_normalized_and_accessors(self):
        """规范化输出带 enabled（缺省 true）；enabled_flows/flow_enabled 只认启用项"""
        cfg = self._two_flows()
        cfg["flows"][1].update(enabled=False, description="备选场景")
        got = self.load(cfg)
        self.assertEqual([f["enabled"] for f in got["flows"]], [True, False])
        with mock.patch.dict(ac.FLOW_CONFIG, {"flows": got["flows"]}):
            self.assertEqual([f["id"] for f in ac.enabled_flows()], ["std"])
            self.assertTrue(ac.flow_enabled("std"))
            self.assertFalse(ac.flow_enabled("alt"))
            self.assertFalse(ac.flow_enabled("nope"))         # 未知 id 一并不认

    # --- roles / pre ---
    def test_unknown_role_key_fail_fast(self):
        """配置里出现能力表没有的角色（含拼写错误）→ 报错（防未知角色按默认档位 fail-open 拉起）"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["roles"]["analisys"] = {"display": "拼错", "order": 99, "artifact": None,
                                    "prompt_file": "prompts/analysis.md"}
        msg = self.errors(cfg)
        self.assertIn("analisys", msg)
        self.assertIn("能力表", msg)

    def test_missing_capability_role_fail_fast(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["roles"].pop("merge_resolve")
        self.assertIn("merge_resolve", self.errors(cfg))

    def test_role_display_empty_fail_fast(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["roles"]["dev"]["display"] = ""
        self.assertIn("display", self.errors(cfg))

    def test_artifact_field_contract(self):
        """artifact 只能是非空字符串或 null（dev 必须 null）"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["roles"]["tech"]["artifact"] = ""
        self.assertIn("artifact", self.errors(cfg))

    def test_role_order_unique_int(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["roles"]["dev"]["order"] = cfg["roles"]["analysis"]["order"]
        self.assertIn("order", self.errors(cfg))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["roles"]["dev"]["order"] = "2"
        self.assertIn("order", self.errors(cfg))

    def test_pre_role_must_exist(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["pre"]["role"] = "nobody"
        self.assertIn("pre.role", self.errors(cfg))

    def test_pre_role_requires_artifact(self):
        """pre.role 配成无产物角色（dev/merge_resolve）→ 报错（前置槽位契约要求产物非空）"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["pre"]["role"] = "dev"
        self.assertIn("artifact", self.errors(cfg))

    def test_dev_artifact_must_be_null(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["roles"]["dev"]["artifact"] = "self-test.md"
        self.assertIn("roles.dev.artifact", self.errors(cfg))

    # --- roles.thinking（#50 已下线：旧键静默忽略，不校验、不进规范化结果） ---
    def test_role_thinking_legacy_key_ignored(self):
        for legacy in ("  high ", "not-in-vocab", 3, ["high"], None):
            cfg = json.loads(json.dumps(self.cfg))
            cfg["roles"]["dev"]["thinking"] = legacy
            got = self.load(cfg)
            self.assertNotIn("thinking", got["roles"]["dev"])
            self.assertNotIn("thinking", got["roles"]["analysis"])

    # --- prompt_file ---
    def test_prompt_file_missing_fail_fast(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["roles"]["dev"]["prompt_file"] = "prompts/nope.md"
        self.assertIn("不存在", self.errors(cfg))

    def test_prompt_file_path_traversal_rejected(self):
        for bad in ("../secret.md", "/etc/passwd", "prompts/../../x.md"):
            cfg = json.loads(json.dumps(self.cfg))
            cfg["roles"]["dev"]["prompt_file"] = bad
            self.assertIn("相对路径", self.errors(cfg))

    # --- nodes ---
    def test_unknown_node_type_fail_fast(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"][0]["node_type"] = "artefact"
        self.assertIn("node_type", self.errors(cfg))

    def test_human_node_rejected(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"].insert(0, {"role": "dev", "node_type": "human"})
        self.assertIn("human 节点本期未实现", self.errors(cfg))

    def test_unknown_node_role_fail_fast(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"][1]["role"] = "qa"
        self.assertIn("不在 roles 中", self.errors(cfg))
        cfg = json.loads(json.dumps(self.cfg))
        del cfg["flows"][0]["nodes"][1]["role"]
        self.assertIn("role 缺失", self.errors(cfg))

    def test_terminal_last_and_unique(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"].insert(1, {"node_type": "terminal"})
        self.assertIn("恰有一个 terminal", self.errors(cfg))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"].insert(0, {"node_type": "terminal"})
        self.assertIn("末节点", self.errors(cfg))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"][-1]["role"] = "dev"
        self.assertIn("不得配 role", self.errors(cfg))

    def test_artifact_node_role_needs_artifact(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"][0]["role"] = "dev"
        self.assertIn("artifact", self.errors(cfg))

    def test_review_node_requires_predecessor(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"].insert(0, {"role": "analysis", "node_type": "review"})
        self.assertIn("前驱", self.errors(cfg))

    def test_review_predecessor_type_restricted(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"] = [{"role": "tech", "node_type": "artifact"},
                                    {"role": "analysis", "node_type": "review"},
                                    {"role": "tech_review", "node_type": "review"},
                                    {"node_type": "terminal"}]
        self.assertIn("前驱", self.errors(cfg))

    def test_review_node_ok_after_code(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"] = [{"role": "dev", "node_type": "code"},
                                    {"role": "analysis", "node_type": "review"},
                                    {"node_type": "terminal"}]
        got = self.load(cfg)
        self.assertEqual(got["flows"][0]["nodes"][1]["artifact"], "analysis.md")

    def test_duplicate_role_in_flow_fail_fast(self):
        """同一流程内 role 重复 → 报错（会话归属键 role:req_id 会冲突）"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"].insert(1, {"role": "analysis", "node_type": "artifact"})
        self.assertIn("重复", self.errors(cfg))

    def test_flow_can_add_and_remove_nodes(self):
        """验收「改配置即可增删流程内节点」：加一条 review、删一条 → 均加载成功"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["nodes"] = [{"role": "analysis", "node_type": "artifact"},
                                    {"role": "dev", "node_type": "code"},
                                    {"role": "tech", "node_type": "review"},
                                    {"node_type": "terminal"}]
        self.assertEqual(len(self.load(cfg)["flows"][0]["nodes"]), 4)
        cfg["flows"][0]["nodes"] = [{"role": "analysis", "node_type": "artifact"},
                                    {"node_type": "terminal"}]
        self.assertEqual(len(self.load(cfg)["flows"][0]["nodes"]), 2)

    def test_flow_can_add_and_remove_flows(self):
        """验收「改配置即可增删后置流程」：增删流程条目均加载成功（多条流程时每条须带 description）"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["description"] = "默认流程"
        cfg["flows"].append({"id": "quick", "name": "快线", "description": "路径明确的小改动",
                             "nodes": [
            {"role": "dev", "node_type": "code"}, {"node_type": "terminal"}]})
        got = self.load(cfg)
        self.assertEqual([f["id"] for f in got["flows"]], ["std", "quick"])
        self.assertEqual(got["flows"][1]["description"], "路径明确的小改动")

    # --- description（#419：流程适用场景/判据的唯一落点，模型据此选流程） ---
    def test_description_required_when_multiple_flows(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"].append({"id": "quick", "name": "快线", "nodes": [
            {"role": "dev", "node_type": "code"}, {"node_type": "terminal"}]})
        msg = self.errors(cfg)
        self.assertIn("description", msg)
        self.assertIn("必填", msg)

    def test_description_optional_for_single_flow(self):
        """只有一条流程时可省略（归一为 ""），给值时去首尾空白"""
        self.assertEqual(self.load()["flows"][0]["description"], "")
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["description"] = "  需求已明确的小改动  "
        self.assertEqual(self.load(cfg)["flows"][0]["description"], "需求已明确的小改动")

    def test_description_type_and_length(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["description"] = {"text": "不是字符串"}
        self.assertIn("必须是字符串", self.errors(cfg))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["flows"][0]["description"] = "长" * 301
        self.assertIn("过长", self.errors(cfg))

    def test_error_message_aggregates_all_violations(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["default_flow"] = "nope"
        cfg["pre"]["role"] = "nobody"
        cfg["roles"]["dev"].pop("prompt_file")
        cfg["flows"][0]["nodes"][1]["node_type"] = "bad"
        msg = self.errors(cfg)
        for token in ("default_flow", "pre.role", "prompt_file", "node_type"):
            self.assertIn(token, msg, msg)
        self.assertGreaterEqual(msg.count("\n- "), 3, msg)


if __name__ == "__main__":
    unittest.main()
