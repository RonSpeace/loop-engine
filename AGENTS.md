# 需求看板 / Loop Engine（req-board）

对话驱动的「需求 → 开发 → 测试」闭环引擎：看板记录各项目需求卡，调度器按卡上的流程拉起 agent 会话开发。本仓库即系统自身，用自己的流程迭代自己。

各条款的背景、判据细节与事件来源见 [docs/agents-detail.md](docs/agents-detail.md)；怎么测、怎么起测试实例、长驻进程清单见 `.loop/project.json` 与 `.loop/guides/`（会自动进提示词的「项目约定」层）。

## 架构概览

- 后端 Flask + SQLite，看板入口 `board.py`（后端共享层在 `board_modules/`，前端页面在 `templates/board.html`，CSS 在 `static/board.css`；JS 源码分片在 `static/board/*.js`，由 `scripts/build_board_js.py` 拼成实际加载的 `static/board.js`——改分片后必须重新生成），只监听 `127.0.0.1:8899`，无鉴权。
- 调度器 `dispatcher.py` 只留配置、状态与主循环，各功能段拆在 `dispatcher_modules/`（经 `runtime.D` 晚绑定读写 dispatcher 上的状态与可补丁名，测试照旧改写 `dispatcher.X`；契约见 `tests/test_dispatcher_split_contract.py`）。调度器作为 ACP 客户端驱动 agent 子进程（默认 `kimi acp`，运行体注册表 `acp_shared.AGENT_RUNTIMES`）；`acp_shared.py` 是会话、角色能力的共享层，审批策略与只读工具拆在 `acp_modules/`（`limits` / `command_policy` / `readonly_tools`），名字经 `acp_shared` 显式回挂——测试替换实现要改定义所在模块。
- 流程是配置：`flows.json`（前置环节 + 流程节点 + 角色展示/模型/技能）+ `prompts/*.md`；项目清单 `projects.json`（本地数据，不进 git）。
- 两个进程都由 launchd 托管（plist 模板在 `plists/*.plist.tmpl`，`scripts/install-launchd.sh` 按本机路径渲染到 `~/Library/LaunchAgents/`；入口 `scripts/launchd-run.sh`，日志在 `~/Library/Logs/req-board/`）。
- 表结构见 [docs/data-model.md](docs/data-model.md)，REST 端点见 [docs/api.md](docs/api.md)，调度细节见 [docs/dispatcher.md](docs/dispatcher.md)，项目清单与提示词三层见 [docs/project-manifest.md](docs/project-manifest.md)。

## 目录约定

- `prompts/`：角色提示词模板；`prompts/skills/`：全局场景技能片段。全局模板不写任何单个项目的专有内容。
- `.loop/`：本项目自己的清单、指引、技能。
- `scripts/`：运维脚本（`doctor.py`、`test_instance.sh`、`dispatcher-restart.sh` 等）；`tests/`：测试；`docs/`：文档与模板。
- `board_modules/`：看板后端共享层（`runtime.py` 兼容桥、`db.py`、`common.py`、`sse.py`），路由蓝图在 `board_modules/blueprints/`（`requirements` / `projects` / `chats` / `settings` / `assistant` / `sessions` / `meta` / `context` 八个资源蓝图，`board.py` 只按 `ALL` 注册），服务层在 `board_modules/services/`（`model_pool` / `harness_config` / `projects` / `requirements` / `session_runtime` / `presentation` / `dispatcher_ops` / `chat_runtime` / `mcp_servers` / `reqchat_runtime` / `assistant_card` / `assistant_runtime` / `assistant_mcp` / `project_git` / `context_preview`）；入口 `board.py` 经晚绑定兼容桥消费——测试在 `board` 模块上改写的名字必须仍有效。
- `data/`、`projects.json`、`feishu.json`、`.env` 是本地数据，已 gitignore，不提交。
- agent 产物统一在 `~/.req-board/artifacts/<项目>/req-<id>/`，worktree 在 `~/.req-board/worktrees/`。

## 编码规范

- 与用户交流、产出文档、代码注释、commit message 默认用中文；代码标识符、API 路径、字段名、命令保留原文。
- 术语对照：done/doing/open/later = 已完成/进行中/待开发/暂缓；analysis/proposal/tech/dev/accept = 需求分析/方案/技术设计/开发/验收；tech_review/code_review = 方案评审/代码审查；流程 quick/standard/heavy = 快速/标准/复杂（显示名取自 `flows.json`）。
- **Git 提交约定（预先授权）**：完成一处改动并验证通过后直接 `git commit`，message 简述改动、风格参考 git log；只提交本次相关的代码文件；`push`、`reset`、`rebase` 等改写历史或触达远程的操作仍需用户明确要求（agent 可 `git fetch` 只读远端引用；远端同步与合并/推送由调度器按项目 `git_sync` 配置在收尾执行）。
- 展示名、流程名、角色名一律由 `flows.json` 派生，禁止在代码里手写副本；角色能力（审批/写白名单/Bash 策略）单一源是 `acp_shared.ROLE_CAPABILITIES`。新增角色 = 改这两处 + 新增 `prompts/<角色>.md`。
- 色值只落在 `static/board.css` 的令牌块，组件规则经 `var()` 消费，不写裸色值；右栏三处会话状态行共用 `.chat-status` 一套实现，不按视图分叉样式。
- 需求编号：写 `#N` 前必须先建卡取号，禁止预判号；外部编号写成 `upstream#351`、`tapd#12345`，不裸写。

