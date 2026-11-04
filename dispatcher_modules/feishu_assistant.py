"""飞书看板助手会话编排：每个群聊一个串行 worker，空闲会话过期回收。"""

import base64
import queue
import time
import urllib.request
from urllib.parse import quote

from dispatcher_modules.runtime import D, log, threading


# 与单项目建卡完全分离：本地状态 state["feishu_assistant"][chat_id] 只存编排元数据
# （session_id / feishu_chat_id / last_event_id / pending / awaiting_project / turn_id / updated_at），
# **不存会话原文与素材字节**；board 侧会话与事件落独立的助手表。
# 计时：worker 起表 90s 总 deadline → 每次调用 board 传剩余秒数（board 权威计时并自行收尾），
# dispatcher 只做超时兜底（cancel + 固定文案一次，保留 session_id 供下次 resume）。
# 不复用建卡的 _chat_boot/_chat_prompt_thread/_start_chat_round 与 CHAT_TIMEOUT(600s) 语义。


def _feishu_assistant():
    """state["feishu_assistant"] 访问器（缺失时补空 dict；与 state["feishu_chats"] 完全分离）"""
    a = D.state.get("feishu_assistant")
    if not isinstance(a, dict):
        a = {}
        D.state["feishu_assistant"] = a
    return a


def _feishu_assistant_entry(chat_id):
    """按 chat_id 取（或建）助手本地状态，字段固定（不落会话原文与素材字节）。"""
    a = _feishu_assistant()
    e = a.get(chat_id)
    if not isinstance(e, dict):
        e = {}
        a[chat_id] = e
    e.setdefault("session_id", None)
    e.setdefault("feishu_chat_id", chat_id)
    e.setdefault("last_event_id", 0)
    e.setdefault("pending", False)
    e.setdefault("awaiting_project", False)
    e.setdefault("turn_id", None)
    e.setdefault("updated_at", time.time())
    return e


def _feishu_assistant_q(chat_id):
    """助手端点的归属校验参数（session id + feishu_chat_id 双绑定）"""
    return f"feishu_chat_id={quote(str(chat_id or ''), safe='')}"


def _feishu_assistant_status(sid, chat_id):
    """GET /api/assistant/sessions/<sid> → (status, row)；看板不可达/不匹配（404）→ (None, None)"""
    try:
        row = D.api_board("GET", f"/assistant/sessions/{sid}?{_feishu_assistant_q(chat_id)}")
    except Exception as e:
        log.warning(f"飞书助手会话状态查询失败（{sid}）：{e}")
        return None, None
    return (row or {}).get("status"), row


def _feishu_assistant_cancel_pending(chat_id, entry, turn_id=None):
    """取消当前未收尾的一轮（board 侧锁内置 cancelling、关进程、保留 ACP session_id）。
    turn_id 缺省取本地状态；**优先用 board 会话行返回的当前 turn_id**（board 侧按 turn_id
    精确栅栏，给错值会被 409 拒绝）。失败只告警（下一条消息按 409 栅栏重试），并清本地标记。"""
    sid = (entry or {}).get("session_id")
    turn_id = turn_id if turn_id is not None else (entry or {}).get("turn_id")
    if not sid:
        entry["pending"] = False
        entry["turn_id"] = None
        return False
    try:
        D.api_board("POST", f"/assistant/sessions/{sid}/cancel",
                  {"feishu_chat_id": chat_id, "turn_id": turn_id})
    except Exception as e:
        log.warning(f"飞书助手轮次取消失败（{sid}）：{e}")
    entry["pending"] = False
    entry["turn_id"] = None
    entry["updated_at"] = time.time()
    D._feishu_save()
    return True


def _feishu_assistant_wait_resumable(chat_id, entry, deadline):
    """409 状态栅栏：等 board 把 cancelling 收尾为可续跑态（idle/interrupted）后重试；
    终态或超时返回 False（新消息不被 409 永久挡住，也不无限等待）。"""
    sid = entry.get("session_id")
    while time.monotonic() < deadline:
        status, _row = _feishu_assistant_status(sid, chat_id)
        if status in (None, "idle", "interrupted"):
            return True
        if status in ("closed", "failed"):
            return False
        time.sleep(D.FEISHU_ASSISTANT_POLL_SEC)
    return False


