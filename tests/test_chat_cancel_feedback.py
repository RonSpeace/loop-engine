#!/usr/bin/env python3
"""req-332 回归测试：聊天建卡页取消无反馈（refreshChat 误判会话 error 字段）。

根因：refreshChat 用 `if (st.error)` 判断接口错误，但 GET /api/chat/<id> 返回整行
chat_sessions，取消/超时中断时 error 列写入中断原因（「用户取消本轮」「本轮超时」等），
导致 200 响应被误判为接口错误提前 return，chatState 永远停留 running，按钮卡「取消」。

改动（前端 JS 三函数，现位于 static/board.js）：
  1. refreshChat 改为按 HTTP 状态（!stRes.ok）判断接口错误；200 会话行即使 error 列
     有值也正常合并状态与渲染；404 时从响应体解析错误文案显示。
  2. renderChatStatus 支持可选文案参数（textOverride），真实接口错误/一次性提示以红色状态卡片
     直显（#430 起状态行是共享卡片：与调度器会话/需求助手同款）。
  3. cancelChat POST 成功后乐观更新本地 chatState 为 interrupted（user_cancel）并立即
     渲染，再 refresh 对账；fetch 加 try/catch，失败弹 toast。

本测试从 static/board.js 提取真实函数源码（花括号配平提取器与
test_process_markdown_merge.py 同源），交给 node harness
（tests/chat_cancel_feedback_harness.js，mock DOM/fetch）逐场景断言：
  A. 200 带 error 字段（取消中断原因）不判错：状态正常合并、按钮/状态行更新；
  B. 404 真实接口错误：状态行显示「会话不存在」、不污染 chatState；
  C. renderChatStatus override 文案直显为红色卡片、无参时按状态渲染（interrupted/running 文案复用）；
  D. 取消乐观更新：按钮立即渲染、状态行立即中断文案、refresh 对账保留 user_cancel；
  E. 取消失败（HTTP 500 / 网络异常）弹 toast 不抛错；
  F. 超时中断（非 user_cancel）同样不再卡按钮；
  G. 200 running 正常轮询路径不回归。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_chat_cancel_feedback -v
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_js, frontend_src

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "chat_cancel_feedback_harness.js"

NEEDED_FUNCS = ("refreshChat", "renderChatStatus", "cancelChat",
                "selectionGuardActive", "nextChatActivity", "mergeChatStreamEvents",
                # #430：状态行改为共享卡片后，renderChatStatus 依赖的真实实现（渲染走真身、不另写桩，
                # 保证 harness 断言的是线上同一套代码）
                "reqChatEventMs", "chatRoundSec", "chatAddActivityModel",
                "chatStatusCardHtml", "applyChatStatusCard", "setStatusNoInput")


# ---------- 从 static/board.js 提取纯函数源码（与 test_process_markdown_merge.py 同源） ----------
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
    """提取 static/board.js 内 (async )?function <name>(...) { ... } 的完整源码文本。"""
    start = js.find("function %s(" % name)
    if start < 0:
        raise AssertionError("function %s not found in static/board.js" % name)
    if start >= 6 and js[start - 6:start] == "async ":  # async function 保留 async 前缀
        start -= 6
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
            else:  # 正则字面量（启发式）
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


class ChatCancelFeedbackTest(unittest.TestCase):
    """#332 聊天建卡取消反馈 node harness 回归测试"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 JS 回归测试")

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

    def test_t1_all_js_checks_pass(self):
        """验收标准全场景：harness 全部断言通过（A-G 共 17 条 + H 重复气泡加固 6 条 = 23 条）"""
        data = self._run_harness()
        checks = data.get("checks", [])
        self.assertGreaterEqual(len(checks), 23, "harness 断言数不足: %d" % len(checks))
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)

    def test_t2_refresh_chat_http_status_wiring(self):
        """防回归：#332 根因修复点——按 HTTP 状态判断接口错误，不再用会话 error 列"""
        src = frontend_src()
        # 新判定就位：非 2xx 才判接口错误，且从响应体解析错误文案显示
        self.assertIn("if (!stRes.ok) {", src)
        self.assertIn("renderChatStatus(err.error || `请求失败（${stRes.status}）`);", src)
        # 旧误判消失：不再以会话行 error 列提前 return（200 带 error 的中断原因正常更新状态）
        self.assertNotIn("if (st.error) { renderChatStatus(st.error); return; }", src)
        # 200 会话行仍照常合并（即使带 error 列）
        self.assertIn("chatState = {\n                    ...chatState,\n                    ...st,", src)

    def test_t3_render_chat_status_override_wiring(self):
        """防回归：renderChatStatus 支持可选文案参数，override 走共享状态卡片（红卡、无判定依据）"""
        src = frontend_src()
        self.assertIn("function renderChatStatus(textOverride) {", src)
        # override 作为模型入参进 chatAddActivityModel，由共享卡片渲染（#430 起不再是 innerHTML 直塞纯文本）
        self.assertIn("chatAddActivityModel(chatState, chatActionPhase, textOverride, chatEvents, Date.now())", src)
        js = frontend_js()
        model = _extract_function(js, "chatAddActivityModel")
        self.assertIn("if (override) {", model)
        self.assertIn("mk('override', 'red', String(override)", model)

    def test_t4_cancel_optimistic_wiring(self):
        """防回归：cancelChat 乐观更新 interrupted + try/catch toast"""
        src = frontend_src()
        self.assertIn("chatState = {...chatState, status: 'interrupted', interrupt_source: 'user_cancel'};", src)
        self.assertIn("showToast('取消失败: 网络异常', true);", src)


if __name__ == "__main__":
    unittest.main()
