# 本机跑通指南（quickstart）

> 目标：不动核心逻辑，把这套系统在新机器上跑起来——飞书/API KEY 全部用自己的配置，数据库本地新建，Agent 的工具与规范配齐，最后用玩具项目完整走一遍「建卡 → 分析 → 开发 → 代码审查 → 验收 → 自动合并」闭环。
> 本文所有路径/环境变量均已对照代码核验（2026-09-30）。改了配置或提示词后无热加载，重启看板与调度器两个进程才生效。
> 两台机器同步引擎代码、各自保留运行时：见 [two-machine-sync.md](two-machine-sync.md)。
> 支持 macOS 与 Linux：前台直跑（`scripts/run_local.sh`）两者通用；launchd 托管仅 macOS。

## 0. 依赖地图：跑起来到底需要什么

```
你的机器
├─ Python 3.11+（建议 .venv）        ← 看板与调度器
│    ├─ flask                        ← board.py
│    ├─ lark-oapi + certifi          ← 可选：仅接飞书时需要（未安装自动按「不接飞书」运行）
│    └─ mysql-connector-python       ← 可选（项目配了 mysql 才需要）
├─ git                                ← worktree / 合并 / 玩具项目仓库
├─ kimi-code CLI（kimi 命令）         ← 所有 agent 的运行体（ACP 模式）
│    └─ 查找顺序：$KIMI_BIN → PATH 里的 kimi → ~/.kimi-code/bin/kimi
├─ 模型配置（API KEY 在这里，不在本项目代码里）
│    └─ harness home（默认 ~/.kimi-code-deepseek，可用 KIMI_CODE_HOME 覆盖）
│         ├─ config.toml             ← provider / API key / 模型名
│         └─ mcp.json                ← MCP 工具（playwright 必配；tapd 可选）
└─ 本仓库（loop-engine）
     ├─ .env                         ← 本机环境变量（模板 .env.example；可缺省）
     ├─ feishu.json                  ← 飞书凭据（设置抽屉「飞书机器人」生成；不用飞书可缺省，已 gitignore）
     ├─ projects.json                ← 项目清单（新 建，模板 projects.example.json）
     ├─ data/board.db                ← SQLite，首次启动自动建表（每台机器独立）
     └─ ~/.req-board/{artifacts,worktrees}/   ← 产物与 worktree，自动创建
```

三条硬规则先记住：

1. **所有后台 agent（调度器各角色/建卡对话/需求助手）都经 `scripts/kimi-chat-acp` wrapper 启动**，wrapper 把 `KIMI_CODE_HOME` 默认指到 `~/.kimi-code-deepseek`——你的模型配置和 MCP 配置都放这个目录（不是交互终端用的 `~/.kimi-code`）。
2. **模型名三处对齐**：harness 的 `config.toml`、环境变量 `KIMI_MODEL`（不设则默认 `deepseek/v4.1-flash`，见 `acp_shared.py:56 ACP_MODEL`）、以及你实际可用的 provider。任何一处对不上，会话会起不来或走错模型。
3. **飞书是可选通道**：未安装 `lark-oapi` 或设了 `REQ_BOARD_NO_FEISHU=1` 时调度器自动关闭飞书，确认通道走看板网页按钮，功能完整。

## 1. Step 1：基础环境（半小时）

```bash
git clone https://github.com/RonSpeace/loop-engine.git && cd loop-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 可选：接飞书机器人时再装
# .venv/bin/pip install -r requirements-feishu.txt
# 可选：以后项目要配 MySQL 只读查询时再装
# .venv/bin/pip install mysql-connector-python
```

安装 kimi-code CLI（后台 agent 的运行体）。装完确认：

```bash
kimi --version          # 或 ~/.kimi-code/bin/kimi --version
```

若你的 kimi 发行版需要登录/激活，先交互式跑一次 `kimi` 完成初始化。

## 2. Step 2：模型与 API KEY 配置（关键）

### 2.1 建 harness home 并配置 config.toml

```bash
mkdir -p ~/.kimi-code-deepseek
# 若你已有 ~/.kimi-code 且配置可用：以它为底复制一份再改
# cp ~/.kimi-code/config.toml ~/.kimi-code-deepseek/config.toml
```

