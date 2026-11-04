// req-3 项目信息条渲染回归 harness（由 tests/test_project_bar_ui.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的真实函数
// （escapeHtml / escapeAttr / formatTokens / formatSuccessRate / displayProjectName / projectBarHtml）
// 与常量块（PROJECT_DISPLAY / PB_STAT_TIP）。输出 JSON {"checks": [{name, pass, detail}]}，
// 全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

// escapeHtml 依赖 document.createElement（提取的真实函数，与 flow_chip_color_harness.js 同款桩）
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

// ---- H1：formatTokens 矩阵（<1000 原样 / k 一位小数 / M 两位小数；非法与负值按 0） ----
{
    const cases = [[0, '0'], [999, '999'], [999.4, '999'], [1500, '1.5k'], [161109, '161.1k'],
                   [1194425, '1.19M'], [1000000, '1.00M'], [-5, '0'], [null, '0'], [NaN, '0'], ['x', '0']];
    const bad = cases.filter(([n, want]) => formatTokens(n) !== want)
        .map(([n, want]) => `${n}→${formatTokens(n)}（期望 ${want}）`);
    check('H1_formatTokens矩阵', bad.length === 0, bad.join('; '));
}

// ---- H2：formatSuccessRate 矩阵（null→"—"；百分比 1 位小数去尾零） ----
{
    const cases = [[null, '—'], [undefined, '—'], [1.0, '100%'], [2 / 3, '66.7%'], [0, '0%'],
                   [0.5, '50%'], ['x', '—']];
    const bad = cases.filter(([r, want]) => formatSuccessRate(r) !== want)
        .map(([r, want]) => `${r}→${formatSuccessRate(r)}（期望 ${want}）`);
    check('H2_formatSuccessRate矩阵', bad.length === 0, bad.join('; '));
}

// 单项目数据（口径样例快照：toy-project 2 卡 2 done 打回 2 Σctx=1,194,425）
const TOY = {
    project: 'toy-project', goal: '闭环冒烟用玩具项目：验证建卡到自动合并的全流程。',
    paths: ['/Users/dev/projects/toy-project'], project_count: null,
    stats: {total: 2, doing: 0, done: 2, dropped: 0, success_rate: 1.0, reject: 2, ctx_tokens: 1194425},
};

// ---- H3：单项目渲染（名称/目标/路径/6 指标/转义/机器人位） ----
{
    const html = projectBarHtml(TOY);
    check('H3a_名称与目标', html.includes('pb-name">toy-project<') && html.includes('目标：闭环冒烟用玩具项目'), html);
    check('H3b_路径全文与title', html.includes('路径：/Users/dev/projects/toy-project')
        && html.includes('title="/Users/dev/projects/toy-project"'), html);
    const six = ['<b>2</b>任务总数', '<b>0</b>进行中', '<b>2</b>完成',
                 '<b>100%</b>成功率', '<b>2</b>打回次数', '<b>1.19M tokens</b>总消耗'];
    const miss = six.filter(s => !html.includes(s));
    check('H3c_六项指标全渲染', miss.length === 0, miss.join('; '));
    check('H3d_总消耗口径tooltip', html.includes('非完整消耗') && html.includes('上下文峰值口径'), html);
    check('H3e_打回口径tooltip', html.includes('verdict=reject') && html.includes('打回'), html);
    check('H3f_机器人可点（具体项目）', html.includes('class="pb-robot on"') && html.includes('<button type="button"')
        && html.includes('title="模型调参"') && !html.includes('aria-disabled="true"'), html);
    // displayProjectName 映射生效（底层 key 不动、只改展示名）
    // 数据驱动：取 PROJECT_DISPLAY 首条映射验证；映射表为空时验证原名透传
    const entry = Object.entries(PROJECT_DISPLAY)[0];
    const [rawName, shownName] = entry || ['my-app', 'my-app'];
    const mapped = projectBarHtml(Object.assign({}, TOY, {project: rawName}));
    check('H3g_展示名映射', mapped.includes(`pb-name">${shownName}<`), mapped);
}

