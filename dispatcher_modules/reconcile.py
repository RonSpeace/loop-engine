"""决策对账：待确认 / 评审决定与看板对账、确认动作执行、启动期对账与已完成结果收养。"""

import json
import time

from dispatcher_modules.runtime import D, log


def reconcile_ghost_pending():
    """#25 启动存量终态对账：卡片已 done/dropped 时，state 里同需求任意 kind 的
    pending_confirm 条目（含历史 review_gate/merge/普通 confirm/confirm_override）
    必为幽灵——人工置终态或推进路径完成绕过事件驱动消费，条目永久残留（#24 现场），
    清理防残留积累并消除 done 卡的「待确认」徽标。人工置 done/dropped 不依赖调度器
    收尾路径，故由启动对账兜底覆盖。
    逐卡现取裁决：卡不在看板/回读 None/回读异常/非终态一律跳过（#394 非终态全 kind 保留，
    由 _purge_ghost_confirm 的 fetch_req 复核最终裁决）；单条失败降级继续，看板不可达整体跳过。"""
    try:
        all_reqs = {r["id"]: r for r in D.api("GET", "")}
    except Exception as e:
        log.warning(f"幽灵待确认对账拉取需求失败（本轮跳过）: {e}")
        return
    with D.state_lock:
        snapshot = list(D.state["pending_confirm"].items())
    for rid, entry in snapshot:
        if not isinstance(entry, dict):
            continue
        try:
            # #27：按条目内容卡 id 判终态（键服从内容），键与内容不同源时不按键误清
            req = all_reqs.get(D._pending_content_req_id(entry, rid))
            if req is None or req.get("status") not in ("done", "dropped"):
                continue
            kind = entry.get("kind") or "confirm"
            if D._purge_ghost_confirm(rid, "启动对账幽灵清理"):
                log.info(f"幽灵待确认对账：需求 #{rid} 已终态，清理残留 {kind} 待确认")
        except Exception as e:
            log.warning(f"需求 #{rid} 启动幽灵对账异常（跳过，继续其它条目）: {e}")
            continue


def reconcile_pending_confirm_source():
    """#27 启动对账（必须**先于** reconcile_ghost_pending 运行：先按键归一条目、后按 key 清账）。

    - 键与条目内容卡 id 不同源 → 按键修正（目标键已有条目则保留原键 + 告警，不覆盖在途条目）；
    - 条目内容卡现取终态（done/dropped）→ 清掉条目（不限 kind，见 tech §3.7 有意覆盖裁定）。

    条目非 dict / req 缺失 / id 不可解析 → 跳过（fail-open，不清理不抛）；看板不可达时不清账
    （fail-safe）。confirm_rounds 一律不动（错源两键分属两张卡，搬运会虚高）。返回 (修正数, 清理数)。"""
    with D.state_lock:
        snapshot = list(D.state["pending_confirm"].items())
    fixed = cleared = 0
    for rid, entry in snapshot:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("req"), dict):
            log.warning(f"待确认条目 #{rid} 缺 req 快照，跳过同源对账（fail-open）")
            continue
        try:
            cid = int(entry["req"]["id"])
        except (KeyError, TypeError, ValueError):
            log.warning(f"待确认条目 #{rid} 的 req.id 不可解析，跳过同源对账（fail-open）")
            continue
        # ① 键修正（键服从内容 id）
        if str(cid) != str(rid):
            moved = occupied = False
            with D.state_lock:
                if D.state["pending_confirm"].get(rid) is entry:
                    if str(cid) in D.state["pending_confirm"]:
                        occupied = True
                    else:
                        D.state["pending_confirm"].pop(rid, None)
                        D.state["pending_confirm"][str(cid)] = entry
                        moved = True
            if moved:
                D.save_state()
                fixed += 1
                msg = (f"待确认条目同源对账：键 #{rid} 与内容（需求 #{cid}）不同源，"
                       f"已按键修正为 #{cid}")
                log.info(msg)
                D.post_dispatcher_event(cid, msg, kind="confirm")
            elif occupied:
                msg = (f"待确认条目同源对账：键 #{rid} 与内容（需求 #{cid}）不同源，"
                       f"但目标键 #{cid} 已有待确认条目，保留原键 #{rid} 未覆盖，请人工处理")
                log.error(msg)
                D.post_dispatcher_event(int(rid), msg, kind="confirm")
        # ② 内容卡现取终态 → 清账（不限 kind；看板不可达本轮不清）
        try:
            req = D.fetch_req(cid)
        except Exception as e:
            log.warning(f"待确认同源对账读取需求 #{cid} 失败（本轮不清账）: {e}")
            continue
        if req is None or req.get("status") not in ("done", "dropped"):
            continue
        popped = False
        with D.state_lock:
            for k in (str(cid), str(rid)):
                if D.state["pending_confirm"].get(k) is entry:
                    D.state["pending_confirm"].pop(k, None)
                    popped = True
        if popped:
            D.save_state()
            cleared += 1
            msg = (f"待确认同源对账：条目内容所属需求 #{cid} 已 {req.get('status')}，"
                   f"清理失效待确认条目（原键 #{rid}）。问题：{entry.get('question') or ''}")
            log.info(msg)
            D.post_dispatcher_event(cid, msg, kind="confirm")
    return fixed, cleared


