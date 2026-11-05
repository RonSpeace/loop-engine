#!/usr/bin/env python3
"""req-board 自动开发调度器（#419：研发流程可配置化，按卡上流程快照推进）

轮询看板上 PROJECTS 中各项目的 open 需求（需静置 MIN_AGE_MINUTES 分钟未被编辑），
按卡上快照（flow/nodes_json/node_index）或前置槽位（无快照）路由到角色拉起 kimi ACP 会话：
- 前置槽位（pre.role，默认 analysis，只读）：产出 suggested_flow（推荐流程）+ repos + 证据清单
- 流程节点（flows.json 配置：artifact/code/review/terminal）：按 node_type 分派推进与收尾
- 评审节点（review）：verdict pass → 下一节点；reject → 自动打回前驱复修（>3 轮转人工）
- 合并冲突由 merge_resolve 角色处理；role_allowed 守卫防 agent 自行拉起其它角色会话
人工介入只剩 need_confirm 一条通道（飞书 y/n 即时处理 + 看板按钮）：
- agent 以 `kimi acp` 子进程运行，调度器作为 ACP 客户端驱动会话，
  session/update 流式事件（消息文本 + 工具调用摘要）上报看板，页面"执行过程"面板展示
- agent 通过结果文件回报结果（done/blocked/need_confirm，前置环节另带 suggested_flow/repos）
- done 需有实际代码变更（code 节点，分支相对基线 diff）或产物文件（artifact/review 节点）才被采信；进程级失败
  （崩溃/超时/未提交结果/非法 JSON）自动重试最多 2 次后转 later 停止自动调度（#113：不再退回 open
  被自动重捞），等待人工改回 open 恢复；blocked（内容性阻塞结论，#385）不重试，直接转 later 待人工处理
- need_confirm（人工唯一介入通道）通过飞书机器人与看板按钮双向沟通，答复后自动继续

运行：.venv/bin/python dispatcher.py（飞书凭据来自仓库根 feishu.json；REQ_BOARD_NO_FEISHU=1 或未安装 lark_oapi 时跳过）
测试钩子（环境变量）：REQ_BOARD_API、REQ_BOARD_PORT、REQ_BOARD_POLL_SEC、REQ_BOARD_MIN_AGE_MIN、KIMI_BIN、
REQ_BOARD_STATE（状态文件路径覆盖，测试运行器指到临时目录用）、
REQ_BOARD_TEST（=1 视为测试进程：跳过真实 worktree 创建，见 #420）
"""
import base64
import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

# dispatcher_modules/ 经 runtime.D 晚绑定读取本模块：`python dispatcher.py`（__main__）与
# `import dispatcher` 必须是同一个模块对象，否则拆出去的代码读到的是另一份状态。
sys.modules.setdefault("dispatcher", sys.modules[__name__])

from storage_time import parse_storage_time, format_storage_time
import notifiers
from notifiers.feishu import FeishuNotifier
from pathlib import Path
from urllib.parse import quote, urlparse

# 飞书 SDK 为可选依赖：未安装 lark_oapi 时调度器照常运行，飞书通道按 REQ_BOARD_NO_FEISHU=1
# 同口径停用（确认/评审走看板按钮）。
try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import *
    # #22：飞书建卡正文抓取（wiki v2 get_node / docx raw_content）所需请求模型。
    # 注意：wiki/v1/resource/node.py 只有 search、无 get_node，不可引用；必须用 wiki v2 Space.get_node。
    from lark_oapi.api.docx.v1.model.raw_content_document_request import RawContentDocumentRequest
    from lark_oapi.api.wiki.v2.model.get_node_space_request import GetNodeSpaceRequest
    LARK_AVAILABLE = True
except ImportError:
    lark = None
    LARK_AVAILABLE = False

# macOS 框架版 Python 默认证书链不全，飞书长连接（websockets 库）走 SSL 需要显式指定 certifi
import ssl
try:
    import certifi
except ImportError:  # certifi 随飞书 SDK 依赖安装；缺失时沿用系统证书链
    certifi = None
_orig_ssl_ctx = ssl.create_default_context
def _ssl_ctx_with_certifi(*args, **kwargs):
    kwargs.setdefault("cafile", certifi.where())
    return _orig_ssl_ctx(*args, **kwargs)
if certifi is not None:
    ssl.create_default_context = _ssl_ctx_with_certifi

# ACP 会话/只读工具共享模块（#107 抽取）：ACPSession、ReadonlyMcpServer、路径/SQL 校验、
# 只读命令白名单、/usage 解析、KIMI/ACP_MODEL 等常量。dispatcher 侧全部经此 import 使用，
# 事件上报注入 post_event（HTTP 上报看板），测试断言仍走 `import dispatcher`（名字原样导出）。
from acp_shared import (  # noqa: E402
    ACPSession, ReadonlyMcpServer, ACP_MODEL,
    FILE_MAX_BYTES, GIT_LOG_EMPTY_HINT, GIT_LOG_LINE_MAX, KIMI,
    READONLY_TOOL_GUIDE, READONLY_TOOL_SERVER, REDLINE_CANCEL_SIGNAL, RESULT_FILENAME,
    ROLE_CAPABILITIES, WORKTREES_ROOT, TAPD_TOOL_PREFIX, PLAYWRIGHT_TOOL_PREFIX,
    append_kill_redline,
    dbq_sql_safe, git_log_count_ok,
    git_log_env, git_log_path_filter, git_log_repo_ok, parse_usage, parse_usage_percent,
    model_alias_pool, pct_value, readonly_cmd_safe, resolve_path_safe, role_capability,
    role_meta, role_model, role_model_detail, resolve_role_model, role_prompt, flows, flow_by_id, flow_names, pre_role, default_flow_id,
    enabled_flows, flow_enabled,
    is_run_local_supervisor,
    TurnAborted,
    project_escalation_model, _pool_lookup, role_budget,
    load_project_manifest, skill_text, project_external_roles,
)

# 项目级 git 同步/推送共享件（Lead 维护，单一事实源）：git_env / repo_lock / run_git /
# remote_names / current_branch / ahead_behind。dispatcher 只消费，不再自行实现这些能力。
import git_ops  # noqa: E402

