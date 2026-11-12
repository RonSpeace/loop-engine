"""agent 运行（ACP 模式）：拉起会话、构造提示词、驱动回合、处理结果文件。"""

import json
import re
import time
from pathlib import Path

import prompt_store
from prompt_i18n import prompt_lang, tr
from acp_shared import PLAYWRIGHT_TOOL_PREFIX, RESULT_FILENAME, TAPD_TOOL_PREFIX, TurnAborted, role_capability

from dispatcher_modules.flow_advance import _apply_model_override, review_escalation_override
from dispatcher_modules.runtime import D, log, subprocess, threading


active_lock = threading.Lock()
active_runs = {}  # req_id(str) -> {"proc": Popen, "project": str, "role": str, "owner": 会话归属键,
                  #                "session": ACPSession, "started_at": float}（#388 session/started_at 供兜底收尾）

# #388 兜底收尾防重：主循环 _sweep_dead_running_once 已补收尾的会话行 id 集合（进程死场景）。
# 会话线程 finally 恢复后经 _swept 检查跳过重复 post_run_record / handle_result
# （避免失败通道二次重试计数与重复 spawn 竞态）；sweep 线程与会话线程并发访问，用锁保护
_swept_rows = set()
_swept_lock = threading.Lock()


def _swept(row_id):
    with _swept_lock:
        return row_id in _swept_rows


def _mark_swept(row_id):
    with _swept_lock:
        _swept_rows.add(row_id)


def readonly_tool_roots(project, workdir, artifact_root):
    """工具 roots 构造：[会话工作目录] + 项目各真实仓库根 + 产物目录。
    多 paths 项目会话 cwd 是聚合目录（symlink 结构），文件逻辑路径 resolve 后落在真实仓库根，
    因此必须含各真实仓库根，否则聚合目录下工具路径全被误判越权。"""
    roots = [Path(workdir)]
    for p in (D.PROJECTS.get(project, {}).get("paths") or []):
        roots.append(Path(p))
    roots.append(Path(artifact_root))
    return roots
def spawn_agent(req, role=None, extra="", resume=False, model_override=None):
    role = role or D.role_for(req)
    # #430：空角色 fail-closed——配置降级下 role_for 恒为空串，放行会把卡误置 doing 且 build_prompt
    # 抛 ValueError，卡停在 doing（主循环只捞 open，无法自愈）。守卫必须先于 set_req(doing)。
    if not role:
        log.error(f"需求 #{req['id']} 无角色可拉起（流程配置降级/前置槽位为空）：拒绝启动会话，卡保持原状态")
        try:
            D.post_dispatcher_event(req["id"], "无角色可拉起（流程配置降级）：会话未启动，需求保持原状态",
                                  kind="action")
        except Exception:
            pass
        return
    # #279：角色与需求 mode/phase 不匹配即非法调用（绕过调度器直接拉起错误角色会话），
    # 在置 doing/写 spawn 事件之前拦截，仅留告警事件
    if not D.role_allowed(req, role):
        log.error(f"需求 #{req['id']} 拒绝拉起角色 {role}（mode={req.get('mode')}, phase={req.get('phase')}）："
                  f"角色与需求当前阶段不匹配，疑似绕过调度器直接调用")
        try:
            D.post_dispatcher_event(req["id"],
                                  f"拒绝拉起非法角色 {role}（mode={req.get('mode')}, phase={req.get('phase')}），会话未启动",
                                  kind="action")
        except Exception:
            pass
        return
    req_id = str(req["id"])
    project = req["project"]
    if role in D.project_external_roles(project):
        # 主 Agent 协作模式：该角色由外部主 Agent 负责——不建会话，停卡等待外部产出；外部写好产物后
        # 把卡指向下一节点并改回待开发即可继续（评审打回到该角色同样停在这里）
        D.set_status(req["id"], "later")
        D.append_summary(req_id, f"{D.ROLE_NAMES.get(role, role)}由外部主 Agent 负责，等待其产出后继续。")
        D.post_dispatcher_event(req_id, f"「{D.ROLE_NAMES.get(role, role)}」由外部主 Agent 负责（projects.json external_roles），"
                                      "未拉起会话，需求转暂缓等待产出", kind="phase")
        log.info(f"需求 #{req_id} 角色 {role} 为外部角色，停卡等待外部主 Agent")
        return
    if not model_override:
        # C3：本卡该角色处于评审升级期 → 本轮用升级模型（评审打回 / 人工 n 打回 / 失败重试共用此入口）
        model_override = review_escalation_override(req_id, role)
    # #419：进入 doing 不再写 phase（phase 为历史只读列；推进按卡上流程快照 node_index）
    D.set_req(req["id"], {"status": "doing"})
    D._note_review_model(req, role)
    try:
        prompt = D.build_prompt(req, role, extra)
    except D.MissingRequiredContext as e:
        # fail-closed：必需技能缺失/不可用 → 不拉会话，卡转暂缓并写明缺什么
        names = "、".join(e.names)
        log.error(f"需求 #{req_id} 角色 {role} 必需技能缺失（{names}），停线转暂缓")
        D.set_status(req["id"], "later")
        D.post_dispatcher_event(req_id, f"「{D.ROLE_NAMES.get(role, role)}」必需技能缺失或不可用：{names}，"
                                        "未拉起会话，需求转暂缓（去技能库修复后改回待开发）", kind="action")
        return
    log.info(f"需求 #{req_id} [{project}] 角色={role} 开始{'（续跑）' if resume else ''}: {req['title']}")
    resume_sid = D.find_resume_sid(req, role) if resume else None
    if resume_sid:
        # 续跑：拉起时旧 id 已知，当场写入 spawn 记录
        D.post_dispatcher_event(req_id, f"拉起「{D.ROLE_NAMES.get(role, role)}」会话（续跑）",
                              kind="spawn", session_id=resume_sid)
        spawn_event_id = None
    else:
        # 新会话：先写记录拿 id，会话建立后回填
        spawn_event_id = D.post_dispatcher_event(req_id,
                                               f"拉起「{D.ROLE_NAMES.get(role, role)}」会话（新会话）",
                                               kind="spawn")
    # 非兜底路径逐字保持原调用形态（不给存量 stub 引入新 kwarg）
    thread_kw = {"kwargs": {"model_override": model_override}} if model_override else {}
    threading.Thread(target=D.run_agent,
                     args=(req, role, prompt, time.time(), extra, resume, spawn_event_id),
                     daemon=True, **thread_kw).start()


