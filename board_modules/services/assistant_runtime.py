"""#32 Phase 5：assistant_runtime 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import ACP_MODEL
from acp_shared import model_alias_pool
from pathlib import Path
import base64
import hashlib
import sqlite3
import tempfile
import threading
import time

from board_modules.runtime import resolve
from board_modules.services.chat_runtime import CHAT_PARALLEL_LIMIT, _chat_running_count
from board_modules.services.assistant_card import ASSISTANT_CARD_PAGE_MAX


# ==================== #24 飞书看板助手（board 侧：会话/事件/工具面/轮次） ====================
# 物理载体 = 独立表 feishu_assistant_sessions / feishu_assistant_events（方案 B，不写 chat_sessions）。
# 一个飞书 chat_id 一个常驻助手会话；轮次由 board 权威计时（deadline 由请求方传 remaining_sec 驱动），
# **不复用** 600 秒的 _chat_boot/_chat_prompt_thread/_start_chat_round（CHAT_TIMEOUT 与建卡收尾语义
# 不得渗入）。助手会话占用 CHAT_PARALLEL_LIMIT 名额，但不占 project、不占飞书建卡名额。
ASSISTANT_TOOL_SERVER = "reqboard-assistant"   # 助手唯一 MCP server 名（只读业务工具 + create_requirement）
ASSISTANT_EVENT_MAX = 4000                     # 单条事件内容上限
ASSISTANT_TIMEOUT_SEC = 90                     # 默认单轮总预算（秒）；调用方可传更小 remaining_sec
ASSISTANT_RESULT_DEFAULT = 20                  # 工具默认返回条数
ASSISTANT_RESULT_MAX = 50                      # 工具返回条数硬上限
ASSISTANT_FIELD_MAX = 500                      # 工具结果单字段长度上限
# ---- #26 助手能力开关与跨项目扫描限量 ----
ASSISTANT_CAP_FILE_TOOLS = "assistant_file_tools"      # 能力一：文件/代码只读分析（feishu.json 布尔，默认关）
ASSISTANT_CAP_STATUS_WRITE = "assistant_status_write"  # 能力二：已建卡状态变更（默认关）
ASSISTANT_CAP_REQ_CARDS = "assistant_req_cards"        # 能力三：需求卡清单卡片（默认关）
ASSISTANT_CAP_GIT_OPS = "assistant_git_ops"            # 能力四：代码拉取与提交（默认关）
ASSISTANT_VISION_KEY = "assistant_vision"              # #52 图片通道档位（默认开，显式 false 才关）
# #31 能力开关位置的统一描述：回绝语文案与工具 description 共用，保证「照着走」的路径一致
# （页面 = 网页左下角齿轮按钮，tab = 通知渠道，小节 = 飞书机器人，分组 = 看板助手能力）
ASSISTANT_CAP_LOCATION = "网页左下角齿轮「设置 → 通知渠道」标签页「飞书机器人」小节的「看板助手能力」分组"
ASSISTANT_FIND_DEFAULT_LIMIT = 200             # file_find 默认条数（上限 = DIR_MAX_ENTRIES）
ASSISTANT_STATUS_WRITE_TARGETS = ("open",)     # 状态写首期唯一目标：待开发
ASSISTANT_STATUS_WRITE_SOURCES = ("later",)    # 状态写首期唯一来源：暂缓
ASSISTANT_HISTORY_DEFAULT = 20                 # 历史聚合默认页大小
ASSISTANT_HISTORY_MAX = 50                     # 历史聚合页大小上限
ASSISTANT_ACTIVE_STATUSES = ("idle", "running", "cancelling", "interrupted")
ASSISTANT_TERMINAL_STATUSES = ("closed", "failed")
# ---- #44 助手附件（飞书素材）：按扩展名分三类，决定进 prompt 的形态 ----
ASSISTANT_ATTACH_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
ASSISTANT_ATTACH_TEXT_EXTS = (".txt", ".md", ".csv", ".json", ".html", ".htm")
ASSISTANT_ATTACH_TEXT_MAX = 20000              # 单文本附件内联上限（字符）
ASSISTANT_ATTACH_TEXT_TOTAL = 40000            # 全部附件正文合计上限（字符）
# 超时固定文案（只发一次）：与 dispatcher 侧兜底文案同口径
ASSISTANT_TIMEOUT_TEXT = "这轮没查完，重发一次或到网页看。"
ASSISTANT_TMP_ROOT = Path(tempfile.gettempdir()) / "req_board_assistant"
assistant_lock = threading.RLock()   # 助手状态机/运行时共用锁（与 chat_lock 独立，慢操作不持锁）
assistant_runtime = {}   # assistant session id -> {"session": ACPSession, "logf": file, "mcp": AssistantMcpServer}
assistant_booting = set()  # 正在建立/恢复助手会话的 id（防并发重复拉起 boot 线程）


def _assistant_row(sid):
    with resolve("get_db")() as conn:
        return conn.execute("SELECT * FROM feishu_assistant_sessions WHERE id = ?", (sid,)).fetchone()


def _assistant_active_row(feishu_chat_id):
    with resolve("get_db")() as conn:
        return conn.execute(
            "SELECT * FROM feishu_assistant_sessions WHERE feishu_chat_id = ? "
            "AND status IN ('idle','running','cancelling','interrupted') ORDER BY id DESC LIMIT 1",
            (str(feishu_chat_id),)).fetchone()


def _assistant_set(sid, **fields):
    """更新助手会话行（自动 updated_at）。字段名由调用方固定（不做用户输入拼接）。"""
    with resolve("get_db")() as conn:
        fields["updated_at"] = resolve("now_str")()
        conn.execute(f"UPDATE feishu_assistant_sessions SET "
                     f"{', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), sid))
        conn.commit()


def _assistant_add_event(sid, kind, content):
    # #30：card 是服务端生成的页 spec（写入前已按 ASSISTANT_CARD_PAGE_MAX 断言，不截断语义）；
    # 其余事件维持既有 [:ASSISTANT_EVENT_MAX] 行为逐字不变（message/tool/system/user 不受影响）
    limit = ASSISTANT_CARD_PAGE_MAX if kind == "card" else ASSISTANT_EVENT_MAX
    with resolve("get_db")() as conn:
        cur = conn.execute(
            "INSERT INTO feishu_assistant_events (assistant_session_id, kind, content, ts) "
            "VALUES (?, ?, ?, ?)",
            (sid, kind, str(content)[:limit], resolve("now_str")()))
        conn.execute("UPDATE feishu_assistant_sessions SET last_event_id = ?, updated_at = ? "
                     "WHERE id = ?", (cur.lastrowid, resolve("now_str")(), sid))
        conn.commit()
        return cur.lastrowid


def _assistant_event_sink(sid):
    """ACPSession 事件注入回调：写 feishu_assistant_events（kind message/tool/system）。"""
    def sink(req_id, kind, content, session_id=None):
        _assistant_add_event(sid, kind, content)
    return sink


def _assistant_get_or_create(feishu_chat_id, model=None):
    """按 feishu_chat_id 幂等 get-or-create 活跃助手会话（插入即落受约束状态 'idle'）。
    并发撞部分唯一索引（IntegrityError）时回读既有活跃行返回，保证不插第二行。
    返回 (row, created)。"""
    feishu_chat_id = str(feishu_chat_id)
    with assistant_lock:
        row = resolve("_assistant_active_row")(feishu_chat_id)
        if row is not None:
            return row, False
        try:
            with resolve("get_db")() as conn:
                cur = conn.execute(
                    "INSERT INTO feishu_assistant_sessions "
                    "(feishu_chat_id, status, model, created_at, updated_at) "
                    "VALUES (?, 'idle', ?, ?, ?)",
                    (feishu_chat_id, (model or None), resolve("now_str")(), resolve("now_str")()))
                conn.commit()
                sid = cur.lastrowid
        except sqlite3.IntegrityError:
            row = resolve("_assistant_active_row")(feishu_chat_id)
            if row is not None:
                return row, False
            raise
        row = _assistant_row(sid)
        return row, True


def _assistant_owned(sid, feishu_chat_id):
    """双绑定归属校验：助手 session id + feishu_chat_id 必须同时命中，否则 None（调用方 404）。"""
    chat = str(feishu_chat_id or "").strip()
    if not chat:
        return None
    with resolve("get_db")() as conn:
        return conn.execute(
            "SELECT * FROM feishu_assistant_sessions WHERE id = ? AND feishu_chat_id = ?",
            (sid, chat)).fetchone()


def _assistant_running_count():
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM feishu_assistant_sessions "
                           "WHERE status = 'running'").fetchone()
    return row["n"] if row else 0


def _assistant_parallel_full():
    """助手与建卡会话共用 CHAT_PARALLEL_LIMIT 名额（含正在跑助手轮次的会话）。"""
    return (_chat_running_count() + _assistant_running_count()) >= CHAT_PARALLEL_LIMIT


def _assistant_workdir(sid):
    """助手会话工作目录：临时空目录（非仓库、非家目录、非真实 DB），只读 roots 仅此一处。"""
    d = ASSISTANT_TMP_ROOT / f"asst-{int(sid)}"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _assistant_materials_dir(sid):
    """#44 本会话附件目录：MATERIALS_ROOT/_assistant/asst-<sid>（sid 为 int、非用户可控字符串）。
    **调用期取值** resolve("MATERIALS_ROOT")——测试把 b.MATERIALS_ROOT 指向临时目录后即生效，
    不新增 import 期常量（否则会冻结真实 ~/.req-board/materials）。"""
    return resolve("MATERIALS_ROOT") / "_assistant" / f"asst-{int(sid)}"


def _assistant_vision_enabled():
    """#44/#52 图片通道档位开关：现读 feishu.json 的 assistant_vision，**缺省开启**——
    缺键 / 文件缺失 / 损坏均按开启处理，只有显式 false 才关闭（与 _assistant_cap_enabled
    的默认关闭口径不同）；每次调用现读，不缓存，保存开关后下一轮即生效。"""
    raw = resolve("_feishu_json_raw")()
    return not (isinstance(raw, dict) and raw.get(ASSISTANT_VISION_KEY) is False)


def _assistant_attach_kind(name):
    """附件按扩展名分类：image（可进 image block）/ text（正文内联）/ path_only（只给路径）。"""
    ext = Path(str(name or "")).suffix.lower()
    if ext in ASSISTANT_ATTACH_IMAGE_EXTS:
        return "image"
    if ext in ASSISTANT_ATTACH_TEXT_EXTS:
        return "text"
    return "path_only"


def _assistant_read_attach_text(path, limit):
    """读附件正文（UTF-8 errors='replace'）→ (text|None, truncated)；读取失败或二进制（含 NUL）
    返回 (None, False)，由调用方降级为仅路径并说明读不出。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None, False
    if b"\x00" in data[:4096]:
        return None, False
    text = data.decode("utf-8", errors="replace")
    if limit <= 0:
        return "", True
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _assistant_attach_blocks(sid, items):
    """#44 附件 → ACP content blocks（逐轮组装，不缓存）：
    - image：读图开启（assistant_vision 缺省开启，显式 false 才关闭）进 {"type":"image", mimeType, data(base64)}，文本块只写文件名；
      关闭或读不出字节 → 文本块给文件名 + 绝对路径（模型可按文本读或让用户改文字描述）；
    - text：正文按 ASSISTANT_ATTACH_TEXT_MAX / ASSISTANT_ATTACH_TEXT_TOTAL 截断后内联，不走代码只读开关；
    - path_only：只给文件名 + 绝对路径（提示可用 file_read 按文本读）。
    返回 blocks 列表（0..N 个 image + 末尾单个 text 块）。"""
    images, lines = [], []
    budget = ASSISTANT_ATTACH_TEXT_TOTAL
    vision = _assistant_vision_enabled()
    for it in items or []:
        name = str(it.get("filename") or "")
        path = str(it.get("path") or "")
        kind = it.get("kind") or _assistant_attach_kind(name)
        if kind == "image":
            data = b""
            if vision:
                try:
                    data = Path(path).read_bytes()
                except OSError:
                    data = b""
            if data:
                mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".gif": "image/gif", ".webp": "image/webp",
                        ".bmp": "image/bmp"}.get(Path(name).suffix.lower(), "image/png")
                images.append({"type": "image", "mimeType": mime,
                               "data": base64.b64encode(data).decode()})
                lines.append(f"[附件] {name}（图片）")
            elif vision:
                lines.append(f"[附件] {name}（图片读取失败；已保存到 {path}）")
            else:
                lines.append(f"[附件] {name}（图片，助手当前未开启读图；已保存到 {path}）"
                             "请用文字描述需求，或让助手用 file_read 按文本读取。")
            continue
        if kind == "text" and budget > 0:
            body, truncated = _assistant_read_attach_text(path, min(ASSISTANT_ATTACH_TEXT_MAX, budget))
            if body is None:
                lines.append(f"[附件] {name}（正文读不出，已按路径提供：{path}）")
                continue
            budget -= len(body)
            head = f"[附件] {name}（绝对路径 {path}）正文：\n{body}"
            if truncated:
                head += f"\n（已截断，完整文件见 {path}）"
            lines.append(head)
            continue
        lines.append(f"[附件] {name}（绝对路径 {path}；可用 file_read 按文本读取）")
    blocks = list(images)
    if lines:
        blocks.append({"type": "text", "text": "\n\n".join(lines)})
    return blocks


