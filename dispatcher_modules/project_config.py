"""项目配置归一：projects.json 读取与热重载、git / git_sync / worktree 配置校验。"""

import json
import logging
import os
import re
from pathlib import Path

from acp_shared import ROLE_CAPABILITIES, flow_names, role_meta

from dispatcher_modules.runtime import D, log


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
    raw = json.loads(D.PROJECTS_FILE.read_text())
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
        st = os.stat(D.PROJECTS_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# 热重载基线：import 后随 PROJECTS 初始化；测试进程把 PROJECTS_FILE 钉到 fixture 时须同步置 None
_PROJECTS_SIG = _projects_file_sig()


def _maybe_reload_projects():
    """#4：projects.json 变化（mtime+size）时重载 PROJECTS，主循环每 tick 调用（单线程）。
    基线为 None（首轮/测试进程未钉基线）→ 仅记录不发重载；重载失败保留旧值 + log.error
    留痕并记录该失败签名（文件再次变化才重试，防每 tick 刷屏）。"""
    sig = _projects_file_sig()
    if sig == D._PROJECTS_SIG:
        return
    if D._PROJECTS_SIG is None or sig is None:
        # 首轮（无基线）或文件缺失/不可 stat：仅记录，不重载不报错（静默跳过）
        D._PROJECTS_SIG = sig
        return
    try:
        new = D.load_projects()
    except Exception as e:  # noqa: BLE001 - 重载失败降级：沿用旧配置，留日志待人工修
        log.error(f"projects.json 热重载失败，继续沿用旧配置: {e}")
        D._PROJECTS_SIG = sig
        return
    D.PROJECTS = new
    D._PROJECTS_SIG = sig
    log.info(f"projects.json 已热重载（{len(D.PROJECTS)} 个项目）")
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
    base = D.requirement_timeout(req)
    if re.search(r"timeout\s*[:：]\s*(\d+)", req.get("notes") or ""):
        return base
    cap = (D.role_budget(role, project=req.get("project")) or {}).get("max_minutes")
    return min(base, cap) if cap else base
# #420：REQ_BOARD_STATE 可覆盖状态文件路径（测试运行器统一指临时目录，物理隔离真实 state；
# 先例 board.py 的 REQ_BOARD_ARTIFACTS）。未设/空串 = 现默认，行为不变；必须保持 Path 类型
# （with_name/.name/read_text 依赖）。
STATE_FILE = (Path(os.environ["REQ_BOARD_STATE"]).expanduser() if os.environ.get("REQ_BOARD_STATE")
              else D.REPO_DIR / "data" / "dispatcher_state.json")
# #40：心跳文件与看板同口径——REQ_BOARD_DATA 非空时用该目录下 dispatcher_heartbeat.json
# （测试实例/演练与看板共用一份数据目录）；未设置时维持 REPO_DIR/data/（生产行为零变化）。
HEARTBEAT_FILE = ((Path(os.environ["REQ_BOARD_DATA"]).expanduser() if os.environ.get("REQ_BOARD_DATA")
                   else D.REPO_DIR / "data") / "dispatcher_heartbeat.json")
# #373：重启来源标记（约定入口写入，调度器启动/退出时读取，读完即删）
RESTART_SOURCE_FILE = D.REPO_DIR / "data" / "dispatcher_restart_source.json"
# 显式停用（环境变量）或未安装飞书 SDK（lark_oapi）时一律按停用处理
NO_FEISHU = os.environ.get("REQ_BOARD_NO_FEISHU") == "1" or not D.LARK_AVAILABLE

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

