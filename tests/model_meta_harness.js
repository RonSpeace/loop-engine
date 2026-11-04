// #11/#13 会话模型文案契约 harness（由 tests/test_model_settle_fallback.py 拼装调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的纯函数，每个检查组按 typeof 守卫——
// 未注入该组的函数即整体跳过（同一 harness 可被 F4 / F11 / F12 三个用例按需拼装）：
//   - modelMetaText              → F4 组（#11 请求值 → 实际值元信息文案）
//   - modelAlertText             → F11 组（#13 兜底可见共享纯函数）
//   - sessionFooterHtml（及其依赖）→ F12 组（#13 中间栏会话行琥珀提示接线）
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

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

// ==================== F4：#11 modelMetaText ====================
if (typeof modelMetaText === 'function') {
    // F4：降级/未落定（请求值 != 实际值）→ 显示「请求 → 实际」
    check('F4_降级展示请求到实际', modelMetaText({model: 'deepseek/v4.1-flash', model_req: 'glm-5.2'})
        === 'glm-5.2 → deepseek/v4.1-flash',
        modelMetaText({model: 'deepseek/v4.1-flash', model_req: 'glm-5.2'}));

    // F4b：请求值 == 实际值（已落定）→ 按现状只显示实际值
    check('F4b_相等只显示实际值', modelMetaText({model: 'glm-5.2', model_req: 'glm-5.2'}) === 'glm-5.2',
        modelMetaText({model: 'glm-5.2', model_req: 'glm-5.2'}));

    // F4c：请求值为 NULL/缺省（存量行）→ 只显示实际值
    check('F4c_请求值缺失只显示实际值', modelMetaText({model: 'glm-5.2', model_req: null}) === 'glm-5.2',
        modelMetaText({model: 'glm-5.2', model_req: null}));
    check('F4d_无实际值返回空串', modelMetaText({model: null, model_req: 'glm-5.2'}) === ''
        && modelMetaText(null) === ''
        && modelMetaText({}) === '', JSON.stringify([modelMetaText({model: null}), modelMetaText(null)]));

    // F4e：文案出口要经 escapeHtml（元信息行统一转义）——函数本身返回原文，调用方转义
    check('F4e_原文含标记由调用方转义', escapeHtml(modelMetaText({model: 'a<b', model_req: 'x&y'}))
        === 'x&amp;y → a&lt;b', escapeHtml(modelMetaText({model: 'a<b', model_req: 'x&y'})));
}

