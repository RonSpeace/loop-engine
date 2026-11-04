# flow-probe：离线流程建议观察工具（#432）

为「是否支持按需求动态组合 agent 节点」积累样本：离线给每条已入流程的需求生成一份**只属于它**的
role 序列建议（仅记录、不执行），样本足够后再决定是否继续该方向。

## 用法

```bash
# 内建自检（无模型、无网络、不读实库）
python3 scripts/flow-probe/flow_probe.py --self-check

# 干跑：只渲染 prompt，不调用模型（推荐先把这条跑一遍核对输入）
python3 scripts/flow-probe/flow_probe.py --dry-run --last 5

# 真跑一批（默认最近 30 张已入流程卡）
python3 scripts/flow-probe/flow_probe.py --last 30

# 指定需求 / 覆盖模型 / 重跑已生成
python3 scripts/flow-probe/flow_probe.py --ids 432,431 --model deepseek/v4.1-flash --force
```

| 参数 | 默认 | 语义 |
|:--|:--|:--|
| `--ids 432,431` | - | 显式 id 列表（覆盖 `--last` 选择集） |
| `--last N` | 30 | 选择集 = 已入流程（`flow` 非空）卡按 id 倒序前 N |
| `--limit N` | 30 | 本次处理上限（与选择集取交集截断） |
| `--force` | 关 | 已生成 `req-<id>.json` 时重跑覆盖（默认跳过并记 `run.log`） |
| `--model M` | 全局默认 | 透传 `kimi -m` |
| `--dry-run` | 关 | 只渲染 prompt 到 `out/dry-run/`，不调用模型 |
| `--out DIR` | `scripts/flow-probe/out` | 输出根（唯一写入位置）；落在仓库 `data/`、`.git/`、`~/.req-board/artifacts/` 之下或仓库根本身会被拒绝 |
| `--db PATH` | `<repo>/data/board.db` | 看板库路径（只读打开） |
| `--artifacts-root DIR` | `~/.req-board/artifacts` | `analysis.md` 根目录 |
| `--self-check` | - | 内建自检，退出码 0/1 |

环境变量：`KIMI_BIN`、`KIMI_CODE_HOME`（默认 `~/.kimi-code-deepseek`）、`FLOW_PROBE_TIMEOUT`（默认 180s）、
`FLOW_PROBE_INTERVAL`（默认 3s）、`FLOW_PROBE_OUTPUT_FORMAT`（`text`|`stream-json`）。
`FLOW_PROBE_TIMEOUT` / `FLOW_PROBE_INTERVAL` 非法值（非数字 / 越界）回落默认并在 stderr 提示，不中断整批。

worktree 内自验：worktree 无 `data/` 与 `.venv/` —— 解释器与看板库都用主工作区绝对路径：

```bash
.venv/bin/python scripts/flow-probe/flow_probe.py \
  --db data/board.db --dry-run --last 3
```

## 零写入契约（逐条）

| 契约 | 实现机制 |
|:--|:--|
| `data/board.db` 只读打开 | `sqlite3.connect("file:<path>?mode=ro", uri=True)` + `PRAGMA query_only=ON`（照抄 `scripts/dbq.py` 先例），全程只 `SELECT`；不写 `data/` |
| 唯一写入根 = `--out`（默认 `scripts/flow-probe/out/`） | 全部写操作经 `write_text/write_json_file/append_log/write_raw`，路径由 `_safe_path` 约束在 `out` 内，越界即报错 |
| 不写 `~/.req-board/artifacts/`、不写 `data/dispatcher_state.json` | 对两者只有 `read_text`（读 `analysis.md`、读 `req_repos`），无写路径 |
| 不调 board API | 不引入 `urllib`/`requests`/`http`，无网络调用 |
| 不启动 ACP 会话 | 只 spawn `kimi -p`（一次性模式），无 ACP JSON-RPC 代码 |
| 不 import 生产模块、仅标准库 | import 段全部 stdlib（`tomllib` 为 3.11+ 可选），不 import `board`/`dispatcher`/`acp_shared` |
| 不接入现有测试套件 | 不改 `tests/`、`board.py`、`dispatcher.py`、`flows.json`、`prompts/` |

保存路径口径：
- `out/req-<id>.json`：提议序列、理由、省略说明（`omitted`）、缺失能力、假设、干跑校验结果、输入快照、基线对照、`model`/`prompt_sha`/`config_sha`/`run_ts`；
- `out/req-<id>.prompt.md`：渲染后 prompt 全文（样本可复原——`notes`/`analysis.md` 事后被编辑、产物被清理也能复现本次输入）；
- `out/SUMMARY.md`：基线流程 vs 提议序列 vs 差异的横向表（每次运行重写为本次窗口）；
- `out/run.log`：逐行追加跳过/失败原因（无 `analysis.md` / 已生成 / 调用失败等）；
- `out/req-<id>.raw.txt`：`invalid` 时保留模型原始 stdout+stderr；该条后续 `--force` 重跑转 `ok` 时自动清理。

