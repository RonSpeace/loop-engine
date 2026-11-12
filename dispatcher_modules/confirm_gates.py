"""待确认条目「键与内容同源」三层闸门：need_confirm 登记、键校验与过期清理。"""

import os
import time
import urllib.request
from pathlib import Path

from dispatcher_modules.agent_run import active_lock
from dispatcher_modules.workspace import effective_repos
from prompt_i18n import prompt_lang, tr
from dispatcher_modules.runtime import D, log, subprocess


# 权威判据（单一）：条目内容卡的 id（entry["req"]["id"]）是身份权威，键服从内容；
# 卡是否终态一律 fetch_req 现取，禁用条目内快照的 status。
_CONFIRM_REQ_KEYS = ("id", "title", "project", "status", "flow", "flow_name", "node_index")


def _confirm_req_snapshot(req):
    """#27 待确认条目的 req 快照：深拷贝裁剪七键（去卡片对象别名，防 state 膨胀；
    不带 summary/notes/nodes_json）。非 dict 入参返回 None。"""
    if not isinstance(req, dict):
        return None
    return {k: req.get(k) for k in _CONFIRM_REQ_KEYS}


def _put_pending_confirm(key, entry):
    """#27 唯一落表出口（调用方须已持 state_lock）：裁剪 req 快照 + 写入 + 回读校验。

    **锁内纯内存函数**：不开锁、不做 IO、不落盘、不发事件、不打日志（state_lock 非重入，
    持锁再取恒死锁，#410/#412）——留痕与 save_state 由调用方在出锁后执行。
    返回 False = 键与内容不同源（调用方留痕）；entry["req"] 非 dict / id 不可解析 → True（fail-open）。"""
    key = str(key)
    entry["req"] = _confirm_req_snapshot(entry.get("req"))
    D.state["pending_confirm"][key] = entry
    snap = D.state["pending_confirm"].get(key)
    if not isinstance(snap, dict) or not isinstance(snap.get("req"), dict):
        return True
    try:
        return int(snap["req"]["id"]) == int(key)
    except (KeyError, TypeError, ValueError):
        return True


def _pending_content_req_id(entry, key):
    """#27 条目内容卡 id（身份权威读取，fail-open 回落键）：纯函数、无 IO。
    entry 非 dict / req 非 dict / id 不可解析 → int(key)。"""
    if isinstance(entry, dict) and isinstance(entry.get("req"), dict):
        try:
            return int(entry["req"]["id"])
        except (KeyError, TypeError, ValueError):
            pass
    return int(key)


def _pending_key_mismatch(pend, key):
    """#27 消费期同源判定：返回 (是否错位, 内容卡 id)。

    纯函数、无 IO、无副作用（可在持锁时调用）：pend 非 dict / req 非 dict / id 不可解析
    → (False, None)（fail-open，兼容 {"req": None} 注入与 pending_confirm={"295": True}）。"""
    if not isinstance(pend, dict) or not isinstance(pend.get("req"), dict):
        return False, None
    try:
        cid = int(pend["req"]["id"])
        return cid != int(key), cid
    except (KeyError, TypeError, ValueError):
        return False, None


def _pending_source_mismatch_note(key, entry, source):
    """#27 写入期错源留痕（调用方须在出锁后调用）：log.error + 需求时间线事件。"""
    try:
        cid = int(entry["req"]["id"])
    except (KeyError, TypeError, ValueError):
        cid = None
    msg = (f"待确认条目键与内容不同源（来源：{source}）：键 #{key}，内容属需求 #{cid}；"
           "消费期将按内容归属拒绝并归位，不会改动任何卡的状态")
    log.error(msg)
    try:
        D.post_dispatcher_event(int(key), msg, kind="confirm")
    except Exception as e:
        log.warning(f"待确认条目错源事件留痕失败（不阻断）: {e}")


