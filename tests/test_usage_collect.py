#!/usr/bin/env python3
"""req-103 /usage 用量解析测试：parse_usage 三值（X/Y/Z token 数与百分比）纯函数 L0 单测。

背景：kimi 0.33.0 起 /usage 输出格式从 `Context: X / Y (Z%)` 变为
`Context: X / Y tokens (Z%)`（多了 tokens 字样），旧固定正则失配导致
collect_usage 返回 None、sessions 记录缺 context。req-98 用 [^()]* 兼容新旧两格式；
req-103 扩展捕获 X/Y 两值落库（agent_runs），parse_usage_percent 保持旧接口兼容。

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试 setUpModule 自动补一份。
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


class L0UsageParse(unittest.TestCase):
    """parse_usage 三值解析新旧格式匹配与失配兜底（T1-T5）"""

    # ---- T1：kimi 0.33.0 新格式（带 tokens 字样）----
    def test_t1_new_format_tokens(self):
        samples = [
            ("Context: 60,000 / 600,000 tokens (10%)",
             {"context_tokens": 60000, "max_tokens": 600000, "context_pct": "10%"}),
            ("Context: 123 / 456 tokens (0.5%)",
             {"context_tokens": 123, "max_tokens": 456, "context_pct": "0.5%"}),
            ("Context: 1,234,567 / 9,999,999 tokens (12.34%)",
             {"context_tokens": 1234567, "max_tokens": 9999999, "context_pct": "12.34%"}),
        ]
        for text, expect in samples:
            self.assertEqual(d.parse_usage(text), expect, text)

    # ---- T2：旧格式（无 tokens 字样）不回归 ----
    def test_t2_old_format(self):
        samples = [
            ("Context: 60,000 / 600,000 (10%)",
             {"context_tokens": 60000, "max_tokens": 600000, "context_pct": "10%"}),
            ("Context: 123 / 456 (0.5%)",
             {"context_tokens": 123, "max_tokens": 456, "context_pct": "0.5%"}),
        ]
        for text, expect in samples:
            self.assertEqual(d.parse_usage(text), expect, text)

    # ---- T3：文本包裹（_capture 可能混入命令回显等多余文本） ----
    def test_t3_embedded_text(self):
        self.assertEqual(
            d.parse_usage("usage info\nContext: 60,000 / 600,000 tokens (10%)\n\nok"),
            {"context_tokens": 60000, "max_tokens": 600000, "context_pct": "10%"})
        self.assertEqual(
            d.parse_usage("some prefix Context: 100 / 200 (3%) suffix"),
            {"context_tokens": 100, "max_tokens": 200, "context_pct": "3%"})

    # ---- T4：失配返回 None（保持静默兜底，不阻断会话流程） ----
    def test_t4_mismatch_none(self):
        for text in ("", "no usage here",
                     "Context: 60,000 / 600,000 tokens",
                     "Context: a / b (10%)",
                     "Context: 60,000 / 600,000 (abc%)"):
            self.assertIsNone(d.parse_usage(text), text)

    # ---- T5：旧接口 parse_usage_percent 兼容（sessions context 字段依赖，只返回百分比） ----
    def test_t5_percent_compat(self):
        self.assertEqual(d.parse_usage_percent("Context: 60,000 / 600,000 tokens (10%)"), "10%")
        self.assertEqual(d.parse_usage_percent("Context: 123 / 456 (0.5%)"), "0.5%")
        self.assertIsNone(d.parse_usage_percent("no usage here"))


if __name__ == "__main__":
    unittest.main()