## 业务红线

- **数据保护**：`data/board.db` 是真实需求数据，任何时候不能删除；改表结构只用迁移（ALTER TABLE），不能删表重建；测试用假数据，测完清理。
- **prompt 通道**：需求的 `notes`、`summary` 会内联进所有角色的提示词。评审过程、评审结论、决策交锋禁止写进这两个字段，只能进 `req_reviews` 表或产物目录；`req_reviews` 永不进 prompt。
- **notes 只写要做什么**：祈使句 + 事实陈述；不写讨论过程、「待确认/已确认不做」、被否决方案的理由、commit 号；范围排除写「不提供：X、Y」，实现雷区写事实 + 证据。
- **建卡与直接开发互斥**：用户说「直接开发」就在 main 工作区改并提交，不补建卡；已建卡的需求走调度器流程，不绕开调度器直接实现。建卡默认 `later`（暂缓），只有用户明确说「开始开发」才建 `open`。拿不准先问一句。人工续跑会话一律走看板聊天框，不自己拉会话。
- **进程安全**：agent 禁止 kill/killpg/pkill 自己的 ACP 驱动进程、其进程组及任何 kimi 进程；禁止自行创建或驱动 ACP 会话（import dispatcher 调 run_agent/spawn_agent，或直接运行 `kimi acp`）；清理测试实例只用 `scripts/test_instance.sh`。停调度器前先确认没有在跑的 agent。
- **写边界**：agent 在各仓库的 per-requirement worktree 内开发（分支 `f-YYYYMMDD-<id>`），不 push、不切分支、不动 main 工作区（可 `git fetch` 查看远端；合并/推送由调度器收尾执行）；写工具只能写 worktree、产物目录、临时目录，目标代码不在 worktree 内时报 need_confirm。
- **状态由调度器管理**：agent 不调看板 API 改需求状态；合并分支由调度器自动执行，不要声明。其它项目的 agent 改状态时带请求头 `X-Actor: agent:<来源标识>`。
- **fail-closed**：配置非法、评审结论缺失、收尾校验不过、角色未知时一律不推进、不直通合并，只留痕等人处理。

## 注意事项

