# -*- coding: utf-8 -*-
"""工具调用摘要（ACPSession._tool_summary）测试（2026-09-17）。

背景：摘要原先优先采用 kimi 的 title（「动作 + 前 ~51 字符参数」），参数一长就只剩路径前缀——
现场连续 20 余行 Tool 事件全显示成 `Running: cd /Users/…/de…`，不同命令看起来完全一样。
现改为**优先用 rawInput 的结构化完整参数自建摘要**：Bash 剥离 `cd` 前缀、路径相对
worktree/产物目录折叠、动作词沿用 title 前缀；取不到结构化参数时逐级退回 title。

验证点：
- T1  Bash：剥离 cd 前缀，显示真正的命令
- T2  Bash：连续多个 cd 全剥离；带引号路径也剥离
- T3  Bash：无 cd 前缀时原样保留
- T4  路径折叠：worktree 内绝对路径 → 相对路径
- T5  路径折叠：产物目录 → `<产物>/x`；主工作区 → `<主工作区>/x`
- T6  长前缀优先替换（worktree 嵌套在主工作区内时不被短根误截）
- T7  退回：无 rawInput（MCP 工具等）→ 原样 title
- T8  兜底：无 title 无 rawInput → 「工具调用」
- T9  locations[0].path 兜底
- T10 pattern 字段（Searching）
- T11 超长：120 字符截断
- T12 桩会话（无 workdir/artifact_root 属性）不崩
- T13 极端：命令剥完为空时保留原文本，不产出空摘要
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import acp_shared as ac  # noqa: E402

WT = "/Users/x/.req-board/worktrees/req-board/dev/req-419/req-board"
ART = "/Users/x/.req-board/artifacts/req-board/req-419"
MAIN = "/Users/x/project/req-board"


def _stub(workdir=WT, artifact=ART, main_ws=None):
    """ACPSession 桩：只备摘要所需的路径上下文（__new__ 绕过 __init__，不起 kimi 进程）"""
    s = ac.ACPSession.__new__(ac.ACPSession)
    s.workdir = Path(workdir)
    s.artifact_root = Path(artifact)
    s.main_ws_roots = [Path(p) for p in (main_ws or [])] or None
    return s


class ToolSummaryTest(unittest.TestCase):
    def setUp(self):
        self.s = _stub()

    def _sum(self, title, raw=None, **extra):
        upd = {"title": title}
        if raw is not None:
            upd["rawInput"] = raw
        upd.update(extra)
        return self.s._tool_summary(upd)

    # ---- Bash：cd 前缀 ----

    def test_t1_strips_cd_prefix(self):
        """T1：`cd <worktree> && <真命令>` → 只留真命令（现场 20 行同款的根因）"""
        cmd = f"cd {WT} && python3 -m unittest discover -s tests -p test_flow_config.py"
        self.assertEqual(
            self._sum("Running: cd /Users/dev/.req-board/worktrees/req-board/de…", {"command": cmd}),
            "Running: python3 -m unittest discover -s tests -p test_flow_config.py")

    def test_t2_multiple_and_quoted_cd(self):
        """T2：连续多个 cd 全剥离；带引号路径同样剥离"""
        cmd = f'cd {WT} && cd "/tmp/my dir" && grep -rn "foo" .'
        self.assertEqual(self._sum("Running: x", {"command": cmd}),
                         'Running: grep -rn "foo" .')
        cmd2 = f"cd {WT}; ls -la"
        self.assertEqual(self._sum("Running: x", {"command": cmd2}), "Running: ls -la")

    def test_t3_no_cd_kept(self):
        """T3：无 cd 前缀 → 原样"""
        self.assertEqual(self._sum("Running: x", {"command": "git status --short"}),
                         "Running: git status --short")

    # ---- 路径折叠 ----

    def test_t4_worktree_path_folded(self):
        """T4：worktree 内绝对路径 → 相对路径"""
        self.assertEqual(
            self._sum(f"Editing {WT}/acp_shared.py", {"file_path": f"{WT}/acp_shared.py"}),
            "Editing: acp_shared.py")
        self.assertEqual(
            self._sum(f"Reading {WT}/board.py", {"path": f"{WT}/board.py"}),
            "Reading: board.py")

    def test_t5_artifact_and_main_ws_folded(self):
        """T5：产物目录 → `<产物>/x`；主工作区 → `<主工作区>/x`"""
        self.assertEqual(
            self._sum("Running: cat x", {"command": f"cat {ART}/analysis.md"}),
            "Running: cat <产物>/analysis.md")
        s = _stub(main_ws=[MAIN])
        self.assertEqual(
            s._tool_summary({"title": "Running: x",
                             "rawInput": {"command": f"sed -n 1,20p {MAIN}/board.py"}}),
            "Running: sed -n 1,20p <主工作区>/board.py")

    def test_t6_longest_prefix_wins(self):
        """T6：worktree 嵌套在主工作区内时，长前缀先替换（不被短根截成 `<主工作区>/req-board/x`）"""
        s = _stub(workdir=f"{MAIN}", main_ws=["/Users/x/project"])
        self.assertEqual(
            s._tool_summary({"title": "Reading x",
                             "rawInput": {"file_path": f"{MAIN}/board.py"}}),
            "Reading: board.py")

    # ---- 退回与兜底 ----

    def test_t7_falls_back_to_title(self):
        """T7：无 rawInput（MCP 工具等）→ 原样 title，行为与改前一致"""
        self.assertEqual(self._sum("mcp__reqboard-tools__list_dir"), "mcp__reqboard-tools__list_dir")
        self.assertEqual(self._sum("Launching coder agent: board.py 状态收缩"),
                         "Launching coder agent: board.py 状态收缩")

    def test_t8_default_title(self):
        """T8：无 title 无 rawInput → 「工具调用」"""
        self.assertEqual(self.s._tool_summary({}), "工具调用")

    def test_t9_locations_fallback(self):
        """T9：无 rawInput 时用 locations[0].path（并折叠）"""
        self.assertEqual(
            self.s._tool_summary({"title": "Reading x", "locations": [{"path": f"{WT}/board.py"}]}),
            "Reading: board.py")

    def test_t10_pattern_field(self):
        """T10：Searching 取 pattern 字段"""
        self.assertEqual(
            self._sum("Searching for 'SEMI_FLOWS|ROLE_CAPABILITIES|M…'",
                      {"pattern": "SEMI_FLOWS|ROLE_CAPABILITIES"}),
            "Searching: SEMI_FLOWS|ROLE_CAPABILITIES")

    def test_t11_truncates_at_120(self):
        """T11：超长 detail 截到 117 + '...'（自建摘要的预算是 120，非 kimi 的 ~60）"""
        cmd = "echo " + "x" * 200
        out = self._sum("Running: x", {"command": cmd})
        self.assertEqual(out, "Running: " + cmd[:117] + "...")
        self.assertEqual(len(out), len("Running: ") + 120)

    def test_t12_stub_without_paths(self):
        """T12：桩会话缺 workdir/artifact_root → 不崩、路径原样"""
        s = ac.ACPSession.__new__(ac.ACPSession)
        self.assertEqual(s._tool_summary({"title": "Editing x",
                                          "rawInput": {"file_path": "/a/b/c.py"}}),
                         "Editing: /a/b/c.py")

    def test_t13_only_cd_keeps_text(self):
        """T13：命令剥完为空 → 保留原文本，不产出 `Running: ` 空摘要"""
        out = self._sum("Running: x", {"command": f"cd {WT} && "})
        self.assertIn("cd", out)


if __name__ == "__main__":
    unittest.main()
