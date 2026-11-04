#!/usr/bin/env python3
"""操作指南入口测试：/guide 静态页可访问、内容齐全；页头含「操作指南」图标入口。
#269：/guide 含「九、内置 agent 定义」版块，提示词与 dispatcher.py/board.py 当前定义逐字一致。

运行：
    .venv/bin/python -m unittest tests.test_guide -v

import board 不触碰真实 data/board.db（init_db 只在 __main__ 调用）。
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402

# import dispatcher（新版块断言用）需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；自动补一份
from tests import _baseline  # noqa: E402

_baseline.pin()
import dispatcher  # noqa: E402,F401


class GuideRouteTest(unittest.TestCase):
    def test_guide_route_serves_static_page(self):
        resp = b.app.test_client().get("/guide")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.content_type)
        html = resp.get_data(as_text=True)
        for anchor in ("status", "flow", "format", "create", "view", "dispatcher-state"):
            self.assertIn(f'id="{anchor}"', html, anchor)
        self.assertIn("need_confirm", html)
        self.assertIn("merge_conflict", html)
        resp.close()

    def test_guide_diagrams_tab(self):
        """/guide 含「架构与调度流程图」tab 与 iframe 入口；两张图经白名单路由可取，白名单外 404"""
        c = b.app.test_client()
        resp = c.get("/guide")
        html = resp.get_data(as_text=True)
        resp.close()
        for token in ('id="tab-guide"', 'id="tab-diagrams"', 'id="pane-diagrams"',
                      "/guide/diagrams/loop-architecture.html", "/guide/diagrams/loop-dispatch-flow.html"):
            self.assertIn(token, html, token)
        for name in ("loop-architecture.html", "loop-dispatch-flow.html"):
            r = c.get(f"/guide/diagrams/{name}")
            self.assertEqual(r.status_code, 200, name)
            self.assertIn("text/html", r.content_type)
            self.assertIn(b"<svg", r.get_data())
            r.close()
        for bad in ("guide.html", "..%2Fguide.html", "loop-architecture.json", "nope.html"):
            r = c.get(f"/guide/diagrams/{bad}")
            self.assertEqual(r.status_code, 404, bad)
            r.close()

    def test_guide_diagram_pane_not_nested_in_guide_pane(self):
        """#46：图 tab 的 pane-diagrams 必须是 pane-guide 的兄弟节点。
        嵌进 pane-guide 里会被 hidden 一起藏掉（切到图 tab 后整块空白，图请求已发出但不显示）。"""
        from html.parser import HTMLParser

        class DivNesting(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack = []
                self.pane_chains = {}

            def handle_starttag(self, tag, attrs):
                if tag != "div":
                    return
                ident = dict(attrs).get("id")
                self.stack.append(ident)
                if ident in ("pane-guide", "pane-diagrams", "pane-prompts"):
                    self.pane_chains[ident] = list(self.stack[:-1])

            def handle_endtag(self, tag):
                if tag == "div" and self.stack:
                    self.stack.pop()

        resp = b.app.test_client().get("/guide")
        parser = DivNesting()
        parser.feed(resp.get_data(as_text=True))
        resp.close()
        self.assertEqual(set(parser.pane_chains), {"pane-guide", "pane-diagrams", "pane-prompts"})
        for pane, chain in parser.pane_chains.items():
            self.assertFalse(set(chain) & set(parser.pane_chains),
                             f"{pane} 嵌在其它 tab 内，切换后会整块隐藏")
        self.assertEqual(parser.pane_chains["pane-guide"], parser.pane_chains["pane-diagrams"])
        self.assertEqual(parser.pane_chains["pane-guide"], parser.pane_chains["pane-prompts"])

    def test_prompt_configuration_examples_load_with_actual_manifest_parser(self):
        """页面上的可复制 ERP 示例须能被真实加载器读取，且角色过滤/Skill 挂载符合指引。"""
        from html.parser import HTMLParser

        class Examples(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.current = None
                self.examples = {}

            def handle_starttag(self, tag, attrs):
                ident = dict(attrs).get("id", "")
                if tag == "code" and ident.startswith("prompt-") and ident.endswith("-example"):
                    self.current = ident
                    self.examples[ident] = ""

            def handle_data(self, data):
                if self.current:
                    self.examples[self.current] += data

            def handle_endtag(self, tag):
                if tag == "code":
                    self.current = None

        for url in ("/guide", "/guide?lang=en"):
            with self.subTest(url=url):
                self._check_prompt_examples(url, Examples)

    def _check_prompt_examples(self, url, Examples):
        import tempfile
        from unittest import mock

        import acp_shared as ac

        with b.app.test_client().get(url) as resp:
            page = resp.get_data(as_text=True)
        self.assertIn('id="tab-prompts"', page)
        self.assertIn('aria-controls="pane-prompts"', page)
        parser = Examples()
        parser.feed(page)
        examples = parser.examples
        manifest = json.loads(examples["prompt-manifest-example"])
        with tempfile.TemporaryDirectory(prefix="erp-prompt-guide-") as tmp:
            root = Path(tmp)
            for name, spec in manifest["guides"].items():
                text = examples[f"prompt-{name}-example"]
                self.assertLessEqual(len(text), ac.MANIFEST_GUIDE_MAX_CHARS, name)
                self.assertTrue(set(spec.get("roles", [])) <= set(ac.FLOW_CONFIG["roles"]))
                path = root / spec["file"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            skill = root / ".loop/skills/erp-module.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(examples["prompt-skill-example"], encoding="utf-8")
            self.assertLessEqual(len(examples["prompt-skill-example"]), ac.MANIFEST_GUIDE_MAX_CHARS)
            self.assertTrue(set(manifest["skills"]) <= set(ac.FLOW_CONFIG["roles"]))
            (root / ".loop/project.json").write_text(json.dumps(manifest), encoding="utf-8")
            loaded = ac.load_project_manifest(root)
            self.assertEqual(loaded["errors"], [])
            self.assertEqual(set(loaded["guides"]), {"business", "framework", "validation", "instance"})
            self.assertEqual(ac.skill_text("erp-module", [root]), examples["prompt-skill-example"])
            with mock.patch.object(dispatcher, "_project_repos", return_value=[root]):
                for role in ("analysis", "tech", "tech_review", "dev", "code_review", "accept"):
                    prompt = dispatcher.project_conventions_text("erp-example", role)
                    for name in ("business", "framework", "validation"):
                        self.assertIn(examples[f"prompt-{name}-example"], prompt, (role, name))
                    self.assertEqual(examples["prompt-instance-example"] in prompt,
                                     role in ("dev", "accept"), role)
                    skills = dispatcher.scenario_skills_text("erp-example", role)
                    self.assertEqual(examples["prompt-skill-example"] in skills,
                                     role in manifest["skills"], role)

    def test_guide_agents_section(self):
        """#269：guide 页含「九、内置 agent 定义」版块，6 角色提示词 + 公共尾段 + 建卡提示词全部可见，
        展示内容与 dispatcher.py / board.py 当前定义逐字一致（转义后比较）"""
        import html as html_mod

        resp = b.app.test_client().get("/guide")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="agents"', html)
        # 每角色一个折叠块 + 提示词全文（与 ROLE_PROMPTS 逐字一致）；#419 后 dev 单块，不再按通道拆行
        for role in b._AGENT_OVERVIEW:
            self.assertIn(f"<summary><code>{role}</code>", html, role)
            self.assertIn(html_mod.escape(dispatcher.ROLE_PROMPTS[role].strip(), quote=False), html, role)
        self.assertNotIn("<summary><code>dev_auto</code>", html)
        # 公共尾段 + 建卡对话提示词
        self.assertIn("<summary><code>COMMON_TAIL</code>", html)
        self.assertIn(html_mod.escape(dispatcher.COMMON_TAIL.strip(), quote=False), html)
        self.assertIn("<summary><code>chat</code> 建卡对话 agent", html)
        self.assertIn("_chat_system_prompt", html)
        # 占位符说明表（build_prompt 填充的占位符可见）
        for ph in ("{project}", "{notes}", "{analysis_md}", "{timeout_minutes}", "{history}", "{extra}", "{workdir}"):
            self.assertIn(ph, html)
        resp.close()

    def test_guide_flows_section_rendered_from_config(self):
        """#419：guide 内置版块「流程与角色」按 flows.json 配置渲染（改配置即改展示）"""
        import acp_shared as ac

        resp = b.app.test_client().get("/guide")
        self.assertEqual(resp.status_code, 200)
        page = resp.get_data(as_text=True)
        for f in ac.flows():
            self.assertIn(f'<code>{f["id"]}</code>', page, f["id"])
            self.assertIn(f["name"], page, f["id"])
        self.assertIn("<b>默认</b>", page)                       # 状态列：默认流程
        self.assertIn("已启用", page)                             # 状态列：启用流程显式标注
        self.assertIn("<th>状态</th>", page)                      # 表头：状态列（原「默认」列改成三态）
        self.assertIn("适用场景", page)                           # #419：流程表新增 description 列
        for f in ac.flows():
            self.assertIn(f["description"], page, f["id"])        # 适用场景文案来自 flows.json
        self.assertIn("收尾（收尾）", page)                       # terminal 节点 = 收尾名 + 收尾类型
        self.assertIn("技术方案（产物）", page)                   # 节点 = 角色 display + node_type 中文
        self.assertIn("方案评审（评审）", page)
        # 角色表按配置逐角色渲染（#423：review 已合并进 code_review，不再有同名两行）
        # 只统计动态角色表：其它 tab 可以在配置指引中引用相同角色 ID。
        role_table = page.split("<h3>角色</h3>", 1)[1].split("</table>", 1)[0]
        for role in b._AGENT_OVERVIEW:
            self.assertIn(f'<tr><td><code>{role}</code></td>', role_table, role)
        self.assertEqual(role_table.count("<tr><td><code>code_review</code></td>"), 1)
        self.assertNotIn("<tr><td><code>review</code></td>", role_table, "review 角色已删除，不得再出现在角色表")
        self.assertIn("<td><code>code_review.md</code></td>", page)   # 角色表产物列
        self.assertIn(ac.pre_role(), page)                       # 前置槽位角色（配置值）
        resp.close()

    def test_guide_marks_disabled_flow(self):
        """软禁用的流程仍在流程表里（定义保留、可一键恢复），但标注「已禁用」

        生产配置当前无禁用流程；另以 standard 注入一份禁用配置，验证标记随配置走
        （禁用对象取非默认流程——状态列优先显示「默认」，拿默认流程验「已禁用」验不出来）。"""
        import acp_shared as ac
        from unittest import mock

        page = b.app.test_client().get("/guide").get_data(as_text=True)
        self.assertNotIn("<b>已禁用</b>", page)                    # 生产配置当前无禁用流程
        flows = [dict(f, enabled=False) if f["id"] == "standard" else f for f in ac.flows()]
        with mock.patch.dict(ac.FLOW_CONFIG, {"flows": flows}):
            page2 = b.app.test_client().get("/guide").get_data(as_text=True)
        self.assertIn("<b>已禁用</b>", page2)
        self.assertIn("<code>standard</code>", page2)

    def test_guide_renders_pre_slot_contract(self):
        """前置环节输出契约在 guide 里可见：analysis 模板写着「见下方『前置环节输出契约』段」，
        若本页不渲染该段，引用就是悬空的（#419 遗留）"""
        import acp_shared as ac

        page = b.app.test_client().get("/guide").get_data(as_text=True)
        self.assertIn("PRE_SLOT_CONTRACT", page)
        self.assertIn("前置环节输出契约", page)
        self.assertIn('"suggested_flow"', page)
        self.assertIn('"suggested_flow_reason"', page)
        self.assertIn('"repos"', page)
        for f in ac.enabled_flows():                             # 目录 = 配置里的启用流程
            self.assertIn(f"- {f['id']}（{f['name']}）", page, f["id"])

    def test_guide_effective_model_column(self):
        """生效模型列（#419 兜底链标注）：roles.<id>.model（配置）→ KIMI_MODEL_<ROLE>（环境变量）
        → 全局 ACP_MODEL；配置非空时优先于环境变量，来源随最高优先级来源标注"""
        import acp_shared as ac
        from unittest import mock

        page = b.app.test_client().get("/guide").get_data(as_text=True)
        self.assertIn("生效模型", page)
        self.assertIn("（全局默认）", page)                        # 生产配置未配 per-role 模型
        self.assertIn(ac.ACP_MODEL, page)                         # 全局默认值可见
        # 配置优先于环境变量：flows.json 配了 model 时，同角色环境变量不生效
        roles = dict(ac.FLOW_CONFIG["roles"])
        roles["dev"] = dict(roles["dev"], model="alias/strong-test")
        with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}), \
                mock.patch.dict("os.environ", {"KIMI_MODEL_DEV": "alias/env-test"}):
            page2 = b.app.test_client().get("/guide").get_data(as_text=True)
        self.assertIn("alias/strong-test（角色配置）", page2)
        self.assertNotIn("alias/env-test", page2)
        # 无配置仅环境变量：来源标注为 KIMI_MODEL_<ROLE>
        with mock.patch.dict("os.environ", {"KIMI_MODEL_ANALYSIS": "alias/env-test"}):
            page3 = b.app.test_client().get("/guide").get_data(as_text=True)
        self.assertIn("alias/env-test（环境变量 KIMI_MODEL_ANALYSIS）", page3)

    def test_guide_runtime_env_section(self):
        """运行时环境与工具版块：全局模型/kimi 路径/内置只读工具/playwright 白名单可见；
        mcp.json 读取失败时该行降级为提示文案、页面仍 200（展示层 fail-safe）"""
        from unittest import mock

        page = b.app.test_client().get("/guide").get_data(as_text=True)
        self.assertIn("运行时环境与工具", page)
        self.assertIn(str(b.KIMI), page)
        self.assertIn("list_dir", page)                           # 内置只读工具清单可见
        self.assertIn("browser_navigate", page)                   # playwright 白名单可见
        # #50：思考档位统一模型默认——全局推理深度行下线，全局模型行保留
        self.assertIn("全局模型 ACP_MODEL", page)
        self.assertNotIn("全局推理深度", page)
        self.assertNotIn("KIMI_THINKING", page)
        # harness home 指向不存在目录 → mcp 行降级，页面仍 200
        with mock.patch.dict("os.environ", {"KIMI_CODE_HOME": "/nonexistent-harness-home"}):
            resp = b.app.test_client().get("/guide")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("未配置（只读工具不受影响", resp.get_data(as_text=True))
            resp.close()

    def test_guide_assistant_git_ops_capability(self):
        """看板助手能力文案为四项，含代码拉取与提交；旧「三项默认关闭」不残留。"""
        page = b.app.test_client().get("/guide").get_data(as_text=True)
        self.assertIn("代码拉取与提交", page)
        self.assertIn("四项默认关闭", page)
        self.assertNotIn("三项默认关闭", page)
        # 能力清单写明两步提交与边界（指南已去掉内部卡号，只锁行为描述）
        self.assertIn("下一轮才真正提交", page)
        self.assertIn("不 push、不切换分支", page)

    def test_guide_language_selection(self):
        """/guide 语言：?lang= 优先；否则按 Accept-Language 首选语言；都没有时默认中文。
        两版都注入动态版块，都带对向语言切换链接。"""
        c = b.app.test_client()
        cases = [
            ("/guide", {}, "zh"),
            ("/guide?lang=en", {}, "en"),
            ("/guide?lang=zh", {"Accept-Language": "en-US,en;q=0.9"}, "zh"),
            ("/guide", {"Accept-Language": "en-US,en;q=0.9"}, "en"),
            ("/guide", {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}, "zh"),
            ("/guide?lang=bogus", {"Accept-Language": "fr-FR"}, "en"),
        ]
        for url, headers, want in cases:
            with self.subTest(url=url, headers=headers):
                resp = c.get(url, headers=headers)
                self.assertEqual(resp.status_code, 200)
                page = resp.get_data(as_text=True)
                resp.close()
                if want == "en":
                    self.assertIn('<html lang="en">', page)
                    self.assertIn('href="/guide?lang=zh"', page)
                    self.assertEqual(resp.headers["Content-Language"], "en")
                else:
                    self.assertIn('<html lang="zh-CN">', page)
                    self.assertIn('href="/guide?lang=en"', page)
                    self.assertEqual(resp.headers["Content-Language"], "zh-CN")
                self.assertIn('id="agents"', page)                 # 动态版块两版都注入
                self.assertNotIn("<!-- builtin-agents-section -->", page)
                self.assertIn("Accept-Language", resp.headers.get("Vary", ""))

    def test_guide_en_falls_back_to_zh_when_missing(self):
        """英文版文件缺失时 ?lang=en 回落中文版（不 404）"""
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="guide-lang-"))
        old = b.GUIDE_HTML
        try:
            (tmp / "guide.html").write_text('<html lang="zh-CN"><!-- builtin-agents-section --></html>',
                                            encoding="utf-8")
            b.GUIDE_HTML = tmp / "guide.html"
            resp = b.app.test_client().get("/guide?lang=en")
            self.assertEqual(resp.status_code, 200)
            self.assertIn('lang="zh-CN"', resp.get_data(as_text=True))
            resp.close()
        finally:
            b.GUIDE_HTML = old
            import shutil as _shutil
            _shutil.rmtree(tmp, ignore_errors=True)

    def test_guide_en_has_same_sections(self):
        """英文版与中文版锚点一致（同一份内容的两种语言），且不含内部卡号"""
        import re
        zh = (REPO / "docs" / "guide.html").read_text(encoding="utf-8")
        en = (REPO / "docs" / "guide.en.html").read_text(encoding="utf-8")
        ids = lambda s: set(re.findall(r'\bid="([^"]+)"', s))
        self.assertEqual(ids(zh) - {"agents-live"}, ids(en) - {"agents-live"})
        for page in (zh, en):
            text = re.sub(r"<style>.*?</style>", "", page, flags=re.S)   # 色值不算卡号
            self.assertIsNone(re.search(r"#\d{2,}", text), "指南不应出现内部卡号")
            self.assertIn("<!-- builtin-agents-section -->", page)

    def test_header_guide_link_carries_ui_language(self):
        """页头指南链接由 i18n.js 按界面语言补 ?lang=（服务端渲染保持 /guide，见上一条测试）"""
        js = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
        self.assertIn("a.guide-link", js)
        self.assertIn("'/guide?lang=' + lang", js)

    def test_index_header_has_guide_link(self):
        resp = b.app.test_client().get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('href="/guide"', html)
        # 入口已图标化：保留 title/aria-label 提示，不再有「操作指南」文字链接
        self.assertIn('class="guide-link"', html)
        self.assertIn('title="操作指南"', html)
        self.assertIn('aria-label="操作指南"', html)
        self.assertNotIn('>操作指南</a>', html)

    def test_guide_documents_feishu_confirm_command(self):
        """#22：飞书建卡三指令（结束建卡/新建建卡/确认建卡）必须在 guide 与 dispatcher.md 齐备。
        改 on_message 行为必须同步 guide（AGENTS.md 红线），故用测试锁定。"""
        resp = b.app.test_client().get("/guide")
        page = resp.get_data(as_text=True)
        resp.close()
        for cmd in ("结束建卡", "新建建卡", "确认建卡"):
            self.assertIn(cmd, page, f"guide 缺少飞书建卡指令 {cmd}")
        self.assertIn("/confirm", page)
        doc = (REPO / "docs" / "dispatcher.md").read_text(encoding="utf-8")
        for cmd in ("结束建卡", "新建建卡", "确认建卡"):
            self.assertIn(cmd, doc, f"docs/dispatcher.md 缺少飞书建卡指令 {cmd}")

    def test_guide_feishu_row_uses_dispatcher_runtime_state(self):
        """#23：guide 运行时环境表的飞书行改按调度器真实运行态渲染（不再凭文件可读就说「已接飞书」）。"""
        import json as _json
        import tempfile
        import time as _time
        from pathlib import Path as _Path
        tmp = _Path(tempfile.mkdtemp(prefix="req23-guide-"))
        old_cfg, old_hb = b.FEISHU_JSON_PATH, b.HEARTBEAT_FILE
        b.FEISHU_JSON_PATH = tmp / "feishu.json"
        b.HEARTBEAT_FILE = tmp / "heartbeat.json"
        try:
            # ① 有效凭据 + 心跳未加载 → 「凭据未加载」（不得出现「已接飞书」）
            b.FEISHU_JSON_PATH.write_text(_json.dumps({"app_id": "cli_g1", "app_secret": "s1"}),
                                          encoding="utf-8")
            b.HEARTBEAT_FILE.write_text(_json.dumps({
                "last_poll_at": _time.time(),
                "feishu": {"enabled": True, "connected": False, "phase": "connecting",
                           "credentials_fingerprint": None}}), encoding="utf-8")
            row = [r for r in b._runtime_env_rows() if r[0] == "飞书渠道"][0][1]
            self.assertIn("凭据未加载", row)
            self.assertNotIn("已接飞书", row)
            self.assertNotIn("未接飞书", row)
            # ② 指纹一致 + connected → 已连接
            b.HEARTBEAT_FILE.write_text(_json.dumps({
                "last_poll_at": _time.time(),
                "feishu": {"enabled": True, "connected": True, "phase": "connected",
                           "credentials_fingerprint": b._feishu_credential_fingerprint("cli_g1", "s1")}}),
                encoding="utf-8")
            row2 = [r for r in b._runtime_env_rows() if r[0] == "飞书渠道"][0][1]
            self.assertIn("已连接", row2)
            # ③ 无凭据 → 凭据未配置 + 原因
            b.FEISHU_JSON_PATH.unlink()
            row3 = [r for r in b._runtime_env_rows() if r[0] == "飞书渠道"][0][1]
            self.assertIn("凭据未配置", row3)
            self.assertNotIn("未接飞书", row3)
            self.assertNotIn("已接飞书", row3)
        finally:
            b.FEISHU_JSON_PATH, b.HEARTBEAT_FILE = old_cfg, old_hb
            import shutil as _shutil
            _shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
