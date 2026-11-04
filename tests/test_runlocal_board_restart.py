#!/usr/bin/env python3
r"""req-8 run_local 直跑模式：看板重启降级 + 本地重启链路（B 组）。

覆盖 PRD 验收 3（看板侧）与 board.py 直跑分支：
- B1  本地重启成功：心跳 pid 校验 → 写标记 → SIGTERM → boot_at 变化 → {restarted:True}；launchctl 零调用
- B2  pid 校验（含空格路径两形态正/反例）+ 本地分支 fail-closed（零信号零标记）
- B3  写标记失败 fail-closed：ValueError 中止、os.kill 零调用（不移植 launchd 容错）
- B4  健康核对防误报：boot_at 不变（心跳在更新）→ 超时 ValueError「未完成自重启」
- B5  网页端点集成（run_local 分支真实链路）：红态种子 → 200 + restarted、留痕恰一条；失败 502 JSON
- B6  reqchat：_dispatcher_pid 直跑回退 + 预检命中拒绝 + 预检通过事件文案模式中性（无 launchctl 字样）
- B7  看板降级：run_local → launchctl 零调用、事件文案=PRD 原文、心跳 board_restart_pending、无失败事件
- B8  /api/dispatcher-health 透出 board_restart_pending（at > BOARD_STARTED_AT 才返回）
- B9  前端接线源码锁（refreshHealth 消费字段 + .health.warn 样式 + 文案 token）

运行（worktree 内，用 main 工作区 .venv；标记隔离见 conftest autouse fixture）：
    .venv/bin/python -m pytest tests/test_runlocal_board_restart.py -q

隔离（#420/#343）：REQ_BOARD_API 缺省不可达；心跳/来源标记/DB 指临时目录；不真调 os.execv、
launchctl、os.kill（全替身）。
"""
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 防误连生产 8899（#343）
os.environ.pop("REQ_BOARD_SUPERVISOR", None)  # 钉无标记基线（unittest 线；pytest 侧另有 conftest）

import board as b  # noqa: E402
import dispatcher as d  # noqa: E402
from tests._frontend_src import frontend_src  # noqa: E402

MARKER = "REQ_BOARD_SUPERVISOR"
RUN_LOCAL = "run_local"
PRD_TEXT = "配置已合并，重启看板后生效；直跑模式请手动重启（Ctrl-C 后重跑 run_local.sh）"

# 已知全局污染（沿 tests/test_sse_stream.py 同款处理）：test_role_guard / test_dispatcher_events_session
# 把共享 threading.Thread 打桩且未还原；本文件 import 期（collection，污染尚未发生）捕获真身，
# 建 MCP server 前复位——否则 serve_forever 线程不启动，srv.close() 里 shutdown() 永久等待
# （全量顺序下挂死；定向单跑不复现）。
_REAL_THREAD = threading.Thread


