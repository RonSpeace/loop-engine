#!/usr/bin/env node
/*
 * req-416 前端 SSE 分发/降级 harness：从渲染后的页面 JS 中提取 #416 两个代码块，
 * 在 mock 环境（EventSource/fetch/DOM/定时器 + 既有刷新函数桩）中执行分发逻辑并断言。
 * 由 tests/test_sse_dispatch.py 调用：node sse_dispatch_harness.js <rendered.js>（#33 起 = 配置脚本 + static/board.js）
 * 退出码 0 = 全部通过；非 0 = 有失败。
 */
'use strict';
const fs = require('fs');

function lineStart(src, i) { return src.lastIndexOf('\n', i) + 1; }

const src = fs.readFileSync(process.argv[2], 'utf8');
// #33：主脚本外置 static/board.js 后，开关/阈值由页面配置脚本（window.__REQ_BOARD_CFG__）注入；
// 输入文件首行即该配置脚本，解析出同值对象放入 mock 环境（与浏览器「配置先于主脚本」的执行序一致）
const cfgMatch = src.match(/window\.__REQ_BOARD_CFG__=(\{[^;]*\});/);
if (!cfgMatch) { console.error('未找到 window.__REQ_BOARD_CFG__ 配置行'); process.exit(2); }
const bootCfg = new Function('return (' + cfgMatch[1] + ');')();
const CONST_MARK = '#416 页面事件流（SSE 推送）常量与状态';
const FN_MARK = '#416 页面事件流（SSE 推送：单连接 + 按 topic 分发 + 降级）';
const iConst = src.indexOf(CONST_MARK);
const iFn = src.indexOf(FN_MARK);
if (iConst < 0 || iFn < 0) { console.error('未找到 #416 代码块标记'); process.exit(2); }
const dbgLine = src.indexOf('window.__sseDebug', iConst);
const constBlock = src.slice(lineStart(src, iConst), src.indexOf('\n', dbgLine));
const FN_END = '\n        sseConnect();';
const fnEnd = src.indexOf(FN_END, iFn);
if (fnEnd < 0) { console.error('未找到 sseConnect() 启动点'); process.exit(2); }
const fnBlock = src.slice(lineStart(src, iFn), fnEnd + FN_END.length);

// #418：从渲染后的页面 JS 提取真实函数源码（大括号配平），注入 sandbox 走真实链条
function extractFunction(source, name) {
  let start = source.indexOf('async function ' + name + '(');
  if (start < 0) start = source.indexOf('function ' + name + '(');
  if (start < 0) { console.error('未找到函数 ' + name); process.exit(2); }
  let depth = 0, end = -1;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) { end = i + 1; break; }
  }
  if (end < 0) { console.error('函数 ' + name + ' 大括号不配平'); process.exit(2); }
  return source.slice(start, end);
}
const POLL_SRC = extractFunction(src, 'scheduleReqChatPoll');        // 真实轮询链（H11）
const SESSIONS_SRC = extractFunction(src, 'refreshAgentSessions');   // 真实会话拉取（H3/H5/H10/H12）
// #426：真实列表渲染/差异重绘/判定（H14/H15——签名短路 + 真实重绘链闭环）
const LIST_SRC = extractFunction(src, 'renderList');
const REPAINT_SRC = extractFunction(src, 'repaintForAgentSessions');
const DECISION_SRC = extractFunction(src, 'agentSessionRefreshDecision');
// #22：真实「飞书 tab chat poke 分流」函数（已渲染→增量、未渲染→整页补拉）
const FEISHU_POKE_SRC = extractFunction(src, 'refreshFeishuChatOnPoke');

// 未捕获的 promise rejection 一律登记并判失败（H12 主线：拉取失败必须被消化，不得冒泡）
const unhandled = [];
process.on('unhandledRejection', (e) => { unhandled.push(e); });

const results = [];
function check(name, cond) { results.push([name, !!cond]); }

