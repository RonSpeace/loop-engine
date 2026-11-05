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

