#!/usr/bin/env python3
r"""req-8 run_local 直跑模式：exec 自重启 + 标记路由 + 脚本 + 启动序列（D 组）。

覆盖 PRD 四条验收 / 红线 3 的调度器侧：
- D1/D2  run_local.sh 导出标记（源码锁 + 探针实跑）；trap 整组清理原文保留
- D3     模式判定单一源（每次现读 env）
- D4     无标记 + SIGTERM（带标记文件）→ 原路径：execv 零调用 + sys.exit(0)
- D5     run_local + 无标记 SIGTERM → 停止语义（Ctrl-C/裸 kill 不被劫持）
- D6     run_local + 新鲜标记 + SIGTERM → exec 自重启路由
- D7     陈旧标记（>TTL）→ 不 exec
- D8     SIGINT 永不 exec（kill 0 停止语义）
- D9     exec 失败兜底：不退出 + log.error + 心跳 restart_exec_failed
- D10    exec 参数（sys.executable + 绝对 dispatcher.py）与 flush 顺序
- D11    auto 加固：写标记失败跳过 kill 不计 attempts / kill 异常清标记
- D12    auto 正常路径（run_local）：kill + attempts + 重启前事件
- D13    文案适配（_verify_pending_restart / _fallback_manual_hint）
- D14    启动序列：exec 后对账链顺序 + 在途卡经 adopt 继续推进
- D15    ambient 清除契约（源码锁：conftest + 四文件模块级 pop）
- D16    ambient 隔离守护：携标记子进程经夹具 runner 复跑四文件全绿
- D17    启动写盘：run_local 时 write_heartbeat({pid, boot_at}) 先于对账链
- D18    脚本校验口径源码锁（末 token；含空格路径；两处注释互引）
- D19    判活循环复修：任一子进程退出即整组停止（探针实跑，不再留半套服务假活）

运行（worktree 内，用 main 工作区 .venv；标记隔离见 conftest autouse fixture）：
    .venv/bin/python -m pytest tests/test_runlocal_supervisor.py -q
    # 裸 unittest 亦可（本文件用例自管标记，不依赖 conftest）：
    .venv/bin/python -m unittest tests.test_runlocal_supervisor -v

隔离（#420/#343）：REQ_BOARD_API 缺省不可达；心跳/来源标记指临时目录；不真调 os.execv/os.kill。
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tests._frontend_src import backend_text  # noqa: E402

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 防误连生产 8899（#343）
os.environ.pop("REQ_BOARD_SUPERVISOR", None)  # 钉无标记基线（unittest 线；pytest 侧另有 conftest）

import dispatcher as d  # noqa: E402
import acp_shared as ac  # noqa: E402

MARKER = "REQ_BOARD_SUPERVISOR"
RUN_LOCAL = "run_local"


def fresh_state():
    return {"chat_id": "oc_req8_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


class _TmpFilesMixin:
    """心跳/来源标记指临时目录，测试后清理"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name)
        self.src_file = t / "dispatcher_restart_source.json"
        self.hb_file = t / "dispatcher_heartbeat.json"
        self._p_src = mock.patch.object(d, "RESTART_SOURCE_FILE", self.src_file)
        self._p_hb = mock.patch.object(d, "HEARTBEAT_FILE", self.hb_file)
        self._p_src.start()
        self._p_hb.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._p_hb.stop)
        self.addCleanup(self._p_src.stop)

    def write_marker(self, source="web", at=None):
        self.src_file.write_text(json.dumps({"source": source, "at": at if at is not None else time.time()}))

    def write_heartbeat(self, hb):
        self.hb_file.write_text(json.dumps(hb))