`~/.kimi-code-deepseek/config.toml` 配置 provider 与 API key（字段名以你手上 kimi 版本的配置文档为准；本仓库 docs/dispatcher.md 记录的形态是 `providers.<name>` 下配模型官方名，顶层 default_model 指向 `provider/model`）。示例（DeepSeek 形态）：

```toml
# 以 kimi-code 实际配置说明为准，以下为结构示意
default_model = "deepseek/v4.1-flash"

[providers.deepseek]
model = "deepseek-flash"      # provider 侧官方模型名
api_key = "sk-你的key"         # API KEY 唯一落点：这里，不进本仓库
```

换任何模型（DeepSeek/GLM/Kimi/其它）都只改这里 + 对齐 `KIMI_MODEL`，代码零改动。

### 2.2 对齐模型名

```bash
# 与 config.toml 的 default_model 全名一致；不设则默认 deepseek/v4.1-flash
export KIMI_MODEL="deepseek/v4.1-flash"
```

建议写进 `.env`（`run_local.sh` 会自动导出）。

**per-role 差异化模型**（机制已内置，#419，纯配置零改码）：每个角色可用不同模型，兜底链 =
`flows.json` 的 `roles.<id>.model`（唯一推荐落点）→ 环境变量 `KIMI_MODEL_<角色大写>`（如
`KIMI_MODEL_DEV`，本地临时覆盖用，配置非空时优先配置）→ 全局 `KIMI_MODEL`。会话启动时经 ACP
`set_config_option` 把模型钉进会话，不依赖 kimi 的 `default_model`。

模型走中转站（如 airoute）配多个模型的方式：同一入口/同一 key 配多个 provider 别名，一个别名指向一个上游模型（字段名以你手上 kimi 版本的配置文档为准）：

```toml
# ~/.kimi-code-deepseek/config.toml（示意：快模型 + 强模型两个别名）
default_model = "deepseek/deepseek-flash"

[providers.deepseek]           # 别名1：快模型（调研/审查类角色够用）
model = "deepseek-v4-flash"
api_key = "sk-你的中转站key"

[providers.dsv4]               # 别名2：强模型（写码/出方案）；块名随意，语义化即可
model = "deepseek-v4"          # 中转站支持的上游模型名
api_key = "sk-同一个key"        # 同一中转站入口
```

然后在 `flows.json` 按角色指配（示例：分析/审查用快模型，开发用强模型），改完重启看板+调度器：

```json
"analysis":    { "...": "...", "model": "deepseek/deepseek-flash" },
"dev":         { "...": "...", "model": "dsv4/deepseek-v4" }
```

各角色当前生效模型与来源（配置/环境变量/全局）可随时在看板 `/guide`「九、内置 agent 定义」的角色表里查看。

### 2.3 配 MCP 工具（mcp.json）

`~/.kimi-code-deepseek/mcp.json` 里至少配 **playwright**（server 名必须叫 `playwright`，代码按 `mcp__playwright__` 前缀放行浏览器工具，`acp_shared.py:483`；dev/accept 的浏览器级验证靠它）：

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

（若已有 `~/.kimi-code/mcp.json`，软链共享即可：`ln -s ~/.kimi-code/mcp.json ~/.kimi-code-deepseek/mcp.json`。）

不需要配的：只读角色的调研工具（list_dir/read_file/git_log/git_diff/search_text/db_query 等）是系统**内置自建**的 MCP server（`acp_shared.ReadonlyMcpServer`，会话级随机端口），零配置；tapd/mysql 全部可选，不配自动降级。

### 2.4 冒烟验证 kimi 可用

```bash
KIMI_CODE_HOME=~/.kimi-code-deepseek kimi --version
# 可选：交互跑一句提问确认 API key/模型可用
KIMI_CODE_HOME=~/.kimi-code-deepseek kimi
```

## 3. Step 3：飞书渠道（二选一）

**方案 A（推荐先用）：不接飞书**

```bash
export REQ_BOARD_NO_FEISHU=1   # 显式跳过飞书（缺凭据时调度器自行降级）
```

所有确认（need_confirm、合并冲突、豁免仲裁）走看板网页按钮，功能完整，只少一个 IM 推送渠道。

**方案 B：接自己的飞书机器人**

1. 飞书开放平台建**企业自建应用**，开启机器人能力，事件订阅选 **长连接（WebSocket）模式**（无需公网回调）。
2. 申请权限：接收与发送单聊消息（`im:message` 相关）。
3. 发布版本后，私聊机器人发一条消息（建立会话）。
4. 凭据写仓库根 `feishu.json`（**推荐页面填写**：右栏「设置」抽屉 → 「飞书机器人」tab → 填 App ID / App Secret → 保存；也可手工建 0600 的 `feishu.json`）：

