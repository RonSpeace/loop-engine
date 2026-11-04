// req-332 回归测试 harness（由 tests/test_chat_cancel_feedback.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的真实函数源码
// （function refreshChat / function renderChatStatus / function cancelChat /
//  function selectionGuardActive / function nextChatActivity /
//  function mergeChatStreamEvents），
// 本文件随后定义 DOM/fetch 桩与依赖 mock，按 #332 验收场景逐条断言。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({ name: name, pass: !!pass, detail: detail == null ? '' : String(detail) });
}

// ---- mock 依赖（提取的函数以全局作用域解析，global 属性赋值即可） ----
const calls = { renderChatMessages: 0, renderChatDraft: 0, renderChatSend: 0, showToast: [], afterChatCreated: 0 };
global.renderChatMessages = () => { calls.renderChatMessages += 1; };
global.renderChatDraft = () => { calls.renderChatDraft += 1; };
global.renderChatSend = () => { calls.renderChatSend += 1; };
global.showToast = (msg) => { calls.showToast.push(String(msg)); };
global.afterChatCreated = async () => { calls.afterChatCreated += 1; };
global.displayProjectName = (p) => p;
global.escapeHtml = (s) => String(s);
global.window = { getSelection: () => null };

// renderChatStatus 走真实实现：chatStatus 元素记录 className/dataset/innerHTML（#430 起状态行是共享卡片）
const statusEl = { classList: { toggle() {}, remove() {} }, innerHTML: '', className: '', dataset: {} };
const els = { chatStatus: statusEl, chatMessages: {}, chatHistory: {}, chatDraftBox: {} };
global.document = { getElementById: (id) => els[id] || {} };

// #430 卡片渲染依赖：文案/图标表与 board.py 同值（逐行比对见 tests/test_chat_status_card.py）、
// ticker 不在本 harness 断言面内（状态卡行为在 tests/chat_status_card_harness.js 覆盖）
let chatAddStatusManualOpen = null;
const CHAT_ADD_STATUS_ICO = {creating: '🔄', restoring: '🔄', confirming: '📨', running: '⏳',
    created: '✅', idle: '💬', interrupted: '⏸', auto: '🔄', suspended: '⏸',
    failed: '⛔', closed: '⏹', created_new: '💬'};
global.ensureChatStatusTicker = () => {};

// refreshChat/cancelChat 依赖的全局状态（与 board.py 同名）
let chatState = null;
let chatEvents = [];
let chatLastId = 0;
let chatFetchSeq = 0;          // 在途请求序号（迟到响应丢弃）
let chatFetchAt = 0;           // 增量合并窗口起点（0=窗口空闲）
const CHAT_FETCH_COALESCE_MS = 800;
let chatActionPhase = null;   // #335：renderChatStatus 分阶段反馈（closing/creating）引用
const selectingPanels = new Set();

async function settle(promise) {
    // 等待 refreshChat 内部 600ms 竞态 setTimeout 之外的同步/异步完成
    await promise;
}

