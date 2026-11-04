#!/usr/bin/env python3
"""#22 飞书多轮聊天建卡 —— dispatcher 侧路由矩阵（L0，全 mock，不触网、不起会话）。

对应用例 51-77、101-106、109-111、113-114：
- y/n 优先级 / 指令整行全等 / 项目名口径 / boot 等 idle 时序 / 409·400 自愈出口 /
  素材句柄不落盘 / 文档两跳 / 群聊 @ 进助手（不写 state["chat_id"]）/ idle 过期兜底 / 回放边界。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_feishu_chat_router -v
"""
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
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_REAL_SAVE_STATE = d.save_state
_REAL_ENQUEUE = d._feishu_enqueue
CHAT = "oc_test"


# ==================== 测试替身 ====================

class Resp:
    def __init__(self, success=True, code=0, msg="", data=None, file=None, file_name=None):
        self._ok = success
        self.code = code
        self.msg = msg
        self.data = data
        self.file = file
        self.file_name = file_name

    def success(self):
        return self._ok


def fake_feishu(docx=None, wiki=None, resource=None):
    """伪 lark 客户端：可观察 raw_content / get_node / message_resource 调用"""
    f = mock.Mock()
    f.im.v1.message.create.return_value = Resp()
    f.im.v1.message_resource.get.return_value = resource or Resp(
        file=io.BytesIO(b"bytes"), file_name="a.png")
    f.docx.v1.document.raw_content.return_value = docx or Resp(
        data=types.SimpleNamespace(content="DOCX正文"))
    f.wiki.v2.space.get_node.return_value = wiki or Resp(
        data=types.SimpleNamespace(node=types.SimpleNamespace(obj_type="docx", obj_token="OBJTOKEN")))
    return f


class RecBoard:
    """伪看板 chat API：记录调用 + 可编排状态序列 / 错误"""

    def __init__(self, session_id=7, statuses=None, default_status="idle",
                 prompt_errors=None, chat_error=None, materials_resp=None,
                 status="idle", row_extra=None, idle_rows=None, confirm_error=None,
                 events=None):
        self.calls = []
        self.trace = []
        self.session_id = session_id
        self.statuses = list(statuses or [])
        self.default_status = default_status
        self.prompt_errors = list(prompt_errors or [])
        self.chat_error = chat_error
        self.confirm_error = confirm_error
        self.materials_resp = materials_resp
        self.status = status
        self.row_extra = dict(row_extra or {})
        self.idle_rows = idle_rows
        self.events = list(events or [])
        self.feishu_events = []
        self.materials = []
        self.closed = []
        self.confirms = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/chat":
            self.trace.append(("chat", (body or {}).get("project")))
            if self.chat_error:
                raise self.chat_error
            return {"id": self.session_id, "project": (body or {}).get("project"), "status": "running"}
        base = path.split("?", 1)[0]
        m = re.fullmatch(r"/chat/(\d+)", base)
        if method == "GET" and m:
            cid = int(m.group(1))
            st = self.statuses.pop(0) if self.statuses else self.default_status
            self.trace.append(("status", st))
            row = {"id": cid, "status": st}
            row.update(self.row_extra)
            return row
        if method == "GET" and re.fullmatch(r"/chat/\d+/events", base):
            return self.events
        m = re.fullmatch(r"/chat/(\d+)/prompt", base)
        if method == "POST" and m:
            self.trace.append(("prompt", body))
            if self.prompt_errors:
                err = self.prompt_errors.pop(0)
                if err:
                    raise err
            return {"status": "running"}
        m = re.fullmatch(r"/chat/(\d+)/confirm", base)
        if method == "POST" and m:
            self.confirms.append(int(m.group(1)))
            if self.confirm_error:
                raise self.confirm_error
            return {"status": "running"}
        m = re.fullmatch(r"/chat/(\d+)/feishu-materials", base)
        if method == "POST" and m:
            self.materials.append(body)
            return self.materials_resp or {"files": []}
        m = re.fullmatch(r"/chat/(\d+)/feishu-events", base)
        if method == "POST" and m:
            self.feishu_events.append(body)
            return {"ok": True}
        m = re.fullmatch(r"/chat/(\d+)/close", base)
        if method == "POST" and m:
            self.closed.append(int(m.group(1)))
            return {"success": True}
        if method == "GET" and path.startswith("/chat?"):
            return self.idle_rows if self.idle_rows is not None else []
        raise AssertionError(f"未预期的看板调用: {method} {path}")


def http_error(code, payload):
    return urllib.error.HTTPError("http://x", code, "err", None,
                                  io.BytesIO(json.dumps(payload).encode()))


# 公网 DNS 桩（SSRF 守卫单测用：解析出公网 IP）
PUBLIC_DNS = [(2, 1, 6, "", ("93.184.216.34", 0))]

_MSG_SEQ = [0]     # #24：桩消息 message_id 自增唯一（去重键 = feishu_chat_id + message_id）


def up(text, chat_id=CHAT, msg_type="text", message_id=None,
       chat_type="p2p", content=None):
    # #24：message_id 默认自增唯一——on_message 现按 feishu_chat_id + message_id 去重，
    # 同一条桩消息复用同一 id 会被判为重复投递而丢弃（真实飞书 message_id 天然唯一）。
    if message_id is None:
        _MSG_SEQ[0] += 1
        message_id = f"om_{_MSG_SEQ[0]}"
    if content is None:
        content = json.dumps({"text": text}) if msg_type == "text" else json.dumps({})
    elif not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return types.SimpleNamespace(event=types.SimpleNamespace(message=types.SimpleNamespace(
        message_type=msg_type, content=content, chat_id=chat_id,
        message_id=message_id, chat_type=chat_type)))


