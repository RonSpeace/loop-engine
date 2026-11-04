"""飞书多轮聊天建卡：项目解析、看板会话 HTTP 原语、对话回放、素材与正文抓取、建卡会话编排。"""

import base64
import ipaddress
import json
import re
import socket
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from dispatcher_modules.feishu_assistant import _feishu_material_to_assistant
from dispatcher_modules.runtime import D, log


# 数据流：飞书消息 → on_message（解析 + 入队，秒回，不阻塞 ws 回调）→ per-chat worker 串行处理
# → 项目解析（未定则追问）→ 素材按句柄延迟下载 → 等会话 idle → /prompt → 轮询轮次结束 → 文本发回飞书。
# 本地状态 state["feishu_chats"][chat_id] 只存文本与下载句柄，素材字节永不落盘/常驻。

_last_feishu_idle_sweep = None  # None = 尚未巡检；不能用 0.0：monotonic 从开机起算，开机不足一个周期时首轮会被误判为「刚巡检过」
# 建卡指令（整行归一化全等，非包含/前缀；无活跃建卡会话时等同普通文本）
FEISHU_CMD_END = "结束建卡"
FEISHU_CMD_NEW = "新建建卡"
FEISHU_CMD_CONFIRM = "确认建卡"
_FEISHU_CMD_EDGE = (" \t\r\n，。！？、；：,.!?;:\u3000"
                    "“”‘’「」『』\"'（）()【】[]〈〉《》<>")
_FEISHU_BUSY_HINT = "上一轮仍在进行，稍后回复任意消息继续。"
# #22 ⑤：飞书文档域名白名单（docx/wiki 路径仅在飞书域名下才走飞书 API）
_FEISHU_DOMAINS = ("feishu.cn", "larksuite.com", "feishu.net")
# #22 ⑥：飞书素材扩展名白名单（与 board 侧 FEISHU_MATERIAL_EXTS 同口径）
_FEISHU_MATERIAL_EXTS = frozenset((
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    ".pdf", ".txt", ".md", ".csv", ".json",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".html", ".htm",
))
_FEISHU_LINK_REJECT_HINT = "链接不可抓取（仅支持公网 http/https）"
# #44 助手附件单文件上限（与 board FEISHU_MATERIAL_MAX_BYTES 同口径；下载侧无字节上限，
# 判定发生在整包读入之后，与旧建卡素材链路同款）
_FEISHU_MATERIAL_MAX_BYTES = 20 * 1024 * 1024
# 单轮附件条数上限（与 board FEISHU_MATERIAL_MAX_FILES 同口径）：富文本多图超过部分截断并留日志
_FEISHU_MATERIAL_MAX_FILES = 5


def _feishu_save():
    try:
        D.save_state()
    except Exception as e:  # 落盘失败不阻断消息处理
        log.warning(f"飞书建卡状态保存失败: {e}")


def _feishu_chats():
    """state["feishu_chats"] 访问器（缺失时补空 dict；测试 fresh_state 无该键时也能工作）"""
    fc = D.state.get("feishu_chats")
    if not isinstance(fc, dict):
        fc = {}
        D.state["feishu_chats"] = fc
    return fc


def _feishu_greeted():
    """#24：独立「已问候 chat 集合」（按 chat_id，与 state["chat_id"] 解耦）。
    访问器在测试 fresh_state 无该键时补空 dict。"""
    g = D.state.get("feishu_greeted")
    if not isinstance(g, dict):
        g = {}
        D.state["feishu_greeted"] = g
    return g


def _feishu_greet_once(chat_id):
    """#24：同一 chat 只问候一次；返回 True 表示本条需要发问候（随后继续处理本条）。"""
    g = _feishu_greeted()
    if chat_id in g:
        return False
    g[chat_id] = time.time()
    _feishu_save()
    return True


def _feishu_seen_once(chat_id, message_id):
    """#24 消息幂等：去重键 = feishu_chat_id + message_id，per-chat 保留近期已处理窗口。
    message_id 缺失/空 → 不去重（fail-open，不因缺字段吞消息）；重复投递返回 False。"""
    if not message_id:
        return True
    seen = D.state.get("feishu_seen")
    if not isinstance(seen, dict):
        seen = {}
        D.state["feishu_seen"] = seen
    ids = seen.get(chat_id)
    if not isinstance(ids, list):
        ids = []
        seen[chat_id] = ids
    key = str(message_id)
    if key in ids:
        return False
    ids.append(key)
    if len(ids) > D.FEISHU_ASSISTANT_SEEN_WINDOW:
        del ids[:len(ids) - D.FEISHU_ASSISTANT_SEEN_WINDOW]
    return True


def _feishu_sess(chat_id):
    return _feishu_chats().get(chat_id)


def _feishu_local_by_session(session_id):
    for cid, s in (_feishu_chats() or {}).items():
        if (s or {}).get("session_id") == session_id:
            return cid, s
    return None, None


