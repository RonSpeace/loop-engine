#!/usr/bin/env python3
"""#22 飞书多轮聊天建卡 —— L1 集成（真 board 实例 + fake 飞书 SDK + agent 轮次打桩）。

对应用例 78-82、112：
- 78 驱动 on_message → DB 出现 channel='feishu' 会话与事件（含前置对话回放）
- 79 去重：原文 user 事件恰好 1 条（回放不含原文）
- 80 刷新语义：重新 GET 列表 + 事件仍完整
- 81 双通道隔离：飞书活跃时网页仍可建同项目 web 会话
- 82 y/n 集成回归：真实看板下 y 回复仍生效
- 112 boot 时序：user 事件落在「会话已建立」system 事件之后

实现说明：**真 board 实例** = 本进程内起 werkzeug HTTP server（唯一端口、临时 db / materials /
artifacts），**agent 轮次打桩** = 用 FakeSession / FakeCreateServer 替换 board.ACPSession /
CreateRequirementMcpServer（**绝不真起 kimi acp**）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_feishu_chat_e2e -v
"""
import json
import shutil
import socket
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

CHAT = "oc_e2e"


# ==================== agent 轮次打桩（不真起 kimi acp） ====================

class FakeProc:
    pid = 424242

    def poll(self):
        return None


class FakeSession:
    """ACPSession 桩：start 即成功、prompt 立即返回（不驱动真实 agent）"""
    _n = 0

    def __init__(self, *a, **k):
        FakeSession._n += 1
        self.session_id = f"fake-sess-{FakeSession._n}"
        self.model_name = "fake-model"
        self.thinking = "off"
        self.proc = FakeProc()
        self.prompts = []
        self.closed = False

    def start(self, workdir, resume_session_id=None):
        return None

    def prompt(self, text, timeout=None):
        self.prompts.append(text)

    def _flush_message(self):
        pass

    def cancel(self):
        pass

    def close(self, kill=False):
        self.closed = True


class FakeCreateServer:
    url = "http://127.0.0.1:0/mcp"

    def __init__(self, *a, **k):
        pass

    def close(self):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get_json(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read())


