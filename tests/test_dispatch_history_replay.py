#!/usr/bin/env python3
"""#51 调度历史回放（流程骨架 / 全部事件）回归测试。

A. 源码结构断言（Python 层）：
   - 顶部标题「调度历史回放」+ 视图切换「流程骨架（默认）/ 全部事件」+「全部展开/全部折叠」；
   - 切视图 / 切卡 / 刷新回默认收起（dispatcherView=skeleton、dispatcherStageOpen={}）；
   - 单阶段展开/收起与全部展开/折叠入口都存在；
   - 全部事件视图沿用改造前的行渲染器（fetch 端点与事件行模板不变，不丢事件）；
   - 新样式只经令牌取色（无裸色值），窄屏媒体查询存在。
B. node 渲染断言（tests/dispatch_history_harness.js）：
   从 static/board.js 提取真实 dispatchTsMs / formatDispatchDuration / buildDispatchStages
   源码执行，覆盖 heavy 全流程 8 阶段、打回重做聚合、统计条真实计数、边界与时长格式化。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m pytest -q tests/test_dispatch_history_replay.py
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css, frontend_js

HARNESS_JS = Path(__file__).resolve().parent / "dispatch_history_harness.js"
NEEDED_FUNCS = ("dispatchTsMs", "formatDispatchDuration", "buildDispatchStages")


# ---------- 从 static/board.js 提取纯函数源码（花括号配平，跳过字符串/模板/注释/正则） ----------
def _prev_char(src, i):
    j = i - 1
    while j >= 0 and src[j].isspace():
        j -= 1
    return src[j] if j >= 0 else ""


def _skip_expr(src, i):
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
            if nxt == "/":
                j = js.find("\n", i)
                i = n if j < 0 else j + 1
            elif nxt == "*":
                j = js.find("*/", i + 2)
                i = n if j < 0 else j + 2
            else:
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


class DispatchHistoryReplayTest(unittest.TestCase):
    """流程骨架 / 全部事件视图回归测试"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过调度历史回放 node 断言")

    def _run_harness(self):
        js = frontend_js()
        funcs = "\n\n".join(_extract_function(js, name) for name in NEEDED_FUNCS)
        with tempfile.NamedTemporaryFile(suffix=".js", mode="w", encoding="utf-8",
                                         delete=False) as f:
            f.write(funcs + "\n\n" + HARNESS_JS.read_text(encoding="utf-8"))
            tmp = f.name
        try:
            proc = subprocess.run([self.node, tmp], capture_output=True, text=True, timeout=60)
        finally:
            Path(tmp).unlink(missing_ok=True)
        if proc.returncode != 0:
            self.fail("node harness 失败: %s\n%s" % (proc.stderr, proc.stdout))
        return json.loads(proc.stdout)

    def test_a1_toolbar_structure(self):
        """顶部：标题 + 流程骨架/全部事件切换 + 全部展开/全部折叠；骨架默认视图"""
        src = frontend_js()
        self.assertIn("let dispatcherView = 'skeleton';", src)
        self.assertIn("调度历史回放", src)
        self.assertIn("tab('skeleton', '流程骨架')", src)
        self.assertIn("tab('events', '全部事件')", src)
        self.assertIn('id="dispExpandAll"', src)
        self.assertIn('id="dispCollapseAll"', src)
        self.assertIn("全部展开", src)
        self.assertIn("全部折叠", src)
        self.assertIn('id="dispatchStats"', src)

    def test_a2_default_collapsed_and_reset(self):
        """默认全部收起；切视图 / 切卡 / 换需求回到收起态"""
        src = frontend_js()
        m = re.search(r"function setDispatcherView\(view\) \{(.*?)\n        \}", src, re.S)
        self.assertIsNotNone(m, "setDispatcherView 未找到")
        self.assertIn("dispatcherStageOpen = {}", m.group(1),
                      "切视图必须重置展开态（全部收起）")
        m = re.search(r"function selectDispatcher\(id\) \{(.*?)\n        \}", src, re.S)
        self.assertIsNotNone(m, "selectDispatcher 未找到")
        self.assertIn("dispatcherView = 'skeleton';", m.group(1))
        self.assertIn("dispatcherStageOpen = {};", m.group(1))
        # 切卡即清缓存：请求未返回的窗口期不得沿用上一张卡的数据
        self.assertIn("dispatcherEventsCache = [];", m.group(1))
        self.assertIn("dispatcherEventsCacheReq = null;", m.group(1))
        self.assertIn("dispatcherStagesCache = [];", m.group(1))
        self.assertIn("if (dispatcherEventsCacheReq !== id) {", src)
        self.assertIn("dispatcherStageOpen = {};", src)

    def test_a3_expand_controls(self):
        """单阶段展开/收起 + 全部展开/折叠（一次作用于全部阶段）"""
        src = frontend_js()
        for fn in ("toggleDispatcherStage", "expandAllDispatcherStages",
                   "collapseAllDispatcherStages"):
            self.assertEqual(1, len(re.findall(r"\bfunction %s\(" % fn, src)),
                             "%s 应恰好声明一次" % fn)
        self.assertIn("dispatcherStageOpen[s.key] = !dispatcherStageOpen[s.key];", src)
        self.assertIn("dispatcherStagesCache.forEach(s => { if (s.events.length) dispatcherStageOpen[s.key] = true; });", src)
        self.assertIn("dispatcherStageOpen = {};\n            renderDispatcherBody();", src)
        self.assertIn("const allOpen = detailStages.length > 0 && detailStages.every", src)
        self.assertIn("const allClosed = detailStages.every", src)
        # 缓存未就绪/切卡窗口期：展开入口必须有当前卡缓存守卫
        for fn in ("toggleDispatcherStage", "expandAllDispatcherStages",
                   "collapseAllDispatcherStages"):
            body = re.search(r"function %s\([^)]*\) \{(.*?)\n        \}" % fn, src, re.S)
            self.assertIsNotNone(body, "%s 未找到" % fn)
            self.assertIn("dispatcherEventsCacheReq !== selectedId", body.group(1),
                          "%s 缺少当前卡缓存守卫" % fn)

    def test_a4_views_and_event_stream_unchanged(self):
        """全部事件视图保留原事件流；不新增/不改变既有接口"""
        src = frontend_js()
        self.assertIn("`/api/requirements/${id}/dispatcher-events`", src)
        self.assertIn("fetch(`/api/requirements/${id}/dispatcher-events`, {cache: 'no-store'})", src)
        # 事件行渲染器保留改造前的结构与全部入口
        self.assertIn("function dispatchEventRows(id, events, allEvents)", src)
        self.assertIn('class="ev-item ev-dispatch"', src)
        self.assertIn('class="ev-confirm-card"', src)
        self.assertIn("openSessionFromSid(${id},'${sid}')", src)
        self.assertIn("copyDispTime(${e.id})", src)
        self.assertIn("dispatcherView === 'skeleton'", src)
        self.assertIn(": (dispatchEventRows(dispatcherEventsCacheReq, dispatcherEventsCache, dispatcherEventsCache)", src)
        # 旧格式 confirm 角色补齐按全量事件 id 判定（不能按单阶段子集下标）
        self.assertIn("const scope = Array.isArray(allEvents) ? allEvents : events;", src)
        self.assertIn("lastConfirmId = e.id", src)
        self.assertIn("e.id === lastConfirmId", src)
        # 切卡后旧请求返回：丢弃过期响应
        self.assertIn("if (!selectedDispatcher || selectedId !== id) return;", src)

    def test_a5_skeleton_structure(self):
        """骨架行：状态圆点 / 时间 / 阶段名 / 耗时 / 箭头 / 徽标 / N 条"""
        src = frontend_js()
        for cls in ("disp-dot", "disp-stage-time", "disp-stage-name", "disp-stage-dur",
                    "disp-caret", "disp-badge", "disp-count", "disp-stage-detail"):
            self.assertIn('class="%s' % cls, src, "骨架结构缺少 .%s" % cls)
        self.assertIn("${s.events.length} 条", src)
        self.assertIn("s.tone = s.terminal ? 'pass'", src)
        self.assertIn("'重做' + s.redo + '轮'", src)
        self.assertIn("'打回' + s.reject + '轮'", src)
        self.assertIn("s.reviewPass ? '评审通过'", src)
        self.assertIn("s.done ? '完成'", src)
        # 收尾行也渲染耗时（不再硬编码跳过）
        self.assertIn('const dur = `<span class="disp-stage-dur">${formatDispatchDuration(s.durationMs)}</span>`;', src)
        self.assertNotIn("s.terminal ? '' : `<span class=\"disp-stage-dur\">", src)

    def test_a7_stats_semantics(self):
        """统计/阶段口径：重试排除终止留痕、人工裁决打回/放行纳入、普通完成不冒充评审通过"""
        src = frontend_js()
        self.assertIn("const reRetry = /\\d+\\s*次自动重试/;", src)
        self.assertIn("!/不自动重试/.test", src)
        self.assertIn("const reManualReject = /^人工裁决打回「(.+?)」再修一轮/;", src)
        self.assertIn("const reManualPass = /^人工裁决放行「(.+?)」/;", src)
        self.assertIn("reReject.test(c) || isManualReject(c)", src)
        self.assertIn("s.reviewPass = true;", src)
        self.assertIn("src.done = true;", src)
        self.assertIn("s.done = true;", src)

    def test_a6_css_tokens_and_narrow(self):
        """新样式只经令牌取色；窄屏媒体查询存在"""
        css = frontend_css()
        start = css.index("/* #51 调度历史回放")
        end = css.index(".artifact-body {", start)
        block = css[start:end]
        for cls in (".disp-titlebar", ".disp-tabs", ".disp-tab", ".disp-action",
                    ".disp-stats", ".disp-stage-row", ".disp-dot", ".disp-caret",
                    ".disp-badge", ".disp-count", ".disp-stage-detail"):
            self.assertIn(cls, block, "缺少样式 %s" % cls)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block),
                          "新样式不应出现裸色值（一并经令牌）")
        self.assertIn("@media (max-width: 760px)", block)
        self.assertIn("var(--success)", block)
        self.assertIn("var(--orange)", block)

    def test_b1_node_stage_checks(self):
        """D1-D5：heavy 全流程 / standard / 边界 / 人工裁决与重试口径 / 时长格式化全部通过"""
        data = self._run_harness()
        checks = data.get("checks", [])
        self.assertGreaterEqual(len(checks), 25, "node 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)


if __name__ == "__main__":
    unittest.main()
