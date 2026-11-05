#!/usr/bin/env python3
"""ACP 会话共享模块（dispatcher.py 与 board.py 共用，req-107 抽取）。

内容：kimi acp 子进程的 JSON-RPC 客户端 ACPSession、只读角色受限工具
ReadonlyMcpServer（list_dir/read_file/db_query/git_log）、路径/SQL 校验纯函数、
只读命令白名单与 /usage 解析。从 dispatcher.py 原样迁出（#107 前为单文件内定义），
事件上报由模块级 post_event 改为构造注入 event_sink 回调：
- dispatcher 场景：event_sink=post_event（HTTP 上报看板）；
- board 建卡会话场景：event_sink=直接写 chat_events 表。

ACPSession 相对原实现的增量参数（dispatcher 调用方默认值不变，行为零变化）：
- event_sink: callable(req_id, kind, content, session_id) | None（None 不报事件）
- readonly: bool，只读审批路由（原 READONLY_ROLES 判断由调用方换算传入）
- extra_allow_prefixes: 额外放行的工具标题前缀（如建卡会话的 mcp__reqboard-create__*）
- extra_mcp_servers: 追加注入会话的 MCP server 条目（建卡会话的 create_requirement）
- allow_fs_write: False 时 fs/write_text_file 一律拒绝（建卡会话不写任何文件）
- allow_git_commit: True 时 Bash git 只读子命令 + add/commit 放行（#302 需求助手）
- allow_git_merge: True 时（配合 allow_git_commit）额外放行 git merge/checkout/switch/stash
  受限形态 + 会话工作目录（项目主工作区）内文件写入（#355 需求助手合并授权：解决代码合并冲突；
  仅 board 需求助手会话开启，dispatcher 全角色缺省 False 行为不变）
- allow_python_safe: True 时 Bash 放行受限 python 自验形态（#387 需求助手：仅
  `python3 -m py_compile <项目内文件>` 与 `<项目>/.venv/bin/python -m unittest discover` 两形态；
  python3 -c/任意脚本/其它模块/重定向/拼接一律拒绝；仅 board 需求助手会话开启，
  dispatcher 全角色与建卡会话缺省 False 行为不变）
- python_roots: allow_python_safe 的路径根集（会话 workdir + 项目各仓库根；
  独立于 readonly_roots/_chat_roots，不放宽到家目录；None = 未启用安全兜底）
- #357 0.37.2 审批文本截断修复：Bash 审批优先用 tool_call_update 参数流缓存的结构化完整命令
  （request_permission 展示文本被 kimi 截断 ~85 字符），详见 _wait_tool_cmd
- activity_sink: callable(kind, detail) | None —— #421 会话活性上报（turn_step/各环节时间戳，
  右栏判活/判死用）；None（建卡会话/需求助手）时零行为，状态切换点见 _activity/_note_tool_step
- tool_profile: None | 'feishu_assistant'（#24）—— 助手会话 ACP 构造档：只注入 extra_mcp_servers
  的助手业务 MCP（不注入 ReadonlyMcpServer）、审批只放行该 MCP 工具、反向 fs/terminal 全拒、
  initialize 能力声明全 false；None（缺省）时上述按角色路由逐字不变
- output_guard: callable(kind, detail) | None —— #28 输出守卫（复读/单次请求超长兜底）：判据 A
  单次 LLM 请求在途时长超 GUARD_REQUEST_SEC（工具执行期不计时）、判据 B 连续 GUARD_REPEAT_N 条
  message 相邻文本相似度 ≥ GUARD_REPEAT_SIM 即触发；触发 → guard_sink 留痕 + cancel 本会话 +
  唤醒在途 prompt 抛 TurnAborted。None（缺省，含 board 建卡/需求助手会话）时零行为、逐字不变
"""

import difflib
import json
import logging
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import time
import itertools
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("acp_shared")

# ==================== 配置（dispatcher.py 同款默认值，board 建卡会话共用） ====================
REPO_DIR = Path(__file__).resolve().parent
KIMI = os.environ.get("KIMI_BIN") or shutil.which("kimi") or str(Path.home() / ".kimi-code/bin/kimi")
# 运行体能力单一源；新适配器尚不能证明所有内置工具都经客户端复核，受限角色拒绝启动。
AGENT_RUNTIMES = {
    "kimi": {"display_name": "Kimi", "argv": (KIMI, "acp"), "env": {},
             "protected_process_names": ("kimi",),
             "supports_model_option": True, "supports_usage_command": True,
             "supports_permission_mode": True, "supports_authenticate": True, "supports_model_pool": True,
             "supports_restricted_roles": True, "session_dir_resolver": "kimi"},
    "claude-code": {"display_name": "Claude Code",
                    "argv": ("npx", "-y", "@zed-industries/claude-code-acp@0.16.2"), "env": {},
                    "protected_process_names": ("claude", "claude-code", "claude-code-acp"),
                    "supports_model_option": False, "supports_usage_command": False,
                    "supports_permission_mode": False, "supports_authenticate": False, "supports_model_pool": False,
                    "supports_restricted_roles": False, "session_dir_resolver": None},
    "codex": {"display_name": "Codex",
              "argv": ("npx", "-y", "@zed-industries/codex-acp@0.16.0"), "env": {},
              "protected_process_names": ("codex", "codex-acp"),
              "supports_model_option": False, "supports_usage_command": False,
              "supports_permission_mode": False, "supports_authenticate": False, "supports_model_pool": False,
              "supports_restricted_roles": False, "session_dir_resolver": None},
}
_RUNTIME_SKIPS = set()


def agent_runtime(runtime_id):
    """未知运行体必须在创建进程前报错，不能静默回落。"""
    if not isinstance(runtime_id, str) or runtime_id not in AGENT_RUNTIMES:
        raise ValueError(f"未知 agent runtime: {runtime_id!r}；合法值：{', '.join(AGENT_RUNTIMES)}")
    return AGENT_RUNTIMES[runtime_id]


def runtime_skip(runtime_id, feature):
    """缺失能力按运行体/能力各记录一次，避免长驻服务刷屏。"""
    key = (runtime_id, feature)
    if key not in _RUNTIME_SKIPS:
        _RUNTIME_SKIPS.add(key)
        log.info("运行体 %s 不支持 %s，跳过", runtime_id, feature)


def runtime_argv(runtime_id, kimi_bin=None):
    runtime = agent_runtime(runtime_id)
    key = "LOOP_AGENT_CMD_" + runtime_id.upper().replace("-", "_")
    override = os.environ.get(key)
    if override is not None:
        argv = shlex.split(override)
        if not argv:
            raise ValueError(f"{key} 不能为空")
        return argv
    if runtime_id == "kimi":
        return [kimi_bin or KIMI, "acp"]
    binary = shutil.which(runtime_id + "-acp")
    return [binary] if binary else list(runtime["argv"])


def normalize_permission_tool(runtime_id, tool_call):
    """适配器的展示标题不可信；只从完整结构化参数识别命令/写路径。

    Claude 0.16.2 Bash 标题为反引号命令；Codex 0.16.0 exec 审批的
    rawInput.command 为 argv 数组。未知/缺失输入不猜测、不当作安全工具。
    """
    agent_runtime(runtime_id)
    if runtime_id == "kimi":
        return tool_call
    tc = dict(tool_call)
    tc.pop("writePaths", None)  # 仅允许本函数从结构化参数派生，不能采信远端附加字段
    raw = tc.get("rawInput")
    if not isinstance(raw, dict):
        return dict(tc, title="Unknown", content=[])
    command = raw.get("command")
    if isinstance(command, list) and command and all(isinstance(v, str) for v in command):
        if len(command) == 3 and Path(command[0]).name in ("bash", "sh", "zsh") and command[1] in ("-c", "-lc"):
            command = command[2]
        else:
            command = shlex.join(command)
    if isinstance(command, str) and command:
        tc.update(title="Bash", rawInput=dict(raw, command=command),
                  content=[{"content": {"type": "text", "text": command}}])
    elif runtime_id == "codex" and isinstance(raw.get("changes"), dict) and raw["changes"]:
        paths = []
        for path, change in raw["changes"].items():
            if not isinstance(path, str) or not isinstance(change, dict):
                return dict(tc, title="Unknown", content=[])
            paths.append(path)
            if change.get("move_path") is not None:
                if not isinstance(change["move_path"], str):
                    return dict(tc, title="Unknown", content=[])
                paths.append(change["move_path"])
        tc.update(title="Patch", writePaths=paths)
    elif isinstance(raw.get("file_path"), str) and any(k in raw for k in ("content", "old_string", "edits")):
        path = raw["file_path"]
        tc.update(title="Write" if "content" in raw else "Edit",
                  content=[{"content": {"type": "text", "text": "Writing " + path}}])
    else:
        tc["title"] = "Unknown"
    return tc


# 会话固定模型：kimi 升级会重置全局 default_model，会话级 set_config_option 覆盖（dispatcher 同款）
# 后台 agent 统一走 DeepSeek V4.1 Flash（~/.kimi-code-deepseek/config.toml 同名条目；
# 官方模型名 deepseek-flash，2026-09-16 由已下线的旧名 v4-flash-vision-exp 切换）
ACP_MODEL = os.environ.get("KIMI_MODEL", "deepseek/v4.1-flash")
# #4 项目级模型覆盖数据源：与 dispatcher/board 共享同一 projects.json（三处各自持有常量、指向同一文件；
# 判定链每次调用现读、不缓存——写侧原子替换保证不会读到半文件）。
# #9：honor REQ_BOARD_PROJECTS（board.py 同款测试实例覆盖）——否则测试实例里 board 写的是临时
# 文件、本模块读的仍是真实 projects.json，项目级覆盖回显/链路验证会串到真实注册表。缺省零变化。
PROJECTS_FILE = Path(os.environ.get("REQ_BOARD_PROJECTS") or (REPO_DIR / "projects.json"))
# #419/#4：per-role 模型四层兜底链唯一判定点 = role_model_detail()
# （项目级 projects.json → 配置 roles.<id>.model → KIMI_MODEL_<ROLE 大写> → 全局 ACP_MODEL）。
# role_model() 为值视图（内部即 detail）。调用方一律经 role_model()/role_model_detail() 取值，
# 不得自行重排层序；本地运维临时覆盖仍可经环境变量（配置非空时优先环境变量，项目级优先于两者）
# ACP 请求超时的分片粒度（秒，2026-09-17 修复）：threading.Event.wait 在 macOS 上是 realtime
# 语义（pthread_cond_timedwait，系统睡眠继续计时），分片等待 + monotonic 记账把睡眠穿透
# 限制在单片内，避免合盖唤醒后误判超时（详见 ACPSession.request）
ACP_WAIT_SLICE = 5
# 消息回显时效（体感修复）：流式 chunk 先攒在 _msg_buf，原来只在「缓冲 > 800 字 / 下一次工具调用 /
# 回合结束」时上报——纯文字回复（尤其回合末总结）因此只能等整轮跑完才出现在页面（实测最近 20 个
# 会话的末条 message 100% 与会话结束同秒）。现在改为「停顿即出」：静默 MSG_FLUSH_IDLE_SEC 即上报，
# 或缓冲超过 MSG_FLUSH_CHARS 立即上报（长输出安全阀）。前端 mergeMessageBlocks 会把相邻 message
# 事件无损拼接后再渲染 markdown，故分段变多不影响排版。
MSG_FLUSH_CHARS = int(os.environ.get("KIMI_MSG_FLUSH_CHARS", "400"))       # 缓冲字符上限（超限立即上报）
MSG_FLUSH_IDLE_SEC = float(os.environ.get("KIMI_MSG_FLUSH_IDLE_SEC", "0.4"))  # 静默多久算「这段说完了」
MSG_FLUSH_TICK_SEC = 0.1        # 静默检测轮询粒度（每会话一个守护线程，空转成本可忽略）
# #421 会话活性上报节流（秒）：llm_response / llm_thought 两类「输出仍在流」的证据只按此时长
# 刷新一次（首个必报）——长输出（一口气写 10 分钟）期间 age 不无界增长，又不至于每 chunk 一次
# HTTP；llm_request / tool_start / tool_end 是纯状态切换，不走节流
ACTIVITY_THROTTLE_SEC = 60
# ---- #28 输出守卫（复读 / 单次请求超长兜底；调度器会话启用，board 会话不传 output_guard = 零行为）----
# 判据 A（stall_request）：单次 LLM 请求在途时长超 GUARD_REQUEST_SEC（工具执行期不计时）。
# 判据 B（repeat_output）：同会话连续 GUARD_REPEAT_N 条 message 事件相邻文本相似度均 ≥
# GUARD_REPEAT_SIM 且各自长度 ≥ GUARD_REPEAT_MIN_LEN。触发即 cancel 本会话 + 唤醒在途 prompt
# 令其抛 TurnAborted（只影响本会话）。改后重启调度器生效（同 MSG_FLUSH_CHARS 口径）。
GUARD_REQUEST_SEC = float(os.environ.get("KIMI_GUARD_REQUEST_SEC", "1200"))    # 判据 A：默认 20 分钟
GUARD_REPEAT_N = int(os.environ.get("KIMI_GUARD_REPEAT_N", "8"))               # 判据 B：连续 N 条
GUARD_REPEAT_SIM = float(os.environ.get("KIMI_GUARD_REPEAT_SIM", "0.8"))       # 判据 B：相邻相似度阈值
GUARD_REPEAT_MIN_LEN = int(os.environ.get("KIMI_GUARD_REPEAT_MIN_LEN", "100"))  # 判据 B：最小文本长度
GUARD_SIM_LEN_CAP = 2000   # 相似度/入队文本截断上限（限界，防超长块逐字符比对放大开销）


class TurnAborted(Exception):
    """#28 输出守卫触发时由 request(guard=True) 抛出（经 prompt() 上抛），供调用方按中止收束收尾。"""


def repetitive_run(texts, sim=None, min_len=None):
    """#28 判据 B 纯函数：texts 是否「相邻两两相似度 ≥ sim」且每条长度 ≥ min_len。

    阈值缺省（None）= **取调用时刻**的模块常量 `GUARD_REPEAT_SIM` / `GUARD_REPEAT_MIN_LEN`
    （不能在函数定义处绑定默认值——那会固定模块首次加载时的值，运行时 env 覆盖/常量改写不生效）。
    texts 长度须 ≥ 2（单条无相邻对 → False）。先用 quick_ratio()（廉价上界）预筛再算 ratio()。
    文本截断到 GUARD_SIM_LEN_CAP 计相似度（现场为同句滑动窗口切分，相邻块高相似、远块不同）。"""
    sim = GUARD_REPEAT_SIM if sim is None else sim
    min_len = GUARD_REPEAT_MIN_LEN if min_len is None else min_len
    if not texts or len(texts) < 2:
        return False
    if any(len(t) < min_len for t in texts):
        return False
    for a, b in zip(texts, texts[1:]):
        a2, b2 = a[:GUARD_SIM_LEN_CAP], b[:GUARD_SIM_LEN_CAP]
        sm = difflib.SequenceMatcher(None, a2, b2, autojunk=False)
        if sm.quick_ratio() < sim:
            return False
        if sm.ratio() < sim:
            return False
    return True


WORKTREES_ROOT = Path.home() / ".req-board" / "worktrees"  # 多 paths 项目聚合目录根
RESULT_FILENAME = ".req-board-result.json"

# ---- #8 进程主管模式标记（board/dispatcher 共用，避免两处判定漂移） ----
# run_local.sh 直跑模式显式导出 REQ_BOARD_SUPERVISOR=run_local；launchd 托管/未知环境不设 =
# 「缺省即旧路径」（优雅退出等 launchd 拉起，行为与旧版逐字一致）。每次现读 env 不缓存：
# 进程内改 env 立即生效（测试 patch.dict 场景依赖此语义）。
SUPERVISOR_ENV_VAR = "REQ_BOARD_SUPERVISOR"
RUN_LOCAL_SUPERVISOR = "run_local"

def supervisor_mode():
    """当前进程的主管模式标记原值（未设/空 = 空串，即 launchd/未知的旧路径）。"""
    return (os.environ.get(SUPERVISOR_ENV_VAR) or "").strip()

def is_run_local_supervisor():
    """是否 run_local 直跑模式（#8：无 launchd supervisor，重启走 exec 自重启/降级提示）。"""
    return supervisor_mode() == RUN_LOCAL_SUPERVISOR

# ---- 只读角色受限工具（本地 HTTP MCP server，#78：替代 Bash 审批白名单通道，agent 经工具调用使用） ----
# kimi acp 0.32.0 实测（2026-08-05 probe）：无客户端工具注册协议；session/new 的 mcpServers 只收远程 URL
# 条目（{type: "http"|"sse"|"acp", name, url, headers}），故在调度器侧起本地 HTTP MCP server 提供只读工具，
# kimi 以 MCP 客户端连接后 agent 直接调用；#82 实测（mode=default 下）MCP 工具调用经
# session/request_permission 且被 _auto_approve 拒绝，现已按 READONLY_TOOL_SERVER 前缀放行；
# 安全全在工具内部校验（fail-closed），_auto_approve 的 Bash 分支维持 fail-closed 不变。
# ---- 角色能力清单（#339 单一事实源）----
# 各流水线角色的能力档位集中声明：_auto_approve 审批路由、_is_readonly 兜底、事件流红线
# 处置、dispatcher 侧 ACPSession 构造参数与收尾校验、artifact_ok 产物契约均从此派生。
# 新增角色 = 在本表加一条（prompt 文案在 dispatcher.ROLE_PROMPTS 登记，经 prompt_key 关联，
# 属内容非能力配置；复用现有模板时新增角色确实只需改本表一处）。
# 字段语义（#419 只保留能力/安全档位；展示/内容键与收尾契约键全部派生，不存本表）：
#   readonly               只读审批路由（等价原 role in READONLY_ROLES）
#   bash                   Bash 策略：allow=全放行（#336 写主工作区护栏仍生效）/
#                          no_commit=拒提交类命令 / whitelist=只读白名单 fail-closed /
#                          git_merge=仅 git_cmd_safe(allow_merge=True) 受限形态（merge_resolve）
#   artifact_write         True=fs 写仅限产物目录（只读角色与 accept）；False=按 write_roots 白名单
#   cancel_on_redline      事件流命中禁杀红线时直接 cancel 会话（dev）；否则仅审批拒绝+留痕
#   manual_mode            True=会话切 mode=default（manual 审批）；dev=False 保持全放行模式
#   tapd_readonly          dispatcher 注入 TAPD 只读工具前缀（extra_allow_prefixes）
#   python_safe            True=只读 Bash 另放行受限 python 自验两形态（#387/#423 评审角色）；
#                          仅显式 True 开启，且须配 python_roots（+可选 python_compile_roots）。
#                          未写的角色经 _ROLE_CAP_DEFAULT 回落 False（fail-closed，勿改成 True）
# #419 退役键（不再存本表）：done_gate / require_suggested_mode / require_verdict / prompt_key——
# 收尾门槛由节点 node_type 派生（code → 代码变更 / artifact·review → 产物非空），评审语义由
# review 节点 + verdict 驱动，提示词模板与产物名由配置 roles.<id>（role_prompt()/role_meta()）派生。
# #404：display（中文名）/order（展示顺序）唯一手写源 = flows.json roles.<id>；
# 本表只保留能力/安全档位，展示/内容键经 role_capability() 转发配置读取，
# 键集一致性由加载期双向校验 + tests/test_flow_config.py 锁定
ROLE_CAPABILITIES = {
    "analysis": {"readonly": True, "bash": "whitelist", "artifact_write": True,
                 "cancel_on_redline": False, "manual_mode": True, "tapd_readonly": True},
    "proposal": {"readonly": True, "bash": "whitelist", "artifact_write": True,
                 "cancel_on_redline": False, "manual_mode": True, "tapd_readonly": True},
    "tech": {"readonly": True, "bash": "whitelist", "artifact_write": True,
             "cancel_on_redline": False, "manual_mode": True, "tapd_readonly": True},
    # #376：agent 评审角色（只读档位同 analysis/proposal；verdict 契约由 node_type=review 承载）。
    # code_review 为**唯一**代码审查角色——两条内置流程（标准/复杂）共用同一角色、
    # 同一份 prompt、同一产物 code_review.md；原 review 角色已删除（提示词用 {stage_input}
    # 适配两条流程的上游产物差异）。python_safe = 评审角色受限 python 自验（#423）。
    "tech_review": {"readonly": True, "bash": "whitelist", "artifact_write": True,
                    "cancel_on_redline": False, "manual_mode": True, "tapd_readonly": True,
                    "python_safe": True},
    "code_review": {"readonly": True, "bash": "whitelist", "artifact_write": True,
                    "cancel_on_redline": False, "manual_mode": True, "tapd_readonly": True,
                    "python_safe": True},
    "accept": {"readonly": False, "bash": "no_commit", "artifact_write": True,
               "cancel_on_redline": False, "manual_mode": True, "tapd_readonly": False},
    "dev": {"readonly": False, "bash": "allow", "artifact_write": False,
            "cancel_on_redline": True, "manual_mode": False, "tapd_readonly": False},
    # #396：自动合并冲突解决（合并差异会话，能力 = 需求助手 #355 合并授权的子集）。
    # git_merge Bash 策略：仅放行 git_cmd_safe(allow_merge=True) 受限形态（add/commit/
    # merge/checkout/switch/stash + 只读子命令），其余命令一律拒（fail-closed）；
    # 写仍受 write_roots 白名单（worktree/产物/tmp，不含主工作区，#303/#336 不变）
    "merge_resolve": {"readonly": False, "bash": "git_merge", "artifact_write": False,
                      "cancel_on_redline": False, "manual_mode": True, "tapd_readonly": False},
}
# 未知角色兜底档位（__new__ 桩/旧调用方只传 role 空串等）——#419 fail-closed：
# 只读 + Bash 白名单 + 不写产物目录，杜绝「新增/拼错角色未配能力即以全放行拉起会话」
_ROLE_CAP_DEFAULT = {"readonly": True, "bash": "whitelist", "artifact_write": False,
                     "cancel_on_redline": False, "manual_mode": True, "tapd_readonly": False,
                     "python_safe": False}
