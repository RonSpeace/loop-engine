#!/usr/bin/env python3
"""#24 飞书看板助手 —— dispatcher 侧消息优先级矩阵与助手编排（L0，全 mock，不触网、不起会话）。

覆盖用例：
- TC-01/02/03 首条消息问候一次，且问候后继续处理本条（y/n 与普通文本）
- TC-04/05 素材分支优先于 y/n；非 text/file/image 忽略；群聊 @ 文本进看板助手（不写 state["chat_id"]）
- TC-06 消息幂等（去重键 = feishu_chat_id + message_id，窗口内重复只处理一次）
- TC-07/08/64 同 chat 串行、不同 chat 线程与会话状态互不串线
- TC-25/28/50 助手轮次：90s deadline（board 权威计时）→ cancel + 固定文案一次、保留 session_id、
  kind=message 才回发（kind=agent 不回发）、晚到输出不再回发
- TC-27/46 助手链路失败（建会话/提交/事件接口）→ 明确降级回复，不静默
- TC-48 提交撞 409（上一轮收尾中）→ 按状态栅栏等到可续跑再试，不被永久挡住
- TC-30/35 assistant_model：未配置/非法/可用三态与真实传入；app_secret 不进状态与日志
- TC-62 启动对账：僵尸 running/cancelling 取消并保留 session_id；closed/failed 清本地条目
- TC-66/67 无活跃旧会话：只有整行「新建建卡」进旧链路；「结束建卡」「确认建卡」只回提示；
  旧 `_feishu_route_text` 默认非指令启动分支已删除（普通文本一律进助手）

隔离：mock 看板 HTTP（不真连 8899）、mock 飞书发送、不起真实 session、不建 worktree。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_feishu_assistant_router -v
"""
import base64
import io
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_REAL_ENQUEUE = d._feishu_enqueue
CHAT = "oc_assist"
CHAT_B = "oc_assist_b"
_MSG_SEQ = [0]


def up(text, chat_id=CHAT, msg_type="text", message_id=None, chat_type="p2p", content=None):
    if message_id is None:
        _MSG_SEQ[0] += 1
        message_id = f"om_a{_MSG_SEQ[0]}"
    if content is None:
        content = json.dumps({"text": text}) if msg_type == "text" else json.dumps(content or {})
    elif not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return types.SimpleNamespace(event=types.SimpleNamespace(message=types.SimpleNamespace(
        message_type=msg_type, content=content, chat_id=chat_id,
        message_id=message_id, chat_type=chat_type)))


def http_error(code, payload):
    return urllib.error.HTTPError("http://x", code, "err", None,
                                  io.BytesIO(json.dumps(payload).encode()))


def fresh_state(chat_id=CHAT):
    return {"chat_id": chat_id, "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}, "feishu_chats": {},
            "feishu_greeted": {}, "feishu_assistant": {}, "feishu_seen": {}}


def active_sess(session_id=7, **kw):
    s = {"project": "loop-engine", "session_id": session_id, "last_event_id": 0,
         "pending": {"text": "", "materials": []}, "awaiting_project": False,
         "replay": [], "updated_at": time.time()}
    s.update(kw)
    return s


class AssistantBoard:
    """伪看板助手 API（/api/assistant/*）：记录调用 + 可编排状态序列 / 事件 / 错误。"""

    def __init__(self, sid=11, statuses=None, default_status="idle", turn_id="turn-1",
                 events=None, create_error=None, prompt_error=None, cancel_error=None,
                 row_extra=None, awaiting_project=False):
        self.calls = []
        self.sid = sid
        self.statuses = list(statuses or [])
        self.default_status = default_status
        self.turn_id = turn_id
        self.events = list(events or [])
        self.create_error = create_error
        self.prompt_error = prompt_error
        self.cancel_error = cancel_error
        self.row_extra = dict(row_extra or {})
        self.awaiting_project = awaiting_project
        self.cancels = []
        self.prompts = []

    def _status(self):
        return self.statuses.pop(0) if self.statuses else self.default_status

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        base = path.split("?", 1)[0]
        if method == "POST" and base == "/assistant/sessions":
            if self.create_error:
                raise self.create_error
            return {"id": self.sid, "feishu_chat_id": (body or {}).get("feishu_chat_id"),
                    "status": "idle", "session_id": "acp-1", "turn_id": None,
                    "model": (body or {}).get("model")}
        m = re.fullmatch(r"/assistant/sessions/(\d+)", base)
        if method == "GET" and m:
            row = {"id": int(m.group(1)), "status": self._status(),
                   "awaiting_project": False, "turn_id": self.turn_id}
            row.update(self.row_extra)
            return row
        m = re.fullmatch(r"/assistant/sessions/(\d+)/prompt", base)
        if method == "POST" and m:
            self.prompts.append(body)
            if self.prompt_error:
                err = self.prompt_error
                self.prompt_error = None
                raise err
            return {"id": int(m.group(1)), "turn_id": self.turn_id, "status": "running"}
        m = re.fullmatch(r"/assistant/sessions/(\d+)/events", base)
        if method == "GET" and m:
            after = int(re.search(r"after=(\d+)", path).group(1))
            return {"session": {"id": int(m.group(1)), "status": self.default_status,
                                "turn_id": self.turn_id,
                                "awaiting_project": self.awaiting_project},
                    "events": [e for e in self.events if e.get("id", 0) > after]}
        m = re.fullmatch(r"/assistant/sessions/(\d+)/cancel", base)
        if method == "POST" and m:
            if self.cancel_error:
                raise self.cancel_error
            self.cancels.append(body)
            return {"ok": True}
        m = re.fullmatch(r"/assistant/sessions/(\d+)/close", base)
        if method == "POST" and m:
            return {"ok": True}
        raise AssertionError(f"未预期的看板调用: {method} {path}")