def reconcile_decisions():
    """扫 pending_route / pending_review，按需求当前 board 状态解释看板按钮/改判的决策。
    - 仍为 route/review：不动
    - open + mode=auto/semi（route 按钮已写入）：丢 pending 并直接 spawn（active_runs 防重复）
    - doing（review 通过按钮）：以 pending 记录的 phase 推下一阶段；accept 特判置 done
    - open（review 否决按钮）：丢 pending 供重捞
    各分支判定后向飞书补发确认消息（网页按钮无即时回执，由对账轮询统一补发；accept 通过不加发，
    避免与 finalize_done 的「已完成」通知重复）。
    兼作改判后陈旧 pending 的清理。"""
    with D.state_lock:
        routes = dict(D.state["pending_route"])
        reviews = dict(D.state["pending_review"])
    all_reqs = {}
    try:
        all_reqs = {r["id"]: r for r in D.api("GET", "")}
    except Exception as e:
        log.warning(f"决策对账拉取需求失败: {e}")
        return
    for req_id_str, pend in routes.items():
        req = all_reqs.get(int(req_id_str))
        if req is None:
            log.info(f"需求 #{req_id_str} 已不存在，通道 pending 静默清除")
            with D.state_lock:
                D.state["pending_route"].pop(req_id_str, None)
            continue
        if req["status"] == "route":
            continue
        with D.state_lock:
            D.state["pending_route"].pop(req_id_str, None)
        if req["status"] == "open" and req.get("mode") in ("auto", "semi"):
            log.info(f"需求 #{req_id_str} 通道已确认（{req.get('mode')}），进入 {D.role_for(req)}")
            D.clear_retries(req_id_str)
            D.clear_flow_rounds(req_id_str)
            # 事件文案带流程名（#397）：semi 显示「半自动·精简/标准/加强」，auto 显示「自动」
            # —— 与飞书通知（FLOW_NAMES 拼接）口径一致，历史回放可辨具体选型
            channel_cn = f"半自动·{D.FLOW_NAMES[D.flow_of(req)]}" if req.get("mode") == "semi" else "自动"
            D.post_dispatcher_event(int(req_id_str),
                                  f"通道已确认（{channel_cn}），进入「{D.ROLE_NAMES.get(D.role_for(req), D.role_for(req))}」",
                                  kind="phase")
            if req.get("mode") == "semi":
                D.notify(f"需求 #{req_id_str} 已在网页端确认走半自动通道（{D.FLOW_NAMES[D.flow_of(req)]}流程）。")
            else:
                D.notify(f"需求 #{req_id_str} 已在网页端确认走自动通道。")
            D._spawn_safe(req, D.role_for(req))
        else:
            # 兜底留痕：用户改判为 later/dropped/done 等状态（未走确认/否决按钮），pending 已消费，
            # 写调度事件便于追溯（如空格误触通过后改回暂缓的场景），不发飞书避免打扰
            log.info(f"需求 #{req_id_str} 通道 pending 已清除：状态改为 {req['status']}（未走确认/否决）")
            D.post_dispatcher_event(int(req_id_str),
                                  f"通道 pending 已清除：需求状态改为「{req['status']}」，未走确认/否决",
                                  kind="phase")
    for req_id_str, pend in reviews.items():
        req = all_reqs.get(int(req_id_str))
        if req is None:
            log.info(f"需求 #{req_id_str} 已不存在，评审 pending 静默清除")
            with D.state_lock:
                D.state["pending_review"].pop(req_id_str, None)
            continue
        if req["status"] == "review":
            continue
        with D.state_lock:
            D.state["pending_review"].pop(req_id_str, None)
        if req["status"] == "doing":
            phase = pend.get("phase")
            if phase == "accept":
                D.append_summary(int(req_id_str), "人工验收通过，需求完成。")
                fresh = D.fetch_req(int(req_id_str)) or req
                D.finalize_done(fresh)
                D.post_dispatcher_event(int(req_id_str), "人工验收通过，需求完成", kind="phase")
            else:
                nxt = D.next_stage(req, phase)
                if nxt:
                    D.set_req(int(req_id_str), {"status": "doing", "phase": nxt})
                    D.clear_retries(req_id_str)
                    D.clear_flow_rounds(req_id_str)
                    fresh = D.fetch_req(int(req_id_str)) or req
                    log.info(f"需求 #{req_id_str} 评审通过（{phase}），进入 {nxt}")
                    D.post_dispatcher_event(int(req_id_str),
                                          f"「{D.ROLE_NAMES.get(phase, phase)}」评审通过，进入「{D.ROLE_NAMES.get(nxt, nxt)}」",
                                          kind="phase")
                    D.notify(f"需求 #{req_id_str} 已在网页端审核通过，进入 {D.ROLE_NAMES.get(nxt, nxt)} 阶段。")
                    D._spawn_safe(fresh, nxt)
                # 无下一角色（正常流程不会出现，如未知 phase）：不推进、不误发通知（对齐 on_message 提示）
        elif req["status"] == "open":
            # 网页端「否决」按钮：pending 已 pop，补发飞书确认（summary 已由 board 追加「[auto] 用户否决」）
            # #376：打回=评审角色 phase 重定向到前置 stage（tech_review→tech / code_review→dev）；需求级否决
            #（board PATCH phase:null）phase 已置空，role_for 回 flow 首 stage；两种出口均清 retries/rounds
            phase = pend.get("phase")
            if phase in ("tech_review", "code_review") and req.get("phase") == phase:
                redirect = D.stage_of_review(req, phase)
                if redirect:
                    D.set_req(int(req_id_str), {"phase": redirect})
                    log.info(f"需求 #{req_id_str} 评审打回，{phase} 重定向到 {redirect}")
            D.clear_retries(int(req_id_str))
            D.clear_flow_rounds(int(req_id_str))
            D.post_dispatcher_event(int(req_id_str), "已在网页端否决，退回待开发", kind="phase")
            D.notify(f"需求 #{req_id_str} 已在网页端否决，退回待开发。")
        else:
            # 兜底留痕：用户改判为 later/dropped/done 等状态（未走通过/否决按钮，如空格误触通过后
            # 改回暂缓），pending 已消费但写调度事件便于追溯；不发飞书避免打扰
            log.info(f"需求 #{req_id_str} 评审 pending 已清除：状态改为 {req['status']}（未走通过/否决）")
            D.post_dispatcher_event(int(req_id_str),
                                  f"评审 pending 已清除：需求状态改为「{req['status']}」，未执行通过/否决动作",
                                  kind="phase")
    # #386 merge_conflict 网页决策对账：网页按钮把 action（retry/takeover）写入 merge_conflicts 表，
    # 本分支以 pending_confirm（kind=merge）为权威消费——与 on_message 同 state_lock 互斥，先 pop 者执行；
    # 无 pending 视为已由飞书 y/n 并发消费的幽灵决策，清表 + 事件留痕（防双通道并发双执行）。
    # retry 语义与飞书 y 一致：成功 → cleanup + restart 配方 + _set_done（含 _kickstart_board_if_own 与
    # #380 自动重启登记）；仍冲突 → 重挂 pending + 重置按钮等待再次点击。
    # takeover 语义与飞书 n 一致：置 later，dev 分支与 worktree 保留。
    try:
        conflicts = D.api_board("GET", "/merge-conflicts") or []
    except Exception as e:
        log.warning(f"决策对账拉取 merge 冲突列表失败（本轮跳过）: {e}")
        conflicts = []
    for mc in conflicts:
        action = mc.get("action")
        if action not in ("retry", "takeover"):
            continue  # action 为空=等待用户操作
        rid = str(mc["req_id"])
        mm = False
        cid = None
        with D.state_lock:
            pend = D.state["pending_confirm"].get(rid)
            if pend is None or pend.get("kind") != "merge":
                pend = None
            else:
                # #27：网页决策编号与条目内容不同源 → 不消费（不 pop），出锁后清表 + 归位
                mm, cid = D._pending_key_mismatch(pend, rid)
                if not mm:
                    D.state["pending_confirm"].pop(rid, None)
        # 决策已消费（无论何种出口）：先清表，后续分支按需重新登记（幂等，失败不阻断）
        D.merge_conflict_clear(rid, "reconcile 消费")
        if mm:
            D._pending_rehang(rid, pend, cid, "网页 merge 决策", question=pend.get("question") or "")
            continue
        req = all_reqs.get(int(rid))
        if pend is None:
            log.info(f"需求 #{rid} 网页端 merge 决策无待确认 pending（可能已由飞书 y/n 消费），表记录已清")
            D.post_dispatcher_event(int(rid),
                                  "网页端 merge 决策无待确认 pending（可能已由飞书回复处理），表记录清理",
                                  kind="confirm")
            continue
        if req is None:
            log.info(f"需求 #{rid} 已不存在，merge 冲突表记录已清")
            continue
        if action == "takeover":
            D.append_summary(int(rid), "用户人工接管合并（网页端按钮），需求置为暂缓，dev 分支与 worktree 保留待人工处理。")
            D.set_status(int(rid), "later")
            D.post_dispatcher_event(int(rid), "用户在网页端人工接管合并，需求置为暂缓，分支/worktree 保留", kind="merge")
            D.notify(f"需求 #{rid} 已在网页端请求人工接管，置为暂缓；dev 分支与 worktree 保留，请人工处理。")
        else:
            # retry：对齐 on_message y 分支（#106 改动文件清单在合并前捕获）
            with D.state_lock:
                rec = D.state.get("worktrees", {}).get(rid)
            touched = bool(rec) and D._touches_dispatcher(req, rec["branch"])
            merge_files = (D._collect_merge_changed_paths(req, rec["branch"]) if rec
                           and D.PROJECTS.get(req.get("project") or "", {}).get("auto_restart_after_merge")
                           else None)
            ok, failed = D.merge_dev_branches(req)
            if ok:
                D.cleanup_dev_worktrees(rid, delete_branch=True)
                restart_failed, restart_ok, restart_warn = D._maybe_auto_restart_after_merge(
                    req, merge_files, int(rid))
                D._set_done(req, dispatcher_touched=touched, restart_failed=restart_failed,
                          restart_ok=restart_ok, restart_warn=restart_warn)
                D.notify(f"需求 #{rid} 已在网页端重试合并成功，已完成。")
                D.post_dispatcher_event(int(rid), "网页端重试合并成功，清理 worktree，需求完成", kind="merge")
            else:
                entry = {"req": req, "question": pend["question"], "kind": "merge"}
                with D.state_lock:
                    ok = D._put_pending_confirm(rid, entry)
                D.save_state()
                if not ok:
                    D._pending_source_mismatch_note(rid, entry, "网页重试仍冲突重挂")
                guard = D._merge_guard_blockers(req, failed)
                fail_text = (f"被主干守卫拦下（{D._branch_guard_text(guard)}），未执行合并"
                             if guard else D._merge_fail_text(failed, D._merge_dirty_blockers(req, failed)))
                D.notify(f"需求 #{rid} 已在网页端重试合并但{fail_text}，"
                       "请继续处理（解决后再次点击「重试合并」或回复 y 重试）。")
                D.post_dispatcher_event(int(rid),
                                      f"网页端重试合并{fail_text}，按钮重置等待继续处理",
                                      kind="merge")
                D.merge_conflict_write(rid)  # 重置按钮等待再次点击（失败降级=按钮消失走飞书通道）
    D.save_state()