def _feishu_assistant_ensure_session(chat_id, entry):
    """get-or-create 助手会话（带解析后的 assistant_model，未配置传 None 走 board 默认链）；
    返回 (sid|None, err|None)。"""
    try:
        row = D.api_board("POST", "/assistant/sessions",
                        {"feishu_chat_id": chat_id, "model": D.FEISHU_ASSISTANT_MODEL})
    except urllib.error.HTTPError as e:
        err = D._feishu_http_err(e)
        log.error(f"飞书助手建会话失败（{chat_id}）：{err}")
        return None, f"{D.FEISHU_ASSISTANT_DOWN_HINT}：{err}"
    except Exception as e:
        log.error(f"飞书助手建会话异常（{chat_id}）：{e}")
        return None, D.FEISHU_ASSISTANT_DOWN_HINT
    sid = (row or {}).get("id")
    if not sid:
        return None, f"{D.FEISHU_ASSISTANT_DOWN_HINT}（未返回会话 id）"
    entry["session_id"] = sid
    entry["feishu_chat_id"] = chat_id
    entry["updated_at"] = time.time()
    D._feishu_save()
    return sid, None


def _feishu_assistant_submit(chat_id, entry, sid, text, deadline, attachments=None):
    """提交一轮 prompt（带剩余秒数，board 权威计时）；返回 (turn_id|None, err)。
    err == "busy" 表示 409（上一轮仍在收尾），由调用方按状态栅栏重试。
    #44：attachments 非空时随 body 提交（board 侧 = 本轮附件，含 base64 字节）；缺省 None 时
    报文与旧调用点逐字一致。"""
    remaining = max(1, int(deadline - time.monotonic()))
    payload = {"feishu_chat_id": chat_id, "text": text, "remaining_sec": remaining}
    if attachments:
        payload["attachments"] = attachments
    try:
        resp = D.api_board("POST", f"/assistant/sessions/{sid}/prompt", payload)
    except urllib.error.HTTPError as e:
        err = D._feishu_http_err(e)
        if e.code == 409:
            return None, "busy"
        log.error(f"飞书助手 prompt 失败（{sid}）：{err}")
        return None, f"{D.FEISHU_ASSISTANT_DOWN_HINT}：{err}"
    except Exception as e:
        log.error(f"飞书助手 prompt 异常（{sid}）：{e}")
        return None, D.FEISHU_ASSISTANT_DOWN_HINT
    turn_id = (resp or {}).get("turn_id")
    if not turn_id:
        return None, f"{D.FEISHU_ASSISTANT_DOWN_HINT}（未返回轮次 id）"
    return turn_id, None


def _feishu_await_round_assistant(chat_id, entry, sid, deadline):
    """#24 助手轮次收尾：独立超时（worker 起表的 90s 总 deadline）与独立状态存储。
    只回发 kind='message'（助手正文）；kind='agent' 是机器人回放事件，**不得再回发**。
    deadline 到点且 board 未收尾 → cancel + 固定文案一次，保留 session_id 供下次续跑；
    晚到输出不再回发。"""
    while True:
        status, row = _feishu_assistant_status(sid, chat_id)
        after = entry.get("last_event_id") or 0
        try:
            resp = D.api_board(
                "GET",
                f"/assistant/sessions/{sid}/events?{_feishu_assistant_q(chat_id)}&after={after}") or {}
        except Exception as e:
            log.warning(f"飞书助手事件拉取失败（{sid}）：{e}")
            resp = {}
        # 会话快照同步（awaiting_project 只在该响应里）：本地状态只留编排元数据
        snap = resp.get("session") or {}
        if snap.get("awaiting_project") is not None:
            entry["awaiting_project"] = bool(snap.get("awaiting_project"))
        for ev in (resp.get("events") or []):
            eid = ev.get("id")
            if isinstance(eid, int) and eid > (entry.get("last_event_id") or 0):
                entry["last_event_id"] = eid
            if ev.get("kind") == "message" and ev.get("content"):
                D.send_text_chunked(chat_id, ev["content"])
            elif ev.get("kind") == "card" and ev.get("content"):
                # #30：需求卡清单卡片（服务端生成的页 spec）→ dispatcher 渲染并显式 chat_id 直发
                D._feishu_send_assistant_card(chat_id, ev["content"])
        entry["updated_at"] = time.time()
        D._feishu_save()
        if status is not None and status != "running":
            entry["pending"] = False
            entry["turn_id"] = None
            entry["updated_at"] = time.time()
            D._feishu_save()
            if status in ("interrupted", "failed"):
                D._feishu_notify(chat_id, "本轮执行中断或失败，重发一次可继续。")
            return
        if time.monotonic() >= deadline:
            # 90s 到点 board 仍未收尾：dispatcher 兜底 cancel + 固定文案（同轮只发一次）
            # cancel 用 board 会话行当前 turn_id（board 按 turn_id 精确栅栏，给错值会被拒）
            _feishu_assistant_cancel_pending(chat_id, entry, turn_id=(row or {}).get("turn_id"))
            D._feishu_notify(chat_id, D.FEISHU_ASSISTANT_TIMEOUT_TEXT)
            return
        time.sleep(D.FEISHU_ASSISTANT_POLL_SEC)