// ==================== F11：#13 modelAlertText 矩阵 ====================
if (typeof modelAlertText === 'function') {
    // 配置空 → ''（存量行/未解析零误报）
    check('F11_配置空返回空串',
        modelAlertText({model_requested: 'glm-5.2', model_configured: null, model_reason: null}) === ''
        && modelAlertText({model_requested: 'glm-5.2'}) === ''
        && modelAlertText(null) === '',
        JSON.stringify([modelAlertText({model_requested: 'glm-5.2'}), modelAlertText(null)]));

    // 配置 == 请求且无原因 → ''（已落定）
    check('F11_配置等于请求且无原因返回空串',
        modelAlertText({model_requested: 'glm-5.2', model_configured: 'glm-5.2', model_reason: ''}) === '',
        modelAlertText({model_requested: 'glm-5.2', model_configured: 'glm-5.2', model_reason: ''}));

    // 配置 != 请求 → 含「配置 X → 实际 Y」
    const mismatch = modelAlertText({model_requested: 'gpt-5', model_configured: 'glm-5.2', model_reason: ''});
    check('F11_配置不等于请求显示配置到实际', mismatch === '配置 glm-5.2 → 实际 gpt-5', mismatch);

    // 仅原因 → 含「模型未落定」+ 原因
    const reasonOnly = modelAlertText({model_requested: '', model_configured: '', model_reason: 'mismatch：未落定'});
    check('F11_仅原因显示模型未落定', reasonOnly === '模型未落定（mismatch：未落定）', reasonOnly);

    // 两者皆有 → 两段齐全
    const both = modelAlertText({model_requested: 'gpt-5', model_configured: 'glm-5.2', model_reason: '原因X'});
    check('F11_两者皆有文案齐全', both === '配置 glm-5.2 → 实际 gpt-5（原因X）', both);

    // 请求为 NULL/空且配置非空 → ''（不误报；仅按原因提示维度判定）
    check('F11_请求缺失不误报',
        modelAlertText({model_requested: null, model_configured: 'glm-5.2', model_reason: null}) === ''
        && modelAlertText({model_requested: '', model_configured: 'glm-5.2', model_reason: ''}) === ''
        && modelAlertText({model_req: null, model_configured: 'glm-5.2'}) === '',
        JSON.stringify([modelAlertText({model_requested: null, model_configured: 'glm-5.2'}),
                        modelAlertText({model_req: null, model_configured: 'glm-5.2'})]));

    // 请求值优先取 model_requested（#13 新字段），再回落 model_req（#11 旧字段）
    check('F11_请求字段优先序',
        modelAlertText({model_requested: 'req-new', model_req: 'req-old', model_configured: 'cfg'})
        === '配置 cfg → 实际 req-new',
        modelAlertText({model_requested: 'req-new', model_req: 'req-old', model_configured: 'cfg'}));

    // 标记字符保持原样（转义由调用方 escapeHtml / escapeAttr 负责；函数内部不转义）
    const raw = modelAlertText({model_requested: 'a&b', model_configured: 'c<d', model_reason: ''});
    check('F11_标记字符原样返回',
        raw === '配置 c<d → 实际 a&b' && raw.indexOf('<') >= 0 && raw.indexOf('&') >= 0, raw);

    // 与调用方 escapeHtml 组合后为安全 HTML
    check('F11_调用方转义后正确',
        escapeHtml(modelAlertText({model_requested: 'a&b', model_configured: 'c<d'}))
        === '配置 c&lt;d → 实际 a&amp;b',
        escapeHtml(modelAlertText({model_requested: 'a&b', model_configured: 'c<d'})));
}

// ==================== F12：#13 sessionFooterHtml 中间栏接线 ====================
// sessionFooterHtml / sessionList 依赖的自由变量（模块作用域声明，供注入函数闭包解析）。
var artifactInfo = {};
var selectedId = null;
var selectedReqChat = null;
var selectedArtifact = null;
var selectedSid = null;
var selectedAgentSession = null;
var expandedAgents = {};
var roleMeta = [{role: 'dev', display: '开发'}];
var agentSessions = [];
var sessionInfo = {};

