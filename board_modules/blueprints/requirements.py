"""requirements 蓝图：需求、评审、状态日志、执行事件、运行记录、产物、merge 冲突与网页确认。

#32 Phase 6（卡 #39）由 board.py 原样迁入：@app.route 改为本蓝图 bp.route，留驻 board.py 的名字经 board_modules.runtime.resolve 晚绑定。
"""


from board_modules.runtime import resolve


import re
from pathlib import Path


from flask import Blueprint, Response, jsonify, request, send_file


from acp_shared import flow_ids
from board_modules.common import _header_str
from board_modules.common import _meta_str
from board_modules.common import _nodes_snapshot_error
from board_modules.common import _status_log_meta
from board_modules.common import _ts_ms
from board_modules.common import resolve_artifact
from board_modules.services.requirements import FIELD_UNSET
from board_modules.services.requirements import STATUSES
from board_modules.services.requirements import _add_review
from board_modules.services.requirements import _log_status_change
from board_modules.services.requirements import req_add
from board_modules.services.requirements import req_delete
from board_modules.services.requirements import req_list
from board_modules.services.session_runtime import _role_injection


bp = Blueprint("requirements", __name__)


ARTIFACT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}  # 图片 <img> 直接展示


ARTIFACT_TEXT_EXTS = {".md", ".svg", ".patch"}  # 文本返回（svg 防脚本，按文本传输；.patch=#381 页面 diff 视图）


@bp.route('/api/requirements', methods=['GET'])
def api_list():
    """查询需求。参数: project(精确), status(逗号分隔, 如 open,doing)"""
    return jsonify(req_list(
        project=request.args.get('project', '').strip() or None,
        status=request.args.get('status', '').strip() or None
    ))


@bp.route('/api/requirements', methods=['POST'])
def api_add():
    """新建需求。body: {title, project, notes?, status?, mode?, source?, model?, reasoning_effort?, source_session_id?}，
    status 默认 open(待开发)，可传 later(暂缓)/done(已完成，已实现留档用)/dropped(已废弃)。
    #303 建卡来源元数据为透传自由字段（均可空，向后兼容）：source=建卡来源（dsh/codex/kimi-code/board-chat…）、
    model=建卡模型、reasoning_effort=推理强度（low/high/max 约定值，不枚举校验）、source_session_id=来源会话 id。
    需求全部平级独立，无会话组/追加需求概念（append_to 已废弃，传入返回 400）"""
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    project = (data.get('project') or '').strip()
    notes = (data.get('notes') or '').strip()
    status = (data.get('status') or 'open').strip()
    mode = (data.get('mode') or '').strip() or None
    source = _meta_str(data.get('source'))
    model = _meta_str(data.get('model'))
    reasoning_effort = _meta_str(data.get('reasoning_effort'), limit=20)
    source_session_id = _meta_str(data.get('source_session_id'))
    if data.get('append_to') is not None:
        return jsonify({'error': '追加需求功能已移除，需求全部平级独立'}), 400
    if not title:
        return jsonify({'error': '标题不能为空'}), 400
    if status not in STATUSES:
        return jsonify({'error': f'非法状态: {status}'}), 400
    if mode not in (None, 'auto', 'semi'):
        return jsonify({'error': f'非法 mode: {mode}'}), 400
    if not project:
        return jsonify({'error': '项目名不能为空'}), 400
    return jsonify(req_add(title, project, notes, status, mode=mode, source=source, model=model,
                           reasoning_effort=reasoning_effort, source_session_id=source_session_id)), 201