def _assistant_close_runtime(rt):
    """关闭助手运行时条目（会话进程 + 日志 + 工具 MCP），幂等。"""
    if not rt:
        return
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


def _assistant_resolve_model(configured=None):
    """助手模型解析（#24）：返回 (model, source, reason)。
    configured = feishu.json 的 assistant_model（None/空 = 未配置 → 四层链末端 ACP_MODEL）；
    已配置则要求命中全局别名池且 model_alias_usable 为真，否则回落 ACP_MODEL 并留痕 reason。
    池不可用（无 config.toml）时不放行非法值，同样回落。"""
    configured = (configured or "").strip() or None
    if not configured:
        return ACP_MODEL, "global", None
    pool, _home, _warn = model_alias_pool()
    if pool is None:
        return ACP_MODEL, "global", f"模型池不可用，assistant_model={configured} 未生效，回落 {ACP_MODEL}"
    entry = pool.get(configured)
    if entry is None:
        return ACP_MODEL, "global", f"assistant_model={configured} 不在别名池，回落 {ACP_MODEL}"
    if not entry.get("usable"):
        return ACP_MODEL, "global", (f"assistant_model={configured} 不可用"
                                     f"（{entry.get('reason') or '未知原因'}），回落 {ACP_MODEL}")
    return configured, "assistant", None


