#!/usr/bin/env python3
"""#23 飞书通道状态：跨侧指纹一致性 + dispatcher 心跳状态写入 + board 四态判定矩阵。

对应用例（tech.md §7.1 1-24 的核心子集）：
- 1/2/3/4 凭据缺失、非法、结构不符、不可读 → disabled_reason 分型且加载指纹为 null
- 5  有效凭据 → enabled=true/connected=false/phase=connecting + 指纹非空不含明文
- 7  跨侧指纹一致性：board._feishu_credential_fingerprint 与 dispatcher.feishu_credential_fingerprint
     逐字节相等且等于参考值（sha256(app_id + \\x00 + app_secret)[:16]，不做 strip）
- 8  指纹稳定性（连续两次调用一致）
- 9  REQ_BOARD_NO_FEISHU=1 → disabled_reason=env，不读文件
- 10/11/12 首连成功 → connected；首连失败 → failed 且原因脱敏；_conn 早退不算首连
- 13/14 on_reconnecting / on_reconnected
- 15 心跳并发写不丢字段（write_heartbeat 与 _update_feishu_status 互不覆盖）
- 16 _record_shutdown 保留 feishu 字段且不刷新 last_poll_at
- 17 错误文案脱敏（secret/换行/截断）
- 18/19/20/21/22/23/24 board 四态优先级 + 心跳过期标注

隔离：临时 FEISHU_CONFIG_FILE / HEARTBEAT_FILE / RESTART_SOURCE_FILE（patch 模块属性）；
不触真实仓库 feishu.json、data/ 与网络。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_feishu_channel_status -v
"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

APP = "cli_status0001"
SECRET = "SECRET-STATUS-XYZ"


def ref_fp(app_id, app_secret):
    return hashlib.sha256(app_id.encode("utf-8") + b"\x00"
                          + app_secret.encode("utf-8")).hexdigest()[:16]


class FingerprintParity(unittest.TestCase):
    """用例 7/8：两侧指纹实现逐字节一致（唯一算法定值）。"""

    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b

    def test_07_08_cross_side_identical(self):
        import board as b
        matrix = [
            ("cli_x", "sec_y"),
            (APP, SECRET),
            ("cli_中文", "密钥"),
            ("cli_z", "with\x00nul"),
            (" cli_lead", " trail "),          # 不做 strip：空白参与摘要
            ("cli_a" * 40, "s" * 300),         # 超长
            ("cli_z", ""),
            ("", "sec_y"),
            ("cli_z", None),
            (None, None),
        ]
        for app_id, secret in matrix:
            with self.subTest(app_id=app_id, secret=secret):
                a = d.feishu_credential_fingerprint(app_id, secret)
                b_ = b._feishu_credential_fingerprint(app_id, secret)
                self.assertEqual(a, b_)
                if not app_id or not secret or not isinstance(app_id, str) or not isinstance(secret, str):
                    self.assertIsNone(a)
                else:
                    self.assertEqual(a, ref_fp(app_id, secret))
                    self.assertEqual(len(a), 16)
                    self.assertTrue(all(c in "0123456789abcdef" for c in a))
                    # 稳定性（用例 8）
                    self.assertEqual(a, d.feishu_credential_fingerprint(app_id, secret))
                    self.assertEqual(a, b._feishu_credential_fingerprint(app_id, secret))
        # 空白不被 strip：加空格必须改变摘要
        self.assertNotEqual(d.feishu_credential_fingerprint("cli_x", "sec"),
                            d.feishu_credential_fingerprint("cli_x", " sec"))


class DispatcherFeishuStatus(unittest.TestCase):
    """用例 1-5、9-17：dispatcher 侧状态写入与脱敏。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="feishu-status-"))
        self.cfg = self.tmp / "feishu.json"
        self.hb = self.tmp / "heartbeat.json"
        self._orig = {n: getattr(d, n) for n in
                      ("FEISHU_CONFIG_FILE", "HEARTBEAT_FILE", "RESTART_SOURCE_FILE",
                       "feishu", "NO_FEISHU", "_FEISHU_SECRET")}
        d.FEISHU_CONFIG_FILE = self.cfg
        d.HEARTBEAT_FILE = self.hb
        d.RESTART_SOURCE_FILE = self.tmp / "restart_source.json"
        d.feishu = None
        d.NO_FEISHU = False
        d._FEISHU_SECRET = None

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(d, n, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj=None, raw=None):
        self.cfg.write_text(raw if raw is not None else json.dumps(obj or
                                                                    {"app_id": APP, "app_secret": SECRET}),
                            encoding="utf-8")

    def _state(self):
        return json.loads(self.hb.read_text())["feishu"]

    # ---- 1/2/3/4/9 ----
    def test_01_missing(self):
        self.assertIsNone(d.init_feishu())
        st = self._state()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["disabled_reason"], "missing")
        self.assertIsNone(st["credentials_fingerprint"])
        self.assertEqual(st["phase"], "disabled")
        self.assertFalse(st["connected"])

    def test_02_03_invalid(self):
        for raw in ("{ not json", '"just-a-string"', "[]",
                    json.dumps({"app_id": APP}),
                    json.dumps({"app_id": "", "app_secret": SECRET}),
                    json.dumps({"app_id": APP, "app_secret": ""}),
                    json.dumps({"app_id": 5, "app_secret": SECRET})):
            with self.subTest(raw=raw):
                d.feishu = None
                self.cfg.write_text(raw, encoding="utf-8")
                self.assertIsNone(d.init_feishu())
                st = self._state()
                self.assertEqual(st["disabled_reason"], "invalid")
                self.assertIsNone(st["credentials_fingerprint"])
                # 不泄露文件片段
                self.assertNotIn("just-a-string", json.dumps(st))

    def test_04_unreadable(self):
        self._write()
        os.chmod(self.cfg, 0o000)
        try:
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                self.assertIsNone(d.init_feishu())
            self.assertEqual(self._state()["disabled_reason"], "unreadable")
        finally:
            os.chmod(self.cfg, 0o600)

    def test_09_no_feishu_env(self):
        self._write()
        d.NO_FEISHU = True
        with mock.patch.object(d, "_read_feishu_config",
                               mock.Mock(side_effect=AssertionError("不应读文件"))):
            self.assertIsNone(d.init_feishu())
        st = self._state()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["disabled_reason"], "env")
        self.assertIsNone(st["credentials_fingerprint"])

    def test_09b_main_writes_env_disabled_without_connecting(self):
        """评审打回级：`REQ_BOARD_NO_FEISHU=1` 的真实启动路径（main）必须写出
        `disabled_reason=env`，且不建 client、不起 WebSocket 线程。"""
        self._write()
        d.NO_FEISHU = True
        noop = mock.Mock()
        patches = {
            "_mount_file_logger": noop, "register_shutdown_handlers": noop,
            "report_previous_exit": noop, "load_state": noop,
            "_verify_pending_restart": noop, "is_run_local_supervisor": lambda: False,
            "reconcile_ghost_pending": noop, "backfill_flow_snapshots": noop,
            "reconcile_adopt_results": noop, "reconcile_startup": noop,
            "reconcile_orphan_sessions": noop, "cleanup_stale_worktrees": noop,
            "recover_manual_commands": noop, "cleanup_orphan_held": noop,
            "sweep_empty_sessions": noop, "manual_commands_poller": noop,
            "held_reaper": noop, "_watchdog_loop": noop,
            "start_feishu_ws": mock.Mock(side_effect=AssertionError("不应起 ws 线程")),
            "_main_loop_tick": mock.Mock(side_effect=KeyboardInterrupt),
        }
        ctx = [mock.patch.object(d, k, v) for k, v in patches.items()]
        for c in ctx:
            c.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                d.main()
        finally:
            for c in ctx:
                c.stop()
        st = self._state()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["disabled_reason"], "env")
        self.assertIsNone(st["credentials_fingerprint"])
        self.assertEqual(st["phase"], "disabled")
        patches["start_feishu_ws"].assert_not_called()

    # ---- 5 ----
    def test_05_valid_credentials(self):
        self._write()
        with mock.patch.object(d.lark, "Client", mock.Mock()):
            cfg = d.init_feishu()
            self.assertEqual(cfg, {"app_id": APP, "app_secret": SECRET})
            st = self._state()
            self.assertTrue(st["enabled"])
            self.assertIsNone(st["disabled_reason"])
            self.assertFalse(st["connected"])
            self.assertEqual(st["phase"], "connecting")
            self.assertEqual(st["credentials_fingerprint"], ref_fp(APP, SECRET))
            self.assertNotIn(SECRET, json.dumps(st))
            self.assertEqual(st["credentials_source"], "feishu.json")

    def test_05b_credentials_source_env_override(self):
        self._write()
        with mock.patch.dict(os.environ, {"REQ_BOARD_FEISHU_JSON": str(self.cfg)}), \
             mock.patch.object(d.lark, "Client", mock.Mock()):
            d.init_feishu()
        self.assertIn("覆盖", self._state()["credentials_source"])

    # ---- 10/11/12 ----
    def _client(self):
        return d._FeishuStatusClient(APP, SECRET, event_handler=mock.Mock(),
                                     log_level=d.lark.LogLevel.WARNING)

    def test_10_first_connect_success(self):
        self._write()
        c = self._client()

        async def _ok(self_):
            self_._conn = mock.Mock()

        with mock.patch.object(d.lark.ws.Client, "_connect", _ok):
            asyncio.run(c._connect())
        st = self._state()
        self.assertTrue(st["connected"])
        self.assertEqual(st["phase"], "connected")
        self.assertIsInstance(st["connected_at"], float)

    def test_11_first_connect_failure_sanitized(self):
        self._write()
        d._FEISHU_SECRET = SECRET
        c = self._client()

        class _Boom(Exception):
            pass

        err = _Boom(f"boom\nsecret={SECRET}")

        async def _boom(self_):
            raise err

        with mock.patch.object(d.lark.ws.Client, "_connect", _boom):
            with self.assertRaises(_Boom):
                asyncio.run(c._connect())
        st = self._state()
        self.assertFalse(st["connected"])
        self.assertEqual(st["phase"], "failed")
        self.assertNotIn(SECRET, st["last_error"])
        self.assertNotIn("\n", st["last_error"])
        self.assertLessEqual(len(st["last_error"]), 300)

    def test_12_early_return_not_reported_as_first_connect(self):
        self._write()
        c = self._client()
        sentinel_conn = mock.Mock()
        c._conn = sentinel_conn

        async def _noop(self_):
            return None

        with mock.patch.object(d.lark.ws.Client, "_connect", _noop):
            asyncio.run(c._connect())
        self.assertFalse(self.hb.exists())   # 早退路径不得上报「首次连接成功」

    # ---- 12b（评审打回级修复）----
    def test_12b_early_return_does_not_leak_sdk_lock(self):
        """包装后的 `_connect` 必须在调用父类前短路早退：SDK `_conn is not None` 直接 return
        时**不释放** `_lock`（lark_oapi ws/client.py:193-196），一旦走到该分支，之后
        `_disconnect()`/`_write_message()` 的 acquire 永久阻塞 → 长连接线程卡死、心跳停在
        connecting。此处断言父类根本没被调用，并验证真实 asyncio 锁仍可获取。"""
        self._write()
        c = self._client()
        c._conn = mock.Mock()

        async def _must_not_be_called(self_):
            raise AssertionError("早退分支不得进入 SDK _connect（会持锁泄漏）")

        with mock.patch.object(d.lark.ws.Client, "_connect", _must_not_be_called):
            async def _both():
                await c._connect()
                await asyncio.wait_for(c._lock.acquire(), timeout=1)   # 锁未被泄漏持有
                c._lock.release()
            asyncio.run(_both())
        self.assertFalse(self.hb.exists())

    # ---- 11c（评审打回级兜底）----
    def test_11c_connect_watchdog_reports_failed_on_hang(self):
        """SDK 内部卡住（`start()` 不返回）时，「连接失败：原因」仍必须写进心跳。"""
        self._write()
        d._update_feishu_status(enabled=True, connected=False, phase="connecting")
        d._feishu_connect_watchdog(timeout=0)
        st = self._state()
        self.assertEqual(st["phase"], "failed")
        self.assertFalse(st["connected"])
        self.assertIn("未建立", st["last_error"])
        # 已迁移的状态不被兜底覆盖
        d._update_feishu_status(connected=True, phase="connected", last_error=None)
        d._feishu_connect_watchdog(timeout=0)
        self.assertEqual(self._state()["phase"], "connected")

    # ---- 13/14 ----
    def test_13_14_reconnect_callbacks(self):
        self._write()
        c = self._client()
        c.on_reconnecting()
        st = self._state()
        self.assertFalse(st["connected"])
        self.assertEqual(st["phase"], "reconnecting")
        c.on_reconnected()
        st2 = self._state()
        self.assertTrue(st2["connected"])
        self.assertEqual(st2["phase"], "connected")
        self.assertIsNone(st2["last_error"])
        self.assertIsInstance(st2["connected_at"], float)

    # ---- 15 ----
    def test_15_concurrent_heartbeat_fields_preserved(self):
        d.write_heartbeat({"last_shutdown": {"at": 1, "signal": "SIGTERM"}})
        d._update_feishu_status(enabled=True, connected=False, phase="connecting")
        d.write_heartbeat({"auto_restart_ok": True})
        hb = json.loads(self.hb.read_text())
        self.assertEqual(hb["feishu"]["phase"], "connecting")
        self.assertEqual(hb["last_shutdown"]["signal"], "SIGTERM")
        self.assertTrue(hb["auto_restart_ok"])
        self.assertIn("last_poll_at", hb)

    # ---- 15b（评审建议修复）----
    def test_15b_heartbeat_tmp_cleaned_on_replace_failure(self):
        """`os.replace` 失败不得留下 `.tmp` 半成品（否则异常退出窗口内心跳文件不可读）。"""
        d.write_heartbeat({"last_poll_at": 111.0})
        tmp = d.HEARTBEAT_FILE.with_suffix(".tmp")
        with mock.patch.object(d.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                d.write_heartbeat({"auto_restart_ok": True})
        self.assertFalse(tmp.exists())
        # 后续写入仍正常（无残留干扰）
        d._update_feishu_status(enabled=True, phase="connecting")
        self.assertEqual(json.loads(self.hb.read_text())["feishu"]["phase"], "connecting")

    # ---- 16 ----
    def test_16_record_shutdown_keeps_feishu_without_refresh(self):
        d.write_heartbeat({"last_poll_at": 111.0})
        d._update_feishu_status(enabled=True, connected=True, phase="connected")
        poll_before = json.loads(self.hb.read_text())["last_poll_at"]
        d._record_shutdown(15)
        hb = json.loads(self.hb.read_text())
        self.assertEqual(hb["last_poll_at"], poll_before)
        self.assertEqual(hb["last_shutdown"]["signal"], "SIGTERM")
        self.assertTrue(hb["feishu"]["connected"])

    # ---- 40 U14：启动首写清旧 feishu + 五态日志文案 ----
    def _run_main_capture(self, extra_patches=None):
        """跑 main() 到第一次 tick（打桩启动链），返回 register_shutdown_handlers 时刻的心跳快照。"""
        seen = {}

        def capture(*a, **k):
            seen["hb"] = json.loads(self.hb.read_text()) if self.hb.exists() else None

        noop = mock.Mock()
        patches = {
            "_mount_file_logger": noop, "register_shutdown_handlers": capture,
            "report_previous_exit": noop, "load_state": noop,
            "_verify_pending_restart": noop, "is_run_local_supervisor": lambda: False,
            "reconcile_ghost_pending": noop, "backfill_flow_snapshots": noop,
            "reconcile_adopt_results": noop, "reconcile_startup": noop,
            "reconcile_orphan_sessions": noop, "cleanup_stale_worktrees": noop,
            "recover_manual_commands": noop, "cleanup_orphan_held": noop,
            "sweep_empty_sessions": noop, "manual_commands_poller": noop,
            "held_reaper": noop, "_watchdog_loop": noop,
            "start_feishu_ws": mock.Mock(side_effect=AssertionError("不应起 ws 线程")),
            "_main_loop_tick": mock.Mock(side_effect=KeyboardInterrupt),
        }
        patches.update(extra_patches or {})
        ctx = [mock.patch.object(d, k, v) for k, v in patches.items()]
        for c in ctx:
            c.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                d.main()
        finally:
            for c in ctx:
                c.stop()
        return seen

    def test_40_u14_startup_clears_stale_feishu_state(self):
        """上一进程残留 connected=true → 启动首写（早于 init_feishu）必须清成 starting；launchd 路径同样写 pid/boot_at。"""
        self._write()
        d._update_feishu_status(enabled=True, connected=True, phase="connected",
                                credentials_fingerprint=ref_fp(APP, SECRET))
        self.assertTrue(json.loads(self.hb.read_text())["feishu"]["connected"])
        d.NO_FEISHU = True
        seen = self._run_main_capture()
        hb = seen["hb"]
        self.assertIsNotNone(hb)
        self.assertEqual(hb["pid"], os.getpid())          # launchd 路径（无 supervisor 标记）也写 pid
        self.assertIsInstance(hb["boot_at"], float)
        self.assertEqual(hb["feishu"]["phase"], "starting")
        self.assertIsNot(hb["feishu"]["connected"], True)  # 禁止带着上一进程的 connected 被看板读走
        # main 末态：init_feishu 写入真实状态（NO_FEISHU → disabled_reason=env）
        st = json.loads(self.hb.read_text())["feishu"]
        self.assertEqual(st["disabled_reason"], "env")
        self.assertFalse(st["connected"])

    def test_40_u14b_log_wording(self):
        """NO_FEISHU → 日志含「显式停用」不含旧句；无凭据 → 含「凭据未配置」不含停用旧句。"""
        self._write()
        d.NO_FEISHU = True
        with self.assertLogs(d.log, level="WARNING") as cm:
            d.init_feishu()
        joined = "\n".join(cm.output)
        self.assertIn("显式停用", joined)
        self.assertNotIn("跳过飞书初始化", joined)
        d.NO_FEISHU = False
        self.cfg.unlink()
        with self.assertLogs(d.log, level="WARNING") as cm2:
            d.init_feishu()
        joined2 = "\n".join(cm2.output)
        self.assertIn("凭据未配置", joined2)
        self.assertNotIn("飞书通道停用", joined2)
        self.assertNotIn("显式停用", joined2)

    def test_40_u14c_ws_thread_failure_log(self):
        """start_feishu_ws 抛错 → 日志含「网络连接失败」并把 failed 写进心跳。"""
        self._write()
        client_cls = mock.Mock()
        client_cls.return_value.start.side_effect = RuntimeError("boom")
        with mock.patch.object(d, "lark", mock.Mock()), \
             mock.patch.object(d, "_FeishuStatusClient", client_cls), \
             mock.patch.object(d.threading, "Thread", mock.Mock()), \
             self.assertLogs(d.log, level="ERROR") as cm:
            d.start_feishu_ws(APP, SECRET)
        self.assertIn("网络连接失败", "\n".join(cm.output))
        self.assertEqual(self._state()["phase"], "failed")

    # ---- 40 U15：write_heartbeat 校正 pid、不动 boot_at ----
    def test_40_u15_write_heartbeat_corrects_pid(self):
        d.HEARTBEAT_FILE.write_text(json.dumps(
            {"pid": 12345, "boot_at": 999.0, "last_poll_at": 111.0,
             "feishu": {"phase": "connecting"}}))
        d.write_heartbeat()
        hb = json.loads(self.hb.read_text())
        self.assertEqual(hb["pid"], os.getpid())
        self.assertEqual(hb["boot_at"], 999.0)            # 不在轮询里刷新 boot_at
        self.assertGreater(hb["last_poll_at"], 111.0)
        self.assertEqual(hb["feishu"]["phase"], "connecting")   # 通道子状态不被轮询覆盖
        # update_poll=False（退出记录等）：不刷新 last_poll_at，pid 仍校正
        self.hb.write_text(json.dumps({"pid": 1, "boot_at": 999.0, "last_poll_at": 222.0}))
        d.write_heartbeat({"last_shutdown": {"at": 1}}, update_poll=False)
        hb2 = json.loads(self.hb.read_text())
        self.assertEqual(hb2["last_poll_at"], 222.0)
        self.assertEqual(hb2["pid"], os.getpid())

    # ---- 17 ----
    def test_17_sanitize_reason(self):
        d._FEISHU_SECRET = SECRET
        out = d._sanitize_feishu_error(f"a\nb\r{SECRET}" + "x" * 500)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertLessEqual(len(out), 300)


class BoardFeishuStateMapping(unittest.TestCase):
    """用例 18-24：board 四态优先级（含打回级主场景）与心跳过期标注。"""

    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b
        cls.tmp = Path(tempfile.mkdtemp(prefix="req23-state-"))
        cls._old_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.b.DB_PATH = cls._old_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.b = self.b
        self.cfg = self.tmp / f"fs-{self._testMethodName}.json"
        self.hb = self.tmp / f"hb-{self._testMethodName}.json"
        self._old = (self.b.FEISHU_JSON_PATH, self.b.HEARTBEAT_FILE)
        self.b.FEISHU_JSON_PATH = self.cfg
        self.b.HEARTBEAT_FILE = self.hb
        self.b._PID_PROBE_CACHE.clear()   # #40：pid 核验缓存逐用例清零（防跨用例污染）
        self._env = os.environ.pop("REQ_BOARD_NO_FEISHU", None)

    def tearDown(self):
        self.b.FEISHU_JSON_PATH, self.b.HEARTBEAT_FILE = self._old
        self.b._PID_PROBE_CACHE.clear()
        if self._env is None:
            os.environ.pop("REQ_BOARD_NO_FEISHU", None)   # 本用例设过的环境变量必须清掉
        else:
            os.environ["REQ_BOARD_NO_FEISHU"] = self._env

    def _write_cfg(self, app_id=APP, secret=SECRET):
        self.cfg.write_text(json.dumps({"app_id": app_id, "app_secret": secret}), encoding="utf-8")

    def _write_hb(self, feishu=None, last_poll_at=None, with_feishu=True, extra=None):
        hb = {"last_poll_at": time.time() if last_poll_at is None else last_poll_at}
        if with_feishu:
            hb["feishu"] = feishu if feishu is not None else {}
        if extra:
            hb.update(extra)
        self.hb.write_text(json.dumps(hb), encoding="utf-8")

    def _fp(self):
        return self.b._feishu_credential_fingerprint(APP, SECRET)

    def _st(self):
        return self.b._feishu_runtime_status()

    # ---- 18 ----
    def test_18_old_heartbeat_unknown_but_restartable(self):
        self._write_cfg()
        self._write_hb(with_feishu=False)
        st = self._st()
        self.assertEqual(st["state"], "unknown")
        self.assertTrue(st["restart_available"])
        self.assertIn("未上报", self.b._feishu_runtime_text(st))

    def test_18b_old_heartbeat_without_config_disabled(self):
        self._write_hb(with_feishu=False)
        st = self._st()
        self.assertEqual(st["state"], "disabled")
        self.assertFalse(st["restart_available"])

    # ---- 19 ----
    def test_19_null_fingerprint_not_loaded(self):
        self._write_cfg()
        self._write_hb({"enabled": True, "disabled_reason": None, "connected": False,
                        "phase": "connecting", "credentials_fingerprint": None})
        st = self._st()
        self.assertEqual(st["state"], "not_loaded")
        self.assertTrue(st["restart_available"])

    # ---- 20 ----
    def test_20_connected_and_failed(self):
        self._write_cfg()
        self._write_hb({"enabled": True, "connected": True, "phase": "connected",
                        "credentials_fingerprint": self._fp()})
        st0 = self._st()
        self.assertEqual(st0["state"], "connected")
        self.assertTrue(st0["connected"])
        self.assertFalse(st0["restart_available"])
        self._write_hb({"enabled": True, "connected": False, "phase": "failed",
                        "credentials_fingerprint": self._fp(),
                        "last_error": "连接失败：超时"})
        st = self._st()
        self.assertEqual(st["state"], "failed")
        self.assertFalse(st["connected"])
        self.assertIn("超时", st["reason"])
        self.assertFalse(st["restart_available"])

    def test_20b_fingerprint_changed_not_loaded(self):
        self._write_cfg()
        self._write_hb({"enabled": True, "connected": True, "phase": "connected",
                        "credentials_fingerprint": ref_fp("cli_old", "old-secret")})
        self.assertEqual(self._st()["state"], "not_loaded")

    # ---- 21（打回级主路径）----
    def test_21_reject_level_main_path(self):
        self._write_cfg()
        self._write_hb({"enabled": False, "disabled_reason": "missing", "connected": False,
                        "phase": "disabled", "credentials_fingerprint": None})
        st = self._st()
        self.assertEqual(st["state"], "not_loaded")
        self.assertTrue(st["restart_available"])
        self.assertNotEqual(st["state"], "disabled")

    # ---- 22 ----
    def test_22_env_disabled_wins(self):
        """评审打回级：显式停用判据不依赖心跳是否上报——环境变量下即使心跳**无 feishu 字段**
        （旧调度器/未上报）也必须判 disabled，不得落 unknown 并露出不会生效的重启按钮。"""
        self._write_cfg()
        os.environ["REQ_BOARD_NO_FEISHU"] = "1"
        # 无心跳文件
        st = self._st()
        self.assertEqual(st["state"], "disabled")
        self.assertFalse(st["restart_available"])
        # 旧心跳（无 feishu 字段）
        self._write_hb(with_feishu=False)
        st = self._st()
        self.assertEqual(st["state"], "disabled")
        self.assertFalse(st["restart_available"])
        self.assertIn("REQ_BOARD_NO_FEISHU", st["reason"])
        # 心跳 disabled_reason=env（调度器侧显式关闭）同样压过重启入口
        os.environ.pop("REQ_BOARD_NO_FEISHU", None)
        self._write_hb({"enabled": False, "disabled_reason": "env", "connected": False,
                        "phase": "disabled", "credentials_fingerprint": None})
        st = self._st()
        self.assertEqual(st["state"], "disabled")
        self.assertFalse(st["restart_available"])

    # ---- 22b（评审建议：guide 单元格转义）----
    def test_22b_guide_row_escapes_error_text(self):
        self._write_cfg()
        self._write_hb({"enabled": True, "connected": False, "phase": "failed",
                        "credentials_fingerprint": self.b._feishu_credential_fingerprint(APP, SECRET),
                        "last_error": "连接失败：<script>alert(1)</script> & \"x\""})
        row = [r for r in self.b._runtime_env_rows() if r[0] == "飞书渠道"][0][1]
        self.assertNotIn("<script>", row)
        self.assertIn("&lt;script&gt;", row)
        self.assertIn("&amp;", row)

    # ---- 23 ----
    def test_23_legacy_disabled_without_reason(self):
        self._write_cfg()
        self._write_hb({"enabled": False, "connected": False, "phase": "disabled"})
        st = self._st()
        self.assertEqual(st["state"], "not_loaded")
        self.assertTrue(st["restart_available"])

    # ---- 24（#40 U8：过期一律 stale，不再沿用上一次 connected）----
    def test_24_stale_overrides_connected(self):
        self._write_cfg()
        self._write_hb({"enabled": True, "connected": True, "phase": "connected",
                        "credentials_fingerprint": self._fp()},
                       last_poll_at=time.time() - 400)
        st = self._st()
        self.assertEqual(st["state"], "stale")          # #40：过期压过 happy path
        self.assertGreaterEqual(st["stale_seconds"], 395)
        self.assertIn("400", st["reason"])              # reason 含过期秒数
        self.assertFalse(st["restart_available"])
        self.assertEqual(st["state_source"], "heartbeat")
        text = self.b._feishu_runtime_text(st)
        self.assertIn("心跳过期", text)
        self.assertNotIn("已连接", text)                 # 不得再拼「已连接 + 最后一次上报」
        self.assertNotIn("状态为最后一次上报", text)
        # 未过期不标注
        self._write_hb({"enabled": True, "connected": True, "phase": "connected",
                        "credentials_fingerprint": self._fp()})
        st2 = self._st()
        self.assertIsNone(st2["stale_seconds"])
        self.assertEqual(st2["state"], "connected")
        self.assertNotIn("心跳过期", self.b._feishu_runtime_text(st2))

    def test_24b_corrupt_heartbeat_fail_safe(self):
        self._write_cfg()
        self.hb.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self._st()["state"], "unknown")
        self.hb.write_text('{"feishu": "not-a-dict"}', encoding="utf-8")
        self.assertEqual(self._st()["state"], "unknown")

    def test_24c_guide_text_never_says_connected_for_not_loaded(self):
        self._write_cfg()
        self._write_hb({"enabled": False, "disabled_reason": "missing",
                        "credentials_fingerprint": None})
        text = self.b._feishu_runtime_text()
        self.assertIn("凭据未加载", text)
        self.assertNotIn("已接飞书", text)
        self.assertNotIn("未接飞书", text)

    # ---- 25（#40 U3：显式停用按心跳内容判定）----
    def test_25_heartbeat_env_disabled(self):
        """心跳 disabled_reason=env + 年龄新鲜 → disabled（看板环境未设该变量也一样）。"""
        self._write_cfg()
        self._write_hb({"enabled": False, "disabled_reason": "env", "connected": False,
                        "phase": "disabled", "credentials_fingerprint": None})
        st = self._st()
        self.assertEqual(st["state"], "disabled")
        self.assertEqual(st["state_source"], "heartbeat")
        self.assertFalse(st["restart_available"])
        self.assertIn("REQ_BOARD_NO_FEISHU", st["reason"])
        self.assertIn("显式停用", self.b._feishu_runtime_text(st))
        # 文件无效 + connected=true（无停用标记）→ disabled（凭据未配置），不是 connected
        self.cfg.unlink()
        self._write_hb({"enabled": True, "connected": True, "phase": "connected",
                        "credentials_fingerprint": None})
        st2 = self._st()
        self.assertEqual(st2["state"], "disabled")
        self.assertEqual(st2["state_source"], "local_file")
        self.assertIn("凭据未配置", self.b._feishu_runtime_text(st2))
        self.assertNotIn("显式停用", self.b._feishu_runtime_text(st2))
        # 文件无效 + 同一心跳带 disabled_reason=env → 仍是「显式停用」（第 3 条先于第 4 条）
        self._write_hb({"enabled": True, "disabled_reason": "env", "connected": True,
                        "phase": "connected", "credentials_fingerprint": None})
        st2b = self._st()
        self.assertEqual(st2b["state"], "disabled")
        self.assertEqual(st2b["state_source"], "heartbeat")
        self.assertIn("REQ_BOARD_NO_FEISHU", st2b["reason"])
        # 文件有效 + 旧指纹 + connected=true → not_loaded（不是 connected，也不是 disabled）
        self._write_cfg()
        self._write_hb({"enabled": True, "connected": True,
                        "phase": "connected",
                        "credentials_fingerprint": ref_fp("cli_old", "old-secret")})
        st3 = self._st()
        self.assertEqual(st3["state"], "not_loaded")
        self.assertTrue(st3["restart_available"])

    # ---- 26（#40 U4：看板环境不得压过一份仍有效的心跳）----
    def test_26_board_env_does_not_override_live_heartbeat(self):
        self._write_cfg()
        os.environ["REQ_BOARD_NO_FEISHU"] = "1"
        pid = 4242
        self.b._PID_PROBE_CACHE[pid] = (time.time(), "ok")   # 存活核验打桩（不真跑 ps）
        self._write_hb({"enabled": True, "connected": True, "phase": "connected",
                        "credentials_fingerprint": self._fp(), "updated_at": time.time()},
                       extra={"pid": pid})
        st = self._st()
        self.assertEqual(st["state"], "connected")
        self.assertEqual(st["pid"], pid)
        self.assertEqual(st["pid_status"], "ok")
        self.assertIsNone(st["reason"])
        # 无心跳文件时同一环境变量才是 disabled + board_env
        self.hb.unlink()
        st2 = self._st()
        self.assertEqual(st2["state"], "disabled")
        self.assertEqual(st2["state_source"], "board_env")
        self.assertFalse(st2["restart_available"])

    # ---- 27（#40 U5：凭据未加载矩阵）----
    def test_27_not_loaded_matrix(self):
        self._write_cfg()
        cases = [
            {"enabled": True, "connected": False, "phase": "connecting",
             "credentials_fingerprint": None},
            {"enabled": False, "disabled_reason": "missing", "connected": False,
             "phase": "disabled", "credentials_fingerprint": None},
            {"enabled": True, "connected": True, "phase": "connected",
             "credentials_fingerprint": ref_fp("cli_old", "old-secret")},
        ]
        for feishu in cases:
            with self.subTest(feishu=feishu):
                self._write_hb(feishu)
                st = self._st()
                self.assertEqual(st["state"], "not_loaded")
                self.assertTrue(st["restart_available"])
                self.assertEqual(st["state_source"], "heartbeat")
                self.assertNotIn(st["state"], ("disabled", "stale"))

    # ---- 28（#40 U6：网络失败不显示为主动停用）----
    def test_28_failed_and_reconnecting_are_not_disabled(self):
        self._write_cfg()
        pid = 4243
        self.b._PID_PROBE_CACHE[pid] = (time.time(), "ok")
        self._write_hb({"enabled": True, "connected": False, "phase": "failed",
                        "credentials_fingerprint": self._fp(),
                        "last_error": "连接超时（TCP 无响应）"}, extra={"pid": pid})
        st = self._st()
        self.assertEqual(st["state"], "failed")
        self.assertIn("连接超时", st["reason"])
        self.assertNotIn("已停用", st["reason"])
        self.assertNotIn("显式停用", st["reason"])
        self.assertFalse(st["restart_available"])
        self.assertIn("网络连接失败", self.b._feishu_runtime_text(st))
        self._write_hb({"enabled": True, "connected": False, "phase": "reconnecting",
                        "credentials_fingerprint": self._fp(),
                        "last_error": "长连接已断开"}, extra={"pid": pid})
        st2 = self._st()
        self.assertEqual(st2["state"], "failed")
        self.assertNotEqual(st2["state"], "disabled")

    # ---- 29（#40 U7：已连接判定与自相矛盾心跳；评审打回级：connected 布尔必须同时为真）----
    def test_29_connected_matrix(self):
        self._write_cfg()
        pid = 4244
        self.b._PID_PROBE_CACHE[pid] = (time.time(), "ok")
        base = {"enabled": True, "connected": True, "phase": "connected",
                "credentials_fingerprint": self._fp()}
        self._write_hb(base, extra={"pid": pid})
        st = self._st()
        self.assertEqual(st["state"], "connected")
        self.assertTrue(st["connected"])                # 评审打回级：state=connected 时布尔必须为真
        self.assertIsNone(st["reason"])                 # 指纹一致且无停用标记 → reason 为 null
        self.assertFalse(st["restart_available"])
        self.assertEqual(st["state_source"], "heartbeat")
        # 指纹为 null 仍是已连接（文件有效）
        self._write_hb({"enabled": True, "connected": True, "phase": "connected",
                        "credentials_fingerprint": None}, extra={"pid": pid})
        st2 = self._st()
        self.assertEqual(st2["state"], "connected")
        self.assertTrue(st2["connected"])
        # 指纹 null + disabled_reason=env：仍是已连接 + 自相矛盾注解（不写 REQ_BOARD_NO_FEISHU）
        self._write_hb({"enabled": True, "disabled_reason": "env", "connected": True,
                        "phase": "connected", "credentials_fingerprint": None},
                       extra={"pid": pid})
        st3 = self._st()
        self.assertEqual(st3["state"], "connected")
        self.assertTrue(st3["connected"])
        self.assertIn("自相矛盾", st3["reason"])
        self.assertNotIn("REQ_BOARD_NO_FEISHU", st3["reason"])
        # enabled=false 同理（长连接证据在，停用标记只作注解）
        self._write_hb({"enabled": False, "connected": True, "phase": "connected",
                        "credentials_fingerprint": self._fp()}, extra={"pid": pid})
        st4 = self._st()
        self.assertEqual(st4["state"], "connected")
        self.assertTrue(st4["connected"])
        self.assertIn("自相矛盾", st4["reason"])
        # 非 connected 态一律 false（布尔与 state 不得互相矛盾）
        for feishu, last_poll in (
                ({"enabled": False, "disabled_reason": "env", "connected": False,
                  "phase": "disabled"}, None),
                ({"enabled": True, "connected": False, "phase": "connecting",
                  "credentials_fingerprint": None}, None),
                ({"enabled": True, "connected": False, "phase": "failed",
                  "credentials_fingerprint": self._fp()}, None),
                (base, 400)):
            with self.subTest(feishu=feishu, last_poll=last_poll):
                self._write_hb(feishu, last_poll_at=(time.time() - last_poll) if last_poll else None,
                               extra={"pid": pid})
                st5 = self._st()
                self.assertNotEqual(st5["state"], "connected")
                self.assertFalse(st5["connected"])

    # ---- 30（#40 U9：pid 缺失/已死只进 pid_status 与 reason 注解，不抢 state）----
    def test_30_pid_status_never_overrides_state(self):
        self._write_cfg()
        feishu = {"enabled": True, "connected": True, "phase": "connected",
                  "credentials_fingerprint": self._fp()}
        # ① pid 缺失（旧版本调度器）
        self._write_hb(feishu)
        st = self._st()
        self.assertEqual(st["state"], "connected")
        self.assertEqual(st["pid_status"], "missing")
        self.assertIsNone(st["pid"])
        self.assertIn("心跳 pid 非当前调度器进程", st["reason"])
        self.assertNotIn("心跳过期", self.b._feishu_runtime_text(st))
        # ② pid 确认已死
        self._write_hb(feishu, extra={"pid": 999999})
        st2 = self._st()
        self.assertEqual(st2["state"], "connected")
        self.assertEqual(st2["pid_status"], "dead")
        self.assertIn("心跳 pid 非当前调度器进程", st2["reason"])
        # ③ 核验抛错/超时 → unknown（同样不改 state）
        with mock.patch.object(self.b, "subprocess",
                               mock.Mock(run=mock.Mock(side_effect=subprocess.TimeoutExpired("ps", 2)))):
            self._write_hb(feishu, extra={"pid": os.getpid()})
            st3 = self._st()
        self.assertEqual(st3["state"], "connected")
        self.assertEqual(st3["pid_status"], "unknown")
        self.assertIn("心跳 pid 非当前调度器进程", st3["reason"])
        # ④ 无 feishu 对象的心跳不产生 stale（即使年龄过期）
        self._write_hb(with_feishu=False, last_poll_at=time.time() - 400)
        st4 = self._st()
        self.assertEqual(st4["state"], "unknown")
        self.assertNotEqual(st4["state"], "stale")

    # ---- 31（#40 U10：无 feishu 对象看环境与本地文件）----
    def test_31_no_feishu_object_fallbacks(self):
        self._write_cfg()
        self._write_hb(with_feishu=False, last_poll_at=time.time() - 400)
        st = self._st()
        self.assertEqual(st["state"], "unknown")
        self.assertEqual(st["state_source"], "heartbeat")
        self.assertTrue(st["restart_available"])
        # 文件无效 → disabled + 凭据未配置（不是网络失败、不是 stale），且不因缺 pid 判 stale
        self.cfg.unlink()
        st2 = self._st()
        self.assertEqual(st2["state"], "disabled")
        self.assertEqual(st2["state_source"], "local_file")
        self.assertEqual(st2["pid_status"], "missing")
        self.assertIn("凭据未配置", self.b._feishu_runtime_text(st2))
        self.assertNotIn("网络连接失败", self.b._feishu_runtime_text(st2))
        # 看板显式停用 + 无 feishu 对象：环境优先，不因本地凭据文件缺失改判「凭据未配置」
        # （--seed 测试实例即此形态：REQ_BOARD_NO_FEISHU=1 + 无心跳 + 无凭据文件）
        os.environ["REQ_BOARD_NO_FEISHU"] = "1"
        st3 = self._st()
        self.assertEqual(st3["state"], "disabled")
        self.assertEqual(st3["state_source"], "board_env")
        self.assertIn("REQ_BOARD_NO_FEISHU", st3["reason"])
        self.assertIn("显式停用", self.b._feishu_runtime_text(st3))

    # ---- 32（#40 U11：last_poll_at 缺失/非法 → unknown，不得是 connected）----
    def test_32_missing_or_invalid_last_poll_unknown(self):
        self._write_cfg()
        for raw_hb in ('{"feishu": {"enabled": true, "connected": true, "phase": "connected",'
                       ' "credentials_fingerprint": null}}',
                       '{"last_poll_at": "not-a-number",'
                       ' "feishu": {"enabled": true, "connected": true, "phase": "connected",'
                       ' "credentials_fingerprint": null}}'):
            with self.subTest(raw=raw_hb):
                self.hb.write_text(raw_hb, encoding="utf-8")
                st = self._st()
                self.assertEqual(st["state"], "unknown")
                self.assertEqual(st["state_source"], "heartbeat")
                self.assertNotEqual(st["state"], "connected")
                self.assertNotEqual(st["state"], "stale")
                self.assertTrue(st["restart_available"])   # 文件有效 → 仍保留重启入口

    # ---- 33（#40 U12：健康接口形状 + 判定不依赖 launchctl）----
    def test_33_health_endpoint_shape(self):
        self._write_cfg()
        pid = 4245
        self.b._PID_PROBE_CACHE[pid] = (time.time(), "ok")
        self._write_hb({"enabled": True, "connected": True, "phase": "connected",
                        "credentials_fingerprint": self._fp(), "updated_at": 1700000000.0},
                       extra={"pid": pid})
        client = self.b.app.test_client()
        d = client.get("/api/dispatcher-health").get_json()
        for k in ("ok", "age", "state", "reason", "state_source", "updated_at", "pid",
                  "pid_status", "feishu"):
            self.assertIn(k, d)
        self.assertEqual(d["state"], "connected")
        self.assertEqual(d["pid"], pid)
        self.assertEqual(d["pid_status"], "ok")
        self.assertEqual(d["updated_at"], 1700000000.0)
        self.assertEqual(d["feishu"]["state"], "connected")
        self.assertTrue(d["feishu"]["connected"])       # 评审打回级：接口布尔与 state 不得矛盾
        raw = client.get("/api/dispatcher-health").get_data(as_text=True)
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("fingerprint", raw)
        # 无心跳文件：ok=false、age=null，state 走兜底（不省略）+ pid_status=missing
        self.hb.unlink()
        d2 = client.get("/api/dispatcher-health").get_json()
        self.assertFalse(d2["ok"])
        self.assertIsNone(d2["age"])
        self.assertIn(d2["state"], ("unknown", "disabled"))
        self.assertEqual(d2["pid_status"], "missing")
        # 判定路径不调用 launchctl（按 label 取的是生产调度器 pid，测试实例/临时进程会误判）
        import inspect
        for fn in (self.b._dispatcher_pid_status, self.b._feishu_runtime_status):
            self.assertNotIn("launchctl", inspect.getsource(fn))


if __name__ == "__main__":
    unittest.main()
