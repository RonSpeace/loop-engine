#!/usr/bin/env python3
"""#50 思考档位统一模型默认：请求/配置/展示通道下线的回归锁。

A 组（行为）：
- A1 类与模块契约：ACPSession 无 thinking 形参/无 _apply_thinking；acp_shared 不导出思考链符号。
- A2/A2b/A3 start() 的 new / resume 两路径不再发 configId="thinking"（mode/model 调用不受影响，
  tech 角色 manual_mode=True → mode + model；dev 角色 → 只有 model）。
- A4 configOptions 含 thought_level 时仍不请求，且 session.thinking 记录观测值。
- A5 旧调用方传 thinking= 关键字 → TypeError（契约收紧，防复活）。
- A6 dispatcher 源码/行为：无 role_thinking 导入、无 post_thinking_events、构造 kwargs 不含 thinking。
- A7 日志门禁：start() 全程 logger 无「推理深度」「思考档位」文案。
- A8 看板三类会话（建卡/需求助手/飞书助手）构造 ACPSession 均不传 thinking。
- A9 flows.json 旧 thinking 键静默忽略（可加载、规范化结果无该键）。
- A10/A11 观测值清理/保持（自 test_thinking_runtime_options.py S2/S3 迁移）。
- A12 源级负断言：前端/后端不得复活思考档位请求、提示行、列与样式。

隔离：ACPSession 桩（__new__ 绕过 __init__，不起子进程）；临时 flows.json；不读真实 projects.json。
"""
import inspect
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402
from tests._dispatcher_src import dispatcher_source  # noqa: E402
from tests._frontend_src import backend_text, frontend_css, frontend_js  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402


def _opts(model="deepseek/v4.1-flash", thinking=None, levels=None):
    out = [{"id": "model", "category": "model", "currentValue": model,
            "options": [{"value": model, "name": model}]}]
    if levels is not None:
        out.append({"id": "thinking", "category": "thought_level", "currentValue": thinking,
                    "options": [{"value": v, "name": v} for v in levels]})
    return {"configOptions": out}


class _StartSession:
    """start() 桩：只保留 initialize → session/new|resume → mode/model 的调用序列。"""

    def __init__(self, role, model="deepseek/v4.1-flash", resume=False):
        s = ac.ACPSession.__new__(ac.ACPSession)
        s.req_id = 50001
        s.role = role
        s.project = "req-board"
        s.session_id = None
        s.model_name = None
        s.thinking = None
        s.model = model
        s.event_sink = None
        s.tool_profile = None
        s._extra_mcp_servers = []
        s._readonly_mcp_servers = lambda workdir: []
        s.calls = []
        self.resume = resume

        def fake_request(method, params=None, **kwargs):
            s.calls.append((method, params or {}))
            if method == "initialize":
                return {"authMethods": []}
            if method == "session/resume":
                if not self.resume:
                    raise RuntimeError("resume not found")
                return {"sessionId": "resumed", "configOptions": _opts()["configOptions"]}
            if method == "session/new":
                return {"sessionId": "new", "configOptions": _opts()["configOptions"]}
            if method == "session/set_config_option":
                return _opts()
            return {}

        s.request = fake_request
        self.session = s

    def run(self):
        return self.session.start(Path(tempfile.gettempdir()), resume_session_id="old" if self.resume else None)


class ContractTest(unittest.TestCase):

    def test_a1_no_thinking_channel(self):
        params = inspect.signature(ac.ACPSession.__init__).parameters
        self.assertNotIn("thinking", params)
        self.assertFalse(hasattr(ac.ACPSession, "_apply_thinking"))
        self.assertFalse(hasattr(ac.ACPSession, "_thinking_settle_of"))
        for name in ("role_thinking", "role_thinking_detail", "thinking_request", "THINKING_LEVELS",
                     "ACP_THINKING", "adjust_thinking_for_efforts", "model_support_efforts",
                     "select_thinking_request"):
            self.assertFalse(hasattr(ac, name), name)

    def test_a5_thinking_kwarg_type_error(self):
        with self.assertRaises(TypeError):
            ac.ACPSession(1, tempfile.gettempdir(), None, "dev", tempfile.gettempdir(), thinking="high")


