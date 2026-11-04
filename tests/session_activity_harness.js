// req-421 会话活性状态行回归测试 harness（由 tests/test_session_activity.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的 function escapeHtml / function agentActivityModel，
// 本文件随后定义常量桩（board.py 经 Jinja 注入的同源常量）与 A1~A25 断言。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

// board.py 服务端注入常量（index() 的 render_template_string 参数）；此处用默认值桩
const ACTIVITY_STALL_SEC = 180;
const ACTIVITY_STALL_FIRST_SEC = 180;
const ACTIVITY_WARN_SEC = 60;
const THOUGHT_FRAMES_ABSENT = false;

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

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

// 基准：服务端「现在」（毫秒）；心跳字段为 epoch 秒，事件 ts_ms 为 epoch 毫秒
const NOW_MS = 1700000000000;
const ago = s => Math.round(NOW_MS / 1000 - s);   // s 秒前的 epoch 秒（心跳）
const agoMs = s => NOW_MS - s * 1000;             // s 秒前（事件 ts_ms）
const row = o => Object.assign({
    id: 1, req_id: 7, status: 'running', session_id: 's1', poisoned: 0, turn_step: 'llm',
    turn_step_detail: null, last_llm_request_at: null, last_llm_response_at: null,
    last_thought_at: null, last_tool_started_at: null, req_status: null,
}, o || {});
const M = (r, status, busy, ev, opts) => agentActivityModel(r, status, busy, NOW_MS, ev, opts);

// ---- A1：十五态矩阵（§2 状态表逐行，全字段断言） ----
{
    const cases = [
        ['1等响应正常', M(row({last_llm_request_at: ago(12)}), 'doing', false, agoMs(3)),
            {kind: 'ok', color: 'green', label: '等模型响应', timerText: '已 12s', canInput: false, autoOpen: false, action: null}],
        ['2等响应偏慢', M(row({last_llm_request_at: ago(80)}), 'doing', false, agoMs(3)),
            {kind: 'slow', color: 'amber', label: '等模型响应（偏慢）', timerText: '已 1m20s', canInput: false, autoOpen: true, action: null}],
        ['3疑似卡死', M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200)),
            {kind: 'stall', color: 'red', label: '疑似卡死', timerText: '已 3m20s', canInput: false, autoOpen: true, action: 'restart'}],
        ['4思考中', M(row({last_thought_at: ago(12)}), 'doing', false, agoMs(3)),
            {kind: 'ok', color: 'green', label: '思考中（模型推理）', timerText: '已 12s', canInput: false, autoOpen: false, action: null}],
        ['5思考偏慢', M(row({last_thought_at: ago(80)}), 'doing', false, agoMs(3)),
            {kind: 'slow', color: 'amber', label: '思考偏慢（推理中）', timerText: '已 1m20s', canInput: false, autoOpen: true, action: null}],
        ['6响应中', M(row({last_llm_response_at: ago(12)}), 'doing', false, agoMs(3)),
            {kind: 'ok', color: 'green', label: '响应中（流式输出）', timerText: '已 12s', canInput: false, autoOpen: false, action: null}],
        ['7响应偏慢', M(row({last_llm_response_at: ago(80)}), 'doing', false, agoMs(3)),
            {kind: 'slow', color: 'amber', label: '响应偏慢（生成中）', timerText: '已 1m20s', canInput: false, autoOpen: true, action: null}],
        ['8输出中断', M(row({last_llm_response_at: ago(200)}), 'doing', false, agoMs(200)),
            {kind: 'stall', color: 'red', label: '输出中断（疑似卡死）', timerText: '已 3m20s', canInput: false, autoOpen: true, action: 'restart'}],
        ['9输出持续中', M(row({last_llm_response_at: ago(200)}), 'doing', false, agoMs(5)),
            {kind: 'output-live', color: 'blue', label: '输出持续中（事件流仍在刷新）', timerText: '已 3m20s', canInput: false, autoOpen: false, action: null}],
        ['10执行命令', M(row({turn_step: 'tool', turn_step_detail: 'Bash', last_tool_started_at: ago(45), last_llm_response_at: ago(50)}), 'doing', false, agoMs(3)),
            {kind: 'tool', color: 'blue', label: '执行命令（Bash）', timerText: '已 45s', canInput: false, autoOpen: false, action: null}],
        ['10b正在写文件', M(row({turn_step: 'tool', turn_step_detail: 'Edit', last_tool_started_at: ago(6)}), 'doing', false, agoMs(3)),
            {kind: 'tool', color: 'blue', label: '正在写文件（Edit）', timerText: '已 6s', canInput: false, autoOpen: false, action: null}],
        ['11会话已结束', M(row({status: 'idle'}), 'doing', false, agoMs(3)),
            {kind: 'idle', color: 'gray', label: '会话已结束 · 需求仍在进行中，可发消息继续', timerText: '', canInput: true, autoOpen: false, action: 'newSession'}],
        ['12已中断', M(row({status: 'interrupted'}), 'doing', false, agoMs(3)),
            {kind: 'stopped', color: 'gray', label: '自动路径已停止，可发消息继续', timerText: '', canInput: true, autoOpen: false, action: 'newSession'}],
        ['13需求已完成', M(row({status: 'running', last_llm_request_at: ago(5)}), 'done', false, null),
            {kind: 'done', color: 'gray', label: '需求已完成，不可继续会话', timerText: '', canInput: false, autoOpen: false, action: null}],
        ['14新会话', M(row({status: 'created', session_id: null}), 'doing', false, null),
            {kind: 'created', color: 'gray', label: '新会话（未开始）：发送第一条消息后开始', timerText: '', canInput: true, autoOpen: false, action: 'newSession'}],
        ['15待接管', M(row({status: 'idle'}), 'doing', true, agoMs(3)),
            {kind: 'waiting', color: 'blue', label: '已发送，等待调度器接管…', timerText: '', canInput: false, autoOpen: false, action: null}],
    ];
    for (const [name, m, exp] of cases) {
        const bad = Object.keys(exp).filter(k => m[k] !== exp[k])
            .map(k => k + '=' + JSON.stringify(m[k]) + '≠' + JSON.stringify(exp[k]));
        check('A1_' + name, bad.length === 0, bad.join(';'));
    }
}

