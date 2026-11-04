"""#32 Phase 4：mcp_servers 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import is_run_local_supervisor
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import re
import threading
import urllib.request

from board_modules.runtime import resolve
from board_modules.services.requirements import _add_review, _log_status_change, req_add
from board_modules.services.dispatcher_ops import _reqchat_launchd_log_dir, _tail_file_lines
from board_modules.services.chat_runtime import CREATE_TOOL_SERVER, _chat_add_event, _chat_set, _copy_chat_materials_to_artifact
# 需求对话产物读写：读=文本类；写=文本类 + .txt/.json（修订文档场景），与调度器产物目录同一位置
REQ_CHAT_TEXT_EXTS = {".md", ".svg", ".txt", ".json"}
REQ_CHAT_WRITE_EXTS = {".md", ".svg", ".txt", ".json"}
REQ_CHAT_READ_MAX = 20000    # read_artifact 单文件返回字符上限
REQ_CHAT_WRITE_MAX = 50000   # write_artifact 单文件内容字符上限
# #434 需求助手评审写回：评审记录 append-only 落 req_reviews（round 服务端自增、reviewer 服务端按
# 会话实时模型填）；verdict 采纳 skill 词表（pass/block/downgrade/close），自由字符串一律拒（fail-closed）
REQ_CHAT_REVIEW_MAX = 20000
REQ_CHAT_REVIEW_VERDICTS = ('pass', 'block', 'downgrade', 'close')
# #434 需求助手可写状态：仅 later(暂缓)/open(待开发) 两值——终审置 open 即可进调度器；
# doing/done 属调度器流转，不由需求助手写（open 会让调度器立即捞卡，工具描述里强约束「仅用户明确要求」）
REQ_CHAT_STATUS_WRITE = ('later', 'open')
# #324 需求对话善后工具集：受限重启调度器/项目服务 + 只读日志
REQ_CHAT_DISPATCHER_LABEL = "local.automation.req-board-dispatcher"  # 调度器 launchd label（不猜 PID）
REQ_CHAT_LOG_TAIL_MAX = 500  # read_logs 单次 tail 行数上限（默认 100）
TAPD_API_BASE_URL = "https://api.tapd.cn"


class CreateRequirementMcpServer:
    """建卡会话唯一的写工具（本地 HTTP MCP server，仿 ReadonlyMcpServer）。
    安全 fail-closed：title/project/notes 服务端校验（project 必须等于会话绑定项目，不信任 agent 参数）；
    幂等：会话已建卡（chat_sessions.req_id 非空）时直接返回已有需求 id，杜绝重复建卡；
    状态机联动：建卡成功后把会话置 created（终态），前端据此刷新列表。"""

    def __init__(self, chat_id, project):
        self.chat_id = chat_id
        self.project = project
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _create_mcp_handler(self))
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass

    def tool_create_requirement(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        title = str(args.get("title") or "").strip()
        notes = str(args.get("notes") or "").strip()
        project = str(args.get("project") or "").strip()
        status = str(args.get("status") or "later").strip()
        if not title:
            raise ValueError("title 必填")
        if len(title) > 200:
            raise ValueError("title 超过 200 字符上限")
        if len(notes) > 2000:
            raise ValueError("notes 超过 2000 字符上限")
        if project != self.project:
            raise ValueError(f"project 必须为会话绑定项目: {self.project}（收到 {project!r}）")
        if status not in ("later", "open"):
            raise ValueError("status 仅支持 later（暂缓，默认）/ open（待开发，仅用户明确要求开发时用）")
        # 幂等：会话已建卡则直接返回已有需求，不重复建卡
        with resolve("get_db")() as conn:
            row = conn.execute(
                "SELECT req_id, model, channel FROM chat_sessions WHERE id = ?", (self.chat_id,)).fetchone()
            if row and row["req_id"]:
                return json.dumps({"id": row["req_id"], "project": project, "status": status,
                                   "created": False, "message": "本会话已建卡，返回已有需求"})
        # #303 板内聊天建卡：source 服务端写死 board-chat（不信任 agent 传入），
        # model 取会话建立时从 configOptions 提取的模型（chat_sessions.model），推理强度留空
        chat_model = row["model"] if row else None
        chat_channel = (row["channel"] if row and "channel" in row.keys() else None) or "web"
        created = req_add(title, project, notes, status=status, source="board-chat", model=chat_model)
        _chat_set(self.chat_id, status="created", req_id=created["id"])
        _chat_add_event(self.chat_id, "system", f"已建卡 #{created['id']}（{status}）")
        # #12 建卡收口三步序：req_add INSERT 原 notes（回 created['id']）→ 复制本会话附件到该需求
        # 产物目录（失败降级不写参考行）→ req_update 回写追加复用路径；无附件时不调 req_update。
        notes_final = _copy_chat_materials_to_artifact(self.chat_id, project, created["id"], notes,
                                                       channel=chat_channel)
        if notes_final != notes:
            resolve("req_update")(created["id"], notes=notes_final)
        return json.dumps({"id": created["id"], "title": title, "project": project,
                           "status": status, "created": True})


def _create_mcp_handler(server):
    """绑定 CreateRequirementMcpServer 实例的 HTTP handler（Streamable Transport，仿只读工具 server）"""
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            accept = self.headers.get("Accept", "")
            if "text/event-stream" in accept:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(b"event: message\ndata: " + body + b"\n\n")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("MCP-Protocol-Version", "2025-11-25")
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n)
            rid = None
            try:
                try:
                    rid = json.loads(body).get("id")
                except Exception:
                    pass
                msg = json.loads(body)
                method, i = msg.get("method"), msg.get("id")
                if method == "initialize":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {
                        "protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": CREATE_TOOL_SERVER, "version": "1.0"}}}
                elif method == "ping":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {}}
                elif method == "tools/list":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {"tools": [
                        {"name": "create_requirement",
                         "description": "创建看板需求卡（本会话唯一写操作；project 必须为会话绑定项目，"
                                        "status 默认 later 暂缓、仅用户明确要求开发才用 open；重复调用幂等返回已有需求）",
                         "inputSchema": {"type": "object", "properties": {
                             "title": {"type": "string"}, "project": {"type": "string"},
                             "notes": {"type": "string"}, "status": {"type": "string"}},
                             "required": ["title", "project"]}}]}}
                elif method == "tools/call":
                    p = msg.get("params") or {}
                    name, args = p.get("name"), p.get("arguments") or {}
                    if name != "create_requirement":
                        raise ValueError(f"未知工具: {name!r}")
                    out = server.tool_create_requirement(args)
                    obj = {"jsonrpc": "2.0", "id": i,
                           "result": {"content": [{"type": "text", "text": out}]}}
                elif i is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    raise ValueError(f"method not found: {method}")
                self._reply(obj)
            except Exception as e:
                self._reply({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32603, "message": str(e)}})

    return _Handler


# ---- 需求助手维护工具（#302：读看板数据 + 显式授权写，fail-closed，不依赖审批） ----
class MaintainMcpServer:
    """需求助手（#326 起为中间栏置顶入口 + 右栏独立视图）的工具 MCP server（本地 HTTP，仿 CreateRequirementMcpServer）。
    读：需求字段/产物/调度器执行事件（全部服务端查询，agent 无可注入参数越权）；
    写：create_requirement（建新卡：project 绑定本需求所属项目、status 限 later/open 默认 later、
        每对话至多 1 张卡，幂等防重复）、update_requirement（字段白名单 title/notes/summary，
        req_id 绑定本对话对应需求）、write_artifact（限本需求产物目录内，文件名正则 + 防穿越 +
        扩展名白名单）、tapd_comment（进程内调 TAPD 开放 API，token 读自 ~/.kimi-code/mcp.json，
        不进 agent 上下文）。
    默认只读纪律由系统提示词约束（未获明确修改要求不得调写工具），工具本身按参数校验。"""

    def __init__(self, req_chat_id, req_id):
        self.req_chat_id = req_chat_id
        self.req_id = req_id
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _maintain_mcp_handler(self))
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass

    # ---- 内部辅助 ----
    def _req(self):
        """本对话绑定的需求行（req_id 固定于构造，不信任任何 agent 参数）"""
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT * FROM requirements WHERE id = ?", (self.req_id,)).fetchone()
        if row is None:
            raise ValueError("需求不存在")
        return row

    def _req_dir(self):
        req = self._req()
        root = resolve("ARTIFACTS_ROOT").resolve()
        req_dir = (resolve("ARTIFACTS_ROOT") / req["project"] / f"req-{self.req_id}").resolve()
        if not req_dir.is_relative_to(root):
            raise ValueError("需求产物目录非法")
        return req_dir

    # ---- 读工具 ----
    def tool_get_requirement(self, args):
        req = self._req()
        return json.dumps({
            "id": req["id"], "title": req["title"], "project": req["project"],
            "status": req["status"], "mode": req["mode"], "phase": req["phase"],
            "notes": req["notes"] or "", "summary": req["summary"] or "",
            "created_at": req["created_at"], "updated_at": req["updated_at"],
            "started_at": req["started_at"], "completed_at": req["completed_at"]},
            ensure_ascii=False)

    def tool_list_artifacts(self, args):
        req_dir = self._req_dir()
        req_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for p in sorted(req_dir.iterdir()):
            if p.is_file():
                entries.append({"name": p.name, "size": p.stat().st_size})
        return json.dumps({"dir": str(req_dir), "files": entries}, ensure_ascii=False)

    def tool_read_artifact(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        filename = str(args.get("filename") or "").strip()
        req_dir = self._req_dir()
        if not re.fullmatch(r'[A-Za-z0-9._-]+', filename):
            raise ValueError("非法文件名（仅允许字母数字._-）")
        target = (req_dir / filename).resolve()
        if not target.is_relative_to(req_dir):
            raise ValueError("非法路径")
        if target.suffix.lower() not in REQ_CHAT_TEXT_EXTS:
            raise ValueError(f"不支持的文件类型 {target.suffix}（仅文本类可读）")
        if not target.exists():
            raise ValueError(f"产物不存在: {filename}")
        content = target.read_text(errors="replace")
        if len(content) > REQ_CHAT_READ_MAX:
            content = content[:REQ_CHAT_READ_MAX] + "\n…（内容过长已截断）"
        return json.dumps({"filename": filename, "content": content}, ensure_ascii=False)

    def tool_list_exec_events(self, args):
        """回看调度器执行过程事件（agent_events，只读）：limit 默认 100、上限 500，返回按时间升序"""
        if not isinstance(args, dict):
            args = {}
        try:
            limit = max(1, min(int(args.get("limit") or 100), 500))
        except (TypeError, ValueError):
            limit = 100
        with resolve("get_db")() as conn:
            rows = conn.execute(
                "SELECT id, kind, content, ts FROM agent_events WHERE req_id = ? "
                "ORDER BY id DESC LIMIT ?", (self.req_id, limit)).fetchall()
        events = [{"id": r["id"], "kind": r["kind"], "content": r["content"],
                   "ts": r["ts"]} for r in reversed(rows)]
        return json.dumps({"events": events}, ensure_ascii=False)

    # ---- 写工具（显式授权，服务端校验） ----
    def tool_create_requirement(self, args):
        """建新需求卡：project 必须为本需求所属项目（服务端绑定，不信任 agent）；
        status 仅 later（默认，暂缓）/ open（仅用户明确要求开发时用）；
        #382 同标题幂等防重复（req_chats.created_req_ids 记录本对话已建卡清单），
        不同标题允许建多张（仿 #107 chat_sessions.req_id 的单卡幂等放开为多卡）。"""
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        title = str(args.get("title") or "").strip()
        notes = str(args.get("notes") or "").strip()
        project = str(args.get("project") or "").strip()
        status = str(args.get("status") or "later").strip()
        req = self._req()
        if not title:
            raise ValueError("title 必填")
        if len(title) > 200:
            raise ValueError("title 超过 200 字符上限")
        if len(notes) > 2000:
            raise ValueError("notes 超过 2000 字符上限")
        if project != req["project"]:
            raise ValueError(f"project 必须为所属项目: {req['project']}（收到 {project!r}）")
        if status not in ("later", "open"):
            raise ValueError("status 仅支持 later（暂缓，默认）/ open（待开发，仅用户明确要求开发时用）")
        with resolve("get_db")() as conn:
            row = conn.execute(
                "SELECT created_req_ids, model FROM req_chats WHERE id = ?", (self.req_chat_id,)).fetchone()
        created_ids = [int(x) for x in str(row["created_req_ids"] or "").split(",") if x.strip().isdigit()] if row else []
        # 幂等：#382 本对话已建【同标题】卡则直接返回已有需求（防 agent 重复建同卡）；
        # 不同标题允许建多张——先把已建卡清单查出来，逐张比对 title
        if created_ids:
            with resolve("get_db")() as conn:
                same = conn.execute(
                    f"SELECT id FROM requirements WHERE id IN ({','.join('?' * len(created_ids))}) AND title = ?",
                    (*created_ids, title)).fetchone()
            if same:
                return json.dumps({"id": same["id"], "project": req["project"], "status": status,
                                   "created": False, "message": "本对话已建同标题需求，返回已有需求"})
        # source 服务端写死 board-reqchat（不信任 agent 传入），model 取会话建立时模型
        chat_model = row["model"] if row else None
        created = req_add(title, req["project"], notes, status=status,
                          source="board-reqchat", model=chat_model)
        resolve("_reqchat_set")(self.req_chat_id, created_req_ids=",".join([*map(str, created_ids), str(created["id"])]))
        resolve("_reqchat_add_event")(self.req_chat_id, "system",
                           f"已建卡 #{created['id']}「{created['title']}」（{status}）")
        return json.dumps({"id": created["id"], "title": created["title"],
                           "project": req["project"], "status": status, "created": True},
                          ensure_ascii=False)

    def tool_update_requirement(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        fields = {}
        for key, label, maxlen in (("title", "标题", 200), ("notes", "需求详情", 10000),
                                   ("summary", "开发总结", 10000)):
            if key in args:
                val = str(args[key]).strip()
                if len(val) > maxlen:
                    raise ValueError(f"{label}超过 {maxlen} 字符上限")
                fields[key] = val
        # #434 评审终审「置 open」需要状态写面。刻意只开 later/open 两值：doing/done 属调度器
        # 流转（人工/调度器收口），需求助手写了会与调度推进打架；open 会让调度器立即捞卡，
        # 故工具描述与系统提示词都强约束「仅用户明确要求开始开发 / 终审已拍板通过」
        status = str(args.get("status") or "").strip()
        if status and status not in REQ_CHAT_STATUS_WRITE:
            raise ValueError(f"status 仅限 {'/'.join(REQ_CHAT_STATUS_WRITE)}")
        if not fields and not status:
            raise ValueError("至少提供 title/notes/summary/status 之一")
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT status FROM requirements WHERE id = ?", (self.req_id,)).fetchone()
            if row is None:
                raise ValueError("需求不存在")
            old_status = row["status"]
            sets = [f"{k} = ?" for k in fields]
            vals = list(fields.values())
            if status:
                sets.append("status = ?")
                vals.append(status)
            sets.append("updated_at = ?")
            vals.append(resolve("now_str")())
            conn.execute(f"UPDATE requirements SET {', '.join(sets)} WHERE id = ?",
                         (*vals, self.req_id))
            conn.commit()
        if status and status != old_status:
            # #375 变更来源桶 = agent；detail 如实说明来源——MCP 工具路径没有 HTTP 头，
            # 不能照抄 _status_log_meta 的「X-Actor=...」格式（会显示成不存在的头）
            _log_status_change(self.req_id, old_status, status, "agent",
                               "agent:reqchat（需求助手 MCP 工具面，无 HTTP 头）")
        changed = list(fields) + (["status"] if status else [])
        resolve("_reqchat_add_event")(self.req_chat_id, "system", f"已更新需求字段: {', '.join(changed)}")
        out = {"updated": changed, "req_id": self.req_id}
        if status:
            out["status"] = status
        return json.dumps(out, ensure_ascii=False)

    def tool_write_artifact(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        filename = str(args.get("filename") or "").strip()
        content = str(args.get("content") or "")
        if not filename:
            raise ValueError("filename 必填")
        if len(content) > REQ_CHAT_WRITE_MAX:
            raise ValueError(f"内容超过 {REQ_CHAT_WRITE_MAX} 字符上限")
        req_dir = self._req_dir()
        if not re.fullmatch(r'[A-Za-z0-9._-]+', filename):
            raise ValueError("非法文件名（仅允许字母数字._-）")
        target = (req_dir / filename).resolve()
        if not target.is_relative_to(req_dir):
            raise ValueError("非法路径")
        if target.suffix.lower() not in REQ_CHAT_WRITE_EXTS:
            raise ValueError(f"不支持的文件类型 {target.suffix}（仅 {sorted(REQ_CHAT_WRITE_EXTS)}）")
        req_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        resolve("_reqchat_add_event")(self.req_chat_id, "system", f"已写入产物 {filename}")
        return json.dumps({"written": filename, "bytes": len(content.encode("utf-8"))},
                          ensure_ascii=False)

    def _live_model_name(self):
        """#434 本会话实时底层模型名（评审记录 reviewer 唯一来源）：优先取运行时会话
        上报的模型显示名，进程已回收时回落 req_chats.model 列；都拿不到写 unknown-model。
        刻意不让 agent 传 reviewer——skill 要求署真实底层模型名，而沙箱里 agent 读不到
        config.toml，只能由服务端按「写入时刻的真实模型」填。"""
        model = None
        with resolve("reqchat_lock"):
            rt = resolve("reqchat_runtime").get(self.req_chat_id) or {}
            try:
                model = rt["session"].model_name
            except Exception:
                model = None
        if not model:
            with resolve("get_db")() as conn:
                row = conn.execute("SELECT model FROM req_chats WHERE id = ?",
                                   (self.req_chat_id,)).fetchone()
            model = (row["model"] if row else "") or ""
        return str(model).strip()[:50] or "unknown-model"

    def tool_post_review(self, args):
        """#434 需求助手写回评审记录（append-only，落 req_reviews）：
        round 服务端自增、notes_snapshot 服务端快照、reviewer 服务端按实时模型填——与
        POST /api/requirements/<id>/reviews 共用 _add_review，两条路径口径一致。
        req_id 绑定本对话需求（不信任 agent 参数）；不提供改/删入口。"""
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        content = str(args.get("content") or "").strip()
        if not content:
            raise ValueError("content 必填（评审反馈全文）")
        if len(content) > REQ_CHAT_REVIEW_MAX:
            raise ValueError(f"content 超过 {REQ_CHAT_REVIEW_MAX} 字符上限")
        verdict = str(args.get("verdict") or "").strip()
        if verdict and verdict not in REQ_CHAT_REVIEW_VERDICTS:
            raise ValueError(f"verdict 仅限 {'/'.join(REQ_CHAT_REVIEW_VERDICTS)}（可省略）")
        row = _add_review(self.req_id, self._live_model_name(), verdict, content)
        if row is None:
            raise ValueError("需求不存在")
        resolve("_reqchat_add_event")(self.req_chat_id, "system",
                           f"已追加评审记录（第 {row['round']} 轮，署名 {row['reviewer']}，"
                           f"结论 {row['verdict'] or '无'}）")
        return json.dumps({"req_id": row["req_id"], "round": row["round"],
                           "reviewer": row["reviewer"], "verdict": row["verdict"],
                           "content_chars": len(content)}, ensure_ascii=False)

    # ---- TAPD 评论（HTTP 桥：进程内调 TAPD 开放 API，token 不进 agent 上下文） ----
    def tool_tapd_comment(self, args):
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        workspace_id = str(args.get("workspace_id") or "").strip()
        entry_id = str(args.get("entry_id") or "").strip()
        comment = str(args.get("comment") or "").strip()
        if not re.fullmatch(r'\d{1,20}', workspace_id):
            raise ValueError("workspace_id 必须为数字")
        if not re.fullmatch(r'\d{1,20}', entry_id):
            raise ValueError("entry_id 必须为数字（需求/缺陷 id）")
        if not comment:
            raise ValueError("comment 必填")
        if len(comment) > 2000:
            raise ValueError("comment 超过 2000 字符上限")
        cfg = _tapd_config()
        if not cfg.get("token"):
            raise ValueError("未找到 TAPD 访问令牌（~/.kimi-code/mcp.json 的 mcpServers.tapd.env.TAPD_ACCESS_TOKEN）")
        base = cfg.get("api_base") or TAPD_API_BASE_URL
        # 短 id（≤9 位）补长 id：云端前缀 11 + workspace_id + 9 位补零（与 mcp-server-tapd 一致）
        if len(entry_id) <= 9:
            pre = "11" if "api.tapd.cn" in base else "10"
            entry_id = f"{pre}{workspace_id}{entry_id.zfill(9)}"
        payload = json.dumps({"workspace_id": int(workspace_id),
                              "entry_id": entry_id, "comment": comment}).encode()
        req = urllib.request.Request(
            f"{base}/comments?s=mcp", data=payload, method="POST",
            headers={"Authorization": f"Bearer {cfg['token']}",
                     "Content-Type": "application/json", "Via": "mcp"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read(200000).decode("utf-8", errors="replace")
        except Exception as e:
            raise ValueError(f"TAPD 评论失败: {e}")
        resolve("_reqchat_add_event")(self.req_chat_id, "system",
                           f"已在 TAPD 发表评论（entry {entry_id}）")
        return json.dumps({"posted": True, "entry_id": entry_id, "response": raw[:500]},
                          ensure_ascii=False)

    # ---- #324 善后工具集：受限重启调度器/项目服务 + 只读日志（服务端 subprocess，fail-closed） ----
    def tool_restart_dispatcher(self, args):
        """按 launchd label 用 launchctl kickstart -k 重启调度器（不猜 PID）。
        前置预检：调度器进程树中存在运行中 kimi agent 时拒绝（防孤儿 agent），
        对话自身进程（board 子进程）不在调度器子树，不会误判。
        重启后核对心跳文件更新与 /api/dispatcher-health；调用与结果写 req_chat_events 留痕。"""
        agents = resolve("_dispatcher_kimi_agents")()
        if agents:
            resolve("_reqchat_add_event")(self.req_chat_id, "system",
                               f"拒绝重启调度器：检测到运行中 kimi agent {len(agents)} 个，"
                               f"第一个: {agents[0][:80]}…")
            raise ValueError(
                f"检测到运行中 kimi agent {len(agents)} 个，拒绝重启调度器"
                f"（否则重启后孤儿 agent 改一半代码）：" + "；".join(a[:100] for a in agents[:3]))
        # #8：文案按模式分支——直跑模式不出现 launchctl 字样（实际路径 = 写标记 + SIGTERM exec 自重启）
        step = ("执行写来源标记并发 SIGTERM（exec 自重启）" if is_run_local_supervisor()
                else f"执行 launchctl kickstart -k {REQ_CHAT_DISPATCHER_LABEL}")
        resolve("_reqchat_add_event")(self.req_chat_id, "system",
                           "用户请求重启调度器：预检通过（无运行中 agent），" + step)
        # #421：重启执行体抽成模块级 _restart_dispatcher（网页端 #421 端点同款复用，来源标识区分）
        res = resolve("_restart_dispatcher")(
            "reqchat", note=lambda msg: resolve("_reqchat_add_event")(self.req_chat_id, "system", msg))
        return json.dumps(res, ensure_ascii=False)

    def tool_read_logs(self, args):
        """只读 tail 白名单日志最近 N 行（默认 100，上限 500）+ 可选关键词过滤；禁 -f、限输出大小。
        白名单：/tmp/req_board_dispatcher.log、/tmp/req_board_agent_*.log、
        日志目录下 req-board*.log（含轮转档 .1/.2）、遗留 automation/data/req-board(-dispatcher)*.log
        （resolve 防穿越）。"""
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        path = str(args.get("path") or "").strip()
        try:
            lines = max(1, min(int(args.get("lines") or 100), REQ_CHAT_LOG_TAIL_MAX))
        except (TypeError, ValueError):
            lines = 100
        keyword = str(args.get("keyword") or "").strip()
        resolved = resolve("_reqchat_log_allowed")(path)
        if resolved is None:
            raise ValueError(f"路径不在白名单: {path}（仅限 /tmp/req_board_dispatcher.log、"
                             f"/tmp/req_board_agent_*.log、日志目录下 req-board*.log"
                             f"（{_reqchat_launchd_log_dir()}）")
        if not Path(resolved).is_file():
            raise ValueError(f"日志不存在或不可读: {path}")
        tail = _tail_file_lines(resolved, lines)
        if keyword:
            kw = keyword.lower()
            tail = [ln for ln in tail if kw in ln.lower()]
        content = "\n".join(tail)
        truncated = False
        if len(content) > REQ_CHAT_READ_MAX:
            content = content[:REQ_CHAT_READ_MAX] + "\n…（内容过长已截断）"
            truncated = True
        return json.dumps({"path": resolved, "returned": len(tail), "truncated": truncated,
                           "content": content}, ensure_ascii=False)

    def tool_restart_project_service(self, args):
        """按 projects.json 的 restart 配方（单一事实源）重启白名单项目本地服务。
        service = projects.json 中配置了 restart 配方的项目名（如 my-app/api-server/web），
        未知服务名 fail-closed 拒绝。执行配方 cmd（cwd=项目首仓库路径）后做 verify 就绪校验。"""
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        service = str(args.get("service") or "").strip()
        cfg = resolve("PROJECT_RESTART").get(service)
        if not cfg:
            raise ValueError(f"服务不在重启白名单: {service!r}（仅限 projects.json 配置了 restart 配方的项目）")
        paths = resolve("PROJECT_PATHS").get(service) or []
        if not paths:
            raise ValueError(f"项目 {service} 未配置本地路径")
        cmd, verify, timeout = cfg["cmd"], cfg["verify"], cfg["timeout"]
        resolve("_reqchat_add_event")(self.req_chat_id, "system",
                           f"用户请求重启项目服务 {service}：执行 {cmd[:120]}")
        try:
            # shell=True：配方命令可能含 $(id -u) 等 shell 语法（launchctl kickstart）；
            # projects.json 为本机可信数据，无注入面（与 dispatcher run_project_restart 一致）
            r = resolve("subprocess").run(cmd, shell=True, cwd=paths[0], capture_output=True, timeout=timeout)
        except resolve("subprocess").TimeoutExpired:
            resolve("_reqchat_add_event")(self.req_chat_id, "system", f"重启 {service} 超时（>{timeout}s）")
            raise ValueError(f"重启 {service} 命令执行超时（>{timeout}s）")
        except Exception as e:
            resolve("_reqchat_add_event")(self.req_chat_id, "system", f"重启 {service} 异常: {e}")
            raise ValueError(f"重启 {service} 命令执行异常: {e}")
        if r.returncode != 0:
            reason = (r.stderr or r.stdout or b"").decode(errors="replace").strip()[:200] \
                or f"退出码 {r.returncode}"
            resolve("_reqchat_add_event")(self.req_chat_id, "system", f"重启 {service} 失败: {reason}")
            raise ValueError(f"重启 {service} 失败（退出码 {r.returncode}）：{reason}")
        ready = None
        detail = "重启命令执行成功"
        if verify:
            ready = resolve("_reqchat_wait_url_up")(verify, timeout=timeout)
            detail += f"，就绪校验 {verify} " + ("通过" if ready else f"未通过（{timeout}s 内未返回 200）")
        resolve("_reqchat_add_event")(self.req_chat_id, "system", f"已重启项目服务 {service}：{detail}")
        return json.dumps({"service": service, "restarted": True, "cmd": cmd,
                           "verify": verify or None, "ready": ready, "detail": detail},
                          ensure_ascii=False)


def _tapd_config():
    """读 kimi-code 全局 MCP 配置里的 tapd server 凭据（token/base url），失败返回 {}。"""
    try:
        raw = json.loads(resolve("KIMI_CODE_MCP_JSON").read_text())
        env = raw.get("mcpServers", {}).get("tapd", {}).get("env") or {}
        return {"token": str(env.get("TAPD_ACCESS_TOKEN") or "").strip(),
                "api_base": str(env.get("TAPD_API_BASE_URL") or "").strip()}
    except Exception:
        return {}


def _maintain_mcp_handler(server):
    """绑定 MaintainMcpServer 实例的 HTTP handler（Streamable Transport，仿建卡工具 server）"""
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            accept = self.headers.get("Accept", "")
            if "text/event-stream" in accept:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(b"event: message\ndata: " + body + b"\n\n")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("MCP-Protocol-Version", "2025-11-25")
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n)
            rid = None
            try:
                try:
                    rid = json.loads(body).get("id")
                except Exception:
                    pass
                msg = json.loads(body)
                method, i = msg.get("method"), msg.get("id")
                if method == "initialize":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {
                        "protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": resolve("REQ_CHAT_TOOL_SERVER"), "version": "1.0"}}}
                elif method == "ping":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {}}
                elif method == "tools/list":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {"tools": [
                        {"name": "get_requirement",
                         "description": "查看当前需求全部字段（标题/需求详情/开发总结/状态/时间）",
                         "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "list_artifacts",
                         "description": "列出本需求产物目录（需求分析/方案/技术设计/自测/验收等阶段文档）",
                         "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "read_artifact",
                         "description": "读取本需求产物文件内容（filename 必填，仅文本类）",
                         "inputSchema": {"type": "object", "properties": {
                             "filename": {"type": "string"}}, "required": ["filename"]}},
                        {"name": "list_exec_events",
                         "description": "回看本需求调度器执行过程事件（limit 默认 100，上限 500）",
                         "inputSchema": {"type": "object", "properties": {
                             "limit": {"type": "integer"}}}},
                        {"name": "create_requirement",
                         "description": "【写】在本需求所属项目下新建需求卡（title/notes/status；"
                                        "status 默认 later 暂缓、仅用户明确要求开发时用 open；"
                                        "project 必须为本需求所属项目；本对话已建同标题卡则幂等返回已有需求，"
                                        "不同标题可建多张；"
                                        "仅在用户明确要求建卡并确认初稿后使用）",
                         "inputSchema": {"type": "object", "properties": {
                             "title": {"type": "string"}, "project": {"type": "string"},
                             "notes": {"type": "string"}, "status": {"type": "string"}},
                             "required": ["title", "project"]}},
                        {"name": "update_requirement",
                         "description": "【写】修改本需求卡片字段（title/notes/summary，以及 status："
                                        "仅 later(暂缓)/open(待开发) 两值）。"
                                        "仅在用户明确要求修改时使用；status 置 open 会让调度器立即把卡"
                                        "捞去自动开发，只有用户明确说「开始开发」或评审终审已拍板"
                                        "通过时才可用",
                         "inputSchema": {"type": "object", "properties": {
                             "title": {"type": "string"}, "notes": {"type": "string"},
                             "summary": {"type": "string"},
                             "status": {"type": "string", "enum": list(REQ_CHAT_STATUS_WRITE)}}}},
                        {"name": "write_artifact",
                         "description": "【写】写入本需求产物目录文件（如修订分析/方案文档；"
                                        "仅在用户明确要求修改时使用；filename 仅字母数字._-，文本类扩展名）",
                         "inputSchema": {"type": "object", "properties": {
                             "filename": {"type": "string"}, "content": {"type": "string"}},
                             "required": ["filename", "content"]}},
                        {"name": "post_review",
                         "description": "【写】追加一轮评审记录到本需求（评审过程与结论的唯一留档位置，"
                                        "只追加、不能改删）。content 必填，按 req-board-review skill 的"
                                        "写回规范分段（■ 阻塞项 / ■ 非阻塞建议 / ■ 待拍板 / ■ 结论 /"
                                        " ■ 本轮修订）；verdict 可选，限 pass/block/downgrade/close；"
                                        "轮次 round 与署名 reviewer（本会话实时底层模型）都由服务端填写，"
                                        "不要自己传。仅在用户明确要求评审/记录评审结论时使用；"
                                        "评审内容禁止写进需求详情(notes)/开发总结(summary)——那是 prompt 通道",
                         "inputSchema": {"type": "object", "properties": {
                             "content": {"type": "string"},
                             "verdict": {"type": "string", "enum": list(REQ_CHAT_REVIEW_VERDICTS)}},
                             "required": ["content"]}},
                        {"name": "tapd_comment",
                         "description": "【写】在 TAPD 对需求/缺陷发表评论"
                                        "（workspace_id 项目 ID、entry_id 需求/缺陷 ID、comment 内容；"
                                        "仅在用户明确要求发表时使用）",
                         "inputSchema": {"type": "object", "properties": {
                             "workspace_id": {"type": "string"}, "entry_id": {"type": "string"},
                             "comment": {"type": "string"}},
                             "required": ["workspace_id", "entry_id", "comment"]}},
                        {"name": "restart_dispatcher",
                         "description": "【写·善后】按 launchd label 重启调度器（local.automation.req-board-dispatcher，"
                                        "launchctl kickstart -k 不猜 PID）；前置预检：有运行中 kimi agent 时拒绝；"
                                        "重启后自动核对心跳与 /api/dispatcher-health。"
                                        "仅在用户明确要求重启调度器时使用",
                         "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "read_logs",
                         "description": "【读·善后】只读 tail 白名单日志最近 N 行（lines 默认 100 上限 500）+ "
                                        "可选 keyword 关键词过滤；仅限 /tmp/req_board_dispatcher.log、"
                                        "/tmp/req_board_agent_*.log、日志目录下 req-board*.log"
                                        "（含 .err.log 与轮转档 .1/.2），越权路径被拒",
                         "inputSchema": {"type": "object", "properties": {
                             "path": {"type": "string"}, "lines": {"type": "integer"},
                             "keyword": {"type": "string"}},
                             "required": ["path"]}},
                        {"name": "restart_project_service",
                         "description": "【写·善后】按 projects.json 的 restart 配方重启白名单项目本地服务"
                                        "（service=配置了 restart 配方的项目名，如 my-app/api-server/"
                                        "web；未知服务名被拒），执行后做 verify 就绪校验。"
                                        "仅在用户明确要求重启项目服务时使用",
                         "inputSchema": {"type": "object", "properties": {
                             "service": {"type": "string"}},
                             "required": ["service"]}}]}}
                elif method == "tools/call":
                    p = msg.get("params") or {}
                    name, args = p.get("name"), p.get("arguments") or {}
                    fn = getattr(server, f"tool_{name}", None)
                    if fn is None:
                        raise ValueError(f"未知工具: {name!r}")
                    out = fn(args)
                    obj = {"jsonrpc": "2.0", "id": i,
                           "result": {"content": [{"type": "text", "text": out}]}}
                elif i is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    raise ValueError(f"method not found: {method}")
                self._reply(obj)
            except Exception as e:
                self._reply({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32603, "message": str(e)}})

    return _Handler