def _feishu_assistant_round(chat_id, text, attachments=None, prepare=None):
    """#24/#44 助手单条消息共用的编排：起表 90s 总 deadline → get-or-create 会话 → 提交一轮 →
    轮询事件与状态 → 收尾。任一环节失败都给出明确降级回复（不静默丢消息）；
    提交撞 409（上一轮收尾中）时按状态栅栏等到可续跑再试一次，不被永久挡住。
    prepare（#44 素材路径用）：会话就绪后、提交前取本轮附件，返回 (attachments|None, err|None)；
    err 非空 → 只回复原因、不提交本轮（重试只重发同一轮，不重复启动轮次）。"""
    deadline = time.monotonic() + D.FEISHU_ASSISTANT_DEADLINE_SEC
    entry = _feishu_assistant_entry(chat_id)
    if entry.get("pending"):
        _feishu_assistant_cancel_pending(chat_id, entry)
    sid, err = _feishu_assistant_ensure_session(chat_id, entry)
    if sid is None:
        D._feishu_notify(chat_id, err or D.FEISHU_ASSISTANT_DOWN_HINT)
        return False
    if prepare is not None:
        attachments, err = prepare()
        if err:
            D._feishu_notify(chat_id, err)
            return False
    turn_id, err = _feishu_assistant_submit(chat_id, entry, sid, text, deadline, attachments=attachments)
    if turn_id is None and err == "busy":
        if _feishu_assistant_wait_resumable(chat_id, entry, deadline):
            turn_id, err = _feishu_assistant_submit(chat_id, entry, sid, text, deadline,
                                                    attachments=attachments)
        else:
            err = D.FEISHU_ASSISTANT_BUSY_HINT
    if turn_id is None:
        D._feishu_notify(chat_id, err or D.FEISHU_ASSISTANT_DOWN_HINT)
        return False
    entry["turn_id"] = turn_id
    entry["pending"] = True
    entry["updated_at"] = time.time()
    D._feishu_save()
    _feishu_await_round_assistant(chat_id, entry, sid, deadline)
    return True


def _feishu_handle_assistant_text(chat_id, text):
    """#24 助手文本单条消息（编排主体见 _feishu_assistant_round）。"""
    return _feishu_assistant_round(chat_id, text)


def _feishu_assistant_material_prepare(handle):
    """#44 素材路径的附件准备（在会话就绪后、提交前调用，与提交共享 90s deadline）：
    扩展名校验 → 下载 → 字节上限校验；返回 (attachments|None, err|None)。
    失败的文件不带入本轮，只回复原因（不发起本轮）。"""
    handle = handle or {}
    kind = handle.get("type") or "file"
    name = handle.get("name") or ""
    if not D._feishu_ext_ok(name):
        return None, f"不支持的文件类型，已跳过：{name}"
    key = handle.get("image_key") if kind == "image" else handle.get("file_key")
    if not key:
        return None, f"文件内容不可解析，已跳过：{name}"
    try:
        data, _fname = D._feishu_download_resource(handle.get("message_id"), key, kind)
    except Exception as e:  # noqa: BLE001 - 下载失败只回原因，不带坏文件进本轮
        log.warning(f"飞书素材下载失败（{handle.get('message_id')}）：{e}")
        return None, f"参考文件下载失败，已跳过：{name}"
    if len(data) > D._FEISHU_MATERIAL_MAX_BYTES:
        return None, (f"文件超过 {D._FEISHU_MATERIAL_MAX_BYTES // (1024 * 1024)}MB 上限，"
                      f"已跳过：{name}")
    # board 侧契约：{name, content_b64}；字节只在本轮局部变量与本次请求体里存在，不写入 state
    return [{"name": name, "content_b64": base64.b64encode(data).decode()}], None