```json
{"app_id": "cli_xxx", "app_secret": "xxx"}
```

5. **改凭据后需重启调度器**（`scripts/dispatcher-restart.sh <pid> config-change`）——调度器仅在启动时读取一次 `feishu.json`；看板侧读写与连通性测试不需重启。飞书私聊机器人即可多轮对话建卡（见 `docs/dispatcher.md`「飞书多轮聊天建卡」）。

## 4. Step 4：本地仓库与项目注册

### 4.1 建玩具项目（第一个闭环用）

```bash
mkdir -p ~/projects/toy-project && cd ~/projects/toy-project
git init -b main
```

放三样东西：

1. 一点真实代码与测试（如 `calc.py` + `test_calc.py`，让 dev 有活干、accept 有东西验）；
2. 一份 `AGENTS.md`（Agent 的项目规范，至少含下面 4.2 的协作约定段）；
3. 提交首个 commit（worktree 基线从默认分支拉取，空仓库会失败）。

### 4.2 玩具项目 AGENTS.md 协作约定段（模板）

```markdown
# toy-project

（项目自身的开发规范：构建/测试命令、代码风格、目录说明……）

## 需求看板协作约定

本项目的需求看板运行在 http://127.0.0.1:8899（若未运行可跳过，不要报错）。

1. 暂缓需求建卡：POST /api/requirements，project 填本项目目录名，status 必须传 "later"。
2. 开始开发标进行中：PATCH 状态为 doing。
3. 完成标已完成：按标题语义匹配后 PATCH 为 done；匹配不确定必须先问用户。
4. 改状态带请求头 X-Actor: agent:toy-project:<需求号>。
5. notes 只写要做什么，不写讨论过程。
```

### 4.3 注册进 projects.json

**推荐直接在页面操作**：左导航「项目列表」标题旁「+」→ 填项目名 / 仓库路径（每行一条绝对路径）/ base_branch / goal → 创建即接入（无需重启）；项目行 hover 可「编辑」（goal/paths/base_branch）与「移除接入」（存在进行中的卡时阻断）。手动编辑文件仍然支持：

```bash
cp projects.example.json projects.json
# 编辑：把 toy-project 的 paths 指向你的绝对路径
```

projects.json 是本机数据（已 gitignore）。字段说明见 `docs/dispatcher.md`「projects.json 可选配置」：`branch_only`（done 时只推 f-分支到远端、不本地合并，适合走 PR/CI 发版的仓库）、`base_branch`、`restart`（done 后自动重启配方）、`mysql`、`repo_map` 均可选，第一轮全不配。手工编辑文件后看板页面会在刷新/下次请求时更新（列表与信息条动态读），但看板进程内的建卡会话项目快照需重启看板才刷新（UI 增删改会自动刷新，无此限制）。

> 测试实例隔离：拷贝一份 projects.json 副本，用 `REQ_BOARD_PROJECTS=<副本路径>` 启动看板（如 `REQ_BOARD_PORT=8898 REQ_BOARD_PROJECTS=/tmp/xxx/projects.json python3 board.py`），UI 增删改只写副本、不触真实注册表。

### 4.4 一次性信任（workspace trust）

kimi 对工作目录有信任机制，信任记录在 harness home 下。用**与后台相同的 home** 在项目目录跑一次交互式 kimi 并接受信任：

```bash
cd ~/projects/toy-project && KIMI_CODE_HOME=~/.kimi-code-deepseek kimi
# 进入后若提示信任目录，接受；退出即可
```

（worktree 目录在 `~/.req-board/worktrees/` 下，如遇「未信任」拒绝，同法对其接受一次；详见第 9 节排障。）

## 5. Step 5：启动与验证

**macOS 推荐：launchd 托管**（登录自启、崩溃自拉起；Linux 用下方「前台直跑」，或自行用 systemd/supervisor 托管 `scripts/run_local.sh`）。

```bash
cd loop-engine
./scripts/install-launchd.sh    # 首次装机/换机/改了 plists/*.tmpl 后跑一次；幂等
./start.sh                      # 日常启动/重启（launchctl kickstart -k）
```

