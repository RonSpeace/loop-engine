#!/usr/bin/env python3
"""C6 工具输出限幅：单次输出总长上限 + 截断提示与续读方式。

- cap_tool_output：超 TOOL_OUTPUT_MAX_CHARS 截断并说明总长与缩小范围的方式；未超原样返回。
- read_file（只读 MCP）/ file_read（看板助手）：未读完时末尾给「继续读用 offset=N」；读到 EOF 不给。
- git_status：超行数上限时显式标注截断（原为静默截断）。
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402


class CapToolOutput(unittest.TestCase):

    def test_short_unchanged(self):
        self.assertEqual(ac.cap_tool_output("abc"), "abc")

    def test_long_truncated_with_hint(self):
        text = "x" * (ac.TOOL_OUTPUT_MAX_CHARS + 500)
        out = ac.cap_tool_output(text)
        self.assertLess(len(out), ac.TOOL_OUTPUT_MAX_CHARS + 200)
        self.assertIn("已截断", out)
        self.assertIn(str(len(text)), out)

    def test_non_str_passthrough(self):
        self.assertIsNone(ac.cap_tool_output(None))


class ReadFileContinuation(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="c6-"))
        (self.tmp / "f.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
        self.srv = ac.ReadonlyMcpServer.__new__(ac.ReadonlyMcpServer)
        self.srv.workdir = self.tmp
        self.srv.roots = [self.tmp]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_more_remaining_gives_next_offset(self):
        out = self.srv.tool_read_file({"path": "f.txt", "offset": 2, "max_lines": 3})
        self.assertIn("line3", out)
        self.assertIn("line5", out)
        self.assertNotIn("line6", out)
        self.assertIn("offset=5", out.splitlines()[-1])

    def test_eof_no_continuation(self):
        out = self.srv.tool_read_file({"path": "f.txt", "offset": 7, "max_lines": 10})
        self.assertIn("line10", out)
        self.assertNotIn("未读完", out)

    def test_exact_fit_no_continuation(self):
        out = self.srv.tool_read_file({"path": "f.txt", "offset": 0, "max_lines": 10})
        self.assertNotIn("未读完", out)


class ReadFileLineAwareCap(unittest.TestCase):
    """评审复修：read_file 按整行限幅——表头 lines 与续读 offset 必须对应实际返回的行"""

    def test_long_lines_capped_with_correct_offset(self):
        tmp = Path(tempfile.mkdtemp(prefix="c6-cap-"))
        try:
            (tmp / "wide.txt").write_text("\n".join(f"{i:05d}" + "y" * 95 for i in range(2000)) + "\n")
            srv = ac.ReadonlyMcpServer.__new__(ac.ReadonlyMcpServer)
            srv.workdir = tmp
            srv.roots = [tmp]
            out = srv.tool_read_file({"path": "wide.txt", "offset": 0, "max_lines": 2000})
            self.assertLessEqual(len(out), ac.TOOL_OUTPUT_MAX_CHARS)
            header, *rest = out.split("\n")
            n = int(header.split("lines=")[1].rstrip(")"))
            self.assertLess(n, 2000)
            self.assertIn(f"offset={n}", rest[-1])            # 续读点 = 实际返回行数
            self.assertTrue(rest[n - 1].startswith(f"{n - 1:05d}"))
            self.assertEqual(ac.cap_tool_output(out), out)    # 总长限幅不再二次截断
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class GitStatusTruncationMarked(unittest.TestCase):

    def test_marker_when_over_limit(self):
        tmp = Path(tempfile.mkdtemp(prefix="c6-git-"))
        try:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            for i in range(ac.GIT_STATUS_MAX_LINES + 5):
                (tmp / f"f{i}.txt").write_text("x")
            srv = ac.ReadonlyMcpServer.__new__(ac.ReadonlyMcpServer)
            srv.workdir = tmp
            srv.roots = [tmp]
            out = srv.tool_git_status({})
            self.assertIn("已截断", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BoardAssistantFileRead(unittest.TestCase):

    def test_board_file_read_continuation(self):
        import board as b
        self.assertTrue(hasattr(b, "ro_read_continuation") or hasattr(ac, "ro_read_continuation"))
        self.assertEqual(ac.ro_read_continuation(5, 3, True), "……（未读完：继续读用 offset=8）")
        self.assertEqual(ac.ro_read_continuation(5, 3, False), "")


if __name__ == "__main__":
    unittest.main()
