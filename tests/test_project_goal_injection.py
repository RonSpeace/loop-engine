#!/usr/bin/env python3
"""#5 goal 注入测试（dispatcher build_prompt / board 建卡对话 / guide 占位符表）。

B1 load_projects 透传 goal（缺失/非 str → ""）；B2 analysis 渲染含「项目目标（goal）：…」；
B3 无 goal 时不渲染该行 + 全角色渲染不 KeyError；B4 board _chat_system_prompt 同文案注入；
B5 模板不含具体 goal + /guide 占位符表含 {project_goal_hint}。

隔离：B1 用临时 projects.json 钉 d.PROJECTS_FILE；build_prompt 用例 mock ensure_dev_worktrees /
dev_repo_listing（防真建 worktree），不触真实 projects.json / board.db。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_project_goal_injection.py -q
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

import board as b  # noqa: E402
import dispatcher as d  # noqa: E402

ROLES = ("analysis", "proposal", "tech", "tech_review", "dev", "code_review", "accept")


class LoadProjectsGoalTest(unittest.TestCase):
    def test_b1_goal_passthrough(self):
        tmp = Path(tempfile.mkdtemp(prefix="req5-goal-"))
        pj = tmp / "projects.json"
        orig = d.PROJECTS_FILE
        d.PROJECTS_FILE = pj
        try:
            pj.write_text(json.dumps({
                "g1": {"paths": ["/p"], "goal": "目标一"},
                "g2": {"paths": ["/p"]},
                "g3": {"paths": ["/p"], "goal": 3},
                "short": "/p",
            }), encoding="utf-8")
            out = d.load_projects()
            self.assertEqual(out["g1"]["goal"], "目标一")
            self.assertEqual(out["g2"]["goal"], "")
            self.assertEqual(out["g3"]["goal"], "")
            self.assertEqual(out["short"]["goal"], "")
        finally:
            d.PROJECTS_FILE = orig
            shutil.rmtree(tmp, ignore_errors=True)


class BuildPromptGoalTest(unittest.TestCase):
    def render(self, role, goal, project="demo"):
        req = {"id": 5, "project": project, "title": "T"}
        projects = {project: {"paths": ["/p"], "confirm": False, "company": False, "repo_map": "",
                              "restart": None, "auto_restart_after_merge": False,
                              "base_branch": None, "goal": goal}}
        wd = Path("/tmp/req5-goal-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {project: wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value="工作目录：/tmp/req5-goal-wd"), \
                mock.patch.object(d, "PROJECTS", projects):
            return d.build_prompt(req, role)

    def test_b2_analysis_prompt_contains_goal(self):
        prompt = self.render("analysis", "G-目标")
        self.assertIn("项目目标（goal）：G-目标", prompt)

    def test_b3_no_goal_clean_and_all_roles(self):
        prompt = self.render("analysis", "")
        self.assertNotIn("项目目标", prompt)               # 无 goal：整行不渲染，无悬空标签
        for role in ROLES:
            rendered = self.render(role, "G")
            self.assertNotIn("{project_goal_hint}", rendered, role)   # 占位符均已填充
            self.assertNotIn("{repo_map_hint}", rendered, role)


class ChatPromptGoalTest(unittest.TestCase):
    def test_b4_chat_prompt_injects_goal(self):
        with mock.patch.object(b, "_auto_projects_config",
                               return_value={"demo": {"paths": [], "goal": "建卡目标G"}}):
            prompt = b._chat_system_prompt("demo")
        self.assertIn("项目目标（goal）：建卡目标G", prompt)
        with mock.patch.object(b, "_auto_projects_config",
                               return_value={"demo": {"paths": [], "goal": ""}}):
            self.assertNotIn("项目目标", b._chat_system_prompt("demo"))
        with mock.patch.object(b, "_auto_projects_config", return_value={}):
            self.assertNotIn("项目目标", b._chat_system_prompt("demo"))

    def test_b5_template_and_guide_placeholder(self):
        with mock.patch.object(b, "_auto_projects_config",
                               return_value={"demo": {"paths": [], "goal": "不该出现在模板"}}):
            tmpl = b._chat_system_prompt_template()
        self.assertNotIn("不该出现在模板", tmpl)
        self.assertNotIn("项目目标", tmpl)                 # guide 展示态（"{project}"）不注入 goal
        resp = b.app.test_client().get("/guide")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("{project_goal_hint}", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
