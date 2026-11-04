#!/usr/bin/env python3
"""COMMON_TAIL 第 13 条路径白名单边界说明测试。

改动：dispatcher.py COMMON_TAIL 新增第 13 条——MCP 只读工具可读路径 roots 共三段
（会话工作目录 / 项目真实仓库根即 main 工作区 / 产物目录），三段之外（如 worktree 父目录）
会被拒绝；只读角色 Bash 白名单仅两段；了解同项目其它需求 worktree/状态用 `git worktree list`
或 db_query。一处改动经 {common} 注入六角色渲染后的 prompt；本测试逐角色断言关键文案。

背景现场：req-293 dev 会话把「list_dir 列 worktree 父目录被拒」误归纳为「main 工作区读不了」，
实际 main 工作区绝对路径在 roots 内可读——提示词写清边界避免同类误判。

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


def fake_req(rid=294, title="T", mode=None):
    req = {"id": rid, "project": "req-board", "title": title}
    if mode:
        req["mode"] = mode
    return req


class PromptPathRootsGuidanceTest(unittest.TestCase):
    """六角色渲染后的 prompt 均含路径白名单三段说明（COMMON_TAIL 第 13 条）"""

    KEYWORDS = ["路径白名单", "main 工作区", "worktree 父目录",
                "git worktree list", "db_query"]

    def render(self, role, mode=None):
        req = fake_req(mode=mode)
        wd = Path("/tmp/req294-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            return d.build_prompt(req, role)

    def assert_guidance(self, prompt):
        for kw in self.KEYWORDS:
            self.assertIn(kw, prompt)

    def test_common_tail_renders_path_roots(self):
        """#42 静态化：COMMON_TAIL 常量直接含路径白名单三段说明（不再 format）"""
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
