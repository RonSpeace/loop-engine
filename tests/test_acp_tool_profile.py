#!/usr/bin/env python3
"""req-24 飞书看板助手 ACP 构造档（tool_profile）三层拒绝矩阵 + 普通 profile 逐字回归。

断言对象 = acp_shared.ACPSession（不 import dispatcher / board，避免耦合到流程与调度器）：
A. 注入层：助手 profile 的 _readonly_mcp_servers 恒空；start 的 mcpServers 只含 extra_mcp_servers；
D. 能力声明层：助手 initialize 的 fs.readTextFile / fs.writeTextFile / terminal 全 false；
B. 审批层：助手 _auto_approve 只放行 mcp__<注入 server 名>__<工具名>，其余全拒；
C. 反向读层：助手 _handle_request 拒绝 fs 读写与全部 terminal/*（fail-closed 方法错误）；
E. 普通 profile（tool_profile=None）回归：注入 / 审批 / fs / terminal / initialize 与现状一致。

隔离手段：全部用 `__new__` 桩 + 假 request（不用 __init__ 起 kimi 子进程），
不写真实仓库、不连网络（只读 MCP server 用临时目录起本地 HTTP，用后 close）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \
        -m unittest tests.test_acp_tool_profile -v
"""
import io
import itertools
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import acp_shared as ac  # noqa: E402

ASSISTANT_PROFILE = ac.TOOL_PROFILE_FEISHU_ASSISTANT
ASSISTANT_SERVER = "assistant_tools"     # board 侧助手 MCP server 名（父代理约定）
ASSISTANT_ENTRY = {"type": "http", "name": ASSISTANT_SERVER,
                   "url": "http://127.0.0.1:51235/mcp", "headers": []}
CREATE_SERVER = "reqboard-create"        # board 建卡会话 MCP 名（助手不得注入）

ASSISTANT_TOOLS = ("projects_status", "project_find", "req_query", "req_card",
                   "req_timeline", "sessions_status", "pending_confirmations",
                   "create_requirement")


def _perm_params(title, text="", tool_call_id="t1"):
    """request_permission 参数：展示文本走 content、工具名走 toolCall.title（生产形态）"""
    tc = {"title": title,
          "content": [{"type": "content", "content": {"type": "text", "text": text}}]}
    if tool_call_id:
        tc["toolCallId"] = tool_call_id
    return {"options": [{"optionId": "approve_once", "kind": "allow_once"},
                        {"optionId": "reject", "kind": "reject_once"}],
            "toolCall": tc}


def _approved(ret):
    return (ret.get("outcome") or {}).get("outcome") == "selected"


def _rejected(ret):
    return (ret.get("outcome") or {}).get("outcome") == "rejected"


def _stub(tmp, *, tool_profile=None, role="analysis", readonly=True,
          extra_mcp_servers=(), extra_allow_prefixes=(), allow_fs_write=False):
    """ACPSession 桩：__new__ 绕过 __init__（不起 kimi 子进程），只初始化被调用路径所需属性"""
    s = ac.ACPSession.__new__(ac.ACPSession)
    s.req_id = 24
    s.role = role
    s.readonly = readonly
    s.tool_profile = tool_profile
    # 路径统一 resolve（macOS /var → /private/var 软链）：Bash 白名单按解析后路径比较，
    # 不 resolve 会把合法命令误判为越界（与既有仅只读测试同款口径）
    s.workdir = Path(tmp).resolve()
    s.artifact_root = (Path(tmp) / "artifacts").resolve()
    s.project = "req-board"
    s.readonly_roots = [Path(tmp).resolve()]
    s.write_roots = None
    s.main_ws_roots = None
    s.allow_fs_write = allow_fs_write
    s.allow_git_commit = False
    s.allow_git_merge = False
    s.allow_python_safe = False
    s.python_roots = None
    s.python_compile_roots = None
    s._allow_prefixes = [f"mcp__{ac.READONLY_TOOL_SERVER}__"] + [str(p) for p in extra_allow_prefixes]
    s._extra_mcp_servers = [dict(e) for e in extra_mcp_servers]
    s.mysql_config = None
    s._mcp = None
    s._tool_cmds = {}
    s._terms = {}
    s._term_lock = threading.Lock()
    s._term_counter = itertools.count(1)
    s.proc = None
    s.events = []
    s._emit = lambda kind, content, session_id=None: s.events.append((kind, content))
    s.sent = []
    s._send = lambda msg: s.sent.append(msg)
    return s