def fresh_state(chat_id=CHAT):
    return {"chat_id": chat_id, "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}, "feishu_chats": {}}


def active_sess(session_id=7, **kw):
    s = {"project": "loop-engine", "session_id": session_id, "last_event_id": 0,
         "pending": {"text": "", "materials": []}, "awaiting_project": False,
         "replay": [], "updated_at": time.time()}
    s.update(kw)
    return s


class RouterBase(unittest.TestCase):
    _RESTORE = ("state", "save_state", "notify", "_feishu_enqueue", "send_text_chunked",
                "PROJECTS", "feishu", "api_board", "CHAT_TIMEOUT", "FEISHU_BOOT_WAIT_SEC",
                "FEISHU_CHAT_IDLE_SEC", "FEISHU_IDLE_SWEEP_INTERVAL", "_last_feishu_idle_sweep",
                "_feishu_download_resource", "_feishu_fetch_link_text", "_feishu_handle",
                "fetch_req", "post_dispatcher_event", "append_summary", "set_status", "spawn_agent",
                "STATE_FILE", "_state_loaded", "_send_feishu")

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
        self.chunked = []
        d.send_text_chunked = lambda cid, text, *a, **k: self.chunked.append((cid, text))
        d.PROJECTS = {"loop-engine": {"paths": ["/Users/x/Loop Engine/Main Repo"]}}
        d.feishu = fake_feishu()
        self.board = RecBoard()
        d.api_board = self.board
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

    def sess(self, chat_id=CHAT):
        return d.state["feishu_chats"].get(chat_id)

    def handle(self, payload, chat_id=CHAT):
        d._feishu_handle(chat_id, payload)

    def text(self, text, chat_id=CHAT):
        self.handle({"kind": "text", "text": text}, chat_id)


# ==================== 51-52 y/n 优先级 ====================

class YnPriority(RouterBase):
    def test_51_yn_wins_over_build_session(self):
        d.state["pending_confirm"]["22"] = {"req": None, "question": "Q?"}
        d.state["feishu_chats"][CHAT] = active_sess()
        d.on_message(up("y 22"))
        self.assertEqual(self.enqueued, [])          # 不走建卡路由
        self.assertEqual(self.spawns, [(22, True)])  # 走原 y/n 续跑

    def test_52_yn_with_active_session(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        d.on_message(up("n 5 原因"))
        self.assertEqual(self.enqueued, [])
        self.assertIn("没有待确认的问题", "".join(self.notifies))


# ==================== 53-54 指令（结束建卡 / 新建建卡）====================

class CloseCommands(RouterBase):
    def test_53_end_command_closes(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        d.on_message(up("结束建卡"))
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "close", "cmd": "结束建卡"})])
        self.handle({"kind": "close"})
        self.assertEqual(self.board.closed, [7])
        self.assertIsNone(self.sess())
        self.assertTrue(any("结束" in n for n in self.notifies))

    def test_53b_end_without_session_only_hints(self):
        # #24：无活跃旧会话时「结束建卡」只回状态提示，不入队、不启动任何会话
        self.notifies.clear()
        d.on_message(up("结束建卡"))
        self.assertEqual(self.enqueued, [])
        self.assertEqual(d.state["feishu_chats"], {})
        self.assertTrue(any("没有进行中的建卡会话" in n for n in self.notifies))

    def test_54_new_command_closes_then_reasks(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        d.on_message(up("新建建卡"))
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "close", "cmd": "新建建卡"})])
        self.handle({"kind": "close"})
        self.assertEqual(self.board.closed, [7])
        self.assertIsNone(self.sess())
        # #24：普通文本不再新建单项目建卡会话（改由助手接管）；只有整行「新建建卡」回到旧新建分支
        self.board = RecBoard()
        d.api_board = self.board
        self.notifies.clear()
        d.on_message(up("新建建卡"))
        self.assertEqual(self.enqueued[-1][1]["kind"], "new")
        self.handle(self.enqueued[-1][1])
        self.assertIsNotNone(self.sess())
        self.assertTrue(any("请把需求内容发过来" in n for n in self.notifies))
        self.assertEqual([c for c in self.board.calls if c[0] == "POST" and c[1] == "/chat"], [])


# ==================== 55-62 项目解析与建卡讨论 ====================

class ProjectResolution(RouterBase):
    def _ask(self, text, handler_text):
        d.on_message(up(text))
        self.assertEqual(self.enqueued[0][1]["kind"], "text")
        self.text(handler_text)

    def test_55_plain_text_asks_project(self):
        self.text("帮我做个需求")
        joined = "".join(self.notifies)
        self.assertIn("loop-engine", joined)
        self.assertIn("Main Repo", joined)
        self.assertEqual([c for c in self.board.calls if c[0] == "POST"], [])
        self.assertEqual(self.sess()["pending"]["text"], "帮我做个需求")

    def test_56_reply_index_creates_session(self):
        self.text("需求原文")
        self.text("1")
        posts = [c for c in self.board.calls if c[0] == "POST" and c[1] == "/chat"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {"project": "loop-engine", "channel": "feishu"})
        prompts = [c for c in self.board.calls if c[0] == "POST" and c[1].endswith("/prompt")]
        self.assertEqual(len(prompts), 1)
        self.assertIn("需求原文", prompts[0][2]["text"])

    def test_57_reply_project_name_adopted(self):
        self.text("需求原文")
        self.text("loop-engine")
        self.assertEqual([c for c in self.board.calls if c[0] == "POST" and c[1] == "/chat"][0][2]["project"],
                         "loop-engine")

    def test_58_reply_dir_name_not_adopted(self):
        self.text("需求原文")
        self.text("Main Repo")
        self.assertEqual([c for c in self.board.calls if c[0] == "POST" and c[1] == "/chat"], [])
        self.assertTrue(any("请回复项目名" in n for n in self.notifies))

    def test_59_two_invalid_clears_pending(self):
        self.text("需求原文")
        self.text("不存在的项目")
        self.assertIsNotNone(self.sess())
        self.text("还是不对")
        self.assertIsNone(self.sess())                 # 清 pending
        self.assertEqual([c for c in self.board.calls if c[0] == "POST" and c[1] == "/chat"], [])
        self.assertTrue(any("无法识别" in n for n in self.notifies))

    def test_60_text_with_project_boots_directly(self):
        self.text("用 loop-engine 做个导出需求")
        self.assertEqual([c for c in self.board.calls if c[0] == "POST" and c[1] == "/chat"][0][2]["project"],
                         "loop-engine")
        self.assertFalse(any("请回复项目名" in n for n in self.notifies))

    def test_61_active_session_continues(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        self.text("继续聊这个")
        self.assertFalse(any("请回复项目名" in n for n in self.notifies))
        prompts = [c for c in self.board.calls if c[0] == "POST" and c[1].endswith("/prompt")]
        self.assertEqual(len(prompts), 1)
        self.assertIn("继续聊这个", prompts[0][2]["text"])

    def test_62_pending_unmatched_no_session_goes_assistant(self):
        # #24：无活跃旧会话的普通文本一律进助手（原「无法识别」提示分支随旧默认新建分支一并删除）
        d.state["pending_confirm"]["9"] = {"req": None, "question": "Q?"}
        msg = up("随便说点啥")
        d.on_message(msg)
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": "随便说点啥",
                                                "message_id": msg.event.message.message_id})])

    def test_63_first_message_greets_then_handles_same_message(self):
        # #24：首条消息只问候一次，且**问候后继续处理本条**（原实现直接 return 把整条吞掉）
        d.state = fresh_state(chat_id=None)
        self.notifies.clear()
        msg = up("你好")
        d.on_message(msg)
        self.assertEqual(d.state["feishu_chats"], {})
        self.assertTrue(any("已连接" in n for n in self.notifies))
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": "你好",
                                                "message_id": msg.event.message.message_id})])
        # 同 chat 后续消息不再问候
        self.notifies.clear()
        msg2 = up("在吗")
        d.on_message(msg2)
        self.assertFalse(any("已连接" in n for n in self.notifies))
        self.assertEqual(self.enqueued[-1][1], {"kind": "assistant", "text": "在吗",
                                               "message_id": msg2.event.message.message_id})