class ScriptMarkerTest(unittest.TestCase):
    """D1/D2：run_local.sh 导出标记（源码锁 + 探针实跑）"""

    def test_d1_source_lock(self):
        """标记 export 存在且在 board.py/dispatcher.py 启动命令之前；trap/判活循环原文保留"""
        src = (REPO / "scripts" / "run_local.sh").read_text(encoding="utf-8")
        self.assertIn("export REQ_BOARD_SUPERVISOR=run_local", src)
        self.assertLess(src.index("export REQ_BOARD_SUPERVISOR=run_local"), src.index('"$PY" board.py'))
        self.assertLess(src.index("export REQ_BOARD_SUPERVISOR=run_local"), src.index('"$PY" dispatcher.py'))
        # 停止语义红线未动（逐字）：trap 整组清理不变
        self.assertIn("trap 'trap - INT TERM EXIT; kill 0 2>/dev/null; exit 0' INT TERM EXIT", src)
        # 判活循环（D19 复修）：任一子进程退出即整组停止 + 点名。旧实现是「只要还有任一
        # 子进程活着就继续循环」，board.py 被端口冲突打死时留下半套服务（详见 D19）
        self.assertIn('while [ -z "$dead" ]; do', src)
        self.assertIn('[ -n "$dead" ] || sleep 1', src)
        self.assertIn("已退出，全组停止", src)

    def test_d2_probe_receives_marker(self):
        """探针替身解释器跑 board 模式：子进程读到 REQ_BOARD_SUPERVISOR=run_local（独立会话）"""
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            out = Path(tmp) / "env.txt"
            probe.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys, time\n"
                f"open({str(out)!r}, 'w').write(os.environ.get('REQ_BOARD_SUPERVISOR', '<unset>'))\n"
                "time.sleep(1.0)\n", encoding="utf-8")
            probe.chmod(0o755)   # run_local.sh 把 REQ_BOARD_PYTHON 当解释器直接执行 → 需可执行位
            env = dict(os.environ, REQ_BOARD_PYTHON=str(probe),
                       REQ_BOARD_NO_FEISHU="1")
            env.pop(MARKER, None)
            # 独立会话：脚本 EXIT trap 的 kill 0 只作用于实例自身进程组
            proc = subprocess.run(["bash", str(REPO / "scripts" / "run_local.sh"), "board"],
                                  cwd=str(REPO), env=env, capture_output=True, text=True,
                                  timeout=30, start_new_session=True)
            self.assertTrue(out.exists(), f"探针未写出 env；stdout={proc.stdout} stderr={proc.stderr}")
            self.assertEqual(out.read_text(), RUN_LOCAL)


class ScriptLivenessLoopTest(unittest.TestCase):
    """D19（复修）：任一子进程退出即整组停止，不留「只有调度器的半套服务」"""

    def test_d19_one_child_death_stops_whole_group(self):
        """一死一活：脚本必须秒级退出（旧实现挂死不退）+ 带走还活着的那个 + 点名死者

        现场（2026-10-10）：8899 被占时 board.py 输出 Address already in use 即死，旧循环
        因「还有调度器活着」继续跑——终端看着在跑、网页拒绝连接。
        """
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            killed = t / "killed"
            probe = t / "probe.py"
            probe.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, time, signal\n"
                "if sys.argv[1] == 'dispatcher.py':\n"
                "    time.sleep(0.3)\n"
                "    sys.exit(3)\n"
                "def _on_term(signum, frame):\n"
                f"    open({str(killed)!r}, 'w').write('1')\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, _on_term)\n"
                "while True:\n"
                "    time.sleep(0.2)\n", encoding="utf-8")
            probe.chmod(0o755)   # run_local.sh 把 REQ_BOARD_PYTHON 当解释器直接执行 → 需可执行位
            env = dict(os.environ, REQ_BOARD_PYTHON=str(probe),
                       REQ_BOARD_NO_FEISHU="1")
            env.pop(MARKER, None)
            # 独立会话：脚本 EXIT trap 的 kill 0 只作用于实例自身进程组
            proc = subprocess.Popen(["bash", str(REPO / "scripts" / "run_local.sh"), "all"],
                                    cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
            t0 = time.time()
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)   # 半套服务假活：先清干净整组再判失败
                out, err = proc.communicate()
                self.fail(f"判活循环未整体退出（{time.time() - t0:.1f}s 仍存活，已强杀进程组）；"
                          f"stdout={out} stderr={err}")
            self.assertLess(time.time() - t0, 10, "整组停止不及时（秒级内应退出）")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("调度器 dispatcher.py", out)   # 点名是哪个子进程死了
            self.assertIn("已退出，全组停止", out)
            self.assertTrue(killed.exists(), f"还活着的子进程未被带走；stdout={out} stderr={err}")