// ---- A2：阈值边界（59/60/61/179/180/181，等响应/思考/响应中三档各跑一遍） ----
{
    const kinds = [['req', 'last_llm_request_at'], ['thought', 'last_thought_at'], ['resp', 'last_llm_response_at']];
    const expect = {59: 'green', 60: 'amber', 61: 'amber', 179: 'amber', 180: 'amber', 181: 'red'};
    for (const [kn, field] of kinds) {
        const got = [59, 60, 61, 179, 180, 181].map(sec => {
            const r = row({turn_step: 'llm'});
            r[field] = ago(sec);
            return M(r, 'doing', false, agoMs(sec)).color;
        });
        check('A2_' + kn + '_阈值边界(60起琥珀/181起红/180仍琥珀)',
            got.join(',') === 'green,amber,amber,amber,amber,red', got.join(','));
    }
    check('A2_阈值表逐点匹配', JSON.stringify([59, 60, 61, 179, 180, 181].map(s => expect[s]))
        === JSON.stringify(['green', 'amber', 'amber', 'amber', 'amber', 'red']), '');
}

// ---- A3：输入区显隐（活动态 1~10 与 done 不可输入；idle/interrupted/created 可输入） ----
{
    const active = [
        M(row({last_llm_request_at: ago(12)}), 'doing', false, null),
        M(row({last_llm_request_at: ago(80)}), 'doing', false, null),
        M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200)),
        M(row({last_llm_response_at: ago(200)}), 'doing', false, agoMs(5)),
        M(row({turn_step: 'tool', last_tool_started_at: ago(45)}), 'doing', false, null),
        M(row({last_llm_response_at: ago(200)}), 'doing', false, agoMs(200)),
    ];
    check('A3_活动态不显示输入', active.every(m => m.canInput === false),
        active.map(m => m.kind + ':' + m.canInput).join(','));
    check('A3_done不显示输入', M(row({status: 'running'}), 'done', false, null).canInput === false, '');
    check('A3_结束态可输入', ['idle', 'interrupted', 'created'].every(st =>
        M(row({status: st}), 'doing', false, null).canInput === true), '');
}

