#!/usr/bin/env python3
"""页面渲染后内嵌 JS 语法检查（防 #272 类回归）。

首页内嵌 JS（skinBoot/配置脚本/冻结脚本）仍在 templates/board.html；主脚本 #33 起外置在 static/board.js。
本测试用 Flask test client 渲染真实页面，对响应内联 <script> 与 static/board.js 文件字节分别做 node --check，
只做语法解析、不执行，不依赖浏览器 DOM。

运行：
    .venv/bin/python -m unittest tests.test_served_js_syntax -v
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402


class ServedJsSyntaxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过页面 JS 语法检查")

    def _check_js(self, js, label):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", encoding="utf-8", delete=False
        ) as f:
            f.write(js)
            tmp = f.name
        try:
            proc = subprocess.run(
                [self.node, "--check", tmp],
                capture_output=True, text=True, timeout=60,
            )
        finally:
            Path(tmp).unlink(missing_ok=True)
        self.assertEqual(
            proc.returncode, 0,
            f"{label} 渲染后的页面 JS 语法错误:\n{proc.stderr}",
        )

    def test_index_rendered_js_passes_node_check(self):
        resp = b.app.test_client().get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertGreaterEqual(len(blocks), 1, "首页应有内联 <script>")
        for js in blocks:
            self._check_js(js, "首页")

    def test_external_board_js_passes_node_check(self):
        """#33 外置主脚本：static/board.js 文件字节语法检查（浏览器直接执行的那份）"""
        js = (REPO / "static" / "board.js").read_text(encoding="utf-8")
        self._check_js(js, "static/board.js")


if __name__ == "__main__":
    unittest.main()
