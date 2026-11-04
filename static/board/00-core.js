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