# 配置归一（load_projects 在 import 期即执行）可能告警，logger 必须早于配置解析创建；
# 生产入口 main() 再为它挂 FileHandler（见 _mount_file_logger）。
log = logging.getLogger("dispatcher")

# ==================== 配置 ====================
REPO_DIR = Path(__file__).resolve().parent
BOARD_API = os.environ.get("REQ_BOARD_API", "http://127.0.0.1:8899/api/requirements")
# 看板根 API（/agent-sessions、/manual-commands 等顶层路由），从 BOARD_API 派生
BOARD_ROOT_API = BOARD_API.rsplit("/requirements", 1)[0] if "/requirements" in BOARD_API else BOARD_API
# 自动开发的项目清单：projects.json（看板 project 名 -> {paths, confirm}），与看板页面共享，加项目改这个文件
# REQ_BOARD_PROJECTS 覆盖路径（与 acp_shared / board 同口径；测试实例与注入预览子进程靠它读隔离清单）
PROJECTS_FILE = Path(os.environ.get("REQ_BOARD_PROJECTS") or (REPO_DIR / "projects.json"))
# 评审/开发产物统一外移（不在任何仓库/worktree 内，避免随分支清理丢失）
ARTIFACTS_ROOT = Path.home() / ".req-board" / "artifacts"
# dev 分支命名规范：f-YYYYMMDD-<req_id>（日期取需求创建日，跨天不漂移）；仅推分支项目（branch_only）分支保留到发版

# ==================== 项目级 Git 配置归一（git / git_sync / worktree） ====================
# 契约（projects.json，Lead 已配置）：
#   git:      {remote, url, credential: "system"|"token"|"ssh", ssh_key}
#   git_sync: {before_card: "off"|"fetch"|"pull", on_done: "off"|"auto"|"manual",
#              diverge: "overlap-check"|"ff-only"|"stop", timeout: 120}
#   worktree: "per-requirement"
# 兼容旧布尔：fetch_before=true → before_card=pull、push_after=true → on_done=auto；
# 枚举值优先于同字段旧布尔。未配置 git_sync = None = 全局零行为变化（任何 git 远端操作都不发生）。
# 非法值一律「忽略 + 告警」不阻断调度（与 restart 的 fail-fast 口径刻意不同：git 同步是增强项，
# 配置写错只应退化为不做，不应让整张卡卡死）。
from dispatcher_modules.project_config import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/project_config.py
    AGENT_TIMEOUT, BUDGET_WRAPUP_SEC, BUDGET_WRAPUP_STEPS, DIAG_TIMEOUT, FLOW_NAMES,
    FLOW_REVIEW_ROUNDS_MAX, GIT_CREDENTIAL_KINDS, GIT_SYNC_BEFORE_CARD, GIT_SYNC_DEFAULTS,
    GIT_SYNC_DIVERGE, GIT_SYNC_ON_DONE, GIT_TIMEOUT, GIT_WORKTREE_PER_REQUIREMENT,
    GIT_WORKTREE_TIMEOUT, HEARTBEAT_FILE, HELD_TTL_SEC, LAUNCHCTL_TIMEOUT, MANUAL_POLL_INTERVAL,
    MAX_PARALLEL, MAX_RETRIES, MAX_TIMEOUT_MINUTES, MERGE_RESOLVE_MAX_ROUNDS,
    MERGE_RESOLVE_TIMEOUT_MIN, MIN_AGE_MINUTES, MIN_TIMEOUT_MINUTES, NO_FEISHU, POLL_INTERVAL,
    PROJECTS, RATE_LIMIT_LOG_FMT, RATE_LIMIT_RETRY_DELAY, RATE_LIMIT_TAIL_BYTES,
    RESTART_SOURCE_FILE, ROLE_NAMES, SEMI_PHASES, STATE_FILE, TICK_WARN_SEC, WATCHDOG_TICK_SEC,
    _GIT_SYNC_ENUM_ALIASES, _LarkReconnectNoiseFilter, _PROJECTS_SIG, _git_sync_enum,
    _maybe_reload_projects, _normalize_git, _normalize_git_sync, _normalize_worktree,
    _projects_file_sig, effective_timeout_minutes, last_tick, last_tick_mono, load_projects,
    requirement_timeout,
)

# ==================== 状态持久化 ====================
state_lock = threading.Lock()
# #420：save_state 落盘守卫（进程级内存标志，不进 state、不落盘）。只有跑过 load_state() 的进程
# （生产唯一入口 main()）才允许写状态文件——任何「只 import、不跑 main」的进程触发 save_state
# 会把 import 时的空骨架覆盖真实 data/dispatcher_state.json（丢会话注册/在途卡/pending_confirm）。
_state_loaded = False
state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {}, "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {}, "held_procs": {}, "pending_restart": {}, "flow_review_rounds": {}, "merge_resolve": {}, "confirm_rounds": {}, "stale_wt_blocked": {}, "model_fallback": {}, "feishu_chats": {}, "feishu_greeted": {}, "feishu_assistant": {}, "feishu_seen": {}, "review_escalation": {}, "review_round_points": {}}

# #284 进程保持池（内存态，不落盘）：key = agent_sessions 行 id（asid），
# 人工续跑一轮正常结束后进程保持，下一条消息直接复用（省拉起+resume）；闲置超时由 held_reaper 回收。
# 保持中的进程 pid 镜像到 state["held_procs"]（调度器重启后据此清理孤儿）。
held_sessions = {}     # asid -> {"session": ACPSession, "logf": file, "req_id": str, "role": str, "kept_at": float}
held_lock = threading.Lock()

def load_state():
    global _state_loaded
    # #420：置位在读文件之前——① 文件缺失/损坏时本进程仍可合法归一化落盘；
    # ② 函数末尾自身的 save_state() 不被自己的守卫拒绝
    _state_loaded = True
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except Exception as e:
            log.warning(f"状态文件损坏，忽略: {e}")
    for k in ("retries", "pending_confirm", "pending_route", "pending_review", "sessions", "worktrees", "req_repos", "held_procs", "pending_restart", "flow_review_rounds", "merge_resolve", "confirm_rounds", "stale_wt_blocked", "model_fallback", "feishu_chats", "feishu_greeted", "feishu_assistant", "feishu_seen", "review_escalation", "review_round_points"):
        if not isinstance(state.get(k), dict):
            state[k] = {}
    # 会话组概念已移除：清掉历史 g:<group_id> 会话键（需求全部平级独立）
    sessions = state.get("sessions") or {}
    for k in [k for k in sessions if str(k).startswith("g:")]:
        sessions.pop(k, None)
    save_state()

