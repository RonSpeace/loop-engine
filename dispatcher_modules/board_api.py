"""看板 API 客户端：需求读写、会话与事件上报、运行记录。"""

import json
import re
import time
import urllib.request

from acp_shared import KIMI, REDLINE_CANCEL_SIGNAL, pct_value
from storage_time import parse_storage_time

from dispatcher_modules.runtime import D, log, subprocess


# 纯本机回环请求，显式禁用代理：避免全局 opener 缓存的系统代理设置失效后导致 Connection refused
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(method, path, body=None):
    req = urllib.request.Request(
        D.BOARD_API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Actor": "dispatcher"},
    )
    with _opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())


def api_board(method, path, body=None):
    """看板顶层 API（agent_sessions / manual_commands 等），纯本机回环，失败由调用方容错。
    #431：带 X-Actor: dispatcher —— 看板据此把调度器自建的注册行 origin 落为 dispatcher"""
    req = urllib.request.Request(
        D.BOARD_ROOT_API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Actor": "dispatcher"},
    )
    with _opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())

def _parse_ts(s):
    """看板时间戳 'YYYY-MM-DD HH:MM:SS'（存储时区）-> epoch；解析失败返回 0（视为足够旧，可捞取）
    注意：失败返回 0 的宽松语义只适用于「捞取」判断，删除路径不得复用（见 _session_age_sec）。"""
    try:
        return parse_storage_time(s or "").timestamp()
    except Exception:
        return 0


def _session_age_sec(created_at):
    """#431：注册行 created_at（看板存储时区字符串）-> 距今秒数。
    缺失/非法/空返回 None —— 调用方（空会话清理）必须按 fail-closed 跳过，不得复用 _parse_ts
    （其失败返回 0 = 视为足够旧，用于删除会误删刚建的行）。"""
    try:
        ts = parse_storage_time(created_at or "").timestamp()
    except Exception:
        return None
    return time.time() - ts

def get_open_reqs(project):
    """open 需求中已过静默期（updated_at 距今 >= MIN_AGE_MINUTES）的才会被捞取。
    看板编辑弹窗每次自动保存都会刷新 updated_at，因此"还在写"的需求不会被扫走。"""
    cutoff = time.time() - D.MIN_AGE_MINUTES * 60
    return [r for r in D.api("GET", f"?project={project}&status=open")
            if _parse_ts(r.get("updated_at")) <= cutoff]

def get_doing_reqs(project):
    return D.api("GET", f"?project={project}&status=doing")

def fetch_req(req_id):
    """按 id 拉取最新需求；兼容字符串/整数入参（int 比较，避免类型不匹配静默漏配）"""
    for r in D.api("GET", ""):
        if int(r["id"]) == int(req_id):
            return r
    return None

def set_status(req_id, status):
    """状态变更（api PATCH 薄封装）。#400·v2：终态（done/later）清 confirm_rounds 键——
    需求 done/later 后人工重开是新周期，追问护栏从头计数（「响应不清零」语义保留在周期内）。
    #412（S1）：pop 取值在锁内、save_state 移锁外——旧实现在持 state_lock 时调 save_state
    （内部再次取非重入 state_lock）恒死锁（#410 现场根因：done/later 且 confirm_rounds 有键）。"""
    if status in ("done", "later"):
        with D.state_lock:
            popped = D.state.get("confirm_rounds", {}).pop(str(req_id), None)
        if popped is not None:
            D.save_state()
    return D.api("PATCH", f"/{req_id}", {"status": status})

def set_req(req_id, fields):
    return D.api("PATCH", f"/{req_id}", fields)

def append_summary(req_id, line):
    """往需求的 summary（开发总结）字段追加一行 [auto] 记录"""
    r = D.fetch_req(req_id)
    if r:
        summary = ((r.get("summary") or "") + f"\n[auto] {line}").strip()
        D.api("PATCH", f"/{req_id}", {"summary": summary})


