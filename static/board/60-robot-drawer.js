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
        // summary 字段是调度器追加写的运行日志（每条以 [auto] 开头，可多行）。展示时拆成两块：
        //   结果总结：人工前言 + 各节点「完成总结」（按其后紧跟的「<节点> 完成」记录标注来自哪一步）；
        //   运行记录：全部 [auto] 记录，每条只显示首行，默认折叠（无结果总结时展开）。
        // 纯函数，供 node harness 单测。
        function splitSummaryRecords(text) {
            const preface = [], records = [];
            for (const line of String(text || '').split('\n')) {
                if (line.startsWith('[auto] ')) records.push([line.slice(7)]);
                else if (records.length) records[records.length - 1].push(line);
                else preface.push(line);
            }
            const results = [];
            records.forEach((r, k) => {
                if (!r[0].startsWith('完成总结：')) return;
                const next = records.slice(k + 1).find(x => !x[0].startsWith('完成总结：'));
                const m = next && next[0].match(/^(.+?) 完成[，；,]/);
                results.push({stage: m ? m[1] : '', body: [r[0].slice(5)].concat(r.slice(1)).join('\n').trim()});
            });
            const log = records.map(r => {
                const head = r[0];
                const tone = /失败|冲突|未解决|未通过|超时|异常|⚠️/.test(head) ? 'bad'
                    : /人工|用户/.test(head) ? 'human' : '';
                return {text: head.startsWith('完成总结：') ? '提交完成总结' : head, tone};
            });
            return {preface: preface.join('\n').trim(), results, log};
        }
        function summarySectionsHtml(text) {
            if (!text || !String(text).trim()) return '';
            const s = splitSummaryRecords(text);
            let h = '';
            if (s.preface || s.results.length) {
                h += '<div class="detail-section-label">结果总结</div><div class="detail-notes sum-results">';
                if (s.preface) h += renderArtifactMd(s.preface);
                h += s.results.map(r => `<div class="sum-result">${r.stage ? `<div class="sum-stage">${escapeHtml(r.stage)}</div>` : ''}${renderArtifactMd(r.body)}</div>`).join('');
                h += '</div>';
            }
            if (s.log.length) {
                const open = !(s.preface || s.results.length);
                h += `<details class="sum-log"${open ? ' open' : ''}><summary>运行记录（${s.log.length}）</summary><ol>`
                    + s.log.map(x => `<li${x.tone ? ` class="sum-${x.tone}"` : ''} data-i18n-skip>${escapeHtml(x.text)}</li>`).join('')
                    + '</ol></details>';
            }
            return h;
        }
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
            const summary = summarySectionsHtml(i.summary);
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