class AssistantBase(unittest.TestCase):
    _RESTORE = ("state", "save_state", "notify", "_feishu_enqueue", "_feishu_handle",
                "send_text_chunked", "api_board", "PROJECTS", "feishu", "FEISHU_ASSISTANT_MODEL",
                "FEISHU_ASSISTANT_MODEL_REASON", "FEISHU_ASSISTANT_DEADLINE_SEC",
                "FEISHU_CONFIG_FILE", "fetch_req", "post_dispatcher_event", "append_summary",
                "set_status", "spawn_agent", "_feishu_new_sess", "_feishu_download_resource")

    def setUp(self):
        self._orig = {n: getattr(d, n) for n in self._RESTORE}
        self._orig_queues = d._feishu_queues
        self._orig_workers = d._feishu_workers
        d.state = fresh_state()
        d.save_state = lambda: None
        self.notifies = []
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        self.enqueued = []
        d._feishu_enqueue = lambda cid, payload: self.enqueued.append((cid, payload))
        self.sent = []
        d.send_text_chunked = lambda cid, text, *a, **k: self.sent.append((cid, text))
        d.PROJECTS = {"loop-engine": {"paths": ["/Users/x/Loop Engine/Main Repo"]}}
        d.feishu = mock.Mock()
        self.board = AssistantBoard()
        d.api_board = self.board
        d.FEISHU_ASSISTANT_MODEL = None
        d.FEISHU_ASSISTANT_MODEL_REASON = None
        self.spawns = []
        d.fetch_req = lambda rid: {"id": rid, "project": "loop-engine", "mode": "auto"}
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.set_status = lambda *a, **k: None
        d.spawn_agent = lambda req, extra="", resume=False: self.spawns.append((req["id"], resume))

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(d, n, v)
        d._feishu_queues = self._orig_queues
        d._feishu_workers = self._orig_workers
        d._feishu_queues.clear()
        d._feishu_workers.clear()

    def handle(self, payload, chat_id=CHAT):
        d._feishu_handle(chat_id, payload)

    def kinds(self):
        return [p.get("kind") for _, p in self.enqueued]

    def joined_notifies(self):
        return "\n".join(self.notifies)


# ==================== 优先级矩阵（TC-04/05/66/67/70） ====================

class PriorityMatrix(AssistantBase):
    def test_material_beats_yn_and_assistant(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        d.state["pending_confirm"]["9"] = {"req": None, "question": "Q?"}
        msg = up("", msg_type="image", content={"image_key": "img_1"})
        d.on_message(msg)
        self.assertEqual(self.kinds(), ["material"])          # 素材分支最先
        self.assertEqual(self.spawns, [])                      # y/n 未被消费
        self.assertTrue(d._feishu_ext_ok(self.enqueued[0][1]["handle"]["name"]))

    def test_yn_beats_active_session_and_assistant(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        d.state["pending_confirm"]["9"] = {"req": None, "question": "Q?"}
        d.on_message(up("y 9"))
        self.assertEqual(self.enqueued, [])                    # 不进建卡也进不了助手
        self.assertEqual(self.spawns, [(9, True)])

    def test_active_legacy_session_routes_legacy_text(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        msg = up("继续聊这个")
        d.on_message(msg)
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "legacy_text", "text": "继续聊这个",
                                                "message_id": msg.event.message.message_id})])

    def test_new_build_command_without_session_enters_legacy_new(self):
        d.on_message(up("新建建卡"))
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "new", "cmd": "新建建卡"})])

    def test_end_without_session_only_hints(self):
        d.on_message(up("结束建卡。"))
        self.assertEqual(self.enqueued, [])
        self.assertEqual(d.state["feishu_chats"], {})
        self.assertIn("没有进行中的建卡会话", self.joined_notifies())

    def test_confirm_without_session_only_hints(self):
        d.on_message(up(" 确认建卡 "))
        self.assertEqual(self.enqueued, [])
        self.assertIn("还没有草稿", self.joined_notifies())

    def test_plain_text_without_session_goes_assistant(self):
        msg = up("哪个项目在跑")
        d.on_message(msg)
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": "哪个项目在跑",
                                                "message_id": msg.event.message.message_id})])

    def test_link_plus_build_task_goes_assistant(self):
        text = "https://example.com/a 建个任务"
        msg = up(text)
        d.on_message(msg)
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": text,
                                                "message_id": msg.event.message.message_id})])

    def test_group_text_routes_assistant_and_sticker_ignored_without_state_write(self):
        # #22 行为变化 2：群聊 @ 文本进看板助手，但不写 state["chat_id"]、不问候
        d.state = fresh_state(chat_id="oc_old")
        msg = up("群消息", chat_type="group")
        d.on_message(msg)
        d.on_message(up("", msg_type="sticker", content={}))
        self.assertEqual(d.state["chat_id"], "oc_old")
        self.assertEqual(self.kinds(), ["assistant"])
        self.assertEqual(self.notifies, [])
        # 群文本进幂等窗口（只记群 chat_id 这一条，sticker 不记）
        self.assertEqual(list(d.state["feishu_seen"]), [CHAT])

    def test_group_message_consumes_seen(self):
        # 群聊消息也进幂等窗口（同 chat + 同 message_id 的后续投递被丢弃），避免重复回复
        d.on_message(up("群消息", chat_type="group", message_id="om_shared"))
        d.on_message(up("私聊消息", message_id="om_shared"))
        self.assertEqual(self.kinds(), ["assistant"])
        self.assertEqual(self.enqueued[0][1]["text"], "群消息")