# 展示/内容键（唯一源 = flows.json roles.<id>）：读走 role_meta，不再存在于能力表
_ROLE_CONTENT_KEYS = ("display", "order", "artifact", "model", "prompt_file", "prompt")


def role_capability(role, key):
    """读角色档位：能力/收尾键查代码能力表（未知角色 fail-closed 兜底）、
    展示/内容键（display/order/artifact/model/prompt*）查 flows.json 配置（#419 单一源）。"""
    if key in _ROLE_CONTENT_KEYS:
        return role_meta(role, key)
    entry = ROLE_CAPABILITIES.get(role)
    if entry is not None and key in entry:
        return entry[key]
    return _ROLE_CAP_DEFAULT.get(key, _ROLE_CAP_DEFAULT["readonly"])

# 严格只读角色（analysis/proposal/tech/review，dispatcher 同款；供 __new__ 桩/旧调用方只传 role 时兜底判断）
# 从能力清单派生，勿再单独维护
READONLY_ROLES = tuple(r for r, c in ROLE_CAPABILITIES.items() if c["readonly"])

# ==================== 研发流程配置（#419：flows.json + prompts/*.md） ====================
# 四层开放：前置环节槽位（pre.role）、后置流程（flows[]）、流程内节点（nodes[]，node_type 决定收尾语义）、
# 节点 agent（roles.<id> 展示/产物/模型 + 提示词文件）。加载期聚合全部错误一次性抛错（fail-fast）；
# 消费端自决（#430）：board 展示层降级（FLOW_CONFIG_ERROR 供 /guide 显示），dispatcher 侧 fail-closed
# 安全停泊——配置非法时不拉起、不推进、不收尾，仅告警留痕。
# 单一源拆分：能力/安全档位仍只存本文件 ROLE_CAPABILITIES；display/order/artifact/model/prompt_file
# 的唯一手写源 = flows.json（加载期双向键集校验 + display/artifact 对齐校验防双源漂移与静默改名）。
NODE_TYPES = ("artifact", "code", "review", "terminal", "human")
NODE_TYPE_LABELS = {"artifact": "产物", "code": "开发", "review": "评审",
                    "terminal": "收尾", "human": "人工"}
# 流程档位配色词表（#428）：语义色名，禁写 hex——色值唯一落点在 board.py CSS（词表↔CSS 由测试双向锁定）
# rose → fuchsia（2026-09-19）：该档原渲染色相与「评审中」徽标红几乎同色，改品红紫后名字与色相一致
FLOW_COLORS = ("green", "indigo", "fuchsia", "slate")
DEFAULT_FLOW_COLOR = "slate"
FLOW_CONFIG_PATH = REPO_DIR / "flows.json"


def _flow_cfg_errors(cfg, base_dir, out=None):
    """校验 flows.json 结构；返回错误清单（空 = 合法）。聚合报错，不 fail-fast 中途返回。
    out 传入 dict 时回填规范化 flows（节点补 artifact/name）。"""
    errs = []
    if not isinstance(cfg, dict):
        return ["顶层必须是 JSON 对象"]
    roles, flows, pre = cfg.get("roles"), cfg.get("flows"), cfg.get("pre")
    if not isinstance(roles, dict) or not roles:
        errs.append("roles 缺失或为空对象")
        roles = {}
    if not isinstance(flows, list) or not flows:
        errs.append("flows 缺失或为空数组")
        flows = []
    if not isinstance(pre, dict) or not str(pre.get("role") or "").strip():
        errs.append("pre.role 缺失")
        pre = {}
    # roles：键集与代码能力表双向一致（配置多/少一个角色都是错误——未知角色会按能力表兜底 fail-open 拉起会话）
    for role in sorted(set(roles) - set(ROLE_CAPABILITIES)):
        errs.append(f"roles.{role} 未在能力表 ROLE_CAPABILITIES 中登记（拒绝未知角色，防拼写错误 fail-open）")
    for role in sorted(set(ROLE_CAPABILITIES) - set(roles)):
        errs.append(f"roles 缺少能力表角色 {role}（键集必须与 ROLE_CAPABILITIES 完全一致）")
    orders = {}
    for role, entry in roles.items():
        if not isinstance(entry, dict):
            errs.append(f"roles.{role} 必须是对象")
            continue
        if not str(entry.get("display") or "").strip():
            errs.append(f"roles.{role}.display 不能为空")
        order = entry.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            errs.append(f"roles.{role}.order 必须是整数")
        elif order in orders:
            errs.append(f"roles.{role}.order={order} 与 roles.{orders[order]}.order 重复")
        else:
            orders[order] = role
        artifact = entry.get("artifact")
        if artifact is not None and not str(artifact).strip():
            errs.append(f"roles.{role}.artifact 必须是非空字符串或 null")
        if role == "dev" and artifact is not None:
            errs.append("roles.dev.artifact 必须为 null（dev 产物契约 = 代码变更）")
        model = entry.get("model")
        if model is not None and not isinstance(model, str):
            errs.append(f"roles.{role}.model 必须是字符串（可选）")
        skills = entry.get("skills")
        if skills is not None:
            if not (isinstance(skills, list) and all(isinstance(n, str) for n in skills)):
                errs.append(f"roles.{role}.skills 必须是技能名列表（可选）")
            else:
                for n in skills:
                    if not re.match(r"^[a-z0-9][a-z0-9-]{0,39}$", n):
                        errs.append(f"roles.{role}.skills 含非法技能名 {n!r}")
                    elif not ((Path(base_dir) / "prompts" / "skills" / n / "SKILL.md").is_file()
                              or (Path(base_dir) / "prompts" / "skills" / f"{n}.md").is_file()):
                        errs.append(f"roles.{role}.skills 的全局技能 {n} 不存在"
                                    f"（prompts/skills/{n}/SKILL.md 或 prompts/skills/{n}.md）")
        pf = entry.get("prompt_file")
        if not str(pf or "").strip():
            errs.append(f"roles.{role}.prompt_file 缺失")
            continue
        p = Path(str(pf))
        if p.is_absolute() or ".." in p.parts:
            errs.append(f"roles.{role}.prompt_file={pf} 必须是仓库内相对路径（拒绝绝对路径与 .. 越界）")
            continue
        if not (Path(base_dir) / p).is_file():
            errs.append(f"roles.{role}.prompt_file={pf} 文件不存在")
    # pre 槽位：必须命中配置角色且有产物（前置槽位契约要求产物非空，防配成 dev/merge_resolve）
    pre_role = str(pre.get("role") or "")
    if pre_role and pre_role not in roles:
        errs.append(f"pre.role={pre_role} 不在 roles 中")
    elif pre_role and not (roles.get(pre_role) or {}).get("artifact"):
        errs.append(f"pre.role={pre_role} 没有 artifact 产物，前置槽位必须有产物契约")
    # flows
    default_flow = cfg.get("default_flow")
    flow_ids = set()
    norm_flows = []

    def _enabled_of(flow):
        """启用标记缺省 true；非布尔值在下方逐条报错（此处按 true 计，避免一处笔误级联出多条错）"""
        v = flow.get("enabled", True)
        return v if isinstance(v, bool) else True

    # description 必填按**启用**条数判定：禁用流程不进推荐目录，不要求写适用场景
    enabled_cnt = sum(1 for f in flows if isinstance(f, dict) and _enabled_of(f))
    for i, flow in enumerate(flows):
        if not isinstance(flow, dict):
            errs.append(f"flows[{i}] 必须是对象")
            continue
        fid = str(flow.get("id") or "").strip()
        if not fid:
            errs.append(f"flows[{i}].id 不能为空")
        elif fid in flow_ids:
            errs.append(f"flows[{i}].id={fid} 重复")
        else:
            flow_ids.add(fid)
        if not str(flow.get("name") or "").strip():
            errs.append(f"flows[{i}].name 不能为空")
        # color（#428）：流程档位配色语义名，缺省 slate；非法值加载期报错（避免配错色名静默全灰）
        color = flow.get("color")
        if color is None:
            color = DEFAULT_FLOW_COLOR
        elif not isinstance(color, str) or color.strip() not in FLOW_COLORS:
            errs.append(f"flows[{i}]({fid}).color={color!r} 不在 {FLOW_COLORS}")
            color = DEFAULT_FLOW_COLOR
        else:
            color = color.strip()
        # enabled（流程软禁用）：缺省 true；false = 定义保留（可一键恢复）但不进推荐目录与可选列表
        enabled = flow.get("enabled", True)
        if not isinstance(enabled, bool):
            errs.append(f"flows[{i}]({fid}).enabled 必须是布尔（true/false），缺省视为 true")
            enabled = True
        # description：流程适用场景/判据的唯一落点（前置角色提示词只讲方法论，不再写死流程名）。
        # 存在多条**启用**流程时每条必填——没有判据的流程目录等于让模型瞎猜；单流程配置可省略。
        desc = flow.get("description")
        desc_txt = desc.strip() if isinstance(desc, str) else ""
        if desc is not None and not isinstance(desc, str):
            errs.append(f"flows[{i}]({fid}).description 必须是字符串")
        elif not desc_txt and enabled and enabled_cnt > 1:
            errs.append(f"flows[{i}]({fid}).description 必填（存在多条启用流程时，每条都要说明适用场景，供前置环节选流程）")
        elif len(desc_txt) > 300:
            errs.append(f"flows[{i}]({fid}).description 过长（{len(desc_txt)} 字，上限 300 字）")
        nodes = flow.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            errs.append(f"flows[{i}].nodes 必须是非空数组")
            continue
        seen_roles, terminals = set(), 0
        norm_nodes = []
        for j, node in enumerate(nodes):
            where = f"flows[{i}]({fid}).nodes[{j}]"
            if not isinstance(node, dict):
                errs.append(f"{where} 必须是对象")
                continue
            ntype = node.get("node_type")
            if ntype not in NODE_TYPES:
                errs.append(f"{where}.node_type={ntype!r} 不在 {NODE_TYPES}")
                continue
            if ntype == "human":
                errs.append(f"{where} human 节点本期未实现（仅预留枚举位）")
                continue
            if ntype == "terminal":
                terminals += 1
                if j != len(nodes) - 1:
                    errs.append(f"{where} terminal 必须是流程末节点")
                if node.get("role"):
                    errs.append(f"{where} terminal 是调度器动作节点，不得配 role")
                norm_nodes.append({"node_type": "terminal"})
                continue
            role = str(node.get("role") or "").strip()
            if not role:
                errs.append(f"{where}.role 缺失（非 terminal 节点必须指定 agent 角色）")
                continue
            if role not in roles:
                errs.append(f"{where}.role={role} 不在 roles 中")
                continue
            if role in seen_roles:
                errs.append(f"{where}.role={role} 在同一流程内重复（会话归属键 role:req_id 会冲突）")
            seen_roles.add(role)
            if ntype in ("artifact", "review") and not (roles[role].get("artifact") or None):
                errs.append(f"{where} node_type={ntype} 的角色 {role} 没有 artifact 产物契约")
            if ntype == "review" and (j == 0 or nodes[j - 1].get("node_type") not in ("artifact", "code")):
                errs.append(f"{where} review 节点的前驱必须是 artifact/code 节点（打回目标可复修）")
            norm_nodes.append({"role": role, "node_type": ntype,
                               "name": str(node.get("name") or "").strip() or None,
                               "artifact": roles[role].get("artifact") or None})
        if terminals != 1:
            errs.append(f"flows[{i}]({fid}) 必须恰有一个 terminal 节点，实际 {terminals} 个")
        norm_flows.append({"id": fid, "name": str(flow.get("name") or "").strip(),
                           "description": desc_txt, "enabled": bool(enabled), "color": color,
                           "nodes": norm_nodes})
    if default_flow not in flow_ids:
        errs.append(f"default_flow={default_flow!r} 不是已定义的 flow id")
    elif not any(f["id"] == default_flow and f["enabled"] for f in norm_flows):
        # 禁用默认流程 = 兜底目标消失（前置推荐缺失/非法时无处可去），加载期直接拦住
        errs.append(f"default_flow={default_flow!r} 指向已禁用的流程（默认流程必须启用）")
    if norm_flows and not any(f["enabled"] for f in norm_flows):
        errs.append("flows 全部被禁用（至少保留一条启用的流程）")
    if out is not None:
        out["flows"] = norm_flows
    return errs


