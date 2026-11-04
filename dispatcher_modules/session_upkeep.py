"""会话巡检：孤儿会话对账、看板手动指令消费、驻留会话回收。"""

import os
import signal
import time

from dispatcher_modules.runtime import D, log, subprocess, threading


def _session_held(row, sessions):
    """#374/#388 持有者判定：state.sessions 中「role:req_id」键（含旧格式「req_id」键）
    存在且 session_id_of(entry) 与行 session_id 匹配 → 有持有者（resume 路径）。"""
    sid = row.get("session_id")
    req_id = row.get("req_id")
    role = row.get("role") or ""
    for key in (D.session_key({"id": req_id}, role), str(req_id)):
        entry = sessions.get(key)
        if entry is not None and D.session_id_of(entry) == sid:
            return True
    return False


def _proc_dead_for(req_id):
    """#388 进程确死判定：active_runs 无该需求记录（重启后内存态丢失）或记录中
    proc.poll() 非 None → 确死；正常运行中的会话（poll() is None）绝不误判。"""
    with D.active_lock:
        rec = D.active_runs.get(str(req_id))
    if rec is None:
        return True
    poll = getattr(rec.get("proc"), "poll", None)
    return not (callable(poll) and poll() is None)


def reconcile_orphan_sessions():
    """#374 启动时清算孤儿 running 会话：调度器重启被杀进程的 agent_sessions 记录永久停在
    running（僵尸），中间栏永远显示运行中。清理范围（#388 扩展）：
    - 无持有者：键不存在、或键存在但 session_id 不匹配（被覆盖键，如 review:370 换新
      会话后旧行无人认领）→ 标 interrupted（中断，人工可继续）+ phase 事件留痕；
    - 有持有者但进程确死（#388）：驱动线程已随旧进程消失，行属僵尸 → 同样标
      interrupted（与运行期 sweep 兜底二分：启动序列一次清干净；运行期 sweep 只收
      active_runs 有记录且 poll 非 None 的死进程，本函数不重复运行）；
    - 有持有者且进程存活（active_runs 有记录且 poll() is None，正常运行/重试/resume）
      → 不动（绝不误伤）；
    - session_id 为空的异常 running 行无法比对持有者，跳过不误清（低概率数据异常）；
    - 看板不可达整体跳过（不崩不误写，仿 reconcile_startup）；单行失败降级继续。
    仅 UPDATE status 字段 + 追加事件，不删行、不动 session_id（可 resume 续上下文）。"""
    try:
        rows = D.api_board("GET", "/agent-sessions")
    except Exception as e:
        log.warning(f"拉取 agent_sessions 失败（孤儿会话清算跳过）: {e}")
        return
    sessions = D.state.get("sessions") or {}
    for r in rows:
        if r.get("status") != "running":
            continue
        sid = r.get("session_id")
        if not sid:
            continue
        req_id = r.get("req_id")
        role = r.get("role") or ""
        if _session_held(r, sessions):
            if not _proc_dead_for(req_id):
                continue  # 合法持有者且进程存活（正常运行/重试/resume），不动
            log.warning(f"孤儿会话清算：req #{req_id} {role} session {sid} 有持有者但进程确死，标记 interrupted（#388）")
        try:
            # #421：统一入口（置 interrupted + 清 turn_step）
            D._close_session_row({"id": req_id}, role, r.get("id"), "interrupted")
            log.warning(f"孤儿会话清算：req #{req_id} {role} session {sid} 标记 interrupted（调度器重启）")
            D.post_dispatcher_event(int(req_id), f"调度器重启，会话 {sid} 中断清理", kind="phase")
        except Exception as e:
            log.warning(f"孤儿会话清算失败 req #{req_id} (row={r.get('id')}): {e}")


def manual_commands_poller():
    """#284 人工续跑命令消费线程：独立短间隔轮询，降低「发消息→被消费」的排队延迟。
    与主循环解耦（主循环仍负责捞需求/决策对账/心跳，15s 间隔不变）；消费逻辑本身
    有 active_lock 防并发拉起同一需求的会话，双线程场景安全。"""
    log.info(f"人工命令消费线程启动（间隔 {D.MANUAL_POLL_INTERVAL}s）")
    while True:
        try:
            consume_manual_commands()
        except Exception as e:
            log.error(f"人工命令消费异常: {e}")
        time.sleep(D.MANUAL_POLL_INTERVAL)