# ==================== 首条消息问候（TC-01/02/03） ====================

class FirstMessageGreeting(AssistantBase):
    def test_first_text_greets_then_routes_same_message(self):
        d.state = fresh_state(chat_id=None)
        msg = up("哪个项目在跑")
        d.on_message(msg)
        self.assertIn("已连接", self.notifies[0])
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": "哪个项目在跑",
                                                "message_id": msg.event.message.message_id})])
        self.notifies.clear()
        d.on_message(up("还有呢"))
        self.assertNotIn("已连接", self.joined_notifies())      # 同 chat 只问候一次

    def test_first_yn_with_pending_proceeds(self):
        d.state = fresh_state(chat_id=None)
        d.state["pending_confirm"]["9"] = {"req": None, "question": "Q?"}
        d.on_message(up("y 9"))
        self.assertIn("已连接", self.notifies[0])
        self.assertEqual(self.spawns, [(9, True)])

    def test_first_yn_without_pending_hints_no_session(self):
        d.state = fresh_state(chat_id=None)
        d.on_message(up("n 9"))
        self.assertIn("已连接", self.notifies[0])
        self.assertIn("没有待确认的问题", self.joined_notifies())
        self.assertEqual(d.state["feishu_assistant"], {})       # 不创建助手会话
        self.assertEqual(self.enqueued, [])

    def test_first_material_does_not_greet(self):
        # 素材分支逐字保持现状：首条素材走素材分支且不问候（下一次文本消息仍会问候）
        d.state = fresh_state(chat_id=None)
        d.on_message(up("", msg_type="image", content={"image_key": "img_1"}))
        self.assertEqual(self.kinds(), ["material"])
        self.assertEqual(self.notifies, [])


# ==================== 消息幂等（TC-06） ====================

class MessageDedup(AssistantBase):
    def test_same_message_id_processed_once(self):
        msg = up("哪个项目在跑", message_id="om_dup")
        d.on_message(msg)
        d.on_message(msg)
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": "哪个项目在跑",
                                                "message_id": "om_dup"})])

    def test_same_id_different_chat_is_not_duplicate(self):
        d.on_message(up("A 的话", chat_id=CHAT, message_id="om_shared"))
        d.on_message(up("B 的话", chat_id=CHAT_B, message_id="om_shared"))
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": "A 的话",
                                                "message_id": "om_shared"}),
                                         (CHAT_B, {"kind": "assistant", "text": "B 的话",
                                                   "message_id": "om_shared"})])

    def test_dedup_covers_yn_and_material(self):
        d.state["pending_confirm"]["9"] = {"req": None, "question": "Q?"}
        d.on_message(up("y 9", message_id="om_yn"))
        d.on_message(up("y 9", message_id="om_yn"))
        self.assertEqual(self.spawns, [(9, True)])              # y/n 只消费一次
        d.on_message(up("", msg_type="image", message_id="om_img",
                        content={"image_key": "img_1"}))
        d.on_message(up("", msg_type="image", message_id="om_img",
                        content={"image_key": "img_1"}))
        self.assertEqual(self.kinds(), ["material"])

    def test_window_is_bounded(self):
        with mock.patch.object(d, "FEISHU_ASSISTANT_SEEN_WINDOW", 3):
            for i in range(6):
                d.on_message(up(f"m{i}", message_id=f"om_{i}"))
        self.assertLessEqual(len(d.state["feishu_seen"][CHAT]), 3)

    def test_missing_message_id_not_deduped(self):
        msg = up("无 id 消息")
        msg.event.message.message_id = None
        d.on_message(msg)
        d.on_message(msg)
        self.assertEqual(len(self.enqueued), 2)


# ==================== 不同 chat 串行 / 隔离（TC-07/08/64） ====================

class ChatIsolation(AssistantBase):
    def test_distinct_workers_per_chat(self):
        seen = []
        d._feishu_handle = lambda cid, payload: seen.append(
            (cid, threading.current_thread().name))
        d._feishu_enqueue = _REAL_ENQUEUE
        d._feishu_queues.clear()
        d._feishu_workers.clear()
        d.on_message(up("A 的话", chat_id=CHAT))
        d.on_message(up("B 的话", chat_id=CHAT_B))
        for _ in range(200):
            if len(seen) >= 2:
                break
            time.sleep(0.01)
        self.assertEqual({c for c, _ in seen}, {CHAT, CHAT_B})
        threads = {t for _, t in seen}
        self.assertEqual(len(threads), 2)                       # 不同 chat 不共享 worker

    def test_chat_state_and_session_isolated(self):
        d._feishu_handle_assistant_text(CHAT, "A 的问题")
        self.board.sid = 22
        d._feishu_handle_assistant_text(CHAT_B, "B 的问题")
        creates = [c for c in self.board.calls if c[1] == "/assistant/sessions"]
        self.assertEqual([c[2]["feishu_chat_id"] for c in creates], [CHAT, CHAT_B])
        a = d.state["feishu_assistant"][CHAT]
        b = d.state["feishu_assistant"][CHAT_B]
        self.assertEqual((a["session_id"], b["session_id"]), (11, 22))
        self.assertNotEqual(a, b)
        # 本地助手状态与旧建卡状态完全分离
        self.assertEqual(d.state["feishu_chats"], {})
        # 本地状态不保存会话原文
        self.assertNotIn("A 的问题", json.dumps(d.state["feishu_assistant"], ensure_ascii=False))


