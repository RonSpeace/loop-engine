        // ==================== #7 P7：布局宽度（拖拽分界）纯函数与应用单点 ====================
        // 宽度钳制（纯函数）：非数值（NaN/Infinity）→ 下限；容器太窄（hi < lo）取下限；否则夹在 [lo, hi]
        function clampColWidth(v, lo, hi) {
            const n = Number(v);
            if (!isFinite(n)) return lo;
            if (hi < lo) return lo;
            return Math.min(Math.max(n, lo), hi);
        }
        // 由邻居下限反推的列上限（纯函数）：容器实测宽 − 两列间距 − 邻居最小宽；小容器收敛到自身下限
        function colMaxWidth(containerW, ownMinW, neighborMinW) {
            const cw = Number(containerW);
            if (!isFinite(cw)) return ownMinW;
            return Math.max(ownMinW, Math.round(cw) - COL_GAP_W - neighborMinW);
        }
        // 宽度应用单点：读容器实测宽 → 钳制存储偏好 → 写两列 inline 宽度；折叠态清空导航 inline 宽度
        // （让 CSS 48px 规则生效，展开时回到存储宽度、不退回 172）；窗口缩放与拖动共用，不回写存储。
        function applyLayoutWidths() {
            const wsEl = document.querySelector('.workspace');
            const navEl = document.querySelector('.nav-col');
            const listEl = document.querySelector('.list-col');
            if (!wsEl || !navEl || !listEl) return;
            if (navCollapsed) {
                navEl.style.width = '';
            } else {
                navEl.style.width = clampColWidth(layoutPref.navW, NAV_MIN_W,
                    colMaxWidth(wsEl.clientWidth, NAV_MIN_W, MAIN_MIN_W)) + 'px';
            }
            const rowEl = document.querySelector('.main-row');
            const rowW = rowEl ? rowEl.clientWidth : wsEl.clientWidth;
            listEl.style.width = clampColWidth(layoutPref.listW, LIST_MIN_W,
                colMaxWidth(rowW, LIST_MIN_W, DETAIL_MIN_W)) + 'px';
        }
        // 分界拖动绑定（pointer 统一鼠标/触控）：按下 → 捕获指针 + 拖动态（禁列宽过渡动画）；
        // 移动 → 实时钳制写内存偏好并应用；松开/取消 → 收尾并把宽度偏好落 localStorage。
        function bindColResizer(resizer, kind) {
            if (!resizer) return;
            resizer.addEventListener('pointerdown', (e) => {
                if (e.button !== 0) return;   // 仅主键拖动
                e.preventDefault();
                const wsEl = document.querySelector('.workspace');
                const colEl = kind === 'nav' ? document.querySelector('.nav-col') : document.querySelector('.list-col');
                if (!wsEl || !colEl) return;
                const startX = e.clientX;
                const startW = colEl.offsetWidth;   // border-box，与 style.width 同口径
                try { resizer.setPointerCapture(e.pointerId); } catch (err) { /* 指针捕获不可用时靠冒泡事件兜底 */ }
                resizer.classList.add('dragging');
                wsEl.classList.add('col-resizing');
                const onMove = (ev) => {
                    const dx = ev.clientX - startX;
                    if (kind === 'nav') {
                        layoutPref.navW = clampColWidth(startW + dx, NAV_MIN_W,
                            colMaxWidth(wsEl.clientWidth, NAV_MIN_W, MAIN_MIN_W));
                    } else {
                        const rowEl = document.querySelector('.main-row');
                        const rowW = rowEl ? rowEl.clientWidth : wsEl.clientWidth;
                        layoutPref.listW = clampColWidth(startW + dx, LIST_MIN_W,
                            colMaxWidth(rowW, LIST_MIN_W, DETAIL_MIN_W));
                    }
                    applyLayoutWidths();
                };
                const onEnd = () => {
                    resizer.classList.remove('dragging');
                    wsEl.classList.remove('col-resizing');
                    resizer.removeEventListener('pointermove', onMove);
                    resizer.removeEventListener('pointerup', onEnd);
                    resizer.removeEventListener('pointercancel', onEnd);
                    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layoutPref)); } catch (err) { /* 写失败降级：仅本次会话 */ }
                };
                resizer.addEventListener('pointermove', onMove);
                resizer.addEventListener('pointerup', onEnd);
                resizer.addEventListener('pointercancel', onEnd);
            });
        }
        // 初始化：绑定两处分界 + 窗口缩放收敛（rAF 合并帧，只视觉收敛不回写存储，放大后回偏好宽度）
        function initLayoutResizers() {
            bindColResizer(document.getElementById('navColResizer'), 'nav');
            bindColResizer(document.getElementById('listColResizer'), 'list');
            let rafId = null;
            window.addEventListener('resize', () => {
                if (rafId !== null) return;
                rafId = requestAnimationFrame(() => { rafId = null; applyLayoutWidths(); });
            });
            applyLayoutWidths();
        }

        // 编辑弹窗的项目下拉（重建选项时保留当前选中值，避免自动保存后 load() 把弹窗里的下拉重置成第一项）
        async function renderEditProjects() {
            const res = await fetch('/api/projects');
            const projects = await res.json();
            const sel = document.getElementById('editProject');
            const cur = sel.value;
            sel.innerHTML = projects.map(p =>
                `<option value="${escapeHtml(p)}">${escapeHtml(displayProjectName(p))}</option>`).join('');
            if (cur && projects.includes(cur)) sel.value = cur;
        }

        // 中间列表：当前选中项的展示需求（「所有项目」返回全量，其余按项目过滤；
        // 状态筛选与项目选择正交叠加；不做状态分组，纯创建时间倒序）
        function visibleItems() {
            let vis = selectedProject === ALL_PROJECTS ? items : items.filter(i => i.project === selectedProject);
            if (selectedStatus) vis = vis.filter(i => i.status === selectedStatus);
            return vis;
        }

        // «所有项目»行状态圆点：单选 toggle（点一下过滤该状态、再点取消、点另一个切换）
        function toggleStatusFilter(status) {
            selectedStatus = selectedStatus === status ? null : status;
            renderNav();
            render();
        }

        // 右侧选中：selectReq 切到需求详情视图，selectSession 切到该会话的完整执行过程，
        // selectArtifact 切到会话产物详情视图，selectReqChat 切到需求助手视图（五者互斥）
        function selectReq(id) {
            clearReqChatTimer();    // 离开需求助手视图时停掉 2 秒轮询
            agentDraft = null;      // #431 退出草稿态（文本留在 localStorage，不发消息不落库）
            selectedId = id;
            addMode = false;
            selectedSid = null;
            selectedAgentSession = null;
            selectedDispatcher = false;
            selectedArtifact = null;
            selectedReqChat = false;
            render();
        }
        function selectSession(id, sid) {
            clearReqChatTimer();
            agentDraft = null;
            selectedId = id;
            addMode = false;
            selectedSid = sid;
            selectedAgentSession = null;
            selectedDispatcher = false;
            selectedArtifact = null;
            selectedReqChat = false;
            render();
        }
        // #273 人工接管：点击中间栏会话行（agent_sessions 行 id），右栏展示执行过程 + 底部聊天框
        function selectAgentSession(id, rowId) {
            clearReqChatTimer();
            agentDraft = null;
            const row = (agentSessions || []).find(x => x.id === rowId && x.req_id === id);
            selectedId = id;
            addMode = false;
            selectedDispatcher = false;
            selectedArtifact = null;
            selectedReqChat = false;
            selectedAgentSession = row ? row.id : rowId;
            selectedSid = row && row.session_id ? row.session_id : null;
            render();
        }
        // #120 遗留缺口收口：调度历史回放的 spawn 会话 id 与中间栏会话行落到同一视图。
        // 纯函数按 session_id 反查 agent_sessions 注册表行（session_id 全局唯一，req_id 双条件防串卡），
        // 命中返回注册表行 id（走 selectAgentSession：执行过程 + 状态栏 + 聊天框），未命中返回 null。
        // 单测见 tests/process_markdown_harness.js T21。
        function agentSessionRowIdForSid(rows, reqId, sid) {
            if (!sid) return null;
            const row = (rows || []).find(x => x && x.session_id === sid && x.req_id === reqId);
            return row ? row.id : null;
        }
        // #14 调度历史回放显性化会话实际使用模型：会话 id → agent_sessions.model（会话观测显示名，
        // 原样显示不裁剪「(airoute)」等后缀）。纯函数按 session_id + req_id 反查注册表行（双条件
        // 防串卡，对齐 agentSessionRowIdForSid）；查不到行或无 model 值（历史会话）统一兜底静态文案
        // ——需求原文指定的默认模型名，未来默认模型变更需同步此常量。单测见
        // tests/test_dispatcher_events_layout.py（S9）。
        const SESSION_MODEL_FALLBACK = 'DeepSeek V4 Flash';
        function agentSessionModelForSid(rows, reqId, sid) {
            if (!sid) return SESSION_MODEL_FALLBACK;
            const row = (rows || []).find(x => x && x.session_id === sid && x.req_id === reqId);
            return row && row.model ? row.model : SESSION_MODEL_FALLBACK;
        }
        // 调度历史回放入口：命中注册表行 → 与中间栏会话行同视图（状态栏判活/判死 + 续聊）；
        // 未命中（历史/旧数据无注册表行）→ 回退 selectSession 只读执行过程视图（renderDetailPane 的 selectedSid 分支）
        function openSessionFromSid(reqId, sid) {
            const rowId = agentSessionRowIdForSid(agentSessions, reqId, sid);
            if (rowId != null) { selectAgentSession(reqId, rowId); return; }
            selectSession(reqId, sid);
        }
        // #390 agent 维度会话列表：点击主行 id 尾部的展开按钮，切换该 agent 组（同需求同角色）
        // 其它会话的显隐；展开态只记内存（expandedAgents），刷新/切卡后默认收起。
        // #424：入参为「当前生效展开态」（自动展开时内存可能无键），取反一次点击即收起（修 #390 首点无效）
        function toggleAgentExpand(reqId, role, expanded) {
            const key = reqId + ':' + role;
            expandedAgents[key] = !expanded;
            render();
        }
        // #49 需求卡折叠判定（纯函数，node harness 断言）：所有状态都支持手动收起/展开；
        // 无键时按状态默认——done 默认收缩（#7 P7 原行为），其余状态（open/doing/later/dropped）默认展开。
        // 手动态 true=手动展开 / false=手动收起，压过默认。不含时间/随机输入（保护 renderList 的 listHtmlSig 短路）。
        function cardCollapsed(i, expandState) {
            if (!i) return false;
            const v = (expandState || {})[String(i.id)];
            if (v === true) return false;
            if (v === false) return true;
            return i.status === 'done';
        }
        // #7 P7 / #49 切换卡片展开/收起：新状态直接赋 cardCollapsed 的返回值（恰为切换后的折叠态——
        // 未表态的 done 卡首点为「手动展开」，未表态的非 done 卡首点为「手动收起」）；不要写成取反形式
        // （那会让首点停在收缩态、视觉零变化，即 #390「首点无效」教训）。经 render() 重算列表签名，
        // 不直接改 DOM class（防签名失配）。状态按需求 id 落 localStorage，刷新/SSE 重绘后保持。
        function toggleCardExpand(id) {
            const item = items.find(i => i.id === id);
            if (!item) return;
            cardExpandState[String(id)] = cardCollapsed(item, cardExpandState);
            try { localStorage.setItem(CARD_EXPAND_KEY, JSON.stringify(cardExpandState)); } catch (e) { /* 写失败降级：仅本次会话 */ }
            render();
        }
        function selectDispatcher(id) {
            clearReqChatTimer();
            agentDraft = null;
            selectedId = id;
            addMode = false;
            selectedSid = null;
            selectedAgentSession = null;
            selectedDispatcher = true;
            selectedArtifact = null;
            selectedReqChat = false;
            dispatcherView = 'skeleton';
            dispatcherStageOpen = {};
            dispatcherEventsCache = [];
            dispatcherEventsCacheReq = null;
            dispatcherStagesCache = [];
            render();
        }
        // 从会话信息栏点击产物文件名进入：filename/path 一律取自 /api/artifacts 元数据（不拼路径）；
        // artifactInfo 缺失（极端情况）时兜底返回，页面不报错
        function selectArtifact(id, role) {
            const entry = (artifactInfo[id] || []).find(e => e.role === role && e.exists);
            if (!entry) return;
            clearReqChatTimer();
            agentDraft = null;
            selectedId = id;
            addMode = false;
            selectedSid = null;
            selectedAgentSession = null;
            selectedDispatcher = false;
            selectedArtifact = {role, filename: entry.filename, path: entry.path, mtime: entry.mtime};
            selectedReqChat = false;
            render();
        }
        // #326 需求助手：点击中间栏会话信息栏置顶「需求助手」行，右栏切换到独立需求助手视图
        // （旧计时器由 renderDetailPane 重建时 loadReqChat 开头清理，这里不重复清）
        function selectReqChat(id) {
            agentDraft = null;
            selectedId = id;
            addMode = false;
            selectedSid = null;
            selectedAgentSession = null;
            selectedDispatcher = false;
            selectedArtifact = null;
            selectedReqChat = true;
            render();
        }
        // 增强 markdown 渲染器（产物详情视图与执行过程 message 事件共用）。
        // 安全基线：先整体 escapeHtml 再语法转换；href 白名单 http/https/mailto，其余降级纯文本；
        // 图片 url 白名单仅命中本站产物 API 形态（/api/requirements/<数字>/artifacts/<合法文件名>），其余降级纯文本。
        // 行内代码先提为占位符再回填，防止 ** / [ 干扰代码内容。
        function renderArtifactMd(src) {
            // #280：围栏代码块若整体是合法 JSON（草稿块形态），先以 2 空格缩进重新格式化再展示。
            // 必须在本函数入口反转义（#278）之前执行：反转义会把 JSON 内合法转义（字面反斜杠+n）
            // 变成裸换行导致 JSON.parse 失败；格式化输出仍是 JSON 转义形式，随后经反转义呈现为真实换行。
            // 解析失败保持原样，普通代码块/日志不受影响；格式化只在渲染层做，不改 chat_events 存储。
            // 字符类 [^] 匹配任意字符（含换行），不含反斜杠——board.py 三引号转义层与测试提取层结果一致。
            // 闭围栏要求独立成行（前瞻 (?=反斜杠n|$)，不消费），避免 JSON 值内恰好含 ``` 字样时在错误位置截断。
            src = (src || '').replace(/```[^\n]*\n[^]*?\n```(?=\n|$)/g, (m) => {
                const nl = m.indexOf('\n');
                const head = m.slice(0, nl + 1);
                const body = m.slice(nl + 1, m.length - 3);
                try {
                    return head + JSON.stringify(JSON.parse(body), null, 2) + '\n```';
                } catch (e) {
                    return m;
                }
            });
            // #296：行内代码与普通围栏块先提为占位符（guardStore），防止 #278 全局反转义把代码段内的
            // 字面反斜杠+n 从中间拆成换行（需求 295 现场：行内代码被劈成孤反引号 + 新段落）。
            // 顺序约束：必须在 #280 JSON 草稿围栏格式化之后执行；JSON 可解析围栏不进保护层——
            // stringify 输出仍含字面反斜杠+n，须继续经 #278 反转义为真实换行（T13 不回归），
            // 故对每个围栏再试 JSON.parse 判定。行内代码只保护单行反引号对（跨行对保持现状，
            // 与逐行 inlineMd 语义一致）。占位符用独立 token @@RB<i>@@ 避开逐行 @@CODE 编号空间，
            // 在 #278 反转义后、escapeHtml 前回填，原文统一走「先转义再转换」XSS 基线。
            const guardStore = [];
            src = (src || '').replace(/(```[^\n]*\n[^]*?\n```(?=\n|$))|(`[^`\n]+`)/g, (m, fence) => {
                if (fence) {
                    const nl = fence.indexOf('\n');
                    const body = fence.slice(nl + 1, fence.length - 3);
                    try { JSON.parse(body); return fence; } catch (e) { /* 普通围栏：保护 */ }
                }
                guardStore.push(m);
                return '@@RB' + (guardStore.length - 1) + '@@';
            });
            // #278：agent 把草稿 JSON 的转义文本（字面反斜杠+n、字面反斜杠+双引号）粘贴进正文时，
            // 先反转义为真实换行/引号再渲染，避免整段挤在单个 <p>；正文真想显示这两个字符的场景几乎不存在，副作用可忽略。
            // 反斜杠用 String.fromCharCode(92) 构造：board.py 三引号转义层与测试提取层（双反斜杠+n 缩为单反斜杠+n）在此写法下结果一致。
            const BS = String.fromCharCode(92);  // 字面反斜杠字符
            src = (src || '').split(BS + 'n').join('\n').split(BS + '"').join('"');
            // #296：反转义完成后回填占位符（保护内容原文，其中字面反斜杠+n/引号保持字面）
            src = src.replace(/@@RB(\d+)@@/g, (m, i) => guardStore[+i] || m);
            const isTableRow = s => /^\|.*\|\s*$/.test(s);
            const isSepRow = s => /^[\s|:|-]+$/.test(s) && s.includes('-') && (s.match(/\|/g) || []).length >= 2;
            const splitRow = r => {
                let s = r.trim();
                if (s.startsWith('|')) s = s.slice(1);
                if (s.endsWith('|')) s = s.slice(0, -1);
                return s.split('|').map(c => c.trim());
            };
            const codeStore = [];
            const imgStore = [];
            const inlineMd = t => {
                const esc = t.replace(/`([^`]+)`/g, (m, c) => {
                    codeStore.push(`<code>${c}</code>`);
                    return `@@CODE${codeStore.length - 1}@@`;
                });
                let html = esc
                    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
                    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
                    // 图片：仅命中本站产物 API 形态才渲染 <img>；其余先提为占位符（降级纯文本），防止被下方链接正则二次处理
                    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (m, alt, url) =>
                        /^\/api\/requirements\/\d+\/artifacts\/[A-Za-z0-9._-]+$/.test(url)
                            ? `<img src="${url}" alt="${alt}">`
                            : (imgStore.push(`![${alt}](${url})`), `@@IMG${imgStore.length - 1}@@`))
                    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, text, url) =>
                        /^(https?|mailto):/i.test(url) ? `<a href="${url}">${text}</a>` : `[${text}](${url})`);
                return html.replace(/@@CODE(\d+)@@/g, (m, i) => codeStore[+i] || '')
                           .replace(/@@IMG(\d+)@@/g, (m, i) => imgStore[+i] || '');
            };
            const lines = escapeHtml(src).split('\n');
            const out = [];
            let inFence = false, fenceLines = [], listType = null, listItems = [], inQuote = false, quoteLines = [];
            const flushList = () => {
                if (!listType) return;
                out.push(`<${listType === 'ol' ? 'ol' : 'ul'}>${listItems.map(li => `<li>${li}</li>`).join('')}</${listType === 'ol' ? 'ol' : 'ul'}>`);
                listType = null; listItems = [];
            };
            const flushQuote = () => {
                if (!inQuote) return;
                out.push(`<blockquote>${quoteLines.join('<br>')}</blockquote>`);
                inQuote = false; quoteLines = [];
            };
            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];
                const t = line.trim();
                // 围栏代码块：整块原样保留（含空行与缩进），不做行内转换
                if (/^```/.test(t)) {
                    if (inFence) { out.push(`<pre><code>${fenceLines.join('\n')}</code></pre>`); inFence = false; fenceLines = []; }
                    else { flushList(); flushQuote(); inFence = true; fenceLines = []; }
                    continue;
                }
                if (inFence) { fenceLines.push(line); continue; }
                // 表格：当前行是管道行且下一行是表头分隔行 → 聚合整块表格（分隔行 vs 分割线歧义：非表格上下文才按 hr）
                if (isTableRow(t) && i + 1 < lines.length && isSepRow(lines[i + 1].trim())) {
                    flushList(); flushQuote();
                    const head = splitRow(t);
                    const seps = splitRow(lines[i + 1].trim());
                    i += 1;  // 跳过分隔行
                    const aligns = seps.map(c => {
                        if (/^:?-+:?$/.test(c)) return c.startsWith(':') && c.endsWith(':') ? 'center' : (c.endsWith(':') ? 'right' : 'left');
                        return null;
                    });
                    const th = head.map((c, k) => `<th${aligns[k] ? ` style="text-align:${aligns[k]}"` : ''}>${inlineMd(c)}</th>`).join('');
                    const body = [];
                    while (i + 1 < lines.length && isTableRow(lines[i + 1].trim())) {
                        i++;
                        body.push(`<tr>${splitRow(lines[i].trim()).map((c, k) => `<td${aligns[k] ? ` style="text-align:${aligns[k]}"` : ''}>${inlineMd(c)}</td>`).join('')}</tr>`);
                    }
                    out.push(`<table><thead><tr>${th}</tr></thead>${body.length ? `<tbody>${body.join('')}</tbody>` : ''}</table>`);
                    continue;
                }
                if (!t) { flushList(); flushQuote(); continue; }
                const h = t.match(/^(#{1,6})\s+(.+)$/);
                if (h) { flushList(); flushQuote(); out.push(`<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`); continue; }
                if (/^(---|\*\*\*|___)\s*$/.test(t)) { flushList(); flushQuote(); out.push('<hr>'); continue; }
                const ul = t.match(/^\s*[-*+]\s+(.+)$/);
                if (ul) {
                    if (listType !== 'ul') { flushList(); flushQuote(); listType = 'ul'; }
                    listItems.push(inlineMd(ul[1]));
                    continue;
                }
                const ol = t.match(/^\s*\d+\.\s+(.+)$/);
                if (ol) {
                    if (listType !== 'ol') { flushList(); flushQuote(); listType = 'ol'; }
                    listItems.push(inlineMd(ol[1]));
                    continue;
                }
                const q = t.match(/^&gt;\s?(.*)$/);
                if (q) {
                    flushList();
                    if (!inQuote) inQuote = true;
                    quoteLines.push(inlineMd(q[1]));
                    continue;
                }
                flushList(); flushQuote();
                out.push(`<p>${inlineMd(line)}</p>`);
            }
            flushList(); flushQuote();
            if (inFence && fenceLines.length) out.push(`<pre><code>${fenceLines.join('\n')}</code></pre>`);
            return out.join('\n');
        }

        // #381：unified diff 类 GitHub 渲染（自写解析器，不引第三方库）。输入为调度器 _gen_review_diff
        // 代写的 diff.patch：头部「===== 仓库 <name>（git diff <base>...<branch>）=====」（截断时随附说明行），
        // 其后为标准 git diff 输出（diff --git / index / --- / +++ / @@ / 行）。
        // 安全基线：逐行 escapeHtml 后再组装 HTML（与 renderArtifactMd 同一基线），diff 内容来自仓库原文、
        // 可含任意字符（如 <script>），转义后不可能产生可执行节点。
        // 结构：文件节（diff --git 头，标题取 b/ 侧路径）/ hunk（@@ 行号，省略的 count 按 1）/ 行
        // （+ 绿 / - 红 / 空格上下文，行号随 hunk 声明推进）；头外其余行中性展示（截断说明行高亮为 note）。
        function renderDiff(src) {
            const esc = s => String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const out = [];
            let aNo = 0, bNo = 0, inHunk = false;  // 行号游标（0 = 尚未进入 hunk 或已出 hunk）
            for (const raw of String(src || '').split('\n')) {
                const line = esc(raw);
                if (/^diff --git /.test(line)) {
                    inHunk = false; aNo = bNo = 0;
                    const m = line.match(/^diff --git a\/(.*?) b\/(.*)$/);
                    out.push(`<div class="diff-file">${esc(m ? m[2] : line.slice(10))}</div>`);
                    continue;
                }
                const h = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
                if (h) {
                    aNo = +h[1]; bNo = +h[3]; inHunk = true;
                    out.push(`<div class="diff-hunk">${line}</div>`);
                    continue;
                }
                if (inHunk && /^(\+|-| )/.test(line)) {
                    const cls = line[0] === '+' ? ' diff-add' : (line[0] === '-' ? ' diff-del' : '');
                    const aCell = line[0] === '+' ? '' : aNo++;  // 加行不占旧行号
                    const bCell = line[0] === '-' ? '' : bNo++;  // 删行不占新行号
                    out.push(`<div class="diff-row${cls}"><span class="diff-n">${aCell || '&nbsp;'}</span><span class="diff-n b">${bCell || '&nbsp;'}</span><span class="diff-c">${line}</span></div>`);
                    continue;
                }
                inHunk = false;
                if (/^===== /.test(line)) out.push(`<div class="diff-repo">${line}</div>`);
                else if (/^（diff 过长已截断/.test(line)) out.push(`<div class="diff-note">${line}</div>`);
                else if (/^(index |--- |\+\+\+ |\ |\s*$)/.test(line)) out.push(`<div class="diff-meta">${line || '&nbsp;'}</div>`);
                else out.push(`<div class="diff-meta">${line}</div>`);
            }
            return `<div class="diff-view">${out.join('\n')}</div>`;
        }

        // 执行过程视图：整页展示某会话的事件流（不做高度截断），doing 时每 2 秒自动刷新。
        // 刷新时若用户停在底部则跟随新事件，否则保留阅读位置。
        // ACP 流式文本可能因缓冲阈值被拆成多条 message 事件，跨事件边界的 Markdown 标记（如 `**`）
        // 逐条渲染会裸显并产生额外换行（例：`**判定`/`**：` 原文应还原为 `**判定**：`），
        // 故相邻 message 事件按原始顺序无分隔拼接（不加工内容、不插分隔符，无损还原原文）
        // 后统一经 renderArtifactMd 渲染；tool/system 等非 message 事件作为明确边界保持独立展示。
        function mergeMessageBlocks(events) {
            const blocks = [];
            for (const e of events || []) {
                const content = e && e.content != null ? String(e.content) : '';
                if (e && e.kind === 'message' && blocks.length && blocks[blocks.length - 1].kind === 'message') {
                    blocks[blocks.length - 1].content += content;
                } else {
                    blocks.push({kind: e ? e.kind : '', content});
                }
            }
            return blocks;
        }
        // #294 选区守卫状态：mousedown 落在周期性重建 DOM 的面板内时记录该面板，mouseup/窗口失焦清除。
        // 拖动选择期间（mousedown 至 mouseup）与面板内已选中文字期间，刷新跳过本轮重建，选区保持、事件下一轮补上
        const selectingPanels = new Set();
        document.addEventListener('mousedown', (e) => {
            const t = e.target;
            if (!t || !t.closest) return;
            const panel = t.closest('#procEvents, #chatHistory');
            if (panel) selectingPanels.add(panel);
        });
        window.addEventListener('mouseup', () => { selectingPanels.clear(); });
        window.addEventListener('blur', () => { selectingPanels.clear(); });

        // #294 选区守卫纯函数：selecting 置位（拖动选择进行中）或面板内存在非折叠选区时返回 true。
        // anchorNode 落点按需求原文判定（跨面板边界以 anchor 为准）：元素节点从自身起、文本节点从
        // 父元素起向上查找面板边界；参数注入（selection/selecting）便于 node harness 脱离浏览器测试。
        function selectionGuardActive(panel, selection, selecting) {
            if (selecting) return true;
            if (!panel || !selection || selection.isCollapsed) return false;
            const anchor = selection.anchorNode;
            if (!anchor) return false;
            let node = anchor.nodeType === 1 ? anchor : anchor.parentNode;
            while (node) {
                if (node === panel) return true;
                node = node.parentNode;
            }
            return false;
        }
        async function refreshProcess(id, sid) {
            const panel = document.getElementById('procEvents');
            if (!panel) return;
            // #294 选区守卫：面板内选中文字/拖动选择期间跳过本轮 DOM 重建（选区不丢，新事件下一轮轮询自然补上）
            if (selectionGuardActive(panel, window.getSelection(), selectingPanels.has(panel))) return;
            // #284 会话事件防串：面板渲染时已打标当前会话（dataset.sid），请求前后校验一致才写入——
            // 迟到的/跨会话轮询响应直接丢弃，防止在跑会话的事件流污染其它会话的面板
            if (panel.dataset.sid && panel.dataset.sid !== sid) return;
            const wasAtBottom = panel.scrollHeight > 0 && panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 32;
            try {
                const res = await fetch(`/api/requirements/${id}/events?session=${encodeURIComponent(sid)}`, {cache: 'no-store'});
                const events = await res.json();
                if (panel.dataset.sid && panel.dataset.sid !== sid) return;
                // #421：记录事件流最后一条的服务端时间（ts_ms）——状态行「事件流」证据与判死双条件用
                lastProcEventAtMs = events.reduce((mx, e) => Math.max(mx, e.ts_ms || 0), 0) || null;
                // 基准变了立即补一次状态卡重算（需求助手侧 refreshReqChat 早有同款补齐，见
                // tests/test_chat_status_card.py）：首屏渲染先于事件并入，「事件流」证据会停在占位
                // 「—（未加载执行过程）」；不补则自动展开档（红/琥珀）要多等一拍 1s tick，
                // 非 running 档没有 ticker 更是要等用户点一下头部才恢复。
                // 走共享 tick（文本级；判定依据仅展开态写）——不用整卡重建，避免冲掉展开态与选区。
                chatStatusTick();
                const blocks = mergeMessageBlocks(events);
                // #284 乐观回显合并：先按 TTL 清理过期，再剔除已被服务器写入的（赋值回数组防只增不减），
                // 剩余 pending 追加到流尾（带「等待接管」标记）
                const now = Date.now();
                pendingUserMsgs = pendingUserMsgs.filter(m => now - m.ts < PENDING_USER_MSG_TTL);
                const serverUserTexts = new Set(blocks.filter(b => b.kind === 'user').map(b => b.content));
                pendingUserMsgs = pendingUserMsgs.filter(m => !serverUserTexts.has(m.text));
                const allBlocks = blocks.concat(pendingUserMsgs.map(m => ({kind: 'user', content: m.text, pending: true})));
                panel.innerHTML = allBlocks.length ? allBlocks.map(b => {
                    const cls = b.kind === 'tool' ? 'ev-tool'
                        : (b.kind === 'system' ? 'ev-sys'
                            : (b.kind === 'user' ? 'ev-user' : 'ev-msg'));
                    // 工具行前缀「⚙ 」已移除（2026-09-17 视觉权重分层）：改由 .ev-tool::before
                    // 的上下文圆点承担，与缩进/缩小/变浅共同把工具行压成从属层级
                    const body = cls === 'ev-msg' ? renderArtifactMd(b.content) : escapeHtml(b.content);
                    const pendingTag = b.pending ? '<span class="ev-pending-tag">已发送，等待调度器接管…</span>' : '';
                    return `<div class="ev-item ${cls}">${body}${pendingTag}</div>`;
                }).join('') : '<div class="ev-empty">暂无执行记录</div>';
                if (wasAtBottom || !panel.dataset.loaded) {
                    panel.scrollTop = panel.scrollHeight;
                    panel.dataset.loaded = '1';
                }
            } catch (e) { /* 网络异常静默，下一轮再试 */ }
        }
        function refreshProcessIfActive() {
            if (!selectedId) return;
            if (selectedAgentSession) {
                const row = (agentSessions || []).find(x => x.id === selectedAgentSession && x.req_id === selectedId);
                if (row && row.session_id) refreshProcess(selectedId, row.session_id);
                return;
            }
            if (!selectedSid) return;
            const item = items.find(i => i.id === selectedId);
            if (!item) return;
            refreshProcess(selectedId, selectedSid);
        }
        // #51 调度历史回放：把事件流整理成阶段骨架（纯函数，node harness 断言）。
        // 阶段边界只认调度器既有 phase/status/spawn 文案（不改事件采集口径）：
        //   status→doing 起「入队」；spawn/「完成，进入「X」」/「进入首节点「X」」起阶段；
        //   未通过打回同时把评审阶段记 reject、目标阶段记 redo；进入流程收尾/需求完成起「收尾」。
        // 同一阶段被打回重做后仍合并为一行（阶段数按去重阶段计），耗时累加各次进入区间。
        function dispatchTsMs(ts, times) {
            // 墙钟字符串只作显示/索引；时间运算使用服务端 epoch，浏览器时区不参与解析。
            const ms = times && times.get(ts);
            return typeof ms === 'number' && isFinite(ms) ? ms : null;
        }
        function formatDispatchDuration(ms) {
            const mins = Math.floor(Math.max(0, Number(ms) || 0) / 60000);
            if (mins < 60) return mins + 'm';
            const h = Math.floor(mins / 60), m = mins % 60;
            return m ? h + 'h' + m + 'm' : h + 'h';
        }
        function buildDispatchStages(events) {
            const list = Array.isArray(events) ? events : [];
            const times = new Map(list.map(e => [e.ts, e.ts_ms]));
            const ENQUEUE = '入队', TERMINAL = '收尾';
            const rePass = /^「(.+?)」评审通过/;
            const reEnter = /^「(.+?)」完成.*?进入「(.+?)」/;
            const reTerminalEnter = /^「(.+?)」完成.*?进入流程收尾/;
            const reReject = /^「(.+?)」未通过，打回「(.+?)」重做（第 (\d+) 轮）/;
            const reManualReject = /^人工裁决打回「(.+?)」再修一轮/;
            const reManualPass = /^人工裁决放行「(.+?)」/;
            const reFlowEnter = /进入首节点「(.+?)」/;
            const reSpawn = /^拉起「(.+?)」会话/;
            const reNodeDone = /^「(.+?)」完成/;
            const reDone = /需求(?:已)?完成|→ 已完成\(done\)/;
            // 重试只认「第 N 次自动重试」；显式排除「不自动重试」的终止留痕（需求 #7 事件 197）
            const reRetry = /\d+\s*次自动重试/;
            const isManualReject = (content) => reManualReject.test(content);
            const byKey = {};
            const stages = [];
            let cur = null;
            let lastMs = null;
            let lastTs = null;
            const stageFor = (label, ts) => {
                let s = byKey[label];
                if (!s) {
                    s = {key: label, label, startTs: ts, visits: [], pass: false,
                         reviewPass: false, done: false,
                         redo: 0, reject: 0, terminal: label === TERMINAL, events: []};
                    byKey[label] = s;
                    stages.push(s);
                }
                return s;
            };
            const open = (label, ts) => {
                const s = stageFor(label, ts);
                s.visits.push({start: ts, end: null});
                cur = s;
                return s;
            };
            const close = (ts) => {
                if (!cur) return;
                const v = cur.visits[cur.visits.length - 1];
                if (v && v.end == null) v.end = ts;
            };
            const start = (label, ts) => {
                if (cur && cur.label === label) return cur;
                close(ts);
                return open(label, ts);
            };
            const append = (s, e) => { if (s) s.events.push(e); };
            const mark = (s, field, val) => { if (s && val > (s[field] || 0)) s[field] = val; };
            list.forEach(e => {
                const content = String((e && e.content) || '');
                const ts = e && e.ts;
                const ms = dispatchTsMs(ts, times);
                if (ms != null) { lastMs = ms; lastTs = ts; }
                let m;
                if (e.kind === 'status' && /→ 进行中\(doing\)/.test(content)) {
                    if (!byKey[ENQUEUE]) start(ENQUEUE, ts);
                    append(cur, e);
                    return;
                }
                if (!byKey[TERMINAL] && reDone.test(content)) {
                    append(cur, e);
                    start(TERMINAL, ts);
                    return;
                }
                if (e.kind === 'status' && /→ 已完成\(done\)/.test(content)) {
                    start(TERMINAL, ts);
                    append(cur, e);
                    return;
                }
                if ((m = reTerminalEnter.exec(content))) {
                    const src = (cur && cur.label === m[1]) ? cur : byKey[m[1]];
                    if (src) { src.pass = true; src.done = true; }
                    append(src || cur, e);
                    start(TERMINAL, ts);
                    return;
                }
                if ((m = reManualReject.exec(content))) {
                    // 「人工裁决打回「Y」再修一轮」不带上限/评审者名：当前阶段记一次打回，
                    // 目标阶段记一次重做；轮次各自 +1（#25 事件 1024，实际第 4 轮）。
                    const target = m[1];
                    const rev = cur;
                    if (rev) {
                        rev.reject = (rev.reject || 0) + 1;
                        append(rev, e);
                    } else {
                        append(stageFor(target, ts), e);
                    }
                    const tgt = start(target, ts);
                    tgt.redo = (tgt.redo || 0) + 1;
                    return;
                }
                if ((m = reReject.exec(content))) {
                    const reviewer = m[1], target = m[2], round = parseInt(m[3], 10) || 1;
                    const rev = (cur && cur.label === reviewer) ? cur : stageFor(reviewer, ts);
                    mark(rev, 'reject', round);
                    append(rev, e);
                    const tgt = start(target, ts);
                    mark(tgt, 'redo', round);
                    return;
                }
                if ((m = reEnter.exec(content))) {
                    const from = m[1], to = m[2];
                    const src = (cur && cur.label === from) ? cur : (byKey[from] || stageFor(from, ts));
                    src.pass = true;
                    src.done = true;
                    append(src, e);
                    start(to, ts);
                    return;
                }
                if ((m = rePass.exec(content))) {
                    const s = (cur && cur.label === m[1]) ? cur : (byKey[m[1]] || stageFor(m[1], ts));
                    s.pass = true;
                    s.reviewPass = true;
                    append(s, e);
                    return;
                }
                if ((m = reManualPass.exec(content))) {
                    // 人工放行只出现在评审超限裁决（dispatcher.py）：按「评审通过」落档
                    const s = (cur && cur.label === m[1]) ? cur : (byKey[m[1]] || stageFor(m[1], ts));
                    s.pass = true;
                    s.reviewPass = true;
                    append(s, e);
                    return;
                }
                if ((m = reFlowEnter.exec(content))) {
                    if (!cur) open(ENQUEUE, ts);
                    append(cur, e);
                    start(m[1], ts);
                    return;
                }
                if ((m = reSpawn.exec(content))) {
                    if (!cur || cur.label !== m[1]) start(m[1], ts);
                    append(cur, e);
                    return;
                }
                if ((m = reNodeDone.exec(content))) {
                    const s = (cur && cur.label === m[1]) ? cur : (byKey[m[1]] || stageFor(m[1], ts));
                    s.pass = true;
                    s.done = true;
                    append(s, e);
                    return;
                }
                if (!cur) open(ENQUEUE, ts);
                append(cur, e);
            });
            if (cur) close(lastTs);
            stages.forEach(s => {
                let ms = 0;
                s.visits.forEach(v => {
                    const a = dispatchTsMs(v.start, times);
                    const b = v.end != null ? dispatchTsMs(v.end, times) : lastMs;
                    if (a != null && b != null && b >= a) ms += b - a;
                });
                s.durationMs = ms;
                s.tone = s.terminal ? 'pass' : (s.redo || s.reject) ? 'warn' : s.pass ? 'pass' : 'normal';
                // 徽标区分评审节点与普通完成节点：只有「评审通过」事件（或曾被打回后人工放行）
                // 才显示「评审通过」；普通节点/收尾显示「完成」。
                s.badge = s.terminal ? '完成'
                    : s.redo ? '重做' + s.redo + '轮'
                    : s.reject ? '打回' + s.reject + '轮'
                    : s.reviewPass ? '评审通过'
                    : s.done ? '完成' : '';
            });
            const firstMs = stages.length ? dispatchTsMs(stages[0].startTs, times) : null;
            return {
                stages,
                stats: {
                    totalMs: firstMs != null && lastMs != null ? Math.max(0, lastMs - firstMs) : 0,
                    stageCount: stages.length,
                    // 打回轮数含人工裁决打回（无第 N 轮文本，按事件条数累计）
                    rejectCount: list.filter(e => {
                        const c = String((e && e.content) || '');
                        return reReject.test(c) || isManualReject(c);
                    }).length,
                    retryCount: list.filter(e => e && e.kind === 'retry'
                        && reRetry.test(String(e.content || ''))
                        && !/不自动重试/.test(String(e.content || ''))).length,
                    eventCount: list.length,
                },
            };
        }
        // 全部事件视图沿用改造前的行渲染（提问卡片 / spawn 链接 / 模型名 / 时间列复制均不变），
        // 骨架明细展开复用同一渲染器：事件内容与改造前逐字一致，不丢事件（需求验收）。
        function dispatchEventRows(id, events, allEvents) {
            const conf = confirmations && confirmations[id];
            const confQ = conf && ['confirm', 'confirm_override'].includes(conf.kind)
                ? conf.question : null;
            // 旧格式提问的角色补齐按「全量事件里最后一条 confirm」判定；骨架明细传入的是
            // 单阶段子集，不能按子集下标判断（否则会把其它阶段的待确认角色套进来）。
            const scope = Array.isArray(allEvents) ? allEvents : events;
            let lastConfirmId = null;
            if (confQ) scope.forEach(e => { if (e && e.kind === 'confirm') lastConfirmId = e.id; });
            const rows = events.map(e => {
                // 点击时间列复制用（时间戳不进 data-* 属性，见 copyDispTime）
                dispTsStore[e.id] = e.ts;
                const q = parseConfirmQuestion(e);
                if (q) {
                    // 角色名：新格式解析自带；旧格式（历史留档）未决时用 conf.role（roleLabel 中文名）补齐
                    let role = q.role;
                    if (!role && confQ && e.id === lastConfirmId && conf.role) role = roleLabel(conf.role);
                    const summary = `「${escapeHtml(role || 'agent')}」需要确认，具体信息如下：`;
                    // 排版：按句号/分号断行（仅展示层处理，不改数据；#411 讨论稿确认的观感）
                    const pretty = escapeHtml(q.question.replace(/[。；]/g, m => m + '\n').trim());
                    return `<div class="ev-item ev-dispatch"><span class="ev-time" onclick="copyDispTime(${e.id})">${escapeHtml(formatEventClock(e.ts))}</span><span>${summary}</span></div>`
                         + `<div class="ev-confirm-card"><div class="card-label">问题全文</div>${pretty}</div>`;
                }
                let extra = '';
                if (e.kind === 'spawn' && e.session_id) {
                    const sid = escapeHtml(e.session_id);
                    // 入口统一（#120 遗留缺口）：命中 agent_sessions 注册表行时与中间栏会话行同一视图
                    // （执行过程 + 状态栏 + 聊天框，可续聊）；未命中（历史/旧数据无注册表行）回退只读
                    // 执行过程视图——标题随落点区分，只读入口不谎称可续聊
                    const jumpTitle = agentSessionRowIdForSid(agentSessions, id, e.session_id) != null
                        ? '查看执行过程并继续会话' : '查看该会话完整执行过程';
                    // #14 括号模型名追加在链接闭合标签之后、正文 span 之内（不新增平级 flex item、
                    // 不写进链接 span——链接热区与 #290 断点合并语义均不变）；模型名先转义再进模板
                    const model = escapeHtml(agentSessionModelForSid(agentSessions, id, e.session_id));
                    extra = ` <span class="sf-id-link" title="${jumpTitle}" onclick="openSessionFromSid(${id},'${sid}')">${sid}</span>(${model})`;
                }
                return `<div class="ev-item ev-dispatch"><span class="ev-time" onclick="copyDispTime(${e.id})">${escapeHtml(formatEventClock(e.ts))}</span><span>${escapeHtml(e.content)}${extra}</span></div>`;
            });
            return rows.join('');
        }
        function dispatchStatsHtml(stats) {
            const parts = [
                ['总耗时', formatDispatchDuration(stats.totalMs)],
                ['阶段', String(stats.stageCount)],
                ['打回', stats.rejectCount + '轮'],
                ['重试', stats.retryCount + '次'],
                ['事件', stats.eventCount + '条'],
            ];
            return parts.map(([k, v]) =>
                `<span class="disp-stat"><span class="disp-stat-k">${k}</span><span class="disp-stat-v">${escapeHtml(v)}</span></span>`
            ).join('');
        }
        // 流程骨架：左侧状态圆点（绿=评审通过/完成，橙=重做/打回，灰=普通阶段）、
        // 时间/阶段名/耗时/状态徽标；有明细的阶段带展开箭头与行尾「N 条」。
        function dispatcherSkeletonHtml(id, stages) {
            if (!stages.length) return '<div class="ev-empty">暂无调度记录</div>';
            return stages.map((s, idx) => {
                const hasDetails = s.events.length > 0;
                const open = !!dispatcherStageOpen[s.key];
                const caret = hasDetails
                    ? `<span class="disp-caret${open ? ' open' : ''}" aria-hidden="true">›</span>`
                    : '<span class="disp-caret" aria-hidden="true"></span>';
                const dur = `<span class="disp-stage-dur">${formatDispatchDuration(s.durationMs)}</span>`;
                const badge = s.badge ? `<span class="disp-badge ${s.tone}">${escapeHtml(s.badge)}</span>` : '';
                const count = hasDetails ? `<span class="disp-count">${s.events.length} 条</span>` : '';
                return `<div class="disp-stage${open ? ' open' : ''}">
                    <div class="disp-stage-row${hasDetails ? ' expandable' : ''}"${hasDetails
                        ? ` onclick="toggleDispatcherStage(${idx})" role="button" tabindex="0"
                            onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleDispatcherStage(${idx});}"`
                        : ''}>
                        <span class="disp-dot ${s.tone}" aria-hidden="true"></span>
                        <span class="disp-stage-time">${escapeHtml(formatEventClock(s.startTs))}</span>
                        <span class="disp-stage-name">${escapeHtml(s.label)}</span>
                        <span class="disp-stage-spacer"></span>
                        ${dur}${caret}${badge}${count}
                    </div>
                    ${open ? `<div class="disp-stage-detail">${dispatchEventRows(id, s.events, dispatcherEventsCache)}</div>` : ''}
                </div>`;
            }).join('');
        }
        // 展开/折叠只重绘面板（事件已缓存），不重复请求；切视图/切卡/刷新回默认收起
        function updateDispatcherControls(stages) {
            document.querySelectorAll('.disp-tab[data-disp-view]').forEach(el => {
                const on = el.dataset.dispView === dispatcherView;
                el.classList.toggle('active', on);
                el.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            const expandBtn = document.getElementById('dispExpandAll');
            const collapseBtn = document.getElementById('dispCollapseAll');
            if (!expandBtn || !collapseBtn) return;
            const detailStages = stages.filter(s => s.events.length > 0);
            const allOpen = detailStages.length > 0 && detailStages.every(s => dispatcherStageOpen[s.key]);
            const allClosed = detailStages.every(s => !dispatcherStageOpen[s.key]);
            const skeleton = dispatcherView === 'skeleton';
            expandBtn.disabled = !skeleton;
            collapseBtn.disabled = !skeleton;
            expandBtn.classList.toggle('active', skeleton && allOpen);
            collapseBtn.classList.toggle('active', skeleton && allClosed);
        }
        function renderDispatcherBody() {
            const statsEl = document.getElementById('dispatchStats');
            const panel = document.getElementById('dispatcherEvents');
            if (!statsEl || !panel) return;
            // 切卡请求未返回的窗口期：缓存必须属于当前卡，绝不能用上一张卡的数据渲染
            if (dispatcherEventsCacheReq !== selectedId) {
                dispatcherStagesCache = [];
                statsEl.innerHTML = '';
                panel.innerHTML = '<div class="ev-empty">加载中…</div>';
                delete panel.dataset.loaded;
                updateDispatcherControls([]);
                return;
            }
            const built = buildDispatchStages(dispatcherEventsCache);
            dispatcherStagesCache = built.stages;
            statsEl.innerHTML = dispatchStatsHtml(built.stats);
            const wasAtBottom = panel.scrollHeight > 0
                && panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 32;
            if (panel.dataset.view !== dispatcherView) {
                delete panel.dataset.loaded;
                panel.dataset.view = dispatcherView;
            }
            panel.innerHTML = dispatcherView === 'skeleton'
                ? dispatcherSkeletonHtml(dispatcherEventsCacheReq, built.stages)
                : (dispatchEventRows(dispatcherEventsCacheReq, dispatcherEventsCache, dispatcherEventsCache)
                    || '<div class="ev-empty">暂无调度记录</div>');
            if (dispatcherView === 'events' && (wasAtBottom || !panel.dataset.loaded)) {
                panel.scrollTop = panel.scrollHeight;
                panel.dataset.loaded = '1';
            }
            updateDispatcherControls(built.stages);
        }
        function setDispatcherView(view) {
            if (view !== 'skeleton' && view !== 'events') return;
            dispatcherView = view;
            dispatcherStageOpen = {};   // 切视图回默认全部收起
            renderDispatcherBody();
        }
        function toggleDispatcherStage(idx) {
            if (dispatcherView !== 'skeleton' || dispatcherEventsCacheReq !== selectedId) return;
            const s = dispatcherStagesCache[idx];
            if (!s || !s.events.length) return;
            dispatcherStageOpen[s.key] = !dispatcherStageOpen[s.key];
            renderDispatcherBody();
        }
        function expandAllDispatcherStages() {
            if (dispatcherView !== 'skeleton' || dispatcherEventsCacheReq !== selectedId) return;
            dispatcherStagesCache.forEach(s => { if (s.events.length) dispatcherStageOpen[s.key] = true; });
            renderDispatcherBody();
        }
        function collapseAllDispatcherStages() {
            if (dispatcherView !== 'skeleton' || dispatcherEventsCacheReq !== selectedId) return;
            dispatcherStageOpen = {};
            renderDispatcherBody();
        }
        // 调度器动作时间线：整条需求回放，每次渲染全量拉取（数据量小，无需增量）
        async function refreshDispatcherEvents(id) {
            const panel = document.getElementById('dispatcherEvents');
            if (!panel) return;
            try {
                const res = await fetch(`/api/requirements/${id}/dispatcher-events`, {cache: 'no-store'});
                const events = await res.json();
                // 切卡后旧请求才返回：丢弃过期响应，避免把上一张卡的事件写进当前面板
                if (!selectedDispatcher || selectedId !== id) return;
                if (dispatcherEventsCacheReq !== id) {
                    dispatcherEventsCacheReq = id;
                    dispatcherStageOpen = {};
                }
                dispatcherEventsCache = Array.isArray(events) ? events : [];
                renderDispatcherBody();
            } catch (e) { /* 网络异常静默，下一轮再试 */ }
        }
        // 提问类 confirm 事件解析（#411 讨论稿）：返回 {role, question}；动作类/其它事件返回 null
        // 新格式：'「X」需要确认：<question>'（dispatcher 写端 #411）；旧格式：'agent 需要确认：<question>'（历史留档）
        function parseConfirmQuestion(e) {
            if (e.kind !== 'confirm') return null;
            let m = e.content.match(/^「([^」]+)」需要确认：/);
            if (m) return {role: m[1], question: e.content.slice(m[0].length)};
            m = e.content.match(/^agent 需要确认：/);
            if (m) return {role: null, question: e.content.slice(m[0].length)};
            return null;
        }
