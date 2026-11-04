// req-5 项目管理前端纯函数回归 harness（由 tests/test_project_admin_ui.py 拼装并调用）：
// 文件开头会被 Python 前置注入从 board.py 提取的真实函数
// （escapeHtml / escapeAttr / pmNameValid / pmPathsToArray / projectStatsText /
//  projectRemoveBlocked / projectChecklistHtml）与常量块（PM_STATUS_ORDER）。
// 输出 JSON {"checks": [{name, pass, detail}]}，全部通过时进程退出码 0。
'use strict';

const results = [];
function check(name, pass, detail) {
    results.push({name: name, pass: !!pass, detail: detail == null ? '' : String(detail)});
}

// escapeHtml 依赖 document.createElement（同 project_bar_harness.js 桩）；
// STATUS_LABEL = /api/meta 注入形态（前端不硬编码状态标签）。
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
global.STATUS_LABEL = {later: '暂缓', open: '待开发', doing: '进行中', done: '已完成', dropped: '已废弃'};

// ---- H1：项目名校验矩阵（格式 + 长度 + 唯一性；尾连字符按 ^[a-z0-9][a-z0-9-]*$ 属合法） ----
{
    const existing = ['loop-engine', 'toy-project'];
    const ok = ['a', 'loop-engine2', 'ab-12', '9x', 'my-project-2', 'trail-'];
    const bad = ['', '  ', 'Loop', 'my_project', '项目', 'a'.repeat(65), 'loop-engine', '-lead',
                 'a b', null, undefined];
    const errs = [];
    ok.forEach(v => { if (!pmNameValid(v, existing, 64)) errs.push(`合法被拒: ${v}`); });
    bad.forEach(v => { if (pmNameValid(v, existing, 64)) errs.push(`非法放行: ${String(v)}`); });
    check('H1_名称校验矩阵', errs.length === 0, errs.join('; '));
}

// ---- H2：paths 多行解析（trim / 空行 / CRLF / 全空） ----
{
    const arr = pmPathsToArray('  /a/b  \n\n/c/d\r\n  /e  \n');
    check('H2a_多行解析', JSON.stringify(arr) === JSON.stringify(['/a/b', '/c/d', '/e']), JSON.stringify(arr));
    const empty = pmPathsToArray('\n   \n');
    check('H2b_全空与空输入', empty.length === 0 && pmPathsToArray(null).length === 0,
        JSON.stringify([empty, pmPathsToArray(null)]));
}

// ---- H3：移除确认弹窗统计文案 ----
{
    const s1 = projectStatsText({doing: 2, done: 3, later: 1, open: 0});
    check('H3a_统计文案', s1.includes('共 6 张') && s1.includes('进行中 2') && s1.includes('已完成 3')
        && s1.includes('暂缓 1') && !s1.includes('待开发'), s1);
    check('H3b_空对象', projectStatsText({}) === '共 0 张' && projectStatsText(null) === '共 0 张',
        projectStatsText(null));
    // 未知状态也计入总数（后端出现新状态时不丢数；标签回退原文）
    const s3 = projectStatsText({doing: 1, ghost_status: 2});
    check('H3c_未知状态计数', s3.includes('共 3 张') && s3.includes('ghost_status 2'), s3);
}

// ---- H4：doing 阻断判定 ----
{
    const r = [projectRemoveBlocked({doing: 1}), projectRemoveBlocked({doing: 0, open: 2}),
               projectRemoveBlocked({}), projectRemoveBlocked(null)];
    check('H4_阻断矩阵', r[0] === true && r[1] === false && r[2] === false && r[3] === false,
        JSON.stringify(r));
}

// ---- H5：接入 checklist 内容与转义（含 路径含空格/单引号 的 shell 引号包裹） ----
{
    const html = projectChecklistHtml('my-project', '/abs/path');
    check('H5a_协作约定关键行',
        html.includes('AGENTS.md') && html.includes('status 必须传 "later"')
        && html.includes('agent:my-project:') && html.includes('需求号') && html.includes('PATCH'),
        html.slice(0, 200));
    check('H5b_信任命令示例（路径单引号包裹）',
        html.includes("cd '/abs/path' &amp;&amp; KIMI_CODE_HOME=~/.kimi-code-deepseek kimi")
        && html.includes('pmCopyBtn') && html.includes('pmAgentsMd'), '');
    const sp = projectChecklistHtml('p', '/a b/c d');
    check('H5b2_含空格路径引号包裹', sp.includes("cd '/a b/c d' &amp;&amp;"), '');
    const sq = projectChecklistHtml('p', "/x/it's ok");
    check('H5b3_含单引号路径转义', sq.includes("cd '/x/it'\\''s ok' &amp;&amp;"), sq.slice(0, 400));
    const evil = projectChecklistHtml('<img onerror=alert(1)>', '/x"><img onerror=2>');
    check('H5c_注入转义', !evil.includes('<img') && evil.includes('&lt;img')
        && !evil.includes('"><img'), evil.slice(0, 300));
}