# ==================== 助手轮次：正文回发 / 超时兜底（TC-25/28/50） ====================

class AssistantRound(AssistantBase):
    def test_happy_path_sends_only_message_events(self):
        self.board.statuses = ["running", "idle"]
        self.board.events = [
            {"id": 1, "kind": "message", "content": "在跑：loop-engine"},
            {"id": 2, "kind": "agent", "content": "[飞书] 机器人：哪条"},
            {"id": 3, "kind": "user", "content": "用户：哪个项目"},
            {"id": 4, "kind": "tool", "content": "req_query"},
        ]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "哪个项目在跑"})
        self.assertEqual(self.sent, [(CHAT, "在跑：loop-engine")])   # kind=agent 不回发
        entry = d.state["feishu_assistant"][CHAT]
        self.assertEqual(entry["last_event_id"], 4)
        self.assertFalse(entry["pending"])                          # 轮次收尾
        prompt = self.board.prompts[0]
        self.assertEqual(prompt["text"], "哪个项目在跑")
        self.assertGreaterEqual(prompt["remaining_sec"], 1)          # 传剩余秒数给 board

    def test_e1_card_event_dispatches_to_card_sender(self):
        """#30：kind='card' 走卡片发送（显式 chat_id），kind='message' 仍走 send_text_chunked。"""
        self.board.statuses = ["running", "idle"]
        card = {"v": 1, "type": "requirement_list", "project": "loop-engine", "page": 1,
                "pages": 2, "total_cards": 25, "truncated": False, "remaining": 0, "items": []}
        self.board.events = [
            {"id": 1, "kind": "card", "content": json.dumps(card)},
            {"id": 2, "kind": "message", "content": "已发出 25 张（2 页）"},
        ]
        cards = []
        with mock.patch.object(d, "_feishu_send_assistant_card",
                               lambda cid, content: cards.append((cid, content))), \
                mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "把卡清单发我"})
        self.assertEqual(len(cards), 1)                          # 卡片事件不回发文本
        self.assertEqual(cards[0][0], CHAT)
        self.assertEqual(json.loads(cards[0][1])["type"], "requirement_list")
        self.assertEqual(self.sent, [(CHAT, "已发出 25 张（2 页）")])   # message 分支逐字不变
        self.assertEqual(d.state["feishu_assistant"][CHAT]["last_event_id"], 2)   # 游标两 kind 都推进

    def test_e3_malformed_card_event_fails_closed(self):
        """#30：非法卡片事件 → 不抛断整轮、不静默（走真实 _feishu_send_assistant_card 的 log.error 分支）。"""
        self.board.statuses = ["running", "idle"]
        self.board.events = [{"id": 1, "kind": "card", "content": "{not json"}]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "把卡清单发我"})
        entry = d.state["feishu_assistant"][CHAT]
        self.assertEqual(entry["last_event_id"], 1)
        self.assertFalse(entry["pending"])                      # 轮次正常收尾，未被打断

    def test_timeout_cancels_and_warns_once(self):
        d.FEISHU_ASSISTANT_DEADLINE_SEC = 0                          # deadline 立即到点
        self.board.statuses = ["running"]
        self.handle({"kind": "assistant", "text": "服务还活着吗"})
        self.assertEqual(len(self.board.cancels), 1)                 # 兜底 cancel
        self.assertEqual(self.board.cancels[0]["feishu_chat_id"], CHAT)
        self.assertEqual(self.board.cancels[0]["turn_id"], "turn-1")
        self.assertEqual(self.notifies.count(d.FEISHU_ASSISTANT_TIMEOUT_TEXT), 1)
        # 保留 session_id 供下次 resume；晚到输出不再回发
        self.assertEqual(d.state["feishu_assistant"][CHAT]["session_id"], 11)
        self.assertEqual(self.sent, [])
        self.assertEqual(len([c for c in self.board.calls if "/events" in c[1]]), 1)
        # 轮次已收尾：board 晚到的助手正文不会被回发
        self.board.events.append({"id": 9, "kind": "message", "content": "晚到的正文"})
        self.assertEqual(self.sent, [])

    def test_board_timeout_message_delivered_with_terminal_status(self):
        """#24 复修：board 权威 90s 超时的固定文案必须回发飞书。board 先写 message 事件、后翻状态，
        dispatcher 读到中断态时该文案已在同拍事件里 → 必须发出（否则用户只看到状态提示、看不到文案）。"""
        self.board.statuses = ["running", "interrupted"]
        self.board.events = [{"id": 1, "kind": "message", "content": d.FEISHU_ASSISTANT_TIMEOUT_TEXT}]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "服务还活着吗"})
        self.assertIn((CHAT, d.FEISHU_ASSISTANT_TIMEOUT_TEXT), self.sent)

    def test_interrupted_keeps_session_for_resume(self):
        self.board.statuses = ["running", "interrupted"]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "在吗"})
        entry = d.state["feishu_assistant"][CHAT]
        self.assertFalse(entry["pending"])
        self.assertEqual(entry["session_id"], 11)
        self.assertIn("重发一次", self.joined_notifies())

    def test_awaiting_project_synced_from_snapshot(self):
        self.board.statuses = ["running", "idle"]
        self.board.awaiting_project = True
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "帮我建个卡"})
        self.assertTrue(d.state["feishu_assistant"][CHAT]["awaiting_project"])

    def test_pending_turn_cancelled_before_new_message(self):
        d.state["feishu_assistant"][CHAT] = {"session_id": 11, "feishu_chat_id": CHAT,
                                             "pending": True, "turn_id": "turn-old",
                                             "last_event_id": 0, "updated_at": time.time()}
        self.board.statuses = ["idle"]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "新问题"})
        self.assertEqual(len(self.board.cancels), 1)                 # 先取消上一轮
        self.assertEqual(self.board.cancels[0]["turn_id"], "turn-old")

    def test_409_fence_retries_after_resumable(self):
        self.board.prompt_error = http_error(409, {"error": "会话正在收尾"})
        self.board.statuses = ["cancelling", "interrupted", "idle"]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "不被卡住的消息"})
        self.assertEqual(len(self.board.prompts), 2)                 # 栅栏后重试成功
        self.assertEqual(self.board.prompts[1]["text"], "不被卡住的消息")
        self.assertNotIn(d.FEISHU_ASSISTANT_BUSY_HINT, self.joined_notifies())


