// #16 流体置换背景：纯函数 harness（由 tests/test_fluid_background.py 拼装调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的
//   function parseCssColor / fluidRenderSize / fluidTokenNames / fluidReadTokens
// 本文件随后定义 H1~H5 断言。断言边界：只覆盖「不触 WebGL/DOM」的纯函数；
// GLSL 编译、上下文丢失、reduced-motion 等行为由浏览器级用例（B1-B9）覆盖。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

// ---- H1 parseCssColor：CSS 颜色 → [r,g,b]（0..1），非法串返回 null 不抛 ----
{
    const approx = (a, b) => Math.abs(a - b) < 1e-9;
    const c1 = parseCssColor('#050d18');
    check('H1a_解析6位hex', Array.isArray(c1) && c1.length === 3
        && approx(c1[0], 5 / 255) && approx(c1[1], 13 / 255) && approx(c1[2], 24 / 255), JSON.stringify(c1));
    const c2 = parseCssColor('#abc');
    check('H1b_解析3位hex', Array.isArray(c2)
        && approx(c2[0], 0xaa / 255) && approx(c2[1], 0xbb / 255) && approx(c2[2], 0xcc / 255), JSON.stringify(c2));
    const c3 = parseCssColor('rgb(1, 2, 3)');
    check('H1c_解析rgb', Array.isArray(c3)
        && approx(c3[0], 1 / 255) && approx(c3[1], 2 / 255) && approx(c3[2], 3 / 255), JSON.stringify(c3));
    const c4 = parseCssColor('rgba(4, 5, 6, 0.5)');
    check('H1d_解析rgba', Array.isArray(c4)
        && approx(c4[0], 4 / 255) && approx(c4[2], 6 / 255), JSON.stringify(c4));
    check('H1e_非法串返回null',
        parseCssColor('nope') === null && parseCssColor('#zzz') === null
        && parseCssColor('#12345') === null && parseCssColor('') === null && parseCssColor(null) === null, '');
}

// ---- H2/H3 fluidRenderSize：DPR 与最长边双钳制，最小不为 0 ----
{
    const big = fluidRenderSize(1920, 1080, 2, 1.5, 1920);
    check('H2_大屏双钳制', big.w === 1920 && big.h === 1080, JSON.stringify(big));
    const small = fluidRenderSize(375, 800, 3, 1.5, 1920);
    check('H3_小屏不超上限且非零',
        small.w === 563 && small.h === 1200 && small.w > 0 && small.h > 0, JSON.stringify(small));
    const zero = fluidRenderSize(0, 0, 0, 1.5, 1920);
    check('H3b_零尺寸不塌为0', zero.w >= 1 && zero.h >= 1, JSON.stringify(zero));
}

// ---- H4 fluidReadTokens：读令牌 + 去空白；缺失回退 null 不抛 ----
{
    const stub = (map) => ({getPropertyValue: (n) => (n in map ? map[n] : '')});
    const t = fluidReadTokens(stub({
        '--fluid-color-a': '#06203c', '--fluid-color-b': '  #0b4f7a  ',
        '--fluid-color-c': 'rgb(34, 211, 238)', '--fluid-veil': '#050d18', '--fluid-opacity': '0.9',
    }));
    check('H4a_令牌解析与去空白',
        t.a === '#06203c' && t.b === '#0b4f7a' && t.c === 'rgb(34, 211, 238)'
        && t.veil === '#050d18' && t.opacity === '0.9', JSON.stringify(t));
    const miss = fluidReadTokens(stub({}));
    check('H4b_缺令牌回退null不抛',
        miss.a === null && miss.b === null && miss.c === null && miss.veil === null && miss.opacity === null,
        JSON.stringify(miss));
    let threw = false;
    try { fluidReadTokens(null); } catch (e) { threw = true; }
    check('H4c_cs为null不抛', !threw, '');
}

// ---- H5 fluidTokenNames：令牌名清单（与 board.py 令牌块集合的对照在 Python 侧断言，防改名漂移） ----
{
    const names = fluidTokenNames();
    const expected = ['--fluid-color-a', '--fluid-color-b', '--fluid-color-c', '--fluid-veil', '--fluid-opacity'];
    check('H5_令牌名清单一致', JSON.stringify(names) === JSON.stringify(expected), JSON.stringify(names));
}

console.log(JSON.stringify({checks: results}));
process.exit(results.every((r) => r.pass) ? 0 : 1);