- **结果契约**：结束前把 `{"status": "done|blocked|need_confirm", "summary", "question"}` 写入产物目录 `result.json`，用脚本或 `json.dumps` 生成，字符串内换行写 `\n`。可选 `todos`（需人工处理的遗留待办，如「重启 xxx 服务并浏览器验收」，仅展示不自动执行；合并分支由调度器自动执行，不要声明）、`waive_gate`（已完成但收尾校验预计不过时申报豁免，转人工裁决）。dev 报 done 须有实际代码变更，只读角色须有对应产物。
- **验证约定**：验证强度按耦合面定，不按改动行数定——先 grep 出谁会读到这个改动，跑这个最小集；改动落在 `board.py`/`dispatcher.py`/`acp_shared.py` 的行为路径或耦合面说不清时跑全量。纯注释改动也要跑「会解析该文件源码的测试」。长耗时验证一律前台运行并等到结果，禁止挂成后台任务后空轮询。结束回合前确认：没有后台任务、关键验证已前台跑完、`result.json` 已写入并可解析。重试/续跑场景先确认改动完好、无残留 `result.json`。全量里出现的失败先判因果（缺环境变量、已知不稳定用例），确认无关后一句话记录、不扩大排查。测试里走到 `build_prompt`/`spawn_agent` 的必须隔离，否则会真建 worktree（巡检用 `scripts/test_isolation_audit.py`）。
- **浏览器级验证约定**：diff 涉及浏览器端行为（内嵌 HTML/JS/CSS、fetch 请求头、表单交互、页面渲染）时，单元测试、curl、JS 语法检查都不构成浏览器级验证。dev 与 accept 同样适用：dev 会话已放行浏览器工具前缀（实际工具取决于本机是否注入 playwright MCP，没有时用项目自带 Playwright/Chrome CDP），浏览器端改动必须起测试实例真开浏览器走主路径，并在 self-test.md 单列「浏览器验证」小节；accept 能拉起被测进程的必须在独立实例上跑真实链路。降级出口：工具确实不可用或实例拉不起被测进程时才可降级并写明原因，dev 写「未做，需人工验收」并声明 `todos`，accept 把人工验收项写进「未覆盖项」。
- **时间感知**：单次运行默认上限 240 分钟，需求 notes 可写 `timeout: <分钟>` 覆盖（15~480）。用 `date` 关注耗时，约剩 5 分钟时先 commit 当前进度（dev）再写 `result.json` 保底。失败原因（超时、进程异常、非法 JSON、预算截停等）会注入重试会话，先读原因自查再继续。
- **会话预算**：每轮工具调用步数有硬上限（dev 1000，accept 和 code_review 500，merge_resolve 200，其余 400，可在 projects.json 的 `budgets` 调），到上限会被截停并只给一轮限步收尾，接近上限前先写保底结果。
- **流程**：前置环节（需求分析）产出 `suggested_flow` 与 `repos`；内置三条流程 quick（dev → 收尾，仅限文案/配置类极小改动，改到 `.loop/project.json` 声明的高风险路径会自动升级为 standard）、standard（dev → code_review → accept）、heavy（tech → tech_review → dev → code_review → accept）；没有合法推荐时兜底走 `default_flow` = heavy。需求 notes 可写 `repos: <仓库目录名, 逗号分隔>` 覆盖分析识别的仓库清单。退役一条流程前先冻结引用它的历史卡 `flow_name`。评审第 1–3 次打回自动重做（第 3 次时若项目配了 `escalation_model` 先升级作者模型），第 4 次转人工。projects.json 的 `external_roles`（如 `["tech"]`）声明由外部主 Agent 负责的角色：流程走到或打回到这些角色时调度器不拉会话、只把卡停成暂缓，外部写好产物后把卡指向下一节点并改回待开发即可继续。
- **生效方式必须写明**：涉及长驻进程的改动，在建卡 notes、分析/方案的证据清单、dev 完成总结里显式写是否需重启、重启对象、谁触发；无法确定写「待方案确认」。`board.py`（含 `board_modules/`）合并后由调度器自动重启；`dispatcher.py`/`acp_shared.py`（含 `dispatcher_modules/`、`acp_modules/`、`notifiers/`）合并后调度器自动重启自身；改 `flows.json`、`prompts/` 无热加载，需重启两个进程（`./start.sh` 与 `scripts/dispatcher-restart.sh <pid> config-change`）。人工在 main 工作区改 `board.py` 后必须 `./start.sh` 并 curl 验证 200；改了 `plists/` 后跑一次 `scripts/install-launchd.sh`；重启调度器走脚本，裸 `kill` 仅应急。
- **模型与思考档位**：角色模型走四层兜底链（projects.json 项目级 → flows.json 角色级 → 环境变量 → 全局）；思考档位统一模型默认（不请求、不配置、不展示）。
- **飞书**：need_confirm 推送后回复 `y <编号>` 同意、`n <编号> <原因>` 否决。改动 `on_message` 的 y/n 处理逻辑时，必须同步更新 [docs/guide.html](docs/guide.html) 的「飞书回复与状态流转」表。
- **本机环境**：仓库在 `~/Documents` 下，launchd 进程要读它须给 `/bin/bash` 授予「完全磁盘访问权限」；看板访问日志默认关闭，排查请求时用 `REQ_BOARD_ACCESS_LOG=1 ./start.sh`。
- **ACP 协议面**：手写客户端收发的方法名由 `tests/test_acp_schema_contract.py` 锁定，改方法面必须同步该测试。

## 快速参考

| 操作 | 命令 |
|:---|:---|
| 启动 / 重启看板 | `./start.sh` |
| 重启调度器 | `scripts/dispatcher-restart.sh <pid> [来源]` |
| 安装 / 修复 launchd 托管 | `scripts/install-launchd.sh` |
| 环境自检（含项目清单与 AGENTS.md 长度） | `.venv/bin/python scripts/doctor.py`（通过 = 退出码 0 且输出「硬失败 0 项」） |
| 查看日志 | `tail -f ~/Library/Logs/req-board/req-board.log`（调度器：`req-board-dispatcher.err.log`） |
| 验证运行 | `curl http://127.0.0.1:8899` |

## 跨项目协作约定（供其它项目的 AGENTS.md 引用）

> **需求看板协作约定**
>
> 本项目的需求看板运行在 `http://127.0.0.1:8899`（若未运行可跳过，不要报错）。
>
> 1. **暂缓需求建卡**：讨论后决定暂缓的需求，调用 `POST /api/requirements` 建卡，project 填本项目目录名，notes 记录方案要点。本项目已接入自动开发时必须传 `status: "later"`——open 状态的卡会被调度器捞走立即开发。
> 2. **开始开发标进行中**：开始做看板上已有的需求时，查询匹配后 `PATCH` 状态为 `doing`。
> 3. **完成标已完成**：完成后调用 `GET /api/requirements?project=<本项目目录名>&status=open,doing`，按标题语义匹配，`PATCH` 状态为 `done`；匹配不确定时先问用户，不得猜。
> 4. **改状态带来源标识**：`PATCH /api/requirements/<id>` 变更状态时带请求头 `X-Actor: agent:<来源标识>`。
> 5. **写卡 notes 只写要做什么**：只写需求本身，不写讨论过程、决策痕迹、被否决方案的理由、commit 号；范围排除用「不提供：X、Y」。

<!-- 维护约定：本文件常驻每个 agent 的上下文，上限 150 行 / 12000 字符（scripts/doctor.py 检查）。
新增规则须同时删除或合并一条旧规则，或把场景性内容下沉到 .loop/guides、.loop/skills；背景与事件经过写进 docs/agents-detail.md。 -->
