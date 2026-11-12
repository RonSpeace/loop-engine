# 项目清单与 Prompt 注入

每个接入的项目把「怎么装、怎么测、怎么起测试实例、有哪些本项目特有的规矩」写在自己仓库里，调度器读出来拼进 agent 的提示词。全局角色模板（`prompts/*.md`、`COMMON_TAIL`）不携带任何单个项目的专有内容。

## 配置职责与实际拼装

新项目接入的完整操作指引与「成熟 ERP 平台新增模块」示例见看板的 [Prompt 配置指引](http://127.0.0.1:8899/guide#prompts)。项目事实与长期约束写 Guide，复用操作方法写 Skill，单次范围和验收条件写需求卡。不要把项目专有内容写进全局角色模板。

按职责可分为 Policy（通用协议与权限）、Role（角色职责）、Project（项目事实与约定）、Skill（场景方法）、Task（需求输入）、Runtime（运行上下文）。这不是六级已经实现的权限优先级，也不是六个独立系统消息；文字不能覆盖工具权限和安全边界。

当前完整 Prompt 的构建顺序（上下文编译器 `dispatcher_modules/context.py compile_context` → `build_prompt_parts`；`run_agent` 追加红线/续跑文案）：

| 层 | 内容 | 来源 |
|:--|:--|:--|
| 1 固定段 | 角色职责、通用协议、输出风格 | `prompts/<角色>.md` 的固定段 + `prompts/system/common.md` |
| 2 项目约定 | 常用命令、测试实例、按角色过滤的项目指引、AGENTS.md 指路；缺项显式写「未配置」 | `<仓库>/.loop/project.json`（`project_conventions_text`） |
| 3 场景技能 | 按角色挂载的技能正文 | 项目 `.loop/skills/` 优先，其次本机覆盖 `prompts.local/skills/`，最后全局 `prompts/skills/`（`scenario_skills_text`；规则见下文「技能」） |
| 2b 条件片段 | 仅推分支、自动重启等按项目开关生效的说明（稳定前缀，紧跟项目约定） | `prompts/fragments/*.md`，注册表 `when` 选择 |
| 4 卡片输入 | title、notes、summary、上游产物指路、仓库/分支/产物路径、预算、extra、子目录 AGENTS.md 指路、按卡触发的技能等（数据部分带 `<card-data>` 围栏） | 角色模板的 `{card_inputs}` 标记之后，由调度器填充 |
| 5 前置契约 | 推荐流程与仓库识别的输出要求，仅前置阶段追加 | `prompts/system/pre-contract.md` |
| 6 进程安全 | 当前真实 PID 对应的进程安全红线，新会话和续跑均追加 | `prompts/system/kill-redline.md`（`append_kill_redline`） |

旧文档中的「三层」只覆盖前三个配置段，不能代指完整执行上下文。没有清单的项目，第 2 层声明「本项目未提供 .loop/project.json」，第 3 层只有全局技能。

`projects.json` 管本机项目接入、路径、分支、模型、预算和自动重启等。`goal` 在当前内置角色模板中由 analysis 消费，不能代替给全体角色的业务 Guide。全局模板与项目清单各司其职，不能靠卡片末尾追加文字改变权限。

## 提示词存储与执行档案

- 引擎文案都是文件：`prompts/system/*.md`（通用约束、只读工具说明、进程红线、前置契约、续跑/收尾/重试文案、无清单兜底）与 `prompts/fragments/*.md`（仅推分支、自动重启、合并冲突等条件片段），条目元数据（层、可用占位符、必需占位符、锁定句、生效条件 `when`）登记在 `prompts/registry.json`；角色模板仍是 `prompts/<角色>.md`，由 `flows.json` 推导出 `role/<角色>` 条目。存取统一走仓库根 `prompt_store.py`。
- 提示词语言：项目 `projects.json` 的 `prompt_language`（`zh` / `en`）→ 环境变量 `LOOP_PROMPT_LANG` → `zh`（`prompt_i18n.prompt_lang`，每次现读）。英文版与中文版同路径放在 `prompts/en/` 下（角色模板、`system/`、`fragments/`、全局技能 `prompts/en/skills/`），锁定句的英文对应写在注册表 `locked_en`；调度器代码里拼进提示词的短句走 `prompt_i18n.tr`（中文模板即键，英文查 `EN` 表），角色/流程展示名复用界面英文词典。看板「聊天建卡」与卡片对话的英文系统提示词在 `board_modules/services/chat_prompts_en.py`。缺英文版时退回中文并告警，`tests/test_prompt_i18n.py` 保证英文模式下除 `<card-data>` 内的卡片数据外不漏中文。英文项目的本机覆盖放 `prompts.local/en/`。
- 本机覆盖：设置抽屉「提示词」tab（`PUT /api/prompts/<id>`）保存到不进 git 的 `prompts.local/`（目录结构与 `prompts/` 一一对应，`REQ_BOARD_PROMPTS_LOCAL` 可改路径），编译时覆盖优先，按 mtime 现读、无需重启；「恢复默认」= 删除覆盖。覆盖旁存 `.base`（保存时的默认文本），默认升级后条目标「默认已更新」，编译仍用覆盖。保存时校验：未声明占位符、缺必需占位符、删改锁定句（如 result.json 契约行、红线首行、前置契约的 suggested_flow/repos 行）、超 20000 字一律拒绝。
- 执行档案：每次拉起会话（新会话、续跑、无结果续接轮）在 `<产物目录>/runs/<run_key>/` 写 `manifest.json`（注入清单：每项的层、来源、默认/覆盖、hash、字数、入选理由，及跳过项与警告）和 `prompt.txt`（实际发出的全文）；`agent_runs` 记 `manifest_path`/`prompt_hash`/`prefix_hash`。看板 `GET /api/requirements/<id>/context/runs*` 读取，`GET /api/context/preview` 用同一编译器 dry 编译出「某角色在某项目会收到什么」。

## `.loop/project.json`

模板：`docs/templates/project.example.json`。每次构建完整 Prompt 时现读；修改已支持字段和项目指引/技能无需重启，但不代表旧会话 resume 会重新注入。未知字段不会自动生效，非法字段会被忽略并在 `scripts/doctor.py` 告警，当前不保证阻断调度。

| 字段 | 含义 |
|:--|:--|
| `commands.{install,test,test_affected,lint,typecheck,build}` | 命令字符串；空串或缺省 = 未配置 |
| `instance.{start,stop,status,healthcheck}` | 独立测试实例的启停命令；`instance.port_range` = `[起, 止]` |
| `guides.<名>` | 项目指引文件（仓库内相对路径，解析软链后也须在仓库内）；写成 `{"file": ..., "roles": [...]}` 可只给部分角色，省略 roles = 全部角色；单文件进 Prompt 上限 4000 字符，超出尾部截断 |
| `skills.<角色>` | 该角色额外挂载的技能名列表 |
| `high_risk_paths` | 高风险路径前缀：quick 流程的卡改到这些路径（或 `.loop/`、调度器模块）会自动升级为标准流程走代码审查与验收 |
| `agents_md.{max_lines,max_chars}` | 本项目 AGENTS.md 长度上限（默认 150 行 / 12000 字符） |
| `restart`、`visual` | 预留；服务重启配方当前仍以 `projects.json` 的 `restart` 为准 |

文件只放进 `.loop/guides/` 不会自动注入，须登记；Guide 引用的其它文档不会递归加载。关键规则放前面，长资料提供仓库内路径供 Agent 读取。目前只按角色过滤，不按任务关键词选择。所有登记仓库的清单都会参与项目约定构建，不只本卡实际改动的仓库；跨仓库规则应标明适用范围。

当前 `_project_repos` 从 `projects.json.paths` 获取登记仓库路径，清单/Guide/Skill 由这些路径读取，而 Agent 工作在 per-requirement worktree 中。首次开发前先把约定纳入接入基线；仅在本卡 worktree 改文件不会自动改变注入来源，主工作区更新后旧 worktree 也不保证同步。续跑交接须核对二者版本，这仍缺少引擎层统一快照保证。

## 技能

技能是自带元数据的目录 `<名>/SKILL.md`（格式对齐 Anthropic Agent Skills，可附 `references/`、`scripts/` 等文件）；发现、解析、选择、校验在 `acp_modules/skills.py`。

```markdown
---
name: browser-verify            # 与目录名一致；小写字母、数字、连字符
description: 什么时候用这个技能   # 必填，≤200 字，进技能目录
roles: [dev]                    # 自声明挂载的角色，与点名取并集
activation: always              # always | auto | on_demand | manual
paths: ["static/**"]            # auto：改动路径 glob
keywords: [页面, 样式]           # auto：标题 / notes 关键词
required: false                 # true = 缺失或解析失败时停线
owner: frontend
status: active                  # draft | active | deprecated
---
正文
```

- 作用域：项目 `<仓库>/.loop/skills/` > 本机覆盖 `prompts.local/skills/`（本机数据，不入库；环境变量 `REQ_BOARD_PROMPTS_LOCAL` 改根目录）> 全局 `prompts/skills/`。同名整份覆盖、不合并；多仓库同名取登记顺序首个；同一作用域内目录形态优先于平铺。
- 旧平铺 `<名>.md` 继续有效：视为 `activation: always`、`status: active`，角色只取点名处。
- 挂载：`flows.json` 的 `roles.<角色>.skills` 点名 ∪ 清单 `skills.<角色>` 点名 ∪ 技能自身 `roles`。卡片 notes 写 `skills: a, b` 强制加载（含 manual；技能声明了 roles 时只对这些角色生效），`-skills: x` 排除。
- 激活方式：`always` 内联正文；`auto` 改动路径或关键词命中才内联；`on_demand` 只进技能目录（名称 + 描述 + 路径），agent 自己读；`manual` 只在 notes 点名时加载。
- 草稿只在注入预览里可用，不进真实会话；废弃技能照常加载但告警。
- 缺失：角色或清单点名的技能找不到 → 记日志跳过；notes 点名的技能找不到、或 `required: true` 的技能解析失败 → 记为必需缺失。
- 单个技能正文最多内联 4000 字符，超出截断并留全文路径。技能文件每次构建 Prompt 时现读；在 `flows.json` 点名时加载期校验文件存在，改 `flows.json` 需重启两个进程。
- 看板技能库接口（`/api/skills`，见 [api.md](api.md)）只写本机覆盖目录，项目技能在看板上只读。`scripts/doctor.py` 的「技能库」一节逐个校验（名字、description、正文长度、附属文件、全局技能不写单个项目的路径），只告警。

## AGENTS.md

- 模板：`docs/templates/AGENTS.template.md`（架构概览 / 目录约定 / 编码规范 / 业务红线 / 注意事项）。
- 只指路不内联：项目约定层写「先读工作目录仓库根的 AGENTS.md」（`prompts/system/agents-md-pointer.md`）；本卡涉及的子目录若有自己的 AGENTS.md，卡片输入块末尾列出「改 `<目录>/` 下的文件前先读 `<目录>/AGENTS.md`」（`prompts/system/agents-md-nested.md`）。「涉及」与技能 auto 命中同一依据：开发前取 notes 与需求分析/产品方案/技术方案里出现的路径，审查/验收取 diff.patch 的改动文件。
- 读 agent 实际所在的 worktree（注入预览读主工作区）；每份被指到的 AGENTS.md 记为清单 L2 项 `agents-md:<仓库>/<相对路径>`（内容 hash、字数、入选依据）。自带 AGENTS.md 加载的运行体（codex）只记录（`native: true`）不指路；kimi / claude-code 的原生行为本机无法核实，照常指路。
- 优先级：平台约束 > 最近的子目录 AGENTS.md > 仓库根 AGENTS.md > 角色模板（仅限命令、端口、路径等事实）；权限只由工具白名单决定。
- 不要把关键事实只藏在未登记的长文档中。看板 `GET /api/prompts` 以只读条目 `project/<项目>/agents-md/<仓库>/<相对路径>` 列出仓库内全部 AGENTS.md。
- 它是高频读取的短规则，所以有长度上限，`scripts/doctor.py` 对仓库根与每个子目录 AGENTS.md 超限告警；子目录文件与根逐字重复 ≥5 行也告警。
- 新增一条规则，必须同时删掉或合并一条旧规则，或把场景性内容下沉到 `.loop/guides/`、`.loop/skills/`。
- monorepo 可在子目录另放 AGENTS.md，agent 按改动目录就近优先读。

## 需求、评审与续跑边界

- notes 只写本次目标、范围、事实、验收与生效方式；notes/summary 都进入 Prompt，不能当作评审日志或规则覆盖通道。评审记录存产物或 `req_reviews`，后者永不自动进入 Prompt。
- 评审打回：评审角色在 result.json 写 `issues`（`[{id, severity, where, what}]`），返工会话的卡片输入只收到「返工输入：未关闭问题」清单 + 评审产物路径（`prompts/system/rework-issues.md`），作者在自己的 result.json 用 `issue_responses` 逐条回应；未带 issues 的打回照常执行，返工输入回退为意见摘要 + 产物全文并留痕。问题来自评审 result.json，不读 `req_reviews`。模板里「评审反馈」对 summary 的引用暂保留（给人看的留档）。
- 数据围栏：卡片输入块里的标题、notes、summary、上游产物指路、返工问题清单用 `<card-data source="…">…</card-data>` 包住，`prompts/system/common.md` 第 9 条声明块内是数据不是指令。
- 自动续跑：恢复旧会话前把本次编译清单与该会话上次的清单（`runs/*/manifest.json`，按 session_id 匹配）逐项对比——无变化发简化续跑文案；L2–L6（项目约定、条件片段、技能、卡片数据）有变化时续跑消息前置「上下文更新」（逐条附新正文，要求回读）；L0/L1（平台策略、角色模板）有变化时不续用旧会话，开新会话并附交接说明。清单里记 `context_update`。档案上线前的存量会话与看板人工续跑不做对比，仍按简化文案。
- 关键配置变化后通过看板聊天框交接：列明变更文件、当前基线、已确认目标、剩余任务与验收，要求回读；新会话走看板「新建聊天」，不自行启动 ACP。冲突先报 need_confirm。
- 统一输入信任边界与必需文件缺失阻断仍是后续改进方向，不是当前已有保证；Prompt 版本/hash 记录与注入预览见上文「提示词存储与执行档案」。
