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
| 3 场景技能 | 按角色点名的技能片段 | 项目 `.loop/skills/<名>.md` 优先，其次全局 `prompts/skills/<名>.md`（`scenario_skills_text`） |
| 4 卡片输入 | title、notes、summary、上游产物指路、仓库/分支/产物路径、预算、extra 等 | 角色模板的 `{card_inputs}` 标记之后，由调度器填充 |
| 5 前置契约 | 推荐流程与仓库识别的输出要求，仅前置阶段追加 | `prompts/system/pre-contract.md` |
| 6 进程安全 | 当前真实 PID 对应的进程安全红线，新会话和续跑均追加 | `prompts/system/kill-redline.md`（`append_kill_redline`） |

旧文档中的「三层」只覆盖前三个配置段，不能代指完整执行上下文。没有清单的项目，第 2 层声明「本项目未提供 .loop/project.json」，第 3 层只有全局技能。

`projects.json` 管本机项目接入、路径、分支、模型、预算和自动重启等。`goal` 在当前内置角色模板中由 analysis 消费，不能代替给全体角色的业务 Guide。全局模板与项目清单各司其职，不能靠卡片末尾追加文字改变权限。

## 提示词存储与执行档案

- 引擎文案都是文件：`prompts/system/*.md`（通用约束、只读工具说明、进程红线、前置契约、续跑/收尾/重试文案、无清单兜底）与 `prompts/fragments/*.md`（仅推分支、自动重启、合并冲突等条件片段），条目元数据（层、可用占位符、必需占位符、锁定句、生效条件 `when`）登记在 `prompts/registry.json`；角色模板仍是 `prompts/<角色>.md`，由 `flows.json` 推导出 `role/<角色>` 条目。存取统一走仓库根 `prompt_store.py`。
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

- 新增全局技能：放 `prompts/skills/<名>.md`，在 `flows.json` 的 `roles.<角色>.skills` 里点名（加载期校验文件存在；改 `flows.json` 需重启两个进程）。
- 新增项目技能：放 `<仓库>/.loop/skills/<名>.md`，在清单 `skills.<角色>` 里点名（下次完整构建 Prompt 时现读）。
- 技能名：小写字母、数字、连字符。项目技能与全局技能同名时整份采用项目的，不做内容合并；多仓库同名时按登记顺序取首个匹配。
- 单个技能文件同样最多内联 4000 字符；缺文件目前记录日志后跳过，首次接入须核对挂载。
- 都不需要改 Python。

## AGENTS.md

- 模板：`docs/templates/AGENTS.template.md`（架构概览 / 目录约定 / 编码规范 / 业务红线 / 注意事项）。
- 项目约定层注入的是「先读工作目录仓库根 AGENTS.md」指路，并非全文内联；不要把关键事实只藏在未登记的长文档中。
- 它是高频读取的短规则，所以有长度上限，`scripts/doctor.py` 超限告警。
- 新增一条规则，必须同时删掉或合并一条旧规则，或把场景性内容下沉到 `.loop/guides/`、`.loop/skills/`。
- monorepo 可在子目录另放 AGENTS.md，agent 按改动目录就近优先读。

## 需求、评审与续跑边界

- notes 只写本次目标、范围、事实、验收与生效方式；notes/summary 都进入 Prompt，不能当作评审日志或规则覆盖通道。评审记录存产物或 `req_reviews`，后者永不自动进入 Prompt。
- 当前部分模板仍把 summary 叫作「评审反馈/历史记录」，属于实现遗留。流程内返工由调度器补充上下文传递；人工评审须通过看板会话明确报告路径与待修问题，不能假设仅存一条评审记录就会自动注入。
- `run_agent` 成功恢复旧会话时发送简化续跑文案，不保证把完整项目约定、技能和卡片最新全文重新发送。重启进程或刷新 UI 不能替代上下文更新。
- 关键配置变化后通过看板聊天框交接：列明变更文件、当前基线、已确认目标、剩余任务与验收，要求回读；新会话走看板「新建聊天」，不自行启动 ACP。冲突先报 need_confirm。
- 统一输入信任边界与必需文件缺失阻断仍是后续改进方向，不是当前已有保证；Prompt 版本/hash 记录与注入预览见上文「提示词存储与执行档案」。
