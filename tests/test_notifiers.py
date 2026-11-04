#!/usr/bin/env python3
"""可插拔通知渠道（notifiers/）测试。

N1 注册表：配置渠道按事件订阅扇出、后台线程发送不阻塞调用方、mtime 变化自动重载、飞书同步渠道异常原样抛出；
N2 Webhook：请求体字段、HMAC-SHA256 签名可被接收方复算、真实本地 HTTP 端到端（含签名头）、失败信息不含 URL；
N3 Slack：Block Kit 结构（header / 链接 section / context / 按钮）、mrkdwn 转义、纯文本消息；
N4 fail-closed：文件损坏 / 顶层非对象 / 单渠道非法只停用对应渠道、不抛；
N5 调度器接入：notify_req_card 飞书路径逐字不变 + 配置渠道收到结构化事件；飞书入站处理期间回执不扇出；
N6 端点：GET 空态 / 不回显密钥与完整 URL；POST 校验零写入、0600、保留旧值、保留未知字段；test 端点不泄 URL；
N7 前端：通知渠道 tab 接线（tab 文案、两个加载器、按钮委托、深链 ?req=）。

隔离：notifiers.json 一律指向临时目录（patch board.NOTIFIERS_JSON_PATH / 注册表 config_file），
HTTP 只连本进程起的 127.0.0.1 临时服务；不触真实仓库根配置与 data/board.db。
"""
import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")

from tests import _baseline  # noqa: E402

_baseline.pin()

import notifiers  # noqa: E402
from notifiers import slack as ntf_slack  # noqa: E402
from notifiers import webhook as ntf_webhook  # noqa: E402

SECRET = "WEBHOOK-SECRET-XYZ"
SLACK_URL = "https://hooks.slack.com/services/T000/B000/SLACKTOKENXYZ"
HOOK_URL = "https://example.com/hook/path?token=HOOKTOKENXYZ"


class _Capture(BaseHTTPRequestHandler):
    """本地接收端：记录请求头与原始请求体，按 server.status 回码。"""

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        self.server.got.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _serve(status=200):
    srv = HTTPServer(("127.0.0.1", 0), _Capture)
    srv.got = []
    srv.status = status
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/hook"


class _Rec(notifiers.Notifier):
    """测试用异步渠道：记录收到的事件。"""
    name = "rec"

    def __init__(self, events=None, delay=0.0):
        super().__init__(events)
        self.got = []
        self.delay = delay

    def deliver(self, event):
        if self.delay:
            time.sleep(self.delay)
        self.got.append(event)
        return True, "ok"


