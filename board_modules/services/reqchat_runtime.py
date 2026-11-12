"""#32 Phase 4：reqchat_runtime 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import PLAYWRIGHT_TOOL_PREFIX
from acp_shared import TAPD_TOOL_PREFIX
from acp_shared import append_kill_redline
from prompt_i18n import prompt_lang, tr
from pathlib import Path
import tempfile
import threading
import time

from board_modules.runtime import resolve
from acp_modules.session_errors import chat_session_error
from board_modules.services.chat_runtime import CHAT_USAGE_TIMEOUT, REQ_NOTES_FMT_HINT, _chat_roots, _chat_workdir, _project_mysql_env
from board_modules.services.mcp_servers import MaintainMcpServer

# ==================== 需求助手（原「需求对话」，#302 引入，#326 改中间栏置顶入口+右栏独立视图，独立于调度器） ====================
REQ_CHAT_TIMEOUT = 10 * 60                     # 单轮 prompt 超时（秒）
REQ_CHAT_PARALLEL_LIMIT = 3                    # 全站最多 N 个需求对话并行跑一轮（running 占名额）
REQ_CHAT_PARALLEL_FULL_MSG = f'最多 {REQ_CHAT_PARALLEL_LIMIT} 个需求助手并行，请等待其中一个结束或取消后再继续'
REQ_CHAT_TOOL_SERVER = "reqboard-maintain"     # 维护工具 MCP server 名（读看板数据 + 显式授权写工具）
# 需求对话事件里「工具调用摘要」渲染最长长度（与调度器事件面板一致的量级）
REQ_CHAT_EVENT_MAX = 4000
REQ_CHAT_STATUS_LABEL = {"later": "暂缓", "open": "待开发", "doing": "进行中", "done": "已完成",
                         "route": "通道待确认", "review": "评审中"}

reqchat_lock = threading.RLock()  # 需求对话状态机/运行时共用锁
reqchat_runtime = {}  # req_chat_id -> {"session": ACPSession, "logf": file, "mcp": MaintainMcpServer}
reqchat_booting = set()  # 正在建立/恢复 kimi 会话的 req_chat_id（防并发重复拉起）


def _reqchat_write_roots(workdir):
    """#434 需求助手写白名单：项目工作目录（#355 合并授权所需）+ 临时目录（/tmp 与系统
    TMPDIR，与 dispatcher.dev_write_roots 同款口径）。评审/查证要写探针与草稿文件，
    此前白名单只有 workdir，越界写会被 #303 取消整轮（2026-09-19 现场）。"""
    roots = [workdir, "/tmp"]
    try:
        roots.append(tempfile.gettempdir())
    except Exception:  # noqa: BLE001 - 取不到系统临时目录不影响 /tmp 兜底
        pass
    return roots


# ==================== 需求助手（#302）：独立于调度器；#326 起为中间栏置顶入口 + 右栏独立视图 ====================
# 生命周期：created(占位) -> running(首轮) -> idle(可对话) <-> running(每轮)
#           interrupted(中断可继续) / failed(建立失败) / closed(已放弃，可新建)
# 每轮结束关闭 kimi 进程（低频维护场景不保持进程），session_id 保留，下轮 resume 续上下文。
# 写权限边界：默认只读（项目代码 + 看板数据）；显式授权写 = 新建需求卡 / 改卡片字段 / 写产物目录 /
# 发 TAPD 评论，全部经 MaintainMcpServer 工具（fail-closed 服务端校验；建卡 project 绑定本需求所属项目、
# status 限 later/open 默认 later、每对话至多 1 张卡幂等）；合并授权后另放开项目工作目录内文件写
# 与 git merge/checkout/switch/stash 受限形态（解决代码合并冲突，仅用户明确要求时；Bash 写仍全拒）。

