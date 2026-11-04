// #430 统一状态卡片回归 harness（由 tests/test_chat_status_card.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的真实函数源码
// （function escapeHtml / reqChatEventMs / reqChatActivityModel / chatRoundSec /
//  chatAddActivityModel / chatStatusCardHtml / applyChatStatusCard / setStatusNoInput /
//  tickChatStatusCard），本文件随后定义 DOM 桩与依赖 mock，按组断言。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({ name, pass: !!pass, detail: detail == null ? '' : String(detail) });
}

// ---- 依赖桩（提取的函数以全局作用域解析，global 属性赋值即可） ----
// escapeHtml 依赖 document.createElement（提取的真实函数，与 process_markdown_harness.js 同款桩）
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
global.displayProjectName = (p) => (p === 'req-board' ? '需求看板' : String(p || ''));
// 卡片展开态手选变量（board.py 里是 let 声明；此处按用例显式设置）
let reqChatStatusManualOpen = null;
let chatAddStatusManualOpen = null;
// 与 board.py 同值的 data-k → signals 键映射（Python 侧另有单一源断言）
const CHAT_STATUS_SIG_KEY = {'事件流': 'event', '模型层': 'model', '判定': 'verdict'};
// 与 board.py 同值的文案/图标表（Python 侧 test_model_tables_single_source 逐行比对，防两处漂移）
const REQ_CHAT_STATUS_ICO = {created: '💬', running: '⏳', idle: '⏹',
    interrupted: '⏸', failed: '⛔', closed: '⏹'};
const REQ_CHAT_CARD_LABEL = {created: '新对话（未开始）：发送第一条消息开始',
    idle: '可继续对话（上轮已结束）', running: '思考中（本轮执行中）',
    interrupted: '自动路径已停止，可发消息继续',
    failed: '对话建立失败（发消息即新建对话）', closed: '对话已结束（发消息即新建对话）'};
const CHAT_ADD_STATUS_ICO = {creating: '🔄', restoring: '🔄', confirming: '📨', running: '⏳',
    created: '✅', idle: '💬', interrupted: '⏸', auto: '🔄', suspended: '⏸',
    failed: '⛔', closed: '⏹', created_new: '💬'};

// ts（本地时间字符串）→ 毫秒：与 reqChatEventMs 同口径
function ms(ts) { return Date.parse(String(ts).replace(' ', 'T') + 'Z'); }
const EV = (ts, kind) => ({id: 1, kind: kind || 'message', content: 'x', ts, ts_ms: ms(ts)});

// ---- DOM 桩 ----
function line(k, text) {
    const v = {textContent: text, className: 'st-v'};
    return {dataset: {k}, querySelector: (sel) => (sel === '.st-v' ? v : null), _v: v};
}
function fakeCard(opts) {
    opts = opts || {};
    return {
        className: '', innerHTML: '', dataset: {kind: opts.kind || '', open: opts.open || '0'},
        querySelector: (sel) => (opts.nodes || {})[sel] || null,
        querySelectorAll: () => opts.lines || [],
        closest: (sel) => (opts.closest ? opts.closest(sel) : null),
    };
}

