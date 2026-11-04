// req-4 模型调参抽屉渲染回归 harness（由 tests/test_robot_drawer_ui.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的真实函数
// （escapeHtml / escapeAttr / formatTokens / robotModelRowsHtml / robotStatsHtml / robotBatchResultText）。
// 输出 JSON {"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
// #6：新增 B2 批量保存渲染断言（表头「保存全部」/行内无保存按钮/data-cur）与 B3 批量文案矩阵。
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

// 状态标签存根（真实来源 = /api/meta 注入；此处只验证渲染分支与缺省原文兜底）
const STATUS_LABEL = {done: '已完成', later: '暂缓', doing: '进行中'};

// ---- D3：主表渲染（四档来源标注 / 清除选项 / 别名池选项 / 覆盖不在池 / 下一张卡生效） ----
{
    const roles = [
        {role: 'dev', display: '需求开发', order: 5, model: 'deepseek/deepseek-v4-pro',
         source: 'project', source_label: '（项目级 projects.json）', override: 'deepseek/deepseek-v4-pro'},
        {role: 'accept', display: '测试验证', order: 7, model: 'deepseek/deepseek-flash',
         source: 'global', source_label: '（全局默认）', override: ''},
    ];
    const aliases = {items: ['deepseek/deepseek-flash', 'deepseek/deepseek-v4-pro'], home: '/h', warning: ''};
    const html = robotModelRowsHtml(roles, aliases, '');
    check('D3a_生效模型与来源标注', html.includes('deepseek/deepseek-v4-pro')
        && html.includes('（项目级 projects.json）') && html.includes('（全局默认）'), html);
    check('D3b_清除选项value空', html.includes('>清除覆盖（回退全局）</option>') && html.includes('value=""'), html);
    check('D3c_别名池选项与当前覆盖选中', html.includes('value="deepseek/deepseek-v4-pro" selected'), html);
    check('D3d_下一张卡生效提示', html.includes('下一张卡生效'), html);
    const html2 = robotModelRowsHtml(
        [{role: 'dev', display: '', model: 'x/legacy', source: 'project',
          source_label: '（项目级 projects.json）', override: 'x/legacy'}],
        {items: ['a/b'], warning: ''}, '');
    check('D3e_覆盖不在池补选项并注明', html2.includes('x/legacy（当前覆盖，不在别名池）')
        && html2.includes('value="x/legacy" selected'), html2);
    let threw = '';
    try { robotModelRowsHtml([], {items: []}, ''); robotModelRowsHtml(null, null, null); }
    catch (e) { threw = String(e); }
    check('D3f_空数据不抛', threw === '', threw);
}

// ---- D4：别名池降级渲染（warning → 模型列手填 input + 格式提示） ----
{
    const warn = 'config.toml 解析失败（bad toml），已按格式校验放行';
    const html = robotModelRowsHtml(
        [{role: 'dev', display: '需求开发', model: 'a/b', source: 'role',
          source_label: '（角色配置）', override: 'm/1'}],
        {items: [], home: null, warning: warn}, warn);
    check('D4a_模型列降级为手填input', html.includes('class="rb-input"') && html.includes('手填模型别名')
        && !html.includes('<select class="rb-select"'), html);
    check('D4b_降级提示文案', html.includes('已降级为手填输入') && html.includes('格式校验'), html);
    check('D4c_当前覆盖值保留', html.includes('value="m/1"'), html);
}

