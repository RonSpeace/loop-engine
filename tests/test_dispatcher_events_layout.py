#!/usr/bin/env python3
"""req-290 防复发：调度历史 spawn 记录合并为单一 flex item（断点只在行尾）。

问题：static/board.js refreshDispatcherEvents 渲染模板中，「时间 + 正文 + session_id 链接」
是 .ev-dispatch 容器的三个平级 flex item（display:flex; gap:10px; white-space:normal）。
容器宽度不足时正文 span 与 session_id 链接各自独立折行，一条逻辑行出现多个断点
（正文断在「新会|话）」，session_id 因 word-break:break-all 断在「…353e|ab8」）。

修复：extra（session_id 链接）由平级第三 flex item 改为嵌套进正文 span，与正文共享
同一折行上下文，断点自然落在行尾。.sf-id-link 为全局共享类（.session-footer 会话信息栏
与 .ev-dispatch 共用），本次只改渲染结构、不动共享 CSS（word-break: break-all 必须保留）。

本测试分两层：
  A. 源码结构断言（Python 层）：模板合并结构、extra 的 class/onclick 保留、
     .sf-id-link 与 .ev-dispatch/.ev-time CSS 未被改动；#411 起增加提问类 confirm
     分支结构断言（摘要行 + 常驻全文卡片 + 断行排版 + 时间列固定宽 8ch）；
  B. node 渲染断言：提取 static/board.js 真实 escapeHtml 源码 + 真实模板行，用样例事件渲染
     出真实 HTML 后断言：链接嵌套于正文 span 内、.ev-dispatch 下恰有两个 span 子节点、
     非 spawn 事件无链接、XSS 转义不退化；#411 起增加 parseConfirmQuestion 行为与
     提问类渲染结构（S5/S6，摘要行 + 兄弟节点卡片 + 无旧橙块/徽标）。

时间列改版（去掉日期 + 点击复制完整时间戳）后追加：
  A. test_a4 列宽断言 8ch→5ch（确认卡缩进同步）；test_a5 断言显示/复制/视觉边界
     （formatEventClock 切 HH:MM、共用 formatTime 不动、onclick 带事件 id + 时间戳走 JS store、
     无 title/无 cursor/无 hover、8ch 不残留）；
  B. S7 用真实 formatEventClock 断言时间列只显示 HH:MM 且点击携带事件 id；S8 用真实
     copyDispTime（注入 store/copyText/showToast）断言点击复制完整时间戳（含秒）与成功/失败提示。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_events_layout -v
    # 或与全量一起：
    .venv/bin/python -m unittest discover -s tests -v
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import backend_text, frontend_js, frontend_src

REPO = Path(__file__).resolve().parent.parent

# 修复后的合并结构（正文 span 内嵌套 extra）；旧结构为 </span>${extra}</div>（平级第三项）
MERGED = "<span>${escapeHtml(e.content)}${extra}</span>"


# ---------- 从 static/board.js 提取纯函数源码（花括号配平，跳过字符串/模板/注释/正则） ----------
def _prev_char(src, i):
    j = i - 1
    while j >= 0 and src[j].isspace():
        j -= 1
    return src[j] if j >= 0 else ""


def _skip_expr(src, i):
    """跳过模板字面量中 ${...} 表达式，返回匹配 } 之后的下标。"""
    depth, n = 1, len(src)
    while i < n:
        c = src[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        elif c in ('"', "'"):
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
        elif c == "`":
            i = _skip_tpl(src, i + 1)
        else:
            i += 1
    raise ValueError("unbalanced ${ expression")


def _skip_tpl(src, i):
    """跳过模板字面量，返回闭反引号之后的下标。"""
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            i = _skip_expr(src, i + 2)
            continue
        i += 1
    raise ValueError("unterminated template literal")


def _extract_function(js, name):
    """提取 static/board.js 内 function <name>(...) { ... } 的完整源码文本（async 函数保留 `async` 前缀）。"""
    start = js.find("function %s(" % name)
    if start < 0:
        raise AssertionError("function %s not found in static/board.js" % name)
    if js[max(0, start - 6):start] == "async ":
        start -= 6
    i = js.index("{", start)
    depth, n = 0, len(js)
    while i < n:
        c = js[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return js[start:i]
        elif c == "/":
            nxt = js[i + 1] if i + 1 < n else ""
            if nxt == "/":  # 行注释
                j = js.find("\n", i)
                i = n if j < 0 else j + 1
            elif nxt == "*":  # 块注释
                j = js.find("*/", i + 2)
                i = n if j < 0 else j + 2
            else:  # 正则字面量（启发式：前一个有效字符不是标识符/数字/右括号时视为正则）
                p = _prev_char(js, i)
                if p and p not in ")]}" and not (p.isalnum() or p in "_$"):
                    j, in_cls = i + 1, False
                    while j < n:
                        ch = js[j]
                        if ch == "\\":
                            j += 2
                            continue
                        if ch == "[":
                            in_cls = True
                        elif ch == "]":
                            in_cls = False
                        elif ch == "/" and not in_cls:
                            break
                        j += 1
                    i = j + 1
                else:
                    i += 1
        elif c in ('"', "'"):
            q = c
            j = i + 1
            while j < n:
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == q:
                    break
                j += 1
            i = j + 1
        elif c == "`":
            i = _skip_tpl(js, i + 1)
        else:
            i += 1
    raise ValueError("unbalanced braces for function %s" % name)


def _extract_templates():
    """从 static/board.js 提取 extra 与普通事件行 return 两个模板字面量文本（模板行均在单行内）。
    #411 起 refreshDispatcherEvents 含提问类 confirm 分支（结构不同），普通行模板需精确锚定。"""
    src = frontend_src()
    m_extra = re.search(r"extra = `(.*?)`;", src, re.S)
    m_ret = re.search(
        r'return `<div class="ev-item ev-dispatch"><span class="ev-time" '
        r'onclick="copyDispTime\(\$\{e\.id\}\)">'
        r'\$\{escapeHtml\(formatEventClock\(e\.ts\)\)\}</span><span>'
        r'\$\{escapeHtml\(e\.content\)\}\$\{extra\}</span></div>`;', src, re.S)
    if not m_extra or not m_ret:
        raise AssertionError("refreshDispatcherEvents 模板行未找到（extra/return）")
    extra_tpl = m_extra.group(1)
    # return 模板 = 包裹 div + 内部（时间 span + 正文 span 含 ${extra}）
    inner = m_ret.group(0)[len("return `<div class=\"ev-item ev-dispatch\">"):-len("</div>`;")]
    return_tpl = '<div class="ev-item ev-dispatch">' + inner + "</div>"
    return src, extra_tpl, return_tpl