def _pending_rehang(key, pend, content_id, source, question=None):
    """#27 消费期错源处理（三处消费点共用，**自带落盘**）：判定命中时条目仍在 state，
    此处「锁内改键 → 出锁 save_state → 留痕/通知」，盘上任一时刻要么原键要么新键。

    - 内容卡现取终态（done/dropped）→ 清掉条目（不归位）；
    - 目标键空闲 → 归位到内容卡键；
    - 目标键已被占用 → 保留原键 + src_conflict_hits 累计；≥3 次 → 清掉（转人工出口，
      通知与留痕带问题全文与正确编号，不静默丢）。
    不触碰任何卡的字段/状态、不动 confirm_rounds。返回 'rehanged'/'kept'/'cleared'。"""
    key = str(key)
    question = question or (pend.get("question") if isinstance(pend, dict) else "") or ""
    hits = int(pend.get("src_conflict_hits") or 0) if isinstance(pend, dict) else 0
    status = None
    try:
        req = D.fetch_req(content_id)
        status = (req or {}).get("status")
    except Exception as e:
        log.warning(f"待确认错源归位读取需求 #{content_id} 失败（按非终态处理）: {e}")
    outcome = "kept"
    with D.state_lock:
        if status in ("done", "dropped"):
            if D.state["pending_confirm"].get(key) is pend:
                D.state["pending_confirm"].pop(key, None)
            outcome = "cleared"
        elif str(content_id) not in D.state["pending_confirm"]:
            if D.state["pending_confirm"].get(key) is pend:
                D.state["pending_confirm"].pop(key, None)
            pend["req"] = _confirm_req_snapshot(pend.get("req"))
            D.state["pending_confirm"][str(content_id)] = pend
            outcome = "rehanged"
        else:
            hits += 1
            pend["src_conflict_hits"] = hits
            if hits >= 3:
                if D.state["pending_confirm"].get(key) is pend:
                    D.state["pending_confirm"].pop(key, None)
                outcome = "cleared"
    D.save_state()
    tail = f"问题：{question}" if question else ""
    if outcome == "rehanged":
        msg = (f"（{source}）回复编号 #{key} 与待确认条目内容不一致（内容属需求 #{content_id}）："
               f"已按内容归属归位到 #{content_id}，未改动任何卡的状态。{tail}")
    elif outcome == "cleared" and status in ("done", "dropped"):
        msg = (f"（{source}）回复编号 #{key} 与待确认条目内容不一致（内容属需求 #{content_id}）："
               f"该需求已 {status}，错源条目已清理，未改动任何卡的状态。{tail}")
    elif outcome == "cleared":
        msg = (f"（{source}）回复编号 #{key} 与待确认条目内容不一致（内容属需求 #{content_id}）："
               f"目标键 #{content_id} 已有待确认条目，连续 {hits} 次无法归位，错源条目已清理转人工处理，"
               f"未改动任何卡的状态。{tail}")
    else:
        msg = (f"（{source}）回复编号 #{key} 与待确认条目内容不一致（内容属需求 #{content_id}）："
               f"目标键 #{content_id} 已有待确认条目，保留原键未归位（第 {hits} 次），"
               f"未改动任何卡的状态。{tail}")
    log.error(msg)
    D.notify(msg)
    try:
        D.post_dispatcher_event(int(key), msg, kind="confirm")
    except Exception as e:
        log.warning(f"待确认错源事件留痕失败（不阻断）: {e}")
    return outcome


def _purge_ghost_confirm(req_id, reason="", terminal=False):
    """#25 终态清账单点 + #27 条目身份守卫（合并口径）：需求进入终态（done/dropped）时，
    同需求 pending_confirm 任意 kind 的条目、pending_merge_review 与 confirm_decisions
    决策行都必为残留——pending 只在事件驱动路径（飞书 y/n、网页 action）消费，人工置终态/
    推进路径完成会绕过消费导致永久残留（#24 现场：done 卡仍挂「待确认」徽标与裁决按钮，
    误 finalize_done/误打 later）。
    终态判定与清理同点完成：terminal=True（仅 _set_done 置 done 成功后使用）直接采信；
    terminal=False 一律现取 fetch_req 最新状态裁决，回读异常/缺卡/缺 status/非终态均 no-op。
    #394 红线按卡状态豁免：非终态卡任意 kind（含 confirm/confirm_override）逐字保留。
    #27 条目身份守卫：条目内容卡 id 是本卡才清——键与内容不同源（内容属别的卡）时不清、
    返回 False，由调用方留痕转人工，防误清在途卡的待确认。
    决策行清理由 confirm_decision_clear 幂等执行（pending 已被其它路径 pop 也能消除孤儿 action）。
    返回是否清理了 pending 条目（清后事件留痕，幂等不阻断终态收尾）。"""
    req_id = str(req_id)
    if not terminal:
        try:
            req = D.fetch_req(req_id)
        except Exception as e:
            log.warning(f"需求 #{req_id} 终态回读失败，幽灵清理跳过（{reason}）: {e}")
            return False
        if (req or {}).get("status") not in ("done", "dropped"):
            return False
    with D.state_lock:
        entry = D.state["pending_confirm"].get(req_id)
        if isinstance(entry, dict) and _pending_content_req_id(entry, req_id) != int(req_id):
            # #27 条目身份守卫：内容属别的卡（键与内容不同源）——不按键误清，保留待人工
            return False
        entry = D.state["pending_confirm"].pop(req_id, None)
        popped_pmr = D.state.get("pending_merge_review", {}).pop(req_id, None) is not None
    D.confirm_decision_clear(req_id, reason)
    D.save_state()
    if entry is not None:
        D.merge_conflict_clear(req_id, reason)
        D.post_dispatcher_event(int(req_id),
                              f"{reason}：清理幽灵待确认（kind={entry.get('kind') or 'confirm'}）",
                              kind="merge")
    return entry is not None or popped_pmr


def _main_pending_push_text(pending, limit=3):
    """{仓库名: 领先提交数|None} → 「repo-a 领先 2 个提交；repo-b（远端无主干分支）」摘要。"""
    parts = []
    for name, ahead in sorted((pending or {}).items()):
        if len(parts) >= limit:
            parts.append("…")
            break
        if ahead is None:
            parts.append(f"{name}（远端无主干分支，无法统计）")
        else:
            parts.append(f"{name} 领先 {ahead} 个提交")
    return "；".join(parts)


