// #381 renderDiff 纯函数测试 harness（由 tests/test_diff_render.py 拼装并调用）：
// 文件开头被 Python 前置注入从 board.py 提取的 renderDiff 函数源码，
// 然后逐条断言类 GitHub 渲染行为与 XSS 基线。
// 输出 JSON：{"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({ name: name, pass: !!pass, detail: detail == null ? '' : String(detail) });
}

// ---- 1. 结构：文件头 / 仓库头 / 截断说明 / hunk / 增删行 / 上下文 ----
{
    const src = [
        '===== 仓库 req-board（git diff 123abc...f-20260828-381）=====',
        '（diff 过长已截断：共 3 行，仅显示前 2000 行；可用 git_diff --stat 定位改动文件后 Read 补看）',
        'diff --git a/app/a.py b/app/a.py',
        'index 1234567..89abcde 100644',
        '--- a/app/a.py',
        '+++ b/app/a.py',
        '@@ -9,6 +9,7 @@',
        ' """',
        '-import os',
        '+import os, sys',
        ' from x',
        '+<script>alert(1)</script>',
        '@@ -20,1 +21,0 @@',
        '-obsolete',
        '',
    ].join('\n');

    const html = renderDiff(src);
    check('D1_文件节头渲染b侧路径',
        html.includes('<div class="diff-file">app/a.py</div>'), html.slice(0, 300));
    check('D2_仓库头渲染为repo行',
        html.includes('<div class="diff-repo">===== 仓库 req-board（git diff 123abc...f-20260828-381）=====</div>'), html.slice(0, 300));
    check('D3_截断说明渲染为note行',
        html.includes('<div class="diff-note">（diff 过长已截断：共 3 行'), html.slice(0, 400));
    check('D4_hunk头渲染且行号保留',
        html.includes('<div class="diff-hunk">@@ -9,6 +9,7 @@</div>'), html);
    check('D5_增行带diff-add且前缀+保留',
        html.includes('<div class="diff-row diff-add">') && html.includes('>+import os, sys</span></div>'), html);
    check('D6_删行带diff-del且前缀-保留',
        html.includes('<div class="diff-row diff-del">') && html.includes('>-import os</span></div>'), html);
    check('D7_上下文行无add/del类且前缀空格保留',
        html.includes('><span class="diff-c"> from x</span></div>')
        && !html.includes('<div class="diff-row"><span class="diff-n">1</span><span class="diff-n b">1</span><span class="diff-c"> from x</span></div> diff-add'), html);
    check('D8_缺省count的hunk头正常渲染',
        html.includes('<div class="diff-hunk">@@ -20,1 +21,0 @@</div>'), html);
    check('D9_空行中性meta不崩溃',
        html.includes('&nbsp;') && html.includes('diff-meta'), html.slice(-300));
}

// ---- 2. XSS：先转义再渲染（验收 3 核心） ----
{
    const html = renderDiff('diff --git a/x.html b/x.html\n@@ -1,2 +1,2 @@\n-<script>alert(1)</script>\n+<img src=x onerror=alert(1)>');
    check('D10_script转义为文本不执行',
        !html.includes('<script>alert(1)</script>') && html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), html);
    check('D11_img标签转义为文本不执行',
        !html.includes('<img src=x') && html.includes('&lt;img src=x onerror=alert(1)&gt;'), html);
}

// ---- 3. 行号推进（@@ -1,3 +1,4 @@ 后：上下文/删/增/增） ----
{
    const html = renderDiff('@@ -1,3 +1,4 @@\n line-a\n-old\n+new\n+more\n');
    const nums = [];
    const re = /<span class="diff-n(?: b)?">([^<]*)<\/span>/g;
    let m;
    while ((m = re.exec(html)) !== null) nums.push(m[1]);
    const aCol = nums.filter((_, i) => i % 2 === 0).join(',');
    const bCol = nums.filter((_, i) => i % 2 === 1).join(',');
    check('D12_旧行号列推进正确（上下文1/删2/增空/增空）', aCol === '1,2,&nbsp;,&nbsp;', 'a=' + aCol);
    check('D13_新行号列推进正确（上下文1/删空/增2/增3）', bCol === '1,&nbsp;,2,3', 'b=' + bCol);
}

// ---- 4. 边界：空输入 / null / 非结构行 ----
{
    check('D14_空输入返回diff-view不崩溃',
        renderDiff('').includes('diff-view'), renderDiff(''));
    check('D15_null输入不崩溃',
        renderDiff(null).includes('diff-view'), renderDiff(null));
    check('D16_随机文本行中性meta展示',
        renderDiff('random line with + plus\n').includes('diff-meta'), renderDiff('random line with + plus\n'));
}

process.stdout.write(JSON.stringify({ checks: results }));