# ==================== 失败降级（TC-27/46） ====================

class AssistantDegrade(AssistantBase):
    def test_create_http_error_not_silent(self):
        self.board.create_error = http_error(500, {"error": "boom"})
        self.handle({"kind": "assistant", "text": "哪个项目在跑"})
        self.assertIn("boom", self.joined_notifies())
        self.assertTrue(self.notifies)

    def test_board_unreachable_not_silent(self):
        d.api_board = mock.Mock(side_effect=urllib.error.URLError("connection refused"))
        self.handle({"kind": "assistant", "text": "哪个项目在跑"})
        self.assertIn(d.FEISHU_ASSISTANT_DOWN_HINT, self.joined_notifies())

    def test_unexpected_exception_degrades(self):
        d.api_board = mock.Mock(side_effect=AssertionError("未预期形态"))
        self.handle({"kind": "assistant", "text": "哪个项目在跑"})
        self.assertIn(d.FEISHU_ASSISTANT_DOWN_HINT, self.joined_notifies())

    def test_prompt_failure_not_silent(self):
        self.board.prompt_error = http_error(500, {"error": "prompt 挂了"})
        self.handle({"kind": "assistant", "text": "哪个项目在跑"})
        self.assertIn("prompt 挂了", self.joined_notifies())

    def test_missing_turn_id_degrades(self):
        self.board.turn_id = None
        self.handle({"kind": "assistant", "text": "哪个项目在跑"})
        self.assertTrue(any("未返回轮次 id" in n for n in self.notifies))

    def test_legacy_text_without_session_falls_to_assistant(self):
        # 入队后被 close 摘掉会话：worker 侧改走助手，不复活已删除的旧默认新建分支
        d.state["feishu_chats"].pop(CHAT, None)
        self.board.statuses = ["idle"]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "legacy_text", "text": "普通文本"})
        creates = [c for c in self.board.calls if c[1] == "/assistant/sessions"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][2]["feishu_chat_id"], CHAT)
        self.assertEqual(self.board.prompts[0]["text"], "普通文本")
        self.assertEqual(d.state["feishu_chats"], {})            # 不新建旧建卡会话

    def test_legacy_text_with_session_stays_legacy(self):
        d.state["feishu_chats"][CHAT] = active_sess(session_id=7)
        self.handle({"kind": "legacy_text", "text": "继续"})
        self.assertEqual([c for c in self.board.calls if "/assistant/" in c[1]], [])


# ==================== assistant_model（TC-30/35） ====================

