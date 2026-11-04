#!/usr/bin/env python3
"""建卡 skill 的 open 硬闸回归测试（建卡 #423 时的现场）：

自动开发项目用 `--status open` 必须 `--user-request` 引用用户明确要求开发的原话，否则
不建卡并非 0 退出。原实现只 print WARN 后照常 POST（护栏而非机制保证，见 AGENTS.md #336
口径），本测试锁定「护栏 → 机制」这一行为，防回退。

用 monkeypatch 替换 create_card.request 与 sys.argv，不起网络、不碰真实看板与 data/board.db。

运行：
    .venv/bin/python -m unittest tests.test_card_open_gate -v
"""
import contextlib
import io
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = REPO / "skills" / "req-board-card" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

import create_card as cc  # noqa: E402


def _stub_request(posts, projects=("req-board", "other-project"), auto=("req-board",),
                  auto_status=200):
    """桩 request：记录 POST payload，其余端点返回固定结构。"""
    def fake(url, method="GET", payload=None):
        if url.endswith("/api/projects"):
            return 200, list(projects)
        if url.endswith("/api/auto-projects"):
            if auto_status != 200:
                return auto_status, {"error": "boom"}
            return 200, list(auto)
        if url.endswith("/api/requirements") and method == "POST":
            posts.append(payload)
            return 201, dict(payload, id=9001)
        if "/api/requirements?" in url:
            return 200, [{"id": 9001}]
        return 404, {}
    return fake


class OpenGateTest(unittest.TestCase):
    def setUp(self):
        self._argv = sys.argv
        self._request = cc.request
        self.posts = []
        cc.request = _stub_request(self.posts)

    def tearDown(self):
        sys.argv = self._argv
        cc.request = self._request

    def _run(self, *args):
        """跑 main()，返回 (退出码 or None, stdout)。None = 正常返回。"""
        sys.argv = ["create_card.py", "--title", "T", *args]
        buf = io.StringIO()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                cc.main()
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    # ---- 硬闸：自动开发项目 + open ----

    def test_open_without_user_request_blocked(self):
        """自动开发项目 open 无 --user-request：退出码 3 且不建卡。"""
        code, out = self._run("--project", "req-board", "--status", "open")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.posts, [], "被拦截时不得发出建卡 POST")
        self.assertIn("[ERROR]", out)
        self.assertIn("--user-request", out)

    def test_open_with_empty_user_request_blocked(self):
        """--user-request 传空白串等同未传（strip 后判定）。"""
        code, _ = self._run("--project", "req-board", "--status", "open",
                            "--user-request", "   ")
        self.assertEqual(code, 3)
        self.assertEqual(self.posts, [])

    def test_open_with_user_request_allowed(self):
        """带用户原话时放行，且 POST status=open。"""
        code, out = self._run("--project", "req-board", "--status", "open",
                              "--user-request", "这个需求开始开发")
        self.assertIsNone(code, out)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["status"], "open")

    def test_auto_endpoint_unavailable_fail_closed(self):
        """判定不了是否接入自动开发（接口 500）时按接入处理：仍拦截。"""
        cc.request = _stub_request(self.posts, auto_status=500)
        code, out = self._run("--project", "req-board", "--status", "open")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.posts, [])
        self.assertIn("无法确认", out)

    # ---- 不受影响的分支（回归） ----

    def test_later_not_blocked(self):
        """默认 later 不受硬闸影响，无需 --user-request。"""
        code, out = self._run("--project", "req-board", "--status", "later")
        self.assertIsNone(code, out)
        self.assertEqual(self.posts[0]["status"], "later")

    def test_non_auto_project_open_allowed(self):
        """未接入自动开发的项目 open 不会被调度器捞走，不做要求。"""
        code, out = self._run("--project", "other-project", "--status", "open")
        self.assertIsNone(code, out)
        self.assertEqual(self.posts[0]["status"], "open")

    # ---- 判定函数三态 ----

    def test_requires_user_request_helper(self):
        self.assertTrue(cc.requires_user_request("open", ["req-board"], "req-board"))
        self.assertTrue(cc.requires_user_request("open", None, "req-board"))    # fail-closed
        self.assertFalse(cc.requires_user_request("open", ["other"], "req-board"))
        self.assertFalse(cc.requires_user_request("later", ["req-board"], "req-board"))
        self.assertFalse(cc.requires_user_request("done", None, "req-board"))


if __name__ == "__main__":
    unittest.main()