def _reap_held_once():
    """回收一轮：返回超时回收的保持进程数（供 held_reaper 循环与单元测试调用）。
    #412（A1）：held_lock 内仅快照+pop，_close_held 移锁外——旧实现锁内 _close_held
    （session.close → MCP 关闭无超时）一旦阻塞即持 held_lock 永久卡住，
    _hold_session 全部阻塞（#410 高嫌疑点 A）。pop 后 key 已出池，无并行 close。"""
    now = time.time()
    stale = []
    with D.held_lock:
        for k, h in D.held_sessions.items():
            if now - h["kept_at"] > D.HELD_TTL_SEC:
                stale.append((k, h))
        for k, h in stale:
            D.held_sessions.pop(k, None)
            D.state.get("held_procs", {}).pop(k, None)
            log.info(f"保持进程闲置超时回收 (asid={k}, req={h['req_id']}, pid={h['session'].proc.pid})")
    for k, h in stale:
        try:
            D._close_held(h)
        except Exception as e:
            # #412（T7）：单项关闭异常不中断其余回收（close 兜底后仍异常时留痕）
            log.warning(f"保持进程 {k} 关闭异常（继续回收其余）: {e}")
    if stale:
        D.save_state()
    return len(stale)


def held_reaper():
    """#284 保持进程回收：闲置超时（HELD_TTL_SEC）后优雅关闭，防止进程长期占用资源。
    仅回收 idle 保持进程；复用中/运行中的进程在 active_runs（不在保持池），不受影响。"""
    log.info(f"保持进程回收线程启动（闲置 TTL {D.HELD_TTL_SEC}s）")
    while True:
        try:
            _reap_held_once()
        except Exception as e:
            log.error(f"保持进程回收异常: {e}")
        time.sleep(30)


def cleanup_orphan_held():
    """调度器启动清理：#284 保持进程随上次调度器死亡可能残留为孤儿（子进程不随父退出）。
    pid 镜像在 state["held_procs"]；精确 pid + 命令行含 kimi 校验（防 pid 复用误杀）后 terminate。"""
    procs = dict(D.state.get("held_procs") or {})
    for asid, pid in procs.items():
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=5).stdout
            if "kimi" in out:
                os.kill(pid, signal.SIGTERM)
                log.info(f"清理孤儿保持进程 pid={pid} (asid={asid})")
        except Exception as e:
            log.warning(f"孤儿保持进程清理失败 pid={pid} (asid={asid}): {e}")
    with D.state_lock:
        D.state["held_procs"] = {}
    D.save_state()


def recover_manual_commands():
    """启动恢复：上次运行中（running）的人工续跑命令随调度器进程中断，标记 failed 供人工重发。"""
    try:
        rows = D.api_board("GET", "/manual-commands?status=running")
    except Exception as e:
        log.warning(f"拉取 running 人工命令失败（跳过）: {e}")
        return
    for r in rows:
        try:
            D.api_board("PATCH", f"/manual-commands/{r['id']}",
                      {"status": "failed", "error": "调度器重启，命令中断"})
        except Exception as e:
            log.warning(f"恢复人工命令 #{r['id']} 失败: {e}")


def consume_manual_commands():
    """#273 人工续跑：轮询消费 pending 命令（看板聊天框发消息 / 新建会话首条消息）。
    同一需求已有会话在跑（active_runs）时留待下轮；命令先 claim 为 running，再起线程执行。"""
    try:
        cmds = D.api_board("GET", "/manual-commands?status=pending")
    except Exception as e:
        log.warning(f"拉取人工续跑命令失败: {e}")
        return
    for cmd in cmds:
        req_id = str(cmd["req_id"])
        try:
            req = D.fetch_req(req_id)
            if req is None:
                D.api_board("PATCH", f"/manual-commands/{cmd['id']}",
                          {"status": "failed", "error": "需求不存在"})
                continue
            with D.active_lock:
                if req_id in D.active_runs:
                    continue  # 已有会话在跑，下轮再消费
            D.api_board("PATCH", f"/manual-commands/{cmd['id']}", {"status": "running"})
            role = cmd.get("role") or D.role_for(req)
            # #284 现场修复：route/review 是等待人工决策的中间态，续跑只执行会话、不置 doing——
            # 否则决策对账会把「状态变 doing」误判为网页端确认/评审通过（accept 阶段会误置 done）
            if req.get("status") not in ("route", "review"):
                fields = {"status": "doing"}
                if req.get("mode") == "semi" and role in D.SEMI_PHASES:
                    fields["phase"] = role
                D.set_req(req["id"], fields)
            # #279：传 set_req 之后的最新需求（phase 可能已按 role 更新），
            # 供 run_agent 的角色守卫按当前阶段校验
            fresh = D.fetch_req(req["id"]) or req
            log.info(f"需求 #{req_id} 人工续跑（role={role}, cmd={cmd['id']}）: {cmd.get('text')}")
            # 网页端没有「普通提问」的 y 按钮（WEB_CONFIRM_ACTIONS 只给 stop_later），聊天框是
            # 网页端唯一答复入口 → 该卡有待答的普通提问时，把这条消息同样留档到 summary
            # （口径与飞书 y 一致；机器生成的两类提问——前置流程确认 / 评审超限裁决——有自己的
            # 留档分支，跳过，避免把问题全文当澄清写进开发总结）
            pend = D.state["pending_confirm"].get(req_id) or {}
            if pend and not pend.get("flow_id") and pend.get("review_index") is None:
                D.append_summary(req["id"], D.confirm_answer_summary(pend.get("question"), cmd.get("text")))
            D.post_dispatcher_event(req["id"], f"人工继续会话：{cmd.get('text')}", kind="confirm")
            threading.Thread(target=D._run_manual_command,
                             args=(cmd, fresh, role, cmd.get("text") or ""),
                             daemon=True).start()
        except Exception as e:
            log.error(f"人工续跑命令 #{cmd.get('id')} 处理失败: {e}")
            try:
                D.api_board("PATCH", f"/manual-commands/{cmd['id']}",
                          {"status": "failed", "error": str(e)[:2000]})
            except Exception:
                pass


