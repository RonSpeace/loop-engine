// req-6 模型设置前端回归 harness（由 tests/test_model_settings_ui.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的真实函数
// （escapeHtml / escapeAttr / modelSettingsFormHtml / modelSettingsDelta）。
// 输出 JSON {"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

// escapeHtml 依赖 document.createElement（提取的真实函数，与 project_bar_harness.js 同款桩）
function makeEscDiv() {
    let text = '';
    return {
        set textContent(v) { text = String(v == null ? '' : v); },
        get innerHTML() {
            return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
    };
}
global.document = {createElement: () => makeEscDiv()};

// ---- 夹具：服务端 GET 形态 → 编辑态（msForm）形态的最小构造 ----
function mkCur(base, over) {
    const cur = JSON.parse(JSON.stringify(base));
    (cur.providers || []).forEach(p => {
        p.orig_name = p.name;
        p.isNew = false;
        p.deleted = false;
        p.key_input = '';
        p.clear_key = false;
    });
    Object.assign(cur, over || {});
    return cur;
}

const fixture = {
    path: '/home/u/.kimi-code/config.toml', home: '/home/u/.kimi-code',
    default_model: 'a/m1', default_dangling: false,
    candidates: ['a/m1', 'b/m2'], warnings: [],
    provider_types: ['openai', 'anthropic'],
    providers: [
        {name: 'a', base_url: 'https://a.example/', type: 'openai', key_configured: true,
         model_count: 1, models: [{alias: 'a/m1', model: 'm1', isNew: false, deleted: false}]},
        {name: 'b', base_url: 'https://b.example/', type: 'openai', key_configured: false,
         model_count: 0, models: []},
    ],
    orphans: [{alias: 'x/legacy', model: 'lx', isNew: false, deleted: false}],
};

// ---- U4：渲染矩阵（厂商块 / key 三态 / 别名分组与未归属 / default_model 行 / 按钮禁用态） ----
{
    const html = modelSettingsFormHtml(fixture);
    check('U4a_工具条路径与生效时机', html.includes('当前配置文件：<code>/home/u/.kimi-code/config.toml</code>')
        && html.includes('下一张卡生效') && html.includes('无需重启看板'), html);
    check('U4b_厂商块与字段值', html.includes('data-oidx="0"') && html.includes('data-oidx="1"')
        && html.includes('value="https://a.example/"') && html.includes('<b>厂商：a</b>'), html);
    check('U4c_key输入恒定空与两态placeholder', html.includes('class="ms-key" type="password" value=""')
        && html.includes('已配置（留空=保留）') && html.includes('未配置'), html);
    check('U4d_清除勾选按已配置态禁用', /class="ms-clear"( disabled)?>/.test(html)
        && html.split('class="ms-clear" disabled').length === 2, html);
    check('U4e_别名分组与未归属殿后', html.includes('<code>a/m1</code>')
        && html.includes('（未归属）') && html.includes('x/legacy')
        && html.lastIndexOf('（未归属）') > html.lastIndexOf('厂商：b'), html);
    check('U4f_删除厂商禁用+提示', html.includes('ms-del-prov" data-oidx="0" disabled')
        && html.includes('该厂商下仍有 1 个模型别名'), html);
    check('U4g_default_model独立行与候选', html.includes('id="msDefaultInput"')
        && html.includes('list="msDmList"') && html.includes('value="a/m1"')
        && html.includes('<datalist id="msDmList"><option value="a/m1"></option><option value="b/m2"></option></datalist>'),
        html);
    check('U4h_空数据与无厂商提示不抛', (() => {
        let ok = true, h = '', h2 = '';
        try { h = modelSettingsFormHtml({}); h2 = modelSettingsFormHtml(null); }
        catch (e) { ok = false; }
        return ok && h.includes('尚无厂商') && h2.includes('尚无厂商');
    })(), '');
    // 悬空警示
    const dangling = modelSettingsFormHtml(Object.assign({}, fixture, {default_dangling: true}));
    check('U4i_悬空警示文案', dangling.includes('不在别名池') && dangling.includes('保存将被拒绝'), dangling);
}

// ---- U4b 补充：别名增删入口与逐行转义位置 ----
{
    const html = modelSettingsFormHtml(fixture);
    check('U4j_别名删除按钮与新增行', html.includes('class="btn ms-del-alias" data-oidx="0" data-alias="a/m1"')
        && html.includes('class="btn ms-add-alias-btn" data-oidx="0"') && html.includes('>添加</button>')
        && !html.includes('添加别名'), html);
    check('U4k_新增厂商表单', html.includes('id="msAddProvBtn"') && html.includes('id="msNewName"'), html);
}

// ---- U5：转义矩阵（名称/别名/model/路径注入 <script> 与引号） ----
{
    const evil = {
        path: '/p/<b>boom</b>"', home: '/h',
        default_model: 'd"x<script>', default_dangling: true,
        candidates: ['d"x<script>'], warnings: ['w <script>'],
        providers: [{name: 'p<script>alert(1)</script>', base_url: 'https://x/"><img src=x>',
                     type: 't"', key_configured: true, model_count: 1,
                     models: [{alias: 'a"l<svg>', model: 'm">', isNew: false, deleted: false}]}],
        orphans: [],
    };
    const html = modelSettingsFormHtml(evil);
    check('U5a_文本位无裸标签', !html.includes('<script>alert') && !html.includes('<img src=x>')
        && !html.includes('<svg>'), html);
    check('U5b_属性位引号转义', html.includes('data-alias="a&quot;l&lt;svg&gt;"')
        && html.includes('value="p&lt;script&gt;alert(1)&lt;/script&gt;"')
        && html.includes('value="d&quot;x&lt;script&gt;"'), html);
    check('U5c_候选与警示转义', !html.includes('<option value="d"x<script>')
        && html.includes('&lt;script&gt;'), html);
}

// ---- U6：delta 矩阵 ----
{
    const orig = {
        default_model: 'a/m1',
        providers: [{name: 'a', base_url: 'https://a/', type: 'openai', key_configured: true, model_count: 1},
                    {name: 'b', base_url: 'https://b/', type: 'openai', key_configured: false, model_count: 0}],
    };
    const same = mkCur(orig);
    check('U6a_无改动为空', modelSettingsDelta(orig, same) === null,
        JSON.stringify(modelSettingsDelta(orig, same)));

    const renamed = mkCur(orig); renamed.providers[0].name = 'a2';
    check('U6b_改名', modelSettingsDelta(orig, renamed).providers_update[0].new_name === 'a2'
        && modelSettingsDelta(orig, renamed).providers_update[0].name === 'a',
        JSON.stringify(modelSettingsDelta(orig, renamed)));

    const based = mkCur(orig); based.providers[0].base_url = 'https://new/';
    const d1 = modelSettingsDelta(orig, based);
    check('U6c_字段改', d1.providers_update.length === 1 && d1.providers_update[0].base_url === 'https://new/',
        JSON.stringify(d1));

    const keyed = mkCur(orig); keyed.providers[0].key_input = 'NEW-KEY';
    check('U6d_key替换', modelSettingsDelta(orig, keyed).providers_update[0].key === 'NEW-KEY',
        JSON.stringify(modelSettingsDelta(orig, keyed)));

    const cleared = mkCur(orig); cleared.providers[0].clear_key = true;
    check('U6e_清除key', modelSettingsDelta(orig, cleared).providers_update[0].clear_key === true,
        JSON.stringify(modelSettingsDelta(orig, cleared)));

    const noClear = mkCur(orig); noClear.providers[1].clear_key = true;   // 未配置 → 不生成 op
    check('U6f_未配置不产清除op', modelSettingsDelta(orig, noClear) === null,
        JSON.stringify(modelSettingsDelta(orig, noClear)));

    const emptyKey = mkCur(orig); emptyKey.providers[0].key_input = '';
    check('U6g_空key过滤', modelSettingsDelta(orig, emptyKey) === null, '');

    const addP = mkCur(orig);
    addP.providers.push({name: 'c', base_url: 'https://c/', type: 'openai', key_input: '',
                         orig_name: 'c', isNew: true, deleted: false, clear_key: false, key_configured: false,
                         model_count: 0, models: [{alias: 'c/m9', model: 'm9', isNew: true, deleted: false}]});
    const d2 = modelSettingsDelta(orig, addP);
    check('U6h_新增厂商与别名', d2.providers_add[0].name === 'c' && d2.models_add[0].alias === 'c/m9'
        && d2.models_add[0].provider === 'c' && !('key' in d2.providers_add[0]), JSON.stringify(d2));

    const addA = mkCur(orig);
    addA.providers[0].models = [{alias: 'a/m3', model: 'm3', isNew: true, deleted: false}];
    check('U6i_存量厂商新增别名', modelSettingsDelta(orig, addA).models_add[0].alias === 'a/m3',
        JSON.stringify(modelSettingsDelta(orig, addA)));

    const delA = mkCur(orig);
    delA.providers[0].models = [{alias: 'a/m1', model: 'm1', isNew: false, deleted: true}];
    check('U6j_删除别名', modelSettingsDelta(orig, delA).models_delete[0] === 'a/m1',
        JSON.stringify(modelSettingsDelta(orig, delA)));

    const delP = mkCur(orig); delP.providers[1].deleted = true;
    check('U6k_删除厂商', modelSettingsDelta(orig, delP).providers_delete[0] === 'b',
        JSON.stringify(modelSettingsDelta(orig, delP)));

    const newThenDel = mkCur(orig);
    newThenDel.providers.push({name: 'c', base_url: 'https://c/', type: 't', key_input: '',
                               orig_name: 'c', isNew: true, deleted: true, clear_key: false,
                               key_configured: false, model_count: 0, models: []});
    check('U6l_新增后删除抵消', modelSettingsDelta(orig, newThenDel) === null,
        JSON.stringify(modelSettingsDelta(orig, newThenDel)));

    const dm = mkCur(orig); dm.default_model = 'b/m2';
    check('U6m_default_model改', modelSettingsDelta(orig, dm).default_model === 'b/m2',
        JSON.stringify(modelSettingsDelta(orig, dm)));

    const dmEmpty = mkCur(orig); dmEmpty.default_model = '';
    check('U6n_清空不生成op', modelSettingsDelta(orig, dmEmpty) === null,
        JSON.stringify(modelSettingsDelta(orig, dmEmpty)));

    const dmSame = mkCur(orig); dmSame.default_model = ' a/m1 ';
    check('U6o_空白等价不生成op', modelSettingsDelta(orig, dmSame) === null, '');

    check('U6p_空入参不抛', modelSettingsDelta(null, null) === null
        && modelSettingsDelta(orig, null) === null, '');
}

// ---- U6q：孤儿删除 ----
{
    const orig = {default_model: '', providers: []};
    const cur = mkCur(orig);
    cur.orphans = [{alias: 'x/o', model: 'mx', isNew: false, deleted: true}];
    check('U6q_孤儿删除', modelSettingsDelta(orig, cur).models_delete[0] === 'x/o',
        JSON.stringify(modelSettingsDelta(orig, cur)));
}

// ---- U4l/U4m（评审必改 1）：key 回填链路——重渲染不回丢用户已输入的 key ----
{
    const st = {
        default_model: '', candidates: [], default_dangling: false, warnings: [],
        providers: [{name: 'c', base_url: 'https://c/', type: 't', key_configured: false,
                     model_count: 0, orig_name: 'c', isNew: true, deleted: false,
                     key_input: 'K1', clear_key: false, models: []}],
        orphans: [],
    };
    const html = modelSettingsFormHtml(st);
    const m = html.match(/class="ms-key" type="password" value="([^"]*)"/);
    check('U4l_重渲染回填已输入key', !!m && m[1] === 'K1', html.slice(0, 300));
    // 以真实渲染回填值构造 DOM stub → sync 不回丢 → delta 携带 key（修复后的完整链路）
    const backfilled = m ? m[1] : 'NONE';
    const bodyStub = {
        querySelector(sel) {
            if (sel === '#msDefaultInput') return {value: ''};
            if (sel === '.ms-prov[data-oidx="0"]') {
                return {querySelector(s) {
                    if (s === '.ms-name') return {value: 'c'};
                    if (s === '.ms-base') return {value: 'https://c/'};
                    if (s === '.ms-type') return {value: 't'};
                    if (s === '.ms-key') return {value: backfilled};
                    if (s === '.ms-clear') return {checked: false};
                    return null;
                }};
            }
            return null;
        }
    };
    readModelSettingsFormState(bodyStub, st);
    const d = modelSettingsDelta({default_model: '', providers: []}, st);
    check('U4m_回填后保存携带key', st.providers[0].key_input === 'K1'
        && !!d && Array.isArray(d.providers_add) && d.providers_add[0].key === 'K1',
        JSON.stringify(d));
    // 初始（未输入）恒空：不存在回显服务端原值的路径（服务端从不下发 key）
    const empty = modelSettingsFormHtml({providers: [{name: 'a', base_url: 'https://a/', type: 't',
        key_configured: true, model_count: 0, isNew: false, deleted: false, key_input: '',
        clear_key: false, models: []}], orphans: []});
    check('U4n_未输入时key输入仍为空', empty.includes('class="ms-key" type="password" value=""'), empty.slice(0, 300));
}