def load_flow_config(path=None, base_dir=None):
    """读 flows.json + 校验 → 规范化配置（flows/roles/pre/default_flow + 提示词正文）。
    任何校验错误一次性抛 ValueError（fail-fast）。"""
    p = Path(path) if path else FLOW_CONFIG_PATH
    base = Path(base_dir) if base_dir else p.parent
    if not p.is_file():
        raise ValueError(f"流程配置文件不存在: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"流程配置文件非法 JSON: {p} 第 {e.lineno} 行 {e.msg}")
    errs = _flow_cfg_errors(raw, base)
    if errs:
        raise ValueError("flows.json 配置校验失败:\n- " + "\n- ".join(errs))
    norm = {}
    _flow_cfg_errors(raw, base, out=norm)
    roles = {}
    for role, entry in raw["roles"].items():
        pf = str(entry["prompt_file"])
        roles[role] = {"display": entry["display"], "order": entry["order"],
                       "artifact": entry.get("artifact"), "model": entry.get("model") or "",
                       "skills": list(entry.get("skills") or []),
                       "prompt_file": pf, "prompt": (Path(base) / pf).read_text(encoding="utf-8")}
    return {"version": raw.get("version", 1), "default_flow": raw["default_flow"],
            "pre": {"role": raw["pre"]["role"]}, "roles": roles, "flows": norm["flows"],
            "path": str(p)}


def _load_flow_config_safe():
    """模块级加载：不因配置错误阻断 import（消费端自决——board 降级展示，dispatcher 侧
    fail-closed 安全停泊：不拉起、不推进、不收尾，仅告警留痕）。"""
    try:
        return load_flow_config(), ""
    except ValueError as e:
        log.error("流程配置加载失败: %s", e)
        return None, str(e)


FLOW_CONFIG, FLOW_CONFIG_ERROR = _load_flow_config_safe()


def flows():
    return list((FLOW_CONFIG or {}).get("flows") or [])


def flow_by_id(flow_id):
    for f in flows():
        if f["id"] == flow_id:
            return f
    return None


def flow_names():
    return {f["id"]: f["name"] for f in flows()}


def flow_ids():
    return {f["id"] for f in flows()}


def enabled_flows():
    """启用的流程（软禁用）：禁用流程保留定义（一键恢复、历史卡展示名照旧），
    但不出现在前置推荐目录与 need_confirm 的可选列表里。"""
    return [f for f in flows() if f.get("enabled", True)]


def flow_enabled(flow_id):
    """流程是否存在且启用（未知 id → False）"""
    f = flow_by_id(flow_id)
    return bool(f and f.get("enabled", True))


def pre_role():
    return (((FLOW_CONFIG or {}).get("pre") or {}).get("role")) or ""


def default_flow_id():
    return (FLOW_CONFIG or {}).get("default_flow") or ""


# ==================== A3 项目清单（<仓库>/.loop/project.json） ====================
# 项目把「怎么装/测/起实例/重启」与本项目专有的指引、技能写在自己的仓库里，调度器读来填提示词的
# 「项目约定」「场景技能」两层（A2），全局模板不再携带任何单个项目的专有内容（A1）。
MANIFEST_REL = Path(".loop") / "project.json"
MANIFEST_COMMAND_KEYS = ("install", "test", "test_affected", "lint", "typecheck", "build")
MANIFEST_INSTANCE_KEYS = ("start", "stop", "status", "healthcheck")
MANIFEST_GUIDE_MAX_CHARS = 4000      # 单个指引/技能文件进 prompt 的字符上限
# GLOBAL_SKILLS_DIR / _SKILL_NAME_RE 定义在 acp_modules/skills.py（技能 v2），文末回挂


def _read_capped(path, limit=MANIFEST_GUIDE_MAX_CHARS):
    text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    if len(text) > limit:
        text = text[:limit] + f"\n……（超过 {limit} 字符已截断，完整内容见 {path}）"
    return text


def _repo_rel_file(repo, rel):
    """清单里引用的文件：必须是仓库内相对路径且存在；返回 (Path|None, 原因)。"""
    try:
        rp = Path(str(rel))
        if rp.is_absolute() or ".." in rp.parts:
            return None, "必须是仓库内相对路径"
        root = Path(repo).resolve()
        full = (root / rp).resolve()
        # 解析软链后仍须落在仓库内：防 .loop/ 下放软链把仓库外文件内容带进 prompt
        if not full.is_relative_to(root):
            return None, "解析后越出仓库（软链），拒绝"
        if not full.is_file():
            return None, "文件不存在"
        return full, ""
    except (OSError, ValueError) as e:
        return None, f"路径不可用（{e.__class__.__name__}）"


def load_project_manifest(repo):
    """读 <repo>/.loop/project.json → 归一化清单。每次调用现读（文件很小，改完即生效）。
    缺文件 = 未配置（path=None、errors 为空）；非法字段逐项宽容降级并记入 errors（doctor 展示），
    绝不抛——清单是项目自己的数据，写错不能卡住调度。
    返回 {path, errors, commands, instance, guides, skills, high_risk_paths, restart, visual, agents_md}。"""
    out = {"path": None, "errors": [], "commands": {}, "instance": {}, "guides": {}, "skills": {},
           "high_risk_paths": [], "restart": None, "visual": {}, "agents_md": {}}
    try:
        f = Path(repo) / MANIFEST_REL
        if not f.is_file():
            return out
        out["path"] = str(f)
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        out["errors"].append(f"清单读取/解析失败: {e}")
        return out
    errs = out["errors"]
    if not isinstance(raw, dict):
        errs.append("清单顶层必须是对象")
        return out
    commands = raw.get("commands")
    if isinstance(commands, dict):
        for key, val in commands.items():
            if key not in MANIFEST_COMMAND_KEYS:
                errs.append(f"commands.{key} 不是已知命令键（{'/'.join(MANIFEST_COMMAND_KEYS)}）")
            elif not isinstance(val, str):
                errs.append(f"commands.{key} 必须是字符串")
            elif val.strip():
                out["commands"][key] = val.strip()
    elif commands is not None:
        errs.append("commands 必须是对象")
    instance = raw.get("instance")
    if isinstance(instance, dict):
        for key in MANIFEST_INSTANCE_KEYS:
            val = instance.get(key)
            if isinstance(val, str) and val.strip():
                out["instance"][key] = val.strip()
            elif val is not None and not isinstance(val, str):
                errs.append(f"instance.{key} 必须是字符串")
        pr = instance.get("port_range")
        if pr is not None:
            if (isinstance(pr, list) and len(pr) == 2
                    and all(isinstance(x, int) and not isinstance(x, bool) for x in pr) and pr[0] <= pr[1]):
                out["instance"]["port_range"] = list(pr)
            else:
                errs.append("instance.port_range 必须是 [起, 止] 两个整数")
    elif instance is not None:
        errs.append("instance 必须是对象")
    guides = raw.get("guides")
    if isinstance(guides, dict):
        for name, spec in guides.items():
            rel, roles = (spec, None) if isinstance(spec, str) else (
                (spec.get("file"), spec.get("roles")) if isinstance(spec, dict) else (None, None))
            if not isinstance(rel, str) or not rel.strip():
                errs.append(f"guides.{name} 必须是文件路径或 {{file, roles}}")
                continue
            full, why = _repo_rel_file(repo, rel)
            if full is None:
                errs.append(f"guides.{name}={rel} {why}")
                continue
            if roles is not None and not (isinstance(roles, list) and all(isinstance(r, str) for r in roles)):
                errs.append(f"guides.{name}.roles 必须是角色名列表")
                roles = None
            try:
                text = _read_capped(full)
            except Exception as e:
                errs.append(f"guides.{name}={rel} 读取失败（{e.__class__.__name__}）")
                continue
            out["guides"][str(name)] = {"file": str(rel), "roles": roles, "text": text}
    elif guides is not None:
        errs.append("guides 必须是对象")
    skills = raw.get("skills")
    if isinstance(skills, dict):
        for role, names in skills.items():
            if isinstance(names, list) and all(isinstance(n, str) and _SKILL_NAME_RE.match(n) for n in names):
                out["skills"][str(role)] = list(names)
            else:
                errs.append(f"skills.{role} 必须是技能名列表（小写字母/数字/连字符）")
    elif skills is not None:
        errs.append("skills 必须是对象")
    hrp = raw.get("high_risk_paths")
    if isinstance(hrp, list) and all(isinstance(x, str) for x in hrp):
        out["high_risk_paths"] = [x for x in hrp if x.strip()]
    elif hrp is not None:
        errs.append("high_risk_paths 必须是字符串列表")
    for key in ("restart", "visual", "agents_md"):
        val = raw.get(key)
        if isinstance(val, dict):
            out[key] = val
        elif val is not None:
            errs.append(f"{key} 必须是对象")
    return out


# A4 AGENTS.md 长度上限（常驻进每个 agent 上下文，膨胀即全员降智）：默认 150 行 / 12000 字符，
# 项目可在清单 agents_md.{max_lines,max_chars} 调整。超限只告警不阻断（doctor 展示）。
AGENTS_MD_MAX_LINES = 150
AGENTS_MD_MAX_CHARS = 12000


def agents_md_problems(repo, manifest=None):
    """A4：检查 <repo>/AGENTS.md 是否存在、是否超长；返回问题描述列表（空 = 无问题）。纯读。"""
    f = Path(repo) / "AGENTS.md"
    if not f.is_file():
        return [f"{f} 不存在（agent 没有项目规范可读；模板见 docs/templates/AGENTS.template.md）"]
    limits = (manifest or {}).get("agents_md") or {}
    def _limit(key, default):
        v = limits.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else default
    max_lines, max_chars = _limit("max_lines", AGENTS_MD_MAX_LINES), _limit("max_chars", AGENTS_MD_MAX_CHARS)
    text = f.read_text(encoding="utf-8", errors="replace")
    n_lines, n_chars = len(text.splitlines()), len(text)
    out = []
    if n_lines > max_lines:
        out.append(f"{f} 共 {n_lines} 行，超过上限 {max_lines} 行")
    if n_chars > max_chars:
        out.append(f"{f} 共 {n_chars} 字符，超过上限 {max_chars} 字符")
    if out:
        out.append("新增规则须替换旧规则或下沉到 .loop/guides、.loop/skills（见 docs/project-manifest.md）")
    return out


def skill_text(name, repos=()):
    """技能片段正文（技能 v2 发现规则，见 acp_modules/skills.py）：项目 <repo>/.loop/skills/ 优先，
    其次本机覆盖 prompts.local/skills/，最后全局 prompts/skills/；每个作用域 <名>/SKILL.md 优先于平铺 <名>.md，
    SKILL.md 只取 frontmatter 之后的正文。草稿不进真实会话；名字非法、找不到或不可用（读取/解析失败）→ None。
    全局目录取本模块的 GLOBAL_SKILLS_DIR（测试可在 acp_shared 上替换）。"""
    if not isinstance(name, str) or not _SKILL_NAME_RE.match(name):
        return None
    try:
        sk = find_skill(name, repos, global_dir=GLOBAL_SKILLS_DIR)
    except Exception as e:      # 读不了就当没有：绝不因技能文件把建 prompt 打断
        log.warning(f"技能 {name} 读取失败: {e}")
        return None
    if sk is None or sk.errors:
        if sk is not None:
            log.warning(f"技能 {name} 不可用（{sk.path}）: {'；'.join(sk.errors)}")
        return None
    return sk.text


def role_meta(role, key):
    """读角色展示/内容档位（display/order/artifact/model/prompt_file）；未知角色返回 None。"""
    entry = ((FLOW_CONFIG or {}).get("roles") or {}).get(role)
    return entry.get(key) if entry else None


def _project_role_model(project, role):
    """层①：projects.json 项目级模型覆盖（#4）。每次调用现读；project 为空/未知、
    条目为字符串简写、models 非 dict、值非字符串或空白 → 返回 None（fail-safe，不抛）。"""
    if not project or not role:
        return None
    try:
        raw = json.loads(Path(PROJECTS_FILE).read_text())
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    entry = raw.get(str(project))
    if not isinstance(entry, dict):
        return None
    models = entry.get("models")
    if not isinstance(models, dict):
        return None
    val = models.get(str(role))
    if not isinstance(val, str):
        return None
    return val.strip() or None


def role_runtime_detail(role, project=None):
    """项目角色 → 项目默认 → 流程角色 → 环境变量 → kimi；显式非法值 fail-closed。"""
    entry = {}
    if project:
        try:
            raw = json.loads(Path(PROJECTS_FILE).read_text())
            if not isinstance(raw, dict):
                raise ValueError("projects.json 必须为对象")
            entry = raw.get(str(project), {})
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise ValueError(f"运行体项目配置读取失败：{exc}") from exc
    if isinstance(entry, dict):
        runtimes = entry.get("runtimes", {})
        if not isinstance(runtimes, dict):
            raise ValueError("项目 runtimes 必须为对象")
        if role in runtimes:
            value, source = runtimes[role], "project_role"
        elif "runtime" in entry:
            value, source = entry["runtime"], "project"
        else:
            value, source = None, None
    else:
        value, source = None, None
    if source is None:
        role_entry = ((FLOW_CONFIG or {}).get("roles") or {}).get(role) or {}
        if "runtime" in role_entry:
            value, source = role_meta(role, "runtime"), "role"
        elif "LOOP_AGENT_RUNTIME" in os.environ:
            value, source = os.environ["LOOP_AGENT_RUNTIME"], "env"
        else:
            value, source = "kimi", "global"
    agent_runtime(value)
    return value, source


def role_runtime(role, project=None):
    return role_runtime_detail(role, project)[0]


def role_model_detail(role, project=None):
    """角色生效模型四层兜底链（唯一判定点，调用方不得自行重排层序）：
    ① projects.json[project]["models"][role] → ② flows.json roles.<id>.model →
    ③ KIMI_MODEL_<ROLE 大写> 环境变量 → ④ 全局 ACP_MODEL。
    返回 (model, source)，model 恒非空；source ∈ {"project","role","env","global"}。"""
    project_model = _project_role_model(project, role)
    if project_model:
        return project_model, "project"
    configured = role_meta(role, "model")
    if configured:
        return configured, "role"
    env_val = os.environ.get("KIMI_MODEL_" + str(role).upper(), "")
    if env_val:
        return env_val, "env"
    return ACP_MODEL, "global"


def role_model(role, project=None):
    """角色生效模型（值视图，恒非空）：四层兜底链见 role_model_detail（唯一判定点）。"""
    return role_model_detail(role, project)[0]


# ==================== #11 别名池静态校验与降级链 ====================
# role_model_detail 之上的一层「池命中判定」：spawn 前发现角色配置值不在 harness 别名池时
# 直接按兜底链回落，不为一个角色的配置错误卡住整张卡；不改链本身（I1）。

def session_homes():
    """候选 harness home（有序去重）：$KIMI_CODE_HOME（已设时）→ ~/.kimi-code-deepseek →
    ~/.kimi-code → 其余 ~/.kimi-code*（glob 兜未来新增 home）。候选序与 board.session_homes 一致
    （有意的 IO 双份：board 侧承载 mock.patch 测试面；解析规则仍单份，见
    model_alias_entries_from_config）。"""
    homes = []
    env_home = os.environ.get("KIMI_CODE_HOME")
    if env_home:
        homes.append(Path(env_home).expanduser())
    homes.extend([Path.home() / ".kimi-code-deepseek", Path.home() / ".kimi-code"])
    try:
        homes.extend(sorted(Path.home().glob(".kimi-code*")))
    except OSError:
        pass
    seen, out = set(), []
    for h in homes:
        if str(h) not in seen:
            seen.add(str(h))
            out.append(h)
    return out


def harness_config_path():
    """session_homes() 候选序首个存在 config.toml 的 home → (realpath, home_str)，无则 (None, None)。"""
    for h in session_homes():
        try:
            p = h / "config.toml"
            if p.is_file():
                return Path(os.path.realpath(p)), str(h)
        except OSError:
            continue
    return None, None


def model_alias_entries_from_config(raw):
    """config.toml 已解析 dict → 别名池条目 [{"alias","provider","support_efforts"}]（保序去重）。
    与 board._model_alias_pool_entries 规则**同源**（#11 唯一共享点）：只读 [models] 表键、
    default_model、旧形态 providers.<p>.model（拼 p/<model>）三类字段；provider 判定沿 board 口径
    （models.<别名>.provider 命中现有 [providers] 段 → 该厂商；合成别名 p/upstream → p；否则 ""）；
    含 support_efforts、type（所属厂商的 type）、protocol（模型声明的 wire protocol）；
    #18 另含 has_model_entry（该别名是否为 [models] 表键，合成别名 False）与 max_context_size
    （表条目的整数值，缺失/非整数 → None）；
    **绝不读取/回传 api_key**。"""
    if not isinstance(raw, dict):
        return []
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}

    def _provider_of(alias):
        cfg = models.get(alias)
        if isinstance(cfg, dict):
            p = cfg.get("provider")
            if isinstance(p, str) and p and p in providers:
                return p
        pname, _, upstream = str(alias).partition("/")
        pcfg = providers.get(pname)
        if isinstance(pcfg, dict) and isinstance(pcfg.get("model"), str) \
                and pcfg["model"].strip() and pcfg["model"].strip() == upstream:
            return pname
        return ""

    def _support_efforts_of(alias):
        # #29：显式空集 [] 与「键缺失/非列表」必须可区分——前者是「模型声明无可选档位」（消费侧
        # 据此不发请求），后者是「未知」（fail-open 维持现行为）。故 list 一律返回（可为 []）。
        cfg = models.get(alias)
        if isinstance(cfg, dict):
            eff = cfg.get("support_efforts")
            if isinstance(eff, list):
                return [str(x) for x in eff if isinstance(x, str) and x.strip()]
        return None

    def _type_of(provider):
        pcfg = providers.get(provider)
        if isinstance(pcfg, dict):
            t = pcfg.get("type")
            if isinstance(t, str) and t.strip():
                return t.strip()
        return ""

    def _protocol_of(alias):
        cfg = models.get(alias)
        if isinstance(cfg, dict):
            p = cfg.get("protocol")
            if isinstance(p, str) and p.strip():
                return p.strip()
        return ""

    def _max_context_size_of(alias):
        cfg = models.get(alias)
        if isinstance(cfg, dict):
            v = cfg.get("max_context_size")
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        return None

    entries, seen = [], set()

    def _add(alias):
        alias = str(alias)
        if alias and alias not in seen:
            seen.add(alias)
            provider = _provider_of(alias)
            entries.append({"alias": alias, "provider": provider,
                            "support_efforts": _support_efforts_of(alias),
                            "type": _type_of(provider), "protocol": _protocol_of(alias),
                            "has_model_entry": alias in models,
                            "max_context_size": _max_context_size_of(alias)})

    for k in models:
        _add(k)
    dm = raw.get("default_model")
    if isinstance(dm, str) and dm.strip():
        _add(dm.strip())
    for pname, pcfg in providers.items():
        if isinstance(pcfg, dict) and isinstance(pcfg.get("model"), str) and pcfg["model"].strip():
            _add(f"{pname}/{pcfg['model'].strip()}")
    return entries


# harness provider `type` 合法词表（#13）：harness 判定 wire protocol 的两条来源之并集——
# ProtocolSchema 枚举（anthropic/openai/openai_responses/google-genai）∪ 已注册 provider
# definition id（anthropic/openai/openai_responses/google-genai/kimi）。证据见 self-test.md
# 「词表取证」小节（`strings ~/.kimi-code/bin/kimi`）；大小写敏感精确匹配（"OpenAI" 不在词表）。
HARNESS_PROVIDER_TYPES = ("openai", "anthropic", "openai_responses", "google-genai", "kimi")
HARNESS_PROVIDER_TYPES_HINT = " / ".join(HARNESS_PROVIDER_TYPES)


def context_size_problem(value):
    """max_context_size 完整性判定（#18 单一源）：返回人类可读单行原因，合法返回 ""。
    None（键缺失）→ 缺必填字段；非 int（含 bool）或 <1 → 值非法。harness 侧与看板 GET 共用。"""
    if value is None:
        return "缺少必填字段 max_context_size"
    if isinstance(value, bool) or not isinstance(value, int):
        return f"max_context_size 需为正整数（当前非整数：{type(value).__name__}）"
    if value < 1:
        return f"max_context_size 需为正整数（≥1，当前为 {value}）"
    return ""


# max_context_size 预填默认值（#18 单一源；经 GET /api/model-settings 的 context_defaults 注入前端）：
# 规则按顺序首条命中 —— type 为空串表通配，match 为模型名大小写不敏感子串（前端匹配），value 为官方上下文窗口。
# 仅作 UI 可改预填，**不参与任何服务端校验**；未命中留空（不猜通用默认值）。取值来自各厂商公开上下文窗口。
CONTEXT_SIZE_DEFAULTS = (
    {"type": "kimi", "match": "k3", "value": 1048576},
    {"type": "kimi", "match": "kimi-for-coding", "value": 262144},
    {"type": "", "match": "claude", "value": 200000},
    {"type": "", "match": "gpt-4.1", "value": 1047576},
)


def model_alias_usable(entry):
    """条目可用性判定（#13/#18）：返回 (usable, reason)。
    #18 置前必填判定：`[models.<别名>]` **表条目**（has_model_entry 为真）缺/非法 max_context_size
    → 不可用（官方唯一必填字段，缺失会被 session/set_config_option 拒绝）；
    其余规则同 #13：模型显式声明 protocol → 可用（与 provider type 无关，harness 优先取 model.protocol）；
    否则要求所属厂商存在且 type ∈ HARNESS_PROVIDER_TYPES。不可用时 reason 为人类可读单行原因。
    兼容：条目无 has_model_entry 键（旧调用/非表语义）→ 不因该字段判不可用。"""
    if not isinstance(entry, dict):
        return False, "条目形态异常"
    if entry.get("has_model_entry"):
        problem = context_size_problem(entry.get("max_context_size"))
        if problem:
            return False, problem
    protocol = entry.get("protocol")
    if isinstance(protocol, str) and protocol.strip():
        return True, ""
    provider = entry.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        return False, "provider 缺失或指向不存在的厂商"
    ptype = entry.get("type")
    if not isinstance(ptype, str) or not ptype.strip():
        return False, f"厂商 {provider} 未声明 type"
    if ptype not in HARNESS_PROVIDER_TYPES:
        return False, (f'厂商 {provider} 的 type "{ptype}" 非法'
                       f"（合法值：{HARNESS_PROVIDER_TYPES_HINT}）")
    return True, ""


def model_alias_pool():
    """harness 别名池（#11 静态校验用）：返回 ({alias: 可用性条目}|None, home, warning)。
    池不可用（无 config.toml / 解析失败 / 空池 / 无 tomllib）→ (None, home, warning)（fail-open，
    调用方不校验、不降级，行为与改动前逐字一致）。"""
    path, home = harness_config_path()
    if not home:
        return None, None, "未找到 config.toml（harness home 无配置），别名池校验跳过"
    try:
        import tomllib
    except ImportError:  # Python < 3.11：无 tomllib，池不可用（不新增运行时依赖）
        return None, home, "当前 Python <3.11 无 tomllib，别名池校验跳过"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 解析失败降级
        return None, home, f"config.toml 解析失败（{e}），别名池校验跳过"
    entries = model_alias_entries_from_config(raw)
    if not entries:
        return None, home, "config.toml 中未找到模型别名，别名池校验跳过"
    pool = {}
    for e in entries:
        usable, reason = model_alias_usable(e)
        pool[e["alias"]] = {"provider": e["provider"], "type": e["type"],
                            "protocol": e["protocol"], "usable": usable, "reason": reason,
                            # #29：per-model 思考档位选档的数据源（list 可为 []，None=未声明）
                            "support_efforts": e.get("support_efforts")}
    return pool, home, ""


_POOL_UNSET = object()