// ---- A4：自动展开（琥珀/红自动展开；其余含 9 行不展开） ----
{
    const autos = [
        ['slow', M(row({last_llm_request_at: ago(80)}), 'doing', false, null).autoOpen],
        ['stall', M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200)).autoOpen],
        ['stall-out', M(row({last_thought_at: ago(200)}), 'doing', false, agoMs(200)).autoOpen],
    ];
    check('A4_琥珀红自动展开', autos.every(x => x[1] === true), JSON.stringify(autos));
    const not = [
        M(row({last_llm_request_at: ago(12)}), 'doing', false, null).autoOpen,
        M(row({last_llm_response_at: ago(200)}), 'doing', false, agoMs(5)).autoOpen,
        M(row({turn_step: 'tool', last_tool_started_at: ago(45)}), 'doing', false, null).autoOpen,
    ];
    check('A4_其余不自动展开（含 9 行/工具态）', not.every(v => v === false), JSON.stringify(not));
}

// ---- A5：手动态优先（1s 重绘不冲掉用户手选） ----
{
    const red = M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200), {manualOpen: false});
    const green = M(row({last_llm_request_at: ago(12)}), 'doing', false, agoMs(1), {manualOpen: true});
    check('A5_手选收起时红态不展开', red.autoOpen === true && red.open === false, 'auto=' + red.autoOpen + ' open=' + red.open);
    check('A5_手选展开时绿态展开', green.autoOpen === false && green.open === true, 'auto=' + green.autoOpen + ' open=' + green.open);
    check('A5_未手选时跟随自动', M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200)).open === true, '');
}

// ---- A6：服务端时钟基准（浏览器时钟偏 ±3600s / 事件 ts_ms 非同浏览器时区，age 仍一致正确） ----
{
    // 前端每次拉取注册表后按 server_now 算偏移（agentServerClockOffsetMs = server_now*1000 − Date.now()），
    // 再以「浏览器现在 + 偏移」作为统一时间基准 → 浏览器时钟快/慢 1 小时都得到同一结论
    const serverNow = 1700000000000;
    const hb = 1700000000 - 12;              // 心跳：12s 前（epoch 秒）
    const evMs = (1700000000 - 3) * 1000;    // 事件：3s 前（epoch 毫秒，服务端 ts_ms）
    const basis = browserNow => (browserNow + (serverNow - browserNow));
    const base = agentActivityModel(row({last_llm_request_at: hb}), 'doing', false, basis(serverNow + 3600 * 1000), evMs);
    const skew2 = agentActivityModel(row({last_llm_request_at: hb}), 'doing', false, basis(serverNow - 3600 * 1000), evMs);
    check('A6_浏览器时钟漂移不影响时长与判断',
        base.timerText === '已 12s' && skew2.timerText === base.timerText && skew2.color === base.color,
        [base.timerText, skew2.timerText].join(','));
    check('A6_事件 age 按服务端 ts_ms 算', base.signals.event.indexOf('最后事件 3s 前') === 0, base.signals.event);
}