// ---- U 新增 1（#13）：别名 protocol 列渲染（有值/空值两态）+ 表头 + 新增行位置 ----
{
    const f = {
        path: '/p/config.toml', default_model: '', default_dangling: false,
        candidates: [], warnings: [], provider_types: ['openai', 'anthropic'],
        providers: [{name: 'a', base_url: 'https://a/', type: 'openai', key_configured: true, model_count: 2,
                     models: [{alias: 'a/m1', model: 'm1', protocol: 'openai', isNew: false, deleted: false},
                              {alias: 'a/m2', model: 'm2', protocol: '', isNew: false, deleted: false}]}],
        orphans: [],
    };
    const html = modelSettingsFormHtml(f);
    check('U13a_别名protocol列有值', html.includes('class="ms-alias-protocol" data-oidx="0" data-alias="a/m1" value="openai"'), html.slice(0, 600));
    check('U13b_别名protocol列空值', html.includes('class="ms-alias-protocol" data-oidx="0" data-alias="a/m2" value=""')
        && html.includes('placeholder="protocol（可选）"'), html.slice(0, 600));
    check('U13c_protocol表头与列序', html.includes('<th>模型别名</th><th>上游 model</th>'
        + '<th>max_context_size</th><th>protocol</th><th>操作</th>'), html.slice(0, 600));
    const iModel = html.indexOf('class="ms-new-model"');
    const iProto = html.indexOf('class="ms-new-protocol"');
    check('U13d_新增行protocol在model后按钮前', iModel >= 0 && iProto > iModel
        && iProto < html.indexOf('class="btn ms-add-alias-btn"'), html.slice(0, 600));
    // 空表占位 colspan=4
    const emptyTbl = modelSettingsFormHtml({
        providers: [{name: 'z', base_url: '', type: '', key_configured: false, model_count: 0,
                     isNew: false, deleted: false, models: []}], orphans: []});
    check('U13e_空表占位colspan5', emptyTbl.includes('<td colspan="5">（无别名）</td>')
        && emptyTbl.includes('<th>protocol</th>') && emptyTbl.includes('<th>max_context_size</th>'),
        emptyTbl.slice(0, 600));
}