def confirm_decision_clear(req_id, reason=""):
    """#400 确认决策表清除（消费成功/幽灵决策清理；幂等，失败不阻断，降级仅日志）"""
    try:
        D.api_board("DELETE", f"/requirements/{req_id}/confirmations")
    except Exception as e:
        log.warning(f"需求 #{req_id} 确认决策表清除失败（{reason}）: {e}")


def reconcile_confirmations():
    """#400 网页确认动作对账：GET /api/confirmations 全量 + 本地过滤 action 非空（#386 先例）。
    以 pending_confirm 为权威消费（与 on_message 同 state_lock 互斥，先 pop 者执行）；
    kind 不匹配 / 无 pending = 已由飞书 y/n 并发消费的幽灵决策 → 清表行 + 事件留痕，不执行动作。
    语义与飞书 y/n 对等（对等复刻 on_message 对应分支，on_message 零改动）；
    confirm_rounds 单调计数不清零（无复位点）。"""
    try:
        confirms = D.api_board("GET", "/confirmations") or []
    except Exception as e:
        log.warning(f"确认对账拉取 confirmations 失败（本轮跳过）: {e}")
        return
    for c in confirms:
        action = c.get("action")
        kind = c.get("kind") or "confirm"
        if action not in D.WEB_CONFIRM_ACTIONS.get(kind, ()):
            continue  # action 为空 / 不在白名单 = 等待用户操作或非本通道决策
        rid = str(c["req_id"])
        mm = False
        cid = None
        with D.state_lock:
            pend = D.state["pending_confirm"].get(rid)
            if pend is None or (pend.get("kind") or "confirm") != kind:
                pend = None
            else:
                # #27：网页决策编号与条目内容不同源 → 不消费（不 pop），出锁后清表 + 归位
                mm, cid = D._pending_key_mismatch(pend, rid)
                if not mm:
                    D.state["pending_confirm"].pop(rid, None)
        # 采取「先消费动作、成功后清表」：动作执行异常时恢复 pending + 保留表行可重试
        # （fail-closed，不静默丢决策；对齐 tech 用例 17）
        if mm:
            D.confirm_decision_clear(rid, "待确认条目键与内容不同源")
            D._pending_rehang(rid, pend, cid, "网页确认动作", question=pend.get("question") or "")
            continue
        req = D.fetch_req(int(rid))
        if pend is None:
            D.confirm_decision_clear(rid, "reconcile 幽灵清理")
            log.info(f"需求 #{rid} 网页端确认决策无待确认 pending（可能已由飞书 y/n 消费），记录已清理")
            D.post_dispatcher_event(int(rid),
                                  "网页端确认决策无待确认 pending（可能已由飞书回复处理），记录清理",
                                  kind="confirm")
            continue
        if req is None:
            D.confirm_decision_clear(rid, "reconcile 残留清理")
            log.info(f"需求 #{rid} 已不存在，确认决策记录已清")
            continue
        if req.get("status") in ("done", "dropped"):
            # #25：卡已进入终态（人工置 done/dropped）——残留 action 不得执行（防 reject/
            # stop_later 把终态卡改回 open/later）；走同一终态清账单点后跳过本次 action
            D._purge_ghost_confirm(rid, "reconcile 终态残留清理")
            log.info(f"需求 #{rid} 已终态，确认决策不执行并清理")
            continue
        question = pend.get("question") or ""
        reason = c.get("reason") or ""   # #415 网页弹窗反馈意见（可选，转开发时注入 dev 提示词）
        try:
            D._execute_confirm_action(rid, req, pend, action, kind, question, reason)
        except Exception as e:
            log.error(f"需求 #{rid} 网页确认动作 {action} 执行异常（{e}），恢复 pending 保留决策待重试")
            with D.state_lock:
                ok = D._put_pending_confirm(rid, pend)
            D.save_state()
            if not ok:
                D._pending_source_mismatch_note(rid, pend, "网页确认动作异常恢复")
            D.post_dispatcher_event(int(rid),
                                  f"网页确认动作执行异常（{action}: {e}），决策保留待重试",
                                  kind="confirm")
            continue
        D.confirm_decision_clear(rid, "reconcile 消费")
    D.save_state()


