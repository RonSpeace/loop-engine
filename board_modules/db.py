"""数据库连接与迁移（#32 拆分 Phase 1）。

DB_PATH 经 runtime.resolve 晚绑定（测试会 patch board.DB_PATH 指向临时库）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from storage_time import format_storage_time
from contextlib import contextmanager

from board_modules.runtime import resolve


@contextmanager
def get_db():
    conn = sqlite3.connect(str(resolve("DB_PATH")))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """初始化数据库（只增不删，改结构用 ALTER TABLE 迁移）"""
    with resolve("get_db")() as conn:   # 晚绑定：patch board.get_db 对 init_db 内部同样生效
        conn.execute("""
            CREATE TABLE IF NOT EXISTS requirements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                project TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                notes TEXT,
                summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        # 迁移：补充 started_at / summary 列（已存在则跳过）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(requirements)")}
        if "started_at" not in cols:
            conn.execute("ALTER TABLE requirements ADD COLUMN started_at TIMESTAMP")
        if "summary" not in cols:
            conn.execute("ALTER TABLE requirements ADD COLUMN summary TEXT")
        if "group_id" in cols:
            # 会话组概念已移除（需求全部平级独立、追加需求功能删除），历史 group_id 一并清掉
            conn.execute("ALTER TABLE requirements DROP COLUMN group_id")
        if "mode" not in cols:
            # 通道：auto(全自动) / semi(半自动)；analysis 产出后由通道确认写入，建卡时为空
            conn.execute("ALTER TABLE requirements ADD COLUMN mode TEXT")
        if "phase" not in cols:
            # 半自动当前阶段：proposal / tech / tech_review / dev / code_review / accept；auto 通道为 NULL
            conn.execute("ALTER TABLE requirements ADD COLUMN phase TEXT")
        if "flow" not in cols:
            # #376：半自动流程（route 确认时写入，doing 后锁定）：standard / lean / heavy；NULL=标准，兼容存量卡
            conn.execute("ALTER TABLE requirements ADD COLUMN flow TEXT")
        if "flow_name" not in cols:
            # #419：进入流程时的流程名快照（显示用，改名/删流程不影响历史卡显示；调度不读本列）
            conn.execute("ALTER TABLE requirements ADD COLUMN flow_name TEXT")
        if "nodes_json" not in cols:
            # #419：节点序列快照（[{role,node_type,artifact}...]，terminal 节点仅 node_type）；按快照推进
            conn.execute("ALTER TABLE requirements ADD COLUMN nodes_json TEXT")
        if "node_index" not in cols:
            # #419：快照内当前节点下标（0 基）；NULL = 未入流程（走前置槽位）。mode/phase 为历史只读列
            conn.execute("ALTER TABLE requirements ADD COLUMN node_index INTEGER")
        # #303 建卡来源元数据：记录这张卡由谁、用什么模型建的（harness 无关，自由字符串透传）
        # source=建卡来源（dsh/codex/kimi-code/board-chat…）；model=建卡模型；reasoning_effort=推理强度
        # source_session_id=来源会话 id（如 DSH_SESSION_ID）；全部可空，历史卡不迁移（NULL）
        if "source" not in cols:
            conn.execute("ALTER TABLE requirements ADD COLUMN source TEXT")
        if "model" not in cols:
            conn.execute("ALTER TABLE requirements ADD COLUMN model TEXT")
        if "reasoning_effort" not in cols:
            conn.execute("ALTER TABLE requirements ADD COLUMN reasoning_effort TEXT")
        if "source_session_id" not in cols:
            conn.execute("ALTER TABLE requirements ADD COLUMN source_session_id TEXT")
        # 调度器 ACP 执行过程事件：doing 卡片"执行过程"面板的数据来源（只增不删）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_events_req ON agent_events(req_id, id)")
        # 需求助手（原「需求对话」#302 详情页常驻聊天，#326 起中间栏置顶入口+右栏独立视图）：需求级独立会话，不走调度器
        # status: created(占位未开始) / idle(可对话) / running(本轮执行中) /
        #         interrupted(中断可继续) / failed(建立失败) / closed(已放弃，可新建)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS req_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                session_id TEXT,
                model TEXT,
                error TEXT,
                interrupt_source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_req_chats_req ON req_chats(req_id, id)")
        # #320：需求对话会话的推理深度原始值（configOptions 的 thought_level/thinking），
        # 前端状态行下方 meta 行展示；存量行保持 NULL（前端判空不显示），仿 #118 chat_sessions.thinking 做法
        reqchat_cols = {r[1] for r in conn.execute("PRAGMA table_info(req_chats)")}
        if "thinking" not in reqchat_cols:
            conn.execute("ALTER TABLE req_chats ADD COLUMN thinking TEXT")
        # 需求助手建卡：本对话新建的需求卡 id（幂等防重复建卡，仿 chat_sessions.req_id）
        if "created_req_id" not in reqchat_cols:
            conn.execute("ALTER TABLE req_chats ADD COLUMN created_req_id INTEGER")
        # #382：需求助手放开「每对话至多 1 张」→ 同 title 幂等、可建多张；
        # 多值清单存 created_req_ids（逗号分隔），存量行从 created_req_id 迁入（旧列保留不写）
        if "created_req_ids" not in reqchat_cols:
            conn.execute("ALTER TABLE req_chats ADD COLUMN created_req_ids TEXT")
            conn.execute("UPDATE req_chats SET created_req_ids = CAST(created_req_id AS TEXT) "
                         "WHERE created_req_id IS NOT NULL")
        # #383：需求助手头部会话信息行展示「used xx%」（上下文占用）——每轮收尾采集落库，
        # 对齐建卡对话 chat_sessions.context_pct；存量行 NULL，前端判空不显示
        if "context_pct" not in reqchat_cols:
            conn.execute("ALTER TABLE req_chats ADD COLUMN context_pct TEXT")
        # 需求对话事件流：独立于 agent_events（调度器体系），只增不删
        conn.execute("""
            CREATE TABLE IF NOT EXISTS req_chat_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_req_chat_events ON req_chat_events(req_chat_id, id)")
        # 会话级执行过程：事件按 ACP 会话打标（历史事件无标记，session_id 为 NULL）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_events)")}
        if "session_id" not in cols:
            conn.execute("ALTER TABLE agent_events ADD COLUMN session_id TEXT")
        # 调度器动作时间线：调度器自身关键决策（拉起会话/阶段流转/合并/重试等），只增不删
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatcher_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatcher_events_req ON dispatcher_events(req_id, id)")
        # 调度历史 spawn 记录可点击 session id（#120）：历史行保持 NULL 不回填
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatcher_events)")}
        if "session_id" not in cols:
            conn.execute("ALTER TABLE dispatcher_events ADD COLUMN session_id TEXT")
        # 调度器 ACP 会话运行记录（报表基础数据源，只增不删）：每次会话收尾写一行，
        # 含起止时间/状态/工具数/token 数/上下文百分比；仅新数据，历史不迁移。
        # #42 缓存命中四列：cached/input=供应商返回 token 求和（NULL=未取得）、
        # cache_state=hit/unknown/na（NULL=存量行，接口归 unknown）、usage_raw=原始证据片段（≤2048）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                role TEXT,
                session_id TEXT,
                started_at REAL,
                ended_at REAL,
                status TEXT,
                tool_count INTEGER,
                context_pct REAL,
                context_tokens INTEGER,
                max_tokens INTEGER,
                cached_tokens INTEGER,
                input_tokens INTEGER,
                cache_state TEXT,
                usage_raw TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_req ON agent_runs(req_id, id)")
        # #42：存量库幂等加列（只 ALTER ADD COLUMN，不删表重建；存量行保持 NULL，不回填 0）
        ar_cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_runs)")}
        for col, decl in (("cached_tokens", "INTEGER"), ("input_tokens", "INTEGER"),
                          ("cache_state", "TEXT"), ("usage_raw", "TEXT")):
            if col not in ar_cols:
                conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {col} {decl}")
        # 调度器 ACP 会话注册表（#273 人工接管）：每条 kimi 会话一行（含空会话/毒会话），
        # 供中间栏会话列表与人工继续/新建；poisoned=1 表示自动重试已耗尽，仅人工可继续
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                session_id TEXT,
                model TEXT,
                thinking TEXT,
                harness TEXT,
                context_pct TEXT,
                branch TEXT,
                workdir TEXT,
                poisoned INTEGER DEFAULT 0,
                status TEXT DEFAULT 'created',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_req ON agent_sessions(req_id, id)")
        # #421 会话活性心跳：调度器在「状态切换时」写（非定时写库），前端据此算判活/判死。
        # 时间戳为 epoch 秒（服务端 time.time()，与 created_at 的北京本地字符串刻意不同——
        # 避免浏览器时区解析歧义）；存量行保持 NULL（前端 fail-safe：不计时、不判卡死）。
        as_cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_sessions)")}
        for col, decl in (("turn_step", "TEXT"), ("turn_step_detail", "TEXT"),
                          ("last_llm_request_at", "REAL"), ("last_llm_response_at", "REAL"),
                          ("last_thought_at", "REAL"), ("last_tool_started_at", "REAL"),
                          # #431 来源两列：行由谁创建（origin 为封闭枚举，origin_detail 记
                          # X-Actor 原文 + UA 供定位）；存量行留空不回填
                          ("origin", "TEXT"), ("origin_detail", "TEXT"),
                          # #11 模型留痕两列：model_requested=spawn 时解析出的实际请求模型，
                          # model_source=来源（project/role/env/global）；实际值复用 model 列
                          ("model_requested", "TEXT"), ("model_source", "TEXT"),
                          # #13 兜底可见两列：model_configured=回落前的用户配置值（NULL=存量/未解析），
                          # model_reason=不可用/未落定原因摘要（NULL=无异常）
                          ("model_configured", "TEXT"), ("model_reason", "TEXT"),
                          # #29 思考档位留痕三列：thinking_requested=per-model 选档后实际发出的档位
                          # （NULL=未请求/存量）；thinking_configured=四层链配置值（回落前，
                          # NULL=存量/未解析）；thinking_reason=回落/未落定原因摘要（NULL=无异常）
                          ("thinking_requested", "TEXT"), ("thinking_configured", "TEXT"),
                          ("thinking_reason", "TEXT")):
            if col not in as_cols:
                conn.execute(f"ALTER TABLE agent_sessions ADD COLUMN {col} {decl}")
        # 人工续跑命令队列（#273）：看板写 pending，调度器轮询消费（claim=running -> done/failed）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                agent_session_id INTEGER,
                role TEXT,
                text TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_commands_status ON manual_commands(status, id)")
        # #400：网页确认动作决策表——用户点击确认按钮写入（action），调度器
        # reconcile_confirmations 对账消费后清除；无行 = 未点击/已消费（按钮可点态）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS confirm_decisions (
                req_id INTEGER PRIMARY KEY,
                kind TEXT,
                action TEXT,
                actor TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # #400 X-Actor 审计列（兼容已建旧表：列缺失则迁移）；POST 时记录 manual:网页确认-<action>
        confirm_dec_cols = {r[1] for r in conn.execute("PRAGMA table_info(confirm_decisions)")}
        if "actor" not in confirm_dec_cols:
            conn.execute("ALTER TABLE confirm_decisions ADD COLUMN actor TEXT")
        # #415 网页「转开发」反馈意见列（兼容迁移同 actor 先例）：POST 时记录用户填写的 reason 文本
        if "reason" not in confirm_dec_cols:
            conn.execute("ALTER TABLE confirm_decisions ADD COLUMN reason TEXT")
        # 建卡对话（#107）：新增需求页改为 agent 会话框（kimi acp 聊需求，agent 直接建卡）
        # chat_sessions: 一个会话绑定一个项目；status: idle(就绪可聊)/running(一轮进行中)/
        #   interrupted(进程中断可续跑)/suspended(挂起可续跑，兼容旧切换流程/外部调用)/
        #   created(已建卡，终态)/failed(建会话失败，终态)/closed(放弃，终态)
        # session_id = kimi ACP 会话 id（续跑用 session/resume）；req_id = 已建卡的需求 id（防重复建卡）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle',
                session_id TEXT,
                draft_title TEXT,
                draft_notes TEXT,
                req_id INTEGER,
                model TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
        chat_source_added = False
        if "interrupt_source" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN interrupt_source TEXT")
            chat_source_added = True
        # #118：采集建卡会话的推理深度原始值（configOptions 的 thought_level/thinking），暂不展示
        if "thinking" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN thinking TEXT")
        # 建卡会话结束元数据：kimi code 版本 / 上下文用量 / 分支 / 是否已收尾（历史会话框展示用）
        if "harness" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN harness TEXT")
        if "context_pct" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN context_pct TEXT")
        if "context_tokens" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN context_tokens INTEGER")
        if "max_tokens" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN max_tokens INTEGER")
        if "branch" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN branch TEXT")
        # #285：建卡会话执行目录（收尾时随 branch 一起落库，历史会话框第二行展示；存量行为 NULL 走前端判空降级）
        if "workdir" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN workdir TEXT")
        if "finalized" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN finalized INTEGER DEFAULT 0")
        # #22 建卡会话双通道隔离：channel = web（网页建卡页，默认）/ feishu（飞书多轮建卡）。
        # 只增不删（存量行自动 'web'）；GET /api/chat 缺省只返回 web（fail-closed）。
        if "channel" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN channel TEXT NOT NULL DEFAULT 'web'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_channel ON chat_sessions(channel, id)")
        # #24 飞书看板助手：旧建卡表**只增列**（不重建、不改 project NOT NULL 约束，存量行自动为
        # chat_scope='chat' / feishu_chat_id=NULL）；助手会话不写本表（方案 B，见下两表）。
        if "chat_scope" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN chat_scope TEXT NOT NULL DEFAULT 'chat'")
        if "feishu_chat_id" not in chat_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN feishu_chat_id TEXT")
        # 建卡对话事件流（消息/工具摘要/系统/用户输入），独立于 agent_events（后者 req_id 为 INTEGER NOT NULL）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_events_chat ON chat_events(chat_id, id)")
        # #24 飞书看板助手会话（方案 B：独立业务表，不写 chat_sessions/chat_events）：
        # 一个 feishu_chat_id 同时最多一条活跃助手会话；无 project 字段、无 created 状态
        # （created 是旧建卡表的「已建卡」终态语义，助手不复用）。
        # status: idle(就绪)/running(一轮进行中)/cancelling(取消中)/interrupted(中断可续)/
        #         closed(已关闭，终态)/failed(建立失败，终态)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feishu_assistant_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feishu_chat_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle',
                session_id TEXT,
                turn_id INTEGER,
                model TEXT,
                assistant_model_source TEXT,
                awaiting_project TEXT,
                pending_json TEXT,
                last_event_id INTEGER,
                error TEXT,
                prompt_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # #54：提示词 hash（NULL = 历史行/首次部署，下轮自动重建 ACP 会话并注入最新提示词）
        assistant_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(feishu_assistant_sessions)")}
        if "prompt_hash" not in assistant_cols:
            conn.execute("ALTER TABLE feishu_assistant_sessions ADD COLUMN prompt_hash TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feishu_assistant_sessions_chat "
                     "ON feishu_assistant_sessions(feishu_chat_id, status)")
        # 部分唯一索引：一 chat 一活跃助手会话（覆盖取消中与中断态）；closed/failed 为终态
        # 不占约束，允许同一 chat 保留历史多行。空值不被唯一索引拦截 → feishu_chat_id 显式 NOT NULL。
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_feishu_assistant_active "
                     "ON feishu_assistant_sessions(feishu_chat_id) "
                     "WHERE status IN ('idle','running','cancelling','interrupted')")
        # 助手事件表（与 chat_events 隔离；id 命名空间独立，跨表引用一律用复合 (source, id)）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feishu_assistant_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assistant_session_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feishu_assistant_events_session "
                     "ON feishu_assistant_events(assistant_session_id, id)")
        # 需求评审日志（#342）：评审过程与结论结构化留档，append-only（只增不删不改）。
        # round=该需求的评审轮次（服务端按 MAX(round)+1 递增）；verdict=结构化结论（pass/close/downgrade 等自由字符串）；
        # notes_snapshot=写入该轮评审时的 notes 快照（PATCH notes 是覆盖式更新，快照供版本溯源）。
        # ⚠️ 隔离硬约束：本表内容【永不进 agent prompt】——notes/summary 才是 prompt 通道
        # （build_prompt 只读这两个字段），评审过程/结论只能写本表或产物目录；
        # 禁止在 req_list 等需求查询中联本表，禁止新增任何 prompt 侧读取方。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS req_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                round INTEGER NOT NULL,
                reviewer TEXT,
                verdict TEXT,
                content TEXT,
                notes_snapshot TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_req_reviews_req ON req_reviews(req_id, round)")
        # #375 需求状态变更审计：status 实际变化时写一行（PATCH /api/requirements/<id> 是同 status 写入通道），
        # 只增不删，需求删除后记录保留；actor=变更来源桶（dispatcher/manual/agent/api/baseline），
        # detail 记 X-Actor 头完整值与 User-Agent（无头调用落 api 桶可按 UA 识别，属约定+定位机制非安全机制）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS req_status_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id INTEGER NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                actor TEXT NOT NULL,
                detail TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_req_status_log_req ON req_status_log(req_id, id)")
        # 历史审计行默认 CURRENT_TIMESTAMP（UTC），首次迁移到配置的存储时区。
        # 已完成迁移的库保持原值；移动机器须保留原 REQ_BOARD_TZ。
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            rows = conn.execute("SELECT id, ts FROM req_status_log").fetchall()
            for row in rows:
                epoch = datetime.fromisoformat(row["ts"]).replace(tzinfo=timezone.utc).timestamp()
                conn.execute("UPDATE req_status_log SET ts = ? WHERE id = ?",
                             (format_storage_time(epoch), row["id"]))
            conn.execute("PRAGMA user_version = 1")
            if rows:
                print(f"[req-status-log] 时间口径迁移：{len(rows)} 行 UTC → 存储时区", flush=True)
        # #386 自动合并冲突网页决策通道：存在记录=该需求处于 merge 冲突待处理；action IS NULL=等待用户
        # 操作（按钮渲染信号），action∈{retry,takeover}=用户已点击待调度器 reconcile 消费（消费后删除）。
        # 与 dispatcher state.pending_confirm（kind=merge）双存储，以 pending 为权威：本表仅作
        # 页面渲染信号+决策通道，调度器消费时校验 pending 存在，无 pending 清表兜底（防幽灵决策）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS merge_conflicts (
                req_id INTEGER PRIMARY KEY,
                action TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # #375 上线快照：存量需求各写一行 baseline（幂等：已有 baseline 行则跳过，防每次重启重写）；
        # 之后新建的卡不记 initial 行（建卡 status 非「变更」），从第一次实际变更起有痕
        baseline_done = {r["req_id"] for r in conn.execute(
            "SELECT DISTINCT req_id FROM req_status_log WHERE actor = 'baseline'").fetchall()}
        for r in conn.execute("SELECT id, status FROM requirements").fetchall():
            if r["id"] not in baseline_done:
                conn.execute(
                    "INSERT INTO req_status_log (req_id, old_status, new_status, actor, detail, ts) "
                    "VALUES (?, NULL, ?, 'baseline', '上线快照', ?)",
                    (r["id"], r["status"], resolve("now_str")()))
        if chat_source_added:
            # 兼容迁移前已有的 interrupted 会话：明确取消过的保留人工恢复，其余按异常中断自动恢复。
            conn.execute(
                "UPDATE chat_sessions SET interrupt_source = CASE "
                "WHEN EXISTS (SELECT 1 FROM chat_events e WHERE e.chat_id = chat_sessions.id "
                "AND e.kind = 'system' AND e.content LIKE '%已取消本轮%') "
                "THEN 'user_cancel' ELSE 'restart' END "
                "WHERE status = 'interrupted' AND interrupt_source IS NULL")
        conn.commit()