def _feishu_is_cmd(text, cmd):
    """#22 D8′/⑨：循环剥首尾空白与中英文标点直到不再变化，再整行全等比较
    （不做包含/前缀）。`结束建卡。。`、`「结束建卡」` 都能命中。"""
    if not text:
        return False
    t = text
    while True:
        s = t.strip(_FEISHU_CMD_EDGE)
        if s == t:
            return s == cmd
        t = s


# ---- 项目解析（D7：按 PROJECTS 项目名整词匹配，与 POST /api/chat 的键集同口径）----

def _feishu_project_names():
    return sorted(D.PROJECTS.keys())


def _feishu_project_lines():
    """追问列表行：`<序号>. <项目名> (<目录名>)`（决策 4：显示项目名与目录名）"""
    lines = []
    for i, name in enumerate(_feishu_project_names(), 1):
        dirs = []
        try:
            dirs = [Path(p).name for p in (D.PROJECTS[name].get("paths") or [])]
        except Exception:
            dirs = []
        suffix = f" ({', '.join(dirs)})" if dirs else ""
        lines.append(f"{i}. {name}{suffix}")
    return lines


def _feishu_project_ask_text():
    return "请回复项目名（或序号）选择要建卡的项目：\n" + "\n".join(_feishu_project_lines())


def _feishu_resolve_project(text):
    """整词匹配项目名；支持纯序号（对应追问列表）。命中返回项目名，否则 None。"""
    t = (text or "").strip()
    if not t:
        return None
    names = _feishu_project_names()
    for name in names:
        if t == name:
            return name
    for name in names:
        if re.search(r"(?<![\w\-])" + re.escape(name) + r"(?![\w\-])", t):
            return name
    if re.fullmatch(r"\d+", t) and 1 <= int(t) <= len(names):
        return names[int(t) - 1]
    return None


# ---- 看板会话 HTTP 原语（全走看板 HTTP API，不直连 sqlite）----

def _feishu_http_err(e):
    """HTTPError → 可读错误文本（优先 body.error）"""
    try:
        body = e.read().decode("utf-8", "replace")
        data = json.loads(body)
        return str(data.get("error") or body)[:300]
    except Exception:
        return f"HTTP {getattr(e, 'code', '?')}"


def _feishu_status_from_error(err):
    """从 board 400「会话状态 <s> 不可对话」文本里取出状态（取不到回落 closed）。"""
    m = re.search(r"会话状态\s*(\S+)", err or "")
    return m.group(1) if m else "closed"


def _feishu_chat_status(session_id):
    """GET /api/chat/<id> → (status, row)；看板不可达返回 (None, None)"""
    try:
        row = D.api_board("GET", f"/chat/{session_id}")
    except Exception as e:
        log.warning(f"飞书会话状态查询失败（{session_id}）：{e}")
        return None, None
    return (row or {}).get("status"), row


def _feishu_poll_until(session_id, stop, timeout, interval=2.0):
    """轮询 GET /api/chat/<id> 直到 status ∈ stop；closed/failed 提前返回；超时返回最后状态。
    返回 (status, row)。该原语被 D8（等 idle）与 D8‴（等轮次结束）复用。"""
    deadline = time.monotonic() + max(0.0, timeout)
    status, row = None, None
    while True:
        status, row = _feishu_chat_status(session_id)
        if status in stop or status in ("closed", "failed"):
            return status, row
        if time.monotonic() >= deadline:
            return status, row
        time.sleep(interval)


def _feishu_prompt_once(session_id, text, attachments):
    # board 侧 /prompt 限制 text ≤4000 字符（含抓来的文档正文）；超限会被 400 拒绝，这里先截断，
    # 避免正文抓取链把整轮 prompt 顶爆（截断提示保留在末尾）。
    body = text or ""
    if len(body) > D.FEISHU_PROMPT_MAX:
        body = body[:D.FEISHU_PROMPT_MAX - 20] + "…（正文过长已截断）"
    return D.api_board("POST", f"/chat/{session_id}/prompt",
                     {"text": body, "attachments": list(attachments or [])})


def _feishu_reset_session(chat_id, status, row=None):
    """本地状态与看板会话不一致（closed/failed/suspended/created）→ 清本地状态 + 明确出口。"""
    _feishu_chats().pop(chat_id, None)
    _feishu_save()
    if status == "created":
        rid = (row or {}).get("req_id")
        extra = f"（已建卡 #{rid}）" if rid else ""
        D._feishu_notify(chat_id, f"会话已建卡{extra}，回复『新建建卡』开始新会话。")
    elif status == "closed":
        D._feishu_notify(chat_id, "会话已结束，回复『新建建卡』开始新会话。")
    else:
        D._feishu_notify(chat_id, "会话已不可用，回复『新建建卡』开始新会话。")