def _pool_lookup(pool, alias):
    """池查询归一化（#13）：返回 (in_pool, usable, reason)。
    set/frozenset（旧口径）→ 成员一律可用；dict（新口径，alias → 可用性条目）→ 取条目
    usable/reason（缺失视为可用，容错注入）；未知形态 fail-open 视为可用。"""
    if isinstance(pool, (set, frozenset)):
        return (alias in pool), True, ""
    if isinstance(pool, dict):
        if alias not in pool:
            return False, False, "不在别名池"
        entry = pool.get(alias)
        if isinstance(entry, dict):
            return True, bool(entry.get("usable", True)), (entry.get("reason") or "不可用")
        return True, True, ""
    try:
        return (alias in pool), True, ""
    except Exception:  # noqa: BLE001 - 未知形态 fail-open
        return True, True, ""


def resolve_role_model(role, project=None, pool=_POOL_UNSET):
    """#11 角色模型静态校验 + 降级链（在 role_model_detail 之上加「池命中 + 可用性校验」，不改链本身）。

    逐层（① project → ② role → ③ env，与 role_model_detail 同款层序）取首个非空且**命中别名池且可用**
    的值；三层全未命中 → 链末端 ACP_MODEL（source="global"）。degraded = 生效值 != 四层链配置值。
    池不可用（pool is None）→ fail-open，原样返回 role_model_detail，degraded=False。
    role 为空/未知、值不在池、值在池但不可用（#13）→ 走上述回落。纯函数；pool 可注入（测试用）。

    返回 {"model","source","configured","configured_source","degraded","reason"}。
    **边界**：覆盖「不在池」与「在池但不可用」（provider type 非法/缺 protocol，model_alias_usable
    判定）两类；形态不匹配/被拒仍由落定回读判定（ACPSession._apply_model）承担。"""
    configured, configured_source = role_model_detail(role, project)
    runtime_id = role_runtime(role, project)
    if not agent_runtime(runtime_id)["supports_model_pool"]:
        runtime_skip(runtime_id, "harness 模型别名池校验")
        pool = None
    if pool is _POOL_UNSET:
        pool, _home, warning = model_alias_pool()
        if warning:
            log.warning(f"角色模型静态校验跳过（role={role}）：{warning}")
    if pool is None:
        return {"model": configured, "source": configured_source,
                "configured": configured, "configured_source": configured_source,
                "degraded": False, "reason": None}
    effective, source = None, None
    for value, src in (
        (_project_role_model(project, role), "project"),
        (role_meta(role, "model"), "role"),
        (os.environ.get("KIMI_MODEL_" + str(role).upper(), ""), "env"),
    ):
        val = str(value).strip() if value else ""
        if val:
            _in_pool, _usable, _why = _pool_lookup(pool, val)
            if _in_pool and _usable:
                effective, source = val, src
                break
    if effective is None:
        effective, source = ACP_MODEL, "global"
    degraded = effective != configured
    reason = None
    if degraded:
        c_in, c_usable, c_why = (_pool_lookup(pool, configured) if configured
                                 else (False, False, ""))
        g_in, g_usable, g_why = _pool_lookup(pool, ACP_MODEL)
        if configured and c_in and not c_usable:
            if source == "global" and not g_usable:
                reason = (f"配置值 {configured} 不可用（{c_why}）；"
                          f"全局兜底值 {ACP_MODEL} 亦不可用（{g_why}），仍以兜底值继续")
            else:
                reason = f"配置值 {configured}（来源 {configured_source}）不可用（{c_why}），已逐层回落"
        elif source == "global":
            if g_in and not g_usable:
                reason = (f"配置值不在别名池；全局兜底值 {ACP_MODEL} 亦不可用（{g_why}），"
                          f"仍以兜底值继续")
            elif g_in:
                reason = "配置值不在别名池"
            else:
                reason = "配置值与全局兜底值均不在别名池（池可能过期）"
                log.warning(f"角色 {role} 的配置值 {configured} 与全局兜底值 {ACP_MODEL} "
                            f"均不在别名池（池可能过期）")
        else:
            reason = f"配置值 {configured}（来源 {configured_source}）不在别名池，已逐层回落"
    return {"model": effective, "source": source, "configured": configured,
            "configured_source": configured_source, "degraded": degraded, "reason": reason}




# C2 会话预算：每轮 prompt 的工具调用步数上限（调度器强制，取代提示词里的「约 200 步」文字约束）。
# 单次运行默认 4 小时（AGENT_TIMEOUT），步数按长任务放宽，只拦失控轮（历史失控轮 653 步出现在 42 分钟内）。
# 项目可在 projects.json 的 budgets.<角色> 覆盖。
DEFAULT_ROLE_MAX_STEPS = {"dev": 1000, "accept": 500, "code_review": 500, "analysis": 400,
                          "proposal": 400, "tech": 400, "tech_review": 400, "merge_resolve": 200}


def role_budget(role, project=None):
    """C2 角色会话预算：{"max_steps": int|None, "max_minutes": int|None}。
    projects.json[project]["budgets"][role] 的正整数值覆盖默认；max_minutes 无默认（不改既有时限）。
    每次调用现读；非法值忽略。"""
    out = {"max_steps": DEFAULT_ROLE_MAX_STEPS.get(str(role)), "max_minutes": None}
    if not project:
        return out
    try:
        raw = json.loads(Path(PROJECTS_FILE).read_text())
    except Exception:
        return out
    entry = raw.get(str(project)) if isinstance(raw, dict) else None
    budgets = entry.get("budgets") if isinstance(entry, dict) else None
    cfg = budgets.get(str(role)) if isinstance(budgets, dict) else None
    if isinstance(cfg, dict):
        for key in ("max_steps", "max_minutes"):
            val = cfg.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                out[key] = val
    return out


def project_external_roles(project):
    """主 Agent 协作模式：projects.json[project]["external_roles"] 声明由外部主 Agent 负责的角色
    （如 ["tech"]）。调度器走到这些角色时不拉起会话、只停卡等待外部产出。每次调用现读；
    缺省/非列表 → []，非字符串元素忽略。"""
    if not project:
        return []
    try:
        raw = json.loads(Path(PROJECTS_FILE).read_text())
    except Exception:
        return []
    entry = raw.get(str(project)) if isinstance(raw, dict) else None
    roles = entry.get("external_roles") if isinstance(entry, dict) else None
    return [r for r in roles if isinstance(r, str) and r.strip()] if isinstance(roles, list) else []


def project_escalation_model(project):
    """C3 评审连续打回时作者升级用的模型别名：projects.json[project]["escalation_model"]。
    每次调用现读；缺省/非字符串/文件不可读 → None（不升级）。"""
    if not project:
        return None
    try:
        raw = json.loads(Path(PROJECTS_FILE).read_text())
    except Exception:
        return None
    entry = raw.get(str(project)) if isinstance(raw, dict) else None
    val = entry.get("escalation_model") if isinstance(entry, dict) else None
    return (val.strip() or None) if isinstance(val, str) else None


def role_prompt(role):
    """角色模板正文：本机覆盖 prompts.local/<file>（提示词存储 role/<id>，现读、改完无需重启）优先，
    否则为 flows.json 加载时读入的默认 prompts/<role>.md。未知角色 → 空串。"""
    default = role_meta(role, "prompt") or ""
    if not default:
        return ""
    try:
        import prompt_store
        ov = prompt_store.override_text(prompt_store.ROLE_PREFIX + str(role))
    except Exception as e:      # 覆盖读不了按默认走（覆盖是本机数据，坏了不能卡调度）
        log.warning("角色 %s 覆盖模板读取失败，按默认: %s", role, e)
        ov = None
    return ov if ov is not None else default


# ==================== 拆分子模块回挂（acp_modules/，名字逐个列出：含下划线名，供调用方与测试沿用 ac.<名>） ====================
from acp_modules.limits import (  # noqa: F401,E402
    DBQ_SCRIPT, BOARD_DB,
    READONLY_TOOL_SERVER, TOOL_PROFILE_FEISHU_ASSISTANT, TOOL_PROFILES, ASSISTANT_DENIED_METHODS,
    ASSISTANT_DENIED_TERMINAL_PREFIX, DIR_MAX_ENTRIES, FILE_MAX_LINES, FILE_MAX_LINES_LIMIT,
    FILE_MAX_BYTES, DBQ_TIMEOUT, MYSQL_MAX_ROWS, MYSQL_MAX_CELL,
    MYSQL_CONNECT_TIMEOUT, MYSQL_QUERY_TIMEOUT_MS, GIT_LOG_DEFAULT_COUNT, GIT_LOG_MAX_COUNT,
    GIT_LOG_TIMEOUT, GIT_LOG_LINE_MAX, GIT_LOG_EMPTY_HINT, GIT_LOG_FORMAT,
    SEARCH_MAX_MATCHES, SEARCH_MAX_SCAN, SEARCH_MAX_BYTES, SEARCH_LINE_MAX,
    GIT_STATUS_MAX_LINES, TOOL_OUTPUT_MAX_CHARS, GIT_DIFF_MAX_LINES, GIT_TOOL_TIMEOUT,
    GIT_DIFF_ALLOWED_ARGS, COMMIT_RE, READONLY_CMDS, READONLY_FLAG_RE,
    NUM_RE, FIND_BANNED, NULL_REDIRECT_RE, _READ_VERB_RE,
)
from acp_modules.command_policy import (  # noqa: F401,E402
    PLAYWRIGHT_TOOL_PREFIX, PLAYWRIGHT_ALLOWED_TOOLS, PLAYWRIGHT_DENIED_TOOLS, TAPD_TOOL_PREFIX,
    TAPD_READONLY_TOOLS, HTTP_URL_RE, _URL_TOKEN_RE, _url_protocol_violation,
    KILL_REDLINE_TMPL, REDLINE_CANCEL_SIGNAL, _KILL_VERB_RE, _KILL_PID_RE,
    _REDLINE_WORD_RE, _APPROVAL_PREFIX_RE, _PROC_CTRL_VERBS, _EXEC_ENTRY_VERBS,
    _EXEC_CHAIN_VERBS, _DATA_VERBS, _PY_EXEC_CALL_RE, _SIG_NAMES,
    _CHAIN_SHORT_OPT_WITH_ARG, _CHAIN_LONG_OPT_WITH_ARG, _TIMEOUT_DURATION_RE, _bare_tokens,
    _strip_quoted_token, _exec_code_raw, _subprocess_call_args, _split_exec_units,
    _kill_pid_targets, _chain_inner, _ambiguous_chain_hit, _stdin_redline,
    _analyze_unit, _exec_code_check, _extract_command, _kill_redline_hit_legacy,
    kill_redline_text, append_kill_redline, kill_redline_hit, GIT_WRITE_SUBCMDS,
    _MAIN_WS_PATH_VERBS, _GIT_GLOBAL_OPT_WITH_ARG, _redirect_targets, _main_ws_path_hit,
    _git_c_write_hit, _LOOSE_WRITE_HINT_RE, _main_ws_loose_hit, main_ws_write_hit,
    dbq_command_safe, _split_shell, _shell_tokens, strip_null_redirects,
    readonly_cmd_safe, readonly_pipeline_safe, _next_top_level_and, strip_cd_prefix,
    GIT_RO_SUBCMDS, GIT_ADD_OK_FLAGS, GIT_COMMIT_OK_FLAGS, _git_path_in_roots,
    _UNITTEST_DISCOVER_NOARG_FLAGS, _UNITTEST_DISCOVER_ARG_FLAGS, python_safe_cmd, _git_ro_free_args_safe,
    _git_branch_safe, _git_tag_safe, _git_worktree_safe, _git_remote_safe,
    _git_config_safe, _git_add_safe, _git_commit_safe, git_cmd_safe,
    _git_single_safe, _git_merge_safe, _git_checkout_safe, _git_switch_safe,
    _git_stash_safe,
)
from acp_modules.skills import (  # noqa: F401,E402
    GLOBAL_SKILLS_DIR, SKILL_FILE, SKILL_MAX_CHARS, SKILL_DESC_MAX, _SKILL_NAME_RE,
    SKILL_ACTIVATIONS, SKILL_STATUSES, SKILL_SCOPES, SKILL_META_KEYS, Skill, Selection,
    prompts_local_dir, local_skills_dir, valid_skill_name, parse_frontmatter, render_frontmatter,
    compose_skill_file, load_skill, skill_chain, find_skill, discover, parse_notes_skills,
    glob_match, paths_from_text, auto_hit, select_skills, skills_block, catalog_block,
    lint as skill_lint, lint_level as skill_lint_level, materialize_for_claude,
)
from acp_modules.readonly_tools import (  # noqa: F401,E402
    READONLY_TOOL_GUIDE, SENSITIVE_ENV_RE, SENSITIVE_SSH_KEY_RE, SENSITIVE_SUFFIXES,
    SENSITIVE_KEYWORDS, sensitive_path_blocked, resolve_path_safe, ro_read_lines,
    ro_read_continuation, ro_fit_lines, cap_tool_output, ro_search_hits,
    ro_glob_paths, git_log_repo_ok, git_log_path_filter, git_log_count_ok,
    git_log_env, dbq_sql_safe, _read_env_file, _mysql_connector,
    ReadonlyMcpServer, _readonly_mcp_handler, parse_usage, _USAGE_NUM_KEYS,
    normalize_provider_usage, _USAGE_CACHE_LINE_RE, parse_usage_cache, parse_usage_percent,
    pct_value,
)


