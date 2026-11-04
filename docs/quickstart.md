# 快速开始

[English](quickstart.en.md) · 简体中文

本文带你在一台新机器上把 Loop Engine 跑起来，并用一个玩具项目走完一次完整闭环：
建卡 → 分析 → 开发 → 代码审查 → 验收 → 自动合并。

- 支持 macOS 与 Linux。前台直跑（`scripts/run_local.sh`）两者通用；launchd 托管仅 macOS。
- 改了配置或提示词后没有热加载，需要重启看板和调度器两个进程。
- 在两台机器间同步引擎代码、各自保留运行数据，见 [two-machine-sync.md](two-machine-sync.md)。

## 0. 需要准备什么

```
你的机器
├─ Python 3.11+（建议 .venv）      ← 看板 board.py 与调度器 dispatcher.py
├─ git                              ← worktree、合并、玩具项目
├─ kimi-code CLI（kimi 命令）       ← 默认 agent 运行体；查找顺序 $KIMI_BIN → PATH → ~/.kimi-code/bin/kimi
├─ harness home（默认 ~/.kimi-code-deepseek，可用 KIMI_CODE_HOME 覆盖）
│    ├─ config.toml                 ← 模型厂商、API Key、模型别名
│    └─ mcp.json                    ← MCP 工具（playwright 用于浏览器验证）
└─ 本仓库
     ├─ .env                        ← 可选，本机环境变量（模板 .env.example）
     ├─ projects.json               ← 要让 agent 开发的项目（模板 projects.example.json）
     ├─ feishu.json                 ← 可选，飞书机器人凭据
     └─ data/board.db               ← SQLite，首次启动自动建表
产物与 worktree 自动建在 ~/.req-board/{artifacts,worktrees}/
```

## 1. 安装

```bash
git clone https://github.com/RonSpeace/loop-engine.git && cd loop-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 可选：接飞书机器人
# .venv/bin/pip install -r requirements-feishu.txt
# 可选：项目要用 MySQL 只读查询
# .venv/bin/pip install mysql-connector-python
```

安装 kimi-code CLI，确认可用：

```bash
kimi --version          # 或 ~/.kimi-code/bin/kimi --version
```

如果你的 kimi 版本需要登录或激活，先交互运行一次 `kimi` 完成初始化。

## 2. 配置模型与 API Key

所有后台 agent 都经 `scripts/kimi-chat-acp` 启动，它把 `KIMI_CODE_HOME` 默认指向 `~/.kimi-code-deepseek`
（交互终端用的 `~/.kimi-code` 不受影响）。模型和 MCP 配置都放在这个目录。

### 2.1 config.toml

```bash
mkdir -p ~/.kimi-code-deepseek
# 已有可用的 ~/.kimi-code 配置时，可复制一份再改：
# cp ~/.kimi-code/config.toml ~/.kimi-code-deepseek/config.toml
```

`~/.kimi-code-deepseek/config.toml` 的大致结构如下（字段以你所用 kimi-code 版本的文档为准）：

```toml
default_model = "deepseek/v4.1-flash"

[providers.deepseek]
type = "openai"                       # openai / anthropic / openai_responses / google-genai / kimi
base_url = "https://api.deepseek.com/v1"
api_key = "sk-你的key"                 # API Key 只放这里，不进本仓库

[models."deepseek/v4.1-flash"]
provider = "deepseek"
model = "deepseek-flash"              # 厂商侧的模型名
max_context_size = 128000
```

文件建好后，也可以在看板的「设置 → 模型设置」里编辑厂商、Key 和模型别名。

### 2.2 让模型名一致

全局模型由环境变量 `KIMI_MODEL` 决定，默认 `deepseek/v4.1-flash`。它必须是 `config.toml` 里存在的别名。
需要改时写进 `.env`（`run_local.sh` 会自动导出）：

```bash
KIMI_MODEL=deepseek/v4.1-flash
```

不同角色可以用不同模型，按以下顺序取第一个有值的：

1. `projects.json` 的 `<项目>.models.<角色>`（看板项目信息条最右侧「模型调参」，保存后下一张卡生效）；
2. `flows.json` 的 `roles.<角色>.model`；
3. 环境变量 `KIMI_MODEL_<角色大写>`，如 `KIMI_MODEL_DEV`；
4. 全局 `KIMI_MODEL`。

