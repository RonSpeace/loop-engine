"""#32 Phase 4：chat_runtime 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_modules.session_errors import chat_session_error
from acp_shared import KIMI
from acp_shared import PLAYWRIGHT_TOOL_PREFIX
from acp_shared import TAPD_TOOL_PREFIX
from acp_shared import append_kill_redline
from prompt_i18n import prompt_lang, tr
from pathlib import Path
import json
import re
import threading
import time

from board_modules.runtime import resolve

# ==================== 建卡对话（req-107：新增需求页改为 agent 会话框） ====================
# 一个会话绑定一个项目（每项目最多 1 个活跃会话，全站最多 3 个并行 running）；
# board 进程内起 kimi acp（共享模块 ACPSession，事件注入写 chat_events），
# 不走调度器轮询。安全：读 = 项目目录 + 只读 MCP 工具（fail-closed）；写 = 仅 create_requirement
# 工具（校验 title/project/notes 且 project == 会话绑定项目，幂等防重复建卡）；不写任何仓库文件。
CHAT_TIMEOUT = 10 * 60           # 单轮 prompt 超时（秒）
CHAT_PARALLEL_LIMIT = 3                    # 全站最多 N 个项目并行跑一轮（running 状态占名额）
CHAT_PARALLEL_FULL_MSG = f'最多 {CHAT_PARALLEL_LIMIT} 个项目并行会话，请等待其中一个结束或取消后再继续'
CHAT_AUTO_RESUME_SOURCES = ("restart", "process_lost", "timeout", "error")
CHAT_USAGE_TIMEOUT = 5           # 建卡收尾采集 /usage 的超时（秒），失败不阻断会话收尾
CHAT_MATERIAL_MAX_FILES = 5                  # 单轮累计上限（前端拦截 + 服务端闸门双保险）
CHAT_MATERIAL_MAX_BYTES = 10 * 1024 * 1024   # 单文件硬上限（按**读入字节**判定，不信任 content_length）
CHAT_MATERIAL_EXTS = ('.html', '.htm')       # 仅 html 原型
CHAT_MATERIAL_READ_CAP = 1024 * 1024         # agent read_file 单文件上限（超此值仅前端提示，不拦上传，方案 D2）
CHAT_MATERIAL_NAME_MAX = 200                 # 文件名长度上限
CHAT_MATERIAL_BODY_LIMIT = CHAT_MATERIAL_MAX_FILES * CHAT_MATERIAL_MAX_BYTES + 1024 * 1024
DRAFT_RE = re.compile(r"```draft\s*\n(.*?)\n```", re.S)  # agent 草稿块（```draft {json 或 yaml} ```）
CREATE_TOOL_SERVER = "reqboard-create"     # 建卡会话唯一的写工具 MCP server 名
# #381：notes 统一排版约定——三通道建卡（skill/聊天建卡/需求助手）提示词共用同一段文案，
# 与 ~/.agents/skills/req-board-card/SKILL.md 的「notes 排版提示」措辞保持一致（外部 md 无法 import，人工同步）
REQ_NOTES_FMT_HINT = ('用空行分段——背景、方案要点、验收标准、生效方式各一段；'
                      '要点用 "- " 列表逐条分行；不要一整段不换行'
                      '（详情页按空行分段渲染成段落/列表，超长单行会挤成一坨）；'
                      '只写「要做什么」——祈使句与事实陈述，不要写讨论过程与「为什么不做」'
                      '（禁止「待用户确认」「已确认不做」「待定」这类协商痕迹、被否决方案的理由、'
                      'commit 号等历史信息；范围排除写成「不提供：X、Y」；'
                      '实现雷区用事实+证据，如「删掉 trae 会让 AI Coding 总成本少算 ¥22,133.52」）')


def _kimi_code_version():
    """抓取当前 kimi code 版本号（如 '0.34.0'），供建卡会话收尾的 harness 字段使用。"""
    try:
        out = resolve("subprocess").run([KIMI, "--version"], capture_output=True, text=True, timeout=10)
        text = (out.stdout or out.stderr or "").strip()
        return text.splitlines()[0] if text else None
    except Exception:
        return None


def _chat_current_branch(project):
    """读取项目当前分支（多路径项目取第一个有 git 分支的仓库）。"""
    for p in resolve("PROJECT_PATHS").get(project) or []:
        try:
            out = resolve("subprocess").run(["git", "branch", "--show-current"], cwd=p,
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if out:
                return out
        except Exception:
            pass
    return None


def _finalize_chat_session(chat_id, session, project):
    """建卡会话收尾：采集上下文用量/kimi 版本/分支并落库（幂等，失败不阻断）。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT finalized FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
    if row and row["finalized"]:
        return
    fields = {"finalized": 1}
    try:
        usage = session.collect_usage(timeout=CHAT_USAGE_TIMEOUT)
        if usage:
            fields.update(context_pct=usage["context_pct"],
                          context_tokens=usage["context_tokens"],
                          max_tokens=usage["max_tokens"])
    except Exception:
        pass
    ver = resolve("_kimi_code_version")()
    if ver:
        fields["harness"] = f"kimi code {ver}"
    branch = resolve("_chat_current_branch")(project)
    if branch:
        fields["branch"] = branch
    fields["workdir"] = _chat_workdir(project)
    try:
        _chat_set(chat_id, **fields)
    except Exception:
        pass


