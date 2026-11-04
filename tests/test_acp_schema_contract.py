# -*- coding: utf-8 -*-
"""ACP 协议契约测试（离线）：我方手写 ACP 客户端 vs 官方 schema。

为什么要有这个测试：`acp_shared.ACPSession` 是**手写** ACP 客户端（不用 acp SDK），
协议面靠约定维持。kimi 0.37.2 新增「Bash 委托客户端执行」（terminal/*，upstream#351）就是一次实战教训——
上游新增客户端方法时，手写客户端只有真跑到那一步才会发现。

两层校验：

1. **方法面（离线常开）**：官方 `schema/v1/meta.json` 逐字节 vendored 到
   `tests/fixtures/acp_meta_v1.json`（sha256 锁定，出处见 `scripts/acp_schema_sync.py`），
   校验 `acp_shared.py` 里**实际收发**的 ACP 方法名：
   - 我方发出的（`self.request("…")` / `"method": "…"` 通知）必须 ∈ 官方 agentMethods；
   - agent 反向请求我方的（`_handle_request` 的 `method == "…"`）必须 ∈ 官方 clientMethods，
     例外只能写进 DEVIATIONS 且必须**不在**官方表里（上游一旦收录，断言失败 → 回来评估）；
   - 「官方有、我方未实现」的客户端方法集合写死为 EXPECTED_UNIMPLEMENTED：
     上游新增方法导致集合变化时这里失败，提示评估是否要支持；
   - `session/update(agent_thought_chunk)`（#421）**无官方快照可校验**——vendored meta 只有
     方法名表、不含 session/update 的 variant 清单，故改为用例内本地构造断言
     （`test_session_update_thought_chunk_shape_handled_locally`）；
   - 我方 `initialize` 的 protocolVersion 必须等于官方 schema 的 version，
     且必须声明 `clientCapabilities.terminal`（upstream#351 现场：不声明则 Bash/Grep/Glob 全报
     "ACP terminal capability is unavailable"）。

2. **报文字段面（可选，装了官方 SDK 才跑）**：用官方 Pydantic 模型逐个校验真实报文形状
   （我方 initialize/session_new/resume/prompt/set_config_option/cancel，
   以及 kimi 反向的 request_permission / terminal/* / fs/* / session_update）。
   跑法（一次性临时环境，不污染仓库 .venv）：

       python3 -m venv /tmp/acpcheck && /tmp/acpcheck/bin/pip install -q agent-client-protocol
       /tmp/acpcheck/bin/python -m unittest discover -s tests -p 'test_acp_schema_contract.py' -t tests

上游漂移检查 / 刷新 vendored 文件：`python3 scripts/acp_schema_sync.py`（`--write` 覆盖）。
"""
import ast
import hashlib
import json
import re
import sys
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import acp_schema_sync as sync  # noqa: E402（只读常数，导入无副作用）

import acp_shared as ac  # noqa: E402
from unittest import mock  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "acp_meta_v1.json"
META = json.loads(FIXTURE.read_text(encoding="utf-8"))
ACP_SRC = (REPO / "acp_shared.py").read_text(encoding="utf-8")
OFFICIAL_AGENT_METHODS = set(META["agentMethods"].values())
OFFICIAL_CLIENT_METHODS = set(META["clientMethods"].values())

# ---- 我方方法面清单（与 acp_shared.py 的实际提取结果双向断言，改代码必须同步改这里） ----
# 发出：ACPSession.request() 的请求 + _send() 的通知（session/cancel）
EXPECTED_OUTBOUND = frozenset({
    "initialize", "authenticate",
    "session/resume", "session/new", "session/prompt",
    "session/set_config_option", "session/cancel",
})
# agent 反向请求（_handle_request 分支）：我方必须在 result 里应答
EXPECTED_INBOUND_REQUESTS = frozenset({
    "session/request_permission",
    "fs/read_text_file", "fs/write_text_file",
    "terminal/create", "terminal/output", "terminal/wait_for_exit",
    "terminal/kill", "terminal/release", "terminal/input",
})
# agent 反向通知（_reader 分支）：只上报事件，不应答
EXPECTED_INBOUND_NOTIFICATIONS = frozenset({"session/update"})

# 已知偏离官方表的方法：kimi 0.43.1 实测不发 terminal/input（Bash 的 stdin 为空实现），
# 我方按 ACP 语义防御性支持。官方 v1 meta 里没有它——若上游收录，test_deviations 会失败，
# 届时把 terminal/input 从 DEVIATIONS 移入正式清单。
DEVIATIONS = {
    "terminal/input": "kimi 0.43.1 未发（空实现）；官方 schema/v1/meta.json 无此方法，防御性支持",
}