def _extract_confirm_templates():
    """提取提问类 confirm 分支（#411）的渲染模板表达式：摘要行 + 常驻全文卡片（两行拼接）。
    返回去掉 return 前缀与结尾分号的表达式文本（node 层直接求值）。"""
    src = frontend_src()
    m = re.search(
        r'return `<div class="ev-item ev-dispatch"><span class="ev-time" '
        r'onclick="copyDispTime\(\$\{e\.id\}\)">'
        r'\$\{escapeHtml\(formatEventClock\(e\.ts\)\)\}</span><span>\$\{summary\}</span></div>`\s*'
        r'\+ `<div class="ev-confirm-card"><div class="card-label">问题全文</div>'
        r'\$\{pretty\}</div>`;', src, re.S)
    if not m:
        raise AssertionError("提问类 confirm 渲染模板未找到")
    return m.group(0)[len("return "):-1]


def _extract_format_clock():
    """提取 formatEventClock 函数源码（node 层用真实函数断言显示形态）。"""
    return _extract_function(frontend_js(), "formatEventClock")


def _extract_copy_disp_time():
    """提取 copyDispTime 函数源码（node 层注入 store/copyText/showToast 断言复制行为）。"""
    return _extract_function(frontend_js(), "copyDispTime")


def _extract_parse_confirm():
    """提取 parseConfirmQuestion 函数源码（node 层行为断言用）。"""
    return _extract_function(frontend_js(), "parseConfirmQuestion")


def _extract_session_model_fn():
    """提取 #14 纯函数 agentSessionModelForSid 源码（node 层行为矩阵 S9 用）。"""
    return _extract_function(frontend_js(), "agentSessionModelForSid")


def _extract_model_fallback():
    """提取兜底文案常量 SESSION_MODEL_FALLBACK 的值（node 层注入给纯函数）。"""
    m = re.search(r"const SESSION_MODEL_FALLBACK = '([^']*)';", frontend_js())
    if not m:
        raise AssertionError("SESSION_MODEL_FALLBACK 常量未找到")
    return m.group(1)


