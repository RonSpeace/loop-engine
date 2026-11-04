"""assistant 蓝图：飞书看板助手会话。

#32 Phase 6（卡 #39）由 board.py 原样迁入：@app.route 改为本蓝图 bp.route，留驻 board.py 的名字经 board_modules.runtime.resolve 晚绑定。
"""


from board_modules.runtime import resolve


import base64
import json
import threading


from flask import Blueprint, jsonify, request


from board_modules.services.assistant_runtime import ASSISTANT_HISTORY_DEFAULT
from board_modules.services.assistant_runtime import ASSISTANT_HISTORY_MAX
from board_modules.services.assistant_runtime import ASSISTANT_TERMINAL_STATUSES
from board_modules.services.assistant_runtime import ASSISTANT_TIMEOUT_SEC
from board_modules.services.assistant_runtime import _assistant_add_event
from board_modules.services.assistant_runtime import _assistant_attach_kind
from board_modules.services.assistant_runtime import _assistant_close_runtime
from board_modules.services.assistant_runtime import _assistant_config_model
from board_modules.services.assistant_runtime import _assistant_get_or_create
from board_modules.services.assistant_runtime import _assistant_materials_dir
from board_modules.services.assistant_runtime import _assistant_owned
from board_modules.services.assistant_runtime import _assistant_parallel_full
from board_modules.services.assistant_runtime import _assistant_public
from board_modules.services.assistant_runtime import _assistant_resolve_model
from board_modules.services.assistant_runtime import _assistant_row
from board_modules.services.assistant_runtime import assistant_lock
from board_modules.services.assistant_runtime import assistant_runtime
from board_modules.services.chat_runtime import CHAT_PARALLEL_FULL_MSG
from board_modules.services.chat_runtime import _material_name_ok
from board_modules.services.chat_runtime import _write_material_to_dir


bp = Blueprint("assistant", __name__)


def _assistant_parse_attachments(raw):
    """#44 附件契约校验（fail-closed，全部在落盘前）：返回 (items, err)。
    items = [{"name", "data"(bytes)}]；形状非法/数量超限/非法文件名/base64 解不开/解码后超 20MB
    一律给中文原因（调用方 400）。exts 与旧建卡飞书素材同源（FEISHU_MATERIAL_EXTS）。"""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "attachments 必须为数组"
    max_files = resolve("FEISHU_MATERIAL_MAX_FILES")
    if len(raw) > max_files:
        return None, f"单轮最多 {max_files} 个附件"
    max_bytes = resolve("FEISHU_MATERIAL_MAX_BYTES")
    items = []
    for item in raw:
        if not isinstance(item, dict):
            return None, "attachments 元素需为对象"
        name = item.get("name")
        if not isinstance(name, str) or not _material_name_ok(name, resolve("FEISHU_MATERIAL_EXTS")):
            return None, f"非法文件名: {name}"
        b64 = item.get("content_b64")
        if not isinstance(b64, str):
            return None, f"content_b64 必须为 base64 字符串: {name}"
        try:
            buf = base64.b64decode(b64, validate=True)
        except Exception:  # noqa: BLE001 - 非法 base64 → 400（零落盘）
            return None, f"base64 解码失败: {name}"
        if len(buf) > max_bytes:
            return None, f"附件超过 {max_bytes // (1024 * 1024)}MB 上限: {name}"
        items.append({"name": name, "data": buf})
    return items, None


def _assistant_save_attachments(sid, items):
    """#44 逐项落盘到本会话附件目录（MATERIALS_ROOT/_assistant/asst-<sid>/），
    返回 [{filename, path, size, kind}]（kind = image/text/path_only）。中途失败抛异常 →
    调用方 400 且不分配 turn（已写的文件允许残留，不进任何 prompt）。"""
    out = []
    d = _assistant_materials_dir(sid)
    for it in items:
        saved = _write_material_to_dir(d, it["name"], it["data"])
        out.append({"filename": saved["filename"], "path": saved["path"],
                    "size": saved["size"], "kind": _assistant_attach_kind(saved["filename"])})
    return out


def _assistant_user_event_text(text, saved):
    """#44 user 事件留痕：附件清单前置（只写文件名与分类，不写 base64/正文）+ 原文本。"""
    if not saved:
        return text
    kinds = {"image": "图片", "text": "文本", "path_only": "仅路径"}
    head = "\n".join(f"[附件] {s['filename']}（{kinds.get(s['kind'], s['kind'])}）" for s in saved)
    return f"{head}\n\n{text}"


@bp.route('/api/assistant/sessions', methods=['POST'])
def api_assistant_session_create():
    """按 feishu_chat_id 幂等 get-or-create 助手会话（body {feishu_chat_id, model?}）。
    插入即落受约束状态 'idle'；并发撞部分唯一索引时回读既有活跃行（不插第二行）。"""
    data = request.get_json(silent=True) or {}
    chat_id = str(data.get("feishu_chat_id") or "").strip()
    if not chat_id:
        return jsonify({'error': 'feishu_chat_id 必填'}), 400
    if len(chat_id) > 128:
        return jsonify({'error': 'feishu_chat_id 超长'}), 400
    model = data.get("model")
    if model is not None and not isinstance(model, str):
        return jsonify({'error': 'model 必须为字符串'}), 400
    model = (model or "").strip() or None
    row, _created = _assistant_get_or_create(chat_id, model)
    return jsonify(_assistant_public(row))