def _assistant_system_prompt():
    """助手系统提示词（首轮注入）：单一源 = prompts/feishu_assistant.md（不登记 flows.json）。
    文件缺失/读失败时降级为最小约束文案（不静默给宽工具面——工具面本身由 server 固定）。"""
    try:
        text = resolve("ASSISTANT_PROMPT_FILE").read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return ("你是需求看板（req-board）的飞书看板助手。只能用服务端提供的只读业务工具查询"
            "项目/需求/会话/待确认项，以及用 create_requirement 建卡（缺省 status=later）。"
            "回答中的卡号、数字、状态必须来自工具结果，查不到就说查不到，禁止编造；"
            "禁止调用未授权工具，禁止把评审元数据或通用数据库内容带入回答。")


def _assistant_first_turn(sid):
    """是否本会话首轮（尚无 message 事件）——只有首轮注入系统提示词。"""
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT 1 FROM feishu_assistant_events "
                           "WHERE assistant_session_id = ? AND kind = 'message' LIMIT 1",
                           (sid,)).fetchone()
    return row is None


def _assistant_prompt_hash():
    """当前生效系统提示词的 sha256（取值 = _assistant_system_prompt，含文件缺失时的降级文案）。"""
    return hashlib.sha256(_assistant_system_prompt().encode("utf-8")).hexdigest()


