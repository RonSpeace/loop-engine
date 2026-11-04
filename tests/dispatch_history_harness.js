'use strict';
// #51 调度历史回放：buildDispatchStages / formatDispatchDuration / dispatchTsMs node 断言。
// 函数源码由 test_dispatch_history_replay.py 从 static/board.js 提取后拼在本文件前面执行。

const buildFromApi = buildDispatchStages;
buildDispatchStages = events => buildFromApi(events && events.map(e => ({...e,
    ts_ms: e.ts_ms == null ? Date.parse(e.ts.replace(' ', 'T') + 'Z') : e.ts_ms})));
const results = [];
function check(name, pass, detail) {
    results.push({ name: name, pass: !!pass, detail: detail == null ? '' : String(detail) });
}

function stageByLabel(stages, label) {
    return stages.find(s => s.label === label);
}

// ---- D1：完整 heavy 流程（8 阶段、打回重做聚合、收尾）----
const heavy = [
    { id: 1, kind: 'status', ts: '2026-09-01 15:00:00', content: '状态变更：待开发(open) → 进行中(doing)' },
    { id: 2, kind: 'spawn', ts: '2026-09-01 15:02:00', content: '拉起「需求分析」会话（新会话）' },
    { id: 3, kind: 'thinking', ts: '2026-09-01 15:02:01', content: '思考档位未请求：需求分析 当前模型无思考档位配置项' },
    { id: 4, kind: 'phase', ts: '2026-09-01 15:15:00', content: '「需求分析」完成，识别涉及仓库 1 个，推荐流程 heavy' },
    { id: 5, kind: 'flow_choice', ts: '2026-09-01 15:15:00', content: '推荐流程 heavy（复杂）｜理由：高风险' },
    { id: 6, kind: 'phase', ts: '2026-09-01 15:15:00', content: '采纳流程「复杂」（heavy），进入首节点「技术方案」' },
    { id: 7, kind: 'spawn', ts: '2026-09-01 15:15:01', content: '拉起「技术方案」会话（新会话）' },
    { id: 8, kind: 'phase', ts: '2026-09-01 15:23:00', content: '「技术方案」完成，进入「方案评审」' },
    { id: 9, kind: 'spawn', ts: '2026-09-01 15:23:01', content: '拉起「方案评审」会话（新会话）' },
    { id: 10, kind: 'phase', ts: '2026-09-01 15:47:00', content: '「方案评审」未通过，打回「技术方案」重做（第 1 轮）' },
    { id: 11, kind: 'spawn', ts: '2026-09-01 15:47:01', content: '拉起「技术方案」会话（续跑）' },
    { id: 12, kind: 'phase', ts: '2026-09-01 15:58:00', content: '「技术方案」完成，进入「方案评审」' },
    { id: 13, kind: 'spawn', ts: '2026-09-01 15:58:01', content: '拉起「方案评审」会话（新会话）' },
    { id: 14, kind: 'phase', ts: '2026-09-01 16:10:00', content: '「方案评审」评审通过' },
    { id: 15, kind: 'phase', ts: '2026-09-01 16:10:00', content: '「方案评审」完成，进入「需求开发」' },
    { id: 16, kind: 'spawn', ts: '2026-09-01 16:10:01', content: '拉起「需求开发」会话（新会话）' },
    { id: 17, kind: 'retry', ts: '2026-09-01 16:12:00', content: '运行失败（会话提前结束），第 1 次自动重试（role=dev）' },
    { id: 18, kind: 'spawn', ts: '2026-09-01 16:12:01', content: '拉起「需求开发」会话（续跑）' },
    { id: 19, kind: 'phase', ts: '2026-09-01 16:33:00', content: '「需求开发」完成，进入「代码审查」' },
    { id: 20, kind: 'spawn', ts: '2026-09-01 16:33:01', content: '拉起「代码审查」会话（新会话）' },
    { id: 21, kind: 'phase', ts: '2026-09-01 16:48:00', content: '「代码审查」评审通过' },
    { id: 22, kind: 'phase', ts: '2026-09-01 16:48:00', content: '「代码审查」完成，进入「测试验证」' },
    { id: 23, kind: 'spawn', ts: '2026-09-01 16:48:01', content: '拉起「测试验证」会话（新会话）' },
    { id: 24, kind: 'phase', ts: '2026-09-01 17:08:00', content: '「测试验证」完成，进入流程收尾（terminal）' },
    { id: 25, kind: 'merge', ts: '2026-09-01 17:08:01', content: '流程收尾（terminal）：开始合并/接测（复杂）' },
    { id: 26, kind: 'merge', ts: '2026-09-01 17:08:30', content: '自动合并 dev 分支 f-20260901-51 到 main，清理 worktree，需求完成' },
    { id: 27, kind: 'status', ts: '2026-09-01 17:08:30', content: '状态变更：进行中(doing) → 已完成(done)' },
];