// ---- A7：历史行兼容（四心跳全 NULL + running → 中性、无计时、无 action、不判卡死） ----
{
    const m = M(row({}), 'doing', false, agoMs(600));
    check('A7_心跳缺失fail-safe', m.kind === 'unknown' && m.color === 'blue' && m.label === '运行中'
        && m.timerText === '' && m.action === null && m.ticking === false, JSON.stringify(m.label));
    // #421 浏览器验证发现：turn_step 非 llm 非 tool（历史行/心跳写入失败）时前端不得给红态与重启入口
    // ——服务端守卫要求 turn_step='llm'，否则点击必被 400 拒（口径一致，fail-safe）
    const m2 = M(row({turn_step: null, last_llm_request_at: ago(400)}), 'doing', false, agoMs(400));
    check('A7_阶段证据缺失不给红态与入口',
        m2.kind === 'unknown' && m2.color === 'blue' && m2.action === null && m2.timerText === '', m2.label);
}

// ---- A8：计时格式 ----
{
    const t = s => M(row({last_llm_request_at: ago(s)}), 'doing', false, null).timerText;
    check('A8_计时格式(12s/1m20s/3m20s/结束态空)',
        t(12) === '已 12s' && t(80) === '已 1m20s' && t(200) === '已 3m20s'
        && M(row({status: 'idle'}), 'doing', false, null).timerText === '', [t(12), t(80), t(200)].join(','));
}

// ---- A9：判定文案 ----
{
    const v = (r, ev) => M(r, 'doing', false, ev).signals.verdict;
    check('A9_3行含疑似卡死', v(row({last_llm_request_at: ago(200)}), agoMs(200)).indexOf('疑似卡死') === 0, '');
    check('A9_8行含输出中断', v(row({last_llm_response_at: ago(200)}), agoMs(200)).indexOf('输出中断') === 0, '');
    check('A9_9行含输出持续中', v(row({last_llm_response_at: ago(200)}), agoMs(5)).indexOf('输出持续中，事件流仍在刷新') > 0, '');
    check('A9_10行含事件流无新行属正常',
        M(row({turn_step: 'tool', last_tool_started_at: ago(45)}), 'doing', false, null)
            .signals.verdict.indexOf('事件流无新行属正常') > 0, '');
    check('A9_11行含非卡死', M(row({status: 'idle'}), 'doing', false, null).signals.verdict.indexOf('非卡死') > 0, '');
}

// ---- A10：XSS（工具标题注入标签 → 转义后无裸标签） ----
{
    const m = M(row({turn_step: 'tool', turn_step_detail: '<img src=x onerror=alert(1)>',
                     last_tool_started_at: ago(3)}), 'doing', false, null);
    const esc = escapeHtml(m.label);
    check('A10_工具标题先转义', m.label.indexOf('<img') > 0 && esc.indexOf('<img') < 0 && esc.indexOf('&lt;img') > 0, esc);
    const esc2 = escapeHtml(M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200)).signals.verdict);
    check('A10_判定行同样可转义', esc2.indexOf('<') < 0, esc2);
}

// ---- A11：计时基准来源（四字段不串） ----
{
    const m1 = M(row({last_llm_request_at: ago(10), last_thought_at: ago(20), last_llm_response_at: ago(30)}), 'doing', false, null);
    const m2 = M(row({last_llm_request_at: ago(10), last_thought_at: ago(20), last_llm_response_at: ago(5)}), 'doing', false, null);
    const m3 = M(row({last_llm_request_at: ago(10), last_thought_at: ago(4), last_llm_response_at: ago(30)}), 'doing', false, null);
    const m4 = M(row({turn_step: 'tool', last_tool_started_at: ago(7), last_llm_request_at: ago(9)}), 'doing', false, null);
    check('A11_请求档取请求时间戳', m1.timerText === '已 10s' && m1.label === '等模型响应', m1.label + '/' + m1.timerText);
    check('A11_响应档取响应时间戳', m2.timerText === '已 5s' && m2.label === '响应中（流式输出）', m2.label + '/' + m2.timerText);
    check('A11_思考档取思考时间戳', m3.timerText === '已 4s' && m3.label === '思考中（模型推理）', m3.label + '/' + m3.timerText);
    check('A11_工具档取工具时间戳', m4.timerText === '已 7s' && m4.label.indexOf('执行命令') === 0, m4.label + '/' + m4.timerText);
}