def _assistant_sync_prompt(sid):
    """#54 提示词硬保证：行内 prompt_hash 与当前提示词不一致（含历史 NULL）时重建 ACP 会话。
    返回 (prompt_hash, rebuilt)。重建 = 关闭并回收旧进程 + 清 session_id，本轮走 session/new
    并强制注入最新提示词（不 resume 旧上下文）。文件未变时继续 resume，不重建。"""
    wanted = _assistant_prompt_hash()
    row = _assistant_row(sid)
    stored = None
    if row is not None:
        try:
            stored = row["prompt_hash"]
        except (KeyError, IndexError):
            stored = None
    if stored == wanted:
        return wanted, False
    with assistant_lock:
        rt = assistant_runtime.pop(sid, None)
    if rt is not None:
        _assistant_close_runtime(rt)     # close() 同时触发 mcp.cancel_event（终止在跑 git）
    resolve("_assistant_set")(sid, session_id=None, prompt_hash=None)
    return wanted, True


def _assistant_session_alive(rt):
    """运行时条目是否可用：进程对象存在且未退出（与 _chat_session_alive 同口径）。"""
    try:
        return rt["session"].proc.poll() is None
    except Exception:
        return False


def _assistant_boot(sid, turn_id):
    """按需起助手 kimi acp 会话（线程内）：已有存活会话直接复用（不重复拉起进程）；
    只注入 AssistantMcpServer（tool_profile='feishu_assistant'），只读 roots = 助手临时空目录，
    禁 fs 写。失败标 failed（终态，释放唯一约束）并返回 None。"""
    row = _assistant_row(sid)
    if row is None:
        return None
    with assistant_lock:
        rt = assistant_runtime.get(sid)
        if rt is not None:
            if _assistant_session_alive(rt):
                # 复用存活进程：MCP 的 turn_id 必须随本轮推进——幂等键 = asst-<sid>-turn-<turn_id>，
                # 若停在首轮，同一 chat 后续建卡会命中旧卡（#24 复修：复用进程后再建卡必落新卡）。
                mcp = rt.get("mcp")
                if mcp is not None:
                    mcp.turn_id = turn_id
                return rt          # 上一轮正常收尾（idle），进程仍在，直接续用
            assistant_runtime.pop(sid, None)
    if rt is not None:
        _assistant_close_runtime(rt)
    chat_id = row["feishu_chat_id"]
    workdir = _assistant_workdir(sid)
    mcp = resolve("AssistantMcpServer")(sid, chat_id, turn_id=turn_id)
    logf = open(f"/tmp/req_board_assistant_{sid}.log", "a")
    session = None
    with assistant_lock:
        assistant_booting.add(sid)
    try:
        session = resolve("ACPSession")(
            sid, workdir, logf, "analysis", Path("/nonexistent-assistant-artifacts"),
            event_sink=_assistant_event_sink(sid), readonly=True,
            extra_allow_prefixes=[f"mcp__{ASSISTANT_TOOL_SERVER}__"],
            extra_mcp_servers=[{"type": "http", "name": ASSISTANT_TOOL_SERVER,
                                "url": mcp.url, "headers": []}],
            allow_fs_write=False, readonly_roots=[workdir], deny_builtin_write=True,
            model=(row["model"] or None), tool_profile="feishu_assistant",
            # #108：经 wrapper 启动（exec -a 覆盖 argv[0]），ps/pgrep 可辨 kimi-chat-acp，防误杀
            kimi_bin=str(resolve("BASE_DIR") / "scripts" / "kimi-chat-acp"))
        with assistant_lock:
            assistant_runtime[sid] = {"session": session, "logf": logf, "mcp": mcp}
        session.start(workdir, resume_session_id=row["session_id"])
        with assistant_lock:
            cur = _assistant_row(sid)
            if cur is None or cur["turn_id"] != turn_id:
                # 本轮已被取消或被新轮取代：丢弃本次 boot 结果
                rt = assistant_runtime.pop(sid, None)
            else:
                rt = None
                # 只落 ACP session_id：model 列保留「请求值」（显示名会破坏下轮的池内校验）
                resolve("_assistant_set")(sid, session_id=session.session_id, error=None)
        if rt is not None:
            _assistant_close_runtime(rt)
            return None
        _assistant_add_event(sid, "system",
                             f"会话已建立（模型 {session.model_name or row['model'] or '默认'}）")
        return assistant_runtime.get(sid)
    except Exception as e:
        try:
            if session:
                session.close(kill=True)
        except Exception:
            pass
        with assistant_lock:
            assistant_runtime.pop(sid, None)
        cur = _assistant_row(sid)
        if cur is not None and cur["turn_id"] == turn_id:
            resolve("_assistant_set")(sid, status="failed", error=str(e)[:500])
            _assistant_add_event(sid, "system", f"会话建立失败: {e}")
        else:
            _assistant_add_event(sid, "system", f"会话建立失败: {e}")
        mcp.close()
        return None
    finally:
        with assistant_lock:
            assistant_booting.discard(sid)