def post_json(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


_MSG_SEQ = [0]     # #24：桩消息 message_id 自增唯一（去重键 = feishu_chat_id + message_id）


def up(text, chat_id=CHAT, msg_type="text", message_id=None, chat_type="p2p", content=None):
    if message_id is None:
        _MSG_SEQ[0] += 1
        message_id = f"om_{_MSG_SEQ[0]}"
    if content is None:
        content = json.dumps({"text": text}) if msg_type == "text" else json.dumps(content or {})
    elif not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return types.SimpleNamespace(event=types.SimpleNamespace(message=types.SimpleNamespace(
        message_type=msg_type, content=content, chat_id=chat_id,
        message_id=message_id, chat_type=chat_type)))


def fresh_state():
    return {"chat_id": CHAT, "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}, "feishu_chats": {}}


class FeishuChatE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b
        cls.tmp = Path(tempfile.mkdtemp(prefix="feishu-e2e-"))
        cls._old = (b.DB_PATH, b.MATERIALS_ROOT, b.ARTIFACTS_ROOT)
        b.DB_PATH = cls.tmp / "board.db"
        b.MATERIALS_ROOT = cls.tmp / "materials"
        b.ARTIFACTS_ROOT = cls.tmp / "artifacts"
        b.init_db()
        cls._patches = [
            mock.patch.object(b, "ACPSession", FakeSession),
            mock.patch.object(b, "CreateRequirementMcpServer", FakeCreateServer),
        ]
        for p in cls._patches:
            p.start()
        from werkzeug.serving import make_server
        cls.port = free_port()
        cls.srv = make_server("127.0.0.1", cls.port, b.app, threaded=True)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        for _ in range(50):
            try:
                get_json(cls.port, "/api/requirements")
                break
            except Exception:
                time.sleep(0.1)
        # dispatcher 指向测试板
        cls._d_orig = {n: getattr(d, n) for n in (
            "state", "save_state", "notify", "_feishu_enqueue", "feishu", "PROJECTS",
            "BOARD_API", "BOARD_ROOT_API", "spawn_agent", "send_text_chunked")}
        cls._created = []

    @classmethod
    def tearDownClass(cls):
        for sid in list(cls._created):
            post_json(cls.port, f"/api/chat/{sid}/close", {})
        cls.srv.shutdown()
        cls.srv.server_close()
        for p in cls._patches:
            p.stop()
        cls.b.DB_PATH, cls.b.MATERIALS_ROOT, cls.b.ARTIFACTS_ROOT = cls._old
        for n, v in cls._d_orig.items():
            setattr(d, n, v)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        d.state = fresh_state()
        d.save_state = lambda: None
        self.notifies = []
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d._feishu_enqueue = lambda cid, payload: d._feishu_handle(cid, payload)  # 同轮同步处理
        d.send_text_chunked = lambda cid, text, *a, **k: None
        d.feishu = mock.Mock()
        d.feishu.im.v1.message.create.return_value = types.SimpleNamespace(success=lambda: True)
        d.PROJECTS = {"loop-engine": {"paths": [str(REPO)]}}
        d.BOARD_API = f"http://127.0.0.1:{self.port}/api/requirements"
        d.BOARD_ROOT_API = f"http://127.0.0.1:{self.port}/api"
        d.spawn_agent = lambda *a, **k: None
        with self.b.chat_lock:
            for rt in list(self.b.chat_runtime.values()):
                self.b._close_chat_runtime(rt)

    def tearDown(self):
        # 关闭本测试遗留的飞书会话（同项目同通道 idle 也会让下次 POST /api/chat 409 → 用例互相污染）
        try:
            rows = get_json(self.port, "/api/chat?channel=feishu&limit=50")
        except Exception:
            return
        for r in rows:
            if r.get("status") in ("idle", "running", "created"):
                try:
                    post_json(self.port, f"/api/chat/{r['id']}/close", {})
                except Exception:
                    pass

    def drive(self, text, project_reply):
        # #24：无活跃旧建卡会话的普通文本已改由看板助手接管，进入旧建卡必须先发整行「新建建卡」
        d.on_message(up("新建建卡"))
        d.on_message(up(text))
        d.on_message(up(project_reply))
        rows = get_json(self.port, "/api/chat?channel=feishu&limit=20")
        if rows:
            self._created.append(rows[0]["id"])
        return rows

    # ---- 78 ----
    def test_78_real_board_persists_feishu_session(self):
        rows = self.drive("需求原文", "loop-engine")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["channel"], "feishu")
        self.assertEqual(rows[0]["project"], "loop-engine")
        events = get_json(self.port, f"/api/chat/{rows[0]['id']}/events")
        contents = [e["content"] for e in events]
        self.assertTrue(any("会话已建立" in c for c in contents))
        self.assertTrue(any(c.startswith("[飞书] 机器人：") for c in contents))
        self.assertTrue(any(c.startswith("[飞书] 用户：") for c in contents))

    # ---- 79 ----
    def test_79_no_duplicate_requirement_event(self):
        rows = self.drive("需求原文", "loop-engine")
        events = get_json(self.port, f"/api/chat/{rows[0]['id']}/events")
        dup = [e for e in events if "需求原文" in (e["content"] or "")]
        self.assertEqual(len(dup), 1)                          # 回放不含原文
        pre = [e for e in events if (e["content"] or "").startswith("[飞书] ")]
        self.assertEqual([e["kind"] for e in pre], ["agent", "user"])

    # ---- 112 ----
    def test_112_user_event_after_boot(self):
        rows = self.drive("需求原文", "loop-engine")
        events = get_json(self.port, f"/api/chat/{rows[0]['id']}/events")
        boot_i = next(i for i, e in enumerate(events) if "会话已建立" in (e["content"] or ""))
        user_i = next(i for i, e in enumerate(events) if "需求原文" in (e["content"] or ""))
        self.assertLess(boot_i, user_i)

    # ---- 80 ----
    def test_80_refresh_keeps_records(self):
        rows = self.drive("需求原文", "loop-engine")
        sid = rows[0]["id"]
        again = get_json(self.port, "/api/chat?channel=feishu&limit=20")
        self.assertIn(sid, [r["id"] for r in again])
        events = get_json(self.port, f"/api/chat/{sid}/events")
        self.assertGreaterEqual(len(events), 4)                # 创建中 + 会话已建立 + 回放2条 + user

    # ---- 81 ----
    def test_81_web_isolated_from_feishu(self):
        self.drive("需求原文", "loop-engine")                   # 飞书活跃会话
        web_rows = get_json(self.port, "/api/chat?project=loop-engine&channel=web")
        self.assertEqual(web_rows, [])                         # 网页看不到飞书会话
        code, body = post_json(self.port, "/api/chat", {"project": "loop-engine", "channel": "web"})
        self.assertEqual(code, 201, body)                      # 不被 409 互卡
        self._created.append(body["id"])

    # ---- 82 ----
    def test_82_yn_reply_still_works(self):
        code, req = post_json(self.port, "/api/requirements",
                              {"title": "e2e-yn", "project": "loop-engine", "notes": "x", "status": "later"})
        self.assertIn(code, (200, 201), req)
        rid = req["id"]
        try:
            d.post_dispatcher_event = d.post_dispatcher_event  # real
            d.state["pending_confirm"][str(rid)] = {"req": None, "question": "Q?"}
            d.on_message(up(f"y {rid}"))
            events = get_json(self.port, f"/api/requirements/{rid}/dispatcher-events")
            contents = [e["content"] for e in events]
            self.assertTrue(any("用户飞书回复：同意" in c for c in contents), contents)
        finally:
            req_del = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/requirements/{rid}", method="DELETE")
            try:
                urllib.request.urlopen(req_del, timeout=5)
            except Exception:
                pass

    # ---- ① 飞书侧确认建卡闭环（L1）----
    def test_83_feishu_confirm_builds_card(self):
        rows = self.drive("需求原文", "loop-engine")
        sid = rows[0]["id"]
        # 模拟本轮聊出草稿（board 正常流程由 _extract_draft 落库）
        with self.b.get_db() as conn:
            conn.execute("UPDATE chat_sessions SET draft_title=?, draft_notes=? WHERE id=?",
                         ("飞书确认的标题", "备注", sid))
            conn.commit()
        self.notifies.clear()
        d.on_message(up("再补一点"))                        # 一轮讨论 → 结束提示草稿已就绪
        self.assertTrue(any("回复『确认建卡』" in n for n in self.notifies), self.notifies)
        self.notifies.clear()
        d.on_message(up("确认建卡"))                        # 飞书确认 → 驱动 /confirm
        events = get_json(self.port, f"/api/chat/{sid}/events")
        contents = [e["content"] or "" for e in events]
        self.assertTrue(any("用户已确认草稿" in c for c in contents), contents)   # /confirm 真被调用
        # 确认后不再重复「回复确认建卡」提示
        self.assertFalse(any("回复『确认建卡』" in n for n in self.notifies))


if __name__ == "__main__":
    unittest.main()