function makeSandbox(search, hidden) {
  const timers = [];
  const ES = class {
    constructor(url) {
      this.url = url; this.readyState = 0; this.closed = false;
      ES.instances.push(this);
      ES.log.push('open:' + url);
    }
    close() { this.closed = true; this.readyState = 2; ES.log.push('close'); }
  };
  ES.instances = []; ES.log = [];
  const domHandlers = {};
  const calls = {
    refreshProcessIfActive: 0, refreshChat: [], refreshReqChat: [],
    refreshAgentSessions: 0, repaintForAgentSessions: 0, fetch: [],
    listWrites: 0, render: 0, refreshAgentSessionStatus: 0, refreshDispatcherEvents: 0,
    loadFeishuChatList: 0,        // #22：chat poke 的「飞书建卡视图」整页补拉分支计数
    refreshFeishuChatSession: 0,  // #22：飞书 tab 增量刷新单条会话分支计数
  };
  // #426：真实 renderList 的 DOM 桩——#list.innerHTML 写入计数（签名短路断言）+ .workspace classList（Set 版）
  let listHtml = '';
  const listEl = {};
  Object.defineProperty(listEl, 'innerHTML', {
    get: () => listHtml,
    set: (v) => { listHtml = String(v); calls.listWrites += 1; },
  });
  const wsTokens = new Set();
  const wsEl = {
    classList: {
      add: (t) => { wsTokens.add(t); },
      remove: (t) => { wsTokens.delete(t); },
      contains: (t) => wsTokens.has(t),
    },
  };
  const net = { fail: false, sessions: null };   // H12：注入 fetch 失败开关 / 会话快照内容
  const sandbox = {
    EventSource: ES,
    document: {
      hidden: !!hidden, addEventListener: (t, f) => { domHandlers[t] = f; },
      getElementById: (id) => (id === 'list' ? listEl : null),
      querySelector: (sel) => (sel === '.workspace' ? wsEl : null),
    },
    location: { search: search || '' },
    window: { __REQ_BOARD_CFG__: bootCfg },   // #33：页面配置脚本的对象（由输入文件首行解析）
    console,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    setInterval: (fn, ms) => { timers.push({ fn, ms, interval: true }); return timers.length; },
    fetch: (url, opt) => {
      calls.fetch.push(url);
      if (String(url).includes('/api/agent-sessions')) calls.refreshAgentSessions += 1;
      if (net.fail) return Promise.reject(new Error('injected fetch failure'));
      const body = String(url).includes('/api/agent-sessions')
        ? (net.sessions || [{ id: 1, status: 'running' }]) : [];
      return Promise.resolve({ json: async () => body });
    },
    // 既有刷新函数桩（分发目标；refreshAgentSessions/scheduleReqChatPoll 见下方真代理）
    refreshProcessIfActive: () => { calls.refreshProcessIfActive += 1; },
    refreshChat: (full) => { calls.refreshChat.push(full); },
    refreshReqChat: (id, full) => { calls.refreshReqChat.push([id, full]); },
    repaintForAgentSessions: () => { calls.repaintForAgentSessions += 1; },
    // 视图状态（含 #418 真实轮询链所需的门控变量：reqChatTimer 由真函数写、REQ_CHAT_REFRESH_MS 读）
    items: [{ id: 7, status: 'doing' }],
    agentSessions: [], selectedId: 7, selectedSid: 'sid-7',
    selectedAgentSession: null, selectedReqChat: false, addMode: false, chatState: null,
    // #22：新增页级全局（SSE chat poke 按当前 tab 分流）；loadFeishuChatList 为飞书视图整页补拉桩，
    // refreshFeishuChatSession 为单条增量刷新桩（真实 refreshFeishuChatOnPoke 据此分流）
    addPaneTab: 'chat',
    feishuChatList: [],
    loadFeishuChatList: () => { calls.loadFeishuChatList += 1; },
    refreshFeishuChatSession: () => { calls.refreshFeishuChatSession += 1; },
    reqChatState: null, reqChatTimer: null, REQ_CHAT_REFRESH_MS: 1000,
    // #426：真实 renderList / repaintForAgentSessions 链路所需的其余视图状态
    flashId: null, expandedAgents: {}, roleMeta: [], selectedDispatcher: null,
    selectedArtifact: null, agentChatBusy: false, mergeConflicts: {}, confirmations: {},
    __net: net,
  };
  // #426：真实 renderList 注入面（free var：autoProjects/visibleItems/cardHtml/listHtmlSig）
  sandbox.autoProjects = [{ name: 'req-board' }];
  sandbox.__items = [{ id: 7, status: 'doing', title: 'A' }];
  sandbox.visibleItems = () => sandbox.__items;
  sandbox.cardHtml = (i) => '<div class="card" data-id="' + i.id + '" data-sel="' +
    (sandbox.selectedId === i.id) + '" data-flash="' + (i.id === sandbox.flashId) + '">' + i.title + '|' +
    (sandbox.agentSessions || []).map(r => r.id + ':' + r.status).join(',') + '</div>';
  sandbox.listHtmlSig = null;
  sandbox.renderList = () => sandbox.realRenderList();
  sandbox.render = () => { calls.render += 1; };
  sandbox.refreshAgentSessionStatus = () => { calls.refreshAgentSessionStatus += 1; };
  sandbox.refreshDispatcherEvents = () => { calls.refreshDispatcherEvents += 1; };
  sandbox.globalThis = sandbox;
  // #418：非桩代理——转发到 run() 注入的「页面真实函数」（refreshAgentSessions 走真 fetch 链、
  // scheduleReqChatPoll 走真门控 + setTimeout 链），H3/H5/H10/H11/H12 因此覆盖真实实现。
  sandbox.refreshAgentSessions = () => sandbox.realRefreshAgentSessions();
  sandbox.scheduleReqChatPoll = (id) => sandbox.realScheduleReqChatPoll(id);
  return { sandbox, ES, calls, timers, domHandlers };
}