NODE_PROGRAM = r"""
'use strict';
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

// 模拟浏览器 DOM 的 textContent→innerHTML 转义行为（& < > 转义，引号不转义，
// 与真实 DOM 在元素文本节点的行为一致）；escapeHtml 依赖 document.createElement。
function makeEscDiv() {
    let text = '';
    return {
        set textContent(v) { text = String(v == null ? '' : v); },
        get innerHTML() {
            return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
    };
}
global.document = { createElement: () => makeEscDiv() };

const results = [];
function check(name, pass, detail) {
    results.push({ name: name, pass: !!pass, detail: detail == null ? '' : String(detail) });
}

// 从 static/board.js 提取的真实 escapeHtml / formatEventClock 源码，包装为函数表达式求值
const escapeHtml = new Function('return (' + payload.escapeHtmlSrc + ');')();
// 时间列显示形态走真实 formatEventClock（时间戳样例 '2026-09-18 10:13:45' → '10:13'，日期不进显示）
const formatEventClock = new Function('return (' + payload.formatEventClockSrc + ');')();
// 以真实模板文本构造渲染函数（模板引用的 e/escapeHtml/formatEventClock/extra/pendBlock 经参数注入；
// extra 模板引用 ${id}/${jumpTitle}/${model}，注入需求 id=290 与真实渲染一致）
const makeExtra = new Function('id', 'escapeHtml', 'sid', 'jumpTitle', 'model', 'return `' + payload.extraTpl + '`;');
const render = new Function('e', 'escapeHtml', 'formatEventClock', 'extra', 'pendBlock', 'return `' + payload.returnTpl + '`;');
// #411 提问类 confirm 渲染：摘要行 + 常驻全文卡片（两行拼接模板，summary/pretty 由调用方注入）
const renderConfirm = new Function('e', 'escapeHtml', 'formatEventClock', 'summary', 'pretty',
    'return (' + payload.confirmTpl + ');');
const parseConfirmQuestion = new Function('return (' + payload.parseConfirmSrc + ');')();
// #14 会话实际模型纯函数（真实源码求值；兜底文案常量由 static/board.js 注入）
const MODEL_FALLBACK = payload.modelFallback;
const agentSessionModelForSid = new Function('SESSION_MODEL_FALLBACK', 'return (' + payload.sessionModelSrc + ');')(MODEL_FALLBACK);
const TS = '2026-09-18 10:13:45';   // 事件时间戳样例（真实存储格式 YYYY-MM-DD HH:MM:SS）
// 时间列渲染前缀（点击复制完整时间戳的 onclick 携带事件 id）
const TIME_OPEN = '<span class="ev-time" onclick="copyDispTime(';
const TIME_CLOSE = ')">10:13</span>';
// 回放会话链接标题：命中 agent_sessions 注册表行 → 「查看执行过程并继续会话」（落点 = 会话视图，含状态栏）；
// 未命中（历史/旧数据）→ 「查看该会话完整执行过程」（落点 = 只读执行过程）。落点由 openSessionFromSid
// 在点击时按同一判定决定（纯函数 agentSessionRowIdForSid，行为矩阵见 process_markdown_harness.js T21）
const JUMP_TITLE_HIT = '查看执行过程并继续会话';
const JUMP_TITLE_MISS = '查看该会话完整执行过程';
// 会话实际模型样例（#14：原样显示不裁剪 (airoute) 后缀）
const MODEL_REAL = 'DeepSeek V4 Flash (airoute)';

function renderEvent(e, jumpTitle, model) {
    let extra = '';
    if (e.kind === 'spawn' && e.session_id) {
        const sid = escapeHtml(e.session_id);
        // 与真实渲染同构：模型名先取值再转义（无记录 → 兜底文案）
        const m = escapeHtml(model == null ? MODEL_FALLBACK : model);
        extra = makeExtra(290, escapeHtml, sid, jumpTitle || JUMP_TITLE_HIT, m);
    }
    // 无待确认上下文时 pendBlock 恒为空串：结构与改版前完全一致（#290 断点结构断言不变）
    return render(e, escapeHtml, formatEventClock, extra, '');
}

// ---- S1：spawn 事件——链接嵌套于正文 span 内，.ev-dispatch 下恰两个 span 子节点 ----
{
    const sid = 'session_f34ca851-f6b3-4538-aaba-f2f4213765cb';
    const html = renderEvent({ id: 426, kind: 'spawn', content: '拉起「开发」会话（新会话）', session_id: sid, ts: TS },
                             JUMP_TITLE_HIT, MODEL_REAL);
    check('S1_链接嵌套于正文span内（正文后跟空格再开链接）',
        html.includes('拉起「开发」会话（新会话） <span class="sf-id-link"'),
        html);
    check('S1_正文span是最后一个子节点（链接在正文span内部闭合）',
        html.endsWith('</span>(' + MODEL_REAL + ')</span></div>'),
        html);
    check('S1_时间戳仍是独立第一个子节点',
        html.startsWith('<div class="ev-item ev-dispatch"><span class="ev-time" onclick="copyDispTime(426)">10:13</span>'),
        html);
    check('S1_无旧结构（正文span不在链接前闭合）',
        !html.includes('</span> <span class="sf-id-link"'),
        html);
    check('S1_恰两个平级span子节点（时间+正文，链接嵌套不计）',
        html === '<div class="ev-item ev-dispatch"><span class="ev-time" onclick="copyDispTime(426)">10:13</span><span>'
            + '拉起「开发」会话（新会话） <span class="sf-id-link" title="' + JUMP_TITLE_HIT + '"'
            + ' onclick="openSessionFromSid(290,\'' + sid + '\')">' + sid + '</span>(' + MODEL_REAL + ')</span></div>',
        html);
    check('S1_class与onclick保留（点击进入会话视图：命中注册表行 → 走统一入口）',
        html.includes('class="sf-id-link"') && html.includes('onclick="openSessionFromSid(290,'),
        html);
    check('S1_模型名紧随链接闭合标签且无空格（session_xxx(模型名)）',
        html.includes('>' + sid + '</span>(' + MODEL_REAL + ')'),
        html);
    // 未命中注册表行（历史/旧数据）时标题为只读口径，落点仍是同一入口（点击时回退 selectSession）
    const htmlMiss = renderEvent({ id: 426, kind: 'spawn', content: '拉起「开发」会话（新会话）',
                                   session_id: sid, ts: TS }, JUMP_TITLE_MISS);
    check('S1_未命中注册表行标题为只读口径且入口不变',
        htmlMiss.includes('title="' + JUMP_TITLE_MISS + '"')
        && htmlMiss.includes('onclick="openSessionFromSid(290,\'' + sid + '\')"')
        && !htmlMiss.includes('title="' + JUMP_TITLE_HIT + '"'),
        htmlMiss);
    // 未传模型（查不到记录的历史会话）→ 兜底文案（需求原文指定的 DeepSeek V4 Flash）
    const htmlFb = renderEvent({ id: 426, kind: 'spawn', content: 'x', session_id: sid, ts: TS },
                               JUMP_TITLE_MISS, null);
    check('S1_无模型记录兜底显示 DeepSeek V4 Flash（无 airoute 后缀）',
        htmlFb.includes('>' + sid + '</span>(' + MODEL_FALLBACK + ')')
        && !htmlFb.includes('airoute'),
        htmlFb);
}

// ---- S2：非 spawn 事件——无链接，正文span正常闭合，无空 span 残留 ----
{
    const html = renderEvent({ id: 7, kind: 'action', content: '需求进入 doing', session_id: null, ts: TS });
    check('S2_非spawn无链接且结构为时间+正文两子节点',
        html === '<div class="ev-item ev-dispatch"><span class="ev-time" onclick="copyDispTime(7)">10:13</span>'
            + '<span>需求进入 doing</span></div>',
        html);
    check('S2_非spawn行不出现模型括号', !html.includes('(' + MODEL_FALLBACK + ')'), html);
}

// ---- S3：XSS 基线不退化——content 与 session_id 均先转义再进模板 ----
{
    const html = renderEvent({ id: 1, kind: 'spawn', content: '<script>alert(1)</script>', session_id: 'sid<evil>', ts: TS });
    check('S3_content脚本被转义',
        !html.includes('<script>alert') && html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'),
        html);
    check('S3_session_id尖括号被转义（链接文本安全）',
        html.includes('>sid&lt;evil&gt;</span>'),
        html);
    check('S3_模型名尖括号被转义（DB 非受控字符串不破结构）',
        renderEvent({ id: 1, kind: 'spawn', content: 'x', session_id: 'sid', ts: TS },
                    JUMP_TITLE_HIT, '<m>&evil')
            .includes('</span>(&lt;m&gt;&amp;evil)</span></div>'),
        '');
}

// ---- S4：44 字符无空格长 session_id 完整渲染（word-break:break-all 由 CSS 负责，DOM 层不截断） ----
{
    const sid = 'session_0123456789abcdef0123456789abcdef0123456789abcdef';
    const html = renderEvent({ id: 1, kind: 'spawn', content: 'x', session_id: sid, ts: TS });
    // sid 恰出现两次：onclick 参数 + 链接文本（各一处，无截断无重复）
    check('S4_长sid完整保留且仅出现两次（onclick+链接文本）',
        html.endsWith(sid + '</span>(' + MODEL_FALLBACK + ')</span></div>')
        && html.split(sid).length - 1 === 2,
        html.slice(-200));
}

// ---- S5：parseConfirmQuestion 行为（#411）——新/旧格式解析、动作类/非 confirm 返回 null ----
{
    const r1 = parseConfirmQuestion({ kind: 'confirm', content: '「代码审查」需要确认：Q1' });
    check('S5_新格式解析角色名+问题',
        !!r1 && r1.role === '代码审查' && r1.question === 'Q1', JSON.stringify(r1));
    const r2 = parseConfirmQuestion({ kind: 'confirm', content: 'agent 需要确认：Q2' });
    check('S5_旧格式解析问题且无角色名',
        !!r2 && r2.role === null && r2.question === 'Q2', JSON.stringify(r2));
    check('S5_动作类confirm返回null（人工续跑/飞书回复等不卡片化）',
        parseConfirmQuestion({ kind: 'confirm', content: '用户飞书回复：同意（y 1）' }) === null);
    check('S5_非confirm事件返回null',
        parseConfirmQuestion({ kind: 'phase', content: 'x' }) === null);
}

// ---- S6：提问类 confirm 渲染结构（#411）——摘要行 + 常驻全文卡片 ----
{
    const summary = '「代码审查」需要确认，具体信息如下：';
    const html = renderConfirm(
        { id: 99, kind: 'confirm', content: '', ts: TS }, escapeHtml, formatEventClock, summary,
        'A。\nB；\nC?');
    check('S6_摘要行正常展示（无加粗修饰，与普通行同构）',
        html.startsWith('<div class="ev-item ev-dispatch"><span class="ev-time" onclick="copyDispTime(99)">10:13</span><span>'
            + summary + '</span></div>'), html);
    check('S6_卡片为纯展示结构（无待确认徽标/无旧橙块class）',
        html.includes('<div class="ev-confirm-card"><div class="card-label">问题全文</div>')
        && !html.includes('ev-confirm-text') && !html.includes('confirm-badge'), html);
    check('S6_卡片与事件行是兄弟节点（不在flex行内，不挤压摘要）',
        html.includes('</span></div><div class="ev-confirm-card">'), html);
    check('S6_全文含断行排版（句号/分号断行由调用侧注入）',
        html.includes('A。\nB；\nC?'), html);
    check('S6_全文转义（XSS 基线不退化；转义发生在调用侧 escapeHtml 后注入）',
        renderConfirm({ id: 99, kind: 'confirm', content: '', ts: TS }, escapeHtml, formatEventClock, summary,
            escapeHtml('<script>alert(1)</script>')).includes('&lt;script&gt;alert(1)&lt;/script&gt;'),
        html);
}

// ---- S7：时间列显示形态——只显示 HH:MM（无日期、无秒），点击携带事件 id、无视觉暗示 ----
{
    const html = renderEvent({ id: 426, kind: 'action', content: 'x', ts: TS });
    check('S7_时间列只显示 HH:MM（日期与秒不进显示）',
        html.startsWith('<div class="ev-item ev-dispatch">' + TIME_OPEN + '426' + TIME_CLOSE),
        html);
    check('S7_显示不含日期/秒（不出现 2026-09-18 或 10:13:45）',
        !html.includes('2026-09-18') && !html.includes('10:13:45'),
        html);
    check('S7_时间 span 无 title/无手型类（不做可点击视觉暗示）',
        !html.includes('<span class="ev-time" title') && !html.includes('ev-time-link'),
        html);
    check('S7_formatEventClock 真实函数：日期与秒被切掉、空值返回空串',
        formatEventClock('2026-09-18 10:13:45') === '10:13' && formatEventClock('') === '' && formatEventClock(null) === '',
        JSON.stringify([formatEventClock('2026-09-18 10:13:45'), formatEventClock(''), formatEventClock(null)]));
}

// ---- S8：copyDispTime 行为——点击复制完整时间戳（含秒），成功/失败提示 ----
(async () => {
    const store = {};
    let copied = null, copyOk = true, toast = null;
    const copyDispTime = new Function('dispTsStore', 'copyText', 'showToast',
        'return (' + payload.copyDispSrc + ');')(
        store,
        async (t) => { copied = t; return copyOk; },
        (m) => { toast = m; });

    store[426] = TS;
    await copyDispTime(426);
    check('S8_点击复制完整时间戳（含秒，日期原样）', copied === TS, String(copied));
    check('S8_成功提示带完整时间戳', toast === '已复制：' + TS, String(toast));

    copied = null; toast = null;
    await copyDispTime(999);   // store 无此事件 id（时间戳缺失）
    check('S8_无时间戳时不复制并提示', copied === null && toast === '暂无可复制的时间', String(toast));

    store[1] = TS; copyOk = false; toast = null;
    await copyDispTime(1);     // 剪贴板写入失败
    check('S8_复制失败回落提示', toast === '复制失败，请手动选择复制', String(toast));

    // ---- S9（#14）：agentSessionModelForSid 行为矩阵——命中返回真实模型原文（含 airoute 后缀），
    //      查不到行/无 model/空 sid/注册表未加载/脏数据一律兜底，req_id 双条件防串卡 ----
    {
        const rows = [
            {id: 1, req_id: 14, session_id: 'session_a', model: 'DeepSeek V4 Flash (airoute)'},
            {id: 2, req_id: 14, session_id: 'session_b', model: 'DeepSeek V4 Pro'},
            {id: 3, req_id: 14, session_id: 'session_c', model: null},
            {id: 4, req_id: 14, session_id: 'session_d'},                  // 缺 model 字段
            {id: 5, req_id: 13, session_id: 'session_a', model: '别卡模型'}, // 别卡同 sid（防串卡兜底）
        ];
        check('S9_命中返回真实模型原文（含 airoute 后缀不裁剪）',
            agentSessionModelForSid(rows, 14, 'session_a') === 'DeepSeek V4 Flash (airoute)',
            String(agentSessionModelForSid(rows, 14, 'session_a')));
        check('S9_同卡另一会话返回各自模型',
            agentSessionModelForSid(rows, 14, 'session_b') === 'DeepSeek V4 Pro',
            String(agentSessionModelForSid(rows, 14, 'session_b')));
        check('S9_req_id 不匹配命中兜底（防串卡）',
            agentSessionModelForSid(rows, 13, 'session_b') === MODEL_FALLBACK,
            String(agentSessionModelForSid(rows, 13, 'session_b')));
        check('S9_未注册 sid 兜底（历史/旧数据）',
            agentSessionModelForSid(rows, 14, 'session_legacy') === MODEL_FALLBACK,
            String(agentSessionModelForSid(rows, 14, 'session_legacy')));
        check('S9_有行但 model 为空/缺字段兜底',
            agentSessionModelForSid(rows, 14, 'session_c') === MODEL_FALLBACK
            && agentSessionModelForSid(rows, 14, 'session_d') === MODEL_FALLBACK,
            '');
        check('S9_空 sid 兜底',
            agentSessionModelForSid(rows, 14, null) === MODEL_FALLBACK
            && agentSessionModelForSid(rows, 14, '') === MODEL_FALLBACK,
            '');
        check('S9_注册表未加载兜底不抛错',
            agentSessionModelForSid(undefined, 14, 'session_a') === MODEL_FALLBACK
            && agentSessionModelForSid(null, 14, 'session_a') === MODEL_FALLBACK,
            '');
        check('S9_脏数据行不抛错',
            agentSessionModelForSid([null, {}, {req_id: 14}], 14, 'session_x') === MODEL_FALLBACK,
            '');
        check('S9_兜底文案为需求指定的 DeepSeek V4 Flash（无 airoute 后缀）',
            MODEL_FALLBACK === 'DeepSeek V4 Flash' && !MODEL_FALLBACK.includes('airoute'),
            MODEL_FALLBACK);
    }

    console.log(JSON.stringify({ checks: results }));
    process.exit(results.every(r => r.pass) ? 0 : 1);
})();
"""


