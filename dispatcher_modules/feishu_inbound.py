"""飞书消息入站：y/n 确认与评审指令、群聊消息解析、长连接与连接看门狗。"""

import json
import re
import time

from acp_shared import default_flow_id, role_model_detail

from dispatcher_modules.agent_run import active_lock
from dispatcher_modules.feishu_chat import _feishu_parse_material, _feishu_save
from dispatcher_modules.flow_advance import _review_bounce_extra
from dispatcher_modules.heartbeat import _update_feishu_status
from prompt_i18n import prompt_lang, tr
from dispatcher_modules.runtime import D, log, threading


def _is_reset_verdict(reason):
    """#376：需求级否决判定（评审卡 n 指令）：原因以「需求级」或「reset」开头 → 需求级否决退 open"""
    r = (reason or "").strip()
    return r.startswith("需求级") or r.lower().startswith("reset")

def _note_review_model(req, role):
    """#376/#419/#4：评审角色拉起时模型留痕（本期不强制异模）：模型唯一源 = 四层兜底链
    role_model_detail（项目级覆盖 → roles.<id>.model → 环境变量 → 全局 ACP_MODEL）。
    四层均未配置（source == "global"）→ 无留痕必要；配置了模型时与被评审阶段对比：
    同模仅事件/日志提示不阻断，异模提示独立模型。"""
    rmodel, rsource = role_model_detail(role, project=req.get("project"))
    if rsource == "global":
        return
    stage = D.stage_of_review(req, role)
    if not stage:
        return
    smodel = D.role_model(stage, project=req.get("project"))
    if rmodel == smodel:
        log.info(f"需求 #{req['id']} 评审角色 {role} 与被评审阶段 {stage} 同模（{rmodel}），仅留痕不阻断")
        D.post_dispatcher_event(req["id"],
                              f"评审角色「{D.ROLE_NAMES.get(role, role)}」与被评审阶段同模（{rmodel}），仅留痕提示",
                              kind="action")
    else:
        D.post_dispatcher_event(req["id"],
                              f"评审角色「{D.ROLE_NAMES.get(role, role)}」使用独立模型 {rmodel}（被评审阶段 {smodel}）",
                              kind="action")

def _spawn_safe(req, role, extra="", resume=False):
    """spawn 前复用 active_runs 去重，防飞书/按钮对账并发重复拉起"""
    with active_lock:
        if str(req["id"]) in D.active_runs:
            log.info(f"需求 #{req['id']} 已有活跃 agent，跳过重复 spawn（role={role}）")
            return False
    D.spawn_agent(req, role=role, extra=extra, resume=resume)
    return True


def _review_gate_rework(req_id, req, question, reason=""):
    """#411：审查发现问题转开发复修（飞书 n <id> 转开发 与 网页「转开发」按钮共用）——
    需求保持 doing（spawn_agent 内部置 doing，不显式 set）；pop pending_merge_review；
    resume 原 dev 会话（保上下文），merge_review.md 审查反馈经 extra 注入 dev 提示词
    （评审结论只走产物通道，不写 summary/notes，守 #342 通道约定）；summary + 调度事件
    各留痕一条（无上限计数，纯展示）；修复后 dev 报 done 重新触发 review 审查——
    转动循环无轮次上限（人工即边界，系统永不自动退回）。"""
    with D.state_lock:
        D.state.get("pending_merge_review", {}).pop(str(req_id), None)
    D.save_state()
    feedback = ""
    try:
        review_md = D.artifact_dir(req) / "merge_review.md"
        if review_md.exists():
            feedback = review_md.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log.warning(f"需求 #{req_id} 读取审查反馈 merge_review.md 失败（{e}），仅注入问题文本")
    extra = f"⚠️ 代码审查发现问题，人工已裁决「转开发」复修（原因：{reason or '未说明'}）。\n审查问题：{question}"
    if feedback:
        extra += f"\n审查结论（merge_review.md）：\n{feedback}"
    D.append_summary(req_id, f"代码审查发现问题（{question}），人工转开发复修（{reason or '未说明'}）；"
                           "原 dev 会话续跑，修复后将重新审查。")
    D.post_dispatcher_event(req_id, "审查发现问题，人工转开发复修（修复后将重新审查）", kind="merge")
    D.notify(f"需求 #{req_id} 审查发现问题，已转开发复修（原 dev 会话续跑并注入审查反馈），修复后将重新审查。")
    D._spawn_safe(req, "dev", extra=extra, resume=True)


