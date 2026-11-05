#!/usr/bin/env python3
"""上下文编译器界面（卡片「上下文」页 / 注入预览 / 设置「提示词」「技能库」）前端回归。

静态（源码文本）：DOM 接线、设置抽屉五 tab 互斥、层色令牌两套皮肤、提示词/技能内容不进翻译层、接口路径。
node harness（tests/context_ui_harness.js）：从 static/board.js 提取真实常量与纯函数，断言分层分组/堆叠条缓存断点、
条目徽标与对比、警告去处、流程节点条、锁定句标记往返与即时校验、行级对比、技能矩阵循环与只读。
零副作用：不起服务、不建 worktree、不触 build_prompt / spawn_agent。
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css, frontend_js, frontend_text
from tests.test_process_markdown_merge import _extract_function

HARNESS_JS = Path(__file__).resolve().parent / "context_ui_harness.js"

CONSTS = ("CTX_LAYERS", "CTX_LAYER_ORDER", "CTX_STABLE_LAYERS", "CTX_KIND_LABEL", "CTX_STATUS",
          "PP_STATUS", "PP_MAX_CHARS", "PP_PH_RE", "SK_ACT", "SK_CYCLE", "SK_STATUS")
FUNCS = ("escapeHtml", "escapeAttr", "ctxFmt", "ctxLayerOf", "ctxShortHash", "ctxGroupItems", "ctxStableSum",
         "ctxStackBarHtml", "ctxLegendHtml", "ctxWarnTarget", "ctxWarningsHtml", "ctxOriginLabel", "ctxItemBadges",
         "ctxItemsHtml", "ctxStatsHtml", "ctxNodeList", "ctxRoleRounds", "promptGroups", "promptLockMarker",
         "promptToEditable", "promptFromEditable", "promptClientCheck", "promptLineDiff", "promptDiffHtml",
         "promptWhenText", "promptCheckHtml", "promptEditorHtml", "skillKey", "skillScopeLabel", "skillFilter",
         "skillRolesText", "skillLintTone", "skillListHtml", "skillCell", "skillCellNext", "skillMatrixHtml",
         "skillFormFrom", "skillDetailHtml")


def _js_const(js, name):
    """提取顶层 const <name> = ...;（对象/数组按括号配平，其余到行尾分号）。"""
    m = re.search(r"const %s = " % re.escape(name), js)
    if not m:
        raise AssertionError("const %s not found" % name)
    i = m.end()
    if js[i] in "{[":
        depth, k = 0, i
        while True:
            c = js[k]
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return js[m.start():k + 1] + ";"
            k += 1
    return js[m.start():js.index(";", i) + 1]


STUB = """
function makeEscDiv() {
    let text = '';
    return {
        set textContent(v) { text = String(v == null ? '' : v); },
        get innerHTML() { return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    };
}
global.document = {createElement: () => makeEscDiv()};
"""


def _harness_source():
    js = frontend_js()
    parts = [STUB] + [_js_const(js, n) for n in CONSTS] + [_extract_function(js, n) for n in FUNCS]
    return "\n\n".join(parts) + "\n\n" + HARNESS_JS.read_text(encoding="utf-8")


class ContextUiStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = frontend_text()
        cls.js = frontend_js()
        cls.css = frontend_css()

    def test_settings_tabs_wired(self):
        for token in ('id="sdTabPrompts"', 'id="sdTabSkills"', 'id="settingsPromptsBody" hidden',
                      'id="settingsSkillsBody" hidden', ">提示词</button>", ">技能库</button>"):
            self.assertIn(token, self.html)
        # 新 tab 在「通知渠道」之后
        self.assertLess(self.html.index('id="sdTabFeishu"'), self.html.index('id="sdTabPrompts"'))
        self.assertLess(self.html.index('id="sdTabPrompts"'), self.html.index('id="sdTabSkills"'))
        sw = _extract_function(self.js, "switchSettingsTab")
        for tab, body in (("prompts", "settingsPromptsBody"), ("skills", "settingsSkillsBody")):
            self.assertIn("document.getElementById('%s').hidden = settingsTab !== '%s';" % (body, tab), sw)
        self.assertIn("if (settingsTab === 'prompts' && !promptsLoaded) loadPromptsTab(false);", sw)
        self.assertIn("if (settingsTab === 'skills' && !skillsLoaded) loadSkillsTab(false);", sw)
        reload_fn = _extract_function(self.js, "reloadSettingsDrawer")
        self.assertIn("loadPromptsTab(true)", reload_fn)
        self.assertIn("loadSkillsTab(true)", reload_fn)
        self.assertIn("promptsLoaded = false", _extract_function(self.js, "openSettingsDrawer"))

    def test_preview_drawer_and_entries(self):
        for token in ('id="ctxPreviewDrawer"', 'id="ctxPreviewMask"', 'id="ctxPreviewControls"', 'id="ctxPreviewBody"',
                      'aria-labelledby="ctxPreviewTitle"'):
            self.assertIn(token, self.html)
        self.assertIn("'/api/context/preview?'", _extract_function(self.js, "runContextPreview"))
        self.assertIn("data-ctx-preview-role", _extract_function(self.js, "decorateRoleRows"))
        self.assertIn('data-sk-act="preview"', _extract_function(self.js, "skillDetailHtml"))
        self.assertIn('data-pp-act="preview"', _extract_function(self.js, "promptEditorHtml"))

    def test_detail_context_tab(self):
        detail = _extract_function(self.js, "detailHtml")
        self.assertIn("detailTabsHtml(i)", detail)
        self.assertIn('id="ctxPane" data-req="${i.id}"', detail)
        pane = _extract_function(self.js, "renderDetailPane")
        self.assertEqual(pane.count("afterDetailRender(item)"), 2)
        self.assertIn("/context/runs", _extract_function(self.js, "ctxLoadRuns"))
        self.assertIn("/prompt`", _extract_function(self.js, "ctxLoadPrompt"))

    def test_api_paths(self):
        self.assertIn("'/api/prompts/' + id", _extract_function(self.js, "ppSave"))
        self.assertIn("method: 'PUT'", _extract_function(self.js, "ppSave"))
        self.assertIn("method: 'DELETE'", _extract_function(self.js, "ppRestore"))
        self.assertIn("rebase: !!rebase", _extract_function(self.js, "ppSave"))
        self.assertIn("JSON.stringify({roles: next.roles, activation: next.activation})", _extract_function(self.js, "skCellClick"))
        self.assertIn("method: 'POST'", _extract_function(self.js, "skCreate"))

    def test_layer_tokens_both_skins(self):
        from tests.test_skin_system import LIGHT, ROOT
        for i in range(8):
            tok = "--layer-%d:" % i
            self.assertIn(tok, ROOT, tok)
            self.assertIn(tok, LIGHT, tok)
        for tok in ("--layer-rt:", "--layer-cut:", "--layer-ink:", "--cx-fence-bg:"):
            self.assertIn(tok, ROOT)
            self.assertIn(tok, LIGHT)
        for i in range(8):
            self.assertIn(".cx-l%d { --cx-c: var(--layer-%d); }" % (i, i), self.css)
        # 分层色只经 CSS 类消费：JS 里不出现层色字面量 / 内联颜色
        part = (Path(__file__).resolve().parent.parent / "static" / "board" / "55-context.js").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{6}\b|rgba?\(", part))

    def test_contents_not_translated(self):
        for fn in ("ctxPaneHtml", "previewBodyHtml", "promptEditorHtml", "skillListHtml"):
            self.assertIn("data-i18n-skip", _extract_function(self.js, fn), fn)


class ContextUiHarnessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过上下文界面 JS 断言")

    def test_harness(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(_harness_source())
            path = f.name
        try:
            proc = subprocess.run([self.node, path], capture_output=True, text=True, timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        try:
            checks = json.loads(proc.stdout).get("checks", [])
        except ValueError:
            self.fail("harness 输出非 JSON: %s\n%s" % (proc.stderr, proc.stdout[:2000]))
        self.assertGreaterEqual(len(checks), 60, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