// ==================== A：共享卡片 DOM 构造 ====================
{
    const m = {
        kind: 'running', color: 'green', label: '思考中（本轮执行中）', ico: '⏳',
        timerText: '已 12s', open: false, ticking: true, canInput: false,
        signals: {event: '最后事件 3s 前', model: '本轮已发出请求', verdict: '活着'},
        signalSev: {event: 'ok', model: '', verdict: 'ok'},
    };
    const html = chatStatusCardHtml(m, '<button class="st-btn">取消本轮</button>');
    check('A1_头部结构：图标+主文案+计时+动作+箭头',
        html.includes('class="st-ico">⏳<') && html.includes('class="st-main">思考中（本轮执行中）<')
        && html.includes('class="st-timer">已 12s<') && html.includes('class="st-act">')
        && html.includes('class="st-btn">取消本轮<') && html.includes('class="st-arw">▶<'), html);
    check('A2_头部点击挂 toggleChatStatusDetail(this)（点正文不收起）',
        html.includes('<div class="st-head" onclick="toggleChatStatusDetail(this)"')
        && html.includes('title="点击展开 / 收起判定依据"'), html.slice(0, 120));
    check('A3_判定依据三行：键列+值列三色',
        html.includes('class="st-line" data-k="事件流"') && html.includes('class="st-line" data-k="模型层"')
        && html.includes('class="st-line" data-k="判定"') && html.includes('class="st-v ok">最后事件 3s 前<'),
        html);
    check('A4_展开态箭头 ▼ 且 .open 由调用方写入 className',
        chatStatusCardHtml(Object.assign({}, m, {open: true}), '').includes('class="st-arw">▼<'), 'open');
    const noSig = chatStatusCardHtml({kind: 'override', color: 'red', label: '会话不存在', ico: '⛔',
        timerText: '', signals: {event: '', model: '', verdict: ''}}, '');
    check('A5_无判定依据：头部静态、无箭头、无展开区',
        noSig.includes('class="st-head st-static"') && !noSig.includes('onclick=') && !noSig.includes('st-arw')
        && !noSig.includes('st-detail'), noSig);
    const esc = chatStatusCardHtml({kind: 'failed', color: 'red', label: '<img src=x onerror=1>',
        ico: '<b>', timerText: '', signals: {event: 'e', model: 'm', verdict: 'v'}}, '');
    check('A6_主文案与图标转义（XSS 防线）',
        !esc.includes('<img') && !esc.includes('<b>') && esc.includes('&lt;img'), esc.slice(0, 160));
    const noTimer = chatStatusCardHtml({kind: 'idle', color: 'gray', label: '就绪', ico: '💬',
        timerText: '', signals: {event: 'e', model: 'm', verdict: 'v'}}, '');
    check('A7_无计时不渲染 .st-timer 节点', !noTimer.includes('st-timer'), noTimer);
}

// ==================== B：applyChatStatusCard / setStatusNoInput ====================
{
    const el = fakeCard();
    applyChatStatusCard(el, {kind: 'running', color: 'green', label: 'L', ico: '⏳', timerText: '已 1s',
        open: true, signals: {event: 'e', model: 'm', verdict: 'v'}, signalSev: {}}, '');
    check('B1_档位/展开态落到 className 与 dataset',
        el.className === 'chat-status st-green open' && el.dataset.kind === 'running'
        && el.dataset.open === '1', el.className + '/' + el.dataset.kind + '/' + el.dataset.open);
    const el2 = fakeCard();
    applyChatStatusCard(el2, {kind: 'idle', color: 'gray', label: 'L', ico: '⏹', timerText: '',
        open: false, signals: {event: 'e', model: 'm', verdict: 'v'}, signalSev: {}}, '');
    check('B2_收起态 className 不含 open、dataset.open=0',
        el2.className === 'chat-status st-gray' && el2.dataset.open === '0', el2.className);

    const calls = [];
    const pane = {classList: {toggle: (cls, on) => calls.push([cls, on])}};
    const row = {parentElement: pane};
    const cardEl = fakeCard({closest: (sel) => (sel === '.chat-input-row' ? row : null)});
    setStatusNoInput(cardEl, true);
    setStatusNoInput(cardEl, false);
    check('B3_不可输入给输入行父容器加 .no-input（卡片内按钮不受影响）',
        calls.length === 2 && calls[0][0] === 'no-input' && calls[0][1] === true && calls[1][1] === false,
        JSON.stringify(calls));
    setStatusNoInput(fakeCard(), true);   // 无输入行（异常路径）不抛
    check('B4_无 .chat-input-row 祖先时不抛错', true, 'ok');
}

