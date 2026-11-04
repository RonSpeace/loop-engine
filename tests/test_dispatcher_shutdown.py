#!/usr/bin/env python3
"""req-373 调度器重启原因可见化：信号记录 + 约定式重启入口（L0 单测）。

现场（#373）：2026-08-27 上午调度器被重启 6 次，日志无 Traceback、无 agent kill 类操作，
无法定位重启来源。根因：dispatcher.py 未注册任何信号 handler，SIGTERM 直接死亡零记录。

修复（dispatcher.py + scripts/dispatcher-restart.sh + board.py 展示侧）：
- 注册 SIGTERM/SIGINT handler：收到信号把「上次退出 = SIGTERM @ 时间（来源?）」写入心跳扩展
  字段 last_shutdown（{at, signal, source?}），随后退出；
- 启动时 report_previous_exit() 读心跳：有 last_shutdown → 「上次退出 SIGTERM @ 时间（来源: Script/未知）」；
  有心跳但无 last_shutdown → 「异常终止（崩溃/被强杀）」；无心跳记录 → 首次部署，不误报；
- 约定式重启入口先写 data/dispatcher_restart_source.json 再 kill，调度器读出即删（防残留误标）。

本测试为 L0 单测（直接调用记录/上报函数 + 断言心跳文件与日志，不发真实信号、不 kill 进程），
覆盖验收三点：优雅退出记录（含来源）/ 异常终止识别（无正常关闭标记）/ 来源标记合并与清理。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_shutdown -v
    # 或与全量一起：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
心跳/来源标记路径取 dispatcher 模块常量（worktree/data/…），测试前后恢复原状，不污染生产数据。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


class L0DispatcherShutdown(unittest.TestCase):
    """L0 单测：信号记录写心跳 / 启动上报日志 / 来源标记合并与清理"""

    def setUp(self):
        # 心跳/来源标记一律指到临时目录（测试隔离：禁止测试读写生产 data/dispatcher_heartbeat.json——
        # 全量套件里曾因 NO_FEISHU=True 的 main() 走到 init_feishu，把生产心跳写成 disabled_reason=env，
        # 看板飞书角标因此显示「显式停用」）。
        self._tmp = Path(tempfile.mkdtemp(prefix="shutdown-"))
        self._patches = [
            mock.patch.object(d, "HEARTBEAT_FILE", self._tmp / "dispatcher_heartbeat.json"),
            mock.patch.object(d, "RESTART_SOURCE_FILE", self._tmp / "dispatcher_restart_source.json"),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _seed_hb(self, last_shutdown=None):
        hb = {"last_poll_at": 1725000000.0}
        if last_shutdown is not None:
            hb["last_shutdown"] = last_shutdown
        d.HEARTBEAT_FILE.parent.mkdir(exist_ok=True)
        d.HEARTBEAT_FILE.write_text(json.dumps(hb))
        return hb

    def _log_calls(self, logger):
        return [c.args[0] for c in logger.call_args_list]
    def test_40_u17_test_process_cannot_write_prod_heartbeat(self):
        """测试隔离防线：测试进程即使把 HEARTBEAT_FILE 指回生产路径，写心跳也必须被拒绝。"""
        prod = REPO / "data" / "dispatcher_heartbeat.json"
        before = prod.read_text() if prod.exists() else None
        with mock.patch.object(d, "HEARTBEAT_FILE", prod):
            d._heartbeat_write_locked({"pid": -1})
        after = prod.read_text() if prod.exists() else None
        self.assertEqual(before, after)   # 生产心跳逐字节不变


    # ---- 验收 1：SIGTERM 优雅退出 → 心跳记录上次退出（含来源） ----
    def test_record_shutdown_writes_last_shutdown_and_keeps_poll(self):
        """收到信号写 last_shutdown（signal=名称），且保留原 last_poll_at"""
        self._seed_hb()
        d._record_shutdown(15)  # SIGTERM
        hb = json.loads(d.HEARTBEAT_FILE.read_text())
        self.assertEqual(hb["last_shutdown"]["signal"], "SIGTERM")
        self.assertIn("at", hb["last_shutdown"])
        self.assertNotIn("source", hb["last_shutdown"])  # 裸 kill：来源未知
        self.assertEqual(hb["last_poll_at"], 1725000000.0)  # 旧字段保留

    def test_record_shutdown_merges_restart_source_and_removes_file(self):
        """约定入口先写来源标记：记录合并 source，且标记文件读完即删（防残留误标）"""
        self._seed_hb()
        d.RESTART_SOURCE_FILE.write_text(json.dumps({"source": "script", "at": 1725000000.0}))
        d._record_shutdown(15)
        hb = json.loads(d.HEARTBEAT_FILE.read_text())
        self.assertEqual(hb["last_shutdown"]["source"], "script")
        self.assertFalse(d.RESTART_SOURCE_FILE.exists())

    def test_shutdown_handler_exits_zero(self):
        """信号 handler 记录后使进程退出（sys.exit(0)）"""
        self._seed_hb()
        with mock.patch.object(d.sys, "exit") as m_exit:
            d._shutdown_handler(2, None)  # SIGINT
        m_exit.assert_called_once_with(0)
        hb = json.loads(d.HEARTBEAT_FILE.read_text())
        self.assertEqual(hb["last_shutdown"]["signal"], "SIGINT")

    # ---- 验收 1 展示：启动上报日志 ----
    def test_report_previous_exit_logs_shutdown_with_source(self):
        """启动读到 last_shutdown：日志「上次退出 SIGTERM @ 时间（来源: script）」"""
        self._seed_hb({"at": 1725000000.0, "signal": "SIGTERM", "source": "script"})
        with mock.patch.object(d.log, "info") as m_info, mock.patch.object(d.log, "warning") as m_warn:
            d.report_previous_exit()
        msg = self._log_calls(m_info)[0]
        self.assertIn("上次退出 SIGTERM @", msg)
        self.assertIn("（来源: script）", msg)
        self.assertFalse(self._log_calls(m_warn))

    def test_report_previous_exit_logs_shutdown_unknown_source(self):
        """无来源标记：显示「来源未知」"""
        self._seed_hb({"at": 1725000000.0, "signal": "SIGTERM"})
        with mock.patch.object(d.log, "info") as m_info, mock.patch.object(d.log, "warning") as m_warn:
            d.report_previous_exit()
        self.assertIn("（来源未知）", self._log_calls(m_info)[0])
        self.assertFalse(self._log_calls(m_warn))

    # ---- 验收 2：kill -9 / 崩溃 → 无正常关闭标记 → 异常终止 ----
    def test_report_previous_exit_logs_abnormal_without_shutdown_key(self):
        """有心跳但无 last_shutdown → 日志警告「异常终止（崩溃/被强杀）」"""
        self._seed_hb()  # 仅 last_poll_at
        with mock.patch.object(d.log, "info") as m_info, mock.patch.object(d.log, "warning") as m_warn:
            d.report_previous_exit()
        self.assertIn("异常终止", self._log_calls(m_warn)[0])
        self.assertFalse(self._log_calls(m_info))

    def test_report_previous_exit_first_start_silent(self):
        """无心跳记录（首次部署/旧版本）→ 不误报，无日志"""
        with mock.patch.object(d.log, "info") as m_info, mock.patch.object(d.log, "warning") as m_warn:
            d.report_previous_exit()
        self.assertFalse(self._log_calls(m_info))
        self.assertFalse(self._log_calls(m_warn))

    # ---- 验收 3：来源标记残留（kill -9 前已写标记的兜底） ----
    def test_report_previous_exit_stale_source_warns(self):
        """来源标记残留但无上次退出记录（进程被强杀未及记录）→ 警告含来源并清理标记"""
        self._seed_hb()
        d.RESTART_SOURCE_FILE.write_text(json.dumps({"source": "script", "at": 1725000000.0}))
        with mock.patch.object(d.log, "warning") as m_warn:
            d.report_previous_exit()
        self.assertIn("script", self._log_calls(m_warn)[0])
        self.assertFalse(d.RESTART_SOURCE_FILE.exists())  # 读完即删

    # ---- 心跳写入向后兼容：轮询写心跳不丢 last_shutdown ----
    def test_write_heartbeat_preserves_last_shutdown(self):
        """每轮 write_heartbeat() 保留 last_shutdown 扩展字段（否则记录会被下一轮覆盖丢失）"""
        self._seed_hb({"at": 1725000000.0, "signal": "SIGTERM", "source": "reqchat"})
        d.write_heartbeat()
        hb = json.loads(d.HEARTBEAT_FILE.read_text())
        self.assertEqual(hb["last_shutdown"]["source"], "reqchat")
        self.assertGreater(hb["last_poll_at"], 1725000000.0)

    # ---- #40 U16：心跳路径与看板同口径（REQ_BOARD_DATA）----
    def test_40_u16_heartbeat_file_respects_req_board_data(self):
        """设置 REQ_BOARD_DATA → dispatcher.HEARTBEAT_FILE 落该目录（演练与看板共用一份）；
        未设置 → 仍是仓库 data/dispatcher_heartbeat.json（生产行为零变化）。"""
        code = "import json, dispatcher; print(json.dumps(str(dispatcher.HEARTBEAT_FILE)))"
        r1 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(REPO), timeout=120)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(json.loads(r1.stdout), str(REPO / "data" / "dispatcher_heartbeat.json"))
        tmp = tempfile.mkdtemp(prefix="req40-hb-")
        try:
            r2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=str(REPO), env=dict(os.environ, REQ_BOARD_DATA=tmp),
                                timeout=120)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(json.loads(r2.stdout), str(Path(tmp) / "dispatcher_heartbeat.json"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
