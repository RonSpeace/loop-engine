# AGENTS.md 条款详解（迁出全文）

> 本文件是 2026-10-13 精简 AGENTS.md 之前的完整原文，逐字保留各条款的背景、判据与事件来源。
> AGENTS.md 只留规则本身；需要细节时读这里对应小节。本文件不进 agent 常驻上下文。
> 这是历史快照：流程数量（现为 quick/standard/heavy 三条）、评审轮次上限等会变的事实以 AGENTS.md 与代码为准。

## 原标题：需求看板（req-board）

跨项目的待开发需求管理应用：记录暂缓/待做的需求，按项目分组，带状态流转。

## 技术栈

- 后端：Flask + SQLite（入口 `board.py`，后端共享层在 `board_modules/`）
- 前端：原生 HTML + CSS + JavaScript（页面在 `templates/board.html`，CSS/JS 外置在 `static/board.css`、`static/board.js`）
- 只监听 `127.0.0.1:8899`，本机使用，无鉴权

## 交流与产出语言约定

- 与用户交流默认使用**中文**；涉及本看板的状态、字段、术语时用中文描述，首次出现可附原文对照。
- 一切产出文档默认用**中文**撰写：需求分析、方案、技术设计、开发总结、验收报告、代码注释、commit message 等。
- 代码标识符、API 路径、数据库字段名、命令、文件名保留原文（如 `completed_at`、`role_for`、`/api/requirements`），不做翻译。

本看板术语中文对照（业务讨论与文档优先用中文列；流程由前置环节推荐、进入即按卡上快照锁定）：

| 代码原文 | 中文 |
|:--|:--|
| done / doing / open / later | 已完成 / 进行中 / 待开发 / 暂缓 |
| route / review | 通道待确认 / 评审中 |
| auto / semi | 自动通道 / 半自动通道 |
| mode / phase / flow | 通道 / 阶段 / 流程 |
| standard / heavy | 标准流程 / 复杂流程（显示名取自 `flows.json` 的 `name`；原简单流程 `auto` 已随流程目录精简删除）|
| analysis / proposal / tech / dev / accept | 需求分析 / 方案 / 技术设计 / 开发 / 验收 |
| tech_review / code_review | 方案评审 / 代码审查（流程内 agent 评审角色，只读档位；`code_review` 是唯一的代码审查角色——标准流程与复杂流程共用同一角色、同一提示词、同一产物 `code_review.md`，#337 的合并前闸门已由该节点承载，**两条流程都含它**） |
| completed_at / started_at / updated_at | 完成时间 / 开始时间 / 更新时间 |
| summary / notes / retries | 开发总结 / 需求详情 / 重试计数 |

## 数据保护（非常重要）

**⚠️ 绝对禁止删除用户数据 ⚠️**

- `data/board.db` 是真实需求数据，任何时候都不能删除
- 修改表结构必须用迁移方式（ALTER TABLE），不能删表重建
- 测试新功能时使用假数据，测完清理

## prompt 通道约定（#342）

- `notes`（需求详情）与 `summary`（开发总结）是 **prompt 通道**：build_prompt 会把这两个字段全角色内联进 agent 上下文，写入前视为「agent 会看到」。
- 评审过程、评审结论、meta 信息（评审轮次交锋、结构化决策等）**禁止写入 notes/summary**，只能进 `req_reviews` 表（`POST /api/requirements/<id>/reviews`，append-only）或产物目录。
- `req_reviews` 内容**永不进 prompt**：需求查询不联该表，禁止新增任何 prompt 侧读取方。

## 快速参考

| 操作 | 命令 |
|:---|:---|
| 启动 / 重启应用 | `./start.sh`（内部走 `launchctl kickstart -k`，任何场景都不会产生孤儿进程） |
| 安装 / 修复 launchd 托管 | `scripts/install-launchd.sh`（首次装机、换机、改了 `plists/*.tmpl` 之后跑一次；幂等） |
| 启动 / 重启应用 | `./start.sh`（内部走 `launchctl kickstart -k`，任何场景都不会产生孤儿进程） |
| 查看日志 | `tail -f ~/Library/Logs/req-board/req-board.log`（调度器：`req-board-dispatcher.log`；可调 `REQ_BOARD_LOG_DIR`） |
| 日志轮转 | `scripts/log-rotate.sh`（launchd `local.automation.log-rotate` 每 30 分钟跑一次，>20MB 归档 `.1`，保留 2 代） |
| 验证运行 | `curl http://127.0.0.1:8899` |
| 停止应用 | `launchctl bootout gui/$(id -u)/local.req-board`（KeepAlive 托管，kill 会被自动拉起） |

看板由 launchd 托管（`local.req-board`，KeepAlive 崩溃自拉起、登录自启；**plist 模板在本仓库 `plists/*.plist.tmpl`**，安装脚本 `scripts/install-launchd.sh` 按本机路径渲染到 `~/Library/LaunchAgents/` 并 bootstrap）。**修改 `board.py` 或 `board_modules/` 后必须重启应用（`./start.sh`）并 curl 验证 200**——适用于人工在 main 工作区修改；调度器驱动的 dev 在独立 worktree 内开发自验，不运行 `./start.sh`。

> **路径提示**：三个 launchd 任务（`local.req-board` / `local.automation.req-board-dispatcher` / `local.automation.log-rotate`）都是本仓库资产：plist 模板在 `plists/*.plist.tmpl`（只含 `{{REPO}}` / `{{LOG_DIR}}` 占位），安装脚本按本机路径渲染；入口脚本 `scripts/launchd-run.sh`（负责 cd 仓库、清 `REQ_BOARD_SUPERVISOR`、补 PATH、加载 `.env`、选 `.venv/bin/python`），安装/卸载走 `scripts/install-launchd.sh`。**唯一与仓库解耦的只剩日志目录**（默认 `~/Library/Logs/req-board/`，可用 `REQ_BOARD_LOG_DIR` 覆盖）——刻意放在仓库外，避免日志写进 `~/Documents`（TCC 保护目录）又被轮转脚本反复触碰。本机 `projects.json` 未配置 `automation` 项目，故 docs 里偶现的 `automation/data/…`（原作者布局）在本机不适用，见「日志与轮转」。