`install-launchd.sh` 会把仓库内 `plists/*.plist.tmpl` 里的 `{{REPO}}` / `{{LOG_DIR}}` 按本机路径渲染成
`~/Library/LaunchAgents/<label>.plist` 再 bootstrap：`local.req-board`（看板）、
`local.automation.req-board-dispatcher`（调度器）、`local.automation.log-rotate`（日志轮转）。
仓库里不存机器绝对路径，所以两台机器（或换机、改用户名）共用同一份模板，不用手改 plist。
卸载/停用：`launchctl bootout gui/$(id -u)/<label> && rm ~/Library/LaunchAgents/<label>.plist`。

> ⚠️ **前置条件**：本仓库若位于 `~/Documents`（macOS 隐私保护目录），必须给 `/bin/bash` 授予「完全磁盘访问权限」
> （系统设置 → 隐私与安全性 → 完全磁盘访问权限），否则 launchd 进程读仓库被拒、服务起不来。
> 未授权时 `install-launchd.sh` 会明确报错。

**通用：前台直跑**（macOS / Linux 均可，不依赖 launchd；改完脚本即时生效、Ctrl-C 全停）：

```bash
./scripts/run_local.sh          # 同时起看板 + 调度器；Ctrl-C 全停
# 或分开：./scripts/run_local.sh board / ./scripts/run_local.sh dispatcher
```

启动前先自检（详见 5.1）：

```bash
.venv/bin/python scripts/doctor.py
```

通过口径：退出码 0 且输出「硬失败 0 项」（⚠️ 为可选项，按需处理）；裸 `python3` 不再作为推荐入口。

**验证清单（全绿才算起好）**：

| 检查 | 命令/位置 | 预期 |
|:--|:--|:--|
| 看板可访问 | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8899` | 200 |
| 数据库建好 | `data/board.db` 存在 | 首次启动自动建表 |
| 调度器轮询 | 调度器前台日志 | 每 15s 一轮，无报错 |
| 心跳 | `data/dispatcher_heartbeat.json` | `last_poll_at` 持续更新 |
| 页面健康角标 | 浏览器打开 127.0.0.1:8899 | 右上角非红色 |
| 自动项目识别 | `curl -s http://127.0.0.1:8899/api/auto-projects` | `["toy-project"]` |

## 6. Step 6：第一条闭环冒烟（核心验收）

1. 网页建卡：项目选 `toy-project`，标题「给 calc 增加乘法函数 mul 并补测试」，notes 写清验收标准（如「mul(2,3)=6；负数与 0 用例；跑通全部单测」），**状态选 open**（明确要开发）。
2. 等 15s + 静置 1 分钟（调度器只捞静置满 1 分钟的 open 卡），观察卡片变 doing：
   - **analysis**（只读调研）→ 产物 `analysis.md` + 推荐 `suggested_flow`（应推荐 standard）；
   - **dev**：自动建 worktree（分支 `f-YYYYMMDD-<id>`）→ 改码 → 自测 → commit；
   - **code_review**：只读审查 diff，verdict=pass 放行；
   - **accept**：独立测试实例验收，产出 `accept.md`；
   - **terminal**：自动 `--no-ff` 合并回 toy-project 的 main，卡转 **done**，worktree/分支回收。
3. 全程盯两处：卡片「执行过程」面板（实时事件流）；`~/.req-board/artifacts/toy-project/req-<id>/`（各角色产物）。
4. 终检：`cd ~/projects/toy-project && git log --oneline` 应出现 `[auto] #<id> ...` 提交；`~/.req-board/worktrees/toy-project/` 已清空。

冒烟过程中 agent 提问（need_confirm）时，到网页卡片上点确认按钮答复（未接飞书时唯一通道）。

**失败排查**：任何环节卡住，先看卡片执行过程面板最后几条事件 + 调度器前台日志；进程级失败会自动重试 1 次后转 later 并在 summary 留原因，改回 open 可重跑。

## 7. Step 7：Agent 工具与规范配齐清单

