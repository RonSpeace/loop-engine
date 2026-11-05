        // 本文件是 static/board.js 的源分片之一。static/board.js 由 static/board/*.js 按文件名顺序拼接生成：
        // 改前端脚本请改 static/board/ 下的分片，再运行 .venv/bin/python scripts/build_board_js.py（tests/test_board_js_parts.py 校验同步）。
        // #419：状态/通道/流程标签不再前端写死，由后端 GET /api/meta 注入
        // （唯一源 = board.STATUS_LABEL_CN 与 flows.json）；拉取失败时按原文显示（fail-safe 降级，不白屏）
        let STATUS_LABEL = {}, MODE_LABEL = {}, FLOW_LABEL = {}, FLOW_COLOR = {};
        let PRE_ROLE = '';  // #431 前置槽位角色（/api/meta 注入）：草稿态展示角色兜底
        const FLOW_COLOR_FALLBACK = 'slate';  // #428 未命中（无 flow / 已退役流程 / meta 降级）= 中性灰
        async function loadMeta() {
            try {
                const d = await (await fetch('/api/meta')).json();
                STATUS_LABEL = d.status_labels || {};
                MODE_LABEL = d.mode_labels || {};
                PRE_ROLE = d.pre_role || '';
                FLOW_LABEL = {};
                FLOW_COLOR = {};
                (d.flows || []).forEach(f => {
                    FLOW_LABEL[f.id] = f.name;
                    FLOW_COLOR[f.id] = f.color || FLOW_COLOR_FALLBACK;
                });
            } catch (e) { /* 降级：标签回退原文 */ }
        }
        // 「所有项目」行内状态筛选圆点：三个活跃状态（颜色与卡片徽标一致），无文字纯色点
        const STATUS_PTS = ['later', 'open', 'doing'];
        const STATUS_PT_LABEL = { later: '暂缓', open: '待开发', doing: '进行中' };
        const ALL_PROJECTS = '__all__';  // 左侧导航「所有项目」视图哨兵值（独立于真实项目名）
        const VIEW_STATE_KEY = 'reqBoardViewState';
        // #7 P7 布局可拖拽 + 已完成卡默认收缩：常量单一落点（node harness 经 _js_const 提取，
        // 行内注释不得含 ASCII 分号）；宽度只记用户拖动后的像素偏好，无存储时用默认值（= 现状宽度）
        const LAYOUT_KEY = 'reqBoardLayout';          // {navW, listW} 两处分界拖动后的宽度偏好
        const CARD_EXPAND_KEY = 'reqBoardCardExpand'; // {"<需求id>": true=手动展开 / false=手动收起}
        const SKIN_KEY = 'reqBoardSkin';              // #15 皮肤偏好（'light' = 浅色；缺省/其它 = 深色默认）
        const NAV_MIN_W = 172;      // 左栏下限 = 原写死宽度（只允许变宽）
        const NAV_DEFAULT_W = 172;  // 左栏默认宽（无存储时）
        const LIST_DEFAULT_W = 540; // 卡片流默认宽（无存储时 = 现状宽度）
        const LIST_MIN_W = 300;     // 卡片流下限
        const DETAIL_MIN_W = 360;   // 对话区下限（不可被挤没）
        const MAIN_MIN_W = 680;     // 主区下限 ⇒ 左栏上限 = 工作区宽 − COL_GAP_W − MAIN_MIN_W
        const COL_GAP_W = 16;       // 与 .workspace / .main-row 的 CSS gap 同源
        // 项目显示名映射：仅改页面上展示的名称；底层 key（DB/projects.json/产物路径/worktree 分支）一律不动
        const PROJECT_DISPLAY = {};
        function displayProjectName(name) {
            return PROJECT_DISPLAY[name] || name;
        }
        let items = [];
        let sessionInfo = {};  // 会话键(角色:需求id) -> ACP 会话信息（有记录说明跑过）；各阶段独立保留
        let agentSessions = [];  // #273 人工接管：agent_sessions 注册表（含毒会话/待接管行），中间栏会话列表数据源
                                // #431：session_id 为空的行只在右栏待接管视图用，中间栏渲染前过滤掉
        let roleMeta = [];  // #404 角色显示名/顺序 {role, display, order}（注入自 /api/roles，单一源 acp_shared.ROLE_CAPABILITIES）
        let expandedAgents = {};  // #390/#424 agent 维度会话列表：组键 "reqId:role" -> 展开态三态
                                  // （true=手动展开 / false=手动收起（压过 running 自动展开）/ 无键=未表态，内存态）
        let autoProjects = [];  // 接入自动开发的项目（左侧导航 + 添加表单下拉）
        let mergeConflicts = {};  // #386 req_id -> {action, updated_at}（merge 冲突待处理；action 非空=已点击待调度器消费）
        let confirmations = {};   // #400 req_id -> {kind, question, options, role, action, over_limit}（确认横幅渲染数据源）
        let editingId = null;
        let editingStatus = 'open';
        let editDirty = false;
        let savedViewState = (() => {
            try { return JSON.parse(localStorage.getItem(VIEW_STATE_KEY) || 'null'); }
            catch (e) { return null; }
        })();
        // 外部通知（Slack / Webhook）回链 /?req=<id>：优先于本地记住的视图，定位到该卡（所有项目、不筛选）
        (() => {
            let rid = NaN;
            try { rid = Number(new URLSearchParams(location.search).get('req')); } catch (e) { rid = NaN; }
            if (Number.isInteger(rid) && rid > 0) {
                savedViewState = {view: 'requirement', requirementId: rid, project: ALL_PROJECTS};
            }
        })();
        let selectedProject = savedViewState && typeof savedViewState.project === 'string'
            ? savedViewState.project : ALL_PROJECTS;
        // 状态筛选（later/open/doing，null = 不筛选）：随视图状态持久化，与项目选择正交
        let selectedStatus = savedViewState && STATUS_PTS.includes(savedViewState.status)
            ? savedViewState.status : null;
        // 存储解析（fail-safe）：JSON.parse 的结果非普通对象（null/数组/标量/非法 JSON）一律回 {}；
        // reqBoardLayout 与 reqBoardCardExpand 两键共用（node harness 断言）。
        function parseStoredObject(raw) {
            try {
                const v = JSON.parse(raw || 'null');
                return v && typeof v === 'object' && !Array.isArray(v) ? v : {};
            } catch (e) { return {}; }
        }
        let navCollapsed = localStorage.getItem('navCollapsed') === '1';  // 左侧菜单栏折叠状态（localStorage 记住，刷新保持）
        // #7 P7 两处持久化态（fail-safe 解析，非法/缺省值回默认）：宽度偏好 {navW, listW}；
        // 卡片手动态 {"<reqId>": true=手动展开|false=手动收起}（无键 = done 默认收缩、其余状态默认展开）
        let layoutPref = Object.assign({navW: NAV_DEFAULT_W, listW: LIST_DEFAULT_W},
            parseStoredObject(localStorage.getItem(LAYOUT_KEY)));
        let cardExpandState = parseStoredObject(localStorage.getItem(CARD_EXPAND_KEY));
        let selectedId = savedViewState && savedViewState.view === 'requirement'
            && Number.isInteger(Number(savedViewState.requirementId))
            ? Number(savedViewState.requirementId) : null;  // 右栏选中的需求 id（null = 空状态）
        let selectedSid = null;  // 右栏正在展示的执行过程会话 id（null = 需求详情视图）
        let selectedAgentSession = null;  // #273 右栏正在展示的 agent_sessions 行 id（聊天/新建会话视图）
        let selectedDispatcher = false;  // 右栏是否展示调度器动作时间线（优先于会话执行过程）
        // #51 调度历史回放：流程骨架（默认）/ 全部事件两视图；展开态只记内存，
        // 切视图、切卡、刷新页面一律回到默认全部收起（需求验收）。
        let dispatcherView = 'skeleton';
        let dispatcherStageOpen = {};
        let dispatcherEventsCache = [];
        let dispatcherEventsCacheReq = null;
        let dispatcherStagesCache = [];
        let addMode = !!(savedViewState && savedViewState.view === 'add');  // 右栏是否展示聊天建卡页
        let flashId = null;      // 新建后短暂高亮的需求 id
        let artifactInfo = {};   // req_id -> [{role, filename, short, path, exists, mtime, source}×N]（来自 /api/artifacts，产物行渲染用；source=current|legacy，缺失为 null）
        let selectedArtifact = null;  // {role, filename, path} | null，右栏产物详情视图（与需求/会话/调度器视图互斥）
        let selectedReqChat = false;  // #326 右栏是否展示需求助手视图（与需求详情/会话/调度器/产物视图互斥）
        let agentDraft = null;  // #431 右栏「新建聊天」草稿态 {reqId}（内存，不落库；文本在 localStorage）
                                // 互斥第二道防线：任何非草稿子视图分支都优先于草稿分支，且六个选中入口清此值

        function setEditStatus(s, byUser) {
            // 守卫（原需求卡 #401 已从看板删除）：已完成卡改回「待开发」= 放入自动开发调度。若实现已提交 main
            // （worktree 基线=实现提交），开发必判「无代码变更」失败转 later（#395/397/398 现场），
            // 须人工确认后才执行；取消则不做任何变化（保持原状态高亮）
            if (byUser && s === 'open') {
                const cur = items.find(i => i.id === editingId);
                if (cur && cur.status === 'done') {
                    if (!confirm(`需求 #${cur.id} 为「已完成」，改回「待开发」将进入自动开发调度。` +
                        '若实现已由人工提交 main，开发会判「无代码变更」失败并转 later（#395/397/398 现场）。\n\n' +
                        '确认改回待开发？')) {
                        return;
                    }
                }
            }
            editingStatus = s;
            document.querySelectorAll('#editStatusOpts .status-opt').forEach(o =>
                o.classList.toggle('active', o.dataset.status === s));
            // 已完成需求禁止变更所属项目；进行中需求禁止修改标题和所属项目
            const proj = document.getElementById('editProject');
            proj.disabled = (s === 'done' || s === 'doing');
            proj.title = s === 'done' ? '已完成需求不可变更所属项目'
                : (s === 'doing' ? '进行中需求不可修改所属项目' : '');
            const titleInput = document.getElementById('editTitle');
            titleInput.disabled = (s === 'doing');
            titleInput.title = s === 'doing' ? '进行中需求不可修改标题' : '';
            if (byUser) {
                editDirty = true;
                autoSave(true);
            }
        }

        function escapeHtml(s) {
            const div = document.createElement('div');
            div.textContent = s || '';
            return div.innerHTML;
        }

        let toastTimer = null;
        function showToast(msg, isError) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'toast show' + (isError ? ' error' : '');
            clearTimeout(toastTimer);
            toastTimer = setTimeout(() => t.classList.remove('show'), 2000);
        }

        // 'YYYY-MM-DD HH:MM:SS' -> 'MM-DD HH:MM'
        function formatTime(s) {
            if (!s) return '';
            return s.slice(5, 16);
        }

        // 调度历史回放时间列：'YYYY-MM-DD HH:MM:SS' -> 'HH:MM'（日期不进显示，秒也不显示；
        // 点该列复制完整时间戳，见 copyDispTime。共用 formatTime 不动，其它视图仍显示 MM-DD HH:MM）
        // 命名唯一性：页面脚本内不得再声明同名函数（函数声明提升会让后一个静默覆盖前一个）
        function formatEventClock(s) {
            if (!s) return '';
            return s.slice(11, 16);
        }

        // #422 会话头部折叠（常驻标题行 + 展开 meta/执行目录/会话记录 三行 + 路径行点整行复制）。
        // 折叠态 key = session_id（空会话 row:<行id>），同一会话在执行过程与会话详情两个入口折叠态一致、
        // 换会话天然默认折叠；只存内存（刷新页面回默认折叠，需求只要求重绘后保留）。
        const sessHeadOpen = {};
        // 该会话的完整路径（点击行时复制）：值走本 store，不进 data-* 属性（防属性注入）
        const sessPathStore = {};

        function sessHeadKey(row) {
            return (row && row.session_id) || ('row:' + ((row && row.id != null) ? row.id : ''));
        }

        // 路径中间省略切分：tail = 自末段向前累积的完整路径段（累积不超过 24 字符，至少留末段），
        // head = 其余（含分隔符，head + tail 恒等于原串、不丢信息）。worktree 得
        // dev/req-419/req-board、会话目录得末段 session_<sid>（超长时单段保留），尾部始终完整。
        function sessPathParts(path) {
            const p = String(path == null ? '' : path);
            if (!p) return {head: '', tail: ''};
            const lead = p.charAt(0) === '/' ? '/' : '';
            const segs = p.slice(lead.length).split('/');
            let i = segs.length - 1;
            let tail = segs[i];
            i--;
            while (i >= 0 && tail.length + segs[i].length + 1 <= 24) {
                tail = segs[i] + '/' + tail;
                i--;
            }
            const head = i < 0 ? lead : lead + segs.slice(0, i + 1).join('/') + '/';
            return {head: head, tail: tail};
        }

        // 会话 id 中间省略切分：tail = 末 8 位（不足 16 字符不切分，整体放 head）
        function sessTitleParts(sid) {
            const s = String(sid == null ? '' : sid);
            if (s.length <= 16) return {head: s, tail: ''};
            return {head: s.slice(0, s.length - 8), tail: s.slice(s.length - 8)};
        }

        // 展开区路径行：整行可点复制（无手型光标/hover 底色/title 气泡等暗示，保持纯文本观感；
        // 反馈只有点击后的成功 toast），复制值从 sessPathStore 取完整路径
        function sessPathRowHtml(kind, label, path) {
            const p = path || '';
            const pp = sessPathParts(p);
            const body = p
                ? (pp.head ? `<span class="sess-h">${escapeHtml(pp.head)}</span>` : '')
                  + (pp.tail ? `<span class="sess-t">${escapeHtml(pp.tail)}</span>` : '')
                : '<span class="sess-t">未找到</span>';
            return `<div class="sess-row sess-path" data-kind="${kind}" onclick="copySessPath(this)">`
                + `<span class="sess-k">${label}</span>${body}</div>`;
        }

        // 会话头部（processHtml/agentSessionHtml 共用）：常驻一行（标签 + 会话 id + 创建时间 + 折叠按钮），
        // 展开三行（meta / 执行目录 / 会话记录）。展开态按 sessHeadOpen 回填——重绘（1s 轮询、SSE、
        // 15s 全量重建）时按 map 决定 class，不无条件重置，展开态不丢。空会话（无 sid）无折叠按钮与展开区。
        function sessHeadHtml(info) {
            const open = !!sessHeadOpen[info.key];
            const sid = info.sid || '';
            sessPathStore[info.key] = {workdir: info.workdir || '', sessionDir: info.sessionDir || ''};
            const t = sessTitleParts(sid);
            let title = `<span class="sess-label">${escapeHtml(info.label)}：</span>`;
            if (sid) {
                if (t.head) title += `<span class="sess-sid sess-h">${escapeHtml(t.head)}</span>`;
                if (t.tail) title += `<span class="sess-t">${escapeHtml(t.tail)}</span>`;
            } else {
                title += '<span class="sess-sid">新会话（未开始）</span>';
            }
            if (info.created) title += `<span class="sess-created"> · 创建于 ${escapeHtml(info.created)}</span>`;
            // #13 兜底可见：配置值 != 实际值时在常驻行尾追加琥珀提示（原因全文放 title；空值零改动）
            if (info.alert) title += `<span class="sess-model-alert" title="${escapeAttr(info.alert)}">${escapeHtml(info.alert)}</span>`;
            if (sid) {
                title += `<button class="sess-fold" type="button" aria-expanded="${open ? 'true' : 'false'}"`
                    + ` title="展开/折叠会话信息" onclick="sessHeadToggle(this)">${open ? '▼' : '▶'}</button>`;
            }
            const more = sid
                ? `<div class="sess-more${open ? ' open' : ''}">`
                  + (info.meta ? `<div class="sess-meta">${info.meta}</div>` : '')
                  + (info.workdir ? sessPathRowHtml('workdir', '执行目录：', info.workdir) : '')
                  + sessPathRowHtml('session', '会话记录：', info.sessionDir)
                  + '</div>'
                : '';
            return `<div class="sess-head" data-sess-key="${escapeHtml(info.key)}">`
                + `<div class="sess-title">${title}</div>${more}</div>`;
        }

        // 展开/收起：只切 class 与字形（换字不旋转、不重建 DOM），按钮 title 悬停提示沿用
        function sessHeadToggle(btn) {
            const head = btn.closest('.sess-head');
            if (!head) return;
            const key = head.dataset.sessKey;
            const open = !sessHeadOpen[key];
            sessHeadOpen[key] = open;
            const more = head.querySelector('.sess-more');
            if (more) more.classList.toggle('open', open);
            btn.textContent = open ? '▼' : '▶';
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        }

        // 写剪贴板：navigator.clipboard 优先（安全上下文/有权限时），不可用或抛错退回
        // 隐藏 textarea + execCommand('copy')；返回是否成功
        function copyText(text) {
            return new Promise(resolve => {
                const fallback = () => {
                    try {
                        const ta = document.createElement('textarea');
                        ta.value = text;
                        ta.setAttribute('readonly', '');
                        ta.style.position = 'fixed';
                        ta.style.top = '-1000px';
                        ta.style.opacity = '0';
                        document.body.appendChild(ta);
                        ta.select();
                        const ok = !!(document.execCommand && document.execCommand('copy'));
                        document.body.removeChild(ta);
                        resolve(ok);
                    } catch (e) {
                        resolve(false);
                    }
                };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(() => resolve(true), fallback);
                } else {
                    fallback();
                }
            });
        }

        // 点路径行整行复制：复制的是完整绝对路径（显示形态中间省略不影响），可直接粘进终端 cd/ls
        async function copySessPath(el) {
            const head = el.closest('.sess-head');
            const st = sessPathStore[head ? head.dataset.sessKey : ''] || {};
            const path = el.dataset.kind === 'session' ? st.sessionDir : st.workdir;
            if (!path) {
                showToast('暂无可复制的路径', true);
                return;
            }
            if (await copyText(path)) showToast('已复制：' + path);
            else showToast('复制失败，请手动选择复制', true);
        }

        // 调度历史回放：时间列点一下就复制该条事件的完整时间戳（'YYYY-MM-DD HH:MM:SS'，含秒）。
        // 时间戳走本 store 按事件 id 取，不进 data-* 属性（防属性注入，同 sessPathStore）
        const dispTsStore = {};

        async function copyDispTime(eventId) {
            const ts = dispTsStore[eventId];
            if (!ts) {
                showToast('暂无可复制的时间', true);
                return;
            }
            if (await copyText(ts)) showToast('已复制：' + ts);
            else showToast('复制失败，请手动选择复制', true);
        }

        // #287 产物创建时间：Unix 秒 -> 'MM-DD HH:mm'（本地时区；mtime 为空时不显示）
        function formatMtime(ts) {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            const p = n => String(n).padStart(2, '0');
            return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
        }

        async function load() {
            const [reqRes, sessRes, artRes, agentRes, mcRes, confRes, rolesRes] = await Promise.all([
                fetch('/api/requirements', {cache: 'no-store'}),
                fetch('/api/sessions', {cache: 'no-store'}),
                fetch('/api/artifacts', {cache: 'no-store'}),
                fetch('/api/agent-sessions', {cache: 'no-store'}),
                fetch('/api/merge-conflicts', {cache: 'no-store'}),
                fetch('/api/confirmations', {cache: 'no-store'}),
                fetch('/api/roles', {cache: 'no-store'}),
            ]);
            items = await reqRes.json();
            sessionInfo = await sessRes.json().catch(() => ({}));
            artifactInfo = await artRes.json().catch(() => ({}));
            agentSessions = await agentRes.json().catch(() => ([]));
            // #404：角色显示名/顺序注入自 /api/roles（单一源 acp_shared.ROLE_CAPABILITIES）；
            // 请求失败降级为空数组 → 全角色显示原文（页面不报错）
            roleMeta = (await rolesRes.json().catch(() => [])) || [];
            mergeConflicts = {};
            (await mcRes.json().catch(() => ([]))).forEach(r => { mergeConflicts[r.req_id] = r; });
            confirmations = {};
            (await confRes.json().catch(() => ([]))).forEach(r => { confirmations[r.req_id] = r; });
            await renderNav();
            renderEditProjects();
            render();
            refreshProjectGitStatus();   // #5.2 首次加载按需拉一次 Git 状态（不随轮询刷新）
        }

        async function refreshAgentSessions() {
            const res = await fetch('/api/agent-sessions', {cache: 'no-store'});
            const rows = await res.json().catch(() => ([]));
            agentSessions = rows;
            // #421：更新服务端时间基准（server_now 为服务端同一时间点）——心跳/事件 age 统一用它算，
            // 浏览器时钟漂移/时区差异不影响判定与显示（缺失时回退本地时钟，仅影响基准精度）
            const sn = rows.length ? rows[0].server_now : null;
            if (typeof sn === 'number') agentServerClockOffsetMs = sn * 1000 - Date.now();
        }

        // 折叠态项目图标：字母头像（项目显示名首字符），样式由 CSS 统一（素雅浅灰底），展开态隐藏
        function navIconHtml(name) {
            const ch = (String(displayProjectName(name)).trim().charAt(0) || '?').toUpperCase();
            return `<span class="nav-icon" aria-hidden="true">${escapeHtml(ch)}</span>`;
        }

        // #5 项目行 hover 操作图标（编辑 / 移除接入）
        const PM_EDIT_ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';
        const PM_REMOVE_ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

        // 左侧项目导航：顶部固定「所有项目」项（展示全部需求），下方为接入自动开发的项目（/api/auto-projects 动态渲染）
        async function renderNav() {
            const res = await fetch('/api/auto-projects', {cache: 'no-store'});
            autoProjects = await res.json();
            // 刷新恢复的项目或需求可能已经被移除；数据加载完成后校验并安全降级。
            if (selectedProject !== ALL_PROJECTS && !autoProjects.includes(selectedProject)) {
                selectedProject = ALL_PROJECTS;
                selectedId = null;
                addMode = false;
            }
            if (selectedId && !items.some(i => i.id === selectedId
                && (selectedProject === ALL_PROJECTS || i.project === selectedProject))) {
                selectedId = null;
            }
            // 折叠态项目图标与名称共用同一行结构：展开态显示名称、隐藏图标，折叠态反之
            const allIcon = `<span class="nav-icon nav-icon-all" aria-hidden="true"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg></span>`;
            const statusPts = STATUS_PTS.map(s =>
                `<button type="button" class="nav-status-pt${selectedStatus === s ? ' active' : ''}" data-status="${s}" title="筛选：${STATUS_PT_LABEL[s]}" aria-label="${STATUS_PT_LABEL[s]}"></button>`).join('');
            const allItem = `<div class="nav-item${selectedProject === ALL_PROJECTS ? ' active' : ''}" data-project="${ALL_PROJECTS}" title="所有项目">${allIcon}<span class="nav-item-name">所有项目</span><span class="nav-status-points">${statusPts}</span></div>`;
            // 每个具体项目行右侧带操作钮：hover 显示编辑/移除接入（#5）与常显 + 进入聊天建卡（「所有项目」行不提供）
            const projectItems = autoProjects.map(p =>
                `<div class="nav-item${p === selectedProject ? ' active' : ''}" data-project="${escapeHtml(p)}" title="${escapeHtml(displayProjectName(p))}">
                    ${navIconHtml(p)}
                    <span class="nav-item-name">${escapeHtml(displayProjectName(p))}</span>
                    <button type="button" class="nav-edit-project" data-edit-project="${escapeAttr(p)}" title="编辑项目" aria-label="编辑项目 ${escapeAttr(displayProjectName(p))}">${PM_EDIT_ICON}</button>
                    <button type="button" class="nav-remove-project" data-remove-project="${escapeAttr(p)}" title="移除接入" aria-label="移除接入 ${escapeAttr(displayProjectName(p))}">${PM_REMOVE_ICON}</button>
                    <button type="button" class="nav-add-project" data-add-project="${escapeHtml(p)}" title="进入聊天建卡" aria-label="进入 ${escapeHtml(displayProjectName(p))} 聊天建卡">+</button>
                </div>`).join('');
            document.getElementById('navList').innerHTML = allItem + projectItems;
        }

        // 点击左侧项目/「所有项目」：切换中间列表；切到不同项时清空右栏选中，回到空状态。
        // 切换视图 = 重置状态筛选（「所有项目」是恢复全量的兜底锚点：点它一定回到全量；
        // 点当前已选中行同理——筛选激活时点它即取消筛选查看该视图全量）
        function selectProject(name) {
            if (name === selectedProject) {
                if (selectedStatus) {
                    selectedStatus = null;
                    renderNav();
                    render();
                }
                return;
            }
            selectedProject = name;
            selectedStatus = null;
            addMode = false;
            selectedId = null;
            selectedSid = null;
            selectedAgentSession = null;
            selectedDispatcher = false;
            selectedArtifact = null;
            selectedReqChat = false;
            renderNav();
            render();
            refreshProjectGitStatus();   // #5.2 切换项目时按需加载一次 Git 状态（不轮询）
        }

        // 点击项目行右侧 +：选中该项目并进入它的聊天建卡页（并行会话：不打断其它项目正在跑的一轮）
        function openChatFor(project) {
            if (!project) return;
            selectedProject = project;
            selectedStatus = null;  // 进入建卡页同样重置状态筛选（与 selectProject 一致）
            addMode = true;
            if (typeof interactionOpenDetail === 'function') interactionOpenDetail();
            selectedId = null;
            selectedSid = null;
            selectedAgentSession = null;
            selectedDispatcher = false;
            selectedArtifact = null;
            selectedReqChat = false;
            if (chatState && chatState.project !== project) {
                const old = chatState;
                chatState = null;
                chatEvents = [];
                chatLastId = 0;
                chatFetchAt = 0;
                if (['idle', 'running'].includes(old.status)) {
                    // 切走：running 轮次后台继续跑完；idle 进程回收，回来自动恢复
                    fetch(`/api/chat/${old.id}/detach`, {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({detached: true})
                    }).catch(() => {});
                }
            }
            renderNav();
            render();
            refreshProjectGitStatus();   // #5.2 项目切换（进入聊天建卡页同样属于切换项目）
            const t = document.getElementById('chatInput');
            if (t) t.focus();
        }

        // 折叠/展开左侧菜单栏：切换 .workspace.nav-collapsed class，状态存 localStorage（刷新保持）
        function toggleNav() {
            navCollapsed = !navCollapsed;
            localStorage.setItem('navCollapsed', navCollapsed ? '1' : '0');
            applyNavCollapsed();
        }
        function applyNavCollapsed() {
            document.querySelector('.workspace').classList.toggle('nav-collapsed', navCollapsed);
            const t = document.getElementById('navToggle');
            t.textContent = navCollapsed ? '»' : '«';
            t.title = navCollapsed ? '展开菜单栏' : '折叠菜单栏';
            applyLayoutWidths();  // #7：折叠清空 inline 宽度（CSS 48px 规则生效）、展开恢复拖动后的存储宽度
        }

        // ==================== #15 皮肤系统（深空玻璃默认 / 浅色）单点应用 ====================
        // 皮肤标识挂 <html> 的 data-skin（缺省 = 深色）；只换令牌取值，组件 DOM 与规则选择器不动。
        function applySkin(skin) {
            const light = skin === 'light';
            if (light) document.documentElement.dataset.skin = 'light';
            else delete document.documentElement.dataset.skin;
            const label = document.getElementById('skinToggleLabel');
            if (label) label.textContent = light ? '深色皮肤' : '浅色皮肤';
            const btn = document.getElementById('skinToggle');
            if (btn) { btn.title = light ? '切换深色皮肤' : '切换浅色皮肤'; btn.setAttribute('aria-label', light ? '切换深色皮肤' : '切换浅色皮肤'); }
            try { localStorage.setItem(SKIN_KEY, light ? 'light' : 'dark'); } catch (e) {}
            // #16 流体配色随皮肤即时刷新（未初始化时函数内早退，不抛）
            if (typeof applyFluidTokens === 'function') applyFluidTokens();
        }
        function toggleSkin() {
            applySkin(document.documentElement.dataset.skin === 'light' ? 'dark' : 'light');
        }
        function initSkin() {
            const btn = document.getElementById('skinToggle');
            if (btn) btn.addEventListener('click', toggleSkin);
            applySkin(localStorage.getItem(SKIN_KEY));
        }

        // ==================== #16 流体置换背景（原生 WebGL2 + GLSL，零外部依赖） ====================
        // 全屏 canvas #fluidBg（body 首子元素、fixed/inset:0/z-index:0/pointer-events:none）替代旧
        // 双层网格底纹：铺满视口、位于内容层 .page（z-index:1）之下、随窗口自适应。
        // 配色唯一落点 = board.py 令牌块（--fluid-*）；本模块经 getComputedStyle 读取，不硬编码色值
        // （applyFluidTokens 内的数值兜底仅「缺令牌时防抛」，不参与配色决策）。
        // 纯函数 parseCssColor / fluidRenderSize / fluidTokenNames / fluidReadTokens 由 node harness 断言。
        const FLUID_MAX_DPR = 1.5;    // 渲染分辨率钳制：DPR 上限
        const FLUID_MAX_EDGE = 1920;  // 渲染分辨率钳制：最长边上限
        const FLUID_VEIL_AMOUNT = 0.7;  // 流体与 --fluid-veil 的混底比例（保内容可读性）
        let fluidEl = null, fluidGl = null, fluidProg = null, fluidLoc = null;
        let fluidRaf = 0, fluidAnimated = false, fluidPaused = true, fluidLast = 0, fluidTime = 0;

        // 令牌名清单（自含字面量：供 node harness 独立提取；与 board.py 令牌块集合的一致性由
        // tests/test_fluid_background.py:H5 对源码文本比对，防改名漂移）
        function fluidTokenNames() {
            return ['--fluid-color-a', '--fluid-color-b', '--fluid-color-c',
                    '--fluid-veil', '--fluid-opacity'];
        }

        // CSS 颜色 → [r,g,b]（0..1）；支持 #rgb / #rrggbb / rgb() / rgba()，非法串返回 null（不抛）
        function parseCssColor(str) {
            if (typeof str !== 'string') return null;
            const s = str.trim();
            if (s.charAt(0) === '#') {
                const h = s.slice(1);
                if (!/^[0-9a-fA-F]+$/.test(h)) return null;
                if (h.length === 3) {
                    return [parseInt(h[0] + h[0], 16) / 255, parseInt(h[1] + h[1], 16) / 255,
                            parseInt(h[2] + h[2], 16) / 255];
                }
                if (h.length === 6) {
                    return [parseInt(h.slice(0, 2), 16) / 255, parseInt(h.slice(2, 4), 16) / 255,
                            parseInt(h.slice(4, 6), 16) / 255];
                }
                return null;
            }
            const lower = s.toLowerCase();
            if (lower.indexOf('rgb(') !== 0 && lower.indexOf('rgba(') !== 0) return null;
            const open = s.indexOf('('), close = s.lastIndexOf(')');
            if (open < 0 || close < open) return null;
            const parts = s.slice(open + 1, close).split(',');
            if (parts.length < 3) return null;
            const out = [];
            for (let i = 0; i < 3; i++) {
                const v = parseFloat(parts[i]);
                if (isNaN(v)) return null;
                out.push(Math.max(0, Math.min(255, v)) / 255);
            }
            return out;
        }

        // 渲染缓冲区尺寸：DPR 上限与最长边上限双钳制；输入非法/为 0 时也保证结果 ≥ 1
        function fluidRenderSize(cssW, cssH, dpr, maxDpr, maxEdge) {
            const w = Math.max(1, Math.round(cssW || 0));
            const h = Math.max(1, Math.round(cssH || 0));
            const d = Math.min(Math.max(dpr || 1, 1), maxDpr);
            let rw = Math.max(1, Math.round(w * d));
            let rh = Math.max(1, Math.round(h * d));
            const edge = Math.max(rw, rh);
            if (edge > maxEdge) {
                const k = maxEdge / edge;
                rw = Math.max(1, Math.round(rw * k));
                rh = Math.max(1, Math.round(rh * k));
            }
            return {w: rw, h: rh};
        }

        // 读取流体令牌：去空白后返回；任一缺失回退 null（不抛），由调用方决定兜底
        function fluidReadTokens(cs) {
            const read = (name) => {
                try {
                    const v = cs ? cs.getPropertyValue(name) : '';
                    return v && v.trim() ? v.trim() : null;
                } catch (e) { return null; }
            };
            return {a: read('--fluid-color-a'), b: read('--fluid-color-b'), c: read('--fluid-color-c'),
                    veil: read('--fluid-veil'), opacity: read('--fluid-opacity')};
        }

        // 顶点：无属性缓冲的全屏三角形（gl_VertexID 推导）
        const FLUID_VS = `#version 300 es
out vec2 vUv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    vUv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;
        // 片元：4 阶 value-noise FBM + 两次域扭曲产出流体置换场，三色插值 + veil 混底 + 暗角 + 抖动
        const FLUID_FS = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform float uTime;
uniform vec2 uRes;
uniform vec3 uColorA;
uniform vec3 uColorB;
uniform vec3 uColorC;
uniform vec3 uVeil;
uniform float uVeilAmount;

float fhash(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}
float vnoise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    float a = fhash(i), b = fhash(i + vec2(1.0, 0.0));
    float c = fhash(i + vec2(0.0, 1.0)), d = fhash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}
float fbm(vec2 p) {
    float v = 0.0, amp = 0.5;
    mat2 rot = mat2(0.8, 0.6, -0.6, 0.8);
    for (int i = 0; i < 4; i++) {
        v += amp * vnoise(p);
        p = rot * p * 2.02 + 11.3;
        amp *= 0.5;
    }
    return v;
}
void main() {
    vec2 uv = vUv;
    float aspect = uRes.x / max(uRes.y, 1.0);
    vec2 p = vec2(uv.x * aspect, uv.y) * 3.0;
    float t = uTime * 0.06;
    vec2 q = vec2(fbm(p + vec2(0.0, t)), fbm(p + vec2(5.2, 1.3) - t));
    vec2 r = vec2(fbm(p + 2.0 * q + vec2(1.7, 9.2) + t * 1.3),
                  fbm(p + 2.0 * q + vec2(8.3, 2.8) - t * 1.1));
    float flow = clamp(fbm(p + 2.5 * r), 0.0, 1.0);
    vec3 col = mix(uColorA, uColorB, smoothstep(0.12, 0.78, flow));
    col = mix(col, uColorC, smoothstep(0.74, 1.0, flow) * 0.5);
    float vig = smoothstep(1.2, 0.25, length((uv - 0.5) * vec2(aspect, 1.0)));
    col *= mix(0.82, 1.06, vig);
    col = mix(uVeil, col, uVeilAmount);
    col += (fhash(uv * uRes + fract(uTime)) - 0.5) / 255.0;
    outColor = vec4(col, 1.0);
}`;

        function fluidCompile(gl, type, src) {
            const sh = gl.createShader(type);
            gl.shaderSource(sh, src);
            gl.compileShader(sh);
            if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) { gl.deleteShader(sh); return null; }
            return sh;
        }

        // 建 program + uniform location（初始化与上下文恢复共用；恢复时必须重跑，否则对象的
        // GL 句柄已随丢失的上下文失效，继续使用只产生 INVALID_OPERATION）
        function fluidBuildProgram(gl) {
            const vs = fluidCompile(gl, gl.VERTEX_SHADER, FLUID_VS);
            const fs = fluidCompile(gl, gl.FRAGMENT_SHADER, FLUID_FS);
            if (!vs || !fs) return null;
            const prog = gl.createProgram();
            gl.attachShader(prog, vs);
            gl.attachShader(prog, fs);
            gl.linkProgram(prog);
            if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
            return {prog: prog, loc: {
                uTime: gl.getUniformLocation(prog, 'uTime'),
                uRes: gl.getUniformLocation(prog, 'uRes'),
                uColorA: gl.getUniformLocation(prog, 'uColorA'),
                uColorB: gl.getUniformLocation(prog, 'uColorB'),
                uColorC: gl.getUniformLocation(prog, 'uColorC'),
                uVeil: gl.getUniformLocation(prog, 'uVeil'),
                uVeilAmount: gl.getUniformLocation(prog, 'uVeilAmount'),
            }};
        }

        // 配色刷新：由 initFluidBg 与 applySkin 调用；未初始化（无 WebGL2 / 初始化失败）时早退，
        // 防止首屏 applySkin 先于 initFluidBg 触发时抛错冒泡（阻断引导区后续初始化）。
        function applyFluidTokens() {
            if (!fluidGl) return;
            const t = fluidReadTokens(getComputedStyle(document.documentElement));
            const opacity = parseFloat(t.opacity);
            if (fluidEl && opacity >= 0 && opacity <= 1) fluidEl.style.opacity = String(opacity);
            const gl = fluidGl, L = fluidLoc;
            gl.useProgram(fluidProg);   // uniform location 仅在当前 program 上有效（否则 INVALID_OPERATION 静默不生效）
            const setCol = (loc, raw, fallback) => {
                const c = parseCssColor(raw) || fallback;
                gl.uniform3f(loc, c[0], c[1], c[2]);
            };
            setCol(L.uColorA, t.a, [0.02, 0.13, 0.24]);
            setCol(L.uColorB, t.b, [0.04, 0.31, 0.48]);
            setCol(L.uColorC, t.c, [0.13, 0.83, 0.93]);
            setCol(L.uVeil, t.veil, [0.02, 0.05, 0.09]);
            gl.uniform1f(L.uVeilAmount, FLUID_VEIL_AMOUNT);
            // 静态单帧模式（reduced-motion、循环已停）下切皮肤须立即重绘，否则 canvas 仍显示
            // 上一档配色；动画模式由下一帧自然刷新，无需在此重绘。
            if (!fluidAnimated) fluidRenderFrame(fluidTime);
        }

        function fluidResize() {
            if (!fluidGl || !fluidEl) return;
            const size = fluidRenderSize(window.innerWidth, window.innerHeight,
                                         window.devicePixelRatio || 1, FLUID_MAX_DPR, FLUID_MAX_EDGE);
            if (fluidEl.width !== size.w || fluidEl.height !== size.h) {
                fluidEl.width = size.w;
                fluidEl.height = size.h;
            }
            fluidGl.viewport(0, 0, size.w, size.h);
        }

        function fluidRenderFrame(t) {
            if (!fluidGl) return;
            try {
                const gl = fluidGl;
                gl.useProgram(fluidProg);
                gl.uniform1f(fluidLoc.uTime, t);
                gl.uniform2f(fluidLoc.uRes, fluidEl.width || 1, fluidEl.height || 1);
                gl.drawArrays(gl.TRIANGLES, 0, 3);
            } catch (e) { /* 上下文丢失等：静默保留静态渐变底 */ }
        }

        function fluidLoop(ts) {
            fluidRaf = 0;
            if (!fluidGl || fluidPaused) return;
            const now = (typeof ts === 'number') ? ts : performance.now();
            if (fluidLast) fluidTime += Math.min(Math.max(now - fluidLast, 0), 100) / 1000;
            fluidLast = now;
            fluidRenderFrame(fluidTime);
            fluidRaf = requestAnimationFrame(fluidLoop);
        }

        // 页签隐藏/回前台暂停与恢复（仅动画模式生效；静态单帧模式保持静止）
        function fluidSetPaused(paused) {
            if (!fluidAnimated) return;
            if (paused) {
                fluidPaused = true;
                if (fluidRaf) { cancelAnimationFrame(fluidRaf); fluidRaf = 0; }
            } else if (fluidPaused) {
                fluidPaused = false;
                fluidLast = 0;
                fluidRaf = requestAnimationFrame(fluidLoop);
            }
        }

        // 初始化：reduced-motion 渲染单帧静态流体；WebGL2 不可用/编译失败/异常 → 令牌化静态渐变底，
        // 全程无控制台输出。上下文丢失 → preventDefault + 停循环（丢失态 canvas 无图像，露出其
        // background-image 的静态渐变底）；恢复 → 经 fluidBuildProgram 重建 program/location 后继续。
        function initFluidBg() {
            fluidEl = document.getElementById('fluidBg');
            if (!fluidEl) return;
            let reduced = false;
            try {
                reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
            } catch (e) { reduced = false; }
            try {
                // preserveDrawingBuffer 仅静态单帧模式（reduced-motion）需要：该模式不跑循环，
                // 单帧内容须在合成后仍可见；动画模式每帧重绘，保持默认走合成快路径。
                const gl = fluidEl.getContext('webgl2', {
                    antialias: false, alpha: false, depth: false, stencil: false,
                    preserveDrawingBuffer: reduced});
                if (!gl) return;   // WebGL2 不可用 → 静态渐变底
                const built = fluidBuildProgram(gl);
                if (!built) return;   // 着色器编译 / program 链接失败 → 静态渐变底
                fluidGl = gl;
                fluidProg = built.prog;
                fluidLoc = built.loc;
                fluidEl.addEventListener('webglcontextlost', (ev) => {
                    ev.preventDefault();
                    fluidPaused = true;                       // 停循环，露出令牌化渐变底
                    if (fluidRaf) { cancelAnimationFrame(fluidRaf); fluidRaf = 0; }
                });
                fluidEl.addEventListener('webglcontextrestored', () => {
                    try {
                        const again = fluidBuildProgram(fluidGl);   // 上下文丢失后旧 GL 对象已失效，必须重建
                        if (!again) return;                        // 重建失败：停在静态渐变底
                        fluidProg = again.prog;
                        fluidLoc = again.loc;
                        fluidResize();
                        applyFluidTokens();
                        if (fluidAnimated) { fluidPaused = true; fluidSetPaused(false); }
                        else fluidRenderFrame(fluidTime);
                    } catch (e) { /* 保持回退态，不产生控制台报错 */ }
                });
                fluidResize();
                applyFluidTokens();
                window.addEventListener('resize', () => {
                    fluidResize();
                    if (!fluidAnimated) fluidRenderFrame(fluidTime);   // 静态单帧随尺寸重绘
                });
                if (reduced) { fluidRenderFrame(fluidTime); return; }  // 停用动画：单帧静态流体
                fluidAnimated = true;
                fluidPaused = false;
                fluidLast = 0;
                fluidRaf = requestAnimationFrame(fluidLoop);
            } catch (e) {
                fluidGl = null;   // 初始化失败：保持静态渐变底，不产生控制台报错
            }
        }


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
            return `
            <div class="card ${i.status}${selected ? ' selected' : ''}${i.id === flashId ? ' flash' : ''}${collapsed ? ' collapsed' : ''}">
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

        // ==================== #3 项目信息条（GET /api/project-stats） ====================
        // 跟随左导航选中项目：render() 与 autoRefresh() 双挂点刷新；签名短路避免重复写 DOM；
        // 过期响应丢弃（快速切项目防串台）；拉取失败保留上次渲染（fail-safe，不炸主页）。
        let projectBarSig = null;
        function formatTokens(n) {
            const v = (typeof n === 'number' && isFinite(n) && n > 0) ? Math.round(n) : 0;
            if (v < 1000) return String(v);
            if (v < 1000000) return (v / 1000).toFixed(1) + 'k';
            return (v / 1000000).toFixed(2) + 'M';
        }
        function formatSuccessRate(r) {
            if (r === null || r === undefined) return '—';
            const v = Number(r);
            if (!isFinite(v)) return '—';
            return Number((v * 100).toFixed(1)) + '%';
        }
        // 属性位转义：escapeHtml 不转引号，title= 等属性位补引号转义
        function escapeAttr(s) {
            return escapeHtml(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }
        // 统计项口径 tooltip（口径见 PRD：成功率分母含已废弃；打回=reject；总消耗=上下文峰值口径）
        const PB_STAT_TIP = {
            total: '全部状态的需求数（含暂缓 / 已废弃）',
            doing: '状态为「进行中」的需求数',
            done: '状态为「已完成」的需求数',
            rate: '完成 ÷（完成 + 已废弃）；无完结卡时显示 —',
            reject: '评审结论为「打回」（verdict=reject）的计数（按需求归属项目统计）',
            tokens: 'Σ agent_runs.context_tokens（各会话上下文峰值合计）——上下文峰值口径，非完整消耗（P4 换真实计量）'
        };
        function projectBarHtml(d) {
            const st = (d && d.stats) || {};
            const isAll = !d || d.project === 'all';
            const name = isAll ? '所有项目' : displayProjectName(d.project);
            let left = `<div class="pb-name">${escapeHtml(name)}</div>`;
            if (!isAll) {
                const goal = String(d.goal || '').trim();
                left += `<div class="pb-goal">目标：${escapeHtml(goal || '未填写')}</div>`;
            }
            const paths = (d && Array.isArray(d.paths)) ? d.paths : [];
            if (isAll) {
                const cnt = (d && typeof d.project_count === 'number') ? d.project_count : 0;
                left += `<div class="pb-paths">项目数：${cnt}</div>`;
            } else if (paths.length) {
                const allPaths = paths.join(' · ');
                left += `<div class="pb-paths" title="${escapeAttr(allPaths)}">路径：${escapeHtml(allPaths)}</div>`;
            } else {
                left += '<div class="pb-paths">路径：未配置</div>';
            }
            const num = v => (typeof v === 'number' && isFinite(v)) ? String(Math.round(v)) : '0';
            // #21 指标数字配色：非零值才加修饰类（颜色只由皮肤令牌定，令牌块外零字面量）；
            // 零值 / 「0%」 / 「—」解析为 0 或 NaN，均不加类，保持 .pb-stat 中性色
            const stat = (label, value, tip, tone) => {
                const text = String(value);
                const cls = (parseFloat(text) > 0) ? ' ' + tone : '';
                return `<span class="pb-stat${cls}" title="${escapeAttr(tip)}"><b>${escapeHtml(text)}</b>${escapeHtml(label)}</span>`;
            };
            const stats = stat('任务总数', num(st.total), PB_STAT_TIP.total, 'pb-stat-total')
                + stat('进行中', num(st.doing), PB_STAT_TIP.doing, 'pb-stat-doing')
                + stat('完成', num(st.done), PB_STAT_TIP.done, 'pb-stat-done')
                + stat('成功率', formatSuccessRate(st.success_rate), PB_STAT_TIP.rate, 'pb-stat-rate')
                + stat('打回次数', num(st.reject), PB_STAT_TIP.reject, 'pb-stat-reject')
                + stat('总消耗', formatTokens(st.ctx_tokens) + ' tokens', PB_STAT_TIP.tokens, 'pb-stat-tokens');
            // 最右端机器人图标位（#4）：具体项目 = 可点 button（打开模型调参抽屉）；
            // 「所有项目」= 禁用态 + 标题提示（title 与 aria 与 PRD 文案一致）
            const robotIcon = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="8" width="16" height="11" rx="2"/><path d="M12 8V5"/><circle cx="12" cy="4" r="1"/><circle cx="9" cy="13" r="1"/><circle cx="15" cy="13" r="1"/></svg>';
            const robot = isAll
                ? `<span class="pb-robot" aria-disabled="true" aria-label="模型调参" title="模型调参需选中具体项目">${robotIcon}</span>`
                : `<button type="button" class="pb-robot on" aria-label="模型调参" title="模型调参">${robotIcon}</button>`;
            return `<div class="pb-left">${left}</div>`
                + `<div class="pb-right"><div class="pb-stats">${stats}</div>${robot}</div>`;
        }
        function renderProjectBar(d) {
            const html = projectBarHtml(d);
            if (html === projectBarSig) return;
            projectBarSig = html;
            const bar = document.getElementById('projectBar');
            if (bar) bar.innerHTML = html;
        }
        async function refreshProjectBar() {
            const sel = selectedProject;
            const param = sel === ALL_PROJECTS ? 'all' : sel;
            try {
                const res = await fetch('/api/project-stats?project=' + encodeURIComponent(param), {cache: 'no-store'});
                const d = await res.json();
                if (selectedProject !== sel) return;  // 过期响应丢弃（快速切项目防串台）
                renderProjectBar(d);
                // #4 切项目联动：抽屉所开项目与当前项目不一致 → 自动关闭（防串台）
                if (robotProject && robotProject !== sel) closeRobotDrawer();
            } catch (e) { /* 拉取失败保留上次渲染，下一轮再试（fail-safe） */ }
        }

        // ==================== #5.2 项目页 Git 状态条 / 拉取 / 推送 ====================
        // 状态按需加载：首次加载与切换项目各拉一次（不随 15s 轮询刷新，避免无谓拉取）；
        // 拉取 / 推送成功后主动刷新一次。整体失败保留错误文案与按钮，仍允许手动重试。
        let projectGitRepos = null;      // 最近一次 POST /git/status 的 repos
        let projectGitError = '';        // 状态整体错误文案（逐仓库错误在 repos[].error 里）
        let projectGitLoading = false;
        let projectGitBusy = false;

        function renderProjectGitBar() {
            const el = document.getElementById('projectGitBar');
            if (!el) return;
            if (selectedProject === ALL_PROJECTS) {
                el.hidden = true;
                el.innerHTML = '';
                return;
            }
            el.hidden = false;
            if (projectGitLoading && !projectGitRepos) {
                el.innerHTML = '<div class="pb-git"><span class="pb-git-label">Git</span>'
                    + '<span class="pb-git-repo">状态加载中…</span></div>';
                return;
            }
            el.innerHTML = gitStatusBarHtml(projectGitRepos || [], projectGitError, {busy: projectGitBusy});
        }

        async function refreshProjectGitStatus() {
            if (selectedProject === ALL_PROJECTS) {
                projectGitRepos = null;
                projectGitError = '';
                renderProjectGitBar();
                return;
            }
            const sel = selectedProject;
            projectGitLoading = true;
            renderProjectGitBar();
            try {
                const res = await fetch('/api/projects/' + encodeURIComponent(sel) + '/git/status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}',
                });
                const d = await res.json().catch(() => ({}));
                if (selectedProject !== sel) return;   // 过期响应丢弃（快速切项目防串台）
                if (!res.ok) {
                    projectGitRepos = null;
                    projectGitError = d.error || ('状态加载失败（HTTP ' + res.status + '）');
                } else {
                    projectGitRepos = Array.isArray(d.repos) ? d.repos : [];
                    projectGitError = d.ok === false ? '部分仓库状态获取失败，详见上方仓库提示' : '';
                }
            } catch (e) {
                if (selectedProject !== sel) return;
                projectGitRepos = null;
                projectGitError = '状态加载失败：网络错误';
            } finally {
                if (selectedProject === sel) {
                    projectGitLoading = false;
                    renderProjectGitBar();
                }
            }
        }

        async function runProjectGitOp(kind) {
            if (selectedProject === ALL_PROJECTS || projectGitBusy) return;
            const project = selectedProject;
            const push = kind === 'push';
            // push 必须先二次确认；用户取消 = 静默返回（不弹 toast、不发请求）
            if (push && !window.confirm(gitPushConfirmText(project, projectGitRepos || []))) return;
            projectGitBusy = true;
            renderProjectGitBar();
            let res, d;
            try {
                res = await fetch('/api/projects/' + encodeURIComponent(project) + '/git/' + (push ? 'push' : 'pull'), {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(push ? {confirm: true} : {}),
                });
                d = await res.json().catch(() => ({}));
            } catch (e) {
                projectGitBusy = false;
                renderProjectGitBar();
                showToast((push ? '推送' : '拉取') + '失败：网络错误', true);
                return;
            }
            projectGitBusy = false;
            if (selectedProject !== project) { renderProjectGitBar(); return; }
            // 请求级失败（404 项目不存在 / 400 body 非法 / push 缺 confirm）才走这里
            if (!res.ok) {
                const msg = d.error || ('HTTP ' + res.status);
                projectGitError = msg;
                renderProjectGitBar();
                showToast((push ? '推送' : '拉取') + '失败：' + msg, true);
                return;
            }
            const out = gitOpResultText(kind, d);
            projectGitError = out.ok ? '' : out.text;
            renderProjectGitBar();
            showToast(out.text, !out.ok);
            await refreshProjectGitStatus();   // 无论成败都刷新，最终状态以服务端为准
        }

        // ==================== #5 项目管理（新增/编辑/移除接入） ====================
        // 后端三写端点 + GET 预填；pm 纯函数（pmNameValid/pmPathsToArray/projectStatsText/
        // projectRemoveBlocked/projectChecklistHtml）供 node harness 断言，接线函数不纳入 harness。
        let pmEditingName = null;   // null = 新增；字符串 = 编辑中的项目名（键不可改）
        let pmBaseMapping = false;  // 编辑态 base_branch 为 dict 映射形态（#369）→ 只读提示、提交不含该字段
        let pmRemovingName = null;

        // 项目名校验：非空 / ≤maxLen / ^[a-z0-9][a-z0-9-]*$ / 不在既有键集（existing = /api/auto-projects 键）
        function pmNameValid(name, existing, maxLen) {
            const v = String(name == null ? '' : name).trim();
            if (!v || v.length > (maxLen || 64)) return false;
            if (!/^[a-z0-9][a-z0-9-]*$/.test(v)) return false;
            return !(Array.isArray(existing) && existing.includes(v));
        }

        // paths 多行文本 → 数组（按行拆分、trim、去空行）
        function pmPathsToArray(text) {
            return String(text == null ? '' : text).split(/\r?\n/).map(s => s.trim()).filter(Boolean);
        }

        // 移除确认弹窗统计文案：共 N 张 + 各状态计数（已知状态按 PM_STATUS_ORDER 排序、未知状态
        // 追加尾部兜底；标签取后端注入的 STATUS_LABEL，缺失回退原文）
        const PM_STATUS_ORDER = ['doing', 'open', 'later', 'done', 'dropped'];
        function projectStatsText(byStatus) {
            const by = byStatus || {};
            const keys = Object.keys(by).filter(k => (Number(by[k]) || 0) > 0);
            const total = keys.reduce((n, k) => n + (Number(by[k]) || 0), 0);
            const ordered = PM_STATUS_ORDER.filter(s => keys.includes(s))
                .concat(keys.filter(s => !PM_STATUS_ORDER.includes(s)));
            const parts = ordered.map(s => `${(STATUS_LABEL && STATUS_LABEL[s]) || s} ${by[s]}`);
            return `共 ${total} 张${parts.length ? '：' + parts.join(' / ') : ''}`;
        }

        // doing 卡阻断判定（PRD：存在进行中的卡时禁止移除）
        function projectRemoveBlocked(byStatus) {
            return !!((byStatus || {}).doing > 0);
        }

        // 接入成功 checklist：AGENTS.md 协作约定段模板（一键复制）+ kimi 目录信任命令
        function projectChecklistHtml(project, firstPath) {
            const p = String(project || '');
            const agentsMd = [
                '## 需求看板协作约定',
                '',
                '本项目的需求看板运行在 http://127.0.0.1:8899（若未运行可跳过，不要报错）。',
                '',
                '1. 暂缓需求建卡：POST /api/requirements，project 填本项目目录名，status 必须传 "later"。',
                '2. 开始开发标进行中：PATCH 状态为 doing。',
                '3. 完成标已完成：按标题语义匹配后 PATCH 为 done；匹配不确定必须先问用户。',
                `4. 改状态带请求头 X-Actor: agent:${p}:<需求号>。`,
                '5. notes 只写要做什么，不写讨论过程。',
            ].join('\n');
            // kimi 目录信任命令：路径加单引号包裹（含空格安全，本机项目路径即含空格）；内嵌单引号按
            // shell 惯例转义拼接。引号/反斜杠用 fromCharCode 构造（沿 board.py 内嵌 JS 先例）：
            // 三引号转义层与 harness 提取层在此写法下结果一致。
            const SQ = String.fromCharCode(39), BS = String.fromCharCode(92);
            const q = String(firstPath || '').split(SQ).join(SQ + BS + SQ + SQ);
            const cmd = `cd '${q}' && KIMI_CODE_HOME=~/.kimi-code-deepseek kimi`;
            return `<h3>接入成功</h3>
                <p class="pm-check-lead">把协作约定段加进项目 AGENTS.md，并按需完成一次性目录信任：</p>
                <div class="pm-check-block">
                    <div class="pm-check-head"><span>AGENTS.md 协作约定段</span>
                        <button type="button" class="btn" id="pmCopyBtn">复制</button></div>
                    <pre id="pmAgentsMd">${escapeHtml(agentsMd)}</pre>
                </div>
                <div class="pm-check-block">
                    <div class="pm-check-head"><span>kimi 目录信任（一次性）</span></div>
                    <pre>${escapeHtml(cmd)}</pre>
                </div>
                <div class="modal-btns"><button class="btn btn-primary" id="pmDone">完成</button></div>`;
        }

        // ---------- #5.1 Git 配置 / 项目页拉取推送纯函数（node harness 断言；接线函数不纳入 harness） ----------
        // git_sync 三个枚举的规范值（与 dispatcher 消费值逐字一致；「不拉取 / 不推送」统一是 off，非 none）
        const PM_GIT_SYNC_VALUES = {
            before_card: ['off', 'fetch', 'pull'],
            on_done: ['off', 'auto', 'manual'],
            diverge: ['ff-only', 'overlap-check', 'stop'],
        };
        const PM_GIT_SYNC_DEFAULTS = {before_card: 'off', on_done: 'off', diverge: 'overlap-check', timeout: 120};

        // 「选择文件夹」成功后把路径追加进 paths 文本（去空行 / 去重，每行一条绝对路径）
        function pmPathsAppend(text, newPath) {
            const path = String(newPath == null ? '' : newPath).trim();
            const lines = String(text == null ? '' : text).split(/\r?\n/).map(s => s.trim()).filter(Boolean);
            if (path && !lines.includes(path)) lines.push(path);
            return lines.join('\n');
        }

        // git-detect 响应 → 表单回填（首个 git 仓库的当前分支 / 远端 / URL + 每仓库当前分支 + 非 git 提示）
        function pmDetectFill(repos) {
            const rows = Array.isArray(repos) ? repos.filter(r => r && typeof r === 'object') : [];
            const gitRows = rows.filter(r => r.is_git);
            const first = gitRows.length ? gitRows[0] : null;
            const remotes = (first && Array.isArray(first.remotes)) ? first.remotes.filter(r => r && r.name) : [];
            const current = first ? String(first.current_branch || '').trim() : '';
            const base = first ? (String(first.base_branch || '').trim() || current) : '';
            const lines = rows.map(r => {
                const name = String(r.name || r.path || '').trim() || '未命名仓库';
                if (!r.is_git) return `${name}：非 git 仓库（请在路径中填写含 .git 的仓库根目录）`;
                const branch = String(r.current_branch || r.base_branch || '').trim();
                return `${name}：当前分支 ${branch || '未识别'}`;
            });
            const nonGit = rows.filter(r => !r.is_git).map(r => String(r.path || r.name || '').trim()).filter(Boolean);
            const errors = rows.filter(r => r.error).map(r => String(r.error));
            return {
                base_branch: base,
                current_branch: current,
                remote: remotes.length ? String(remotes[0].name) : '',
                url: remotes.length ? String(remotes[0].url || '') : '',
                remotes: remotes.map(r => ({name: String(r.name), url: String(r.url || '')})),
                line: lines.join('\n'),
                non_git: nonGit,
                errors: errors,
                ok: gitRows.length > 0,
            };
        }

        // 同步策略表单值 → git_sync 提交对象（枚举白名单校验；非法/缺失回落编辑前值与默认值；timeout 保序）
        function pmSyncForm(values, base) {
            const v = values || {};
            const b = (base && typeof base === 'object') ? base : {};
            const alias = {none: 'off'};   // 兼容别名：none ≡ off（对外契约统一写 off）
            const norm = x => {
                const s = (x == null) ? '' : String(x);
                return alias[s] || s;
            };
            const pick = key => {
                const allowed = PM_GIT_SYNC_VALUES[key] || [];
                const fromForm = norm(v[key]);
                if (allowed.includes(fromForm)) return fromForm;
                const fromBase = norm(b[key]);
                if (allowed.includes(fromBase)) return fromBase;
                return PM_GIT_SYNC_DEFAULTS[key];
            };
            const rawTimeout = (v.timeout != null && v.timeout !== '') ? v.timeout : b.timeout;
            const t = Math.round(Number(rawTimeout));
            const timeout = (isFinite(t) && t > 0) ? t : PM_GIT_SYNC_DEFAULTS.timeout;
            return {before_card: pick('before_card'), on_done: pick('on_done'), diverge: pick('diverge'), timeout: timeout};
        }

        // 凭据表单 → POST /git/credential body（有新密钥才 save；已配置且留空 = 只 test；密码绝不回填旧值）
        function pmCredentialBody(kind, username, password, sshKey, url, configured) {
            const k = (kind === 'ssh' || kind === 'system') ? kind : 'token';
            const body = {kind: k, url: String(url == null ? '' : url).trim()};
            const user = String(username == null ? '' : username).trim();
            const pw = String(password == null ? '' : password);
            const key = String(sshKey == null ? '' : sshKey).trim();
            if (k === 'token' && user) body.username = user;
            if (k === 'ssh' && key) body.ssh_key = key;
            if (k === 'system') body.action = 'test';                 // 系统凭据无密钥：保存动作退化为连通性测试
            else if (pw || key) body.action = 'save';                 // 有新密钥才 save
            else body.action = configured ? 'test' : 'save';          // 留空且已配置 = 测试；未配置交给后端报缺失
            if (k === 'token' && pw) body.password = pw;
            return body;
        }

        // 凭据状态文案（GET /git/credential 的 state；永不包含密码）
        function pmCredentialStateText(state) {
            const st = state || {};
            if (!st.configured) return '未配置（使用本机 git 配置）';
            const kind = st.kind === 'ssh' ? 'SSH key' : (st.kind === 'system' ? '系统凭据' : '用户名+令牌');
            const who = (st.kind === 'token' && st.username) ? '（' + st.username + '）' : '';
            return '已保存：' + kind + who;
        }

        // 单仓库状态文案：仓库名 + 分支 + ↑ahead ↓behind + 未提交数 + 远端（null 一律降级显示，不抛）
        function gitRepoStatusText(repo) {
            const r = repo || {};
            const name = String(r.name || '').trim();
            const prefix = name ? name + '：' : '';
            if (r.error) return prefix + String(r.error);
            const branch = String(r.branch || r.current_branch || '').trim() || '未知分支（detached）';
            const num = v => (typeof v === 'number' && isFinite(v)) ? Math.max(0, Math.round(v)) : null;
            const ahead = num(r.ahead);
            const behind = num(r.behind);
            const ab = (ahead === null && behind === null)
                ? '无远端跟踪'
                : '↑' + (ahead === null ? '—' : ahead) + ' ↓' + (behind === null ? '—' : behind);
            let dirtyText = '工作区干净';
            if (typeof r.dirty === 'boolean') dirtyText = r.dirty ? '有未提交改动' : '工作区干净';
            else {
                const d = num(r.dirty);
                if (d) dirtyText = d + ' 处未提交';
            }
            const remote = String(r.remote || '').trim() || '未绑定远端';
            return prefix + [branch, ab, dirtyText, remote].join(' · ');
        }

        // 项目页 Git 状态条 HTML（每仓库一行状态 + 拉取 / 推送按钮；errorText 为整体错误，busy 时按钮禁用）
        function gitStatusBarHtml(repos, errorText, opts) {
            const o = opts || {};
            const rows = Array.isArray(repos) ? repos.filter(Boolean) : [];
            const items = rows.map(r => {
                const warn = r.error || r.dirty || (typeof r.ahead === 'number' && r.ahead > 0)
                    || (typeof r.behind === 'number' && r.behind > 0);
                const name = String(r.name || '');
                return `<span class="pb-git-repo${warn ? ' pb-git-warn' : ''}"`
                    + `${name ? ` data-git-repo="${escapeAttr(name)}"` : ''}>${escapeHtml(gitRepoStatusText(r))}</span>`;
            }).join('');
            const err = errorText ? `<span class="pb-git-err">${escapeHtml(errorText)}</span>` : '';
            const busy = !!o.busy;
            const actions = o.actions === false ? '' : '<span class="pb-git-actions">'
                + `<button type="button" class="btn btn-sm" data-git-op="pull"${busy ? ' disabled' : ''} title="拉取当前项目全部仓库（按项目同步策略）">拉取</button>`
                + `<button type="button" class="btn btn-sm" data-git-op="push"${busy ? ' disabled' : ''} title="推送当前项目全部仓库（需二次确认）">推送</button>`
                + '</span>';
            return '<div class="pb-git"><span class="pb-git-label">Git</span>'
                + (items || '<span class="pb-git-repo">暂无仓库状态</span>') + err + actions + '</div>';
        }

        // push 二次确认文案（含项目名 / 仓库清单 / 未提交提醒；confirm=false 时调用方静默返回）
        function gitPushConfirmText(project, repos) {
            const rows = Array.isArray(repos) ? repos.filter(Boolean) : [];
            const list = rows.map(r => String(r.name || '')).filter(Boolean);
            const name = String(project || '').trim() || '当前项目';
            const target = list.length ? list.join('、') : '全部仓库';
            const dirty = rows.some(r => (typeof r.dirty === 'number' && r.dirty > 0) || r.dirty === true);
            return `确认把项目「${name}」的 ${target} 推送到远端？`
                + (dirty ? '\n\n注意：存在未提交改动，未提交内容不会被推送。' : '')
                + '\n\n推送会改写远端分支，请确认本地已通过测试。';
        }

        // 拉取 / 推送响应 → toast 文案（HTTP 200 + ok:false 的逐仓库失败也在此展示，不当请求级错误）
        function gitOpResultText(kind, d) {
            const push = kind === 'push';
            const label = push ? '推送' : '拉取';
            const rows = Array.isArray(d && d.repos) ? d.repos.filter(Boolean) : [];
            if (d && d.error) return {ok: false, text: label + '失败：' + String(d.error)};
            const failedStatus = ['failed', 'rejected', 'conflict', 'behind'];
            const failed = rows.filter(r => failedStatus.includes(String(r.status || '')));
            const conflicts = rows.filter(r => r.status === 'conflict'
                || (Array.isArray(r.conflicts) && r.conflicts.length > 0));
            if (failed.length || conflicts.length) {
                const seen = [];
                const detail = failed.concat(conflicts.filter(r => !failed.includes(r))).map(r => {
                    const nm = String(r.name || '仓库');
                    if (seen.includes(nm)) return '';
                    seen.push(nm);
                    return nm + '：' + String(r.message || r.error || '失败');
                }).filter(Boolean).join('；');
                const guide = conflicts.length ? '请先解决冲突后重试。' : '请检查远端地址 / 凭据与本地改动后重试。';
                return {ok: false, text: label + '失败：' + detail + ' ' + guide};
            }
            if (d && d.ok === false) return {ok: false, text: label + '失败：后端返回失败'};
            const done = rows.map(r => {
                const nm = String(r.name || '仓库');
                return nm + '：' + (String(r.message || '').trim() || String(r.status || '完成'));
            }).join('；');
            return {ok: true, text: label + '完成' + (done ? '：' + done : '')};
        }

        function pmSetError(msg) {
            const el = document.getElementById('pmError');
            el.textContent = msg || '';
            el.hidden = !msg;
        }

        function pmCopyAgentsMd() {
            const el = document.getElementById('pmAgentsMd');
            const text = el ? el.textContent : '';
            const fail = () => showToast('复制失败，请手动选择文本复制', true);
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(() => showToast('已复制'), fail);
            } else {
                fail();
            }
        }

        // ---------- #5.1 Git 配置接线（表单填充 / 选择文件夹 / 探测仓库 / 凭据保存并测试） ----------
        let pmGitBase = {};     // 编辑态项目 git 配置（remote/url/credential/ssh_key）
        let pmSyncBase = {};    // 编辑态 git_sync（用于 timeout 与非法值回落）
        let pmCredState = {configured: false};   // GET /git/credential 的安全视图（永不含密码）

        function pmFillRemoteOptions(names, selected) {
            const sel = document.getElementById('pmRemote');
            const list = [];
            (names || []).forEach(n => {
                const v = String(n || '').trim();
                if (v && !list.includes(v)) list.push(v);
            });
            if (!list.length) list.push('origin');
            if (selected && !list.includes(selected)) list.unshift(selected);
            sel.innerHTML = list.map(n => `<option value="${escapeAttr(n)}">${escapeHtml(n)}</option>`).join('');
            sel.value = (selected && list.includes(selected)) ? selected : list[0];
        }

        function pmSyncCredRows() {
            const kind = document.getElementById('pmCredKind').value;
            document.getElementById('pmCredTokenRow').hidden = kind !== 'token';
            document.getElementById('pmCredSshRow').hidden = kind !== 'ssh';
        }

        function pmApplySyncSelects(sync) {
            const v = pmSyncForm({}, sync);
            document.getElementById('pmSyncBefore').value = v.before_card;
            document.getElementById('pmSyncOnDone').value = v.on_done;
            document.getElementById('pmSyncDiverge').value = v.diverge;
        }

        // 表单 → 项目 git 配置（remote 默认 origin；url 留空不提交；ssh_key 仅 ssh 类型提交）
        function pmCollectGitConfig() {
            const kind = document.getElementById('pmCredKind').value;
            const remote = document.getElementById('pmRemote').value.trim() || 'origin';
            const url = document.getElementById('pmGitUrl').value.trim();
            const sshKey = document.getElementById('pmCredSsh').value.trim();
            const git = {remote: remote, credential: kind, ssh_key: kind === 'ssh' ? sshKey : ''};
            if (url) git.url = url;
            return git;
        }

        // 表单 → git_sync（白名单校验 + timeout 沿用编辑前值，避免覆盖成默认 120）
        function pmCollectSync() {
            return pmSyncForm({
                before_card: document.getElementById('pmSyncBefore').value,
                on_done: document.getElementById('pmSyncOnDone').value,
                diverge: document.getElementById('pmSyncDiverge').value,
            }, pmSyncBase);
        }

        // 凭据状态（只读取「已保存 / 未配置」，服务端永不含密码）
        async function pmLoadCredentialState(name) {
            try {
                const res = await fetch('/api/projects/' + encodeURIComponent(name) + '/git/credential', {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (pmEditingName !== name) return;   // 弹窗已切换 / 关闭：丢弃旧响应
                if (res.ok && d.state) {
                    pmCredState = d.state;
                    document.getElementById('pmCredState').textContent = pmCredentialStateText(pmCredState);
                }
            } catch (e) { /* 拉取失败保持「未配置」展示；保存时以服务端结果为准 */ }
        }

        // 「选择文件夹」：成功追加一行路径并自动探测；取消（cancelled）静默返回；不支持（501）隐藏按钮并提示手输
        async function pmPickFolder() {
            const btn = document.getElementById('pmPickFolder');
            btn.disabled = true;
            try {
                const res = await fetch('/api/system/pick-folder', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
                });
                const d = await res.json().catch(() => ({}));
                if (res.status === 501 || d.unsupported) {
                    // 当前环境没有系统选择框（Linux 无图形会话/未装 zenity、Docker 等）：隐藏按钮，提示直接输入路径
                    btn.hidden = true;
                    pmSetError(d.error || '当前环境不支持系统文件夹选择，请直接输入仓库绝对路径');
                    document.getElementById('pmPaths').focus();
                    return;
                }
                if (!res.ok) { pmSetError(d.error || '选择文件夹失败'); return; }
                if (d.cancelled || !d.path) return;   // 用户取消：静默
                document.getElementById('pmPaths').value = pmPathsAppend(document.getElementById('pmPaths').value, d.path);
                await pmDetectRepos();   // 路径变化后自动探测
            } catch (e) { pmSetError('网络错误，选择文件夹失败'); }
            finally { btn.disabled = false; }
        }

        // 「探测仓库」：POST git-detect，回填 base_branch / 远端下拉 / URL，逐仓库显示当前分支
        async function pmDetectRepos() {
            const btn = document.getElementById('pmDetect');
            const paths = pmPathsToArray(document.getElementById('pmPaths').value);
            if (!paths.length) { pmSetError('请先填写至少一条仓库绝对路径'); return; }
            btn.disabled = true;
            pmSetError('');
            try {
                const res = await fetch('/api/system/git-detect', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({paths: paths})
                });
                const d = await res.json().catch(() => ({}));
                if (!res.ok) { pmSetError(d.error || '探测仓库失败'); return; }
                const fill = pmDetectFill(d.repos);
                if (fill.base_branch && !pmBaseMapping) {
                    document.getElementById('pmBase').value = fill.base_branch;   // 检测到当前分支
                }
                pmFillRemoteOptions(fill.remotes.map(r => r.name), fill.remote || 'origin');
                if (fill.url) document.getElementById('pmGitUrl').value = fill.url;
                const box = document.getElementById('pmDetectResult');
                box.textContent = fill.line;
                box.hidden = !fill.line;
                box.classList.toggle('warn', !fill.ok);
                if (!fill.ok) pmSetError('未检测到 git 仓库：请确认路径指向含 .git 的仓库根目录');
            } catch (e) { pmSetError('网络错误，探测仓库失败'); }
            finally { btn.disabled = false; }
        }

        // 保存凭据并测试（新增模式在项目创建成功后复用；密码只提交、保存后立即清空输入不回显）
        async function pmSaveCredential(name) {
            if (!name) return {ok: false, message: '项目尚未创建'};
            const body = pmCredentialBody(
                document.getElementById('pmCredKind').value,
                document.getElementById('pmCredUser').value,
                document.getElementById('pmCredToken').value,
                document.getElementById('pmCredSsh').value,
                document.getElementById('pmGitUrl').value,
                !!(pmCredState && pmCredState.configured)
            );
            let res, d;
            try {
                res = await fetch('/api/projects/' + encodeURIComponent(name) + '/git/credential', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
                });
                d = await res.json().catch(() => ({}));
            } catch (e) { return {ok: false, message: '网络错误，凭据保存失败'}; }
            if (!res.ok) return {ok: false, message: d.error || ('凭据保存失败（HTTP ' + res.status + '）')};
            pmCredState = d.state || pmCredState || {configured: false};
            document.getElementById('pmCredState').textContent = pmCredentialStateText(pmCredState);
            document.getElementById('pmCredToken').value = '';   // 密码不回显：保存后立即清空
            const test = d.test || null;
            if (d.ok === false) {
                // save 后自动测试失败仍保凭据（后端语义）：文案区分「已保存但测试失败」
                const tail = (test && test.message) ? '：' + test.message : '';
                return {ok: false, message: (body.action === 'save' ? '凭据已保存，但连通性测试失败' : '凭据测试失败') + tail};
            }
            return {ok: true, message: (test && test.message)
                || (body.action === 'save' ? '凭据已保存并测试通过' : '凭据测试通过')};
        }

        // 「保存并测试」：新增模式先创建项目（创建成功后自动保存凭据）；编辑模式直接保存并测试
        async function pmSaveCredentialAndTest() {
            if (!pmEditingName) { await submitProjectModal(); return; }
            const btn = document.getElementById('pmCredSaveTest');
            btn.disabled = true;
            pmSetError('');
            const project = pmEditingName;
            try {
                // 先把远端 / 凭据类型 PATCH 进项目配置，保证 dispatcher 读到与凭据一致的配置
                let res, d;
                try {
                    res = await fetch('/api/projects/' + encodeURIComponent(project), {
                        method: 'PATCH', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({git: pmCollectGitConfig()}),
                    });
                    d = await res.json().catch(() => ({}));
                } catch (e) { pmSetError('网络错误，保存 Git 配置失败'); return; }
                if (!res.ok) { pmSetError(d.error || '保存 Git 配置失败'); return; }
                const out = await pmSaveCredential(project);
                if (out.ok) showToast(out.message || '凭据已保存并测试通过');
                else pmSetError(out.message);
            } finally { btn.disabled = false; }
        }

        // 打开新增（name 空）或编辑模态：编辑先 GET 预填（失败不打开，fail-closed）
        async function openProjectModal(name) {
            // 防御：看板进程未重启时旧模板没有 Git 表单节点 → 明确提示并放弃，避免半初始化异常
            if (!document.getElementById('pmRemote')) {
                showToast('项目 Git 配置界面需重启看板后生效，请稍后再试', true);
                return;
            }
            pmEditingName = name || null;
            pmBaseMapping = false;
            pmGitBase = {};
            pmSyncBase = {};
            pmCredState = {configured: false};
            pmSetError('');
            document.getElementById('projectModalForm').hidden = false;
            document.getElementById('projectModalChecklist').hidden = true;
            document.getElementById('projectModalTitle').textContent = pmEditingName ? '编辑项目' : '新增项目';
            document.getElementById('pmSubmit').textContent = pmEditingName ? '保存' : '创建';
            const nameEl = document.getElementById('pmName');
            const pathsEl = document.getElementById('pmPaths');
            const baseEl = document.getElementById('pmBase');
            const goalEl = document.getElementById('pmGoal');
            nameEl.disabled = !!pmEditingName;
            baseEl.disabled = false;
            document.getElementById('pmBaseHint').hidden = true;
            // Git 表单复位（新增 = 默认值；编辑 = 下方预填覆盖）
            pmFillRemoteOptions(['origin'], 'origin');
            document.getElementById('pmGitUrl').value = '';
            document.getElementById('pmCredKind').value = 'system';
            document.getElementById('pmCredUser').value = '';
            document.getElementById('pmCredToken').value = '';
            document.getElementById('pmCredSsh').value = '';
            document.getElementById('pmCredState').textContent = '未配置';
            pmSyncCredRows();
            pmApplySyncSelects({});
            const detectBox = document.getElementById('pmDetectResult');
            detectBox.textContent = '';
            detectBox.hidden = true;
            detectBox.classList.remove('warn');
            if (!pmEditingName) {
                nameEl.value = '';
                pathsEl.value = '';
                baseEl.value = '';
                goalEl.value = '';
            } else {
                let d = null;
                try {
                    const res = await fetch('/api/projects/' + encodeURIComponent(pmEditingName), {cache: 'no-store'});
                    d = await res.json().catch(() => ({}));
                    if (!res.ok) { showToast(d.error || '加载项目失败', true); return; }
                } catch (e) { showToast('加载项目失败', true); return; }
                nameEl.value = pmEditingName;
                pathsEl.value = (d.paths || []).join('\n');
                if (d.base_branch && typeof d.base_branch === 'object') {
                    pmBaseMapping = true;   // 映射形态：不进输入框、提交不含该字段
                    baseEl.value = '';
                    baseEl.disabled = true;
                    document.getElementById('pmBaseHint').hidden = false;
                } else {
                    baseEl.value = d.base_branch || '';
                }
                goalEl.value = d.goal || '';
                // Git 远端 / 凭据类型 / 同步策略预填（字段缺失一律回落默认值）
                const git = (d.git && typeof d.git === 'object') ? d.git : {};
                pmGitBase = {remote: git.remote || 'origin', url: git.url || '',
                             credential: git.credential || 'system', ssh_key: git.ssh_key || ''};
                pmFillRemoteOptions([pmGitBase.remote], pmGitBase.remote);
                document.getElementById('pmGitUrl').value = pmGitBase.url || '';
                const credKind = ['system', 'token', 'ssh'].includes(pmGitBase.credential)
                    ? pmGitBase.credential : 'system';
                document.getElementById('pmCredKind').value = credKind;
                document.getElementById('pmCredSsh').value = pmGitBase.ssh_key || '';
                pmSyncCredRows();
                pmSyncBase = (d.git_sync && typeof d.git_sync === 'object') ? d.git_sync : {};
                pmApplySyncSelects(pmSyncBase);
                pmLoadCredentialState(pmEditingName);   // 只显示「已保存 / 未配置」，不回显密码
            }
            document.getElementById('projectModal').classList.add('show');
        }

        function closeProjectModal() {
            document.getElementById('projectModal').classList.remove('show');
            pmEditingName = null;
            pmBaseMapping = false;
            pmGitBase = {};
            pmSyncBase = {};
            pmCredState = {configured: false};
        }

        async function submitProjectModal() {
            pmSetError('');
            const name = document.getElementById('pmName').value.trim();
            const paths = pmPathsToArray(document.getElementById('pmPaths').value);
            const base = document.getElementById('pmBase').value.trim();
            const goal = document.getElementById('pmGoal').value.trim();
            if (!pmEditingName && !pmNameValid(name, autoProjects, 64)) {
                pmSetError('项目名非法或已存在：仅小写字母/数字/连字符、以字母或数字开头、≤64 字符、不可重名');
                return;
            }
            if (!paths.length) { pmSetError('请填写至少一条仓库绝对路径（每行一条）'); return; }
            const body = {paths: paths, goal: goal, git: pmCollectGitConfig(), git_sync: pmCollectSync()};
            // base_branch 留空 = 不提交该键：新增走后端默认 main；编辑保持原值（不可清空，恢复自动探测走文件专家路径）
            if (!pmBaseMapping && base) body.base_branch = base;
            let res, d;
            try {
                res = await fetch(pmEditingName ? '/api/projects/' + encodeURIComponent(pmEditingName) : '/api/projects', {
                    method: pmEditingName ? 'PATCH' : 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(pmEditingName ? body : Object.assign({name: name}, body)),
                });
                d = await res.json().catch(() => ({}));
            } catch (e) { pmSetError('网络错误，提交失败'); return; }
            if (!res.ok) { pmSetError(d.error || '提交失败'); return; }
            if (pmEditingName) {
                closeProjectModal();
                showToast('已保存');
                refreshProjectBar();        // goal/paths 信息条即时更新
                refreshProjectGitStatus();  // 远端 / 凭据类型变更后状态条同步刷新
                return;
            }
            // 创建成功（后续凭据失败不回滚、不丢项目）→ 保存凭据 → 切 checklist 视图
            const created = (d && d.project) || name;
            const kind = document.getElementById('pmCredKind').value;
            const hasSecret = document.getElementById('pmCredToken').value || document.getElementById('pmCredSsh').value;
            let credMsg = '';
            if (kind !== 'system' || hasSecret) {
                const out = await pmSaveCredential(created);
                if (out.ok) credMsg = out.message || '凭据已保存并测试通过';
                else {
                    credMsg = '凭据保存失败：' + out.message + '（项目已创建，可在编辑项目中重试）';
                    showToast(credMsg, true);
                }
            }
            document.getElementById('projectModalForm').hidden = true;
            const box = document.getElementById('projectModalChecklist');
            box.innerHTML = '<div class="pm-check">' + projectChecklistHtml(name, paths[0])
                + (credMsg ? `<div class="pm-check-note">${escapeHtml(credMsg)}</div>` : '')
                + '<div class="pm-check-note">新项目已热接入，关闭后出现在左列表（无需重启看板）。</div></div>';
            box.hidden = false;
            document.getElementById('pmCopyBtn').addEventListener('click', pmCopyAgentsMd);
            document.getElementById('pmDone').addEventListener('click', async () => {
                closeProjectModal();
                await renderNav();
            });
        }

        // 打开移除确认模态：拉该项目需求自行聚合计数（不扩 /api/project-stats；拉取失败禁用移除）
        async function openRemoveProjectModal(name) {
            pmRemovingName = name || null;
            document.getElementById('removeProjectName').textContent = displayProjectName(name);
            document.getElementById('removeProjectStats').textContent = '正在统计…';
            document.getElementById('removeProjectWarn').hidden = true;
            document.getElementById('removeProjectOk').disabled = true;
            document.getElementById('removeProjectModal').classList.add('show');
            try {
                const res = await fetch('/api/requirements?project=' + encodeURIComponent(name), {cache: 'no-store'});
                const rows = await res.json();
                if (pmRemovingName !== name) return;   // 过期响应丢弃（快速切换防串台）
                if (!res.ok || !Array.isArray(rows)) throw new Error('stats failed');
                const by = {};
                rows.forEach(r => { by[r.status] = (by[r.status] || 0) + 1; });
                document.getElementById('removeProjectStats').textContent = projectStatsText(by);
                if (projectRemoveBlocked(by)) {
                    document.getElementById('removeProjectWarn').hidden = false;
                } else {
                    document.getElementById('removeProjectOk').disabled = false;
                }
            } catch (e) {
                if (pmRemovingName !== name) return;
                document.getElementById('removeProjectStats').textContent = '统计失败，已禁用移除（可稍后重试）';
            }
        }

        function closeRemoveProjectModal() {
            document.getElementById('removeProjectModal').classList.remove('show');
            pmRemovingName = null;
        }

        async function confirmRemoveProject() {
            if (!pmRemovingName) return;
            const name = pmRemovingName;
            const okBtn = document.getElementById('removeProjectOk');
            okBtn.disabled = true;
            let res, d;
            try {
                res = await fetch('/api/projects/' + encodeURIComponent(name), {method: 'DELETE'});
                d = await res.json().catch(() => ({}));
            } catch (e) { showToast('网络错误，移除失败', true); okBtn.disabled = false; return; }
            if (res.status === 409) {   // doing 阻断：前端统计后状态变化才会走到这里
                document.getElementById('removeProjectWarn').hidden = false;
                showToast(d.error || '存在进行中的卡，已阻断', true);
                return;
            }
            if (!res.ok && res.status !== 404) {
                showToast(d.error || '移除失败', true);
                okBtn.disabled = false;
                return;
            }
            closeRemoveProjectModal();
            showToast('已移除接入');
            await renderNav();
            render();
            refreshProjectGitStatus();   // #5.2 移除后选中项可能回退「所有项目」：状态条随之隐藏/切换
        }

        // ==================== #3 全局设置抽屉（只读，懒加载一次 + 内存缓存） / #6 双 tab ====================
        // 首次打开 fetch GET /api/settings-drawer（四分组服务端渲染片段，含 8 份提示词全文），
        // 缓存于内存；失败给「重试」入口，永不抛（fail-safe 不炸主页）。
        // #6：双 tab（全局信息 / 模型设置）——打开默认停全局；模型 tab 懒加载（每次打开后的首次切换
        // 重拉、同次打开内切换不重拉以保留未保存修改）；模型 tab 失败独立重试，不阻塞全局 tab。
        let settingsDrawerHtml = null;
        let settingsLoading = false;
        let settingsTab = 'global';       // 当前 tab：global | model | feishu
        let modelSettingsLoaded = false;  // 本次打开内模型设置是否已加载（打开时重置 → 首次切换重拉）
        let modelSettingsLoading = false;
        let feishuSettingsLoaded = false;   // #22 本次打开内飞书设置是否已加载（打开时重置 → 首次切换重拉）
        // #23 重启后等待「已连接」心跳的上限（毫秒）：重启窗口内调度器先 connecting 再 connected，
        // 轮询必须等满这个窗口，不能一见非 not_loaded 就停。
        const FEISHU_RESTART_WAIT_MS = 60000;
        let feishuSettingsLoading = false;
        let notifierSettingsLoaded = false;   // 通知渠道（Webhook / Slack）本次打开内是否已加载
        let notifierSettingsLoading = false;
        let fsBase = null;                  // #22 飞书设置服务端原始数据（重拉后重置）
        let msBase = null;   // 服务端原始数据（delta 基准；重拉后重置）
        let msForm = null;   // 本地编辑态（渲染 + DOM 收集；含 deleted/isNew 标记）
        function openSettingsDrawer() {
            document.getElementById('settingsMask').classList.add('show');
            const panel = document.getElementById('settingsDrawer');
            panel.inert = false;   // 展开才可聚焦/可被读屏发现（关闭态 off-screen 但不该可 Tab 到）
            panel.classList.add('show');
            modelSettingsLoaded = false;   // 每次打开后首次切模型 tab 重拉
            feishuSettingsLoaded = false;  // 每次打开后首次切通知渠道 tab 重拉（飞书 + Webhook/Slack）
            notifierSettingsLoaded = false;
            promptsLoaded = false;          // 提示词 / 技能库：每次打开后首次切换重拉（同次打开内保留未保存草稿）
            skillsLoaded = false;
            switchSettingsTab('global');
            loadSettingsDrawer(false);
        }
        function closeSettingsDrawer() {
            document.getElementById('settingsMask').classList.remove('show');
            const panel = document.getElementById('settingsDrawer');
            panel.classList.remove('show');
            panel.inert = true;
        }
        function switchSettingsTab(tab) {
            settingsTab = ['model', 'feishu', 'prompts', 'skills'].includes(tab) ? tab : 'global';
            document.getElementById('sdTabGlobal').classList.toggle('on', settingsTab === 'global');
            document.getElementById('sdTabModel').classList.toggle('on', settingsTab === 'model');
            document.getElementById('sdTabFeishu').classList.toggle('on', settingsTab === 'feishu');
            document.getElementById('sdTabPrompts').classList.toggle('on', settingsTab === 'prompts');
            document.getElementById('sdTabSkills').classList.toggle('on', settingsTab === 'skills');
            document.getElementById('sdTabGlobal').setAttribute('aria-selected', String(settingsTab === 'global'));
            document.getElementById('sdTabModel').setAttribute('aria-selected', String(settingsTab === 'model'));
            document.getElementById('sdTabFeishu').setAttribute('aria-selected', String(settingsTab === 'feishu'));
            document.getElementById('sdTabPrompts').setAttribute('aria-selected', String(settingsTab === 'prompts'));
            document.getElementById('sdTabSkills').setAttribute('aria-selected', String(settingsTab === 'skills'));
            document.getElementById('settingsBody').hidden = settingsTab !== 'global';
            document.getElementById('settingsModelBody').hidden = settingsTab !== 'model';
            document.getElementById('settingsFeishuBody').hidden = settingsTab !== 'feishu';
            document.getElementById('settingsPromptsBody').hidden = settingsTab !== 'prompts';
            document.getElementById('settingsSkillsBody').hidden = settingsTab !== 'skills';
            // 提示词 / 技能库是左右分栏的编辑器：抽屉加宽（窄屏仍按 100vw 封顶）
            document.getElementById('settingsDrawer').classList.toggle('sd-wide', settingsTab === 'prompts' || settingsTab === 'skills');
            if (settingsTab === 'model' && !modelSettingsLoaded) loadModelSettings(false);
            if (settingsTab === 'feishu' && !feishuSettingsLoaded) loadFeishuSettings(false);
            if (settingsTab === 'feishu' && !notifierSettingsLoaded) loadNotifierSettings(false);
            if (settingsTab === 'prompts' && !promptsLoaded) loadPromptsTab(false);
            if (settingsTab === 'skills' && !skillsLoaded) loadSkillsTab(false);
        }
        function reloadSettingsDrawer() {
            if (settingsTab === 'model') loadModelSettings(true);
            else if (settingsTab === 'feishu') { loadFeishuSettings(true); loadNotifierSettings(true); }
            else if (settingsTab === 'prompts') loadPromptsTab(true);
            else if (settingsTab === 'skills') loadSkillsTab(true);
            else loadSettingsDrawer(true);
        }
        async function loadSettingsDrawer(force) {
            const body = document.getElementById('settingsBody');
            if (settingsDrawerHtml && !force) { body.innerHTML = settingsDrawerHtml; decorateRoleRows(body); return; }
            if (settingsLoading) return;
            settingsLoading = true;
            body.innerHTML = '<div class="sd-note">加载中…</div>';
            try {
                const res = await fetch('/api/settings-drawer', {cache: 'no-store'});
                const d = await res.json();
                if (!d || !d.html) throw new Error('empty');
                settingsDrawerHtml = d.html;
                body.innerHTML = settingsDrawerHtml;
                decorateRoleRows(body);
            } catch (e) {
                settingsDrawerHtml = '';
                body.innerHTML = '<div class="sd-warn">设置内容加载失败。</div>'
                    + '<button type="button" class="btn" id="settingsRetryBtn">重试</button>';
                const btn = document.getElementById('settingsRetryBtn');
                if (btn) btn.addEventListener('click', () => loadSettingsDrawer(true));
            } finally {
                settingsLoading = false;
            }
        }
        // ---- #6 模型设置：GET /api/model-settings（严格读；失败仅抽屉内降级 + 重试） ----
        async function loadModelSettings(force) {
            const body = document.getElementById('settingsModelBody');
            if (modelSettingsLoaded && !force) return;
            if (modelSettingsLoading) return;
            modelSettingsLoading = true;
            body.innerHTML = '<div class="sd-note">模型设置加载中…</div>';
            try {
                const res = await fetch('/api/model-settings', {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                msBase = d;
                msForm = modelSettingsStateFromData(d);
                modelSettingsLoaded = true;
                renderModelSettingsForm();
            } catch (e) {
                msBase = null;
                msForm = null;
                modelSettingsLoaded = false;
                body.innerHTML = '<div class="sd-warn">模型设置加载失败：' + escapeHtml(String((e && e.message) || e)) + '</div>'
                    + '<button type="button" class="btn" id="msRetryBtn">重试</button>';
            } finally {
                modelSettingsLoading = false;
            }
        }
        function modelSettingsStateFromData(d) {
            const data = d || {};
            const providers = (data.providers || []).map(p => ({
                name: p.name, base_url: p.base_url || '', type: p.type || '',
                key_configured: !!p.key_configured, model_count: Number(p.model_count || 0),
                orig_name: p.name, isNew: false, deleted: false, key_input: '', clear_key: false,
                models: [],
            }));
            const known = {};
            providers.forEach(p => { known[p.name] = p; });
            const orphans = [];
            (data.models || []).forEach(m => {
                // #18：max_context_size（编辑态统一为字符串，同 protocol 手法）与逐条不完整原因
                const row = {alias: m.alias, model: m.model || '', protocol: m.protocol || '',
                    max_context_size: (m.max_context_size == null ? '' : String(m.max_context_size)),
                    incomplete_reason: m.incomplete_reason || '',
                    isNew: false, deleted: false};
                const p = known[m.provider];
                if (p) p.models.push(row); else orphans.push(row);
            });
            return {
                path: data.path || '', home: data.home || '',
                default_model: data.default_model || '',
                default_dangling: !!data.default_model_dangling,
                candidates: data.default_model_candidates || [],
                warnings: data.warnings || [],
                provider_types: data.provider_types || [],
                context_defaults: data.context_defaults || [],
                providers: providers, orphans: orphans,
            };
        }
        // #18：max_context_size 预填（纯函数；规则单一源 = 服务端 GET 注入的 context_defaults）：
        // type 为空串即通配；match 为模型名大小写不敏感子串；按顺序首条命中；未命中返回 ''（留空不猜）。
        // 仅作 UI 可改预填，不参与服务端校验。
        function msDefaultContextSize(type, model, defaults) {
            const t = String(type == null ? '' : type).toLowerCase();
            const m = String(model == null ? '' : model).toLowerCase();
            if (!m) return '';
            const list = Array.isArray(defaults) ? defaults : [];
            for (const r of list) {
                if (!r) continue;
                const rt = String(r.type == null ? '' : r.type).toLowerCase();
                const rm = String(r.match == null ? '' : r.match).toLowerCase();
                const v = r.value;
                if (!rm || (rt && rt !== t)) continue;
                if (m.indexOf(rm) === -1) continue;
                if (typeof v === 'number' && isFinite(v) && v >= 1 && Math.floor(v) === v) return v;
            }
            return '';
        }
        // #18：max_context_size 整数解析（纯函数）：仅接受十进制整数字面量且 ≥1；
        // 空串/小数/科学计数/负数/含字符/超安全整数 → null（与后端 _cfg_ok_pos_int 同口径）。
        function msParseContextSize(v) {
            const s = String(v == null ? '' : v).trim();
            if (!/^[0-9]+$/.test(s)) return null;
            const n = parseInt(s, 10);
            if (!isFinite(n) || n < 1) return null;
            return n;
        }
        // 渲染纯函数（node harness 断言）：厂商块 / 别名分组（缺失 provider 归「（未归属）」殿后）/
        // default_model 编辑行（input + datalist，候选 = 服务端下发）/ key 三态输入（恒空）
        function modelSettingsFormHtml(d) {
            const data = d || {};
            const providers = Array.isArray(data.providers) ? data.providers : [];
            const orphans = Array.isArray(data.orphans) ? data.orphans : [];
            const candidates = Array.isArray(data.candidates) ? data.candidates : [];
            const dm = String(data.default_model == null ? '' : data.default_model);
            const dmList = candidates.map(m => `<option value="${escapeAttr(m)}"></option>`).join('');
            // #13：provider type 合法值单一源 = 服务端下发的 provider_types（后端 HARNESS_PROVIDER_TYPES）
            const ptypes = Array.isArray(data.provider_types) ? data.provider_types : [];
            const typeListHtml = '<datalist id="msProviderTypes">'
                + ptypes.map(t => `<option value="${escapeAttr(t)}"></option>`).join('') + '</datalist>';
            const typeHint = ptypes.length ? escapeHtml(ptypes.join(' / ')) : '（未取到合法值清单）';
            const dmWarn = data.default_dangling
                ? '<div class="ms-warn">当前值不在别名池（models 别名 ∪ 厂商 model 合成别名）或为空值，保存将被拒绝，请修正。</div>' : '';
            const toolbar = '<div class="ms-top">'
                + '<div class="ms-path">当前配置文件：<code>' + escapeHtml(String(data.path || '')) + '</code></div>'
                + '<div class="ms-note">保存后无需重启看板 / 调度器：下一张卡生效（已启动会话沿用旧值）。'
                + 'API KEY 留空 = 保留现有 key；填写 = 替换；「清除已配置 key」= 删除（不回显原值）。'
                + '删除厂商 / 别名前先确认无外部引用（projects.json / flows.json 中的同名引用不会自动清理）。</div>'
                + '<button type="button" class="btn btn-primary" id="msSaveBtn">保存</button>'
                + '<div class="ms-error" id="msError"></div>'
                + '</div>';
            const dmRow = '<div class="ms-dm"><label for="msDefaultInput">default_model（全局默认模型别名）</label>'
                + `<input id="msDefaultInput" list="msDmList" value="${escapeAttr(dm)}" autocomplete="off" spellcheck="false">`
                + `<datalist id="msDmList">${dmList}</datalist>` + dmWarn + '</div>';
            const warns = (data.warnings && data.warnings.length)
                ? '<div class="sd-warn">' + data.warnings.map(w => escapeHtml(String(w))).join('<br>') + '</div>' : '';
            const blocks = providers.map((p, oidx) => {
                if (p.deleted) return '';
                const configured = !!p.key_configured;
                const cnt = Number(p.model_count || 0);
                const models = (p.models || []).filter(m => !m.deleted);
                const aliasRows = models.map(m => '<tr>'
                    + '<td><code>' + escapeHtml(m.alias) + '</code></td>'
                    + '<td>' + escapeHtml(m.model) + '</td>'
                    + '<td><input class="ms-alias-ctx" data-oidx="' + oidx + '"'
                    + ' data-alias="' + escapeAttr(m.alias) + '"'
                    + ' value="' + escapeAttr(m.max_context_size == null ? '' : String(m.max_context_size)) + '"'
                    + ' placeholder="max_context_size（必填）" autocomplete="off" spellcheck="false">'
                    + (m.incomplete_reason
                        ? '<div class="ms-field-hint ms-ctx-warn">' + escapeHtml(m.incomplete_reason) + '</div>'
                        : '') + '</td>'
                    + '<td><input class="ms-alias-protocol" data-oidx="' + oidx + '"'
                    + ' data-alias="' + escapeAttr(m.alias) + '" value="' + escapeAttr(m.protocol || '') + '"'
                    + ' placeholder="protocol（可选）" autocomplete="off" spellcheck="false"></td>'
                    + `<td><button type="button" class="btn ms-del-alias" data-oidx="${oidx}"`
                    + ` data-alias="${escapeAttr(m.alias)}">删除</button></td></tr>`).join('');
                const table = '<table class="ms-alias-table">'
                    + '<tr><th>模型别名</th><th>上游 model</th><th>max_context_size</th><th>protocol</th><th>操作</th></tr>'
                    + (models.length ? aliasRows : '<tr><td colspan="5">（无别名）</td></tr>') + '</table>';
                const addRow = '<div class="ms-add-alias">'
                    + '<input class="ms-new-alias" placeholder="新别名（建议 厂商名/model，如 ' + escapeAttr(p.name) + '/m3）" autocomplete="off">'
                    + '<input class="ms-new-model" placeholder="上游 model" autocomplete="off">'
                    + '<input class="ms-new-ctx" placeholder="max_context_size（必填）" autocomplete="off" spellcheck="false">'
                    + '<input class="ms-new-protocol" placeholder="protocol（可选）" autocomplete="off" spellcheck="false">'
                    + `<button type="button" class="btn ms-add-alias-btn" data-oidx="${oidx}">添加</button></div>`;
                const delProv = `<button type="button" class="btn ms-del-prov" data-oidx="${oidx}"`
                    + (cnt > 0 ? ' disabled title="该厂商下仍有模型别名，先删除全部别名再删除厂商"' : ' title="删除该厂商（连同其配置段）"')
                    + '>删除厂商</button>';
                const hint = cnt > 0
                    ? '<div class="ms-hint">该厂商下仍有 ' + String(cnt) + ' 个模型别名，先删除全部别名再删除厂商。</div>' : '';
                return '<div class="ms-prov" data-oidx="' + oidx + '">'
                    + '<div class="ms-prov-head"><b>厂商：' + escapeHtml(p.name) + '</b>' + delProv + '</div>'
                    + '<div class="ms-fields">'
                    + '<div class="ms-field"><label>名称</label><input class="ms-name" value="' + escapeAttr(p.name) + '" autocomplete="off"></div>'
                    + '<div class="ms-field"><label>base URL</label><input class="ms-base" value="' + escapeAttr(p.base_url || '') + '" autocomplete="off" spellcheck="false"></div>'
                    + '<div class="ms-field"><label>类型（provider type）</label><input class="ms-type" list="msProviderTypes" value="' + escapeAttr(p.type || '') + '" autocomplete="off">'
                    + '<span class="ms-field-hint">合法值：' + typeHint + '</span></div>'
                    + '<div class="ms-field"><label>API KEY</label><input class="ms-key" type="password" value="' + escapeAttr(p.key_input || '') + '" autocomplete="off"'
                    + ' placeholder="' + (configured ? '已配置（留空=保留）' : '未配置') + '"></div>'
                    + '</div>'
                    + '<label class="ms-clear-wrap"><input type="checkbox" class="ms-clear"'
                    + (configured ? '' : ' disabled') + '>清除已配置 key</label>'
                    + hint + table + addRow + '</div>';
            }).join('');
            const orphanModels = orphans.filter(m => !m.deleted);
            const orphanHtml = orphanModels.length ? ('<div class="ms-prov" data-orphan="1">'
                + '<div class="ms-prov-head"><b>（未归属）</b></div>'
                + '<div class="ms-hint">provider 缺失或指向不存在厂商的别名：只能删除（或直接编辑文件）。</div>'
                + '<table class="ms-alias-table"><tr><th>模型别名</th><th>上游 model</th><th>操作</th></tr>'
                + orphanModels.map(m => '<tr><td><code>' + escapeHtml(m.alias) + '</code></td>'
                    + '<td>' + escapeHtml(m.model) + '</td>'
                    + '<td><button type="button" class="btn ms-del-alias" data-oidx="orphan"'
                    + ` data-alias="${escapeAttr(m.alias)}">删除</button></td></tr>`).join('')
                + '</table></div>') : '';
            const noProv = providers.filter(p => !p.deleted).length ? ''
                : '<div class="ms-hint">尚无厂商：请先在下方「新增厂商」添加厂商，再为其添加模型别名。</div>';
            const newProv = '<div class="ms-prov"><div class="ms-prov-head"><b>新增厂商</b></div>'
                + '<div class="ms-fields">'
                + '<div class="ms-field"><label>名称</label><input id="msNewName" autocomplete="off"></div>'
                + '<div class="ms-field"><label>base URL</label><input id="msNewBase" autocomplete="off" spellcheck="false"></div>'
                + '<div class="ms-field"><label>类型</label><input id="msNewType" list="msProviderTypes" autocomplete="off">'
                + '<span class="ms-field-hint">合法值：' + typeHint + '</span></div>'
                + '<div class="ms-field"><label>API KEY（可选）</label><input id="msNewKey" type="password" value="" autocomplete="off"></div>'
                + '</div><button type="button" class="btn" id="msAddProvBtn">添加厂商</button></div>';
            return toolbar + warns + dmRow + typeListHtml + blocks + orphanHtml + noProv + newProv;
        }
        // DOM → 编辑态（只读输入值；删除态/新增态由操作函数维护）。
        function readModelSettingsFormState(body, state) {
            if (!state || !body) return state;
            const dmInput = body.querySelector('#msDefaultInput');
            if (dmInput) state.default_model = String(dmInput.value == null ? '' : dmInput.value);
            (state.providers || []).forEach((p, oidx) => {
                const block = body.querySelector('.ms-prov[data-oidx="' + oidx + '"]');
                if (!block) return;
                const nameI = block.querySelector('.ms-name');
                const baseI = block.querySelector('.ms-base');
                const typeI = block.querySelector('.ms-type');
                const keyI = block.querySelector('.ms-key');
                const clearI = block.querySelector('.ms-clear');
                if (nameI) p.name = String(nameI.value == null ? '' : nameI.value);
                if (baseI) p.base_url = String(baseI.value == null ? '' : baseI.value);
                if (typeI) p.type = String(typeI.value == null ? '' : typeI.value);
                if (keyI) p.key_input = String(keyI.value == null ? '' : keyI.value);
                if (clearI) p.clear_key = !!clearI.checked;
                // #13：protocol 按 data-alias 对位回写（不按索引——表格已滤掉删除态，索引会错位）
                const protoIs = block.querySelectorAll ? block.querySelectorAll('.ms-alias-protocol') : [];
                Array.prototype.forEach.call(protoIs, el => {
                    const a = el.getAttribute ? el.getAttribute('data-alias') : null;
                    const row = (p.models || []).find(m => m.alias === a);
                    if (row) row.protocol = String(el.value == null ? '' : el.value);
                });
                // #18：max_context_size 同款按 data-alias 对位回写
                const ctxIs = block.querySelectorAll ? block.querySelectorAll('.ms-alias-ctx') : [];
                Array.prototype.forEach.call(ctxIs, el => {
                    const a = el.getAttribute ? el.getAttribute('data-alias') : null;
                    const row = (p.models || []).find(m => m.alias === a);
                    if (row) row.max_context_size = String(el.value == null ? '' : el.value);
                });
            });
            return state;
        }
        // 编辑态 → 提交载荷（纯函数；空 = null「没有改动」）。空 key 输入不生成 op（留空=保留）；
        // 清空 default_model 不生成 op（保存前另有拦截）。
        function modelSettingsDelta(orig, cur) {
            if (!orig || !cur) return null;
            const out = {};
            const upd = [], addP = [], delP = [], addM = [], delM = [], updM = [];
            const origProv = {};
            (orig.providers || []).forEach(p => { origProv[p.name] = p; });
            // #13：protocol 基线（按别名取，供 models_update 判定「变/清」）
            const origProto = {};
            // #18：max_context_size 基线（按别名取，供 models_update 判定「变/未变」）
            const origCtx = {};
            const _ctxStr = (m) => String(m.max_context_size == null ? '' : m.max_context_size).trim();
            (orig.providers || []).forEach(p => (p.models || []).forEach(m => {
                origProto[m.alias] = m.protocol || '';
                origCtx[m.alias] = _ctxStr(m);
            }));
            (orig.orphans || []).forEach(m => {
                origProto[m.alias] = m.protocol || '';
                origCtx[m.alias] = _ctxStr(m);
            });
            (cur.providers || []).forEach(p => {
                if (p.deleted) {
                    if (!p.isNew) delP.push(p.orig_name || p.name);
                    return;
                }
                if (p.isNew) {
                    const item = {name: p.name, base_url: p.base_url, type: p.type};
                    if (p.key_input) item.key = p.key_input;
                    addP.push(item);
                    (p.models || []).forEach(m => {
                        if (m.deleted) return;
                        const mi = {alias: m.alias, provider: p.name, model: m.model};
                        if (m.protocol) mi.protocol = m.protocol;
                        const n = msParseContextSize(m.max_context_size);
                        if (n !== null) mi.max_context_size = n;
                        addM.push(mi);
                    });
                    return;
                }
                const o = origProv[p.orig_name || p.name] || {};
                const item = {name: p.orig_name || p.name};
                let changed = false;
                if (p.name !== (p.orig_name || p.name)) { item.new_name = p.name; changed = true; }
                if ((p.base_url || '') !== (o.base_url || '')) { item.base_url = p.base_url; changed = true; }
                if ((p.type || '') !== (o.type || '')) { item.type = p.type; changed = true; }
                if (p.key_input) { item.key = p.key_input; changed = true; }
                if (p.clear_key && o.key_configured) { item.clear_key = true; changed = true; }
                (p.models || []).forEach(m => {
                    if (m.deleted) { if (!m.isNew) delM.push(m.alias); }
                    else if (m.isNew) {
                        const mi = {alias: m.alias, provider: p.name, model: m.model};
                        if (m.protocol) mi.protocol = m.protocol;
                        const n = msParseContextSize(m.max_context_size);
                        if (n !== null) mi.max_context_size = n;
                        addM.push(mi);
                    } else {
                        // #13 protocol 与 #18 max_context_size 同一别名合并为一条 models_update
                        const op = {alias: m.alias};
                        let dirty = false;
                        const now = String(m.protocol == null ? '' : m.protocol);
                        const was = String(origProto[m.alias] == null ? '' : origProto[m.alias]);
                        if (now !== was) {
                            if (now) op.protocol = now; else op.clear_protocol = true;
                            dirty = true;
                        }
                        const nowCtx = _ctxStr(m);
                        const wasCtx = String(origCtx[m.alias] == null ? '' : origCtx[m.alias]);
                        if (nowCtx !== wasCtx) {
                            const n = msParseContextSize(nowCtx);
                            if (n !== null) { op.max_context_size = n; dirty = true; }
                        }
                        if (dirty) updM.push(op);
                    }
                });
                if (changed) upd.push(item);
            });
            (cur.orphans || []).forEach(m => {
                if (m.deleted && !m.isNew) delM.push(m.alias);
            });
            // 同单又删同别名 → 以 models_delete 为准（服务端 models_delete 亦剔除同别名字段 intent）
            const delSet = {};
            delM.forEach(a => { delSet[a] = 1; });
            const updMFinal = updM.filter(it => !delSet[it.alias]);
            const curDm = String(cur.default_model == null ? '' : cur.default_model).trim();
            const origDm = String(orig.default_model == null ? '' : orig.default_model).trim();
            if (curDm !== origDm && curDm !== '') out.default_model = curDm;
            if (upd.length) out.providers_update = upd;
            if (addP.length) out.providers_add = addP;
            if (delP.length) out.providers_delete = delP;
            if (addM.length) out.models_add = addM;
            if (updMFinal.length) out.models_update = updMFinal;
            if (delM.length) out.models_delete = delM;
            return Object.keys(out).length ? out : null;
        }
        function renderModelSettingsForm() {
            const body = document.getElementById('settingsModelBody');
            if (!body || !msForm) return;
            body.innerHTML = modelSettingsFormHtml(msForm);
        }
        function msSyncForm() {
            readModelSettingsFormState(document.getElementById('settingsModelBody'), msForm);
        }
        function msErr(msg) {
            const el = document.getElementById('msError');
            if (el) el.textContent = msg || '';
        }
        function msAddProvider() {
            if (!msForm) return;
            msSyncForm();
            const g = (id) => { const el = document.getElementById(id); return el ? String(el.value == null ? '' : el.value).trim() : ''; };
            const name = g('msNewName'), base = g('msNewBase'), type = g('msNewType'), key = g('msNewKey');
            if (!name || !base || !type) { msErr('新增厂商需填写名称 / base URL / 类型'); return; }
            msForm.providers.push({name: name, base_url: base, type: type, key_configured: false,
                model_count: 0, orig_name: name, isNew: true, deleted: false, key_input: key,
                clear_key: false, models: []});
            renderModelSettingsForm();
        }
        function msAddAlias(oidx) {
            if (!msForm) return;
            msSyncForm();
            const p = msForm.providers[Number(oidx)];
            if (!p) return;
            const block = document.querySelector('#settingsModelBody .ms-prov[data-oidx="' + oidx + '"]');
            const aEl = block ? block.querySelector('.ms-new-alias') : null;
            const mEl = block ? block.querySelector('.ms-new-model') : null;
            const pEl = block ? block.querySelector('.ms-new-protocol') : null;
            const cEl = block ? block.querySelector('.ms-new-ctx') : null;
            const alias = aEl ? String(aEl.value == null ? '' : aEl.value).trim() : '';
            const model = mEl ? String(mEl.value == null ? '' : mEl.value).trim() : '';
            const protocol = pEl ? String(pEl.value == null ? '' : pEl.value).trim() : '';
            if (!alias || !model) { msErr('新增别名需填写别名与上游 model'); return; }
            // #18：max_context_size 必填——空则按厂商 type + 模型名预填默认值（可改），仍非法即拦截
            let ctxRaw = cEl ? String(cEl.value == null ? '' : cEl.value).trim() : '';
            if (!ctxRaw) {
                const d = msDefaultContextSize(p.type, model, (msForm && msForm.context_defaults) || []);
                if (d !== '' && d != null) ctxRaw = String(d);
            }
            const ctx = msParseContextSize(ctxRaw);
            if (ctx === null) {
                msErr('新增别名「' + alias + '」的 max_context_size 必填（需为 ≥1 的整数）');
                return;
            }
            p.models = p.models || [];
            p.models.push({alias: alias, model: model, protocol: protocol,
                           max_context_size: String(ctx), incomplete_reason: '',
                           isNew: true, deleted: false});
            renderModelSettingsForm();
        }
        function msDeleteProvider(oidx) {
            if (!msForm) return;
            msSyncForm();
            const p = msForm.providers[Number(oidx)];
            if (!p) return;
            p.deleted = true;
            renderModelSettingsForm();
        }
        function msDeleteAlias(oidx, alias) {
            if (!msForm) return;
            msSyncForm();
            let m = null;
            if (oidx === 'orphan' || oidx == null) {
                m = (msForm.orphans || []).find(x => x.alias === alias);
            } else {
                const p = msForm.providers[Number(oidx)];
                m = p && (p.models || []).find(x => x.alias === alias);
            }
            if (m) m.deleted = true;
            renderModelSettingsForm();
        }
        async function saveModelSettings() {
            if (!msForm || !msBase) return;
            msSyncForm();
            msErr('');
            const dm = String(msForm.default_model == null ? '' : msForm.default_model).trim();
            if (msBase.default_model && !dm) { msErr('default_model 不能为空（原值非空被清空，已拦截）'); return; }
            for (const p of msForm.providers || []) {
                if (!p.deleted && !p.isNew && p.key_input && p.clear_key) {
                    msErr('厂商「' + p.name + '」不能同时填写新 key 与勾选「清除已配置 key」');
                    return;
                }
            }
            const delta = modelSettingsDelta(msBase, msForm);
            if (!delta) { showToast('没有改动'); return; }
            // #18 读法 A 前端预检（与后端同口径）：仅新增行与 ctx 已改动的存量行要求正整数；
            // 未触碰的存量不完整条目不阻断。「对不完整条目只改 protocol」由服务端最终态校验 400 兜底。
            const badCtx = [];
            const baseCtx = {};
            (msBase.providers || []).forEach(p => (p.models || []).forEach(m => {
                baseCtx[m.alias] = String(m.max_context_size == null ? '' : m.max_context_size).trim();
            }));
            for (const p of msForm.providers || []) {
                if (p.deleted) continue;
                for (const m of p.models || []) {
                    if (m.deleted) continue;
                    const cur = String(m.max_context_size == null ? '' : m.max_context_size).trim();
                    const touched = m.isNew || cur !== String(baseCtx[m.alias] == null ? '' : baseCtx[m.alias]);
                    if (touched && msParseContextSize(cur) === null) badCtx.push(m.alias);
                }
            }
            if (badCtx.length) {
                msErr('max_context_size 必填且需为 ≥1 的整数：' + badCtx.join('、'));
                return;
            }
            const btn = document.getElementById('msSaveBtn');
            if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
            try {
                const res = await fetch('/api/model-settings', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(delta),
                });
                const d = await res.json().catch(() => ({}));
                if (!res.ok) { msErr(d.error || ('保存失败（HTTP ' + res.status + '）')); return; }
                showToast('已保存（下一张卡生效）');
                await loadModelSettings(true);   // 重拉重置基准（orig）
            } catch (e) {
                msErr('保存失败：' + ((e && e.message) || e));
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = '保存'; }
            }
        }
        document.getElementById('settingsModelBody').addEventListener('click', (e) => {
            const t = e.target && e.target.closest ? e.target.closest('button') : null;
            if (!t) return;
            if (t.id === 'msSaveBtn') { saveModelSettings(); return; }
            if (t.id === 'msAddProvBtn') { msAddProvider(); return; }
            if (t.id === 'msRetryBtn') { loadModelSettings(true); return; }
            if (t.classList.contains('ms-add-alias-btn')) { msAddAlias(t.getAttribute('data-oidx')); return; }
            if (t.classList.contains('ms-del-prov')) { msDeleteProvider(t.getAttribute('data-oidx')); return; }
            if (t.classList.contains('ms-del-alias')) { msDeleteAlias(t.getAttribute('data-oidx'), t.getAttribute('data-alias')); return; }
        });
        // #18：新增别名行的 max_context_size 预填——输入上游 model 时按厂商 type + 模型名填入默认值
        // （仅当该输入仍为空；用户手填或清空后不再覆盖，预填值可改）。
        document.getElementById('settingsModelBody').addEventListener('input', (e) => {
            const t = e.target;
            if (!t || !t.classList || !t.classList.contains('ms-new-model')) return;
            const row = t.closest ? t.closest('.ms-add-alias') : null;
            const block = t.closest ? t.closest('.ms-prov') : null;
            const ctxEl = row ? row.querySelector('.ms-new-ctx') : null;
            if (!ctxEl || String(ctxEl.value == null ? '' : ctxEl.value).trim()) return;
            const typeEl = block ? block.querySelector('.ms-type') : null;
            const type = typeEl ? String(typeEl.value == null ? '' : typeEl.value) : '';
            const d = msDefaultContextSize(type, t.value, (msForm && msForm.context_defaults) || []);
            if (d !== '' && d != null) ctxEl.value = String(d);
        });
        // ---- #22 飞书机器人设置：GET/POST /api/feishu-settings + POST /api/feishu-settings/test ----
        function feishuSettingsFormHtml(d) {
            const data = d || {};
            const appId = String(data.app_id == null ? '' : data.app_id);
            const configured = !!data.app_secret_configured;
            const warns = (data.warnings && data.warnings.length)
                ? '<div class="sd-warn">' + data.warnings.map(w => escapeHtml(String(w))).join('<br>') + '</div>' : '';
            const note = '<div class="sd-note">飞书机器人凭据（仓库根 <code>' + escapeHtml(String(data.path || '')) + '</code>）。'
                + 'APP SECRET 留空 = 保留现有值；勾选「清除已配置 secret」= 删除（不回显原值）。'
                + '凭据由调度器进程持有：保存后需重启调度器才会加载新值。</div>';
            // #40 运行态区（与 guide 运行时环境表同一状态模型）：状态名 + 原因 + 来源（五态单一表）
            const runtime = (data.runtime && typeof data.runtime === 'object') ? data.runtime : {};
            const state = String(runtime.state || 'unknown');
            const reason = String(runtime.reason == null ? '' : runtime.reason);
            const stateLabels = {connected: '已连接', not_loaded: '凭据未加载', stale: '心跳过期',
                                 failed: '网络连接失败', disabled: '显式停用', unknown: '调度器未上报'};
            const stateLabel = (state === 'disabled' && reason.indexOf('REQ_BOARD_NO_FEISHU') < 0)
                ? '凭据未配置' : (stateLabels[state] || state);
            const rtCls = (state === 'connected') ? 'fs-ok'
                : ((state === 'not_loaded' || state === 'unknown') ? 'fs-warn' : 'fs-off');
            const rt = '<div class="fs-status ' + rtCls + '" id="fsRuntime">'
                + '<b>通道状态：' + escapeHtml(stateLabel) + '</b>'
                + (reason ? '<div>原因：' + escapeHtml(reason) + '</div>' : '')
                + '<div>凭据来源：' + escapeHtml(String(runtime.source || '—')) + '</div>'
                + '</div>';
            const restartBtn = runtime.restart_available
                ? '<button type="button" class="btn" id="fsRestartBtn">重启调度器使其生效</button>' : '';
            const toolbar = '<div class="ms-top">'
                + '<button type="button" class="btn btn-primary" id="fsSaveBtn">保存</button>'
                + '<button type="button" class="btn" id="fsTestBtn">测试连通性</button>'
                + restartBtn
                + '<div class="ms-error" id="fsError"></div></div>';
            // #24 看板助手模型下拉：按供应商分组的**可用**别名（服务端已过滤 usable=true）；
            // 当前值不在可用分组里（池内不可用/池不可用）时追加为「当前值」项，避免静默丢配置。
            const am = String(data.assistant_model == null ? '' : data.assistant_model);
            const amGroups = Array.isArray(data.assistant_models) ? data.assistant_models : [];
            let amOpts = '<option value=""' + (am ? '' : ' selected') + '>（未配置，走默认模型）</option>';
            let amPresent = false;
            amGroups.forEach(g => {
                const items = Array.isArray(g && g.items) ? g.items : [];
                if (!items.length) return;
                amOpts += '<optgroup label="' + escapeAttr(String(g.label || '')) + '">';
                items.forEach(a => {
                    const alias = String(a);
                    if (alias === am) amPresent = true;
                    amOpts += '<option value="' + escapeAttr(alias) + '"'
                        + (alias === am ? ' selected' : '') + '>' + escapeHtml(alias) + '</option>';
                });
                amOpts += '</optgroup>';
            });
            if (am && !amPresent) {
                amOpts += '<option value="' + escapeAttr(am) + '" selected>' + escapeHtml(am)
                    + '（当前值，别名池内不可用）</option>';
            }
            const fields = '<div class="ms-fields">'
                + '<div class="ms-field"><label>APP ID</label>'
                + '<input id="fsAppId" value="' + escapeAttr(appId) + '" placeholder="cli_..." autocomplete="off" spellcheck="false"></div>'
                + '<div class="ms-field"><label>APP SECRET</label>'
                + '<input id="fsAppSecret" type="password" value="" autocomplete="off" placeholder="'
                + (configured ? '已配置（留空=保留）' : '未配置') + '"></div>'
                + '<div class="ms-field"><label>看板助手模型</label>'
                + '<select id="fsAssistantModel">' + amOpts + '</select></div>'
                + '</div>'
                + '<label class="ms-clear-wrap"><input type="checkbox" id="fsClearSecret"'
                + (configured ? '' : ' disabled') + '>清除已配置 secret</label>';
            // #31 看板助手能力分组：独立分区标题 + 逐项一行说明 + 当前状态标注；
            // 未配置飞书凭据时仍可见并给出「需先完成飞书配置」提示（不整块隐藏）
            const capRow = (id, name, on, desc, defaultOn) => '<div class="cap-item">'
                + '<label class="cap-head"><input type="checkbox" id="' + id + '"'
                + (on === true ? ' checked' : '') + '><b>' + escapeHtml(name) + '</b>'
                + '<span class="cap-state ' + (on === true ? 'on' : 'off') + '">'
                + (on === true ? (defaultOn ? '当前已开启（默认开启）' : '当前已开启')
                               : (defaultOn ? '当前关闭' : '当前关闭（默认关闭）'))
                + '</span></label>'
                + '<div class="sd-note cap-desc">' + escapeHtml(desc) + '</div></div>';
            const capCredWarn = configured ? ''
                : '<div class="sd-warn" id="fsCapsCredWarn">尚未配置飞书凭据：需先在上方填写 APP ID / '
                  + 'APP SECRET 并保存、由调度器加载后，助手才会响应消息；'
                  + '下面的能力开关与凭据无关，可随时开启。</div>';
            const caps = '<div class="sd-group" id="fsAssistantCaps"><h3>看板助手能力</h3>'
                + '<div class="sd-note">读图（多模态）<b>默认开启</b>，其余四个开关<b>默认关闭</b>，开启后助手才能执行对应操作。'
                + '由看板按工具调用实时读取，<b>保存即生效</b>、无需重启调度器'
                + '（上方凭据变更仍需重启调度器）。</div>'
                + capCredWarn
                + capRow('fsAssistantVision', '读图（多模态）', data.assistant_vision,
                         '开启后助手能直接查看飞书发来的图片并描述内容；关闭后图片只提供文件名与路径。', true)
                + capRow('fsAssistantFileTools', '代码只读分析', data.assistant_file_tools,
                         '开启后助手能读文件、列文件、按内容检索（范围 = 项目清单全部项目，跨项目，结论引用文件与行号）。')
                + capRow('fsAssistantStatusWrite', '状态变更', data.assistant_status_write,
                         '开启后助手能把已建卡从 暂缓 改为 待开发（仅此一条转换，约 1 分钟后被调度器纳入开发）。')
                + capRow('fsAssistantReqCards', '需求卡清单卡片', data.assistant_req_cards,
                         '开启后助手能按项目发出需求卡清单飞书交互卡片（编号 / 标题 / 状态 / 流程 / 更新时间）。')
                + capRow('fsAssistantGitOps', '代码拉取与提交', data.assistant_git_ops,
                         '开启后助手能按项目拉取当前分支最新代码、处理合并冲突、预览后提交改动（仅限项目清单内项目，不 push、不切换分支）。')
                + '</div>';
            const result = '<div class="sd-note" id="fsResult"></div>';
            return note + warns + rt + toolbar + fields + caps + result;
        }
        function fsErr(msg) {
            const el = document.getElementById('fsError');
            if (el) el.textContent = msg || '';
        }
        function fsResult(html) {
            const el = document.getElementById('fsResult');
            if (el) el.innerHTML = html;
        }
        async function loadFeishuSettings(force) {
            // 「通知渠道」tab 内的飞书小节容器（表单本身与 #22 一致，只是挂到小节里）
            const body = document.getElementById('settingsFeishuForm');
            if (feishuSettingsLoaded && !force) return;
            if (feishuSettingsLoading) return;
            feishuSettingsLoading = true;
            body.innerHTML = '<div class="sd-note">飞书设置加载中…</div>';
            try {
                const res = await fetch('/api/feishu-settings', {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                fsBase = d;
                feishuSettingsLoaded = true;
                body.innerHTML = feishuSettingsFormHtml(d);
            } catch (e) {
                feishuSettingsLoaded = false;
                body.innerHTML = '<div class="sd-warn">飞书设置加载失败：' + escapeHtml(String((e && e.message) || e)) + '</div>'
                    + '<button type="button" class="btn" id="fsRetryBtn">重试</button>';
            } finally {
                feishuSettingsLoading = false;
            }
        }
        function readFeishuSecretInput() {
            const el = document.getElementById('fsAppSecret');
            return el ? String(el.value == null ? '' : el.value) : '';
        }
        async function saveFeishuSettings() {
            fsErr('');
            fsResult('');
            const appIdEl = document.getElementById('fsAppId');
            const appId = appIdEl ? String(appIdEl.value == null ? '' : appIdEl.value).trim() : '';
            const clearEl = document.getElementById('fsClearSecret');
            const clearSecret = !!(clearEl && clearEl.checked);
            const secret = readFeishuSecretInput();
            const bodyReq = {app_id: appId};
            if (secret) bodyReq.app_secret = secret;
            if (clearSecret) bodyReq.clear_secret = true;
            // #24 助手模型：始终提交下拉当前值（'' = 清除/未配置）；服务端严格校验可用性
            const amEl = document.getElementById('fsAssistantModel');
            bodyReq.assistant_model = amEl ? String(amEl.value == null ? '' : amEl.value) : '';
            // #26/#30/#52/#54 能力开关：始终提交五个布尔值（元素缺失按未勾选处理，不抛错）
            const capFileEl = document.getElementById('fsAssistantFileTools');
            const capStatusEl = document.getElementById('fsAssistantStatusWrite');
            const capCardsEl = document.getElementById('fsAssistantReqCards');
            const capGitEl = document.getElementById('fsAssistantGitOps');
            const capVisionEl = document.getElementById('fsAssistantVision');
            bodyReq.assistant_file_tools = !!(capFileEl && capFileEl.checked);
            bodyReq.assistant_status_write = !!(capStatusEl && capStatusEl.checked);
            bodyReq.assistant_req_cards = !!(capCardsEl && capCardsEl.checked);
            bodyReq.assistant_git_ops = !!(capGitEl && capGitEl.checked);
            bodyReq.assistant_vision = !!(capVisionEl && capVisionEl.checked);
            const btn = document.getElementById('fsSaveBtn');
            if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
            try {
                const res = await fetch('/api/feishu-settings', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(bodyReq),
                });
                const d = await res.json().catch(() => ({}));
                if (!res.ok) { fsErr(d.error || ('保存失败（HTTP ' + res.status + '）')); return; }
                // #23：保存成功只提示 + 重拉状态，**绝不自动调用重启端点**（静默自动重启明确不做）
                showToast(d.restart_required ? '已保存（需重启调度器生效）' : '已保存并已生效');
                await loadFeishuSettings(true);
            } catch (e) {
                fsErr('保存失败：' + ((e && e.message) || e));
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = '保存'; }
            }
        }
        async function fetchFeishuRuntime() {
            // #23 只取运行态、**不重绘表单**：重启后的等待窗口用它轮询——整表单重绘会拆掉
            // 状态区/按钮/成功提示（评审必改 2 的根因）。
            try {
                const res = await fetch('/api/feishu-settings', {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) return null;
                fsBase = d;
                return (d && d.runtime) || null;
            } catch (e) {
                return null;
            }
        }
        async function restartFeishuDispatcher() {
            // #23 一键重启调度器使其加载新凭据：window.confirm 二次确认 → POST（服务端按同一口径
            // 再判一次有运行中 agent 即 409）→ 轮询 `/api/feishu-settings` 直到 `connected` 或超时。
            // 轮询**不因 connecting/failed 提前停**：重启窗口内调度器先是 connecting，那正是「已连接」
            // 的中间态；等待期间不重绘表单，成功提示与按钮状态保留到最后。
            fsErr('');
            if (!window.confirm('确认重启调度器？重启会中断调度器当前的处理，'
                + '有运行中 agent 时会被拒绝。')) return;
            const btn = document.getElementById('fsRestartBtn');
            if (btn) { btn.disabled = true; btn.textContent = '重启中…'; }
            fsResult('<span class="sd-note">正在重启调度器并等待新心跳…</span>');
            try {
                const res = await fetch('/api/feishu-settings/restart', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({confirm: true}),
                });
                const d = await res.json().catch(() => ({}));
                if (!res.ok) {
                    fsErr(d.error || ('重启失败（HTTP ' + res.status + '）'));
                    fsResult('');
                    return;
                }
                const detail = String(d.detail || '调度器已重启');
                let rt = null;
                const deadline = Date.now() + FEISHU_RESTART_WAIT_MS;
                while (Date.now() < deadline) {
                    rt = await fetchFeishuRuntime();
                    if (rt && rt.state === 'connected') break;
                    await new Promise(r => setTimeout(r, 1500));
                }
                await loadFeishuSettings(true);   // 收尾整表单重绘（按钮可见性随之刷新）
                const ok = !!(rt && rt.state === 'connected');
                fsResult('<span class="sd-note">调度器已重启：' + escapeHtml(detail) + '；'
                    + (ok ? '长连接已建立（已连接）。'
                          : '尚未收到「已连接」心跳（当前：'
                            + escapeHtml(String((rt && (rt.reason || rt.state)) || '无状态'))
                            + '），点「重新加载」可查看最新状态。') + '</span>');
            } catch (e) {
                fsErr('重启失败：' + ((e && e.message) || e));
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = '重启调度器使其生效'; }
            }
        }
        async function testFeishuSettings() {
            fsErr('');
            fsResult('测试中…');
            const appIdEl = document.getElementById('fsAppId');
            const appId = appIdEl ? String(appIdEl.value == null ? '' : appIdEl.value).trim() : '';
            const secret = readFeishuSecretInput();
            const bodyReq = {};
            if (appId) bodyReq.app_id = appId;
            if (secret) bodyReq.app_secret = secret;
            try {
                const res = await fetch('/api/feishu-settings/test', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(bodyReq),
                });
                const d = await res.json().catch(() => ({}));
                const codeTxt = (d.code === null || d.code === undefined) ? 'null' : String(d.code);
                const cls = d.ok ? 'sd-note' : 'sd-warn';
                fsResult('<span class="' + cls + '">测试' + (d.ok ? '成功' : '失败')
                    + '：code=' + escapeHtml(codeTxt) + '，' + escapeHtml(String(d.msg == null ? '' : d.msg))
                    + (d.expire != null ? ('（expire=' + escapeHtml(String(d.expire)) + '）') : '') + '</span>'
                    + '<div class="sd-note">仅验证 tenant_access_token（凭据接口可用），'
                    + '不代表 WebSocket 长连接已建立——长连接状态见上方「通道状态」。</div>');
            } catch (e) {
                fsResult('<span class="sd-warn">测试失败：' + escapeHtml(String((e && e.message) || e)) + '</span>');
            }
        }
        // ---- 可插拔通知渠道（Webhook / Slack）：GET/POST /api/notifier-settings + POST /api/notifier-settings/test ----
        // 地址与 secret 按密钥对待：服务端只回 *_configured + 脱敏 host，输入框留空 = 保留旧值。
        const NTF_CHANNELS = [
            {id: 'webhook', key: 'Webhook', name: 'Webhook', kind: '仅出站 · JSON POST',
             note: '把事件以 JSON POST 到你的地址（字段：event / title / text / requirement / details）。'
                 + '填写签名 secret 后附带 X-Loop-Timestamp 与 X-Loop-Signature（HMAC-SHA256）请求头，接收方可据此校验来源。',
             urlPh: 'https://example.com/hooks/loop', secret: true},
            {id: 'slack', key: 'Slack', name: 'Slack', kind: '仅出站 · Incoming Webhook',
             note: '在 Slack 应用里开启 Incoming Webhooks，把频道的 webhook URL 填到这里；'
                 + '消息带「在看板中打开」按钮，链接到对应需求卡。',
             urlPh: 'https://hooks.slack.com/services/…', secret: false},
        ];
        function ntfChannelHtml(meta, c, events) {
            const ch = c || {};
            const k = meta.key;
            const on = !!ch.enabled;
            const err = ch.error ? String(ch.error) : '';
            const stCls = err ? 'fs-off' : (on ? 'fs-ok' : 'fs-warn');
            const stLabel = err ? '配置非法，已停用' : (on ? '已启用' : '未启用');
            const st = '<div class="fs-status ' + stCls + '" id="ntf' + k + 'Status">'
                + '<b>渠道状态：' + escapeHtml(stLabel) + '</b>'
                + (err ? '<div>原因：' + escapeHtml(err) + '</div>' : '')
                + '<div>地址：' + (ch.url_configured ? escapeHtml(String(ch.url_hint || '已配置')) : '未配置') + '</div>'
                + '</div>';
            const toolbar = '<div class="ms-top">'
                + '<button type="button" class="btn btn-primary" data-ntf-save="' + meta.id + '">保存</button>'
                + '<button type="button" class="btn" data-ntf-test="' + meta.id + '">发送测试</button>'
                + '<div class="ms-error" id="ntf' + k + 'Error"></div></div>';
            let fields = '<div class="ms-fields">'
                + '<div class="ms-field ntf-wide"><label>Webhook URL</label>'
                + '<input id="ntf' + k + 'Url" type="password" value="" autocomplete="off" spellcheck="false" placeholder="'
                + escapeAttr(ch.url_configured ? '已配置（留空=保留）' : meta.urlPh) + '"></div>';
            if (meta.secret) {
                fields += '<div class="ms-field"><label>签名 secret（可选）</label>'
                    + '<input id="ntf' + k + 'Secret" type="password" value="" autocomplete="off" placeholder="'
                    + (ch.secret_configured ? '已配置（留空=保留）' : '未配置') + '"></div>';
            }
            fields += '<div class="ms-field"><label>超时（秒）</label>'
                + '<input id="ntf' + k + 'Timeout" type="number" min="1" max="30" step="1" value="'
                + escapeAttr(String(ch.timeout == null ? 5 : ch.timeout)) + '"></div>'
                + '</div>';
            let clears = '<div class="ntf-clears">'
                + '<label class="ms-clear-wrap"><input type="checkbox" id="ntf' + k + 'ClearUrl"'
                + (ch.url_configured ? '' : ' disabled') + '>清除已配置地址</label>';
            if (meta.secret) {
                clears += '<label class="ms-clear-wrap"><input type="checkbox" id="ntf' + k + 'ClearSecret"'
                    + (ch.secret_configured ? '' : ' disabled') + '>清除已配置 secret</label>';
            }
            clears += '</div>';
            const sub = Array.isArray(ch.events) ? ch.events : [];
            const evs = '<div class="ntf-events-title">推送事件</div><div class="ntf-events">'
                + (events || []).map(e => '<label class="ms-clear-wrap"><input type="checkbox" data-ntf-event="'
                    + meta.id + '" value="' + escapeAttr(String(e.id)) + '"'
                    + (sub.indexOf(e.id) >= 0 ? ' checked' : '') + '>' + escapeHtml(String(e.label)) + '</label>').join('')
                + '</div>';
            return '<section class="sd-group ntf-channel" id="ntf' + k + '">'
                + '<div class="ntf-head"><h3>' + escapeHtml(meta.name) + '</h3>'
                + '<span class="ntf-kind">' + escapeHtml(meta.kind) + '</span>'
                + '<label class="ntf-enable"><span>启用</span><input type="checkbox" id="ntf' + k + 'Enabled"'
                + (ch.configured_enabled ? ' checked' : '') + '></label></div>'
                + '<div class="sd-note">' + escapeHtml(meta.note) + '</div>'
                + st + toolbar + fields + clears + evs
                + '<div class="sd-note ntf-result" id="ntf' + k + 'Result"></div>'
                + '</section>';
        }
        function notifierSettingsHtml(d) {
            const data = d || {};
            const warns = (data.warnings && data.warnings.length)
                ? '<div class="sd-warn">' + data.warnings.map(w => escapeHtml(String(w))).join('<br>') + '</div>' : '';
            const chans = data.channels || {};
            const general = '<section class="sd-group ntf-channel" id="ntfGeneral">'
                + '<div class="ntf-head"><h3>看板地址</h3><span class="ntf-kind">通知里的卡片链接</span></div>'
                + '<div class="sd-note">Webhook / Slack 渠道配置保存在 <code>' + escapeHtml(String(data.path || '')) + '</code>'
                + '（0600，不进 git）；调度器检测到文件变化自动重载，<b>保存即生效</b>、无需重启。'
                + '通知里的卡片链接以下面的看板地址为前缀，留空 = 默认 <code>'
                + escapeHtml(String(data.board_url_default || '')) + '</code></div>'
                + warns
                + '<div class="ms-fields"><div class="ms-field ntf-wide"><label>看板地址</label>'
                + '<input id="ntfBoardUrl" value="' + escapeAttr(String(data.board_url || '')) + '" placeholder="'
                + escapeAttr(String(data.board_url_default || '')) + '" autocomplete="off" spellcheck="false"></div></div>'
                + '<div class="ms-top"><button type="button" class="btn" data-ntf-save="general">保存看板地址</button>'
                + '<div class="ms-error" id="ntfGeneralError"></div></div>'
                + '</section>';
            return NTF_CHANNELS.map(m => ntfChannelHtml(m, chans[m.id], data.events)).join('') + general;
        }
        async function loadNotifierSettings(force) {
            const body = document.getElementById('settingsNotifierBody');
            if (!body) return;
            if (notifierSettingsLoaded && !force) return;
            if (notifierSettingsLoading) return;
            notifierSettingsLoading = true;
            body.innerHTML = '<div class="sd-note">通知渠道加载中…</div>';
            try {
                const res = await fetch('/api/notifier-settings', {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                notifierSettingsLoaded = true;
                body.innerHTML = notifierSettingsHtml(d);
            } catch (e) {
                notifierSettingsLoaded = false;
                body.innerHTML = '<div class="sd-warn">通知渠道加载失败：' + escapeHtml(String((e && e.message) || e)) + '</div>'
                    + '<button type="button" class="btn" id="ntfRetryBtn">重试</button>';
            } finally {
                notifierSettingsLoading = false;
            }
        }
        function ntfMeta(id) { return NTF_CHANNELS.find(m => m.id === id) || null; }
        function ntfVal(id) {
            const el = document.getElementById(id);
            return el ? String(el.value == null ? '' : el.value).trim() : '';
        }
        function ntfChecked(id) {
            const el = document.getElementById(id);
            return !!(el && el.checked);
        }
        function ntfSetText(id, text) {
            const el = document.getElementById(id);
            if (el) el.textContent = text || '';
        }
        async function saveNotifierChannel(id, btn) {
            let bodyReq;
            let errId;
            if (id === 'general') {
                errId = 'ntfGeneralError';
                bodyReq = {channel: 'general', board_url: ntfVal('ntfBoardUrl')};
            } else {
                const m = ntfMeta(id);
                if (!m) return;
                const k = m.key;
                errId = 'ntf' + k + 'Error';
                bodyReq = {channel: id, enabled: ntfChecked('ntf' + k + 'Enabled')};
                const url = ntfVal('ntf' + k + 'Url');
                if (url) bodyReq.url = url;
                if (ntfChecked('ntf' + k + 'ClearUrl')) bodyReq.clear_url = true;
                if (m.secret) {
                    const sec = ntfVal('ntf' + k + 'Secret');
                    if (sec) bodyReq.secret = sec;
                    if (ntfChecked('ntf' + k + 'ClearSecret')) bodyReq.clear_secret = true;
                }
                const tmo = Number(ntfVal('ntf' + k + 'Timeout'));
                if (tmo) bodyReq.timeout = tmo;
                bodyReq.events = Array.from(document.querySelectorAll('input[data-ntf-event="' + id + '"]'))
                    .filter(x => x.checked).map(x => x.value);
            }
            ntfSetText(errId, '');
            const label = btn ? btn.textContent : '';
            if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
            try {
                const res = await fetch('/api/notifier-settings', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(bodyReq),
                });
                const d = await res.json().catch(() => ({}));
                if (!res.ok) { ntfSetText(errId, d.error || ('保存失败（HTTP ' + res.status + '）')); return; }
                showToast(d.changed ? '已保存（调度器自动重载，无需重启）' : '没有改动');
                await loadNotifierSettings(true);
            } catch (e) {
                ntfSetText(errId, '保存失败：' + ((e && e.message) || e));
            } finally {
                if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = label; }
            }
        }
        async function testNotifierChannel(id) {
            const m = ntfMeta(id);
            if (!m) return;
            const k = m.key;
            const out = document.getElementById('ntf' + k + 'Result');
            ntfSetText('ntf' + k + 'Error', '');
            if (out) out.textContent = '测试中…';
            const bodyReq = {channel: id};
            const url = ntfVal('ntf' + k + 'Url');
            if (url) bodyReq.url = url;
            if (m.secret) {
                const sec = ntfVal('ntf' + k + 'Secret');
                if (sec) bodyReq.secret = sec;
            }
            try {
                const res = await fetch('/api/notifier-settings/test', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(bodyReq),
                });
                const d = await res.json().catch(() => ({}));
                const ok = !!(res.ok && d.ok);
                const msg = String(d.msg || d.error || ('HTTP ' + res.status));
                if (out) {
                    out.innerHTML = '<span class="' + (ok ? 'sd-note' : 'sd-warn') + '">'
                        + escapeHtml(ok ? '测试消息已发送' : '测试失败') + '：' + escapeHtml(msg) + '</span>';
                }
            } catch (e) {
                if (out) out.innerHTML = '<span class="sd-warn">测试失败：' + escapeHtml(String((e && e.message) || e)) + '</span>';
            }
        }
        document.getElementById('settingsFeishuBody').addEventListener('click', (e) => {
            const t = e.target && e.target.closest ? e.target.closest('button') : null;
            if (!t) return;
            if (t.id === 'fsSaveBtn') { saveFeishuSettings(); return; }
            if (t.id === 'fsTestBtn') { testFeishuSettings(); return; }
            if (t.id === 'fsRestartBtn') { restartFeishuDispatcher(); return; }
            if (t.id === 'fsRetryBtn') { loadFeishuSettings(true); return; }
            if (t.id === 'ntfRetryBtn') { loadNotifierSettings(true); return; }
            if (t.dataset && t.dataset.ntfSave) { saveNotifierChannel(t.dataset.ntfSave, t); return; }
            if (t.dataset && t.dataset.ntfTest) { testNotifierChannel(t.dataset.ntfTest); return; }
        });
        document.getElementById('settingsGearBtn').addEventListener('click', openSettingsDrawer);
        document.getElementById('sdTabGlobal').addEventListener('click', () => switchSettingsTab('global'));
        document.getElementById('sdTabModel').addEventListener('click', () => switchSettingsTab('model'));
        document.getElementById('sdTabFeishu').addEventListener('click', () => switchSettingsTab('feishu'));
        document.getElementById('settingsMask').addEventListener('click', closeSettingsDrawer);
        document.getElementById('settingsCloseBtn').addEventListener('click', closeSettingsDrawer);
        document.getElementById('settingsReloadBtn').addEventListener('click', reloadSettingsDrawer);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeSettingsDrawer();
        });

        // ==================== 上下文编译器界面（context-design 第八节）：卡片「上下文」页 + 注入预览 ====================
        // 数据：GET /api/requirements/<id>/context/runs[/<run_key>[/prompt]]（执行档案）、GET /api/context/preview（dry 编译）。
        // 分层渲染（堆叠条 + 图例 + 警告 + 按层分组的条目）由纯函数 ctx*Html 产出，卡片上下文页与注入预览共用；
        // 层色只经 board.css 令牌 --layer-0..7 / --layer-rt 消费。提示词/文件内容一律 data-i18n-skip（不翻译）。
        const CTX_LAYERS = {
            L0: {name: '平台策略', nature: '指令 · 稳定前缀', cls: 'cx-l0'},
            L1: {name: '角色', nature: '指令 · 稳定前缀', cls: 'cx-l1'},
            L2: {name: '项目约定', nature: '指令 · 稳定前缀', cls: 'cx-l2'},
            L3: {name: '条件片段', nature: '指令 · 稳定前缀', cls: 'cx-l3'},
            L4: {name: '常驻技能', nature: '指令 · 稳定前缀', cls: 'cx-l4'},
            L5: {name: '技能目录', nature: '索引 · 稳定前缀末尾', cls: 'cx-l5'},
            L6: {name: '任务数据', nature: '数据 · 围栏内，不是指令', cls: 'cx-l6'},
            L7: {name: '运行参数', nature: '指令 · 调度器生成', cls: 'cx-l7'},
            runtime: {name: '运行时文案', nature: '续跑 / 收尾 / 补发', cls: 'cx-lrt'},
        };
        const CTX_LAYER_ORDER = ['L0', 'L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7', 'runtime'];
        const CTX_STABLE_LAYERS = ['L0', 'L1', 'L2', 'L3', 'L4', 'L5'];
        const CTX_KIND_LABEL = {new: '新会话', resume: '续跑', followup: '续接轮', preview: '预览'};
        const CTX_STATUS = {
            running: ['进行中', 'cx-tone-blue'], done: ['完成', 'cx-tone-green'], failed: ['失败', 'cx-tone-red'],
            timed_out: ['超时', 'cx-tone-red'], aborted: ['中止', 'cx-tone-amber'],
        };
        function ctxFmt(n) {
            return Number(n || 0).toLocaleString('en-US');
        }
        function ctxLayerOf(layer) {
            return CTX_LAYERS[layer] ? layer : 'runtime';
        }
        function ctxShortHash(h) {
            const s = String(h || '');
            return s.length > 8 ? s.slice(0, 4) + '…' + s.slice(-4) : (s || '—');
        }
        // 条目按层分组（层序固定 L0..L7 + 运行时文案），组内保持清单原序
        function ctxGroupItems(items) {
            const by = {};
            (items || []).forEach(it => {
                if (!it || typeof it !== 'object') return;
                const k = ctxLayerOf(it.layer);
                (by[k] = by[k] || []).push(it);
            });
            return CTX_LAYER_ORDER.filter(k => by[k]).map(k => ({
                layer: k, items: by[k], sum: by[k].reduce((a, x) => a + (Number(x.chars) || 0), 0),
            }));
        }
        function ctxStableSum(groups) {
            return (groups || []).filter(g => CTX_STABLE_LAYERS.includes(g.layer)).reduce((a, g) => a + g.sum, 0);
        }
        // 分层堆叠条：宽度按字数占比；最后一个稳定前缀层右侧画缓存断点（.cx-cut）
        function ctxStackBarHtml(groups) {
            const total = (groups || []).reduce((a, g) => a + g.sum, 0) || 1;
            let last = -1;
            (groups || []).forEach((g, i) => { if (CTX_STABLE_LAYERS.includes(g.layer)) last = i; });
            const segs = (groups || []).map((g, i) => {
                const L = CTX_LAYERS[g.layer];
                const pct = Math.max(g.sum / total * 100, g.sum ? 0.6 : 0).toFixed(2);
                return `<div class="cx-seg ${L.cls}${i === last ? ' cx-cut' : ''}" style="width:${pct}%" title="${escapeAttr(g.layer + ' ' + L.name + '：' + ctxFmt(g.sum) + ' 字')}"></div>`;
            }).join('');
            return `<div class="cx-bar" role="img" aria-label="各层字数占比">${segs}</div>`;
        }
        function ctxLegendHtml(groups) {
            return '<div class="cx-legend">' + (groups || []).map(g => {
                const L = CTX_LAYERS[g.layer];
                return `<span><i class="cx-dot ${L.cls}" aria-hidden="true"></i>${escapeHtml(g.layer === 'runtime' ? '' : g.layer + ' ')}${escapeHtml(L.name)} <span class="cx-mono cx-dim">${ctxFmt(g.sum)}</span></span>`;
            }).join('') + '</div>';
        }
        // 警告横幅：带去处（缺技能 → 技能库该项；截断 → 该条目 / 对应提示词；覆盖基于旧默认 → 提示词 tab）
        function ctxWarnTarget(w) {
            const id = String((w && w.id) || '');
            if (id.startsWith('skill:')) return {act: 'skill', arg: id.slice(6), label: '去技能库查看'};
            if (w && w.code === 'stale_override' && id) return {act: 'prompt', arg: id, label: '去提示词处理'};
            if (id.startsWith('guide:')) return {act: 'item', arg: id, label: '查看该指引'};
            if (id) return {act: 'item', arg: id, label: '定位该条目'};
            return null;
        }
        function ctxWarningsHtml(warnings) {
            return (warnings || []).map(w => {
                const code = (w && w.code) || '';
                const tone = (code === 'missing_required' || code === 'manifest_error') ? 'cx-banner-bad'
                    : (code === 'missing_skill' || code === 'truncated' || code === 'stale_override') ? 'cx-banner-warn' : 'cx-banner-info';
                const t = ctxWarnTarget(w);
                const go = t ? ` <button type="button" class="cx-link" data-cx-act="${t.act}" data-cx-arg="${escapeAttr(t.arg)}">${escapeHtml(t.label)} →</button>` : '';
                // 告警文案来自编译器（含路径与数字），整句不进翻译层，避免中英夹杂
                return `<div class="cx-banner ${tone}"><span data-i18n-skip>${escapeHtml(w.message || code)}</span>${go}</div>`;
            }).join('');
        }
        function ctxOriginLabel(origin) {
            return {default: '默认', override: '本机覆盖', project: '项目', engine: '引擎', card: '卡片', runtime: '调度器'}[origin] || origin || '—';
        }
        function ctxItemBadges(it, mark) {
            let b = '';
            if (Number(it.truncated) > 0) b += `<span class="cx-pill cx-tone-amber">截断 ${ctxFmt(it.truncated)} 字</span>`;
            if (it.origin === 'override') b += '<span class="cx-pill cx-tone-blue">本机覆盖</span>';
            if (ctxLayerOf(it.layer) === 'L6') b += '<span class="cx-pill cx-tone-fence">数据围栏</span>';
            if (/^auto/.test(String(it.why || ''))) b += '<span class="cx-pill cx-tone-violet">自动触发</span>';
            if (mark === 'changed') b += '<span class="cx-pill cx-tone-blue cx-strong">变更</span>';
            if (mark === 'added') b += '<span class="cx-pill cx-tone-green cx-strong">新增</span>';
            return b;
        }
        // 按层分组的条目列表。opts = {open: {id: true}, diff: {changed, added, removed}|null, excerpts: {id: {text}|{error}|'loading'}}
        function ctxItemsHtml(groups, opts) {
            const o = opts || {};
            const open = o.open || {};
            const diff = o.diff || null;
            const mark = id => !diff ? '' : (diff.added || []).includes(id) ? 'added' : (diff.changed || []).includes(id) ? 'changed' : '';
            let h = '<div class="cx-groups">' + (groups || []).map(g => {
                const L = CTX_LAYERS[g.layer];
                const head = `<div class="cx-ghead"><i class="cx-dot ${L.cls}" aria-hidden="true"></i>`
                    + (g.layer === 'runtime' ? '' : `<span class="cx-mono cx-dim">${g.layer}</span>`)
                    + `<b>${escapeHtml(L.name)}</b><span class="cx-note">${escapeHtml(L.nature)}</span>`
                    + `<span class="cx-spacer"></span><span class="cx-mono cx-dim">${ctxFmt(g.sum)} 字</span></div>`;
                return `<div class="cx-group">${head}` + g.items.map(it => {
                    const id = String(it.id || '');
                    const isOpen = !!open[id];
                    const btn = `<button type="button" class="cx-item-btn" data-cx-act="toggle" data-cx-arg="${escapeAttr(id)}" aria-expanded="${isOpen}">`
                        + `<span class="cx-caret" aria-hidden="true">${isOpen ? '▼' : '▶'}</span>`
                        + `<b class="cx-item-title">${escapeHtml(it.title || id)}</b>`
                        + `<span class="cx-mono cx-src" data-i18n-skip title="${escapeAttr(it.src || '')}">${escapeHtml(it.src || '')}</span>`
                        + ctxItemBadges(it, mark(id))
                        + `<span class="cx-mono cx-num">${ctxFmt(it.chars)}</span></button>`;
                    let detail = '';
                    if (isOpen) {
                        const ex = (o.excerpts || {})[id];
                        let exHtml = '';
                        if (ex === 'loading') exHtml = '<div class="cx-note">摘录加载中…</div>';
                        else if (ex && ex.text != null) exHtml = `<div class="cx-note">${escapeHtml(ex.label || '当前文件摘录')}</div><pre class="cx-pre" data-i18n-skip>${escapeHtml(ex.text)}</pre>`;
                        else if (ex && ex.error) exHtml = `<div class="cx-note">${escapeHtml(ex.error)}</div>`;
                        detail = `<div class="cx-detail"><div class="cx-kv">`
                            + `<span>入选理由：<span data-i18n-skip>${escapeHtml(it.why || '—')}</span></span>`
                            + `<span>版本：<span class="cx-mono">${escapeHtml(it.hash || '—')}</span></span>`
                            + `<span>来源：${escapeHtml(ctxOriginLabel(it.origin))}</span>`
                            + `<span>条目：<span class="cx-mono" data-i18n-skip>${escapeHtml(id)}</span></span></div>${exHtml}</div>`;
                    }
                    return `<div class="cx-item" data-cx-item="${escapeAttr(id)}">${btn}${detail}</div>`;
                }).join('') + '</div>';
            }).join('') + '</div>';
            if (diff && (diff.removed || []).length) {
                h += '<div class="cx-removed"><span class="cx-pill cx-tone-red cx-strong">已移除</span> '
                    + diff.removed.map(x => `<span class="cx-mono" data-i18n-skip>${escapeHtml(x)}</span>`).join('、') + '</div>';
            }
            return h;
        }
        // 统计行：执行档案（prev = 同角色上一轮清单，用于前缀一致性判断）
        function ctxStatsHtml(m, prev) {
            const model = (m && m.model) || {};
            const mid = typeof model === 'object' ? model.id : model;
            const msrc = typeof model === 'object' ? model.source : '';
            let cache = ['首轮', 'cx-tone-gray', '同角色无上一轮可比'];
            if (prev && prev.prefix_hash && m.prefix_hash) {
                cache = prev.prefix_hash === m.prefix_hash
                    ? ['与上一轮一致', 'cx-tone-green', '稳定前缀未变，可命中缓存']
                    : ['有变化', 'cx-tone-amber', '稳定前缀与上一轮不同'];
            }
            return '<div class="cx-stats">'
                + `<div><div class="cx-k">模型</div><div class="cx-mono" data-i18n-skip>${escapeHtml(mid || '—')}</div><div class="cx-k">来源：<span data-i18n-skip>${escapeHtml(msrc || '—')}</span></div></div>`
                + `<div><div class="cx-k">运行体</div><div class="cx-mono">${escapeHtml(m.runtime || '—')}</div></div>`
                + `<div><div class="cx-k">提示词总量</div><div class="cx-mono">${ctxFmt(m.chars)} 字</div></div>`
                + `<div><div class="cx-k">前缀缓存</div><div><span class="cx-pill ${cache[1]}">${cache[0]}</span></div><div class="cx-k">${cache[2]}</div></div>`
                + `<div><div class="cx-k">前缀 hash</div><div class="cx-mono" title="${escapeAttr(m.prefix_hash || '')}">${escapeHtml(ctxShortHash(m.prefix_hash))}</div></div>`
                + `<div><div class="cx-k">清单 hash</div><div class="cx-mono" title="${escapeAttr(m.prompt_hash || '')}">${escapeHtml(ctxShortHash(m.prompt_hash))}</div></div>`
                + '</div>';
        }
        function ctxPreviewStatsHtml(m, groups) {
            const total = (groups || []).reduce((a, g) => a + g.sum, 0);
            const stable = ctxStableSum(groups);
            return '<div class="cx-stats">'
                + `<div><div class="cx-k">总量</div><div class="cx-mono cx-big">${ctxFmt(m.chars || total)} 字</div></div>`
                + `<div><div class="cx-k">稳定前缀（可缓存）</div><div class="cx-mono cx-big">${ctxFmt(stable)} 字</div></div>`
                + `<div><div class="cx-k">本卡专有</div><div class="cx-mono cx-big">${ctxFmt(total - stable)} 字</div></div>`
                + `<div><div class="cx-k">前缀 hash</div><div class="cx-mono cx-big" title="${escapeAttr(m.prefix_hash || '')}">${escapeHtml(ctxShortHash(m.prefix_hash))}</div></div>`
                + '</div>';
        }
        // 分层构成块（标题 + 堆叠条 + 图例），两处共用
        function ctxCompositionHtml(groups) {
            return '<div class="cx-col"><div class="cx-row cx-between"><b>分层构成</b>'
                + '<span class="cx-note">竖线左侧 = 稳定前缀（可缓存）· 右侧 = 本卡专有</span></div>'
                + ctxStackBarHtml(groups) + ctxLegendHtml(groups) + '</div>';
        }
        // 流程节点条模型：前置角色（有档案时）+ 卡上流程快照节点（去重、跳过收尾）+ 档案里出现的其它角色
        function ctxNodeList(item, runs, preRole) {
            const out = [];
            const seen = {};
            const add = (role, name) => {
                if (!role || seen[role]) return;
                seen[role] = true;
                out.push({role: role, name: name || ''});
            };
            const rs = runs || [];
            if (preRole && rs.some(r => r.role === preRole)) add(preRole, '');
            let nodes = [];
            try { nodes = JSON.parse((item && item.nodes_json) || '[]'); } catch (e) { nodes = []; }
            (Array.isArray(nodes) ? nodes : []).forEach(n => {
                if (n && typeof n === 'object' && n.node_type !== 'terminal' && n.role) add(n.role, n.name || '');
            });
            rs.slice().reverse().forEach(r => add(r.role, ''));
            return out.map(n => {
                const mine = rs.filter(r => r.role === n.role);
                return {role: n.role, name: n.name, runs: mine.length, latest: mine[0] || null};
            });
        }
        // 某角色的各轮：按时间正序编号（第 1 轮…），列表新 → 旧
        function ctxRoleRounds(runs, role) {
            const mine = (runs || []).filter(r => r.role === role);
            const n = mine.length;
            return mine.map((r, i) => Object.assign({}, r, {round: n - i}));
        }
        function ctxStatusPill(status) {
            const s = CTX_STATUS[status] || [status || '—', 'cx-tone-gray'];
            return `<span class="cx-pill ${s[1]}">${escapeHtml(s[0])}</span>`;
        }

        // ---------------- 卡片「上下文」页（右栏需求详情的第二个分页） ----------------
        let detailTab = 'summary';   // 需求详情分页：summary（概要）| context（上下文）；跨卡保持
        let ctxState = null;          // {req, runs, loading, error, role, runKey, manifests, diff, open, full, prompts, excerpts}
        function ctxFreshState(reqId) {
            return {req: reqId, runs: null, loading: false, error: '', role: null, runKey: null, manifests: {},
                    mErr: {}, diff: true, open: {}, full: false, prompts: {}, excerpts: {}};
        }
        function setDetailTab(tab) {
            detailTab = tab === 'context' ? 'context' : 'summary';
            renderDetailPane();
        }
        function detailTabsHtml(i) {
            const warn = ctxState && ctxState.req === i.id && ctxState.runs && ctxState.runs[0] ? Number(ctxState.runs[0].warnings || 0) : 0;
            const tab = (id, label, extra) => `<button type="button" class="cx-tab${detailTab === id ? ' on' : ''}" role="tab" aria-selected="${detailTab === id}" onclick="setDetailTab('${id}')">${label}${extra || ''}</button>`;
            return `<div class="cx-tabs" role="tablist" aria-label="详情分页">${tab('summary', '概要')}${tab('context', '上下文', warn ? ` <span class="cx-pill cx-tone-amber" title="最近一轮的警告数">${warn}</span>` : '')}</div>`;
        }
        // renderDetailPane 渲染完需求详情后调用：上下文分页挂载（按需拉档案列表），概要分页拉评审记录
        function afterDetailRender(item) {
            if (detailTab === 'context') ctxMount(item);
            else loadReviews(item.id);
        }
        function ctxMount(item) {
            if (!ctxState || ctxState.req !== item.id) ctxState = ctxFreshState(item.id);
            renderCtxPane();
            if (ctxState.runs === null && !ctxState.loading) ctxLoadRuns(item.id);
        }
        async function ctxLoadRuns(reqId) {
            const st = ctxState;
            st.loading = true;
            st.error = '';
            try {
                const res = await fetch(`/api/requirements/${reqId}/context/runs`, {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                st.runs = Array.isArray(d.runs) ? d.runs : [];
                if (!st.role) {
                    const item = items.find(x => x.id === reqId);
                    st.role = st.runs[0] ? st.runs[0].role : (item ? currentNodeRole(item) : null);
                }
                if (!st.runKey) {
                    const first = st.runs.find(r => r.role === st.role);
                    st.runKey = first ? first.run_key : null;
                }
            } catch (e) {
                st.runs = [];
                st.error = String((e && e.message) || e);
            } finally {
                st.loading = false;
            }
            if (ctxState !== st) return;   // 迟到响应：已切卡
            renderCtxPane();
            if (st.runKey) ctxLoadManifest(st.runKey);
        }
        async function ctxLoadManifest(runKey) {
            const st = ctxState;
            if (!st || st.manifests[runKey]) { renderCtxPane(); return; }
            try {
                const res = await fetch(`/api/requirements/${st.req}/context/runs/${encodeURIComponent(runKey)}`, {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                st.manifests[runKey] = d;
                if (d.prev_run_key && !st.manifests[d.prev_run_key]) {
                    // 上一轮清单只用于前缀一致性对比：后台补拉，不阻塞当前渲染
                    fetch(`/api/requirements/${st.req}/context/runs/${encodeURIComponent(d.prev_run_key)}`, {cache: 'no-store'})
                        .then(r => r.ok ? r.json() : null)
                        .then(p => { if (p && ctxState === st) { st.manifests[d.prev_run_key] = p; renderCtxPane(); } })
                        .catch(() => {});
                }
            } catch (e) {
                st.mErr[runKey] = String((e && e.message) || e);
            }
            if (ctxState === st) renderCtxPane();
        }
        async function ctxLoadPrompt(runKey) {
            const st = ctxState;
            if (!st || st.prompts[runKey] != null) return;
            st.prompts[runKey] = {loading: true};
            renderCtxPane();
            try {
                const res = await fetch(`/api/requirements/${st.req}/context/runs/${encodeURIComponent(runKey)}/prompt`, {cache: 'no-store'});
                const t = await res.text();
                if (!res.ok) {
                    let msg = 'HTTP ' + res.status;
                    try { msg = JSON.parse(t).error || msg; } catch (e) { /* 非 JSON 错误体 */ }
                    throw new Error(msg);
                }
                st.prompts[runKey] = {text: t};
            } catch (e) {
                st.prompts[runKey] = {error: String((e && e.message) || e)};
            }
            if (ctxState === st) renderCtxPane();
        }
        // 条目摘录：提示词存储条目取 /api/prompts/<id>，技能取 /api/skills/<名>；其它条目只给来源（全文见「实际发出的全文」）
        function ctxExcerptSource(it, project) {
            const id = String((it && it.id) || '');
            if (/^(system|fragment|role)\//.test(id)) return {url: '/api/prompts/' + id, pick: d => d.text};
            if (id.startsWith('skill:')) {
                const q = it.origin === 'project' && project ? '?project=' + encodeURIComponent(project) : '';
                return {url: '/api/skills/' + encodeURIComponent(id.slice(6)) + q, pick: d => d.body};
            }
            const g = id.match(/^guide:([^/]+)\/(.+)$/);
            if (g && project) return {url: '/api/prompts/project/' + project + '/' + g[1] + '/guide/' + g[2], pick: d => d.text};
            return null;
        }
        async function ctxLoadExcerpt(holder, it, project, rerender) {
            const id = String(it.id || '');
            if (holder[id] !== undefined) return;
            const srcDef = ctxExcerptSource(it, project);
            if (!srcDef) {
                holder[id] = {error: '该条目由调度器按卡片生成，没有独立文件；完整内容见「查看实际发出的全文」。'};
                rerender();
                return;
            }
            holder[id] = 'loading';
            rerender();
            try {
                const res = await fetch(srcDef.url, {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                const text = String(srcDef.pick(d) || '');
                holder[id] = {text: text.length > 1200 ? text.slice(0, 1200) + '\n…' : text,
                              label: '当前文件摘录（该轮实际发出的版本见全文；版本 hash 不同即已改动）'};
            } catch (e) {
                holder[id] = {error: '摘录加载失败：' + String((e && e.message) || e)};
            }
            rerender();
        }
        function ctxPaneHtml(item) {
            const st = ctxState;
            if (!st || st.req !== item.id) return '';
            if (st.runs === null || st.loading) return '<div class="cx-note">执行档案加载中…</div>';
            let h = '';
            if (st.error) h += `<div class="cx-banner cx-banner-bad"><span>执行档案加载失败：${escapeHtml(st.error)}</span> <button type="button" class="cx-link" data-cx-act="reload">重试</button></div>`;
            const nodes = ctxNodeList(item, st.runs, PRE_ROLE);
            h += '<section class="cx-col"><div class="cx-note">流程节点（点选查看每一轮收到的上下文）</div><div class="cx-row cx-wrap">'
                + nodes.map(n => {
                    const latest = n.latest;
                    const sub = n.runs ? `${n.runs} 轮 · ${(CTX_STATUS[latest.status] || [latest.status || '—'])[0]}` : '未开始';
                    const tone = !n.runs ? 'cx-sub-idle' : (CTX_STATUS[latest.status] || ['', 'cx-tone-gray'])[1].replace('cx-tone-', 'cx-sub-');
                    return `<button type="button" class="cx-pick${n.role === st.role ? ' on' : ''}" aria-pressed="${n.role === st.role}" data-cx-act="node" data-cx-arg="${escapeAttr(n.role)}"><b>${escapeHtml(n.name || roleLabel(n.role))}</b><span class="cx-sub ${tone}">${escapeHtml(sub)}</span></button>`;
                }).join('') + '</div></section>';
            if (!nodes.length && !st.error) {
                return h + '<div class="cx-empty"><div>这张卡还没有执行档案（尚未拉起过会话，或档案记录未开启）。</div></div>';
            }
            const rounds = ctxRoleRounds(st.runs, st.role);
            if (!rounds.length) {
                return h + `<div class="cx-empty"><div>「${escapeHtml(roleLabel(st.role || ''))}」尚未运行。</div>`
                    + `<button type="button" class="btn btn-sm" data-cx-act="preview">在注入预览里看它将收到什么 →</button></div>`;
            }
            h += '<div class="cx-row cx-wrap">' + rounds.map(r => {
                const on = r.run_key === st.runKey;
                return `<button type="button" class="cx-pick cx-run${on ? ' on' : ''}" aria-pressed="${on}" data-cx-act="run" data-cx-arg="${escapeAttr(r.run_key)}">`
                    + `<span><span class="cx-mono cx-dim" data-i18n-skip>${escapeHtml(r.run_key)}</span></span>`
                    + `<span>第 ${r.round} 轮 · ${escapeHtml(CTX_KIND_LABEL[r.kind] || r.kind || '—')} ${ctxStatusPill(r.status)}`
                    + (Number(r.warnings) ? ` <span class="cx-pill cx-tone-amber">警告 ${Number(r.warnings)}</span>` : '') + '</span></button>';
            }).join('') + '</div>';
            const m = st.manifests[st.runKey];
            if (!m) {
                return h + (st.mErr[st.runKey]
                    ? `<div class="cx-banner cx-banner-bad"><span>清单加载失败：${escapeHtml(st.mErr[st.runKey])}</span></div>`
                    : '<div class="cx-note">清单加载中…</div>');
            }
            const prev = m.prev_run_key ? st.manifests[m.prev_run_key] : null;
            const groups = ctxGroupItems(m.items);
            h += ctxStatsHtml(m, prev);
            h += ctxCompositionHtml(groups);
            h += ctxWarningsHtml(m.warnings);
            if (m.context_update && m.context_update.sent) {
                h += `<div class="cx-banner cx-banner-ok"><span>续跑补发：与上一轮相比 ${(m.context_update.changed || []).length} 项有变化，已在续跑消息前附「上下文更新」。</span></div>`;
            }
            const diffOn = st.diff && !!m.prev_run_key;
            const d = m.diff || {};
            h += '<div class="cx-row cx-wrap">'
                + `<button type="button" class="btn btn-sm${diffOn ? ' cx-on' : ''}" data-cx-act="diff" aria-pressed="${diffOn}"${m.prev_run_key ? '' : ' disabled'}>${m.prev_run_key ? (diffOn ? '对比上一轮：开' : '对比上一轮：关') : '首轮，无可对比'}</button>`
                + `<button type="button" class="btn btn-sm${st.full ? ' cx-on' : ''}" data-cx-act="full" aria-pressed="${st.full}">${st.full ? '返回分层列表' : '查看实际发出的全文'}</button>`
                + (diffOn ? `<span class="cx-note">与 <span class="cx-mono" data-i18n-skip>${escapeHtml(m.prev_run_key)}</span> 相比：变更 ${(d.changed || []).length} · 新增 ${(d.added || []).length} · 已移除 ${(d.removed || []).length}</span>` : '')
                + '</div>';
            if (st.full) {
                const p = st.prompts[st.runKey];
                if (!p || p.loading) h += '<div class="cx-note">全文加载中…</div>';
                else if (p.error) h += `<div class="cx-banner cx-banner-bad"><span>全文加载失败：${escapeHtml(p.error)}</span></div>`;
                else h += `<div class="cx-note">实际发出的提示词（prompt.txt，${ctxFmt(p.text.length)} 字）</div><pre class="cx-pre cx-full" data-i18n-skip>${escapeHtml(p.text)}</pre>`;
            } else {
                h += ctxItemsHtml(groups, {open: st.open, diff: diffOn ? d : null, excerpts: st.excerpts});
            }
            return h;
        }
        function renderCtxPane() {
            const el = document.getElementById('ctxPane');
            if (!el || !ctxState) return;
            const item = items.find(x => x.id === ctxState.req);
            if (!item || Number(el.dataset.req) !== item.id) return;
            el.innerHTML = ctxPaneHtml(item);
            const tabs = document.querySelector('.detail-pane .cx-tabs');
            if (tabs) tabs.outerHTML = detailTabsHtml(item);
        }
        function ctxFocusItem(id) {
            const st = ctxState;
            st.full = false;
            st.open[id] = true;
            const m = st.manifests[st.runKey];
            const it = m && (m.items || []).find(x => x.id === id);
            renderCtxPane();
            if (it) ctxLoadExcerpt(st.excerpts, it, m.project, renderCtxPane);
            const el = document.querySelector(`#ctxPane [data-cx-item="${CSS.escape(id)}"]`);
            if (el && el.scrollIntoView) el.scrollIntoView({block: 'center'});
        }
        // 警告去处：缺技能 → 设置抽屉技能库并选中；覆盖 → 提示词 tab；截断项目指引 → 提示词 tab 只读条目
        function ctxFollowWarning(act, arg, project) {
            if (act === 'skill') { openSettingsAt('skills', {skill: arg}); return true; }
            if (act === 'prompt') { openSettingsAt('prompts', {prompt: arg}); return true; }
            const g = String(arg || '').match(/^guide:([^/]+)\/(.+)$/);
            if (act === 'item' && g && project) { openSettingsAt('prompts', {prompt: 'project/' + project + '/' + g[1] + '/guide/' + g[2]}); return true; }
            return false;
        }
        document.getElementById('detail').addEventListener('click', (e) => {
            const t = e.target && e.target.closest ? e.target.closest('[data-cx-act]') : null;
            if (!t || !ctxState || !t.closest('#ctxPane') || t.disabled) return;
            const st = ctxState;
            const act = t.dataset.cxAct;
            const arg = t.dataset.cxArg;
            const item = items.find(x => x.id === st.req);
            if (act === 'node') {
                st.role = arg;
                const first = (st.runs || []).find(r => r.role === arg);
                st.runKey = first ? first.run_key : null;
                st.open = {};
                st.full = false;
                renderCtxPane();
                if (st.runKey) ctxLoadManifest(st.runKey);
            } else if (act === 'run') {
                st.runKey = arg;
                st.open = {};
                st.full = false;
                renderCtxPane();
                ctxLoadManifest(arg);
            } else if (act === 'toggle') {
                st.open[arg] = !st.open[arg];
                renderCtxPane();
                const m = st.manifests[st.runKey];
                const it = m && (m.items || []).find(x => x.id === arg);
                if (st.open[arg] && it) ctxLoadExcerpt(st.excerpts, it, m.project, renderCtxPane);
            } else if (act === 'diff') {
                st.diff = !st.diff;
                renderCtxPane();
            } else if (act === 'full') {
                st.full = !st.full;
                renderCtxPane();
                if (st.full) ctxLoadPrompt(st.runKey);
            } else if (act === 'reload') {
                ctxState = ctxFreshState(st.req);
                if (item) ctxMount(item);
            } else if (act === 'preview') {
                if (item) openContextPreview({project: item.project, role: st.role, req: item.id});
            } else if (act === 'skill' || act === 'prompt' || act === 'item') {
                if (!ctxFollowWarning(act, arg, item && item.project) && act === 'item') ctxFocusItem(arg);
            }
        });

        // ---------------- 注入预览（右侧浮层抽屉，叠在设置抽屉之上） ----------------
        let pvState = {project: '', role: '', req: '', data: null, loading: false, error: '', open: {}, excerpts: {}, full: false, seq: 0};
        function openContextPreview(opts) {
            const o = opts || {};
            const projects = autoProjects || [];
            pvState.project = o.project || pvState.project || (selectedProject !== ALL_PROJECTS ? selectedProject : '') || projects[0] || '';
            pvState.role = o.role || pvState.role || 'dev';
            pvState.req = o.req != null ? String(o.req) : (o.project && o.project !== pvState.project ? '' : pvState.req);
            if (pvState.req && !items.some(i => String(i.id) === pvState.req && i.project === pvState.project)) pvState.req = '';
            document.getElementById('ctxPreviewMask').classList.add('show');
            const panel = document.getElementById('ctxPreviewDrawer');
            panel.inert = false;
            panel.classList.add('show');
            renderPreviewControls();
            runContextPreview();
            const close = document.getElementById('ctxPreviewCloseBtn');
            if (close) close.focus();
        }
        function closeContextPreview() {
            document.getElementById('ctxPreviewMask').classList.remove('show');
            const panel = document.getElementById('ctxPreviewDrawer');
            panel.classList.remove('show');
            panel.inert = true;
        }
        function previewOpen() {
            const p = document.getElementById('ctxPreviewDrawer');
            return !!(p && p.classList.contains('show'));
        }
        function previewRoleOptions() {
            const roles = (roleMeta || []).slice().sort((a, b) => (a.order || 99) - (b.order || 99));
            return roles.length ? roles.map(r => ({id: r.role, display: r.display || r.role})) : [{id: 'dev', display: 'dev'}];
        }
        function renderPreviewControls() {
            const box = document.getElementById('ctxPreviewControls');
            const projects = autoProjects || [];
            const cards = items.filter(i => i.project === pvState.project).slice(0, 200);
            box.innerHTML = '<div class="cx-row cx-wrap cx-end">'
                + `<label class="cx-lbl"><span>项目</span><select id="pvProject">${projects.map(p => `<option value="${escapeAttr(p)}"${p === pvState.project ? ' selected' : ''}>${escapeHtml(displayProjectName(p))}</option>`).join('')}</select></label>`
                + `<label class="cx-lbl"><span>角色</span><select id="pvRole">${previewRoleOptions().map(r => `<option value="${escapeAttr(r.id)}"${r.id === pvState.role ? ' selected' : ''}>${escapeHtml(r.display)}</option>`).join('')}</select></label>`
                + `<label class="cx-lbl cx-grow"><span>卡片（可选）</span><select id="pvReq"><option value="">不指定（占位卡）</option>${cards.map(c => `<option value="${c.id}"${String(c.id) === pvState.req ? ' selected' : ''} data-i18n-skip>#${c.id} ${escapeHtml(String(c.title || '').slice(0, 40))}</option>`).join('')}</select></label>`
                + '<button type="button" class="btn btn-sm" id="pvRunBtn">重新编译</button></div>';
        }
        async function runContextPreview() {
            const seq = ++pvState.seq;
            pvState.loading = true;
            pvState.error = '';
            pvState.open = {};
            pvState.excerpts = {};
            pvState.full = false;
            renderPreviewBody();
            const q = new URLSearchParams({project: pvState.project, role: pvState.role});
            if (pvState.req) q.set('req', pvState.req);
            try {
                const res = await fetch('/api/context/preview?' + q.toString(), {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                if (seq !== pvState.seq) return;
                pvState.data = d;
            } catch (e) {
                if (seq !== pvState.seq) return;
                pvState.data = null;
                pvState.error = String((e && e.message) || e);
            }
            pvState.loading = false;
            renderPreviewBody();
        }
        function previewBodyHtml(st) {
            if (st.loading) return '<div class="cx-note">编译中…（与调度器同一个编译器，不拉会话、不花 token）</div>';
            if (st.error) return `<div class="cx-banner cx-banner-bad"><span>注入预览失败：${escapeHtml(st.error)}</span></div>`;
            const m = st.data;
            if (!m) return '';
            const groups = ctxGroupItems(m.items);
            let h = '';
            if (m.placeholder_card) h += '<div class="cx-banner cx-banner-info"><span>未指定卡片：任务数据层用占位卡。选一张已有卡片可连任务数据层一起看。</span></div>';
            h += ctxPreviewStatsHtml(m, groups) + ctxCompositionHtml(groups) + ctxWarningsHtml(m.warnings);
            if ((m.skipped || []).length) {
                h += '<details class="cx-skipped"><summary>未入选 ' + m.skipped.length + ' 项</summary><ul>'
                    + m.skipped.map(s => `<li><span class="cx-mono" data-i18n-skip>${escapeHtml(s.id || '')}</span> — <span data-i18n-skip>${escapeHtml(s.why || '')}</span></li>`).join('') + '</ul></details>';
            }
            h += '<div class="cx-row cx-wrap">'
                + `<button type="button" class="btn btn-sm" data-cx-act="pvcopy">复制清单 JSON</button>`
                + `<button type="button" class="btn btn-sm${st.full ? ' cx-on' : ''}" data-cx-act="pvfull" aria-pressed="${st.full}">${st.full ? '返回分层列表' : '查看完整提示词'}</button></div>`;
            h += st.full ? `<pre class="cx-pre cx-full" data-i18n-skip>${escapeHtml(m.prompt || '')}</pre>`
                : ctxItemsHtml(groups, {open: st.open, excerpts: st.excerpts});
            return h;
        }
        function renderPreviewBody() {
            const el = document.getElementById('ctxPreviewBody');
            if (el) el.innerHTML = previewBodyHtml(pvState);
        }
        document.getElementById('ctxPreviewControls').addEventListener('change', (e) => {
            const id = e.target && e.target.id;
            if (id === 'pvProject') { pvState.project = e.target.value; pvState.req = ''; renderPreviewControls(); }
            else if (id === 'pvRole') pvState.role = e.target.value;
            else if (id === 'pvReq') pvState.req = e.target.value;
            else return;
            runContextPreview();
        });
        document.getElementById('ctxPreviewControls').addEventListener('click', (e) => {
            if (e.target && e.target.id === 'pvRunBtn') runContextPreview();
        });
        document.getElementById('ctxPreviewBody').addEventListener('click', (e) => {
            const t = e.target && e.target.closest ? e.target.closest('[data-cx-act]') : null;
            if (!t || t.disabled) return;
            const act = t.dataset.cxAct;
            const arg = t.dataset.cxArg;
            const m = pvState.data;
            if (act === 'toggle') {
                pvState.open[arg] = !pvState.open[arg];
                renderPreviewBody();
                const it = m && (m.items || []).find(x => x.id === arg);
                if (pvState.open[arg] && it) ctxLoadExcerpt(pvState.excerpts, it, m.project, renderPreviewBody);
            } else if (act === 'pvfull') {
                pvState.full = !pvState.full;
                renderPreviewBody();
            } else if (act === 'pvcopy' && m) {
                const copy = Object.assign({}, m);
                delete copy.prompt;
                const txt = JSON.stringify(copy, null, 1);
                const done = () => showToast('已复制清单 JSON');
                if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(done, () => showToast('复制失败', true));
                else showToast('复制失败', true);
            } else if (act === 'skill' || act === 'prompt' || act === 'item') {
                if (ctxFollowWarning(act, arg, m && m.project)) closeContextPreview();
                else if (act === 'item') { pvState.open[arg] = true; renderPreviewBody(); }
            }
        });
        document.getElementById('ctxPreviewMask').addEventListener('click', closeContextPreview);
        document.getElementById('ctxPreviewCloseBtn').addEventListener('click', closeContextPreview);
        // ESC 只关最上层：预览打开时在捕获阶段拦下，不让设置抽屉一起关
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && previewOpen()) { e.stopPropagation(); closeContextPreview(); }
        }, true);
        // ==================== 设置抽屉「提示词」「技能库」两个 tab（context-design 第五、六、八节） ====================
        // 提示词：GET /api/prompts（按层分组的目录）→ GET /api/prompts/<id>（详情）→ PUT（存本机覆盖，校验以服务端为准）
        //         / DELETE（恢复默认）；默认已更新（stale）给三方对比与「采用新默认 / 保留我的覆盖」；项目约定只读。
        // 技能库：GET /api/skills（列表 + 校验 + 近 30 天加载次数）→ GET/PUT/POST/DELETE /api/skills[/<名>]；
        //         列表 ↔ 角色 × 技能矩阵（点格子循环切换激活方式，PUT roles/activation）。项目技能只读。
        // 提示词正文、技能正文与描述、文件内容一律在 textarea / pre / data-i18n-skip 内，不经界面翻译层。
        let promptsLoaded = false;
        let promptsLoading = false;
        let ppState = {items: [], localDir: '', sel: null, details: {}, dErr: {}, drafts: {}, rebase: {},
                       diff: false, three: false, serverErr: {}, pending: null};
        const PP_STATUS = {
            default: ['默认', 'cx-tone-gray'], override: ['已覆盖', 'cx-tone-blue'], stale: ['默认已更新', 'cx-tone-amber'],
            project: ['只读', 'cx-tone-gray'], missing: ['缺失', 'cx-tone-red'],
        };
        const PP_MAX_CHARS = 20000;   // 与 prompt_store.MAX_CHARS 一致（服务端为准，这里只做即时提示）
        const PP_PH_RE = /\{([a-z_][a-z0-9_]*)\}/g;   // 与 prompt_store._PH_RE 同口径

        // 目录分组：可编辑条目按层（L0..L7 / 运行时文案），项目约定只读条目单列一组放最后
        function promptGroups(list) {
            const by = {};
            (list || []).forEach(it => {
                const k = it.kind === 'project' ? 'project' : ctxLayerOf(it.layer);
                (by[k] = by[k] || []).push(it);
            });
            return CTX_LAYER_ORDER.concat(['project']).filter(k => by[k]).map(k => ({
                key: k, layer: k === 'project' ? 'L2' : k,
                name: k === 'project' ? '项目约定（只读）' : CTX_LAYERS[k].name,
                locked: by[k].some(x => (x.locked || []).length), items: by[k],
            }));
        }
        function promptTreeHtml(groups, sel) {
            return '<nav class="pp-tree" aria-label="提示词目录">' + groups.map(g => {
                const L = CTX_LAYERS[g.layer];
                return `<div class="cx-group"><div class="cx-ghead"><i class="cx-dot ${L.cls}" aria-hidden="true"></i>`
                    + (g.key === 'runtime' || g.key === 'project' ? '' : `<span class="cx-mono cx-dim">${g.key}</span>`)
                    + `<b>${escapeHtml(g.name)}</b>${g.locked ? '<span class="cx-pill cx-tone-amber">含锁定句</span>' : ''}</div>`
                    + g.items.map(it => {
                        const s = PP_STATUS[it.status] || [it.status || '—', 'cx-tone-gray'];
                        const on = it.id === sel;
                        return `<div class="cx-item"><button type="button" class="cx-item-btn pp-node${on ? ' on' : ''}" aria-current="${on ? 'true' : 'false'}" data-pp-act="sel" data-pp-arg="${escapeAttr(it.id)}">`
                            + `<span class="pp-node-main"><span>${escapeHtml(it.title || it.id)}</span><span class="cx-mono cx-dim pp-file" data-i18n-skip>${escapeHtml(it.kind === 'project' ? it.file : it.file || it.id)}</span></span>`
                            + `<span class="cx-pill ${s[1]}">${escapeHtml(s[0])}</span></button></div>`;
                    }).join('') + '</div>';
            }).join('') + '</nav>';
        }
        // 锁定句（契约句）：编辑区里替换成不可改内容的标记行，保存时再还原——标记可整体移动，删掉即校验失败
        function promptLockMarker(i) {
            return '⟦锁定句 ' + (i + 1) + '⟧';
        }
        function promptToEditable(text, locked) {
            let t = String(text == null ? '' : text);
            const missing = [];
            (locked || []).forEach((s, i) => {
                const k = t.indexOf(s);
                if (k < 0) { missing.push(i); return; }
                t = t.slice(0, k) + promptLockMarker(i) + t.slice(k + s.length);
            });
            return {text: t, missing: missing};
        }
        function promptFromEditable(text, locked) {
            let t = String(text == null ? '' : text);
            (locked || []).forEach((s, i) => { t = t.split(promptLockMarker(i)).join(s); });
            return t;
        }
        // 即时校验（服务端校验的镜像：未声明占位符 / 缺必需占位符 / 锁定句 / 超长）；返回 [[级别, 文案]]
        function promptClientCheck(d, editable) {
            const out = [];
            const locked = d.locked || [];
            locked.forEach((s, i) => {
                const n = String(editable).split(promptLockMarker(i)).length - 1;
                if (n !== 1) out.push(['bad', `锁定句标记 ${promptLockMarker(i)} ${n ? '重复' : '被删除'}：契约句必须原样保留一次`]);
            });
            const full = promptFromEditable(editable, locked);
            const declared = d.placeholders || [];
            const used = [];
            let m;
            PP_PH_RE.lastIndex = 0;
            while ((m = PP_PH_RE.exec(full))) if (!used.includes(m[1])) used.push(m[1]);
            const unknown = used.filter(u => !declared.includes(u));
            const missing = (d.required || []).filter(r => full.indexOf('{' + r + '}') < 0);
            unknown.forEach(u => out.push(['bad', `未声明的占位符 {${u}}：调度器不会替换它`]));
            missing.forEach(r => out.push(['bad', `缺少必需占位符 {${r}}`]));
            if (!unknown.length && !missing.length) out.push(['ok', declared.length ? '占位符齐全' : '本项不使用占位符']);
            if (locked.length && !out.some(x => x[1].startsWith('锁定句标记'))) out.push(['ok', '契约句完整（锁定，不可删改）']);
            if (full.length > PP_MAX_CHARS) out.push(['bad', `超过 ${ctxFmt(PP_MAX_CHARS)} 字（当前 ${ctxFmt(full.length)}）`]);
            else out.push(['ok', `${ctxFmt(full.length)} 字`]);
            return out;
        }
        // 行级对比（LCS）：返回 [[' '|'-'|'+', 行]]
        function promptLineDiff(a, b) {
            const A = String(a == null ? '' : a).split('\n');
            const B = String(b == null ? '' : b).split('\n');
            const n = A.length, m = B.length;
            if (n * m > 4000000) return A.map(x => ['-', x]).concat(B.map(x => ['+', x]));
            const dp = [];
            for (let i = 0; i <= n; i++) dp.push(new Int32Array(m + 1));
            for (let i = n - 1; i >= 0; i--) {
                for (let j = m - 1; j >= 0; j--) {
                    dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
                }
            }
            const out = [];
            let i = 0, j = 0;
            while (i < n && j < m) {
                if (A[i] === B[j]) { out.push([' ', A[i]]); i++; j++; }
                else if (dp[i + 1][j] >= dp[i][j + 1]) out.push(['-', A[i++]]);
                else out.push(['+', B[j++]]);
            }
            while (i < n) out.push(['-', A[i++]]);
            while (j < m) out.push(['+', B[j++]]);
            return out;
        }
        function promptDiffHtml(a, b) {
            const rows = promptLineDiff(a, b);
            if (!rows.some(r => r[0] !== ' ')) return '<div class="cx-note">与默认完全一致</div>';
            return '<pre class="cx-pre pp-diff" data-i18n-skip>' + rows.map(r => {
                const cls = r[0] === '+' ? 'pp-add' : r[0] === '-' ? 'pp-del' : 'pp-same';
                return `<span class="${cls}">${r[0]} ${escapeHtml(r[1])}</span>`;
            }).join('\n') + '</pre>';
        }
        function promptWhenText(w) {
            if (!w) return '';
            const parts = [];
            if ((w.roles || []).length) parts.push('角色 ' + w.roles.map(r => roleLabel(r)).join('、'));
            if ((w.flags || []).length) parts.push('开关 ' + w.flags.join(' 且 '));
            return parts.join('；') || '恒生效';
        }
        function promptCheckHtml(checks) {
            return checks.map(c => `<span class="pp-check pp-${c[0]}">${c[0] === 'ok' ? '✓' : c[0] === 'bad' ? '✕' : '!'} ${escapeHtml(c[1])}</span>`).join('');
        }
        // 编辑区（纯函数）：d = GET /api/prompts/<id> 详情；st = {editable, dirty, diff, three, serverErr, localDir, rebase}
        function promptEditorHtml(d, st) {
            const s = PP_STATUS[d.status] || [d.status || '—', 'cx-tone-gray'];
            const L = CTX_LAYERS[ctxLayerOf(d.layer)];
            const ro = !!d.readonly;
            const full = ro ? d.text : promptFromEditable(st.editable, d.locked);
            const checks = ro ? [] : promptClientCheck(d, st.editable);
            const bad = checks.some(c => c[0] === 'bad');
            let h = `<div class="cx-row cx-wrap pp-head"><i class="cx-dot ${L.cls}" aria-hidden="true"></i><b class="pp-title">${escapeHtml(d.title || d.id)}</b>`
                + `<span class="cx-pill ${s[1]}">${escapeHtml(s[0])}</span>`
                + `<span class="cx-pill cx-tone-violet" id="ppDirty"${st.dirty ? '' : ' hidden'}>有未保存修改</span><span class="cx-spacer"></span>`;
            if (ro) {
                h += '<button type="button" class="btn btn-sm" data-pp-act="copypath">复制文件路径</button>';
            } else {
                h += `<button type="button" class="btn btn-sm${st.diff ? ' cx-on' : ''}" data-pp-act="diff" aria-pressed="${!!st.diff}">${st.diff ? '返回编辑' : '对比默认'}</button>`
                    + `<button type="button" class="btn btn-sm" data-pp-act="restore"${d.status === 'default' ? ' disabled' : ''}>恢复默认</button>`;
            }
            h += '<button type="button" class="btn btn-sm" data-pp-act="preview">在注入预览里看效果</button>';
            if (!ro) h += `<button type="button" class="btn btn-sm btn-primary" id="ppSaveBtn" data-pp-act="save"${bad || !st.dirty ? ' disabled' : ''}>保存</button>`;
            h += '</div>';
            h += '<div class="cx-stats pp-meta">'
                + `<div><div class="cx-k">${ro ? '文件（项目仓库）' : '默认文件'}</div><div class="cx-mono" data-i18n-skip>${escapeHtml(ro ? d.file : 'prompts/' + d.file)}</div></div>`
                + (ro ? '' : `<div><div class="cx-k">覆盖文件（本机，不进 git）</div><div class="cx-mono" data-i18n-skip>${d.override_text != null ? escapeHtml((st.localDir || 'prompts.local') + '/' + d.file) : '（无）'}</div></div>`)
                + `<div><div class="cx-k">用于</div><div>${escapeHtml(d.used_by || '—')}</div></div>`
                + (d.when ? `<div><div class="cx-k">生效条件（引擎定义，只读）</div><div>${escapeHtml(promptWhenText(d.when))}</div></div>` : '')
                + '</div>';
            if (ro) {
                h += '<div class="cx-banner cx-banner-info"><span>只读：这份文件在项目仓库里，第一期请在编辑器里修改并提交到该仓库；这里显示的是下次编译会读到的内容。</span></div>';
                return h + `<pre class="cx-pre pp-ro" data-i18n-skip>${escapeHtml(d.text || '')}</pre>`;
            }
            if (d.status === 'stale') {
                h += `<div class="cx-banner cx-banner-warn"><span>升级后默认版本已更新，你的覆盖基于旧版默认。编译仍使用你的覆盖，直到你处理。</span> <button type="button" class="btn btn-sm${st.three ? ' cx-on' : ''}" data-pp-act="three" aria-pressed="${!!st.three}">${st.three ? '收起三方对比' : '三方对比'}</button></div>`;
                if (st.three) {
                    const col = (title, t) => `<div class="pp-col"><b>${title}</b><pre class="cx-pre" data-i18n-skip>${escapeHtml(t || '')}</pre></div>`;
                    h += '<div class="pp-three">' + col('旧默认（你保存时的默认）', d.base_text) + col('新默认', d.default_text) + col('你的覆盖', d.override_text) + '</div>'
                        + '<div class="cx-row cx-wrap"><button type="button" class="btn btn-sm" data-pp-act="takenew">采用新默认（删除覆盖）</button>'
                        + '<button type="button" class="btn btn-sm" data-pp-act="rebasenew">以新默认为底稿重新编辑</button>'
                        + '<button type="button" class="btn btn-sm" data-pp-act="keepmine">保留我的覆盖并标记已处理</button></div>';
                }
            } else if (d.status === 'override') {
                h += '<div class="cx-banner cx-banner-info"><span>当前生效的是你的本机覆盖；编译时使用覆盖，清单里记为「本机覆盖」。</span></div>';
            }
            if (st.rebase) h += '<div class="cx-banner cx-banner-warn"><span>正在以新默认为底稿编辑：保存后覆盖改为基于新默认。</span></div>';
            if (st.diff) {
                h += '<div class="cx-col"><span class="cx-note">与默认相比（- 默认里有、你删掉的；+ 你新增或改写的）</span>' + promptDiffHtml(d.default_text, full) + '</div>';
            } else {
                const ph = d.placeholders || [];
                if (ph.length) {
                    h += '<div class="cx-row cx-wrap"><span class="cx-note">可用占位符（点击插入到光标处）：</span>'
                        + ph.map(p => `<button type="button" class="pp-chip cx-mono" data-pp-act="ins" data-pp-arg="${escapeAttr(p)}">{${escapeHtml(p)}}${(d.required || []).includes(p) ? ' · 必需' : ''}</button>`).join('') + '</div>';
                }
                if ((d.locked || []).length) {
                    h += '<div class="cx-col"><span class="cx-note">契约句（调度器按它解析结果，锁定不可删改；正文里以标记行代替，可整体移动）</span>'
                        + `<pre class="cx-pre pp-locked" data-i18n-skip>${d.locked.map((l, i) => '🔒 ' + promptLockMarker(i) + ' ' + escapeHtml(l)).join('\n')}</pre></div>`;
                }
                h += `<label class="cx-lbl"><span>可编辑正文</span><textarea id="ppEdit" class="pp-edit cx-mono" rows="16" spellcheck="false">${escapeHtml(st.editable)}</textarea></label>`;
            }
            h += `<div class="cx-col pp-checks" id="ppCheck"><b>校验</b>${promptCheckHtml(checks)}</div>`;
            if ((st.serverErr || []).length) {
                h += '<div class="cx-banner cx-banner-bad"><span>服务端拒绝保存：' + st.serverErr.map(e => escapeHtml(e.message || e.code || '')).join('；') + '</span></div>';
            }
            return h;
        }
        async function loadPromptsTab(force) {
            const body = document.getElementById('settingsPromptsBody');
            if (promptsLoaded && !force) return;
            if (promptsLoading) return;
            promptsLoading = true;
            body.innerHTML = '<div class="sd-note">提示词加载中…</div>';
            try {
                const res = await fetch('/api/prompts', {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                ppState.items = d.items || [];
                ppState.localDir = d.local_dir || '';
                if (force) { ppState.details = {}; ppState.dErr = {}; }
                promptsLoaded = true;
                const want = ppState.pending || ppState.sel;
                ppState.pending = null;
                ppState.sel = ppState.items.some(x => x.id === want) ? want : (ppState.items[0] || {}).id || null;
                renderPromptsTab();
                if (ppState.sel) ppLoadDetail(ppState.sel);
            } catch (e) {
                promptsLoaded = false;
                body.innerHTML = '<div class="sd-warn">提示词加载失败：' + escapeHtml(String((e && e.message) || e)) + '</div>'
                    + '<button type="button" class="btn" data-pp-act="retry">重试</button>';
            } finally {
                promptsLoading = false;
            }
        }
        async function ppLoadDetail(id, force) {
            if (ppState.details[id] && !force) { renderPromptsTab(); return; }
            try {
                const res = await fetch('/api/prompts/' + id, {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                ppState.details[id] = d;
                delete ppState.dErr[id];
            } catch (e) {
                ppState.dErr[id] = String((e && e.message) || e);
            }
            renderPromptsTab();
        }
        function ppEditable(d) {
            const id = d.id;
            if (ppState.drafts[id] != null) return ppState.drafts[id];
            return promptToEditable(d.text, d.locked).text;
        }
        function ppDirty(d) {
            return ppState.drafts[d.id] != null && promptFromEditable(ppState.drafts[d.id], d.locked) !== d.text;
        }
        function renderPromptsTab() {
            const body = document.getElementById('settingsPromptsBody');
            if (!promptsLoaded) return;
            const st = ppState;
            const d = st.details[st.sel];
            let editor;
            if (!st.sel) editor = '<div class="cx-note">没有可编辑的提示词条目</div>';
            else if (st.dErr[st.sel]) editor = `<div class="sd-warn">条目加载失败：${escapeHtml(st.dErr[st.sel])}</div>`;
            else if (!d) editor = '<div class="cx-note">加载中…</div>';
            else editor = promptEditorHtml(d, {editable: ppEditable(d), dirty: ppDirty(d), diff: st.diff, three: st.three,
                                               serverErr: st.serverErr[st.sel], localDir: st.localDir, rebase: !!st.rebase[st.sel]});
            const prevScroll = body.scrollTop;
            body.innerHTML = '<p class="sd-note pp-intro">发给 agent 的每一段文字都是文件：保存写进本机覆盖目录 <code data-i18n-skip>'
                + escapeHtml(st.localDir || 'prompts.local/') + '</code>（不进 git），下一次编译即生效，无需重启；「恢复默认」= 删除覆盖。</p>'
                + '<div class="pp-split">' + promptTreeHtml(promptGroups(st.items), st.sel)
                + `<div class="pp-editor cx-col">${editor}</div></div>`;
            body.scrollTop = prevScroll;
        }
        // 只刷新校验区 / 保存按钮 / 未保存标记（输入时不重建 textarea，保住光标）
        function ppRefreshChecks() {
            const d = ppState.details[ppState.sel];
            if (!d) return;
            const checks = promptClientCheck(d, ppEditable(d));
            const box = document.getElementById('ppCheck');
            if (box) box.innerHTML = '<b>校验</b>' + promptCheckHtml(checks);
            const dirty = ppDirty(d);
            const btn = document.getElementById('ppSaveBtn');
            if (btn) btn.disabled = !dirty || checks.some(c => c[0] === 'bad');
            const tag = document.getElementById('ppDirty');
            if (tag) tag.hidden = !dirty;
        }
        function ppSetItemStatus(d) {
            const it = ppState.items.find(x => x.id === d.id);
            if (it) it.status = d.status;
        }
        async function ppSave(text, rebase) {
            const id = ppState.sel;
            const d = ppState.details[id];
            if (!d) return;
            const res = await fetch('/api/prompts/' + id, {method: 'PUT', headers: {'Content-Type': 'application/json'},
                                                            body: JSON.stringify({text: text, rebase: !!rebase})});
            const r = await res.json().catch(() => ({}));
            if (!res.ok) {
                ppState.serverErr[id] = r.errors || [{message: r.error || ('HTTP ' + res.status)}];
                renderPromptsTab();
                showToast('保存失败：' + ppState.serverErr[id].map(e => e.message || e.code).join('；'), true);
                return;
            }
            delete ppState.serverErr[id];
            delete ppState.drafts[id];
            delete ppState.rebase[id];
            ppState.details[id] = r;
            ppState.three = false;
            ppSetItemStatus(r);
            renderPromptsTab();
            showToast('已保存为本机覆盖，下一次编译生效');
        }
        async function ppRestore() {
            const id = ppState.sel;
            if (!window.confirm('恢复默认会删除本机覆盖文件，确定？')) return;
            const res = await fetch('/api/prompts/' + id, {method: 'DELETE'});
            const r = await res.json().catch(() => ({}));
            if (!res.ok) { showToast('恢复默认失败：' + (r.error || ('HTTP ' + res.status)), true); return; }
            delete ppState.drafts[id];
            delete ppState.rebase[id];
            delete ppState.serverErr[id];
            ppState.details[id] = r;
            ppState.three = false;
            ppSetItemStatus(r);
            renderPromptsTab();
            showToast('已恢复默认');
        }
        function ppPreviewRole(d) {
            const id = String(d.id || '');
            if (id.startsWith('role/')) return id.slice(5);
            if (d.when && (d.when.roles || []).length) return d.when.roles[0];
            return 'dev';
        }
        document.getElementById('settingsPromptsBody').addEventListener('click', (e) => {
            const t = e.target && e.target.closest ? e.target.closest('[data-pp-act]') : null;
            if (!t || t.disabled) return;
            const act = t.dataset.ppAct;
            const arg = t.dataset.ppArg;
            const d = ppState.details[ppState.sel];
            if (act === 'retry') { loadPromptsTab(true); return; }
            if (act === 'sel') {
                ppState.sel = arg;
                ppState.diff = false;
                ppState.three = false;
                ppLoadDetail(arg);
                return;
            }
            if (!d) return;
            if (act === 'diff') { ppState.diff = !ppState.diff; renderPromptsTab(); }
            else if (act === 'three') { ppState.three = !ppState.three; renderPromptsTab(); }
            else if (act === 'save') ppSave(promptFromEditable(ppEditable(d), d.locked), ppState.rebase[d.id]);
            else if (act === 'restore' || act === 'takenew') ppRestore();
            else if (act === 'keepmine') ppSave(d.override_text, true);
            else if (act === 'rebasenew') {
                ppState.drafts[d.id] = promptToEditable(d.default_text, d.locked).text;
                ppState.rebase[d.id] = true;
                ppState.three = false;
                ppState.diff = false;
                renderPromptsTab();
            } else if (act === 'ins') {
                const ta = document.getElementById('ppEdit');
                if (!ta) return;
                const pos = ta.selectionStart == null ? ta.value.length : ta.selectionStart;
                const end = ta.selectionEnd == null ? pos : ta.selectionEnd;
                const tok = '{' + arg + '}';
                ta.value = ta.value.slice(0, pos) + tok + ta.value.slice(end);
                ppState.drafts[d.id] = ta.value;
                ta.focus();
                ta.setSelectionRange(pos + tok.length, pos + tok.length);
                ppRefreshChecks();
            } else if (act === 'preview') {
                const parts = String(d.id).split('/');
                openContextPreview({role: ppPreviewRole(d), project: d.readonly ? parts[1] : undefined});
            } else if (act === 'copypath') {
                if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(d.file || '').then(() => showToast('已复制路径'), () => showToast('复制失败', true));
            }
        });
        document.getElementById('settingsPromptsBody').addEventListener('input', (e) => {
            if (!e.target || e.target.id !== 'ppEdit') return;
            ppState.drafts[ppState.sel] = e.target.value;
            ppRefreshChecks();
        });

        // ---------------- 技能库 ----------------
        let skillsLoaded = false;
        let skillsLoading = false;
        let skState = {data: null, q: '', scope: 'all', view: 'list', sel: null, details: {}, dErr: {}, forms: {},
                       errors: {}, creating: false, createErr: '', pending: null, busy: {}};
        const SK_ACT = {
            always: {label: '始终', hint: '角色匹配就内联正文', cls: 'cx-tone-blue'},
            auto: {label: '自动', hint: '命中路径或关键词时内联', cls: 'cx-tone-violet'},
            on_demand: {label: '按需', hint: '只进目录，agent 自取', cls: 'cx-tone-teal'},
            manual: {label: '手动', hint: '卡片 notes 点名才加载', cls: 'cx-tone-amber'},
        };
        const SK_CYCLE = ['', 'always', 'auto', 'on_demand', 'manual'];
        const SK_STATUS = {active: ['启用', 'cx-tone-green'], draft: ['草稿', 'cx-tone-violet'], deprecated: ['废弃', 'cx-tone-gray']};
        const SK_SCOPE = {global: '全局', local: '本机', project: '项目'};
        function skillKey(sk) {
            return sk.scope === 'project' ? 'project:' + sk.project + ':' + sk.name : 'g:' + sk.name;
        }
        function skillScopeLabel(sk) {
            if (sk.scope === 'project') return '项目 ' + (sk.project || '');
            if (sk.scope === 'local') return sk.override_of ? '本机覆盖' : '本机';
            return '全局';
        }
        function skillFilter(skills, q, scope) {
            const s = String(q || '').trim().toLowerCase();
            return (skills || []).filter(k => (scope === 'all' || k.scope === scope)
                && (!s || String(k.name).toLowerCase().includes(s) || String(k.description || '').toLowerCase().includes(s)));
        }
        function skillRolesText(sk, roles) {
            const disp = id => ((roles || []).find(r => r.id === id) || {}).display || id;
            return (sk.effective_roles || sk.roles || []).map(disp).join('、');
        }
        function skillLintTone(level) {
            return level === 'error' ? ['校验失败', 'pp-bad'] : level === 'warn' ? ['有提醒', 'pp-warn'] : ['校验通过', 'pp-ok'];
        }
        function skillListHtml(list, sel, roles) {
            if (!list.length) return '<div class="cx-note">没有匹配的技能</div>';
            return list.map(k => {
                const a = SK_ACT[k.activation] || {label: k.activation || '—', cls: 'cx-tone-gray'};
                const st = SK_STATUS[k.status] || [k.status || '—', 'cx-tone-gray'];
                const lt = skillLintTone(k.lint_level);
                const on = skillKey(k) === sel;
                return `<button type="button" class="cx-pick sk-row${on ? ' on' : ''}" aria-current="${on ? 'true' : 'false'}" data-sk-act="sel" data-sk-arg="${escapeAttr(skillKey(k))}">`
                    + `<span class="cx-row"><b class="cx-mono" data-i18n-skip>${escapeHtml(k.name)}</b><span class="cx-pill ${a.cls}">${escapeHtml(a.label)}</span>`
                    + `<span class="cx-note">${escapeHtml(skillScopeLabel(k))}</span><span class="cx-spacer"></span><span class="cx-pill ${st[1]}">${escapeHtml(st[0])}</span></span>`
                    + `<span class="sk-desc" data-i18n-skip>${escapeHtml(k.description || '')}</span>`
                    + `<span class="cx-row cx-note sk-meta"><span>${escapeHtml(skillRolesText(k, roles) || '未挂载角色')}</span><span>近 30 天加载 ${Number(k.uses_30d || 0)} 次</span><span class="${lt[1]}">${lt[0]}</span></span></button>`;
            }).join('');
        }
        // 矩阵格：返回 {value: '' | activation, mounted: [来源], editable, inRoles}
        function skillCell(sk, role) {
            const inRoles = (sk.roles || []).includes(role);
            const mounted = (sk.mounted || []).filter(m => m.role === role).map(m => m.source);
            return {value: (inRoles || mounted.length) ? (sk.activation || '') : '', inRoles: inRoles, mounted: mounted,
                    editable: !!sk.editable};
        }
        // 点格子：— → 始终 → 自动 → 按需 → 手动 → —。返回要 PUT 的 {roles, activation}；
        // 激活方式按技能生效（同一行同步变化）；由 flows.json / 项目清单点名的格子不能回到「—」（跳回「始终」）
        function skillCellNext(sk, role) {
            const c = skillCell(sk, role);
            let next = SK_CYCLE[(SK_CYCLE.indexOf(c.value) + 1) % SK_CYCLE.length];
            if (!next && c.mounted.length) next = 'always';
            const roles = (sk.roles || []).filter(r => r !== role);
            if (next) {
                if (c.inRoles || !c.mounted.length) roles.push(role);
                return {roles: roles, activation: next};
            }
            return {roles: roles, activation: sk.activation};
        }
        function skillMatrixHtml(list, roles) {
            return '<div class="cx-note">点格子循环切换：— → 始终 → 自动 → 按需 → 手动 → —。改动写回技能的 roles / activation；激活方式按技能生效，同一行的格子同步变化。</div>'
                + '<div class="sk-tablebox"><table class="sk-matrix"><thead><tr><th scope="col">技能</th>'
                + (roles || []).map(r => `<th scope="col">${escapeHtml(r.display)}</th>`).join('') + '</tr></thead><tbody>'
                + list.map(k => '<tr>' + `<th scope="row"><span class="cx-mono" data-i18n-skip>${escapeHtml(k.name)}</span> <span class="cx-note">${escapeHtml(skillScopeLabel(k))}</span></th>`
                    + (roles || []).map(r => {
                        const c = skillCell(k, r.id);
                        const a = SK_ACT[c.value];
                        const why = !c.editable ? '项目技能只读' : c.mounted.length && !c.inRoles ? '由 ' + c.mounted.join('、') + ' 点名' : '点击切换';
                        return `<td><button type="button" class="sk-cell${a ? ' cx-pill ' + a.cls : ''}${c.mounted.length ? ' sk-mounted' : ''}" data-sk-act="cell" data-sk-arg="${escapeAttr(skillKey(k) + '|' + r.id)}"${c.editable ? '' : ' disabled'} aria-label="${escapeAttr(k.name + ' 在 ' + r.display + '：' + (a ? a.label : '未挂载') + '，' + why)}" title="${escapeAttr(why)}">${a ? escapeHtml(a.label) : '—'}${c.mounted.length ? '<sup aria-hidden="true">*</sup>' : ''}</button></td>`;
                    }).join('') + '</tr>').join('')
                + '</tbody></table></div><div class="cx-note">* = 由 flows.json 或项目清单点名挂载（在这里只能改激活方式，取消挂载请改配置文件）。</div>';
        }
        function skillFormFrom(d) {
            return {description: d.description || '', activation: d.activation || 'on_demand', roles: (d.roles || []).slice(),
                    paths: (d.paths || []).join(', '), keywords: (d.keywords || []).join(', '), required: !!d.required,
                    status: d.status || 'active', body: d.body || ''};
        }
        function skillDetailHtml(sk, d, f, roles, errors) {
            const ro = !sk.editable;
            const dis = ro ? ' disabled' : '';
            let h = `<div class="cx-row cx-wrap"><b class="cx-mono sk-name" data-i18n-skip>${escapeHtml(sk.name)}</b><span class="cx-note cx-mono" data-i18n-skip>${escapeHtml(sk.rel_path || sk.path || '')}</span><span class="cx-spacer"></span>`
                + '<button type="button" class="btn btn-sm" data-sk-act="preview">在注入预览里试用</button>'
                + (sk.scope === 'local' ? `<button type="button" class="btn btn-sm" data-sk-act="delete">${sk.override_of ? '删除本机覆盖' : '删除草稿'}</button>` : '')
                + (ro ? '' : '<button type="button" class="btn btn-sm btn-primary" data-sk-act="save">保存</button>') + '</div>';
            if (ro) h += `<div class="cx-banner cx-banner-info"><span>项目技能（${escapeHtml(sk.project || '')}）：文件在项目仓库里，在看板上只读；同名时优先于全局与本机版本。</span></div>`;
            else if (sk.scope === 'global') h += '<div class="cx-banner cx-banner-info"><span>全局默认随代码发布；保存会写一份本机覆盖（prompts.local/skills/），不改默认文件。</span></div>';
            else if (sk.override_of) h += '<div class="cx-banner cx-banner-info"><span>本机覆盖：编译时优先于全局默认；删除本机覆盖即恢复默认。</span></div>';
            if (f.status === 'draft') h += '<div class="cx-banner cx-banner-violet"><span>草稿：发布（状态改为启用）前不会进入任何真实会话。</span></div>';
            if ((errors || []).length) h += '<div class="cx-banner cx-banner-bad"><span>服务端拒绝保存：' + errors.map(e => escapeHtml(e.message || e)).join('；') + '</span></div>';
            h += `<label class="cx-lbl"><span>描述（会进技能目录，agent 靠它判断要不要用；≤ 200 字）</span><textarea id="skDesc" rows="2"${dis}>${escapeHtml(f.description)}</textarea></label>`;
            h += '<div class="cx-col"><span class="cx-note">激活方式</span><div class="cx-row cx-wrap" role="group" aria-label="激活方式">' + Object.keys(SK_ACT).map(id => {
                const on = id === f.activation;
                return `<button type="button" class="sk-act${on ? ' on cx-pill ' + SK_ACT[id].cls : ''}" aria-pressed="${on}" data-sk-act="act" data-sk-arg="${id}"${dis}><b>${SK_ACT[id].label}</b><span class="cx-note">${SK_ACT[id].hint}</span></button>`;
            }).join('') + '</div></div>';
            const mountedRoles = (sk.mounted || []).map(m => m.role);
            h += '<div class="cx-col"><span class="cx-note">挂载角色（展示名取自 flows.json）</span><div class="cx-row cx-wrap" role="group" aria-label="挂载角色">' + (roles || []).map(r => {
                const on = f.roles.includes(r.id);
                const cfg = mountedRoles.includes(r.id);
                return `<button type="button" class="pp-chip${on ? ' on' : ''}" aria-pressed="${on}" data-sk-act="role" data-sk-arg="${escapeAttr(r.id)}"${dis}${cfg ? ' title="flows.json / 项目清单已点名该角色"' : ''}>${on ? '✓ ' : '+ '}${escapeHtml(r.display)}${cfg ? ' *' : ''}</button>`;
            }).join('') + '</div></div>';
            if (f.activation === 'auto') {
                h += '<div class="cx-row cx-wrap cx-stretch">'
                    + `<label class="cx-lbl cx-grow"><span>命中路径（glob，逗号分隔，任一命中即触发）</span><input id="skPaths" class="cx-mono" value="${escapeAttr(f.paths)}"${dis}></label>`
                    + `<label class="cx-lbl cx-grow"><span>命中关键词（标题或 notes，逗号分隔）</span><input id="skKeywords" value="${escapeAttr(f.keywords)}"${dis}></label></div>`;
            }
            h += '<div class="cx-row cx-wrap">'
                + `<label class="cx-check"><input type="checkbox" id="skRequired"${f.required ? ' checked' : ''}${dis}><span>必需：文件缺失或解析失败时停线，不拉会话</span></label>`
                + `<label class="cx-lbl cx-inline"><span>状态</span><select id="skStatus"${dis}>${Object.keys(SK_STATUS).map(s => `<option value="${s}"${s === f.status ? ' selected' : ''}>${SK_STATUS[s][0]}</option>`).join('')}</select></label></div>`;
            h += `<label class="cx-lbl"><span>正文（SKILL.md，frontmatter 由上面的表单生成）</span><textarea id="skBody" class="cx-mono" rows="10" spellcheck="false"${dis}>${escapeHtml(f.body)}</textarea></label>`;
            const lint = sk.lint || [];
            h += '<div class="cx-row cx-wrap cx-top"><div class="cx-col cx-grow"><b>校验</b>'
                + (lint.length ? lint.map(l => { const lv = l.level === 'error' ? 'bad' : l.level === 'warn' ? 'warn' : 'ok'; return `<span class="pp-check pp-${lv}">${lv === 'ok' ? '✓' : lv === 'bad' ? '✕' : '!'} ${escapeHtml(l.message)}</span>`; }).join('') : '<span class="pp-check pp-ok">✓ 没有问题</span>')
                + '</div><div class="cx-col cx-grow"><b>使用情况（近 30 天）</b>'
                + `<span>加载 <span class="cx-mono">${Number(sk.uses_30d || 0)}</span> 次 · 正文 <span class="cx-mono">${ctxFmt(sk.chars)}</span> 字</span>`
                + `<span class="cx-note">挂载来源：${(sk.mounted || []).length ? sk.mounted.map(m => escapeHtml(roleLabel(m.role) + ' ← ' + m.source)).join('；') : '仅技能自身 roles'}</span></div></div>`;
            return h;
        }
        function skillCreateHtml(err) {
            return '<div class="sk-create cx-col"><b>新建技能（草稿）</b>'
                + '<div class="cx-row cx-wrap cx-end"><label class="cx-lbl"><span>名称（小写字母/数字/连字符）</span><input id="skNewName" class="cx-mono" maxlength="40" autocomplete="off"></label>'
                + '<label class="cx-lbl cx-grow"><span>描述</span><input id="skNewDesc" maxlength="200" autocomplete="off"></label>'
                + '<button type="button" class="btn btn-sm btn-primary" data-sk-act="create">创建</button><button type="button" class="btn btn-sm" data-sk-act="cancelnew">取消</button></div>'
                + (err ? `<div class="cx-banner cx-banner-bad"><span>${escapeHtml(err)}</span></div>` : '') + '</div>';
        }
        async function loadSkillsTab(force) {
            const body = document.getElementById('settingsSkillsBody');
            if (skillsLoaded && !force) return;
            if (skillsLoading) return;
            skillsLoading = true;
            if (!skState.data) body.innerHTML = '<div class="sd-note">技能库加载中…</div>';
            try {
                const res = await fetch('/api/skills', {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                skState.data = d;
                if (force) { skState.details = {}; skState.forms = {}; skState.errors = {}; }
                skillsLoaded = true;
                const skills = d.skills || [];
                if (skState.pending) {
                    const hit = skills.find(k => skillKey(k) === skState.pending) || skills.find(k => k.name === skState.pending);
                    skState.sel = hit ? skillKey(hit) : skState.sel;
                    skState.view = 'list';
                    skState.q = '';
                    skState.scope = 'all';
                    if (!hit) showToast('技能库里没有「' + skState.pending + '」：文件缺失，可新建同名技能或取消必需', true);
                    skState.pending = null;
                }
                if (!skills.some(k => skillKey(k) === skState.sel)) skState.sel = skills[0] ? skillKey(skills[0]) : null;
                renderSkillsTab();
                if (skState.sel) skLoadDetail(skState.sel);
            } catch (e) {
                skillsLoaded = false;
                body.innerHTML = '<div class="sd-warn">技能库加载失败：' + escapeHtml(String((e && e.message) || e)) + '</div>'
                    + '<button type="button" class="btn" data-sk-act="retry">重试</button>';
            } finally {
                skillsLoading = false;
            }
        }
        function skFind(key) {
            return ((skState.data || {}).skills || []).find(k => skillKey(k) === key) || null;
        }
        async function skLoadDetail(key, force) {
            const sk = skFind(key);
            if (!sk) return;
            if (skState.details[key] && !force) { renderSkillsTab(); return; }
            try {
                const q = sk.scope === 'project' ? '?project=' + encodeURIComponent(sk.project) : '';
                const res = await fetch('/api/skills/' + encodeURIComponent(sk.name) + q, {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
                skState.details[key] = d;
                skState.forms[key] = skillFormFrom(d);
                delete skState.dErr[key];
            } catch (e) {
                skState.dErr[key] = String((e && e.message) || e);
            }
            renderSkillsTab();
        }
        // 重绘前把输入框里的值收回表单态（激活方式 / 角色按钮会触发重绘）
        function skSyncForm() {
            const f = skState.forms[skState.sel];
            if (!f) return;
            const v = id => { const el = document.getElementById(id); return el ? el : null; };
            if (v('skDesc')) f.description = v('skDesc').value;
            if (v('skPaths')) f.paths = v('skPaths').value;
            if (v('skKeywords')) f.keywords = v('skKeywords').value;
            if (v('skRequired')) f.required = v('skRequired').checked;
            if (v('skStatus')) f.status = v('skStatus').value;
            if (v('skBody')) f.body = v('skBody').value;
        }
        function skSplit(s) {
            return String(s || '').split(/[,，\n]/).map(x => x.trim()).filter(Boolean);
        }
        function renderSkillsTab() {
            const body = document.getElementById('settingsSkillsBody');
            if (!skillsLoaded || !skState.data) return;
            const st = skState;
            const data = st.data;
            const roles = data.roles || [];
            const list = skillFilter(data.skills, st.q, st.scope);
            const seg = (cur, act, opts) => `<div class="cx-segctl" role="group">${opts.map(o => `<button type="button" class="${cur === o[0] ? 'on' : ''}" aria-pressed="${cur === o[0]}" data-sk-act="${act}" data-sk-arg="${o[0]}">${o[1]}</button>`).join('')}</div>`;
            let h = '<div class="cx-row cx-wrap sk-toolbar">'
                + `<label class="cx-lbl sk-search"><span class="cx-vh">搜索技能</span><input id="skQuery" placeholder="搜索名称或描述" value="${escapeAttr(st.q)}" autocomplete="off"></label>`
                + seg(st.scope, 'scope', [['all', '全部'], ['global', '全局'], ['local', '本机'], ['project', '项目']])
                + '<span class="cx-spacer"></span>'
                + seg(st.view, 'view', [['list', '列表'], ['matrix', '角色 × 技能矩阵']])
                + '<button type="button" class="btn btn-sm btn-primary" data-sk-act="new">新建技能</button></div>';
            if (st.creating) h += skillCreateHtml(st.createErr);
            if (st.view === 'matrix') {
                h += skillMatrixHtml(list, roles);
            } else {
                const sk = skFind(st.sel);
                let detail = '<div class="cx-note">选择左侧技能查看详情</div>';
                if (sk && st.dErr[st.sel]) detail = `<div class="sd-warn">技能详情加载失败：${escapeHtml(st.dErr[st.sel])}</div>`;
                else if (sk && st.details[st.sel]) detail = skillDetailHtml(sk, st.details[st.sel], st.forms[st.sel], roles, st.errors[st.sel]);
                else if (sk) detail = '<div class="cx-note">加载中…</div>';
                h += `<div class="pp-split"><div class="sk-list cx-col">${skillListHtml(list, st.sel, roles)}</div><div class="pp-editor cx-col">${detail}</div></div>`;
            }
            const prevScroll = body.scrollTop;
            body.innerHTML = h;
            body.scrollTop = prevScroll;
        }
        async function skSave() {
            skSyncForm();
            const key = skState.sel;
            const sk = skFind(key);
            const f = skState.forms[key];
            if (!sk || !f) return;
            const payload = {description: f.description, activation: f.activation, roles: f.roles, required: f.required,
                             status: f.status, body: f.body, paths: skSplit(f.paths), keywords: skSplit(f.keywords)};
            const res = await fetch('/api/skills/' + encodeURIComponent(sk.name), {method: 'PUT', headers: {'Content-Type': 'application/json'},
                                                                               body: JSON.stringify(payload)});
            const r = await res.json().catch(() => ({}));
            if (!res.ok) {
                skState.errors[key] = r.errors || [{message: r.error || ('HTTP ' + res.status)}];
                renderSkillsTab();
                showToast('保存失败', true);
                return;
            }
            delete skState.errors[key];
            showToast('已保存为本机版本，下一次编译生效');
            await loadSkillsTab(true);
        }
        async function skCellClick(arg) {
            const [key, role] = String(arg).split('|');
            const sk = skFind(key);
            if (!sk || !sk.editable || skState.busy[key]) return;
            const next = skillCellNext(sk, role);
            const old = {roles: sk.roles, activation: sk.activation, mounted: sk.mounted};
            sk.roles = next.roles;
            sk.activation = next.activation;
            skState.busy[key] = true;
            renderSkillsTab();
            try {
                const res = await fetch('/api/skills/' + encodeURIComponent(sk.name), {method: 'PUT', headers: {'Content-Type': 'application/json'},
                                                                                   body: JSON.stringify({roles: next.roles, activation: next.activation})});
                const r = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error((r.errors || []).map(e => e.message).join('；') || r.error || ('HTTP ' + res.status));
                Object.assign(sk, {roles: r.roles || next.roles, activation: r.activation || next.activation, scope: r.scope || sk.scope,
                                   override_of: r.override_of !== undefined ? r.override_of : sk.override_of, lint: r.lint || sk.lint,
                                   lint_level: r.lint_level || sk.lint_level});
                delete skState.details[key];
            } catch (e) {
                Object.assign(sk, old);
                showToast('保存失败：' + String((e && e.message) || e), true);
            } finally {
                delete skState.busy[key];
            }
            renderSkillsTab();
            const cell = document.querySelector(`#settingsSkillsBody [data-sk-arg="${CSS.escape(arg)}"]`);
            if (cell) cell.focus();
        }
        // 新建草稿时预填一段正文骨架：服务端 lint 不接受空正文；建好后在详情里改写
        const SK_BODY_STARTER = '什么时候用：\n- （触发这个技能的场景）\n\n怎么做：\n1. （步骤）\n\n注意：\n- （常见失误）\n';
        async function skCreate() {
            const name = (document.getElementById('skNewName') || {}).value || '';
            const desc = (document.getElementById('skNewDesc') || {}).value || '';
            const res = await fetch('/api/skills', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                    body: JSON.stringify({name: name.trim(), description: desc.trim(), body: SK_BODY_STARTER})});
            const r = await res.json().catch(() => ({}));
            if (!res.ok) {
                skState.createErr = r.error || (r.errors || []).map(e => e.message).join('；') || ('HTTP ' + res.status);
                renderSkillsTab();
                return;
            }
            skState.creating = false;
            skState.createErr = '';
            skState.pending = 'g:' + name.trim();
            showToast('已新建草稿技能');
            await loadSkillsTab(true);
        }
        async function skDelete() {
            const sk = skFind(skState.sel);
            if (!sk || !window.confirm('删除本机版本（覆盖或草稿）？全局默认不受影响。')) return;
            const res = await fetch('/api/skills/' + encodeURIComponent(sk.name), {method: 'DELETE'});
            const r = await res.json().catch(() => ({}));
            if (!res.ok) { showToast('删除失败：' + (r.error || ('HTTP ' + res.status)), true); return; }
            showToast('已删除本机版本');
            await loadSkillsTab(true);
        }
        document.getElementById('settingsSkillsBody').addEventListener('click', (e) => {
            const t = e.target && e.target.closest ? e.target.closest('[data-sk-act]') : null;
            if (!t || t.disabled) return;
            const act = t.dataset.skAct;
            const arg = t.dataset.skArg;
            const st = skState;
            if (act === 'retry') { loadSkillsTab(true); return; }
            if (act === 'scope' || act === 'view') { skSyncForm(); st[act] = arg; renderSkillsTab(); return; }
            if (act === 'sel') { skSyncForm(); st.sel = arg; skLoadDetail(arg); return; }
            if (act === 'new') { skSyncForm(); st.creating = true; st.createErr = ''; renderSkillsTab(); const n = document.getElementById('skNewName'); if (n) n.focus(); return; }
            if (act === 'cancelnew') { skSyncForm(); st.creating = false; renderSkillsTab(); return; }
            if (act === 'create') { skCreate(); return; }
            if (act === 'cell') { skCellClick(arg); return; }
            const f = st.forms[st.sel];
            const sk = skFind(st.sel);
            if (act === 'act' && f) { skSyncForm(); f.activation = arg; renderSkillsTab(); }
            else if (act === 'role' && f) {
                skSyncForm();
                f.roles = f.roles.includes(arg) ? f.roles.filter(r => r !== arg) : f.roles.concat([arg]);
                renderSkillsTab();
            } else if (act === 'save') skSave();
            else if (act === 'delete') skDelete();
            else if (act === 'preview' && sk) {
                const roles = (f && f.roles.length ? f.roles : sk.effective_roles) || [];
                openContextPreview({role: roles[0] || 'dev', project: sk.project || undefined});
            }
        });
        document.getElementById('settingsSkillsBody').addEventListener('input', (e) => {
            if (!e.target || e.target.id !== 'skQuery') return;
            const pos = e.target.selectionStart;
            skSyncForm();
            skState.q = e.target.value;
            renderSkillsTab();
            const q = document.getElementById('skQuery');
            if (q) { q.focus(); q.setSelectionRange(pos, pos); }
        });

        // ---------------- 抽屉入口 ----------------
        // 从卡片上下文 / 注入预览的警告跳转：打开设置抽屉并定位到某个提示词条目或技能
        function openSettingsAt(tab, opts) {
            const o = opts || {};
            const panel = document.getElementById('settingsDrawer');
            if (!panel.classList.contains('show')) openSettingsDrawer();
            if (tab === 'prompts' && o.prompt) {
                if (promptsLoaded && ppState.items.some(x => x.id === o.prompt)) { ppState.sel = o.prompt; ppState.diff = false; ppState.three = false; ppLoadDetail(o.prompt); }
                else { ppState.pending = o.prompt; promptsLoaded = false; }
            }
            if (tab === 'skills' && o.skill) { skState.pending = o.skill; skillsLoaded = false; }
            switchSettingsTab(tab);
        }
        // 全局信息 tab 的角色表：每行补「注入预览」按钮（服务端片段不改，前端按首列角色 id 装饰）
        function decorateRoleRows(root) {
            (root.querySelectorAll('table.sd-roles tr') || []).forEach((tr, i) => {
                if (tr.dataset.pvDecorated) return;
                tr.dataset.pvDecorated = '1';
                const code = tr.querySelector('td code');
                if (i === 0 || !code) { const th = document.createElement('th'); th.textContent = '注入预览'; tr.appendChild(th); return; }
                const td = document.createElement('td');
                td.innerHTML = `<button type="button" class="btn btn-sm" data-ctx-preview-role="${escapeAttr(code.textContent.trim())}">注入预览</button>`;
                tr.appendChild(td);
            });
        }
        document.getElementById('settingsBody').addEventListener('click', (e) => {
            const t = e.target && e.target.closest ? e.target.closest('[data-ctx-preview-role]') : null;
            if (t) openContextPreview({role: t.dataset.ctxPreviewRole});
        });
        document.getElementById('sdTabPrompts').addEventListener('click', () => switchSettingsTab('prompts'));
        document.getElementById('sdTabSkills').addEventListener('click', () => switchSettingsTab('skills'));
        // 交互层只消费既有数据与 API，不参与调度状态流转。
        function interactionText(key) {
            const en = window.LOOP_INTERACTION_EN || {};
            return window.loopI18nLang === 'en' ? (en[key] || key) : key;
        }
        let interactionHealth = null;
        let interactionQuery = '';
        let interactionFocusId = null;
        let interactionFormDraft = {title: '', notes: '', project: '', status: 'later'};
        function interactionInboxRows(reqs, confirms, merges, health) {
            const rows = [];
            reqs.filter(i => !['done', 'dropped'].includes(i.status)).forEach(i => {
                const c = confirms[i.id], m = merges[i.id];
                let reason = '';
                if (c && !c.terminal && !c.action) {
                    const q = c.question || '';
                    reason = /校验|豁免|waive_gate/.test(q) ? '收尾校验需要你裁决'
                        : c.over_limit || /打回|多轮|第.*次/.test(q) ? '评审多次打回，需要人工决定'
                        : '有一个问题需要你确认';
                } else if (m && !m.action) reason = '合并受阻，请检查冲突或 Git 身份';
                else if (i.status === 'later') {
                    // 外部角色等待由调度器写入 summary；只在当前暂停态展示。
                    const tail = String(i.summary || '').split('\n').filter(Boolean).slice(-1)[0] || '';
                    if (/外部.*(?:负责|产出)|external_roles/.test(tail)) reason = '等待外部角色提交产物';
                    else if (/校验.*(?:未通|失败|不过)|waive_gate/.test(tail)) reason = '收尾校验需要你裁决';
                    else if (/合并.*(?:失败|冲突|受阻)|git.*身份/i.test(tail)) reason = '合并受阻，请检查冲突或 Git 身份';
                }
                if (reason) rows.push({id: i.id, title: i.title, reason});
            });
            if (health && !health.ok && health.state !== 'disabled')
                rows.push({id: null, title: '调度器异常', reason: '调度器未正常上报，请查看服务状态'});
            return rows;
        }
        function interactionRenderInbox() {
            const button = document.getElementById('interactionInboxButton');
            if (!button) return;
            const rows = interactionInboxRows(items, confirmations, mergeConflicts, interactionHealth);
            const count = document.getElementById('interactionInboxCount');
            count.textContent = rows.length;
            button.classList.toggle('has-items', rows.length > 0);
            button.setAttribute('aria-label', interactionText('待你处理') + ' · ' + rows.length);
            const box = document.getElementById('interactionInbox');
            const html = rows.length ? rows.map(r => `<div class="interaction-inbox-row">
                <button type="button" class="interaction-inbox-item" data-inbox-id="${r.id || ''}">
                <span data-i18n-skip>${r.id ? '#' + r.id + ' · ' + escapeHtml(r.title) : interactionText(r.title)}</span>
                <small>${interactionText(r.reason)}</small></button>
                <button type="button" class="btn" data-inbox-id="${r.id || ''}">${interactionText('查看')}</button></div>`).join('')
                : `<p>${interactionText('目前没有需要你处理的事项')}</p>`;
            if (box.innerHTML !== html) box.innerHTML = html;
        }
        function interactionOpenDetail() {
            document.body.classList.add('interaction-detail-open');
            document.body.classList.remove('interaction-menu-open');
        }
        function interactionCloseDetail() {
            document.body.classList.remove('interaction-detail-open');
            document.getElementById('interactionBack')?.remove();
            const card = document.querySelector(`[data-interaction-id="${interactionFocusId || selectedId}"]`);
            card?.focus({preventScroll: true});
        }
        function interactionQuickForm() {
            if (!addMode || !document.getElementById('chatInput')) return;
            let box = document.getElementById('interactionQuick');
            if (!box) {
                box = document.createElement('details');
                box.id = 'interactionQuick';
                box.className = 'interaction-quick';
                const d = interactionFormDraft;
                const project = d.project || (selectedProject === ALL_PROJECTS ? autoProjects[0] : selectedProject);
                box.innerHTML = `<summary>${interactionText('直接填表')}</summary>
                    <p class="interaction-fallback" hidden>${interactionText('聊天暂不可用，可以直接建卡')}</p>
                    <form id="interactionQuickForm">
                    <label for="interactionTitle">${interactionText('标题')}</label>
                    <input id="interactionTitle" name="title" required maxlength="200" value="${escapeAttr(d.title)}">
                    <label for="interactionProject">${interactionText('所属项目')}</label>
                    <select id="interactionProject" name="project" required>${autoProjects.map(p => `<option value="${escapeAttr(p)}"${p === project ? ' selected' : ''}>${escapeHtml(displayProjectName(p))}</option>`).join('')}</select>
                    <label for="interactionNotes">${interactionText('需求详情')}</label>
                    <textarea id="interactionNotes" name="notes" maxlength="2000" rows="3">${escapeHtml(d.notes)}</textarea>
                    <label for="interactionStatus">${interactionText('状态')}</label>
                    <select id="interactionStatus" name="status">${['later', 'open'].map(s => `<option value="${s}"${d.status === s ? ' selected' : ''}>${interactionText(STATUS_LABEL[s] || s)}</option>`).join('')}</select>
                    <p>${interactionText('默认为暂缓；选择待开发会进入自动开发')}</p>
                    <button class="btn btn-primary" type="submit">${interactionText('创建需求')}</button>
                    <p id="interactionFormError" role="status" data-i18n-skip></p></form>`;
                document.querySelector('#detail .chat-head').after(box);
                const form = box.querySelector('form');
                form.addEventListener('input', () => { interactionFormDraft = Object.fromEntries(new FormData(form)); });
                form.addEventListener('submit', async ev => {
                    ev.preventDefault();
                    const button = form.querySelector('button');
                    if (button.disabled) return;
                    button.disabled = true;
                    const payload = Object.fromEntries(new FormData(form));
                    try {
                        const res = await fetch('/api/requirements', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
                        const data = await res.json();
                        if (!res.ok) throw new Error('create');
                        interactionFormDraft = {title: '', notes: '', project: payload.project, status: 'later'};
                        await load();
                        selectReq(data.id);
                        interactionOpenDetail();
                        showToast(interactionText('需求已创建'));
                    } catch (err) {
                        document.getElementById('interactionFormError').textContent = interactionText('创建失败，请检查网络或表单后重试');
                    } finally { button.disabled = false; }
                });
            }
            const title = document.querySelector('#detail .chat-head .proc-title');
            if (title && window.loopI18nLang === 'en' && title.textContent.startsWith('聊天建卡'))
                title.textContent = title.textContent.replace('聊天建卡', interactionText('聊天建卡'));
            const status = document.getElementById('chatStatus');
            const bad = !autoProjects.length || (chatState && ['failed', 'error', 'closed'].includes(chatState.status))
                || /失败|不支持|未配置|不可用|暂无可用|failed|unsupported|not configured|unavailable/i.test(status?.textContent || '');
            box.closest('.add-pane')?.classList.toggle('interaction-chat-unavailable', !!bad);
            if (bad) {
                box.open = true;
                box.querySelector('.interaction-fallback').hidden = false;
            }
        }
        function interactionSync() {
            if (!document.getElementById('interactionToolbar')) return;
            interactionRenderInbox();
            document.querySelectorAll('[data-interaction-status]').forEach(button => {
                const active = (button.dataset.interactionStatus || null) === selectedStatus;
                button.setAttribute('aria-pressed', String(active));
            });
            const cards = document.querySelectorAll('#list > .card');
            const vis = autoProjects.length ? visibleItems() : [];
            cards.forEach((card, n) => {
                if (!vis[n]) return;
                card.dataset.interactionId = vis[n].id;
                card.tabIndex = 0;
                card.hidden = !(`${vis[n].id} ${vis[n].title} ${vis[n].notes || ''}`.toLowerCase().includes(interactionQuery));
            });
            interactionQuickForm();
            const detail = document.getElementById('detail');
            if (!document.getElementById('interactionBack')) {
                const back = document.createElement('button');
                back.id = 'interactionBack'; back.className = 'btn interaction-back';
                back.textContent = '← ' + interactionText('返回看板');
                back.addEventListener('click', interactionCloseDetail);
                detail.prepend(back);
            }
        }
        function interactionNew() {
            const project = selectedProject === ALL_PROJECTS ? autoProjects[0] : selectedProject;
            if (!project) { showToast(interactionText('请先添加项目'), true); return; }
            openChatFor(project);
            interactionOpenDetail();
            interactionSync();
            const box = document.getElementById('interactionQuick');
            (box?.open ? document.getElementById('interactionTitle') : document.getElementById('chatInput'))?.focus();
        }
        function interactionHelp(open) {
            const help = document.getElementById('interactionHelp');
            help.hidden = !open;
            if (open) help.querySelector('button').focus();
            else document.getElementById('interactionHelpButton').focus();
        }
        function interactionInit() {
            const toolbar = document.createElement('div');
            toolbar.id = 'interactionToolbar'; toolbar.className = 'interaction-toolbar';
            toolbar.innerHTML = `<input type="search" id="interactionSearch" aria-label="${interactionText('搜索需求')}" placeholder="${interactionText('搜索需求')}  /">
                <button type="button" class="btn" id="interactionNew">+ ${interactionText('新建')}</button>
                <button type="button" class="btn" id="interactionHelpButton" aria-label="${interactionText('键盘快捷键')}">?</button>`;
            document.querySelector('.main-col').prepend(toolbar);
            const tabs = document.createElement('div');
            tabs.className = 'interaction-status-tabs';
            tabs.setAttribute('aria-label', interactionText('筛选状态'));
            function statusTabs() {
                tabs.innerHTML = ['', 'later', 'open', 'doing', 'done', 'dropped'].map(status => `<button type="button" class="btn" data-interaction-status="${status}">${interactionText(status ? STATUS_LABEL[status] || status : '全部状态')}</button>`).join('');
                interactionSync();
            }
            tabs.onclick = ev => {
                const button = ev.target.closest('[data-interaction-status]');
                if (!button) return;
                selectedStatus = button.dataset.interactionStatus || null;
                renderNav(); render();
            };
            toolbar.after(tabs);
            loadMeta().then(statusTabs);
            document.getElementById('interactionNew').onclick = interactionNew;
            document.getElementById('interactionHelpButton').onclick = () => interactionHelp(true);
            document.getElementById('interactionSearch').oninput = ev => { interactionQuery = ev.target.value.trim().toLowerCase(); interactionSync(); };
            const menu = document.createElement('button');
            menu.className = 'btn interaction-menu'; menu.id = 'interactionMenu';
            menu.textContent = '☰'; menu.setAttribute('aria-label', interactionText('菜单'));
            menu.onclick = () => { const open = document.body.classList.toggle('interaction-menu-open'); menu.setAttribute('aria-expanded', String(open)); };
            document.querySelector('.head-right').prepend(menu);
            const inbox = document.getElementById('interactionInbox');
            const inboxButton = document.getElementById('interactionInboxButton');
            inboxButton.title = interactionText('待你处理');
            document.getElementById('interactionInboxLabel').textContent = interactionText('待你处理');
            document.getElementById('interactionHelp').setAttribute('aria-label', interactionText('键盘快捷键'));
            inboxButton.onclick = () => { inbox.hidden = !inbox.hidden; inboxButton.setAttribute('aria-expanded', String(!inbox.hidden)); };
            inbox.onclick = ev => {
                const target = ev.target.closest('[data-inbox-id]');
                if (!target) return;
                inbox.hidden = true; inboxButton.setAttribute('aria-expanded', 'false');
                if (!target.dataset.inboxId) { openSettingsDrawer(); return; }
                selectedProject = ALL_PROJECTS; selectedStatus = null;
                interactionFocusId = Number(target.dataset.inboxId);
                selectReq(interactionFocusId); renderNav(); interactionOpenDetail();
            };
            const help = document.getElementById('interactionHelp');
            help.innerHTML = `<div class="interaction-help-panel"><h2>${interactionText('键盘快捷键')}</h2>${[['N','新建需求'],['/','搜索需求'],['J / K','下一张 / 上一张卡片'],['Enter','打开详情'],['Esc','关闭抽屉'],['?','快捷键帮助']].map(([key, label]) => `<p><kbd>${key}</kbd><span>${interactionText(label)}</span></p>`).join('')}<button type="button" class="btn">${interactionText('关闭')}</button></div>`;
            help.querySelector('button').onclick = () => interactionHelp(false);
            help.onclick = ev => { if (ev.target === help) interactionHelp(false); };
            document.addEventListener('click', ev => {
                if (!ev.target.closest('.interaction-inbox-wrap')) { inbox.hidden = true; inboxButton.setAttribute('aria-expanded', 'false'); }
                if (ev.target.closest('#list .card-title, #list .sf-disp-link, #list .session-row, #navList .nav-add-project')) interactionOpenDetail();
            });
            document.addEventListener('keydown', ev => {
                if (ev.defaultPrevented || ev.isComposing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
                const input = ev.target.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]');
                if (input) return;
                if (ev.key === 'Escape') {
                    if (!help.hidden) interactionHelp(false);
                    else { inbox.hidden = true; inboxButton.setAttribute('aria-expanded', 'false'); interactionCloseDetail(); document.body.classList.remove('interaction-menu-open'); }
                    return;
                }
                if (!help.hidden) { if (ev.key === 'Tab') { ev.preventDefault(); help.querySelector('button').focus(); } return; }
                if (ev.target.closest('.modal-mask.show, .settings-drawer.open, .robot-drawer.open')) return;
                if (ev.key.toLowerCase() === 'n') { ev.preventDefault(); interactionNew(); }
                else if (ev.key === '/') { ev.preventDefault(); document.getElementById('interactionSearch').focus(); }
                else if (ev.key === '?') { ev.preventDefault(); interactionHelp(true); }
                else if (['j', 'k'].includes(ev.key.toLowerCase())) {
                    const cards = [...document.querySelectorAll('#list > .card:not([hidden])')];
                    let index = cards.findIndex(c => c === document.activeElement);
                    index = index < 0 ? (ev.key.toLowerCase() === 'j' ? 0 : cards.length - 1) : Math.max(0, Math.min(cards.length - 1, index + (ev.key.toLowerCase() === 'j' ? 1 : -1)));
                    const card = cards[index];
                    if (card) { ev.preventDefault(); interactionFocusId = Number(card.dataset.interactionId); card.focus({preventScroll: true}); card.scrollIntoView({block: 'nearest'}); }
                } else if (ev.key === 'Enter' && ev.target.matches('#list > .card')) {
                    ev.preventDefault(); selectReq(Number(ev.target.dataset.interactionId)); interactionOpenDetail();
                }
            });
            new MutationObserver(() => interactionQuickForm()).observe(document.getElementById('detail'), {childList: true, subtree: true, characterData: true});
            async function health() {
                try { const r = await fetch('/api/dispatcher-health', {cache: 'no-store'}); if (r.ok) interactionHealth = await r.json(); }
                catch (e) { interactionHealth = {ok: false}; }
                interactionRenderInbox();
            }
            health();
            setInterval(() => { if (!document.hidden) health(); }, 15000);
            interactionSync();
        }
        document.addEventListener('DOMContentLoaded', interactionInit);
        // ==================== #4 模型调参抽屉（右侧；角色×模型主表 + 角色×模型统计） ====================
        // 触发：信息条机器人位（#projectBar 容器事件委托，innerHTML 重绘不失联）；仅具体项目可点。
        // 数据：打开时 GET 项目 models（四层链生效值 + 别名池）与 model-stats（角色×模型分组），
        // 保存 PUT（成功 toast「下一张卡生效」；4xx 行内错误不清空选择）；全程 fail-safe 不炸主页。
        let robotProject = null;    // 抽屉所开项目（切项目自动关闭）
        let robotLoading = false;
        let robotStatsRange = 'all';   // #42 统计时间范围（all/7/30，重渲染时保持选中）
        function openRobotDrawer(project) {
            if (!project || project === ALL_PROJECTS) return;
            robotProject = project;
            document.getElementById('robotMask').classList.add('show');
            const panel = document.getElementById('robotDrawer');
            panel.inert = false;   // 展开才可聚焦（沿设置抽屉先例）
            panel.classList.add('show');
            loadRobotDrawer(true);
        }
        function closeRobotDrawer() {
            robotProject = null;
            document.getElementById('robotMask').classList.remove('show');
            const panel = document.getElementById('robotDrawer');
            panel.classList.remove('show');
            panel.inert = true;
        }
        // 主表渲染纯函数（node harness 断言）：四层来源标注 / 清除覆盖选项 / 别名池选项 /
        // 覆盖不在池时补选项并注明 / 别名池降级为手填 input；全部文本位 escapeHtml、属性位 escapeAttr。
        // #6：行内「保存」按钮移除，表头末列右端一个「保存全部」（批量提交，控件带 data-cur 原值）。
        // 模型下拉按 aliases.groups 渲染 <optgroup>（groups 缺省回落扁平 items；
        // provider 缺失/悬空归「（未归属）」由服务端分组）。
        function robotModelRowsHtml(roles, aliases, warning) {
            const items = (aliases && Array.isArray(aliases.items)) ? aliases.items : [];
            const groups = (aliases && Array.isArray(aliases.groups)) ? aliases.groups : [];
            const rows = (roles || []).map(r => {
                const override = String(r.override || '');
                let control;
                if (warning) {
                    control = `<input class="rb-input" data-role="${escapeAttr(r.role)}" data-cur="${escapeAttr(override)}" value="${escapeAttr(override)}" placeholder="手填模型别名（仅格式校验）">`;
                } else {
                    const optFor = m => `<option value="${escapeAttr(m)}"${m === override ? ' selected' : ''}>${escapeHtml(m)}</option>`;
                    const opts = ['<option value="">清除覆盖（回退全局）</option>'];
                    if (groups.length) {
                        for (const g of groups) {
                            const gitems = (g && Array.isArray(g.items)) ? g.items : [];
                            if (!gitems.length) continue;
                            opts.push(`<optgroup label="${escapeAttr(String((g && g.label) || ''))}">${gitems.map(optFor).join('')}</optgroup>`);
                        }
                    } else {
                        opts.push(items.map(optFor).join(''));
                    }
                    if (override && items.indexOf(override) === -1) {
                        opts.push(`<option value="${escapeAttr(override)}" selected>${escapeHtml(override)}（当前覆盖，不在别名池）</option>`);
                    }
                    control = `<select class="rb-select" data-role="${escapeAttr(r.role)}" data-cur="${escapeAttr(override)}">${opts.join('')}</select>`;
                }
                return `<tr><td><code>${escapeHtml(r.role)}</code><div class="rb-dim">${escapeHtml(r.display || '')}</div></td>`
                    + `<td>${escapeHtml(r.model)}<div class="rb-dim">${escapeHtml(r.source_label || '')}</div></td>`
                    + `<td>${control}<div class="rb-msg" data-msg="${escapeAttr(r.role)}"></div></td>`
                    + '</tr>';
            }).join('');
            const warn = warning
                ? `<div class="sd-warn">别名池不可用（${escapeHtml(warning)}）——模型列已降级为手填输入，保存时仅做格式校验。</div>` : '';
            return '<div class="rb-head"><h3>角色 × 模型（项目级覆盖）</h3>'
                + '<button type="button" class="btn rb-save-all" id="robotSaveAllBtn">保存全部</button></div>'
                + warn
                + '<table class="rb-table"><tr><th>角色</th><th>当前生效模型</th><th>项目级覆盖</th></tr>'
                + rows + '</table>'
                + '<p class="sd-note">保存后下一张卡生效（已启动会话沿用旧模型）；「清除覆盖（回退全局）」= 删除本项目的角色级覆盖。</p>';
        }
        // #6 批量结果文案纯函数（harness 锁定）：全成功 / 含失败 / 零变更
        function robotBatchResultText(applied, failed) {
            const a = Math.max(0, Number(applied) || 0);
            const f = Math.max(0, Number(failed) || 0);
            if (!a && !f) return '没有改动';
            if (f > 0) return '保存完成：生效 ' + a + ' 项，失败 ' + f + ' 项（失败项保留当前选择，可重试）';
            return '已保存 ' + a + ' 项（下一张卡生效）';
        }
        // 统计渲染纯函数：分组行 / 状态分布 / 空态「暂无会话数据」 / 数值格式化
        // #42 缓存命中列：cached tokens + 三态计数（命中/未提供/不适用），命中率有值才显示
        // （Σcached 与 Σinput 均有正值时服务端才给 rate；上下文峰值口径不变、永不冒充命中）
        function robotCacheCell(g) {
            const cs = g.cache_states || {};
            const n = v => Math.max(0, Number(v) || 0);
            const parts = ['命中 ' + n(cs.hit), '未提供 ' + n(cs.unknown), '不适用 ' + n(cs.na)];
            let rate = '';
            if (g.cache_hit_rate != null && isFinite(g.cache_hit_rate)) {
                // 显式 0 命中是真实返回（报 0 也是 hit），照出 0%；null（分母缺失）不出数字
                rate = '，命中率 ' + Math.round(g.cache_hit_rate * 100) + '%';
            }
            return escapeHtml(formatTokens(n(g.cached_tokens)))
                + '<div class="rb-dim">' + escapeHtml(parts.join(' / ') + rate) + '</div>';
        }
        // #42 时间范围 → since 参数（YYYY-MM-DD，含当日；all/非法返回空串 = 不带参数）
        function robotStatsSinceParam(range) {
            const days = Number(range);
            if (!range || range === 'all' || !isFinite(days) || days <= 0) return '';
            const d = new Date();
            d.setDate(d.getDate() - (days - 1));
            const p = x => String(x).padStart(2, '0');
            return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
        }
        function robotStatsHtml(groups) {
            const gs = groups || [];
            if (!gs.length) return '<div class="sd-note">暂无会话数据</div>';
            const num = v => String(typeof v === 'number' && isFinite(v) ? Math.round(v) : 0);
            const rows = gs.map(g => {
                const st = Object.keys(g.statuses || {}).map(k => `${STATUS_LABEL[k] || k} ${g.statuses[k]}`).join(' / ');
                return `<tr><td><code>${escapeHtml(g.role)}</code></td><td>${escapeHtml(g.model)}</td>`
                    + `<td>${num(g.sessions)}</td><td>${escapeHtml(formatTokens(g.ctx_tokens))}</td>`
                    + `<td>${robotCacheCell(g)}</td>`
                    + `<td>${num(g.tool_count)}</td><td>${escapeHtml(st || '—')}</td></tr>`;
            }).join('');
            return '<table class="rb-table"><tr><th>角色</th><th>模型</th><th>会话数</th>'
                + '<th>上下文峰值合计</th><th>缓存命中</th><th>工具次数</th><th>关联卡状态</th></tr>' + rows + '</table>'
                + '<p class="sd-note">模型列 = 会话启动时记录的显示名；上下文峰值/工具次数按会话关联运行记录求和'
                + '（关联不到计 0，含毒/失败会话按行计入）；状态分布按去重后的关联卡统计。</p>'
                + '<p class="sd-note">缓存命中 = 供应商返回的缓存 token 求和；未返回记「未提供」，零完成请求记「不适用」；'
                + '命中率 = Σ缓存命中/Σ输入 token（仅计返回了字段的运行；显式 0 命中出 0%，分母缺失不出数字）。'
                + '上下文峰值为非完整消耗口径，不是缓存命中。</p>';
        }
        async function loadRobotDrawer() {
            const project = robotProject;
            if (!project || robotLoading) return;
            robotLoading = true;
            const body = document.getElementById('robotBody');
            body.innerHTML = '<div class="sd-note">加载中…</div>';
            try {
                const base = '/api/projects/' + encodeURIComponent(project);
                const since = robotStatsSinceParam(robotStatsRange);
                const [mres, sres] = await Promise.all([
                    fetch(base + '/models', {cache: 'no-store'}),
                    fetch(base + '/model-stats' + (since ? '?since=' + since : ''), {cache: 'no-store'}),
                ]);
                const md = await mres.json().catch(() => ({}));
                const sd = await sres.json().catch(() => ({}));
                if (robotProject !== project) return;   // 过期响应丢弃（快速切项目防串台）
                if (!mres.ok) throw new Error(md.error || ('HTTP ' + mres.status));
                const stats = sres.ok ? robotStatsHtml(sd.groups)
                    : '<div class="sd-warn">统计加载失败，可点「重新加载」重试。</div>';
                const rangeOpts = [['all', '全部'], ['7', '近 7 天'], ['30', '近 30 天']]
                    .map(([v, label]) => `<option value="${v}"${v === robotStatsRange ? ' selected' : ''}>${label}</option>`)
                    .join('');
                body.innerHTML = '<div class="sd-group">'
                    + robotModelRowsHtml(md.roles, md.aliases, md.aliases && md.aliases.warning)
                    + '</div><div class="sd-group"><h3>角色 × 模型统计</h3>'
                    + '<select id="robotStatsRange" class="rb-select" title="按运行结束时间过滤统计">' + rangeOpts + '</select>'
                    + stats + '</div>';
                const allBtn = document.getElementById('robotSaveAllBtn');
                if (allBtn) allBtn.addEventListener('click', () => saveRobotModelsAll(project));
                const rangeSel = document.getElementById('robotStatsRange');
                if (rangeSel) rangeSel.addEventListener('change', () => {
                    robotStatsRange = rangeSel.value || 'all';
                    loadRobotDrawer();
                });
            } catch (e) {
                if (robotProject !== project) return;
                body.innerHTML = '<div class="sd-warn">模型数据加载失败：' + escapeHtml(String((e && e.message) || e)) + '</div>'
                    + '<button type="button" class="btn" id="robotRetryBtn">重试</button>';
                const rb = document.getElementById('robotRetryBtn');
                if (rb) rb.addEventListener('click', loadRobotDrawer);
            } finally {
                robotLoading = false;
            }
        }
        // 单角色底层请求（#6 重构：不自行 toast/重拉，返回 {ok, changed, model, error} 供批量汇总；
        // 行内错误照旧展示、不清空选择）。语义与 P2 一致：PUT /api/projects/<name>/models。
        async function saveRobotModel(project, role) {
            const body = document.getElementById('robotBody');
            const ctrl = body.querySelector('.rb-select[data-role="' + role + '"], .rb-input[data-role="' + role + '"]');
            const changedVal = c => String(c.value == null ? '' : c.value) !== (c.getAttribute('data-cur') || '');
            const payload = {role: role};
            if (ctrl && changedVal(ctrl)) payload.model = String(ctrl.value == null ? '' : ctrl.value);
            if (!('model' in payload)) return {ok: true, changed: false, model: ''};
            const msg = body.querySelector('.rb-msg[data-msg="' + role + '"]');
            if (msg) msg.textContent = '';
            let resp;
            try {
                resp = await fetch('/api/projects/' + encodeURIComponent(project) + '/models', {
                    method: 'PUT', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
            } catch (e) {
                const err = '保存失败：' + ((e && e.message) || e);
                if (msg) msg.textContent = err;
                return {ok: false, error: err};
            }
            const d = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                const err = d.error || ('保存失败（HTTP ' + resp.status + '）');   // 行内错误，不清空选择
                if (msg) msg.textContent = err;
                return {ok: false, error: err};
            }
            return {ok: true, changed: !!d.changed, model: d.model || ''};
        }
        // #6 批量保存：收集 value !== data-cur 的控件 → 无改动 toast；否则按角色去重
        // 串行复用单角色 PUT → 汇总反馈（robotBatchResultText）；
        // 全成功才重拉（失败项保留当前选择可重试）。
        async function saveRobotModelsAll(project) {
            const body = document.getElementById('robotBody');
            const ctrls = Array.from(body.querySelectorAll('.rb-select, .rb-input'));
            const roles = [];
            for (const ctrl of ctrls) {
                const cur = String(ctrl.value == null ? '' : ctrl.value);
                if (cur === (ctrl.getAttribute('data-cur') || '')) continue;
                const role = ctrl.getAttribute('data-role');
                if (role && roles.indexOf(role) === -1) roles.push(role);
            }
            if (!roles.length) { showToast('没有改动'); return; }
            const btn = document.getElementById('robotSaveAllBtn');
            if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
            let applied = 0, failed = 0;
            for (const role of roles) {
                const r = await saveRobotModel(project, role);
                if (r && r.ok) applied++; else failed++;
            }
            if (btn) { btn.disabled = false; btn.textContent = '保存全部'; }
            showToast(robotBatchResultText(applied, failed), failed > 0);
            if (!failed) loadRobotDrawer();   // 全成功才重拉（生效值与来源刷新）
        }
        document.getElementById('projectBar').addEventListener('click', (e) => {
            const btn = e.target && e.target.closest ? e.target.closest('.pb-robot') : null;
            if (!btn || btn.getAttribute('aria-disabled') === 'true' || btn.disabled) return;
            if (selectedProject === ALL_PROJECTS) return;
            openRobotDrawer(selectedProject);
        });
        document.getElementById('robotMask').addEventListener('click', closeRobotDrawer);
        document.getElementById('robotCloseBtn').addEventListener('click', closeRobotDrawer);
        document.getElementById('robotReloadBtn').addEventListener('click', loadRobotDrawer);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeRobotDrawer();
        });

        // #426 防线 B：上次写入 #list 的 HTML 签名（空态 = 空串，null = 尚未写入）；签名相同则不写 innerHTML，
        // 避免心跳期整列卡片节点重建导致呼吸 CSS 动画从 0% 重启。
        // 短路前提：cardHtml 输入面（items、selectedId/selectedSid/selectedDispatcher/selectedArtifact/
        // selectedReqChat、flashId、sessionInfo、artifactInfo、agentSessions、mergeConflicts、confirmations、
        // roleMeta、expandedAgents、cardExpandState）不含随墙钟变化的内容；新增此类卡片内容时必须同步调整本机制。
        // 另：#list 的 innerHTML 只允许本函数写入，外部清空需同步重置 listHtmlSig。
        let listHtmlSig = null;
        function renderList() {
            const list = document.getElementById('list');
            const ws = document.querySelector('.workspace');
            const cards = (autoProjects.length ? visibleItems() : []).map(i => cardHtml(i)).join('');
            // 聊天建卡页固定在右栏、列表留在中间（与看板一贯布局一致）：列表为空时也保留列表列，放一句提示；
            // 只有不在建卡页且列表为空时才收起列表列、让右栏空态占满
            const html = cards || (addMode && autoProjects.length
                ? '<div class="empty list-empty-hint">该项目还没有需求，在右侧和 Agent 聊完即可建卡</div>' : '');
            // empty-main 与 innerHTML 解耦：class 维护幂等（DOMTokenList 对已存在/不存在的 token 不改属性）、
            // 不重建节点，故在签名短路早退之前执行
            if (html) ws.classList.remove('empty-main'); else ws.classList.add('empty-main');
            if (html === listHtmlSig) return;
            listHtmlSig = html;
            list.innerHTML = html;
            if (typeof interactionSync === 'function') interactionSync();
        }

        // 右栏：添加表单 / 需求详情视图 / 会话执行过程视图 / 会话产物详情视图；
        // 当前可见列表为空（无需求/无项目）时列表列隐藏、右栏占满主内容区展示空态文案并居中；
        // 列表非空但选中项不存在或不在当前项目时回到空状态（显示选择提示）
        // 右栏：添加表单 / 需求详情视图 / 需求助手视图 / 会话执行过程视图 / 会话产物详情视图；
        // 当前可见列表为空（无需求/无项目）时列表列隐藏、右栏占满主内容区展示空态文案并居中；
        // 列表非空但选中项不存在或不在当前项目时回到空状态（显示选择提示）
        function renderDetailPane() {
            const pane = document.getElementById('detail');
            if (addMode) {
                pane.innerHTML = addFormHtml();
                bindAddForm();
                return;
            }
            const vis = visibleItems();
            if (!autoProjects.length || vis.length === 0) {
                selectedId = null;
                selectedSid = null;
                selectedAgentSession = null;
                selectedDispatcher = false;
                selectedArtifact = null;
                selectedReqChat = false;
                pane.innerHTML = !autoProjects.length
                    ? '<div class="empty">暂无项目，请先在 projects.json 中配置</div>'
                    : selectedStatus
                        ? `<div class="empty">当前视图暂无${STATUS_PT_LABEL[selectedStatus]}需求</div>`
                        : selectedProject === ALL_PROJECTS
                            ? '<div class="empty">所有项目下暂无需求，点击项目名右侧的 + 进入聊天建卡</div>'
                            : '<div class="empty">该项目暂无需求，点击项目名右侧的 + 进入聊天建卡</div>';
                return;
            }
            if (!selectedId || !vis.some(i => i.id === selectedId)) {
                selectedId = null;
                selectedSid = null;
                selectedAgentSession = null;
                selectedDispatcher = false;
                selectedArtifact = null;
                selectedReqChat = false;
                pane.innerHTML = '<div class="empty">从中间列表选择需求查看详情</div>';
                return;
            }
            const item = items.find(i => i.id === selectedId);
            if (selectedReqChat) {
                // #326 需求助手独立视图：渲染后加载对话（DOM 就位后再取 #reqChatEvents，避免拿到上一屏旧节点）
                pane.innerHTML = reqAssistantHtml(item);
                loadReqChat(item.id);
            } else if (selectedArtifact) {
                pane.innerHTML = artifactHtml(item);
                loadArtifactContent(item.id, selectedArtifact);
            } else if (selectedAgentSession) {
                const row = (agentSessions || []).find(x => x.id === selectedAgentSession && x.req_id === item.id);
                if (row) {
                    pane.innerHTML = agentSessionHtml(item, row);
                    // #421：切换会话/全量重建 → 复位状态行视图态（手动态与事件流基准不沿用上一会话）
                    agentStatusManualOpen = null;
                    lastProcEventAtMs = null;
                    // #284 会话事件防串：面板打标本次会话，跨会话轮询/迟到响应据此丢弃
                    const evPanel = document.getElementById('procEvents');
                    if (evPanel) evPanel.dataset.sid = row.session_id || '';
                    if (row.session_id) refreshProcess(item.id, row.session_id);
                    renderAgentChatStatus(row);
                    restoreAgentChatDraft(item.id, row);
                } else {
                    selectedAgentSession = null;
                    selectedSid = null;
                    pane.innerHTML = detailHtml(item);
                    afterDetailRender(item);
                }
            } else if (agentDraft && agentDraft.reqId === item.id && item.status !== 'done'
                       && !selectedSid && !selectedDispatcher && !selectedArtifact && !selectedReqChat) {
                // #431「新建聊天」草稿态：复用会话详情视图渲染合成行（无注册表行、无 fetch、无落库）。
                // 守卫含四个非草稿子视图（互斥第二道防线）与 done 判定（终态卡不给草稿视图）
                pane.innerHTML = agentSessionHtml(item, agentDraftRow(item));
                agentStatusManualOpen = null;
                lastProcEventAtMs = null;
                renderAgentChatStatus(null, agentChatBusy);   // row=null → P6「新会话（未开始）」档
                restoreAgentDraft(item.id);
            } else if (selectedSid) {
                pane.innerHTML = processHtml(item, selectedSid);
                const evPanel = document.getElementById('procEvents');
                if (evPanel) evPanel.dataset.sid = selectedSid;
                refreshProcess(item.id, selectedSid);
            } else if (selectedDispatcher) {
                pane.innerHTML = dispatcherHtml(item);
                refreshDispatcherEvents(item.id);
            } else {
                pane.innerHTML = detailHtml(item);
                afterDetailRender(item);   // 概要分页拉评审记录；上下文分页挂载执行档案（55-context.js）
            }
        }

        // 右栏需求详情视图（#326 起回归纯详情：无聊天区、长文自然展开、整页滚动；
        // 需求助手在中间栏会话信息栏置顶行进入独立视图；调度器 ACP 会话信息见左栏卡片会话入口与执行过程头部）
        function detailHtml(i) {
            const canDelete = !['doing', 'done'].includes(i.status);
            const meta = [`<span class="meta-chip chip-project">${escapeHtml(displayProjectName(i.project))}</span>`, `创建于 ${escapeHtml(formatTime(i.created_at))}`];
            if (i.started_at) meta.push(`开始于 ${escapeHtml(formatTime(i.started_at))}`);
            if (i.status === 'done' && i.completed_at) meta.push(`完成于 ${escapeHtml(formatTime(i.completed_at))}`);
            // #350 建卡模型有值才展示（存量卡/页面人工建卡 model 为空则不显示该段）
            if (i.model) meta.push(`创建人：${escapeHtml(i.model)}`);
            const nodeLabel = currentNodeLabel(i);  // #419：当前节点（快照推导）；phase 历史只读，不再渲染
            if (nodeLabel) meta.push(`当前节点：${escapeHtml(nodeLabel)}`);
            meta.push(`编号：${i.id}`);
            // 需求详情/开发总结与产物详情、执行过程 message 事件共用增强 markdown 渲染
            // （先整体 escapeHtml 再语法转换，安全基线一致），列表/粗体/行内代码/表格不再以原文平铺
            const notes = i.notes ? renderArtifactMd(i.notes) : '暂无需求详情';
            const summary = i.summary ? `<div class="detail-section-label">开发总结</div><div class="detail-notes">${renderArtifactMd(i.summary)}</div>` : '';
            // #342 评审记录区块：#346 起默认整体隐藏（内联 display:none），loadReviews 按结果决定是否显示；
            // 内容异步拉取避免详情主查询联 req_reviews 表；无记录不占位、失败时显示失败提示
            const reviews = `<div id="detailReviewsWrap" style="display:none"><div class="detail-section-label">评审记录</div><div id="detailReviews"></div></div>`;
            // #386 merge 冲突待处理（doing + 冲突表命中）：渲染「重试合并 / 人工接管」，与卡片头部按钮同机制
            const mergeActions = (i.status === 'doing' && mergeConflicts[i.id])
                ? `<div class="detail-actions"><button class="btn" onclick="decideMerge(${i.id},'retry')">重试合并</button><button class="btn" onclick="decideMerge(${i.id},'takeover')">人工接管</button></div>` : '';
            // 上下文分页（55-context.js）：头部与元信息共用，正文换成执行档案视图 #ctxPane
            if (detailTab === 'context') {
                return `
            <div class="detail-pane">
                <div class="detail-head">
                    ${statusBadgeHtml(i)}
                    <div class="detail-title">${escapeHtml(i.title)}</div>
                </div>
                <div class="detail-meta">${meta.join(' · ')}</div>
                ${detailTabsHtml(i)}
                <div class="cx-pane" id="ctxPane" data-req="${i.id}"></div>
            </div>`;
            }
            return `
            <div class="detail-pane">
                <div class="detail-head">
                    ${statusBadgeHtml(i)}
                    <div class="detail-title">${escapeHtml(i.title)}</div>
                </div>
                <div class="detail-meta">${meta.join(' · ')}</div>
                ${detailTabsHtml(i)}
                ${mergeActions}
                <div class="detail-section-label">需求详情</div>
                <div class="detail-notes${i.notes ? '' : ' empty'}">${notes}</div>
                ${summary}
                ${reviews}
                <div class="detail-actions">
                    <button class="btn" onclick="openEdit(${i.id})">编辑</button>
                    ${canDelete ? `<button class="btn btn-danger" onclick="delReq(${i.id})">删除</button>` : ''}
                </div>
            </div>`;
        }

        // #342 评审记录：详情渲染后异步拉取按轮展示（append-only）；
        // 面板打标需求 id，切换需求后的迟到响应据此丢弃（防串，同 #284 会话事件模式）
        async function loadReviews(reqId) {
            const el = document.getElementById('detailReviews');
            if (!el) return;
            el.dataset.req = reqId;
            // #346 区块默认隐藏：无记录保持隐藏，有记录/加载失败时恢复显示
            const wrap = document.getElementById('detailReviewsWrap');
            try {
                const res = await fetch(`/api/requirements/${reqId}/reviews`);
                if (!res.ok) throw new Error('http ' + res.status);
                const rows = await res.json();
                const cur = document.getElementById('detailReviews');
                if (!cur || cur.dataset.req != reqId) return;
                if (!rows.length) return; // 无评审记录：区块保持隐藏，不填充文案
                if (wrap) wrap.style.display = '';
                cur.innerHTML = rows.map(r => {
                    const head = [`第 ${r.round} 轮`];
                    if (r.verdict) head.push(`结论：<span class="verdict">${escapeHtml(r.verdict)}</span>`);
                    if (r.reviewer) head.push(`评审人：${escapeHtml(r.reviewer)}`);
                    head.push(escapeHtml(formatTime(r.created_at)));
                    return `<div class="review-item"><div class="review-head">${head.join(' · ')}</div>` +
                           `<div class="detail-notes">${renderArtifactMd(r.content || '')}</div></div>`;
                }).join('');
            } catch (e) {
                const cur = document.getElementById('detailReviews');
                if (cur && cur.dataset.req == reqId) {
                    if (wrap) wrap.style.display = '';
                    cur.innerHTML = '<div class="review-head">评审记录加载失败</div>';
                }
            }
        }

        // #326 右栏需求助手视图（独立视图，复用 .detail-pane.fill 模式）：
        // 头部=需求标题（点击返回详情）+ 状态徽标 + 会话信息行（session id · 模型，
        // 由 renderReqChatStatus 随 reqChatState 填充）；中部=对话消息流（独立滚动）；
        // 底部=状态行+输入框+按钮常驻。交互函数族（loadReqChat/refreshReqChat/renderReqChatEvents/
        // renderReqChatStatus/bindReqChatInput/reqChatAction/scheduleReqChatPoll）按固定 DOM id 取节点复用。
        function reqAssistantHtml(i) {
            return `
            <div class="detail-pane fill req-assistant-pane">
                <div class="proc-head">
                    <div class="detail-head">
                        <span class="badge ${i.status}">${STATUS_LABEL[i.status] || i.status}</span>
                        <div class="detail-title"><span class="pill ra-title" title="返回需求详情" onclick="selectReq(${i.id})">${escapeHtml(i.title)}</span></div>
                    </div>
                    <div class="detail-meta">需求助手 · <span id="reqChatHeadMeta">正在加载…</span></div>
                </div>
                <div class="reqchat-pane">
                    <div class="reqchat-scroll-wrap">
                        <div class="reqchat-events" id="reqChatEvents"><div class="chat-msg sys">正在加载对话记录…</div></div>
                        <button class="reqchat-back-bottom" id="reqChatBackBottom" onclick="reqChatBackToBottom()" title="回到最新">回到最新</button>
                    </div>
                    <div class="chat-input-row">
                        <div class="chat-status" id="reqChatStatus"></div>
                        <textarea id="reqChatInput" placeholder="问关于这个需求的问题；也可明确要求修改需求详情/产物，或在 TAPD 发评论。回车发送（Shift+Enter 换行）" maxlength="4000"></textarea>
                        <button class="btn btn-primary" id="reqChatSend">发送</button>
                    </div>
                </div>
            </div>`;
        }

        // ---- 需求助手（#326：中间栏会话信息栏置顶入口 + 右栏独立视图，前身为 #302 详情页常驻聊天），独立于调度器 ----
        // 任何状态可用；默认只读，显式要求才写（改需求详情/产物/TAPD 评论）。
        // 会话生命周期：created -> running -> idle；interrupted 可继续；closed/failed 可新建。
        let reqChatEvents = [];   // 当前需求助手对话事件累积（切换需求时重置）
        let reqChatState = null;  // {id, status, session_id, model, error}
        let reqChatTimer = null;
        // 需求助手增量游标与在途互斥（重复气泡修复）：
        // reqChatLastId = 服务端事件 id 游标，与 reqChatEvents 解耦，只在响应落地时前进——
        //   旧写法从数组末尾取 id，poke（每写一条事件一条）+ 发送后的主动全量刷新并发时会拿同一旧游标
        //   重复拉取同一区间再 concat，同一事件进数组两次（重复气泡）。
        // reqChatFetchSeq = 在途请求序号，只接受最新一次响应，迟到响应丢弃。
        // reqChatFetchAt = 最近一次增量请求发起时间（0=窗口空闲），窗口内合并 poke 避免并发同游标重拉；
        //   按时间计算、到期自动放行，响应异常也不会卡死刷新链。
        let reqChatLastId = 0;
        let reqChatFetchSeq = 0;
        let reqChatFetchAt = 0;
        const REQ_CHAT_FETCH_COALESCE_MS = 800;
        // 需求助手轮询间隔（回显时效优化：与聊天/执行过程统一压到 1 秒）
        const REQ_CHAT_REFRESH_MS = 1000;
        // #325 长对话按轮折叠：默认只展开最近 REQ_CHAT_ROUNDS_VISIBLE 轮，更早轮次折叠为一行；
        // reqChatShowAllRounds 为用户点击展开全部后的持久状态（跨重建保持），切换需求时重置
        const REQ_CHAT_ROUNDS_VISIBLE = 9;
        let reqChatShowAllRounds = false;
        // 失败兜底文案（与建卡页 .chat-msg.sys 同款弱化行）；初始占位「正在加载对话记录…」由模板写入；
        // #323 起无对话/过滤后为空不再显示空态提示行，事件区留空白
        const REQ_CHAT_ERROR_HINT = '<div class="chat-msg sys">对话记录加载失败，请刷新页面重试</div>';
        // 需求助手生命周期回执前缀（对齐 #107 建卡对话 isChatLifecycleEvent 做法，独立一组）：
        // 生命周期事件照常写 req_chat_events 表供排障，仅前端渲染时过滤，由状态行（renderReqChatStatus）覆盖展示；
        // 错误详情/中断原因/工具操作回执不在列表中，正常展示。前缀必须精确，避免误伤保留事件
        // （如「已取消本轮，对话中断（可继续对话）」隐藏 vs「本轮超时，对话已中断（可继续对话）」保留）。
        const REQ_CHAT_LIFECYCLE_PREFIXES = [
            '对话已建立（',
            '对话已恢复（',
            '本轮完成',
            '已取消本轮',
            '对话已关闭',
            '看板服务已重启',
            '对话进程已失效',
        ];

        function isReqChatLifecycleEvent(event) {
            if (event.kind !== 'system') return false;
            const content = String(event.content || '').trim();
            return REQ_CHAT_LIFECYCLE_PREFIXES.some(prefix => content.startsWith(prefix));
        }

        // 离开需求助手视图（切详情/会话/调度器/产物/建卡）时兜底停掉 2 秒轮询，避免打向已卸载视图
        function clearReqChatTimer() {
            if (reqChatTimer) { clearTimeout(reqChatTimer); reqChatTimer = null; }
        }

        // 由 renderDetailPane 在 reqAssistantHtml 写入 DOM 后调用（DOM 就位时取 #reqChatEvents，避免拿到上一屏
        // 已脱离文档的旧节点/空节点导致「正在加载对话记录…」永久停留）；拉取失败给出明确兜底而非静默
        async function loadReqChat(reqId) {
            const box = document.getElementById('reqChatEvents');
            if (!box) return;
            reqChatEvents = [];
            reqChatState = null;
            reqChatLastId = 0;              // 切换需求：游标与在途窗口一并重置（旧视图在途响应已由选中断言丢弃）
            reqChatFetchAt = 0;
            reqChatShowAllRounds = false;   // #325 切换需求重置折叠展开状态
            reqChatStatusManualOpen = null; // #430 切换需求重置状态卡展开态（跟随自动展开）
            clearReqChatTimer();
            // #325 事件委托（box 随详情视图每次重建，监听随旧节点回收，无重复绑定）：
            // 工具消息点击展开/收起单行折叠；滚动时同步「回到最新」按钮显隐
            box.addEventListener('click', (e) => {
                const t = e.target && e.target.closest ? e.target.closest('.chat-msg.tool') : null;
                if (t) t.classList.toggle('open');
            });
            box.addEventListener('scroll', () => updateReqChatBackBottom(), {passive: true});
            // 贴底保持：对话区可视高度变化（建卡会话行异步填充、窗口缩放、输入框拖高等）时若原在底部则重新贴底，
            // 否则打开长对话会停在距底几十像素处、首屏即悬「回到最新」；贴底标记由 updateReqChatBackBottom 维护
            const ro = new ResizeObserver(() => {
                if (!box.isConnected) { ro.disconnect(); return; }
                if (box.dataset.atBottom === '1') { box.scrollTop = box.scrollHeight; updateReqChatBackBottom(); }
            });
            ro.observe(box);
            try {
                const res = await fetch(`/api/requirements/${reqId}/chat`, {cache: 'no-store'});
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                if (selectedId !== reqId || !selectedReqChat) return;  // 已切走（其它需求/其它视图），丢弃过期结果
                reqChatState = data.chat;
                if (reqChatState) {
                    await refreshReqChat(reqId, true);
                } else {
                    box.innerHTML = '';
                    renderReqChatStatus();
                }
                bindReqChatInput(reqId);
            } catch (e) {
                if (selectedId === reqId && box.isConnected) box.innerHTML = REQ_CHAT_ERROR_HINT;
            }
        }

        // 聊天事件流合并（纯函数，tests/process_markdown_harness.js T18 断言）：
        // 建卡对话（chatEvents）与需求助手（reqChatEvents）共用——两条链路的服务端事件同形（chat_events /
        // req_chat_events 均按 id 单调递增）、刷新语义相同：full=true 用服务端结果整体替换（对账真值，
        // 丢弃本地累积）；增量按事件 id 去重后升序返回。
        // 去重是必需的兜底：服务端每写一条事件发一次 SSE poke，发送消息后前端还会主动做一次全量刷新，
        // 并发/迟到的增量响应会带着同一区间再拼接一次，不去重就会出现同一事件的重复气泡。
        // 无 id 的事件（两条链路都无本地乐观回显，服务端事件恒有 id）跳过，避免污染去重键。
        function mergeChatStreamEvents(prev, incoming, full) {
            const byId = new Map();
            const list = (full ? [] : (prev || [])).concat(incoming || []);
            for (const e of list) {
                if (!e || e.id == null) continue;
                byId.set(e.id, e);
            }
            return Array.from(byId.values()).sort((a, b) => a.id - b.id);
        }

        async function refreshReqChat(reqId, full) {
            const box = document.getElementById('reqChatEvents');
            if (!box) return;
            // 互斥/合并：增量请求刚发起（合并窗口内）时不再发起新的增量——poke 是「每写一条事件一条」，
            // 并发请求会拿同一游标重复拉取同一区间。窗口按发起时间计算、到期自动放行，
            // 不依赖响应返回，异常时也不会把刷新链永久卡死（正确性由游标推进与去重兜底）。
            if (!full && reqChatFetchAt && Date.now() - reqChatFetchAt < REQ_CHAT_FETCH_COALESCE_MS) return;
            const seq = ++reqChatFetchSeq;
            if (!full) reqChatFetchAt = Date.now();
            try {
                // 游标 reqChatLastId 与事件数组解耦（只在响应落地时前进）：数组被重置/乱序时不会回退重拉
                const after = full ? 0 : reqChatLastId;
                const res = await fetch(`/api/requirements/${reqId}/chat/events?after=${after}`, {cache: 'no-store'});
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                if (selectedId !== reqId || !selectedReqChat) return;
                // 迟到响应（已被更新的请求取代，典型：poke 触发的增量 vs 发送后的主动全量）直接丢弃，
                // 否则旧响应会把已并入的事件再拼一次（重复气泡的根因）
                if (seq !== reqChatFetchSeq) return;
                if (data.chat) reqChatState = data.chat;
                const events = data.events || [];
                // 增量轮询收到本对话建卡系统事件 → 刷新看板列表（新卡即时可见；不 await，不影响轮询；
                // 全量加载历史事件不触发，避免切回会话时反复刷新）
                if (!full && events.some(e => e.kind === 'system' && /^已建卡 #\d+/.test(e.content || ''))) {
                    load();
                }
                // 先渲染状态行（长文本换行会撑高输入区、压缩对话区高度）再渲染事件并滚底，
                // 避免滚底后状态行才撑高、对话区停在距底几十像素处（首屏误悬「回到最新」）
                renderReqChatStatus();
                if (events.length || full) {
                    reqChatEvents = mergeChatStreamEvents(reqChatEvents, events, full);
                    // 游标推进到已并入的最大事件 id（合并结果已按 id 升序）
                    if (reqChatEvents.length) reqChatLastId = reqChatEvents[reqChatEvents.length - 1].id;
                    renderReqChatEvents();
                    // 事件并入后按最新事件流重算一次状态卡：计时与「最后事件 Ns 前」由事件时间派生，
                    // 首次渲染时事件还没并进来；running 态有 1s tick 兜底重绘，非 running 态没有 tick，
                    // 漏这一步就会长期停在占位文案「—（尚无事件时间）」（#430 复核实测）
                    renderReqChatStatus();
                }
                scheduleReqChatPoll(reqId);
            } catch (e) {
                // 增量轮询失败静默等下一轮重试；首次全量加载失败则兜底提示，避免永远停留「正在加载对话记录…」
                if (full && selectedId === reqId && box.isConnected) box.innerHTML = REQ_CHAT_ERROR_HINT;
            } finally {
                // 只清自己这一次的窗口标记（更晚的请求已接手时保持其标记）
                if (!full && seq === reqChatFetchSeq) reqChatFetchAt = 0;
            }
        }

        function renderReqChatEvents() {
            const box = document.getElementById('reqChatEvents');
            if (!box) return;
            // #325 滚动语义（.reqchat-events 已变真滚动容器）：距底 32px 内视为在底部，新消息自动滚底；
            // 用户上翻历史后不抢滚动（保持相对位置，内容增量导致的高度差做位移修正），并显示「回到最新」按钮
            const wasAtBottom = box.scrollHeight > 0 && box.scrollTop + box.clientHeight >= box.scrollHeight - 32;
            const prevTop = box.scrollTop;
            const prevHeight = box.scrollHeight;
            // 与建卡页 chatEventsHtml 同款渲染（.chat-msg 气泡体系）：ACP 流式可能把一条回复拆成
            // 相邻多条 message 事件，先 mergeMessageBlocks 合并再渲染，防 markdown 围栏跨事件错位；
            // 生命周期回执先过滤再 merge（对齐 chatEventsHtml 顺序），过滤后为空留白（#323 删除空态提示）
            const blocks = mergeMessageBlocks(reqChatEvents.filter(e => !isReqChatLifecycleEvent(e)));
            let html = '';
            if (blocks.length) {
                // #325 按轮折叠：user 消息开启新一轮（前导非 user 块归入第一段）；
                // 默认只展开最近 REQ_CHAT_ROUNDS_VISIBLE 轮，更早轮次折叠为一行（数据仍在
                // reqChatEvents，点击展开后重新渲染，消息不丢失）
                const segs = [];
                for (const b of blocks) {
                    if (b.kind === 'user' || !segs.length) segs.push({items: [b]});
                    else segs[segs.length - 1].items.push(b);
                }
                if (!reqChatShowAllRounds && segs.length > REQ_CHAT_ROUNDS_VISIBLE) {
                    const keep = REQ_CHAT_ROUNDS_VISIBLE;
                    const folded = segs.slice(0, segs.length - keep);
                    const n = folded.reduce(
                        (sum, s) => sum + s.items.filter(x => x.kind === 'user' || x.kind === 'message').length, 0);
                    html += `<div class="reqchat-round-fold" onclick="reqChatExpandAllRounds()">更早的 ${n} 条对话已折叠 · 点击展开</div>`;
                    segs.splice(0, segs.length - keep);
                }
                html += segs.map(seg => seg.items.map(ev => {
                    if (ev.kind === 'user') return `<div class="chat-msg user">${escapeHtml(ev.content)}</div>`;
                    if (ev.kind === 'tool') return `<div class="chat-msg tool">⚙ ${escapeHtml(ev.content)}</div>`;
                    if (ev.kind === 'system') return `<div class="chat-msg sys">${escapeHtml(ev.content)}</div>`;
                    return `<div class="chat-msg agent">${renderArtifactMd(ev.content)}</div>`;
                }).join('')).join('');
            }
            box.innerHTML = html;
            if (wasAtBottom || !box.dataset.loaded) {
                box.scrollTop = box.scrollHeight;
                box.dataset.loaded = '1';
            } else {
                box.scrollTop = prevTop + (box.scrollHeight - prevHeight);
            }
            updateReqChatBackBottom();
        }

        // #325 展开全部折叠轮次（持久状态，重建后保持）；折叠条仅隐藏渲染，数据在 reqChatEvents 不丢失
        function reqChatExpandAllRounds() {
            reqChatShowAllRounds = true;
            renderReqChatEvents();
        }

        // #325 「回到最新」按钮显隐：距底不足 32px 隐藏，用户上翻后显示；
        // 同步维护 dataset.atBottom 贴底标记，供 loadReqChat 的 ResizeObserver 在可视高度变化后保持贴底
        function updateReqChatBackBottom() {
            const box = document.getElementById('reqChatEvents');
            const btn = document.getElementById('reqChatBackBottom');
            if (!box || !btn) return;
            const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 32;
            box.dataset.atBottom = atBottom ? '1' : '';
            btn.style.display = atBottom ? 'none' : 'flex';
        }

        function reqChatBackToBottom() {
            const box = document.getElementById('reqChatEvents');
            if (!box) return;
            box.scrollTop = box.scrollHeight;
            updateReqChatBackBottom();
        }

        // #430 需求助手状态模型（纯函数）：与调度器会话共用同一张状态卡片（结构/配色/展开/计时），
        // 但证据面不同——req_chats 没有调度器的心跳与阶段字段（turn_step / last_llm_*），
        // 故不做判死（无红态「疑似卡死」）也不给「重启调度器」入口，只用「会话状态 + 事件流时间」两路证据。
        // 计时基准 = 本轮最后一条 user 事件时间（updated_at 会被建卡/写产物等工具调用刷新，不能当轮次起点）。
        const REQ_CHAT_STATUS_ICO = {created: '💬', running: '⏳', idle: '⏹',
            interrupted: '⏸', failed: '⛔', closed: '⏹'};
        const REQ_CHAT_CARD_LABEL = {created: '新对话（未开始）：发送第一条消息开始',
            idle: '可继续对话（上轮已结束）', running: '思考中（本轮执行中）',
            interrupted: '自动路径已停止，可发消息继续',
            failed: '对话建立失败（发消息即新建对话）', closed: '对话已结束（发消息即新建对话）'};

        // 事件计时只消费服务端 epoch；缺失时不猜测浏览器本地时区。
        function reqChatEventMs(ev) {
            const t = ev && ev.ts_ms;
            return typeof t === 'number' && isFinite(t) ? t : null;
        }

        function reqChatActivityModel(s, events, nowMs) {
            const evs = events || [];
            const lastOf = kind => {
                for (let i = evs.length - 1; i >= 0; i -= 1) {
                    if (!kind || evs[i].kind === kind) {
                        const t = reqChatEventMs(evs[i]);
                        if (t !== null) return t;
                    }
                }
                return null;
            };
            const startMs = lastOf('user');      // 本轮起点：最后一条用户消息
            const lastMs = lastOf(null);         // 事件流最后一条
            const sec = startMs === null ? null : Math.max(0, (nowMs - startMs) / 1000);
            const evSec = lastMs === null ? null : Math.max(0, (nowMs - lastMs) / 1000);
            const fmt = v => v < 60 ? (Math.round(v) + 's')
                : (Math.floor(v / 60) + 'm' + String(Math.round(v % 60)).padStart(2, '0') + 's');
            const evLine = evSec === null ? '—（尚无事件时间）' : ('最后事件 ' + fmt(evSec) + ' 前'
                + (evSec > 60 ? '（已超过 1 分钟没有新事件）' : ''));
            const mk = (kind, color, label, o) => {
                o = o || {};
                const secNum = (o.sec === null || o.sec === undefined) ? null : Math.max(0, o.sec);
                return {kind: kind, color: color, label: label, ico: o.ico || REQ_CHAT_STATUS_ICO[kind] || '💬',
                        timerText: secNum === null ? '' : ('已 ' + fmt(secNum)),
                        // ticking：是否需要 1s 重绘。运行中即使暂时算不出计时也要开——事件拉到后计时才出现，
                        // 靠 tick 的结构变化重建补上（否则静默思考期间「已 Ns」永远不显示）
                        ticking: o.ticking === undefined ? secNum !== null : !!o.ticking,
                        canInput: !!o.canInput, action: o.action || null,
                        open: reqChatStatusManualOpen === null ? !!o.autoOpen : reqChatStatusManualOpen,
                        signals: o.signals || {event: '', model: '', verdict: ''},
                        // 值列严重度：事件流按静默时长；模型层中性；判定按档位（运行中=ok / 失败=bad）
                        signalSev: {event: evSec === null ? '' : (evSec > 60 ? 'warn' : 'ok'),
                                    model: o.sevModel || '', verdict: o.sevVerdict || ''}};
            };
            if (!s || s.status === 'created') {
                return mk('created', 'gray', REQ_CHAT_CARD_LABEL.created, {
                    ico: '💬', canInput: true, signals: {
                        event: evLine, model: '无在途请求（新对话）', verdict: '未开始'}});
            }
            if (s.status === 'running') {
                return mk('running', 'green', REQ_CHAT_CARD_LABEL.running, {
                    ico: '⏳', sec: sec, ticking: true, canInput: false, action: 'cancel',
                    sevVerdict: 'ok', signals: {
                        event: evLine, model: '本轮已发出请求，等待模型响应',
                        verdict: '活着（本轮执行中；需求助手无心跳证据，不判卡死）'}});
            }
            if (s.status === 'idle') {
                return mk('idle', 'gray', REQ_CHAT_CARD_LABEL.idle, {
                    ico: '⏹', canInput: true, signals: {
                        event: evLine, model: '无在途请求（上轮已结束）', verdict: '可继续（保留上下文）'}});
            }
            if (s.status === 'interrupted') {
                return mk('interrupted', 'gray', REQ_CHAT_CARD_LABEL.interrupted, {
                    ico: '⏸', canInput: true, signals: {
                        event: evLine,
                        model: '无在途请求（会话已中断）'
                            + (s.interrupt_source ? ' · 中断来源 ' + s.interrupt_source : ''),
                        verdict: '已停止（可人工续跑）'}});
            }
            if (s.status === 'failed') {
                return mk('failed', 'red', REQ_CHAT_CARD_LABEL.failed + (s.error ? '：' + s.error : ''), {
                    ico: '⛔', canInput: true, autoOpen: true, action: 'newSession', sevVerdict: 'bad', signals: {
                        event: evLine, model: '无在途请求（会话未建立）',
                        verdict: '不可恢复（发消息将新建对话）'}});
            }
            if (s.status === 'closed') {
                return mk('closed', 'gray', REQ_CHAT_CARD_LABEL.closed, {
                    ico: '⏹', canInput: true, action: 'newSession', signals: {
                        event: evLine, model: '无在途请求（对话已结束）',
                        verdict: '已结束（发消息将新建对话）'}});
            }
            // 兜底：未知状态（服务端新增状态未同步前端）中性展示，仍可输入
            return mk(String(s.status), 'gray', String(s.status), {
                ico: '❔', canInput: true, signals: {
                    event: evLine, model: '未知状态（前端未识别）', verdict: String(s.status)}});
        }

        function renderReqChatStatus() {
            const el = document.getElementById('reqChatStatus');
            const input = document.getElementById('reqChatInput');
            const btn = document.getElementById('reqChatSend');
            if (!el || !input || !btn) return;
            const s = reqChatState;
            // #326：头部会话信息行（session id · 模型 · 上下文占用）随状态一起刷新；无会话显示「新对话（未开始）」
            const headMeta = document.getElementById('reqChatHeadMeta');
            if (headMeta) {
                const hp = [];
                if (s && s.session_id) hp.push(String(s.session_id));
                if (s && s.model) hp.push(String(s.model));
                // #383：上下文占用（每轮收尾采集，如「used 35%」）；NULL/空不显示
                if (s && s.context_pct) hp.push('used ' + String(s.context_pct));
                headMeta.textContent = hp.length ? hp.join(' · ') : '新对话（未开始）';
            }
            const m = reqChatActivityModel(s, reqChatEvents, Date.now());
            // 主按钮恒定「发送」（#430：取消本轮/新建对话移入状态卡动作位，与调度器会话同款）；
            // 不可输入时由 .no-input 隐藏输入件，仍同步 disabled 兜住键盘与脚本路径
            btn.textContent = '发送';
            btn.disabled = !m.canInput;
            input.disabled = !m.canInput;
            setStatusNoInput(el, !m.canInput);
            const rid = (s && s.req_id) || selectedId;
            const act = m.action === 'cancel'
                ? `<button class="st-btn" onclick="event.stopPropagation();cancelReqChatRound(${rid})">取消本轮</button>`
                : (m.action === 'newSession'
                    ? `<a href="#" class="chat-new-session" onclick="event.preventDefault();event.stopPropagation();newReqChatSession(${rid})">新建对话</a>`
                    : '');
            applyChatStatusCard(el, m, act);
            ensureChatStatusTicker(!!m.ticking);
        }

        // 1s tick：同档位只改文本节点（计时/事件流时长），档位变化整卡重建
        function reqChatStatusTickOnce() {
            if (agentStatusPointerDown) return;
            const el = document.getElementById('reqChatStatus');
            if (!el) return;
            const m = reqChatActivityModel(reqChatState, reqChatEvents, Date.now());
            tickChatStatusCard(el, m, () => renderReqChatStatus());
        }

        // 需求助手输入框未发送草稿：按 req_id 存 localStorage（切换视图/需求或刷新后回填），发送成功/新建对话时清除
        const REQ_CHAT_DRAFT_KEY = 'reqBoardReqChatDrafts';
        function getReqChatDrafts() {
            try { return JSON.parse(localStorage.getItem(REQ_CHAT_DRAFT_KEY) || '{}') || {}; }
            catch (e) { return {}; }
        }
        function getReqChatDraft(reqId) { return getReqChatDrafts()[String(reqId)] || ''; }
        function saveReqChatDraft(reqId, text) {
            if (!reqId) return;
            const drafts = getReqChatDrafts();
            const key = String(reqId);
            if (text) drafts[key] = text;
            else delete drafts[key];
            try { localStorage.setItem(REQ_CHAT_DRAFT_KEY, JSON.stringify(drafts)); } catch (e) {}
        }

        function bindReqChatInput(reqId) {
            const input = document.getElementById('reqChatInput');
            const btn = document.getElementById('reqChatSend');
            if (!input || !btn) return;
            input.value = getReqChatDraft(reqId);   // 恢复未发送草稿
            // DOM 每次重建后重新绑定保存（程序赋值不触发 input 事件，回填不会误存）
            input.addEventListener('input', () => saveReqChatDraft(reqId, input.value));
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); reqChatAction(reqId); }
            });
            btn.addEventListener('click', () => reqChatAction(reqId));
        }

        // 发送消息驱动一轮（回合制）：无活跃对话时服务端自动新建（closed/failed 发消息即新建对话）；
        // 中断态续跑同样走该接口（服务端按会话状态续上下文），不再有按钮多态与 resume/prompt 双路径
        async function reqChatAction(reqId) {
            const input = document.getElementById('reqChatInput');
            const btn = document.getElementById('reqChatSend');
            if (!input || !btn) return;
            const text = input.value.trim();
            if (!text) return;
            let res;
            try {
                res = await fetch(`/api/requirements/${reqId}/chat/prompt`, {method: 'POST',
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
            } catch (e) { /* 网络异常：保留输入框文字与草稿 */ return; }
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                showToast('发送失败: ' + (err.error || res.status), true);
                return;
            }
            input.value = '';
            saveReqChatDraft(reqId, '');
            await refreshReqChat(reqId, true);
        }

        // 取消本轮（卡片内动作，等价于原先主按钮的「取消」态）：乐观更新，随后轮询/SSE 对账
        async function cancelReqChatRound(reqId) {
            if (!reqId) return;
            try {
                await fetch(`/api/requirements/${reqId}/chat/cancel`, {method: 'POST'});
            } catch (e) {
                showToast('取消失败: 网络异常', true);
                return;
            }
            reqChatState = reqChatState ? {...reqChatState, status: 'interrupted'} : reqChatState;
            renderReqChatStatus();
        }

        // 新建对话（卡片内动作，仅 failed/closed 提供：活跃对话未收尾时服务端会 409，与旧「新建」按钮口径一致）
        async function newReqChatSession(reqId) {
            if (!reqId) return;
            const res = await fetch(`/api/requirements/${reqId}/chat`, {method: 'POST'});
            if (res.ok) {
                saveReqChatDraft(reqId, '');
                await loadReqChat(reqId);
            } else {
                const err = await res.json().catch(() => ({}));
                showToast('新建对话失败: ' + (err.error || res.status), true);
            }
        }

        function scheduleReqChatPoll(reqId) {
            if (reqChatTimer) { clearTimeout(reqChatTimer); reqChatTimer = null; }
            // 已切走（其它需求或离开需求助手视图）不再轮询
            if (selectedId !== reqId || !selectedReqChat) return;
            if (!sseDegraded) return;   // #416：SSE 正常时跳过轮询（降级/关闭时才走）
            const s = reqChatState;
            // 仅 running（本轮执行中）与 created（首轮拉起中）需要轮询；idle 无变化不轮询
            if (!s || (s.status !== 'running' && s.status !== 'created')) return;
            reqChatTimer = setTimeout(() => refreshReqChat(reqId, false), REQ_CHAT_REFRESH_MS);
        }

        // 右栏执行过程视图
        function findSessionRow(i, sid) {
            return sessionList(i).find(r => r.s && r.s.id === sid) || null;
        }
        function processHtml(i, sid) {
            const row = findSessionRow(i, sid);
            const label = row ? row.label : '会话';
            const s = row ? row.s : null;
            // #282 创建时间：按 session_id 从已加载的注册表行反查（spawn 事件的 session_id 与注册表一致）；
            // 查不到（老数据/注册表缺失）则不显示，不报错
            const regRow = (agentSessions || []).find(x => x.session_id === sid && x.req_id === i.id);
            const createdTime = regRow && regRow.created_at ? formatTime(regRow.created_at) : '';
            const parts = [];
            if (s && s.harness) parts.push(escapeHtml(s.harness));
            const mm = modelMetaText(s);
            if (mm) parts.push(escapeHtml(mm));
            if (s && s.context) parts.push(`used ${escapeHtml(s.context)}`);
            if (s && s.branch) parts.push(`分支 ${escapeHtml(s.branch)}`);
            // #422 头部折叠：常驻标题行 + 展开三行（meta/执行目录/会话记录）
            const head = sessHeadHtml({
                key: sessHeadKey({session_id: sid}),
                label: label, sid: sid || null, created: createdTime,
                meta: parts.join(' · '),
                workdir: s && s.workdir,
                sessionDir: row && row.sessionDir,
                alert: modelAlertText(s),   // #13 兜底可见
            });
            return `
            <div class="detail-pane fill">
                ${head}
                <div class="proc-events" id="procEvents"></div>
            </div>`;
        }

        // #273 人工接管：右栏会话详情 = 执行过程/待接管占位 + 状态栏 + 聊天输入框
        // （聊天框发消息默认继续当前会话；#431 状态栏「新建聊天」进入前端草稿态，发送首条消息才落注册行）
        function agentSessionHtml(i, row) {
            const label = roleLabel(row.role);
            const s = {id: row.session_id, model: row.model,
                       harness: row.harness, context: row.context_pct, branch: row.branch, workdir: row.workdir,
                       model_req: row.model_requested, model_req_source: row.model_source,
                       // #13 兜底可见：配置值（回落前）与原因摘要
                       model_requested: row.model_requested,
                       model_configured: row.model_configured, model_reason: row.model_reason};
            // #282 创建时间：注册行直接带 created_at；空会话（无 session_id，显示「新会话（未开始）」）不显示
            const createdTime = row.session_id && row.created_at ? formatTime(row.created_at) : '';
            const parts = [];
            if (s.harness) parts.push(escapeHtml(s.harness));
            const mm2 = modelMetaText(s);
            if (mm2) parts.push(escapeHtml(mm2));
            if (s.context) parts.push(`used ${escapeHtml(s.context)}`);
            if (s.branch) parts.push(`分支 ${escapeHtml(s.branch)}`);
            // #422 头部折叠：空会话（无 sid）渲染为「新会话（未开始）」单行、无折叠按钮与展开区
            const head = sessHeadHtml({
                key: sessHeadKey(row),
                label: label, sid: row.session_id, created: createdTime,
                meta: parts.join(' · '),
                workdir: row.workdir,
                sessionDir: row.session_dir,
                alert: modelAlertText(s),   // #13 兜底可见
            });
            const eventsPart = row.session_id
                ? '<div class="proc-events" id="procEvents"></div>'
                : '<div class="proc-events"><div class="ev-empty">新会话尚未开始：发送第一条消息后，调度器会创建真实会话并执行。</div></div>';
            // #421：状态行常驻（done 卡也要看到「需求已完成，不可继续会话」）——done 时加 no-input
            // 只隐藏输入件，不整块消失；输入区显隐统一由 renderAgentChatStatus 按状态模型 toggle
            const chatPart = `<div class="agent-chat${(row.req_status || i.status) === 'done' ? ' no-input' : ''}">
                    <div class="chat-input-row">
                        <div class="chat-status" id="agentChatStatus"></div>
                        <textarea id="agentChatInput" placeholder="向该会话发送消息，Agent 会继续执行（回车发送，Shift+Enter 换行）" maxlength="4000" onkeydown="if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();sendAgentChat();}"></textarea>
                        <button class="btn btn-primary" id="agentChatSend" onclick="sendAgentChat()">发送</button>
                    </div>
                </div>`;
            return `
            <div class="detail-pane fill">
                ${head}
                ${eventsPart}
                ${chatPart}
            </div>`;
        }

        // #431 草稿态合成注册表行：无 id/session_id（视图与状态行按「新会话（未开始）」渲染）；
        // 展示角色 = 卡上快照当前节点 role，未入流程/terminal 兜底前置槽位 PRE_ROLE
        function agentDraftRow(i) {
            return {id: null, req_id: i.id, role: currentNodeRole(i) || PRE_ROLE || '',
                    session_id: null, model: null, harness: null,
                    context_pct: null, branch: null, workdir: null, poisoned: 0,
                    status: 'created', req_status: i.status, created_at: null, session_dir: null};
        }

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
                        <span class="proc-role">调度器：</span><span class="proc-sid">需求 #${i.id} · ${escapeHtml(i.title)}</span>
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
        // 匹配克制：仅命中末尾提问句——引导词（还有/还要/要不要/是否/要继续等）+ 语气词结尾，
        // 或最后一句以问号/语气词+问号结尾；「把状态改为 open」类引导句不命中（走补充逻辑）。
        const CHAT_CLOSING_ASK_RE = /(?:还有|还要|要不要|是否|要继续|想继续|想再|还有其他|有别的|还有什么|还需)[^。！!？?\n]{0,40}?[吗么吧呢][。！!]?\s*$|[^。！!？?\n]*[？?]\s*$/;
        function applyChatClosing(text) {
            const t = String(text);
            if (t.trim().endsWith(CHAT_CLOSING_LINE)) return t;
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

        // 调度器健康角标：每 10 秒查一次健康接口，按服务端 state 五态渲染
        // #40：状态名由 state 决定（心跳过期 / 网络连接失败 / 显式停用 / 凭据未配置 / 凭据未加载 /
        // 已连接 / 调度器未上报）；age 不再参与状态名判定，pid 异常只作角标后缀说明。
        // #408：角标不再展示「上次退出 SIGTERM@时间（来源: X）/ 自动重启成功」类内部运维记录
        // （该类信息仅保留在心跳文件与启动日志）；有心跳但无 last_shutdown 记录（崩溃/被强杀）
        // 仍提示异常终止；首次部署（无心跳）返回空
        function shutdownText(h) {
            if (!h.last_shutdown || !h.last_shutdown.at) return h.age === null ? '' : ' · 上次退出异常终止（无正常关闭标记）';
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
                // #40：pid 说明只在服务端把它写进 reason 时（有心跳证据可注）才附在角标后面
                const pidTip = (reason.indexOf('心跳 pid 非当前调度器进程') >= 0)
                    ? ' · 心跳 pid 非当前调度器进程' : '';
                let cls, label;
                if (state === 'disabled') {
                    // 显式停用（REQ_BOARD_NO_FEISHU）与凭据文件问题分开，不把凭据未配置显示成已停用
                    cls = 'health warn';
                    label = reason.indexOf('REQ_BOARD_NO_FEISHU') >= 0 ? '显式停用' : '凭据未配置';
                } else if (state === 'stale') {
                    cls = 'health bad';
                    label = '心跳过期';
                } else if (state === 'failed') {
                    cls = 'health bad';
                    label = '网络连接失败';
                } else if (state === 'not_loaded') {
                    cls = 'health warn';
                    label = '凭据未加载';
                } else if (state === 'connected') {
                    // #8 直跑模式：看板有新改动待重启（看板重启后由服务端 BOARD_STARTED_AT 比较自动隐藏）
                    // 该琥珀提示只出现在已连接分支，排在通道故障之后
                    if (h.board_restart_pending) {
                        badge.className = 'health warn';
                        badge.title = pendingTip;
                        text.textContent = '看板有新改动待重启 · 直跑模式请 Ctrl-C 后重跑 run_local.sh';
                        return;
                    }
                    cls = 'health ok';
                    label = '已连接';
                } else {
                    cls = 'health bad';
                    label = '调度器未上报';
                }
                badge.className = cls;
                badge.title = reason || label;   // 复位：防待重启提示残留标题
                text.textContent = label + pidTip + shutdownText(h);
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