# C4 summary 进 prompt 的折叠口径：保留末尾 N 条 [auto] 记录全文，更早的折成一行摘要
SUMMARY_PROMPT_KEEP = 6


def compact_summary(summary, keep=None):
    """C4：summary（prompt 通道）进 prompt 前折叠早期记录，防打回轮次越多 prompt 越长。
    按 [auto] 记录分组（多行记录整体保留）；首段人工前言与含「人工」的记录（答复/裁决/否决）
    永不折叠——它们是已澄清的事实，折掉会让 agent 重复追问；其余只留末尾 keep 条。
    人工裁决类记录有「人工…」与「用户…」（用户否决方案/评审/豁免等）两种写法，均按人工记录钉住。"""
    text = (summary or "").strip()
    keep = SUMMARY_PROMPT_KEEP if keep is None else keep
    preface, entries = [], []
    for line in text.splitlines():
        if line.startswith("[auto] "):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            preface.append(line)
    if len(entries) <= keep:
        return text
    older, recent = entries[:-keep], entries[-keep:]
    human = lambda e: "人工" in e[0] or e[0].startswith("[auto] 用户")
    pinned = [e for e in older if human(e)]
    folded = [e for e in older if not human(e)]
    if not folded:
        return text
    rejects = sum(1 for e in folded if "未通过" in e[0])
    digest = f"（更早 {len(folded)} 条记录已折叠，其中评审打回 {rejects} 次；完整历史见看板卡片开发总结）"
    out = preface + [digest] + [ln for e in pinned + recent for ln in e]
    return "\n".join(out).strip()


def confirm_answer_summary(question, answer, limit=120):
    """人工答复 need_confirm 的留档文本（写入 summary → summary 是 prompt 通道，
    会话重开/重试后的新 agent 也能看到这条澄清，不再重复追问）。

    与另几条人工分支（裁决放行 / 否决 / 豁免）的留档口径一致——原先只有「普通提问的答复」
    漏了留档：答复只进会话上下文，会话一重开就丢。问题文本压成单行并截断到 limit 字。"""
    q = " ".join(str(question or "").split())
    if len(q) > limit:
        q = q[:limit] + "…"
    return f"人工答复（问题：{q}）：{answer or '同意'}。"


# 单行超过该长度才考虑按要点拆分（短行/短原因不拆，宁缺毋滥）
SUMMARY_LONG_LINE_MIN = 120
# 拆后行数上限（含引导行），超限时超出部分并入最后一段（不丢原文）
SUMMARY_MAX_POINTS = 20

# 内联编号标记：1) 1） 1、 1. （英文句点必须后跟空白，防命中 2.5/1.2.3）；
# 前缀白名单=段首/空白/句读标点——编号只出现在条目边界；值内数字一律不算编号：
# rgb(240,253,244) 的 `244)`、OK(skipped=17) 的 `17)`、PORT=8901) 的 `8901)` 前字符是
# `,`/`(`/`=`，误判会把内容切开并重新编号（#428 曾把验收色值切成 `1. /(21,128,`）
_NUM_MARK_RE = re.compile(r"(?:^|(?<=[\s：:；;。！？!?]))(\d{1,3})([)）、]|\.\s)")
# 断句标点：中文 。；！？ 任意位置可断；英文 . ; ! ? 仅后跟空白或行尾时断
# （防 transition .2s、README.md、REQ_BOARD_PORT=8901 误拆）
_SENT_SPLIT_RE = re.compile(r"[。；！？]|[.;!?](?=\s|$)")