# #412（S1b）：save_state 写盘串行化锁——与 state_lock 分离：锁内仅序列化（内存），
# 锁外原子写；state_lock 内不再有任何文件 IO（#410 S1 死锁的配套，验收①严格版）
_state_disk_lock = threading.Lock()


def save_state():
    """#412：state_lock 内仅 json.dumps（内存），锁外原子写（tmp + os.replace）。
    旧实现锁内 write_text：① state_lock 内文件 IO；② 非原子（写一半崩溃可能损坏 STATE_FILE）。
    并发 save_state 的旧快照后写覆盖新快照窗口极小（下次 save_state 自愈），
    _state_disk_lock 保证写盘不交错。

    #420：未 load_state 的进程（只 import、不跑 main）一律拒绝落盘（fail-closed，零文件 IO），
    告警含目标路径（防「静默不落盘」）。生产唯一入口 main() 先 load_state，其后照常写。"""
    if not _state_loaded:
        log.warning("save_state 被拒绝（本进程未 load_state，#420 防线）：%s 保持原样", STATE_FILE)
        return
    with state_lock:
        payload = json.dumps(state, ensure_ascii=False, indent=2)
    with _state_disk_lock:
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, STATE_FILE)

# ==================== 看板 API ====================
# 纯本机回环请求，显式禁用代理：避免全局 opener 缓存的系统代理设置失效后导致 Connection refused
from dispatcher_modules.board_api import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/board_api.py
    SUMMARY_LONG_LINE_MIN, SUMMARY_MAX_POINTS, SUMMARY_PROMPT_KEEP, _NUM_MARK_RE, _SENT_SPLIT_RE,
    _close_session_row, _flush_model_degraded_summary, _format_long_segment,
    _model_unavailable_reason, _note_model_fallback, _opener, _parse_ts,
    _rate_limit_failure_reason, _redline_cancel_reason, _session_age_sec, _touch_session_activity,
    api, api_board, append_summary, backfill_spawn_session, compact_summary,
    confirm_answer_summary, fetch_req, find_resume_sid, format_summary_points, get_doing_reqs,
    get_open_reqs, kimi_version, mark_session_poisoned, owner_key, post_dispatcher_event,
    post_event, post_model_events, post_run_record, session_id_of, session_key, set_req,
    set_status, sync_agent_session, update_session_info,
)

# ==================== 工作区与路径 ====================
from dispatcher_modules.workspace import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/workspace.py
    _REAL_DEV_WORKTREE_PATH, _REAL_WORKTREES_ROOT, _TAPD_ID_RE, _default_branch,
    _link_repo_local_config, artifact_dir, dev_branch, dev_workdir_for, dev_worktree_path,
    dev_write_roots, effective_repos, in_test_env, outside_repos, parse_notes_repos,
    result_file_path, tapd_id_from_notes, validate_repos, workdir_for,
)

# ==================== 项目级 Git 同步/推送（git_sync v1） ====================
# 职责：按项目 git_sync 配置在主工作区做 fetch/快进/分叉判定（before_card + 收尾前），
# 以及 done 合并后按 on_done 推送主干（auto）或只统计待推送（manual）。
# 硬约束：全部 git 远端操作走 git_ops（git_env 禁交互 + repo_lock 跨进程串行 + run_git）；
# 未配置 git_sync（None）时本段所有入口零动作（不发任何 git 命令），既有行为逐字不变；
# 任何失败只 log.warning + post_dispatcher_event(kind=merge)，绝不抛、不阻断需求推进。
from dispatcher_modules.git_sync import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/git_sync.py
    DEFAULT_RESTART_SKIP_PREFIXES, DISPATCHER_AUTO_RESTART_DELAYED, DISPATCHER_AUTO_RESTART_OK,
    DISPATCHER_AUTO_RESTART_PENDING, DISPATCHER_RESTART_HINT, DISPATCHER_RESTART_HINT_RUN_LOCAL,
    GIT_IDENTITY_HINT, MAX_AUTO_RESTART_ATTEMPTS, RESTART_SOURCE_TTL_SEC, RESTART_TRIGGER_DIRS,
    RESTART_TRIGGER_FILES, _IDENTITY_BLOCKED, _RESTART_TODO_ACT_RE, _RESTART_TODO_BRACKET_RE,
    _RESTART_TODO_HUMAN_RE, _RESTART_TODO_LEAD_RE, _branch_base, _branch_guard_text,
    _collect_merge_changed_paths, _dirty_blockers_text, _fast_forward_main_repo, _git_err,
    _git_sync_event, _git_sync_remote, _git_sync_timeout, _identity_error, _judge_restart_needed,
    _kickstart_board_if_own, _local_base_branch, _maybe_auto_restart_after_merge,
    _merge_dirty_blockers, _merge_fail_text, _merge_guard_blockers, _merge_preflight,
    _project_git_cfg, _project_git_sync, _push_done_one_repo, _push_done_worktrees,
    _push_main_once, _reconcile_restart_todos, _repo_paths, _retry_rec, _sync_main_worktrees,
    _sync_one_main_repo, _touches_dispatcher, _wait_board_up, _wait_url_up, cleanup_dev_worktrees,
    clear_flow_rounds, clear_retries, dev_repo_listing, ensure_dev_worktrees, is_restart_trigger,
    merge_conflict_clear, merge_conflict_write, merge_dev_branches, push_company_branches,
    resume_failure_hint, run_project_restart,
)

