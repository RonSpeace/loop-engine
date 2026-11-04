#!/usr/bin/env python3
"""#22 飞书机器人设置端点测试（GET/POST /api/feishu-settings + POST /api/feishu-settings/test）。

对应技术方案用例 1-13：
1 GET 无文件 200 空态；2 GET 有文件不回显 secret；3 POST 新建 200 + 0600 + 二次写生成 .bak；
4 app_id 空 → 400 零写入；5 仅带 app_id 保留旧 secret；6 clear_secret 置空；7 非法 app_id 形态；
8 非 JSON body；9 写入异常 500 原文件完好；10-13 连通性测试三态 + 缺省用文件值。

隔离：临时 FEISHU_JSON_PATH（patch 模块属性）+ 临时 DB；绝不触真实仓库根 feishu.json 与 data/board.db。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_feishu_settings_api -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402

SECRET = "SECRET-FEISHU-XYZ"


class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, n=-1):
        return self._payload if n is None or n < 0 else self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FeishuSettingsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req22-fs-"))
        cls._old_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._old_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.path = self.tmp / f"feishu-{self._testMethodName}.json"
        self._old_path = b.FEISHU_JSON_PATH
        b.FEISHU_JSON_PATH = self.path
        # #23：心跳也隔离（本机真实心跳会带 feishu 字段，污染运行态判定）
        self.heartbeat = self.tmp / f"hb-{self._testMethodName}.json"
        self._old_hb = b.HEARTBEAT_FILE
        b.HEARTBEAT_FILE = self.heartbeat
        # 事件表逐用例清空（全局留痕断言不跨用例累积）
        with b.get_db() as conn:
            conn.execute("DELETE FROM agent_events")
            conn.commit()

    def tearDown(self):
        b.FEISHU_JSON_PATH = self._old_path
        b.HEARTBEAT_FILE = self._old_hb

    def _hb(self, feishu=None, last_poll_at=None, with_feishu=True):
        """写心跳文件；feishu=None 且 with_feishu=False 时写「旧版本心跳」（无 feishu 字段）。"""
        hb = {"last_poll_at": time.time() if last_poll_at is None else last_poll_at}
        if with_feishu:
            hb["feishu"] = feishu if feishu is not None else {}
        self.heartbeat.write_text(json.dumps(hb), encoding="utf-8")

    def _hb_state(self, **kwargs):
        base = {"enabled": True, "disabled_reason": None, "connected": False,
                "phase": "connecting", "credentials_source": "feishu.json",
                "credentials_fingerprint": None, "last_error": None, "connected_at": None}
        base.update(kwargs)
        return base

    def _events(self, req_id=None):
        with b.get_db() as conn:
            if req_id is None:
                rows = conn.execute("SELECT req_id, kind, content FROM agent_events"
                                    " ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT req_id, kind, content FROM agent_events"
                                    " WHERE req_id = ? ORDER BY id", (req_id,)).fetchall()
        return [dict(r) for r in rows]

    def _write(self, app_id="cli_test1234", secret=SECRET):
        self.path.write_text(json.dumps({"app_id": app_id, "app_secret": secret}),
                             encoding="utf-8")
        os.chmod(self.path, 0o600)

    def _get(self):
        return self.client.get("/api/feishu-settings")

    def _post(self, body):
        return self.client.post("/api/feishu-settings", json=body)

    def _test_post(self, body):
        return self.client.post("/api/feishu-settings/test", json=body)

    def _snapshot(self):
        bak = self.path.parent / (self.path.name + ".bak")
        return {
            "exists": self.path.exists(),
            "bytes": self.path.read_bytes() if self.path.exists() else None,
            "mode": (self.path.stat().st_mode & 0o777) if self.path.exists() else None,
            "bak": bak.read_bytes() if bak.exists() else None,
        }

    def _assert_zero_write(self, snap):
        self.assertEqual(self.path.exists(), snap["exists"])
        if snap["exists"]:
            self.assertEqual(self.path.read_bytes(), snap["bytes"])
            self.assertEqual(self.path.stat().st_mode & 0o777, snap["mode"])
        bak = self.path.parent / (self.path.name + ".bak")
        self.assertEqual(bak.read_bytes() if bak.exists() else None, snap["bak"])
        self.assertFalse((self.path.parent / (self.path.name + ".tmp")).exists())


class FeishuSettingsGetTest(FeishuSettingsBase):
    def test_01_get_missing_file_empty(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        # #24：响应新增 assistant_model（未配置 null）与 assistant_models（可用别名分组）
        # #26：新增 assistant_file_tools / assistant_status_write 两个能力开关（缺省 false）
        # #30：新增 assistant_req_cards（需求卡清单卡片，缺省 false）
        # #52：新增 assistant_vision（读图/多模态，缺省 true）
        # #54：新增 assistant_git_ops（代码拉取与提交，缺省 false）
        self.assertEqual(set(d), {"path", "app_id", "app_secret_configured", "warnings",
                                  "runtime", "restart_required",
                                  "assistant_model", "assistant_models",
                                  "assistant_file_tools", "assistant_status_write",
                                  "assistant_req_cards", "assistant_git_ops",
                                  "assistant_vision"})
        self.assertFalse(d["assistant_file_tools"])
        self.assertFalse(d["assistant_status_write"])
        self.assertFalse(d["assistant_req_cards"])
        self.assertFalse(d["assistant_git_ops"])
        self.assertIs(d["assistant_vision"], True)
        self.assertIsNone(d["assistant_model"])
        self.assertIsInstance(d["assistant_models"], list)
        self.assertEqual(d["app_id"], "")
        self.assertFalse(d["app_secret_configured"])
        self.assertEqual(d["warnings"], [])
        self.assertEqual(d["path"], str(self.path))
        # #23：无文件 → 停用态，不提供重启入口
        self.assertEqual(d["runtime"]["state"], "disabled")
        self.assertFalse(d["restart_required"])
        self.assertFalse(d["runtime"]["restart_available"])

    def test_02_get_with_file_no_secret_leak(self):
        self._write()
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["app_id"], "cli_test1234")
        self.assertTrue(d["app_secret_configured"])
        raw = resp.get_data(as_text=True)
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("app_secret", raw.replace("app_secret_configured", ""))
        # 解析失败路径也不回显文件内容
        self.path.write_text("SECRET-LEAK-XYZ not-json", encoding="utf-8")
        resp2 = self._get()
        self.assertEqual(resp2.status_code, 200)
        d2 = resp2.get_json()
        self.assertEqual(d2["app_id"], "")
        self.assertFalse(d2["app_secret_configured"])
        self.assertTrue(d2["warnings"])

    def test_02b_invalid_json_warnings_hide_file_content(self):
        # ⑧：解析失败的 warnings 只给固定文案，绝不回显 JSONDecodeError 原文（可能含文件片段）。
        marker = "SECRET-LEAK-XYZ-0987654321"
        self.path.write_text('{"app_id": "cli_x", "app_secret": "%s"' % marker,
                             encoding="utf-8")
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["app_id"], "")
        self.assertFalse(d["app_secret_configured"])
        joined = " ".join(d["warnings"])
        self.assertTrue(joined)
        self.assertNotIn(marker, joined)
        self.assertNotIn("Expecting", joined)
        self.assertIn("解析失败", joined)

    def test_02c_env_path_expands_user(self):
        # ⑧：REQ_BOARD_FEISHU_JSON 覆盖时也 expanduser（与 dispatcher 口径一致）
        import subprocess
        env = dict(os.environ, REQ_BOARD_FEISHU_JSON="~/req22-feishu-env.json")
        proc = subprocess.run([sys.executable, "-c", "import board; print(board.FEISHU_JSON_PATH)"],
                              cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(Path.home() / "req22-feishu-env.json"))


class FeishuSettingsPostTest(FeishuSettingsBase):
    def test_03_post_create_then_bak(self):
        resp = self._post({"app_id": "cli_newapp01", "app_secret": SECRET})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["changed"])
        d = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(d, {"app_id": "cli_newapp01", "app_secret": SECRET})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("需重启调度器生效", resp.get_json()["restart_hint"])
        first_bytes = self.path.read_bytes()
        # 第二次写 → .bak = 上一版
        resp2 = self._post({"app_id": "cli_newapp02", "app_secret": "SECRET-2"})
        self.assertEqual(resp2.status_code, 200)
        bak = self.path.parent / (self.path.name + ".bak")
        self.assertEqual(bak.read_bytes(), first_bytes)
        self.assertEqual(bak.stat().st_mode & 0o777, 0o600)

    def test_04_empty_app_id_zero_write(self):
        self._write()
        snap = self._snapshot()
        resp = self._post({"app_id": ""})
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(snap)

    def test_05_only_app_id_keeps_secret(self):
        self._write(secret=SECRET)
        resp = self._post({"app_id": "cli_changed9"})
        self.assertEqual(resp.status_code, 200)
        d = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(d["app_id"], "cli_changed9")
        self.assertEqual(d["app_secret"], SECRET)

    def test_06_clear_secret(self):
        self._write()
        resp = self._post({"app_id": "cli_test1234", "clear_secret": True})
        self.assertEqual(resp.status_code, 200)
        d = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(d["app_secret"], "")
        self.assertFalse(self._get().get_json()["app_secret_configured"])

    def test_07_illegal_app_id_form(self):
        self._write()
        for bad in ("abc", "cli_", "CLI_x", "cli_ bad", "x" * 130):
            with self.subTest(bad=bad):
                snap = self._snapshot()
                resp = self._post({"app_id": bad})
                self.assertEqual(resp.status_code, 400, bad)
                self._assert_zero_write(snap)

    def test_08_non_json_body(self):
        self._write()
        snap = self._snapshot()
        resp = self.client.post("/api/feishu-settings", data="not json",
                                content_type="text/plain")
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(snap)

    def test_09_write_failure_500(self):
        self._write()
        snap = self._snapshot()
        with mock.patch.object(b.os, "replace", side_effect=OSError("disk boom")):
            resp = self._post({"app_id": "cli_changed9", "app_secret": "SECRET-9"})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.path.read_bytes(), snap["bytes"])
        self.assertFalse((self.path.parent / (self.path.name + ".tmp")).exists())

    def test_09b_invalid_json_only_app_id_400(self):
        # ⑧：文件存在但 JSON 非法 + 未同时给全值 → 400 且**文件字节不变**（绝不用空 secret 覆盖）
        self.path.write_text("not-json SECRET-LEAK-XYZ", encoding="utf-8")
        snap = self._snapshot()
        resp = self._post({"app_id": "cli_rebuild1"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("解析失败", resp.get_json()["error"])
        self._assert_zero_write(snap)

    def test_09c_invalid_json_with_both_rebuilds(self):
        # ⑧：同时给出 app_id + app_secret → 允许重建（覆盖损坏文件）
        self.path.write_text("not-json SECRET-LEAK-XYZ", encoding="utf-8")
        resp = self._post({"app_id": "cli_rebuild2", "app_secret": "NEW-SECRET"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        d = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(d, {"app_id": "cli_rebuild2", "app_secret": "NEW-SECRET"})


class FeishuSettingsTestEndpointTest(FeishuSettingsBase):
    def _capture_urlopen(self, payload, sink=None, exc=None):
        def fake(req, timeout=None):
            if sink is not None:
                sink["url"] = req.full_url
                sink["body"] = json.loads(req.data.decode("utf-8"))
                sink["timeout"] = timeout
            if exc is not None:
                raise exc
            return _FakeResp(payload)
        return fake

    def test_10_test_success(self):
        sink = {}
        with mock.patch.object(b.urllib.request, "urlopen",
                               self._capture_urlopen({"code": 0, "tenant_access_token": "t-xx",
                                                      "expire": 7200}, sink)):
            resp = self._test_post({"app_id": "cli_x", "app_secret": SECRET})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["code"], 0)
        self.assertEqual(d["expire"], 7200)
        self.assertNotIn(SECRET, resp.get_data(as_text=True))
        self.assertEqual(sink["url"], b.FEISHU_TENANT_TOKEN_URL)
        self.assertEqual(sink["timeout"], 10)

    def test_11_test_business_failure(self):
        with mock.patch.object(b.urllib.request, "urlopen",
                               self._capture_urlopen({"code": 10003, "msg": "invalid param"})):
            resp = self._test_post({"app_id": "cli_x", "app_secret": SECRET})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["code"], 10003)
        self.assertEqual(d["msg"], "invalid param")

    def test_12_test_network_error(self):
        with mock.patch.object(b.urllib.request, "urlopen",
                               self._capture_urlopen({}, exc=urllib.error.URLError("boom"))):
            resp = self._test_post({"app_id": "cli_x", "app_secret": SECRET})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertFalse(d["ok"])
        self.assertIsNone(d["code"])
        self.assertTrue(d["msg"].startswith("网络错误"), d["msg"])

    def test_13_test_uses_stored_credentials(self):
        self._write(app_id="cli_stored77", secret=SECRET)
        sink = {}
        with mock.patch.object(b.urllib.request, "urlopen",
                               self._capture_urlopen({"code": 0, "expire": 100}, sink)):
            resp = self._test_post({})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(sink["body"]["app_id"], "cli_stored77")
        self.assertEqual(sink["body"]["app_secret"], SECRET)
        self.assertNotIn(SECRET, resp.get_data(as_text=True))

    def test_13b_missing_credentials_no_call(self):
        called = []
        with mock.patch.object(b.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: called.append(1)):
            resp = self._test_post({})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertFalse(d["ok"])
        self.assertIsNone(d["code"])
        self.assertEqual(called, [])


class FeishuRuntimeStatusTest(FeishuSettingsBase):
    """#23 运行态判定与设置 API 返回（tech §7.2 用例 25-30、42-43）。"""

    def _fp(self, app_id="cli_test1234", secret=SECRET):
        return b._feishu_credential_fingerprint(app_id, secret)

    # ---- 25/26 ----
    def test_25_26_get_runtime_not_loaded(self):
        self._write()
        self._hb(self._hb_state(credentials_fingerprint=None))
        d = self._get().get_json()
        self.assertEqual(d["runtime"]["state"], "not_loaded")
        self.assertFalse(d["runtime"]["connected"])     # 评审打回级：布尔与 state 一致（非 connected 恒 false）
        self.assertTrue(d["restart_required"])
        self.assertTrue(d["runtime"]["restart_available"])
        # 评审必改 3：接口**不回传任何凭据指纹**（只用于服务端内部比对）
        raw = self._get().get_data(as_text=True)
        self.assertNotIn("fingerprint", raw)
        self.assertNotIn(self._fp(), raw)
        self.assertNotIn(SECRET, raw)
        # 指纹一致 + connected → 已连接且无按钮
        self._hb(self._hb_state(connected=True, phase="connected",
                                credentials_fingerprint=self._fp()))
        d2 = self._get().get_json()
        self.assertEqual(d2["runtime"]["state"], "connected")
        self.assertTrue(d2["runtime"]["connected"])     # 评审打回级：state=connected → 布尔为 true
        self.assertFalse(d2["runtime"]["restart_available"])
        self.assertFalse(d2["restart_required"])

    def test_06_json_formatting_does_not_require_restart(self):
        """用例 6：相同凭据不同 JSON 排版（紧凑/缩进/键序不同）→ 指纹相同，不要求重启。"""
        self._hb(self._hb_state(connected=True, phase="connected",
                                credentials_fingerprint=self._fp()))
        self.path.write_text('{"app_id":"cli_test1234","app_secret":"%s"}' % SECRET,
                             encoding="utf-8")
        d1 = self._get().get_json()
        self.assertEqual(d1["runtime"]["state"], "connected")
        self.assertFalse(d1["restart_required"])
        self.path.write_text(json.dumps({"app_secret": SECRET, "app_id": "cli_test1234"},
                                        ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        d2 = self._get().get_json()
        self.assertEqual(d2["runtime"]["state"], "connected")   # 排版变化不触发「尚未加载」
        self.assertFalse(d2["restart_required"])
        # 值变一个字节 → 触发未加载
        self.path.write_text(json.dumps({"app_id": "cli_test1234", "app_secret": SECRET + "x"}),
                             encoding="utf-8")
        self.assertEqual(self._get().get_json()["runtime"]["state"], "not_loaded")

    # ---- 27 ----
    def test_27_post_new_cred_restart_required(self):
        self._hb(self._hb_state(credentials_fingerprint=None))
        d = self._post({"app_id": "cli_newapp01", "app_secret": SECRET}).get_json()
        self.assertTrue(d["changed"])
        self.assertTrue(d["restart_required"])
        self.assertEqual(d["runtime"]["state"], "not_loaded")

    # ---- 28（打回级主路径回归）----
    def test_28_post_after_startup_without_creds(self):
        """调度器启动时无凭据（enabled=false/missing，指纹 null）+ 随后保存有效凭据
        → 必须判 not_loaded（而非停用），按钮可见。"""
        self._hb(self._hb_state(enabled=False, disabled_reason="missing",
                                phase="disabled", credentials_fingerprint=None))
        d = self._post({"app_id": "cli_recover01", "app_secret": SECRET}).get_json()
        self.assertEqual(d["runtime"]["state"], "not_loaded")
        self.assertTrue(d["restart_required"])

    # ---- 29 ----
    def test_29_unchanged_creds_still_restart_required(self):
        self._hb(self._hb_state(credentials_fingerprint=None))
        self._post({"app_id": "cli_test1234", "app_secret": SECRET})   # 首次落盘（规范格式）
        d = self._post({"app_id": "cli_test1234", "app_secret": SECRET}).get_json()
        self.assertFalse(d["changed"])
        self.assertTrue(d["restart_required"])

    # ---- 30 ----
    def test_30_clear_secret_shows_disabled(self):
        self._write()
        self._hb(self._hb_state(connected=True, phase="connected",
                                credentials_fingerprint=self._fp()))
        d = self._post({"app_id": "cli_test1234", "clear_secret": True}).get_json()
        self.assertEqual(d["runtime"]["state"], "disabled")
        self.assertFalse(d["runtime"]["connected"])
        self.assertNotEqual(d["runtime"]["state"], "connected")

    # ---- 42 ----
    def test_42_dispatcher_health_has_feishu(self):
        self._write()
        self._hb(self._hb_state(connected=True, phase="connected",
                                credentials_fingerprint=self._fp()))
        d = self.client.get("/api/dispatcher-health").get_json()
        for k in ("ok", "age", "last_shutdown", "auto_restart_ok", "board_restart_pending"):
            self.assertIn(k, d)
        self.assertEqual(d["feishu"]["state"], "connected")
        self.assertTrue(d["feishu"]["connected"])       # 评审打回级：健康接口布尔与 state 一致
        self._hb(with_feishu=False)
        d2 = self.client.get("/api/dispatcher-health").get_json()
        self.assertIn("ok", d2)
        self.assertEqual(d2["feishu"]["state"], "unknown")

    # ---- 43 ----
    def test_43_tenant_token_success_is_not_connected(self):
        self._write()
        self._hb(self._hb_state(credentials_fingerprint=None))
        with mock.patch.object(b.urllib.request, "urlopen",
                               self._capture_urlopen({"code": 0, "expire": 100})):
            t = self._test_post({"app_id": "cli_test1234", "app_secret": SECRET}).get_json()
        self.assertTrue(t["ok"])
        self.assertEqual(self._get().get_json()["runtime"]["state"], "not_loaded")

    def _capture_urlopen(self, payload, sink=None):
        payload_b = json.dumps(payload).encode("utf-8")

        class _R:
            def read(self, n=-1):
                return payload_b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake(req, timeout=None, **kw):
            if sink is not None:
                sink["url"] = getattr(req, "full_url", None)
                sink["body"] = json.loads(getattr(req, "data", b"{}").decode("utf-8"))
                sink["timeout"] = timeout
            return _R()

        return _fake


class FeishuRestartApiTest(FeishuSettingsBase):
    """#23 重启端点（tech §7.2 用例 33-41）：预检/拒绝/失败/成功 + 零真实进程调用 + 全局留痕。"""

    def _no_real_proc(self):
        """断言测试中零真实 launchctl / os.kill / ps 调用。"""
        return [mock.patch.object(b.subprocess, "run",
                                  side_effect=AssertionError("不得执行真实进程命令")),
                mock.patch.object(b.os, "kill",
                                  side_effect=AssertionError("不得发送真实信号"))]

    def _restart(self, body):
        return self.client.post("/api/feishu-settings/restart", json=body)

    # ---- 33 ----
    def test_33_missing_confirm_400(self):
        self._write()
        for body in ({}, {"confirm": False}, {"confirm": "true"}):
            with self.subTest(body=body):
                resp = self._restart(body)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._events(), [])

    # ---- 34 ----
    def test_34_running_agent_rejected_409(self):
        self._write()
        self._hb(self._hb_state(credentials_fingerprint=None))
        with mock.patch.object(b, "_dispatcher_kimi_agents",
                               return_value=["kimi acp --req 7"]):
            resp = self._restart({"confirm": True})
        self.assertEqual(resp.status_code, 409)
        d = resp.get_json()
        self.assertIn("1", d["error"])
        self.assertIn("拒绝重启", d["error"])
        self.assertEqual(d["agents"], 1)
        ev = self._events(0)
        self.assertEqual(len(ev), 1)
        self.assertIn("拒绝重启", ev[0]["content"])
        # 未加载态不因拒绝而改变，按钮仍在
        self.assertTrue(self._get().get_json()["restart_required"])

    # ---- 36 ----
    def test_36_proc_tree_error_fail_closed(self):
        self._write()
        with mock.patch.object(b, "_dispatcher_kimi_agents",
                               side_effect=RuntimeError("ps 失败")):
            resp = self._restart({"confirm": True})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("无法确认", resp.get_json()["error"])
        self.assertEqual(len(self._events(0)), 1)

    # ---- 37 ----
    def test_37_success_path_and_global_event(self):
        self._write()
        self._hb(self._hb_state(credentials_fingerprint=None))
        with mock.patch.object(b, "_dispatcher_kimi_agents", return_value=[]), \
             mock.patch.object(b, "_restart_dispatcher",
                               return_value={"restarted": True, "label": "local.req-board-dispatcher",
                                             "health": {"ok": True, "age": 1},
                                             "heartbeat_updated": True,
                                             "detail": "心跳文件已更新"}) as rd:
            resp = self._restart({"confirm": True})
        self.assertEqual(resp.status_code, 200)
        rd.assert_called_once_with("feishu-settings")     # 来源标记固定
        d = resp.get_json()
        self.assertTrue(d["restarted"])
        self.assertIn("runtime", d)
        ev = self._events(0)
        self.assertEqual(len(ev), 1)
        self.assertIn("feishu-settings", ev[0]["content"])
        self.assertNotIn(SECRET, ev[0]["content"])

    # ---- 35 ----
    def test_35_multiple_running_agents_rejected(self):
        """用例 35：多个运行中 agent → 409，错误含数量、命令行仅截断展示前 3 条、不写 secret。"""
        self._write()
        self._hb(self._hb_state(credentials_fingerprint=None))
        agents = [f"kimi acp --req {i}" for i in range(1, 6)]
        with mock.patch.object(b, "_dispatcher_kimi_agents", return_value=agents):
            resp = self._restart({"confirm": True})
        self.assertEqual(resp.status_code, 409)
        d = resp.get_json()
        self.assertEqual(d["agents"], 5)
        self.assertIn("5", d["error"])
        self.assertIn("kimi acp --req 1", d["error"])          # 第一条完整展示（截断 100 字）
        self.assertNotIn("kimi acp --req 4", d["error"])       # 第 4 条起不再展示
        self.assertNotIn(SECRET, d["error"])
        self.assertEqual(len(self._events(0)), 1)

    # ---- 38 ----
    def test_38_restart_failure_502(self):
        self._write()
        with mock.patch.object(b, "_dispatcher_kimi_agents", return_value=[]), \
             mock.patch.object(b, "_restart_dispatcher",
                               side_effect=ValueError("launchctl kickstart 退出码 5")):
            resp = self._restart({"confirm": True})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("退出码 5", resp.get_json()["error"])
        self.assertEqual(len(self._events(0)), 1)

    # ---- 39/40/41 ----
    def test_39_stub_env_zero_real_calls(self):
        """REQ_BOARD_FEISHU_RESTART_STUB=1：不触发真实 launchctl/os.kill，成功返回 runtime。"""
        self._write()
        self._hb(self._hb_state(credentials_fingerprint=None))
        with mock.patch.dict(os.environ, {"REQ_BOARD_FEISHU_RESTART_STUB": "1"}):
            with self._no_real_proc()[0], self._no_real_proc()[1]:
                with mock.patch.object(b, "_dispatcher_kimi_agents", return_value=[]):
                    resp = self._restart({"confirm": True})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["label"], "stub")
        self.assertEqual(d["runtime"]["state"], "not_loaded")

    def test_40_global_event_not_in_requirement_timeline(self):
        """req_id=0 哨兵行不出现在任何真实需求的事件时间线里。"""
        with b.get_db() as conn:
            conn.execute("INSERT INTO requirements (title, project, status, notes, created_at, updated_at)"
                         " VALUES ('t', 'req-board', 'later', '', ?, ?)",
                         (b.now_str(), b.now_str()))
            req_id = conn.execute("SELECT MAX(id) FROM requirements").fetchone()[0]
            conn.commit()
        b._log_global_agent_event("全局调度器动作（来源 feishu-settings）")
        with b.get_db() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM agent_events WHERE req_id = ?",
                                (req_id,)).fetchone()[0]
        self.assertEqual(rows, 0)
        ev = self._events(0)
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["content"])
        self.assertNotIn(SECRET, ev[0]["content"])