(async () => {
    // ---- A：#332 核心——200 会话行带 error 列（取消中断原因）不判为接口错误 ----
    {
        global.fetch = async (url) => (url.includes('/events')
            ? { ok: true, json: async () => [] }
            : { ok: true, json: async () => ({ id: 5, project: 'vip-member', status: 'interrupted', interrupt_source: 'user_cancel', error: '用户取消本轮', draft_title: null }) });
        chatState = { id: 5, project: 'vip-member', status: 'running', activity: null };
        chatEvents = [];
        chatLastId = 0;
        statusEl.innerHTML = '';
        const sendBefore = calls.renderChatSend;
        await settle(refreshChat(true));
        check('A1_200带error不判错：状态合并为interrupted',
            chatState.status === 'interrupted' && chatState.error === '用户取消本轮',
            JSON.stringify(chatState));
        check('A2_不再提前return：renderChatSend被调用（按钮更新）',
            calls.renderChatSend > sendBefore, 'sendCalls=' + calls.renderChatSend);
        check('A3_状态行显示中断文案（可继续会话）',
            statusEl.innerHTML.includes('会话已中断') && statusEl.innerHTML.includes('保留上下文'),
            statusEl.innerHTML);
    }

    // ---- B：真实接口错误（404 会话不存在）有可见提示且不污染状态 ----
    {
        global.fetch = async (url) => (url.includes('/events')
            ? { ok: true, json: async () => [] }
            : { ok: false, status: 404, json: async () => ({ error: '会话不存在' }) });
        chatState = { id: 9, project: 'x', status: 'running', activity: null };
        chatEvents = [];
        chatLastId = 0;
        statusEl.innerHTML = '';
        await settle(refreshChat(true));
        check('B1_404判错：状态行显示服务端错误文案',
            statusEl.innerHTML.includes('会话不存在'), statusEl.innerHTML);
        check('B2_404不合并状态（仍为running）',
            chatState.status === 'running', chatState.status);
    }

    // ---- C：renderChatStatus 支持可选文案（一次性提示/接口错误直显为红色卡片） ----
    {
        statusEl.innerHTML = '';
        renderChatStatus('会话不存在');
        check('C1_override文案直显为红色卡片且无判定依据（不可展开）',
            statusEl.className === 'chat-status st-red' && statusEl.innerHTML.includes('会话不存在')
            && !statusEl.innerHTML.includes('st-detail'), statusEl.className + '|' + statusEl.innerHTML);
        chatState = { id: 1, project: 'p', status: 'interrupted', interrupt_source: 'user_cancel', draft_title: null };
        renderChatStatus();
        check('C2_无参按状态渲染：interrupted+user_cancel显示可继续会话',
            statusEl.innerHTML.includes('会话已中断，可继续会话（保留上下文）'), statusEl.innerHTML);
        chatState = { id: 1, project: 'p', status: 'running', activity: null };
        renderChatStatus();
        check('C3_running显示绿色思考卡片（计时/取消本轮在卡片动作位）',
            statusEl.className === 'chat-status st-green' && statusEl.innerHTML.includes('思考中（模型推理）')
            && statusEl.innerHTML.includes('取消本轮'), statusEl.className + '|' + statusEl.innerHTML);
    }

    // ---- D：取消乐观更新——按钮立即变发送、状态行立即中断，refresh 对账 ----
    {
        global.fetch = async (url, opts) => {
            if (url.endsWith('/cancel')) return { ok: true, json: async () => ({ success: true }) };
            if (url.includes('/events')) return { ok: true, json: async () => [] };
            return { ok: true, json: async () => ({ id: 7, project: 'p', status: 'interrupted', interrupt_source: 'user_cancel', error: '用户取消本轮', draft_title: null }) };
        };
        chatState = { id: 7, project: 'p', status: 'running', activity: null };
        chatEvents = [];
        chatLastId = 0;
        statusEl.innerHTML = '';
        const sendBefore = calls.renderChatSend;
        await settle(cancelChat());
        check('D1_取消成功：乐观更新status=interrupted',
            chatState.status === 'interrupted', chatState.status);
        check('D2_乐观更新后立即渲染按钮（发送/取消态切换）',
            calls.renderChatSend > sendBefore, 'sendCalls=' + calls.renderChatSend);
        check('D3_状态行立即显示中断文案',
            statusEl.innerHTML.includes('会话已中断'), statusEl.innerHTML);
        check('D4_refresh对账保留interrupt_source=user_cancel',
            chatState.interrupt_source === 'user_cancel', JSON.stringify(chatState));
    }

    // ---- E：取消失败有 toast 反馈（HTTP 错误与网络异常两条路径） ----
    {
        global.fetch = async (url, opts) => {
            if (url.endsWith('/cancel')) return { ok: false, status: 500, json: async () => ({ error: '服务繁忙' }) };
            if (url.includes('/events')) return { ok: true, json: async () => [] };
            return { ok: true, json: async () => ({ id: 7, project: 'p', status: 'running', draft_title: null }) };
        };
        chatState = { id: 7, project: 'p', status: 'running', activity: null };
        chatEvents = [];
        chatLastId = 0;
        const toastBefore = calls.showToast.length;
        await settle(cancelChat());
        check('E1_HTTP失败弹toast（含取消失败）',
            calls.showToast.length > toastBefore && calls.showToast[calls.showToast.length - 1].includes('取消失败'),
            calls.showToast.slice(toastBefore).join('|'));
        global.fetch = async (url, opts) => {
            if (url.endsWith('/cancel')) throw new Error('network down');
            return { ok: true, json: async () => ({ id: 7, project: 'p', status: 'running', draft_title: null }) };
        };
        const toast2 = calls.showToast.length;
        await settle(cancelChat());
        check('E2_网络异常弹toast且不抛错',
            calls.showToast.length > toast2 && calls.showToast[calls.showToast.length - 1].includes('取消'),
            calls.showToast.slice(toast2).join('|'));
    }

    // ---- F：超时/异常中断（非 user_cancel）同样不再卡按钮 ----
    {
        global.fetch = async (url) => (url.includes('/events')
            ? { ok: true, json: async () => [] }
            : { ok: true, json: async () => ({ id: 6, project: 'p', status: 'interrupted', interrupt_source: 'timeout', error: '本轮超时', draft_title: null }) });
        chatState = { id: 6, project: 'p', status: 'running', activity: null };
        chatEvents = [];
        chatLastId = 0;
        statusEl.innerHTML = '';
        await settle(refreshChat(true));
        check('F1_超时中断状态正常合并（不再卡running）',
            chatState.status === 'interrupted', chatState.status);
        check('F2_超时中断显示自动恢复文案',
            statusEl.innerHTML.includes('后台自动恢复'), statusEl.innerHTML);
    }

    // ---- G：200 running 正常轮询路径不回归（activity 计算 + 渲染） ----
    {
        global.fetch = async (url) => (url.includes('/events')
            ? { ok: true, json: async () => [] }
            : { ok: true, json: async () => ({ id: 10, project: 'p', status: 'running', error: null, draft_title: null }) });
        chatState = { id: 10, project: 'p', status: 'running', activity: null };
        chatEvents = [];
        chatLastId = 0;
        const msgBefore = calls.renderChatMessages;
        await settle(refreshChat(true));
        check('G1_200 running 正常合并渲染',
            chatState.status === 'running' && calls.renderChatMessages > msgBefore,
            JSON.stringify(chatState));
    }

    // ---- H：重复气泡加固（去重兜底 + 增量窗口合并 + 迟到响应丢弃）----
    // H1：增量响应带着已并入的区间（服务端 poke 与主动全量并发时的现场形态）→ 按 id 去重
    {
        global.fetch = async (url) => (url.includes('/events')
            ? { ok: true, json: async () => ([{ id: 2, kind: 'message', content: 'b' }, { id: 3, kind: 'message', content: 'c' }]) }
            : { ok: true, json: async () => ({ id: 13, project: 'p', status: 'running', draft_title: null }) });
        chatState = { id: 13, project: 'p', status: 'running', activity: null };
        chatEvents = [{ id: 1, kind: 'user', content: 'a' }, { id: 2, kind: 'message', content: 'b' }];
        chatLastId = 2;
        chatFetchAt = 0;
        chatFetchSeq = 0;
        await settle(refreshChat(false));
        const ids = chatEvents.map(e => e.id);
        check('H1_增量重复区间按id去重（幂等，不再拼成4条）',
            ids.join(',') === '1,2,3', ids.join(','));
        check('H1b_游标推进到已并入的最大事件id', chatLastId === 3, 'cursor=' + chatLastId);
    }

    // H2：增量请求在途（窗口内）时，poke 触发的第二次增量被合并，不重复拉取
    {
        let release;
        const gate = new Promise(r => { release = r; });
        let eventsCall = 0;
        global.fetch = async (url) => {
            if (url.includes('/events')) {
                eventsCall += 1;
                await gate;
                return { ok: true, json: async () => ([{ id: 9, kind: 'message', content: 'x' }]) };
            }
            return { ok: true, json: async () => ({ id: 12, project: 'p', status: 'running', draft_title: null }) };
        };
        chatState = { id: 12, project: 'p', status: 'running', activity: null };
        chatEvents = [];
        chatLastId = 0;
        chatFetchAt = 0;
        chatFetchSeq = 0;
        const p1 = refreshChat(false);
        const p2 = refreshChat(false);      // 窗口内：应被跳过
        await settle(p2);
        check('H2_增量窗口内第二次增量被合并（只发一次请求）', eventsCall === 1, 'calls=' + eventsCall);
        release();
        await settle(p1);
        check('H2b_放行后增量正常落地', chatEvents.length === 1 && chatLastId === 9,
            'len=' + chatEvents.length + ' cursor=' + chatLastId);
    }

    // H3：迟到的增量响应（已被后发起的全量刷新取代）必须丢弃，不得把旧区间再拼一次
    {
        const fullEvents = [{ id: 1, kind: 'user', content: 'a' }, { id: 2, kind: 'message', content: 'b' },
                            { id: 3, kind: 'message', content: 'c' }];
        let release;
        const gate = new Promise(r => { release = r; });
        let eventsCall = 0;
        global.fetch = async (url) => {
            if (url.includes('/events')) {
                eventsCall += 1;
                if (eventsCall === 1) {                       // 增量：旧游标 + 慢响应
                    await gate;
                    return { ok: true, json: async () => ([{ id: 2, kind: 'message', content: 'b' },
                                                           { id: 3, kind: 'message', content: 'c' }]) };
                }
                return { ok: true, json: async () => (fullEvents) };
            }
            return { ok: true, json: async () => ({ id: 11, project: 'p', status: 'running', draft_title: null }) };
        };
        chatState = { id: 11, project: 'p', status: 'running', activity: null };
        chatEvents = [{ id: 1, kind: 'user', content: 'a' }];
        chatLastId = 1;
        chatFetchAt = 0;
        chatFetchSeq = 0;
        const pInc = refreshChat(false);     // 增量（卡住）
        const pFull = refreshChat(true);     // 后发起：应取代增量
        await settle(pFull);
        release();
        await settle(pInc);
        const ids = chatEvents.map(e => e.id);
        check('H3_迟到增量被全量取代（不重复拼接）', ids.join(',') === '1,2,3', ids.join(','));
        check('H3b_全量刷新后游标为最大事件id', chatLastId === 3, 'cursor=' + chatLastId);
    }

    const failed = results.filter(r => !r.pass).length;
    console.log(JSON.stringify({ checks: results }));
    process.exit(failed ? 1 : 0);
})().catch(e => {
    console.error(e.stack || String(e));
    process.exit(2);
});
