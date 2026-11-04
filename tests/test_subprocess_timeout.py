#!/usr/bin/env python3
"""req-412 T14：dispatcher.py 全部 subprocess.run 调用必须带 timeout（静态扫描防回归）。

背景：#410 高嫌疑点 C——收尾路径 git 挂起会无限期卡住收尾线程；#412 已全量补 timeout。
扫描策略：AST 遍历每个 `subprocess.run(...)` 调用节点，断言关键字含 `timeout=`。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_subprocess_timeout -v
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests._dispatcher_src import dispatcher_source_files  # noqa: E402


class SubprocessTimeoutScanTest(unittest.TestCase):
    """T14：subprocess.run 全量 timeout 静态扫描"""

    def _run_calls(self):
        calls = []
        for path in dispatcher_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr == "run"):
                    has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                    calls.append((path, node.lineno, has_timeout))
        return calls

    def test_all_subprocess_run_have_timeout(self):
        calls = self._run_calls()
        self.assertGreater(len(calls), 20,
                           f"扫描到的 subprocess.run 调用偏少（{len(calls)}），疑似解析异常")
        lacks = [f"{p.name}:{ln}" for p, ln, has_timeout in calls if not has_timeout]
        self.assertEqual(lacks, [],
                         f"以下 subprocess.run 调用缺 timeout（行 {lacks}）——"
                         f"收尾路径可能被 git 挂起无限期卡住（#412 验收③）")

    def test_scan_findings_in_source(self):
        """旁证：AST 行号都能在源码中定位到 subprocess.run 字样"""
        for path, ln, _ in self._run_calls():
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertIn("subprocess.run(", lines[ln - 1], f"{path.name} 行 {ln} 定位不一致")


if __name__ == "__main__":
    unittest.main()