| 类别 | 内容 | 状态 |
|:--|:--|:--|
| 内置工具 | Read/Grep/Glob/Edit/Write/Bash（kimi 自带）+ 只读 MCP（系统内置，零配置） | 开箱即用 |
| 浏览器工具 | playwright MCP（mcp.json 配 server 名 `playwright`） | Step 2.3 已配 |
| 建卡技能 | `skills/req-board-card` 供**其它项目**的 agent 用：`mkdir -p ~/.agents/skills && cp -r skills/req-board-card ~/.agents/skills/`（技能正文按该路径引用脚本） | 按需安装 |
| 项目规范 | 每个接入仓库的 AGENTS.md（协作约定段 + 项目自身规范） | 每项目一次 |
| 流程/提示词 | flows.json + prompts/ 已配齐 8 角色 2 流程 | 开箱即用，改动需重启 |
| 角色与配置总览 | 看板 `/guide`「九、内置 agent 定义」（动态版块：流程表、角色×生效模型、提示词全文、运行时环境与工具） | 开箱即看 |
| 提示词公共尾 | `acp_shared.py` 的 COMMON_TAIL（结果契约/安全红线） | 内置 |
| 可选集成 | tapd（需求来源/回评）、mysql（只读查库）、飞书（IM 确认与建卡） | 不配自动降级 |

## 8. 可配置项与残留硬编码对照表

已参数化（照本文配置即可，无需改码）：

| 项 | 配置点 | 代码依据 |
|:--|:--|:--|
| 项目仓库 | `projects.json`（本机文件，gitignore） | `dispatcher.py:72` |
| 飞书凭据 | 仓库根 `feishu.json`（设置抽屉「飞书机器人」读写，0600，已 gitignore）；跳过开关 `REQ_BOARD_NO_FEISHU=1`；改凭据后需重启调度器 | `dispatcher.py` `load_feishu_config()` / `board.py` `/api/feishu-settings` |
| API KEY/模型 | harness `config.toml` + `KIMI_MODEL`（或保持默认 `deepseek/v4.1-flash`） | `acp_shared.py:52,56` |
| per-role 模型 | `flows.json` 的 `roles.<id>.model`（空 = 走全局）；临时覆盖 `.env` 加 `KIMI_MODEL_<角色大写>` | `acp_shared.py role_model_detail()`（四层链唯一判定点）、`dispatcher.py` 会话 set_config_option |
| 项目级模型覆盖 | `projects.json` 的 `<项目>.models.<角色>`（**仅该项目生效**；UI = 项目信息条最右端机器人抽屉，保存后下一张卡生效、无需重启——projects.json 已热重载；变更仍受四层链优先级：项目级 > 角色级 > 环境变量 > 全局） | 写路径 `PUT /api/projects/<name>/models`（board.py，fail-closed + 原子替换 + .bak），判定 `acp_shared.role_model_detail()`，热重载 `dispatcher._maybe_reload_projects()` |
| 思考档位 | #50 起统一模型默认：不请求、不配置、不展示（`flows.json`/`projects.json` 的 thinking 键不再读取，旧配置静默忽略） | `ACPSession.start()` 不再发 `set_config_option {configId:"thinking"}` |
| kimi 二进制 | `KIMI_BIN` 环境变量 | `acp_shared.py:52` |
| harness 目录 | `KIMI_CODE_HOME`（默认 `~/.kimi-code-deepseek`） | wrapper `scripts/kimi-chat-acp` |
| 看板端口 | `REQ_BOARD_PORT`（默认 8899） | `board.py` 启动段 |
| 看板 API 地址 | `REQ_BOARD_API` | `dispatcher.py:68` |
| 轮询/超时等 | `REQ_BOARD_POLL_SEC` / `REQ_BOARD_MIN_AGE_MIN` 等 | `dispatcher.py` 常量段 |
| 进程主管模式 | `REQ_BOARD_SUPERVISOR`（`run_local.sh` 自动导出 `run_local`；不设 = launchd/未知=旧路径，行为逐字不变） | 判定 `acp_shared.py supervisor_mode()`；消费方 dispatcher.py（重启走 exec 自重启）/ board.py（重启降级提示） |

历史布局的残留（**不阻塞运行**）——launchd 资产已全部收进本仓库：plist 模板在 `plists/*.plist.tmpl`、
入口脚本 `scripts/launchd-run.sh`、安装脚本 `scripts/install-launchd.sh`，日志移出到 `~/Library/Logs/req-board/`
（`REQ_BOARD_LOG_DIR` 可覆盖）。仍带历史布局痕迹的只有：

