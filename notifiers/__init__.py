"""可插拔通知渠道（出站）：注册表 + 统一接口 + 本地配置 notifiers.json。

调度器经 `NotifierRegistry` 把两类出站通知扇出到全部已启用渠道：
  - 文本消息（`send_text`，事件名 "message"）：回执、告警等普通文本；
  - 需求卡事件（`send_event`）：need_confirm / done / failed / blocked / merge_conflict 等结构化事件。

渠道分两种：
  - 同步渠道（飞书）：由调度器在启动时注册，沿用既有发送逻辑（行为逐字不变），入站 y/n 回复仍是飞书专属；
  - 异步渠道（Webhook / Slack）：由 notifiers.json 配置驱动，每渠道一个后台线程 + 有界队列，
    **绝不阻塞调度主循环**；配置文件 mtime 变化时自动重载（保存即生效，无需重启调度器）。

fail-closed：配置非法（JSON 坏 / 结构不符 / 字段非法）一律只停用对应渠道并记录原因，绝不抛到调用方。
密钥（Webhook secret、Slack webhook URL）只在本进程内使用：不进日志、不进状态、不回显给界面。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

__all__ = [
    "EVENT_KINDS", "CARD_EVENT_KINDS", "EVENT_LABELS", "UI_EVENT_KINDS", "CHANNEL_TYPES", "NotifyEvent", "Notifier",
    "NotifierRegistry", "config_path", "read_config", "parse_config", "build_channels",
    "event_text", "requirement_url", "mask_url", "valid_http_url",
]

log = logging.getLogger("notifiers")

# 需求卡结构化事件（与 dispatcher.CARD_EVENTS 键一致；route/review 为历史兼容事件）
CARD_EVENT_KINDS = ("need_confirm", "done", "failed", "blocked", "merge_conflict", "route", "review")
# 可订阅事件全集：卡片事件 + 普通文本 message；test 为设置页「发送测试」专用，不可订阅、恒放行
EVENT_KINDS = CARD_EVENT_KINDS + ("message",)
# 事件显示名单一源（dispatcher.CARD_EVENTS 的卡片标题、设置页事件订阅项都从这里取）
EVENT_LABELS = {
    "route": "通道待确认", "review": "阶段待评审", "need_confirm": "需要确认", "done": "开发完成",
    "failed": "自动开发失败", "blocked": "需求阻塞", "merge_conflict": "自动合并冲突",
    "message": "普通文本消息",
}
# 设置页可勾选的订阅项（route/review 为历史事件、新流程不再产生，不在界面展示，配置文件仍接受）
UI_EVENT_KINDS = ("need_confirm", "done", "failed", "blocked", "merge_conflict", "message")
# 配置驱动的渠道类型（飞书不在此列：凭据仍读 feishu.json，由调度器注册）
CHANNEL_TYPES = ("webhook", "slack")

DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 30.0
QUEUE_MAX = 200          # 单渠道待发上限：满了丢弃新消息并告警（不阻塞调用方）
TEXT_LIMIT = 3000        # 单条正文上限（Slack section 文本上限 3000）

CONFIG_ENV = "REQ_BOARD_NOTIFIERS_JSON"


def config_path(repo_dir) -> Path:
    """notifiers.json 路径单一口径（board / dispatcher 同源）：
    REQ_BOARD_NOTIFIERS_JSON 显式覆盖 → 否则与 feishu.json 同目录（REQ_BOARD_FEISHU_JSON 覆盖时跟随，
    测试实例天然隔离）→ 否则仓库根 notifiers.json。"""
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    fs = os.environ.get("REQ_BOARD_FEISHU_JSON")
    if fs:
        return Path(fs).expanduser().parent / "notifiers.json"
    return Path(repo_dir) / "notifiers.json"


# ==================== 配置解析（纯函数，fail-closed） ====================

def valid_http_url(url, https_only=False) -> bool:
    if not isinstance(url, str) or not url or len(url) > 2048:
        return False
    if any(ord(ch) < 33 for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    schemes = ("https",) if https_only else ("http", "https")
    return parts.scheme in schemes and bool(parts.netloc)


def mask_url(url) -> str:
    """URL 脱敏摘要：只留 scheme://host（去掉路径、查询串与 userinfo——其中常含令牌）。"""
    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return f"{parts.scheme}://{host}/…" if parts.scheme and host else ""