def _execute_confirm_action(rid, req, pend, action, kind, question, reason=""):
    """#400 网页确认动作执行（对等复刻 on_message 对应分支语义；on_message 零改动）。
    已由 reconcile_confirmations 先 pop 认领；异常上抛由其恢复 pending + 保留表行重试。
    reason：#415 网页转开发弹窗填写的反馈意见（可选，空则不改变原默认原因）。"""
    if action == "proceed":
        if kind == "confirm" and pend.get("review_index") is None and not pend.get("flow_id"):
            answer = reason or "同意继续"
            D.append_summary(int(rid), D.confirm_answer_summary(question, answer))
            D.post_dispatcher_event(int(rid), f"用户网页确认继续：{answer}", kind="confirm")
            D.notify(f"需求 #{rid} 已收到继续确认，{D.ROLE_NAMES.get(D.role_for(req), D.role_for(req)) or '当前角色'}阶段继续执行。")
            D.spawn_agent(req, extra=f"用户对之前的问题「{question}」的答复：同意。{answer}", resume=True)
        return
    if action == "stop_later":
        # 新增明示出口（网页专属超限按钮）：转暂缓 + summary 留痕；非飞书 n 的对等复刻
        # （飞书 n 普通确认语义=退 open，n+later 前缀仅 confirm_override 存在）
        D.append_summary(int(rid), f"用户停止追问，转暂缓。（问题：{question}）")
        D.set_status(int(rid), "later")
        D.notify(f"需求 #{rid} 已停止追问并置为暂缓，请人工处理。")
        D.post_dispatcher_event(int(rid), "用户在网页端停止追问，需求置为暂缓", kind="confirm")
        return
    if action == "pass":
        if kind == "confirm" and pend.get("review_index") is not None:
            node = D.current_node(req)
            if node is not None and (node.get("node_type") or "") == "review":
                with D.state_lock:
                    D.state.get("flow_review_rounds", {}).get(rid, {}).pop(
                        str(pend.get("review_index")), None
                    )
                _fid, nodes, idx = D.flow_snapshot(req)
                prev = nodes[idx - 1] if nodes and idx > 0 else None
                D._clear_review_escalation(
                    req["id"], str(pend.get("review_index")), prev.get("role") if prev else None
                )
                D.save_state()
                D.append_summary(int(rid), f"人工裁决放行（{D.node_display(node)}）："
                                         f"{reason or '同意'}。")
                D.post_dispatcher_event(
                    int(rid), f"人工裁决放行「{D.node_display(node)}」，进入下一节点", kind="phase"
                )
                D._advance_after_node(req, node)
            else:
                D.notify(f"需求 #{rid} 当前节点已变化，未执行放行，请在看板确认现状。")
            return
        if kind == "review_gate":
            # #337：审查发现问题，人工确认仍合并——红字留档后放行 finalize_done
            with D.state_lock:
                pmr = D.state.get("pending_merge_review", {}).pop(str(req["id"]), None)
            D.save_state()
            D.append_summary(int(rid), f"⚠️ 代码审查发现问题，人工确认仍合并：{question}")
            D.post_dispatcher_event(int(rid), "审查发现问题，人工确认仍合并（发现已留档）", kind="merge")
            D.finalize_done(req, summary=(pmr or {}).get("summary"))
            D.notify(f"需求 #{rid} 审查发现的问题已红字留档，人工确认放行合并。")
        elif kind == "confirm_override":
            # #399：人工确认豁免——红字留档后 finalize_done 统一收口
            D.append_summary(int(rid), f"⚠️ 机制校验未过，人工确认豁免：{question}")
            if pend.get("todos") is not None:
                with D.state_lock:
                    D.state.setdefault("req_todos", {})[str(req["id"])] = pend.get("todos")
                D.save_state()
            D.post_dispatcher_event(int(rid), "收尾校验未过，人工确认豁免收口需求", kind="merge")
            D.finalize_done(req, summary=pend.get("summary"))
            D.notify(f"需求 #{rid} 收尾校验未过，经人工确认豁免，需求已完成。")
        return
    if action == "rework":
        if kind == "confirm" and pend.get("review_index") is not None:
            node = D.current_node(req)
            _fid, nodes, idx = D.flow_snapshot(req)
            prev = nodes[idx - 1] if nodes and idx > 0 else None
            if node is not None and (node.get("node_type") or "") == "review" and prev:
                D.set_req(rid, {"node_index": idx - 1})
                fresh = D.fetch_req(req["id"]) or req
                cur = ((D.state.get("flow_review_rounds") or {}).get(rid, {})
                       or {}).get(str(pend.get("review_index")), 0)
                D.append_summary(
                    int(rid), f"人工裁决打回「{D.node_display(prev)}」重做："
                              f"{reason or '未说明'}"
                )
                D.post_dispatcher_event(
                    int(rid), f"人工裁决打回「{D.node_display(prev)}」再修一轮", kind="phase"
                )
                D.notify(f"需求 #{rid} 已按人工裁决打回「{D.node_display(prev)}」重做。")
                D._spawn_safe(
                    fresh,
                    prev.get("role"),
                    extra=D._review_bounce_extra(
                        fresh, node, reason or "（网页端打回）", cur or None
                    ),
                    resume=True,
                )
            else:
                D.set_status(int(rid), "later")
                D.notify(f"需求 #{rid} 当前节点已变化，已置为暂缓等待人工处理。")
            return
        if kind == "review_gate":
            # #411：审查发现问题，人工转开发——等效飞书 n <id> 转开发（共用 _review_gate_rework）
            # #415：网页弹窗填写了意见则原样注入；未填写保持默认「（网页端转开发）」
            D._review_gate_rework(str(req["id"]), req, question, reason or "（网页端转开发）")
        return
    if action == "reject":
        if kind == "review_gate":
            # #337：审查发现问题，人工否决合并——转暂缓保留分支/worktree
            with D.state_lock:
                D.state.get("pending_merge_review", {}).pop(str(req["id"]), None)
            D.append_summary(int(rid), f"代码审查发现问题（{question}），人工否决合并；"
                                     "dev 分支与 worktree 保留待人工处理。")
            D.set_status(int(rid), "later")
            D.notify(f"需求 #{rid} 审查发现问题，已否决合并并置为暂缓，dev 分支与 worktree 保留，请人工处理。")
            D.post_dispatcher_event(int(rid), "审查发现问题，人工否决合并，需求转暂缓（分支/worktree 保留）",
                                  kind="merge")
        elif kind == "confirm_override":
            # #399：人工否决豁免——退 open（phase 置空 + 清 retries）重新分析
            D.set_req(int(rid), {"status": "open", "phase": None})
            D.clear_retries(str(req["id"]))
            D.clear_flow_rounds(str(req["id"]))
            D.append_summary(int(rid), f"用户否决豁免：未说明（校验未过：{question}），退回待开发重新分析。")
            D.notify(f"需求 #{rid} 已否决豁免并退回待开发，将重新分析。")
            D.post_dispatcher_event(int(rid), "用户否决豁免，退回待开发", kind="confirm")
        return
    if action == "reject_later":
        # 仅 confirm_override 白名单：等效飞书 n+later 前缀 → 转 later 人工收口
        D.set_status(int(rid), "later")
        D.append_summary(int(rid), f"人工否决豁免（later）：未说明（校验未过：{question}），"
                                 "dev 分支与 worktree 保留待人工处理。")
        D.notify(f"需求 #{rid} 已否决豁免并置为暂缓，dev 分支与 worktree 保留，请人工处理。")
        D.post_dispatcher_event(int(rid), "人工否决豁免（later），需求转暂缓（分支/worktree 保留）",
                              kind="confirm")

