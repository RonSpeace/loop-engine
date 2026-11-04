// req-428 流程 chip 配色回归 harness（由 tests/test_flow_color.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的 function escapeHtml / function flowChipHtml，
// 本文件随后定义桩常量（/api/meta 注入值，与仓库真实 flows.json 一致）与 C1~C13 断言。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

// escapeHtml 依赖 document.createElement（提取的真实函数，与 session_activity_harness.js 同款桩）
function makeEscDiv() {
    let text = '';
    return {
        set textContent(v) { text = String(v == null ? '' : v); },
        get innerHTML() {
            return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
    };
}
global.document = {createElement: () => makeEscDiv()};

// 桩：loadMeta() 从 /api/meta 构建的注入表（键值 = flows.json 三条流程的真实 color）
let FLOW_LABEL = {auto: '简单', standard: '标准', heavy: '复杂'};
let MODE_LABEL = {auto: '自动', semi: '半自动'};
let FLOW_COLOR = {auto: 'green', standard: 'indigo', heavy: 'fuchsia'};
const FLOW_COLOR_FALLBACK = 'slate';

const K = o => flowChipHtml(Object.assign({id: 1, flow: null, flow_name: null, mode: null}, o));
const CLS = html => { const m = String(html).match(/class="([^"]*)"/); return m ? m[1] : ''; };
const classes = html => CLS(html).split(/\s+/).filter(Boolean);

// ---- C1~C3：三档流程各自取色（简单绿/标准靛蓝/复杂品红紫） ----
{
    const got = K({flow: 'auto'});
    check('C1_简单流程绿', CLS(got).includes('chip-flow-green') && got.includes('简单'), CLS(got));
    check('C2_标准流程靛蓝', CLS(K({flow: 'standard'})).includes('chip-flow-indigo'), CLS(K({flow: 'standard'})));
    check('C3_复杂流程品红紫', CLS(K({flow: 'heavy'})).includes('chip-flow-fuchsia'), CLS(K({flow: 'heavy'})));
}

// ---- C4~C7：未命中统一中性灰，且文案链不变 ----
{
    const retired = K({flow: 'lean', flow_name: '精简'});
    check('C4_已退役流程灰且文案取快照名',
        CLS(retired).includes('chip-flow-slate') && retired.includes('精简') && !retired.includes('lean'),
        retired);
    const semi = K({mode: 'semi'});
    check('C5_无流程快照历史卡灰_半自动文案',
        CLS(semi).includes('chip-flow-slate') && semi.includes('半自动'), semi);
    const legacy = K({mode: 'auto'});
    check('C6_无流程快照历史卡灰_自动文案',
        CLS(legacy).includes('chip-flow-slate') && legacy.includes('自动'), legacy);
    const noFlow = K({flow_name: '标准'});
    check('C7_无flow有快照名灰且文案取快照名',
        CLS(noFlow).includes('chip-flow-slate') && noFlow.includes('标准'), noFlow);
}

// ---- C8：颜色锁死不按 mode 取色（mode 历史值只影响文案，不影响颜色） ----
{
    const got = K({flow: 'auto', mode: 'semi'});
    check('C8_flow_auto+mode_semi仍绿_锁死不按mode取色',
        CLS(got).includes('chip-flow-green') && !CLS(got).includes('chip-flow-slate'), CLS(got));
}

// ---- C9：/api/meta 降级（FLOW_COLOR 为空）→ 全中性灰，不白屏 ----
{
    const saved = FLOW_COLOR;
    FLOW_COLOR = {};
    try {
        const got = K({flow: 'standard'});
        check('C9_meta降级全灰', CLS(got).includes('chip-flow-slate') && got.includes('标准'), got);
    } finally { FLOW_COLOR = saved; }
}

// ---- C10~C13：边界与安全 ----
{
    check('C10_三无卡不渲染chip', K({}) === '', JSON.stringify(K({})));
    const esc = K({flow_name: '<b>x</b>'});
    check('C11_快照名转义', esc.includes('&lt;b&gt;') && !esc.includes('<b>'), esc);
    const one = K({flow: 'heavy'});
    const cls = classes(one);
    check('C12_单卡class恰一个meta-chip与一个chip-flow',
        cls.filter(c => c === 'meta-chip').length === 1
        && cls.filter(c => c.startsWith('chip-flow-')).length === 1, one);
    check('C13_无旧类残留', !one.includes('chip-auto') && !one.includes('chip-semi'), one);
}

console.log(JSON.stringify({checks: results}));
process.exit(results.every(r => r.pass) ? 0 : 1);