def _feishu_send_prompt(chat_id, session_id, text, attachments):
    """#22 D8‴：发 prompt 前的状态对账与 409/400 自愈。
    idle/interrupted → 直接发；running → 等到 idle（复用轮询原语）后重发（不做固定次数重试）；
    closed/failed/created/suspended → 清本地状态 + 明确出口；等待超时 → 提示 + 保留 pending（不静默丢消息）。
    返回 True 已发出 / False 未发出。"""
    deadline = time.monotonic() + D.CHAT_TIMEOUT
    resent = False
    while True:
        status, row = _feishu_chat_status(session_id)
        if status == "running":
            status, row = _feishu_poll_until(
                session_id, {"idle", "interrupted", "closed", "failed", "suspended"},
                max(1.0, deadline - time.monotonic()), interval=2.0)
            if status in ("closed", "failed", "suspended"):
                _feishu_reset_session(chat_id, status, row)
                return False
            if status not in ("idle", "interrupted"):
                D._feishu_notify(chat_id, _FEISHU_BUSY_HINT)
                return False
        elif status in ("closed", "failed", "created", "suspended"):
            _feishu_reset_session(chat_id, status, row)
            return False
        try:
            _feishu_prompt_once(session_id, text, attachments)
            return True
        except urllib.error.HTTPError as e:
            err = _feishu_http_err(e)
            if e.code == 409 and not resent:
                # 运行中/boot 窗口：等到 idle 再发一次（不做固定次数重试）
                resent = True
                if time.monotonic() >= deadline:
                    D._feishu_notify(chat_id, _FEISHU_BUSY_HINT)
                    return False
                continue   # 回循环顶部：GET 状态 → running 分支等到 idle 后重发
            if e.code == 400 and "不可对话" in err:
                # 会话状态不可对话（closed/failed/suspended 等）：同一自愈出口——清本地状态 + 明确出口
                _feishu_reset_session(chat_id, _feishu_status_from_error(err))
                return False
            log.error(f"飞书 /prompt 失败（{session_id}）：{err}")
            D._feishu_notify(chat_id, f"消息发送失败：{err}")
            return False
        except Exception as e:
            log.error(f"飞书 /prompt 异常（{session_id}）：{e}")
            D._feishu_notify(chat_id, "消息发送失败，请稍后重试。")
            return False


# ---- 前置对话回放（D8/B11：只回放会话创建前的对话与 dispatcher 侧辅助提示）----

def _feishu_note_replay(sess, kind, content):
    """会话创建前的前置对话/辅助提示暂存，建会话后一次性回放（不落 pending）"""
    sess.setdefault("replay", []).append({"kind": kind, "content": content})


def _feishu_emit(chat_id, sess, kind, content):
    """dispatcher 侧辅助提示（素材失败/超限/文档降级）：有会话直接落库，否则暂存回放"""
    sid = (sess or {}).get("session_id")
    if sid:
        try:
            D.api_board("POST", f"/chat/{sid}/feishu-events",
                      {"events": [{"kind": kind, "content": content}]})
        except Exception as e:
            log.warning(f"飞书辅助提示落库失败（{sid}）：{e}")
    else:
        _feishu_note_replay(sess, kind, content)


def _feishu_replay(chat_id, session_id, sess):
    """建会话后回放前置对话（带方向前缀），清空暂存。不回放用户原文与建卡事件。"""
    events = list((sess or {}).get("replay") or [])
    if not events:
        return
    try:
        D.api_board("POST", f"/chat/{session_id}/feishu-events", {"events": events})
    except Exception as e:
        log.warning(f"飞书前置对话回放失败（{session_id}）：{e}")
    sess["replay"] = []


# ---- 素材（D9：句柄延迟下载，字节不进 state）----

def _feishu_ext_ok(name):
    """#22 ⑥：扩展名是否在飞书素材白名单内（无扩展名 = False）"""
    return Path(name or "").suffix.lower() in _FEISHU_MATERIAL_EXTS


def _feishu_parse_material(msg):
    """file/image 消息 → 下载句柄（只含 message_id/key/type/name/size_hint，不含字节）。
    #22 ⑥：image 句柄名必须带可识别扩展名；file 无 file_name → 返回 None（调用方提示跳过），
    不产生无扩展名句柄（否则 board 侧 /feishu-materials 会 400 拖垮整批素材）。"""
    mtype = getattr(msg, "message_type", "")
    if mtype not in ("file", "image"):
        return None
    try:
        content = json.loads(getattr(msg, "content", "") or "{}")
    except Exception:
        return None
    message_id = getattr(msg, "message_id", None)
    if mtype == "image":
        key = content.get("image_key")
        if not key:
            return None
        name = content.get("file_name") or f"图片-{key[:12]}.png"
        if not _feishu_ext_ok(name):
            name = f"{name}.png"
        return {"message_id": message_id, "image_key": key, "type": "image",
                "name": name, "size_hint": None}
    key = content.get("file_key")
    if not key:
        return None
    name = content.get("file_name")
    if not name:
        return None           # 文件名缺失：不产生无扩展名句柄，由调用方提示「已跳过」
    return {"message_id": message_id, "file_key": key, "type": "file",
            "name": name, "size_hint": None}


