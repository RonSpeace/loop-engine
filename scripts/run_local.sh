#!/bin/bash
# 本机前台直跑：看板 + 调度器（不依赖 launchd；开发/试用模式，Ctrl-C 全停）
# 用法: scripts/run_local.sh [all|board|dispatcher]   （默认 all）
# 说明：
# - .env 存在时自动导出（KEY=VALUE 行）；飞书凭据由仓库根 feishu.json 提供
#   （设置抽屉 → 飞书机器人），缺失由调度器自行降级；显式 REQ_BOARD_NO_FEISHU 仍尊重。
# - 改 board.py / dispatcher.py / flows.json / prompts/ 后无热加载：Ctrl-C 重跑本脚本。
# - 本脚本导出 REQ_BOARD_SUPERVISOR=run_local（#8 直跑模式标记）：调度器重启改为进程内 os.execv
#   自重启（同 PID，整组不停止）；看板自动重启降级为页头角标提示（人工 Ctrl-C 后重跑本脚本）。
#   不设该变量 = launchd/未知模式，行为与旧版逐字一致。
# - 生产化（开机自启/崩溃拉起）再转 launchd 模式：按本机路径生成 plist 后用 ./start.sh。

set -u
cd "$(dirname "$0")/.."

PY="${REQ_BOARD_PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi
fi

# .env 导出（存在则读入；模板见 .env.example）
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

MODE="${1:-all}"
case "$MODE" in
    all|board|dispatcher) ;;
    *) echo "用法: $0 [all|board|dispatcher]" >&2; exit 2 ;;
esac

# #8：直跑模式标记（子进程拉起前导出，board/dispatcher 均继承）——调度器据此 exec 自重启、
# 看板据此降级重启提示；不设 = launchd/未知模式（行为与旧版逐字一致，向后兼容）。
export REQ_BOARD_SUPERVISOR=run_local

# Ctrl-C / 终止时杀掉本脚本拉起的全部子进程
trap 'trap - INT TERM EXIT; kill 0 2>/dev/null; exit 0' INT TERM EXIT

PIDS=()
NAMES=()   # 与 PIDS 平行：退出时点名是哪个子进程死了
if [ "$MODE" = "all" ] || [ "$MODE" = "board" ]; then
    echo "[run_local] 启动看板 http://${REQ_BOARD_HOST:-127.0.0.1}:${REQ_BOARD_PORT:-8899} ..."
    "$PY" board.py &
    PIDS+=($!)
    NAMES+=("看板 board.py")
fi
if [ "$MODE" = "all" ] || [ "$MODE" = "dispatcher" ]; then
    echo "[run_local] 启动调度器（15s 轮询，日志见本终端）..."
    "$PY" dispatcher.py &
    PIDS+=($!)
    NAMES+=("调度器 dispatcher.py")
fi

# 任一子进程退出即整体退出（避免半套服务假活）——注意是「任一」不是「全部」：
# 旧实现只要还有任一子进程活着就继续循环，于是 board.py 被端口冲突打死时（实测输出
# Address already in use）脚本不停，留下「只有调度器、没有看板」的半套服务：终端看着在跑、
# 网页却拒绝连接（2026-10-10 现场）。判活必须逐个看，第一个死掉的即整组停止。
# （macOS 自带 bash 3.2 无 wait -n，用轮询判活实现同等语义）
# 与 #8 兼容：run_local 下调度器重启走信号 handler 内 os.execv，PID 不变，
# 判活循环观察不到「死亡」，不会误触发整组停止。
dead=""
dead_name=""
while [ -z "$dead" ]; do
    i=0
    for p in "${PIDS[@]}"; do
        if ! kill -0 "$p" 2>/dev/null; then
            dead="$p"
            dead_name="${NAMES[$i]}"
            break
        fi
        i=$((i + 1))
    done
    [ -n "$dead" ] || sleep 1
done
echo "[run_local] ${dead_name}（pid ${dead}）已退出，全组停止（详见上方日志）"
exit 1