class FakeCompleted:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeResp:
    def __init__(self, payload=b"{}"):
        self._data = payload

    def read(self, n=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class LocalRestartChainTest(unittest.TestCase):
    """B1/B2/B3/B4：_restart_dispatcher_local 链路"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.hb = t / "hb.json"
        self.src = t / "src.json"
        self.pid = 4242
        self._patches = [
            mock.patch.object(b, "HEARTBEAT_FILE", self.hb),
            mock.patch.object(b, "RESTART_SOURCE_FILE", self.src),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.notes = []
        self.kills = []

    def _seed_hb(self, boot_at=1000.0, pid=None):
        self.hb.write_text(json.dumps(
            {"last_poll_at": time.time(), "pid": self.pid if pid is None else pid, "boot_at": boot_at}))

    def _ps_ok(self, command="/usr/bin/python3 dispatcher.py"):
        return mock.patch.object(b.subprocess, "run",
                                 return_value=FakeCompleted(stdout=(command + "\n").encode()))

    def _kill_updates_boot(self, new_boot=2000.0):
        def fake_kill(pid, sig):
            self.kills.append((pid, sig))
            if sig != signal.SIGTERM:
                return   # os.kill(pid, 0) 存活探测不改心跳
            rec = json.loads(self.hb.read_text())
            rec["boot_at"] = new_boot
            rec["last_poll_at"] = time.time()
            self.hb.write_text(json.dumps(rec))
        return fake_kill

    @staticmethod
    def _sigterm_calls(m_kill):
        return [c for c in m_kill.call_args_list if c.args[1] == signal.SIGTERM]

    def test_b1_local_restart_success(self):
        """成功链路：pid 校验通过 → 写标记 {source:web} → SIGTERM → boot_at 变化 → restarted；launchctl 零调用"""
        self._seed_hb()
        cmds = []

        def fake_run(cmd, **kw):
            cmds.append(cmd)
            return FakeCompleted(stdout=b"/usr/bin/python3 dispatcher.py\n")

        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b, "subprocess", mock.Mock(run=mock.Mock(side_effect=fake_run))), \
             mock.patch.object(b.os, "kill", side_effect=self._kill_updates_boot()), \
             mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(b'{"ok": true, "age": 1}')):
            res = b._restart_dispatcher("web", note=self.notes.append)
        self.assertTrue(res["restarted"])
        self.assertEqual(res["label"], "run_local")
        sigterms = [k for k in self.kills if k[1] == signal.SIGTERM]
        self.assertEqual(sigterms, [(self.pid, signal.SIGTERM)])
        marker = json.loads(self.src.read_text())
        self.assertEqual(marker["source"], "web")
        self.assertTrue(all("launchctl" not in " ".join(cmd) for cmd in cmds), f"launchctl 被调用: {cmds}")

    def test_b2_pid_validation_two_forms_and_negatives(self):
        """末 token 口径两形态均通过；缺 pid / 进程死 / 末 token 非 dispatcher.py → None"""
        # 形态 1：相对脚本路径（run_local 首启）
        self._seed_hb()
        with mock.patch.object(b.subprocess, "run",
                               return_value=FakeCompleted(stdout=b"/usr/bin/python3 dispatcher.py\n")), \
             mock.patch.object(b.os, "kill"):
            self.assertEqual(b._dispatcher_pid_heartbeat(), self.pid)
        # 形态 2：含空格绝对路径（exec 自重启后 argv —— 旧 $2 口径会截断误拒）
        exec_form = ('/Users/x/Loop Engine/Main Repo/.venv/bin/python '
                     '/Users/x/Loop Engine/Main Repo/dispatcher.py')
        with mock.patch.object(b.subprocess, "run",
                               return_value=FakeCompleted(stdout=(exec_form + "\n").encode())), \
             mock.patch.object(b.os, "kill"):
            self.assertEqual(b._dispatcher_pid_heartbeat(), self.pid)
        # 反例：心跳缺 pid
        self._seed_hb(pid=None)  # None 会被 _seed_hb 替换为默认 pid，改直接写
        self.hb.write_text(json.dumps({"last_poll_at": time.time(), "boot_at": 1.0}))
        self.assertIsNone(b._dispatcher_pid_heartbeat())
        # 反例：进程不存在
        self._seed_hb()
        with mock.patch.object(b.os, "kill", side_effect=ProcessLookupError()):
            self.assertIsNone(b._dispatcher_pid_heartbeat())
        # 反例：末 token 非 dispatcher.py（陈旧/复用 pid 场景）
        self._seed_hb()
        with mock.patch.object(b.subprocess, "run",
                               return_value=FakeCompleted(stdout=b"/usr/bin/python3 board.py\n")), \
             mock.patch.object(b.os, "kill"):
            self.assertIsNone(b._dispatcher_pid_heartbeat())
        # 本地重启 fail-closed：定位失败 → ValueError + 零信号零标记
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b, "_dispatcher_pid_heartbeat", return_value=None), \
             mock.patch.object(b.os, "kill") as m_kill:
            with self.assertRaises(ValueError) as ctx:
                b._restart_dispatcher_local("web")
        m_kill.assert_not_called()
        self.assertFalse(self.src.exists())
        self.assertIn("run_local", str(ctx.exception))

    def test_b3_marker_write_fail_aborts_without_signal(self):
        """写标记失败 → ValueError 中止、os.kill 零调用（直跑不移植 launchd 写失败容错）"""
        self._seed_hb()
        dir_as_file = Path(self.tmp.name) / "marker_is_dir"
        dir_as_file.mkdir()
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b, "RESTART_SOURCE_FILE", dir_as_file), \
             mock.patch.object(b, "subprocess", mock.Mock(run=mock.Mock(
                 return_value=FakeCompleted(stdout=b"/usr/bin/python3 dispatcher.py\n")))), \
             mock.patch.object(b.os, "kill") as m_kill:
            with self.assertRaises(ValueError) as ctx:
                b._restart_dispatcher_local("web")
        self.assertEqual(self._sigterm_calls(m_kill), [])   # 零信号（存活探测 os.kill(pid,0) 不算信号）
        self.assertIn("写重启来源标记失败", str(ctx.exception))

    def test_b4_boot_at_unchanged_times_out(self):
        """boot_at 不变（心跳在更新）→ 超时 ValueError「未完成自重启」（防旧进程存活误报成功）"""
        self._seed_hb(boot_at=1000.0)

        def fake_kill(pid, sig):
            self.kills.append((pid, sig))
            if sig != signal.SIGTERM:
                return
            rec = json.loads(self.hb.read_text())
            rec["last_poll_at"] = time.time() + 1   # 心跳持续更新但 boot_at 不变
            self.hb.write_text(json.dumps(rec))

        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b, "REQ_CHAT_HEALTH_WAIT", 1), \
             mock.patch.object(b.subprocess, "run",
                               return_value=FakeCompleted(stdout=b"/usr/bin/python3 dispatcher.py\n")), \
             mock.patch.object(b.os, "kill", side_effect=fake_kill), \
             mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(b'{"ok": true, "age": 1}')):
            with self.assertRaises(ValueError) as ctx:
                b._restart_dispatcher_local("web")
        self.assertIn("完成 exec 自重启", str(ctx.exception))
        self.assertIn("boot_at 未变化", str(ctx.exception))


class WebEndpointLocalTest(unittest.TestCase):
    """B5：POST /api/dispatcher/restart 在 run_local 下走本地分支（真实链路 + 留痕恰一条）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req8-b5-"))
        cls._orig_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.hb = self.tmp / f"hb_{self._testMethodName}.json"
        self.src = self.tmp / f"src_{self._testMethodName}.json"
        self._patches = [
            mock.patch.object(b, "HEARTBEAT_FILE", self.hb),
            mock.patch.object(b, "RESTART_SOURCE_FILE", self.src),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        with b.get_db() as conn:
            for table in ("agent_sessions", "agent_events", "requirements"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        resp = self.client.post("/api/requirements", json={
            "title": "B5", "project": "req-board", "status": "doing"})
        self.req_id = resp.get_json()["id"]
        sid = self._mk_red_session()
        self.body = {"req_id": self.req_id, "agent_session_id": sid, "confirm": True}

    def _mk_red_session(self):
        ago = lambda s: time.time() - s
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO agent_sessions (req_id, role, session_id, status, turn_step,"
                " last_llm_request_at, last_llm_response_at, last_thought_at, last_tool_started_at)"
                " VALUES (?, 'dev', 's_b5', 'running', 'llm', ?, ?, ?, ?)",
                (self.req_id, ago(300), ago(290), ago(280), ago(270)))
            conn.commit()
            return cur.lastrowid

    def _events_n(self):
        with b.get_db() as conn:
            return conn.execute("SELECT COUNT(*) c FROM agent_events WHERE req_id = ?",
                                (self.req_id,)).fetchone()["c"]

    def _boot_kill(self, new_boot):
        def fake_kill(pid, sig):
            if sig != signal.SIGTERM:
                return   # os.kill(pid, 0) 存活探测不改心跳
            rec = json.loads(self.hb.read_text())
            rec["boot_at"] = new_boot
            self.hb.write_text(json.dumps(rec))
        return fake_kill

    def test_b5a_success_200_and_single_event(self):
        self.hb.write_text(json.dumps({"last_poll_at": time.time(), "pid": 4242, "boot_at": 1000.0}))
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b.subprocess, "run",
                               return_value=FakeCompleted(stdout=b"/usr/bin/python3 dispatcher.py\n")), \
             mock.patch.object(b.os, "kill", side_effect=self._boot_kill(2000.0)), \
             mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(b'{"ok": true, "age": 1}')):
            resp = self.client.post("/api/dispatcher/restart", json=self.body)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["restarted"])
        self.assertEqual(data["label"], "run_local")
        self.assertEqual(self._events_n(), 1)   # 留痕恰一条（#421：成功后才写）

    def test_b5b_failure_502_json(self):
        """本地分支失败（心跳缺 pid → fail-closed）→ 502 + JSON 错误体（非 500 空白页）"""
        self.hb.write_text(json.dumps({"last_poll_at": time.time(), "boot_at": 1000.0}))
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b.subprocess, "run") as m_run:
            resp = self.client.post("/api/dispatcher/restart", json=self.body)
        self.assertEqual(resp.status_code, 502, resp.get_data(as_text=True))
        self.assertTrue((resp.get_json() or {}).get("error"))
        m_run.assert_not_called()
        self.assertEqual(self._events_n(), 0)   # 失败不写「重启发起」留痕