def _finalize_if_created(chat_id, session):
    """会话已建卡（status=created）时执行收尾；其它状态不动。"""
    with resolve("get_db")() as conn:
        row = conn.execute(
            "SELECT status, project FROM chat_sessions WHERE id = ?", (chat_id,)
        ).fetchone()
    if row and row["status"] == "created":
        _finalize_chat_session(chat_id, session, row["project"])


chat_lock = threading.RLock()  # 建卡会话状态机/运行时共用锁（RLock：自愈路径可嵌套持有）
chat_runtime = {}  # chat_id -> {"session": ACPSession, "logf": file}
chat_booting = set()  # 正在建立/恢复 kimi 会话的 chat_id（防并发重复拉起 boot 线程）


def _chat_system_prompt(project):
    """建卡会话系统提示词（首轮拼接在用户消息前）：角色/工具边界/草稿格式/建卡流程。
    #5：项目配置了 goal 时在绑定行后注入「项目目标（goal）：…」行，无 goal/未注册（含 guide
    展示态 "{project}"）整行不出现；数据源 _auto_projects_config()（动态读、fail-safe）。"""
    if prompt_lang(project) == "en":
        from board_modules.services.chat_prompts_en import create_card_prompt
        goal = (resolve("_auto_projects_config")().get(project) or {}).get("goal") or ""
        return create_card_prompt(project, _chat_workdir(project), goal, bool(_project_mysql_env(project)))
    mysql_hint = (
        "你可以用 mysql_query 工具只读查询该项目 MySQL 生产库"
        "（SELECT/EXPLAIN/WITH/VALUES 等只读语句，最多 200 行）。\n"
        if _project_mysql_env(project) else "")
    goal = (resolve("_auto_projects_config")().get(project) or {}).get("goal") or ""
    goal_hint = f"项目目标（goal）：{goal}\n" if goal else ""
    return (
        "你是需求看板（req-board）的建卡助手。当前绑定项目：" + project +
        "，工作目录：" + _chat_workdir(project) + "。\n" + goal_hint +
        "你可以用只读工具了解项目代码（list_dir/read_file/db_query/git_log，或内置的读取/搜索类工具）"
        "和查看看板数据库；" + mysql_hint +
        "禁止写任何仓库文件（写入会被拒绝），不要尝试 Bash 命令（只读白名单外一律拒绝）。\n"
        "用户可在输入框粘贴/拖拽/选择本机 html 参考文件（仅 .html/.htm）；发送时会把文件绝对路径"
        "随消息一并给出——请用 read_file 按需读取源码理解结构/样式/交互，不要内联或试图渲染其正文"
        "（浏览器工具仅 http/https，禁止打开本地文件）。read_file 默认只回前 200 行、单文件上限 1MB，"
        "较大的原型请用 offset/max_lines 分段读取自己需要的段落。\n"
        "你可以用 Playwright 浏览器工具调研外部信息（导航/点击/输入/截图/页面读取等），"
        "但仅限 http/https 页面，禁止访问 file:// 与 data: 等本地协议；本机/内网地址可正常访问。"
        "工具调用会展示在聊天流中，用户可随时取消本轮。\n"
        "你也可以用 tapd MCP 只读工具查询 TAPD 需求/缺陷/评论等（仅 get_* 只读工具，写操作会被拒绝）。\n"
        "任务：和用户对话澄清需求，讨论清楚后输出需求草稿，格式如下（供页面预览，必须用此格式）：\n"
        "```draft\n{\"title\": \"需求标题\", \"notes\": \"背景、方案要点、验收标准、生效方式\"}\n```\n"
        "notes 用纯文本，" + REQ_NOTES_FMT_HINT + "，不超过 1500 字。"
        "正文引用草稿内容时必须用真实换行，禁止把 JSON 转义文本（字面 \\n、\\\" 等）粘贴进正文。"
        "一轮结束时用文字向用户提问澄清。\n"
        "本会话无交互终端，AskUserQuestion 等提问类工具调用后会立即返回未作答（dismiss），"
        "禁止使用；需要用户决策时直接在对话消息文本里提问。\n"
        "用户确认草稿后，下一轮会收到确认消息，届时调用 create_requirement 工具建卡"
        "（project 必须为 " + project + "；status 默认 later（暂缓，不会被自动开发捞取），"
        "仅当用户明确要求「开始开发」时才用 open）。\n"
        "时间管理：本轮约 10 分钟超时，用 `date` 关注实际耗时；接近时限时先输出 ```draft 草稿块"
        "（当前已澄清的结论，JSON 格式）保底再继续，避免超时中断丢失内容。\n"
        "输出规范：结论先行、能短则短，单条回复一般控制在两三百字内；禁止开场白、客套与复述用户需求；"
        "信息多用 - 列表；复杂问题先给结论+关键依据，再问是否需要展开；"
        "对话正文不要用代码块包裹（```draft 草稿块除外），代码块必须成对闭合。\n"
        "独立判断：对用户提出的技术/产品方案要有独立判断，敢于提出异议与替代建议。触发条件："
        "方案有明显更优替代、与项目现状或约定冲突、遗漏关键约束或风险（兼容/数据/回滚/生效方式/安全/性能）、"
        "引入不必要复杂度或长期维护成本、实现后解决不了真实问题。表达方式：结论先行（如「这个方案我不太建议，"
        "原因是…」），先给 1-2 条关键依据（尽量引用代码/事实），再给替代建议与选择代价，由用户拍板。边界："
        "没有实质异议时不要为反对而反对；有异议不等于拒绝执行；用户坚持后按原方案继续细化，"
        "并把分歧点与取舍写进草稿 notes 或单独说明。"
    )