// ---- U 新增 1（#13）：转义矩阵（alias/protocol 含 < " &） ----
{
    const evil = {
        path: '/p', default_model: '', default_dangling: false,
        candidates: [], warnings: [], provider_types: ['<b>&"'],
        providers: [{name: 'a', base_url: 'https://a/', type: 't', key_configured: false, model_count: 1,
                     models: [{alias: 'x"<svg>&', model: 'm', protocol: 'p"<b>&', isNew: false, deleted: false}]}],
        orphans: [],
    };
    const html = modelSettingsFormHtml(evil);
    check('U13f_别名protocol属性位转义', html.includes('data-alias="x&quot;&lt;svg&gt;&amp;"')
        && html.includes('value="p&quot;&lt;b&gt;&amp;"')
        && !html.includes('value="p"<b>&"'), html);
    check('U13g_datalist选项转义', html.includes('<option value="&lt;b&gt;&amp;&quot;"></option>')
        && !html.includes('<option value="<b>&"'), html.slice(0, 600));
    check('U13h_文本位无裸标签', !html.includes('<svg>') && !html.includes('<b>&'), html);
}

// ---- U 新增 2（#13）：provider type datalist 单一源（注入值渲染，无硬编码） ----
{
    const f = {
        path: '/p', default_model: '', default_dangling: false, candidates: [], warnings: [],
        provider_types: ['openai', 'anthropic'],
        providers: [{name: 'a', base_url: 'https://a/', type: 'openai', key_configured: false, model_count: 0,
                     models: [], isNew: false, deleted: false}],
        orphans: [],
    };
    const html = modelSettingsFormHtml(f);
    check('U13i_datalist选项等于provider_types',
        html.includes('<datalist id="msProviderTypes"><option value="openai"></option><option value="anthropic"></option></datalist>'), html.slice(0, 800));
    check('U13j_厂商type输入挂list', html.includes('class="ms-type" list="msProviderTypes"')
        && html.includes('id="msNewType" list="msProviderTypes"'), html.slice(0, 800));
    check('U13k_提示文案由注入值渲染', html.includes('合法值：openai / anthropic'), html.slice(0, 800));
    // provider_types 为空 → 占位提示
    const empty = modelSettingsFormHtml({providers: [], orphans: []});
    check('U13l_空清单占位提示', empty.includes('合法值：（未取到合法值清单）')
        && empty.includes('<datalist id="msProviderTypes"></datalist>'), empty.slice(0, 800));
}

