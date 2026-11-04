# 双机同步：机器 A ⇄ 机器 B

> 目标：**Loop Engine 引擎代码**在两台机器（如笔记本与台式机）间双向同步；**各机运行时的项目、数据、密钥、worktree 全部留在本机**，不跨机。
> 远端用任一私有 git 托管仓库（GitHub / GitLab / Gitee / 自建均可，`origin/main`），同步脚本 `scripts/sync.sh`。

## 1. 同步边界

| 类别 | 内容 | 是否入库/同步 |
|:--|:--|:--|
| 引擎代码 | `dispatcher.py`、`acp_shared.py`、`board.py`、`board_modules/`、`templates/`、`static/`、`prompts/`、`flows.json`、`scripts/`、`tests/`、`docs/`、`.loop/`、`AGENTS.md` | ✅ 入 git，双向同步 |
| 机器差异模板 | `plists/*.plist.tmpl`（`{{REPO}}` / `{{LOG_DIR}}`） | ✅ 入 git；安装时按本机路径渲染 |
| 运行时数据 | `data/`（board.db、调度器状态） | ❌ 各机独立 |
| 密钥/本机配置 | `.env`、`feishu.json`、`projects.json` | ❌ 不入 git；手工拷贝本机 |
| 依赖与产物 | `.venv/`、`__pycache__/`、`.pytest_cache/`、`.playwright-mcp/` | ❌ 各机重建 |
| Agent 运行时 | `~/.req-board/`（artifacts、worktrees、分支）、`~/.kimi-code-deepseek/`、`~/Library/Logs/req-board/`、`~/Library/LaunchAgents/*.plist` | ❌ 各机独立 |

一句话：**git 管引擎，`.gitignore` 管本机**。仓库里不写任何机器绝对路径，所以两台机器的用户名/仓库路径可以不同。

## 2. 一次性搭建

### 2.1 建私有远端仓库

在任一 git 托管平台新建私有仓库（如 `loop-engine`），**不要**勾选"初始化 README / .gitignore / 许可证"（要一个空仓库）。

### 2.2 已有仓库的机器（机器 A）

```bash
cd <本机仓库目录>                 # 例如 ~/code/loop-engine
git remote add origin <远端仓库地址>
git push -u origin main
```

首次 push 按平台要求认证（HTTPS 用个人访问令牌当密码，或配置 SSH key）；系统凭据管理器（macOS `osxkeychain` 等）会记住，之后不用再输。

推送后在托管平台确认默认分支是 `main`（新建空仓库默认可能是 `master`）。

### 2.3 另一台机器（机器 B）

```bash
git clone <远端仓库地址> <本机仓库目录>   # 例如 ~/code/loop-engine
```

> ⚠️ 仓库目录名由你自定，但**建议两台机器保持一致**：引擎按目录名识别项目（`projects.json` 的项目名、看板 project 字段）并拼 worktree 路径，目录名不同会导致两台看到的项目名不一致，需各自在 `projects.json` 里对齐。

然后补本机运行时（这些不在 git 里，从另一台拷或用模板建）：

```bash
cd <本机仓库目录>

# 1) Python 依赖（飞书 SDK 为可选依赖，只在接飞书时安装）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# .venv/bin/pip install -r requirements-feishu.txt   # 可选：接飞书时

# 2) 本机配置（模板见仓库）
cp projects.example.json projects.json    # 改成这台机器上的项目路径
cp .env.example .env                      # 按需改端口/模型覆盖
# feishu.json：两台机器各自独立的飞书应用凭据（见第 6 节，可选）

# 3) macOS：仓库放在 ~/Documents 等受保护目录时，给 launchd 授予读取权限
#    系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加 /bin/bash

# 4) 自检 + 安装 launchd（按本机路径渲染 plist）
.venv/bin/python scripts/doctor.py
scripts/install-launchd.sh
```

### 2.4 git 身份

两台机器各自设一次（用你自己的身份）：

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

## 3. 日常同步

```bash
# 开工前：拉取并合并（也可 --pull-only，不推）
scripts/sync.sh

# 收工前：确保本地提交都推到远端（也可 --push-only）
scripts/sync.sh
```

脚本做的事：