class ModePredicateTest(unittest.TestCase):
    """D3：模式判定每次现读 env"""

    def test_d3_three_states_and_live_read(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(MARKER, None)
            self.assertEqual(ac.supervisor_mode(), "")
            self.assertFalse(ac.is_run_local_supervisor())
            os.environ[MARKER] = RUN_LOCAL
            self.assertTrue(ac.is_run_local_supervisor())
            os.environ[MARKER] = "launchd"
            self.assertFalse(ac.is_run_local_supervisor())
            self.assertEqual(ac.supervisor_mode(), "launchd")
            del os.environ[MARKER]
            self.assertFalse(ac.is_run_local_supervisor())


class ShutdownHandlerRoutingTest(_TmpFilesMixin, unittest.TestCase):
    """D4-D9：信号 handler 分流（exec 判定唯一决策点）"""

    def _run_handler(self, signum, marker=None):
        m_log = mock.Mock()
        with mock.patch.object(d, "log", m_log), mock.patch.object(d.sys, "exit") as m_exit:
            if marker:
                with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}):
                    d._shutdown_handler(signum, None)
            else:
                os.environ.pop(MARKER, None)
                d._shutdown_handler(signum, None)
        return m_log, m_exit

    def test_d4_unmarked_term_with_marker_file_exits_original_path(self):
        """无标记 + SIGTERM + 标记文件 → 原路径：execv 零调用 + sys.exit(0) + last_shutdown 带来源"""
        with mock.patch("os.execv") as m_exec:
            self.write_marker("script")
            _, m_exit = self._run_handler(signal.SIGTERM, marker=None)
        m_exec.assert_not_called()
        m_exit.assert_called_once_with(0)
        hb = json.loads(self.hb_file.read_text())
        self.assertEqual(hb["last_shutdown"]["source"], "script")

    def test_d5_run_local_without_marker_is_stop_semantics(self):
        """run_local + 无标记 SIGTERM（Ctrl-C/kill 0 场景）→ 退出，不 exec"""
        with mock.patch("os.execv") as m_exec:
            _, m_exit = self._run_handler(signal.SIGTERM, marker=RUN_LOCAL)
        m_exec.assert_not_called()
        m_exit.assert_called_once_with(0)

    def test_d6_run_local_fresh_marker_routes_to_exec(self):
        """run_local + 新鲜标记 + SIGTERM → _exec_self_restart(source)；不 sys.exit；last_shutdown 已写"""
        self.write_marker("auto")
        with mock.patch.object(d, "_exec_self_restart", return_value=True) as m_exec_restart:
            _, m_exit = self._run_handler(signal.SIGTERM, marker=RUN_LOCAL)
        m_exec_restart.assert_called_once_with("auto")
        m_exit.assert_not_called()
        hb = json.loads(self.hb_file.read_text())
        self.assertEqual(hb["last_shutdown"]["source"], "auto")

    def test_d7_stale_marker_not_exec(self):
        """run_local + 标记 at 早于 TTL → 不 exec（fail-safe 退出）"""
        self.write_marker("auto", at=time.time() - (d.RESTART_SOURCE_TTL_SEC + 60))
        with mock.patch("os.execv") as m_exec:
            _, m_exit = self._run_handler(signal.SIGTERM, marker=RUN_LOCAL)
        m_exec.assert_not_called()
        m_exit.assert_called_once_with(0)

    def test_d8_sigint_never_exec(self):
        """run_local + 新鲜标记 + SIGINT → 停止语义（Ctrl-C 终止条路径）"""
        self.write_marker("auto")
        with mock.patch("os.execv") as m_exec:
            _, m_exit = self._run_handler(signal.SIGINT, marker=RUN_LOCAL)
        m_exec.assert_not_called()
        m_exit.assert_called_once_with(0)

    def test_d9_exec_failure_records_and_does_not_exit(self):
        """execv 抛异常：不 sys.exit（绝不裸退出）+ log.error + 心跳 restart_exec_failed 落盘"""
        self.write_marker("auto")
        with mock.patch("os.execv", side_effect=OSError("boom")):
            m_log, m_exit = self._run_handler(signal.SIGTERM, marker=RUN_LOCAL)
        m_exit.assert_not_called()
        self.assertTrue(any("exec 自重启失败" in str(c.args[0]) for c in m_log.error.call_args_list))
        hb = json.loads(self.hb_file.read_text())
        self.assertIn("restart_exec_failed", hb)
        self.assertEqual(hb["restart_exec_failed"]["source"], "auto")
        self.assertIn("boom", hb["restart_exec_failed"]["reason"])

    def test_d9b_exec_unexpected_return_records_failure(self):
        """execv 意外返回（未替换映像）同样按失败兜底（不退出 + 留痕）"""
        self.write_marker("auto")
        with mock.patch("os.execv", return_value=None):
            m_log, m_exit = self._run_handler(signal.SIGTERM, marker=RUN_LOCAL)
        m_exit.assert_not_called()
        hb = json.loads(self.hb_file.read_text())
        self.assertIn("意外返回", hb["restart_exec_failed"]["reason"])
        self.assertTrue(any("exec 自重启失败" in str(c.args[0]) for c in m_log.error.call_args_list))


