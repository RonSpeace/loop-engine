#!/usr/bin/env python3
"""会话角色守卫测试（#279 现场防复发）：spawn_agent/run_agent 校验 role 与需求 mode/phase 匹配，
拦截绕过调度器直接调用拉起错误角色会话（如 auto 需求被拉 accept「测试验证」）。
同时覆盖人工续跑（_run_manual_command）在角色不匹配时标记 failed。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_role_guard -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899（#343）

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

# 各测试类会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_req", "post_dispatcher_event", "post_event",
    "build_prompt", "ensure_dev_worktrees", "find_resume_sid", "threading", "ACPSession",
    "kimi_version",
    "update_session_info", "sync_agent_session", "post_run_record", "handle_result",
    "result_state", "requirement_timeout", "api_board", "run_agent")}


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fake_req(rid=279999, mode=None, phase=None, flow=None):
    req = {"id": rid, "project": "req-board", "title": "T"}
    if mode:
        req["mode"] = mode
    if phase:
        req["phase"] = phase
    if flow:
        req["flow"] = flow
    return req


class RoleAllowedTest(unittest.TestCase):
    def test_current_node_only_dev(self):
        # auto 流程已删除：卡当前节点为 dev 改用「standard 流程 + phase=dev」表达
        req = fake_req(mode="semi", phase="dev", flow="standard")
        self.assertTrue(d.role_allowed(req, "dev"))
        self.assertFalse(d.role_allowed(req, "accept"))
        self.assertFalse(d.role_allowed(req, "analysis"))
        self.assertFalse(d.role_allowed(req, "proposal"))

    def test_semi_phase_match(self):
        req = fake_req(mode="semi", phase="dev")
        self.assertTrue(d.role_allowed(req, "dev"))
        self.assertFalse(d.role_allowed(req, "accept"))
        req2 = fake_req(mode="semi", phase="accept")
        self.assertTrue(d.role_allowed(req2, "accept"))
        self.assertFalse(d.role_allowed(req2, "dev"))

    def test_no_mode_only_analysis(self):
        req = fake_req()
        self.assertTrue(d.role_allowed(req, "analysis"))
        self.assertFalse(d.role_allowed(req, "dev"))


class SpawnAgentGuardTest(unittest.TestCase):
    def setUp(self):
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_todos": {}}
        self.calls = []
        d.post_dispatcher_event = lambda *a, **k: self.calls.append(("event", a, k))
        d.set_req = lambda *a, **k: self.calls.append(("set_req", a, k))
        self.threads = []
        d.threading.Thread = lambda target=None, args=(), kwargs=None, **kw: mock.Mock(
            start=lambda: self.threads.append((target, args)))
        # spawn_agent 放行分支会真调 build_prompt → 真建 worktree + 真建分支（#419 清理现场：
        # 本类跑全量时在 ~/.req-board/worktrees 下留下 req-279999）。隔离掉，tearDown 恢复。
        wd = Path("/tmp/reqguard-wd")
        d.ensure_dev_worktrees = lambda req: (wd, {"req-board": wd})

    def tearDown(self):
        _restore_d()

    def test_reject_illegal_role(self):
        """auto 需求被显式拉 accept：拒绝、不置 doing、不启线程、留告警事件"""
        d.spawn_agent(fake_req(mode="auto"), role="accept")
        self.assertTrue(any(c[0] == "event" for c in self.calls))
        self.assertFalse(any(c[0] == "set_req" for c in self.calls))
        self.assertEqual(self.threads, [])

    def test_reject_illegal_role_without_explicit(self):
        """role 未传但 mode/phase 不匹配（role_for 兜底取出的角色恒匹配，此处验证无 role 时放行）"""
        d.spawn_agent(fake_req(mode="auto"))
        self.assertEqual(len(self.threads), 1)

    def test_allow_legal_role(self):
        d.spawn_agent(fake_req(mode="semi", phase="dev", flow="standard"), role="dev")
        self.assertEqual(len(self.threads), 1)
        d.spawn_agent(fake_req(mode="semi", phase="accept", flow="standard"), role="accept")
        self.assertEqual(len(self.threads), 2)


class RunAgentGuardTest(unittest.TestCase):
    def setUp(self):
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_todos": {}}
        self.events = []
        d.post_event = lambda *a, **k: self.events.append((a, k))
        d.ACPSession = mock.Mock()  # 若校验失效走到构造，本类测试立即暴露

    def tearDown(self):
        _restore_d()

    def test_reject_illegal_role(self):
        """直接调 run_agent(req, "accept") 且需求 auto：返回 False、写拒绝事件、不构造会话"""
        ret = d.run_agent(fake_req(mode="auto"), "accept", "prompt", time.time())
        self.assertFalse(ret)
        self.assertTrue(any("拒绝拉起非法角色" in a[2] for a, k in self.events))
        d.ACPSession.assert_not_called()

    def test_allow_legal_role_returns_true(self):
        """合法路径（standard+dev）不误拦：会话全链路 mock，run_agent 返回 True"""
        sess = mock.Mock()
        sess.proc.pid = 1
        sess.session_id = "s_guard_test"
        sess.start.return_value = False
        sess.collect_usage.return_value = None
        d.ACPSession.return_value = sess
        d.kimi_version = lambda: None
        d.update_session_info = lambda *a, **k: None
        d.sync_agent_session = lambda *a, **k: None
        d.post_run_record = lambda *a, **k: None
        d.handle_result = lambda *a, **k: None
        d.result_state = lambda req: ("ok", "")
        d.requirement_timeout = lambda req: 30
        ret = d.run_agent(fake_req(mode="semi", phase="dev", flow="standard"), "dev", "prompt", time.time())
        self.assertTrue(ret)
        sess.start.assert_called_once()


class ManualCommandGuardTest(unittest.TestCase):
    def setUp(self):
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_todos": {}}
        self.calls = []
        d.api_board = lambda method, path, body=None: self.calls.append((method, path, body))
        d.run_agent = lambda *a, **k: False  # 模拟 run_agent 角色校验拦截

    def tearDown(self):
        _restore_d()

    def test_manual_command_failed_on_guard_reject(self):
        """人工续跑被角色守卫拦截：命令标记 failed 而非 done，且不继续"""
        cmd = {"id": 9, "req_id": 279999, "agent_session_id": 3, "role": "accept", "text": "继续"}
        row = {"id": 3, "session_id": "s_old"}
        d.api_board = lambda method, path, body=None: self.calls.append((method, path, body)) or (
            [row] if method == "GET" and "/sessions" in path else None)
        d._run_manual_command(cmd, fake_req(mode="auto"), "accept", "继续")
        self.assertTrue(any(m == "PATCH" and b and b.get("status") == "failed"
                            for m, p, b in self.calls))
        self.assertFalse(any(m == "PATCH" and b and b.get("status") == "done"
                             for m, p, b in self.calls))


if __name__ == "__main__":
    unittest.main()