// ---- H6：「选择文件夹」路径追加（trim / 去重 / 空输入） ----
{
    const out = pmPathsAppend('/a/b\n/c/d', '/e/f');
    check('H6a_追加路径', out === '/a/b\n/c/d\n/e/f', JSON.stringify(out));
    const dup = pmPathsAppend('/a/b\n/c/d\n', ' /a/b ');
    check('H6b_去重与trim', dup === '/a/b\n/c/d', JSON.stringify(dup));
    check('H6c_空输入', pmPathsAppend('', '/x') === '/x' && pmPathsAppend('', '') === ''
        && pmPathsAppend(null, null) === '', JSON.stringify([pmPathsAppend('', '/x'), pmPathsAppend(null, null)]));
}

// ---- H7：git-detect 响应 → 表单回填（当前分支/远端列表/URL/非 git 提示/异常降级） ----
{
    const repos = [
        {path: '/a', name: 'a', is_git: true, current_branch: 'feature/x',
         branches: ['main', 'feature/x'],
         remotes: [{name: 'origin', url: 'https://x/a.git'}, {name: 'upstream', url: 'https://x/u.git'}],
         base_branch: 'feature/x', error: null},
        {path: '/b', name: 'b', is_git: false, current_branch: '', branches: [],
         remotes: [], base_branch: '', error: null},
    ];
    const f = pmDetectFill(repos);
    check('H7a_回填分支与首个远端', f.base_branch === 'feature/x' && f.current_branch === 'feature/x'
        && f.remote === 'origin' && f.url === 'https://x/a.git', JSON.stringify(f));
    check('H7b_远端下拉候选', f.remotes.length === 2 && f.remotes[1].name === 'upstream',
        JSON.stringify(f.remotes));
    check('H7c_逐仓库当前分支行', f.line.includes('a：当前分支 feature/x')
        && f.line.includes('b：非 git 仓库'), f.line);
    check('H7d_非git清单与ok', f.non_git.length === 1 && f.non_git[0] === '/b' && f.ok === true,
        JSON.stringify([f.non_git, f.ok]));
    const none = pmDetectFill([{path: '/c', name: 'c', is_git: false, error: null}]);
    check('H7e_全非git', none.ok === false && none.base_branch === '' && none.remotes.length === 0
        && none.line.includes('非 git 仓库'), JSON.stringify(none));
    const weird = pmDetectFill(null);
    check('H7f_输入异常降级', weird.ok === false && weird.line === '' && weird.remotes.length === 0
        && weird.non_git.length === 0, JSON.stringify(weird));
}

// ---- H8：git_sync 表单组装（枚举透传 / none→off 兼容 / 非法值回落编辑前值 / 默认值） ----
{
    const a = pmSyncForm({before_card: 'pull', on_done: 'auto', diverge: 'stop', timeout: '90'}, {});
    check('H8a_表单值透传', a.before_card === 'pull' && a.on_done === 'auto'
        && a.diverge === 'stop' && a.timeout === 90, JSON.stringify(a));
    const b = pmSyncForm({before_card: 'none', on_done: 'none'}, {});
    check('H8b_none归一off', b.before_card === 'off' && b.on_done === 'off', JSON.stringify(b));
    const c = pmSyncForm({before_card: 'bogus'},
        {before_card: 'fetch', on_done: 'manual', diverge: 'ff-only', timeout: 300});
    check('H8c_非法值回落编辑前值', c.before_card === 'fetch' && c.on_done === 'manual'
        && c.diverge === 'ff-only' && c.timeout === 300, JSON.stringify(c));
    const d = pmSyncForm(null, null);
    check('H8d_默认值', d.before_card === 'off' && d.on_done === 'off'
        && d.diverge === 'overlap-check' && d.timeout === 120, JSON.stringify(d));
}

// ---- H9：凭据 POST body（save/test 判定 / 密码不回填 / ssh / system / 未知类型降级） ----
{
    const save = pmCredentialBody('token', 'me', 'tok', '', 'https://x', false);
    check('H9a_token保存', save.action === 'save' && save.kind === 'token' && save.username === 'me'
        && save.password === 'tok' && save.url === 'https://x' && !('ssh_key' in save), JSON.stringify(save));
    const blank = pmCredentialBody('token', 'me', '', '', '', true);
    check('H9b_已存密码留空只测试', blank.action === 'test' && !('password' in blank), JSON.stringify(blank));
    const ssh = pmCredentialBody('ssh', '', '', '/k/id_ed25519', '', false);
    check('H9c_ssh保存', ssh.action === 'save' && ssh.kind === 'ssh'
        && ssh.ssh_key === '/k/id_ed25519' && !('password' in ssh), JSON.stringify(ssh));
    const sys = pmCredentialBody('system', '', '', '', '', true);
    check('H9d_系统凭据只测试', sys.action === 'test' && sys.kind === 'system', JSON.stringify(sys));
    const weird = pmCredentialBody('weird', '', '', '', '', false);
    check('H9e_未知类型降级token', weird.kind === 'token', JSON.stringify(weird));
}

