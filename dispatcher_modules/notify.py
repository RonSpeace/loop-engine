"""通知渠道与消息卡片：注册表（飞书 / Webhook / Slack）、文本分片发送、需求卡片与列表卡片渲染。"""

import json
import re
from pathlib import Path

import notifiers
from notifiers.feishu import FeishuNotifier

from dispatcher_modules.runtime import D, log


# 注册表单例：飞书为同步渠道（启动即注册，未启用时其发送函数自行只记日志）；Webhook/Slack 由
# notifiers.json（与 feishu.json 同目录，REQ_BOARD_NOTIFIERS_JSON 可覆盖）驱动，文件变化自动重载、
# 后台线程发送、配置非法只停用该渠道（fail-closed，不崩调度器）。
NOTIFIERS_CONFIG_FILE = notifiers.config_path(D.REPO_DIR)
NOTIFIERS = notifiers.NotifierRegistry(
    config_file=NOTIFIERS_CONFIG_FILE,
    board_url=D.BOARD_ROOT_API.rsplit("/api", 1)[0] if "/api" in D.BOARD_ROOT_API else D.BOARD_ROOT_API,
    logger=log)
NOTIFIERS.register(FeishuNotifier(
    send_text=lambda text: _feishu_notify_text(text),
    send_card=lambda event, req, **kw: _feishu_notify_req_card(event, req, **kw)))


def _send_feishu(msg_type, content_obj, chat_id=None):
    """底层通用发送出口（text/interactive 共用）：chat_id 缺省仍取 state["chat_id"]（y/n 通道逐字不变）；
    飞书建卡按会话显式传 chat_id。未启用（feishu None）/未连接仅 log.info 记录返回 False；
    已连接发送失败 log.error（含 resp.code/msg）返回 False；成功返回 True"""
    if not D.feishu:
        log.info(f"[飞书未启用/未连接，消息仅记录] {msg_type}: {json.dumps(content_obj, ensure_ascii=False)[:200]}")
        return False
    chat_id = chat_id or D.state.get("chat_id")
    if not chat_id:
        log.info(f"[飞书未连接，消息仅记录] {msg_type}: {json.dumps(content_obj, ensure_ascii=False)[:200]}")
        return False
    req = (D.CreateMessageRequest.builder()
           .receive_id_type("chat_id")
           .request_body(D.CreateMessageRequestBody.builder()
                         .receive_id(chat_id)
                         .msg_type(msg_type)
                         .content(json.dumps(content_obj, ensure_ascii=False))
                         .build())
           .build())
    resp = D.feishu.im.v1.message.create(req)
    if not resp.success():
        log.error(f"飞书发送失败: {resp.code} {resp.msg}")
        return False
    return True

def notify(text, chat_id=None):
    """给用户发文本消息（回执类）；chat_id 缺省取 state["chat_id"]（y/n 通道不变）。
    未建立会话/未启用飞书时只记日志。内部收敛至 _send_feishu。
    显式 chat_id = 飞书会话内回复，只回飞书；缺省 = 经通知注册表扇出：飞书逐字照旧同步发送，
    Webhook/Slack 等配置渠道进后台队列（飞书入站处理期间的回执只回飞书，见 on_message）。"""
    if chat_id:
        D._send_feishu("text", {"text": text}, chat_id)
    else:
        NOTIFIERS.send_text(text)


def _feishu_notify_text(text):
    """飞书渠道的文本出口（注册表回调）：与接入注册表前的 notify 缺省分支逐字一致。"""
    D._send_feishu("text", {"text": text})

def _feishu_notify(chat_id, text):
    """飞书建卡按会话回发（显式 chat_id）；测试可 mock notify 统一捕获。"""
    D.notify(text, chat_id=chat_id)

