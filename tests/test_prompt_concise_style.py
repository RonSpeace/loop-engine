#!/usr/bin/env python3
"""req-327 提示词输出规范（简洁直诉重点）测试。

改动：dispatcher.py COMMON_TAIL 新增「输出风格」段（对话类/产物文档/result.json summary 三档
硬规则）；ROLE_PROMPTS 六角色模板各加产物格式要求；run_agent 两条 resume 续接文案注入
RESUME_STYLE_REMINDER（存量会话保留旧系统提示词，续接时补提醒）；board.py
_chat_system_prompt / _reqchat_system_prompt 输出规范扩充对话简洁档规则。
req-334 追加：board.py 两处聊天系统提示词新增「独立判断」硬规则（建卡对话完整版 + 需求助手
轻量版），本文件新增 IndependentJudgmentPromptTest 断言（独立判断/替代建议等关键词）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899（#343）

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402
import board as b  # noqa: E402


def fake_req(rid=327, title="T", mode=None):
    req = {"id": rid, "project": "req-board", "title": title}
    if mode:
        req["mode"] = mode
    return req


class CommonTailStyleTest(unittest.TestCase):
    """验收：COMMON_TAIL 渲染含输出风格三档硬规则，六角色渲染后 prompt 均含"""

    KEYWORDS = ["输出风格", "结论先行", "两三百字", "禁止开场白",
                "每要点一行", "必须完整保留，不得因精简缺失", "一般不超过 10 行"]

    def render(self, role, mode=None):
        req = fake_req(mode=mode)
        wd = Path("/tmp/req327-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            return d.build_prompt(req, role)

    def assert_style(self, prompt):
        for kw in self.KEYWORDS:
            self.assertIn(kw, prompt, kw)

    def test_common_tail_renders_style_rules(self):
        """#42 静态化：COMMON_TAIL 常量直接含输出风格三档硬规则（不再 format、无占位符）"""
        self.assert_style(d.COMMON_TAIL)
        self.assertNotIn("{result_json}", d.COMMON_TAIL)  # 静态常量无 format 占位符

    def test_analysis_prompt(self):
        self.assert_style(self.render("analysis"))

    def test_proposal_prompt(self):
        self.assert_style(self.render("proposal"))

    def test_tech_prompt(self):
        self.assert_style(self.render("tech"))

    def test_dev_auto_prompt(self):
        self.assert_style(self.render("dev", mode="auto"))

    def test_dev_semi_prompt(self):
        self.assert_style(self.render("dev", mode="semi"))

    def test_accept_prompt(self):
        self.assert_style(self.render("accept"))



def _render_for_repo_project(role):
    """A1：按「本仓库项目」（基线钉桩 req-board → REPO，含 .loop/project.json）渲染最终 prompt——
    项目专有内容（实例脚本/端口段/渲染断言方式）来自项目清单，不在全局模板里。"""
    wd = Path("/tmp/a1-render-wd")
    with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
            mock.patch.object(d, "dev_repo_listing", return_value="工作目录：/tmp/a1-render-wd"):
        return d.build_prompt({"id": 433, "project": "req-board", "title": "T", "notes": "N"}, role)


PROJECT_ONLY_TOKENS = ("scripts/test_instance.sh", "REQ_BOARD_PYTHON", "8902-8941", "8942-8981",
                       "test_instance.sh status", "node harness", "8899", "launchd", "./start.sh")


class RoleArtifactFormatTest(unittest.TestCase):
    """验收：六角色模板各自产物有篇幅与格式要求；验收/证据/改动清单要求完整保留"""

    def test_analysis_artifact_format(self):
        p = d.ROLE_PROMPTS["analysis"]
        self.assertIn("结构精炼、每要点一行", p)
        self.assertIn("证据清单逐条附出处", p)

    def test_proposal_artifact_format(self):
        p = d.ROLE_PROMPTS["proposal"]
        self.assertIn("结构精炼、每要点一行", p)
        self.assertIn("验收标准逐条完整", p)

    def test_tech_artifact_format(self):
        p = d.ROLE_PROMPTS["tech"]
        self.assertIn("结构精炼、每要点一行", p)
        self.assertIn("跨仓库改动清单与测试用例清单必须完整保留", p)

    def test_dev_self_test_format(self):
        for key in ("dev_auto", "dev_semi"):
            p = d.ROLE_PROMPTS[key]
            self.assertIn("自测报告要点化", p, key)
            self.assertIn("删除过程叙述", p, key)

    def test_accept_checklist_format(self):
        tmpl = d.ROLE_PROMPTS["accept"]
        for token in PROJECT_ONLY_TOKENS:                 # A1：全局模板不带单个项目的专有内容
            self.assertNotIn(token, tmpl, token)
        p = _render_for_repo_project("accept")            # 关键词在本仓库项目的最终 prompt 里齐全
        self.assertIn("核对表逐条对应证据、必须完整", p)
        # #433：验证方式显式契约（承载关系/端口分配/浏览器级/落点/清理/自证/降级）+ 产出结构小节
        for kw in ("被测对象", "实例脚本", "与验证无关",
                   "不可行", "不以耗时",
                   "scripts/test_instance.sh", "REQ_BOARD_PYTHON", "实例地址", "启动命令", "操作步骤", "lsof",
                   "8902-8941", "8942-8981", "取模", "test_instance.sh status", "顺延",
                   "不构成浏览器级验证", "可判定断言", "node harness",
                   "产物目录", "/tmp", "filename", ".playwright-mcp",
                   "未覆盖", "造数",
                   "trap", "killpg", "pkill",
                   "git status --porcelain",
                   "未覆盖项", "summary",
                   "禁止修改任何源码文件", "禁止 git 提交类操作", "禁止重启 main 实例",
                   "验证方式与证据", "清理确认", "worktree 状态"):
            self.assertIn(kw, p, kw)