> **日志与轮转**：看板/调度器的 stdout+stderr 都落在 `~/Library/Logs/req-board/`（`req-board*.log` / `req-board-dispatcher*.log`，由 plist 的 `StandardOutPath` 指定；非 macOS 默认 `${XDG_STATE_HOME:-~/.local/state}/loop-engine/logs`，`REQ_BOARD_LOG_DIR` 可覆盖）。launchd 只管追加、不轮转，故由定时任务 `local.automation.log-rotate`（模板在本仓库 `plists/`，每 30 分钟）跑 `scripts/log-rotate.sh`：超过 20MB 归档为 `.1`（保留 2 代）并原地清空——copytruncate 方式、**不重启服务**（launchd 以 O_APPEND 持有句柄，2026-09-18 实测清空后续写正常、无稀疏空洞）。看板**访问日志默认关闭**：Werkzeug 每请求一行，调度器 2 秒/15 秒轮询 + 浏览器 SSE 轮询会把它撑到几百 MB（2026-09-18 实测 424MB，真实报错被淹没）；排查请求时用 `REQ_BOARD_ACCESS_LOG=1 ./start.sh` 打开。需求助手的 `read_logs` 白名单认得这个目录（`board.py: _reqchat_log_allowed`）。

> **⚠️ `~/Documents` 下的 TCC 陷阱**：本仓库位于 `~/Documents`（受 macOS 隐私保护）。launchd 拉起的进程读该目录会被拒（`Operation not permitted`，报 `getcwd … Operation not permitted` / `exit code 126`），**唯一解是给 `/bin/bash` 授予「完全磁盘访问权限」**（系统设置 → 隐私与安全性 → 完全磁盘访问权限）。未授权时 `install-launchd.sh` 会失败、plist 装了也起不来。首次冷启动可能因 TCC 权限评估停顿约 45 秒（之后 1-2 秒），`start.sh` 已改用就绪轮询而非固定 sleep。

## 流程配置（#419）

> **实现进度（截至本分支当前提交）**：配置层（`flows.json`/`prompts/*.md`/`load_flow_config` 校验）、展示档位单一源 + 未知角色 fail-closed、看板快照三列与 `GET /api/meta`、前端标签后端注入、`PATCH` 快照字段面、**dispatcher 流程引擎（快照驱动 `role_for`/`role_allowed`、按 `node_type` 分派推进、terminal 三入口收尾、前置槽位 prompt 契约、启动回填）与状态收缩（`STATUSES` 5 值，route/review 过渡窗口照存）已接入**——调度推进按卡快照走配置流程。遗留：`on_message`/`reconcile_decisions` 的历史 route/review 消费分支与 `review_gate` 消费面仅作过渡窗口兼容（新流程不再写入）。下述契约以 tech.md 为准。

- 研发流程是**配置数据**，唯一源 = 仓库根 `flows.json`（前置槽位 `pre.role` + `flows[]` 流程与节点 + `roles.<id>` 展示名/顺序/产物/模型/prompt_file 路径）+ `prompts/*.md` 提示词正文。
- 四层开放：前置环节 1 个共享槽位（角色可换，强制产出 `suggested_flow`（流程 id）与 `repos`）、后置流程 N 条、流程内节点可增删（`node_type` = artifact/code/review/terminal，human 本期未实现）、节点 agent 的名称/顺序/提示词/产物/模型可配。
- 角色新增 = 改两处数据：`acp_shared.ROLE_CAPABILITIES`（能力/安全档位）+ `flows.json` 的 `roles`（展示/内容位）；键集双向一致由加载期校验强制，未知角色兜底 fail-closed（只读 + Bash 白名单），不配即不放行。
- 加载期校验（`load_flow_config()` 聚合报错，fail-fast）+ 消费端自决：**board 降级展示；dispatcher 侧 fail-closed 安全停泊**——配置非法时不拉起、不推进、不收尾，仅 `log.error` + 时间线留痕（#430），不按错误配置推进。
- 内置流程显示名：standard=标准 / heavy=复杂（当前两条，`default_flow` = `heavy`——前置环节没产出合法推荐时兜底走「更完整」那条）；显示名即 `flows.json` 的 `name`，改配置即改全站文案（含历史遗留通知里的流程名）。退役一条流程 = 删掉该条定义（软禁用 `enabled: false` 亦可），删前先把引用它的历史卡 `flow_name` 冻结（卡面 chip 靠它显示，否则退化成原文 id）
- 流程软禁用：`flows[]` 每条可带 `"enabled": false` —— 定义保留（改回 true 即恢复），但不进前置推荐目录与可选流程列表；存量卡/在途卡照旧（存在面 `flows()`/`flow_by_id()` 不变），模型仍推荐被禁 id 则静默兜底 `default_flow` + `flow_fallback` 留痕。`default_flow` 必须启用、至少留一条启用流程（加载期强制）。
- `flows[].color`（#428）：流程档位配色的**语义色名**（`green`/`indigo`/`fuchsia`/`slate`，缺省 `slate`、非法值加载期报错、禁写 hex）——经 `GET /api/meta` 注入前端，映射中间栏流程 chip 配色（标准靛蓝 / 复杂品红紫，`green` 现无流程使用但仍属合法词表项；无流程快照/已退役流程中性灰；该档 2026-09-19 由玫红 rose `#fff1f2`/`#be123c` 换成品红紫 fuchsia `#fdf4ff`/`#a21caf`——原玫红与「评审中」徽标红几乎同色，同屏情绪串台，色名同步改名以保持名实一致）；色值唯一落点 = `static/board.css` 的**令牌块**（`:root` 深色默认 / `:root[data-skin="light"]` 浅色覆盖，规则经 `var()` 消费；#15 皮肤系统），词表↔令牌由 `tests/test_flow_color.py` 双向锁定，前端零手写映射。
- 改 `flows.json`/`prompts/` 后**必须重启两个进程**（看板 `./start.sh`、调度器 `scripts/dispatcher-restart.sh <pid> config-change`），无热加载。

## 会话状态行（三处共用一张状态卡片，#430）