{
    const built = buildDispatchStages(heavy);
    const st = built.stages;
    check('D1_阶段数与顺序（入队+6节点+收尾）', st.length === 8
        && st.map(s => s.label).join('|') === '入队|需求分析|技术方案|方案评审|需求开发|代码审查|测试验证|收尾',
        st.map(s => s.label).join('|'));
    check('D1_事件全部归入阶段（总数不丢）',
        st.reduce((n, s) => n + s.events.length, 0) === heavy.length,
        st.reduce((n, s) => n + s.events.length, 0) + '/' + heavy.length);
    const analysis = stageByLabel(st, '需求分析');
    check('D1_需求分析：普通完成绿档 + 完成徽标（不冒充评审通过）',
        analysis.pass === true && analysis.done === true && analysis.reviewPass === false
        && analysis.tone === 'pass' && analysis.badge === '完成',
        JSON.stringify([analysis.pass, analysis.done, analysis.reviewPass, analysis.tone, analysis.badge]));
    const tech = stageByLabel(st, '技术方案');
    check('D1_技术方案：打回重做记 redo1、橙档、徽标重做1轮',
        tech.redo === 1 && tech.tone === 'warn' && tech.badge === '重做1轮',
        JSON.stringify([tech.redo, tech.tone, tech.badge]));
    const review = stageByLabel(st, '方案评审');
    check('D1_方案评审：打回 1 轮橙档（终态通过不覆盖打回语义）',
        review.reject === 1 && review.pass === true && review.tone === 'warn' && review.badge === '打回1轮',
        JSON.stringify([review.reject, review.pass, review.badge]));
    const codeReview = stageByLabel(st, '代码审查');
    check('D1_代码审查：评审通过绿档', codeReview.tone === 'pass' && codeReview.badge === '评审通过',
        codeReview.badge);
    const terminal = stageByLabel(st, '收尾');
    check('D1_收尾：完成徽标绿档', terminal.terminal && terminal.tone === 'pass' && terminal.badge === '完成',
        JSON.stringify([terminal.terminal, terminal.tone, terminal.badge]));
    check('D1_收尾行也有耗时（不再硬编码隐藏）',
        typeof terminal.durationMs === 'number' && formatDispatchDuration(terminal.durationMs) === '0m',
        formatDispatchDuration(terminal.durationMs));
    check('D1_普通阶段灰档无徽标（入队）',
        stageByLabel(st, '入队').tone === 'normal' && stageByLabel(st, '入队').badge === '',
        JSON.stringify([stageByLabel(st, '入队').tone, stageByLabel(st, '入队').badge]));
    check('D1_重做阶段耗时累加两次进入区间（8m+11m=19m）',
        formatDispatchDuration(tech.durationMs) === '19m', formatDispatchDuration(tech.durationMs));
    check('D1_各阶段起点为首次进入时间', analysis.startTs === '2026-09-01 15:02:00'
        && tech.startTs === '2026-09-01 15:15:00', analysis.startTs + '/' + tech.startTs);
    check('D1_统计条：事件27条/阶段8/打回1轮/重试1次/总耗时2h8m',
        built.stats.eventCount === 27 && built.stats.stageCount === 8
        && built.stats.rejectCount === 1 && built.stats.retryCount === 1
        && formatDispatchDuration(built.stats.totalMs) === '2h8m',
        JSON.stringify([built.stats.eventCount, built.stats.stageCount, built.stats.rejectCount,
            built.stats.retryCount, formatDispatchDuration(built.stats.totalMs)]));
    check('D1_明细条数按阶段真实事件数（需求分析5条、测试验证2条）',
        analysis.events.length === 5 && stageByLabel(st, '测试验证').events.length === 2,
        analysis.events.length + '/' + stageByLabel(st, '测试验证').events.length);
    const tones = new Set(st.map(s => s.tone));
    check('D1_状态圆点三档齐全（绿/橙/灰）',
        tones.has('pass') && tones.has('warn') && tones.has('normal'),
        Array.from(tones).join(','));
    check('D1_收尾阶段明细含合并与状态终态（3条）',
        stageByLabel(st, '收尾').events.length === 3
        && stageByLabel(st, '收尾').events.some(e => e.kind === 'status'),
        String(stageByLabel(st, '收尾').events.length));
}

