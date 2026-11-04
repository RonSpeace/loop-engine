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
from storage_time import parse_storage_time, format_storage_time
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
PROJECTS_FILE = REPO_DIR / "projects.json"
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
GIT_SYNC_BEFORE_CARD = ("off", "fetch", "pull")
GIT_SYNC_ON_DONE = ("off", "auto", "manual")
GIT_SYNC_DIVERGE = ("overlap-check", "ff-only", "stop")
GIT_SYNC_DEFAULTS = {"before_card": "off", "on_done": "off",
                     "diverge": "overlap-check", "timeout": 120}
GIT_CREDENTIAL_KINDS = ("system", "token", "ssh")
GIT_WORKTREE_PER_REQUIREMENT = "per-requirement"
# 前端/历史配置友好别名：语义等价 off（不拉取/不推送），归一后不保留别名形态。
_GIT_SYNC_ENUM_ALIASES = {"none": "off", "noop": "off", "disable": "off", "disabled": "off"}


def _git_sync_enum(project, field, value, allowed):
    """git_sync 枚举字段归一：合法值返回规范化字符串；None=未配置返回 None；
    别名映射为 off；非法值告警后返回 None（调用方回落到默认值）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        log.warning("projects.json: %s.git_sync.%s 必须是字符串（当前 %r），已忽略", project, field, value)
        return None
    val = value.strip().lower()
    val = _GIT_SYNC_ENUM_ALIASES.get(val, val)
    if val in allowed:
        return val
    log.warning("projects.json: %s.git_sync.%s 值非法（%r，合法值：%s），已忽略",
                project, field, value, "|".join(allowed))
    return None


def _normalize_git_sync(project, raw, legacy_before=None, legacy_done=None):
    """git_sync 归一：未配置（且无旧布尔开启）返回 None（=零行为变化）；否则返回完整 dict。
    旧布尔 fetch_before/push_after 只做兜底：对应枚举字段未给出合法值时生效（枚举优先）。
    项目根与 git_sync 段内的旧布尔都接受（历史配置形态不确定，宽容兼容）。"""
    if raw is not None and not isinstance(raw, dict):
        log.warning("projects.json: %s.git_sync 必须是对象（当前 %r），已忽略", project, raw)
        raw = None
    if legacy_before is None:
        legacy_before = (raw or {}).get("fetch_before")
    if legacy_done is None:
        legacy_done = (raw or {}).get("push_after")
    legacy_enabled = legacy_before is True or legacy_done is True
    # 注意用 is None 判定「未配置」：显式写 git_sync:{} 表示启用但全默认（等价全 off），
    # 是合法配置形态，不能因为空 dict 为假值就当成未配置。
    if raw is None and not legacy_enabled:
        return None
    raw = raw or {}
    sync = dict(GIT_SYNC_DEFAULTS)
    explicit = {}
    for field, allowed in (("before_card", GIT_SYNC_BEFORE_CARD),
                           ("on_done", GIT_SYNC_ON_DONE),
                           ("diverge", GIT_SYNC_DIVERGE)):
        explicit[field] = _git_sync_enum(project, field, raw.get(field), allowed)
        if explicit[field]:
            sync[field] = explicit[field]
    timeout_raw = raw.get("timeout")
    if timeout_raw is not None:
        try:
            timeout = int(timeout_raw)
        except (TypeError, ValueError):
            timeout = 0
        if timeout > 0:
            sync["timeout"] = timeout
        else:
            log.warning("projects.json: %s.git_sync.timeout 必须是正整数秒（当前 %r），回落到 %s",
                        project, timeout_raw, GIT_SYNC_DEFAULTS["timeout"])
    for legacy, field, mapped in ((legacy_before, "before_card", "pull"),
                                  (legacy_done, "on_done", "auto")):
        if legacy is None:
            continue
        if not isinstance(legacy, bool):
            log.warning("projects.json: %s.%s 旧布尔配置必须是 true/false（当前 %r），已忽略",
                        project, "fetch_before" if field == "before_card" else "push_after", legacy)
            continue
        if explicit.get(field) is None and legacy:
            sync[field] = mapped
    return sync


def _normalize_git(project, raw):
    """git 段归一：未配置返回 None；非法字段忽略 + 告警并回落到默认（remote=origin、
    credential=system）。只解析结构，不做凭据读取/校验（凭据由 git_ops 按项目名取）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning("projects.json: %s.git 必须是对象（当前 %r），已忽略", project, raw)
        return None
    cfg = {"remote": "origin", "url": "", "credential": "system", "ssh_key": ""}
    remote = raw.get("remote")
    if remote is not None:
        if isinstance(remote, str) and remote.strip():
            cfg["remote"] = remote.strip()
        else:
            log.warning("projects.json: %s.git.remote 必须是非空字符串（当前 %r），回落 origin",
                        project, remote)
    url = raw.get("url")
    if isinstance(url, str):
        cfg["url"] = url.strip()
    elif url is not None:
        log.warning("projects.json: %s.git.url 必须是字符串（当前 %r），已忽略", project, url)
    credential = raw.get("credential")
    if credential is not None:
        if isinstance(credential, str) and credential.strip().lower() in GIT_CREDENTIAL_KINDS:
            cfg["credential"] = credential.strip().lower()
        else:
            log.warning("projects.json: %s.git.credential 值非法（%r，合法值：%s），回落 system",
                        project, credential, "|".join(GIT_CREDENTIAL_KINDS))
    ssh_key = raw.get("ssh_key")
    if isinstance(ssh_key, str):
        cfg["ssh_key"] = ssh_key.strip()
    elif ssh_key is not None:
        log.warning("projects.json: %s.git.ssh_key 必须是字符串（当前 %r），已忽略", project, ssh_key)
    return cfg


def _normalize_worktree(project, raw):
    """worktree 段归一：目前只支持 per-requirement（每需求每仓库独立 worktree）。未知值
    只告警并按 per-requirement 处理——worktree 布局是调度器硬前提，非法值不能静默改布局。"""
    if raw is None:
        return GIT_WORKTREE_PER_REQUIREMENT
    if isinstance(raw, str) and raw.strip().lower() == GIT_WORKTREE_PER_REQUIREMENT:
        return GIT_WORKTREE_PER_REQUIREMENT
    log.warning("projects.json: %s.worktree 未知值（%r），按 %s 处理",
                project, raw, GIT_WORKTREE_PER_REQUIREMENT)
    return GIT_WORKTREE_PER_REQUIREMENT


def load_projects():
    """兼容归一：纯字符串路径 → {path} → {paths, confirm}，返回 {name: {"paths": [...], "confirm": bool}}"""
    raw = json.loads(PROJECTS_FILE.read_text())
    out = {}
    for name, cfg in raw.items():
        if isinstance(cfg, str):
            cfg = {"paths": [cfg]}
        elif isinstance(cfg, dict) and "path" in cfg and not cfg.get("paths"):
            cfg = {"paths": [cfg["path"]]}
        cfg = dict(cfg)
        paths = [str(Path(p)) for p in (cfg.get("paths") or []) if p]
        # branch_only（旧名 company，兼容别名）：仅推分支项目——done 时不本地合并 main，
        # 只把有改动仓库的 f-分支 push 到 origin（由外部 CI/CD 或人工发版时合并）；强制人工确认通道。
        # 内部字段沿用 company 键名。
        company = bool(cfg.get("branch_only")) or bool(cfg.get("company"))
        # repo_map：仓库地图（跨仓库定位代码的第一来源），analysis prompt 注入路径；不存在则忽略
        repo_map = str(cfg.get("repo_map") or "").strip() or ""
        # restart（#300）：done 后自动重启项目本地服务的配方 {cmd, verify?, timeout?}；
        # 非 dict/缺 cmd 视为未配置，timeout 非法回退默认 30s（配置为本地可信数据，宽容降级）
        # #394：配方扩展 trigger_prefixes/skip_prefixes（合并改动路径前缀判定清单，仅
        # auto_restart_after_merge=True 时使用）：显式配置但非字符串列表 = 配置错误 fail-fast
        restart_raw = cfg.get("restart")
        restart = None
        if isinstance(restart_raw, dict) and str(restart_raw.get("cmd") or "").strip():
            for rkey in ("trigger_prefixes", "skip_prefixes"):
                val = restart_raw.get(rkey)
                if val is not None and (not isinstance(val, list)
                                        or any(not isinstance(x, str) for x in val)):
                    raise ValueError(f"projects.json: {name}.restart.{rkey} 必须是字符串列表（当前 {val!r}）")
            try:
                restart = {"cmd": str(restart_raw.get("cmd")).strip(),
                           "verify": str(restart_raw.get("verify") or "").strip(),
                           "timeout": max(1, int(restart_raw.get("timeout") or 30))}
                for rkey in ("trigger_prefixes", "skip_prefixes"):
                    if restart_raw.get(rkey) is not None:
                        restart[rkey] = [str(x) for x in restart_raw.get(rkey)]
            except (TypeError, ValueError):
                restart = None
        # auto_restart_after_merge（#394）：done 合并成功后按改动文件类型三档判定是否自动重启
        # 项目服务（skip/restart/unknown）；缺省 False = 不启用文件类型判定（维持 #300 行为，
        # 项目已有 restart 配方则照常执行）。显式配置但非布尔值 = 配置错误 fail-fast。
        auto = cfg.get("auto_restart_after_merge")
        if auto is not None and not isinstance(auto, bool):
            raise ValueError(f"projects.json: {name}.auto_restart_after_merge 必须是布尔值（当前 {auto!r}）")
        auto = bool(auto)
        # goal（#5）：项目目标，注入 analysis 提示词（build_prompt 的 {project_goal_hint}）；
        # 非字符串/缺失 → ""（宽容降级，与 board _auto_projects_config 同口径）
        goal = cfg.get("goal") if isinstance(cfg.get("goal"), str) else ""
        # base_branch：项目级主干起点配置（str 或 {仓库绝对路径: 分支}），_default_branch
        # 运行时校验存在性（fail-open 回退默认探测），这里仅透传（#369 现场：归一化时
        # 丢字段导致配置永不生效，worktree 基线错拿本地 master）
        out[name] = {"paths": paths, "confirm": bool(cfg.get("confirm")) or company,
                     "company": company, "repo_map": repo_map, "restart": restart,
                     "auto_restart_after_merge": auto,
                     "base_branch": cfg.get("base_branch"), "goal": goal,
                     # 项目级 git 同步/推送（git_sync v1，见上方归一注释）：
                     # git=None/git_sync=None = 未配置 = 该维度零行为变化；worktree 恒归一
                     "git": _normalize_git(name, cfg.get("git")),
                     "git_sync": _normalize_git_sync(name, cfg.get("git_sync"),
                                                     cfg.get("fetch_before"),
                                                     cfg.get("push_after")),
                     "worktree": _normalize_worktree(name, cfg.get("worktree"))}
    return out

PROJECTS = load_projects()


def _projects_file_sig():
    """projects.json 变更签名 (mtime_ns, size)；stat 失败返回 None（#4 热重载）。"""
    try:
        st = os.stat(PROJECTS_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# 热重载基线：import 后随 PROJECTS 初始化；测试进程把 PROJECTS_FILE 钉到 fixture 时须同步置 None
_PROJECTS_SIG = _projects_file_sig()


def _maybe_reload_projects():
    """#4：projects.json 变化（mtime+size）时重载 PROJECTS，主循环每 tick 调用（单线程）。
    基线为 None（首轮/测试进程未钉基线）→ 仅记录不发重载；重载失败保留旧值 + log.error
    留痕并记录该失败签名（文件再次变化才重试，防每 tick 刷屏）。"""
    global PROJECTS, _PROJECTS_SIG
    sig = _projects_file_sig()
    if sig == _PROJECTS_SIG:
        return
    if _PROJECTS_SIG is None or sig is None:
        # 首轮（无基线）或文件缺失/不可 stat：仅记录，不重载不报错（静默跳过）
        _PROJECTS_SIG = sig
        return
    try:
        new = load_projects()
    except Exception as e:  # noqa: BLE001 - 重载失败降级：沿用旧配置，留日志待人工修
        log.error(f"projects.json 热重载失败，继续沿用旧配置: {e}")
        _PROJECTS_SIG = sig
        return
    PROJECTS = new
    _PROJECTS_SIG = sig
    log.info(f"projects.json 已热重载（{len(PROJECTS)} 个项目）")
MAX_PARALLEL = 3              # 每个项目最多同时运行的 agent 数
POLL_INTERVAL = int(os.environ.get("REQ_BOARD_POLL_SEC", "15"))   # 秒
# #284：人工续跑命令消费独立短间隔（默认 2s）——主循环 15s 的轮询会让「发消息→被消费」排队最坏等 15 秒，
# 独立线程把排队延迟压到秒级；其余逻辑（捞需求/对账/心跳）维持原间隔
MANUAL_POLL_INTERVAL = int(os.environ.get("REQ_BOARD_MANUAL_POLL_SEC", "2"))   # 秒
# #284：人工续跑一轮正常结束后进程保持（复用 kimi 会话跨轮 prompt，省拉起+resume），
# 闲置超时回收（默认 300s）——对齐聊天建卡页「进程保持 + 切走回收」模型（board 侧无前端 detach 通知，
# 以 TTL 兜底）
HELD_TTL_SEC = int(os.environ.get("REQ_BOARD_HELD_TTL_SEC", "300"))   # 秒
MIN_AGE_MINUTES = int(os.environ.get("REQ_BOARD_MIN_AGE_MIN", "1"))  # open 需求需静置该分钟数才会被捞取
AGENT_TIMEOUT = 240 * 60    # 单次运行上限（默认 4 小时，长任务可连续跑；需求 notes 可传 timeout: <分钟> 覆盖）
MIN_TIMEOUT_MINUTES, MAX_TIMEOUT_MINUTES = 15, 480   # per-requirement 超时预算 clamp 范围
MAX_RETRIES = 1             # 进程级失败自动重试次数（#273：最多重跑 2 次，耗尽转人工接管；
                            # #385 起收窄：blocked（内容性阻塞）不经此计数，正常退出直接转 later）
# #345：API 并发限流（403）失败分类与重试退避。限流是环境侧并发位被占，与进程行为无关：
# 30 秒立即重发会再次 403 烧光唯一重试 → 归类「API 并发限流」并延迟重试等待并发位释放
RATE_LIMIT_RETRY_DELAY = 90  # 限流失败的重试延迟（秒，需求区间 60~120 取 90）
RATE_LIMIT_LOG_FMT = "/tmp/req_board_agent_{req_id}.log"  # ACP stderr 重定向（与 spawn_agent 处 logf 约定一致）
RATE_LIMIT_TAIL_BYTES = 8192  # 限流特征只查日志尾部（限流发生在进程终止前）

def requirement_timeout(req):
    """per-requirement 单轮超时预算（分钟）：notes 支持 `timeout: <分钟>`（15~120，默认 60）。
    #109 时间感知：大需求可显式放大单轮上限，不全局放宽。"""
    notes = req.get("notes") or ""
    m = re.search(r"timeout\s*[:：]\s*(\d+)", notes)
    if not m:
        return AGENT_TIMEOUT // 60
    return max(MIN_TIMEOUT_MINUTES, min(MAX_TIMEOUT_MINUTES, int(m.group(1))))


# C2 步数截停后的收尾轮：限步数与时长，只够写 result.json 保底
BUDGET_WRAPUP_STEPS = 15
BUDGET_WRAPUP_SEC = 300


def effective_timeout_minutes(req, role):
    """C2 单轮时限（分钟）：merge_resolve 固定时间盒；notes 显式 `timeout:` 优先；
    否则取默认时限与角色预算 max_minutes（projects.json budgets）的较小值。"""
    if role == "merge_resolve":
        return MERGE_RESOLVE_TIMEOUT_MIN
    base = requirement_timeout(req)
    if re.search(r"timeout\s*[:：]\s*(\d+)", req.get("notes") or ""):
        return base
    cap = (role_budget(role, project=req.get("project")) or {}).get("max_minutes")
    return min(base, cap) if cap else base
# #420：REQ_BOARD_STATE 可覆盖状态文件路径（测试运行器统一指临时目录，物理隔离真实 state；
# 先例 board.py 的 REQ_BOARD_ARTIFACTS）。未设/空串 = 现默认，行为不变；必须保持 Path 类型
# （with_name/.name/read_text 依赖）。
STATE_FILE = (Path(os.environ["REQ_BOARD_STATE"]).expanduser() if os.environ.get("REQ_BOARD_STATE")
              else REPO_DIR / "data" / "dispatcher_state.json")
# #40：心跳文件与看板同口径——REQ_BOARD_DATA 非空时用该目录下 dispatcher_heartbeat.json
# （测试实例/演练与看板共用一份数据目录）；未设置时维持 REPO_DIR/data/（生产行为零变化）。
HEARTBEAT_FILE = ((Path(os.environ["REQ_BOARD_DATA"]).expanduser() if os.environ.get("REQ_BOARD_DATA")
                   else REPO_DIR / "data") / "dispatcher_heartbeat.json")
# #373：重启来源标记（约定入口写入，调度器启动/退出时读取，读完即删）
RESTART_SOURCE_FILE = REPO_DIR / "data" / "dispatcher_restart_source.json"
# 显式停用（环境变量）或未安装飞书 SDK（lark_oapi）时一律按停用处理
NO_FEISHU = os.environ.get("REQ_BOARD_NO_FEISHU") == "1" or not LARK_AVAILABLE

# #412（C）：subprocess 超时常量。收尾路径 git 写操作 60s（worktree 冷启动 90s）、
# launchctl 15s、只读诊断 30s。仅收尾路径补 timeout，防止 git 挂起无限期卡住收尾线程（#410 高嫌疑点 C）
GIT_TIMEOUT = 60
GIT_WORKTREE_TIMEOUT = 90
LAUNCHCTL_TIMEOUT = 15
DIAG_TIMEOUT = 30
# #412（D）：主循环看门狗——独立 daemon 线程检测 _main_loop_tick 停滞（tick 卡死时
# 同步计时执行不到，必须独立线程）；只告警不 kill（fail-closed，不自动熔断/重启调度器）
WATCHDOG_TICK_SEC = 5
TICK_WARN_SEC = int(os.environ.get("REQ_BOARD_TICK_WARN_SEC", "60"))
# 主循环每个 tick 起点更新的时间戳（模块级，watchdog 线程/测试读写）
last_tick = 0.0
# 单调时钟副本（2026-09-17 修复）：wall clock 在系统睡眠期间继续走，monotonic 暂停。
# 停滞检测必须用 monotonic，否则手提合盖睡眠会被误判为「tick 停滞」
# （现场：合盖 8 小时产生 36 条误报，09-13 起每天 26~115 条）。wall clock 仅用于日志展示。
last_tick_mono = 0.0

# 角色常量
# #425：route 确认的流程名白名单 = 配置启用流程（flows.json），见 on_message；不再维护硬编码清单
# FLOW_NAMES 派生自配置（flows.json 的 flows[].name），别再手写——手写副本会在改名时悄悄过期
# （#421 现场：heavy 改名「复杂」后，这里仍留着「加强」）。含软禁用流程（历史卡展示名照旧）。
FLOW_NAMES = flow_names()
FLOW_REVIEW_ROUNDS_MAX = 3  # #376/C3：agent 评审轮内重做上限——第 1–3 次拒自动打回重做（第 MAX 次拒时若配了
                            # 项目 escalation_model 先升级作者模型），第 MAX+1 次强制人评。注意别和卡评审 skill
                            # （req-board-review）自己的 2 轮硬上限搞混——两条独立通道，不联动
# #396：merge_resolve（自动合并冲突解决）配额与时间盒——每需求最多 2 轮（含首次），单轮 15 分钟；
# 失败/耗尽/need_confirm 均转现有飞书 y/n + 网页按钮人工通道（fail-closed，现有通道不改动）
MERGE_RESOLVE_MAX_ROUNDS = 2
MERGE_RESOLVE_TIMEOUT_MIN = 15
# phase 全集（board 白名单同款）：阶段角色 + agent 评审角色（评审角色 phase 走卡）
SEMI_PHASES = ("proposal", "tech", "tech_review", "dev", "code_review", "accept")
# #404/#419：角色中文名唯一手写源 = flows.json roles.<id>.display（#419 从能力表迁出，单一源），
# 此处派生（键集 = 能力表键集；配置加载失败时降级显示 role 原文，不阻断模块导入）
ROLE_NAMES = {role: (role_meta(role, "display") or role) for role in ROLE_CAPABILITIES}

# 模块级只挂 stderr：任何 import dispatcher 的进程（测试/脚本）都不写生产日志 /tmp/req_board_dispatcher.log，
# 避免生产日志被测试输出污染；生产入口 main() 再挂 FileHandler（见 _mount_file_logger）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)

class _LarkReconnectNoiseFilter(logging.Filter):
    """飞书长连接会被服务端定期关闭（约 20~45 分钟一次），SDK 内部自动重连，属正常现象；
    但 SDK 把这类断线日志打在 ERROR 级，刷屏误报。这里直接丢弃这两类已知噪音，
    "connect failed" 保留 ERROR（连续出现说明网络/凭证真的有问题）。"""
    NOISE = ("receive message loop exit", "ping failed")
    def filter(self, record):
        return not any(s in record.getMessage() for s in self.NOISE)

logging.getLogger("Lark").addFilter(_LarkReconnectNoiseFilter())

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
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(method, path, body=None):
    req = urllib.request.Request(
        BOARD_API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Actor": "dispatcher"},
    )
    with _opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())


def api_board(method, path, body=None):
    """看板顶层 API（agent_sessions / manual_commands 等），纯本机回环，失败由调用方容错。
    #431：带 X-Actor: dispatcher —— 看板据此把调度器自建的注册行 origin 落为 dispatcher"""
    req = urllib.request.Request(
        BOARD_ROOT_API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Actor": "dispatcher"},
    )
    with _opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())

def _parse_ts(s):
    """看板时间戳 'YYYY-MM-DD HH:MM:SS'（存储时区）-> epoch；解析失败返回 0（视为足够旧，可捞取）
    注意：失败返回 0 的宽松语义只适用于「捞取」判断，删除路径不得复用（见 _session_age_sec）。"""
    try:
        return parse_storage_time(s or "").timestamp()
    except Exception:
        return 0


def _session_age_sec(created_at):
    """#431：注册行 created_at（看板存储时区字符串）-> 距今秒数。
    缺失/非法/空返回 None —— 调用方（空会话清理）必须按 fail-closed 跳过，不得复用 _parse_ts
    （其失败返回 0 = 视为足够旧，用于删除会误删刚建的行）。"""
    try:
        ts = parse_storage_time(created_at or "").timestamp()
    except Exception:
        return None
    return time.time() - ts

def get_open_reqs(project):
    """open 需求中已过静默期（updated_at 距今 >= MIN_AGE_MINUTES）的才会被捞取。
    看板编辑弹窗每次自动保存都会刷新 updated_at，因此"还在写"的需求不会被扫走。"""
    cutoff = time.time() - MIN_AGE_MINUTES * 60
    return [r for r in api("GET", f"?project={project}&status=open")
            if _parse_ts(r.get("updated_at")) <= cutoff]

def get_doing_reqs(project):
    return api("GET", f"?project={project}&status=doing")

def fetch_req(req_id):
    """按 id 拉取最新需求；兼容字符串/整数入参（int 比较，避免类型不匹配静默漏配）"""
    for r in api("GET", ""):
        if int(r["id"]) == int(req_id):
            return r
    return None

def set_status(req_id, status):
    """状态变更（api PATCH 薄封装）。#400·v2：终态（done/later）清 confirm_rounds 键——
    需求 done/later 后人工重开是新周期，追问护栏从头计数（「响应不清零」语义保留在周期内）。
    #412（S1）：pop 取值在锁内、save_state 移锁外——旧实现在持 state_lock 时调 save_state
    （内部再次取非重入 state_lock）恒死锁（#410 现场根因：done/later 且 confirm_rounds 有键）。"""
    if status in ("done", "later"):
        with state_lock:
            popped = state.get("confirm_rounds", {}).pop(str(req_id), None)
        if popped is not None:
            save_state()
    return api("PATCH", f"/{req_id}", {"status": status})

def set_req(req_id, fields):
    return api("PATCH", f"/{req_id}", fields)

def append_summary(req_id, line):
    """往需求的 summary（开发总结）字段追加一行 [auto] 记录"""
    r = fetch_req(req_id)
    if r:
        summary = ((r.get("summary") or "") + f"\n[auto] {line}").strip()
        api("PATCH", f"/{req_id}", {"summary": summary})


# C4 summary 进 prompt 的折叠口径：保留末尾 N 条 [auto] 记录全文，更早的折成一行摘要
SUMMARY_PROMPT_KEEP = 6


def compact_summary(summary, keep=None):
    """C4：summary（prompt 通道）进 prompt 前折叠早期记录，防打回轮次越多 prompt 越长。
    按 [auto] 记录分组（多行记录整体保留）；首段人工前言与含「人工」的记录（答复/裁决/否决）
    永不折叠——它们是已澄清的事实，折掉会让 agent 重复追问；其余只留末尾 keep 条。
    人工裁决类记录有「人工…」与「用户…」（用户否决方案/评审/豁免等）两种写法，均按人工记录钉住。"""
    text = (summary or "").strip()
    keep = SUMMARY_PROMPT_KEEP if keep is None else keep
    preface, entries = [], []
    for line in text.splitlines():
        if line.startswith("[auto] "):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            preface.append(line)
    if len(entries) <= keep:
        return text
    older, recent = entries[:-keep], entries[-keep:]
    human = lambda e: "人工" in e[0] or e[0].startswith("[auto] 用户")
    pinned = [e for e in older if human(e)]
    folded = [e for e in older if not human(e)]
    if not folded:
        return text
    rejects = sum(1 for e in folded if "未通过" in e[0])
    digest = f"（更早 {len(folded)} 条记录已折叠，其中评审打回 {rejects} 次；完整历史见看板卡片开发总结）"
    out = preface + [digest] + [ln for e in pinned + recent for ln in e]
    return "\n".join(out).strip()


def confirm_answer_summary(question, answer, limit=120):
    """人工答复 need_confirm 的留档文本（写入 summary → summary 是 prompt 通道，
    会话重开/重试后的新 agent 也能看到这条澄清，不再重复追问）。

    与另几条人工分支（裁决放行 / 否决 / 豁免）的留档口径一致——原先只有「普通提问的答复」
    漏了留档：答复只进会话上下文，会话一重开就丢。问题文本压成单行并截断到 limit 字。"""
    q = " ".join(str(question or "").split())
    if len(q) > limit:
        q = q[:limit] + "…"
    return f"人工答复（问题：{q}）：{answer or '同意'}。"


# 单行超过该长度才考虑按要点拆分（短行/短原因不拆，宁缺毋滥）
SUMMARY_LONG_LINE_MIN = 120
# 拆后行数上限（含引导行），超限时超出部分并入最后一段（不丢原文）
SUMMARY_MAX_POINTS = 20

# 内联编号标记：1) 1） 1、 1. （英文句点必须后跟空白，防命中 2.5/1.2.3）；
# 前缀白名单=段首/空白/句读标点——编号只出现在条目边界；值内数字一律不算编号：
# rgb(240,253,244) 的 `244)`、OK(skipped=17) 的 `17)`、PORT=8901) 的 `8901)` 前字符是
# `,`/`(`/`=`，误判会把内容切开并重新编号（#428 曾把验收色值切成 `1. /(21,128,`）
_NUM_MARK_RE = re.compile(r"(?:^|(?<=[\s：:；;。！？!?]))(\d{1,3})([)）、]|\.\s)")
# 断句标点：中文 。；！？ 任意位置可断；英文 . ; ! ? 仅后跟空白或行尾时断
# （防 transition .2s、README.md、REQ_BOARD_PORT=8901 误拆）
_SENT_SPLIT_RE = re.compile(r"[。；！？]|[.;!?](?=\s|$)")


def format_summary_points(text):
    """把 agent 提供的 summary/原因长文本按要点分行（`- ` 列表 / `1. ` 编号），返回格式化字符串。

    规则（宁缺毋滥，任何输入都不丢原文）：
    - None/空白 → ""；短单行/短段（≤ SUMMARY_LONG_LINE_MIN）→ 原样返回；
    - 多行文本按 \n 逐段独立处理：空行原样保留、段首尾空白 strip；
    - 超长段含 ≥2 个内联编号（1) 1、 1. 等）→ 拆为引导行 + `1. ` 重新编号行；
    - 超长段无编号但含 ≥2 个断句标点 → 拆为 `- ` 列表行（标点保留行尾）；
    - 段首已有单个 `- ` 前缀在拆分时先剥离，避免 `- - ` 双前缀；
    - 行数上限（SUMMARY_MAX_POINTS，含引导行）按段独立，超限并入最后一段；
    - 其余（超长但拆分点不足 2 个）→ 原样返回。
    """
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""
    out = []
    for seg in text.split("\n"):
        stripped = seg.strip()
        if not stripped:
            out.append("")                # 空行保留
            continue
        if len(stripped) <= SUMMARY_LONG_LINE_MIN:
            out.append(stripped)          # 短段原样
            continue
        out.append(_format_long_segment(stripped))
    return "\n".join(out)


def _format_long_segment(seg):
    """超长单段拆分（seg 已 strip 且 > SUMMARY_LONG_LINE_MIN）。段首单个 `- ` 前缀先剥离。"""
    work = seg[2:] if seg.startswith("- ") else seg
    if len(work) <= SUMMARY_LONG_LINE_MIN:
        return seg                        # 剥前缀后不超长 → 原样（含前缀）

    # 编号拆分：首个编号标记前的文本为引导行（可空），之后按标记切分并重新编号
    marks = list(_NUM_MARK_RE.finditer(work))
    if len(marks) >= 2:
        head = work[:marks[0].start()].strip()
        segs = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(work)
            body = work[m.end():end].strip()
            if body:
                segs.append(body)
        lines = ([head] if head else [])
        limit = SUMMARY_MAX_POINTS - len(lines)  # 编号行数上限
        if len(segs) > limit:
            # 超限：超出部分并入最后一个编号段（不丢内容）
            segs = segs[:limit - 1] + [" ".join(segs[limit - 1:])]
        lines += ["%d. %s" % (i + 1, s) for i, s in enumerate(segs)]
        return "\n".join(lines)

    # 断句拆分：标点保留行尾，全部输出为 `- ` 列表行
    split_pts = list(_SENT_SPLIT_RE.finditer(work))
    if len(split_pts) < 2:
        return seg  # 拆分点不足 2 个，不拆
    parts = []
    start = 0
    for m in split_pts:
        seg_pt = work[start:m.end()].strip()
        if seg_pt:
            parts.append(seg_pt)
        start = m.end()
    tail = work[start:].strip()
    if tail:
        parts.append(tail)
    if len(parts) > SUMMARY_MAX_POINTS:
        parts = parts[:SUMMARY_MAX_POINTS - 1] + ["".join(parts[SUMMARY_MAX_POINTS - 1:])]
    return "\n".join("- %s" % p for p in parts)

def post_event(req_id, kind, content, session_id=None):
    """上报一条执行过程事件到看板（kind: message/tool/system）；session_id 标记归属会话，
    看板按会话展示执行过程（历史事件无标记，session_id 为 None 时归入全量兼容展示）"""
    try:
        api("POST", f"/{req_id}/events", {"events": [{"kind": kind, "content": content, "session_id": session_id}]})
    except Exception as e:
        log.warning(f"需求 #{req_id} 事件上报失败: {e}")

def _redline_cancel_reason(req_id):
    """检测需求最近的 dev 红线取消信号（#113）。命中返回事件完整 content（含命中原因），
    未命中/查询异常返回 None。事件接口 board.py GET /<id>/events 已实现，走 HTTP API 不直读
    SQLite；异常（看板未起/超时/重启窗口）由 try/except 兜住退化现有文案。
    匹配过窄可退化，过宽会误导续跑 agent 跳过真实原因自查 → 宁可漏判不可误判。"""
    try:
        events = api("GET", f"/{req_id}/events")
    except Exception:
        return None
    for e in events or []:
        content = e.get("content") or ""
        if content.startswith(REDLINE_CANCEL_SIGNAL):
            return content
    return None

def _rate_limit_failure_reason(req_id):
    """检测会话收尾的 API 并发限流（403）信号（#345）。命中返回限流归因文案，未命中/日志
    不可读返回 None。错误文本来源：ACP stderr 重定向 logf = RATE_LIMIT_LOG_FMT（Popen
    stderr=logf），限流发生在进程终止前，读日志尾部即可命中。匹配过窄可退化，过宽会误导
    归类与续跑 agent → 宁可漏判不可误判（仿 _redline_cancel_reason）。"""
    try:
        with open(RATE_LIMIT_LOG_FMT.format(req_id=req_id), "rb") as f:
            tail = f.read()[-RATE_LIMIT_TAIL_BYTES:].decode("utf-8", errors="replace")
    except Exception:
        return None
    if "concurrent request limit" in tail:
        return "API 并发限流（403：并发位被占），请求被拒绝，会话被终止"
    if re.search(r"\b403\b", tail):
        return "API 并发限流（403：请求被拒绝），并发位被占，会话被终止"
    return None

def _model_unavailable_reason(req_id):
    """#11 运行期模型不可用归因（与 _rate_limit_failure_reason 同源：读 ACP stderr 尾部）。
    窄特征、宁可漏判不可误判：欠费/额度（insufficient|quota|balance）、模型下线
    （model not found|not available|decommission）、尾部连续 ≥3 次 5xx。未命中/日志不可读 → None。"""
    try:
        with open(RATE_LIMIT_LOG_FMT.format(req_id=req_id), "rb") as f:
            tail = f.read()[-RATE_LIMIT_TAIL_BYTES:].decode("utf-8", errors="replace")
    except Exception:
        return None
    low = tail.lower()
    if re.search(r"insufficient|quota|balance", low):
        return "模型额度/欠费不可用（insufficient/quota/balance），会话被终止"
    if re.search(r"model not found|not available|decommission", low):
        return "模型已下线/不可用（model not found / not available / decommission）"
    if len(re.findall(r"\b(?:500|502|503|504)\b", tail)) >= 3:
        return "模型服务连续 5xx（≥3 次），会话被终止"
    return None

def _note_model_fallback(req_id, role, *, source=None, requested=None, configured=None,
                         add_role=False, fact=None):
    """#11 记录本轮模型降级事实（state["model_fallback"][req_id]）供收尾汇总与运行期兜底判定。
    与 state["retries"] 完全解耦（handle_result 整条覆盖 retries 不会清掉本键）。幂等追加；
    `used` 由 handle_result 置位后本函数保持不复位（兜底轮只允许一次）。"""
    with state_lock:
        fb = state.setdefault("model_fallback", {})
        entry = fb.get(req_id) or {}
        roles = list(entry.get("degraded_roles") or [])
        facts = dict(entry.get("degraded_facts") or {})
        if add_role and role not in roles:
            roles.append(role)
        if fact:
            facts[role] = fact
        entry.update({"role": role,
                      "source": source if source is not None else entry.get("source"),
                      "requested": requested if requested is not None else entry.get("requested"),
                      "configured": configured if configured is not None else entry.get("configured"),
                      "used": bool(entry.get("used")),
                      "degraded_roles": roles, "degraded_facts": facts})
        fb[req_id] = entry
    # 不在此落盘：本记录只在同进程内被 handle_result 读取（重启后下一轮 run_agent 会重写），
    # 避免为一次无改写价值的写入触发 state 落盘（收尾汇总路径按需 save_state）

def _flush_model_degraded_summary(req, role):
    """#11 本轮降级角色事实汇总写入需求 summary（一行一角色，按「prompt 通道约定」只写事实），
    写后原地清空 degraded_roles 防跨轮重复；无降级时零行为。"""
    req_id = str(req["id"])
    with state_lock:
        entry = (state.get("model_fallback") or {}).get(req_id) or {}
        roles = list(entry.get("degraded_roles") or [])
        if roles:
            facts = dict(entry.get("degraded_facts") or {})
            entry["degraded_roles"] = []
            state["model_fallback"][req_id] = entry
    if not roles:
        return
    save_state()
    lines = [facts.get(r) or f"{r} 模型降级（详情见时间线）" for r in roles]
    append_summary(req_id, "模型降级：" + "；".join(lines))

def post_model_events(req, role, session, resolved):
    """#11 建会话后模型留痕（时间线 kind="model"）：降级一条 + 未落定一条（各自独立判定）。
    失败仅降级不阻断会话。"""
    req_id = str(req["id"])
    rlabel = ROLE_NAMES.get(role, role)
    requested = getattr(session, "model_requested", None) or getattr(session, "model", None)
    effective = getattr(session, "model_effective", None) or requested
    try:
        if resolved.get("degraded"):
            # #13：文案扩为「配置 X → 请求 Y → 实际 Z」；旧关键串（模型降级 / 配置值 xxx）保留
            post_dispatcher_event(int(req_id),
                                  f"模型降级：{rlabel} 配置 {resolved.get('configured')} → "
                                  f"请求 {requested} → 实际 {effective}（来源 {resolved.get('source')}，"
                                  f"原因 {resolved.get('reason') or '运行期模型不可用'}）",
                                  kind="model")
        settle = getattr(session, "model_settle", None)
        if settle and settle != "settled":
            merr = getattr(session, "model_error", None)
            tail = f"，原因 {str(merr)[:200]}" if merr else ""
            post_dispatcher_event(int(req_id),
                                  f"模型未落定：{rlabel} 请求 {requested}，回读 {effective}"
                                  f"（{settle}），会话继续{tail}", kind="model")
    except Exception as e:
        log.warning(f"需求 #{req_id} 模型留痕事件发送失败（不阻断会话）: {e}")

def post_run_record(req, role, session, started_at, ended_at, returncode, timed_out, usage=None,
                    aborted=False, usage_ext=None):
    """会话收尾写一条运行记录到看板 agent_runs（报表基础数据源，只增不改）+ 一条明确的结束事件。
    status 按进程级判定：done / timed_out / failed / aborted（#28 输出守卫中止）；usage 为
    collect_usage 的 {context_tokens, max_tokens, context_pct}（超时/中止/异常路径为 None，
    token 字段置空）。#42：usage_ext = ACPSession.cache_summary() 结果（{state, cached_tokens,
    input_tokens, raw}）——cache_state 只由 usage_ext['state'] 决定，与峰值 usage 是否 None 解耦
    （峰值解析失败不得把已返回的缓存字段记成 na）；usage is None 仅保持既有峰值三字段 NULL 语义；
    na 时缓存 token 由采集端构造即为 NULL。写库失败仅警告不阻断收尾（对齐 post_event 容错）；
    sessions 状态文件不动，历史数据不迁移"""
    req_id = str(req["id"])
    sid = getattr(session, "session_id", None) if session else None
    if aborted:
        status = "aborted"
    elif timed_out:
        status = "timed_out"
    elif returncode == 0:
        status = "done"
    else:
        status = "failed"
    ext = usage_ext if isinstance(usage_ext, dict) else {}
    try:
        api("POST", f"/{req_id}/runs", {
            "role": role, "session_id": sid,
            "started_at": started_at, "ended_at": ended_at,
            "status": status, "tool_count": len(session._tool_calls) if session else 0,
            "context_pct": pct_value(usage["context_pct"]) if usage else None,
            "context_tokens": usage["context_tokens"] if usage else None,
            "max_tokens": usage["max_tokens"] if usage else None,
            # #42 缓存命中指标（hit=真实返回；unknown=完成请求但供应商未返回；na=零完成请求）
            "cached_tokens": ext.get("cached_tokens"),
            "input_tokens": ext.get("input_tokens"),
            "cache_state": ext.get("state"),
            "usage_raw": ext.get("raw"),
        })
    except Exception as e:
        log.warning(f"需求 #{req_id} 运行记录写入失败: {e}")
    post_event(req_id, "system", f"运行结束（{status}）", session_id=sid)

def post_dispatcher_event(req_id, content, kind="action", session_id=None):
    """记录一条调度器自身动作到看板（需求级时间线，供"调度历史回放"展示）。
    返回新记录 id（供会话建立后回填 session_id），失败返回 None"""
    try:
        resp = api("POST", f"/{req_id}/dispatcher-events",
                   {"events": [{"kind": kind, "content": content, "session_id": session_id}]})
        return resp.get("id") if isinstance(resp, dict) else None
    except Exception as e:
        log.warning(f"需求 #{req_id} 调度器事件上报失败: {e}")
        return None

def backfill_spawn_session(req_id, event_id, session_id):
    """会话建立后按 spawn 记录 id 回填 session_id；失败仅降级不阻断（双端不同步上线安全）"""
    if not event_id or not session_id:
        return
    try:
        api("PATCH", f"/{req_id}/dispatcher-events/{event_id}", {"session_id": session_id})
    except Exception as e:
        log.warning(f"需求 #{req_id} spawn 记录回填 session_id 失败: {e}")

def owner_key(req):
    """会话归属键 = 需求 id（需求平级独立，无会话组概念）"""
    return str(req["id"])

def session_key(req, role=None):
    """会话归属键按角色区分：analysis:<id> / proposal:<id> / tech:<id> / dev:<id> / accept:<id>。
    各阶段互不覆盖且全程保留（供看板按阶段展示）；重试续跑同一角色仍命中同一键"""
    if not role:
        return str(req["id"])
    return f"{role}:{req['id']}"

def session_id_of(entry):
    """sessions 记录取 session_id：旧格式是纯 id 字符串，新格式是信息对象 {id, model, ...}"""
    return entry.get("id") if isinstance(entry, dict) else entry

def kimi_version():
    """抓取当前 kimi code 版本号（如 '0.33.0'），供会话记录的 harness 字段使用。
    每次调用实时执行 KIMI --version，不做调度器启动时一次性缓存——保证语义是"该会话实际跑的 kimi 版本"。
    失败/超时/输出为空返回 None，调用方忽略即可，不阻断会话建立"""
    try:
        out = subprocess.run([KIMI, "--version"], capture_output=True, text=True, timeout=10)
        text = (out.stdout or out.stderr or "").strip()
        return text.splitlines()[0] if text else None
    except Exception:
        return None

def update_session_info(req, key=None, **fields):
    """更新该需求会话归属键下的 ACP 会话信息（id/model/thinking/context/branch/workdir/harness）。
    无记录且未给 id 时不创建；空值字段忽略"""
    k = key or owner_key(req)
    with state_lock:
        sessions = state.setdefault("sessions", {})
        entry = sessions.get(k)
        if entry is None and not fields.get("id"):
            return
        if not isinstance(entry, dict):
            entry = {"id": entry} if entry else {}
        entry.update({kk: vv for kk, vv in fields.items() if vv})
        sessions[k] = entry
    save_state()

def sync_agent_session(req, role, session_id=None, row_id=None, **fields):
    """同步 ACP 会话注册表（agent_sessions）：调度器写入，失败仅降级不阻断。
    返回看板侧行 id（供后续按 id 更新）；无法写入时返回 None。"""
    body = {"req_id": req["id"], "role": role}
    if row_id:
        body["id"] = row_id
    if session_id:
        body["session_id"] = session_id
    body.update({k: v for k, v in fields.items() if v is not None})
    try:
        resp = api_board("POST", "/agent-sessions", body)
        return (resp or {}).get("id")
    except Exception as e:
        log.warning(f"需求 #{req['id']} agent_sessions 同步失败: {e}")
        return None


def _touch_session_activity(req_id, role, holder, kind, detail=None):
    """#421 会话活性心跳：ACPSession 状态切换时按注册表行 id 更新（失败仅告警，不阻断会话）。
    holder["row_id"] 为 None（sync_agent_session 写失败）→ 跳过并只告警一次，避免每轮刷 400 噪声。"""
    row_id = (holder or {}).get("row_id")
    if not row_id:
        if holder is not None and not holder.get("warned"):
            holder["warned"] = True
            log.warning(f"需求 #{req_id} 会话活性心跳跳过（无注册表行 id，role={role}）")
        return
    now = round(time.time(), 3)
    body = {"id": row_id}
    if kind == "llm_request":
        body.update(turn_step="llm", last_llm_request_at=now)
    elif kind == "llm_thought":
        body["last_thought_at"] = now
    elif kind == "llm_response":
        body["last_llm_response_at"] = now
    elif kind == "tool_start":
        body.update(turn_step="tool", last_tool_started_at=now,
                    turn_step_detail=(" ".join(str(detail or "").split())[:48] or None))
    elif kind == "tool_end":
        # 在途工具清空 = 下一次模型请求的近似起点（ACP 无原生通知）
        body.update(turn_step="llm", last_llm_request_at=now)
    else:
        return
    try:
        api_board("POST", "/agent-sessions", body)
    except Exception as e:
        log.warning(f"需求 #{req_id} 会话活性心跳写入失败（row={row_id}, {kind}）: {e}")


def _close_session_row(req, role, row_id, status, **fields):
    """#421：把注册表行置 idle/interrupted 的统一入口（附带 turn_step='' 清理；
    只为证据整洁，前端正确性不依赖它）。失败仅降级。"""
    if row_id is None:
        return
    try:
        sync_agent_session(req, role, row_id=row_id, status=status, turn_step="", **fields)
    except Exception as e:
        log.warning(f"需求 #{req.get('id')} 会话行置 {status} 失败（row={row_id}）: {e}")


def mark_session_poisoned(req_id, role=None):
    """重试耗尽：把会话标记为毒（#273 人工接管）——不再删记录，自动路径跳过，人工可在 UI 继续。
    同时同步 agent_sessions 注册表，供中间栏展示「已停止自动重试」徽标。"""
    with state_lock:
        sessions = state.get("sessions", {})
        keys = [str(req_id)]
        if role:
            keys.append(session_key({"id": req_id}, role))
        for k in keys:
            entry = sessions.get(k)
            if isinstance(entry, dict):
                entry["poisoned"] = True
                sid = entry.get("id")
            elif entry:
                sessions[k] = {"id": entry, "poisoned": True}
                sid = entry
            else:
                sid = None
            if sid and role:
                try:
                    api_board("POST", "/agent-sessions",
                              {"req_id": req_id, "role": role, "session_id": sid, "poisoned": 1})
                except Exception as e:
                    log.warning(f"需求 #{req_id} 毒会话标记同步失败: {e}")
    save_state()

def find_resume_sid(req, role=None):
    """查该需求应恢复的 ACP session_id（阶段内重试/need_confirm 续跑）。
    需求平级独立：只按需求 id 键（analysis 用独立键）查询；阶段推进时会话已被丢弃，自然查不到；
    #273：标记为毒（poisoned）的会话不参与自动 resume，仅人工可继续。"""
    sessions = state.get("sessions", {})
    entry = sessions.get(session_key(req, role))
    if isinstance(entry, dict) and entry.get("poisoned"):
        return None
    return session_id_of(entry)

# ==================== 角色与提示词 ====================
def workdir_for(project):
    """单 paths 项目直连仓库本体；多 paths 项目用聚合目录"""
    cfg = PROJECTS[project]
    if len(cfg["paths"]) == 1:
        return Path(cfg["paths"][0])
    return WORKTREES_ROOT / project

def artifact_dir(req):
    """评审/开发产物目录：统一在 ~/.req-board/artifacts/<project>/req-<id>/（不入任何仓库/worktree）"""
    return ARTIFACTS_ROOT / req["project"] / f"req-{req['id']}"

def result_file_path(req):
    """结果 JSON 按需求隔离（并发 agent 互不覆盖）：产物目录 result.json"""
    return artifact_dir(req) / "result.json"

def parse_notes_repos(notes):
    """从需求 notes 提取人工声明的涉及仓库清单：`repos: a, b` 行（逗号/空白分隔），
    兼容 JSON 数组写法 `repos: ["a","b"]`。无声明返回 []。人工声明覆盖 analysis 识别。"""
    for line in (notes or "").splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("repos:"):
            continue
        value = stripped[len("repos:"):].strip()
        if not value:
            continue
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in re.split(r"[,，\s]+", value) if x.strip()]
    return []

def validate_repos(project, names):
    """过滤出属于项目 paths 的仓库名（basename 匹配），非法名忽略并警告；去重保序。"""
    valid = {Path(p).name for p in PROJECTS[project]["paths"]}
    out = []
    seen = set()
    for n in (names or []):
        n = str(n).strip()
        if not n or n in seen:
            continue
        if n not in valid:
            log.warning(f"项目 {project} 涉及仓库名 {n!r} 不在 paths 清单中，已忽略")
            continue
        seen.add(n)
        out.append(n)
    return out

def outside_repos(project, names):
    """#303 防复发：声明/识别的涉及仓库名中不在项目 paths 清单内的部分（目录名比对，去重保序）。
    清单外名字 = 疑似建错项目（需求实际属于其它项目）或项目仓库清单缺配置的信号，
    供 analysis→dev 闸门拦截（免确认 auto 也转通道确认让人工把关）。"""
    valid = {Path(p).name for p in PROJECTS[project]["paths"]}
    out, seen = [], set()
    for n in (names or []):
        n = str(n).strip()
        if n and n not in valid and n not in seen:
            seen.add(n)
            out.append(n)
    return out

def effective_repos(req):
    """需求实际生效的涉及仓库清单（目录名列表）：notes 人工声明 > analysis 产物 > []（全量兜底）。"""
    notes_repos = parse_notes_repos(req.get("notes"))
    if notes_repos:
        return validate_repos(req["project"], notes_repos)
    return list(state.get("req_repos", {}).get(str(req["id"]), []) or [])

# TAPD 链接形态（stories 与 bugs 两种）：https://www.tapd.cn/<workspace>/prong/stories/view/<id>
# 或 https://www.tapd.cn/<workspace>/bugtrace/bugs/view/<id>；提取末尾的需求/缺陷 ID
_TAPD_ID_RE = re.compile(r"tapd\.cn/\d+/(?:prong/stories/view|bugtrace/bugs/view)/(\d+)")

def tapd_id_from_notes(req):
    """从需求 notes 提取 TAPD 需求/缺陷 ID（Bug转需求类需求建卡时 notes 常带 TAPD 链接）；
    无链接返回 None。"""
    m = _TAPD_ID_RE.search(req.get("notes") or "")
    return m.group(1) if m else None

def dev_branch(req):
    """开发分支名（命名规范）：f-YYYYMMDD-<req_id>，日期取需求创建日；
    需求来自 TAPD（notes 含 TAPD 链接）时追加 -tapd<需求ID>，便于分支与 TAPD 需求对应。"""
    d = str(req.get("created_at") or "")[:10].replace("-", "")
    if not d:
        d = format_storage_time()[:10].replace("-", "")
    branch = f"f-{d}-{req['id']}"
    tid = tapd_id_from_notes(req)
    if tid:
        branch += f"-tapd{tid}"
    return branch

def dev_worktree_path(req, repo):
    """单个仓库的 dev worktree 路径"""
    return WORKTREES_ROOT / req["project"] / "dev" / f"req-{req['id']}" / Path(repo).name

# #420：测试进程短路判据的基准值（import 时捕获，与 scripts/test_isolation_audit.py 判据一致）。
# 用例把 WORKTREES_ROOT / dev_worktree_path 指到临时目录即视为「已隔离」→ 照走真实 git 逻辑
# （test_worktree_base_stability / test_worktree_local_config 是真实分支/基线语义的回归锁）。
# 判据用函数身份比较（is）：将来若包装/装饰 dev_worktree_path 必须同步这里，
# 否则短路静默失效（方向是回到真跑 git）。
_REAL_WORKTREES_ROOT = WORKTREES_ROOT
_REAL_DEV_WORKTREE_PATH = dev_worktree_path


def in_test_env():
    """#420：测试进程判定——REQ_BOARD_TEST=1（显式逃生/脚本用）或 unittest 已加载（覆盖全部套件）。
    生产进程两者皆假 → 恒 False（tests/test_state_isolation_guard.py T9 用真实 venv 子进程锁定）。"""
    return os.environ.get("REQ_BOARD_TEST") == "1" or "unittest" in sys.modules

def _default_branch(repo, cfg=None):
    """仓库主干起点探测（离线可用）：projects.json 项目可选配置 base_branch 优先
    （如某项目的开发主线 f-20260303-main——本地 master 落后主线上百个
    提交，走默认探测会拿到过时基线）→ 本地 master → main → origin/HEAD 短名 →
    当前 HEAD。base_branch 支持字符串（适用于该项目全部仓库）或 {仓库绝对路径: 分支}
    dict；配置的分支在仓库内不存在时回退默认探测并告警（fail-open）。
    返回 (起点引用, 起点 commit sha)；起点为 None 时由 git 默认取当前 HEAD（兜底，不传起点）。
    基线一致性的核心：dev worktree 分支从主干拉取，调研/开发基线不随主工作区 checkout 漂移。"""
    configured = None
    if isinstance(cfg, dict):
        configured = cfg.get("base_branch")
        if isinstance(configured, dict):
            configured = configured.get(str(repo))
    if configured:
        r = subprocess.run(["git", "rev-parse", "--verify", "-q", configured], cwd=repo,
                           capture_output=True, text=True, timeout=DIAG_TIMEOUT)
        if r.returncode == 0:
            return configured, r.stdout.strip()
        log.warning("base_branch 配置 %r 在 %s 不存在，回退默认分支探测", configured, repo)
    for cand in ("master", "main"):
        r = subprocess.run(["git", "rev-parse", "--verify", "-q", cand], cwd=repo,
                           capture_output=True, text=True, timeout=DIAG_TIMEOUT)
        if r.returncode == 0:
            return cand, r.stdout.strip()
    r = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                       cwd=repo, capture_output=True, text=True, timeout=DIAG_TIMEOUT)
    ref = r.stdout.strip() if r.returncode == 0 else ""
    if ref:
        r2 = subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=repo,
                            capture_output=True, text=True, timeout=DIAG_TIMEOUT)
        if r2.returncode == 0:
            return ref, r2.stdout.strip()
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                       timeout=DIAG_TIMEOUT)
    return None, (r.stdout.strip() if r.returncode == 0 else None)

def dev_workdir_for(req):
    """dev 角色工作目录：单仓库 = 该仓库 worktree；多仓库 = 各仓库 worktree 的聚合目录"""
    cfg = PROJECTS[req["project"]]
    if len(cfg["paths"]) == 1:
        return dev_worktree_path(req, cfg["paths"][0])
    return WORKTREES_ROOT / req["project"] / "dev" / f"req-{req['id']}"

def dev_write_roots(req):
    """#303 防复发：dispatcher 各角色的内置 Edit/Write 与 fs 写绝对路径白名单 =
    ① worktree 根（聚合目录或单仓库 worktree）② 各仓库 worktree 路径 ③ 产物目录
    ④ 临时目录（/tmp 与系统 TMPDIR）。主工作区（各仓库真实路径）不在其中 → 禁止写。"""
    roots = {Path(dev_workdir_for(req)).resolve(), Path(artifact_dir(req)).resolve(),
             Path("/tmp").resolve()}
    try:
        roots.add(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass
    with state_lock:
        rec = state.get("worktrees", {}).get(str(req["id"])) or {}
    for _, wt in rec.get("worktrees") or []:
        roots.add(Path(wt).resolve())
    return sorted(roots, key=str)

def _link_repo_local_config(wt: Path) -> None:
    """worktree 内补 projects.json（本地数据、gitignore）：软链到主工作区那份，
    使 worktree 与 main 的项目清单语义一致。

    #419 合并带出的现场：不补的后果是 50 个测试文件自带的 bootstrap 会写入一份**只有 1 个项目**
    的最小清单——于是 PROJECTS 在 worktree=1、main=7，凡遍历 PROJECTS 的次数/列表断言两边结论
    不同且不报错（worktree 全绿、合并到 main 转红）。软链而非复制：复制会随主工作区改配置变旧，
    又制造新的隐性差异。

    幂等（已是正确软链则跳过；旧的最小清单/失效软链重建）；任何失败只告警不抛——补本地配置
    不得阻断 worktree 创建（fail-open）。判断存在性用 `is_symlink() or exists()`：断链软链的
    exists() 为 False，否则无法自愈。"""
    src = PROJECTS_FILE
    dst = wt / "projects.json"
    if not src.exists():
        log.warning(f"主工作区 projects.json 不存在，跳过 worktree 本地配置补链: {wt}")
        return
    try:
        if dst.is_symlink():
            if Path(os.readlink(dst)) == src:
                return
            dst.unlink()
        elif dst.exists():
            log.warning(f"worktree 内 projects.json 为普通文件（历史最小清单），替换为主工作区软链: {dst}")
            dst.unlink()
        dst.symlink_to(src)
    except OSError as e:
        log.warning(f"补链 worktree projects.json 失败（跳过，不影响创建）: {dst}: {e}")


# ==================== 项目级 Git 同步/推送（git_sync v1） ====================
# 职责：按项目 git_sync 配置在主工作区做 fetch/快进/分叉判定（before_card + 收尾前），
# 以及 done 合并后按 on_done 推送主干（auto）或只统计待推送（manual）。
# 硬约束：全部 git 远端操作走 git_ops（git_env 禁交互 + repo_lock 跨进程串行 + run_git）；
# 未配置 git_sync（None）时本段所有入口零动作（不发任何 git 命令），既有行为逐字不变；
# 任何失败只 log.warning + post_dispatcher_event(kind=merge)，绝不抛、不阻断需求推进。

def _project_git_cfg(project):
    """项目 git 段（归一后 dict）或 None。"""
    cfg = (PROJECTS.get(project) or {}).get("git")
    return cfg if isinstance(cfg, dict) else None


def _project_git_sync(project):
    """项目 git_sync 段（归一后 dict）或 None。None = 未配置 = 零行为变化。"""
    cfg = (PROJECTS.get(project) or {}).get("git_sync")
    return cfg if isinstance(cfg, dict) else None


def _git_sync_remote(project):
    return str((_project_git_cfg(project) or {}).get("remote") or "origin")


def _git_sync_timeout(sync):
    try:
        timeout = int((sync or {}).get("timeout") or GIT_SYNC_DEFAULTS["timeout"])
    except (TypeError, ValueError):
        timeout = GIT_SYNC_DEFAULTS["timeout"]
    return timeout if timeout > 0 else GIT_SYNC_DEFAULTS["timeout"]


def _repo_paths(req):
    cfg = PROJECTS.get(req.get("project") or "") or {}
    return [str(p) for p in (cfg.get("paths") or []) if p]


def _git_err(err, limit=200):
    """git stderr → 单行摘要（事件/日志用，不泄露凭据：run_git 的 stderr 只含 git 自身输出）。"""
    lines = [ln.strip() for ln in str(err or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "未知错误")[:limit]


def _git_sync_event(req, content):
    """git_sync 失败/需人工留痕：warn + 时间线事件；上报失败不阻断（post_dispatcher_event 已兜底）。"""
    log.warning("需求 #%s git_sync：%s", req.get("id"), content)
    try:
        post_dispatcher_event(int(req["id"]), content, kind="merge")
    except Exception:  # noqa: BLE001 - 时间线留痕失败不得阻断收尾
        pass


def _local_base_branch(repo, cfg):
    """主干分支的本地名 + 起点 sha。_default_branch 可能返回 origin/<branch>（本地主干缺失时
    的远端探测结果），这里剥掉远端前缀取本地分支名——后续 merge/push 都用本地名。"""
    ref, sha = _default_branch(repo, cfg)
    if not ref:
        return None, sha
    if "/" in ref:
        head, tail = ref.split("/", 1)
        try:
            if head in git_ops.remote_names(repo):
                return tail, sha
        except Exception:  # noqa: BLE001 - 远端探测失败按原样使用，不阻断
            pass
    return ref, sha


def _sync_main_worktrees(req, sync, mode=None):
    """主工作区同步（fetch / pull 两档）。返回汇总 dict（测试与调用方只读）：

    {"mode": 实际档位, "fetched": [仓库名], "fast_forwarded": [仓库名],
     "merged": [仓库名]（分叉 overlap 空→自动合并成功）, "diverged": [仓库名],
     "skipped": [仓库名], "failed": [仓库名]}

    规则（逐仓库、with repo_lock 串行）：
    1) git fetch --prune <remote>；
    2) mode=fetch 到此为止（只更新远端引用，不动工作区）；
    3) mode=pull：仅当「主工作区干净」且「当前分支 == 主干」才慢进，否则跳过（绝不 stash/切分支）；
    4) 本地仅落后 → git merge --ff-only <remote>/<base>；
    5) 分叉（本地领先+落后）：diverge=ff-only 跳过；stop 报事件转人工；overlap-check 用
       merge-base 两侧 diff --name-only 求交集，空 → merge --no-edit <remote>/<base>，
       非空 → 跳过 + 事件转人工（不制造半自动冲突现场）。
    所有异常/失败只 warn+事件，返回 failed，不抛。"""
    mode = mode or (sync or {}).get("before_card") or "off"
    result = {"mode": mode, "fetched": [], "fast_forwarded": [], "merged": [],
              "diverged": [], "skipped": [], "failed": []}
    if mode not in ("fetch", "pull"):
        return result
    sync = sync or {}
    for repo in _repo_paths(req):
        try:
            _sync_one_main_repo(req, repo, sync, mode, result)
        except Exception as e:  # noqa: BLE001 - 单个仓库任何异常都不得阻断需求
            name = Path(repo).name
            result["failed"].append(name)
            _git_sync_event(req, f"仓库 {name} 同步异常（{e}），跳过本次自动同步，不影响需求推进")
    return result


def _sync_one_main_repo(req, repo, sync, mode, result):
    """单仓库主工作区同步（_sync_main_worktrees 的子步骤，异常由调用方兜底）。"""
    name = Path(repo).name
    project = req.get("project")
    remote = _git_sync_remote(project)
    timeout = _git_sync_timeout(sync)
    if not Path(repo).exists():
        log.warning("需求 #%s 仓库路径不存在，跳过 git_sync 同步：%s", req.get("id"), repo)
        result["skipped"].append(name)
        return
    if remote not in git_ops.remote_names(repo):
        # 本地纯项目属正常（无远端不改）；不算失败、不刷事件
        log.info("需求 #%s 仓库 %s 无远端 %s，跳过 git_sync 同步", req.get("id"), name, remote)
        result["skipped"].append(name)
        return
    with git_ops.git_env(project) as env:
        with git_ops.repo_lock(repo, timeout=timeout):
            rc, _out, err = git_ops.run_git(repo, ["fetch", "--prune", remote],
                                            env=env, timeout=timeout)
            if rc != 0:
                result["failed"].append(name)
                _git_sync_event(req, f"仓库 {name} fetch {remote} 失败（{_git_err(err)}），"
                                     f"跳过本次自动同步，不影响需求推进")
                return
            result["fetched"].append(name)
            if mode == "fetch":
                return
            _fast_forward_main_repo(req, repo, sync, remote, timeout, env, result, name)


def _fast_forward_main_repo(req, repo, sync, remote, timeout, env, result, name):
    """pull 档：干净 + 在主干才快进/按 diverge 处理分叉（_sync_main_worktrees 的子步骤）。"""
    cfg = PROJECTS.get(req.get("project") or "") or {}
    base, _ = _local_base_branch(repo, cfg)
    if not base:
        log.warning("需求 #%s 仓库 %s 主干探测失败，跳过快进", req.get("id"), name)
        result["skipped"].append(name)
        return
    current = git_ops.current_branch(repo)
    if current != base:
        # 不在主干（含 detached HEAD）：不切分支、不 stash，只跳过——主工作区现场由人工掌控
        log.info("需求 #%s 仓库 %s 主工作区当前分支 %r 不是主干 %r，跳过快进",
                 req.get("id"), name, current, base)
        result["skipped"].append(name)
        return
    st_rc, st_out, _st_err = git_ops.run_git(repo, ["status", "--porcelain"],
                                             env=env, timeout=timeout)
    if st_rc != 0 or st_out.strip():
        log.info("需求 #%s 仓库 %s 主工作区有未提交改动，跳过快进（不 stash、不改现场）",
                 req.get("id"), name)
        result["skipped"].append(name)
        return
    ahead, behind = git_ops.ahead_behind(repo, base, remote=remote, env=env)
    if behind is None or ahead is None:
        log.info("需求 #%s 仓库 %s 远端无 %s/%s 分支，无内容可快进",
                 req.get("id"), name, remote, base)
        result["skipped"].append(name)
        return
    if behind == 0:
        return  # 已最新（或仅本地领先，分叉在下面单独判定）
    if ahead == 0:
        rc, _out, err = git_ops.run_git(repo, ["merge", "--ff-only", f"{remote}/{base}"],
                                        env=env, timeout=timeout)
        if rc != 0:
            result["failed"].append(name)
            _git_sync_event(req, f"仓库 {name} 快进合并 {remote}/{base} 失败"
                                 f"（{_git_err(err)}），跳过本次自动同步，不影响需求推进")
        else:
            result["fast_forwarded"].append(name)
            log.info("需求 #%s 仓库 %s 主工作区已快进到 %s/%s", req.get("id"), name, remote, base)
        return
    # 分叉：本地领先 ahead、落后 behind
    diverge = sync.get("diverge") or GIT_SYNC_DEFAULTS["diverge"]
    if diverge == "ff-only":
        log.info("需求 #%s 仓库 %s 主工作区分叉（本地 %s / 远端 %s），diverge=ff-only 跳过",
                 req.get("id"), name, ahead, behind)
        result["diverged"].append(name)
        return
    if diverge == "stop":
        _git_sync_event(req, f"仓库 {name} 主工作区分叉（本地领先 {ahead}、落后 {behind}），"
                             f"按 diverge=stop 停止自动同步，转人工处理")
        result["diverged"].append(name)
        return
    # overlap-check：merge-base 两侧改动文件交集为空才允许自动 merge（避免替人做冲突决策）
    mb_rc, mb_out, mb_err = git_ops.run_git(repo, ["merge-base", "HEAD", f"{remote}/{base}"],
                                            env=env, timeout=timeout)
    merge_base = mb_out.strip()
    if mb_rc != 0 or not merge_base:
        result["failed"].append(name)
        _git_sync_event(req, f"仓库 {name} 分叉判定失败（merge-base 不可用：{_git_err(mb_err)}），"
                             f"跳过自动合并，不影响需求推进")
        return
    _lrc, local_out, _lerr = git_ops.run_git(
        repo, ["diff", "--name-only", f"{merge_base}..HEAD"], env=env, timeout=timeout)
    _rrc, remote_out, _rerr = git_ops.run_git(
        repo, ["diff", "--name-only", f"{merge_base}..{remote}/{base}"], env=env, timeout=timeout)
    local_files = {ln for ln in local_out.splitlines() if ln}
    remote_files = {ln for ln in remote_out.splitlines() if ln}
    overlap = sorted(local_files & remote_files)
    if overlap:
        sample = "、".join(overlap[:5]) + ("…" if len(overlap) > 5 else "")
        _git_sync_event(req, f"仓库 {name} 主工作区分叉（本地领先 {ahead}、落后 {behind}），"
                             f"与远端改动重叠文件 {sample}，跳过自动合并转人工处理")
        result["diverged"].append(name)
        return
    rc, _out, err = git_ops.run_git(repo, ["merge", "--no-edit", f"{remote}/{base}"],
                                    env=env, timeout=timeout)
    if rc != 0:
        git_ops.run_git(repo, ["merge", "--abort"], env=env, timeout=timeout)
        result["failed"].append(name)
        _git_sync_event(req, f"仓库 {name} 分叉自动合并 {remote}/{base} 失败"
                             f"（{_git_err(err)}），已 abort，跳过本次同步，不影响需求推进")
        return
    result["merged"].append(name)
    post_dispatcher_event(int(req["id"]),
                          f"仓库 {name} 主工作区分叉但改动文件无重叠，已自动合并 {remote}/{base}"
                          f"（本地领先 {ahead}、落后 {behind}）", kind="merge")


def _merge_preflight(req, repo):
    """git_sync 主干守卫（合并前校验）：主工作区当前分支必须是本项目主干 base。
    返回 None=可合并；str=不合并原因（走现有失败/人工通道，绝不在错分支上落合并）。
    fail-open：未配置 base_branch / 探测失败 / 探测异常一律返回 None（维持旧行为——守卫不得
    把原本能合并的卡拦死）；只有确实读到非主干分支才拦截。"""
    try:
        cfg = PROJECTS.get(req.get("project") or "") or {}
        base, _ = _local_base_branch(str(repo), cfg)
        if not base:
            return None
        current = git_ops.current_branch(str(repo))
        if not current:
            return f"主工作区分支不可读（detached HEAD），主干应为 {base}"
        if current != base:
            return f"主工作区当前分支 {current}，主干应为 {base}"
    except Exception as e:  # noqa: BLE001 - 守卫探测失败降级为不拦截（不阻断合并主流程）
        log.warning("需求 #%s 仓库 %s 主干守卫校验失败（按可合并继续）: %s", req.get("id"), repo, e)
    return None


def _merge_guard_blockers(req, failed):
    """合并失败仓库复核主干守卫：{仓库路径: 原因}；git_sync 未启用时恒 {}（失败路径零开销）。"""
    if not _project_git_sync(req.get("project")):
        return {}
    out = {}
    for repo in failed or []:
        try:
            why = _merge_preflight(req, repo)
        except Exception as e:  # noqa: BLE001 - 复核失败按非守卫原因处理（沿用旧文案）
            log.warning("需求 #%s 仓库 %s 主干守卫复核失败: %s", req.get("id"), repo, e)
            continue
        if why:
            out[str(repo)] = why
    return out


def _branch_guard_text(guard, limit=3):
    """{仓库: 原因} → 「仓库名：主工作区当前分支 x，主干应为 y」一行文本。"""
    parts = [f"{Path(p).name}：{why}" for p, why in (guard or {}).items()]
    more = "…" if len(parts) > limit else ""
    return "；".join(parts[:limit]) + more


def _push_main_once(repo, remote, base, env, timeout):
    """推送主干（只推 <base> 一个 ref，绝不 --force）；返回 (rc, stderr)。"""
    rc, _out, err = git_ops.run_git(repo, ["push", remote, base], env=env, timeout=timeout)
    return rc, err


def _push_done_worktrees(req, sync):
    """done 合并成功后按 git_sync.on_done 处理主干远端。返回 (push_failed, pending_push)：
    - auto：逐仓库 with repo_lock `git push <remote> <base>`；非快进先 fetch + ff-only 重试一次，
      仍失败记入 push_failed（不阻断 done，本地合并保留）；
    - manual：不推送，统计「领先远端提交数」写入 pending_push（事件 + _set_done 三处提示）；
    - off/None：零动作（不碰远端）。
    push_failed={仓库名}；pending_push={仓库名: 领先提交数 | None(无法统计)}。"""
    if not sync:
        return set(), {}
    on_done = sync.get("on_done") or GIT_SYNC_DEFAULTS["on_done"]
    if on_done not in ("auto", "manual"):
        return set(), {}
    if not _repo_paths(req):
        return set(), {}
    failed, pending = set(), {}
    for repo in _repo_paths(req):
        name = Path(repo).name
        try:
            _push_done_one_repo(req, repo, sync, on_done, failed, pending)
        except Exception as e:  # noqa: BLE001 - 推送失败不得阻断 done
            log.warning("需求 #%s 仓库 %s done 推送异常: %s", req.get("id"), name, e)
            if on_done == "auto":
                failed.add(name)
            else:
                pending[name] = None
            _git_sync_event(req, f"仓库 {name} done 收尾远端操作异常（{e}），"
                                 f"本地合并已保留，不阻断需求完成")
    return failed, pending


def _push_done_one_repo(req, repo, sync, on_done, failed, pending):
    """单仓库 done 推送/统计（_push_done_worktrees 的子步骤）。"""
    name = Path(repo).name
    project = req.get("project")
    cfg = PROJECTS.get(project or "") or {}
    base, _ = _local_base_branch(repo, cfg)
    remote = _git_sync_remote(project)
    timeout = _git_sync_timeout(sync)
    if not base:
        log.warning("需求 #%s 仓库 %s 主干探测失败，跳过 done %s", req.get("id"), name, on_done)
        if on_done == "auto":
            failed.add(name)
        return
    if remote not in git_ops.remote_names(repo):
        log.info("需求 #%s 仓库 %s 无远端 %s，跳过 done %s（本地项目属正常）",
                 req.get("id"), name, remote, on_done)
        return
    with git_ops.git_env(project) as env:
        with git_ops.repo_lock(repo, timeout=timeout):
            if on_done == "auto":
                rc, err = _push_main_once(repo, remote, base, env, timeout)
                if rc == 0:
                    post_dispatcher_event(int(req["id"]),
                                          f"仓库 {name} 主干 {base} 已自动推送到 {remote}", kind="merge")
                    return
                log.warning("需求 #%s 仓库 %s push %s 失败（%s），尝试 fetch+ff 重试一次",
                            req.get("id"), name, base, _git_err(err))
                frc, _fout, ferr = git_ops.run_git(repo, ["fetch", "--prune", remote],
                                                   env=env, timeout=timeout)
                if frc != 0:
                    failed.add(name)
                    _git_sync_event(req, f"仓库 {name} 主干 push 失败且 fetch 重试失败"
                                         f"（{_git_err(ferr)}），本地合并已保留，请手动推送")
                    return
                mrc, _mout, merr = git_ops.run_git(
                    repo, ["merge", "--ff-only", f"{remote}/{base}"], env=env, timeout=timeout)
                if mrc != 0:
                    failed.add(name)
                    _git_sync_event(req, f"仓库 {name} 主干 push 非快进且无法快进合并"
                                         f"（{_git_err(merr)}），本地合并已保留，请手动推送")
                    return
                rc2, err2 = _push_main_once(repo, remote, base, env, timeout)
                if rc2 != 0:
                    failed.add(name)
                    _git_sync_event(req, f"仓库 {name} 主干 fetch+ff 后再次 push 仍失败"
                                         f"（{_git_err(err2)}），本地合并已保留，请手动推送")
                else:
                    post_dispatcher_event(int(req["id"]),
                                          f"仓库 {name} 主干 {base} 先快进再推送成功（{remote}）",
                                          kind="merge")
                return
            # manual：不推，只统计领先远端提交数（fetch 只读，保证数字可信）
            frc, _fout, ferr = git_ops.run_git(repo, ["fetch", "--prune", remote],
                                               env=env, timeout=timeout)
            if frc != 0:
                log.warning("需求 #%s 仓库 %s manual fetch 失败（%s），按现有远端引用统计",
                            req.get("id"), name, _git_err(ferr))
            ahead, _behind = git_ops.ahead_behind(repo, base, remote=remote, env=env)
            if ahead is None:
                pending[name] = None
                _git_sync_event(req, f"仓库 {name} 远端无主干 {base} 分支，"
                                     f"待手动推送（git push {remote} {base}）")
            elif ahead > 0:
                pending[name] = ahead
                post_dispatcher_event(int(req["id"]),
                                      f"合并完成，仓库 {name} 主干领先 {remote}/{base} {ahead} 个提交，"
                                      f"按 on_done=manual 待手动推送（git push {remote} {base}）",
                                      kind="merge")


def ensure_dev_worktrees(req):
    """analysis 起全部角色：在每个涉及仓库创建独立 worktree（分支 f-<req_id>，幂等），
    分支显式从主干默认分支拉取，保证调研/开发基线 = 主干，不随主工作区 checkout 漂移。
    同一需求跨重试/评审复用同一分支与 worktree。返回 (workdir, {子目录名: worktree 路径})

    #420：测试进程 + 路径未隔离 → 短路为「只返回形状」（不 mkdir / 不跑 git / 不改 state）：
    未隔离用例的假 req id 会真建 f-<日期>-<假id> 分支与 ~/.req-board/worktrees 工作树；
    已隔离用例（WORKTREES_ROOT 或 dev_worktree_path 被 patch）判据不成立，照走真实逻辑。"""
    cfg = PROJECTS[req["project"]]
    if (in_test_env() and WORKTREES_ROOT == _REAL_WORKTREES_ROOT
            and dev_worktree_path is _REAL_DEV_WORKTREE_PATH):
        log.warning("测试进程跳过真实 worktree 创建（#420 短路）: req=%s project=%s",
                    req.get("id"), req.get("project"))
        return dev_workdir_for(req), {Path(p).name: dev_worktree_path(req, p) for p in cfg["paths"]}
    # git_sync.before_card：仅当本轮确有 worktree 需新建时同步一次主工作区
    # （fetch 档只更新远端引用；pull 档才在干净且同主干时快进/判定分叉）。
    # 已有 worktree 的复用/重试路径不重复同步——避免每次 spawn 都打扰主工作区。
    sync = _project_git_sync(req.get("project"))
    if sync and sync.get("before_card") in ("fetch", "pull"):
        pending_repos = [p for p in cfg["paths"]
                         if not (dev_worktree_path(req, p) / ".git").exists()]
        if pending_repos:
            _sync_main_worktrees(req, sync)
    branch = dev_branch(req)
    repo_map = {}
    base = {}
    for p in cfg["paths"]:
        repo = Path(p)
        wt = dev_worktree_path(req, p)
        wt.parent.mkdir(parents=True, exist_ok=True)
        if not (wt / ".git").exists():
            has_branch = subprocess.run(["git", "rev-parse", "--verify", "-q", branch],
                                        cwd=repo, capture_output=True, timeout=DIAG_TIMEOUT).returncode == 0
            start, start_sha = _default_branch(repo, cfg)
            # worktree 目录异常丢失时 git 簿记可能残留（分支仍标记已检出），prune 后重试一次
            for attempt in (1, 2):
                try:
                    if has_branch:
                        subprocess.run(["git", "worktree", "add", str(wt), branch],
                                       cwd=repo, check=True, capture_output=True,
                                       timeout=GIT_WORKTREE_TIMEOUT)
                    elif start:
                        subprocess.run(["git", "worktree", "add", str(wt), "-b", branch, start],
                                       cwd=repo, check=True, capture_output=True,
                                       timeout=GIT_WORKTREE_TIMEOUT)
                    else:
                        subprocess.run(["git", "worktree", "add", str(wt), "-b", branch],
                                       cwd=repo, check=True, capture_output=True,
                                       timeout=GIT_WORKTREE_TIMEOUT)
                    break
                except subprocess.CalledProcessError:
                    subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True,
                                   timeout=GIT_WORKTREE_TIMEOUT)
                    if attempt == 2:
                        raise
            # 基线记录：分支 HEAD（新建分支 == 主干起点 sha），供 diff 检测与收口使用
            r = subprocess.run(["git", "rev-parse", "--verify", "-q", branch],
                               cwd=repo, capture_output=True, text=True, timeout=DIAG_TIMEOUT)
            base[str(p)] = (r.stdout.strip() if r.returncode == 0 else (start_sha or ""))
        else:
            # 幂等复用（跨重试/评审）：仅补记缺失的基线，不覆盖既有记录（#343 现场：
            # dev commit 后 resume 会话再次进入本分支，旧实现无条件用分支当前 HEAD
            # 覆盖 base，has_code_changes 判「无代码变更」死循环）。补记用
            # merge-base(默认分支, 分支) = 分叉点 = 主干起点，与新建分支时的语义一致
            existing = state.get("worktrees", {}).get(str(req["id"]), {}).get("base", {})
            recorded = existing.get(str(p)) if isinstance(existing, dict) else None
            if recorded:
                base[str(p)] = recorded
            else:
                start, start_sha = _default_branch(repo, cfg)
                mb = ""
                if start:
                    r = subprocess.run(["git", "merge-base", start, branch],
                                       cwd=repo, capture_output=True, text=True, timeout=DIAG_TIMEOUT)
                    if r.returncode == 0:
                        mb = r.stdout.strip()
                base[str(p)] = mb or start_sha or ""
        _link_repo_local_config(wt)   # 本地配置与主工作区同源（单仓库测试语义一致，见函数注释）
        repo_map[Path(p).name] = wt
    with state_lock:
        rec = state.setdefault("worktrees", {}).get(str(req["id"])) or {}
        rec.update({
            "project": req["project"], "branch": branch, "base": base,
            # (归属仓库, worktree 路径)：删除 worktree 必须在归属仓库 cwd 下执行；
            # 注意每个 p 重新计算 wt（勿用循环残留变量，否则所有条目指向最后一个仓库）
            "worktrees": [(str(Path(p)), str(dev_worktree_path(req, p))) for p in cfg["paths"]]})
        state["worktrees"][str(req["id"])] = rec
    save_state()
    return dev_workdir_for(req), repo_map

def dev_repo_listing(req, repo_map, focus=None):
    wd = dev_workdir_for(req)
    branch = dev_branch(req)
    if len(repo_map) == 1:
        name, wt = next(iter(repo_map.items()))
        return f"工作目录：{wd}（独立 worktree，分支 {branch}）"
    lines = [f"工作目录：{wd}（dev 聚合目录，非 git 仓库；各仓库 worktree 子目录），分支 {branch}",
             "仓库清单（子目录 -> worktree）："]
    focus = set(focus or [])
    for name, wt in repo_map.items():
        mark = "（本次相关）" if focus and name in focus else ""
        lines.append(f"- {wd / name} -> {wt}{mark}")
    if focus:
        lines.append(f"说明：工作目录包含全部 {len(repo_map)} 个仓库的 worktree；"
                     f"本次需求相关仓库 {len(focus)} 个（analysis/notes 识别，指引用）。"
                     f"清单外仓库确有需要也可直接修改，完成后系统按实际改动识别涉及仓库。")
    return "\n".join(lines)

def cleanup_dev_worktrees(req_id, delete_branch=True, keep_repos=None):
    """清理需求的 dev worktrees（可选删分支）。done 合并后、人工接管、启动兜底时调用。
    delete_branch: True=全删分支；False=全留；"changed"=只删「未实际改动」仓库的分支
    （keep_repos=有改动仓库名集合，用于仅推分支项目收口：有改动的分支保留到发版，空分支删除）。"""
    with state_lock:
        rec = state.get("worktrees", {}).pop(str(req_id), None)
    if not rec:
        return
    # 每个 worktree 必须在它归属的仓库 cwd 下删除，否则 git 找不到对应仓库
    for repo, wt in rec.get("worktrees") or []:
        r = subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=repo, capture_output=True,
                           timeout=GIT_WORKTREE_TIMEOUT)
        if r.returncode != 0:
            log.warning(f"清理 worktree {wt} 失败: {r.stderr.decode(errors='replace')[:200]}")
    if delete_branch:
        keep = set(keep_repos or []) if delete_branch == "changed" else None
        for p in PROJECTS.get(rec["project"], {}).get("paths", []):
            if keep is not None and Path(p).name in keep:
                continue
            r = subprocess.run(["git", "branch", "-D", rec["branch"]], cwd=p, capture_output=True,
                               timeout=DIAG_TIMEOUT)
            if r.returncode != 0:
                log.warning(f"删除分支 {rec['branch']}@{p} 失败: {r.stderr.decode(errors='replace')[:200]}")
    save_state()

def push_company_branches(req, branch, repos):
    """#288：仅推分支项目（branch_only）done 前把有改动仓库的 f-分支 push 到 origin。
    只 push <branch> 分支本身（不 --force、不推其他 refs）；仓库无 origin 远端属正常
    （本地纯项目），跳过不算失败。返回 push 失败仓库名集合——失败不阻断 done，
    由 _set_done（push_failed）转 summary/时间线/done 卡片红字三处提醒手动 push。"""
    failed = set()
    for p in PROJECTS[req["project"]]["paths"]:
        name = Path(p).name
        if name not in repos:
            continue
        try:
            has_origin = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=p,
                                        capture_output=True, text=True, timeout=DIAG_TIMEOUT).returncode == 0
            if not has_origin:
                log.info(f"需求 #{req['id']} 仓库 {name} 无 origin 远端，跳过 push")
                continue
            r = subprocess.run(["git", "push", "origin", branch], cwd=p,
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                log.warning(f"需求 #{req['id']} push {branch}@{name} 失败: "
                            f"{r.stderr.decode(errors='replace')[:200]}")
                failed.add(name)
        except Exception as e:
            log.warning(f"需求 #{req['id']} push {branch}@{name} 异常: {e}")
            failed.add(name)
    return failed


def merge_dev_branches(req):
    """把 dev 分支 --no-ff 合并回各仓库 main（main worktree）。返回 (ok, 失败仓库列表)
    #412（C）：补 timeout + TimeoutExpired 时尽力 git merge --abort（清理超时残留的
    MERGE_HEAD 现场）并列入 failed；正常冲突（returncode!=0）保留现场语义不变
    （merge_resolve 依赖冲突现场）。
    git_sync 主干守卫：项目配置了 git_sync 时，合并前校验主工作区当前分支 == 本项目主干
    （_merge_preflight）；不一致不执行 merge（否则会把 dev 落进错误分支），记入 failed 并留
    kind=merge 事件，结论由调用方走现有失败/人工通道（文案见 _merge_conflict_fallback(guard)，
    不得误报「工作区脏」）。未配置 git_sync = 零额外校验、行为逐字不变。"""
    branch = dev_branch(req)
    failed = []
    sync_enabled = bool(_project_git_sync(req.get("project")))
    for p in PROJECTS[req["project"]]["paths"]:
        if sync_enabled:
            why = _merge_preflight(req, p)
            if why:
                log.warning("需求 #%s 仓库 %s 未合并（git_sync 主干守卫）：%s", req["id"], p, why)
                post_dispatcher_event(int(req["id"]),
                                      f"自动合并跳过：{Path(p).name} {why}；请在主干上重试合并",
                                      kind="merge")
                failed.append(str(p))
                continue
        try:
            r = subprocess.run(["git", "merge", "--no-ff", branch, "-m", f"auto merge req #{req['id']}"],
                               cwd=p, capture_output=True, timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning(f"需求 #{req['id']} 仓库 {p} 合并超时（>{GIT_TIMEOUT}s），尝试 abort 清理残留")
            try:
                subprocess.run(["git", "merge", "--abort"], cwd=p, capture_output=True,
                               timeout=GIT_TIMEOUT)
            except Exception as e:
                log.warning(f"需求 #{req['id']} 仓库 {p} 合并超时后 abort 失败: {e}")
            failed.append(str(p))
            continue
        if r.returncode != 0:
            failed.append(str(p))
    return (not failed), failed


def _merge_dirty_blockers(req, failed):
    """合并失败后区分「main 工作区未提交改动挡住合并」与真冲突（仅失败路径调用，成功路径零开销）。
    git 预检拒绝（local changes / untracked files would be overwritten）不会留下 MERGE_HEAD，
    此时取工作区未提交 + 未跟踪文件 ∩ dev 分支改动文件。merge_resolve 会话改的是 dev 分支，
    解决不了 main 工作区的脏现场，调用方据此跳过 merge_resolve 并给出准确提示。
    返回 {仓库路径: [文件...]}，只含被挡住的仓库；探测异常的仓库按真冲突处理（fail-closed）。"""
    branch = dev_branch(req)
    blockers = {}
    for p in failed:
        try:
            if subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=p,
                              capture_output=True, timeout=DIAG_TIMEOUT).returncode == 0:
                continue  # 有 MERGE_HEAD = 真冲突现场
            st = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                                cwd=p, capture_output=True, text=True, timeout=DIAG_TIMEOUT)
            df = subprocess.run(["git", "diff", "--name-only", "-z", f"HEAD...{branch}"],
                                cwd=p, capture_output=True, text=True, timeout=DIAG_TIMEOUT)
            if st.returncode != 0 or df.returncode != 0:
                continue
            dirty, fields = set(), st.stdout.split("\0")
            i = 0
            while i < len(fields):
                ent = fields[i]
                i += 1
                if len(ent) < 4:
                    continue
                dirty.add(ent[3:])
                if ent[0] in "RC":
                    i += 1  # -z 下重命名/复制条目后跟原路径字段
            hit = sorted(dirty & {f for f in df.stdout.split("\0") if f})
            if hit:
                blockers[str(p)] = hit
        except Exception as e:
            log.warning(f"需求 #{req['id']} 仓库 {p} 合并失败原因探测失败（按真冲突处理）: {e}")
    return blockers


def _dirty_blockers_text(blockers, limit=5):
    """{仓库: [文件]} → 「my-app：a.py、b.html」一行文本（每仓库最多列 limit 个文件）"""
    parts = []
    for p, files in blockers.items():
        more = f" 等 {len(files)} 个" if len(files) > limit else ""
        parts.append(f"{Path(p).name}：{'、'.join(files[:limit])}{more}")
    return "；".join(parts)


def _merge_fail_text(failed, blockers):
    """合并重试失败的提示文本：工作区脏现场给出文件与处理方式，否则沿用「冲突（仓库）」口径"""
    if blockers:
        return (f"被 main 工作区未提交改动挡住，非代码冲突（{_dirty_blockers_text(blockers)}）"
                "——请先提交或 stash 这些改动再重试")
    return f"仍冲突（{', '.join(failed)}）"

def _wait_url_up(url, timeout=10, interval=0.5):
    """轮询 url 直到返回 200（服务重启窗口内连接会被拒），返回是否就绪。
    #300：从 _wait_board_up 参数化而来，供项目 restart 配方的 verify 就绪校验复用"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _opener.open(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _wait_board_up(timeout=10):
    """kickstart 后轮询看板 API 直到可访问（重启窗口内连接会被拒），返回是否就绪"""
    return _wait_url_up(BOARD_API, timeout=timeout)


def _kickstart_board_if_own(project, req_id):
    """项目仓库就是看板自身（REPO_DIR）时，合并后 kickstart launchd 托管看板让新代码生效。
    #264：重启动作落调度器时间线——成功/失败（异常或非零退出码）均发 kind=merge 事件，失败保留 log.warning。
    #8：run_local 直跑模式无 launchd（kickstart 必然失败）→ 不调 launchctl，降级为「事件 +
    # 心跳 board_restart_pending 标记」（看板经 /api/dispatcher-health 透出、页头角标提示人工重启），
    # 不产生「失败」事件；launchd 分支逐字不变。"""
    try:
        paths = [str(Path(p).resolve()) for p in PROJECTS[project]["paths"]]
        if str(REPO_DIR.resolve()) in paths:
            if is_run_local_supervisor():
                msg = "配置已合并，重启看板后生效；直跑模式请手动重启（Ctrl-C 后重跑 run_local.sh）"
                try:
                    write_heartbeat({"board_restart_pending": {"at": time.time(), "req_id": int(req_id)}})
                except Exception as e:
                    # 写失败只告警不误报失败：事件本身仍是有效提示通道
                    log.warning(f"写入看板待重启心跳标记失败: {e}")
                post_dispatcher_event(int(req_id), msg, kind="merge")
                log.info(f"需求合并后看板待重启（run_local 直跑模式，已提示人工处理）")
                return
            r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/local.req-board"],
                               capture_output=True, timeout=LAUNCHCTL_TIMEOUT)
            if r.returncode != 0:
                # 非零退出码不会抛异常，需显式视为失败（否则失败路径无事件）
                reason = (r.stderr or b"").decode(errors="replace").strip() \
                    or f"launchctl kickstart 退出码 {r.returncode}"
                raise RuntimeError(reason)
            log.info(f"需求合并后已重启看板（local.req-board）")
            # #266：kickstart 后看板端口尚未就绪，立即写事件会被 ConnectionRefused 吞掉；
            # 等就绪再写，超时按失败处理（失败事件 + 告警）
            if _wait_board_up():
                post_dispatcher_event(int(req_id), "合并后已重启看板（local.req-board），新代码生效", kind="merge")
            else:
                raise RuntimeError("看板重启后 10s 内未就绪")
    except Exception as e:
        log.warning(f"重启看板失败: {e}")
        post_dispatcher_event(int(req_id), f"看板自动重启失败: {e}，需人工处理", kind="merge")


def run_project_restart(project, req_id):
    """#300：done 流程本地合并成功后，按 projects.json 的 restart 配方自动重启项目本地服务。
    配方 {cmd, verify?, timeout?}：先执行 cmd（shell、cwd=项目首个仓库路径、带执行超时），
    再轮询 verify 直到返回 200（未配 verify 则 cmd 成功即视为就绪）；成功发 kind=merge 事件留痕。
    返回失败原因字符串（"" 表示成功或未配置），失败由调用方传 _set_done(restart_failed) 落
    summary + 时间线 + done 卡片红字三处留痕，一律不阻断 done。
    任何异常（cmd 超时/非零退出/verify 超时/配置畸形）全部降级为原因字符串，不抛出。
    仅推分支（company/branch_only）项目不本地合并、天然走不到这里；req-board 保留 _kickstart_board_if_own 特例，
    不建议同时配 restart（避免双重重启）。"""
    cfg = PROJECTS.get(project, {}).get("restart")
    if not cfg:
        return ""
    cmd = str(cfg.get("cmd") or "").strip()
    if not cmd:
        return ""
    path = (PROJECTS[project].get("paths") or [None])[0]
    verify = str(cfg.get("verify") or "").strip()
    timeout = max(1, int(cfg.get("timeout") or 30))
    try:
        # shell=True：配方命令可能含 $(id -u) 等 shell 语法（launchctl kickstart）；
        # projects.json 为本机可信数据，无注入面
        r = subprocess.run(cmd, shell=True, cwd=path, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or b"").decode(errors="replace").strip()[:200] \
                or f"退出码 {r.returncode}"
            return f"重启命令执行失败（退出码 {r.returncode}）：{detail}"
    except subprocess.TimeoutExpired:
        return f"重启命令执行超时（>{timeout}s）"
    except Exception as e:
        return f"重启命令执行异常：{e}"
    if verify and not _wait_url_up(verify, timeout=timeout):
        return f"服务 {timeout}s 内未就绪（verify {verify} 未返回 200）"
    log.info(f"需求 #{req_id} 已自动重启项目服务（{project}：{cmd[:100]}"
             + (f"，就绪校验 {verify}" if verify else "") + "）")
    post_dispatcher_event(int(req_id),
                          f"已自动重启项目服务（{project}）"
                          + (f"，就绪校验通过：{verify}" if verify else ""),
                          kind="merge")
    return ""

# #318：重启类待办对账（restart 配方已成功执行时）：纯重启条目剔除，混合条目标注保留人工部分。
# 匹配「重启/restart + 目标（服务/进程/实例/应用/server）」短语，如「重启 com.example.web Web 服务」；
# 不匹配「重启看板/调度器」等（restart 配方不负责这些，误剔会吞掉真实人工事项）
_RESTART_TODO_ACT_RE = re.compile(
    r"(?:重启|restart)[^。\n]{0,80}?(?:服务|进程|实例|应用|server)", re.IGNORECASE)
# 重启片段剔除后剩余内容可能带重启方式说明（括号块，如「（pkill -f src.web，launchd 自动拉起）」），
# 先剥离再判人工信号，避免说明性残留被误判为人工事项
_RESTART_TODO_BRACKET_RE = re.compile(r"^[（(【\[][^)）】\]]*[)）】\]]")
# 人工部分前置的承接片段（「）后」「然后」「并」等），标注前剥掉保持语义干净
_RESTART_TODO_LEAD_RE = re.compile(
    r"^(?:[、，,；;。\s]*(?:然后|再|并|后|and|then)[、，,；;。\s]*)")
# 剩余内容的人工动作信号（承接词/人工动作词）：有此信号才保留并标注
_RESTART_TODO_HUMAN_RE = re.compile(
    r"后|然后|再|并|验收|检查|验证|确认|测试|查看|核对|verify|check|test", re.IGNORECASE)


def _reconcile_restart_todos(todos):
    """restart 配方已成功执行时的 todos 对账（#318）：
    非重启条目原样保留；重启条目剔除重启动作片段（含括号内的重启方式说明）后，剩余仍含
    人工事项（浏览器验收等）则标注「（服务已由调度器自动重启）」保留，纯重启条目直接剔除。
    返回对账后的新列表（不修改入参）。"""
    out = []
    for todo in todos:
        if not _RESTART_TODO_ACT_RE.search(todo):
            out.append(todo)
            continue
        rest = _RESTART_TODO_ACT_RE.sub("", todo).strip()
        rest = _RESTART_TODO_BRACKET_RE.sub("", rest).strip()
        rest = _RESTART_TODO_LEAD_RE.sub("", rest).strip(" \t，,；;。")
        if not rest or not _RESTART_TODO_HUMAN_RE.search(rest):
            continue  # 纯重启条目（剩余仅为重启方式说明）：剔除
        out.append(f"{rest}（服务已由调度器自动重启）")
    return out

# 改动涉及调度器自身模块（dispatcher.py / acp_shared.py）时 done 通知/总结附带的「需手动重启调度器」提醒文案（#380 起为兜底文案）
DISPATCHER_RESTART_HINT = ("改动涉及调度器模块（dispatcher.py / acp_shared.py），已合并；"
                           "需手动重启调度器生效（kill <pid>，launchd KeepAlive 自动拉起）")
# #8：run_local 直跑模式兜底提示（无 KeepAlive，人工 Ctrl-C 后重跑 run_local.sh）
DISPATCHER_RESTART_HINT_RUN_LOCAL = ("改动涉及调度器模块（dispatcher.py / acp_shared.py），已合并；"
                                     "直跑模式请手动重启：Ctrl-C 后重跑 run_local.sh")
# #8：来源标记新鲜度上限（秒）——at 距今超过 TTL 视为陈旧，不触发 exec 自重启
# （防残留标记在窗口外被随后 Ctrl-C 的 kill 0 SIGTERM 命中而劫持停止语义）
RESTART_SOURCE_TTL_SEC = 300
# #380：done 流程自动重启调度器（调度器模块改动免手动重启）——命中时写入 pending_restart，
# 主循环择机 kill -TERM 自身（KeepAlive 拉起），重启成功由新进程启动自证补发通知。
DISPATCHER_AUTO_RESTART_PENDING = "改动涉及调度器模块（dispatcher.py / acp_shared.py），已合并；调度器将自动重启生效（无需手动操作）"
DISPATCHER_AUTO_RESTART_DELAYED = "改动涉及调度器模块（dispatcher.py / acp_shared.py），已合并；将在并行 agent 结束后自动重启调度器生效"
DISPATCHER_AUTO_RESTART_OK = "调度器已自动重启，调度器模块（dispatcher.py / acp_shared.py）改动已生效"
# #380 自动重启判定文件名清单（#423）：调度器进程内加载的模块改动都要计入，否则只改 acp_shared.py
# 时静默不生效（旧清单只认 dispatcher.py）
RESTART_TRIGGER_FILES = frozenset({"dispatcher.py", "acp_shared.py"})
MAX_AUTO_RESTART_ATTEMPTS = 3  # 自动重启 kill 重试上限（超过转手动提醒兜底）

def _branch_base(req, repo_path):
    """dev 分支三点 diff 基线：优先取建 worktree 时记录的主干起点 commit sha；
    无记录（存量 state/兜底）时回退主工作区当前分支 HEAD。返回空串表示无法确定基线。"""
    rec = state.get("worktrees", {}).get(str(req["id"]))
    base_sha = ((rec or {}).get("base") or {}).get(str(repo_path)) or ""
    if base_sha:
        return base_sha
    try:
        return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path,
                              capture_output=True, text=True, timeout=DIAG_TIMEOUT).stdout.strip()
    except Exception:
        return ""

def _touches_dispatcher(req, branch):
    """检测 dev 分支相对各仓库基线（主干起点，见 _branch_base）的变更文件是否命中
    RESTART_TRIGGER_FILES（dispatcher.py / acp_shared.py，均为调度器进程内加载的模块）。
    命中说明需求改动长驻进程（调度器自身），合并后需重启才生效。
    提醒为附加信息不阻断：任何仓库检测失败/无该分支时静默返回 False（宁缺勿滥）。"""
    files = set()
    for p in PROJECTS[req["project"]]["paths"]:
        try:
            base = _branch_base(req, p)
            if not base:
                continue
            r = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=p,
                               capture_output=True, text=True, timeout=DIAG_TIMEOUT)
            if r.returncode == 0:
                files.update(Path(f).name for f in r.stdout.splitlines() if f.strip())
        except Exception as e:
            log.warning(f"检测需求 #{req['id']} dev 分支变更文件失败（{p}）: {e}")
    return bool(files & RESTART_TRIGGER_FILES)

# #394：合并改动路径三档判定（skip/restart/unknown）与 diff 采集。判定仅按路径前缀匹配，
# 无 IO；skip 判断静态文件（按请求读盘不重启），trigger 为需重启的服务端目录/文件，
# 未知路径（不在 skip ∪ trigger）保守不自动重启 + 告警人工确认（fail-closed：diff 获取
# 失败同样按 unknown 处理）。判定清单按项目存于 projects.json（按各项目目录
# 结构特化），skip_prefixes 缺省 ["src/web/static/"]。
DEFAULT_RESTART_SKIP_PREFIXES = ("src/web/static/",)


def _collect_merge_changed_paths(req, branch):
    """#394：采集 dev 分支相对各仓库基线（_branch_base）的改动文件相对路径列表（保留完整
    相对路径以做前缀匹配，区别于 _touches_dispatcher 只取 basename）。任一仓库基线缺失/
    diff 失败 → 返回 None（调用方按 unknown 保守处理，不静默）。"""
    files = []
    for p in PROJECTS[req["project"]]["paths"]:
        try:
            base = _branch_base(req, p)
            if not base:
                return None
            r = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=p,
                               capture_output=True, text=True, timeout=DIAG_TIMEOUT)
            if r.returncode != 0:
                return None
            files.extend(f.strip() for f in r.stdout.splitlines() if f.strip())
        except Exception as e:
            log.warning(f"需求 #{req['id']} 采集合并改动文件清单失败（{p}）: {e}")
            return None
    return files


def _judge_restart_needed(changed_paths, project_cfg):
    """#394：按改动文件路径三档判定（纯前缀匹配，无 IO）。
    返回 "skip" | "restart" | "unknown"：
    - 全部改动 ∈ skip_prefixes（或空清单/纯合并无改动）→ "skip"（不重启无告警）；
    - 命中 trigger_prefixes 且无未知路径 → "restart"（执行 restart 配方）；
    - 存在未知路径（任一路径 ∉ skip ∪ trigger）→ "unknown"（未知优先，保守不重启+告警）。
    changed_paths 为 None（采集失败）时调用方已按 unknown 处理，函数按空清单返回 skip，
    但调用方不应依赖该行为。"""
    cfg = project_cfg or {}
    restart = cfg.get("restart") or {}
    trigger = tuple(str(p) for p in (restart.get("trigger_prefixes") or ()) if str(p).strip())
    skip = tuple(str(p) for p in (restart.get("skip_prefixes")
                                  or DEFAULT_RESTART_SKIP_PREFIXES) if str(p).strip())
    paths = [str(p) for p in (changed_paths or []) if str(p).strip()]
    if not paths:
        return "skip"

    def _hit(path, prefixes):
        return any(path.startswith(t) for t in prefixes)

    unknown = [p for p in paths if not (_hit(p, trigger) or _hit(p, skip))]
    if unknown:
        return "unknown"
    return "restart" if any(_hit(p, trigger) for p in paths) else "skip"


def _maybe_auto_restart_after_merge(req, merge_files, req_id):
    """#394：合并成功后按项目开关与判定结果处理自动重启（finalize_done / on_message /
    网页端重试合并三处共用）。merge_files 为合并前采集的改动文件清单（None=采集失败）。
    返回 (restart_failed, restart_ok)：
    - 开关未开：维持 #300 行为（走 run_project_restart，有配方则执行）；
    - 开关开 + "restart"：执行 run_project_restart；
    - "skip"：不执行（不重启无告警）；
    - "unknown"（含采集失败）：不执行 + 告警（summary/merge 事件，调用方传 _set_done
      restart_warn 留痕，不阻断 done）。
    仅推分支（company/branch_only）项目不本地合并、天然不走这里。"""
    cfg = PROJECTS.get(req["project"], {}) or {}
    if not cfg.get("auto_restart_after_merge"):
        restart_failed = run_project_restart(req["project"], req_id) or None
        return restart_failed, restart_failed is None and bool(cfg.get("restart")), ""
    verdict = _judge_restart_needed(merge_files, cfg) if merge_files is not None else "unknown"
    if verdict == "restart":
        restart_failed = run_project_restart(req["project"], req_id) or None
        return restart_failed, restart_failed is None, ""
    if verdict == "unknown":
        unknown = [p for p in (merge_files or []) if p]
        sample = "、".join(sorted(unknown)[:5]) or "（合并改动文件清单获取失败）"
        warn = (f"改动文件含无法判定是否需重启的路径（{sample}），已按未知路径保守跳过自动重启；"
                f"若确认需要请人工重启（{(cfg.get('restart') or {}).get('cmd', '')[:80] or '重启服务'}）")
        return None, False, warn
    return None, False, ""  # skip：不重启无告警


def _retry_rec(req_id):
    """读重试记录，兼容旧 int 值（#109 升级前状态文件 retries[req_id] 为纯计数）。
    返回 dict：{"count": int, "last_failure": str|None}（last_failure 为最近一次失败原因）。"""
    rec = state["retries"].get(req_id)
    if isinstance(rec, dict):
        return {"count": rec.get("count", 0), "last_failure": rec.get("last_failure")}
    return {"count": rec if isinstance(rec, int) else 0, "last_failure": None}


def resume_failure_hint(req_id):
    """#109：续跑注入上次失败原因（state 无失败记录返回空串）。
    主循环重捞 resume 时拼进 extra，重试不再盲续；进程内重试走 handle_result 既有 extra。"""
    lf = _retry_rec(str(req_id)).get("last_failure")
    if not lf:
        return ""
    return (f"上次尝试失败原因：{lf}。先针对该原因自查"
            f"（运行超时/进程异常先确认改动完好、无残留 result.json）再继续。")


def clear_retries(req_id):
    """阶段成功推进时清零重试计数：语义为每阶段独立 MAX_RETRIES 次容错额度，
    analysis 失败计数不带到 dev（否则 dev 首次失败就显示「第 2 次」且只剩 1 次额度）。
    仅在成功推进路径调用（analysis 完成→dev/route、评审通过→下一阶段、dev→accept 等）；
    _set_done 另有 pop（幂等，保留）。
    #405：重做轮数（flow_review_rounds）由 clear_flow_rounds 管理（仅人工裁决/新工作周期清），
    本函数不再联动清——评审打回重做完成后轮数跨重做连续累计（#376 上限恢复生效）。"""
    with state_lock:
        state["retries"].pop(str(req_id), None)
        # #11：阶段成功推进=新的模型请求周期，清掉本轮降级记录（防跨阶段重复汇总）
        state.setdefault("model_fallback", {}).pop(str(req_id), None)
    save_state()


def clear_flow_rounds(req_id):
    """#405：评审轮内重做计数清零——仅人工裁决/新工作周期调用。与 retries 语义拆分：
    retries 随阶段成功推进清（各阶段独立容错额度），flow_review_rounds 跨打回重做连续累计
    （第 1–3 次拒自动打回重做，第 4 次拒触发强制人工裁决，FLOW_REVIEW_ROUNDS_MAX=3）。
    幂等：无记录 no-op；旧 state 残留方向偏保守（更快强制人评），可接受。"""
    with state_lock:
        state.get("flow_review_rounds", {}).pop(str(req_id), None)
        state.get("review_escalation", {}).pop(str(req_id), None)    # C3 升级记录同周期清
        state.get("review_round_points", {}).pop(str(req_id), None)
    save_state()

def merge_conflict_write(req_or_id, action=None):
    """#386 merge 冲突决策通道表写（POST 登记/重置 action；None=等待用户操作，按钮可见）。
    写失败不阻断既有收尾：降级=网页按钮不可见，飞书 y/n 通道仍可用；事件留痕便于追溯。"""
    try:
        api("POST", f"/{req_or_id}/merge-conflict", {"action": action})
        return True
    except Exception as e:
        log.warning(f"需求 #{req_or_id} merge 冲突表登记(action={action})失败（降级飞书通道）: {e}")
        try:
            post_dispatcher_event(int(req_or_id),
                                  f"网页端 merge 决策按钮登记失败（{e}），降级飞书 y/n 通道", kind="merge")
        except Exception:
            pass
        return False


def merge_conflict_clear(req_or_id, reason=""):
    """#386 merge 冲突决策通道表清除（消费成功/人工接管/幽灵决策清理；幂等，失败不阻断）。"""
    try:
        api("DELETE", f"/{req_or_id}/merge-conflict")
    except Exception as e:
        log.warning(f"需求 #{req_or_id} merge 冲突表清除失败（{reason}）: {e}")


# ==================== #27 待确认条目「键与内容同源」三层闸门 ====================
# 权威判据（单一）：条目内容卡的 id（entry["req"]["id"]）是身份权威，键服从内容；
# 卡是否终态一律 fetch_req 现取，禁用条目内快照的 status。
_CONFIRM_REQ_KEYS = ("id", "title", "project", "status", "flow", "flow_name", "node_index")


def _confirm_req_snapshot(req):
    """#27 待确认条目的 req 快照：深拷贝裁剪七键（去卡片对象别名，防 state 膨胀；
    不带 summary/notes/nodes_json）。非 dict 入参返回 None。"""
    if not isinstance(req, dict):
        return None
    return {k: req.get(k) for k in _CONFIRM_REQ_KEYS}


def _put_pending_confirm(key, entry):
    """#27 唯一落表出口（调用方须已持 state_lock）：裁剪 req 快照 + 写入 + 回读校验。

    **锁内纯内存函数**：不开锁、不做 IO、不落盘、不发事件、不打日志（state_lock 非重入，
    持锁再取恒死锁，#410/#412）——留痕与 save_state 由调用方在出锁后执行。
    返回 False = 键与内容不同源（调用方留痕）；entry["req"] 非 dict / id 不可解析 → True（fail-open）。"""
    key = str(key)
    entry["req"] = _confirm_req_snapshot(entry.get("req"))
    state["pending_confirm"][key] = entry
    snap = state["pending_confirm"].get(key)
    if not isinstance(snap, dict) or not isinstance(snap.get("req"), dict):
        return True
    try:
        return int(snap["req"]["id"]) == int(key)
    except (KeyError, TypeError, ValueError):
        return True


def _pending_content_req_id(entry, key):
    """#27 条目内容卡 id（身份权威读取，fail-open 回落键）：纯函数、无 IO。
    entry 非 dict / req 非 dict / id 不可解析 → int(key)。"""
    if isinstance(entry, dict) and isinstance(entry.get("req"), dict):
        try:
            return int(entry["req"]["id"])
        except (KeyError, TypeError, ValueError):
            pass
    return int(key)


def _pending_key_mismatch(pend, key):
    """#27 消费期同源判定：返回 (是否错位, 内容卡 id)。

    纯函数、无 IO、无副作用（可在持锁时调用）：pend 非 dict / req 非 dict / id 不可解析
    → (False, None)（fail-open，兼容 {"req": None} 注入与 pending_confirm={"295": True}）。"""
    if not isinstance(pend, dict) or not isinstance(pend.get("req"), dict):
        return False, None
    try:
        cid = int(pend["req"]["id"])
        return cid != int(key), cid
    except (KeyError, TypeError, ValueError):
        return False, None


def _pending_source_mismatch_note(key, entry, source):
    """#27 写入期错源留痕（调用方须在出锁后调用）：log.error + 需求时间线事件。"""
    try:
        cid = int(entry["req"]["id"])
    except (KeyError, TypeError, ValueError):
        cid = None
    msg = (f"待确认条目键与内容不同源（来源：{source}）：键 #{key}，内容属需求 #{cid}；"
           "消费期将按内容归属拒绝并归位，不会改动任何卡的状态")
    log.error(msg)
    try:
        post_dispatcher_event(int(key), msg, kind="confirm")
    except Exception as e:
        log.warning(f"待确认条目错源事件留痕失败（不阻断）: {e}")


def _pending_rehang(key, pend, content_id, source, question=None):
    """#27 消费期错源处理（三处消费点共用，**自带落盘**）：判定命中时条目仍在 state，
    此处「锁内改键 → 出锁 save_state → 留痕/通知」，盘上任一时刻要么原键要么新键。

    - 内容卡现取终态（done/dropped）→ 清掉条目（不归位）；
    - 目标键空闲 → 归位到内容卡键；
    - 目标键已被占用 → 保留原键 + src_conflict_hits 累计；≥3 次 → 清掉（转人工出口，
      通知与留痕带问题全文与正确编号，不静默丢）。
    不触碰任何卡的字段/状态、不动 confirm_rounds。返回 'rehanged'/'kept'/'cleared'。"""
    key = str(key)
    question = question or (pend.get("question") if isinstance(pend, dict) else "") or ""
    hits = int(pend.get("src_conflict_hits") or 0) if isinstance(pend, dict) else 0
    status = None
    try:
        req = fetch_req(content_id)
        status = (req or {}).get("status")
    except Exception as e:
        log.warning(f"待确认错源归位读取需求 #{content_id} 失败（按非终态处理）: {e}")
    outcome = "kept"
    with state_lock:
        if status in ("done", "dropped"):
            if state["pending_confirm"].get(key) is pend:
                state["pending_confirm"].pop(key, None)
            outcome = "cleared"
        elif str(content_id) not in state["pending_confirm"]:
            if state["pending_confirm"].get(key) is pend:
                state["pending_confirm"].pop(key, None)
            pend["req"] = _confirm_req_snapshot(pend.get("req"))
            state["pending_confirm"][str(content_id)] = pend
            outcome = "rehanged"
        else:
            hits += 1
            pend["src_conflict_hits"] = hits
            if hits >= 3:
                if state["pending_confirm"].get(key) is pend:
                    state["pending_confirm"].pop(key, None)
                outcome = "cleared"
    save_state()
    tail = f"问题：{question}" if question else ""
    if outcome == "rehanged":
        msg = (f"（{source}）回复编号 #{key} 与待确认条目内容不一致（内容属需求 #{content_id}）："
               f"已按内容归属归位到 #{content_id}，未改动任何卡的状态。{tail}")
    elif outcome == "cleared" and status in ("done", "dropped"):
        msg = (f"（{source}）回复编号 #{key} 与待确认条目内容不一致（内容属需求 #{content_id}）："
               f"该需求已 {status}，错源条目已清理，未改动任何卡的状态。{tail}")
    elif outcome == "cleared":
        msg = (f"（{source}）回复编号 #{key} 与待确认条目内容不一致（内容属需求 #{content_id}）："
               f"目标键 #{content_id} 已有待确认条目，连续 {hits} 次无法归位，错源条目已清理转人工处理，"
               f"未改动任何卡的状态。{tail}")
    else:
        msg = (f"（{source}）回复编号 #{key} 与待确认条目内容不一致（内容属需求 #{content_id}）："
               f"目标键 #{content_id} 已有待确认条目，保留原键未归位（第 {hits} 次），"
               f"未改动任何卡的状态。{tail}")
    log.error(msg)
    notify(msg)
    try:
        post_dispatcher_event(int(key), msg, kind="confirm")
    except Exception as e:
        log.warning(f"待确认错源事件留痕失败（不阻断）: {e}")
    return outcome


def _purge_ghost_confirm(req_id, reason="", terminal=False):
    """#25 终态清账单点 + #27 条目身份守卫（合并口径）：需求进入终态（done/dropped）时，
    同需求 pending_confirm 任意 kind 的条目、pending_merge_review 与 confirm_decisions
    决策行都必为残留——pending 只在事件驱动路径（飞书 y/n、网页 action）消费，人工置终态/
    推进路径完成会绕过消费导致永久残留（#24 现场：done 卡仍挂「待确认」徽标与裁决按钮，
    误 finalize_done/误打 later）。
    终态判定与清理同点完成：terminal=True（仅 _set_done 置 done 成功后使用）直接采信；
    terminal=False 一律现取 fetch_req 最新状态裁决，回读异常/缺卡/缺 status/非终态均 no-op。
    #394 红线按卡状态豁免：非终态卡任意 kind（含 confirm/confirm_override）逐字保留。
    #27 条目身份守卫：条目内容卡 id 是本卡才清——键与内容不同源（内容属别的卡）时不清、
    返回 False，由调用方留痕转人工，防误清在途卡的待确认。
    决策行清理由 confirm_decision_clear 幂等执行（pending 已被其它路径 pop 也能消除孤儿 action）。
    返回是否清理了 pending 条目（清后事件留痕，幂等不阻断终态收尾）。"""
    req_id = str(req_id)
    if not terminal:
        try:
            req = fetch_req(req_id)
        except Exception as e:
            log.warning(f"需求 #{req_id} 终态回读失败，幽灵清理跳过（{reason}）: {e}")
            return False
        if (req or {}).get("status") not in ("done", "dropped"):
            return False
    with state_lock:
        entry = state["pending_confirm"].get(req_id)
        if isinstance(entry, dict) and _pending_content_req_id(entry, req_id) != int(req_id):
            # #27 条目身份守卫：内容属别的卡（键与内容不同源）——不按键误清，保留待人工
            return False
        entry = state["pending_confirm"].pop(req_id, None)
        popped_pmr = state.get("pending_merge_review", {}).pop(req_id, None) is not None
    confirm_decision_clear(req_id, reason)
    save_state()
    if entry is not None:
        merge_conflict_clear(req_id, reason)
        post_dispatcher_event(int(req_id),
                              f"{reason}：清理幽灵待确认（kind={entry.get('kind') or 'confirm'}）",
                              kind="merge")
    return entry is not None or popped_pmr


def _main_pending_push_text(pending, limit=3):
    """{仓库名: 领先提交数|None} → 「repo-a 领先 2 个提交；repo-b（远端无主干分支）」摘要。"""
    parts = []
    for name, ahead in sorted((pending or {}).items()):
        if len(parts) >= limit:
            parts.append("…")
            break
        if ahead is None:
            parts.append(f"{name}（远端无主干分支，无法统计）")
        else:
            parts.append(f"{name} 领先 {ahead} 个提交")
    return "；".join(parts)


def _set_done(req, dispatcher_touched=False, summary=None, push_failed=None, restart_failed=None,
              restart_ok=False, restart_warn="",
              main_push_failed=None, main_pending_push=None):
    """统一 done 出口：置 done + 清重试 + （req-board 自身项目）重启看板 + 通知。
    dispatcher_touched=True 时附加「需手动重启调度器」提醒（summary + 飞书 + 时间线）。
    push_failed 非空（#288：仅推分支项目 f-分支自动 push 失败仓库名集合）时附加「手动 push」
    提醒（summary + 时间线 + done 卡片红字，同样不阻断 done）。
    main_push_failed 非空（git_sync.on_done=auto：本地合并项目主干自动推送失败仓库名集合）时
    附加「手动推送主干」提醒（summary + 时间线 + done 卡片红字，同样不阻断 done）。
    main_pending_push 非空（git_sync.on_done=manual：{仓库名: 领先远端提交数|None}）时附加
    「主干待手动推送」提醒（summary + 时间线 + done 卡片红字三处，不阻断 done）。
    文案口径：仅推分支项目用「f-分支」措辞，本地合并项目统一用「主干/手动推送」。
    restart_failed 非空（#300：done 后按 restart 配方自动重启项目服务失败的原因字符串）时
    附加「需人工重启」提醒（summary + 时间线 + done 卡片红字，同样不阻断 done）。
    restart_ok（#318）：restart 配方已成功执行（调用方显式透传，未配置/失败不成立）时对
    todos 对账——纯重启类条目剔除、混合条目标注「调度器已自动重启」，避免 done 通知同时
    出现「已自动重启项目服务」与「待办：重启服务」半过时红字；对账在消费处统一完成，
    summary 留档与卡片展示保持一致。
    summary 可选：done 卡片正文展示完成摘要（为空显示「需求已完成」）。
    #279：agent 声明的遗留待办（todos）从 state["req_todos"] 消费——handle_result 解析
    result.json 时暂存（result 文件解析成功即删，done 流程无法再读），此处逐条写入 summary
    留档并随 done 卡片红字展示，消费后清理防 state 膨胀。
    #266 时序修复：先写 summary/通知，最后才重启看板——kickstart 重启窗口内看板 API
    不可达，若提前重启，提醒写入与 done 卡片会因 ConnectionRefused 中断（#264 现场）。"""
    req_id = str(req["id"])
    set_status(int(req_id), "done")
    # #25：done 收尾成功后单点清账——terminal=True 直接采信（置 done 前的 req 快照仍是旧
    # 状态，不能用来判终态）；同时清 pending_confirm（任意 kind）与 confirm_decisions 决策行
    # #27：条目身份守卫命中（键与内容不同源，内容属别的卡）→ 保留未清理，补留痕转人工
    purged = _purge_ghost_confirm(req_id, "需求已完成", terminal=True)
    if not purged:
        with state_lock:
            blocked = state["pending_confirm"].get(req_id)
        if isinstance(blocked, dict) and _pending_content_req_id(blocked, req_id) != int(req_id):
            cid = _pending_content_req_id(blocked, req_id)
            post_dispatcher_event(int(req_id),
                                  f"需求 #{req_id} 已完成，但待确认条目内容属需求 #{cid}"
                                  f"（与本卡不一致），已保留未清理，请人工处理",
                                  kind="confirm")
    with state_lock:
        state["retries"].pop(req_id, None)
        # #11：done 收尾一并清模型降级记录（防残留）
        state.setdefault("model_fallback", {}).pop(req_id, None)
        todos = state.setdefault("req_todos", {}).pop(req_id, None) or []
    save_state()
    reminder = ""
    if dispatcher_touched:
        # #380：不再只发手动提醒——登记 pending_restart 由主循环择机自动重启（kill -TERM 自身，
        # KeepAlive 拉起）；done 通知文案按是否存在并行 agent 分支（重启成功/失败的后续通知
        # 分别由新进程启动自证与重试耗尽兜底路径补发）。pending_restart 在函数末尾登记（见下）。
        with active_lock:
            busy = bool(active_runs)
        hint = DISPATCHER_AUTO_RESTART_DELAYED if busy else DISPATCHER_AUTO_RESTART_PENDING
        reminder = f"\n⚠️ {hint}"
        try:
            append_summary(req_id, f"⚠️ {hint}")
        except Exception as e:
            log.warning(f"需求 #{req_id} done 提醒写 summary 失败（不阻断通知）: {e}")
        post_dispatcher_event(int(req_id),
                              "检测到改动涉及 dispatcher.py，将在并行 agent 结束后自动重启调度器" if busy
                              else "检测到改动涉及 dispatcher.py，调度器将自动重启（无需手动操作）",
                              kind="merge")
    if push_failed:
        # 该参数仅由仅推分支项目 push_company_branches 产生
        push_hint = (f"⚠️ 仅推分支项目 f-分支自动 push 失败（{', '.join(sorted(push_failed))}），"
                     f"本地分支已保留，请手动 push（git push origin <分支>）")
        reminder = (reminder + "\n" + push_hint) if reminder else f"\n{push_hint}"
        try:
            append_summary(req_id, push_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done push 失败提醒写 summary 失败（不阻断通知）: {e}")
        post_dispatcher_event(int(req_id), "仅推分支项目 f-分支自动 push 失败，done 通知附「手动 push」提醒", kind="merge")
    if main_push_failed:
        main_push_hint = (f"⚠️ 主干自动推送失败（{'、'.join(sorted(main_push_failed))}），"
                          f"本地合并已保留，请手动推送主干（git push <远端> <主干>）")
        reminder = (reminder + "\n" + main_push_hint) if reminder else f"\n{main_push_hint}"
        try:
            append_summary(req_id, main_push_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done 主干推送失败提醒写 summary 失败（不阻断通知）: {e}")
        post_dispatcher_event(int(req_id),
                              "主干自动推送失败，done 通知附「手动推送主干」提醒", kind="merge")
    if main_pending_push:
        pending_hint = (f"⚠️ 主干有提交待手动推送：{_main_pending_push_text(main_pending_push)}"
                        f"（git push <远端> <主干>）")
        reminder = (reminder + "\n" + pending_hint) if reminder else f"\n{pending_hint}"
        try:
            append_summary(req_id, pending_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done 待手动推送提醒写 summary 失败（不阻断通知）: {e}")
        post_dispatcher_event(int(req_id),
                              f"主干待手动推送（{_main_pending_push_text(main_pending_push)}），"
                              f"done 通知附提示", kind="merge")
    if restart_failed:
        restart_hint = f"⚠️ 项目服务自动重启失败：{restart_failed}，需人工重启"
        reminder = (reminder + "\n" + restart_hint) if reminder else f"\n{restart_hint}"
        try:
            append_summary(req_id, restart_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done 重启失败提醒写 summary 失败（不阻断通知）: {e}")
        post_dispatcher_event(int(req_id), f"项目服务自动重启失败：{restart_failed}，需人工重启", kind="merge")
    if restart_warn:
        warn_hint = f"⚠️ {restart_warn}"
        reminder = (reminder + "\n" + warn_hint) if reminder else f"\n{warn_hint}"
        try:
            append_summary(req_id, warn_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done 重启判定告警写 summary 失败（不阻断通知）: {e}")
        post_dispatcher_event(int(req_id), restart_warn, kind="merge")
    display_todos = todos
    if todos:
        # #318：restart 配方已成功执行（restart_ok，调用方显式透传）时对账——纯重启类待办
        # 剔除、混合条目标注「调度器已自动重启」，保留浏览器验收等纯人工事项；未配置 restart
        # 或自动重启失败（restart_failed 非空，已有红字提醒）不过滤，行为不变。
        if restart_ok:
            display_todos = _reconcile_restart_todos(todos)
            dropped = len(todos) - len(display_todos)
            if dropped:
                post_dispatcher_event(int(req_id),
                                      f"restart 配方已自动执行，剔除重启类待办 {dropped} 条", kind="merge")
        for todo in display_todos:
            try:
                append_summary(req_id, f"⚠️ 待办：{todo}")
            except Exception as e:
                log.warning(f"需求 #{req_id} done 待办写 summary 失败（不阻断通知）: {e}")
        if display_todos:
            post_dispatcher_event(int(req_id), f"done 通知展示 agent 声明的遗留待办 {len(display_todos)} 条（红字）", kind="merge")
    if PROJECTS[req["project"]].get("company"):
        notify_req_card("done", req, summary=summary, company=True, reminder=reminder, todos=display_todos)
    else:
        notify_req_card("done", req, summary=summary, company=False, reminder=reminder, todos=display_todos)
    # 重启放最后：先确保上面所有看板写入与飞书通知完成
    _kickstart_board_if_own(req["project"], req["id"])
    if dispatcher_touched:
        # #380：pending_restart 必须最后登记（done 通知/合并/看板重启全部完成后）——主循环
        # 下一轮才据此 kill 自身，天然满足「先写通知最后重启」（#266 时序）。
        _queue_auto_restart(req_id)

def finalize_done(req, summary=None):
    """需求到达 done 的统一出口：合并 dev 分支 → 清理 worktree → done。
    合并冲突则保留分支/worktree，先自动拉起 merge_resolve 会话解决（#396，配额 2 轮/15 分钟），
    失败/耗尽/语义冲突再转 pending_confirm（kind=merge）由用户处理后重试。
    summary 可选：done 卡片正文的完成摘要（dev 完成路径透传，accept/merge 重试路径不传）。"""
    req_id = str(req["id"])
    # git_sync（项目级仓库同步/推送）：None=未配置 → 本函数所有 git_sync 分支零动作
    sync = _project_git_sync(req.get("project"))
    with state_lock:
        rec = state.get("worktrees", {}).get(req_id)
    if not rec:
        _set_done(req, summary=summary)
        return
    # #106：改动文件清单必须在合并前捕获——--no-ff 合并后 branch 成为 main 祖先，
    # 三点 diff {base}...{branch} 恒为空（合并后检测恒 False，重启提醒失效）
    dispatcher_touched = _touches_dispatcher(req, rec["branch"])
    changed = changed_repos(req, rec["branch"])
    # #394：合并改动文件清单同样必须在合并前采集（同 #106 三档判定依据）；
    # 仅开关开启时采集（开关未开维持 #300 行为，判定不介入、无额外 git 调用）
    merge_files = (_collect_merge_changed_paths(req, rec["branch"])
                   if PROJECTS[req["project"]].get("auto_restart_after_merge") else None)
    if PROJECTS[req["project"]].get("company"):
        # 仅推分支项目（branch_only）：main 由外部 CI/CD 或人工发版时合并，dev 只建分支——不合并；
        # 按实际 diff 收口：cleanup 前把有改动仓库的 f-分支 push 到远端（只 push 分支本身、
        # 不 --force/不推其他 refs），push 失败不阻断 done。
        push_failed = push_company_branches(req, rec["branch"], changed)
        cleanup_dev_worktrees(int(req_id), delete_branch="changed", keep_repos=changed)
        _set_done(req, dispatcher_touched=dispatcher_touched, summary=summary,
                  push_failed=push_failed)
        _annotate_extra_repos(req, changed)
        return
    # git_sync：合并前 pull 档再同步一次（主工作区拉齐远端最新；失败只 warn+事件，不阻断合并）。
    # 这里不设 mode 覆盖，沿用 before_card 配置（fetch 档不合并、off 档零动作）。
    if sync and sync.get("before_card") == "pull":
        _sync_main_worktrees(req, sync)
    ok, failed = merge_dev_branches(req)
    if not ok:
        # git_sync 主干守卫：主工作区不在主干 → 不合并（绝不在错分支落 dev），走人工通道且
        # 文案明确「切回主干后重试」，不得落到下面「工作区脏」口径误导。
        guard = _merge_guard_blockers(req, failed)
        if guard:
            _merge_conflict_fallback(req, rec, failed, guard=guard)
            return
        # main 工作区未提交改动挡住合并：merge_resolve（改 dev 分支）无从解决，直接转人工并列出文件
        blockers = _merge_dirty_blockers(req, failed)
        if blockers:
            _merge_conflict_fallback(req, rec, failed, blockers=blockers)
            return
        # #396：先自动拉起 merge_resolve 会话解决（配额 2 轮/15 分钟）；失败/耗尽转现有
        # 飞书 y/n + 网页按钮人工通道（_merge_conflict_fallback，行为与 #386 前一致）
        if _start_merge_resolve(req, rec, failed, summary=summary):
            return
        _merge_conflict_fallback(req, rec, failed)
        return
    cleanup_dev_worktrees(int(req_id), delete_branch=True)
    _annotate_extra_repos(req, changed)
    post_dispatcher_event(int(req_id),
                          f"自动合并 dev 分支 {rec['branch']} 到 main，清理 worktree，需求完成"
                          + (f"（实际改动仓库：{', '.join(sorted(changed))}）" if changed else ""),
                          kind="merge")
    # git_sync.on_done：auto=推送主干（禁 force、非快进 fetch+ff 重试一次）；manual=只统计待推；
    # off/未配置=零动作。失败只进 _set_done 三处提醒，绝不阻断 done、不丢本地合并。
    main_push_failed, main_pending_push = _push_done_worktrees(req, sync)
    # #300/#394：本地合并成功后按项目开关自动重启项目服务（成功发 merge 事件；失败原因经
    # _set_done 落 summary + 时间线 + done 卡片红字，不阻断 done；unknown 保守不重启 + 告警）
    restart_failed, restart_ok, restart_warn = _maybe_auto_restart_after_merge(
        req, merge_files, req_id)
    # #318：对账判定「restart 配方已成功执行」（未配置/失败不成立），供 _set_done 过滤重启类待办
    # 新参数只在有值时传：既有测试/调用方用固定签名 mock _set_done 的兼容性不受影响。
    extra = {}
    if main_push_failed:
        extra["main_push_failed"] = main_push_failed
    if main_pending_push:
        extra["main_pending_push"] = main_pending_push
    _set_done(req, dispatcher_touched=dispatcher_touched, summary=summary,
              restart_failed=restart_failed, restart_ok=restart_ok, restart_warn=restart_warn,
              **extra)

def _merge_resolve_entry(req_id):
    """merge_resolve 状态条目（无则空 dict）"""
    with state_lock:
        return dict(state.get("merge_resolve", {}).get(str(req_id), {}) or {})


def _start_merge_resolve(req, rec, failed, summary=None):
    """#396 finalize_done 合并冲突：登记状态并拉起 merge_resolve 会话（配额内）。
    返回 True=已拉起（冲突收尾转入自动解决流程）；False=配额耗尽/不可用，调用方走人工通道。"""
    req_id = str(req["id"])
    with state_lock:
        entry = state.setdefault("merge_resolve", {}).get(req_id) or {}
        attempts = int(entry.get("attempts", 0) or 0)
        if attempts >= MERGE_RESOLVE_MAX_ROUNDS:
            return False
        state["merge_resolve"][req_id] = {"req": req, "failed": list(failed),
                                          "attempts": attempts + 1,
                                          "branch": rec["branch"],
                                          "summary": summary,
                                          "started_at": time.time()}
    save_state()
    try:
        append_summary(req_id,
                       f"自动合并冲突（{', '.join(failed)}），拉起「冲突解决」会话自动处理"
                       f"（第 {attempts + 1} 轮，上限 {MERGE_RESOLVE_MAX_ROUNDS} 轮）；失败将转飞书/网页人工通道。")
    except Exception as e:
        log.warning(f"需求 #{req_id} merge_resolve 启动写 summary 失败（不阻断）: {e}")
    post_dispatcher_event(req_id,
                          f"自动合并冲突（{', '.join(failed)}），拉起「冲突解决」会话（第 {attempts + 1} 轮）",
                          kind="merge")
    log.warning(f"需求 #{req_id} 合并冲突，拉起 merge_resolve（第 {attempts + 1} 轮）: {failed}")
    spawn_agent(req, role="merge_resolve")
    return True


def _merge_resolve_retry(req, reason):
    """#396 merge_resolve 进程级失败重试（配额内，独立计数不污染 dev 重试语义）。
    返回 True=已重试；False=配额耗尽，调用方走人工通道。"""
    req_id = str(req["id"])
    with state_lock:
        entry = state.get("merge_resolve", {}).get(req_id)
        if not entry or int(entry.get("attempts", 0) or 0) >= MERGE_RESOLVE_MAX_ROUNDS:
            return False
        entry["attempts"] = int(entry.get("attempts", 0) or 0) + 1
    save_state()
    post_dispatcher_event(req_id,
                          f"merge_resolve 运行失败（{reason}），第 {entry['attempts']} 次自动重试",
                          kind="retry")
    spawn_agent(req, role="merge_resolve", resume=True,
                extra=f"上次尝试失败（第 {entry['attempts']} 次）：{reason}。"
                      "先确认改动完好（git status / 冲突文件是否已解决）、worktree 内冲突是否仍在，"
                      "再继续解决并提交；如属语义冲突需要人工决策，直接报 need_confirm。")
    return True


def _merge_conflict_fallback(req, rec, failed, question=None, reason=None, blockers=None, guard=None):
    """#396 merge_resolve 失败/语义冲突兜底：转现有飞书 y/n + 网页按钮人工通道（行为与 #386 前一致——
    pending_confirm(kind=merge) + 红卡 + 按钮登记；分支/worktree 现场保留）。
    blockers（_merge_dirty_blockers 结果）非空 = main 工作区未提交改动挡住合并，提示文本改为列出文件。
    guard（_merge_guard_blockers 结果）非空 = git_sync 主干守卫拦下（主工作区不在主干），
    提示「切回主干后重试」，不得用「工作区脏」文案误导（两者互斥，guard 优先判定）。"""
    req_id = str(req["id"])
    rec = rec or {}
    branch = rec.get("branch") or dev_branch(req)
    with state_lock:
        state.get("merge_resolve", {}).pop(req_id, None)
    save_state()
    if guard:
        detail = _branch_guard_text(guard)
        append_summary(int(req_id),
                       f"自动合并被主干守卫拦下（{detail}）：git_sync 启用时不把 dev 分支合并到"
                       f"非主干分支；已保留分支 {branch} 与 worktree。请切回主干后点「重试合并」"
                       f"或回复 y 重试，或 n 人工接管")
        post_dispatcher_event(int(req_id),
                              f"自动合并被主干守卫拦下（{detail}），未执行合并——"
                              f"保留分支 {branch} 与 worktree 待人工处理", kind="merge")
        question = question or f"主工作区不在主干分支（{detail}），dev 分支 {branch} 未合并，需切回主干后重试"
    elif blockers:
        detail = _dirty_blockers_text(blockers)
        append_summary(int(req_id),
                       f"自动合并被 main 工作区未提交改动挡住（非代码冲突：{detail}），已保留分支 {branch} 与 worktree；"
                       "请先提交或 stash 这些改动，再点「重试合并」或回复 y 重试，或 n 人工接管")
        post_dispatcher_event(int(req_id),
                              f"自动合并被 main 工作区未提交改动挡住（{detail}），非代码冲突，"
                              f"未拉起 merge_resolve——保留分支 {branch} 与 worktree 待人工处理", kind="merge")
        question = question or f"dev 分支 {branch} 合并被 main 工作区未提交改动挡住（{detail}），需先提交或 stash"
    elif reason:
        append_summary(int(req_id),
                       f"自动合并冲突且 merge_resolve 未解决（{reason}），已保留分支 {branch} 与 worktree；"
                       "请在对应仓库解决冲突后回复 y 重试合并，或 n 人工接管")
        post_dispatcher_event(int(req_id),
                              f"自动合并冲突，merge_resolve 未解决（{reason}）——"
                              f"保留分支 {branch} 与 worktree 待人工处理", kind="merge")
    else:
        append_summary(int(req_id),
                       f"自动合并冲突（{', '.join(failed)}），已保留分支 {branch} 与 worktree；"
                       "请在对应仓库解决冲突后回复 y 重试合并，或 n 人工接管")
        post_dispatcher_event(int(req_id),
                              f"自动合并冲突（{', '.join(failed)}），保留分支 {branch} 与 worktree 待人工处理",
                              kind="merge")
    entry = {"req": req, "question": question or f"自动合并 dev 分支 {branch} 冲突，需人工处理",
             "kind": "merge"}
    with state_lock:
        ok = _put_pending_confirm(req_id, entry)
    save_state()
    if not ok:
        _pending_source_mismatch_note(req_id, entry, "合并冲突重挂")
    log.error(f"需求 #{req_id} 合并冲突: {failed}")
    notify_req_card("merge_conflict", req, failed=failed, branch=branch)
    # #386：登记网页端「重试合并/人工接管」按钮信号（失败降级=按钮不可见，飞书通道仍可用，不阻断收尾）
    merge_conflict_write(req_id)


def _finish_merge_resolve(req, req_id, agent_summary=""):
    """#396 merge_resolve done（已在 worktree 内把 main 合并进 dev 分支并提交）：
    调度器恢复 main worktree 干净现场（git merge --abort，恢复动作不进 agent 权限）→
    重试 merge_dev_branches；成功则清理 worktree + _set_done；仍冲突转人工通道（fail-closed）。"""
    with state_lock:
        entry = state.get("merge_resolve", {}).pop(req_id, None) or {}
        rec = state.get("worktrees", {}).get(req_id)
    save_state()
    dev_summary = entry.get("summary")
    if agent_summary:
        try:
            append_summary(req_id, f"合并冲突已由 agent 解决：{format_summary_points(agent_summary)}")
        except Exception as e:
            log.warning(f"需求 #{req_id} merge_resolve 解决说明写 summary 失败（不阻断）: {e}")
    if rec:
        # 冲突现场在 main worktree（=主工作区，agent 被 #303/#336 硬拦禁止写）：
        # 重试前必须由调度器先 git merge --abort 清理 MERGE_HEAD，否则直接再跑 merge 被 git 拒绝
        for p in PROJECTS[req["project"]]["paths"]:
            try:
                subprocess.run(["git", "merge", "--abort"], cwd=p, capture_output=True,
                               timeout=DIAG_TIMEOUT)
            except Exception:
                pass
    dispatcher_touched = bool(rec) and _touches_dispatcher(req, rec["branch"])
    ok, failed2 = merge_dev_branches(req)
    if not ok:
        guard = _merge_guard_blockers(req, failed2)
        if guard:
            append_summary(req_id,
                           f"merge_resolve 解决后重试合并仍被主干守卫拦下"
                           f"（{_branch_guard_text(guard)}），转人工通道处理")
            _merge_conflict_fallback(req, rec, failed2, guard=guard)
            return
        blockers = _merge_dirty_blockers(req, failed2)
        append_summary(req_id,
                       f"merge_resolve 解决后重试合并{_merge_fail_text(failed2, blockers)}，转人工通道处理")
        _merge_conflict_fallback(req, rec, failed2, blockers=blockers)
        return
    changed = changed_repos(req, rec["branch"]) if rec else set()
    cleanup_dev_worktrees(int(req_id), delete_branch=True)
    _annotate_extra_repos(req, changed)
    post_dispatcher_event(int(req_id),
                          f"merge_resolve 已解决冲突，自动合并成功，清理 worktree，需求完成"
                          + (f"（实际改动仓库：{', '.join(sorted(changed))}）" if changed else ""),
                          kind="merge")
    restart_failed = run_project_restart(req["project"], req_id) or None
    restart_ok = restart_failed is None and bool(PROJECTS[req["project"]].get("restart"))
    _set_done(req, dispatcher_touched=dispatcher_touched, summary=dev_summary,
              restart_failed=restart_failed, restart_ok=restart_ok)


def _annotate_extra_repos(req, changed):
    """done 反馈闭环：实际改动仓库含清单外（analysis/notes 未识别）时事件 + summary 标注。"""
    focus = set(effective_repos(req))
    extra = changed - focus
    if not extra:
        return
    names = ", ".join(sorted(extra))
    post_dispatcher_event(int(req["id"]),
                          f"实际改动含清单外仓库：{names}（analysis/notes 未识别，已按实际改动处理）",
                          kind="merge")
    append_summary(int(req["id"]), f"实际改动仓库含清单外：{names}（analysis/notes 未识别）。")

def _stale_worktree_blocker(rec):
    """残留 worktree 能否安全回收：逐仓库校验「worktree 无未提交改动」+「dev 分支已合并入主干」。
    返回阻塞原因（str），可回收返回 ""。人工置 done 而分支未合并时不得强删（会丢内容）。"""
    branch = (rec or {}).get("branch")
    if not branch:
        return "记录缺分支名"
    cfg = PROJECTS.get((rec or {}).get("project") or "", {})
    for repo, wt in rec.get("worktrees") or []:
        name = Path(repo).name
        if Path(wt).exists():
            r = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                               capture_output=True, text=True, timeout=DIAG_TIMEOUT)
            if r.returncode != 0:
                return f"{name}: worktree 状态不可读"
            if r.stdout.strip():
                return f"{name}: worktree 有未提交改动（{len(r.stdout.strip().splitlines())} 项）"
        start, _ = _default_branch(repo, cfg)
        if not start:
            return f"{name}: 主干分支探测失败"
        r = subprocess.run(["git", "merge-base", "--is-ancestor", branch, start], cwd=repo,
                           capture_output=True, text=True, timeout=DIAG_TIMEOUT)
        if r.returncode != 0:
            return f"{name}: 分支 {branch} 未合并入主干 {start}"
    return ""

def _warn_stale_worktree(req_id, rec, status, why):
    """不可回收的残留 worktree 告警（同一原因只发一次，恢复可回收后清标记）。"""
    key = str(req_id)
    with state_lock:
        warned = state.setdefault("stale_wt_blocked", {})
        if warned.get(key) == why:
            return
        warned[key] = why
    save_state()
    branch = (rec or {}).get("branch")
    log.warning(f"需求 #{req_id} 残留 worktree 未回收（状态 {status}）：{why}；分支 {branch} 与现场保留")
    post_dispatcher_event(int(req_id),
                          f"残留 worktree 未自动回收：{why}；分支 {branch} 与 worktree 保留待人工确认")

def _clear_stale_worktree_warning(req_id):
    """回收成功后清告警标记（下次再阻塞能重新告警）。"""
    key = str(req_id)
    with state_lock:
        warned = state.setdefault("stale_wt_blocked", {})
        if warned.pop(key, None) is None:
            return
    save_state()

def cleanup_stale_worktrees():
    """清理 worktree 记录对应需求已 done/dropped/不存在时的残留（启动 + 主循环定期对账）。

    #428：人工合并、人工置 done 不走调度器收尾，残留 worktree/分支会一直留到下次重启——
    故除启动外由 _maybe_sweep_stale_worktrees 定期执行（原实现只在 main() 启动时跑一次；
    :461 的 "dropped" 判断保留作兜底，清理迁移遗漏的存量 dropped 行残留 worktree）。

    安全闸：只有「dev 分支已合并入各仓库主干」且「worktree 无未提交改动」才回收；否则保留
    现场 + 一次告警。定期执行不得放大强删风险——人工置 done 但分支未合并时删分支会丢内容。"""
    with state_lock:
        records = dict(state.get("worktrees", {}))
    if not records:
        return
    try:
        reqs = {str(r["id"]): r for r in api("GET", "")}
    except Exception as e:
        log.warning(f"清理残留 worktree 拉取需求失败: {e}")
        return
    for req_id, rec in records.items():
        status = (reqs.get(req_id) or {}).get("status")
        if not (status in ("done", "dropped") or reqs.get(req_id) is None):
            continue
        why = _stale_worktree_blocker(rec)
        if why:
            _warn_stale_worktree(req_id, rec, status, why)
            continue
        log.info(f"清理需求 #{req_id} 的 dev worktree（状态 {status}）：分支已合并且现场无未提交改动")
        cleanup_dev_worktrees(int(req_id), delete_branch=True)
        _clear_stale_worktree_warning(req_id)

# 残留 worktree 巡检间隔（秒）：只在到点的 tick 真查（有 git 开销），默认 5 分钟
STALE_WORKTREE_INTERVAL = int(os.environ.get("REQ_BOARD_STALE_WT_SEC", "300"))
_last_stale_wt_sweep = 0.0

def _maybe_sweep_stale_worktrees():
    """主循环低频巡检（#428）：人工收尾不触发调度器 done 路径，靠定期对账回收残留。"""
    global _last_stale_wt_sweep
    now = time.monotonic()
    if now - _last_stale_wt_sweep < STALE_WORKTREE_INTERVAL:
        return
    _last_stale_wt_sweep = now
    cleanup_stale_worktrees()

# #431 空会话行（session_id 为空、未被首条消息接管）清理：TTL 秒（0 = 关闭定期清理）；
# 巡检间隔秒。删除闸门在看板 DELETE 端点（有未消费人工命令/真实会话一律 409），调度器只做候选筛选。
EMPTY_SESSION_TTL_SEC = int(os.environ.get("REQ_BOARD_EMPTY_SESSION_TTL_SEC", "86400"))
EMPTY_SESSION_SWEEP_INTERVAL = int(os.environ.get("REQ_BOARD_EMPTY_SESSION_SWEEP_SEC", "1800"))
_last_empty_session_sweep = 0.0

def sweep_empty_sessions(max_age_sec=None):
    """#431 清理空会话注册行（候选筛选后逐个经看板 DELETE 端点删除，服务端可拒）。
    候选 = session_id 为空 + status=='created' + 非毒 + （max_age_sec is None 或 龄 > max_age_sec）；
    龄解析失败（None）不删（fail-closed）。max_age_sec=None = 启动存量清理（不限龄）。
    看板不可达单列 warn，异常不外抛（不阻断启动序列/主循环）。"""
    try:
        rows = api_board("GET", "/agent-sessions")
    except Exception as e:
        log.warning(f"空会话清理：看板不可达，本轮跳过（{e}）")
        return
    cand = []
    for r in (rows or []):
        if r.get("session_id") or r.get("status") != "created" or r.get("poisoned"):
            continue
        if max_age_sec is not None:
            age = _session_age_sec(r.get("created_at"))
            if age is None or age <= max_age_sec:
                continue
        cand.append(r)
    deleted = skipped_busy = skipped_missing = 0
    for r in cand:
        path = f"/requirements/{r.get('req_id')}/sessions/{r.get('id')}"
        try:
            api_board("DELETE", path)
            deleted += 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                skipped_busy += 1      # 有未消费人工命令/会话运行中（看板守卫）
            elif e.code == 404:
                skipped_missing += 1   # 行/需求不存在，或看板仍是旧代码（无 DELETE 路由）
            else:
                log.warning(f"空会话清理：删除 {path} 失败 HTTP {e.code}")
        except Exception as e:
            log.warning(f"空会话清理：删除 {path} 失败 {e}")
    if cand:
        log.info(f"空会话清理：候选 {len(cand)}，删除 {deleted}，跳过 409={skipped_busy}，"
                 f"跳过 404={skipped_missing}")

def _maybe_sweep_empty_sessions():
    """主循环低频巡检（#431）：TTL<=0 关闭；按 EMPTY_SESSION_SWEEP_INTERVAL 节流。"""
    global _last_empty_session_sweep
    if EMPTY_SESSION_TTL_SEC <= 0:
        return
    now = time.monotonic()
    if now - _last_empty_session_sweep < EMPTY_SESSION_SWEEP_INTERVAL:
        return
    _last_empty_session_sweep = now
    sweep_empty_sessions(max_age_sec=EMPTY_SESSION_TTL_SEC)

# ==================== #419 流程引擎（配置驱动 + 卡上快照） ====================
# 单一源 = flows.json（结构）+ prompts/*.md（提示词正文）；卡上快照 flow/flow_name/nodes_json/node_index
# 一经写入即冻结——改配置（删流程/改名/复用 id/增删节点）不影响进行中卡与历史卡。
# 无快照的历史卡按 mode/phase 懒映射到当前配置（与 backfill_flow_snapshots 同源映射，§4.2），
# 首次采纳流程即写快照冻结，此后推进只读快照。

def flow_of(req):
    """卡所在流程 id：flow 列命中配置 → 采用；否则 default_flow 兜底（存量卡零行为变化）"""
    fid = (req or {}).get("flow")
    return fid if flow_by_id(fid) else (default_flow_id() or "standard")

def flow_nodes(flow_id):
    f = flow_by_id(flow_id)
    return list(f["nodes"]) if f else []

def _snapshot_raw(req):
    """卡上快照原始解析：([nodes], index)；无/非法 → (None, None)"""
    raw, idx = (req or {}).get("nodes_json"), (req or {}).get("node_index")
    if not raw or idx is None:
        return None, None
    try:
        nodes = json.loads(raw)
        idx = int(idx)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(nodes, list) or not nodes or not 0 <= idx < len(nodes):
        return None, None
    return nodes, idx

def _legacy_position(req):
    """历史卡（无快照）按 mode/phase 懒映射到配置：(flow_id, node_index)；未入流程 → None。
    映射与 backfill_flow_snapshots(§4.2) 同源：auto → auto/0；semi → flow/phase 下标（phase 缺失 → 0）。
    已显式写过 node_index（推进中间态）时优先采信，避免映射把推进结果抹回起点。"""
    def _idx_or(fallback):
        raw = (req or {}).get("node_index")
        try:
            return int(raw) if raw is not None else fallback
        except (TypeError, ValueError):
            return fallback
    mode = (req or {}).get("mode")
    if mode == "auto":
        # auto 流程已随流程目录精简删除（自动通道 #419 后无产生路径：pending_route 无写入方），
        # flow_by_id("auto") 恒为假 → 此处恒取 flow_of(req)（= 兜底默认流程）；
        # 无快照的 mode=auto 卡因此落到默认流程首节点（实测无此类在途卡）
        fid = "auto" if flow_by_id("auto") else flow_of(req)
        return fid, _idx_or(0)
    if mode == "semi":
        fid = flow_of(req)
        phase = (req or {}).get("phase")
        if phase:
            for i, n in enumerate(flow_nodes(fid)):
                if n.get("role") == phase:
                    return fid, i
        return fid, _idx_or(0)
    return None

def flow_snapshot(req):
    """卡当前流程位置：(flow_id, nodes, node_index)；前置阶段（未入流程）→ (None, None, None)"""
    nodes, idx = _snapshot_raw(req)
    if nodes is not None:
        return ((req or {}).get("flow"), nodes, idx)
    pos = _legacy_position(req)
    if not pos:
        return (None, None, None)
    fid, idx = pos
    f = flow_by_id(fid)
    return (fid, list(f["nodes"]), idx) if f else (None, None, None)

def nodes_of(req):
    """卡当前节点序列（快照优先；历史卡懒映射）；前置阶段 → None"""
    return flow_snapshot(req)[1]

def node_index_of(req):
    return flow_snapshot(req)[2]

def current_node(req):
    """卡当前节点；前置阶段 → None"""
    _fid, nodes, idx = flow_snapshot(req)
    return nodes[idx] if nodes else None

def in_pre_stage(req):
    """卡是否处于前置环节（未入流程、无快照）——前置槽位契约按此判定（与角色名解耦）"""
    return nodes_of(req) is None

def node_display(node):
    """节点显示名：节点自身 name → 角色 display → 角色原文；terminal → 「收尾」"""
    if not node:
        return ""
    if node.get("name"):
        return str(node["name"])
    role = node.get("role")
    if not role:
        return "收尾"
    return role_meta(role, "display") or ROLE_NAMES.get(role) or role

def stage_of_review(req, review_role):
    """评审节点角色 → 被评审的前驱节点角色（tech_review→tech / code_review→dev）；无则 None"""
    nodes = nodes_of(req) or flow_nodes(flow_of(req))
    for i, n in enumerate(nodes):
        if n.get("role") == review_role and n.get("node_type") == "review":
            return nodes[i - 1].get("role") if i > 0 else None
    return None

def next_stage(req, phase):
    """phase（节点角色）的下一节点角色；末位（terminal/无后继）→ None"""
    nodes = nodes_of(req) or flow_nodes(flow_of(req))
    for i, n in enumerate(nodes):
        if n.get("role") == phase:
            nxt = nodes[i + 1] if i + 1 < len(nodes) else None
            return nxt.get("role") if nxt and nxt.get("role") else None
    return None

def role_for(req):
    """#419：前置阶段（无快照）→ pre.role（共享槽位，可配）；有快照 → 当前节点角色；
    terminal 节点不拉起 agent → None（收尾由推进链内联/启动扫描/主循环兜底执行，§1.5）"""
    node = current_node(req)
    if node is None:
        return pre_role()
    if (node.get("node_type") or "") == "terminal":
        return None
    return node.get("role")

def at_terminal_node(req):
    """收尾判据（#430）：当前节点存在且 node_type == 'terminal'。不可用「无角色」代替——
    配置降级时 role_for 恒为空串，会把未入流程的卡误判成收尾（PATCH done + 合分支不可回滚）。"""
    node = current_node(req)
    return bool(node) and (node.get("node_type") or "") == "terminal"

def role_allowed(req, role):
    """role 是否与卡当前节点匹配（#279 现场防复发：dev agent 直接调用 run_agent(req,"accept")
    绕过调度器拉起其它角色会话，会话无收尾、注册表残留 running 僵尸记录）。
    #419：放行 = 卡当前节点角色（无快照 → pre.role，含 reject 打回后指针已回退到前驱的情形）；
    terminal 行不拉起 agent。
    #430：空角色 fail-closed——降级下 role_for 恒为空串，`role == role_for(req)` 会把空角色
    当合法放行。
    #396：merge_resolve 仍按 state["merge_resolve"] 闸门放行（防 #279 式绕过直接拉起）。"""
    if not role:
        return False
    if role == "merge_resolve":
        return str(req["id"]) in state.get("merge_resolve", {})
    return role == role_for(req)

# #42 前缀缓存：公共尾静态化——常量不再过 str.format（build_prompt 常量直注 {common}），
# 时限数字/步数/结果路径等按卡值全部移到模板 {card_inputs} 之后的卡片输入块；JSON 示例改单层花括号。
# 8 条条目职责与措辞骨架不变（test_common_tail_* 等断言的静态文案全数保留）。
COMMON_TAIL = """
工作约束：
1. 时间：本次运行有每轮时限（具体分钟数见文末卡片输入），超时强制终止；优先最小可用实现，用 `date` 关注实际耗时。时间保底：约剩 5 分钟时先写结果 JSON result.json 保底（路径见文末卡片输入；可覆盖为最终结果）再收尾。
2. 步数：每轮工具调用有步数上限（具体步数见文末卡片输入；调度器强制，达到即取消本轮、只给一次限步收尾）；接近上限同样先写保底。
3. 结束前把结果 JSON 写入 result.json（路径见文末卡片输入）：{"status": "done|blocked|need_confirm", "summary": "做了什么/验证方式及结果/失败原因", "question": "仅 need_confirm 时填"}
   - done = 已完成并验证通过（只读角色 = 产物已写好）；blocked = 无法继续，summary 写原因；need_confirm = 需求有多种解释且影响方向或缺关键信息，带倾向建议追问
   - 可选字段 "todos"（字符串数组）：需人工处理的遗留待办，仅展示不自动执行；合并分支由调度器自动执行，不要声明；无则省略
   - 可选字段 "waive_gate"（字符串）：已完成但收尾校验预计不通过（如实现已由人工提交、分支相对基线无改动）时写豁免理由，非空即转人工裁决；不申报按校验失败处理
   - 可选字段 "options"（仅 need_confirm）：label/hint 文本数组（≤5 个），不得含 action 类字段
   - 可执行命令的角色用脚本或 python json.dumps 生成，禁止手工拼 JSON；只读角色用 Write/fs 工具写入后回读校验；字符串内换行写 \\n，裸换行 = 非法 JSON
4. 结束回合前必须过一遍检查单：a. 没有运行中或等待中的后台任务（含 run_in_background/nohup），有就先终止；b. 关键验证（全量测试、构建、启动自验）已在前台跑完，没跑就前台分块执行，禁止空轮询；c. result.json 已写入并回读可解析。禁止以「等待后台任务通知」结束回合——回合结束即会话结束，后台任务随进程丢失。
5. 重试/续跑场景（有上次会话或残留 result.json）先确认改动完好、无残留 result.json，再继续验证。
6. 状态与会话由调度器管理：不要调用看板 API 改需求状态；不得自行创建/驱动 ACP 会话（import dispatcher 调 run_agent/spawn_agent，或运行 kimi acp/kimi-chat-acp），会被拦截。
7. 本环境是无头 ACP 会话，没有交互终端：AskUserQuestion 等提问类工具会被直接 dismiss，不要使用；需要决策时结束回合，写 status: "need_confirm" + question（带倾向建议）。
8. 路径白名单：只读工具与只读 Bash 的可读 roots = 工作目录 + 项目各真实仓库根（main 工作区，用绝对路径）+ 本需求产物目录，worktree 父目录等其它路径会被拒；查其它需求用 `git worktree list` 或 db_query 查 requirements 表。写工具只能写 worktree、产物目录、/tmp，写主工作区会被取消本轮；目标代码不在 worktree 内时报 need_confirm，说明「需求可能建错项目/项目仓库清单缺配置」。

输出风格：
- 对话回复：结论先行，一般两三百字内；禁止开场白、客套与复述需求；多用 `-` 列表。
- 产物文档（analysis/prd/tech/self-test/accept）：每要点一行，删除过程叙述与铺垫；验收标准、证据清单、跨仓库改动清单必须完整保留，不得因精简缺失。
- result.json summary：结论 + 验证命令与关键结果，`- ` 列表逐行，一般不超过 10 行。
"""

# dev_auto/dev_semi 共享的要求第 2-8 条 + {common} 挂载点（#339 去重：改 dev 约束只改这里，
# 两模板仅开头输入说明与第 1 条范围表述不同）。拼接结果必须与原逐字模板字节一致
# （test_prompt_*/test_common_tail_* 等直接断言 prompt 文本）。
# #419：提示词正文单一源 = prompts/*.md（flows.json roles.<id>.prompt_file 指向，role_prompt() 读取）。
# 本表为派生兼容视图（引用点：/guide 提示词全文、若干测试）+ 内容均取自配置，不构成第二源；
# 新增/改提示词只改 prompts/*.md。dev_auto/dev_semi 是通道时代的旧键，与 dev 同指向 dev.md——
# 仅旧测试按键断言提示词内容时用，guide 只渲染 dev 一块（#419 后 dev 不再按通道拆行）。
ROLE_PROMPTS = {key: role_prompt(role) for key, role in (
    ("analysis", "analysis"), ("proposal", "proposal"), ("tech", "tech"),
    ("dev", "dev"), ("dev_auto", "dev"), ("dev_semi", "dev"), ("accept", "accept"),
    ("tech_review", "tech_review"),
    ("code_review", "code_review"), ("merge_resolve", "merge_resolve"),
)}

# #419 前置槽位输出契约：前置环节是全部后置流程共享的槽位（角色可换），强制产出
# suggested_flow（流程 id）与 repos（涉及仓库清单）。契约由槽位承载——build_prompt 在
# 「卡处于前置阶段（无快照）」时追加本段，与角色模板内容/角色名解耦（换 pre.role 也生效）。
# #419：可选流程目录由 _flow_catalog_text() 从配置生成（id｜适用场景｜节点序列），判据不再
# 写死在前置角色提示词里——增删流程/改 description/改节点序列都在配置侧一处生效。
def _flow_catalog_text():
    """可选流程目录（#419）：每条 = id（名称）｜description｜节点序列。
    description 是人工写的适用场景；节点序列是配置派生的硬判据（增删节点自动跟随，永不失真）。
    软禁用：只列启用流程——被软禁用（enabled: false）的流程不进目录，模型看不到也就不会推荐。"""
    lines = []
    for f in enabled_flows():
        seq = " → ".join(node_display(n) for n in (f.get("nodes") or []))
        desc = (f.get("description") or "").strip() or "（未说明适用场景）"
        lines.append(f"- {f['id']}（{f['name']}）｜{desc}｜节点：{seq}")
    return "\n".join(lines) or default_flow_id()


def _flow_reason_text(result, limit=200):
    """前置环节给的选流程理由（#419）：清洗成单行短文本——换行/多空格压成一个空格、截断到 limit 字；
    缺失或非字符串 → 空串（调用方显示「（未给理由）」）。只用于留档展示，不参与任何校验。"""
    raw = (result or {}).get("suggested_flow_reason")
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:limit]


PRE_SLOT_CONTRACT = """
前置环节输出契约（本环节为全部后置流程共享的槽位，要求与角色无关，由调度器强制）：
结束前把结果 JSON 写入 {result_json}，除 status/summary 外必须带：
- "suggested_flow": 推荐的后置流程 id（从下方可选流程中选一条**够用的最省**流程；产出缺失/非法时调度器按默认流程 {default_flow} 兜底并留痕，不阻塞）
- "suggested_flow_reason": 一句话说明命中了所选流程的哪条适用场景（≤200 字，不复述需求内容；可选）
- "repos": 本需求实际涉及改动的仓库目录名数组（名字须取自仓库清单；含仅需读不需改的仓库；识别不出可省略）
可选流程（id（名称）｜适用场景｜节点序列）：
{flow_catalog}
注意：repos 含仓库清单之外的目录名时转人工确认把关（疑似建错项目），不会直接进入开发。
"""


def pre_slot_contract_text(result_json="{result_json}"):
    """前置槽位契约全文：build_prompt 注入与 /guide 展示共用同一份格式化（避免两处各自
    format 后漂移）；可选流程目录取自配置里的启用流程（软禁用流程不出现）。"""
    return PRE_SLOT_CONTRACT.format(
        result_json=result_json,
        flow_catalog=_flow_catalog_text(),
        default_flow=default_flow_id())


def stage_input_for(req):
    """#419 输入规则（取代按角色名硬编码的映射与 prd.md 存在性探测）：
    - 前置槽位 = 序列第 0 个节点，输入 = 需求本身（title + notes，模板已内联）；
    - 第 N 个节点输入 = 所有已完成上游节点（第 0..N-1，含前置槽位）产物的「显示名 + 路径」清单；
      只按快照/配置列路径，不做存在性探测、不做内容判断（agent 自行按需读取）。"""
    _fid, nodes, idx = flow_snapshot(req)
    art = artifact_dir(req)
    pre_art = role_meta(pre_role(), "artifact")
    pre_label = f"{role_meta(pre_role(), 'display') or pre_role()}产物 {art / pre_art}" if pre_art else None
    # 前置槽位（序列第 0 节点）的产物：其输入 = 需求本身（title + notes）
    if nodes is None:
        return "需求本身（title + notes）"
    parts = ["需求本身（title + notes）"]
    if pre_label:
        parts.append(pre_label)
    for n in nodes[:idx]:
        fn = n.get("artifact")
        if fn:
            parts.append(f"{node_display(n)}产物 {art / fn}")
    return " + ".join(parts)

_COMMAND_LABELS = {"install": "安装依赖", "test": "全量测试", "test_affected": "受影响测试", "lint": "lint",
                   "typecheck": "类型检查", "build": "构建"}


def _project_repos(project):
    """项目仓库路径列表（宽容：未知项目 / 旧字符串形态 / 无 paths → []）。"""
    cfg = PROJECTS.get(project) if isinstance(PROJECTS, dict) else None
    if isinstance(cfg, str):
        return [cfg]
    paths = cfg.get("paths") if isinstance(cfg, dict) else None
    return [str(p) for p in paths] if isinstance(paths, list) else []


def project_conventions_text(project, role):
    """A2 第二层「项目约定」：由各仓库 .loop/project.json 渲染——常用命令、测试实例、按角色过滤的
    指引正文、AGENTS.md 指路；缺项显式写「未配置」，没有清单则明说未提供（A3：不让 agent 套用
    其它项目的命令/端口/脚本）。纯数据拼接，指引正文不过 str.format（花括号安全）。"""
    blocks = []
    for repo in _project_repos(project):
        m = load_project_manifest(repo)
        if not m["path"]:
            continue
        lines = [f"项目约定（{project}，来源 {m['path']}；与通用说明不一致时以此为准）："]
        cmds = [f"{_COMMAND_LABELS[k]} `{m['commands'][k]}`" for k in _COMMAND_LABELS if k in m["commands"]]
        if cmds:
            lines.append("- 常用命令：" + "；".join(cmds))
        inst = m["instance"]
        if inst:
            parts = [f"{label} `{inst[key]}`" for key, label in
                     (("start", "启动"), ("stop", "停止"), ("status", "状态"), ("healthcheck", "健康检查"))
                     if key in inst]
            if "port_range" in inst:
                parts.append(f"端口段 {inst['port_range'][0]}-{inst['port_range'][1]}")
            lines.append("- 测试实例：" + "｜".join(parts))
        if (Path(repo) / "AGENTS.md").is_file():
            lines.append("- 项目规范：先读工作目录仓库根的 AGENTS.md（改动目录下另有 AGENTS.md 时就近优先）")
        missing = [_COMMAND_LABELS[k] for k in _COMMAND_LABELS if k not in m["commands"]]
        if not inst:
            missing.append("测试实例")
        if missing:
            lines.append("- 未配置：" + "、".join(missing)
                         + "（需要时按仓库 AGENTS.md 约定；没有约定就如实说明，不要假设存在对应脚本或端口）")
        for name, g in m["guides"].items():
            if g["roles"] is None or role in g["roles"]:
                lines.append(f"【项目指引：{name}】\n{g['text']}")
        blocks.append("\n".join(lines))
    if not blocks:
        return (f"\n项目约定（{project}）：本项目未提供 .loop/project.json，测试命令、测试实例、重启方式均未配置——"
                "按各仓库 AGENTS.md 约定执行；没有约定就如实说明，不要假设存在实例脚本或固定端口。"
                "起不了测试实例时：验证退化为可执行的单元/脚本级验证，报告里写明「未做实例/浏览器级验证 + 原因」，"
                "需人工验收的项列出来（dev 写进 todos，accept 写进未覆盖项）。\n")
    return "\n" + "\n\n".join(blocks) + "\n"


def role_skill_names(role):
    """flows.json roles.<id>.skills 声明的技能名（未知角色 / 未配置 → []）。"""
    names = role_meta(role, "skills")
    return list(names) if isinstance(names, list) else []


def scenario_skills_text(project, role):
    """A2 第三层「场景技能」：flows.json 角色声明 + 项目清单 skills.<角色> 点名的技能片段，
    项目技能（.loop/skills/）优先于全局同名（prompts/skills/）；找不到的技能记日志跳过。"""
    repos = _project_repos(project)
    names = list(role_skill_names(role))
    for repo in repos:
        for n in (load_project_manifest(repo)["skills"].get(role) or []):
            if n not in names:
                names.append(n)
    parts = []
    for n in names:
        text = skill_text(n, repos)
        if text is None:
            log.warning(f"项目 {project} 角色 {role} 声明的技能 {n} 未找到（已跳过）")
            continue
        parts.append(f"【场景技能：{n}】\n{text}")
    return ("\n" + "\n\n".join(parts) + "\n") if parts else ""


# #42 前缀缓存：固定段/可变段切分标记（模板独占一行）与固定段占位符白名单。
# 标记之前只允许这四键；{repo_listing}/{repo_map_hint}/{id}/{notes}/{extra}/{merge_ctx} 等按卡值
# 一律放标记之后的卡片输入块（P11 静态扫描强制，防按卡值回流固定段）。
CARD_INPUTS_MARK = "{card_inputs}"
FIXED_PART_PLACEHOLDERS = {"project", "project_goal_hint", "readonly_tool_guide", "common"}


def split_role_template(tmpl):
    """#42：按独占标记 {card_inputs} 把角色模板拆成（固定段, 卡片输入段）。
    标记缺失 → warning + 固定段=整模板、卡片段=空（回退现行为：功能不炸、逐字稳定失效；
    反方向「旧调度器 + 新模板」会在 format 缺 card_inputs 键时抛 KeyError——模板与代码必须同批生效）。"""
    if CARD_INPUTS_MARK not in tmpl:
        log.warning("角色模板缺 {card_inputs} 标记，整段按可变段回退（前缀缓存退化；"
                    "需更新 prompts/<role>.md 并重启调度器）")
        return tmpl, ""
    return tmpl.split(CARD_INPUTS_MARK, 1)


def build_prompt_parts(req, role, extra=""):
    """#42 前缀缓存：build_prompt 的拆段核心。返回按序四段：
    [固定段（角色 prose + 静态公共尾）, 项目约定（A2 层2）, 场景技能（A2 层3）, 卡片输入块（可变尾）]。
    逐字稳定的比较单位 = 同一（角色, 项目, 生效清单/技能/约定文件状态）；卡片内容/评审反馈/上游产物/
    运行参数变化只落在卡片输入块。两段模板用同一 kwargs 各自 format（str.format 忽略未用键），
    占位符名与 kwargs 全部不变。前置槽位契约与 kill 红线不在此拼装（build_prompt / run_agent 追加，
    二者含按卡/按进程值，不参与固定段逐字稳定口径）。"""
    # analysis 起全部角色在同一 per-requirement worktree（从主干拉取，基线=主干）工作，
    # 调研基线 = 开发基线，不随主工作区 checkout 漂移；跨阶段幂等复用同一 worktree/分支
    _, repo_map = ensure_dev_worktrees(req)
    repo = dev_repo_listing(req, repo_map, focus=effective_repos(req))
    # #419：提示词模板来自配置 prompts/<role>.md（单一源；dev_auto/dev_semi 合一为 dev.md）
    tmpl = role_prompt(role)
    if not tmpl:
        raise ValueError(f"未知角色: {role}")
    # #419：节点输入 = 上游节点产物清单（含前置槽位），不做角色名映射/不做 prd.md 探测
    stage_input = stage_input_for(req)
    if role == "dev" and PROJECTS[req["project"]].get("company"):
        extra = ((extra + "\n") if extra else "") + \
            "本项目为仅推分支项目（branch_only）：done 时调度器自动把有改动仓库的 f-分支 push 到 origin，" \
            "由外部 CI/CD 或人工在发版时合并到主干；你只在本分支提交，不要 push、不要合并任何分支、" \
            "不要操作 main 分支。待办应写「基于已推送的 f-分支完成测试验证」类表述，不要写「合并分支到 master」。"
    # #365：仅推分支项目在 build_prompt 注入待办口径（非仅推分支项目 accept 模板不含该句式）
    if role == "accept" and PROJECTS[req["project"]].get("company"):
        extra = ((extra + "\n") if extra else "") + \
            "本项目为仅推分支项目（branch_only）：done 时调度器自动 push f-分支，由外部 CI/CD 或人工在发版时" \
            "合并到主干；待办应写「基于已推送的 f-分支完成测试验证」类表述，不要写「合并分支到 master」。"
    # #318：项目配置了 restart 配方时注入「服务由调度器 done 时自动重启」提示——
    # 源头预防：todos 只声明纯人工事项（如浏览器验收），不声明重启服务/进程类待办
    if role == "dev" and PROJECTS[req["project"]].get("restart"):
        extra = ((extra + "\n") if extra else "") + \
            "本项目服务由调度器自动重启（done 时按 projects.json 的 restart 配方执行并做就绪校验，无需人工重启）；" \
            "todos 只声明纯人工事项（如浏览器验收），不要声明重启服务/进程类待办。"
    # #396：merge_resolve 冲突上下文注入（冲突仓库清单 + 解决方向）；时间盒覆盖为 15 分钟。
    # 评审整改（主问题 1）：主干引用不再硬编码 main——逐冲突仓库复用 _default_branch 探测
    # （base_branch 配置 → master → main → origin/HEAD，master 优先），注入各仓库具体 merge 目标
    merge_ctx = ""
    if role == "merge_resolve":
        with state_lock:
            entry = state.get("merge_resolve", {}).get(str(req["id"])) or {}
        failed = entry.get("failed") or []
        cfg = PROJECTS[req["project"]]
        lines = []
        for p in failed:
            try:
                base, base_sha = _default_branch(p, cfg)
            except Exception:
                base, base_sha = None, None
            ref = base or base_sha or "main"  # 探测全面失败回退 main（与原行为一致）
            lines.append(f"- {Path(p).name}（主干: {ref}）：git merge {ref}")
        merge_ctx = (f"冲突仓库（需解决）：{', '.join(failed)}\n"
                     "各仓库主干引用（merge 目标，调度器主干探测：base_branch 配置 → master → main → origin/HEAD）：\n"
                     + "\n".join(lines) + "\n"
                     "解决方向：在对应仓库 worktree 内执行上述 `git merge <该仓库主干>`（把主干合并进当前 dev 分支）→ "
                     "解冲突 → 提交；调度器随后恢复主工作区合并现场并自动重试合并 dev 分支回各仓库主干，收尾无需你处理。")
    result_json = result_file_path(req)
    art_root = str(artifact_dir(req))
    repo_map_path = PROJECTS[req["project"]].get("repo_map") or ""
    repo_map_hint = (f"仓库地图（跨仓库定位代码的第一来源，先读再分析）：{repo_map_path}\n"
                     if repo_map_path else "")
    # #5 goal 注入：项目目标进 analysis 上下文（{project_goal_hint} 整行占位符，无 goal 不渲染；
    # goal 是 projects.json 项目级常量，与卡无关 → 留固定段）
    project_goal = (PROJECTS.get(req["project"]) or {}).get("goal") or ""
    project_goal_hint = (f"项目目标（goal）：{project_goal}\n" if project_goal else "")
    # 评审反馈直接内联（tech/dev 模板卡片输入块 {review_feedback}）：有 summary（否决原因/历史记录）给全文，
    # 无则显式声明不存在，避免 agent 自行探索数据库找「评审反馈」
    # C4：summary 折叠早期记录后注入；模板已有 {review_feedback} 时 {history} 置空（原 tech/dev 全文注入两遍）
    summary = compact_summary(req.get("summary"))
    review_feedback = (f"开发总结（评审反馈/历史记录）：\n{summary}\n" if summary
                       else "无附加反馈（评审通过或尚无否决记录）。")
    history = (f"历史记录（前几次尝试的总结/评审反馈）：{summary}\n"
               if summary and "{review_feedback}" not in tmpl else "")
    # #42：common 常量直注（静态化后不再按卡 format）；时限数字/步数/结果路径由卡片输入块消费
    kw = dict(
        project=req["project"], id=req["id"], title=req["title"],
        notes=req.get("notes") or "（无）",
        repo_listing=repo,
        repo_map_hint=repo_map_hint,
        project_goal_hint=project_goal_hint,
        branch=dev_branch(req),
        timeout_minutes=effective_timeout_minutes(req, role),
        max_steps=_steps_text(role, req),
        analysis_md=artifact_dir(req) / "analysis.md",
        prd_md=artifact_dir(req) / "prd.md",
        tech_md=artifact_dir(req) / "tech.md",
        self_test_md=artifact_dir(req) / "self-test.md",
        accept_md=artifact_dir(req) / "accept.md",
        diff_patch=artifact_dir(req) / "diff.patch",
        tech_review_md=artifact_dir(req) / "tech_review.md",
        code_review_md=artifact_dir(req) / "code_review.md",
        artifact_root=art_root,
        result_json=result_json,
        review_feedback=review_feedback,
        stage_input=stage_input,
        history=history,
        extra=(f"补充上下文：{extra}\n" if extra else ""),
        merge_ctx=merge_ctx,
        readonly_tool_guide=READONLY_TOOL_GUIDE,
        common=COMMON_TAIL,
        card_inputs="",
    )
    # #42 拆段：两段用同一 kwargs 各自 format（str.format 忽略未用键），占位符语义不变
    fixed_tmpl, card_tmpl = split_role_template(tmpl)
    fixed = fixed_tmpl.format(**kw)
    card = card_tmpl.format(**kw)
    # A2 三层注入顺序保持：固定段（通用协议 + 静态公共尾）→「项目约定」→「场景技能」→ 卡片输入块
    return [fixed,
            project_conventions_text(req["project"], role),
            scenario_skills_text(req["project"], role),
            card]


def build_prompt(req, role, extra=""):
    # 只读评审节点拿不到范围 diff → 调度器代写 diff.patch（上游含 code 节点时才有 diff，§1.4）
    _node = current_node(req)
    if _node and _node.get("node_type") == "review":
        if any(n.get("node_type") == "code" for n in (nodes_of(req) or [])[:node_index_of(req) or 0]):
            _gen_review_diff(req)
    # #42 前缀缓存拼装：固定段 → 项目约定 → 场景技能 → 卡片输入块（可变尾）
    prompt = "".join(build_prompt_parts(req, role, extra=extra))
    # #419 前置槽位契约：仅当卡处于前置阶段（未入流程、无快照）时追加——判定按槽位而非角色名，
    # 换 pre.role 后任何角色都会被告知 suggested_flow/repos 输出要求（流程内同角色节点不重复注入）。
    # 契约含按卡 result_json 与流程目录，保持追加在最末（不参与固定段逐字稳定口径）
    if in_pre_stage(req):
        prompt += pre_slot_contract_text(result_file_path(req))
    return prompt

# ==================== 飞书机器人 ====================
feishu = None
# #23：进程内最近加载的 app_secret，**仅用于错误文案脱敏**（不落盘、不入日志、不进心跳）
_FEISHU_SECRET = None


def _sanitize_feishu_error(text, secret=None):
    """#23 错误文案脱敏：替换 app_secret 明文、去掉换行、截断 300 字。异常文本一律经此出口。"""
    s = "" if text is None else str(text)
    for sec in (secret, _FEISHU_SECRET):
        if sec:
            s = s.replace(sec, "***")
    return s.replace("\r", " ").replace("\n", " ")[:300]

# #22 飞书凭据单一源：仓库根 feishu.json = {"app_id","app_secret"}（0600，gitignore，设置抽屉维护）。
# REQ_BOARD_FEISHU_JSON 可覆盖（测试隔离）。dispatcher 仅启动时读一次，改凭据须重启调度器。
FEISHU_CONFIG_FILE = (Path(os.environ["REQ_BOARD_FEISHU_JSON"]).expanduser()
                      if os.environ.get("REQ_BOARD_FEISHU_JSON") else REPO_DIR / "feishu.json")
# #22：会话 boot 完成后才置 idle（board `_chat_boot` 异步），建会话后须轮询等待该窗口
FEISHU_BOOT_WAIT_SEC = int(os.environ.get("REQ_BOARD_FEISHU_BOOT_WAIT_SEC", "180"))
# #22：飞书建卡单轮发送上限（与 board 侧建卡会话轮次超时对齐；等待原语 D8‴/D11 复用）
CHAT_TIMEOUT = int(os.environ.get("REQ_BOARD_CHAT_TIMEOUT_SEC", "600"))
# #22：空闲飞书建卡会话兜底释放（第三条释放路径）；0 = 关闭兜底；巡检节流间隔（秒）
FEISHU_CHAT_IDLE_SEC = int(os.environ.get("REQ_BOARD_FEISHU_CHAT_IDLE_SEC", "86400"))
FEISHU_IDLE_SWEEP_INTERVAL = int(os.environ.get("REQ_BOARD_FEISHU_IDLE_SWEEP_SEC", "300"))
FEISHU_TEXT_CHUNK_SIZE = int(os.environ.get("REQ_BOARD_FEISHU_TEXT_CHUNK", "2000"))  # 单条飞书文本上限
FEISHU_TEXT_MAX_CHUNKS = 5                                                            # 最多分几条
FEISHU_PROMPT_MAX = 4000   # board 侧 /prompt 的 text 上限（含抓来的文档正文），超限先截断
# #23 首连超时兜底：SDK 异常路径卡住时仍要给出「连接失败：原因」（`_feishu_connect_watchdog`）
FEISHU_CONNECT_TIMEOUT_SEC = int(os.environ.get("REQ_BOARD_FEISHU_CONNECT_TIMEOUT_SEC", "60"))
# #24 飞书看板助手：单条消息的总 deadline（worker 起表 → 传剩余秒数给 board；board 权威计时收尾，
# dispatcher 只做超时兜底与明确回复）。与建卡 CHAT_TIMEOUT（600s）语义完全隔离。
FEISHU_ASSISTANT_DEADLINE_SEC = int(os.environ.get("REQ_BOARD_FEISHU_ASSISTANT_DEADLINE_SEC", "90"))
# 轮询间隔（事件 + 状态）
FEISHU_ASSISTANT_POLL_SEC = float(os.environ.get("REQ_BOARD_FEISHU_ASSISTANT_POLL_SEC", "1.5"))
# per-chat 已处理 message_id 的保留窗口（去重键 = feishu_chat_id + message_id）
FEISHU_ASSISTANT_SEEN_WINDOW = int(os.environ.get("REQ_BOARD_FEISHU_ASSISTANT_SEEN", "64"))
# 90s deadline 到点、board 未收尾时的固定回发文案（同轮只发一次）
FEISHU_ASSISTANT_TIMEOUT_TEXT = "这轮没查完，重发一次或到网页看。"
# 助手链路不可用/失败时的降级提示前缀（不静默丢消息）
FEISHU_ASSISTANT_DOWN_HINT = "看板助手暂时不可用，稍后重发一次"
FEISHU_ASSISTANT_BUSY_HINT = "助手上一轮仍在收尾，请重发一次"
# #24 无活跃旧建卡会话时「结束建卡」「确认建卡」的状态提示（只回提示，不启动任何会话）
FEISHU_NO_SESSION_END_HINT = "当前没有进行中的建卡会话。"
FEISHU_NO_SESSION_CONFIRM_HINT = "还没有草稿。回复『新建建卡』开始建卡会话。"
# #24 无活跃旧会话时「新建建卡」的起手提示（随后未定项目由旧链路追问项目清单）
FEISHU_NEW_BUILD_HINT = "好的，请把需求内容发过来；已确定项目时直接回复项目名（或序号）。"
# #24 助手模型配置（feishu.json 的可选 assistant_model）：未配置/非法 = None（走 board 四层默认），
# 解析原因（非敏感）留痕，供日志与测试断言；app_secret 永不进入这两个全局。
FEISHU_ASSISTANT_MODEL = None
FEISHU_ASSISTANT_MODEL_REASON = None


def feishu_credential_fingerprint(app_id, app_secret):
    """#23 凭据指纹（board/dispatcher **逐字同源**的唯一定值，见 tech §4.1）：
    sha256(app_id + b"\\x00" + app_secret).hexdigest()[:16]。**不做 strip、不做大小写归一化**；
    任一侧缺失/为空/非字符串 → None（不产出摘要）。给定值两侧逐字节相等。"""
    if not isinstance(app_id, str) or not isinstance(app_secret, str):
        return None
    if not app_id or not app_secret:
        return None
    return hashlib.sha256(app_id.encode("utf-8") + b"\x00"
                          + app_secret.encode("utf-8")).hexdigest()[:16]


def _feishu_credentials_source():
    """#23 凭据来源标识（非敏感，供界面显示加载自哪个文件）。"""
    return ("feishu.json（REQ_BOARD_FEISHU_JSON 覆盖）"
            if os.environ.get("REQ_BOARD_FEISHU_JSON") else "feishu.json")


def _read_feishu_config():
    """#23 统一凭据解析（唯一实现）：返回 (cfg|None, reason|None)。
    reason ∈ {missing, unreadable, invalid}——**「没读到凭据」绝不写成 env**（否则 board 会
    误判为用户显式停用，保存新凭据后进不了「尚未加载」态）。cfg={"app_id","app_secret"}。"""
    path = FEISHU_CONFIG_FILE
    try:
        if not path.exists():
            return None, "missing"
        text = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - 权限/IO 失败一律按不可读降级
        log.warning(f"飞书凭据读取失败（{path}）：{e}")
        return None, "unreadable"
    try:
        cfg = json.loads(text)
    except Exception:
        log.warning(f"飞书凭据解析失败（{path}）：JSON 非法")
        return None, "invalid"
    if not isinstance(cfg, dict):
        log.warning(f"飞书凭据结构不符（{path}）：顶层非对象")
        return None, "invalid"
    app_id = cfg.get("app_id")
    app_secret = cfg.get("app_secret")
    if (not isinstance(app_id, str) or not isinstance(app_secret, str)
            or not app_id or not app_secret):
        log.warning(f"飞书凭据不完整（{path}）：需要非空的 app_id 与 app_secret")
        return None, "invalid"
    return {"app_id": app_id, "app_secret": app_secret}, None


def load_feishu_config():
    """#22 读 feishu.json（REQ_BOARD_FEISHU_JSON 可覆盖，测试隔离）→ {app_id, app_secret}；
    缺失/非法 JSON/字段不全 → None（fail-soft，调用方降级为「飞书未启用」，绝不抛）。"""
    cfg, _reason = _read_feishu_config()
    return cfg


def _read_feishu_assistant_model():
    """#24：读 feishu.json 的可选 assistant_model（**只取该字段**，app_secret 明文不离开本函数、
    不进日志/状态）。文件缺失/不可读/JSON 非法/字段缺失 → None（fail-soft，调用方按未配置处理）。"""
    try:
        if not FEISHU_CONFIG_FILE.exists():
            return None
        cfg = json.loads(FEISHU_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 读取/解析失败一律降级为未配置
        log.warning(f"飞书助手模型读取失败（{FEISHU_CONFIG_FILE}）：{e}")
        return None
    if not isinstance(cfg, dict):
        return None
    return cfg.get("assistant_model")


def _feishu_assistant_model_resolve(raw):
    """#24：assistant_model 静态校验（与角色模型同一别名池口径，只新增判定不改进既有权衡）。
    返回 (model|None, reason|None)：未配置 = (None, None)；非法/不在池/在池但不可用 = (None, 原因)；
    池不可用（无 config.toml / 解析失败）→ fail-open 原样采用 + 原因留痕（与 resolve_role_model 一致）。"""
    if raw is None:
        return None, None
    if not isinstance(raw, str) or not raw.strip():
        return None, "assistant_model 非字符串或为空，按未配置处理"
    alias = raw.strip()
    pool, _home, warning = model_alias_pool()
    if pool is None:
        return alias, f"别名池不可用，未校验 assistant_model（{warning}）"
    entry = pool.get(alias)
    if entry is None:
        return None, f"assistant_model「{alias}」不在模型别名池，按未配置处理"
    if not entry.get("usable", True):
        return None, f"assistant_model「{alias}」不可用（{entry.get('reason') or '未说明'}），按未配置处理"
    return alias, None


def init_feishu():
    """#22/#23 fail-soft：无凭据 / 凭据非法 → 返回 None + log.warning（不抛），并把
    enabled=false + disabled_reason（missing/unreadable/invalid）写入心跳飞书状态。成功返回
    {"app_id","app_secret"}、建好全局 feishu 客户端，并写 enabled=true/connected=false/connecting
    + 已加载凭据指纹。REQ_BOARD_NO_FEISHU=1 时直接跳过（不读文件），写 disabled_reason=env。
    #24：凭据可用时顺带解析可选 assistant_model（fail-soft + 原因留痕），并按 chat_id 对账
    持久化的助手本地状态（清理僵尸 running/cancelling，保留 session_id）。"""
    global feishu, _FEISHU_SECRET, FEISHU_ASSISTANT_MODEL, FEISHU_ASSISTANT_MODEL_REASON
    src = _feishu_credentials_source()
    if NO_FEISHU:
        log.warning("REQ_BOARD_NO_FEISHU=1，显式停用飞书通道（不建立长连接）" if LARK_AVAILABLE
                    else "未安装飞书 SDK（lark_oapi），停用飞书通道（不建立长连接）")
        _update_feishu_status(enabled=False, disabled_reason="env", connected=False,
                              phase="disabled", credentials_source=src,
                              credentials_fingerprint=None, last_error=None,
                              connected_at=None)
        return None
    cfg, reason = _read_feishu_config()
    if not cfg:
        log.warning(f"飞书凭据未配置（{FEISHU_CONFIG_FILE}）：{reason}，不建立长连接")
        _update_feishu_status(enabled=False, disabled_reason=reason or "missing",
                              connected=False, phase="disabled", credentials_source=src,
                              credentials_fingerprint=None, last_error=None,
                              connected_at=None)
        return None
    _FEISHU_SECRET = cfg["app_secret"]
    FEISHU_ASSISTANT_MODEL, FEISHU_ASSISTANT_MODEL_REASON = _feishu_assistant_model_resolve(
        _read_feishu_assistant_model())
    if FEISHU_ASSISTANT_MODEL_REASON:
        log.warning(f"飞书助手模型未生效：{FEISHU_ASSISTANT_MODEL_REASON}")
    elif FEISHU_ASSISTANT_MODEL:
        log.info(f"飞书助手模型：{FEISHU_ASSISTANT_MODEL}")
    feishu = lark.Client.builder().app_id(cfg["app_id"]).app_secret(cfg["app_secret"]).build()
    _update_feishu_status(enabled=True, disabled_reason=None, connected=False,
                          phase="connecting", credentials_source=src,
                          credentials_fingerprint=feishu_credential_fingerprint(
                              cfg["app_id"], cfg["app_secret"]),
                          last_error=None, connected_at=None)
    _feishu_assistant_reconcile()
    return cfg

# 需求卡片长度限制（#115）：先截断后转义，保证转义对象不超字段上限
CARD_TITLE_LIMIT = 80    # 卡片正文需求标题
CARD_BODY_LIMIT = 500    # 卡片正文动态内容（问题/摘要/失败原因）
# #30 需求卡清单卡片：渲染侧硬上限（防未来 board 放宽或脏数据把一页塞爆飞书卡片元素数）
ASSISTANT_CARD_MAX_ITEMS = 20    # 单页最多渲染的需求卡区块数

# 七类关键状态通知事件映射：template 颜色 + 事件标题（#115/#385）
CARD_EVENTS = {
    "route": {"template": "blue", "title": "通道待确认"},
    "review": {"template": "orange", "title": "阶段待评审"},
    "need_confirm": {"template": "orange", "title": "需要确认"},
    "done": {"template": "green", "title": "开发完成"},
    "failed": {"template": "red", "title": "自动开发失败"},
    "blocked": {"template": "orange", "title": "需求阻塞"},
    "merge_conflict": {"template": "red", "title": "自动合并冲突"},
}

def _send_feishu(msg_type, content_obj, chat_id=None):
    """底层通用发送出口（text/interactive 共用）：chat_id 缺省仍取 state["chat_id"]（y/n 通道逐字不变）；
    飞书建卡按会话显式传 chat_id。未启用（feishu None）/未连接仅 log.info 记录返回 False；
    已连接发送失败 log.error（含 resp.code/msg）返回 False；成功返回 True"""
    if not feishu:
        log.info(f"[飞书未启用/未连接，消息仅记录] {msg_type}: {json.dumps(content_obj, ensure_ascii=False)[:200]}")
        return False
    chat_id = chat_id or state.get("chat_id")
    if not chat_id:
        log.info(f"[飞书未连接，消息仅记录] {msg_type}: {json.dumps(content_obj, ensure_ascii=False)[:200]}")
        return False
    req = (CreateMessageRequest.builder()
           .receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder()
                         .receive_id(chat_id)
                         .msg_type(msg_type)
                         .content(json.dumps(content_obj, ensure_ascii=False))
                         .build())
           .build())
    resp = feishu.im.v1.message.create(req)
    if not resp.success():
        log.error(f"飞书发送失败: {resp.code} {resp.msg}")
        return False
    return True

def notify(text, chat_id=None):
    """给用户发飞书文本消息（回执类）；chat_id 缺省取 state["chat_id"]（y/n 通道不变）。
    未建立会话/未启用飞书时只记日志。内部收敛至 _send_feishu"""
    if chat_id:
        _send_feishu("text", {"text": text}, chat_id)
    else:
        _send_feishu("text", {"text": text})

def _feishu_notify(chat_id, text):
    """飞书建卡按会话回发（显式 chat_id）；测试可 mock notify 统一捕获。"""
    notify(text, chat_id=chat_id)

def _feishu_typing(message_id, reaction_id=None):
    """#44「正在输入」反馈：飞书无原生 typing 接口，用用户消息上的 Typing 表情代替。
    reaction_id 为空 → create（成功返回 reaction_id）；有 → delete。失败只 log.info（不升级为故障、
    不重试、不做保活重贴）；feishu 未启用或缺 message_id 直接返回 None。
    create 成功但响应取不到 reaction_id → 返回 None 并在日志注明表情可能残留。"""
    if feishu is None or not message_id:
        return None
    if reaction_id:
        try:
            req = (DeleteMessageReactionRequest.builder()
                   .message_id(message_id)
                   .reaction_id(reaction_id)
                   .build())
            resp = feishu.im.v1.message_reaction.delete(req)
            if not resp.success():
                log.info(f"飞书 Typing 表情操作失败（{message_id}）：{resp.code} {resp.msg}")
        except Exception as e:  # noqa: BLE001 - 表情失败不影响业务回复
            log.info(f"飞书 Typing 表情操作失败（{message_id}）：{e}")
        return None
    try:
        body = (CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type("Typing").build())
                .build())
        req = (CreateMessageReactionRequest.builder()
               .message_id(message_id)
               .request_body(body)
               .build())
        resp = feishu.im.v1.message_reaction.create(req)
        if not resp.success():
            log.info(f"飞书 Typing 表情操作失败（{message_id}）：{resp.code} {resp.msg}")
            return None
        rid = (getattr(resp, "data", None) and getattr(resp.data, "reaction_id", None)) or None
        if not rid:
            log.info(f"飞书 Typing 表情已贴但未取到 reaction_id（{message_id}）：表情可能残留在用户消息上")
        return rid
    except Exception as e:  # noqa: BLE001 - 表情失败不影响业务回复
        log.info(f"飞书 Typing 表情操作失败（{message_id}）：{e}")
        return None

def send_text_chunked(chat_id, text, chunk_size=None, max_chunks=None):
    """#22 D3：agent 长回复分段发回飞书（默认 2000 字/条、最多 5 条）；超出末条追加
    「剩余内容见看板」。返回实际发送条数。"""
    chunk_size = chunk_size or FEISHU_TEXT_CHUNK_SIZE
    max_chunks = max_chunks or FEISHU_TEXT_MAX_CHUNKS
    s = "" if text is None else str(text)
    chunks = [s[i:i + chunk_size] for i in range(0, len(s), chunk_size)] or [""]
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        chunks[-1] = chunks[-1] + "…（剩余内容见看板）"
    for c in chunks:
        _send_feishu("text", {"text": c}, chat_id)
    return len(chunks)

def esc_md(s):
    """lark_md 特殊字符转义：& < > → &amp; &lt; &gt;（对齐 automation check-bpm-pending-approvals.js 的 escMd）"""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def truncate(s, limit):
    """超长截断：>limit 截断并追加 …；==limit 不截断。先截断后转义（调用方先 truncate 再 esc_md）"""
    s = "" if s is None else str(s)
    if len(s) <= limit:
        return s
    return s[:limit] + "…"

def artifact_short(req, filename=None):
    """产物短路径（手机端可读）：相对 ~/.req-board/artifacts/ 根（如 req-board/req-115/analysis.md）；
    产物目录越界（如测试临时目录）退化为 req-<id>；禁止出现本地绝对长路径"""
    try:
        rel = artifact_dir(req).relative_to(ARTIFACTS_ROOT).as_posix()
    except ValueError:
        rel = f"req-{req['id']}"
    if filename:
        return f"{rel}/{filename}"
    return rel

def _path_short(p):
    """本地绝对路径降级展示：artifacts 根内 → 相对短路径；根外原样保留"""
    try:
        return Path(p).relative_to(ARTIFACTS_ROOT).as_posix()
    except (TypeError, ValueError):
        return str(p or "")

def _md_field(name, value):
    """双栏 is_short 字段（lark_md，值做转义）"""
    return {"is_short": True, "text": {"tag": "lark_md", "content": f"**{name}**\n{esc_md(value)}"}}


def _card_paragraphs(text, limit):
    """把卡片正文长文本切成飞书友好的段落列表（每段后续渲染为独立 div）：
    - 先按 limit 截断（沿用 #115 先截断后转义约定），再按 \n 拆段、跳过空行；
    - 行首 markdown 无序列表标记（`- `/`* `/`+ ` 后跟空白）替换为 `• `——lark_md
      不支持列表语法，不替换会原样显示且多段在卡片里挤成一行；
    - 有序编号（1. / 1) 等）原样保留。
    """
    paragraphs = []
    for line in (truncate(text or "", limit) or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        paragraphs.append(re.sub(r"^([-*+])\s+", "• ", line))
    return paragraphs


def _body_divs(title, text, limit):
    """标题与正文各占一个独立 div：lark_md 不支持块级元素，多段塞进同一 div
    会在飞书卡片中挤成一行。返回 [标题 div, 正文段落 div…]"""
    divs = [{"tag": "div", "text": {"tag": "lark_md", "content": f"**{esc_md(title)}**"}}]
    for paragraph in _card_paragraphs(text, limit):
        divs.append({"tag": "div", "text": {"tag": "lark_md", "content": esc_md(paragraph)}})
    return divs


def send_text_lossless(chat_id, text, chunk_size=None):
    """#30 无损文本发送：按 FEISHU_TEXT_CHUNK_SIZE 全量切片、**逐片检查成败**（不丢尾、不掩盖失败）。
    与 send_text_chunked（最多 5 片、超限丢尾、不看每片成败）语义不同，专供卡片降级路径；
    不改 send_text_chunked 既有语义（回归锚点，见 tests/test_feishu_chat_router.py::test_76_chunked_sends）。
    返回 (sent, failed)。"""
    chunk_size = chunk_size or FEISHU_TEXT_CHUNK_SIZE
    s = "" if text is None else str(text)
    chunks = [s[i:i + chunk_size] for i in range(0, len(s), chunk_size)] or [""]
    sent = failed = 0
    for idx, chunk in enumerate(chunks, 1):
        try:
            ok = _send_feishu("text", {"text": chunk}, chat_id)
        except Exception as e:  # noqa: BLE001 - 发送异常不打断整轮，记录后继续
            log.error(f"飞书降级文本发送异常（第 {idx}/{len(chunks)} 片）：{e}")
            ok = False
        if ok:
            sent += 1
        else:
            failed += 1
            log.error(f"飞书降级文本发送失败（第 {idx}/{len(chunks)} 片，chat={chat_id}）")
    return sent, failed


def build_req_list_card(payload):
    """#30 需求卡清单卡片（纯函数）：页 spec → 飞书 interactive 卡片。
    每张需求一个独立 div（lark_md 不支持块级元素，多卡同 div 会挤成一行）；标题先 truncate 后 esc_md；
    items 缺失/非列表 → 视作空页（页脚「（无条目）」）**不抛**（发送端 fail-soft，避免打断整轮）。"""
    p = payload if isinstance(payload, dict) else {}
    project = p.get("project") or ""
    items = p.get("items")
    if not isinstance(items, list):
        items = []
    page = p.get("page") or 1
    pages = p.get("pages") or 1
    total = p.get("total_cards")
    total = len(items) if total is None else total
    shown = items[:ASSISTANT_CARD_MAX_ITEMS]
    elements = [{"tag": "div", "text": {"tag": "lark_md",
                                        "content": f"共 {total} 张 · 第 {page}/{pages} 页"}}]
    for it in shown:
        if not isinstance(it, dict):
            continue
        title = esc_md(truncate(it.get("title") or "", CARD_TITLE_LIMIT))
        label = it.get("status_label") or it.get("status") or ""
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": (
            f"**#{esc_md(it.get('id'))} {title}**\n"
            f"状态：{esc_md(label)} · 流程：{esc_md(it.get('flow') or '未指定')}"
            f" · 更新：{esc_md(it.get('updated_at') or '')}")}})
    if not items:
        footer = "（无条目）"
    else:
        lines = [f"共 {total} 张，第 {page}/{pages} 页"]
        if p.get("truncated"):
            lines.append(f"<font color='red'>⚠️ 还有 {p.get('remaining') or 0} 张未展示，"
                         "可加状态或关键词缩小范围</font>")
        if len(items) > len(shown):
            lines.append(f"（本页仅渲染前 {len(shown)} 张）")
        footer = "\n".join(lines)
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": footer}})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "blue",
                       "title": {"tag": "plain_text",
                                 "content": truncate(f"需求卡清单 · {project}（第 {page}/{pages} 页）",
                                                     CARD_TITLE_LIMIT)}},
            "elements": elements}


def requirement_list_fallback_text(payload):
    """#30 卡片发送失败时的**完整**降级文本：逐条覆盖该页全部条目（不丢卡）。"""
    p = payload if isinstance(payload, dict) else {}
    project = p.get("project") or ""
    items = p.get("items")
    if not isinstance(items, list):
        items = []
    page = p.get("page") or 1
    pages = p.get("pages") or 1
    total = p.get("total_cards")
    total = len(items) if total is None else total
    head = f"需求卡清单 · {project}（第 {page}/{pages} 页，共 {total} 张）"
    if not items:
        return head + "\n该项目当前没有符合条件的需求卡"
    lines = [head]
    for it in items:
        if not isinstance(it, dict):
            continue
        lines.append(f"#{it.get('id')} {it.get('title') or ''}"
                     f"｜{it.get('status_label') or it.get('status') or ''}"
                     f"｜{it.get('flow') or '未指定'}｜{it.get('updated_at') or ''}")
    if p.get("truncated"):
        lines.append(f"⚠️ 还有 {p.get('remaining') or 0} 张未展示，可按状态或关键词缩小范围后重发")
    return "\n".join(lines)


def _phase_name(phase):
    return ROLE_NAMES.get(phase, phase) if phase else ""

def build_req_card(event, req, **kw):
    """构造语义化需求卡片（纯函数）：事件 → {template 颜色, 标题}；统一结构 =
    [fields 双栏, 标题, 正文段落…（各独立 div，飞书卡片不支持块级 markdown）, hr, 尾部回复指引]。
    调用方显式传结构化数据，不从旧通知文本反向解析"""
    cfg = CARD_EVENTS[event]
    rid = req["id"]
    project = req.get("project") or ""
    title = truncate(req.get("title") or "", CARD_TITLE_LIMIT)
    if event == "route":
        suggested = kw.get("suggested") or "semi"
        fields = [_md_field("项目", project),
                  _md_field("建议通道", "自动" if suggested == "auto" else "半自动")]
        lines = [f"证据清单：{artifact_short(req, kw.get('artifact'))}"]
        # #303 防复发：涉及清单外仓库时红字警示（疑似建错项目/项目配置缺仓库），
        # font 标签硬编码在 esc_md 之外不转义（与 done 提醒行同款）
        outside = kw.get("outside") or []
        if outside:
            lines.append(f"<font color='red'>⚠️ 涉及清单外仓库：{esc_md(', '.join(outside))}"
                         "（本项目仓库清单不含这些仓库，疑似建错项目或项目配置缺仓库；"
                         "确认继续则仅按清单仓库开发）</font>")
        body_text = "\n".join(lines)
        footer = f"回复 y {rid} 确认，n {rid} <原因> 否决。"
    elif event == "review":
        fields = [_md_field("项目", project), _md_field("待评审阶段", _phase_name(kw.get("phase")))]
        body_text = f"产物目录：{_path_short(kw.get('artifact_dir'))}"
        footer = f"回复 y {rid} 通过，n {rid} <原因> 否决。"
    elif event == "need_confirm":
        fields = [_md_field("项目", project), _md_field("当前阶段", _phase_name(kw.get("phase")))]
        body_text = kw.get("question") or "（agent 未说明问题）"
        round_n = kw.get("round_n") or 0
        if round_n:
            # #400：追问护栏明示出口——轮次 ≤ 上限仅标注；超限追加出口提示（不自动转 later）
            if kw.get("over_limit"):
                body_text += (f"\n（已连续第 {round_n} 轮待确认：飞书 y 继续追问 / n 恢复待开发；"
                              "网页可点「停止追问转暂缓」）")
            else:
                body_text += f"\n（第 {round_n} 轮待确认）"
        footer = f"回复 y {rid} 同意，n {rid} <原因> 否决。"
    elif event == "done":
        company = bool(kw.get("company"))
        fields = [_md_field("项目", project),
                  _md_field("合并方式", "开发分支已自动 push 到远端（待发版合并）" if company else "开发分支已合并回 main")]
        body_text = kw.get("summary") or "需求已完成"
        lines = [f"产物：{esc_md(artifact_short(req, 'result.json'))}"]
        reminder = (kw.get("reminder") or "").strip().lstrip("⚠️").strip()
        if reminder:
            # 需人工操作的事项用红字突出（lark_md 支持 <font color='red'>）；仍位于「产物」行下方
            lines.append(f"<font color='red'>⚠️ {esc_md(truncate(reminder, CARD_BODY_LIMIT))}</font>")
        # #279：agent 声明的遗留待办逐条红字展示在卡片最底部（「产物」行下方、reminder 之后）；
        # 纯展示不可勾销；先截断后转义，<font> 标签硬编码不转义
        for todo in kw.get("todos") or []:
            lines.append(f"<font color='red'>⚠️ 待办：{esc_md(truncate(todo, CARD_BODY_LIMIT))}</font>")
        footer = "\n".join(lines)
    elif event == "failed":
        fields = [_md_field("项目", project), _md_field("失败次数", f"{kw.get('retries') or 0} 次")]
        body_text = kw.get("reason") or ""
        footer = "请人工处理后把状态改回 open 恢复调度。"
    elif event == "blocked":
        fields = [_md_field("项目", project), _md_field("当前阶段", _phase_name(kw.get("phase")))]
        body_text = kw.get("reason") or ""
        footer = "已转 later 待人工处理，可在看板需求助手续跑；处理后把状态改回 open 恢复调度。"
    else:  # merge_conflict
        failed = kw.get("failed") or []
        fields = [_md_field("项目", project),
                  _md_field("冲突仓库", ", ".join(str(f) for f in failed))]
        body_text = f"已保留分支 {kw.get('branch') or ''} 与 worktree"
        footer = f"请解决后回复 y {rid} 重试，或 n {rid} 人工接管。"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": cfg["template"],
                   "title": {"tag": "plain_text", "content": f"需求 #{rid} {cfg['title']}"}},
        "elements": [
            {"tag": "div", "fields": fields},
            *_body_divs(title, body_text, CARD_BODY_LIMIT),
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": footer}},
        ],
    }

def fallback_text(event, req, **kw):
    """七类事件降级文本：逐字复用升级前 notify 文案，保证卡片发送失败降级后信息与升级前一致"""
    rid = str(req["id"])
    title = req.get("title") or ""
    if event == "route":
        suggested = kw.get("suggested") or "semi"
        outside = kw.get("outside") or []
        outside_line = ""
        if outside:
            outside_line = (f"\n⚠️ 涉及清单外仓库：{', '.join(outside)}"
                            "（本项目仓库清单不含这些仓库，疑似建错项目或项目配置缺仓库）")
        return (f"🧭 需求 #{rid}「{title}」{ROLE_NAMES.get('analysis', 'analysis')} 建议通道：{suggested}{outside_line}\n"
                f"证据清单：{artifact_dir(req) / (kw.get('artifact') or 'analysis.md')}\n\n"
                f"回复 y {rid} 确认，n {rid} <原因> 否决。")
    if event == "review":
        return (f"📋 需求 #{rid}「{title}」{_phase_name(kw.get('phase'))} 阶段产物待评审：\n"
                f"{artifact_dir(req)}\n\n回复 y {rid} 通过，n {rid} <原因> 否决。")
    if event == "need_confirm":
        question = kw.get("question") or "（agent 未说明问题）"
        round_n = kw.get("round_n") or 0
        if round_n:
            if kw.get("over_limit"):
                question += ("\n（已连续第 %d 轮待确认：飞书 y 继续追问 / n 恢复待开发；"
                             "网页可点「停止追问转暂缓」）" % round_n)
            else:
                question += f"\n（第 {round_n} 轮待确认）"
        return f"❓ 需求 #{rid}「{title}」需要确认：\n{question}\n\n回复 y {rid} 同意，n {rid} <原因> 否决。"
    if event == "blocked":
        return (f"⏸ 需求 #{rid}「{title}」已阻塞（{_phase_name(kw.get('phase'))} 阶段）。\n"
                f"阻塞原因：{kw.get('reason') or ''}\n"
                f"已转 later 待人工处理，可在看板需求助手续跑。")
    if event == "done":
        reminder = kw.get("reminder") or ""
        # #279：agent 声明的遗留待办逐条拼入降级文本（卡片发送失败也不丢失待办信息）
        todos_text = "".join(f"\n⚠️ 待办：{t}" for t in (kw.get("todos") or []))
        if kw.get("company"):
            return f"✅ 需求 #{rid} 已完成：{title}（开发分支已自动 push 到远端，待发版合并）{reminder}{todos_text}"
        return f"✅ 需求 #{rid} 已完成：{title}{reminder}{todos_text}"
    if event == "failed":
        return (f"❌ 需求 #{rid}「{title}」自动开发失败 {kw.get('retries') or 0} 次，已停止自动调度"
                f"（卡片转 later，等待人工重新开放）。\n最后原因：{kw.get('reason') or ''}\n"
                f"请人工处理后把状态改回 open 恢复调度。")
    failed = ", ".join(str(f) for f in (kw.get("failed") or []))
    return (f"⚠️ 需求 #{rid}「{title}」自动合并冲突（{failed}）。\n"
            f"已保留分支 {kw.get('branch') or ''} 与 worktree，"
            f"请解决后回复 y {rid} 重试，或 n {rid} 人工接管。")

def notify_req_card(event, req, **kw):
    """语义化需求卡片通知（七类关键状态）：构造卡片 → _send_feishu("interactive")；
    成功不重复发送；已连接但发送失败 → 降级 notify(fallback_text)（底层已 log.error）；
    未连接仅记日志不降级（沿用现有行为）"""
    card = build_req_card(event, req, **kw)
    if _send_feishu("interactive", card):
        return
    if not feishu or not state.get("chat_id"):
        return
    notify(fallback_text(event, req, **kw))

def _feishu_send_assistant_card(chat_id, content):
    """#30 发送一页需求卡清单：解析页 spec → 构造卡片 → **显式 chat_id** 直发；
    失败降级为该页**完整**文本（send_text_lossless，不丢尾）。
    - 解析/结构非法 → log.error + 明确文本提示（fail-closed，不静默）；未连接时不发任何消息。
    - 绝不复用 notify_req_card / state["chat_id"]（那是 y/n 通道，会错投）。"""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        log.error(f"助手卡片事件数据异常（chat={chat_id}）：{str(content)[:200]}")
        if feishu:
            send_text_lossless(chat_id, "需求卡清单有内容发送异常，未展示（可重发一次）。")
        return False
    tag = f"第 {payload.get('page')}/{payload.get('pages')} 页"
    if _send_feishu("interactive", build_req_list_card(payload), chat_id=chat_id):
        log.info(f"已发送需求卡清单卡片（chat={chat_id} {tag}）")
        return True
    if not feishu:
        log.info(f"飞书未连接，需求卡清单卡片仅记录（chat={chat_id} {tag}）")
        return False
    log.error(f"需求卡清单卡片发送失败，降级为该页完整文本（chat={chat_id} {tag}）")
    send_text_lossless(chat_id, requirement_list_fallback_text(payload))
    return False


def _is_reset_verdict(reason):
    """#376：需求级否决判定（评审卡 n 指令）：原因以「需求级」或「reset」开头 → 需求级否决退 open"""
    r = (reason or "").strip()
    return r.startswith("需求级") or r.lower().startswith("reset")

def _note_review_model(req, role):
    """#376/#419/#4：评审角色拉起时模型留痕（本期不强制异模）：模型唯一源 = 四层兜底链
    role_model_detail（项目级覆盖 → roles.<id>.model → 环境变量 → 全局 ACP_MODEL）。
    四层均未配置（source == "global"）→ 无留痕必要；配置了模型时与被评审阶段对比：
    同模仅事件/日志提示不阻断，异模提示独立模型。"""
    rmodel, rsource = role_model_detail(role, project=req.get("project"))
    if rsource == "global":
        return
    stage = stage_of_review(req, role)
    if not stage:
        return
    smodel = role_model(stage, project=req.get("project"))
    if rmodel == smodel:
        log.info(f"需求 #{req['id']} 评审角色 {role} 与被评审阶段 {stage} 同模（{rmodel}），仅留痕不阻断")
        post_dispatcher_event(req["id"],
                              f"评审角色「{ROLE_NAMES.get(role, role)}」与被评审阶段同模（{rmodel}），仅留痕提示",
                              kind="action")
    else:
        post_dispatcher_event(req["id"],
                              f"评审角色「{ROLE_NAMES.get(role, role)}」使用独立模型 {rmodel}（被评审阶段 {smodel}）",
                              kind="action")

def _spawn_safe(req, role, extra="", resume=False):
    """spawn 前复用 active_runs 去重，防飞书/按钮对账并发重复拉起"""
    with active_lock:
        if str(req["id"]) in active_runs:
            log.info(f"需求 #{req['id']} 已有活跃 agent，跳过重复 spawn（role={role}）")
            return False
    spawn_agent(req, role=role, extra=extra, resume=resume)
    return True


def _review_gate_rework(req_id, req, question, reason=""):
    """#411：审查发现问题转开发复修（飞书 n <id> 转开发 与 网页「转开发」按钮共用）——
    需求保持 doing（spawn_agent 内部置 doing，不显式 set）；pop pending_merge_review；
    resume 原 dev 会话（保上下文），merge_review.md 审查反馈经 extra 注入 dev 提示词
    （评审结论只走产物通道，不写 summary/notes，守 #342 通道约定）；summary + 调度事件
    各留痕一条（无上限计数，纯展示）；修复后 dev 报 done 重新触发 review 审查——
    转动循环无轮次上限（人工即边界，系统永不自动退回）。"""
    with state_lock:
        state.get("pending_merge_review", {}).pop(str(req_id), None)
    save_state()
    feedback = ""
    try:
        review_md = artifact_dir(req) / "merge_review.md"
        if review_md.exists():
            feedback = review_md.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log.warning(f"需求 #{req_id} 读取审查反馈 merge_review.md 失败（{e}），仅注入问题文本")
    extra = f"⚠️ 代码审查发现问题，人工已裁决「转开发」复修（原因：{reason or '未说明'}）。\n审查问题：{question}"
    if feedback:
        extra += f"\n审查结论（merge_review.md）：\n{feedback}"
    append_summary(req_id, f"代码审查发现问题（{question}），人工转开发复修（{reason or '未说明'}）；"
                           "原 dev 会话续跑，修复后将重新审查。")
    post_dispatcher_event(req_id, "审查发现问题，人工转开发复修（修复后将重新审查）", kind="merge")
    notify(f"需求 #{req_id} 审查发现问题，已转开发复修（原 dev 会话续跑并注入审查反馈），修复后将重新审查。")
    _spawn_safe(req, "dev", extra=extra, resume=True)


def _feishu_group_text(msg):
    """群聊 @ 消息正文归一（#22 群聊支持）：飞书群 @ 文本里用 `@_user_N` 占位符标记被 @ 的人，
    按 message.mentions[].key 全部剥离后再去首尾空白。解析失败/无正文返回空串（调用方按无正文忽略）。"""
    try:
        content = json.loads(msg.content)
        text = content.get("text", "") if isinstance(content, dict) else ""
    except Exception:  # noqa: BLE001 - 异常报文按无正文处理，不影响 ws 回调线程
        return ""
    for mention in (getattr(msg, "mentions", None) or []):
        key = getattr(mention, "key", "")
        if isinstance(key, str) and key:
            text = text.replace(key, "")
    return str(text).strip()


def _feishu_post_text_and_handles(msg):
    """富文本（msg_type=post）→ (正文, 图片素材句柄列表)。

    飞书里「文字 + 粘贴截图」的消息常以 post 形态下发（不是 text/image 两条），此前在
    on_message 的类型白名单里被静默丢弃 —— 用户表现就是「@机器人贴图后没反应」。这里抽出
    所有 text/a 片段拼正文、img 片段生成图片句柄（只含 image_key，字节仍延迟到 worker 下载）。
    兼容 {"title","content":[[...]]} 与按语言包裹 {"zh_cn": {...}} 两种报文形态。"""
    try:
        content = json.loads(getattr(msg, "content", "") or "{}")
    except Exception:  # noqa: BLE001 - 异常报文按无正文处理，不影响 ws 回调线程
        return "", []
    if not isinstance(content, dict):
        return "", []
    if "content" not in content:
        for key in ("zh_cn", "en_us", "ja_jp"):
            wrapped = content.get(key)
            if isinstance(wrapped, dict) and "content" in wrapped:
                content = wrapped
                break
    lines, image_keys = [], []
    for paragraph in (content.get("content") or []):
        if not isinstance(paragraph, list):
            continue
        parts = []
        for element in paragraph:
            if not isinstance(element, dict):
                continue
            tag = element.get("tag")
            if tag == "text":
                parts.append(str(element.get("text") or ""))
            elif tag == "a":  # 富文本里的超链接：取可见文字，无文字取 href
                parts.append(str(element.get("text") or element.get("href") or ""))
            elif tag == "img":
                key = element.get("image_key")
                if isinstance(key, str) and key:
                    image_keys.append(key)
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    title = str(content.get("title") or "").strip()
    text = "\n".join([x for x in [title] + lines if x])
    for mention in (getattr(msg, "mentions", None) or []):
        key = getattr(mention, "key", "")
        if isinstance(key, str) and key:
            text = text.replace(key, "")
    message_id = getattr(msg, "message_id", None)
    handles = [{"message_id": message_id, "image_key": key, "type": "image",
                "name": f"图片-{key[:12]}.png", "size_hint": None}
               for key in image_keys]
    return text.strip(), handles


def _feishu_text_and_handles(msg):
    """已支持的任意消息 → (正文, 素材句柄列表)。text/post 解正文，file/image 走素材句柄；
    纯图片/文件补默认正文（与私聊素材语义一致），保证助手本轮拿到可读文本。"""
    mtype = getattr(msg, "message_type", "") or ""
    if mtype == "text":
        return _feishu_group_text(msg), []
    if mtype == "post":
        return _feishu_post_text_and_handles(msg)
    handle = _feishu_parse_material(msg)
    if handle is None:
        return "", []
    return ("（上传了图片）" if handle.get("type") == "image" else "（上传了文件）"), [handle]


def on_message(data: "P2ImMessageReceiveV1"):
    msg = data.event.message
    mtype = getattr(msg, "message_type", "") or ""
    chat_type = getattr(msg, "chat_type", None)
    # ① 只处理 text/post/file/image；其余（sticker 等）忽略但必须留日志——
    #    静默 return 会让用户看到的是「@了机器人却毫无反应」，问题无法定位。
    if mtype not in ("text", "post", "file", "image"):
        log.info(f"忽略不支持的飞书消息类型（chat_type={chat_type}, message_type={mtype}）")
        return
    # ①-a 群聊（#22 行为变化 2）：@机器人 的文本/图文路由到看板助手；**不写 state["chat_id"]**
    # （私聊通知通道保持唯一）、不问候、不走 y/n 与建卡链路，回复按群 chat_id 显式发回群。
    # 飞书侧本应用只订阅「群组中@机器人消息」事件（未开 im:message.group_msg），能收到的群消息
    # 必然 @ 了机器人；若将来放开群消息权限，这里需补 mentions 归属校验，否则会回所有群消息。
    # 仅当 chat_type 是显式字符串且非 p2p 才走群分支——老测试桩用 Mock/缺省，按 p2p 兼容。
    # 图文混排（文字+粘贴截图）在飞书是 msg_type=post：正文与图片统一由 _feishu_text_and_handles
    # 解析，图片按素材句柄随本轮附件送助手（此前 post 不在类型白名单里，被静默丢弃）。
    if isinstance(chat_type, str) and chat_type and chat_type != "p2p":
        if not _feishu_seen_once(msg.chat_id, getattr(msg, "message_id", None)):
            log.info(f"忽略重复投递的飞书群消息（chat_id={msg.chat_id}）")
            return
        text, handles = _feishu_text_and_handles(msg)
        if not text and not handles:
            log.info(f"忽略无正文/无素材的飞书群消息（chat_id={msg.chat_id}, message_type={mtype}）")
            return
        log.info(f"收到飞书群消息: {text}" + (f"（含 {len(handles)} 个附件）" if handles else ""))
        payload = {"kind": "assistant", "text": text,
                   "message_id": getattr(msg, "message_id", None)}
        if handles:
            payload["handles"] = handles
        _feishu_enqueue(msg.chat_id, payload)
        _feishu_save()
        return
    # ①′ #24 消息幂等：去重键 = feishu_chat_id + message_id（覆盖 y/n、素材与助手文本），
    # 同一 message_id 只处理一次；message_id 缺失（测试桩/异常报文）不做去重，不吞消息。
    if not _feishu_seen_once(msg.chat_id, getattr(msg, "message_id", None)):
        log.info(f"忽略重复投递的飞书消息（chat_id={msg.chat_id}）")
        return
    state["chat_id"] = msg.chat_id
    save_state()
    # file/image → 素材分支（句柄入队，worker 异步处理；优先级最高）
    # #44：无活跃旧建卡会话时素材进看板助手（有旧会话时仍进旧 pending 链路，见 _feishu_handle_material）；
    # payload 带 message_id 供 worker 在用户消息上贴/撤 Typing 表情。
    if mtype in ("file", "image"):
        handle = _feishu_parse_material(msg)
        if handle:
            _feishu_enqueue(msg.chat_id, {"kind": "material", "handle": handle,
                                          "message_id": getattr(msg, "message_id", None)})
        else:
            # #22 ⑥：文件名缺失/内容不可解析 → 不产生无扩展名句柄，明确提示跳过（不拖垮整批素材）
            _feishu_notify(msg.chat_id, "文件名缺失，已跳过该文件。")
        return
    if mtype == "post":
        # 私聊富文本：有图 → 图文一起作为助手本轮附件；无图 → 取正文后按普通文本走后续 y/n 与路由
        text, handles = _feishu_post_text_and_handles(msg)
        if handles:
            log.info(f"收到飞书消息（图文）: {text or '（图片）'}")
            _feishu_enqueue(msg.chat_id, {"kind": "assistant",
                                          "text": text or "（上传了图片）",
                                          "message_id": getattr(msg, "message_id", None),
                                          "handles": handles})
            _feishu_save()
            return
    else:
        text = json.loads(msg.content).get("text", "").strip()
    if not text:
        log.info(f"忽略无正文的飞书消息（chat_id={msg.chat_id}, message_type={mtype}）")
        return
    log.info(f"收到飞书消息: {text}")
    # ② #24 首条消息问候（独立「已问候 chat 集合」，同 chat 只问候一次）——**不再 return**，
    # 问候后本条第 y/n、素材、建卡与助手路由照常处理（原实现把首条消息整条吞掉）。
    if _feishu_greet_once(msg.chat_id):
        notify("已连接 req-board 调度器。待确认/评审问题会发到这里，回复 y <编号> 同意、n <编号> <原因> 否决。")
    # ③ y/n 正则（逐字保留、优先级最高）
    m = re.match(r"^(y|yes|同意|n|no|拒绝)\s*#?(\d+)\s*(.*)$", text, re.I)
    if not m:
        # ③′-⑥：文本路由（指令 / 活跃旧建卡会话 / 看板助手）
        _feishu_route_text(msg.chat_id, text, getattr(msg, "message_id", None))
        return
    approve, req_id, reason = m.group(1).lower() in ("y", "yes", "同意"), int(m.group(2)), m.group(3).strip()
    # #27：回复编号与待确认条目内容不同源时不消费——锁内 get + 纯判定（命中不 pop），
    # 出锁后再按内容归属归位/清理（_pending_rehang 自带落盘），全程不改动任何卡的状态。
    mm = False
    cid = None
    pc = None
    with state_lock:
        pc = state["pending_confirm"].get(str(req_id))
        mm, cid = _pending_key_mismatch(pc, req_id)
        if mm:
            pend = None
        else:
            pend = state["pending_confirm"].pop(str(req_id), None)
            kind = "confirm" if pend else None
            if pend is None:
                pend = state["pending_route"].pop(str(req_id), None)
                kind = "route" if pend else None
            if pend is None:
                pend = state["pending_review"].pop(str(req_id), None)
                kind = "review" if pend else None
    if mm:
        _pending_rehang(req_id, pc, cid, "飞书 y/n", question=pc.get("question") or "")
        return
    if not pend:
        notify(f"需求 #{req_id} 没有待确认的问题。")
        return
    save_state()
    req = fetch_req(req_id) or pend.get("req")
    # 记录用户飞书回复（含原文）到需求时间线，完整呈现「agent 需要确认 → 用户回复 → 继续执行」闭环
    verdict = "同意" if approve else f"否决 - {reason or '未说明'}"
    post_dispatcher_event(req_id, f"用户飞书回复：{verdict}（{text}）", kind="confirm")
    if not approve:
        if kind == "route":
            set_status(req_id, "open")
            append_summary(req_id, f"用户否决通道建议：{reason or '未说明'}（analysis 建议 {pend.get('suggested_mode')}）")
            notify(f"需求 #{req_id} 已否决通道建议并退回待开发，将重新分析。")
            post_dispatcher_event(req_id, "用户否决通道建议，退回待开发", kind="phase")
        elif kind == "review":
            phase = pend.get("phase")
            # #376：人评否决双出口——需求级否决（原因以「需求级」/「reset」开头）= phase 置空退 open；
            # 默认打回 = 评审角色 phase 重定向前置 stage（重做算新一轮，清 retries）；普通 phase 保留自身
            if _is_reset_verdict(reason):
                set_req(req_id, {"status": "open", "phase": None})
                clear_flow_rounds(req_id)
                append_summary(req_id, f"用户需求级否决评审：{reason or '未说明'}（阶段 {phase}），需求退回待开发")
                notify(f"需求 #{req_id} 已需求级否决（{ROLE_NAMES.get(phase, phase)} 阶段），退回待开发。")
                post_dispatcher_event(req_id,
                                      f"用户需求级否决「{ROLE_NAMES.get(phase, phase)}」评审，退回待开发",
                                      kind="phase")
            else:
                nxt = stage_of_review(req, phase) or phase
                clear_retries(req_id)
                clear_flow_rounds(req_id)
                if nxt != phase:
                    set_req(req_id, {"status": "open", "phase": nxt})
                    append_summary(req_id, f"用户否决评审：{reason or '未说明'}（阶段 {phase}），打回{ROLE_NAMES.get(nxt, nxt)}重做")
                else:
                    set_status(req_id, "open")  # phase 保留，重捞从当前阶段重跑（反馈进提示词）
                    append_summary(req_id, f"用户否决评审：{reason or '未说明'}（阶段 {phase}）")
                notify(f"需求 #{req_id} 评审未通过（{ROLE_NAMES.get(phase, phase)} 阶段），已退回待开发，将按反馈重跑。")
                post_dispatcher_event(req_id,
                                      f"用户否决「{ROLE_NAMES.get(phase, phase)}」评审，退回待开发",
                                      kind="phase")
        elif pend.get("kind") == "review_gate":
            # #411：审查发现问题转人工三档裁决——reason 以「转开发」开头 = 转开发复修
            # （保持 doing、resume 原 dev 会话、merge_review.md 反馈注入，修复后重新审查）；
            # 普通 n = 否决合并转暂缓，保留分支/worktree 待人工处理（不直通合并）
            if reason.startswith("转开发"):
                _review_gate_rework(req_id, req, pend.get("question") or "",
                                    reason[3:].strip().lstrip("：: "))
            else:
                with state_lock:
                    state.get("pending_merge_review", {}).pop(str(req_id), None)
                append_summary(req_id, f"代码审查发现问题（{pend['question']}），人工否决合并：{reason or '未说明'}；"
                                       "dev 分支与 worktree 保留待人工处理。")
                set_status(req_id, "later")
                notify(f"需求 #{req_id} 审查发现问题，已否决合并并置为暂缓，dev 分支与 worktree 保留，请人工处理。")
                post_dispatcher_event(req_id, "审查发现问题，人工否决合并，需求转暂缓（分支/worktree 保留）", kind="merge")
        elif pend.get("kind") == "merge":
            append_summary(req_id, f"用户人工接管合并（{reason or '未说明'}），需求置为暂缓，dev 分支与 worktree 保留待人工处理。")
            set_status(req_id, "later")
            notify(f"需求 #{req_id} 已置为暂缓，dev 分支与 worktree 保留，请人工处理。")
            post_dispatcher_event(req_id, "用户人工接管合并，需求置为暂缓，分支/worktree 保留", kind="merge")
            merge_conflict_clear(req_id, "飞书 n 接管")
        elif pend.get("kind") == "confirm_override":
            # #399：人工否决豁免——默认退 open(phase 置空+清 retries)重新分析；
            # 原因以 `later` 开头 → 转 later 人工收口（分支/worktree 保留）
            if reason.lower().startswith("later"):
                set_status(req_id, "later")
                append_summary(req_id, f"人工否决豁免（later）：{reason[5:].strip() or '未说明'}（校验未过：{pend['question']}），"
                                       "dev 分支与 worktree 保留待人工处理。")
                notify(f"需求 #{req_id} 已否决豁免并置为暂缓，dev 分支与 worktree 保留，请人工处理。")
                post_dispatcher_event(req_id, "人工否决豁免（later），需求转暂缓（分支/worktree 保留）",
                                      kind="confirm")
            else:
                set_req(req_id, {"status": "open", "phase": None})
                clear_retries(req_id)
                clear_flow_rounds(req_id)
                append_summary(req_id, f"用户否决豁免：{reason or '未说明'}（校验未过：{pend['question']}），"
                                       "退回待开发重新分析。")
                notify(f"需求 #{req_id} 已否决豁免并退回待开发，将重新分析。")
                post_dispatcher_event(req_id, "用户否决豁免，退回待开发", kind="confirm")
        elif pend.get("review_index") is not None:
            # #419：评审轮次上限人工裁决——n 打回前驱节点再修一轮（指针先回退再 resume）
            node = current_node(req)
            _fid, nodes, idx = flow_snapshot(req)
            prev = (nodes[idx - 1] if nodes and idx > 0 else None)
            if node is not None and (node.get("node_type") or "") == "review" and prev:
                set_req(req_id, {"node_index": idx - 1})
                fresh = fetch_req(req_id) or req
                cur = (state.get("flow_review_rounds", {}).get(str(req_id), {})
                       or {}).get(str(pend.get("review_index")), 0)
                append_summary(req_id, f"人工裁决打回「{node_display(prev)}」重做：{reason or '未说明'}")
                post_dispatcher_event(req_id, f"人工裁决打回「{node_display(prev)}」再修一轮", kind="phase")
                notify(f"需求 #{req_id} 已按人工裁决打回「{node_display(prev)}」重做。")
                _spawn_safe(fresh, prev.get("role"),
                            extra=_review_bounce_extra(fresh, node, reason, cur or None), resume=True)
            else:
                set_status(req_id, "later")
                with state_lock:   # C3：转暂缓即退出本轮评审周期，升级记录与各轮要点一并清
                    state.get("review_escalation", {}).pop(str(req_id), None)
                    state.get("review_round_points", {}).pop(str(req_id), None)
                save_state()
                notify(f"需求 #{req_id} 当前节点已变化，已置为暂缓等待人工处理。")
            return
        elif pend.get("flow_id"):
            # #419：前置槽位流程确认被否决 → 转人工处理（不重复拉起前置环节）
            set_status(req_id, "later")
            append_summary(req_id, f"人工否决流程推荐（{pend['flow_id']}）：{reason or '未说明'}（问题：{pend['question']}）")
            notify(f"需求 #{req_id} 否决了流程推荐，已置为暂缓等待人工处理。")
            post_dispatcher_event(req_id, "人工否决流程推荐，需求转暂缓", kind="phase")
        else:
            set_status(req_id, "open")
            append_summary(req_id, f"用户否决方案：{reason or '未说明'}（问题：{pend['question']}）")
            notify(f"需求 #{req_id} 已否决并退回待开发。")
        return
    if kind == "route":
        mode = pend.get("suggested_mode") or "auto"
        if mode == "manual_close":
            # #399：analysis 建议人工收口（实现已人工提交/验证记录类）——人工 y 直接收口，不经 set_req/spawn
            append_summary(req_id, f"人工确认收口：{reason or '未说明'}（analysis 建议人工收口）。")
            post_dispatcher_event(req_id, "人工确认收口，需求完成", kind="phase")
            finalize_done(req)
            notify(f"需求 #{req_id} 已确认人工收口，需求完成。")
            return
        # #425：流程名白名单配置派生（flows.json 启用流程），不再维护硬编码清单
        flow = default_flow_id()
        flow_hint = ""
        if mode == "semi" and reason:
            words = reason.split()          # 兼容纯空白 reason，不新增 IndexError 路径
            if words:
                word = words[0].strip().lower()
                cfg_flow = flow_by_id(word)
                if cfg_flow and flow_enabled(word):
                    flow = word
                else:
                    why = "未启用" if cfg_flow else "未识别"
                    flow_hint = f"（{why}流程「{word}」，按{FLOW_NAMES.get(flow, flow)}流程处理）"
        if mode != "semi":
            flow = None
        fields = {"mode": mode, "status": "open", "phase": None}
        if flow:
            fields["flow"] = flow
        set_req(req_id, fields)
        clear_retries(req_id)
        clear_flow_rounds(req_id)
        fresh = fetch_req(req_id) or req
        channel = '自动' if mode == 'auto' else f'半自动·{FLOW_NAMES.get(flow, flow)}流程'
        post_dispatcher_event(req_id, f"用户确认走{channel}通道，进入开发", kind="phase")
        notify(f"需求 #{req_id} 已确认走{channel}通道。{flow_hint}")
        # #303 防复发：确认继续时把清单外仓库警示注入 dev 会话（worktree 内无这些仓库代码，
        # 确有需要报 need_confirm，禁止越界写主工作区）
        extra = ""
        outside = pend.get("outside") or []
        if outside:
            extra = (f"注意：analysis 识别本需求涉及清单外仓库 {', '.join(outside)}，"
                     "本项目 worktree 内没有这些仓库的代码。如确认需改这些仓库："
                     "禁止写任何真实仓库主工作区，结束回合报 need_confirm 说明"
                     "「需求可能属于其它项目/项目仓库清单缺配置」等待人工处理。")
        _spawn_safe(fresh, role_for(fresh), extra=extra)
    elif kind == "review":
        phase = pend.get("phase")
        if phase == "accept":
            append_summary(req_id, "人工验收通过，需求完成。")
            finalize_done(req)
            post_dispatcher_event(req_id, "人工验收通过，需求完成", kind="phase")
        else:
            nxt = next_stage(req, phase)
            if nxt:
                set_req(req_id, {"status": "doing", "phase": nxt})
                clear_retries(req_id)
                clear_flow_rounds(req_id)
                fresh = fetch_req(req_id) or req
                post_dispatcher_event(req_id,
                                      f"「{ROLE_NAMES.get(phase, phase)}」评审通过，进入「{ROLE_NAMES.get(nxt, nxt)}」",
                                      kind="phase")
                notify(f"需求 #{req_id} 评审通过（{ROLE_NAMES.get(phase, phase)} 阶段），进入 {ROLE_NAMES.get(nxt, nxt)} 阶段。")
                _spawn_safe(fresh, nxt)
            else:
                notify(f"需求 #{req_id} 评审通过，但阶段 {ROLE_NAMES.get(phase, phase)} 无下一角色。")
    elif pend.get("kind") == "review_gate":
        # #337：审查发现问题，人工确认仍合并——审查发现红字留档后放行 finalize_done
        with state_lock:
            pmr = state.get("pending_merge_review", {}).pop(str(req_id), None)
        save_state()
        append_summary(req_id, f"⚠️ 代码审查发现问题，人工确认仍合并：{pend['question']}")
        post_dispatcher_event(req_id, "审查发现问题，人工确认仍合并（发现已留档）", kind="merge")
        finalize_done(req, summary=(pmr or {}).get("summary"))
        notify(f"需求 #{req_id} 审查发现的问题已红字留档，人工确认放行合并。")
    elif pend.get("kind") == "confirm_override":
        # #399：人工确认豁免——红字留档后 finalize_done 统一收口（合并/冲突/#380 重启判定照常，不跳过）
        append_summary(req_id, f"⚠️ 机制校验未过，人工确认豁免：{pend['question']}")
        if pend.get("todos") is not None:
            with state_lock:
                state.setdefault("req_todos", {})[str(req_id)] = pend.get("todos")
            save_state()
        post_dispatcher_event(req_id, "收尾校验未过，人工确认豁免收口需求", kind="merge")
        finalize_done(req, summary=pend.get("summary"))
        notify(f"需求 #{req_id} 收尾校验未过，经人工确认豁免，需求已完成。")
    elif pend.get("kind") == "merge":
        # #106：改动文件清单在合并前捕获（--no-ff 合并后三点 diff 恒空）；
        # 用户解决冲突的提交发生在重试前，已包含在捕获范围内
        with state_lock:
            rec = state.get("worktrees", {}).get(str(req_id))
        touched = bool(rec) and _touches_dispatcher(req, rec["branch"])
        merge_files = (_collect_merge_changed_paths(req, rec["branch"]) if rec
                       and PROJECTS.get(req.get("project") or "", {}).get("auto_restart_after_merge")
                       else None)
        ok, failed = merge_dev_branches(req)
        if ok:
            cleanup_dev_worktrees(req_id, delete_branch=True)
            # #300/#394：人工合并重试成功同样按开关触发重启判定（与 finalize_done 保持一致）
            restart_failed, restart_ok, restart_warn = _maybe_auto_restart_after_merge(
                req, merge_files, req_id)
            _set_done(req, dispatcher_touched=touched, restart_failed=restart_failed,
                      restart_ok=restart_ok, restart_warn=restart_warn)
            notify(f"需求 #{req_id} 合并重试成功，已完成。")
            post_dispatcher_event(req_id, "人工处理合并成功，清理 worktree，需求完成", kind="merge")
            merge_conflict_clear(req_id, "飞书 y 重试成功")
        else:
            entry = {"req": req, "question": pend["question"], "kind": "merge"}
            with state_lock:
                ok = _put_pending_confirm(str(req_id), entry)
            save_state()
            if not ok:
                _pending_source_mismatch_note(str(req_id), entry, "合并重试仍冲突重挂")
            guard = _merge_guard_blockers(req, failed)
            fail_text = (f"被主干守卫拦下（{_branch_guard_text(guard)}），未执行合并"
                         if guard else _merge_fail_text(failed, _merge_dirty_blockers(req, failed)))
            notify(f"需求 #{req_id} 合并{fail_text}，请继续处理（解决后回复 y 重试）。")
            post_dispatcher_event(req_id, f"合并{fail_text}，继续待人工处理", kind="merge")
            merge_conflict_write(req_id)  # 重置网页按钮等待再次点击
    else:
        if pend.get("flow_id"):
            # #419：前置槽位流程确认（confirm 项目 / 清单外仓库把关）——y 采纳推荐流程并推进
            f = flow_by_id(pend["flow_id"])
            if _adopt_flow_and_spawn(req, pend["flow_id"]):
                post_dispatcher_event(req_id,
                                      f"采纳推荐流程 {pend['flow_id']}（{f['name'] if f else ''}）"
                                      "｜来源：人工确认",
                                      kind="flow_choice")
                notify(f"需求 #{req_id} 已采纳推荐流程「{f['name'] if f else pend['flow_id']}」并继续推进。")
            else:
                notify(f"需求 #{req_id} 采纳流程失败（快照写入未生效），请人工处理。")
            return
        if pend.get("review_index") is not None:
            # #419：评审轮次上限人工裁决——y 放行进入下一节点
            node = current_node(req)
            if node is not None and (node.get("node_type") or "") == "review":
                with state_lock:
                    state.get("flow_review_rounds", {}).get(str(req_id), {}).pop(str(pend["review_index"]), None)
                _fid_y, nodes_y, idx_y = flow_snapshot(req)
                prev_y = nodes_y[idx_y - 1] if nodes_y and idx_y > 0 else None
                _clear_review_escalation(req_id, str(pend["review_index"]),
                                         prev_y.get("role") if prev_y else None)
                save_state()
                append_summary(req_id, f"人工裁决放行（{node_display(node)}）：{reason or '同意'}。")
                post_dispatcher_event(req_id, f"人工裁决放行「{node_display(node)}」，进入下一节点", kind="phase")
                _advance_after_node(req, node)
            else:
                notify(f"需求 #{req_id} 当前节点已变化，未执行放行，请在看板确认现状。")
            return
        # 普通提问的答复留档到 summary（prompt 通道）：会话重开/重试后的新 agent 也能看到这条
        # 澄清。上面两条（前置流程确认 / 评审超限裁决）各有自己的留档分支，不重复写。
        append_summary(req_id, confirm_answer_summary(pend.get("question"), reason))
        spawn_agent(req, extra=f"用户对之前的问题「{pend['question']}」的答复：同意。{reason}", resume=True)
        role = role_for(req)
        notify(f"已收到，需求 #{req_id} 继续{ROLE_NAMES.get(role, role) or '收尾'}阶段。")

# 未安装 lark_oapi 时基类退化为 object（NO_FEISHU 恒真，start_feishu_ws 不会被调用）
class _FeishuStatusClient(lark.ws.Client if LARK_AVAILABLE else object):
    """#23 长连接状态观测：包装 `_connect()` 上报首连成功/失败，挂 SDK 公开
    `on_reconnecting`/`on_reconnected` 上报重连中/已恢复。只把 `_conn` 用于区分「早退」
    与「本次真的连上」，不作业务判据；上报动作自身不抛（回调异常不得影响 SDK 重连）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_reconnecting = lambda: _update_feishu_status(
            connected=False, phase="reconnecting",
            last_error="长连接已断开，SDK 正在重连")
        self.on_reconnected = lambda: _update_feishu_status(
            connected=True, phase="connected", last_error=None,
            connected_at=time.time())

    async def _connect(self):
        # 评审打回级修复：**不进入** SDK 的「持锁早退」分支。
        # SDK `lark_oapi/ws/client.py` `_connect()` 在 `await self._lock.acquire()` 之后若
        # `_conn is not None` 直接 `return`——不经过 `finally: self._lock.release()`，锁被永久持有；
        # 之后 `_disconnect()`/`_write_message()` 的 `acquire` 会永久阻塞（长连接线程卡死、
        # 心跳停在 connecting）。这里在调用父类前先复现该早退语义（唯一效果就是 return），
        # 既保留「重连复用已有连接」的行为，又保证泄漏分支永不被走到。
        if getattr(self, "_conn", None) is not None:
            return
        try:
            await super()._connect()
        except Exception as e:  # noqa: BLE001 - 先上报原因再原样抛出，SDK 行为逐字不变
            _update_feishu_status(connected=False, phase="failed",
                                  last_error=_sanitize_feishu_error(e))
            raise
        _update_feishu_status(connected=True, phase="connected",
                              last_error=None, connected_at=time.time())


def _feishu_connect_watchdog(timeout=None):
    """#23 首连超时兜底：长连接建立失败的原因必须能写进心跳（界面「连接失败：原因」可达）。
    SDK 内部异常路径（持锁早退 / `_disconnect` 阻塞等）可能让 `start()` 永不返回，此线程兜底：
    超时后若状态仍停在 `connecting`，改写为 `failed` + 可读原因；已迁移（connected/failed/
    reconnecting）则不动，超时窗口内成功连接不受影响。"""
    timeout = FEISHU_CONNECT_TIMEOUT_SEC if timeout is None else timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        st = (load_heartbeat() or {}).get("feishu")
        phase = st.get("phase") if isinstance(st, dict) else None
        if phase and phase != "connecting":
            return
    st = (load_heartbeat() or {}).get("feishu")
    if isinstance(st, dict) and st.get("phase") == "connecting":
        _update_feishu_status(
            connected=False, phase="failed",
            last_error=f"飞书长连接在 {timeout}s 内未建立（SDK 未返回结果，线程疑似卡住）")


def start_feishu_ws(app_id, app_secret):
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .build())
    client = _FeishuStatusClient(app_id, app_secret, event_handler=handler,
                                 log_level=lark.LogLevel.WARNING)
    threading.Thread(target=_feishu_connect_watchdog, daemon=True).start()
    try:
        client.start()  # 阻塞，自动重连
    except Exception as e:  # noqa: BLE001 - 线程异常不静默：写心跳 failed 后线程正常退出
        err = _sanitize_feishu_error(e)
        log.error(f"网络连接失败：飞书长连接线程退出（{err}）")
        _update_feishu_status(connected=False, phase="failed", last_error=err)

# ==================== 飞书多轮聊天建卡（#22 D4-D11） ====================
# 数据流：飞书消息 → on_message（解析 + 入队，秒回，不阻塞 ws 回调）→ per-chat worker 串行处理
# → 项目解析（未定则追问）→ 素材按句柄延迟下载 → 等会话 idle → /prompt → 轮询轮次结束 → 文本发回飞书。
# 本地状态 state["feishu_chats"][chat_id] 只存文本与下载句柄，素材字节永不落盘/常驻。

_last_feishu_idle_sweep = 0.0
# 建卡指令（整行归一化全等，非包含/前缀；无活跃建卡会话时等同普通文本）
FEISHU_CMD_END = "结束建卡"
FEISHU_CMD_NEW = "新建建卡"
FEISHU_CMD_CONFIRM = "确认建卡"
_FEISHU_CMD_EDGE = (" \t\r\n，。！？、；：,.!?;:\u3000"
                    "“”‘’「」『』\"'（）()【】[]〈〉《》<>")
_FEISHU_BUSY_HINT = "上一轮仍在进行，稍后回复任意消息继续。"
# #22 ⑤：飞书文档域名白名单（docx/wiki 路径仅在飞书域名下才走飞书 API）
_FEISHU_DOMAINS = ("feishu.cn", "larksuite.com", "feishu.net")
# #22 ⑥：飞书素材扩展名白名单（与 board 侧 FEISHU_MATERIAL_EXTS 同口径）
_FEISHU_MATERIAL_EXTS = frozenset((
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    ".pdf", ".txt", ".md", ".csv", ".json",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".html", ".htm",
))
_FEISHU_LINK_REJECT_HINT = "链接不可抓取（仅支持公网 http/https）"
# #44 助手附件单文件上限（与 board FEISHU_MATERIAL_MAX_BYTES 同口径；下载侧无字节上限，
# 判定发生在整包读入之后，与旧建卡素材链路同款）
_FEISHU_MATERIAL_MAX_BYTES = 20 * 1024 * 1024
# 单轮附件条数上限（与 board FEISHU_MATERIAL_MAX_FILES 同口径）：富文本多图超过部分截断并留日志
_FEISHU_MATERIAL_MAX_FILES = 5


def _feishu_save():
    try:
        save_state()
    except Exception as e:  # 落盘失败不阻断消息处理
        log.warning(f"飞书建卡状态保存失败: {e}")


def _feishu_chats():
    """state["feishu_chats"] 访问器（缺失时补空 dict；测试 fresh_state 无该键时也能工作）"""
    fc = state.get("feishu_chats")
    if not isinstance(fc, dict):
        fc = {}
        state["feishu_chats"] = fc
    return fc


def _feishu_greeted():
    """#24：独立「已问候 chat 集合」（按 chat_id，与 state["chat_id"] 解耦）。
    访问器在测试 fresh_state 无该键时补空 dict。"""
    g = state.get("feishu_greeted")
    if not isinstance(g, dict):
        g = {}
        state["feishu_greeted"] = g
    return g


def _feishu_greet_once(chat_id):
    """#24：同一 chat 只问候一次；返回 True 表示本条需要发问候（随后继续处理本条）。"""
    g = _feishu_greeted()
    if chat_id in g:
        return False
    g[chat_id] = time.time()
    _feishu_save()
    return True


def _feishu_seen_once(chat_id, message_id):
    """#24 消息幂等：去重键 = feishu_chat_id + message_id，per-chat 保留近期已处理窗口。
    message_id 缺失/空 → 不去重（fail-open，不因缺字段吞消息）；重复投递返回 False。"""
    if not message_id:
        return True
    seen = state.get("feishu_seen")
    if not isinstance(seen, dict):
        seen = {}
        state["feishu_seen"] = seen
    ids = seen.get(chat_id)
    if not isinstance(ids, list):
        ids = []
        seen[chat_id] = ids
    key = str(message_id)
    if key in ids:
        return False
    ids.append(key)
    if len(ids) > FEISHU_ASSISTANT_SEEN_WINDOW:
        del ids[:len(ids) - FEISHU_ASSISTANT_SEEN_WINDOW]
    return True


def _feishu_sess(chat_id):
    return _feishu_chats().get(chat_id)


def _feishu_local_by_session(session_id):
    for cid, s in (_feishu_chats() or {}).items():
        if (s or {}).get("session_id") == session_id:
            return cid, s
    return None, None


def _feishu_is_cmd(text, cmd):
    """#22 D8′/⑨：循环剥首尾空白与中英文标点直到不再变化，再整行全等比较
    （不做包含/前缀）。`结束建卡。。`、`「结束建卡」` 都能命中。"""
    if not text:
        return False
    t = text
    while True:
        s = t.strip(_FEISHU_CMD_EDGE)
        if s == t:
            return s == cmd
        t = s


# ---- 项目解析（D7：按 PROJECTS 项目名整词匹配，与 POST /api/chat 的键集同口径）----

def _feishu_project_names():
    return sorted(PROJECTS.keys())


def _feishu_project_lines():
    """追问列表行：`<序号>. <项目名> (<目录名>)`（决策 4：显示项目名与目录名）"""
    lines = []
    for i, name in enumerate(_feishu_project_names(), 1):
        dirs = []
        try:
            dirs = [Path(p).name for p in (PROJECTS[name].get("paths") or [])]
        except Exception:
            dirs = []
        suffix = f" ({', '.join(dirs)})" if dirs else ""
        lines.append(f"{i}. {name}{suffix}")
    return lines


def _feishu_project_ask_text():
    return "请回复项目名（或序号）选择要建卡的项目：\n" + "\n".join(_feishu_project_lines())


def _feishu_resolve_project(text):
    """整词匹配项目名；支持纯序号（对应追问列表）。命中返回项目名，否则 None。"""
    t = (text or "").strip()
    if not t:
        return None
    names = _feishu_project_names()
    for name in names:
        if t == name:
            return name
    for name in names:
        if re.search(r"(?<![\w\-])" + re.escape(name) + r"(?![\w\-])", t):
            return name
    if re.fullmatch(r"\d+", t) and 1 <= int(t) <= len(names):
        return names[int(t) - 1]
    return None


# ---- 看板会话 HTTP 原语（全走看板 HTTP API，不直连 sqlite）----

def _feishu_http_err(e):
    """HTTPError → 可读错误文本（优先 body.error）"""
    try:
        body = e.read().decode("utf-8", "replace")
        data = json.loads(body)
        return str(data.get("error") or body)[:300]
    except Exception:
        return f"HTTP {getattr(e, 'code', '?')}"


def _feishu_status_from_error(err):
    """从 board 400「会话状态 <s> 不可对话」文本里取出状态（取不到回落 closed）。"""
    m = re.search(r"会话状态\s*(\S+)", err or "")
    return m.group(1) if m else "closed"


def _feishu_chat_status(session_id):
    """GET /api/chat/<id> → (status, row)；看板不可达返回 (None, None)"""
    try:
        row = api_board("GET", f"/chat/{session_id}")
    except Exception as e:
        log.warning(f"飞书会话状态查询失败（{session_id}）：{e}")
        return None, None
    return (row or {}).get("status"), row


def _feishu_poll_until(session_id, stop, timeout, interval=2.0):
    """轮询 GET /api/chat/<id> 直到 status ∈ stop；closed/failed 提前返回；超时返回最后状态。
    返回 (status, row)。该原语被 D8（等 idle）与 D8‴（等轮次结束）复用。"""
    deadline = time.monotonic() + max(0.0, timeout)
    status, row = None, None
    while True:
        status, row = _feishu_chat_status(session_id)
        if status in stop or status in ("closed", "failed"):
            return status, row
        if time.monotonic() >= deadline:
            return status, row
        time.sleep(interval)


def _feishu_prompt_once(session_id, text, attachments):
    # board 侧 /prompt 限制 text ≤4000 字符（含抓来的文档正文）；超限会被 400 拒绝，这里先截断，
    # 避免正文抓取链把整轮 prompt 顶爆（截断提示保留在末尾）。
    body = text or ""
    if len(body) > FEISHU_PROMPT_MAX:
        body = body[:FEISHU_PROMPT_MAX - 20] + "…（正文过长已截断）"
    return api_board("POST", f"/chat/{session_id}/prompt",
                     {"text": body, "attachments": list(attachments or [])})


def _feishu_reset_session(chat_id, status, row=None):
    """本地状态与看板会话不一致（closed/failed/suspended/created）→ 清本地状态 + 明确出口。"""
    _feishu_chats().pop(chat_id, None)
    _feishu_save()
    if status == "created":
        rid = (row or {}).get("req_id")
        extra = f"（已建卡 #{rid}）" if rid else ""
        _feishu_notify(chat_id, f"会话已建卡{extra}，回复『新建建卡』开始新会话。")
    elif status == "closed":
        _feishu_notify(chat_id, "会话已结束，回复『新建建卡』开始新会话。")
    else:
        _feishu_notify(chat_id, "会话已不可用，回复『新建建卡』开始新会话。")


def _feishu_send_prompt(chat_id, session_id, text, attachments):
    """#22 D8‴：发 prompt 前的状态对账与 409/400 自愈。
    idle/interrupted → 直接发；running → 等到 idle（复用轮询原语）后重发（不做固定次数重试）；
    closed/failed/created/suspended → 清本地状态 + 明确出口；等待超时 → 提示 + 保留 pending（不静默丢消息）。
    返回 True 已发出 / False 未发出。"""
    deadline = time.monotonic() + CHAT_TIMEOUT
    resent = False
    while True:
        status, row = _feishu_chat_status(session_id)
        if status == "running":
            status, row = _feishu_poll_until(
                session_id, {"idle", "interrupted", "closed", "failed", "suspended"},
                max(1.0, deadline - time.monotonic()), interval=2.0)
            if status in ("closed", "failed", "suspended"):
                _feishu_reset_session(chat_id, status, row)
                return False
            if status not in ("idle", "interrupted"):
                _feishu_notify(chat_id, _FEISHU_BUSY_HINT)
                return False
        elif status in ("closed", "failed", "created", "suspended"):
            _feishu_reset_session(chat_id, status, row)
            return False
        try:
            _feishu_prompt_once(session_id, text, attachments)
            return True
        except urllib.error.HTTPError as e:
            err = _feishu_http_err(e)
            if e.code == 409 and not resent:
                # 运行中/boot 窗口：等到 idle 再发一次（不做固定次数重试）
                resent = True
                if time.monotonic() >= deadline:
                    _feishu_notify(chat_id, _FEISHU_BUSY_HINT)
                    return False
                continue   # 回循环顶部：GET 状态 → running 分支等到 idle 后重发
            if e.code == 400 and "不可对话" in err:
                # 会话状态不可对话（closed/failed/suspended 等）：同一自愈出口——清本地状态 + 明确出口
                _feishu_reset_session(chat_id, _feishu_status_from_error(err))
                return False
            log.error(f"飞书 /prompt 失败（{session_id}）：{err}")
            _feishu_notify(chat_id, f"消息发送失败：{err}")
            return False
        except Exception as e:
            log.error(f"飞书 /prompt 异常（{session_id}）：{e}")
            _feishu_notify(chat_id, "消息发送失败，请稍后重试。")
            return False


# ---- 前置对话回放（D8/B11：只回放会话创建前的对话与 dispatcher 侧辅助提示）----

def _feishu_note_replay(sess, kind, content):
    """会话创建前的前置对话/辅助提示暂存，建会话后一次性回放（不落 pending）"""
    sess.setdefault("replay", []).append({"kind": kind, "content": content})


def _feishu_emit(chat_id, sess, kind, content):
    """dispatcher 侧辅助提示（素材失败/超限/文档降级）：有会话直接落库，否则暂存回放"""
    sid = (sess or {}).get("session_id")
    if sid:
        try:
            api_board("POST", f"/chat/{sid}/feishu-events",
                      {"events": [{"kind": kind, "content": content}]})
        except Exception as e:
            log.warning(f"飞书辅助提示落库失败（{sid}）：{e}")
    else:
        _feishu_note_replay(sess, kind, content)


def _feishu_replay(chat_id, session_id, sess):
    """建会话后回放前置对话（带方向前缀），清空暂存。不回放用户原文与建卡事件。"""
    events = list((sess or {}).get("replay") or [])
    if not events:
        return
    try:
        api_board("POST", f"/chat/{session_id}/feishu-events", {"events": events})
    except Exception as e:
        log.warning(f"飞书前置对话回放失败（{session_id}）：{e}")
    sess["replay"] = []


# ---- 素材（D9：句柄延迟下载，字节不进 state）----

def _feishu_ext_ok(name):
    """#22 ⑥：扩展名是否在飞书素材白名单内（无扩展名 = False）"""
    return Path(name or "").suffix.lower() in _FEISHU_MATERIAL_EXTS


def _feishu_parse_material(msg):
    """file/image 消息 → 下载句柄（只含 message_id/key/type/name/size_hint，不含字节）。
    #22 ⑥：image 句柄名必须带可识别扩展名；file 无 file_name → 返回 None（调用方提示跳过），
    不产生无扩展名句柄（否则 board 侧 /feishu-materials 会 400 拖垮整批素材）。"""
    mtype = getattr(msg, "message_type", "")
    if mtype not in ("file", "image"):
        return None
    try:
        content = json.loads(getattr(msg, "content", "") or "{}")
    except Exception:
        return None
    message_id = getattr(msg, "message_id", None)
    if mtype == "image":
        key = content.get("image_key")
        if not key:
            return None
        name = content.get("file_name") or f"图片-{key[:12]}.png"
        if not _feishu_ext_ok(name):
            name = f"{name}.png"
        return {"message_id": message_id, "image_key": key, "type": "image",
                "name": name, "size_hint": None}
    key = content.get("file_key")
    if not key:
        return None
    name = content.get("file_name")
    if not name:
        return None           # 文件名缺失：不产生无扩展名句柄，由调用方提示「已跳过」
    return {"message_id": message_id, "file_key": key, "type": "file",
            "name": name, "size_hint": None}


def _feishu_download_resource(message_id, key, res_type):
    """下载飞书消息资源（im.v1.message_resource.get）→ (bytes, file_name)；失败抛异常。"""
    req = (GetMessageResourceRequest.builder()
           .message_id(message_id)
           .file_key(key)
           .type(res_type)
           .build())
    resp = feishu.im.v1.message_resource.get(req)
    if not resp.success():
        raise RuntimeError(f"{resp.code} {resp.msg}")
    data = resp.file.read() if hasattr(resp.file, "read") else resp.file
    return data, getattr(resp, "file_name", None)


def _feishu_upload_materials(session_id, files):
    """base64 上传到 /feishu-materials；files = [{"name","data"(bytes)}]"""
    payload = {"files": [{"name": f["name"],
                          "content_b64": base64.b64encode(f["data"]).decode()} for f in files]}
    return api_board("POST", f"/chat/{session_id}/feishu-materials", payload)


def _feishu_download_pending(chat_id, sess, session_id):
    """项目选定后下载 pending 素材（句柄 → 字节 → 上传）→ 返回存储名列表（放进 attachments）。"""
    handles = list(((sess.get("pending") or {}).get("materials")) or [])
    sess.setdefault("pending", {"text": "", "materials": []})["materials"] = []
    if not handles:
        return []
    files = []
    for h in handles:
        key = h.get("file_key") or h.get("image_key")
        try:
            data, fname = _feishu_download_resource(h.get("message_id"), key, h.get("type") or "file")
        except Exception as e:
            log.warning(f"飞书素材下载失败（{h.get('name')}）：{e}")
            _feishu_notify(chat_id, f"文件「{h.get('name')}」下载失败：{e}")
            _feishu_emit(chat_id, sess, "system", f"[飞书] 素材「{h.get('name')}」下载失败：{e}")
            continue
        # #22 ⑥：上传前最终文件名兜底——扩展名不在白名单则 image 补 .png，其它跳过并提示
        name = fname or h.get("name") or key
        if not _feishu_ext_ok(name):
            if (h.get("type") or "") == "image":
                name = f"{name or key}.png"
            else:
                label = h.get("name") or name
                _feishu_notify(chat_id, f"文件「{label}」文件名缺失或扩展名不受支持，已跳过。")
                _feishu_emit(chat_id, sess, "system",
                             f"[飞书] 素材「{label}」文件名缺失或扩展名不受支持，已跳过")
                continue
        files.append({"name": name, "data": data})
    if not files:
        return []
    try:
        resp = _feishu_upload_materials(session_id, files)
    except urllib.error.HTTPError as e:
        msg = _feishu_http_err(e)
        _feishu_notify(chat_id, f"参考文件上传失败：{msg}")
        _feishu_emit(chat_id, sess, "system", f"[飞书] 参考文件上传失败：{msg}")
        return []
    except Exception as e:
        log.warning(f"飞书参考文件上传异常：{e}")
        _feishu_notify(chat_id, "参考文件上传失败，请重试。")
        return []
    names = []
    for f in (resp or {}).get("files") or []:
        fn = f.get("filename")
        if fn:
            names.append(fn)
        if f.get("over_read_limit"):
            hint = f"文件 {fn} 超过 agent 单文件读取上限 1MB，可能读取不完整"
            _feishu_notify(chat_id, hint)
            _feishu_emit(chat_id, sess, "system", f"[飞书] {hint}")
    return names


# ---- 正文抓取链（D10：docx 直链 / wiki 两跳 / 普通 http；每步失败降级仅传链接）----

def _feishu_find_links(text):
    return re.findall(r"https?://[^\s<>\"']+", text or "")


def _feishu_ip_public(ip_str):
    """IP 是否属于可抓取的公网地址（私有/回环/链路本地/保留/多播/未指定 一律 False）。"""
    try:
        ip = ipaddress.ip_address(str(ip_str))
    except Exception:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def _feishu_url_allowed(url):
    """#22 ⑤ SSRF 守卫：仅允许公网 http/https。
    拒 scheme 非 http(s)、host 为空/localhost/.local/.internal；DNS 解析出任一 IP 属于
    私有/回环/链路本地/保留/多播/未指定，或解析失败 → 拒。"""
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    return all(_feishu_ip_public(info[4][0]) for info in infos)


class _FeishuRedirectGuard(urllib.request.HTTPRedirectHandler):
    """#22 ⑤：重定向每一跳都复跑 SSRF 守卫。默认 handler 直接跟随 Location，
    公网地址 302 到回环/链路本地即绕过「只校验原文 URL」的守卫。"""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _feishu_url_allowed(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"{_FEISHU_LINK_REJECT_HINT}（重定向目标 {newurl}）", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 抓正文专用 opener：与共享 _opener 隔离，redirect 每跳复校 SSRF
_feishu_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             _FeishuRedirectGuard())


def _feishu_connected_ip(resp):
    """已建立连接的对端 IP（DNS 重绑定兜底：连接到私有地址即拒，不读响应体）。"""
    try:
        return resp.fp.raw._sock.getpeername()[0]
    except Exception:
        return None


def _feishu_is_feishu_host(url):
    """#22 ⑤：host 是否属于飞书域名（feishu.cn / larksuite.com / feishu.net 及其子域，大小写不敏感）"""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    return any(host == dom or host.endswith("." + dom) for dom in _FEISHU_DOMAINS)


def _feishu_fetch_link_text(url, cap=200 * 1024, timeout=10):
    """普通 http(s) 抓正文：urllib（走禁代理 opener）→ 去标签 → 压缩空白，cap 200KB。
    #22 ⑤：先过 SSRF 守卫，非公网地址直接拒绝（抛 ValueError，调用方降级提示）；
    请求走 `_feishu_opener`（重定向每跳复校），连接建立后再核对对端 IP（DNS 重绑定兜底）。"""
    if not _feishu_url_allowed(url):
        raise ValueError(_FEISHU_LINK_REJECT_HINT)
    req = urllib.request.Request(url, headers={"User-Agent": "req-board-dispatcher"})
    with _feishu_opener.open(req, timeout=timeout) as resp:
        peer = _feishu_connected_ip(resp)
        if peer is not None and not _feishu_ip_public(peer):
            raise ValueError(f"{_FEISHU_LINK_REJECT_HINT}（对端 {peer}）")
        raw = resp.read(cap)
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _feishu_docx_raw(document_id):
    """docx.v1.document.raw_content（token 即 document_id）→ 正文文本；失败抛异常。"""
    req = RawContentDocumentRequest.builder().document_id(document_id).build()
    resp = feishu.docx.v1.document.raw_content(req)
    if not resp.success():
        raise RuntimeError(f"{resp.code} {resp.msg}")
    return (resp.data.content if resp.data else "") or ""


def _feishu_wiki_one_level(node_token):
    """wiki 两步：先 wiki v2 Space.get_node 取 obj_token/obj_type（只解析一层），
    obj_type=='docx' 才调 raw_content；其它类型降级。返回 (text|None, note|None)。"""
    req = GetNodeSpaceRequest.builder().token(node_token).build()
    resp = feishu.wiki.v2.space.get_node(req)
    if not resp.success():
        raise RuntimeError(f"{resp.code} {resp.msg}")
    node = resp.data.node if resp.data else None
    obj_type = getattr(node, "obj_type", None)
    obj_token = getattr(node, "obj_token", None)
    if obj_type == "docx" and obj_token:
        return _feishu_docx_raw(obj_token), None
    return None, f"该类型文档（{obj_type or '未知'}）暂不支持抓正文"


def _feishu_fetch_doc_text(url):
    """按 URL 形态抓正文；返回 (text|None, note|None)。非 http(s) 不抓。
    #22 ⑤：`/docx/`、`/wiki/` 路径**仅在飞书域名下**才走飞书文档 API，否则按普通链接处理
    （并过 SSRF 守卫）；被守卫拒绝时返回降级提示（不阻断本轮）。"""
    if not re.match(r"^https?://", url or ""):
        return None, None
    try:
        if _feishu_is_feishu_host(url):
            m = re.search(r"/docx/([A-Za-z0-9]+)", url)
            if m:
                return _feishu_docx_raw(m.group(1)), None
            m = re.search(r"/wiki/([A-Za-z0-9]+)", url)
            if m:
                return _feishu_wiki_one_level(m.group(1))
        if not _feishu_url_allowed(url):
            return None, _FEISHU_LINK_REJECT_HINT
        return _feishu_fetch_link_text(url), None
    except Exception as e:
        return None, f"链接内容抓取失败：{e}"


def _feishu_expand_text(chat_id, sess, text):
    """把文本里的链接抓成正文追加；失败降级为仅链接 + 提示（不阻断本轮）。
    #22 ⑤：非飞书文档链接先过 SSRF 守卫，被拒只提示不抓取。"""
    out = text or ""
    for url in _feishu_find_links(out):
        if not _feishu_is_feishu_host(url) and not _feishu_url_allowed(url):
            _feishu_notify(chat_id, f"{_FEISHU_LINK_REJECT_HINT}（{url}）")
            _feishu_emit(chat_id, sess, "system", f"[飞书] {_FEISHU_LINK_REJECT_HINT}（{url}）")
            continue
        doc_text, note = _feishu_fetch_doc_text(url)
        if doc_text:
            out += f"\n\n[链接正文 {url}]\n{doc_text}"
        elif note:
            _feishu_notify(chat_id, f"{note}（{url}）")
            _feishu_emit(chat_id, sess, "system", f"[飞书] {note}（{url}）")
    return out


# ---- 建卡会话编排（D8/D8′/D11）----

def _feishu_boot_and_prompt(chat_id, sess):
    """#22 D8：建会话 → 等 idle → 回放前置对话 → 发首轮 prompt → 等待本轮结束。
    409 → 提示两条出口并保留 pending；boot 超时/failed → 提示 + 保留 session_id（下次走 D8‴ 对账）。"""
    project = sess.get("project")
    try:
        resp = api_board("POST", "/chat", {"project": project, "channel": "feishu"})
    except urllib.error.HTTPError as e:
        if e.code == 409:
            _feishu_notify(chat_id, "该项目已有进行中的飞书建卡会话：在飞书回复『结束建卡』，"
                                    "或在看板「飞书建卡」视图点『关闭会话』。")
            return None
        msg = _feishu_http_err(e)
        log.error(f"飞书建会话失败（{project}）：{msg}")
        _feishu_notify(chat_id, f"创建会话失败：{msg}")
        return None
    except Exception as e:
        log.error(f"飞书建会话异常（{project}）：{e}")
        _feishu_notify(chat_id, "创建会话失败，请稍后重试。")
        return None
    session_id = (resp or {}).get("id")
    if not session_id:
        _feishu_notify(chat_id, "创建会话失败（未返回会话 id）。")
        return None
    sess["session_id"] = session_id
    sess["updated_at"] = time.time()
    _feishu_save()
    status, row = _feishu_poll_until(session_id, {"idle"}, FEISHU_BOOT_WAIT_SEC, interval=1.0)
    if status != "idle":
        if status == "failed":
            _feishu_notify(chat_id, "会话启动失败，回复任意消息重试。")
        else:
            _feishu_notify(chat_id, "会话启动超时，回复任意消息重试。")
        return session_id   # 保留 session_id，下次消息走 D8‴ 对账
    _feishu_replay(chat_id, session_id, sess)
    text = _feishu_expand_text(chat_id, sess, (sess.get("pending") or {}).get("text") or "")
    attachments = _feishu_download_pending(chat_id, sess, session_id)
    if not text and not attachments:
        text = "（请开始建卡）"
    if _feishu_send_prompt(chat_id, session_id, text, attachments):
        sess["pending"] = {"text": "", "materials": []}
        sess["updated_at"] = time.time()
        _feishu_save()
        _feishu_await_round(chat_id, session_id, sess.get("last_event_id", 0))
    return session_id


def _feishu_send_user_prompt(chat_id, sess, text):
    """活跃建卡会话的后续讨论轮（含链接抓取 + 待续发 pending 合并）。"""
    session_id = sess.get("session_id")
    pending = sess.get("pending") or {}
    combined = "\n".join(x for x in [pending.get("text"), text] if x)
    body = _feishu_expand_text(chat_id, sess, combined)
    attachments = _feishu_download_pending(chat_id, sess, session_id)
    if _feishu_send_prompt(chat_id, session_id, body, attachments):
        sess["pending"] = {"text": "", "materials": []}
        sess["updated_at"] = time.time()
        _feishu_save()
        _feishu_await_round(chat_id, session_id, sess.get("last_event_id", 0))
    else:
        sess.setdefault("pending", {"text": "", "materials": []})["text"] = combined
        _feishu_save()


def _feishu_ask_project(chat_id, sess):
    text = _feishu_project_ask_text()
    sess["awaiting_project"] = True
    _feishu_note_replay(sess, "agent", f"[飞书] 机器人：{text}")
    _feishu_notify(chat_id, text)


def _feishu_new_sess(project=None, text="", handle=None):
    materials = [handle] if handle else []
    return {"project": project, "session_id": None, "last_event_id": 0,
            "pending": {"text": text or "", "materials": materials},
            "awaiting_project": project is None, "project_tries": 0,
            "replay": [], "updated_at": time.time()}


def _feishu_handle_text(chat_id, text):
    """文本消息处理：无会话 → 建卡讨论（解析项目）；有会话无 session → 项目选择；有 session → 续跑。"""
    fc = _feishu_chats()
    sess = fc.get(chat_id)
    if sess is None:
        sess = _feishu_new_sess(None, text)
        fc[chat_id] = sess
        project = _feishu_resolve_project(text)
        if project:
            sess["project"] = project
            sess["awaiting_project"] = False
            _feishu_boot_and_prompt(chat_id, sess)
        else:
            _feishu_ask_project(chat_id, sess)
        _feishu_save()
        return
    if sess.get("session_id"):
        _feishu_send_user_prompt(chat_id, sess, text)
        return
    # 会话已建但尚未拿到 session_id（等 idle / boot 超时）→ 视为续跑，走 D8‴ 对账
    project = _feishu_resolve_project(text)
    if project and not sess.get("awaiting_project"):
        sess["project"] = project
    if sess.get("awaiting_project"):
        if project:
            sess["project"] = project
            sess["awaiting_project"] = False
            _feishu_note_replay(sess, "user", f"[飞书] 用户：{text}")
            _feishu_boot_and_prompt(chat_id, sess)
        else:
            # #24：追问项目期间收到的文本先累积进 pending（「新建建卡」→ 需求原文 → 项目名
            # 三步序里需求原文不再被丢弃），选定项目后随首轮 prompt 一起发给 agent。
            if text:
                pend = sess.setdefault("pending", {"text": "", "materials": []})
                pend["text"] = "\n".join(x for x in [pend.get("text"), text] if x)
            sess["project_tries"] = sess.get("project_tries", 0) + 1
            if sess["project_tries"] >= 2:
                _feishu_notify(chat_id, _feishu_project_ask_text()
                               + "\n仍无法识别项目，本次建卡已取消，可重新发送需求。")
                fc.pop(chat_id, None)
            else:
                _feishu_ask_project(chat_id, sess)
    else:
        _feishu_send_user_prompt(chat_id, sess, text)
    _feishu_save()


def _feishu_handle_material(chat_id, handle):
    """素材消息处理：有活跃旧建卡会话 → 句柄存 pending（延迟下载）走旧链路；无旧会话 → #44 交给助手
    （下载后作为本轮附件提交，不再新建建卡会话追问项目）。"""
    fc = _feishu_chats()
    sess = fc.get(chat_id)
    if sess is None:
        _feishu_material_to_assistant(chat_id, handle)
        return
    sess.setdefault("pending", {"text": "", "materials": []})
    sess["pending"].setdefault("materials", []).append(handle)
    if sess.get("session_id"):
        session_id = sess["session_id"]
        attachments = _feishu_download_pending(chat_id, sess, session_id)
        if attachments and _feishu_send_prompt(chat_id, session_id, "（上传了参考文件）", attachments):
            sess["updated_at"] = time.time()
            _feishu_save()
            _feishu_await_round(chat_id, session_id, sess.get("last_event_id", 0))
    elif not sess.get("awaiting_project"):
        _feishu_ask_project(chat_id, sess)
    _feishu_save()


def _feishu_close_chat(chat_id):
    """#22 D8′：释放路径 ①（指令 结束建卡/新建建卡）——close 看板会话 + 清本地状态 + 回执。"""
    sess = _feishu_chats().get(chat_id)
    sid = (sess or {}).get("session_id")
    if sid:
        try:
            api_board("POST", f"/chat/{sid}/close")
        except Exception as e:
            log.warning(f"飞书关闭会话失败（{sid}）：{e}")
    _feishu_chats().pop(chat_id, None)
    _feishu_save()
    _feishu_notify(chat_id, "已结束本次建卡会话。回复『新建建卡』开始新的建卡会话。")


def _feishu_start_build(chat_id):
    """#24：无活跃旧建卡会话时的「新建建卡」= 旧建卡新建分支——清掉残留本地状态后起一轮
    新的单项目建卡讨论。后续文本因本地会话存在继续走旧链路（未定项目时由旧链路追问）。
    这里只回一条普通提示（不落前置回放），避免与随后的项目追问重复一条「机器人」回放事件。"""
    sess = _feishu_chats().get(chat_id)
    sid = (sess or {}).get("session_id")
    if sid:
        try:
            api_board("POST", f"/chat/{sid}/close")
        except Exception as e:
            log.warning(f"飞书关闭会话失败（{sid}）：{e}")
    _feishu_chats().pop(chat_id, None)
    _feishu_chats()[chat_id] = _feishu_new_sess(None, "")
    _feishu_save()
    _feishu_notify(chat_id, FEISHU_NEW_BUILD_HINT)


def _feishu_route_text(chat_id, text, message_id=None):
    """#24 文本路由（on_message 的 ③′-⑥）：
    ① 有活跃旧建卡会话（state["feishu_chats"][chat_id] 命中）→ 三条指令与所有文本走旧链路；
    ② 无活跃旧会话且整行精确规范化「新建建卡」→ 旧建卡新建分支；
    ③ 无活跃旧会话的「结束建卡」「确认建卡」→ 只回状态提示，不启动任何会话；
    ④ 其余普通文本 → 看板助手路由。
    原「无活跃会话时默认新建单项目建卡会话」的兜底分支已删除（普通文本改由助手接管）。
    #44：`message_id` 只进 assistant/legacy_text/material 三类 payload（worker 据此贴/撤 Typing 表情）；
    close/confirm/new 与未入队的提示不带（不贴表情）。"""
    sess = _feishu_chats().get(chat_id)
    if sess is not None:
        if _feishu_is_cmd(text, FEISHU_CMD_END) or _feishu_is_cmd(text, FEISHU_CMD_NEW):
            _feishu_enqueue(chat_id, {"kind": "close", "cmd": text})
            return
        if _feishu_is_cmd(text, FEISHU_CMD_CONFIRM):
            _feishu_enqueue(chat_id, {"kind": "confirm", "cmd": text})
            return
        _feishu_enqueue(chat_id, {"kind": "legacy_text", "text": text, "message_id": message_id})
        return
    if _feishu_is_cmd(text, FEISHU_CMD_NEW):
        _feishu_enqueue(chat_id, {"kind": "new", "cmd": text})
        return
    if _feishu_is_cmd(text, FEISHU_CMD_END):
        _feishu_notify(chat_id, FEISHU_NO_SESSION_END_HINT)
        return
    if _feishu_is_cmd(text, FEISHU_CMD_CONFIRM):
        _feishu_notify(chat_id, FEISHU_NO_SESSION_CONFIRM_HINT)
        return
    _feishu_enqueue(chat_id, {"kind": "assistant", "text": text, "message_id": message_id})


def _feishu_confirm(chat_id, sess):
    """#22 ① 飞书侧「确认建卡」：读会话行 → 已建卡/无草稿/正常三条出口。
    正常 → POST /chat/<id>/confirm 驱动 create_requirement → 等本轮结束把建卡结果回发。
    400/409 用看板错误文案明示失败原因（不静默）。"""
    sid = (sess or {}).get("session_id")
    if not sid:
        _feishu_notify(chat_id, "还没有草稿，请先和机器人讨论出草稿。")
        return
    status, row = _feishu_chat_status(sid)
    if status == "created":
        rid = (row or {}).get("req_id")
        extra = f" #{rid}" if rid else ""
        _feishu_notify(chat_id, f"本会话已建卡{extra}，回复『新建建卡』开始新会话。")
        return
    if not ((row or {}).get("draft_title") or "").strip():
        _feishu_notify(chat_id, "还没有草稿，请先和机器人讨论出草稿。")
        return
    sess["draft_prompted"] = (row or {}).get("draft_title")
    try:
        api_board("POST", f"/chat/{sid}/confirm")
    except urllib.error.HTTPError as e:
        _feishu_notify(chat_id, f"确认建卡失败：{_feishu_http_err(e)}")
        return
    except Exception as e:
        log.error(f"飞书确认建卡异常（{sid}）：{e}")
        _feishu_notify(chat_id, "确认建卡失败，请稍后重试。")
        return
    sess["updated_at"] = time.time()
    _feishu_save()
    _feishu_await_round(chat_id, sid, sess.get("last_event_id", 0))


def _feishu_draft_ready_prompt(chat_id, sess, row):
    """#22 ①：每轮结束读会话行，草稿就绪且本轮尚未提示过 → 提示「回复『确认建卡』」。
    同一草稿只提示一次；已建卡（req_id）不再提示。"""
    title = ((row or {}).get("draft_title") or "").strip()
    if not title or sess is None or (row or {}).get("req_id"):
        return
    if sess.get("draft_prompted") == title:
        return
    sess["draft_prompted"] = title
    _feishu_notify(chat_id, f"草稿已就绪：{title}。回复『确认建卡』落卡；回复其它内容继续讨论。")


def _feishu_await_round(chat_id, session_id, after_id):
    """#22 D11：轮询 /chat/<id> + /events?after= 直到 status ∉ running；agent 正文
    （kind='message'，见 acp_shared._flush_message）发回飞书——kind='agent' 是回放事件
    （机器人自己说过的话），**不得再回发**（否则重复）。
    上限 CHAT_TIMEOUT + 60s。interrupted/failed → 提示并保留 session 供下一条消息续跑。
    每轮结束：草稿就绪则提示「回复『确认建卡』」。"""
    deadline = time.monotonic() + CHAT_TIMEOUT + 60
    last_id = after_id or 0
    while True:
        status, row = _feishu_chat_status(session_id)
        try:
            events = api_board("GET", f"/chat/{session_id}/events?after={last_id}") or []
        except Exception as e:
            log.warning(f"飞书轮次事件拉取失败（{session_id}）：{e}")
            events = []
        for ev in events:
            eid = ev.get("id")
            if isinstance(eid, int) and eid > last_id:
                last_id = eid
            if ev.get("kind") == "message" and ev.get("content"):
                send_text_chunked(chat_id, ev["content"])
        sess = _feishu_chats().get(chat_id)
        if sess is not None:
            sess["last_event_id"] = last_id
            sess["updated_at"] = time.time()
        _feishu_save()
        if status is not None and status != "running":
            if status in ("interrupted", "failed"):
                _feishu_notify(chat_id, "本轮执行中断或失败，可回复任意消息继续。")
            _feishu_draft_ready_prompt(chat_id, sess, row)
            return last_id
        if time.monotonic() >= deadline:
            _feishu_notify(chat_id, "本轮执行超时，可回复任意消息查看进展。")
            return last_id
        time.sleep(2)


# ==================== 看板助手会话编排（#24） ====================
# 与单项目建卡完全分离：本地状态 state["feishu_assistant"][chat_id] 只存编排元数据
# （session_id / feishu_chat_id / last_event_id / pending / awaiting_project / turn_id / updated_at），
# **不存会话原文与素材字节**；board 侧会话与事件落独立的助手表。
# 计时：worker 起表 90s 总 deadline → 每次调用 board 传剩余秒数（board 权威计时并自行收尾），
# dispatcher 只做超时兜底（cancel + 固定文案一次，保留 session_id 供下次 resume）。
# 不复用建卡的 _chat_boot/_chat_prompt_thread/_start_chat_round 与 CHAT_TIMEOUT(600s) 语义。


def _feishu_assistant():
    """state["feishu_assistant"] 访问器（缺失时补空 dict；与 state["feishu_chats"] 完全分离）"""
    a = state.get("feishu_assistant")
    if not isinstance(a, dict):
        a = {}
        state["feishu_assistant"] = a
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
        row = api_board("GET", f"/assistant/sessions/{sid}?{_feishu_assistant_q(chat_id)}")
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
        api_board("POST", f"/assistant/sessions/{sid}/cancel",
                  {"feishu_chat_id": chat_id, "turn_id": turn_id})
    except Exception as e:
        log.warning(f"飞书助手轮次取消失败（{sid}）：{e}")
    entry["pending"] = False
    entry["turn_id"] = None
    entry["updated_at"] = time.time()
    _feishu_save()
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
        time.sleep(FEISHU_ASSISTANT_POLL_SEC)
    return False


def _feishu_assistant_ensure_session(chat_id, entry):
    """get-or-create 助手会话（带解析后的 assistant_model，未配置传 None 走 board 默认链）；
    返回 (sid|None, err|None)。"""
    try:
        row = api_board("POST", "/assistant/sessions",
                        {"feishu_chat_id": chat_id, "model": FEISHU_ASSISTANT_MODEL})
    except urllib.error.HTTPError as e:
        err = _feishu_http_err(e)
        log.error(f"飞书助手建会话失败（{chat_id}）：{err}")
        return None, f"{FEISHU_ASSISTANT_DOWN_HINT}：{err}"
    except Exception as e:
        log.error(f"飞书助手建会话异常（{chat_id}）：{e}")
        return None, FEISHU_ASSISTANT_DOWN_HINT
    sid = (row or {}).get("id")
    if not sid:
        return None, f"{FEISHU_ASSISTANT_DOWN_HINT}（未返回会话 id）"
    entry["session_id"] = sid
    entry["feishu_chat_id"] = chat_id
    entry["updated_at"] = time.time()
    _feishu_save()
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
        resp = api_board("POST", f"/assistant/sessions/{sid}/prompt", payload)
    except urllib.error.HTTPError as e:
        err = _feishu_http_err(e)
        if e.code == 409:
            return None, "busy"
        log.error(f"飞书助手 prompt 失败（{sid}）：{err}")
        return None, f"{FEISHU_ASSISTANT_DOWN_HINT}：{err}"
    except Exception as e:
        log.error(f"飞书助手 prompt 异常（{sid}）：{e}")
        return None, FEISHU_ASSISTANT_DOWN_HINT
    turn_id = (resp or {}).get("turn_id")
    if not turn_id:
        return None, f"{FEISHU_ASSISTANT_DOWN_HINT}（未返回轮次 id）"
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
            resp = api_board(
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
                send_text_chunked(chat_id, ev["content"])
            elif ev.get("kind") == "card" and ev.get("content"):
                # #30：需求卡清单卡片（服务端生成的页 spec）→ dispatcher 渲染并显式 chat_id 直发
                _feishu_send_assistant_card(chat_id, ev["content"])
        entry["updated_at"] = time.time()
        _feishu_save()
        if status is not None and status != "running":
            entry["pending"] = False
            entry["turn_id"] = None
            entry["updated_at"] = time.time()
            _feishu_save()
            if status in ("interrupted", "failed"):
                _feishu_notify(chat_id, "本轮执行中断或失败，重发一次可继续。")
            return
        if time.monotonic() >= deadline:
            # 90s 到点 board 仍未收尾：dispatcher 兜底 cancel + 固定文案（同轮只发一次）
            # cancel 用 board 会话行当前 turn_id（board 按 turn_id 精确栅栏，给错值会被拒）
            _feishu_assistant_cancel_pending(chat_id, entry, turn_id=(row or {}).get("turn_id"))
            _feishu_notify(chat_id, FEISHU_ASSISTANT_TIMEOUT_TEXT)
            return
        time.sleep(FEISHU_ASSISTANT_POLL_SEC)


def _feishu_assistant_round(chat_id, text, attachments=None, prepare=None):
    """#24/#44 助手单条消息共用的编排：起表 90s 总 deadline → get-or-create 会话 → 提交一轮 →
    轮询事件与状态 → 收尾。任一环节失败都给出明确降级回复（不静默丢消息）；
    提交撞 409（上一轮收尾中）时按状态栅栏等到可续跑再试一次，不被永久挡住。
    prepare（#44 素材路径用）：会话就绪后、提交前取本轮附件，返回 (attachments|None, err|None)；
    err 非空 → 只回复原因、不提交本轮（重试只重发同一轮，不重复启动轮次）。"""
    deadline = time.monotonic() + FEISHU_ASSISTANT_DEADLINE_SEC
    entry = _feishu_assistant_entry(chat_id)
    if entry.get("pending"):
        _feishu_assistant_cancel_pending(chat_id, entry)
    sid, err = _feishu_assistant_ensure_session(chat_id, entry)
    if sid is None:
        _feishu_notify(chat_id, err or FEISHU_ASSISTANT_DOWN_HINT)
        return False
    if prepare is not None:
        attachments, err = prepare()
        if err:
            _feishu_notify(chat_id, err)
            return False
    turn_id, err = _feishu_assistant_submit(chat_id, entry, sid, text, deadline, attachments=attachments)
    if turn_id is None and err == "busy":
        if _feishu_assistant_wait_resumable(chat_id, entry, deadline):
            turn_id, err = _feishu_assistant_submit(chat_id, entry, sid, text, deadline,
                                                    attachments=attachments)
        else:
            err = FEISHU_ASSISTANT_BUSY_HINT
    if turn_id is None:
        _feishu_notify(chat_id, err or FEISHU_ASSISTANT_DOWN_HINT)
        return False
    entry["turn_id"] = turn_id
    entry["pending"] = True
    entry["updated_at"] = time.time()
    _feishu_save()
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
    if not _feishu_ext_ok(name):
        return None, f"不支持的文件类型，已跳过：{name}"
    key = handle.get("image_key") if kind == "image" else handle.get("file_key")
    if not key:
        return None, f"文件内容不可解析，已跳过：{name}"
    try:
        data, _fname = _feishu_download_resource(handle.get("message_id"), key, kind)
    except Exception as e:  # noqa: BLE001 - 下载失败只回原因，不带坏文件进本轮
        log.warning(f"飞书素材下载失败（{handle.get('message_id')}）：{e}")
        return None, f"参考文件下载失败，已跳过：{name}"
    if len(data) > _FEISHU_MATERIAL_MAX_BYTES:
        return None, (f"文件超过 {_FEISHU_MATERIAL_MAX_BYTES // (1024 * 1024)}MB 上限，"
                      f"已跳过：{name}")
    # board 侧契约：{name, content_b64}；字节只在本轮局部变量与本次请求体里存在，不写入 state
    return [{"name": name, "content_b64": base64.b64encode(data).decode()}], None


def _feishu_assistant_handles_prepare(handles):
    """多素材（富文本多图/多文件）→ 本轮附件：逐条复用单文件下载与校验，超过 board 单轮上限截断；
    单条失败跳过并留日志；全部失败 → 返回原因（不发起本轮）。返回 (attachments|None, err|None)。"""
    items = [h for h in (handles or []) if isinstance(h, dict)]
    if len(items) > _FEISHU_MATERIAL_MAX_FILES:
        log.warning(f"飞书素材超过单轮上限 {_FEISHU_MATERIAL_MAX_FILES} 个，已截断（共 {len(items)} 个）")
        items = items[:_FEISHU_MATERIAL_MAX_FILES]
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
        _feishu_save()


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
        _feishu_handle_text(chat_id, payload.get("text") or "")
    elif kind == "legacy_text":
        if _feishu_chats().get(chat_id) is None:
            _feishu_assistant_safe(chat_id, payload.get("text") or "")
        else:
            _feishu_handle_text(chat_id, payload.get("text") or "")
    elif kind == "assistant":
        _feishu_assistant_safe(chat_id, payload.get("text") or "",
                               payload.get("handles") or [])
    elif kind == "material":
        _feishu_handle_material(chat_id, payload.get("handle") or {})
    elif kind == "close":
        _feishu_close_chat(chat_id)
    elif kind == "new":
        _feishu_start_build(chat_id)
    elif kind == "confirm":
        _feishu_confirm(chat_id, _feishu_chats().get(chat_id))


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
        _feishu_notify(chat_id, FEISHU_ASSISTANT_DOWN_HINT)
        return False


def _feishu_worker(chat_id, q):
    while True:
        payload = q.get()
        # #44：只有真正入队处理的助手/旧建卡文本/素材三类消息贴 Typing 表情（close/confirm/new 不贴），
        # 处理前贴、finally 撤；create 失败（rid 为 None）不撤。task_done 在最外层 finally，不被表情异常吞掉。
        kind = (payload or {}).get("kind")
        mid = (payload or {}).get("message_id")
        rid = _feishu_typing(mid) if (mid and kind in ("assistant", "legacy_text", "material")) else None
        try:
            _feishu_handle(chat_id, payload)
        except Exception as e:
            log.error(f"飞书建卡消息处理异常（{chat_id}）：{e}")
        finally:
            try:
                if rid:
                    _feishu_typing(mid, rid)
            finally:
                q.task_done()


def _feishu_enqueue(chat_id, payload):
    """#22 D6：入队即返回（per-chat worker 异步串行处理），不阻塞 ws 回调线程。"""
    with _feishu_queue_lock:
        q = _feishu_queues.get(chat_id)
        if q is None:
            q = queue.Queue()
            _feishu_queues[chat_id] = q
            t = threading.Thread(target=_feishu_worker, args=(chat_id, q), daemon=True)
            _feishu_workers[chat_id] = t
            t.start()
    q.put(payload)


# ---- 空闲会话过期兜底（D8″：释放路径 ③）----

def _feishu_expire_idle_chats():
    """#22 D8″：主循环低频巡检（节流 ≥300s）。只处理 channel=feishu 且 status=idle、updated_at
    超阈值、本地无 pending 的会话 → close + 清本地状态 + 日志；不碰 web 会话。
    IDLE_SEC=0 关闭兜底；飞书未启用（无客户端）时无会话可释放，直接跳过（也避免单测触网）。"""
    global _last_feishu_idle_sweep
    if feishu is None or FEISHU_CHAT_IDLE_SEC <= 0:
        return
    now = time.monotonic()
    if now - _last_feishu_idle_sweep < FEISHU_IDLE_SWEEP_INTERVAL:
        return
    _last_feishu_idle_sweep = now
    try:
        rows = api_board("GET", "/chat?channel=feishu&status=idle&limit=50") or []
    except Exception as e:
        log.warning(f"飞书空闲会话巡检：拉取失败（{e}）")
        return
    closed = 0
    for row in rows:
        if (row.get("channel") or "web") != "feishu" or row.get("status") != "idle":
            continue
        age = _session_age_sec(row.get("updated_at"))
        if age is None or age <= FEISHU_CHAT_IDLE_SEC:
            continue
        cid, local = _feishu_local_by_session(row.get("id"))
        if local is not None and ((local.get("pending") or {}).get("text")):
            continue   # 本地有未投递消息，交给用户消息驱动，不兜底关闭
        try:
            api_board("POST", f"/chat/{row.get('id')}/close")
        except Exception as e:
            log.warning(f"飞书空闲会话关闭失败（{row.get('id')}）：{e}")
            continue
        if cid is not None:
            _feishu_chats().pop(cid, None)
        _feishu_save()
        log.info(f"飞书空闲会话已过期关闭：session={row.get('id')}"
                 f"（闲置 > {FEISHU_CHAT_IDLE_SEC}s）")
        closed += 1
    if closed:
        log.info(f"飞书空闲会话巡检：关闭 {closed} 个")


# ==================== agent 运行（ACP 模式） ====================
active_lock = threading.Lock()
active_runs = {}  # req_id(str) -> {"proc": Popen, "project": str, "role": str, "owner": 会话归属键,
                  #                "session": ACPSession, "started_at": float}（#388 session/started_at 供兜底收尾）

# #388 兜底收尾防重：主循环 _sweep_dead_running_once 已补收尾的会话行 id 集合（进程死场景）。
# 会话线程 finally 恢复后经 _swept 检查跳过重复 post_run_record / handle_result
# （避免失败通道二次重试计数与重复 spawn 竞态）；sweep 线程与会话线程并发访问，用锁保护
_swept_rows = set()
_swept_lock = threading.Lock()


def _swept(row_id):
    with _swept_lock:
        return row_id in _swept_rows


def _mark_swept(row_id):
    with _swept_lock:
        _swept_rows.add(row_id)


def readonly_tool_roots(project, workdir, artifact_root):
    """工具 roots 构造：[会话工作目录] + 项目各真实仓库根 + 产物目录。
    多 paths 项目会话 cwd 是聚合目录（symlink 结构），文件逻辑路径 resolve 后落在真实仓库根，
    因此必须含各真实仓库根，否则聚合目录下工具路径全被误判越权。"""
    roots = [Path(workdir)]
    for p in (PROJECTS.get(project, {}).get("paths") or []):
        roots.append(Path(p))
    roots.append(Path(artifact_root))
    return roots
def spawn_agent(req, role=None, extra="", resume=False, model_override=None):
    role = role or role_for(req)
    # #430：空角色 fail-closed——配置降级下 role_for 恒为空串，放行会把卡误置 doing 且 build_prompt
    # 抛 ValueError，卡停在 doing（主循环只捞 open，无法自愈）。守卫必须先于 set_req(doing)。
    if not role:
        log.error(f"需求 #{req['id']} 无角色可拉起（流程配置降级/前置槽位为空）：拒绝启动会话，卡保持原状态")
        try:
            post_dispatcher_event(req["id"], "无角色可拉起（流程配置降级）：会话未启动，需求保持原状态",
                                  kind="action")
        except Exception:
            pass
        return
    # #279：角色与需求 mode/phase 不匹配即非法调用（绕过调度器直接拉起错误角色会话），
    # 在置 doing/写 spawn 事件之前拦截，仅留告警事件
    if not role_allowed(req, role):
        log.error(f"需求 #{req['id']} 拒绝拉起角色 {role}（mode={req.get('mode')}, phase={req.get('phase')}）："
                  f"角色与需求当前阶段不匹配，疑似绕过调度器直接调用")
        try:
            post_dispatcher_event(req["id"],
                                  f"拒绝拉起非法角色 {role}（mode={req.get('mode')}, phase={req.get('phase')}），会话未启动",
                                  kind="action")
        except Exception:
            pass
        return
    req_id = str(req["id"])
    project = req["project"]
    if role in project_external_roles(project):
        # 主 Agent 协作模式：该角色由外部主 Agent 负责——不建会话，停卡等待外部产出；外部写好产物后
        # 把卡指向下一节点并改回待开发即可继续（评审打回到该角色同样停在这里）
        set_status(req["id"], "later")
        append_summary(req_id, f"{ROLE_NAMES.get(role, role)}由外部主 Agent 负责，等待其产出后继续。")
        post_dispatcher_event(req_id, f"「{ROLE_NAMES.get(role, role)}」由外部主 Agent 负责（projects.json external_roles），"
                                      "未拉起会话，需求转暂缓等待产出", kind="phase")
        log.info(f"需求 #{req_id} 角色 {role} 为外部角色，停卡等待外部主 Agent")
        return
    if not model_override:
        # C3：本卡该角色处于评审升级期 → 本轮用升级模型（评审打回 / 人工 n 打回 / 失败重试共用此入口）
        model_override = review_escalation_override(req_id, role)
    # #419：进入 doing 不再写 phase（phase 为历史只读列；推进按卡上流程快照 node_index）
    set_req(req["id"], {"status": "doing"})
    _note_review_model(req, role)
    prompt = build_prompt(req, role, extra)
    log.info(f"需求 #{req_id} [{project}] 角色={role} 开始{'（续跑）' if resume else ''}: {req['title']}")
    resume_sid = find_resume_sid(req, role) if resume else None
    if resume_sid:
        # 续跑：拉起时旧 id 已知，当场写入 spawn 记录
        post_dispatcher_event(req_id, f"拉起「{ROLE_NAMES.get(role, role)}」会话（续跑）",
                              kind="spawn", session_id=resume_sid)
        spawn_event_id = None
    else:
        # 新会话：先写记录拿 id，会话建立后回填
        spawn_event_id = post_dispatcher_event(req_id,
                                               f"拉起「{ROLE_NAMES.get(role, role)}」会话（新会话）",
                                               kind="spawn")
    # 非兜底路径逐字保持原调用形态（不给存量 stub 引入新 kwarg）
    thread_kw = {"kwargs": {"model_override": model_override}} if model_override else {}
    threading.Thread(target=run_agent,
                     args=(req, role, prompt, time.time(), extra, resume, spawn_event_id),
                     daemon=True, **thread_kw).start()


def result_state(req):
    """回合结束时的结果文件状态：'ok'/'missing'/'invalid'（detail 供事件与续接文案）。
    路径检查与 handle_result 一致：产物目录 result.json，兼容旧约定工作目录根 .req-board-result.json。"""
    result_file = result_file_path(req)
    if not result_file.exists():
        legacy = workdir_for(req["project"]) / RESULT_FILENAME
        if legacy.exists():
            result_file = legacy
    if not result_file.exists():
        return "missing", "result.json 不存在"
    try:
        json.loads(result_file.read_text())
        return "ok", ""
    except Exception as e:
        return "invalid", f"result.json 解析失败（{e}）"


# #327：存量 resume 会话保留旧系统提示词（不含输出风格新规），续接文案补一行风格提醒
RESUME_STYLE_REMINDER = (
    "输出风格（新规范，本 resume 会话的系统提示词可能未含）：回复结论先行、能短则短"
    "（单条一般两三百字内），禁止开场白/客套/复述需求；产物文档删除过程叙述、每要点一行；"
    "result.json summary 要点化、一般不超过 10 行。")


def _artifact_ls(req):
    """#388 产物目录现状清单（续接轮注入，一眼可见缺什么）：
    返回 (文件清单, 目录路径文本)；目录不存在/不可读 → (空清单, "（不可读）")，降级不崩。"""
    try:
        art = artifact_dir(req)
        if art.is_dir():
            return [p.name for p in sorted(art.iterdir())], str(art)
        return [], str(art)
    except Exception:
        return [], "（不可读）"


def build_followup_prompt(role, state, req):
    """无结果续接文案（#273 护栏）：dev/accept 强调终止后台任务 + 前台验证；
    只读角色只需补写 result.json（审批路由仍强制只读，不放松约束）。
    #388：注入产物目录现状清单；强化「写入失败必须重写、不许乐观声明」。"""
    reason = ("result.json 存在但 JSON 解析失败" if state == "invalid"
              else "result.json 不存在")
    ls, art_txt = _artifact_ls(req)
    art_hint = f"产物目录现状（{art_txt}）：" + ("、".join(ls) if ls else "（空）")
    strict = (f"{art_hint}\n"
              "写 result.json 若失败（报错/回读校验不通过），必须重写后再回读校验，"
              "禁止声称已写入但实际未校验。")
    if role_capability(role, "readonly"):
        return (f"你结束了回合，但结果文件未提交（{reason}）。"
                "请立即用 Write/fs 工具把 result.json 写入产物目录"
                "（status/summary 必填；前置环节角色另带 suggested_flow: 推荐流程 id 与 repos: 涉及仓库数组），"
                "写后回读校验 JSON 可解析。禁止写入产物目录以外的文件。写完后结束回合。\n"
                f"{strict}")
    return (f"你结束了回合，但结果文件未提交（{reason}）。"
            "注意：ACP 没有异步通知，回合结束即会话结束，后台任务会随进程丢失。"
            "请先终止仍在运行或等待中的后台任务；必要验证改为前台分块执行并等待完成；"
            "然后用脚本或 python json.dumps 写 result.json（status/summary 必填），"
            "写后回读校验 JSON 可解析。写完后结束回合。\n"
            f"{strict}")


def _close_held(h):
    """关闭保持进程（优雅 terminate + 关闭日志；进程组兜底由 ACPSession.close 内部处理）"""
    try:
        h["session"]._flush_message()
        h["session"].close(kill=False)
    except Exception:
        pass
    try:
        h["logf"].close()
    except Exception:
        pass


def _hold_session(asid, req_id, role, session, logf):
    """一轮正常结束后把进程放入保持池；pid 镜像到 state（调度器重启后据此清理孤儿）"""
    with held_lock:
        held_sessions[asid] = {"session": session, "logf": logf,
                               "req_id": str(req_id), "role": role, "kept_at": time.time()}
        state.setdefault("held_procs", {})[asid] = session.proc.pid
    save_state()
    log.info(f"需求 #{req_id} 会话进程保持 (asid={asid}, pid={session.proc.pid})")


def _steps_text(role, req):
    """提示词里的步数上限文案（C2）：有角色预算给具体数字，否则沿用「约 200 步」（kimi 自身每轮上限）。"""
    n = (role_budget(role, project=req.get("project")) or {}).get("max_steps")
    return str(n) if n else "约 200"


def _budget_wrapup(session, req, role):
    """C2：守卫中止后的收束。步数预算截停 → 同会话给一轮限步收尾写 result.json：写成 → 返回 False
    （按正常回合收尾，交 handle_result 采信结果）；未写成 → 返回结构化原因串（truthy，走 aborted 失败
    通道，归因 `budget_exceeded: steps`）。其它守卫（复读/超长单请求）→ 返回 True（行为不变）。"""
    tripped = (getattr(session, "_guard", None) or {}).get("tripped") or {}
    if tripped.get("kind") != "budget_steps":
        return True
    detail = tripped.get("detail") or "本轮工具调用达上限"
    post_event(req["id"], "system", f"预算截停（{detail}），给一轮限步收尾写结果",
               session_id=getattr(session, "session_id", None))
    session.step_budget = BUDGET_WRAPUP_STEPS
    wrapup = (f"已达到本轮工具步数上限（{detail}），会话被调度器截停。立即停止新的探索与修改，"
              f"用不超过 {BUDGET_WRAPUP_STEPS - 5} 步把当前进度写入 result.json：已完成且验证通过 → done；"
              "否则 blocked，summary 写清已完成部分、未完成部分与下一步。写完即结束本轮。")
    try:
        session.prompt(wrapup, timeout=BUDGET_WRAPUP_SEC)
    except Exception as e:
        log.warning(f"需求 #{req['id']} 预算收尾轮异常: {e}")
    state_, _detail = result_state(req)
    if state_ == "ok":
        post_event(req["id"], "system", "预算截停后收尾轮已提交结果",
                   session_id=getattr(session, "session_id", None))
        return False
    return f"budget_exceeded: steps（{detail}），收尾轮未提交结果"


def run_agent(req, role, prompt, started_at, extra="", resume=False, spawn_event_id=None,
              agent_session_id=None, user_text=None, resume_session_id=None,
              hold=False, held=None, model_override=None):
    """ACP 会话主流程：启动 kimi acp -> 握手 -> prompt -> 收束结果。
    #284 进程保持：hold=True 且本轮正常结束 → 进程放保持池（下条消息复用，省拉起+resume）；
    held 非空 → 复用保持池进程（跳过 Popen/start/resume 直接 prompt），正常结束仍放回保持池。
    超时/异常路径照旧杀进程不保持；自动开发（spawn_agent）不传 hold，行为与现状一致。"""
    # #279：最底层会话启动器同样校验角色（防止绕过 spawn_agent 直接调用，如 dev agent
    # import dispatcher 拉 accept 会话）；拦截返回 False，正常完成返回 True（调用方区分）
    if not role_allowed(req, role):
        log.error(f"需求 #{req['id']} 拒绝运行角色 {role}（mode={req.get('mode')}, phase={req.get('phase')}）："
                  f"角色与需求当前阶段不匹配，疑似绕过调度器直接调用")
        try:
            post_event(req["id"], "system",
                       f"拒绝拉起非法角色 {role}（mode={req.get('mode')}, phase={req.get('phase')}），会话未启动")
        except Exception:
            pass
        return False
    req_id = str(req["id"])
    # analysis 起全部角色在 per-requirement worktree 工作（build_prompt 已创建，基线=主干）
    workdir = dev_workdir_for(req)
    timed_out = False
    aborted = False   # #28 输出守卫中止标记（复读/单次请求超长）
    returncode = 1
    # 活跃时长起点（2026-09-17 修复）：续接预算用 monotonic 计算——started_at 是 wall clock，
    # 系统睡眠会被算进「已用预算」，唤醒后误判预算耗尽而放弃续接
    started_mono = time.monotonic()
    row_id = agent_session_id
    # #421 会话活性心跳持有人（每轮新建）：ACPSession 状态切换（llm 请求/思考/响应、工具起止）
    # 时按注册表行 id 落时间戳，右栏据此判活/判死。复用保持进程的轮次必须**重绑** sink
    # （会话可能还挂着上一轮的旧 holder → 心跳会写到旧行或永远失聪）
    activity = {"row_id": agent_session_id, "warned": False}

    def _activity_sink(kind, detail=None):
        _touch_session_activity(req["id"], role, activity, kind, detail)

    # #28 输出守卫触发留痕（三落点：agent_events 时间线 / dispatcher_events 调度历史 /
    # summary 单行事实句）。守卫在会话线程/读线程内触发，故各自 try/except 不抛，避免反向打断收束。
    def _output_guard_sink(kind, detail):
        label = "预算截停" if str(kind).startswith("budget") else "输出守卫"   # C2 预算复用守卫通道
        try:
            post_event(req["id"], "system", f"{label}中止（{kind}）：{detail}",
                       session_id=getattr(session, "session_id", None))
        except Exception:
            pass
        try:
            post_dispatcher_event(req["id"], f"本轮已中止（{label}：{kind}）—— {detail}",
                                  kind="action")
        except Exception:
            pass
        try:
            append_summary(req["id"], f"本轮{label}中止（{kind}）：{detail}")
        except Exception:
            pass

    usage = None  # collect_usage 三值 {context_tokens, max_tokens, context_pct}，超时/异常路径为 None
    usage_ext = None  # #42 缓存命中三态 {state, cached_tokens, input_tokens, raw}，见 cache_summary()
    logf = held["logf"] if held is not None else open(f"/tmp/req_board_agent_{req_id}.log", "a")
    kept = held is not None
    session = None
    arun = None  # #388：本线程登记的 active_runs 对象（finally 对象匹配删除，防误删新会话登记）
    try:
        if kept:
            # #284 复用保持进程：跳过 Popen/session.start，直接进入 prompt 流程
            session = held["session"]
            # #421 显式重绑活性心跳（池复用的会话对象可能还挂着上一轮的旧 holder）
            session.activity_sink = _activity_sink
            # #28 显式重绑输出守卫（防沿用上一轮旧闭包/旧计数/旧 tripped 状态）
            session.rearm_output_guard(_output_guard_sink)
            # #388：active_runs 补充 session/started_at（运行期兜底 sweep 补收尾时
            # 运行记录与 handle_result 的起始时间/会话信息来源）；arun 保留对象引用
            # 供 finally 对象匹配删除（防误删 sweep/失败通道新登记的新会话）
            arun = {"proc": session.proc, "project": req["project"],
                    "role": role, "owner": owner_key(req),
                    "session": session, "started_at": started_at}
            with active_lock:
                active_runs[req_id] = arun
            log.info(f"需求 #{req_id} 复用保持进程 (pid={session.proc.pid}, role={role})")
            resumed = True
        else:
            # #423：只读评审角色（python_safe 能力键）放开受限 python 自验——python_roots 取完整
            # readonly_tool_roots（含项目各真实仓库根：形态 B 的 `<root>/.venv/bin/python` 精确
            # 匹配所需，worktree 内无 .venv；形态 A 的 py_compile 落点另收窄，见下）
            py_safe = role_capability(role, "python_safe")
            # #11 spawn 前静态校验 + 降级链：角色配置值不在别名池 → 直接按兜底链回落（不阻断）。
            # 运行期兜底轮（model_override 非空）本轮改用全局兜底模型（不写配置文件、不产生永久降级）
            resolved = _apply_model_override(resolve_role_model(role, project=req["project"]),
                                             model_override)
            session = ACPSession(req["id"], workdir, logf, role, artifact_dir(req), project=req["project"],
                                 kimi_bin=str(REPO_DIR / "scripts" / "kimi-chat-acp"),
                                 event_sink=post_event, readonly=role_capability(role, "readonly"),
                                 readonly_roots=readonly_tool_roots(req["project"], workdir, artifact_dir(req)),
                                 write_roots=dev_write_roots(req),
                                 main_ws_roots=PROJECTS.get(req["project"], {}).get("paths"),
                                 # #379 dev/accept（bash=allow/no_commit）注入 Playwright 浏览器工具前缀，
                                 # 审批按 _auto_approve 双清单校验（fail-closed）；只读角色不注入、行为零变化
                                 extra_allow_prefixes=(
                                     ([TAPD_TOOL_PREFIX] if role_capability(role, "tapd_readonly") else [])
                                     + ([PLAYWRIGHT_TOOL_PREFIX]
                                        if role_capability(role, "bash") in ("allow", "no_commit")
                                        else [])),
                                 # #396：merge_resolve 注入需求助手合并授权（#355）子集——
                                 # git_cmd_safe(allow_merge=True) 受限形态 + worktree 内写
                                 allow_git_commit=role_capability(role, "bash") == "git_merge",
                                 allow_git_merge=role_capability(role, "bash") == "git_merge",
                                 # #423：py_compile 会在目标旁写 __pycache__，落点收窄到 worktree/产物目录
                                 allow_python_safe=py_safe,
                                 python_roots=(readonly_tool_roots(req["project"], workdir, artifact_dir(req))
                                               if py_safe else None),
                                 python_compile_roots=([workdir, artifact_dir(req)] if py_safe else None),
                                 model=resolved["model"] or None,
                                 # #421 会话活性心跳（状态切换时才写，非定时轮询）
                                 activity_sink=_activity_sink,
                                 # #28 输出守卫留痕回调（复读/单次请求超长触发时）
                                 output_guard=_output_guard_sink)
            # #11 请求值/来源落在会话对象上（update_session_info / sync_agent_session 落库用）
            session.model_requested = resolved["model"]
            session.model_source = resolved["source"]
            # #13 配置值与降级原因同落会话对象（透传给 update_session_info / sync_agent_session）；
            # 未降级时 reason 不写（保持「无异常不写该列」）
            session.model_configured = resolved["configured"]
            if resolved["degraded"]:
                session.model_reason = resolved["reason"]
            _note_model_fallback(req_id, role, source=resolved["source"],
                                 requested=resolved["model"], configured=resolved["configured"],
                                 add_role=resolved["degraded"],
                                 fact=(f"{role} 请求 {resolved['configured']} → 实际 "
                                       f"{resolved['model']}（{resolved['reason']}）"
                                       if resolved["degraded"] else None))
            # #388：active_runs 补充 session/started_at（运行期兜底 sweep 补收尾时
            # 运行记录与 handle_result 的起始时间/会话信息来源）
            arun = {"proc": session.proc, "project": req["project"],
                    "role": role, "owner": owner_key(req),
                    "session": session, "started_at": started_at}
            with active_lock:
                active_runs[req_id] = arun
            log.info(f"需求 #{req_id} ACP 进程启动 (pid={session.proc.pid}, role={role})")
            resume_sid = (resume_session_id if resume_session_id is not None
                          else (find_resume_sid(req, role) if resume else None))
            resumed = session.start(workdir, resume_session_id=resume_sid)
            # #11 落定回读留痕（仅非 held 复用轮）：降级 / 未落定 → 记录 + 时间线 kind="model"
            settle = getattr(session, "model_settle", None)
            if settle and settle != "settled":
                # #13：未落定原因（settle + harness 原始报错摘要 ≤200）落 model_reason
                session.model_reason = f"{settle}：{getattr(session, 'model_error', None) or '未落定'}"
                _note_model_fallback(
                    req_id, role, add_role=True,
                    fact=(f"{role} 请求 {resolved['model']} → 回读 "
                          f"{getattr(session, 'model_effective', None) or resolved['model']}"
                          f"（{settle}）"))
            if not kept:
                post_model_events(req, role, session, resolved)
        ver = kimi_version()
        update_session_info(req, key=session_key(req, role), id=session.session_id,
                            model=session.model_name, thinking=session.thinking,
                            model_requested=getattr(session, "model_requested", None),
                            model_source=getattr(session, "model_source", None),
                            model_configured=getattr(session, "model_configured", None),
                            model_reason=getattr(session, "model_reason", None),
                            harness=f"kimi code {ver}" if ver else None)
        # #273 人工接管：同步 agent_sessions 注册表（失败仅降级，不阻断会话）
        row_id = sync_agent_session(req, role, session_id=session.session_id, row_id=row_id,
                                    model=session.model_name, thinking=session.thinking,
                                    model_requested=getattr(session, "model_requested", None),
                                    model_source=getattr(session, "model_source", None),
                                    model_configured=getattr(session, "model_configured", None),
                                    model_reason=getattr(session, "model_reason", None),
                                    harness=f"kimi code {ver}" if ver else None,
                                    workdir=str(workdir), status="running")
        # #421：同步失败返回 None 时保留旧 id（心跳继续写已知行，不因一次抖动失聪）
        activity["row_id"] = row_id or activity["row_id"]
        if spawn_event_id and session.session_id:
            # 回填 spawn 记录 session_id（#120）：失败仅降级，不阻断会话流程
            backfill_spawn_session(req_id, spawn_event_id, session.session_id)
        if kept:
            post_event(req["id"], "system", f"agent 复用保持进程（{role}），继续执行",
                       session_id=session.session_id)
        else:
            post_event(req["id"], "system", f"agent 启动（{role}），ACP 会话已建立", session_id=session.session_id)
        post_event(req["id"], "system", "会话已恢复，继续执行" if resumed else "会话已建立，开始执行",
                   session_id=session.session_id)
        if user_text:
            post_event(req["id"], "user", user_text, session_id=session.session_id)
        actual_prompt = prompt
        if user_text is not None and not resumed:
            actual_prompt = prompt + f"\n\n用户消息：{user_text}"
        elif user_text is not None:
            actual_prompt = (f"继续之前看板需求 #{req['id']} 的 {role} 工作（角色 {role}）。\n"
                             f"用户消息：{user_text}\n{extra}\n"
                             f"{RESUME_STYLE_REMINDER}\n"
                             "提醒：结束前必须把结果 JSON 写入产物目录 result.json"
                             "（status: done/blocked/need_confirm，契约不变）。\n"
                             "工具调用步数有每轮上限（约 200 步），接近上限时先把结果 JSON 写入 "
                             "result.json 保底（契约不变，可覆盖为最终结果），再继续剩余工作，"
                             "避免会话被截断时无结果可采。")
        elif resumed:
            actual_prompt = (f"继续之前看板需求 #{req['id']} 的开发工作（角色 {role}）。\n{extra}\n"
                             f"{RESUME_STYLE_REMINDER}\n"
                             "若上次会话中途结束：先确认改动完好（git status 等）、产物目录无残留 result.json，"
                             "再继续验证。\n"
                             "提醒：结束前必须把结果 JSON 写入产物目录 result.json"
                             "（status: done/blocked/need_confirm，契约不变）。\n"
                             f"工具调用步数有每轮上限（{_steps_text(role, req)} 步），接近上限时先把结果 JSON 写入 "
                             "result.json 保底（契约不变，可覆盖为最终结果），再继续剩余工作，"
                             "避免会话被截断时无结果可采。")
        # #108：进程安全红线统一注入（新会话与续跑两条路径一次覆盖，注入点唯一）
        actual_prompt = append_kill_redline(actual_prompt, session.proc.pid)
        if not hold and not kept:
            # C2：自动开发会话按角色预算限步（人工续跑 hold/复用轮不限，由人掌控）
            session.step_budget = (role_budget(role, project=req.get("project")) or {}).get("max_steps")
        try:
            # #396：merge_resolve 单轮时间盒 15 分钟（prompt 内 {timeout_minutes} 同步覆盖）
            budget_min = effective_timeout_minutes(req, role)
            session.prompt(actual_prompt, timeout=budget_min * 60)
            returncode = 0
            # #273 护栏：回合结束但结果文件未提交（不存在/解析失败）时，同一会话续接一轮，
            # 让 agent 补写/重写 result.json；上限 1 次，剩余预算不足 1 分钟不续接（避免误判超时）。
            state, detail = result_state(req)
            if state != "ok":
                remaining = budget_min * 60 - (time.monotonic() - started_mono)
                if remaining >= 60:
                    post_event(req["id"], "system",
                               f"回合结束但未提交结果（{detail}），续接一轮（剩余 {int(remaining // 60)} 分钟）",
                               session_id=session.session_id)
                    follow_prompt = build_followup_prompt(role, state, req)
                    try:
                        session.prompt(follow_prompt, timeout=remaining)
                    except TurnAborted as e:
                        # #28 输出守卫在续接轮触发：须先于 except Exception 命中（TurnAborted 属 Exception
                        # 子类），否则被吞成「续接异常」丢失中止语义
                        log.warning(f"需求 #{req_id} 续接轮输出守卫中止: {e}")
                        post_event(req["id"], "system", f"续接轮输出守卫中止：{e}，取消会话",
                                   session_id=session.session_id)
                        session.cancel()
                        time.sleep(3)
                        aborted = _budget_wrapup(session, req, role)
                    except TimeoutError:
                        timed_out = True
                        post_event(req["id"], "system", "续接超时，取消会话",
                                   session_id=session.session_id)
                        session.cancel()
                        time.sleep(3)
                    except Exception as e:
                        log.warning(f"需求 #{req_id} ACP 续接异常: {e}")
                        post_event(req["id"], "system", f"续接异常: {e}",
                                   session_id=getattr(session, "session_id", None))
            if not timed_out and not aborted:
                usage = session.collect_usage()
                if usage:
                    # sessions 状态文件继续只存百分比（展示不破坏）；token 数走 agent_runs 落库
                    update_session_info(req, key=session_key(req, role), context=usage["context_pct"])
        except TurnAborted as e:
            # #28 输出守卫在首轮 prompt 触发：走超时同款收束（取消会话、不采集 usage、进失败重试通道）
            log.warning(f"需求 #{req_id} 输出守卫中止会话: {e}")
            post_event(req["id"], "system", f"本轮已中止（输出守卫）：{e}，取消会话",
                       session_id=session.session_id)
            session.cancel()
            time.sleep(3)
            aborted = _budget_wrapup(session, req, role)
            if not aborted:
                returncode = 0      # C2 收尾轮已提交结果：按正常回合收尾
        except TimeoutError:
            timed_out = True
            post_event(req["id"], "system", "运行超时，取消会话", session_id=session.session_id)
            session.cancel()
            time.sleep(3)
    except Exception as e:
        log.warning(f"需求 #{req_id} ACP 运行异常: {e}")
        post_event(req["id"], "system", f"运行异常: {e}",
                   session_id=getattr(session, "session_id", None))
    finally:
        # #284 进程保持：正常结束（未超时/未异常）且 hold（人工续跑）→ 进程放保持池供下条消息复用；
        # 复用轮（kept）正常结束同样放回；超时/异常路径照旧杀进程不保持
        keep = (session is not None and not timed_out and not aborted and returncode == 0
                and (hold or kept))
        if session:
            session._flush_message()
            if keep:
                try:
                    _hold_session(agent_session_id or row_id, req_id, role, session, logf)
                except Exception as e:
                    log.warning(f"需求 #{req_id} 保持进程失败: {e}")
                    session.close(kill=False)
                    logf.close()
            else:
                session.close(kill=(timed_out or aborted))
                if returncode != 0 and session.proc.returncode:
                    returncode = session.proc.returncode
                logf.close()
        with active_lock:
            # #388 对象匹配删除：仅当登记仍指向本线程会话时删除——sweep 兜底/失败通道
            # 可能已摘除旧记录或登记新会话，无条件 pop 会把新登记的活跃会话顶掉
            # （审查问题 2：顶掉后 #380 自动重启检查与运行期 sweep 均对重试会话失明）
            if active_runs.get(req_id) is arun:
                active_runs.pop(req_id, None)
        try:
            # 会话信息栏分支名：统一记录需求 dev 分支（全角色同一 worktree），
            # 不再读主工作区当前分支（主工作区 checkout 状态不影响调研/开发基线）
            branch = dev_branch(req)
            update_session_info(req, key=session_key(req, role), branch=branch,
                                workdir=str(workdir))
            # #421：收尾统一走 _close_session_row（置 idle + 清 turn_step，防右栏残留「工具中」档）
            _close_session_row(req, role, row_id, "idle",
                               session_id=getattr(session, "session_id", None),
                               branch=branch, context_pct=(usage or {}).get("context_pct"))
        except Exception:
            pass
        # #388 防重：主循环兜底（进程死场景）已补过收尾（行已 idle、run 记录已落、
        # handle_result 已执行）→ 跳过重复 post_run_record（避免失败通道二次重试/重复报表行）
        if _swept(row_id):
            log.info(f"需求 #{req_id} 会话收尾已由兜底完成（row={row_id}），跳过重复运行记录")
            return True
        # 统一收尾：运行记录（起止时间/状态/工具数/token/百分比）+ 明确的结束事件，
        # 报表不再靠最后一条事件猜结束时间；失败仅降级不阻断 handle_result。
        # #42：缓存命中三态在收尾时统一采集——超时/中止前已完成过请求的会话照记 hit/unknown，
        # 只看缓存采集自身（_llm_turns_done + 已取得字段），与峰值 usage 是否 None 无关
        try:
            usage_ext = session.cache_summary() if session is not None else None
        except Exception as e:
            log.warning(f"需求 #{req_id} 缓存命中汇总失败（不阻断收尾）: {e}")
            usage_ext = None
        post_run_record(req, role, session, started_at, time.time(), returncode, timed_out, usage,
                        aborted=aborted, usage_ext=usage_ext)
    if _swept(row_id):
        log.info(f"需求 #{req_id} 结果处理已由兜底完成（row={row_id}），跳过重复 handle_result")
        return True
    # #11 本轮降级角色事实汇总写入 summary（无论成败，事实先行；无降级时零行为）
    try:
        _flush_model_degraded_summary(req, role)
    except Exception as e:
        log.warning(f"需求 #{req_id} 模型降级汇总写入失败（不阻断收尾）: {e}")
    try:
        handle_result(req, role, returncode, timed_out, started_at, aborted=aborted)
    except Exception:
        # #412（B）：handle_result 异常必留痕——旧实现无 try/except，异常静默丢失且不留 traceback
        log.exception(f"需求 #{req_id} handle_result 异常（role={role}，returncode={returncode}，"
                      f"timed_out={timed_out}）")
    return True

REVIEW_DIFF_MAX_LINES = 2000  # #337：代码审查单仓库 diff 行数上限（截断如实标注，reviewer 可用只读工具补看）

def _gen_review_diff(req):
    """#337：为合并前只读审查生成 dev 分支相对基线的三点 diff 全文，调度器代写产物目录 diff.patch。
    只读 MCP git_diff 白名单仅 stat 类（acp_shared.GIT_DIFF_ALLOWED_ARGS），reviewer 自己拿不到
    范围 diff 内容（reviewer 的 Bash 只读白名单不含 git 内容 diff），故由调度器在拉起 review 前生成；单仓库超 REVIEW_DIFF_MAX_LINES
    截断并如实标注（reviewer 可用 git_diff --stat 定位后 Read 具体文件补看）。在合并前调用，diff 天然有效。"""
    branch = dev_branch(req)
    parts = []
    for p in PROJECTS[req["project"]]["paths"]:
        try:
            base = _branch_base(req, p)
            if not base:
                continue
            r = subprocess.run(["git", "diff", f"{base}...{branch}"], cwd=p,
                               capture_output=True, text=True, timeout=DIAG_TIMEOUT)
            if r.returncode != 0 or not r.stdout.strip():
                continue
            lines = r.stdout.splitlines()
            header = f"===== 仓库 {Path(p).name}（git diff {base}...{branch}）====="
            if len(lines) > REVIEW_DIFF_MAX_LINES:
                header += (f"\n（diff 过长已截断：共 {len(lines)} 行，仅显示前 {REVIEW_DIFF_MAX_LINES} 行；"
                           "可用 git_diff --stat 定位改动文件后 Read 补看）")
                lines = lines[:REVIEW_DIFF_MAX_LINES]
            parts.append(header + "\n" + "\n".join(lines))
        except Exception as e:
            log.warning(f"需求 #{req['id']} 审查 diff 生成失败（{p}）: {e}")
    # #383：无实际 diff（parts 为空）时不写 diff.patch——语义收敛为「文件在=有内容」，前端按
    # exists 渲染分支差异产物行、无内容时不显示；存量「（无 diff 内容）」占位文件不回改
    if parts:
        try:
            art = artifact_dir(req)
            art.mkdir(parents=True, exist_ok=True)
            (art / "diff.patch").write_text("\n\n".join(parts))
        except Exception as e:
            log.warning(f"需求 #{req['id']} 写 diff.patch 失败: {e}")

def changed_repos(req, branch):
    """需求 dev 分支相对各仓库基线（主干起点，见 _branch_base）有实际文件变更的仓库名集合。
    单仓库返回 {basename}；分支不存在（diff 非零）/基线无法确定的仓库忽略。"""
    changed = set()
    for p in PROJECTS[req["project"]]["paths"]:
        try:
            base = _branch_base(req, p)
            if not base:
                continue
            r = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=p,
                               capture_output=True, text=True, timeout=DIAG_TIMEOUT)
            if r.returncode == 0 and any(f.strip() for f in r.stdout.splitlines()):
                changed.add(Path(p).name)
        except Exception as e:
            log.warning(f"需求 #{req['id']} dev 分支变更检测失败（{p}）: {e}")
    return changed

def has_code_changes(req, since):
    """dev done 校验（#113 修正）：需求独立分支相对基线（当前分支 HEAD 三点 diff）有实际文件变更
    即认可 done（新会话接管旧 worktree 时已有未合并提交也算改动）；无任何变更仍拒绝 done；
    多仓库任一仓库有变更即认可。分支不存在（diff 非零）→ False。
    since 参数保留（调用方兼容），实现不再使用——语义从时间窗口改为分支 diff（#113 第二轮 dev
    因只看本次 run 后的提交被判「无代码变更」的修复）。"""
    return bool(changed_repos(req, dev_branch(req)))

def artifact_ok(req, role):
    """节点 done 契约（artifact/review 类）：对应产物文件存在且非空。产物名取自配置派生门面
    role_capability(role, "artifact")（唯一源 flows.json roles.<id>.artifact，#419）。
    前置槽位契约（suggested_flow/repos）不在产物存在性上拦截：缺推荐由 _pre_stage_done 兜底
    默认流程 + 留痕，repos 清单外转人工确认。"""
    art = artifact_dir(req)
    expected = role_capability(role, "artifact")
    if not expected:
        return True, ""
    f = art / expected
    if not f.exists() or not f.read_text(errors="replace").strip():
        return False, f"缺少产物 {expected}"
    return True, ""

def _extract_todos(result):
    """从 result.json 提取可选字段 todos（#279）：字符串数组，agent 声明的遗留人工操作，纯展示。
    兼容旧格式宁缺勿滥：非 dict / 缺失 / 非列表 → None（视为未声明，不报错）；
    元素过滤为去空白非空字符串，非法元素忽略；原始列表为空 → 返回 []（显式声明无待办）；
    有元素但全部非法 → None（视为未声明，不覆盖已声明的值）。"""
    if not isinstance(result, dict):
        return None
    todos = result.get("todos")
    if not isinstance(todos, list):
        return None
    cleaned = [t.strip() for t in todos if isinstance(t, str) and t.strip()]
    if not cleaned and todos:
        return None
    return cleaned

# #400：追问护栏阈值（单调计数，无任何清零/复位点——响应不清零语义下计数恒为 1、上限不可达，已废弃）
CONFIRM_ROUNDS_MAX = 5

# #400：网页确认动作白名单（按 kind 固定生成按钮，agent 无法声明）；其余 kind 首期不走本通道
# #411：review_gate 三档——pass 仍合并 / rework 转开发（新）/ reject 转暂缓（原「否决合并」改名）
WEB_CONFIRM_ACTIONS = {
    "review_gate": ("pass", "rework", "reject"),
    "confirm_override": ("pass", "reject", "reject_later"),
    # 评审轮次超限的人工裁决与飞书 y/n 对齐；普通提问的 pass/rework 由 API 层拒绝。
    "confirm": ("pass", "rework", "proceed", "stop_later"),
}


def _parse_confirm_options(result):
    """#400：need_confirm result.json 可选 options（纯对话类选项，fail-closed）——
    仅接受 label/hint 文本，未知键（含 action 动作码）自然丢弃；非法/超限置空不阻断落表。
    返回 None 表示未声明（页面退化 question 全文 + 回复入口）。"""
    raw = (result or {}).get("options")
    if not isinstance(raw, list):
        return None
    options = []
    for item in raw:
        if len(options) >= 5:
            break
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")[:20].strip()
        if not label:
            continue
        hint = str(item.get("hint") or "")[:50].strip()
        options.append({"label": label, "hint": hint} if hint else {"label": label})
    return options or None


def _register_need_confirm(req_id, entry, role):
    """#400：need_confirm 统一落表 + 追问护栏（confirm_rounds 单调计数，无清零点）。
    - 每次落表 +1；N ≤ CONFIRM_ROUNDS_MAX 正常（通知带「第 N 轮待确认」）
    - N > MAX 仍落表 + entry["over_limit"]=True，不自动转 later（明示出口：飞书 y/n、
      网页「停止追问转暂缓」按钮）；护栏只影响落表标记与文案，不拦截任何用户通道。
    返回落表轮次 N（调用方构造通知文案）。"""
    req_id = str(req_id)
    with state_lock:
        rounds = int(state.setdefault("confirm_rounds", {}).get(req_id, 0)) + 1
        state["confirm_rounds"][req_id] = rounds
        entry["role"] = role
        if rounds > CONFIRM_ROUNDS_MAX:
            entry["over_limit"] = True
        # #27：唯一落表出口（锁内纯内存：裁剪 + 写入 + 回读校验），留痕在出锁后
        ok = _put_pending_confirm(req_id, entry)
    if not ok:
        _pending_source_mismatch_note(req_id, entry, "need_confirm 落表")
    save_state()
    return rounds


def _notify_need_confirm(req, question, role, rounds):
    """#400：need_confirm 通知（问责护栏轮次标注），卡片/降级文案统一出口"""
    notify_req_card("need_confirm", req, question=question, phase=role,
                    round_n=rounds, over_limit=rounds > CONFIRM_ROUNDS_MAX)


# ==================== #419 流程推进（节点完成 → 下一节点 / 收尾） ====================
def _write_flow_snapshot(req, flow_id, index=0):
    """写卡流程快照（flow/flow_name/nodes_json/node_index）并**写后回读校验**（§1.4/窗口 B）：
    未生效（旧看板对未知字段静默忽略）→ 事件留痕 + 返回 None，调用方暂停该卡推进
    （不重复拉起前置环节，等看板升级/人工处理）。成功返回刷新后的卡。"""
    f = flow_by_id(flow_id)
    if not f:
        return None
    payload = {"flow": flow_id, "flow_name": f["name"],
               "nodes_json": json.dumps(f["nodes"], ensure_ascii=False), "node_index": index}
    set_req(req["id"], payload)
    fresh = fetch_req(req["id"])
    ok = False
    if fresh:
        try:
            ok = ((fresh.get("nodes_json") or "") == payload["nodes_json"]
                  and (fresh.get("flow") or "") == flow_id
                  and int(fresh.get("node_index")) == index)
        except (TypeError, ValueError):
            ok = False
    if not ok:
        log.error(f"需求 #{req['id']} 流程快照写入未生效（看板未升级/写入被忽略），暂停推进")
        post_dispatcher_event(int(req["id"]),
                              "流程快照写入未生效（看板可能未升级），暂停该卡推进（不重复拉起前置环节）",
                              kind="phase")
        return None
    return fresh


def _adopt_flow_and_spawn(req, flow_id, extra=""):
    """采纳流程（前置环节产出或人工确认的结果）：写快照 index=0 → 拉起首节点（terminal 首节点直接收尾）"""
    req_id = str(req["id"])
    f = flow_by_id(flow_id)
    if not f:
        log.error(f"需求 #{req_id} 采纳流程失败：配置中无 flow {flow_id!r}")
        return False
    fresh = _write_flow_snapshot(req, flow_id, 0)
    if fresh is None:
        return False
    first = f["nodes"][0]
    post_dispatcher_event(int(req_id),
                          f"采纳流程「{f['name']}」（{flow_id}），进入首节点「{node_display(first)}」",
                          kind="phase")
    append_summary(req_id, f"采用流程 {f['name']}（{flow_id}）。")
    if (first.get("node_type") or "") == "terminal":
        _run_terminal(fresh)
    else:
        _spawn_safe(fresh, first.get("role"), extra=extra)
    return True


def _pre_stage_done(req, result, todos):
    """前置槽位完成（§1.4）：repos 结论入库 → 推荐流程（缺失/非法 → default_flow + 留痕）→
    免确认项目直通采纳；清单外仓库 / confirm 项目 → need_confirm 确认推荐流程（y 采纳、n 转人工，
    即原 route 两条人工把关的等价迁移）。"""
    req_id = str(req["id"])
    if todos is not None:
        with state_lock:
            state.setdefault("req_todos", {})[req_id] = todos
        save_state()
    raw_repos = (result or {}).get("repos") or []
    repos = validate_repos(req["project"], raw_repos)
    with state_lock:
        state.setdefault("req_repos", {})[req_id] = repos
    save_state()
    suggested = (result or {}).get("suggested_flow")
    fid = suggested if (flow_by_id(suggested) and flow_enabled(suggested)) else None
    source = "模型推荐"
    if fid is None:
        fid = default_flow_id() or ((enabled_flows() or [{}])[0].get("id") or "standard")
        source = "兜底默认"
        f = flow_by_id(fid)
        # 推荐了被软禁用的流程 → 与非法 id 同路径静默兜底，但留痕文案区分「已禁用 / 不存在」
        why = (f"推荐流程 {suggested!r} 已禁用（enabled: false）" if suggested and flow_by_id(suggested)
               else f"前置环节未产出有效流程推荐（{suggested!r}）")
        post_dispatcher_event(int(req_id),
                              f"{why}，按默认流程「{f['name'] if f else fid}」推进",
                              kind="flow_fallback")
        append_summary(req_id, f"{why}，兜底默认流程 {fid}。")
    repos_hint = f"，识别涉及仓库 {len(repos)} 个" if repos else "（未识别涉及仓库）"
    pre_name = role_meta(pre_role(), "display") or pre_role()
    post_dispatcher_event(int(req_id), f"「{pre_name}」完成{repos_hint}，推荐流程 {fid}", kind="phase")
    # #419 选流程留档（供复盘/离线评估筛样本）：理由由前置环节模型自述，只落事件、不进 summary（#342）
    chosen = flow_by_id(fid)
    chosen_name = chosen["name"] if chosen else fid
    if source == "模型推荐":
        choice_txt = (f"推荐流程 {fid}（{chosen_name}）｜理由：{_flow_reason_text(result) or '（未给理由）'}"
                      "｜来源：模型推荐")
    else:
        choice_txt = (f"推荐流程缺失、非法或已禁用（{suggested!r}），兜底流程 {fid}（{chosen_name}）"
                      "｜来源：兜底默认")
    post_dispatcher_event(int(req_id), choice_txt, kind="flow_choice")
    outside = outside_repos(req["project"], raw_repos)
    if outside or PROJECTS[req["project"]]["confirm"]:
        why = (f"识别到清单外仓库 {', '.join(outside)}（疑似建错项目或项目仓库清单缺配置）"
               if outside else "本项目配置为进流程前人工确认")
        names = "、".join(f"{fx['id']}（{fx['name']}）" for fx in enabled_flows())
        question = (f"前置环节完成{repos_hint}，推荐流程「{fid}」；{why}。可选流程：{names}。"
                    "回复 y 采纳推荐流程并继续；回复 n 转人工处理。")
        entry = {"req": req, "question": question, "flow_id": fid}
        rounds = _register_need_confirm(req_id, entry, pre_role())
        _notify_need_confirm(req, question, pre_role(), rounds)
        post_dispatcher_event(int(req_id), f"「{pre_name}」需要确认：{question}", kind="confirm")
        return
    _adopt_flow_and_spawn(req, fid)


QUICK_FLOW_ID = "quick"
QUICK_UPGRADE_FLOW_ID = "standard"


def _quick_high_risk_prefixes(req):
    """项目清单声明的高风险路径前缀（各仓库 .loop/project.json 的 high_risk_paths 合并）。"""
    out = []
    for repo in _project_repos(req.get("project")):
        out.extend(load_project_manifest(repo)["high_risk_paths"])
    return out


def _path_under(path, prefix):
    pre = str(prefix).strip().rstrip("/")
    return bool(pre) and (path == pre or path.startswith(pre + "/"))


def _quick_upgrade(req, node, nodes, nxt_i):
    """B2 quick 流程升级闸：quick 卡的 code 节点完成、下一步即收尾时，改动命中高风险范围 → 快照
    改写为标准流程并进入其同角色节点之后的节点（代码审查），返回 True（调用方不再推进）；
    未命中返回 False。高风险范围 = 调度器模块（RESTART_TRIGGER_FILES，按 basename）∪ 项目清单
    high_risk_paths（路径前缀）∪ `.loop/`（会进后续所有 prompt）；改动清单取不到按命中处理。
    fail-closed：需要升级但标准流程不可用 → 转暂缓等人工，绝不按 quick 无审查收尾。
    必须先于指针 +1 判定：指针一旦指向 terminal，重启对账/主循环兜底会直接收尾。
    仅 quick 流程采集改动清单（其它流程零新增 git 调用）。"""
    req_id = str(req["id"])
    if (req.get("flow") or "") != QUICK_FLOW_ID:
        return False
    nxt = nodes[nxt_i] if nxt_i < len(nodes) else None
    if nxt is not None and (nxt.get("node_type") or "") != "terminal":
        return False
    paths = _collect_merge_changed_paths(req, dev_branch(req))
    if paths is None:
        why = "改动文件清单采集失败，按保守处理"
    else:
        prefixes = _quick_high_risk_prefixes(req)
        hit = sorted({f for f in paths
                      if Path(f).name in RESTART_TRIGGER_FILES or _path_under(f, ".loop")
                      or any(_path_under(f, pre) for pre in prefixes)})
        if not hit:
            return False
        why = "改动触及高风险路径 " + "、".join(hit[:5]) + (" 等" if len(hit) > 5 else "")
    target = flow_by_id(QUICK_UPGRADE_FLOW_ID) if flow_enabled(QUICK_UPGRADE_FLOW_ID) else None
    t_nodes = (target or {}).get("nodes") or []
    same = next((i for i, n in enumerate(t_nodes) if n.get("role") == node.get("role")), None)
    if not target or same is None or same + 1 >= len(t_nodes):
        log.error(f"需求 #{req_id} quick 流程需升级（{why}）但标准流程不可用，转暂缓")
        set_status(req["id"], "later")
        append_summary(req_id, f"{node_display(node)} 完成；{why}，快速流程应升级但标准流程不可用，转暂缓等人工处理。")
        post_dispatcher_event(int(req_id), f"快速流程应升级（{why}）但标准流程不可用，需求转暂缓",
                              kind="flow_choice")
        notify(f"需求 #{req_id} 走快速流程但{why}，标准流程不可用无法自动升级，已转暂缓，请人工处理。")
        return True
    fresh = _write_flow_snapshot(req, target["id"], same + 1)
    if fresh is None:
        return True      # 快照写入未生效已留痕：暂停推进，不按 quick 直接收尾
    nxt_node = t_nodes[same + 1]
    append_summary(req_id, f"{node_display(node)} 完成；{why}，流程由快速升级为{target['name']}，"
                           f"进入{node_display(nxt_node)}。")
    post_dispatcher_event(int(req_id),
                          f"流程升级 quick → {target['id']}（{target['name']}）｜理由：{why}｜来源：调度器",
                          kind="flow_choice")
    if (nxt_node.get("node_type") or "") == "terminal":
        _run_terminal(fresh)
    else:
        _spawn_safe(fresh, nxt_node.get("role"))
    return True


def _advance_after_node(req, node):
    """agent 节点完成 → 快照指针 +1（写后回读）→ 下一节点：terminal 同回合内联执行收尾，
    否则拉起下一节点角色（§1.5 推进链内联，不依赖只捞 open 的主循环）。"""
    req_id = str(req["id"])
    _fid, nodes, idx = flow_snapshot(req)
    if nodes is None:
        log.error(f"需求 #{req_id} 推进失败：卡无流程快照（{node_display(node)} 完成）")
        return
    if (node.get("node_type") or "") == "code":
        # #384：code 节点完成即代写 diff.patch（相对基线三点 diff），供后续评审/验收节点读取；
        # 生成失败不阻断推进（评审节点 build_prompt 时会再试）
        try:
            _gen_review_diff(req)
        except Exception as e:
            log.warning(f"需求 #{req_id} 代写 diff.patch 失败（不阻断推进）: {e}")
    nxt_i = idx + 1
    if (node.get("node_type") or "") == "code" and _quick_upgrade(req, node, nodes, nxt_i):
        return
    set_req(req["id"], {"node_index": nxt_i})
    fresh = fetch_req(req["id"]) or req
    nxt = nodes[nxt_i] if nxt_i < len(nodes) else None
    if nxt is None or (nxt.get("node_type") or "") == "terminal":
        append_summary(req_id, f"{node_display(node)} 完成，进入收尾。")
        post_dispatcher_event(int(req_id), f"「{node_display(node)}」完成，进入流程收尾（terminal）",
                              kind="phase")
        _run_terminal(fresh)
        return
    append_summary(req_id, f"{node_display(node)} 完成，进入{node_display(nxt)}。")
    post_dispatcher_event(int(req_id), f"「{node_display(node)}」完成，进入「{node_display(nxt)}」",
                          kind="phase")
    _spawn_safe(fresh, nxt.get("role"))

def _run_terminal(req):
    """terminal 收尾（调度器动作节点，不拉起 agent）：本地合并项目合并分支 / 仅推分支项目 push f-分支
    → 成功置 done 并清理 worktree 与分支；冲突拉起 merge_resolve（配额 2 轮/单轮 15 分钟），
    耗尽转 need_confirm（以上分支 finalize_done 内部已覆盖）。幂等：已 done 直接跳过。"""
    req_id = str(req["id"])
    fresh = fetch_req(req_id) or req
    if (fresh.get("status") or req.get("status")) == "done":
        return
    flow_label = fresh.get("flow_name") or fresh.get("flow") or ""
    post_dispatcher_event(int(req_id), f"流程收尾（terminal）：开始合并/接测（{flow_label}）", kind="merge")
    finalize_done(fresh, summary=f"流程「{flow_label}」走完，进入收尾。")


def _apply_model_override(resolved, model_override):
    """run_agent 本轮模型覆盖：str = #11 运行期兜底（全局模型、记降级）；dict = C3 评审升级
    （{model, source="escalation", degraded, reason}，保留 configured 供「配置 X → 实际 Y」留痕）；
    空 = 原样。"""
    if not model_override:
        return resolved
    if isinstance(model_override, dict):
        return dict(resolved, **model_override)
    return dict(resolved, model=model_override, source="global", degraded=True,
                reason="运行期模型不可用，本轮改用全局兜底模型")


def review_escalation_override(req_id, role):
    """C3：本卡该角色处于评审升级期时返回 run_agent 的 dict 覆盖，否则 None。"""
    with state_lock:
        model = ((state.get("review_escalation") or {}).get(str(req_id)) or {}).get(role)
    if not model:
        return None
    return {"model": model, "source": "escalation", "degraded": False,
            "reason": "评审连续打回，作者模型升级"}


def _escalation_candidate(req, role):
    """C3 升级目标校验：返回 (model, None) 或 (None, 不升级原因)。
    项目未配置 / 与作者当前模型相同 / 不在别名池 / 在池但不可用 → 不升级；池不可用 fail-open。"""
    model = project_escalation_model(req.get("project"))
    if not model:
        return None, "项目未配置 escalation_model"
    current = (resolve_role_model(role, project=req.get("project")) or {}).get("model")
    if model == current:
        return None, f"作者模型已是 {model}"
    pool, _home, _warning = model_alias_pool()
    if pool is not None:
        in_pool, usable, why = _pool_lookup(pool, model)
        if not in_pool:
            return None, f"escalation_model {model} 不在别名池"
        if not usable:
            return None, f"escalation_model {model} 不可用（{why or '未说明'}）"
    return model, None


def _clear_review_escalation(req_id, rounds_key, role):
    """C3：评审节点通过 / 人工放行 → 清该节点的各轮要点与作者角色的升级记录。"""
    with state_lock:
        (state.get("review_round_points") or {}).get(str(req_id), {}).pop(rounds_key, None)
        if role:
            (state.get("review_escalation") or {}).get(str(req_id), {}).pop(role, None)


# C4 打回 extra 内联评审产物的长度上限（超出截断并给完整路径，评审方可把细节留在产物文件）
REVIEW_EXTRA_MAX_CHARS = 6000


def _review_bounce_extra(req, node, reason, round_no=None):
    """评审打回时注入上游节点续跑上下文的 extra（#419/#342）：评审结论只进 req_reviews 表，
    送达上游靠 extra——resume 路径 build_prompt 产物被丢弃、仅 `{extra}` 进入续跑消息，
    不注入则上游看不到审查发现；附评审产物全文（上游只读产物目录，注入不依赖其主动读取）。"""
    tag = f"（第 {round_no} 轮）" if round_no else ""
    extra = (f"⚠️「{node_display(node)}」评审未通过，已打回本节点复修{tag}。\n"
             f"评审意见：{reason or '未说明原因'}")
    art = node.get("artifact") or role_meta(node.get("role"), "artifact")
    if art:
        try:
            p = artifact_dir(req) / art
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if len(text) > REVIEW_EXTRA_MAX_CHARS:
                    text = (text[:REVIEW_EXTRA_MAX_CHARS]
                            + f"\n……（产物超长已截断，完整内容见 {p}）")
                if text:
                    extra += f"\n\n评审产物（{art}）全文：\n{text}"
        except Exception as e:
            log.warning(f"需求 #{req['id']} 读取评审产物 {art} 失败（{e}），extra 仅含意见摘要")
    return extra


def _review_node_done(req, node, result, summary):
    """评审节点完成（§1.4）：verdict pass → 前进（清轮次）；reject → 打回前驱节点复修
    （**先回退指针再 resume**，否则 #279 守卫按当前节点校验会拦截致卡死；打回意见经 extra 注入
    上游续跑会话，评审结论本身只进 req_reviews 表、不写 notes/summary——#342）；轮次键 = 评审节点下标；
    轮次 > FLOW_REVIEW_ROUNDS_MAX → need_confirm（人工唯一通道）。"""
    req_id = str(req["id"])
    role = node.get("role")
    verdict = (result or {}).get("verdict")
    if verdict not in ("pass", "reject"):
        log.warning(f"需求 #{req_id} 评审节点 {role} verdict 缺失/非法（{verdict!r}），fail-closed 按打回处理")
        post_dispatcher_event(int(req_id),
                              f"「{node_display(node)}」评审结论缺失（verdict={verdict!r}），按打回处理",
                              kind="phase")
        verdict = "reject"
    # 评审留档（req_reviews，append-only）：结论摘要 + 产物路径；agent 只读无权写，调度器代写
    try:
        api_board("POST", f"/requirements/{req_id}/reviews",
                  {"reviewer": f"agent:{role}", "verdict": verdict,
                   "content": f"[{node_display(node)}] {format_summary_points(summary) or '（无意见摘要）'}"
                              f"\n产物：{artifact_short(req, role_meta(role, 'artifact'))}"})
    except Exception as e:
        log.warning(f"需求 #{req_id} 评审留档 req_reviews 失败: {e}")
    _fid, nodes, idx = flow_snapshot(req)
    rounds_key = str(idx)
    prev = nodes[idx - 1] if idx > 0 else None
    if verdict == "pass":
        with state_lock:
            state.get("flow_review_rounds", {}).get(req_id, {}).pop(rounds_key, None)
        _clear_review_escalation(req_id, rounds_key, prev.get("role") if prev else None)
        save_state()
        append_summary(req_id, f"{node_display(node)}通过。")
        post_dispatcher_event(int(req_id), f"「{node_display(node)}」评审通过", kind="phase")
        _advance_after_node(req, node)
        return

    points = (format_summary_points(summary) or "未说明")[:300]
    with state_lock:
        rounds = state.setdefault("flow_review_rounds", {}).setdefault(req_id, {})
        rounds[rounds_key] = int(rounds.get(rounds_key, 0)) + 1
        cur = rounds[rounds_key]
        # C3 各轮打回要点（转人工时对比展示；只进 state 与确认问题，不进 notes/summary——#342）
        state.setdefault("review_round_points", {}).setdefault(req_id, {}).setdefault(
            rounds_key, []).append({"round": cur, "points": points})
    save_state()
    if prev and cur <= FLOW_REVIEW_ROUNDS_MAX:
        esc_tag = ""
        if cur == FLOW_REVIEW_ROUNDS_MAX:
            # C3 熔断第一级：作者模型升级后再打回（记录随卡 × 角色，spawn_agent 自动带上）
            model, why = _escalation_candidate(req, prev.get("role"))
            if model:
                with state_lock:
                    state.setdefault("review_escalation", {}).setdefault(req_id, {})[prev.get("role")] = model
                save_state()
                esc_tag = f"，作者模型升级为 {model}"
                post_dispatcher_event(int(req_id),
                                      f"「{node_display(node)}」连续 {cur} 轮未通过，"
                                      f"「{node_display(prev)}」作者模型升级为 {model}", kind="model")
            else:
                post_dispatcher_event(int(req_id),
                                      f"「{node_display(node)}」连续 {cur} 轮未通过，作者模型未升级（{why}）",
                                      kind="model")
        # 顺序敏感（§1.4）：先回退指针到前驱（写后回读）再 resume，否则 #279 守卫按当前节点拦截致卡死
        set_req(req["id"], {"node_index": idx - 1})
        fresh = fetch_req(req["id"]) or req
        append_summary(req_id, f"{node_display(node)}未通过，打回{node_display(prev)}重做（第 {cur} 轮{esc_tag}）。")
        post_dispatcher_event(int(req_id),
                              f"「{node_display(node)}」未通过，打回「{node_display(prev)}」重做（第 {cur} 轮）",
                              kind="phase")
        _spawn_safe(fresh, prev.get("role"),
                    extra=_review_bounce_extra(fresh, node, format_summary_points(summary), cur),
                    resume=True)
        return
    with state_lock:
        history = list(((state.get("review_round_points") or {}).get(req_id) or {}).get(rounds_key) or [])
        escalated = ((state.get("review_escalation") or {}).get(req_id) or {}).get(
            prev.get("role") if prev else None)
    compare = "\n".join(f"- 第 {h['round']} 轮：{h['points']}" for h in history[-4:])
    esc_note = f"（已升级作者模型 {escalated}）" if escalated else ""
    append_summary(req_id, f"「{node_display(node)}」连续 {cur} 轮未通过{esc_note}，转人工裁决。")
    question = (f"「{node_display(node)}」连续 {cur} 轮评审未通过{esc_note}。各轮打回要点：\n{compare}\n"
                "回复 y 放行进入下一节点；回复 n <原因> 打回前驱节点再修一轮。")
    entry = {"req": req, "question": question, "review_index": idx}
    rounds_n = _register_need_confirm(req_id, entry, role)
    _notify_need_confirm(req, question, role, rounds_n)
    post_dispatcher_event(int(req_id), f"「{node_display(node)}」需要确认：{question}", kind="confirm")


def backfill_flow_snapshots():
    """#419 存量卡快照回填（dispatcher 启动，幂等；§4.2）：仅处理 nodes_json 为空且
    status ∈ {open, doing, route, review} 的卡——route → open 且清快照（重走前置环节）；
    mode 为空 = 未入流程（前置环节中/排队中）→ 不写快照，保持前置阶段（与 role_for/_legacy_position
    同源，防前置槽位被跳过、analysis 结果被当后置流程首节点产物采纳）；
    mode=auto → flow=auto/index=0；mode=semi → flow=flow||default，index=phase 下标（缺失 → 0）；
    status=review 视为所在节点已通过（下标 +1，落 terminal 的 open+terminal 卡由启动扫描收尾）；
    旧 flow id 不在配置 → default_flow + 留痕；done/dropped 有 flow 值的卡仅冻结 flow_name（展示用）。"""
    def _backfill_one(r):
        rid = str(r["id"])
        status = r.get("status")
        if r.get("nodes_json"):
            return
        if status in ("done", "dropped"):
            if r.get("flow") and not r.get("flow_name"):
                f = flow_by_id(r["flow"])
                if f:
                    set_req(r["id"], {"flow_name": f["name"]})
            return
        if status not in ("open", "doing", "route", "review"):
            return
        if status == "route":
            set_req(r["id"], {"status": "open", "flow": None, "flow_name": None,
                              "nodes_json": None, "node_index": None})
            post_dispatcher_event(int(rid), "启动回填：旧「通道待确认」卡转 open，重走前置环节", kind="phase")
            return
        # auto 流程已删除 → 本条件恒为假，mode=auto 的无快照卡掉进下方 else（视为未入流程、不写快照）；
        # 自动通道 #419 后无产生路径，实测无此类在途卡
        if (r.get("mode") or "") == "auto" and flow_by_id("auto"):
            fid, idx = "auto", 0
        elif (r.get("mode") or "") == "semi":
            raw_fid = r.get("flow")
            fid = raw_fid if flow_by_id(raw_fid) else (default_flow_id()
                                                       or ((enabled_flows() or [{}])[0].get("id") or "standard"))
            if raw_fid and not flow_by_id(raw_fid):
                post_dispatcher_event(int(rid),
                                      f"启动回填：旧流程 id {raw_fid!r} 已不在配置，改用默认流程 {fid}",
                                      kind="flow_fallback")
            nodes = flow_nodes(fid)
            idx = 0
            phase = r.get("phase")
            if phase:
                for i, n in enumerate(nodes):
                    if n.get("role") == phase:
                        idx = i
                        break
        else:
            # mode 为空 = 未入流程（前置环节中/排队中，§1.5「无快照 → 前置环节」）：
            # 不写快照、不钉到后置流程节点——否则前置槽位被跳过（suggested_flow/repos 契约丢失），
            # 前置产物还会被当流程首节点产物采纳
            if status == "review":
                # 无流程信息的遗留「评审中」卡无法定位节点 → 转 open 重走前置环节（与 route 同）
                set_req(r["id"], {"status": "open", "flow": None, "flow_name": None,
                                  "nodes_json": None, "node_index": None})
                post_dispatcher_event(int(rid),
                                      "启动回填：无流程信息的旧「评审中」卡转 open，重走前置环节",
                                      kind="phase")
            return
        if status == "review":
            idx = min(idx + 1, max(len(flow_nodes(fid)) - 1, 0))
            set_status(r["id"], "open")
        fresh = _write_flow_snapshot(r, fid, idx)
        if fresh is not None:
            post_dispatcher_event(int(rid),
                                  f"启动回填：流程快照 {fid} 节点 {idx}（status {status} → {fresh.get('status')}）",
                                  kind="phase")

    for project in PROJECTS:
        for st in ("open,doing,route,review",):
            try:
                rows = api("GET", f"?project={project}&status={st}")
            except Exception as e:
                log.warning(f"启动回填拉取 {project}/{st} 失败（跳过）: {e}")
                continue
            for r in rows or []:
                try:
                    _backfill_one(r)
                except Exception as e:
                    log.warning(f"启动回填需求 #{r.get('id')} 失败（跳过）: {e}")


def handle_result(req, role, returncode, timed_out, started_at, aborted=False):
    req_id = str(req["id"])
    workdir = workdir_for(req["project"])
    # 结果文件按需求隔离在产物目录；兼容旧约定（工作目录根 .req-board-result.json）做兜底
    result_file = artifact_dir(req) / "result.json"
    if not result_file.exists():
        legacy = workdir / RESULT_FILENAME
        if legacy.exists():
            result_file = legacy
    result = None
    parse_error = None
    backup_file = None
    redline_hit = False  # #113：dev 红线取消归因标记（仅 handle_result 内使用，不落 state）
    if result_file.exists():
        try:
            result = json.loads(result_file.read_text())
        except Exception as e:
            # 解析失败保留现场：改名备份（.invalid 后缀，os.replace 覆盖旧备份防多次重试堆积），
            # 错误信息含行/列（JSONDecodeError 自带 lineno/colno）与文件路径，便于排查
            parse_error = e
            backup_file = result_file.with_name(result_file.name + ".invalid")
            try:
                os.replace(result_file, backup_file)
            except OSError as e2:
                backup_file = None
                log.warning(f"需求 #{req_id} 结果文件非法 JSON（{result_file}: {e}），且备份失败: {e2}")
                result_file.unlink(missing_ok=True)
            else:
                log.warning(f"需求 #{req_id} 结果文件非法 JSON（{result_file}: {e}），已保留现场 {backup_file}")
        else:
            result_file.unlink(missing_ok=True)

    status = (result or {}).get("status")
    summary = (result or {}).get("summary", "")
    # #279：解析成功即 result 文件已删（上面 unlink），todos 在此提取，暂存 state 供 done 流程消费
    todos = _extract_todos(result)

    # #284 防御：需求已完成（done）后，迟到的会话收尾（人工续跑/自动会话）不得回卷状态——
    # 结果现场已清理（上面 unlink/备份），直接跳过状态推进/通知/重试，避免 done 被改回 review/later
    if req.get("status") == "done":
        log.info(f"需求 #{req_id} 已处于 done，会话收尾跳过状态推进（role={role}）")
        return

    valid_done = False
    gate_fail_reason = None  # #399：内容仲裁分流信号（校验失败原因；进程异常路径不设，天然排除在仲裁外）
    _gate_node = current_node(req)
    # #419：完成门槛由节点类型决定（取代角色能力表的 done_gate 查表）——
    # code 节点要代码变更；artifact/review/前置槽位要产物非空（review 结论在推进处校验）
    _gate_type = (_gate_node or {}).get("node_type") or "artifact"
    if status == "done" and not timed_out and not aborted and returncode == 0:
        if _gate_type == "code":
            if has_code_changes(req, started_at):
                valid_done = True
            else:
                gate_fail_reason = "dev 分支相对基线无代码变更"
                summary = "声称完成但无代码变更或进程异常，按失败处理"
        else:
            ok, why = artifact_ok(req, role)
            if ok:
                valid_done = True
            else:
                gate_fail_reason = why
                summary = f"声称完成但产物校验失败（{gate_fail_reason}），按失败处理"
    elif status == "done":
        summary = summary or "声称完成但进程异常，按失败处理"

    if valid_done:
        # 成功推进：清零重试计数（每个节点独立容错额度）
        clear_retries(req_id)
        if role == "merge_resolve":
            # #396：agent 已解决冲突 → 恢复 main 现场 → 重试合并 → 成功完成/仍冲突转人工
            _finish_merge_resolve(req, req_id, agent_summary=summary)
            return
        node = current_node(req)
        if node is None:
            # 前置槽位完成（§1.4）：推荐流程 + 涉及仓库 → 采纳流程（或人工确认）→ 拉起流程首节点
            _pre_stage_done(req, result, todos)
            return
        ntype = node.get("node_type")
        if ntype == "review":
            # 评审节点（§1.4）：pass 前进 / reject 打回前驱复修（轮次上限 → need_confirm）
            _review_node_done(req, node, result, summary)
            return
        # artifact / code 节点完成：todos 暂存 → 摘要留痕 → 推进下一节点（末个 agent 节点内联 terminal）
        if todos is not None:
            with state_lock:
                state.setdefault("req_todos", {})[req_id] = todos
            save_state()
        if summary:
            append_summary(req_id, f"完成总结：{format_summary_points(summary)}")
        _advance_after_node(req, node)
        return

    if role == "merge_resolve":
        # #396：merge_resolve 非 done 收尾不外溢通用通道（通用通道按 dev 重试语义计数/毒会话）——
        # need_confirm（语义冲突）/blocked 直接转人工；进程级失败（超时/异常/未提交/非法 JSON）
        # 按 merge_resolve 独立配额重试，耗尽转人工（fail-closed，分支/worktree 现场保留）
        with state_lock:
            rec = state.get("worktrees", {}).get(req_id)
        failed = list((_merge_resolve_entry(req_id) or {}).get("failed") or [])
        if status == "need_confirm" and not timed_out and returncode == 0:
            question = result.get("question") or "（agent 未说明问题）"
            _merge_conflict_fallback(req, rec, failed, question=question)
            return
        if status == "blocked" and not timed_out and returncode == 0:
            _merge_conflict_fallback(req, rec, failed, reason=summary or "agent 报告阻塞但未说明原因")
            return
        if parse_error:
            reason = f"结果文件非法 JSON（{result_file}: {parse_error}）"
        elif timed_out:
            reason = "运行超时"
        elif returncode == 0:
            reason = summary or "进程正常退出但未提交结果（会话提前结束或未写 result.json）"
        else:
            reason = summary or f"进程异常退出（退出码 {returncode}），未提交结果"
        if _merge_resolve_retry(req, reason):
            return
        _merge_conflict_fallback(req, rec, failed, reason=reason)
        return

    # #399：内容仲裁通道——进程正常退出 + status=done + 收尾校验不符 + result.json 带豁免申报
    # waive_gate（非空字符串）→ 不计 retries、不走进程失败重试，转 pending_confirm 人工裁决
    # （fail-closed：无人工显式 y 绝不放行）；未申报 = 视为谎报/契约缺失，保持原失败通道不变
    # （merge_resolve 非 done 已在上方独立通道拦截，此处仅覆盖其它角色的 done 谎报豁免）
    if gate_fail_reason and not valid_done:
        waive = (result or {}).get("waive_gate")
        if isinstance(waive, str) and waive.strip():
            question = (f"收尾校验未通过：{gate_fail_reason}。agent 申报豁免：{waive.strip()}。"
                        "回复 y 豁免完成（直接收口需求）/ n 退回待开发。")
            entry = {"req": req, "question": question, "kind": "confirm_override",
                     "summary": (result or {}).get("summary", ""), "todos": todos,
                     "options": None}
            rounds = _register_need_confirm(req_id, entry, role)
            log.info(f"需求 #{req_id} 收尾校验未过（{gate_fail_reason}），agent 申报豁免，"
                     f"转人工裁决（role={role}，第 {rounds} 轮）")
            _notify_need_confirm(req, question, role, rounds)
            # #411：confirm_override 事件也走提问格式（「X」需要确认：<问题全文>）——回放常驻卡片
            # 承载全文；原摘要式 content 无全文（未决期间网页端仅有横幅展示，横幅已移除故需此兜底载体）
            post_dispatcher_event(req_id, f"「{ROLE_NAMES.get(role, role)}」需要确认：{question}",
                                  kind="confirm")
            return

    if status == "need_confirm" and not timed_out and returncode == 0:
        question = result.get("question") or "（agent 未说明问题）"
        # #419：评审发现问题不再按角色名转人工三档裁决——评审节点按 verdict 驱动
        # （pass 前进 / reject 自动打回上游复修，见 _review_node_done）；agent 自判需要人工时
        # 走普通 need_confirm（对话类 options 解析）。kind=review_gate 不再新写入（消费分支
        # 保留仅为过渡窗口兼容历史条目）。
        entry = {"req": req, "question": question, "options": _parse_confirm_options(result)}
        rounds = _register_need_confirm(req_id, entry, role)
        log.info(f"需求 #{req_id} 待确认: {question}（第 {rounds} 轮）")
        _notify_need_confirm(req, question, role, rounds)
        # #411 讨论稿：事件留档带角色名（board 回放解析「「X」需要确认：」前缀做摘要行 + 全文卡片）
        post_dispatcher_event(req_id, f"「{ROLE_NAMES.get(role, role)}」需要确认：{question}", kind="confirm")
        return

    # #385：blocked 分流——blocked 是内容性阻塞结论（agent 明确报告，多依赖外部修复，#376 accept 21:27
    # 现场实测：30s 重试轮只会复核后重报 blocked 再转 later，浪费一轮 LLM 会话）。进程正常退出时
    # 不进重试通道，直接人工收口：等效「重试耗尽」路径（later + 毒会话 + 通知，保留人工续跑入口），
    # 不经 retries 计数（计数语义收窄为纯进程级失败）；blocked 但进程异常（超时/非 0 退出码）仍走下方
    # 进程级失败重试通道
    if status == "blocked" and not timed_out and returncode == 0:
        reason = summary or "agent 报告阻塞但未说明原因"
        set_status(req["id"], "later")
        append_summary(req_id,
                       f"需求阻塞（已转 later 待人工处理，不再自动重试）：{format_summary_points(reason)}")
        with state_lock:
            # 对齐重试耗尽路径 pop：残留 last_failure 会经 resume_failure_hint 注入人工续跑 prompt 造成误导
            state["retries"].pop(req_id, None)
            # #11：转 later 停止自动调度，清模型降级记录（防残留跨周期误用）
            state.setdefault("model_fallback", {}).pop(req_id, None)
        save_state()
        mark_session_poisoned(req_id, role)
        log.info(f"需求 #{req_id} 报告阻塞（{reason}），直接转 later 待人工处理（role={role}）")
        notify_req_card("blocked", req, reason=reason, phase=role)
        post_dispatcher_event(req_id,
                              f"需求阻塞（{reason}），已转 later 待人工处理（不自动重试，role={role}）",
                              kind="retry")
        return

    rate_limited = False  # #345：API 并发限流（403）归因标记（仅 handle_result 内使用，不落 state）
    if parse_error:
        kept = f"，现场已备份为 {backup_file}" if backup_file else ""
        reason = f"结果文件非法 JSON（{result_file}: {parse_error}）{kept}，未提交结果"
    elif timed_out:
        reason = f"运行超时（budget_exceeded: time，上限 {effective_timeout_minutes(req, role)} 分钟）"
    elif aborted:
        # #28 输出守卫中止（须置于通用 else 之前，否则 aborted 的负退出码退化为「进程异常退出」）；
        # C2 预算截停传入结构化原因串
        reason = aborted if isinstance(aborted, str) else "本轮输出复读/单次请求超长，已中止会话，未提交结果"
    else:
        # #345：先做限流分类——限流是环境侧并发位被占，与进程行为无关，须与「进程异常退出」
        # 「正常退出未提交结果」区分（403 现场即落入「进程异常退出（退出码 -15）」的笼统归因）；
        # 覆盖正常/异常退出两类失败路径，parse_error/timed_out（有明确文件/调度证据）保持优先
        rate_limit_reason = _rate_limit_failure_reason(req_id)
        if rate_limit_reason:
            reason = rate_limit_reason
            rate_limited = True
        elif returncode == 0:
            reason = summary or "进程正常退出但未提交结果（会话提前结束或未写 result.json）"
            # #113：dev 红线取消归因——仅"无结果文件 + 退出码 0 + dev"路径查询事件，其余失败路径零触发
            if not result and role == "dev":
                redline_evt = _redline_cancel_reason(req_id)
                if redline_evt:
                    reason = f"命中进程安全红线，会话被拦截取消，未写 result.json（事件：{redline_evt}）"
                    redline_hit = True
        else:
            reason = summary or f"进程异常退出（退出码 {returncode}），未提交结果"
    with state_lock:
        rec = _retry_rec(req_id)
        retries = rec.get("count", 0) + 1
        # #109：失败原因持久化（升级前为纯 int 计数，读取方用 _retry_rec 兼容），
        # 调度器重启/主循环重捞 resume 时据此注入续跑 prompt，重试不再盲续
        state["retries"][req_id] = {"count": retries, "last_failure": reason}
    save_state()
    if retries <= MAX_RETRIES:
        # #11 运行期模型不可用 → 单次兜底重跑（不新增失败分支，只给既有重试追加 model_override）：
        # 三重收敛——① 归因命中（限流 _rate_limit_failure_reason，或 _model_unavailable_reason）
        # ② 来源非全局 ③ 本轮未用过；#345 的延迟与限流 hint 逐字保留，仅让这次重试换模型。
        use_fallback = False
        with state_lock:
            fb = (state.get("model_fallback") or {}).get(req_id) or {}
            eligible = (fb.get("role") == role and fb.get("source") not in (None, "", "global")
                        and not fb.get("used"))
        if eligible and (rate_limited or _model_unavailable_reason(req_id)):
            use_fallback = True
            with state_lock:
                state.setdefault("model_fallback", {}).setdefault(req_id, {})["used"] = True
            save_state()
        log.warning(f"需求 #{req_id} 第 {retries} 次失败（{reason}），自动重试（role={role}）"
                    + ("，本次改用全局兜底模型" if use_fallback else ""))
        post_dispatcher_event(req_id, f"运行失败（{reason}），第 {retries} 次自动重试（role={role}）"
                              + ("，本次改用全局兜底模型" if use_fallback else ""), kind="retry")
        # #345：限流失败延迟重试（并发位未释放前不立即重发，避免二次 403 烧光唯一重试），
        # 其它失败类型保持原退避 min(retries*30, 120)
        delay = RATE_LIMIT_RETRY_DELAY if rate_limited else min(retries * 30, 120)
        time.sleep(delay)
        retry_hint = ""
        if rate_limited:
            retry_hint = ("上次失败为 API 并发限流（403）：并发位被占，本次已延迟重试等待并发位释放；"
                          "若再遇 403 不要立即重发请求。")
        elif redline_hit:
            retry_hint = ("上次会话因命中进程安全红线被拦截取消：先读被拦截命令自查（看板执行过程/agent_events），"
                          "测试实例清理必须使用项目约定的实例脚本（PID 文件 + 精确 PID/后代清理），"
                          "禁止自行编写 trap/kill/killpg/pkill 或清理 ACP 驱动进程/进程组命令。")
        elif parse_error and backup_file:
            retry_hint = (f"上次结果文件非法 JSON，原文件已改名保留在 {backup_file}，先读取自查避免重蹈覆辙；"
                          f"写 result.json 请用 python -c 'json.dumps(...)' 或脚本生成，"
                          f"summary 内换行必须转义为 \\n。")
        elif isinstance(aborted, str) and aborted.startswith("budget_exceeded"):
            retry_hint = ("上次会话工具步数超预算被截停：先读 summary 与产物确认已有进度，"
                          "缩小探索范围、直奔改动点，接近上限前先写 result.json 保底。")
        elif aborted:
            retry_hint = ("上次会话因输出复读/单次请求超长被输出守卫中止：避免重复输出同一段文本，"
                          "若再次复读，自动重试耗尽后将转人工处理。")
        elif returncode == 0 and not result:
            retry_hint = ("上次进程正常退出但未提交结果：先确认改动完好（git status / 产物目录）"
                          "再前台跑验证确认无误，最后用 python json.dumps 补写 result.json（status/summary 必填）。")
        if use_fallback:
            retry_hint += ("本次已改用全局兜底模型重试（该角色配置的模型运行期不可用）；"
                           "若仍失败将按现有路径转人工处理。")
        # 非兜底路径逐字保持原调用形态（不给存量调用点/stub 引入新 kwarg）
        extra_kw = {"model_override": ACP_MODEL} if use_fallback else {}
        spawn_agent(req, role=role,
                    extra=f"上次尝试失败（第 {retries} 次）：{reason}。{retry_hint}",
                    resume=True, **extra_kw)
    else:
        # #113：重试耗尽不再退回 open（主循环只捞 open，否则一分钟静置后自动重捞形成无限循环），
        # 转 later 停止自动调度，等待人工明确改回 open 后才开始新的重试周期
        set_status(req["id"], "later")
        append_summary(req_id, f"自动开发失败 {retries} 次（已停止自动调度，等待人工重新开放），"
                               f"最后原因：{format_summary_points(reason)}")
        with state_lock:
            state["retries"].pop(req_id, None)
            # #11：重试耗尽转 later，清模型降级记录（防残留）
            state.setdefault("model_fallback", {}).pop(req_id, None)
        mark_session_poisoned(req_id, role)
        log.error(f"需求 #{req_id} 重试耗尽，停止自动调度转 later: {reason}")
        notify_req_card("failed", req, retries=retries, reason=reason)
        post_dispatcher_event(req_id, f"自动开发失败 {retries} 次，停止自动调度转 later（role={role}，{reason}）", kind="retry")

# ==================== 决策对账 & 主循环 ====================
def reconcile_ghost_pending():
    """#25 启动存量终态对账：卡片已 done/dropped 时，state 里同需求任意 kind 的
    pending_confirm 条目（含历史 review_gate/merge/普通 confirm/confirm_override）
    必为幽灵——人工置终态或推进路径完成绕过事件驱动消费，条目永久残留（#24 现场），
    清理防残留积累并消除 done 卡的「待确认」徽标。人工置 done/dropped 不依赖调度器
    收尾路径，故由启动对账兜底覆盖。
    逐卡现取裁决：卡不在看板/回读 None/回读异常/非终态一律跳过（#394 非终态全 kind 保留，
    由 _purge_ghost_confirm 的 fetch_req 复核最终裁决）；单条失败降级继续，看板不可达整体跳过。"""
    try:
        all_reqs = {r["id"]: r for r in api("GET", "")}
    except Exception as e:
        log.warning(f"幽灵待确认对账拉取需求失败（本轮跳过）: {e}")
        return
    with state_lock:
        snapshot = list(state["pending_confirm"].items())
    for rid, entry in snapshot:
        if not isinstance(entry, dict):
            continue
        try:
            # #27：按条目内容卡 id 判终态（键服从内容），键与内容不同源时不按键误清
            req = all_reqs.get(_pending_content_req_id(entry, rid))
            if req is None or req.get("status") not in ("done", "dropped"):
                continue
            kind = entry.get("kind") or "confirm"
            if _purge_ghost_confirm(rid, "启动对账幽灵清理"):
                log.info(f"幽灵待确认对账：需求 #{rid} 已终态，清理残留 {kind} 待确认")
        except Exception as e:
            log.warning(f"需求 #{rid} 启动幽灵对账异常（跳过，继续其它条目）: {e}")
            continue


def reconcile_pending_confirm_source():
    """#27 启动对账（必须**先于** reconcile_ghost_pending 运行：先按键归一条目、后按 key 清账）。

    - 键与条目内容卡 id 不同源 → 按键修正（目标键已有条目则保留原键 + 告警，不覆盖在途条目）；
    - 条目内容卡现取终态（done/dropped）→ 清掉条目（不限 kind，见 tech §3.7 有意覆盖裁定）。

    条目非 dict / req 缺失 / id 不可解析 → 跳过（fail-open，不清理不抛）；看板不可达时不清账
    （fail-safe）。confirm_rounds 一律不动（错源两键分属两张卡，搬运会虚高）。返回 (修正数, 清理数)。"""
    with state_lock:
        snapshot = list(state["pending_confirm"].items())
    fixed = cleared = 0
    for rid, entry in snapshot:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("req"), dict):
            log.warning(f"待确认条目 #{rid} 缺 req 快照，跳过同源对账（fail-open）")
            continue
        try:
            cid = int(entry["req"]["id"])
        except (KeyError, TypeError, ValueError):
            log.warning(f"待确认条目 #{rid} 的 req.id 不可解析，跳过同源对账（fail-open）")
            continue
        # ① 键修正（键服从内容 id）
        if str(cid) != str(rid):
            moved = occupied = False
            with state_lock:
                if state["pending_confirm"].get(rid) is entry:
                    if str(cid) in state["pending_confirm"]:
                        occupied = True
                    else:
                        state["pending_confirm"].pop(rid, None)
                        state["pending_confirm"][str(cid)] = entry
                        moved = True
            if moved:
                save_state()
                fixed += 1
                msg = (f"待确认条目同源对账：键 #{rid} 与内容（需求 #{cid}）不同源，"
                       f"已按键修正为 #{cid}")
                log.info(msg)
                post_dispatcher_event(cid, msg, kind="confirm")
            elif occupied:
                msg = (f"待确认条目同源对账：键 #{rid} 与内容（需求 #{cid}）不同源，"
                       f"但目标键 #{cid} 已有待确认条目，保留原键 #{rid} 未覆盖，请人工处理")
                log.error(msg)
                post_dispatcher_event(int(rid), msg, kind="confirm")
        # ② 内容卡现取终态 → 清账（不限 kind；看板不可达本轮不清）
        try:
            req = fetch_req(cid)
        except Exception as e:
            log.warning(f"待确认同源对账读取需求 #{cid} 失败（本轮不清账）: {e}")
            continue
        if req is None or req.get("status") not in ("done", "dropped"):
            continue
        popped = False
        with state_lock:
            for k in (str(cid), str(rid)):
                if state["pending_confirm"].get(k) is entry:
                    state["pending_confirm"].pop(k, None)
                    popped = True
        if popped:
            save_state()
            cleared += 1
            msg = (f"待确认同源对账：条目内容所属需求 #{cid} 已 {req.get('status')}，"
                   f"清理失效待确认条目（原键 #{rid}）。问题：{entry.get('question') or ''}")
            log.info(msg)
            post_dispatcher_event(cid, msg, kind="confirm")
    return fixed, cleared


def reconcile_decisions():
    """扫 pending_route / pending_review，按需求当前 board 状态解释看板按钮/改判的决策。
    - 仍为 route/review：不动
    - open + mode=auto/semi（route 按钮已写入）：丢 pending 并直接 spawn（active_runs 防重复）
    - doing（review 通过按钮）：以 pending 记录的 phase 推下一阶段；accept 特判置 done
    - open（review 否决按钮）：丢 pending 供重捞
    各分支判定后向飞书补发确认消息（网页按钮无即时回执，由对账轮询统一补发；accept 通过不加发，
    避免与 finalize_done 的「已完成」通知重复）。
    兼作改判后陈旧 pending 的清理。"""
    with state_lock:
        routes = dict(state["pending_route"])
        reviews = dict(state["pending_review"])
    all_reqs = {}
    try:
        all_reqs = {r["id"]: r for r in api("GET", "")}
    except Exception as e:
        log.warning(f"决策对账拉取需求失败: {e}")
        return
    for req_id_str, pend in routes.items():
        req = all_reqs.get(int(req_id_str))
        if req is None:
            log.info(f"需求 #{req_id_str} 已不存在，通道 pending 静默清除")
            with state_lock:
                state["pending_route"].pop(req_id_str, None)
            continue
        if req["status"] == "route":
            continue
        with state_lock:
            state["pending_route"].pop(req_id_str, None)
        if req["status"] == "open" and req.get("mode") in ("auto", "semi"):
            log.info(f"需求 #{req_id_str} 通道已确认（{req.get('mode')}），进入 {role_for(req)}")
            clear_retries(req_id_str)
            clear_flow_rounds(req_id_str)
            # 事件文案带流程名（#397）：semi 显示「半自动·精简/标准/加强」，auto 显示「自动」
            # —— 与飞书通知（FLOW_NAMES 拼接）口径一致，历史回放可辨具体选型
            channel_cn = f"半自动·{FLOW_NAMES[flow_of(req)]}" if req.get("mode") == "semi" else "自动"
            post_dispatcher_event(int(req_id_str),
                                  f"通道已确认（{channel_cn}），进入「{ROLE_NAMES.get(role_for(req), role_for(req))}」",
                                  kind="phase")
            if req.get("mode") == "semi":
                notify(f"需求 #{req_id_str} 已在网页端确认走半自动通道（{FLOW_NAMES[flow_of(req)]}流程）。")
            else:
                notify(f"需求 #{req_id_str} 已在网页端确认走自动通道。")
            _spawn_safe(req, role_for(req))
        else:
            # 兜底留痕：用户改判为 later/dropped/done 等状态（未走确认/否决按钮），pending 已消费，
            # 写调度事件便于追溯（如空格误触通过后改回暂缓的场景），不发飞书避免打扰
            log.info(f"需求 #{req_id_str} 通道 pending 已清除：状态改为 {req['status']}（未走确认/否决）")
            post_dispatcher_event(int(req_id_str),
                                  f"通道 pending 已清除：需求状态改为「{req['status']}」，未走确认/否决",
                                  kind="phase")
    for req_id_str, pend in reviews.items():
        req = all_reqs.get(int(req_id_str))
        if req is None:
            log.info(f"需求 #{req_id_str} 已不存在，评审 pending 静默清除")
            with state_lock:
                state["pending_review"].pop(req_id_str, None)
            continue
        if req["status"] == "review":
            continue
        with state_lock:
            state["pending_review"].pop(req_id_str, None)
        if req["status"] == "doing":
            phase = pend.get("phase")
            if phase == "accept":
                append_summary(int(req_id_str), "人工验收通过，需求完成。")
                fresh = fetch_req(int(req_id_str)) or req
                finalize_done(fresh)
                post_dispatcher_event(int(req_id_str), "人工验收通过，需求完成", kind="phase")
            else:
                nxt = next_stage(req, phase)
                if nxt:
                    set_req(int(req_id_str), {"status": "doing", "phase": nxt})
                    clear_retries(req_id_str)
                    clear_flow_rounds(req_id_str)
                    fresh = fetch_req(int(req_id_str)) or req
                    log.info(f"需求 #{req_id_str} 评审通过（{phase}），进入 {nxt}")
                    post_dispatcher_event(int(req_id_str),
                                          f"「{ROLE_NAMES.get(phase, phase)}」评审通过，进入「{ROLE_NAMES.get(nxt, nxt)}」",
                                          kind="phase")
                    notify(f"需求 #{req_id_str} 已在网页端审核通过，进入 {ROLE_NAMES.get(nxt, nxt)} 阶段。")
                    _spawn_safe(fresh, nxt)
                # 无下一角色（正常流程不会出现，如未知 phase）：不推进、不误发通知（对齐 on_message 提示）
        elif req["status"] == "open":
            # 网页端「否决」按钮：pending 已 pop，补发飞书确认（summary 已由 board 追加「[auto] 用户否决」）
            # #376：打回=评审角色 phase 重定向到前置 stage（tech_review→tech / code_review→dev）；需求级否决
            #（board PATCH phase:null）phase 已置空，role_for 回 flow 首 stage；两种出口均清 retries/rounds
            phase = pend.get("phase")
            if phase in ("tech_review", "code_review") and req.get("phase") == phase:
                redirect = stage_of_review(req, phase)
                if redirect:
                    set_req(int(req_id_str), {"phase": redirect})
                    log.info(f"需求 #{req_id_str} 评审打回，{phase} 重定向到 {redirect}")
            clear_retries(int(req_id_str))
            clear_flow_rounds(int(req_id_str))
            post_dispatcher_event(int(req_id_str), "已在网页端否决，退回待开发", kind="phase")
            notify(f"需求 #{req_id_str} 已在网页端否决，退回待开发。")
        else:
            # 兜底留痕：用户改判为 later/dropped/done 等状态（未走通过/否决按钮，如空格误触通过后
            # 改回暂缓），pending 已消费但写调度事件便于追溯；不发飞书避免打扰
            log.info(f"需求 #{req_id_str} 评审 pending 已清除：状态改为 {req['status']}（未走通过/否决）")
            post_dispatcher_event(int(req_id_str),
                                  f"评审 pending 已清除：需求状态改为「{req['status']}」，未执行通过/否决动作",
                                  kind="phase")
    # #386 merge_conflict 网页决策对账：网页按钮把 action（retry/takeover）写入 merge_conflicts 表，
    # 本分支以 pending_confirm（kind=merge）为权威消费——与 on_message 同 state_lock 互斥，先 pop 者执行；
    # 无 pending 视为已由飞书 y/n 并发消费的幽灵决策，清表 + 事件留痕（防双通道并发双执行）。
    # retry 语义与飞书 y 一致：成功 → cleanup + restart 配方 + _set_done（含 _kickstart_board_if_own 与
    # #380 自动重启登记）；仍冲突 → 重挂 pending + 重置按钮等待再次点击。
    # takeover 语义与飞书 n 一致：置 later，dev 分支与 worktree 保留。
    try:
        conflicts = api_board("GET", "/merge-conflicts") or []
    except Exception as e:
        log.warning(f"决策对账拉取 merge 冲突列表失败（本轮跳过）: {e}")
        conflicts = []
    for mc in conflicts:
        action = mc.get("action")
        if action not in ("retry", "takeover"):
            continue  # action 为空=等待用户操作
        rid = str(mc["req_id"])
        mm = False
        cid = None
        with state_lock:
            pend = state["pending_confirm"].get(rid)
            if pend is None or pend.get("kind") != "merge":
                pend = None
            else:
                # #27：网页决策编号与条目内容不同源 → 不消费（不 pop），出锁后清表 + 归位
                mm, cid = _pending_key_mismatch(pend, rid)
                if not mm:
                    state["pending_confirm"].pop(rid, None)
        # 决策已消费（无论何种出口）：先清表，后续分支按需重新登记（幂等，失败不阻断）
        merge_conflict_clear(rid, "reconcile 消费")
        if mm:
            _pending_rehang(rid, pend, cid, "网页 merge 决策", question=pend.get("question") or "")
            continue
        req = all_reqs.get(int(rid))
        if pend is None:
            log.info(f"需求 #{rid} 网页端 merge 决策无待确认 pending（可能已由飞书 y/n 消费），表记录已清")
            post_dispatcher_event(int(rid),
                                  "网页端 merge 决策无待确认 pending（可能已由飞书回复处理），表记录清理",
                                  kind="confirm")
            continue
        if req is None:
            log.info(f"需求 #{rid} 已不存在，merge 冲突表记录已清")
            continue
        if action == "takeover":
            append_summary(int(rid), "用户人工接管合并（网页端按钮），需求置为暂缓，dev 分支与 worktree 保留待人工处理。")
            set_status(int(rid), "later")
            post_dispatcher_event(int(rid), "用户在网页端人工接管合并，需求置为暂缓，分支/worktree 保留", kind="merge")
            notify(f"需求 #{rid} 已在网页端请求人工接管，置为暂缓；dev 分支与 worktree 保留，请人工处理。")
        else:
            # retry：对齐 on_message y 分支（#106 改动文件清单在合并前捕获）
            with state_lock:
                rec = state.get("worktrees", {}).get(rid)
            touched = bool(rec) and _touches_dispatcher(req, rec["branch"])
            merge_files = (_collect_merge_changed_paths(req, rec["branch"]) if rec
                           and PROJECTS.get(req.get("project") or "", {}).get("auto_restart_after_merge")
                           else None)
            ok, failed = merge_dev_branches(req)
            if ok:
                cleanup_dev_worktrees(rid, delete_branch=True)
                restart_failed, restart_ok, restart_warn = _maybe_auto_restart_after_merge(
                    req, merge_files, int(rid))
                _set_done(req, dispatcher_touched=touched, restart_failed=restart_failed,
                          restart_ok=restart_ok, restart_warn=restart_warn)
                notify(f"需求 #{rid} 已在网页端重试合并成功，已完成。")
                post_dispatcher_event(int(rid), "网页端重试合并成功，清理 worktree，需求完成", kind="merge")
            else:
                entry = {"req": req, "question": pend["question"], "kind": "merge"}
                with state_lock:
                    ok = _put_pending_confirm(rid, entry)
                save_state()
                if not ok:
                    _pending_source_mismatch_note(rid, entry, "网页重试仍冲突重挂")
                guard = _merge_guard_blockers(req, failed)
                fail_text = (f"被主干守卫拦下（{_branch_guard_text(guard)}），未执行合并"
                             if guard else _merge_fail_text(failed, _merge_dirty_blockers(req, failed)))
                notify(f"需求 #{rid} 已在网页端重试合并但{fail_text}，"
                       "请继续处理（解决后再次点击「重试合并」或回复 y 重试）。")
                post_dispatcher_event(int(rid),
                                      f"网页端重试合并{fail_text}，按钮重置等待继续处理",
                                      kind="merge")
                merge_conflict_write(rid)  # 重置按钮等待再次点击（失败降级=按钮消失走飞书通道）
    save_state()


def confirm_decision_clear(req_id, reason=""):
    """#400 确认决策表清除（消费成功/幽灵决策清理；幂等，失败不阻断，降级仅日志）"""
    try:
        api_board("DELETE", f"/requirements/{req_id}/confirmations")
    except Exception as e:
        log.warning(f"需求 #{req_id} 确认决策表清除失败（{reason}）: {e}")


def reconcile_confirmations():
    """#400 网页确认动作对账：GET /api/confirmations 全量 + 本地过滤 action 非空（#386 先例）。
    以 pending_confirm 为权威消费（与 on_message 同 state_lock 互斥，先 pop 者执行）；
    kind 不匹配 / 无 pending = 已由飞书 y/n 并发消费的幽灵决策 → 清表行 + 事件留痕，不执行动作。
    语义与飞书 y/n 对等（对等复刻 on_message 对应分支，on_message 零改动）；
    confirm_rounds 单调计数不清零（无复位点）。"""
    try:
        confirms = api_board("GET", "/confirmations") or []
    except Exception as e:
        log.warning(f"确认对账拉取 confirmations 失败（本轮跳过）: {e}")
        return
    for c in confirms:
        action = c.get("action")
        kind = c.get("kind") or "confirm"
        if action not in WEB_CONFIRM_ACTIONS.get(kind, ()):
            continue  # action 为空 / 不在白名单 = 等待用户操作或非本通道决策
        rid = str(c["req_id"])
        mm = False
        cid = None
        with state_lock:
            pend = state["pending_confirm"].get(rid)
            if pend is None or (pend.get("kind") or "confirm") != kind:
                pend = None
            else:
                # #27：网页决策编号与条目内容不同源 → 不消费（不 pop），出锁后清表 + 归位
                mm, cid = _pending_key_mismatch(pend, rid)
                if not mm:
                    state["pending_confirm"].pop(rid, None)
        # 采取「先消费动作、成功后清表」：动作执行异常时恢复 pending + 保留表行可重试
        # （fail-closed，不静默丢决策；对齐 tech 用例 17）
        if mm:
            confirm_decision_clear(rid, "待确认条目键与内容不同源")
            _pending_rehang(rid, pend, cid, "网页确认动作", question=pend.get("question") or "")
            continue
        req = fetch_req(int(rid))
        if pend is None:
            confirm_decision_clear(rid, "reconcile 幽灵清理")
            log.info(f"需求 #{rid} 网页端确认决策无待确认 pending（可能已由飞书 y/n 消费），记录已清理")
            post_dispatcher_event(int(rid),
                                  "网页端确认决策无待确认 pending（可能已由飞书回复处理），记录清理",
                                  kind="confirm")
            continue
        if req is None:
            confirm_decision_clear(rid, "reconcile 残留清理")
            log.info(f"需求 #{rid} 已不存在，确认决策记录已清")
            continue
        if req.get("status") in ("done", "dropped"):
            # #25：卡已进入终态（人工置 done/dropped）——残留 action 不得执行（防 reject/
            # stop_later 把终态卡改回 open/later）；走同一终态清账单点后跳过本次 action
            _purge_ghost_confirm(rid, "reconcile 终态残留清理")
            log.info(f"需求 #{rid} 已终态，确认决策不执行并清理")
            continue
        question = pend.get("question") or ""
        reason = c.get("reason") or ""   # #415 网页弹窗反馈意见（可选，转开发时注入 dev 提示词）
        try:
            _execute_confirm_action(rid, req, pend, action, kind, question, reason)
        except Exception as e:
            log.error(f"需求 #{rid} 网页确认动作 {action} 执行异常（{e}），恢复 pending 保留决策待重试")
            with state_lock:
                ok = _put_pending_confirm(rid, pend)
            save_state()
            if not ok:
                _pending_source_mismatch_note(rid, pend, "网页确认动作异常恢复")
            post_dispatcher_event(int(rid),
                                  f"网页确认动作执行异常（{action}: {e}），决策保留待重试",
                                  kind="confirm")
            continue
        confirm_decision_clear(rid, "reconcile 消费")
    save_state()


def _execute_confirm_action(rid, req, pend, action, kind, question, reason=""):
    """#400 网页确认动作执行（对等复刻 on_message 对应分支语义；on_message 零改动）。
    已由 reconcile_confirmations 先 pop 认领；异常上抛由其恢复 pending + 保留表行重试。
    reason：#415 网页转开发弹窗填写的反馈意见（可选，空则不改变原默认原因）。"""
    if action == "proceed":
        if kind == "confirm" and pend.get("review_index") is None and not pend.get("flow_id"):
            answer = reason or "同意继续"
            append_summary(int(rid), confirm_answer_summary(question, answer))
            post_dispatcher_event(int(rid), f"用户网页确认继续：{answer}", kind="confirm")
            notify(f"需求 #{rid} 已收到继续确认，{ROLE_NAMES.get(role_for(req), role_for(req)) or '当前角色'}阶段继续执行。")
            spawn_agent(req, extra=f"用户对之前的问题「{question}」的答复：同意。{answer}", resume=True)
        return
    if action == "stop_later":
        # 新增明示出口（网页专属超限按钮）：转暂缓 + summary 留痕；非飞书 n 的对等复刻
        # （飞书 n 普通确认语义=退 open，n+later 前缀仅 confirm_override 存在）
        append_summary(int(rid), f"用户停止追问，转暂缓。（问题：{question}）")
        set_status(int(rid), "later")
        notify(f"需求 #{rid} 已停止追问并置为暂缓，请人工处理。")
        post_dispatcher_event(int(rid), "用户在网页端停止追问，需求置为暂缓", kind="confirm")
        return
    if action == "pass":
        if kind == "confirm" and pend.get("review_index") is not None:
            node = current_node(req)
            if node is not None and (node.get("node_type") or "") == "review":
                with state_lock:
                    state.get("flow_review_rounds", {}).get(rid, {}).pop(
                        str(pend.get("review_index")), None
                    )
                _fid, nodes, idx = flow_snapshot(req)
                prev = nodes[idx - 1] if nodes and idx > 0 else None
                _clear_review_escalation(
                    req["id"], str(pend.get("review_index")), prev.get("role") if prev else None
                )
                save_state()
                append_summary(int(rid), f"人工裁决放行（{node_display(node)}）："
                                         f"{reason or '同意'}。")
                post_dispatcher_event(
                    int(rid), f"人工裁决放行「{node_display(node)}」，进入下一节点", kind="phase"
                )
                _advance_after_node(req, node)
            else:
                notify(f"需求 #{rid} 当前节点已变化，未执行放行，请在看板确认现状。")
            return
        if kind == "review_gate":
            # #337：审查发现问题，人工确认仍合并——红字留档后放行 finalize_done
            with state_lock:
                pmr = state.get("pending_merge_review", {}).pop(str(req["id"]), None)
            save_state()
            append_summary(int(rid), f"⚠️ 代码审查发现问题，人工确认仍合并：{question}")
            post_dispatcher_event(int(rid), "审查发现问题，人工确认仍合并（发现已留档）", kind="merge")
            finalize_done(req, summary=(pmr or {}).get("summary"))
            notify(f"需求 #{rid} 审查发现的问题已红字留档，人工确认放行合并。")
        elif kind == "confirm_override":
            # #399：人工确认豁免——红字留档后 finalize_done 统一收口
            append_summary(int(rid), f"⚠️ 机制校验未过，人工确认豁免：{question}")
            if pend.get("todos") is not None:
                with state_lock:
                    state.setdefault("req_todos", {})[str(req["id"])] = pend.get("todos")
                save_state()
            post_dispatcher_event(int(rid), "收尾校验未过，人工确认豁免收口需求", kind="merge")
            finalize_done(req, summary=pend.get("summary"))
            notify(f"需求 #{rid} 收尾校验未过，经人工确认豁免，需求已完成。")
        return
    if action == "rework":
        if kind == "confirm" and pend.get("review_index") is not None:
            node = current_node(req)
            _fid, nodes, idx = flow_snapshot(req)
            prev = nodes[idx - 1] if nodes and idx > 0 else None
            if node is not None and (node.get("node_type") or "") == "review" and prev:
                set_req(rid, {"node_index": idx - 1})
                fresh = fetch_req(req["id"]) or req
                cur = ((state.get("flow_review_rounds") or {}).get(rid, {})
                       or {}).get(str(pend.get("review_index")), 0)
                append_summary(
                    int(rid), f"人工裁决打回「{node_display(prev)}」重做："
                              f"{reason or '未说明'}"
                )
                post_dispatcher_event(
                    int(rid), f"人工裁决打回「{node_display(prev)}」再修一轮", kind="phase"
                )
                notify(f"需求 #{rid} 已按人工裁决打回「{node_display(prev)}」重做。")
                _spawn_safe(
                    fresh,
                    prev.get("role"),
                    extra=_review_bounce_extra(
                        fresh, node, reason or "（网页端打回）", cur or None
                    ),
                    resume=True,
                )
            else:
                set_status(int(rid), "later")
                notify(f"需求 #{rid} 当前节点已变化，已置为暂缓等待人工处理。")
            return
        if kind == "review_gate":
            # #411：审查发现问题，人工转开发——等效飞书 n <id> 转开发（共用 _review_gate_rework）
            # #415：网页弹窗填写了意见则原样注入；未填写保持默认「（网页端转开发）」
            _review_gate_rework(str(req["id"]), req, question, reason or "（网页端转开发）")
        return
    if action == "reject":
        if kind == "review_gate":
            # #337：审查发现问题，人工否决合并——转暂缓保留分支/worktree
            with state_lock:
                state.get("pending_merge_review", {}).pop(str(req["id"]), None)
            append_summary(int(rid), f"代码审查发现问题（{question}），人工否决合并；"
                                     "dev 分支与 worktree 保留待人工处理。")
            set_status(int(rid), "later")
            notify(f"需求 #{rid} 审查发现问题，已否决合并并置为暂缓，dev 分支与 worktree 保留，请人工处理。")
            post_dispatcher_event(int(rid), "审查发现问题，人工否决合并，需求转暂缓（分支/worktree 保留）",
                                  kind="merge")
        elif kind == "confirm_override":
            # #399：人工否决豁免——退 open（phase 置空 + 清 retries）重新分析
            set_req(int(rid), {"status": "open", "phase": None})
            clear_retries(str(req["id"]))
            clear_flow_rounds(str(req["id"]))
            append_summary(int(rid), f"用户否决豁免：未说明（校验未过：{question}），退回待开发重新分析。")
            notify(f"需求 #{rid} 已否决豁免并退回待开发，将重新分析。")
            post_dispatcher_event(int(rid), "用户否决豁免，退回待开发", kind="confirm")
        return
    if action == "reject_later":
        # 仅 confirm_override 白名单：等效飞书 n+later 前缀 → 转 later 人工收口
        set_status(int(rid), "later")
        append_summary(int(rid), f"人工否决豁免（later）：未说明（校验未过：{question}），"
                                 "dev 分支与 worktree 保留待人工处理。")
        notify(f"需求 #{rid} 已否决豁免并置为暂缓，dev 分支与 worktree 保留，请人工处理。")
        post_dispatcher_event(int(rid), "人工否决豁免（later），需求转暂缓（分支/worktree 保留）",
                              kind="confirm")

def _mark_running_rows_idle(req, role, rows=None):
    """#388 采纳后收尾：本需求 role 匹配的 running 会话行 → idle（驱动进程已随旧调度器
    死亡、结果已消费）。rows 可选（调用方已拉取的 agent-sessions 列表，避免重复 GET）；
    失败仅降级（日志），不阻断采纳结果。"""
    if rows is None:
        try:
            rows = api_board("GET", "/agent-sessions")
        except Exception:
            return
    for r in rows:
        if (r.get("status") == "running" and str(r.get("req_id")) == str(req["id"])
                and (r.get("role") or "") == role):
            # #421：统一入口（置 idle + 清 turn_step）；失败由 _close_session_row 内部告警降级
            _close_session_row(req, role, r.get("id"), "idle")


# #388（审查问题 A）：本次启动已采纳合法结果的卡——reconcile_startup 无条件把 doing
# 退 open，会把免确认采纳后保持 doing 的卡闪变成 open（验收标准 1：消除 doing/open 闪变）
_adopted_rids = set()


def reconcile_adopt_results():
    """#388 重启对账「采纳合法结果」：doing 卡 + 产物 result.json 存在、JSON 合法且
    status ∈ {done, blocked, need_confirm}（收尾契约值）→ 直接走 handle_result 采纳推进
    （analysis → route / 免确认直接进 dev），不退回 open、不重跑；缺失/非法/非收尾状态
    不动（由 reconcile_startup 退 open 重跑，行为不变）。
    - 顺序依赖：必须先于 reconcile_startup（R1）——先退 open 的话重捞 prompt 会指示
      「删除残留 result.json」，合法结果被主动清理后重跑整轮；
    - 采纳归因：returncode=0、timed_out=False（正常完成语义）；started_at 现场不可得
      （post_run_record 未落库），传 time.time() 兜底（dev has_code_changes 已废弃 since）；
    - 幂等：handle_result 解析成功后 unlink result.json，天然防重复采纳；
    - 采纳成功把本需求 role 的 running 会话行标 idle（中间栏无僵尸残留）；
    - 角色判定：#388（审查问题 3）优先取该需求 running 会话行的 role（auto 通道
      review 会话残留时 role_for 恒返回 dev 会错配，行 role=review 时正确归因
      finalize_done），无 running 行时 role_for 兜底；
    - 看板不可达整体跳过（仿 reconcile_startup）；单卡失败降级继续。"""
    session_rows = None  # 懒加载：首个可采纳卡才 GET agent-sessions（复用给行置 idle）
    for project in PROJECTS:
        try:
            for r in get_doing_reqs(project):
                rid = str(r["id"])
                if rid in state["pending_confirm"] or \
                   rid in state["pending_route"] or \
                   rid in state["pending_review"]:
                    continue
                rf = result_file_path(r)
                if not rf.exists():
                    continue
                try:
                    parsed = json.loads(rf.read_text())
                except Exception as e:
                    log.warning(f"启动对账：需求 #{r['id']} 结果文件非法 JSON（{rf}: {e}），"
                                "跳过采纳（现场保留，交 reconcile_startup 退 open 重跑）")
                    continue
                if (parsed or {}).get("status") not in ("done", "blocked", "need_confirm"):
                    log.warning(f"启动对账：需求 #{r['id']} 结果 status="
                                f"{(parsed or {}).get('status')!r} 非收尾契约值，跳过采纳")
                    continue
                if session_rows is None:
                    try:
                        session_rows = api_board("GET", "/agent-sessions")
                    except Exception:
                        session_rows = []
                role = None
                for sr in session_rows:
                    if (sr.get("status") == "running" and str(sr.get("req_id")) == rid
                            and (sr.get("role") or "")):
                        role = sr.get("role")
                        break
                role = role or role_for(r)
                # #419：status=done 未必过 handle_result 的 valid_done 前置——按**节点类型**预检：
                # code 节点要求代码改动、artifact/review 节点要求产物存在（前置槽位同 artifact）；
                # 预检不合格不采纳（交 reconcile_startup 退 open 走既有重捞），否则 adopt 会把
                # handle_result 失败通道的同步 sleep（30-120s）+ 无主 resume spawn 带进启动序列
                _node = current_node(r)
                _ntype = (_node or {}).get("node_type") or "artifact"
                if (parsed or {}).get("status") == "done":
                    if _ntype == "code":
                        if not has_code_changes(r, time.time()):
                            log.warning(f"启动对账：需求 #{r['id']} {role} 结果 status=done 但 dev 分支无代码变更，"
                                        "不采纳（交 reconcile_startup 退 open 重跑）")
                            continue
                    else:
                        ok, why = artifact_ok(r, role)
                        if not ok:
                            log.warning(f"启动对账：需求 #{r['id']} {role} 结果 status=done 但产物校验失败"
                                        f"（{why}），不采纳（交 reconcile_startup 退 open 重跑）")
                            continue
                log.warning(f"启动对账：需求 #{r['id']} {role} 上次结果合法（{rf.name}），重启收尾采纳，不重跑")
                try:
                    handle_result(r, role, 0, False, time.time())
                    _mark_running_rows_idle(r, role, session_rows)
                    _adopted_rids.add(rid)  # 记录已采纳：reconcile_startup 不再把该卡退 open
                    post_dispatcher_event(int(r["id"]),
                                          f"调度器重启，采纳上次合法收尾结果（{ROLE_NAMES.get(role, role)}，不重跑）",
                                          kind="phase")
                except Exception:
                    # #412（B）：启动对账 handle_result 异常留痕（旧实现 log.warning 无 traceback）
                    log.exception(f"启动对账：需求 #{r['id']} 结果采纳失败（跳过）")
        except Exception as e:
            log.warning(f"启动对账采纳项目 {project} 失败（跳过）: {e}")


def reconcile_startup():
    """重启时把没有待确认的 doing 卡片退回 open（重启后内存态运行记录必然丢失，
    重捞按卡上快照续跑）；退回后补写调度事件，让「调度历史回放」时间线有痕（#299）。
    #388：合法结果先经 reconcile_adopt_results 采纳（本函数之前调用），此处只处理
    缺失/非法结果的 doing 卡（现状行为不变）。
    #419：先处理「卡上快照当前节点为 terminal」的 doing/open 卡——直接执行收尾（幂等，已 done 跳过），
    这类卡**不退回 open**（terminal 无角色可拉起，退回必卡死）；merge_resolve 在途的卡跳过
    （由 merge_resolve 链自身收尾）。覆盖「回填产出的 open+terminal（旧 review@accept 卡）」与
    「重启中断时卡在 doing+terminal」两种死角。
    看板暂时不可达（如与看板重启窗口重叠）时跳过，避免启动即崩。"""
    for project in PROJECTS:
        try:
            rows = list(get_doing_reqs(project) or [])
            try:
                rows += list(get_open_reqs(project) or [])
            except Exception as e:
                log.warning(f"启动 reconcile 拉取 {project} open 卡失败（跳过 open 扫描）: {e}")
            for r in rows:
                rid = str(r["id"])
                if rid in state["pending_confirm"] or \
                   rid in state["pending_route"] or \
                   rid in state["pending_review"]:
                    continue
                node = current_node(r)
                if node is not None and (node.get("node_type") or "") == "terminal":
                    if rid in state.get("merge_resolve", {}):
                        log.info(f"启动 reconcile：需求 #{rid} 处于收尾节点且 merge_resolve 在途，跳过（由 merge_resolve 链收尾）")
                        continue
                    log.warning(f"启动 reconcile：需求 #{rid} [{project}] 处于收尾节点（terminal），直接执行收尾")
                    try:
                        _run_terminal(r)
                    except Exception as e:
                        log.error(f"启动 reconcile：需求 #{rid} 收尾执行异常（跳过）: {e}")
                    _adopted_rids.add(rid)
                    continue
                if r.get("status") != "doing" or rid in _adopted_rids:
                    continue
                log.warning(f"启动 reconcile：需求 #{r['id']} [{project}] 调度器重启导致运行记录丢失，退回 open 待重捞续跑")
                set_status(r["id"], "open")
                post_dispatcher_event(int(r["id"]), "调度器重启，进行中需求退回 open（1 分钟后自动重捞续跑）", kind="phase")
        except Exception as e:
            log.warning(f"启动 reconcile 项目 {project} 失败（跳过）: {e}")


def _session_held(row, sessions):
    """#374/#388 持有者判定：state.sessions 中「role:req_id」键（含旧格式「req_id」键）
    存在且 session_id_of(entry) 与行 session_id 匹配 → 有持有者（resume 路径）。"""
    sid = row.get("session_id")
    req_id = row.get("req_id")
    role = row.get("role") or ""
    for key in (session_key({"id": req_id}, role), str(req_id)):
        entry = sessions.get(key)
        if entry is not None and session_id_of(entry) == sid:
            return True
    return False


def _proc_dead_for(req_id):
    """#388 进程确死判定：active_runs 无该需求记录（重启后内存态丢失）或记录中
    proc.poll() 非 None → 确死；正常运行中的会话（poll() is None）绝不误判。"""
    with active_lock:
        rec = active_runs.get(str(req_id))
    if rec is None:
        return True
    poll = getattr(rec.get("proc"), "poll", None)
    return not (callable(poll) and poll() is None)


def reconcile_orphan_sessions():
    """#374 启动时清算孤儿 running 会话：调度器重启被杀进程的 agent_sessions 记录永久停在
    running（僵尸），中间栏永远显示运行中。清理范围（#388 扩展）：
    - 无持有者：键不存在、或键存在但 session_id 不匹配（被覆盖键，如 review:370 换新
      会话后旧行无人认领）→ 标 interrupted（中断，人工可继续）+ phase 事件留痕；
    - 有持有者但进程确死（#388）：驱动线程已随旧进程消失，行属僵尸 → 同样标
      interrupted（与运行期 sweep 兜底二分：启动序列一次清干净；运行期 sweep 只收
      active_runs 有记录且 poll 非 None 的死进程，本函数不重复运行）；
    - 有持有者且进程存活（active_runs 有记录且 poll() is None，正常运行/重试/resume）
      → 不动（绝不误伤）；
    - session_id 为空的异常 running 行无法比对持有者，跳过不误清（低概率数据异常）；
    - 看板不可达整体跳过（不崩不误写，仿 reconcile_startup）；单行失败降级继续。
    仅 UPDATE status 字段 + 追加事件，不删行、不动 session_id（可 resume 续上下文）。"""
    try:
        rows = api_board("GET", "/agent-sessions")
    except Exception as e:
        log.warning(f"拉取 agent_sessions 失败（孤儿会话清算跳过）: {e}")
        return
    sessions = state.get("sessions") or {}
    for r in rows:
        if r.get("status") != "running":
            continue
        sid = r.get("session_id")
        if not sid:
            continue
        req_id = r.get("req_id")
        role = r.get("role") or ""
        if _session_held(r, sessions):
            if not _proc_dead_for(req_id):
                continue  # 合法持有者且进程存活（正常运行/重试/resume），不动
            log.warning(f"孤儿会话清算：req #{req_id} {role} session {sid} 有持有者但进程确死，标记 interrupted（#388）")
        try:
            # #421：统一入口（置 interrupted + 清 turn_step）
            _close_session_row({"id": req_id}, role, r.get("id"), "interrupted")
            log.warning(f"孤儿会话清算：req #{req_id} {role} session {sid} 标记 interrupted（调度器重启）")
            post_dispatcher_event(int(req_id), f"调度器重启，会话 {sid} 中断清理", kind="phase")
        except Exception as e:
            log.warning(f"孤儿会话清算失败 req #{req_id} (row={r.get('id')}): {e}")


def manual_commands_poller():
    """#284 人工续跑命令消费线程：独立短间隔轮询，降低「发消息→被消费」的排队延迟。
    与主循环解耦（主循环仍负责捞需求/决策对账/心跳，15s 间隔不变）；消费逻辑本身
    有 active_lock 防并发拉起同一需求的会话，双线程场景安全。"""
    log.info(f"人工命令消费线程启动（间隔 {MANUAL_POLL_INTERVAL}s）")
    while True:
        try:
            consume_manual_commands()
        except Exception as e:
            log.error(f"人工命令消费异常: {e}")
        time.sleep(MANUAL_POLL_INTERVAL)


def _reap_held_once():
    """回收一轮：返回超时回收的保持进程数（供 held_reaper 循环与单元测试调用）。
    #412（A1）：held_lock 内仅快照+pop，_close_held 移锁外——旧实现锁内 _close_held
    （session.close → MCP 关闭无超时）一旦阻塞即持 held_lock 永久卡住，
    _hold_session 全部阻塞（#410 高嫌疑点 A）。pop 后 key 已出池，无并行 close。"""
    now = time.time()
    stale = []
    with held_lock:
        for k, h in held_sessions.items():
            if now - h["kept_at"] > HELD_TTL_SEC:
                stale.append((k, h))
        for k, h in stale:
            held_sessions.pop(k, None)
            state.get("held_procs", {}).pop(k, None)
            log.info(f"保持进程闲置超时回收 (asid={k}, req={h['req_id']}, pid={h['session'].proc.pid})")
    for k, h in stale:
        try:
            _close_held(h)
        except Exception as e:
            # #412（T7）：单项关闭异常不中断其余回收（close 兜底后仍异常时留痕）
            log.warning(f"保持进程 {k} 关闭异常（继续回收其余）: {e}")
    if stale:
        save_state()
    return len(stale)


def held_reaper():
    """#284 保持进程回收：闲置超时（HELD_TTL_SEC）后优雅关闭，防止进程长期占用资源。
    仅回收 idle 保持进程；复用中/运行中的进程在 active_runs（不在保持池），不受影响。"""
    log.info(f"保持进程回收线程启动（闲置 TTL {HELD_TTL_SEC}s）")
    while True:
        try:
            _reap_held_once()
        except Exception as e:
            log.error(f"保持进程回收异常: {e}")
        time.sleep(30)


def cleanup_orphan_held():
    """调度器启动清理：#284 保持进程随上次调度器死亡可能残留为孤儿（子进程不随父退出）。
    pid 镜像在 state["held_procs"]；精确 pid + 命令行含 kimi 校验（防 pid 复用误杀）后 terminate。"""
    procs = dict(state.get("held_procs") or {})
    for asid, pid in procs.items():
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=5).stdout
            if "kimi" in out:
                os.kill(pid, signal.SIGTERM)
                log.info(f"清理孤儿保持进程 pid={pid} (asid={asid})")
        except Exception as e:
            log.warning(f"孤儿保持进程清理失败 pid={pid} (asid={asid}): {e}")
    with state_lock:
        state["held_procs"] = {}
    save_state()


def recover_manual_commands():
    """启动恢复：上次运行中（running）的人工续跑命令随调度器进程中断，标记 failed 供人工重发。"""
    try:
        rows = api_board("GET", "/manual-commands?status=running")
    except Exception as e:
        log.warning(f"拉取 running 人工命令失败（跳过）: {e}")
        return
    for r in rows:
        try:
            api_board("PATCH", f"/manual-commands/{r['id']}",
                      {"status": "failed", "error": "调度器重启，命令中断"})
        except Exception as e:
            log.warning(f"恢复人工命令 #{r['id']} 失败: {e}")


def consume_manual_commands():
    """#273 人工续跑：轮询消费 pending 命令（看板聊天框发消息 / 新建会话首条消息）。
    同一需求已有会话在跑（active_runs）时留待下轮；命令先 claim 为 running，再起线程执行。"""
    try:
        cmds = api_board("GET", "/manual-commands?status=pending")
    except Exception as e:
        log.warning(f"拉取人工续跑命令失败: {e}")
        return
    for cmd in cmds:
        req_id = str(cmd["req_id"])
        try:
            req = fetch_req(req_id)
            if req is None:
                api_board("PATCH", f"/manual-commands/{cmd['id']}",
                          {"status": "failed", "error": "需求不存在"})
                continue
            with active_lock:
                if req_id in active_runs:
                    continue  # 已有会话在跑，下轮再消费
            api_board("PATCH", f"/manual-commands/{cmd['id']}", {"status": "running"})
            role = cmd.get("role") or role_for(req)
            # #284 现场修复：route/review 是等待人工决策的中间态，续跑只执行会话、不置 doing——
            # 否则决策对账会把「状态变 doing」误判为网页端确认/评审通过（accept 阶段会误置 done）
            if req.get("status") not in ("route", "review"):
                fields = {"status": "doing"}
                if req.get("mode") == "semi" and role in SEMI_PHASES:
                    fields["phase"] = role
                set_req(req["id"], fields)
            # #279：传 set_req 之后的最新需求（phase 可能已按 role 更新），
            # 供 run_agent 的角色守卫按当前阶段校验
            fresh = fetch_req(req["id"]) or req
            log.info(f"需求 #{req_id} 人工续跑（role={role}, cmd={cmd['id']}）: {cmd.get('text')}")
            # 网页端没有「普通提问」的 y 按钮（WEB_CONFIRM_ACTIONS 只给 stop_later），聊天框是
            # 网页端唯一答复入口 → 该卡有待答的普通提问时，把这条消息同样留档到 summary
            # （口径与飞书 y 一致；机器生成的两类提问——前置流程确认 / 评审超限裁决——有自己的
            # 留档分支，跳过，避免把问题全文当澄清写进开发总结）
            pend = state["pending_confirm"].get(req_id) or {}
            if pend and not pend.get("flow_id") and pend.get("review_index") is None:
                append_summary(req["id"], confirm_answer_summary(pend.get("question"), cmd.get("text")))
            post_dispatcher_event(req["id"], f"人工继续会话：{cmd.get('text')}", kind="confirm")
            threading.Thread(target=_run_manual_command,
                             args=(cmd, fresh, role, cmd.get("text") or ""),
                             daemon=True).start()
        except Exception as e:
            log.error(f"人工续跑命令 #{cmd.get('id')} 处理失败: {e}")
            try:
                api_board("PATCH", f"/manual-commands/{cmd['id']}",
                          {"status": "failed", "error": str(e)[:2000]})
            except Exception:
                pass


def _run_manual_command(cmd, req, role, text):
    """人工续跑命令执行线程：取会话行，resume 旧会话或新建会话，注入用户消息跑一轮。"""
    cmd_id = cmd["id"]
    asid = cmd.get("agent_session_id")
    try:
        rows = api_board("GET", f"/requirements/{req['id']}/sessions")
        row = next((r for r in rows if r["id"] == asid), None)
        if row is None:
            api_board("PATCH", f"/manual-commands/{cmd_id}",
                      {"status": "failed", "error": "会话不存在"})
            return
        resume_sid = row.get("session_id") or None
        prompt = build_prompt(req, role) if not resume_sid else "人工继续会话"
        # #284 进程保持复用：同会话行有存活保持进程 → 直接复用（省拉起+resume），
        # 进程已死（poll 非 None）则丢弃保持项走正常拉起
        # #412（A2）：held_lock 内仅旧快照+pop，_close_held 移锁外（与 _reap_held_once 同型）
        held = None
        dead_held = None
        with held_lock:
            h = held_sessions.get(asid)
            if h is not None:
                if h["session"].proc.poll() is None:
                    held = h
                    held_sessions.pop(asid, None)
                    state.get("held_procs", {}).pop(asid, None)
                else:
                    held_sessions.pop(asid, None)
                    state.get("held_procs", {}).pop(asid, None)
                    dead_held = h
        if dead_held is not None:
            _close_held(dead_held)
        if held is not None:
            log.info(f"需求 #{req['id']} 人工续跑复用保持进程（cmd={cmd_id}, asid={asid}）")
        ok = run_agent(req, role, prompt, started_at=time.time(), extra="",
                       agent_session_id=asid, user_text=text, resume_session_id=resume_sid,
                       hold=True, held=held)
        if ok is False:
            # #279：角色守卫拦截（角色与需求当前阶段不匹配），命令失败不置 done；
            # 仅显式 False 判定（旧调用/旧 mock 无返回值视为成功路径，兼容）
            api_board("PATCH", f"/manual-commands/{cmd_id}",
                      {"status": "failed", "error": "角色与需求当前阶段不匹配，拒绝拉起会话"})
            return
        api_board("PATCH", f"/manual-commands/{cmd_id}", {"status": "done"})
        # #273 人工续跑成功（进入 review/done）：清除毒会话标记，自动路径与展示同步
        fresh = fetch_req(req["id"])
        if fresh and fresh.get("status") in ("review", "done"):
            try:
                api_board("POST", "/agent-sessions", {"id": asid, "poisoned": 0})
            except Exception:
                pass
            with state_lock:
                entry = state.get("sessions", {}).get(session_key(req, role))
                if isinstance(entry, dict):
                    entry["poisoned"] = False
            save_state()
    except Exception as e:
        log.warning(f"需求 #{req['id']} 人工续跑异常: {e}")
        try:
            api_board("PATCH", f"/manual-commands/{cmd_id}",
                      {"status": "failed", "error": str(e)[:2000]})
        except Exception:
            pass


def load_heartbeat():
    """读心跳 dict；文件缺失/解析失败返回 None（#373：读 last_shutdown 扩展字段入口）"""
    try:
        return json.loads(HEARTBEAT_FILE.read_text())
    except Exception:
        return None

_heartbeat_lock = threading.Lock()   # #23：心跳读-改-写串行化（ws 回调线程与主循环并发写）


_PROD_HEARTBEAT_FILE = REPO_DIR / "data" / "dispatcher_heartbeat.json"


def _heartbeat_write_locked(hb):
    """写心跳（调用方须已持 _heartbeat_lock）：临时文件 + 原子替换。
    评审建议修复：`os.replace` 失败时清掉 `.tmp`（否则残留半成品文件，异常退出窗口内心跳不可读）。
    测试隔离防线：测试进程（unittest 已加载）写「生产心跳路径」一律拒绝——防止测试里未 patch
    HEARTBEAT_FILE 的 main()/init_feishu 分支把看板飞书状态写成「显式停用」。"""
    if in_test_env() and Path(HEARTBEAT_FILE) == _PROD_HEARTBEAT_FILE:
        log.warning("测试进程拒绝写生产心跳文件（测试隔离防线；请 patch dispatcher.HEARTBEAT_FILE）: %s",
                    HEARTBEAT_FILE)
        return
    tmp = HEARTBEAT_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(hb))
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def write_heartbeat(extra=None, update_poll=True, reset_feishu=False):
    """每轮轮询结束写心跳（原子替换，供看板页健康角标判断调度器是否存活/卡住）。
    #373：merge 旧心跳保留 last_shutdown（信号退出记录），extra 可选合并写入。
    #23：读-改-写纳入进程内写锁；update_poll=False 用于「只改字段、不刷新轮询时间」的写入
    （退出记录等）——既有调用缺省行为逐字不变。
    #40：锁内校正 pid（心跳 pid 必须指向当前写下心跳的进程，旧进程残留值覆盖掉）；
    **不动 boot_at**（只有启动首写更新它，exec 自重启靠 boot_at 变化判定已发生）。
    reset_feishu=True（仅 main 启动首写）：把 feishu 整体换成 `{phase: starting, connected: false}`
    ——**禁止新进程在 init_feishu 之前带着上一进程的 connected=true 被看板读走**（随后 init_feishu
    写入真实状态）；last_poll_at 一并刷新：本进程已活着，避免「重启后到首轮轮询前」显示过期心跳。"""
    with _heartbeat_lock:
        hb = load_heartbeat() or {}
        if update_poll:
            hb["last_poll_at"] = time.time()
        if extra:
            hb.update(extra)
        if hb.get("pid") != os.getpid():
            hb["pid"] = os.getpid()
        if reset_feishu:
            hb["feishu"] = {"phase": "starting", "connected": False, "enabled": None,
                            "updated_at": time.time()}
        _heartbeat_write_locked(hb)


def _update_feishu_status(**updates):
    """#23 合并写心跳 feishu 子状态（与 write_heartbeat 同锁，防 ws 线程与主循环互相覆盖字段）。
    写失败只告警：绝不因心跳写入问题阻断长连接或主循环。"""
    try:
        with _heartbeat_lock:
            hb = load_heartbeat() or {}
            st = hb.get("feishu")
            if not isinstance(st, dict):
                st = {}
            st.update(updates)
            st["updated_at"] = time.time()
            hb["feishu"] = st
            _heartbeat_write_locked(hb)
    except Exception as e:  # noqa: BLE001
        log.warning(f"写飞书心跳状态失败: {e}")

# ---- #373 调度器重启原因可见化：SIGTERM/SIGINT 信号记录 + 约定式重启来源 ----
def _signal_name(signum):
    try:
        return signal.Signals(signum).name
    except Exception:
        return f"信号{signum}"

def _read_restart_source():
    """读重启来源标记文件**不删除**（{source, at} dict；缺失/解析失败/非 dict 返回 None）。
    #8：_shutdown_handler 需在 _record_shutdown 消费（弹出）标记前预读，据其判 exec 自重启。"""
    try:
        if not RESTART_SOURCE_FILE.exists():
            return None
        data = json.loads(RESTART_SOURCE_FILE.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _pop_restart_source():
    """读并删除重启来源标记文件（scripts/dispatcher-restart.sh / board restart_dispatcher 等约定入口写入）。
    返回 source 字符串；无标记/读失败返回 None。读完即删防残留误标（#373）。"""
    data = _read_restart_source()
    try:
        src = (data or {}).get("source")
        return src if isinstance(src, str) and src else None
    except Exception:
        return None
    finally:
        try:
            RESTART_SOURCE_FILE.unlink()
        except Exception:
            pass

def _restart_source_fresh(entry, ttl=RESTART_SOURCE_TTL_SEC):
    """来源标记是否新鲜可信（#8 exec 自重启第三道闸）：at 为数字且距今 ≤ TTL。
    缺失/非法/过期一律不新鲜（fail-safe：不 exec，按普通停止语义退出）。"""
    at = (entry or {}).get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    return (time.time() - at) <= ttl

def _exec_self_restart(source):
    """#8 run_local 直跑模式 exec 自重启：同进程槽位重载自身（PID 不变，run_local.sh 判活循环
    不触发「任一子进程退出即整组停止」；env 继承含 .env，cwd 不动；脚本用绝对路径防 cwd 差异）。
    先 flush 标准流再 exec（防日志丢失）。成功不返回；异常/意外返回 → log.error + 心跳
    restart_exec_failed 留痕，返回 False（调用方拒绝重启、绝不裸退出）。"""
    try:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        log.info(f"run_local 直跑模式：exec 自重启（来源 {source}，PID {os.getpid()} 不变）")
        os.execv(sys.executable, [sys.executable, str(REPO_DIR / "dispatcher.py")])
        reason = "os.execv 意外返回（未替换进程映像）"
    except Exception as e:  # noqa: BLE001 - handler 内绝不外抛，失败走拒绝重启路径
        reason = str(e)
    log.error(f"exec 自重启失败，拒绝重启（绝不裸退出）: {reason}")
    try:
        write_heartbeat({"restart_exec_failed": {"at": time.time(), "reason": reason[:300], "source": source}})
    except Exception as e:
        log.warning(f"写 restart_exec_failed 心跳标记失败: {e}")
    return False

# ==================== #380 done 流程自动重启调度器 ====================
def _queue_auto_restart(req_id):
    """#380：done 命中 dispatcher.py 时登记待自动重启（复用 state 持久化）。
    键：req_id(str) → {"at": 写入时间, "attempts": kill 尝试计数}；主循环见
    _maybe_auto_restart，重启成功由新进程 _verify_pending_restart 自证清标记。"""
    with state_lock:
        state.setdefault("pending_restart", {})[str(req_id)] = {"at": time.time(), "attempts": 0}
    save_state()

def _stray_acp_pids():
    """自动重启安全阀兜底（#380 方案 B）：pgrep -f "kimi acp" 命中外部存疑 kimi 进程 → 有则本轮不重启。
    排除：① board 起的助手进程（#107 建卡 / #302 需求助手：kimi-chat-acp 包装器及其 kimi 子进程，
    祖先链含 board.py 进程）；② 保持池 pid（held_procs：重启后由 cleanup_orphan_held 收编，
    与手动重启行为一致）。
    注意：pgrep 为启发式兜底（kimi 进程自改名 kimi-code 后 argv 无 "kimi acp" 字样），
    主安全阀是 active_runs；pgrep 不可用/失败时返回空（不拦截，防误判锁死）。"""
    try:
        out = subprocess.run(["pgrep", "-f", "kimi acp"], capture_output=True, text=True, timeout=5).stdout
        cand = {int(p) for p in out.split() if p.strip().isdigit()}
    except Exception:
        return []
    if not cand:
        return []
    try:
        board_pids = {int(p) for p in subprocess.run(
            ["pgrep", "-f", "board.py"], capture_output=True, text=True, timeout=5).stdout.split()}
    except Exception:
        board_pids = set()
    held = [int(v) for v in (state.get("held_procs") or {}).values() if str(v).isdigit()]
    try:
        out = subprocess.run(["ps", "ax", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
        parent = {}
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[0].isdigit():
                parent[int(parts[0])] = int(parts[1])
    except Exception:
        parent = {}

    def is_board_side(pid):
        """pid 的祖先链（限 15 层）命中 board.py 进程 → board 助手进程，排除"""
        seen, pp = set(), pid
        for _ in range(15):
            pp = parent.get(pp)
            if pp is None or pp in seen or pp == 1:
                return False
            seen.add(pp)
            if pp in board_pids:
                return True
        return False

    return sorted(p for p in cand if p not in held and not is_board_side(p))

def _fallback_manual_hint(req_id):
    """#380：自动重启多次未生效 → 转「需手动重启调度器」提醒兜底（summary + 时间线，不静默）。
    #8：run_local 直跑模式文案改「Ctrl-C 后重跑 run_local.sh」（直跑无 KeepAlive）。"""
    hint = DISPATCHER_RESTART_HINT_RUN_LOCAL if is_run_local_supervisor() else DISPATCHER_RESTART_HINT
    try:
        append_summary(req_id, f"⚠️ {hint}")
    except Exception as e:
        log.warning(f"需求 #{req_id} 自动重启兜底提醒写 summary 失败: {e}")
    try:
        post_dispatcher_event(req_id, f"调度器自动重启未能生效（已尝试 {MAX_AUTO_RESTART_ATTEMPTS - 1} 次 kill），"
                                      f"需手动重启：{hint}", kind="merge")
    except Exception as e:
        log.warning(f"需求 #{req_id} 自动重启兜底提醒事件上报失败: {e}")

def _maybe_auto_restart():
    """主循环每轮检查（#380）：pending_restart 非空且已写入超过 POLL_INTERVAL（确保 done 通知/
    合并/看板重启先完成），且无并行 agent（active_runs 空）、无外部存疑 kimi 进程 → 写来源标记
    （source=auto，复用 #373）→ kill -TERM 自身。launchd 模式由 KeepAlive 拉起、新进程自证；
    #8 run_local 直跑模式由信号 handler 内 exec 自重启（整组不停止）。
    每次发起 kill attempts+1；单需求 attempts 超 MAX_AUTO_RESTART_ATTEMPTS（第 N 轮判定放弃，
    即最多实际发起 N-1 次 kill）→ 放弃该需求并转「需手动重启调度器」提醒兜底（不静默）；
    有并行/存疑进程仅顺延不计数（等 agent 结束自动生效）。
    #8 直跑加固：标记写失败跳过本轮 kill（无标记的 SIGTERM 会被 handler 按停止语义退出、整组
    停止；该分支在 attempts 自增之前 return，本轮不计入尝试次数）；kill 异常清标记（防残留
    标记在 TTL 内被随后 Ctrl-C 的 kill 0 SIGTERM 命中而误触发 exec）。"""
    with state_lock:
        pending = dict(state.get("pending_restart") or {})
    if not pending:
        return
    if time.time() - min(v.get("at", 0) for v in pending.values()) < POLL_INTERVAL:
        return
    # 安全阀①：并行 agent 不打断（重启顺延，全部结束后下一轮自动生效）
    with active_lock:
        if active_runs:
            log.info(f"存在 {len(active_runs)} 个并行 agent，调度器自动重启顺延至其结束后")
            return
    # 安全阀②：外部存疑 kimi 进程兜底（排除 board 助手进程/保持池）
    stray = _stray_acp_pids()
    if stray:
        log.warning(f"发现外部 kimi acp 进程 {stray}，本轮跳过调度器自动重启")
        return
    run_local = is_run_local_supervisor()
    # 发起重启：先写来源标记（#373 机制，新进程/exec 后自证来源=auto）再 kill -TERM 自身
    try:
        RESTART_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESTART_SOURCE_FILE.write_text(json.dumps({"source": "auto", "at": round(time.time(), 3)}))
    except Exception as e:
        log.warning(f"写自动重启来源标记失败（重启后来源将显示未知）: {e}")
        if run_local:
            log.error("run_local 直跑模式：来源标记写失败，跳过本轮自动重启（不发 SIGTERM；下一轮重试）")
            return
    with state_lock:
        for rid in pending:
            rec = state["pending_restart"].get(rid)
            if rec is None:
                continue
            rec["attempts"] = rec.get("attempts", 0) + 1
        give_up = [rid for rid, rec in state["pending_restart"].items()
                   if rid in pending and rec.get("attempts", 0) >= MAX_AUTO_RESTART_ATTEMPTS]
        for rid in give_up:
            del state["pending_restart"][rid]
    save_state()
    for rid in give_up:
        _fallback_manual_hint(rid)
    with state_lock:
        still = state.get("pending_restart") or {}
    if not still:
        return
    if run_local:
        # #8：重启前一条事件（PRD §2「重启前后各记一条」；仅直跑分支，launchd 零新增事件）
        for rid in sorted(still):
            try:
                post_dispatcher_event(int(rid),
                                      "直跑模式：调度器将以 exec 自重启生效（不中断看板/整组存活）",
                                      kind="merge")
            except Exception as e:
                log.warning(f"需求 #{rid} 直跑自重启预告事件上报失败: {e}")
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        if run_local:
            log.info("自动重启：已发送 SIGTERM 到自身（来源 auto），run_local 直跑模式将由 exec 自重启")
        else:
            log.info("自动重启：已发送 SIGTERM 到自身（来源 auto），KeepAlive 将拉起新进程")
    except Exception as e:
        log.error(f"自动重启 kill 失败: {e}")
        if run_local:
            # #8：清标记（防残留标记在 TTL 内被随后 Ctrl-C 的 kill 0 SIGTERM 命中而误触发 exec）
            try:
                RESTART_SOURCE_FILE.unlink()
            except Exception:
                pass
            log.error("run_local 直跑模式：kill 失败，已清除来源标记（防劫持 Ctrl-C 停止语义）")

def _verify_pending_restart():
    """新进程启动自证（#380，main 在 load_state 后调用）：pending_restart 非空说明 done 流程
    登记了自动重启且本进程即重启结果 → 清标记（新进程已就位，配置已生效；保留标记会让主循环
    再次 kill 自身造成重启循环）+ 逐需求补发通知：
    - 心跳 last_shutdown.at >= 最早 pending 写入时间 → 上次确已优雅退出：来源 auto → 「调度器
      已自动重启，dispatcher.py 改动已生效」（验收原文）；其他来源（人工 script 等）注明来源；
    - 心跳无更新的 last_shutdown（崩溃/强杀/未及记录）→ 注明异常退出后由 KeepAlive 拉起，
      同样清标记并通知（fail-safe：配置已生效，不静默）。"""
    with state_lock:
        pending = dict(state.get("pending_restart") or {})
    if not pending:
        return
    hb = load_heartbeat()
    last = (hb or {}).get("last_shutdown")
    oldest = min(v.get("at", 0) for v in pending.values())
    confirmed = bool(isinstance(last, dict) and isinstance(last.get("at"), (int, float))
                     and last["at"] >= oldest)
    with state_lock:
        state["pending_restart"] = {}
    save_state()
    if confirmed and last.get("source") == "auto":
        try:
            # #380：自证成功 → 心跳标记透出到看板健康角标「自动重启成功」（board.py 读该字段渲染）
            write_heartbeat({"auto_restart_ok": True})
        except Exception as e:
            log.warning(f"写自动重启成功心跳标记失败: {e}")
    for rid in sorted(pending):
        if confirmed:
            note = (DISPATCHER_AUTO_RESTART_OK if last.get("source") == "auto"
                    else f"调度器已重启（来源: {last.get('source') or '未知'}），dispatcher.py 改动已生效")
        elif is_run_local_supervisor():
            # #8：直跑模式无 KeepAlive 承担拉起，去该措辞（exec 自重启正常路径在 handler 内先写
            # last_shutdown，走不到本分支；此分支兜底异常场景）
            note = "调度器已重启（上次退出未记录优雅关闭，直跑模式重载），dispatcher.py 改动已生效"
        else:
            note = "调度器已重启（上次退出未记录优雅关闭，KeepAlive 拉起），dispatcher.py 改动已生效"
        try:
            append_summary(rid, f"✅ {note}")
        except Exception as e:
            log.warning(f"需求 #{rid} 自动重启成功通知写 summary 失败: {e}")
        try:
            post_dispatcher_event(rid, note, kind="merge")
        except Exception as e:
            log.warning(f"需求 #{rid} 自动重启成功事件上报失败: {e}")
    log.info(f"自动重启自证：pending_restart 已清空（确认上次退出={confirmed}，"
             f"来源={last.get('source') if confirmed else '无'}），新代码生效")

def _record_shutdown(signum):
    """进程收到 SIGTERM/SIGINT 时把「上次退出」写进心跳（可单测；不含 sys.exit）：
    last_shutdown = {at, signal, source?}，来源标记文件存在则合并（约定入口写入）。
    直接合并写心跳（不更新 last_poll_at：退出时没有新的轮询，保留上次轮询时间语义）。"""
    entry = {"at": time.time(), "signal": _signal_name(signum)}
    src = _pop_restart_source()
    if src:
        entry["source"] = src
    write_heartbeat({"last_shutdown": entry}, update_poll=False)

def _shutdown_handler(signum, frame):
    """信号 handler：记录 + 退出；#8：run_local 直跑模式下「新鲜重启请求（SIGTERM + 来源标记 +
    TTL 内）」改 exec 自重启（同 PID，整组不停止）。只做记录/判定的轻量操作，禁止网络/事务
    等耗时操作（分析 #373 风险点 2）。判定顺序：预读标记（不删）→ 记录退出（消费标记，先记录
    再 exec 使新进程既有自证链 _verify_pending_restart 判 confirmed）→ 满足条件 exec → 否则原退出。"""
    source_entry = _read_restart_source()
    source = (source_entry or {}).get("source")
    if not isinstance(source, str) or not source:
        source = None
    try:
        _record_shutdown(signum)
        log.info(f"收到信号 {_signal_name(signum)}，已记录上次退出")
    except Exception as e:
        # 记录异常不阻断 exec 判定（防「直跑+新鲜标记」被记录异常退化成整组停止）
        log.error(f"记录上次退出失败: {e}")
    if (signum == signal.SIGTERM and source and is_run_local_supervisor()
            and _restart_source_fresh(source_entry)):
        # exec 成功不返回；失败/意外返回 → 拒绝重启、绝不裸退出（保持进程存活；
        # auto 路径由下一轮重试/兜底文案承接，web/reqchat 由健康核对超时报错）
        _exec_self_restart(source)
        return
    log.info(f"收到信号 {_signal_name(signum)}，进程退出")
    sys.exit(0)

def register_shutdown_handlers():
    """#373：注册 SIGTERM/SIGINT handler——此前 SIGTERM 直接死亡零记录，
    重启来源事后只能靠 launchd 日志+log show 人工排查。仅主线程（main）调用。"""
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

def report_previous_exit():
    """#373：启动时上报上次退出原因（写启动日志，看板健康角标经心跳透出）：
    - 心跳有 last_shutdown → 上次 SIGTERM/SIGINT 优雅退出（含来源：script/reqchat 等或未知）
    - 心跳存在但无 last_shutdown → 异常终止（崩溃/被强杀/kill -9）
    - 无心跳记录 → 首次部署/旧版本，不误报"""
    try:
        hb = load_heartbeat()
        last = hb.get("last_shutdown") if hb else None
        src = _pop_restart_source()
        if isinstance(last, dict) and last.get("at"):
            if src and "source" not in last:
                # 兜底：退出时未读到来源（竞态残留），启动时补进心跳
                last = dict(last, source=src)
                write_heartbeat({"last_shutdown": last})
            at = format_storage_time(last["at"])
            src_txt = f"（来源: {last['source']}）" if last.get("source") else "（来源未知）"
            log.info(f"调度器启动：上次退出 {last.get('signal', '信号')} @ {at}{src_txt}")
        elif src:
            log.warning(f"调度器启动：发现重启来源标记（{src}），但无上次退出记录（进程异常终止未及记录）")
        elif hb:
            log.warning("调度器启动：上次退出无正常关闭标记（异常终止：崩溃/被强杀）")
        # 无心跳记录（首次部署）→ 静默，不误报
    except Exception as e:
        log.warning(f"调度器启动：读取上次退出记录失败: {e}")

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
