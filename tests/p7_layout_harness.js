// req-7 P7 布局可拖拽 + 完成卡收缩 harness（由 tests/test_p7_layout_ui.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的真实纯函数
// （parseStoredObject / clampColWidth / colMaxWidth / cardCollapsed）与常量块（COL_GAP_W）。
// 输出 JSON {"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

// ---- H1：clampColWidth 矩阵（中值 / 下界 / 上界 / NaN / lo>hi / 字符串数字） ----
{
    const cases = [[500, 172, 704, 500], [172, 172, 704, 172], [100, 172, 704, 172],
                   [999, 172, 704, 704], [undefined, 172, 704, 172], [NaN, 172, 704, 172],
                   ['x', 172, 704, 172], [500, 704, 172, 704], ['300', 172, 704, 300]];
    const bad = cases.filter(([v, lo, hi, want]) => clampColWidth(v, lo, hi) !== want)
        .map(([v, lo, hi, want]) => `${v}/${lo}/${hi}→${clampColWidth(v, lo, hi)}（期望 ${want}）`);
    check('H1_clampColWidth矩阵', bad.length === 0, bad.join('; '));
}

// ---- H2：colMaxWidth（邻居下限反推列上限；COL_GAP_W 参与计算；小容器收敛下限；非数值容错） ----
{
    check('H2a_nav上限=容器−间距−主区下限', colMaxWidth(1400, 172, 680) === 704,
        String(colMaxWidth(1400, 172, 680)));
    check('H2b_list上限=容器−间距−对话区下限', colMaxWidth(1200, 300, 360) === 824,
        String(colMaxWidth(1200, 300, 360)));
    check('H2c_小容器收敛到自身下限', colMaxWidth(500, 172, 680) === 172,
        String(colMaxWidth(500, 172, 680)));
    check('H2d_COL_GAP_W参与计算', colMaxWidth(1400, 172, 680) === 1400 - COL_GAP_W - 680,
        `COL_GAP_W=${COL_GAP_W}`);
    check('H2e_非数值容器容错取下限', colMaxWidth(undefined, 172, 680) === 172
        && colMaxWidth(NaN, 300, 360) === 300);
}

// ---- H3：#49 卡片折叠（所有状态可手动收起/展开；无键时 done 默认收缩、其余默认展开；跨状态保持；容错） ----
{
    const done7 = {id: 7, status: 'done'};
    check('H3a_done无键默认收缩', cardCollapsed(done7, {}) === true);
    check('H3b_done手动态true展开优先', cardCollapsed(done7, {'7': true}) === false);
    check('H3c_done手动态false收缩', cardCollapsed(done7, {'7': false}) === true);
    check('H3d_数字id与字符串键一致', cardCollapsed({id: 12, status: 'done'}, {'12': true}) === false
        && cardCollapsed({id: 12, status: 'done'}, {'12': false}) === true);
    const others = ['doing', 'open', 'later', 'dropped'];
    const defaultBad = [], collapseBad = [], expandBad = [];
    for (const st of others) {
        if (cardCollapsed({id: 3, status: st}, {}) !== false) defaultBad.push(st);
        if (cardCollapsed({id: 3, status: st}, {'3': false}) !== true) collapseBad.push(st);
        if (cardCollapsed({id: 3, status: st}, {'3': true}) !== false) expandBad.push(st);
    }
    check('H3e_非done无键默认展开', defaultBad.length === 0, defaultBad.join('; '));
    check('H3f_非done手动态false收起', collapseBad.length === 0, collapseBad.join('; '));
    check('H3g_非done手动态true展开', expandBad.length === 0, expandBad.join('; '));
    // 状态变化（open→doing→done）不重置手动态：同一 id 的存储值跨状态生效
    const collapsedState = {'9': false}, expandedState = {'9': true};
    const statuses = ['open', 'doing', 'done'];
    check('H3h_折叠态跨状态保持',
        statuses.every(st => cardCollapsed({id: 9, status: st}, collapsedState) === true)
        && statuses.every(st => cardCollapsed({id: 9, status: st}, expandedState) === false));
    check('H3i_缺参容错', cardCollapsed(null, {}) === false && cardCollapsed(undefined, null) === false);
}

// ---- H4：parseStoredObject（非法/空/数组/标量 → {}；合法对象透传） ----
{
    check('H4a_null与空串回空对象', JSON.stringify(parseStoredObject(null)) === '{}'
        && JSON.stringify(parseStoredObject('')) === '{}'
        && JSON.stringify(parseStoredObject(undefined)) === '{}');
    check('H4b_非法JSON回空对象', JSON.stringify(parseStoredObject('{oops')) === '{}');
    check('H4c_数组拒绝', JSON.stringify(parseStoredObject('[]')) === '{}'
        && JSON.stringify(parseStoredObject('[1,2]')) === '{}');
    check('H4d_标量拒绝', JSON.stringify(parseStoredObject('3')) === '{}'
        && JSON.stringify(parseStoredObject('"s"')) === '{}'
        && JSON.stringify(parseStoredObject('true')) === '{}');
    const obj = parseStoredObject('{"navW": 300, "7": true}');
    check('H4e_合法对象透传', obj.navW === 300 && obj['7'] === true);
    check('H4f_拖尾空白与换行可解析', parseStoredObject('  {"listW": 400}\n').listW === 400);
}

console.log(JSON.stringify({checks: results}));
process.exit(results.every(r => r.pass) ? 0 : 1);