def _write(path, obj):
    Path(path).write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ntf-reg-"))
        self.cfg = self.tmp / "notifiers.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reg(self):
        return notifiers.NotifierRegistry(config_file=self.cfg, board_url="http://board.local")

    def test_N1_fanout_by_subscription_and_non_blocking(self):
        reg = self._reg()
        recs = {"webhook": _Rec(events=["done", "message"], delay=0.3), "slack": _Rec(events=["failed"])}
        _write(self.cfg, {"channels": {"webhook": {"enabled": True, "url": "http://x/y"},
                                       "slack": {"enabled": True, "webhook_url": "https://h/s"}}})
        with mock.patch.object(notifiers, "build_channels", lambda parsed, bu: dict(recs)):
            t0 = time.monotonic()
            reg.send_event("done", {"id": 7, "title": "T", "project": "p", "status": "done"},
                           title="开发完成", details={"summary": "ok"})
            reg.send_text("hello")
            reg.send_event("failed", {"id": 8, "title": "F"}, title="自动开发失败",
                           details={"retries": 2, "reason": "boom"})
            self.assertLess(time.monotonic() - t0, 0.25, "扇出不应等待慢渠道（后台线程发送）")
            self.assertTrue(reg.flush(5))
        self.assertEqual([e.kind for e in recs["webhook"].got], ["done", "message"])
        self.assertEqual([e.kind for e in recs["slack"].got], ["failed"])
        ev = recs["webhook"].got[0]
        self.assertEqual(ev.requirement["url"], "http://board.local/?req=7")
        self.assertIn("ok", ev.text)
        self.assertIn("boom", recs["slack"].got[0].text)

    def test_N1b_reload_on_mtime_change(self):
        reg = self._reg()
        made = []
        with mock.patch.object(notifiers, "build_channels",
                               lambda parsed, bu: made.append(sorted(parsed["channels"])) or {}):
            reg.send_text("a")                       # 无文件：无配置渠道
            _write(self.cfg, {"channels": {"webhook": {"enabled": True, "url": "http://x/y"}}})
            reg.send_text("b")                       # 新文件 → 重载
            reg.send_text("c")                       # 未变 → 不重载
            st = self.cfg.stat()
            _write(self.cfg, {"channels": {"slack": {"enabled": True, "webhook_url": "https://h/s"}}})
            os.utime(self.cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            reg.send_text("d")
        self.assertEqual(made, [[], ["webhook"], ["slack"]])   # 首次（无文件）→ 新文件 → 改文件，各重载一次

    def test_N1c_sync_channel_errors_propagate_others_isolated(self):
        reg = self._reg()
        boom = notifiers.CallableNotifier("feishu", on_text=mock.Mock(side_effect=RuntimeError("x")),
                                          on_event=mock.Mock())
        reg.register(boom)
        with self.assertRaises(RuntimeError):
            reg.send_text("hi")
        quiet = notifiers.CallableNotifier("other", on_text=mock.Mock(side_effect=RuntimeError("y")),
                                           on_event=mock.Mock(), raise_errors=False)
        reg2 = self._reg()
        reg2.register(quiet)
        reg2.send_text("hi")    # 不抛

    def test_N1d_inbound_scope_skips_configured_channels_only(self):
        reg = self._reg()
        rec = _Rec()
        fs = notifiers.CallableNotifier("feishu", on_text=mock.Mock(), on_event=mock.Mock())
        reg.register(fs)
        _write(self.cfg, {"channels": {"webhook": {"enabled": True, "url": "http://x/y"}}})
        with mock.patch.object(notifiers, "build_channels", lambda parsed, bu: {"webhook": rec}):
            with reg.inbound_scope():
                reg.send_text("y/n 回执")
            reg.flush(5)
            self.assertEqual(rec.got, [])
            fs._on_text.assert_called_once_with("y/n 回执")
            reg.send_text("普通回执")
            reg.flush(5)
        self.assertEqual([e.text for e in rec.got], ["普通回执"])

    def test_N1e_queue_full_drops_without_blocking(self):
        rec = _Rec(delay=0.05)
        w = notifiers._Worker(rec, mock.Mock())
        with mock.patch.object(w.q, "put_nowait", side_effect=__import__("queue").Full):
            self.assertFalse(w.put(notifiers.NotifyEvent("message", "", "x")))
        w.stop()

    def test_N1f_config_path_rules(self):
        with mock.patch.dict(os.environ, {"REQ_BOARD_NOTIFIERS_JSON": "/a/b.json"}):
            self.assertEqual(notifiers.config_path("/repo"), Path("/a/b.json"))
        env = {k: v for k, v in os.environ.items() if k != "REQ_BOARD_NOTIFIERS_JSON"}
        env["REQ_BOARD_FEISHU_JSON"] = "/seed/feishu.json"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(notifiers.config_path("/repo"), Path("/seed/notifiers.json"))
        env.pop("REQ_BOARD_FEISHU_JSON")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(notifiers.config_path("/repo"), Path("/repo/notifiers.json"))


class WebhookTest(unittest.TestCase):
    def _event(self):
        return notifiers.NotifyEvent(
            kind="need_confirm", title="需要确认", text="要不要继续？",
            requirement={"id": 3, "title": "T", "project": "p", "status": "doing",
                         "url": "http://b/?req=3"},
            details={"question": "要不要继续？"}, raw={"req": object(), "kw": {"x": object()}})

    def test_N2_payload_fields_and_no_raw(self):
        p = ntf_webhook.build_payload(self._event(), now=1_700_000_000)
        self.assertEqual(p["source"], "loop-engine")
        self.assertEqual(p["schema"], 1)
        self.assertEqual(p["event"], "need_confirm")
        self.assertEqual(p["requirement"]["id"], 3)
        self.assertEqual(p["details"], {"question": "要不要继续？"})
        self.assertEqual(p["sent_at"], "2023-11-14T22:13:20+00:00")
        self.assertNotIn("raw", p)
        json.dumps(p)   # 可序列化

    def test_N2b_signature_verifiable(self):
        body, headers = ntf_webhook.build_request("http://x", self._event(), secret=SECRET, now=1_700_000_000)
        self.assertEqual(headers["X-Loop-Timestamp"], "1700000000")
        want = "sha256=" + hmac.new(SECRET.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(headers["X-Loop-Signature"], want))
        self.assertEqual(headers["X-Loop-Event"], "need_confirm")
        self.assertNotIn(SECRET, body.decode("utf-8"))
        _body, plain = ntf_webhook.build_request("http://x", self._event(), secret="")
        self.assertNotIn("X-Loop-Signature", plain)

    def test_N2c_end_to_end_local_http(self):
        srv, url = _serve(200)
        try:
            ok, info = ntf_webhook.WebhookNotifier(url, secret=SECRET, timeout=5).deliver(self._event())
            self.assertTrue(ok, info)
            got = srv.got[0]
            ts = got["headers"]["X-Loop-Timestamp"]
            want = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + got["body"],
                                        hashlib.sha256).hexdigest()
            self.assertEqual(got["headers"]["X-Loop-Signature"], want)
            self.assertEqual(json.loads(got["body"])["event"], "need_confirm")
            srv.status = 500
            ok, info = ntf_webhook.WebhookNotifier(url + "?token=SEKRIT", timeout=5).deliver(self._event())
            self.assertFalse(ok)
            self.assertEqual(info, "HTTP 500")
        finally:
            srv.shutdown()
        ok, info = ntf_webhook.post_json("http://127.0.0.1:1/x?token=SEKRIT", b"{}", {}, 2)
        self.assertFalse(ok)
        self.assertNotIn("SEKRIT", info)
        self.assertNotIn("127.0.0.1", info)


