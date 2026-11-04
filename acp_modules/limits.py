"""只读工具与审批相关的常量（MCP server 名、助手构造档、输出限量、只读命令表）。

由 acp_shared.py 拆出；acp_shared 以显式名单回挂全部名字（含下划线名）。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DBQ_SCRIPT = REPO_DIR / "scripts" / "dbq.py"
BOARD_DB = REPO_DIR / "data" / "board.db"      # db_query 默认看板库


READONLY_TOOL_SERVER = "reqboard-tools"      # MCP server 名（会话 mcpServers 条目）

# ---- #24 飞书看板助手 ACP 构造档（tool_profile）----
# 助手会话与普通会话（dev/只读/建卡/需求助手）在同一条 ACPSession 实现上分档：助手只注入
# 自己的业务工具 MCP，绝不注入 ReadonlyMcpServer，审批只放行该 MCP 工具、反向 fs/terminal
# 全拒、initialize 能力声明全 false（三层拒绝矩阵）。判定唯一入口 = _is_assistant_profile()；
# 缺省 tool_profile=None 时四条路径逐字不变。
TOOL_PROFILE_FEISHU_ASSISTANT = "feishu_assistant"
TOOL_PROFILES = (TOOL_PROFILE_FEISHU_ASSISTANT,)
# 助手 profile 反向请求拒绝清单（fs 读写；terminal/* 按方法前缀拒绝，见 _handle_request）
ASSISTANT_DENIED_METHODS = ("fs/read_text_file", "fs/write_text_file")
ASSISTANT_DENIED_TERMINAL_PREFIX = "terminal/"

DIR_MAX_ENTRIES = 500                        # list_dir 条目上限（超出截断提示）
FILE_MAX_LINES = 200                         # read_file 默认行数
FILE_MAX_LINES_LIMIT = 2000                  # read_file max_lines 上限
FILE_MAX_BYTES = 1024 * 1024                 # read_file 内容字节上限（对齐 fs/read 上限）
DBQ_TIMEOUT = 30                             # db_query 子进程超时（秒）
MYSQL_MAX_ROWS = 200                         # mysql_query 单次最多返回行数
MYSQL_MAX_CELL = 120                         # mysql_query 单元格截断字符数
MYSQL_CONNECT_TIMEOUT = 10                   # mysql_query 建连超时（秒）
MYSQL_QUERY_TIMEOUT_MS = 30000               # mysql_query 单语句执行上限（MAX_EXECUTION_TIME）
GIT_LOG_DEFAULT_COUNT = 15                   # git_log 默认 count（对齐 #84 现场 -15）
GIT_LOG_MAX_COUNT = 100                      # git_log count 上限
GIT_LOG_TIMEOUT = 30                         # git_log 子进程超时（秒），对齐 DBQ_TIMEOUT
GIT_LOG_LINE_MAX = 200                       # git_log 单行截断字符数（对齐 dbq.py MAX_CELL 防御思想）
GIT_LOG_EMPTY_HINT = "（无提交匹配）"           # git_log 空匹配提示（非 error）
GIT_LOG_FORMAT = "%h %ad %an %s"             # git_log 固定输出格式（不暴露自定义 --format/--pretty）
# ---- #119 新增只读工具限量常量（search_text / git_status / git_diff，fail-closed 输出限量） ----
SEARCH_MAX_MATCHES = 50                      # search_text 命中行上限
SEARCH_MAX_SCAN = 2000                       # search_text 扫描文件数上限（超出提示缩小 path 范围）
SEARCH_MAX_BYTES = 1024 * 1024               # search_text 跳过超 1MB 大文件（对齐 FILE_MAX_BYTES）
SEARCH_LINE_MAX = 200                        # search_text 单行截断字符数（对齐 GIT_LOG_LINE_MAX）
GIT_STATUS_MAX_LINES = 500                   # git_status 输出行上限
TOOL_OUTPUT_MAX_CHARS = 30000                # C6：只读工具单次输出总长上限（超出截断并说明续读方式）
GIT_DIFF_MAX_LINES = 1000                    # git_diff 输出行上限（超大 diff 截断提示）
GIT_TOOL_TIMEOUT = 30                        # git_status/git_diff 子进程超时（秒，对齐 GIT_LOG_TIMEOUT）
# git_diff 只读参数白名单：仅枚举放行，其余（-o/--output/--ext-diff/--no-index/commit 范围等）一律拒绝
GIT_DIFF_ALLOWED_ARGS = ("--stat", "--name-only", "--name-status", "--numstat",
                         "--shortstat", "--summary", "--cached", "--no-color")
COMMIT_RE = re.compile(r"\bgit\s+(add|commit|push|reset|rebase|checkout|switch|stash|merge|cherry-pick|revert|clean|restore|mv|rm)\b", re.I)
# 只读 Bash 命令白名单。#55（2026-10-20）analysis 会话实测：agent 惯用
# `ls x 2>/dev/null; echo "---"; ls y 2>/dev/null` 这类带分隔符/丢弃重定向的只读命令，因
# echo 不在表内 + 重定向被 token 层整体拒绝 → agent 误判「Bash 被限制」后弃用 Bash。
# echo 只写 stdout、pwd 只读进程状态，均无文件副作用；写入型重定向仍由 token 层拒绝。
READONLY_CMDS = ("ls", "cat", "head", "tail", "wc", "file", "stat", "find", "date", "echo", "pwd")
READONLY_FLAG_RE = re.compile(r"-{1,2}[A-Za-z0-9=:]+")
NUM_RE = re.compile(r"\d+[kKmM]?")
FIND_BANNED = ("-exec", "-execdir", "-ok", "-okdir", "-delete")
# 只读命令放行的「丢弃输出」型重定向：`[1|2]>/dev/null` 与 `2>&1`（操作符两侧可带空格）。
# 只是丢弃/合并输出，不落任何文件；其余重定向（> file / >> / &>* / 2>/tmp/x 等）不剥离，
# 继续由 _shell_tokens 判非法（fail-closed，白名单不因此放宽写边界）。
NULL_REDIRECT_RE = re.compile(r"([12]?)\s*>\s*(/dev/null|&1)(?=$|[\s;|])")

_READ_VERB_RE = re.compile(r"^\s*(SELECT|PRAGMA|EXPLAIN|WITH|VALUES)\b", re.I | re.S)