# 官方 clientMethods 里我方**有意不实现**的（kimi 0.43.1 不发；上游新增方法时断言失败 → 评估）
EXPECTED_UNIMPLEMENTED = frozenset({"elicitation/create", "elicitation/complete"})

# 官方 SDK 的 clientMethods 可能领先 vendored 快照（SDK 按 tag 生成，meta.json 取 main），
# 因此 SDK 层只断言「未实现集合 ⊆ 本超集」——不锁死，避免两个上游来源的相位差造成噪音失败。
KNOWN_UNIMPLEMENTED_SUPERSET = EXPECTED_UNIMPLEMENTED | frozenset({
    "mcp/connect", "mcp/message", "mcp/disconnect",
})


# ---------------- 源码提取（方法面的事实来源） ----------------
def _block_src(src, header_re):
    """按缩进切出 class/def 块：从匹配行到下一个「同级或更外层」的 class/def 之前。"""
    lines = src.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(header_re, l)), None)
    if start is None:
        raise AssertionError(f"源码里找不到 {header_re}")
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if not stripped:
            continue
        if (len(lines[i]) - len(stripped)) <= indent and re.match(r"(class |def )", stripped):
            end = i
            break
    return "\n".join(lines[start:end])


def _session_src():
    return _block_src(ACP_SRC, r"^class ACPSession\b")


def _method_src(name):
    return _block_src(_session_src(), rf"^    def {re.escape(name)}\(")


def _init_payload():
    """AST 取出 ACPSession.start() 里真正的 initialize 报文（不用复制粘贴的副本）。"""
    tree = ast.parse(ACP_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_init_payload" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("acp_shared.ACPSession.start() 里找不到 _init_payload 赋值")


def _payload_cases():
    """(标签, 官方 SDK 模型名, 报文) 三元组：出站 = 我方实际发送的形状（见 acp_shared 对应方法），
    入站 = kimi 0.43.1 实测的反向报文形状（terminal 为 Bash 委托，见 upstream#351）。

    注意：`session/update(agent_thought_chunk)`（#421 思考档活性证据）**不在此列**——vendored
    快照只有方法名表、不含 session/update 的 variant 清单，列入也只能靠可选 SDK 校验（本机不装
    即整体跳过）；改为本地构造断言（见 AcpMethodSurfaceTest 的同名用例）。"""
    sid = "session_00000000-0000-0000-0000-000000000000"
    outbound = [
        ("initialize", "InitializeRequest", _init_payload()),
        ("session/new", "NewSessionRequest", {
            "cwd": "/tmp/wt",
            "mcpServers": [{"type": "http", "name": "req_board_readonly",
                            "url": "http://127.0.0.1:51234/mcp", "headers": []}]}),
        ("session/resume", "ResumeSessionRequest", {
            "sessionId": sid, "cwd": "/tmp/wt", "mcpServers": []}),
        ("session/prompt", "PromptRequest", {
            "sessionId": sid, "prompt": [{"type": "text", "text": "/usage"}]}),
        # #44：助手带附件轮次的多模态 content blocks（图片走 image content；文本块在前）
        ("session/prompt(image)", "PromptRequest", {
            "sessionId": sid, "prompt": [{"type": "text", "text": "这是一张图"},
                                         {"type": "image", "mimeType": "image/png",
                                          "data": "iVBORw0KGgoAAAANSUhEUg=="}]}),
        ("session/set_config_option(model)", "SetSessionConfigOptionSelectRequest", {
            "sessionId": sid, "configId": "model", "value": "deepseek/v4.1-flash"}),
        ("session/set_config_option(mode)", "SetSessionConfigOptionSelectRequest", {
            "sessionId": sid, "configId": "mode", "value": "default"}),
        ("session/cancel", "CancelNotification", {"sessionId": sid}),
    ]
    inbound = [
        ("session/request_permission", "RequestPermissionRequest", {
            "sessionId": sid,
            "toolCall": {"toolCallId": "call_1", "title": "Running: ls -la",
                         "kind": "execute", "status": "pending",
                         "rawInput": {"command": "ls -la"}},
            "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"}]}),
        ("terminal/create", "CreateTerminalRequest", {
            "sessionId": sid, "command": "/bin/bash", "args": ["-c", "ls -la"],
            "cwd": "/tmp/wt", "outputByteLimit": 100000,
            "env": [{"name": "NO_COLOR", "value": "1"}, {"name": "TERM", "value": "dumb"}]}),
        ("fs/read_text_file", "ReadTextFileRequest", {
            "sessionId": sid, "path": "/tmp/wt/a.py", "line": 1, "limit": 200}),
        ("fs/write_text_file", "WriteTextFileRequest", {
            "sessionId": sid, "path": "/tmp/wt/a.py", "content": "x = 1\n"}),
        ("terminal/output", "TerminalOutputRequest", {"sessionId": sid, "terminalId": "t-1"}),
        ("terminal/wait_for_exit", "WaitForTerminalExitRequest", {"sessionId": sid, "terminalId": "t-1"}),
        ("terminal/kill", "KillTerminalRequest", {"sessionId": sid, "terminalId": "t-1"}),
        ("terminal/release", "ReleaseTerminalRequest", {"sessionId": sid, "terminalId": "t-1"}),
        ("session/update(agent_message_chunk)", "SessionNotification", {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "hello"}}}),
        ("session/update(tool_call)", "SessionNotification", {
            "sessionId": sid,
            "update": {"sessionUpdate": "tool_call", "toolCallId": "call_1",
                       "title": "Running: ls", "kind": "execute", "status": "in_progress"}}),
        ("session/update(tool_call_update)", "SessionNotification", {
            "sessionId": sid,
            "update": {"sessionUpdate": "tool_call_update", "toolCallId": "call_1",
                       "status": "completed", "rawInput": {"command": "ls -la"},
                       "content": [{"type": "content",
                                    "content": {"type": "text", "text": "ok"}}]}}),
    ]
    return outbound, inbound


