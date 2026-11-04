#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""req-28 输出守卫测试：agent 复读 / 单次请求超长时的事件持续在写兜底。

背景（需求 #28）：2026-10-12 卡 #25 方案评审会话（grok-4.7）在单次 LLM 请求内持续输出同一句话，
agent_events 新增 220+ 条、零工具调用、无新请求；#421 的「心跳与最新事件双静默 >180s」判据不触发，
只能等 60 分钟运行上限。本改动给 ACPSession 加两个「本轮无效」判据：
- 判据 A（stall_request）：单次 LLM 请求在途时长超 GUARD_REQUEST_SEC（工具执行期不计时）；
- 判据 B（repeat_output）：同会话连续 GUARD_REPEAT_N 条 message 相邻文本相似度 ≥ GUARD_REPEAT_SIM。
触发即 guard_sink 留痕 + cancel 本会话 + 唤醒在途 prompt 抛 TurnAborted（只影响本会话）。

分层：
- G1-G2（L0）：常量默认/env 覆盖、repetitive_run 纯函数。
- G3-G11（L0 桩）：判据 B 正反例与边界、判据 A 与工具边界、prompt 唤醒抛 TurnAborted。
- G12-G19（dispatcher 侧）：run_agent 首轮/续接轮中止集成、状态映射、归因、隔离、守卫关闭、
  检查函数绝不上抛、留痕三落点。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_output_guard -v