def _set_done(req, dispatcher_touched=False, summary=None, push_failed=None, restart_failed=None,
              restart_ok=False, restart_warn="",
              main_push_failed=None, main_pending_push=None):
    """统一 done 出口：置 done + 清重试 + （req-board 自身项目）重启看板 + 通知。
    dispatcher_touched=True 时附加「需手动重启调度器」提醒（summary + 飞书 + 时间线）。
    push_failed 非空（#288：仅推分支项目 f-分支自动 push 失败仓库名集合）时附加「手动 push」
    提醒（summary + 时间线 + done 卡片红字，同样不阻断 done）。
    main_push_failed 非空（git_sync.on_done=auto：本地合并项目主干自动推送失败仓库名集合）时
    附加「手动推送主干」提醒（summary + 时间线 + done 卡片红字，同样不阻断 done）。
    main_pending_push 非空（git_sync.on_done=manual：{仓库名: 领先远端提交数|None}）时附加
    「主干待手动推送」提醒（summary + 时间线 + done 卡片红字三处，不阻断 done）。
    文案口径：仅推分支项目用「f-分支」措辞，本地合并项目统一用「主干/手动推送」。
    restart_failed 非空（#300：done 后按 restart 配方自动重启项目服务失败的原因字符串）时
    附加「需人工重启」提醒（summary + 时间线 + done 卡片红字，同样不阻断 done）。
    restart_ok（#318）：restart 配方已成功执行（调用方显式透传，未配置/失败不成立）时对
    todos 对账——纯重启类条目剔除、混合条目标注「调度器已自动重启」，避免 done 通知同时
    出现「已自动重启项目服务」与「待办：重启服务」半过时红字；对账在消费处统一完成，
    summary 留档与卡片展示保持一致。
    summary 可选：done 卡片正文展示完成摘要（为空显示「需求已完成」）。
    #279：agent 声明的遗留待办（todos）从 state["req_todos"] 消费——handle_result 解析
    result.json 时暂存（result 文件解析成功即删，done 流程无法再读），此处逐条写入 summary
    留档并随 done 卡片红字展示，消费后清理防 state 膨胀。
    #266 时序修复：先写 summary/通知，最后才重启看板——kickstart 重启窗口内看板 API
    不可达，若提前重启，提醒写入与 done 卡片会因 ConnectionRefused 中断（#264 现场）。"""
    req_id = str(req["id"])
    D.set_status(int(req_id), "done")
    # #25：done 收尾成功后单点清账——terminal=True 直接采信（置 done 前的 req 快照仍是旧
    # 状态，不能用来判终态）；同时清 pending_confirm（任意 kind）与 confirm_decisions 决策行
    # #27：条目身份守卫命中（键与内容不同源，内容属别的卡）→ 保留未清理，补留痕转人工
    purged = D._purge_ghost_confirm(req_id, "需求已完成", terminal=True)
    if not purged:
        with D.state_lock:
            blocked = D.state["pending_confirm"].get(req_id)
        if isinstance(blocked, dict) and _pending_content_req_id(blocked, req_id) != int(req_id):
            cid = _pending_content_req_id(blocked, req_id)
            D.post_dispatcher_event(int(req_id),
                                  f"需求 #{req_id} 已完成，但待确认条目内容属需求 #{cid}"
                                  f"（与本卡不一致），已保留未清理，请人工处理",
                                  kind="confirm")
    with D.state_lock:
        D.state["retries"].pop(req_id, None)
        # #11：done 收尾一并清模型降级记录（防残留）
        D.state.setdefault("model_fallback", {}).pop(req_id, None)
        todos = D.state.setdefault("req_todos", {}).pop(req_id, None) or []
    D.save_state()
    reminder = ""
    if dispatcher_touched:
        # #380：不再只发手动提醒——登记 pending_restart 由主循环择机自动重启（kill -TERM 自身，
        # KeepAlive 拉起）；done 通知文案按是否存在并行 agent 分支（重启成功/失败的后续通知
        # 分别由新进程启动自证与重试耗尽兜底路径补发）。pending_restart 在函数末尾登记（见下）。
        with active_lock:
            busy = bool(D.active_runs)
        hint = D.DISPATCHER_AUTO_RESTART_DELAYED if busy else D.DISPATCHER_AUTO_RESTART_PENDING
        reminder = f"\n⚠️ {hint}"
        try:
            D.append_summary(req_id, f"⚠️ {hint}")
        except Exception as e:
            log.warning(f"需求 #{req_id} done 提醒写 summary 失败（不阻断通知）: {e}")
        D.post_dispatcher_event(int(req_id),
                              "检测到改动涉及 dispatcher.py，将在并行 agent 结束后自动重启调度器" if busy
                              else "检测到改动涉及 dispatcher.py，调度器将自动重启（无需手动操作）",
                              kind="merge")
    if push_failed:
        # 该参数仅由仅推分支项目 push_company_branches 产生
        push_hint = (f"⚠️ 仅推分支项目 f-分支自动 push 失败（{', '.join(sorted(push_failed))}），"
                     f"本地分支已保留，请手动 push（git push origin <分支>）")
        reminder = (reminder + "\n" + push_hint) if reminder else f"\n{push_hint}"
        try:
            D.append_summary(req_id, push_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done push 失败提醒写 summary 失败（不阻断通知）: {e}")
        D.post_dispatcher_event(int(req_id), "仅推分支项目 f-分支自动 push 失败，done 通知附「手动 push」提醒", kind="merge")
    if main_push_failed:
        main_push_hint = (f"⚠️ 主干自动推送失败（{'、'.join(sorted(main_push_failed))}），"
                          f"本地合并已保留，请手动推送主干（git push <远端> <主干>）")
        reminder = (reminder + "\n" + main_push_hint) if reminder else f"\n{main_push_hint}"
        try:
            D.append_summary(req_id, main_push_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done 主干推送失败提醒写 summary 失败（不阻断通知）: {e}")
        D.post_dispatcher_event(int(req_id),
                              "主干自动推送失败，done 通知附「手动推送主干」提醒", kind="merge")
    if main_pending_push:
        pending_hint = (f"⚠️ 主干有提交待手动推送：{_main_pending_push_text(main_pending_push)}"
                        f"（git push <远端> <主干>）")
        reminder = (reminder + "\n" + pending_hint) if reminder else f"\n{pending_hint}"
        try:
            D.append_summary(req_id, pending_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done 待手动推送提醒写 summary 失败（不阻断通知）: {e}")
        D.post_dispatcher_event(int(req_id),
                              f"主干待手动推送（{_main_pending_push_text(main_pending_push)}），"
                              f"done 通知附提示", kind="merge")
    if restart_failed:
        restart_hint = f"⚠️ 项目服务自动重启失败：{restart_failed}，需人工重启"
        reminder = (reminder + "\n" + restart_hint) if reminder else f"\n{restart_hint}"
        try:
            D.append_summary(req_id, restart_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done 重启失败提醒写 summary 失败（不阻断通知）: {e}")
        D.post_dispatcher_event(int(req_id), f"项目服务自动重启失败：{restart_failed}，需人工重启", kind="merge")
    if restart_warn:
        warn_hint = f"⚠️ {restart_warn}"
        reminder = (reminder + "\n" + warn_hint) if reminder else f"\n{warn_hint}"
        try:
            D.append_summary(req_id, warn_hint)
        except Exception as e:
            log.warning(f"需求 #{req_id} done 重启判定告警写 summary 失败（不阻断通知）: {e}")
        D.post_dispatcher_event(int(req_id), restart_warn, kind="merge")
    display_todos = todos
    if todos:
        # #318：restart 配方已成功执行（restart_ok，调用方显式透传）时对账——纯重启类待办
        # 剔除、混合条目标注「调度器已自动重启」，保留浏览器验收等纯人工事项；未配置 restart
        # 或自动重启失败（restart_failed 非空，已有红字提醒）不过滤，行为不变。
        if restart_ok:
            display_todos = D._reconcile_restart_todos(todos)
            dropped = len(todos) - len(display_todos)
            if dropped:
                D.post_dispatcher_event(int(req_id),
                                      f"restart 配方已自动执行，剔除重启类待办 {dropped} 条", kind="merge")
        for todo in display_todos:
            try:
                D.append_summary(req_id, f"⚠️ 待办：{todo}")
            except Exception as e:
                log.warning(f"需求 #{req_id} done 待办写 summary 失败（不阻断通知）: {e}")
        if display_todos:
            D.post_dispatcher_event(int(req_id), f"done 通知展示 agent 声明的遗留待办 {len(display_todos)} 条（红字）", kind="merge")
    if D.PROJECTS[req["project"]].get("company"):
        D.notify_req_card("done", req, summary=summary, company=True, reminder=reminder, todos=display_todos)
    else:
        D.notify_req_card("done", req, summary=summary, company=False, reminder=reminder, todos=display_todos)
    # 重启放最后：先确保上面所有看板写入与飞书通知完成
    D._kickstart_board_if_own(req["project"], req["id"])
    if dispatcher_touched:
        # #380：pending_restart 必须最后登记（done 通知/合并/看板重启全部完成后）——主循环
        # 下一轮才据此 kill 自身，天然满足「先写通知最后重启」（#266 时序）。
        D._queue_auto_restart(req_id)

def finalize_done(req, summary=None):
    """需求到达 done 的统一出口：合并 dev 分支 → 清理 worktree → done。
    合并冲突则保留分支/worktree，先自动拉起 merge_resolve 会话解决（#396，配额 2 轮/15 分钟），
    失败/耗尽/语义冲突再转 pending_confirm（kind=merge）由用户处理后重试。
    summary 可选：done 卡片正文的完成摘要（dev 完成路径透传，accept/merge 重试路径不传）。"""
    req_id = str(req["id"])
    # git_sync（项目级仓库同步/推送）：None=未配置 → 本函数所有 git_sync 分支零动作
    sync = D._project_git_sync(req.get("project"))
    with D.state_lock:
        rec = D.state.get("worktrees", {}).get(req_id)
    if not rec:
        D._set_done(req, summary=summary)
        return
    # #106：改动文件清单必须在合并前捕获——--no-ff 合并后 branch 成为 main 祖先，
    # 三点 diff {base}...{branch} 恒为空（合并后检测恒 False，重启提醒失效）
    dispatcher_touched = D._touches_dispatcher(req, rec["branch"])
    changed = D.changed_repos(req, rec["branch"])
    # #394：合并改动文件清单同样必须在合并前采集（同 #106 三档判定依据）；
    # 仅开关开启时采集（开关未开维持 #300 行为，判定不介入、无额外 git 调用）
    merge_files = (D._collect_merge_changed_paths(req, rec["branch"])
                   if D.PROJECTS[req["project"]].get("auto_restart_after_merge") else None)
    if D.PROJECTS[req["project"]].get("company"):
        # 仅推分支项目（branch_only）：main 由外部 CI/CD 或人工发版时合并，dev 只建分支——不合并；
        # 按实际 diff 收口：cleanup 前把有改动仓库的 f-分支 push 到远端（只 push 分支本身、
        # 不 --force/不推其他 refs），push 失败不阻断 done。
        push_failed = D.push_company_branches(req, rec["branch"], changed)
        D.cleanup_dev_worktrees(int(req_id), delete_branch="changed", keep_repos=changed)
        D._set_done(req, dispatcher_touched=dispatcher_touched, summary=summary,
                  push_failed=push_failed)
        D._annotate_extra_repos(req, changed)
        return
    # git_sync：合并前 pull 档再同步一次（主工作区拉齐远端最新；失败只 warn+事件，不阻断合并）。
    # 这里不设 mode 覆盖，沿用 before_card 配置（fetch 档不合并、off 档零动作）。
    if sync and sync.get("before_card") == "pull":
        D._sync_main_worktrees(req, sync)
    ok, failed = D.merge_dev_branches(req)
    if not ok:
        # git_sync 主干守卫：主工作区不在主干 → 不合并（绝不在错分支落 dev），走人工通道且
        # 文案明确「切回主干后重试」，不得落到下面「工作区脏」口径误导。
        guard = D._merge_guard_blockers(req, failed)
        if guard:
            D._merge_conflict_fallback(req, rec, failed, guard=guard)
            return
        # main 工作区未提交改动挡住合并：merge_resolve（改 dev 分支）无从解决，直接转人工并列出文件
        blockers = D._merge_dirty_blockers(req, failed)
        if blockers:
            D._merge_conflict_fallback(req, rec, failed, blockers=blockers)
            return
        # #396：先自动拉起 merge_resolve 会话解决（配额 2 轮/15 分钟）；失败/耗尽转现有
        # 飞书 y/n + 网页按钮人工通道（_merge_conflict_fallback，行为与 #386 前一致）
        if D._start_merge_resolve(req, rec, failed, summary=summary):
            return
        D._merge_conflict_fallback(req, rec, failed)
        return
    D.cleanup_dev_worktrees(int(req_id), delete_branch=True)
    D._annotate_extra_repos(req, changed)
    D.post_dispatcher_event(int(req_id),
                          f"自动合并 dev 分支 {rec['branch']} 到 main，清理 worktree，需求完成"
                          + (f"（实际改动仓库：{', '.join(sorted(changed))}）" if changed else ""),
                          kind="merge")
    # git_sync.on_done：auto=推送主干（禁 force、非快进 fetch+ff 重试一次）；manual=只统计待推；
    # off/未配置=零动作。失败只进 _set_done 三处提醒，绝不阻断 done、不丢本地合并。
    main_push_failed, main_pending_push = D._push_done_worktrees(req, sync)
    # #300/#394：本地合并成功后按项目开关自动重启项目服务（成功发 merge 事件；失败原因经
    # _set_done 落 summary + 时间线 + done 卡片红字，不阻断 done；unknown 保守不重启 + 告警）
    restart_failed, restart_ok, restart_warn = D._maybe_auto_restart_after_merge(
        req, merge_files, req_id)
    # #318：对账判定「restart 配方已成功执行」（未配置/失败不成立），供 _set_done 过滤重启类待办
    # 新参数只在有值时传：既有测试/调用方用固定签名 mock _set_done 的兼容性不受影响。
    extra = {}
    if main_push_failed:
        extra["main_push_failed"] = main_push_failed
    if main_pending_push:
        extra["main_pending_push"] = main_pending_push
    D._set_done(req, dispatcher_touched=dispatcher_touched, summary=summary,
              restart_failed=restart_failed, restart_ok=restart_ok, restart_warn=restart_warn,
              **extra)

def _merge_resolve_entry(req_id):
    """merge_resolve 状态条目（无则空 dict）"""
    with D.state_lock:
        return dict(D.state.get("merge_resolve", {}).get(str(req_id), {}) or {})


def _start_merge_resolve(req, rec, failed, summary=None):
    """#396 finalize_done 合并冲突：登记状态并拉起 merge_resolve 会话（配额内）。
    返回 True=已拉起（冲突收尾转入自动解决流程）；False=配额耗尽/不可用，调用方走人工通道。"""
    req_id = str(req["id"])
    with D.state_lock:
        entry = D.state.setdefault("merge_resolve", {}).get(req_id) or {}
        attempts = int(entry.get("attempts", 0) or 0)
        if attempts >= D.MERGE_RESOLVE_MAX_ROUNDS:
            return False
        D.state["merge_resolve"][req_id] = {"req": req, "failed": list(failed),
                                          "attempts": attempts + 1,
                                          "branch": rec["branch"],
                                          "summary": summary,
                                          "started_at": time.time()}
    D.save_state()
    try:
        D.append_summary(req_id,
                       f"自动合并冲突（{', '.join(failed)}），拉起「冲突解决」会话自动处理"
                       f"（第 {attempts + 1} 轮，上限 {D.MERGE_RESOLVE_MAX_ROUNDS} 轮）；失败将转飞书/网页人工通道。")
    except Exception as e:
        log.warning(f"需求 #{req_id} merge_resolve 启动写 summary 失败（不阻断）: {e}")
    D.post_dispatcher_event(req_id,
                          f"自动合并冲突（{', '.join(failed)}），拉起「冲突解决」会话（第 {attempts + 1} 轮）",
                          kind="merge")
    log.warning(f"需求 #{req_id} 合并冲突，拉起 merge_resolve（第 {attempts + 1} 轮）: {failed}")
    D.spawn_agent(req, role="merge_resolve")
    return True


def _merge_resolve_retry(req, reason):
    """#396 merge_resolve 进程级失败重试（配额内，独立计数不污染 dev 重试语义）。
    返回 True=已重试；False=配额耗尽，调用方走人工通道。"""
    req_id = str(req["id"])
    with D.state_lock:
        entry = D.state.get("merge_resolve", {}).get(req_id)
        if not entry or int(entry.get("attempts", 0) or 0) >= D.MERGE_RESOLVE_MAX_ROUNDS:
            return False
        entry["attempts"] = int(entry.get("attempts", 0) or 0) + 1
    D.save_state()
    D.post_dispatcher_event(req_id,
                          f"merge_resolve 运行失败（{reason}），第 {entry['attempts']} 次自动重试",
                          kind="retry")
    D.spawn_agent(req, role="merge_resolve", resume=True,
                extra=tr(prompt_lang(req.get("project")), "上次尝试失败（第 {n} 次）：{reason}。{hint}",
                         n=entry["attempts"], reason=reason,
                         hint=tr(prompt_lang(req.get("project")),
                                 "先确认改动完好（git status / 冲突文件是否已解决）、worktree 内冲突是否仍在，"
                                 "再继续解决并提交；如属语义冲突需要人工决策，直接报 need_confirm。")))
    return True


def _merge_conflict_fallback(req, rec, failed, question=None, reason=None, blockers=None, guard=None):
    """#396 merge_resolve 失败/语义冲突兜底：转现有飞书 y/n + 网页按钮人工通道（行为与 #386 前一致——
    pending_confirm(kind=merge) + 红卡 + 按钮登记；分支/worktree 现场保留）。
    blockers（_merge_dirty_blockers 结果）非空 = main 工作区未提交改动挡住合并，提示文本改为列出文件。
    guard（_merge_guard_blockers 结果）非空 = git_sync 主干守卫拦下（主工作区不在主干），
    提示「切回主干后重试」，不得用「工作区脏」文案误导（两者互斥，guard 优先判定）。"""
    req_id = str(req["id"])
    rec = rec or {}
    branch = rec.get("branch") or D.dev_branch(req)
    with D.state_lock:
        D.state.get("merge_resolve", {}).pop(req_id, None)
    D.save_state()
    if guard:
        detail = D._branch_guard_text(guard)
        D.append_summary(int(req_id),
                       f"自动合并被主干守卫拦下（{detail}）：git_sync 启用时不把 dev 分支合并到"
                       f"非主干分支；已保留分支 {branch} 与 worktree。请切回主干后点「重试合并」"
                       f"或回复 y 重试，或 n 人工接管")
        D.post_dispatcher_event(int(req_id),
                              f"自动合并被主干守卫拦下（{detail}），未执行合并——"
                              f"保留分支 {branch} 与 worktree 待人工处理", kind="merge")
        question = question or f"主工作区不在主干分支（{detail}），dev 分支 {branch} 未合并，需切回主干后重试"
    elif blockers:
        detail = D._dirty_blockers_text(blockers)
        D.append_summary(int(req_id),
                       f"自动合并被 main 工作区未提交改动挡住（非代码冲突：{detail}），已保留分支 {branch} 与 worktree；"
                       "请先提交或 stash 这些改动，再点「重试合并」或回复 y 重试，或 n 人工接管")
        D.post_dispatcher_event(int(req_id),
                              f"自动合并被 main 工作区未提交改动挡住（{detail}），非代码冲突，"
                              f"未拉起 merge_resolve——保留分支 {branch} 与 worktree 待人工处理", kind="merge")
        question = question or f"dev 分支 {branch} 合并被 main 工作区未提交改动挡住（{detail}），需先提交或 stash"
    elif reason:
        D.append_summary(int(req_id),
                       f"自动合并冲突且 merge_resolve 未解决（{reason}），已保留分支 {branch} 与 worktree；"
                       "请在对应仓库解决冲突后回复 y 重试合并，或 n 人工接管")
        D.post_dispatcher_event(int(req_id),
                              f"自动合并冲突，merge_resolve 未解决（{reason}）——"
                              f"保留分支 {branch} 与 worktree 待人工处理", kind="merge")
    else:
        D.append_summary(int(req_id),
                       f"自动合并冲突（{', '.join(failed)}），已保留分支 {branch} 与 worktree；"
                       "请在对应仓库解决冲突后回复 y 重试合并，或 n 人工接管")
        D.post_dispatcher_event(int(req_id),
                              f"自动合并冲突（{', '.join(failed)}），保留分支 {branch} 与 worktree 待人工处理",
                              kind="merge")
    entry = {"req": req, "question": question or f"自动合并 dev 分支 {branch} 冲突，需人工处理",
             "kind": "merge"}
    with D.state_lock:
        ok = _put_pending_confirm(req_id, entry)
    D.save_state()
    if not ok:
        _pending_source_mismatch_note(req_id, entry, "合并冲突重挂")
    log.error(f"需求 #{req_id} 合并冲突: {failed}")
    D.notify_req_card("merge_conflict", req, failed=failed, branch=branch)
    # #386：登记网页端「重试合并/人工接管」按钮信号（失败降级=按钮不可见，飞书通道仍可用，不阻断收尾）
    D.merge_conflict_write(req_id)


def _finish_merge_resolve(req, req_id, agent_summary=""):
    """#396 merge_resolve done（已在 worktree 内把 main 合并进 dev 分支并提交）：
    调度器恢复 main worktree 干净现场（git merge --abort，恢复动作不进 agent 权限）→
    重试 merge_dev_branches；成功则清理 worktree + _set_done；仍冲突转人工通道（fail-closed）。"""
    with D.state_lock:
        entry = D.state.get("merge_resolve", {}).pop(req_id, None) or {}
        rec = D.state.get("worktrees", {}).get(req_id)
    D.save_state()
    dev_summary = entry.get("summary")
    if agent_summary:
        try:
            D.append_summary(req_id, f"合并冲突已由 agent 解决：{D.format_summary_points(agent_summary)}")
        except Exception as e:
            log.warning(f"需求 #{req_id} merge_resolve 解决说明写 summary 失败（不阻断）: {e}")
    if rec:
        # 冲突现场在 main worktree（=主工作区，agent 被 #303/#336 硬拦禁止写）：
        # 重试前必须由调度器先 git merge --abort 清理 MERGE_HEAD，否则直接再跑 merge 被 git 拒绝
        for p in D.PROJECTS[req["project"]]["paths"]:
            try:
                subprocess.run(["git", "merge", "--abort"], cwd=p, capture_output=True,
                               timeout=D.DIAG_TIMEOUT)
            except Exception:
                pass
    dispatcher_touched = bool(rec) and D._touches_dispatcher(req, rec["branch"])
    ok, failed2 = D.merge_dev_branches(req)
    if not ok:
        guard = D._merge_guard_blockers(req, failed2)
        if guard:
            D.append_summary(req_id,
                           f"merge_resolve 解决后重试合并仍被主干守卫拦下"
                           f"（{D._branch_guard_text(guard)}），转人工通道处理")
            D._merge_conflict_fallback(req, rec, failed2, guard=guard)
            return
        blockers = D._merge_dirty_blockers(req, failed2)
        D.append_summary(req_id,
                       f"merge_resolve 解决后重试合并{D._merge_fail_text(failed2, blockers)}，转人工通道处理")
        D._merge_conflict_fallback(req, rec, failed2, blockers=blockers)
        return
    changed = D.changed_repos(req, rec["branch"]) if rec else set()
    D.cleanup_dev_worktrees(int(req_id), delete_branch=True)
    D._annotate_extra_repos(req, changed)
    D.post_dispatcher_event(int(req_id),
                          f"merge_resolve 已解决冲突，自动合并成功，清理 worktree，需求完成"
                          + (f"（实际改动仓库：{', '.join(sorted(changed))}）" if changed else ""),
                          kind="merge")
    restart_failed = D.run_project_restart(req["project"], req_id) or None
    restart_ok = restart_failed is None and bool(D.PROJECTS[req["project"]].get("restart"))
    D._set_done(req, dispatcher_touched=dispatcher_touched, summary=dev_summary,
              restart_failed=restart_failed, restart_ok=restart_ok)


def _annotate_extra_repos(req, changed):
    """done 反馈闭环：实际改动仓库含清单外（analysis/notes 未识别）时事件 + summary 标注。"""
    focus = set(effective_repos(req))
    extra = changed - focus
    if not extra:
        return
    names = ", ".join(sorted(extra))
    D.post_dispatcher_event(int(req["id"]),
                          f"实际改动含清单外仓库：{names}（analysis/notes 未识别，已按实际改动处理）",
                          kind="merge")
    D.append_summary(int(req["id"]), f"实际改动仓库含清单外：{names}（analysis/notes 未识别）。")

def _stale_worktree_blocker(rec):
    """残留 worktree 能否安全回收：逐仓库校验「worktree 无未提交改动」+「dev 分支已合并入主干」。
    返回阻塞原因（str），可回收返回 ""。人工置 done 而分支未合并时不得强删（会丢内容）。"""
    branch = (rec or {}).get("branch")
    if not branch:
        return "记录缺分支名"
    cfg = D.PROJECTS.get((rec or {}).get("project") or "", {})
    for repo, wt in rec.get("worktrees") or []:
        name = Path(repo).name
        if Path(wt).exists():
            r = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                               capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
            if r.returncode != 0:
                return f"{name}: worktree 状态不可读"
            if r.stdout.strip():
                return f"{name}: worktree 有未提交改动（{len(r.stdout.strip().splitlines())} 项）"
        start, _ = D._default_branch(repo, cfg)
        if not start:
            return f"{name}: 主干分支探测失败"
        r = subprocess.run(["git", "merge-base", "--is-ancestor", branch, start], cwd=repo,
                           capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
        if r.returncode != 0:
            return f"{name}: 分支 {branch} 未合并入主干 {start}"
    return ""

def _warn_stale_worktree(req_id, rec, status, why):
    """不可回收的残留 worktree 告警（同一原因只发一次，恢复可回收后清标记）。"""
    key = str(req_id)
    with D.state_lock:
        warned = D.state.setdefault("stale_wt_blocked", {})
        if warned.get(key) == why:
            return
        warned[key] = why
    D.save_state()
    branch = (rec or {}).get("branch")
    log.warning(f"需求 #{req_id} 残留 worktree 未回收（状态 {status}）：{why}；分支 {branch} 与现场保留")
    D.post_dispatcher_event(int(req_id),
                          f"残留 worktree 未自动回收：{why}；分支 {branch} 与 worktree 保留待人工确认")

def _clear_stale_worktree_warning(req_id):
    """回收成功后清告警标记（下次再阻塞能重新告警）。"""
    key = str(req_id)
    with D.state_lock:
        warned = D.state.setdefault("stale_wt_blocked", {})
        if warned.pop(key, None) is None:
            return
    D.save_state()

def cleanup_stale_worktrees():
    """清理 worktree 记录对应需求已 done/dropped/不存在时的残留（启动 + 主循环定期对账）。

    #428：人工合并、人工置 done 不走调度器收尾，残留 worktree/分支会一直留到下次重启——
    故除启动外由 _maybe_sweep_stale_worktrees 定期执行（原实现只在 main() 启动时跑一次；
    :461 的 "dropped" 判断保留作兜底，清理迁移遗漏的存量 dropped 行残留 worktree）。

    安全闸：只有「dev 分支已合并入各仓库主干」且「worktree 无未提交改动」才回收；否则保留
    现场 + 一次告警。定期执行不得放大强删风险——人工置 done 但分支未合并时删分支会丢内容。"""
    with D.state_lock:
        records = dict(D.state.get("worktrees", {}))
    if not records:
        return
    try:
        reqs = {str(r["id"]): r for r in D.api("GET", "")}
    except Exception as e:
        log.warning(f"清理残留 worktree 拉取需求失败: {e}")
        return
    for req_id, rec in records.items():
        status = (reqs.get(req_id) or {}).get("status")
        if not (status in ("done", "dropped") or reqs.get(req_id) is None):
            continue
        why = _stale_worktree_blocker(rec)
        if why:
            _warn_stale_worktree(req_id, rec, status, why)
            continue
        log.info(f"清理需求 #{req_id} 的 dev worktree（状态 {status}）：分支已合并且现场无未提交改动")
        D.cleanup_dev_worktrees(int(req_id), delete_branch=True)
        _clear_stale_worktree_warning(req_id)

# 残留 worktree 巡检间隔（秒）：只在到点的 tick 真查（有 git 开销），默认 5 分钟
STALE_WORKTREE_INTERVAL = int(os.environ.get("REQ_BOARD_STALE_WT_SEC", "300"))
_last_stale_wt_sweep = None  # None = 尚未巡检；不能用 0.0：monotonic 从开机起算，开机不足一个周期时首轮会被误判为「刚巡检过」

def _maybe_sweep_stale_worktrees():
    """主循环低频巡检（#428）：人工收尾不触发调度器 done 路径，靠定期对账回收残留。"""
    now = time.monotonic()
    if D._last_stale_wt_sweep is not None and now - D._last_stale_wt_sweep < STALE_WORKTREE_INTERVAL:
        return
    D._last_stale_wt_sweep = now
    D.cleanup_stale_worktrees()

# #431 空会话行（session_id 为空、未被首条消息接管）清理：TTL 秒（0 = 关闭定期清理）；
# 巡检间隔秒。删除闸门在看板 DELETE 端点（有未消费人工命令/真实会话一律 409），调度器只做候选筛选。
EMPTY_SESSION_TTL_SEC = int(os.environ.get("REQ_BOARD_EMPTY_SESSION_TTL_SEC", "86400"))
EMPTY_SESSION_SWEEP_INTERVAL = int(os.environ.get("REQ_BOARD_EMPTY_SESSION_SWEEP_SEC", "1800"))
_last_empty_session_sweep = None  # None = 尚未巡检；不能用 0.0：monotonic 从开机起算，开机不足一个周期时首轮会被误判为「刚巡检过」

def sweep_empty_sessions(max_age_sec=None):
    """#431 清理空会话注册行（候选筛选后逐个经看板 DELETE 端点删除，服务端可拒）。
    候选 = session_id 为空 + status=='created' + 非毒 + （max_age_sec is None 或 龄 > max_age_sec）；
    龄解析失败（None）不删（fail-closed）。max_age_sec=None = 启动存量清理（不限龄）。
    看板不可达单列 warn，异常不外抛（不阻断启动序列/主循环）。"""
    try:
        rows = D.api_board("GET", "/agent-sessions")
    except Exception as e:
        log.warning(f"空会话清理：看板不可达，本轮跳过（{e}）")
        return
    cand = []
    for r in (rows or []):
        if r.get("session_id") or r.get("status") != "created" or r.get("poisoned"):
            continue
        if max_age_sec is not None:
            age = D._session_age_sec(r.get("created_at"))
            if age is None or age <= max_age_sec:
                continue
        cand.append(r)
    deleted = skipped_busy = skipped_missing = 0
    for r in cand:
        path = f"/requirements/{r.get('req_id')}/sessions/{r.get('id')}"
        try:
            D.api_board("DELETE", path)
            deleted += 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                skipped_busy += 1      # 有未消费人工命令/会话运行中（看板守卫）
            elif e.code == 404:
                skipped_missing += 1   # 行/需求不存在，或看板仍是旧代码（无 DELETE 路由）
            else:
                log.warning(f"空会话清理：删除 {path} 失败 HTTP {e.code}")
        except Exception as e:
            log.warning(f"空会话清理：删除 {path} 失败 {e}")
    if cand:
        log.info(f"空会话清理：候选 {len(cand)}，删除 {deleted}，跳过 409={skipped_busy}，"
                 f"跳过 404={skipped_missing}")

def _maybe_sweep_empty_sessions():
    """主循环低频巡检（#431）：TTL<=0 关闭；按 EMPTY_SESSION_SWEEP_INTERVAL 节流。"""
    if D.EMPTY_SESSION_TTL_SEC <= 0:
        return
    now = time.monotonic()
    if D._last_empty_session_sweep is not None and now - D._last_empty_session_sweep < D.EMPTY_SESSION_SWEEP_INTERVAL:
        return
    D._last_empty_session_sweep = now
    D.sweep_empty_sessions(max_age_sec=D.EMPTY_SESSION_TTL_SEC)