class ExecSelfRestartArgsTest(_TmpFilesMixin, unittest.TestCase):
    """D10：exec 参数与 flush"""

    def test_d10_execv_args_and_flush(self):
        with mock.patch("os.execv") as m_exec, \
             mock.patch.object(sys, "stdout", mock.MagicMock()) as so, \
             mock.patch.object(sys, "stderr", mock.MagicMock()) as se:
            d._exec_self_restart("auto")
        m_exec.assert_called_once_with(sys.executable, [sys.executable, str(REPO / "dispatcher.py")])
        so.flush.assert_called_once()
        se.flush.assert_called_once()
        # 意外返回（mock 使 execv 未替换映像）→ 返回 False 且留痕
        hb = json.loads(self.hb_file.read_text())
        self.assertIn("restart_exec_failed", hb)


class MaybeAutoRestartHardeningTest(_TmpFilesMixin, unittest.TestCase):
    """D11/D12：auto 路径 run_local 加固与正常路径"""

    def setUp(self):
        super().setUp()
        d.state = fresh_state()
        self._p_save = mock.patch.object(d, "save_state")
        self._p_stray = mock.patch.object(d, "_stray_acp_pids", return_value=[])
        self._p_save.start()
        self._p_stray.start()
        self.addCleanup(self._p_stray.stop)
        self.addCleanup(self._p_save.stop)
        self.summaries, self.events = [], []
        self._p_sum = mock.patch.object(d, "append_summary",
                                        side_effect=lambda rid, line: self.summaries.append(line))
        self._p_ev = mock.patch.object(d, "post_dispatcher_event",
                                       side_effect=lambda rid, content, kind="action":
                                       self.events.append((rid, kind, content)))
        self._p_sum.start()
        self._p_ev.start()
        self.addCleanup(self._p_ev.stop)
        self.addCleanup(self._p_sum.stop)
        self._p_log = mock.patch.object(d, "log", mock.Mock())
        self._p_log.start()
        self.addCleanup(self._p_log.stop)

    def _pending(self, rid, at=None, attempts=0):
        d.state.setdefault("pending_restart", {})[str(rid)] = {
            "at": at if at is not None else time.time() - 100, "attempts": attempts}

    def test_d11a_run_local_marker_write_fail_skips_kill_without_attempt(self):
        """run_local + 写标记失败 → 跳过 kill；该轮 attempts 不自增（不虚报尝试次数）"""
        self._pending(99)
        dir_as_file = Path(self._tmp.name) / "marker_is_dir"   # 指向目录 → write_text 必失败
        dir_as_file.mkdir()
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(d, "RESTART_SOURCE_FILE", dir_as_file), \
             mock.patch.object(d.os, "kill") as m_kill:
            d._maybe_auto_restart()
        m_kill.assert_not_called()
        self.assertEqual(d.state["pending_restart"]["99"]["attempts"], 0)

    def test_d11b_run_local_kill_fail_clears_marker(self):
        """run_local + kill 异常 → 清标记（防残留标记劫持 Ctrl-C 停止语义）"""
        self._pending(99)
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(d.os, "kill", side_effect=OSError("boom")):
            d._maybe_auto_restart()
        self.assertFalse(self.src_file.exists())
        self.assertEqual(d.state["pending_restart"]["99"]["attempts"], 1)

    def test_d11c_unmarked_kill_fail_keeps_marker(self):
        """无标记（launchd 旧路径）+ kill 异常 → 仅 log，不清理标记（逐字不变）"""
        self._pending(99)
        os.environ.pop(MARKER, None)
        with mock.patch.object(d.os, "kill", side_effect=OSError("boom")):
            d._maybe_auto_restart()
        self.assertTrue(self.src_file.exists())

    def test_d11d_unmarked_marker_write_fail_still_kills(self):
        """无标记 + 写标记失败 → 仍 kill（launchd 路径逐字不变）"""
        self._pending(99)
        dir_as_file = Path(self._tmp.name) / "marker_is_dir2"
        dir_as_file.mkdir()
        os.environ.pop(MARKER, None)
        with mock.patch.object(d, "RESTART_SOURCE_FILE", dir_as_file), \
             mock.patch.object(d.os, "kill") as m_kill:
            d._maybe_auto_restart()
        m_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_d12_run_local_normal_path_posts_event_and_kills(self):
        """run_local 正常路径：标记 + kill + attempts+1 机制不变；kill 前补发「将 exec 自重启」事件"""
        self._pending(99)
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
             mock.patch.object(d.os, "kill") as m_kill:
            d._maybe_auto_restart()
        m_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        src = json.loads(self.src_file.read_text())
        self.assertEqual(src["source"], "auto")
        self.assertEqual(d.state["pending_restart"]["99"]["attempts"], 1)
        self.assertTrue(any(k == "merge" and "exec 自重启" in c for _, k, c in self.events))

    def test_d12b_unmarked_normal_path_no_new_event(self):
        """无标记正常路径：不新增「将 exec 自重启」事件（launchd 零新增事件）"""
        self._pending(99)
        os.environ.pop(MARKER, None)
        with mock.patch.object(d.os, "kill") as m_kill:
            d._maybe_auto_restart()
        m_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        self.assertEqual(self.events, [])


