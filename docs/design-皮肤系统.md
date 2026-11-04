# 看板皮肤系统设计方案：深空玻璃（默认）/ 浅色双皮肤

> 状态：令牌表与设计语言定稿，落地见技术方案 `artifacts/loop-engine/req-15/tech.md`。
> 视觉唯一基准 = 素材原型 `materials/index (1).html`（深色 = `:root` + 深色组件规则；浅色 = `body.light-mode`）。两套皮肤逐值对齐原型，不以主观观感替代。
> 本文件是令牌的**唯一文档源**：表 A（原型原子令牌）、表 B（结构令牌）、表 C（语义色）。口径标注词：**同原型** / **保持改造前** / **派生** / **差异理由**。

## 一、皮肤机制

- **单一源** = CSS 变量两层：`:root`（深色 = 默认）+ `:root[data-skin="light"]`（浅色覆盖，只写取值不同的令牌）。
- 组件规则**只消费令牌**：改皮肤 = 换令牌取值，选择器与规则文本不动；不写前缀式覆盖（如 `body[data-skin] .chat-status`），避免双色源与状态卡片「规则无视图前缀」硬约束冲突。
- **皮肤标识挂 `<html>`（`:root`）**，非 `body`：`<head>` 内联脚本可在首帧前设置，避免浅色闪烁；`html[data-skin="light"]` 特异性高于 `:root`，覆盖必然生效。
- **缺省无属性 = 深色**：`localStorage` 无值或非法值都落深色，符合「默认深色」。
- **切换入口** = 页头 `.head-right` 内、`#healthBadge` 之前的按钮 `#skinToggle`：图标 + 文字，文字显示**目标皮肤**（深色下显示「浅色皮肤」），文案节点 `#skinToggleLabel`，aria-label 同步。
- **localStorage 键 = `reqBoardSkin`**：仅当值为 `light` 才写 `data-skin` 属性；`toggleSkin()` / `applySkin(skin)` 负责属性、按钮文案、aria、落值。
- **首帧防闪**：`<head>` 最前内联 `skinBoot` 脚本（读 `reqBoardSkin`，`light` 才设属性），CSS 外链（令牌块）紧随其后、阻塞加载，必须早于 `<body>`。
- **布局零位移**：尺寸/间距/字号/位置一律不进皮肤覆盖；滚动条只改**颜色**（`scrollbar-color`），不设 `scrollbar-width`、不写 `::-webkit-scrollbar`。

## 二、口径规则（逐条标注）

- **同原型**：原型 `body.light-mode` 有明确覆盖值的令牌与组件（六面全部属此列）→ 浅色取原型值。
- **保持改造前**：原型未定义的 board 独有组件（徽标、流程 chip、状态卡片、调度器角标、状态选择器、toast、模型设置 / 机器人抽屉表单、建卡页、需求助手、代码 / 产物块）→ 浅色取改造前字面量。
- **派生**：上述 board 独有档的深色值（原型未定义），按统一规则派生——底色 = 语义亮色 12%~16% 透明（沿原型 `.status` 手法），描边 = 30%~35% 透明，前景 / 圆点 = 语义亮档。
- **差异理由**：受硬约束或本卡范围排除而不照原型者，逐条在表内或下方注明。
- **原型尺寸类不采用（差异理由）**：原型值属尺寸 / 间距 / 字号 / 位置（如 `.theme-toggle` 36px、`.robot` 38px、`.sidebar` 256px、`.drawer` 右距 18px）→ 一律不取，只取颜色 / 边框 / 阴影 / 圆角 / 模糊 / 动效。

## 三、浅色六面基线变化（**基线切换，非回归**）

浅色六面本就与原型不同，下列变化属**基线切换而非回归**，须在 analysis / self-test / accept 逐条明示：

| 面 | 改造前 | 切换后（原型浅色） |
|:--|:--|:--|
| 画布 | `#eef1f5` | `#fafafa` |
| 面板 / 卡片 | 白底带内高光 / 阴影 | 白底、**无内高光、无阴影** |
| 卡片选中态 | 浅蓝底 | **白底 + 蓝边 + 蓝环**（`--card-sel-border/--card-sel-shadow`） |
| 次级字 | `#5f6774` | `#6b7280` |
| 面板圆角 | 8px | **14px**（`--radius` 浅色未覆盖，继承深色 14px，两皮肤一致） |

