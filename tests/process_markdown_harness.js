// req-114 / req-271 回归测试 harness（由 tests/test_process_markdown_merge.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的纯函数源码
// （function escapeHtml / function renderArtifactMd / function mergeMessageBlocks），
// 本文件随后定义 document 桩、复刻 refreshProcess 的渲染映射（renderBlocks）与
// chatEventsHtml 的渲染映射（renderChatBlocks，#271 建卡对话页）并逐条断言。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

// 模拟浏览器 DOM 的 textContent→innerHTML 转义行为（& < > 转义，引号不转义，
// 与真实 DOM 在元素文本节点的行为一致）；escapeHtml 依赖 document.createElement。
function makeEscDiv() {
    let text = '';
    return {
        set textContent(v) { text = String(v == null ? '' : v); },
        get innerHTML() {
            return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
    };
}
global.document = { createElement: () => makeEscDiv() };

// board.py 内嵌 JS 的模块级状态（#422 会话头部折叠态与路径 store）：函数源码被提取后
// 由 harness 提供同名桩，供 T20 断言折叠态回填与「完整路径不进 HTML 属性」
var sessHeadOpen = {};
var sessPathStore = {};

const results = [];
function check(name, pass, detail) {
    results.push({ name: name, pass: !!pass, detail: detail == null ? '' : String(detail) });
}

// 复刻 board.py refreshProcess 的渲染映射（ev-msg 走 renderArtifactMd，tool/system 走 escapeHtml）
function renderBlocks(blocks) {
    return blocks.map(b => {
        const cls = b.kind === 'tool' ? 'ev-tool' : (b.kind === 'system' ? 'ev-sys' : 'ev-msg');
        const prefix = b.kind === 'tool' ? '\u2699 ' : '';
        const body = cls === 'ev-msg' ? renderArtifactMd(b.content) : escapeHtml(b.content);
        return `<div class="ev-item ${cls}">${prefix}${body}</div>`;
    }).join('');
}

// ---- T1：复刻真实现场 session_f34ca851-f6b3-4538-aaba-f2f4213765cb 事件 8966/8967 ----
{
    const events = [
        { id: 8965, kind: 'tool', content: 'mcp__reqboard-tools__read_file' },
        { id: 8966, kind: 'message', content: '完成。需求 #113 proposal 阶段产出：\n\n**产物**\n- `~/...`\n\n**生效方式**：涉及 dispatcher.py（+acp_shared.py），合并后需手动重启 `local.automation.req-board-dispatcher`，由维护者触发。\n\n**判定' },
        { id: 8967, kind: 'message', content: '**：需求原文 + analysis 已很具体，无方向性歧义。' },
        { id: 8968, kind: 'system', content: '运行结束（done）' },
    ];
    const blocks = mergeMessageBlocks(events);
    const html = renderBlocks(blocks);
    check('T1_相邻message被拼接为一块且tool/system独立',
        blocks.length === 3
        && blocks[0].kind === 'tool' && blocks[2].kind === 'system'
        && blocks[1].kind === 'message' && blocks[1].content === events[1].content + events[2].content,
        'blocks=' + blocks.map(b => b.kind + '(' + b.content.length + '字)').join(','));
    check('T1_跨边界加粗正确渲染（<strong>判定</strong>：）',
        html.includes('<strong>判定</strong>：需求原文'),
        html.slice(html.indexOf('<strong>判定</strong>'), html.indexOf('<strong>判定</strong>') + 60));
    check('T1_不再出现两行裸星号（无 <p>**判定</p> / <p>**：</p>）',
        !html.includes('<p>**判定</p>') && !html.includes('<p>**：</p>'),
        '');
}

// ---- T2：文本超过消息缓冲阈值、Markdown 标记恰好被拆分的场景 ----
{
    const longText = '背景说明内容'.repeat(120);  // 720 字符，超过常见流式缓冲阈值量级
    const evA = { id: 11, kind: 'message', content: '第一段。\n\n' + longText + ' 此处有一个 **加粗' };
    const evB = { id: 12, kind: 'message', content: '内容** 需要跨事件后仍正常显示。' };
    const blocks = mergeMessageBlocks([evA, evB]);
    const html = renderBlocks(blocks);
    check('T2_长文本跨边界加粗还原', html.includes('<strong>加粗内容</strong>'), '');
    check('T2_拼接处无裸星号残留',
        !html.includes('**加粗') && !html.includes('内容**'),
        html.slice(html.indexOf('此处有一个'), html.indexOf('此处有一个') + 80));
}

// ---- T3：现有 Markdown 形态不退化（段落/标题/列表/行内代码/链接/围栏代码/表格） ----
{
    const md = [
        '# 一级标题',
        '',
        '普通段落，含 **加粗** 与 *斜体*。',
        '',
        '- 列表项一',
        '- 列表项二',
        '',
        '1. 有序一',
        '2. 有序二',
        '',
        '行内代码 `a**b` 与链接 [示例](https://example.com)。',
        '',
        '```',
        'fence content',
        '```',
        '',
        '| 列1 | 列2 |',
        '| --- | --- |',
        '| a | b |',
    ].join('\n');
    const html = renderArtifactMd(md);
    const need = ['<h1>一级标题</h1>', '<ul>', '<ol>', '<code>a**b</code>',
        '<a href="https://example.com">示例</a>', '<pre><code>fence content</code></pre>',
        '<table>', '<strong>加粗</strong>', '<em>斜体</em>'];
    const missing = need.filter(k => !html.includes(k));
    check('T3_现有Markdown形态全部正常', missing.length === 0, '缺失: ' + missing.join(' / '));
}

// ---- T4：tool/system 为明确边界，块数与顺序保持 ----
{
    const blocks = mergeMessageBlocks([
        { kind: 'message', content: '甲' },
        { kind: 'tool', content: '工具调用' },
        { kind: 'message', content: '乙' },
        { kind: 'message', content: '丙' },
        { kind: 'system', content: '系统事件' },
    ]);
    check('T4_边界切分正确且顺序不变',
        blocks.length === 4
        && blocks.map(b => b.kind).join(',') === 'message,tool,message,system'
        && blocks[2].content === '乙' + '丙',
        'blocks=' + blocks.map(b => b.kind).join(','));
}

// ---- T5：XSS 安全基线不因拼接而退化（先整体转义再语法转换） ----
{
    const events = [
        { kind: 'message', content: '**前**<script>alert(1)</script>' },
        { kind: 'message', content: '<img src=x onerror=alert(1)>后' },
    ];
    const html = renderBlocks(mergeMessageBlocks(events));
    check('T5_脚本标签被转义',
        !html.includes('<script>') && html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'),
        '');
    check('T5_非法图片语法不产生 <img>',
        !html.includes('<img') && html.includes('&lt;img src=x onerror=alert(1)&gt;'),
        '');
}

// ---- T6：空/缺失 content 不崩溃，拼接无损 ----
{
    const blocks = mergeMessageBlocks([
        { kind: 'message', content: null },
        { kind: 'message' },
        { kind: 'tool', content: null },
    ]);
    check('T6_空内容不崩溃且不误拼非message',
        blocks.length === 2 && blocks[0].content === '' && blocks[1].content === '',
        JSON.stringify(blocks));
}

// ---- T7：拆分点恰在换行处的段落仍保持两段（无分隔拼接不丢原文换行） ----
{
    const events = [
        { kind: 'message', content: '第一段内容。\n\n' },
        { kind: 'message', content: '第二段内容。' },
    ];
    const html = renderBlocks(mergeMessageBlocks(events));
    check('T7_换行处拆分拼接后仍是两个段落',
        html.includes('<p>第一段内容。</p>') && html.includes('<p>第二段内容。</p>'),
        html);
}

// 复刻 board.py chatEventsHtml 的渲染映射（#271：chat-msg 类名；user/tool/sys 走 escapeHtml，
// 其余走 renderArtifactMd；入参为已合并的 blocks）
function renderChatBlocks(blocks) {
    return blocks.map(e => {
        if (e.kind === 'user') return `<div class="chat-msg user">${escapeHtml(e.content)}</div>`;
        if (e.kind === 'tool') return `<div class="chat-msg tool">\u2699 ${escapeHtml(e.content)}</div>`;
        if (e.kind === 'system') return `<div class="chat-msg sys">${escapeHtml(e.content)}</div>`;
        return `<div class="chat-msg agent">${renderArtifactMd(e.content)}</div>`;
    }).join('');
}

// ---- T8：#271 建卡对话 chat 23 现场（事件 309/310 代码块围栏跨事件） ----
// 309 尾部 ```draft 开启（无闭合）、310 头部 ``` 闭合：旧逻辑逐条渲染时 310 单独渲染，
// 闭合围栏被误判为新的代码块开头，问题清单整体进入代码块、** 加粗裸显。
// 事件内容为 chat_events 表 chat_id=23 原样提取（309/310 均为 801 字符）。
{
    const ev309 = { id: 309, kind: 'message', content: "ystem_prompt` | 永远展示最新；dispatcher 顶层无副作用（`load_projects()` 仅读配置，飞书/主循环都在 `main()` 里），import 安全 | 要改 board.py（合并后自动重启生效）；board 进程加载 dispatcher 代码，需保持其顶层无副作用 |\n| C 抽共享模块 | 提示词移到独立 `prompts.py`，两边 import | 最干净 | 动 dispatcher.py 结构，需手动重启调度器 + 回归测试，改动面最大 |\n\n## 展示形式建议\n\n- 版块放「四、建卡对话」之后（或末尾），标题如「六、内置 agent 定义」\n- 开头一张**总览表**：角色 / 中文名 / 通道 / 阶段 / 权限 / 产物文件\n- 每个 agent 一个**折叠块**（`<details>` 默认收起，展开见提示词全文）：analysis / proposal / tech / dev_auto / dev_semi / accept，外加公共尾段 `COMMON_TAIL` 单独一块、建卡对话 agent 一块\n- 一张**占位符说明表**（`{notes}` 等是什么、谁填）\n- 折叠块样式沿用现有 guide 风格\n\n先出一版草稿（按方案 B 写）：\n\n```draft\n{\"title\": \"操作指南页新增「内置 agent 定义」版块\", \"notes\": \"背景：操作指南页（/guide）目前只有状态流转/回复格式/建卡对话说明，没有系统内置 agent 的介绍；调度器 6 套角色提示词（dispatcher.py ROLE_PROMPTS + 公共尾段 COMMON_TAIL）与建卡对话提示词（board.py _chat_system_prompt）分散在代码里，想知道每个 agent 的定位与提示词" };
    const ev310 = { id: 310, kind: 'message', content: "只能翻代码。\\n\\n方案要点：\\n- guide 页新增「内置 agent 定义」版块（建议放建卡对话一节之后）：总览表（角色/中文名/通道/阶段/权限/产物）+ 每 agent 折叠块展示提示词全文（analysis/proposal/tech/dev_auto/dev_semi/accept + 公共尾段 + 建卡对话 agent）+ 占位符说明表\\n- 数据动态读取保持单一事实来源：guide 路由渲染时从 dispatcher 模块取 ROLE_PROMPTS/COMMON_TAIL、从 board 本进程取 _chat_system_prompt，服务端拼进页面；提示词后续改动无需再同步文档\\n- 不改 dispatcher.py 提示词本体，不抽共享模块，避免动调度器；dispatcher 顶层仅 load_projects 无副作用，import 安全\\n- 现有 guide 版块与样式保持不变\\n\\n验收标准：\\n- /guide 出现新版块，6 个调度器角色提示词 + 公共尾段 + 建卡对话提示词全部可见（折叠可展开）\\n- 展示内容与 dispatcher.py/board.py 当前定义一致\\n- 页面上其它版块与样式不受影响\\n\\n生效方式：改动 board.py（guide 路由改为动态渲染）——合并后调度器自动重启看板，合并即生效；不改 dispatcher.py，无需重启调度器\"}\n```\n\n三个问题确认一下：\n\n1. **数据来源选哪个**？我推荐 B（动态读取）；如果你更看重「不动 board.py、改 guide.html 即可」的轻量，可以选 A（静态复制，但提示词更新需手动同步）。\n2. **提示词展示**：全部**折叠全文**可以吗？还是你只要摘要（角色定位 + 关键约束）？\n3. 版块位置放「四、建卡对话」**之后**（六）可以吧？" };
    // 缺陷复现：310 单独渲染（等价旧逻辑逐条渲染），闭合围栏被误判为新代码块开头
    const aloneHtml = renderChatBlocks([ev310]);
    check('T8_缺陷复现：310单独渲染时问题清单整体进入代码块（无列表结构）',
        aloneHtml.includes('<pre><code>') && !aloneHtml.includes('<ol>'),
        aloneHtml.slice(aloneHtml.indexOf('三个问题确认一下'), aloneHtml.indexOf('三个问题确认一下') + 60));
    check('T8_缺陷复现：310单独渲染时加粗裸显（** 原样出现）',
        aloneHtml.includes('**数据来源选哪个**') && !aloneHtml.includes('<strong>数据来源选哪个</strong>'),
        '');
    // 修复后：309+310 无分隔拼接再渲染
    const merged = renderChatBlocks(mergeMessageBlocks([ev309, ev310]));
    check('T8_合并后问题清单正常渲染为有序列表',
        merged.includes('<ol>') && merged.includes('<li><strong>数据来源选哪个</strong>') && merged.includes('<li><strong>提示词展示</strong>'),
        '');
    check('T8_合并后加粗生效且无裸星号',
        merged.includes('<strong>数据来源选哪个</strong>') && !merged.includes('**数据来源选哪个**'),
        '');
    check('T8_合并后draft草稿块仍为代码块展示',
        merged.includes('<pre><code>') && merged.includes('操作指南页新增'),
        '');
}

// ---- T9：#271 行内代码被从单词中间切开（chat 23 事件 308 尾部 `_chat_s / 309 头部 ystem_prompt`） ----
{
    const events = [
        { kind: 'message', content: 'guide 路由渲染时从 dispatcher 模块读 `ROLE_PROMPTS`/`COMMON_TAIL` + 本进程 `_chat_s' },
        { kind: 'message', content: 'ystem_prompt` 统一展示；' },
    ];
    const html = renderChatBlocks(mergeMessageBlocks(events));
    check('T9_跨事件行内代码合并后正确渲染',
        html.includes('<code>_chat_system_prompt</code>') && !html.includes('`_chat_s'),
        html.slice(html.indexOf('本进程'), html.indexOf('本进程') + 60));
}

// ---- T10：chat 渲染分支（user/tool/sys 独立展示、类名不变；单事件 message 形态不退化） ----
{
    const blocks = [
        { kind: 'user', content: '1.选 B\n2.折叠全文' },
        { kind: 'tool', content: 'Reading /Users/dev/project/req-board/board.py' },
        { kind: 'system', content: '会话创建中…' },
        { kind: 'message', content: '**加粗**与 `代码` 正常。' },
    ];
    const html = renderChatBlocks(blocks);
    check('T10_四分支类名与内容正确',
        html.includes('<div class="chat-msg user">1.选 B\n2.折叠全文</div>')
        && html.includes('<div class="chat-msg tool">\u2699 Reading /Users/dev/project/req-board/board.py</div>')
        && html.includes('<div class="chat-msg sys">会话创建中…</div>')
        && html.includes('<div class="chat-msg agent"><p><strong>加粗</strong>与 <code>代码</code> 正常。</p>')
        , html);
}

// ---- T11：#278 字面 \n / \"（agent 把草稿 JSON 转义文本粘贴进正文）反转义为真实换行 ----
// 复刻 chat_events 614（chat 29）现场形态：JSON 转义形式残留在消息正文（\n 为字面反斜杠+n）
{
    const md = '草稿如下：\n\n```draft\n{"title": "T", "notes": "背景：\\n- 2026-08-09 讨论\\n- 方案待定"}\n```\n\n**待确认**：\\n- 换行应生效\\n- 引号\\"应还原';
    const html = renderArtifactMd(md);
    check('T11_字面反斜杠n反转义后列表正常渲染',
        html.includes('<li>换行应生效</li>') && html.includes('<li>引号"应还原</li>'),
        html);
    check('T11_无字面反斜杠n/反斜杠引号残留',
        !html.includes('\\n- 换行应生效') && !html.includes('\\n- 引号') && !html.includes('\\"应还原'),
        '');
    check('T11_draft草稿块仍以代码块展示且加粗正常',
        html.includes('<pre><code>') && html.includes('"notes"') && html.includes('<strong>待确认</strong>'),
        '');
}

// ---- T12：#278 正常 markdown 消息（无字面转义）渲染回归不变，含路径反斜杠不误伤 ----
{
    const md = '**加粗**与 `代码` 正常。\n\n路径 C:\\Users\\dev 含反斜杠但不应被替换。';
    const html = renderArtifactMd(md);
    check('T12_正常markdown形态与普通反斜杠不受影响',
        html.includes('<strong>加粗</strong>') && html.includes('<code>代码</code>')
        && html.includes('<p>路径 C:\\Users\\dev') && html.includes('C:\\Users\\dev'),
        html);
}

// ---- T13：#280 draft JSON 围栏块自动格式化（2 空格缩进 + notes 换行真实呈现） ----
// 复刻 chat 25 现场形态：单行 JSON，notes 内换行为字面反斜杠+n（chat_events 存储原样，
// 未反转义）；格式化在入口反转义之前执行，stringify 输出再经反转义呈现真实换行。
{
    const md = '草稿如下：\n\n```draft\n{"title": "排版优化", "notes": "背景：\\n- 2026-08-10 讨论\\n- 方案待定"}\n```';
    const html = renderArtifactMd(md);
    const want = '<pre><code>{\n  "title": "排版优化",\n  "notes": "背景：\n- 2026-08-10 讨论\n- 方案待定"\n}</code></pre>';
    check('T13_单行draftJSON格式化为2空格缩进且notes换行真实呈现',
        html.includes(want),
        html.slice(html.indexOf('<pre><code>'), html.indexOf('</code></pre>') + 14));
    check('T13_格式化输出无字面反斜杠n残留（反转义生效）',
        !html.includes('\\n- 2026-08-10 讨论') && !html.includes('\\n  "title"'),
        '');
}
{
    // 多行合法 JSON 同样格式化；`json` 语言标记不影响 parse
    const md = '```json\n{\n  "a": 1\n}\n```';
    const html = renderArtifactMd(md);
    check('T13_多行合法JSON围栏同样格式化',
        html.includes('<pre><code>{\n  "a": 1\n}</code></pre>'),
        html);
}
{
    // 非法 JSON / 普通代码块：parse 失败保持原样，展示与现状一致
    const mdBad = '```\n{"a": 1, broken\n```';
    const htmlBad = renderArtifactMd(mdBad);
    check('T13_非法JSON围栏保持原样',
        htmlBad.includes('<pre><code>{"a": 1, broken</code></pre>'),
        htmlBad);
    // 无围栏的正文 JSON 形状文本不受影响
    const mdN = '普通文本 {"a": 1} 结尾。';
    const htmlN = renderArtifactMd(mdN);
    check('T13_无围栏正文JSON文本不被格式化',
        htmlN.includes('<p>普通文本 {"a": 1} 结尾。</p>'),
        htmlN);
}
{
    // XSS 基线：格式化输出仍整体经 escapeHtml，值内含 <img> 不被注入
    const md = '```draft\n{"x": "<img src=x onerror=alert(1)>"}\n```';
    const html = renderArtifactMd(md);
    check('T13_格式化后XSS仍被转义',
        html.includes('&lt;img src=x onerror=alert(1)&gt;') && !html.includes('<img'),
        html);
}
{
    // 边界：JSON 值内恰好含 ``` 字样（本需求 notes 即讨论 draft 围栏格式）——闭围栏独立成行，
    // 匹配应跳过值内部的 ``` 命中真正的闭围栏行，JSON 仍被格式化
    const md = '```draft\n{"title": "T", "notes": "常见 ```draft 草稿块\\n- 换行\\n- 事项"}\n```';
    const html = renderArtifactMd(md);
    check('T13_JSON值内含三个反引号仍正确格式化（闭围栏独立成行）',
        html.includes('<pre><code>{\n  "title": "T",\n  "notes": "常见 ```draft 草稿块\n- 换行\n- 事项"\n}</code></pre>'),
        html.slice(html.indexOf('<pre><code>'), html.indexOf('</code></pre>') + 14));
}

// ---- T16：#296 行内代码/普通围栏内的字面反斜杠+n 不被 #278 全局反转义拆开 ----
// 复刻需求 295 会话详情页现场（agent_events 15288 原文行形态）：行内代码 `\n` 被从中间劈开，
// 孤反引号留在上一行、「 转义」成为新段落；存储无损，缺陷只在渲染层。
{
    const md = '- `result.json`：`status: done`，`suggested_mode: auto`，`repos: ["req-board"]`，回读校验 JSON 合法、summary 换行均已 `\\n` 转义';
    const html = renderArtifactMd(md);
    check('T16_现场行内代码内字面反斜杠n保持字面显示且整行不拆',
        html.includes('<li><code>result.json</code>：<code>status: done</code>，<code>suggested_mode: auto</code>，<code>repos: ["req-board"]</code>，回读校验 JSON 合法、summary 换行均已 <code>\\n</code> 转义</li>')
        && !html.includes('<p>转义</p>'),
        html);
}
{
    // 正文中的字面反斜杠+n/引号仍反转义（#278 不回归），行内代码内则保持字面
    const html = renderArtifactMd('正文字面 \\" 应还原为引号，行内代码 `\\"` 与 `\\n` 保持字面。');
    check('T16_行内代码内字面反斜杠引号/反斜杠n保持字面且正文反转义不回归',
        html.includes('<p>正文字面 " 应还原为引号，行内代码 <code>\\"</code> 与 <code>\\n</code> 保持字面。</p>'),
        html);
}
{
    // 普通围栏（非 JSON）内的字面反斜杠+n/引号保持字面显示（需求明确要求的语义变化）
    const html = renderArtifactMd('日志样例：\n```\nline1 字面 \\n 保持\nline2 字面 \\" 保持\n```\n结尾。');
    check('T16_普通围栏内字面反斜杠n/引号保持字面显示',
        html.includes('<pre><code>line1 字面 \\n 保持\nline2 字面 \\" 保持</code></pre>'),
        html);
}
{
    // JSON 草稿围栏：格式化 + 反转义链路保持（T13 不回归），同一消息内行内代码保护共存
    const md = '草稿如下：\n\n```draft\n{"title": "T", "notes": "背景：\\n- 2026-08-10 讨论\\n- 方案待定"}\n```\n\n行内 `\\n` 保留字面。';
    const html = renderArtifactMd(md);
    check('T16_JSON草稿围栏格式化与反转义链路不回归',
        html.includes('<pre><code>{\n  "title": "T",\n  "notes": "背景：\n- 2026-08-10 讨论\n- 方案待定"\n}</code></pre>'),
        html.slice(html.indexOf('<pre><code>'), html.indexOf('</code></pre>') + 14));
    check('T16_同一消息内行内代码保护与JSON围栏共存',
        html.includes('<p>行内 <code>\\n</code> 保留字面。</p>'),
        html);
}
{
    // XSS 基线：保护层回填发生在 escapeHtml 之前，代码内容中的 < > 仍被统一转义
    const html = renderArtifactMd('行内代码 `<b>x</b>` 与普通围栏：\n```\n<img src=x onerror=alert(1)>\n```\n完毕。');
    check('T16_保护层回填后仍经escapeHtml统一转义（XSS不退化）',
        html.includes('<code>&lt;b&gt;x&lt;/b&gt;</code>')
        && html.includes('<pre><code>&lt;img src=x onerror=alert(1)&gt;</code></pre>')
        && !html.includes('<b>x</b>') && !html.includes('<img'),
        html);
}

// ---- T14：#281 会话行高亮判定（isSessionLinkActive）全入口组合 ----
// 复刻三种右栏入口 × 两类中间栏行（注册表行 rowId≠null / 旧格式兜底行 rowId=null）的判定矩阵
{
    // 调度历史入口（selectSession 只设 selectedSid）
    check('T14_调度历史入口注册表行高亮',
        isSessionLinkActive(273, 'session_A', null, 273, 5, 'session_A') === true,
        '注册表行应通过 selectedSid 兜底匹配');
    check('T14_调度历史入口旧格式行高亮',
        isSessionLinkActive(273, 'session_A', null, 273, null, 'session_A') === true,
        '');
    check('T14_调度历史入口不误伤其它会话行',
        isSessionLinkActive(273, 'session_A', null, 273, 6, 'session_B') === false,
        'selectedSid 只匹配同 session_id 的行');
    // 中间栏点击入口（selectAgentSession 设 selectedAgentSession）
    check('T14_中间栏点击注册表行高亮',
        isSessionLinkActive(273, null, 5, 273, 5, 'session_A') === true,
        '');
    check('T14_中间栏点击不误伤其它注册表行',
        isSessionLinkActive(273, null, 5, 273, 6, 'session_B') === false,
        '');
    // 需求不匹配 / 空会话 / 需求详情视图
    check('T14_需求不匹配不高亮',
        isSessionLinkActive(273, 'session_A', null, 274, null, 'session_A') === false
        && isSessionLinkActive(273, null, 5, 274, 5, 'session_A') === false,
        '');
    check('T14_空会话行（sessionId=null）不误高亮',
        isSessionLinkActive(273, null, null, 273, 7, null) === false
        && isSessionLinkActive(273, 'session_A', null, 273, 7, null) === false,
        '需求详情视图与选中其它会话时空会话行均不高亮');
    check('T14_需求详情视图（无选中会话）全不高亮',
        isSessionLinkActive(273, null, null, 273, 5, 'session_A') === false
        && isSessionLinkActive(273, null, null, 273, null, 'session_A') === false,
        '');
}

// ---- T17：#326 需求助手入口行高亮判定（isReqChatLinkActive）----
// 右栏展示该需求的需求助手视图（selectedReqChat=true）时中间栏「需求助手」行高亮；其余组合均不高亮
{
    check('T17_需求助手视图入口行高亮',
        isReqChatLinkActive(326, true, 326) === true,
        '');
    check('T17_需求不匹配不高亮',
        isReqChatLinkActive(326, true, 327) === false,
        '');
    check('T17_需求详情视图（selectedReqChat=false）不高亮',
        isReqChatLinkActive(326, false, 326) === false,
        '');
    check('T17_未选中任何需求不高亮',
        isReqChatLinkActive(null, false, 326) === false,
        '');
}

// ---- T15：#294 选区守卫纯函数（selectionGuardActive）判定矩阵 ----
// 面板内非折叠选区 / 拖动选择标志 → 拦截；面板外 anchor、折叠选区、无 anchor、panel 缺失 → 放行
{
    const panel = { id: 'procEvents', nodeType: 1 };
    const insideText = { nodeType: 3, parentNode: { nodeType: 1, parentNode: panel } };
    const insideEl = { nodeType: 1, parentNode: panel };
    const outsideText = { nodeType: 3, parentNode: { nodeType: 1, parentNode: { nodeType: 1, parentNode: null } } };
    check('T15_面板内文本节点anchor非折叠选区拦截',
        selectionGuardActive(panel, { isCollapsed: false, anchorNode: insideText }, false) === true, '');
    check('T15_面板内元素节点anchor拦截',
        selectionGuardActive(panel, { isCollapsed: false, anchorNode: insideEl }, false) === true, '');
    check('T15_anchor直取面板拦截',
        selectionGuardActive(panel, { isCollapsed: false, anchorNode: panel }, false) === true, '');
    check('T15_面板外anchor放行',
        selectionGuardActive(panel, { isCollapsed: false, anchorNode: outsideText }, false) === false, '');
    check('T15_折叠选区放行',
        selectionGuardActive(panel, { isCollapsed: true, anchorNode: insideText }, false) === false, '');
    check('T15_无anchor放行',
        selectionGuardActive(panel, { isCollapsed: false, anchorNode: null }, false) === false, '');
    check('T15_panel为null放行',
        selectionGuardActive(null, { isCollapsed: false, anchorNode: insideText }, false) === false, '');
    check('T15_拖动选择标志拦截（即使折叠/无anchor）',
        selectionGuardActive(panel, { isCollapsed: true, anchorNode: null }, true) === true, '');
}

// ---- T17：#297 会话详情视图刷新决策（agentSessionRefreshDecision）判定矩阵 ----
// 复刻需求 #297 现场：需求转 done / 注册表行消失 → full（全量重建，聊天框消失）；
// 仅注册表行状态变化（running→idle 等）→ status（不重建面板，滚动位置不重置）；
// 无变化 → none（维持 renderList + refreshProcessIfActive 增量刷新）。
// 空会话→真实会话（session_id 从无到有）必须归入 full（否则事件流面板永不出现）。
{
    const doing = { id: 297, status: 'doing' };
    const done = { id: 297, status: 'done' };
    const rowRunning = { id: 9, req_id: 297, session_id: 's1', status: 'running', poisoned: null, model: 'm', thinking: 't', harness: 'h', context_pct: 30, branch: 'b', workdir: 'w' };
    const rowIdle = Object.assign({}, rowRunning, { status: 'idle' });
    check('T17_需求转done全量重建（doing→done，聊天框按chatPart为空消失）',
        agentSessionRefreshDecision(doing, done, rowIdle, rowIdle) === 'full', '');
    check('T17_需求状态变化其它方向同样全量（done→open）',
        agentSessionRefreshDecision(done, Object.assign({}, done, { status: 'open' }), rowIdle, rowIdle) === 'full', '');
    check('T17_注册表行消失全量重建（null/undefined 均走 renderDetailPane 兜底）',
        agentSessionRefreshDecision(doing, doing, rowIdle, null) === 'full'
        && agentSessionRefreshDecision(doing, doing, rowIdle, undefined) === 'full', '');
    check('T17_注册表行出现全量重建（中间栏新行被调度器注册）',
        agentSessionRefreshDecision(doing, doing, null, rowIdle) === 'full', '');
    check('T17_session_id从无到有全量重建（空会话被调度器claim后事件流面板出现）',
        agentSessionRefreshDecision(doing, doing, Object.assign({}, rowIdle, { session_id: null }), rowIdle) === 'full', '');
    check('T17_session_id变化全量重建（补写/换会话，需重新打标dataset.sid）',
        agentSessionRefreshDecision(doing, doing, rowIdle, Object.assign({}, rowIdle, { session_id: 's2' })) === 'full', '');
    check('T17_running到idle仅刷新状态行（滚动位置保持）',
        agentSessionRefreshDecision(doing, doing, rowRunning, rowIdle) === 'status', '');
    check('T17_idle到running仅刷新状态行（调度器接管后思考中恢复）',
        agentSessionRefreshDecision(doing, doing, rowIdle, rowRunning) === 'status', '');
    check('T17_毒会话标记变化仅刷新状态行',
        agentSessionRefreshDecision(doing, doing, rowIdle, Object.assign({}, rowIdle, { poisoned: 1 })) === 'status', '');
    // #421：req_status（done 权威源）变化 → 全量重建（输入件显隐与状态行按新需求状态重算）；
    // 活性心跳字段变化 → 只刷状态行（不重建面板，事件流滚动位置不重置）
    check('T17_#421 req_status 变化全量重建（done 兜底即时收口输入区）',
        agentSessionRefreshDecision(doing, doing, Object.assign({}, rowIdle, { req_status: 'doing' }),
                                    Object.assign({}, rowIdle, { req_status: 'done' })) === 'full', '');
    check('T17_#421 心跳字段变化仅刷新状态行',
        ['turn_step', 'turn_step_detail', 'last_llm_request_at', 'last_llm_response_at',
         'last_thought_at', 'last_tool_started_at'].every(f =>
            agentSessionRefreshDecision(doing, doing, rowIdle,
                Object.assign({}, rowIdle, {[f]: 1789000000})) === 'status'), '');
    check('T17_元信息变化（context/model）仅刷新状态行',
        agentSessionRefreshDecision(doing, doing, rowIdle, Object.assign({}, rowIdle, { context_pct: 80 })) === 'status'
        && agentSessionRefreshDecision(doing, doing, rowIdle, Object.assign({}, rowIdle, { model: 'm2' })) === 'status', '');
    check('T17_无变化维持增量刷新（含字段全等的新对象）',
        agentSessionRefreshDecision(doing, doing, rowIdle, rowIdle) === 'none'
        && agentSessionRefreshDecision(doing, doing, rowIdle, Object.assign({}, rowIdle)) === 'none', '');
    check('T17_需求项缺失不误判全量（undefined状态对视为无变化）',
        agentSessionRefreshDecision(undefined, undefined, rowIdle, rowIdle) === 'none', '');
}

// 聊天事件流合并（重复气泡修复，建卡对话与需求助手共用同一纯函数）：poke 是「每写一条事件一条」，
// 而发送消息后前端还会主动做一次全量刷新——并发/迟到的增量响应会带着同一区间再 concat 一次。
// mergeChatStreamEvents 按事件 id 去重兜底，全量刷新则整体替换本地累积。
// 下面用现场 chunk（需求 419 需求助手 1543~1546）复刻该场景；建卡对话（chat_events）事件同形，共用同一入口。
{
    const chunk = [
        { id: 1543, kind: 'user', content: '如果我想自定义研发流程，我应该怎么做？我在哪里查看当前的流程？' },
        { id: 1544, kind: 'system', content: '对话已恢复（上下文延续）' },
        { id: 1545, kind: 'tool', content: 'Searching: /Users/dev/project/req-board' },
        { id: 1546, kind: 'tool', content: 'Searching: docs' },
    ];
    const dup = mergeChatStreamEvents(chunk, chunk, false);
    check('T18_同一区间被拼两次后按id去重（4条不翻倍）', dup.length === 4, 'len=' + dup.length);
    check('T18_去重结果按id升序且取服务端原值',
        dup.map(e => e.id).join(',') === '1543,1544,1545,1546' && dup[0].content === chunk[0].content,
        dup.map(e => e.id).join(','));
    check('T18_重复气泡的可见形态被消除（user 1条、tool 2条）',
        dup.filter(e => e.kind === 'user').length === 1 && dup.filter(e => e.kind === 'tool').length === 2,
        'user=' + dup.filter(e => e.kind === 'user').length
        + ' tool=' + dup.filter(e => e.kind === 'tool').length);

    // 迟到响应乱序落地（旧 chunk 晚于新 chunk 应用）也必须收敛成同一结果
    const late = mergeChatStreamEvents(
        [{ id: 1545, kind: 'tool', content: 'Searching: /Users/dev/project/req-board' },
         { id: 1546, kind: 'tool', content: 'Searching: docs' }], chunk, false);
    check('T18_乱序落地后仍有序无重复',
        late.map(e => e.id).join(',') === '1543,1544,1545,1546', late.map(e => e.id).join(','));

    // 幂等：同一批反复合并不增长（合并可重入）
    check('T18_合并幂等（重复调用不增长）', mergeChatStreamEvents(dup, chunk, false).length === 4, '');

    // full=true：服务端真值整体替换本地累积（本地被删/被纠正的历史一并丢弃）
    const replaced = mergeChatStreamEvents(
        [{ id: 1, kind: 'user', content: 'x' }, { id: 1543, kind: 'user', content: 'old' }],
        [{ id: 1543, kind: 'user', content: 'new' }, { id: 1547, kind: 'tool', content: 'd' }], true);
    check('T18_全量刷新整体替换本地累积',
        replaced.map(e => e.id).join(',') === '1543,1547' && replaced[0].content === 'new',
        replaced.map(e => e.id).join(','));

    // 防御：空入参 / 无 id 事件（本视图无本地乐观回显，服务端事件恒有 id）不污染结果也不抛异常
    check('T18_空入参与无id事件防御',
        mergeChatStreamEvents(null, null, false).length === 0
        && mergeChatStreamEvents([{ id: 1, kind: 'user', content: 'a' }],
            [{ kind: 'user', content: 'no-id' }], false).map(e => e.id).join(',') === '1', '');
}

// ---- T19：#424 运行中会话 → 组自动展开判定（agentGroupExpanded）三态矩阵 ----
// 运行态判定 = 组内 agent_sessions 行 status==='running'（主行/子行都算）；折叠优先
// （用户手动收起压过 running 与选中子行）；未表态时组内任一行 running 自动展开、运行结束恢复收起。
{
    check('T19_未表态+主行running自动展开（主行形态）',
        agentGroupExpanded(undefined, [{ status: 'running' }, { status: 'idle' }], false) === true, '');
    check('T19_未表态+子行running自动展开（折叠时运行中会话为组内最晚一条）',
        agentGroupExpanded(undefined, [{ status: 'idle' }, { status: 'running' }], false) === true, '');
    check('T19_未表态+无running收起',
        agentGroupExpanded(undefined, [{ status: 'idle' }, { status: 'interrupted' }], false) === false, '');
    check('T19_未表态+选中子行展开（#390 语义保留）',
        agentGroupExpanded(undefined, [{ status: 'idle' }], true) === true, '');
    check('T19_手动展开恒展开（无running也展开）',
        agentGroupExpanded(true, [{ status: 'idle' }], false) === true, '');
    check('T19_手动收起压过running（验收2：收起后不再自动展开）',
        agentGroupExpanded(false, [{ status: 'running' }], false) === false, '');
    check('T19_手动收起压过选中子行（折叠绝对优先契约）',
        agentGroupExpanded(false, [{ status: 'running' }], true) === false, '');
    check('T19_空组与rows缺失防御（不抛异常）',
        agentGroupExpanded(undefined, [], false) === false
        && agentGroupExpanded(undefined, null, false) === false, '');
    check('T19_行缺status不误判（空对象/undefined）',
        agentGroupExpanded(undefined, [{}, { status: undefined }], false) === false, '');
    check('T19_运行结束（running→idle）未表态组恢复收起',
        agentGroupExpanded(undefined, [{ status: 'running' }], false) === true
        && agentGroupExpanded(undefined, [{ status: 'idle' }], false) === false, '');
    check('T19_created/idle/interrupted均不自动展开',
        agentGroupExpanded(undefined, [{ status: 'created' }], false) === false
        && agentGroupExpanded(undefined, [{ status: 'idle' }], false) === false
        && agentGroupExpanded(undefined, [{ status: 'interrupted' }], false) === false, '');
}

// ---- T20：#422 会话头部折叠（sessPathParts/sessTitleParts/sessHeadKey/sessHeadHtml）----
// 需求 #422：常驻标题行 + 行尾折叠按钮，展开三行（meta / 执行目录 / 会话记录）；路径行
// 中间省略而尾部（会话 id、worktree 末段）完整；折叠态按 key 存内存、重绘按 map 回填。
{
    const WORKDIR = '/Users/dev/.req-board/worktrees/req-board/dev/req-419/req-board';
    const SID = 'session_5966f2b3-4538-aaba-f2f4213765cb';
    const SESSDIR = '/Users/dev/.kimi-code-deepseek/sessions/wd_req-board_a9414f280d77/' + SID;
    const wp = sessPathParts(WORKDIR);
    check('T20_路径切分：worktree尾部为dev/req-419/req-board且head+tail===原串',
        wp.tail === 'dev/req-419/req-board' && wp.head + wp.tail === WORKDIR,
        JSON.stringify(wp));
    const sp = sessPathParts(SESSDIR);
    check('T20_会话目录切分：尾部为完整末段session_<sid>且head+tail===原串',
        sp.tail === SID && sp.head + sp.tail === SESSDIR,
        JSON.stringify({head: sp.head, tail: sp.tail}));
    // 边界：单段/空串/相对路径/末段超长（单段保留不截断）
    const longSeg = 'z'.repeat(30);
    const cases = ['abc', '/abc', 'a/b/c', 'a/b/', '/x/y/' + longSeg];
    const bad = cases.filter(c => {
        const r = sessPathParts(c);
        return r.head + r.tail !== c || !r.tail;
    });
    check('T20_路径切分边界：head+tail恒等于原串且至少留末段',
        bad.length === 0 && sessPathParts('').head === '' && sessPathParts('').tail === '',
        'bad=' + JSON.stringify(bad));
    check('T20_超长末段单段保留（不截断会话id/目录名）',
        sessPathParts('/x/y/' + longSeg).tail === longSeg
        && sessPathParts('/x/y/' + longSeg).head === '/x/y/',
        JSON.stringify(sessPathParts('/x/y/' + longSeg)));

    // 会话 id 中间省略：末 8 位为尾（≤16 字符不切分）
    const st = sessTitleParts(SID);
    check('T20_会话id切分：尾部为末8位且head+tail===原串',
        st.tail === SID.slice(-8) && st.head + st.tail === SID && st.head.length === SID.length - 8,
        JSON.stringify({head: st.head, tail: st.tail}));
    check('T20_会话id边界：短id不切分/17字符起切分',
        sessTitleParts('short').tail === '' && sessTitleParts('short').head === 'short'
        && sessTitleParts('1234567890123456').tail === ''
        && sessTitleParts('12345678901234567').tail === '12345678901234567'.slice(-8),
        JSON.stringify(sessTitleParts('12345678901234567')));

    // 折叠 key：会话 id 优先，空会话按行 id
    check('T20_折叠key：有会话id取id、空会话取row:<行id>',
        sessHeadKey({session_id: 'session_A', id: 5}) === 'session_A'
        && sessHeadKey({session_id: null, id: 5}) === 'row:5'
        && sessHeadKey({id: 7}) === 'row:7',
        '');

    // 展开态回填：未展开无 open 类/按钮 ▶，sessHeadOpen[key]=true 时 .open/按钮 ▼
    const info = {key: 'session_A', label: '需求开发', sid: 'session_A', created: '09-18 03:12',
                  meta: 'kimi code 0.37.2 · deepseek/v4.1-flash · max', workdir: WORKDIR, sessionDir: SESSDIR};
    delete sessHeadOpen['session_A'];
    const closed = sessHeadHtml(info);
    sessHeadOpen['session_A'] = true;
    const opened = sessHeadHtml(info);
    delete sessHeadOpen['session_A'];
    check('T20_默认折叠：无open类、按钮▶、aria-expanded=false',
        !/sess-more open/.test(closed) && closed.includes('▶') && closed.includes('aria-expanded="false"')
        && !closed.includes('▼'),
        '');
    check('T20_展开态：sess-more带open类、按钮▼、aria-expanded=true',
        closed.includes('class="sess-more"') && opened.includes('class="sess-more open"')
        && opened.includes('▼') && opened.includes('aria-expanded="true"') && !opened.includes('▶'),
        '');
    delete sessHeadOpen['session_A'];
    const re1 = sessHeadHtml(info);
    sessHeadOpen['session_A'] = true;
    const re2 = sessHeadHtml(info);
    delete sessHeadOpen['session_A'];
    check('T20_折叠态由map回填（重绘连续渲染不丢展开态）',
        re1.includes('class="sess-more"') && re2.includes('class="sess-more open"')
        && sessHeadHtml(info).includes('class="sess-more"'),
        '');

    // 头部 HTML 契约：标题行 + meta/执行目录/会话记录三段 + 点行复制属性 + 尾部完整
    check('T20_头部契约：三段齐全且meta单行渲染',
        opened.includes('需求开发：') && opened.includes('class="sess-meta"')
        && opened.includes('kimi code 0.37.2 · deepseek/v4.1-flash · max')
        && opened.includes('执行目录：') && opened.includes('会话记录：')
        && opened.includes(' · 创建于 09-18 03:12'),
        '');
    check('T20_路径行可点复制：无 title 提示 + data-kind + 完整尾部',
        !opened.includes('点击复制') && opened.includes('data-kind="workdir"')
        && opened.includes('data-kind="session"')
        && opened.includes('<span class="sess-t">dev/req-419/req-board</span>')
        && opened.includes('<span class="sess-t">' + SID + '</span>'),
        '');
    check('T20_完整路径不进HTML属性（值走store，防属性注入）',
        !opened.includes(WORKDIR) && !opened.includes(SESSDIR)
        && sessPathStore['session_A'] && sessPathStore['session_A'].workdir === WORKDIR
        && sessPathStore['session_A'].sessionDir === SESSDIR,
        '');
    check('T20_路径原文被拆进head/tail两个span（中间省略形态）',
        opened.includes('<span class="sess-h">' + wp.head + '</span><span class="sess-t">dev/req-419/req-board</span>')
        && opened.includes('<span class="sess-h">' + sp.head + '</span><span class="sess-t">' + SID + '</span>'),
        '');

    // 降级：空会话无折叠按钮/无展开区；会话目录解析不到 → 未找到；workdir 为空 → 不渲染执行目录行
    const empty = sessHeadHtml({key: 'row:7', label: '需求开发', sid: null, created: '', meta: '',
                                workdir: '', sessionDir: null});
    check('T20_空会话（无sid）无折叠按钮与展开区，仅标题行',
        !empty.includes('sess-fold') && !empty.includes('sess-more') && empty.includes('新会话（未开始）')
        && empty.includes('class="sess-title"'),
        '');
    const noDir = sessHeadHtml({key: 'session_B', label: '开发', sid: 'session_B', created: '',
                                meta: '', workdir: '', sessionDir: null});
    check('T20_会话目录解析不到降级为「未找到」，workdir为空不渲染执行目录行',
        noDir.includes('会话记录：') && noDir.includes('未找到')
        && !noDir.includes('执行目录：') && !noDir.includes('undefined'),
        '');
}

// ---- T21：#120 入口统一——回放会话 id → agent_sessions 注册表行反查（agentSessionRowIdForSid）----
// 命中返回行 id（openSessionFromSid 据此走 selectAgentSession：执行过程 + 状态栏 + 聊天框，
// 与中间栏会话行同一视图）；未命中（历史/旧数据无注册表行、空 sid）返回 null → 回退
// selectSession 只读执行过程视图。session_id 全局唯一，req_id 为双条件防串卡。
{
    const rows = [
        {id: 434, req_id: 428, session_id: 'session_b30fca95', status: 'running'},
        {id: 435, req_id: 428, session_id: 'session_b9f0f1fc', status: 'idle'},
        {id: 433, req_id: 428, session_id: null},                  // 空会话（调度器 claim 前无 sid）
        {id: 12, req_id: 427, session_id: 'session_b30fca95'},     // 别卡同 sid（防串卡兜底）
    ];
    check('T21_命中注册表行返回行id（走会话视图，含状态栏）',
        agentSessionRowIdForSid(rows, 428, 'session_b30fca95') === 434, '');
    check('T21_同卡另一会话返回各自行id（不串号）',
        agentSessionRowIdForSid(rows, 428, 'session_b9f0f1fc') === 435, '');
    check('T21_idle 会话同样命中（非 running 也能进会话视图/续聊）',
        agentSessionRowIdForSid([{id: 9, req_id: 1, session_id: 'session_i', status: 'idle'}],
                                1, 'session_i') === 9, '');
    check('T21_req_id 不匹配不命中（防串卡）',
        agentSessionRowIdForSid(rows, 427, 'session_b9f0f1fc') === null, '');
    check('T21_未注册 sid（历史/旧数据）返回 null（回退只读执行过程）',
        agentSessionRowIdForSid(rows, 428, 'session_legacy') === null, '');
    check('T21_空 sid 返回 null（不误命中空会话行）',
        agentSessionRowIdForSid(rows, 428, null) === null
        && agentSessionRowIdForSid(rows, 428, '') === null, '');
    check('T21_注册表未加载（undefined/null）返回 null 不抛错',
        agentSessionRowIdForSid(undefined, 428, 'session_b30fca95') === null
        && agentSessionRowIdForSid(null, 428, 'session_b30fca95') === null, '');
    check('T21_脏数据行（null/缺字段）不抛错',
        agentSessionRowIdForSid([null, {}, {req_id: 428}], 428, 'session_x') === null, '');
}

// ---- T22（#431）：中间栏过滤——只渲染已建立会话（session_id 非空）的注册行 ----
// 待接管窗口（行已建、session_id 未回填）不展示该行；过滤发生在分组前，sessionList() 语义不变。
{
    const rows = [
        {role: 'dev', s: {id: null}},              // 待接管窗口
        {role: 'tech', s: {id: 'session_a'}},
        {role: 'accept', s: {id: ''}},             // 空串同样视为未建立
        {role: 'proposal', s: {id: 'session_b'}},  // 旧格式兜底行（有 sid）
        {role: 'code_review', s: null},            // 脏行
        null,
    ];
    const out = visibleSessionRows(rows);
    check('T22_只保留 session_id 非空行且顺序不变',
        out.length === 2 && out[0].s.id === 'session_a' && out[1].s.id === 'session_b',
        JSON.stringify(out.map(r => r.s && r.s.id)));
    check('T22_undefined/null 入参返回空数组不抛错',
        Array.isArray(visibleSessionRows(undefined)) && visibleSessionRows(undefined).length === 0
        && visibleSessionRows(null).length === 0, '');
    check('T22_空数组与脏行不抛错', visibleSessionRows([]).length === 0
        && visibleSessionRows([null, {}, {s: null}, {s: {}}]).length === 0, '');
}

// ---- T23（#431）：草稿态展示角色 = 卡上快照当前节点 role（terminal/无快照/脏 JSON → null）----
{
    const nodes = [{role: 'tech', node_type: 'artifact'}, {role: 'dev', node_type: 'code'},
                   {node_type: 'terminal'}];
    const json = JSON.stringify(nodes);
    check('T23_正常节点返回快照 role',
        currentNodeRole({nodes_json: json, node_index: 1}) === 'dev', '');
    check('T23_terminal 节点返回 null（收尾节点不给草稿会话角色）',
        currentNodeRole({nodes_json: json, node_index: 2}) === null, '');
    check('T23_索引越界/未入流程返回 null',
        currentNodeRole({nodes_json: json, node_index: 9}) === null
        && currentNodeRole({nodes_json: json, node_index: null}) === null
        && currentNodeRole({nodes_json: null, node_index: 0}) === null, '');
    check('T23_脏 JSON / 非对象元素不抛错',
        currentNodeRole({nodes_json: '{oops', node_index: 0}) === null
        && currentNodeRole({nodes_json: '[1,2]', node_index: 0}) === null
        && currentNodeRole(null) === null, '');
}

console.log(JSON.stringify({ checks: results }));
process.exit(results.every(r => r.pass) ? 0 : 1);