def format_summary_points(text):
    """把 agent 提供的 summary/原因长文本按要点分行（`- ` 列表 / `1. ` 编号），返回格式化字符串。

    规则（宁缺毋滥，任何输入都不丢原文）：
    - None/空白 → ""；短单行/短段（≤ SUMMARY_LONG_LINE_MIN）→ 原样返回；
    - 多行文本按 \n 逐段独立处理：空行原样保留、段首尾空白 strip；
    - 超长段含 ≥2 个内联编号（1) 1、 1. 等）→ 拆为引导行 + `1. ` 重新编号行；
    - 超长段无编号但含 ≥2 个断句标点 → 拆为 `- ` 列表行（标点保留行尾）；
    - 段首已有单个 `- ` 前缀在拆分时先剥离，避免 `- - ` 双前缀；
    - 行数上限（SUMMARY_MAX_POINTS，含引导行）按段独立，超限并入最后一段；
    - 其余（超长但拆分点不足 2 个）→ 原样返回。
    """
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""
    out = []
    for seg in text.split("\n"):
        stripped = seg.strip()
        if not stripped:
            out.append("")                # 空行保留
            continue
        if len(stripped) <= SUMMARY_LONG_LINE_MIN:
            out.append(stripped)          # 短段原样
            continue
        out.append(_format_long_segment(stripped))
    return "\n".join(out)


def _format_long_segment(seg):
    """超长单段拆分（seg 已 strip 且 > SUMMARY_LONG_LINE_MIN）。段首单个 `- ` 前缀先剥离。"""
    work = seg[2:] if seg.startswith("- ") else seg
    if len(work) <= SUMMARY_LONG_LINE_MIN:
        return seg                        # 剥前缀后不超长 → 原样（含前缀）

    # 编号拆分：首个编号标记前的文本为引导行（可空），之后按标记切分并重新编号
    marks = list(_NUM_MARK_RE.finditer(work))
    if len(marks) >= 2:
        head = work[:marks[0].start()].strip()
        segs = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(work)
            body = work[m.end():end].strip()
            if body:
                segs.append(body)
        lines = ([head] if head else [])
        limit = SUMMARY_MAX_POINTS - len(lines)  # 编号行数上限
        if len(segs) > limit:
            # 超限：超出部分并入最后一个编号段（不丢内容）
            segs = segs[:limit - 1] + [" ".join(segs[limit - 1:])]
        lines += ["%d. %s" % (i + 1, s) for i, s in enumerate(segs)]
        return "\n".join(lines)

    # 断句拆分：标点保留行尾，全部输出为 `- ` 列表行
    split_pts = list(_SENT_SPLIT_RE.finditer(work))
    if len(split_pts) < 2:
        return seg  # 拆分点不足 2 个，不拆
    parts = []
    start = 0
    for m in split_pts:
        seg_pt = work[start:m.end()].strip()
        if seg_pt:
            parts.append(seg_pt)
        start = m.end()
    tail = work[start:].strip()
    if tail:
        parts.append(tail)
    if len(parts) > SUMMARY_MAX_POINTS:
        parts = parts[:SUMMARY_MAX_POINTS - 1] + ["".join(parts[SUMMARY_MAX_POINTS - 1:])]
    return "\n".join("- %s" % p for p in parts)

def post_event(req_id, kind, content, session_id=None):
    """上报一条执行过程事件到看板（kind: message/tool/system）；session_id 标记归属会话，
    看板按会话展示执行过程（历史事件无标记，session_id 为 None 时归入全量兼容展示）"""
    try:
        D.api("POST", f"/{req_id}/events", {"events": [{"kind": kind, "content": content, "session_id": session_id}]})
    except Exception as e:
        log.warning(f"需求 #{req_id} 事件上报失败: {e}")

def _redline_cancel_reason(req_id):
    """检测需求最近的 dev 红线取消信号（#113）。命中返回事件完整 content（含命中原因），
    未命中/查询异常返回 None。事件接口 board.py GET /<id>/events 已实现，走 HTTP API 不直读
    SQLite；异常（看板未起/超时/重启窗口）由 try/except 兜住退化现有文案。
    匹配过窄可退化，过宽会误导续跑 agent 跳过真实原因自查 → 宁可漏判不可误判。"""
    try:
        events = D.api("GET", f"/{req_id}/events")
    except Exception:
        return None
    for e in events or []:
        content = e.get("content") or ""
        if content.startswith(REDLINE_CANCEL_SIGNAL):
            return content
    return None

