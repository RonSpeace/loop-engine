# -*- coding: utf-8 -*-
"""ACP terminal 服务测试（upstream#351）：kimi 0.37.2 起 Bash 工具经 ACP 协议委托客户端执行，
dispatcher/看板作为客户端提供 terminal/create|output|wait_for_exit|kill|release|input。

验证点：
- dev 角色 create → output 累积 → wait_for_exit 退出码（正常命令）
- 输出超限截断（outputByteLimit）
- 只读角色：白名单命令放行、白名单外拒绝
- 全员：进程清理红线拒绝（kill/pkill 命中 kimi）
- dev 角色：写主工作区护栏拒绝
- no_commit 角色：提交类命令拒绝
- kill 后 wait 返回、release 清理、会话 close 清理孤儿进程
- _handle_request 路由与错误返回
"""
import itertools
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import acp_shared as ac
import dispatcher as d  # noqa: E402（桩实例用 dispatcher 导出的 ACPSession）


def _session_stub(role="dev", readonly=False, main_ws_roots=None, workdir=None):
    """ACPSession 桩：__new__ 绕过 __init__（不起 kimi 子进程），只初始化 terminal 状态"""
    s = d.ACPSession.__new__(d.ACPSession)
    s.req_id = 99999
    s.role = role
    s.readonly = readonly
    s.workdir = Path(workdir or tempfile.gettempdir())
    s.artifact_root = s.workdir
    s.main_ws_roots = [Path(r).resolve() for r in (main_ws_roots or [])] or None
    s.readonly_roots = [s.workdir]
    s.proc = None
    s._terms = {}
    s._term_lock = threading.Lock()
    s._term_counter = itertools.count(1)
    s._send_lock = threading.Lock()
    s._pending = {}
    s._msg_buf = []
    s._buf_lock = threading.Lock()
    s._tool_calls = {}
    s._tool_posted = set()
    s._emit = lambda *a, **k: None
    return s