class _StartHarness:
    """把 start() 的对外调用全换成假实现，只保留「initialize 报文 + mcpServers 组装」这层
    （本文件 A/D/E 断言的来源）——不触碰真实 kimi 进程。"""

    def __init__(self, session):
        self.session = session
        self.calls = []
        session.request = self.request
        session._apply_config = lambda result: None
        session._apply_permission_mode = lambda: None
        session._apply_model = lambda: None

    def request(self, method, params, timeout=60):
        self.calls.append((method, params))
        if method == "session/new":
            return {"sessionId": "sess-test"}
        return {}

    def params_of(self, method):
        return next(p for m, p in self.calls if m == method)


class AssistantInjectionAndCapabilityTest(unittest.TestCase):
    """A/D 层：助手 profile 注入层 + initialize 能力声明层"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="req24-tool-profile-")

    def test_a1_readonly_mcp_never_created(self):
        """助手 profile：_readonly_mcp_servers 恒空、不创建 ReadonlyMcpServer"""
        s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE, extra_mcp_servers=[ASSISTANT_ENTRY])
        self.assertEqual(s._readonly_mcp_servers(self.tmp), [])
        self.assertIsNone(s._mcp, "助手 profile 不得创建 ReadonlyMcpServer")

    def test_a2_start_injects_only_extra_mcp_servers(self):
        """助手 profile：session/new 的 mcpServers 只有 extra_mcp_servers（助手 MCP）"""
        s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE, extra_mcp_servers=[ASSISTANT_ENTRY])
        h = _StartHarness(s)
        self.assertFalse(s.start(self.tmp))
        servers = h.params_of("session/new")["mcpServers"]
        self.assertEqual(servers, [ASSISTANT_ENTRY])
        names = [e["name"] for e in servers]
        self.assertNotIn(ac.READONLY_TOOL_SERVER, names, "助手不得注入只读工具 MCP")
        self.assertNotIn(CREATE_SERVER, names, "助手不得注入建卡工具 MCP")

    def test_a3_start_without_extra_yields_no_mcp(self):
        """fail-closed：未注入任何 server 时 mcpServers 为空（绝无「退回只读工具」兜底）"""
        s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE)
        h = _StartHarness(s)
        s.start(self.tmp)
        self.assertEqual(h.params_of("session/new")["mcpServers"], [])

    def test_a4_resume_path_same_injection(self):
        """resume 与 new 走同一 mcpServers 组装（不因续跑放宽）"""
        s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE, extra_mcp_servers=[ASSISTANT_ENTRY])
        h = _StartHarness(s)
        self.assertTrue(s.start(self.tmp, resume_session_id="old-sess"))
        self.assertEqual(h.params_of("session/resume")["mcpServers"], [ASSISTANT_ENTRY])

    def test_a5_profile_overrides_role_routing(self):
        """profile 优先于 role/readonly：即使按 dev（readonly=False）构造也走助手档"""
        s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE, role="dev", readonly=False)
        self.assertEqual(s._readonly_mcp_servers(self.tmp), [])
        self.assertTrue(_rejected(s._auto_approve(_perm_params("Read"))),
                        "助手档不受 dev 角色的全放行影响")

    def test_d1_initialize_capabilities_all_false(self):
        """助手 initialize：fs.readTextFile/writeTextFile=false、terminal=false"""
        s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE, extra_mcp_servers=[ASSISTANT_ENTRY])
        h = _StartHarness(s)
        s.start(self.tmp)
        payload = h.params_of("initialize")
        caps = payload["clientCapabilities"]
        self.assertIs(caps["terminal"], False)
        self.assertIs(caps["fs"]["readTextFile"], False)
        self.assertIs(caps["fs"]["writeTextFile"], False)
        self.assertEqual(payload["protocolVersion"], 1)
        self.assertTrue(payload["clientInfo"]["name"])


class AssistantApprovalLayerTest(unittest.TestCase):
    """B 层：助手 profile 审批白名单（只放行注入的助手 MCP 工具）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="req24-tool-profile-")
        self.s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE,
                       extra_mcp_servers=[ASSISTANT_ENTRY])

    def test_b1_assistant_mcp_tools_allowed(self):
        for tool in ASSISTANT_TOOLS:
            title = f"mcp__{ASSISTANT_SERVER}__{tool}"
            self.assertTrue(_approved(self.s._auto_approve(_perm_params(title))), title)

    def test_b2_everything_else_rejected(self):
        cases = [
            ("Read", ""),
            ("Grep", ""),
            ("Glob", ""),
            ("Write", "Writing /tmp/req24-should-not-land.md"),
            ("Edit", "Editing /tmp/req24-should-not-land.md"),
            ("Bash", "Requesting approval to Running: ls -la"),
            ("mcp__reqboard-tools__read_file", ""),
            ("mcp__reqboard-tools__db_query", ""),
            ("mcp__playwright__browser_navigate", ""),
            ("mcp__tapd__get_story_or_task_count", ""),
            ("mcp__other-server__anything", ""),
            ("", ""),
            (f"mcp__{ASSISTANT_SERVER}__", ""),   # 前缀裸名（无工具名）也拒
        ]
        for title, text in cases:
            self.assertTrue(_rejected(self.s._auto_approve(_perm_params(title, text))),
                            f"{title!r} 应被助手档拒绝")

    def test_b3_no_injected_server_rejects_all(self):
        """fail-closed：未注入助手 MCP（配置缺失）时连助手工具名也拒"""
        s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE)
        self.assertEqual(s._assistant_mcp_prefixes(), [])
        for tool in ("projects_status", "create_requirement"):
            self.assertTrue(_rejected(s._auto_approve(
                _perm_params(f"mcp__{ASSISTANT_SERVER}__{tool}"))), tool)

    def test_b4_whitelist_derived_from_injected_names(self):
        """白名单前缀动态取自 extra_mcp_servers 的 name（改名不漂移）；非注入名一律拒"""
        other = {"type": "http", "name": "other_tools", "url": "http://127.0.0.1:1/mcp", "headers": []}
        s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE, extra_mcp_servers=[other])
        self.assertEqual(s._assistant_mcp_prefixes(), ["mcp__other_tools__"])
        self.assertTrue(_approved(s._auto_approve(_perm_params("mcp__other_tools__x"))))
        self.assertTrue(_rejected(s._auto_approve(
            _perm_params(f"mcp__{ASSISTANT_SERVER}__projects_status"))))