def _rate_limit_failure_reason(req_id):
    """检测会话收尾的 API 并发限流（403）信号（#345）。命中返回限流归因文案，未命中/日志
    不可读返回 None。错误文本来源：ACP stderr 重定向 logf = RATE_LIMIT_LOG_FMT（Popen
    stderr=logf），限流发生在进程终止前，读日志尾部即可命中。匹配过窄可退化，过宽会误导
    归类与续跑 agent → 宁可漏判不可误判（仿 _redline_cancel_reason）。"""
    try:
        with open(D.RATE_LIMIT_LOG_FMT.format(req_id=req_id), "rb") as f:
            tail = f.read()[-D.RATE_LIMIT_TAIL_BYTES:].decode("utf-8", errors="replace")
    except Exception:
        return None
    if "concurrent request limit" in tail:
        return "API 并发限流（403：并发位被占），请求被拒绝，会话被终止"
    if re.search(r"\b403\b", tail):
        return "API 并发限流（403：请求被拒绝），并发位被占，会话被终止"
    return None

def _model_unavailable_reason(req_id):
    """#11 运行期模型不可用归因（与 _rate_limit_failure_reason 同源：读 ACP stderr 尾部）。
    窄特征、宁可漏判不可误判：欠费/额度（insufficient|quota|balance）、模型下线
    （model not found|not available|decommission）、尾部连续 ≥3 次 5xx。未命中/日志不可读 → None。"""
    try:
        with open(D.RATE_LIMIT_LOG_FMT.format(req_id=req_id), "rb") as f:
            tail = f.read()[-D.RATE_LIMIT_TAIL_BYTES:].decode("utf-8", errors="replace")
    except Exception:
        return None
    low = tail.lower()
    if re.search(r"insufficient|quota|balance", low):
        return "模型额度/欠费不可用（insufficient/quota/balance），会话被终止"
    if re.search(r"model not found|not available|decommission", low):
        return "模型已下线/不可用（model not found / not available / decommission）"
    if len(re.findall(r"\b(?:500|502|503|504)\b", tail)) >= 3:
        return "模型服务连续 5xx（≥3 次），会话被终止"
    return None

def _note_model_fallback(req_id, role, *, source=None, requested=None, configured=None,
                         add_role=False, fact=None):
    """#11 记录本轮模型降级事实（state["model_fallback"][req_id]）供收尾汇总与运行期兜底判定。
    与 state["retries"] 完全解耦（handle_result 整条覆盖 retries 不会清掉本键）。幂等追加；
    `used` 由 handle_result 置位后本函数保持不复位（兜底轮只允许一次）。"""
    with D.state_lock:
        fb = D.state.setdefault("model_fallback", {})
        entry = fb.get(req_id) or {}
        roles = list(entry.get("degraded_roles") or [])
        facts = dict(entry.get("degraded_facts") or {})
        if add_role and role not in roles:
            roles.append(role)
        if fact:
            facts[role] = fact
        entry.update({"role": role,
                      "source": source if source is not None else entry.get("source"),
                      "requested": requested if requested is not None else entry.get("requested"),
                      "configured": configured if configured is not None else entry.get("configured"),
                      "used": bool(entry.get("used")),
                      "degraded_roles": roles, "degraded_facts": facts})
        fb[req_id] = entry
    # 不在此落盘：本记录只在同进程内被 handle_result 读取（重启后下一轮 run_agent 会重写），
    # 避免为一次无改写价值的写入触发 state 落盘（收尾汇总路径按需 save_state）

