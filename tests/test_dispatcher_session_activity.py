#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""req-421 会话活性上报测试：ACPSession 状态切换 → agent_sessions 心跳时间戳。

背景（需求 #421）：右栏会话状态行要能判活/判死——模型请求发出后长时间无响应 = 卡死。
数据源是调度器在**状态切换时**（非定时轮询）落时间戳到 agent_sessions：
`turn_step`（'llm'/'tool'/''）、`turn_step_detail`、`last_llm_request_at`、
`last_llm_response_at`、`last_thought_at`、`last_tool_started_at`（epoch 秒）。

分层：
- D1-D12（L0 纯逻辑）：`ACPSession.__new__` 桩 + activity_sink 收集器，直接喂 `_on_update`
  报文，校验「响应 / 工具 / 思考」三档的上报序列、60s 节流、重新武装、兜底与降级；
  **不拉起任何进程**（#420：本文件不触碰 build_prompt/spawn_agent）。
- D13-D16（L0 dispatcher 侧）：mock `api_board` / `sync_agent_session`，校验 kind→字段映射、
  row_id 缺失的降级（只告警一次）、保持池复用轮的重绑（行为级跑 run_agent kept 路径）、
  四处收尾写入点均带 `turn_step=''`。
- D17：签名面回归（`activity_sink` 为末位关键字参数且默认 None，既有位置参数顺序未变）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_session_activity -v
"""
import inspect
import json
import sys
import tempfile
import threading
import time
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
def _stub(activity_sink=True):
    """只备 `_on_update` / `_activity` 所需状态（等价 __init__ 的 #421 初始化）。
    activity_sink=True 时装收集器（记录 calls）；False → None（board 建卡会话场景）。"""
    s = ac.ACPSession.__new__(ac.ACPSession)
    s.req_id = 421
    s.session_id = "session_421"
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
    s.events = []
    s.calls = []
    s._emit = lambda kind, content, session_id=None: s.events.append((kind, content))
    s.activity_sink = (lambda kind, detail=None: s.calls.append((kind, detail))) if activity_sink else None
    return s


def _kinds(s):
    return [k for k, _detail in s.calls]


def _chunk(text="输出"):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def _thought(text="推理"):
    return {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}


def _tool_call(tid="t1", title="Running: ls -la"):
    return {"sessionUpdate": "tool_call", "toolCallId": tid, "title": title}


def _tool_raw(tid="t1", command="ls -la"):
    """kimi 实测形态：tool_call_update 的 rawInput 分块（无 status）"""
    return {"sessionUpdate": "tool_call_update", "toolCallId": tid, "rawInput": {"command": command}}


def _tool_done(tid="t1", status="completed"):
    return {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": status}


def _age_out(s, kind, seconds=None):
    """把某档的节流时刻拨回过去（等价「静默超过节流窗口」）"""
    s._out_sent_mono[kind] = time.monotonic() - (ac.ACTIVITY_THROTTLE_SEC + 1
                                                 if seconds is None else seconds)


class L0UpdateSequences(unittest.TestCase):
    """D1-D10：`_on_update` 的状态切换序列（响应 / 工具 / 思考三档）"""

    def test_d1_transition_sequence(self):
        """D1：chunk×2（60s 内只报一次响应）→ tool_call（工具起）→ completed（工具终）"""
        s = _stub()
        s._resp_pending = True   # 等价 prompt() 起点
        s._on_update(_chunk("第一段"))
        s._on_update(_chunk("第二段"))
        s._on_update(_tool_call())
        s._on_update(_tool_done())
        self.assertEqual(s.calls, [("llm_response", None),
                                   ("tool_start", "Running: ls -la"),
                                   ("tool_end", None)])
        self.assertEqual(s._tool_open, set())

    def test_d2_tool_end_not_lost_after_dedupe_gate(self):
        """D2：真实 kimi 序列（rawInput 分块先行 → 打标 _tool_posted → completed）→ tool_end 仍上报
        （上报点在 _tool_posted 去重早退闸之前，否则 completed 永远落在闸后）"""
        s = _stub()
        s._on_update(_tool_call())
        s._on_update(_tool_raw())            # rawInput 到达：打标去重
        self.assertIn("t1", s._tool_posted)
        s._on_update(_tool_done())           # completed 在去重闸之后（原实现此处 return）
        self.assertEqual(_kinds(s), ["tool_start", "tool_end"])
        self.assertEqual(s._tool_open, set())

    def test_d3_concurrent_tools_one_end(self):
        """D3：并发工具——集合由空转非空才报 tool_start；第一个 completed 不上报 tool_end
        （集合未空），最后一个 completed 才报"""
        s = _stub()
        s._on_update(_tool_call("t1", "Running: ls"))
        s._on_update(_tool_call("t2", "Running: git status"))
        self.assertEqual(_kinds(s), ["tool_start"], "已在工具阶段，第二个工具起不再重复报")
        self.assertEqual(s._tool_open, {"t1", "t2"})
        s._on_update(_tool_done("t1"))
        self.assertEqual(_kinds(s), ["tool_start"], "仍有在途工具，不得报 tool_end")
        self.assertEqual(s._tool_open, {"t2"})
        s._on_update(_tool_done("t2"))
        self.assertEqual(_kinds(s), ["tool_start", "tool_end"])

    def test_d4_rearm_after_tool_end(self):
        """D4：tool_end 后重新武装——工具结果回传后的首个输出再无条件上报 llm_response"""
        s = _stub()
        s._on_update(_tool_call())
        s._on_update(_tool_done())
        s._on_update(_chunk("工具跑完了，继续"))
        self.assertEqual(_kinds(s), ["tool_start", "tool_end", "llm_response"])

    def test_d5_missing_completed_fallback(self):
        """D5：completed 事件缺失（tool 态直接收到 message chunk）→ 隐式 tool_end + 本轮响应上报"""
        s = _stub()
        s._on_update(_tool_call())
        self.assertEqual(s._tool_open, {"t1"})
        s._on_update(_chunk("工具阶段的文字输出"))
        self.assertEqual(_kinds(s), ["tool_start", "tool_end", "llm_response"])
        self.assertEqual(s._tool_open, set())
        self.assertFalse(s._resp_pending)

    def test_d6_thought_semantics_and_throttle(self):
        """D6：思考 chunk 首个必报、60s 内连续只报一次、窗口过后再报一次"""
        s = _stub()
        for _ in range(3):
            s._on_update(_thought())
        self.assertEqual(s.calls, [("llm_thought", None)])
        self.assertTrue(s._thought_seen_logged, "首次观测留一次日志（实测证据）")
        _age_out(s, "llm_thought")
        s._on_update(_thought())
        self.assertEqual(_kinds(s), ["llm_thought", "llm_thought"])

    def test_d7_thought_does_not_close_response_gate(self):
        """D7：思考不关闭响应闸门——「thought → message」后 message 仍无条件上报；60s 内不重复"""
        s = _stub()
        s._resp_pending = True   # 等价 prompt() 起点
        s._on_update(_thought())
        self.assertTrue(s._resp_pending, "思考不动响应闸门（否则响应档不可达）")
        s._on_update(_chunk("思考后的回答"))
        self.assertEqual(_kinds(s), ["llm_thought", "llm_response"])
        s._on_update(_chunk("继续说"))
        self.assertEqual(_kinds(s), ["llm_thought", "llm_response"], "60s 内不重复上报响应")

    def test_d8_long_output_throttle_refresh(self):
        """D8：长输出（节流窗口已过）再报一次 —— 输出期间 age 不无界增长"""
        s = _stub()
        s._on_update(_chunk("第一段"))
        self.assertEqual(_kinds(s), ["llm_response"])
        _age_out(s, "llm_response")
        s._on_update(_chunk("第二段"))
        self.assertEqual(_kinds(s), ["llm_response", "llm_response"])

    def test_d9_thought_writes_no_event_stream(self):
        """D9：思考 chunk 不写事件流、不动消息缓冲（只刷 last_thought_at）"""
        s = _stub()
        s._on_update(_thought("一大段推理"))
        self.assertEqual(s.events, [], "思考不进事件流（前端执行过程面板不展示推理）")
        self.assertEqual(s._msg_buf, [], "思考不混入 message 缓冲")
        self.assertEqual(s._tool_posted, set())
        self.assertEqual(_kinds(s), ["llm_thought"])

    def test_d10_unknown_kind_no_crash(self):
        """D10：未知 sessionUpdate 类型不崩、不上报，每类只记一次（实测核对用）"""
        s = _stub()
        s._on_update({"sessionUpdate": "plan", "entries": []})
        s._on_update({"sessionUpdate": "plan", "entries": []})
        s._on_update({})                      # 连 sessionUpdate 都没有
        self.assertEqual(s.calls, [])
        self.assertEqual(s._unknown_updates_logged, {"plan", None})


class L0PromptLifecycle(unittest.TestCase):
    """D11-D12：prompt() 的起点/收尾语义与 sink 降级"""

    def test_d11_prompt_arms_and_finally_resets(self):
        """D11：prompt() 起点报 llm_request（不走节流）+ 武装闸门；finally 清在途集合/闸门/节流表；
        collect_usage() 直接走 request()（/usage 斜杠命令）→ 不产生心跳"""
        s = _stub()
        s._tool_open = {"t1"}
        s._out_sent_mono = {"llm_response": time.monotonic(), "llm_thought": time.monotonic()}
        sent = []
        s.request = lambda method, params, timeout=60, guard=False: sent.append((method, params)) or {}
        out = s.prompt("干活", timeout=30)
        self.assertEqual(out, {})
        self.assertEqual(sent[0][0], "session/prompt")
        self.assertEqual(s.calls, [("llm_request", None)], "llm_request 属状态切换，不被 60s 节流吃掉")
        self.assertEqual(s._tool_open, set(), "回合结束清在途工具集合")
        self.assertFalse(s._resp_pending)
        self.assertEqual(s._out_sent_mono, {}, "回合结束清节流表（下一轮首个输出无条件上报）")
        s.calls.clear()
        self.assertIsNone(s.collect_usage(timeout=5))
        self.assertEqual(s.calls, [], "collect_usage 不经 prompt()，不触发活性上报")

    def test_d12_sink_none_or_raising_is_silent(self):
        """D12：sink None（board 建卡/需求助手会话）零行为；sink 抛异常静默降级，不阻断事件流"""
        s = _stub(activity_sink=False)
        s._on_update(_chunk("文字"))
        self.assertEqual(s._msg_buf, ["文字"], "零行为：消息缓冲照旧（仅活性上报缺席）")
        for update in (_thought(), _tool_call(), _tool_done()):
            s._on_update(update)          # 不抛异常即通过
        self.assertEqual(s.calls, [])
        self.assertEqual([k for k, _c in s.events], ["message", "tool"],
                         "事件流照旧（tool_call 前 flush 文字；completed 补工具摘要）")

        s2 = _stub()
        attempted = []
        def boom(kind, detail=None):
            attempted.append(kind)
            raise RuntimeError("sink down")
        s2.activity_sink = boom
        s2._on_update(_chunk("文字"))
        s2._on_update(_tool_call())
        s2._on_update(_thought())
        self.assertEqual(attempted, ["llm_response", "tool_start", "llm_thought"],
                         "sink 异常被 _activity 吞掉，后续状态切换照常推进")


class L0TouchSessionActivity(unittest.TestCase):
    """D13-D14：dispatcher 侧 kind→字段映射 / row_id 缺失降级"""

    _MUTATED = ("api_board", "log")

    def setUp(self):
        self._orig = {n: getattr(d, n) for n in self._MUTATED}
        d.log = mock.Mock()
        self.posts = []
        d.api_board = lambda method, path, body=None: self.posts.append((method, path, body)) or {}

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)

    def _holder(self, row_id=501):
        return {"row_id": row_id, "warned": False}

    def test_d13_kind_to_fields_mapping(self):
        """D13：五种 kind 的字段映射（turn_step + 时间戳；tool_start 带 detail 且压成单行截 48）"""
        holder = self._holder()
        ts = 1700000000.25
        with mock.patch.object(d.time, "time", return_value=ts):
            d._touch_session_activity(421, "dev", holder, "llm_request")
            d._touch_session_activity(421, "dev", holder, "llm_thought")
            d._touch_session_activity(421, "dev", holder, "llm_response")
            d._touch_session_activity(421, "dev", holder, "tool_start", "Running:   ls -la")
            d._touch_session_activity(421, "dev", holder, "tool_end")
            d._touch_session_activity(421, "dev", holder, "tool_start", "x" * 80 + "\n换行")
            d._touch_session_activity(421, "dev", holder, "没见过的kind")
        bodies = [b for method, path, b in self.posts]
        self.assertEqual(bodies[:6], [
            {"id": 501, "turn_step": "llm", "last_llm_request_at": ts},
            {"id": 501, "last_thought_at": ts},
            {"id": 501, "last_llm_response_at": ts},
            {"id": 501, "turn_step": "tool", "last_tool_started_at": ts,
             "turn_step_detail": "Running: ls -la"},
            # 在途工具清空 = 下一次模型请求的近似起点（ACP 无原生通知）
            {"id": 501, "turn_step": "llm", "last_llm_request_at": ts},
            {"id": 501, "turn_step": "tool", "last_tool_started_at": ts,
             "turn_step_detail": "x" * 48},
        ])
        self.assertEqual(len(bodies), 6, "未知 kind 不写")
        self.assertEqual([path for _m, path, _b in self.posts], ["/agent-sessions"] * 6)

    def test_d13_heartbeat_failure_only_warns(self):
        """D13（降级）：看板写入失败仅告警，不抛（不得阻断会话）"""
        def boom(method, path, body=None):
            raise RuntimeError("board down")
        d.api_board = boom
        d._touch_session_activity(421, "dev", self._holder(), "llm_request")  # 不抛即通过
        self.assertEqual(d.log.warning.call_count, 1)

    def test_d14_no_row_id_skips_and_warns_once(self):
        """D14：注册表行 id 为 None（sync 写失败）→ 不发 HTTP、告警只出现一次（防每轮刷 400 噪声）"""
        holder = self._holder(row_id=None)
        for _ in range(4):
            d._touch_session_activity(421, "dev", holder, "llm_request")
        self.assertEqual(self.posts, [])
        self.assertEqual(d.log.warning.call_count, 1)
        d._touch_session_activity(421, "dev", None, "llm_request")   # holder 为 None 也不崩
        self.assertEqual(self.posts, [])


class _FakeSession:
    """ACPSession 替身（保持池复用轮）：proc 支持 poll，activity_sink 可被重绑"""

    def __init__(self, sid="session_kept"):
        self.session_id = sid
        self.model_name = "test-model"
        self.thinking = "low"
        self.proc = mock.Mock(pid=4242, returncode=0)
        self.activity_sink = None
        self.output_guard = None
        self.prompts = []
        self.closed = False

    def start(self, workdir, resume_session_id=None):
        return False

    def prompt(self, text, timeout):
        self.prompts.append(text)
        return {}

    def collect_usage(self):
        return None

    def cancel(self):
        pass

    def close(self, kill=False):
        self.closed = True

    def rearm_output_guard(self, sink):
        self.output_guard = sink

    def _flush_message(self):
        pass


class L0DispatcherWiring(unittest.TestCase):
    """D15 + D16(1)：run_agent（保持池复用轮）——sink 重绑到本轮 holder、收尾写 turn_step=''

    行为级跑 run_agent 的 kept 路径（不构造 ACPSession、不起进程）：mock 掉全部副作用
    （post_event/sync_agent_session/handle_result/update_session_info/_hold_session/save_state），
    不写 data/dispatcher_state.json（#420 隔离）。"""

    _MUTATED = ("state", "api_board", "active_runs", "role_allowed", "artifact_dir", "log",
                "dev_workdir_for", "post_event", "post_run_record", "update_session_info",
                "kimi_version", "dev_branch", "owner_key", "sync_agent_session",
                "handle_result", "result_state", "requirement_timeout", "append_kill_redline",
                "_hold_session", "save_state")

    def setUp(self):
        self._orig = {n: getattr(d, n) for n in self._MUTATED}
        self.posts = []
        self.syncs = []
        self.next_row_id = 777
        d.state = {"sessions": {}, "held_procs": {}, "retries": {}, "pending_confirm": {},
                   "worktrees": {}, "req_todos": {}}
        d.active_runs = {}
        d.log = mock.Mock()
        d.api_board = lambda method, path, body=None: self.posts.append((method, path, body)) or {}
        d.save_state = lambda: None
        d.role_allowed = lambda req, role: True
        d.artifact_dir = lambda req: Path(tempfile.gettempdir())
        d.dev_workdir_for = lambda req: Path(tempfile.gettempdir())
        d.post_event = lambda *a, **k: None
        d.post_run_record = lambda *a, **k: None
        d.update_session_info = lambda *a, **k: None
        d.kimi_version = lambda: None
        d.dev_branch = lambda req: "f-test"
        d.owner_key = lambda req: "owner"
        d.handle_result = lambda *a, **k: None
        d.result_state = lambda req: ("ok", "")
        d.requirement_timeout = lambda req: 30
        d.append_kill_redline = lambda prompt, pid: prompt
        d._hold_session = lambda *a, **k: None

        def fake_sync(req, role, session_id=None, row_id=None, **fields):
            self.syncs.append({"req": req, "role": role, "session_id": session_id,
                               "row_id": row_id, "fields": fields})
            return self.next_row_id
        d.sync_agent_session = fake_sync
        self.logf = tempfile.NamedTemporaryFile(mode="w", prefix="req421-", delete=False)
        self.addCleanup(lambda: Path(self.logf.name).unlink(missing_ok=True))

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)

    def _req(self):
        return {"id": 421, "project": "req-board", "title": "t", "mode": "semi", "phase": "accept"}

    def _heartbeats(self):
        return [b for _m, path, b in self.posts if path == "/agent-sessions"]

    def test_d15_kept_session_sink_rebound(self):
        """D15：复用保持池会话（不构造 ACPSession）→ run_agent 必须把 activity_sink 重绑到
        本轮 holder：心跳用**本轮 sync 返回的**行 id 写，不沿用上一轮旧 holder（旧行已 idle）"""
        old_calls = []
        fake = _FakeSession()
        fake.activity_sink = lambda kind, detail=None: old_calls.append(kind)   # 上一轮残留 sink
        held = {"session": fake, "logf": self.logf, "req_id": "421", "role": "accept",
                "kept_at": time.time()}
        d.run_agent(self._req(), "accept", "继续", started_at=time.time(), user_text="继续",
                    hold=True, held=held, agent_session_id=25)
        self.assertTrue(fake.prompts, "kept 路径应直接 prompt")
        self.assertIsNot(fake.activity_sink, None)
        fake.activity_sink("llm_request")
        self.assertEqual(old_calls, [], "旧 sink 已被替换（不得再写上一轮 holder）")
        self.assertEqual(self._heartbeats()[-1], {"id": 777, "turn_step": "llm",
                                                  "last_llm_request_at": mock.ANY})

    def test_d16_run_agent_cleanup_clears_turn_step(self):
        """D16(1)：run_agent 收尾写入点（finally）带 status=idle + turn_step=''"""
        fake = _FakeSession()
        held = {"session": fake, "logf": self.logf, "req_id": "421", "role": "accept",
                "kept_at": time.time()}
        d.run_agent(self._req(), "accept", "继续", started_at=time.time(), user_text="继续",
                    hold=True, held=held, agent_session_id=25)
        last = self.syncs[-1]
        self.assertEqual(last["row_id"], 777)
        self.assertEqual(last["session_id"], "session_kept")
        self.assertEqual(last["fields"]["status"], "idle")
        self.assertEqual(last["fields"]["turn_step"], "")
        self.assertEqual(last["fields"]["branch"], "f-test")