def _feishu_download_resource(message_id, key, res_type):
    """下载飞书消息资源（im.v1.message_resource.get）→ (bytes, file_name)；失败抛异常。"""
    req = (D.GetMessageResourceRequest.builder()
           .message_id(message_id)
           .file_key(key)
           .type(res_type)
           .build())
    resp = D.feishu.im.v1.message_resource.get(req)
    if not resp.success():
        raise RuntimeError(f"{resp.code} {resp.msg}")
    data = resp.file.read() if hasattr(resp.file, "read") else resp.file
    return data, getattr(resp, "file_name", None)


def _feishu_upload_materials(session_id, files):
    """base64 上传到 /feishu-materials；files = [{"name","data"(bytes)}]"""
    payload = {"files": [{"name": f["name"],
                          "content_b64": base64.b64encode(f["data"]).decode()} for f in files]}
    return D.api_board("POST", f"/chat/{session_id}/feishu-materials", payload)


def _feishu_download_pending(chat_id, sess, session_id):
    """项目选定后下载 pending 素材（句柄 → 字节 → 上传）→ 返回存储名列表（放进 attachments）。"""
    handles = list(((sess.get("pending") or {}).get("materials")) or [])
    sess.setdefault("pending", {"text": "", "materials": []})["materials"] = []
    if not handles:
        return []
    files = []
    for h in handles:
        key = h.get("file_key") or h.get("image_key")
        try:
            data, fname = D._feishu_download_resource(h.get("message_id"), key, h.get("type") or "file")
        except Exception as e:
            log.warning(f"飞书素材下载失败（{h.get('name')}）：{e}")
            D._feishu_notify(chat_id, f"文件「{h.get('name')}」下载失败：{e}")
            _feishu_emit(chat_id, sess, "system", f"[飞书] 素材「{h.get('name')}」下载失败：{e}")
            continue
        # #22 ⑥：上传前最终文件名兜底——扩展名不在白名单则 image 补 .png，其它跳过并提示
        name = fname or h.get("name") or key
        if not _feishu_ext_ok(name):
            if (h.get("type") or "") == "image":
                name = f"{name or key}.png"
            else:
                label = h.get("name") or name
                D._feishu_notify(chat_id, f"文件「{label}」文件名缺失或扩展名不受支持，已跳过。")
                _feishu_emit(chat_id, sess, "system",
                             f"[飞书] 素材「{label}」文件名缺失或扩展名不受支持，已跳过")
                continue
        files.append({"name": name, "data": data})
    if not files:
        return []
    try:
        resp = _feishu_upload_materials(session_id, files)
    except urllib.error.HTTPError as e:
        msg = _feishu_http_err(e)
        D._feishu_notify(chat_id, f"参考文件上传失败：{msg}")
        _feishu_emit(chat_id, sess, "system", f"[飞书] 参考文件上传失败：{msg}")
        return []
    except Exception as e:
        log.warning(f"飞书参考文件上传异常：{e}")
        D._feishu_notify(chat_id, "参考文件上传失败，请重试。")
        return []
    names = []
    for f in (resp or {}).get("files") or []:
        fn = f.get("filename")
        if fn:
            names.append(fn)
        if f.get("over_read_limit"):
            hint = f"文件 {fn} 超过 agent 单文件读取上限 1MB，可能读取不完整"
            D._feishu_notify(chat_id, hint)
            _feishu_emit(chat_id, sess, "system", f"[飞书] {hint}")
    return names


# ---- 正文抓取链（D10：docx 直链 / wiki 两跳 / 普通 http；每步失败降级仅传链接）----

def _feishu_find_links(text):
    return re.findall(r"https?://[^\s<>\"']+", text or "")


def _feishu_ip_public(ip_str):
    """IP 是否属于可抓取的公网地址（私有/回环/链路本地/保留/多播/未指定 一律 False）。"""
    try:
        ip = ipaddress.ip_address(str(ip_str))
    except Exception:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def _feishu_url_allowed(url):
    """#22 ⑤ SSRF 守卫：仅允许公网 http/https。
    拒 scheme 非 http(s)、host 为空/localhost/.local/.internal；DNS 解析出任一 IP 属于
    私有/回环/链路本地/保留/多播/未指定，或解析失败 → 拒。"""
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    return all(_feishu_ip_public(info[4][0]) for info in infos)


class _FeishuRedirectGuard(urllib.request.HTTPRedirectHandler):
    """#22 ⑤：重定向每一跳都复跑 SSRF 守卫。默认 handler 直接跟随 Location，
    公网地址 302 到回环/链路本地即绕过「只校验原文 URL」的守卫。"""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not D._feishu_url_allowed(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"{_FEISHU_LINK_REJECT_HINT}（重定向目标 {newurl}）", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 抓正文专用 opener：与共享 _opener 隔离，redirect 每跳复校 SSRF
_feishu_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             _FeishuRedirectGuard())