def _flush_model_degraded_summary(req, role):
    """#11 本轮降级角色事实汇总写入需求 summary（一行一角色，按「prompt 通道约定」只写事实），
    写后原地清空 degraded_roles 防跨轮重复；无降级时零行为。"""
    req_id = str(req["id"])
    with D.state_lock:
        entry = (D.state.get("model_fallback") or {}).get(req_id) or {}
        roles = list(entry.get("degraded_roles") or [])
        if roles:
            facts = dict(entry.get("degraded_facts") or {})
            entry["degraded_roles"] = []
            D.state["model_fallback"][req_id] = entry
    if not roles:
        return
    D.save_state()
    lines = [facts.get(r) or f"{r} 模型降级（详情见时间线）" for r in roles]
    D.append_summary(req_id, "模型降级：" + "；".join(lines))

def post_model_events(req, role, session, resolved):
    """#11 建会话后模型留痕（时间线 kind="model"）：降级一条 + 未落定一条（各自独立判定）。
    失败仅降级不阻断会话。"""
    req_id = str(req["id"])
    rlabel = D.ROLE_NAMES.get(role, role)
    requested = getattr(session, "model_requested", None) or getattr(session, "model", None)
    effective = getattr(session, "model_effective", None) or requested
    try:
        if resolved.get("degraded"):
            # #13：文案扩为「配置 X → 请求 Y → 实际 Z」；旧关键串（模型降级 / 配置值 xxx）保留
            D.post_dispatcher_event(int(req_id),
                                  f"模型降级：{rlabel} 配置 {resolved.get('configured')} → "
                                  f"请求 {requested} → 实际 {effective}（来源 {resolved.get('source')}，"
                                  f"原因 {resolved.get('reason') or '运行期模型不可用'}）",
                                  kind="model")
        settle = getattr(session, "model_settle", None)
        if settle and settle != "settled":
            merr = getattr(session, "model_error", None)
            tail = f"，原因 {str(merr)[:200]}" if merr else ""
            D.post_dispatcher_event(int(req_id),
                                  f"模型未落定：{rlabel} 请求 {requested}，回读 {effective}"
                                  f"（{settle}），会话继续{tail}", kind="model")
    except Exception as e:
        log.warning(f"需求 #{req_id} 模型留痕事件发送失败（不阻断会话）: {e}")

def post_run_record(req, role, session, started_at, ended_at, returncode, timed_out, usage=None,
                    aborted=False, usage_ext=None):
    """会话收尾写一条运行记录到看板 agent_runs（报表基础数据源，只增不改）+ 一条明确的结束事件。
    status 按进程级判定：done / timed_out / failed / aborted（#28 输出守卫中止）；usage 为
    collect_usage 的 {context_tokens, max_tokens, context_pct}（超时/中止/异常路径为 None，
    token 字段置空）。#42：usage_ext = ACPSession.cache_summary() 结果（{state, cached_tokens,
    input_tokens, raw}）——cache_state 只由 usage_ext['state'] 决定，与峰值 usage 是否 None 解耦
    （峰值解析失败不得把已返回的缓存字段记成 na）；usage is None 仅保持既有峰值三字段 NULL 语义；
    na 时缓存 token 由采集端构造即为 NULL。写库失败仅警告不阻断收尾（对齐 post_event 容错）；
    sessions 状态文件不动，历史数据不迁移"""
    req_id = str(req["id"])
    sid = getattr(session, "session_id", None) if session else None
    if aborted:
        status = "aborted"
    elif timed_out:
        status = "timed_out"
    elif returncode == 0:
        status = "done"
    else:
        status = "failed"
    ext = usage_ext if isinstance(usage_ext, dict) else {}
    try:
        D.api("POST", f"/{req_id}/runs", {
            "role": role, "session_id": sid,
            "started_at": started_at, "ended_at": ended_at,
            "status": status, "tool_count": len(session._tool_calls) if session else 0,
            "context_pct": pct_value(usage["context_pct"]) if usage else None,
            "context_tokens": usage["context_tokens"] if usage else None,
            "max_tokens": usage["max_tokens"] if usage else None,
            # #42 缓存命中指标（hit=真实返回；unknown=完成请求但供应商未返回；na=零完成请求）
            "cached_tokens": ext.get("cached_tokens"),
            "input_tokens": ext.get("input_tokens"),
            "cache_state": ext.get("state"),
            "usage_raw": ext.get("raw"),
        })
    except Exception as e:
        log.warning(f"需求 #{req_id} 运行记录写入失败: {e}")
    D.post_event(req_id, "system", f"运行结束（{status}）", session_id=sid)