class AssistantModel(AssistantBase):
    def test_resolve_states(self):
        self.assertEqual(d._feishu_assistant_model_resolve(None), (None, None))
        for raw in (123, "   ", []):
            model, reason = d._feishu_assistant_model_resolve(raw)
            self.assertIsNone(model)
            self.assertTrue(reason)
        with mock.patch.object(d, "model_alias_pool",
                               return_value=({"a": {"usable": True, "reason": ""}}, "/h", "")):
            self.assertEqual(d._feishu_assistant_model_resolve("a"), ("a", None))
            model, reason = d._feishu_assistant_model_resolve("b")
            self.assertIsNone(model)
            self.assertIn("不在模型别名池", reason)
        with mock.patch.object(d, "model_alias_pool",
                               return_value=({"a": {"usable": False, "reason": "厂商 x 未声明 type"}},
                                             "/h", "")):
            model, reason = d._feishu_assistant_model_resolve("a")
            self.assertIsNone(model)
            self.assertIn("不可用", reason)
        with mock.patch.object(d, "model_alias_pool", return_value=(None, None, "未找到 config.toml")):
            self.assertEqual(d._feishu_assistant_model_resolve("a"), ("a", "别名池不可用，未校验 assistant_model（未找到 config.toml）"))

    def test_configured_model_passed_on_create(self):
        d.FEISHU_ASSISTANT_MODEL = "gpt-4.1"
        self.board.statuses = ["idle"]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "在跑什么"})
        create = [c for c in self.board.calls if c[1] == "/assistant/sessions"][0]
        self.assertEqual(create[2], {"feishu_chat_id": CHAT, "model": "gpt-4.1"})

    def test_unconfigured_model_passes_none(self):
        self.board.statuses = ["idle"]
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "assistant", "text": "在跑什么"})
        create = [c for c in self.board.calls if c[1] == "/assistant/sessions"][0]
        self.assertIsNone(create[2]["model"])

    def test_read_assistant_model_never_leaks_secret(self):
        tmp = Path(tempfile.mkdtemp(prefix="req24-feishu-"))
        try:
            cfg = tmp / "feishu.json"
            cfg.write_text(json.dumps({"app_id": "cli_x", "app_secret": "SECRET-XYZ",
                                       "assistant_model": "kimi-k3"}), encoding="utf-8")
            d.FEISHU_CONFIG_FILE = cfg
            with mock.patch.object(d, "model_alias_pool",
                                   return_value=({"kimi-k3": {"usable": True, "reason": ""}}, "/h", "")):
                model, reason = d._feishu_assistant_model_resolve(d._read_feishu_assistant_model())
            self.assertEqual((model, reason), ("kimi-k3", None))
            self.assertNotIn("SECRET-XYZ", json.dumps(
                {"model": model, "reason": reason}, ensure_ascii=False))
            # 无该字段 → 未配置
            cfg.write_text(json.dumps({"app_id": "cli_x", "app_secret": "SECRET-XYZ"}),
                           encoding="utf-8")
            self.assertIsNone(d._read_feishu_assistant_model())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_assistant_model_fail_soft(self):
        tmp = Path(tempfile.mkdtemp(prefix="req24-feishu-"))
        try:
            d.FEISHU_CONFIG_FILE = tmp / "absent.json"
            self.assertIsNone(d._read_feishu_assistant_model())
            bad = tmp / "bad.json"
            bad.write_text("{ not json", encoding="utf-8")
            d.FEISHU_CONFIG_FILE = bad
            self.assertIsNone(d._read_feishu_assistant_model())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ==================== 启动对账（TC-62） ====================

class StartupReconcile(AssistantBase):
    def _entry(self, sid=11, **kw):
        e = {"session_id": sid, "feishu_chat_id": CHAT, "last_event_id": 0, "pending": True,
             "turn_id": "turn-1", "awaiting_project": False, "updated_at": time.time()}
        e.update(kw)
        d.state["feishu_assistant"][CHAT] = e
        return e

    def test_zombie_running_cancelled_and_session_kept(self):
        self._entry()
        self.board.statuses = ["running"]
        d._feishu_assistant_reconcile()
        self.assertEqual(self.board.cancels, [{"feishu_chat_id": CHAT, "turn_id": "turn-1"}])
        entry = d.state["feishu_assistant"][CHAT]
        self.assertEqual(entry["session_id"], 11)               # 保留 session_id
        self.assertFalse(entry["pending"])

    def test_cancelling_also_cleaned(self):
        self._entry()
        self.board.statuses = ["cancelling"]
        d._feishu_assistant_reconcile()
        self.assertEqual(len(self.board.cancels), 1)

    def test_reconcile_cancels_with_board_turn_id(self):
        # 本地持久化的 turn_id 可能已过期 → 用 board 会话行当前 turn_id（board 按 turn_id 精确栅栏）
        self._entry()
        self.board.turn_id = "turn-9"
        self.board.statuses = ["running"]
        d._feishu_assistant_reconcile()
        self.assertEqual(self.board.cancels[0]["turn_id"], "turn-9")

    def test_closed_entry_dropped(self):
        self._entry()
        self.board.statuses = ["closed"]
        d._feishu_assistant_reconcile()
        self.assertNotIn(CHAT, d.state["feishu_assistant"])

    def test_board_unreachable_keeps_entry(self):
        self._entry()
        d.api_board = mock.Mock(side_effect=urllib.error.URLError("down"))
        d._feishu_assistant_reconcile()
        self.assertIn(CHAT, d.state["feishu_assistant"])

    def test_idle_entry_pending_cleared(self):
        self._entry()
        self.board.statuses = ["idle"]
        d._feishu_assistant_reconcile()
        self.assertFalse(d.state["feishu_assistant"][CHAT]["pending"])

    def test_no_entries_no_board_calls(self):
        d._feishu_assistant_reconcile()
        self.assertEqual(self.board.calls, [])


# ==================== #44 Typing 表情（worker 边界） ====================

class _StopWorker(Exception):
    pass


class _OneShotQueue:
    """一次性队列替身：第一次 get 返回 payload，第二次 get 抛 _StopWorker 结束 worker 循环。"""

    def __init__(self, payload):
        self.payload = payload
        self.done = 0
        self.gets = 0

    def get(self):
        self.gets += 1
        if self.gets == 1:
            return self.payload
        raise _StopWorker()

    def task_done(self):
        self.done += 1


