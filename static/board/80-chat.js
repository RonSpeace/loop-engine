        // ============ 建卡对话（#107）：新增需求页改为 agent 会话框 ============
        // 一个会话绑定一个项目；chatState=当前会话，chatEvents=已拉事件，chatLastId=增量游标
        let chatState = null;
        let chatEvents = [];
        // chatLastId = 服务端事件 id 游标（独立于 chatEvents，只在响应落地时前进到已并入的最大 id）；
        // chatFetchSeq = 在途请求序号，只接受最新一次响应（迟到响应丢弃）；chatFetchAt = 最近一次增量
        // 请求发起时间（0=窗口空闲），窗口内合并 poke。与需求助手同款（重复气泡修复），见 mergeChatStreamEvents。
        let chatLastId = 0;
        let chatFetchSeq = 0;
        let chatFetchAt = 0;
        const CHAT_FETCH_COALESCE_MS = 800;
        let chatActionPhase = null;   // 新建会话分阶段反馈：'closing'（正在结束当前会话）| 'creating' | null
        let newSessionArmed = false;   // 有草稿时"新建会话"两步确认的待确认态
        let newSessionTimer = null;
        let chatHistory = [];          // 已建卡历史会话（按 id 升序：最旧在上、最新在下）
        let chatHistoryLoading = false;
        let chatHistoryHasMore = true;
        let chatHistoryProject = null;
        // #22 飞书建卡只读视图：addPaneTab=chat（聊天建卡，默认）| feishu（飞书建卡，只读）；
        // feishuChatList 按 id 升序（最旧在上、最新在下），向上滚动用 before_id 续拉更早页。
        let addPaneTab = 'chat';
        let feishuChatList = [];        // [{...session, events: [...]}]
        let feishuChatLoading = false;
        let feishuChatHasMore = true;
        let feishuChatReq = 0;
        const FEISHU_CHAT_STATUSES = 'created,closed,failed,idle,running,interrupted,suspended';
        const FEISHU_CHAT_PAGE = 20;
        // #24 看板助手会话独立表，列表由聚合历史接口读取；两表自增 id 会撞号 → 统一按 (source,id)
        const ASSISTANT_CHAT_PAGE = 20;
        function chatRowKey(source, id) { return (source === 'assistant' ? 'assistant' : 'chat') + ':' + id; }
        // #12 参考文件：本会话待发送清单（不本地持久化，刷新即丢——与「发送后清空」一致）
        let pendingChatMaterials = [];   // [{filename, path, size, over_read_limit}]
        let chatMaterialsOwner = null;   // 归属会话 id：切换/新建会话时整清单清空
        const CHAT_MATERIAL_MAX_FILES = 5;
        const CHAT_MATERIAL_MAX_BYTES = 10 * 1024 * 1024;
        const CHAT_MATERIAL_EXTS = ['.html', '.htm'];

        // 三入口（按钮/拖拽/粘贴）共用的可输入闸：与 renderChatStatus 对发送件/附件按钮的口径同源
        // ——只看 textarea 的 disabled（由 m.canInput 同步），running/creating 等态一律关，避免
        // 「按钮不可点但拖拽/粘贴仍能入队」的双口径（#12 代码审查）。
        function chatMaterialInputEnabled() {
            if (!chatState || chatState.status === 'created') return false;
            const input = document.getElementById('chatInput');
            return !(input && input.disabled);
        }

        // 会话切换即清空清单（同会话重渲染不清 chip）
        function chatMaterialReset(chatId) {
            if (chatMaterialsOwner !== chatId) {
                pendingChatMaterials = [];
                chatMaterialsOwner = chatId;
            }
            renderChatAttachBar();
        }

        // 文件名合法性（与服务端 _material_name_ok 同规则；前端拦截 → 不发请求）
        function chatMaterialNameError(name) {
            const n = String(name || '');
            if (!n.trim()) return '文件名为空';
            if (n.length > 200) return '文件名过长（>200 字符）';
            if (n.indexOf('/') >= 0 || n.indexOf('..') >= 0) return '文件名含非法路径字符';
            for (let i = 0; i < n.length; i += 1) {
                const code = n.charCodeAt(i);
                if (code < 32) return '文件名含控制字符';
                if (code === 92) return '文件名含非法路径字符';   // 反斜杠
            }
            const low = n.toLowerCase();
            if (!CHAT_MATERIAL_EXTS.some((ext) => low.slice(-ext.length) === ext)) return '仅支持 .html / .htm 文件';
            return null;
        }

        // 三入口统一收口：类型/大小/累计数量前端拦截，任一不合规 → 整体拒、不发请求（不部分接受）
        function chatMaterialAddFiles(fileList) {
            const files = Array.from(fileList || []).filter((f) => f && f.name);
            if (!files.length) return;
            if (!chatMaterialInputEnabled()) { showToast('当前状态不可添加参考文件（会话进行中或已建卡）', true); return; }
            const errors = [];
            files.forEach((f) => {
                const err = chatMaterialNameError(f.name);
                if (err) { errors.push(f.name + '：' + err); return; }
                if (f.size > CHAT_MATERIAL_MAX_BYTES) errors.push(f.name + '：超过 10MB 上限');
            });
            if (errors.length) { showToast(errors.join('；'), true); return; }
            if (pendingChatMaterials.length + files.length > CHAT_MATERIAL_MAX_FILES) {
                showToast(`单轮最多 ${CHAT_MATERIAL_MAX_FILES} 个参考文件，当前已有 ${pendingChatMaterials.length} 个`, true);
                return;
            }
            uploadChatMaterials(files);
        }

        async function uploadChatMaterials(files) {
            if (!chatState) { showToast('会话尚未就绪，请稍候', true); return; }
            const fd = new FormData();
            files.forEach((f) => fd.append('file', f));
            let res;
            try {
                res = await fetch(`/api/chat/${chatState.id}/materials`, {method: 'POST', body: fd});
            } catch (e) { showToast('上传失败: 网络异常', true); return; }
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                showToast('上传失败: ' + (err.error || res.status), true);
                return;
            }
            const data = await res.json().catch(() => ({}));
            const added = data.files || [];
            added.forEach((f) => pendingChatMaterials.push(f));
            renderChatAttachBar();
            showToast(`已添加 ${added.length} 个参考文件`);
        }

        function removeChatMaterial(name) {
            pendingChatMaterials = pendingChatMaterials.filter((m) => m.filename !== name);
            renderChatAttachBar();
        }

        // chip 渲染：**文本位用 escapeHtml、属性位用 escapeAttr**——escapeHtml 只转 & < >，
        // 文件名策略允许 `"`（werkzeug 会把 multipart 的 %22 还原成 `"`），属性位直接用 escapeHtml
        // 会被 `x" onmouseover="…` 之类文件名逃出属性（自伤型 self-XSS，看板仅本机单用户）
        function renderChatAttachBar() {
            const box = document.getElementById('chatAttachChips');
            if (!box) return;
            box.innerHTML = pendingChatMaterials.map((m) => {
                const tip = m.over_read_limit ? '超过 agent 读取上限(1MB)，本轮无法读取' : m.path;
                return `<span class="chat-attach-chip${m.over_read_limit ? ' warn' : ''}" title="${escapeAttr(tip)}">`
                     + `<span class="nm">${escapeHtml(m.filename)}</span>`
                     + `<button type="button" class="rm" data-name="${escapeAttr(m.filename)}" title="移除">×</button>`
                     + `</span>`;
            }).join('');
        }
        let chatHistoryReq = 0;
        // 输入框未发送草稿：按项目存 localStorage（切页面/刷新后回填），发送/确认建卡/新建会话时清除
        const CHAT_DRAFT_KEY = 'reqBoardChatDrafts';
        function getChatDrafts() {
            try { return JSON.parse(localStorage.getItem(CHAT_DRAFT_KEY) || '{}') || {}; }
            catch (e) { return {}; }
        }
        function getChatDraft(project) { return getChatDrafts()[project] || ''; }
        function saveChatDraft(project, text) {
            if (!project) return;
            const drafts = getChatDrafts();
            if (text) drafts[project] = text;
            else delete drafts[project];
            try { localStorage.setItem(CHAT_DRAFT_KEY, JSON.stringify(drafts)); } catch (e) {}
        }

        function addFormHtml() {
            const project = selectedProject === ALL_PROJECTS ? (autoProjects[0] || '') : selectedProject;
            // #22：标题旁「聊天建卡 / 飞书建卡」切换 chip（选中态复用现有 `.on` 约定）
            const chatChip = '<button type="button" class="add-pane-chip' + (addPaneTab === 'chat' ? ' on' : '')
                + '" id="addPaneTabChat">聊天建卡</button>';
            const feishuChip = '<button type="button" class="add-pane-chip' + (addPaneTab === 'feishu' ? ' on' : '')
                + '" id="addPaneTabFeishu">飞书建卡</button>';
            const head = `
                <div class="chat-head">
                    <div class="proc-title">聊天建卡${project ? ' · ' + escapeHtml(displayProjectName(project)) : ''}</div>
                    <div class="add-pane-tabs" role="tablist">${chatChip}${feishuChip}</div>
                </div>`;
            if (addPaneTab === 'feishu') {
                // 飞书建卡只读视图：仅展示飞书会话与「关闭会话」唯一写动作位
                return `
            <div class="detail-pane add-pane chat-pane fill">
                ${head}
                <div class="chat-history" id="feishuChatHistory">
                    <div class="chat-history-loading" id="feishuChatLoading" hidden>加载更早会话…</div>
                    <div id="feishuChatSessions"></div>
                </div>
            </div>`;
            }
            return `
            <div class="detail-pane add-pane chat-pane fill">
                ${head}
                <div class="chat-history" id="chatHistory">
                    <div id="chatHistorySessions"></div>
                    <div class="chat-messages" id="chatMessages"></div>
                    <div id="chatDraftBox"></div>
                </div>
                <div class="chat-input-row">
                    <div class="chat-status" id="chatStatus"></div>
                    <div class="chat-attach-bar" id="chatAttachBar">
                        <button type="button" class="btn btn-sm chat-attach-btn" id="chatAttachBtn" title="粘贴 / 拖拽 / 点击选择本机 html 参考文件（仅 .html/.htm，单个 ≤10MB，单轮 ≤5 个）">📎 参考文件</button>
                        <input type="file" id="chatFileInput" accept=".html,.htm" multiple hidden>
                        <span class="chat-attach-chips" id="chatAttachChips"></span>
                    </div>
                    <textarea id="chatInput" placeholder="描述你的需求，Agent 会读项目代码帮你澄清并产出建卡草稿。回车发送（Shift+Enter 换行）" maxlength="4000"></textarea>
                    <button class="btn btn-primary" id="chatSend">发送</button>
                </div>
            </div>`;
        }
        // #12 建卡对话参考文件三入口接线（独立函数：node harness 可用桩 DOM 驱动真实事件断言，
        // 不再只有「源码文本 needle」级覆盖）：
        //   点击选择 = 按钮委托 → 隐藏 file input.click()；
        //   拖拽 = 绑 .chat-input-row（恒有高度，落点覆盖整输入区），**高亮类挂在 #chatAttachBar**
        //          （与 CSS `.chat-attach-bar.drag-over` 同元素，否则高亮永不出现）；
        //   粘贴 = textarea 的 paste 事件取 clipboardData.files；
        //   chip 的 × 与 file input change 用委托（chip/整条 bar 内容随选择重渲染）。
        function bindChatMaterialInputs(input, attachBar) {
            const fileInput = document.getElementById('chatFileInput');
            const setDrag = (on) => {
                if (attachBar && attachBar.classList) attachBar.classList.toggle('drag-over', !!on);
            };
            if (attachBar) {
                attachBar.addEventListener('click', (e) => {
                    const t = e.target;
                    if (t.closest && t.closest('#chatAttachBtn')) {
                        const fi = document.getElementById('chatFileInput');
                        if (fi) fi.click();
                        return;
                    }
                    const rm = t.closest ? t.closest('.chat-attach-chip .rm') : null;
                    if (rm) { e.preventDefault(); removeChatMaterial(rm.dataset.name); }
                });
                attachBar.addEventListener('change', (e) => {
                    if (e.target && e.target.id === 'chatFileInput') {
                        chatMaterialAddFiles(e.target.files);
                        e.target.value = '';   // 允许再次选择同一文件
                    }
                });
            }
            const inputRow = input && input.closest ? input.closest('.chat-input-row') : null;
            if (inputRow) {
                ['dragenter', 'dragover'].forEach((evt) => inputRow.addEventListener(evt, (e) => {
                    if (!chatMaterialInputEnabled() || !e.dataTransfer) return;
                    e.preventDefault();
                    setDrag(true);
                }));
                inputRow.addEventListener('dragleave', () => setDrag(false));
                inputRow.addEventListener('drop', (e) => {
                    setDrag(false);
                    if (!chatMaterialInputEnabled()) return;
                    e.preventDefault();   // 阻止浏览器默认打开本地文件
                    if (e.dataTransfer && e.dataTransfer.files) chatMaterialAddFiles(e.dataTransfer.files);
                });
            }
            if (input) {
                input.addEventListener('paste', (e) => {
                    const files = e.clipboardData && e.clipboardData.files;
                    if (files && files.length) { e.preventDefault(); chatMaterialAddFiles(files); }
                });
            }
            return fileInput;   // 仅供测试观察，产品侧不消费
        }
        // 添加面板 DOM 每次重建，事件需在渲染后重新绑定；已存在会话则恢复渲染，否则初始化新会话
        function bindAddForm() {
            // #22：标题旁视图切换 chip（两个分支都绑）
            const tabChat = document.getElementById('addPaneTabChat');
            if (tabChat) tabChat.addEventListener('click', () => switchAddPaneTab('chat'));
            const tabFeishu = document.getElementById('addPaneTabFeishu');
            if (tabFeishu) tabFeishu.addEventListener('click', () => switchAddPaneTab('feishu'));
            if (addPaneTab === 'feishu') { bindFeishuChatPane(); return; }
            const input = document.getElementById('chatInput');
            if (!input) return;
            const project = selectedProject === ALL_PROJECTS ? (autoProjects[0] || '') : selectedProject;
            input.value = getChatDraft(project);   // 恢复未发送草稿
            input.addEventListener('input', () => saveChatDraft(project, input.value));
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
            });
            document.getElementById('chatSend').addEventListener('click', () => sendChat());
            bindChatMaterialInputs(input, document.getElementById('chatAttachBar'));
            renderChatAttachBar();
            // 状态卡内的动作（新建会话链接 / 取消本轮按钮）自带 onclick，无需事件委托
            // 草稿/继续按钮随轮询重渲染，用事件委托绑定在持久容器上（避免重渲染后按钮失效）
            const draftBox = document.getElementById('chatDraftBox');
            if (draftBox) {
                draftBox.addEventListener('click', (e) => {
                    const btn = e.target.closest('button');
                    if (!btn) return;
                    if (btn.id === 'chatDraftOk') confirmChatDraft();
                    else if (btn.id === 'chatDraftCancel') discardChatDraft();
                    else if (btn.id === 'chatResumeBtn') resumeChat();
                });
            }
            bindChatHistoryScroll();
            if (chatState && chatState.project === project) {
                renderChatHistory({scrollToBottom: false});
                renderChatPane();
                input.focus();
                // 重新进入聊天页时全量对账，补上离开期间已到达但尚未拉取的回复
                refreshChat(true);
            } else {
                chatState = null;
                chatEvents = [];
                chatLastId = 0;
                chatFetchAt = 0;
                initChatPanel(project).catch(() => renderChatStatus(interactionText('聊天暂不可用，可以直接建卡')));
            }
        }

        // #22「飞书建卡」只读视图：切换 chip → 重渲染 add-pane；数据源 GET /api/chat?channel=feishu
        // （含 failed，否则建会话失败的记录在视图消失）；首屏 FEISHU_CHAT_PAGE 条 + 向上滚动 before_id 续拉；
        // 消息渲染复用 chatEventsHtml；唯一写动作位 = footer 内「关闭会话」（二次确认 → POST /close）。
        function switchAddPaneTab(tab) {
            const next = (tab === 'feishu') ? 'feishu' : 'chat';
            if (next === addPaneTab) return;
            addPaneTab = next;
            renderDetailPane();
        }
        function bindFeishuChatPane() {
            const hist = document.getElementById('feishuChatHistory');
            if (hist) {
                hist.addEventListener('scroll', () => {
                    if (hist.scrollTop <= 40 && !feishuChatLoading && feishuChatHasMore) {
                        loadFeishuChatList(false);
                    }
                });
            }
            const box = document.getElementById('feishuChatSessions');
            if (box) {
                box.addEventListener('click', (e) => {
                    const btn = (e.target && e.target.closest) ? e.target.closest('button[data-feishu-close]') : null;
                    if (!btn) return;
                    closeFeishuChat(Number(btn.getAttribute('data-feishu-close')),
                                    btn.getAttribute('data-feishu-source') || 'chat');
                });
            }
            renderFeishuChatPane();
            loadFeishuChatList(true);
        }
        function feishuSessionMeta(s) {
            const parts = [];
            if (s.project) parts.push(escapeHtml(String(s.project)));
            parts.push(escapeHtml(String(s.status || '')));
            if (s.req_id) parts.push('#' + escapeHtml(String(s.req_id)));
            return parts.join(' · ') + '，会话 ' + escapeHtml(String(s.id));
        }
        function feishuSessionBoxHtml(s) {
            // #22：终态（created/failed/closed）无可关闭——不渲染「关闭会话」按钮，
            // 避免点关闭是假动作（api_chat_close 对这三个状态直接 200 且不改状态）。
            // #24：会话类型标签区分「飞书建卡」（source=chat）与「看板助手」（source=assistant）；
            // 主键用复合 (source, id)，关闭按钮带 data-feishu-source 供按来源分流。
            const source = (s && s.source === 'assistant') ? 'assistant' : 'chat';
            const terminal = s.status === 'created' || s.status === 'failed' || s.status === 'closed';
            const closeBtn = terminal ? '' :
                `<button type="button" class="btn btn-sm" data-feishu-close="${s.id}" data-feishu-source="${source}" title="关闭该${source === 'assistant' ? '看板助手' : '飞书建卡'}会话（二次确认，关闭后不可继续）">关闭会话</button>`;
            const kindLabel = source === 'assistant' ? '看板助手' : '飞书建卡';
            return `<div class="chat-session-box" data-feishu-key="${source}:${s.id}" data-feishu-source="${source}">
                <div class="chat-session-kind">${kindLabel}</div>
                <div class="chat-session-messages">${chatEventsHtml(s.events, {created: !!s.req_id})}</div>
                <div class="chat-session-footer feishu-footer"><span>${feishuSessionMeta(s)}</span>
                    ${closeBtn}
                </div>
            </div>`;
        }
        // #24 助手会话事件（独立端点）：feishu_chat_id 为双绑定归属校验参数
        async function fetchAssistantEvents(sid, feishuChatId) {
            try {
                const q = `after=0&feishu_chat_id=${encodeURIComponent(String(feishuChatId == null ? '' : feishuChatId))}`;
                const res = await fetch(`/api/assistant/sessions/${sid}/events?${q}`, {cache: 'no-store'});
                if (!res.ok) return [];
                const d = await res.json();
                return Array.isArray(d && d.events) ? d.events : [];
            } catch (e) {
                return [];
            }
        }
        function scrollFeishuChatToBottom() {
            const hist = document.getElementById('feishuChatHistory');
            if (hist) hist.scrollTop = hist.scrollHeight;
        }
        function renderFeishuChatPane({scrollToBottom = false} = {}) {
            const box = document.getElementById('feishuChatSessions');
            if (!box) return;
            const hist = document.getElementById('feishuChatHistory');
            const prevTop = hist ? hist.scrollTop : 0;
            const prevHeight = hist ? hist.scrollHeight : 0;
            if (!feishuChatList.length) {
                box.innerHTML = '<div class="chat-msg sys">（暂无飞书建卡会话）</div>';
                return;
            }
            box.innerHTML = feishuChatList.map(feishuSessionBoxHtml).join('');
            // 首屏/刷新滚到底；向上续拉更早页时保持视口位置（列表在顶部增长）
            if (hist) {
                if (scrollToBottom) hist.scrollTop = hist.scrollHeight;
                else if (feishuChatList.length) hist.scrollTop = prevTop + (hist.scrollHeight - prevHeight);
            }
        }
        // #22：chat poke 的飞书分支——已渲染会话走单条增量（只追加新事件、滚到底、不整页重绘/不清空）；
        // 未在列表（含列表为空）→ 整页补拉列表。
        function refreshFeishuChatOnPoke(chatId) {
            // SSE chat poke 只来自旧建卡会话（source 缺省按 'chat'，与 chatRowKey 归一一致）
            if (chatId != null && feishuChatList.some(h => (h.source || 'chat') === 'chat' && h.id === chatId))
                refreshFeishuChatSession(chatId);
            else loadFeishuChatList(true);
        }
        // 增量刷新单条会话（按来源分流）：source='chat' → GET /api/chat/<id> + /events?after=<maxId>；
        // source='assistant' → GET /api/assistant/sessions/<id>?feishu_chat_id=… + /events?after=<maxId>；
        // 原位替换该会话盒子后滚到底；会话已不在列表时回落整页补拉。
        async function refreshFeishuChatSession(chatId, source = 'chat') {
            const key = chatRowKey(source, chatId);
            const idx = feishuChatList.findIndex(h => chatRowKey(h.source, h.id) === key);
            if (idx < 0) { loadFeishuChatList(true); return; }
            const evs = feishuChatList[idx].events || [];
            const after = evs.length ? evs[evs.length - 1].id : 0;
            const chatRef = String(feishuChatList[idx].feishu_chat_id == null ? '' : feishuChatList[idx].feishu_chat_id);
            let st = null, fresh = null;
            try {
                if (source === 'assistant') {
                    const q = `feishu_chat_id=${encodeURIComponent(chatRef)}`;
                    const [stRes, evRes] = await Promise.all([
                        fetch(`/api/assistant/sessions/${chatId}?${q}`, {cache: 'no-store'}),
                        fetch(`/api/assistant/sessions/${chatId}/events?after=${after}&${q}`, {cache: 'no-store'}),
                    ]);
                    if (stRes.ok) st = await stRes.json();
                    if (evRes.ok) {
                        const d = await evRes.json();
                        fresh = (d && d.events) || [];
                        if (d && d.session) st = Object.assign({}, st || {}, d.session);
                    }
                } else {
                    const [stRes, evRes] = await Promise.all([
                        fetch(`/api/chat/${chatId}`, {cache: 'no-store'}),
                        fetch(`/api/chat/${chatId}/events?after=${after}`, {cache: 'no-store'}),
                    ]);
                    if (stRes.ok) st = await stRes.json();
                    if (evRes.ok) fresh = await evRes.json();
                }
            } catch (e) { return; }   // 网络异常静默：保留已渲染内容
            const cur = feishuChatList.findIndex(h => chatRowKey(h.source, h.id) === key);
            if (cur < 0) return;
            // 按事件 id 去重 + 升序（与网页链路同语义，就地合并避免整页重绘）
            const seen = new Set();
            const merged = (feishuChatList[cur].events || [])
                .concat(Array.isArray(fresh) ? fresh : [])
                .filter(e => e && e.id != null && !seen.has(e.id) && seen.add(e.id))
                .sort((a, b) => a.id - b.id);
            const next = {...feishuChatList[cur], ...(st || {}), events: merged};
            feishuChatList[cur] = next;
            const host = document.getElementById('feishuChatSessions');
            const cell = host ? host.querySelector(`[data-feishu-key="${key}"]`) : null;
            if (cell) {
                const tmp = document.createElement('div');
                tmp.innerHTML = feishuSessionBoxHtml(next);
                cell.replaceWith(tmp.firstElementChild);
            } else {
                renderFeishuChatPane({scrollToBottom: true});
            }
            scrollFeishuChatToBottom();
        }
        async function loadFeishuChatList(initial = true) {
            const box = document.getElementById('feishuChatSessions');
            if (!box) return;
            const req = ++feishuChatReq;
            feishuChatLoading = true;
            if (initial) { feishuChatList = []; feishuChatHasMore = true; }
            const loadingEl = document.getElementById('feishuChatLoading');
            if (loadingEl) loadingEl.hidden = !feishuChatHasMore;
            try {
                const known = new Set(feishuChatList.map(h => chatRowKey(h.source, h.id)));
                const chats = feishuChatList.filter(h => h.source === 'chat');
                let cursor = chats.length ? chats[0].id : null;
                let appended = false;
                while (feishuChatHasMore) {
                    let q = `channel=feishu&status=${FEISHU_CHAT_STATUSES}&limit=${FEISHU_CHAT_PAGE}`;
                    if (cursor) q += `&before_id=${cursor}`;
                    const res = await fetch(`/api/chat?${q}`, {cache: 'no-store'});
                    if (!res.ok) return;
                    const rows = await res.json();
                    if (!Array.isArray(rows) || !rows.length) { feishuChatHasMore = false; break; }
                    if (req !== feishuChatReq) return;
                    cursor = rows[rows.length - 1].id;
                    if (rows.length < FEISHU_CHAT_PAGE) feishuChatHasMore = false;
                    const fresh = rows.filter(r => !known.has(chatRowKey('chat', r.id)));
                    for (const r of fresh) {
                        known.add(chatRowKey('chat', r.id));
                        r.source = 'chat';
                        r.events = await fetchChatEvents(r.id);
                    }
                    if (req !== feishuChatReq) return;
                    if (fresh.length) {
                        feishuChatList = fresh.slice().reverse().concat(feishuChatList);
                        appended = true;
                    }
                    if (appended || !feishuChatHasMore) break;
                }
                // #24 看板助手会话（独立表，source='assistant'）：聚合接口读取后与旧建卡行合并，
                // 统一按 (source, id) 复合主键去重（两表自增 id 会撞号，禁止只按数字 id 索引）
                const ares = await fetch(`/api/assistant/history?limit=${ASSISTANT_CHAT_PAGE}`, {cache: 'no-store'});
                if (ares.ok) {
                    const arows = await ares.json();
                    const afresh = (Array.isArray(arows) ? arows : []).filter(
                        r => r && r.source === 'assistant' && !known.has(chatRowKey('assistant', r.id)));
                    for (const r of afresh) {
                        known.add(chatRowKey('assistant', r.id));
                        r.events = await fetchAssistantEvents(r.id, r.feishu_chat_id);
                    }
                    if (req !== feishuChatReq) return;
                    if (afresh.length) {
                        feishuChatList = afresh.slice().reverse().concat(feishuChatList);
                    }
                }
                renderFeishuChatPane({scrollToBottom: initial});
            } catch (e) {
                // 网络异常静默：保留已加载内容
            } finally {
                if (req === feishuChatReq) {
                    feishuChatLoading = false;
                    const el = document.getElementById('feishuChatLoading');
                    if (el) el.hidden = true;
                }
            }
        }
        async function closeFeishuChat(chatId, source = 'chat') {
            if (!chatId) return;
            // #24：关闭按来源分流——旧建卡 → /api/chat/<id>/close；看板助手 → /api/assistant/sessions/<id>/close
            // （助手端点需双绑定 feishu_chat_id，从列表行取）
            const isAssistant = source === 'assistant';
            const row = feishuChatList.find(h => chatRowKey(h.source, h.id) === chatRowKey(source, chatId));
            const kindLabel = isAssistant ? '看板助手' : '飞书建卡';
            if (!window.confirm('确认关闭' + kindLabel + '会话 #' + chatId + '？关闭后该会话不可继续。')) return;
            const url = isAssistant ? `/api/assistant/sessions/${chatId}/close` : `/api/chat/${chatId}/close`;
            const opts = isAssistant
                ? {method: 'POST', headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({feishu_chat_id: row ? row.feishu_chat_id : null})}
                : {method: 'POST'};
            try {
                const res = await fetch(url, opts);
                if (!res.ok) {
                    const d = await res.json().catch(() => ({}));
                    showToast('关闭失败：' + (d.error || res.status), true);
                    return;
                }
                showToast('已关闭会话 #' + chatId);
                await loadFeishuChatList(true);
            } catch (e) {
                showToast('关闭失败：' + ((e && e.message) || e), true);
            }
        }

        // 初始化/恢复目标项目的会话（并行会话：其它项目的活跃会话不挂起、不打断）
        async function initChatPanel(project) {
            if (!project) { renderChatStatus('暂无可用项目，请先在 projects.json 中配置'); return; }
            try {
                // 只按项目拉取：历史会话多了以后全局 20 条窗口可能漏掉本项目的可续会话
                // #22：显式 channel=web（双保险）——绝不捡起飞书会话
                const list = await (await fetch(`/api/chat?project=${encodeURIComponent(project)}&channel=web`, {cache: 'no-store'})).json();
                const mine = (Array.isArray(list) ? list : [])
                    .filter(c => c.project === project)
                    .filter(c => ['idle', 'running', 'interrupted', 'suspended'].includes(c.status))
                    .sort((a, b) => b.id - a.id)[0];
                if (mine) {
                    let resumed = false;
                    if (['interrupted', 'suspended'].includes(mine.status)) {
                        const resumeRes = await fetch(`/api/chat/${mine.id}/resume`, {method: 'POST'});
                        if (!resumeRes.ok) {
                            const err = await resumeRes.json().catch(() => ({}));
                            await loadChat(mine);
                            showToast('会话恢复失败: ' + (err.error || resumeRes.status), true);
                            return;
                        }
                        mine.status = 'running';
                        mine.activity = 'restoring';
                        resumed = true;
                    }
                    await loadChat(mine);
                    if (resumed) await waitForChatResume(mine.id);
                    return;
                }
            } catch (e) { /* 网络异常走新建 */ }
            const res = await fetch('/api/chat', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({project})
            });
            const data = await res.json();
            if (res.ok) {
                data.activity = 'creating';
                await loadChat(data);
            } else if (res.status === 409 && data.chat_id) {
                // 同项目已有活跃会话：恢复它，提示用户
                showToast('该项目已有活跃会话，已切换展示');
                await loadChat(data);
            } else {
                renderChatStatus('会话创建失败: ' + (data.error || res.status));
            }
        }

        async function loadChat(info) {
            chatState = {
                id: info.id,
                project: info.project,
                status: info.status || 'running',
                activity: info.activity || null,
            };
            chatEvents = [];
            chatLastId = 0;
            chatFetchAt = 0;
            chatAddStatusManualOpen = null;   // #430 换会话重置状态卡展开态（跟随自动展开）
            // 回到前台：清除后台标记，本轮结束后进程保留在当前页
            fetch(`/api/chat/${info.id}/detach`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({detached: false})
            }).catch(() => {});
            await loadChatHistory(info.project);
            await refreshChat(true);
        }

        async function fetchChatEvents(chatId) {
            try {
                const res = await fetch(`/api/chat/${chatId}/events?after=0`, {cache: 'no-store'});
                if (!res.ok) return [];
                return await res.json();
            } catch (e) {
                return [];
            }
        }

        // #335：close 收尾后台化后，收尾元数据（used x% / 模型 / 分支等）由后台线程落库；
        // 新建会话入历史盒时可能尚未 finalized，异步轮询补齐后原位刷新该条，保证历史框元数据最终一致
        async function refreshChatHistoryMeta(chatId) {
            const deadline = Date.now() + 40000;
            while (Date.now() < deadline) {
                const idx = chatHistory.findIndex(h => h.id === chatId);
                if (idx < 0) return;
                if (chatHistory[idx].finalized) return;
                try {
                    const res = await fetch(`/api/chat/${chatId}`, {cache: 'no-store'});
                    if (!res.ok) return;
                    const info = await res.json();
                    const i = chatHistory.findIndex(h => h.id === chatId);
                    if (i < 0) return;
                    if (info.finalized) {
                        chatHistory[i] = {...chatHistory[i], ...info};
                        renderChatHistory({scrollToBottom: false});
                        return;
                    }
                } catch (e) { /* 网络异常继续轮询 */ }
                await new Promise(resolve => setTimeout(resolve, 500));
            }
        }

        // 历史已建卡会话：默认拉最近 3 个，向上滚动时再拉更早 3 个。
        // 需求 #274：过滤「无可见消息」的会话（如只有「会话创建中/已建立」生命周期
        // 事件的早期测试会话），分页游标基于原始返回的最后一条 id 推进、整页被过滤
        // 时循环续拉更早页，避免游标卡死/分页提前终止；纯展示层改动，不删任何数据。
        async function loadChatHistory(project, {initial = true} = {}) {
            if (!project) return;
            const req = ++chatHistoryReq;
            chatHistoryLoading = true;
            chatHistoryProject = project;
            if (initial) {
                chatHistory = [];
                chatHistoryHasMore = true;
            }
            renderChatHistoryLoading(true);
            try {
                // chatHistory 在循环内追加后立即退出，故已知 id 集合只需构造一次
                const known = new Set(chatHistory.map(h => h.id));
                let cursor = null;    // 原始返回最后一条 id（更早一页的起点）
                let appended = false; // 本批是否已追加可见会话（整页为空则继续续拉）
                while (chatHistoryHasMore) {
                    let q = `project=${encodeURIComponent(project)}&channel=web&status=created,closed&limit=3`;
                    if (cursor) q += `&before_id=${cursor}`;
                    const res = await fetch(`/api/chat?${q}`, {cache: 'no-store'});
                    if (!res.ok) return;
                    const rows = await res.json();
                    if (!Array.isArray(rows) || !rows.length) { chatHistoryHasMore = false; break; }
                    if (req !== chatHistoryReq) return;
                    cursor = rows[rows.length - 1].id;
                    if (rows.length < 3) chatHistoryHasMore = false;
                    const fresh = rows.filter(r => !known.has(r.id));
                    const withEvents = [];
                    for (const r of fresh) {
                        withEvents.push({...r, events: await fetchChatEvents(r.id)});
                    }
                    if (req !== chatHistoryReq) return;
                    // 可见性判定与 chatEventsHtml 完全一致（生命周期/建卡过程 system 不算可见消息）
                    const visible = withEvents
                        .filter(s => s.events.some(e => !isChatLifecycleEvent(e) && !isChatProcessEvent(e)))
                        .sort((a, b) => a.id - b.id);
                    if (visible.length) {
                        chatHistory = initial ? visible : visible.concat(chatHistory);
                        appended = true;
                    }
                    if (appended || !chatHistoryHasMore) break;
                    // 整页被过滤且还有更早数据：用原始游标续拉下一页（有界：rows 不满 3 即停）
                }
                renderChatHistory({scrollToBottom: initial});
            } catch (e) {
                // 网络异常静默，保留已加载内容，下一轮滚动再试
            } finally {
                if (req === chatHistoryReq) {
                    chatHistoryLoading = false;
                    renderChatHistoryLoading(false);
                }
            }
        }

        function renderChatHistoryLoading(show) {
            const box = document.getElementById('chatHistorySessions');
            if (!box) return;
            if (show && chatHistoryHasMore && !document.getElementById('chatHistoryLoading')) {
                box.insertAdjacentHTML('afterbegin',
                    '<div class="chat-history-loading" id="chatHistoryLoading">加载更早会话…</div>');
            } else if (!show) {
                const el = document.getElementById('chatHistoryLoading');
                if (el) el.remove();
            }
        }

        function bindChatHistoryScroll() {
            const hist = document.getElementById('chatHistory');
            if (!hist || hist.dataset.scrollBound) return;
            hist.dataset.scrollBound = '1';
            hist.addEventListener('scroll', () => {
                if (hist.scrollTop <= 40 && !chatHistoryLoading && chatHistoryHasMore && chatHistoryProject) {
                    loadChatHistory(chatHistoryProject, {initial: false});
                }
            });
        }

        // 建卡终态收尾展示（需求 #272）：created 会话的最后一条 agent 消息若以
        // 「还有其他要讨论的吗？」等收尾提问/话题引导结束，替换或补充为「当前会话已结束。」，
        // 使历史已建卡会话展示与新建会话（confirm 提示词约束）一致。仅改展示，不改 chat_events 存库内容。
        const CHAT_CLOSING_LINE = '当前会话已结束。';
        const CHAT_CLOSING_LINE_EN = 'This session has ended.';   // 英文提示词项目的 agent 结束语
        // 匹配克制：仅命中末尾提问句——引导词（还有/还要/要不要/是否/要继续等）+ 语气词结尾，
        // 或最后一句以问号/语气词+问号结尾；「把状态改为 open」类引导句不命中（走补充逻辑）。
        const CHAT_CLOSING_ASK_RE = /(?:还有|还要|要不要|是否|要继续|想继续|想再|还有其他|有别的|还有什么|还需)[^。！!？?\n]{0,40}?[吗么吧呢][。！!]?\s*$|[^。！!？?\n]*[？?]\s*$/;
        function applyChatClosing(text) {
            const t = String(text);
            if (t.trim().endsWith(CHAT_CLOSING_LINE) || t.trim().endsWith(CHAT_CLOSING_LINE_EN)) return t;
            const m = t.match(CHAT_CLOSING_ASK_RE);
            if (m) return t.slice(0, m.index) + CHAT_CLOSING_LINE;
            return t + '\n\n' + CHAT_CLOSING_LINE;
        }

        // #30 卡片事件（kind='card'，content = 页 spec JSON）的人类可读摘要：
        // 只给「已发送需求卡清单 · 项目 X · 第 p/N 页 · 共 M 张」，**不吐 JSON**（未知 kind 默认分支会原样渲染）
        function cardEventSummary(content) {
            let p = null;
            try { p = JSON.parse(String(content == null ? '' : content)); } catch (err) { p = null; }
            if (!p || typeof p !== 'object' || p.type !== 'requirement_list') return '已发送需求卡清单';
            const parts = ['已发送需求卡清单'];
            if (p.project) parts.push('项目 ' + String(p.project));
            parts.push('第 ' + String(p.page == null ? '?' : p.page) + '/'
                + String(p.pages == null ? '?' : p.pages) + ' 页');
            if (p.total_cards != null) parts.push('共 ' + String(p.total_cards) + ' 张');
            if (p.truncated && p.remaining) parts.push('还有 ' + String(p.remaining) + ' 张未展示');
            return parts.join(' · ');
        }

        function chatEventsHtml(events, {created = false} = {}) {
            const visible = (events || []).filter(e => !isChatLifecycleEvent(e) && !isChatProcessEvent(e));
            if (!visible.length) return '<div class="chat-msg sys">（无消息）</div>';
            // ACP 流式输出可能把一条完整回复拆成多条相邻 message 事件，跨事件边界的 Markdown
            // 标记（`**` 加粗、行内代码、代码块围栏开/闭）逐条渲染无法配对会裸显错位
            // （例：```draft 开启在事件 309、闭合在事件 310，310 单独渲染时闭合围栏被误判为
            // 新的代码块开头，问题清单整体进入代码块），故先按原始顺序无分隔拼接相邻 message
            // 再统一渲染；tool/system/user 事件仍作为渲染边界（mergeMessageBlocks 只合并相邻 message）。
            const blocks = mergeMessageBlocks(visible);
            if (created && blocks.length) {
                const last = blocks[blocks.length - 1];
                if (last.kind === 'message') last.content = applyChatClosing(last.content);
            }
            return blocks.map(e => {
                if (e.kind === 'user') return `<div class="chat-msg user">${escapeHtml(e.content)}</div>`;
                if (e.kind === 'tool') return `<div class="chat-msg tool">⚙ ${escapeHtml(e.content)}</div>`;
                if (e.kind === 'system') return `<div class="chat-msg sys">${escapeHtml(e.content)}</div>`;
                // #30 需求卡清单卡片事件：content 是页 spec JSON，前端只显示人类可读摘要（不吐 JSON）
                if (e.kind === 'card') return `<div class="chat-msg sys">${escapeHtml(cardEventSummary(e.content))}</div>`;
                // #22：显式认 agent（dispatcher 回放的机器人追问/说明，如「[飞书] 机器人：…」）
                // 与 message 同款走机器人气泡（左对齐）；user 仍是用户气泡（含 `[飞书] 用户：` 前缀）。
                if (e.kind === 'message' || e.kind === 'agent') return `<div class="chat-msg agent">${renderArtifactMd(e.content)}</div>`;
                return `<div class="chat-msg agent">${renderArtifactMd(e.content)}</div>`;
            }).join('');
        }

        function buildChatSessionFooter(s) {
            const parts = [];
            if (s.req_id) parts.push(`编号：${escapeHtml(String(s.req_id))}`);
            if (s.session_id) {
                const sid = String(s.session_id);
                parts.push(sid.startsWith('session_') ? escapeHtml(sid) : `session_${escapeHtml(sid)}`);
            }
            if (s.harness) parts.push(escapeHtml(s.harness));
            if (s.model) parts.push(escapeHtml(s.model));
            if (s.context_pct) parts.push(`used ${escapeHtml(s.context_pct)}`);
            // #285：第一行会话配置链（编号/session_id/harness/model/used，不含分支）；
            // 第二行「执行目录 · 分支」小号次级色弱化（workdir 存量 NULL 时判空降级）
            const line1 = `${s.req_id ? '已建卡，' : ''}${parts.join(' · ')}，会话结束。`;
            const meta = [];
            if (s.workdir) meta.push(`执行目录 ${escapeHtml(s.workdir)}`);
            if (s.branch) meta.push(`分支 ${escapeHtml(s.branch)}`);
            if (!meta.length) return line1;
            return line1 + '\n<div class="chat-session-dir">' + meta.join(' · ') + '</div>';
        }

        function chatSessionBoxHtml(s) {
            return `<div class="chat-session-box" data-chat-id="${s.id}">
                <div class="chat-session-messages">${chatEventsHtml(s.events, {created: !!s.req_id})}</div>
                <div class="chat-session-footer">${buildChatSessionFooter(s)}</div>
            </div>`;
        }

        function renderChatHistory({scrollToBottom = false} = {}) {
            const box = document.getElementById('chatHistorySessions');
            if (!box) return;
            const hist = document.getElementById('chatHistory');
            const prevTop = hist ? hist.scrollTop : 0;
            const prevHeight = hist ? hist.scrollHeight : 0;
            box.innerHTML = chatHistory.map(chatSessionBoxHtml).join('');
            if (hist) {
                if (scrollToBottom) hist.scrollTop = hist.scrollHeight;
                else if (chatHistory.length) hist.scrollTop = prevTop + (hist.scrollHeight - prevHeight);
            }
        }

        // session/resume 通常约 1 秒完成；恢复期间短时快轮询，避免等待常规 4 秒刷新。
        async function waitForChatResume(chatId, timeoutMs = 8000) {
            const deadline = Date.now() + timeoutMs;
            while (Date.now() < deadline && chatState &&
                   chatState.id === chatId && chatState.status === 'running') {
                await new Promise(resolve => setTimeout(resolve, 250));
                if (!chatState || chatState.id !== chatId) return;
                try {
                    const res = await fetch(`/api/chat/${chatId}`, {cache: 'no-store'});
                    if (!res.ok) continue;
                    const st = await res.json();
                    if (!chatState || chatState.id !== chatId) return;
                    chatState = {
                        ...chatState,
                        ...st,
                        activity: st.status === 'running'
                            ? nextChatActivity(chatState.activity, chatEvents)
                            : null,
                    };
                    renderChatDraft();
                    renderChatStatus();
                    renderChatSend();
                    if (st.status !== 'running') {
                        await refreshChat(true);
                        return;
                    }
                } catch (e) { /* 短时网络异常继续轮询，超时后回退常规刷新 */ }
            }
        }

        // 拉取会话状态 + 增量事件并渲染（full=true 全量重渲染）
        async function refreshChat(full) {
            if (!chatState || !document.getElementById('chatMessages')) return;
            // #294 选区守卫：聊天流内选中文字/拖动选择期间跳过本轮重建（状态与事件延迟至下一轮轮询补上）
            const chatHist = document.getElementById('chatHistory');
            if (selectionGuardActive(chatHist, window.getSelection(), selectingPanels.has(chatHist))) return;
            const chatId = chatState.id;
            // 互斥/合并：增量请求刚发起（合并窗口内）时不再发起新的增量——poke 是「每写一条事件一条」，
            // 与轮询/发送后的主动刷新并发时会拿同一游标重复拉取同一区间。窗口按发起时间计算、到期自动
            // 放行，不依赖响应返回（异常时不会卡死刷新链；正确性由游标推进与去重兜底）。
            if (!full && chatFetchAt && Date.now() - chatFetchAt < CHAT_FETCH_COALESCE_MS) return;
            const seq = ++chatFetchSeq;
            if (!full) chatFetchAt = Date.now();
            try {
                const [stRes, evRes] = await Promise.all([
                    fetch(`/api/chat/${chatState.id}`, {cache: 'no-store'}),
                    fetch(`/api/chat/${chatState.id}/events?after=${full ? 0 : chatLastId}`, {cache: 'no-store'}),
                ]);
                // #332：接口错误按 HTTP 状态判断——200 会话行的 error 列是中断原因（取消/超时），
                // 不是接口错误，必须正常更新状态，否则按钮永远卡「取消」、chatLastId 不更新
                if (!stRes.ok) {
                    const err = await stRes.json().catch(() => ({}));
                    renderChatStatus(err.error || `请求失败（${stRes.status}）`);
                    return;
                }
                const st = await stRes.json();
                const evs = await evRes.json();
                // 迟到响应（已被更新的请求取代，典型：poke 触发的增量 vs 发送后的主动全量）直接丢弃，
                // 否则旧响应会把已并入的事件再拼一次（重复气泡的根因）；会话已切走同样丢弃
                if (seq !== chatFetchSeq) return;
                if (!chatState || chatState.id !== chatId) return;
                const prevStatus = chatState.status;
                const nextEvents = mergeChatStreamEvents(chatEvents, evs, full);
                chatState = {
                    ...chatState,
                    ...st,
                    activity: st.status === 'running'
                        ? nextChatActivity(chatState.activity, nextEvents)
                        : null,
                };
                // 收尾竞态：快速回复时状态已结束、但增量事件可能还没包含最后一条回复；
                // 结束瞬间再全量对账一次，避免回复被"吞"（需刷新页面才看到）
                if (prevStatus === 'running' && st.status !== 'running') {
                    setTimeout(() => {
                        if (chatState && chatState.id === chatId) refreshChat(true);
                    }, 600);
                }
                // 游标推进到已并入的最大事件 id（合并结果已按 id 升序）
                if (nextEvents.length) chatLastId = nextEvents[nextEvents.length - 1].id;
                chatEvents = nextEvents;
                renderChatMessages();
                renderChatDraft();
                renderChatStatus();
                renderChatSend();
                // 建卡成功：刷新列表并高亮新卡片
                if (st.status === 'created' && st.req_id && prevStatus !== 'created') {
                    await afterChatCreated(st);
                }
            } catch (e) { /* 网络异常静默，下一轮再试 */ } finally {
                // 只清自己这一次的窗口标记（更晚的请求已接手时保持其标记）
                if (!full && seq === chatFetchSeq) chatFetchAt = 0;
            }
        }

        // 建卡成功：等收尾元数据落库，把会话收进历史框，并自动在底部开启新会话
        async function afterChatCreated(st) {
            await load();
            flashCard(st.req_id);
            const deadline = Date.now() + 40000;
            let info = st;
            while (Date.now() < deadline && !info.finalized) {
                try {
                    const res = await fetch(`/api/chat/${st.id}`, {cache: 'no-store'});
                    if (res.ok) info = await res.json();
                } catch (e) { /* 继续轮询 */ }
                if (!info.finalized) await new Promise(resolve => setTimeout(resolve, 500));
            }
            if (!chatHistory.some(h => h.id === st.id)) {
                const events = await fetchChatEvents(st.id);
                chatHistory.push({...info, events});
                chatHistory.sort((a, b) => a.id - b.id);
                renderChatHistory({scrollToBottom: false});
            }
            try {
                const res = await fetch('/api/chat', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({project: st.project})
                });
                const data = await res.json();
                if (res.ok) {
                    data.activity = 'creating';
                    await loadChat(data);
                } else {
                    showToast('建卡成功，但新会话创建失败: ' + (data.error || res.status), true);
                    renderChatStatus(`已建卡 #${st.req_id}（可刷新开启新会话）`);
                }
            } catch (e) {
                showToast('建卡成功，但新会话创建失败', true);
            }
        }

        function renderChatPane() {
            chatMaterialReset(chatState && chatState.id);
            renderChatMessages();
            renderChatDraft();
            renderChatStatus();
            renderChatSend();
        }

        const CHAT_LIFECYCLE_PREFIXES = [
            '会话创建中',
            '会话已建立',
            '切换项目，会话已挂起',
            '切换项目，当前一轮已中断并挂起',
            '已切换到其它项目，当前一轮在后台继续完成',
            '已切换到其它项目，会话已挂起，切回后自动继续',
            '会话已关闭，可新建会话',
            '会话恢复中',
            '会话已恢复',
            '看板服务已重启，会话等待自动恢复',
            '会话进程已失效，正在自动恢复',
            '会话已挂起，切回项目后可继续',
        ];

        function isChatLifecycleEvent(event) {
            if (event.kind !== 'system') return false;
            const content = String(event.content || '').trim();
            return CHAT_LIFECYCLE_PREFIXES.some(prefix => content.startsWith(prefix));
        }

        // 建卡过程事件：仍写入 chat_events，仅前端过滤展示（需求 #112）
        function isChatProcessEvent(event) {
            if (event.kind !== 'system') return false;
            const content = String(event.content || '').trim();
            return content === '用户已确认草稿，等待建卡…' || content.startsWith('已建卡 #');
        }

        // activity 状态机：以最新一条生命周期 system 事件为准——
        // "会话已恢复/已建立" 表示恢复/创建完成（清空，进入"思考中"）；
        // "会话恢复中/创建中/已确认草稿" 设置对应阶段；无新事件则保持现状。
        function nextChatActivity(current, events) {
            for (let index = events.length - 1; index >= 0; index -= 1) {
                const event = events[index];
                if (event.kind !== 'system') continue;
                const content = String(event.content || '').trim();
                if (content.startsWith('会话已恢复') || content.startsWith('会话已建立')) return null;
                if (content.startsWith('会话恢复中')) return 'restoring';
                if (content.startsWith('会话创建中')) return 'creating';
                if (content.startsWith('用户已确认草稿')) return 'confirming';
                return current;
            }
            return current;
        }

        function renderChatMessages() {
            const box = document.getElementById('chatMessages');
            if (!box) return;
            const hist = document.getElementById('chatHistory');
            const nearBottom = !hist || hist.scrollHeight - hist.scrollTop - hist.clientHeight < 120;
            const visible = chatEvents.filter(e => !isChatLifecycleEvent(e) && !isChatProcessEvent(e));
            box.innerHTML = visible.length ? chatEventsHtml(chatEvents, {created: !!(chatState && chatState.status === 'created')}) : '';
            if (hist && nearBottom) hist.scrollTop = hist.scrollHeight;
        }

        function renderChatDraft() {
            const box = document.getElementById('chatDraftBox');
            if (!box) return;
            const s = chatState;
            if (!s) { box.innerHTML = ''; return; }
            if (s.status === 'created') {
                box.innerHTML = '';
                return;
            }
            if (['interrupted', 'suspended'].includes(s.status)) {
                const manualResume = s.status === 'suspended' || !s.interrupt_source || s.interrupt_source === 'user_cancel';
                if (!s.draft_title) {
                    box.innerHTML = manualResume
                        ? `<div class="detail-actions" style="justify-content:center"><button class="btn btn-sm" id="chatResumeBtn">继续会话</button></div>`
                        : '';
                } else {
                    // 草稿仍在：确认建卡会自动恢复会话；仅用户主动取消时显示手动续跑按钮
                    box.innerHTML = `
                    <div class="chat-draft">
                        <div class="chat-draft-title">草稿 · ${escapeHtml(displayProjectName(s.project))}</div>
                        <div class="chat-draft-title">${escapeHtml(s.draft_title)}</div>
                        ${s.draft_notes ? `<div class="chat-draft-notes">${escapeHtml(s.draft_notes)}</div>` : ''}
                        <div class="detail-actions">
                            <button class="btn btn-sm btn-primary" id="chatDraftOk">确认建卡</button>
                            ${manualResume ? '<button class="btn btn-sm" id="chatResumeBtn">继续会话</button>' : ''}
                            <button class="btn btn-sm" id="chatDraftCancel">取消草稿</button>
                        </div>
                    </div>`;
                }
                return;
            }
            if (s.status !== 'idle' || !s.draft_title) { box.innerHTML = ''; return; }
            box.innerHTML = `
            <div class="chat-draft">
                <div class="chat-draft-title">草稿 · ${escapeHtml(displayProjectName(s.project))}</div>
                <div class="chat-draft-title">${escapeHtml(s.draft_title)}</div>
                ${s.draft_notes ? `<div class="chat-draft-notes">${escapeHtml(s.draft_notes)}</div>` : ''}
                <div class="detail-actions">
                    <button class="btn btn-sm btn-primary" id="chatDraftOk">确认建卡</button>
                    <button class="btn btn-sm" id="chatDraftCancel">取消草稿</button>
                </div>
            </div>`;
        }

        // #430 聊天建卡状态模型（纯函数）：与调度器会话/需求助手共用同一张状态卡片。
        // 证据面 = 会话状态 + activity 阶段（创建/恢复/建卡）+ chatActionPhase（结束/新建进行中）+ 事件流时间；
        // 建卡对话没有调度器的心跳字段，故不判死、不给「重启调度器」入口。
        // textOverride = 真实接口错误/一次性提示（#332）：红色卡片、只有头部、不参与判定依据。
        const CHAT_ADD_STATUS_ICO = {creating: '🔄', restoring: '🔄', confirming: '📨', running: '⏳',
            created: '✅', idle: '💬', interrupted: '⏸', auto: '🔄', suspended: '⏸',
            failed: '⛔', closed: '⏹', created_new: '💬'};

        function chatAddActivityModel(s, phase, override, events, nowMs) {
            const evs = events || [];
            let lastMs = null;
            for (let i = evs.length - 1; i >= 0; i -= 1) {
                const t = reqChatEventMs(evs[i]);
                if (t !== null) { lastMs = t; break; }
            }
            const evSec = lastMs === null ? null : Math.max(0, (nowMs - lastMs) / 1000);
            const fmt = v => v < 60 ? (Math.round(v) + 's')
                : (Math.floor(v / 60) + 'm' + String(Math.round(v % 60)).padStart(2, '0') + 's');
            const evLine = evSec === null ? '—（尚无事件时间）' : ('最后事件 ' + fmt(evSec) + ' 前'
                + (evSec > 60 ? '（已超过 1 分钟没有新事件）' : ''));
            const mk = (kind, color, label, o) => {
                o = o || {};
                const secNum = (o.sec === null || o.sec === undefined) ? null : Math.max(0, o.sec);
                return {kind: kind, color: color, label: label, ico: o.ico || CHAT_ADD_STATUS_ICO[kind] || '💬',
                        timerText: secNum === null ? '' : ('已 ' + fmt(secNum)),
                        // ticking：运行中即使暂时算不出计时也开（事件拉到后计时才出现，靠 tick 的结构变化重建补上）
                        ticking: o.ticking === undefined ? secNum !== null : !!o.ticking,
                        canInput: !!o.canInput, action: o.action || null,
                        open: chatAddStatusManualOpen === null ? !!o.autoOpen : chatAddStatusManualOpen,
                        signals: o.signals || {event: '', model: '', verdict: ''},
                        signalSev: {event: evSec === null ? '' : (evSec > 60 ? 'warn' : 'ok'),
                                    model: o.sevModel || '', verdict: o.sevVerdict || ''}};
            };
            if (override) {
                // #332：接口错误/一次性提示的可见反馈——红色卡片、无判定依据（不可展开）
                return mk('override', 'red', String(override), {
                    ico: '⛔', canInput: true, signals: {event: '', model: '', verdict: ''}});
            }
            const project = s ? displayProjectName(s.project) : '';
            if (phase === 'closing') {
                return mk('closing', 'blue', '正在结束当前会话…', {
                    ico: '🔄', canInput: false, signals: {
                        event: evLine, model: '会话收尾进行中（新建会话流程）', verdict: '处理中'}});
            }
            if (phase === 'creating') {
                return mk('creating', 'blue', `正在创建 ${project} 会话…`, {
                    ico: '🔄', canInput: false, signals: {
                        event: evLine, model: '会话创建进行中（新建会话流程）', verdict: '处理中'}});
            }
            if (!s) {
                return mk('created_new', 'gray', '新对话：发送第一条消息开始', {
                    ico: '💬', canInput: true, signals: {
                        event: evLine, model: '无在途请求（新会话）', verdict: '未开始'}});
            }
            if (s.status === 'running') {
                if (s.activity === 'creating') {
                    return mk('creating', 'blue', `正在创建 ${project} 会话…`, {
                        ico: '🔄', canInput: false, signals: {
                            event: evLine, model: '会话创建进行中（kimi 会话拉起）', verdict: '处理中'}});
                }
                if (s.activity === 'restoring') {
                    return mk('restoring', 'blue', `正在恢复 ${project} 会话…`, {
                        ico: '🔄', canInput: false, signals: {
                            event: evLine, model: '会话恢复进行中（续上下文）', verdict: '处理中'}});
                }
                if (s.activity === 'confirming') {
                    return mk('confirming', 'blue', '正在建卡…', {
                        ico: '📨', canInput: false, signals: {
                            event: evLine, model: '已确认草稿，等待建卡完成', verdict: '处理中'}});
                }
                return mk('running', 'green', '思考中（模型推理）', {
                    ico: '⏳', sec: chatRoundSec(s, evs, nowMs), ticking: true,
                    canInput: false, action: 'cancel', sevVerdict: 'ok',
                    signals: {event: evLine, model: '本轮已发出请求，等待模型输出',
                              verdict: '活着（本轮执行中；建卡对话无心跳证据，不判卡死）'}});
            }
            if (s.status === 'created') {
                return mk('created', 'gray', `已建卡 #${s.req_id}（later 暂缓，不会被自动开发捞取）`, {
                    ico: '✅', canInput: false, autoOpen: true, signals: {
                        event: evLine, model: '无在途请求（本会话已建卡）',
                        verdict: '已结束（收尾后自动开启新会话）'}});
            }
            if (s.status === 'interrupted') {
                const manualResume = !s.interrupt_source || s.interrupt_source === 'user_cancel';
                if (!manualResume) {
                    // 超时/进程失效等自动恢复路径：琥珀卡片（过渡态，稍后自动续跑）
                    return mk('auto', 'amber', '会话已中断，正在后台自动恢复…', {
                        ico: '🔄', canInput: false, autoOpen: true, sevVerdict: 'warn', signals: {
                            event: evLine,
                            model: '中断来源 ' + (s.interrupt_source || '未知') + ' · 后台自动恢复中',
                            verdict: '恢复中（无需操作）'}});
                }
                return mk('interrupted', 'gray',
                    s.draft_title ? '会话已中断，草稿仍保留（可继续会话或确认建卡）' : '会话已中断，可继续会话（保留上下文）', {
                    ico: '⏸', canInput: true, signals: {
                        event: evLine, model: '无在途请求（本轮已中断，保留上下文）',
                        verdict: '已停止（可人工续跑）'}});
            }
            if (s.status === 'suspended') {
                return mk('suspended', 'gray', '会话已挂起，切回项目后可继续', {
                    ico: '⏸', canInput: true, signals: {
                        event: evLine, model: '无在途请求（会话挂起）', verdict: '已挂起（切回项目自动恢复）'}});
            }
            if (s.status === 'failed') {
                return mk('failed', 'red', '会话建立失败：' + (s.error || ''), {
                    ico: '⛔', canInput: true, autoOpen: true, sevVerdict: 'bad', signals: {
                        event: evLine, model: '无在途请求（会话未建立）',
                        verdict: '不可恢复（可重新进入项目新建会话）'}});
            }
            if (s.status === 'closed') {
                return mk('closed', 'gray', '会话已放弃', {
                    ico: '⏹', canInput: true, signals: {
                        event: evLine, model: '无在途请求（会话已关闭）', verdict: '已结束'}});
            }
            // idle：就绪/草稿就绪（新建会话链接由渲染层放进动作位，二段确认文案同源）
            const base = s.draft_title ? '草稿已就绪，可确认建卡或继续对话' : '就绪，可继续对话';
            return mk('idle', 'gray', base, {
                ico: '💬', canInput: true, action: 'newSession', signals: {
                    event: evLine, model: s.draft_title ? '草稿待确认（不会自动建卡）' : '无在途请求（会话就绪）',
                    verdict: s.draft_title ? '待确认草稿' : '可继续（保留上下文）'}});
        }

        // 建卡对话的轮次计时基准：本轮最后一条 user 事件（服务端不落轮次起点字段）
        function chatRoundSec(s, events, nowMs) {
            for (let i = events.length - 1; i >= 0; i -= 1) {
                if (events[i].kind !== 'user') continue;
                const t = reqChatEventMs(events[i]);
                if (t !== null) return Math.max(0, (nowMs - t) / 1000);
            }
            return null;
        }

        function renderChatStatus(textOverride) {
            const el = document.getElementById('chatStatus');
            if (!el) return;
            const m = chatAddActivityModel(chatState, chatActionPhase, textOverride, chatEvents, Date.now());
            // 主按钮恒定「发送」；不可输入时 .no-input 隐藏输入件（含阶段反馈期间），卡片动作位承载取消本轮/新建会话
            const sendInput = document.getElementById('chatInput');
            const sendBtn = document.getElementById('chatSend');
            if (sendInput) sendInput.disabled = !m.canInput;   // 隐藏之外同步 disabled：三处口径一致，兜住键盘/脚本路径
            if (sendBtn) sendBtn.disabled = !m.canInput;
            const attachBtn = document.getElementById('chatAttachBtn');
            if (attachBtn) attachBtn.disabled = !m.canInput;   // #12 附件栏同口径（隐藏由 .no-input 负责）
            setStatusNoInput(el, !m.canInput);
            const act = m.action === 'cancel'
                ? '<button class="st-btn" onclick="event.stopPropagation();cancelChat()">取消本轮</button>'
                : (m.action === 'newSession'
                    ? `<a href="#" class="chat-new-session" id="chatNewSession" onclick="event.preventDefault();event.stopPropagation();newChatSession()">`
                      + `${(newSessionArmed && chatState && chatState.draft_title) ? '确认新建会话？' : '新建会话'}</a>`
                    : '');
            applyChatStatusCard(el, m, act);
            ensureChatStatusTicker(!!m.ticking);
        }

        // 1s tick：同档位只改文本节点（计时/事件流时长），档位变化整卡重建
        function chatAddStatusTickOnce() {
            if (agentStatusPointerDown) return;
            const el = document.getElementById('chatStatus');
            if (!el) return;
            const m = chatAddActivityModel(chatState, chatActionPhase, null, chatEvents, Date.now());
            tickChatStatusCard(el, m, () => renderChatStatus());
        }

        // 发送按钮恒定「发送」：取消本轮已移入状态卡动作位；可见性与 disabled 由 renderChatStatus 按状态模型统一维护
        function renderChatSend() {
            const btn = document.getElementById('chatSend');
            if (!btn) return;
            btn.textContent = '发送';
            btn.classList.remove('btn-danger');
            btn.dataset.mode = 'send';
        }

        async function cancelChat() {
            if (!chatState || chatState.status !== 'running') return;
            let res;
            try {
                res = await fetch(`/api/chat/${chatState.id}/cancel`, {method: 'POST'});
            } catch (e) {
                showToast('取消失败: 网络异常', true);
                return;
            }
            if (res.ok) {
                // #332 乐观更新：取消立即反馈（按钮变「发送」+ 中断文案），随后 refresh 对账真实状态
                chatState = {...chatState, status: 'interrupted', interrupt_source: 'user_cancel'};
                renderChatStatus();
                renderChatSend();
                await refreshChat(true);
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('取消失败: ' + (err.error || res.status), true);
                await refreshChat(true);
            }
        }

        // 结束当前会话并开新会话：关掉的会话收进历史背景框（含底部元信息），再自动新建同项目会话
        async function newChatSession() {
            if (!chatState || chatState.status !== 'idle' || chatActionPhase) return;
            const link = document.getElementById('chatNewSession');
            if (chatState.draft_title && !newSessionArmed) {
                // 有草稿时两步确认，避免误点丢草稿
                newSessionArmed = true;
                if (link) link.textContent = '确认新建会话？';
                clearTimeout(newSessionTimer);
                newSessionTimer = setTimeout(() => {
                    newSessionArmed = false;
                    const el = document.getElementById('chatNewSession');
                    if (el) el.textContent = '新建会话';
                }, 3000);
                return;
            }
            newSessionArmed = false;
            clearTimeout(newSessionTimer);
            const chatId = chatState.id;
            const project = chatState.project;
            // 分阶段反馈（#335）：先显示「正在结束当前会话…」；close 立即返回（收尾后台执行）
            chatActionPhase = 'closing';
            renderChatStatus();
            renderChatSend();
            const res = await fetch(`/api/chat/${chatId}/close`, {method: 'POST'});
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                chatActionPhase = null;
                renderChatStatus();
                renderChatSend();
                showToast('新建会话失败: ' + (err.error || res.status), true);
                return;
            }
            saveChatDraft(project, '');
            try {
                const info = await (await fetch(`/api/chat/${chatId}`, {cache: 'no-store'})).json();
                info.events = await fetchChatEvents(chatId);
                if (!chatHistory.some(h => h.id === chatId)) {
                    chatHistory.push(info);
                    chatHistory.sort((a, b) => a.id - b.id);
                    renderChatHistory({scrollToBottom: false});
                    // #335：close 收尾后台化，历史盒条目元数据异步补齐（finalized 后原位刷新）
                    refreshChatHistoryMeta(chatId);
                }
            } catch (e) { /* 历史盒渲染失败不阻断新建会话 */ }
            chatState = null;
            chatEvents = [];
            chatLastId = 0;
            chatFetchAt = 0;
            // 进入创建阶段：initChatPanel 内部 POST /api/chat 并后台起 kimi 会话
            chatActionPhase = 'creating';
            renderChatStatus();
            renderChatSend();
            await initChatPanel(project);
            chatActionPhase = null;
            if (chatState) {
                renderChatStatus();
                renderChatSend();
                // 新会话 id 一定大于被关闭的旧会话；成立才确认本次新建成功
                if (chatState.id > chatId && ['idle', 'running'].includes(chatState.status)) {
                    showToast('新会话已就绪');
                }
            }
        }

        async function sendChat() {
            const input = document.getElementById('chatInput');
            const text = (input.value || '').trim();
            if (!text) return;
            if (!chatState) { showToast('会话尚未就绪，请稍候', true); return; }
            if (chatState.status === 'running') { showToast('正在思考中，请稍候', true); return; }
            if (chatState.status === 'created') { showToast('本会话已建卡，请开启新会话', true); return; }
            const res = await fetch(`/api/chat/${chatState.id}/prompt`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text, attachments: pendingChatMaterials.map((m) => m.filename)})
            });
            if (res.ok) {
                input.value = '';
                saveChatDraft(chatState.project, '');
                pendingChatMaterials = [];
                renderChatAttachBar();
                await refreshChat(true);
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('发送失败: ' + (err.error || res.status), true);
            }
        }

        async function confirmChatDraft() {
            const res = await fetch(`/api/chat/${chatState.id}/confirm`, {method: 'POST'});
            if (res.ok) {
                chatState.activity = 'confirming';
                saveChatDraft(chatState.project, '');
                renderChatStatus();
                await refreshChat(true);
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('确认失败: ' + (err.error || res.status), true);
            }
        }

        async function discardChatDraft() {
            const res = await fetch(`/api/chat/${chatState.id}/discard`, {method: 'POST'});
            if (res.ok) {
                chatState.draft_title = null;
                chatState.draft_notes = null;
                renderChatDraft();
                renderChatStatus();
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('操作失败: ' + (err.error || res.status), true);
            }
        }

        async function resumeChat() {
            const res = await fetch(`/api/chat/${chatState.id}/resume`, {method: 'POST'});
            if (res.ok) {
                const chatId = chatState.id;
                chatState.activity = 'restoring';
                await refreshChat(true);
                await waitForChatResume(chatId);
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('恢复失败: ' + (err.error || res.status), true);
            }
        }

        // 新建需求短暂高亮（选中态 + 动画），动画结束后移除 flash 类
        function flashCard(id) {
            flashId = id;
            render();
            setTimeout(() => {
                if (flashId === id) { flashId = null; renderList(); }
            }, 2200);
        }

        function openEdit(id) {
            const item = items.find(i => i.id === id);
            if (!item) return;
            editingId = id;
            editDirty = false;
            setSaveState('');
            document.getElementById('editTitle').value = item.title;
            document.getElementById('editProject').value = item.project;
            setEditStatus(item.status);
            document.getElementById('editNotes').value = item.notes || '';
            document.getElementById('editModal').classList.add('show');
        }

        async function closeEdit() {
            clearTimeout(saveTimer);
            if (editDirty) await autoSave(true);
            editingId = null;
            document.getElementById('editModal').classList.remove('show');
        }

        function setSaveState(text, isError) {
            const el = document.getElementById('saveState');
            el.textContent = text;
            el.className = 'save-state' + (isError ? ' error' : '');
        }

        let saveTimer = null;
        // 自动保存：输入停顿 0.8s 后触发；状态切换/关闭弹窗立即触发
        function autoSave(immediate) {
            clearTimeout(saveTimer);
            if (immediate) { doSave(); return; }
            setSaveState('编辑中…');
            saveTimer = setTimeout(doSave, 800);
        }

        async function doSave() {
            if (!editingId || !editDirty) return;
            const title = document.getElementById('editTitle').value.trim();
            const project = document.getElementById('editProject').value.trim();
            if (!title) { setSaveState('标题不能为空，未保存', true); return; }
            if (!project) { setSaveState('项目名不能为空，未保存', true); return; }
            setSaveState('保存中…');
            try {
                // X-Actor 中文部分须 encodeURIComponent：fetch 头值仅允许 ISO-8859-1，中文会抛 TypeError 致保存静默失败
                const res = await fetch(`/api/requirements/${editingId}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json', 'X-Actor': `manual:${encodeURIComponent('下拉改状态')}`},
                    body: JSON.stringify({
                        title, project,
                        status: editingStatus,
                        notes: document.getElementById('editNotes').value.trim()
                    })
                });
                if (res.ok) {
                    editDirty = false;
                    setSaveState('已保存');
                    await load();
                } else {
                    const err = await res.json();
                    setSaveState('保存失败: ' + (err.error || res.status), true);
                }
            } catch (e) {
                setSaveState('保存失败: ' + e.message, true);
            }
        }

        let deleteTargetId = null;

        function delReq(id) {
            const item = items.find(i => i.id === id);
            if (!item) return;
            deleteTargetId = id;
            document.getElementById('deleteConfirmTitleText').textContent = item.title;
            document.getElementById('deleteConfirmModal').classList.add('show');
            document.getElementById('deleteConfirmCancel').focus();
        }

        function closeDeleteConfirm() {
            const modal = document.getElementById('deleteConfirmModal');
            if (modal) modal.classList.remove('show');
            deleteTargetId = null;
        }

        async function confirmDelete() {
            const id = deleteTargetId;
            closeDeleteConfirm();
            if (id == null) return;
            const res = await fetch(`/api/requirements/${id}`, {method: 'DELETE'});
            if (res.ok) {
                await load();
                showToast('已删除');
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('删除失败: ' + (err.error || res.status), true);
            }
        }

        // #386 自动合并冲突网页决策：重试合并 = POST {action:'retry'}（调度器对账后重试合并，成功→done；
        // 仍冲突→保持 doing 继续等待，按钮重置）；人工接管 = POST {action:'takeover'}（与飞书 n 语义一致：
        // 置 later，dev 分支与 worktree 保留）。action 由调度器 reconcile 消费（最长 POLL_INTERVAL 15s），
        // 写表成功仅代表决策已登记，与飞书 y/n 双通道并存同锁互斥
        async function decideMerge(id, action) {
            const item = items.find(i => i.id === id);
            if (!item) return;
            const res = await fetch(`/api/requirements/${id}/merge-conflict`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-Actor': `manual:${encodeURIComponent(action === 'retry' ? 'merge-重试合并' : 'merge-人工接管')}`},
                body: JSON.stringify({action})
            });
            if (res.ok) {
                showToast(action === 'retry' ? '已请求重试合并，调度器将自动处理（约 15 秒内）' : '已请求人工接管，调度器将置为暂缓（约 15 秒内）');
                await load();
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('操作失败: ' + (err.error || res.status), true);
            }
        }

        // 定时刷新：每 15 秒拉取最新需求，及时看到状态变化
        // 页面不可见或编辑弹窗打开时跳过，避免无效请求和打断编辑
        const REFRESH_INTERVAL = 15000;
        async function autoRefresh() {
            if (document.hidden) return;
            if (document.getElementById('editModal').classList.contains('show')) return;
            refreshProjectBar();  // #3 统计兜底刷新（独立于下方「数据无变化早退」，恒定 15s 节拍）
            try {
                const [reqRes, sessRes, artRes, agentRes, mcRes, confRes] = await Promise.all([
                    fetch('/api/requirements', {cache: 'no-store'}),
                    fetch('/api/sessions', {cache: 'no-store'}),
                    fetch('/api/artifacts', {cache: 'no-store'}),
                    fetch('/api/agent-sessions', {cache: 'no-store'}),
                    fetch('/api/merge-conflicts', {cache: 'no-store'}),
                    fetch('/api/confirmations', {cache: 'no-store'}),
                ]);
                const latest = await reqRes.json();
                const latestSessions = await sessRes.json().catch(() => ({}));
                const latestArtifacts = await artRes.json().catch(() => ({}));
                const latestAgentSessions = await agentRes.json().catch(() => ([]));
                const latestMc = {};
                (await mcRes.json().catch(() => ([]))).forEach(r => { latestMc[r.req_id] = r; });
                const latestConfirmations = {};
                (await confRes.json().catch(() => ([]))).forEach(r => { latestConfirmations[r.req_id] = r; });
                // 六个数据都无变化时不重绘，避免无谓的 DOM 更新；
                // 任一有变化就更新对应全局变量并重绘（会话记录/产物元数据/会话注册表随 requirements 一起刷新；
                // 注册表不刷新会导致新建立的会话以旧格式入口展示，详情页缺聊天框与创建时间）
                if (JSON.stringify(latest) === JSON.stringify(items) &&
                    JSON.stringify(latestSessions) === JSON.stringify(sessionInfo) &&
                    JSON.stringify(latestArtifacts) === JSON.stringify(artifactInfo) &&
                    agentSessionsKey(latestAgentSessions) === agentSessionsKey(agentSessions) &&
                    JSON.stringify(latestMc) === JSON.stringify(mergeConflicts) &&
                    JSON.stringify(latestConfirmations) === JSON.stringify(confirmations)) return;
                // #297 会话详情差异判定：覆盖全局前先快照旧需求项与旧注册表行，供 agentSessionRefreshDecision 比较
                const prevItem = items.find(i => i.id === selectedId);
                const prevRow = selectedAgentSession
                    ? (agentSessions || []).find(x => x.id === selectedAgentSession && x.req_id === selectedId)
                    : null;
                items = latest;
                sessionInfo = latestSessions;
                artifactInfo = latestArtifacts;
                agentSessions = latestAgentSessions;
                mergeConflicts = latestMc;
                confirmations = latestConfirmations;
                repaintForAgentSessions(prevItem, prevRow);
            } catch (e) { /* 网络异常时静默，下一轮再试 */ }
        }
        setInterval(autoRefresh, REFRESH_INTERVAL);

        // #416：按新旧数据差异重绘（从 autoRefresh 分支抽出，供 SSE sessions poke 与 15 秒轮询共用）。
        // prevItem/prevRow 为覆盖全局前的快照，供 agentSessionRefreshDecision 比较；调用方需先更新 items/agentSessions。
        function repaintForAgentSessions(prevItem, prevRow) {
            // 右栏正在编辑添加表单时：只重绘列表，不重建右栏（避免清掉用户输入）
            if (addMode) {
                renderList();
            } else if (selectedAgentSession && items.some(i => i.id === selectedId)) {
                // #297 会话详情视图：从「右栏保留」分支拆出单独处理——聊天框/「思考中…」/输入禁用
                // 只在视图重建时计算，原路径永不触达，需求转 done 后残留。差异判定用新旧两份数据：
                // 终态/结构性变化全量 render()（聊天框按 chatPart='' 自然消失）；仅注册表行状态变化
                // 不重建面板，激活 refreshAgentSessionStatus 只刷状态行与输入态，事件流滚动位置不重置
                const newItem = items.find(i => i.id === selectedId);
                const newRow = (agentSessions || []).find(x => x.id === selectedAgentSession && x.req_id === selectedId);
                const decision = agentSessionRefreshDecision(prevItem, newItem, prevRow, newRow);
                if (decision === 'full') {
                    render();
                } else {
                    renderList();
                    refreshProcessIfActive();
                    // busy 期间（发送消息后等待调度器 claim/跑完）状态行由 pollAgentSessionUntilIdle
                    // 2 秒轮询维护，跳过避免把「思考中…」误刷回「可继续」
                    if (decision === 'status' && !agentChatBusy) {
                        refreshAgentSessionStatus(newRow, newRow);
                    }
                }
            } else if (selectedReqChat && items.some(i => i.id === selectedId)) {
                // #326 需求助手视图：列表重绘、右栏保留（对话由自身 2 秒轮询维护，
                // 重建会清空输入草稿与滚动位置）
                renderList();
            } else if ((selectedSid || selectedDispatcher || selectedArtifact) && items.some(i => i.id === selectedId)) {
                // 会话执行过程 / 调度时间线 / 产物详情：列表重绘、右栏保留（产物内容静态，不随轮询刷新，重新点击文件名即可刷新）
                renderList();
                if (selectedSid) refreshProcessIfActive();
                else if (selectedDispatcher) refreshDispatcherEvents(selectedId);
            } else if (agentDraft && agentDraft.reqId === selectedId && !selectedSid && !selectedDispatcher
                       && !selectedArtifact && !selectedReqChat && items.some(i => i.id === selectedId)) {
                // #431 草稿态：只重绘中间栏、右栏与 textarea 原样保留（renderList 签名短路）。
                // 必须拦在最终 else 之前——注册表心跳（任何写入都发布 sessions）、全局 sessions poke
                // 与 15s autoRefresh 三条高频路径都会走到这里，重建右栏会丢焦点/光标/输入法组合态
                renderList();
            } else {
                render();
            }
        }

        // 调度器健康角标：每 10 秒查一次健康接口。角标只回答「引擎在不在跑」：
        //   调度器心跳正常 → 运行中（绿）；心跳过期 / 未上报 → 红。
        // 飞书是可选通知渠道：未配置 / 显式停用属于正常状态，只写进悬浮说明，不占角标、不变色；
        // 只有「配置了但坏了」才在角标后缀说明（网络连接失败 = 红，凭据未加载待重启 = 提示档）。
        // 上次异常退出、pid 说明等运维细节一律只进悬浮说明（title），不进角标正文。
        function shutdownText(h) {
            if (!h.last_shutdown || !h.last_shutdown.at) return h.age === null ? '' : '上次退出异常终止（无正常关闭标记）';
            return '';
        }
        async function refreshHealth() {
            if (document.hidden) return;
            const badge = document.getElementById('healthBadge');
            const text = document.getElementById('healthText');
            const pendingTip = '配置已合并，重启看板后生效；直跑模式请手动重启（Ctrl-C 后重跑 run_local.sh）';
            try {
                const res = await fetch('/api/dispatcher-health', {cache: 'no-store'});
                const h = await res.json();
                const f = (h.feishu && typeof h.feishu === 'object') ? h.feishu : {};
                const state = String(h.state || f.state || '');
                const reason = String(h.reason || f.reason || '');
                const pidTip = (reason.indexOf('心跳 pid 非当前调度器进程') >= 0) ? '心跳 pid 非当前调度器进程' : '';
                const feishuNote = state === 'connected' ? '飞书通知：已连接'
                    : state === 'disabled' ? ('飞书通知：' + (reason.indexOf('REQ_BOARD_NO_FEISHU') >= 0 ? '显式停用' : '凭据未配置（可选）'))
                    : '';
                const tips = [];
                let cls, label;
                if (state === 'stale') {
                    cls = 'health bad';
                    label = '心跳过期';
                    if (reason) tips.push(reason);
                } else if (!h.ok) {
                    cls = 'health bad';
                    label = '调度器未上报';
                    tips.push('调度器没有运行或尚未上报心跳；开发任务不会被领取');
                } else {
                    if (h.board_restart_pending) {
                        badge.className = 'health warn';
                        badge.title = pendingTip;
                        text.textContent = '看板有新改动待重启 · 直跑模式请 Ctrl-C 后重跑 run_local.sh';
                        return;
                    }
                    cls = 'health ok';
                    label = '运行中';
                    if (state === 'failed') {
                        cls = 'health bad';
                        label = '运行中 · 飞书网络连接失败';
                        if (reason) tips.push(reason);
                    } else if (state === 'not_loaded') {
                        cls = 'health warn';
                        label = '运行中 · 飞书凭据未加载';
                        tips.push('重启调度器后生效');
                    }
                }
                if (feishuNote) tips.push(feishuNote);
                if (pidTip) tips.push(pidTip);
                const sd = shutdownText(h);
                if (sd) tips.push(sd);
                badge.className = cls;
                badge.title = tips.length ? label + ' · ' + tips.join(' · ') : label;
                text.textContent = label;
            } catch (e) { /* 网络异常时保留现状，下一轮再试 */ }
        }
        refreshHealth();
        setInterval(refreshHealth, 10000);
        // 后台标签页会跳过定时刷新，切回页面/窗口时立即补一次刷新
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) return;
            autoRefresh();
            // #416：后台标签页期间收到的 poke 在此补一次（DOM 重建延后到回前台）
            if (ssePendingPoke) sseAlignView();
            // #421/#430：回前台立即补一次计时渲染并恢复活动态 ticker（三处状态行通用，不依赖 autoRefresh）
            chatStatusTick();
            const st = document.getElementById('agentChatStatus')
                || document.getElementById('reqChatStatus') || document.getElementById('chatStatus');
            if (st && st.querySelector('.st-timer')) ensureChatStatusTicker(true);
        });
        window.addEventListener('focus', autoRefresh);
        // #16 流体背景：后台页签暂停渲染、回前台恢复（独立监听，不改动上面既有刷新逻辑）
        document.addEventListener('visibilitychange', () => fluidSetPaused(document.hidden));

        // 事件绑定
        // 左侧项目导航：点击切换项目（委托绑定，项目名动态渲染）
        document.getElementById('navList').addEventListener('click', (e) => {
            // #5 行内操作钮优先于项目切换：编辑 / 移除接入
            const editBtn = e.target.closest('.nav-edit-project');
            if (editBtn) {
                openProjectModal(editBtn.dataset.editProject);
                return;
            }
            const rmBtn = e.target.closest('.nav-remove-project');
            if (rmBtn) {
                openRemoveProjectModal(rmBtn.dataset.removeProject);
                return;
            }
            const addBtn = e.target.closest('.nav-add-project');
            if (addBtn) {
                openChatFor(addBtn.dataset.addProject);
                return;
            }
            // 状态筛选圆点优先于项目切换：点击只切状态、不改变选中项目
            const pt = e.target.closest('.nav-status-pt');
            if (pt) {
                toggleStatusFilter(pt.dataset.status);
                return;
            }
            const item = e.target.closest('.nav-item');
            if (item) selectProject(item.dataset.project);
        });
        // #5 项目管理：列表头部「+」新增入口 + 新增/编辑模态 + 移除确认模态接线
        document.getElementById('navProjectAddBtn').addEventListener('click', () => openProjectModal(null));
        // #5.1 Git 配置弹窗接线：选择文件夹 / 探测仓库 / 凭据类型联动 / 保存并测试
        // 逐个判空：静态 JS 会先于模板热更新（看板进程未重启时旧模板没有这些节点），缺节点只跳过接线，
        // 不能让整页初始化在这里中断；重启看板后新模板就位即自动生效。
        const pmPickBtn = document.getElementById('pmPickFolder');
        if (pmPickBtn) pmPickBtn.addEventListener('click', pmPickFolder);
        const pmDetectBtn = document.getElementById('pmDetect');
        if (pmDetectBtn) pmDetectBtn.addEventListener('click', pmDetectRepos);
        const pmCredKindSel = document.getElementById('pmCredKind');
        if (pmCredKindSel) pmCredKindSel.addEventListener('change', pmSyncCredRows);
        const pmCredTestBtn = document.getElementById('pmCredSaveTest');
        if (pmCredTestBtn) pmCredTestBtn.addEventListener('click', pmSaveCredentialAndTest);
        // #5.2 项目页 Git 状态条：拉取 / 推送（事件委托；push 在 runProjectGitOp 内二次确认）
        const projectGitBarEl = document.getElementById('projectGitBar');
        if (projectGitBarEl) {
            projectGitBarEl.addEventListener('click', (e) => {
                const btn = e.target.closest('[data-git-op]');
                if (btn) runProjectGitOp(btn.dataset.gitOp);
            });
        }
        document.getElementById('pmCancel').addEventListener('click', closeProjectModal);
        document.getElementById('pmSubmit').addEventListener('click', submitProjectModal);
        document.getElementById('removeProjectCancel').addEventListener('click', closeRemoveProjectModal);
        document.getElementById('removeProjectOk').addEventListener('click', confirmRemoveProject);
        document.getElementById('projectModal').addEventListener('click', (e) => {
            if (e.target.id === 'projectModal') closeProjectModal();
        });
        document.getElementById('removeProjectModal').addEventListener('click', (e) => {
            if (e.target.id === 'removeProjectModal') closeRemoveProjectModal();
        });
        // 折叠按钮：切换左侧菜单栏折叠状态
        document.getElementById('navToggle').addEventListener('click', toggleNav);
        document.getElementById('editModal').addEventListener('click', (e) => {
            if (e.target.id === 'editModal') closeEdit();
        });
        document.getElementById('deleteConfirmModal').addEventListener('click', (e) => {
            if (e.target.id === 'deleteConfirmModal') closeDeleteConfirm();
        });
        document.getElementById('deleteConfirmCancel').addEventListener('click', closeDeleteConfirm);
        document.getElementById('deleteConfirmOk').addEventListener('click', confirmDelete);
        // 标题改为双行 textarea：Enter 不换行（编辑弹窗中拦截）
        document.getElementById('editTitle').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') e.preventDefault();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                if (document.getElementById('removeProjectModal').classList.contains('show')) closeRemoveProjectModal();
                else if (document.getElementById('projectModal').classList.contains('show')) closeProjectModal();
                else if (document.getElementById('deleteConfirmModal').classList.contains('show')) closeDeleteConfirm();
                else closeEdit();
            }
        });
        // 编辑弹窗内任何输入都触发自动保存（防抖）
        ['editTitle', 'editProject', 'editNotes'].forEach(id => {
            document.getElementById(id).addEventListener('input', () => {
                editDirty = true;
                autoSave();
            });
        });