// ---- U 新增 3（#13）：readModelSettingsFormState 读取 protocol（按 data-alias 对位，存量 + 新增别名行） ----
{
    const st = {
        default_model: '', providers: [{name: 'a', base_url: 'https://a/', type: 'openai', key_configured: true,
                     model_count: 3, orig_name: 'a', isNew: false, deleted: false, key_input: '', clear_key: false,
                     models: [{alias: 'a/m1', model: 'm1', protocol: '', isNew: false, deleted: false},
                              {alias: 'a/m2', model: 'm2', protocol: 'openai', isNew: false, deleted: false},
                              {alias: 'a/m3', model: 'm3', protocol: '', isNew: true, deleted: false}]}],
        orphans: [],
    };
    // 模拟 DOM：按 data-alias 对位（顺序与 models 索引错开，验证不按索引）
    const protoEls = [
        {getAttribute() { return 'a/m2'; }, value: ''},          // 存量：清空
        {getAttribute() { return 'a/m1'; }, value: 'anthropic'}, // 存量：填值
        {getAttribute() { return 'a/m3'; }, value: 'openai'},    // 新增别名行
    ];
    const block = {
        querySelector(s) {
            if (s === '.ms-name') return {value: 'a'};
            if (s === '.ms-base') return {value: 'https://a/'};
            if (s === '.ms-type') return {value: 'openai'};
            if (s === '.ms-key') return {value: ''};
            if (s === '.ms-clear') return {checked: false};
            return null;
        },
        querySelectorAll(s) { return s === '.ms-alias-protocol' ? protoEls : []; },
    };
    const bodyStub = {querySelector(sel) {
        if (sel === '#msDefaultInput') return {value: ''};
        if (sel === '.ms-prov[data-oidx="0"]') return block;
        return null;
    }};
    readModelSettingsFormState(bodyStub, st);
    const m = st.providers[0].models;
    check('U13m_read按data-alias对位回写protocol', m[0].protocol === 'anthropic' && m[1].protocol === '' && m[2].protocol === 'openai',
        JSON.stringify(m));
}