## 节点池口径

- 池 = `flows.json` 的 `roles` 去掉 `analysis`（前置环节槽位）与 `merge_resolve`（调度器专用），按 `order` 升序：
  `proposal / tech / tech_review / dev / code_review / accept`。`display`/`artifact` 读 `flows.json`（单一源）。
- `node_type` 与角色语义句是工具内 `ROLE_SPEC` 的副本（语义原文出处 = `prompts/<role>.md`）；键集与
  `flows.json.roles`（去掉两个排除项）不一致时 fail-fast，提示更新工具（防静默漂移）。
- 样本可追溯：`config_sha`（`flows.json` 哈希）、`template_sha`（`prompt.md` 哈希）、`role_prompts_sha`
  （六份角色提示词拼接哈希）随每条记录落盘。

## 模型调用口径

- `kimi -p <prompt>` 一次性非交互模式（**不是** `kimi acp`，不创建 ACP 会话），stdin 关闭、串行执行、
  每条之间按 `FLOW_PROBE_INTERVAL` 限速。
- `KIMI_CODE_HOME` 未设时默认 `~/.kimi-code-deepseek`（与后台流水线同模型）；`model` 取 `--model`，
  否则读该 home 下 `config.toml` 的 `default_model`（读不到记 `unknown`）→ 与 `flows.json` 的
  `roles.<id>.model` 覆盖口径不同，样本记录的是「全局默认模型」。
- cwd = 仓库外临时目录（`tempfile.mkdtemp`，退出清理）：`kimi -p` 具备工具能力（属护栏而非机制保证），
  临时 cwd 避免模型读到仓库内 `AGENTS.md`/`flows.json` 把流程信息带进输入。
- 输出提取三步：剥 `• ` 前缀整体解析 → 去 ```` ``` ```` 围栏 → 首个 `{` 到末个 `}` 切片；失败即 `invalid`。
- 单条失败（超时 / 非零退出 / 非法 JSON / 干跑校验不过）隔离在该条：记 `invalid` + 原因、保留原始输出，
  不重试、不中断整批。`checks` 四项在「未评估」（调用失败 / 无 JSON）时为 `null`，只有真跑了干跑校验才是
  `true/false`——不要把 `null` 读成「校验不过」。

## 已知副作用与边界（如实声明）

- **prompt 只能走 argv**：`kimi -p -`（从 stdin 读）实测不可用——kimi 2.0.1 把字面 `-` 当 prompt
  （2026-09-19 验证），故渲染后全文作为子进程 argv 传入；单条 prompt 受 macOS `ARG_MAX`（约 1MB）限制，
  现样本约 11KB，无实际风险。
- **argv 瞬时撞调度器安全阀（已实测，影响很小）**：`kimi -p` 的全文 argv 只在进程启动瞬间可见——
  kimi 2.0.1 随即把进程名改写为 `kimi-code`（2026-09-19 实测：单次调用 43s，全文 argv 可见窗口约 0.12s；
  0.05s 轮询 320 次仅命中 2 次）。该窗口内调度器 `_stray_acp_pids()`（`pgrep -f "kimi acp"`，15s 节奏，
  仅在有待自动重启时执行）可能命中本子进程 → 本轮自动重启顺延；人工按 AGENTS.md 执行
  `pgrep -f "kimi acp"` 检查时也可能看到它。渲染结果命中字面 `kimi acp` 时工具在 stderr 与 `run.log`
  打一条 `warn`。**不影响**需求助手 `restart_dispatcher` 预检（`_dispatcher_kimi_agents()` 只扫
  调度器进程树后代）与看板网页端重启入口（有意不做该预检）。复查方法：调用期间以 0.05s 轮询
  `pgrep -f "kimi acp"`，观察是否出现 `~/.kimi-code/bin/kimi -p ...` 进程。
- **样本默认不持久**：`out/` 在仓库内（已 gitignore），dev 在 worktree 内跑出的样本随 worktree 回收消失；
  长期采集请把 `--out` 指到仓库外固定目录（如 `~/flow-probe-out`）。
- **kimi 侧留会话目录**：每次调用在 `KIMI_CODE_HOME/sessions/` 下留一个 `wd_flow-probe-*` 会话目录
  （`kimi -p` 自身行为，**不进**调度器 `agent_sessions` 注册表）；介意可手工清理或换用独立 `KIMI_CODE_HOME`。
- **输入量级**：prompt 内联整份 `analysis.md`（现样本约 10KB / 3k tokens），未做截断；超长 analysis 会推高
  单次调用成本，也可能触 `ARG_MAX`（以 `invalid`「调用失败」收场，不中断整批）。

## 删除

```bash
rm -rf scripts/flow-probe/     # 并还原 .gitignore 中 scripts/flow-probe/out/ 一行
```

工具与生产零耦合（不 import 生产模块、不被任何测试引用），删除后系统行为与删除前完全一致。
