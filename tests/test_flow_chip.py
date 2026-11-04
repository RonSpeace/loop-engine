#!/usr/bin/env python3
"""#419 流程 chip：flowChipHtml 标签契约回归测试（node harness）。

来源：2026-09-18 走查发现「简单/标准/复杂 三个流程 chip 同为绿色」——根因是该函数
**标签**读卡上流程快照、**颜色**仍按历史字段 mode 取值（新卡 mode 为空 → 一律 chip-auto），
而 #419 的三关验证（dev 自测 / 代码审查 / 验收）都只断言了标签文本、颜色语义从未被定义。
本用例先把**已经被定义过**的部分锁住，避免后续改配色时把标签逻辑一起改坏：

  1. 标签优先级：flow_name 快照 > /api/meta 注入的 FLOW_LABEL[flow] > 原文 flow id >
     历史字段 mode 兜底（MODE_LABEL[mode] > 原文 mode）；
  2. 无任何标签 → 返回空串，不渲染 chip；
  3. label 经真实 escapeHtml（DOM 桩）出口，含 `<`/`&` 的标签被转义；
  4. chip 结构与 class 命名空间（`meta-chip chip-flow-<color>`，色名取自 /api/meta 注入）：
     只锁形状与命名空间、不点名具体色名——三档分色与色值由 tests/test_flow_color.py 锁定，
     本文件按原计划在 harness 里补了 D14「三档 class 互不相同」（#428 配色落地）。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_flow_chip -v
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "flow_chip_harness.js"

# escapeHtml 一并提取：label 走的是真实转义函数（不做断言桩，避免「测自己的桩」）
NEEDED_FUNCS = ("escapeHtml", "flowChipHtml")


def _script_block():
    """#33：主脚本已外置 static/board.js，直接读该文件（模板里只剩 skinBoot/配置脚本/外链）。"""
    return frontend_js()


def _extract_source():
    """提取纯函数源码（与 test_process_markdown_merge 同款）"""
    js = _script_block()
    return "\n\n".join(_extract_function(js, name) for name in NEEDED_FUNCS)


class FlowChipContractTest(unittest.TestCase):
    """流程 chip 标签契约（node 断言）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过流程 chip 标签契约测试")

    def _run_harness(self):
        source = _extract_source()
        harness = HARNESS_JS.read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(source + "\n\n" + harness)
            path = f.name
        try:
            proc = subprocess.run([self.node, path], capture_output=True, text=True, timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        if proc.returncode != 0:
            self.fail("node harness 失败: %s\n%s" % (proc.stderr, proc.stdout))
        return json.loads(proc.stdout)

    def test_t1_all_checks_pass(self):
        """D1-D14：标签优先级 / mode 兜底 / 空标签 / 结构取值域 / 转义 / 三档分色 全部通过"""
        data = self._run_harness()
        checks = data.get("checks", [])
        self.assertGreaterEqual(len(checks), 13, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)

    def test_t2_wiring_present(self):
        """防回归：chip 函数存在、label 经 escapeHtml、卡面 meta 行按 i 调用、配色走 chip-flow-* 命名空间"""
        src = frontend_src()
        self.assertIn("function flowChipHtml(i)", src)
        self.assertIn("escapeHtml(label)", src)
        self.assertIn("${flowChipHtml(i)}", src)
        self.assertIn(".chip-flow-", src)   # CSS 规则命名空间；旧 chip-auto/chip-semi 的删除由 test_flow_color.B3 全源码级锁定


if __name__ == "__main__":
    unittest.main()
