# 数据模型

> 由 AGENTS.md「数据模型」章节拆出（2026-08 瘦身），修改表结构/字段语义时同步维护本文档。

`requirements` 表：id、title、project、status、notes、summary、mode、phase、flow、flow_name、nodes_json、node_index、created_at、updated_at、started_at、completed_at、source、model、reasoning_effort、source_session_id

`agent_events` 表：id、req_id、kind（message/tool/system）、content、ts —— 调度器 ACP 执行过程事件，只增不删，需求删除后事件保留

`agent_runs` 表：id、req_id、role、session_id、started_at、ended_at、status（done/timed_out/failed/aborted）、tool_count、context_pct、context_tokens、max_tokens —— 调度器 ACP 会话运行记录（报表基础数据源，起止时间/状态/工具数/token 数由收尾统一落库，不再靠最后一条事件近似），只增不改；仅新数据，历史不迁移。`aborted`（#28）= 输出守卫中止（输出复读 / 单次请求超长），优先于 timed_out/failed

`agent_sessions` 表：id、req_id、role、session_id、model、thinking、harness、context_pct、branch、workdir、poisoned、status（created/idle/running/interrupted）、turn_step、turn_step_detail、last_llm_request_at、last_llm_response_at、last_thought_at、last_tool_started_at、model_requested、model_source、model_configured、model_reason、created_at、updated_at —— 调度器 ACP 会话注册表（#273 人工接管），每条 kimi 会话一行（含待接管行/毒会话），中间栏会话列表与人工继续/新建的数据源；调度器经 `POST /api/agent-sessions` 写入；interrupted=调度器重启时清算的无持有者孤儿 running（#374，进程被杀未收尾），行/session_id 保留，人工可发消息继续（语义对齐 req_chats.interrupted）。**#11 模型留痕两列**：`model_requested`=spawn 时解析出的实际请求模型（四层链 + 别名池静态校验后的值），`model_source`=来源（`project`/`role`/`env`/`global`）；「实际值」复用 `model`（`session.model_name` 观测显示名，语义不变，不污染 model-stats 等既有消费方）；请求值 != 实际值时前端会话元信息行展示「请求 → 实际」，相等/NULL 按现状只显示实际值；两列 ALTER TABLE 新增，存量行 NULL。**#431 渲染范围**：`session_id` 为空的行（「新建聊天」首条消息落行、调度器尚未接管回填）**不在中间栏展示**——`sessionFooterHtml` 渲染前先过滤，`GET`/`sessionList()` 语义不变；回填 `session_id` 后由 SSE `sessions` poke 自动出现。**#431 来源两列**：`origin`（创建来源，封闭枚举 `manual`（看板人工首条消息）/`agent`/`api`（含无头与未知桶兜底）/`dispatcher`，由请求头 `X-Actor` 冒号前段经 `_origin_bucket()` 收敛）、`origin_detail`（`X-Actor` 原文 + UA，同 `_status_log_meta` 口径，仅快速定位、非安全机制）；存量行留空（NULL）不回填，两列不在 `POST /api/agent-sessions` 的 body 白名单内（服务端按请求头推导，防伪造）。**#421 活性心跳**（状态行判活/判死数据源）：`turn_step`=`llm`（等模型响应/思考/输出中）/`tool`（工具执行中）/`''`（无在途，收尾清理）；`turn_step_detail`=工具标题摘要（≤48 字）；四个 `last_*_at` 为 epoch 秒（服务端 `time.time()`，与 `created_at` 的北京本地字符串刻意不同——避免浏览器时区解析歧义）：`last_llm_request_at`（prompt 发出）、`last_llm_response_at`（message chunk 到达，60s 节流刷新）、`last_thought_at`（thinking chunk，60s 节流）、`last_tool_started_at`（在途工具由空转非空）；**只在状态切换/节流刷新时写、无定时写库**，存量行保持 NULL（前端 fail-safe：不计时、不判卡死）。**#13 模型可用性两列**（沿用 #11 留痕面）：`model_configured` = 四层链解析出的**用户配置值**（回落前；NULL = 存量行或未解析，前端 fail-safe 不提示）、`model_reason` = 不可用或未落定**原因摘要**（人类可读单行，≤200 字；NULL = 无异常）；`model_requested` 语义不变（仍为**回落后的实际请求值**），`model` 仍为观测显示名。两列 `ALTER TABLE ADD COLUMN` 幂等迁移、存量行 NULL，`_AGENT_SESSION_FIELDS` 白名单与 `POST /api/agent-sessions` INSERT 列同步（`GET` 走 `s.*` 自动带出）。前端共享纯函数 `modelAlertText(s)`（中间栏会话行 `.session-model-warn` 与右栏信息条 `.sess-model-alert` 共用）：`model_configured` 与 `model_requested` 均非空且不等、或 `model_reason` 非空时显示「配置 X → 实际 Y（原因）」，否则为空（存量行零误报）。**#29 思考档位留痕三列**（同款 ALTER TABLE 幂等迁移，存量行 NULL；#50 起不再写入、只读保留）：`thinking_requested` = 历史选档后实际发出的档位、`thinking_configured` = 历史配置值、`thinking_reason` = 历史回落或未落定原因摘要；`thinking` 列语义不变（响应校正后的观测生效值），#50 起仍随收尾写入但前端不再展示。三列保留在 `_AGENT_SESSION_FIELDS` 白名单与 `POST /api/agent-sessions` INSERT 列，`GET` 照常返回供外部只读消费者；前端不再读取，无任何回落提示渲染。