## 四、表 A · 原型原子令牌（逐值）

| 变量名 | 深色（`:root`） | 浅色（`:root[data-skin="light"]`） | 用途 | 口径 |
|:--|:--|:--|:--|:--|
| `--canvas-bg` | `#050d18` | `#fafafa` | body 画布底 | 同原型 |
| `--fluid-color-a` | `#06203c` | `#f4f8ff` | 流体底色（冷） | 自研（#16；深=高饱和冷 / 浅=低饱和冷） |
| `--fluid-color-b` | `#0b4f7a` | `#dbeafe` | 流体主色（冷蓝） | 自研（#16） |
| `--fluid-color-c` | `#22d3ee` | `#bfdbfe` | 流体高光（青 / 近白蓝，低用量） | 自研（#16） |
| `--fluid-veil` | `#050d18` | `#fafafa` | 流体压底混色（= 画布底） | 自研（#16） |
| `--fluid-opacity` | `0.9` | `0.6` | 流体 canvas 整层不透明度 | 自研（#16；浅色更淡） |
| `--fluid-fallback` | 径向+线性渐变 | 同式浅色版 | WebGL2 不可用 / 降级 / reduced-motion 的静态渐变底 | 自研（#16） |
| `--glass-bg` | `rgba(5,22,48,.72)` | `#fff` | 玻璃面板底 | 同原型 |
| `--glass-strong` | `rgba(4,18,40,.9)` | `#fff` | 抽屉 / 弹窗实体底 | 同原型 |
| `--glass-border` | `rgba(141,203,255,.16)` | `#e5e7eb` | 1px 冷色边框 | 同原型 |
| `--glass-blur` | `14px` | `14px` | 模糊半径 | 同原型 |
| `--glass-filter` | `blur(14px) saturate(1.2)` | `none` | `backdrop-filter` 消费值（浅色去模糊） | 同原型 |
| `--glass-shadow` | `0 12px 32px rgba(0,0,0,.16)` | `0 1px 2px rgba(17,24,39,.035)` | 面板投影 | 同原型 |
| `--glass-highlight` | `rgba(255,255,255,.045)` | `transparent` | 面板 inset 顶白（浅色无该 inset） | 同原型 |
| `--text` | `#edf8ff` | `#111827` | 正文 | 同原型 |
| `--muted` | `#8da9c5` | `#6b7280` | 次级文字 | 同原型 |
| `--accent` | `#3b82f6` | `#2563eb` | 强调 | 同原型 |
| `--cyan` | `#22d3ee` | `#2563eb` | 青（选中 / 焦点 / 呼吸） | 同原型 |
| `--success` | `#26d69a` | `#22c55e` | 成功 | 同原型 |
| `--orange` | `#e05e36` | `#b45309` | 橙 | 同原型 |
| `--danger` | `#ff5b6e` | `#be123c` | 危险 | 同原型 |
| `--radius` | `14px` | 未覆盖 → `14px` | 面板圆角 | 同原型＋差异理由：浅色原型未覆盖该令牌，两皮肤一致由 8px 连带变 14px |
| `--mono` | `SFMono-Regular,Consolas,monospace` | 同 | 数字 / 项目名 / 编号 | 同原型 |
| `color-scheme` | `dark` | `light` | 原生控件 / 滚动条 chrome | 同原型 |
| （排除）`--display` / `--body` | — | — | Inter / Noto Sans SC 显示字体 | 差异理由：不外链字体，沿看板系统字体栈 |

## 五、表 B · 结构令牌（六面，浅色同原型）