try:  # 可选开发依赖：官方 Python SDK（不在仓库 .venv 里，装了才跑第 2 层）
    import acp.meta as _sdk_meta  # noqa: F401
    import acp.schema as _sdk_schema  # noqa: F401
    HAS_SDK = True
except Exception:  # pragma: no cover - 取决于本地是否装了可选依赖
    _sdk_meta = _sdk_schema = None
    HAS_SDK = False


class AcpMethodSurfaceTest(unittest.TestCase):
    """第 1 层：方法面契约（离线，官方 vendored 快照）"""

    def test_fixture_is_official_snapshot(self):
        """vendored 文件必须是官方原样（sha256 锁定），且版本号可解析"""
        raw = FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), sync.VENDORED_META_SHA256,
                         "tests/fixtures/acp_meta_v1.json 被改动过——请用 "
                         "scripts/acp_schema_sync.py --write 刷新并同步脚本里的 sha256 常数")
        self.assertEqual(META["version"], 1)
        for section in ("agentMethods", "clientMethods"):
            values = list(META[section].values())
            self.assertTrue(values, f"{section} 不应为空")
            self.assertEqual(len(values), len(set(values)), f"{section} 方法名不应重复")

    def test_official_maps_are_self_consistent(self):
        """官方表自身的 key/value 形态（key 蛇形，value 为方法名；agent/client 两侧不重叠）"""
        for section in ("agentMethods", "clientMethods"):
            for key, value in META[section].items():
                self.assertTrue(re.fullmatch(r"[a-z_]+", key), f"{section}.{key} 不是蛇形 key")
                self.assertRegex(value, r"^[a-z_]+(/[a-zA-Z_]+)?$",
                                 f"{section}.{key} 的值 {value!r} 不是方法名")
        self.assertEqual(OFFICIAL_AGENT_METHODS & OFFICIAL_CLIENT_METHODS, set(),
                         "agentMethods 与 clientMethods 不应有交集（同向方法各归一侧）")

    def test_outbound_methods_match_official(self):
        """我方发出的方法：清单 == 源码提取结果，且都在官方 agentMethods 里"""
        session = _session_src()
        sent = set(re.findall(r'self\.request\(\s*"([^"]+)"', session)) | \
            set(re.findall(r'"method":\s*"([^"]+)"', session))
        self.assertEqual(sent, set(EXPECTED_OUTBOUND),
                         "acp_shared.ACPSession 发出的 ACP 方法与 EXPECTED_OUTBOUND 不一致")
        self.assertFalse(sent - OFFICIAL_AGENT_METHODS,
                         f"发出了官方 agentMethods 之外的方法: {sorted(sent - OFFICIAL_AGENT_METHODS)}")

    def test_inbound_requests_match_official(self):
        """agent 反向请求：清单 == 源码提取结果，且 ∈ 官方 clientMethods ∪ 已声明偏离"""
        handled = set(re.findall(r'method ==\s*"([^"]+)"', _method_src("_handle_request")))
        self.assertEqual(handled, set(EXPECTED_INBOUND_REQUESTS),
                         "acp_shared.ACPSession._handle_request 的分支与 EXPECTED_INBOUND_REQUESTS 不一致")
        self.assertFalse(handled - OFFICIAL_CLIENT_METHODS - set(DEVIATIONS),
                         f"处理了官方 clientMethods 之外且未声明的反向请求: "
                         f"{sorted(handled - OFFICIAL_CLIENT_METHODS - set(DEVIATIONS))}")

    def test_inbound_notifications_match_official(self):
        """agent 反向通知：清单 == 源码提取结果，且 ∈ 官方 clientMethods"""
        consumed = set(re.findall(r'msg\.get\("method"\) ==\s*"([^"]+)"', _method_src("_reader")))
        self.assertEqual(consumed, set(EXPECTED_INBOUND_NOTIFICATIONS),
                         "acp_shared.ACPSession._reader 的通知分支与 EXPECTED_INBOUND_NOTIFICATIONS 不一致")
        self.assertFalse(consumed - OFFICIAL_CLIENT_METHODS,
                         f"消费了官方 clientMethods 之外的通知: {sorted(consumed - OFFICIAL_CLIENT_METHODS)}")

    def test_deviations_are_not_official(self):
        """偏离项必须确实不在官方表里——上游一旦收录，这里失败，提示把该项并入正式清单"""
        for method in DEVIATIONS:
            self.assertNotIn(method, OFFICIAL_CLIENT_METHODS,
                             f"{method} 已被官方 v1 schema 收录：请从 DEVIATIONS 移除并重新评估实现")

    def test_session_update_thought_chunk_shape_handled_locally(self):
        """#421 入站 `session/update(agent_thought_chunk)` 形状的**本地**断言。

        为什么不用快照/SDK 校验：vendored 快照（tests/fixtures/acp_meta_v1.json）只锁方法名，
        不含 session/update 的 variant 清单；第 2 层 SDK 校验依赖可选依赖（本机未装时整体跳过）。
        故此处本地构造 kimi 实测形状（与 agent_message_chunk 同构：content.type == "text"），
        断言我方客户端吃下该报文不崩、只走「思考档」活性上报（不写事件流）。更细的节流/序列
        断言见 tests/test_dispatcher_session_activity.py（D6/D7/D9/D10）。"""
        shape = {"sessionUpdate": "agent_thought_chunk",
                 "content": {"type": "text", "text": "思考"}}
        self.assertIn('kind == "agent_thought_chunk"', _method_src("_on_update"),
                      "ACPSession._on_update 缺 agent_thought_chunk 分支（#421 思考档活性证据）")
        import acp_shared as ac  # 局部导入：本文件其余用例只读源码，不依赖模块副作用
        s = ac.ACPSession.__new__(ac.ACPSession)   # 桩：绕过 __init__（不起 kimi 子进程）
        s.req_id = 0
        s.session_id = "session_test"
        s._msg_buf = []
        s._buf_lock = threading.Lock()
        s._capture = None
        s._tool_calls = {}
        s._tool_posted = set()
        s.events = []
        s.calls = []
        s._emit = lambda kind, content, session_id=None: s.events.append((kind, content))
        s.activity_sink = lambda kind, detail=None: s.calls.append((kind, detail))
        s._on_update(shape)
        self.assertEqual(s.calls, [("llm_thought", None)], "思考 chunk 只刷思考档时间戳")
        self.assertEqual(s.events, [], "思考 chunk 不写事件流（#421）")
        self.assertEqual(s._msg_buf, [], "思考 chunk 不进消息缓冲")

    def test_unimplemented_official_client_methods_locked(self):
        """官方有、我方未实现的客户端方法集合锁定：上游新增方法时失败 → 评估是否要支持"""
        implemented = set(EXPECTED_INBOUND_REQUESTS) | set(EXPECTED_INBOUND_NOTIFICATIONS)
        unimplemented = OFFICIAL_CLIENT_METHODS - implemented - set(DEVIATIONS)
        self.assertEqual(
            unimplemented, set(EXPECTED_UNIMPLEMENTED),
            "官方 clientMethods 与「我方已实现」的差集变了：上游新增/删除了客户端方法。"
            "先确认 kimi 是否会发这个方法（不会发就加进 EXPECTED_UNIMPLEMENTED 并注明），"
            f"当前差集={sorted(unimplemented)}")

    def test_initialize_payload_pins_protocol_version_and_terminal_capability(self):
        """initialize 报文：protocolVersion == 官方 version；必须声明 terminal 与 fs 能力（upstream#351）"""
        payload = _init_payload()
        self.assertEqual(payload["protocolVersion"], META["version"],
                         "我方 initialize 的 protocolVersion 与官方 schema/v1 版本不一致")
        caps = payload.get("clientCapabilities") or {}
        self.assertIs(caps.get("terminal"), True,
                      "未声明 clientCapabilities.terminal：kimi 0.37.2+ 会禁用 Bash/Grep/Glob（upstream#351）")
        self.assertIs((caps.get("fs") or {}).get("readTextFile"), True)
        self.assertIs((caps.get("fs") or {}).get("writeTextFile"), True)
        self.assertTrue(payload.get("clientInfo", {}).get("name"),
                        "initialize 应带 clientInfo.name（服务端日志排障用）")