`manual_commands` 表：id、req_id、agent_session_id、role、text、status（pending/running/done/failed）、error、created_at、updated_at —— 人工续跑命令队列（#273），看板聊天框发消息写 pending；#431「新建聊天」首条消息（`POST /api/requirements/<id>/sessions/messages`）在同一事务里落注册行 + 写本表 pending（行存在 ⇒ 命令存在或已被消费；本表的 pending/running 行是空行不可删的守卫依据），调度器轮询消费

`chat_sessions` 表：id、project、channel（web/feishu，默认 `web`）、status（idle/running/interrupted/suspended/created/failed/closed）、session_id、draft_title、draft_notes、req_id、model、error、created_at、updated_at（**#24 另增 `chat_scope` 与 `feishu_chat_id` 两列，见下**）—— 建卡对话会话（#107），一个会话绑定一个项目；`channel`（#22）区分网页建卡（`web`）与飞书建卡（`feishu`），每「项目 × 通道」最多 1 个活跃会话（两通道互不占名额，`CHAT_PARALLEL_LIMIT` 全局名额仍共用），`ALTER TABLE` 只增不删、存量行默认 `web`；suspended=挂起可续跑（兼容旧切换流程/外部调用），切走 running 轮次由 detach 标记为后台继续；req_id 非空=已建卡（终态，防重复）。**#24 增列（均 `ALTER TABLE ADD COLUMN`，不重建、不改 `project NOT NULL` 约束）**：`chat_scope TEXT NOT NULL DEFAULT 'chat'`（存量行自动为 chat，仅供旧会话兼容/历史筛选）与 `feishu_chat_id TEXT NULL`（存量建卡行为 NULL，不参与助手唯一性约束）；**助手会话不写本表**（方案 B，见下两表）

`chat_events` 表：id、chat_id、kind（message/tool/system/user）、content、ts —— 建卡对话事件流（独立于 agent_events，后者 req_id 为 INTEGER NOT NULL）

`feishu_assistant_sessions` 表（#24）：id、feishu_chat_id（TEXT NOT NULL）、status（idle/running/cancelling/interrupted/closed/failed）、session_id、turn_id、model、assistant_model_source、awaiting_project、pending_json、last_event_id、error、prompt_hash、created_at、updated_at —— 飞书看板助手会话（方案 B：**独立业务表，不写 `chat_sessions`/`chat_events`**；无 project 字段、无 `created` 状态——那是旧建卡表的「已建卡」终态语义，助手不复用）。一个 `feishu_chat_id` 同时最多一条活跃会话，由**部分唯一索引** `feishu_assistant_sessions(feishu_chat_id) WHERE status IN ('idle','running','cancelling','interrupted')` 在数据层保证（覆盖取消中与中断态；`closed`/`failed` 为终态、不占约束，允许同一 chat 保留历史多行；`feishu_chat_id` 显式 `NOT NULL`——空值不被唯一索引拦截）。插入走「先查活跃行、查不到即插」，并发撞唯一约束时捕获 IntegrityError 回读既有活跃行（get-or-create 幂等，不插第二行）；另有普通索引 `(feishu_chat_id, status)`；建表/索引均 `IF NOT EXISTS` 幂等；**#54** `prompt_hash TEXT`（`ALTER TABLE` 幂等迁移，历史行 NULL）：当前生效系统提示词的 sha256，每轮开始比对，不一致时重建 ACP 会话并强制注入最新提示词

