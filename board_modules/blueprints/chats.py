"""chats 蓝图：建卡对话与需求聊天（含飞书素材上传）。

#32 Phase 6（卡 #39）由 board.py 原样迁入：@app.route 改为本蓝图 bp.route，留驻 board.py 的名字经 board_modules.runtime.resolve 晚绑定。
"""


from board_modules.runtime import resolve


from pathlib import Path
import base64
import json
import threading
import time


from flask import Blueprint, jsonify, request


from board_modules.services.chat_runtime import CHAT_MATERIAL_BODY_LIMIT
from board_modules.services.chat_runtime import CHAT_MATERIAL_EXTS
from board_modules.services.chat_runtime import CHAT_MATERIAL_MAX_BYTES
from board_modules.services.chat_runtime import CHAT_MATERIAL_MAX_FILES
from board_modules.services.chat_runtime import CHAT_PARALLEL_FULL_MSG
from board_modules.services.chat_runtime import CHAT_PARALLEL_LIMIT
from board_modules.services.chat_runtime import _chat_add_event
from board_modules.services.chat_runtime import _chat_material_paths
from board_modules.services.chat_runtime import _chat_running_count
from board_modules.services.chat_runtime import _chat_session_alive
from board_modules.services.chat_runtime import _chat_set
from board_modules.services.chat_runtime import _chat_start_new
from board_modules.services.chat_runtime import _close_chat_runtime
from board_modules.services.chat_runtime import _finalize_then_close_chat
from board_modules.services.chat_runtime import _material_name_ok
from board_modules.services.chat_runtime import _read_capped
from board_modules.services.chat_runtime import _start_chat_round
from board_modules.services.chat_runtime import _write_chat_material
from board_modules.services.chat_runtime import chat_booting
from board_modules.services.chat_runtime import chat_lock
from board_modules.services.chat_runtime import chat_runtime
from board_modules.services.reqchat_runtime import REQ_CHAT_PARALLEL_FULL_MSG
from board_modules.services.reqchat_runtime import REQ_CHAT_PARALLEL_LIMIT
from board_modules.services.reqchat_runtime import _reqchat_active
from board_modules.services.reqchat_runtime import _reqchat_add_event
from board_modules.services.reqchat_runtime import _reqchat_cancel
from board_modules.services.reqchat_runtime import _reqchat_close
from board_modules.services.reqchat_runtime import _reqchat_create
from board_modules.services.reqchat_runtime import _reqchat_current
from board_modules.services.reqchat_runtime import _reqchat_running_count
from board_modules.services.reqchat_runtime import _reqchat_session_alive
from board_modules.services.reqchat_runtime import _reqchat_start_round
from board_modules.services.reqchat_runtime import reqchat_booting
from board_modules.services.reqchat_runtime import reqchat_lock
from board_modules.services.reqchat_runtime import reqchat_runtime


bp = Blueprint("chats", __name__)


FEISHU_MATERIAL_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.pdf',
                        '.txt', '.md', '.csv', '.json', '.doc', '.docx', '.xls', '.xlsx',
                        '.ppt', '.pptx', '.zip', '.html', '.htm')


