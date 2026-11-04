// #12 建卡对话参考文件（前端）回归 harness —— 由 tests/test_chat_material_upload.py 拼装并调用：
// 文件开头会被 Python 前置注入从 board.py 提取的真实函数源码
// （function escapeHtml / chatMaterialInputEnabled / chatMaterialNameError / chatMaterialAddFiles /
//  uploadChatMaterials / removeChatMaterial / renderChatAttachBar / bindChatMaterialInputs），
// 本文件随后定义 DOM / fetch 桩，并把 drop / paste / change / click 事件**真的派发**给
// bindChatMaterialInputs 绑定的监听器，断言到「发起上传 / chip 出现 / 高亮类落点」这一层
// （不再直调 chatMaterialAddFiles、不再替换 uploadChatMaterials）。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

// ---- 依赖桩 ----
let toasts = [];
function showToast(msg, isError) { toasts.push([String(msg), !!isError]); }

// escapeHtml 依赖 document.createElement（提取的真实函数，与 chat_status_card_harness.js 同款桩）
function makeEscDiv() {
    let text = '';
    return {
        set textContent(v) { text = String(v == null ? '' : v); },
        get innerHTML() {
            return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
    };
}

function makeEl(id) {
    const handlers = {};
    const cls = new Set();
    return {
        id, disabled: false, innerHTML: '', value: '', clicked: 0, files: null,
        classList: {
            add: (c) => cls.add(c),
            remove: (c) => cls.delete(c),
            contains: (c) => cls.has(c),
            toggle: (c, force) => {
                const on = force === undefined ? !cls.has(c) : !!force;
                if (on) cls.add(c); else cls.delete(c);
                return on;
            },
            _all: () => Array.from(cls),
        },
        _handlers: handlers,
        addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
        dispatch(t, ev) { (handlers[t] || []).forEach((fn) => fn(ev || {})); },
        hasListener(t) { return !!(handlers[t] && handlers[t].length); },
        click() { this.clicked += 1; },
        closest() { return null; },
    };
}

const chipsEl = makeEl('chatAttachChips');
const fileInputEl = makeEl('chatFileInput');
const chatInputEl = makeEl('chatInput');
const barEl = makeEl('chatAttachBar');
const inputRowEl = makeEl('chatInputRow');
inputRowEl.closest = (sel) => (sel === '.chat-input-row' ? inputRowEl : null);
chatInputEl.closest = (sel) => (sel === '.chat-input-row' ? inputRowEl : null);

globalThis.document = {
    createElement: () => makeEscDiv(),
    getElementById: (id) => ({chatAttachChips: chipsEl, chatFileInput: fileInputEl,
                              chatInput: chatInputEl, chatAttachBar: barEl}[id] || null),
};

// fetch 桩：记录 (url, body)，可切换失败态
let fetchCalls = [];
let fetchOk = true;
globalThis.fetch = async (url, opts) => {
    fetchCalls.push({url: String(url), opts: opts || {}});
    return {
        ok: fetchOk, status: fetchOk ? 200 : 500,
        json: async () => (fetchOk
            ? {files: [{filename: 'stub.html', path: '/m/stub.html', size: 1, over_read_limit: false}]}
            : {error: '服务器错误'}),
    };
};

// 与 board.py 同值的常量（Python 侧另有源码单一源断言）
const CHAT_MATERIAL_MAX_FILES = 5;
const CHAT_MATERIAL_MAX_BYTES = 10 * 1024 * 1024;
const CHAT_MATERIAL_READ_CAP = 1024 * 1024;
const CHAT_MATERIAL_EXTS = ['.html', '.htm'];
// board.py 里是 let 声明
let chatState = {id: 7, status: 'idle'};
let pendingChatMaterials = [];

function reset(pending) {
    pendingChatMaterials = (pending || []).slice();
    toasts = [];
    fetchCalls = [];
    fetchOk = true;
    chipsEl.innerHTML = '';
    fileInputEl.value = '';
    fileInputEl.files = null;
    chatInputEl.disabled = false;
    chatState = {id: 7, status: 'idle'};
    barEl.classList.remove('drag-over');
    inputRowEl.classList.remove('drag-over');
}
const f = (name, size) => ({name: name, size: size == null ? 10 : size});
const settle = () => new Promise((r) => setTimeout(r, 0));
function filesUploaded() {
    return fetchCalls.filter((c) => /\/materials$/.test(c.url)).length;
}
function uploadedCounts() {
    return fetchCalls.filter((c) => /\/materials$/.test(c.url))
        .map((c) => (c.opts.body && c.opts.body.getAll ? c.opts.body.getAll('file').length : -1));
}

// 绑定真实监听器（真实 bindChatMaterialInputs）
const boundFileInput = bindChatMaterialInputs(chatInputEl, barEl);

// ---- 1. 文件名合法性矩阵（与服务端 _material_name_ok 同规则） ----
(function nameMatrix() {
    const ok = ['a.html', 'A.HTM', '原型.html', 'my proto(v2).html', '中文 名字.htm'];
    const bad = ['', '   ', '../x.html', '../../x.html', 'a/b.html', 'a\\b.html', '..', 'a..html',
                 'a.txt', 'a.png', 'a.html.bak', 'a', 'a\nb.html', 'x'.repeat(200) + '.html'];
    const badMissed = bad.filter((n) => chatMaterialNameError(n) === null);
    const okFailed = ok.filter((n) => chatMaterialNameError(n) !== null);
    check('name-matrix', badMissed.length === 0 && okFailed.length === 0,
        'bad 未拒: ' + JSON.stringify(badMissed) + ' ok 误拒: ' + JSON.stringify(okFailed));
})();

// ---- 2. 监听层已挂载（drop/paste/change/click） ----
(function listenersBound() {
    check('listeners-bound',
        inputRowEl.hasListener('dragover') && inputRowEl.hasListener('drop') &&
        inputRowEl.hasListener('dragleave') && chatInputEl.hasListener('paste') &&
        barEl.hasListener('change') && barEl.hasListener('click'),
        JSON.stringify({row: Object.keys(inputRowEl._handlers), input: Object.keys(chatInputEl._handlers),
                        bar: Object.keys(barEl._handlers)}));
})();

(async function main() {
    // ---- 3. 拖拽入口：真实 drop 事件 → 高亮落点 + 上传 fetch + chip 出现 ----
    reset([]);
    inputRowEl.dispatch('dragover', {dataTransfer: {files: [f('drop.html')]}, preventDefault() {}});
    check('drag-highlight-on-attach-bar',
        barEl.classList.contains('drag-over') && !inputRowEl.classList.contains('drag-over'),
        'bar=' + JSON.stringify(barEl.classList._all()) + ' row=' + JSON.stringify(inputRowEl.classList._all()));
    check('drag-highlight-matches-css-selector', !!document.getElementById('chatAttachBar') &&
        document.getElementById('chatAttachBar').classList.contains('drag-over'), 'CSS `.chat-attach-bar.drag-over` 的选择器须命中该元素');
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('drop.html')]}, preventDefault() {}});
    check('drop-clears-highlight', !barEl.classList.contains('drag-over'), JSON.stringify(barEl.classList._all()));
    check('drop-triggers-upload', filesUploaded() === 1, JSON.stringify(fetchCalls.map((c) => c.url)));
    check('drop-formdata-count', JSON.stringify(uploadedCounts()) === '[1]', JSON.stringify(uploadedCounts()));
    await settle(); await settle();
    check('drop-chip-rendered', pendingChatMaterials.length === 1 && chipsEl.innerHTML.indexOf('stub.html') > 0,
        'pending=' + pendingChatMaterials.length + ' html=' + chipsEl.innerHTML);

    // ---- 4. 粘贴入口：真实 paste 事件 → 上传 fetch ----
    reset([]);
    chatInputEl.dispatch('paste', {clipboardData: {files: [f('paste.html')]}, preventDefault() {}});
    check('paste-triggers-upload', filesUploaded() === 1, JSON.stringify(fetchCalls.map((c) => c.url)));
    await settle(); await settle();
    check('paste-chip-rendered', pendingChatMaterials.length === 1, 'pending=' + pendingChatMaterials.length);

    // ---- 5. 点选入口：change 事件（文件选择）→ 上传 fetch，且 value 清空允许重选 ----
    reset([]);
    fileInputEl.files = [f('pick.html')];
    fileInputEl.value = 'C:\\fakepath\\pick.html';
    barEl.dispatch('change', {target: fileInputEl});
    check('change-triggers-upload', filesUploaded() === 1, JSON.stringify(fetchCalls.map((c) => c.url)));
    check('change-resets-value', fileInputEl.value === '', 'value=' + fileInputEl.value);
    await settle(); await settle();
    check('change-chip-rendered', pendingChatMaterials.length === 1, 'pending=' + pendingChatMaterials.length);

    // ---- 6. 📎 按钮 click → 隐藏 file input.click() ----
    reset([]);
    fileInputEl.clicked = 0;
    barEl.dispatch('click', {target: {closest: (sel) => (sel === '#chatAttachBtn' ? {} : null)}});
    check('attach-btn-opens-file-input', fileInputEl.clicked === 1, 'clicked=' + fileInputEl.clicked);
    check('attach-btn-no-upload', filesUploaded() === 0, JSON.stringify(fetchCalls));

    // ---- 7. chip 的 × 委托移除（真实 click 事件 → removeChatMaterial） ----
    reset([{filename: 'keep.html', path: '/m/keep.html'}, {filename: 'drop.html', path: '/m/drop.html'}]);
    renderChatAttachBar();
    check('chip-escaped-and-has-remove',
        chipsEl.innerHTML.indexOf('data-name="drop.html"') > 0 && chipsEl.innerHTML.indexOf('class="rm"') > 0,
        chipsEl.innerHTML);
    barEl.dispatch('click', {preventDefault() {}, target: {closest: (sel) => (sel === '.chat-attach-chip .rm' ? {dataset: {name: 'drop.html'}} : null)}});
    check('remove-only-target', pendingChatMaterials.length === 1 && pendingChatMaterials[0].filename === 'keep.html',
        JSON.stringify(pendingChatMaterials));
    // 转义：含 < > " 的文件名渲染后不得出现裸标签；**属性位必须用 escapeAttr 转双引号**
    reset([{filename: '<img src=x onerror=1>.html', path: '/m/x.html'}]);
    renderChatAttachBar();
    check('chip-name-escaped', chipsEl.innerHTML.indexOf('&lt;img') > 0 && chipsEl.innerHTML.indexOf('<img') < 0,
        chipsEl.innerHTML);
    reset([{filename: 'x" onmouseover="alert(1)" a=".html', path: '/m/q"uote.html'}]);
    renderChatAttachBar();
    check('chip-attr-quotes-escaped',
        chipsEl.innerHTML.indexOf('data-name="x&quot; onmouseover=&quot;alert(1)&quot; a=&quot;.html"') > 0 &&
        chipsEl.innerHTML.indexOf('data-name="x"') < 0 &&
        chipsEl.innerHTML.indexOf('title="/m/q&quot;uote.html"') > 0,
        chipsEl.innerHTML);

    // ---- 8. 非法名 / 非 html / 超 10MB：提示且不发请求（拖拽入口） ----
    reset([]);
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('a.txt')]}, preventDefault() {}});
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('../x.html')]}, preventDefault() {}});
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('big.html', CHAT_MATERIAL_MAX_BYTES + 1)]}, preventDefault() {}});
    check('reject-no-request', filesUploaded() === 0 && toasts.length === 3,
        'uploads=' + filesUploaded() + ' toasts=' + JSON.stringify(toasts));
    check('reject-toast-is-error', toasts.every((t) => t[1]), JSON.stringify(toasts));

    // ---- 9. 累计口径：已 3 + 新增 3 → 整体拒（不发请求），3 + 2 放行 ----
    reset([{filename: 'p1.html'}, {filename: 'p2.html'}, {filename: 'p3.html'}]);
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('n1.html'), f('n2.html'), f('n3.html')]}, preventDefault() {}});
    check('cumulative-reject', filesUploaded() === 0 && pendingChatMaterials.length === 3,
        'uploads=' + filesUploaded() + ' pending=' + pendingChatMaterials.length);
    check('cumulative-toast', toasts.some((t) => t[0].indexOf('单轮最多 5 个') === 0), JSON.stringify(toasts));
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('n1.html'), f('n2.html')]}, preventDefault() {}});
    check('cumulative-boundary-ok', filesUploaded() === 1 && uploadedCounts()[0] === 2,
        JSON.stringify(uploadedCounts()));
    await settle(); await settle();

    // ---- 10. 不可输入态（与按钮 disabled 同口径）：drop / paste 均不入队 ----
    reset([]);
    chatInputEl.disabled = true;                 // renderChatStatus 在 running/creating 等态同步置位
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('a.html')]}, preventDefault() {}});
    chatInputEl.dispatch('paste', {clipboardData: {files: [f('a.html')]}, preventDefault() {}});
    check('disabled-blocks-drop-and-paste',
        filesUploaded() === 0 && pendingChatMaterials.length === 0 && toasts.length <= 2,
        'uploads=' + filesUploaded() + ' pending=' + pendingChatMaterials.length + ' toasts=' + JSON.stringify(toasts));
    // created 终态同样拦（不依赖 disabled）
    reset([]);
    chatState = {id: 7, status: 'created'};
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('a.html')]}, preventDefault() {}});
    chatInputEl.dispatch('paste', {clipboardData: {files: [f('a.html')]}, preventDefault() {}});
    check('created-blocks-drop-and-paste', filesUploaded() === 0 && pendingChatMaterials.length === 0,
        'uploads=' + filesUploaded() + ' pending=' + pendingChatMaterials.length + ' toasts=' + JSON.stringify(toasts));

    // ---- 11. >1MB（≤10MB）允许上传，chip 标 warn（服务端 over_read_limit） ----
    reset([{filename: 'big.html', path: '/m/big.html', over_read_limit: true, size: CHAT_MATERIAL_READ_CAP + 1}]);
    renderChatAttachBar();
    check('chip-warn-class', chipsEl.innerHTML.indexOf('chat-attach-chip warn') > 0, chipsEl.innerHTML);
    check('chip-warn-tip', chipsEl.innerHTML.indexOf('1MB') > 0, chipsEl.innerHTML);

    // ---- 12. 上传接口失败（500）：不产生 chip + 报错，纯文本发送路径不被阻塞（pending 不变） ----
    reset([]);
    fetchOk = false;
    inputRowEl.dispatch('drop', {dataTransfer: {files: [f('boom.html')]}, preventDefault() {}});
    await settle(); await settle();
    check('upload-500-toast-no-chip',
        filesUploaded() === 1 && pendingChatMaterials.length === 0 && toasts.some((t) => t[0].indexOf('上传失败') === 0),
        'uploads=' + filesUploaded() + ' pending=' + pendingChatMaterials.length + ' toasts=' + JSON.stringify(toasts));

    check('binder-wired-via-document', boundFileInput === fileInputEl, String(boundFileInput && boundFileInput.id));

    process.stdout.write(JSON.stringify({checks: results}, null, 1));
    const failed = results.filter((r) => !r.pass);
    if (failed.length) {
        process.stderr.write('\nFAILED: ' + failed.map((r) => r.name + ' → ' + r.detail).join('\n'));
        process.exit(1);
    }
})();
