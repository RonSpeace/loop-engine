        // ==================== #416 页面事件流（SSE 推送：单连接 + 按 topic 分发 + 降级） ====================
        // poke-only：收到通知只触发既有 GET 增量拉取/差异重绘，不新建数据通道；沿用 after 游标、
        // mergeMessageBlocks、乐观回显、dataset.sid 防串。
        function sseStreamUrl() {
            const p = new URLSearchParams();
            if (selectedId) p.set('req', String(selectedId));
            if (selectedSid) p.set('session', String(selectedSid));
            const q = p.toString();
            return '/api/stream' + (q ? '?' + q : '');
        }

        function sseClose() {
            if (!sseConn) return;
            const c = sseConn;
            sseConn = null;
            sseConnUrl = null;
            c.onerror = null;   // 摘掉回调：主动 close 不走降级分支
            c.onopen = null;
            c.onmessage = null;
            try { c.close(); } catch (e) { /* 忽略 */ }
        }

        // 连接/重连：先 close 再建，任一时刻 ≤1 条；永久失败退避期间不新建
        function sseConnect() {
            if (!SSE_ON) return;
            if (sseRetryTimer) return;
            sseClose();
            let conn;
            try { conn = new EventSource(sseStreamUrl()); } catch (e) { return; }
            sseConn = conn;
            sseConnUrl = sseStreamUrl();
            window.__sseDebug.openCount += 1;
            conn.onopen = () => {
                if (conn !== sseConn) return;
                const first = sseFailCount > 0 || sseDegraded;
                sseFailCount = 0;
                sseDegraded = false;
                window.__sseDebug.lastReconnectAt = Date.now();
                if (first) window.__sseDebug.reconnects += 1;
                sseAlignView();   // 对齐刷新：拉齐降级/断线窗口内事件
            };
            conn.onmessage = (ev) => {
                if (conn !== sseConn) return;
                let p = null;
                try { p = JSON.parse(ev.data); } catch (e) { return; }
                if (p && p.topic) handleSsePoke(p);
            };
            conn.onerror = () => {
                if (conn !== sseConn) return;
                if (conn.readyState === 2) {
                    // CLOSED = 永久失败（503 超限/开关关闭/首包非 event-stream）：浏览器不再自动重连
                    sseClose();
                    sseDegraded = true;
                    sseKickDegradedPolls();
                    if (!sseRetryTimer) {
                        sseRetryTimer = setTimeout(() => { sseRetryTimer = null; sseConnect(); }, SSE_RETRY_MS);
                    }
                } else {
                    // CONNECTING = 瞬时失败（进程重启/网络闪断）：不 close、不手动重连，
                    // 浏览器按首帧 retry: 3000 自动重连（≈3s，验收 3）；失败 2 次先降级为 1 秒轮询兜底
                    sseFailCount += 1;
                    if (sseFailCount >= 2) { sseDegraded = true; sseKickDegradedPolls(); }
                }
            };
        }

        // #416（评审必改）：降级瞬间重建 reqchat 轮询链——它与 proc/chat 的 setInterval 不同，是自续
        // setTimeout 链，SSE 正常期间已被门控停掉、不会自愈；不补这一脚则需求助手面板在降级后静默停更。
        function sseKickDegradedPolls() {
            if (selectedReqChat && selectedId) scheduleReqChatPoll(selectedId);
        }

        // #426 防线 A：注册表快照稳定键——比较前剔除每请求现取的 server_now（同一响应内同值、跨响应必不同，
        // 直接 JSON.stringify 比较会永久判「有变化」而空转 repaint）；session_dir（#422 会话头部）/
        // updated_at / last_*（#421 活性判定）一律保留；仅用于比较，不改存储快照形状（消费方零改动）。
        // 非数组输入（接口异常返回对象）等视为空表，不抛异常。
        function agentSessionsKey(rows) {
            return JSON.stringify((Array.isArray(rows) ? rows : []).map(r => {
                const {server_now, ...rest} = r;
                return rest;
            }));
        }

        // 会话注册表 poke：快照 → 拉取 → 有变化才差异重绘（refreshAgentSessions 只 fetch 不重绘）
        // 拉取失败（fetch reject）内部消化：保留旧快照与旧 DOM，下次 poke/对齐再补，不上抛
        async function sseRefreshSessions() {
            const prevItem = items.find(i => i.id === selectedId);
            const prevRow = selectedAgentSession
                ? (agentSessions || []).find(x => x.id === selectedAgentSession && x.req_id === selectedId)
                : null;
            const before = agentSessionsKey(agentSessions);
            try {
                await refreshAgentSessions();
            } catch (e) {
                return;
            }
            if (agentSessionsKey(agentSessions) === before) return;
            repaintForAgentSessions(prevItem, prevRow);
        }

        // 对齐/补刷：拉到当前可见视图的最新状态（含会话注册表重绘路径）
        // 会话拉取失败只影响注册表那一环，面板三步刷新照常执行（catch 后继续往下走，不提前 return）
        async function sseAlignView() {
            ssePendingPoke = false;
            try {
                await sseRefreshSessions();
            } catch (e) { /* 已在上层消化；此处兜底防未来改动漏掉 */ }
            if (!selectedId) return;
            if (selectedReqChat) refreshReqChat(selectedId, false);
            else if (selectedSid || selectedAgentSession) refreshProcessIfActive();
            if (addMode && chatState) refreshChat(false);
        }

        function handleSsePoke(p) {
            window.__sseDebug.lastPokeAt = Date.now();
            if (document.hidden) { ssePendingPoke = true; return; }   // 后台标签页：暂缓 DOM 重建，回前台补一次
            if (p.topic === 'init') { sseAlignView(); return; }
            if (p.topic === 'proc') {
                if (selectedId && p.req_id === selectedId) refreshProcessIfActive();
            } else if (p.topic === 'sessions') {
                sseRefreshSessions();
            } else if (p.topic === 'chat') {
                // #22：飞书 tab 已渲染的会话走增量（refreshFeishuChatOnPoke），未渲染才整页补拉
                if (addMode && addPaneTab === 'feishu') refreshFeishuChatOnPoke(p.chat_id);
                else if (addMode && chatState && p.chat_id === chatState.id) refreshChat(false);
            } else if (p.topic === 'reqchat') {
                if (selectedId && p.req_id === selectedId && selectedReqChat) refreshReqChat(p.req_id, false);
            }
        }

        // 视图切换对账：URL 参数跟随当前卡片/会话，变了才重建（连接数不随切换增长）
        setInterval(() => {
            if (!SSE_ON) return;
            if (!sseConn) { if (!sseRetryTimer) sseConnect(); return; }
            if (sseConnUrl !== sseStreamUrl()) sseConnect();
        }, 500);
        sseConnect();

        // #431 刷新恢复：上次视图是某需求且有非空草稿文本 → 预置草稿态（无文本不进草稿态）
        if (savedViewState && savedViewState.view === 'requirement'
            && Number.isInteger(Number(savedViewState.requirementId))) {
            const draftReqId = Number(savedViewState.requirementId);
            if (selectedId === draftReqId && agentDraftText(draftReqId)) agentDraft = {reqId: draftReqId};
        }
        applyNavCollapsed();  // 初始化折叠态（localStorage 记住的展开/折叠状态）
        initSkin();           // #15 初始化皮肤（默认深色；localStorage 记住的浅色偏好恢复）
        initFluidBg();        // #16 初始化流体置换背景（WebGL2 不可用/降级时保留令牌化静态渐变底）
        initLayoutResizers();  // #7 P7：初始化两处拖拽分界 + 应用存储宽度（折叠态在 applyNavCollapsed 内已清 inline）
        loadMeta().finally(() => load());  // #419：标签元数据先注入（失败也照常渲染，降级原文）