@bp.route('/api/chat', methods=['GET'])
def api_chat_list():
    """最近建卡会话列表；project 可精确筛选，供前端恢复该项目上次会话。
    status 支持逗号分隔多值（如 created,closed）；给定 status 或 limit 时按 id 倒序分页
    （before_id 取更早、limit 控制页大小），供历史会话增量加载。
    #22：channel 缺省只返回 web（fail-closed）；channel=feishu 只返回飞书会话；channel=all 返回全部。"""
    project = request.args.get('project', '').strip()
    status = request.args.get('status', '').strip()
    before_id = request.args.get('before_id', '').strip()
    limit = request.args.get('limit', '').strip()
    # #22 双通道隔离（服务端权威）：缺省只返回 channel='web'（fail-closed）；显式 channel=feishu
    # 只返回飞书会话；channel=all 为逃生口（两端都给，仅内部/调试用）。
    channel = request.args.get('channel', '').strip()
    with resolve("get_db")() as conn:
        sql = (
            "SELECT id, project, status, channel, session_id, model, req_id, draft_title, error, interrupt_source, "
            "thinking, harness, context_pct, context_tokens, max_tokens, workdir, branch, finalized, "
            "created_at, updated_at FROM chat_sessions "
        )
        params = []
        where = []
        if project:
            where.append("project = ?")
            params.append(project)
        if channel == 'all':
            pass
        else:
            where.append("channel = ?")
            params.append('feishu' if channel == 'feishu' else 'web')
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if len(statuses) == 1:
                where.append("status = ?")
                params.append(statuses[0])
            elif statuses:
                where.append(f"status IN ({','.join('?' * len(statuses))})")
                params.extend(statuses)
        if before_id:
            try:
                where.append("id < ?")
                params.append(int(before_id))
            except ValueError:
                pass
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        # #22：分页分支条件由「带 status」扩为「带 status 或 limit」——飞书视图用 limit 分页、
        # 不带 status 也能走 before_id 游标；不带任何参数（网页首屏）维持原活跃优先固定分支。
        if status or limit:
            try:
                lim = max(1, min(int(limit), 50))
            except ValueError:
                lim = 20
            sql += "ORDER BY id DESC LIMIT ?"
            params.append(lim)
        else:
            sql += (
                "ORDER BY CASE WHEN status IN ('idle','running','interrupted') THEN 0 ELSE 1 END, "
                "id DESC LIMIT 20"
            )
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/chat', methods=['POST'])
def api_chat_new():
    """新建建卡会话 {project, channel?}。channel ∈ web（默认）/ feishu。
    同项目同通道已有活跃会话（idle/running）时 409 返回现有会话；
    全站 running 会话达到 CHAT_PARALLEL_LIMIT 时 429（并行项目上限，两通道共用）。"""
    data = request.get_json(silent=True) or {}
    project = str(data.get("project") or "").strip()
    channel = str(data.get("channel") or "web").strip() or "web"
    if channel not in ("web", "feishu"):
        return jsonify({'error': f'未知 channel: {channel}（仅支持 web / feishu）'}), 400
    if project not in resolve("PROJECT_PATHS"):
        return jsonify({'error': f'未知项目: {project}（projects.json 中不存在）'}), 400
    with chat_lock:
        with resolve("get_db")() as conn:
            active = conn.execute(
                "SELECT id, project FROM chat_sessions WHERE project = ? AND channel = ? "
                "AND status IN ('idle','running') ORDER BY id DESC LIMIT 1", (project, channel)).fetchone()
            if active:
                return jsonify({'error': '该项目已有活跃建卡会话', 'chat_id': active['id'],
                                'project': active['project']}), 409
            if _chat_running_count() >= CHAT_PARALLEL_LIMIT:
                return jsonify({'error': CHAT_PARALLEL_FULL_MSG}), 429
            cur = conn.execute(
                "INSERT INTO chat_sessions (project, status, channel, created_at, updated_at) "
                "VALUES (?, 'running', ?, ?, ?)",
                (project, channel, resolve("now_str")(), resolve("now_str")()))
            chat_id = cur.lastrowid
            conn.commit()
    _chat_add_event(chat_id, "system", f"会话创建中（项目 {project}）…")
    _chat_start_new(chat_id, project)
    return jsonify({'id': chat_id, 'project': project, 'status': 'running'}), 201


@bp.route('/api/chat/<int:chat_id>', methods=['GET'])
def api_chat_get(chat_id):
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return jsonify({'error': '会话不存在'}), 404
    return jsonify(dict(row))


@bp.route('/api/chat/<int:chat_id>/events', methods=['GET'])
def api_chat_events(chat_id):
    """查询会话事件（after 增量拉取）"""
    try:
        after = int(request.args.get('after', '0'))
    except ValueError:
        after = 0
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT id, kind, content, ts FROM chat_events WHERE chat_id = ? AND id > ? ORDER BY id LIMIT 500",
            (chat_id, after)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/chat/<int:chat_id>/materials', methods=['POST'])
