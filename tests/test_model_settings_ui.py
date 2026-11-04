#!/usr/bin/env python3
"""#6 模型设置前端测试（设置抽屉双 tab + 模型设置表单）。

U1（静态，模板与 static/board.js/board.css 源码文本）：双 tab DOM / 默认全局 / 打开重置 / 模型 tab 懒加载与重试；
U3（静态）：settingsReloadBtn 按当前 tab 重载；
U7（静态）：保存编排（空 delta 不请求 / default_model 清空拦截 / 成功重拉 / 失败行内错误不清空）；
U8（静态 + harness）：具名函数可被 _extract_function 提取；
U4~U6（node harness，tests/model_settings_harness.js）：渲染矩阵（含 default_model 编辑行）、
      转义矩阵、delta 矩阵。
#13（静态 + harness）：model 行 protocol 列（渲染/转义/回读）、provider type 合法值单一源
      （datalist + 提示由注入值渲染，源码无硬编码字面量）、delta protocol 矩阵
      （新增/更新/清空/同别名又删只留删）、保存错误文案与成功重拉逐字不变。
#18（静态 + harness）：model 行 max_context_size 列（渲染/转义/回读/行内原因）、
      context_defaults 注入与预填纯函数 msDefaultContextSize/msParseContextSize 矩阵、
      delta 把整数写进 models_add/models_update、新增行预填接线与保存前正整数预检。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_model_settings_ui.py -q
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "model_settings_harness.js"
sys.path.insert(0, str(REPO))

NEEDED_FUNCS = ("escapeHtml", "escapeAttr", "modelSettingsFormHtml", "modelSettingsDelta",
                "readModelSettingsFormState", "msDefaultContextSize", "msParseContextSize")


def _board_src():
    return frontend_src()


def _script_block():
    return frontend_js()


class ModelSettingsStaticTest(unittest.TestCase):
    """U1/U3/U7/U8：接线静态断言（源码文本）"""

    @classmethod
    def setUpClass(cls):
        cls.src = _board_src()
        cls.js = _script_block()

    def test_u1_dom_tabs_and_default(self):
        for token in ('id="sdTabGlobal"', 'id="sdTabModel"', 'class="sd-tab on" id="sdTabGlobal"',
                      'id="settingsBody"', 'id="settingsModelBody" hidden',
                      "function switchSettingsTab(", "async function loadModelSettings(",
                      "function modelSettingsFormHtml(", "function modelSettingsDelta(",
                      "function readModelSettingsFormState("):
            self.assertIn(token, self.src, token)
        open_fn = _extract_function(self.js, "openSettingsDrawer")
        self.assertIn("switchSettingsTab('global')", open_fn)   # 打开默认停全局
        self.assertIn("modelSettingsLoaded = false", open_fn)   # 每次打开重置 → 首次切换重拉

    def test_u2_lazy_load_and_retry(self):
        switch = _extract_function(self.js, "switchSettingsTab")
        self.assertIn("if (settingsTab === 'model' && !modelSettingsLoaded) loadModelSettings(false);", switch)
        loader = _extract_function(self.js, "loadModelSettings")
        self.assertIn("'/api/model-settings'", loader)
        self.assertIn("msRetryBtn", loader)
        self.assertIn("if (modelSettingsLoaded && !force) return;", loader)   # 同次打开内切换不重拉
        self.assertNotIn("settingsBody", loader)   # 只操作模型容器，不阻塞全局 tab

    def test_u3_reload_per_tab(self):
        reload_fn = _extract_function(self.js, "reloadSettingsDrawer")
        self.assertIn("loadModelSettings(true)", reload_fn)
        self.assertIn("loadSettingsDrawer(true)", reload_fn)
        self.assertIn("settingsReloadBtn').addEventListener('click', reloadSettingsDrawer", self.src)
        # 既有 ESC/遮罩关闭路径不动
        self.assertIn("if (e.key === 'Escape') closeSettingsDrawer();", self.js)

    def test_u7_save_orchestration(self):
        save = _extract_function(self.js, "saveModelSettings")
        self.assertIn("modelSettingsDelta(msBase, msForm)", save)
        self.assertIn("if (!delta) { showToast('没有改动'); return; }", save)
        self.assertIn("default_model 不能为空", save)          # 清空前端拦截
        self.assertIn("'/api/model-settings'", save)
        self.assertIn("method: 'POST'", save)
        self.assertIn("await loadModelSettings(true);", save)   # 成功重拉（重置 orig）
        self.assertIn("btn.disabled = true", save)              # 保存中禁用
        self.assertIn("msErr(d.error", save)                    # 失败行内错误、表单不清空

    def test_u7b_key_backfill(self):
        """评审必改 1 回归：key 输入回填本地未提交的 key_input——重渲染（增删别名/厂商后保存）
        不回丢用户已输入 key；初始（未输入）为空，服务端不下发原值、无回显路径。"""
        form = _extract_function(self.js, "modelSettingsFormHtml")
        self.assertIn("escapeAttr(p.key_input || '')", form)
        sync = _extract_function(self.js, "readModelSettingsFormState")
        self.assertIn("p.key_input = String(keyI.value", sync)

    def test_u13_state_from_data_new_fields(self):
        """#13：GET 形态解析——model 行补 protocol；state 顶层补 provider_types。"""
        fn = _extract_function(self.js, "modelSettingsStateFromData")
        self.assertIn("protocol: m.protocol || ''", fn)
        self.assertIn("provider_types: data.provider_types || []", fn)

    def test_u13_alias_protocol_render_source(self):
        """#13：别名 protocol 列渲染结构（有值/空值、表头、新增行）——harness 另有行为断言。"""
        form = _extract_function(self.js, "modelSettingsFormHtml")
        self.assertIn('class="ms-alias-protocol" data-oidx="', form)
        self.assertIn("escapeAttr(m.protocol || '')", form)
        self.assertIn('placeholder="protocol（可选）"', form)
        self.assertIn("<th>模型别名</th><th>上游 model</th><th>max_context_size</th>"
                      "<th>protocol</th><th>操作</th>", form)
        self.assertIn('<td colspan="5">（无别名）</td>', form)
        self.assertIn('class="ms-new-protocol"', form)

    def test_u14e_context_size_render_and_wiring(self):
        """#18：ctx 列渲染位（输入 + 行内原因）、新增行输入、context_defaults 注入与预填接线。"""
        form = _extract_function(self.js, "modelSettingsFormHtml")
        self.assertIn('class="ms-alias-ctx" data-oidx="', form)
        self.assertIn("escapeAttr(m.max_context_size", form)
        self.assertIn('placeholder="max_context_size（必填）"', form)
        self.assertIn('class="ms-field-hint ms-ctx-warn"', form)
        self.assertIn("escapeHtml(m.incomplete_reason)", form)
        self.assertIn('class="ms-new-ctx"', form)
        state = _extract_function(self.js, "modelSettingsStateFromData")
        self.assertIn("context_defaults: data.context_defaults || []", state)
        self.assertIn("max_context_size: (m.max_context_size == null ? ''", state)
        self.assertIn("incomplete_reason: m.incomplete_reason || ''", state)
        # 预填接线：新增行 model 输入 → 按厂商 type + 模型名填默认值（仅当仍为空）
        self.assertIn("classList.contains('ms-new-model')", self.js)
        self.assertIn("msDefaultContextSize(type, t.value, (msForm && msForm.context_defaults) || [])", self.js)
        # 新增别名校验 + 保存前预检（读法 A：新增行与已改动行要求正整数）
        add = _extract_function(self.js, "msAddAlias")
        self.assertIn("msDefaultContextSize(p.type, model, (msForm && msForm.context_defaults) || [])", add)
        self.assertIn("msParseContextSize(ctxRaw)", add)
        save = _extract_function(self.js, "saveModelSettings")
        self.assertIn("msParseContextSize(cur) === null", save)
        self.assertIn("max_context_size 必填且需为 ≥1 的整数：", save)

    def test_u13_provider_types_single_source(self):
        """#13：provider type 合法值单一源——datalist 与提示由注入 provider_types 渲染，
        源码不含硬编码字面量「常见：openai-compatible / openai / anthropic」。"""
        form = _extract_function(self.js, "modelSettingsFormHtml")
        self.assertIn("const ptypes = Array.isArray(data.provider_types) ? data.provider_types : [];", form)
        self.assertIn('<datalist id="msProviderTypes">', form)
        self.assertIn('class="ms-type" list="msProviderTypes"', form)
        self.assertIn('id="msNewType" list="msProviderTypes"', form)
        self.assertIn("ptypes.length ? escapeHtml(ptypes.join(' / ')) : '（未取到合法值清单）'", form)
        self.assertNotIn("常见：openai-compatible / openai / anthropic", self.src)

    def test_u13_read_form_reads_protocol_by_alias(self):
        """#13：readModelSettingsFormState 按 data-alias 对位回写 protocol（不按索引）。"""
        sync = _extract_function(self.js, "readModelSettingsFormState")
        self.assertIn("block.querySelectorAll('.ms-alias-protocol')", sync)
        self.assertIn("el.getAttribute('data-alias')", sync)
        self.assertIn("row.protocol = String(el.value", sync)

    def test_u13_error_and_reload_unchanged(self):
        """#13 回归护栏：400 错误文案进 msErr、保存成功重拉 loadModelSettings(true) 逐字不变。"""
        save = _extract_function(self.js, "saveModelSettings")
        self.assertIn("msErr(d.error || ('保存失败（HTTP ' + res.status + '）'));", save)
        self.assertIn("await loadModelSettings(true);", save)

    def test_u8_extractable_functions(self):
        for name in NEEDED_FUNCS + ("switchSettingsTab", "loadModelSettings", "saveModelSettings",
                                    "readModelSettingsFormState", "renderModelSettingsForm",
                                    "msAddProvider", "msAddAlias", "msDeleteProvider", "msDeleteAlias"):
            _extract_function(self.js, name)   # 不可提取即抛 AssertionError


def _harness_source():
    js = _script_block()
    parts = [_extract_function(js, name) for name in NEEDED_FUNCS]
    return "\n\n".join(parts) + "\n\n" + HARNESS_JS.read_text(encoding="utf-8")


class ModelSettingsHarnessTest(unittest.TestCase):
    """U4~U6：模型设置渲染/转义/delta 矩阵（node 断言）"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 #6 模型设置 JS 断言")

    def _run_harness(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(_harness_source())
            path = f.name
        try:
            proc = subprocess.run([self.node, path], capture_output=True, text=True, timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        if proc.returncode != 0:
            self.fail("node harness 失败: %s\n%s" % (proc.stderr, proc.stdout))
        return json.loads(proc.stdout)

    def test_u456_matrix_pass(self):
        """U4 渲染 / U5 转义 / U6 delta：全矩阵通过"""
        checks = self._run_harness().get("checks", [])
        self.assertGreaterEqual(len(checks), 25, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)


if __name__ == "__main__":
    unittest.main()