class _ReactionResp:
    """飞书 reaction 响应替身（success/code/msg/data.reaction_id）。"""

    def __init__(self, ok=True, reaction_id="rid-1"):
        self._ok = ok
        self.code, self.msg = (0, "") if ok else (99991672, "permission denied")
        self.data = (types.SimpleNamespace(reaction_id=reaction_id)
                     if reaction_id is not None else None)

    def success(self):
        return self._ok


class TypingFeedback(AssistantBase):
    """#44：assistant/legacy_text/material 处理期间在用户消息上贴 Typing 表情，处理后撤；
    close/confirm/new、缺 message_id、feishu 未启用、create 失败一律不撤（不贴或只贴）。"""

    def setUp(self):
        super().setUp()
        self.order = []
        d._feishu_handle = lambda cid, payload: self.order.append(("handle", payload.get("kind")))
        self.reactions = []
        self.create_resp = _ReactionResp()
        self.create_error = None
        feishu = mock.Mock()
        feishu.im.v1.message_reaction.create.side_effect = self._create
        feishu.im.v1.message_reaction.delete.side_effect = self._delete
        d.feishu = feishu

    def _create(self, req):
        self.reactions.append(("create", req.message_id, req.body.reaction_type.emoji_type))
        if self.create_error:
            raise self.create_error
        return self.create_resp

    def _delete(self, req):
        self.reactions.append(("delete", req.message_id, req.reaction_id))
        return _ReactionResp()

    def run_worker(self, payload):
        q = _OneShotQueue(payload)
        with self.assertRaises(_StopWorker):
            d._feishu_worker(CHAT, q)
        return q

    def test_01_assistant_text_create_then_delete(self):
        q = self.run_worker({"kind": "assistant", "text": "在跑什么", "message_id": "om_t1"})
        self.assertEqual(self.order, [("handle", "assistant")])
        self.assertEqual(self.reactions, [("create", "om_t1", "Typing"),
                                          ("delete", "om_t1", "rid-1")])
        self.assertEqual(q.done, 1)

    def test_02_create_failure_no_delete_business_ok(self):
        self.create_resp = _ReactionResp(ok=False)
        self.run_worker({"kind": "assistant", "text": "x", "message_id": "om_t2"})
        self.assertEqual(self.reactions, [("create", "om_t2", "Typing")])   # 不撤
        self.assertEqual(self.order, [("handle", "assistant")])              # 业务照常

    def test_02b_create_exception_no_delete(self):
        self.create_error = RuntimeError("permission denied")
        self.run_worker({"kind": "assistant", "text": "x", "message_id": "om_t3"})
        self.assertEqual(self.reactions, [("create", "om_t3", "Typing")])
        self.assertEqual(self.order, [("handle", "assistant")])

    def test_02c_create_without_reaction_id_no_delete_with_log(self):
        self.create_resp = _ReactionResp(reaction_id=None)
        with mock.patch.object(d.log, "info") as li:
            self.run_worker({"kind": "assistant", "text": "x", "message_id": "om_t4"})
        self.assertEqual(self.reactions, [("create", "om_t4", "Typing")])
        self.assertTrue(any("可能残留" in str(c) for c in li.call_args_list), li.call_args_list)

    def test_03_handler_exception_still_deletes_and_task_done(self):
        def _boom(cid, payload):
            raise RuntimeError("handler boom")
        d._feishu_handle = _boom
        q = self.run_worker({"kind": "assistant", "text": "x", "message_id": "om_t5"})
        self.assertEqual([r[0] for r in self.reactions], ["create", "delete"])
        self.assertEqual(q.done, 1)                                          # worker 不死

    def test_04_legacy_text_and_material_also_react(self):
        for kind in ("legacy_text", "material"):
            self.reactions.clear()
            self.run_worker({"kind": kind, "message_id": f"om_{kind}"})
            self.assertEqual(self.reactions, [("create", f"om_{kind}", "Typing"),
                                              ("delete", f"om_{kind}", "rid-1")])

    def test_05_close_confirm_new_no_reaction(self):
        for kind in ("close", "confirm", "new"):
            self.reactions.clear()
            self.run_worker({"kind": kind, "message_id": f"om_{kind}"})
            self.assertEqual(self.reactions, [])

    def test_07_missing_message_id_no_reaction(self):
        self.run_worker({"kind": "assistant", "text": "x"})
        self.assertEqual(self.reactions, [])
        self.assertEqual(self.order, [("handle", "assistant")])

    def test_08_feishu_none_returns_none(self):
        d.feishu = None
        self.assertIsNone(d._feishu_typing("om_x"))
        d.feishu = None
        self.assertIsNone(d._feishu_typing(None))

    def test_09_yn_and_unqueued_hints_no_reaction(self):
        # 入队即贴：y/n 在 on_message 内同步处理、不入队 → 不产生 reaction 调用
        d.state["pending_confirm"]["9"] = {"req": None, "question": "Q?"}
        d.on_message(up("y 9", message_id="om_yn44"))
        d.on_message(up("y 9", message_id="om_yn44b"))
        self.assertEqual(self.spawns, [(9, True)])
        self.assertEqual(self.enqueued, [])
        # 未入队的提示（无会话的「结束建卡」）同样不贴
        d.on_message(up("结束建卡", message_id="om_end44"))
        self.assertEqual(self.enqueued, [])
        self.assertEqual(self.reactions, [])

    def test_11_group_text_enqueued_no_reaction_and_sticker_ignored(self):
        # 群聊 @ 文本会入队（reaction 由 worker 贴，on_message 阶段不贴）；sticker 仍忽略
        d.on_message(up("群消息", chat_type="group", message_id="om_g1"))
        d.on_message(up("", msg_type="sticker", message_id="om_g2", content={}))
        self.assertEqual(self.kinds(), ["assistant"])
        self.assertEqual(self.reactions, [])

    def test_12_duplicate_delivery_enqueued_once(self):
        msg = up("哪个项目在跑", message_id="om_dup44")
        d.on_message(msg)
        d.on_message(msg)
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": "哪个项目在跑",
                                                "message_id": "om_dup44"})])   # 去重先于入队