def _feishu_group_text(msg):
    """群聊 @ 消息正文归一（#22 群聊支持）：飞书群 @ 文本里用 `@_user_N` 占位符标记被 @ 的人，
    按 message.mentions[].key 全部剥离后再去首尾空白。解析失败/无正文返回空串（调用方按无正文忽略）。"""
    try:
        content = json.loads(msg.content)
        text = content.get("text", "") if isinstance(content, dict) else ""
    except Exception:  # noqa: BLE001 - 异常报文按无正文处理，不影响 ws 回调线程
        return ""
    for mention in (getattr(msg, "mentions", None) or []):
        key = getattr(mention, "key", "")
        if isinstance(key, str) and key:
            text = text.replace(key, "")
    return str(text).strip()


def _feishu_post_text_and_handles(msg):
    """富文本（msg_type=post）→ (正文, 图片素材句柄列表)。

    飞书里「文字 + 粘贴截图」的消息常以 post 形态下发（不是 text/image 两条），此前在
    on_message 的类型白名单里被静默丢弃 —— 用户表现就是「@机器人贴图后没反应」。这里抽出
    所有 text/a 片段拼正文、img 片段生成图片句柄（只含 image_key，字节仍延迟到 worker 下载）。
    兼容 {"title","content":[[...]]} 与按语言包裹 {"zh_cn": {...}} 两种报文形态。"""
    try:
        content = json.loads(getattr(msg, "content", "") or "{}")
    except Exception:  # noqa: BLE001 - 异常报文按无正文处理，不影响 ws 回调线程
        return "", []
    if not isinstance(content, dict):
        return "", []
    if "content" not in content:
        for key in ("zh_cn", "en_us", "ja_jp"):
            wrapped = content.get(key)
            if isinstance(wrapped, dict) and "content" in wrapped:
                content = wrapped
                break
    lines, image_keys = [], []
    for paragraph in (content.get("content") or []):
        if not isinstance(paragraph, list):
            continue
        parts = []
        for element in paragraph:
            if not isinstance(element, dict):
                continue
            tag = element.get("tag")
            if tag == "text":
                parts.append(str(element.get("text") or ""))
            elif tag == "a":  # 富文本里的超链接：取可见文字，无文字取 href
                parts.append(str(element.get("text") or element.get("href") or ""))
            elif tag == "img":
                key = element.get("image_key")
                if isinstance(key, str) and key:
                    image_keys.append(key)
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    title = str(content.get("title") or "").strip()
    text = "\n".join([x for x in [title] + lines if x])
    for mention in (getattr(msg, "mentions", None) or []):
        key = getattr(mention, "key", "")
        if isinstance(key, str) and key:
            text = text.replace(key, "")
    message_id = getattr(msg, "message_id", None)
    handles = [{"message_id": message_id, "image_key": key, "type": "image",
                "name": f"图片-{key[:12]}.png", "size_hint": None}
               for key in image_keys]
    return text.strip(), handles


def _feishu_text_and_handles(msg):
    """已支持的任意消息 → (正文, 素材句柄列表)。text/post 解正文，file/image 走素材句柄；
    纯图片/文件补默认正文（与私聊素材语义一致），保证助手本轮拿到可读文本。"""
    mtype = getattr(msg, "message_type", "") or ""
    if mtype == "text":
        return _feishu_group_text(msg), []
    if mtype == "post":
        return _feishu_post_text_and_handles(msg)
    handle = _feishu_parse_material(msg)
    if handle is None:
        return "", []
    return ("（上传了图片）" if handle.get("type") == "image" else "（上传了文件）"), [handle]


def on_message(data):
    """飞书入站消息入口：处理期间的缺省 notify 回执（y/n 结果、问候等）只回飞书，
    不扇出到 Webhook/Slack（那是对飞书对话的应答，不是状态事件）。"""
    with D.NOTIFIERS.inbound_scope():
        return D._on_message(data)