def result_state(req):
    """回合结束时的结果文件状态：'ok'/'missing'/'invalid'（detail 供事件与续接文案）。
    路径检查与 handle_result 一致：产物目录 result.json，兼容旧约定工作目录根 .req-board-result.json。"""
    result_file = D.result_file_path(req)
    if not result_file.exists():
        legacy = D.workdir_for(req["project"]) / RESULT_FILENAME
        if legacy.exists():
            result_file = legacy
    if not result_file.exists():
        return "missing", "result.json 不存在"
    try:
        json.loads(result_file.read_text())
        return "ok", ""
    except Exception as e:
        return "invalid", f"result.json 解析失败（{e}）"


# #327：存量 resume 会话保留旧系统提示词（不含输出风格新规），续接文案补一行风格提醒。
# 正文在 prompts/system/resume-style.md（本常量 = 默认文本兼容视图）；续跑文案经 resume_text() 渲染
RESUME_STYLE_REMINDER = (
    "输出风格（新规范，本 resume 会话的系统提示词可能未含）：回复结论先行、能短则短"
    "（单条一般两三百字内），禁止开场白/客套/复述需求；产物文档删除过程叙述、每要点一行；"
    "result.json summary 要点化、一般不超过 10 行。")


def resume_text(req, role, extra, user_text=None):
    """续跑文案（恢复旧会话时发送）：人工续跑 system/resume-user，自动续跑 system/resume-auto；
    两者都内嵌输出风格提醒 system/resume-style（{resume_style}）。返回 (正文, 条目 id)。"""
    lang = prompt_lang(req.get("project"))
    style = prompt_store.read("system/resume-style", lang)
    if user_text is not None:
        return prompt_store.render("system/resume-user", lang=lang, req_id=req["id"], role=role, user_text=user_text,
                                   extra=extra, resume_style=style), "system/resume-user"
    return prompt_store.render("system/resume-auto", lang=lang, req_id=req["id"], role=role, extra=extra,
                               resume_style=style, max_steps=_steps_text(role, req)), "system/resume-auto"


def _record_start(req, role, sent, kind, session, compiled=None, extra_items=(), parent=None,
                  context_update=None, redline=True):
    """执行档案落盘（失败只告警，绝不阻断会话）。"""
    try:
        return D.record_session_start(req, role, sent, kind=kind, session=session, compiled=compiled,
                                      extra_items=extra_items, parent=parent,
                                      context_update=context_update, redline=redline)
    except Exception as e:
        log.warning(f"需求 #{req.get('id')} 执行档案记录失败（不阻断会话）: {e}")
        return None


def _artifact_ls(req):
    """#388 产物目录现状清单（续接轮注入，一眼可见缺什么）：
    返回 (文件清单, 目录路径文本)；目录不存在/不可读 → (空清单, "（不可读）")，降级不崩。"""
    try:
        art = D.artifact_dir(req)
        if art.is_dir():
            return [p.name for p in sorted(art.iterdir())], str(art)
        return [], str(art)
    except Exception:
        return [], "（不可读）"


def build_followup_prompt(role, state, req):
    """无结果续接文案（#273 护栏）：dev/accept 强调终止后台任务 + 前台验证；
    只读角色只需补写 result.json（审批路由仍强制只读，不放松约束）。
    #388：注入产物目录现状清单；强化「写入失败必须重写、不许乐观声明」。"""
    reason = ("result.json 存在但 JSON 解析失败" if state == "invalid"
              else "result.json 不存在")
    ls, art_txt = _artifact_ls(req)
    art_hint = f"产物目录现状（{art_txt}）：" + ("、".join(ls) if ls else "（空）")
    strict = (f"{art_hint}\n"
              "写 result.json 若失败（报错/回读校验不通过），必须重写后再回读校验，"
              "禁止声称已写入但实际未校验。")
    if role_capability(role, "readonly"):
        return (f"你结束了回合，但结果文件未提交（{reason}）。"
                "请立即用 Write/fs 工具把 result.json 写入产物目录"
                "（status/summary 必填；前置环节角色另带 suggested_flow: 推荐流程 id 与 repos: 涉及仓库数组），"
                "写后回读校验 JSON 可解析。禁止写入产物目录以外的文件。写完后结束回合。\n"
                f"{strict}")
    return (f"你结束了回合，但结果文件未提交（{reason}）。"
            "注意：ACP 没有异步通知，回合结束即会话结束，后台任务会随进程丢失。"
            "请先终止仍在运行或等待中的后台任务；必要验证改为前台分块执行并等待完成；"
            "然后用脚本或 python json.dumps 写 result.json（status/summary 必填），"
            "写后回读校验 JSON 可解析。写完后结束回合。\n"
            f"{strict}")


def _close_held(h):
    """关闭保持进程（优雅 terminate + 关闭日志；进程组兜底由 ACPSession.close 内部处理）"""
    try:
        h["session"]._flush_message()
        h["session"].close(kill=False)
    except Exception:
        pass
    try:
        h["logf"].close()
    except Exception:
        pass


