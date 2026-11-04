#!/bin/bash
# 启动/重启需求看板（launchd 托管：local.req-board，KeepAlive 崩溃自拉起）
# 重启 = launchctl kickstart -k：杀旧实例并立即拉起，不产生孤儿进程
#
# 首次安装 / 换机 / 改了 plists/*.tmpl 之后：先跑 scripts/install-launchd.sh
# 本脚本只管日常重启，不负责安装（plist 由安装脚本按本机路径渲染）。

cd "$(dirname "$0")"
REPO="$(pwd -P)"

LABEL=local.req-board
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_FILE="${REQ_BOARD_LOG_DIR:-$HOME/Library/Logs/req-board}/req-board.log"

if [ ! -f "$PLIST" ]; then
    echo "❌ 缺少 $PLIST"
    echo "   模板：$REPO/plists/$LABEL.plist.tmpl"
    echo "   首次安装/换机后请先运行：scripts/install-launchd.sh（按本机路径渲染）"
    exit 1
fi

# 未加载则先 bootstrap（RunAtLoad 会自动启动）
if ! launchctl list | grep -q "^$LABEL"; then
    launchctl bootstrap gui/$(id -u) "$PLIST" 2>/dev/null || true
fi

if ! launchctl kickstart -k gui/$(id -u)/$LABEL; then
    echo "❌ launchctl kickstart 失败（服务未重启，可能仍是旧进程）"
    exit 1
fi

# 轮询等就绪，不用固定 sleep：首次启动（含 TCC 首次对 ~/Documents 做权限评估）实测可达
# 45 秒，固定 sleep 2 会把正常的冷启动误报成失败（2026-10-10 现场）。热启动约 1-2 秒。
PORT="${REQ_BOARD_PORT:-8899}"
ready=0
for _ in $(seq 1 30); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$PORT/" 2>/dev/null)" = "200" ]; then
        ready=1
        break
    fi
    sleep 1
done

if [ "$ready" = "1" ]; then
    echo "✅ 需求看板已启动: http://127.0.0.1:$PORT"
else
    echo "❌ 启动失败（30 秒内未就绪），查看日志:"
    tail -20 "$LOG_FILE"
    exit 1
fi