// ---- U 新增 4（#13）：modelSettingsDelta protocol 矩阵 ----
{
    const orig = {
        default_model: 'a/m1',
        providers: [{name: 'a', base_url: 'https://a/', type: 'openai', key_configured: true, model_count: 2,
                     models: [{alias: 'a/m1', model: 'm1', protocol: 'openai'},
                              {alias: 'a/m2', model: 'm2', protocol: ''}]}],
        orphans: [],
    };
    check('U13n_protocol无变化为空', modelSettingsDelta(orig, mkCur(orig)) === null,
        JSON.stringify(modelSettingsDelta(orig, mkCur(orig))));

    const addProto = mkCur(orig);
    addProto.providers[0].models.push({alias: 'a/m3', model: 'm3', protocol: 'anthropic', isNew: true, deleted: false});
    const d1 = modelSettingsDelta(orig, addProto);
    check('U13o_新别名带protocol', d1.models_add[0].alias === 'a/m3' && d1.models_add[0].protocol === 'anthropic'
        && !d1.models_update, JSON.stringify(d1));

    const addNoProto = mkCur(orig);
    addNoProto.providers[0].models.push({alias: 'a/m4', model: 'm4', protocol: '', isNew: true, deleted: false});
    const d2 = modelSettingsDelta(orig, addNoProto);
    check('U13p_新别名无protocol不带键', !('protocol' in d2.models_add[0]), JSON.stringify(d2));

    const changed = mkCur(orig);
    changed.providers[0].models[0].protocol = 'anthropic';
    const d3 = modelSettingsDelta(orig, changed);
    check('U13q_存量值变更models_update', d3.models_update.length === 1 && d3.models_update[0].alias === 'a/m1'
        && d3.models_update[0].protocol === 'anthropic', JSON.stringify(d3));

    const filled = mkCur(orig);
    filled.providers[0].models[1].protocol = 'openai';   // 空 → 非空
    const d4 = modelSettingsDelta(orig, filled);
    check('U13r_空变非空也是update', d4.models_update.length === 1 && d4.models_update[0].alias === 'a/m2'
        && d4.models_update[0].protocol === 'openai', JSON.stringify(d4));

    const cleared = mkCur(orig);
    cleared.providers[0].models[0].protocol = '';        // 非空 → 空
    const d5 = modelSettingsDelta(orig, cleared);
    check('U13s_清空models_update_clear_protocol', d5.models_update.length === 1
        && d5.models_update[0].clear_protocol === true && !('protocol' in d5.models_update[0]), JSON.stringify(d5));

    // 同一别名既在 models_update 又在 models_delete → 只出 models_delete
    const both = mkCur(orig);
    both.providers[0].models[0].protocol = 'anthropic';  // a/m1 → updM
    both.providers[0].models.push({alias: 'a/m1', model: 'dup', protocol: 'openai', isNew: false, deleted: true}); // a/m1 → delM
    const d6 = modelSettingsDelta(orig, both);
    check('U13t_同别名update又delete只留delete', d6.models_delete.includes('a/m1')
        && !(d6.models_update || []).some(it => it.alias === 'a/m1'), JSON.stringify(d6));
}