def _hold_session(asid, req_id, role, session, logf):
    """一轮正常结束后把进程放入保持池；pid 镜像到 state（调度器重启后据此清理孤儿）"""
    with D.held_lock:
        D.held_sessions[asid] = {"session": session, "logf": logf,
                               "req_id": str(req_id), "role": role, "kept_at": time.time()}
        D.state.setdefault("held_procs", {})[asid] = session.proc.pid
    D.save_state()
    log.info(f"需求 #{req_id} 会话进程保持 (asid={asid}, pid={session.proc.pid})")


def _steps_text(role, req):
    """提示词里的步数上限文案（C2）：有角色预算给具体数字，否则沿用「约 200 步」（kimi 自身每轮上限）。"""
    n = (D.role_budget(role, project=req.get("project")) or {}).get("max_steps")
    return str(n) if n else tr(prompt_lang(req.get("project")), "约 200")


def _budget_wrapup(session, req, role):
    """C2：守卫中止后的收束。步数预算截停 → 同会话给一轮限步收尾写 result.json：写成 → 返回 False
    （按正常回合收尾，交 handle_result 采信结果）；未写成 → 返回结构化原因串（truthy，走 aborted 失败
    通道，归因 `budget_exceeded: steps`）。其它守卫（复读/超长单请求）→ 返回 True（行为不变）。"""
    tripped = (getattr(session, "_guard", None) or {}).get("tripped") or {}
    if tripped.get("kind") != "budget_steps":
        return True
    detail = tripped.get("detail") or "本轮工具调用达上限"
    D.post_event(req["id"], "system", f"预算截停（{detail}），给一轮限步收尾写结果",
               session_id=getattr(session, "session_id", None))
    session.step_budget = D.BUDGET_WRAPUP_STEPS
    lang = prompt_lang(req.get("project"))
    if lang != "zh":
        m = re.fullmatch(r"本轮工具调用达 (\d+) 步上限", detail)
        detail = (tr(lang, "本轮工具调用达 {n} 步上限", n=m.group(1)) if m
                  else tr(lang, "本轮工具调用达上限"))
    wrapup = prompt_store.render("system/wrapup", lang=lang, detail=detail, wrapup_steps=D.BUDGET_WRAPUP_STEPS - 5)
    try:
        session.prompt(wrapup, timeout=D.BUDGET_WRAPUP_SEC)
    except Exception as e:
        log.warning(f"需求 #{req['id']} 预算收尾轮异常: {e}")
    state_, _detail = D.result_state(req)
    if state_ == "ok":
        D.post_event(req["id"], "system", "预算截停后收尾轮已提交结果",
                   session_id=getattr(session, "session_id", None))
        return False
    return f"budget_exceeded: steps（{detail}），收尾轮未提交结果"


