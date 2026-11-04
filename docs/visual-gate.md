# 种子数据测试实例与视觉闸门

面向在 worktree 里做前端 / 样式改动的 agent 与人工自验：先起一个**数据确定**的测试实例，再用无头浏览器把
「看起来对不对」变成可机读的闸门结论。全程不碰 main 实例（8899）与真实数据。

## 一、种子数据测试实例（`--seed`）

```bash
# worktree 无 venv 时用 REQ_BOARD_PYTHON 指向 main 工作区的解释器
REQ_BOARD_PYTHON=<python> scripts/test_instance.sh start 8931 --seed
REQ_BOARD_PYTHON=<python> scripts/test_instance.sh stop 8931      # 连同临时数据目录一并删除
```

`--seed` 做三件事：

1. 建每端口临时数据目录 `/tmp/req_board_test_<PORT>_data`；
2. 跑 `scripts/seed_test_db.py --data-dir <该目录>` 写入固定数据集；
3. 以隔离环境启动看板：

| 环境变量 | 取值 | 作用 |
|:--|:--|:--|
| `REQ_BOARD_DATA` | 临时数据目录 | `board.db` / 心跳 / `dispatcher_state.json` 的位置（缺省仍是仓库根 `data/`） |
| `REQ_BOARD_PROJECTS` | `<数据目录>/projects.json` | 项目注册表（`project_list()` 也已跟随，不再串读真实注册表） |
| `REQ_BOARD_FREEZE_TIME` | `2026-01-15 10:30:00` | 冻结时钟（北京时间），见下 |
| `REQ_BOARD_NO_FEISHU` / `REQ_BOARD_SSE` | `1` / `0` | 关飞书通道与 SSE 长连接 |
| `REQ_BOARD_ARTIFACTS` / `REQ_BOARD_MATERIALS` / `REQ_BOARD_FEISHU_JSON` / `KIMI_CODE_HOME` | 数据目录下的子路径 | 产物、素材、飞书凭据、kimi 配置都不读本机真实文件 |

不带 `--seed` 的 `start/stop/status` 行为与原先一致。

### 数据集内容

- 3 个演示项目（`demo-alpha` / `demo-beta` / `demo-gamma`，路径指向不存在的 `/tmp/req-board-seed/...`，看板对缺失目录宽容）；
- 每个项目 × 每个状态（later / open / doing / done / dropped）× `flows.json` 里的每个流程各一张卡（流程动态探测），
  含长 / 短标题、长 Markdown 备注、多行 `[auto]` 开发总结；
- `req_status_log`（baseline + 流转）、`agent_sessions`（仅终态：idle / failed / 毒会话，**不种 running / interrupted**，
  避免看板启动恢复逻辑拉起 kimi）、`agent_runs`、`agent_events`、`dispatcher_events`（flow_choice / spawn / model /
  phase / retry / merge / action / confirm）、`req_reviews`（pass 与 reject）；
- `dispatcher_state.json`（进行中卡的会话信息栏 + 一条待确认）；**不写心跳文件**——种子实例由脚本带 `REQ_BOARD_NO_FEISHU=1` 启动，页头角标恒为「显式停用」（不是真实调度器状态）。

所有时间戳由冻结时刻倒推，不读系统时钟；同一份 `flows.json` 下两次落库的全表逻辑转储完全一致
（`seed_test_db.dump_db()`，测试 `tests/test_seed_data.py` 钉死）。

### 安全闸门

`seed_test_db.py` 拒绝写入：本仓库与主工作区的 `data/`、`~/.req-board` 下任何路径；目标库已有数据时须显式 `--force`
（删库重建）。种子数据只能落临时目录。

### 冻结时钟 `REQ_BOARD_FREEZE_TIME`

值为北京时间 `YYYY-MM-DD HH:MM:SS`（非法值拒绝启动）。设置后：

- 服务端 `now_str()`、`/api/agent-sessions` 的 `server_now`、心跳 `age` 取该固定时刻；
- 首页 `<head>` 最前注入一段内联脚本，把浏览器 `Date.now()` / 无参 `new Date()` 钉到同一时刻。

未设置时响应逐字节不变。冻结期间新写入的行（如在测试实例里手工建卡）时间戳也是冻结时刻，属预期。

## 二、视觉闸门 `scripts/visual_gate.py`

纯 Python + Chrome DevTools Protocol（venv 自带的 `websockets`），不依赖 playwright / selenium。浏览器默认取
playwright 缓存里的 chromium headless shell，其次本机 Google Chrome；可用 `VISUAL_GATE_CHROME` 指定。

