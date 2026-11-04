#!/bin/bash
# launchd 托管下的进程入口：读取 .env → exec 真正的进程
# 用法: scripts/launchd-run.sh <board|dispatcher>
#
# 为什么需要这一层：
#   launchd 不读 .env（只有 run_local.sh 会读）。若把变量直接写进 plist，敏感值
#   会落到 ~/Library/LaunchAgents/ 的明文文件里，且与 run_local 的环境语义分叉。
#   本脚本让「launchd 托管」与「直跑」共用同一套 .env 读取规则，plist 里只留路径。
#   飞书凭据由仓库根 feishu.json 提供（设置抽屉 → 飞书机器人），缺失由调度器自行降级；
#   显式 REQ_BOARD_NO_FEISHU 仍尊重。
#
# 两条硬约束：
#   1. 绝不导出 REQ_BOARD_SUPERVISOR=run_local——那是 run_local.sh 的直跑标记（#8），
#      在 launchd 下会把自动重启引向错误分支（调度器 exec 自重启、看板降级成人工提示）。
#      这里显式 unset：launchd 下必须为空 = launchd 路径，行为与旧版逐字一致。
#   2. 补 PATH——launchd 的默认 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，不含
#      ~/.kimi-code/bin（kimi CLI）、/opt/homebrew/bin、/usr/local/bin（node）。
#      缺了它调度器拉不起 agent。
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# 约束 1：清掉可能从调用方环境继承的直跑标记
unset REQ_BOARD_SUPERVISOR

# 约束 2：把常用用户级 bin 追加到 PATH（追加而非前置：系统工具优先，避免被同名程序顶掉）
export PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}:$HOME/.local/bin:$HOME/.kimi-code/bin:/opt/homebrew/bin:/usr/local/bin"

# .env 导出（与 run_local.sh 同一套规则；模板见 .env.example）
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

PY="${REQ_BOARD_PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"; else PY="python3"; fi
fi

case "${1:-}" in
    board)      APP="board.py" ;;
    dispatcher) APP="dispatcher.py" ;;
    *) echo "用法: $0 <board|dispatcher>" >&2; exit 2 ;;
esac

echo "[launchd-run] $(date '+%Y-%m-%d %H:%M:%S') 启动 $APP（wrapper pid $$，仓库 $REPO）"

# exec：不额外留一层进程，launchd 看到的 pid 就是真进程的 pid（与 run_local 的可观测性一致）
exec "$PY" "$APP"
