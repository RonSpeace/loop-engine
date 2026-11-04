#!/usr/bin/env python3
"""req-104 验证规范测试：关键验证前台运行、禁止后台任务空轮询。

改动：COMMON_TAIL 新增第 8/9 条（前台验证、禁止后台任务+空轮询、重试/续跑确认）；
run_agent 续跑 prompt 补「先确认改动完好、无残留 result.json」；AGENTS.md 新增
「验证约定」段。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
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

import dispatcher as d  # noqa: E402


class PromptVerificationRuleTest(unittest.TestCase):
    """验收标准 1/3：渲染后的 COMMON_TAIL 含新规则；AGENTS.md 验证约定同步"""

    RULES = ("前台", "后台任务", "空轮询", "run_in_background")

    def test_t1_common_tail_contains_rules(self):
        """#42 静态化：COMMON_TAIL 常量含验证规则且无 format 占位符；
        结果路径按卡变化 → 改挂 build_prompt 产物（见 test_t2）"""
        rendered = d.COMMON_TAIL
        for kw in self.RULES:
            self.assertIn(kw, rendered, kw)
        self.assertIn("重试/续跑场景", rendered)
        self.assertIn("result.json", rendered)
        self.assertNotIn("{result_json}", rendered)  # 静态常量无 format 占位符

    def test_t2_build_prompt_dev_renders_rules(self):
        """验收 1：build_prompt dev 角色渲染结果含新规则（工作约束区注入）"""
        req = {"id": 104, "project": "req-board", "title": "T", "mode": "auto", "notes": "N"}
        wd = Path("/tmp/req104-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(req, "dev")
        for kw in self.RULES:
            self.assertIn(kw, prompt, kw)
        self.assertIn("重试/续跑场景", prompt)
        self.assertIn("self-test.md", prompt)

    def test_t3_resume_prompt_confirm_step(self):
        """续跑 prompt 补确认步骤：源码含「先确认改动完好、无残留 result.json」"""
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("先确认改动完好（git status 等）、产物目录无残留 result.json", src)

    def test_t4_agents_md_verification_section(self):
        """验收 3：AGENTS.md 结果契约/验证约定同步（前台验证 + 重试/续跑确认）"""
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("**验证约定**", text)
        for kw in ("前台", "后台任务", "空轮询", "重试/续跑场景"):
            self.assertIn(kw, text, kw)


class ReadonlyWriteToolGuidanceTest(unittest.TestCase):
    """只读角色写文件引导（#120 教训）：提示词明确用 Write/fs 工具，不用 Bash/python 脚本"""

    def test_t1_readonly_prompts_advise_write_tool(self):
        wd = Path("/tmp/req161-wd")
        # build_prompt 首句就是 ensure_dev_worktrees：不隔离会真建 worktree + 真建分支（#419 清理现场）
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            for role in ("analysis", "proposal", "tech"):
                prompt = d.build_prompt({"id": 161, "project": "req-board", "title": "T",
                                         "notes": "N", "mode": "semi"}, role)
                self.assertIn("一律用 Write/fs 工具写入", prompt, role)
                self.assertIn("不要用 Bash/python 脚本写文件", prompt, role)
                self.assertIn("回读校验", prompt, role)

    def test_t2_dev_prompt_keeps_json_dumps_guidance(self):
        """dev 不受只读 Write 约束影响，仍按 json.dumps 脚本写 result.json"""
        req = {"id": 161, "project": "req-board", "title": "T", "mode": "auto", "notes": "N"}
        wd = Path("/tmp/req161-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(req, "dev")
        self.assertIn("python json.dumps", prompt)
        self.assertNotIn("一律用 Write/fs 工具写入", prompt)

    def test_t3_readonly_tool_guide_mentions_write(self):
        self.assertIn("用 Write/fs 工具", d.READONLY_TOOL_GUIDE)


class BrowserVerificationRuleTest(unittest.TestCase):
    """#375 教训：浏览器端改动 curl/单测/语法检查不算浏览器级验证。
    dev prompt 要求 self-test.md 单列「浏览器验证」小节（无手段时 todos 声明人工验收）；
    review prompt 加浏览器一致性核对项。
    #433：AGENTS.md 约定扩至 dev 与 accept（含降级出口/未覆盖项），docs/dispatcher.md 同步 accept 契约。"""

    def test_t1_dev_prompt_browser_rule(self):
        req = {"id": 376, "project": "req-board", "title": "T", "mode": "auto", "notes": "N"}
        wd = Path("/tmp/req376-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(req, "dev")
        # #379：dev 会话已注入浏览器工具 → 浏览器端改动必须真开浏览器（保留兜底文案）
        for kw in ("浏览器验证", "不构成浏览器级验证", "未做，需人工验收",
                   "必须真开浏览器", "browser_navigate", "console 无报错"):
            self.assertIn(kw, prompt, kw)

    def test_t2_review_prompt_browser_consistency(self):
        """评审角色的浏览器一致性要求：#423 合并后唯一评审角色 = code_review，两条内置流程都走它"""
        wd = Path("/tmp/req376-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"), \
                mock.patch.object(d, "_gen_review_diff", lambda r: None):
            prompt = d.build_prompt({"id": 376, "project": "req-board", "title": "T",
                                     "notes": "N", "mode": "auto"}, "code_review")
        self.assertIn("浏览器一致性", prompt)
        self.assertIn("浏览器验证", prompt)
        self.assertIn("不构成浏览器级验证", prompt)

    def test_t3_agents_md_browser_rule(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("浏览器级验证约定", text)
        self.assertIn("不构成", text)
        # #379：dev 会话已放行浏览器工具前缀（实际工具取决于本机 MCP 注入，未注入时用项目自带驱动）
        self.assertIn("已放行浏览器工具前缀", text)
        self.assertIn("playwright MCP", text)
        self.assertIn("必须", text)
        # #433：约定扩到 dev 与 accept；含降级出口与 accept.md 未覆盖项
        self.assertIn("dev 与 accept 同样适用", text)
        self.assertIn("降级出口", text)
        self.assertIn("未覆盖项", text)

    def test_t4_dispatcher_md_accept_contract(self):
        """#433：docs/dispatcher.md 同步 accept 验证方式契约（判据/端口/降级/清理/自证）"""
        text = (REPO / "docs/dispatcher.md").read_text(encoding="utf-8")
        self.assertIn("accept 验证方式契约", text)
        self.assertIn("scripts/test_instance.sh", text)


if __name__ == "__main__":
    unittest.main()