// ---- U 新增 5（#18）：max_context_size 列渲染（有值/空值/行内原因）+ 属性位转义 + 新增行位置 ----
{
    const f = {
        path: '/p', default_model: '', default_dangling: false, candidates: [], warnings: [],
        provider_types: ['openai'], context_defaults: [{type: '', match: 'claude', value: 200000}],
        providers: [{name: 'a', base_url: 'https://a/', type: 'openai', key_configured: false, model_count: 2,
                     models: [{alias: 'a/m1', model: 'm1', protocol: '', max_context_size: 128000,
                               incomplete_reason: '', isNew: false, deleted: false},
                              {alias: 'a/m2', model: 'm2', protocol: '', max_context_size: '',
                               incomplete_reason: '缺少必填字段 max_context_size',
                               isNew: false, deleted: false}]}],
        orphans: [],
    };
    const html = modelSettingsFormHtml(f);
    check('U14a_ctx列有值', html.includes('class="ms-alias-ctx" data-oidx="0" data-alias="a/m1" value="128000"'),
        html.slice(0, 900));
    check('U14b_ctx列空值含必填占位', html.includes('class="ms-alias-ctx" data-oidx="0" data-alias="a/m2" value=""')
        && html.includes('placeholder="max_context_size（必填）"'), html.slice(0, 900));
    check('U14c_行内不完整原因', html.includes('class="ms-field-hint ms-ctx-warn">缺少必填字段 max_context_size</div>'),
        html.slice(0, 900));
    check('U14d_新增行ctx在model后protocol前', (() => {
        const iM = html.indexOf('class="ms-new-model"');
        const iC = html.indexOf('class="ms-new-ctx"');
        const iP = html.indexOf('class="ms-new-protocol"');
        return iC > iM && iC < iP;
    })(), html.slice(0, 900));
    // 属性位/原因文本位转义（值含 < " &）
    const evil = {
        path: '/p', default_model: '', default_dangling: false, candidates: [], warnings: [],
        provider_types: [], context_defaults: [],
        providers: [{name: 'a', base_url: '', type: '', key_configured: false, model_count: 1,
                     models: [{alias: 'a/m1', model: 'm', protocol: '', max_context_size: '1"<b>&',
                               incomplete_reason: 'r"<i>', isNew: false, deleted: false}]}],
        orphans: [],
    };
    const evilHtml = modelSettingsFormHtml(evil);
    check('U14e_ctx属性位与原因文本位转义', evilHtml.includes('value="1&quot;&lt;b&gt;&amp;"')
        && evilHtml.includes('r"&lt;i&gt;') && !evilHtml.includes('1"<b>&') && !evilHtml.includes('<i>'),
        evilHtml.slice(0, 900));
}