// ---- F（#9）：厂商分组渲染 ----
{
    const roles = [
        {role: 'dev', display: '需求开发', model: 'deepseek/a', source: 'project',
         source_label: '（项目级 projects.json）', override: 'deepseek/a'},
        {role: 'code_review', display: '代码审查', model: 'moonshot/b', source: 'global',
         source_label: '（全局默认）', override: ''},
    ];
    const aliases = {items: ['deepseek/a', 'moonshot/b'], home: '/h', warning: '',
                     groups: [{label: 'deepseek', items: ['deepseek/a']},
                              {label: 'moonshot', items: ['moonshot/b']}]};
    const html = robotModelRowsHtml(roles, aliases, '');
    check('F1a_表头3列无思考深度', html.includes('<th>角色</th>')
        && html.includes('<th>当前生效模型</th>') && html.includes('<th>项目级覆盖</th>')
        && !html.includes('<th>思考深度</th>') && !html.includes('rb-act'), html);
    check('F1c_保存全部移到标题行右端且不占列',
        html.includes('<div class="rb-head"><h3>角色 × 模型（项目级覆盖）</h3>')
        && html.includes('id="robotSaveAllBtn"')
        && html.indexOf('id="robotSaveAllBtn"') < html.indexOf('<table class="rb-table">'), html);
    check('F3a_optgroup分组与成员', html.includes('<optgroup label="deepseek">')
        && html.includes('<optgroup label="moonshot">')
        && /<optgroup label="deepseek"><option value="deepseek\/a"/.test(html), html);
    const html2 = robotModelRowsHtml(
        [{role: 'dev', display: '', model: 'x/1', source: 'role', source_label: '', override: ''}],
        {items: ['a/1', 'b/2'], warning: ''}, '');
    check('F3b_groups缺省回落扁平items', !html2.includes('<optgroup')
        && html2.includes('value="a/1"') && html2.includes('value="b/2"'), html2);
    let empty = '';
    let threw = '';
    try {
        empty = robotModelRowsHtml([], {items: [], warning: ''}, '');
    } catch (e) { threw = String(e); }
    check('F3c_空数据仍渲染3列表头且不抛', threw === '' && empty.includes('<th>角色</th>')
        && empty.includes('<th>当前生效模型</th>') && empty.includes('<th>项目级覆盖</th>')
        && !empty.includes('<th>思考深度</th>'), threw || empty);
    const evil = robotModelRowsHtml(
        [{role: 'dev', display: '', model: 'a"b', source: 'project', source_label: '',
          override: '"><img src=x>'}],
        {items: [], groups: [{label: '"><svg onload=1>', items: ['ok/1']}], warning: ''}, '');
    check('F4a_组标签属性位转义', evil.includes('label="&quot;&gt;&lt;svg onload=1&gt;"')
        && !evil.includes('<svg onload=1>'), evil);
    check('F4b_模型覆盖值属性位转义', !evil.includes('<img src=x>')
        && evil.includes('value="&quot;&gt;&lt;img src=x&gt;"'), evil);
    const evilT = robotModelRowsHtml(
        [{role: 'dev', display: '', model: 'm', source: 'global', source_label: '<b>坏</b>',
          override: ''}],
        {items: [], warning: ''}, '');
    check('F4d_生效模型来源标注转义', !evilT.includes('<b>坏</b>'), evilT);
}

// ---- D5：统计渲染（分组行 / 状态分布 / 空态 / 数值格式化 / 缺省状态原文） ----
{
    const groups = [
        {role: 'dev', model: 'm-a', sessions: 3, ctx_tokens: 1194425, tool_count: 12,
         statuses: {done: 2, later: 1}},
        {role: 'accept', model: '（未记录）', sessions: 1, ctx_tokens: 0, tool_count: 0,
         statuses: {weird_status: 1}},
    ];
    const html = robotStatsHtml(groups);
    check('D5a_行与格式化', html.includes('<td>3</td>') && html.includes('1.19M')
        && html.includes('<td>12</td>') && html.includes('（未记录）'), html);
    check('D5b_状态中文化', html.includes('已完成 2') && html.includes('暂缓 1'), html);
    check('D5c_状态缺省原文', html.includes('weird_status 1'), html);
    check('D5d_空态文案', robotStatsHtml([]).includes('暂无会话数据')
        && robotStatsHtml(null).includes('暂无会话数据')
        && robotStatsHtml(undefined).includes('暂无会话数据'), '');
    check('D5e_口径注记', html.includes('显示名') && html.includes('关联不到计 0'), html);
    // #42 缓存命中列：cached tokens + 三态计数 + 命中率（分母 Σinput>0 即出数字，0 命中出 0%）
    const cacheHtml = robotStatsHtml([
        {role: 'dev', model: 'm-a', sessions: 2, ctx_tokens: 100, tool_count: 1,
         cached_tokens: 25, input_tokens: 100,
         cache_states: {hit: 1, unknown: 1, na: 0}, cache_hit_rate: 0.25},
        {role: 'dev', model: 'm-b', sessions: 1, ctx_tokens: 5, tool_count: 0,
         cached_tokens: 0, input_tokens: 500,
         cache_states: {hit: 1, unknown: 0, na: 0}, cache_hit_rate: 0},
        {role: 'dev', model: 'm-c', sessions: 1, ctx_tokens: 5, tool_count: 0,
         cached_tokens: 0, input_tokens: 0,
         cache_states: {hit: 0, unknown: 1, na: 0}, cache_hit_rate: null},
    ]);
    check('D5f_缓存三态与命中率', cacheHtml.includes('<th>缓存命中</th>')
        && cacheHtml.includes('命中 1 / 未提供 1 / 不适用 0')
        && cacheHtml.includes('命中率 25%')
        && cacheHtml.includes('命中率 0%')                     // 显式 0 命中出 0%（真实零命中）
        && cacheHtml.includes('命中 0 / 未提供 1 / 不适用 0'), cacheHtml);
    check('D5g_缓存口径注记', cacheHtml.includes('未返回记「未提供」')
        && cacheHtml.includes('零完成请求记「不适用」')
        && cacheHtml.includes('仅计返回了字段的运行')
        && cacheHtml.includes('不是缓存命中'), cacheHtml);
    // #42 时间范围 → since 参数纯函数（all/非法 → 空串；7 → 本地日期字符串）
    const since7 = robotStatsSinceParam('7');
    check('D5h_since参数', robotStatsSinceParam('all') === ''
        && robotStatsSinceParam('bad') === ''
        && /^\d{4}-\d{2}-\d{2}$/.test(since7)
        && robotStatsSinceParam('1') === (d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
            + '-' + String(d.getDate()).padStart(2, '0'))(new Date()), since7);
}