def _feishu_connected_ip(resp):
    """已建立连接的对端 IP（DNS 重绑定兜底：连接到私有地址即拒，不读响应体）。"""
    try:
        return resp.fp.raw._sock.getpeername()[0]
    except Exception:
        return None


def _feishu_is_feishu_host(url):
    """#22 ⑤：host 是否属于飞书域名（feishu.cn / larksuite.com / feishu.net 及其子域，大小写不敏感）"""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    return any(host == dom or host.endswith("." + dom) for dom in _FEISHU_DOMAINS)


def _feishu_fetch_link_text(url, cap=200 * 1024, timeout=10):
    """普通 http(s) 抓正文：urllib（走禁代理 opener）→ 去标签 → 压缩空白，cap 200KB。
    #22 ⑤：先过 SSRF 守卫，非公网地址直接拒绝（抛 ValueError，调用方降级提示）；
    请求走 `_feishu_opener`（重定向每跳复校），连接建立后再核对对端 IP（DNS 重绑定兜底）。"""
    if not D._feishu_url_allowed(url):
        raise ValueError(_FEISHU_LINK_REJECT_HINT)
    req = urllib.request.Request(url, headers={"User-Agent": "req-board-dispatcher"})
    with _feishu_opener.open(req, timeout=timeout) as resp:
        peer = _feishu_connected_ip(resp)
        if peer is not None and not _feishu_ip_public(peer):
            raise ValueError(f"{_FEISHU_LINK_REJECT_HINT}（对端 {peer}）")
        raw = resp.read(cap)
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _feishu_docx_raw(document_id):
    """docx.v1.document.raw_content（token 即 document_id）→ 正文文本；失败抛异常。"""
    req = D.RawContentDocumentRequest.builder().document_id(document_id).build()
    resp = D.feishu.docx.v1.document.raw_content(req)
    if not resp.success():
        raise RuntimeError(f"{resp.code} {resp.msg}")
    return (resp.data.content if resp.data else "") or ""


def _feishu_wiki_one_level(node_token):
    """wiki 两步：先 wiki v2 Space.get_node 取 obj_token/obj_type（只解析一层），
    obj_type=='docx' 才调 raw_content；其它类型降级。返回 (text|None, note|None)。"""
    req = D.GetNodeSpaceRequest.builder().token(node_token).build()
    resp = D.feishu.wiki.v2.space.get_node(req)
    if not resp.success():
        raise RuntimeError(f"{resp.code} {resp.msg}")
    node = resp.data.node if resp.data else None
    obj_type = getattr(node, "obj_type", None)
    obj_token = getattr(node, "obj_token", None)
    if obj_type == "docx" and obj_token:
        return _feishu_docx_raw(obj_token), None
    return None, f"该类型文档（{obj_type or '未知'}）暂不支持抓正文"


def _feishu_fetch_doc_text(url):
    """按 URL 形态抓正文；返回 (text|None, note|None)。非 http(s) 不抓。
    #22 ⑤：`/docx/`、`/wiki/` 路径**仅在飞书域名下**才走飞书文档 API，否则按普通链接处理
    （并过 SSRF 守卫）；被守卫拒绝时返回降级提示（不阻断本轮）。"""
    if not re.match(r"^https?://", url or ""):
        return None, None
    try:
        if _feishu_is_feishu_host(url):
            m = re.search(r"/docx/([A-Za-z0-9]+)", url)
            if m:
                return _feishu_docx_raw(m.group(1)), None
            m = re.search(r"/wiki/([A-Za-z0-9]+)", url)
            if m:
                return _feishu_wiki_one_level(m.group(1))
        if not D._feishu_url_allowed(url):
            return None, _FEISHU_LINK_REJECT_HINT
        return D._feishu_fetch_link_text(url), None
    except Exception as e:
        return None, f"链接内容抓取失败：{e}"


def _feishu_expand_text(chat_id, sess, text):
    """把文本里的链接抓成正文追加；失败降级为仅链接 + 提示（不阻断本轮）。
    #22 ⑤：非飞书文档链接先过 SSRF 守卫，被拒只提示不抓取。"""
    out = text or ""
    for url in _feishu_find_links(out):
        if not _feishu_is_feishu_host(url) and not D._feishu_url_allowed(url):
            D._feishu_notify(chat_id, f"{_FEISHU_LINK_REJECT_HINT}（{url}）")
            _feishu_emit(chat_id, sess, "system", f"[飞书] {_FEISHU_LINK_REJECT_HINT}（{url}）")
            continue
        doc_text, note = _feishu_fetch_doc_text(url)
        if doc_text:
            out += f"\n\n[链接正文 {url}]\n{doc_text}"
        elif note:
            D._feishu_notify(chat_id, f"{note}（{url}）")
            _feishu_emit(chat_id, sess, "system", f"[飞书] {note}（{url}）")
    return out


