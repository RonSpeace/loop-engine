        // ==================== #416 页面事件流（SSE 推送）常量与状态 ====================
        // 双开关：服务端 REQ_BOARD_SSE=0（index() 注入 false）或页面 ?sse=0；关闭时 sseDegraded 恒 true，
        // 1 秒轮询常开、不创建 EventSource（功能与改动前一致）
        const SSE_SERVER_ENABLED = window.__REQ_BOARD_CFG__.sseEnabled;
        const SSE_ON = SSE_SERVER_ENABLED && new URLSearchParams(location.search).get('sse') !== '0';
        const SSE_RETRY_MS = 30000;   // 永久失败（503 超限/开关关闭）退避重试间隔；瞬时失败走浏览器 retry: 3000
        let sseConn = null;           // 当前 EventSource（任一时刻 ≤1 条）
        let sseConnUrl = null;        // 当前连接对应的 URL（视图切换对账用；EventSource 无 url 属性）
        let sseFailCount = 0;         // 瞬时失败次数（≥2 降级）
        let sseDegraded = !SSE_ON;    // true = 允许轮询（SSE 关闭/降级）；轮询门控判据固定为此
        let sseRetryTimer = null;     // 永久失败退避定时器
        let ssePendingPoke = false;   // 后台标签页收到的 poke（回前台补一次）
        window.__sseDebug = {openCount: 0, reconnects: 0, lastPokeAt: null, lastReconnectAt: null};

        // #295：三处聊天/执行过程轮询统一 2 秒（原 4 秒，聊天体感钝）；
        // 回显时效优化：活跃视图再压到 1 秒（事件本身已改成「停顿即出」，轮询是剩下的固定延迟），
        // 主列表 REFRESH_INTERVAL（15s）与页头健康角标（10s）保持不动
        const PROC_REFRESH_MS = 1000;
        const CHAT_REFRESH_MS = 1000;
        // 终态收尾：卡片离开 doing 后再多刷几轮，抓最后一批事件（末条消息 / 「运行结束」），
        // 否则终态事件要等手动刷新才出现
        const PROC_SETTLE_TICKS = 3;
        let procSettleTicks = 0;
        // 执行过程视图：doing 时每 1 秒刷新；离开 doing 后按 PROC_SETTLE_TICKS 收尾几轮再停
        // #416：SSE 正常时跳过（poke 即时刷新）；仅降级/关闭时轮询（门控判据固定为 !sseDegraded）
        setInterval(() => {
            if (document.hidden) return;
            if (!sseDegraded) return;
            const item = items.find(i => i.id === selectedId);
            if (!item) return;
            if (item.status === 'doing') {
                procSettleTicks = PROC_SETTLE_TICKS;
                refreshProcessIfActive();
            } else if (procSettleTicks > 0) {
                procSettleTicks -= 1;
                refreshProcessIfActive();
            }
        }, PROC_REFRESH_MS);
        // 建卡对话（#107）：会话运行中每 1 秒拉取状态 + 增量事件（#416：SSE 正常时跳过）
        setInterval(() => {
            if (document.hidden) return;
            if (!sseDegraded) return;
            if (addMode && chatState && chatState.status === 'running') refreshChat(false);
        }, CHAT_REFRESH_MS);

        // 会话信息：按角色固定顺序取该需求有记录的会话；旧格式纯需求 id 键视为 dev（需求开发）兜底。
        // 左侧只保留精简行（角色 + 可点击会话 id），模型/分支/上下文用量等完整信息在右栏详情里。
        // #404：角色→中文名与展示顺序由后端注入（roleMeta，来源 /api/roles →
        // acp_shared.ROLE_CAPABILITIES 的 display/order，唯一手写事实源），前端不再手写映射表；
        // 新增角色需同步的地方只剩 acp_shared.ROLE_CAPABILITIES 一处（一致性测试
        // tests/test_role_manifest.py 锁定注入键集/字段/顺序，前端零改动）。
        // 未知角色（含 /api/roles 请求失败降级）显示原文（防御路径保留）。
        function roleLabel(role) {
            const m = roleMeta.find(z => z.role === role);
            return (m && m.display) || role;
        }
        // #419 流程 chip：只读卡上 flow_name 快照（缺失 → /api/meta 注入映射按 id → 原文 id；
        // 未入流程的历史卡用 mode 历史值兜底显示）。流程改名/删除不影响存量卡显示。
        // #428 颜色只编码流程档位（按卡上 i.flow 取色，不读 i.mode）；无流程/已退役/未配色 = 中性灰。
        function flowChipHtml(i) {
            const label = i.flow_name || FLOW_LABEL[i.flow] || i.flow
                || (i.mode ? (MODE_LABEL[i.mode] || i.mode) : '');
            if (!label) return '';
            const cls = 'chip-flow-' + (FLOW_COLOR[i.flow] || FLOW_COLOR_FALLBACK);
            return ` · <span class="meta-chip ${cls}">${escapeHtml(label)}</span>`;
        }
        // #419：卡面「当前节点」由卡上快照推导（nodes_json + node_index；角色 display 走 roleLabel 注入表），
        // 不再渲染 i.phase（phase 为历史只读数据）。terminal 节点无角色，显示「收尾」。
        function currentNodeLabel(i) {
            if (!i.nodes_json) return '';
            let nodes;
            try { nodes = JSON.parse(i.nodes_json); } catch (e) { return ''; }
            if (!Array.isArray(nodes) || i.node_index === null || i.node_index === undefined) return '';
            const n = nodes[i.node_index];
            if (!n || typeof n !== 'object') return '';
            if (n.node_type === 'terminal') return '收尾';
            if (n.name) return n.name;
            return n.role ? roleLabel(n.role) : '';
        }
        // 卡片顶部流程进度条（纯函数）：段 = 需求分析 + 快照里的非收尾节点；分析阶段（尚无快照）只亮第一段。
        // 阻塞 = 有待确认/合并冲突，或流程已开跑后被停成暂缓（评审打满转人工、外部角色等）→ 当前段橙色。
        // 已完成卡全亮；未开跑的待开发/暂缓卡不渲染（避免满屏空轨道）。
        function cardProgressModel(i, blocked) {
            if (!i || i.status === 'dropped') return null;
            let nodes = null;
            if (i.nodes_json) { try { nodes = JSON.parse(i.nodes_json); } catch (e) { nodes = null; } }
            const steps = [{label: roleLabel('analysis') || '需求分析'}];
            if (Array.isArray(nodes)) {
                for (const n of nodes) {
                    if (!n || typeof n !== 'object' || n.node_type === 'terminal') continue;
                    steps.push({label: n.name || (n.role ? roleLabel(n.role) : '')});
                }
            }
            if (steps.length < 2 && i.status !== 'doing') return null;
            const total = steps.length;
            let cur;
            if (i.status === 'done') cur = total;
            else if (!Array.isArray(nodes) || i.node_index === null || i.node_index === undefined) cur = 0;
            else {
                let k = 0;
                for (let j = 0; j < nodes.length && j < i.node_index; j++) {
                    if (nodes[j] && nodes[j].node_type !== 'terminal') k++;
                }
                cur = Math.min(k + 1, total);
                if (nodes[i.node_index] && nodes[i.node_index].node_type === 'terminal') cur = total;
            }
            const started = i.status === 'doing' || i.status === 'done' || (i.dispatcher_count || 0) > 0;
            if (!started) return null;
            return {steps, cur, blocked: !!blocked && i.status !== 'done'};
        }
        function cardProgressHtml(i, blocked) {
            const m = cardProgressModel(i, blocked);
            if (!m) return '';
            const n = m.steps.length;
            const segs = m.steps.map((s, k) => {
                const cls = k < m.cur ? 'done' : k === m.cur ? (m.blocked ? 'cur blocked' : (i.status === 'doing' ? 'cur' : '')) : '';
                return `<span class="seg ${cls}" style="--i:${k}"></span>`;
            }).join('');
            const now = m.cur >= n ? '已完成' : m.steps[m.cur].label;
            const title = m.steps.map(s => s.label).join(' → ') + ' · 当前：' + now + (m.blocked ? '（需要你处理）' : '');
            return `<div class="card-progress" style="--n:${n}" title="${escapeAttr(title)}">${segs}</div>`;
        }
        // #431：草稿态合成行的展示角色 = 卡上快照当前节点 role；未入流程/无快照/脏 JSON/terminal → null
        // （调用方兜底 PRE_ROLE）。只读卡面数据，与后端 _first_message_allowed_role 同源同口径；
        // 但角色裁决权在服务端（前端不传 role），此处仅用于草稿视图的角色标签。
        function currentNodeRole(i) {
            if (!i || !i.nodes_json) return null;
            let nodes;
            try { nodes = JSON.parse(i.nodes_json); } catch (e) { return null; }
            if (!Array.isArray(nodes) || i.node_index === null || i.node_index === undefined) return null;
            const n = nodes[i.node_index];
            if (!n || typeof n !== 'object') return null;
            if (n.node_type === 'terminal') return null;
            return n.role || null;
        }
        // #431 中间栏过滤：只渲染已建立会话（session_id 非空）的注册行——待接管窗口（行已建、
        // session_id 未回填）不展示该行，回填后由 SSE sessions poke 自动带到。
        // 只在渲染层过滤，sessionList() 语义不变（右栏执行过程反查/旧格式兜底仍用它）。
        function visibleSessionRows(rows) {
            return (rows || []).filter(r => r && r.s && r.s.id);
        }
        // #390：中间栏会话行不再展示毒会话/中断的可继续灰块徽标（#389 曾合并为中性「可继续」标记）——
        // 用户确认展示意义不大；人工继续入口保留在右栏聊天框（会话状态行），不依赖行尾徽标
        // #11 模型元信息（纯函数）：请求值 != 实际值时显示「请求 → 实际」（降级/未落定场景），
        // 相等 / 请求值缺失（NULL）时按现状只显示实际值。
        function modelMetaText(s) {
            if (!s || !s.model) return '';
            const req = s.model_req;
            return (req && req !== s.model) ? `${req} → ${s.model}` : `${s.model}`;
        }
        // #13 兜底可见（共享纯函数，中间栏会话行 .session-model-warn 与右栏 .sess-model-alert 共用）：
        // 配置值（回落前）与回落后的实际请求值不等、或存在原因摘要 → 返回提示文案；否则 ''（存量行零误报）。
        function modelAlertText(s) {
            if (!s) return '';
            const cfg = s.model_configured || '';
            const req = s.model_requested || s.model_req || '';
            const reason = s.model_reason || '';
            const mismatch = !!cfg && !!req && cfg !== req;
            if (!mismatch && !reason) return '';
            if (!mismatch && reason) return '模型未落定（' + reason + '）';
            if (!reason) return '配置 ' + cfg + ' → 实际 ' + req;
            return '配置 ' + cfg + ' → 实际 ' + req + '（' + reason + '）';
        }
        function sessionList(i) {
            const rows = [];
            // #273 注册表优先：一个角色可有多条会话（含毒会话/待接管行），全部并入列表
            // （#390 渲染时按 agent 组——同需求同角色——合并：组内最早一条为主行，其余收起为子行）
            const seen = new Set();
            for (const r of (agentSessions || [])) {
                if (r.req_id !== i.id) continue;
                seen.add(r.role);
                rows.push({
                    role: r.role,
                    label: roleLabel(r.role),
                    s: {id: r.session_id || null, model: r.model,
                        harness: r.harness, context: r.context_pct, branch: r.branch, workdir: r.workdir,
                        // #11 模型请求值/来源（降级/未落定时展示「请求 → 实际」）
                        // #13 配置值与原因摘要（兜底可见）
                        model_req: r.model_requested, model_req_source: r.model_source,
                        model_requested: r.model_requested,
                        model_configured: r.model_configured, model_reason: r.model_reason},
                    sessionDir: r.session_dir || null,
                    rowId: r.id, poisoned: !!r.poisoned, status: r.status || 'idle'
                });
            }
            // 旧数据兜底：注册表还没有的阶段从 dispatcher_state 会话信息补（无毒会话概念）
            const hasDevKey = !!sessionInfo[`dev:${i.id}`];
            for (const m of roleMeta) {
                const role = m.role, label = m.display || role;
                if (seen.has(role)) continue;
                let s = sessionInfo[`${role}:${i.id}`];
                if (!s && role === 'dev' && !hasDevKey) s = sessionInfo[`${i.id}`];
                if (!s || !s.id) continue;
                // #11 旧数据兜底分支：state 会话信息里的请求值/来源（update_session_info 写入）
                s = Object.assign({}, s, {model_req: s.model_requested, model_req_source: s.model_source,
                    model_requested: s.model_requested, model_configured: s.model_configured,
                    model_reason: s.model_reason});
                rows.push({role, label, s, rowId: null, poisoned: false, status: 'idle'});
            }
            // 固定角色顺序（注：由 /api/roles 的 order 注入，单一源 acp_shared.ROLE_CAPABILITIES；
            // 未知角色排尾），同角色按创建顺序（id 升序）
            const roleOrder = new Map(roleMeta.map((r, idx) => [r.role, idx]));
            rows.sort((a, b) => ((roleOrder.get(a.role) ?? 99) - (roleOrder.get(b.role) ?? 99))
                || ((a.rowId || 0) - (b.rowId || 0)));
            return rows;
        }
        // 卡片底部的会话信息栏：顶部固定一行「需求助手」入口（#326，始终渲染）；其后只显示有会话记录的阶段。
        // #390 会话列表改 agent 维度：同一角色（agent）的多条会话（重试/续跑/人工继续产生）只渲染最早
        // 一条为主行（角色标签 + 会话 id，行首「会话」徽标已去掉），主行 id 尾部展开按钮控制其余会话
        // 子行的显隐；产物/分支差异仍挂主行下直接展示、不随展开收起。
        // 会话 id 可点击，点击在右栏展示完整执行过程（等同原来的"执行过程"入口）。
        // 有会话记录且该阶段主产物文件存在（artifactInfo exists=true）时，会话行下方追加
        // 「会话产物：<短路径>」（相对 artifacts 根的路径，如 req-board/req-69/analysis.md）；
        // 路径与文件名由 .sa-file 包成同一 flex 项连成一体（.sf-body 的 gap 只保留在标签与组合项之间），
        // 完整绝对路径放 title 悬停可见，仅文件名可点击（点击在右栏展示产物详情）。
        // 会话行入口高亮判定（纯函数，#281 回归防复发，由 tests/process_markdown_harness.js 断言）：
        // 右栏展示某会话时中间栏对应行高亮。注册表行（rowId != null）按 selectedAgentSession（中间栏
        // 点击入口）或 selectedSid（调度历史入口 selectSession 只设此值）匹配，session_id 全局唯一
        // 无误匹配；旧格式兜底行（rowId == null）按 selectedSid 匹配；selectedSid 非空守卫防止
        // 空会话行（sessionId=null）在需求详情视图误高亮。
        function isSessionLinkActive(selectedId, selectedSid, selectedAgentSession, reqId, rowId, sessionId) {
            if (selectedId !== reqId) return false;
            if (rowId != null && selectedAgentSession === rowId) return true;
            return selectedSid != null && selectedSid === sessionId;
        }
        // #326 需求助手入口行高亮判定（纯函数，tests/process_markdown_harness.js T17 断言）：
        // 右栏展示该需求的需求助手视图时，中间栏「需求助手」行高亮
        function isReqChatLinkActive(selectedId, selectedReqChat, reqId) {
            return selectedId === reqId && !!selectedReqChat;
        }
        // #424 组展开判定（纯函数，tests/process_markdown_harness.js T19 断言）：
        // state = 用户手动态（true=手动展开 / false=手动收起 / undefined=未表态）；
        // rows = 组内全部会话行（主行 + 子行，均带 status）；subSelected = 组内非主行会话当前被选中。
        // 优先级：手动收起压过一切（运行中、选中子行都不再强制展开）→ 手动展开恒展开 →
        // 未表态：组内任一行 running（主行/子行都算）自动展开 → 选中子行强制展开（#390 语义）→ 收起。
        function agentGroupExpanded(state, rows, subSelected) {
            if (state === false) return false;
            if (state === true) return true;
            if ((rows || []).some(r => r && r.status === 'running')) return true;
            return !!subSelected;
        }
        function sessionFooterHtml(i) {
            const artMap = artifactInfo[i.id] || [];
            // #326 会话信息栏置顶「需求助手」行：始终渲染（未聊过显示「新对话（未开始）」，对齐会话行
            // sidText 文案；session_id 来自 /api/requirements 列表附带的 req_chat_sid 字段），
            // #390 行首徽标与 agent 会话行统一去掉（与角色标签重复），不显示状态文字，不渲染会话产物行；点击切到右栏需求助手视图
            const asstActive = isReqChatLinkActive(selectedId, selectedReqChat, i.id);
            const asstText = i.req_chat_sid ? escapeHtml(i.req_chat_sid) : '新对话（未开始）';
            const asstRow = `<div class="session-block"><div class="session-row"><div class="sf-body"><span class="sr-label">需求助手：</span><span class="sf-id-link${asstActive ? ' active' : ''}" title="打开需求助手" onclick="selectReqChat(${i.id})">${asstText}</span></div></div></div>`;
            // #383：分支差异（diff.patch）是需求开发会话的产物（review 拉起时由调度器 _gen_review_diff
            // 代写生成，故 dev 完成后才存在）——不再独立成行，改为挂 dev 会话行下的产物区（复用
            // .session-artifact 样式，同 dev 行下 self-test.md 产物行），标签保留「分支差异：」专名；
            // #53 缺失可见化：条目存在但文件不存在时渲染「产物缺失（路径不存在）」（不再静默留白）；
            // 无 dev 会话行时兜底独立渲染（仅文件存在时，不新增孤立缺失行）
            const diffEntry = artMap.find(e => e.role === 'diff');
            const diffActive = selectedId === i.id && selectedArtifact && diffEntry
                && selectedArtifact.role === 'diff' && selectedArtifact.filename === diffEntry.filename;
            // sessionList 原始行先存（map 后是模板字符串数组，无法再判 role）；
            // #431：先过滤掉 session_id 为空的行（待接管窗口不展示）再分组/展开判定
            const sessRows = visibleSessionRows(sessionList(i));
            // 分支差异挂首个 dev 会话行下（dev 多会话行时只挂一份，避免 N 行 N 份重复渲染）
            const devIdx = sessRows.findIndex(r => r.role === 'dev');
            const hasDevRow = devIdx !== -1;
            // #390 agent 维度：组内只渲染最早一条会话为主行（sessRows 已按角色顺序+id 升序，组首即最早），
            // 其余会话收起为子行（主行 id 尾部展开按钮控制显隐）；产物/分支差异仍挂主行下
            const mainIdx = new Set();
            sessRows.forEach((r, idx) => { if (idx === 0 || sessRows[idx - 1].role !== r.role) mainIdx.add(idx); });
            // 组展开判定（#424 升级为顶层纯函数 agentGroupExpanded，定义见本函数之前）：用户手动态
            // （折叠优先）+ 组内任一行 running（主行/子行都算）自动展开 + 组内非主行会话当前被选中
            const rows = [];
            for (let idx = 0; idx < sessRows.length; idx++) {
                if (!mainIdx.has(idx)) continue;  // 子行随组主行一起渲染
                const {role, label, s, rowId, status} = sessRows[idx];
                // 该组除主行外的其余会话（子行）
                const subRows = sessRows.slice(idx + 1).filter(r => r.role === role);
                // 组内非主行会话当前被选中（右栏在展示它的执行过程时让对应行可见，避免「选中但行不可见」）
                const subSelected = subRows.some(r => (r.rowId != null && selectedAgentSession === r.rowId)
                    || (selectedSid != null && r.s && r.s.id && selectedSid === r.s.id));
                const expanded = agentGroupExpanded(expandedAgents[i.id + ':' + role], [sessRows[idx], ...subRows], subSelected);
                const sidText = s && s.id ? s.id : '新会话（未开始）';
                const sid = s && s.id ? escapeHtml(s.id) : '';
                // #53：不再按 exists 过滤，有会话行即取该角色条目——缺失时渲染可见提示而非静默留白
                const entry = s && s.id ? artMap.find(e => e.role === role) : null;
                // 入口高亮：右栏展示该会话执行过程时对应会话 id active（同卡多会话仅匹配当前选中的那个）；
                // 右栏展示产物详情时对应产物文件名 active（role+filename 双重匹配；entry 为空时防 null）
                const sidActive = isSessionLinkActive(selectedId, selectedSid, selectedAgentSession, i.id, rowId, s.id);
                const artifactActive = selectedId === i.id && selectedArtifact && entry
                    && selectedArtifact.role === role && selectedArtifact.filename === entry.filename;
                // #53 缺失行不可点击（无 sa-link/onclick）；title 为当前根期望路径（悬停可查缺在哪）
                const artifactLine = !entry
                    ? ''
                    : entry.exists
                    ? `<div class="session-artifact" title="${escapeHtml(entry.path)}"><span class="sf-body">会话产物：<span class="sa-file"><span class="sa-path">${escapeHtml(entry.short.slice(0, -entry.filename.length))}</span><span class="sa-link${artifactActive ? ' active' : ''}" onclick="selectArtifact(${i.id},'${role}')">${escapeHtml(entry.filename)}</span></span></span></div>`
                    : `<div class="session-artifact artifact-missing" title="${escapeHtml(entry.path)}"><span class="sf-body">会话产物：<span class="sa-missing">产物缺失（路径不存在）</span></span></div>`;
                // #13 兜底可见：配置值（回落前）与实际请求值不一致、或存在未落定原因时，主行下方渲染琥珀提示行
                // （文案单行、原因全文放 title；无异常时 alertLine = '' → 输出与改动前逐字一致）
                const alertText = modelAlertText(s);
                const alertLine = alertText
                    ? `<div class="session-row session-model-warn"><div class="sf-body"><span class="smw-label">模型回落：</span><span class="smw-text" title="${escapeAttr(alertText)}">${escapeHtml(alertText)}</span></div></div>`
                    : '';
                // #424 呼吸 class 只由 status 决定（不额外要求 session_id 非空：空会话被调度器 claim 后
                // 先于 session_id 变 running，占位文案所在行呼吸即「这条在跑」，组展开也必有呼吸元素）
                const linkCls = 'sf-id-link' + (sidActive ? ' active' : '') + (status === 'running' ? ' running' : '');
                const link = rowId != null
                    ? `<span class="${linkCls}" title="查看执行过程并继续会话" onclick="selectAgentSession(${i.id},${rowId})">${escapeHtml(sidText)}</span>`
                    : `<span class="${linkCls}" title="查看完整执行过程" onclick="selectSession(${i.id},'${sid}')">${sid}</span>`;
                // #383：分支差异产物行挂 dev 会话行下（.session-artifact 同款；点击进右栏 diff 视图）；
                // 仅首个 dev 会话行（=dev 组主行）挂载（review: dev 多会话行时避免重复渲染）；
                // #53 缺失时渲染不可点击的「产物缺失（路径不存在）」提示行
                const diffLine = (role === 'dev' && idx === devIdx && diffEntry)
                    ? (diffEntry.exists
                        ? `<div class="session-artifact" title="${escapeHtml(diffEntry.path)}"><span class="sf-body">分支差异：<span class="sa-file"><span class="sa-path">${escapeHtml(diffEntry.short.slice(0, -diffEntry.filename.length))}</span><span class="sa-link${diffActive ? ' active' : ''}" onclick="selectArtifact(${i.id},'diff')">${escapeHtml(diffEntry.filename)}</span></span></span></div>`
                        : `<div class="session-artifact artifact-missing" title="${escapeHtml(diffEntry.path)}"><span class="sf-body">分支差异：<span class="sa-missing">产物缺失（路径不存在）</span></span></div>`)
                    : '';
                // #390 展开按钮：组内多条会话时显示（仅主行渲染），点击切换子行显隐；
                // 行首「会话」徽标已去掉（与角色标签重复），subHtml 为展开时的组内其它会话行。
                // #424：onclick 传「当前生效展开态」（自动展开时内存可能无键），取反后首点即收起
                const expandBtn = subRows.length > 0
                    ? `<span class="sf-expand${expanded ? ' open' : ''}" title="${expanded ? '收起其它会话' : '展开其它会话'}" onclick="toggleAgentExpand(${i.id},'${role}',${expanded})"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></span>`
                    : '';
                const subHtml = expanded ? subRows.map(({rowId: subRowId, s: subS, status: subStatus}) => {
                    const subSid = subS && subS.id ? escapeHtml(subS.id) : '';
                    const subActive = isSessionLinkActive(selectedId, selectedSid, selectedAgentSession, i.id, subRowId, subS && subS.id);
                    const subCls = 'sf-id-link' + (subActive ? ' active' : '') + (subStatus === 'running' ? ' running' : '');
                    const subLink = subRowId != null
                        ? `<span class="${subCls}" title="查看执行过程并继续会话" onclick="selectAgentSession(${i.id},${subRowId})">${subSid || '新会话（未开始）'}</span>`
                        : `<span class="${subCls}" title="查看完整执行过程" onclick="selectSession(${i.id},'${subSid}')">${subSid || '新会话（未开始）'}</span>`;
                    return `<div class="session-row session-sub"><div class="sf-body">${subLink}</div></div>`;
                }).join('') : '';
                // #390 展开态顺序：主行 → 子行（其它会话）→ 会话产物/分支差异（产物恒在最下面）
                rows.push(`<div class="session-block"><div class="session-row"><div class="sf-body"><span class="sr-label">${label}：</span>${link}${expandBtn}</div></div>${subHtml}${alertLine}${artifactLine}${diffLine}</div>`);
            }
            // #383 兜底：diff 条目存在但 dev 会话行缺失（会话记录被清理/旧数据）时独立渲染，不丢失分支差异；
            // #53 该兜底仍只渲染存在项（无 dev 会话行不新增孤立缺失行，避免未开发卡噪声）
            const diffRow = (!hasDevRow && diffEntry && diffEntry.exists)
                ? `<div class="session-block"><div class="session-row"><div class="sf-body"><span class="sr-label">分支差异：</span><span class="sa-link${diffActive ? ' active' : ''}" title="${escapeHtml(diffEntry.path)}" onclick="selectArtifact(${i.id},'diff')">${escapeHtml(diffEntry.filename)}</span></div></div></div>`
                : '';
            // #326：会话信息栏始终渲染——顶部固定「需求助手」行，其后才是有会话记录的阶段行；
            // #383 diffRow 变量保留在拼接串中（仅无 dev 会话行兜底时非空）
            return `<div class="session-footer">${asstRow}${diffRow}${rows.join('')}</div>`;
        }

        // #400 网页确认交互：动作按钮排按 kind 固定生成（agent 无法声明）；merge 沿用现有卡片按钮
        function confirmActionButtonsHtml(conf) {
            let btns = '';
            const sub = a => `onclick="sendConfirmAction(${conf.req_id},'${a}')"`;
            if (conf.kind === 'confirm_override') {
                btns = `<button class="btn btn-sm btn-primary" ${sub('pass')}>豁免完成</button>` +
                       `<button class="btn btn-sm btn-danger" ${sub('reject')}>退回待开发</button>` +
                       `<button class="btn btn-sm" ${sub('reject_later')}>置暂缓</button>`;
            } else if (conf.kind === 'confirm' && conf.review_index != null) {
                // 评审轮次超限：网页与飞书 y/n 对齐；打回原因会注入前驱角色的重做提示词
                btns = `<button class="btn btn-sm btn-primary" ${sub('pass')}>放行进入下一节点</button>` +
                       `<button class="btn btn-sm btn-danger" onclick="sendReviewRework(${conf.req_id})">打回上一步重做</button>` +
                       `<button class="btn btn-sm" ${sub('stop_later')}>停止并转暂缓</button>`;
            } else if (conf.kind === 'confirm' && conf.over_limit) {
                btns = `<button class="btn btn-sm btn-primary" onclick="sendConfirmProceed(${conf.req_id})">同意并继续</button>` +
                       `<button class="btn btn-sm" ${sub('stop_later')}>停止追问转暂缓</button>`;
            } else if (conf.kind === 'confirm') {
                btns = `<button class="btn btn-sm btn-primary" onclick="sendConfirmProceed(${conf.req_id})">同意并继续</button>`;
            }
            if (conf.action) btns = '<span class="confirm-submitted">已提交待处理</span>';
            return btns;
        }

        // 待确认卡片的状态徽标：有未消费确认 → 橙色「待确认」，其余按状态渲染
        // （#419：route/review 为历史状态、无人工入口，其 pending 条目仅留痕，不显示「待确认」徽标）
        // （#25：终态卡（done/dropped）直接渲染状态徽标——残留 pending 不再显示「待确认」，与卡片状态一致）
        function statusBadgeHtml(i) {
            const terminal = i.status === 'done' || i.status === 'dropped';
            const conf = confirmations[i.id];
            const pendingBadge = !terminal && !!(conf && conf.kind !== 'route' && conf.kind !== 'review');
            return pendingBadge
                ? '<span class="badge route">待确认</span>'
                : `<span class="badge ${i.status}">${STATUS_LABEL[i.status] || i.status}</span>`;
        }

        // 待确认动作按钮（卡片头部 card-actions，与 merge 按钮同位置同机制）：
        // confirm_override → 豁免完成/退回待开发/置暂缓；confirm → 仅超限态「停止追问转暂缓」
        // （其余情况无按钮，直接去对应角色会话回复）；非空才渲染（含 conf.action 已提交态占位）
        // #25：终态卡不渲染确认动作按钮（残留 pending 也不再可操作，防误改终态卡）
        function pendingConfirmActionsHtml(i) {
            if (i.status === 'done' || i.status === 'dropped') return '';
            const conf = confirmations[i.id];
            if (!conf || !['confirm_override', 'confirm'].includes(conf.kind)) return '';
            const btns = confirmActionButtonsHtml(conf);
            return btns ? `<div class="card-actions">${btns}</div>` : '';
        }

        // 动作按钮：POST 确认决策（调度器 reconcile 消费，最长 15s）；请求带 X-Actor 留痕（#375）
        async function sendConfirmAction(reqId, action, reason) {
            const body = {action};
            if (reason) body.reason = reason;
            const res = await fetch(`/api/requirements/${reqId}/confirmations`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-Actor': `manual:${encodeURIComponent('网页确认-' + action)}`},
                body: JSON.stringify(body)
            });
            if (res.ok) {
                showToast('已提交确认请求，调度器将自动处理（约 15 秒内）');
                await load();
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('操作失败: ' + (err.error || res.status), true);
            }
        }

        function sendReviewRework(reqId) {
            const reason = window.prompt('填写打回原因（会注入技术方案/前驱角色的重做提示词）：', '');
            if (reason === null) return;
            return sendConfirmAction(reqId, 'rework', reason.trim() || '网页端打回重做');
        }

        function sendConfirmProceed(reqId) {
            const reason = window.prompt('填写确认依据或补充说明（会留给当前角色继续执行）：', '同意继续');
            if (reason === null) return;
            return sendConfirmAction(reqId, 'proceed', reason.trim() || '同意继续');
        }

        // 单张卡片的 HTML（需求平级独立，无会话组/成员行概念）
        function cardHtml(i) {
            const canDelete = !['doing', 'done'].includes(i.status);
            const selected = selectedId === i.id;
            // #7 P7 / #49 卡片可收缩（只留徽章 + 标题 + 元信息行）：会话列表与调度历史行不渲染；
            // 所有状态都渲染展开/收起图标（done 默认收缩、其余状态默认展开；open/later 与 .card-del 并列）；
            // 判定为纯函数，手动态按需求 id 持久化，状态刷新/轮询/SSE 重绘不重置
            const collapsed = cardCollapsed(i, cardExpandState);
            // 入口高亮（文字药丸）从右栏选中状态派生：需求详情视图 → 标题 active；
            // 调度历史回放 → 「调度历史回放」active；其余视图（会话执行过程/产物详情/需求助手）标题与回放链接均不高亮
            const titleActive = selected && !selectedSid && !selectedDispatcher && !selectedArtifact && !selectedReqChat;
            const dispActive = selected && selectedDispatcher;
            const dispRow = i.dispatcher_count > 0
                ? `<div class="dispatcher-footer"><div class="session-block"><div class="session-row"><span class="badge session">调度器</span><div class="sf-body"><span class="sf-disp-link${dispActive ? ' active' : ''}" title="回放调度器对此需求的全部操作" onclick="selectDispatcher(${i.id})">调度历史回放</span></div></div></div></div>`
                : '';
            const conf = confirmations[i.id];
            const blocked = !!(conf && conf.kind !== 'route' && conf.kind !== 'review') || !!mergeConflicts[i.id]
                || (i.status === 'later' && (i.dispatcher_count || 0) > 0);
            return `
            <div class="card ${i.status}${selected ? ' selected' : ''}${i.id === flashId ? ' flash' : ''}${collapsed ? ' collapsed' : ''}">
                ${cardProgressHtml(i, blocked)}
                ${statusBadgeHtml(i)}
                <div class="card-body">
                    <div class="card-title" onclick="selectReq(${i.id})"><span class="pill${titleActive ? ' active' : ''}">${escapeHtml(i.title)}</span></div>
                    <div class="card-meta">创建于 ${escapeHtml(formatTime(i.created_at))}${i.status === 'done' && i.completed_at ? ` · 完成于 ${escapeHtml(formatTime(i.completed_at))}` : ''} · 编号：${i.id}${flowChipHtml(i)}</div>
                    ${i.status === 'doing' && mergeConflicts[i.id] ? `<div class="card-actions"><button class="btn btn-sm" onclick="decideMerge(${i.id},'retry')">重试合并</button><button class="btn btn-sm" onclick="decideMerge(${i.id},'takeover')">人工接管</button></div>` : ''}
                    ${pendingConfirmActionsHtml(i)}
                </div>
                <span class="card-collapse${collapsed ? '' : ' open'}" role="button" aria-expanded="${collapsed ? 'false' : 'true'}" title="${collapsed ? '展开角色会话' : '收起角色会话'}" onclick="toggleCardExpand(${i.id})"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></span>
                ${canDelete ? `<span class="card-del" title="删除" onclick="delReq(${i.id})">✕</span>` : ''}
                ${collapsed ? '' : sessionFooterHtml(i)}
                ${collapsed ? '' : dispRow}
            </div>`;
        }

        // 未选中任何需求（首次打开/刷新/切换项目）或选中项已失效时，自动选中当前项目最新需求
        // （列表按创建时间倒序，第一张即最新）；项目下无需求或添加表单打开时不干预
        function ensureAutoSelect() {
            if (addMode) return;
            // #431 草稿态失效清理（只在 selectedId 切换/选中项失效时可能命中；置于早退之前）
            if (agentDraft && (agentDraft.reqId !== selectedId
                               || !visibleItems().some(i => i.id === agentDraft.reqId))) agentDraft = null;
            if (selectedId && visibleItems().some(i => i.id === selectedId)) return;
            const vis = visibleItems();
            selectedId = vis.length > 0 ? vis[0].id : null;
            selectedSid = null;
            selectedAgentSession = null;
            selectedDispatcher = false;
            selectedArtifact = null;
            selectedReqChat = false;
        }

        function persistViewState() {
            const state = {project: selectedProject, view: addMode ? 'add' : 'requirement',
                           status: selectedStatus};
            if (!addMode && selectedId) state.requirementId = selectedId;
            localStorage.setItem(VIEW_STATE_KEY, JSON.stringify(state));
        }

        function render() {
            ensureAutoSelect();
            persistViewState();
            renderList();
            renderDetailPane();
            if (typeof interactionSync === 'function') interactionSync();
            refreshProjectBar();  // #3 项目信息条（非 await：不阻塞主渲染链路）
        }