def _feishu_assistant_handles_prepare(handles):
    """多素材（富文本多图/多文件）→ 本轮附件：逐条复用单文件下载与校验，超过 board 单轮上限截断；
    单条失败跳过并留日志；全部失败 → 返回原因（不发起本轮）。返回 (attachments|None, err|None)。"""
    items = [h for h in (handles or []) if isinstance(h, dict)]
    if len(items) > D._FEISHU_MATERIAL_MAX_FILES:
        log.warning(f"飞书素材超过单轮上限 {D._FEISHU_MATERIAL_MAX_FILES} 个，已截断（共 {len(items)} 个）")
        items = items[:D._FEISHU_MATERIAL_MAX_FILES]
    if not items:
        return None, "没有可用的参考文件"
    attachments, reasons = [], []
    for handle in items:
        got, err = _feishu_assistant_material_prepare(handle)
        if got:
            attachments.extend(got)
        elif err:
            reasons.append(err)
    if not attachments:
        return None, (reasons[0] if reasons else "参考文件全部不可用")
    if reasons:
        log.warning(f"飞书素材部分跳过（{len(reasons)} 个）：{'；'.join(reasons)}")
    return attachments, None


def _feishu_material_to_assistant(chat_id, handle):
    """#44 无活跃旧建卡会话的素材消息 → 看板助手：下载后作为本轮附件提交（不再新建建卡会话、
    不再追问项目）。无文字时文本用「（上传了图片）」/「（上传了文件）」；全部失败 → 只回复原因。"""
    kind = (handle or {}).get("type") or "file"
    text = "（上传了图片）" if kind == "image" else "（上传了文件）"
    return _feishu_assistant_round(chat_id, text,
                                   prepare=lambda: _feishu_assistant_material_prepare(handle))


def _feishu_assistant_reconcile():
    """#24 启动对账：按 chat_id 读取持久化的 state["feishu_assistant"] 与 board 助手表对账，
    清理僵尸 running/cancelling（取消本轮、保留 session_id 供下次续跑）；终态会话清本地条目；
    看板不可达（status=None）保持原样，下次启动/下一条消息再对账。"""
    a = _feishu_assistant()
    if not a:
        return
    changed = False
    for chat_id in list(a.keys()):
        entry = a.get(chat_id)
        if not isinstance(entry, dict) or not entry.get("session_id"):
            continue
        status, row = _feishu_assistant_status(entry["session_id"], chat_id)
        if status is None:
            continue
        if status in ("running", "cancelling"):
            log.warning(f"飞书助手会话僵尸轮次清理（{entry['session_id']} status={status}）："
                        "取消本轮并保留 session_id 供下次续跑")
            # cancel 用 board 会话行当前 turn_id（本地持久化的 turn_id 可能已过期）
            _feishu_assistant_cancel_pending(chat_id, entry, turn_id=(row or {}).get("turn_id"))
            changed = True
        elif status in ("closed", "failed"):
            a.pop(chat_id, None)
            changed = True
        else:
            entry["pending"] = False
            entry["turn_id"] = None
            changed = True
    if changed:
        D._feishu_save()


# ---- per-chat 串行 worker（D6：on_message 秒回，不阻塞 ws 回调）----

_feishu_queues = {}       # chat_id -> queue.Queue
_feishu_workers = {}      # chat_id -> Thread
_feishu_queue_lock = threading.Lock()


def _feishu_handle(chat_id, payload):
    """worker 内串行处理单条入队消息（测试可直接调用）。同 chat 串行、不同 chat 不共享上下文。
    #24：本 chat 属于旧建卡还是看板助手由 payload 的路由结论（kind）决定——`legacy_text`
    在 worker 侧再校验活跃旧会话是否仍在（入队与处理之间可能被 `close` 摘掉），
    已无活跃旧会话时改走助手，避免复活已删除的旧默认新建分支。"""
    kind = (payload or {}).get("kind")
    if kind == "text":
        D._feishu_handle_text(chat_id, payload.get("text") or "")
    elif kind == "legacy_text":
        if D._feishu_chats().get(chat_id) is None:
            _feishu_assistant_safe(chat_id, payload.get("text") or "")
        else:
            D._feishu_handle_text(chat_id, payload.get("text") or "")
    elif kind == "assistant":
        _feishu_assistant_safe(chat_id, payload.get("text") or "",
                               payload.get("handles") or [])
    elif kind == "material":
        D._feishu_handle_material(chat_id, payload.get("handle") or {})
    elif kind == "close":
        D._feishu_close_chat(chat_id)
    elif kind == "new":
        D._feishu_start_build(chat_id)
    elif kind == "confirm":
        D._feishu_confirm(chat_id, D._feishu_chats().get(chat_id))


