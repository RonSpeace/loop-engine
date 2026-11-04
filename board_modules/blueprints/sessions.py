"""sessions 蓝图：只读会话列表、需求会话与手动命令。

#32 Phase 6（卡 #39）由 board.py 原样迁入：@app.route 改为本蓝图 bp.route，留驻 board.py 的名字经 board_modules.runtime.resolve 晚绑定。
"""


from board_modules.runtime import resolve


import json


from flask import Blueprint, jsonify, request


from acp_shared import pre_role, AGENT_RUNTIMES
from board_modules.common import _origin_bucket
from board_modules.common import _snapshot_node
from board_modules.common import _status_log_meta
from board_modules.services.session_runtime import _AGENT_SESSION_FIELDS
from board_modules.services.session_runtime import _agent_sessions_with_meta
from board_modules.services.session_runtime import resolve_session_dir


bp = Blueprint("sessions", __name__)


@bp.route('/api/sessions', methods=['GET'])
def api_sessions():
    """各需求的 ACP 会话信息（按需求 id 键）：id/model/thinking/context/branch/harness。
    由调度器在会话建立和收束时写入 dispatcher_state.json；卡片底部的会话信息栏据此渲染。
    兼容旧的纯 session_id 字符串值"""
    try:
        sessions = json.loads((resolve("DATA_DIR") / "dispatcher_state.json").read_text()).get("sessions", {})
    except Exception:
        return jsonify({})
    return jsonify({k: ({"id": v} if isinstance(v, str) else v)
                    for k, v in sessions.items() if v})


@bp.route('/api/agent-sessions', methods=['GET'])
def api_agent_sessions_list():
    """全部调度器 ACP 会话注册表记录（前端中间栏按 req_id 分组渲染）。
    #422：每行附计算字段 session_dir（该会话记录目录绝对路径，解析不到为 null，不落库）。
    #421：每行另附 req_status/server_now（见 _agent_sessions_with_meta）。"""
    with resolve("get_db")() as conn:
        rows = _agent_sessions_with_meta(conn)
    out = []
    for d in rows:
        harness = d.get('harness') or ''
        if harness in AGENT_RUNTIMES and harness != 'kimi':
            d['session_dir'] = resolve_session_dir(d['session_id'], runtime=harness)
        else:
            d['session_dir'] = resolve_session_dir(d['session_id'])
        out.append(d)
    return jsonify(out)


