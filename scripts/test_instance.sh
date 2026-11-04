#!/bin/bash
# 需求 #108 测试实例精确启停（worktree 内自验用，不跑 ./start.sh——那是 main 实例）。
# 用法: scripts/test_instance.sh start [PORT=8901] [--seed] | stop [PORT] | status [PORT]
#   --seed（可选，仅 start）：建每端口临时数据目录 /tmp/req_board_test_<PORT>_data，跑
#   scripts/seed_test_db.py 落确定性种子数据，并以隔离环境启动（REQ_BOARD_DATA/REQ_BOARD_PROJECTS
#   指向该目录、REQ_BOARD_FREEZE_TIME 冻结时钟、关飞书与 SSE）——供 scripts/visual_gate.py 等
#   浏览器级自验使用；stop 时连同该临时目录一并删除。不带 --seed 的行为与原先完全一致。
# 原则：全程按 PID 文件 + 后代收集 + lsof 精确清理，禁止 pkill 路径猜测
# （防误杀他人会话/驱动 kimi 进程，见 acp_shared.KILL_REDLINE）。
# 端口默认 8901（避开 main 实例 8899；board 支持 REQ_BOARD_PORT 覆盖）。
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# python 解释器：默认 python3；worktree 无 venv 时用 REQ_BOARD_PYTHON 覆盖（如 main 工作区 .venv）
PY="${REQ_BOARD_PYTHON:-python3}"
PORT="${2:-8901}"
case "$PORT" in
    ''|*[!0-9]*) echo "非法端口: $PORT" >&2; exit 2 ;;
esac
PID_FILE="/tmp/req_board_test_${PORT}.pid"
LOG_FILE="/tmp/req_board_test_${PORT}.log"
CMD="${1:-status}"
SEED_FLAG="${3:-}"
case "$SEED_FLAG" in
    ''|--seed) ;;
    *) echo "未知参数: $SEED_FLAG（仅支持 --seed）" >&2; exit 2 ;;
esac
# --seed 模式的每端口临时数据目录（路径只由已校验的纯数字端口拼成；stop 时删除）
SEED_DIR="/tmp/req_board_test_${PORT}_data"

collect_descendants() {
    # 递归收集 <pid> 的全部后代 PID（board → kimi-chat-acp 链；Popen start_new_session=True
    # 只脱离会话组、PPID 不变，仍可追溯）。必须在杀父进程前调用（父死后后代 PPID 变 1 无法追溯）。
    local pid="$1" kids k
    kids=$(ps -axo pid=,ppid= | awk -v p="$pid" '$2 == p {print $1}')
    for k in $kids; do
        echo "$k"
        collect_descendants "$k"
    done
}

start() {
    stop >/dev/null 2>&1 || true   # 幂等：该端口已有实例先清理
    if [ "$SEED_FLAG" = "--seed" ]; then
        local freeze
        freeze=$(cd "$REPO_ROOT" && "$PY" scripts/seed_test_db.py --print-freeze-time) || return 1
        (cd "$REPO_ROOT" && "$PY" scripts/seed_test_db.py --data-dir "$SEED_DIR" --freeze-time "$freeze" \
            >>"$LOG_FILE" 2>&1) || {
            echo "种子数据写入失败，日志尾部: $LOG_FILE" >&2
            tail -n 20 "$LOG_FILE" >&2
            return 1
        }
        # 隔离环境：数据/项目注册表/产物/素材/飞书凭据/kimi 配置全部指向临时目录，时钟冻结，关飞书与 SSE
        (cd "$REPO_ROOT" && REQ_BOARD_PORT="$PORT" REQ_BOARD_DATA="$SEED_DIR" \
            REQ_BOARD_PROJECTS="$SEED_DIR/projects.json" REQ_BOARD_FREEZE_TIME="$freeze" \
            REQ_BOARD_NO_FEISHU=1 REQ_BOARD_SSE=0 \
            REQ_BOARD_ARTIFACTS="$SEED_DIR/artifacts" REQ_BOARD_MATERIALS="$SEED_DIR/materials" \
            REQ_BOARD_FEISHU_JSON="$SEED_DIR/feishu.json" KIMI_CODE_HOME="$SEED_DIR/kimi-home" \
            "$PY" board.py >>"$LOG_FILE" 2>&1 &
            echo $! > "$PID_FILE")
    else
        (cd "$REPO_ROOT" && REQ_BOARD_PORT="$PORT" "$PY" board.py >>"$LOG_FILE" 2>&1 &
            echo $! > "$PID_FILE")
    fi
    local pid
    pid=$(cat "$PID_FILE")
    local i
    for i in $(seq 1 30); do
        if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT")" = "200" ]; then
            echo "测试实例已启动: http://127.0.0.1:$PORT (board pid=$pid)"
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "启动失败（进程已退出），日志尾部: $LOG_FILE" >&2
            tail -n 20 "$LOG_FILE" >&2
            rm -f "$PID_FILE"
            return 1
        fi
        sleep 1
    done
    echo "启动超时（30s），日志尾部: $LOG_FILE" >&2
    tail -n 20 "$LOG_FILE" >&2
    return 1
}

stop() {
    local pid="" leftover=""
    [ -f "$PID_FILE" ] && pid=$(cat "$PID_FILE")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        leftover=$(collect_descendants "$pid")
        # TERM board（触发 _sigterm_handler → _cleanup_chat_procs 清理 chat kimi 子进程），等 ≤3s
        kill -TERM "$pid" 2>/dev/null
        local i
        for i in 1 2 3; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null
        fi
    fi
    # 预收集的后代中仍存活者逐个精确 KILL（覆盖 board 被 SIGKILL 的孤儿场景）
    local k
    for k in $leftover; do
        kill -0 "$k" 2>/dev/null && kill -KILL "$k" 2>/dev/null
    done
    # lsof 兜底：端口占用者逐个精确 KILL（重复 stop 的幂等路径）
    local holders h
    holders=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
    for h in $holders; do
        kill -KILL "$h" 2>/dev/null
    done
    rm -f "$PID_FILE"
    # --seed 模式的临时数据目录随实例一并清理（非 --seed 启动不会有该目录）
    [ -d "$SEED_DIR" ] && rm -rf "$SEED_DIR"
    echo "已停止: 端口 $PORT 测试实例"
}

status() {
    local pid=""
    [ -f "$PID_FILE" ] && pid=$(cat "$PID_FILE")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "运行中: board pid=${pid}（端口 ${PORT}），后代进程："
        local k
        for k in $(collect_descendants "$pid"); do
            ps -o pid=,command= -p "$k" 2>/dev/null
        done
    else
        echo "无运行实例（端口 ${PORT}）"
    fi
}

case "$CMD" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) echo "用法: $0 start [PORT=8901] [--seed] | stop [PORT] | status [PORT]" >&2; exit 2 ;;
esac