右栏底部状态行三处——调度器会话 `#agentChatStatus`、需求助手 `#reqChatStatus`、聊天建卡 `#chatStatus`——共用同一套实现：卡片容器/五档配色（绿=正常、琥珀=偏慢或过渡中、红=失败或疑似卡死、蓝=执行中阶段未知、灰=已停止或已结束）/图标层级/判定依据展开/1 秒计时 = `board.py` 的 `.chat-status` 系列 CSS + `chatStatusCardHtml` / `applyChatStatusCard` / `setStatusNoInput` / `toggleChatStatusDetail` / `tickChatStatusCard` / `chatStatusTick` / `ensureChatStatusTicker`（**卡片规则一律不带视图前缀**）。每处的差异**只允许**落在状态模型纯函数（`agentActivityModel` / `reqChatActivityModel` / `chatAddActivityModel`）与动作位 HTML。

- 新增/改造状态行必须复用共享卡片，不得再按视图分叉样式；主按钮三处恒为「发送」，取消本轮/新建对话（新建会话）在卡片动作位。
- 不可输入只隐藏输入件（`.no-input .chat-input-row > textarea` / `> button`，直接子代选择器），卡片内动作按钮不受影响。
- 需求助手与聊天建卡没有调度器的心跳/阶段字段（`turn_step` / `last_llm_*`），只做「会话状态 + 事件流时间」两档证据：**不判死、不给「重启调度器」**；计时基准 = 本轮最后一条 user 事件（`updated_at` 会被建卡/写产物等工具调用刷新，不能当轮次起点）。
- 契约由 `tests/test_status_line_card.py`（CSS + 接线）与 `tests/test_chat_status_card.py`（共享构造器 + 两个模型矩阵，node harness `tests/chat_status_card_harness.js`）锁定。

## 生效方式约定（长驻进程改动必须显式标注）

- 涉及长驻进程/服务类改动（被 launchd 托管、需重启/部署才生效的进程）的需求：建卡 notes「生效方式」栏、analysis/tech 证据清单与方案、dev 完成总结必须显式标注生效方式（是否需重启/部署、重启对象、谁触发），不留空；无法确定写「待方案确认」。
- **重启不对称**：`board.py`（含 `board_modules/`）改动合并后由调度器自动重启（`_kickstart_board_if_own`），「合并即生效」；调度器自身模块（`dispatcher.py` / `acp_shared.py`，#423 起清单含后者）由独立 launchd 服务 `local.automation.req-board-dispatcher` 托管，改动合并后由调度器登记自动重启（#380，`state["pending_restart"]`），仅首次部署/兜底/应急才手动走 `scripts/dispatcher-restart.sh <pid> [来源]`（KeepAlive 自动拉起）。
- **直跑模式例外（#8）**：`run_local.sh` 导出的 `REQ_BOARD_SUPERVISOR=run_local` 下无 launchd supervisor——调度器重启改走进程内 `os.execv` 自重启（同 PID，整组不停止；失败拒绝重启绝不裸退出）；看板自动重启降级为事件 + 页头琥珀角标提示（人工 Ctrl-C 后重跑 `run_local.sh`）。未设标记（launchd/未知）全链路逐字不变（缺省即旧路径）。

## Git 提交约定（对本项目工作的 AI Agent，预先授权）

- 完成一处改动并验证通过（编译/重启/curl 等）后，**直接 `git commit`，无需等用户逐次确认**；message 简述改动内容，风格参考 git log。
- 只提交本次改动相关的代码文件；`data/`、`projects.json` 等本地数据不提交（已 gitignore）。
- 授权仅限 commit：`push`、`reset`、`rebase` 等改写历史或触达远程的操作仍需用户明确要求。

## 建卡与直接开发的边界（教训来源 #415）

> **引用说明**：`#415` 是**事件引用**——指「卡 #415 那次发生过的事」，不是「卡 #415 里写了这条」。卡 #415 本身讲的是网页「转开发」按钮补反馈意见，与本节内容无关；本节的教训来自那次的做法：卡已建（2026-09-05 11:39:14），却在 main 工作区直接实现，11:45:12 以 `curl` 置 done，全程零调度器事件（`agent_events` / `req_reviews` 均为 0 条）。

**两条互斥路径，不允许混用：**

- **直接开发的需求不要建卡**：用户明确说「直接开发」时，直接在 main 工作区改代码、按上面 Git 约定提交即可。改完不要为留档/复盘建卡（done 卡是终态，调度器不会捞它；已实现需求建卡需先向用户确认用途，见 req-board-card skill）。痕迹以 git log 为准。
- **建卡的需求不用直接开发**：已建卡（含人工建的）的需求，走调度器流程（open → 分析/开发/审查），AI 不绕开调度器直接改 main 实现它；除非用户明确要求人工收口（卡上会有「人工收口」标记）。**「建卡／走调度器流程」只定路径、不定时机**：卡默认 `later`（暂缓，调度器不捞），只有用户明确说「开始开发／安排开发」才建 `open`；引用不出这句原话就是没有该指令（建卡 #423 时的现场教训）。
- 判断冲突时（用户只说「按这个效果开发」，没说建卡也没说不建卡）：**先问一句**「直接开发，还是建卡走流程（建卡默认暂缓，要立刻开发请说明）」，不要自作主张补建卡，也不要把「走流程」当成开发指令。

## 需求卡 notes 编写规范（教训来源 #409）

**notes 只写「要做什么」，不写讨论过程与「为什么不做」。** 读卡的是下游 agent（分析/开发/审查）与接手的人，协商痕迹对他们是噪音。

- **不要写**：决策过程（「待用户确认」「已确认不做」「待定」这类段落）；被否决方案的取舍理由与解释性理由（「理由：…」「不要再为此引入…」）；历史元信息（commit 号、「X 月 X 日走查后收敛了范围」这类过程句）；针对开发者的劝告式语句。
- **范围排除**用陈述式要求：「页面不提供：新增、删除、导出 CSV、…」。
- **实现雷区**用事实 + 证据：「删掉 trae 会让 AI Coding 总成本少算 ¥22,133.52（202604~202608）」，不用「坑」这种过程视角。
- 适用于所有写 notes 的入口：人工、建卡对话（#107）、需求助手（#302）的 `create_requirement` / `update_requirement`。写入前按本节自查。

## 需求编号引用规范（教训来源 #433）