if (typeof sessionFooterHtml === 'function' && typeof sessionList === 'function') {
    artifactInfo = {1: []};
    roleMeta = [{role: 'dev', display: '开发'}];
    sessionInfo = {};
    selectedId = null;
    selectedReqChat = null;
    selectedArtifact = null;
    selectedSid = null;
    selectedAgentSession = null;
    expandedAgents = {};

    const mkRow = (cfg, req, reason) => ({req_id: 1, role: 'dev', session_id: 'sid-1',
        model: 'glm-5.2', thinking: null, harness: null, context_pct: null, branch: null,
        workdir: null, model_requested: req, model_source: 'project',
        model_configured: cfg, model_reason: reason, session_dir: null, id: 11,
        poisoned: 0, status: 'idle'});
    const req = {id: 1, req_chat_sid: null, status: 'open'};

    // 有回落提示 → 渲染琥珀行（含标签与完整文案）
    agentSessions = [mkRow('cfg-x', 'glm-5.2', '原因X')];
    const withAlert = sessionFooterHtml(req);
    check('F12_有回落提示渲染琥珀行',
        withAlert.indexOf('session-model-warn') >= 0 && withAlert.indexOf('smw-label') >= 0
        && withAlert.indexOf('模型回落：') >= 0
        && withAlert.indexOf('配置 cfg-x → 实际 glm-5.2（原因X）') >= 0, withAlert);

    // 无提示（配置 == 请求、无原因）→ 不渲染琥珀行（负向：无提示输入零改动）
    agentSessions = [mkRow('glm-5.2', 'glm-5.2', '')];
    const noAlert = sessionFooterHtml(req);
    check('F12_无提示不渲染琥珀行', noAlert.indexOf('session-model-warn') < 0, noAlert);

    // 无会话行 → 不渲染琥珀行
    agentSessions = [];
    const empty = sessionFooterHtml(req);
    check('F12_无会话不渲染琥珀行', empty.indexOf('session-model-warn') < 0, empty);

    // 提示文本经 escapeHtml、title 经 escapeAttr（标记字符不裸出）
    agentSessions = [mkRow('cf<g', 'req&y', 'r<m')];
    const esc = sessionFooterHtml(req);
    check('F12_提示文本经转义',
        esc.indexOf('cf&lt;g') >= 0 && esc.indexOf('req&amp;y') >= 0 && esc.indexOf('r&lt;m') >= 0
        && esc.indexOf('<g') < 0, esc);

    // C6（#50）：历史行仍携带思考三列 → 前端不再读取，不得渲染任何思考提示
    agentSessions = [Object.assign(mkRow('glm-5.2', 'glm-5.2', ''),
        {thinking_requested: 'medium', thinking_configured: 'high',
         thinking_reason: '已回落 medium'})];
    const histThink = sessionFooterHtml(req);
    check('F12_历史思考列不渲染提示',
        histThink.indexOf('session-thinking-warn') < 0 && histThink.indexOf('思考回落：') < 0, histThink);

    // ==================== #53 产物缺失可见化 + legacy 元数据 ====================
    const mkArt = (role, fn, exists, path) => ({role, filename: fn,
        short: 'p/req-1/' + fn, path: path || ('/a/p/req-1/' + fn), exists,
        mtime: exists ? 1 : null, source: exists ? 'current' : null});

    // 存在（含 legacy 命中）：与改动前一致，可点击行 + 分支差异可点击
    artifactInfo = {1: [mkArt('dev', 'self-test.md', true), mkArt('diff', 'diff.patch', true)]};
    agentSessions = [mkRow('glm-5.2', 'glm-5.2', '')];
    const artHit = sessionFooterHtml(req);
    check('F12_产物存在渲染可点击行',
        artHit.indexOf('sa-path') >= 0 && artHit.indexOf('sa-link') >= 0
        && artHit.indexOf("selectArtifact(1,'dev')") >= 0 && artHit.indexOf("selectArtifact(1,'diff')") >= 0
        && artHit.indexOf('artifact-missing') < 0, artHit);

    // 缺失：会话产物与分支差异都显示「产物缺失（路径不存在）」，且不可点击
    artifactInfo = {1: [mkArt('dev', 'self-test.md', false), mkArt('diff', 'diff.patch', false)]};
    const artMiss = sessionFooterHtml(req);
    check('F12_产物缺失显示提示且不可点击',
        artMiss.indexOf('artifact-missing') >= 0 && artMiss.indexOf('会话产物：<span class="sa-missing">产物缺失（路径不存在）') >= 0
        && artMiss.indexOf('分支差异：<span class="sa-missing">产物缺失（路径不存在）') >= 0
        && artMiss.indexOf('sa-link') < 0 && artMiss.indexOf('selectArtifact(') < 0, artMiss);

    // 无会话行 → 不渲染任何产物提示（避免未开发卡噪声）
    agentSessions = [];
    const artNoRow = sessionFooterHtml(req);
    check('F12_无会话行不渲染产物提示',
        artNoRow.indexOf('会话产物：') < 0 && artNoRow.indexOf('产物缺失') < 0, artNoRow);

    // 无 dev 会话行 → diff 缺失不新增孤立提示行
    agentSessions = [Object.assign(mkRow('glm-5.2', 'glm-5.2', ''), {role: 'analysis'})];
    const noDev = sessionFooterHtml(req);
    check('F12_无dev行不渲染孤立缺失差异',
        noDev.indexOf('分支差异') < 0 && noDev.indexOf('产物缺失') < 0, noDev);

    // 缺失行路径经 escapeHtml（title 不裸出标记字符）
    artifactInfo = {1: [mkArt('dev', 'self-test.md', false, '/a/<&/self-test.md')]};
    agentSessions = [mkRow('glm-5.2', 'glm-5.2', '')];
    const escArt = sessionFooterHtml(req);
    check('F12_缺失路径经转义',
        escArt.indexOf('/a/&lt;&amp;/self-test.md') >= 0 && escArt.indexOf('/a/<&') < 0, escArt);
}

process.stdout.write(JSON.stringify({checks: results}));
process.exit(results.every(r => r.pass) ? 0 : 1);
