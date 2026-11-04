"""#32 Phase 4：dispatcher_ops 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import is_run_local_supervisor
from pathlib import Path
import json
import os
import re
import signal
import sys
import time
import urllib.request

from board_modules.runtime import resolve
# read_logs 白名单里 launchd 日志目录的候选文件名（严格闭集，不合模式一律拒）。
# 目录 = REQ_BOARD_LOG_DIR，缺省按平台：macOS ~/Library/Logs/req-board（plists/ 下三个 plist 的
# StandardOutPath 落点）；其它系统 $XDG_STATE_HOME/loop-engine/logs（缺省 ~/.local/state/loop-engine/logs）
REQ_CHAT_LOG_NAME_RE = re.compile(r"req-board(-dispatcher)?(\.err)?\.log(\.\d+)?\Z")


# ==================== #324 善后工具集：服务端实现（进程内 subprocess，不进 agent Bash 白名单） ====================

def _reqchat_automation_data():
    """遗留（原作者布局）automation 项目 data 目录——旧日志落点，保留兼容；
    projects.json 未配置 automation 时返回 None（fail-closed）。新布局日志见 _reqchat_launchd_log_dir。"""
    paths = resolve("PROJECT_PATHS").get("automation") or []
    if not paths:
        return None
    try:
        return (Path(paths[0]) / "data").resolve()
    except Exception:
        return None


def _default_log_dir():
    """平台缺省日志目录（与 scripts/log-rotate.sh 同一口径）：macOS ~/Library/Logs/req-board；
    其它系统 $XDG_STATE_HOME/loop-engine/logs，XDG_STATE_HOME 未设/非绝对路径时 ~/.local/state/loop-engine/logs。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "req-board"
    xdg = (os.environ.get("XDG_STATE_HOME") or "").strip()
    state = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".local" / "state"
    return state / "loop-engine" / "logs"


def _reqchat_launchd_log_dir():
    """日志目录（launchd 下即 plists/ 三个 plist 的 StandardOutPath 落点）：REQ_BOARD_LOG_DIR 或
    平台缺省（_default_log_dir）。目录不存在也照常返回（白名单判定不看文件是否存在）。"""
    raw = os.environ.get("REQ_BOARD_LOG_DIR") or ""
    base = Path(raw).expanduser() if raw.strip() else _default_log_dir()
    try:
        return base.resolve()
    except Exception:
        return None


def _reqchat_log_allowed(path_str):
    """read_logs 路径白名单校验（fail-closed）：resolve 防穿越后精确匹配或模式匹配。
    白名单（三处）：/tmp/req_board_dispatcher.log、/tmp/req_board_agent_<数字>.log、
    日志目录（_reqchat_launchd_log_dir）下 req-board*.log 及其轮转档、
    遗留 automation/data/ 下 req-board.log 与 req-board-dispatcher*.log。
    命中返回规范化绝对路径字符串，否则返回 None。"""
    p = Path(path_str or "")
    if not p.is_absolute():
        return None
    try:
        resolved = p.resolve()
    except Exception:
        return None
    if resolved == (Path("/tmp") / "req_board_dispatcher.log").resolve():
        return str(resolved)
    if resolved.parent == Path("/tmp").resolve() and re.fullmatch(r"req_board_agent_\d+\.log", resolved.name):
        return str(resolved)
    log_dir = _reqchat_launchd_log_dir()
    if log_dir is not None and resolved.parent == log_dir and REQ_CHAT_LOG_NAME_RE.match(resolved.name):
        return str(resolved)
    base = _reqchat_automation_data()
    if base is not None and resolved.is_relative_to(base):
        if resolved.name == "req-board.log":
            return str(resolved)
        if re.fullmatch(r"req-board-dispatcher.*\.log", resolved.name):
            return str(resolved)
    return None


def _tail_file_lines(path, n):
    """读文件最后 n 行（大文件安全：从末尾分块往回读），解码错误替换。"""
    lines = []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        block = b""
        while pos > 0 and len(block.splitlines()) <= n:
            chunk = min(8192, pos)
            pos -= chunk
            f.seek(pos)
            block = f.read(chunk) + block
        parts = block.splitlines()
        lines = parts[-n:] if len(parts) > n else parts
    return [ln.decode("utf-8", errors="replace") for ln in lines]