// ---- H10：凭据状态文案（已保存 / 未配置；永不出现密码） ----
{
    const saved = pmCredentialStateText({configured: true, kind: 'token', username: 'me'});
    check('H10a_已保存文案', saved.includes('已保存') && saved.includes('用户名+令牌') && saved.includes('me'), saved);
    check('H10b_未配置文案', pmCredentialStateText({configured: false}) === '未配置（使用本机 git 配置）',
        pmCredentialStateText({configured: false}));
    check('H10c_空输入', pmCredentialStateText(null) === '未配置（使用本机 git 配置）',
        pmCredentialStateText(null));
}

// ---- H11：单仓库状态文案（分支 / ↑ahead ↓behind / dirty 数 / 远端；null 降级） ----
{
    const text = gitRepoStatusText({name: 'app', branch: 'main', ahead: 2, behind: 1, dirty: 3, remote: 'origin'});
    check('H11a_状态文案', text === 'app：main · ↑2 ↓1 · 3 处未提交 · origin', text);
    const bare = gitRepoStatusText({name: 'app', branch: null, ahead: null, behind: null, dirty: 0, remote: ''});
    check('H11b_null降级', bare === 'app：未知分支（detached） · 无远端跟踪 · 工作区干净 · 未绑定远端', bare);
    check('H11c_错误优先', gitRepoStatusText({name: 'app', error: '权限不足'}) === 'app：权限不足',
        gitRepoStatusText({name: 'app', error: '权限不足'}));
}

// ---- H12：状态条 HTML（拉取/推送按钮 / 转义 / busy 禁用 / 空数据） ----
{
    const html = gitStatusBarHtml(
        [{name: 'app', branch: 'main', ahead: 1, behind: 0, dirty: 0, remote: 'origin', error: null}], '', {});
    check('H12a_含拉取推送按钮', html.includes('data-git-op="pull"') && html.includes('data-git-op="push"')
        && html.includes('拉取') && html.includes('推送'), html);
    check('H12b_状态行', html.includes('app：main · ↑1 ↓0 · 工作区干净 · origin'), html);
    const warn = gitStatusBarHtml([{name: 'app', error: '<bad>'}], '整体失败', {});
    check('H12c_错误与注入转义', warn.includes('&lt;bad&gt;') && !warn.includes('<bad>')
        && warn.includes('整体失败'), warn);
    const busy = gitStatusBarHtml([], '', {busy: true});
    check('H12d_busy禁用按钮', busy.includes('disabled') && busy.includes('data-git-op="push"'), busy);
    const plain = gitStatusBarHtml(null, null, {actions: false});
    check('H12e_无数据无按钮', plain.includes('暂无仓库状态') && !plain.includes('data-git-op'), plain);
}

// ---- H13：push 二次确认文案（项目名 / 仓库清单 / 未提交提醒 / 空数据兜底） ----
{
    const text = gitPushConfirmText('my-project', [{name: 'app'}, {name: 'api'}]);
    check('H13a_确认文案', text.includes('my-project') && text.includes('app、api')
        && text.includes('推送') && text.includes('远端'), text);
    const dirty = gitPushConfirmText('p', [{name: 'app', dirty: 2}]);
    check('H13b_未提交提醒', dirty.includes('未提交改动'), dirty);
    const empty = gitPushConfirmText('', null);
    check('H13c_空数据兜底', empty.includes('当前项目') && empty.includes('全部仓库'), empty);
}

// ---- H14：拉取/推送响应 → toast 文案（成功 / 逐仓库失败 / 冲突指引 / 请求级错误） ----
{
    const ok = gitOpResultText('push',
        {ok: true, repos: [{name: 'app', status: 'pushed', message: '已推送 2 个提交', ahead: 2}]});
    check('H14a_成功文案', ok.ok === true && ok.text.includes('推送完成')
        && ok.text.includes('app：已推送 2 个提交'), JSON.stringify(ok));
    const rej = gitOpResultText('push', {ok: false, repos: [{name: 'app', status: 'rejected', message: '非快进'}]});
    check('H14b_逐仓库失败', rej.ok === false && rej.text.includes('推送失败')
        && rej.text.includes('非快进') && rej.text.includes('请检查'), JSON.stringify(rej));
    const conflict = gitOpResultText('pull',
        {ok: false, repos: [{name: 'app', status: 'conflict', message: '有冲突',
                            conflicts: [{code: 'UU', path: 'a.js'}]}]});
    check('H14c_冲突指引', conflict.ok === false && conflict.text.includes('冲突')
        && conflict.text.includes('请先解决冲突'), JSON.stringify(conflict));
    const reqErr = gitOpResultText('pull', {error: '项目不存在'});
    check('H14d_请求级错误', reqErr.ok === false && reqErr.text.includes('拉取失败')
        && reqErr.text.includes('项目不存在'), JSON.stringify(reqErr));
}

console.log(JSON.stringify({checks: results}));
process.exit(results.every(r => r.pass) ? 0 : 1);