def _on_message(data):
    msg = data.event.message
    mtype = getattr(msg, "message_type", "") or ""
    chat_type = getattr(msg, "chat_type", None)
    # ① 只处理 text/post/file/image；其余（sticker 等）忽略但必须留日志——
    #    静默 return 会让用户看到的是「@了机器人却毫无反应」，问题无法定位。
    if mtype not in ("text", "post", "file", "image"):
        log.info(f"忽略不支持的飞书消息类型（chat_type={chat_type}, message_type={mtype}）")
        return
    # ①-a 群聊（#22 行为变化 2）：@机器人 的文本/图文路由到看板助手；**不写 state["chat_id"]**
    # （私聊通知通道保持唯一）、不问候、不走 y/n 与建卡链路，回复按群 chat_id 显式发回群。
    # 飞书侧本应用只订阅「群组中@机器人消息」事件（未开 im:message.group_msg），能收到的群消息
    # 必然 @ 了机器人；若将来放开群消息权限，这里需补 mentions 归属校验，否则会回所有群消息。
    # 仅当 chat_type 是显式字符串且非 p2p 才走群分支——老测试桩用 Mock/缺省，按 p2p 兼容。
    # 图文混排（文字+粘贴截图）在飞书是 msg_type=post：正文与图片统一由 _feishu_text_and_handles
    # 解析，图片按素材句柄随本轮附件送助手（此前 post 不在类型白名单里，被静默丢弃）。
    if isinstance(chat_type, str) and chat_type and chat_type != "p2p":
        if not D._feishu_seen_once(msg.chat_id, getattr(msg, "message_id", None)):
            log.info(f"忽略重复投递的飞书群消息（chat_id={msg.chat_id}）")
            return
        text, handles = _feishu_text_and_handles(msg)
        if not text and not handles:
            log.info(f"忽略无正文/无素材的飞书群消息（chat_id={msg.chat_id}, message_type={mtype}）")
            return
        log.info(f"收到飞书群消息: {text}" + (f"（含 {len(handles)} 个附件）" if handles else ""))
        payload = {"kind": "assistant", "text": text,
                   "message_id": getattr(msg, "message_id", None)}
        if handles:
            payload["handles"] = handles
        D._feishu_enqueue(msg.chat_id, payload)
        _feishu_save()
        return
    # ①′ #24 消息幂等：去重键 = feishu_chat_id + message_id（覆盖 y/n、素材与助手文本），
    # 同一 message_id 只处理一次；message_id 缺失（测试桩/异常报文）不做去重，不吞消息。
    if not D._feishu_seen_once(msg.chat_id, getattr(msg, "message_id", None)):
        log.info(f"忽略重复投递的飞书消息（chat_id={msg.chat_id}）")
        return
    D.state["chat_id"] = msg.chat_id
    D.save_state()
    # file/image → 素材分支（句柄入队，worker 异步处理；优先级最高）
    # #44：无活跃旧建卡会话时素材进看板助手（有旧会话时仍进旧 pending 链路，见 _feishu_handle_material）；
    # payload 带 message_id 供 worker 在用户消息上贴/撤 Typing 表情。
    if mtype in ("file", "image"):
        handle = _feishu_parse_material(msg)
        if handle:
            D._feishu_enqueue(msg.chat_id, {"kind": "material", "handle": handle,
                                          "message_id": getattr(msg, "message_id", None)})
        else:
            # #22 ⑥：文件名缺失/内容不可解析 → 不产生无扩展名句柄，明确提示跳过（不拖垮整批素材）
            D._feishu_notify(msg.chat_id, "文件名缺失，已跳过该文件。")
        return
    if mtype == "post":
        # 私聊富文本：有图 → 图文一起作为助手本轮附件；无图 → 取正文后按普通文本走后续 y/n 与路由
        text, handles = _feishu_post_text_and_handles(msg)
        if handles:
            log.info(f"收到飞书消息（图文）: {text or '（图片）'}")
            D._feishu_enqueue(msg.chat_id, {"kind": "assistant",
                                          "text": text or "（上传了图片）",
                                          "message_id": getattr(msg, "message_id", None),
                                          "handles": handles})
            _feishu_save()
            return
    else:
        text = json.loads(msg.content).get("text", "").strip()
    if not text:
        log.info(f"忽略无正文的飞书消息（chat_id={msg.chat_id}, message_type={mtype}）")
        return
    log.info(f"收到飞书消息: {text}")
    # ② #24 首条消息问候（独立「已问候 chat 集合」，同 chat 只问候一次）——**不再 return**，
    # 问候后本条第 y/n、素材、建卡与助手路由照常处理（原实现把首条消息整条吞掉）。
    if D._feishu_greet_once(msg.chat_id):
        D.notify("已连接 req-board 调度器。待确认/评审问题会发到这里，回复 y <编号> 同意、n <编号> <原因> 否决。")
    # ③ y/n 正则（逐字保留、优先级最高）
    m = re.match(r"^(y|yes|同意|n|no|拒绝)\s*#?(\d+)\s*(.*)$", text, re.I)
    if not m:
        # ③′-⑥：文本路由（指令 / 活跃旧建卡会话 / 看板助手）
        D._feishu_route_text(msg.chat_id, text, getattr(msg, "message_id", None))
        return
    approve, req_id, reason = m.group(1).lower() in ("y", "yes", "同意"), int(m.group(2)), m.group(3).strip()
    # #27：回复编号与待确认条目内容不同源时不消费——锁内 get + 纯判定（命中不 pop），
    # 出锁后再按内容归属归位/清理（_pending_rehang 自带落盘），全程不改动任何卡的状态。
    mm = False
    cid = None
    pc = None
    with D.state_lock:
        pc = D.state["pending_confirm"].get(str(req_id))
        mm, cid = D._pending_key_mismatch(pc, req_id)
        if mm:
            pend = None
        else:
            pend = D.state["pending_confirm"].pop(str(req_id), None)
            kind = "confirm" if pend else None
            if pend is None:
                pend = D.state["pending_route"].pop(str(req_id), None)
                kind = "route" if pend else None
            if pend is None:
                pend = D.state["pending_review"].pop(str(req_id), None)
                kind = "review" if pend else None
    if mm:
        D._pending_rehang(req_id, pc, cid, "飞书 y/n", question=pc.get("question") or "")
        return
    if not pend:
        D.notify(f"需求 #{req_id} 没有待确认的问题。")
        return
    D.save_state()
    req = D.fetch_req(req_id) or pend.get("req")
    # 记录用户飞书回复（含原文）到需求时间线，完整呈现「agent 需要确认 → 用户回复 → 继续执行」闭环
    verdict = "同意" if approve else f"否决 - {reason or '未说明'}"
    D.post_dispatcher_event(req_id, f"用户飞书回复：{verdict}（{text}）", kind="confirm")
    if not approve:
        if kind == "route":
            D.set_status(req_id, "open")
            D.append_summary(req_id, f"用户否决通道建议：{reason or '未说明'}（analysis 建议 {pend.get('suggested_mode')}）")
            D.notify(f"需求 #{req_id} 已否决通道建议并退回待开发，将重新分析。")
            D.post_dispatcher_event(req_id, "用户否决通道建议，退回待开发", kind="phase")
        elif kind == "review":
            phase = pend.get("phase")
            # #376：人评否决双出口——需求级否决（原因以「需求级」/「reset」开头）= phase 置空退 open；
            # 默认打回 = 评审角色 phase 重定向前置 stage（重做算新一轮，清 retries）；普通 phase 保留自身
            if _is_reset_verdict(reason):
                D.set_req(req_id, {"status": "open", "phase": None})
                D.clear_flow_rounds(req_id)
                D.append_summary(req_id, f"用户需求级否决评审：{reason or '未说明'}（阶段 {phase}），需求退回待开发")
                D.notify(f"需求 #{req_id} 已需求级否决（{D.ROLE_NAMES.get(phase, phase)} 阶段），退回待开发。")
                D.post_dispatcher_event(req_id,
                                      f"用户需求级否决「{D.ROLE_NAMES.get(phase, phase)}」评审，退回待开发",
                                      kind="phase")
            else:
                nxt = D.stage_of_review(req, phase) or phase
                D.clear_retries(req_id)
                D.clear_flow_rounds(req_id)
                if nxt != phase:
                    D.set_req(req_id, {"status": "open", "phase": nxt})
                    D.append_summary(req_id, f"用户否决评审：{reason or '未说明'}（阶段 {phase}），打回{D.ROLE_NAMES.get(nxt, nxt)}重做")
                else:
                    D.set_status(req_id, "open")  # phase 保留，重捞从当前阶段重跑（反馈进提示词）
                    D.append_summary(req_id, f"用户否决评审：{reason or '未说明'}（阶段 {phase}）")
                D.notify(f"需求 #{req_id} 评审未通过（{D.ROLE_NAMES.get(phase, phase)} 阶段），已退回待开发，将按反馈重跑。")
                D.post_dispatcher_event(req_id,
                                      f"用户否决「{D.ROLE_NAMES.get(phase, phase)}」评审，退回待开发",
                                      kind="phase")
        elif pend.get("kind") == "review_gate":
            # #411：审查发现问题转人工三档裁决——reason 以「转开发」开头 = 转开发复修
            # （保持 doing、resume 原 dev 会话、merge_review.md 反馈注入，修复后重新审查）；
            # 普通 n = 否决合并转暂缓，保留分支/worktree 待人工处理（不直通合并）
            if reason.startswith("转开发"):
                _review_gate_rework(req_id, req, pend.get("question") or "",
                                    reason[3:].strip().lstrip("：: "))
            else:
                with D.state_lock:
                    D.state.get("pending_merge_review", {}).pop(str(req_id), None)
                D.append_summary(req_id, f"代码审查发现问题（{pend['question']}），人工否决合并：{reason or '未说明'}；"
                                       "dev 分支与 worktree 保留待人工处理。")
                D.set_status(req_id, "later")
                D.notify(f"需求 #{req_id} 审查发现问题，已否决合并并置为暂缓，dev 分支与 worktree 保留，请人工处理。")
                D.post_dispatcher_event(req_id, "审查发现问题，人工否决合并，需求转暂缓（分支/worktree 保留）", kind="merge")
        elif pend.get("kind") == "merge":
            D.append_summary(req_id, f"用户人工接管合并（{reason or '未说明'}），需求置为暂缓，dev 分支与 worktree 保留待人工处理。")
            D.set_status(req_id, "later")
            D.notify(f"需求 #{req_id} 已置为暂缓，dev 分支与 worktree 保留，请人工处理。")
            D.post_dispatcher_event(req_id, "用户人工接管合并，需求置为暂缓，分支/worktree 保留", kind="merge")
            D.merge_conflict_clear(req_id, "飞书 n 接管")
        elif pend.get("kind") == "confirm_override":
            # #399：人工否决豁免——默认退 open(phase 置空+清 retries)重新分析；
            # 原因以 `later` 开头 → 转 later 人工收口（分支/worktree 保留）
            if reason.lower().startswith("later"):
                D.set_status(req_id, "later")
                D.append_summary(req_id, f"人工否决豁免（later）：{reason[5:].strip() or '未说明'}（校验未过：{pend['question']}），"
                                       "dev 分支与 worktree 保留待人工处理。")
                D.notify(f"需求 #{req_id} 已否决豁免并置为暂缓，dev 分支与 worktree 保留，请人工处理。")
                D.post_dispatcher_event(req_id, "人工否决豁免（later），需求转暂缓（分支/worktree 保留）",
                                      kind="confirm")
            else:
                D.set_req(req_id, {"status": "open", "phase": None})
                D.clear_retries(req_id)
                D.clear_flow_rounds(req_id)
                D.append_summary(req_id, f"用户否决豁免：{reason or '未说明'}（校验未过：{pend['question']}），"
                                       "退回待开发重新分析。")
                D.notify(f"需求 #{req_id} 已否决豁免并退回待开发，将重新分析。")
                D.post_dispatcher_event(req_id, "用户否决豁免，退回待开发", kind="confirm")
        elif pend.get("review_index") is not None:
            # #419：评审轮次上限人工裁决——n 打回前驱节点再修一轮（指针先回退再 resume）
            node = D.current_node(req)
            _fid, nodes, idx = D.flow_snapshot(req)
            prev = (nodes[idx - 1] if nodes and idx > 0 else None)
            if node is not None and (node.get("node_type") or "") == "review" and prev:
                D.set_req(req_id, {"node_index": idx - 1})
                fresh = D.fetch_req(req_id) or req
                cur = (D.state.get("flow_review_rounds", {}).get(str(req_id), {})
                       or {}).get(str(pend.get("review_index")), 0)
                D.append_summary(req_id, f"人工裁决打回「{D.node_display(prev)}」重做：{reason or '未说明'}")
                D.post_dispatcher_event(req_id, f"人工裁决打回「{D.node_display(prev)}」再修一轮", kind="phase")
                D.notify(f"需求 #{req_id} 已按人工裁决打回「{D.node_display(prev)}」重做。")
                D._spawn_safe(fresh, prev.get("role"),
                            extra=_review_bounce_extra(fresh, node, reason, cur or None), resume=True)
            else:
                D.set_status(req_id, "later")
                with D.state_lock:   # C3：转暂缓即退出本轮评审周期，升级记录与各轮要点一并清
                    D.state.get("review_escalation", {}).pop(str(req_id), None)
                    D.state.get("review_round_points", {}).pop(str(req_id), None)
                D.save_state()
                D.notify(f"需求 #{req_id} 当前节点已变化，已置为暂缓等待人工处理。")
            return
        elif pend.get("flow_id"):
            # #419：前置槽位流程确认被否决 → 转人工处理（不重复拉起前置环节）
            D.set_status(req_id, "later")
            D.append_summary(req_id, f"人工否决流程推荐（{pend['flow_id']}）：{reason or '未说明'}（问题：{pend['question']}）")
            D.notify(f"需求 #{req_id} 否决了流程推荐，已置为暂缓等待人工处理。")
            D.post_dispatcher_event(req_id, "人工否决流程推荐，需求转暂缓", kind="phase")
        else:
            D.set_status(req_id, "open")
            D.append_summary(req_id, f"用户否决方案：{reason or '未说明'}（问题：{pend['question']}）")
            D.notify(f"需求 #{req_id} 已否决并退回待开发。")
        return
    if kind == "route":
        mode = pend.get("suggested_mode") or "auto"
        if mode == "manual_close":
            # #399：analysis 建议人工收口（实现已人工提交/验证记录类）——人工 y 直接收口，不经 set_req/spawn
            D.append_summary(req_id, f"人工确认收口：{reason or '未说明'}（analysis 建议人工收口）。")
            D.post_dispatcher_event(req_id, "人工确认收口，需求完成", kind="phase")
            D.finalize_done(req)
            D.notify(f"需求 #{req_id} 已确认人工收口，需求完成。")
            return
        # #425：流程名白名单配置派生（flows.json 启用流程），不再维护硬编码清单
        flow = default_flow_id()
        flow_hint = ""
        if mode == "semi" and reason:
            words = reason.split()          # 兼容纯空白 reason，不新增 IndexError 路径
            if words:
                word = words[0].strip().lower()
                cfg_flow = D.flow_by_id(word)
                if cfg_flow and D.flow_enabled(word):
                    flow = word
                else:
                    why = "未启用" if cfg_flow else "未识别"
                    flow_hint = f"（{why}流程「{word}」，按{D.FLOW_NAMES.get(flow, flow)}流程处理）"
        if mode != "semi":
            flow = None
        fields = {"mode": mode, "status": "open", "phase": None}
        if flow:
            fields["flow"] = flow
        D.set_req(req_id, fields)
        D.clear_retries(req_id)
        D.clear_flow_rounds(req_id)
        fresh = D.fetch_req(req_id) or req
        channel = '自动' if mode == 'auto' else f'半自动·{D.FLOW_NAMES.get(flow, flow)}流程'
        D.post_dispatcher_event(req_id, f"用户确认走{channel}通道，进入开发", kind="phase")
        D.notify(f"需求 #{req_id} 已确认走{channel}通道。{flow_hint}")
        # #303 防复发：确认继续时把清单外仓库警示注入 dev 会话（worktree 内无这些仓库代码，
        # 确有需要报 need_confirm，禁止越界写主工作区）
        extra = ""
        outside = pend.get("outside") or []
        if outside:
            extra = tr(prompt_lang(fresh.get("project")),
                       "注意：analysis 识别本需求涉及清单外仓库 {repos}，本项目 worktree 内没有这些仓库的代码。"
                       "如确认需改这些仓库：禁止写任何真实仓库主工作区，结束回合报 need_confirm 说明"
                       "「需求可能属于其它项目/项目仓库清单缺配置」等待人工处理。", repos=", ".join(outside))
        D._spawn_safe(fresh, D.role_for(fresh), extra=extra)
    elif kind == "review":
        phase = pend.get("phase")
        if phase == "accept":
            D.append_summary(req_id, "人工验收通过，需求完成。")
            D.finalize_done(req)
            D.post_dispatcher_event(req_id, "人工验收通过，需求完成", kind="phase")
        else:
            nxt = D.next_stage(req, phase)
            if nxt:
                D.set_req(req_id, {"status": "doing", "phase": nxt})
                D.clear_retries(req_id)
                D.clear_flow_rounds(req_id)
                fresh = D.fetch_req(req_id) or req
                D.post_dispatcher_event(req_id,
                                      f"「{D.ROLE_NAMES.get(phase, phase)}」评审通过，进入「{D.ROLE_NAMES.get(nxt, nxt)}」",
                                      kind="phase")
                D.notify(f"需求 #{req_id} 评审通过（{D.ROLE_NAMES.get(phase, phase)} 阶段），进入 {D.ROLE_NAMES.get(nxt, nxt)} 阶段。")
                D._spawn_safe(fresh, nxt)
            else:
                D.notify(f"需求 #{req_id} 评审通过，但阶段 {D.ROLE_NAMES.get(phase, phase)} 无下一角色。")
    elif pend.get("kind") == "review_gate":
        # #337：审查发现问题，人工确认仍合并——审查发现红字留档后放行 finalize_done
        with D.state_lock:
            pmr = D.state.get("pending_merge_review", {}).pop(str(req_id), None)
        D.save_state()
        D.append_summary(req_id, f"⚠️ 代码审查发现问题，人工确认仍合并：{pend['question']}")
        D.post_dispatcher_event(req_id, "审查发现问题，人工确认仍合并（发现已留档）", kind="merge")
        D.finalize_done(req, summary=(pmr or {}).get("summary"))
        D.notify(f"需求 #{req_id} 审查发现的问题已红字留档，人工确认放行合并。")
    elif pend.get("kind") == "confirm_override":
        # #399：人工确认豁免——红字留档后 finalize_done 统一收口（合并/冲突/#380 重启判定照常，不跳过）
        D.append_summary(req_id, f"⚠️ 机制校验未过，人工确认豁免：{pend['question']}")
        if pend.get("todos") is not None:
            with D.state_lock:
                D.state.setdefault("req_todos", {})[str(req_id)] = pend.get("todos")
            D.save_state()
        D.post_dispatcher_event(req_id, "收尾校验未过，人工确认豁免收口需求", kind="merge")
        D.finalize_done(req, summary=pend.get("summary"))
        D.notify(f"需求 #{req_id} 收尾校验未过，经人工确认豁免，需求已完成。")
    elif pend.get("kind") == "merge":
        # #106：改动文件清单在合并前捕获（--no-ff 合并后三点 diff 恒空）；
        # 用户解决冲突的提交发生在重试前，已包含在捕获范围内
        with D.state_lock:
            rec = D.state.get("worktrees", {}).get(str(req_id))
        touched = bool(rec) and D._touches_dispatcher(req, rec["branch"])
        merge_files = (D._collect_merge_changed_paths(req, rec["branch"]) if rec
                       and D.PROJECTS.get(req.get("project") or "", {}).get("auto_restart_after_merge")
                       else None)
        ok, failed = D.merge_dev_branches(req)
        if ok:
            D.cleanup_dev_worktrees(req_id, delete_branch=True)
            # #300/#394：人工合并重试成功同样按开关触发重启判定（与 finalize_done 保持一致）
            restart_failed, restart_ok, restart_warn = D._maybe_auto_restart_after_merge(
                req, merge_files, req_id)
            D._set_done(req, dispatcher_touched=touched, restart_failed=restart_failed,
                      restart_ok=restart_ok, restart_warn=restart_warn)
            D.notify(f"需求 #{req_id} 合并重试成功，已完成。")
            D.post_dispatcher_event(req_id, "人工处理合并成功，清理 worktree，需求完成", kind="merge")
            D.merge_conflict_clear(req_id, "飞书 y 重试成功")
        else:
            entry = {"req": req, "question": pend["question"], "kind": "merge"}
            with D.state_lock:
                ok = D._put_pending_confirm(str(req_id), entry)
            D.save_state()
            if not ok:
                D._pending_source_mismatch_note(str(req_id), entry, "合并重试仍冲突重挂")
            guard = D._merge_guard_blockers(req, failed)
            fail_text = (f"被主干守卫拦下（{D._branch_guard_text(guard)}），未执行合并"
                         if guard else D._merge_fail_text(failed, D._merge_dirty_blockers(req, failed)))
            D.notify(f"需求 #{req_id} 合并{fail_text}，请继续处理（解决后回复 y 重试）。")
            D.post_dispatcher_event(req_id, f"合并{fail_text}，继续待人工处理", kind="merge")
            D.merge_conflict_write(req_id)  # 重置网页按钮等待再次点击
    else:
        if pend.get("flow_id"):
            # #419：前置槽位流程确认（confirm 项目 / 清单外仓库把关）——y 采纳推荐流程并推进
            f = D.flow_by_id(pend["flow_id"])
            if D._adopt_flow_and_spawn(req, pend["flow_id"]):
                D.post_dispatcher_event(req_id,
                                      f"采纳推荐流程 {pend['flow_id']}（{f['name'] if f else ''}）"
                                      "｜来源：人工确认",
                                      kind="flow_choice")
                D.notify(f"需求 #{req_id} 已采纳推荐流程「{f['name'] if f else pend['flow_id']}」并继续推进。")
            else:
                D.notify(f"需求 #{req_id} 采纳流程失败（快照写入未生效），请人工处理。")
            return
        if pend.get("review_index") is not None:
            # #419：评审轮次上限人工裁决——y 放行进入下一节点
            node = D.current_node(req)
            if node is not None and (node.get("node_type") or "") == "review":
                with D.state_lock:
                    D.state.get("flow_review_rounds", {}).get(str(req_id), {}).pop(str(pend["review_index"]), None)
                _fid_y, nodes_y, idx_y = D.flow_snapshot(req)
                prev_y = nodes_y[idx_y - 1] if nodes_y and idx_y > 0 else None
                D._clear_review_escalation(req_id, str(pend["review_index"]),
                                         prev_y.get("role") if prev_y else None)
                D.save_state()
                D.append_summary(req_id, f"人工裁决放行（{D.node_display(node)}）：{reason or '同意'}。")
                D.post_dispatcher_event(req_id, f"人工裁决放行「{D.node_display(node)}」，进入下一节点", kind="phase")
                D._advance_after_node(req, node)
            else:
                D.notify(f"需求 #{req_id} 当前节点已变化，未执行放行，请在看板确认现状。")
            return
        # 普通提问的答复留档到 summary（prompt 通道）：会话重开/重试后的新 agent 也能看到这条
        # 澄清。上面两条（前置流程确认 / 评审超限裁决）各有自己的留档分支，不重复写。
        D.append_summary(req_id, D.confirm_answer_summary(pend.get("question"), reason))
        D.spawn_agent(req, extra=tr(prompt_lang(req.get("project")), "用户对之前的问题「{question}」的答复：同意。{answer}",
                                    question=pend["question"], answer=reason), resume=True)
        role = D.role_for(req)
        D.notify(f"已收到，需求 #{req_id} 继续{D.ROLE_NAMES.get(role, role) or '收尾'}阶段。")