例如分析与评审用快模型、开发用强模型：在 `config.toml` 里配两个别名，再在 `flows.json` 里给
`roles.dev.model` 填强模型的别名，重启看板和调度器。各角色当前生效的模型可在看板 `/guide` 的
「内置 agent 定义」里查看。

### 2.3 mcp.json

`~/.kimi-code-deepseek/mcp.json` 至少配 `playwright`，dev 与 accept 角色靠它做浏览器级验证。
server 名必须是 `playwright`：

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

已有 `~/.kimi-code/mcp.json` 时可以软链共享：`ln -s ~/.kimi-code/mcp.json ~/.kimi-code-deepseek/mcp.json`。
只读角色用的调研工具（读文件、检索、git 日志等）由系统内置，无需配置。

### 2.4 冒烟验证

```bash
KIMI_CODE_HOME=~/.kimi-code-deepseek kimi --version
KIMI_CODE_HOME=~/.kimi-code-deepseek kimi      # 可选：交互问一句，确认 Key 和模型可用
```

## Agent runtimes（运行体选择）

每个角色通过 ACP 驱动一个 agent 运行体。注册表在 `acp_shared.AGENT_RUNTIMES`：

| 运行体 | 可用角色 | 说明 |
|:--|:--|:--|
| `kimi`（默认） | 全部 | kimi-code CLI，模型按上文第 2 节配置 |
| `claude-code` | 仅 dev | 优先用 PATH 上的 `claude-code-acp`，否则 `npx -y @zed-industries/claude-code-acp@0.16.2`；需先登录 `claude` CLI |
| `codex` | 仅 dev | 优先用 PATH 上的 `codex-acp`，否则 `npx -y @zed-industries/codex-acp@0.16.0`；需先登录 `codex` CLI |

后两者需要 Node.js / npm。选择顺序：`projects.json` 的 `<项目>.runtimes.<角色>` → `<项目>.runtime` →
`flows.json` 的 `roles.<角色>.runtime` → 环境变量 `LOOP_AGENT_RUNTIME` → `kimi`。未知或空的运行体 ID 会在启动前被拒绝。

```json
{"my-project": {"paths": ["/path/to/repo"], "runtimes": {"dev": "codex"}}}
```

- Claude Code / Codex 使用各自适配器的默认模型，不读 kimi 的模型别名。
- 它们目前**只开放给开发角色**：还不能证明只读限制在这两个运行体上同样成立，所以分析、评审、验收、
  冲突解决和助手会话在启动前就会被拒绝。这些角色请保持 `kimi`；把全局运行体改掉会让这些阶段停住。
- 要指定启动命令，用 `LOOP_AGENT_CMD_KIMI`、`LOOP_AGENT_CMD_CLAUDE_CODE`、`LOOP_AGENT_CMD_CODEX`，
  值按 shell 引号语法拆成参数（不经 shell 执行；kimi 需自行带上 `acp`），如 `LOOP_AGENT_CMD_CODEX='/opt/bin/codex-acp'`。
- 改 `.env` 或 `flows.json` 后重启看板与调度器；项目级选择从下一个新会话开始生效。

## 3. 飞书（可选）

**不接飞书**（推荐先这样）：所有确认都在看板网页上完成，功能完整。未安装 `lark-oapi` 或没有凭据时调度器自动关闭飞书；
也可以显式设置 `REQ_BOARD_NO_FEISHU=1`。

**接自己的飞书机器人**：

1. 在飞书开放平台建企业自建应用，开启机器人能力，事件订阅选**长连接（WebSocket）**模式（无需公网地址）。
2. 申请收发单聊消息的权限（`im:message` 相关），发布版本。
3. 在看板「设置 → 飞书机器人」填入 App ID / App Secret 并保存（写入仓库根目录的 `feishu.json`）。
4. 重启调度器（它只在启动时读取凭据），然后私聊机器人发一条消息建立会话。

## 4. 准备项目

### 4.1 建一个玩具项目

一键脚本会在 `~/projects/toy-project` 建好带测试的 git 仓库（可用 `TOY_DIR=...` 改位置）：

```bash
bash scripts/setup_toy_project.sh
```

