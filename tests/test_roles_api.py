#!/usr/bin/env python3
"""GET /api/roles（#404）端点测试：角色显示名/顺序注入数据（单一源 acp_shared.ROLE_CAPABILITIES 的 display/order）。

- 正常：200、JSON 数组、键集 == ROLE_CAPABILITIES、按 order 升序、每项含 role/display/order；
- fail-safe：某键缺 display → 该条回退原文、端点仍 200（缺 order → 排尾部），不漏返回 500；
  字段完备性（漏填即测试红）由 tests/test_role_manifest.py 的 ManifestCompletenessTest 承担。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_roles_api -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import acp_shared as ac  # noqa: E402
import board as b  # noqa: E402


class RolesApiTest(unittest.TestCase):
    def test_api_roles_returns_sorted_list(self):
        resp = b.app.test_client().get("/api/roles")
        self.assertEqual(resp.status_code, 200)
        items = resp.get_json()
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), len(ac.ROLE_CAPABILITIES))
        for r in items:
            self.assertIn("role", r)
            self.assertIn("display", r)
            self.assertIn("order", r)
        self.assertEqual(tuple(r["role"] for r in items),
                         tuple(r["role"] for r in b._role_injection()))
        orders = [r["order"] for r in items]
        self.assertEqual(orders, sorted(orders), "返回必须按 order 升序")

    def test_api_roles_missing_field_fallback(self):
        """mock 某键在配置里缺 display/order → 该条 display 回退原文、order 排尾部、端点仍 200
        （#419：展示字段唯一源 = flows.json roles.<id>，fail-safe 降级路径不变）"""
        roles = dict(ac.FLOW_CONFIG["roles"])
        roles["analysis"] = {"display": "", "order": None, "artifact": "analysis.md",
                             "model": "", "prompt_file": "prompts/analysis.md", "prompt": ""}
        with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
            resp = b.app.test_client().get("/api/roles")
            self.assertEqual(resp.status_code, 200)
            items = {r["role"]: r for r in resp.get_json()}
            self.assertEqual(items["analysis"]["display"], "analysis")  # 回退原文
            self.assertEqual(items["analysis"]["order"], 999)           # 缺 order 排尾部
            self.assertEqual(items["dev"]["display"], ac.role_meta("dev", "display"))


if __name__ == "__main__":
    unittest.main()