1. 检查没有未提交的已跟踪改动、没有合并/rebase 中间态；
2. `git fetch origin main`；
3. 本地落后 → 快进；两边都有新提交 → `git merge --no-edit origin/main`（**不用 rebase**）；
4. 推送 `origin main`；
5. 按本次改动文件打印需要重启哪个进程（只提示，不自动重启）。

**为什么用 merge 不用 rebase**：调度器会把需求分支 `--no-ff` 合并进本地 `main`（提交信息 `auto merge req #N`）。这些合并提交一旦推送就是两台共享的历史；`rebase` 会改写它，另一台机器下次同步就会历史分叉。合并提交多一点换取历史稳定，是划算的。

### 冲突处理

```bash
# sync.sh 会在冲突时停住并列出冲突文件
git status                      # 看冲突
# 手工解决（或按需 git checkout --theirs/--ours <文件>）
git add <文件>
git commit --no-edit
scripts/sync.sh                 # 继续推送
```

同一类冲突第二次出现时，`rerere`（脚本自动开启）会套用上次的解法。

## 4. 两台同时改引擎的约定

两台机器各跑一套看板 + 调度器，各自把需求自动合并到**本地 main**，同步靠 git 合并。为了少打架：

1. **引擎自迭代同一时间只在一台跑**：即"对着 loop-engine 建卡、让调度器自动开发引擎"只在一台进行；另一台只做人工小改/紧急修复，改完立刻 `scripts/sync.sh`。
2. **开工前、收工后各同步一次**；改完一小步就 commit，不要攒。
3. **不回写已推送历史**：禁止 `rebase` / `reset --hard` 后强推 `main`。history 是两台共同的。
4. **调度器空闲时同步**：它正在合并需求分支时同步可能撞 `index.lock`，等它空闲再跑。
5. 长改动不怕冲突、怕长分支：优先小步提交 + 频繁同步，而不是开一个放三天的大分支。

## 5. 同步后重启矩阵

`scripts/sync.sh` 会按改动自动提示，手工对照：

| 改动文件 | 动作 |
|:--|:--|
| `dispatcher.py` / `acp_shared.py` | `scripts/dispatcher-restart.sh <pid> sync` |
| `board.py` / `board_modules/` / `templates/` / `static/` | `./start.sh`，再 `curl http://127.0.0.1:8899` 验证 200 |
| `flows.json` / `prompts/` | `./start.sh` + `scripts/dispatcher-restart.sh <pid> config-change`（无热加载） |
| `plists/` | `scripts/install-launchd.sh`（会重启看板+调度器，挑空闲时） |
| 只有文档/测试 | 通常不用重启 |

## 6. 飞书：两台各自独立应用

同一个飞书应用凭据在两台机器同时跑长连接时，事件可能被分发到其中一台，`y <编号>` / `n <编号>` 回复就可能落到错机器。推荐：

- 两台机器各建一个自建应用，各自生成 `feishu.json`（本机不入库）；或
- 只有一台接飞书，另一台 `.env` 里加 `REQ_BOARD_NO_FEISHU=1`，网页按钮通道照常用。

## 7. 新机器 onboarding 清单

- [ ] 私有远端仓库已建，两台都已 `git remote -v` 看到 `origin`
- [ ] 仓库已克隆到本机目录（两台目录名建议一致，见 2.3）
- [ ] `.venv` 建好、`requirements.txt` 装好（接飞书时再装 `requirements-feishu.txt`）
- [ ] `.env`、`projects.json`、`feishu.json` 本机就位（不入库）
- [ ] macOS 且仓库在受保护目录时，`/bin/bash` 有「完全磁盘访问权限」
- [ ] `.venv/bin/python scripts/doctor.py` 通过（退出码 0 且「硬失败 0 项」）
- [ ] `scripts/install-launchd.sh` 成功，`curl http://127.0.0.1:8899` 返回 200
- [ ] `git config rerere.enabled true` 已开（首次跑 `scripts/sync.sh` 会自动开）
- [ ] 跑一次 `scripts/sync.sh` 确认能拉能推

## 8. 备份与回滚

- 私有远端仓库本身就是异地备份；重要节点可 `git tag -a <名字> -m <说明> && git push origin <名字>`。
- 误操作找回：`git reflog`；远端历史被强推过才需要看远端 reflog。