class WordingAdaptationTest(_TmpFilesMixin, unittest.TestCase):
    """D13：文案适配"""

    def setUp(self):
        super().setUp()
        d.state = fresh_state()
        self._p_save = mock.patch.object(d, "save_state")
        self._p_save.start()
        self.addCleanup(self._p_save.stop)
        self.summaries, self.events, self.hb_writes = [], [], []
        self._p_sum = mock.patch.object(d, "append_summary",
                                        side_effect=lambda rid, line: self.summaries.append(line))
        self._p_ev = mock.patch.object(d, "post_dispatcher_event",
                                       side_effect=lambda rid, content, kind="action":
                                       self.events.append((rid, kind, content)))
        self._p_hbw = mock.patch.object(d, "write_heartbeat",
                                        side_effect=lambda extra=None: self.hb_writes.append(extra))
        self._p_lhb = mock.patch.object(d, "load_heartbeat", return_value={"last_shutdown": None})
        for p in (self._p_sum, self._p_ev, self._p_hbw, self._p_lhb):
            p.start()
            self.addCleanup(p.stop)

    def test_d13_verify_unconfirmed_run_local_mentions_no_keepalive(self):
        """exec 模式异常兜底（无 last_shutdown）：run_local 文案不含 KeepAlive；无标记逐字含"""
        d.state["pending_restart"] = {"99": {"at": 1000.0, "attempts": 1}}
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}):
            d._verify_pending_restart()
        self.assertTrue(self.summaries and all("KeepAlive" not in s for s in self.summaries))
        self.summaries.clear()
        d.state["pending_restart"] = {"99": {"at": 1000.0, "attempts": 1}}
        os.environ.pop(MARKER, None)
        d._verify_pending_restart()
        self.assertTrue(any("KeepAlive 拉起" in s for s in self.summaries))

    def test_d13b_fallback_hint_run_local(self):
        """_fallback_manual_hint：run_local 文案含「Ctrl-C 后重跑 run_local.sh」；无标记含 kill <pid>"""
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}):
            d._fallback_manual_hint(99)
        self.assertTrue(any("Ctrl-C 后重跑 run_local.sh" in s for s in self.summaries))
        self.summaries.clear()
        os.environ.pop(MARKER, None)
        d._fallback_manual_hint(99)
        self.assertTrue(any("kill <pid>" in s for s in self.summaries))