def _feishu_typing(message_id, reaction_id=None):
    """#44「正在输入」反馈：飞书无原生 typing 接口，用用户消息上的 Typing 表情代替。
    reaction_id 为空 → create（成功返回 reaction_id）；有 → delete。失败只 log.info（不升级为故障、
    不重试、不做保活重贴）；feishu 未启用或缺 message_id 直接返回 None。
    create 成功但响应取不到 reaction_id → 返回 None 并在日志注明表情可能残留。"""
    if D.feishu is None or not message_id:
        return None
    if reaction_id:
        try:
            req = (D.DeleteMessageReactionRequest.builder()
                   .message_id(message_id)
                   .reaction_id(reaction_id)
                   .build())
            resp = D.feishu.im.v1.message_reaction.delete(req)
            if not resp.success():
                log.info(f"飞书 Typing 表情操作失败（{message_id}）：{resp.code} {resp.msg}")
        except Exception as e:  # noqa: BLE001 - 表情失败不影响业务回复
            log.info(f"飞书 Typing 表情操作失败（{message_id}）：{e}")
        return None
    try:
        body = (D.CreateMessageReactionRequestBody.builder()
                .reaction_type(D.Emoji.builder().emoji_type("Typing").build())
                .build())
        req = (D.CreateMessageReactionRequest.builder()
               .message_id(message_id)
               .request_body(body)
               .build())
        resp = D.feishu.im.v1.message_reaction.create(req)
        if not resp.success():
            log.info(f"飞书 Typing 表情操作失败（{message_id}）：{resp.code} {resp.msg}")
            return None
        rid = (getattr(resp, "data", None) and getattr(resp.data, "reaction_id", None)) or None
        if not rid:
            log.info(f"飞书 Typing 表情已贴但未取到 reaction_id（{message_id}）：表情可能残留在用户消息上")
        return rid
    except Exception as e:  # noqa: BLE001 - 表情失败不影响业务回复
        log.info(f"飞书 Typing 表情操作失败（{message_id}）：{e}")
        return None

def send_text_chunked(chat_id, text, chunk_size=None, max_chunks=None):
    """#22 D3：agent 长回复分段发回飞书（默认 2000 字/条、最多 5 条）；超出末条追加
    「剩余内容见看板」。返回实际发送条数。"""
    chunk_size = chunk_size or D.FEISHU_TEXT_CHUNK_SIZE
    max_chunks = max_chunks or D.FEISHU_TEXT_MAX_CHUNKS
    s = "" if text is None else str(text)
    chunks = [s[i:i + chunk_size] for i in range(0, len(s), chunk_size)] or [""]
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        chunks[-1] = chunks[-1] + "…（剩余内容见看板）"
    for c in chunks:
        D._send_feishu("text", {"text": c}, chat_id)
    return len(chunks)

def esc_md(s):
    """lark_md 特殊字符转义：& < > → &amp; &lt; &gt;（对齐 automation check-bpm-pending-approvals.js 的 escMd）"""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def truncate(s, limit):
    """超长截断：>limit 截断并追加 …；==limit 不截断。先截断后转义（调用方先 truncate 再 esc_md）"""
    s = "" if s is None else str(s)
    if len(s) <= limit:
        return s
    return s[:limit] + "…"

def artifact_short(req, filename=None):
    """产物短路径（手机端可读）：相对 ~/.req-board/artifacts/ 根（如 req-board/req-115/analysis.md）；
    产物目录越界（如测试临时目录）退化为 req-<id>；禁止出现本地绝对长路径"""
    try:
        rel = D.artifact_dir(req).relative_to(D.ARTIFACTS_ROOT).as_posix()
    except ValueError:
        rel = f"req-{req['id']}"
    if filename:
        return f"{rel}/{filename}"
    return rel

def _path_short(p):
    """本地绝对路径降级展示：artifacts 根内 → 相对短路径；根外原样保留"""
    try:
        return Path(p).relative_to(D.ARTIFACTS_ROOT).as_posix()
    except (TypeError, ValueError):
        return str(p or "")

def _md_field(name, value):
    """双栏 is_short 字段（lark_md，值做转义）"""
    return {"is_short": True, "text": {"tag": "lark_md", "content": f"**{name}**\n{esc_md(value)}"}}