// ---- D2：standard 流程（无技术方案/评审，无重做）----
{
    const standard = [
        { id: 1, kind: 'status', ts: '2026-09-02 10:00:00', content: '状态变更：待开发(open) → 进行中(doing)' },
        { id: 2, kind: 'spawn', ts: '2026-09-02 10:00:01', content: '拉起「需求分析」会话（新会话）' },
        { id: 3, kind: 'phase', ts: '2026-09-02 10:05:00', content: '「需求分析」完成，识别涉及仓库 1 个，推荐流程 standard' },
        { id: 4, kind: 'phase', ts: '2026-09-02 10:05:00', content: '采纳流程「标准」（standard），进入首节点「需求开发」' },
        { id: 5, kind: 'spawn', ts: '2026-09-02 10:05:01', content: '拉起「需求开发」会话（新会话）' },
        { id: 6, kind: 'phase', ts: '2026-09-02 10:20:00', content: '「需求开发」完成，进入「代码审查」' },
        { id: 7, kind: 'spawn', ts: '2026-09-02 10:20:01', content: '拉起「代码审查」会话（新会话）' },
        { id: 8, kind: 'phase', ts: '2026-09-02 10:25:00', content: '「代码审查」评审通过' },
        { id: 9, kind: 'phase', ts: '2026-09-02 10:25:00', content: '「代码审查」完成，进入「测试验证」' },
        { id: 10, kind: 'spawn', ts: '2026-09-02 10:25:01', content: '拉起「测试验证」会话（新会话）' },
        { id: 11, kind: 'phase', ts: '2026-09-02 10:35:00', content: '「测试验证」完成，进入流程收尾（terminal）' },
        { id: 12, kind: 'merge', ts: '2026-09-02 10:35:30', content: '自动合并 dev 分支到 main，清理 worktree，需求完成' },
    ];
    const built = buildDispatchStages(standard);
    check('D2_standard 阶段序列',
        built.stages.map(s => s.label).join('|') === '入队|需求分析|需求开发|代码审查|测试验证|收尾',
        built.stages.map(s => s.label).join('|'));
    check('D2_无打回/重做统计为 0',
        built.stats.rejectCount === 0 && built.stats.retryCount === 0
        && built.stages.every(s => !s.redo && !s.reject), '');
    check('D2_代码审查通过绿档', stageByLabel(built.stages, '代码审查').badge === '评审通过', '');
}