class StartupSequenceTest(_TmpFilesMixin, unittest.TestCase):
    """D14/D17：启动序列（exec 后在场续跑 + 直跑启动写盘）"""

    def _run_main(self, order):
        """跑 main() 至第一次 tick：mock 全部启动步骤，记录调用序；tick 抛 KeyboardInterrupt 退出"""
        def rec(name):
            return lambda *a, **k: order.append(name)

        d.state = fresh_state()
        patches = [
            mock.patch.object(d, "_mount_file_logger"),
            mock.patch.object(d, "register_shutdown_handlers", rec("handlers")),
            mock.patch.object(d, "report_previous_exit", rec("report")),
            mock.patch.object(d, "load_state", rec("load")),
            mock.patch.object(d, "_verify_pending_restart", rec("verify")),
            mock.patch.object(d, "reconcile_ghost_pending", rec("ghost")),
            mock.patch.object(d, "backfill_flow_snapshots", rec("backfill")),
            mock.patch.object(d, "reconcile_adopt_results", rec("adopt")),
            mock.patch.object(d, "reconcile_startup", rec("startup")),
            mock.patch.object(d, "reconcile_orphan_sessions", rec("orphan")),
            mock.patch.object(d, "cleanup_stale_worktrees", rec("stale-wt")),
            mock.patch.object(d, "recover_manual_commands", rec("manual-cmds")),
            mock.patch.object(d, "cleanup_orphan_held", rec("held")),
            mock.patch.object(d, "sweep_empty_sessions", rec("sweep")),
            mock.patch.object(d, "write_heartbeat",
                              side_effect=lambda extra=None, **k: order.append(("heartbeat", extra))),
            mock.patch.object(d, "NO_FEISHU", True),
            mock.patch.object(d.threading, "Thread"),
            mock.patch.object(d.time, "sleep"),
            mock.patch.object(d, "_main_loop_tick", side_effect=KeyboardInterrupt),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(KeyboardInterrupt):
            d.main()

    def test_d14a_startup_order_run_local(self):
        """run_local：启动序 = 写盘 → handlers → report → load → verify → 对账链（ghost→backfill→
        adopt→startup→orphan）——exec 后同链路接管在途卡/会话"""
        order = []
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}):
            self._run_main(order)
        names = [x[0] if isinstance(x, tuple) else x for x in order]
        self.assertEqual(names[:11], ["heartbeat", "handlers", "report", "load", "verify",
                                      "ghost", "backfill", "adopt", "startup", "orphan", "stale-wt"])

    def test_d17_startup_heartbeat_payload_run_local_and_absent_unmarked(self):
        """#40：run_local 与无标记（launchd 路径）首写都含 pid/boot_at，且首写先于 handler；
        run_local 的 exec 自重启仍靠 boot_at 变化判定（os.execv 不换 PID）。"""
        order = []
        with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}):
            self._run_main(order)
        first = order[0]
        self.assertEqual(first[0], "heartbeat")
        extra = first[1] or {}
        self.assertEqual(extra.get("pid"), os.getpid())
        self.assertIsInstance(extra.get("boot_at"), float)
        order2 = []
        os.environ.pop(MARKER, None)
        self._run_main(order2)
        first2 = order2[0]
        self.assertEqual(first2[0], "heartbeat")     # 无标记路径同样写（launchd 需心跳 pid = 当前进程）
        extra2 = first2[1] or {}
        self.assertEqual(extra2.get("pid"), os.getpid())
        self.assertIsInstance(extra2.get("boot_at"), float)

    def test_d14b_inflight_result_adopted_after_restart(self):
        """exec 重启后在场续跑：doing 卡 + 合法 result.json → reconcile_adopt_results 经
        handle_result 继续推进（借用 test_dispatcher_reconcile_adopt 替身模式，run_local 环境）"""
        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp)
            (art / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))
            calls = []
            with mock.patch.dict(os.environ, {MARKER: RUN_LOCAL}), \
                 mock.patch.object(d, "get_doing_reqs",
                                   return_value=[{"id": 8, "project": "req-board", "status": "doing",
                                                  "phase": None, "notes": ""}]), \
                 mock.patch.object(d, "artifact_dir", return_value=art), \
                 mock.patch.object(d, "artifact_ok", return_value=(True, "")), \
                 mock.patch.object(d, "handle_result",
                                   side_effect=lambda req, role, rc, tout, started:
                                   calls.append((req["id"], role, rc))), \
                 mock.patch.object(d, "api_board", return_value=[]), \
                 mock.patch.object(d, "log", mock.Mock()):
                d.state = fresh_state()
                d.reconcile_adopt_results()
        self.assertEqual(calls, [(8, "analysis", 0)])