def _card_paragraphs(text, limit):
    """把卡片正文长文本切成飞书友好的段落列表（每段后续渲染为独立 div）：
    - 先按 limit 截断（沿用 #115 先截断后转义约定），再按 \n 拆段、跳过空行；
    - 行首 markdown 无序列表标记（`- `/`* `/`+ ` 后跟空白）替换为 `• `——lark_md
      不支持列表语法，不替换会原样显示且多段在卡片里挤成一行；
    - 有序编号（1. / 1) 等）原样保留。
    """
    paragraphs = []
    for line in (truncate(text or "", limit) or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        paragraphs.append(re.sub(r"^([-*+])\s+", "• ", line))
    return paragraphs


def _body_divs(title, text, limit):
    """标题与正文各占一个独立 div：lark_md 不支持块级元素，多段塞进同一 div
    会在飞书卡片中挤成一行。返回 [标题 div, 正文段落 div…]"""
    divs = [{"tag": "div", "text": {"tag": "lark_md", "content": f"**{esc_md(title)}**"}}]
    for paragraph in _card_paragraphs(text, limit):
        divs.append({"tag": "div", "text": {"tag": "lark_md", "content": esc_md(paragraph)}})
    return divs


def send_text_lossless(chat_id, text, chunk_size=None):
    """#30 无损文本发送：按 FEISHU_TEXT_CHUNK_SIZE 全量切片、**逐片检查成败**（不丢尾、不掩盖失败）。
    与 send_text_chunked（最多 5 片、超限丢尾、不看每片成败）语义不同，专供卡片降级路径；
    不改 send_text_chunked 既有语义（回归锚点，见 tests/test_feishu_chat_router.py::test_76_chunked_sends）。
    返回 (sent, failed)。"""
    chunk_size = chunk_size or D.FEISHU_TEXT_CHUNK_SIZE
    s = "" if text is None else str(text)
    chunks = [s[i:i + chunk_size] for i in range(0, len(s), chunk_size)] or [""]
    sent = failed = 0
    for idx, chunk in enumerate(chunks, 1):
        try:
            ok = D._send_feishu("text", {"text": chunk}, chat_id)
        except Exception as e:  # noqa: BLE001 - 发送异常不打断整轮，记录后继续
            log.error(f"飞书降级文本发送异常（第 {idx}/{len(chunks)} 片）：{e}")
            ok = False
        if ok:
            sent += 1
        else:
            failed += 1
            log.error(f"飞书降级文本发送失败（第 {idx}/{len(chunks)} 片，chat={chat_id}）")
    return sent, failed


def build_req_list_card(payload):
    """#30 需求卡清单卡片（纯函数）：页 spec → 飞书 interactive 卡片。
    每张需求一个独立 div（lark_md 不支持块级元素，多卡同 div 会挤成一行）；标题先 truncate 后 esc_md；
    items 缺失/非列表 → 视作空页（页脚「（无条目）」）**不抛**（发送端 fail-soft，避免打断整轮）。"""
    p = payload if isinstance(payload, dict) else {}
    project = p.get("project") or ""
    items = p.get("items")
    if not isinstance(items, list):
        items = []
    page = p.get("page") or 1
    pages = p.get("pages") or 1
    total = p.get("total_cards")
    total = len(items) if total is None else total
    shown = items[:D.ASSISTANT_CARD_MAX_ITEMS]
    elements = [{"tag": "div", "text": {"tag": "lark_md",
                                        "content": f"共 {total} 张 · 第 {page}/{pages} 页"}}]
    for it in shown:
        if not isinstance(it, dict):
            continue
        title = esc_md(truncate(it.get("title") or "", D.CARD_TITLE_LIMIT))
        label = it.get("status_label") or it.get("status") or ""
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": (
            f"**#{esc_md(it.get('id'))} {title}**\n"
            f"状态：{esc_md(label)} · 流程：{esc_md(it.get('flow') or '未指定')}"
            f" · 更新：{esc_md(it.get('updated_at') or '')}")}})
    if not items:
        footer = "（无条目）"
    else:
        lines = [f"共 {total} 张，第 {page}/{pages} 页"]
        if p.get("truncated"):
            lines.append(f"<font color='red'>⚠️ 还有 {p.get('remaining') or 0} 张未展示，"
                         "可加状态或关键词缩小范围</font>")
        if len(items) > len(shown):
            lines.append(f"（本页仅渲染前 {len(shown)} 张）")
        footer = "\n".join(lines)
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": footer}})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "blue",
                       "title": {"tag": "plain_text",
                                 "content": truncate(f"需求卡清单 · {project}（第 {page}/{pages} 页）",
                                                     D.CARD_TITLE_LIMIT)}},
            "elements": elements}


