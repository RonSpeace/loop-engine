"""调度器生命周期：重启来源记录与自重启、done 流程合并后自动重启、退出信号记录与上次退出原因上报。"""

import json
import os
import signal
import sys
import time

from acp_shared import is_run_local_supervisor
from storage_time import format_storage_time

from dispatcher_modules.runtime import D, log, subprocess


# ---- #373 调度器重启原因可见化：SIGTERM/SIGINT 信号记录 + 约定式重启来源 ----
def _signal_name(signum):
    try:
        return signal.Signals(signum).name
    except Exception:
        return f"信号{signum}"

def _read_restart_source():
    """读重启来源标记文件**不删除**（{source, at} dict；缺失/解析失败/非 dict 返回 None）。
    #8：_shutdown_handler 需在 _record_shutdown 消费（弹出）标记前预读，据其判 exec 自重启。"""
    try:
        if not D.RESTART_SOURCE_FILE.exists():
            return None
        data = json.loads(D.RESTART_SOURCE_FILE.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _pop_restart_source():
    """读并删除重启来源标记文件（scripts/dispatcher-restart.sh / board restart_dispatcher 等约定入口写入）。
    返回 source 字符串；无标记/读失败返回 None。读完即删防残留误标（#373）。"""
    data = _read_restart_source()
    try:
        src = (data or {}).get("source")
        return src if isinstance(src, str) and src else None
    except Exception:
        return None
    finally:
        try:
            D.RESTART_SOURCE_FILE.unlink()
        except Exception:
            pass

def _restart_source_fresh(entry, ttl=D.RESTART_SOURCE_TTL_SEC):
    """来源标记是否新鲜可信（#8 exec 自重启第三道闸）：at 为数字且距今 ≤ TTL。
    缺失/非法/过期一律不新鲜（fail-safe：不 exec，按普通停止语义退出）。"""
    at = (entry or {}).get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    return (time.time() - at) <= ttl

def _exec_self_restart(source):
    """#8 run_local 直跑模式 exec 自重启：同进程槽位重载自身（PID 不变，run_local.sh 判活循环
    不触发「任一子进程退出即整组停止」；env 继承含 .env，cwd 不动；脚本用绝对路径防 cwd 差异）。
    先 flush 标准流再 exec（防日志丢失）。成功不返回；异常/意外返回 → log.error + 心跳
    restart_exec_failed 留痕，返回 False（调用方拒绝重启、绝不裸退出）。"""
    try:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        log.info(f"run_local 直跑模式：exec 自重启（来源 {source}，PID {os.getpid()} 不变）")
        os.execv(sys.executable, [sys.executable, str(D.REPO_DIR / "dispatcher.py")])
        reason = "os.execv 意外返回（未替换进程映像）"
    except Exception as e:  # noqa: BLE001 - handler 内绝不外抛，失败走拒绝重启路径
        reason = str(e)
    log.error(f"exec 自重启失败，拒绝重启（绝不裸退出）: {reason}")
    try:
        D.write_heartbeat({"restart_exec_failed": {"at": time.time(), "reason": reason[:300], "source": source}})
    except Exception as e:
        log.warning(f"写 restart_exec_failed 心跳标记失败: {e}")
    return False

# ==================== #380 done 流程自动重启调度器 ====================
def _queue_auto_restart(req_id):
    """#380：done 命中 dispatcher.py 时登记待自动重启（复用 state 持久化）。
    键：req_id(str) → {"at": 写入时间, "attempts": kill 尝试计数}；主循环见
    _maybe_auto_restart，重启成功由新进程 _verify_pending_restart 自证清标记。"""
    with D.state_lock:
        D.state.setdefault("pending_restart", {})[str(req_id)] = {"at": time.time(), "attempts": 0}
    D.save_state()

def _stray_acp_pids():
    """自动重启安全阀兜底（#380 方案 B）：pgrep -f "kimi acp" 命中外部存疑 kimi 进程 → 有则本轮不重启。
    排除：① board 起的助手进程（#107 建卡 / #302 需求助手：kimi-chat-acp 包装器及其 kimi 子进程，
    祖先链含 board.py 进程）；② 保持池 pid（held_procs：重启后由 cleanup_orphan_held 收编，
    与手动重启行为一致）。
    注意：pgrep 为启发式兜底（kimi 进程自改名 kimi-code 后 argv 无 "kimi acp" 字样），
    主安全阀是 active_runs；pgrep 不可用/失败时返回空（不拦截，防误判锁死）。"""
    try:
        out = subprocess.run(["pgrep", "-f", "kimi acp"], capture_output=True, text=True, timeout=5).stdout
        cand = {int(p) for p in out.split() if p.strip().isdigit()}
    except Exception:
        return []
    if not cand:
        return []
    try:
        board_pids = {int(p) for p in subprocess.run(
            ["pgrep", "-f", "board.py"], capture_output=True, text=True, timeout=5).stdout.split()}
    except Exception:
        board_pids = set()
    held = [int(v) for v in (D.state.get("held_procs") or {}).values() if str(v).isdigit()]
    try:
        out = subprocess.run(["ps", "ax", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
        parent = {}
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[0].isdigit():
                parent[int(parts[0])] = int(parts[1])
    except Exception:
        parent = {}

    def is_board_side(pid):
        """pid 的祖先链（限 15 层）命中 board.py 进程 → board 助手进程，排除"""
        seen, pp = set(), pid
        for _ in range(15):
            pp = parent.get(pp)
            if pp is None or pp in seen or pp == 1:
                return False
            seen.add(pp)
            if pp in board_pids:
                return True
        return False

    return sorted(p for p in cand if p not in held and not is_board_side(p))

def _fallback_manual_hint(req_id):
    """#380：自动重启多次未生效 → 转「需手动重启调度器」提醒兜底（summary + 时间线，不静默）。
    #8：run_local 直跑模式文案改「Ctrl-C 后重跑 run_local.sh」（直跑无 KeepAlive）。"""
    hint = D.DISPATCHER_RESTART_HINT_RUN_LOCAL if is_run_local_supervisor() else D.DISPATCHER_RESTART_HINT
    try:
        D.append_summary(req_id, f"⚠️ {hint}")
    except Exception as e:
        log.warning(f"需求 #{req_id} 自动重启兜底提醒写 summary 失败: {e}")
    try:
        D.post_dispatcher_event(req_id, f"调度器自动重启未能生效（已尝试 {D.MAX_AUTO_RESTART_ATTEMPTS - 1} 次 kill），"
                                      f"需手动重启：{hint}", kind="merge")
    except Exception as e:
        log.warning(f"需求 #{req_id} 自动重启兜底提醒事件上报失败: {e}")

def _maybe_auto_restart():
    """主循环每轮检查（#380）：pending_restart 非空且已写入超过 POLL_INTERVAL（确保 done 通知/
    合并/看板重启先完成），且无并行 agent（active_runs 空）、无外部存疑 kimi 进程 → 写来源标记
    （source=auto，复用 #373）→ kill -TERM 自身。launchd 模式由 KeepAlive 拉起、新进程自证；
    #8 run_local 直跑模式由信号 handler 内 exec 自重启（整组不停止）。
    每次发起 kill attempts+1；单需求 attempts 超 MAX_AUTO_RESTART_ATTEMPTS（第 N 轮判定放弃，
    即最多实际发起 N-1 次 kill）→ 放弃该需求并转「需手动重启调度器」提醒兜底（不静默）；
    有并行/存疑进程仅顺延不计数（等 agent 结束自动生效）。
    #8 直跑加固：标记写失败跳过本轮 kill（无标记的 SIGTERM 会被 handler 按停止语义退出、整组
    停止；该分支在 attempts 自增之前 return，本轮不计入尝试次数）；kill 异常清标记（防残留
    标记在 TTL 内被随后 Ctrl-C 的 kill 0 SIGTERM 命中而误触发 exec）。"""
    with D.state_lock:
        pending = dict(D.state.get("pending_restart") or {})
    if not pending:
        return
    if time.time() - min(v.get("at", 0) for v in pending.values()) < D.POLL_INTERVAL:
        return
    # 安全阀①：并行 agent 不打断（重启顺延，全部结束后下一轮自动生效）
    with D.active_lock:
        if D.active_runs:
            log.info(f"存在 {len(D.active_runs)} 个并行 agent，调度器自动重启顺延至其结束后")
            return
    # 安全阀②：外部存疑 kimi 进程兜底（排除 board 助手进程/保持池）
    stray = D._stray_acp_pids()
    if stray:
        log.warning(f"发现外部 kimi acp 进程 {stray}，本轮跳过调度器自动重启")
        return
    run_local = is_run_local_supervisor()
    # 发起重启：先写来源标记（#373 机制，新进程/exec 后自证来源=auto）再 kill -TERM 自身
    try:
        D.RESTART_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        D.RESTART_SOURCE_FILE.write_text(json.dumps({"source": "auto", "at": round(time.time(), 3)}))
    except Exception as e:
        log.warning(f"写自动重启来源标记失败（重启后来源将显示未知）: {e}")
        if run_local:
            log.error("run_local 直跑模式：来源标记写失败，跳过本轮自动重启（不发 SIGTERM；下一轮重试）")
            return
    with D.state_lock:
        for rid in pending:
            rec = D.state["pending_restart"].get(rid)
            if rec is None:
                continue
            rec["attempts"] = rec.get("attempts", 0) + 1
        give_up = [rid for rid, rec in D.state["pending_restart"].items()
                   if rid in pending and rec.get("attempts", 0) >= D.MAX_AUTO_RESTART_ATTEMPTS]
        for rid in give_up:
            del D.state["pending_restart"][rid]
    D.save_state()
    for rid in give_up:
        _fallback_manual_hint(rid)
    with D.state_lock:
        still = D.state.get("pending_restart") or {}
    if not still:
        return
    if run_local:
        # #8：重启前一条事件（PRD §2「重启前后各记一条」；仅直跑分支，launchd 零新增事件）
        for rid in sorted(still):
            try:
                D.post_dispatcher_event(int(rid),
                                      "直跑模式：调度器将以 exec 自重启生效（不中断看板/整组存活）",
                                      kind="merge")
            except Exception as e:
                log.warning(f"需求 #{rid} 直跑自重启预告事件上报失败: {e}")
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        if run_local:
            log.info("自动重启：已发送 SIGTERM 到自身（来源 auto），run_local 直跑模式将由 exec 自重启")
        else:
            log.info("自动重启：已发送 SIGTERM 到自身（来源 auto），KeepAlive 将拉起新进程")
    except Exception as e:
        log.error(f"自动重启 kill 失败: {e}")
        if run_local:
            # #8：清标记（防残留标记在 TTL 内被随后 Ctrl-C 的 kill 0 SIGTERM 命中而误触发 exec）
            try:
                D.RESTART_SOURCE_FILE.unlink()
            except Exception:
                pass
            log.error("run_local 直跑模式：kill 失败，已清除来源标记（防劫持 Ctrl-C 停止语义）")

def _verify_pending_restart():
    """新进程启动自证（#380，main 在 load_state 后调用）：pending_restart 非空说明 done 流程
    登记了自动重启且本进程即重启结果 → 清标记（新进程已就位，配置已生效；保留标记会让主循环
    再次 kill 自身造成重启循环）+ 逐需求补发通知：
    - 心跳 last_shutdown.at >= 最早 pending 写入时间 → 上次确已优雅退出：来源 auto → 「调度器
      已自动重启，dispatcher.py 改动已生效」（验收原文）；其他来源（人工 script 等）注明来源；
    - 心跳无更新的 last_shutdown（崩溃/强杀/未及记录）→ 注明异常退出后由 KeepAlive 拉起，
      同样清标记并通知（fail-safe：配置已生效，不静默）。"""
    with D.state_lock:
        pending = dict(D.state.get("pending_restart") or {})
    if not pending:
        return
    hb = D.load_heartbeat()
    last = (hb or {}).get("last_shutdown")
    oldest = min(v.get("at", 0) for v in pending.values())
    confirmed = bool(isinstance(last, dict) and isinstance(last.get("at"), (int, float))
                     and last["at"] >= oldest)
    with D.state_lock:
        D.state["pending_restart"] = {}
    D.save_state()
    if confirmed and last.get("source") == "auto":
        try:
            # #380：自证成功 → 心跳标记透出到看板健康角标「自动重启成功」（board.py 读该字段渲染）
            D.write_heartbeat({"auto_restart_ok": True})
        except Exception as e:
            log.warning(f"写自动重启成功心跳标记失败: {e}")
    for rid in sorted(pending):
        if confirmed:
            note = (D.DISPATCHER_AUTO_RESTART_OK if last.get("source") == "auto"
                    else f"调度器已重启（来源: {last.get('source') or '未知'}），dispatcher.py 改动已生效")
        elif is_run_local_supervisor():
            # #8：直跑模式无 KeepAlive 承担拉起，去该措辞（exec 自重启正常路径在 handler 内先写
            # last_shutdown，走不到本分支；此分支兜底异常场景）
            note = "调度器已重启（上次退出未记录优雅关闭，直跑模式重载），dispatcher.py 改动已生效"
        else:
            note = "调度器已重启（上次退出未记录优雅关闭，KeepAlive 拉起），dispatcher.py 改动已生效"
        try:
            D.append_summary(rid, f"✅ {note}")
        except Exception as e:
            log.warning(f"需求 #{rid} 自动重启成功通知写 summary 失败: {e}")
        try:
            D.post_dispatcher_event(rid, note, kind="merge")
        except Exception as e:
            log.warning(f"需求 #{rid} 自动重启成功事件上报失败: {e}")
    log.info(f"自动重启自证：pending_restart 已清空（确认上次退出={confirmed}，"
             f"来源={last.get('source') if confirmed else '无'}），新代码生效")

def _record_shutdown(signum):
    """进程收到 SIGTERM/SIGINT 时把「上次退出」写进心跳（可单测；不含 sys.exit）：
    last_shutdown = {at, signal, source?}，来源标记文件存在则合并（约定入口写入）。
    直接合并写心跳（不更新 last_poll_at：退出时没有新的轮询，保留上次轮询时间语义）。"""
    entry = {"at": time.time(), "signal": _signal_name(signum)}
    src = _pop_restart_source()
    if src:
        entry["source"] = src
    D.write_heartbeat({"last_shutdown": entry}, update_poll=False)

def _shutdown_handler(signum, frame):
    """信号 handler：记录 + 退出；#8：run_local 直跑模式下「新鲜重启请求（SIGTERM + 来源标记 +
    TTL 内）」改 exec 自重启（同 PID，整组不停止）。只做记录/判定的轻量操作，禁止网络/事务
    等耗时操作（分析 #373 风险点 2）。判定顺序：预读标记（不删）→ 记录退出（消费标记，先记录
    再 exec 使新进程既有自证链 _verify_pending_restart 判 confirmed）→ 满足条件 exec → 否则原退出。"""
    source_entry = _read_restart_source()
    source = (source_entry or {}).get("source")
    if not isinstance(source, str) or not source:
        source = None
    try:
        _record_shutdown(signum)
        log.info(f"收到信号 {_signal_name(signum)}，已记录上次退出")
    except Exception as e:
        # 记录异常不阻断 exec 判定（防「直跑+新鲜标记」被记录异常退化成整组停止）
        log.error(f"记录上次退出失败: {e}")
    if (signum == signal.SIGTERM and source and is_run_local_supervisor()
            and _restart_source_fresh(source_entry)):
        # exec 成功不返回；失败/意外返回 → 拒绝重启、绝不裸退出（保持进程存活；
        # auto 路径由下一轮重试/兜底文案承接，web/reqchat 由健康核对超时报错）
        D._exec_self_restart(source)
        return
    log.info(f"收到信号 {_signal_name(signum)}，进程退出")
    sys.exit(0)

def register_shutdown_handlers():
    """#373：注册 SIGTERM/SIGINT handler——此前 SIGTERM 直接死亡零记录，
    重启来源事后只能靠 launchd 日志+log show 人工排查。仅主线程（main）调用。"""
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

def report_previous_exit():
    """#373：启动时上报上次退出原因（写启动日志，看板健康角标经心跳透出）：
    - 心跳有 last_shutdown → 上次 SIGTERM/SIGINT 优雅退出（含来源：script/reqchat 等或未知）
    - 心跳存在但无 last_shutdown → 异常终止（崩溃/被强杀/kill -9）
    - 无心跳记录 → 首次部署/旧版本，不误报"""
    try:
        hb = D.load_heartbeat()
        last = hb.get("last_shutdown") if hb else None
        src = _pop_restart_source()
        if isinstance(last, dict) and last.get("at"):
            if src and "source" not in last:
                # 兜底：退出时未读到来源（竞态残留），启动时补进心跳
                last = dict(last, source=src)
                D.write_heartbeat({"last_shutdown": last})
            at = format_storage_time(last["at"])
            src_txt = f"（来源: {last['source']}）" if last.get("source") else "（来源未知）"
            log.info(f"调度器启动：上次退出 {last.get('signal', '信号')} @ {at}{src_txt}")
        elif src:
            log.warning(f"调度器启动：发现重启来源标记（{src}），但无上次退出记录（进程异常终止未及记录）")
        elif hb:
            log.warning("调度器启动：上次退出无正常关闭标记（异常终止：崩溃/被强杀）")
        # 无心跳记录（首次部署）→ 静默，不误报
    except Exception as e:
        log.warning(f"调度器启动：读取上次退出记录失败: {e}")