"""
import inspect
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402


# ---------------- 桩会话（__new__ 绕过 __init__，不起 kimi 子进程） ----------------
def _stub(guard=True):
    s = ac.ACPSession.__new__(ac.ACPSession)
    s.req_id = 28
    s.session_id = "session_28"
    s.role = "dev"
    s._msg_buf = []
    s._buf_lock = threading.Lock()
    s._last_chunk_at = 0.0
    s._capture = None
    s._tool_calls = {}
    s._tool_posted = set()
    s._tool_cmds = {}
    s._resp_pending = False
    s._out_sent_mono = {}
    s._tool_open = set()
    s._thought_seen_logged = False
    s._unknown_updates_logged = set()
    s._closed = False
    s._pending = {}
    s._next_id = 0
    s.events = []
    s.guard_calls = []
    s.cancel_count = 0
    s._emit = lambda kind, content, session_id=None: s.events.append((kind, content))
    s.activity_sink = None
    s.guard_sink = (lambda kind, detail: s.guard_calls.append((kind, detail))) if guard else None
    s._guard = s._new_guard() if guard else None
    s._guard_lock = threading.Lock()
    s._guard_pending = None
    s.cancel = lambda: setattr(s, "cancel_count", s.cancel_count + 1)
    return s


def _feed(s, text):
    """把一段文本当作一条 message 事件上报（等价 _on_update 攒满缓冲后 _flush_message）。"""
    s._msg_buf = [text]
    s._last_chunk_at = time.time()
    s._flush_message()


def _chunk(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


# 长且互不相同 / 相邻中度相似 / 复读的构造文本
def _distinct(n, length=220):
    """n 段互不相同的长文本（每段用互不重叠的汉字区间 → 段间几乎无公共字符，相似度极低）。"""
    out = []
    for i in range(n):
        base = 0x4E00 + i * 400
        body = "".join(chr(base + (j % 300)) for j in range(length))
        out.append(f"第{i}段：" + body)
    return out


def _moderate(n=8, shared=210, diff=90):
    """n 段相邻相似度 ≈ 0.7（落在 0.6~0.75，验收 2 误判边界）的中度同构长输出。"""
    head = "HEAD" + "x" * (shared - 4)
    return [head + chr(ord("A") + i) * diff for i in range(n)]


# 子进程内起守卫桩（G1 env 覆盖行为级验证）：喂 8 条输出后打印 guard_sink 收到的 kind 列表
_GUARD_STUB = r'''
import sys, threading
sys.path.insert(0, {repo!r})
import acp_shared as ac
s = ac.ACPSession.__new__(ac.ACPSession)
s.req_id = 1
s.session_id = "s"
s._msg_buf = []
s._buf_lock = threading.Lock()
s._last_chunk_at = 0.0
s._guard_pending = None
s._guard = ac.ACPSession._new_guard(s)
s._guard_lock = threading.Lock()
s.guard_calls = []
s.guard_sink = lambda k, d: s.guard_calls.append(k)
s.cancel = lambda: None
s._emit = lambda *a, **k: None
s._activity = lambda *a, **k: None
if {kind!r} == "sliding":
    base = "ABCDEFG" * 200
    texts = [base[i * 137:(i + 1) * 137] for i in range(8)]
else:
    texts = ["重复的句子" * 30] * 8
for t in texts:
    s._msg_buf = [t]
    s._flush_message()
print(s.guard_calls)
'''


class G1Constants(unittest.TestCase):
    """G1：常量默认值与 env 覆盖（模块加载时读取，单一源）"""

    def test_g1_defaults(self):
        self.assertEqual(ac.GUARD_REQUEST_SEC, 1200)
        self.assertEqual(ac.GUARD_REPEAT_N, 8)
        self.assertEqual(ac.GUARD_REPEAT_SIM, 0.8)
        self.assertEqual(ac.GUARD_REPEAT_MIN_LEN, 100)

    def test_g1_env_override(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); import acp_shared as ac; "
                "print(ac.GUARD_REQUEST_SEC, ac.GUARD_REPEAT_N, ac.GUARD_REPEAT_SIM, "
                "ac.GUARD_REPEAT_MIN_LEN)") % str(REPO)
        env = {k: v for k, v in os.environ.items() if not k.startswith("KIMI_GUARD_")}
        env.update({"KIMI_GUARD_REQUEST_SEC": "600", "KIMI_GUARD_REPEAT_N": "4",
                    "KIMI_GUARD_REPEAT_SIM": "0.9", "KIMI_GUARD_REPEAT_MIN_LEN": "50"})
        out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
        self.assertEqual(out.strip(), "600.0 4 0.9 50")

    def test_g1_env_override_drives_guard_behavior(self):
        """评审阻塞复修：env 覆盖必须真正作用于判定（不是只改了常量）。
        子进程内起守卫桩喂输出，验证 sim / min_len 覆盖改变触发结果。"""
        self.assertEqual(self._stub_guard("sliding", {}), "['repeat_output']",
                         "默认 0.8：滑动窗口切分（相邻 0.978）触发")
        self.assertEqual(self._stub_guard("sliding", {"KIMI_GUARD_REPEAT_SIM": "0.99"}), "[]",
                         "覆盖 sim=0.99：0.978 < 0.99 不触发")
        self.assertEqual(self._stub_guard("identical", {}), "['repeat_output']")
        self.assertEqual(self._stub_guard("identical", {"KIMI_GUARD_REPEAT_MIN_LEN": "10000"}), "[]",
                         "覆盖 min_len=10000：240 字块不入队、不触发")

    def test_g1_runtime_constant_drives_guard(self):
        """阈值常量在**调用时刻**读取：运行时改写常量同样改变判定（不在函数定义处绑定旧值）"""
        base = "ABCDEFG" * 200
        chunks = [base[i * 137:(i + 1) * 137] for i in range(8)]
        s = _stub()
        with mock.patch.object(ac, "GUARD_REPEAT_SIM", 0.99):
            for t in chunks:
                _feed(s, t)
        self.assertEqual(s.guard_calls, [], "sim 提到 0.99 → 0.978 不触发")
        s2 = _stub()
        with mock.patch.object(ac, "GUARD_REPEAT_MIN_LEN", 10000):
            for _ in range(8):
                _feed(s2, "重复的句子" * 30)
        self.assertEqual(s2.guard_calls, [], "min_len 提到 10000 → 长 240 字块不入队")
        s3 = _stub()
        with mock.patch.object(ac, "GUARD_REPEAT_SIM", 0.5):
            for t in _moderate(8):        # 相邻 ~0.7
                _feed(s3, t)
        self.assertEqual(len(s3.guard_calls), 1,
                         "sim 降到 0.5：相邻 0.7 由「不触发」变「触发」——证明阈值在调用时刻被读取")
        self.assertFalse(ac.repetitive_run(_moderate(8)), "默认 0.8 下 0.7 判 False（对照）")
        with mock.patch.object(ac, "GUARD_REPEAT_SIM", 0.5):
            self.assertTrue(ac.repetitive_run(_moderate(8)),
                            "repetitive_run 缺省阈值取调用时刻的模块常量")

    def _stub_guard(self, kind, extra_env):
        """子进程内构造守卫桩喂输出；返回 guard_sink 收到的 kind 列表字符串。"""
        import subprocess
        code = _GUARD_STUB.format(repo=str(REPO), kind=kind)
        env = {k: v for k, v in os.environ.items() if not k.startswith("KIMI_GUARD_")}
        env.update(extra_env)
        return subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip()


class G2RepetitiveRun(unittest.TestCase):
    """G2：repetitive_run 纯函数（相同 / 滑动窗口切分 → True；不足 / 太短 → False）"""

    def test_g2_identical(self):
        self.assertTrue(ac.repetitive_run(["同一句话" * 40] * 8))

    def test_g2_sliding_window(self):
        """同一句的滑动窗口切分（步长与句长互质）→ 相邻块高相似 → True"""
        s = "ABCDEFG" * 200
        chunks = [s[i * 137:(i + 1) * 137] for i in range(8)]
        self.assertTrue(ac.repetitive_run(chunks))

    def test_g2_short_and_insufficient(self):
        self.assertFalse(ac.repetitive_run(["短" * 10] * 8))          # 全短于 min_len
        self.assertFalse(ac.repetitive_run(["同一句话" * 40]))         # 单条无相邻对
        self.assertFalse(ac.repetitive_run(_distinct(8)))              # 8 段互不相同


class G3RepeatTriggers(unittest.TestCase):
    """G3：判据 B 端到端——连续 N 条相似 message → 留痕 + cancel 恰好一次、后续不重复触发"""

    def test_g3_triggers_once_and_not_again(self):
        s = _stub()
        for _ in range(8):
            _feed(s, "重复的句子" * 30)
        self.assertEqual(len(s.guard_calls), 1)
        self.assertEqual(s.guard_calls[0][0], "repeat_output")
        self.assertEqual(s.cancel_count, 1)
        self.assertEqual(s._guard["tripped"]["kind"], "repeat_output")
        for _ in range(3):
            _feed(s, "重复的句子" * 30)
        self.assertEqual(len(s.guard_calls), 1, "单次置位：已中止后不再重复触发")
        self.assertEqual(s.cancel_count, 1)

    def test_g3_concurrent_single_trip(self):
        """并发双线程各喂复读 → 原子 check-and-set 保证只触发一次"""
        s = _stub()

        def _worker():
            for _ in range(8):
                _feed(s, "并发复读" * 40)

        ts = [threading.Thread(target=_worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(s.guard_calls), 1)
        self.assertEqual(s.cancel_count, 1)


class G4G5G6RepeatNegatives(unittest.TestCase):
    """G4-G6：判据 B 反例——充分不同 / 中度相似（0.6~0.75）/ 不足 N-1 对"""

    def test_g4_distinct_not_triggered(self):
        s = _stub()
        for t in _distinct(8):
            _feed(s, t)
        self.assertEqual(s.guard_calls, [])
        self.assertEqual(s.cancel_count, 0)

    def test_g5_moderate_similarity_not_triggered(self):
        """验收 2 误判边界：相邻 0.6~0.75 的合法同构长输出不得命中"""
        texts = _moderate(8)
        ratios = [round(ac.difflib.SequenceMatcher(
            None, texts[i][:ac.GUARD_SIM_LEN_CAP], texts[i + 1][:ac.GUARD_SIM_LEN_CAP],
            autojunk=False).ratio(), 3) for i in range(len(texts) - 1)]
        self.assertTrue(all(0.6 <= r <= 0.75 for r in ratios), ratios)
        s = _stub()
        for t in texts:
            _feed(s, t)
        self.assertEqual(s.guard_calls, [])
        self.assertEqual(s.cancel_count, 0)

    def test_g6_insufficient_pairs_not_triggered(self):
        """仅前 4 条相邻对 ≥0.8（3 对），其余转不同 → 不足 N-1 对，不触发"""
        same = "复读片段" * 60
        texts = [same, same, same, same] + _distinct(4)
        s = _stub()
        for t in texts:
            _feed(s, t)
        self.assertEqual(s.guard_calls, [])
        self.assertEqual(s.cancel_count, 0)


class G7RequestGuard(unittest.TestCase):
    """G7：判据 A——在途时长超阈值触发，阈值内不触发"""

    def test_g7_stall_triggers(self):
        s = _stub()
        s._guard["llm_active"] = True
        s._guard["llm_started_mono"] = time.monotonic() - 1300
        s._check_request_guard()
        self.assertEqual(len(s.guard_calls), 1)
        self.assertEqual(s.guard_calls[0][0], "stall_request")
        self.assertEqual(s.cancel_count, 1)

    def test_g7_within_threshold(self):
        s = _stub()
        s._guard["llm_active"] = True
        s._guard["llm_started_mono"] = time.monotonic() - 10
        s._check_request_guard()
        self.assertEqual(s.guard_calls, [])


class G8G9G10ToolBoundary(unittest.TestCase):
    """G8-G10：工具边界——执行期不计时、隐式 tool_end 接守卫、长工具回合不误伤"""

    def test_g8_explicit_tool_path(self):
        s = _stub()
        s._guard["llm_active"] = True
        s._guard["llm_started_mono"] = time.monotonic() - 5000   # 起点极早
        s._note_tool_step("t1", {"status": "in_progress"}, started=True)
        self.assertFalse(s._guard["llm_active"], "工具执行期 llm 计时暂停")
        s._check_request_guard()
        self.assertEqual(s.guard_calls, [], "工具执行期不计时 → 不触发")
        s._note_tool_step("t1", {"status": "completed"})
        self.assertTrue(s._guard["llm_active"], "tool_end 后恢复计时")
        self.assertGreater(s._guard["llm_started_mono"], time.monotonic() - 1, "tool_end 重置起点")
        s._check_request_guard()
        self.assertEqual(s.guard_calls, [])

    def test_g9_implicit_tool_end(self):
        """评审阻塞 2：隐式兜底 tool_end（上游 completed 缺失）也须接守卫，防 llm_active 停在 False"""
        s = _stub()
        s._tool_open = {"t1"}
        s._guard["llm_active"] = False
        s._guard["llm_started_mono"] = time.monotonic() - 5000
        s._on_update(_chunk("工具后继续输出"))
        self.assertTrue(s._guard["llm_active"], "隐式 tool_end 后 llm 计时恢复")
        self.assertGreater(s._guard["llm_started_mono"], time.monotonic() - 1)

    def test_g10_long_tool_turn_not_triggered(self):
        """单轮内多轮工具交替（总时长远超阈值，但每段 llm 活跃窗口 < 阈值）→ 不触发"""
        s = _stub()
        s._guard["llm_active"] = True
        s._guard["llm_started_mono"] = time.monotonic() - 1000
        s._note_tool_step("t1", {"status": "in_progress"}, started=True)   # 暂停
        s._check_request_guard()
        s._note_tool_step("t1", {"status": "completed"})                  # 重置起点
        s._check_request_guard()
        self.assertEqual(s.guard_calls, [])
        self.assertEqual(s.cancel_count, 0)


class G11PromptWakeup(unittest.TestCase):
    """G11：prompt() 在途被守卫唤醒 → 抛 TurnAborted；未触发正常返回"""

    def test_g11_prompt_raises_turn_aborted(self):
        s = _stub()
        s._send = lambda msg: None      # 不真有进程
        done = {"tripped": False}

        def _trip_soon():
            for _ in range(500):
                if s._guard_pending is not None:
                    break
                time.sleep(0.01)
            s._trip_guard("repeat_output", "test")
            done["tripped"] = True

        t = threading.Thread(target=_trip_soon)
        t.start()
        with self.assertRaises(ac.TurnAborted):
            s.prompt("hello", timeout=10)
        t.join(timeout=5)
        self.assertTrue(done["tripped"])
        self.assertEqual(len(s.guard_calls), 1)
        self.assertEqual(s.cancel_count, 1)

    def test_g11_prompt_returns_without_trip(self):
        s = _stub()

        def _send(msg):
            p = s._pending[msg["id"]]
            p["response"] = {"result": {"ok": True}}
            p["event"].set()

        s._send = _send
        self.assertEqual(s.prompt("hello", timeout=5), {"ok": True})
        self.assertEqual(s.guard_calls, [])


# ---------------- dispatcher 侧集成（run_agent 桩） ----------------
class FakeProc(types.SimpleNamespace):
    pass


class FakeSession:
    """ACPSession 替身：记录 prompt 调用，按顺序注入行为（None=正常返回）。"""

    def __init__(self, behaviours=None):
        self.session_id = "session_guard_test"
        self.model_name = "test"
        self.thinking = "low"
        self.proc = FakeProc(pid=12345, returncode=0)
        self.prompts = []
        self.behaviours = behaviours or []
        self.cancelled = 0
        self.closed = False
        self.closed_kill = None
        self.usage_called = False
        self.output_guard = None

    def start(self, workdir, resume_session_id=None):
        return False

    def prompt(self, text, timeout):
        self.prompts.append((text, timeout))
        idx = len(self.prompts) - 1
        if idx < len(self.behaviours) and self.behaviours[idx]:
            self.behaviours[idx](self)
        return {}

    def collect_usage(self):
        self.usage_called = True
        return None

    def cancel(self):
        self.cancelled += 1

    def close(self, kill=False):
        self.closed = True
        self.closed_kill = kill

    def _flush_message(self):
        pass

    def rearm_output_guard(self, sink):
        self.output_guard = sink


def _raise(exc):
    def _f(_s):
        raise exc
    return _f


_D_REALS = {name: getattr(d, name) for name in (
    "ACPSession", "artifact_dir", "workdir_for", "dev_workdir_for", "post_event",
    "post_run_record", "handle_result", "update_session_info", "backfill_spawn_session",
    "kimi_version", "readonly_tool_roots", "dev_branch", "owner_key", "api_board",
    "post_dispatcher_event", "append_summary", "_hold_session",
)}


class RunAgentGuardTest(unittest.TestCase):
    """G12-G13/G19：run_agent 首轮与续接轮的 TurnAborted 收束（不走无结果续接、不进保持池）"""

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in _D_REALS}
        self.art = Path(tempfile.mkdtemp(prefix="req28-run-art-"))
        self.wd = Path(tempfile.mkdtemp(prefix="req28-run-wd-"))
        self.events = []
        self.runs = []
        self.results = []
        self.dispatch = []
        self.summaries = []
        self.held = []
        d.artifact_dir = lambda req: self.art
        d.workdir_for = lambda project: self.wd
        d.dev_workdir_for = lambda req: self.wd
        d.post_event = lambda req_id, kind, content, session_id=None: self.events.append((kind, content))
        d.post_run_record = lambda *a, **k: self.runs.append((a, k))
        d.post_dispatcher_event = lambda req_id, content, kind="action", session_id=None: self.dispatch.append(
            (kind, content))
        d.append_summary = lambda req_id, line: self.summaries.append(line)
        d.update_session_info = lambda *a, **k: None
        d.backfill_spawn_session = lambda *a, **k: None
        d.kimi_version = lambda: "test"
        d.readonly_tool_roots = lambda *a, **k: []
        d.dev_branch = lambda req: "f-guard"
        d.owner_key = lambda req: "owner"
        d.api_board = lambda method, path, body=None: {"id": 1}
        d._hold_session = lambda *a, **k: self.held.append(a)
        d.handle_result = lambda req, role, returncode, timed_out, started_at, aborted=False: self.results.append(
            (role, returncode, timed_out, aborted))
        self._sleep = mock.patch.object(d.time, "sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        for name, value in self._orig.items():
            setattr(d, name, value)
        d.active_runs.pop("28002", None)
        shutil.rmtree(self.art, ignore_errors=True)
        shutil.rmtree(self.wd, ignore_errors=True)

    def _req(self):
        return {"id": 28002, "project": "req-board", "title": "T",
                "mode": "semi", "phase": "dev", "flow": "standard", "notes": ""}

    def _run(self, fake, **kw):
        def _factory(*a, **k):
            fake.output_guard = k.get("output_guard")
            return fake
        d.ACPSession = _factory
        d.run_agent(self._req(), "dev", "初始 prompt", started_at=time.time(), **kw)

    def test_g12_first_prompt_abort(self):
        """首轮抛 TurnAborted → 不进续接、不采集 usage、close(kill)、aborted 透传收尾"""
        fake = FakeSession(behaviours=[_raise(d.TurnAborted("x"))])
        self._run(fake)
        self.assertEqual(len(fake.prompts), 1, "中止后不得进「无结果续接」")
        self.assertFalse(fake.usage_called)
        self.assertTrue(fake.closed)
        self.assertTrue(fake.closed_kill)
        self.assertEqual(fake.cancelled, 1)
        self.assertTrue(any("输出守卫" in c for _, c in self.events))
        self.assertEqual(self.runs[-1][1].get("aborted"), True)
        self.assertEqual(self.results[-1], ("dev", 1, False, True))

    def test_g13_followup_prompt_abort(self):
        """续接轮抛 TurnAborted → 不被 except Exception 吞、aborted=True、不进保持池"""
        fake = FakeSession(behaviours=[None, _raise(d.TurnAborted("y"))])
        self._run(fake, hold=True)
        self.assertEqual(len(fake.prompts), 2, "首轮正常 + 续接轮")
        self.assertFalse(fake.usage_called, "续接轮中止后不再采集 usage")
        self.assertTrue(fake.closed_kill)
        self.assertEqual(self.held, [], "带 tripped 会话不得进保持池")
        self.assertTrue(any("续接轮输出守卫中止" in c for _, c in self.events))
        self.assertEqual(self.runs[-1][1].get("aborted"), True)
        self.assertEqual(self.results[-1], ("dev", 0, False, True))

    def test_g19_sink_three_landing_points(self):
        """G19：留痕三落点——post_event(agent_events) / post_dispatcher_event(调度历史) / append_summary"""
        fake = FakeSession(behaviours=[_raise(d.TurnAborted("z"))])
        self._run(fake)
        fake.output_guard("repeat_output", "连续 8 条输出高度重复")
        self.assertTrue(any(k == "system" and "输出守卫中止" in c for k, c in self.events))
        self.assertEqual(self.dispatch[0][0], "action")
        self.assertIn("输出守卫", self.dispatch[0][1])
        self.assertEqual(len(self.summaries), 1)
        self.assertNotIn("\n", self.summaries[0])


class PostRunRecordAborted(unittest.TestCase):
    """G14：post_run_record 状态映射 aborted（优先 timed_out/failed）"""

    def setUp(self):
        self.calls = []
        self.events = []
        self._orig = (d.api, d.post_event)
        d.api = lambda method, path, body=None: self.calls.append((method, path, body)) or {"success": True}
        d.post_event = lambda rid, kind, content, session_id=None: self.events.append((kind, content))

    def tearDown(self):
        d.api, d.post_event = self._orig

    def _session(self):
        s = mock.Mock()
        s.session_id = "session_x"
        s._tool_calls = {"t1": "Read"}
        return s

    def test_g14_aborted_maps(self):
        d.post_run_record({"id": 28}, "dev", self._session(), 1.0, 2.0, -9, False, None, aborted=True)
        self.assertEqual(self.calls[-1][2]["status"], "aborted")
        self.assertEqual(self.events[-1], ("system", "运行结束（aborted）"))

    def test_g14_aborted_precedence_over_timed_out(self):
        d.post_run_record({"id": 28}, "dev", self._session(), 1.0, 2.0, -9, True, None, aborted=True)
        self.assertEqual(self.calls[-1][2]["status"], "aborted")

    def test_g14_default_unchanged(self):
        d.post_run_record({"id": 28}, "dev", self._session(), 1.0, 2.0, 0, False, None)
        self.assertEqual(self.calls[-1][2]["status"], "done")


class HandleResultAborted(unittest.TestCase):
    """G15：handle_result aborted 归因 + 进重试通道 + valid_done 不成立"""

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in (
            "api", "artifact_dir", "workdir_for", "post_event", "spawn_agent", "save_state",
            "set_status", "append_summary", "post_dispatcher_event", "notify_req_card",
            "mark_session_poisoned",
        )}
        self.art = Path(tempfile.mkdtemp(prefix="req28-hr-art-"))
        self.wd = Path(tempfile.mkdtemp(prefix="req28-hr-wd-"))
        self.spawns = []
        self.retries = []
        d.artifact_dir = lambda req: self.art
        d.workdir_for = lambda project: self.wd
        d.api = lambda method, path, body=None: {}
        d.post_event = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.save_state = lambda *a, **k: None
        d.set_status = lambda *a, **k: None
        d.notify_req_card = lambda *a, **k: None
        d.mark_session_poisoned = lambda *a, **k: None
        d.spawn_agent = lambda req, **k: self.spawns.append((req, k))
        self._sleep = mock.patch.object(d.time, "sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        for name, value in self._orig.items():
            setattr(d, name, value)
        shutil.rmtree(self.art, ignore_errors=True)
        shutil.rmtree(self.wd, ignore_errors=True)

    def test_g15_aborted_reason_and_retry(self):
        req = {"id": 28003, "project": "req-board", "title": "T", "status": "open",
               "mode": "semi", "phase": "dev", "flow": "standard", "notes": ""}
        with d.state_lock:
            d.state["retries"].pop("28003", None)
        d.handle_result(req, "dev", -9, False, time.time(), aborted=True)
        with d.state_lock:
            rec = d.state["retries"].get("28003")
        self.assertIsNotNone(rec)
        self.assertIn("输出复读/单次请求超长", rec["last_failure"])
        self.assertEqual(len(self.spawns), 1, "aborted 属自动重试")
        with d.state_lock:
            d.state["retries"].pop("28003", None)
        d.save_state = lambda *a, **k: None


class SessionIsolationAndGuardOff(unittest.TestCase):
    """G16-G18：会话隔离 / 守卫关闭零行为 / 检查函数绝不上抛"""

    def test_g16_isolation(self):
        s1, s2 = _stub(), _stub()
        for _ in range(8):
            _feed(s1, "复读" * 60)
        self.assertEqual(len(s1.guard_calls), 1)
        self.assertEqual(s1.cancel_count, 1)
        self.assertEqual(s2.guard_calls, [], "另一会话不受影响")
        self.assertEqual(s2.cancel_count, 0)

    def test_g17_guard_off_no_side_effect(self):
        s = _stub(guard=False)
        self.assertIsNone(s._guard)
        for _ in range(8):
            _feed(s, "复读" * 60)
        self.assertEqual(s.guard_calls, [])
        self.assertEqual(s.cancel_count, 0)
        self.assertEqual(len(s.events), 8, "守卫关闭时 message 正常上报")
        s._check_request_guard()
        s._guard_mark_llm(True, reset_start=True)

    def test_g18_checks_never_raise(self):
        """注入非法守卫状态 → 检查函数内部异常被自吞（防 _flush_loop 线程/读循环被中断）"""
        s = _stub()
        s._guard = {"recent": object(), "llm_active": True,
                    "llm_started_mono": "not-a-number", "aborted": False, "tripped": None}
        s._msg_buf = ["x" * 200]
        s._flush_message()          # _check_output_guard 内部 append 抛 → 吞
        s._check_request_guard()    # 减法类型错误 → 吞
        s._check_output_guard("y" * 200)
        self.assertEqual(s.guard_calls, [])
        self.assertEqual(s.cancel_count, 0)


class G20Signature(unittest.TestCase):
    """G20：ACPSession.__init__ 追加参数面（output_guard 后仅追加 runtime，两者默认 None）"""

    def test_g20_output_guard_appended_last(self):
        params = list(inspect.signature(ac.ACPSession.__init__).parameters.values())
        names = [p.name for p in params]
        self.assertIn("output_guard", names)
        self.assertEqual(names[-2:], ["output_guard", "runtime"])
        self.assertIsNone(params[-2].default)
        self.assertIsNone(params[-1].default)


if __name__ == "__main__":
    unittest.main()