@bp.route('/api/assistant/sessions/<int:sid>', methods=['GET'])
def api_assistant_session_get(sid):
    """读助手会话行（双绑定归属校验：session id + feishu_chat_id）。"""
    row = _assistant_owned(sid, request.args.get('feishu_chat_id'))
    if row is None:
        return jsonify({'error': '助手会话不存在或归属不匹配'}), 404
    return jsonify(_assistant_public(row))


@bp.route('/api/assistant/sessions/<int:sid>/prompt', methods=['POST'])
def api_assistant_prompt(sid):
    """起一轮助手对话：分配新 turn_id、置 running、写 user 事件、按需 boot ACP 会话，
    后台线程跑 prompt（deadline 由调用方 remaining_sec 驱动）。running/cancelling → 409。
    #44 可选 attachments（≤5 项 {name, content_b64}）：在分配 turn 之前校验并落盘到本会话附件目录，
    任一校验不过 → 400 且零落盘；落盘在锁内、取 turn 之前（校验类出口零落盘）。"""
    cl = request.content_length
    if cl is not None and cl > resolve("FEISHU_MATERIAL_BODY_LIMIT"):
        return jsonify({'error': '上传内容超过大小限制'}), 413
    data = request.get_json(silent=True) or {}
    chat_id = str(data.get("feishu_chat_id") or "").strip()
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({'error': '消息不能为空'}), 400
    if len(text) > 4000:
        return jsonify({'error': '消息超过 4000 字符'}), 400
    try:
        remaining = int(data.get("remaining_sec"))
    except (TypeError, ValueError):
        remaining = ASSISTANT_TIMEOUT_SEC
    remaining = max(1, min(remaining, 600))
    with assistant_lock:
        row = _assistant_owned(sid, chat_id)
        if row is None:
            return jsonify({'error': '助手会话不存在或归属不匹配'}), 404
        if row["status"] in ("running", "cancelling"):
            return jsonify({'error': '会话正在运行，请等待本轮结束'}), 409
        if row["status"] not in ("idle", "interrupted"):
            return jsonify({'error': f'会话状态 {row["status"]} 不可对话'}), 400
        if _assistant_parallel_full():
            return jsonify({'error': CHAT_PARALLEL_FULL_MSG}), 429
        items, err = _assistant_parse_attachments(data.get("attachments"))
        if err:
            return jsonify({'error': err}), 400
        try:
            saved = _assistant_save_attachments(sid, items)
        except (ValueError, OSError) as e:
            # 写盘中途失败：不分配 turn、不起线程、不写 user 事件；已写文件允许残留（不进任何 prompt）
            return jsonify({'error': f'附件落盘失败: {e}'}), 400
        turn_id = (row["turn_id"] or 0) + 1
        model, source, reason = _assistant_resolve_model(row["model"] or _assistant_config_model())
        resolve("_assistant_set")(sid, status="running", turn_id=turn_id, model=model,
                       assistant_model_source=source, error=None)
        _assistant_add_event(sid, "user", _assistant_user_event_text(text, saved))
        if reason:
            _assistant_add_event(sid, "system", f"助手模型回落：{reason}")
    threading.Thread(target=resolve("_assistant_prompt_thread"),
                     args=(sid, text, turn_id, remaining, saved or None),
                     daemon=True, name=f"req-board-assistant-{sid}").start()
    return jsonify({"id": sid, "turn_id": turn_id, "status": "running"})


@bp.route('/api/assistant/sessions/<int:sid>/events', methods=['GET'])
def api_assistant_events(sid):
    """增量读取助手事件（after 游标；ORDER BY id，单次上限 500），附会话状态快照。"""
    row = _assistant_owned(sid, request.args.get('feishu_chat_id'))
    if row is None:
        return jsonify({'error': '助手会话不存在或归属不匹配'}), 404
    try:
        after = int(request.args.get('after', '0'))
    except (TypeError, ValueError):
        after = 0
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT id, kind, content, ts FROM feishu_assistant_events "
            "WHERE assistant_session_id = ? AND id > ? ORDER BY id LIMIT 500",
            (sid, after)).fetchall()
    pending = None
    if row["pending_json"]:
        try:
            pending = json.loads(row["pending_json"])
        except Exception:  # noqa: BLE001 - 存量/异常内容按无 pending 降级
            pending = None
    return jsonify({"session": {"id": row["id"], "status": row["status"], "turn_id": row["turn_id"],
                                "session_id": row["session_id"],
                                "awaiting_project": row["awaiting_project"], "pending": pending,
                                "last_event_id": row["last_event_id"]},
                    "events": [dict(r) for r in rows]})