```bash
# 只看候选页
<python> scripts/visual_gate.py --url http://127.0.0.1:8931/ --skins dark,light --viewports 1440x900

# 与基线比对（基线 = 另一个 worktree / 改动前代码起的种子实例）
<python> scripts/visual_gate.py --url http://127.0.0.1:8931/ --baseline-url http://127.0.0.1:8932/ \
    --contract contract.json --out /tmp/vg-out

<python> scripts/visual_gate.py --help      # 全部参数与闸门说明
```

每个「皮肤 × 视口」加载一次页面：`prefers-reduced-motion: reduce`、注入
`*{animation:none!important;transition:none!important}` 与 `#fluidBg{visibility:hidden}`、冻结 `Date`
（`--freeze-time`，默认与种子数据同为 `2026-01-15 10:30:00`）、时区固定 Asia/Shanghai；等网络静默后冻结页面
定时器再采集（采集期间页面不会被轮询重渲染）。

| 闸门 | 判定 |
|:--|:--|
| `console` | 控制台 error、未捕获异常、同源请求失败或 4xx/5xx → 失败 |
| `tokens` | 采集 `:root` 全部 CSS 自定义属性；有基线时取值不同即失败 |
| `computed_style` | 探针（按钮 / 输入 / 下拉 / 文本域 / 链接 / 卡片，每类最多 12 个）在常态、强制 `:hover`、强制 `:focus`+`:focus-visible` 下的关键计算样式。可聚焦控件须有可见焦点指示（outline 宽度 > 0 且非透明，或 box-shadow 变化）；hover 不得改变包围盒；有基线时列出属性差异 |
| `layout_shift` | 需基线：同键元素（唯一 id 优先，否则 CSS 路径）包围盒 dx/dy/dw/dh 超过 0.5px → 失败 |
| `contrast` | 可见文本 WCAG 对比度（正文 4.5，≥24px 或 ≥18.66px 粗体 3.0）；背景沿祖先逐层 alpha 合成，遇渐变 / 图片记 `undetermined`（不算通过也不算失败） |
| `native_controls` | 仅 dark 皮肤：`select` / `input` / `textarea` 有效背景偏亮，或控件 / 可滚动容器 `color-scheme` 不含 `dark`（滚动容器自定义了 `scrollbar-color` 则不报） |

产物在 `--out` 目录：`visual-report.json`（`status`、`meta`、每组 `runs[].gates.<闸门>.{status,counts,failures,truncated}`，
失败清单每闸门最多 50 条）、`visual-report.html`（自包含，汇总表 + 失败明细 + 截图，有基线时并排）、每组一张 PNG。

退出码：`0` 全部通过 / `1` 有闸门失败 / `2` 工具错误（找不到 Chromium、页面不可达、参数或契约非法）。

### 契约文件（可选）

```json
{"allow": {"tokens": ["--accent"],
           "contrast": ["--text-3", ".muted"],
           "computed": ["#saveBtn"],
           "layout": ["#navList*"]},
 "probes": [{"key": "save", "selector": "#saveBtn"}]}
```

- `allow.tokens` / `allow.computed` / `allow.layout`：令牌名或探针键，支持 fnmatch 通配；`allow.computed` 命中的探针
  其焦点、hover、基线差异一并放行；
- `allow.contrast`：以 `--` 开头视为颜色令牌（文本色等于该令牌解析值即放行，如已知低于 AA 的 `--text-3`），
  否则视为 CSS 选择器（元素自身或祖先命中即放行）；
- `probes`：追加自定义探针；
- 未知顶层键 / 未知 `allow` 键直接报错退出（退出码 2）。`console` 与 `native_controls` 没有放行通道。

### 已知边界（第一版）

- 有效背景按 DOM 祖先链推算，不处理绝对定位叠放到非祖先元素之上的情况；`#fluidBg` 被隐藏，半透明卡片
  按纯色 `--bg` 之上合成。
- 强制伪类只作用于探针元素自身：焦点环画在父级（`:focus-within`）或伪元素上的控件会被报缺失，用
  `allow.computed` 放行。
- CSS 路径键含 `nth-of-type`：候选页增删节点会让其后同级元素的键漂移，表现为 `added` / `missing`。
- 只采集首屏默认视图（不点击、不打开抽屉 / 弹窗）；整体在画布外的元素不做对比度检查。