# ---- 建卡会话编排（D8/D8′/D11）----

def _feishu_boot_and_prompt(chat_id, sess):
    """#22 D8：建会话 → 等 idle → 回放前置对话 → 发首轮 prompt → 等待本轮结束。
    409 → 提示两条出口并保留 pending；boot 超时/failed → 提示 + 保留 session_id（下次走 D8‴ 对账）。"""
    project = sess.get("project")
    try:
        resp = D.api_board("POST", "/chat", {"project": project, "channel": "feishu"})
    except urllib.error.HTTPError as e:
        if e.code == 409:
            D._feishu_notify(chat_id, "该项目已有进行中的飞书建卡会话：在飞书回复『结束建卡』，"
                                    "或在看板「飞书建卡」视图点『关闭会话』。")
            return None
        msg = _feishu_http_err(e)
        log.error(f"飞书建会话失败（{project}）：{msg}")
        D._feishu_notify(chat_id, f"创建会话失败：{msg}")
        return None
    except Exception as e:
        log.error(f"飞书建会话异常（{project}）：{e}")
        D._feishu_notify(chat_id, "创建会话失败，请稍后重试。")
        return None
    session_id = (resp or {}).get("id")
    if not session_id:
        D._feishu_notify(chat_id, "创建会话失败（未返回会话 id）。")
        return None
    sess["session_id"] = session_id
    sess["updated_at"] = time.time()
    _feishu_save()
    status, row = _feishu_poll_until(session_id, {"idle"}, D.FEISHU_BOOT_WAIT_SEC, interval=1.0)
    if status != "idle":
        if status == "failed":
            D._feishu_notify(chat_id, "会话启动失败，回复任意消息重试。")
        else:
            D._feishu_notify(chat_id, "会话启动超时，回复任意消息重试。")
        return session_id   # 保留 session_id，下次消息走 D8‴ 对账
    _feishu_replay(chat_id, session_id, sess)
    text = _feishu_expand_text(chat_id, sess, (sess.get("pending") or {}).get("text") or "")
    attachments = _feishu_download_pending(chat_id, sess, session_id)
    if not text and not attachments:
        text = "（请开始建卡）"
    if _feishu_send_prompt(chat_id, session_id, text, attachments):
        sess["pending"] = {"text": "", "materials": []}
        sess["updated_at"] = time.time()
        _feishu_save()
        _feishu_await_round(chat_id, session_id, sess.get("last_event_id", 0))
    return session_id


def _feishu_send_user_prompt(chat_id, sess, text):
    """活跃建卡会话的后续讨论轮（含链接抓取 + 待续发 pending 合并）。"""
    session_id = sess.get("session_id")
    pending = sess.get("pending") or {}
    combined = "\n".join(x for x in [pending.get("text"), text] if x)
    body = _feishu_expand_text(chat_id, sess, combined)
    attachments = _feishu_download_pending(chat_id, sess, session_id)
    if _feishu_send_prompt(chat_id, session_id, body, attachments):
        sess["pending"] = {"text": "", "materials": []}
        sess["updated_at"] = time.time()
        _feishu_save()
        _feishu_await_round(chat_id, session_id, sess.get("last_event_id", 0))
    else:
        sess.setdefault("pending", {"text": "", "materials": []})["text"] = combined
        _feishu_save()


def _feishu_ask_project(chat_id, sess):
    text = _feishu_project_ask_text()
    sess["awaiting_project"] = True
    _feishu_note_replay(sess, "agent", f"[飞书] 机器人：{text}")
    D._feishu_notify(chat_id, text)


def _feishu_new_sess(project=None, text="", handle=None):
    materials = [handle] if handle else []
    return {"project": project, "session_id": None, "last_event_id": 0,
            "pending": {"text": text or "", "materials": materials},
            "awaiting_project": project is None, "project_tries": 0,
            "replay": [], "updated_at": time.time()}