# ==================== #44 无旧建卡会话的素材 → 助手 ====================

class MaterialToAssistant(AssistantBase):
    def setUp(self):
        super().setUp()
        self.new_sess = []
        d._feishu_new_sess = lambda *a, **k: self.new_sess.append((a, k)) or active_sess()
        self.downloads = []
        self.dl_error = None
        self.dl_data = b"PNGDATA"
        d._feishu_download_resource = self._download

    def _download(self, mid, key, rtype):
        self.downloads.append((mid, key, rtype))
        if self.dl_error:
            raise self.dl_error
        return self.dl_data, "x.png"

    def _payload(self, name="图.png", mtype="image", key="img_1", mid="om_img"):
        k = "image_key" if mtype == "image" else "file_key"
        return {"kind": "material", "message_id": mid,
                "handle": {"message_id": mid, k: key, "type": mtype,
                           "name": name, "size_hint": None}}

    def _run(self, payload):
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle(payload)

    def test_13_image_without_legacy_session_goes_assistant(self):
        self.board.statuses = ["idle"]
        self._run(self._payload())
        self.assertEqual(self.new_sess, [])                       # 不新建建卡会话
        self.assertFalse(any("请回复项目名" in n for n in self.notifies))
        prompt = self.board.prompts[0]
        self.assertEqual(prompt["text"], "（上传了图片）")
        att = prompt["attachments"][0]
        self.assertEqual(att["name"], "图.png")
        self.assertEqual(base64.b64decode(att["content_b64"]), b"PNGDATA")
        self.assertEqual(self.downloads, [("om_img", "img_1", "image")])

    def test_14_file_without_legacy_session_goes_assistant(self):
        self.board.statuses = ["idle"]
        self._run(self._payload(name="需求说明.txt", mtype="file", key="fk_1", mid="om_f1"))
        self.assertEqual(self.new_sess, [])
        prompt = self.board.prompts[0]
        self.assertEqual(prompt["text"], "（上传了文件）")
        self.assertEqual(prompt["attachments"][0]["name"], "需求说明.txt")
        self.assertEqual(self.downloads[0][2], "file")            # 下载 type 与消息类型一致

    def test_15_active_legacy_session_still_pending(self):
        d.state["feishu_chats"][CHAT] = active_sess(session_id=None, project=None,
                                                    awaiting_project=False)
        self._run(self._payload())
        pend = d.state["feishu_chats"][CHAT]["pending"]["materials"]
        self.assertEqual([h["name"] for h in pend], ["图.png"])   # 仍进旧 pending
        self.assertEqual(self.board.prompts, [])                  # 不进助手
        self.assertTrue(any("请回复项目名" in n for n in self.notifies))

    def test_16_session_closed_before_worker_goes_assistant(self):
        # 入队后旧会话被 close 摘掉：worker 处理时按无旧会话走助手（与 legacy_text 二次校验同口径）
        d.state["feishu_chats"].pop(CHAT, None)
        self.board.statuses = ["idle"]
        self._run(self._payload())
        self.assertEqual(self.board.prompts[0]["attachments"][0]["name"], "图.png")
        self.assertEqual(d.state["feishu_chats"], {})

    def test_18_download_failure_notifies_without_submit(self):
        self.dl_error = RuntimeError("999 boom")
        with mock.patch.object(d.log, "warning") as lw:
            self._run(self._payload())
        self.assertEqual(self.board.prompts, [])                  # 不发起本轮
        self.assertTrue(any("下载失败" in n for n in self.notifies), self.notifies)
        self.assertTrue(any("999 boom" in str(c) for c in lw.call_args_list), lw.call_args_list)

    def test_19_oversize_file_dropped_with_reason(self):
        self.dl_data = b"x" * (20 * 1024 * 1024 + 1)
        self._run(self._payload(name="big.zip", mtype="file", key="fk_big", mid="om_big"))
        self.assertEqual(self.board.prompts, [])
        self.assertTrue(any("20MB" in n for n in self.notifies), self.notifies)

    def test_21_unsupported_ext_skipped_without_download(self):
        self._run(self._payload(name="evil.exe", mtype="file", key="fk_e", mid="om_e"))
        self.assertEqual(self.downloads, [])                      # 扩展名先校验，不下载
        self.assertEqual(self.board.prompts, [])
        self.assertTrue(any("不支持的文件类型" in n for n in self.notifies), self.notifies)

    def test_23_bytes_and_base64_not_persisted_in_state(self):
        self.board.statuses = ["idle"]
        self._run(self._payload())
        raw = json.dumps(d.state, ensure_ascii=False)
        self.assertNotIn("content_b64", raw)
        self.assertNotIn(base64.b64encode(b"PNGDATA").decode(), raw)


if __name__ == "__main__":
    unittest.main()
