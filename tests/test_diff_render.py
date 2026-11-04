#!/usr/bin/env python3
"""#381 页面查看 dev 分支 diff：renderDiff 纯函数回归测试（node harness）。

从 static/board.js 提取真实 renderDiff 源码（#33 外置），交给 node 执行断言
（tests/diff_harness.js），覆盖：
  1. 类 GitHub 结构：diff --git 文件头（b 侧路径）/ 仓库头（=====）/ 截断说明 note /
     @@ hunk 头 / + 绿 - 红 / 空格上下文 / --- +++ index 等元数据中性行；
  2. XSS 基线：<script>/<img onerror> 等 diff 内容先转义再渲染（验收 3 核心断言）；
  3. 行号推进：@@ -a,b +c,d @@ 后上下文/删/增行的新旧行号列正确；
  4. 边界：空输入 / null / 随机文本行不崩溃。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_diff_render -v
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_js, frontend_src

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "diff_harness.js"

NEEDED_FUNCS = ("renderDiff",)


# ---------- 从 static/board.js 提取纯函数源码（花括号配平，跳过字符串/模板/注释/正则） ----------
def _prev_char(src, i):
    j = i - 1
    while j >= 0 and src[j].isspace():
        j -= 1
    return src[j] if j >= 0 else ""


def _skip_expr(src, i):
    """跳过模板字面量中 ${...} 表达式，返回匹配 } 之后的下标。"""
    depth, n = 1, len(src)
    while i < n:
        c = src[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        elif c in ('"', "'"):
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
        elif c == "`":
            i = _skip_tpl(src, i + 1)
        else:
            i += 1
    raise ValueError("unbalanced ${ expression")


def _skip_tpl(src, i):
    """跳过模板字面量，返回闭反引号之后的下标。"""
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            i = _skip_expr(src, i + 2)
            continue
        i += 1
    raise ValueError("unterminated template literal")


def _extract_function(js, name):
    """提取 static/board.js 内 function <name>(...) { ... } 的完整源码文本。"""
    start = js.find("function %s(" % name)
    if start < 0:
        raise AssertionError("function %s not found in static/board.js" % name)
    i = js.index("{", start)
    depth, n = 0, len(js)
    while i < n:
        c = js[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return js[start:i]
        elif c == "/":
            nxt = js[i + 1] if i + 1 < n else ""
            if nxt == "/":  # 行注释
                j = js.find("\n", i)
                i = n if j < 0 else j + 1
            elif nxt == "*":  # 块注释
                j = js.find("*/", i + 2)
                i = n if j < 0 else j + 2
            else:  # 正则字面量（启发式：前一个有效字符不是标识符/数字/右括号时视为正则）
                p = _prev_char(js, i)
                if p and p not in ")]}" and not (p.isalnum() or p in "_$"):
                    j, in_cls = i + 1, False
                    while j < n:
                        ch = js[j]
                        if ch == "\\":
                            j += 2
                            continue
                        if ch == "[":
                            in_cls = True
                        elif ch == "]":
                            in_cls = False
                        elif ch == "/" and not in_cls:
                            break
                        j += 1
                    i = j + 1
                else:
                    i += 1
        elif c in ('"', "'"):
            q = c
            j = i + 1
            while j < n:
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == q:
                    break
                j += 1
            i = j + 1
        elif c == "`":
            i = _skip_tpl(js, i + 1)
        else:
            i += 1
    raise ValueError("unbalanced braces for function %s" % name)


def _extract_functions():
    js = frontend_js()
    return "\n\n".join(
        _extract_function(js, name)
        for name in NEEDED_FUNCS
    )


class DiffRenderTest(unittest.TestCase):
    """renderDiff 纯函数 node 断言回归测试"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 renderDiff 回归测试")

    def _run_harness(self):
        funcs = _extract_functions()
        harness = HARNESS_JS.read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile(suffix=".js", mode="w", encoding="utf-8",
                                         delete=False) as f:
            f.write(funcs + "\n\n" + harness)
            tmp = f.name
        try:
            proc = subprocess.run([self.node, tmp], capture_output=True, text=True, timeout=60)
        finally:
            Path(tmp).unlink(missing_ok=True)
        if proc.returncode != 0:
            self.fail("node harness 失败: %s\n%s" % (proc.stderr, proc.stdout))
        return json.loads(proc.stdout)

    def test_t1_all_checks_pass(self):
        """D1-D16：结构/行号/XSS/边界 全部断言通过"""
        data = self._run_harness()
        checks = data.get("checks", [])
        self.assertGreaterEqual(len(checks), 16, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)

    def test_t2_wiring_present(self):
        """防回归：renderDiff 定义存在；loadArtifactContent 按 filename 后缀分发 .patch 分支"""
        src = frontend_src()
        self.assertIn("function renderDiff(src)", src)
        self.assertIn("const isPatch = /\\.patch$/i.test(art.filename);", src)
        self.assertIn("isPatch ? renderDiff(await res.text()) : renderArtifactMd(await res.text())", src)


if __name__ == "__main__":
    unittest.main()
