// #419 流程 chip：flowChipHtml 标签契约回归 harness（由 tests/test_flow_chip.py 拼装调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的 function escapeHtml / function flowChipHtml，
// 本文件随后定义注入映射桩（= /api/meta 注入的 FLOW_LABEL / MODE_LABEL / FLOW_COLOR）与 D1~D14 断言。
//
// 断言边界（重要）：本文件只锁 **标签契约** + **chip 结构 / class 取值域**。
// 「哪个流程配哪个颜色」自 #428 起由 flows.json 的 color 经 /api/meta 注入（class = chip-flow-<color>），
// 色名与色值由 tests/test_flow_color.py 锁定；此处只补 D14「三档 class 互不相同」，
// 不重复点名具体色名。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

// /api/meta 注入的映射桩（FLOW_LABEL 取自 flows.json 的 name，MODE_LABEL 取自 board.MODE_LABELS，
// FLOW_COLOR 取自 flows.json 的 color；lean 不在表中，用于覆盖已退役流程的降级路径）
const FLOW_LABEL = {auto: '简单', standard: '标准', heavy: '复杂', lean: '精简'};
const MODE_LABEL = {auto: '自动', semi: '半自动'};
const FLOW_COLOR = {auto: 'green', standard: 'indigo', heavy: 'fuchsia'};
const FLOW_COLOR_FALLBACK = 'slate';

// escapeHtml 依赖 document.createElement（提取的是真实函数，桩与 session_activity_harness.js 同款）
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

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

const chip = (i) => flowChipHtml(i);
const labelOf = (html) => {
    const m = /<span class="meta-chip [^"]*">([\s\S]*?)<\/span>/.exec(html || '');
    return m ? m[1] : null;
};
const classOf = (html) => {
    const m = /<span class="meta-chip ([^"]*)">/.exec(html || '');
    return m ? m[1] : null;
};
// 结构 = 「 · 」前缀 + meta-chip + chip-<name> 类名（类名不锁具体取值：三档配色方案尚未定稿，
// 锁死现存两个类名会让未来的配色改动先撞红测试，见文件头「断言边界」）
const SHAPE = /^ · <span class="meta-chip chip-[a-z0-9-]+">[\s\S]*<\/span>$/;

// ---- 1. 标签优先级：卡上流程快照 > 注入映射 > 原文 id > 历史 mode 兜底 ----
{
    // 快照名与配置现名不同（流程改名后存量卡仍显示创建时的快照名）——必须取快照
    const renamed = chip({flow: 'standard', flow_name: '改名前-标准', mode: null});
    check('D1_快照flow_name优先于配置现名', labelOf(renamed) === '改名前-标准' && SHAPE.test(renamed),
        JSON.stringify([labelOf(renamed), renamed]));

    // 历史卡有 mode 时，快照/注入映射仍优先（不得显示「半自动」）
    check('D2_快照压过mode兜底', labelOf(chip({flow_name: '标准', mode: 'semi'})) === '标准',
        String(labelOf(chip({flow_name: '标准', mode: 'semi'}))));

    check('D3_flow走注入映射', labelOf(chip({flow: 'heavy', flow_name: null, mode: null})) === '复杂',
        String(labelOf(chip({flow: 'heavy', flow_name: null, mode: null}))));

    check('D4_注入映射未命中回退原文id', labelOf(chip({flow: 'weird'})) === 'weird',
        String(labelOf(chip({flow: 'weird'}))));

    check('D5_flow_name空串继续回退映射', labelOf(chip({flow: 'auto', flow_name: ''})) === '简单',
        String(labelOf(chip({flow: 'auto', flow_name: ''}))));
}

// ---- 2. 历史卡（无流程快照）：mode 兜底 ----
{
    check('D6_历史卡mode-auto显示自动', labelOf(chip({mode: 'auto'})) === '自动',
        String(labelOf(chip({mode: 'auto'}))));
    check('D7_历史卡mode-semi显示半自动', labelOf(chip({mode: 'semi'})) === '半自动',
        String(labelOf(chip({mode: 'semi'}))));
    check('D8_MODE_LABEL未命中回退原文', labelOf(chip({mode: 'unknown'})) === 'unknown',
        String(labelOf(chip({mode: 'unknown'}))));
}

// ---- 3. 无标签 → 不渲染 chip；边界输入不崩溃 ----
{
    check('D9_无任何标签返回空串', chip({}) === '', JSON.stringify(chip({})));
    check('D10_空flow与空flow_name返回空串', chip({flow: '', flow_name: '', mode: null}) === '',
        JSON.stringify(chip({flow: '', flow_name: '', mode: null})));
    check('D11_多余字段不影响标签', labelOf(chip({flow: 'auto', status: 'doing', id: 427})) === '简单',
        String(labelOf(chip({flow: 'auto', status: 'doing', id: 427}))));
}

// ---- 4. 结构与 class 取值域（只锁命名空间与形状，不锁流程→颜色映射） ----
{
    const rows = [
        {flow: 'auto', flow_name: '简单'}, {flow: 'standard', flow_name: '标准'},
        {flow: 'heavy', flow_name: '复杂'}, {flow: 'lean', flow_name: '精简'},
        {mode: 'auto'}, {mode: 'semi'},
    ];
    const bad = rows.filter((r) => {
        const html = chip(r);
        const cls = classOf(html);
        return !SHAPE.test(html) || /undefined|null/.test(cls) || cls === 'chip-';
    });
    check('D12_chip结构与class命名空间', bad.length === 0,
        JSON.stringify(bad.map((r) => chip(r))));
}

// ---- 5. label 经真实 escapeHtml 出口（防注入；转义语义由 board.escapeHtml 承担） ----
{
    const html = chip({flow_name: '<b>复杂</b>&"x"'});
    check('D13_label经escapeHtml转义',
        labelOf(html) === '&lt;b&gt;复杂&lt;/b&gt;&amp;"x"' && !/<b>/.test(html),
        String(html));
}

// ---- 6. 三档流程分色（#428）：class 互不相同，色名由 tests/test_flow_color.py 锁定 ----
{
    const three = ['auto', 'standard', 'heavy'].map((f) => classOf(chip({flow: f, flow_name: null})));
    check('D14_三档流程class互不相同', new Set(three).size === 3, JSON.stringify(three));
}

console.log(JSON.stringify({checks: results}));
process.exit(results.every((r) => r.pass) ? 0 : 1);