def api_chat_materials(chat_id):
    """#12 建卡对话上传本机 html 参考文件（multipart，字段名 file，单次 ≤5 个、单文件 ≤10MB、仅 .html/.htm）。
    fail-closed：会话存在 + 未建卡 + 文件名合法 + 读入字节判定；**任一失败零落盘**（先全校验再统一写盘）。
    体积权威判定 = 逐文件 _read_capped 读入字节；content_length 仅作早退优化，缺失（chunked）不 fail-open。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT project, status, channel FROM chat_sessions WHERE id = ?",
                           (chat_id,)).fetchone()
    if row is None:
        return jsonify({'error': '会话不存在'}), 404
    # #22：网页素材端点为 web 会话专用（.html/.htm 白名单）；飞书会话走 /feishu-materials。
    if (row['channel'] or 'web') != 'web':
        return jsonify({'error': '本会话为飞书建卡会话，请使用飞书素材端点'}), 400
    if row['status'] == 'created':
        return jsonify({'error': '本会话已建卡，不能上传参考文件'}), 409
    cl = request.content_length
    if cl is not None and cl > CHAT_MATERIAL_BODY_LIMIT:
        return jsonify({'error': '上传内容超过大小限制'}), 413
    files = [f for f in request.files.getlist('file') if f and (f.filename or '').strip()]
    if not files:
        return jsonify({'error': '未收到文件'}), 400
    if len(files) > CHAT_MATERIAL_MAX_FILES:
        return jsonify({'error': f'单轮最多 {CHAT_MATERIAL_MAX_FILES} 个文件'}), 400
    pending = []
    for f in files:
        raw = f.filename or ''
        if not _material_name_ok(raw):
            return jsonify({'error': f'非法文件名: {raw}'}), 400
        data, over = _read_capped(f.stream, CHAT_MATERIAL_MAX_BYTES)
        if over:
            return jsonify({'error': '文件超过 10MB 上限'}), 413
        pending.append((raw, data))
    try:
        saved = [_write_chat_material(row['project'], chat_id, raw, data) for raw, data in pending]
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'files': saved})


@bp.route('/api/chat/<int:chat_id>/feishu-materials', methods=['POST'])
def api_chat_feishu_materials(chat_id):
    """#22 飞书建卡会话上传素材（JSON + base64）：{files:[{name, content_b64}]}。
    仅 channel=='feishu' 会话可调（否则 400）；白名单 = FEISHU_MATERIAL_EXTS，单文件 ≤20MB、单轮 ≤5；
    先全校验后落盘（任一失败零落盘）。体积权威判定 = base64 解码后字节数；content_length 仅作早退优化。
    响应 {files:[{filename,size,path,over_read_limit}]}（over_read_limit 沿用 CHAT_MATERIAL_READ_CAP，仅标记）。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT project, status, channel FROM chat_sessions WHERE id = ?",
                           (chat_id,)).fetchone()
    if row is None:
        return jsonify({'error': '会话不存在'}), 404
    if (row['channel'] or 'web') != 'feishu':
        return jsonify({'error': '仅飞书建卡会话可上传飞书素材'}), 400
    if row['status'] == 'created':
        return jsonify({'error': '本会话已建卡，不能上传参考文件'}), 409
    cl = request.content_length
    if cl is not None and cl > resolve("FEISHU_MATERIAL_BODY_LIMIT"):
        return jsonify({'error': '上传内容超过大小限制'}), 413
    data = request.get_json(silent=True) or {}
    files = data.get('files')
    if not isinstance(files, list) or not files:
        return jsonify({'error': '未收到文件'}), 400
    if len(files) > resolve("FEISHU_MATERIAL_MAX_FILES"):
        return jsonify({'error': f'单轮最多 {resolve("FEISHU_MATERIAL_MAX_FILES")} 个文件'}), 400
    pending = []
    for item in files:
        if not isinstance(item, dict):
            return jsonify({'error': 'files 元素需为对象'}), 400
        raw_name = item.get('name')
        if not isinstance(raw_name, str) or not _material_name_ok(raw_name, resolve("FEISHU_MATERIAL_EXTS")):
            return jsonify({'error': f'非法文件名: {raw_name}'}), 400
        b64 = item.get('content_b64')
        if not isinstance(b64, str):
            return jsonify({'error': f'content_b64 必须为 base64 字符串: {raw_name}'}), 400
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:  # noqa: BLE001 - 非法 base64 → 400（零落盘）
            return jsonify({'error': f'base64 解码失败: {raw_name}'}), 400
        if len(raw) > resolve("FEISHU_MATERIAL_MAX_BYTES"):
            return jsonify({'error': f'文件超过 {resolve("FEISHU_MATERIAL_MAX_BYTES") // (1024 * 1024)}MB 上限'}), 413
        pending.append((raw_name, raw))
    try:
        saved = [_write_chat_material(row['project'], chat_id, name, raw) for name, raw in pending]
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'files': saved})