# ==================== #27 待确认条目「键与内容同源」三层闸门 ====================
# 权威判据（单一）：条目内容卡的 id（entry["req"]["id"]）是身份权威，键服从内容；
# 卡是否终态一律 fetch_req 现取，禁用条目内快照的 status。
from dispatcher_modules.confirm_gates import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/confirm_gates.py
    EMPTY_SESSION_SWEEP_INTERVAL, EMPTY_SESSION_TTL_SEC, STALE_WORKTREE_INTERVAL,
    _CONFIRM_REQ_KEYS, _annotate_extra_repos, _clear_stale_worktree_warning, _confirm_req_snapshot,
    _finish_merge_resolve, _last_empty_session_sweep, _last_stale_wt_sweep,
    _main_pending_push_text, _maybe_sweep_empty_sessions, _maybe_sweep_stale_worktrees,
    _merge_conflict_fallback, _merge_resolve_entry, _merge_resolve_retry, _pending_content_req_id,
    _pending_key_mismatch, _pending_rehang, _pending_source_mismatch_note, _purge_ghost_confirm,
    _put_pending_confirm, _set_done, _stale_worktree_blocker, _start_merge_resolve,
    _warn_stale_worktree, cleanup_stale_worktrees, finalize_done, sweep_empty_sessions,
)

# ==================== #419 流程引擎（配置驱动 + 卡上快照） ====================
# 单一源 = flows.json（结构）+ prompts/*.md（提示词正文）；卡上快照 flow/flow_name/nodes_json/node_index
# 一经写入即冻结——改配置（删流程/改名/复用 id/增删节点）不影响进行中卡与历史卡。
# 无快照的历史卡按 mode/phase 懒映射到当前配置（与 backfill_flow_snapshots 同源映射，§4.2），
# 首次采纳流程即写快照冻结，此后推进只读快照。
from dispatcher_modules.flow_engine import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/flow_engine.py
    CARD_INPUTS_MARK, COMMON_TAIL, FIXED_PART_PLACEHOLDERS, PRE_SLOT_CONTRACT, ROLE_PROMPTS,
    _COMMAND_LABELS, _flow_catalog_text, _flow_reason_text, _legacy_position, _project_repos,
    _snapshot_raw, at_terminal_node, build_prompt, build_prompt_parts, current_node, flow_nodes,
    flow_of, flow_snapshot, in_pre_stage, next_stage, node_display, node_index_of, nodes_of,
    pre_slot_contract_text, project_conventions_text, role_allowed, role_for,
    role_skill_names, scenario_skills_text, split_role_template, stage_input_for, stage_of_review,
    FRAGMENT_ORDER, project_flags, selected_fragments,
    AGENTS_MD_NATIVE_RUNTIMES, agents_md_files, agents_md_nested_text, card_data, card_paths_hint,
    fragments_text, runtime_for,
)

# ==================== 上下文编译器 + 执行档案（context-design 第三、七节） ====================
from dispatcher_modules.context import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/context.py
    Compiled, MissingRequiredContext, build_manifest, compile_context, context_runtime_item, last_compiled, new_run_key,
    previous_manifest, public_items, record_session_start, runs_dir, write_run_record,
    resume_plan as context_resume_plan,
)

# ==================== 飞书机器人 ====================
from dispatcher_modules.feishu_client import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/feishu_client.py
    ASSISTANT_CARD_MAX_ITEMS, CARD_BODY_LIMIT, CARD_EVENTS, CARD_TITLE_LIMIT, CHAT_TIMEOUT,
    FEISHU_ASSISTANT_BUSY_HINT, FEISHU_ASSISTANT_DEADLINE_SEC, FEISHU_ASSISTANT_DOWN_HINT,
    FEISHU_ASSISTANT_MODEL, FEISHU_ASSISTANT_MODEL_REASON, FEISHU_ASSISTANT_POLL_SEC,
    FEISHU_ASSISTANT_SEEN_WINDOW, FEISHU_ASSISTANT_TIMEOUT_TEXT, FEISHU_BOOT_WAIT_SEC,
    FEISHU_CHAT_IDLE_SEC, FEISHU_CONFIG_FILE, FEISHU_CONNECT_TIMEOUT_SEC,
    FEISHU_IDLE_SWEEP_INTERVAL, FEISHU_NEW_BUILD_HINT, FEISHU_NO_SESSION_CONFIRM_HINT,
    FEISHU_NO_SESSION_END_HINT, FEISHU_PROMPT_MAX, FEISHU_TEXT_CHUNK_SIZE, FEISHU_TEXT_MAX_CHUNKS,
    _FEISHU_SECRET, _feishu_assistant_model_resolve, _feishu_credentials_source,
    _read_feishu_assistant_model, _read_feishu_config, _sanitize_feishu_error, feishu,
    feishu_credential_fingerprint, init_feishu, load_feishu_config,
)

# ==================== 可插拔通知渠道（notifiers/） ====================
# 注册表单例：飞书为同步渠道（启动即注册，未启用时其发送函数自行只记日志）；Webhook/Slack 由
# notifiers.json（与 feishu.json 同目录，REQ_BOARD_NOTIFIERS_JSON 可覆盖）驱动，文件变化自动重载、
# 后台线程发送、配置非法只停用该渠道（fail-closed，不崩调度器）。
from dispatcher_modules.notify import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/notify.py
    NOTIFIERS, NOTIFIERS_CONFIG_FILE, _body_divs, _card_paragraphs, _feishu_notify,
    _feishu_notify_req_card, _feishu_notify_text, _feishu_send_assistant_card, _feishu_typing,
    _md_field, _notifier_details, _path_short, _phase_name, _send_feishu, artifact_short,
    build_req_card, build_req_list_card, esc_md, fallback_text, notify, notify_req_card,
    requirement_list_fallback_text, send_text_chunked, send_text_lossless, truncate,
)

from dispatcher_modules.feishu_inbound import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/feishu_inbound.py
    _FeishuStatusClient, _feishu_connect_watchdog, _feishu_group_text,
    _feishu_post_text_and_handles, _feishu_text_and_handles, _is_reset_verdict, _note_review_model,
    _on_message, _review_gate_rework, _spawn_safe, on_message, start_feishu_ws,
)