// ---- U 新增 6（#18）：readModelSettingsFormState 读取 max_context_size（按 data-alias 对位） ----
{
    const st = {
        default_model: '', providers: [{name: 'a', base_url: 'https://a/', type: 'openai', key_configured: true,
                     model_count: 3, orig_name: 'a', isNew: false, deleted: false, key_input: '', clear_key: false,
                     models: [{alias: 'a/m1', model: 'm1', protocol: '', max_context_size: '128000',
                               isNew: false, deleted: false},
                              {alias: 'a/m2', model: 'm2', protocol: '', max_context_size: '',
                               isNew: false, deleted: false},
                              {alias: 'a/m3', model: 'm3', protocol: '', max_context_size: '',
                               isNew: true, deleted: false}]}],
        orphans: [],
    };
    const ctxEls = [
        {getAttribute() { return 'a/m2'; }, value: '64000'},     // 存量：补值
        {getAttribute() { return 'a/m1'; }, value: ''},          // 存量：清空
        {getAttribute() { return 'a/m3'; }, value: '262144'},    // 新增别名行
    ];
    const block = {
        querySelector(s) {
            if (s === '.ms-name') return {value: 'a'};
            if (s === '.ms-base') return {value: 'https://a/'};
            if (s === '.ms-type') return {value: 'openai'};
            if (s === '.ms-key') return {value: ''};
            if (s === '.ms-clear') return {checked: false};
            return null;
        },
        querySelectorAll(s) { return s === '.ms-alias-ctx' ? ctxEls : []; },
    };
    const bodyStub = {querySelector(sel) {
        if (sel === '#msDefaultInput') return {value: ''};
        if (sel === '.ms-prov[data-oidx="0"]') return block;
        return null;
    }};
    readModelSettingsFormState(bodyStub, st);
    const m = st.providers[0].models;
    check('U14f_read按data-alias回写ctx', m[0].max_context_size === '' && m[1].max_context_size === '64000'
        && m[2].max_context_size === '262144', JSON.stringify(m));
}