class L0CloseSessionRowSites(unittest.TestCase):
    """D16(2)(3)(4)：另外三处收尾写入点（_mark_running_rows_idle / reconcile_orphan_sessions /
    _finish_dead_session）同样经 _close_session_row 带上 turn_step=''"""

    _MUTATED = ("api_board", "state", "active_runs", "log", "post_dispatcher_event",
                "post_run_record", "handle_result")

    def setUp(self):
        self._orig = {n: getattr(d, n) for n in self._MUTATED}
        self.posts = []
        self.rows = []
        d.log = mock.Mock()
        d.state = {"sessions": {}}
        d.active_runs = {}
        d.post_dispatcher_event = lambda *a, **k: None
        d.post_run_record = lambda *a, **k: None
        d.handle_result = lambda *a, **k: None

        def fake_board(method, path, body=None):
            if method == "GET":
                return self.rows
            self.posts.append((method, path, body))
            return {}
        d.api_board = fake_board

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)

    def test_d16_mark_running_rows_idle(self):
        """D16(2)：#388 采纳后收尾——只动本需求本角色的 running 行，且清 turn_step"""
        self.rows = [{"id": 901, "req_id": 421, "role": "dev", "status": "running",
                      "session_id": "s"},
                     {"id": 902, "req_id": 421, "role": "dev", "status": "idle"},
                     {"id": 903, "req_id": 421, "role": "analysis", "status": "running"}]
        d._mark_running_rows_idle({"id": 421, "project": "req-board"}, "dev")
        self.assertEqual(self.posts, [("POST", "/agent-sessions",
                                       {"req_id": 421, "role": "dev", "id": 901,
                                        "status": "idle", "turn_step": ""})])

    def test_d16_reconcile_orphan_sessions(self):
        """D16(3)：启动孤儿清算——running 无持有者行标 interrupted 且清 turn_step"""
        self.rows = [{"id": 205, "req_id": 370, "role": "dev", "status": "running",
                      "session_id": "session_dead"}]
        d.reconcile_orphan_sessions()
        self.assertEqual(self.posts, [("POST", "/agent-sessions",
                                       {"req_id": 370, "role": "dev", "id": 205,
                                        "status": "interrupted", "turn_step": ""})])

    def test_d16_finish_dead_session(self):
        """D16(4)：进程死后兜底收尾——行置 idle（带 session_id）且清 turn_step"""
        row = {"id": 901, "session_id": "session_dead"}
        rec = {"proc": mock.Mock(returncode=1), "started_at": time.time(), "session": object()}
        d._finish_dead_session(row, {"id": 421, "project": "req-board"}, "dev", rec)
        self.assertEqual(self.posts, [("POST", "/agent-sessions",
                                       {"req_id": 421, "role": "dev", "id": 901,
                                        "session_id": "session_dead",
                                        "status": "idle", "turn_step": ""})])