# ==================== 飞书多轮聊天建卡（#22 D4-D11） ====================
# 数据流：飞书消息 → on_message（解析 + 入队，秒回，不阻塞 ws 回调）→ per-chat worker 串行处理
# → 项目解析（未定则追问）→ 素材按句柄延迟下载 → 等会话 idle → /prompt → 轮询轮次结束 → 文本发回飞书。
# 本地状态 state["feishu_chats"][chat_id] 只存文本与下载句柄，素材字节永不落盘/常驻。
from dispatcher_modules.feishu_chat import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/feishu_chat.py
    FEISHU_CMD_CONFIRM, FEISHU_CMD_END, FEISHU_CMD_NEW, _FEISHU_BUSY_HINT, _FEISHU_CMD_EDGE,
    _FEISHU_DOMAINS, _FEISHU_LINK_REJECT_HINT, _FEISHU_MATERIAL_EXTS, _FEISHU_MATERIAL_MAX_BYTES,
    _FEISHU_MATERIAL_MAX_FILES, _FeishuRedirectGuard, _feishu_ask_project, _feishu_await_round,
    _feishu_boot_and_prompt, _feishu_chat_status, _feishu_chats, _feishu_close_chat,
    _feishu_confirm, _feishu_connected_ip, _feishu_docx_raw, _feishu_download_pending,
    _feishu_download_resource, _feishu_draft_ready_prompt, _feishu_emit, _feishu_expand_text,
    _feishu_ext_ok, _feishu_fetch_doc_text, _feishu_fetch_link_text, _feishu_find_links,
    _feishu_greet_once, _feishu_greeted, _feishu_handle_material, _feishu_handle_text,
    _feishu_http_err, _feishu_ip_public, _feishu_is_cmd, _feishu_is_feishu_host,
    _feishu_local_by_session, _feishu_new_sess, _feishu_note_replay, _feishu_opener,
    _feishu_parse_material, _feishu_poll_until, _feishu_project_ask_text, _feishu_project_lines,
    _feishu_project_names, _feishu_prompt_once, _feishu_replay, _feishu_reset_session,
    _feishu_resolve_project, _feishu_route_text, _feishu_save, _feishu_seen_once,
    _feishu_send_prompt, _feishu_send_user_prompt, _feishu_sess, _feishu_start_build,
    _feishu_status_from_error, _feishu_upload_materials, _feishu_url_allowed,
    _feishu_wiki_one_level, _last_feishu_idle_sweep,
)

# ==================== 看板助手会话编排（#24） ====================
# 与单项目建卡完全分离：本地状态 state["feishu_assistant"][chat_id] 只存编排元数据
# （session_id / feishu_chat_id / last_event_id / pending / awaiting_project / turn_id / updated_at），
# **不存会话原文与素材字节**；board 侧会话与事件落独立的助手表。
# 计时：worker 起表 90s 总 deadline → 每次调用 board 传剩余秒数（board 权威计时并自行收尾），
# dispatcher 只做超时兜底（cancel + 固定文案一次，保留 session_id 供下次 resume）。
# 不复用建卡的 _chat_boot/_chat_prompt_thread/_start_chat_round 与 CHAT_TIMEOUT(600s) 语义。
from dispatcher_modules.feishu_assistant import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/feishu_assistant.py
    _feishu_assistant, _feishu_assistant_cancel_pending, _feishu_assistant_ensure_session,
    _feishu_assistant_entry, _feishu_assistant_handles_prepare, _feishu_assistant_material_prepare,
    _feishu_assistant_q, _feishu_assistant_reconcile, _feishu_assistant_round,
    _feishu_assistant_safe, _feishu_assistant_status, _feishu_assistant_submit,
    _feishu_assistant_wait_resumable, _feishu_await_round_assistant, _feishu_enqueue,
    _feishu_expire_idle_chats, _feishu_handle, _feishu_handle_assistant_text,
    _feishu_material_to_assistant, _feishu_queue_lock, _feishu_queues, _feishu_worker,
    _feishu_workers,
)

# ==================== agent 运行（ACP 模式） ====================
from dispatcher_modules.agent_run import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/agent_run.py
    CONFIRM_ROUNDS_MAX, RESUME_STYLE_REMINDER, REVIEW_DIFF_MAX_LINES, WEB_CONFIRM_ACTIONS,
    _artifact_ls, _budget_wrapup, _close_held, _extract_todos, _gen_review_diff, _hold_session,
    _mark_swept, _notify_need_confirm, _parse_confirm_options, _register_need_confirm, _steps_text,
    _swept, _swept_lock, _swept_rows, active_lock, active_runs, artifact_ok, build_followup_prompt,
    changed_repos, has_code_changes, readonly_tool_roots, result_state, run_agent, spawn_agent,
    _record_start, resume_text,
)

# ==================== #419 流程推进（节点完成 → 下一节点 / 收尾） ====================
from dispatcher_modules.flow_advance import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/flow_advance.py
    QUICK_FLOW_ID, QUICK_UPGRADE_FLOW_ID, REVIEW_EXTRA_MAX_CHARS, _adopt_flow_and_spawn,
    _advance_after_node, _apply_model_override, _clear_review_escalation, _escalation_candidate,
    _path_under, _pre_stage_done, _quick_high_risk_prefixes, _quick_upgrade, _review_bounce_extra,
    _review_node_done, _run_terminal, _write_flow_snapshot, backfill_flow_snapshots, handle_result,
    review_escalation_override,
)

# ==================== 决策对账 & 主循环 ====================
from dispatcher_modules.reconcile import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/reconcile.py
    _adopted_rids, _execute_confirm_action, _mark_running_rows_idle, confirm_decision_clear,
    reconcile_adopt_results, reconcile_confirmations, reconcile_decisions, reconcile_ghost_pending,
    reconcile_pending_confirm_source, reconcile_startup,
)

from dispatcher_modules.session_upkeep import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/session_upkeep.py
    _proc_dead_for, _reap_held_once, _run_manual_command, _session_held, cleanup_orphan_held,
    consume_manual_commands, held_reaper, manual_commands_poller, reconcile_orphan_sessions,
    recover_manual_commands,
)

from dispatcher_modules.heartbeat import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/heartbeat.py
    _PROD_HEARTBEAT_FILE, _heartbeat_lock, _heartbeat_write_locked, _update_feishu_status,
    load_heartbeat, write_heartbeat,
)