- **写 `#N` 前必须先建卡取号**：卡号的唯一来源是看板 `POST /api/requirements` 的自增 id；**禁止预判号**（并发会话各按「当前最大号 +1」推算会拿到同一个号——那次事故即 24 处引用指向了另一张卡）。
- **外部编号不得裸写**：上游 issue / TAPD 等写成 `upstream#351`、`tapd#12345`，与本地 `#N` 区分（`#351` 曾是反例：13 处注释指的是 kimi/ACP 上游的 Bash 委托，而本地 351 号卡是另一个项目的一条需求）。

## 自动开发调度器（dispatcher.py）

轮询 `projects.json` 中各项目的 open 需求，自动拉起 `kimi acp` 子进程开发（调度器作为 ACP 客户端驱动会话）。**本节只列红线与行为契约；流程叙事、实现细节与排障见 [docs/dispatcher.md](docs/dispatcher.md)。**

- **进程安全红线（#108）**：agent 禁止 kill/killpg/pkill 自己的 ACP 驱动进程 PID、其进程组及任何 kimi 进程（含 kimi-chat-acp）；禁止自行创建/驱动 ACP 会话（import dispatcher 调 run_agent/spawn_agent，或直接运行 `kimi acp`）——会话只能由调度器拉起，人工续跑一律走看板聊天框入口（自行拉起的会话无人收尾，注册表会残留 running 僵尸记录）。清理测试实例残留必须用 `scripts/test_instance.sh`（PID 文件 + 精确 PID，禁 pkill 路径猜测）。停止调度器前先确认没有正在运行的 agent（`pgrep -f "kimi acp"`），否则孤儿 agent 改一半代码。
- **结果契约**：结束前把结果写入 `.req-board/req-<id>/result.json`：`{"status": "done|blocked|need_confirm", "summary": "...", "question": "..."}`；analysis 另带 `suggested_mode: auto|semi|manual_close`，可选 `repos`（本需求实际改动的仓库目录名数组，名字须在仓库清单中，识别不出可省略）。可选 `todos`（字符串数组）声明需人工处理的**遗留待办**（如「重启 xxx 服务并浏览器验收」），调度器在 done 通知逐条红字展示并写入 summary 留档，仅展示不自动执行；**合并分支由调度器自动执行**，不要声明；配置了 `restart` 配方的项目服务自动重启，todos 不要声明重启类待办。必须用脚本或 python `json.dumps` 生成（禁止手工拼 JSON），字符串内换行必须 `\n` 转义（裸换行 = 非法 JSON，现场改名 `result.json.invalid` 保留）；兼容旧约定（工作目录根 `.req-board-result.json`）兜底。done 需有实际代码变更（dev）或对应产物（只读角色）才采信；dev 报 done 后先过只读 `code_review` 代码审查节点（#337，两条内置流程都含该节点）才放行合并；收尾校验不通过且带 `waive_gate` 转人工裁决，未申报视为谎报（fail-closed）。失败/重试/审查/仲裁的分流细节见 docs。
- **涉及仓库清单（repos）**：需求 notes 可人工声明 `repos: <仓库目录名, 逗号分隔>`，**覆盖** analysis 识别结果；两者都无则 dev 视为全量。清单是「指引 + 反馈」而非硬约束：dev 在项目全部仓库建 worktree，清单外仓库确有需要也可改，done 时按实际改动仓库收口并在事件/summary 标注。analysis 识别出清单外仓库（疑似建错项目）→ 免确认也不直通 dev，转「通道待确认」人工把关。
- **dev 独立 worktree（analysis 起全角色）**：调度器在项目全部仓库创建 per-requirement worktree（分支 `f-YYYYMMDD-<req_id>`，从主干默认分支拉取，幂等复用），agent 在其中调研/开发/自测/提交——**不 push、不切换分支、不动 main 工作区**；产物统一写 `~/.req-board/artifacts/<project>/req-<id>/`。验证在 worktree 内进行，跑测试/脚本一律用 main 工作区 `.venv/bin/python` 绝对路径（worktree 内无 venv）；worktree 内 `projects.json` 是主工作区那份的**软链**（本地数据不进 git，调度器建/复用时幂等补齐）——保证项目清单语义与 main 一致，避免测试在 worktree/main 下结论不同；需重启服务自验时用测试端口/独立实例，不要运行 `./start.sh`。普通项目 done 自动合并回 main；仅推分支项目（`branch_only`）不本地合并，只把有改动仓库的 f-分支 push 到 origin（细节见 docs）。
- **写工具白名单（#303）**：内置 Edit/Write 与 fs 写的绝对路径仅限三处——各仓库 worktree 目录、产物目录、临时目录（/tmp 与系统 TMPDIR）；写真实仓库主工作区（含软链指向的仓库文件）会被拦截，目标代码不在 worktree 内时报 need_confirm 等人工处理。**#434 起三处闸门同源**：审批层与 fs 落地层都以会话显式 `write_roots` 为准（此前这两层只认产物目录，与 #303 各说各话——需求助手写 `/tmp` 探针被拦并取消整轮），判定入口 `acp_shared._in_explicit_write_roots`；未配 `write_roots` 的会话维持原产物口径（fail-closed 默认不变）。
- **残留 worktree 回收（#428）**：worktree/分支回收除 done 收尾路径外，还有启动对账 + 主循环定期巡检（`cleanup_stale_worktrees`，间隔 `REQ_BOARD_STALE_WT_SEC` 默认 300s）——人工合并、人工置 done 不走调度器收尾也不会留下残留（人工收尾后无需手动 `git worktree remove`/`git branch -d`，等一个巡检周期即可）。回收前逐仓库校验「worktree 无未提交改动」+「dev 分支已合并入主干」，不满足则保留现场并一次性告警（需求时间线可见）——**人工置 done 但分支未合并时不会被强删**；非 done/dropped/卡不存在的需求一律不碰。
- **Bash 写主工作区护栏（#336）**：dev/accept 的 Bash 审批对写操作目标做启发式解析（重定向 / `tee` / `git -C <路径>`+写子命令 / `rm`/`mv`/`chmod`），目标落在主工作区即拒绝并事件留痕。**边界如实声明**：这是防手滑护栏而非机制保证（base64/变量拼接、`cd` 后相对路径、解释器 `-c` 与 heredoc 正文、`sudo`/`xargs` 链均可绕过）；机制保证仅只读角色（白名单 fail-closed），写角色是「护栏 + prompt 自觉」。
- **验证约定**：**验证强度按耦合面定，不按改动行数定**——先 grep 出「谁会读到/依赖这个改动」（测试、脚本、进程），跑这个最小集。反例（过度）：只改 `AGENTS.md` 却跑全量——耦合面实测仅 4 个测试文件 / 100 用例 / 约 4 秒（2026-09-19），全量 1535 用例 / 175 秒中多出的部分与该改动无关（当时全量还固定报 2 个缺环境变量的环境 error，每个会话都得自证一次无关——该噪声已从根上修掉：改用例显式注入，不再依赖机器环境），徒增待解释的噪声。反例（不足）：改动落在 `board.py` / `dispatcher.py` / `acp_shared.py` 的**行为路径**，或耦合面无法界定（公共函数 / prompt 模板 / 配置加载）却只跑局部。**纯注释 / 文档字符串改动按「会解析该文件源码的测试」走**——这类改动看似无害，但仓库里有大量测试按源码文本断言（基线：`board.py` 注释改动 = 19 个文件 / 381 用例 / 59.5 秒），只跑「改的是注释」对应的那一个用例（如 `test_served_js_syntax`）属验证不足。全量中出现的失败先判因果（缺环境变量 / 已知 flaky），确认无关后一句话记录、不扩大排查。长耗时验证（全量测试、构建、启动自验等）一律**前台运行并等待完成**，禁止把关键验证作为后台任务（`run_in_background`/nohup 等）启动后空轮询等待结果——ACP 会话可能在等待中结束（进程退出码 0），后台任务随进程丢失，验证结果不可采信（#102 失败根因）。结束回合前必须过检查单：1) 没有后台任务在运行/等待；2) 关键验证已前台跑完；3) `result.json` 已写入并回读可解析。**重试/续跑场景**：先确认改动完好、无残留 `result.json`，再继续验证。**测试隔离**：测试里凡走到 `build_prompt`/`spawn_agent`（其首句是 `ensure_dev_worktrees`）必须隔离——否则会真在 `~/.req-board/worktrees` 下建 worktree、真建 `f-` 分支。调度器侧另有兜底（#420：未 `load_state()` 的进程 `save_state` 拒绝写盘并告警；测试进程中 `ensure_dev_worktrees` 对未隔离路径短路、不跑 git），但兜底只覆盖这两处、不等于测试进程零副作用，用例仍须显式隔离；巡检用 `python3 scripts/test_isolation_audit.py`（探针跑全量，列出未隔离用例，**裸跑**不设 `REQ_BOARD_STATE`，否则 B 判据误报）。
- **浏览器级验证约定（#375 教训）**：diff 涉及浏览器端行为（内嵌 HTML/JS/CSS、`fetch`/HTTP 请求头、表单交互、页面渲染）时，单元测试、curl 端到端、JS 语法检查**不构成**浏览器级验证（#375 的 X-Actor 中文头 bug 即三层验证恰好全部绕开浏览器 fetch 的头值字符集校验）。**对 dev 与 accept 同样适用**：dev 会话**已具备浏览器工具**（#379 注入 playwright），浏览器端改动**必须**起测试实例真开浏览器走主路径，并在 self-test.md 单列「浏览器验证」小节；accept 按「项目实例脚本能否拉起被测进程」判承载关系，能拉起的必须在该 worktree 独立实例（显式唯一端口）上跑真实链路，并在 accept.md「验证方式与证据」小节留痕（端口分配/截图落点/清理手段/现场自证见 `prompts/accept.md` 契约）。**降级出口**：工具确实不可用或实例脚本拉不起被测进程时才可降级（写明原因）——dev 如实写「未做，需人工验收」并在 `result.json` 的 `todos` 声明人工验收项（两处必须一致）；accept 在 accept.md 写明「不可行 + 原因」、人工验收项进「未覆盖项」小节、summary 点明指引（accept 不声明 `todos`，待办归 dev）。
- **时间感知（#109）**：单次运行默认上限 60 分钟，需求 notes 可传 `timeout: <分钟>` 覆盖（clamp 15~120，不全局放宽）。用 `date` 关注实际耗时，**约剩 5 分钟**时先 commit 当前进度（dev 角色）+ 写 `result.json` 保底再继续。**失败原因**（运行超时/进程异常/非法 JSON 等）持久化在 `data/dispatcher_state.json` 的 `retries[req_id].last_failure`，重试/续跑会话会注入该原因，先读原因自查再继续，不盲续。
- **会话续跑**：会话记录存 `data/dispatcher_state.json` 的 `sessions`（归属键 = 需求 id，analysis 阶段用 `analysis:<id>`）；自动重试耗尽的会话标记 `poisoned`（毒会话）——自动路径跳过、中间栏仍展示，人可在右栏聊天框继续或新建；调度器重启时清算无人认领的 running 僵尸会话并标 `interrupted`。细节见 docs。
- **建卡对话（#107）与需求助手（#302）安全边界**：均由 board 进程内直接起 `kimi acp` 子进程（不走调度器）。建卡对话：读=项目目录 + 只读 MCP（fail-closed，.env 等敏感文件黑名单不可读）；写=**仅** `create_requirement`（project 强制绑定项目、status 限 later/open 默认 later、幂等防重复）；禁止写任何仓库文件；配置 `mysql` 的项目额外提供 `mysql_query` 只读工具（凭据不进 agent 上下文）。**#12 起建卡对话支持上传本机 html 参考文件**（粘贴/拖拽/点选，仅 .html/.htm，单文件 ≤10MB、单轮 ≤5 个，服务端 fail-closed 复核文件名与读入字节）：落盘根 `REQ_BOARD_MATERIALS`（默认 `~/.req-board/materials/<project>/chat-<id>/`，本机数据区、与 artifacts/worktrees 平行，原文件保留不自动清理），发送时把「文件名+绝对路径」清单注入该轮 prompt（agent 用 `read_file` 读源码，不内联不渲染），建卡成功后复制一份到 `ARTIFACTS_ROOT/<project>/req-<id>/materials/` 并把该绝对路径追加进 notes（回写走 `req_add`→复制→`req_update` 三步序，复制失败降级不写）。需求助手：写=**仅显式授权的工具**（`create_requirement`、`update_requirement`（title/notes/summary + **#434** status，仅 later/open）、`write_artifact`（仅本需求产物目录）、**`post_review`（#434：追加评审记录到 `req_reviews`，只追加不可改删；round 与 reviewer 由服务端填——reviewer 取写入时刻的实时底层模型，agent 读不到 config.toml 也不必读）**、`tapd_comment`（token 读自 `~/.kimi-code/mcp.json` 的 tapd env，不进 agent 上下文）），git 另授权 `add`/`commit` 与 #355 合并授权（`merge`/`checkout`/`switch`/`stash` 受限形态，仅需求助手会话开启），配套放开项目工作目录内文件写入（**#434** 起写白名单 = 项目工作目录 + `/tmp` 与系统 TMPDIR，会话专属子目录 `/tmp/req_board_reqchat_<id>/`；其它绝对路径仍拒）；Bash 写操作无论何时全拒（fail-closed）。**「需求助手能跑 req-board-review」即靠 `post_review` + `update_requirement(status=open)` 这两条写回面**（沙箱内没有 curl、不能跑脚本，skill 的脚本写法只在完整 agent 环境可用；skill 内已加「运行环境适配」分支）。流程细节见 docs。
- **飞书交互**：机器人走 WebSocket 长连接（无需公网）；agent 报 need_confirm 时推送问题，回复 `y <编号>` 同意、`n <编号> <原因>` 否决（按 kind 分派，语义见 [docs/guide.html](docs/guide.html)「飞书回复与状态流转」表）。**改动 on_message 的 y/n 回复处理逻辑时，必须同步更新该表与回复格式说明。**#24 起飞书私聊普通文本（非 y/n / 素材 / 建卡指令，且无活跃建卡会话）进入跨项目「看板助手」（自然语言查项目/卡/服务状态 + 确认项目后跨项目建卡，单轮 90 秒超时，ACP `tool_profile='feishu_assistant'` 只注入助手业务工具）；旧建卡链路仅在有活跃会话或整行「新建建卡」时进入，首条消息问候后继续处理本条——细节见 [docs/dispatcher.md](docs/dispatcher.md)「飞书看板助手（#24）」。**#26 起**助手另有开关控制的能力（`feishu.json` 的 `assistant_file_tools` / `assistant_status_write`，**#30 起**加 `assistant_req_cards`，**默认关闭、每次工具调用现读 → 网页「设置 → 通知渠道」标签页「飞书机器人」小节的「看板助手能力」分组保存即生效、无需重启**）：**代码只读分析**（file_read / file_find / code_search，范围 = 项目清单全部项目、跨项目，受路径/敏感黑名单/限量约束，跨项目扫描全局共用一份预算）与**状态变更**（仅 暂缓 → 待开发，统一走 `req_update` + `_log_status_change` 审计，已是 open 幂等不写审计）；**#30 需求卡清单卡片**（`send_requirement_cards`，工具面 12→13）按项目取需求卡清单，board 侧服务端确定性分页（页内 ≤20 条且页 spec ≤3000 字符，最多 10 页，超出末页显式 truncated+remaining）写 `kind='card'` 事件，dispatcher 侧 `build_req_list_card` 渲染飞书交互卡片（失败降级为该页完整文本 `send_text_lossless`，绕开 `send_text_chunked` 的 5 片丢尾）；开关关闭时工具明确回绝（中文能力名 + 指向网页设置），不静默降级。**#54 代码拉取与提交**（`project_git_status` / `project_git_pull` / `project_git_commit`，工具面 13→16）：`feishu.json` 的 `assistant_git_ops` 控制、默认关闭、每次工具调用现读；`project` 只按 `projects.json` 清单 key 命中，`repo` 只在该项目 paths 内精确匹配（多仓库缺省/未知/多候选一律拒绝并列候选），固定 argv 的 git 子进程、无 push、不切换分支；拉取遇冲突保留现场并返回冲突文件清单 + 四个选项（ours/theirs/手工/abort），resolve 只自动处理 UU/AA，delete/rename 返回 manual_required；提交为两阶段（本轮预览发 token、**下一轮**带 token 才能提交，绑定会话/项目/分支/消息/HEAD/工作区指纹，10 分钟单次有效），未跟踪文件需显式 include_untracked，敏感文件名窄名单 + 高置信凭证格式可显式放行；预算 = 轮次剩余 deadline - 5s，取消/超时终止 git 子进程。**提示词硬保证**：`feishu_assistant_sessions.prompt_hash` 与当前提示词 sha256 不一致（含历史 NULL）时，关闭旧 ACP 进程、清 `session_id`、`session/new` 并强制注入最新提示词，旧上下文丢弃。**#52**：`feishu.json` 的 `assistant_vision`（读图/多模态）在同分组提供网页开关，**缺省开启**（缺键/文件损坏按开启，显式 `false` 才关闭）；每次组装 prompt 现读，关闭时图片降级为文件名 + 绝对路径并附「助手当前未开启读图」说明。
- **网页端重启调度器入口（#421）**：右栏会话状态行红态（疑似卡死）提供「重启调度器」——二次确认后 `POST /api/dispatcher/restart`，服务端按同一红态口径复核（`status='running'` + `turn_step='llm'` + 心跳与 DB 最新事件双静默 >180s，证据不足一律 400），通过后写 agent_events 留痕并复用 `_restart_dispatcher(source='web')`。**有意不做**需求助手那套「有运行中 kimi agent 即拒」预检（卡死现场恰有活进程，预检会让入口永远不可用）；代价 = 其它运行中会话被中断（#374 下次启动清算为 interrupted 可人工续跑）、卡死进程可能残留为孤儿进程（人工 `ps -axo pid,ppid,command` 定位 ppid≈1 后 kill，agent 不得自行 kill）。
- **输出守卫（#28，事件持续在写时的复读/超长单请求兜底）**：`ACPSession` 加两个「本轮无效」判据——判据 A（单次 LLM 请求在途时长超 `GUARD_REQUEST_SEC`，默认 1200s；工具执行期不计时）+ 判据 B（连续 `GUARD_REPEAT_N` 条 message 事件相邻文本相似度 ≥ `GUARD_REPEAT_SIM`，默认 8 / 0.8）。阈值 4 个 env 常量单一源在 `acp_shared.py`（`KIMI_GUARD_REQUEST_SEC`/`KIMI_GUARD_REPEAT_N`/`KIMI_GUARD_REPEAT_SIM`/`KIMI_GUARD_REPEAT_MIN_LEN`），改后重启调度器生效。判据挂 `_flush_loop` 看门狗与 `_flush_message`（事件持续在写也跑，补 #421 双静默红态的盲区）；触发即单次置位留痕（agent_events + dispatcher_events + summary 单行）→ cancel 本会话（只影响本会话，`close(kill)` 仅杀本进程组）→ 唤醒在途 prompt 抛 `TurnAborted`，`run_agent` 首轮/续接轮均捕获并走超时同款收束（跳过无结果续接、`agent_runs.status=aborted`、进失败重试通道）。仅调度器会话启用（board 建卡/需求助手不传 `output_guard` = 零行为）。生效方式：`acp_shared.py`/`dispatcher.py` ∈ `RESTART_TRIGGER_FILES` → 合并后由调度器自动重启生效（首次部署 `scripts/dispatcher-restart.sh <pid>`）。
- **会话预算（C2）**：自动开发会话每轮工具调用步数上限由调度器强制——`acp_shared.role_budget(role, project)` 取默认 `DEFAULT_ROLE_MAX_STEPS`（dev 300 / accept、code_review 200 / analysis、proposal、tech、tech_review 150 / merge_resolve 80），projects.json 项目级 `budgets.<角色>.max_steps|max_minutes` 覆盖；达上限经 #28 守卫通道（kind=`budget_steps`，时间线「预算截停」）取消本轮，同会话给一轮限步（`BUDGET_WRAPUP_STEPS`）收尾写 result.json：写成按正常回合收尾，未写成走 aborted 失败通道，`last_failure` = `budget_exceeded: steps…`。`max_minutes` 收紧单轮时限（notes 显式 `timeout:` 优先，merge_resolve 时间盒不变，`effective_timeout_minutes`），超时归因 `运行超时（budget_exceeded: time…）`。人工续跑（hold/复用轮）不限步。上下文 token 上限未实现：kimi 普通回合不推 `usage_update`，运行中拿不到可靠用量。
- **summary 进 prompt 折叠（C4）**：`build_prompt` 注入前经 `compact_summary` 折叠——保留末尾 `SUMMARY_PROMPT_KEEP`(6) 条 `[auto]` 记录全文，更早的折成一行摘要（含打回次数）；首段人工前言与含「人工」的记录（答复/裁决）永不折叠；模板已含 `{review_feedback}`（tech/dev）时 `{history}` 置空（原全文注入两遍）。打回 extra 内联评审产物限 `REVIEW_EXTRA_MAX_CHARS`(6000) 字符，超出截断并给完整路径。
- **工具输出限幅（C6）**：只读 MCP `tools/call` 单次输出经 `cap_tool_output` 限 `TOOL_OUTPUT_MAX_CHARS`(30000) 字符（超出截断并说明缩小范围方式）；`read_file`/看板助手 `file_read` 未读完时末尾给「继续读用 offset=N」（`ro_read_continuation`）；`git_status` 超行数显式标注截断。
- **评审打回熔断与升级（C3）**：同一评审节点第 `FLOW_REVIEW_ROUNDS_MAX`(=2) 次打回时，作者角色模型升级为 projects.json 项目级 `escalation_model`（`acp_shared.project_escalation_model` 每次现读；未配置/不在别名池/不可用/与作者同模型 → 不升级，`kind="model"` 时间线留痕后照常打回），升级记录 `state["review_escalation"][req_id][role]` 由 `spawn_agent` 自动带上（`run_agent` 的 dict 型 `model_override`，`model_source=escalation`；评审打回 / 人工 n 打回 / 失败重试共用），评审通过 / 人工放行 / `clear_flow_rounds` 清除；第 MAX+1 次打回转 need_confirm，问题附各轮打回要点（`state["review_round_points"]`，不写 notes/summary）。
- **改动调度器模块后重启**：一律走 `scripts/dispatcher-restart.sh <pid> [来源]`（校验 pid 确为 dispatcher.py 进程、写来源标记再 TERM，KeepAlive 自动拉起）；看板需求助手 `restart_dispatcher` 工具等效（来源记 reqchat）；裸 `kill` 仅应急。done 流程命中调度器模块改动（`dispatcher.py` / `acp_shared.py`）时由调度器自动重启（#380），本约定用于首次部署/兜底/应急。
- **角色能力与展示单一事实源（#339/#419）**：能力/安全档位（审批路由/写白名单/产物契约/Bash 策略）单一源 = `acp_shared.py` 的 `ROLE_CAPABILITIES`（经 `role_capability()` 派生）；展示/内容位（display/order/artifact/model/prompt_file）单一源 = `flows.json` 的 `roles.<id>`——board 侧经 `GET /api/meta`（状态/流程/节点标签）与 `GET /api/roles`（角色 display/order）注入，dispatcher 的 `ROLE_NAMES`/`ROLE_PROMPTS` 与 `board.ARTIFACT_FILES` 均为配置派生，**无手写展示副本、不需要手工同步**。键集双向一致由加载期校验 `load_flow_config()` 强制；展示一致性由 `tests/test_role_manifest.py` 与 `tests/test_flow_config.py` 锁定。新增角色 = 改两处：`acp_shared.ROLE_CAPABILITIES` + `flows.json` 的 `roles`（并新增其 `prompt_file` 指向的 `prompts/<role>.md`）——见 [docs/dispatcher.md](docs/dispatcher.md)。
- **模型请求值与降级兜底（#11）**：建会话前经 `acp_shared.resolve_role_model()` 做别名池静态校验（只覆盖「不在池」；池不可用 fail-open），不在池即按四层链兜底回落并留痕（`kind="model"` 时间线 + `agent_sessions.model_requested`/`model_source`）；`_apply_model()` 写后回读判定落定（非 settled 记 WARNING + 事件，`acp_shared` logger 已挂调度器 FileHandler）；运行期不可用（限流/欠费/下线/连续 5xx）复用既有重试分支，仅追加单次 `model_override=ACP_MODEL` 兜底重跑（`state["model_fallback"]`，不写永久降级）。`role_model_detail` 语义不变。**#13 复修**：静态校验由「不在池」扩至「在池但不可用」——别名池改 `model_alias_pool()` 返回 `{alias: {provider, type, protocol, usable, reason}}`，可用性判定 `model_alias_usable()` = 模型显式 `protocol` 或所属厂商 `type` ∈ 词表 `HARNESS_PROVIDER_TYPES`（openai / anthropic / openai_responses / google-genai / kimi，大小写敏感精确匹配；`HARNESS_PROVIDER_TYPES_HINT` 供文案），不可用走同一兜底链回落并留痕（`resolve_role_model(pool=)` 兼容旧 set 口径，成员一律可用）；留痕新增 `agent_sessions.model_configured`（四层链配置值，回落前；NULL=存量/未解析）与 `model_reason`（不可用/未落定原因摘要 ≤200 字；NULL=无异常），前端共享纯函数 `modelAlertText` 在「配置值≠请求值」或 `model_reason` 非空时展示「配置 X → 实际 Y（原因）」。模型设置 tab 暴露 `provider_types` 与 model 行 `protocol`：provider `type` 按词表校验（非法 400 + 合法值清单）、`models_add` 支持可选 `protocol`、新增 op `models_update`（按 `alias` 设/改/清 `protocol`，`protocol` 与 `clear_protocol` 不可同传），`_harness_cfg_splice` 新增 `model_field_set`/`model_field_delete` intent（仅标准 `[models.<alias>]` 段形态，非该形态按既有 400 拒绝）。**#18 补齐**：官方 `[models.<alias>]` 唯一必填字段 `max_context_size`（整数 ≥1）纳入——判定层 `model_alias_usable()` **置前**判表条目（`has_model_entry`，由 `model_alias_entries_from_config` 富化）该字段缺失/非法 → 不可用并回落（不等 `session/set_config_option` 拒绝，reason 经 `_why` 透传）；完整性文案单一源 = `context_size_problem()`（判定层与看板 GET 的 `incomplete_reason` 共用）；设置页 `models_add` 必填、`models_update` 可设/改（无清空语义）、最终态校验读法 A（被 `models_add`/`models_update` 作用过的条目最终态必须完整，未触碰的存量不阻断，同单新增又删除跳过），整数渲染走新增 `_toml_render_value`（裸整数）；新增别名按厂商 `type` + 模型名预填 `max_context_size`（规则单一源 `CONTEXT_SIZE_DEFAULTS` 经 GET `context_defaults` 注入，仅 UI 可改预填、不参与校验、未命中留空）。明确不做：存量不完整条目批量高亮/一键补全；`max_output_size`/`capabilities` 不纳入；无 DB 迁移。**#50 思考档位统一模型默认**：下线思考档位请求与配置/展示通道——`ACPSession.start()` 不再发 `set_config_option {configId:"thinking"}`（`session/new` 与 `session/resume` 两路径一致），`projects.json` 的 `thinking` 段与 `flows.json` 的 `roles.<id>.thinking` 不再读取/校验（旧键静默忽略、不阻断加载），`KIMI_THINKING*` 环境变量链失效；`agent_sessions.thinking` 仍记录响应观测值，`thinking_requested`/`thinking_configured`/`thinking_reason` 三列只读保留（不再写入、无新增 `kind="thinking"` 时间线）；前端删除「思考回落：」提示行、机器人抽屉「思考深度」列与设置页「全局推理深度」行，模型回落提示链路逐字不变。
- **ACP 协议面契约（测试锁定）**：手写 ACP 客户端收发的**方法名**由 `tests/test_acp_schema_contract.py` 锁定（官方 `schema/v1/meta.json` vendored 快照 + 从 `acp_shared.py` 源码提取，双向断言）；改方法面必须同步该测试的 `EXPECTED_*`/`DEVIATIONS`，上游是否漂移用 `python3 scripts/acp_schema_sync.py` 查——详见 [docs/dispatcher.md](docs/dispatcher.md)「ACP 协议契约测试」。
- 项目清单在 `projects.json`（看板 project 名 → 本地目录，含本地路径已 gitignore），加项目改这个文件；可选 `mysql`/`repo_map`/`base_branch`/`branch_only`/`restart` 配置见 docs。