class L0TerminalLifecycle(unittest.TestCase):
    """终端生命周期：create → output → wait_for_exit → release"""

    def _make(self, **kw):
        s = _session_stub(**kw)
        params = {"command": "bash", "args": ["-c", "echo hello; echo err >&2"],
                  "cwd": str(s.workdir)}
        return s, params

    def test_create_output_wait(self):
        s, params = self._make()
        r = s._terminal_create(params)
        self.assertIn("terminalId", r)
        tid = r["terminalId"]
        # 轮询输出（模拟 kimi 250ms 轮询）
        out = ""
        for _ in range(50):
            o = s._terminal_output({"terminalId": tid})
            out = o["output"]
            if o["exitStatus"] is not None:
                break
            time.sleep(0.05)
        self.assertIn("hello", out)
        self.assertEqual(s._terminal_wait_exit({"terminalId": tid})["exitCode"], 0)
        self.assertEqual(s._terminal_release({"terminalId": tid}), {})

    def test_exit_code_nonzero(self):
        s, params = self._make()
        params["args"] = ["-c", "exit 3"]
        tid = s._terminal_create(params)["terminalId"]
        w = s._terminal_wait_exit({"terminalId": tid})
        self.assertEqual(w["exitCode"], 3)
        o = s._terminal_output({"terminalId": tid})
        self.assertEqual(o["exitStatus"]["exitCode"], 3)

    def test_output_byte_limit_truncated(self):
        s, params = self._make()
        params["args"] = ["-c", "head -c 20000 /dev/zero | tr '\\0' 'x'"]
        params["outputByteLimit"] = 1024
        tid = s._terminal_create(params)["terminalId"]
        s._terminal_wait_exit({"terminalId": tid})
        o = s._terminal_output({"terminalId": tid})
        self.assertTrue(o["truncated"])
        self.assertLessEqual(len(o["output"]), 1024)

    def test_kill_then_wait(self):
        s, params = self._make()
        params["args"] = ["-c", "sleep 30"]
        tid = s._terminal_create(params)["terminalId"]
        s._terminal_kill({"terminalId": tid})
        w = s._terminal_wait_exit({"terminalId": tid})
        self.assertIsNotNone(w["exitCode"])
        # kill 后再 release 幂等
        self.assertEqual(s._terminal_release({"terminalId": tid}), {})

    def _wait_dead(self, proc):
        for _ in range(50):
            if proc.poll() is not None:
                return
            time.sleep(0.05)
        self.fail(f"进程未终止: pid={proc.pid}")

    def test_release_kills_running(self):
        s, params = self._make()
        params["args"] = ["-c", "sleep 30"]
        tid = s._terminal_create(params)["terminalId"]
        proc = s._terms[tid]["proc"]
        s._terminal_release({"terminalId": tid})
        # 记录已移除，进程被终止（不再可 wait——release 是终态清理）
        self.assertNotIn(tid, s._terms)
        self._wait_dead(proc)

    def test_close_cleans_terminals(self):
        s, params = self._make()
        params["args"] = ["-c", "sleep 30"]
        tid = s._terminal_create(params)["terminalId"]
        proc = s._terms[tid]["proc"]
        s._terminal_cleanup()
        # 会话关闭：记录清空、进程被终止
        self.assertNotIn(tid, s._terms)
        self._wait_dead(proc)

    def test_exit_closes_pipes_before_release(self):
        s, params = self._make()
        params["args"] = ["-c", "echo hi"]
        tid = s._terminal_create(params)["terminalId"]
        proc = s._terms[tid]["proc"]
        s._terminal_wait_exit({"terminalId": tid})
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertIn(tid, s._terms)

    def test_release_closes_pipes(self):
        s, params = self._make()
        params["args"] = ["-c", "echo hi"]
        tid = s._terminal_create(params)["terminalId"]
        proc = s._terms[tid]["proc"]
        s._terminal_wait_exit({"terminalId": tid})
        s._terminal_release({"terminalId": tid})
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)

    def test_session_close_closes_pipes(self):
        s = _session_stub()
        s._mcp = None
        s.proc = subprocess.Popen(
            ["bash", "-c", "sleep 30"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        s.close(kill=True)
        self.assertTrue(s.proc.stdin.closed)
        self.assertTrue(s.proc.stdout.closed)
        self._wait_dead(s.proc)

    def test_unknown_terminal(self):
        s, _ = self._make()
        with self.assertRaises(ValueError):
            s._terminal_output({"terminalId": "t-unknown"})


class L1TerminalApproval(unittest.TestCase):
    """终端创建审批护栏"""

    def _dev(self):
        td = tempfile.mkdtemp(prefix="tacp-main-")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        main = Path(td) / "main"
        main.mkdir()
        s = _session_stub(role="dev", main_ws_roots=[str(main)], workdir=td)
        return s, main

    def _readonly(self):
        return _session_stub(role="analysis", readonly=True)

    def test_dev_allow(self):
        s, _ = self._dev()
        tid = s._terminal_create({"command": "bash", "args": ["-c", "echo ok"]})["terminalId"]
        self.assertTrue(tid)

    def test_dev_block_write_main_ws(self):
        s, main = self._dev()
        with self.assertRaises(PermissionError) as ctx:
            s._terminal_create({"command": "bash", "args": ["-c", f"echo x > {main}/f.txt"]})
        self.assertIn("写主工作区", str(ctx.exception))

    def test_dev_allow_venv_python_redirect_tmp(self):
        s, main = self._dev()
        # 主工作区路径出现但写目标合法（重定向 /tmp）→ 放行
        tid = s._terminal_create({"command": "bash", "args": ["-c",
                                  f"{main}/.venv/bin/python -c 'print(1)' > /tmp/t.txt"]})["terminalId"]
        self.assertTrue(tid)

    def test_kill_redline_all_roles(self):
        s, _ = self._dev()
        with self.assertRaises(PermissionError) as ctx:
            s._terminal_create({"command": "bash", "args": ["-c", "pkill -f kimi"]})
        self.assertIn("红线", str(ctx.exception))
        with self.assertRaises(PermissionError):
            s._terminal_create({"command": "bash", "args": ["-c", "kill -9 0"]})

    def test_readonly_allow_whitelist(self):
        s = self._readonly()
        tid = s._terminal_create({"command": "bash", "args": ["-c", "date"]})["terminalId"]
        self.assertTrue(tid)
        tid = s._terminal_create({"command": "bash", "args": ["-c", "ls -la"]})["terminalId"]
        self.assertTrue(tid)

    def test_readonly_block_arbitrary(self):
        s = self._readonly()
        for script in ("python3 -c 'print(1)'", "rm -rf /tmp/x", "echo x > /tmp/x.txt"):
            with self.assertRaises(PermissionError, msg=script):
                s._terminal_create({"command": "bash", "args": ["-c", script]})

    def test_no_commit_role_blocks_commit(self):
        with tempfile.TemporaryDirectory() as td:
            s = _session_stub(role="accept", workdir=td)
            with self.assertRaises(PermissionError):
                s._terminal_create({"command": "bash", "args": ["-c", "git add . && git commit -m x"]})
            # 非提交命令放行
            tid = s._terminal_create({"command": "bash", "args": ["-c", "ls"]})["terminalId"]
            self.assertTrue(tid)


class L2HandleRequestRoute(unittest.TestCase):
    """_handle_request 路由：terminal 方法返回 result，未知方法 method not found"""

    def _session(self):
        s = _session_stub(role="dev")
        s._send = lambda msg: self.sent.append(msg)
        self.sent = []
        return s

    def test_route_create(self):
        s = self._session()
        s._handle_request({"jsonrpc": "2.0", "id": 1, "method": "terminal/create",
                           "params": {"command": "bash", "args": ["-c", "echo hi"]}})
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["id"], 1)
        self.assertIn("terminalId", msg["result"])

    def test_route_unknown(self):
        s = self._session()
        s._handle_request({"jsonrpc": "2.0", "id": 1, "method": "terminal/foo", "params": {}})
        msg = self.sent[0]
        self.assertEqual(msg["error"]["code"], -32601)
        self.assertIn("method not found", msg["error"]["message"])

    def test_route_approval_error(self):
        s = _session_stub(role="analysis", readonly=True)
        s._send = lambda msg: self.sent.append(msg)
        self.sent = []
        s._handle_request({"jsonrpc": "2.0", "id": 2, "method": "terminal/create",
                           "params": {"command": "bash", "args": ["-c", "python3 -c 'x'"]}})
        msg = self.sent[0]
        self.assertEqual(msg["error"]["code"], -32603)
        self.assertIn("白名单", msg["error"]["message"])


if __name__ == "__main__":
    unittest.main()
