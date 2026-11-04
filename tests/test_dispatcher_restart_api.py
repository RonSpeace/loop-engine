#!/usr/bin/env python3
"""req-421 网页端「疑似卡死 → 重启调度器」（C 组）：POST /api/dispatcher/restart 测试。

服务端按红态口径复核（fail-closed）：会话 running + turn_step='llm' + 心跳与事件流双静默 > AGENT_STALL_SEC
才放行；与需求助手 tool 的有意差异是不做「有运行中 kimi agent 即拒」预检（卡死现场恰有一个活着的 agent）。

覆盖 9 例：
- C1 守卫矩阵：非 running / turn_step 非 llm / 心跳新鲜（10s 前）/ 四心跳全 NULL / 缺 confirm / confirm=false
  → 均 400 且 subprocess.run 零调用
- C2 红态放行：心跳与最近事件均 >180s 前 → 200；kickstart 命令行含 gui/<uid>/local.automation.req-board-dispatcher；
  来源标记文件 {"source": "web"}；响应含 restarted/health
- C3 留痕：成功后该需求 agent_events 新增一条 kind='system' 且 content 含「网页触发重启」
- C4 失败路径：kickstart 非 0 → 502 且错误文本透出；_restart_dispatcher 抛异常同样返回 JSON 错误体（非 500 空白页）
- C5 归属校验：agent_session_id 属于其它 req_id → 404 且零命令
- C6 来源标识合法：写入的 source 匹配 ^[a-zA-Z0-9_-]+$
- C7 事件流新鲜 → 拒：心跳旧但最近事件 5s 前（会话标记 / 无标记两种都算活性证据）→ 400
- C8 响应中 → 拒：last_llm_response_at 1s 前、last_llm_request_at 20min 前 → 400
- C9 思考中 → 拒：last_thought_at 30s 前、其余心跳 >180s 前 → 400

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_restart_api -v

隔离（#420）：临时目录 + monkeypatch board.DB_PATH（不写生产库）；subprocess.run 与
_reqchat_dispatcher_health 全 mock、RESTART_SOURCE_FILE 指向临时文件——本文件不拉起任何进程、不写真实标记文件。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# #8 标记隔离：REQ_BOARD_SUPERVISOR 随进程树进入测试进程（部署后 ambient 恒在）。本文件断言
# launchd 原路径（kickstart / 写标记容错），须钉住「无标记」基线——模块级 pop 覆盖
# pytest/unittest 双线（pytest 侧另有 conftest autouse 兜底）；需要 run_local 的用例在
# mock.patch.dict 块内显式置值。
os.environ.pop("REQ_BOARD_SUPERVISOR", None)

import board as b  # noqa: E402

# _reqchat_dispatcher_health 的成功替身（不真等 REQ_CHAT_HEALTH_WAIT 秒、不外呼 8899）
HEALTH_OK = {"heartbeat_updated": True, "health": {"ok": True, "age": 1},
             "detail": "心跳文件已更新；/api/dispatcher-health ok（age 1s）"}
KICKSTART_CMD = ["launchctl", "kickstart", "-k",
                 f"gui/{os.getuid()}/{b.REQ_CHAT_DISPATCHER_LABEL}"]


class FakeCompleted:
    """subprocess.run 返回值替身（与 test_req_chat_mcp.py 同款）"""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DispatcherRestartApiTest(unittest.TestCase):
    """POST /api/dispatcher/restart：守卫矩阵、红态放行、留痕与失败路径"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="req421-restart-")
        cls._orig_db = b.DB_PATH
        b.DB_PATH = Path(cls.tmp) / "test.db"
        b.init_db()
        b.app.config["TESTING"] = True
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 来源标记文件逐例独立（互不残留），且绝不写真实 data/dispatcher_restart_source.json
        self.src_file = Path(self.tmp) / f"restart_source_{self._testMethodName}.json"
        self._patches = [
            mock.patch.object(b, "RESTART_SOURCE_FILE", self.src_file),
            mock.patch.object(b, "_reqchat_dispatcher_health", return_value=dict(HEALTH_OK)),
            mock.patch.object(b.subprocess, "run", return_value=FakeCompleted()),
        ]
        for p in self._patches:
            p.start()
        self.mrun = b.subprocess.run
        with b.get_db() as conn:
            conn.execute("DELETE FROM agent_sessions")
            conn.execute("DELETE FROM agent_events")
            conn.execute("DELETE FROM requirements")
            conn.commit()
        self.req_id = self._new_req()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    # ---- 现场构造 ----
    def _new_req(self, title="重启测试"):
        resp = self.client.post("/api/requirements", json={
            "title": title, "project": "req-board", "status": "doing"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()["id"]

    def _mk_session(self, req_id=None, session_id=None, status="running", turn_step="llm",
                    detail=None, llm_request=None, llm_response=None, thought=None,
                    tool_started=None):
        """直插注册表行构造现场：注册表 POST 的 INSERT 语句不含 #421 六列（心跳只走「按 id 更新」
        路径写入），直插最贴近调度器写完心跳后的行形态；心跳值为 epoch 秒，None = 无该证据。"""
        rid = self.req_id if req_id is None else req_id
        sid = session_id or f"s_{rid}_{int(time.time() * 1000) % 10 ** 7}"
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO agent_sessions (req_id, role, session_id, status, turn_step,"
                " turn_step_detail, last_llm_request_at, last_llm_response_at,"
                " last_thought_at, last_tool_started_at)"
                " VALUES (?, 'dev', ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, sid, status, turn_step, detail, llm_request, llm_response,
                 thought, tool_started))
            conn.commit()
            return cur.lastrowid

    @staticmethod
    def _ago(seconds):
        return time.time() - seconds

    def _red_session(self, req_id=None, session_id=None):
        """红态现场：running + turn_step='llm' + 四心跳均 > AGENT_STALL_SEC 前（无事件 = 事件流静默）"""
        return self._mk_session(req_id=req_id, session_id=session_id,
                                llm_request=self._ago(300), llm_response=self._ago(290),
                                thought=self._ago(280), tool_started=self._ago(270))

    def _body(self, sid, **over):
        body = {"req_id": self.req_id, "agent_session_id": sid, "confirm": True}
        body.update(over)
        return body

    def _post_restart(self, body):
        return self.client.post("/api/dispatcher/restart", json=body)

    def _add_event(self, ts, content="事件", session_id=None, req_id=None, kind="system"):
        with b.get_db() as conn:
            conn.execute("INSERT INTO agent_events (req_id, kind, content, session_id, ts)"
                         " VALUES (?, ?, ?, ?, ?)",
                         (self.req_id if req_id is None else req_id, kind, content,
                          session_id, ts))
            conn.commit()

    def _events(self, req_id=None):
        with b.get_db() as conn:
            return conn.execute("SELECT * FROM agent_events WHERE req_id = ? ORDER BY id",
                                (self.req_id if req_id is None else req_id,)).fetchall()

    @staticmethod
    def _ts_str(seconds_ago):
        """agent_events.ts（北京时间字符串；#421 统一按 +08:00 解析成 epoch 毫秒）"""
        return (datetime.now(b.BEIJING_TZ) - timedelta(seconds=seconds_ago)) \
            .strftime('%Y-%m-%d %H:%M:%S')

    # ---- C1 ----
    def test_c1_guard_matrix_rejects_without_command(self):
        """任一守卫不满足 → 400 + 错误文本，且不执行任何命令（替身 launchctl 零调用）"""
        stale = {"llm_request": self._ago(300), "llm_response": self._ago(290),
                 "thought": self._ago(280), "tool_started": self._ago(270)}
        fresh = {"llm_request": self._ago(10), "llm_response": self._ago(10),
                 "thought": self._ago(10), "tool_started": self._ago(10)}
        cases = [
            ("非 running", self._mk_session(status="idle", **stale), True),
            ("turn_step 非 llm", self._mk_session(status="running", turn_step="tool", **stale), True),
            ("心跳 age=10s（<阈值）", self._mk_session(status="running", **fresh), True),
            ("四心跳全 NULL", self._mk_session(status="running", turn_step="llm"), True),
            ("缺 confirm", self._red_session(), None),        # None = 不传 confirm
            ("confirm 非 true", self._red_session(), "false"),
        ]
        for name, sid, confirm in cases:
            with self.subTest(name=name):
                body = self._body(sid)
                if confirm is None:
                    del body["confirm"]
                elif confirm == "false":
                    body["confirm"] = False
                resp = self._post_restart(body)
                self.assertEqual(resp.status_code, 400, f"{name}: {resp.get_data(as_text=True)}")
                self.assertTrue((resp.get_json() or {}).get("error"), f"{name}: 错误文本不应为空")
        self.mrun.assert_not_called()

    # ---- C2 ----
    def test_c2_red_state_restarts(self):
        """红态放行：双静默 > 阈值 → kickstart 调度器 + 来源标记 source=web + 响应带 health/restarted"""
        sid = self._red_session(session_id="s_red")
        self._add_event(self._ts_str(3600), content="一小时前的旧事件", session_id="s_red")
        resp = self._post_restart(self._body(sid))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        out = resp.get_json()
        self.assertTrue(out["restarted"])
        self.assertEqual(out["label"], b.REQ_CHAT_DISPATCHER_LABEL)
        self.assertEqual(out["health"], {"ok": True, "age": 1})
        self.assertTrue(out["heartbeat_updated"])
        self.assertTrue(out["detail"])
        self.mrun.assert_called_once()
        self.assertEqual(self.mrun.call_args.args[0], KICKSTART_CMD)
        src = json.loads(self.src_file.read_text())
        self.assertEqual(src["source"], "web")
        self.assertIsInstance(src["at"], float)

    # ---- C3 ----
    def test_c3_writes_event_trail(self):
        """留痕：重启端点先写 agent_events（kind='system'，content 含「网页触发重启」+ 会话标识）"""
        sid = self._red_session(session_id="s_trail")
        resp = self._post_restart(self._body(sid))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rows = self._events()
        self.assertEqual(len(rows), 1, f"应恰有一条重启留痕，实际 {[r['content'] for r in rows]}")
        self.assertEqual(rows[0]["kind"], "system")
        self.assertIn("网页触发重启", rows[0]["content"])
        self.assertIn("调度器", rows[0]["content"])
        self.assertIn("s_trail", rows[0]["content"])
        self.assertEqual(rows[0]["session_id"], "s_trail")

    # ---- C4 ----
    def test_c4_failure_paths_return_json_error(self):
        """失败路径：kickstart 非 0 → 502 + 错误文本透出；_restart_dispatcher 抛异常 → JSON 错误体（非 500 空白页）"""
        sid = self._red_session(session_id="s_fail")
        with mock.patch.object(b.subprocess, "run",
                               return_value=FakeCompleted(returncode=1, stderr=b"boom")):
            resp = self._post_restart(self._body(sid))
        self.assertEqual(resp.status_code, 502, resp.get_data(as_text=True))
        self.assertTrue(resp.is_json, "失败必须返回 JSON 错误体")
        err = (resp.get_json() or {}).get("error")
        self.assertTrue(err)
        self.assertIn("boom", err, "launchctl 的失败原因应透出给页面")
        # 同一现场再试一次（先清掉上一次的留痕事件——它是「最新活性证据」，会被 180s 静默守卫拦下）
        with b.get_db() as conn:
            conn.execute("DELETE FROM agent_events WHERE req_id = ?", (self.req_id,))
            conn.commit()
        with mock.patch.object(b, "_restart_dispatcher",
                               side_effect=ValueError("重启调度器失败: 模拟异常")):
            resp2 = self._post_restart(self._body(sid))
        self.assertEqual(resp2.status_code, 502, resp2.get_data(as_text=True))
        self.assertTrue(resp2.is_json, "异常路径不得回落成 500 空白/HTML 页")
        self.assertIn("模拟异常", (resp2.get_json() or {}).get("error", ""))

    # ---- C5 ----
    def test_c5_ownership_404(self):
        """归属校验：agent_session_id 属于其它 req_id → 404 且零命令"""
        other_req = self._new_req(title="别的卡")
        other_sid = self._red_session(req_id=other_req, session_id="s_other")
        resp = self._post_restart(self._body(other_sid))   # req_id 为 self.req_id，会话属 other_req
        self.assertEqual(resp.status_code, 404, resp.get_data(as_text=True))
        self.assertTrue((resp.get_json() or {}).get("error"))
        self.mrun.assert_not_called()

    # ---- C6 ----
    def test_c6_source_token_is_safe(self):
        """来源标识：写入 source 匹配 ^[a-zA-Z0-9_-]+$（该文件会被调度器日志/角标消费，取值须安全）"""
        sid = self._red_session(session_id="s_src")
        with mock.patch.object(b, "_restart_dispatcher", wraps=b._restart_dispatcher) as m:
            resp = self._post_restart(self._body(sid))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(m.call_args.args[0], "web")
        source = json.loads(self.src_file.read_text())["source"]
        self.assertRegex(source, r"^[a-zA-Z0-9_-]+$")
        self.assertTrue(re.fullmatch(r"[a-zA-Z0-9_-]+", source))
        self.assertEqual(source, "web")

    # ---- C7 ----
    def test_c7_fresh_event_stream_rejects(self):
        """事件流新鲜：心跳虽旧，最近事件 5s 前 → 400（心跳+事件双静默缺一不可）"""
        sid = self._red_session(session_id="s_ev")
        self._add_event(self._ts_str(5), content="正在输出", session_id="s_ev")
        resp = self._post_restart(self._body(sid))
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("最近活性证据", (resp.get_json() or {}).get("error", ""))
        # 历史无标记事件（session_id IS NULL，过渡期统一归入）同样计为活性证据
        with b.get_db() as conn:
            conn.execute("DELETE FROM agent_events WHERE req_id = ?", (self.req_id,))
            conn.commit()
        sid2 = self._red_session(session_id="s_ev2")
        self._add_event(self._ts_str(5), content="无标记事件", session_id=None)
        resp2 = self._post_restart(self._body(sid2))
        self.assertEqual(resp2.status_code, 400, resp2.get_data(as_text=True))
        self.assertIn("最近活性证据", (resp2.get_json() or {}).get("error", ""))
        self.mrun.assert_not_called()

    # ---- C8 ----
    def test_c8_recent_response_rejects(self):
        """响应中：last_llm_response_at 1s 前 → 400（即使请求心跳 20min 前）"""
        sid = self._mk_session(session_id="s_beat", llm_request=self._ago(1200),
                               llm_response=self._ago(1))
        resp = self._post_restart(self._body(sid))
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("最近活性证据", (resp.get_json() or {}).get("error", ""))
        self.mrun.assert_not_called()

    # ---- C9 ----
    def test_c9_recent_thought_rejects(self):
        """思考中：last_thought_at 30s 前 → 400（思考流仍活）"""
        sid = self._mk_session(session_id="s_think", thought=self._ago(30),
                               llm_request=self._ago(300), llm_response=self._ago(290),
                               tool_started=self._ago(280))
        resp = self._post_restart(self._body(sid))
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("最近活性证据", (resp.get_json() or {}).get("error", ""))
        self.mrun.assert_not_called()


if __name__ == "__main__":
    unittest.main()