def _dispatcher_pid_heartbeat():
    """#8：从心跳文件定位调度器 pid（run_local 直跑模式无 launchd label 可查，调度器启动/exec
    时写入 pid）。三重校验 fail-closed，任一不满足返回 None：① 心跳 pid 为正整数；
    ② os.kill(pid, 0) 存活；③ ps 全命令行末个空白分隔 token 的 basename == dispatcher.py。
    末 token 口径（与 scripts/dispatcher-restart.sh 同规则，bash/python 无法共享代码、注释互引）
    覆盖两种启动形态——`<python> dispatcher.py`（run_local 首启）与 `<python> <含空格绝对路径>/
    dispatcher.py`（exec 自重启后；旧 argv[1] 口径遇含空格路径被截断误拒）；前提：启动命令行
    无尾随参数（run_local/exec/launchd 三形态现均满足）。陈旧/复用 pid（非 dispatcher.py 进程）
    会被拦下，不会当作调度器树根使预检失真（复用者恰为另一 dispatcher.py 进程属启发式边界）。
    -ww（BSD: 不限输出宽）防非终端下 ps 截断长命令行。"""
    try:
        pid = json.loads(resolve("HEARTBEAT_FILE").read_text()).get("pid")
    except Exception:
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except Exception:
        return None
    try:
        r = resolve("subprocess").run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                           capture_output=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    tokens = r.stdout.decode(errors="replace").strip().split()
    if not tokens or os.path.basename(tokens[-1]) != "dispatcher.py":
        return None
    return pid


def _heartbeat_boot_at():
    """#8：读心跳 boot_at（run_local 直跑模式启动/exec 重启时写入）；缺失/非法返回 None。
    execv 不换 PID，boot_at 变化才是「重启已发生」的判定依据（last_poll_at 会被旧进程照常刷新）。"""
    try:
        v = json.loads(resolve("HEARTBEAT_FILE").read_text()).get("boot_at")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    except Exception:
        return None


def _dispatcher_pid():
    """launchctl print 解析调度器主 PID（按 label，不猜 PID）；服务不在运行返回 None。
    #8：run_local 直跑模式无 launchd 服务 → 回退心跳 pid 三重校验（_dispatcher_pid_heartbeat），
    使 reqchat 预检 _dispatcher_kimi_agents 按真实调度器子树判定；launchd 路径逐字不变。"""
    if is_run_local_supervisor():
        return resolve("_dispatcher_pid_heartbeat")()
    try:
        r = resolve("subprocess").run(["launchctl", "print", f'gui/{os.getuid()}/{resolve("REQ_CHAT_DISPATCHER_LABEL")}'],
                           capture_output=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"^\s*pid = (\d+)$", r.stdout.decode(errors="replace"), re.M)
    return int(m.group(1)) if m else None


def _dispatcher_kimi_agents():
    """返回调度器进程树中运行中 kimi agent 的命令行列表（空 = 可安全重启）。
    判定：ps 全量取 pid/ppid/command，从调度器 PID 递归收集后代，命令行含 'kimi' 即命中
    （调度器 agent 为 Popen 的 `kimi acp` 子进程，#284 保持进程同样挂在调度器树下）。
    对话自身进程（kimi-chat-acp wrapper，父进程=board 而非 dispatcher）不在该子树，天然排除。"""
    pid = _dispatcher_pid()
    if pid is None:
        return []
    try:
        r = resolve("subprocess").run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, timeout=10)
    except Exception as e:
        raise RuntimeError(f"ps 读取进程列表失败: {e}")
    if r.returncode != 0:
        raise RuntimeError("ps 读取进程列表失败")
    children, cmds = {}, {}
    for line in r.stdout.decode(errors="replace").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            p, pp = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(pp, []).append(p)
        cmds[p] = parts[2]
    hit, stack, seen = [], [pid], set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        cmd = cmds.get(p) or ""
        if "kimi" in cmd.lower():
            hit.append(cmd)
        stack.extend(children.get(p, []))
    return hit