想手动建也可以：一个有首个提交的 git 仓库，包含少量代码和测试（让 dev 有活干、accept 有东西验），
以及一份 `AGENTS.md`。

### 4.2 项目的 AGENTS.md

`AGENTS.md` 写项目自身的规范（构建/测试命令、代码风格、目录说明），并加上看板协作约定：

```markdown
## 需求看板协作约定

本项目的需求看板运行在 http://127.0.0.1:8899（若未运行可跳过，不要报错）。

1. 暂缓需求建卡：POST /api/requirements，project 填本项目目录名，status 必须传 "later"。
2. 开始开发标进行中：PATCH 状态为 doing。
3. 完成标已完成：按标题语义匹配后 PATCH 为 done；匹配不确定必须先问用户。
4. 改状态带请求头 X-Actor: agent:toy-project:<需求号>。
5. notes 只写要做什么，不写讨论过程。
```

### 4.3 注册到 projects.json

```bash
cp projects.example.json projects.json
# 把 toy-project 的 paths 改成你的绝对路径
```

也可以启动看板后在左栏「项目列表」旁点 `+` 添加（填项目名、仓库路径、主干分支、项目目标），无需重启。
`projects.json` 是本机数据，已加入 gitignore。可选字段（`branch_only`、`git_sync`、`restart`、`budgets`、
`models`、`runtimes` 等）第一轮都不用配，说明见 [dispatcher.md](dispatcher.md)。

### 4.4 信任工作目录

kimi 对工作目录有信任机制，信任记录在 harness home 里。用同一个 home 在项目目录交互运行一次并接受：

```bash
cd ~/projects/toy-project && KIMI_CODE_HOME=~/.kimi-code-deepseek kimi
```

如果 agent 在 `~/.req-board/worktrees/` 下报目录未信任，用同样方法对该目录接受一次。

## 5. 启动

先自检：

```bash
.venv/bin/python scripts/doctor.py
```

通过的标准：退出码 0，且输出「硬失败 0 项」。⚠️ 是可选项，按需处理。

**前台直跑**（macOS / Linux 通用，Ctrl-C 全部停止）：

```bash
./scripts/run_local.sh               # 同时启动看板和调度器
# 或分开：./scripts/run_local.sh board / ./scripts/run_local.sh dispatcher
```

**macOS 常驻**（launchd 托管，登录自启、崩溃自动拉起）：

```bash
./scripts/install-launchd.sh         # 首次安装、换机或改了 plists/*.tmpl 后运行一次
./start.sh                           # 日常启动 / 重启看板
```

仓库若放在 `~/Documents` 下，需要在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里给 `/bin/bash` 授权，
否则 launchd 进程读不到仓库。日志在 `~/Library/Logs/req-board/`。Linux 可用 systemd 等托管 `scripts/run_local.sh`。

**确认已经跑起来**：

| 检查 | 方法 | 预期 |
|:--|:--|:--|
| 看板 | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8899` | `200` |
| 项目已识别 | `curl -s http://127.0.0.1:8899/api/auto-projects` | `["toy-project"]` |
| 调度器心跳 | `data/dispatcher_heartbeat.json` 的 `last_poll_at` | 约每 15 秒更新 |
| 页面 | 浏览器打开 http://127.0.0.1:8899 | 右上角健康角标不是红色 |

## 6. 跑通第一条闭环

1. 建一张状态为 **open** 的卡，写清验收标准。可以在看板里聊天建卡后把状态改成「待开发」，或直接调接口：

   ```bash
   curl -s -X POST http://127.0.0.1:8899/api/requirements \
     -H 'Content-Type: application/json' \
     -d '{"project": "toy-project", "status": "open",
          "title": "给 calc 增加乘法函数 mul 并补测试",
          "notes": "验收：mul(2,3)=6；覆盖负数与 0；全部单测通过。"}'
   ```

2. 卡片静置约 1 分钟后被调度器捞起，变为「进行中」，依次经过：
   - **需求分析**：写 `analysis.md`，推荐流程（这张卡应推荐 standard）；
   - **开发**：建 worktree（分支 `f-YYYYMMDD-<编号>`），改代码、自测、提交；
   - **代码审查**：只读审查 diff，通过后放行；
   - **验收**：独立验证，写 `accept.md`；
   - **收尾**：以 `--no-ff` 合并回 toy-project 的 main，卡片变为「已完成」，清理 worktree 和分支。