class StartNoThinkingRequestTest(unittest.TestCase):

    def _thinking_calls(self, calls):
        return [c for c in calls
                if c[0] == "session/set_config_option" and c[1].get("configId") == "thinking"]

    def test_a2_new_path_tech_mode_and_model_only(self):
        h = _StartSession("tech")
        h.run()
        self.assertEqual(self._thinking_calls(h.session.calls), [])
        configs = [p.get("configId") for m, p in h.session.calls if m == "session/set_config_option"]
        self.assertIn("mode", configs)
        self.assertIn("model", configs)

    def test_a2b_new_path_dev_model_only(self):
        h = _StartSession("dev")
        h.run()
        self.assertEqual(self._thinking_calls(h.session.calls), [])
        configs = [p.get("configId") for m, p in h.session.calls if m == "session/set_config_option"]
        self.assertEqual(configs, ["model"])   # dev 无 manual_mode：不切 mode

    def test_a3_resume_path_no_thinking_request(self):
        h = _StartSession("tech", resume=True)
        self.assertTrue(h.run())
        self.assertEqual(self._thinking_calls(h.session.calls), [])
        configs = [p.get("configId") for m, p in h.session.calls if m == "session/set_config_option"]
        self.assertIn("mode", configs)
        self.assertIn("model", configs)

    def test_a4_config_options_record_observation_without_request(self):
        s = ac.ACPSession.__new__(ac.ACPSession)
        s.req_id = 50002
        s.role = "dev"
        s.session_id = "s-a4"
        s.model_name = None
        s.thinking = None
        s.model = "deepseek/v4.1-flash"
        s.calls = []

        def fake_request(method, params=None, **kwargs):
            s.calls.append((method, params or {}))
            if method == "initialize":
                return {"authMethods": []}
            if method == "session/new":
                return {"sessionId": "s-a4", "configOptions": _opts(
                    thinking="high", levels=["low", "high", "max"])["configOptions"]}
            if method == "session/set_config_option":
                return _opts(thinking="high", levels=["low", "high", "max"])
            return {}

        s.request = fake_request
        s._readonly_mcp_servers = lambda workdir: []
        s._extra_mcp_servers = []
        s.event_sink = None
        s.tool_profile = None
        s.start(Path(tempfile.gettempdir()))
        self.assertEqual(self._thinking_calls(s.calls), [])
        self.assertEqual(s.thinking, "high")


class DispatcherWiringTest(unittest.TestCase):

    def test_a6_no_thinking_channel_in_dispatcher(self):
        src = dispatcher_source()
        self.assertNotIn("role_thinking", src)
        self.assertNotIn("post_thinking_events", src)
        self.assertNotIn('kind="thinking"', src)
        self.assertNotIn("thinking=role_thinking", src)
        self.assertFalse(hasattr(d, "role_thinking"))
        self.assertFalse(hasattr(d, "post_thinking_events"))

    def test_a7_no_thinking_logs(self):
        h = _StartSession("tech")
        with mock.patch.object(ac, "log") as log:
            h.run()
        rendered = str(log.method_calls)
        self.assertNotIn("推理深度", rendered)
        self.assertNotIn("思考档位", rendered)


class BoardSessionsTest(unittest.TestCase):

    def test_a8_board_session_construction_without_thinking(self):
        src = backend_text()
        sites = list(re.finditer(r'ACPSession(?:"\))?\(', src))
        self.assertEqual(len(sites), 3, "建卡 + 需求助手 + 飞书助手 三处构造点")
        for m in sites:
            seg = src[m.start():m.start() + 900].split("kimi_bin=")[0]
            self.assertNotIn("thinking=", seg)


class FlowConfigIgnoreTest(unittest.TestCase):

    def test_a9_legacy_thinking_key_ignored(self):
        tmp = Path(tempfile.mkdtemp(prefix="req50-flow-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        raw = json.loads((REPO / "flows.json").read_text(encoding="utf-8"))
        raw["roles"]["dev"]["thinking"] = "high"
        raw["roles"]["dev"]["thinking"] = "not-in-vocab"   # 旧键不再校验
        cfg_path = tmp / "flows.json"
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        got = ac.load_flow_config(str(cfg_path), base_dir=str(REPO))
        self.assertNotIn("thinking", got["roles"]["dev"])


class ObservationStateTest(unittest.TestCase):
    """A10/A11：_apply_config 只维护观测值（自 test_thinking_runtime_options.py S2/S3 迁移）。"""

    def _stub(self):
        s = ac.ACPSession.__new__(ac.ACPSession)
        s.req_id = 50003
        s.session_id = "s-obs"
        s.model_name = None
        s.thinking = None
        return s

    def test_a10_model_without_thinking_clears_stale_value(self):
        s = self._stub()
        s._apply_config(_opts("deepseek/v4.1-flash", "high", ["low", "high"]))
        self.assertEqual(s.thinking, "high")
        s._apply_config(_opts("gpt-6-sol"))           # 切到无思考配置项的模型
        self.assertIsNone(s.thinking)

    def test_a11_response_without_config_options_keeps_state(self):
        s = self._stub()
        s._apply_config(_opts("deepseek/v4.1-flash", "high", ["low", "high"]))
        s._apply_config({})
        self.assertEqual(s.thinking, "high")


class SourceNegativeTest(unittest.TestCase):
    """A12：源级负断言——请求/提示行/列/样式不得复活。"""

    def test_a12_frontend_tokens_removed(self):
        js = frontend_js()
        for token in ("thinkingAlertText", "session-thinking-warn", "sess-thinking-alert",
                      "思考回落：", "思考深度", ".rb-think", "thinking_levels", "payload.thinking"):
            self.assertNotIn(token, js, token)
        self.assertIn("modelAlertText", js)            # 模型回落提示保留
        self.assertIn("session-model-warn", js)
        self.assertIn("sess-model-alert", js)

    def test_a12_css_tokens_removed(self):
        css = frontend_css()
        for token in (".rb-think", ".session-thinking-warn", ".sess-thinking-alert", "思考档位回落"):
            self.assertNotIn(token, css, token)
        self.assertIn(".session-model-warn", css)

    def test_a12_backend_tokens_removed(self):
        src = backend_text() + (REPO / "acp_shared.py").read_text(encoding="utf-8")
        for token in ("role_thinking", "ACP_THINKING", "THINKING_LEVELS", "过滤思考深度"):
            self.assertNotIn(token, src, token)


if __name__ == "__main__":
    unittest.main()
