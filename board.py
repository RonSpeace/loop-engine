#!/usr/bin/env python3
"""
需求看板 - 应用入口
==================
后端共享层在 board_modules/（runtime 兼容桥 / db / common / sse）；本文件是 Flask 应用入口与
兼容 facade，路由全部在 board_modules/blueprints/（requirements / projects / chats / settings /
assistant / sessions / meta 七个资源蓝图，按 blueprints.ALL 注册）。跨项目的待开发需求管理：
记录、状态流转（待开发/进行中/已完成）、按项目分组。
服务层在 board_modules/services/（model_pool / harness_config / projects / requirements / session_runtime / presentation / dispatcher_ops / chat_runtime / mcp_servers / reqchat_runtime / assistant_card / assistant_runtime / assistant_mcp / project_git）。
只监听 127.0.0.1，本机使用，无鉴权。

运行: ./start.sh
访问: http://127.0.0.1:8899
"""

import atexit
import hashlib
import html
import logging
import os
import select
import json
import base64
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from storage_time import parse_storage_time
from pathlib import Path

from flask import Flask, render_template, request

# ACP 会话/只读工具共享模块（#107）：ACPSession 事件上报注入回调（直接写 chat_events 表），
# 只读工具 roots = 会话项目目录 + 项目各仓库根；board 常规不 import dispatcher（重依赖 + 调度器状态），
# 仅 /guide 路由延迟 import dispatcher 读取提示词常量用于展示（#269：dispatcher 顶层仅 load_projects
# 无副作用；import 失败时该版块降级显示，不影响看板主进程）
from acp_shared import ACPSession, KIMI, ROLE_CAPABILITIES, role_meta, role_model_detail, NODE_TYPES, pre_role, HARNESS_PROVIDER_TYPES, HARNESS_PROVIDER_TYPES_HINT, CONTEXT_SIZE_DEFAULTS

# #26 助手文件/代码只读分析：路径解析、敏感黑名单、限量常量与只读语义纯函数的单一源 = acp_shared
from acp_shared import ro_search_hits, FILE_MAX_BYTES, FILE_MAX_LINES_LIMIT, DIR_MAX_ENTRIES, SEARCH_MAX_MATCHES

