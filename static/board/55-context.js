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