def run_agent(req, role, prompt, started_at, extra="", resume=False, spawn_event_id=None,
              agent_session_id=None, user_text=None, resume_session_id=None,
              hold=False, held=None, model_override=None):
    """ACP 会话主流程：启动 kimi acp -> 握手 -> prompt -> 收束结果。
    #284 进程保持：hold=True 且本轮正常结束 → 进程放保持池（下条消息复用，省拉起+resume）；
    held 非空 → 复用保持池进程（跳过 Popen/start/resume 直接 prompt），正常结束仍放回保持池。
    超时/异常路径照旧杀进程不保持；自动开发（spawn_agent）不传 hold，行为与现状一致。"""
    # #279：最底层会话启动器同样校验角色（防止绕过 spawn_agent 直接调用，如 dev agent
    # import dispatcher 拉 accept 会话）；拦截返回 False，正常完成返回 True（调用方区分）
    if not D.role_allowed(req, role):
        log.error(f"需求 #{req['id']} 拒绝运行角色 {role}（mode={req.get('mode')}, phase={req.get('phase')}）："
                  f"角色与需求当前阶段不匹配，疑似绕过调度器直接调用")
        try:
            D.post_event(req["id"], "system",
                       f"拒绝拉起非法角色 {role}（mode={req.get('mode')}, phase={req.get('phase')}），会话未启动")
        except Exception:
            pass
        return False
    req_id = str(req["id"])
    # analysis 起全部角色在 per-requirement worktree 工作（build_prompt 已创建，基线=主干）
    workdir = D.dev_workdir_for(req)
    timed_out = False
    aborted = False   # #28 输出守卫中止标记（复读/单次请求超长）
    returncode = 1
    # 活跃时长起点（2026-09-17 修复）：续接预算用 monotonic 计算——started_at 是 wall clock，
    # 系统睡眠会被算进「已用预算」，唤醒后误判预算耗尽而放弃续接
    started_mono = time.monotonic()
    row_id = agent_session_id
    # #421 会话活性心跳持有人（每轮新建）：ACPSession 状态切换（llm 请求/思考/响应、工具起止）
    # 时按注册表行 id 落时间戳，右栏据此判活/判死。复用保持进程的轮次必须**重绑** sink
    # （会话可能还挂着上一轮的旧 holder → 心跳会写到旧行或永远失聪）
    activity = {"row_id": agent_session_id, "warned": False}

    def _activity_sink(kind, detail=None):
        D._touch_session_activity(req["id"], role, activity, kind, detail)

    # #28 输出守卫触发留痕（三落点：agent_events 时间线 / dispatcher_events 调度历史 /
    # summary 单行事实句）。守卫在会话线程/读线程内触发，故各自 try/except 不抛，避免反向打断收束。
    def _output_guard_sink(kind, detail):
        label = "预算截停" if str(kind).startswith("budget") else "输出守卫"   # C2 预算复用守卫通道
        try:
            D.post_event(req["id"], "system", f"{label}中止（{kind}）：{detail}",
                       session_id=getattr(session, "session_id", None))
        except Exception:
            pass
        try:
            D.post_dispatcher_event(req["id"], f"本轮已中止（{label}：{kind}）—— {detail}",
                                  kind="action")
        except Exception:
            pass
        try:
            D.append_summary(req["id"], f"本轮{label}中止（{kind}）：{detail}")
        except Exception:
            pass

    usage = None  # collect_usage 三值 {context_tokens, max_tokens, context_pct}，超时/异常路径为 None
    usage_ext = None  # #42 缓存命中三态 {state, cached_tokens, input_tokens, raw}，见 cache_summary()
    logf = held["logf"] if held is not None else open(f"/tmp/req_board_agent_{req_id}.log", "a")
    kept = held is not None
    session = None
    run_meta = None  # 执行档案（manifest 路径 / prompt_hash / prefix_hash），随运行记录落 agent_runs
    # 本次 build_prompt 的编译结果（清单）；被替换/缺失（测试桩、人工续跑占位 prompt）时为 None，只记全文
    compiled = D.last_compiled(req["id"], role)
    if compiled is not None and compiled.prompt != prompt:
        compiled = None
    ctx_plan = None   # 续跑补发计划（context.resume_plan）
    arun = None  # #388：本线程登记的 active_runs 对象（finally 对象匹配删除，防误删新会话登记）
    try:
        if kept:
            # #284 复用保持进程：跳过 Popen/session.start，直接进入 prompt 流程
            session = held["session"]
            # #421 显式重绑活性心跳（池复用的会话对象可能还挂着上一轮的旧 holder）
            session.activity_sink = _activity_sink
            # #28 显式重绑输出守卫（防沿用上一轮旧闭包/旧计数/旧 tripped 状态）
            session.rearm_output_guard(_output_guard_sink)
            # #388：active_runs 补充 session/started_at（运行期兜底 sweep 补收尾时
            # 运行记录与 handle_result 的起始时间/会话信息来源）；arun 保留对象引用
            # 供 finally 对象匹配删除（防误删 sweep/失败通道新登记的新会话）
            arun = {"proc": session.proc, "project": req["project"],
                    "role": role, "owner": D.owner_key(req),
                    "session": session, "started_at": started_at}
            with active_lock:
                D.active_runs[req_id] = arun
            log.info(f"需求 #{req_id} 复用保持进程 (pid={session.proc.pid}, role={role})")
            resumed = True
        else:
            # #423：只读评审角色（python_safe 能力键）放开受限 python 自验——python_roots 取完整
            # readonly_tool_roots（含项目各真实仓库根：形态 B 的 `<root>/.venv/bin/python` 精确
            # 匹配所需，worktree 内无 .venv；形态 A 的 py_compile 落点另收窄，见下）
            py_safe = role_capability(role, "python_safe")
            # #11 spawn 前静态校验 + 降级链：角色配置值不在别名池 → 直接按兜底链回落（不阻断）。
            # 运行期兜底轮（model_override 非空）本轮改用全局兜底模型（不写配置文件、不产生永久降级）
            resolved = _apply_model_override(D.resolve_role_model(role, project=req["project"]),
                                             model_override)
            session = D.ACPSession(req["id"], workdir, logf, role, D.artifact_dir(req), project=req["project"],
                                 kimi_bin=str(D.REPO_DIR / "scripts" / "kimi-chat-acp"),
                                 event_sink=D.post_event, readonly=role_capability(role, "readonly"),
                                 readonly_roots=D.readonly_tool_roots(req["project"], workdir, D.artifact_dir(req)),
                                 write_roots=D.dev_write_roots(req),
                                 main_ws_roots=D.PROJECTS.get(req["project"], {}).get("paths"),
                                 # #379 dev/accept（bash=allow/no_commit）注入 Playwright 浏览器工具前缀，
                                 # 审批按 _auto_approve 双清单校验（fail-closed）；只读角色不注入、行为零变化
                                 extra_allow_prefixes=(
                                     ([TAPD_TOOL_PREFIX] if role_capability(role, "tapd_readonly") else [])
                                     + ([PLAYWRIGHT_TOOL_PREFIX]
                                        if role_capability(role, "bash") in ("allow", "no_commit")
                                        else [])),
                                 # #396：merge_resolve 注入需求助手合并授权（#355）子集——
                                 # git_cmd_safe(allow_merge=True) 受限形态 + worktree 内写
                                 allow_git_commit=role_capability(role, "bash") == "git_merge",
                                 allow_git_merge=role_capability(role, "bash") == "git_merge",
                                 # #423：py_compile 会在目标旁写 __pycache__，落点收窄到 worktree/产物目录
                                 allow_python_safe=py_safe,
                                 python_roots=(D.readonly_tool_roots(req["project"], workdir, D.artifact_dir(req))
                                               if py_safe else None),
                                 python_compile_roots=([workdir, D.artifact_dir(req)] if py_safe else None),
                                 model=resolved["model"] or None,
                                 # #421 会话活性心跳（状态切换时才写，非定时轮询）
                                 activity_sink=_activity_sink,
                                 # #28 输出守卫留痕回调（复读/单次请求超长触发时）
                                 output_guard=_output_guard_sink)
            # #11 请求值/来源落在会话对象上（update_session_info / sync_agent_session 落库用）
            session.model_requested = resolved["model"]
            session.model_source = resolved["source"]
            # #13 配置值与降级原因同落会话对象（透传给 update_session_info / sync_agent_session）；
            # 未降级时 reason 不写（保持「无异常不写该列」）
            session.model_configured = resolved["configured"]
            if resolved["degraded"]:
                session.model_reason = resolved["reason"]
            D._note_model_fallback(req_id, role, source=resolved["source"],
                                 requested=resolved["model"], configured=resolved["configured"],
                                 add_role=resolved["degraded"],
                                 fact=(f"{role} 请求 {resolved['configured']} → 实际 "
                                       f"{resolved['model']}（{resolved['reason']}）"
                                       if resolved["degraded"] else None))
            # #388：active_runs 补充 session/started_at（运行期兜底 sweep 补收尾时
            # 运行记录与 handle_result 的起始时间/会话信息来源）
            arun = {"proc": session.proc, "project": req["project"],
                    "role": role, "owner": D.owner_key(req),
                    "session": session, "started_at": started_at}
            with active_lock:
                D.active_runs[req_id] = arun
            log.info(f"需求 #{req_id} ACP 进程启动 (pid={session.proc.pid}, role={role})")
            resume_sid = (resume_session_id if resume_session_id is not None
                          else (D.find_resume_sid(req, role) if resume else None))
            if resume_sid and compiled is not None and user_text is None:
                # 续跑补发：与该会话上次清单对比；L0/L1 变化 → 不续用旧会话，新会话带完整提示词 + 交接
                ctx_plan = D.context_resume_plan(req, role, compiled, resume_sid)
                if ctx_plan["mode"] == "new_session":
                    D.post_event(req["id"], "system",
                                 "上下文的平台策略/角色模板已变化（" + "、".join(ctx_plan["changed"])
                                 + "），不续用旧会话，改开新会话并附交接说明")
                    resume_sid = None
                    prompt = prompt + "\n\n" + ctx_plan["handoff"]
            resumed = session.start(workdir, resume_session_id=resume_sid)
            # #11 落定回读留痕（仅非 held 复用轮）：降级 / 未落定 → 记录 + 时间线 kind="model"
            settle = getattr(session, "model_settle", None)
            if settle and settle != "settled":
                # #13：未落定原因（settle + harness 原始报错摘要 ≤200）落 model_reason
                session.model_reason = f"{settle}：{getattr(session, 'model_error', None) or '未落定'}"
                D._note_model_fallback(
                    req_id, role, add_role=True,
                    fact=(f"{role} 请求 {resolved['model']} → 回读 "
                          f"{getattr(session, 'model_effective', None) or resolved['model']}"
                          f"（{settle}）"))
            if not kept:
                D.post_model_events(req, role, session, resolved)
        runtime_id = getattr(session, "runtime_id", "kimi")
        ver = D.kimi_version() if runtime_id == "kimi" else None
        harness = (f"kimi code {ver}" if ver else None) if runtime_id == "kimi" else runtime_id
        D.update_session_info(req, key=D.session_key(req, role), id=session.session_id,
                            model=session.model_name, thinking=session.thinking,
                            model_requested=getattr(session, "model_requested", None),
                            model_source=getattr(session, "model_source", None),
                            model_configured=getattr(session, "model_configured", None),
                            model_reason=getattr(session, "model_reason", None),
                            harness=harness)
        # #273 人工接管：同步 agent_sessions 注册表（失败仅降级，不阻断会话）
        row_id = D.sync_agent_session(req, role, session_id=session.session_id, row_id=row_id,
                                    model=session.model_name, thinking=session.thinking,
                                    model_requested=getattr(session, "model_requested", None),
                                    model_source=getattr(session, "model_source", None),
                                    model_configured=getattr(session, "model_configured", None),
                                    model_reason=getattr(session, "model_reason", None),
                                    harness=harness,
                                    workdir=str(workdir), status="running")
        # #421：同步失败返回 None 时保留旧 id（心跳继续写已知行，不因一次抖动失聪）
        activity["row_id"] = row_id or activity["row_id"]
        if spawn_event_id and session.session_id:
            # 回填 spawn 记录 session_id（#120）：失败仅降级，不阻断会话流程
            D.backfill_spawn_session(req_id, spawn_event_id, session.session_id)
        if kept:
            D.post_event(req["id"], "system", f"agent 复用保持进程（{role}），继续执行",
                       session_id=session.session_id)
        else:
            D.post_event(req["id"], "system", f"agent 启动（{role}），ACP 会话已建立", session_id=session.session_id)
        D.post_event(req["id"], "system", "会话已恢复，继续执行" if resumed else "会话已建立，开始执行",
                   session_id=session.session_id)
        if user_text:
            D.post_event(req["id"], "user", user_text, session_id=session.session_id)
        actual_prompt = prompt
        resume_id = None
        lang = prompt_lang(req.get("project"))
        if user_text is not None and not resumed:
            actual_prompt = prompt + tr(lang, "\n\n用户消息：{text}", text=user_text)
        elif resumed:
            # 续跑文案（人工续跑 / 自动续跑）：正文在 prompts/system/resume-*.md，均内嵌输出风格提醒
            actual_prompt, resume_id = resume_text(req, role, extra, user_text)
        # #108：进程安全红线统一注入（新会话与续跑两条路径一次覆盖，注入点唯一）
        actual_prompt = (D.append_kill_redline(actual_prompt, session.proc.pid) if lang == "zh"
                         else D.append_kill_redline(actual_prompt, session.proc.pid, lang=lang))
        # 执行档案：本轮实际发出的全文 + 注入清单（编译结果取自本次 build_prompt；被替换/缺失时只记全文）
        extra_items = []
        context_update = None
        if ctx_plan is not None:
            sent = bool(resumed and ctx_plan["mode"] == "update")
            context_update = {"sent": sent or (ctx_plan["mode"] == "new_session" and not resumed),
                              "mode": ctx_plan["mode"], "changed": ctx_plan["changed"],
                              "prev_run_key": ctx_plan["prev_run_key"]}
            if sent:
                # L2–L6 有变化：续跑消息前置「上下文更新」块
                actual_prompt = ctx_plan["block"] + "\n\n" + actual_prompt
                extra_items.append(D.context_runtime_item("system/context-update", ctx_plan["block"],
                                                          "续跑补发：" + "、".join(ctx_plan["changed"])))
                D.post_event(req["id"], "system", "续跑补发上下文更新：" + "、".join(ctx_plan["changed"]),
                             session_id=session.session_id)
            elif ctx_plan["mode"] == "new_session":
                extra_items.append(D.context_runtime_item("system/resume-handoff", ctx_plan["handoff"],
                                                          "L0/L1 变化改开新会话"))
        if resume_id:
            extra_items.append(D.context_runtime_item(resume_id, actual_prompt, "恢复旧会话"))
        run_meta = _record_start(req, role, actual_prompt, "resume" if resumed else "new", session,
                                 compiled=compiled, extra_items=extra_items, context_update=context_update)
        if not hold and not kept:
            # C2：自动开发会话按角色预算限步（人工续跑 hold/复用轮不限，由人掌控）
            session.step_budget = (D.role_budget(role, project=req.get("project")) or {}).get("max_steps")
        try:
            # #396：merge_resolve 单轮时间盒 15 分钟（prompt 内 {timeout_minutes} 同步覆盖）
            budget_min = D.effective_timeout_minutes(req, role)
            session.prompt(actual_prompt, timeout=budget_min * 60)
            returncode = 0
            # #273 护栏：回合结束但结果文件未提交（不存在/解析失败）时，同一会话续接一轮，
            # 让 agent 补写/重写 result.json；上限 1 次，剩余预算不足 1 分钟不续接（避免误判超时）。
            state, detail = D.result_state(req)
            if state != "ok":
                remaining = budget_min * 60 - (time.monotonic() - started_mono)
                if remaining >= 60:
                    D.post_event(req["id"], "system",
                               f"回合结束但未提交结果（{detail}），续接一轮（剩余 {int(remaining // 60)} 分钟）",
                               session_id=session.session_id)
                    follow_prompt = build_followup_prompt(role, state, req)
                    _record_start(req, role, follow_prompt, "followup", session,
                                  parent=(run_meta or {}).get("run_key"), redline=False)
                    try:
                        session.prompt(follow_prompt, timeout=remaining)
                    except TurnAborted as e:
                        # #28 输出守卫在续接轮触发：须先于 except Exception 命中（TurnAborted 属 Exception
                        # 子类），否则被吞成「续接异常」丢失中止语义
                        log.warning(f"需求 #{req_id} 续接轮输出守卫中止: {e}")
                        D.post_event(req["id"], "system", f"续接轮输出守卫中止：{e}，取消会话",
                                   session_id=session.session_id)
                        session.cancel()
                        time.sleep(3)
                        aborted = _budget_wrapup(session, req, role)
                    except TimeoutError:
                        timed_out = True
                        D.post_event(req["id"], "system", "续接超时，取消会话",
                                   session_id=session.session_id)
                        session.cancel()
                        time.sleep(3)
                    except Exception as e:
                        log.warning(f"需求 #{req_id} ACP 续接异常: {e}")
                        D.post_event(req["id"], "system", f"续接异常: {e}",
                                   session_id=getattr(session, "session_id", None))
            if not timed_out and not aborted:
                usage = session.collect_usage()
                if usage:
                    # sessions 状态文件继续只存百分比（展示不破坏）；token 数走 agent_runs 落库
                    D.update_session_info(req, key=D.session_key(req, role), context=usage["context_pct"])
        except TurnAborted as e:
            # #28 输出守卫在首轮 prompt 触发：走超时同款收束（取消会话、不采集 usage、进失败重试通道）
            log.warning(f"需求 #{req_id} 输出守卫中止会话: {e}")
            D.post_event(req["id"], "system", f"本轮已中止（输出守卫）：{e}，取消会话",
                       session_id=session.session_id)
            session.cancel()
            time.sleep(3)
            aborted = _budget_wrapup(session, req, role)
            if not aborted:
                returncode = 0      # C2 收尾轮已提交结果：按正常回合收尾
        except TimeoutError:
            timed_out = True
            D.post_event(req["id"], "system", "运行超时，取消会话", session_id=session.session_id)
            session.cancel()
            time.sleep(3)
    except Exception as e:
        log.warning(f"需求 #{req_id} ACP 运行异常: {e}")
        D.post_event(req["id"], "system", f"运行异常: {e}",
                   session_id=getattr(session, "session_id", None))
    finally:
        # #284 进程保持：正常结束（未超时/未异常）且 hold（人工续跑）→ 进程放保持池供下条消息复用；
        # 复用轮（kept）正常结束同样放回；超时/异常路径照旧杀进程不保持
        keep = (session is not None and not timed_out and not aborted and returncode == 0
                and (hold or kept))
        if session:
            session._flush_message()
            if keep:
                try:
                    D._hold_session(agent_session_id or row_id, req_id, role, session, logf)
                except Exception as e:
                    log.warning(f"需求 #{req_id} 保持进程失败: {e}")
                    session.close(kill=False)
                    logf.close()
            else:
                session.close(kill=(timed_out or aborted))
                if returncode != 0 and session.proc.returncode:
                    returncode = session.proc.returncode
                logf.close()
        with active_lock:
            # #388 对象匹配删除：仅当登记仍指向本线程会话时删除——sweep 兜底/失败通道
            # 可能已摘除旧记录或登记新会话，无条件 pop 会把新登记的活跃会话顶掉
            # （审查问题 2：顶掉后 #380 自动重启检查与运行期 sweep 均对重试会话失明）
            if D.active_runs.get(req_id) is arun:
                D.active_runs.pop(req_id, None)
        try:
            # 会话信息栏分支名：统一记录需求 dev 分支（全角色同一 worktree），
            # 不再读主工作区当前分支（主工作区 checkout 状态不影响调研/开发基线）
            branch = D.dev_branch(req)
            D.update_session_info(req, key=D.session_key(req, role), branch=branch,
                                workdir=str(workdir))
            # #421：收尾统一走 _close_session_row（置 idle + 清 turn_step，防右栏残留「工具中」档）
            D._close_session_row(req, role, row_id, "idle",
                               session_id=getattr(session, "session_id", None),
                               branch=branch, context_pct=(usage or {}).get("context_pct"))
        except Exception:
            pass
        # #388 防重：主循环兜底（进程死场景）已补过收尾（行已 idle、run 记录已落、
        # handle_result 已执行）→ 跳过重复 post_run_record（避免失败通道二次重试/重复报表行）
        if D._swept(row_id):
            log.info(f"需求 #{req_id} 会话收尾已由兜底完成（row={row_id}），跳过重复运行记录")
            return True
        # 统一收尾：运行记录（起止时间/状态/工具数/token/百分比）+ 明确的结束事件，
        # 报表不再靠最后一条事件猜结束时间；失败仅降级不阻断 handle_result。
        # #42：缓存命中三态在收尾时统一采集——超时/中止前已完成过请求的会话照记 hit/unknown，
        # 只看缓存采集自身（_llm_turns_done + 已取得字段），与峰值 usage 是否 None 无关
        try:
            usage_ext = session.cache_summary() if session is not None else None
        except Exception as e:
            log.warning(f"需求 #{req_id} 缓存命中汇总失败（不阻断收尾）: {e}")
            usage_ext = None
        # 执行档案字段仅在有档案时传（不给存量 stub 引入新 kwarg）
        run_kw = {"run_meta": {k: run_meta.get(k) for k in ("manifest_path", "prompt_hash", "prefix_hash")}} \
            if run_meta else {}
        D.post_run_record(req, role, session, started_at, time.time(), returncode, timed_out, usage,
                        aborted=aborted, usage_ext=usage_ext, **run_kw)
    if D._swept(row_id):
        log.info(f"需求 #{req_id} 结果处理已由兜底完成（row={row_id}），跳过重复 handle_result")
        return True
    # #11 本轮降级角色事实汇总写入 summary（无论成败，事实先行；无降级时零行为）
    try:
        D._flush_model_degraded_summary(req, role)
    except Exception as e:
        log.warning(f"需求 #{req_id} 模型降级汇总写入失败（不阻断收尾）: {e}")
    try:
        D.handle_result(req, role, returncode, timed_out, started_at, aborted=aborted)
    except Exception:
        # #412（B）：handle_result 异常必留痕——旧实现无 try/except，异常静默丢失且不留 traceback
        log.exception(f"需求 #{req_id} handle_result 异常（role={role}，returncode={returncode}，"
                      f"timed_out={timed_out}）")
    return True