def requirement_list_fallback_text(payload):
    """#30 卡片发送失败时的**完整**降级文本：逐条覆盖该页全部条目（不丢卡）。"""
    p = payload if isinstance(payload, dict) else {}
    project = p.get("project") or ""
    items = p.get("items")
    if not isinstance(items, list):
        items = []
    page = p.get("page") or 1
    pages = p.get("pages") or 1
    total = p.get("total_cards")
    total = len(items) if total is None else total
    head = f"需求卡清单 · {project}（第 {page}/{pages} 页，共 {total} 张）"
    if not items:
        return head + "\n该项目当前没有符合条件的需求卡"
    lines = [head]
    for it in items:
        if not isinstance(it, dict):
            continue
        lines.append(f"#{it.get('id')} {it.get('title') or ''}"
                     f"｜{it.get('status_label') or it.get('status') or ''}"
                     f"｜{it.get('flow') or '未指定'}｜{it.get('updated_at') or ''}")
    if p.get("truncated"):
        lines.append(f"⚠️ 还有 {p.get('remaining') or 0} 张未展示，可按状态或关键词缩小范围后重发")
    return "\n".join(lines)


def _phase_name(phase):
    return D.ROLE_NAMES.get(phase, phase) if phase else ""

def build_req_card(event, req, **kw):
    """构造语义化需求卡片（纯函数）：事件 → {template 颜色, 标题}；统一结构 =
    [fields 双栏, 标题, 正文段落…（各独立 div，飞书卡片不支持块级 markdown）, hr, 尾部回复指引]。
    调用方显式传结构化数据，不从旧通知文本反向解析"""
    cfg = D.CARD_EVENTS[event]
    rid = req["id"]
    project = req.get("project") or ""
    title = truncate(req.get("title") or "", D.CARD_TITLE_LIMIT)
    if event == "route":
        suggested = kw.get("suggested") or "semi"
        fields = [_md_field("项目", project),
                  _md_field("建议通道", "自动" if suggested == "auto" else "半自动")]
        lines = [f"证据清单：{artifact_short(req, kw.get('artifact'))}"]
        # #303 防复发：涉及清单外仓库时红字警示（疑似建错项目/项目配置缺仓库），
        # font 标签硬编码在 esc_md 之外不转义（与 done 提醒行同款）
        outside = kw.get("outside") or []
        if outside:
            lines.append(f"<font color='red'>⚠️ 涉及清单外仓库：{esc_md(', '.join(outside))}"
                         "（本项目仓库清单不含这些仓库，疑似建错项目或项目配置缺仓库；"
                         "确认继续则仅按清单仓库开发）</font>")
        body_text = "\n".join(lines)
        footer = f"回复 y {rid} 确认，n {rid} <原因> 否决。"
    elif event == "review":
        fields = [_md_field("项目", project), _md_field("待评审阶段", _phase_name(kw.get("phase")))]
        body_text = f"产物目录：{_path_short(kw.get('artifact_dir'))}"
        footer = f"回复 y {rid} 通过，n {rid} <原因> 否决。"
    elif event == "need_confirm":
        fields = [_md_field("项目", project), _md_field("当前阶段", _phase_name(kw.get("phase")))]
        body_text = kw.get("question") or "（agent 未说明问题）"
        round_n = kw.get("round_n") or 0
        if round_n:
            # #400：追问护栏明示出口——轮次 ≤ 上限仅标注；超限追加出口提示（不自动转 later）
            if kw.get("over_limit"):
                body_text += (f"\n（已连续第 {round_n} 轮待确认：飞书 y 继续追问 / n 恢复待开发；"
                              "网页可点「停止追问转暂缓」）")
            else:
                body_text += f"\n（第 {round_n} 轮待确认）"
        footer = f"回复 y {rid} 同意，n {rid} <原因> 否决。"
    elif event == "done":
        company = bool(kw.get("company"))
        fields = [_md_field("项目", project),
                  _md_field("合并方式", "开发分支已自动 push 到远端（待发版合并）" if company else "开发分支已合并回 main")]
        body_text = kw.get("summary") or "需求已完成"
        lines = [f"产物：{esc_md(artifact_short(req, 'result.json'))}"]
        reminder = (kw.get("reminder") or "").strip().lstrip("⚠️").strip()
        if reminder:
            # 需人工操作的事项用红字突出（lark_md 支持 <font color='red'>）；仍位于「产物」行下方
            lines.append(f"<font color='red'>⚠️ {esc_md(truncate(reminder, D.CARD_BODY_LIMIT))}</font>")
        # #279：agent 声明的遗留待办逐条红字展示在卡片最底部（「产物」行下方、reminder 之后）；
        # 纯展示不可勾销；先截断后转义，<font> 标签硬编码不转义
        for todo in kw.get("todos") or []:
            lines.append(f"<font color='red'>⚠️ 待办：{esc_md(truncate(todo, D.CARD_BODY_LIMIT))}</font>")
        footer = "\n".join(lines)
    elif event == "failed":
        fields = [_md_field("项目", project), _md_field("失败次数", f"{kw.get('retries') or 0} 次")]
        body_text = kw.get("reason") or ""
        footer = "请人工处理后把状态改回 open 恢复调度。"
    elif event == "blocked":
        fields = [_md_field("项目", project), _md_field("当前阶段", _phase_name(kw.get("phase")))]
        body_text = kw.get("reason") or ""
        footer = "已转 later 待人工处理，可在看板需求助手续跑；处理后把状态改回 open 恢复调度。"
    else:  # merge_conflict
        failed = kw.get("failed") or []
        fields = [_md_field("项目", project),
                  _md_field("冲突仓库", ", ".join(str(f) for f in failed))]
        body_text = f"已保留分支 {kw.get('branch') or ''} 与 worktree"
        footer = f"请解决后回复 y {rid} 重试，或 n {rid} 人工接管。"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": cfg["template"],
                   "title": {"tag": "plain_text", "content": f"需求 #{rid} {cfg['title']}"}},
        "elements": [
            {"tag": "div", "fields": fields},
            *_body_divs(title, body_text, D.CARD_BODY_LIMIT),
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": footer}},
        ],
    }

