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