@bp.route('/api/chat/<int:chat_id>/feishu-events', methods=['POST'])
def api_chat_feishu_events(chat_id):
    """#22 飞书侧回放前置对话与辅助提示：{events:[{kind:'user'|'agent'|'system', content}]}。
    仅 channel=='feishu' 会话可调（否则 400）；落 chat_events（复用 _chat_add_event [:4000] 截断）。
    只承载「建卡会话创建前的飞书对话」与 dispatcher 侧辅助提示，不回放用户原文、不回放建卡事件。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT channel FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return jsonify({'error': '会话不存在'}), 404
    if (row['channel'] or 'web') != 'feishu':
        return jsonify({'error': '仅飞书建卡会话可写入飞书事件'}), 400
    data = request.get_json(silent=True) or {}
    events = data.get('events')
    if not isinstance(events, list):
        return jsonify({'error': 'events 必须为数组'}), 400
    for ev in events:
        if not isinstance(ev, dict):
            return jsonify({'error': 'events 元素需为对象'}), 400
        kind = str(ev.get('kind') or '')
        if kind not in ('user', 'agent', 'system'):
            return jsonify({'error': f'未知事件类型: {kind}'}), 400
    for ev in events:
        content = ev.get('content')
        _chat_add_event(chat_id, ev['kind'], '' if content is None else content)
    return jsonify({'ok': True})


@bp.route('/api/chat/<int:chat_id>/prompt', methods=['POST'])
def api_chat_prompt(chat_id):
    """用户发一条消息（回合制：本请求只排队，后台线程驱动 kimi 完整跑一轮；
    进程失效时自动恢复会话再排队，避免永久 running）。
    #12：可带 attachments（本会话已上传的参考文件名数组）——服务端按「累计数量 + 文件名合法性 +
    路径复核」重建绝对路径，拼进送给 kimi 的 prompt；非法/越界/不存在一律 400 且不发起请求。"""
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()
    if not text:
        return jsonify({'error': '消息不能为空'}), 400
    if len(text) > 4000:
        return jsonify({'error': '消息超过 4000 字符'}), 400
    attachments = data.get('attachments') or []
    if not isinstance(attachments, list) or any(not isinstance(a, str) for a in attachments):
        return jsonify({'error': 'attachments 必须为字符串数组'}), 400
    if len(attachments) > CHAT_MATERIAL_MAX_FILES:
        return jsonify({'error': f'单轮最多 {CHAT_MATERIAL_MAX_FILES} 个参考文件'}), 400
    prompt_text, event_content = text, text
    if attachments:
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT project, channel FROM chat_sessions WHERE id = ?",
                               (chat_id,)).fetchone()
        if row is None:
            return jsonify({'error': '会话不存在'}), 404
        # #22 素材白名单与上传侧同源：按会话行 channel 取（未知/缺失通道回落 web，fail-closed）
        is_feishu = (row['channel'] or 'web') == 'feishu'
        exts = resolve("FEISHU_MATERIAL_EXTS") if is_feishu else CHAT_MATERIAL_EXTS
        paths, err = _chat_material_paths(row['project'], chat_id, attachments, exts)
        if err:
            return jsonify({'error': err}), 400
        names = [Path(p).name for p in paths]
        # user 事件清单**前置**：_chat_add_event 的 [:4000] 截断不会吞掉清单（正文尾部可能被截，已知边界）
        event_content = "📎 参考文件：" + ", ".join(names) + "\n\n" + text
        # #22 注入文案按 channel 分流（web 保持原文案逐字不变）
        if is_feishu:
            ref_hint = ("本轮参考文件（飞书上传的图片/文档，可用 read_file 读取，图片走多模态查看，"
                        "单文件读取上限 1MB）：\n")
        else:
            ref_hint = ("本轮参考文件（本机 html 原型，可用 read_file 读取源码理解结构/样式/交互；"
                        "请勿尝试在浏览器打开本地文件）：\n")
        prompt_text = text + "\n\n" + ref_hint + "\n".join(f"- {p}" for p in paths)
    resp, code = _start_chat_round(chat_id, prompt_text, event=('user', event_content))
    return jsonify(resp), code


@bp.route('/api/chat/<int:chat_id>/confirm', methods=['POST'])
def api_chat_confirm(chat_id):
    """用户确认草稿：驱动 agent 调 create_requirement 建卡（进程失效自动恢复）"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return jsonify({'error': '会话不存在'}), 404
    if row['req_id']:
        return jsonify({'error': f'本会话已建卡 #{row["req_id"]}'}), 409
    if not (row['draft_title'] or '').strip():
        return jsonify({'error': '尚无草稿可确认（先让 kimi 聊出草稿）'}), 400
    prompt = ("用户已在页面确认以下需求草稿，请调用 create_requirement 工具建卡：\n"
              f"```draft\n{json.dumps({'title': row['draft_title'], 'project': row['project'], 'notes': row['draft_notes'] or ''}, ensure_ascii=False)}\n```\n"
              "status 默认 later；仅当用户明确要求「开始开发」时才用 open。\n"
              "建卡成功后：简洁汇报需求 ID、标题、项目与状态（later/open），不得继续提问或把话题抛回给用户；\n"
              "最后一行固定输出「当前会话已结束。」")
    resp, code = _start_chat_round(chat_id, prompt, event=('system', '用户已确认草稿，等待建卡…'))
    return jsonify(resp), code


