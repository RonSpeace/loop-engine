        // ==================== #421 会话活性状态行 ====================
        // 判死阈值由服务端注入（board.py AGENT_STALL_SEC 同源，避免前后端漂移）
        const ACTIVITY_STALL_SEC = window.__REQ_BOARD_CFG__.stallSec;
        const ACTIVITY_STALL_FIRST_SEC = window.__REQ_BOARD_CFG__.stallFirstSec;   // 首帧档（仅「等模型响应」证据）
        const ACTIVITY_WARN_SEC = 60;                                   // 偏慢阈值
        const THOUGHT_FRAMES_ABSENT = window.__REQ_BOARD_CFG__.thoughtFramesAbsent;

        // 纯函数状态模型（node 单测）：注册表行 + 需求状态 + busy + 服务端基准 now + 事件流最后一条 ts_ms
        // → {kind, color, label, timerText, canInput, autoOpen, action, ticking, signals:{event,model,verdict}}
        // 优先级 P1~P6：done > running 活动态 > busy 待接管 > interrupted/毒 > idle > created（tech.md §2）。
        // llm 档由「最新证据」决定（request/thought/response；同秒 tie-break response > thought > request）；
        // 红（判死）= age > 阈值 且 事件流静默 > 阈值（事件证据未知时回退只看心跳，文案标注）。
        // 心跳缺失（历史行）→ 中性「运行中」不计时不判卡死；工具态不计入卡死判定、不给重启入口。
        function agentActivityModel(row, itemStatus, busy, nowMs, lastEventMs, opts) {
            opts = opts || {};
            const r = row || {};
            const stall = opts.stallSec > 0 ? opts.stallSec : ACTIVITY_STALL_SEC;
            const stallFirst = opts.stallFirstSec > 0 ? opts.stallFirstSec : ACTIVITY_STALL_FIRST_SEC;
            const thoughtAbsent = opts.thoughtAbsent === undefined ? THOUGHT_FRAMES_ABSENT : !!opts.thoughtAbsent;
            // 展开态：用户手选（manualOpen=true/false）优先，未手选时跟随 autoOpen（1s 重绘不冲掉手动态）
            const manual = opts.manualOpen === undefined ? null : opts.manualOpen;
            const reqStatus = r.req_status || itemStatus;
            const num = t => (typeof t === 'number' && isFinite(t) && t > 0) ? t : null;
            const ageOf = t => { const v = num(t); return v === null ? null : Math.max(0, nowMs / 1000 - v); };
            // 事件时间取服务端下发的 ts_ms（epoch 毫秒），与心跳（epoch 秒）口径不同
            const evNum = (typeof lastEventMs === 'number' && isFinite(lastEventMs) && lastEventMs > 0)
                ? lastEventMs : null;
            const fmt = sec => sec < 60 ? (sec + 's')
                : (Math.floor(sec / 60) + 'm' + String(Math.floor(sec % 60)).padStart(2, '0') + 's');
            const evAge = evNum === null ? null : Math.max(0, (nowMs - evNum) / 1000);
            const evBase = () => evAge === null ? '—（未加载执行过程）'
                : ('最后事件 ' + fmt(Math.round(evAge)) + ' 前'
                   + (evAge > 60 ? '（已超过 1 分钟没有新事件）' : ''));
            // #421 走查·交互稿对齐：状态图标（.st-ico）与判定依据值列严重度（.st-v.ok/.warn/.bad）
            // 图标按档位取（模型侧统一 ⏳，工具态在调用点按写文件/执行命令区分）；严重度由档位 + 事件流静默推出，
            // 不新增数据源、不改任何判定阈值与文案
            const STATUS_ICON = {ok: '⏳', slow: '⏳', stall: '⏳', 'output-live': '⏳', tool: '⚙',
                                 unknown: '❔', waiting: '📨', stopped: '⏸', idle: '⏹',
                                 created: '💬', done: '✅'};
            const STATUS_SEV = {ok: 'ok', 'output-live': 'ok', tool: 'ok', slow: 'warn', stall: 'bad'};
            const mk = (kind, color, label, o) => {
                o = o || {};
                const sec = o.sec === null || o.sec === undefined ? null : Math.max(0, Math.round(o.sec));
                const sev = STATUS_SEV[kind] || '';
                return {kind: kind, color: color, label: label, ico: o.ico || STATUS_ICON[kind] || '⏳',
                        timerText: sec === null ? '' : ('已 ' + fmt(sec)),
                        ticking: sec !== null, canInput: !!o.canInput, autoOpen: !!o.autoOpen,
                        open: manual === null ? !!o.autoOpen : !!manual,
                        action: o.action || null,
                        signalSev: {event: evAge === null ? '' : (evAge > 60 ? 'warn' : 'ok'),
                                    model: sev, verdict: sev},
                        signals: o.signals || {event: '', model: '', verdict: ''}};
            };
            if (reqStatus === 'done') {
                // P1：需求已完成（服务端 400 兜底）——状态行常驻、不显示输入件、无「新建聊天」
                return mk('done', 'gray', '需求已完成，不可继续会话', {sec: null, signals: {
                    event: evBase(), model: '无在途请求（需求已完成）', verdict: '已结束'}});
            }
            if (r.status === 'running') {
                // P2：活动态（含 running+poisoned=1 的人工续跑过渡——毒标记不参与存活判定）
                if ((r.turn_step || '') === 'tool') {
                    // 10 行：执行命令 / 写文件——中性色 + 已运行时长，不计入卡死判定、无重启入口
                    const name = String(r.turn_step_detail || '工具');
                    const isWrite = /^(Edit|Write|MultiEdit)/i.test(name);
                    const outAge = ageOf(r.last_llm_response_at);
                    return mk('tool', 'blue',
                        (isWrite ? '正在写文件（' : '执行命令（') + name + '）',
                        {sec: ageOf(r.last_tool_started_at), ico: isWrite ? '✍' : '⚙', signals: {
                            event: evBase() + ' · 命令执行中，正常',
                            model: '无在途请求（工具执行中）'
                                   + (outAge === null ? '' : ' · 上次输出 ' + fmt(Math.round(outAge)) + ' 前'),
                            verdict: '活着（命令执行中，事件流无新行属正常）'}});
                }
                if ((r.turn_step || '') !== 'llm') {
                    // 阶段证据缺失/未知（非 llm 非 tool，如历史行或心跳写入失败）：fail-safe 不判卡死、
                    // 不给重启入口——服务端守卫同样要求 turn_step='llm'（红态与服务端放行口径一致）
                    return mk('unknown', 'blue', '运行中', {sec: null, signals: {
                        event: evBase(), model: '阶段证据缺失（turn_step 未记录）· 无法判定活性',
                        verdict: '未知（阶段证据缺失，不判卡死）'}});
                }
                const reqAt = num(r.last_llm_request_at), thAt = num(r.last_thought_at),
                      resAt = num(r.last_llm_response_at);
                if (reqAt === null && thAt === null && resAt === null) {
                    // 心跳缺失（历史行/未写）：fail-safe 不误报——不计时、不判卡死、无重启入口
                    return mk('unknown', 'blue', '运行中', {sec: null, signals: {
                        event: evBase(), model: '心跳缺失（历史行）· 无法判定活性',
                        verdict: '未知（无心跳证据，不判卡死）'}});
                }
                let which = 'request', at = reqAt;
                if (thAt !== null && (at === null || thAt >= at)) { which = 'thought'; at = thAt; }
                if (resAt !== null && (at === null || resAt >= at)) { which = 'response'; at = resAt; }
                const age = Math.max(0, nowMs / 1000 - at);
                const evUnknown = evAge === null;
                const silent = evUnknown || evAge > stall;    // 事件流静默（证据未知时回退只看心跳）
                const redThreshold = (which === 'request' && thoughtAbsent) ? stallFirst : stall;
                const first = which === 'request';
                const evSuffix = which === 'thought' ? ' · 模型推理中，无新事件行属正常' : '';
                const modelLine = which === 'request' ? ('llm 请求已发出 ' + fmt(Math.round(age)) + '，等待响应')
                    : which === 'thought' ? ('思考 chunk ' + fmt(Math.round(age)) + ' 前到达 · 无在途请求')
                    : ('最近输出 ' + fmt(Math.round(age)) + ' 前 · 无在途请求');
                if (age > redThreshold && silent) {
                    // 3/8 行：疑似卡死（红）+ 重启入口；降级口径下首帧档不自动展开
                    return mk('stall', 'red', first ? '疑似卡死' : '输出中断（疑似卡死）',
                        {sec: age, autoOpen: !(first && thoughtAbsent), action: 'restart', signals: {
                            event: evBase() + evSuffix,
                            model: modelLine,
                            verdict: (first ? ('疑似卡死（>' + redThreshold + 's 无响应')
                                            : ('输出中断（>' + stall + 's 无新输出或思考'))
                                     + (evUnknown ? '，事件流证据未知）' : '，且事件流无新行）')}});
                }
                if (age > stall && !silent) {
                    // 9 行：输出持续中（事件流仍在刷新）——中性蓝、判活、不给重启入口
                    return mk('output-live', 'blue', '输出持续中（事件流仍在刷新）',
                        {sec: age, signals: {
                            event: evBase() + ' · 事件流仍在刷新，判活', model: modelLine,
                            verdict: '活着（输出持续中，事件流仍在刷新）'}});
                }
                if (age >= ACTIVITY_WARN_SEC) {
                    // 2/5/7 行：偏慢（琥珀）+ 自动展开判定依据
                    const label = first ? '等模型响应（偏慢）'
                        : which === 'thought' ? '思考偏慢（推理中）' : '响应偏慢（生成中）';
                    const verdict = first ? '观察中（等模型响应偏慢）'
                        : which === 'thought' ? '观察中（推理偏慢）' : '观察中（生成偏慢）';
                    return mk('slow', 'amber', label, {sec: age, autoOpen: true, signals: {
                        event: evBase() + evSuffix, model: modelLine, verdict: verdict}});
                }
                // 1/4/6 行：正常（绿），收起判定依据
                const label = first ? '等模型响应'
                    : which === 'thought' ? '思考中（模型推理）' : '响应中（流式输出）';
                const verdict = first ? '活着（正常推理中）'
                    : which === 'thought' ? '活着（思考中，模型推理）' : '活着（响应流式中）';
                return mk('ok', 'green', label, {sec: age, signals: {
                    event: evBase() + evSuffix, model: modelLine, verdict: verdict}});
            }
            if (busy) {
                // P3：已发送待接管（调度器尚未 claim 或尚未置 running）
                return mk('waiting', 'blue', '已发送，等待调度器接管…', {sec: null, signals: {
                    event: evBase(), model: '无在途请求（等待调度器接管）', verdict: '等待接管'}});
            }
            if (r.status === 'interrupted' || r.poisoned) {
                // P4：自动路径已停止（中断/毒会话）——灰、可输入
                return mk('stopped', 'gray', '自动路径已停止，可发消息继续',
                    {canInput: true, action: 'newSession', signals: {
                        event: evBase(), model: '无在途请求（会话已中断）',
                        verdict: '已停止（可人工续跑）'}});
            }
            if (r.status === 'idle') {
                // P5：会话已结束、卡未完成——灰、可输入（不是卡死）
                return mk('idle', 'gray', '会话已结束 · 需求仍在进行中，可发消息继续',
                    {canInput: true, action: 'newSession', signals: {
                        event: evBase(), model: '无在途请求（会话已结束）',
                        verdict: '会话已结束，非卡死（卡未完成，可人工续跑）'}});
            }
            // P6：新会话（created / 无 session_id）
            return mk('created', 'gray', '新会话（未开始）：发送第一条消息后开始',
                {canInput: true, action: 'newSession', signals: {
                    event: evBase(), model: '无在途请求（新会话）', verdict: '未开始'}});
        }

        // #421 状态行视图态：manualOpen=null 跟随自动展开；切换会话/全量重建时复位
        let agentStatusManualOpen = null;
        // 需求助手 / 聊天建卡的状态卡展开态（同款手选记忆；切视图/重建时复位）
        let reqChatStatusManualOpen = null;
        let chatAddStatusManualOpen = null;
        let agentServerClockOffsetMs = 0;     // server_now*1000 − Date.now()（心跳与事件 age 的服务端基准）
        let lastProcEventAtMs = null;         // 事件流最后一条（服务端 ts_ms）
        let agentStatusPointerDown = false;   // 指针按下期间跳过重绘（防吞行内「重启调度器」等按钮点击）
        let chatStatusTicker = null;          // 三处状态行共用的 1s ticker（同一时刻只挂一个状态卡）

        function agentStatusNowMs() { return Date.now() + agentServerClockOffsetMs; }

        function agentStatusRow() {
            if (!selectedAgentSession) return null;
            return (agentSessions || []).find(x => x.id === selectedAgentSession && x.req_id === selectedId) || null;
        }

        // ==================== 统一状态卡片（三处状态行单一实现） ====================
        // 调度器会话（#agentChatStatus）/ 需求助手（#reqChatStatus）/ 聊天建卡（#chatStatus）共用同一套
        // DOM 结构、配色档位、展开交互与 1s tick；差异只在各自的「状态模型」纯函数：
        //   agentActivityModel（会话活性，含判死/重启入口）/ reqChatActivityModel（需求助手）/
        //   chatAddActivityModel（聊天建卡）。
        // 模型字段：{kind, color, label, ico, timerText, canInput, open, ticking, action,
        //            signalSev{event,model,verdict}, signals{event,model,verdict}}
        // canInput=false → 由调用方给容器加 .no-input（只隐藏输入件，卡片内动作按钮不受影响）。
        // signals 三项全空 → 不渲染判定依据与箭头（一次性提示/接口错误卡片只有头部）。
        // #421 走查 判定依据三行 = 键列 + 值列（值列按 signalSev 三色）；点击展开只挂在头部 .st-head
        // （点判定依据正文不再收起，正文可正常选中复制）
        const CHAT_STATUS_SIG_KEY = {'事件流': 'event', '模型层': 'model', '判定': 'verdict'};

        function chatStatusCardHtml(m, actionsHtml) {
            const open = !!m.open;
            const sev = m.signalSev || {};
            const sg = m.signals || {};
            const hasSignals = !!(sg.event || sg.model || sg.verdict);
            const sigRow = (k, text, s) => `<div class="st-line" data-k="${k}">`
                + `<span class="st-k">${k}</span>`
                + `<span class="st-v ${s || ''}">${escapeHtml(text)}</span></div>`;
            return `<div class="st-head${hasSignals ? '' : ' st-static'}"`
                + (hasSignals ? ' onclick="toggleChatStatusDetail(this)" title="点击展开 / 收起判定依据"' : '')
                + `>`
                + `<span class="st-ico">${escapeHtml(m.ico || '')}</span>`
                + `<span class="st-main">${escapeHtml(m.label)}</span>`
                + (m.timerText ? `<span class="st-timer">${escapeHtml(m.timerText)}</span>` : '')
                + (actionsHtml ? `<span class="st-act">${actionsHtml}</span>` : '')
                + (hasSignals ? `<span class="st-arw">${open ? '▼' : '▶'}</span>` : '')
                + `</div>`
                + (hasSignals
                    ? `<div class="st-detail">`
                      + sigRow('事件流', sg.event, sev.event)
                      + sigRow('模型层', sg.model, sev.model)
                      + sigRow('判定', sg.verdict, sev.verdict)
                      + `</div>`
                    : '');
        }

        // 状态模型 → 卡片 DOM：档位/展开态落到 className 与 dataset（tick 靠 dataset.kind 判是否需整卡重建）
        function applyChatStatusCard(el, m, actionsHtml) {
            el.className = 'chat-status st-' + m.color + (m.open ? ' open' : '');
            el.dataset.kind = m.kind;
            el.dataset.open = m.open ? '1' : '0';
            el.innerHTML = chatStatusCardHtml(m, actionsHtml);
        }

        // 不可输入时隐藏输入件：容器 = 状态卡所在 .chat-input-row 的父节点（三处结构一致）
        function setStatusNoInput(el, on) {
            const row = el.closest ? el.closest('.chat-input-row') : null;
            const pane = row && row.parentElement;
            if (pane && pane.classList) pane.classList.toggle('no-input', !!on);
        }

        // 点头部展开/收起：展开态由各视图的手选变量记忆（用户手选后 1s tick 不再改变展开态）
        function toggleChatStatusDetail(head) {
            const el = head && head.closest ? head.closest('.chat-status') : null;
            if (!el) return;
            const open = el.dataset.open !== '1';
            if (el.id === 'agentChatStatus') { agentStatusManualOpen = open; renderAgentChatStatus(agentStatusRow(), agentChatBusy); }
            else if (el.id === 'reqChatStatus') { reqChatStatusManualOpen = open; renderReqChatStatus(); }
            else if (el.id === 'chatStatus') { chatAddStatusManualOpen = open; renderChatStatus(); }
        }

        // 1 秒渲染 tick（不发网络请求）：只在文本节点级更新计时/相对时长/文案，档位变化走整卡重建
        function tickChatStatusCard(el, m, rebuild) {
            if (el.dataset.kind !== m.kind) { rebuild(); return; }
            // 计时的出现/消失属结构变化（节点不存在时无法按文本节点更新，如事件流刚拉到、计时才开始），
            // 交回整卡重建；否则「已 Ns」会一直不显示，直到某次档位变化才补上
            if (!!m.timerText !== !!el.querySelector('.st-timer')) { rebuild(); return; }
            // 同档位内文案可能变化（绿档 等模型响应 → 思考中 / 琥珀档三种偏慢文案），一并按文本节点级更新
            const setTxt = (sel, txt) => {
                const n = el.querySelector(sel);
                if (n && n.textContent !== txt) n.textContent = txt;
            };
            setTxt('.st-ico', m.ico || '');
            setTxt('.st-main', m.label);
            setTxt('.st-timer', m.timerText);
            if (el.dataset.open === '1') {
                const sev = m.signalSev || {};
                el.querySelectorAll('.st-line').forEach(ln => {
                    const key = CHAT_STATUS_SIG_KEY[ln.dataset.k];
                    const v = ln.querySelector('.st-v');
                    if (!key || !v || !m.signals || m.signals[key] === undefined) return;
                    v.textContent = m.signals[key];
                    v.className = 'st-v ' + (sev[key] || '');
                });
            }
        }

        // 单一 1s ticker：三处视图互斥（右栏同一时刻只挂一个状态卡），按当前挂载的节点分派
        function chatStatusTick() {
            if (agentStatusPointerDown) return;   // 指针按下期间跳过重绘（防吞行内按钮点击）
            if (document.getElementById('agentChatStatus')) { agentStatusTickOnce(); return; }
            if (document.getElementById('reqChatStatus')) { reqChatStatusTickOnce(); return; }
            if (document.getElementById('chatStatus')) chatAddStatusTickOnce();
        }

        function ensureChatStatusTicker(on) {
            if (on) {
                if (!chatStatusTicker) {
                    chatStatusTicker = setInterval(() => { if (!document.hidden) chatStatusTick(); }, 1000);
                }
            } else if (chatStatusTicker) {
                clearInterval(chatStatusTicker);
                chatStatusTicker = null;
            }
        }

        // 状态模型 → DOM。1 秒 tick 只改文本节点；档位变化才整卡重建；点击展开态由 agentStatusManualOpen 保持
        function renderAgentChatStatus(row, busy) {
            const el = document.getElementById('agentChatStatus');
            if (!el) return;
            const item = items.find(i => i.id === ((row && row.req_id != null) ? row.req_id : selectedId));
            const m = agentActivityModel(row, item && item.status, !!(busy || agentChatBusy),
                                         agentStatusNowMs(), lastProcEventAtMs,
                                         {manualOpen: agentStatusManualOpen});
            setStatusNoInput(el, !m.canInput);
            const input = document.getElementById('agentChatInput');
            const btn = document.getElementById('agentChatSend');
            if (input) input.disabled = !m.canInput;
            if (btn) btn.disabled = !m.canInput;
            const act = m.action === 'restart'
                ? '<button class="st-btn st-btn-primary" onclick="event.stopPropagation();restartDispatcherFromStatus()">重启调度器</button>'
                : '';
            const newLink = m.action === 'newSession' && selectedId
                ? `<a href="#" class="chat-new-session" onclick="event.preventDefault();event.stopPropagation();newAgentSession(${selectedId})">新建聊天</a>`
                : '';
            applyChatStatusCard(el, m, act + newLink);
            ensureChatStatusTicker(!!m.ticking);
        }

        // 1 秒渲染 tick（不发网络请求）：模型重算交给共享 tick，档位变化整卡重建
        function agentStatusTickOnce() {
            if (agentStatusPointerDown) return;
            const el = document.getElementById('agentChatStatus');
            const row = agentStatusRow();
            if (!el || !row) return;
            const item = items.find(i => i.id === selectedId);
            const m = agentActivityModel(row, item && item.status, agentChatBusy,
                                         agentStatusNowMs(), lastProcEventAtMs);
            tickChatStatusCard(el, m, () => renderAgentChatStatus(row, agentChatBusy));
        }

        document.addEventListener('pointerdown', () => { agentStatusPointerDown = true; }, true);
        document.addEventListener('pointerup', () => { agentStatusPointerDown = false; }, true);
        document.addEventListener('pointercancel', () => { agentStatusPointerDown = false; }, true);

        // #421 疑似卡死 → 重启调度器（二次确认 + 服务端红态复核；失败透出服务端守卫原因）
        async function restartDispatcherFromStatus() {
            const row = agentStatusRow();
            if (!row || !selectedId) return;
            if (!confirm('确认重启调度器？\n\n仅在判定会话疑似卡死时使用：其它运行中的会话会被中断，'
                       + '卡死进程可能残留为孤儿进程（需人工 ps/pgrep 定位后 kill）。')) return;
            try {
                const res = await fetch('/api/dispatcher/restart', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({req_id: selectedId, agent_session_id: row.id, confirm: true})
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { showToast('重启失败: ' + (data.error || res.status), true); return; }
                showToast('调度器已重启' + (data.detail ? '（' + data.detail + '）' : ''));
                refreshHealth();
            } catch (e) { showToast('重启失败: ' + e, true); }
        }

        // #297：刷新注册表行对应的状态行与输入态。调用方已持有最新注册表行时传 freshRow 直接渲染
        // （autoRefresh 手里已有新旧两份数据，避免额外请求）；未传时自行拉取注册表后按行 id 反查。
        async function refreshAgentSessionStatus(row, freshRow) {
            if (!row) return;
            if (!freshRow) {
                await refreshAgentSessions();
                freshRow = (agentSessions || []).find(x => x.id === row.id);
            }
            if (freshRow) renderAgentChatStatus(freshRow);
        }

        // #297：autoRefresh 会话详情视图差异判定（纯函数，tests/process_markdown_harness.js 断言）。
        // 返回 'full'：全量 render()（聊天框/「思考中…」/输入禁用只在视图重建时计算——需求转 done、
        //   注册表行出现/消失、session_id 变化均属此类）；'status'：不重建面板，仅刷新状态行与输入态
        //   （注册表行字段变化，如 running→idle，事件流滚动位置不重置）；'none'：无变化，
        //   维持 renderList() + refreshProcessIfActive() 增量刷新。
        // 入参：itemOld/itemNew=旧/新需求项（可能为 undefined），rowOld/rowNew=旧/新注册表行（可能为 null/undefined）。
        function agentSessionRefreshDecision(itemOld, itemNew, rowOld, rowNew) {
            // 需求状态变化（如 doing→done）：聊天框渲染条件 i.status==='done'，必须全量重建才生效
            if ((itemOld && itemOld.status) !== (itemNew && itemNew.status)) return 'full';
            // 注册表行出现/消失：行消失走 renderDetailPane 现有兜底；空会话→真实会话
            // （session_id 从无到有）需重建面板出现事件流并重新打标 dataset.sid
            if (!rowOld || !rowNew) return 'full';
            // session_id 变化（极端场景：调度器补写 session_id / 换会话）→ 全量重建
            if (rowOld.session_id !== rowNew.session_id) return 'full';
            // #421：req_status 变化（done 兜底）→ 全量重建——输入件显隐与状态行需按新需求状态重算
            if (rowOld.req_status !== rowNew.req_status) return 'full';
            // 其余展示字段变化（status/poisoned/model/harness/context_pct/branch/workdir
            // 与 #421 活性心跳字段）：只刷新状态行与输入态（renderAgentChatStatus 幂等且不触碰事件流面板）
            const FIELDS = ['status', 'poisoned', 'model', 'harness', 'context_pct',
                            'branch', 'workdir', 'turn_step', 'turn_step_detail',
                            'last_llm_request_at', 'last_llm_response_at', 'last_thought_at',
                            'last_tool_started_at'];
            for (const f of FIELDS) {
                if (rowOld[f] !== rowNew[f]) return 'status';
            }
            return 'none';
        }

        // #284 乐观回显：发送成功即本地渲染用户消息，调度器真实写入事件流后按内容去重移除；
        // TTL 兜底（调度器异常未消费时 2 分钟后移除，避免永久残留）
        let pendingUserMsgs = [];   // [{text, ts}]
        const PENDING_USER_MSG_TTL = 120000;

        async function sendAgentChat() {
            const input = document.getElementById('agentChatInput');
            if (!input) return;
            const text = (input.value || '').trim();
            if (!text) return;
            if (!selectedId) return;
            if (agentDraft && agentDraft.reqId === selectedId && !selectedAgentSession) {
                // #431 草稿态首条消息：新端点单事务落注册行（session_id 空）+ 入队 pending 命令；
                // 角色由服务端按卡上快照裁决（前端不传 role，避免前后端解析不一致造成 400）。
                // 失败保留输入与草稿态（不落库、不切视图）
                const res = await fetch(`/api/requirements/${selectedId}/sessions/messages`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json',
                              'X-Actor': 'manual:' + encodeURIComponent('网页新建会话')},
                    body: JSON.stringify({text})
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok || !data.id) {
                    showToast('发送失败: ' + (data.error || res.status), true);
                    return;
                }
                clearAgentDraft(selectedId);   // 已落库：清草稿（含 localStorage 键）
                agentDraft = null;
                selectedAgentSession = data.id;
                input.value = '';
                pendingUserMsgs.push({text, ts: Date.now()});
                agentChatBusy = true;
                agentChatSeenRunning = false;
                agentChatSentAt = Date.now();
                await refreshAgentSessions();   // 草稿视图 → 待接管会话视图（中间栏仍不展示该行）
                render();
                pollAgentSessionUntilIdle(data.id);
                return;
            }
            if (!selectedAgentSession) return;
            const res = await fetch(`/api/requirements/${selectedId}/sessions/${selectedAgentSession}/messages`, {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})
            });
            if (res.ok) {
                input.value = '';
                saveAgentChatDraft(selectedId, selectedAgentSession, '');
                pendingUserMsgs.push({text, ts: Date.now()});
                agentChatBusy = true;
                agentChatSeenRunning = false;
                agentChatSentAt = Date.now();
                await refreshAgentSessions();
                const row = (agentSessions || []).find(x => x.id === selectedAgentSession);
                if (row && row.session_id) refreshProcess(selectedId, row.session_id);  // 立即回显（含 pending 合并）
                if (row) renderAgentChatStatus(row, true);
                pollAgentSessionUntilIdle(selectedAgentSession);
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('发送失败: ' + (err.error || res.status), true);
            }
        }

        // 会话详情聊天框未发送草稿：按 req_id + 会话行 id 存 localStorage（切视图/刷新后回填），发送成功后清除
        const AGENT_CHAT_DRAFT_KEY = 'reqBoardAgentChatDrafts';
        function getAgentChatDrafts() {
            try { return JSON.parse(localStorage.getItem(AGENT_CHAT_DRAFT_KEY) || '{}') || {}; }
            catch (e) { return {}; }
        }
        function agentChatDraftKey(reqId, rowId) { return reqId + ':' + rowId; }
        function saveAgentChatDraft(reqId, rowId, text) {
            const drafts = getAgentChatDrafts();
            const key = agentChatDraftKey(reqId, rowId);
            if (text) drafts[key] = text;
            else delete drafts[key];
            try { localStorage.setItem(AGENT_CHAT_DRAFT_KEY, JSON.stringify(drafts)); } catch (e) {}
        }
        function restoreAgentChatDraft(reqId, row) {
            const input = document.getElementById('agentChatInput');
            if (!input) return;
            input.value = getAgentChatDrafts()[agentChatDraftKey(reqId, row.id)] || '';
            // DOM 每次重建后重新绑定保存（程序赋值不触发 input 事件，回填不会误存）
            input.addEventListener('input', () => saveAgentChatDraft(reqId, row.id, input.value));
        }

        // #431 新建聊天草稿文本：按需求 id 存 localStorage（刷新/切走后再点「新建聊天」可续写；
        // 草稿只在前端，不入 agent_sessions、不写 manual_commands）。清空输入框即删键——
        // 发送前不留「脏状态」：刷新后不再自动进入草稿态。
        const AGENT_DRAFT_KEY = 'reqBoardAgentDrafts';
        function getAgentDrafts() {
            try { return JSON.parse(localStorage.getItem(AGENT_DRAFT_KEY) || '{}') || {}; }
            catch (e) { return {}; }
        }
        function agentDraftText(reqId) { return getAgentDrafts()[String(reqId)] || ''; }
        function saveAgentDraft(reqId, text) {
            const drafts = getAgentDrafts();
            if (text) drafts[String(reqId)] = text;
            else delete drafts[String(reqId)];
            try { localStorage.setItem(AGENT_DRAFT_KEY, JSON.stringify(drafts)); } catch (e) {}
        }
        function clearAgentDraft(reqId) { saveAgentDraft(reqId, ''); }
        // 草稿视图输入框回填 + 绑定保存（与 restoreAgentChatDraft 同型；程序赋值不触发 input 事件）
        function restoreAgentDraft(reqId) {
            const input = document.getElementById('agentChatInput');
            if (!input) return;
            input.value = agentDraftText(reqId);
            input.addEventListener('input', () => saveAgentDraft(reqId, input.value));
        }

        let agentChatTimer = null;
        let agentChatBusy = false;
        let agentChatSeenRunning = false;
        let agentChatSentAt = 0;
        function pollAgentSessionUntilIdle(asid) {
            if (agentChatTimer) clearInterval(agentChatTimer);
            // #388 待接管行 → 真实会话视图重建：#431 起「新建聊天」首条消息落的是无 session_id 的行，其视图
            // （「新会话尚未开始…」）没有 id=procEvents 的事件流面板。发送第一条消息后调度器
            // 写入 session_id，但 session_id 从无到有的变化会被本轮询抢先刷新全局 agentSessions
            // 吞掉（autoRefresh 的 agentSessionRefreshDecision 从旧快照里已读不到旧值，永远判不出
            // 'full'），事件流面板因此永不出现。这里记录当前视图面板对应的 sid（空会话视为 null），
            // 注册表 session_id 出现/变化时主动重建右栏，让 renderDetailPane 渲染出 procEvents 面板。
            const evPanel = document.getElementById('procEvents');
            let lastSid = evPanel ? (evPanel.dataset.sid || null) : null;
            agentChatTimer = setInterval(async () => {
                if (!selectedId || !asid) { clearInterval(agentChatTimer); agentChatTimer = null; return; }
                // #284：用户切到其它会话后停止跟踪轮询，防止把在跑会话的事件灌进当前会话面板
                // #372：切走必须同步复位 busy，否则残留 true 会永久堵死 autoRefresh 的状态行刷新守卫
                if (selectedAgentSession !== asid) { agentChatBusy = false; clearInterval(agentChatTimer); agentChatTimer = null; return; }
                await refreshAgentSessions();
                const row = (agentSessions || []).find(x => x.id === asid);
                if (!row) {
                    agentChatBusy = false;
                    clearInterval(agentChatTimer); agentChatTimer = null; return;
                }
                if (row.session_id && row.session_id !== lastSid) {
                    lastSid = row.session_id;
                    renderDetailPane();
                }
                if (row.session_id) refreshProcess(selectedId, row.session_id);
                if (row.status === 'running') agentChatSeenRunning = true;
                if (agentChatBusy) {
                    // 已发送但调度器尚未 claim（pending）时也保持“思考中”，
                    // 直到看到 running 后回到 idle（或超时兜底）才恢复可输入
                    renderAgentChatStatus(row, true);
                    const done = agentChatSeenRunning && row.status !== 'running';
                    const stale = Date.now() - agentChatSentAt > 60000;
                    if (done || stale) {
                        agentChatBusy = false;
                        renderAgentChatStatus(row);
                        clearInterval(agentChatTimer);
                        agentChatTimer = null;
                    }
                } else {
                    renderAgentChatStatus(row);
                }
                if (!agentChatBusy && row.status !== 'running') {
                    clearInterval(agentChatTimer);
                    agentChatTimer = null;
                }
            }, CHAT_REFRESH_MS);
        }

        // #431「新建聊天」：不再预建空会话行（旧端点已 410）——只进入前端草稿态（内存 + localStorage），
        // 发送首条消息才由新端点落注册行。同步复位忙碌态并停会话轮询：防草稿态首 1s 落 P3
        //「已发送，等待调度器接管…」且输入件被禁用（切走语义同 #372）
        function newAgentSession(id) {
            agentDraft = {reqId: id};
            selectedId = id;
            addMode = false;
            selectedSid = null;
            selectedAgentSession = null;
            selectedDispatcher = false;
            selectedArtifact = null;
            selectedReqChat = false;
            agentChatBusy = false;
            agentChatSeenRunning = false;
            if (agentChatTimer) { clearInterval(agentChatTimer); agentChatTimer = null; }
            render();
        }

        // 右栏调度器动作时间线视图（整个需求的调度过程回放）
        // #51：顶部标题 + 视图切换（流程骨架默认 / 全部事件）+ 全部展开/折叠；
        // 统计条与面板由 renderDispatcherBody 在事件拉取后填充。
        function dispatcherHtml(i) {
            const tab = (view, label) => `<button type="button" class="disp-tab${
                dispatcherView === view ? ' active' : ''}" data-disp-view="${view}"
                role="tab" aria-selected="${dispatcherView === view ? 'true' : 'false'}"
                onclick="setDispatcherView('${view}')">${label}</button>`;
            return `
            <div class="detail-pane fill">
                <div class="proc-head">
                    <div class="proc-session-info">
                        <span class="proc-role">调度器：</span><span class="proc-sid">需求 #${i.id} · <span data-i18n-skip>${escapeHtml(i.title)}</span></span>
                    </div>
                </div>
                <div class="proc-title disp-titlebar">
                    <span class="disp-heading">调度历史回放</span>
                    <div class="disp-toolbar">
                        <div class="disp-tabs" role="tablist">${tab('skeleton', '流程骨架')}${tab('events', '全部事件')}</div>
                        <div class="disp-expand-actions">
                            <button type="button" class="disp-action" id="dispExpandAll" disabled onclick="expandAllDispatcherStages()">全部展开</button>
                            <button type="button" class="disp-action" id="dispCollapseAll" disabled onclick="collapseAllDispatcherStages()">全部折叠</button>
                        </div>
                    </div>
                </div>
                <div class="disp-stats" id="dispatchStats"></div>
                <div class="proc-events" id="dispatcherEvents"><div class="ev-empty">加载中…</div></div>
            </div>`;
        }

        // 右栏会话产物详情视图：头部展示阶段/文件名/完整路径，正文按类型加载
        // （图片直接 <img> 展示；.md/.svg 走增强 markdown 渲染，404 等失败提示产物不存在）
        function artifactHtml(i) {
            // #404：角色标签走 roleMeta 注入；'diff' 产物 key 特判保留（非会话角色映射，明确不并入角色表）
            const rm = roleMeta.find(r => r.role === selectedArtifact.role);
            const roleLabel = rm ? (rm.display || rm.role)
                : (selectedArtifact.role === 'diff' ? '分支差异' : selectedArtifact.role);
            return `
            <div class="detail-pane fill">
                <div class="proc-head">
                    <div class="proc-session-info">
                        <span class="proc-role">${roleLabel}产物：</span><span class="proc-sid">${escapeHtml(selectedArtifact.filename)}</span>${selectedArtifact.mtime ? ` · 创建于 ${escapeHtml(formatMtime(selectedArtifact.mtime))}` : ''}
                        <span class="proc-dir">产物路径：${escapeHtml(selectedArtifact.path)}</span>
                    </div>
                </div>
                <div class="artifact-body" id="artifactBody"><div class="ev-empty">加载中…</div></div>
            </div>`;
        }
        async function loadArtifactContent(id, art) {
            const body = document.getElementById('artifactBody');
            if (!body) return;
            if (/\.(png|jpe?g|gif|webp)$/i.test(art.filename)) {
                body.innerHTML = `<img src="/api/requirements/${id}/artifacts/${encodeURIComponent(art.filename)}" class="artifact-img" alt="${escapeHtml(art.filename)}">`;
                return;
            }
            const isPatch = /\.patch$/i.test(art.filename);
            try {
                const res = await fetch(`/api/requirements/${id}/artifacts/${encodeURIComponent(art.filename)}`, {cache: 'no-store'});
                if (!res.ok) { body.innerHTML = '<div class="ev-empty">产物不存在或已被移除</div>'; return; }
                // #381：diff.patch 走类 GitHub diff 渲染（自写 unified diff 解析器）；
                // 不进 renderArtifactMd——diff 的 + - / ==== / <script> 会被 markdown 增强误解析
                body.innerHTML = isPatch ? renderDiff(await res.text()) : renderArtifactMd(await res.text());
            } catch (e) {
                body.innerHTML = '<div class="ev-empty">产物加载失败</div>';
            }
        }

        // 右栏聊天建卡页：项目由左侧导航 + 入口显式绑定（无下拉框），标题展示绑定项目