def fallback_text(event, req, **kw):
    """七类事件降级文本：逐字复用升级前 notify 文案，保证卡片发送失败降级后信息与升级前一致"""
    rid = str(req["id"])
    title = req.get("title") or ""
    if event == "route":
        suggested = kw.get("suggested") or "semi"
        outside = kw.get("outside") or []
        outside_line = ""
        if outside:
            outside_line = (f"\n⚠️ 涉及清单外仓库：{', '.join(outside)}"
                            "（本项目仓库清单不含这些仓库，疑似建错项目或项目配置缺仓库）")
        return (f"🧭 需求 #{rid}「{title}」{D.ROLE_NAMES.get('analysis', 'analysis')} 建议通道：{suggested}{outside_line}\n"
                f"证据清单：{D.artifact_dir(req) / (kw.get('artifact') or 'analysis.md')}\n\n"
                f"回复 y {rid} 确认，n {rid} <原因> 否决。")
    if event == "review":
        return (f"📋 需求 #{rid}「{title}」{_phase_name(kw.get('phase'))} 阶段产物待评审：\n"
                f"{D.artifact_dir(req)}\n\n回复 y {rid} 通过，n {rid} <原因> 否决。")
    if event == "need_confirm":
        question = kw.get("question") or "（agent 未说明问题）"
        round_n = kw.get("round_n") or 0
        if round_n:
            if kw.get("over_limit"):
                question += ("\n（已连续第 %d 轮待确认：飞书 y 继续追问 / n 恢复待开发；"
                             "网页可点「停止追问转暂缓」）" % round_n)
            else:
                question += f"\n（第 {round_n} 轮待确认）"
        return f"❓ 需求 #{rid}「{title}」需要确认：\n{question}\n\n回复 y {rid} 同意，n {rid} <原因> 否决。"
    if event == "blocked":
        return (f"⏸ 需求 #{rid}「{title}」已阻塞（{_phase_name(kw.get('phase'))} 阶段）。\n"
                f"阻塞原因：{kw.get('reason') or ''}\n"
                f"已转 later 待人工处理，可在看板需求助手续跑。")
    if event == "done":
        reminder = kw.get("reminder") or ""
        # #279：agent 声明的遗留待办逐条拼入降级文本（卡片发送失败也不丢失待办信息）
        todos_text = "".join(f"\n⚠️ 待办：{t}" for t in (kw.get("todos") or []))
        if kw.get("company"):
            return f"✅ 需求 #{rid} 已完成：{title}（开发分支已自动 push 到远端，待发版合并）{reminder}{todos_text}"
        return f"✅ 需求 #{rid} 已完成：{title}{reminder}{todos_text}"
    if event == "failed":
        return (f"❌ 需求 #{rid}「{title}」自动开发失败 {kw.get('retries') or 0} 次，已停止自动调度"
                f"（卡片转 later，等待人工重新开放）。\n最后原因：{kw.get('reason') or ''}\n"
                f"请人工处理后把状态改回 open 恢复调度。")
    failed = ", ".join(str(f) for f in (kw.get("failed") or []))
    return (f"⚠️ 需求 #{rid}「{title}」自动合并冲突（{failed}）。\n"
            f"已保留分支 {kw.get('branch') or ''} 与 worktree，"
            f"请解决后回复 y {rid} 重试，或 n {rid} 人工接管。")