| 变量名 | 深色 | 浅色 | 用途 | 口径 |
|:--|:--|:--|:--|:--|
| `--bg` | `var(--canvas-bg)` | `var(--canvas-bg)` `#fafafa` | 页面底 | 同原型 |
| `--surface` | `#041228` | `#fff` | 实心面底 | 同原型／派生 |
| `--border` | `var(--glass-border)` | `#e5e7eb` | 通用边框 | 同原型 |
| `--border-strong` | `rgba(141,203,255,.30)` | `#d1d5db` | 强调边框（`.task-card:hover` 边） | 同原型／派生 |
| `--headline-glow` | `rgba(147,197,253,.85)` | `rgba(96,165,250,.85)` | 页头底线来回流动高光（#20） | 自研（board 独有新增，两皮肤各给值） |
| `--text-2` | `#b7cce0` | `#6b7280` | 次级正文（原型 `.project-row` 字色） | 同原型 |
| `--text-3` | `#718da7` | `#9ca3af` | 三级文字（原型 `.artifact`） | 同原型 |
| `--accent-hover` | `#2563eb` | `#1d4ed8` | 强调 hover | 保持改造前／派生：原型无 hover 档 |
| `--ring` | `rgba(34,211,238,.10)` | `rgba(37,99,235,.15)` | 焦点环 | 保持改造前／派生 |
| `--hover-fill` | `rgba(255,255,255,.06)` | `#f3f4f6` | 悬浮填充 | 同原型 |
| `--sel-bg` | `rgba(34,211,238,.13)` | `#eff6ff` | 选中底 | 同原型 |
| `--sel-bg-2` | `rgba(34,211,238,.10)` | `#dbeafe` | 选中底（二档） | 保持改造前／派生 |
| `--mask` | `rgba(0,5,14,.58)` | `rgba(0,5,14,.58)` | 遮罩 | 同原型：浅色未覆盖 → 沿用原型暗遮罩 |
| `--scroll-thumb` | `rgba(141,203,255,.25)` | `rgba(17,24,39,.25)` | 滚动条颜色（仅颜色） | 派生 |
| `--err-fg` | `#ff9aa6` | `#dc2626` | 错误前景 | 保持改造前／派生 |
| `--topbar-bg` | `rgba(3,14,33,.76)` | `#fff` | 顶栏底 | 同原型 |
| `--topbar-border` | `rgba(141,203,255,.12)` | `#e5e7eb` | 顶栏边 | 同原型 |
| `--topbar-filter` | `blur(14px)` | `none` | 顶栏模糊 | 同原型 |
| `--glass-nav` | `rgba(5,22,48,.72)` | `#fff` | 左栏玻璃底 | 同原型 |
| `--glass-list` | `transparent` | `transparent` | 中列玻璃底 | 同原型 |
| `--glass-detail` | `rgba(5,22,48,.72)` | `#fff` | 右栏玻璃底 | 同原型 |
| `--glass-project` | `rgba(4,18,40,.78)` | `#fff` | 摘要条玻璃底 | 同原型 |
| `--glass-nav-solid` | `#051628` | `#fff` | 左栏降级实体底 | 同原型／派生 |
| `--glass-list-solid` | `transparent` | `transparent` | 中列降级实体底 | 同原型／派生 |
| `--glass-detail-solid` | `#051628` | `#fff` | 右栏降级实体底 | 同原型／派生 |
| `--glass-project-solid` | `#041526` | `#fff` | 摘要条降级实体底 | 同原型／派生 |
| `--panel-shadow` | `0 12px 32px rgba(0,0,0,.16)` | `0 4px 14px rgba(17,24,39,.055)` | 摘要条专用投影 | 同原型 |
| `--panel-inset-top` | `rgba(255,255,255,.08)` | `rgba(255,255,255,.9)` | 摘要条 inset 顶白 | 同原型 |
| `--panel-inset-bottom` | `color-mix(in srgb,var(--accent) 18%,transparent)` | `color-mix(in srgb,var(--accent) 10%,transparent)` | 摘要条 inset 底光 | 同原型 |
| `--panel-grad` | `linear-gradient(105deg,color-mix(in srgb,var(--accent) 9%,transparent),transparent 32%)` | 同式 `5%` / `30%` | 摘要条渐变层 | 同原型 |
| `--pb-name-glow` | `color-mix(in srgb,var(--cyan) 34%,transparent)` | `color-mix(in srgb,var(--accent) 18%,transparent)` | 项目名微光 | 同原型 |
| `--card-bg` | `rgba(4,19,43,.62)` | `#fff` | 卡片底 | 同原型 |
| `--card-bg-hover` | `rgba(7,27,56,.78)` | `#fff` | 卡片 hover 底 | 同原型 |
| `--card-border` | `var(--glass-border)` | `#e5e7eb` | 卡片边 | 同原型 |
| `--card-border-hover` | `rgba(106,188,255,.34)` | `#d1d5db` | 卡片 hover 边 | 同原型 |
| `--card-shadow` | `inset 0 1px 0 rgba(255,255,255,.025)` | `none` | 卡片影（浅色无影） | 同原型 |
| `--card-shadow-hover` | `inset 0 1px 0 rgba(255,255,255,.025)` | `none` | 卡片 hover 影 | 同原型 |
| `--card-sel-border` | `var(--cyan)` | `#2563eb` | 选中卡边 | 同原型 |
| `--card-sel-shadow` | `0 0 0 1px rgba(34,211,238,.18),0 0 14px rgba(34,211,238,.07),inset 0 1px 0 rgba(255,255,255,.06)` | `0 0 0 1px rgba(37,99,235,.08)` | 选中卡环（浅色 = 白底 + 蓝边 + 蓝环） | 同原型 |
| `--card-title-fg` | `#dcecff` | `#111827` | 卡片标题 | 同原型 |
| `--card-meta-fg` | `var(--muted)` | `#6b7280` | 卡片元信息 | 同原型 |
| `--sf-role-fg` | `#a9bed1` | `#9ca3af` | 会话行角色名 | 同原型 |
| `--sf-guid-fg` | `#63b6ff` | `#2563eb` | 会话 GUID | 同原型 |
| `--sf-guid-active-fg` | `#7ee9ff` | `#2563eb` | 运行中 GUID | 同原型 |
| `--sf-artifact-fg` | `#718da7` | `#9ca3af` | 产物名 | 同原型 |
| `--sf-list-bd` | `rgba(141,203,255,.16)` | `#e5e7eb` | 会话列表虚线边 | 同原型 |
| `--h3-fg` | `#a9c4dc` | `#6b7280` | 小节标题 | 同原型 |
| `--rich-bg` | `rgba(2,12,29,.38)` | `#f8fafc` | 富文本块底 | 同原型 |
| `--rich-bd` | `rgba(141,203,255,.13)` | `#e5e7eb` | 富文本块边 | 同原型 |
| `--rich-fg` | `#c6d9ea` | `#374151` | 富文本块字 | 同原型 |
| `--io-bg` | `rgba(2,12,29,.45)` | `#f8fafc` | I/O 框底 | 同原型 |
| `--io-pre-fg` | `#cce2f5` | `#374151` | I/O 框正文 | 同原型 |
| `--tl-fg` | `#aec5d8` | `#4b5563` | 时间线正文 | 同原型 |
| `--tl-time-fg` | `#6f8ca6` | `#9ca3af` | 时间线时刻 | 同原型 |
| `--field-bg` | `rgba(0,0,0,.25)` | `#fff` | 表单字段底 | 同原型 |
| `--field-bd` | `var(--glass-border)` | `#d1d5db` | 表单字段边 | 同原型 |
| `--field-fg` | `#fff` | `#111827` | 表单字段字 | 同原型 |
| `--btn-bg` | `rgba(255,255,255,.04)` | `#fff` | 按钮底 | 同原型 |
| `--btn-bg-hover` | `rgba(255,255,255,.09)` | `#f3f4f6` | 按钮 hover 底 | 同原型 |
| `--btn-danger-fg` | `#ff9aa6` | `#be123c` | 危险按钮字 | 同原型 |
| `--btn-neutral-bg` | `#16233a` | `#e5e7eb` | 次要档实心底（描边取同值 = 无可见线框） | 差异（#17 新增） |
| `--btn-neutral-fg` | `#dcecff` | `#374151` | 次要档文字 | 差异（#17 新增） |
| `--btn-neutral-bg-hover` | `#1e2f4a` | `#d1d5db` | 次要档 hover 底（同时作 hover `border-color`） | 差异（#17 新增） |
| `--radius-card` | `12px` | 同 | 卡片圆角 | 同原型 |
| `--radius-ctl` | `9px` | 同 | 控件圆角 | 同原型 |
| `--radius-chip` | `10px` | 同 | chip 圆角 | 同原型 |
| `--radius-box` | `10px` | 同 | 内容框圆角 | 同原型 |
| `--radius-drawer` | `18px` | 同 | 抽屉圆角 | 同原型 |
| `--radius-modal` | `16px` | 同 | 弹窗圆角 | 同原型 |
| `--code-bg` | `rgba(2,12,29,.45)` | `#eef4ff` | 代码块底 | 保持改造前／派生 |
| `--code-fg` | `#cce2f5` | `#3a5b94` | 代码块字 | 保持改造前／派生 |