class DispatcherEventsLayoutTest(unittest.TestCase):
    """调度历史 spawn 记录单一 flex item 结构回归测试"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 JS 渲染回归测试")

    def _run_node(self):
        _, extra_tpl, return_tpl = _extract_templates()
        confirm_tpl = _extract_confirm_templates()
        parse_confirm_src = _extract_parse_confirm()
        escape_html_src = _extract_function(frontend_js(), "escapeHtml")
        format_clock_src = _extract_format_clock()
        copy_disp_src = _extract_copy_disp_time()
        session_model_src = _extract_session_model_fn()
        model_fallback = _extract_model_fallback()
        payload = {
            "escapeHtmlSrc": escape_html_src,
            "extraTpl": extra_tpl,
            "returnTpl": return_tpl,
            "confirmTpl": confirm_tpl,
            "parseConfirmSrc": parse_confirm_src,
            "formatEventClockSrc": format_clock_src,
            "copyDispSrc": copy_disp_src,
            "sessionModelSrc": session_model_src,
            "modelFallback": model_fallback,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8",
                                         delete=False) as f:
            f.write(NODE_PROGRAM)
            js_tmp = f.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8",
                                         delete=False) as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            json_tmp = f.name
        try:
            proc = subprocess.run([self.node, js_tmp, json_tmp],
                                  capture_output=True, text=True, timeout=60)
        finally:
            Path(js_tmp).unlink(missing_ok=True)
            Path(json_tmp).unlink(missing_ok=True)
        if proc.returncode != 0:
            self.fail("node 渲染断言失败: %s\n%s" % (proc.stderr, proc.stdout))
        return json.loads(proc.stdout)

    def test_a1_template_merged_structure(self):
        """验收 1：extra 嵌套进正文 span（单一 flex item），无平级第三项"""
        src, extra_tpl, return_tpl = _extract_templates()
        self.assertIn(MERGED, return_tpl,
                      "正文 span 内应嵌套 ${extra}: %r" % return_tpl)
        self.assertNotIn("</span>${extra}", return_tpl,
                         "旧平级结构不应残留: %r" % return_tpl)

    def test_a2_extra_keeps_link_class_and_onclick(self):
        """验收 3：session_id 链接的 class 与点击入口（#120 入口统一后走 openSessionFromSid）保留"""
        src, extra_tpl, return_tpl = _extract_templates()
        self.assertIn('class="sf-id-link"', extra_tpl)
        self.assertIn('onclick="openSessionFromSid(${id},\'${sid}\')"', extra_tpl)
        # 标题随注册表命中选择（命中 → 会话视图含状态栏；未命中 → 只读执行过程），模板变量 ${jumpTitle}
        self.assertIn('title="${jumpTitle}"', extra_tpl)
        self.assertIn("? '查看执行过程并继续会话' : '查看该会话完整执行过程'", src,
                      "回放链接标题应按 agentSessionRowIdForSid 命中与否区分")

    def test_a3_shared_css_untouched(self):
        """验收 1/2：.sf-id-link 共享类 word-break:break-all 保留；.ev-dispatch 结构不动；
        .ev-time 的 flex-shrink:0（时间戳不收缩）保留（列宽 8ch→5ch 见 test_a5）"""
        src = frontend_src()
        m = re.search(r"\.sf-id-link \{.*?\}", src, re.S)
        self.assertIsNotNone(m, ".sf-id-link 共享类 CSS 未找到")
        self.assertIn("word-break: break-all", m.group(0),
                      ".sf-id-link 的 word-break: break-all 必须保留（长 token 可断）")
        self.assertIn(".ev-dispatch { display: flex; gap: 10px; margin: 4px 0; white-space: normal; }",
                      src, ".ev-dispatch CSS 不应被改动")
        self.assertIn("flex-shrink: 0", src[src.index(".ev-time"):src.index(".ev-time") + 200],
                      ".ev-time 的 flex-shrink:0（时间戳不收缩）应保留")

    def test_a4_confirm_card_structure(self):
        """#411 验收：提问类 confirm 分支结构——摘要行构造、全文卡片、断行排版、时间列固定宽"""
        src = frontend_src()
        self.assertIn('`「${escapeHtml(role || \'agent\')}」需要确认，具体信息如下：`', src,
                      "摘要行模板（角色名 + 需要确认，具体信息如下：）未找到")
        self.assertIn('<div class="ev-confirm-card"><div class="card-label">问题全文</div>',
                      src, "纯展示卡片结构未找到")
        self.assertNotIn(".ev-confirm-text {", src, "旧橙块 CSS 定义应移除")
        self.assertNotIn('class="ev-confirm-text"', src, "旧橙块 HTML class 应移除")
        self.assertIn("replace(/[。；]/g, m => m + '\\n')", src,
                      "全文断行（句号/分号断行）未找到")
        self.assertIn("min-width: 5ch", src,
                      ".ev-time 固定时间列宽 5ch（HH:MM，卡片缩进对齐文本起点）未找到")
        m = re.search(r"\.ev-confirm-card \{[^}]*margin-left: calc\(5ch \+ 10px\)[^}]*\}", src, re.S)
        self.assertIsNotNone(m, ".ev-confirm-card 左对齐文本起点（calc(5ch + 10px)）未找到")

    def test_a5_time_column_click_copy(self):
        """时间列：只显示 HH:MM（日期/秒不进显示）+ 点一下复制完整时间戳（含秒）；
        视觉保持纯文本（默认光标、无 hover、无 title），列宽随显示收窄到 5ch"""
        src = frontend_src()
        # 显示形态：formatEventClock 切 11..16（HH:MM）；共用 formatTime 不动（其它视图仍是 MM-DD HH:MM）
        self.assertIn("s.slice(11, 16)", src, "formatEventClock（HH:MM 切片）未找到")
        self.assertIn("s.slice(5, 16)", src, "formatTime 不应被改动（其它视图保持 MM-DD HH:MM）")
        # 点击复制：onclick 带事件 id，时间戳走 JS store（不进 data-* 属性，防属性注入）
        self.assertIn('onclick="copyDispTime(${e.id})"', src, "时间列 onclick（复制入口）未找到")
        self.assertIn("dispTsStore[e.id] = e.ts;", src, "时间戳 store 写入未找到")
        self.assertNotIn("data-ts=", src, "时间戳不应写进 data-* 属性")
        self.assertNotIn('class="ev-time" title', src, "时间列不应加 title 提示")
        # 视觉克制：默认光标、无 hover 变化
        m = re.search(r"\.ev-time \{[^}]*\}", src)
        self.assertIsNotNone(m, ".ev-time CSS 未找到")
        seg = m.group(0)
        self.assertNotIn("cursor", seg, ".ev-time 不应加手型光标")
        self.assertNotIn("hover", seg, ".ev-time 不应加 hover 变化")
        self.assertIn("min-width: 5ch", seg, ".ev-time 列宽应为 5ch（HH:MM 5 字符）")
        self.assertNotIn("8ch", src, "旧 8ch 列宽/确认卡缩进不应残留")
        # 命名唯一性（防函数声明提升互相覆盖）：时间列格式化函数最初取名 formatClock，与页面底部
        # 调度器健康角标的 formatClock(age) 撞名——后声明的函数静默生效，时间列实测渲染成 "Invalid "。
        # 时间列用到的两个全局名（格式化函数 + 复制入口）必须全页唯一。
        m = re.search(r"\$\{escapeHtml\((format\w*Clock)\(e\.ts\)\)\}", src)
        self.assertIsNotNone(m, "时间列格式化函数调用未找到")
        for fn in (m.group(1), "copyDispTime"):
            self.assertEqual(1, len(re.findall(r"\bfunction %s\(" % fn, src)),
                             "页面脚本内 %s 应恰好声明一次（同名函数声明提升会静默覆盖）" % fn)

    def test_a6_session_link_entry_unified(self):
        """#120 入口统一：回放 spawn 链接命中 agent_sessions 注册表行时落到会话视图
        （执行过程 + 状态栏 + 聊天框，与中间栏会话行一致），未命中回退只读执行过程视图。
        行为矩阵（纯函数 agentSessionRowIdForSid）见 process_markdown_harness.js T21。"""
        src = frontend_src()
        # 纯函数（按 session_id 反查注册表行 id；req_id 双条件防串卡）
        self.assertIn("function agentSessionRowIdForSid(rows, reqId, sid)", src,
                      "会话 id → 注册表行 id 的纯函数未找到")
        self.assertIn("x.session_id === sid && x.req_id === reqId", src,
                      "反查应按 session_id + req_id 双条件匹配")
        # 入口分发：命中走 selectAgentSession（带状态栏/聊天框），未命中回退 selectSession
        m = re.search(r"function openSessionFromSid\(reqId, sid\) \{(.*?)\n        \}", src, re.S)
        self.assertIsNotNone(m, "openSessionFromSid 入口分发函数未找到")
        body = m.group(1)
        self.assertIn("agentSessionRowIdForSid(agentSessions, reqId, sid)", body,
                      "点击时应按当前注册表实时判定（不用渲染期快照）")
        self.assertIn("selectAgentSession(reqId, rowId)", body,
                      "命中注册表行应走 selectAgentSession（会话视图，含状态栏）")
        self.assertIn("selectSession(reqId, sid)", body,
                      "未命中应回退 selectSession（只读执行过程视图）")
        # 回放输出端：链接 onclick 指向统一入口，标题按命中与否区分
        src_tpl, extra_tpl, _ = _extract_templates()
        self.assertIn("openSessionFromSid(${id},'${sid}')", extra_tpl,
                      "回放链接应走统一入口（openSessionFromSid）")
        self.assertNotIn("onclick=\"selectSession(${id},'${sid}')\"", extra_tpl,
                         "旧入口（仅 selectSession）不应残留")
        # 命名唯一性（同 #278 formatClock 教训）：函数声明提升会静默覆盖同名函数
        for fn in ("agentSessionRowIdForSid", "openSessionFromSid"):
            self.assertEqual(1, len(re.findall(r"\bfunction %s\(" % fn, src)),
                             "页面脚本内 %s 应恰好声明一次" % fn)

    def test_b1_node_rendered_checks_pass(self):
        """node 渲染断言（S1-S9，含真实 formatEventClock/copyDispTime/agentSessionModelForSid）全部通过"""
        data = self._run_node()
        checks = data.get("checks", [])
        self.assertGreaterEqual(len(checks), 32, "node 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)

    def test_a7_session_model_rendered(self):
        """#14 验收：spawn 行在会话 id 后追加括号模型名——模型取 agent_sessions.model 原样值
        （查不到记录兜底 DeepSeek V4 Flash）；括号追加在链接闭合标签之后、正文 span 之内；
        模型名先转义；非 spawn 行不追加。纯函数行为矩阵见 node S9。"""
        src = frontend_src()
        # 纯函数（session_id + req_id 双条件反查注册表行，与 agentSessionRowIdForSid 同构）
        self.assertIn("function agentSessionModelForSid(rows, reqId, sid)", src,
                      "会话 id → 实际模型的纯函数未找到")
        self.assertIn("x.session_id === sid && x.req_id === reqId", src,
                      "模型反查应按 session_id + req_id 双条件匹配（防串卡）")
        # 兜底常量：需求原文指定的默认模型名，集中一处定义
        self.assertIn("const SESSION_MODEL_FALLBACK = 'DeepSeek V4 Flash';", src,
                      "兜底文案常量未找到（需求指定 DeepSeek V4 Flash）")
        self.assertEqual(1, len(re.findall(r"const SESSION_MODEL_FALLBACK =", src)),
                         "兜底常量应只定义一次")
        # 渲染端：模型取值经 escapeHtml，模板在链接闭合标签后紧跟括号（无空格）
        self.assertIn("escapeHtml(agentSessionModelForSid(agentSessions, id, e.session_id))", src,
                      "模型取值应经 escapeHtml 转义（DB 非受控字符串）")
        src_tpl, extra_tpl, return_tpl = _extract_templates()
        self.assertIn("</span>(${model})", extra_tpl,
                      "模型括号应追加在链接闭合标签之后且无空格: %r" % extra_tpl)
        self.assertNotIn("${model}", return_tpl,
                         "模型括号只应出现在 spawn 分支的 extra，非 spawn 行不追加")
        # 命名唯一性（同 #278 formatClock 教训）：函数声明提升会静默覆盖同名函数
        for fn in ("agentSessionModelForSid",):
            self.assertEqual(1, len(re.findall(r"\bfunction %s\(" % fn, src)),
                             "页面脚本内 %s 应恰好声明一次" % fn)

    def test_c1_storage_layer_untouched(self):
        """验收 4：存储/API 零改动——session_id 独立列与 PATCH 回填 SQL 原样保留"""
        # #32：建表/迁移在 board_modules/db.py，回填 SQL 在端点（board.py），两处合读
        src = backend_text()
        self.assertIn("ALTER TABLE dispatcher_events ADD COLUMN session_id TEXT", src)
        self.assertIn("UPDATE dispatcher_events SET session_id = ? WHERE id = ? AND req_id = ?", src)
        self.assertIn("CREATE TABLE IF NOT EXISTS dispatcher_events", src)


if __name__ == "__main__":
    unittest.main()
