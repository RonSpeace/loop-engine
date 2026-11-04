#!/usr/bin/env python3
"""#42（req-42）缓存命中指标测试：normalize_provider_usage / parse_usage_cache /
ACPSession 采集（prompt 响应、update 嗅探、collect_usage 兼容）/ post_run_record 落库判据。

U1 字段超集映射；U2 非法形态双 None；U3 /usage 缓存行解析；U4 prompt 响应带 usage → hit；
U5 无 usage=unknown、首轮即失败=na、失败轮不计数；U6 update 嗅探 + 一次性日志不变；
U7 collect_usage 三值形状不变、原文进 raw、/usage 不计轮；U8 多轮求和 + raw 截断；
U9 post_run_record 判据与峰值解耦（unknown 不降 na / na token NULL / 超时前 hit 照记）；
U10 POST /runs 请求体含 4 个新字段。

ACPSession 用 __new__ 桩（不走 __init__，不拉 kimi acp 子进程；prompt 依赖的
_activity/_rearm_guard_for_prompt/_guard_mark_llm 对缺省属性零行为，桩安全）。

运行（worktree 内，用主工作区 .venv）：
    .venv/bin/python -m pytest tests/test_usage_cache_metrics.py -q
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402


def _stub_session():
    """ACPSession.__new__ 桩：不执行 __init__（不 spawn 子进程）；补 prompt() 构造请求参数
    必需的 session_id。"""
    s = ac.ACPSession.__new__(ac.ACPSession)
    s.session_id = "sid-stub"
    return s


class U1NormalizeProviderUsage(unittest.TestCase):
    """U1：字段名超集逐一映射（camelCase/snake_case/别名）"""

    def test_field_superset_mapping(self):
        cases = [
            ({"cachedTokens": 100, "inputTokens": 500}, {"cached_tokens": 100, "input_tokens": 500}),
            ({"cached_tokens": 0, "input_tokens": 1}, {"cached_tokens": 0, "input_tokens": 1}),  # 显式 0 也是返回
            ({"cache_read_tokens": 12}, {"cached_tokens": 12, "input_tokens": None}),
            ({"cacheReadInputTokens": 34, "prompt_tokens": 9}, {"cached_tokens": 34, "input_tokens": 9}),
            ({"prompt_cache_hit_tokens": 7, "promptTokens": 8}, {"cached_tokens": 7, "input_tokens": 8}),
            ({"promptCacheHitTokens": 7.0, "inputTokens": 8.0}, {"cached_tokens": 7, "input_tokens": 8}),
        ]
        for obj, expect in cases:
            self.assertEqual(ac.normalize_provider_usage(obj), expect, obj)


class U2InvalidShapes(unittest.TestCase):
    """U2：非 dict/无字段/负数/非数字/bool → 双 None，不抛异常"""

    def test_invalid_shapes_none(self):
        for obj in (None, "x", 3, [], {}, {"cachedTokens": -1}, {"inputTokens": "x"},
                    {"cachedTokens": True}, {"cachedTokens": [1]}, {"cached": 5}):
            r = ac.normalize_provider_usage(obj)
            self.assertEqual(r, {"cached_tokens": None, "input_tokens": None}, obj)

    def test_float_number_is_int_captured(self):
        self.assertEqual(ac.normalize_provider_usage({"cachedTokens": 1.5})["cached_tokens"], 1)


class U3ParseUsageCache(unittest.TestCase):
    """U3：/usage 文本缓存行候选解析（千分位/小数）；无缓存行/百分比行 → None"""

    def test_cache_line_parsed(self):
        for text, expect in (
            ("Context: 1,000 / 2,000 tokens (50%)\nCache hit: 1,234 tokens", 1234),
            ("缓存命中：2,048 tokens", 2048),
            ("cached tokens: 350.0", 350),
        ):
            self.assertEqual(ac.parse_usage_cache(text)["cached_tokens"], expect, text)
            self.assertIsNone(ac.parse_usage_cache(text)["input_tokens"], text)

    def test_no_cache_line_none(self):
        for text in ("", "Context: 60,000 / 600,000 tokens (10%)", "no usage here"):
            self.assertIsNone(ac.parse_usage_cache(text)["cached_tokens"], text)

    def test_percent_line_not_misread(self):
        self.assertIsNone(ac.parse_usage_cache("Cache hit rate: 12%")["cached_tokens"])


class U4PromptUsageCaptured(unittest.TestCase):
    """U4：prompt() 响应带 usage → 采集累积、完成轮计数、cache_summary=hit（显式 0 也是 hit）"""

    def test_response_usage_hit(self):
        s = _stub_session()
        s.request = mock.Mock(return_value={"usage": {"cachedTokens": 100, "inputTokens": 500}})
        s.prompt("hi", 10)
        self.assertEqual(s._llm_turns_done, 1)
        summ = s.cache_summary()
        self.assertEqual(summ["state"], "hit")
        self.assertEqual((summ["cached_tokens"], summ["input_tokens"]), (100, 500))
        self.assertIn("cachedTokens", summ["raw"])           # raw = 原始 usage 对象序列化

    def test_explicit_zero_is_hit(self):
        s = _stub_session()
        s.request = mock.Mock(return_value={"usage": {"cached_tokens": 0, "input_tokens": 2000}})
        s.prompt("hi", 5)
        summ = s.cache_summary()
        self.assertEqual(summ["state"], "hit")
        self.assertEqual(summ["cached_tokens"], 0)           # 报 0 是真实返回，不是 unknown
        self.assertEqual(summ["input_tokens"], 2000)

    def test_flat_result_keys_also_captured(self):
        s = _stub_session()
        s.request = mock.Mock(return_value={"cached_tokens": 42, "input_tokens": 9})
        s.prompt("hi", 5)
        self.assertEqual(s.cache_summary()["cached_tokens"], 42)


class U5UnknownAndNa(unittest.TestCase):
    """U5：全程无 usage=unknown；首轮即超时/中止/断连=na（计数不在 finally，失败轮不计）；
    超时前已有成功轮 → 照记 hit 不降级"""

    def test_no_usage_anywhere_unknown(self):
        s = _stub_session()
        s.request = mock.Mock(return_value={})
        s.prompt("hi", 5)
        self.assertEqual(s._llm_turns_done, 1)
        summ = s.cache_summary()
        self.assertEqual(summ["state"], "unknown")
        self.assertIsNone(summ["cached_tokens"])
        self.assertIsNone(summ["input_tokens"])

    def test_first_turn_timeout_na(self):
        s = _stub_session()
        s.request = mock.Mock(side_effect=TimeoutError("ACP session/prompt 超时"))
        with self.assertRaises(TimeoutError):
            s.prompt("hi", 5)
        self.assertEqual(getattr(s, "_llm_turns_done", 0), 0)   # 失败轮不计完成请求（桩上属性未建即为 0）
        summ = s.cache_summary()
        self.assertEqual(summ["state"], "na")
        self.assertIsNone(summ["cached_tokens"])

    def test_abort_and_disconnect_na(self):
        for exc in (ac.TurnAborted("输出守卫中止"), RuntimeError("ACP 连接断开")):
            s = _stub_session()
            s.request = mock.Mock(side_effect=exc)
            with self.assertRaises(Exception):
                s.prompt("hi", 5)
            self.assertEqual(getattr(s, "_llm_turns_done", 0), 0)
            self.assertEqual(s.cache_summary()["state"], "na")

    def test_prior_success_then_timeout_keeps_hit(self):
        s = _stub_session()
        s.request = mock.Mock(side_effect=[{"usage": {"cached_tokens": 9, "input_tokens": 3}},
                                           TimeoutError("t")])
        s.prompt("a", 5)
        with self.assertRaises(TimeoutError):
            s.prompt("b", 5)
        self.assertEqual(s._llm_turns_done, 1)
        summ = s.cache_summary()
        self.assertEqual(summ["state"], "hit")
        self.assertEqual(summ["cached_tokens"], 9)


class U6UpdateSniff(unittest.TestCase):
    """U6：session/update 携带 usage dict（未知 kind）→ 采集成功；未知 kind 一次性日志行为不变"""

    def test_update_usage_captured_and_once_log_kept(self):
        s = _stub_session()
        with mock.patch.object(ac.log, "info") as li:
            s._on_update({"sessionUpdate": "mystery_kind", "usage": {"cached_tokens": 5, "input_tokens": 7}})
            s._on_update({"sessionUpdate": "mystery_kind"})
        summ = s.cache_summary()
        self.assertEqual(summ["state"], "hit")
        self.assertEqual((summ["cached_tokens"], summ["input_tokens"]), (5, 7))
        kind_logs = [c for c in li.call_args_list if "mystery_kind" in str(c)]
        self.assertEqual(len(kind_logs), 1)                  # 每类未知 kind 只记一次

    def test_update_without_usage_noop(self):
        s = _stub_session()
        s._on_update({"sessionUpdate": "mystery_kind"})
        self.assertEqual(s.cache_summary()["state"], "na")   # 无 usage、无完成轮 → na


class U7CollectUsageCompat(unittest.TestCase):
    """U7：collect_usage 返回三值形状不变（建卡会话兼容）；原文片段进 raw；
    /usage 斜杠命令不计入 _llm_turns_done；/usage 文本缓存行 = hit"""

    def test_collect_usage_shape_and_raw(self):
        s = _stub_session()

        def fake_request(method, params, timeout=60, guard=False):
            s._capture.append("Context: 1,000 / 2,000 tokens (50%)\nCache: 300 tokens")
            return {}

        s.request = mock.Mock(side_effect=fake_request)
        s._flush_message = lambda: None
        usage = s.collect_usage()
        self.assertEqual(usage, {"context_tokens": 1000, "max_tokens": 2000, "context_pct": "50%"})
        self.assertEqual(getattr(s, "_llm_turns_done", 0), 0)   # /usage 不是真实模型请求
        summ = s.cache_summary()
        self.assertEqual(summ["state"], "hit")               # 原文含缓存行 = 供应商真实返回
        self.assertEqual(summ["cached_tokens"], 300)
        self.assertIn("Cache: 300", summ["raw"])

    def test_collect_usage_failure_none(self):
        s = _stub_session()
        s.request = mock.Mock(side_effect=TimeoutError("t"))
        s._flush_message = lambda: None
        self.assertIsNone(s.collect_usage())
        self.assertEqual(s.cache_summary()["state"], "na")
        self.assertIsNone(s.cache_summary()["cached_tokens"])


class U8MultiTurnSum(unittest.TestCase):
    """U8：多轮求和正确；raw 截断 2048"""

    def test_multi_turn_sum_and_raw_truncated(self):
        s = _stub_session()
        big = {"usage": {"cached_tokens": 3, "input_tokens": 10, "pad": "x" * 4000}}
        s.request = mock.Mock(side_effect=[big,
                                           {"usage": {"cached_tokens": 4, "input_tokens": 6}},
                                           {}])
        s.prompt("a", 5)
        s.prompt("b", 5)
        s.prompt("c", 5)
        self.assertEqual(s._llm_turns_done, 3)
        summ = s.cache_summary()
        self.assertEqual(summ["state"], "hit")
        self.assertEqual((summ["cached_tokens"], summ["input_tokens"]), (7, 16))
        self.assertLessEqual(len(summ["raw"]), 2048)


class U9PostRunRecordDecoupled(unittest.TestCase):
    """U9：post_run_record 判据与峰值 usage 解耦——unknown+峰值 None 仍记 unknown；
    na → cache_state=na 且 token NULL；hit → hit+数值；超时前已有成功轮照记 hit。
    （usage_ext 由调用方 run_agent 传 session.cache_summary() 结果，本组直接按调用形态构造）"""

    def setUp(self):
        self.bodies = []
        self._api = d.api
        self._post_event = d.post_event
        d.api = lambda method, path, body=None: self.bodies.append(body) or {"success": True}
        d.post_event = lambda *a, **k: None

    def tearDown(self):
        d.api = self._api
        d.post_event = self._post_event

    def _session(self):
        s = mock.Mock()
        s.session_id = "sid-42"
        s._tool_calls = {}
        return s

    def test_unknown_with_peak_none_stays_unknown(self):
        d.post_run_record({"id": 42}, "dev", self._session(), 1.0, 2.0, 0, False, None,
                          usage_ext={"state": "unknown", "cached_tokens": None,
                                     "input_tokens": None, "raw": "txt"})
        b = self.bodies[-1]
        self.assertEqual(b["cache_state"], "unknown")        # 不因峰值解析失败降 na
        self.assertIsNone(b["cached_tokens"])
        self.assertIsNone(b["context_tokens"])               # 峰值 None 仅影响峰值三字段

    def test_na_tokens_null_even_when_timed_out(self):
        d.post_run_record({"id": 42}, "dev", self._session(), 1.0, 2.0, 0, True, None,
                          usage_ext={"state": "na", "cached_tokens": None,
                                     "input_tokens": None, "raw": None})
        b = self.bodies[-1]
        self.assertEqual(b["status"], "timed_out")
        self.assertEqual(b["cache_state"], "na")
        self.assertIsNone(b["cached_tokens"])
        self.assertIsNone(b["input_tokens"])

    def test_hit_records_numbers_and_raw(self):
        d.post_run_record({"id": 42}, "dev", self._session(), 1.0, 2.0, 0, False,
                          {"context_tokens": 5, "max_tokens": 6, "context_pct": "1%"},
                          usage_ext={"state": "hit", "cached_tokens": 12,
                                     "input_tokens": 100, "raw": "r"})
        b = self.bodies[-1]
        self.assertEqual(b["cache_state"], "hit")
        self.assertEqual((b["cached_tokens"], b["input_tokens"]), (12, 100))
        self.assertEqual(b["usage_raw"], "r")

    def test_timeout_with_prior_hit_keeps_hit(self):
        d.post_run_record({"id": 42}, "dev", self._session(), 1.0, 2.0, 0, True, None,
                          usage_ext={"state": "hit", "cached_tokens": 8,
                                     "input_tokens": 9, "raw": None})
        b = self.bodies[-1]
        self.assertEqual(b["status"], "timed_out")
        self.assertEqual(b["cache_state"], "hit")
        self.assertEqual(b["cached_tokens"], 8)

    def test_no_usage_ext_fields_absent_safe(self):
        """usage_ext 缺省（None）→ 四字段为 None（旧调用方形态不炸）"""
        d.post_run_record({"id": 42}, "dev", None, 1.0, 2.0, 1, False, None)
        b = self.bodies[-1]
        self.assertIsNone(b["cache_state"])
        self.assertIsNone(b["cached_tokens"])
        self.assertIsNone(b["usage_raw"])


class U10RequestBodyFields(unittest.TestCase):
    """U10：POST /runs 请求体含 4 个新字段（mock api 捕获断言）"""

    def test_body_contains_four_new_fields(self):
        bodies = []
        with mock.patch.object(d, "api", lambda m, p, body=None: bodies.append(body) or {}), \
                mock.patch.object(d, "post_event", lambda *a, **k: None):
            s = mock.Mock()
            s.session_id = "sid"
            s._tool_calls = {}
            d.post_run_record({"id": 42}, "dev", s, 1.0, 2.0, 0, False, None,
                              usage_ext={"state": "hit", "cached_tokens": 1,
                                         "input_tokens": 2, "raw": "x"})
        for k in ("cached_tokens", "input_tokens", "cache_state", "usage_raw"):
            self.assertIn(k, bodies[-1], k)
        self.assertEqual(bodies[-1]["cached_tokens"], 1)


if __name__ == "__main__":
    unittest.main()