class DispatcherPidFallbackTest(unittest.TestCase):
    """B6 前半：_dispatcher_pid 直跑回退（launchd 路径不变）"""

    def test_b6a_run_local_falls_back_to_heartbeat(self):
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b, "_dispatcher_pid_heartbeat", return_value=42) as m_hb, \
             mock.patch.object(b.subprocess, "run") as m_run:
            self.assertEqual(b._dispatcher_pid(), 42)
        m_hb.assert_called_once()
        m_run.assert_not_called()   # 不调 launchctl

    def test_b6b_unmarked_uses_launchctl(self):
        os.environ.pop(MARKER, None)
        with mock.patch.object(b.subprocess, "run",
                               return_value=FakeCompleted(stdout=b"\tpid = 4242\n")), \
             mock.patch.object(b, "_dispatcher_pid_heartbeat") as m_hb:
            self.assertEqual(b._dispatcher_pid(), 4242)
        m_hb.assert_not_called()


class ReqchatNeutralWordingTest(unittest.TestCase):
    """B6 后半：reqchat 预检拒绝 / 放行文案模式中性"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req8-b6-"))
        cls._orig_db = b.DB_PATH
        cls._orig_art = b.ARTIFACTS_ROOT
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        b.ARTIFACTS_ROOT = cls.tmp / "artifacts"
        b.ARTIFACTS_ROOT.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        b.ARTIFACTS_ROOT = cls._orig_art
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            for table in ("req_chat_events", "req_chats", "requirements"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        req = b.req_add("B6", "req-board", notes="", status="later")
        self.chat_id = b._reqchat_create(req["id"])
        if threading.Thread is not _REAL_THREAD:
            threading.Thread = _REAL_THREAD   # 复位被其它测试模块污染的全局（见 _REAL_THREAD 注释）
        self.srv = b.MaintainMcpServer(self.chat_id, req["id"])
        self.addCleanup(self.srv.close)

    def _events(self):
        with b.get_db() as conn:
            return [r["content"] for r in conn.execute(
                "SELECT content FROM req_chat_events WHERE req_chat_id = ?", (self.chat_id,)).fetchall()]

    def test_b6c_refuses_when_agent_running(self):
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b, "_dispatcher_kimi_agents",
                               return_value=["kimi acp -- /tmp/x"]), \
             mock.patch.object(b, "_restart_dispatcher") as m_restart:
            with self.assertRaises(ValueError):
                self.srv.tool_restart_dispatcher({})
        m_restart.assert_not_called()
        self.assertTrue(any("拒绝重启调度器" in e for e in self._events()))

    def test_b6d_pass_event_neutral_in_run_local(self):
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(b, "_dispatcher_kimi_agents", return_value=[]), \
             mock.patch.object(b, "_restart_dispatcher",
                               return_value={"restarted": True, "label": "run_local",
                                             "health": {"ok": True, "age": 1},
                                             "heartbeat_updated": True, "detail": "ok"}) as m_restart:
            self.srv.tool_restart_dispatcher({})
        m_restart.assert_called_once()
        texts = [e for e in self._events() if "预检通过" in e]
        self.assertTrue(texts)
        for e in texts:
            self.assertNotIn("launchctl", e)
            self.assertIn("exec 自重启", e)

    def test_b6e_unmarked_pass_event_keeps_launchctl_text(self):
        os.environ.pop(MARKER, None)
        with mock.patch.object(b, "_dispatcher_kimi_agents", return_value=[]), \
             mock.patch.object(b, "_restart_dispatcher",
                               return_value={"restarted": True, "label": "x",
                                             "health": {"ok": True, "age": 1},
                                             "heartbeat_updated": True, "detail": "ok"}):
            self.srv.tool_restart_dispatcher({})
        texts = [e for e in self._events() if "预检通过" in e]
        self.assertTrue(any("launchctl kickstart" in e for e in texts))


class BoardKickstartDegradeTest(unittest.TestCase):
    """B7：看板自动重启在直跑模式降级为「事件 + 心跳标记」"""

    def test_b7_run_local_degrade(self):
        events, hb_writes = [], []
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(d, "PROJECTS", {"req-board": {"paths": [str(d.REPO_DIR.resolve())]}}), \
             mock.patch.object(d, "subprocess", mock.Mock(run=mock.Mock())) as m_sp, \
             mock.patch.object(d, "write_heartbeat",
                               side_effect=lambda extra=None: hb_writes.append(extra)), \
             mock.patch.object(d, "post_dispatcher_event",
                               side_effect=lambda rid, content, kind="action":
                               events.append((rid, kind, content))), \
             mock.patch.object(d, "log", mock.Mock()):
            d._kickstart_board_if_own("req-board", 8)
        m_sp.run.assert_not_called()   # launchctl 零调用
        self.assertEqual(events, [(8, "merge", PRD_TEXT)])   # 文案 = PRD 原文，且无「失败」事件
        self.assertEqual(len(hb_writes), 1)
        flag = hb_writes[0]["board_restart_pending"]
        self.assertEqual(flag["req_id"], 8)
        self.assertIsInstance(flag["at"], float)

    def test_b7b_unmarked_keeps_launchctl_path(self):
        os.environ.pop(MARKER, None)
        events = []
        with mock.patch.object(d, "PROJECTS", {"req-board": {"paths": [str(d.REPO_DIR.resolve())]}}), \
             mock.patch.object(d, "subprocess", mock.Mock(
                 run=mock.Mock(return_value=mock.Mock(returncode=0)))), \
             mock.patch.object(d, "_wait_board_up", return_value=True), \
             mock.patch.object(d, "post_dispatcher_event",
                               side_effect=lambda rid, content, kind="action":
                               events.append((rid, kind, content))), \
             mock.patch.object(d, "log", mock.Mock()):
            d._kickstart_board_if_own("req-board", 8)
        self.assertEqual(events, [(8, "merge", "合并后已重启看板（local.req-board），新代码生效")])


class HealthEndpointPendingTest(unittest.TestCase):
    """B8：/api/dispatcher-health 透出 board_restart_pending（at > BOARD_STARTED_AT 才返回）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hb = Path(self.tmp.name) / "hb.json"
        p = mock.patch.object(b, "HEARTBEAT_FILE", self.hb)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = b.app.test_client()
        self.now = time.time()
        self.hb.write_text(json.dumps({"last_poll_at": self.now}))

    def _get(self):
        resp = self.client.get("/api/dispatcher-health")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_b8a_flag_after_board_start_visible(self):
        flag = {"at": self.now, "req_id": 8}
        self.hb.write_text(json.dumps({"last_poll_at": self.now, "board_restart_pending": flag}))
        with mock.patch.object(b, "BOARD_STARTED_AT", self.now - 10):
            self.assertEqual(self._get()["board_restart_pending"], flag)

    def test_b8b_flag_before_board_start_hidden(self):
        """看板已重启（flag.at ≤ BOARD_STARTED_AT）→ null（无需清理写入自动隐藏）"""
        self.hb.write_text(json.dumps({"last_poll_at": self.now,
                                       "board_restart_pending": {"at": self.now - 100, "req_id": 8}}))
        with mock.patch.object(b, "BOARD_STARTED_AT", self.now):
            self.assertIsNone(self._get()["board_restart_pending"])

    def test_b8c_absent_flag_and_no_heartbeat_null(self):
        with mock.patch.object(b, "BOARD_STARTED_AT", self.now - 10):
            self.assertIsNone(self._get()["board_restart_pending"])
        self.hb.unlink()
        data = self._get()
        self.assertIsNone(data["board_restart_pending"])
        self.assertIsNone(data["age"])


