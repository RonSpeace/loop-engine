#!/usr/bin/env python3
"""#22 飞书凭据单一源（dispatcher 侧）：feishu.json 取代 .env 两键 + init_feishu fail-soft。

对应用例 45-50：
- 45 feishu.json 缺失 → init_feishu() 返回 None、feishu is None、不抛
- 46 完整 → lark.Client.builder() 以文件值调用
- 47 缺 app_secret / 非法 JSON → None + warn，不抛
- 48 .env 写有两键但无 feishu.json → 仍 None（证明已不读 .env）
- 49 REQ_BOARD_NO_FEISHU=1 → 不读文件、不建 client
- 50 main() 无凭据时不抛（mock 后续启动链），日志含跳过原因

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_feishu_creds_source -v
"""
import json
import shutil
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


class FeishuCredsSource(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="feishu-creds-"))
        self.cfg = self.tmp / "feishu.json"
        self.hb = self.tmp / "heartbeat.json"
        self._orig = {n: getattr(d, n) for n in
                      ("FEISHU_CONFIG_FILE", "HEARTBEAT_FILE", "feishu", "NO_FEISHU")}
        d.FEISHU_CONFIG_FILE = self.cfg
        d.HEARTBEAT_FILE = self.hb          # #23：init_feishu 会写心跳，必须隔离（勿碰 data/）
        d.feishu = None
        d.NO_FEISHU = False

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(d, n, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj, raw=None):
        self.cfg.write_text(raw if raw is not None else json.dumps(obj), encoding="utf-8")

    # ---- 45 ----
    def test_45_missing_file_returns_none(self):
        self.assertFalse(self.cfg.exists())
        with self.assertLogs(d.log, level="WARNING") as cm:
            self.assertIsNone(d.init_feishu())
        self.assertIsNone(d.feishu)
        self.assertTrue(any("飞书" in m for m in cm.output))

    # ---- 46 ----
    def test_46_complete_file_builds_client(self):
        self._write({"app_id": "cli_x", "app_secret": "sec_y"})
        fake_client = mock.Mock()
        fake_client.builder.return_value.app_id.return_value.app_secret.return_value.build.return_value = "CLIENT"
        with mock.patch.object(d.lark, "Client", fake_client):
            cfg = d.init_feishu()
        self.assertEqual(cfg, {"app_id": "cli_x", "app_secret": "sec_y"})
        self.assertEqual(d.feishu, "CLIENT")
        builder = fake_client.builder.return_value
        builder.app_id.assert_called_with("cli_x")
        builder.app_id.return_value.app_secret.assert_called_with("sec_y")

    # ---- 47 ----
    def test_47_incomplete_or_invalid_returns_none(self):
        cases = [
            {"app_id": "cli_x"},                              # 缺 app_secret
            {"app_secret": "sec"},                            # 缺 app_id
            {"app_id": "", "app_secret": ""},                 # 空值
        ]
        for obj in cases:
            with self.subTest(obj=obj):
                d.feishu = None
                self._write(obj)
                with self.assertLogs(d.log, level="WARNING"):
                    self.assertIsNone(d.init_feishu())
                self.assertIsNone(d.feishu)
        # 非法 JSON
        d.feishu = None
        self._write(None, raw="{ not json")
        with self.assertLogs(d.log, level="WARNING"):
            self.assertIsNone(d.init_feishu())
        self.assertIsNone(d.feishu)

    # ---- 48 ----
    def test_48_env_keys_no_longer_read(self):
        """.env 有两键 + 无 feishu.json → 仍 None（凭据只认 feishu.json）"""
        env = REPO / ".env"
        backup = None
        existed = env.exists()
        if existed:
            backup = env.read_text(encoding="utf-8")
        try:
            env.write_text("FEISHU_BOT_APP_ID=cli_env\nFEISHU_BOT_APP_SECRET=sec_env\n", encoding="utf-8")
            self.assertFalse(self.cfg.exists())
            self.assertIsNone(d.load_feishu_config())
            self.assertIsNone(d.init_feishu())
        finally:
            if existed and backup is not None:
                env.write_text(backup, encoding="utf-8")
            elif not existed:
                env.unlink(missing_ok=True)

    # ---- 49 ----
    def test_49_no_feishu_skips_without_reading(self):
        self._write({"app_id": "cli_x", "app_secret": "sec_y"})
        d.NO_FEISHU = True
        with mock.patch.object(d, "load_feishu_config",
                               mock.Mock(side_effect=AssertionError("不应读文件"))) as lf, \
             mock.patch.object(d.lark, "Client",
                               mock.Mock(side_effect=AssertionError("不应建 client"))):
            self.assertIsNone(d.init_feishu())
        lf.assert_not_called()
        self.assertIsNone(d.feishu)

    # ---- 50 ----
    def test_50_main_without_creds_does_not_raise(self):
        """无 feishu.json → main() 启动链不抛，日志含跳过原因、不起 ws 线程"""
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
            "start_feishu_ws": mock.Mock(side_effect=AssertionError("无凭据不应起 ws")),
            "_main_loop_tick": mock.Mock(side_effect=KeyboardInterrupt),
        }
        ctx = [mock.patch.object(d, k, v) for k, v in patches.items()]
        for c in ctx:
            c.start()
        try:
            with self.assertLogs(d.log, level="WARNING") as cm:
                with self.assertRaises(KeyboardInterrupt):
                    d.main()
        finally:
            for c in ctx:
                c.stop()
        joined = "\n".join(cm.output)
        self.assertIn("凭据未配置", joined)
        self.assertTrue("飞书" in joined, joined)
        patches["start_feishu_ws"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