def _mark_running_rows_idle(req, role, rows=None):
    """#388 采纳后收尾：本需求 role 匹配的 running 会话行 → idle（驱动进程已随旧调度器
    死亡、结果已消费）。rows 可选（调用方已拉取的 agent-sessions 列表，避免重复 GET）；
    失败仅降级（日志），不阻断采纳结果。"""
    if rows is None:
        try:
            rows = D.api_board("GET", "/agent-sessions")
        except Exception:
            return
    for r in rows:
        if (r.get("status") == "running" and str(r.get("req_id")) == str(req["id"])
                and (r.get("role") or "") == role):
            # #421：统一入口（置 idle + 清 turn_step）；失败由 _close_session_row 内部告警降级
            D._close_session_row(req, role, r.get("id"), "idle")


# #388（审查问题 A）：本次启动已采纳合法结果的卡——reconcile_startup 无条件把 doing
# 退 open，会把免确认采纳后保持 doing 的卡闪变成 open（验收标准 1：消除 doing/open 闪变）
_adopted_rids = set()


def reconcile_adopt_results():
    """#388 重启对账「采纳合法结果」：doing 卡 + 产物 result.json 存在、JSON 合法且
    status ∈ {done, blocked, need_confirm}（收尾契约值）→ 直接走 handle_result 采纳推进
    （analysis → route / 免确认直接进 dev），不退回 open、不重跑；缺失/非法/非收尾状态
    不动（由 reconcile_startup 退 open 重跑，行为不变）。
    - 顺序依赖：必须先于 reconcile_startup（R1）——先退 open 的话重捞 prompt 会指示
      「删除残留 result.json」，合法结果被主动清理后重跑整轮；
    - 采纳归因：returncode=0、timed_out=False（正常完成语义）；started_at 现场不可得
      （post_run_record 未落库），传 time.time() 兜底（dev has_code_changes 已废弃 since）；
    - 幂等：handle_result 解析成功后 unlink result.json，天然防重复采纳；
    - 采纳成功把本需求 role 的 running 会话行标 idle（中间栏无僵尸残留）；
    - 角色判定：#388（审查问题 3）优先取该需求 running 会话行的 role（auto 通道
      review 会话残留时 role_for 恒返回 dev 会错配，行 role=review 时正确归因
      finalize_done），无 running 行时 role_for 兜底；
    - 看板不可达整体跳过（仿 reconcile_startup）；单卡失败降级继续。"""
    session_rows = None  # 懒加载：首个可采纳卡才 GET agent-sessions（复用给行置 idle）
    for project in D.PROJECTS:
        try:
            for r in D.get_doing_reqs(project):
                rid = str(r["id"])
                if rid in D.state["pending_confirm"] or \
                   rid in D.state["pending_route"] or \
                   rid in D.state["pending_review"]:
                    continue
                rf = D.result_file_path(r)
                if not rf.exists():
                    continue
                try:
                    parsed = json.loads(rf.read_text())
                except Exception as e:
                    log.warning(f"启动对账：需求 #{r['id']} 结果文件非法 JSON（{rf}: {e}），"
                                "跳过采纳（现场保留，交 reconcile_startup 退 open 重跑）")
                    continue
                if (parsed or {}).get("status") not in ("done", "blocked", "need_confirm"):
                    log.warning(f"启动对账：需求 #{r['id']} 结果 status="
                                f"{(parsed or {}).get('status')!r} 非收尾契约值，跳过采纳")
                    continue
                if session_rows is None:
                    try:
                        session_rows = D.api_board("GET", "/agent-sessions")
                    except Exception:
                        session_rows = []
                role = None
                for sr in session_rows:
                    if (sr.get("status") == "running" and str(sr.get("req_id")) == rid
                            and (sr.get("role") or "")):
                        role = sr.get("role")
                        break
                role = role or D.role_for(r)
                # #419：status=done 未必过 handle_result 的 valid_done 前置——按**节点类型**预检：
                # code 节点要求代码改动、artifact/review 节点要求产物存在（前置槽位同 artifact）；
                # 预检不合格不采纳（交 reconcile_startup 退 open 走既有重捞），否则 adopt 会把
                # handle_result 失败通道的同步 sleep（30-120s）+ 无主 resume spawn 带进启动序列
                _node = D.current_node(r)
                _ntype = (_node or {}).get("node_type") or "artifact"
                if (parsed or {}).get("status") == "done":
                    if _ntype == "code":
                        if not D.has_code_changes(r, time.time()):
                            log.warning(f"启动对账：需求 #{r['id']} {role} 结果 status=done 但 dev 分支无代码变更，"
                                        "不采纳（交 reconcile_startup 退 open 重跑）")
                            continue
                    else:
                        ok, why = D.artifact_ok(r, role)
                        if not ok:
                            log.warning(f"启动对账：需求 #{r['id']} {role} 结果 status=done 但产物校验失败"
                                        f"（{why}），不采纳（交 reconcile_startup 退 open 重跑）")
                            continue
                log.warning(f"启动对账：需求 #{r['id']} {role} 上次结果合法（{rf.name}），重启收尾采纳，不重跑")
                try:
                    D.handle_result(r, role, 0, False, time.time())
                    _mark_running_rows_idle(r, role, session_rows)
                    D._adopted_rids.add(rid)  # 记录已采纳：reconcile_startup 不再把该卡退 open
                    D.post_dispatcher_event(int(r["id"]),
                                          f"调度器重启，采纳上次合法收尾结果（{D.ROLE_NAMES.get(role, role)}，不重跑）",
                                          kind="phase")
                except Exception:
                    # #412（B）：启动对账 handle_result 异常留痕（旧实现 log.warning 无 traceback）
                    log.exception(f"启动对账：需求 #{r['id']} 结果采纳失败（跳过）")
        except Exception as e:
            log.warning(f"启动对账采纳项目 {project} 失败（跳过）: {e}")