// ---- D3：边界（空 / 单事件 / 未完成无收尾 / 失败重试不计次）----
{
    const empty = buildDispatchStages([]);
    check('D3_空事件：0 阶段、统计全零',
        empty.stages.length === 0 && empty.stats.eventCount === 0 && empty.stats.stageCount === 0
        && empty.stats.totalMs === 0 && empty.stats.rejectCount === 0 && empty.stats.retryCount === 0, '');
    check('D3_非数组输入不抛错', buildDispatchStages(null).stages.length === 0, '');
    const single = buildDispatchStages([
        { id: 1, kind: 'action', ts: '2026-09-03 09:00:00', content: '调度器启动' },
    ]);
    check('D3_无阶段事件兜底为入队 1 阶段',
        single.stages.length === 1 && single.stages[0].label === '入队'
        && single.stages[0].events.length === 1, single.stages.map(s => s.label).join('|'));
    const running = buildDispatchStages([
        { id: 1, kind: 'spawn', ts: '2026-09-03 09:00:00', content: '拉起「需求开发」会话（新会话）' },
        { id: 2, kind: 'thinking', ts: '2026-09-03 09:10:00', content: '思考中' },
    ]);
    check('D3_未完成流程无收尾阶段，普通阶段灰档',
        running.stages.length === 1 && running.stages[0].tone === 'normal'
        && running.stages[0].durationMs === 10 * 60000,
        JSON.stringify(running.stages.map(s => [s.label, s.tone, s.durationMs])));
    check('D3_未完成普通阶段无状态徽标（不虚报评审通过）',
        running.stages[0].badge === '', JSON.stringify(running.stages[0].badge));
    const failed = buildDispatchStages([
        { id: 1, kind: 'spawn', ts: '2026-09-03 09:00:00', content: '拉起「需求开发」会话（新会话）' },
        { id: 2, kind: 'retry', ts: '2026-09-03 09:01:00', content: '自动开发失败 2 次，停止自动调度转 later' },
    ]);
    check('D3_失败终止事件不计「重试次数」（只计自动重试）',
        failed.stats.retryCount === 0, String(failed.stats.retryCount));
}