// ==================== C：tickChatStatusCard（同档只改文本 / 换档整卡重建） ====================
{
    const ico = {textContent: ''}, main = {textContent: ''}, timer = {textContent: ''};
    const el = fakeCard({kind: 'running', open: '1', nodes: {'.st-ico': ico, '.st-main': main, '.st-timer': timer}});
    let rebuilt = 0;
    tickChatStatusCard(el, {kind: 'running', ico: '⏳', label: '思考中（模型推理）', timerText: '已 8s',
        signals: {event: 'e', model: 'm', verdict: 'v'}, signalSev: {}}, () => { rebuilt += 1; });
    check('C1_同档内文本节点级更新（图标/主文案/计时）',
        rebuilt === 0 && ico.textContent === '⏳' && main.textContent === '思考中（模型推理）'
        && timer.textContent === '已 8s', [ico.textContent, main.textContent, timer.textContent].join('|'));

    const l1 = line('事件流', '旧'), l2 = line('模型层', '旧'), l3 = line('判定', '旧');
    const el2 = fakeCard({kind: 'running', open: '1', lines: [l1, l2, l3]});
    tickChatStatusCard(el2, {kind: 'running', ico: '⏳', label: 'L', timerText: '',
        signals: {event: '最后事件 70s 前', model: '本轮已发出请求', verdict: '活着'},
        signalSev: {event: 'warn', model: '', verdict: 'ok'}}, () => { rebuilt += 1; });
    check('C2_展开时按 signalSev 重着色值列',
        l1._v.textContent === '最后事件 70s 前' && l1._v.className === 'st-v warn'
        && l3._v.className === 'st-v ok', [l1._v.className, l2._v.className, l3._v.className].join('|'));

    const el3 = fakeCard({kind: 'running', open: '0', lines: [line('事件流', '旧')]});
    tickChatStatusCard(el3, {kind: 'running', ico: '⏳', label: 'L', timerText: '',
        signals: {event: '新', model: 'm', verdict: 'v'}, signalSev: {}}, () => { rebuilt += 1; });
    check('C3_收起态不写判定依据文本', el3.querySelectorAll()[0]._v.textContent === '旧', 'collapsed');

    const el4 = fakeCard({kind: 'running'});
    tickChatStatusCard(el4, {kind: 'idle', ico: '⏹', label: 'L', timerText: '',
        signals: {event: 'e', model: 'm', verdict: 'v'}, signalSev: {}}, () => { rebuilt += 1; });
    check('C4_档位变化走整卡重建', rebuilt === 1, 'rebuilt=' + rebuilt);

    // 计时节点从无到有 / 从有到无：属结构变化，必须整卡重建（否则「已 Ns」永远不显示）
    let rebuilt5 = 0;
    const el5 = fakeCard({kind: 'running', open: '0'});   // 无 .st-timer 节点
    tickChatStatusCard(el5, {kind: 'running', ico: '⏳', label: 'L', timerText: '已 3s',
        signals: {event: 'e', model: 'm', verdict: 'v'}, signalSev: {}}, () => { rebuilt5 += 1; });
    check('C5_计时从无到有触发整卡重建', rebuilt5 === 1, 'rebuilt=' + rebuilt5);

    let rebuilt6 = 0;
    const el6 = fakeCard({kind: 'running', nodes: {'.st-timer': {textContent: '已 3s'}}});
    tickChatStatusCard(el6, {kind: 'running', ico: '⏳', label: 'L', timerText: '',
        signals: {event: 'e', model: 'm', verdict: 'v'}, signalSev: {}}, () => { rebuilt6 += 1; });
    check('C6_计时从有到无触发整卡重建', rebuilt6 === 1, 'rebuilt=' + rebuilt6);
}

