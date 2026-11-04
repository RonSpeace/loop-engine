#!/bin/bash
# 需求看板日志轮转：launchd 只负责把 stdout/stderr 追加到文件，不管大小——看板 err 日志曾被
# Werkzeug 的访问日志撑到 424MB（2026-09-18 清理）。本脚本按大小轮转，超限即归档并原地清空。
#
# 为什么用 copytruncate（复制一份 .1 再清空原文件）而不是 mv：
#   launchd 启动服务时就把日志文件句柄握在手里，mv 之后它继续往被改名的 inode 写，
#   新的 req-board.log 永远不会出现、后续日志全丢在 .1 里。实测（2026-09-18）清空原文件后
#   launchd 接着正常追加、不产生稀疏空洞，所以原地清空是安全的，且不需要重启服务。
#   已知代价：复制与清空之间的几行可能丢（标准 copytruncate 语义，可接受）。
#
# 由 launchd 定时任务 local.automation.log-rotate 每 30 分钟调用一次；也可手工执行。
# 可调：REQ_BOARD_LOG_DIR（默认 macOS ~/Library/Logs/req-board；Linux 等
#       ${XDG_STATE_HOME:-~/.local/state}/loop-engine/logs，与 dispatcher_ops._default_log_dir 同口径）、REQ_BOARD_LOG_MAX_MB（默认 20）、
#       REQ_BOARD_LOG_KEEP（默认 2，保留 .1/.2）
set -euo pipefail

if [ "$(uname -s)" = "Darwin" ]; then
    DEFAULT_LOG_DIR="$HOME/Library/Logs/req-board"
else
    DEFAULT_LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/loop-engine/logs"
fi
DATA_DIR="${REQ_BOARD_LOG_DIR:-$DEFAULT_LOG_DIR}"
MAX_MB="${REQ_BOARD_LOG_MAX_MB:-20}"
KEEP="${REQ_BOARD_LOG_KEEP:-2}"
LIMIT_BYTES=$(( MAX_MB * 1024 * 1024 ))

LOG_FILES=(
    req-board.log
    req-board.err.log
    req-board-dispatcher.log
    req-board-dispatcher.err.log
)

ts() { date '+%Y-%m-%d %H:%M:%S'; }

rotated=0
for name in "${LOG_FILES[@]}"; do
    f="$DATA_DIR/$name"
    [ -f "$f" ] || continue
    # wc -c 在 BSD/GNU 上通用（stat 的参数两边不同：-f%z vs -c%s）
    size=$(wc -c < "$f" 2>/dev/null | tr -d ' ')
    size=${size:-0}
    if [ "$size" -le "$LIMIT_BYTES" ]; then
        continue
    fi
    # 代际后移：.N 丢弃，.N-1 → .N …… .1 让位
    rm -f "$f.$KEEP"
    i=$KEEP
    while [ "$i" -gt 1 ]; do
        prev=$(( i - 1 ))
        [ -f "$f.$prev" ] && mv "$f.$prev" "$f.$i"
        i=$prev
    done
    cp "$f" "$f.1"
    : > "$f"
    echo "$(ts) 轮转 $name（$(( size / 1024 / 1024 ))MB → $name.1，保留 $KEEP 代）"
    rotated=$(( rotated + 1 ))
done

if [ "$rotated" -eq 0 ]; then
    echo "$(ts) 无需轮转（上限 ${MAX_MB}MB）"
fi