def _reqchat_wait_url_up(url, timeout=10, interval=0.5):
    """轮询 url 直到返回 200（服务重启窗口内连接会被拒），返回是否就绪（#300 同款语义）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _reqchat_heartbeat_ts():
    """读心跳文件 last_poll_at；文件缺失/解析失败返回 None。"""
    try:
        return json.loads(resolve("HEARTBEAT_FILE").read_text())["last_poll_at"]
    except Exception:
        return None


def _reqchat_dispatcher_health(timeout=None, expect_boot_at=None):
    """restart_dispatcher 后核对调度器恢复：心跳文件更新 + /api/dispatcher-health ok。
    返回 {"heartbeat_updated": bool, "health": {ok, age}, "detail": str}。
    #8：expect_boot_at 非 None（run_local exec 自重启）时改按心跳 boot_at 变化判定——execv
    同 PID，旧进程存活期间 last_poll_at 照常刷新，不能作为「重启已发生」的证据；不带参调用
    行为不变（launchd kickstart 场景沿用 last_poll_at 更新判定）。"""
    timeout = timeout or resolve("REQ_CHAT_HEALTH_WAIT")
    old = resolve("_reqchat_heartbeat_ts")()
    heartbeat_updated = False
    deadline = time.time() + timeout
    while time.time() < deadline and not heartbeat_updated:
        if expect_boot_at is not None:
            boot = _heartbeat_boot_at()
            if boot is not None and boot != expect_boot_at:
                heartbeat_updated = True
        else:
            ts = resolve("_reqchat_heartbeat_ts")()
            if ts and ts != old:
                heartbeat_updated = True
        if not heartbeat_updated:
            time.sleep(0.5)
    health, detail = {"ok": False, "age": None}, []
    try:
        port = os.environ.get("REQ_BOARD_PORT", "8899")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dispatcher-health", timeout=2) as resp:
            health = json.loads(resp.read().decode())
    except Exception as e:
        detail.append(f"/api/dispatcher-health 查询失败: {e}")
    parts = [("心跳文件已更新" if heartbeat_updated else "心跳文件未更新")]
    if detail:
        parts.append(detail[0])
    elif health.get("ok"):
        parts.append(f"/api/dispatcher-health ok（age {health.get('age')}s）")
    else:
        parts.append(f"/api/dispatcher-health 异常（ok={health.get('ok')}, age={health.get('age')}）")
    return {"heartbeat_updated": heartbeat_updated, "health": health, "detail": "；".join(parts)}


def _restart_dispatcher(source, note=None):
    """#421/#324：重启调度器并核对健康。launchd 模式 = launchctl kickstart -k（按 label，不猜
    PID）；#8 run_local 直跑模式分流 _restart_dispatcher_local（写标记 + SIGTERM，调度器进程内
    exec 自重启）。返回 {restarted, label, health, heartbeat_updated, detail}；失败抛 ValueError。
    source 为重启来源标识（reqchat / web，写 dispatcher_restart_source.json，须匹配 [a-zA-Z0-9_-]+）；
    note 为可选留痕回调 callable(msg)（reqchat 写 req_chat_events；网页端由端点写 agent_events）。"""
    if is_run_local_supervisor():
        return _restart_dispatcher_local(source, note)

    def _note(msg):
        if note:
            try:
                note(msg)
            except Exception:
                pass
    # #373：先写重启来源标记再 kickstart，新进程启动日志/健康角标可显示来源
    try:
        resolve("RESTART_SOURCE_FILE").write_text(json.dumps({"source": source, "at": round(time.time(), 3)}))
    except Exception as e:
        _note(f"写重启来源标记失败（不影响重启）: {e}")
    try:
        r = resolve("subprocess").run(["launchctl", "kickstart", "-k",
                            f'gui/{os.getuid()}/{resolve("REQ_CHAT_DISPATCHER_LABEL")}'],
                           capture_output=True, timeout=15)
    except Exception as e:
        _note(f"重启调度器失败: {e}")
        raise ValueError(f"重启调度器失败: {e}")
    if r.returncode != 0:
        reason = (r.stderr or r.stdout or b"").decode(errors="replace").strip()[:200] \
            or f"launchctl kickstart 退出码 {r.returncode}"
        _note(f"重启调度器失败: {reason}")
        raise ValueError(f"重启调度器失败: {reason}")
    health = resolve("_reqchat_dispatcher_health")()
    _note(f'已重启调度器（{resolve("REQ_CHAT_DISPATCHER_LABEL")}）：{health["detail"]}')
    return {"restarted": True, "label": resolve("REQ_CHAT_DISPATCHER_LABEL"),
            "health": health["health"], "heartbeat_updated": health["heartbeat_updated"],
            "detail": health["detail"]}


def _restart_dispatcher_local(source, note=None):
    """#8 run_local 直跑模式重启：定位调度器 pid（心跳三重校验）→ 读旧 boot_at → 写来源标记
    （写失败 → ValueError 中止、不发信号；不移植 launchd「写失败不影响重启」容错——直跑下无标记
    的 SIGTERM 会被调度器按停止语义处理）→ SIGTERM（异常清标记 + ValueError）→ 轮询心跳 boot_at
    变化（execv 同 PID，旧进程存活期间心跳照常更新，仅看心跳会误报成功）。超时/校验失败抛
    ValueError（web 端 502 / reqchat 工具错误），返回结构与 launchd 分支一致（label=run_local）。"""
    def _note(msg):
        if note:
            try:
                note(msg)
            except Exception:
                pass
    pid = resolve("_dispatcher_pid_heartbeat")()
    if pid is None:
        raise ValueError("run_local 直跑模式：未能从心跳定位调度器 pid（心跳缺 pid / 进程不存在 / "
                         "命令行校验不通过），未发信号；请手动重启：Ctrl-C 后重跑 run_local.sh")
    old_boot = _heartbeat_boot_at()
    try:
        resolve("RESTART_SOURCE_FILE").write_text(json.dumps({"source": source, "at": round(time.time(), 3)}))
    except Exception as e:
        raise ValueError(f"写重启来源标记失败，中止重启（未发信号）: {e}")
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as e:
        try:
            resolve("RESTART_SOURCE_FILE").unlink()
        except Exception:
            pass
        raise ValueError(f"发送 SIGTERM 失败（已清除来源标记）: {e}")
    _note(f"已向调度器 pid {pid} 发送 SIGTERM（run_local exec 自重启，等待心跳 boot_at 变化）")
    health = resolve("_reqchat_dispatcher_health")(expect_boot_at=old_boot)
    if not health["heartbeat_updated"]:
        raise ValueError(f'run_local 直跑模式：调度器未在 {resolve("REQ_CHAT_HEALTH_WAIT")}s 内完成 exec 自重启'
                         f"（心跳 boot_at 未变化）；请手动重启：Ctrl-C 后重跑 run_local.sh")
    _note(f"已重启调度器（run_local exec 自重启，pid {pid} 不变）：{health['detail']}")
    return {"restarted": True, "label": "run_local",
            "health": health["health"], "heartbeat_updated": health["heartbeat_updated"],
            "detail": health["detail"]}


def _log_global_agent_event(content):
    """#23 全局（无需求上下文）调度器动作留痕：写 agent_events 的 req_id=0 哨兵行。
    `req_id=0` 是「全局调度器动作」保留值，不对应任何需求自增 id，故不会出现在任何需求的
    时间线里（需求事件查询一律按真实 req_id 过滤）。写失败只告警——留痕不是主路径。
    内容只写状态/来源/数量/原因，**不写 secret**。"""
    try:
        with resolve("get_db")() as conn:
            conn.execute("INSERT INTO agent_events (req_id, kind, content, session_id, ts)"
                         " VALUES (0, 'system', ?, NULL, ?)",
                         (str(content)[:2000], resolve("now_str")()))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        resolve("app").logger.warning(f"写全局调度器事件失败: {e}")
