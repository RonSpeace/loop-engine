"""#32 Phase 5：assistant_mcp 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import DIR_MAX_ENTRIES
from acp_shared import FILE_MAX_BYTES
from acp_shared import FILE_MAX_LINES
from acp_shared import FILE_MAX_LINES_LIMIT
from acp_shared import SEARCH_MAX_MATCHES
from acp_shared import TOOL_OUTPUT_MAX_CHARS
from acp_shared import resolve_path_safe
from acp_shared import ro_fit_lines
from acp_shared import ro_glob_paths
from acp_shared import ro_read_continuation
from acp_shared import ro_read_lines
from acp_shared import role_meta
from acp_shared import sensitive_path_blocked
from board_modules.common import STATUS_LABEL_CN
from board_modules.common import _status_label_cn
from board_modules.common import _ts_ms
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import secrets
import threading
import time

from board_modules.runtime import resolve
from board_modules.services.requirements import STATUSES, _log_status_change, req_add, req_list
from board_modules.services.assistant_card import ASSISTANT_CARD_FLOW_FALLBACK, ASSISTANT_CARD_MAX_ITEMS, ASSISTANT_CARD_MAX_PAGES, ASSISTANT_CARD_PAGE_MAX, _assistant_add_card_event, _card_page_payload
from board_modules.services.assistant_runtime import ASSISTANT_CAP_FILE_TOOLS, ASSISTANT_CAP_GIT_OPS, ASSISTANT_CAP_LOCATION, ASSISTANT_CAP_REQ_CARDS, ASSISTANT_CAP_STATUS_WRITE, ASSISTANT_FIELD_MAX, ASSISTANT_FIND_DEFAULT_LIMIT, ASSISTANT_RESULT_DEFAULT, ASSISTANT_RESULT_MAX, ASSISTANT_STATUS_WRITE_SOURCES, ASSISTANT_STATUS_WRITE_TARGETS, ASSISTANT_TOOL_SERVER, _assistant_add_event, _assistant_materials_dir


# ---- #24 飞书看板助手工具面（固定只读业务工具 + 唯一写工具 create_requirement） ----
class AssistantMcpServer:
    """飞书看板助手的工具 MCP server（本地 HTTP，streamable transport，仿 CreateRequirementMcpServer）。

    安全边界（fail-closed）：
    - 工具面**固定 16 个**：projects_status / project_find / req_query / req_card / req_timeline /
      send_requirement_cards / sessions_status / pending_confirmations + 只读代码分析 3 个
      （file_read / file_find / code_search）+ 2 个写工具（create_requirement /
      set_requirement_status）+ 项目代码同步 3 个（project_git_status / project_git_pull /
      project_git_commit，由 assistant_git_ops 开关控制、默认关闭）。
    - **绝不暴露** db_query / read_file / list_dir / 通用 git_* / Bash / Playwright / TAPD 等通用能力；
      三个 project_git_* 只认 projects.json 清单内项目、固定 argv、无 push。
    - **绝不读取 req_reviews**（评审过程与结论永不进 prompt/工具结果）。
    - 字段白名单 + 结果条数上限 + 单字段长度上限；未知工具/未知参数一律拒绝。
    - create_requirement：project 必须在 projects.json 清单内；status 仅 later/open 且**缺省 later**；
      幂等键 = 本助手会话本轮的 source_session_id（同一条飞书消息/轮次重试不重复落卡）；
      来源链接（若给出）写入 notes。
    - #26 只读代码分析：读范围 = 项目清单全部项目路径（跨项目），复用 acp_shared 的路径/敏感文件/
      限量语义；两能力各由 feishu.json 开关控制、**默认关闭且每次调用现读**，关闭时明确回绝。
    - #26 set_requirement_status：仅 暂缓(later) → 待开发(open)，统一走 req_update（含静默期刷新）
      + _log_status_change 审计；幂等（已是 open 不改任何字段、不写审计）。
    - #30 send_requirement_cards：按项目取需求卡清单，**服务端**确定性分页 + 渲染字段（编号/标题/
      中文状态/流程/更新时间），逐页写 kind='card' 事件（dispatcher 侧渲染飞书卡片）；受开关控制、
      默认关闭、每次调用现读；关闭时明确回绝不写任何事件（零副作用）。
    """

    READONLY_TOOLS = ("projects_status", "project_find", "req_query", "req_card",
                      "req_timeline", "send_requirement_cards", "sessions_status",
                      "pending_confirmations", "file_read", "file_find", "code_search")
    WRITE_TOOLS = ("create_requirement", "set_requirement_status")
    GIT_TOOLS = ("project_git_status", "project_git_pull", "project_git_commit")
    ALL_TOOLS = READONLY_TOOLS + WRITE_TOOLS + GIT_TOOLS
    GIT_COMMIT_TOKEN_TTL = 600

    def __init__(self, assistant_session_id, feishu_chat_id, turn_id=None):
        self.assistant_session_id = assistant_session_id
        self.feishu_chat_id = str(feishu_chat_id)
        self.turn_id = turn_id
        # #54：轮次 deadline / 取消事件由 _assistant_prompt_thread 注入；直接调用时为 None
        self.turn_deadline = None
        self.cancel_event = threading.Event()
        self._commit_tokens = {}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _assistant_mcp_handler(self))
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        # 先置取消标记：正在跑的 git 子进程在下个轮询周期终止，不再继续 merge/commit
        try:
            self.cancel_event.set()
        except Exception:
            pass
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass

    # ---- 工具面 ----
    def _tool_defs(self):
        obj = {"type": "object", "properties": {}}
        return [
            {"name": "projects_status",
             "description": "各项目的需求数量（按状态）与运行中会话数；用于回答「哪些项目在跑 / 各项目多少卡」。",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "project_find",
             "description": "按关键词（项目名/目录名）模糊定位项目；返回候选清单与需求数量。零命中或多命中时据此反问用户。",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "req_query",
             "description": "查询需求卡列表（可按项目/状态/标题关键词过滤），返回条数受限的字段白名单列表。",
             "inputSchema": {"type": "object", "properties": {
                 "project": {"type": "string"}, "status": {"type": "string"},
                 "keyword": {"type": "string"}, "limit": {"type": "integer"}}}},
            {"name": "req_card",
             "description": "按需求编号查单张卡（标题/项目/状态/阶段/时间线字段/notes/summary）。",
             "inputSchema": {"type": "object", "properties": {"req_id": {"type": "integer"}},
                             "required": ["req_id"]}},
            {"name": "req_timeline",
             "description": "按需求编号查调度器动作时间线（节点进度、最近事件）。",
             "inputSchema": {"type": "object", "properties": {
                 "req_id": {"type": "integer"}, "limit": {"type": "integer"}},
                 "required": ["req_id"]}},
            {"name": "send_requirement_cards",
             "description": "把某项目的需求卡清单以飞书交互卡片发出（每张卡一个区块，字段 = 编号/标题/"
                            "中文状态/流程/更新时间，**服务端取数与渲染，你不要自行拼卡片或数编号**）。"
                            "需在" + ASSISTANT_CAP_LOCATION + "开启「需求卡清单卡片」；未开启时工具会"
                            "明确回绝并给出该位置，如实转述即可。适用于「把 X 项目的卡清单/一览发我」这类"
                            "清单诉求；查单张卡仍用 req_card。成功后只回一句回执（项目 + 张数 + 页数），"
                            "不要再用文本重复列出同一份清单。",
             "inputSchema": {"type": "object", "properties": {
                 "project": {"type": "string"}, "status": {"type": "string"},
                 "keyword": {"type": "string"}}, "required": ["project"]}},
            {"name": "sessions_status",
             "description": "调度器健康（心跳年龄与飞书连接状态）、运行中的 agent 会话清单、待确认项条数。",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "pending_confirmations",
             "description": "当前待人工确认项清单（按需求编号，含类型与问题），不含评审元数据。",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "create_requirement",
             "description": "创建看板需求卡（本助手唯一写操作）。project 必须是 projects.json 中的项目；"
                            "status 默认 later（暂缓，不会被自动开发捞取），仅用户明确要求开始开发才用 open。"
                            "同一轮重试幂等（不重复落卡）；来源链接放 source_url。",
             "inputSchema": {"type": "object", "properties": {
                 "project": {"type": "string"}, "title": {"type": "string"},
                 "notes": {"type": "string"}, "status": {"type": "string"},
                 "source_url": {"type": "string"}}, "required": ["project", "title"]}},
            {"name": "set_requirement_status",
             "description": "修改已建卡状态（唯一支持：暂缓 later → 待开发 open；需在"
                            + ASSISTANT_CAP_LOCATION + "开启「状态变更」）。仅用户明确要求时调用；"
                            "改成待开发后约 1 分钟会被调度器"
                            "自动纳入开发流程。其它状态转换请引导用户到网页操作。",
             "inputSchema": {"type": "object", "properties": {
                 "req_id": {"type": "integer"}, "status": {"type": "string"}},
                 "required": ["req_id", "status"]}},
            {"name": "file_read",
             "description": "读取项目代码/文本文件（只读分析，需在" + ASSISTANT_CAP_LOCATION
                            + "开启「代码只读分析」）。"
                            "path 为绝对路径或相对 project 根的路径；返回带行号（文件内 1-based 绝对行号）。"
                            "敏感文件（.env、私钥等）拒读；能力未开启时明确回绝。",
             "inputSchema": {"type": "object", "properties": {
                 "path": {"type": "string"}, "project": {"type": "string"},
                 "offset": {"type": "integer"}, "max_lines": {"type": "integer"},
                 "meta": {"type": "boolean"}}, "required": ["path"]}},
            {"name": "file_find",
             "description": "按 glob 模式在项目内列文件（只读分析，需在" + ASSISTANT_CAP_LOCATION
                            + "开启「代码只读分析」）。pattern 相对"
                            "项目根（支持 **）；不传 project 时跨项目清单全部项目检索，返回「项目名: 相对路径」。",
             "inputSchema": {"type": "object", "properties": {
                 "pattern": {"type": "string"}, "project": {"type": "string"},
                 "limit": {"type": "integer"}}, "required": ["pattern"]}},
            {"name": "code_search",
             "description": "按关键词检索项目代码内容（只读分析，需在" + ASSISTANT_CAP_LOCATION
                            + "开启「代码只读分析」）。不传 project 时跨"
                            "项目清单全部项目检索；返回「项目名/相对路径:行号: 行内容」，可据此引用文件与行号。",
             "inputSchema": {"type": "object", "properties": {
                 "keyword": {"type": "string"}, "project": {"type": "string"},
                 "path": {"type": "string"}, "max_matches": {"type": "integer"}},
                 "required": ["keyword"]}},
            {"name": "project_git_status",
             "description": "查看指定项目仓库的当前分支、HEAD、上游与工作区状态（只读，需在"
                            + ASSISTANT_CAP_LOCATION + "开启「代码拉取与提交」）。"
                            "project 必须是项目清单中的项目名；多仓库项目需指定 repo。"
                            "拉取前必须先调用本工具回显项目、仓库、分支与 HEAD。",
             "inputSchema": {"type": "object", "properties": {
                 "project": {"type": "string"}, "repo": {"type": "string"}},
                 "required": ["project"]}},
            {"name": "project_git_pull",
             "description": "拉取指定项目当前分支的最新代码，或处理拉取产生的合并冲突（需在"
                            + ASSISTANT_CAP_LOCATION + "开启「代码拉取与提交」）。"
                            "action=pull（缺省）先 fetch 再快进/合并；action=resolve 需 side=ours/theirs"
                            "（仅内容冲突 UU/AA，delete/rename 需手工）；action=abort 放弃本次合并。"
                            "冲突时现场保留并返回文件清单与四个选项，不得自行覆盖。不 push、不切换分支。",
             "inputSchema": {"type": "object", "properties": {
                 "project": {"type": "string"}, "repo": {"type": "string"},
                 "branch": {"type": "string"}, "remote": {"type": "string"},
                 "action": {"type": "string"}, "side": {"type": "string"}},
                 "required": ["project"]}},
            {"name": "project_git_commit",
             "description": "提交指定项目的改动（需在" + ASSISTANT_CAP_LOCATION
                            + "开启「代码拉取与提交」）。不带 confirm_token 时只做预览："
                            "返回待提交清单、目标分支与确认 token，**必须先把摘要发给用户**；"
                            "用户确认后的下一轮再带同一 token 提交（同轮不能提交）。"
                            "未跟踪文件需显式列入 include_untracked。合并冲突须先 resolve 再提交。",
             "inputSchema": {"type": "object", "properties": {
                 "project": {"type": "string"}, "repo": {"type": "string"},
                 "message": {"type": "string"}, "confirm_token": {"type": "string"},
                 "include_untracked": {"type": "array", "items": {"type": "string"}},
                 "allow_sensitive": {"type": "array", "items": {"type": "string"}},
                 "allow_secret_findings": {"type": "boolean"}},
                 "required": ["project", "message"]}},
        ]

    def call(self, name, args):
        if name not in self.ALL_TOOLS:
            raise ValueError(f"未知工具: {name!r}（助手工具面仅 {', '.join(self.ALL_TOOLS)}）")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValueError("arguments 必须为对象")
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            raise ValueError(f"未知工具: {name!r}")
        return fn(args)

    # ---- 内部辅助 ----
    @staticmethod
    def _clip(text, limit=ASSISTANT_FIELD_MAX):
        s = "" if text is None else str(text)
        return s if len(s) <= limit else s[:limit] + "…（已截断）"

    @staticmethod
    def _int_arg(args, key, default, lo=1, hi=ASSISTANT_RESULT_MAX):
        try:
            v = int(args.get(key))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(v, hi))

    @staticmethod
    def _reject_unknown(args, allowed):
        unknown = sorted(set(args) - set(allowed))
        if unknown:
            raise ValueError(f"未知参数: {', '.join(unknown)}（仅支持 {', '.join(allowed)}）")

    @staticmethod
    def _require(req_id):
        with resolve("get_db")() as conn:
            row = conn.execute("SELECT * FROM requirements WHERE id = ?", (req_id,)).fetchone()
        if row is None:
            raise ValueError(f"需求 #{req_id} 不存在")
        return row

    # ---- #26 能力开关 / 只读范围 / 限量 ----
    @staticmethod
    def _cap_or_raise(field, cap_name):
        """能力开关关闭时明确回绝（文案只含中文能力名 + 网页路径，不出现配置字段名）。"""
        if not _assistant_cap_enabled(field):
            raise ValueError(
                f"{cap_name}能力未开启。请打开{ASSISTANT_CAP_LOCATION}勾选该开关后保存"
                "（保存即生效，无需重启调度器）。")

    @staticmethod
    def _assistant_roots():
        """#26 能力一读范围 = 项目清单全部项目路径（跨项目）；调用时现读，故新增项目即时可见。"""
        roots = []
        for paths in resolve("PROJECT_PATHS").values():
            for one in (paths or []):
                try:
                    r = Path(one).resolve()
                except OSError:
                    continue
                if r not in roots:
                    roots.append(r)
        return roots

    @staticmethod
    def _project_roots(project, roots):
        """项目名 → 该项目全部可读根（按 projects.json 顺序，须在 roots 内）；无可用根即 raise。"""
        if project not in resolve("PROJECT_PATHS"):
            raise ValueError(f"未知项目: {project}（必须在 projects.json 清单内）")
        out = []
        for one in (resolve("PROJECT_PATHS").get(project) or []):
            try:
                r = Path(one).resolve()
            except OSError:
                continue
            if r in roots and r not in out:
                out.append(r)
        if not out:
            raise ValueError(f"项目 {project} 无可用路径")
        return out

    @staticmethod
    def _root_label(project, root, roots_of_project):
        """扫描结果的路径前缀：单仓库项目 = 项目名；多仓库项目加仓库目录名（防跨根同名路径歧义）。"""
        return str(project) if len(roots_of_project) <= 1 else f"{project}/{root.name}"

    @staticmethod
    def _strict_int(args, key, default, lo, hi):
        """严格整数参数：越界/类型非法一律 raise（不走 _int_arg 的夹取语义）。"""
        v = args.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < lo or v > hi:
            raise ValueError(f"{key} 必须为 {lo}..{hi} 的整数: {v!r}")
        return v

    def _resolve_in_project(self, path_str, project, roots):
        """`project` 内相对路径 → 逐个根解析（多仓库项目取**实际存在**的那个根，全不存在时取首个候选取报错）；
        绝对路径按 roots 判定。"""
        if Path(path_str).is_absolute():
            return resolve_path_safe(path_str, self._project_roots(project, roots)[0], roots)
        candidates = [p for p in (resolve_path_safe(path_str, r, roots)
                                  for r in self._project_roots(project, roots)) if p is not None]
        for p in candidates:
            if p.exists():
                return p
        return candidates[0] if candidates else None

    def _scan_targets(self, args, roots):
        """跨项目扫描目标 [(标签, 搜索目录)]：给 project 只扫该项目（可再给 path 收窄到子目录）；
        不给 = **清单全部项目的全部路径**各扫一份。标签已含收窄后的相对子路径，便于回答里直接引用。"""
        project = args.get("project")
        path_str = args.get("path")
        if project:
            bases = self._project_roots(project, roots)
            if not path_str:
                return [(self._root_label(project, r, bases), r) for r in bases]
            p = self._resolve_in_project(path_str, project, roots)
            if p is None:
                raise ValueError(f"越权/非法路径: {path_str!r}")
            if not p.is_dir():
                raise ValueError(f"path 需为项目内目录（单文件请用 file_read）: {path_str!r}")
            base = next((b for b in bases if b == p or b in p.parents), bases[0])
            rel = p.relative_to(base).as_posix()
            label = self._root_label(project, base, bases)
            return [(label if rel == "." else f"{label}/{rel}", p)]
        if path_str:
            raise ValueError("指定 path 时必须同时给 project（path 相对项目根）")
        if not roots:
            raise ValueError("项目清单为空，无可读路径")
        targets = []
        for name in sorted(resolve("PROJECT_PATHS")):
            bases = self._project_roots(name, roots)
            targets += [(self._root_label(name, r, bases), r) for r in bases]
        return targets

    # ---- 只读工具 ----
    def tool_projects_status(self, args):
        self._reject_unknown(args, ())
        with resolve("get_db")() as conn:
            counts = {}
            for r in conn.execute("SELECT project, status, COUNT(*) AS n FROM requirements "
                                  "GROUP BY project, status"):
                counts.setdefault(r["project"], {})[r["status"]] = r["n"]
            active = {r["project"]: r["n"] for r in conn.execute(
                "SELECT req.project AS project, COUNT(*) AS n FROM agent_sessions a "
                "JOIN requirements req ON req.id = a.req_id WHERE a.status = 'running' "
                "GROUP BY req.project")}
        projects = []
        for name in resolve("PROJECT_PATHS"):
            projects.append({"project": name, "paths": len(resolve("PROJECT_PATHS").get(name) or []),
                             "requirements": counts.get(name, {}),
                             "running_sessions": active.get(name, 0)})
        # 库里出现过但已从清单移除的项目（fail-safe 展示）
        for name in sorted(set(counts) - set(resolve("PROJECT_PATHS"))):
            projects.append({"project": name, "configured": False,
                             "requirements": counts.get(name, {}),
                             "running_sessions": active.get(name, 0)})
        return json.dumps({"projects": projects,
                           "total_requirements": sum(sum(v.values()) for v in counts.values()),
                           "running_sessions": sum(active.values())}, ensure_ascii=False)

    def tool_project_find(self, args):
        self._reject_unknown(args, ("query",))
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 必填")
        q = query.lower()
        with resolve("get_db")() as conn:
            counts = {r["project"]: r["n"] for r in conn.execute(
                "SELECT project, COUNT(*) AS n FROM requirements GROUP BY project")}
        matches = []
        for name in resolve("PROJECT_PATHS"):
            paths = resolve("PROJECT_PATHS").get(name) or []
            haystacks = [name.lower()] + [Path(p).name.lower() for p in paths]
            if any(q in h or h in q for h in haystacks):
                matches.append({"project": name, "requirements": counts.get(name, 0),
                                "paths": [Path(p).name for p in paths]})
        return json.dumps({"query": query, "matches": matches,
                           "all_projects": sorted(resolve("PROJECT_PATHS"))}, ensure_ascii=False)

    def tool_req_query(self, args):
        self._reject_unknown(args, ("project", "status", "keyword", "limit"))
        project = str(args.get("project") or "").strip()
        status = str(args.get("status") or "").strip()
        keyword = str(args.get("keyword") or "").strip().lower()
        limit = self._int_arg(args, "limit", ASSISTANT_RESULT_DEFAULT)
        if status and status not in STATUSES:
            raise ValueError(f"未知状态: {status}（合法值 {'/'.join(STATUSES)}）")
        rows = req_list(project or None, status or None)
        if keyword:
            rows = [r for r in rows if keyword in str(r.get("title") or "").lower()]
        out = [{"id": r["id"], "title": self._clip(r.get("title"), 200),
                "project": r.get("project"), "status": r.get("status"),
                "flow_name": r.get("flow_name"), "phase": r.get("phase"),
                "created_at": r.get("created_at"), "updated_at": r.get("updated_at")}
               for r in rows[:limit]]
        return json.dumps({"count": len(out), "truncated": len(rows) > len(out),
                           "requirements": out}, ensure_ascii=False)

    def tool_req_card(self, args):
        self._reject_unknown(args, ("req_id",))
        try:
            req_id = int(args.get("req_id"))
        except (TypeError, ValueError):
            raise ValueError("req_id 必填且为整数")
        row = self._require(req_id)
        # 流程快照（#419）：当前节点 = nodes_json[node_index]；phase 是历史只读列，进行中的卡常为空
        try:
            raw = row["nodes_json"]
            nodes = json.loads(raw) if raw else []
            if not isinstance(nodes, list):
                nodes = []
        except (ValueError, TypeError, KeyError, IndexError):
            nodes = []
        try:
            idx = row["node_index"]
            node_index = idx if isinstance(idx, int) else None
        except (KeyError, IndexError):
            node_index = None
        node_list = [{"index": i, "role": n.get("role"), "node_type": n.get("node_type"),
                      "name": n.get("name")}
                     for i, n in enumerate(nodes) if isinstance(n, dict)]
        current_node = None
        if node_index is not None and 0 <= node_index < len(node_list):
            cur = node_list[node_index]
            current_node = "收尾" if cur["node_type"] == "terminal" else (
                cur["name"] or role_meta(cur["role"], "display") or cur["role"])
        # 产物清单（只列已存在的主产物文件名与 mtime，不读内容——助手无文件读权限）
        req_dir = resolve("ARTIFACTS_ROOT") / str(row["project"]) / f"req-{req_id}"
        products = []
        for role, fn in resolve("ARTIFACT_FILES").items():
            p = req_dir / fn
            try:
                if p.exists():
                    products.append({"role": role, "filename": fn, "mtime": int(p.stat().st_mtime)})
            except OSError:
                continue
        # 耗时：started_at → completed_at（进行中取当前时刻）
        start_ms, end_ms = _ts_ms(row["started_at"]), _ts_ms(row["completed_at"])
        elapsed_sec = None
        if start_ms:
            end = end_ms or _ts_ms(resolve("now_str")())
            if end:
                elapsed_sec = max(0, int((end - start_ms) / 1000))
        # 字段白名单：**不含 req_reviews**（评审元数据永不进工具结果/回答）
        return json.dumps({
            "id": row["id"], "title": self._clip(row["title"], 200),
            "project": row["project"], "status": row["status"],
            "mode": row["mode"], "phase": row["phase"], "flow_name": row["flow_name"],
            "node_index": node_index, "current_node": current_node, "nodes": node_list,
            "products": products, "elapsed_sec": elapsed_sec,
            "notes": self._clip(row["notes"]), "summary": self._clip(row["summary"]),
            "created_at": row["created_at"], "started_at": row["started_at"],
            "completed_at": row["completed_at"], "updated_at": row["updated_at"]},
            ensure_ascii=False)

    def tool_req_timeline(self, args):
        self._reject_unknown(args, ("req_id", "limit"))
        try:
            req_id = int(args.get("req_id"))
        except (TypeError, ValueError):
            raise ValueError("req_id 必填且为整数")
        self._require(req_id)
        limit = self._int_arg(args, "limit", ASSISTANT_RESULT_DEFAULT)
        with resolve("get_db")() as conn:
            rows = conn.execute(
                "SELECT id, kind, content, ts, session_id FROM dispatcher_events "
                "WHERE req_id = ? ORDER BY id DESC LIMIT ?", (req_id, limit)).fetchall()
        events = [{"id": r["id"], "kind": r["kind"], "content": self._clip(r["content"]),
                   "ts": r["ts"]} for r in reversed(rows)]
        return json.dumps({"req_id": req_id, "events": events}, ensure_ascii=False)

    @staticmethod
    def _card_item(row):
        """#30 单张卡的卡片字段：全部服务端生成（模型不参与），状态取纯中文映射（STATUS_LABEL_CN）"""
        status = row.get("status")
        return {"id": row["id"],
                "title": AssistantMcpServer._clip(row.get("title"), 200),
                "status": status,
                "status_label": STATUS_LABEL_CN.get(status, status),
                "flow": row.get("flow_name") or ASSISTANT_CARD_FLOW_FALLBACK,
                "updated_at": row.get("updated_at")}

    @staticmethod
    def _card_page_len(project, items, total_cards, truncated, remaining):
        """保守估算单页序列化长度：page/pages 用两位数占位（真实页数 ≤ 10），保证真实长度 ≤ 估算值。"""
        return len(json.dumps(_card_page_payload(project, items, 99, 99, total_cards,
                                                 truncated, remaining), ensure_ascii=False))

    def tool_send_requirement_cards(self, args):
        """#30 需求卡清单卡片：服务端取数 → 中文映射 → 进程内分页 → 逐页写 kind='card' 事件。
        分页必须在落库前（事件写入按上限截断，截断的 JSON 非法）；超页上限不静默丢（末页显式提示）。"""
        self._reject_unknown(args, ("project", "status", "keyword"))
        self._cap_or_raise(ASSISTANT_CAP_REQ_CARDS, "需求卡清单卡片")
        project = str(args.get("project") or "").strip()
        if not project:
            raise ValueError("project 必填")
        if project not in resolve("PROJECT_PATHS"):
            raise ValueError(f"未知项目: {project}（必须在 projects.json 清单内；可先用 project_find 定位）")
        status = str(args.get("status") or "").strip()
        codes = [s.strip() for s in status.split(",") if s.strip()]
        bad = [s for s in codes if s not in STATUSES]
        if bad:
            raise ValueError(f"未知状态: {'/'.join(bad)}（合法值 {'/'.join(STATUSES)}）")
        keyword = str(args.get("keyword") or "").strip().lower()
        rows = req_list(project, None)   # 全量取数（不受 ASSISTANT_RESULT_MAX 夹取，截断只由页上限承担）
        if codes:
            rows = [r for r in rows if r.get("status") in codes]
        if keyword:
            rows = [r for r in rows if keyword in str(r.get("title") or "").lower()]
        items = [self._card_item(r) for r in rows]
        total = len(items)
        pages_items, cur = [], []
        for it in items:
            cand = cur + [it]
            if cur and (len(cand) > ASSISTANT_CARD_MAX_ITEMS
                        or self._card_page_len(project, cand, total, False, 0)
                        > ASSISTANT_CARD_PAGE_MAX):
                pages_items.append(cur)
                cur = [it]
            else:
                cur = cand
        if cur:
            pages_items.append(cur)
        truncated = len(pages_items) > ASSISTANT_CARD_MAX_PAGES
        if truncated:
            pages_items = pages_items[:ASSISTANT_CARD_MAX_PAGES]
        pages_items = pages_items or [[]]   # 空清单也发一页空页（不静默成功）
        sent = sum(len(p) for p in pages_items)
        remaining = total - sent
        pages = len(pages_items)
        for idx, page_items in enumerate(pages_items, 1):
            payload = _card_page_payload(project, page_items, idx, pages, total,
                                         truncated and idx == pages, remaining)
            text = json.dumps(payload, ensure_ascii=False)
            if len(text) > ASSISTANT_CARD_PAGE_MAX:   # fail-closed：绝不写截断 JSON
                raise ValueError(f"卡片第 {idx} 页超出长度上限（{len(text)} > "
                                 f"{ASSISTANT_CARD_PAGE_MAX}），请缩小范围")
            _assistant_add_card_event(self.assistant_session_id, text)
        if not total:
            note = "该项目当前没有符合条件的需求卡"
        elif remaining:
            note = (f"已发出前 {sent} 张（{pages} 页），还有 {remaining} 张未展示；"
                    "可按状态或关键词缩小范围后重发")
        else:
            note = f"已发出 {sent} 张（{pages} 页）"
        return json.dumps({"project": project, "total_items": total, "sent_pages": pages,
                           "truncated": bool(remaining), "remaining": max(0, remaining),
                           "note": note}, ensure_ascii=False)

    def tool_sessions_status(self, args):
        self._reject_unknown(args, ())
        try:
            hb = json.loads(resolve("HEARTBEAT_FILE").read_text())
            last_poll = hb.get("last_poll_at")
            age = max(0, int(time.time() - last_poll)) if isinstance(last_poll, (int, float)) else None
        except Exception:
            age = None
        health = {"heartbeat_age_seconds": age,
                  "stale": (age is None or age > resolve("HEALTH_STALE_SEC"))}
        try:
            health["feishu"] = resolve("_feishu_runtime_status")().get("state")
        except Exception:
            health["feishu"] = None
        with resolve("get_db")() as conn:
            rows = conn.execute(
                "SELECT a.id, a.req_id, a.role, a.session_id, a.model, a.status, a.turn_step, "
                "a.updated_at, r.title AS req_title, r.project AS project "
                "FROM agent_sessions a JOIN requirements r ON r.id = a.req_id "
                "WHERE a.status = 'running' ORDER BY a.id DESC LIMIT ?",
                (ASSISTANT_RESULT_MAX,)).fetchall()
            pending = self._pending_rows(conn)
        sessions = [{"agent_session_id": r["id"], "req_id": r["req_id"],
                     "req_title": self._clip(r["req_title"], 200), "project": r["project"],
                     "role": r["role"], "status": r["status"], "turn_step": r["turn_step"],
                     "model": r["model"], "updated_at": r["updated_at"]}
                    for r in rows]
        return json.dumps({"health": health, "running_sessions": sessions,
                           "pending_count": len(pending)}, ensure_ascii=False)

    def _pending_rows(self, conn):
        """待确认项（数据源 = dispatcher_state 三 pending 字典 + 需求状态），字段白名单。"""
        st = resolve("_read_pending_confirm_state")()
        statuses = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id, status FROM requirements").fetchall()}
        out = []
        for dict_key, fixed_kind in (("pending_confirm", None), ("pending_route", "route"),
                                     ("pending_review", "review")):
            for rid, raw in (st.get(dict_key) or {}).items():
                try:
                    req_id = int(rid)
                except (TypeError, ValueError):
                    continue
                if statuses.get(req_id) in ("done", "dropped"):
                    continue  # #25：终态卡确认项不进助手清单（对账仍走 dispatcher 清账）
                entry = raw if isinstance(raw, dict) else {}
                kind = (entry.get("kind") or "confirm") if dict_key == "pending_confirm" else fixed_kind
                question = self._clip(entry.get("question") or "", 300)
                if not question:
                    question = ("analysis 建议通道待确认" if fixed_kind == "route"
                                else "评审完成，待人工确认" if fixed_kind == "review" else "")
                out.append({"req_id": req_id, "kind": kind, "question": question,
                            "role": entry.get("role") or entry.get("phase"),
                            "phase": entry.get("phase"),
                            "status": statuses.get(req_id),
                            "over_limit": bool(entry.get("over_limit"))})
        return out

    def tool_pending_confirmations(self, args):
        self._reject_unknown(args, ())
        with resolve("get_db")() as conn:
            rows = self._pending_rows(conn)
        return json.dumps({"count": len(rows), "pending": rows[:ASSISTANT_RESULT_MAX]},
                          ensure_ascii=False)

    # ---- #26 只读代码分析工具（开关默认关；读范围 = 项目清单全部项目） ----
    def _attachment_root(self, path_str, project):
        """#44 本会话附件目录例外：当且仅当 path 为绝对路径、未给 project、且 resolve 后落在
        `MATERIALS_ROOT/_assistant/asst-<本会话>` 内时返回该附件根；否则 None（走原判定）。
        附件根由可信 self.assistant_session_id 构造——跨会话路径 / 符号链接逃逸 resolve 后不在根内。"""
        if project or not isinstance(path_str, str) or not path_str.strip():
            return None
        if not Path(path_str).is_absolute():
            return None
        try:
            root = _assistant_materials_dir(self.assistant_session_id).resolve()
            target = Path(path_str).resolve()
        except OSError:
            return None
        return root if target.is_relative_to(root) else None

    def tool_file_read(self, args):
        """读项目文件（带行号）。path 绝对或相对 project 根；校验顺序 = 开关 → 参数 → 路径 → 敏感 → 限量。
        #44：本会话附件目录内的绝对路径例外——跳过代码只读开关，且可读 roots 显式换为 [附件根]
        （MATERIALS_ROOT 不在项目根清单内，不换根必然判越权）。"""
        attach_root = self._attachment_root(args.get("path"), args.get("project"))
        if attach_root is None:
            self._cap_or_raise(ASSISTANT_CAP_FILE_TOOLS, "代码只读分析")
        self._reject_unknown(args, ("path", "project", "offset", "max_lines", "meta"))
        path_str = args.get("path")
        if not isinstance(path_str, str) or not path_str.strip():
            raise ValueError("path 必须为非空字符串")
        project = args.get("project")
        if attach_root is not None:
            roots = [attach_root]
            p = resolve_path_safe(path_str, Path("/"), roots)
        else:
            roots = self._assistant_roots()
            if project:
                p = self._resolve_in_project(path_str, project, roots)
            elif Path(path_str).is_absolute():
                p = resolve_path_safe(path_str, Path("/"), roots)
            else:
                raise ValueError("相对路径需指定 project（或改用绝对路径）")
        if p is None:
            raise ValueError(f"越权/非法路径: {path_str!r}")
        if not p.is_file():
            raise ValueError(f"不是文件: {p}")
        if sensitive_path_blocked(p):
            raise ValueError(f"敏感文件禁止读取（黑名单）: {p.name}")
        st = p.stat()
        meta = args.get("meta", False)
        if not isinstance(meta, bool):
            raise ValueError(f"meta 必须为布尔: {meta!r}")
        if meta:
            raw = p.read_bytes()[:4096]
            ftype = "text" if b"\x00" not in raw else "binary"
            try:
                nlines = len(p.read_text(errors="replace").splitlines())
            except OSError:
                nlines = -1
            return (f"{p}\ttype={ftype}\tsize={st.st_size}\t"
                    f"mtime={datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}"
                    f"\tlines={nlines}")
        if st.st_size > FILE_MAX_BYTES:
            raise ValueError(f"文件超过 {FILE_MAX_BYTES} 字节上限: {st.st_size}")
        offset = args.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError(f"offset 必须为非负整数: {offset!r}")
        # 行数类参数单独校验（不得走 _int_arg 的默认 hi=ASSISTANT_RESULT_MAX=50，会夹掉合法 2000）
        max_lines = self._strict_int(args, "max_lines", FILE_MAX_LINES, 1, FILE_MAX_LINES_LIMIT)
        lines = ro_read_lines(p, offset, max_lines + 1)     # C6：多读一行判断是否未读完
        has_more = len(lines) > max_lines
        lines = lines[:max_lines]
        keep = ro_fit_lines(lines, TOOL_OUTPUT_MAX_CHARS - 600)
        if keep < len(lines):
            lines, has_more = lines[:keep], True
        body = "\n".join(f"{offset + i}\t{ln}" for i, ln in enumerate(lines, 1))
        tail = ro_read_continuation(offset, len(lines), has_more)
        return f"文件: {p} (offset={offset}, lines={len(lines)})\n" + body + (f"\n{tail}" if tail else "")

    def tool_file_find(self, args):
        """按 glob 模式列文件（跨项目时共用一份全局扫描预算 + 墙钟时限）。"""
        self._cap_or_raise(ASSISTANT_CAP_FILE_TOOLS, "代码只读分析")
        self._reject_unknown(args, ("pattern", "project", "limit"))
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError("pattern 必须为非空字符串")
        pattern = pattern.strip()
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("pattern 必须为项目内相对模式（不得绝对路径或 ..）")
        limit = self._strict_int(args, "limit", ASSISTANT_FIND_DEFAULT_LIMIT, 1, DIR_MAX_ENTRIES)
        roots = self._assistant_roots()
        targets = self._scan_targets(args, roots)
        deadline = time.monotonic() + resolve("ASSISTANT_SCAN_DEADLINE_SEC")
        budget, scanned, out, truncated = resolve("ASSISTANT_SCAN_MAX_FILES_TOTAL"), 0, [], False
        for label, base in targets:
            if len(out) >= limit or budget <= 0 or time.monotonic() > deadline:
                truncated = True
                break
            paths, sc, tr = ro_glob_paths(base, pattern, limit - len(out), budget, deadline)
            scanned += sc
            budget -= sc
            out += [f"{label}: {rel}" for rel in paths]
            if tr:
                truncated = True
                break
        if not out:
            if truncated:
                return f"模式: {pattern}（扫描未完成：已达时限或上限，请缩小范围）"
            return f"模式: {pattern}（无匹配）"
        head = f"模式: {pattern}（{len(out)} 条，扫描 {scanned} 个文件"
        head += "，已达上限截断，请缩小范围）" if truncated else "）"
        return head + "\n" + "\n".join(out)

    def tool_code_search(self, args):
        """按关键词检索代码内容（跨项目时共用一份全局扫描预算 + 墙钟时限）。"""
        self._cap_or_raise(ASSISTANT_CAP_FILE_TOOLS, "代码只读分析")
        self._reject_unknown(args, ("keyword", "project", "path", "max_matches"))
        keyword = args.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("keyword 必须为非空字符串")
        kw = keyword.strip()
        max_matches = self._strict_int(args, "max_matches", SEARCH_MAX_MATCHES, 1, SEARCH_MAX_MATCHES)
        roots = self._assistant_roots()
        targets = self._scan_targets(args, roots)
        deadline = time.monotonic() + resolve("ASSISTANT_SCAN_DEADLINE_SEC")
        budget, scanned, hits, out, truncated = resolve("ASSISTANT_SCAN_MAX_FILES_TOTAL"), 0, 0, [], False
        for label, target in targets:
            if hits >= max_matches or budget <= 0 or time.monotonic() > deadline:
                truncated = hits < max_matches
                break
            lines, sc, h, tr = resolve("ro_search_hits")(target, kw, max_matches - hits, budget, deadline)
            scanned += sc
            budget -= sc
            hits += h
            out += [f"{label}/{ln}" for ln in lines]
            if tr or hits >= max_matches:
                truncated = tr
                break
        if not out:
            if truncated:
                return f"关键词: {kw}（扫描未完成：已达时限或上限，请缩小范围）"
            return f"关键词: {kw}（无匹配）"
        head = f"关键词: {kw}（命中 {hits} 处，扫描 {scanned} 个文件"
        head += "，已达上限截断，请缩小范围）" if truncated else "）"
        return head + "\n" + "\n".join(out)

    # ---- #54 项目代码同步（开关默认关；固定 argv、无 push、冲突保留现场） ----
    def _project_git(self):
        self._cap_or_raise(ASSISTANT_CAP_GIT_OPS, "代码拉取与提交")
        return resolve("ProjectGit")(deadline=self.turn_deadline,
                                     cancel_event=self.cancel_event)

    @staticmethod
    def _git_failure(exc):
        name = type(exc).__name__
        if name == "GitCancelled":
            return {"status": "failed", "error": "操作已取消", "timed_out": False,
                    "cancelled": True}
        if name == "GitTimedOut":
            return {"status": "failed", "error": "本轮剩余时间不足，请重发",
                    "timed_out": True, "cancelled": False}
        return None

    def _git_result(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 取消/超时转结构化结果，其余照常上抛
            failure = self._git_failure(exc)
            if failure is None:
                raise
            return failure

    def _prune_commit_tokens(self):
        now = time.monotonic()
        for token, entry in list(self._commit_tokens.items()):
            if entry.get("expires_at", 0) <= now:
                self._commit_tokens.pop(token, None)

    def tool_project_git_status(self, args):
        self._reject_unknown(args, ("project", "repo"))
        git = self._project_git()
        out = self._git_result(git.status, args.get("project"), args.get("repo"))
        return json.dumps(out, ensure_ascii=False)

    def tool_project_git_pull(self, args):
        self._reject_unknown(args, ("project", "repo", "branch", "remote", "action", "side"))
        action = str(args.get("action") or "pull").strip() or "pull"
        if action not in ("pull", "resolve", "abort"):
            raise ValueError("action 仅支持 pull（缺省）/ resolve / abort")
        git = self._project_git()
        if action == "resolve":
            out = self._git_result(git.resolve_conflicts, args.get("project"),
                                   args.get("repo"), args.get("side"))
        elif action == "abort":
            out = self._git_result(git.abort_merge, args.get("project"), args.get("repo"))
        else:
            out = self._git_result(git.pull, args.get("project"), args.get("repo"),
                                   args.get("branch"), args.get("remote"))
        return json.dumps(out, ensure_ascii=False)

    def tool_project_git_commit(self, args):
        self._reject_unknown(args, ("project", "repo", "message", "confirm_token",
                                    "include_untracked", "allow_sensitive",
                                    "allow_secret_findings"))
        git = self._project_git()
        self._prune_commit_tokens()
        token = args.get("confirm_token")
        include_untracked = args.get("include_untracked")
        allow_sensitive = args.get("allow_sensitive") or []
        allow_secret = args.get("allow_secret_findings", False)
        message = args.get("message")
        if token in (None, ""):
            out = self._git_result(git.prepare_commit, args.get("project"), args.get("repo"),
                                   message, include_untracked, allow_sensitive, allow_secret)
            if isinstance(out, dict) and out.get("status") == "preview":
                new_token = secrets.token_hex(16)
                out["confirm_token"] = new_token
                out["issued_turn_id"] = self.turn_id
                out["expires_in"] = self.GIT_COMMIT_TOKEN_TTL
                self._commit_tokens[new_token] = {
                    "assistant_session_id": self.assistant_session_id,
                    "project": args.get("project"), "repo": out.get("repo"),
                    "branch": out.get("branch"), "message": message,
                    "include_untracked": include_untracked,
                    "allow_sensitive": allow_sensitive,
                    "allow_secret_findings": bool(allow_secret),
                    "fingerprint": out.get("fingerprint"),
                    "head": (out.get("head") or {}).get("sha"),
                    "issued_turn_id": self.turn_id,
                    "expires_at": time.monotonic() + self.GIT_COMMIT_TOKEN_TTL,
                }
            return json.dumps(out, ensure_ascii=False)
        if not isinstance(token, str) or token not in self._commit_tokens:
            raise ValueError("confirm_token 无效或已使用；请重新预览，并把摘要发给用户确认")
        entry = self._commit_tokens[token]
        if entry.get("assistant_session_id") != self.assistant_session_id:
            raise ValueError("confirm_token 不属于当前助手会话")
        cur_turn, issued_turn = self.turn_id, entry.get("issued_turn_id")
        if (not isinstance(cur_turn, int) or not isinstance(issued_turn, int)
                or cur_turn <= issued_turn):
            raise ValueError("请把摘要发给用户，等用户确认后的下一轮再提交（同一轮不能连续预览→提交）")
        if message != entry.get("message"):
            raise ValueError("message 与预览时不一致；请重新预览并把新摘要发给用户确认")
        if entry.get("project") != args.get("project"):
            raise ValueError("project 与预览时不一致；请重新预览")
        current_repo = git.resolve_repo(args.get("project"),
                                        args.get("repo") or entry.get("repo"))
        if Path(str(current_repo)).resolve() != Path(str(entry.get("repo"))).resolve():
            raise ValueError("repo 与预览时不一致；请重新预览")
        current_branch = git.current_branch(args.get("project"), entry.get("repo"))
        if current_branch != entry.get("branch"):
            raise ValueError(f"当前分支已从预览时的 {entry.get('branch')} 变为 "
                             f"{current_branch or 'detached HEAD'}，请重新预览并确认目标分支")
        self._commit_tokens.pop(token, None)   # 单次使用
        out = self._git_result(git.apply_commit, entry.get("project"), entry.get("repo"),
                               entry.get("message"), entry.get("include_untracked"),
                               entry.get("allow_sensitive"), entry.get("allow_secret_findings"),
                               entry.get("fingerprint"), entry.get("head"),
                               expected_branch=entry.get("branch"))
        return json.dumps(out, ensure_ascii=False)

    # ---- 写工具 ----
    def tool_create_requirement(self, args):
        self._reject_unknown(args, ("project", "title", "notes", "status", "source_url"))
        project = str(args.get("project") or "").strip()
        title = str(args.get("title") or "").strip()
        notes = str(args.get("notes") or "").strip()
        status = str(args.get("status") or "later").strip()
        source_url = str(args.get("source_url") or "").strip()
        if not title:
            raise ValueError("title 必填")
        if len(title) > 200:
            raise ValueError("title 超过 200 字符上限")
        if project not in resolve("PROJECT_PATHS"):
            raise ValueError(f"未知项目: {project}（必须在 projects.json 清单内）")
        if status not in ("later", "open"):
            raise ValueError("status 仅支持 later（暂缓，默认）/ open（待开发，仅用户明确要求开发时用）")
        if source_url and not (source_url.startswith("http://") or source_url.startswith("https://")):
            raise ValueError("source_url 必须是 http/https 链接")
        if len(source_url) > 500:
            raise ValueError("source_url 超长")
        if source_url:
            suffix = f"\n来源：{source_url}"
            notes = (notes[:max(0, 2000 - len(suffix))] + suffix) if notes else f"来源：{source_url}"
        if len(notes) > 2000:
            raise ValueError("notes 超过 2000 字符上限")
        # 幂等键 = 本助手会话本轮（一条飞书消息 = 一轮）：同轮重试不重复落卡，
        # 同一 chat 的不同轮次可建多张卡（一 chat 多卡）。
        idem = f"asst-{self.assistant_session_id}-turn-{self.turn_id}"
        with resolve("get_db")() as conn:
            same = conn.execute(
                "SELECT id, title, project, status FROM requirements WHERE source_session_id = ? "
                "ORDER BY id DESC LIMIT 1", (idem,)).fetchone()
            row = conn.execute("SELECT model FROM feishu_assistant_sessions WHERE id = ?",
                               (self.assistant_session_id,)).fetchone()
        if same is not None:
            return json.dumps({"id": same["id"], "title": same["title"], "project": same["project"],
                               "status": same["status"], "created": False,
                               "message": "本轮已建卡，返回已有需求"}, ensure_ascii=False)
        model = (row["model"] if row else None)
        created = req_add(title, project, notes, status=status,
                          source="board-feishu-assistant", model=model, source_session_id=idem)
        _assistant_add_event(self.assistant_session_id, "system",
                             f"已建卡 #{created['id']}「{created['title']}」（{project}，{status}）")
        return json.dumps({"id": created["id"], "title": created["title"], "project": project,
                           "status": status, "created": True}, ensure_ascii=False)

    def tool_set_requirement_status(self, args):
        """已建卡状态变更（#26 首期唯一转换：暂缓 later → 待开发 open）。
        成功路径统一走 req_update（状态白名单 + updated_at 刷新 + started_at/completed_at 归零），
        再 _log_status_change 留痕；已是目标状态 → 幂等返回且不写审计、不动 updated_at。"""
        self._cap_or_raise(ASSISTANT_CAP_STATUS_WRITE, "状态变更")
        self._reject_unknown(args, ("req_id", "status"))
        req_id = args.get("req_id")
        if isinstance(req_id, bool) or not isinstance(req_id, int):
            raise ValueError(f"req_id 必须为整数: {req_id!r}")
        target = args.get("status")
        if target not in ASSISTANT_STATUS_WRITE_TARGETS:
            raise ValueError("仅支持把卡改成「待开发(open)」；其它目标状态请到网页操作")
        row = self._require(req_id)
        old = row["status"]
        if old == target:
            return json.dumps({"req_id": req_id, "status": target, "changed": False,
                               "message": "该卡已是待开发，未做变更"}, ensure_ascii=False)
        if old not in ASSISTANT_STATUS_WRITE_SOURCES:
            raise ValueError(f"仅支持 暂缓(later) → 待开发(open)：#{req_id} 当前为"
                             f"{_status_label_cn(old)}，请到网页操作")
        _new_row, err = resolve("req_update")(req_id, status=target)
        if err:
            raise ValueError(f"状态更新失败: {err}")
        detail = self._clip(
            f"agent:feishu-assistant（chat {self.feishu_chat_id}，助手会话 #{self.assistant_session_id}"
            f" 第 {self.turn_id} 轮）")
        _log_status_change(req_id, old, target, "agent", detail)
        _assistant_add_event(self.assistant_session_id, "system",
                             f"已改状态 #{req_id}：{_status_label_cn(old)} → {_status_label_cn(target)}")
        return json.dumps({
            "req_id": req_id, "old_status": old, "status": target, "changed": True,
            "message": "已从暂缓改为待开发",
            "warning": "该卡约 1 分钟后会被调度器自动纳入开发流程（起 worktree 并跑 agent）；"
                       "如需暂缓请到网页改回。",
        }, ensure_ascii=False)


def _assistant_mcp_handler(server):
    """绑定 AssistantMcpServer 实例的 HTTP handler（Streamable Transport，仿 _create_mcp_handler）。"""
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
                        "serverInfo": {"name": ASSISTANT_TOOL_SERVER, "version": "1.0"}}}
                elif method == "ping":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {}}
                elif method == "tools/list":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {"tools": server._tool_defs()}}
                elif method == "tools/call":
                    p = msg.get("params") or {}
                    out = server.call(p.get("name"), p.get("arguments") or {})
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


def _assistant_cap_enabled(field):
    """#26 助手能力开关：现读 feishu.json，字段非 True（缺字段 / 文件缺失 / 损坏）→ False（fail-closed）。
    **每次工具调用都读、不缓存**——网页保存开关后长驻助手会话即时生效。"""
    raw = resolve("_feishu_json_raw")()
    return isinstance(raw, dict) and raw.get(field) is True
