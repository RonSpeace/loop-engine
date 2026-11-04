#!/usr/bin/env python3
"""req-120 调度历史「拉起会话」记录展示可点击 session id 测试。

改动：
- board.py：dispatcher_events 增加 session_id 列（ALTER 迁移，历史行 NULL 不回填）；
  GET 时间线返回 session_id；POST 支持 session_id 参数并返回新记录 id；
  新增 PATCH /dispatcher-events/<event_id> 回填端点（幂等，404=不存在/跨需求，400=空值）。
- dispatcher.py：post_dispatcher_event 返回记录 id；续跑 spawn 当场带旧 session_id；
  新会话 spawn 先写记录拿 id，run_agent 会话建立后按 id 回填（失败仅降级）。

- L0（默认运行）：mock 副作用，覆盖 post_dispatcher_event 返回 id / spawn_agent 两条路径
  （续跑当场带 sid、新会话传 event id）/ run_agent 回填与降级。
- E2E（测试板实例在跑时才运行）：真实看板落库，覆盖迁移列存在、POST 返回 id、
  PATCH 回填/幂等/空值 400/跨需求 404、POST 带 session_id（续跑当场写路径）。

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

E2E 需先启动测试板实例（测试端口，避免与 main 8899 冲突；DB 为该 worktree 的 data/board.db）：
    REQ_BOARD_PORT=8902 .venv/bin/python board.py

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import socket
import sqlite3
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

E2E_PORT = int(os.environ.get("REQ120_E2E_PORT", "8902"))
# E2E 直连本需求自启的测试板（新代码），不依赖 dispatcher.BOARD_API——
# 全量 discover 时 dispatcher 的 BOARD_API 由最先 import 的模块决定（默认指向 8899 旧实例）
E2E_API = f"http://127.0.0.1:{E2E_PORT}/api/requirements"

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


class L0DispatcherEventSession(unittest.TestCase):
    """L0 单测：mock 副作用，校验返回 id / spawn 两路径 / run_agent 回填与降级"""

    # 本类会替换的 dispatcher 模块属性，tearDown 统一恢复（防污染同进程后续测试）
    _MUTATED = ("state", "save_state", "api", "post_dispatcher_event", "set_req",
                "build_prompt", "threading", "ACPSession", "post_event", "kimi_version",
                "handle_result", "find_resume_sid", "update_session_info", "post_run_record",
                "sync_agent_session")

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in self._MUTATED}
        d.state = {"sessions": {}, "retries": {}, "pending_confirm": {},
                   "pending_route": {}, "pending_review": {}, "worktrees": {}}
        d.save_state = lambda: None
        self.calls = []  # api 调用记录 (method, path, body)

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)

    # ---- L1：post_dispatcher_event 返回 id ----
    def test_l1_returns_event_id(self):
        """验收 1：POST 响应带 id 时返回 id；body 透传 session_id"""
        d.api = lambda method, path, body=None: self.calls.append((method, path, body)) or {"success": True, "id": 42}
        rid = d.post_dispatcher_event(7, "拉起「开发」会话（新会话）", kind="spawn")
        self.assertEqual(rid, 42)
        method, path, body = self.calls[0]
        self.assertEqual((method, path), ("POST", "/7/dispatcher-events"))
        self.assertEqual(body["events"][0]["kind"], "spawn")
        self.assertEqual(body["events"][0]["session_id"], None)

        rid2 = d.post_dispatcher_event(7, "拉起「开发」会话（续跑）", kind="spawn", session_id="old-sid")
        self.assertEqual(rid2, 42)
        self.assertEqual(self.calls[1][2]["events"][0]["session_id"], "old-sid")

    def test_l1_no_id_on_old_board_or_failure(self):
        """旧 board 不返回 id / api 抛异常 → 返回 None（不回填路径，等同现状）"""
        d.api = lambda method, path, body=None: {"success": True}
        self.assertIsNone(d.post_dispatcher_event(7, "x"))
        d.api = mock.Mock(side_effect=RuntimeError("board down"))
        self.assertIsNone(d.post_dispatcher_event(7, "x"))

    # ---- L2/L3：spawn_agent 两条路径 ----
    def _spawn_setup(self, event_ids):
        """公共 mock：set_req/build_prompt 空转，post_dispatcher_event 捕获参数并返回 event_ids 队列"""
        d.set_req = lambda *a, **k: None
        d.build_prompt = lambda *a, **k: "prompt"
        self.posted = []  # (content, kind, session_id)
        self.post_dispatcher_event = d.post_dispatcher_event
        d.post_dispatcher_event = lambda rid, content, kind="action", session_id=None: (
            self.posted.append((content, kind, session_id)) or (event_ids.pop(0) if event_ids else None))
        self.thread_calls = []
        d.threading = mock.MagicMock()
        d.threading.Thread = lambda target, args, daemon: self.thread_calls.append((target, args)) or mock.MagicMock()

    def test_l2_resume_spawn_carries_old_sid(self):
        """验收 2：续跑拉起当场带旧 session id（不经过回填路径，spawn_event_id=None）"""
        self._spawn_setup([])
        d.state["sessions"]["dev:90"] = {"id": "old-sid"}
        d.spawn_agent({"id": 90, "project": "req-board", "title": "t", "mode": "semi", "phase": "dev", "flow": "standard"}, role="dev", resume=True)
        content, kind, sid = self.posted[0]
        self.assertEqual(kind, "spawn")
        self.assertIn("续跑", content)
        self.assertEqual(sid, "old-sid")
        target, args = self.thread_calls[0]
        self.assertIsNone(args[6])  # spawn_event_id

    def test_l3_new_spawn_passes_event_id_to_thread(self):
        """验收 1：新会话先写记录拿 id，event id 经线程参数传给 run_agent"""
        self._spawn_setup([7])
        d.spawn_agent({"id": 90, "project": "req-board", "title": "t", "mode": "semi", "phase": "dev", "flow": "standard"}, role="dev")
        content, kind, sid = self.posted[0]
        self.assertEqual(kind, "spawn")
        self.assertIn("新会话", content)
        self.assertIsNone(sid)  # 新会话：当场无 sid
        target, args = self.thread_calls[0]
        self.assertEqual(target, d.run_agent)
        self.assertEqual(args[6], 7)  # spawn_event_id

    def test_l3_resume_no_record_falls_back_to_new(self):
        """边界：resume=True 但查无旧 id → 走新会话路径（不串号）"""
        self._spawn_setup([9])
        d.spawn_agent({"id": 90, "project": "req-board", "title": "t", "mode": "semi", "phase": "dev", "flow": "standard"}, role="dev", resume=True)
        content, kind, sid = self.posted[0]
        self.assertEqual(kind, "spawn")
        self.assertIn("新会话", content)
        self.assertIsNone(sid)
        self.assertEqual(self.thread_calls[0][1][6], 9)

    # ---- L4：run_agent 会话建立后回填 ----
    def _run_agent_env(self):
        d.api = lambda method, path, body=None: self.calls.append((method, path, body)) or {"success": True}
        d.post_event = lambda *a, **k: None
        d.kimi_version = lambda: None
        d.handle_result = mock.Mock()
        d.find_resume_sid = lambda req, role=None: None
        d.update_session_info = lambda req, key=None, **f: None
        d.post_run_record = lambda *a, **k: None
        # #279 后补：sync_agent_session 走真实 api_board（模块级，未被 d.api mock 覆盖），
        # 不隔离会真实写入 main 看板 agent_sessions（此前每次跑 L4 都会污染 req-90 的
        # new-sid 测试记录，updated_at 被刷新）；会话注册表同步非本类验收点，置空
        d.sync_agent_session = lambda *a, **k: None
        s = mock.Mock()
        s.proc.pid = 12345
        s.session_id = "new-sid"
        s.start.return_value = False
        s.collect_usage.return_value = None
        s.proc.returncode = None
        d.ACPSession = lambda *a, **k: s
        return s

    def test_l4_backfill_after_session_start(self):
        """验收 1：start() 成功后按 event id 回填 session_id（PATCH 定位）"""
        s = self._run_agent_env()
        d.run_agent({"id": 90, "project": "req-board", "title": "t", "mode": "semi", "phase": "dev", "flow": "standard"}, "dev", "p", 0.0, "", False, spawn_event_id=7)
        patch_calls = [(m, p, b) for m, p, b in self.calls if m == "PATCH"]
        self.assertEqual(len(patch_calls), 1)
        self.assertEqual(patch_calls[0][1], "/90/dispatcher-events/7")
        self.assertEqual(patch_calls[0][2], {"session_id": "new-sid"})
        s.start.assert_called_once()

    def test_l4_no_event_id_skips_backfill(self):
        """无 spawn_event_id（续跑/旧端）→ 不回填、无 PATCH 调用"""
        self._run_agent_env()
        d.run_agent({"id": 90, "project": "req-board", "title": "t", "mode": "semi", "phase": "dev", "flow": "standard"}, "dev", "p", 0.0, "", True, None)
        self.assertFalse([c for c in self.calls if c[0] == "PATCH"])

    def test_l4_backfill_failure_only_degrades(self):
        """回填失败仅降级不阻断：后续流程（收尾记录/结果处理）正常执行"""
        s = self._run_agent_env()
        d.handle_result = mock.Mock()
        def flaky_api(method, path, body=None):
            if method == "PATCH":
                raise RuntimeError("board down")
            self.calls.append((method, path, body))
            return {"success": True}
        d.api = flaky_api
        d.run_agent({"id": 90, "project": "req-board", "title": "t", "mode": "semi", "phase": "dev", "flow": "standard"}, "dev", "p", 0.0, "", False, spawn_event_id=7)
        d.handle_result.assert_called_once()  # 会话流程未被阻断


def _board_up():
    try:
        with socket.create_connection(("127.0.0.1", E2E_PORT), timeout=1):
            return True
    except OSError:
        return False


@unittest.skipUnless(_board_up(), f"测试板实例未运行（REQ_BOARD_PORT={E2E_PORT}），跳过 E2E")
class E2EDispatcherEventSession(unittest.TestCase):
    """E2E：真实看板落库，校验迁移/返回 id/回填/幂等/校验/续跑当场写"""

    @classmethod
    def setUpClass(cls):
        cls.created = []  # 待清理的需求 id
        cls.db_path = REPO / "data" / "board.db"

    @classmethod
    def tearDownClass(cls):
        for rid in cls.created:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    E2E_API + f"/{rid}", method="DELETE"), timeout=5)
            except Exception:
                pass

    def new_req(self):
        body = json.dumps({"title": "req120-e2e", "project": "req-board", "notes": "测试", "status": "open"})
        req = urllib.request.Request(E2E_API, method="POST", data=body.encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        rid = data["id"]
        self.created.append(rid)
        return rid

    def get_events(self, rid):
        url = E2E_API + f"/{rid}/dispatcher-events"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())

    def post_event(self, rid, content, kind="action", session_id=None):
        body = json.dumps({"events": [{"kind": kind, "content": content, "session_id": session_id}]})
        req = urllib.request.Request(E2E_API + f"/{rid}/dispatcher-events", method="POST",
                                     data=body.encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def patch_event(self, rid, event_id, session_id):
        body = json.dumps({"session_id": session_id})
        req = urllib.request.Request(E2E_API + f"/{rid}/dispatcher-events/{event_id}", method="PATCH",
                                     data=body.encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_e1_migration_column(self):
        """验收 4：dispatcher_events 含 session_id 列（迁移生效）；非 spawn 历史行不受影响"""
        with sqlite3.connect(str(self.db_path)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatcher_events)")}
            self.assertIn("session_id", cols)
            # 迁移只加列：非 spawn 行（历史/其它测试写入）session_id 必须为 NULL；
            # spawn 行允许存在本类多次运行回填的残留（只增不删，不做全表 NULL 断言）
            rows = conn.execute(
                "SELECT id, session_id FROM dispatcher_events WHERE kind != 'spawn'").fetchall()
            for r in rows:
                if r[1] is not None:
                    self.fail(f"非 spawn 历史行 {r[0]} session_id 应为 NULL，实际 {r[1]!r}")

    def test_e2_post_returns_id_and_null_sid(self):
        """验收 1：POST 返回新记录 id（int）；GET 列表含该记录且 session_id 为 null"""
        rid = self.new_req()
        resp = self.post_event(rid, "拉起测试会话", kind="spawn")
        self.assertIsInstance(resp.get("id"), int)
        ev = [e for e in self.get_events(rid) if e["content"] == "拉起测试会话"][0]
        self.assertEqual(ev["id"], resp["id"])
        self.assertEqual(ev["kind"], "spawn")
        self.assertIsNone(ev["session_id"])

    def test_e3_patch_backfill(self):
        """验收 1：PATCH 回填成功，GET 显示该 session id"""
        rid = self.new_req()
        resp = self.post_event(rid, "拉起回填会话", kind="spawn")
        ev_id = resp["id"]
        self.assertEqual(self.patch_event(rid, ev_id, "sid-1"), {"success": True})
        ev = [e for e in self.get_events(rid) if e["id"] == ev_id][0]
        self.assertEqual(ev["session_id"], "sid-1")

    def test_e4_patch_idempotent(self):
        """验收 3：重复 PATCH 同一 id（同值/新值）均 200，最终值为最后一次"""
        rid = self.new_req()
        ev_id = self.post_event(rid, "拉起幂等会话", kind="spawn")["id"]
        self.patch_event(rid, ev_id, "sid-a")
        self.patch_event(rid, ev_id, "sid-a")
        self.patch_event(rid, ev_id, "sid-b")
        ev = [e for e in self.get_events(rid) if e["id"] == ev_id][0]
        self.assertEqual(ev["session_id"], "sid-b")

    def test_e5_patch_empty_sid_400(self):
        """验收 5：session_id 缺失/全空白 → 400"""
        rid = self.new_req()
        ev_id = self.post_event(rid, "拉起空值会话", kind="spawn")["id"]
        for bad in [{}, {"session_id": ""}, {"session_id": "   "}]:
            req = urllib.request.Request(E2E_API + f"/{rid}/dispatcher-events/{ev_id}", method="PATCH",
                                         data=json.dumps(bad).encode(),
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)

    def test_e6_patch_missing_or_cross_req_404(self):
        """验收 3：事件不存在 / 跨需求（req_id 不匹配防串号）→ 404"""
        rid = self.new_req()
        other = self.new_req()
        ev_id = self.post_event(rid, "拉起跨需求会话", kind="spawn")["id"]
        req = urllib.request.Request(E2E_API + f"/{rid}/dispatcher-events/999999", method="PATCH",
                                     data=json.dumps({"session_id": "x"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)
        req = urllib.request.Request(E2E_API + f"/{other}/dispatcher-events/{ev_id}", method="PATCH",
                                     data=json.dumps({"session_id": "x"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_e7_post_with_session_id(self):
        """验收 2：POST 带 session_id（续跑当场写路径）→ GET 显示该 sid"""
        rid = self.new_req()
        resp = self.post_event(rid, "拉起续跑会话", kind="spawn", session_id="old-sid")
        ev = [e for e in self.get_events(rid) if e["content"] == "拉起续跑会话"][0]
        self.assertEqual(ev["id"], resp["id"])
        self.assertEqual(ev["session_id"], "old-sid")