# ---- #373 调度器重启原因可见化：SIGTERM/SIGINT 信号记录 + 约定式重启来源 ----
from dispatcher_modules.lifecycle import (  # noqa: E402,F401  拆分回挂，见 dispatcher_modules/lifecycle.py
    _exec_self_restart, _fallback_manual_hint, _maybe_auto_restart, _pop_restart_source,
    _queue_auto_restart, _read_restart_source, _record_shutdown, _restart_source_fresh,
    _shutdown_handler, _signal_name, _stray_acp_pids, _verify_pending_restart,
    register_shutdown_handlers, report_previous_exit,
)

def _mount_file_logger():
    """生产入口（main）专用：把调度器日志落盘 /tmp/req_board_dispatcher.log。
    模块级 basicConfig 只挂 stderr，import dispatcher 的进程（测试/脚本）不写生产日志；
    main() 是唯一生产入口，挂载后生产行为不变（仍写 /tmp 日志 + stderr）。
    #11：同一 FileHandler 幂等增挂 acp_shared logger——模型落定/降级 WARNING 才有落盘痕迹
    （验收 #3「可从调度器日志定位」）。重复调用不重复挂载、不新建多余 handler。"""
    path = "/tmp/req_board_dispatcher.log"
    need = [lg for lg in (log, logging.getLogger("acp_shared"))
            if not any(isinstance(h, logging.FileHandler) and h.baseFilename == path
                       for h in lg.handlers)]
    if not need:
        return
    fh = logging.FileHandler(path)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for lg in need:
        lg.addHandler(fh)

def _finish_dead_session(row, req, role, rec):
    """#388 兜底收尾线程：进程死后补走 run_agent finally 的收尾三件事——
    行置 idle + 运行记录落库 + handle_result（result.json 合法则采纳推进，缺失/非法走
    handle_result 既有失败通道重试）。run_agent 线程恢复后经 _swept 标记跳过重复收尾。
    收尾前先按对象匹配摘除旧的（死进程）active_runs 登记——否则 handle_result 内
    _spawn_safe 见 active_runs 含本需求被陈旧记录误拦，下一阶段（dev/review）不会被拉起，
    卡片停在 doing 无人驱动（审查问题 1）；对象匹配防误删 sweep 之后新会话的登记。"""
    req_id = str(req["id"])
    with active_lock:
        if active_runs.get(req_id) is rec:
            active_runs.pop(req_id, None)
    proc = rec.get("proc")
    # 运行记录状态按进程级判定：poll() 已完成收集时 returncode 可用；None 兜底按异常退出记
    returncode = getattr(proc, "returncode", None)
    if returncode is None:
        returncode = 1
    try:
        # #421：统一入口（置 idle + 清 turn_step）
        _close_session_row(req, role, row.get("id"), "idle", session_id=row.get("session_id"))
    except Exception as e:
        log.warning(f"需求 #{req_id} 兜底收尾会话行置 idle 失败: {e}")
    try:
        # #42：死进程兜底同样带上缓存命中三态（会话对象上已采集的轮次照记，取不到为 None）
        dead_sess = rec.get("session")
        try:
            dead_ext = dead_sess.cache_summary() if dead_sess is not None else None
        except Exception:
            dead_ext = None
        post_run_record(req, role, dead_sess,
                        rec.get("started_at") or time.time(), time.time(),
                        returncode, False, None, usage_ext=dead_ext)
    except Exception as e:
        log.warning(f"需求 #{req_id} 兜底收尾运行记录失败: {e}")
    try:
        handle_result(req, role, 0, False, rec.get("started_at") or time.time())
    except Exception:
        # #412（B）：兜底收尾 handle_result 异常留痕（旧实现 log.warning 无 traceback）
        log.exception(f"需求 #{req_id} 兜底收尾 handle_result 失败")


def _sweep_dead_running_once():
    """#388 主循环兜底（每 tick 一道）：run_agent 收尾在会话线程 finally 内，调度器被杀/
    进程死后收尾丢失（#386 现场）——补扫 agent_sessions 中 status=running 且「有持有者键 +
    进程确死」的会话行，走 _finish_dead_session 补收尾（状态转 idle + run 记录落库 +
    handle_result）。收尾在独立线程执行（handle_result 失败退避不阻塞主循环）。
    边界二分（与 #374 互补）：
    - 无持有者键 → 归启动时 reconcile_orphan_sessions 清算（interrupted）；
    - 有持有者键但 active_runs 无记录（重启残留）→ 归启动序列 adopt/orphan 处理，
      运行期不碰（避免与主循环重捞竞态双 spawn）；
    - 本函数只收「active_runs 有记录且 proc.poll() 非 None」的运行期死进程。
    _swept_rows 防重：会话线程 finally 恢复后跳过重复收尾。
    返回本轮发现的僵尸数（单测断言用）。"""
    try:
        rows = api_board("GET", "/agent-sessions")
    except Exception as e:
        log.warning(f"拉取 agent_sessions 失败（运行中会话兜底扫描跳过）: {e}")
        return 0
    sessions = state.get("sessions") or {}
    found = 0
    for r in rows:
        if r.get("status") != "running":
            continue
        sid = r.get("session_id")
        rid = str(r.get("req_id") or "")
        if not sid or not rid:
            continue
        if not _session_held(r, sessions):
            continue  # 无持有者键归 #374 启动清算
        row_id = r.get("id")
        if _swept(row_id):
            continue
        with active_lock:
            rec = active_runs.get(rid)
        if rec is None:
            continue  # 重启残留归启动序列（adopt/orphan），运行期不碰
        poll = getattr(rec.get("proc"), "poll", None)
        if callable(poll) and poll() is None:
            continue  # 正常运行中的会话，绝不误动
        req = fetch_req(rid)
        if req is None:
            continue
        role = (r.get("role") or "").strip() or rec.get("role") or role_for(req)
        _mark_swept(row_id)
        found += 1
        log.warning(f"运行中兜底收尾：req #{rid} {role} 会话进程已死（row={row_id}），补收尾（idle + 运行记录 + handle_result）")
        threading.Thread(target=_finish_dead_session, args=(r, req, role, rec),
                         daemon=True).start()
    return found