class L0SignatureSurface(unittest.TestCase):
    """D17：ACPSession.__init__ 签名面——既有位置参数顺序不得变（board 建卡会话/需求助手
    按位置传参，顺序变了会静默错配），新增参数只允许在既有位置参数之后追加且默认 None。
    #9 在 model 后新增 thinking（#50 已删除，思考档位统一模型默认）；
    #421 追加 activity_sink（会话活性上报）；#24 追加 tool_profile（助手 ACP 构造档）；
    #28 追加 output_guard（输出守卫留痕回调），本表随之更新。"""

    EXISTING_POSITIONAL = [
        "req_id", "workdir", "logf", "role", "artifact_root", "project", "event_sink",
        "readonly", "extra_allow_prefixes", "extra_mcp_servers", "allow_fs_write",
        "readonly_roots", "write_roots", "kimi_bin", "deny_builtin_write", "mysql_config",
        "allow_git_commit", "allow_git_merge", "main_ws_roots", "model",
        "allow_python_safe", "python_roots", "python_compile_roots",
    ]
    # 既有位置参数之后追加的关键字参数（顺序即追加顺序，默认必须为 None）
    APPENDED_KEYWORDS = ["activity_sink", "tool_profile", "output_guard"]

    def test_d17_activity_sink_appended_last(self):
        params = list(inspect.signature(ac.ACPSession.__init__).parameters.values())
        names = [p.name for p in params]
        self.assertEqual(names[0], "self")
        self.assertEqual(names[1:1 + len(self.EXISTING_POSITIONAL)], self.EXISTING_POSITIONAL,
                         "既有位置参数顺序被改动（只允许在末尾追加）")
        self.assertEqual(names[1 + len(self.EXISTING_POSITIONAL):], self.APPENDED_KEYWORDS,
                         "追加参数清单与顺序被改动（只允许追加且默认 None）")
        for p in params[1 + len(self.EXISTING_POSITIONAL):]:
            self.assertIsNone(p.default, f"{p.name} 默认 None（不传时既有行为逐字不变）")

    def test_d17_activity_throttle_constant(self):
        """节流常量存在且为 60s（llm_response / llm_thought 两档）"""
        self.assertEqual(ac.ACTIVITY_THROTTLE_SEC, 60)


if __name__ == "__main__":
    unittest.main()