// ---- D5：人工裁决打回/放行 + 「不自动重试」排除（评审反馈回归）----
{
    const manual = [
        { id: 1, kind: 'status', ts: '2026-09-04 10:00:00', content: '状态变更：待开发(open) → 进行中(doing)' },
        { id: 2, kind: 'spawn', ts: '2026-09-04 10:00:01', content: '拉起「技术方案」会话（新会话）' },
        { id: 3, kind: 'phase', ts: '2026-09-04 10:05:00', content: '「技术方案」完成，进入「方案评审」' },
        { id: 4, kind: 'spawn', ts: '2026-09-04 10:05:01', content: '拉起「方案评审」会话（新会话）' },
        { id: 5, kind: 'phase', ts: '2026-09-04 10:10:00', content: '「方案评审」未通过，打回「技术方案」重做（第 1 轮）' },
        { id: 6, kind: 'spawn', ts: '2026-09-04 10:10:01', content: '拉起「技术方案」会话（续跑）' },
        { id: 7, kind: 'phase', ts: '2026-09-04 10:15:00', content: '「技术方案」完成，进入「方案评审」' },
        { id: 8, kind: 'spawn', ts: '2026-09-04 10:15:01', content: '拉起「方案评审」会话（新会话）' },
        { id: 9, kind: 'phase', ts: '2026-09-04 10:20:00', content: '「方案评审」未通过，打回「技术方案」重做（第 2 轮）' },
        { id: 10, kind: 'spawn', ts: '2026-09-04 10:20:01', content: '拉起「技术方案」会话（续跑）' },
        { id: 11, kind: 'phase', ts: '2026-09-04 10:25:00', content: '「技术方案」完成，进入「方案评审」' },
        { id: 12, kind: 'spawn', ts: '2026-09-04 10:25:01', content: '拉起「方案评审」会话（新会话）' },
        { id: 13, kind: 'phase', ts: '2026-09-04 10:30:00', content: '「方案评审」未通过，打回「技术方案」重做（第 3 轮）' },
        { id: 14, kind: 'spawn', ts: '2026-09-04 10:30:01', content: '拉起「技术方案」会话（续跑）' },
        { id: 15, kind: 'phase', ts: '2026-09-04 10:35:00', content: '「技术方案」完成，进入「方案评审」' },
        { id: 16, kind: 'spawn', ts: '2026-09-04 10:35:01', content: '拉起「方案评审」会话（新会话）' },
        { id: 17, kind: 'phase', ts: '2026-09-04 10:40:00', content: '人工裁决打回「技术方案」再修一轮' },
        { id: 18, kind: 'spawn', ts: '2026-09-04 10:40:01', content: '拉起「技术方案」会话（续跑）' },
        { id: 19, kind: 'phase', ts: '2026-09-04 10:45:00', content: '「技术方案」完成，进入「方案评审」' },
        { id: 20, kind: 'spawn', ts: '2026-09-04 10:45:01', content: '拉起「方案评审」会话（新会话）' },
        { id: 21, kind: 'phase', ts: '2026-09-04 10:50:00', content: '人工裁决放行「方案评审」，进入下一节点' },
        { id: 22, kind: 'phase', ts: '2026-09-04 10:50:01', content: '「方案评审」完成，进入「需求开发」' },
        { id: 23, kind: 'spawn', ts: '2026-09-04 10:50:02', content: '拉起「需求开发」会话（新会话）' },
        { id: 24, kind: 'retry', ts: '2026-09-04 10:50:03', content: '运行失败，第 1 次自动重试（role=dev）' },
        { id: 25, kind: 'retry', ts: '2026-09-04 10:50:04', content: '需求阻塞，已转 later 待人工处理（不自动重试，role=accept）' },
    ];
    const built = buildDispatchStages(manual);
    const tech = stageByLabel(built.stages, '技术方案');
    const review = stageByLabel(built.stages, '方案评审');
    check('D5_人工裁决打回计入统计与目标重做（3+1=4 轮）',
        built.stats.rejectCount === 4 && review.reject === 4 && tech.redo === 4
        && review.badge === '打回4轮' && tech.badge === '重做4轮',
        JSON.stringify([built.stats.rejectCount, review.reject, tech.redo, review.badge, tech.badge]));
    check('D5_人工裁决放行识别为评审通过（不依赖后续完成事件）',
        review.pass === true && review.reviewPass === true && review.badge === '打回4轮',
        JSON.stringify([review.pass, review.reviewPass, review.badge]));
    check('D5_「不自动重试」终止留痕不计重试次数',
        built.stats.retryCount === 1, String(built.stats.retryCount));
    const manualPassOnly = buildDispatchStages([
        { id: 1, kind: 'status', ts: '2026-09-04 11:00:00', content: '状态变更：待开发(open) → 进行中(doing)' },
        { id: 2, kind: 'spawn', ts: '2026-09-04 11:00:01', content: '拉起「方案评审」会话（新会话）' },
        { id: 3, kind: 'phase', ts: '2026-09-04 11:05:00', content: '人工裁决放行「方案评审」，进入下一节点' },
    ]);
    const rp = stageByLabel(manualPassOnly.stages, '方案评审');
    check('D5_人工放行无后续「完成，进入」事件时仍有通过态徽标',
        rp.pass === true && rp.reviewPass === true && rp.tone === 'pass' && rp.badge === '评审通过',
        JSON.stringify([rp.pass, rp.reviewPass, rp.tone, rp.badge]));
}

// ---- D4：时长格式化 ----
{
    check('D4_格式化：0/59s/1m/1h/1h46m',
        formatDispatchDuration(0) === '0m'
        && formatDispatchDuration(59000) === '0m'
        && formatDispatchDuration(60000) === '1m'
        && formatDispatchDuration(3600000) === '1h'
        && formatDispatchDuration(6360000) === '1h46m',
        [0, 59000, 60000, 3600000, 6360000].map(formatDispatchDuration).join('|'));
    check('D4_时间戳解析：合法/空/非法',
        dispatchTsMs('2026-09-01 15:02:03', new Map([['2026-09-01 15:02:03', 1234]])) === 1234
        && dispatchTsMs('') === null && dispatchTsMs(null) === null
        && dispatchTsMs('bad') === null, '');
}

console.log(JSON.stringify({ checks: results }));
process.exit(results.every(r => r.pass) ? 0 : 1);