@unittest.skipUnless(
    HAS_SDK,
    "未安装可选依赖 agent-client-protocol（官方 SDK）。跑法见本文件顶部 docstring 或 "
    "docs/dispatcher.md「ACP 协议契约测试」")
class AcpPayloadShapeTest(unittest.TestCase):
    """第 2 层：报文字段面契约（官方 Pydantic 模型；需可选依赖 agent-client-protocol）"""

    def test_sdk_protocol_version_matches_vendored(self):
        self.assertEqual(_sdk_meta.PROTOCOL_VERSION, META["version"],
                         "官方 SDK 的协议版本与 vendored meta.json 不一致（vendored 快照已过期？）")

    def test_our_method_surface_known_to_sdk(self):
        sdk_agent = set(_sdk_meta.AGENT_METHODS.values())
        sdk_client = set(_sdk_meta.CLIENT_METHODS.values())
        self.assertFalse(set(EXPECTED_OUTBOUND) - sdk_agent,
                         f"我方发出的方法不在 SDK 方法表里: {sorted(set(EXPECTED_OUTBOUND) - sdk_agent)}")
        ours = set(EXPECTED_INBOUND_REQUESTS) | set(EXPECTED_INBOUND_NOTIFICATIONS)
        self.assertFalse(ours - sdk_client - set(DEVIATIONS),
                         f"我方处理的反向方法不在 SDK 方法表里: "
                         f"{sorted(ours - sdk_client - set(DEVIATIONS))}")
        unimplemented = sdk_client - ours - set(DEVIATIONS)
        self.assertLessEqual(unimplemented, set(KNOWN_UNIMPLEMENTED_SUPERSET),
                             f"SDK clientMethods 出现未登记的新方法: "
                             f"{sorted(unimplemented - set(KNOWN_UNIMPLEMENTED_SUPERSET))}")

    def test_outbound_payloads_match_official_models(self):
        outbound, _ = _payload_cases()
        for label, model_name, payload in outbound:
            with self.subTest(label=label):
                getattr(_sdk_schema, model_name).model_validate(payload)

    def test_inbound_payloads_match_official_models(self):
        _, inbound = _payload_cases()
        for label, model_name, payload in inbound:
            with self.subTest(label=label):
                getattr(_sdk_schema, model_name).model_validate(payload)


class AcpPromptContentBlocksTest(unittest.TestCase):
    """#44 ACPSession.prompt(content=…)：缺省逐字保持纯文本报文；给定则按调用方 blocks 发送。"""

    @staticmethod
    def _stub():
        s = ac.ACPSession.__new__(ac.ACPSession)     # 不走 __init__：不 spawn kimi acp
        s.session_id = "sid-stub"
        return s

    def test_plain_text_payload_unchanged(self):
        s = self._stub()
        s.request = mock.Mock(return_value={})
        s.prompt("hi", 5)
        self.assertEqual(s.request.call_args[0][0], "session/prompt")
        self.assertEqual(s.request.call_args[0][1],
                         {"sessionId": "sid-stub", "prompt": [{"type": "text", "text": "hi"}]})

    def test_content_blocks_sent_as_given(self):
        s = self._stub()
        s.request = mock.Mock(return_value={})
        blocks = [{"type": "text", "text": "看图"},
                  {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}]
        s.prompt("看图", 5, content=blocks)
        self.assertEqual(s.request.call_args[0][1],
                         {"sessionId": "sid-stub", "prompt": blocks})
        self.assertEqual(s.request.call_args[0][0], "session/prompt")


if __name__ == "__main__":
    unittest.main()