def _feishu_assistant_safe(chat_id, text, handles=None):
    """助手编排兜底：任何未预期异常都转成明确降级回复（不静默、不冒泡到 worker 日志了事）。
    handles 非空（群/私聊图文消息的图片素材）→ 下载后随本轮附件提交（复用 _feishu_assistant_round）。"""
    try:
        if handles:
            return _feishu_assistant_round(
                chat_id, text,
                prepare=lambda: _feishu_assistant_handles_prepare(handles))
        return _feishu_handle_assistant_text(chat_id, text)
    except Exception as e:  # noqa: BLE001 - 助手链路失败一律降级回复
        log.error(f"飞书助手处理异常（{chat_id}）：{e}")
        D._feishu_notify(chat_id, D.FEISHU_ASSISTANT_DOWN_HINT)
        return False


def _feishu_worker(chat_id, q):
    while True:
        payload = q.get()
        # #44：只有真正入队处理的助手/旧建卡文本/素材三类消息贴 Typing 表情（close/confirm/new 不贴），
        # 处理前贴、finally 撤；create 失败（rid 为 None）不撤。task_done 在最外层 finally，不被表情异常吞掉。
        kind = (payload or {}).get("kind")
        mid = (payload or {}).get("message_id")
        rid = D._feishu_typing(mid) if (mid and kind in ("assistant", "legacy_text", "material")) else None
        try:
            D._feishu_handle(chat_id, payload)
        except Exception as e:
            log.error(f"飞书建卡消息处理异常（{chat_id}）：{e}")
        finally:
            try:
                if rid:
                    D._feishu_typing(mid, rid)
            finally:
                q.task_done()


def _feishu_enqueue(chat_id, payload):
    """#22 D6：入队即返回（per-chat worker 异步串行处理），不阻塞 ws 回调线程。"""
    with _feishu_queue_lock:
        q = D._feishu_queues.get(chat_id)
        if q is None:
            q = queue.Queue()
            D._feishu_queues[chat_id] = q
            t = threading.Thread(target=_feishu_worker, args=(chat_id, q), daemon=True)
            D._feishu_workers[chat_id] = t
            t.start()
    q.put(payload)


# ---- 空闲会话过期兜底（D8″：释放路径 ③）----

def _feishu_expire_idle_chats():
    """#22 D8″：主循环低频巡检（节流 ≥300s）。只处理 channel=feishu 且 status=idle、updated_at
    超阈值、本地无 pending 的会话 → close + 清本地状态 + 日志；不碰 web 会话。
    IDLE_SEC=0 关闭兜底；飞书未启用（无客户端）时无会话可释放，直接跳过（也避免单测触网）。"""
    if D.feishu is None or D.FEISHU_CHAT_IDLE_SEC <= 0:
        return
    now = time.monotonic()
    if D._last_feishu_idle_sweep is not None and now - D._last_feishu_idle_sweep < D.FEISHU_IDLE_SWEEP_INTERVAL:
        return
    D._last_feishu_idle_sweep = now
    try:
        rows = D.api_board("GET", "/chat?channel=feishu&status=idle&limit=50") or []
    except Exception as e:
        log.warning(f"飞书空闲会话巡检：拉取失败（{e}）")
        return
    closed = 0
    for row in rows:
        if (row.get("channel") or "web") != "feishu" or row.get("status") != "idle":
            continue
        age = D._session_age_sec(row.get("updated_at"))
        if age is None or age <= D.FEISHU_CHAT_IDLE_SEC:
            continue
        cid, local = D._feishu_local_by_session(row.get("id"))
        if local is not None and ((local.get("pending") or {}).get("text")):
            continue   # 本地有未投递消息，交给用户消息驱动，不兜底关闭
        try:
            D.api_board("POST", f"/chat/{row.get('id')}/close")
        except Exception as e:
            log.warning(f"飞书空闲会话关闭失败（{row.get('id')}）：{e}")
            continue
        if cid is not None:
            D._feishu_chats().pop(cid, None)
        D._feishu_save()
        log.info(f"飞书空闲会话已过期关闭：session={row.get('id')}"
                 f"（闲置 > {D.FEISHU_CHAT_IDLE_SEC}s）")
        closed += 1
    if closed:
        log.info(f"飞书空闲会话巡检：关闭 {closed} 个")