3. 过程中看两处：卡片的执行过程面板（实时事件），以及 `~/.req-board/artifacts/toy-project/req-<编号>/`（各角色产物）。
4. 完成后：`git -C ~/projects/toy-project log --oneline` 能看到 `auto merge req #<编号>` 合并提交。

agent 提问（need_confirm）时，在卡片上点按钮或在右栏聊天框回答。卡住时先看执行过程最后几条事件和调度器日志；
进程级失败会自动重试 1 次，仍失败则转为暂缓并在摘要里写明原因，修好后改回 open 即可重跑。

## 7. 常用配置项

| 项 | 在哪里配 |
|:--|:--|
| 项目与仓库 | `projects.json`（或看板左栏 `+`） |
| 模型与 API Key | harness home 的 `config.toml` + `KIMI_MODEL` |
| 按角色 / 按项目的模型 | `flows.json` 的 `roles.<角色>.model`；项目级在看板「模型调参」 |
| 运行体 | `projects.json` 的 `runtimes` / `runtime`，或 `flows.json` 的 `roles.<角色>.runtime` |
| kimi 可执行文件 | `KIMI_BIN` |
| harness home | `KIMI_CODE_HOME`（默认 `~/.kimi-code-deepseek`） |
| 看板端口 / 地址 | `REQ_BOARD_PORT`（默认 8899）；调度器访问看板用 `REQ_BOARD_API` |
| 存储时区 | `REQ_BOARD_TZ`（两个进程须一致） |
| 飞书 | `feishu.json`（设置抽屉）；`REQ_BOARD_NO_FEISHU=1` 关闭 |
| 项目自带约定 | 各仓库的 `.loop/project.json`，见 [project-manifest.md](project-manifest.md) |

`.env.example` 列出了全部可选环境变量。

## 8. 排障

| 现象 | 处理 |
|:--|:--|
| kimi not found | 安装 kimi-code CLI，或 `export KIMI_BIN=/path/to/kimi` |
| 会话报模型错误 / 401 | 核对 `config.toml` 的 Key 与模型名，并与 `KIMI_MODEL` 对齐；用 2.4 的方法冒烟 |
| agent 报目录未信任 | 在该目录执行 `KIMI_CODE_HOME=~/.kimi-code-deepseek kimi` 接受一次 |
| 卡片一直不被捞起 | 状态是 open、静置 ≥1 分钟、卡片项目名与 `projects.json` 的键完全一致 |
| worktree 创建失败 | 项目路径必须是有提交、有主干分支的 git 仓库；必要时配置 `base_branch` |
| 浏览器验证提示没有工具 | `mcp.json` 里没有名为 `playwright` 的 server |
| 飞书显示「显式停用」 | 未安装 `lark-oapi` 或设置了 `REQ_BOARD_NO_FEISHU=1` |
| 飞书收不到消息 | 核对凭据、应用已发布、使用长连接模式，改凭据后重启调度器 |
| 改了 `flows.json` / `prompts/` 不生效 | 重启：直跑模式 Ctrl-C 后重跑 `run_local.sh`；launchd 模式 `./start.sh` 与 `scripts/dispatcher-restart.sh <pid>` |
| 端口冲突 | 设置 `REQ_BOARD_PORT`，并同步调度器的 `REQ_BOARD_API` |
| launchd 服务起不来（`Operation not permitted`） | 给 `/bin/bash` 授予完全磁盘访问权限后重跑 `scripts/install-launchd.sh` |
| 需要隔离的测试看板 | `scripts/test_instance.sh start <端口> --seed` 启动，`scripts/test_instance.sh stop <端口>` 清理 |

## 9. 下一步

- 接入真实项目：按第 4 节注册，并在项目仓库里写 `.loop/project.json`（测试命令、指引、高风险路径），
  写法见看板 `/guide` 的「项目配置指引」页签和 [project-manifest.md](project-manifest.md)。
- 了解流程与状态：看板 `/guide`。
- 调度器细节：[dispatcher.md](dispatcher.md)；接口：[api.md](api.md)。