// ==================== D：需求助手状态模型 ====================
{
    const T0 = '2026-09-18 12:00:00';
    const t0 = ms(T0);
    const evs = [EV('2026-09-18 11:59:50', 'user'), EV(T0, 'user'), EV(T0, 'tool')];

    const none = reqChatActivityModel(null, [], t0);
    check('D1_无会话：灰卡新对话、可输入、💬',
        none.kind === 'created' && none.color === 'gray' && none.ico === '💬' && none.canInput
        && none.timerText === '' && none.label.includes('新对话'), JSON.stringify(none));

    reqChatStatusManualOpen = null;
    const run = reqChatActivityModel({status: 'running', session_id: 's1', model: 'DeepSeek V4.1 Flash'},
        evs, t0 + 12000);
    check('D2_running：绿卡+⏳+已 12s（基准=最后一条 user 事件）+不可输入+取消本轮',
        run.color === 'green' && run.ico === '⏳' && run.timerText === '已 12s' && run.ticking
        && !run.canInput && run.action === 'cancel' && run.signalSev.verdict === 'ok',
        run.timerText + '/' + run.action);
    check('D3_running 判定依据：事件流 + 模型层 + 判定（不判卡死）',
        run.signals.event.includes('最后事件 12s 前') && run.signals.model.includes('等待模型响应')
        && run.signals.verdict.includes('不判卡死'), JSON.stringify(run.signals));

    const runNoEv = reqChatActivityModel({status: 'running'}, [], t0);
    check('D4_running 无事件：暂不计时但 1s tick 仍开（事件拉到后靠结构重建补计时）',
        runNoEv.timerText === '' && runNoEv.ticking === true
        && runNoEv.signals.event.includes('尚无事件时间'), runNoEv.signals.event);

    const idle = reqChatActivityModel({status: 'idle'}, evs, t0 + 120000);
    check('D5_idle：灰卡可输入、无新建动作（活跃会话新建会 409）、事件流超 1 分钟标 warn',
        idle.color === 'gray' && idle.canInput && idle.action === null && idle.signalSev.event === 'warn'
        && idle.timerText === '', JSON.stringify(idle.signalSev));

    const inter = reqChatActivityModel({status: 'interrupted', interrupt_source: 'user_cancel'}, evs, t0);
    check('D6_interrupted：灰卡⏸+可发消息继续+中断来源入模型层',
        inter.kind === 'interrupted' && inter.ico === '⏸' && inter.canInput && inter.action === null
        && inter.signals.model.includes('user_cancel') && inter.label.includes('自动路径已停止'),
        inter.label + '|' + inter.signals.model);

    const failed = reqChatActivityModel({status: 'failed', error: '进程启动失败'}, evs, t0);
    check('D7_failed：红卡⛔+错误入主文案+自动展开+新建对话动作+判定 bad',
        failed.color === 'red' && failed.ico === '⛔' && failed.label.includes('进程启动失败')
        && failed.open === true && failed.action === 'newSession' && failed.signalSev.verdict === 'bad',
        failed.label + '/' + failed.action);

    const closed = reqChatActivityModel({status: 'closed'}, evs, t0);
    check('D8_closed：灰卡+新建对话动作+可输入（发消息自动新建）',
        closed.color === 'gray' && closed.canInput && closed.action === 'newSession'
        && closed.signals.verdict.includes('新建对话'), closed.signals.verdict);

    const unknown = reqChatActivityModel({status: 'weird'}, [], t0);
    check('D9_未知状态兜底：灰卡可输入、不抛错',
        unknown.color === 'gray' && unknown.canInput && unknown.ico === '❔', unknown.label);

    reqChatStatusManualOpen = true;   // 用户手选展开后，自动展开态不再决定 open
    const failedManual = reqChatActivityModel({status: 'failed', error: 'e'}, evs, t0);
    const idleManual = reqChatActivityModel({status: 'idle'}, evs, t0);
    check('D10_手选展开态优先于 autoOpen（1s 重绘不冲掉手动态）',
        failedManual.open === true && idleManual.open === true, 'manual');
    reqChatStatusManualOpen = null;
}