## 数据模型

表结构、字段语义（notes/summary/mode/phase）、status 六值与时间戳规则见 [docs/data-model.md](docs/data-model.md)。

## API（供本页面前端和各项目的 AI Agent 使用）

全部 REST 端点一览表见 [docs/api.md](docs/api.md)（服务运行在 `http://127.0.0.1:8899`）。

## 跨项目协作约定（供其它项目的 AGENTS.md 引用）

各项目的 AGENTS.md 中应包含以下约定，使 AI Agent 自动维护本看板：

> **需求看板协作约定**
>
> 本项目的需求看板运行在 `http://127.0.0.1:8899`（若未运行可跳过，不要报错）。
>
> 1. **暂缓需求建卡**：讨论后决定暂缓的需求，调用 `POST /api/requirements` 建卡，project 填本项目目录名，notes 记录当时讨论的方案要点。若本项目已接入自动开发（在看板 projects.json 中），必须传 `status: "later"`——open 状态的卡会被调度器捞走立即开发。
> 2. **开始开发标进行中**：开始做看板上已有的需求时，查询匹配后 `PATCH` 状态为 `doing`。
> 3. **完成标已完成**：完成功能开发后，调用 `GET /api/requirements?project=<本项目目录名>&status=open,doing`，按标题语义匹配刚完成的需求，`PATCH` 状态为 `done`；匹配不确定时必须先询问用户，不得猜。
> 4. **改状态带来源标识（#375）**：AI agent 调用 `PATCH /api/requirements/<id>` 变更状态时带请求头 `X-Actor: agent:<来源标识>`（来源标识如 `<仓库目录名>:<需求号>` 或会话名），看板 `req_status_log` 审计链据此定位变更来源；不带头则落 `api` 桶（User-Agent 兜底识别）。
> 5. **写卡 notes 只写要做什么（#409）**：建卡或改 notes 时只写需求本身（祈使句 + 事实陈述），不写讨论过程、决策痕迹（「待用户确认」「已确认不做」）、被否决方案的理由、commit 号等历史信息；范围排除用「不提供：X、Y」，实现雷区用事实 + 证据。完整规范见 req-board 仓 AGENTS.md「需求卡 notes 编写规范」。

## 待办/暂缓事项

（本项目自身的暂缓需求也记录在自己的看板上，project = `req-board`）