class SlackTest(unittest.TestCase):
    def test_N3_block_kit_card(self):
        ev = notifiers.NotifyEvent(
            kind="merge_conflict", title="自动合并冲突", text="冲突仓库：a <b> & c",
            requirement={"id": 12, "title": "修 <bug> & 改", "project": "proj", "status": "later",
                         "url": "https://board.example.com/?req=12"})
        p = ntf_slack.build_payload(ev)
        types_ = [b["type"] for b in p["blocks"]]
        self.assertEqual(types_, ["header", "section", "context", "actions"])
        self.assertEqual(p["blocks"][0]["text"]["text"], "需求 #12 自动合并冲突")
        sec = p["blocks"][1]["text"]["text"]
        self.assertIn("<https://board.example.com/?req=12|#12 修 &lt;bug&gt; &amp; 改>", sec)
        self.assertIn("a &lt;b&gt; &amp; c", sec)
        btn = p["blocks"][3]["elements"][0]
        self.assertEqual(btn["url"], "https://board.example.com/?req=12")
        self.assertIn("proj", p["blocks"][2]["elements"][0]["text"])
        self.assertTrue(p["text"].startswith("需求 #12 自动合并冲突"))

    def test_N3b_plain_message_and_limits(self):
        p = ntf_slack.build_payload(notifiers.NotifyEvent(kind="message", title="", text="x" * 5000))
        self.assertEqual([b["type"] for b in p["blocks"]], ["section"])
        self.assertLessEqual(len(p["blocks"][0]["text"]["text"]), notifiers.TEXT_LIMIT)
        t = ntf_slack.build_payload(notifiers.NotifyEvent(kind="test", title="T" * 300, text="hi"))
        self.assertLessEqual(len(t["blocks"][0]["text"]["text"]), ntf_slack.HEADER_LIMIT)

    def test_N3c_deliver_posts_json(self):
        srv, url = _serve(200)
        try:
            ok, _ = ntf_slack.SlackNotifier(url, timeout=5).deliver(
                notifiers.NotifyEvent(kind="message", title="", text="hi"))
            self.assertTrue(ok)
            self.assertEqual(json.loads(srv.got[0]["body"])["text"], "hi")
        finally:
            srv.shutdown()


