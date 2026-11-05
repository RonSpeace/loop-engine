// 上下文编译器界面回归 harness（由 tests/test_context_ui.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 static/board.js 提取的真实常量与纯函数
// （ctx* 分层渲染、prompt* 锁定句/校验/行级对比、skill* 列表/矩阵）。
// 输出 JSON {"checks": [{name, pass, detail}]}。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}
function roleLabel(r) { return {dev: '需求开发', analysis: '需求分析', accept: '测试验证'}[r] || r; }

// ---- 分层分组 / 堆叠条 ----
const items = [
    {id: 'system/common', layer: 'L0', title: '通用约束', src: 'prompts/system/common.md', origin: 'default', hash: 'aaa', chars: 1000, why: '全部角色', truncated: 0},
    {id: 'role/dev', layer: 'L1', title: '需求开发', src: 'prompts/dev.md', origin: 'override', hash: 'bbb', chars: 500, why: '当前节点角色', truncated: 0},
    {id: 'guide:r/arch', layer: 'L2', title: '项目指引 arch', src: '/x/arch.md', origin: 'project', hash: 'ccc', chars: 4000, why: 'roles 含 dev', truncated: 812},
    {id: 'card:title-notes', layer: 'L6', title: '<b>标题</b>', src: 'requirements#8', origin: 'card', hash: 'ddd', chars: 300, why: '本卡', truncated: 0},
    {id: 'skill:browser-verify', layer: 'L6', title: '场景技能 browser-verify', src: 's.md', origin: 'default', hash: 'eee', chars: 200, why: 'auto：命中 static/**', truncated: 0},
    {id: 'runtime:card-frame', layer: 'L7', title: '运行参数', src: 'dispatcher', origin: 'engine', hash: 'fff', chars: 100, why: '本次运行', truncated: 0},
    {id: 'system/context-update', layer: 'runtime', title: '上下文更新', src: 'p', origin: 'default', hash: 'ggg', chars: 50, why: '续跑', truncated: 0},
    {id: 'weird', layer: 'L99', title: '未知层', src: '', origin: '', hash: '', chars: 10, why: '', truncated: 0},
];
const groups = ctxGroupItems(items);
check('分组按层序', groups.map(g => g.layer).join(',') === 'L0,L1,L2,L6,L7,runtime', groups.map(g => g.layer).join(','));
check('未知层并入运行时文案', groups[groups.length - 1].items.length === 2);
check('分组字数合计', groups.find(g => g.layer === 'L6').sum === 500);
check('稳定前缀字数', ctxStableSum(groups) === 5500, ctxStableSum(groups));
const bar = ctxStackBarHtml(groups);
check('堆叠条恰有一个缓存断点', (bar.match(/cx-cut/g) || []).length === 1, bar);
check('缓存断点在最后一个稳定层（L2）', /cx-seg cx-l2 cx-cut/.test(bar), bar);
check('堆叠条只经类名取色（无颜色字面量）', !/#[0-9a-f]{3,6}|rgb/i.test(bar));
check('图例含层名', ctxLegendHtml(groups).includes('项目约定'));

// ---- 条目列表：徽标 / 展开 / 对比 / 转义 ----
const html = ctxItemsHtml(groups, {open: {'role/dev': true}, diff: {changed: ['role/dev'], added: ['card:title-notes'], removed: ['old/item']},
                                   excerpts: {'role/dev': {text: '<script>x</script>'}}});
check('条目标题转义', html.includes('&lt;b&gt;标题&lt;/b&gt;') && !html.includes('<b>标题</b>'));
check('展开态 aria-expanded=true', /data-cx-arg="role\/dev" aria-expanded="true"/.test(html));
check('收起态 aria-expanded=false', /data-cx-arg="system\/common" aria-expanded="false"/.test(html));
check('截断徽标', html.includes('截断 812 字'));
check('本机覆盖徽标', html.includes('本机覆盖'));
check('L6 数据围栏徽标', (html.match(/数据围栏/g) || []).length === 2);
check('auto 自动触发徽标', html.includes('自动触发'));
check('变更 / 新增徽标', html.includes('>变更<') && html.includes('>新增<'));
check('移除条目单列', html.includes('已移除') && html.includes('old/item'));
check('摘录在 pre 且 data-i18n-skip', /<pre class="cx-pre" data-i18n-skip>&lt;script&gt;/.test(html));
const html2 = ctxItemsHtml(groups, {open: {}, diff: null});
check('关闭对比无变更徽标', !html2.includes('>变更<') && !html2.includes('已移除'));

// ---- 警告去处 ----
const w = ctxWarningsHtml([
    {code: 'missing_skill', id: 'skill:db-migration', message: '技能 db-migration 未找到'},
    {code: 'truncated', id: 'guide:r/arch', message: '截断'},
    {code: 'stale_override', id: 'system/common', message: '覆盖基于旧默认'},
    {code: 'missing_required', id: 'skill:x', message: '<必需>'},
]);
check('缺技能 → 技能库', /data-cx-act="skill" data-cx-arg="db-migration"/.test(w));
check('截断指引 → 查看该指引', /data-cx-act="item" data-cx-arg="guide:r\/arch">查看该指引/.test(w));
check('覆盖基于旧默认 → 提示词', /data-cx-act="prompt" data-cx-arg="system\/common"/.test(w));
check('必需缺失为红色横幅且转义', w.includes('cx-banner-bad') && w.includes('&lt;必需&gt;'));

// ---- 统计行：前缀缓存判断 ----
const m = {model: {id: 'm1', source: '项目级'}, runtime: 'kimi', chars: 1234, prefix_hash: 'abcdef123456', prompt_hash: '999999999999'};
check('首轮', ctxStatsHtml(m, null).includes('首轮'));
check('前缀一致', ctxStatsHtml(m, {prefix_hash: 'abcdef123456'}).includes('与上一轮一致'));
check('前缀变化', ctxStatsHtml(m, {prefix_hash: 'zzz'}).includes('有变化'));
check('hash 缩写', ctxStatsHtml(m, null).includes('abcd…3456'));

// ---- 流程节点条 ----
const card = {nodes_json: JSON.stringify([{role: 'dev', node_type: 'code'}, {role: 'code_review', node_type: 'review', name: '审查'}, {role: 'accept', node_type: 'artifact'}, {node_type: 'terminal'}])};
const runs = [
    {run_key: '20261104-143000-dev-e5f6', role: 'dev', status: 'running'},
    {run_key: '20261104-120000-merge_resolve-aaaa', role: 'merge_resolve', status: 'done'},
    {run_key: '20261104-101200-dev-c3d4', role: 'dev', status: 'done'},
    {run_key: '20261104-091500-analysis-a1b2', role: 'analysis', status: 'done'},
];
const nodes = ctxNodeList(card, runs, 'analysis');
check('节点：前置 + 流程 + 档案里的其它角色', nodes.map(n => n.role).join(',') === 'analysis,dev,code_review,accept,merge_resolve', nodes.map(n => n.role).join(','));
check('节点轮数与最新状态', nodes[1].runs === 2 && nodes[1].latest.status === 'running');
check('节点名取快照 name', nodes[2].name === '审查');
check('未运行节点 runs=0', nodes[3].runs === 0 && nodes[3].latest === null);
check('无前置档案不显示前置节点', ctxNodeList(card, [], 'analysis')[0].role === 'dev');
check('脏 nodes_json 不抛错', ctxNodeList({nodes_json: '{bad'}, runs, '').length === 3);
const rounds = ctxRoleRounds(runs, 'dev');
check('轮次编号：新 → 旧，第 2 轮在前', rounds[0].round === 2 && rounds[1].round === 1);

// ---- 提示词：锁定句标记往返 ----
const LOCK = '3. 结束前写 result.json：{"status": "done"}';
const text = '1. 时间\n' + LOCK + '\n4. 检查单';
const ed = promptToEditable(text, [LOCK]);
check('锁定句替换为标记', ed.text === '1. 时间\n⟦锁定句 1⟧\n4. 检查单' && ed.missing.length === 0, ed.text);
check('标记还原为原文（逐字节）', promptFromEditable(ed.text, [LOCK]) === text);
check('锁定句缺失时报告 missing', promptToEditable('无', [LOCK]).missing[0] === 0);
const d = {placeholders: ['pid'], required: ['pid'], locked: [LOCK]};
let c = promptClientCheck(d, ed.text + ' {pid}');
check('合法文本无 bad', !c.some(x => x[0] === 'bad'), JSON.stringify(c));
c = promptClientCheck(d, ed.text);
check('缺必需占位符', c.some(x => x[0] === 'bad' && x[1].includes('{pid}')), JSON.stringify(c));
c = promptClientCheck(d, ed.text + ' {pid} {timeout}');
check('未声明占位符', c.some(x => x[0] === 'bad' && x[1].includes('{timeout}')), JSON.stringify(c));
c = promptClientCheck(d, '1. 时间 {pid}');
check('删掉锁定句标记即报错', c.some(x => x[0] === 'bad' && x[1].includes('被删除')), JSON.stringify(c));
c = promptClientCheck(d, ed.text + '{pid}\n⟦锁定句 1⟧');
check('锁定句标记重复即报错', c.some(x => x[0] === 'bad' && x[1].includes('重复')), JSON.stringify(c));
c = promptClientCheck({placeholders: [], required: [], locked: []}, 'x'.repeat(20001));
check('超长报错', c.some(x => x[0] === 'bad' && x[1].includes('20,000')), JSON.stringify(c));
check('非占位花括号不误报', !promptClientCheck({placeholders: [], required: [], locked: []}, 'JSON {"a": 1} {X}').some(x => x[0] === 'bad'));

// ---- 行级对比 ----
const diff = promptLineDiff('a\nb\nc', 'a\nB\nc\nd');
check('LCS 行对比', JSON.stringify(diff) === JSON.stringify([[' ', 'a'], ['-', 'b'], ['+', 'B'], [' ', 'c'], ['+', 'd']]), JSON.stringify(diff));
check('一致时提示完全一致', promptDiffHtml('x', 'x').includes('完全一致'));
check('对比输出转义且不翻译', /data-i18n-skip/.test(promptDiffHtml('<a>', '<b>')) && promptDiffHtml('<a>', '<b>').includes('&lt;b&gt;'));

// ---- 提示词目录分组 ----
const pg = promptGroups([{id: 'system/common', layer: 'L0', locked: ['x']}, {id: 'role/dev', layer: 'L1', kind: 'role'},
                         {id: 'project/p/r/manifest', layer: 'L2', kind: 'project'}, {id: 'system/wrapup', layer: 'runtime'}]);
check('提示词分组：项目约定只读组放最后', pg.map(g => g.key).join(',') === 'L0,L1,runtime,project', pg.map(g => g.key).join(','));
check('含锁定句标记组', pg[0].locked === true && pg[1].locked === false);

// ---- 编辑器（纯渲染） ----
const det = {id: 'system/kill-redline', title: '进程安全红线', layer: 'L0', status: 'stale', file: 'system/kill-redline.md', placeholders: ['pid'], required: ['pid'],
             locked: [LOCK], used_by: '全部角色', when: null, text: text, default_text: text, override_text: text, base_text: 'old', readonly: false};
let eh = promptEditorHtml(det, {editable: ed.text, dirty: false, diff: false, three: true, serverErr: [{message: '锁定句被删改'}], localDir: '/l', rebase: false});
check('stale 横幅 + 三方对比三列', eh.includes('默认已更新') && (eh.match(/class="pp-col"/g) || []).length === 3);
check('三方对比动作', eh.includes('data-pp-act="takenew"') && eh.includes('data-pp-act="keepmine"') && eh.includes('data-pp-act="rebasenew"'));
check('未修改时保存禁用', /id="ppSaveBtn" data-pp-act="save" disabled/.test(eh));
check('占位符芯片', eh.includes('data-pp-act="ins" data-pp-arg="pid"'));
check('服务端错误展示', eh.includes('服务端拒绝保存') && eh.includes('锁定句被删改'));
check('正文在 textarea（翻译层跳过）', /<textarea id="ppEdit"/.test(eh));
eh = promptEditorHtml(det, {editable: ed.text + ' {pid}', dirty: true, diff: false, three: false, serverErr: [], localDir: '/l'});
check('修改且合法时保存可用', /id="ppSaveBtn" data-pp-act="save">/.test(eh));
const ro = promptEditorHtml({id: 'project/p/r/manifest', title: 'p 清单', layer: 'L2', status: 'project', file: '/repo/.loop/project.json', readonly: true, text: '{"a":1}', used_by: 'x'}, {});
check('只读条目：无保存、显示路径、内容不翻译', !ro.includes('ppSaveBtn') && ro.includes('/repo/.loop/project.json') && /pre class="cx-pre pp-ro" data-i18n-skip/.test(ro));

// ---- 技能库 ----
const roles = [{id: 'dev', display: '需求开发'}, {id: 'accept', display: '测试验证'}];
const g = {name: 'browser-verify', scope: 'global', roles: ['dev'], activation: 'always', mounted: [], editable: true, description: '浏览器', effective_roles: ['dev'], lint_level: 'ok', status: 'active'};
const mnt = {name: 'render-assert', scope: 'global', roles: [], activation: 'auto', mounted: [{role: 'dev', source: 'flows.json'}], editable: true};
const proj = {name: 'render-assert', scope: 'project', project: 'demo', roles: ['dev'], activation: 'always', mounted: [], editable: false, description: '<x>', lint_level: 'error', status: 'draft'};
check('技能键区分项目作用域', skillKey(g) === 'g:browser-verify' && skillKey(proj) === 'project:demo:render-assert');
check('格子：挂载 = 激活方式', skillCell(g, 'dev').value === 'always' && skillCell(g, 'accept').value === '');
let nx = skillCellNext(g, 'accept');
check('— → 始终（加角色）', nx.activation === 'always' && nx.roles.join() === 'dev,accept', JSON.stringify(nx));
nx = skillCellNext(g, 'dev');
check('始终 → 自动', nx.activation === 'auto' && nx.roles.includes('dev'), JSON.stringify(nx));
nx = skillCellNext(Object.assign({}, g, {activation: 'manual'}), 'dev');
check('手动 → —（去角色，激活方式不变）', !nx.roles.includes('dev') && nx.activation === 'manual', JSON.stringify(nx));
nx = skillCellNext(Object.assign({}, mnt, {activation: 'manual'}), 'dev');
check('配置点名的格子不回到 —', nx.activation === 'always' && !nx.roles.includes('dev'), JSON.stringify(nx));
const mx = skillMatrixHtml([g, proj], roles);
check('矩阵：项目技能格子禁用', /data-sk-arg="project:demo:render-assert\|dev" disabled/.test(mx), mx);
check('矩阵：表头为 flows.json 展示名', mx.includes('<th scope="col">需求开发</th>'));
check('矩阵：aria-label 描述状态', mx.includes('aria-label="browser-verify 在 需求开发：始终'));
check('搜索过滤', skillFilter([g, proj], '浏览', 'all').length === 1 && skillFilter([g, proj], '', 'project').length === 1);
const lh = skillListHtml([proj], '', roles);
check('列表：描述转义 + 不翻译', lh.includes('&lt;x&gt;') && /sk-desc" data-i18n-skip/.test(lh));
check('列表：校验失败与草稿', lh.includes('校验失败') && lh.includes('草稿'));
const sd = skillDetailHtml(proj, {}, skillFormFrom({description: 'd', activation: 'auto', roles: ['dev'], paths: ['static/**'], body: 'b', status: 'draft'}), roles, []);
check('项目技能详情只读（输入禁用、无保存）', !sd.includes('data-sk-act="save"') && /id="skBody"[^>]*disabled/.test(sd));
check('auto 时显示路径 / 关键词', sd.includes('id="skPaths"') && sd.includes('static/**'));
check('角色芯片 aria-pressed', /aria-pressed="true" data-sk-act="role" data-sk-arg="dev"/.test(sd));

process.stdout.write(JSON.stringify({checks: results}));
process.exit(results.every(r => r.pass) ? 0 : 1);