def _project_mysql_env(project):
    """返回项目 mysql 查询配置对应的 .env 绝对路径；未配置返回 None。"""
    cfg = resolve("PROJECT_MYSQL").get(project)
    if not cfg:
        return None
    env_file = cfg.get("env_file")
    if env_file:
        return env_file
    paths = resolve("PROJECT_PATHS").get(project) or []
    return str(Path(paths[0]) / ".env") if paths else None


def _chat_set(chat_id, **fields):
    with resolve("get_db")() as conn:
        fields["updated_at"] = resolve("now_str")()
        conn.execute(f"UPDATE chat_sessions SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), chat_id))
        conn.commit()
    resolve("_sse_publish")('chat', chat_id=chat_id)   # #416 发布点 5（纯状态变更也即时可见）


def _chat_running_count():
    """当前全站 running（并行占名额）的建卡会话数。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM chat_sessions WHERE status = 'running'").fetchone()
    return row["n"] if row else 0


def _chat_add_event(chat_id, kind, content):
    with resolve("get_db")() as conn:
        conn.execute("INSERT INTO chat_events (chat_id, kind, content, ts) VALUES (?, ?, ?, ?)",
                     (chat_id, kind, str(content)[:4000], resolve("now_str")()))
        conn.commit()
    resolve("_sse_publish")('chat', chat_id=chat_id)   # #416 发布点 2


def _chat_event_sink(chat_id):
    """ACPSession 事件注入回调：写 chat_events 表（kind message/tool/system）"""
    def sink(req_id, kind, content, session_id=None):
        _chat_add_event(chat_id, kind, content)
    return sink


def _recover_chat_sessions():
    """board 启动时将残留会话标为可自动恢复的重启中断（仅网页建卡会话）。

    #22：channel='web' 过滤——飞书会话由 dispatcher 自己持有并管理，看板重启不应把它标成
    interrupted 再自动恢复（否则 `_spawn_resume_round` 与飞书 worker 并发驱动同一 kimi 会话，
    并占死 CHAT_PARALLEL_LIMIT 名额）。"""
    with resolve("get_db")() as conn:
        rows = conn.execute(
            "SELECT id FROM chat_sessions WHERE status IN ('running', 'idle') "
            "AND channel = 'web' ORDER BY id DESC"
        ).fetchall()
        conn.execute(
            "UPDATE chat_sessions SET status = 'interrupted', interrupt_source = 'restart', "
            "error = '看板服务重启，会话等待自动恢复', updated_at = ? "
            "WHERE status IN ('running', 'idle') AND channel = 'web'", (resolve("now_str")(),))
        conn.commit()
    for row in rows:
        _chat_add_event(row["id"], "system", "看板服务已重启，会话等待自动恢复…")


def _auto_resume_chat_session():
    """后台恢复最新一条非用户主动取消的中断会话（仅网页建卡会话）；失败由 _chat_boot 记录且不循环重试。

    #22：channel='web' 过滤同 `_recover_chat_sessions`——飞书会话不走看板自动恢复。"""
    with chat_lock:
        with resolve("get_db")() as conn:
            active = conn.execute(
                "SELECT 1 FROM chat_sessions WHERE status IN ('idle', 'running') "
                "AND channel = 'web' LIMIT 1"
            ).fetchone()
            if active:
                return
            row = conn.execute(
                "SELECT id, project, status, session_id, interrupt_source "
                "FROM chat_sessions WHERE status = 'interrupted' AND channel = 'web' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        if row and row["interrupt_source"] in CHAT_AUTO_RESUME_SOURCES:
            resolve("_spawn_resume_round")(row["id"], row["project"], row["session_id"], None)


def _chat_recovery_worker():
    """持续检测最新建卡会话，使其它页面停留期间产生的可恢复中断也能自动续上。"""
    while True:
        try:
            _auto_resume_chat_session()
        except Exception:
            pass
        time.sleep(2)


def _start_chat_recovery_worker():
    threading.Thread(target=_chat_recovery_worker, daemon=True,
                     name="req-board-chat-recovery").start()


def _chat_workdir(project):
    """会话工作目录：单 paths 项目直连仓库；多 paths 用第一个仓库（聚合目录语义由只读 roots 覆盖）"""
    paths = resolve("PROJECT_PATHS").get(project) or []
    return paths[0] if paths else str(Path.home())


def _chat_roots(project, workdir):
    # #110：只读范围放开至家目录（敏感文件内容读取由 acp_shared 黑名单拦截，list_dir 仍可列出）
    return [workdir] + (resolve("PROJECT_PATHS").get(project) or []) + [str(Path.home())]


def _chat_materials_dir(project, chat_id):
    """会话上传附件目录：MATERIALS_ROOT/<project>/chat-<chat_id>（project 取会话行、chat_id 为 int，
    均非用户可控字符串；仍以 resolve + is_relative_to 兜底）。"""
    return resolve("MATERIALS_ROOT") / str(project) / f"chat-{int(chat_id)}"


def _material_name_ok(name, exts=CHAT_MATERIAL_EXTS):
    """上传文件名合法性（fail-closed）：空/仅空白、含 / 或 \\ 或 ..、控制字符、超长、
    扩展名不在 `exts`（大小写不敏感）一律拒。**不做 basename 静默截断**——原名含分隔符即拒
    （`../x.html` 因含 `/` 与 `..` 被拒）。其余字符（中文、空格、括号等）允许。
    #22：exts 按会话 channel 传参（web=CHAT_MATERIAL_EXTS / feishu=FEISHU_MATERIAL_EXTS）。"""
    if not name or not name.strip() or len(name) > CHAT_MATERIAL_NAME_MAX:
        return False
    if '/' in name or '\\' in name or '..' in name:
        return False
    if any(ord(ch) < 32 for ch in name):
        return False
    return Path(name).suffix.lower() in exts


def _read_capped(stream, cap):
    """分块读取至 EOF 或超过 cap，返回 (data, over)。**按读入字节判定**：content_length 缺失
    （chunked）时同样权威；超限立即中止并丢弃已读缓冲，不整包缓冲、不 fail-open。"""
    chunks, total = [], 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > cap:
            return b"", True
        chunks.append(chunk)


def _write_material_to_dir(d, raw_name, data):
    """落盘已校验的文件到指定目录（#44 抽出：建卡与助手附件共用）：
    目录在 MATERIALS_ROOT 内 resolve 复核 → mkdir → 目标 resolve 在目录内 → 同名加 -1/-2 后缀
    （不覆盖）→ 写盘。返回 {filename, size, path, over_read_limit}；非法路径/文件名抛 ValueError。"""
    if not d.resolve().is_relative_to(resolve("MATERIALS_ROOT").resolve()):
        raise ValueError("非法路径")
    d.mkdir(parents=True, exist_ok=True)
    root = d.resolve()
    target = (d / raw_name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("非法文件名")
    stem, ext = Path(raw_name).stem, Path(raw_name).suffix
    n = 0
    while target.exists():
        n += 1
        target = root / f"{stem}-{n}{ext}"
    target.write_bytes(data)
    return {"filename": target.name, "size": len(data), "path": str(target),
            "over_read_limit": len(data) > CHAT_MATERIAL_READ_CAP}


def _write_chat_material(project, chat_id, raw_name, data):
    """落盘已校验的文件：目录内 resolve 复核 → 同名冲突加 -1/-2 后缀（不覆盖）→ 写盘。
    返回 {filename, size, path, over_read_limit}。"""
    return _write_material_to_dir(_chat_materials_dir(project, chat_id), raw_name, data)


def _chat_material_paths(project, chat_id, names, exts=CHAT_MATERIAL_EXTS):
    """复核前端回传的参考文件名 → 绝对路径列表。任一名非法/越界/不存在 → (None, 错误文案)。
    #22：exts 按会话 channel 传入（web=CHAT_MATERIAL_EXTS / feishu=FEISHU_MATERIAL_EXTS），
    与上传侧同源，杜绝「落盘成功但 /prompt 400」的断链。"""
    d = _chat_materials_dir(project, chat_id)
    if not d.resolve().is_relative_to(resolve("MATERIALS_ROOT").resolve()):
        return None, "非法路径"
    root = d.resolve()
    paths = []
    for name in names:
        if not isinstance(name, str) or not _material_name_ok(name, exts):
            return None, f"非法文件名: {name}"
        target = (d / name).resolve()
        if not target.is_relative_to(root):
            return None, f"非法路径: {name}"
        if not target.is_file():
            return None, f"参考文件不存在: {name}"
        paths.append(str(target))
    return paths, None


def _copy_chat_materials_to_artifact(chat_id, project, req_id, notes, channel='web'):
    """#12 建卡收口：把会话上传的参考文件复制到 ARTIFACTS_ROOT/<project>/req-<id>/materials/
    （analysis/dev 只读 roots 含该产物目录，见 dispatcher.readonly_tool_roots），并返回追加了复用路径
    （绝对路径）的 notes。复制失败降级：返回原 notes 不追加 + 记 system 事件，不阻断建卡。
    预算截断保证 ≤2000 且参考行完整（agent notes 的 2000 校验在调用方、保持在前，方案必改 4）。
    #22：notes 追加文案按 channel 分流（web 保持「N 个 html 原型」；feishu 用「N 个参考文件，图片/文档」）。"""
    src = _chat_materials_dir(project, chat_id)
    files = sorted(p for p in src.glob('*') if p.is_file()) if src.is_dir() else []
    if not files:
        return notes
    try:
        dst = resolve("ARTIFACTS_ROOT") / str(project) / f"req-{int(req_id)}" / "materials"
        dst.mkdir(parents=True, exist_ok=True)
        for p in files:
            (dst / p.name).write_bytes(p.read_bytes())
    except Exception as e:  # noqa: BLE001 - 降级：建卡照常成功，仅少一条 notes 复用行
        _chat_add_event(chat_id, "system", f"参考文件复制失败（{e}），notes 未写入复用路径")
        return notes
    desc = (f"{len(files)} 个参考文件，图片/文档" if channel == 'feishu'
            else f"{len(files)} 个 html 原型")
    ref = f"\n参考文件：{dst}（{desc}）"
    return notes[:max(0, 2000 - len(ref))] + ref


def _extract_draft(events):
    """从事件列表的 message 事件中解析最新草稿块 ```draft ... ``` → (title, notes)；无则 None。
    兼容两种内容格式：JSON 对象（{"title": ..., "notes": ...}）与 YAML 键值（title: ... / notes: ...）。"""
    buf = []
    for e in events:
        if e["kind"] == "message":
            buf.append(e["content"] or "")
    m = DRAFT_RE.search("\n".join(buf))
    if not m:
        return None
    body = m.group(1).strip()
    data = None
    try:
        data = json.loads(body)
    except Exception:
        data = None
    if isinstance(data, dict):
        title = str(data.get("title") or "").strip()
        notes = str(data.get("notes") or "").strip()
    else:
        title_m = re.search(r"^title:\s*(.+)$", body, re.M)
        notes_m = re.search(r"^notes:\s*(.+)$", body, re.M | re.S)
        if not title_m:
            return None
        title = title_m.group(1).strip()
        notes = notes_m.group(1).strip() if notes_m else ""
    if not title:
        return None
    return title[:200], notes[:2000]


def _close_chat_runtime(rt):
    """关闭一个 chat_runtime 条目（会话进程 + 日志文件），幂等。"""
    try:
        rt["session"].close(kill=True)
    except Exception:
        pass
    try:
        rt["logf"].close()
    except Exception:
        pass
    try:
        rt["create_srv"].close()
    except Exception:
        pass


def _finalize_then_close_chat(chat_id, rt, project):
    """后台收尾旧会话：先采集 /usage/版本/分支（短超时），再杀进程。失败不阻断。"""
    try:
        _finalize_chat_session(chat_id, rt["session"], project)
    except Exception:
        pass
    try:
        _close_chat_runtime(rt)
    except Exception:
        pass


def _chat_session_alive(rt):
    """运行时条目是否可用：进程对象存在且未退出。"""
    try:
        return rt["session"].proc.poll() is None
    except Exception:
        return False


def _chat_mark_stale(chat_id):
    """进程失效自愈：把残留 idle/running 转 interrupted（保留 session_id 供 resume）。"""
    _chat_set(chat_id, status="interrupted", interrupt_source="process_lost",
              error="会话进程已失效，等待自动恢复")
    _chat_add_event(chat_id, "system", "会话进程已失效，正在自动恢复…")


def _abort_running_round(chat_id, message, source="error"):
    """强制收尾仍处于 running 的会话：标 interrupted + 记录原因 + 清理运行时（幂等）。
    线程收尾兜底：任何提前返回/未捕获异常都不允许把会话留在 running。"""
    with chat_lock:
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
        if not row or row["status"] != "running":
            return
        _chat_set(chat_id, status="interrupted", interrupt_source=source, error=message)
        _chat_add_event(chat_id, "system", message)
        rt = chat_runtime.pop(chat_id, None)
    if rt:
        _close_chat_runtime(rt)


def _boot_and_prompt(chat_id, project, resume_session_id, text):
    """恢复会话（session/resume，失败自动回退新会话）后排队本轮；text 为空则只恢复。"""
    resolve("_chat_boot")(chat_id, project, resume_session_id=resume_session_id)
    with resolve("get_db")() as conn:
        r2 = conn.execute("SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
    if text and r2 and r2["status"] == "idle":
        # #109：续跑注入上次中断原因（超时/异常/取消），先了解原因再继续，避免盲续
        reason = _chat_interrupt_reason(chat_id)
        if reason:
            text = f"上次会话中断原因：{reason}。先针对该原因自查再继续。\n\n" + text
        _chat_set(chat_id, status="running")
        _chat_prompt_thread(chat_id, text)


def _spawn_resume_round(chat_id, project, resume_session_id, text, event=None):
    """置 running、写事件并启动恢复线程（与 _start_chat_round 共用；锁可嵌套）。"""
    with chat_lock:
        chat_booting.add(chat_id)
        _chat_set(chat_id, status="running", error=None)
        _chat_add_event(chat_id, "system", "会话恢复中…")
        if event:
            _chat_add_event(chat_id, event[0], event[1])
        threading.Thread(target=_boot_and_prompt,
                         args=(chat_id, project, resume_session_id, text), daemon=True).start()


def _start_chat_round(chat_id, text, event=None):
    """统一驱动一轮（/prompt 与 /confirm 共用）。

    text: 发送给 kimi 的完整 prompt；
    event: (kind, content) 写入 chat_events 的事件（用户消息或 system 提示），None 则不写。
    进程失效（board 重启/子进程崩溃）时自动转 interrupted 并 resume 续上下文后排队，
    避免"idle 无运行时直接发消息"永久卡 running。
    并行上限：非 running 会话起新轮次会占用一个全局并行名额（CHAT_PARALLEL_LIMIT）。"""
    stale_rt = None
    with chat_lock:
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            return {"error": "会话不存在"}, 404
        status = row["status"]
        if status == "created":
            return {"error": f'本会话已建卡 #{row["req_id"]}，不能继续'}, 400
        if status == "running":
            rt = chat_runtime.get(chat_id)
            if (rt is not None and _chat_session_alive(rt)) or chat_id in chat_booting:
                return {"error": "会话正在运行，请等待本轮结束"}, 409
            # 孤儿 running：进程已失效，转 interrupted 自动恢复
            stale_rt = chat_runtime.pop(chat_id, None)
            _chat_mark_stale(chat_id)
        else:
            if status not in ("idle", "interrupted"):
                return {"error": f'会话状态 {status} 不可对话'}, 400
            if _chat_running_count() >= CHAT_PARALLEL_LIMIT:
                return {"error": CHAT_PARALLEL_FULL_MSG}, 429
            if status == "idle":
                rt = chat_runtime.get(chat_id)
                if rt is not None and _chat_session_alive(rt):
                    # 正常路径：直接起本轮
                    _chat_set(chat_id, status="running")
                    if event:
                        _chat_add_event(chat_id, event[0], event[1])
                    threading.Thread(target=_chat_prompt_thread, args=(chat_id, text), daemon=True).start()
                    return {"success": True}, 202
                # 死会话：清残留、转 interrupted 自动恢复
                stale_rt = chat_runtime.pop(chat_id, None)
                _chat_mark_stale(chat_id)
        # 恢复路径：置 running、写事件并启动恢复线程（与状态检查同一把锁，防并发重复拉起）
        resolve("_spawn_resume_round")(chat_id, row["project"], row["session_id"], text, event=event)
    if stale_rt:
        _close_chat_runtime(stale_rt)
    return {"success": True}, 202


def _chat_prompt_thread(chat_id, text):
    """驱动一轮 prompt（后台线程）：超时 cancel+杀进程标 interrupted（可续跑）。
    首轮（尚无 message 事件）在用户消息前拼接系统提示词（角色/草稿格式/建卡流程）。
    兜底：任何提前返回/未捕获异常都不允许把会话留在 running。"""
    rt = chat_runtime.get(chat_id)
    if rt is None:
        _abort_running_round(chat_id, "会话进程不可用，本轮未执行", source="process_lost")
        return
    session = rt["session"]
    try:
        with resolve("get_db")() as conn:
            base = conn.execute("SELECT COALESCE(MAX(id), 0) FROM chat_events WHERE chat_id = ?",
                                (chat_id,)).fetchone()[0]
            row = conn.execute("SELECT project FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
            first = conn.execute("SELECT 1 FROM chat_events WHERE chat_id = ? AND kind = 'message' LIMIT 1",
                                 (chat_id,)).fetchone() is None
        if first and row:
            # #108：首轮注入进程安全红线（含本会话驱动进程真实 PID；续跑会话已在上下文内，不重复注入）
            lang = prompt_lang(row["project"])
            text = (append_kill_redline(_chat_system_prompt(row["project"]), session.proc.pid, lang)
                    + tr(lang, "\n\n用户消息：\n") + text)
        try:
            session.prompt(text, timeout=CHAT_TIMEOUT)
            session._flush_message()
        except TimeoutError:
            try:
                session.cancel()
                time.sleep(2)
            except Exception:
                pass
            _finalize_if_created(chat_id, session)
            _abort_running_round(chat_id, "本轮超时，会话已中断并等待自动恢复", source="timeout")
            return
        except Exception as e:
            _finalize_if_created(chat_id, session)
            _abort_running_round(chat_id, f"本轮异常: {e}", source="error")
            return
        # 建卡成功（create_requirement 已把会话置 created）：收尾采集用量/版本/分支
        _finalize_if_created(chat_id, session)
        # 正常结束：解析本轮新增事件中的草稿块（agent 输出 ```draft {title, notes} ```）
        with resolve("get_db")() as conn:
            events = [dict(r) for r in conn.execute(
                "SELECT kind, content FROM chat_events WHERE chat_id = ? AND id > ? ORDER BY id",
                (chat_id, base)).fetchall()]
        draft = _extract_draft(events)
        close_detached = None
        with chat_lock:
            with resolve("get_db")() as conn:
                row = conn.execute("SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
            status_now = row["status"] if row else None
            if status_now == "running":
                # 建卡成功由 create_requirement 工具置 created；其余情况回 idle
                if draft:
                    _chat_set(chat_id, status="idle", draft_title=draft[0], draft_notes=draft[1])
                else:
                    _chat_set(chat_id, status="idle")
                status_now = "idle"
            rt = chat_runtime.get(chat_id)
            # 切走后的后台轮次跑完：回收进程（保留 idle/created，下次对话走自动恢复续上下文）
            if rt is not None and rt.get("detached") and status_now in ("idle", "created"):
                chat_runtime.pop(chat_id, None)
                close_detached = rt
        if close_detached:
            _close_chat_runtime(close_detached)
    finally:
        _abort_running_round(chat_id, "本轮异常结束，会话已中断（可继续会话）")


def _chat_boot(chat_id, project, resume_session_id=None):
    """起 kimi acp 会话（线程内）：只读 MCP 工具 + create_requirement 写工具注入；失败标 failed"""
    workdir = _chat_workdir(project)
    create_srv = resolve("CreateRequirementMcpServer")(chat_id, project)
    logf = open(f"/tmp/req_board_chat_{chat_id}.log", "a")
    session = None
    with chat_lock:
        chat_booting.add(chat_id)
    try:
        mysql_env = _project_mysql_env(project)
        session = resolve("ACPSession")(
            chat_id, workdir, logf, "analysis", Path("/nonexistent-chat-artifacts"),
            project=project, event_sink=_chat_event_sink(chat_id), readonly=True,
            extra_allow_prefixes=[f"mcp__{CREATE_TOOL_SERVER}__", PLAYWRIGHT_TOOL_PREFIX, TAPD_TOOL_PREFIX],
            extra_mcp_servers=[{"type": "http", "name": CREATE_TOOL_SERVER, "url": create_srv.url, "headers": []}],
            allow_fs_write=False, readonly_roots=_chat_roots(project, workdir),
            deny_builtin_write=True,
            mysql_config={"env_file": mysql_env} if mysql_env else None,
            # #108：经 wrapper 启动（exec -a 覆盖 argv[0]），ps/pgrep 可辨 kimi-chat-acp，防误杀
            kimi_bin=str(resolve("BASE_DIR") / "scripts" / "kimi-chat-acp"))
        with chat_lock:
            chat_runtime[chat_id] = {"session": session, "logf": logf, "create_srv": create_srv}
        resumed = session.start(workdir, resume_session_id=resume_session_id)
        with chat_lock:
            with resolve("get_db")() as conn:
                row = conn.execute(
                    "SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)
                ).fetchone()
            if row and row["status"] != "running":
                chat_runtime.pop(chat_id, None)
                if row["status"] == "suspended":
                    _chat_set(chat_id, session_id=session.session_id, model=session.model_name,
                              thinking=session.thinking, interrupt_source=None, error=None)
                    _chat_add_event(chat_id, "system", "会话已挂起，切回项目后可继续")
                _close_chat_runtime({"session": session, "logf": logf, "create_srv": create_srv})
                return
        _chat_set(chat_id, status="idle", session_id=session.session_id, model=session.model_name,
                  thinking=session.thinking, interrupt_source=None, error=None)
        _chat_add_event(chat_id, "system",
                        f"会话已建立（项目 {project}，模型 {session.model_name or '默认'}）"
                        if not resumed else f"会话已恢复（项目 {project}）")
    except Exception as e:
        message = chat_session_error(e, project)
        try:
            if session:
                session.close(kill=True)
        except Exception:
            pass
        with resolve("get_db")() as conn:
            row = conn.execute(
                "SELECT status FROM chat_sessions WHERE id = ?", (chat_id,)
            ).fetchone()
        if row and row["status"] not in ("suspended", "closed"):
            _chat_add_event(chat_id, "system", message)
            _chat_set(chat_id, status="failed", error=message)
    finally:
        with chat_lock:
            chat_booting.discard(chat_id)


def _chat_start_new(chat_id, project):
    threading.Thread(target=resolve("_chat_boot"), args=(chat_id, project), daemon=True).start()


def _cleanup_chat_procs():
    """board 退出时清理 kimi 子进程（launchd KeepAlive 场景防孤儿）"""
    with chat_lock:
        items = list(chat_runtime.values())
    for rt in items:
        try:
            rt["session"].close(kill=True)
        except Exception:
            pass
        try:
            rt["logf"].close()
        except Exception:
            pass


def _chat_interrupt_reason(chat_id):
    """最近一次中断类 system 事件（超时/异常/取消）内容，供续跑 prompt 注入失败原因闭环"""
    with resolve("get_db")() as conn:
        row = conn.execute(
            "SELECT content FROM chat_events WHERE chat_id = ? AND kind = 'system' "
            "AND (content LIKE '%超时%' OR content LIKE '%异常%' OR content LIKE '%取消%' "
            "OR content LIKE '%中断%') ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
    return row["content"] if row else None
