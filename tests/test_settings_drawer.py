#!/usr/bin/env python3
"""#3 全局设置抽屉测试：GET /api/settings-drawer 四分组（只读）渲染与 fail-safe。

B1 端点 200 + 四分组标题；B2 工作流组与 /guide 流程表逐字相等（共享 helper 单一源）；
B3 角色组 8 行（键/中文名/顺序/产物/生效模型/权限档位，单一源派生）；
B4 生效模型来源标注（复用 _effective_model_label 兜底链）；
B5 提示词 8 块折叠默认收起、正文转义、注入不裸渲染；
B6 运行时组与 /guide 运行时表逐字相等 + mcp 读取失败降级；
B7 某角色提示词缺失 → warn 提示 + 其余照常；B8 flows 配置非法 → 流程组降级提示不抛。

不触碰 DB / projects.json（纯展示层）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_settings_drawer.py -q
"""
import html as html_mod
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import acp_shared as ac  # noqa: E402
import board as b  # noqa: E402


def esc(s):
    return html_mod.escape(s, quote=False)


class SettingsDrawerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = b.app.test_client()

    def _drawer(self):
        resp = self.client.get("/api/settings-drawer")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("html", data)
        return data["html"]

    def _guide(self):
        resp = self.client.get("/guide")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_b1_four_groups(self):
        """B1：200 + {"html"} 含工作流/角色/提示词/运行时环境四个分组标题"""
        h = self._drawer()
        for title in ("工作流", "角色", "提示词", "运行时环境与工具"):
            self.assertIn(f"<h3>{title}</h3>", h, title)
        self.assertEqual(h.count('<div class="sd-group">'), 4)

    def test_b2_flows_table_equals_guide(self):
        """B2：工作流组流程表与 /guide 的 <table class="flows"> 逐字相等；
        每流程 id/名称/描述/节点序列（角色中文名 + node_type 中文）齐全，默认/启用标记在"""
        h = self._drawer()
        guide_table = re.search(r'<table class="flows">.*?</table>', self._guide(), re.S).group(0)
        self.assertIn(guide_table, h)
        for f in ac.flows():
            self.assertIn(f'<code>{f["id"]}</code>', h, f["id"])
            self.assertIn(f["name"], h, f["id"])
            self.assertIn(f["description"], h, f["id"])
        self.assertIn("<b>默认</b>", h)
        self.assertIn("已启用", h)
        self.assertIn("收尾（收尾）", h)
        self.assertIn("技术方案（产物）", h)

    def test_b3_roles_table_single_source(self):
        """B3：角色组 8 行；逐行 键/中文名/顺序/产物/生效模型==_effective_model_label/权限==_AGENT_OVERVIEW"""
        h = self._drawer()
        self.assertEqual(len(b._AGENT_OVERVIEW), len(ac.ROLE_CAPABILITIES))
        for role in sorted(ac.ROLE_CAPABILITIES):
            m = re.search(r"<tr><td><code>%s</code></td>(.*?)</tr>" % re.escape(role), h, re.S)
            self.assertIsNotNone(m, "角色 %s 无表格行" % role)
            seg = m.group(1)
            self.assertIn(esc(b._role_display(role)), seg, role)
            order = ac.role_meta(role, "order")
            self.assertIn(f"<td>{esc(str(order) if order is not None else '')}</td>", seg, role)
            fallback = (b._AGENT_OVERVIEW.get(role) or (None, None))[1]
            artifact = b._role_artifact_label(role, fallback)
            self.assertIn(f"<td><code>{esc(artifact)}</code></td>", seg, role)
            self.assertIn(esc(b._effective_model_label(role)), seg, role)
            perm = (b._AGENT_OVERVIEW.get(role) or ("—", None))[0]
            self.assertIn(esc(perm), seg, role)

    def test_b4_effective_model_source_label(self):
        """B4：mock roles.dev.model → 生效模型带「（角色配置）」来源标注（复用兜底链）"""
        roles = dict(ac.FLOW_CONFIG["roles"])
        roles["dev"] = dict(roles["dev"], model="alias/x")
        with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
            h = self._drawer()
        self.assertIn("alias/x（角色配置）", h)

    def test_b5_prompts_collapsed_and_escaped(self):
        """B5：8 个 <details> 默认收起；<pre> 全文 == 转义后的 role_prompt；
        注入 <script> 被转义（无裸标签）"""
        h = self._drawer()
        self.assertEqual(h.count("<details>"), len(ac.ROLE_CAPABILITIES))
        self.assertNotIn("<details open", h)
        self.assertEqual(h.count("</details>"), len(ac.ROLE_CAPABILITIES))
        for role in ac.ROLE_CAPABILITIES:
            prompt = ac.role_prompt(role)
            self.assertTrue(prompt, "角色 %s 提示词为空，B5 前置不成立" % role)
            self.assertIn(f"<pre>{esc(prompt.strip())}</pre>", h, role)
        roles = {k: dict(v) for k, v in ac.FLOW_CONFIG["roles"].items()}
        roles["dev"]["prompt"] = "<script>alert(1)</script>"
        with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
            h2 = self._drawer()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", h2)
        self.assertNotIn("<script>alert", h2)

    def test_b6_runtime_table_equals_guide(self):
        """B6：运行时组与 /guide 运行时表逐字相等；KIMI_CODE_HOME 不存在 → mcp 行降级 + 200"""
        h = self._drawer()
        guide_table = re.search(
            r"<table>\n<tr><th>项</th><th>当前值（本进程环境实时读取）</th></tr>\n.*?</table>",
            self._guide(), re.S).group(0)
        self.assertIn(guide_table, h)
        with mock.patch.dict(os.environ, {"KIMI_CODE_HOME": "/nonexistent-harness-home"}):
            h2 = self._drawer()
        self.assertIn("未配置（只读工具不受影响", h2)

    def test_b7_missing_prompt_degrades(self):
        """B7：某角色提示词为空 → warn 提示 + 其余角色照常 + 200"""
        roles = {k: dict(v) for k, v in ac.FLOW_CONFIG["roles"].items()}
        roles["dev"]["prompt"] = ""
        with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
            h = self._drawer()
        self.assertIn("以下角色提示词为空", h)
        self.assertIn("dev", h.split("以下角色提示词为空")[1][:200])
        self.assertEqual(h.count("<details>"), len(ac.ROLE_CAPABILITIES) - 1)
        self.assertIn("analysis", h)

    def test_b8_invalid_flow_config_degrades(self):
        """B8：flow 配置不可用（FLOW_CONFIG=None）→ 200 + 流程组降级提示，不抛"""
        with mock.patch.object(ac, "FLOW_CONFIG", None):
            h = self._drawer()
        self.assertIn("当前无可用流程", h)
        self.assertIn("<h3>角色</h3>", h)          # 其余分组照常
        self.assertIn("<h3>提示词</h3>", h)
        self.assertIn("<h3>运行时环境与工具</h3>", h)

    def test_b9_roles_table_unknown_role_fallback(self):
        """B9（评审反馈 4）：_AGENT_OVERVIEW 未登记角色（配置层被 mock 注入）不 KeyError，
        权限档位兜底「—」，端点仍 200"""
        roles = {k: dict(v) for k, v in ac.FLOW_CONFIG["roles"].items()}
        caps = {k: v for k, v in ac.ROLE_CAPABILITIES.items()}
        caps["newrole"] = dict(caps["dev"])
        roles["newrole"] = dict(roles["dev"], display="新角色", order=77,
                                prompt_file="prompts/dev.md", prompt="x")
        with mock.patch.object(b, "ROLE_CAPABILITIES", caps), \
                mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
            h = self._drawer()
        m = re.search(r"<tr><td><code>newrole</code></td>(.*?)</tr>", h, re.S)
        self.assertIsNotNone(m)
        self.assertIn("<td>—</td>", m.group(1))


if __name__ == "__main__":
    unittest.main()
