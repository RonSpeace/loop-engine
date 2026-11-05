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
