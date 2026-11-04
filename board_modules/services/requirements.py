"""#32 Phase 3：requirements 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from board_modules.common import _meta_str
from board_modules.common import _status_label_cn
import json

from board_modules.runtime import resolve

# 状态机（#419 收缩为 5 值）：later(暂缓) -> open(待开发) -> doing(进行中) -> done(已完成)；dropped(已废弃)
# later 与 open 的区别：调度器只捞 open，later 用于"记下来但暂不让自动开发碰"
# dropped(已废弃) = 终态：方案被否决/走不下去（如开发完成但方向废弃），调度器不捞，可删除
STATUSES = ('later', 'open', 'doing', 'done', 'dropped')
# route(通道待确认)/review(评审中) 为历史状态（LEGACY）：新流程不再产生，仅 req_status_log 历史行回放
# 与过渡窗口 A（旧调度器仍写这两值）照存不转换，由调度器启动回填统一转换；看板无人工入口
LEGACY_STATUSES = ('route', 'review')
# status 写入白名单 = 5 值 + 过渡期照存的历史值（照存不改写、不触发任何副作用）
WRITABLE_STATUSES = STATUSES + LEGACY_STATUSES
# 未显式传入字段的哨兵（phase 需支持「显式 null 清 phase」），与 JSON 缺键区分
FIELD_UNSET = object()

# ==================== 数据库 ====================


# ==================== 数据操作 ====================

# #421：事件时间戳统一按存储时区（REQ_BOARD_TZ）解析成 epoch 毫秒下发，前端用「服务端基准 − ts_ms」
# 算事件 age——不依赖浏览器解析本地字符串，浏览器时区/时钟漂移不影响判定与显示。




def req_list(project=None, status=None):
    """查询需求列表，可按项目/状态过滤，按创建时间倒序"""
    # req_chat_sid：该需求最新需求助手会话的 session_id（中间栏「需求助手」置顶行展示用，#326）
    sql = """SELECT r.*, (SELECT COUNT(*) FROM dispatcher_events e WHERE e.req_id = r.id) AS dispatcher_count,
             (SELECT c.session_id FROM req_chats c WHERE c.req_id = r.id ORDER BY c.id DESC LIMIT 1) AS req_chat_sid
             FROM requirements r"""
    conds, params = [], []
    if project:
        conds.append("r.project = ?")
        params.append(project)
    if status:
        codes = [s for s in status.split(',') if s in STATUSES]
        if codes:
            conds.append(f"r.status IN ({','.join('?' * len(codes))})")
            params.extend(codes)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY r.created_at DESC, r.id DESC"
    with resolve("get_db")() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def req_add(title, project, notes="", status="open", mode=None,
            source=None, model=None, reasoning_effort=None, source_session_id=None):
    """新建需求卡。status='done' 时同步写 completed_at（与 req_update 进 done 记完成时间语义对齐）：
    支持「已实现需求建卡即 done 留档」——终态卡不会被调度器捞（仅 open 卡自动开发）。"""
    with resolve("get_db")() as conn:
        cur = conn.execute(
            "INSERT INTO requirements (title, project, notes, status, mode, source, model, reasoning_effort, "
            "source_session_id, completed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, project, notes, status, mode, source, model, reasoning_effort, source_session_id,
             (resolve("now_str")() if status == "done" else None), resolve("now_str")(), resolve("now_str")())
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM requirements WHERE id = ?", (cur.lastrowid,)).fetchone())

def req_update(req_id, title=None, project=None, notes=None, summary=None, status=None, mode=None,
               phase=FIELD_UNSET, flow=FIELD_UNSET, flow_name=FIELD_UNSET, nodes_json=FIELD_UNSET,
               node_index=FIELD_UNSET, source=None, model=None, reasoning_effort=None,
               source_session_id=None):
    """按传入字段更新；进入 done 记 completed_at，进入 doing 记 started_at，离开各自状态则清空。
    mode/phase/flow 为内部流转字段（通道确认/阶段推进）；route/review 状态不触发 started_at 变更。
    phase/flow 用 FIELD_UNSET 哨兵区分「未传=不更新」与「显式 None=清空」（#376 需求级否决清 phase）。"""
    fields, params = [], []
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if project is not None:
        fields.append("project = ?")
        params.append(project)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    if summary is not None:
        fields.append("summary = ?")
        params.append(summary)
    if mode is not None:
        fields.append("mode = ?")
        params.append(mode)
    if phase is not FIELD_UNSET:
        fields.append("phase = ?")
        params.append(phase)
    if flow is not FIELD_UNSET:
        fields.append("flow = ?")
        params.append(flow)
    # #419 流程快照三列（调度器写入；flow_name 仅展示，nodes_json/node_index 推进依据）
    if flow_name is not FIELD_UNSET:
        fields.append("flow_name = ?")
        params.append(flow_name)
    if nodes_json is not FIELD_UNSET:
        fields.append("nodes_json = ?")
        params.append(nodes_json)
    if node_index is not FIELD_UNSET:
        fields.append("node_index = ?")
        params.append(node_index)
    # #303 建卡来源元数据（与 mode/phase 同款：传 None 不更新，可空透传字段）
    if source is not None:
        fields.append("source = ?")
        params.append(source)
    if model is not None:
        fields.append("model = ?")
        params.append(model)
    if reasoning_effort is not None:
        fields.append("reasoning_effort = ?")
        params.append(reasoning_effort)
    if source_session_id is not None:
        fields.append("source_session_id = ?")
        params.append(source_session_id)
    if status is not None:
        # #419 过渡窗口 A：route/review 照存不转换（旧调度器兼容），待调度器启动回填统一转换；
        # 其他未知值仍拒绝（5 值白名单）
        if status not in WRITABLE_STATUSES:
            return None, f"非法状态: {status}"
        fields.append("status = ?")
        params.append(status)
        if status == 'done':
            fields.append("completed_at = ?")
            params.append(resolve("now_str")())
        else:
            fields.append("completed_at = NULL")
        if status == 'doing':
            fields.append("started_at = ?")
            params.append(resolve("now_str")())
        elif status in ('later', 'open'):
            fields.append("started_at = NULL")
    if not fields:
        return None, "没有要更新的字段"
    fields.append("updated_at = ?")
    params.append(resolve("now_str")())
    params.append(req_id)
    with resolve("get_db")() as conn:
        cur = conn.execute(f"UPDATE requirements SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        if cur.rowcount == 0:
            return None, "需求不存在"
        return dict(conn.execute("SELECT * FROM requirements WHERE id = ?", (req_id,)).fetchone()), None

def req_delete(req_id):
    with resolve("get_db")() as conn:
        cur = conn.execute("DELETE FROM requirements WHERE id = ?", (req_id,))
        # #386：需求删除时清 merge 冲突决策记录（孤儿记录由对账兜底，此处主动清理）
        conn.execute("DELETE FROM merge_conflicts WHERE req_id = ?", (req_id,))
        conn.commit()
        return cur.rowcount > 0

def project_list():
    """已有项目名（用于输入自动补全），并集 projects.json 中接入自动开发的项目"""
    with resolve("get_db")() as conn:
        names = {r['project'] for r in conn.execute(
            "SELECT DISTINCT project FROM requirements"
        ).fetchall()}
    try:
        names |= set(json.loads(resolve("PROJECTS_FILE").read_text()).keys())
    except Exception:
        pass
    return sorted(names)



# #410 状态中文名（回放 status 事件 content 用）：与前端 STATUS_LABEL（GET /api/meta 注入）对齐；
# #419 起可写状态 5 值，route/review 为 LEGACY（仅历史 req_status_log 行回放 + 过渡窗口照存值，不产生新值）
# #431 注册行来源桶（封闭枚举）：manual=看板人工首条消息 / agent=AI 代理 / api=无头调用与未知桶
# / dispatcher=调度器自建。与 req_status_log 的开放 actor 桶刻意分开——_status_log_meta() 本体不动，
# 只有注册行来源经 _origin_bucket() 收敛，避免落库出现无法枚举的自由值。




# #410 状态中文名（回放 status 事件 content 用）：与前端 STATUS_LABEL（GET /api/meta 注入）对齐；
# #419 起可写状态 5 值，route/review 为 LEGACY（仅历史 req_status_log 行回放 + 过渡窗口照存值，不产生新值）








def _log_status_change(req_id, old_status, new_status, actor, detail):
    """#375/#410 状态变更留痕：audit 行（req_status_log）+ 调度回放 status 事件，同一事务。
    PATCH 端点（actor/detail 由 X-Actor 头解析）与需求助手 MCP 工具（actor='agent'、detail 说明
    来源为 agent:reqchat，MCP 线程无 Flask 请求上下文，不能走 _status_log_meta）共用本实现。"""
    with resolve("get_db")() as conn:
        # ts 显式 now_str()（本地时间）：与同事务的 dispatcher_events 行及卡面时间一致；
        # 历史上走列默认 CURRENT_TIMESTAMP（UTC），致同一事务两行差 8 小时（#410 只修了事件行）
        conn.execute(
            "INSERT INTO req_status_log (req_id, old_status, new_status, actor, detail, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (req_id, old_status, new_status, actor, detail, resolve("now_str")()))
        conn.execute(
            "INSERT INTO dispatcher_events (req_id, kind, content, ts) VALUES (?, 'status', ?, ?)",
            (req_id, f"状态变更：{_status_label_cn(old_status)} → {_status_label_cn(new_status)}",
             resolve("now_str")()))
        conn.commit()

def _add_review(req_id, reviewer, verdict, content):
    """#342 追加一轮评审记录（append-only）：round 服务端按现有最大轮次 +1、notes_snapshot 服务端
    快照当前 notes 落库，均不由调用方传；需求不存在返回 None。
    HTTP 端点（POST /api/requirements/<id>/reviews）与需求助手 MCP 工具（#434 post_review）共用本实现，
    避免两条写回路径口径漂移。本表内容永不进 prompt（见文件头隔离硬约束）。"""
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT notes FROM requirements WHERE id = ?", (req_id,)).fetchone()
        if req is None:
            return None
        next_round = conn.execute(
            "SELECT COALESCE(MAX(round), 0) + 1 FROM req_reviews WHERE req_id = ?", (req_id,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO req_reviews (req_id, round, reviewer, verdict, content, notes_snapshot, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (req_id, next_round, _meta_str(reviewer, 50), _meta_str(verdict, 50), content,
             req['notes'], resolve("now_str")()))
        conn.commit()
        row = conn.execute("SELECT * FROM req_reviews WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)