class AssistantReverseRequestLayerTest(unittest.TestCase):
    """C 层：助手 profile 反向请求（fs 读写 + 全部 terminal/*）fail-closed 拒绝"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req24-tool-profile-"))
        self.s = _stub(self.tmp, tool_profile=ASSISTANT_PROFILE,
                       extra_mcp_servers=[ASSISTANT_ENTRY], allow_fs_write=True)

    def test_c1_fs_and_terminal_denied(self):
        target = self.tmp / "secret.txt"
        target.write_text("top-secret")
        out = self.tmp / "should-not-land.txt"
        probes = [
            ("fs/read_text_file", {"path": str(target)}),
            ("fs/write_text_file", {"path": str(out), "content": "x"}),
            ("terminal/create", {"command": "/bin/bash", "args": ["-c", "ls -la"]}),
            ("terminal/output", {"terminalId": "t1"}),
            ("terminal/wait_for_exit", {"terminalId": "t1"}),
            ("terminal/kill", {"terminalId": "t1"}),
            ("terminal/release", {"terminalId": "t1"}),
            ("terminal/input", {"terminalId": "t1", "data": "x"}),
        ]
        for i, (method, params) in enumerate(probes):
            self.s.sent = []
            self.s._handle_request({"id": i, "method": method, "params": params})
            self.assertEqual(len(self.s.sent), 1, method)
            msg = self.s.sent[0]
            self.assertNotIn("result", msg, f"{method} 必须 fail-closed")
            self.assertEqual(msg["error"]["code"], -32601, method)
            self.assertIn("method not found", msg["error"]["message"])
        self.assertFalse(out.exists(), "助手 profile 不得落盘任何文件")
        self.assertEqual(self.s._terms, {}, "助手 profile 不得创建终端")

    def test_c2_permission_requests_still_answered(self):
        """反向读拒绝不影响审批应答：request_permission 仍走 _auto_approve"""
        self.s._handle_request({"id": 1, "method": "session/request_permission",
                                "params": _perm_params(f"mcp__{ASSISTANT_SERVER}__projects_status")})
        self.assertTrue(_approved(self.s.sent[0]["result"]))
        self.s.sent = []
        self.s._handle_request({"id": 2, "method": "session/request_permission",
                                "params": _perm_params("Read")})
        self.assertTrue(_rejected(self.s.sent[0]["result"]))


class NormalProfileRegressionTest(unittest.TestCase):
    """E 层：tool_profile=None（普通会话）注入/审批/fs/terminal/initialize 逐字回归"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req24-tool-profile-"))

    def test_e1_default_profile_is_none(self):
        s = _stub(self.tmp)
        self.assertIsNone(s.tool_profile)
        self.assertFalse(s._is_assistant_profile())

    def test_e2_readonly_mcp_injected_as_before(self):
        s = _stub(self.tmp, role="analysis", readonly=True)
        try:
            entries = s._readonly_mcp_servers(self.tmp)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["type"], "http")
            self.assertEqual(entries[0]["name"], ac.READONLY_TOOL_SERVER)
        finally:
            if s._mcp:
                s._mcp.close()
        s2 = _stub(self.tmp, role="dev", readonly=False)
        self.assertEqual(s2._readonly_mcp_servers(self.tmp), [])

    def test_e3_approval_matrix_unchanged(self):
        s = _stub(self.tmp, role="analysis", readonly=True)
        self.assertTrue(_approved(s._auto_approve(_perm_params("mcp__reqboard-tools__read_file"))))
        for title in ("Read", "Grep", "Glob", "Write",
                      f"mcp__{ASSISTANT_SERVER}__projects_status", "mcp__other__x"):
            self.assertTrue(_rejected(s._auto_approve(_perm_params(title))), title)
        # 只读 Bash 白名单命令仍走预检 approve（现状语义，未被助手档影响）
        self.assertTrue(_approved(s._auto_approve(_perm_params(
            "Bash", "Requesting approval to Running: ls -la", tool_call_id="b1"))))

    def test_e4_fs_read_ok_terminal_untouched(self):
        target = self.tmp / "plain.txt"
        target.write_text("hello")
        out = self.tmp / "blocked.txt"
        s = _stub(self.tmp, role="analysis", readonly=True, allow_fs_write=False)
        s._handle_request({"id": 1, "method": "fs/read_text_file", "params": {"path": str(target)}})
        self.assertEqual(s.sent[0]["result"], {"content": "hello"})
        s.sent = []
        s._handle_request({"id": 2, "method": "fs/write_text_file",
                           "params": {"path": str(out), "content": "x"}})
        self.assertEqual(s.sent[0]["error"]["code"], -32603,
                         "普通 profile 的写拒绝走原错误码（非助手的 -32601）")
        self.assertFalse(out.exists())
        s.sent = []
        s._handle_request({"id": 3, "method": "terminal/output", "params": {"terminalId": "nope"}})
        self.assertEqual(s.sent[0]["error"]["code"], -32603,
                         "普通 profile 的 terminal/* 走真实实现（终端不存在），不被助手档短接")

    def test_e5_initialize_capabilities_true(self):
        s = _stub(self.tmp, role="dev", readonly=False)
        h = _StartHarness(s)
        s.start(self.tmp)
        caps = h.params_of("initialize")["clientCapabilities"]
        self.assertIs(caps["terminal"], True)
        self.assertIs(caps["fs"]["readTextFile"], True)
        self.assertIs(caps["fs"]["writeTextFile"], True)

    def test_e6_start_injects_readonly_plus_extra(self):
        extra = {"type": "http", "name": CREATE_SERVER, "url": "http://127.0.0.1:1/mcp", "headers": []}
        s = _stub(self.tmp, role="analysis", readonly=True, extra_mcp_servers=[extra])
        h = _StartHarness(s)
        try:
            s.start(self.tmp)
            names = [e["name"] for e in h.params_of("session/new")["mcpServers"]]
            self.assertEqual(names, [ac.READONLY_TOOL_SERVER, CREATE_SERVER])
        finally:
            if s._mcp:
                s._mcp.close()

    def test_e7_unknown_profile_fails_fast_before_spawn(self):
        """未知 tool_profile 构造即报错，且不得走到起子进程那一步"""
        with mock.patch.object(ac.subprocess, "Popen",
                               side_effect=RuntimeError("Popen 不应被调用（未知 profile 须 fail-fast）")):
            with self.assertRaises(ValueError):
                ac.ACPSession(24, self.tmp, io.StringIO(), "dev", self.tmp, tool_profile="nope")

    def test_e8_none_and_known_profile_construct_without_error(self):
        """tool_profile=None 与 'feishu_assistant' 都是合法构造值（起进程被拦，只断言校验层）"""
        for value in (None, ASSISTANT_PROFILE):
            with mock.patch.object(ac.subprocess, "Popen",
                                   side_effect=RuntimeError("构造校验层已通过，主动中断")):
                with self.assertRaises(RuntimeError):
                    ac.ACPSession(24, self.tmp, io.StringIO(), "dev", self.tmp, tool_profile=value)


if __name__ == "__main__":
    unittest.main()
