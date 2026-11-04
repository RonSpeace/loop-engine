"""#32 Phase 3：session_runtime 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import role_meta
from board_modules.common import _now_ts
import os
import re
import time

from board_modules.runtime import resolve
# 角色 -> 主产物文件名代码默认（与 dispatcher.py 产物约定一致；仅展示约定主产物，不列出阶段目录全部文件）
# #419：真源 = flows.json 的 roles.<role>.artifact（见 _artifact_files()），本表仅作配置缺该字段时的兜底
# （dev 在配置里 artifact=null，展示约定仍为 self-test.md）；tech_review/code_review=#376 agent 评审产物。
# review 已不是角色（代码审查合并为 code_review），此处**仅作历史产物展示兜底**：配置里查不到该角色、
# 回退本表的这一条，让 auto 通道闸门时代落盘的 merge_review.md 仍出现在 /api/artifacts 产物面板里
# （角色标签退化为原文 id，属预期）。
_ARTIFACT_FILES_DEFAULT = {"analysis": "analysis.md", "proposal": "prd.md",
                           "tech": "tech.md", "dev": "self-test.md", "accept": "accept.md",
                           "review": "merge_review.md",
                           "tech_review": "tech_review.md", "code_review": "code_review.md"}


def _artifact_files():
    """角色 -> 主产物文件名（#419 配置派生）：roles.<role>.artifact，缺/空回退代码默认；
    非角色条目 diff 固定保留——diff.patch 是 #381 审查闸门期间调度器 _gen_review_diff 代写的
    dev 分支相对基线 diff，/api/artifacts 与前端 diff 视图依赖，不属任何角色产物。"""
    out = {}
    for role, default_fn in _ARTIFACT_FILES_DEFAULT.items():
        fn = role_meta(role, "artifact") or default_fn
        if fn:
            out[role] = fn
    for role in resolve("ROLE_CAPABILITIES"):      # 配置新增角色（带 artifact）自动进产物聚合
        fn = role_meta(role, "artifact")
        if fn and role not in out:
            out[role] = fn
    out["diff"] = "diff.patch"
    return out


def _role_injection():
    """角色显示名/顺序注入数据（#404/#419）：单一源 = flows.json roles.<id> 的 display/order
    （#419 从 acp_shared.ROLE_CAPABILITIES 迁出，键集仍以能力表为准），前端会话信息栏角色标签/排序
    由 GET /api/roles 动态注入（不再手写映射表）。缺 display 回退原文、缺 order 排尾部（fail-safe 降级）；
    字段完备性由 tests/test_flow_config.py 与 tests/test_role_manifest.py 锁定，漏填由测试红而非运行期故障。"""
    items = []
    for role in resolve("ROLE_CAPABILITIES"):
        display = role_meta(role, "display") or role
        order = role_meta(role, "order")
        items.append({"role": role, "display": display,
                      "order": order if isinstance(order, int) else 999})
    return sorted(items, key=lambda r: r["order"])

# ==================== 调度器 ACP 会话注册表（#273 人工接管） ====================
# agent_sessions：每条 kimi 会话一行（含待接管行/毒会话），中间栏会话列表与人工继续/新建的数据源。
# 调度器经 POST /api/agent-sessions 写入；看板经 GET 读取，人工经 messages 接口入队命令。
# #431：session_id 为空的行（「新建聊天」首条消息落行、调度器未接管）不进中间栏渲染；
# 空行只由 POST /api/requirements/<id>/sessions/messages 创建，常规删除走看板 DELETE 端点。

_AGENT_SESSION_FIELDS = ('session_id', 'model', 'thinking', 'harness', 'context_pct',
                         'branch', 'workdir', 'poisoned', 'status',
                         # #421 活性心跳（漏加白名单 = 静默丢弃）
                         'turn_step', 'turn_step_detail', 'last_llm_request_at',
                         'last_llm_response_at', 'last_thought_at', 'last_tool_started_at',
                         # #11 模型留痕（请求值/来源；实际值复用 model）
                         'model_requested', 'model_source',
                         'model_configured', 'model_reason',
                         # #29 思考档位留痕（选档后的请求值/配置值/原因摘要）
                         'thinking_requested', 'thinking_configured', 'thinking_reason')


def _agent_sessions_with_meta(conn, where="", args=()):
    """#421：注册表行附 req_status（LEFT JOIN requirements，done 判定权威源，收口 15s 轮询滞后窗口）
    与 server_now（服务端时间基准，同一响应内同值——前端据此抵消浏览器时钟漂移/时区差异）。"""
    now = _now_ts()
    rows = conn.execute(
        "SELECT s.*, r.status AS req_status FROM agent_sessions s "
        "LEFT JOIN requirements r ON r.id = s.req_id " + where + " ORDER BY s.id", args).fetchall()
    return [dict(r, server_now=now) for r in rows]
_SESSION_DIR_CACHE = {}             # sid -> (path|None, ts)


def _session_dir_names(sid):
    """目录名候选：注册表现网存 session_<uuid>，兼容裸 <uuid> 存量值；非法 sid 一律不匹配
    （含 / 或 \\ 或 .. 的形态直接拒绝，无路径穿越面）。"""
    s = str(sid or '')
    if not s or len(s) > 200 or not re.fullmatch(r'[A-Za-z0-9._-]+', s) or '..' in s:
        return []
    return [s] if s.startswith('session_') else ['session_' + s, s]


def resolve_session_dir(sid, runtime="kimi"):
    """反查 sid 的会话记录目录（绝对路径、无尾斜杠）；解析不到返回 None。
    命中值每次 isdir 复验（目录被清理即时回「未找到」）；未命中走 60s 负缓存，
    稳态下每行只 1 次 stat、零目录扫描。只对注册表内已有的 sid 解析，异常吞掉返回 None。"""
    from acp_shared import agent_runtime, runtime_skip
    try:
        resolver = agent_runtime(runtime)["session_dir_resolver"]
    except ValueError:
        return None
    if resolver is None:
        runtime_skip(runtime, "会话记录目录解析")
        return None
    names = _session_dir_names(sid)
    if not names:
        return None
    cached = _SESSION_DIR_CACHE.get(names[0])
    if cached is not None:
        path, ts = cached
        if path:
            if os.path.isdir(path):
                return path
        elif time.time() - ts < resolve("_SESSION_DIR_MISS_TTL"):
            return None
    found = None
    for home in resolve("session_homes")():
        sessions = home / 'sessions'
        try:
            if not sessions.is_dir():
                continue
            parents = [sessions] + [p for p in sessions.iterdir() if p.is_dir()]
        except OSError:
            continue
        for name in names:
            for parent in parents:
                cand = parent / name
                if cand.is_dir():
                    found = str(cand)
                    break
            if found:
                break
        if found:
            break
    _SESSION_DIR_CACHE[names[0]] = (found, time.time())
    return found
