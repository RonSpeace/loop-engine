---
name: req-board-card
description: 在需求看板（req-board，http://127.0.0.1:8899）上为当前或指定项目创建需求卡。当用户说「帮我建卡」「记到需求看板」「建一张 xx 项目的需求卡」「把 xx 记录为暂缓/待办需求」，或要求把本项目讨论的方案、需求、待办登记到 req-board 时使用。
---

# req-board-card

在任意项目目录下，为当前项目在 req-board 创建需求卡（遵循 req-board 的跨项目协作约定）。看板是本机应用（127.0.0.1:8899），未运行时可尝试启动或告知用户后正常结束，不要报错。

## 工作流

1. **确认看板可用**：请求 `GET {REQ_BOARD_URL}/api/projects`（默认 `http://127.0.0.1:8899`，可用环境变量 `REQ_BOARD_URL` 覆盖，例如 worktree 测试实例）。
2. **确定项目名**：默认取当前工作目录名（`basename "$(pwd)"`）；用户指定其它项目时以用户为准，建卡前用 `GET /api/projects` 确认名称精确匹配。**目录名不在项目清单中时（如多仓库容器目录 `vip/`）不得按目录名硬猜项目**：列出候选项，并按讨论内容的主体代码域（本次改动涉及哪些仓库/服务）推荐项目，询问用户确认后再建卡。
3. **确定状态**（原则：**建卡不直接开发**——默认 `later`，只有用户明确要求开发才 `open`）：
   - **先判断是否「已实现/已完成」类需求**：用户描述含「已经做了/已实现/已完成/已提交 main/已上线」等信号时，**必须先向用户确认建卡用途，不得直接建卡**：
     - 留档/复盘 → `status=done`（已完成留档；notes 首行写 `[已实现] 实现已由人工提交 main（<commit>），建卡目的：留档`）。done 是终态，不会被调度器捞。
     - 还要安排后续（验收/补流程等）→ 仍是 `later`；**已实现的需求禁止用 `open`**——实现已在 main 时 worktree 基线=实现提交，dev 报 done 必判「无代码变更」而失败（#395/397/398 现场教训）。除非用户明确要求「开始开发」才 `open` 并告知风险。
     - 不需要建卡 → 不建，结束流程。
   - 未检测到「已实现」信号：默认 `later`（暂缓，只记录，调度器不会开发）；仅当用户明确要求「开始开发/安排开发」时才用 `open`。
   - **「建卡 / 记到看板 / 走调度器流程」只定路径、不定时机**：这类措辞的意思是「别绕开调度器直接改 main」，**不是**「现在就开发」，因此不构成开发指令 —— 一律 `later`。要用 `open` 必须能引用用户明确要求开发的**原话**（脚本 `--user-request`）；引不出来就说明本来没有该指令，按 `later` 建并告知用户「已按暂缓建卡，要开发说一声」（建卡 #423 时的现场教训：把「建卡走流程」读成开发指令，卡被置 open，用户手动改回 later）。
   - 用 `GET /api/auto-projects` 判断项目是否接入自动开发：已接入且未获明确开发指令时，必须显式传 `later`——API 缺省是 `open`，会被调度器立刻捞走开发。代价是时敏的：**下一轮轮询（15s 级）即建 worktree、起 agent 会话**，此时再改回 `later` 属中途变更、需处理残留。脚本对「自动开发项目 + `open`」要求 `--user-request`，缺失直接非 0 退出、不建卡。
4. **建卡**：`POST /api/requirements`，JSON body `{"title": "...", "project": "<项目名>", "status": "later|open|done", "notes": "..."}`。`title` 用户没给就按讨论内容概括；`notes` 记录背景 + 方案要点 + 验收标准 + 生效方式，用户没提供就记录当前讨论结论；不传 `mode`/`phase`。**done 卡**（已实现留档）notes 首行写 `[已实现] 实现已由人工提交 main（<commit>），建卡目的：留档`。**生效方式栏**：涉及长驻进程/服务类改动（需重启/部署才生效）时，标注是否需重启/部署、重启对象、谁触发；无法确定写「待方案确认」；不涉及可写「无需额外操作」。**外部来源链接**：需求来源于 TAPD 等外部系统时，notes 背景段必须带完整可点击链接（如 `https://www.tapd.cn/<workspace>/prong/stories/view/<id>`），不允许只写 ID 或编号，方便评审与回溯。**notes 排版提示**：用空行分段（背景/方案要点/验收标准/生效方式各一段），要点用 `- ` / `1. ` 列表或 ① ② ③ 分行；看板详情会自动渲染成段落/列表，超长单行会挤成一坨。**建卡来源元数据（#303/#350）**：脚本自动上报建卡来源/模型/推理强度——DeepSeek Harness 环境（`DSH_SESSION_JSONL` / `DSH_SESSION_ID` / `DSH_HOME` **任一存在即认定**）上报 `source=dsh` + `source_session_id`，能读到会话文件时再补会话模型/推理强度；**只命中环境、读不到会话文件时 model 留空且不回退 kimi-code 探测**（不猜模型、不误标来源）；**kimi-code 等非 DSH harness 建卡由脚本自动探测**：读 `~/.kimi-code/config.toml`（`KIMI_CONFIG` 可覆盖）的 `default_model`，取其模型段 `display_name` 作为 `--model` 上报（`source=kimi-code`，如 `DeepSeek V4 Flash Vision Exp`），**无需手传 `--source/--model`**；`--model` 取值与评审记录一致——底层大模型名，**不写 agent 壳名**；显式传 `--source/--model/--reasoning-effort` 仍逐字段优先；探测不到（无配置/无法解析）时才省略 `--model`，不得编造；非 harness 环境且未显式指定参数时不上报，行为与现状一致。
5. **验证汇报**：`GET /api/requirements?project=<项目名>&status=<状态>` 按标题找到刚建的卡，把卡片 id 和状态报告给用户。

## 推荐脚本

优先运行共享目录下的脚本（它固化了上面的状态与校验规则）：

```bash
python3 ~/.agents/skills/req-board-card/scripts/create_card.py --title "标题" [--project 项目名] [--status later|open|done] [--notes "备注"] [--source dsh|codex|kimi-code|...] [--model 模型名] [--reasoning-effort low|high|max] [--user-request "用户明确要求开发的原话"]
```

脚本默认 `status=later`、`project=当前目录名`，支持 `REQ_BOARD_URL` 覆盖。**自动开发项目 + `--status open` 必须带 `--user-request "<用户原话>"`**（缺失则以退出码 3 失败、不建卡）；非自动开发项目与 `later`/`done` 不受此限。建卡来源元数据（#303/#350）：`DSH_SESSION_JSONL` / `DSH_SESSION_ID` / `DSH_HOME` 任一存在即认定为 DSH 环境，上报 `source=dsh` + `source_session_id`（能解析会话文件时补模型/推理强度，读不到就留空、不回退 kimi-code 探测）；非 DSH 环境才走 **kimi-code 环境自动探测**（读 `~/.kimi-code/config.toml` 的 `default_model.display_name`，如 `DeepSeek V4 Flash Vision Exp`，`KIMI_CONFIG` 可覆盖路径）并上报 `source=kimi-code` + `model`，**无需手传**；`--source/--model/--reasoning-effort` 显式指定可逐字段覆盖；无 DSH 变量、无法探测且未指定参数时不上报（与旧行为一致）。脚本失败时退回手工执行上面工作流。

## 参考

API 细节见 [references/api.md](references/api.md)，仅需要时读取。