def _feishu_handle_text(chat_id, text):
    """文本消息处理：无会话 → 建卡讨论（解析项目）；有会话无 session → 项目选择；有 session → 续跑。"""
    fc = _feishu_chats()
    sess = fc.get(chat_id)
    if sess is None:
        sess = D._feishu_new_sess(None, text)
        fc[chat_id] = sess
        project = _feishu_resolve_project(text)
        if project:
            sess["project"] = project
            sess["awaiting_project"] = False
            _feishu_boot_and_prompt(chat_id, sess)
        else:
            _feishu_ask_project(chat_id, sess)
        _feishu_save()
        return
    if sess.get("session_id"):
        _feishu_send_user_prompt(chat_id, sess, text)
        return
    # 会话已建但尚未拿到 session_id（等 idle / boot 超时）→ 视为续跑，走 D8‴ 对账
    project = _feishu_resolve_project(text)
    if project and not sess.get("awaiting_project"):
        sess["project"] = project
    if sess.get("awaiting_project"):
        if project:
            sess["project"] = project
            sess["awaiting_project"] = False
            _feishu_note_replay(sess, "user", f"[飞书] 用户：{text}")
            _feishu_boot_and_prompt(chat_id, sess)
        else:
            # #24：追问项目期间收到的文本先累积进 pending（「新建建卡」→ 需求原文 → 项目名
            # 三步序里需求原文不再被丢弃），选定项目后随首轮 prompt 一起发给 agent。
            if text:
                pend = sess.setdefault("pending", {"text": "", "materials": []})
                pend["text"] = "\n".join(x for x in [pend.get("text"), text] if x)
            sess["project_tries"] = sess.get("project_tries", 0) + 1
            if sess["project_tries"] >= 2:
                D._feishu_notify(chat_id, _feishu_project_ask_text()
                               + "\n仍无法识别项目，本次建卡已取消，可重新发送需求。")
                fc.pop(chat_id, None)
            else:
                _feishu_ask_project(chat_id, sess)
    else:
        _feishu_send_user_prompt(chat_id, sess, text)
    _feishu_save()


def _feishu_handle_material(chat_id, handle):
    """素材消息处理：有活跃旧建卡会话 → 句柄存 pending（延迟下载）走旧链路；无旧会话 → #44 交给助手
    （下载后作为本轮附件提交，不再新建建卡会话追问项目）。"""
    fc = _feishu_chats()
    sess = fc.get(chat_id)
    if sess is None:
        _feishu_material_to_assistant(chat_id, handle)
        return
    sess.setdefault("pending", {"text": "", "materials": []})
    sess["pending"].setdefault("materials", []).append(handle)
    if sess.get("session_id"):
        session_id = sess["session_id"]
        attachments = _feishu_download_pending(chat_id, sess, session_id)
        if attachments and _feishu_send_prompt(chat_id, session_id, "（上传了参考文件）", attachments):
            sess["updated_at"] = time.time()
            _feishu_save()
            _feishu_await_round(chat_id, session_id, sess.get("last_event_id", 0))
    elif not sess.get("awaiting_project"):
        _feishu_ask_project(chat_id, sess)
    _feishu_save()


def _feishu_close_chat(chat_id):
    """#22 D8′：释放路径 ①（指令 结束建卡/新建建卡）——close 看板会话 + 清本地状态 + 回执。"""
    sess = _feishu_chats().get(chat_id)
    sid = (sess or {}).get("session_id")
    if sid:
        try:
            D.api_board("POST", f"/chat/{sid}/close")
        except Exception as e:
            log.warning(f"飞书关闭会话失败（{sid}）：{e}")
    _feishu_chats().pop(chat_id, None)
    _feishu_save()
    D._feishu_notify(chat_id, "已结束本次建卡会话。回复『新建建卡』开始新的建卡会话。")


def _feishu_start_build(chat_id):
    """#24：无活跃旧建卡会话时的「新建建卡」= 旧建卡新建分支——清掉残留本地状态后起一轮
    新的单项目建卡讨论。后续文本因本地会话存在继续走旧链路（未定项目时由旧链路追问）。
    这里只回一条普通提示（不落前置回放），避免与随后的项目追问重复一条「机器人」回放事件。"""
    sess = _feishu_chats().get(chat_id)
    sid = (sess or {}).get("session_id")
    if sid:
        try:
            D.api_board("POST", f"/chat/{sid}/close")
        except Exception as e:
            log.warning(f"飞书关闭会话失败（{sid}）：{e}")
    _feishu_chats().pop(chat_id, None)
    _feishu_chats()[chat_id] = D._feishu_new_sess(None, "")
    _feishu_save()
    D._feishu_notify(chat_id, D.FEISHU_NEW_BUILD_HINT)


def _feishu_route_text(chat_id, text, message_id=None):
    """#24 文本路由（on_message 的 ③′-⑥）：
    ① 有活跃旧建卡会话（state["feishu_chats"][chat_id] 命中）→ 三条指令与所有文本走旧链路；
    ② 无活跃旧会话且整行精确规范化「新建建卡」→ 旧建卡新建分支；
    ③ 无活跃旧会话的「结束建卡」「确认建卡」→ 只回状态提示，不启动任何会话；
    ④ 其余普通文本 → 看板助手路由。
    原「无活跃会话时默认新建单项目建卡会话」的兜底分支已删除（普通文本改由助手接管）。
    #44：`message_id` 只进 assistant/legacy_text/material 三类 payload（worker 据此贴/撤 Typing 表情）；
    close/confirm/new 与未入队的提示不带（不贴表情）。"""
    sess = _feishu_chats().get(chat_id)
    if sess is not None:
        if _feishu_is_cmd(text, FEISHU_CMD_END) or _feishu_is_cmd(text, FEISHU_CMD_NEW):
            D._feishu_enqueue(chat_id, {"kind": "close", "cmd": text})
            return
        if _feishu_is_cmd(text, FEISHU_CMD_CONFIRM):
            D._feishu_enqueue(chat_id, {"kind": "confirm", "cmd": text})
            return
        D._feishu_enqueue(chat_id, {"kind": "legacy_text", "text": text, "message_id": message_id})
        return
    if _feishu_is_cmd(text, FEISHU_CMD_NEW):
        D._feishu_enqueue(chat_id, {"kind": "new", "cmd": text})
        return
    if _feishu_is_cmd(text, FEISHU_CMD_END):
        D._feishu_notify(chat_id, D.FEISHU_NO_SESSION_END_HINT)
        return
    if _feishu_is_cmd(text, FEISHU_CMD_CONFIRM):
        D._feishu_notify(chat_id, D.FEISHU_NO_SESSION_CONFIRM_HINT)
        return
    D._feishu_enqueue(chat_id, {"kind": "assistant", "text": text, "message_id": message_id})