// ---- D6：转义矩阵（角色名/模型名注入 <script>/引号 → 文本位转义、属性位 escapeAttr） ----
{
    const evil = robotModelRowsHtml(
        [{role: '<script>alert(1)</script>', display: '"><img src=x>', model: 'a"b<svg>',
          source: 'project', source_label: '<b>', override: 'o"n<script>'}],
        {items: ['ok/one'], home: '/h', warning: ''}, '');
    check('D6a_文本位无裸标签', !evil.includes('<script>alert') && !evil.includes('<img src=x>')
        && !evil.includes('<svg>'), evil);
    check('D6b_属性位引号转义', evil.includes('data-role="&lt;script&gt;alert(1)&lt;/script&gt;"')
        && evil.includes('value="o&quot;n&lt;script&gt;"'), evil);
    const evil2 = robotStatsHtml(
        [{role: '<img>', model: '"q"<script>', sessions: 1, ctx_tokens: 1, tool_count: 1,
          statuses: {'<x>': 1}}]);
    check('D6c_统计同样转义', !evil2.includes('<img>') && !evil2.includes('<script>')
        && evil2.includes('&lt;x&gt; 1'), evil2);
}

// ---- B2（#6）：批量保存渲染——分组标题行右端「保存全部」（#9x 起不再占表格列）/ 行内无保存按钮 / 控件带 data-cur ----
{
    const roles = [
        {role: 'dev', display: '需求开发', model: 'a/b', source: 'project',
         source_label: '（项目级 projects.json）', override: 'a/b'},
        {role: 'accept', display: '测试验证', model: 'a/b', source: 'global',
         source_label: '（全局默认）', override: ''},
    ];
    const html = robotModelRowsHtml(roles, {items: ['a/b'], home: '/h', warning: ''}, '');
    check('B2a_标题行保存全部按钮', html.includes('id="robotSaveAllBtn"')
        && html.includes('class="btn rb-save-all"') && html.includes('保存全部'), html);
    check('B2b_行内无保存按钮', !html.includes('class="btn rb-save"')
        && !html.includes('>保存</button>'), html);
    check('B2c_控件带data-cur', html.includes('data-role="dev" data-cur="a/b"')
        && html.includes('data-role="accept" data-cur=""'), html);
    const html2 = robotModelRowsHtml(
        [{role: 'dev', display: '', model: 'x', source: 'role', source_label: '', override: 'm/1'}],
        {items: [], home: null, warning: 'w'}, 'w');
    check('B2d_降级input同样带data-cur', html2.includes('class="rb-input"')
        && html2.includes('data-cur="m/1"'), html2);
    check('B2e_表尾注记保留原文案', html.includes('下一张卡生效'), html);
}

// ---- B3（#6）：批量结果文案矩阵（全成功 / 含失败 / 零变更） ----
{
    check('B3a_全成功文案', robotBatchResultText(3, 0) === '已保存 3 项（下一张卡生效）',
        robotBatchResultText(3, 0));
    check('B3b_含失败文案', robotBatchResultText(2, 1) === '保存完成：生效 2 项，失败 1 项（失败项保留当前选择，可重试）',
        robotBatchResultText(2, 1));
    check('B3c_零变更文案', robotBatchResultText(0, 0) === '没有改动', robotBatchResultText(0, 0));
    check('B3d_边界不抛', robotBatchResultText(null, undefined) === '没有改动'
        && robotBatchResultText(-1, -2) === '没有改动', '');
}

console.log(JSON.stringify({checks: results}));
// 不调用 process.exit()：大输出（detail 含完整 HTML）时 pipe 写入可能被截断在 64KB，
// 用 exitCode 让 node 自然退出并 flush stdout。
process.exitCode = results.every(r => r.pass) ? 0 : 1;
