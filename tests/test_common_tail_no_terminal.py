#!/usr/bin/env python3
"""req-293 COMMON_TAIL 无交互终端说明测试。

改动：dispatcher.py COMMON_TAIL 新增第 12 条——本环境是无头 ACP 会话、没有交互终端，
禁止 AskUserQuestion/提问类工具（调用后立即返回未作答 dismiss）；需要用户决策或缺少
关键信息时结束回合并在 result.json 写 status: "need_confirm" + question（带倾向建议），
调度器转飞书提问并等待回复。一处改动经 {common} 注入六角色（analysis/proposal/tech/
dev_auto/dev_semi/accept）渲染后的 prompt；本测试逐角色断言关键文案。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899（#343）

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


def fake_req(rid=293, title="T", mode=None):
    req = {"id": rid, "project": "req-board", "title": title}
    if mode:
        req["mode"] = mode
    return req


class PromptNoTerminalGuidanceTest(unittest.TestCase):
    """六角色渲染后的 prompt 均含无交互终端说明：禁止提问工具、问题走 need_confirm（#293）"""

    KEYWORDS = ["无头 ACP 会话", "没有交互终端", "AskUserQuestion", "提问类工具",
                "dismiss", "不要使用", "need_confirm", "带倾向建议"]

    def render(self, role, mode=None):
        req = fake_req(mode=mode)
        wd = Path("/tmp/req293-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            return d.build_prompt(req, role)

    def assert_guidance(self, prompt):
        for kw in self.KEYWORDS:
            self.assertIn(kw, prompt)

    def test_common_tail_renders_no_terminal(self):
        """#42 静态化：COMMON_TAIL 常量直接含无交互终端说明（不再 format）"""
        self.assert_guidance(d.COMMON_TAIL)

    def test_analysis_prompt(self):
        self.assert_guidance(self.render("analysis"))

    def test_proposal_prompt(self):
        self.assert_guidance(self.render("proposal"))

    def test_tech_prompt(self):
        self.assert_guidance(self.render("tech"))

    def test_dev_auto_prompt(self):
        self.assert_guidance(self.render("dev", mode="auto"))

    def test_dev_semi_prompt(self):
        self.assert_guidance(self.render("dev", mode="semi"))

    def test_accept_prompt(self):
        self.assert_guidance(self.render("accept"))


if __name__ == "__main__":
    unittest.main()
