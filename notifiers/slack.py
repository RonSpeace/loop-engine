"""Slack 渠道：Incoming Webhook（POST Block Kit JSON）。

webhook URL 本身即凭据（拿到即可向频道发消息），按密钥对待：不进日志、不回显界面。
消息结构：header（事件标题）+ section（卡片链接 + 正文，mrkdwn）+ context（项目/状态）+
「在看板中打开」按钮（看板深链 /?req=<id>）；纯文本消息只有一个 section。顶层 text 为通知预览兜底。
"""
from __future__ import annotations

import json

from notifiers import Notifier, NotifyEvent, TEXT_LIMIT
from notifiers.webhook import USER_AGENT, post_json

HEADER_LIMIT = 150      # Slack header 纯文本上限
BUTTON_TEXT = "在看板中打开"


def esc(s) -> str:
    """Slack mrkdwn 转义：& < > 三个控制字符（其余原样）。"""
    return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def build_payload(event: NotifyEvent) -> dict:
    """Block Kit 请求体（纯函数）。"""
    req = event.requirement or None
    if event.kind in ("message", "test") or not req:
        text = event.text or event.title
        blocks = []
        if event.title and event.kind != "message":
            blocks.append({"type": "header",
                           "text": {"type": "plain_text", "text": _clip(event.title, HEADER_LIMIT)}})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(esc(text), TEXT_LIMIT)}})
        return {"text": _clip(text, TEXT_LIMIT), "blocks": blocks}
    rid = req.get("id")
    title = req.get("title") or ""
    url = req.get("url") or ""
    head = f"需求 #{rid} {event.title}"
    link = f"<{url}|#{rid} {esc(title)}>" if url else f"#{rid} {esc(title)}"
    body = f"*{link}*"
    if event.text:
        body += "\n" + esc(event.text)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": _clip(head, HEADER_LIMIT)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(body, TEXT_LIMIT)}},
    ]
    ctx = []
    if req.get("project"):
        ctx.append(f"项目：{esc(req['project'])}")
    if req.get("status"):
        ctx.append(f"状态：{esc(req['status'])}")
    if ctx:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(ctx)}]})
    if url:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": BUTTON_TEXT}, "url": url}]})
    return {"text": _clip(f"{head}：{title}", TEXT_LIMIT), "blocks": blocks}


class SlackNotifier(Notifier):
    name = "slack"

    def __init__(self, webhook_url, timeout=5.0, events=None):
        super().__init__(events)
        self.webhook_url = webhook_url
        self.timeout = timeout

    def deliver(self, event: NotifyEvent):
        body = json.dumps(build_payload(event), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT}
        return post_json(self.webhook_url, body, headers, self.timeout)
