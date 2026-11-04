"""通用 Webhook 渠道：POST JSON，可选 HMAC-SHA256 签名。

请求头：
  Content-Type: application/json; charset=utf-8
  User-Agent: loop-engine-notifier/1
  X-Loop-Event: <事件名>
  X-Loop-Timestamp: <unix 秒>                         （配置了 secret 时）
  X-Loop-Signature: sha256=<hex(HMAC(secret, "<ts>.<body>"))>   （配置了 secret 时）

接收方校验：用同一 secret 对「时间戳 + "." + 原始请求体字节」计算 HMAC-SHA256，与签名头做常量时间比较，
并拒绝时间戳偏差过大的请求（防重放）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from notifiers import Notifier, NotifyEvent

SCHEMA_VERSION = 1
USER_AGENT = "loop-engine-notifier/1"
RESP_PREVIEW = 200


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """签名单一实现：sha256=<hex(HMAC_SHA256(secret, f"{timestamp}." + body))>。"""
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def build_payload(event: NotifyEvent, now=None) -> dict:
    """Webhook 请求体（纯函数）。不含任何密钥；raw（飞书构卡参数）不外发。"""
    ts = now if now is not None else time.time()
    return {
        "source": "loop-engine",
        "schema": SCHEMA_VERSION,
        "event": event.kind,
        "title": event.title,
        "text": event.text,
        "requirement": event.requirement,
        "details": event.details,
        "sent_at": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
    }


def build_request(url, event: NotifyEvent, secret="", now=None):
    """→ (body_bytes, headers)。时间戳与 sent_at 同源（now 可注入，测试用）。"""
    ts = now if now is not None else time.time()
    body = json.dumps(build_payload(event, ts), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT,
               "X-Loop-Event": event.kind}
    if secret:
        stamp = str(int(ts))
        headers["X-Loop-Timestamp"] = stamp
        headers["X-Loop-Signature"] = sign(secret, stamp, body)
    return body, headers


def post_json(url, body: bytes, headers: dict, timeout: float):
    """同步 POST → (ok, info)。info 只含状态码 / 异常类型与原因，**不含 URL**（URL 可能带令牌）。"""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            preview = resp.read(RESP_PREVIEW).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"网络错误：{type(e.reason).__name__ if not isinstance(e.reason, str) else e.reason}"
    except Exception as e:  # noqa: BLE001 - 超时/连接重置等
        return False, f"网络错误：{type(e).__name__}"
    if 200 <= code < 300:
        return True, f"HTTP {code}" + (f" {preview.strip()[:80]}" if preview.strip() else "")
    return False, f"HTTP {code}"


class WebhookNotifier(Notifier):
    name = "webhook"

    def __init__(self, url, secret="", timeout=5.0, events=None):
        super().__init__(events)
        self.url = url
        self.secret = secret or ""
        self.timeout = timeout

    def deliver(self, event: NotifyEvent):
        body, headers = build_request(self.url, event, self.secret)
        return post_json(self.url, body, headers, self.timeout)