class ACPSession:
    """kimi acp 子进程的 JSON-RPC 客户端：驱动会话并把流式事件上报事件接收方。
    协议：stdin/stdout 上每行一条 JSON-RPC 2.0 消息（NDJSON）。
    role/readonly 决定审批路由：dev 全放行；readonly=True 严格只读（仅只读 MCP 工具 +
    产物目录内 Write + dbq 只读查库 + 只读命令白名单）；accept 拒绝提交类命令。
    tool_profile（#24）另开一档：'feishu_assistant' 只注入助手业务 MCP、审批只放行其工具、
    反向 fs/terminal 全拒、initialize 能力声明全 false；None（缺省）= 上述按角色路由逐字不变。
    event_sink 为事件上报回调（dispatcher 走 HTTP 上报看板；board 建卡会话直接写 DB）。"""

    def __init__(self, req_id, workdir, logf, role, artifact_root, project=None,
                 event_sink=None, readonly=False, extra_allow_prefixes=(),
                 extra_mcp_servers=(), allow_fs_write=True, readonly_roots=None,
                 write_roots=None, kimi_bin=None, deny_builtin_write=False, mysql_config=None,
                 allow_git_commit=False, allow_git_merge=False, main_ws_roots=None, model=None,
                 allow_python_safe=False, python_roots=None, python_compile_roots=None,
                 activity_sink=None, tool_profile=None, output_guard=None, runtime=None):
        self.runtime_id = runtime if runtime is not None else role_runtime(role, project)
        self.runtime = agent_runtime(self.runtime_id)
        if not self.runtime["supports_restricted_roles"] and (
                readonly or role_capability(role, "readonly")
                or role_capability(role, "bash") != "allow" or tool_profile
                or deny_builtin_write or not allow_fs_write):
            raise ValueError(f"运行体 {self.runtime_id} 尚未验证受限工具等价约束，拒绝启动受限/只读角色 {role}")
        self.req_id = req_id
        self.workdir = Path(workdir)
        self.role = role
        self.artifact_root = Path(artifact_root)
        self.project = project
        self.event_sink = event_sink          # callable(req_id, kind, content, session_id)
        self.readonly = readonly              # 只读审批路由（dispatcher: role in READONLY_ROLES）
        # #24 助手专用构造档：None = 普通会话（行为逐字不变）；'feishu_assistant' = 三层拒绝矩阵
        # （见 _is_assistant_profile）。未知值 fail-fast——构造即报错，不静默降级为普通会话
        self.tool_profile = tool_profile or None
        if self.tool_profile not in (None,) + TOOL_PROFILES:
            raise ValueError(f"未知 tool_profile: {tool_profile!r}"
                             f"（合法值：{', '.join(TOOL_PROFILES)}；缺省 None）")
        # #376/#419 per-role 模型：model None/空 = 全局 ACP_MODEL（调用方经 role_model(role) 传参）
        self.model = model or ACP_MODEL
        # #11 模型留痕：请求值 / 来源 / 落定回读判定 / 回读生效值（_apply_model 填充）
        self.model_requested = self.model
        self.model_source = None
        self.model_settle = None
        self.model_effective = None
        self.model_error = None   # #13 落定失败时 harness 原始报错摘要（≤200，供 model_reason 落库）
        self.allow_fs_write = allow_fs_write  # False 时 fs/write_text_file 一律拒绝
        self.deny_builtin_write = deny_builtin_write  # True 时内置 Edit/Write 工具调用直接取消本轮（建卡会话）
        self.allow_git_commit = allow_git_commit      # True 时 Bash git add/commit 放行（#302 需求对话，默认全角色关闭）
        self.allow_git_merge = allow_git_merge        # True 时 git merge/checkout/switch/stash 受限形态放行 + 工作目录内文件写（#355 需求助手合并授权，仅配合 allow_git_commit 使用）
        self.allow_python_safe = bool(allow_python_safe)  # True 时 Bash 放行受限 python 自验形态（#387 需求助手 / #423 评审角色；默认全角色关闭）
        # #387 python 自验路径根集（会话 workdir + 项目各仓库根；独立于 readonly_roots，
        # 不放宽到家目录）。None/空 = 未启用（python 项恒拒，安全兜底）。统一 resolve
        self.python_roots = [Path(r).resolve() for r in (python_roots or [])] or None
        # #423：形态 A（py_compile）目标落点根集，缺省回退 python_roots——py_compile 会在目标旁
        # 写 __pycache__，评审角色收窄到 worktree/产物目录，不吃主工作区仓库根
        self.python_compile_roots = [Path(r).resolve() for r in (python_compile_roots or [])] or None
        # #357 0.37.2 审批文本截断修复：tool_call_update 参数流里的结构化完整命令缓存
        # （toolCallId → command；request_permission 的展示文本被 kimi 截断到 ~85 字符，
        # 长命令只能用缓存里的完整命令做白名单校验）
        self._tool_cmds = {}
        # #303 防复发：内置 Edit/Write 与 fs 写的绝对路径白名单（dispatcher 各角色传
        # worktree/产物/临时目录；主工作区等真实仓库不在其中）。None = 不限制（建卡会话
        # deny_builtin_write 全禁写、不需白名单）。根路径统一 resolve（/tmp→/private/tmp 软链）
        self.write_roots = [Path(r).resolve() for r in (write_roots or [])] or None
        # #336：主工作区（各仓库真实路径）清单，dev/accept 的 Bash 写目标护栏用（防手滑，
        # 非机制保证）。None = 护栏未启用（建卡/需求助手会话走只读路由，无需）。统一 resolve
        self.main_ws_roots = [Path(r).resolve() for r in (main_ws_roots or [])] or None
        # 只读工具 roots（None 时回退 [workdir]；dispatcher 传 readonly_tool_roots 的完整 roots）
        self.readonly_roots = [Path(r) for r in (readonly_roots or [workdir])]
        # 额外放行的工具标题前缀（建卡会话的 create_requirement MCP 工具）
        self._allow_prefixes = [f"mcp__{READONLY_TOOL_SERVER}__"] + [str(p) for p in extra_allow_prefixes]
        self._extra_mcp_servers = list(extra_mcp_servers)  # 追加注入会话的 MCP server 条目
        self.mysql_config = mysql_config  # 可选的每项目 MySQL 只读查询配置（None = 不暴露 mysql_query）
        self._mcp = None  # 只读角色受限工具 HTTP MCP server（start 时创建，close 时销毁）
        self.proc = subprocess.Popen(
            runtime_argv(self.runtime_id, kimi_bin), cwd=str(self.workdir),
            **({"env": dict(os.environ, **self.runtime["env"])} if self.runtime["env"] else {}),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf,
            start_new_session=True)
        self._next_id = 0
        self._send_lock = threading.Lock()
        self._pending = {}       # 请求 id -> {"event": Event, "response": msg}
        self._msg_buf = []       # agent_message_chunk 聚合缓冲
        self._buf_lock = threading.Lock()
        self._tool_calls = {}    # toolCallId -> 初始 tool_call 的裸标题（kimi acp 的 tool_call 不带参数）
        self._tool_posted = set()  # 已上报摘要的 toolCallId（参数在后续 tool_call_update 里到达）
        # #42 缓存命中指标：_turn_usages = 各完成轮 normalize 后的 usage；_usage_raws = 供应商原始
        # 片段（响应 usage 对象 / update usage / /usage 原文，首见留存）；_llm_turns_done = 完成的
        # 真实模型请求轮数（prompt() 成功返回 +1；/usage 斜杠命令与失败轮不计）
        self._turn_usages = []
        self._usage_raws = []
        self._llm_turns_done = 0
        self.session_id = None
        self.model_name = None  # session/new|resume 响应 configOptions 里的当前模型显示名
        # #50 思考档位统一模型默认：只保留响应观测值（会话信息/落库用），不请求、不配置、不展示
        self.thinking = None
        self._capture = None    # 非 None 时 message chunk 收集到此缓冲（采集 /usage 输出），不上报
        # #421 会话活性上报（dispatcher 心跳）：状态切换时把 turn_step + 各环节时间戳落到
        # agent_sessions，右栏据此判活/判死（模型请求发出后长时间无响应 = 卡死）。
        # activity_sink(kind, detail) 由调度器注入；None（建卡会话/需求助手）时零行为 ——
        # 不得多写事件流、不得抛错（边界见 tests/test_dispatcher_session_activity.py）。
        self.activity_sink = activity_sink
        self._resp_pending = False   # 本轮/本工具轮之后是否还没收到任何 message/tool 输出
        self._out_sent_mono = {}     # kind -> 最近上报的 monotonic 时刻（llm_response/llm_thought 节流）
        self._tool_open = set()      # 在途工具 toolCallId 集合（并发工具用）
        # #28 输出守卫（复读/超长单请求兜底）：output_guard 为 None（board 建卡/需求助手会话）
        # 时 self._guard=None，全部守卫方法 getattr 短路 → 既有行为逐字不变。guard_sink(kind, detail)
        # 由调度器注入（留痕）；守卫状态收敛到单字典，_guard_lock 保护 recent/tripped 的并发读写。
        self.guard_sink = output_guard
        self._guard = self._new_guard() if output_guard is not None else None
        self.step_budget = None      # C2：本轮工具调用步数上限（调度器自动开发会话按角色预算设置）
        self._guard_lock = threading.Lock()
        self._guard_pending = None   # 在途 session/prompt 的 pending 记录（触发时 set 其 event 唤醒）
        self._thought_seen_logged = False     # 「已观测到 thought chunk」每会话一次日志（实测证据）
        self._unknown_updates_logged = set()  # 未知 sessionUpdate kind（每类一次日志）
        # ACP terminal 服务（upstream#351）：kimi 0.37.2 起 Bash 工具经 ACP 协议委托客户端
        # 执行（terminal/create 等），dispatcher/看板作为客户端提供终端进程托管。
        self._terms = {}                 # terminalId -> record
        self._term_lock = threading.Lock()
        self._term_counter = itertools.count(1)
        self._last_chunk_at = 0.0        # 最近一条 agent_message_chunk 到达时间（静默 flush 用）
        self._closed = False             # close() 置位：停止静默 flush 线程
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def _flush_loop(self):
        """静默 flush（回显时效）：连续 MSG_FLUSH_IDLE_SEC 没有新 chunk 就把已攒文本上报，
        让「说完一段就停」的回复（尤其回合末总结）在停顿后立刻出现在页面，而不是等整轮结束。
        #28：兼作判据 A（单次 LLM 请求在途时长）看门狗——无输出也跑，故事件持续在写时也能兜底。"""
        while not self._closed:
            time.sleep(MSG_FLUSH_TICK_SEC)
            try:
                self._check_request_guard()
                if self._msg_buf and time.time() - self._last_chunk_at >= MSG_FLUSH_IDLE_SEC:
                    self._flush_message()
            except Exception:  # 会话收尾竞态（属性被清理）不影响主流程
                return

    # ---- #28 输出守卫（判据 A 时长看门狗 / 判据 B 复读检测；触发即中止本会话） ----
    def _new_guard(self):
        """守卫状态单字典（output_guard 非 None 时创建）。recent 只保留最近 N 条足长 message。"""
        return {"recent": deque(maxlen=GUARD_REPEAT_N), "llm_active": False,
                "llm_started_mono": None, "aborted": False, "tripped": None, "steps": 0}

    def rearm_output_guard(self, sink):
        """保持池复用轮换轮时重绑守卫（防沿用上一轮旧闭包/旧计数/旧 tripped）。"""
        self.guard_sink = sink
        self._guard = self._new_guard() if sink is not None else None
        if getattr(self, "_guard_lock", None) is None:
            self._guard_lock = threading.Lock()
        self._guard_pending = None

    def _guard_mark_llm(self, active, reset_start=False):
        """置本次 LLM 请求在途标记；reset_start=True 时重置计时起点。_guard 为 None（守卫关闭/
        桩会话）时零行为。工具执行期（tool_start→tool_end）置 False → 该段不计入判据 A 时长。"""
        g = getattr(self, "_guard", None)
        if g is None:
            return
        g["llm_active"] = bool(active)
        if reset_start:
            g["llm_started_mono"] = time.monotonic()

    def _mark_tool_end(self):
        """#421/#28：在途工具集合清空 → 本轮输出结束的公共序列（显式 tool_end 与 _on_update
        隐式兜底两处共用，单一入口防判据 A 的 llm_active 停在 False）。#28 判据 A：tool_end
        重置 llm 计时起点（工具执行期不计时）。"""
        self._tool_open = set()
        self._activity("tool_end")
        self._resp_pending = True
        self._out_sent_mono = {}
        self._guard_mark_llm(True, reset_start=True)

    def _trip_guard(self, kind, detail):
        """触发守卫：单次置位（加锁原子 check-and-set，仅首个调用者 sink+cancel+唤醒）→ 留痕
        （guard_sink）→ cancel 本会话 → 唤醒在途 session/prompt 等待令其抛 TurnAborted。"""
        g = getattr(self, "_guard", None)
        if g is None:
            return
        first = False
        with self._guard_lock:
            if g.get("tripped") is None:
                g["tripped"] = {"kind": kind, "detail": detail}
                g["aborted"] = True
                first = True
        if not first:
            return
        log.warning(f"#{getattr(self, 'req_id', None)} 输出守卫触发（{kind}）：{detail}，取消本会话")
        sink = getattr(self, "guard_sink", None)
        if sink:
            try:
                sink(kind, detail)
            except Exception as e:
                log.warning(f"#{getattr(self, 'req_id', None)} 输出守卫留痕失败（忽略）: {e}")
        try:
            self.cancel()
        except Exception:
            pass
        p = getattr(self, "_guard_pending", None)
        if p is not None:
            try:
                p["event"].set()
            except Exception:
                pass

    def _check_request_guard(self):
        """#28 判据 A：单次 LLM 请求在途时长超阈值 → 触发。挂 _flush_loop（该线程以
        `except Exception: return` 收敛，故本函数**整体自吞异常、绝不上抛**）。"""
        g = getattr(self, "_guard", None)
        if g is None:
            return
        try:
            started = g.get("llm_started_mono")
            if not g.get("llm_active") or started is None or g.get("aborted"):
                return
            elapsed = time.monotonic() - started
            if elapsed >= GUARD_REQUEST_SEC:
                self._trip_guard("stall_request",
                                 f"单次模型请求在途 {int(elapsed)}s 超阈值 {int(GUARD_REQUEST_SEC)}s")
        except Exception as e:
            log.warning(f"#{getattr(self, 'req_id', None)} 输出守卫判据 A 异常（忽略）: {e}")

    def _check_step_budget(self):
        """C2 步数预算：本轮新工具调用数达 step_budget → 触发守卫（kind=budget_steps）。
        挂 _on_update 读循环，整体自吞异常；step_budget 为空或守卫关闭 → 零行为。"""
        g = getattr(self, "_guard", None)
        budget = getattr(self, "step_budget", None)
        if g is None or not budget:
            return
        try:
            if g.get("aborted"):
                return
            g["steps"] = int(g.get("steps") or 0) + 1
            if g["steps"] >= budget:
                self._trip_guard("budget_steps", f"本轮工具调用达 {budget} 步上限")
        except Exception as e:
            log.warning(f"#{getattr(self, 'req_id', None)} 步数预算检查异常（忽略）: {e}")

    def _check_output_guard(self, text):
        """#28 判据 B：连续 N 条 message 事件相邻文本高度重复 → 触发。挂 _on_update 读循环
        （无外层捕获），故**整体自吞异常、绝不上抛**。短于 MIN_LEN 的块不入队（防碎片噪声）。"""
        g = getattr(self, "_guard", None)
        if g is None:
            return
        try:
            if len(text) < GUARD_REPEAT_MIN_LEN or g.get("aborted"):
                return
            with self._guard_lock:
                g["recent"].append(text[:GUARD_SIM_LEN_CAP])
                recent = list(g["recent"])
            if len(recent) >= GUARD_REPEAT_N and repetitive_run(recent[-GUARD_REPEAT_N:]):
                self._trip_guard("repeat_output",
                                 f"连续 {GUARD_REPEAT_N} 条输出高度重复（相似度 ≥ {GUARD_REPEAT_SIM}）")
        except Exception as e:
            log.warning(f"#{getattr(self, 'req_id', None)} 输出守卫判据 B 异常（忽略）: {e}")

    # ---- 底层收发 ----
    def _send(self, msg):
        with self._send_lock:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    log.warning(f"#{self.req_id} ACP 非 JSON 行: {line[:200]!r}")
                    continue
                if "id" in msg and "method" in msg:
                    # agent -> client 反向请求（权限审批 / 文件读写），单独线程应答避免阻塞读循环
                    threading.Thread(target=self._handle_request, args=(msg,), daemon=True).start()
                elif "id" in msg:
                    p = self._pending.pop(msg["id"], None)
                    if p:
                        p["response"] = msg
                        p["event"].set()
                elif msg.get("method") == "session/update":
                    self._on_update(msg.get("params", {}).get("update", {}))
            for p in self._pending.values():
                p["event"].set()
        finally:
            for p in self._pending.values():
                p["event"].set()

    def request(self, method, params, timeout=60, guard=False):
        self._next_id += 1
        rid = self._next_id
        p = {"event": threading.Event(), "response": None}
        self._pending[rid] = p
        if guard:
            self._guard_pending = p   # #28：登记在途请求，守卫触发时 set 其 event 唤醒
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        # 超时按单调时钟分片记账（2026-09-17 修复）：threading.Event.wait 在 macOS 上走
        # pthread_cond_timedwait（realtime 语义，本机 CPython 未链接 relative_np 变体），
        # 系统睡眠会吃掉超时预算——合盖唤醒后 agent 被误判超时 kill，丢失已完成工作。
        # 每片最多等 ACP_WAIT_SLICE 秒，外层用 monotonic 累计（睡眠期间不前进），
        # 故睡眠最多造成单片误差；进程真阻塞时 monotonic 正常前进，超时判定不受影响。
        try:
            # #28：请求发出前已被守卫中止（重复输出在 prompt 前就攒够）→ 直接抛，不再等待
            if guard and self._guard_aborted():
                self._pending.pop(rid, None)
                raise TurnAborted(f"输出守卫已中止（{method}）")
            deadline = time.monotonic() + timeout
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    self._pending.pop(rid, None)
                    raise TimeoutError(f"ACP {method} 超时")
                if p["event"].wait(min(remain, ACP_WAIT_SLICE)):
                    break
            # #28：被守卫唤醒（等待被打破且未收到响应）→ 抛专用异常供调用方按中止收束
            if guard and self._guard_aborted():
                self._pending.pop(rid, None)
                raise TurnAborted(f"输出守卫中止（{method}）")
        finally:
            if guard:
                self._guard_pending = None
        resp = p["response"]
        if resp is None:
            raise RuntimeError(f"ACP 连接断开（{method}）")
        if "error" in resp:
            raise RuntimeError(f"ACP {method} 错误: {resp['error']}")
        return resp.get("result") or {}

    def _guard_aborted(self):
        """守卫是否已置中止位（_guard 为 None = 守卫关闭 → 恒 False）。"""
        g = getattr(self, "_guard", None)
        return bool(g and g.get("aborted"))

    # ---- 事件上报（注入回调，None 不报） ----
    def _emit(self, kind, content, session_id=None):
        if self.event_sink:
            try:
                self.event_sink(self.req_id, kind, content, session_id)
            except Exception:
                pass

    def _activity(self, kind, detail=None):
        """#421 会话活性上报（dispatcher 心跳）：activity_sink 为 None（建卡/需求助手会话）时零行为；
        异常静默，与 _emit 同款降级。llm_response / llm_thought 走 60s 节流（首个必报）。"""
        sink = getattr(self, "activity_sink", None)
        if not sink:
            return
        if kind in ("llm_response", "llm_thought"):
            mono = getattr(self, "_out_sent_mono", None)
            if mono is None:
                self._out_sent_mono = mono = {}
            last = mono.get(kind)
            now = time.monotonic()
            if last is not None and now - last < ACTIVITY_THROTTLE_SEC:
                return
            mono[kind] = now
        try:
            sink(kind, detail)
        except Exception:
            pass

    def _is_readonly(self):
        """只读审批路由判断：构造传入 readonly 优先；缺失（__new__ 桩/旧调用方只传 role）时按角色兜底"""
        ro = getattr(self, "readonly", None)
        if ro is not None:
            return bool(ro)
        return role_capability(getattr(self, "role", ""), "readonly")

    def _is_assistant_profile(self):
        """#24 助手 ACP 构造档唯一判定入口（三层拒绝矩阵共用）：缺省/未设 tool_profile 的
        普通会话恒 False（行为逐字不变）；未知值在 __init__ 已 fail-fast。"""
        return getattr(self, "tool_profile", None) == TOOL_PROFILE_FEISHU_ASSISTANT

    # ---- agent 反向请求：按角色路由审批；文件读写本进程代办（只读角色按产物白名单） ----
    def _handle_request(self, msg):
        method, params = msg["method"], msg.get("params", {})
        # #24 助手 profile 反向读层：fs 读写与全部 terminal/* fail-closed 拒绝（initialize 能力
        # 声明已为 false，此处是兜底）。普通 profile 不进此分支，行为逐字不变。
        if self._is_assistant_profile() and (method in ASSISTANT_DENIED_METHODS
                                            or method.startswith(ASSISTANT_DENIED_TERMINAL_PREFIX)):
            log.warning(f"#{self.req_id} 助手 profile 拒绝反向方法 {method}")
            self._send({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601, "message": f"method not found: {method}"}})
            return
        try:
            if method == "session/request_permission":
                result = self._auto_approve(params)
            elif method == "fs/read_text_file":
                result = {"content": Path(params["path"]).read_text(errors="replace")[:1024 * 1024]}
            elif method == "fs/write_text_file":
                path = Path(params["path"])
                if not getattr(self, "allow_fs_write", True):
                    raise PermissionError(f"本会话禁止写文件: {path}")
                if not self._in_write_roots(path):
                    # #303 防复发：fs 写为本进程代办，可在落地前拦截（内置 Edit/Write 只能事后取消本轮）
                    raise PermissionError(f"目标路径不在写白名单内（仅限 worktree/产物/临时目录）: {path}")
                if self._is_readonly() or role_capability(self.role, "artifact_write"):
                    if not self._in_artifact(path):
                        # #355 需求助手合并授权：只读角色例外放行会话工作目录（项目主工作区）
                        # 内文件写入（解决代码合并冲突所需）；#434 再加"显式 write_roots 白名单内"
                        # （worktree/产物/临时目录），与 #303 判定同源；其余路径仍拒
                        if not (getattr(self, "allow_git_merge", False) and self._in_workdir(path)) \
                                and not self._in_explicit_write_roots(path):
                            raise PermissionError(f"只读角色禁止写文件: {path}")
                    path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(params.get("content", ""))
                result = {}
            elif method == "terminal/create":
                result = self._terminal_create(params)
            elif method == "terminal/output":
                result = self._terminal_output(params)
            elif method == "terminal/wait_for_exit":
                result = self._terminal_wait_exit(params)
            elif method == "terminal/kill":
                result = self._terminal_kill(params)
            elif method == "terminal/release":
                result = self._terminal_release(params)
            elif method == "terminal/input":
                result = self._terminal_input(params)
            else:
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32601, "message": f"method not found: {method}"}})
                return
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except Exception as e:
            self._send({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32603, "message": str(e)}})

    # ---- ACP terminal 服务（upstream#351：kimi 0.37.2 起 Bash 委托客户端执行） ----
    # 协议要点（0.37.2 实测）：
    # - Bash 工具 = bash -c "<script>"（env 带 NO_COLOR=1/TERM=dumb），kimi 发 terminal/create；
    # - kimi 每 250ms 轮询 terminal/output（响应为累积 output 字符串，自带 emitted 游标去重），
    #   不需要本端推送 terminal_output 事件；stdin 为空实现，不会发 terminal/input；
    # - 进程退出后 kimi 调 terminal/wait_for_exit 拿退出码，结束调 terminal/release。
    def _terminal_script(self, params):
        """还原 Bash 工具实际执行的命令文本（create 请求的 command+args）。"""
        command = params.get("command") or ""
        args = list(params.get("args") or [])
        # #357：kimi 的 Bash = /bin/bash -c "<script>"（command 为 bash 绝对路径，
        # 实测 /bin/bash），仅匹配 "bash" 会走兜底 join 把 -c 脚本与引号语义弄丢、
        # 白名单解析必拒（只读角色 Bash 全灭的根因之一）
        if command in ("bash", "/bin/bash", "/usr/bin/bash") and len(args) >= 2 and args[0] == "-c":
            return args[1]
        return " ".join([command] + args)

    def _readonly_bash_ok(self, text):
        """只读分支 Bash 白名单的**唯一判定入口**（#423）：审批预检层 _auto_approve 与 terminal
        执行层 _terminal_approve 共用，两层同源同结论。
        先剥离 kimi 惯用的开头连续 `cd <roots 内路径> && ` 前缀（strip_cd_prefix：越界/畸形整体拒，
        不回落原文），再对剩余部分跑四个只读出口（dbq / 只读命令 / 拼接 / 受限 python）；
        git 出口维持原文语义（git_cmd_safe 自带 cd 处理，路径校验口径不变）。
        roots 统一取会话 readonly_roots（两层一致，兜底 [workdir]）。"""
        roots = [str(r) for r in (getattr(self, "readonly_roots", None) or [])] or [str(self.workdir)]
        workdir = str(self.workdir)
        st = strip_cd_prefix(text, roots, base=workdir)
        if st is None:
            return False
        rest, eff = st
        if (dbq_command_safe(rest)
                or readonly_cmd_safe(rest, roots, base=str(eff))
                or readonly_pipeline_safe(rest, roots, base=str(eff))
                or (getattr(self, "allow_python_safe", False)
                    and getattr(self, "python_roots", None)
                    and python_safe_cmd(rest, [str(r) for r in self.python_roots], base=str(eff),
                                        compile_roots=[str(r) for r in
                                                       (getattr(self, "python_compile_roots", None) or [])] or None))):
            return True
        return bool(getattr(self, "allow_git_commit", False)
                    and git_cmd_safe(text, [workdir, str(self.artifact_root)], base=workdir,
                                     allow_merge=getattr(self, "allow_git_merge", False)))

    def _notify_readonly_bash_denied(self, script):
        """只读角色 Bash 被白名单拒绝时向会话事件流留痕（每会话一次）。

        背景（#55 2026-10-20 analysis 会话）：terminal 层拒绝只回 JSON-RPC error，页面上只看到
        agent 自述「Bash 被限制」，用户无从得知拒绝原因与可用范围。这里留一条 system 事件说明
        白名单范围（去重防重试刷屏）。"""
        if getattr(self, "_bash_deny_notified", False):
            return
        self._bash_deny_notified = True
        emit = getattr(self, "_emit", None)
        if not emit:
            return
        try:
            emit("system",
                 f"只读角色 Bash 命令被白名单拒绝（{script[:80]}）：仅允许 "
                 "ls/cat/head/tail/wc/file/stat/find/date/echo/pwd（路径限会话 roots，支持 ;/| 拼接与 "
                 "2>/dev/null、2>&1 丢弃型重定向）；目录/内容检索请用内置 Search 或 MCP "
                 "list_dir/search_text，git 历史用 MCP git_log/git_status/git_diff。",
                 session_id=getattr(self, "session_id", None))
        except Exception:
            # 留痕是辅助信息，不能因事件通道缺失/异常把原始 PermissionError 替换掉。
            pass

    def _terminal_approve(self, script):
        """Bash 委托执行护栏（fail-closed）：无论 kimi 是否先走 request_permission，
        创建终端前按角色复核，与 _auto_approve 同源规则：
        - 全员：进程清理红线（kill/killpg/pkill 命中 kimi 或本会话驱动进程）
        - 写角色（dev/accept）：写主工作区护栏；no_commit 角色另拒提交类命令
        - 只读角色：命令白名单（_readonly_bash_ok 唯一入口，含 kimi 的 cd 前缀剥离）"""
        redline_script = script
        if getattr(self, "runtime_id", "kimi") != "kimi":
            # 复用既有结构解析器的数据/执行区分；新运行体也保护全部已注册 agent 进程名。
            # 只替换检测副本，实际命令不变，Kimi 默认规则不变。
            names = {name for runtime in AGENT_RUNTIMES.values()
                     for name in runtime["protected_process_names"]}
            pattern = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
            redline_script = re.sub(pattern, "kimi", script, flags=re.I)
        hit = kill_redline_hit(redline_script, getattr(getattr(self, "proc", None), "pid", None))
        if hit:
            raise PermissionError(f"进程清理红线命令被拦截: {hit}")
        if self._is_readonly():
            # #355/#357：与 _auto_approve 的 Bash 白名单同源（upstream#351 terminal 委托曾只认
            # readonly_cmd_safe，git/dbq/管道拼接在终端层被误拒——0.37.2 升级回归；
            # #423：cd 前缀剥离 + 受限 python 统一走 _readonly_bash_ok 唯一入口。
            # 本层 script 是结构化完整命令，无展示文本截断问题）
            if not self._readonly_bash_ok(script):
                self._notify_readonly_bash_denied(script)
                raise PermissionError(
                    "命令不在只读白名单内，拒绝创建终端（只读角色仅允许 "
                    "ls/cat/head/tail/wc/file/stat/find/date/echo/pwd，路径限会话 roots；"
                    "支持 ;/| 拼接与 2>/dev/null、2>&1 丢弃型重定向；目录/内容检索用内置 "
                    "Search 或 MCP list_dir/search_text，git 历史用 MCP git_log/git_status/git_diff）")
        else:
            if role_capability(self.role, "bash") == "git_merge":
                # #396 merge_resolve：终端复核机制层——仅放行受限 git 形态，其余一律拒（fail-closed）
                if not git_cmd_safe(script, [str(self.workdir), str(self.artifact_root)],
                                    base=str(self.workdir), allow_merge=True):
                    raise PermissionError(
                        "merge_resolve 仅允许受限 git 命令（add/commit/merge/checkout/switch/stash"
                        " 受限形态 + 只读子命令），其余命令一律拒绝")
            elif role_capability(self.role, "bash") == "no_commit" and COMMIT_RE.search(script):
                raise PermissionError("提交类命令被拦截")
            hit = main_ws_write_hit(script, getattr(self, "main_ws_roots", None),
                                    base=getattr(self, "workdir", None))
            if hit:
                raise PermissionError(f"写主工作区命令被拦截: {hit}")

    def _terminal_create(self, params):
        """terminal/create：spawn 命令进程，后台线程累积输出，返回 terminalId。"""
        script = self._terminal_script(params)
        self._terminal_approve(script)
        command = params.get("command") or ""
        args = list(params.get("args") or [])
        cwd = params.get("cwd") or str(self.workdir)
        env = dict(os.environ)
        for e in params.get("env") or []:
            if isinstance(e, dict) and e.get("name") is not None:
                env[e["name"]] = str(e.get("value", ""))
        try:
            proc = subprocess.Popen([command] + args, cwd=cwd, env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace",
                                    start_new_session=True)
        except Exception as e:
            raise PermissionError(f"命令启动失败: {e}")
        with self._term_lock:
            tid = f"t{next(self._term_counter)}"
            rec = {"proc": proc, "chunks": [], "size": 0, "truncated": False,
                   "exit_code": None, "signal": None, "done": threading.Event(),
                   "limit": int(params.get("outputByteLimit") or 0)}
            self._terms[tid] = rec
        threading.Thread(target=self._terminal_reader, args=(rec,), daemon=True).start()
        # #357 观测：所有 Bash 执行均应经 terminal/create（复核为机制保证），warning 级别
        # 便于从日志确认执行链路与命令形态（原 log.info 级别 lastResort 不输出）
        log.warning(f"#{self.req_id} terminal/create {tid}: {script[:120]}")
        return {"terminalId": tid}

    @staticmethod
    def _close_proc_pipes(proc, names=("stdin", "stdout", "stderr")):
        """关闭子进程残留管道。

        Popen 对象被会话或终端记录持有时，子进程退出不会释放父进程的 pipe fd。
        调度器由 launchd 拉起，maxfiles 软限制是 256，漏关的 stdin/stdout 会累积到
        Errno 24（Too many open files），后续连 ACP 进程都无法启动。
        """
        if proc is None:
            return
        for name in names:
            stream = getattr(proc, name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass

    def _terminal_reader(self, rec):
        try:
            while True:
                chunk = rec["proc"].stdout.read(8192)
                if not chunk:
                    break
                with self._term_lock:
                    if rec["limit"]:
                        remain = rec["limit"] - rec["size"]
                        if remain > 0:
                            rec["chunks"].append(chunk[:remain])
                            rec["size"] += min(len(chunk), remain)
                        if rec["size"] >= rec["limit"]:
                            rec["truncated"] = True
                    else:
                        rec["chunks"].append(chunk)
                        rec["size"] += len(chunk)
        except Exception:
            pass
        finally:
            try:
                rc = rec["proc"].wait()
            except Exception:
                rc = None
            with self._term_lock:
                rec["exit_code"] = rc
                if rc is None:
                    rec["signal"] = "SIGKILL"
            # 命令已结束：先关管道再通知等待方，避免记录被会话持有时 fd 一直留到 GC
            self._close_proc_pipes(rec["proc"])
            rec["done"].set()

    def _terminal_output(self, params):
        """terminal/output：返回累积输出与（若已退出的）退出状态。"""
        tid = params.get("terminalId")
        with self._term_lock:
            rec = self._terms.get(tid)
            if not rec:
                raise ValueError(f"终端不存在: {tid}")
            output = "".join(rec["chunks"])
            truncated = rec["truncated"]
            if rec["done"].is_set():
                exit_status = {"exitCode": rec["exit_code"], "signal": rec["signal"]}
            else:
                exit_status = None
        return {"output": output, "truncated": truncated, "exitStatus": exit_status}

    def _terminal_wait_exit(self, params):
        """terminal/wait_for_exit：阻塞等待进程退出，返回退出码。"""
        tid = params.get("terminalId")
        with self._term_lock:
            rec = self._terms.get(tid)
            if not rec:
                raise ValueError(f"终端不存在: {tid}")
        rec["done"].wait()
        with self._term_lock:
            return {"exitCode": rec["exit_code"], "signal": rec["signal"]}

    def _terminal_kill(self, params):
        """terminal/kill：杀掉终端进程。"""
        tid = params.get("terminalId")
        with self._term_lock:
            rec = self._terms.get(tid)
            if not rec:
                raise ValueError(f"终端不存在: {tid}")
        try:
            os.killpg(os.getpgid(rec["proc"].pid), signal.SIGKILL)
        except Exception:
            pass
        rec["done"].wait(timeout=10)
        return {}

    def _terminal_release(self, params):
        """terminal/release：释放终端（进程若未退出则终止）。"""
        tid = params.get("terminalId")
        with self._term_lock:
            rec = self._terms.pop(tid, None)
        if not rec:
            return {}
        if not rec["done"].is_set():
            try:
                os.killpg(os.getpgid(rec["proc"].pid), signal.SIGKILL)
            except Exception:
                pass
            rec["done"].wait(timeout=10)
        self._close_proc_pipes(rec["proc"])
        return {}

    def _terminal_input(self, params):
        """terminal/input：写 stdin（kimi 0.37.2 Bash 工具为空实现，防御性支持）。"""
        tid = params.get("terminalId")
        with self._term_lock:
            rec = self._terms.get(tid)
            if not rec:
                raise ValueError(f"终端不存在: {tid}")
        data = params.get("data") or ""
        try:
            rec["proc"].stdin.write(data)
            rec["proc"].stdin.flush()
        except Exception:
            pass
        return {}

    def _terminal_cleanup(self):
        """会话关闭时终止所有托管终端进程（防孤儿）。"""
        with self._term_lock:
            recs = list(self._terms.values())
            self._terms.clear()
        for rec in recs:
            if not rec["done"].is_set():
                try:
                    os.killpg(os.getpgid(rec["proc"].pid), signal.SIGKILL)
                except Exception:
                    pass
                rec["done"].wait(timeout=10)
            self._close_proc_pipes(rec["proc"])

    def _in_artifact(self, path):
        """路径是否属于本需求产物目录/结果文件（或旧约定工作目录根结果文件，兼容）"""
        p = Path(path).resolve()
        art = self.artifact_root.resolve()
        res = (self.artifact_root / "result.json").resolve()
        legacy = (self.workdir / RESULT_FILENAME).resolve()
        return p == res or art == p or art in p.parents or p == legacy

    def _in_write_roots(self, path):
        """#303 防复发：内置写工具路径白名单校验。未配置白名单（None）→ 放行（建卡会话等）；
        相对路径基于会话 cwd（worktree）→ 放行；绝对路径 resolve 后必须落在白名单根内
        （软链 → 真实路径，防 ~/.agents/skills/... 类软链指向主工作区绕过）"""
        roots = getattr(self, "write_roots", None)
        if not roots:
            return True
        p = Path(str(path))
        if not p.is_absolute():
            return True
        rp = p.resolve()
        return any(rp == r or r in rp.parents for r in roots)

    def _in_explicit_write_roots(self, path):
        """#434 三处闸门同源：会话**显式配置**了 write_roots 时，白名单内的路径在审批层与
        fs 落地层同样放行——此前这两层只认产物目录（#107 遗留口径），与 #303 的 write_roots
        各说各话：声明可写 /tmp，实际审批拒 + fs 拒（需求助手 2026-09-19 现场：/tmp 探针被
        拦并取消整轮）。write_roots 为 None/空 = 未配置 → 维持原产物口径的 fail-closed 默认
        （注意不能直接调 _in_write_roots 判空：它在未配置时返回 True，会把所有路径放行）。"""
        roots = getattr(self, "write_roots", None)
        return bool(roots) and self._in_write_roots(path)

    def _in_workdir(self, path):
        """路径是否位于会话工作目录（项目主工作区）内（#355 需求助手合并授权 fs 写例外）"""
        p = Path(path).resolve()
        wd = self.workdir.resolve()
        return p == wd or wd in p.parents

    @staticmethod
    def _toolcall_text(tc):
        parts = []
        for c in tc.get("content") or []:
            inner = c.get("content") or {}
            if isinstance(inner, dict) and inner.get("type") == "text":
                parts.append(inner.get("text", ""))
            elif isinstance(inner, str):
                parts.append(inner)
        return " ".join(parts)

    def _wait_tool_cmd(self, tool_call_id, timeout=2.0):
        """#357 0.37.2 审批文本截断修复：取回 tool_call_update 参数流里的完整命令（缓存）。
        request_permission 与完整 rawInput 几乎同时到达（实测差 ~3ms，kimi 流式分块发送，
        完整 command 在最后一块），但存在时序波动（实测偶发 >0.3s），超时放宽到 2s 吸收波动；
        仍无则返回 None（调用方回退展示文本，截断文本 fail-closed 拒绝，安全方向不变）。
        超时 miss 打 warning 便于观测（截断文本误拒的代价）。"""
        if not tool_call_id:
            return None
        end = time.monotonic() + timeout
        while True:
            cmd = self._tool_cmds.get(tool_call_id)
            if cmd:
                return cmd
            if time.monotonic() >= end:
                log.warning(f"#{self.req_id} _wait_tool_cmd 超时 miss tid={tool_call_id} "
                            f"cached={len(self._tool_cmds)}（回退展示文本，截断将 fail-closed 拒绝）")
                return None
            time.sleep(0.005)

    def _pick(self, options):
        for want in ("allow_always", "allow_once"):
            for o in options:
                if o.get("kind") == want:
                    return {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}
        if options:
            return {"outcome": {"outcome": "selected", "optionId": options[0]["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}

    def _reject(self, options):
        for o in options:
            if o.get("kind") == "reject_once" or o.get("optionId") == "reject":
                return {"outcome": {"outcome": "rejected", "optionId": o["optionId"]}}
        if options:
            return {"outcome": {"outcome": "rejected", "optionId": options[0]["optionId"]}}
        return {"outcome": {"outcome": "rejected"}}

    def _assistant_mcp_prefixes(self):
        """#24 助手审批白名单前缀：mcp__<extra_mcp_servers 条目的 name>__（动态派生）。
        单一源 = 实际注入会话的 MCP server 名，不在 acp_shared 硬编码助手 server 名
        （board 侧改名不漂移）；未注入任何条目时返回空列表 = 全拒（fail-closed）。"""
        prefixes = []
        for srv in getattr(self, "_extra_mcp_servers", None) or []:
            name = srv.get("name") if isinstance(srv, dict) else None
            if name:
                prefixes.append(f"mcp__{name}__")
        return prefixes

    def _approve_assistant_tool(self, title, options):
        """#24 助手 profile 审批：仅放行注入的助手 MCP 工具前缀且工具名非空，其余（含空 title、
        前缀裸名、内置 Read/Grep/Glob、Bash、Playwright、TAPD、未知 MCP、Write/Edit）一律拒绝。"""
        for p in self._assistant_mcp_prefixes():
            if title.startswith(p) and title[len(p):]:
                return self._pick(options)
        log.warning(f"#{self.req_id} 助手 profile 审批拒绝非助手工具: {title!r}")
        return self._reject(options)

    def _playwright_tool_check(self, title, text):
        """#379 Playwright 工具校验（dev/accept/只读三条审批路径共用，防双清单逻辑漂移）：
        双清单（放行 20 项/拒绝 4 项，清单外 fail-closed）+ L2 best-effort URL 协议校验。
        返回 None（放行）或违规描述（拒绝）；纯逻辑无副作用，便于单测。"""
        tool = title[len(PLAYWRIGHT_TOOL_PREFIX):]
        if tool in PLAYWRIGHT_DENIED_TOOLS or tool not in PLAYWRIGHT_ALLOWED_TOOLS:
            return f"危险/未知工具 {tool}"
        viol = _url_protocol_violation(text)
        if viol:
            return f"非 http(s) URL {viol}"
        return None

    def _runtime_write_allowed(self, path):
        """异构适配器写路径按会话 cwd 解析，包含相对路径、移动源/目标与软链。"""
        if not isinstance(path, str) or not path or "\x00" in path:
            return False
        workdir = Path(self.workdir).resolve()
        target = Path(path)
        target = (target if target.is_absolute() else workdir / target).resolve()
        roots = getattr(self, "write_roots", None) or [workdir, self.artifact_root]
        return any(target == Path(root).resolve() or Path(root).resolve() in target.parents
                   for root in roots)

    def _auto_approve(self, params):
        """工具审批策略按角色路由：
        dev：全放行（等价原无头行为），但 Bash 写主工作区护栏（#336）例外；readonly
        （analysis/proposal/tech/建卡会话）：只读，仅放行产物目录内的 Write、只读 MCP 受限
        工具（含额外允许前缀）与命令例外（scripts/dbq.py 只读查库 + 只读检查命令白名单，
        见 readonly_cmd_safe）；accept：受限，拒绝提交类命令与写主工作区命令，其余放行；
        merge_resolve（#396）：Bash 仅放行受限 git 形态（git_cmd_safe(allow_merge=True)，预检
        未命中交由 terminal 层完整命令复核拒绝）、Write/Edit 仅限写白名单内（worktree/产物/
        tmp），其余工具一律拒。"""
        options = params.get("options") or []
        runtime_id = getattr(self, "runtime_id", "kimi")
        tc = normalize_permission_tool(runtime_id, params.get("toolCall") or {})
        if runtime_id != "kimi":
            # 新适配器并不保证 terminal/create 兜底，必须在本次完整审批中复核。
            if tc.get("title") == "Bash":
                try:
                    self._terminal_approve(tc["rawInput"]["command"])
                except PermissionError:
                    return {"outcome": {"outcome": "cancelled"}}
            elif tc.get("title") in ("Write", "Edit", "Patch"):
                paths = tc.get("writePaths") or [tc["rawInput"]["file_path"]]
                if not all(self._runtime_write_allowed(path) for path in paths):
                    return {"outcome": {"outcome": "cancelled"}}
            else:
                return {"outcome": {"outcome": "cancelled"}}
            # 绝不选择 allow_always（Claude 会把权限规则持久化并绕过后续审批）。
            option = next((o for o in options if o.get("kind") == "allow_once"), None)
            return ({"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
                    if option else {"outcome": {"outcome": "cancelled"}})
        title = tc.get("title") or ""
        text = self._toolcall_text(tc)
        # #24 助手 profile 审批层：只放行本会话注入的助手业务 MCP 工具（前缀由 extra_mcp_servers
        # 条目的 name 动态派生，避免硬编码漂移），其余一律拒绝——内置 Read/Grep/Glob、Bash、
        # Playwright、TAPD、未知 MCP、Write/Edit 全灭。普通 profile 不进此分支，行为逐字不变。
        if self._is_assistant_profile():
            return self._approve_assistant_tool(title, options)
        # #344：guard 输入优先用结构化 rawInput.command（_extract_command 支持 dict 直取）——
        # resume/续跑会话的审批文本是多段拼接（命令段 + 含撇号描述段，如 user's/session's），
        # 撇号致整体引号计数为奇、_split_exec_units 判解析失败，护栏曾因此一刀切误拦合法命令。
        raw_input = tc.get("rawInput")
        cmd_src = raw_input if isinstance(raw_input, dict) and raw_input.get("command") else text
        # #336 写角色 Bash 护栏（dev/accept 共用，防手滑非机制保证）：解析写操作目标，
        # 目标落在主工作区（各仓库真实路径）即拒；主路径出现但写目标合法的放行
        # （.venv python 跑测试重定向 /tmp、git -C <主工作区> log 只读诊断）。
        # 审批文本带「Requesting approval to Running: 」前缀，先 _extract_command 剥离。
        # main_ws_roots 未注入（None）→ 护栏未启用，行为与现状一致（向后兼容桩/旧调用方）。
        if not self._is_readonly() and title == "Bash":
            hit = main_ws_write_hit(_extract_command(cmd_src), getattr(self, "main_ws_roots", None),
                                    base=getattr(self, "workdir", None))
            if hit:
                log.warning(f"#{self.req_id} 审批拒绝写主工作区命令（{hit}）: {text[:120]}")
                self._emit("system",
                           f"写主工作区命令已被审批拦截（{hit}）；仅允许写 worktree/产物/临时目录，"
                           "目标代码不在 worktree 内时报 need_confirm 等待人工处理。",
                           session_id=getattr(self, "session_id", None))
                return self._reject(options)
        if not self._is_readonly() and role_capability(self.role, "bash") == "git_merge":
            # #396 merge_resolve：Bash 仅放行受限 git 形态（fail-closed：预检未命中仍 approve，
            # 由 terminal 层用完整命令复核拒绝——#357 同款，展示文本可能被截断不可据此拒绝）；
            # Write/Edit 限写白名单内（worktree/产物/tmp，主工作区拒绝）；MCP/其它工具走下方通用路由
            if title == "Bash":
                _cmds = getattr(self, "_tool_cmds", None)
                cmd_text = (_cmds.get(tc.get("toolCallId") or "") if _cmds else None) or _extract_command(text)
                if git_cmd_safe(cmd_text, [self.workdir, self.artifact_root], base=self.workdir,
                                allow_merge=True):
                    return self._pick(options)
                log.warning(f"#{self.req_id} merge_resolve Bash 预检未命中（交由 terminal 层复核）: {cmd_text[:80]}")
                return self._pick(options)
            if title in ("Write", "Edit"):
                m = re.search(r"Writing\s+(.+)", text) or re.search(r"Editing\s+(.+)", text)
                if m and self._in_write_roots(m.group(1).strip()):
                    return self._pick(options)
                # 路径明确不在写白名单内（或展示文本无法定位路径）→ 拒绝（fail-closed；
                # 截断安全：缩短只会让根内路径仍在根内，不会把越界路径误判为合法）
                log.warning(f"#{self.req_id} merge_resolve 写路径预检未命中，拒绝: {text[:80]}")
                return self._reject(options)
        if not self._is_readonly() and role_capability(self.role, "bash") == "allow":
            if title.startswith(PLAYWRIGHT_TOOL_PREFIX):
                # #379：playwright 工具放行唯一通道 = 前缀已注入（_allow_prefixes）+ 双清单校验；
                # 未注入前缀的会话（旧进程复用/异常构造）对 playwright 工具一律拒（fail-closed）
                if PLAYWRIGHT_TOOL_PREFIX in (getattr(self, "_allow_prefixes", None) or ()):
                    reason = self._playwright_tool_check(title, text)
                else:
                    reason = "前缀未注入（会话无浏览器能力，不可放行）"
                if reason:
                    log.warning(f"#{self.req_id} 审批拒绝 Playwright {reason}")
                    self._emit("system",
                               f"Playwright 工具已被审批拦截（{reason}），会话继续",
                               session_id=getattr(self, "session_id", None))
                    return self._reject(options)
            return self._pick(options)
        # #108 禁杀红线：kill/pkill 命中 kimi 或本会话驱动进程 PID → 硬拒
        # （accept 实质加固；chat/只读 fail-closed 语义不变，显式化 + 日志留痕；dev 不经过此段）
        # #113 v5：先剥离「Requesting approval to Running: 」前缀提取真实命令再检测（L2 联动，杜绝首动词误判）
        # #344：同 guard 分支，优先结构化 rawInput.command，避免拼接描述段触发 legacy 全文本兜底误拦
        if title == "Bash":
            hit = kill_redline_hit(_extract_command(cmd_src), getattr(getattr(self, "proc", None), "pid", None))
            if hit:
                log.warning(f"#{self.req_id} 审批拒绝进程清理红线命令（{hit}）: {text[:120]}")
                return self._reject(options)
        if role_capability(self.role, "bash") == "no_commit":
            if title == "Bash" and COMMIT_RE.search(text):
                return self._reject(options)
            if title.startswith(PLAYWRIGHT_TOOL_PREFIX):
                # #379：同 dev 分支——前缀未注入一律拒；已注入走双清单校验（fail-closed）
                if PLAYWRIGHT_TOOL_PREFIX in (getattr(self, "_allow_prefixes", None) or ()):
                    reason = self._playwright_tool_check(title, text)
                else:
                    reason = "前缀未注入（会话无浏览器能力，不可放行）"
                if reason:
                    log.warning(f"#{self.req_id} 审批拒绝 Playwright {reason}")
                    self._emit("system",
                               f"Playwright 工具已被审批拦截（{reason}），会话继续",
                               session_id=getattr(self, "session_id", None))
                    return self._reject(options)
            return self._pick(options)
        # 只读角色/产物写角色：仅放行产物目录（fs/write_text_file 走白名单兜底），
        # #434 起再加「会话显式 write_roots 白名单内」——与 #303 判定同源（见
        # _in_explicit_write_roots；未配置白名单的会话仍只认产物目录，fail-closed 默认不变）
        if title in ("Write", "Edit"):
            m = re.search(r"Writing\s+(.+)", text) or re.search(r"Editing\s+(.+)", text)
            if m and (self._in_artifact(m.group(1).strip())
                      or self._in_explicit_write_roots(m.group(1).strip())):
                return self._pick(options)
        # 命令例外：只读数据库查询脚本（DB 层强制只读）或只读检查命令白名单。
        # #119 修复：审批文本含「Requesting approval to Running: 」前缀，必须先剥离再校验
        # （红线分支 #113 v5 同款姿势），否则白名单内命令（ls/cat/date 等）全部误拒。
        if title == "Bash":
            # #357 v2：审批文本被 kimi 截断（~85 字符），且完整参数流在审批响应后才到达
            # （实测：request_permission 后 kimi 暂停发送剩余参数块，等响应才发完整 command，
            # 审批层等待完整命令 = 死锁，2s 超时仍 miss 见日志）。审批层改为「预检」：
            # 缓存/展示文本命中白名单 → 直接 approve；未命中 → 也 approve（零等待），
            # 由 terminal 层（_terminal_approve，与审批层同源 fail-closed）用完整 script
            # 复核——0.37.2 所有 Bash 均经 terminal/create，复核为机制保证。
            _cmds = getattr(self, "_tool_cmds", None)  # 桩会话可能未初始化
            cmd_text = (_cmds.get(tc.get("toolCallId") or "") if _cmds else None) or _extract_command(text)
            # #423：与 terminal 层共用唯一判定入口 _readonly_bash_ok（含 kimi 的 cd 前缀剥离；
            # roots 统一取会话 readonly_roots，两层结论一致）。cmd_text 的 base 解析基准 =
            # 命令实际执行目录（进程 cwd 非 workdir 时也能正确判定）
            if self._readonly_bash_ok(cmd_text):
                return self._pick(options)
            log.warning(f"#{self.req_id} Bash 审批预检未命中（交由 terminal 层复核）: {cmd_text[:80]}")
            return self._pick(options)
        # 只读受限工具（MCP，#82）：工具名以 mcp__reqboard-tools__ 开头即放行（安全在工具内部 fail-closed：
        # 路径 roots 校验、SQL 只读白名单、输出限量；审批只做"哪个工具"路由，不解析参数）
        # 额外前缀（如建卡会话 mcp__reqboard-create__*）同样只做工具名路由，安全在工具内部校验
        allow_prefixes = getattr(self, "_allow_prefixes", None) or [f"mcp__{READONLY_TOOL_SERVER}__"]
        if any(title.startswith(p) for p in allow_prefixes):
            if title.startswith(PLAYWRIGHT_TOOL_PREFIX):
                # #379 与 dev/accept 分支共用同一校验函数（双清单硬拒/放行 + best-effort URL）
                reason = self._playwright_tool_check(title, text)
                if reason:
                    log.warning(f"#{self.req_id} 审批拒绝 Playwright {reason}")
                    return self._reject(options)
            if title.startswith(TAPD_TOOL_PREFIX):
                tool = title[len(TAPD_TOOL_PREFIX):]
                # tapd MCP 只读白名单：仅放行 get_* 只读工具（读需求/缺陷/评论/迭代等），
                # create/update/comment/send 等写工具与未知工具一律拒绝（fail-closed）
                if tool not in TAPD_READONLY_TOOLS:
                    log.warning(f"#{self.req_id} 审批拒绝 TAPD 写/未知工具 {tool}")
                    return self._reject(options)
                return self._pick(options)
            return self._pick(options)
        return self._reject(options)

    # `cd <path> &&` / `cd <path>;` 前缀（agent 惯用形态：路径往往就是 per-req worktree，
    # 会吃光摘要预算把真正的命令挤掉）。支持带引号路径
    _CD_PREFIX_RE = re.compile(r"""^(?:cd\s+(?:"[^"]*"|'[^']*'|\S+)\s*(?:&&|;)\s*)+""")

    @staticmethod
    def _action_of(title):
        """从 title 取动作词：'Running: <cmd>'→'Running'、'Editing /p'→'Editing'、
        无冒号无空格的工具名（mcp__reqboard-tools__read_file）整体保留。"""
        head = title.split(":", 1)[0] if ":" in title else title.split(" ", 1)[0]
        return head.strip() or title

    def _fold_paths(self, text):
        """把摘要里的绝对路径折叠成可读短形态（2026-09-17）：worktree 内 → 相对路径；
        产物目录 → `<产物>/x`；主工作区 → `<主工作区>/x`。长前缀优先替换，避免短根截断长根。
        桩会话（__new__ 构造）缺这些属性时原样返回。"""
        pairs = []
        for attr, label in (("workdir", ""), ("artifact_root", "<产物>/"),
                            ("main_ws_roots", "<主工作区>/")):
            val = getattr(self, attr, None)
            for root in (val if isinstance(val, list) else ([val] if val else [])):
                try:
                    pairs.append((str(Path(root)), label))
                except Exception:   # noqa: BLE001 - 展示层兜底：路径异常不影响摘要生成
                    continue
        for root, label in sorted(pairs, key=lambda p: -len(p[0])):
            if root:
                text = text.replace(root + "/", label)
        return text

    def _tool_summary(self, update, fallback_title=None):
        """把 ACP tool_call(_update) 渲染成一行摘要：动作 + 关键参数（命令/路径/模式等）。

        2026-09-17 改：**优先用 rawInput 自建摘要**。kimi 的 title 是「动作 + 前 ~51 字符参数」，
        参数一长（典型 `cd <worktree 长路径> && <真正的命令>`、worktree 下的绝对文件路径）后半段
        全被截掉，不同操作显示成同一行（现场：连续 20 余行 Tool 事件全长一个样）。结构化完整参数
        就在同一次 update 的 rawInput 里（#357 已把它缓存给审批用），据此重建：Bash 剥离 cd 前缀、
        路径相对 worktree/产物目录折叠、动作词沿用 title 前缀。取不到结构化参数时逐级退回 title，
        未知工具（MCP / 子 agent 拉起等）行为与改前一致。"""
        title = update.get("title") or fallback_title or update.get("kind") or "工具调用"
        raw = update.get("rawInput")
        detail = ""
        if isinstance(raw, dict):
            for key in ("command", "path", "pattern", "query", "url", "file_path", "filePath"):
                if raw.get(key):
                    detail = str(raw[key])
                    break
            else:
                if raw:
                    detail = json.dumps(raw, ensure_ascii=False)
        if not detail:
            locs = update.get("locations") or []
            if locs and isinstance(locs[0], dict) and locs[0].get("path"):
                detail = str(locs[0]["path"])
        if not detail:
            return title                       # 无结构化参数 → 原样 title（行为不变）
        detail = " ".join(detail.split())
        if isinstance(raw, dict) and raw.get("command"):
            stripped = self._CD_PREFIX_RE.sub("", detail).strip()   # Bash：剥离 cd 前缀
            if stripped:                       # 全是 cd（极端形态）时保留原文本，不返回空摘要
                detail = stripped
        detail = self._fold_paths(detail)
        if len(detail) > 120:
            detail = detail[:117] + "..."
        return f"{self._action_of(title)}: {detail}"

    # ---- 流式事件 -> 事件接收方 ----
    def _note_tool_step(self, tid, update, started=None, finished=None):
        """#421 工具阶段活性：集合由空转非空 → tool_start；集合清空 → tool_end（并发工具的中间完成不上报）。
        started/finished 为 None 时按 status 推断（tool_call 事件无 status 字段，调用方显式传 started=True）。
        顺序要求：`_resp_pending`（本轮首个输出）先报 llm_response，再报 tool_start —— 先响应到达、后工具开始。"""
        if getattr(self, "_tool_open", None) is None:
            self._tool_open = set()
        key = tid or f"_{id(update)}"
        status = (update or {}).get("status")
        if finished is None:
            finished = status in ("completed", "failed", "cancelled")
        if started is None:
            started = (not finished) and status in (None, "pending", "in_progress")
        if finished:
            if key in self._tool_open:
                self._tool_open.discard(key)
            if not self._tool_open:
                # #28：tool_end 公共序列（含判据 A 计时起点重置）收敛到单一 helper
                self._mark_tool_end()
            return
        if not started:
            return
        # 模型已开始输出（工具调用本身即输出证据）：首帧必报语义
        if getattr(self, "_resp_pending", False):
            self._resp_pending = False
            self._activity("llm_response")
        was_empty = not self._tool_open
        self._tool_open.add(key)
        if was_empty:
            # #28 判据 A：工具执行期不计时（暂停 llm 在途计时，tool_end 再重置起点）
            self._guard_mark_llm(False)
            title = ((getattr(self, "_tool_calls", None) or {}).get(key)
                     or (update or {}).get("title") or "工具调用")
            self._activity("tool_start", " ".join(str(title).split())[:48])

    def _on_update(self, update):
        # #42 缓存命中嗅探：session/update 可能携带 usage dict（在途轮的供应商用量），静默采集；
        # 未知 kind 的一次性日志行为不变（见函数尾部）
        _u = update.get("usage")
        if isinstance(_u, dict):
            self._note_usage(_u, raw=_u)
        kind = update.get("sessionUpdate")
        runtime_id = getattr(self, "runtime_id", "kimi")
        if runtime_id != "kimi" and kind in ("tool_call", "tool_call_update"):
            normalized = normalize_permission_tool(runtime_id, update)
            # 未识别的工具只保留展示标题；审批层仍拒绝，不能凭展示名获得授权。
            if normalized.get("title") != "Unknown":
                update = normalized
            raw = normalized.get("rawInput") or {}
            try:
                if normalized.get("title") == "Bash":
                    self._terminal_approve(raw["command"])
                elif normalized.get("title") in ("Write", "Edit", "Patch"):
                    paths = normalized.get("writePaths") or [raw["file_path"]]
                    if not all(self._runtime_write_allowed(path) for path in paths):
                        raise PermissionError("写路径越出会话白名单")
            except PermissionError as exc:
                self._emit("system", f"运行体工具红线被拦截：{exc}", session_id=self.session_id)
                self.cancel()
                return
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                if self._capture is not None:
                    self._capture.append(content.get("text", ""))
                    return
                # #421 活性：先兜底隐式 tool_end（tool 态收到输出 = 上游 completed 事件缺失），
                # 再报本次响应到达——顺序决定事件序列语义（工具阶段先收尾，才算这一轮输出）
                if getattr(self, "_tool_open", None):
                    # #28：与显式 tool_end 共用公共序列（含判据 A 计时重置），防 llm_active 停在 False
                    self._mark_tool_end()
                self._activity("llm_response")   # message chunk = 响应/输出到达
                self._resp_pending = False       # 首个无条件上报；其后按 60s 节流（_activity 内部管）
                with self._buf_lock:
                    self._msg_buf.append(content.get("text", ""))
                    full = sum(len(s) for s in self._msg_buf) > MSG_FLUSH_CHARS
                    self._last_chunk_at = time.time()
                if full:
                    self._flush_message()
        elif kind == "agent_thought_chunk":
            # #421：模型推理 chunk = 活性证据。只刷 last_thought_at，不写事件流、不动 _resp_pending
            # （思考不关闭响应闸门，避免「思考之后 message chunk 不再无条件上报」导致响应档不可达）
            if not getattr(self, "_thought_seen_logged", False):
                self._thought_seen_logged = True
                log.info(f"#{getattr(self, 'req_id', None)} 已观测到 agent_thought_chunk（#421 思考档活性证据）")
            self._activity("llm_thought")
        elif kind == "tool_call":
            self._flush_message()
            tid = update.get("toolCallId")
            title = update.get("title") or update.get("kind") or "工具调用"
            if getattr(self, "deny_builtin_write", False) and title in ("Edit", "Write"):
                # 建卡会话硬性只读：Kimi 对 git 工作目录内的 Edit/Write 有自动放行策略，
                # 不会发 session/request_permission；只能在工具调用事件到达时立即取消本轮。
                self._emit("system", f"建卡会话禁止文件写入（{title}），本轮已取消",
                           session_id=self.session_id)
                self.cancel()
                return
            self._note_tool_step(tid, update, started=True)   # #421 工具阶段起点（tool_call 无 status）
            if not tid or tid not in self._tool_calls:
                self._check_step_budget()                     # C2：同一 toolCallId 重复上报不重复计步
            if tid:
                self._tool_calls[tid] = title
            else:
                self._emit("tool", self._tool_summary(update), session_id=self.session_id)
        elif kind == "tool_call_update":
            tid = update.get("toolCallId")
            if not tid:
                return
            # #357 0.37.2 审批文本截断修复：参数流完整命令入缓存（request_permission 的
            # 展示文本被截断，审批侧 _wait_tool_cmd 优先取缓存做白名单校验）。
            # 必须在 _tool_posted 去重检查之前写入：kimi 流式分块发送参数，前面可能有
            # rawInput 存在但 command 尚空的 update 已触发去重，带完整命令的 update 到达时
            # 会被挡住，缓存不写在审批等待窗口内将 miss（实测序列见 #357）
            raw = update.get("rawInput")
            if isinstance(raw, dict) and raw.get("command"):
                _cmds = getattr(self, "_tool_cmds", None)  # 桩会话（L3 事件流测试）可能未初始化
                if _cmds is not None:
                    _cmds[tid] = str(raw["command"])
            # #421 活性：工具状态切换（started/finished）必须在 _tool_posted 去重早退闸**之前**上报——
            # 真实 kimi 序列是「rawInput 分块先行 → 打标 _tool_posted」，completed 落在闸后，
            # 放闸后则永远不触发。上报只看 status 变化，与去重语义无关
            self._note_tool_step(tid, update)
            if tid in self._tool_posted:
                return
            if raw:
                self._flush_message()
                if getattr(self, "deny_builtin_write", False):
                    title = self._tool_calls.get(tid) or update.get("title") or ""
                    if title in ("Edit", "Write"):
                        # 兜底：tool_call 事件丢失/延迟时，rawInput 到达仍要拦截，避免写入落地
                        self._emit("system", f"建卡会话禁止文件写入（{title}），本轮已取消",
                                   session_id=self.session_id)
                        self.cancel()
                        return
                # #303 防复发：内置 Edit/Write 写绝对路径白名单校验（参数在 rawInput 才可见，
                # 此时工具已执行，拦截语义 = 取消本轮阻止后续动作 + 事件留痕；fs 写为本进程
                # 代办可在落地前拦截）。仅 dispatcher 配置了 write_roots 的角色启用
                if getattr(self, "write_roots", None):
                    title = self._tool_calls.get(tid) or update.get("title") or ""
                    if title in ("Edit", "Write", "MultiEdit"):
                        raw = update.get("rawInput")
                        path = None
                        if isinstance(raw, dict):
                            path = raw.get("file_path") or raw.get("path")
                            edits = raw.get("edits")
                            if not path and isinstance(edits, list) and edits and isinstance(edits[0], dict):
                                path = edits[0].get("file_path")
                        if path and not self._in_write_roots(path):
                            self._emit("system",
                                       f"内置写工具目标路径不在白名单内（{str(path)[:150]}），本轮已取消。"
                                       "绝对路径写仅限 worktree/产物/临时目录；"
                                       "禁止写真实仓库主工作区（含软链指向的仓库文件），"
                                       "目标代码不在 worktree 内时报 need_confirm 等待人工处理。",
                                       session_id=self.session_id)
                            self.cancel()
                            return
                self._tool_posted.add(tid)
                # #108 事件流兜底（L3）：首次 rawInput 上报检测禁杀红线（_tool_posted 去重既有逻辑）
                raw = update.get("rawInput")
                cmd_text = raw.get("command") if isinstance(raw, dict) else str(raw)
                hit = kill_redline_hit(cmd_text, getattr(getattr(self, "proc", None), "pid", None)) if cmd_text else None
                if hit:
                    if role_capability(self.role, "cancel_on_redline"):
                        self._emit("system", f"{REDLINE_CANCEL_SIGNAL}（{hit}），会话已取消",
                                   session_id=self.session_id)
                        self.cancel()
                    else:
                        self._emit("system", f"进程清理红线命令（{hit}），已被审批拒绝",
                                   session_id=self.session_id)
                # #268 L3 兜底：#379 建卡/dev/accept 会话（playwright 前缀在放行名单）启用；rawInput.url 非 http(s) → 取消当轮
                if PLAYWRIGHT_TOOL_PREFIX in (getattr(self, "_allow_prefixes", None) or ()):
                    raw = update.get("rawInput")
                    url = raw.get("url") if isinstance(raw, dict) else None
                    if url and not HTTP_URL_RE.match(str(url)):
                        self._emit("system", f"非 http(s) URL 导航已被拦截（{str(url)[:100]}），本轮已取消",
                                   session_id=self.session_id)
                        self.cancel()
                        return  # 不再上报 tool 摘要，违规调用不留可用结果
                self._emit("tool", self._tool_summary(update, self._tool_calls.get(tid)),
                           session_id=self.session_id)
            elif update.get("status") == "failed":
                self._flush_message()
                self._tool_posted.add(tid)
                self._emit("tool", f"失败: {self._tool_summary(update, self._tool_calls.get(tid))}",
                           session_id=self.session_id)
            elif update.get("status") == "completed" and tid in self._tool_calls:
                self._tool_posted.add(tid)
                self._emit("tool", self._tool_calls[tid], session_id=self.session_id)
        else:
            # #421 实测核对用：未知/未处理的 sessionUpdate 类型每类只记一次（无功能依赖，不崩）
            kinds = getattr(self, "_unknown_updates_logged", None)
            if kinds is None:
                self._unknown_updates_logged = kinds = set()
            if kind not in kinds:
                kinds.add(kind)
                log.info(f"#{getattr(self, 'req_id', None)} 未处理的 sessionUpdate 类型: "
                         f"{kind!r}（#421 实测核对用，无功能依赖）")

    def _flush_message(self):
        with self._buf_lock:
            if not self._msg_buf:
                return
            text = "".join(self._msg_buf).strip()
            self._msg_buf = []
        if text:
            self._emit("message", text[:4000], session_id=self.session_id)
            # #28 判据 B：每条上报的 message 事件入队做复读检测
            self._check_output_guard(text)

    # ---- 会话流程 ----
    def _runtime_supports(self, capability):
        runtime_id = getattr(self, "runtime_id", "kimi")
        supported = agent_runtime(runtime_id)[capability]
        if not supported:
            runtime_skip(runtime_id, capability)
        return supported

    def _apply_config(self, result):
        models = (result or {}).get("models") or {}
        if models.get("currentModelId"):
            self.model_name = models["currentModelId"]
        opts = (result or {}).get("configOptions")
        if not isinstance(opts, list):
            return
        thinking_opt = None
        for opt in opts:
            if opt.get("category") == "thought_level" or opt.get("id") == "thinking":
                thinking_opt = opt
                continue
            cur = opt.get("currentValue")
            if cur and (opt.get("category") == "model" or opt.get("id") == "model"):
                self.model_name = next((o.get("name") for o in opt.get("options") or []
                                        if o.get("value") == cur), cur)
        # #50：只记录响应观测值（不再据此选档/请求）。当前模型无思考配置项时清掉
        # 上一个模型残留的观测值（否则 gpt 会话显示切换前 deepseek 的 high）
        if thinking_opt is None:
            self.thinking = None
            return
        if thinking_opt.get("currentValue"):
            self.thinking = thinking_opt.get("currentValue")

    def _apply_permission_mode(self):
        """只读/受限角色切 mode=default（服务端映射为 manual 审批），使 Bash/Write 走客户端审批。
        注意：Kimi 对 git 工作目录内的内置 Edit/Write 仍有自动放行策略，不会发审批请求；
        建卡会话依赖 ACPSession.deny_builtin_write 在工具调用事件到达时直接取消本轮。"""
        if not self._runtime_supports("supports_permission_mode"):
            return
        if not role_capability(self.role, "manual_mode"):
            return
        try:
            self.request("session/set_config_option", {
                "sessionId": self.session_id, "configId": "mode", "value": "default"}, timeout=30)
            log.info(f"#{self.req_id} 角色 {self.role} 已切 mode=default（manual 审批）")
        except Exception as e:
            log.warning(f"#{self.req_id} 切换 mode=default 失败（提示词约束兜底）: {e}")

    def _model_settle_of(self, requested, result):
        """#11 落定回读判定（口径 §1.3）：返回 (settle, effective)。
        settle ∈ {settled, mismatch, not_in_options, unverified, unreadable}，严格按序：
        rejected（异常，调用方处理）→ unreadable → 归一化 C≈R（原样回显，先于 options 定位，
        防误报）→ options 定位目标项 t → mismatch → unverified / not_in_options。"""
        opt = None
        for o in (result or {}).get("configOptions") or []:
            if o.get("category") == "model" or o.get("id") == "model":
                opt = o
                break
        cur = opt.get("currentValue") if opt else None
        if not cur:
            return "unreadable", requested
        if str(cur).strip().lower() == str(requested).strip().lower():
            return "settled", cur
        options = opt.get("options") or []
        target = next((o for o in options if o.get("value") == requested), None) \
            or next((o for o in options if o.get("name") == requested), None)
        if target is not None and (cur == target.get("value") or cur == target.get("name")):
            return "settled", cur
        if any(cur == o.get("value") or cur == o.get("name") for o in options):
            return "mismatch", cur
        return ("unverified" if target is not None else "not_in_options"), cur

    def _apply_model(self):
        """会话级固定模型为 self.model（缺省 ACP_MODEL；#376 评审角色按 role 配置），
        不依赖 kimi 全局配置（升级会重置 default_model，如 k3-256k 触发周限额/不可用导致会话秒退）。
        幂等，新会话与续跑都调用；失败仅警告不阻断会话。
        #11：写后回读 configOptions.currentValue 判定是否落定（_model_settle_of），非 settled
        记 WARNING + 事件留痕（不再静默），仍不阻断会话。"""
        if not self._runtime_supports("supports_model_option"):
            return
        model = getattr(self, "model", None) or ACP_MODEL
        try:
            result = self.request("session/set_config_option", {
                "sessionId": self.session_id, "configId": "model", "value": model}, timeout=30)
            # 响应含更新后的 configOptions，以此校正会话信息栏 model 显示名（与 _apply_config 同款解析）
            self._apply_config(result)
            settle, effective = self._model_settle_of(model, result)
        except Exception as e:
            settle, effective = "rejected", model
            self.model_settle, self.model_effective = settle, effective
            self.model_error = str(e)[:200]   # #13：harness 原始报错摘要（供 model_reason 落库）
            log.warning(f"#{self.req_id} 设置会话模型 {model} 失败（沿用默认模型）: {e}")
            self._emit("system", f"模型未落定（rejected）：请求 {model}，设置被拒（{e}），会话继续")
            return
        self.model_settle, self.model_effective = settle, effective
        if settle == "settled":
            self.model_error = None
            log.info(f"#{self.req_id} 已固定会话模型 {model}（回读 {effective}，显示名 {self.model_name}）")
            return
        # unverified（形态不可比，默认别名即此形态）只记一次，防同一会话刷屏
        if settle == "unverified" and getattr(self, "_model_unverified_logged", False):
            return
        self._model_unverified_logged = True
        log.warning(f"#{self.req_id} 会话模型未落定（{settle}）：请求 {model}，回读 {effective}")
        self._emit("system", f"模型未落定（{settle}）：请求 {model}，回读 {effective}，会话继续")

    def _readonly_mcp_servers(self, workdir):
        """只读角色：起本地 HTTP MCP server 提供受限工具（list_dir/read_file/db_query/git_log/
        search_text/git_status/git_diff；配置了 mysql 时追加 mysql_query）。
        roots 取构造时传入的 readonly_roots（dispatcher 含项目各真实仓库根 + 产物目录；
        board 建卡会话含工作目录 + 项目各仓库根）。失败仅返回空列表（agent 退回内置
        Read/Grep/Glob + fs 兜底），不阻断会话。
        #24 助手 profile 恒返回空列表——绝不注入 ReadonlyMcpServer（也不注入建卡工具），
        助手只持有调用方经 extra_mcp_servers 显式注入的业务 MCP。"""
        if self._is_assistant_profile():
            return []
        if not self._is_readonly():
            return []
        try:
            self._mcp = ReadonlyMcpServer(
                workdir, getattr(self, "readonly_roots", None) or [workdir],
                mysql_config=getattr(self, "mysql_config", None))
            return [{"type": "http", "name": READONLY_TOOL_SERVER, "url": self._mcp.url, "headers": []}]
        except Exception as e:
            log.warning(f"#{self.req_id} 只读工具 MCP server 启动失败（agent 退回内置工具兜底）: {e}")
            return []

    def start(self, workdir, resume_session_id=None):
        """initialize -> （按需 authenticate）-> session/resume 或 session/new。
        resume_session_id 给定时尝试恢复旧会话（阶段内重试/need_confirm 续跑），失败回退新会话；
        只读角色在会话建立（resume/new 均含）时经 mcpServers 注入受限工具 HTTP MCP server
        与调用方传入的额外 MCP server（如建卡会话的 create_requirement）。
        #24 助手 profile：能力声明全 false，mcpServers 只含 extra_mcp_servers（助手业务 MCP）——
        _readonly_mcp_servers 对该 profile 恒返回空列表。"""
        _init_payload = {
            "protocolVersion": 1,
            # kimi 0.37.2 起 ACP 服务端按 clientCapabilities.terminal 协商终端能力：
            # 未声明则 terminalEnabled=false，Bash/Grep/Glob 全部报
            # "ACP terminal capability is unavailable"（upstream#351 现场，2026-08-19）
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True},
                                   "terminal": True},
            "clientInfo": {"name": "req-board", "version": "2.1"},
        }
        if self._is_assistant_profile():
            # 助手声明无 fs 读写、无终端能力：服务端据此禁用内置 Read/Grep/Glob/Bash
            # （Bash 经 terminal 委托执行，terminal=false 即无执行通道）
            _init_payload["clientCapabilities"] = {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
        result = self.request("initialize", _init_payload, timeout=120)
        auth_methods = result.get("authMethods") or []
        if auth_methods and self._runtime_supports("supports_authenticate"):
            self.request("authenticate", {"methodId": auth_methods[0].get("id", "login")}, timeout=120)
        mcp_servers = self._readonly_mcp_servers(workdir) + list(self._extra_mcp_servers)
        if resume_session_id:
            try:
                result = self.request("session/resume", {
                    "sessionId": resume_session_id,
                    "cwd": str(workdir), "mcpServers": mcp_servers,
                }, timeout=120)
                self.session_id = resume_session_id
                self._apply_config(result)
                self._apply_permission_mode()
                self._apply_model()
                return True
            except Exception:
                pass
        result = self.request("session/new", {"cwd": str(workdir), "mcpServers": mcp_servers}, timeout=120)
        self.session_id = result["sessionId"]
        self._apply_config(result)
        self._apply_permission_mode()
        self._apply_model()
        return False

    def collect_usage(self, timeout=120):
        """会话收尾时发送 /usage 斜杠命令，从返回消息解析上下文用量。
        返回 {context_tokens, max_tokens, context_pct}，失败返回 None。
        #42：原文片段留存 _usage_raws 供缓存指标的 raw（返回形状不变，建卡会话零影响）；
        /usage 斜杠命令不是真实模型请求，不计入 _llm_turns_done。"""
        self._flush_message()
        if not self._runtime_supports("supports_usage_command"):
            return None
        self._capture = []
        try:
            self.request("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": "/usage"}],
            }, timeout=timeout)
            text = "".join(self._capture)
            self._note_raw_text(text)
            return parse_usage(text)
        except Exception:
            return None
        finally:
            self._capture = None

    def _note_raw_text(self, raw):
        """#42：原始 usage 证据留存（首见为准，供 cache_summary 的 raw 与 /usage 缓存行兜底解析）；
        dict 转 JSON 文本（失败降级 str）；桩会话（未走 __init__）安全。"""
        if getattr(self, "_usage_raws", None) is None:
            self._usage_raws = []
        if self._usage_raws:
            return
        if isinstance(raw, dict):
            try:
                raw = json.dumps(raw, ensure_ascii=False)
            except Exception:
                raw = str(raw)
        self._usage_raws.append(str(raw))

    def _note_usage(self, usage_obj, raw=None):
        """#42：采集一轮供应商 usage（prompt 响应 result / session/update 携带的 usage dict）。
        双字段皆空不记入 _turn_usages（unknown 由完成轮数判定，不由空记录凑数）；
        raw 交 _note_raw_text 首见留存；解析失败静默——指标缺失 ≠ 会话失败。"""
        try:
            norm = normalize_provider_usage(usage_obj)
        except Exception:
            norm = {"cached_tokens": None, "input_tokens": None}
        if raw is not None:
            self._note_raw_text(raw)
        if norm["cached_tokens"] is None and norm["input_tokens"] is None:
            return
        if getattr(self, "_turn_usages", None) is None:
            self._turn_usages = []
        self._turn_usages.append(norm)

    def cache_summary(self):
        """#42 缓存命中三态汇总（判据只看缓存采集自身，与峰值 parse_usage 三值解耦）：
        - hit：任一轮取得字段（显式 0 也是真实返回）→ token 求和；或 /usage 原文里有缓存行；
        - unknown：完成过 ≥1 轮真实请求但未取得任何字段（含解析失败）；
        - na：零完成请求（启动失败/首轮即超时或中止）→ token NULL。
        raw = 首个原始 usage 对象或 /usage 原文片段（落库截 2048），供「从真实会话记录核对」。"""
        usages = getattr(self, "_turn_usages", None) or []
        raws = getattr(self, "_usage_raws", None) or []
        raw = raws[0][:2048] if raws else None
        if usages:
            cached = sum(u["cached_tokens"] for u in usages if u["cached_tokens"] is not None)
            inp = sum(u["input_tokens"] for u in usages if u["input_tokens"] is not None)
            return {"state": "hit", "cached_tokens": cached, "input_tokens": inp, "raw": raw}
        for t in raws:   # /usage 原文里报了缓存行 = 供应商真实返回（input 无从取得，不估算）
            parsed = parse_usage_cache(t)
            if parsed["cached_tokens"] is not None:
                return {"state": "hit", "cached_tokens": parsed["cached_tokens"],
                        "input_tokens": parsed["input_tokens"], "raw": t[:2048]}
        if getattr(self, "_llm_turns_done", 0) >= 1:
            return {"state": "unknown", "cached_tokens": None, "input_tokens": None, "raw": raw}
        return {"state": "na", "cached_tokens": None, "input_tokens": None, "raw": None}

    def prompt(self, text, timeout, content=None):
        """发一轮 session/prompt。#44：`content` 非空时按调用方给定的 content blocks 发送
        （多模态：text / image），缺省 None 时报文逐字保持 [{"type": "text", "text": text}]。"""
        # #421 活性：客户端发出 session/prompt = 一次真实模型请求（llm 档起点，纯状态切换不走节流）
        self._activity("llm_request")
        self._resp_pending = True   # 本轮首个输出无条件上报 llm_response
        self._out_sent_mono = {}
        # #28 输出守卫：重新武装（清上一轮 tripped/aborted/recent，重置计时起点），令本轮可独立触发
        self._rearm_guard_for_prompt()
        self._guard_mark_llm(True, reset_start=True)
        blocks = content if content else [{"type": "text", "text": text}]
        try:
            result = self.request("session/prompt", {
                "sessionId": self.session_id,
                "prompt": blocks,
            }, timeout=timeout, guard=True)
        finally:
            # 回合结束：清在途状态（下一轮由新的 prompt/tool_end 重新武装）
            self._tool_open = set()
            self._resp_pending = False
            self._out_sent_mono = {}
            self._guard_mark_llm(False)
        # #42：完成轮计数放在 finally 之外——request 超时/守卫中止/断连先抛异常再进 finally，
        # 失败轮不算完成请求（na 判据 = 零完成请求）；响应 result 可能携带供应商 usage
        # （此前被丢弃），交 _note_usage 采集（无字段为无害 no-op）
        self._llm_turns_done = getattr(self, "_llm_turns_done", 0) + 1
        if isinstance(result, dict):
            u = result.get("usage")
            self._note_usage(u if isinstance(u, dict) else result,
                             raw=u if isinstance(u, dict) else None)
        return result

    def _rearm_guard_for_prompt(self):
        """#28：每轮 prompt 重置守卫（与 rearm_output_guard 口径一致，防跨轮残留 tripped/recent）。"""
        g = getattr(self, "_guard", None)
        if g is None:
            return
        with self._guard_lock:
            g["tripped"] = None
            g["aborted"] = False
            g["recent"].clear()
            g["steps"] = 0          # C2 步数预算按轮计
        self._guard_pending = None

    def cancel(self):
        try:
            self._send({"jsonrpc": "2.0", "method": "session/cancel",
                        "params": {"sessionId": self.session_id}})
        except Exception:
            pass

    def close(self, kill=False):
        self._closed = True          # 停掉静默 flush 线程（先停线程再收进程）
        try:
            self._terminal_cleanup()
        except Exception:
            pass
        try:
            if self._mcp:
                self._mcp.close()
        except Exception:
            pass
        try:
            if kill:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            else:
                if getattr(self, "runtime_id", "kimi") == "kimi":
                    self.proc.terminate()
                else:
                    # npx 是启动父进程；只停父进程会留下适配器持有 stdout，关闭管道死锁。
                    os.killpg(self.proc.pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        if getattr(self, "runtime_id", "kimi") != "kimi":
            try:
                # 本会话独立进程组由 Popen(start_new_session=True) 创建，只清理自己的后代。
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # stderr 可能是调用方的日志文件，随管道一起关；调用方再次 close 是空操作
        self._close_proc_pipes(self.proc)