@bp.route('/api/assistant/sessions/<int:sid>/cancel', methods=['POST'])
def api_assistant_cancel(sid):
    """取消进行中的一轮：body {feishu_chat_id, turn_id}。锁内标 cancelling，锁外对当前
    ACPSession cancel 并关闭进程（保留 ACP session_id 供下次 resume），写 interrupted 事件。"""
    data = request.get_json(silent=True) or {}
    chat_id = str(data.get("feishu_chat_id") or "").strip()
    if data.get("turn_id") is None:
        return jsonify({'error': 'turn_id 必填'}), 400
    try:
        turn_id = int(data.get("turn_id"))
    except (TypeError, ValueError):
        return jsonify({'error': 'turn_id 必须为整数'}), 400
    with assistant_lock:
        row = _assistant_owned(sid, chat_id)
        if row is None:
            return jsonify({'error': '助手会话不存在或归属不匹配'}), 404
        if row["turn_id"] != turn_id:
            return jsonify({'error': 'turn_id 不匹配当前轮，拒绝取消'}), 409
        if row["status"] not in ("running", "cancelling"):
            return jsonify({'ok': True})   # 幂等：已收尾
        resolve("_assistant_set")(sid, status="cancelling")
        rt = assistant_runtime.pop(sid, None)
    if rt is not None:
        # 慢操作不持锁（cancel + 杀进程最长数秒），完成后再回锁收尾
        _assistant_close_runtime(rt)
    with assistant_lock:
        cur = _assistant_row(sid)
        if cur is not None and cur["turn_id"] == turn_id and cur["status"] == "cancelling":
            resolve("_assistant_set")(sid, status="interrupted", error="本轮已取消")
            _assistant_add_event(sid, "system", "已取消本轮，会话中断（可继续）")
    return jsonify({'ok': True})


@bp.route('/api/assistant/sessions/<int:sid>/close', methods=['POST'])
def api_assistant_close(sid):
    """关闭助手会话（终态 closed，释放唯一约束）；body {feishu_chat_id}。"""
    data = request.get_json(silent=True) or {}
    chat_id = str(data.get("feishu_chat_id") or "").strip()
    with assistant_lock:
        row = _assistant_owned(sid, chat_id)
        if row is None:
            return jsonify({'error': '助手会话不存在或归属不匹配'}), 404
        if row["status"] not in ASSISTANT_TERMINAL_STATUSES:
            resolve("_assistant_set")(sid, status="closed")
            _assistant_add_event(sid, "system", "助手会话已关闭")
        rt = assistant_runtime.pop(sid, None)
    if rt is not None:
        _assistant_close_runtime(rt)
    return jsonify({'ok': True})


@bp.route('/api/assistant/history', methods=['GET'])
def api_assistant_history():
    """历史聚合列表：助手会话（source='assistant'）+ channel='feishu' 旧建卡会话（source='chat'）。
    两表自增 id 会撞号 → 每项带 source，跨表引用一律用复合 (source, id)；
    before_id 游标格式 = '<source>:<id>'（返回该项之后的更早页，按 updated_at 倒序）。"""
    try:
        limit = max(1, min(int(request.args.get('limit')), ASSISTANT_HISTORY_MAX))
    except (TypeError, ValueError):
        limit = ASSISTANT_HISTORY_DEFAULT
    before_id = (request.args.get('before_id') or '').strip()
    with resolve("get_db")() as conn:
        assists = [dict(r) for r in conn.execute(
            "SELECT id, feishu_chat_id, status, session_id, created_at, updated_at "
            "FROM feishu_assistant_sessions ORDER BY updated_at DESC, id DESC LIMIT 500").fetchall()]
        chats = [dict(r) for r in conn.execute(
            "SELECT id, project, status, feishu_chat_id, created_at, updated_at "
            "FROM chat_sessions WHERE channel = 'feishu' ORDER BY updated_at DESC, id DESC LIMIT 500").fetchall()]
        items = []
        for r in assists:
            # #30：末条事件可能是 kind='card'（页 spec JSON）——历史摘要跳过它，避免接口吐卡片 JSON 片段
            last = conn.execute("SELECT content FROM feishu_assistant_events "
                                "WHERE assistant_session_id = ? AND kind != 'card' "
                                "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            items.append({"source": "assistant", "id": r["id"],
                          "feishu_chat_id": r["feishu_chat_id"], "project": None,
                          "status": r["status"], "created_at": r["created_at"],
                          "updated_at": r["updated_at"],
                          "last_message": (last["content"] if last else None)})
        for r in chats:
            last = conn.execute("SELECT content FROM chat_events WHERE chat_id = ? "
                                "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            items.append({"source": "chat", "id": r["id"], "feishu_chat_id": r["feishu_chat_id"],
                          "project": r["project"], "status": r["status"],
                          "created_at": r["created_at"], "updated_at": r["updated_at"],
                          "last_message": (last["content"] if last else None)})
    items.sort(key=lambda x: (str(x["updated_at"] or ""), x["source"], x["id"]), reverse=True)
    if before_id:
        marker = next((i for i, x in enumerate(items)
                       if f'{x["source"]}:{x["id"]}' == before_id), None)
        items = items[marker + 1:] if marker is not None else []
    for x in items:
        x["last_message"] = (None if x["last_message"] is None
                             else str(x["last_message"])[:200])
    return jsonify(items[:limit])