@bp.route('/api/requirements/<int:req_id>', methods=['PATCH'])
def api_update(req_id):
    """更新需求。body 可选字段: title/project/notes/summary/status
    notes 为需求详情（用户填写），summary 为开发总结（调度器/agent 填写）
    status 取值: later(暂缓) open(待开发) doing(进行中) done(已完成) dropped(已废弃)；
    #419 过渡窗口 A：历史值 route/review 照存不转换（旧调度器兼容，由调度器启动回填统一转换）
    #419 流程快照字段：flow（显式传入须命中 flows.json）/flow_name/nodes_json/node_index
    #375：status 实际变化时写一条 req_status_log 审计行；变更来源经 X-Actor 头标注
    （dispatcher / manual:<操作名> / agent:<来源>），无头落 api 桶（detail 记 User-Agent 兜底）"""
    data = request.get_json(silent=True) or {}
    title = data.get('title')
    project = data.get('project')
    mode = data.get('mode')
    phase_raw = data.get('phase')
    # #376：phase 需支持「显式 null 清 phase」（需求级否决退 open）：缺键=不更新，显式 null=清空
    phase = phase_raw if 'phase' in data else FIELD_UNSET
    flow_raw = data.get('flow')
    flow = flow_raw if 'flow' in data else FIELD_UNSET
    flow_name = data.get('flow_name') if 'flow_name' in data else FIELD_UNSET
    nodes_raw = data.get('nodes_json')
    nodes_json = nodes_raw if 'nodes_json' in data else FIELD_UNSET
    node_index = data.get('node_index') if 'node_index' in data else FIELD_UNSET
    # #303 建卡来源元数据（透传可空字段，传 None 不更新；空串视为清除）
    source = _meta_str(data.get('source'))
    model = _meta_str(data.get('model'))
    reasoning_effort = _meta_str(data.get('reasoning_effort'), limit=20)
    source_session_id = _meta_str(data.get('source_session_id'))
    if title is not None and not title.strip():
        return jsonify({'error': '标题不能为空'}), 400
    if project is not None and not project.strip():
        return jsonify({'error': '项目名不能为空'}), 400
    # #419：mode/phase 为历史只读列，过渡期保留可写且不校验（窗口 A 旧调度器写入面宽容，
    # 两者均不驱动看板展示/流程推进）
    if flow is not FIELD_UNSET and flow is not None and flow_raw is not None and flow not in flow_ids():
        # #419：显式传入 flow 时校验命中配置（存量卡引用已删流程 id 不受影响）；None=清快照不校验
        return jsonify({'error': f'非法 flow: {flow}（不在 {sorted(flow_ids())}）'}), 400
    if nodes_json is not FIELD_UNSET and nodes_json is not None:
        err = _nodes_snapshot_error(nodes_json)
        if err:
            return jsonify({'error': err}), 400
    # #375：读旧 status（兼 404 校验）；写点比较 old/new，status 未变化的 PATCH（仅改 notes 等）不写行
    with resolve("get_db")() as conn:
        cur_row = conn.execute("SELECT project, status FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if cur_row is None:
        return jsonify({'error': '需求不存在'}), 404
    old_status = cur_row['status']
    if project is not None:
        # 已完成需求禁止变更所属项目
        target_status = data.get('status') or old_status
        if target_status == 'done' and project.strip() != cur_row['project']:
            return jsonify({'error': '已完成需求不可变更所属项目'}), 400
    item, err = resolve("req_update")(
        req_id,
        title=title.strip() if title is not None else None,
        project=project.strip() if project is not None else None,
        notes=data.get('notes'),
        summary=data.get('summary'),
        status=data.get('status'),
        mode=mode,
        phase=phase,
        flow=flow,
        flow_name=flow_name,
        nodes_json=nodes_json,
        node_index=node_index,
        source=source,
        model=model,
        reasoning_effort=reasoning_effort,
        source_session_id=source_session_id
    )
    if err:
        return jsonify({'error': err}), 400 if '不存在' not in err else 404
    if item['status'] != old_status:
        # #410：状态变更显性化 —— 审计行与调度回放 status 事件同一事务，
        # 单点覆盖 dispatcher/manual/agent/api 全部来源；前端渲染与 kind 无关，零改动
        _log_status_change(req_id, old_status, item['status'], *_status_log_meta())
    return jsonify(item)


@bp.route('/api/requirements/<int:req_id>', methods=['DELETE'])
def api_delete(req_id):
    """删除需求。doing(进行中)/done(已完成) 禁止删除；dropped(已废弃) 可删除"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT status FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        return jsonify({'error': '需求不存在'}), 404
    if row['status'] in ('doing', 'done'):
        return jsonify({'error': '进行中/已完成需求不可删除'}), 400
    req_delete(req_id)
    return jsonify({'success': True})


@bp.route('/api/roles', methods=['GET'])
def api_roles():
    """角色显示名/顺序（#404/#419）：由 flows.json roles.<id> 派生，按 order 升序；
    请求失败时前端静默降级为角色原文显示"""
    return jsonify(_role_injection())


@bp.route('/api/requirements/<int:req_id>/artifacts/<filename>', methods=['GET'])
def api_artifact_content(req_id, filename):
    """只读产物内容：.md/.svg 按文本返回，图片二进制返回。安全四件套：req_id 取 int + project 查 DB、
    filename 白名单正则、逐候选根 resolve() + 双 is_relative_to、扩展名白名单；任何一层不过即 404。
    #53 候选顺序：当前根 → legacy 根（只读兜底），每个根独立做路径校验，不放宽安全面。"""
    # ① 需求存在性 + project 从 DB 查（不信任任何前端传入参数）
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT project FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        return jsonify({'error': '需求不存在'}), 404
    # ② filename 白名单正则（禁 /、\、..；%2F 等编码在 werkzeug 解码后同样被此正则拦截）
    if not re.fullmatch(r'[A-Za-z0-9._-]+', filename):
        return jsonify({'error': '非法文件名'}), 404
    # ③ 扩展名白名单（filename 已过白名单，后缀与命中根无关）
    ext = Path(filename).suffix.lower()
    if ext not in resolve("ARTIFACT_TEXT_EXTS") and ext not in resolve("ARTIFACT_IMAGE_EXTS"):
        return jsonify({'error': '不支持的文件类型'}), 404
    # ④ 候选根解析：需求目录本身须在根内（拦 project 带 .. 的构造库），目标文件须在需求目录内
    #    （拦 filename=.. / 编码穿越/符号链接逃逸）；全部候选不合规或不存在 → 404
    _root, target, source = resolve_artifact(row['project'], req_id, filename)
    if source is None:
        return jsonify({'error': '产物不存在'}), 404
    if ext in resolve("ARTIFACT_IMAGE_EXTS"):
        return send_file(str(target), mimetype={
            '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.gif': 'image/gif', '.webp': 'image/webp'}[ext])
    return Response(target.read_text(errors='replace'), mimetype='text/plain; charset=utf-8')


@bp.route('/api/requirements/<int:req_id>/events', methods=['GET'])
def api_events_list(req_id):
    """查询某需求的执行过程事件。参数: after(事件 id，传上次拿到的最大 id 做增量拉取)"""
    try:
        after = int(request.args.get('after', '0'))
    except ValueError:
        after = 0
    session_id = (request.args.get('session') or '').strip()[:64] or None
    with resolve("get_db")() as conn:
        if session_id:
            # 历史无标记事件（session_id IS NULL）作为过渡统一归入各会话面板
            rows = conn.execute(
                "SELECT id, kind, content, ts, session_id FROM agent_events "
                "WHERE req_id = ? AND id > ? AND (session_id = ? OR session_id IS NULL) ORDER BY id LIMIT 500",
                (req_id, after, session_id)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, kind, content, ts, session_id FROM agent_events "
                "WHERE req_id = ? AND id > ? ORDER BY id LIMIT 500",
                (req_id, after)).fetchall()
    return jsonify([dict(r, ts_ms=_ts_ms(r['ts'])) for r in rows])


@bp.route('/api/requirements/<int:req_id>/events', methods=['POST'])
def api_events_add(req_id):
    """追加执行过程事件。body: {events: [{kind, content}]}，kind: message(消息文本)/tool(工具调用)/system(生命周期)"""
    data = request.get_json(silent=True) or {}
    events = data.get('events')
    if not isinstance(events, list) or not events:
        return jsonify({'error': 'events 不能为空'}), 400
    with resolve("get_db")() as conn:
        wrote = False
        for e in events[:50]:
            kind = str(e.get('kind') or '')[:20]
            content = str(e.get('content') or '')[:4000]
            sid = str(e.get('session_id') or '')[:64] or None
            if kind:
                conn.execute(
                    "INSERT INTO agent_events (req_id, kind, content, session_id, ts) VALUES (?, ?, ?, ?, ?)",
                    (req_id, kind, content, sid, resolve("now_str")()))
                wrote = True
        conn.commit()
    if wrote:
        resolve("_sse_publish")('proc', req_id=req_id)   # #416 发布点 1：提交后通知，前端收到即拉增量
    return jsonify({'success': True}), 201


@bp.route('/api/requirements/<int:req_id>/reviews', methods=['GET'])
def api_reviews_list(req_id):
    """查询某需求的评审记录，按轮次升序返回（含 notes_snapshot 供溯源）"""
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT id, req_id, round, reviewer, verdict, content, notes_snapshot, created_at "
            "FROM req_reviews WHERE req_id = ? ORDER BY round, id", (req_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/requirements/<int:req_id>/reviews', methods=['POST'])
def api_reviews_add(req_id):
    """追加一轮评审记录。body: {reviewer?, verdict?, content}（content 必填；
    verdict 为 pass/close/downgrade 等结构化结论，自由字符串透传）。
    round 由服务端按该需求现有最大轮次 +1 计算；notes_snapshot 由服务端快照当前 notes 落库，不由调用方传"""
    data = request.get_json(silent=True) or {}
    content = str(data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content 不能为空'}), 400
    row = _add_review(req_id, data.get('reviewer'), data.get('verdict'), content)
    if row is None:
        return jsonify({'error': '需求不存在'}), 404
    return jsonify(row), 201


@bp.route('/api/requirements/<int:req_id>/status-log', methods=['GET'])
def api_status_log(req_id):
    """查询某需求状态变更链（含上线 baseline 快照行），按 id 升序返回。
    actor=变更来源桶（dispatcher/manual/agent/api/baseline），detail 含 X-Actor 完整值与 User-Agent"""
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT id, req_id, old_status, new_status, actor, detail, ts "
            "FROM req_status_log WHERE req_id = ? ORDER BY id", (req_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/requirements/<int:req_id>/runs', methods=['POST'])
def api_runs_add(req_id):
    """写入一条 ACP 会话运行记录。body: {role, session_id, started_at, ended_at, status,
    tool_count, context_pct, context_tokens, max_tokens,
    cached_tokens?, input_tokens?, cache_state?, usage_raw?, manifest_path?, prompt_hash?, prefix_hash?}；
    缺失字段存 NULL，只增不删。
    #42 校验：cached/input 需非负整数或省略（负数/非整数 400）；cache_state 枚举
    hit/unknown/na 外 400；usage_raw 截 2048。na 的 token 由调用方传 NULL，接口不代填 0"""
    data = request.get_json(silent=True) or {}

    def _nonneg_int_or_400(key):
        v = data.get(key)
        if v is None:
            return None, None
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return None, (jsonify({'error': f'{key} 需为非负整数'}), 400)
        return v, None

    cached_tokens, err = _nonneg_int_or_400('cached_tokens')
    if err:
        return err
    input_tokens, err = _nonneg_int_or_400('input_tokens')
    if err:
        return err
    cache_state = data.get('cache_state')
    if cache_state is not None and cache_state not in ('hit', 'unknown', 'na'):
        return jsonify({'error': 'cache_state 需为 hit/unknown/na'}), 400
    usage_raw = data.get('usage_raw')
    usage_raw = str(usage_raw)[:2048] if usage_raw is not None else None

    def _short_text(key, limit):
        v = data.get(key)
        return str(v)[:limit] if v not in (None, '') else None
    # 上下文编译器执行档案（可选）：manifest 路径 + prompt/prefix hash
    manifest_path = _short_text('manifest_path', 1024)
    prompt_hash = _short_text('prompt_hash', 64)
    prefix_hash = _short_text('prefix_hash', 64)
    with resolve("get_db")() as conn:
        conn.execute(
            "INSERT INTO agent_runs (req_id, role, session_id, started_at, ended_at, status,"
            " tool_count, context_pct, context_tokens, max_tokens,"
            " cached_tokens, input_tokens, cache_state, usage_raw,"
            " manifest_path, prompt_hash, prefix_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (req_id,
             str(data.get('role') or '')[:20] or None,
             str(data.get('session_id') or '')[:64] or None,
             data.get('started_at'), data.get('ended_at'),
             str(data.get('status') or '')[:20] or None,
             data.get('tool_count'), data.get('context_pct'),
             data.get('context_tokens'), data.get('max_tokens'),
             cached_tokens, input_tokens, cache_state, usage_raw,
             manifest_path, prompt_hash, prefix_hash))
        conn.commit()
    return jsonify({'success': True}), 201


@bp.route('/api/requirements/<int:req_id>/dispatcher-events', methods=['GET'])
def api_dispatcher_events_list(req_id):
    """查询某需求的调度器动作时间线（按发生顺序全量返回）"""
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT id, kind, content, ts, session_id FROM dispatcher_events WHERE req_id = ? ORDER BY id",
            (req_id,)).fetchall()
    return jsonify([dict(r, ts_ms=_ts_ms(r["ts"])) for r in rows])


@bp.route('/api/requirements/<int:req_id>/dispatcher-events', methods=['POST'])
def api_dispatcher_events_add(req_id):
    """追加调度器动作。body: {events: [{kind, content, session_id?}]}，kind: spawn/phase/merge/retry/confirm/action 等。
    返回新记录 id（{success, id}，取最后一条插入的 lastrowid；调度器单条使用），供会话建立后回填 session_id"""
    data = request.get_json(silent=True) or {}
    events = data.get('events')
    if not isinstance(events, list) or not events:
        return jsonify({'error': 'events 不能为空'}), 400
    last_id = None
    with resolve("get_db")() as conn:
        for e in events[:50]:
            kind = str(e.get('kind') or 'action')[:20]
            content = str(e.get('content') or '').strip()[:2000]
            sid = str(e.get('session_id') or '').strip()[:64] or None
            if content:
                cur = conn.execute(
                    "INSERT INTO dispatcher_events (req_id, kind, content, session_id, ts) VALUES (?, ?, ?, ?, ?)",
                    (req_id, kind, content, sid, resolve("now_str")()))
                last_id = cur.lastrowid
        conn.commit()
    return jsonify({'success': True, 'id': last_id}), 201


@bp.route('/api/requirements/<int:req_id>/dispatcher-events/<int:event_id>', methods=['PATCH'])
def api_dispatcher_event_backfill(req_id, event_id):
    """回填调度器事件 session_id（会话建立后按记录 id 定位，幂等可重复调用）。
    body: {session_id}；校验事件归属该需求、session_id 非空并截断 [:64]"""
    sid = str((request.get_json(silent=True) or {}).get('session_id') or '').strip()[:64] or None
    if not sid:
        return jsonify({'error': 'session_id 不能为空'}), 400
    with resolve("get_db")() as conn:
        cur = conn.execute(
            "UPDATE dispatcher_events SET session_id = ? WHERE id = ? AND req_id = ?",
            (sid, event_id, req_id))
        if cur.rowcount == 0:
            return jsonify({'error': '事件不存在'}), 404
        conn.commit()
    return jsonify({'success': True})


@bp.route('/api/merge-conflicts', methods=['GET'])
def api_merge_conflicts_list():
    """活动 merge 冲突列表（前端按钮渲染信号 + 调度器对账轮询数据源）。
    存在记录=该需求处于 merge 冲突待处理；action 为空=等待用户操作，非空=用户已点击待调度器消费。"""
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT req_id, action, updated_at FROM merge_conflicts ORDER BY req_id").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/requirements/<int:req_id>/merge-conflict', methods=['POST'])
def api_merge_conflict_post(req_id):
    """merge 冲突决策通道写入：body {action: null|retry|takeover}（缺省 null）。
    - action 为空（调度器登记/重试失败重置）：upsert 写记录（存在=UPDATE，不存在=INSERT），按钮出现
    - action=retry/takeover（网页按钮写用户决策）：仅已有冲突记录时 UPDATE；无记录返回 404 拒绝
      （防幽灵点击：未登记冲突的需求不得使用本通道）"""
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    if action is not None and action not in ('retry', 'takeover'):
        return jsonify({'error': f'非法 action: {action}'}), 400
    with resolve("get_db")() as conn:
        if conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone() is None:
            return jsonify({'error': '需求不存在'}), 404
        cur = conn.execute("SELECT req_id FROM merge_conflicts WHERE req_id = ?", (req_id,)).fetchone()
        if cur is None:
            if action:
                return jsonify({'error': '需求不在 merge 冲突待处理状态'}), 404
            conn.execute("INSERT INTO merge_conflicts (req_id, action, updated_at) VALUES (?, ?, ?)",
                         (req_id, action, resolve("now_str")()))
        else:
            conn.execute("UPDATE merge_conflicts SET action = ?, updated_at = ? WHERE req_id = ?",
                         (action, resolve("now_str")(), req_id))
        conn.commit()
    return jsonify({'success': True, 'req_id': req_id, 'action': action})


@bp.route('/api/requirements/<int:req_id>/merge-conflict', methods=['DELETE'])
def api_merge_conflict_delete(req_id):
    """清除冲突记录（调度器消费成功/人工接管/幽灵决策清理后调用；幂等，无记录也返回 success）"""
    with resolve("get_db")() as conn:
        cur = conn.execute("DELETE FROM merge_conflicts WHERE req_id = ?", (req_id,))
        conn.commit()
    return jsonify({'success': True, 'deleted': cur.rowcount})


WEB_CONFIRM_ACTIONS = {
    "review_gate": ("pass", "rework", "reject"),
    "confirm_override": ("pass", "reject", "reject_later"),
    # 评审轮次超限的普通 confirm 与飞书 y/n 对齐；其它普通提问仍只开放显式停止出口。
    "confirm": ("pass", "rework", "proceed", "stop_later"),
}


def _confirm_pending(req_id):
    """按 req_id 查 pending 条目：返回 (kind, entry)；不存在返回 (None, None)（防幽灵点击）"""
    st = resolve("_read_pending_confirm_state")()
    for dict_key in ("pending_confirm", "pending_route", "pending_review"):
        entry = st.get(dict_key, {}).get(str(req_id))
        if entry is not None:
            if dict_key == "pending_confirm":
                return entry.get("kind") or "confirm", entry
            return dict_key.replace("pending_", ""), entry
    return None, None


@bp.route('/api/confirmations', methods=['GET'])
def api_confirmations():
    """#400 网页确认渲染数据源：读 dispatcher_state.json 三 pending 字典汇总（同 api_sessions
    模式，state 是事实源），联 confirm_decisions 表透出 action（有未消费行 = 已点击待调度器消费，
    跨 15s autoRefresh 全量重绘不丢态）。终态卡（done/dropped）行仍返回但带 terminal=True——
    展示面据此短路，调度器据此识别 action 走终态清账。
    输出字段：req_id/kind/question/options/role/phase/status/terminal/over_limit/action/reason。"""
    st = resolve("_read_pending_confirm_state")()
    with resolve("get_db")() as conn:
        decisions = {r["req_id"]: r for r in conn.execute("SELECT * FROM confirm_decisions").fetchall()}
        statuses = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM requirements").fetchall()}
    out = []
    for dict_key, fixed_kind in (("pending_confirm", None), ("pending_route", "route"),
                                 ("pending_review", "review")):
        for rid, raw in (st.get(dict_key) or {}).items():
            entry = raw if isinstance(raw, dict) else {}
            req_id = int(rid)
            # 真实落表（普通 need_confirm）entry 无 kind 字段：此处归一为 confirm，
            # 与 _confirm_pending 保持一致——前端确认按钮渲染（confirmFrameHtml/确认按钮排）
            # 及超限 stop_later 按钮均按 kind==='confirm' 判定（code_review 打回主因修复）
            kind = (entry.get("kind") or "confirm") if dict_key == "pending_confirm" else fixed_kind
            if dict_key == "pending_confirm":
                question = entry.get("question") or "（agent 未说明问题）"
                options = entry.get("options") or []
                role = entry.get("role")
                phase = entry.get("phase")
            elif dict_key == "pending_route":
                suggested = entry.get("suggested_mode") or "semi"
                question = f"analysis 建议走{'自动' if suggested == 'auto' else '半自动'}通道，确认？"
                options = []
                role = "analysis"
                phase = None
            else:
                phase = entry.get("phase")
                question = f"评审完成（{phase}），待人工确认" if phase else "评审完成，待人工确认"
                options = []
                role = phase  # #400：pending_review 的 phase = 被评审完成角色，直接匹配 agent_sessions.role
            dec = decisions.get(req_id)
            status = statuses.get(req_id)
            out.append({"req_id": req_id, "kind": kind, "question": question, "options": options,
                        "role": role, "phase": phase, "status": status,
                        # #25：终态标记（计算字段）——展示面据此短路；行仍保留，调度器据此
                        # 识别 action 并走终态清账，不被展示过滤误删
                        "terminal": status in ("done", "dropped"),
                        "over_limit": bool(entry.get("over_limit")),
                        "review_index": entry.get("review_index"),
                        "action": dec["action"] if dec else None,
                        "reason": dec["reason"] if dec else None})
    return jsonify(out)


@bp.route('/api/requirements/<int:req_id>/confirmations', methods=['POST'])
def api_requirement_confirm_post(req_id):
    """#400 网页确认动作决策写入：body {action}；#415 可选 {reason}（转开发反馈意见，截断 500 字）。
    仅 kind 白名单动作（其余 400）。
    防幽灵：需求存在且 state 有对应 pending 条目，否则 404；#25：需求已 done/dropped → 404
    （终态卡不接受动作，state 残留也不放行）；幂等：已有未消费 action → 409
    （防连点双发）；confirm 的 stop_later 仅超限态（over_limit）允许（非超限 400）。"""
    data = request.get_json(silent=True) or {}
    action = str(data.get('action') or '').strip()[:20]
    reason = str(data.get('reason') or '').strip()[:500]
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT id, status FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        return jsonify({'error': '需求不存在'}), 404
    # #25：终态卡（done/dropped）不再接受确认动作——state 残留也一律 404（防迟到点击
    # 触发 reject/stop_later 把终态卡改回 open/later）；终态清账由调度器单点完成
    if row["status"] in ("done", "dropped"):
        return jsonify({'error': '需求已终结，无待确认项'}), 404
    kind, entry = resolve("_confirm_pending")(req_id)
    if kind is None:
        return jsonify({'error': '需求不在待确认状态'}), 404
    if action not in resolve("WEB_CONFIRM_ACTIONS").get(kind, ()):
        return jsonify({'error': f'非法 action: {action}'}), 400
    if kind == "confirm" and action in ("pass", "rework") and entry.get("review_index") is None:
        return jsonify({'error': '普通提问不支持直接放行或打回，请通过角色会话答复'}), 400
    if kind == "confirm" and action == "proceed" and (
        entry.get("review_index") is not None or entry.get("flow_id")
    ):
        return jsonify({'error': '当前确认项不支持 proceed'}), 400
    if kind == "confirm" and action == "stop_later" and not entry.get("over_limit"):
        return jsonify({'error': '当前不在超限状态，不支持停止追问'}), 400
    with resolve("get_db")() as conn:
        if conn.execute("SELECT req_id FROM confirm_decisions WHERE req_id = ?", (req_id,)).fetchone():
            return jsonify({'error': '已提交待处理'}), 409
        conn.execute("INSERT INTO confirm_decisions (req_id, kind, action, reason, actor, created_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (req_id, kind, action, reason, _header_str(request.headers.get('X-Actor'))[:200],
                      resolve("now_str")(), resolve("now_str")()))
        conn.commit()
    return jsonify({'success': True, 'req_id': req_id, 'action': action, 'reason': reason}), 202


@bp.route('/api/requirements/<int:req_id>/confirmations', methods=['DELETE'])
def api_requirement_confirm_delete(req_id):
    """清除确认决策记录（调度器 reconcile 消费成功后调用；幂等，无行也 200；不开放前端调用）"""
    with resolve("get_db")() as conn:
        cur = conn.execute("DELETE FROM confirm_decisions WHERE req_id = ?", (req_id,))
        conn.commit()
    return jsonify({'success': True, 'deleted': cur.rowcount})