# ==================== 64-65 / 105-106 素材（句柄）====================

class Materials(RouterBase):
    def _mat(self, name="a.png", key="fk1", mtype="file", mid="om_x"):
        content = {"file_key": key, "file_name": name} if mtype == "file" else {"image_key": key}
        return up("", msg_type=mtype, message_id=mid, content=content)

    def test_64_material_before_project_stored_then_uploaded(self):
        # #44：无活跃旧建卡会话的素材改走助手；「素材先于项目」只发生在**有**活跃旧会话时
        d.state["feishu_chats"][CHAT] = active_sess(session_id=None, project=None,
                                                    awaiting_project=False)
        self.board = RecBoard(materials_resp={"files": [
            {"filename": "a.png", "size": 4, "path": "/x/a.png", "over_read_limit": False}]})
        d.api_board = self.board
        d.on_message(self._mat())
        self.assertEqual(self.enqueued[0][1]["kind"], "material")
        self.handle(self.enqueued[0][1])
        h = self.sess()["pending"]["materials"][0]
        self.assertEqual(set(h), {"message_id", "file_key", "type", "name", "size_hint"})
        self.assertIn("请回复项目名", "".join(self.notifies))
        self.text("loop-engine")
        self.assertEqual(len(self.board.materials), 1)
        atts = [c for c in self.board.calls if c[0] == "POST" and c[1].endswith("/prompt")][0][2]["attachments"]
        self.assertEqual(atts, ["a.png"])

    def _handle(self, name="a.png", key="fk1", mtype="file", mid="om_x"):
        return {"message_id": mid, "file_key": key, "type": mtype, "name": name, "size_hint": None}

    def test_65_download_failure_notifies(self):
        self.board = RecBoard()
        d.api_board = self.board
        d._feishu_download_resource = mock.Mock(side_effect=RuntimeError("999 boom"))
        self.text("需求原文")
        self.handle({"kind": "material", "handle": self._handle()})
        self.text("loop-engine")
        self.assertTrue(any("999" in n for n in self.notifies))

    def test_105_pending_only_handles_and_small(self):
        tmp = Path(tempfile.mkdtemp(prefix="feishu-state-"))
        try:
            d.STATE_FILE = tmp / "dispatcher_state.json"
            d._state_loaded = True
            d.save_state = _REAL_SAVE_STATE
            spy = mock.Mock(side_effect=RuntimeError("不应在选项目前下载"))
            d._feishu_download_resource = spy
            # #44：只有活跃旧建卡会话下的素材才走 pending 延迟下载链路
            d.state["feishu_chats"][CHAT] = active_sess(session_id=None, project=None,
                                                        awaiting_project=False)
            self.handle({"kind": "material", "handle": self._handle()})
            d.save_state()
            raw = (tmp / "dispatcher_state.json").read_text(encoding="utf-8")
            self.assertNotIn("base64", raw)
            self.assertNotIn("content_b64", raw)
            entry = json.loads(raw)["feishu_chats"][CHAT]
            self.assertEqual(set(entry["pending"]["materials"][0]),
                             {"message_id", "file_key", "type", "name", "size_hint"})
            self.assertLess(len(json.dumps(entry, ensure_ascii=False)), 4096)
            self.assertEqual(spy.call_count, 0)          # 下载延迟到项目选定后
            self.board = RecBoard(materials_resp={"files": [
                {"filename": "a.png", "size": 4, "path": "/x/a.png", "over_read_limit": False}]})
            d.api_board = self.board
            d._feishu_download_resource = lambda *a, **k: (b"data", "a.png")
            self.text("loop-engine")
            prompts = [c for c in self.board.calls if c[0] == "POST" and c[1].endswith("/prompt")]
            self.assertEqual(prompts[0][2]["attachments"], ["a.png"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_106_download_failure_system_event(self):
        self.board = RecBoard()
        d.api_board = self.board
        d._feishu_download_resource = mock.Mock(side_effect=RuntimeError("999 boom"))
        self.text("需求原文")
        self.handle({"kind": "material", "handle": self._handle()})
        self.text("loop-engine")
        flat = [ev for body in self.board.feishu_events for ev in body["events"]]
        self.assertTrue(any(ev["kind"] == "system" and "下载失败" in ev["content"] for ev in flat))

    def test_64b_image_without_file_name_has_ext(self):
        # ⑥ 无 file_name 的图片 → 句柄名带合法扩展名（否则 board /feishu-materials 400）
        msg = up("", msg_type="image", message_id="om_img", content={"image_key": "img_abc"})
        handle = d._feishu_parse_material(msg.event.message)
        self.assertIsNotNone(handle)
        self.assertTrue(d._feishu_ext_ok(handle["name"]), handle["name"])
        # 上传路径：SDK 也拿不到 file_name（返回 None）→ 仍带扩展名、非 400 路径
        self.board = RecBoard(materials_resp={"files": [
            {"filename": "图片-img_abc.png", "size": 4, "path": "/x", "over_read_limit": False}]})
        d.api_board = self.board
        d._feishu_download_resource = lambda mid, key, t: (b"data", None)
        d.state["feishu_chats"][CHAT] = active_sess(session_id=None, project=None,
                                                    awaiting_project=False)
        d.on_message(up("", msg_type="image", message_id="om_img", content={"image_key": "img_abc"}))
        self.handle(self.enqueued[0][1])
        self.text("loop-engine")
        sent = self.board.materials[0]["files"][0]["name"]
        self.assertTrue(d._feishu_ext_ok(sent), sent)

    def test_64c_file_without_name_skipped(self):
        # ⑥ 无 file_name 的文件 → 不产生无扩展名句柄，明确提示跳过
        msg = up("", msg_type="file", message_id="om_f", content={"file_key": "fk1"})
        self.assertIsNone(d._feishu_parse_material(msg.event.message))
        d.on_message(msg)
        self.assertEqual(self.enqueued, [])
        self.assertTrue(any("已跳过" in n for n in self.notifies))


# ==================== 66-70 / 101 文档与链接 ====================

class DocLinks(RouterBase):
    def _send(self, text):
        d.state["feishu_chats"][CHAT] = active_sess()
        self.text(text)
        return [c for c in self.board.calls if c[0] == "POST" and c[1].endswith("/prompt")]

    def test_66_docx_direct(self):
        d.feishu = fake_feishu(docx=Resp(data=types.SimpleNamespace(content="DOCX正文内容")))
        prompts = self._send("看下 https://x.feishu.cn/docx/TOKEN123 这段")
        self.assertIn("DOCX正文内容", prompts[0][2]["text"])
        req = d.feishu.docx.v1.document.raw_content.call_args.args[0]
        self.assertEqual(req.document_id, "TOKEN123")

    def test_67_wiki_two_hops(self):
        d.feishu = fake_feishu(
            wiki=Resp(data=types.SimpleNamespace(node=types.SimpleNamespace(obj_type="docx", obj_token="OBJTOK"))),
            docx=Resp(data=types.SimpleNamespace(content="WIKI正文")))
        prompts = self._send("wiki https://x.feishu.cn/wiki/NODETOK")
        self.assertIn("WIKI正文", prompts[0][2]["text"])
        d.feishu.wiki.v2.space.get_node.assert_called_once()
        req = d.feishu.docx.v1.document.raw_content.call_args.args[0]
        self.assertEqual(req.document_id, "OBJTOK")

    def test_68_wiki_non_docx_degrades(self):
        d.feishu = fake_feishu(
            wiki=Resp(data=types.SimpleNamespace(node=types.SimpleNamespace(obj_type="sheet", obj_token="S1"))))
        self._send("wiki https://x.feishu.cn/wiki/N2")
        d.feishu.docx.v1.document.raw_content.assert_not_called()
        self.assertTrue(any("不支持抓正文" in n for n in self.notifies))

    def test_69_wiki_api_failure_degrades(self):
        d.feishu = fake_feishu(wiki=Resp(success=False, code=131006, msg="permission denied"))
        prompts = self._send("wiki https://x.feishu.cn/wiki/N3")
        self.assertEqual(len(prompts), 1)              # 不阻断本轮
        self.assertTrue(any("抓取失败" in n for n in self.notifies))

    def test_70_plain_http_and_non_http(self):
        d._feishu_fetch_link_text = mock.Mock(return_value="去标签正文")
        with mock.patch.object(d.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            prompts = self._send("看 https://example.com/a 以及 file:///etc/passwd")
        self.assertIn("去标签正文", prompts[0][2]["text"])
        d._feishu_fetch_link_text.assert_called_once_with("https://example.com/a")

    def test_101_wiki_uses_v2_space_get_node(self):
        d.feishu = fake_feishu()
        self._send("wiki https://x.feishu.cn/wiki/NODETOK")
        req = d.feishu.wiki.v2.space.get_node.call_args.args[0]
        # 断言走的是 wiki v2 space 面（v1 node 只有 search，无 get_node）
        self.assertEqual(req.uri, "/open-apis/wiki/v2/spaces/get_node")
        self.assertEqual(req.token, "NODETOK")


# ==================== 71-72 / 114 群聊与指令匹配 ====================

class GroupAndCommands(RouterBase):
    def test_71_group_text_routes_assistant_no_state_write(self):
        # #22 行为变化 2：群聊 @ 文本进看板助手，但**不写** state["chat_id"]（私聊通知通道保持唯一）
        d.state = fresh_state(chat_id="oc_old")
        msg = up("群消息", chat_type="group")
        d.on_message(msg)
        self.assertEqual(d.state["chat_id"], "oc_old")   # 不写 state["chat_id"]
        self.assertEqual(self.enqueued, [(CHAT, {"kind": "assistant", "text": "群消息",
                                                "message_id": msg.event.message.message_id})])
        self.assertEqual(self.notifies, [])             # 群聊不问候、不发提示

    def test_71b_group_mention_placeholder_stripped(self):
        # 群 @ 文本里的 @_user_N 占位符按 mentions[].key 剥离后再进助手
        msg = up("@_user_1 查一下项目状态", chat_type="group")
        msg.event.message.mentions = [types.SimpleNamespace(key="@_user_1", name="Ron's CC")]
        d.on_message(msg)
        self.assertEqual(self.enqueued[0][1]["text"], "查一下项目状态")

    def test_71c_group_post_text_and_image_routes_assistant(self):
        # 图文混排（飞书 msg_type=post）此前被类型白名单静默丢弃 → 「贴图后没反应」。
        # 现在正文进 text、图片生成素材句柄随 payload 入队（下载延迟到 worker）。
        d.state = fresh_state(chat_id="oc_old")
        msg = up("", msg_type="post", chat_type="group", content={
            "title": "",
            "content": [[{"tag": "text", "text": "@_user_1 按图改一下看板"}],
                        [{"tag": "img", "image_key": "img_v2_abc12345"}]],
        })
        msg.event.message.mentions = [types.SimpleNamespace(key="@_user_1", name="Ron's CC")]
        d.on_message(msg)
        self.assertEqual(d.state["chat_id"], "oc_old")          # 群聊仍不写私聊通知通道
        payload = self.enqueued[0][1]
        self.assertEqual(payload["kind"], "assistant")
        self.assertEqual(payload["text"], "按图改一下看板")
        self.assertEqual(len(payload["handles"]), 1)
        self.assertEqual(payload["handles"][0]["image_key"], "img_v2_abc12345")
        self.assertEqual(payload["handles"][0]["type"], "image")
        self.assertTrue(payload["handles"][0]["name"].endswith(".png"))

    def test_71d_group_post_without_content_ignored(self):
        # 空富文本（无正文无图）不发起助手轮次，避免 board 侧 400「消息不能为空」
        d.state = fresh_state(chat_id="oc_old")
        d.on_message(up("", msg_type="post", chat_type="group", content={"content": []}))
        self.assertEqual(self.enqueued, [])
        self.assertEqual(d.state["chat_id"], "oc_old")

    def test_71e_group_image_message_routes_assistant_with_handle(self):
        # 纯图片（截图单独发）也进助手：默认正文「（上传了图片）」+ 图片句柄
        msg = up("", msg_type="image", chat_type="group",
                 content={"image_key": "img_v2_solo0001"})
        d.on_message(msg)
        payload = self.enqueued[0][1]
        self.assertEqual(payload["text"], "（上传了图片）")
        self.assertEqual(payload["handles"][0]["image_key"], "img_v2_solo0001")

    def test_71f_unsupported_type_ignored_without_enqueue(self):
        # 未支持类型（贴纸等）继续忽略，但已改为留日志（不再静默）
        d.state = fresh_state(chat_id="oc_old")
        d.on_message(up("", msg_type="sticker", content={}, chat_type="group"))
        self.assertEqual(self.enqueued, [])

    def test_71g_handles_prepare_partial_and_all_failed(self):
        # 多附件：逐条下载，部分失败保留可用项；全部失败返回原因（不发起本轮）
        def dl(_message_id, key, _rtype):
            if key == "bad":
                raise RuntimeError("download failed")
            return b"bytes", "a.png"
        d._feishu_download_resource = dl
        ok, err = d._feishu_assistant_handles_prepare([
            {"message_id": "m1", "image_key": "ok1", "type": "image", "name": "a.png"},
            {"message_id": "m1", "image_key": "bad", "type": "image", "name": "b.png"},
        ])
        self.assertIsNone(err)
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["name"], "a.png")
        ok, err = d._feishu_assistant_handles_prepare([
            {"message_id": "m1", "image_key": "bad", "type": "image", "name": "b.png"},
        ])
        self.assertIsNone(ok)
        self.assertIn("下载失败", err)

    def test_72_sticker_ignored(self):
        d.state = fresh_state(chat_id="oc_old")
        d.on_message(up("", msg_type="sticker", content={}))
        self.assertEqual(self.enqueued, [])
        self.assertEqual(d.state["chat_id"], "oc_old")

    def test_114_commands_matching_rules(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        # ① 讨论原话不触发（活跃旧会话 → 走旧链路续聊）
        msg = up("那就确认建卡，结束建卡流程")
        d.on_message(msg)
        self.assertEqual(self.enqueued[-1][1], {"kind": "legacy_text", "text": "那就确认建卡，结束建卡流程",
                                                "message_id": msg.event.message.message_id})
        # ② 首尾空白 + 中英文标点触发
        d.on_message(up("  结束建卡。"))
        self.assertEqual(self.enqueued[-1][1]["kind"], "close")
        d.on_message(up("新建建卡！"))
        self.assertEqual(self.enqueued[-1][1]["kind"], "close")
        # ③ #24 无活跃建卡会话 → 「结束建卡」只回状态提示（不入队、不新建会话、不进助手）
        before = len(self.enqueued)
        d.state["feishu_chats"].clear()
        self.notifies.clear()
        d.on_message(up("结束建卡"))
        self.assertEqual(len(self.enqueued), before)
        self.assertTrue(any("没有进行中的建卡会话" in n for n in self.notifies))

    def test_114d_guide_documents_rules(self):
        html = (REPO / "docs" / "guide.html").read_text(encoding="utf-8")
        self.assertIn("匹配规则（整行全等）", html)
        self.assertIn("群聊消息", html)
        self.assertIn("#22 行为变化", html)

    def test_114b_cmd_punct_multi_layer(self):
        # ⑨ 循环剥标点：多层标点/引号/空白都能命中
        d.state["feishu_chats"][CHAT] = active_sess()
        for t in ("结束建卡。。", "「结束建卡」", "  结束建卡！？  ", "新建建卡。"):
            with self.subTest(t=t):
                d.on_message(up(t))
                self.assertEqual(self.enqueued[-1][1]["kind"], "close")
        # 含指令词但不是整行全等 → 不触发（讨论原话，活跃旧会话 → 旧链路续聊）
        for t in ("那就确认建卡，结束建卡流程", "我想结束建卡了"):
            with self.subTest(t=t):
                msg = up(t)
                d.on_message(msg)
                self.assertEqual(self.enqueued[-1][1],
                                 {"kind": "legacy_text", "text": t,
                                  "message_id": msg.event.message.message_id})


# ==================== 73-77 交付细节 ====================

class Delivery(RouterBase):
    def test_73_post_chat_409_hints_two_exits(self):
        self.board = RecBoard(chat_error=http_error(409, {"error": "该项目已有活跃建卡会话"}))
        d.api_board = self.board
        self.text("loop-engine 需求原文")
        joined = "".join(self.notifies)
        self.assertIn("结束建卡", joined)
        self.assertIn("关闭会话", joined)
        self.assertIsNotNone(self.sess())                # pending 保留

    def test_74_on_message_returns_immediately(self):
        calls = []
        d._feishu_handle = lambda cid, payload: calls.append(
            (threading.current_thread().name, cid, payload))
        d._feishu_enqueue = _REAL_ENQUEUE
        d._feishu_queues.clear()
        d._feishu_workers.clear()
        t0 = time.time()
        d.on_message(up("你好呀"))
        self.assertLess(time.time() - t0, 0.5)
        for _ in range(200):
            if calls:
                break
            time.sleep(0.01)
        self.assertTrue(calls)
        self.assertNotEqual(calls[0][0], threading.current_thread().name)

    def test_75_await_round_sends_message_not_agent(self):
        self.board.statuses = ["running", "idle"]
        ev = [{"id": 5, "kind": "message", "content": "方案A"},
              {"id": 6, "kind": "agent", "content": "[飞书] 机器人：请回复项目名"},
              {"id": 7, "kind": "user", "content": "[飞书] 用户：loop-engine"}]
        orig = self.board.calls

        def board(method, path, body=None):
            orig.append((method, path, body))
            if path.endswith("/events?after=0"):
                return ev
            if method == "GET" and re.fullmatch(r"/chat/\d+", path):
                return {"id": 7, "status": self.board.statuses.pop(0) if self.board.statuses else "idle"}
            return {}
        d.api_board = board
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            d._feishu_await_round(CHAT, 7, 0)
        # 只回发 kind='message'（真实 agent 正文）；kind='agent'（回放）不得再回发
        self.assertEqual(self.chunked, [(CHAT, "方案A")])

    def test_75b_await_round_timeout_hint(self):
        d.api_board = lambda method, path, body=None: ({"id": 7, "status": "running"}
                                                       if path == "/chat/7" else [])
        d.CHAT_TIMEOUT = -60          # deadline 立即到期
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            d._feishu_await_round(CHAT, 7, 0)
        self.assertTrue(any("超时" in n for n in self.notifies))

    def test_76_chunked_sends(self):
        sent = []
        d._send_feishu = lambda mt, obj, cid=None: sent.append((mt, obj, cid)) or True
        d.send_text_chunked = self._orig["send_text_chunked"]
        n = d.send_text_chunked(CHAT, "a" * 12000)
        self.assertEqual(n, 5)
        self.assertEqual(len(sent), 5)
        self.assertTrue(sent[-1][1]["text"].endswith("…（剩余内容见看板）"))
        self.assertTrue(all(cid == CHAT for _, _, cid in sent))

    def test_77_expire_idle_chats(self):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(seconds=200000)).strftime("%Y-%m-%d %H:%M:%S")
        new = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            {"id": 1, "channel": "feishu", "status": "idle", "updated_at": old},
            {"id": 2, "channel": "feishu", "status": "idle", "updated_at": new},
            {"id": 3, "channel": "web", "status": "idle", "updated_at": old},
        ]
        board = RecBoard(idle_rows=rows)
        d.api_board = board
        d.feishu = fake_feishu()
        d.FEISHU_CHAT_IDLE_SEC = 86400
        d.FEISHU_IDLE_SWEEP_INTERVAL = 300
        d._last_feishu_idle_sweep = 0.0
        d.state["feishu_chats"]["oc_a"] = active_sess(session_id=1)
        d._feishu_expire_idle_chats()
        self.assertEqual(board.closed, [1])              # 仅第一条
        self.assertNotIn("oc_a", d.state["feishu_chats"])  # 本地状态清
        # 节流：紧接第二次不重复
        board.closed.clear()
        d._feishu_expire_idle_chats()
        self.assertEqual(board.closed, [])
        # IDLE_SEC=0 → 整体跳过
        d.FEISHU_CHAT_IDLE_SEC = 0
        d._last_feishu_idle_sweep = 0.0
        d._feishu_expire_idle_chats()
        self.assertEqual(board.closed, [])


# ==================== 102-104 状态对账 / 自愈出口 ====================

class Reconcile(RouterBase):
    def _run(self, status, extra=None):
        self.board = RecBoard(default_status=status, row_extra=extra)
        d.api_board = self.board
        d.state = fresh_state()
        d.state["feishu_chats"][CHAT] = active_sess()
        self.notifies.clear()
        self.text("下一轮消息")
        return self.board

    def test_102_closed_clears_state(self):
        self.board = RecBoard(default_status="closed")
        d.api_board = self.board
        d.state["feishu_chats"][CHAT] = active_sess()
        self.text("下一轮消息")
        self.assertEqual([c for c in self.board.calls if c[1].endswith("/prompt")], [])
        self.assertIsNone(self.sess())
        self.assertTrue(any("会话已结束" in n for n in self.notifies))

    def test_103_terminal_statuses_and_ok_statuses(self):
        for status, hint in (("failed", "不可用"), ("suspended", "不可用"), ("created", "已建卡")):
            with self.subTest(status=status):
                self._run(status, extra={"req_id": 42} if status == "created" else None)
                self.assertIsNone(self.sess())
                self.assertTrue(any(hint in n for n in self.notifies), self.notifies)
        for status in ("idle", "interrupted"):
            with self.subTest(status=status):
                board = self._run(status)
                prompts = [c for c in board.calls if c[1].endswith("/prompt")]
                self.assertEqual(len(prompts), 1)

    def test_104_prompt_400_not_conversational_self_heals(self):
        self.board = RecBoard(prompt_errors=[http_error(400, {"error": "会话状态 idle 不可对话"})])
        d.api_board = self.board
        d.state["feishu_chats"][CHAT] = active_sess()
        self.text("消息")
        prompts = [c for c in self.board.calls if c[1].endswith("/prompt")]
        self.assertEqual(len(prompts), 1)                # 不重试
        self.assertIsNone(self.sess())                   # 清状态
        self.assertTrue(self.notifies)


# ==================== 109-111 首轮 boot 时序 ====================

class BootTiming(RouterBase):
    def test_109_waits_idle_before_prompt(self):
        self.board = RecBoard(statuses=["running", "running", "idle"])
        d.api_board = self.board
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.text("loop-engine 需求原文")
        trace = self.board.trace
        kinds = [k for k, _ in trace]
        prompt_i = kinds.index("prompt")
        statuses_before = [v for k, v in trace[:prompt_i] if k == "status"]
        self.assertEqual(statuses_before[-1], "idle")     # 首个 idle 之后才 prompt
        self.assertNotIn("running", statuses_before[2:])  # idle 之后无 running
        self.assertEqual(sum(1 for k, _ in trace if k == "prompt"), 1)
        self.assertFalse(any("409" in n for n in self.notifies))
        self.assertFalse(any("超时" in n or "失败" in n for n in self.notifies))

    def test_110_boot_timeout_keeps_session(self):
        self.board = RecBoard(default_status="running")
        d.api_board = self.board
        d.FEISHU_BOOT_WAIT_SEC = 0
        self.text("loop-engine 需求原文")
        self.assertTrue(any("启动超时" in n for n in self.notifies))
        self.assertEqual(self.sess()["session_id"], 7)
        # 后续消息按 D8‴ 对账继续
        self.board.default_status = "idle"
        self.text("继续")
        prompts = [c for c in self.board.calls if c[1].endswith("/prompt")]
        self.assertEqual(len(prompts), 1)

    def test_111_prompt_409_then_resend(self):
        self.board = RecBoard(statuses=["idle", "running", "idle", "idle"],
                              prompt_errors=[http_error(409, {"error": "会话正在运行，请等待本轮结束"}), None])
        d.api_board = self.board
        d.state["feishu_chats"][CHAT] = active_sess()
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.text("消息")
        prompts = [c for c in self.board.calls if c[1].endswith("/prompt")]
        self.assertEqual(len(prompts), 2)                # 409 后重发一次成功
        self.assertEqual(self.sess()["pending"]["text"], "")

    def test_111b_prompt_409_timeout_keeps_pending(self):
        self.board = RecBoard(statuses=["idle"], default_status="running",
                              prompt_errors=[http_error(409, {"error": "会话正在运行，请等待本轮结束"})])
        d.api_board = self.board
        d.CHAT_TIMEOUT = 0
        d.state["feishu_chats"][CHAT] = active_sess()
        self.text("不能丢的消息")
        self.assertTrue(any("上一轮仍在进行" in n for n in self.notifies))
        self.assertEqual(self.sess()["pending"]["text"], "不能丢的消息")


# ==================== 113 回放边界 ====================

class ReplayBoundary(RouterBase):
    def test_113_replay_excludes_requirement_and_card_event(self):
        self.text("需求原文")
        self.text("loop-engine")
        flat = [ev for body in self.board.feishu_events for ev in body["events"]]
        self.assertEqual([ev["kind"] for ev in flat], ["agent", "user"])
        self.assertTrue(all("已建卡" not in ev["content"] for ev in flat))
        self.assertTrue(all("需求原文" not in ev["content"] for ev in flat))


# ==================== ① 飞书侧「确认建卡」闭环 ====================

class ConfirmBuild(RouterBase):
    def test_confirm_route_enqueues_confirm(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        d.on_message(up("确认建卡"))
        self.assertEqual(self.enqueued[-1][1], {"kind": "confirm", "cmd": "确认建卡"})

    def test_confirm_no_active_session_only_hints(self):
        # #24：无活跃建卡会话 → 「确认建卡」只回状态提示（不入队、不启动任何会话、不进助手）
        self.notifies.clear()
        d.on_message(up("确认建卡"))
        self.assertEqual(self.enqueued, [])
        self.assertEqual(d.state["feishu_chats"], {})
        self.assertTrue(any("还没有草稿" in n for n in self.notifies))

    def test_confirm_draft_calls_confirm_and_reports_result(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        board = RecBoard(statuses=["idle", "running", "idle"], row_extra={"draft_title": "标题X"},
                         events=[{"id": 5, "kind": "message", "content": "建卡成功：#321 标题X"}])
        d.api_board = board
        with mock.patch.object(d.time, "sleep", lambda *a, **k: None):
            self.handle({"kind": "confirm"})
        self.assertEqual(board.confirms, [7])                       # 调 /confirm
        self.assertTrue(any("已建卡" in c or "#321" in c for _, c in self.chunked))  # 建卡结果回发
        self.assertEqual(self.sess()["draft_prompted"], "标题X")    # 不再重复「回复确认建卡」

    def test_confirm_no_draft_hints(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        board = RecBoard(default_status="idle")                     # 无 draft_title
        d.api_board = board
        self.handle({"kind": "confirm"})
        self.assertEqual(board.confirms, [])                        # 不调 /confirm
        self.assertTrue(any("还没有草稿" in n for n in self.notifies))

    def test_confirm_created_hints_card_id(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        board = RecBoard(default_status="created", row_extra={"req_id": 42})
        d.api_board = board
        self.handle({"kind": "confirm"})
        self.assertEqual(board.confirms, [])
        self.assertTrue(any("#42" in n for n in self.notifies))

    def test_confirm_http_error_not_silent(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        board = RecBoard(default_status="idle", row_extra={"draft_title": "T"},
                         confirm_error=http_error(409, {"error": "本会话已建卡 #9"}))
        d.api_board = board
        self.handle({"kind": "confirm"})
        self.assertTrue(any("确认建卡失败" in n and "#9" in n for n in self.notifies))


# ==================== ① 每轮结束草稿提示 ====================

class DraftReadyPrompt(RouterBase):
    def test_draft_ready_prompt_once_per_draft(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        board = RecBoard(default_status="idle", row_extra={"draft_title": "标题A"})
        d.api_board = board
        self.text("再改改")
        self.assertTrue(any("回复『确认建卡』" in n for n in self.notifies))
        self.notifies.clear()
        self.text("继续")
        self.assertFalse(any("回复『确认建卡』" in n for n in self.notifies))   # 同一草稿不重复

    def test_no_draft_no_prompt(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        d.api_board = RecBoard(default_status="idle")
        self.text("随便说说")
        self.assertFalse(any("回复『确认建卡』" in n for n in self.notifies))


# ==================== ⑤ SSRF 守卫与飞书域名校验 ====================

class SsrfGuard(RouterBase):
    def test_rejects_non_public_urls(self):
        for url in ("http://127.0.0.1:8899/api/feishu-settings", "http://localhost/x",
                    "file:///etc/passwd", "http://internal.local/x", "http://x.internal/x", "not a url"):
            with self.subTest(url=url):
                self.assertFalse(d._feishu_url_allowed(url))

    def test_allows_public_url(self):
        with mock.patch.object(d.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            self.assertTrue(d._feishu_url_allowed("https://example.com/a"))
        # DNS 解析失败 → 拒
        with mock.patch.object(d.socket, "getaddrinfo", side_effect=OSError("no dns")):
            self.assertFalse(d._feishu_url_allowed("https://nope.invalid/a"))

    def test_ssrf_no_get_and_degrade(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        with mock.patch.object(d._opener, "open") as op:
            self.text("看 http://127.0.0.1:8899/api/feishu-settings 与 http://internal.local/x")
        op.assert_not_called()                                     # 不发起 GET
        self.assertTrue(any("不可抓取" in n for n in self.notifies))  # 有降级提示

    def test_public_link_still_fetched(self):
        d.state["feishu_chats"][CHAT] = active_sess()
        d._feishu_fetch_link_text = mock.Mock(return_value="公网正文")
        with mock.patch.object(d.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            self.text("看 https://example.com/a")
        prompts = [c for c in self.board.calls if c[0] == "POST" and c[1].endswith("/prompt")]
        self.assertIn("公网正文", prompts[0][2]["text"])
        d._feishu_fetch_link_text.assert_called_once_with("https://example.com/a")

    def test_docx_path_non_feishu_host_uses_plain_link(self):
        # ⑤ /docx/ 路径仅在飞书域名下才走 docx API；非飞书域名按普通链接处理
        d.state["feishu_chats"][CHAT] = active_sess()
        d._feishu_fetch_link_text = mock.Mock(return_value="普通正文")
        with mock.patch.object(d.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            self.text("看 https://example.com/docx/TOKEN123")
        d.feishu.docx.v1.document.raw_content.assert_not_called()
        d._feishu_fetch_link_text.assert_called_once_with("https://example.com/docx/TOKEN123")

    def test_redirect_target_revalidated(self):
        # ② 重定向每跳复校：302 到回环/链路本地/非 http(s) 一律拒（默认 handler 会直接跟随）
        guard = d._FeishuRedirectGuard()
        req = urllib.request.Request("https://example.com/a")
        for bad in ("http://127.0.0.1:8899/api/feishu-settings",
                    "http://169.254.169.254/latest/meta-data/",
                    "file:///etc/passwd",
                    "http://internal.local/x"):
            with self.subTest(target=bad):
                with self.assertRaises(urllib.error.HTTPError):
                    guard.redirect_request(req, None, 302, "Found", {}, bad)
        # 公网目标放行（返回新的 Request，由 urllib 继续走下一跳）
        with mock.patch.object(d.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            out = guard.redirect_request(req, None, 302, "Found", {}, "https://example.com/b")
        self.assertTrue(out is not None and out.full_url == "https://example.com/b")

    def test_fetch_uses_guarded_opener(self):
        # ② 抓正文走专用 opener（redirect 复校），不再用共享 _opener
        with mock.patch.object(d._opener, "open") as shared, \
                mock.patch.object(d, "_feishu_url_allowed", return_value=True), \
                mock.patch.object(d._feishu_opener, "open", side_effect=OSError("stop")) as guarded:
            with self.assertRaises(Exception):
                d._feishu_fetch_link_text("https://example.com/a")
        shared.assert_not_called()
        guarded.assert_called_once()

    def test_connected_private_peer_rejected(self):
        # ② DNS 重绑定兜底：连上后对端是私有 IP → 拒（不读响应体）
        resp = mock.Mock()
        resp.fp.raw._sock.getpeername.return_value = ("127.0.0.1", 8899)
        resp.read = mock.Mock(return_value=b"<html>secret</html>")
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=resp)
        ctx.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(d, "_feishu_url_allowed", return_value=True), \
                mock.patch.object(d._feishu_opener, "open", return_value=ctx):
            with self.assertRaises(ValueError):
                d._feishu_fetch_link_text("https://example.com/a")
        resp.read.assert_not_called()

    def test_connected_public_peer_reads_body(self):
        resp = mock.Mock()
        resp.fp.raw._sock.getpeername.return_value = ("93.184.216.34", 443)
        resp.read = mock.Mock(return_value="<html><body>公网正文</body></html>".encode())
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=resp)
        ctx.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(d, "_feishu_url_allowed", return_value=True), \
                mock.patch.object(d._feishu_opener, "open", return_value=ctx):
            out = d._feishu_fetch_link_text("https://example.com/a")
        self.assertIn("公网正文", out)


if __name__ == "__main__":
    unittest.main()
