#!/bin/bash
# 安装/更新 launchd 托管（看板 + 调度器 + 日志轮转）
# 用法: scripts/install-launchd.sh [--no-load]
#         --no-load  只渲染 plist + 建日志目录，不 bootstrap/不启动
#
# 做的事：
#   1. 把仓库 plists/*.plist.tmpl 按本机路径渲染成 ~/Library/LaunchAgents/<label>.plist
#      （{{REPO}} = 本机仓库根目录，{{LOG_DIR}} = 日志目录；仓库里不存任何机器绝对路径，
#       两台机器共用同一份模板，换机/改用户名都不用手改 plist）
#   2. 建好日志目录（launchd 不会替你建，建不出来 job 直接起不来）
#   3. bootout 旧的同名 job 再 bootstrap（改了模板后重跑本脚本即可生效）
#   4. 自检：launchctl list + curl 看板
#
# 幂等：可反复运行。换机/重装后一条命令恢复全部托管。
# 卸载：launchctl bootout gui/$(id -u)/<label> && rm ~/Library/LaunchAgents/<label>.plist
#       （三个 label 见下方 LABELS；渲染出的 plist 是真文件，不再是软链）
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO/plists"
DEST_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="${REQ_BOARD_LOG_DIR:-$HOME/Library/Logs/req-board}"
DOMAIN="gui/$(id -u)"
PORT="${REQ_BOARD_PORT:-8899}"

LABELS=(
    local.req-board
    local.automation.req-board-dispatcher
    local.automation.log-rotate
)

NO_LOAD=0
if [ "${1:-}" = "--no-load" ]; then NO_LOAD=1; fi

echo "[install-launchd] 仓库        : $REPO"
echo "[install-launchd] 日志目录    : $LOG_DIR"
echo "[install-launchd] LaunchAgents: $DEST_DIR"
echo

# --- 0. 前置检查 ---------------------------------------------------------------
for f in board.py dispatcher.py; do
    [ -f "$REPO/$f" ] || { echo "❌ 缺 $REPO/$f"; exit 1; }
done
if [ -x "$REPO/.venv/bin/python" ]; then
    echo "   解释器: $REPO/.venv/bin/python"
else
    echo "   ⚠️  $REPO/.venv/bin/python 不存在或不可执行——plist 会退化为 python3（多半起不来）"
fi

# 端口占用检查：只放行「已由本 job 托管」的占用，其余一律拒绝。
# 这一条正是 2026-10-10 那次故障的成因（直跑实例还活着就 bootstrap 第二份，撞 8899）。
PRE_PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
if [ -n "$PRE_PID" ] && [ "$NO_LOAD" = "0" ]; then
    if launchctl print "$DOMAIN/local.req-board" >/dev/null 2>&1; then
        echo "   端口 $PORT 已由 launchd 托管看板占用（pid ${PRE_PID}），将 bootout 后重装"
    else
        echo "❌ 端口 $PORT 已被 pid $PRE_PID 占用，且不是 launchd 托管的看板。"
        echo "   先停掉它再安装。直跑模式（run_local.sh）的正确停机姿势："
        echo "       kill -TERM -<run_local.sh 的 pid>     # trap 的 kill 0 带走整组"
        echo "   确认释放：lsof -nP -iTCP:$PORT -sTCP:LISTEN"
        echo "   占用者进程："
        ps -o pid,ppid,pgid,command -p "$PRE_PID" || true
        exit 1
    fi
fi

mkdir -p "$DEST_DIR" "$LOG_DIR"

# --- 1. 渲染 plist 模板到 ~/Library/LaunchAgents -------------------------------
# plist 不支持变量展开，直接软链模板会得到含 {{...}} 的非法 XML；因此按本机路径
# 渲染成真实文件。模板只认 {{REPO}} / {{LOG_DIR}} 两个占位符。
escape_sed() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
REPO_ESC="$(escape_sed "$REPO")"
LOG_DIR_ESC="$(escape_sed "$LOG_DIR")"

render_plist() {
    local tmpl="$1" out="$2"
    sed -e "s|{{REPO}}|$REPO_ESC|g" -e "s|{{LOG_DIR}}|$LOG_DIR_ESC|g" "$tmpl" > "$out"
    if grep -q '{{' "$out"; then
        echo "❌ 渲染后仍有未替换占位符: $out"
        grep -n '{{' "$out" || true
        return 1
    fi
}

for label in "${LABELS[@]}"; do
    src="$SRC_DIR/$label.plist.tmpl"
    dst="$DEST_DIR/$label.plist"
    [ -f "$src" ] || { echo "❌ 缺模板 $src"; exit 1; }
    # 旧版安装是指向 plists/*.plist 的软链，先删掉，避免写入穿透到仓库
    rm -f "$dst"
    render_plist "$src" "$dst" || exit 1
    if ! plutil -lint "$dst" >/dev/null 2>&1; then
        echo "❌ 渲染出的 plist 格式非法: $dst"
        plutil -lint "$dst" || true
        exit 1
    fi
    echo "   渲染 ${dst}（仓库 ${REPO}）"
done

if [ "$NO_LOAD" = "1" ]; then
    echo
    echo "[install-launchd] --no-load：已渲染未启动。"
    echo "   启动：launchctl bootstrap $DOMAIN ~/Library/LaunchAgents/local.req-board.plist"
    exit 0
fi

# --- 2. 卸载旧的同名 job（bootout 是唯一可靠的停止手段；裸 kill 会被 KeepAlive 拉起）----
for label in "${LABELS[@]}"; do
    if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        echo "   已卸载旧 job: $label"
    fi
done

# --- 3. bootstrap（board / dispatcher 的 RunAtLoad 使其立即启动）-----------------
for label in "${LABELS[@]}"; do
    if ! launchctl bootstrap "$DOMAIN" "$DEST_DIR/$label.plist"; then
        echo "❌ bootstrap 失败: $label"
        exit 1
    fi
    echo "   已加载: $label"
done

# --- 4. 自检 ------------------------------------------------------------------
sleep 3
echo
echo "[install-launchd] launchctl list:"
launchctl list 2>/dev/null | grep -E "req-board|log-rotate" || echo "   （未列出，异常）"

code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)"
if [ "$code" = "200" ]; then
    echo "✅ 看板已由 launchd 托管并启动: http://127.0.0.1:$PORT"
else
    echo "❌ 看板未就绪（HTTP ${code}）。日志尾部："
    tail -20 "$LOG_DIR/req-board.err.log" 2>/dev/null || echo "   （无 $LOG_DIR/req-board.err.log）"
    exit 1
fi