function run(search, hidden) {
  const env = makeSandbox(search, hidden);
  const body = 'with (sandbox) {\n' + constBlock + '\n' + fnBlock +
    '\nsandbox.realScheduleReqChatPoll = ' + POLL_SRC + ';\n' +
    '\nsandbox.realRefreshAgentSessions = ' + SESSIONS_SRC + ';\n' +
    '\nsandbox.realRenderList = ' + LIST_SRC + ';\n' +
    '\nsandbox.realRepaint = ' + REPAINT_SRC + ';\n' +
    '\nsandbox.agentSessionRefreshDecision = ' + DECISION_SRC + ';\n' +
    '\nsandbox.realRefreshFeishuChatOnPoke = ' + FEISHU_POKE_SRC + ';\n' +
    '\nsandbox.refreshFeishuChatOnPoke = (chatId) => sandbox.realRefreshFeishuChatOnPoke(chatId);\n' +
    '\nreturn {sseConnRef: () => sseConn, poke: handleSsePoke, align: sseAlignView, connect: sseConnect,' +
    ' degraded: () => sseDegraded, pending: () => ssePendingPoke,' +
    ' sessionsKey: (rows) => agentSessionsKey(rows)};\n}';
  env.run = new Function('sandbox', body)(env.sandbox);
  return env;
}