@bp.route('/api/chat/<int:chat_id>/discard', methods=['POST'])
def api_chat_discard(chat_id):
    """取消当前草稿（继续对话）"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT id FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return jsonify({'error': '会话不存在'}), 404
    _chat_set(chat_id, draft_title=None, draft_notes=None)
    _chat_add_event(chat_id, 'system', '草稿已取消，可继续对话')
    return jsonify({'success': True})


@bp.route('/api/chat/<int:chat_id>/cancel', methods=['POST'])
def api_chat_cancel(chat_id):
    """取消进行中的一轮（杀进程，标 interrupted 可续跑）"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return jsonify({'error': '会话不存在'}), 404
    if row['status'] == 'running':
        rt = chat_runtime.get(chat_id)
        if rt:
            try:
                rt['session'].cancel()
                time.sleep(1)
                rt['session'].close(kill=True)
            except Exception:
                pass
            with chat_lock:
                chat_runtime.pop(chat_id, None)
        _chat_set(chat_id, status='interrupted', interrupt_source='user_cancel', error='用户取消本轮')
        _chat_add_event(chat_id, 'system', '已取消本轮，会话中断（可稍后继续）')
    return jsonify({'success': True})


@bp.route('/api/chat/<int:chat_id>/resume', methods=['POST'])
def api_chat_resume(chat_id, resume_prompt=None):
    """恢复中断会话（session/resume 续上下文），可选携带排队消息；
    idle/running 残留死进程时也允许自动转入恢复。
    并行会话：其它项目的活跃会话不再阻塞恢复，只受 CHAT_PARALLEL_LIMIT 全局名额限制。"""
    data = request.get_json(silent=True) or {}
    text = resume_prompt or str(data.get('text') or '').strip()
    with chat_lock:
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            return jsonify({'error': '会话不存在'}), 404
        if row['status'] == 'created':
            return jsonify({'error': f'本会话已建卡 #{row["req_id"]}，不能继续'}), 400
        if chat_id in chat_booting:
            return jsonify({'error': '会话正在完成建立或挂起，请稍候再继续'}), 409
        if row['status'] in ('running', 'idle'):
            rt = chat_runtime.get(chat_id)
            if rt is not None and _chat_session_alive(rt):
                return jsonify({'error': f'会话状态 {row["status"]}，无需恢复'}), 409
            # 进程已失效（board 重启/子进程崩溃残留）：直接走恢复（spawn 会把状态置 running）
        elif row['status'] not in ('interrupted', 'suspended'):
            return jsonify({'error': f'会话状态 {row["status"]} 不可恢复'}), 400
        if row['status'] != 'running' and _chat_running_count() >= CHAT_PARALLEL_LIMIT:
            return jsonify({'error': CHAT_PARALLEL_FULL_MSG}), 429
        resolve("_spawn_resume_round")(chat_id, row['project'], row['session_id'], text or None,
                            event=('user', text) if text else None)
    return jsonify({'success': True}), 202