// ==================== E：聊天建卡状态模型 ====================
{
    const T0 = '2026-09-18 12:00:00';
    const t0 = ms(T0);
    const evs = [EV('2026-09-18 11:59:55', 'user'), EV(T0, 'message')];
    const sig = (m) => !!(m.signals.event || m.signals.model || m.signals.verdict);

    const ov = chatAddActivityModel({status: 'running'}, null, '会话不存在', [], t0);
    check('E1_接口错误 override：红卡直显、无判定依据（头部不可展开）',
        ov.color === 'red' && ov.label === '会话不存在' && ov.ico === '⛔' && !sig(ov), ov.label);

    const closing = chatAddActivityModel({status: 'idle', project: 'req-board'}, 'closing', null, evs, t0);
    const creating = chatAddActivityModel({status: 'idle', project: 'req-board'}, 'creating', null, evs, t0);
    check('E2_新建会话阶段反馈：蓝卡+项目名+不可输入',
        closing.color === 'blue' && closing.label.includes('正在结束') && !closing.canInput
        && creating.color === 'blue' && creating.label.includes('需求看板') && !creating.canInput,
        closing.label + '/' + creating.label);

    const c1 = chatAddActivityModel({status: 'running', project: 'req-board', activity: 'creating'}, null, null, evs, t0);
    const r1 = chatAddActivityModel({status: 'running', project: 'req-board', activity: 'restoring'}, null, null, evs, t0);
    const k1 = chatAddActivityModel({status: 'running', project: 'req-board', activity: 'confirming'}, null, null, evs, t0);
    check('E3_running 三阶段：创建/恢复/建卡均为蓝卡且不可输入',
        [c1, r1, k1].every(x => x.color === 'blue' && !x.canInput)
        && c1.label.includes('正在创建') && r1.label.includes('正在恢复') && k1.label.includes('正在建卡'),
        [c1.label, r1.label, k1.label].join('|'));

    const run = chatAddActivityModel({status: 'running', project: 'req-board'}, null, null,
        [EV(T0, 'user')], t0 + 5000);
    check('E4_running：绿卡+⏳+已 5s（基准=最后一条 user 事件）+取消本轮动作+不可输入',
        run.color === 'green' && run.ico === '⏳' && run.timerText === '已 5s' && run.action === 'cancel'
        && !run.canInput && run.signalSev.verdict === 'ok', run.timerText + '/' + run.action);

    const created = chatAddActivityModel({status: 'created', req_id: 428}, null, null, evs, t0);
    check('E5_created（已建卡）：灰卡✅+不可输入+自动展开+卡号入主文案',
        created.color === 'gray' && created.ico === '✅' && !created.canInput && created.open === true
        && created.label.includes('#428'), created.label);

    const interManual = chatAddActivityModel({status: 'interrupted', interrupt_source: 'user_cancel'}, null, null, evs, t0);
    const interDraft = chatAddActivityModel({status: 'interrupted', interrupt_source: 'user_cancel', draft_title: 'T'},
        null, null, evs, t0);
    check('E6_手动中断：灰卡⏸+可继续（有草稿时点名草稿）',
        interManual.color === 'gray' && interManual.canInput && interManual.label.includes('保留上下文')
        && !interManual.label.includes('草稿')
        && interDraft.canInput && interDraft.label.includes('草稿仍保留'),
        interManual.label + '|' + interDraft.label);

    const interAuto = chatAddActivityModel({status: 'interrupted', interrupt_source: 'timeout'}, null, null, evs, t0);
    check('E7_自动恢复中断：琥珀卡🔄+不可输入（过渡态，无需操作）',
        interAuto.color === 'amber' && interAuto.ico === '🔄' && !interAuto.canInput
        && interAuto.signals.model.includes('timeout') && interAuto.signalSev.verdict === 'warn',
        interAuto.label);

    const susp = chatAddActivityModel({status: 'suspended'}, null, null, evs, t0);
    const failed = chatAddActivityModel({status: 'failed', error: 'boot 异常'}, null, null, evs, t0);
    const closed = chatAddActivityModel({status: 'closed'}, null, null, evs, t0);
    check('E8_suspended/failed/closed：挂起灰卡可输入、失败红卡⛔、关闭灰卡',
        susp.color === 'gray' && susp.canInput && failed.color === 'red' && failed.ico === '⛔'
        && failed.canInput && failed.label.includes('boot 异常') && closed.color === 'gray' && closed.canInput,
        [susp.label, failed.label, closed.label].join('|'));

    const idle = chatAddActivityModel({status: 'idle'}, null, null, evs, t0);
    const idleDraft = chatAddActivityModel({status: 'idle', draft_title: 'T'}, null, null, evs, t0);
    check('E9_idle：灰卡💬+新建会话动作；有草稿时文案与判定切换',
        idle.action === 'newSession' && idle.canInput && idle.label.includes('就绪')
        && idle.signals.verdict === '可继续（保留上下文）'
        && idleDraft.label.includes('草稿已就绪') && idleDraft.signals.verdict === '待确认草稿',
        idle.label + '|' + idleDraft.label);

    const blank = chatAddActivityModel(null, null, null, [], t0);
    check('E10_无会话无阶段：灰卡💬空态可输入',
        blank.color === 'gray' && blank.canInput && blank.label.includes('新对话'), blank.label);

    chatAddStatusManualOpen = false;   // 手选收起优先于 autoOpen（failed/created 的自动展开）
    const failedCollapsed = chatAddActivityModel({status: 'failed', error: 'e'}, null, null, evs, t0);
    check('E11_手选收起优先于 autoOpen', failedCollapsed.open === false, 'collapsed');
    chatAddStatusManualOpen = null;
}

const failed = results.filter(r => !r.pass).length;
console.log(JSON.stringify({checks: results}));
process.exit(failed ? 1 : 0);