REVIEW_DIFF_MAX_LINES = 2000  # #337：代码审查单仓库 diff 行数上限（截断如实标注，reviewer 可用只读工具补看）

def _gen_review_diff(req):
    """#337：为合并前只读审查生成 dev 分支相对基线的三点 diff 全文，调度器代写产物目录 diff.patch。
    只读 MCP git_diff 白名单仅 stat 类（acp_shared.GIT_DIFF_ALLOWED_ARGS），reviewer 自己拿不到
    范围 diff 内容（reviewer 的 Bash 只读白名单不含 git 内容 diff），故由调度器在拉起 review 前生成；单仓库超 REVIEW_DIFF_MAX_LINES
    截断并如实标注（reviewer 可用 git_diff --stat 定位后 Read 具体文件补看）。在合并前调用，diff 天然有效。"""
    branch = D.dev_branch(req)
    parts = []
    for p in D.PROJECTS[req["project"]]["paths"]:
        try:
            base = D._branch_base(req, p)
            if not base:
                continue
            r = subprocess.run(["git", "diff", f"{base}...{branch}"], cwd=p,
                               capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
            if r.returncode != 0 or not r.stdout.strip():
                continue
            lines = r.stdout.splitlines()
            header = f"===== 仓库 {Path(p).name}（git diff {base}...{branch}）====="
            if len(lines) > REVIEW_DIFF_MAX_LINES:
                header += (f"\n（diff 过长已截断：共 {len(lines)} 行，仅显示前 {REVIEW_DIFF_MAX_LINES} 行；"
                           "可用 git_diff --stat 定位改动文件后 Read 补看）")
                lines = lines[:REVIEW_DIFF_MAX_LINES]
            parts.append(header + "\n" + "\n".join(lines))
        except Exception as e:
            log.warning(f"需求 #{req['id']} 审查 diff 生成失败（{p}）: {e}")
    # #383：无实际 diff（parts 为空）时不写 diff.patch——语义收敛为「文件在=有内容」，前端按
    # exists 渲染分支差异产物行、无内容时不显示；存量「（无 diff 内容）」占位文件不回改
    if parts:
        try:
            art = D.artifact_dir(req)
            art.mkdir(parents=True, exist_ok=True)
            (art / "diff.patch").write_text("\n\n".join(parts))
        except Exception as e:
            log.warning(f"需求 #{req['id']} 写 diff.patch 失败: {e}")

def changed_repos(req, branch):
    """需求 dev 分支相对各仓库基线（主干起点，见 _branch_base）有实际文件变更的仓库名集合。
    单仓库返回 {basename}；分支不存在（diff 非零）/基线无法确定的仓库忽略。"""
    changed = set()
    for p in D.PROJECTS[req["project"]]["paths"]:
        try:
            base = D._branch_base(req, p)
            if not base:
                continue
            r = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=p,
                               capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
            if r.returncode == 0 and any(f.strip() for f in r.stdout.splitlines()):
                changed.add(Path(p).name)
        except Exception as e:
            log.warning(f"需求 #{req['id']} dev 分支变更检测失败（{p}）: {e}")
    return changed

def has_code_changes(req, since):
    """dev done 校验（#113 修正）：需求独立分支相对基线（当前分支 HEAD 三点 diff）有实际文件变更
    即认可 done（新会话接管旧 worktree 时已有未合并提交也算改动）；无任何变更仍拒绝 done；
    多仓库任一仓库有变更即认可。分支不存在（diff 非零）→ False。
    since 参数保留（调用方兼容），实现不再使用——语义从时间窗口改为分支 diff（#113 第二轮 dev
    因只看本次 run 后的提交被判「无代码变更」的修复）。"""
    return bool(D.changed_repos(req, D.dev_branch(req)))

def artifact_ok(req, role):
    """节点 done 契约（artifact/review 类）：对应产物文件存在且非空。产物名取自配置派生门面
    role_capability(role, "artifact")（唯一源 flows.json roles.<id>.artifact，#419）。
    前置槽位契约（suggested_flow/repos）不在产物存在性上拦截：缺推荐由 _pre_stage_done 兜底
    默认流程 + 留痕，repos 清单外转人工确认。"""
    art = D.artifact_dir(req)
    expected = role_capability(role, "artifact")
    if not expected:
        return True, ""
    f = art / expected
    if not f.exists() or not f.read_text(errors="replace").strip():
        return False, f"缺少产物 {expected}"
    return True, ""

def _extract_todos(result):
    """从 result.json 提取可选字段 todos（#279）：字符串数组，agent 声明的遗留人工操作，纯展示。
    兼容旧格式宁缺勿滥：非 dict / 缺失 / 非列表 → None（视为未声明，不报错）；
    元素过滤为去空白非空字符串，非法元素忽略；原始列表为空 → 返回 []（显式声明无待办）；
    有元素但全部非法 → None（视为未声明，不覆盖已声明的值）。"""
    if not isinstance(result, dict):
        return None
    todos = result.get("todos")
    if not isinstance(todos, list):
        return None
    cleaned = [t.strip() for t in todos if isinstance(t, str) and t.strip()]
    if not cleaned and todos:
        return None
    return cleaned

# #400：追问护栏阈值（单调计数，无任何清零/复位点——响应不清零语义下计数恒为 1、上限不可达，已废弃）
CONFIRM_ROUNDS_MAX = 5

# #400：网页确认动作白名单（按 kind 固定生成按钮，agent 无法声明）；其余 kind 首期不走本通道
# #411：review_gate 三档——pass 仍合并 / rework 转开发（新）/ reject 转暂缓（原「否决合并」改名）
WEB_CONFIRM_ACTIONS = {
    "review_gate": ("pass", "rework", "reject"),
    "confirm_override": ("pass", "reject", "reject_later"),
    # 评审轮次超限的人工裁决与飞书 y/n 对齐；普通提问的 pass/rework 由 API 层拒绝。
    "confirm": ("pass", "rework", "proceed", "stop_later"),
}


def _parse_confirm_options(result):
    """#400：need_confirm result.json 可选 options（纯对话类选项，fail-closed）——
    仅接受 label/hint 文本，未知键（含 action 动作码）自然丢弃；非法/超限置空不阻断落表。
    返回 None 表示未声明（页面退化 question 全文 + 回复入口）。"""
    raw = (result or {}).get("options")
    if not isinstance(raw, list):
        return None
    options = []
    for item in raw:
        if len(options) >= 5:
            break
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")[:20].strip()
        if not label:
            continue
        hint = str(item.get("hint") or "")[:50].strip()
        options.append({"label": label, "hint": hint} if hint else {"label": label})
    return options or None


def _register_need_confirm(req_id, entry, role):
    """#400：need_confirm 统一落表 + 追问护栏（confirm_rounds 单调计数，无清零点）。
    - 每次落表 +1；N ≤ CONFIRM_ROUNDS_MAX 正常（通知带「第 N 轮待确认」）
    - N > MAX 仍落表 + entry["over_limit"]=True，不自动转 later（明示出口：飞书 y/n、
      网页「停止追问转暂缓」按钮）；护栏只影响落表标记与文案，不拦截任何用户通道。
    返回落表轮次 N（调用方构造通知文案）。"""
    req_id = str(req_id)
    with D.state_lock:
        rounds = int(D.state.setdefault("confirm_rounds", {}).get(req_id, 0)) + 1
        D.state["confirm_rounds"][req_id] = rounds
        entry["role"] = role
        if rounds > CONFIRM_ROUNDS_MAX:
            entry["over_limit"] = True
        # #27：唯一落表出口（锁内纯内存：裁剪 + 写入 + 回读校验），留痕在出锁后
        ok = D._put_pending_confirm(req_id, entry)
    if not ok:
        D._pending_source_mismatch_note(req_id, entry, "need_confirm 落表")
    D.save_state()
    return rounds


def _notify_need_confirm(req, question, role, rounds):
    """#400：need_confirm 通知（问责护栏轮次标注），卡片/降级文案统一出口"""
    D.notify_req_card("need_confirm", req, question=question, phase=role,
                    round_n=rounds, over_limit=rounds > CONFIRM_ROUNDS_MAX)


