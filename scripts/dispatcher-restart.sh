#!/bin/bash
# #373 约定式重启入口：先写重启来源标记再发 SIGTERM；launchd 模式 KeepAlive 自动拉起，
# run_local 直跑模式（REQ_BOARD_SUPERVISOR=run_local）由调度器信号 handler 内 exec 自重启。
# 用途：人工重启调度器一律走本脚本（替代裸 kill <pid>），新进程启动日志/看板健康角标
#       可显示「上次退出 SIGTERM @ 时间（来源: script）」。
# 用法：scripts/dispatcher-restart.sh <pid> [来源]   （来源默认 script，仅限字母数字与-_
# 说明：先校验 pid 确为 dispatcher.py 进程（防误杀），写 data/dispatcher_restart_source.json，
#       再 kill -TERM；调度器信号 handler 读到来源后记录进心跳并删除标记。
set -euo pipefail
if [ $# -lt 1 ]; then
    echo "用法: $0 <pid> [来源]" >&2
    exit 1
fi
pid="$1"
src="${2:-script}"
case "$src" in
    *[!a-zA-Z0-9_-]*) echo "来源只允许字母数字与-_：$src" >&2; exit 1 ;;
esac
# 目标校验（#8 改末 token 口径）：取整条 command 的末个空白分隔 token 的 basename 精确等于 dispatcher.py。
# 末 token 口径覆盖两种启动形态——`<python> dispatcher.py`（run_local 首启）与
# `<python> <含空格绝对路径>/dispatcher.py`（exec 自重启后 argv 为绝对路径；旧 awk $2 口径
# 遇含空格路径被截断为 ".../Loop" 必然误拒）；前提：启动命令行无尾随参数（run_local/exec/launchd
# 三形态现均满足）。不能用子串匹配整条 command——「命令行里含 dispatcher.py 字样」的 bash
# （如本脚本自身所在命令）会被误放行，造成误杀（C locale 下 PS 输出全命令字符串）。
# -ww（BSD: 不限输出宽）防非终端/管道下 PS 截断长命令行，零语义副作用。
# 与 board.py 的 _dispatcher_pid_heartbeat()（末 token basename==dispatcher.py）同规则两处落地
# （bash/python 无法共享代码，注释互引）。
last="$(ps -ww -p "$pid" -o command= 2>/dev/null | awk '{print $NF}')"
case "${last##*/}" in
    dispatcher.py) ;;
    *) echo "pid $pid 非 dispatcher.py 进程（command 末 token: ${last:-未知}），拒绝重启" >&2; exit 1 ;;
esac
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$DIR/data"
python3 - "$src" "$DIR/data/dispatcher_restart_source.json" <<'PY'
import json, sys, time
with open(sys.argv[2], "w") as f:
    json.dump({"source": sys.argv[1], "at": round(time.time(), 3)}, f)
PY
# 注意：${src}/${pid} 必须大括号定界——bash 3.2 在 C locale（LANG 为空）下会把
# 变量后紧跟的 UTF-8 多字节字符首字节误当变量名延续（$src（ → unbound variable）
echo "已写入重启来源标记（${src}），发送 SIGTERM 给 pid ${pid}（launchd 模式 KeepAlive 拉起；run_local 直跑模式进程内 exec 自重启）"
# 发送失败即清标记（#8 同口径）：防残留标记在 TTL 内被随后 Ctrl-C 的 kill 0 SIGTERM 命中而
# 误触发 exec、劫持停止语义（调度器 auto 路径与 board 直跑分支同款处理）
if ! kill -TERM "${pid}"; then
    rm -f "$DIR/data/dispatcher_restart_source.json"
    echo "发送 SIGTERM 失败（pid ${pid} 已退出？），已清除来源标记" >&2
    exit 1
fi