def notify_req_card(event, req, **kw):
    """语义化需求卡片通知（七类关键状态）：经通知注册表扇出——飞书走 _feishu_notify_req_card
    （逐字照旧），Webhook/Slack 等配置渠道收到同一事件的结构化版本（后台发送，不阻塞）。"""
    NOTIFIERS.send_event(event, req, title=D.CARD_EVENTS[event]["title"],
                         details=_notifier_details(kw), raw={"req": req, "kw": kw})


def _notifier_details(kw):
    """配置渠道的事件细节：阶段 id → 显示名（flows.json 派生），其余原样（注册表再做 JSON 安全化）。"""
    out = dict(kw)
    if out.get("phase"):
        out["phase"] = _phase_name(out["phase"])
    return out


def _feishu_notify_req_card(event, req, **kw):
    """飞书需求卡片（注册表回调）：构造卡片 → _send_feishu("interactive")；
    成功不重复发送；已连接但发送失败 → 降级 notify(fallback_text)（底层已 log.error）；
    未连接仅记日志不降级（沿用现有行为）"""
    card = build_req_card(event, req, **kw)
    if D._send_feishu("interactive", card):
        return
    if not D.feishu or not D.state.get("chat_id"):
        return
    # 降级文本只补发飞书：配置渠道已收到同一事件的结构化版本，不再重复扇出
    with NOTIFIERS.inbound_scope():
        D.notify(fallback_text(event, req, **kw))

def _feishu_send_assistant_card(chat_id, content):
    """#30 发送一页需求卡清单：解析页 spec → 构造卡片 → **显式 chat_id** 直发；
    失败降级为该页**完整**文本（send_text_lossless，不丢尾）。
    - 解析/结构非法 → log.error + 明确文本提示（fail-closed，不静默）；未连接时不发任何消息。
    - 绝不复用 notify_req_card / state["chat_id"]（那是 y/n 通道，会错投）。"""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        log.error(f"助手卡片事件数据异常（chat={chat_id}）：{str(content)[:200]}")
        if D.feishu:
            D.send_text_lossless(chat_id, "需求卡清单有内容发送异常，未展示（可重发一次）。")
        return False
    tag = f"第 {payload.get('page')}/{payload.get('pages')} 页"
    if D._send_feishu("interactive", build_req_list_card(payload), chat_id=chat_id):
        log.info(f"已发送需求卡清单卡片（chat={chat_id} {tag}）")
        return True
    if not D.feishu:
        log.info(f"飞书未连接，需求卡清单卡片仅记录（chat={chat_id} {tag}）")
        return False
    log.error(f"需求卡清单卡片发送失败，降级为该页完整文本（chat={chat_id} {tag}）")
    D.send_text_lossless(chat_id, requirement_list_fallback_text(payload))
    return False