// ---- A12：响应已到、流式中（request 很旧但 response 新鲜 → 绿，无重启入口） ----
{
    const m = M(row({last_llm_request_at: ago(1200), last_llm_response_at: ago(12)}), 'doing', false, agoMs(3));
    check('A12_响应已到即绿且无动作入口',
        m.kind === 'ok' && m.color === 'green' && m.label === '响应中（流式输出）' && m.action === null, m.label);
}

// ---- A13：响应后静默 >180s（心跳 + 事件流双静默 → 红 + 重启入口） ----
{
    const m = M(row({last_llm_request_at: ago(400), last_llm_response_at: ago(200)}), 'doing', false, agoMs(200));
    check('A13_响应后双静默判红', m.color === 'red' && m.action === 'restart' && m.label === '输出中断（疑似卡死）', m.label);
}

// ---- A14：输出持续中（同心跳但事件流仍在刷新 → 9 行中性蓝、不给重启入口） ----
{
    const m = M(row({last_llm_request_at: ago(400), last_llm_response_at: ago(200)}), 'doing', false, agoMs(5));
    check('A14_事件流新鲜走 9 行', m.kind === 'output-live' && m.color === 'blue'
        && m.label === '输出持续中（事件流仍在刷新）' && m.action === null, m.label);
}

// ---- A15：事件流证据未知（回退只看心跳，文案标注） ----
{
    const m = M(row({last_llm_request_at: ago(400), last_llm_response_at: ago(200)}), 'doing', false, null);
    check('A15_证据未知仍判红且文案标注', m.color === 'red' && m.action === 'restart'
        && m.signals.verdict.indexOf('事件流证据未知') > 0, m.signals.verdict);
}

// ---- A16：退出路径（新证据出现立即退出红态） ----
{
    const red = M(row({last_llm_request_at: ago(400), last_llm_response_at: ago(200)}), 'doing', false, agoMs(200));
    const back = M(row({last_llm_request_at: ago(400), last_llm_response_at: ago(1)}), 'doing', false, agoMs(1));
    check('A16_新证据立即转绿并撤下入口',
        red.color === 'red' && red.action === 'restart' && back.color === 'green' && back.action === null, back.label);
}

// ---- A17：思考持续到达不误判 ----
{
    const m = M(row({last_llm_request_at: ago(1200), last_llm_response_at: ago(1200), last_thought_at: ago(12)}),
                'doing', false, agoMs(3));
    check('A17_思考档绿且无动作入口', m.kind === 'ok' && m.label === '思考中（模型推理）' && m.action === null, m.label);
}

// ---- A18：思考中断仍判死 ----
{
    const m = M(row({last_llm_request_at: ago(400), last_thought_at: ago(200)}), 'doing', false, agoMs(200));
    check('A18_思考中断判红+入口', m.color === 'red' && m.action === 'restart' && m.label === '输出中断（疑似卡死）', m.label);
}

// ---- A19：档位推进（思考 → 输出） ----
{
    const a = M(row({last_llm_request_at: ago(1200), last_thought_at: ago(12)}), 'doing', false, agoMs(3));
    const b = M(row({last_llm_request_at: ago(1200), last_thought_at: ago(12), last_llm_response_at: ago(1)}), 'doing', false, agoMs(1));
    check('A19_思考→输出档位推进且计时基准切换',
        a.label === '思考中（模型推理）' && b.label === '响应中（流式输出）' && b.timerText === '已 1s',
        a.label + ' → ' + b.label + '/' + b.timerText);
}