class IsolationContractTest(unittest.TestCase):
    """D15/D18：源码锁"""

    def test_d15_ambient_clear_contract(self):
        """conftest 含 autouse 清标记 fixture；四个受影响文件含模块级 pop"""
        conftest = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
        self.assertIn("autouse=True", conftest)
        self.assertIn('os.environ.pop("REQ_BOARD_SUPERVISOR"', conftest)
        for name in ("test_done_restart_reminder", "test_dispatcher_restart_api",
                     "test_req_chat_mcp", "test_done_auto_restart"):
            src = (REPO / "tests" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIn('os.environ.pop("REQ_BOARD_SUPERVISOR", None)', src, name)

    def test_d18_script_identity_rule_locked(self):
        """两处同规则：脚本取末 token（awk $NF）+ 注释含含空格路径/exec 理由；board 侧末 token
        basename + 注释互引（bash/python 无法共享代码）"""
        sh = (REPO / "scripts" / "dispatcher-restart.sh").read_text(encoding="utf-8")
        self.assertIn("awk '{print $NF}'", sh)
        self.assertIn("含空格", sh)
        self.assertIn("exec", sh)
        self.assertIn("_dispatcher_pid_heartbeat", sh)   # 注释互引
        self.assertIn("ps -ww", sh)
        b_src = backend_text()
        start = b_src.index("def _dispatcher_pid_heartbeat")
        body = b_src[start:start + 2600]
        self.assertIn("tokens[-1]", body)      # 末 token 口径
        self.assertIn("basename", body)
        self.assertIn("含空格", body)
        self.assertIn("dispatcher-restart.sh", body)  # 注释互引


class AmbientIsolationGuardTest(unittest.TestCase):
    """D16：可执行守护——携标记子进程经夹具 runner 复跑四个受影响文件断言全绿。

    禁用 pytest 跑本守护（conftest autouse 会清标记 = 假绿）；runner 是 conftest 的 unittest 版。
    基线说明（2026-10-10 起）：项目基线已抽到 tests/_baseline.py，conftest / runner / 各测试文件
    模块级共用同一份，裸 unittest 直跑这 4 个文件同样全绿（108 用例）——早先「裸跑 errors=4/15
    的 KeyError 'req-board' 环境噪声」已随基线统一消失。本守护仍走 runner，只是要一个**不清
    REQ_BOARD_SUPERVISOR** 的运行器来复现 ambient 场景（pytest 侧 autouse 会清 → 假绿）。
    """

    def test_d16_marker_ambient_four_files_green_via_runner(self):
        env = dict(os.environ, REQ_BOARD_SUPERVISOR=RUN_LOCAL,
                   REQ_BOARD_API="http://127.0.0.1:1/api/requirements")
        proc = subprocess.run(
            [sys.executable, str(REPO / "tests" / "run_unittest_with_fixture.py"),
             "tests.test_dispatcher_restart_api", "tests.test_req_chat_mcp",
             "tests.test_done_auto_restart", "tests.test_done_restart_reminder"],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
        tail = "\n".join((proc.stderr or "").splitlines()[-25:])
        self.assertEqual(proc.returncode, 0, f"携标记复跑失败（runner 输出尾部）:\n{tail}")


if __name__ == "__main__":
    unittest.main()