// ---- H4：all 聚合态渲染（名称/项目数/无目标行/机器人禁用） ----
{
    const all = {
        project: 'all', goal: '', paths: [], project_count: 2,
        stats: {total: 5, doing: 1, done: 2, dropped: 0, success_rate: 1.0, reject: 2, ctx_tokens: 1765161},
    };
    const html = projectBarHtml(all);
    check('H4a_聚合名称与项目数', html.includes('pb-name">所有项目<') && html.includes('项目数：2'), html);
    check('H4b_聚合不渲染目标行', !html.includes('目标：'), html);
    check('H4c_聚合数值', html.includes('<b>5</b>任务总数') && html.includes('<b>1.77M tokens</b>总消耗'), html);
    check('H4d_机器人禁用（所有项目）', html.includes('aria-disabled="true"')
        && html.includes('模型调参需选中具体项目') && !html.includes('<button'), html);
}

// ---- H5：goal 缺失/空串 → "未填写" ----
{
    const h1 = projectBarHtml(Object.assign({}, TOY, {goal: ''}));
    const h2 = projectBarHtml(Object.assign({}, TOY, {goal: null}));
    check('H5_goal缺失未填写', h1.includes('目标：未填写') && h2.includes('目标：未填写'),
        h1.slice(0, 120));
}

// ---- H6：注入转义（文本位无裸标签；属性位引号转义） ----
{
    const evil = projectBarHtml(Object.assign({}, TOY, {
        goal: '<img src=x onerror=alert(1)>"q"',
        paths: ['/a"b<c>'],
    }));
    check('H6a_文本位转义', evil.includes('&lt;img') && !evil.includes('<img ')
        && evil.includes('&lt;c&gt;') && !evil.includes('<c>'), evil);
    check('H6b_属性位引号转义', evil.includes('title="/a&quot;b&lt;c&gt;"'), evil);
}

// ---- H7：数据缺失降级（paths 空 → 未配置；stats 缺 → 零值；不抛） ----
{
    let html = '';
    try {
        html = projectBarHtml({project: 'solo', stats: {}, paths: []});
    } catch (e) {
        check('H7_数据缺失不抛', false, String(e));
    }
    if (html) {
        check('H7_数据缺失不抛', html.includes('路径：未配置') && html.includes('<b>0</b>任务总数')
            && html.includes('<b>—</b>成功率'), html);
    }
    const empty = projectBarHtml(null);
    check('H8_空数据降级为聚合零值', empty.includes('所有项目') && empty.includes('项目数：0'), empty);
}

// ---- H9：#21 指标数字配色修饰类（TOY：doing=0 为唯一零值，其余非零） ----
{
    const html = projectBarHtml(TOY);
    const toned = ['pb-stat-total', 'pb-stat-done', 'pb-stat-rate', 'pb-stat-reject', 'pb-stat-tokens'];
    const miss = toned.filter(c => !html.includes(`<span class="pb-stat ${c}"`));
    check('H9a_非零指标带配色修饰类', miss.length === 0, miss.join('; '));
    check('H9b_零值指标保持中性（无修饰类）',
        html.includes('<span class="pb-stat" title=') && !html.includes('pb-stat-doing'), html);
}

// ---- H10：#21 全零与「—」一律不套色（stats 缺失降级态） ----
{
    const zero = projectBarHtml({project: 'solo', stats: {}, paths: []});
    const classes = ['pb-stat-total', 'pb-stat-doing', 'pb-stat-done',
                     'pb-stat-rate', 'pb-stat-reject', 'pb-stat-tokens'];
    const leaked = classes.filter(c => zero.includes(c));
    check('H10_全零与「—」不套色', leaked.length === 0 && zero.includes('<b>—</b>成功率'), leaked.join('; '));
}

console.log(JSON.stringify({checks: results}));
process.exit(results.every(r => r.pass) ? 0 : 1);