class FrontendWiringLockTest(unittest.TestCase):
    """B9：前端接线源码锁（JS 语法由既有 test_served_js_syntax 覆盖）"""

    def test_b9_refresh_health_consumes_state_and_warn_style(self):
        """#40：refreshHealth 先读服务端 state 按五态渲染；琥珀待重启提示只在 connected 分支。"""
        src = frontend_src()
        start = src.index("async function refreshHealth()")
        end = src.index("refreshHealth();", start)   # 函数体在首次调用之前结束（不锁固定字符窗口）
        body = src[start:end]
        self.assertIn("h.state", body)               # #40：状态名以服务端 state 为准
        self.assertLess(body.index("h.state"), body.index("h.board_restart_pending"))
        self.assertIn("显式停用", body)
        self.assertIn("凭据未配置", body)
        self.assertIn("心跳过期", body)
        self.assertIn("网络连接失败", body)
        self.assertIn("凭据未加载", body)
        self.assertIn("已连接", body)
        self.assertIn("调度器未上报", body)
        self.assertIn("心跳 pid 非当前调度器进程", body)
        self.assertIn("h.board_restart_pending", body)
        self.assertIn("health warn", body)
        self.assertIn("看板有新改动待重启", body)
        self.assertIn("Ctrl-C 后重跑 run_local.sh", body)
        self.assertIn(PRD_TEXT, body)   # title 全文（PRD 原文）
        self.assertNotIn("调度器正常", body)          # 旧「正常/疑似故障」文案不再出现
        self.assertNotIn("心跳已过期", body)          # 旧 stale_seconds 拼接不再出现
        # 琥珀样式两行
        self.assertIn(".health.warn .dot { background: var(--health-warn-dot); }", src)
        self.assertIn(".health.warn { color: var(--health-warn-fg); }", src)


if __name__ == "__main__":
    unittest.main()