@bp.route('/api/agent-sessions', methods=['POST'])
def api_agent_sessions_upsert():
    """调度器写入/更新 ACP 会话注册表。body 两种形态：
    - {id, ...}：按行 id 更新；
    - {req_id, role, session_id, ...}：按 (req_id, role, session_id) 匹配更新，无则插入。
    返回 {id}。session_id 为空的行（待接管窗口）由看板新端点创建，本接口不创建。
    #431：插入时按请求头写来源两列（origin 经 _origin_bucket 收敛）；更新路径不改 origin，
    两列不在 _AGENT_SESSION_FIELDS 白名单内（外部 body 不能写/篡改来源）。"""
    data = request.get_json(silent=True) or {}

    def clean(k, v):
        if k == 'poisoned':
            return 1 if v else 0
        if k.startswith('last_') and k.endswith('_at'):
            # #421 心跳时间戳：epoch 秒；非法/空 → None（不 500）
            try:
                return None if v is None or v == '' else float(v)
            except (TypeError, ValueError):
                return None
        if k == 'turn_step':
            return None if v is None else str(v)[:20]          # '' = 无在途（清态语义，保留空串）
        if k == 'turn_step_detail':
            return None if v is None else str(v)[:48]          # 工具标题摘要
        return None if v is None else str(v)[:255]

    upd = {k: clean(k, v) for k, v in data.items() if k in _AGENT_SESSION_FIELDS}
    with resolve("get_db")() as conn:
        row_id = data.get('id')
        if row_id is not None:
            if not upd:
                return jsonify({'error': '无更新字段'}), 400
            sets = ', '.join(f'{k} = ?' for k in upd)
            conn.execute(f"UPDATE agent_sessions SET {sets}, updated_at = ? WHERE id = ?",
                         (*upd.values(), resolve("now_str")(), int(row_id)))
            conn.commit()
            resolve("_sse_publish")('sessions')   # #416 发布点 4a
            return jsonify({'id': int(row_id)})
        req_id = data.get('req_id')
        role = str(data.get('role') or '')[:20]
        if req_id is None or not role:
            return jsonify({'error': 'req_id/role 必填'}), 400
        session_id = str(data.get('session_id') or '')[:64] or None
        row = None
        if session_id:
            row = conn.execute(
                "SELECT id FROM agent_sessions WHERE req_id = ? AND role = ? AND session_id = ?",
                (req_id, role, session_id)).fetchone()
        if row:
            if upd:
                sets = ', '.join(f'{k} = ?' for k in upd)
                conn.execute(f"UPDATE agent_sessions SET {sets}, updated_at = ? WHERE id = ?",
                             (*upd.values(), resolve("now_str")(), row['id']))
                conn.commit()
                resolve("_sse_publish")('sessions')   # #416 发布点 4b
            return jsonify({'id': row['id']})
        if not session_id:
            return jsonify({'error': 'session_id 必填（待接管行由看板首条消息端点创建）'}), 400
        actor, detail = _status_log_meta()   # #431 来源：请求头推导，body 不能写
        cur = conn.execute(
            "INSERT INTO agent_sessions (req_id, role, session_id, model, thinking, harness,"
            " context_pct, branch, workdir, poisoned, status, turn_step, turn_step_detail,"
            " last_llm_request_at, last_llm_response_at, last_thought_at, last_tool_started_at,"
            " model_requested, model_source, model_configured, model_reason,"
            " thinking_requested, thinking_configured, thinking_reason,"
            " origin, origin_detail, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (req_id, role, session_id, upd.get('model'), upd.get('thinking'), upd.get('harness'),
             upd.get('context_pct'), upd.get('branch'), upd.get('workdir'),
             upd.get('poisoned', 0), upd.get('status', 'idle'),
             # #421：新建行时同步落心跳字段（与更新路径白名单对称，防静默丢弃）
             upd.get('turn_step'), upd.get('turn_step_detail'), upd.get('last_llm_request_at'),
             upd.get('last_llm_response_at'), upd.get('last_thought_at'), upd.get('last_tool_started_at'),
             # #11：模型请求值/来源（与更新路径白名单对称）
             upd.get('model_requested'), upd.get('model_source'),
             # #13：兜底可见两列（配置值 / 原因摘要）
             upd.get('model_configured'), upd.get('model_reason'),
             # #29：思考档位三列（与更新路径白名单对称，漏加=静默丢弃）
             upd.get('thinking_requested'), upd.get('thinking_configured'), upd.get('thinking_reason'),
             _origin_bucket(actor), detail,
             resolve("now_str")(), resolve("now_str")()))
        conn.commit()
        resolve("_sse_publish")('sessions')   # #416 发布点 4c（新会话插入）
        return jsonify({'id': cur.lastrowid}), 201


@bp.route('/api/requirements/<int:req_id>/sessions', methods=['GET'])
def api_req_sessions(req_id):
    """某需求的 ACP 会话注册表记录（含毒会话/空会话），按创建顺序"""
    with resolve("get_db")() as conn:
        return jsonify(_agent_sessions_with_meta(conn, "WHERE s.req_id = ?", (req_id,)))


@bp.route('/api/requirements/<int:req_id>/sessions', methods=['POST'])
def api_req_session_new(req_id):
    """#431 已下线（410）：空会话不再预建——「新建聊天」是前端草稿态，发送首条消息才落库。
    保留路由（不删）以便部署瞬间未刷新的旧页面得到明确提示而非 404。"""
    return jsonify({'error': '该端点已下线（#431），请用 POST /api/requirements/<id>/sessions/messages 发送首条消息'}), 410


def _first_message_allowed_role(req):
    """#431 首条消息/删除的允许角色口径（沿用 #419 旧端点）：卡上快照当前节点 role；
    未入流程/无快照 → 前置槽位 pre.role。terminal 节点返回 None（调用方判 400）。"""
    node = _snapshot_node(req)
    if node is not None and node.get('node_type') == 'terminal':
        return None
    return (node or {}).get('role') or pre_role()