def _parse_events(raw):
    """events 字段：缺省 = 全部可订阅事件；非法 → (None, 原因)。"""
    if raw is None:
        return list(EVENT_KINDS), None
    if not isinstance(raw, list) or not all(isinstance(e, str) for e in raw):
        return None, "events 必须为字符串数组"
    unknown = [e for e in raw if e not in EVENT_KINDS]
    if unknown:
        return None, f"events 含未知事件: {', '.join(unknown)}"
    return list(dict.fromkeys(raw)), None


def _parse_timeout(raw):
    if raw is None:
        return DEFAULT_TIMEOUT, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not (0 < raw <= MAX_TIMEOUT):
        return None, f"timeout 必须为 (0, {int(MAX_TIMEOUT)}] 秒内的数字"
    return float(raw), None


def parse_channel(ctype, raw):
    """单渠道配置 → (spec|None, reason|None)。spec 恒含 enabled；enabled=false 时不校验其余字段的完整性
    （允许「先存地址、暂不启用」），但字段一旦出现且形态非法仍判非法（fail-closed）。"""
    if not isinstance(raw, dict):
        return None, "配置需为对象"
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        return None, "enabled 必须为布尔值"
    events, why = _parse_events(raw.get("events"))
    if why:
        return None, why
    timeout, why = _parse_timeout(raw.get("timeout"))
    if why:
        return None, why
    spec = {"enabled": enabled, "events": events, "timeout": timeout}
    if ctype == "webhook":
        url = raw.get("url", "")
        secret = raw.get("secret", "")
        if url and not valid_http_url(url):
            return None, "url 必须为 http(s) 地址"
        if not isinstance(secret, str):
            return None, "secret 必须为字符串"
        if enabled and not url:
            return None, "已启用但未配置 url"
        spec.update(url=url or "", secret=secret)
    elif ctype == "slack":
        url = raw.get("webhook_url", "")
        if url and not valid_http_url(url, https_only=True):
            return None, "webhook_url 必须为 https 地址（Slack Incoming Webhook）"
        if enabled and not url:
            return None, "已启用但未配置 webhook_url"
        spec.update(webhook_url=url or "")
    else:
        return None, f"未知渠道类型: {ctype}"
    return spec, None


def parse_config(raw):
    """整份配置 → {"board_url", "channels": {type: spec}, "errors": {type|"_": reason}}。
    顶层非对象 / channels 非对象 → 全部配置渠道停用（errors["_"]）；单渠道非法只停用该渠道。"""
    out = {"board_url": None, "channels": {}, "errors": {}}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        out["errors"]["_"] = "顶层需为 JSON 对象"
        return out
    bu = raw.get("board_url")
    if bu is not None:
        if valid_http_url(bu):
            out["board_url"] = bu.rstrip("/")
        else:
            out["errors"]["board_url"] = "board_url 必须为 http(s) 地址，已忽略"
    chans = raw.get("channels", {})
    if not isinstance(chans, dict):
        out["errors"]["_"] = "channels 需为对象"
        return out
    for ctype, craw in chans.items():
        if ctype not in CHANNEL_TYPES:
            out["errors"][str(ctype)] = f"未知渠道类型 {ctype}，已忽略"
            continue
        spec, why = parse_channel(ctype, craw)
        if why:
            out["errors"][ctype] = why
        else:
            out["channels"][ctype] = spec
    return out