class ResumeStyleReminderTest(unittest.TestCase):
    """验收：存量 resume 会话续接文案补输出风格提醒（dispatcher.py:2127+ 两条路径）"""

    def test_reminder_constant_content(self):
        for kw in ("结论先行", "两三百字", "禁止开场白", "每要点一行", "一般不超过 10 行"):
            self.assertIn(kw, d.RESUME_STYLE_REMINDER, kw)

    def test_both_resume_branches_inject_reminder(self):
        # user_text 续接与无结果 resumed 两条续跑文案（prompts/system/resume-*.md）均拼接输出风格提醒
        req = {"id": 7, "project": "req-board"}
        for user_text in ("你好", None):
            text, _rid = d.resume_text(req, "dev", "", user_text)
            self.assertIn(d.RESUME_STYLE_REMINDER, text)


class ChatPromptStyleTest(unittest.TestCase):
    """验收：建卡对话 / 需求对话系统提示词含对话简洁档规则"""

    KEYWORDS = ["结论先行", "两三百字", "禁止开场白", "再问是否需要展开", "- 列表"]

    def test_chat_system_prompt(self):
        prompt = b._chat_system_prompt("req-board")
        for kw in self.KEYWORDS:
            self.assertIn(kw, prompt, kw)
        # 既有代码块约束保留
        self.assertIn("代码块必须成对闭合", prompt)

    def test_reqchat_system_prompt(self):
        tmp = Path(tempfile.mkdtemp(prefix="req327-db-"))
        try:
            orig_db, orig_art = b.DB_PATH, b.ARTIFACTS_ROOT
            b.DB_PATH = tmp / "board.db"
            b.ARTIFACTS_ROOT = tmp / "artifacts"
            b.init_db()
            req = b.req_add("需求", "req-board", notes="背景", status="later")
            prompt = b._reqchat_system_prompt(req["id"])
            for kw in self.KEYWORDS:
                self.assertIn(kw, prompt, kw)
            self.assertIn("代码块必须成对闭合", prompt)
        finally:
            b.DB_PATH, b.ARTIFACTS_ROOT = orig_db, orig_art
            shutil.rmtree(tmp, ignore_errors=True)


class IndependentJudgmentPromptTest(unittest.TestCase):
    """验收：#334 建卡对话 / 需求对话系统提示词含独立判断（敢于异议与替代建议）规则"""

    COMMON = ["独立判断", "替代建议", "结论先行"]

    def test_chat_system_prompt(self):
        prompt = b._chat_system_prompt("req-board")
        for kw in self.COMMON:
            self.assertIn(kw, prompt, kw)
        # 建卡版完整规则：触发条件/表达方式/边界
        for kw in ("触发条件", "明显更优替代", "选择代价", "由用户拍板",
                   "不要为反对而反对", "用户坚持后", "写进草稿 notes"):
            self.assertIn(kw, prompt, kw)

    def test_reqchat_system_prompt(self):
        tmp = Path(tempfile.mkdtemp(prefix="req334-db-"))
        try:
            orig_db, orig_art = b.DB_PATH, b.ARTIFACTS_ROOT
            b.DB_PATH = tmp / "board.db"
            b.ARTIFACTS_ROOT = tmp / "artifacts"
            b.init_db()
            req = b.req_add("需求", "req-board", notes="背景", status="later")
            prompt = b._reqchat_system_prompt(req["id"])
            for kw in self.COMMON:
                self.assertIn(kw, prompt, kw)
            # 需求助手轻量版规则
            for kw in ("只是输入", "有实质异议时", "按其确认的方向执行"):
                self.assertIn(kw, prompt, kw)
        finally:
            b.DB_PATH, b.ARTIFACTS_ROOT = orig_db, orig_art
            shutil.rmtree(tmp, ignore_errors=True)


class NotesFormatHintTest(unittest.TestCase):
    """验收：#381 notes 统一排版约定——两处聊天系统提示词均引用 REQ_NOTES_FMT_HINT 常量"""

    KEYWORDS = ["空行分段", "背景、方案要点、验收标准、生效方式各一段", "列表逐条分行", "挤成一坨"]

    def assert_hint(self, prompt):
        for kw in self.KEYWORDS:
            self.assertIn(kw, prompt, kw)

    def test_chat_system_prompt(self):
        self.assert_hint(b._chat_system_prompt("req-board"))

    def test_reqchat_system_prompt(self):
        tmp = Path(tempfile.mkdtemp(prefix="req381-db-"))
        try:
            orig_db, orig_art = b.DB_PATH, b.ARTIFACTS_ROOT
            b.DB_PATH = tmp / "board.db"
            b.ARTIFACTS_ROOT = tmp / "artifacts"
            b.init_db()
            req = b.req_add("需求", "req-board", notes="背景", status="later")
            self.assert_hint(b._reqchat_system_prompt(req["id"]))
        finally:
            b.DB_PATH, b.ARTIFACTS_ROOT = orig_db, orig_art
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