// ---- A20：同秒 tie-break（response > thought > request） ----
{
    const all = M(row({last_llm_request_at: ago(12), last_thought_at: ago(12), last_llm_response_at: ago(12)}), 'doing', false, agoMs(3));
    const rt = M(row({last_llm_request_at: ago(12), last_thought_at: ago(12)}), 'doing', false, agoMs(3));
    const none = M(row({}), 'doing', false, null);
    check('A20_三者同秒取响应档', all.label === '响应中（流式输出）', all.label);
    check('A20_请求与思考同秒取思考档', rt.label === '思考中（模型推理）', rt.label);
    check('A20_全空走中性', none.kind === 'unknown' && none.label === '运行中', none.label);
}

// ---- A21：思考档文案与事件流行 ----
{
    const m = M(row({last_thought_at: ago(12)}), 'doing', false, agoMs(30));
    check('A21_思考档文案与事件流行',
        m.label.indexOf('模型推理') > 0 && m.signals.event.indexOf('模型推理中，无新事件行属正常') > 0
        && m.signals.model.indexOf('思考 chunk') === 0, m.signals.event);
}

// ---- A22：优先级 running+poisoned=1（人工续跑过渡走活动态，不显示「自动路径已停止」） ----
{
    const m = M(row({poisoned: 1, last_thought_at: ago(12)}), 'doing', false, agoMs(3));
    check('A22_毒标记不参与存活判定', m.kind === 'ok' && m.canInput === false
        && m.label.indexOf('自动路径已停止') < 0, m.label);
}

// ---- A23：优先级 done 优先于 running ----
{
    const m = M(row({status: 'running', last_llm_request_at: ago(5)}), 'done', false, agoMs(1));
    check('A23_done 优先', m.kind === 'done' && m.canInput === false && m.timerText === '', m.label);
}

// ---- A24：优先级 待接管 / 毒 / 结束 ----
{
    const wait = M(row({status: 'idle'}), 'doing', true, null);
    const pois = M(row({status: 'idle', poisoned: 1}), 'doing', false, null);
    const idle = M(row({status: 'idle'}), 'doing', false, null);
    check('A24_busy+idle 走待接管且不可输入', wait.kind === 'waiting' && wait.canInput === false, wait.label);
    check('A24_毒会话可输入', pois.kind === 'stopped' && pois.canInput === true, pois.label);
    check('A24_idle 可输入', idle.kind === 'idle' && idle.canInput === true, idle.label);
}

// ---- A25：思考帧缺失降级口径（THOUGHT_FRAMES_ABSENT：仅首帧档阈值放宽且红态不自动展开） ----
{
    const deg = {thoughtAbsent: true, stallFirstSec: 300};
    const r200 = M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200), deg);
    const r310 = M(row({last_llm_request_at: ago(310)}), 'doing', false, agoMs(310), deg);
    const on = M(row({last_llm_request_at: ago(310)}), 'doing', false, agoMs(310));
    const thought = M(row({last_thought_at: ago(200)}), 'doing', false, agoMs(200), deg);
    check('A25_降级档 200s 不判红', r200.color === 'amber' && r200.action === null, r200.label);
    check('A25_降级档 310s 判红但不自动展开（保留入口）',
        r310.kind === 'stall' && r310.action === 'restart' && r310.autoOpen === false, r310.label);
    check('A25_未降级时 180s 阈值生效且自动展开',
        on.kind === 'stall' && on.autoOpen === true && on.action === 'restart', on.label);
    check('A25_降级只影响首帧档（思考档阈值不变）',
        thought.color === 'red' && thought.autoOpen === true, thought.label);
}