def read_config(path):
    """读 notifiers.json → (raw|None, error|None)。缺失 = (None, None)（未配置不是错误）；
    不可读 / JSON 非法 → (None, 固定文案)（不回显解析异常原文，可能含文件片段/密钥）。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "notifiers.json 不可读，请检查文件权限"
    try:
        return json.loads(text), None
    except ValueError:
        return None, "notifiers.json 解析失败，请检查文件格式"


# ==================== 事件模型与文案 ====================

@dataclass
class NotifyEvent:
    """一条出站通知。kind ∈ EVENT_KINDS ∪ {"test"}；text 恒为可直接展示的纯文本。"""
    kind: str
    title: str
    text: str
    requirement: Optional[dict] = None   # {id, title, project, status, url}
    details: dict = field(default_factory=dict)   # JSON 安全的结构化细节
    raw: dict = field(default_factory=dict)       # 调用方原始关键字参数（飞书构卡用，不外发）


def requirement_url(board_url, req_id) -> str:
    """看板卡片深链：/?req=<id>（看板首页识别该参数后选中对应卡片）。"""
    base = (board_url or "").rstrip("/")
    return f"{base}/?req={int(req_id)}" if base else ""


def _clip(s, n=TEXT_LIMIT):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def event_text(kind, details) -> str:
    """需求卡事件正文（渠道无关的纯文本）：按事件挑关键信息，不含飞书 y/n 回复指引（出站渠道不支持回复）。"""
    d = details or {}
    lines = []
    if d.get("phase"):
        lines.append(f"阶段：{d['phase']}")
    if kind == "need_confirm":
        lines.append(d.get("question") or "（agent 未说明问题）")
        if d.get("round_n"):
            lines.append(f"（已连续第 {d['round_n']} 轮待确认）" if d.get("over_limit")
                         else f"（第 {d['round_n']} 轮待确认）")
        lines.append("请在看板上答复。")
    elif kind == "done":
        lines.append(d.get("summary") or "需求已完成")
        if d.get("reminder"):
            lines.append(f"⚠️ {str(d['reminder']).strip().lstrip('⚠️').strip()}")
        for t in d.get("todos") or []:
            lines.append(f"⚠️ 待办：{t}")
    elif kind == "failed":
        lines.append(f"自动开发失败 {d.get('retries') or 0} 次，已停止自动调度。")
        if d.get("reason"):
            lines.append(f"最后原因：{d['reason']}")
    elif kind == "blocked":
        if d.get("reason"):
            lines.append(f"阻塞原因：{d['reason']}")
        lines.append("已转暂缓待人工处理。")
    elif kind == "merge_conflict":
        failed = d.get("failed") or []
        if failed:
            lines.append(f"冲突仓库：{', '.join(str(f) for f in failed)}")
        if d.get("branch"):
            lines.append(f"已保留分支 {d['branch']} 与 worktree，请在看板处理。")
    elif kind == "route":
        if d.get("suggested"):
            lines.append(f"建议通道：{d['suggested']}")
    elif kind == "review":
        lines.append("阶段产物待评审。")
    return _clip("\n".join(str(x) for x in lines if x))


def _json_safe(v, depth=0):
    if depth > 4:
        return str(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple, set)):
        return [_json_safe(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x, depth + 1) for k, x in v.items()}
    return str(v)


# ==================== 渠道接口 ====================

class Notifier:
    """出站渠道基类。子类实现 deliver(event)；同步渠道（sync=True）在调用线程直接发送，
    异步渠道由注册表放进后台队列发送。raise_errors=True 的渠道异常原样抛给调用方（飞书沿用旧行为）。"""
    name = "base"
    sync = False
    raise_errors = False

    def __init__(self, events=None):
        self.events = set(events if events is not None else EVENT_KINDS)

    def accepts(self, kind) -> bool:
        return kind == "test" or kind in self.events

    def deliver(self, event: NotifyEvent):   # pragma: no cover - 接口
        raise NotImplementedError


class _Worker:
    """单渠道后台发送线程：有界队列，满了丢新消息（log.warning），发送异常只记日志。"""

    def __init__(self, notifier: Notifier, logger):
        import queue
        self.notifier = notifier
        self.log = logger
        self.q = queue.Queue(maxsize=QUEUE_MAX)
        self.t = threading.Thread(target=self._run, name=f"notifier-{notifier.name}", daemon=True)
        self.t.start()

    def put(self, event) -> bool:
        import queue
        try:
            self.q.put_nowait(event)
            return True
        except queue.Full:
            self.log.warning(f"通知渠道 {self.notifier.name} 待发队列已满，丢弃一条 {event.kind} 通知")
            return False

    def stop(self):
        with contextlib.suppress(Exception):
            self.q.put_nowait(None)

    def _run(self):
        while True:
            ev = self.q.get()
            try:
                if ev is None:
                    return
                ok, info = self.notifier.deliver(ev)
                if ok:
                    self.log.info(f"通知渠道 {self.notifier.name} 已发送 {ev.kind}")
                else:
                    self.log.warning(f"通知渠道 {self.notifier.name} 发送 {ev.kind} 失败：{info}")
            except Exception as e:  # noqa: BLE001 - 后台线程永不因单条失败退出
                self.log.warning(f"通知渠道 {self.notifier.name} 发送 {ev.kind} 异常：{type(e).__name__}")
            finally:
                self.q.task_done()

    def join(self, timeout=5.0):
        """测试/收尾用：等队列清空（带超时）。"""
        import time
        deadline = time.monotonic() + timeout
        while self.q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.q.unfinished_tasks


def build_channels(parsed, board_url):
    """解析结果 → {type: Notifier}（只含 enabled 的渠道）。"""
    from notifiers.slack import SlackNotifier
    from notifiers.webhook import WebhookNotifier
    out = {}
    for ctype, spec in (parsed.get("channels") or {}).items():
        if not spec.get("enabled"):
            continue
        if ctype == "webhook":
            out[ctype] = WebhookNotifier(spec["url"], secret=spec.get("secret") or "",
                                         timeout=spec["timeout"], events=spec["events"])
        elif ctype == "slack":
            out[ctype] = SlackNotifier(spec["webhook_url"], timeout=spec["timeout"],
                                       events=spec["events"])
    return out


class NotifierRegistry:
    """渠道注册表：静态渠道（register，飞书）+ 配置驱动渠道（notifiers.json，mtime 变化自动重载）。"""

    def __init__(self, config_file=None, board_url="", logger=None):
        self.config_file = Path(config_file) if config_file else None
        self.default_board_url = (board_url or "").rstrip("/")
        self.board_url = self.default_board_url
        self.log = logger or log
        self._static = []
        self._workers = {}
        self._errors = {}
        self._stamp = object()   # 哨兵：首次必加载
        self._lock = threading.RLock()
        self._local = threading.local()

    # ---- 注册与配置 ----
    def register(self, notifier: Notifier):
        with self._lock:
            self._static = [n for n in self._static if n.name != notifier.name] + [notifier]

    def _file_stamp(self):
        if not self.config_file:
            return None
        try:
            st = self.config_file.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload(self, force=False):
        """按文件戳重载配置渠道；任何异常都只停用配置渠道并记录，不抛。"""
        stamp = self._file_stamp()
        with self._lock:
            if not force and stamp == self._stamp:
                return
            self._stamp = stamp
            for w in self._workers.values():
                w.stop()
            self._workers = {}
            self._errors = {}
            self.board_url = self.default_board_url
            if not self.config_file:
                return
            try:
                raw, err = read_config(self.config_file)
                if err:
                    self._errors["_"] = err
                    self.log.warning(f"通知渠道配置不可用（{self.config_file}）：{err}，Webhook/Slack 渠道全部停用")
                    return
                parsed = parse_config(raw)
                self._errors.update(parsed["errors"])
                for k, why in parsed["errors"].items():
                    self.log.warning(f"通知渠道配置非法（{k}）：{why}，该渠道停用")
                if parsed["board_url"]:
                    self.board_url = parsed["board_url"]
                for ctype, n in build_channels(parsed, self.board_url).items():
                    self._workers[ctype] = _Worker(n, self.log)
                if self._workers:
                    self.log.info(f"通知渠道已加载：{', '.join(sorted(self._workers))}")
            except Exception as e:  # noqa: BLE001 - fail-closed：停用配置渠道，绝不崩调度器
                self._workers = {}
                self._errors["_"] = f"加载异常：{type(e).__name__}"
                self.log.warning(f"通知渠道配置加载异常（{type(e).__name__}），Webhook/Slack 渠道全部停用")

    def status(self):
        """非敏感状态：{static: [...], channels: [...], errors: {...}}（不含 URL/密钥）。"""
        with self._lock:
            return {"static": [n.name for n in self._static],
                    "channels": sorted(self._workers),
                    "errors": dict(self._errors)}

    # ---- 仅飞书上下文：飞书入站回复、飞书卡片降级文本期间的 send_text 不扇出到配置渠道 ----
    @contextlib.contextmanager
    def inbound_scope(self):
        prev = getattr(self._local, "inbound", False)
        self._local.inbound = True
        try:
            yield
        finally:
            self._local.inbound = prev

    def _in_inbound(self):
        return bool(getattr(self._local, "inbound", False))

    # ---- 出站 ----
    def send_text(self, text):
        ev = NotifyEvent(kind="message", title="", text=_clip(text), raw={"text": text})
        self._dispatch(ev, configured=not self._in_inbound())

    def send_event(self, kind, req, title="", details=None, raw=None):
        req = req or {}
        rid = req.get("id")
        reqd = None
        if rid is not None:
            try:
                url = requirement_url(self.board_url, rid)
            except (TypeError, ValueError):
                url = ""
            reqd = {"id": rid, "title": req.get("title") or "", "project": req.get("project") or "",
                    "status": req.get("status") or "", "url": url}
        det = _json_safe(details or {})
        ev = NotifyEvent(kind=kind, title=title or kind, text=event_text(kind, det),
                         requirement=reqd, details=det, raw=dict(raw or {}))
        self._dispatch(ev, configured=True)

    def _dispatch(self, ev, configured=True):
        if configured:
            try:
                self.reload()
                with self._lock:
                    workers = list(self._workers.values())
                for w in workers:
                    if w.notifier.accepts(ev.kind):
                        w.put(ev)
            except Exception as e:  # noqa: BLE001 - 出站扇出永不影响调用方与飞书
                self.log.warning(f"通知渠道扇出异常（{type(e).__name__}），已跳过")
        with self._lock:
            static = list(self._static)
        for n in static:
            if not n.accepts(ev.kind):
                continue
            if n.raise_errors:
                n.deliver(ev)
                continue
            try:
                n.deliver(ev)
            except Exception as e:  # noqa: BLE001
                self.log.warning(f"通知渠道 {n.name} 发送异常：{type(e).__name__}")

    def flush(self, timeout=5.0):
        """测试/收尾用：等各后台队列发完。"""
        with self._lock:
            workers = list(self._workers.values())
        return all(w.join(timeout) for w in workers)


class CallableNotifier(Notifier):
    """以回调包装的同步渠道（飞书即用此类注册）：on_text(text) / on_event(event: NotifyEvent)。"""
    sync = True

    def __init__(self, name, on_text: Callable[[str], Any], on_event: Callable[[NotifyEvent], Any],
                 raise_errors=True, events=None):
        super().__init__(events)
        self.name = name
        self._on_text = on_text
        self._on_event = on_event
        self.raise_errors = raise_errors

    def deliver(self, event):
        if event.kind in ("message", "test"):
            return self._on_text(event.raw.get("text", event.text))
        return self._on_event(event)


__all__.append("CallableNotifier")