@bp.route('/api/requirements/<int:req_id>/sessions/messages', methods=['POST'])
def api_req_session_first_message(req_id):
    """#431 人工首条消息：单事务落注册行（session_id 空）+ 入队 pending 人工命令，返回新行 id。
    这是「新建聊天」唯一的落库点（旧 POST /sessions 已 410）。校验顺序（任一失败零写入）：
    需求不存在 404 → text 空 400 → 需求 done 400 → 当前节点 terminal 400 → role 不符 400。"""
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT * FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return jsonify({'error': '需求不存在'}), 404
    if not text:
        return jsonify({'error': '消息不能为空'}), 400
    if req['status'] == 'done':
        return jsonify({'error': '已完成需求不可继续会话'}), 400
    allowed = resolve("_first_message_allowed_role")(req)
    if allowed is None:
        return jsonify({'error': '当前节点为收尾节点，无可继续的 agent 会话'}), 400
    role = str(data.get('role') or '')[:20]
    if role and role != allowed:
        return jsonify({'error': f'当前节点只允许新建 {allowed} 会话'}), 400
    actor, detail = _status_log_meta()
    with resolve("get_db")() as conn:
        cur = conn.execute(
            "INSERT INTO agent_sessions (req_id, role, status, origin, origin_detail,"
            " created_at, updated_at) VALUES (?, ?, 'created', ?, ?, ?, ?)",
            (req_id, allowed, _origin_bucket(actor), detail, resolve("now_str")(), resolve("now_str")()))
        row_id = cur.lastrowid
        cur2 = conn.execute(
            "INSERT INTO manual_commands (req_id, agent_session_id, role, text, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (req_id, row_id, allowed, text, resolve("now_str")(), resolve("now_str")()))
        conn.commit()
    resolve("_sse_publish")('sessions')   # #416 发布点 7（首条消息落行；命令入队不发布）
    return jsonify({'id': row_id, 'command_id': cur2.lastrowid}), 201


@bp.route('/api/requirements/<int:req_id>/sessions/<int:asid>', methods=['DELETE'])
def api_req_session_delete(req_id, asid):
    """#431 空行兜底出口（无 UI 入口）：只删「session_id 为空 + 无未消费人工命令 + 非 running」的行。
    拒绝矩阵 fail-closed：跨需求/不存在 404；真实会话（session_id 非空）409；
    同会话行存在 pending/running 人工命令 409；行 status=running 409。
    只删 agent_sessions 行（manual_commands 历史 done/failed 保留留档）。"""
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT id FROM requirements WHERE id = ?", (req_id,)).fetchone()
        sess = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ? AND req_id = ?", (asid, req_id)).fetchone()
        if req is None or sess is None:
            return jsonify({'error': '需求或会话不存在'}), 404
        if sess['session_id']:
            return jsonify({'error': '真实会话不可删除'}), 409
        busy = conn.execute(
            "SELECT COUNT(*) AS n FROM manual_commands WHERE agent_session_id = ?"
            " AND status IN ('pending', 'running')", (asid,)).fetchone()['n']
        if busy:
            return jsonify({'error': '该空会话有未消费的人工命令，不可删除'}), 409
        if sess['status'] == 'running':
            return jsonify({'error': '该会话正在运行中'}), 409
        conn.execute("DELETE FROM agent_sessions WHERE id = ?", (asid,))
        conn.commit()
    resolve("_sse_publish")('sessions')
    return jsonify({'deleted': 1})


@bp.route('/api/requirements/<int:req_id>/sessions/<int:asid>/messages', methods=['POST'])
def api_req_session_message(req_id, asid):
    """人工继续会话：发一条消息入队（pending），调度器消费后 resume/新建会话并跑一轮。
    空会话（created，session_id 为空）首条消息会触发新建真实会话。"""
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()
    if not text:
        return jsonify({'error': '消息不能为空'}), 400
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT * FROM requirements WHERE id = ?", (req_id,)).fetchone()
        sess = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ? AND req_id = ?", (asid, req_id)).fetchone()
    if req is None or sess is None:
        return jsonify({'error': '需求或会话不存在'}), 404
    if req['status'] == 'done':
        return jsonify({'error': '已完成需求不可继续会话'}), 400
    if sess['status'] == 'running':
        return jsonify({'error': '该会话正在运行中'}), 409
    with resolve("get_db")() as conn:
        cur = conn.execute(
            "INSERT INTO manual_commands (req_id, agent_session_id, role, text, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (req_id, asid, sess['role'], text, resolve("now_str")(), resolve("now_str")()))
        conn.commit()
    return jsonify({'id': cur.lastrowid, 'status': 'pending'}), 202


@bp.route('/api/manual-commands', methods=['GET'])
def api_manual_commands_list():
    """调度器轮询消费：取指定状态（默认 pending）的命令，按入队顺序"""
    status = (request.args.get('status') or 'pending').strip()[:20]
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_commands WHERE status = ? ORDER BY id LIMIT 20", (status,)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/manual-commands/<int:cmd_id>', methods=['PATCH'])
def api_manual_commands_update(cmd_id):
    """调度器更新命令状态：pending -> running -> done/failed（error 可选）"""
    data = request.get_json(silent=True) or {}
    status = str(data.get('status') or '')[:20]
    error = str(data.get('error') or '')[:2000] or None
    with resolve("get_db")() as conn:
        conn.execute("UPDATE manual_commands SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                     (status, error, resolve("now_str")(), cmd_id))
        conn.commit()
    return jsonify({'success': True})