| 位置 | 内容 | 说明 |
|:--|:--|:--|
| `board.py` `_reqchat_automation_data()` | 遗留 `automation/data/` 日志白名单分支 | 保留兼容：`projects.json` 配了 `automation` 项目才生效；未配置即 fail-closed。当前日志走 `_reqchat_launchd_log_dir()` |
| 各 `tests/*.py` 顶部注释 | 示例命令写了 `/<原路径>/.venv/bin/python -m unittest …` | 纯注释示例，按本机 `.venv/bin/python` 跑即可，不影响行为 |
| `tests/test_req_chat_mcp.py`、`test_main_ws_write_guard.py` 等 | 用例内的假路径 fixture（`mock.patch` 注入） | 故意写成假数据，与真实机器路径无关 |
| 数据库 | 无 | 全新自动建库；`data/board.db` 是本机真实需求数据，不随仓库同步 |

## 9. 排障速查

| 症状 | 原因与处置 |
|:--|:--|
| 飞书通道显示「显式停用」 | 未安装 `lark-oapi` 或设了 `REQ_BOARD_NO_FEISHU=1`；要接飞书：`.venv/bin/pip install -r requirements-feishu.txt` 后重启调度器 |
| 会话起不来：kimi not found | 装 kimi-code CLI，或 `export KIMI_BIN=/path/to/kimi` |
| 会话报模型错误/401 | config.toml 的 API key/模型名与 `KIMI_MODEL` 不一致；用 Step 2.4 方式带 `KIMI_CODE_HOME` 冒烟 |
| agent 报目录未信任（workspace trust） | `cd <目录> && KIMI_CODE_HOME=~/.kimi-code-deepseek kimi` 接受一次（项目目录与 `~/.req-board/worktrees/` 聚合目录都可能要） |
| 卡一直不被捞 | 确认 status=open 且建卡后静置 ≥1 分钟；projects.json 键名与卡的 project 完全一致 |
| worktree 创建失败 | 项目路径必须是 git 仓库且有默认分支（master/main）；可配 `base_branch` |
| dev 浏览器验证报无工具 | mcp.json 未配 `playwright` server（名字必须精确） |
| launchd 服务起不来（`Operation not permitted` / `getcwd` / exit 126） | 仓库在 `~/Documents` 被 TCC 拦：给 `/bin/bash` 开「完全磁盘访问权限」，再跑 `scripts/install-launchd.sh` |
| 首次 `./start.sh` 卡 40 多秒才 200 | TCC 首次权限评估停顿（之后 1-2 秒）；`start.sh` 已用就绪轮询，属正常 |
| 改了 board.py/flows.json/prompts 不生效 | 无热加载：重启对应进程（launchd：`./start.sh` / `scripts/dispatcher-restart.sh <pid>`；直跑：Ctrl-C 重跑 `run_local.sh`） |
| 直跑模式（`run_local.sh`）下的自动重启行为（#8） | 调度器：合并涉 dispatcher.py/acp_shared.py 时不退出整组——写来源标记 + SIGTERM 后进程内 `os.execv` 自重启（同 PID，日志「exec 自重启」）；exec 失败=拒绝重启绝不裸退出（auto 重试 3 轮后转手动提示）。看板：不调 launchctl，页头出现琥珀角标「看板有新改动待重启 · 直跑模式请 Ctrl-C 后重跑 run_local.sh」（事件+心跳标记落痕），人工 Ctrl-C 重跑后自动消失。无标记（launchd）环境行为逐字不变 |
| 手动重启调度器（直跑/launchd 通用） | `scripts/dispatcher-restart.sh <pid> [来源]`：校验目标为 dispatcher.py（末 token 口径，含空格路径安全）→ 写标记 → SIGTERM；launchd 模式 KeepAlive 拉起，直跑模式 exec 自重启 |
| 端口冲突 | `REQ_BOARD_PORT=8899` 改端口，调度器侧同步 `REQ_BOARD_API` |
| 飞书连不上/收不到通知 | 核对 `feishu.json` 凭据（设置抽屉「飞书机器人」填完后是否重启调度器）、应用已发布、长连接模式；或直接 `REQ_BOARD_NO_FEISHU=1` 走网页按钮 |
| 验证类测试实例端口互杀 | 用 `scripts/test_instance.sh start <端口>` 管理（PID 文件精确清理） |

## 10. 跑通之后

按《Loop-Engine-行动计划》推进：git 基线化（若尚未 init）→ 把真实项目按 Step 4 流程接入 → 阶段 1 统一对话入口。