def _watchdog_check():
    """#412（D）单步检测（测试可直调）：tick 停滞超阈值 → log.warning，返回是否停滞。
    #410 形态：tick 阻塞在 state_lock 永不返回，同步计时永远执行不到，必须独立线程轮询；
    只告警不 kill（fail-closed，不自动熔断/重启调度器）。
    计时用 monotonic（last_tick_mono）：系统睡眠期间不前进，合盖睡眠不误报，
    真阻塞（含 #410 锁死）时 monotonic 正常前进仍能检出；wall clock 只用于日志展示时间。"""
    stalled = time.monotonic() - last_tick_mono
    if last_tick_mono and stalled > TICK_WARN_SEC:
        log.warning(f"主循环 tick 停滞告警：{stalled:.0f}s 无新 tick"
                    f"（last_tick={format_storage_time(last_tick)[11:]}），"
                    f"调度器仍在运行但主循环疑似阻塞（_main_loop_tick），心跳未更新")
        return True
    return False


def _watchdog_loop():
    """#412（D）：主循环看门狗独立 daemon 线程（WATCHDOG_TICK_SEC 轮询 _watchdog_check）"""
    log.info(f"主循环看门狗线程启动（轮询 {WATCHDOG_TICK_SEC}s，停滞阈值 {TICK_WARN_SEC}s）")
    while True:
        time.sleep(WATCHDOG_TICK_SEC)
        try:
            _watchdog_check()
        except Exception:
            pass


_NO_ROLE_WARN_INTERVAL = 600   # #430 停泊告警节流：同需求最短间隔（秒），防 15s tick 刷屏
_NO_ROLE_WARN_TTL = 3600       # 节流条目保留时长（秒），超时条目顺手清理
_no_role_warned = {}           # {req_id: 上次告警 ts}；进程内 dict，不持久化（重启即清零）


def _warn_no_role(req):
    """#430 配置降级安全停泊：无角色可拉起且卡不在 terminal → 不推进、不拉起、不收尾，仅告警留痕。
    卡保持 open，每 tick 重新评估；配置修复并重启调度器后自动恢复拉起。"""
    rid = str(req["id"])
    now = time.time()
    for k in [k for k, ts in _no_role_warned.items() if now - ts > _NO_ROLE_WARN_TTL]:
        _no_role_warned.pop(k, None)
    if now - _no_role_warned.get(rid, 0) < _NO_ROLE_WARN_INTERVAL:
        return
    _no_role_warned[rid] = now
    log.error(f"需求 #{rid} 无角色可拉起（流程配置降级/当前节点无角色），停泊不推进："
              f"不拉起、不推进、不收尾，等待配置修复并重启调度器")
    try:
        post_dispatcher_event(rid, "流程配置降级：无角色可拉起，已停泊（不推进/不拉起/不收尾）",
                              kind="action")
    except Exception as e:
        log.warning(f"需求 #{rid} 停泊留痕上报失败: {e}")


def _main_loop_tick():
    """单轮调度（#380 起）：先做自动重启检查再 spawn。同轮 reconcile spawn 的新 agent 由
    run_agent 线程构造完 ACPSession（50-300ms）后才登记 active_runs，若先 spawn 再检查会出现
    「检查时 active_runs 恰为空 → kill 自身」的竞态（daemon 线程被杀、已 Popen 的 kimi 进程
    孤儿化）；检查前移后 pending 满龄时先 kill 自身，新进程从零开始 spawn，同轮竞态机制性
    消除，跨轮窗口由 POLL_INTERVAL（15s）覆盖。
    #412（D）：tick 起点先更新 last_tick——卡死在 tick 内也会被 watchdog 检出。"""
    global last_tick, last_tick_mono
    last_tick = time.time()
    last_tick_mono = time.monotonic()   # 停滞检测基准（排除系统睡眠，见 last_tick_mono 定义）
    try:
        _maybe_auto_restart()
    except Exception as e:
        log.error(f"自动重启检查异常: {e}")
    try:
        _maybe_reload_projects()   # #4：projects.json mtime 热重载（模型覆盖等配置当轮生效）
    except Exception as e:
        log.error(f"projects.json 热重载异常: {e}")
    try:
        _maybe_sweep_stale_worktrees()   # #428：人工收尾的残留 worktree 定期对账（低频）
    except Exception as e:
        log.error(f"残留 worktree 巡检异常: {e}")
    try:
        _maybe_sweep_empty_sessions()    # #431：超期空会话行清理（低频；TTL=0 关闭）
    except Exception as e:
        log.error(f"空会话清理异常: {e}")
    try:
        _feishu_expire_idle_chats()      # #22 D8″：空闲飞书建卡会话过期兜底（低频）
    except Exception as e:
        log.error(f"飞书空闲会话巡检异常: {e}")
    try:
        reconcile_decisions()
        # #400 网页确认动作对账（confirm_decisions 表消费；与 reconcile_decisions 同轮互不影响）
        try:
            reconcile_confirmations()
        except Exception as e:
            log.error(f"确认对账异常: {e}")
        # #388 收尾兜底：进程已死的 running 会话行补收尾（线程内执行，不阻塞本 tick；
        # 收尾 handle_result 可能 spawn 下一阶段，_spawn_safe 的 active_runs 去重兜底并发）
        try:
            _sweep_dead_running_once()
        except Exception as e:
            log.error(f"运行中会话兜底扫描异常: {e}")
        # 人工续跑命令由 manual_commands_poller 独立线程消费（短间隔，降低发消息排队延迟）
        with active_lock:
            running = {}
            active_owners = set()
            for r in active_runs.values():
                running[r["project"]] = running.get(r["project"], 0) + 1
                active_owners.add(r.get("owner"))
        pending = set(state["pending_confirm"]) | set(state["pending_route"]) | set(state["pending_review"])
        for project in PROJECTS:
            slots = MAX_PARALLEL - running.get(project, 0)
            for req in get_open_reqs(project):
                if slots == 0:
                    break
                if str(req["id"]) in pending:
                    continue
                owner = owner_key(req)
                if owner in active_owners:
                    continue
                role = role_for(req)
                if not role:
                    if at_terminal_node(req):
                        # #419：快照当前节点为 terminal（无角色可拉起）→ 主循环兜底执行收尾
                        # （§1.5 入口③，防任何未预见路径再次落成 open+terminal 死锁）
                        log.warning(f"需求 #{req['id']} 当前节点为收尾（terminal），主循环兜底执行收尾")
                        try:
                            _run_terminal(req)
                        except Exception as e:
                            log.error(f"需求 #{req['id']} 主循环收尾执行异常: {e}")
                    else:
                        # #430：无角色且卡不在 terminal（配置降级/无快照的前置卡）→ 安全停泊，
                        # 不得按收尾处理（否则会把未入流程的卡 PATCH done + 合分支，不可回滚）
                        _warn_no_role(req)
                    continue
                sid = find_resume_sid(req, role)
                # #109：续跑注入上次失败原因（重试计数/原因已持久化，调度器重启后仍可读到）
                spawn_agent(req, role=role,
                            extra=resume_failure_hint(req["id"]) if sid else "",
                            resume=bool(sid))
                active_owners.add(owner)
                slots -= 1
    except Exception as e:
        log.error(f"轮询异常: {e}")
    write_heartbeat()