`feishu_assistant_events` 表（#24）：id、assistant_session_id、kind（user/message/tool/system/card）、content、ts —— 飞书看板助手事件流（与 `chat_events` 隔离，只增不删）；索引 `(assistant_session_id, id)`。**#30 新增 `kind='card'`**：需求卡清单卡片的一页页 spec（版本化 JSON，服务端生成、长度 ≤ `ASSISTANT_CARD_PAGE_MAX` 才落库，不截断），由 dispatcher 渲染为飞书 interactive 卡片；`content` 是结构化 JSON，前端渲染与 `/api/history` 的 `last_message` 均跳过该 kind

- **id 命名空间与复合主键（#24）**：`feishu_assistant_sessions`/`feishu_assistant_events` 与 `chat_sessions`/`chat_events` 各自 `AUTOINCREMENT`，**数字 id 会撞号**——跨表引用统一用复合 `(source, id)`（`source ∈ {chat, assistant}`），禁止裸数字 id 定位。列表主键、事件游标、关闭端点均按来源分流：旧会话走 `/api/chat/<id>/events` 与 `/api/chat/<id>/close`，助手走 `/api/assistant/sessions/<id>/events` 与 `/close`；前端事件序列按 `(source, id)` 排序，撞号不串会话

`req_chats` 表：id、req_id、status（created/idle/running/interrupted/failed/closed）、session_id、model、error、interrupt_source、created_at、updated_at —— 需求助手会话（#302 引入，原「需求对话」详情页常驻聊天，#326 起改为中间栏置顶入口 + 右栏独立视图），一张卡一个对话，历史 closed 行保留可回看；每轮结束关进程、session_id 保留供 resume 续上下文

`req_chat_events` 表：id、req_chat_id、kind（message/tool/system/user）、content、ts —— 需求助手事件流（独立于调度器体系，只增不删）

`req_reviews` 表：id、req_id、round、reviewer、verdict、content、notes_snapshot、created_at —— 需求评审日志（#342），评审过程与结论结构化留档，append-only（只增不删不改）；round=服务端按 MAX(round)+1 递增的评审轮次，verdict=结构化结论（pass/close/downgrade 等自由字符串），notes_snapshot=写入该轮评审时的 notes 快照（PATCH notes 覆盖式更新，快照供版本溯源）。⚠️ 隔离硬约束：本表内容**永不进 agent prompt**（prompt 通道只有 notes/summary），需求查询不联本表，禁止新增任何 prompt 侧读取方

`req_status_log` 表：id、req_id、old_status、new_status、actor、detail、ts —— 需求状态变更审计（#375），只增不删（需求删除后记录保留）；status **实际变化**时写一行（PATCH 同一状态或仅改 notes 不写，建卡不记 initial 行）；actor=变更来源桶（`dispatcher` / `manual` / `agent` / `api` / `baseline`），由请求头 `X-Actor` 解析（冒号前段，如 `manual:下拉改状态` → manual），无头归 `api`；detail 记录 X-Actor 头完整值与 User-Agent（无头调用落 api 桶可按 UA 识别 curl/requests/kimi-code 等，属约定+快速定位机制而非安全机制，本机无鉴权头可伪造）；上线时存量需求各写一行 baseline 快照（old_status=NULL、actor=baseline，幂等不重复写），之后新建的卡从第一次实际变更起有痕；经 `GET /api/requirements/<id>/status-log` 查询全链。**ts 口径（#428）**：写入方显式传 `now_str()`（北京时间），与卡面 `started_at`/`completed_at` 及 `dispatcher_events` 同源；历史行由列默认值 `CURRENT_TIMESTAMP` 落 UTC，应用启动时经 `PRAGMA user_version` 守卫一次性 `+8 hours` 迁移（幂等，可回滚 `-8 hours`）

