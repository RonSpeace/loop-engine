#!/usr/bin/env python3
"""角色输入与公共约束的四项修复（按角色模拟最终提示词时发现）：

R1 开发角色卡片带阶段输入：复杂流程下能拿到 tech.md / tech_review.md（方案评审的「开发须知」）路径
R2 验收、冲突解决角色卡片带需求详情（notes）
R3 作者 result.json 的 issue_responses 读后留档，复审评审的卡片输入里能看到；作者不回应 / 评审通过后清掉
R4 通用约束按角色能力措辞（测试、写路径、JSON 生成方式、todos）；项目约定只在项目事实上优先；评审不跑项目测试命令

中英文两套都验证。运行：.venv/bin/python -m pytest tests/test_prompt_role_inputs.py -q
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.test_prompt_i18n import EnglishCompileBase  # noqa: E402  复用隔离底座（临时仓库 + 项目清单 + 产物目录）

import dispatcher as d  # noqa: E402
import prompt_store  # noqa: E402
from dispatcher_modules import flow_advance  # noqa: E402


def heavy_index(role):
    return next(i for i, n in enumerate(d.flow_by_id("heavy")["nodes"]) if n.get("role") == role)


class RoleInputsZh(EnglishCompileBase):

    lang = "zh"
    words = {"stage": "阶段输入：", "notes": "需求详情：", "responses": "作者回应（上一轮问题",
             "tech_md": "技术方案产物", "review_md": "方案评审产物"}

    def setUp(self):
        super().setUp()
        self._art = mock.patch.object(d, "ARTIFACTS_ROOT", self.base / "art")
        self._art.start()

    def tearDown(self):
        self._art.stop()
        super().tearDown()

    def prompt(self, req, role):
        return d.compile_context(req, role, dry=True).prompt

    def test_r1_dev_gets_stage_input(self):
        req = self.req(31, flow="heavy", node_index=heavy_index("dev"))
        card = d.build_prompt_parts(req, "dev", dry=True)[3]
        self.assertIn(self.words["stage"], card)
        self.assertIn(self.words["tech_md"], card)
        self.assertIn(self.words["review_md"], card)
        self.assertIn("tech_review.md", card)

    def test_r2_accept_and_merge_resolve_get_notes(self):
        d.state["merge_resolve"]["32"] = {"failed": [str(self.r1)]}
        for role in ("accept", "merge_resolve"):
            req = self.req(32, flow="heavy", node_index=heavy_index("accept"), notes="验收：按钮变蓝 NOTE-32")
            card = d.build_prompt_parts(req, role, dry=True)[3]
            self.assertIn(self.words["notes"], card, role)
            self.assertIn('<card-data source="requirement.notes">验收：按钮变蓝 NOTE-32</card-data>', card, role)

    def test_r3_issue_responses_reach_the_reviewer(self):
        req = self.req(33, flow="heavy", node_index=heavy_index("code_review"))
        d.artifact_dir(req).mkdir(parents=True, exist_ok=True)
        self.assertNotIn(self.words["responses"], self.prompt(req, "code_review"))   # 首轮：没有回应
        responses = [{"id": "I1", "resolution": "已改为写入 URL query RESP-33"}]
        flow_advance._keep_issue_responses(req, "dev", {"status": "done", "issue_responses": responses})
        path = d.issue_responses_path(req, "dev")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), responses)
        text = self.prompt(req, "code_review")
        self.assertIn(self.words["responses"], text)
        self.assertIn('<card-data source="author.issue_responses">', text)
        self.assertIn("RESP-33", text)
        # 方案评审核对的是 tech 的回应，不会串到 dev 的
        treq = self.req(33, flow="heavy", node_index=heavy_index("tech_review"))
        self.assertNotIn("RESP-33", self.prompt(treq, "tech_review"))
        # 作者下一轮没带回应 → 旧回应清掉，不串到下一次复审
        flow_advance._keep_issue_responses(req, "dev", {"status": "done"})
        self.assertFalse(path.exists())

    def test_r3_review_pass_clears_responses(self):
        req = self.req(34, flow="heavy", node_index=heavy_index("code_review"))
        d.artifact_dir(req).mkdir(parents=True, exist_ok=True)
        flow_advance._keep_issue_responses(req, "dev", {"issue_responses": [{"id": "I1", "resolution": "x"}]})
        node = d.flow_by_id("heavy")["nodes"][heavy_index("code_review")]
        with mock.patch.object(d, "api_board", lambda *a, **k: None), \
                mock.patch.object(d, "append_summary", lambda *a, **k: None), \
                mock.patch.object(d, "post_dispatcher_event", lambda *a, **k: None), \
                mock.patch.object(d, "_advance_after_node", lambda *a, **k: None):
            flow_advance._review_node_done(req, node, {"verdict": "pass"}, "ok")
        self.assertFalse(d.issue_responses_path(req, "dev").exists())

    def test_r4_common_and_conventions_are_role_aware(self):
        common = prompt_store.read("system/common", self.lang)
        conv_req = self.req(35, flow="heavy", node_index=heavy_index("code_review"))
        text = self.prompt(conv_req, "code_review")
        if self.lang == "zh":
            self.assertIn("只读产物角色与冲突解决不跑测试", common)
            self.assertIn("只读角色与验收只能写产物目录", common)
            self.assertIn("只读角色与冲突解决（Bash 不能运行 python）用 Write/fs 工具", common)
            self.assertIn("验收角色不写", common)
            self.assertIn("权限与结果契约仍以上方说明为准", text)
            self.assertIn("本角色无权执行", text)
            self.assertNotIn("与通用说明不一致时以此为准）", text)
        else:
            self.assertIn("conflict resolution do not run tests", common)
            self.assertIn("read-only roles and acceptance only the artifact directory", common)
            self.assertIn("whose Bash cannot run python", common)
            self.assertIn("the acceptance role does not write it", common)
            self.assertIn("permissions and the result contract still follow the instructions above", text)
            self.assertIn("You may not run the project's test commands", text)
        self.assertIn(prompt_store.read("system/common", self.lang), text)


class RoleInputsEn(RoleInputsZh):

    lang = "en"
    words = {"stage": "Stage input: ", "notes": "Requirement details: ", "responses": "Author responses (item-by-item",
             "tech_md": "Tech design artifact", "review_md": "Design review artifact"}


if __name__ == "__main__":
    unittest.main()