def main():
    _mount_file_logger()
    # #8/#40：启动最前写 pid/boot_at（早于 register_shutdown_handlers/report_previous_exit/load_state/
    # 对账链）——供看板 pid 发现（重启入口）与 exec 自重启健康核对（boot_at 不被启动对账耗时拖出
    # 12s 健康核对窗口）；#40 起 launchd 与直跑同一路径都写（launchd 路径同样需要「心跳 pid = 当前
    # 调度器进程」），并在同一次加锁写入里清掉上一进程残留的 feishu 通道状态。
    try:
        write_heartbeat({"pid": os.getpid(), "boot_at": time.time()}, reset_feishu=True)
    except Exception as e:
        log.warning(f"启动写心跳 pid/boot_at 失败: {e}")
    register_shutdown_handlers()   # #373：尽早注册，SIGTERM/SIGINT 有记录地退出
    report_previous_exit()         # #373：启动时上报上次退出（信号时间 + 重启来源）
    load_state()
    try:
        _verify_pending_restart()  # #380：新进程自证自动重启（清标记 + 补发成功通知）
    except Exception as e:
        log.warning(f"自动重启自证失败: {e}")
    if NO_FEISHU:
        log.warning("REQ_BOARD_NO_FEISHU=1，显式停用飞书通道（确认/评审走看板按钮）" if LARK_AVAILABLE
                    else "未安装飞书 SDK（lark_oapi），停用飞书通道（确认/评审走看板按钮）")
        # #23：跳过连接也必须写心跳 disabled_reason=env——否则界面落「未上报」并露出不会生效的
        # 重启按钮（init_feishu 内部的 NO_FEISHU 分支即负责该写入，返回 None 不建 client）
        init_feishu()
    else:
        cfg = init_feishu()   # #22 fail-soft：无凭据/非法 → None，不崩溃
        if not cfg:
            log.warning("飞书凭据未配置，未建立长连接（y/n 确认走看板按钮；补 feishu.json 后重启调度器生效）")
        else:
            threading.Thread(target=start_feishu_ws,
                             args=(cfg["app_id"], cfg["app_secret"]), daemon=True).start()
    # #27 待确认条目同源对账：键与内容不同源先按键归一（目标键占用则保留原键告警）、
    # 内容卡现取终态清账——**必须先于 reconcile_ghost_pending**（后者按键判终态并 pop，
    # 错键条目会被静默清掉）
    try:
        reconcile_pending_confirm_source()
    except Exception as e:
        log.error(f"启动待确认同源对账失败（继续轮询）: {e}")
    # #407 幽灵待确认对账：done 卡残留 review_gate/merge pending 先清（启动第一步，
    # 避免残留状态干扰后续对账判定）；confirm/confirm_override 不碰
    try:
        reconcile_ghost_pending()
    except Exception as e:
        log.error(f"启动幽灵待确认对账失败（继续轮询）: {e}")
    # #419 存量卡流程快照回填（必须在 reconcile_startup 的 terminal 扫描之前：旧 review@accept
    # 卡正是靠回填产出 open+terminal，再由扫描直接收尾，否则 open 卡无角色可拉起 → 卡死）
    try:
        backfill_flow_snapshots()
    except Exception as e:
        log.error(f"启动流程快照回填失败（继续轮询）: {e}")
    # #388 顺序依赖：采纳合法结果必须先于 reconcile_startup 退 open（R1），
    # 否则退 open 后重捞的 prompt 会指示删除残留 result.json，合法结果被清后重跑整轮
    try:
        reconcile_adopt_results()
    except Exception as e:
        log.error(f"启动对账采纳合法结果失败（继续轮询）: {e}")
    try:
        reconcile_startup()
    except Exception as e:
        log.error(f"启动 reconcile 失败（继续轮询）: {e}")
    try:
        reconcile_orphan_sessions()
    except Exception as e:
        log.error(f"孤儿会话清算失败（继续轮询）: {e}")
    try:
        cleanup_stale_worktrees()
    except Exception as e:
        log.error(f"清理残留 worktree 失败: {e}")
    try:
        recover_manual_commands()
    except Exception as e:
        log.error(f"恢复人工命令失败: {e}")
    try:
        cleanup_orphan_held()
    except Exception as e:
        log.error(f"清理孤儿保持进程失败: {e}")
    # #431 上线存量清理：清掉历史遗留的空会话行（不限龄、幂等）。必须在 recover_manual_commands
    # 之后——pending 命令已就位，待接管行受看板 DELETE 守卫（有未消费命令 → 409）保护，首条消息不会丢
    try:
        sweep_empty_sessions(max_age_sec=None)
    except Exception as e:
        log.warning(f"启动空会话清理失败（继续轮询）: {e}")
    # #284：人工续跑命令消费独立短间隔线程（发消息排队延迟从最坏 15s 压到 ~2s）
    threading.Thread(target=manual_commands_poller, daemon=True).start()
    # #284：保持进程闲置超时回收线程
    threading.Thread(target=held_reaper, daemon=True).start()
    # #412（D）：主循环看门狗线程（检测 tick 停滞，只告警不 kill）
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    log.info(f"调度器启动：projects={list(PROJECTS)}, 每项目并行上限={MAX_PARALLEL}, 轮询间隔={POLL_INTERVAL}s")
    while True:
        _main_loop_tick()
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