def post_dispatcher_event(req_id, content, kind="action", session_id=None):
    """记录一条调度器自身动作到看板（需求级时间线，供"调度历史回放"展示）。
    返回新记录 id（供会话建立后回填 session_id），失败返回 None"""
    try:
        resp = D.api("POST", f"/{req_id}/dispatcher-events",
                   {"events": [{"kind": kind, "content": content, "session_id": session_id}]})
        return resp.get("id") if isinstance(resp, dict) else None
    except Exception as e:
        log.warning(f"需求 #{req_id} 调度器事件上报失败: {e}")
        return None

def backfill_spawn_session(req_id, event_id, session_id):
    """会话建立后按 spawn 记录 id 回填 session_id；失败仅降级不阻断（双端不同步上线安全）"""
    if not event_id or not session_id:
        return
    try:
        D.api("PATCH", f"/{req_id}/dispatcher-events/{event_id}", {"session_id": session_id})
    except Exception as e:
        log.warning(f"需求 #{req_id} spawn 记录回填 session_id 失败: {e}")

def owner_key(req):
    """会话归属键 = 需求 id（需求平级独立，无会话组概念）"""
    return str(req["id"])

def session_key(req, role=None):
    """会话归属键按角色区分：analysis:<id> / proposal:<id> / tech:<id> / dev:<id> / accept:<id>。
    各阶段互不覆盖且全程保留（供看板按阶段展示）；重试续跑同一角色仍命中同一键"""
    if not role:
        return str(req["id"])
    return f"{role}:{req['id']}"

def session_id_of(entry):
    """sessions 记录取 session_id：旧格式是纯 id 字符串，新格式是信息对象 {id, model, ...}"""
    return entry.get("id") if isinstance(entry, dict) else entry

def kimi_version():
    """抓取当前 kimi code 版本号（如 '0.33.0'），供会话记录的 harness 字段使用。
    每次调用实时执行 KIMI --version，不做调度器启动时一次性缓存——保证语义是"该会话实际跑的 kimi 版本"。
    失败/超时/输出为空返回 None，调用方忽略即可，不阻断会话建立"""
    try:
        out = subprocess.run([KIMI, "--version"], capture_output=True, text=True, timeout=10)
        text = (out.stdout or out.stderr or "").strip()
        return text.splitlines()[0] if text else None
    except Exception:
        return None

def update_session_info(req, key=None, **fields):
    """更新该需求会话归属键下的 ACP 会话信息（id/model/thinking/context/branch/workdir/harness）。
    无记录且未给 id 时不创建；空值字段忽略"""
    k = key or D.owner_key(req)
    with D.state_lock:
        sessions = D.state.setdefault("sessions", {})
        entry = sessions.get(k)
        if entry is None and not fields.get("id"):
            return
        if not isinstance(entry, dict):
            entry = {"id": entry} if entry else {}
        entry.update({kk: vv for kk, vv in fields.items() if vv})
        sessions[k] = entry
    D.save_state()

def sync_agent_session(req, role, session_id=None, row_id=None, **fields):
    """同步 ACP 会话注册表（agent_sessions）：调度器写入，失败仅降级不阻断。
    返回看板侧行 id（供后续按 id 更新）；无法写入时返回 None。"""
    body = {"req_id": req["id"], "role": role}
    if row_id:
        body["id"] = row_id
    if session_id:
        body["session_id"] = session_id
    body.update({k: v for k, v in fields.items() if v is not None})
    try:
        resp = D.api_board("POST", "/agent-sessions", body)
        return (resp or {}).get("id")
    except Exception as e:
        log.warning(f"需求 #{req['id']} agent_sessions 同步失败: {e}")
        return None


