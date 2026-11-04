/* Loop Engine 界面中英切换（DOM 翻译层）。
 *
 * 为什么在 DOM 层而不是改渲染函数：board.js 的渲染函数被 tests/*_harness.js 逐个抽取到 node 里单测，
 * 在函数里插 t('…') 会让抽取出来的函数缺依赖。本层只在浏览器里工作：渲染照旧产出中文，
 * 本层把「界面文案」节点按词典替换成英文，并用 MutationObserver 跟上后续重绘。
 *
 * 不翻译的内容（用户数据 / agent 输出）：SKIP 选择器内的文本节点一律原样；EXACT_ONLY 内只做整节点
 * 精确匹配；LABEL_ONLY 内只换「标签：」前缀。词典单一源 scripts/i18n_en_source.py → static/i18n.en.js。
 * 切换语言写 localStorage('reqBoardLang') 后整页刷新（中文是渲染原文，无需逆向还原）。
 */
(function () {
    'use strict';
    var KEY = 'reqBoardLang';
    var HAN = /[一-鿿]/;
    var SKIP = [
        '.card-title', '.detail-title', '.detail-notes:not(.empty)', '.chat-msg.user', '.chat-msg.agent',
        '.chat-msg.tool', '.chat-draft-title', '.chat-draft-notes', '.artifact-body', '.diff-view',
        '.ev-item.ev-msg', '.ev-item.ev-user', '.ev-item.ev-tool', '.sd-body pre', 'pre', 'code',
        'textarea', 'script', 'style', '[data-i18n-skip]'
    ].join(',');
    var FORCE = '.ev-pending-tag, .ev-empty';
    var EXACT_ONLY = '.nav-item-name, .pb-name, .sess-title, .proc-title';
    var LABEL_ONLY = '.pb-goal, .pb-paths';
    var ATTRS = ['title', 'placeholder', 'aria-label'];

    function storedLang() {
        try { return localStorage.getItem(KEY); } catch (e) { return null; }
    }
    function detectLang() {
        var s = storedLang();
        if (s === 'en' || s === 'zh') return s;
        var nav = (navigator.languages && navigator.languages[0]) || navigator.language || 'zh';
        return /^zh/i.test(nav) ? 'zh' : 'en';
    }
    var lang = detectLang();
    window.loopI18nLang = lang;
    window.setLoopLang = function (next) {
        try { localStorage.setItem(KEY, next); } catch (e) { /* 隐私模式：仅本次生效 */ }
        location.reload();
    };

    var dict = window.LOOP_I18N_EN || {phrases: {}, patterns: [], labels: {}};
    var exact = dict.phrases || {};
    var labels = dict.labels || {};
    var patterns = (dict.patterns || []).map(function (p) { return [new RegExp(p[0], 'g'), p[1]]; });
    // 子串替换只用 ≥2 个汉字的短语（单字只能整节点精确命中，防止误伤）；最长优先
    var keys = Object.keys(exact).filter(function (k) {
        return (k.match(/[一-鿿]/g) || []).length >= 2;
    }).sort(function (a, b) { return b.length - a.length; });
    var esc = function (s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); };
    var phraseRe = keys.length ? new RegExp(keys.map(esc).join('|'), 'g') : null;
    var PUNCT = [[/（/g, ' ('], [/）/g, ')'], [/：/g, ': '], [/，/g, ', '], [/；/g, '; '], [/、/g, ', '],
                 [/。/g, '. '], [/「/g, '“'], [/」/g, '”'], [/？/g, '?'], [/！/g, '!'], [/ {2,}/g, ' ']];

    function normalize(s) {
        PUNCT.forEach(function (p) { s = s.replace(p[0], p[1]); });
        return s.replace(/\( /g, '(').replace(/ \)/g, ')').replace(/^ +| +$/g, function (m) { return m; });
    }

    // 返回译文；不需要翻译时返回 null
    function translate(s, mode) {
        if (!s || !HAN.test(s)) return null;
        var t = s.trim();
        if (Object.prototype.hasOwnProperty.call(exact, t)) return s.replace(t, exact[t]);
        if (mode === 'exact') return null;
        if (mode === 'label') {
            for (var k in labels) {
                if (t.indexOf(k) === 0) return s.replace(k, labels[k]);
            }
            return null;
        }
        var out = phraseRe ? s.replace(phraseRe, function (m) { return exact[m]; }) : s;
        patterns.forEach(function (p) { out = out.replace(p[0], p[1]); });
        if (!HAN.test(out)) out = normalize(out);
        return out === s ? null : out;
    }

    function modeFor(el) {
        if (!el || !el.closest) return 'full';
        if (el.closest(FORCE)) return 'full';
        if (el.closest(SKIP)) return 'skip';
        if (el.closest(LABEL_ONLY)) return 'label';
        if (el.closest(EXACT_ONLY)) return 'exact';
        return 'full';
    }

    function doText(node) {
        var mode = modeFor(node.parentElement);
        if (mode === 'skip') return;
        var out = translate(node.nodeValue, mode);
        if (out !== null) node.nodeValue = out;
    }

    function doAttrs(el) {
        var mode = modeFor(el);
        for (var i = 0; i < ATTRS.length; i++) {
            var v = el.getAttribute(ATTRS[i]);
            if (!v || !HAN.test(v)) continue;
            // 属性几乎都是界面文案；跳过区内只做精确匹配（标题截断提示等可能带用户数据）
            var out = translate(v, mode === 'skip' ? 'exact' : (mode === 'label' ? 'exact' : 'full'));
            if (out !== null) el.setAttribute(ATTRS[i], out);
        }
    }

    function walk(root) {
        if (!root) return;
        if (root.nodeType === 3) { doText(root); return; }
        if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
        if (root.nodeType === 1) doAttrs(root);
        var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
        var n;
        while ((n = w.nextNode())) {
            if (n.nodeType === 3) doText(n);
            else doAttrs(n);
        }
    }

    // 暴露给 board.js / 测试：翻译一段文案（toast、confirm 等不进 DOM 的字符串）
    window.loopT = function (s) {
        if (lang !== 'en') return s;
        var out = translate(String(s), 'full');
        return out === null ? s : out;
    };

    function setupToggle() {
        var btn = document.getElementById('langToggle');
        if (!btn) return;
        var label = document.getElementById('langToggleLabel');
        if (label) label.textContent = lang === 'en' ? '中文' : 'EN';
        btn.setAttribute('aria-label', lang === 'en' ? '切换到中文' : 'Switch to English');
        btn.setAttribute('title', lang === 'en' ? '切换到中文' : 'Switch to English');
        btn.setAttribute('data-i18n-skip', '');
        btn.addEventListener('click', function () { window.setLoopLang(lang === 'en' ? 'zh' : 'en'); });
    }

    // 页头「操作指南」链接带上界面语言：/guide 按 ?lang= 选中文版或英文版
    function setupGuideLink() {
        var links = document.querySelectorAll('a.guide-link');
        for (var i = 0; i < links.length; i++) links[i].setAttribute('href', '/guide?lang=' + lang);
    }

    function start() {
        setupToggle();
        setupGuideLink();
        if (lang !== 'en') return;
        document.documentElement.lang = 'en';
        walk(document.body);
        var queue = [];
        var scheduled = false;
        var flush = function () {
            scheduled = false;
            var batch = queue; queue = [];
            for (var i = 0; i < batch.length; i++) walk(batch[i]);
        };
        new MutationObserver(function (records) {
            for (var i = 0; i < records.length; i++) {
                var r = records[i];
                if (r.type === 'childList') {
                    for (var j = 0; j < r.addedNodes.length; j++) queue.push(r.addedNodes[j]);
                } else if (r.type === 'characterData') {
                    queue.push(r.target);
                } else if (r.type === 'attributes') {
                    queue.push(r.target);
                }
            }
            // 同步刷：避免中文先闪一帧；本层替换后节点已无汉字，再触发的变更会被 translate 快速放过
            if (!scheduled) { scheduled = true; flush(); }
        }).observe(document.body, {childList: true, subtree: true, characterData: true,
                                   attributes: true, attributeFilter: ATTRS});
        // 原生 alert/confirm/prompt：文案来自 board.js 字面量，同样走词典
        ['alert', 'confirm', 'prompt'].forEach(function (fn) {
            var orig = window[fn];
            window[fn] = function (msg) {
                var args = Array.prototype.slice.call(arguments);
                args[0] = window.loopT(msg);
                return orig.apply(window, args);
            };
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