class FeishuSettingsCapSwitchTest(FeishuSettingsBase):
    """#26 看板助手能力开关：GET 回读 / POST 显式落盘 / 缺省不写 / 类型校验零写入 / 不影响凭据指纹。"""

    def test_50_get_reflects_switch(self):
        self._write()
        self.assertEqual(self._get().get_json()["assistant_file_tools"], False)
        d = json.loads(self.path.read_text(encoding="utf-8"))
        d["assistant_file_tools"] = True
        self.path.write_text(json.dumps(d), encoding="utf-8")
        body = self._get().get_json()
        self.assertIs(body["assistant_file_tools"], True)
        self.assertIs(body["assistant_status_write"], False)   # 未配置 = 关闭

    def test_51_post_explicit_switch_writes_and_toggles(self):
        self._write()
        resp = self._post({"app_id": "cli_test1234",
                           "assistant_file_tools": True, "assistant_status_write": True})
        self.assertEqual(resp.status_code, 200)
        d = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(d["assistant_file_tools"], True)
        self.assertIs(d["assistant_status_write"], True)
        self.assertEqual(d["app_secret"], SECRET)        # 凭据保留
        back = self._get().get_json()
        self.assertIs(back["assistant_file_tools"], True)
        self._post({"app_id": "cli_test1234", "assistant_file_tools": False})
        d2 = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(d2["assistant_file_tools"], False)
        self.assertIs(d2["assistant_status_write"], True)   # 缺省字段保留旧值
        self.assertIs(self._get().get_json()["assistant_status_write"], True)

    def test_52_post_without_switch_keeps_file_byte_exact(self):
        resp = self._post({"app_id": "cli_newapp01", "app_secret": SECRET})
        self.assertEqual(resp.status_code, 200)
        d = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(d, {"app_id": "cli_newapp01", "app_secret": SECRET})

    def test_53_non_bool_switch_zero_write(self):
        self._write()
        for bad in ("true", 1, None, [], {}):
            snap = self._snapshot()
            resp = self._post({"app_id": "cli_test1234", "assistant_file_tools": bad})
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn("assistant_file_tools", resp.get_json()["error"])
            self._assert_zero_write(snap)

    def test_54_switch_does_not_change_credential_fingerprint(self):
        self._write()
        self._hb(self._hb_state(connected=True, phase="connected",
                                credentials_fingerprint=self._fp()))
        self.assertFalse(self._get().get_json()["restart_required"])
        self._post({"app_id": "cli_test1234", "assistant_file_tools": True})
        d = self._get().get_json()
        self.assertIs(d["assistant_file_tools"], True)
        self.assertEqual(d["runtime"]["state"], "connected")   # 只改开关 → 不算凭据变更
        self.assertFalse(d["restart_required"])

    def test_55_req_cards_switch_roundtrip_and_guards(self):
        """#30 需求卡清单卡片开关：POST 落盘 → GET 回读 true；非布尔 400 零写入；不改指纹。"""
        self._write()
        self._hb(self._hb_state(connected=True, phase="connected",
                                credentials_fingerprint=self._fp()))
        resp = self._post({"app_id": "cli_test1234", "assistant_req_cards": True})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(json.loads(self.path.read_text(encoding="utf-8"))["assistant_req_cards"], True)
        back = self._get().get_json()
        self.assertIs(back["assistant_req_cards"], True)
        self.assertFalse(back["restart_required"])              # 只改开关 → 不翻转 restart_required
        for bad in ("true", 1, None, [], {}):
            snap = self._snapshot()
            r = self._post({"app_id": "cli_test1234", "assistant_req_cards": bad})
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("assistant_req_cards", r.get_json()["error"])
            self._assert_zero_write(snap)
        # 缺省字段保留旧值（true 不被清掉）
        self._post({"app_id": "cli_test1234", "assistant_file_tools": True})
        self.assertIs(json.loads(self.path.read_text(encoding="utf-8"))["assistant_req_cards"], True)

    def test_56_vision_switch_default_on_and_explicit_off(self):
        """#52 读图开关：GET 缺键/文件缺失默认 true；POST 显式 false 落盘回读；非布尔 400 零写入。"""
        self.assertIs(self._get().get_json()["assistant_vision"], True)   # 文件缺失 = 缺省开启
        self._write()
        self.assertIs(self._get().get_json()["assistant_vision"], True)   # 缺键 = 开启
        resp = self._post({"app_id": "cli_test1234", "assistant_vision": False})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(json.loads(self.path.read_text(encoding="utf-8"))["assistant_vision"], False)
        self.assertIs(self._get().get_json()["assistant_vision"], False)  # 显式 false 才关闭
        self._post({"app_id": "cli_test1234", "assistant_vision": True})
        self.assertIs(self._get().get_json()["assistant_vision"], True)
        for bad in ("false", 0, None, [], {}):
            snap = self._snapshot()
            r = self._post({"app_id": "cli_test1234", "assistant_vision": bad})
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("assistant_vision", r.get_json()["error"])
            self._assert_zero_write(snap)
        # 缺省字段保留旧值（true 不被清掉）
        self._post({"app_id": "cli_test1234", "assistant_file_tools": True})
        self.assertIs(json.loads(self.path.read_text(encoding="utf-8"))["assistant_vision"], True)

    def test_57_git_ops_switch_roundtrip_and_guards(self):
        """#54 代码拉取与提交开关：POST 落盘 → GET 回读 true；非布尔 400 零写入；不改指纹。"""
        self._write()
        self._hb(self._hb_state(connected=True, phase="connected",
                                credentials_fingerprint=self._fp()))
        resp = self._post({"app_id": "cli_test1234", "assistant_git_ops": True})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(json.loads(self.path.read_text(encoding="utf-8"))["assistant_git_ops"], True)
        back = self._get().get_json()
        self.assertIs(back["assistant_git_ops"], True)
        self.assertFalse(back["restart_required"])
        for bad in ("true", 1, None, [], {}):
            snap = self._snapshot()
            r = self._post({"app_id": "cli_test1234", "assistant_git_ops": bad})
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("assistant_git_ops", r.get_json()["error"])
            self._assert_zero_write(snap)
        self._post({"app_id": "cli_test1234", "assistant_file_tools": True})
        self.assertIs(json.loads(self.path.read_text(encoding="utf-8"))["assistant_git_ops"], True)

    def _fp(self):
        return b._feishu_credential_fingerprint("cli_test1234", SECRET)


if __name__ == "__main__":
    unittest.main()