def _touch_session_activity(req_id, role, holder, kind, detail=None):
    """#421 会话活性心跳：ACPSession 状态切换时按注册表行 id 更新（失败仅告警，不阻断会话）。
    holder["row_id"] 为 None（sync_agent_session 写失败）→ 跳过并只告警一次，避免每轮刷 400 噪声。"""
    row_id = (holder or {}).get("row_id")
    if not row_id:
        if holder is not None and not holder.get("warned"):
            holder["warned"] = True
            log.warning(f"需求 #{req_id} 会话活性心跳跳过（无注册表行 id，role={role}）")
        return
    now = round(time.time(), 3)
    body = {"id": row_id}
    if kind == "llm_request":
        body.update(turn_step="llm", last_llm_request_at=now)
    elif kind == "llm_thought":
        body["last_thought_at"] = now
    elif kind == "llm_response":
        body["last_llm_response_at"] = now
    elif kind == "tool_start":
        body.update(turn_step="tool", last_tool_started_at=now,
                    turn_step_detail=(" ".join(str(detail or "").split())[:48] or None))
    elif kind == "tool_end":
        # 在途工具清空 = 下一次模型请求的近似起点（ACP 无原生通知）
        body.update(turn_step="llm", last_llm_request_at=now)
    else:
        return
    try:
        D.api_board("POST", "/agent-sessions", body)
    except Exception as e:
        log.warning(f"需求 #{req_id} 会话活性心跳写入失败（row={row_id}, {kind}）: {e}")


def _close_session_row(req, role, row_id, status, **fields):
    """#421：把注册表行置 idle/interrupted 的统一入口（附带 turn_step='' 清理；
    只为证据整洁，前端正确性不依赖它）。失败仅降级。"""
    if row_id is None:
        return
    try:
        D.sync_agent_session(req, role, row_id=row_id, status=status, turn_step="", **fields)
    except Exception as e:
        log.warning(f"需求 #{req.get('id')} 会话行置 {status} 失败（row={row_id}）: {e}")


def mark_session_poisoned(req_id, role=None):
    """重试耗尽：把会话标记为毒（#273 人工接管）——不再删记录，自动路径跳过，人工可在 UI 继续。
    同时同步 agent_sessions 注册表，供中间栏展示「已停止自动重试」徽标。"""
    with D.state_lock:
        sessions = D.state.get("sessions", {})
        keys = [str(req_id)]
        if role:
            keys.append(session_key({"id": req_id}, role))
        for k in keys:
            entry = sessions.get(k)
            if isinstance(entry, dict):
                entry["poisoned"] = True
                sid = entry.get("id")
            elif entry:
                sessions[k] = {"id": entry, "poisoned": True}
                sid = entry
            else:
                sid = None
            if sid and role:
                try:
                    D.api_board("POST", "/agent-sessions",
                              {"req_id": req_id, "role": role, "session_id": sid, "poisoned": 1})
                except Exception as e:
                    log.warning(f"需求 #{req_id} 毒会话标记同步失败: {e}")
    D.save_state()

def find_resume_sid(req, role=None):
    """查该需求应恢复的 ACP session_id（阶段内重试/need_confirm 续跑）。
    需求平级独立：只按需求 id 键（analysis 用独立键）查询；阶段推进时会话已被丢弃，自然查不到；
    #273：标记为毒（poisoned）的会话不参与自动 resume，仅人工可继续。"""
    sessions = D.state.get("sessions", {})
    entry = sessions.get(session_key(req, role))
    if isinstance(entry, dict) and entry.get("poisoned"):
        return None
    return session_id_of(entry)