def _feishu_confirm(chat_id, sess):
    """#22 ① 飞书侧「确认建卡」：读会话行 → 已建卡/无草稿/正常三条出口。
    正常 → POST /chat/<id>/confirm 驱动 create_requirement → 等本轮结束把建卡结果回发。
    400/409 用看板错误文案明示失败原因（不静默）。"""
    sid = (sess or {}).get("session_id")
    if not sid:
        D._feishu_notify(chat_id, "还没有草稿，请先和机器人讨论出草稿。")
        return
    status, row = _feishu_chat_status(sid)
    if status == "created":
        rid = (row or {}).get("req_id")
        extra = f" #{rid}" if rid else ""
        D._feishu_notify(chat_id, f"本会话已建卡{extra}，回复『新建建卡』开始新会话。")
        return
    if not ((row or {}).get("draft_title") or "").strip():
        D._feishu_notify(chat_id, "还没有草稿，请先和机器人讨论出草稿。")
        return
    sess["draft_prompted"] = (row or {}).get("draft_title")
    try:
        D.api_board("POST", f"/chat/{sid}/confirm")
    except urllib.error.HTTPError as e:
        D._feishu_notify(chat_id, f"确认建卡失败：{_feishu_http_err(e)}")
        return
    except Exception as e:
        log.error(f"飞书确认建卡异常（{sid}）：{e}")
        D._feishu_notify(chat_id, "确认建卡失败，请稍后重试。")
        return
    sess["updated_at"] = time.time()
    _feishu_save()
    _feishu_await_round(chat_id, sid, sess.get("last_event_id", 0))


def _feishu_draft_ready_prompt(chat_id, sess, row):
    """#22 ①：每轮结束读会话行，草稿就绪且本轮尚未提示过 → 提示「回复『确认建卡』」。
    同一草稿只提示一次；已建卡（req_id）不再提示。"""
    title = ((row or {}).get("draft_title") or "").strip()
    if not title or sess is None or (row or {}).get("req_id"):
        return
    if sess.get("draft_prompted") == title:
        return
    sess["draft_prompted"] = title
    D._feishu_notify(chat_id, f"草稿已就绪：{title}。回复『确认建卡』落卡；回复其它内容继续讨论。")


def _feishu_await_round(chat_id, session_id, after_id):
    """#22 D11：轮询 /chat/<id> + /events?after= 直到 status ∉ running；agent 正文
    （kind='message'，见 acp_shared._flush_message）发回飞书——kind='agent' 是回放事件
    （机器人自己说过的话），**不得再回发**（否则重复）。
    上限 CHAT_TIMEOUT + 60s。interrupted/failed → 提示并保留 session 供下一条消息续跑。
    每轮结束：草稿就绪则提示「回复『确认建卡』」。"""
    deadline = time.monotonic() + D.CHAT_TIMEOUT + 60
    last_id = after_id or 0
    while True:
        status, row = _feishu_chat_status(session_id)
        try:
            events = D.api_board("GET", f"/chat/{session_id}/events?after={last_id}") or []
        except Exception as e:
            log.warning(f"飞书轮次事件拉取失败（{session_id}）：{e}")
            events = []
        for ev in events:
            eid = ev.get("id")
            if isinstance(eid, int) and eid > last_id:
                last_id = eid
            if ev.get("kind") == "message" and ev.get("content"):
                D.send_text_chunked(chat_id, ev["content"])
        sess = _feishu_chats().get(chat_id)
        if sess is not None:
            sess["last_event_id"] = last_id
            sess["updated_at"] = time.time()
        _feishu_save()
        if status is not None and status != "running":
            if status in ("interrupted", "failed"):
                D._feishu_notify(chat_id, "本轮执行中断或失败，可回复任意消息继续。")
            _feishu_draft_ready_prompt(chat_id, sess, row)
            return last_id
        if time.monotonic() >= deadline:
            D._feishu_notify(chat_id, "本轮执行超时，可回复任意消息查看进展。")
            return last_id
        time.sleep(2)