class FailClosedTest(unittest.TestCase):
    def test_N4_parse_rules(self):
        self.assertEqual(notifiers.parse_config("x")["errors"]["_"], "顶层需为 JSON 对象")
        self.assertIn("_", notifiers.parse_config({"channels": []})["errors"])
        p = notifiers.parse_config({"board_url": "ftp://x", "channels": {
            "webhook": {"enabled": True},
            "slack": {"enabled": True, "webhook_url": "http://hooks.slack.com/x"},
            "pager": {}}})
        self.assertEqual(p["channels"], {})
        self.assertIn("url", p["errors"]["webhook"])
        self.assertIn("https", p["errors"]["slack"])
        self.assertIn("pager", p["errors"])
        self.assertIn("board_url", p["errors"])
        bad = [{"enabled": "yes", "url": "http://x"}, {"enabled": True, "url": "http://x", "events": ["nope"]},
               {"enabled": True, "url": "http://x", "timeout": 0}, {"enabled": True, "url": "javascript:x"},
               {"enabled": True, "url": "http://x", "secret": 5}]
        for raw in bad:
            spec, why = notifiers.parse_channel("webhook", raw)
            self.assertIsNone(spec, raw)
            self.assertTrue(why)
        ok, why = notifiers.parse_channel("webhook", {"enabled": False, "url": ""})
        self.assertIsNone(why)
        self.assertFalse(ok["enabled"])

    def test_N4b_registry_never_raises_on_bad_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="ntf-bad-"))
        try:
            cfg = tmp / "notifiers.json"
            reg = notifiers.NotifierRegistry(config_file=cfg, logger=mock.Mock())
            for content in ("{not json", "[]", json.dumps({"channels": {"webhook": {"enabled": True}}})):
                _write(cfg, content)
                reg.reload(force=True)
                reg.send_text("x")
                reg.send_event("done", {"id": 1}, title="t")
                self.assertEqual(reg.status()["channels"], [])
                self.assertTrue(reg.status()["errors"])
            with mock.patch.object(notifiers, "build_channels", side_effect=ValueError("boom")):
                _write(cfg, json.dumps({"channels": {}}) + " ")
                reg.reload(force=True)
                self.assertIn("_", reg.status()["errors"])
            # 一个渠道非法，另一个照常加载
            _write(cfg, {"channels": {"webhook": {"enabled": True},
                                      "slack": {"enabled": True, "webhook_url": "https://h/s"}}})
            reg.reload(force=True)
            self.assertEqual(reg.status()["channels"], ["slack"])
            self.assertIn("webhook", reg.status()["errors"])
            reg.reload(force=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_N4c_mask_url(self):
        self.assertEqual(notifiers.mask_url(SLACK_URL), "https://hooks.slack.com/…")
        self.assertEqual(notifiers.mask_url("https://u:p@h:8443/x?t=1"), "https://h:8443/…")
        self.assertEqual(notifiers.mask_url("nope"), "")


class DispatcherIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import dispatcher as d
        cls.d = d

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ntf-disp-"))
        self.rec = _Rec()
        self._old_cfg = self.d.NOTIFIERS.config_file
        self.d.NOTIFIERS.config_file = self.tmp / "notifiers.json"
        _write(self.d.NOTIFIERS.config_file, {"channels": {"webhook": {"enabled": True, "url": "http://x/y"}}})
        self._p = mock.patch.object(notifiers, "build_channels", lambda parsed, bu: {"webhook": self.rec})
        self._p.start()
        self.d.NOTIFIERS.reload(force=True)

    def tearDown(self):
        self._p.stop()
        self.d.NOTIFIERS.config_file = self._old_cfg
        self.d.NOTIFIERS.reload(force=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_N5_card_feishu_unchanged_plus_structured_event(self):
        d = self.d
        req = {"id": 41, "project": "req-board", "title": "标题"}
        with mock.patch.object(d, "_send_feishu", return_value=True) as sf, \
                mock.patch.object(d, "artifact_dir", lambda r: d.ARTIFACTS_ROOT / "x"):
            d.notify_req_card("need_confirm", req, question="继续吗？", phase="dev")
            d.NOTIFIERS.flush(5)
        sf.assert_called_once_with("interactive", d.build_req_card("need_confirm", req, question="继续吗？",
                                                                   phase="dev"))
        ev = self.rec.got[0]
        self.assertEqual(ev.kind, "need_confirm")
        self.assertEqual(ev.title, d.CARD_EVENTS["need_confirm"]["title"])
        self.assertEqual(ev.details["phase"], d._phase_name("dev"))
        self.assertIn("继续吗？", ev.text)
        self.assertTrue(ev.requirement["url"].endswith("/?req=41"))

    def test_N5b_feishu_fallback_path_unchanged(self):
        d = self.d
        req = {"id": 42, "project": "req-board", "title": "T"}
        sent = []
        with mock.patch.object(d, "_send_feishu", side_effect=lambda *a, **k: sent.append(a) or False), \
                mock.patch.object(d, "feishu", object()), \
                mock.patch.dict(d.state, {"chat_id": "oc_x"}):
            d.notify_req_card("failed", req, retries=3, reason="r")
        self.assertEqual(sent[0][0], "interactive")
        self.assertEqual(sent[1], ("text", {"text": d.fallback_text("failed", req, retries=3, reason="r")}))
        d.NOTIFIERS.flush(5)
        self.assertEqual([e.kind for e in self.rec.got], ["failed"])   # 降级文本不重复扇出到配置渠道

    def test_N5c_notify_text_paths(self):
        d = self.d
        with mock.patch.object(d, "_send_feishu", return_value=True) as sf:
            d.notify("普通回执")
            d.notify("会话回复", chat_id="oc_chat")
            d.NOTIFIERS.flush(5)
        self.assertEqual(sf.call_args_list[0], mock.call("text", {"text": "普通回执"}))
        self.assertEqual(sf.call_args_list[1], mock.call("text", {"text": "会话回复"}, "oc_chat"))
        self.assertEqual([e.text for e in self.rec.got], ["普通回执"])

    def test_N5d_on_message_replies_stay_in_feishu(self):
        d = self.d
        seen = []
        with mock.patch.object(d, "_on_message", lambda data: d.notify("需求 #9 没有待确认的问题。")), \
                mock.patch.object(d, "_send_feishu", side_effect=lambda *a, **k: seen.append(a) or True):
            d.on_message(object())
            d.NOTIFIERS.flush(5)
        self.assertEqual(seen, [("text", {"text": "需求 #9 没有待确认的问题。"})])
        self.assertEqual(self.rec.got, [])

    def test_N5e_card_titles_single_source(self):
        for k, v in self.d.CARD_EVENTS.items():
            self.assertEqual(v["title"], notifiers.EVENT_LABELS[k])


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b
        cls.tmp = Path(tempfile.mkdtemp(prefix="ntf-api-"))
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.path = self.tmp / f"notifiers-{self._testMethodName}.json"
        self._p = mock.patch.object(self.b, "NOTIFIERS_JSON_PATH", self.path)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _get(self):
        r = self.client.get("/api/notifier-settings")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def _post(self, body):
        return self.client.post("/api/notifier-settings", json=body)

    def test_N6_get_empty(self):
        d = self._get()
        self.assertEqual(d["path"], str(self.path))
        self.assertEqual(d["warnings"], [])
        self.assertFalse(d["channels"]["webhook"]["enabled"])
        self.assertFalse(d["channels"]["slack"]["url_configured"])
        self.assertEqual([e["id"] for e in d["events"]], list(notifiers.UI_EVENT_KINDS))

    def test_N6b_save_and_never_echo_secrets(self):
        r = self._post({"channel": "webhook", "enabled": True, "url": HOOK_URL, "secret": SECRET,
                        "timeout": 3, "events": ["done", "failed"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["changed"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        r = self._post({"channel": "slack", "enabled": True, "url": SLACK_URL})
        self.assertEqual(r.status_code, 200, r.get_json())
        raw = self.client.get("/api/notifier-settings").get_data(as_text=True)
        for s in (SECRET, "HOOKTOKENXYZ", "SLACKTOKENXYZ", "/hook/path", "/services/"):
            self.assertNotIn(s, raw)
        d = json.loads(raw)
        wh = d["channels"]["webhook"]
        self.assertTrue(wh["enabled"] and wh["url_configured"] and wh["secret_configured"])
        self.assertEqual(wh["url_hint"], "https://example.com/…")
        self.assertEqual(wh["events"], ["done", "failed"])
        self.assertEqual(d["channels"]["slack"]["url_hint"], "https://hooks.slack.com/…")
        # 留空 = 保留旧值；关闭开关不丢地址
        r = self._post({"channel": "webhook", "enabled": False})
        self.assertEqual(r.status_code, 200)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["channels"]["webhook"]["url"], HOOK_URL)
        self.assertEqual(saved["channels"]["webhook"]["secret"], SECRET)
        self.assertFalse(saved["channels"]["webhook"]["enabled"])
        # 清除 secret
        self._post({"channel": "webhook", "clear_secret": True})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("secret", saved["channels"]["webhook"])
        # 相同内容再存 = changed false
        self.assertFalse(self._post({"channel": "slack", "enabled": True}).get_json()["changed"])

    def test_N6c_validation_zero_write(self):
        self._post({"channel": "slack", "enabled": False, "url": SLACK_URL})
        before = self.path.read_bytes()
        bad = [
            {"channel": "pager"},
            {"channel": "slack", "secret": "x"},                       # slack 无 secret 字段
            {"channel": "slack", "url": "http://hooks.slack.com/x"},   # 非 https
            {"channel": "webhook", "enabled": True},                   # 启用但无 url
            {"channel": "webhook", "url": "ftp://x"},
            {"channel": "webhook", "url": "http://x", "timeout": 99},
            {"channel": "webhook", "url": "http://x", "events": ["nope"]},
            {"channel": "webhook", "enabled": "yes"},
            {"channel": "webhook", "url": "http://x", "clear_url": True},
            {"channel": "general", "board_url": "nope"},
        ]
        for body in bad:
            r = self._post(body)
            self.assertEqual(r.status_code, 400, body)
            self.assertEqual(self.path.read_bytes(), before, body)
        self.assertEqual(self.client.post("/api/notifier-settings", data="x").status_code, 400)

    def test_N6d_corrupt_file_get_warns_post_refuses(self):
        self.path.write_text("{oops " + SECRET, encoding="utf-8")
        d = self._get()
        self.assertTrue(d["warnings"])
        self.assertNotIn(SECRET, json.dumps(d))
        self.assertFalse(d["channels"]["slack"]["enabled"])
        r = self._post({"channel": "slack", "url": SLACK_URL})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith("{oops"))

    def test_N6e_unknown_fields_preserved_and_general(self):
        _write(self.path, {"custom": 1, "channels": {"slack": {"enabled": False, "note": "keep"}}})
        self.assertEqual(self._post({"channel": "slack", "url": SLACK_URL}).status_code, 200)
        self.assertEqual(self._post({"channel": "general", "board_url": "https://b.example.com/"}).status_code, 200)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["custom"], 1)
        self.assertEqual(saved["channels"]["slack"]["note"], "keep")
        self.assertEqual(saved["board_url"], "https://b.example.com")
        self.assertEqual(self._get()["board_url"], "https://b.example.com")
        self._post({"channel": "general", "board_url": ""})
        self.assertNotIn("board_url", json.loads(self.path.read_text(encoding="utf-8")))

    def test_N6f_test_endpoint(self):
        r = self.client.post("/api/notifier-settings/test", json={"channel": "slack"})
        self.assertEqual(r.get_json()["ok"], False)
        self.assertEqual(self.client.post("/api/notifier-settings/test", json={"channel": "x"}).status_code, 400)
        srv, url = _serve(200)
        try:
            self._post({"channel": "webhook", "url": url, "secret": SECRET})
            r = self.client.post("/api/notifier-settings/test", json={"channel": "webhook"})
            d = r.get_json()
            self.assertTrue(d["ok"], d)
            self.assertEqual(json.loads(srv.got[0]["body"])["event"], "test")
            self.assertIn("X-Loop-Signature", srv.got[0]["headers"])
            srv.status = 403
            d = self.client.post("/api/notifier-settings/test",
                                 json={"channel": "slack", "url": url.replace("http://", "https://")}).get_json()
            self.assertFalse(d["ok"])
        finally:
            srv.shutdown()
        d = self.client.post("/api/notifier-settings/test",
                             json={"channel": "webhook", "url": "http://127.0.0.1:1/x?token=SEKRIT"}).get_json()
        self.assertFalse(d["ok"])
        self.assertNotIn("SEKRIT", json.dumps(d))
        self.assertNotIn(SECRET, json.dumps(d))


class FrontendWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests._frontend_src import frontend_js, frontend_src
        cls.src = frontend_src()
        cls.js = frontend_js()

    def test_N7_tab_and_sections(self):
        self.assertIn(">通知渠道</button>", self.src)
        self.assertIn('id="settingsFeishuForm"', self.src)
        self.assertIn('id="settingsNotifierBody"', self.src)
        self.assertIn("getElementById('settingsFeishuForm')", self.js)
        self.assertIn("if (settingsTab === 'feishu' && !notifierSettingsLoaded) loadNotifierSettings(false);",
                      self.js)
        self.assertIn("loadNotifierSettings(true); }", self.js)
        for needle in ("'/api/notifier-settings'", "'/api/notifier-settings/test'", "data-ntf-save",
                       "data-ntf-test", "saveNotifierChannel(t.dataset.ntfSave, t)",
                       "testNotifierChannel(t.dataset.ntfTest)"):
            self.assertIn(needle, self.js)

    def test_N7b_deep_link(self):
        self.assertIn("new URLSearchParams(location.search).get('req')", self.js)
        self.assertIn("savedViewState = {view: 'requirement', requirementId: rid, project: ALL_PROJECTS};", self.js)


if __name__ == "__main__":
    unittest.main()