## 六、表 C · 语义色（board 独有档）

浅色 = 改造前字面量（**保持改造前**）；深色按统一规则**派生**。

| 组 | 令牌（`-bg` / `-fg` / `-bd` / `-pt`） | 浅色（保持改造前） | 深色（派生） |
|:--|:--|:--|:--|
| 徽标 open | `--st-open-*` | `#fffbeb` / `#b45309` / `#f59e0b` | `rgba(245,158,11,.15)` / `#fbbf24` / `#f59e0b` |
| 徽标 later | `--st-later-*` | `#f5f3ff` / `#7c3aed` / `#a78bfa` | `rgba(167,139,250,.15)` / `#c4b5fd` / `#a78bfa` |
| 徽标 doing | `--st-doing-*` | `#eff6ff` / `#2563eb` / `#3b82f6` | `rgba(59,130,246,.15)` / `#93c5fd` / `#3b82f6` |
| 徽标 done | `--st-done-*` | `#f0fdf4` / `#15803d` / `#22c55e` | `rgba(38,214,154,.14)` / `#56e9b7` / `#22c55e` |
| 徽标 route | `--st-route-*` | `#fff7ed` / `#c2410c` / `#f97316` | `rgba(249,115,22,.15)` / `#fdba74` / `#f97316` |
| 徽标 review | `--st-review-*` | `#fef2f2` / `#b91c1c` / `#ef4444` | `rgba(239,68,68,.15)` / `#fca5a5` / `#ef4444` |
| 徽标 dropped | `--st-dropped-*` | `#f3f4f6` / `#4b5563` / `#9ca3af` | `rgba(148,163,184,.13)` / `#94a3b8` / `#9ca3af` |
| 状态选择器描边 | `--st-<name>-bd` | `#c4b5fd` / `#fbbf24` / `#60a5fa` / `#4ade80` / `#9ca3af` | 深色取上表 30% 透明（later/open/doing/done/dropped） |
| 流程 chip | `--cf-{green,indigo,fuchsia,slate}-{bg,fg}` | `#f0fdf4`/`#15803d`、`#eef2ff`/`#4338ca`、`#fdf4ff`/`#a21caf`、`#f8fafc`/`#64748b` | `rgba(38,214,154,.14)`/`#56e9b7`、`rgba(99,102,241,.16)`/`#a5b4fc`、`rgba(217,70,239,.16)`/`#f0abfc`、`rgba(148,163,184,.13)`/`#94a3b8` |
| 状态卡片五档 | `--cs-{green,amber,red,blue,gray}-{bg,bd,fg}` | `#f0fdf4`/`#bbf7d0`/`#15803d`、`#fffbeb`/`#fde68a`/`#b45309`、`#fef2f2`/`#fecaca`/`#b91c1c`、`#eff6ff`/`#bfdbfe`/`#1d4ed8`、`#f9fafb`/`var(--border)`/`#6b7280` | `rgba(38,214,154,.12)`/`rgba(38,214,154,.30)`/`#56e9b7`、`rgba(245,158,11,.12)`/`rgba(245,158,11,.32)`/`#fbbf24`、`rgba(255,91,110,.12)`/`rgba(255,91,110,.32)`/`#ff9aa6`、`rgba(59,130,246,.12)`/`rgba(59,130,246,.32)`/`#93c5fd`、`rgba(141,203,255,.06)`/`var(--glass-border)`/`#8da9c5` |
| 判定值列 | `--cs-ok` / `--cs-warn` / `--cs-bad-fg` | `#15803d` / `#b45309` / `#b91c1c` | `#56e9b7` / `#fbbf24` / `#ff9aa6` |
| 状态卡片细节 | `--cs-detail-bg/-bd`、`--cs-btn-bg/-hover`、`--cs-head-hover` | `rgba(255,255,255,.6)` / `rgba(0,0,0,.07)` / `#fff` / `#fff5f5` / `rgba(255,255,255,.45)` | `rgba(141,203,255,.06)` / `rgba(141,203,255,.12)` / `rgba(255,255,255,.06)` / `rgba(255,255,255,.12)` / `rgba(255,255,255,.06)` |
| 调度器角标 | `--health-{ok,warn,bad}-{fg,dot}` | `var(--text-2)`/`#16a34a`、`#b45309`/`#f59e0b`、`#dc2626`/`#dc2626` | `#56e9b7`/`#26d69a`、`#fbbf24`/`#f59e0b`、`#ff9aa6`/`#ff5b6e` |
| 全局提示 | `--toast-bg/-bd`、`--toast-err-bg`、`--warn-bg/-bd/-fg` | `#fff`/`#d1d5db`、`#dc2626`、`#fffbeb`/`#fde68a`/`#b45309` | `rgba(4,18,40,.92)`/`var(--glass-border)`、`#ff5b6e`、`rgba(245,158,11,.12)`/`rgba(245,158,11,.32)`/`#fbbf24` |
| 项目条指标数字（#21） | `--pb-stat-{total,doing,done,rate,tokens,reject}` | `#2563eb`/`#0e7490`/`#16a34a`/`#0d9488`/`#4f46e5`/`#be123c` | `#3b82f6`/`#22d3ee`/`#26d69a`/`#5eead4`/`#818cf8`/`#ff5b6e`（打回=危险档，其余蓝/青/绿/teal/靛蓝） |

