#!/usr/bin/env python3
"""#22 飞书机器人设置前端测试（设置抽屉第三 tab + 飞书表单接线）+ 「飞书建卡」只读视图结构。

对应技术方案用例 14-20（设置抽屉）与 B12 结构兜底：
14 #sdTabFeishu 位于 #sdTabModel 右侧；15 settingsTab 三分支互斥显隐（含 #settingsFeishuBody）；
16 reloadSettingsDrawer 走 loadFeishuSettings(true)；17 首次切 tab 懒加载（打开重置标志）；
18 保存调 POST /api/feishu-settings、成功文案含「需重启调度器生效」、失败展示 error 不清空输入；
19 「测试连通性」渲染 code + msg；20 新增 JS 通过 node --check（等同 test_served_js_syntax）。
B12：add-pane 切换 chip、renderFeishuChatPane、数据源含 failed、只读视图仅「关闭会话」写动作位。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_feishu_settings_ui -v
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import backend_text, frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _src():
    return frontend_src()


def _extract_top_func(js, name):
    """按「下一个 8 空格缩进的顶层 function 声明」切分函数源码。

    用于参数含对象解构（`{created = false} = {}`）的函数——`_extract_function` 会把解构的
    `{}` 当作函数体边界提前截断。"""
    marker = "function %s(" % name
    start = js.index(marker)
    nxt = js.find("\n        function ", start + len(marker))
    if nxt < 0:
        nxt = len(js)
    return js[start:nxt].rstrip()


class FeishuSettingsUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _src()
        cls.js = frontend_js()

    def test_14_third_tab_after_model(self):
        self.assertIn('id="sdTabFeishu"', self.src)
        self.assertIn(">飞书机器人</button>", self.src)
        self.assertLess(self.src.index('id="sdTabModel"'), self.src.index('id="sdTabFeishu"'))
        self.assertIn("全局设置（全局信息 / 模型设置 / 飞书机器人）", self.src)

    def test_15_three_branch_panes(self):
        self.assertIn('id="settingsFeishuBody" hidden', self.src)
        switch = _extract_function(self.js, "switchSettingsTab")
        self.assertIn("'feishu'", switch)
        self.assertIn("settingsTab === 'feishu'", switch)
        self.assertIn("document.getElementById('settingsFeishuBody').hidden = settingsTab !== 'feishu';", switch)
        self.assertIn("document.getElementById('settingsBody').hidden = settingsTab !== 'global';", switch)
        self.assertIn("document.getElementById('settingsModelBody').hidden = settingsTab !== 'model';", switch)

    def test_16_reload_routes_to_feishu(self):
        reload_fn = _extract_function(self.js, "reloadSettingsDrawer")
        self.assertIn("loadFeishuSettings(true)", reload_fn)
        self.assertIn("loadModelSettings(true)", reload_fn)
        self.assertIn("loadSettingsDrawer(true)", reload_fn)

    def test_17_lazy_load_reset_on_open(self):
        open_fn = _extract_function(self.js, "openSettingsDrawer")
        self.assertIn("feishuSettingsLoaded = false", open_fn)
        switch = _extract_function(self.js, "switchSettingsTab")
        self.assertIn("if (settingsTab === 'feishu' && !feishuSettingsLoaded) loadFeishuSettings(false);", switch)
        loader = _extract_function(self.js, "loadFeishuSettings")
        self.assertIn("if (feishuSettingsLoaded && !force) return;", loader)
        self.assertIn("'/api/feishu-settings'", loader)
        self.assertIn("fsRetryBtn", loader)

    def test_18_save_wiring(self):
        form = _extract_function(self.js, "feishuSettingsFormHtml")
        for needle in ('id="fsAppId"', 'id="fsAppSecret"', 'type="password"', 'id="fsSaveBtn"',
                       'id="fsTestBtn"', 'id="fsError"', 'id="fsResult"', 'id="fsClearSecret"',
                       "已配置（留空=保留）", "清除已配置 secret"):
            self.assertIn(needle, form, needle)
        save = _extract_function(self.js, "saveFeishuSettings")
        self.assertIn("'/api/feishu-settings'", save)
        self.assertIn("method: 'POST'", save)
        self.assertIn("需重启调度器生效", save)
        self.assertIn("fsErr(d.error", save)          # 失败行内错误、表单不清空
        self.assertIn("await loadFeishuSettings(true);", save)
        # 失败路径不重渲染表单（输入保留）
        self.assertNotIn("body.innerHTML", save)

    def test_19_test_button_renders_code_and_msg(self):
        test_fn = _extract_function(self.js, "testFeishuSettings")
        self.assertIn("'/api/feishu-settings/test'", test_fn)
        self.assertIn("method: 'POST'", test_fn)
        self.assertIn("code=", test_fn)
        self.assertIn("d.msg", test_fn)
        self.assertIn("d.ok ? '成功' : '失败'", test_fn)

    def test_20_served_js_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用")
        import board as b  # noqa: E402
        html = b.app.test_client().get("/").get_data(as_text=True)
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertGreaterEqual(len(blocks), 1)
        for js in blocks:
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                             delete=False) as f:
                f.write(js)
                tmp = f.name
            try:
                proc = subprocess.run([node, "--check", tmp], capture_output=True,
                                      text=True, timeout=60)
            finally:
                Path(tmp).unlink(missing_ok=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        # #33：主脚本已外置，static/board.js 文件字节单独做一次语法检查
        proc = subprocess.run([node, "--check", str(REPO / "static" / "board.js")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class FeishuChatPaneUiTest(unittest.TestCase):
    """B12/B12′/B12″：「飞书建卡」只读视图结构（源码断言）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _src()
        cls.js = frontend_js()

    def test_b12_toggle_chip_and_state(self):
        self.assertIn("let addPaneTab = 'chat';", self.js)
        form = _extract_function(self.js, "addFormHtml")
        self.assertIn('id="addPaneTabChat"', form)
        self.assertIn('id="addPaneTabFeishu"', form)
        self.assertIn("add-pane-chip", form)
        self.assertIn("addPaneTab === 'feishu'", form)
        switch = _extract_function(self.js, "switchAddPaneTab")
        self.assertIn("renderDetailPane()", switch)

    def test_b12_data_source_and_render(self):
        loader = _extract_function(self.js, "loadFeishuChatList")
        self.assertIn("channel=feishu", loader)
        self.assertIn("limit=${FEISHU_CHAT_PAGE}", loader)
        self.assertIn("&before_id=", loader)
        self.assertIn("fetchChatEvents(r.id)", loader)
        self.assertIn("FEISHU_CHAT_STATUSES", self.js)
        for st in ("created", "closed", "failed", "idle", "running", "interrupted", "suspended"):
            self.assertIn(st, self.js)
        self.assertIn("created,closed,failed,idle,running,interrupted,suspended", self.js)
        # 消息渲染复用 chatEventsHtml
        box = _extract_function(self.js, "feishuSessionBoxHtml")
        self.assertIn("chatEventsHtml(s.events", box)

    def test_b12_close_only_write_action(self):
        bind = _extract_function(self.js, "bindFeishuChatPane")
        self.assertIn("data-feishu-close", bind)
        box = _extract_function(self.js, "feishuSessionBoxHtml")
        self.assertIn("data-feishu-close=", box)
        self.assertIn("关闭会话", box)
        self.assertNotIn("发送", box)          # 只读：无发送按钮
        self.assertNotIn("textarea", box)      # 只读：无输入区
        close_fn = _extract_function(self.js, "closeFeishuChat")
        self.assertIn("/close", close_fn)
        self.assertIn("window.confirm", close_fn)
        self.assertIn("loadFeishuChatList(true)", close_fn)

    def test_b12_sse_refresh(self):
        # ③：飞书 tab 的 chat poke 走增量分流（已渲染→单条增量、未渲染→整页补拉），
        # 不再无条件整页 loadFeishuChatList(true) 清空重拉。
        poke = _extract_function(self.js, "handleSsePoke")
        self.assertIn("addPaneTab === 'feishu'", poke)
        self.assertIn("refreshFeishuChatOnPoke(p.chat_id)", poke)
        self.assertNotIn("addPaneTab === 'feishu') loadFeishuChatList(true)", poke)
        onpoke = _extract_function(self.js, "refreshFeishuChatOnPoke")
        self.assertIn("feishuChatList.some", onpoke)
        self.assertIn("refreshFeishuChatSession(chatId)", onpoke)
        self.assertIn("loadFeishuChatList(true)", onpoke)    # 未渲染 → 整页补拉
        incr = _extract_function(self.js, "refreshFeishuChatSession")
        self.assertIn("/events?after=", incr)                # 增量游标
        self.assertIn("replaceWith", incr)                   # 原位替换该会话盒子（不整页重绘）
        self.assertIn("feishuChatSessions", incr)

    def test_served_js_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用")
        import board as b  # noqa: E402
        html = b.app.test_client().get("/").get_data(as_text=True)
        for js in re.findall(r"<script>(.*?)</script>", html, re.S):
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                             delete=False) as f:
                f.write(js)
                tmp = f.name
            try:
                proc = subprocess.run([node, "--check", tmp], capture_output=True,
                                      text=True, timeout=60)
            finally:
                Path(tmp).unlink(missing_ok=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        # #33：主脚本已外置，static/board.js 文件字节单独做一次语法检查
        proc = subprocess.run([node, "--check", str(REPO / "static" / "board.js")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class FeishuViewRenderTest(unittest.TestCase):
    """②/④：飞书视图气泡渲染规则（node 跑真实函数 + 源码接线断言）。

    ② chatEventsHtml 显式认 kind='agent'（机器人追问/说明）走机器人气泡；kind='message' 同样走
       机器人气泡；kind='user'（含 `[飞书] 用户：` 前缀）仍走用户气泡。isChatProcessEvent /
       isChatLifecycleEvent 只看 system 事件的内容特征，绝不滤掉 message/agent 正文。
    ④ feishuSessionBoxHtml 对终态（created/failed/closed）不渲染「关闭会话」按钮，其余状态保留。
    """

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.js = frontend_js()

    def _run_node(self, script):
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                         delete=False) as f:
            f.write(script)
            tmp = f.name
        try:
            proc = subprocess.run([self.node, tmp], capture_output=True, text=True, timeout=60)
        finally:
            Path(tmp).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_b22_agent_and_message_render_agent_bubble(self):
        if not self.node:
            self.skipTest("node 不可用")
        fn = _extract_top_func(self.js, "chatEventsHtml")
        script = "\n".join([
            "function escapeHtml(s){return String(s);}",
            "function renderArtifactMd(s){return 'MD:'+String(s);}",
            "function isChatLifecycleEvent(){return false;}",
            "function isChatProcessEvent(){return false;}",
            "function applyChatClosing(t){return t;}",
            "function mergeMessageBlocks(evs){return (evs||[]).map("
            "e=>({kind:e&&e.kind,content:String(e&&e.content!=null?e.content:'')}));}",
            fn,
            "console.log(chatEventsHtml([{kind:'user',content:'U'},{kind:'agent',content:'A'},"
            "{kind:'message',content:'M'},{kind:'system',content:'S'}]));",
        ])
        out = self._run_node(script)
        self.assertIn('<div class="chat-msg user">U</div>', out)
        self.assertIn('<div class="chat-msg agent">MD:A</div>', out)   # agent（机器人追问/说明）
        self.assertIn('<div class="chat-msg agent">MD:M</div>', out)   # message（agent 正文）
        self.assertIn('<div class="chat-msg sys">S</div>', out)

    def test_b30_card_event_renders_summary_not_json(self):
        """#30：kind='card' 的页 spec JSON 不直接渲染，只显示人类可读摘要（不吐 JSON）。"""
        if not self.node:
            self.skipTest("node 不可用")
        fn = _extract_top_func(self.js, "chatEventsHtml")
        summary = _extract_function(self.js, "cardEventSummary")
        card = {"v": 1, "type": "requirement_list", "project": "loop-engine", "page": 1,
                "pages": 3, "total_cards": 37, "truncated": False, "remaining": 0, "items": []}
        script = "\n".join([
            "function escapeHtml(s){return String(s);}",
            "function renderArtifactMd(s){return 'MD:'+String(s);}",
            "function isChatLifecycleEvent(){return false;}",
            "function isChatProcessEvent(){return false;}",
            "function applyChatClosing(t){return t;}",
            "function mergeMessageBlocks(evs){return (evs||[]).map("
            "e=>({kind:e&&e.kind,content:String(e&&e.content!=null?e.content:'')}));}",
            summary,
            fn,
            "console.log(chatEventsHtml([{kind:'card',content:%s}]));" % json.dumps(json.dumps(card)),
        ])
        out = self._run_node(script)
        self.assertIn("已发送需求卡清单", out)
        self.assertIn("项目 loop-engine", out)
        self.assertIn("第 1/3 页", out)
        self.assertIn("共 37 张", out)
        self.assertNotIn('"type"', out)          # 不吐原始 JSON 片段
        self.assertNotIn("{", out)

    def test_b22_filters_only_system_events(self):
        # 两个判据都先判 kind==='system'：kind='message'/'agent' 的 agent 正文永不被滤掉。
        for name in ("isChatProcessEvent", "isChatLifecycleEvent"):
            fn = _extract_function(self.js, name)
            self.assertIn("event.kind !== 'system'", fn, name)

    def test_b23_terminal_sessions_hide_close_button(self):
        if not self.node:
            self.skipTest("node 不可用")
        fn = _extract_top_func(self.js, "feishuSessionBoxHtml")
        script = "\n".join([
            "function chatEventsHtml(){return 'EV';}",
            "function feishuSessionMeta(s){return 'META';}",
            fn,
            "const out={};",
            "for (const st of ['created','failed','closed','idle','running','interrupted','suspended'])"
            " out[st]=feishuSessionBoxHtml({id:1,status:st,events:[]});",
            "console.log(JSON.stringify(out));",
        ])
        out = json.loads(self._run_node(script))
        for st in ("created", "failed", "closed"):
            self.assertNotIn("data-feishu-close", out[st], st)
            self.assertNotIn("关闭会话", out[st], st)
        for st in ("idle", "running", "interrupted", "suspended"):
            self.assertIn("data-feishu-close", out[st], st)
            self.assertIn("关闭会话", out[st], st)


class FeishuRuntimeUiTest(unittest.TestCase):
    """#23 飞书通道状态 UI（用例 45-54）：运行态区渲染、按钮可见性矩阵、保存不静默重启、
    重启函数接线与 409/400/502 原因展示。

    渲染断言用 node 真实执行 `feishuSettingsFormHtml` / `saveFeishuSettings` /
    `restartFeishuDispatcher`（从 static/board.js 提取，非源码文本匹配）。
    """

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.src = _src()
        cls.js = frontend_js()

    def _node(self, script):
        if not self.node:
            self.skipTest("node 不可用")
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                         delete=False) as f:
            f.write(script)
            tmp = f.name
        try:
            proc = subprocess.run([self.node, tmp], capture_output=True, text=True, timeout=60)
        finally:
            Path(tmp).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _form_harness(self, payload, extra=""):
        form = _extract_function(self.js, "feishuSettingsFormHtml")
        return "\n".join([
            "function escapeHtml(s){return String(s);}",
            "function escapeAttr(s){return String(s);}",
            form,
            "const out = feishuSettingsFormHtml(%s);" % json.dumps(payload),
            extra,
        ])

    # ---- 46 ----
    def test_46_form_renders_runtime_state(self):
        out = self._node(self._form_harness({
            "path": "/tmp/feishu.json", "app_id": "cli_x", "app_secret_configured": True,
            "warnings": [],
            "runtime": {"state": "not_loaded", "reason": "调度器尚未加载当前凭据",
                        "source": "feishu.json", "restart_available": True,
                        "stale_seconds": 400},
        }, "console.log(out);"))
        self.assertIn('id="fsRuntime"', out)
        self.assertIn("凭据未加载", out)                # #40：状态名与五态表一致
        self.assertNotIn("通道状态：调度器尚未加载", out)  # 旧状态名不再作为状态名渲染
        self.assertNotIn("心跳已过期 400s", out)        # #40：非 stale 不再拼 stale_seconds（旧字段仅接口保留）
        self.assertIn("原因：", out)
        self.assertIn("凭据来源：feishu.json", out)
        self.assertIn('id="fsRestartBtn"', out)
        self.assertIn("重启调度器使其生效", out)
        self.assertIn("fs-warn", out)                  # 未加载 = 警告档
        self.assertIn('id="fsAppSecret" type="password" value=""', out)   # secret 不回显

    # ---- 46c（#40 五态渲染矩阵：状态名 + 档位 + 重启按钮）----
    def test_46c_form_renders_five_states(self):
        cases = [
            ("stale", "最后心跳 400s 前，上次上报 phase=connected", "心跳过期", "fs-off", False),
            ("failed", "连接失败：超时", "网络连接失败", "fs-off", False),
            ("disabled", "REQ_BOARD_NO_FEISHU=1（显式停用飞书通道）", "显式停用", "fs-off", False),
            ("disabled", "feishu.json 不可读，请检查文件权限", "凭据未配置", "fs-off", False),
            ("unknown", "调度器未上报飞书状态（旧版本或未运行）", "调度器未上报", "fs-warn", True),
        ]
        for state, reason, label, cls, expect_btn in cases:
            with self.subTest(state=state, reason=reason):
                out = self._node(self._form_harness({
                    "path": "/tmp/feishu.json", "app_id": "cli_x", "app_secret_configured": True,
                    "warnings": [],
                    "runtime": {"state": state, "reason": reason, "source": "feishu.json",
                                "restart_available": expect_btn},
                }, "console.log(out);"))
                self.assertIn(label, out, state)
                self.assertIn(cls, out, state)
                self.assertEqual('id="fsRestartBtn"' in out, expect_btn, state)
                if state == "disabled":
                    # 显式停用与凭据文件问题互不冒充
                    if "REQ_BOARD_NO_FEISHU" in reason:
                        self.assertNotIn("凭据未配置", out)
                    else:
                        self.assertNotIn("显式停用", out)

    def test_46b_connected_form_has_no_restart_button(self):
        out = self._node(self._form_harness({
            "path": "/tmp/feishu.json", "app_id": "cli_x", "app_secret_configured": True,
            "warnings": [], "runtime": {"state": "connected", "reason": None,
                                        "source": "feishu.json", "restart_available": False},
        }, "console.log(out);"))
        self.assertIn("已连接", out)
        self.assertIn("fs-ok", out)
        self.assertNotIn('id="fsRestartBtn"', out)

    # ---- 47 ----
    def test_47_button_visibility_matrix(self):
        cases = [
            ("not_loaded", True, "fs-warn", True),
            ("unknown", True, "fs-warn", True),
            ("connected", False, "fs-ok", False),
            ("failed", False, "fs-off", False),
            ("stale", False, "fs-off", False),
            ("disabled", False, "fs-off", False),
        ]
        for state, available, cls, expect_btn in cases:
            with self.subTest(state=state):
                out = self._node(self._form_harness({
                    "path": "/tmp/feishu.json", "app_id": "cli_x", "app_secret_configured": True,
                    "warnings": [],
                    "runtime": {"state": state, "reason": "原因", "source": "feishu.json",
                                "restart_available": available},
                }, "console.log(out);"))
                self.assertEqual('id="fsRestartBtn"' in out, expect_btn, state)
                self.assertIn(cls, out, state)

    # ---- 48 ----
    def test_48_save_never_calls_restart_endpoint(self):
        save = _extract_function(self.js, "saveFeishuSettings")
        self.assertNotIn("/api/feishu-settings/restart", save)
        self.assertIn("需重启调度器生效", save)
        self.assertIn("await loadFeishuSettings(true);", save)
        out = self._node("\n".join([
            "const calls = [];",
            "let feishuSettingsLoaded=true; let fsBase=null;",
            "const els = {fsAppId:{value:'cli_x'}, fsAppSecret:{value:'s'}, fsClearSecret:{checked:false},",
            "  fsSaveBtn:{}, fsError:{textContent:''}, fsResult:{innerHTML:''}};",
            "global.document = {getElementById: id => els[id] || null};",
            "global.showToast = () => {};",
            "function escapeHtml(s){return String(s);}",
            "function fsErr(m){els.fsError.textContent = m || '';}",
            "function fsResult(h){els.fsResult.innerHTML = h || '';}",
            "function readFeishuSecretInput(){return String(els.fsAppSecret.value || '');}",
            "async function loadFeishuSettings(f) { calls.push(['/api/feishu-settings','LOAD']); }",
            "global.fetch = async (url, opts) => {",
            "  calls.push([url, (opts && opts.method) || 'GET']);",
            "  return {ok:true, status:200, json: async () => ({ok:true, changed:true, restart_required:true})};",
            "};",
            "async " + save,
            "saveFeishuSettings().then(() => console.log(JSON.stringify(calls)));",
        ]))
        calls = json.loads(out)
        self.assertIn(["/api/feishu-settings", "POST"], calls)
        self.assertEqual([c for c in calls if "restart" in c[0]], [])
        self.assertIn(["/api/feishu-settings", "LOAD"], calls)

    # ---- 49 ----
    def test_49_restart_function_wiring(self):
        fn = _extract_function(self.js, "restartFeishuDispatcher")
        self.assertIn("window.confirm", fn)
        self.assertIn("'/api/feishu-settings/restart'", fn)
        self.assertIn("method: 'POST'", fn)
        self.assertIn("confirm: true", fn)
        self.assertIn("btn.disabled = true", fn)
        self.assertIn("重启中…", fn)
        self.assertIn("loadFeishuSettings(true)", fn)
        self.assertIn("fetchFeishuRuntime", fn)

    def test_49b_restart_waits_through_connecting_and_keeps_result(self):
        """评审必改 2：轮询不得一见 connecting/failed 就停，且成功提示不能被表单重绘吃掉。

        真实执行 `restartFeishuDispatcher`（含真实 `fetchFeishuRuntime`）：前两次运行态为
        `failed（正在建立长连接）`、第三次 `connected`；断言轮询走了 3 次、最终提示含「已连接」。
        """
        fn = _extract_function(self.js, "restartFeishuDispatcher")
        poll = _extract_function(self.js, "fetchFeishuRuntime")
        out = self._node("\n".join([
            "const calls = []; let confirms = 0, polls = 0;",
            "let feishuSettingsLoaded=true; let fsBase=null;",
            "const FEISHU_RESTART_WAIT_MS = 60000;",
            "const btn = {disabled:false, textContent:''};",
            "const els = {fsRestartBtn:btn, fsError:{textContent:''}, fsResult:{innerHTML:''}};",
            "global.document = {getElementById: id => els[id] || null};",
            "global.window = {confirm: () => { confirms++; return true; }};",
            "function escapeHtml(s){return String(s);}",
            "function fsErr(m){ els.fsError.textContent = m || ''; }",
            "function fsResult(h){ els.fsResult.innerHTML = h || ''; }",
            "async function loadFeishuSettings(f) { calls.push('LOAD'); }",
            "global.fetch = async (url, opts) => {",
            "  calls.push([url, (opts && opts.method) || 'GET', opts && opts.body]);",
            "  if (String(url).indexOf('restart') >= 0) {",
            "    return {ok:true, status:200, json: async () => ({restarted:true, detail:'心跳文件已更新'})};",
            "  }",
            "  polls++;",
            "  const rt = polls <= 2 ? {state:'failed', reason:'正在建立长连接'} : {state:'connected'};",
            "  return {ok:true, status:200, json: async () => ({runtime: rt})};",
            "};",
            "async " + poll,
            "async " + fn,
            "restartFeishuDispatcher().then(() => console.log(JSON.stringify(",
            "  {calls, confirms, polls, result: els.fsResult.innerHTML, disabled: btn.disabled})));",
        ]))
        d = json.loads(out)
        self.assertEqual(d["confirms"], 1)
        self.assertEqual(d["polls"], 3)                 # 前两次 failed 不停，第三次 connected 才停
        post = [c for c in d["calls"] if isinstance(c, list) and "restart" in c[0]]
        self.assertEqual(len(post), 1)
        self.assertEqual(json.loads(post[0][2]), {"confirm": True})
        self.assertEqual(d["calls"][-1], "LOAD")        # 表单重绘在轮询之后（提示不会被吃掉）
        self.assertIn("长连接已建立（已连接）", d["result"])
        self.assertFalse(d["disabled"])

    def test_49c_restart_timeout_says_not_yet_connected(self):
        """超时未收到 connected 时必须如实说「尚未收到」，不得假装成功。"""
        fn = _extract_function(self.js, "restartFeishuDispatcher")
        poll = _extract_function(self.js, "fetchFeishuRuntime")
        out = self._node("\n".join([
            "const calls = []; let polls = 0;",
            "let feishuSettingsLoaded=true; let fsBase=null;",
            "const FEISHU_RESTART_WAIT_MS = 200;",
            "const els = {fsRestartBtn:{disabled:false, textContent:''}, fsError:{textContent:''},",
            "  fsResult:{innerHTML:''}};",
            "global.document = {getElementById: id => els[id] || null};",
            "global.window = {confirm: () => true};",
            "function escapeHtml(s){return String(s);}",
            "function fsErr(m){ els.fsError.textContent = m || ''; }",
            "function fsResult(h){ els.fsResult.innerHTML = h || ''; }",
            "async function loadFeishuSettings(f) {}",
            "global.fetch = async (url, opts) => {",
            "  if (String(url).indexOf('restart') >= 0) {",
            "    return {ok:true, status:200, json: async () => ({detail:'ok'})};",
            "  }",
            "  polls++;",
            "  return {ok:true, status:200,",
            "    json: async () => ({runtime: {state:'failed', reason:'连接失败：凭据不合法'}})};",
            "};",
            "async " + poll,
            "async " + fn,
            "restartFeishuDispatcher().then(() => console.log(JSON.stringify(",
            "  {polls, result: els.fsResult.innerHTML})));",
        ]))
        d = json.loads(out)
        self.assertGreaterEqual(d["polls"], 1)
        self.assertIn("尚未收到", d["result"])
        self.assertIn("凭据不合法", d["result"])
        self.assertNotIn("已连接（已连接）", d["result"])

    def test_49d_cancel_confirm_no_request(self):
        fn = _extract_function(self.js, "restartFeishuDispatcher")
        out = self._node("\n".join([
            "const calls = [];",
            "global.document = {getElementById: () => ({})};",
            "global.window = {confirm: () => false};",
            "global.fetch = async (u) => { calls.push(u); return {ok:true, json: async()=>({})}; };",
            "let fsErrMsg='';",
            "function fsErr(m){fsErrMsg = m;}",
            "function fsResult(h){}",
            "async " + fn,
            "restartFeishuDispatcher().then(() => console.log(JSON.stringify(calls)));",
        ]))
        self.assertEqual(json.loads(out), [])

    # ---- 50 ----
    def test_50_error_reasons_shown_inline(self):
        fn = _extract_function(self.js, "restartFeishuDispatcher")
        self.assertIn("res.ok", fn)
        self.assertIn("d.error", fn)
        self.assertIn("HTTP ", fn)
        # 409/400/502 都走同一行内错误分支，且不清空表单（不重渲染 body）
        self.assertNotIn("body.innerHTML", fn)
        out = self._node("\n".join([
            "let errMsg = '';",
            "global.document = {getElementById: (id) => id === 'fsRestartBtn' ? {disabled:false, textContent:''} : {textContent:'', innerHTML:''}};",
            "global.window = {confirm: () => true};",
            "global.fetch = async () => ({ok:false, status:409,",
            "  json: async () => ({error:'检测到运行中 kimi agent 1 个，拒绝重启调度器'})});",
            "function fsErr(m){errMsg = m;}",
            "function fsResult(h){}",
            "function escapeHtml(s){return String(s);}",
            "async " + fn,
            "restartFeishuDispatcher().then(() => console.log(errMsg));",
        ]))
        self.assertIn("拒绝重启调度器", out)

    # ---- 51 ----
    def test_51_test_button_scope_hint(self):
        test_fn = _extract_function(self.js, "testFeishuSettings")
        self.assertIn("code=", test_fn)
        self.assertIn("d.msg", test_fn)
        self.assertIn("仅验证 tenant_access_token", test_fn)
        self.assertIn("不代表 WebSocket 长连接", test_fn)

    # ---- 52 ----
    def test_52_guide_uses_same_status_helper(self):
        src = backend_text()
        start = src.index("def _runtime_env_rows()")
        rows = src[start:src.index("\ndef ", start)]
        self.assertIn("_feishu_runtime_text()", rows)
        # 全源负向断言：旧文案须同时不在后端与前端全源（模板 + static/board.js + static/board.css）
        self.assertNotIn("已接飞书（feishu.json 凭据已配置）", frontend_src() + src)

    # ---- 54 ----
    def test_54_status_styles_reuse_tokens(self):
        for cls in (".fs-status", ".fs-status.fs-ok", ".fs-status.fs-warn", ".fs-status.fs-off"):
            self.assertIn(cls, self.src, cls)
        block = self.src[self.src.index(".fs-status {"):]
        block = block[:block.index("}") + 1]
        for token in ("var(--text-2)", "var(--card-bg)", "var(--card-border)"):
            self.assertIn(token, block)
        # 语义类只用既有令牌（不引入新皮肤变量定义）
        self.assertNotIn("--fs-", self.src)


class FeishuCapSwitchUiTest(FeishuRuntimeUiTest):
    """#26 看板助手能力复选框：渲染（默认未勾选 / 显式 true 勾选）与保存接线（两个布尔字段）。"""

    def test_55_form_renders_cap_checkboxes(self):
        out = self._node(self._form_harness({
            "path": "/tmp/feishu.json", "app_id": "cli_x", "app_secret_configured": True,
            "warnings": [], "runtime": {"state": "connected", "source": "feishu.json",
                                        "restart_available": False},
        }, "console.log(out);"))
        self.assertIn('id="fsAssistantFileTools"', out)
        self.assertIn('id="fsAssistantStatusWrite"', out)
        self.assertIn('id="fsAssistantReqCards"', out)     # #30 需求卡清单卡片开关
        self.assertIn('id="fsAssistantGitOps"', out)       # #54 代码拉取与提交开关
        self.assertIn('id="fsAssistantVision"', out)       # #52 读图（多模态）开关
        self.assertIn("其余四个开关", out)
        self.assertNotIn("其余三个开关", out)
        self.assertIn("读图（多模态）", out)
        self.assertIn("看板助手能力", out)
        self.assertIn("保存即生效", out)
        self.assertIn("仍需重启调度器", out)
        # #31 每个开关配一行「开启后能做什么」说明 + 当前状态标注（默认关闭 = 设计预期）
        self.assertIn("开启后助手能读文件", out)
        self.assertIn("开启后助手能把已建卡从 暂缓 改为 待开发", out)
        self.assertIn("开启后助手能按项目拉取当前分支最新代码", out)
        self.assertIn("开启后助手能直接查看飞书发来的图片", out)
        self.assertIn("当前关闭（默认关闭）", out)
        # 缺省（无字段）= 未勾选
        self.assertNotIn('id="fsAssistantFileTools" checked', out)
        self.assertNotIn('id="fsAssistantStatusWrite" checked', out)
        self.assertNotIn('id="fsAssistantReqCards" checked', out)
        self.assertNotIn('id="fsAssistantGitOps" checked', out)
        self.assertNotIn('id="fsAssistantVision" checked', out)
        on = self._node(self._form_harness({
            "path": "/tmp/feishu.json", "app_id": "cli_x", "app_secret_configured": True,
            "assistant_file_tools": True, "assistant_status_write": True,
            "assistant_req_cards": True, "assistant_git_ops": True,
            "assistant_vision": True,
            "warnings": [], "runtime": {"state": "connected", "source": "feishu.json",
                                        "restart_available": False},
        }, "console.log(out);"))
        self.assertIn('id="fsAssistantFileTools" checked', on)
        self.assertIn('id="fsAssistantStatusWrite" checked', on)
        self.assertIn('id="fsAssistantReqCards" checked', on)
        self.assertIn('id="fsAssistantGitOps" checked', on)
        self.assertIn('id="fsAssistantVision" checked', on)
        self.assertIn("当前已开启", on)          # #31 已开启态标注
        # #52 读图默认开启：服务端回 true 时勾选并标注「默认开启」；显式 false 时取消勾选
        self.assertIn("当前已开启（默认开启）", on)
        off = self._node(self._form_harness({
            "path": "/tmp/feishu.json", "app_id": "cli_x", "app_secret_configured": True,
            "assistant_vision": False,
            "warnings": [], "runtime": {"state": "connected", "source": "feishu.json",
                                        "restart_available": False},
        }, "console.log(out);"))
        self.assertNotIn('id="fsAssistantVision" checked', off)
        self.assertNotIn("当前已开启（默认开启）", off)
        self.assertIn("当前关闭", off)

    def test_57_unconfigured_caps_group_visible_with_hint(self):
        """#31 未配置飞书凭据时「看板助手能力」分组仍可见，并给出「需先完成飞书配置」提示。"""
        out = self._node(self._form_harness({
            "path": "/tmp/feishu.json", "app_id": "", "app_secret_configured": False,
            "warnings": [], "runtime": {"state": "unknown", "source": "",
                                        "restart_available": False},
        }, "console.log(out);"))
        self.assertIn('id="fsAssistantCaps"', out)
        self.assertIn("<h3>看板助手能力</h3>", out)
        self.assertIn('id="fsAssistantFileTools"', out)
        self.assertIn('id="fsAssistantStatusWrite"', out)
        self.assertIn('id="fsAssistantReqCards"', out)
        self.assertIn('id="fsAssistantGitOps"', out)
        self.assertIn('id="fsAssistantVision"', out)
        self.assertIn("需先在上方填写 APP ID", out)
        self.assertIn('id="fsCapsCredWarn"', out)

    def test_56_save_posts_both_cap_flags(self):
        save = _extract_function(self.js, "saveFeishuSettings")
        out = self._node("\n".join([
            "const calls = [];",
            "let feishuSettingsLoaded=true; let fsBase=null;",
            "const els = {fsAppId:{value:'cli_x'}, fsAppSecret:{value:''}, fsClearSecret:{checked:false},",
            "  fsAssistantFileTools:{checked:true}, fsAssistantStatusWrite:{checked:false},",
            "  fsAssistantReqCards:{checked:true},",
            "  fsAssistantGitOps:{checked:true},",
            "  fsAssistantVision:{checked:false},",
            "  fsSaveBtn:{}, fsError:{textContent:''}, fsResult:{innerHTML:''}};",
            "global.document = {getElementById: id => els[id] || null};",
            "global.showToast = () => {};",
            "function escapeHtml(s){return String(s);}",
            "function fsErr(m){els.fsError.textContent = m || '';}",
            "function fsResult(h){els.fsResult.innerHTML = h || '';}",
            "function readFeishuSecretInput(){return String(els.fsAppSecret.value || '');}",
            "async function loadFeishuSettings(f) { calls.push(['/api/feishu-settings','LOAD']); }",
            "global.fetch = async (url, opts) => {",
            "  calls.push([url, (opts && opts.method) || 'GET', (opts && opts.body) || '']);",
            "  return {ok:true, status:200, json: async () => ({ok:true, changed:true, restart_required:false})};",
            "};",
            "async " + save,
            "saveFeishuSettings().then(() => console.log(JSON.stringify(calls)));",
        ]))
        calls = json.loads(out)
        post = [c for c in calls if c[0] == "/api/feishu-settings" and c[1] == "POST"]
        self.assertEqual(len(post), 1)
        body = json.loads(post[0][2])
        self.assertIs(body["assistant_file_tools"], True)
        self.assertIs(body["assistant_status_write"], False)
        self.assertIs(body["assistant_req_cards"], True)     # #30 第三个能力开关一并提交
        self.assertIs(body["assistant_git_ops"], True)       # #54 代码拉取与提交一并提交
        self.assertIs(body["assistant_vision"], False)       # #52 读图开关一并提交（显式关闭）
        self.assertEqual([c for c in calls if "restart" in c[0]], [])


if __name__ == "__main__":
    unittest.main()