- `notes` = 需求详情（用户填写，agent 只读）：背景、方案要点、验收标准等
- `summary` = 开发总结（调度器写，用户只读）：完成总结、失败原因、用户否决记录等 `[auto]` 行
- `mode` = 通道：`auto`（全自动）/ `semi`（半自动），由通道确认（analysis 产出后）写入；建卡时为空
- `phase` = 半自动当前阶段：`proposal` / `tech` / `tech_review` / `dev` / `code_review` / `accept`（#376 增 agent 评审角色 phase，走卡）；auto 通道为 NULL
- `flow` = 流程 id（#376 半自动流程；#419 起所有入流程的卡都写，id 定义在仓库根 `flows.json`）：`standard`（标准：tech→dev→accept）/ `heavy`（复杂：tech 后方案评审、dev 后代码审查）/ `auto`（简单：dev→code_review）；`lean` 已删除（历史卡 flow 值仍为 `lean`，展示走 `flow_name` 快照、推进走卡上快照）；进入流程时写入，此后按卡上快照推进
- `flow_name` = 进入流程时的流程名快照（#419，显示用）：流程改名/删除不影响历史卡显示；调度不读本列
- `nodes_json` = 节点序列快照（#419，JSON 数组 `[{role,node_type,artifact}...]`，terminal 节点仅 `{node_type}`）；进行中的卡按快照推进，不受 `flows.json` 变更影响
- `node_index` = 快照内当前节点下标（#419，0 基；NULL = 未入流程，走前置槽位）
- `mode`/`phase` = #419 起为历史只读列（新代码不读不写，保留存量值）；`flow`/`flow_name`/`nodes_json`/`node_index` 为流程配置化后的权威字段
- 流程配置文件：仓库根 `flows.json`（前置槽位 `pre.role` + `flows[]`（每条可带 `enabled: false` 软禁用：定义保留、不进推荐目录与可选列表，存量卡照旧）+ `roles.<id>` 展示/产物/模型/prompt_file）+ `prompts/*.md`（提示词正文），改配置需重启进程生效（无热加载）
- `source` / `model` / `reasoning_effort` / `source_session_id` = 建卡来源元数据（#303）：`source` = 建卡来源（dsh/codex/kimi-code/board-chat…自由字符串，由建卡入口上报）；`model` = 建卡模型（底层大模型名，如 GLM-5.3，详情页 meta 行展示「创建人」）；`reasoning_effort` = 推理强度；`source_session_id` = 来源会话 id——四字段全部可空，历史卡为 NULL
- 调度器只捞取 `updated_at` 静置满 `MIN_AGE_MINUTES`（1 分钟）的 open 需求；录入区支持创建时直接填写需求详情，卡片创建即完整
- doing（进行中）/done（已完成）/route（通道待确认）/review（评审中）需求禁止删除（前端隐藏按钮，DELETE API 返回 400）；route/review 由调度器置入，编辑弹窗不提供手动选项
- 看板 route 卡提供「走自动 / 走半自动」确认按钮，review 卡提供「通过 / 否决」按钮（否决弹窗填原因可空）

status 六值：`later`（暂缓，仅记录、调度器不捞）→ `open`（待开发，调度器只捞这个状态）→ `doing`（进行中）→ `done`（已完成）；`route`（通道待确认：analysis 产出后等人确认 auto/semi，免确认路径不出现）与 `review`（评审中：半自动阶段产物待评审）为等待人工的中间态，归入看板「待办」tab，不参与调度器捞取

时间戳规则：进入 `doing` 记 `started_at`（semi 每阶段进入都会刷新，语义为最近一个阶段开始时间），`route`/`review` 不触发变更；退回 `later`/`open` 清空；进入 `done` 记 `completed_at`（`started_at` 保留），离开 `done` 清空。