def _run_manual_command(cmd, req, role, text):
    """人工续跑命令执行线程：取会话行，resume 旧会话或新建会话，注入用户消息跑一轮。"""
    cmd_id = cmd["id"]
    asid = cmd.get("agent_session_id")
    try:
        rows = D.api_board("GET", f"/requirements/{req['id']}/sessions")
        row = next((r for r in rows if r["id"] == asid), None)
        if row is None:
            D.api_board("PATCH", f"/manual-commands/{cmd_id}",
                      {"status": "failed", "error": "会话不存在"})
            return
        resume_sid = row.get("session_id") or None
        prompt = D.build_prompt(req, role) if not resume_sid else "人工继续会话"
        # #284 进程保持复用：同会话行有存活保持进程 → 直接复用（省拉起+resume），
        # 进程已死（poll 非 None）则丢弃保持项走正常拉起
        # #412（A2）：held_lock 内仅旧快照+pop，_close_held 移锁外（与 _reap_held_once 同型）
        held = None
        dead_held = None
        with D.held_lock:
            h = D.held_sessions.get(asid)
            if h is not None:
                if h["session"].proc.poll() is None:
                    held = h
                    D.held_sessions.pop(asid, None)
                    D.state.get("held_procs", {}).pop(asid, None)
                else:
                    D.held_sessions.pop(asid, None)
                    D.state.get("held_procs", {}).pop(asid, None)
                    dead_held = h
        if dead_held is not None:
            D._close_held(dead_held)
        if held is not None:
            log.info(f"需求 #{req['id']} 人工续跑复用保持进程（cmd={cmd_id}, asid={asid}）")
        ok = D.run_agent(req, role, prompt, started_at=time.time(), extra="",
                       agent_session_id=asid, user_text=text, resume_session_id=resume_sid,
                       hold=True, held=held)
        if ok is False:
            # #279：角色守卫拦截（角色与需求当前阶段不匹配），命令失败不置 done；
            # 仅显式 False 判定（旧调用/旧 mock 无返回值视为成功路径，兼容）
            D.api_board("PATCH", f"/manual-commands/{cmd_id}",
                      {"status": "failed", "error": "角色与需求当前阶段不匹配，拒绝拉起会话"})
            return
        D.api_board("PATCH", f"/manual-commands/{cmd_id}", {"status": "done"})
        # #273 人工续跑成功（进入 review/done）：清除毒会话标记，自动路径与展示同步
        fresh = D.fetch_req(req["id"])
        if fresh and fresh.get("status") in ("review", "done"):
            try:
                D.api_board("POST", "/agent-sessions", {"id": asid, "poisoned": 0})
            except Exception:
                pass
            with D.state_lock:
                entry = D.state.get("sessions", {}).get(D.session_key(req, role))
                if isinstance(entry, dict):
                    entry["poisoned"] = False
            D.save_state()
    except Exception as e:
        log.warning(f"需求 #{req['id']} 人工续跑异常: {e}")
        try:
            D.api_board("PATCH", f"/manual-commands/{cmd_id}",
                      {"status": "failed", "error": str(e)[:2000]})
        except Exception:
            pass