def reconcile_startup():
    """重启时把没有待确认的 doing 卡片退回 open（重启后内存态运行记录必然丢失，
    重捞按卡上快照续跑）；退回后补写调度事件，让「调度历史回放」时间线有痕（#299）。
    #388：合法结果先经 reconcile_adopt_results 采纳（本函数之前调用），此处只处理
    缺失/非法结果的 doing 卡（现状行为不变）。
    #419：先处理「卡上快照当前节点为 terminal」的 doing/open 卡——直接执行收尾（幂等，已 done 跳过），
    这类卡**不退回 open**（terminal 无角色可拉起，退回必卡死）；merge_resolve 在途的卡跳过
    （由 merge_resolve 链自身收尾）。覆盖「回填产出的 open+terminal（旧 review@accept 卡）」与
    「重启中断时卡在 doing+terminal」两种死角。
    看板暂时不可达（如与看板重启窗口重叠）时跳过，避免启动即崩。"""
    for project in D.PROJECTS:
        try:
            rows = list(D.get_doing_reqs(project) or [])
            try:
                rows += list(D.get_open_reqs(project) or [])
            except Exception as e:
                log.warning(f"启动 reconcile 拉取 {project} open 卡失败（跳过 open 扫描）: {e}")
            for r in rows:
                rid = str(r["id"])
                if rid in D.state["pending_confirm"] or \
                   rid in D.state["pending_route"] or \
                   rid in D.state["pending_review"]:
                    continue
                node = D.current_node(r)
                if node is not None and (node.get("node_type") or "") == "terminal":
                    if rid in D.state.get("merge_resolve", {}):
                        log.info(f"启动 reconcile：需求 #{rid} 处于收尾节点且 merge_resolve 在途，跳过（由 merge_resolve 链收尾）")
                        continue
                    log.warning(f"启动 reconcile：需求 #{rid} [{project}] 处于收尾节点（terminal），直接执行收尾")
                    try:
                        D._run_terminal(r)
                    except Exception as e:
                        log.error(f"启动 reconcile：需求 #{rid} 收尾执行异常（跳过）: {e}")
                    D._adopted_rids.add(rid)
                    continue
                if r.get("status") != "doing" or rid in D._adopted_rids:
                    continue
                log.warning(f"启动 reconcile：需求 #{r['id']} [{project}] 调度器重启导致运行记录丢失，退回 open 待重捞续跑")
                D.set_status(r["id"], "open")
                D.post_dispatcher_event(int(r["id"]), "调度器重启，进行中需求退回 open（1 分钟后自动重捞续跑）", kind="phase")
        except Exception as e:
            log.warning(f"启动 reconcile 项目 {project} 失败（跳过）: {e}")