# 未安装 lark_oapi 时基类退化为 object（NO_FEISHU 恒真，start_feishu_ws 不会被调用）
class _FeishuStatusClient(D.lark.ws.Client if D.LARK_AVAILABLE else object):
    """#23 长连接状态观测：包装 `_connect()` 上报首连成功/失败，挂 SDK 公开
    `on_reconnecting`/`on_reconnected` 上报重连中/已恢复。只把 `_conn` 用于区分「早退」
    与「本次真的连上」，不作业务判据；上报动作自身不抛（回调异常不得影响 SDK 重连）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_reconnecting = lambda: _update_feishu_status(
            connected=False, phase="reconnecting",
            last_error="长连接已断开，SDK 正在重连")
        self.on_reconnected = lambda: _update_feishu_status(
            connected=True, phase="connected", last_error=None,
            connected_at=time.time())

    async def _connect(self):
        # 评审打回级修复：**不进入** SDK 的「持锁早退」分支。
        # SDK `lark_oapi/ws/client.py` `_connect()` 在 `await self._lock.acquire()` 之后若
        # `_conn is not None` 直接 `return`——不经过 `finally: self._lock.release()`，锁被永久持有；
        # 之后 `_disconnect()`/`_write_message()` 的 `acquire` 会永久阻塞（长连接线程卡死、
        # 心跳停在 connecting）。这里在调用父类前先复现该早退语义（唯一效果就是 return），
        # 既保留「重连复用已有连接」的行为，又保证泄漏分支永不被走到。
        if getattr(self, "_conn", None) is not None:
            return
        try:
            await super()._connect()
        except Exception as e:  # noqa: BLE001 - 先上报原因再原样抛出，SDK 行为逐字不变
            _update_feishu_status(connected=False, phase="failed",
                                  last_error=D._sanitize_feishu_error(e))
            raise
        _update_feishu_status(connected=True, phase="connected",
                              last_error=None, connected_at=time.time())


def _feishu_connect_watchdog(timeout=None):
    """#23 首连超时兜底：长连接建立失败的原因必须能写进心跳（界面「连接失败：原因」可达）。
    SDK 内部异常路径（持锁早退 / `_disconnect` 阻塞等）可能让 `start()` 永不返回，此线程兜底：
    超时后若状态仍停在 `connecting`，改写为 `failed` + 可读原因；已迁移（connected/failed/
    reconnecting）则不动，超时窗口内成功连接不受影响。"""
    timeout = D.FEISHU_CONNECT_TIMEOUT_SEC if timeout is None else timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        st = (D.load_heartbeat() or {}).get("feishu")
        phase = st.get("phase") if isinstance(st, dict) else None
        if phase and phase != "connecting":
            return
    st = (D.load_heartbeat() or {}).get("feishu")
    if isinstance(st, dict) and st.get("phase") == "connecting":
        _update_feishu_status(
            connected=False, phase="failed",
            last_error=f"飞书长连接在 {timeout}s 内未建立（SDK 未返回结果，线程疑似卡住）")


def start_feishu_ws(app_id, app_secret):
    handler = (D.lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .build())
    client = D._FeishuStatusClient(app_id, app_secret, event_handler=handler,
                                 log_level=D.lark.LogLevel.WARNING)
    threading.Thread(target=_feishu_connect_watchdog, daemon=True).start()
    try:
        client.start()  # 阻塞，自动重连
    except Exception as e:  # noqa: BLE001 - 线程异常不静默：写心跳 failed 后线程正常退出
        err = D._sanitize_feishu_error(e)
        log.error(f"网络连接失败：飞书长连接线程退出（{err}）")
        _update_feishu_status(connected=False, phase="failed", last_error=err)

