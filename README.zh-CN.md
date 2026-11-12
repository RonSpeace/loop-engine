<div align="center">

# Loop Engine

**便宜模型 + 严格闸门 = 稳定产出。**
一块需求看板：把需求卡变成评审过、测过、合进主干的代码，不用你盯着。

[English](README.md) · [简体中文](README.zh-CN.md)

[![CI](https://github.com/RonSpeace/loop-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/RonSpeace/loop-engine/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)

</div>

<!-- SCREENSHOT: 真实项目的看板总览（待补） -->

---

Loop Engine 是一个对话驱动的闭环：**需求 → 开发 → 评审 → 验收 → 合并**。
你在聊天里说清要做什么，它整理成一张卡，后面交给一条 agent 流水线。
每张卡有自己的 git worktree，所有关卡通过才合并。

> Loop Engine 用 Loop Engine 开发。本仓库的大部分改动，是它自己的流水线分析、实现、审查并合并的。

## 为什么

单个编码 agent 是自己给自己判卷。换更强的模型能好一点，但贵，出了错也不吭声。Loop Engine 走另一条路：

- **靠闸门，不靠自觉。** 独立的只读评审角色审方案、审 diff，可以打回；打回的活自动重做。
  验收角色在独立测试实例上跑真实链路。
- **出问题就停（fail-closed）。** 缺评审结论、配置非法、收尾校验不过，卡片一律停下等你，不合半成品。
- **便宜模型就够。** 每一步都有人把关，大部分活交给便宜模型。多次打回后可以给作者换更强的模型，再不行转给你。
- **无人值守。** 调度器自己捞卡、跑流程、合并、发通知。只有「待你处理」亮了才需要你。
- **适配你的项目。** 流程、角色、提示词都是配置；每个仓库可以带自己的规则、指引和技能。

## 30 秒看懂工作方式

[![Loop Engine 应用架构](docs/diagrams/loop-architecture.svg)](docs/diagrams/loop-architecture.svg)

1. **看板**（`board.py`，Flask + SQLite）：需求卡、聊天、实时会话、设置。只在本机 8899 端口。
2. **调度器**（`dispatcher.py`）：每 15 秒捞一次「待开发」的卡，建 worktree，按卡上的流程逐个节点推进，
   校验结果，合并，发通知。
3. **ACP 客户端**（`acp_shared.py`）：通过 [Agent Client Protocol](https://agentclientprotocol.com)
   把 agent 拉成子进程，每次工具调用都按角色审批。
4. **上下文编译器**：按层拼出每次的提示词——引擎规则、角色模板、项目约定、技能，最后是这张卡本身。

一张卡从建卡到合并：

[![一张卡的调用链路](docs/diagrams/loop-dispatch-flow.svg)](docs/diagrams/loop-dispatch-flow.svg)

点图查看大图。交互版（可缩放、搜索、切换明暗）在 [`docs/diagrams/`](docs/diagrams/)：本地打开对应 `.html`，或看板「操作指南」里的图表页。

## 快速开始

macOS 或 Linux 上一条命令：

```bash
curl -fsSL https://raw.githubusercontent.com/RonSpeace/loop-engine/main/scripts/install.sh | bash
```

它会检查 git、Python 3.11+ 和 Node.js，把 Loop Engine 下载到 `~/loop-engine`，建好 Python 环境（系统 Python 不行时
自动改用 [uv](https://docs.astral.sh/uv/)），安装 [OpenCode](https://opencode.ai)，并添加 `loop` 命令。之后
`loop init` 带你走完剩下的步骤：

1. **选模型服务商：** DeepSeek、智谱 GLM、通义千问、Moonshot Kimi、OpenRouter、OpenAI，或任何 OpenAI 兼容接口。
2. **粘贴 API Key。** 当场验证 Key 和你选的模型（多数角色用便宜模型，开发角色可以另选一个更强的）。Key 只存在
   `.env` 里，只有你自己能读。
3. **选项目：** 一个小的演示项目，或你已有的 git 仓库。
4. **自检、启动、建第一张卡。** 演示项目上，agent 会把这张卡走完分析、开发、评审、验收，然后合并。打开
   <http://127.0.0.1:8899> 就能看着它跑。

日常使用：

```bash
loop start     # 后台启动看板 + 调度器
loop status    # 是否在运行、几张卡在开发
loop logs      # 跟踪日志
loop stop      # 停止（有 agent 在干活时拒绝，除非加 --force）
loop doctor    # 环境自检
loop init      # 重新配置：换服务商、模型或项目
```

想手动装？克隆仓库、建 `.venv`、`pip install -r requirements.txt`，然后运行
`.venv/bin/python scripts/loop_init.py`。完整步骤——其它运行体（kimi、Claude Code、Codex）、MCP 工具、排障——见
**[docs/quickstart.md](docs/quickstart.md)**。

**Docker：**

```bash
mkdir -p config && cp projects.example.json config/projects.json   # paths 填 LOOP_REPOS 下的路径
LOOP_UID=$(id -u) LOOP_GID=$(id -g) LOOP_REPOS=$HOME/code docker compose up --build
```

一个容器跑两个进程；数据库放在命名卷里，看板只发布到 `127.0.0.1:8899`。按你用的运行体，取消注释对应的登录目录挂载
（`~/.kimi-code-deepseek`、`~/.claude`、`~/.codex`）。

**Linux：** 用 `scripts/run_local.sh`（launchd 与 `start.sh` 仅限 macOS）。日志默认在
`~/.local/state/loop-engine/logs`（`REQ_BOARD_LOG_DIR` 可改）。需要常驻时用 systemd 用户服务包一层，或用 Docker。

> **安全提示：** 看板**没有登录鉴权**，能访问它的人就能改卡、让 agent 改你的代码。默认只监听
> `127.0.0.1`，不要把它暴露到 `0.0.0.0` 或不可信网络。

## 核心概念

| 概念 | 含义 |
|---|---|
| **需求卡** | 一条需求。状态：`later` 暂缓 → `open` 待开发 → `doing` 进行中 → `done` 已完成，或 `dropped` 已废弃。调度器只捞 `open`。 |
| **流程** | 卡片要走的节点序列。快速 `quick`：开发 → 收尾；标准 `standard`：开发 → 代码审查 → 验收 → 收尾；复杂 `heavy`：技术方案 → 方案评审 → 开发 → 代码审查 → 验收 → 收尾。由需求分析推荐。 |
| **角色** | 一类 agent 工作：`analysis`、`tech`、`tech_review`、`dev`、`code_review`、`accept`，外加处理合并冲突的 `merge_resolve`。评审只读；只有 `dev` 和 `merge_resolve` 改代码。 |
| **闸门** | 能让卡停下的检查：按角色的工具审批、评审结论（`pass` / `reject`）、收尾校验（有代码变更或有产物）、需要你拍板的提问（`need_confirm`）。 |
| **运行体** | 实际跑角色的 agent CLI：`kimi`、`claude-code`、`codex`、`opencode`，可按项目、按角色选。 |

流程和角色写在 `flows.json` 与 `prompts/*.md`，像改其它配置一样改。

## 看板怎么用

- **建卡。** 点「+ 新建」（或按 <kbd>N</kbd>），在聊天里描述需求；agent 整理出标题和 notes，你确认后落卡。
  聊天用不了时，展开「直接填表」建卡。新卡默认「暂缓」，改成「待开发」才会开工。
- **看进度条。** 运行中的卡片顶部有一条按流程分段的细线，当前段在呼吸。**橙色只表示「需要你决定」。**
- **清空「待你处理」。** 页头的「待你处理」列出所有等你的卡：agent 的提问、评审反复不过、收尾校验需要裁决、合并受阻。点一行直接打开那张卡。
- **看过程、插话。** 点开会话可以实时看消息、工具调用和 diff；在会话下方的聊天框发消息，就能给 agent 补充指示。
- **看 agent 看到了什么。** 卡片详情的「上下文」页签列出每一轮实际注入的提示词分层。
- **页头角标。** 只回答一个问题：引擎在不在跑。飞书是可选渠道，没配置不会变红。
- **手机。** 窄屏自动切成单栏。

键盘快捷键（光标不在输入框时）：

| 按键 | 作用 |
|---|---|
| <kbd>N</kbd> | 新建需求 |
| <kbd>/</kbd> | 搜索 |
| <kbd>J</kbd> / <kbd>K</kbd> | 下一张 / 上一张卡 |
| <kbd>Enter</kbd> | 打开当前卡 |
| <kbd>Esc</kbd> | 关闭抽屉 |
| <kbd>?</kbd> | 快捷键帮助 |

设置（左下角齿轮）：「全局信息」「模型设置」「通知渠道」（飞书、Webhook、Slack）「提示词」（在本机覆盖引擎提示词）
「技能库」。动态背景默认关闭，可在「全局信息 → 外观」打开。

页头的「操作指南」图标打开站内指南，每个界面都有详细说明。

## 运行体

每个角色跑在一个 ACP 运行体上，可按项目、按角色选：

| 运行体 | 流水线角色 | 建卡与助手 | 说明 |
|---|---|---|---|
| `kimi`（默认） | 全部 | 支持 | kimi-code CLI（`kimi acp`），可接任意 OpenAI / Anthropic / Gemini 兼容模型 |
| `opencode` | 全部 | 支持 | [OpenCode](https://opencode.ai)（`opencode acp`）。可接**任意 OpenAI 兼容接口**（DeepSeek、通义、GLM、自建网关）：设 `LOOP_OPENAI_BASE_URL`、`LOOP_OPENAI_API_KEY`、`LOOP_OPENCODE_MODEL` |
| `claude-code` | 仅 dev | 不支持 | 经 `@zed-industries/claude-code-acp`，使用已登录的 `claude` CLI |
| `codex` | 仅 dev | 不支持 | 经 `@zed-industries/codex-acp`，使用已登录的 `codex` CLI |

```json
{"my-project": {"paths": ["/path/to/repo"], "runtimes": {"dev": "claude-code"}}}
```

选择顺序：`projects.json` 的 `<项目>.runtimes.<角色>` → `<项目>.runtime` → `flows.json` 的
`roles.<角色>.runtime` → 环境变量 `LOOP_AGENT_RUNTIME` → `kimi`。未知运行体在启动前就被拒绝。

只读角色、建卡会话和看板助手只能跑在通过**运行体认证套件**
（[`tests/test_runtime_certification.py`](tests/test_runtime_certification.py)）的运行体上：套件用真实运行体
配一个脚本化的假模型，让它尝试写文件、跑命令、读项目外的文件、开子 agent，每一次都必须被拦下。
Kimi 和 OpenCode 已通过；Claude Code 和 Codex 通过前只开放 dev。当前运行体起不了聊天时，看板会提示你去改哪里。

配置细节见 [快速开始 → 运行体](docs/quickstart.md#agent-runtimes运行体选择)。

## 愿景与路线图

**我们押注：可靠性应该来自流程，而不是来自模型的价格。**

工厂变得可靠，不是靠招天才，而是靠工位、质检和标准。Loop Engine 把这套做法带进软件开发：很多个便宜的
agent，每个只做一件事，每一步都有另一个来检查，没有任何东西靠信任合并。

**最终图景：** 一个开发者带着一整支 agent 软件团队；十个人的公司，交付能力像一百人的公司，花的是 token，
不是人头。你只说清楚要什么，引擎负责规划、开发、评审、测试、合并，并说明它做了什么。只有真正需要你
拍板的事，才会来找你。

**北极星指标：** *每一次经过验证、成功合并的改动花了多少钱*，以及*人需要介入几次*。
我们要把两者都压向零，并且公开度量。

### 第一阶段 · 让人敢放手（现在）

一个人、一台机器、一个仓库，无人值守地稳定运转。

- [x] 可插拔运行体：OpenCode 支持全部角色，Claude Code / Codex 支持开发角色
- [x] 运行体认证：一个运行体要先证明自己写不了、执行不了、逃不出去，才能担任评审角色
- [x] 中英文界面、Docker Compose
- [ ] `loop init`：一条命令从零走到第一张合并的卡，内置模型预设
- [x] 英文提示词（`loop init` 选语言，或项目级 `prompt_language`）
- [ ] Claude Code、Codex、Gemini CLI 通过评审角色认证

### 第二阶段 · 拿数据说话（下一步）

这样的主张需要数字来证明。

- [ ] **公开基准测试：** 同一批任务，便宜模型 + 闸门，对比单个顶级模型。比合并成功率、返工次数、
      每次改动成本和人工介入次数；可复现，每个版本都重跑。
- [ ] 实时自举计数：本仓库有多少是它自己的流水线写的
- [ ] 可分享的需求回放：一个链接看完一张卡的一生，包括每条提示词、每次评审、每次打回和每份 diff

### 第三阶段 · 走进团队（之后）

在团队已经在用的地方工作。

- [ ] GitHub / GitLab Issues ⇄ 需求卡；PR 模式（开一个评审过的 PR，而不是直接合并）
- [ ] 一个看板，管多个仓库、多台执行机器
- [ ] 账号、角色、审计日志和登录，支持团队共享部署
- [ ] 人类评审者作为一等闸门，和 agent 评审者并列

### 第四阶段 · agent 协作的开放标准（远景）

- [ ] **Loop spec：** 一个开放、不绑定运行体的流程、闸门与裁决格式，任何 agent 工具都能接入
- [ ] 流程与技能市场：分享和复用经过验证的流程，比如「经安全评审的后端改动」「先演练再执行的数据迁移」
- [ ] 会学习的闭环：打回和失败自动回流到提示词、技能和模型路由
- [ ] 常驻维护：依赖升级、不稳定测试、文档过期，都作为长期卡片持续处理

想决定先做哪一块？[提个 issue](https://github.com/RonSpeace/loop-engine/issues)，告诉我们哪个阶段对你最重要。

## 文档索引

| 文档 | 用途 |
|---|---|
| [快速开始](docs/quickstart.md) | 安装、模型、第一条闭环、Docker、排障 |
| 站内指南（`/guide`，源文件 [docs/guide.html](docs/guide.html)） | 日常使用看板 |
| [项目清单与提示词分层](docs/project-manifest.md) | 让 agent 按你项目的规则干活（`.loop/`） |
| [调度器详解](docs/dispatcher.md) | 调度、闸门、合并、通知 |
| [REST API](docs/api.md) · [数据模型](docs/data-model.md) | 和看板集成 |
| [图](docs/diagrams/) | 应用架构与调用链路交互图（源文件在 `src/`） |
| [AGENTS.md](AGENTS.md) | 贡献规则——也是 agent 开发本仓库时遵守的规则 |

## 贡献

欢迎提 issue 和 PR，请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

项目往哪走，见 [愿景与路线图](#愿景与路线图)。

## 许可

[MIT](LICENSE)