def _reqchat_set(req_chat_id, **fields):
    with resolve("get_db")() as conn:
        fields["updated_at"] = resolve("now_str")()
        conn.execute(f"UPDATE req_chats SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), req_chat_id))
        conn.commit()
        row = conn.execute("SELECT req_id FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
    resolve("_sse_publish")('reqchat', req_id=row['req_id'] if row else None)   # #416 发布点 6


def _reqchat_running_count():
    """当前全站 running（并行占名额）的需求对话数。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM req_chats WHERE status = 'running'").fetchone()
    return row["n"] if row else 0


def _reqchat_add_event(req_chat_id, kind, content):
    with resolve("get_db")() as conn:
        conn.execute("INSERT INTO req_chat_events (req_chat_id, kind, content, ts) VALUES (?, ?, ?, ?)",
                     (req_chat_id, kind, str(content)[:REQ_CHAT_EVENT_MAX], resolve("now_str")()))
        conn.commit()
        row = conn.execute("SELECT req_id FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
    resolve("_sse_publish")('reqchat', req_id=row['req_id'] if row else None)   # #416 发布点 3


def _reqchat_event_sink(req_chat_id):
    """ACPSession 事件注入回调：写 req_chat_events 表（kind message/tool/system）"""
    def sink(req_id, kind, content, session_id=None):
        _reqchat_add_event(req_chat_id, kind, content)
    return sink


def _reqchat_current(req_id):
    """该需求最新一条对话会话（任意状态）；无任何对话返回 None。"""
    with resolve("get_db")() as conn:
        return conn.execute(
            "SELECT * FROM req_chats WHERE req_id = ? ORDER BY id DESC LIMIT 1", (req_id,)).fetchone()


def _reqchat_active(req_id):
    """该需求最新一条「非终态」对话（created/idle/running/interrupted）；无则 None。"""
    with resolve("get_db")() as conn:
        return conn.execute(
            "SELECT * FROM req_chats WHERE req_id = ? AND status IN "
            "('created', 'idle', 'running', 'interrupted') ORDER BY id DESC LIMIT 1", (req_id,)).fetchone()


def _reqchat_create(req_id):
    """新建对话占位行（created）。"""
    with resolve("get_db")() as conn:
        cur = conn.execute("INSERT INTO req_chats (req_id, status, created_at, updated_at) VALUES (?, 'created', ?, ?)",
                           (req_id, resolve("now_str")(), resolve("now_str")()))
        conn.commit()
    return cur.lastrowid


def _reqchat_system_prompt(req_id, model=None, req_chat_id=None):
    """需求对话系统提示词（首轮注入）：角色/需求上下文/权限边界/工具纪律。
    model=本会话实时底层模型名（#434：异模检查与评审署名口径，agent 不用自己去读 config.toml）；
    req_chat_id=对话 id（给出本会话专属临时目录，避免多会话在 /tmp 撞名）。两者缺省不影响既有调用。"""
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT * FROM requirements WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        return "（需求不存在）"
    project = req["project"]
    workdir = _chat_workdir(project)
    req_dir = resolve("ARTIFACTS_ROOT") / project / f"req-{req_id}"
    status_cn = REQ_CHAT_STATUS_LABEL.get(req["status"], req["status"])
    mode_cn = {"auto": "全自动", "semi": "半自动"}.get(req["mode"] or "", req["mode"] or "未定")
    notes = (req["notes"] or "").strip() or "（无）"
    summary = (req["summary"] or "").strip() or "（无）"
    tmp_dir = f"/tmp/req_board_reqchat_{req_chat_id}/" if req_chat_id else "/tmp"
    if prompt_lang(project) == "en":
        from board_modules.services.chat_prompts_en import req_chat_prompt
        return req_chat_prompt(req_id=req_id, req=req, project=project, workdir=workdir, req_dir=req_dir,
                               model=model, tmp_dir=tmp_dir)
    return (
        "你是需求看板（req-board）的「需求助手」，帮助用户了解与维护这个需求。\n"
        f"当前需求：#{req_id}「{req['title']}」（项目 {project}，状态 {status_cn}，通道 {mode_cn}）\n"
        f"需求详情（notes）：\n{notes}\n"
        f"开发总结（summary）：\n{summary}\n"
        f"产物目录：{req_dir}（用 list_artifacts/read_artifact 工具查看各阶段产物：需求分析/方案/技术设计/自测/验收）\n"
        f"工作目录：{workdir}\n"
        f"本会话底层模型：{model or '未知'}（评审记录 reviewer 由服务端按实时模型填写，不要自己写；"
        "评审的异模检查以本行为准，不要去读 config.toml 等家目录配置）\n"
        f"本会话临时目录：{tmp_dir}（临时文件写这里，避免与其它会话撞名）\n"
        "可用能力：\n"
        "- 只读：查看需求与产物（get_requirement/list_artifacts/read_artifact）、"
        "回看调度器执行过程（list_exec_events）、只读工具了解项目代码（list_dir/read_file/db_query/git_log）、"
        "Bash 只读命令（ls/cat/date 等）与 git 只读子命令（status/log/diff/show/branch/worktree list 等）；\n"
        "- 只读：tapd MCP 工具（mcp__tapd__get_* 白名单，可查 TAPD 需求/缺陷/评论/迭代/用户等，写操作会被拒绝）；\n"
        "- 默认只读：不修改任何东西。仅当用户**明确要求**修改时，才可使用写工具：\n"
        "  · create_requirement：在项目 " + project + " 下新建需求卡（title/notes；" + REQ_NOTES_FMT_HINT +
        "；status 默认 later 暂缓、仅用户明确要求「开始开发」时才用 open；"
        "本对话同标题不重复建卡（幂等），不同标题可建多张）；"
        "先输出 title/notes 初稿，用户明确确认「建卡」后再调用；\n"
        "  · update_requirement：修改需求详情(notes)/开发总结(summary)/标题(title)/状态(status，"
        "仅 later(暂缓) 与 open(待开发) 两值；置 open 会让调度器把它捞去自动开发，"
        "只有用户明确说「开始开发」，或评审终审已拍板通过时才可用）；\n"
        "  · write_artifact：写入本需求产物目录（如修订分析/方案文档）；\n"
        "  · post_review：追加本轮评审记录（仅用户明确要求评审时使用；content 必填，"
        "分段规则见 req-board-review skill 的写回规范；verdict 可选，限 pass/block/downgrade/close）。"
        "轮次(round)与署名(reviewer)都由服务端自动填，不要自己写；记录只追加、不能改删；"
        "评审过程与结论禁止写进需求详情/开发总结（那两栏是 prompt 通道）；\n"
        "  · tapd_comment：在 TAPD 上对需求/缺陷发表评论（workspace_id + entry_id + comment）；\n"
        "  · git add/commit：在项目工作目录做本地提交（仅用户明确要求时；先 git status/diff 自查，"
        "勿提交 .env 等敏感或无关文件；push/reset/rebase/amend/cherry-pick/revert 等一律被拒）。\n"
        "处理代码合并（#355，同样仅用户**明确要求**时）：可用 git merge <分支>（常用 --no-ff/--no-edit；"
        "冲突可 --abort 回滚，解决后 --continue --no-edit 或 add+commit 收尾）、"
        "git checkout/switch <分支>（仅切分支，禁 -b/-f/-- 路径形态）、git stash（暂存）/pop/apply（恢复）；"
        "冲突文件用内置编辑工具解决（仅限本项目工作目录内、只改冲突所需最小内容，禁止动无关文件）。\n"
        "善后工具（同样仅当用户**明确要求**善后操作时才使用，默认只读纪律不变）：\n"
        "  · restart_dispatcher：重启调度器让 dispatcher.py 改动生效（launchd label 方式，不猜 PID；"
        "有运行中 agent 时会拒绝，属正常防护，不要反复重试）；\n"
        "  · restart_project_service：按 projects.json 配方重启白名单项目服务"
        "（service=项目名，如 my-app/api-server/web）；\n"
        "  · read_logs：只读 tail 白名单日志（调度器/agent/看板日志）排查问题，"
        "可用 lines/keyword 参数，越权路径会被拒绝。\n"
        "Bash 用法：命令已在项目目录执行（无需 cd；如要 cd 只能用 `cd <项目目录> && git <命令>` 单条拼接，"
        "禁止 echo/管道/分号；git 命令支持 `cd ... && git add ... && git commit -m ...` 串联）。\n"
        "代码自验（#387，仅用户明确要求自验时使用，默认只读纪律不变）：可执行 `python3 -m py_compile <项目内 .py>` "
        "与 `<项目>/.venv/bin/python -m unittest discover -s <tests目录> -v` 两形态（flag 仅限 -h/-v/-q/-f/-c/-b/-s/-t/-p/-k）；"
        "python3 -c/任意 python 脚本/其它模块（pytest 等）/重定向/管道/命令拼接一律被拒（fail-closed），"
        "输出直接读取，不要重定向到文件。\n"
        "文件写入边界（机制，越界必被拒）：只有本项目工作目录内与本机临时目录"
        f"（{tmp_dir}，含系统 TMPDIR）可写，其它绝对路径一律拒绝——越界写会被拦截并可能取消本轮，"
        "不要试探家目录配置（如 ~/.kimi-code）等路径。\n"
        "写入纪律（默认不写文件）：例外仅两类——用户明确要求处理代码合并冲突时，编辑本项目工作目录内的"
        "冲突文件（最小改动，改完 git status/diff 自查）；以及用户明确要求修改时用上面的写工具落库。"
        "需要留存的文档一律用 write_artifact 落产物目录，不写仓库文件；"
        "禁止用 /tmp 中转绕开这条纪律（如把项目代码拷到 /tmp 改）。\n"
        "落库一律走上面的 MCP 写工具：本会话没有 curl、不能运行任意脚本与命令"
        "（python 仅 py_compile 与 venv unittest 两形态），Bash 写操作无论何时一律被拒。\n"
        "本会话无交互终端，AskUserQuestion 等提问类工具调用后会立即返回未作答（dismiss），禁止使用；"
        "需要用户决策时直接在对话消息文本里提问。\n"
        "时间管理：本轮约 10 分钟超时，用 `date` 关注实际耗时；接近时限时先输出当前结论保底再继续。\n"
        "输出规范：结论先行、能短则短，单条回复一般控制在两三百字内；禁止开场白、客套与复述需求内容；"
        "信息多用 - 列表；复杂问题先给结论+关键依据，再问是否需要展开；"
        "对话正文不要用代码块包裹，代码块必须成对闭合。\n"
        "独立判断：用户提出的修改/思路只是输入，有实质异议时结论先行直说，给依据与替代建议；"
        "用户坚持后按其确认的方向执行。"
    )


def _close_reqchat_runtime(rt):
    """关闭一个 reqchat_runtime 条目（会话进程 + 日志文件 + MCP server），幂等。"""
    try:
        rt["session"].close(kill=True)
    except Exception:
        pass
    try:
        rt["logf"].close()
    except Exception:
        pass
    try:
        rt["mcp"].close()
    except Exception:
        pass


def _reqchat_session_alive(rt):
    try:
        return rt is not None and rt["session"].proc.poll() is None
    except Exception:
        return False


def _reqchat_mark_stale(req_chat_id):
    """孤儿 running（进程已失效）：转 interrupted，留待继续对话时自动恢复。"""
    _reqchat_set(req_chat_id, status="interrupted", interrupt_source="process_lost",
                 error="对话进程已失效，可继续对话恢复")
    _reqchat_add_event(req_chat_id, "system", "对话进程已失效，会话中断（可继续对话）")


def _reqchat_boot_sync(req_chat_id, req_id, resume_session_id=None):
    """同步建立/恢复 kimi 会话（run 线程内）：只读工具 + 维护写工具注入。
    成功返回 runtime 条目；失败标 failed（cancel/close 场景保持原状态）并返回 None。"""
    with resolve("get_db")() as conn:
        req = conn.execute("SELECT * FROM requirements WHERE id = ?", (req_id,)).fetchone()
    project = req["project"]
    workdir = _chat_workdir(project)
    mcp = MaintainMcpServer(req_chat_id, req_id)
    logf = open(f"/tmp/req_board_reqchat_{req_chat_id}.log", "a")
    session = None
    try:
        mysql_env = _project_mysql_env(project)
        session = resolve("ACPSession")(
            req_chat_id, workdir, logf, "analysis", Path("/nonexistent-reqchat-artifacts"),
            project=project, event_sink=_reqchat_event_sink(req_chat_id), readonly=True,
            extra_allow_prefixes=[f"mcp__{REQ_CHAT_TOOL_SERVER}__", PLAYWRIGHT_TOOL_PREFIX, TAPD_TOOL_PREFIX],
            extra_mcp_servers=[{"type": "http", "name": REQ_CHAT_TOOL_SERVER, "url": mcp.url, "headers": []}],
            allow_fs_write=True, readonly_roots=_chat_roots(project, workdir),
            # #355 需求助手合并授权：放开项目工作目录内文件写（内置写/fs 写白名单 =
            # workdir）+ git merge/checkout/switch/stash 受限形态（解决代码合并冲突所需；
            # 默认只读纪律由 prompt 约束「仅用户明确要求时」；push/reset/rebase 等仍拒）
            # #434：白名单加临时目录（/tmp + 系统 TMPDIR，与 dispatcher dev_write_roots 同款口径）——
            # 评审/查证过程要写探针与草稿文件，此前越界即取消本轮；语义见 acp_shared 三处闸门
            write_roots=_reqchat_write_roots(workdir), deny_builtin_write=False,
            allow_git_commit=True, allow_git_merge=True,
            # #387 需求助手自验授权：受限 python 白名单（仅 py_compile/venv unittest discover
            # 两形态；python3 -c/任意脚本/重定向/拼接仍被 token 层与白名单拒，fail-closed；
            # roots 独立集合 = workdir + 项目各仓库，不放宽到家目录）
            allow_python_safe=True,
            python_roots=[workdir] + (resolve("PROJECT_PATHS").get(project) or []),
            mysql_config={"env_file": mysql_env} if mysql_env else None,
            # #108：经 wrapper 启动（exec -a 覆盖 argv[0]），ps/pgrep 可辨 kimi-chat-acp，防误杀
            kimi_bin=str(resolve("BASE_DIR") / "scripts" / "kimi-chat-acp"))
        with reqchat_lock:
            reqchat_runtime[req_chat_id] = {"session": session, "logf": logf, "mcp": mcp}
        resumed = session.start(workdir, resume_session_id=resume_session_id)
        # 保存 kimi 会话 id / 模型 / 推理深度：下轮 resume 续上下文（进程每轮关闭，会话记录保留）
        _reqchat_set(req_chat_id, session_id=session.session_id, model=session.model_name,
                     thinking=session.thinking, interrupt_source=None, error=None)
        _reqchat_add_event(req_chat_id, "system",
                           f"对话已建立（模型 {session.model_name or '默认'}）" if not resumed
                           else "对话已恢复（上下文延续）")
        return {"session": session, "logf": logf, "mcp": mcp}
    except Exception as e:
        message = chat_session_error(e, project)
        if session is not None:
            try:
                session.close(kill=True)
            except Exception:
                pass
        with reqchat_lock:
            reqchat_runtime.pop(req_chat_id, None)
        with resolve("get_db")() as conn:
            cur = conn.execute("SELECT status FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
        if cur and cur["status"] not in ("interrupted", "closed"):
            _reqchat_add_event(req_chat_id, "system", message)
            _reqchat_set(req_chat_id, status="failed", error=message)
        try:
            mcp.close()
        except Exception:
            pass
        try:
            logf.close()
        except Exception:
            pass
        return None


def _reqchat_abort(req_chat_id, message, source):
    """中断当前轮（超时/异常）：杀进程、标 interrupted（可继续对话）。"""
    with reqchat_lock:
        rt = reqchat_runtime.pop(req_chat_id, None)
    if rt is not None:
        _close_reqchat_runtime(rt)
    with resolve("get_db")() as conn:
        cur = conn.execute("SELECT status FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
    if cur and cur["status"] == "interrupted":
        return  # 用户已取消，不重复置状态
    _reqchat_set(req_chat_id, status="interrupted", interrupt_source=source, error=message)
    _reqchat_add_event(req_chat_id, "system", message)


def _reqchat_run_round(req_chat_id, text):
    """一轮完整执行（run 线程）：boot（首轮/恢复续上下文）→ 注入提示词 → prompt → 收尾。
    兜底：任何提前返回/未捕获异常都不允许把会话留在 running。"""
    with reqchat_lock:
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT * FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
        if row is None:
            return
        reqchat_booting.add(req_chat_id)
        _reqchat_set(req_chat_id, status="running", interrupt_source=None, error=None)
        req_id = row["req_id"]
        resume_sid = row["session_id"]
    rt = None
    try:
        rt = _reqchat_boot_sync(req_chat_id, req_id, resume_session_id=resume_sid)
        if rt is None:
            return  # boot 失败已标 failed（或 cancel 场景保持 interrupted）
        # boot 后复查：期间被 cancel/close 则不再执行本轮
        with reqchat_lock:
            with resolve("get_db")() as conn:
                cur = conn.execute("SELECT status FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
            if cur is None or cur["status"] != "running":
                return
        session = rt["session"]
        with resolve("get_db")() as conn:
            first = conn.execute(
                "SELECT 1 FROM req_chat_events WHERE req_chat_id = ? AND kind = 'message' LIMIT 1",
                (req_chat_id,)).fetchone() is None
        if first:
            # 首轮注入系统提示词 + 进程安全红线（续跑会话已在上下文内，不重复注入）
            with resolve("get_db")() as conn:
                prow = conn.execute("SELECT project FROM requirements WHERE id = ?", (req_id,)).fetchone()
            lang = prompt_lang(prow["project"] if prow else None)
            text = append_kill_redline(
                _reqchat_system_prompt(req_id, model=session.model_name, req_chat_id=req_chat_id),
                session.proc.pid, lang) + tr(lang, "\n\n用户消息：\n") + text
        try:
            session.prompt(text, timeout=REQ_CHAT_TIMEOUT)
            session._flush_message()
        except TimeoutError:
            try:
                session.cancel()
                time.sleep(2)
            except Exception:
                pass
            _reqchat_abort(req_chat_id, "本轮超时，对话已中断（可继续对话）", "timeout")
            return
        except Exception as e:
            _reqchat_abort(req_chat_id, f"本轮异常: {e}", "error")
            return
        _reqchat_set(req_chat_id, status="idle")
        _reqchat_add_event(req_chat_id, "system", "本轮完成")
    except Exception as e:
        _reqchat_abort(req_chat_id, f"本轮异常结束: {e}", "error")
    finally:
        with reqchat_lock:
            reqchat_booting.discard(req_chat_id)
        if rt is not None:
            # #383：关进程前采集上下文用量（一轮结束 = 最新占用，头部展示「used xx%」），
            # 失败不阻断（超时/异常路径进程可能已不可用，静默跳过）
            try:
                usage = rt["session"].collect_usage(timeout=CHAT_USAGE_TIMEOUT)
                if usage and usage.get("context_pct"):
                    _reqchat_set(req_chat_id, context_pct=usage["context_pct"])
            except Exception:
                pass
            _close_reqchat_runtime(rt)


def _reqchat_start_round(req_chat_id, text):
    """统一驱动一轮（prompt/resume 共用）：锁内决策 + 起 run 线程。
    进程失效（board 重启/子进程崩溃）时自动转 interrupted 并 resume 续上下文后排队；
    并行上限：非 running 会话起新轮次占用一个全局并行名额（REQ_CHAT_PARALLEL_LIMIT）。"""
    stale_rt = None
    with reqchat_lock:
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT * FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
        if row is None:
            return {"error": "对话不存在"}, 404
        status = row["status"]
        if status == "running":
            rt = reqchat_runtime.get(req_chat_id)
            if (rt is not None and _reqchat_session_alive(rt)) or req_chat_id in reqchat_booting:
                return {"error": "对话正在运行，请等待本轮结束"}, 409
            # 孤儿 running：进程已失效，转 interrupted 自动恢复
            stale_rt = reqchat_runtime.pop(req_chat_id, None)
            _reqchat_mark_stale(req_chat_id)
        elif status in ("failed", "closed"):
            return {"error": f"对话状态 {status} 不可继续（可新建对话）"}, 400
        else:
            if _reqchat_running_count() >= REQ_CHAT_PARALLEL_LIMIT:
                return {"error": REQ_CHAT_PARALLEL_FULL_MSG}, 429
        threading.Thread(target=resolve("_reqchat_run_round"), args=(req_chat_id, text), daemon=True).start()
    if stale_rt:
        _close_reqchat_runtime(stale_rt)
    return {"success": True}, 202


def _reqchat_cancel(req_chat_id):
    """取消进行中的一轮（杀进程，标 interrupted 可继续）；boot 中由 run 线程复查放弃。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT status FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
    if row is None:
        return {"error": "对话不存在"}, 404
    if row["status"] == "running":
        with reqchat_lock:
            rt = reqchat_runtime.get(req_chat_id)
        if rt is not None:
            try:
                rt["session"].cancel()
                time.sleep(1)
                rt["session"].close(kill=True)
            except Exception:
                pass
            with reqchat_lock:
                reqchat_runtime.pop(req_chat_id, None)
            _close_reqchat_runtime(rt)
        _reqchat_set(req_chat_id, status="interrupted", interrupt_source="user_cancel", error="用户取消本轮")
        _reqchat_add_event(req_chat_id, "system", "已取消本轮，对话中断（可继续对话）")
    return {"success": True}, 200


def _reqchat_close(req_chat_id):
    """明确放弃对话（终态 closed，可再新建）：杀进程、保留事件历史。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT status FROM req_chats WHERE id = ?", (req_chat_id,)).fetchone()
    if row is None:
        return {"error": "对话不存在"}, 404
    with reqchat_lock:
        rt = reqchat_runtime.pop(req_chat_id, None)
    if rt is not None:
        try:
            rt["session"].close(kill=True)
        except Exception:
            pass
        _close_reqchat_runtime(rt)
    if row["status"] != "closed":
        _reqchat_set(req_chat_id, status="closed")
        _reqchat_add_event(req_chat_id, "system", "对话已关闭")
    return {"success": True}, 200


def _recover_req_chats():
    """board 启动时将残留 running/idle 标为可继续的中断（不自动恢复，详情页手动继续）。"""
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT id FROM req_chats WHERE status IN ('running', 'idle') ORDER BY id DESC"
        ).fetchall()
        conn.execute(
            "UPDATE req_chats SET status = 'interrupted', interrupt_source = 'restart', "
            "error = '看板服务重启，对话等待继续', updated_at = ? "
            "WHERE status IN ('running', 'idle')", (resolve("now_str")(),))
        conn.commit()
    for row in rows:
        _reqchat_add_event(row["id"], "system", "看板服务已重启，对话中断（可继续对话）")


def _cleanup_reqchat_procs():
    """board 退出时清理需求对话 kimi 子进程（launchd KeepAlive 场景防孤儿）"""
    with reqchat_lock:
        items = list(reqchat_runtime.values())
    for rt in items:
        _close_reqchat_runtime(rt)