def _assistant_finish(sid, turn_id, status, message=None, kind="system", error=None):
    """轮次收尾（turn_id 栅栏）：只有当前轮且仍在 running 时才改状态；旧线程晚到（或本轮已被
    cancel/新轮取代）一律不改。interrupted/failed 收尾时回收运行时进程（保留 session_id 供 resume）。"""
    close_rt = None
    with assistant_lock:
        row = _assistant_row(sid)
        if row is not None and row["turn_id"] == turn_id and row["status"] == "running":
            # #24 复修：先写正文/提示事件、后翻状态——dispatcher 每拍先读 status 再读 events，
            # 若状态先翻到 interrupted，调用方会看到「非 running」却读不到超时文案而收尾返回，
            # 文案永远发不出去（board 先超时、飞书用户收不到）。
            if message:
                _assistant_add_event(sid, kind, message)
            err = error
            if err is None and status in ("interrupted", "failed"):
                err = message
            resolve("_assistant_set")(sid, status=status, error=(str(err)[:500] if err else None))
        if status in ASSISTANT_TERMINAL_STATUSES or status in ("interrupted", "cancelling"):
            close_rt = assistant_runtime.pop(sid, None)
    if close_rt:
        _assistant_close_runtime(close_rt)


def _assistant_prompt_thread(sid, text, turn_id, budget, attachments=None):
    """助手专用轮次（后台线程）：boot + prompt 共享同一剩余预算，超时 cancel+杀进程标 interrupted。
    不复用 _chat_prompt_thread/_chat_boot/_start_chat_round（600s 与建卡收尾语义不得渗入）。
    #44：attachments 非空时组装 content blocks（图片 image block / 文本内联 / 仅路径），
    逐轮按本轮附件组装，不只首轮。"""
    deadline = time.monotonic() + max(1, int(budget or ASSISTANT_TIMEOUT_SEC))
    rt = None
    try:
        prompt_hash, rebuilt = _assistant_sync_prompt(sid)
        rt = _assistant_boot(sid, turn_id)
        if rt is None:
            return   # boot 失败已自行把会话置 failed
        session = rt["session"]
        mcp = rt.get("mcp")
        if mcp is not None:
            mcp.turn_deadline = deadline      # #54：git 工具预算 = 轮次剩余 - 预留
        prompt = text
        if rebuilt or _assistant_first_turn(sid):
            prompt = _assistant_system_prompt() + "\n\n用户消息：\n" + text
        remaining = max(1, int(deadline - time.monotonic()))
        blocks = _assistant_attach_blocks(sid, attachments or [])
        if blocks:
            session.prompt(prompt, timeout=remaining,
                           content=[{"type": "text", "text": prompt}] + blocks)
        else:
            session.prompt(prompt, timeout=remaining)
        session._flush_message()
        resolve("_assistant_set")(sid, prompt_hash=prompt_hash)
    except TimeoutError:
        with assistant_lock:
            rt2 = assistant_runtime.get(sid)
        if rt2 is not None:
            try:
                rt2["session"].cancel()
            except Exception:
                pass
        # 超时固定文案只发一次（turn_id 栅栏 + 单次写入）
        _assistant_finish(sid, turn_id, "interrupted", ASSISTANT_TIMEOUT_TEXT, kind="message",
                          error="本轮超时")
        return
    except Exception as e:
        _assistant_finish(sid, turn_id, "failed", f"本轮异常: {e}")
        return
    _assistant_finish(sid, turn_id, "idle")


def _cleanup_assistant_procs():
    """board 退出时清理飞书看板助手的 kimi 子进程（launchd KeepAlive 场景防孤儿）。"""
    with assistant_lock:
        items = list(assistant_runtime.values())
    for rt in items:
        _assistant_close_runtime(rt)


# ==================== #24 飞书看板助手 API（独立表；dispatcher 侧按本契约调用） ====================
def _assistant_public(row):
    """助手会话对外字段（稳定契约）：无 project、无内幕字段。"""
    return {"id": row["id"], "feishu_chat_id": row["feishu_chat_id"], "status": row["status"],
            "session_id": row["session_id"], "turn_id": row["turn_id"], "model": row["model"]}


def _assistant_config_model():
    """feishu.json 的 assistant_model（未配置/非法 → None）。旧文件无该字段等价未配置。"""
    try:
        _aid, _secret, _warns, am = resolve("_feishu_json_read")()
        return am
    except Exception:  # noqa: BLE001 - 读取异常按未配置降级
        return None