// ---- U 新增 7（#18）：modelSettingsDelta max_context_size 矩阵 ----
{
    const orig = {
        default_model: 'a/m1',
        providers: [{name: 'a', base_url: 'https://a/', type: 'openai', key_configured: true, model_count: 2,
                     models: [{alias: 'a/m1', model: 'm1', protocol: '', max_context_size: 128000},
                              {alias: 'a/m2', model: 'm2', protocol: '', max_context_size: ''}]}],
        orphans: [],
    };
    check('U14g_ctx无变化为空', modelSettingsDelta(orig, mkCur(orig)) === null,
        JSON.stringify(modelSettingsDelta(orig, mkCur(orig))));

    const addCtx = mkCur(orig);
    addCtx.providers[0].models.push({alias: 'a/m3', model: 'm3', protocol: '',
                                     max_context_size: '262144', isNew: true, deleted: false});
    const d1 = modelSettingsDelta(orig, addCtx);
    check('U14h_新别名带整数ctx', d1.models_add[0].alias === 'a/m3'
        && d1.models_add[0].max_context_size === 262144
        && typeof d1.models_add[0].max_context_size === 'number', JSON.stringify(d1));

    const changedCtx = mkCur(orig);
    changedCtx.providers[0].models[0].max_context_size = '200000';
    const d2 = modelSettingsDelta(orig, changedCtx);
    check('U14i_存量ctx变更models_update整数', d2.models_update.length === 1
        && d2.models_update[0].alias === 'a/m1' && d2.models_update[0].max_context_size === 200000,
        JSON.stringify(d2));

    const filledCtx = mkCur(orig);
    filledCtx.providers[0].models[1].max_context_size = '64000';
    const d3 = modelSettingsDelta(orig, filledCtx);
    check('U14j_空变有值也是update', d3.models_update.length === 1
        && d3.models_update[0].max_context_size === 64000, JSON.stringify(d3));

    const sameCtx = mkCur(orig);
    sameCtx.providers[0].models[0].max_context_size = ' 128000 ';   // 空白等价 → 不生成 op
    check('U14k_同值不生成op', modelSettingsDelta(orig, sameCtx) === null,
        JSON.stringify(modelSettingsDelta(orig, sameCtx)));

    const badCtx = mkCur(orig);
    badCtx.providers[0].models[0].max_context_size = 'abc';          // 非法 → 不生成该字段
    check('U14l_非法ctx不生成字段', modelSettingsDelta(orig, badCtx) === null,
        JSON.stringify(modelSettingsDelta(orig, badCtx)));

    const both = mkCur(orig);
    both.providers[0].models[0].protocol = 'anthropic';
    both.providers[0].models[0].max_context_size = '200000';
    const d4 = modelSettingsDelta(orig, both);
    check('U14m_protocol与ctx合并一条update', d4.models_update.length === 1
        && d4.models_update[0].protocol === 'anthropic'
        && d4.models_update[0].max_context_size === 200000, JSON.stringify(d4));

    const addBad = mkCur(orig);
    addBad.providers[0].models.push({alias: 'a/m4', model: 'm4', protocol: '',
                                     max_context_size: 'x', isNew: true, deleted: false});
    const d5 = modelSettingsDelta(orig, addBad);
    check('U14n_新别名非法ctx不带键', !('max_context_size' in d5.models_add[0]), JSON.stringify(d5));
}

// ---- U 新增 8（#18）：预填纯函数 msDefaultContextSize / msParseContextSize 矩阵 ----
{
    const defs = [
        {type: 'kimi', match: 'k3', value: 1048576},
        {type: 'kimi', match: 'kimi-for-coding', value: 262144},
        {type: '', match: 'claude', value: 200000},
        {type: '', match: 'gpt-4.1', value: 1047576},
    ];
    check('U14o_预填kimi_k3', msDefaultContextSize('kimi', 'k3', defs) === 1048576, '');
    check('U14p_预填kimi_for_coding', msDefaultContextSize('kimi', 'kimi-for-coding', defs) === 262144, '');
    check('U14q_预填claude通配型', msDefaultContextSize('anthropic', 'claude-sonnet-4', defs) === 200000, '');
    check('U14r_预填gpt41', msDefaultContextSize('openai', 'gpt-4.1-mini', defs) === 1047576, '');
    check('U14s_type不匹配不预填', msDefaultContextSize('openai', 'k3', defs) === '', '');
    check('U14t_未命中留空不猜', msDefaultContextSize('anthropic', 'mystery', defs) === ''
        && msDefaultContextSize('', '', defs) === '' && msDefaultContextSize(null, null, defs) === '', '');
    check('U14u_大小写不敏感', msDefaultContextSize('KIMI', 'K3-Mini', defs) === 1048576, '');
    check('U14v_非法默认值不用', msDefaultContextSize('kimi', 'k3', [{type: '', match: 'k3', value: 'x'}]) === ''
        && msDefaultContextSize('kimi', 'k3', null) === '', '');
    check('U14w_整数解析矩阵', msParseContextSize('128000') === 128000 && msParseContextSize(' 42 ') === 42
        && msParseContextSize('0') === null && msParseContextSize('') === null
        && msParseContextSize('1.5') === null && msParseContextSize('-1') === null
        && msParseContextSize('1e5') === null && msParseContextSize('abc') === null
        && msParseContextSize(null) === null && msParseContextSize(undefined) === null, '');
}

console.log(JSON.stringify({checks: results}));
// 不调用 process.exit()：大输出（detail 含完整 HTML）时 pipe 写入可能被截断在 64KB，
// 用 exitCode 让 node 自然退出并 flush stdout。
process.exitCode = results.every(r => r.pass) ? 0 : 1;