@bp.route('/api/chat/<int:chat_id>/detach', methods=['POST'])
def api_chat_detach(chat_id):
    """前台切走/切回项目时标记会话归属（并行会话，不中断 running 轮次）。
    detached=true：running 轮次标记为后台继续（进程不杀，跑完自动回收）；
    idle 会话直接回收进程（保留状态与 session_id，下次对话自动恢复）。
    detached=false：会话回到前台，清除后台标记。"""
    data = request.get_json(silent=True) or {}
    detached = bool(data.get("detached", True))
    close_rt = None
    with chat_lock:
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            return jsonify({'error': '会话不存在'}), 404
        rt = chat_runtime.get(chat_id)
        if rt is None:
            return jsonify({'success': True})
        if detached:
            if row['status'] == 'running':
                rt['detached'] = True
                _chat_add_event(chat_id, 'system', '已切换到其它项目，当前一轮在后台继续完成…')
            elif row['status'] == 'idle':
                chat_runtime.pop(chat_id, None)
                close_rt = rt
                _chat_add_event(chat_id, 'system', '已切换到其它项目，会话已挂起，切回后自动继续')
            else:
                return jsonify({'success': True})
        else:
            rt['detached'] = False
    if close_rt:
        _close_chat_runtime(close_rt)
    return jsonify({'success': True})


@bp.route('/api/chat/<int:chat_id>/suspend', methods=['POST'])
def api_chat_suspend(chat_id):
    """挂起会话：停止当前进程，保留 session_id、草稿和事件供稍后恢复。
    兼容接口：新建 UI 改用 /detach（切走不中断 running 轮次），本接口保留给旧流程/外部调用。"""
    with chat_lock:
        with resolve("get_db")() as conn:
            row = conn.execute(
                "SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            return jsonify({'error': '会话不存在'}), 404
        if row['status'] in ('created', 'failed', 'closed'):
            return jsonify({'error': f'会话状态 {row["status"]} 不可挂起'}), 400
        was_running = row['status'] == 'running'
        _chat_set(chat_id, status='suspended', interrupt_source='project_switch', error=None)
        rt = chat_runtime.pop(chat_id, None)
    if rt:
        if was_running:
            try:
                rt['session'].cancel()
            except Exception:
                pass
        _close_chat_runtime(rt)
    _chat_add_event(
        chat_id, 'system',
        '切换项目，当前一轮已中断并挂起；切回后可继续'
        if was_running else '切换项目，会话已挂起；切回后可继续'
    )
    return jsonify({'success': True, 'status': 'suspended'})


@bp.route('/api/chat/<int:chat_id>/close', methods=['POST'])
def api_chat_close(chat_id):
    """明确放弃会话（终态 closed；收尾元信息采集与杀进程放后台执行，接口立即返回）。"""
    with resolve("get_db")() as conn:
        row = conn.execute(
            "SELECT status, project FROM chat_sessions WHERE id = ?", (chat_id,)
        ).fetchone()
    if row is None:
        return jsonify({'error': '会话不存在'}), 404
    rt = chat_runtime.pop(chat_id, None) if row['status'] in ('idle', 'running') else None
    if rt:
        # 收尾元信息（历史框底部展示 used x% / 模型 / 分支等）与杀进程放后台执行，
        # close 立即返回，避免「新建会话」等待旧会话收尾（/usage 最长 5 秒）而卡顿
        if row['status'] == 'idle':
            threading.Thread(target=_finalize_then_close_chat,
                             args=(chat_id, rt, row["project"]), daemon=True).start()
        else:
            try:
                _close_chat_runtime(rt)
            except Exception:
                pass
    if row['status'] not in ('created', 'failed', 'closed'):
        _chat_set(chat_id, status='closed')
        _chat_add_event(chat_id, 'system', '会话已关闭，可新建会话')
    return jsonify({'success': True})


@bp.route('/api/requirements/<int:req_id>/chat', methods=['GET'])
def api_req_chat_get(req_id):
    """获取该需求当前对话（最新一条，任意状态）；无对话返回 chat=null。"""
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return jsonify({'error': '需求不存在'}), 404
    row = _reqchat_current(req_id)
    return jsonify({'chat': dict(row) if row else None})


@bp.route('/api/requirements/<int:req_id>/chat', methods=['POST'])
def api_req_chat_new(req_id):
    """新建需求对话（占位 created，首条消息才创建真实 kimi 会话）；
    存在活跃会话（created/idle/running/interrupted）返回 409 与现有会话。"""
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return jsonify({'error': '需求不存在'}), 404
    active = _reqchat_active(req_id)
    if active is not None:
        return jsonify({'chat': dict(active), 'error': '已有活跃对话'}), 409
    chat_id = _reqchat_create(req_id)
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT * FROM req_chats WHERE id = ?", (chat_id,)).fetchone()
    return jsonify({'chat': dict(row)}), 201


@bp.route('/api/requirements/<int:req_id>/chat/prompt', methods=['POST'])
def api_req_chat_prompt(req_id):
    """发送消息驱动一轮（回合制，202 排队）：无对话自动创建；running 409；终态 400。"""
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()
    if not text:
        return jsonify({'error': '消息不能为空'}), 400
    if len(text) > 4000:
        return jsonify({'error': '消息超过 4000 字符'}), 400
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return jsonify({'error': '需求不存在'}), 404
    active = _reqchat_active(req_id)
    if active is None:
        # 无对话：自动创建（后续对话复用该行，历史 closed 行保留可回看）
        chat_id = _reqchat_create(req_id)
    else:
        chat_id = active["id"]
    _reqchat_add_event(chat_id, "user", text)
    resp, code = _reqchat_start_round(chat_id, text)
    return jsonify(resp), code


@bp.route('/api/requirements/<int:req_id>/chat/events', methods=['GET'])
def api_req_chat_events(req_id):
    """需求对话事件增量拉取（after=事件 id，按当前最新对话会话）。"""
    try:
        after = max(0, int(request.args.get('after') or 0))
    except (TypeError, ValueError):
        after = 0
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return jsonify({'error': '需求不存在'}), 404
    row = _reqchat_current(req_id)
    if row is None:
        return jsonify({'chat': None, 'events': []})
    with resolve("get_db")() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT id, kind, content, ts FROM req_chat_events WHERE req_chat_id = ? AND id > ? ORDER BY id",
            (row["id"], after)).fetchall()]
    return jsonify({'chat': dict(row), 'events': events})