# ==================== #32 拆分：模块化兼容桥 ====================
# 子模块经 runtime.resolve 晚绑定读取本模块（board / __main__）的可补丁常量与函数；
# 先注册别名，保证 `python board.py`（__main__）下 sys.modules["board"] 指向同一模块。
sys.modules.setdefault("board", sys.modules[__name__])
from board_modules.db import get_db, init_db
from board_modules.common import STATUS_LABEL_CN, _now_ts, now_str, _ts_ms, _status_label_cn, parse_artifact_legacy_roots
from board_modules.sse import _SSE_ENABLED, _SSE_MAX_CONNS, _SSE_HEARTBEAT_SEC, _SSE_PROBE_SEC, _SSE_QUEUE_MAX, _sse_lock, _sse_subs, _SSE_STATS, _SSEConn, _sse_publish, _sse_acquire, _sse_peer_gone, _SSEStream
# SSE 发布序号单一源在本模块：sse.py 经 runtime.resolve 读、runtime.bump 写回（不留模块级副本）。
_sse_seq = 0             # 全局递增序号，便于测试断言与排障
BASE_DIR = Path(__file__).parent
# REQ_BOARD_DATA 仅测试实例覆盖用（种子数据/视觉闸门隔离真实库；缺省行为零变化 = 仓库根 data/）
DATA_DIR = Path(os.environ.get("REQ_BOARD_DATA") or (BASE_DIR / "data")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "board.db"
# 调度器每轮轮询结束写的心跳文件，供页头健康角标判断调度器是否存活/卡住
HEARTBEAT_FILE = DATA_DIR / "dispatcher_heartbeat.json"
# #373：重启来源标记（调度器侧约定入口 scripts/dispatcher-restart.sh / 本页 restart_dispatcher 工具写入）
RESTART_SOURCE_FILE = DATA_DIR / "dispatcher_restart_source.json"
HEALTH_STALE_SEC = 180  # 心跳超过该秒数未更新视为调度器疑似故障（≈3 个轮询周期）
# #8：看板进程启动时刻——判断心跳里的 board_restart_pending 是否已被本次启动消费（标记 at 早于
# 本进程启动即视为「重启已发生」自动隐藏，无需任何清理写入）
BOARD_STARTED_AT = time.time()

# REQ_BOARD_FREEZE_TIME（仅测试实例/视觉闸门用，缺省零变化）：冻结「现在」到固定时刻，
# 值为存储时区字符串 YYYY-MM-DD HH:MM:SS（与 now_str / _ts_ms 共用 REQ_BOARD_TZ）。
# 设置后：now_str()、server_now、心跳 age 等进页面的服务端「现在」均取该时刻；首页另注入一段内联脚本
# 把浏览器 Date.now()/new Date() 钉到同一时刻（计时器/角标渲染逐次一致）。非法值直接拒绝启动——
# 静默回落真实时钟会让视觉基线悄悄失去确定性。
FREEZE_TIME_STR = (os.environ.get("REQ_BOARD_FREEZE_TIME") or "").strip() or None
FREEZE_TS = None  # 冻结时刻的 epoch 秒；None = 未冻结（走真实时钟）
if FREEZE_TIME_STR:
    try:
        FREEZE_TS = parse_storage_time(FREEZE_TIME_STR).timestamp()
    except ValueError:
        raise SystemExit(f"REQ_BOARD_FREEZE_TIME 非法: {FREEZE_TIME_STR!r}（需为 YYYY-MM-DD HH:MM:SS）")


# #421 会话活性判死阈值（秒）：前端红态与服务端重启端点守卫同口径（改一处须同步前端常量）
AGENT_STALL_SEC = 180
# #421 首帧档（仅「等模型响应」证据）的红阈值：上游不发 agent_thought_chunk 的降级口径放宽到 300s
# （tech.md §5）。未实测确认「不发」前默认与其它档一致（180）；env 可覆盖以启用降级口径。
AGENT_STALL_FIRST_FRAME_SEC = int(os.environ.get("REQ_BOARD_STALL_FIRST_FRAME_SEC") or AGENT_STALL_SEC)
THOUGHT_FRAMES_ABSENT = AGENT_STALL_FIRST_FRAME_SEC > AGENT_STALL_SEC


# 需求产物只读访问：~/.req-board/artifacts/<project>/req-<id>/<文件名>（调度器阶段产物约定，本需求只消费）。
# REQ_BOARD_ARTIFACTS 仅测试实例覆盖用（默认值与 dispatcher.py 产物目录约定一致，主实例行为零变化）
ARTIFACTS_ROOT = Path(os.environ.get('REQ_BOARD_ARTIFACTS') or Path.home() / ".req-board" / "artifacts")
# #53 历史产物根（只读兜底）：REQ_BOARD_ARTIFACTS_LEGACY 用 os.pathsep 分隔多个旧家目录产物根，
# 当前根始终优先；空/未设 = []（行为与改动前一致）。子模块经 runtime.resolve 晚绑定读取。
ARTIFACTS_LEGACY_ROOTS = parse_artifact_legacy_roots(os.environ.get('REQ_BOARD_ARTIFACTS_LEGACY'), ARTIFACTS_ROOT)

# #32 Phase 6：兼容 facade 移入 board_modules/facade.py，board.py 只做一次挂载；名字、对象
# 与 runtime.resolve 的晚绑定行为零变化（facade 内仍是原先那批逐模块 import）。
from board_modules.facade import *  # noqa: F401,F403


ARTIFACT_FILES = _artifact_files()

REQ_CHAT_HEALTH_WAIT = 12    # restart_dispatcher 后心跳/健康核对最长等待秒数


# 操作指南静态页（页头「操作指南」入口；描述状态流转/回复格式/建卡对话/产物查看，与 dispatcher 行为同步）
GUIDE_HTML = BASE_DIR / "docs" / "guide.html"

app = Flask(__name__)

# 访问日志默认关闭：Werkzeug 把每条请求（含 200）写 stderr，而调度器 2 秒/15 秒轮询 + 浏览器
# SSE 轮询让这份 launchd 日志一天涨几十 MB——2026-09-18 实测 req-board.err.log 被访问日志撑到
# 424MB，真实报错被淹没。看板是本机单用户工具，请求日志价值低；要排障时用
# REQ_BOARD_ACCESS_LOG=1 启动即可恢复（只影响 werkzeug 的 INFO 请求行，异常/traceback 照常记录）。
if os.environ.get("REQ_BOARD_ACCESS_LOG") != "1":
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ==================== API ====================


# ==================== #4 项目中心 P2：项目级模型覆盖（机器人抽屉后端） ====================
# 数据源 = projects.json（与 dispatcher/acp_shared 共享同一文件）；写路径三件套：
# fail-closed 校验、原子替换（os.replace）、.bak 备份。落点 = os.path.realpath(PROJECTS_FILE)
# 的真实文件——worktree 内 projects.json 为指向主工作区的软链，POSIX rename 不跟随软链，
# 直接替换软链路径会替换软链本身。四层判定唯一源 = acp_shared.role_model_detail（不得本文件重排）。
# REQ_BOARD_PROJECTS 仅测试实例覆盖用（浏览器级验证/accept 隔离真实注册表；缺省行为零变化）
PROJECTS_FILE = Path(os.environ.get("REQ_BOARD_PROJECTS") or (BASE_DIR / "projects.json"))
_MODEL_WRITE_LOCK = threading.Lock()  # 项目配置写锁（#4 模型覆盖 + #5 项目增删改共用；非重入：锁由调用方获取，helper 不加锁）


# ==================== #6 模型设置（harness config.toml 全量维护：GET/POST /api/model-settings） ====================
# 写策略 = 行级最小拼接（只改目标行/段，未编辑行逐字节保留）+ 写前 tomllib 回环校验兜底；失败一律零写入。
# 写三件套 = 同目录 tmp（fsync）→ .bak（写前原字节）→ os.replace；三者权限 = 原文件 stat 显式 chmod。
# HTTP 面不出现 api_key 字样（读回 key_configured，请求字段 key/clear_key）；密钥不进日志与错误文案。


def _refresh_project_paths_snapshot():
    """写成功后刷新 import 期 PROJECT_PATHS 快照（#5 决策 D1）：建卡会话校验（POST /api/chat）、
    工作目录与只读 roots 即时可用；fail-safe：刷新失败保持旧快照（不阻断写路径响应）。"""
    global PROJECT_PATHS
    try:
        PROJECT_PATHS = _projects_config()
    except Exception:  # noqa: BLE001
        pass


# ==================== 会话产物（只读） ====================


# ==================== 执行过程事件（调度器 ACP 写入，页面轮询读取） ====================


# ==================== #416 页面事件流（SSE 推送，poke-only） ====================


# ==================== 需求评审记录（#342）：评审过程与结论结构化留档，append-only ====================
# ⚠️ 隔离硬约束：req_reviews 内容【永不进 agent prompt】——notes/summary 才是 prompt 通道
# （build_prompt 只读这两个字段），评审过程/结论只能写本表或产物目录；
# 本表不出现在 req_list 等需求查询结果里，禁止新增任何 prompt 侧读取方。


# ==================== 状态变更审计（#375）：req_status_log 查询，只增不删 ====================

# ==================== 会话运行记录（调度器写入，报表数据源，只增不删） ====================

# #422 会话记录目录解析（board 侧 glob 反查，不落库、不改 dispatcher）。
# kimi 的会话记录目录形如 <home>/sessions/wd_<workdir 哈希>/session_<sid>/，其中 wd_<hash> 由
# kimi 按 workdir 生成、看板拼不出，只能反查；本机存在多个 harness home（~/.kimi-code、
# ~/.kimi-code-deepseek）。解析结果仅作 GET 响应的计算字段 session_dir，不写库。
_SESSION_DIR_MISS_TTL = 60.0        # 未命中负缓存：目录被清理/新目录出现最多 60s 内纠正


# #12 建卡对话上传本机 html 参考文件：落盘根 = 本机数据区（与 artifacts/worktrees 平行，
# 可用 REQ_BOARD_MATERIALS 覆盖，测试实例隔离用）。原文件保留、不自动清理（方案 D3）。
MATERIALS_ROOT = Path(os.environ.get('REQ_BOARD_MATERIALS') or Path.home() / ".req-board" / "materials")
# #22 飞书凭据单一源 = 仓库根 feishu.json（0600，gitignore）；REQ_BOARD_FEISHU_JSON 供测试隔离
# （board/dispatcher 同名）。设置抽屉「飞书机器人」tab 读写该文件；密钥永不回显、不进日志。
# 覆盖值同样 expanduser()，与 dispatcher 口径一致（dispatcher.py 用 Path(os.environ[...]).expanduser()）——
# 否则 `~/x/feishu.json` 看板写相对路径、调度器读家目录，保存成功但长连接仍用旧文件。
_ENV_FEISHU_JSON = os.environ.get('REQ_BOARD_FEISHU_JSON')
FEISHU_JSON_PATH = (Path(_ENV_FEISHU_JSON).expanduser() if _ENV_FEISHU_JSON
                    else (BASE_DIR / "feishu.json"))
# #22 飞书建卡会话素材（与 web 口径互不干扰，独立常量）：图片/文档白名单，单文件 20MB。
# JSON+base64 传输 → body 上限按 4/3 膨胀 + 1MB 余量（authoritative 判定仍是逐文件解码后字节数）。
FEISHU_MATERIAL_MAX_FILES = 5
FEISHU_MATERIAL_MAX_BYTES = 20 * 1024 * 1024
FEISHU_MATERIAL_BODY_LIMIT = (FEISHU_MATERIAL_MAX_FILES * FEISHU_MATERIAL_MAX_BYTES * 4 // 3
                              + 1024 * 1024)

# TAPD 评论凭据：读自 kimi-code 全局 MCP 配置（~/.kimi-code/mcp.json 的 mcpServers.tapd.env），
# 进程内直接调 TAPD 开放 API（HTTP 桥，不经 agent 上下文；agent 只调本服务工具）
KIMI_CODE_MCP_JSON = Path.home() / ".kimi-code" / "mcp.json"


PROJECT_PATHS = _projects_config()


PROJECT_RESTART = _projects_restart_config()


def _projects_mysql_config():
    """projects.json 可选的每项目 mysql 只读查询配置（项目名 -> {"env_file": 路径或 None}）。
    仅显式配置的项目开放 mysql_query 工具；env_file 省略时默认取项目首个路径下 .env。"""
    try:
        raw = json.loads((BASE_DIR / "projects.json").read_text())
    except Exception:
        return {}
    out = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        m = cfg.get("mysql")
        if m is True:
            out[name] = {"env_file": None}
        elif isinstance(m, dict):
            env_file = str(m.get("env_file") or "").strip() or None
            out[name] = {"env_file": env_file}
    return out


PROJECT_MYSQL = _projects_mysql_config()


ASSISTANT_SCAN_MAX_FILES_TOTAL = 2000          # 跨项目扫描：全部项目**共用一份**文件扫描预算
ASSISTANT_SCAN_DEADLINE_SEC = 20               # 跨项目扫描墙钟时限（秒）；防大目录遍历拖到单轮 90s 被取消
ASSISTANT_PROMPT_FILE = BASE_DIR / "prompts" / "feishu_assistant.md"


def _sigterm_handler(signum, frame):
    _cleanup_chat_procs()
    _cleanup_reqchat_procs()
    _cleanup_assistant_procs()
    sys.exit(0)


atexit.register(_cleanup_chat_procs)
atexit.register(_cleanup_reqchat_procs)
atexit.register(_cleanup_assistant_procs)
signal.signal(signal.SIGTERM, _sigterm_handler)


# ---- 建卡对话 API ----


# ==================== 需求助手 API（#302 引入，独立于调度器；#326 起前端为中间栏置顶入口 + 右栏独立视图） ====================


# ==================== 调度器动作时间线（调度器自身写入，页面回放读取） ====================


# ==================== 自动合并冲突网页决策（#386） ====================


# ==================== 网页确认交互（#400） ====================
# 动作白名单按 kind（与 dispatcher.WEB_CONFIRM_ACTIONS 对等；route/review/merge 首期不走本通道）
# #411：review_gate 三档——pass 仍合并 / rework 转开发 / reject 转暂缓


def _read_pending_confirm_state():
    """读 dispatcher_state.json 的 pending_confirm/pending_route/pending_review 三字典。
    同 api_sessions 读取模式（try/except，失败返回空）；渲染数据源，state 仍是权威。"""
    try:
        st = json.loads((DATA_DIR / "dispatcher_state.json").read_text())
    except Exception:
        return {}
    return {
        "pending_confirm": st.get("pending_confirm") or {},
        "pending_route": st.get("pending_route") or {},
        "pending_review": st.get("pending_review") or {},
    }


@app.after_request
def no_store_api(resp):
    """API 响应禁止缓存，保证页面定时刷新拿到的是最新状态"""
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp

# ==================== 页面 ====================
# 首页模板 templates/board.html（#32 Phase 2 迁出；#33 起 CSS/JS 外置在 static/board.css、static/board.js）。
# HTML 仅为兼容导出（测试/外部只读），与 Jinja loader 同一路径；首页渲染走 render_template。
HTML = (Path(app.root_path) / app.template_folder / "board.html").read_text(encoding="utf-8")


# ==================== #22 飞书机器人设置（feishu.json 读写 + 连通性测试） ====================
# 凭据单一源 = 仓库根 feishu.json（{"app_id","app_secret"}，0600，gitignore）；本模块读写，
# dispatcher 启动时读一次（改凭据需重启调度器）。密钥永不回显、不进日志与错误文案。
def _feishu_json_read():
    """读 feishu.json：返回 (app_id, app_secret, warnings, assistant_model)。
    文件缺失 → 空态（'', '', [], None）；**文件存在但不可读/解析失败/结构非法 → 失败态
    （'', None, [文案], None）**——secret 返回 None 供调用方区分「未配置」（''）与「文件损坏」（None），
    绝不把损坏文件当空值静默覆盖。
    #24：assistant_model = 可选助手模型别名（旧文件无该字段 = 未配置，None）。
    warnings 只用固定文案描述问题，**不回显 JSONDecodeError 原文**（其 msg 可能含文件片段）。"""
    warnings = []
    try:
        text = FEISHU_JSON_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "", "", warnings, None
    except OSError:
        return "", None, ["feishu.json 不可读，请检查文件权限"], None
    try:
        raw = json.loads(text)
    except Exception:  # noqa: BLE001 - 固定文案，不回显 JSONDecodeError（可能含文件片段）
        return "", None, ["feishu.json 解析失败，请检查文件格式"], None
    if not isinstance(raw, dict):
        return "", None, ["feishu.json 结构不符（顶层非对象），请检查文件格式"], None
    app_id = raw.get("app_id") if isinstance(raw.get("app_id"), str) else ""
    app_secret = raw.get("app_secret") if isinstance(raw.get("app_secret"), str) else ""
    am = raw.get("assistant_model")
    assistant_model = am.strip() if isinstance(am, str) and am.strip() else None
    return app_id, app_secret, warnings, assistant_model


def _feishu_json_raw():
    """feishu.json 原始对象（保留未知字段用）；缺失/不可解析/非对象 → None。"""
    try:
        raw = json.loads(FEISHU_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 缺失/损坏一律按 None（上层按既有 400 口径拒绝重建）
        return None
    return raw if isinstance(raw, dict) else None


def _feishu_credential_fingerprint(app_id, app_secret):
    """#23 凭据指纹（与 dispatcher.feishu_credential_fingerprint **逐字同源**的唯一定值，tech §4.1）：
    sha256(app_id + b"\\x00" + app_secret).hexdigest()[:16]。**不做 strip、不做大小写归一化**；
    任一侧缺失/为空/非字符串 → None。两侧实现由 tests/test_feishu_channel_status.py 逐字节锁定。"""
    if not isinstance(app_id, str) or not isinstance(app_secret, str):
        return None
    if not app_id or not app_secret:
        return None
    return hashlib.sha256(app_id.encode("utf-8") + b"\x00"
                          + app_secret.encode("utf-8")).hexdigest()[:16]


def _read_heartbeat_dict():
    """读心跳文件为 dict；缺失/解析失败/非对象 → None（fail-safe，绝不抛）。"""
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
    except Exception:
        return None
    return hb if isinstance(hb, dict) else None


def _heartbeat_pid(hb):
    """#40 心跳里的调度器 pid（正整数才认；缺失/非法/布尔 → None）。"""
    pid = hb.get("pid") if isinstance(hb, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


# #40 心跳 pid 存活核验：结果按 pid 缓存（防 10s 轮询 × 多视图重复起 ps 子进程）
PID_PROBE_CACHE_TTL = 5.0
_PID_PROBE_CACHE = {}
# #40 pid_status 非 ok 时的注解（追加进 reason；只作证据说明，不改 state）
PID_STATUS_NOTES = {'missing': '心跳 pid 非当前调度器进程（心跳未写入 pid，旧版本或未上报）',
                    'dead': '心跳 pid 非当前调度器进程（pid 已不是调度器进程）',
                    'unknown': '心跳 pid 非当前调度器进程（存活核验未完成）'}


def _dispatcher_pid_status(pid):
    """#40 `pid_status ∈ {ok, missing, dead, unknown}`：只核验心跳里的 pid——
    ① `os.kill(pid, 0)` 存活；② `ps -ww -p <pid> -o command=` 末个空白分隔 token 的 basename
    为 dispatcher.py（口径同 dispatcher_ops._dispatcher_pid_heartbeat，超时 2 秒）。
    不查 launchd label（那条路径按 label 取的是生产调度器 pid，测试实例/临时进程会被判成别人）。
    核验抛错/超时记 unknown；结果按 pid 缓存 PID_PROBE_CACHE_TTL 秒，不让子进程拖住健康接口。"""
    if pid is None:
        return 'missing'
    now = time.time()
    cached = _PID_PROBE_CACHE.get(pid)
    if cached and now - cached[0] < PID_PROBE_CACHE_TTL:
        return cached[1]
    try:
        os.kill(pid, 0)
        r = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                           capture_output=True, timeout=2)
        tokens = r.stdout.decode(errors="replace").strip().split()
        status = ('ok' if r.returncode == 0 and tokens
                  and os.path.basename(tokens[-1]) == "dispatcher.py" else 'dead')
    except ProcessLookupError:
        status = 'dead'
    except Exception:  # noqa: BLE001 - 含 ps 超时/权限不足：未核实（不猜）
        status = 'unknown'
    _PID_PROBE_CACHE[pid] = (now, status)
    if len(_PID_PROBE_CACHE) > 64:   # pid 轮换时防缓存无界：只清过期项
        for key in [k for k, v in _PID_PROBE_CACHE.items() if now - v[0] >= PID_PROBE_CACHE_TTL]:
            _PID_PROBE_CACHE.pop(key, None)
    return status


def _feishu_runtime_status():
    """#23/#40 飞书运行态判定（tech §4.2 **严格短路**；board / guide / 设置抽屉的唯一入口）。
    state ∈ {disabled, not_loaded, failed, stale, connected, unknown}；返回
    {state, reason, source, connected, stale_seconds, restart_available, state_source, updated_at,
     pid, pid_status}。任何读取异常都降级为可读文案，不抛（设置抽屉/guide 不会 500）。
    **指纹只用于内部比对，绝不出现在返回值/接口响应里**（tech §5.2「不回传 secret 或完整指纹」）。
    #40 判定以心跳内容为权威（看板自己的 REQ_BOARD_NO_FEISHU 不覆盖一份仍有效的心跳）；
    短路顺序：0 心跳缺 last_poll_at → unknown；1 年龄 > 180 → stale；1b phase=starting（启动窗口）
    → unknown；2 文件有效 + 指纹为 null 或一致 + connected + phase=connected → connected；
    3 心跳 disabled_reason=env → disabled（heartbeat）；4 无 feishu 对象且看板显式停用 → disabled
    （board_env）；5 当前凭据文件无效 → disabled（凭据未配置）；6 指纹为空/不一致 → not_loaded；
    7 其余指纹一致（connecting/reconnecting/failed）→ failed；8 无 feishu 对象且文件有效 → unknown。
    pid 缺失/已死只进 pid_status 与 reason 注解，不抢 state。"""
    app_id, app_secret, warns, _assistant_model = _feishu_json_read()
    file_ok = bool(app_id and app_secret)   # secret 为 None（损坏）或 ''（缺失/空）都算无效
    current_fp = _feishu_credential_fingerprint(app_id, app_secret) if file_ok else None
    source = ('feishu.json（REQ_BOARD_FEISHU_JSON 覆盖）' if _ENV_FEISHU_JSON else 'feishu.json')
    if file_ok:
        local_reason = None
    elif app_secret is None:
        local_reason = warns[0] if warns else 'feishu.json 不可读或结构非法'
    else:
        local_reason = '凭据未配置（feishu.json 缺失，或 app_id / app_secret 为空）'
    hb = _read_heartbeat_dict()
    st = hb.get('feishu') if isinstance(hb, dict) else None
    if not isinstance(st, dict):
        st = None
    pid = _heartbeat_pid(hb)
    pid_status = _dispatcher_pid_status(pid)
    lp = hb.get('last_poll_at') if isinstance(hb, dict) else None
    lp_num = isinstance(lp, (int, float)) and not isinstance(lp, bool)
    age = int(_now_ts() - lp) if lp_num else None
    st_upd = st.get('updated_at') if st is not None else None
    updated_at = (st_upd if isinstance(st_upd, (int, float)) and not isinstance(st_upd, bool)
                  else (lp if lp_num else None))
    out = {'state': 'disabled', 'reason': local_reason, 'source': source, 'connected': False,
           'stale_seconds': None, 'restart_available': False, 'state_source': 'heartbeat',
           'updated_at': updated_at, 'pid': pid, 'pid_status': pid_status}

    def result(state, reason, state_source, restart_available=False):
        """逐态收口：`connected` 只由本函数写入（仅 connected 态为 true，其余一律 false，
        防止分支里先写真值再被本函数覆盖——评审打回级修复）；心跳来源且**有 feishu 对象**且
        pid 非 ok 时把注解追加到 reason（无 feishu 对象的本条不看 pid）。"""
        if state_source == 'heartbeat' and st is not None and pid_status != 'ok':
            note = PID_STATUS_NOTES.get(pid_status)
            if note:
                reason = f"{reason}；{note}" if reason else note
        out.update(state=state, reason=reason, state_source=state_source,
                   connected=(state == 'connected'),
                   restart_available=restart_available)
        return out

    # 0. 心跳有 feishu 但 last_poll_at 缺失/非法：依据不足 → unknown（不看 connected，不装 connected/stale）
    if st is not None and age is None:
        return result('unknown', '心跳缺 last_poll_at，无法核对通道状态（调度器上报不完整）',
                      'heartbeat', restart_available=file_ok)
    # 1. 年龄过期 → stale（只看年龄；pid 缺失/已死不改写本条，新鲜心跳也不得因此显示过期）
    if st is not None and age is not None and age > HEALTH_STALE_SEC:
        out['stale_seconds'] = age
        phase = st.get('phase')
        return result('stale', f"最后心跳 {age}s 前" + (f"，上次上报 phase={phase}" if phase else ''),
                      'heartbeat')
    # 1b. 启动首写窗口（#40 dispatcher.main 清旧 feishu 置 phase=starting，年龄未过期）：
    # 通道状态尚未上报 → unknown（照实显示「调度器正在启动」，不误报「凭据未加载」+ 重启按钮）
    if st is not None and st.get('phase') == 'starting':
        return result('unknown', '调度器正在启动：通道状态尚未上报', 'heartbeat')
    # 2. 已连接：文件有效 +（指纹为 null 或一致）+ connected + phase=connected
    loaded_fp = st.get('credentials_fingerprint') if st is not None else None
    loaded_fp = loaded_fp if isinstance(loaded_fp, str) else None
    if (st is not None and file_ok and (loaded_fp is None or loaded_fp == current_fp)
            and st.get('connected') is True and st.get('phase') == 'connected'):
        conflict = st.get('disabled_reason') == 'env' or st.get('enabled') is False
        return result('connected',
                      '心跳字段自相矛盾：长连接已建立，同时带停用标记' if conflict else None,
                      'heartbeat')
    # 3. 心跳 disabled_reason=env（调度器侧显式停用）；已判定 connected 的不被本条改写
    if st is not None and st.get('disabled_reason') == 'env':
        return result('disabled', 'REQ_BOARD_NO_FEISHU=1（显式停用飞书通道）', 'heartbeat')
    # 4. 没有 feishu 对象（心跳缺失/旧调度器）：看板环境变量优先——显式停用不因本地凭据文件
    # 缺失改判「凭据未配置」（--seed 实例即此形态：REQ_BOARD_NO_FEISHU=1 + 无心跳 + 无凭据文件）
    if st is None and os.environ.get('REQ_BOARD_NO_FEISHU') == '1':
        return result('disabled', 'REQ_BOARD_NO_FEISHU=1（显式停用飞书通道）', 'board_env')
    # 5. 当前凭据文件无效：原因取自本地读取（凭据未配置，不是显式停用、不是网络失败）
    if not file_ok:
        return result('disabled', local_reason, 'local_file')
    # 6. 指纹为空或与当前凭据不一致 → 调度器未加载当前凭据（enabled=false 不得压过指纹不一致）
    if st is not None and (loaded_fp is None or loaded_fp != current_fp):
        return result('not_loaded', '调度器尚未加载当前凭据（配置文件已变更或重启前未读到）',
                      'heartbeat', restart_available=True)
    # 7. 指纹一致但未连接（connecting / reconnecting / failed 过渡态）
    if st is not None:
        err = st.get('last_error')
        phase = st.get('phase') or 'failed'
        phase_text = {'connecting': '正在建立长连接', 'reconnecting': '正在重连',
                      'failed': '连接失败'}.get(phase, phase)
        return result('failed', f"{phase_text}：{err}" if err else phase_text, 'heartbeat')
    # 8. 无 feishu 对象、环境未停用、文件有效 → 不武断声称未配置；保留重启入口（本条不因缺 pid 判 stale）
    return result('unknown', '调度器未上报飞书状态（旧版本或未运行）', 'heartbeat',
                  restart_available=True)


# ==================== #32 Phase 6：路由蓝图注册与兼容导出（卡 #39） ====================
# 全部路由已迁至 board_modules/blueprints/；本模块只保留入口、路径/常量与兼容 facade。
from board_modules.blueprints import ALL as _BLUEPRINTS   # 注册顺序见 blueprints/__init__.py
for _bp in _BLUEPRINTS:
    app.register_blueprint(_bp.bp)


# ==================== 启动 ====================
if __name__ == '__main__':
    init_db()
    _recover_chat_sessions()
    _recover_req_chats()
    _start_chat_recovery_worker()
    # 端口默认 8899，可用环境变量 REQ_BOARD_PORT 覆盖（worktree 测试用，避免与 main 实例冲突）
    port = int(os.environ.get('REQ_BOARD_PORT', '8899'))
    print(f"需求看板: http://127.0.0.1:{port}")
    # 只监听本机回环地址，局域网/外网均不可达，无需鉴权
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