## 七、设计语言条目

**深空玻璃（默认深色）**

- 画布 `#050d18` + 流体置换背景（`#fluidBg` 全屏 WebGL2 canvas，令牌 `--fluid-*` 驱动；位于内容层 `.page`（z-index:1）之下、`pointer-events:none`）。
- 玻璃面板 `rgba(5,22,48,.72)` + `blur 14px`；1px 冷色边框 `rgba(141,203,255,.16)`。
- 圆角 14px；强调 `#3b82f6` + 青 `#22d3ee`；品牌渐变光晕（页头 h1 渐变文字 + 光晕，`@supports (background-clip:text)` 包裹，兜底 `color: var(--accent)`）。
- 指标等宽字（`--mono`）+ 微光；运行中会话 GUID 呼吸 2.2s（关键帧 `sfIdBreath`：`0%,100% opacity .62` / `50% opacity 1` + 双层青光晕）。

**浅色**

- `#fafafa` 画布 + 低饱和冷色流体（`--fluid-*` 浅色档，`--fluid-opacity: .6` 更淡）；**无模糊**（`--glass-filter: none`）。
- 页头 h1 纯 `#2563eb` 无光晕。

## 八、切换机制要点

| 项 | 值 |
|:--|:--|
| 皮肤标识 | `<html>` 的 `data-skin`（缺省 = 深色） |
| localStorage 键 | `reqBoardSkin` |
| 写属性条件 | 值 `light` 才写 `data-skin` |
| 页头按钮 | `#skinToggle`（文案节点 `#skinToggleLabel`，显示**目标皮肤**；高 24px） |
| 首帧防闪 | `<head>` 最前内联 `skinBoot`，CSS 外链（令牌块）紧随其后、`<body>` 之前 |
| 函数 | `applySkin(skin)` / `toggleSkin()`；常量 `SKIN_KEY = 'reqBoardSkin'` |

## 九、三处对照表约定

`analysis.md` / `self-test.md` / `accept.md` 各含**一张逐值对照表**，逐条列「令牌 / 深色值 / 浅色值 / 口径」，作为方案、自验、验收三方共用的比对基准：

- 六面（表 B）浅色 == 原型 `body.light-mode` 值；其余面（表 C）浅色 == 改造前字面量。
- 深色语义色（表 C）标「派生 + 规则」，验收按「语义一致 + 对比度达标」判定，不要求等于原型。
- 浅色六面基线变化（第三章）须显式标注「基线切换，非回归」。