@bp.route('/api/requirements/<int:req_id>/chat/cancel', methods=['POST'])
def api_req_chat_cancel(req_id):
    """取消进行中的一轮（杀进程，标 interrupted 可继续）。"""
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return jsonify({'error': '需求不存在'}), 404
    row = _reqchat_current(req_id)
    if row is None:
        return jsonify({'error': '尚无对话'}), 404
    resp, code = _reqchat_cancel(row["id"])
    return jsonify(resp), code


@bp.route('/api/requirements/<int:req_id>/chat/resume', methods=['POST'])
def api_req_chat_resume(req_id):
    """恢复中断对话（session/resume 续上下文），可选携带排队消息。"""
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return jsonify({'error': '需求不存在'}), 404
    row = _reqchat_current(req_id)
    if row is None:
        return jsonify({'error': '尚无对话'}), 404
    chat_id = row["id"]
    with reqchat_lock:
        if row["status"] == "running":
            if _reqchat_session_alive(reqchat_runtime.get(chat_id)) or chat_id in reqchat_booting:
                return jsonify({'error': '对话正在运行，请等待本轮结束'}), 409
        elif row["status"] in ("failed", "closed"):
            return jsonify({'error': f'对话状态 {row["status"]} 不可恢复（可新建对话）'}), 400
        if _reqchat_running_count() >= REQ_CHAT_PARALLEL_LIMIT:
            return jsonify({'error': REQ_CHAT_PARALLEL_FULL_MSG}), 429
    if text:
        _reqchat_add_event(chat_id, "user", text)
    resp, code = _reqchat_start_round(chat_id, text)
    return jsonify(resp), code


@bp.route('/api/requirements/<int:req_id>/chat/close', methods=['POST'])
def api_req_chat_close(req_id):
    """明确放弃对话（终态 closed，可再新建）：杀进程、保留事件历史。"""
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return jsonify({'error': '需求不存在'}), 404
    row = _reqchat_current(req_id)
    if row is None:
        return jsonify({'error': '尚无对话'}), 404
    resp, code = _reqchat_close(row["id"])
    return jsonify(resp), code