(async () => {
  // ---- H1/H2：proc poke 按 req 分发 ----
  {
    const env = run('', false);
    env.ES.instances[0].onopen();                       // 建立
    env.run.poke({ topic: 'proc', req_id: 7 });
    check('H1 proc poke(req 匹配) → refreshProcessIfActive', env.calls.refreshProcessIfActive === 1);
    const before = env.calls.refreshProcessIfActive;
    env.run.poke({ topic: 'proc', req_id: 8 });
    check('H2 proc poke(req 不匹配) 不刷新', env.calls.refreshProcessIfActive === before);
  }
  // ---- H3：sessions poke → 拉取 + 差异重绘（无变化不重绘）----
  {
    const env = run('', false);
    env.ES.instances[0].onopen();
    await new Promise(r => setImmediate(r));   // 先让 onopen 的对齐刷新落地（真 fetch 链异步）
    env.calls.refreshAgentSessions = 0;
    env.calls.repaintForAgentSessions = 0;
    await env.run.poke({ topic: 'sessions' });   // 注册表内容与上次一致 → 只拉取不重绘
    await new Promise(r => setImmediate(r));
    check('H3a sessions poke → refreshAgentSessions 被调用', env.calls.refreshAgentSessions >= 1);
    check('H3b 无变化不重绘（仅拉取）', env.calls.repaintForAgentSessions === 0);
    env.sandbox.__net.sessions = [{ id: 2, status: 'idle' }];   // 注册表变化
    await env.run.poke({ topic: 'sessions' });
    await new Promise(r => setImmediate(r));
    check('H3c 有变化 → repaintForAgentSessions 触发渲染', env.calls.repaintForAgentSessions === 1);
  }
  // ---- H4：chat / reqchat 分发与自筛 ----
  {
    const env = run('', false);
    env.ES.instances[0].onopen();
    env.sandbox.addMode = true; env.sandbox.chatState = { id: 42, status: 'running' };
    env.run.poke({ topic: 'chat', chat_id: 42 });
    check('H4a chat poke（chat_id 匹配）→ refreshChat', env.calls.refreshChat.length === 1);
    env.run.poke({ topic: 'chat', chat_id: 99 });
    check('H4b chat poke（chat_id 不匹配）不刷新', env.calls.refreshChat.length === 1);
    env.sandbox.selectedReqChat = true;
    env.run.poke({ topic: 'reqchat', req_id: 7 });
    check('H4c reqchat poke（req 匹配 + 视图可见）→ refreshReqChat', env.calls.refreshReqChat.length === 1);
    env.sandbox.selectedReqChat = false;
    env.run.poke({ topic: 'reqchat', req_id: 7 });
    check('H4d reqchat poke（视图不可见）不刷新', env.calls.refreshReqChat.length === 1);
  }
  // ---- #22 H4e/H4f：飞书 tab chat poke 走增量还是整页补拉 ----
  {
    const env = run('', false);
    env.ES.instances[0].onopen();
    env.sandbox.addMode = true; env.sandbox.addPaneTab = 'feishu';
    // 已在飞书视图列表中 → 增量刷新单条（不整页清空重拉）
    env.sandbox.feishuChatList = [{ id: 42, events: [] }];
    env.run.poke({ topic: 'chat', chat_id: 42 });
    check('H4e 飞书 tab chat poke（已在列表）→ 增量刷新单条、不整页清空',
      env.calls.refreshFeishuChatSession === 1 && env.calls.loadFeishuChatList === 0);
    // 不在列表（新会话/外部产生）→ 整页补拉列表
    env.run.poke({ topic: 'chat', chat_id: 77 });
    check('H4f 飞书 tab chat poke（不在列表）→ 整页补拉列表',
      env.calls.refreshFeishuChatSession === 1 && env.calls.loadFeishuChatList === 1);
    // 切回聊天建卡 tab：仍走 refreshChat（H4a 路径不受影响）
    env.sandbox.addPaneTab = 'chat';
    env.sandbox.chatState = { id: 42, status: 'running' };
    env.run.poke({ topic: 'chat', chat_id: 42 });
    check('H4g 切回 chat tab 仍走 refreshChat',
      env.calls.refreshChat.length === 1 && env.calls.refreshFeishuChatSession === 1);
  }
  // ---- H5：init 帧对齐 ----
  {
    const env = run('', false);
    env.ES.instances[0].onopen();
    await env.run.poke({ topic: 'init' });
    await new Promise(r => setImmediate(r));
    check('H5 init 帧 → 会话注册表对齐 + 执行过程对齐', env.calls.refreshAgentSessions >= 1 && env.calls.refreshProcessIfActive >= 1);
  }
  // ---- H6：永久失败（CLOSED）立即降级 + 30s 退避 ----
  {
    const env = run('', false);
    const conn = env.ES.instances[0];
    conn.readyState = 2;
    conn.onerror();
    check('H6a CLOSED → 立即 sseDegraded=true（轮询放行）', env.run.degraded() === true);
    check('H6b CLOSED → 连接已关闭且退避定时器 30000ms', conn.closed === true && env.timers.some(t => t.ms === 30000));
  }
  // ---- H7：瞬时失败（CONNECTING）×2 降级；onopen 复位 ----
  {
    const env = run('', false);
    const conn = env.ES.instances[0];
    conn.readyState = 0;
    conn.onerror();
    check('H7a CONNECTING 1 次不降级、不 close（浏览器自动重连）', env.run.degraded() === false && conn.closed === false);
    conn.onerror();
    check('H7b CONNECTING 2 次 → 降级放行轮询', env.run.degraded() === true);
    conn.onopen();
    check('H7c onopen 复位降级', env.run.degraded() === false);
  }
  // ---- H8：单连接（切换 5 次后存活 ≤1，close 先于新建）----
  {
    const env = run('', false);
    for (let k = 0; k < 5; k++) {
      env.sandbox.selectedSid = 'sid-' + k;
      env.run.connect();
      check('H8.' + k + ' 任一时刻存活连接 ≤1', env.ES.instances.filter(i => !i.closed).length === 1);
    }
    check('H8.5 创建 6 条、历史连接均已关闭', env.ES.instances.length === 6 && env.ES.instances.slice(0, 5).every(i => i.closed));
    check('H8.6 close 先于新建（日志成对）', env.ES.log.every((x, i) => x !== 'close' || env.ES.log[i + 1] !== 'close'));
  }
  // ---- H9：开关（?sse=0）不建连接 ----
  {
    const env = run('?sse=0', false);
    check('H9a ?sse=0 → 不创建 EventSource、降级态（轮询常开）', env.ES.instances.length === 0 && env.run.degraded() === true);
    const env2 = run('', false);
    check('H9b 默认开启', env2.ES.instances.length === 1 && env2.run.degraded() === false);
  }
  // ---- H11：降级瞬间重建 reqchat 轮询链（#418：不桩 scheduleReqChatPoll，走真实门控 + 定时器链）----
  {
    const pollTimers = (env) => env.timers.filter(t => t.ms === env.sandbox.REQ_CHAT_REFRESH_MS && !t.interval);
    const reqChatEnv = (status) => {
      const env = run('', false);
      env.sandbox.selectedReqChat = true;
      env.sandbox.reqChatState = { id: 5, status: status };
      return env;
    };
    const env = reqChatEnv('running');
    const conn = env.ES.instances[0];
    conn.readyState = 2;
    conn.onerror();
    check('H11a 永久失败降级 → 真实链排上 REQ_CHAT_REFRESH_MS 定时器', env.run.degraded() === true &&
      pollTimers(env).length === 1 && env.sandbox.reqChatTimer !== null);
    pollTimers(env)[0].fn();   // 到点：真实回调应拉取 refreshReqChat(7, false)
    check('H11a2 定时器到点 → refreshReqChat(7,false)（真下游效果）',
      env.calls.refreshReqChat.some(([id, full]) => id === 7 && full === false));
    const env2 = reqChatEnv('running');
    const c2 = env2.ES.instances[0];
    c2.readyState = 0;
    c2.onerror();
    check('H11b 瞬时失败 1 次不拉起（未降级，真函数门控拦住）',
      pollTimers(env2).length === 0 && env2.sandbox.reqChatTimer === null);
    c2.onerror();
    check('H11c 瞬时失败 2 次降级 → 真函数排上轮询定时器',
      env2.run.degraded() === true && pollTimers(env2).length === 1);
    const env3 = run('', false);   // 不在需求助手视图时不误触发
    env3.ES.instances[0].readyState = 2;
    env3.ES.instances[0].onerror();
    check('H11d 非需求助手视图不触发（无定时器）', pollTimers(env3).length === 0);
    const env4 = reqChatEnv('idle');   // 视图可见但会话 idle：真函数第二道门控拦住
    env4.ES.instances[0].readyState = 2;
    env4.ES.instances[0].onerror();
    check('H11e 视图可见但 idle（无需轮询）→ 无定时器',
      env4.run.degraded() === true && pollTimers(env4).length === 0);
  }
  // ---- H10：后台标签页 poke 不重建 DOM，回前台补一次 ----
  {
    const env = run('', false);
    env.ES.instances[0].onopen();
    await new Promise(r => setImmediate(r));   // 让 onopen 触发的对齐刷新先落地
    env.sandbox.document.hidden = true;
    const before = env.calls.refreshProcessIfActive;
    env.run.poke({ topic: 'proc', req_id: 7 });
    check('H10a 后台 poke 不重建 DOM 且挂起标记', env.calls.refreshProcessIfActive === before && env.run.pending() === true);
    env.sandbox.document.hidden = false;
    await env.run.align();
    check('H10b 回前台补一次（对齐刷新触发）', env.calls.refreshAgentSessions >= 1 && env.calls.refreshProcessIfActive === before + 1);
  }
  // ---- H12：注入 fetch 失败时对齐刷新仍执行（#418：真实 refreshAgentSessions reject 必须被消化）----
  {
    const env = run('', false);
    env.ES.instances[0].onopen();   // 建立：先让一次成功的对齐刷新落地
    await new Promise(r => setImmediate(r));
    const snap0 = JSON.stringify(env.sandbox.agentSessions);
    const beforeProc = env.calls.refreshProcessIfActive;
    const beforeRepaint = env.calls.repaintForAgentSessions;
    env.sandbox.__net.fail = true;   // 此后所有 fetch 一律 reject
    let resolved = false;
    try { await env.run.align(); resolved = true; } catch (e) { resolved = false; }
    check('H12a 拉取失败不冒泡：对齐刷新 promise 正常 resolve', resolved);
    check('H12b 失败仍执行后续面板刷新（refreshProcessIfActive 照常调用）',
      env.calls.refreshProcessIfActive === beforeProc + 1);
    check('H12c 失败不重绘且保留旧会话快照',
      env.calls.repaintForAgentSessions === beforeRepaint && JSON.stringify(env.sandbox.agentSessions) === snap0);
    await env.run.poke({ topic: 'sessions' });   // sessions poke 路径（分发内不 await，靠 microtask 落地）
    await new Promise(r => setImmediate(r));
    check('H12d sessions poke 拉取失败也不产生 unhandled rejection', unhandled.length === 0);
    env.sandbox.__net.fail = false;                                        // 恢复网络 + 注册表变化
    env.sandbox.__net.sessions = [{ id: 2, status: 'idle' }];
    await env.run.align();
    check('H12e 恢复后下一次成功拉取仍重绘（catch 不影响后续路径）',
      env.calls.repaintForAgentSessions === beforeRepaint + 1 &&
      JSON.stringify(env.sandbox.agentSessions) === '[{"id":2,"status":"idle"}]');
  }

  // ---- #426 H3d/H3e：含 server_now 的注册表快照（缺陷形态）——仅 server_now 变化不重绘 ----
  {
    const env = run('', false);
    env.sandbox.__net.sessions = [{ id: 1, status: 'running', req_id: 7, updated_at: 't1', server_now: 111 }];
    env.ES.instances[0].onopen();               // 建立：对齐刷新先落地
    await new Promise(r => setImmediate(r));
    check('H3d-pre 快照落盘（agentSessions 含 server_now）',
      env.sandbox.agentSessions.length === 1 && env.sandbox.agentSessions[0].server_now === 111);
    env.calls.refreshAgentSessions = 0;
    env.calls.repaintForAgentSessions = 0;
    env.sandbox.__net.sessions = [{ id: 1, status: 'running', req_id: 7, updated_at: 't1', server_now: 222 }];
    await env.run.poke({ topic: 'sessions' });  // 仅 server_now 变化（跨响应必不同）
    await new Promise(r => setImmediate(r));
    check('H3d 仅 server_now 变化 → 不重绘（稳定键剔除该字段）',
      env.calls.refreshAgentSessions >= 1 && env.calls.repaintForAgentSessions === 0);
    env.sandbox.__net.sessions = [{ id: 1, status: 'idle', req_id: 7, updated_at: 't1', server_now: 333 }];
    await env.run.poke({ topic: 'sessions' });
    await new Promise(r => setImmediate(r));
    check('H3e status 变（server_now 同时变）→ 仍重绘（真实变化不误吞）',
      env.calls.repaintForAgentSessions === 1);
  }
  // ---- #426 H13：agentSessionsKey 稳定键语义（只剔 server_now）----
  {
    const env = run('', false);
    const key = env.run.sessionsKey;
    const a = [{ id: 1, status: 'running', updated_at: 't1', server_now: 111 }];
    check('H13a 同内容不同 server_now → 键相等',
      key(a) === key([{ id: 1, status: 'running', updated_at: 't1', server_now: 222 }]));
    check('H13b 仅 session_dir 不同 → 键不等（防过度剔除，#422 消费面）',
      key([{ id: 1, session_dir: '/a' }]) !== key([{ id: 1, session_dir: '/b' }]));
    check('H13c 仅 status/updated_at/last_llm_response_at 不同 → 键不等（活性数据仍入比较）',
      key([{ id: 1, status: 'running' }]) !== key([{ id: 1, status: 'idle' }]) &&
      key([{ id: 1, updated_at: 't1' }]) !== key([{ id: 1, updated_at: 't2' }]) &&
      key([{ id: 1, last_llm_response_at: 't1' }]) !== key([{ id: 1, last_llm_response_at: 't2' }]));
    check('H13d null/undefined/非数组（{}）→ 空表键，不抛异常',
      key(null) === '[]' && key(undefined) === '[]' && key({}) === '[]');
    check('H13e 键中不含 server_now 字段名', key(a).indexOf('server_now') === -1);
  }
  // ---- #426 H14：renderList 签名短路（真实函数 + #list/.workspace DOM 桩）----
  {
    const env = run('', false);
    const list = env.sandbox.document.getElementById('list');
    const ws = env.sandbox.document.querySelector('.workspace').classList;
    const render = () => env.sandbox.realRenderList();
    render();
    const w1 = env.calls.listWrites;
    check('H14a 首次渲染写入 #list 一次', w1 === 1 && list.innerHTML.includes('data-id="7"'));
    render();
    check('H14b 同输入连续两次 → 短路不写（计数不增、值不变）',
      env.calls.listWrites === w1 && list.innerHTML.includes('data-id="7"'));
    env.sandbox.__items = [{ id: 7, status: 'doing', title: 'A' }, { id: 8, status: 'open', title: 'B' }];
    render();
    check('H14c items 增加 → 重写一次（不误吞）',
      env.calls.listWrites === w1 + 1 && list.innerHTML.includes('data-id="8"'));
    env.sandbox.__items = [];
    render();
    check('H14d 空态 → innerHTML=\'\' + empty-main 添加', list.innerHTML === '' && ws.contains('empty-main'));
    const w2 = env.calls.listWrites;
    render();
    check('H14e 空态重复调用幂等（空串签名短路、class 保持）',
      env.calls.listWrites === w2 && ws.contains('empty-main'));
    env.sandbox.__items = [{ id: 7, status: 'doing', title: 'A' }];
    render();
    check('H14f 空态↔非空切换重写且 empty-main 移除',
      env.calls.listWrites === w2 + 1 && !ws.contains('empty-main') && list.innerHTML.includes('data-id="7"'));
    const w3 = env.calls.listWrites;
    env.sandbox.selectedId = 8; render();
    env.sandbox.flashId = 7; render();
    env.sandbox.agentSessions = [{ id: 1, status: 'running', req_id: 7 }]; render();
    check('H14g 输入面变化（selectedId/flashId/注册表 status）各自触发重写',
      env.calls.listWrites === w3 + 3);
  }
  // ---- #426 H15：真实重绘链闭环（心跳级变化 → 短路不写 DOM；status→idle → 写一次）----
  {
    const env = run('', false);
    const list = env.sandbox.document.getElementById('list');
    const prevRow = { id: 1, status: 'running', req_id: 7, session_id: 's1', last_llm_response_at: 't1' };
    const prevItem = { id: 7, status: 'doing' };
    env.sandbox.agentSessions = [prevRow];
    env.sandbox.selectedSid = null;
    env.sandbox.selectedAgentSession = 1;         // 会话详情分支（真实 decision 判定）
    env.sandbox.realRenderList();
    const w0 = env.calls.listWrites;
    const st0 = env.calls.refreshAgentSessionStatus;
    env.sandbox.repaintForAgentSessions = env.sandbox.realRepaint;
    env.sandbox.agentSessions = [{ ...prevRow, last_llm_response_at: 't2' }];   // 仅心跳字段变化
    env.sandbox.repaintForAgentSessions(prevItem, prevRow);
    check('H15a 心跳级变化（真判定 status）→ 真实 renderList 短路，不写 #list',
      env.calls.listWrites === w0 && env.calls.refreshAgentSessionStatus === st0 + 1);
    env.sandbox.agentSessions = [{ ...prevRow, status: 'idle', last_llm_response_at: 't2' }];
    env.sandbox.repaintForAgentSessions(prevItem, prevRow);
    check('H15b 行 status→idle → HTML 变化 → 重写一次（不误吞）',
      env.calls.listWrites === w0 + 1 && list.innerHTML.includes('idle'));
  }

  const failed = results.filter(r => !r[1]);
  results.forEach(([n, ok]) => console.log((ok ? 'PASS ' : 'FAIL ') + n));
  console.log(failed.length ? `FAILED ${failed.length}/${results.length}` : `ALL PASS ${results.length}`);
  process.exit(failed.length ? 1 : 0);
})();