// ---- A26：#421 走查 状态图标（交互稿 .st-ico；按档位取，工具态按写文件/执行命令区分） ----
{
    const cases = [
        ['绿档-等响应', M(row({last_llm_request_at: ago(12)}), 'doing', false, agoMs(3)), '⏳'],
        ['绿档-思考', M(row({last_thought_at: ago(12)}), 'doing', false, agoMs(3)), '⏳'],
        ['绿档-响应', M(row({last_llm_response_at: ago(12)}), 'doing', false, agoMs(3)), '⏳'],
        ['琥珀', M(row({last_llm_request_at: ago(80)}), 'doing', false, null), '⏳'],
        ['红', M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200)), '⏳'],
        ['输出持续中', M(row({last_llm_response_at: ago(200)}), 'doing', false, agoMs(5)), '⏳'],
        ['工具-命令', M(row({turn_step: 'tool', turn_step_detail: 'Bash: npm test'}), 'doing', false, null), '⚙'],
        ['工具-写文件', M(row({turn_step: 'tool', turn_step_detail: 'Edit dispatcher.py'}), 'doing', false, null), '✍'],
        ['证据缺失', M(row({}), 'doing', false, null), '❔'],
        ['待接管', M(row({status: 'idle'}), 'doing', true, null), '📨'],
        ['已中断', M(row({status: 'interrupted'}), 'doing', false, null), '⏸'],
        ['已结束', M(row({status: 'idle'}), 'doing', false, null), '⏹'],
        ['新会话', M(row({status: 'created'}), 'doing', false, null), '💬'],
        ['已完成', M(row({status: 'running'}), 'done', false, null), '✅'],
    ];
    const bad = cases.filter(c => c[1].ico !== c[2]);
    check('A26_状态图标逐态匹配', bad.length === 0, bad.map(c => c[0] + '=' + c[1].ico).join(','));
}

// ---- A27：#421 走查 判定依据值列严重度（交互稿 .st-v.ok/.warn/.bad） ----
{
    const ok = M(row({last_llm_request_at: ago(12)}), 'doing', false, agoMs(3));
    const slow = M(row({last_llm_request_at: ago(80)}), 'doing', false, agoMs(3));
    const slowQuiet = M(row({last_llm_request_at: ago(80)}), 'doing', false, agoMs(80));
    const stall = M(row({last_llm_request_at: ago(200)}), 'doing', false, agoMs(200));
    const live = M(row({last_llm_response_at: ago(200)}), 'doing', false, agoMs(5));
    const tool = M(row({turn_step: 'tool', turn_step_detail: 'Bash: npm test'}), 'doing', false, agoMs(3));
    const idle = M(row({status: 'idle'}), 'doing', false, null);
    const unknown = M(row({}), 'doing', false, null);
    check('A27_绿档三行全 ok',
        ok.signalSev.event === 'ok' && ok.signalSev.model === 'ok' && ok.signalSev.verdict === 'ok',
        JSON.stringify(ok.signalSev));
    check('A27_琥珀档观察中 warn（事件流新鲜时事件行仍 ok）',
        slow.signalSev.model === 'warn' && slow.signalSev.verdict === 'warn' && slow.signalSev.event === 'ok',
        JSON.stringify(slow.signalSev));
    check('A27_事件流静默 >60s → 事件行 warn',
        slowQuiet.signalSev.event === 'warn' && stall.signalSev.event === 'warn',
        JSON.stringify([slowQuiet.signalSev.event, stall.signalSev.event]));
    check('A27_红档判死 bad（事件流静默 warn）',
        stall.signalSev.model === 'bad' && stall.signalSev.verdict === 'bad' && stall.signalSev.event === 'warn',
        JSON.stringify(stall.signalSev));
    check('A27_输出持续中判活 ok',
        live.signalSev.model === 'ok' && live.signalSev.verdict === 'ok' && live.signalSev.event === 'ok',
        JSON.stringify(live.signalSev));
    check('A27_工具态判活 ok', tool.signalSev.verdict === 'ok' && tool.signalSev.event === 'ok',
        JSON.stringify(tool.signalSev));
    check('A27_结束态/证据未知中性不着色',
        idle.signalSev.verdict === '' && idle.signalSev.model === '' && idle.signalSev.event === ''
        && unknown.signalSev.verdict === '' && unknown.signalSev.event === '',
        JSON.stringify([idle.signalSev, unknown.signalSev]));
}

console.log(JSON.stringify({checks: results}));
process.exit(results.every(r => r.pass) ? 0 : 1);
