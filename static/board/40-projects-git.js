        // ==================== #3 项目信息条（GET /api/project-stats） ====================
        // 跟随左导航选中项目：render() 与 autoRefresh() 双挂点刷新；签名短路避免重复写 DOM；
        // 过期响应丢弃（快速切项目防串台）；拉取失败保留上次渲染（fail-safe，不炸主页）。
        let projectBarSig = null;
        function formatTokens(n) {
            const v = (typeof n === 'number' && isFinite(n) && n > 0) ? Math.round(n) : 0;
            if (v < 1000) return String(v);
            if (v < 1000000) return (v / 1000).toFixed(1) + 'k';
            return (v / 1000000).toFixed(2) + 'M';
        }
        function formatSuccessRate(r) {
            if (r === null || r === undefined) return '—';
            const v = Number(r);
            if (!isFinite(v)) return '—';
            return Number((v * 100).toFixed(1)) + '%';
        }
        // 属性位转义：escapeHtml 不转引号，title= 等属性位补引号转义
        function escapeAttr(s) {
            return escapeHtml(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }
        // 统计项口径 tooltip（口径见 PRD：成功率分母含已废弃；打回=reject；总消耗=上下文峰值口径）
        const PB_STAT_TIP = {
            total: '全部状态的需求数（含暂缓 / 已废弃）',
            doing: '状态为「进行中」的需求数',
            done: '状态为「已完成」的需求数',
            rate: '完成 ÷（完成 + 已废弃）；无完结卡时显示 —',
            reject: '评审结论为「打回」（verdict=reject）的计数（按需求归属项目统计）',
            tokens: 'Σ agent_runs.context_tokens（各会话上下文峰值合计）——上下文峰值口径，非完整消耗（P4 换真实计量）'
        };
        function projectBarHtml(d) {
            const st = (d && d.stats) || {};
            const isAll = !d || d.project === 'all';
            const name = isAll ? '所有项目' : displayProjectName(d.project);
            let left = `<div class="pb-name">${escapeHtml(name)}</div>`;
            if (!isAll) {
                const goal = String(d.goal || '').trim();
                left += `<div class="pb-goal">目标：${escapeHtml(goal || '未填写')}</div>`;
            }
            const paths = (d && Array.isArray(d.paths)) ? d.paths : [];
            if (isAll) {
                const cnt = (d && typeof d.project_count === 'number') ? d.project_count : 0;
                left += `<div class="pb-paths">项目数：${cnt}</div>`;
            } else if (paths.length) {
                const allPaths = paths.join(' · ');
                left += `<div class="pb-paths" title="${escapeAttr(allPaths)}">路径：${escapeHtml(allPaths)}</div>`;
            } else {
                left += '<div class="pb-paths">路径：未配置</div>';
            }
            const num = v => (typeof v === 'number' && isFinite(v)) ? String(Math.round(v)) : '0';
            // #21 指标数字配色：非零值才加修饰类（颜色只由皮肤令牌定，令牌块外零字面量）；
            // 零值 / 「0%」 / 「—」解析为 0 或 NaN，均不加类，保持 .pb-stat 中性色
            const stat = (label, value, tip, tone) => {
                const text = String(value);
                const cls = (parseFloat(text) > 0) ? ' ' + tone : '';
                return `<span class="pb-stat${cls}" title="${escapeAttr(tip)}"><b>${escapeHtml(text)}</b>${escapeHtml(label)}</span>`;
            };
            const stats = stat('任务总数', num(st.total), PB_STAT_TIP.total, 'pb-stat-total')
                + stat('进行中', num(st.doing), PB_STAT_TIP.doing, 'pb-stat-doing')
                + stat('完成', num(st.done), PB_STAT_TIP.done, 'pb-stat-done')
                + stat('成功率', formatSuccessRate(st.success_rate), PB_STAT_TIP.rate, 'pb-stat-rate')
                + stat('打回次数', num(st.reject), PB_STAT_TIP.reject, 'pb-stat-reject')
                + stat('总消耗', formatTokens(st.ctx_tokens) + ' tokens', PB_STAT_TIP.tokens, 'pb-stat-tokens');
            // 最右端机器人图标位（#4）：具体项目 = 可点 button（打开模型调参抽屉）；
            // 「所有项目」= 禁用态 + 标题提示（title 与 aria 与 PRD 文案一致）
            const robotIcon = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="8" width="16" height="11" rx="2"/><path d="M12 8V5"/><circle cx="12" cy="4" r="1"/><circle cx="9" cy="13" r="1"/><circle cx="15" cy="13" r="1"/></svg>';
            const robot = isAll
                ? `<span class="pb-robot" aria-disabled="true" aria-label="模型调参" title="模型调参需选中具体项目">${robotIcon}</span>`
                : `<button type="button" class="pb-robot on" aria-label="模型调参" title="模型调参">${robotIcon}</button>`;
            return `<div class="pb-left">${left}</div>`
                + `<div class="pb-right"><div class="pb-stats">${stats}</div>${robot}</div>`;
        }
        function renderProjectBar(d) {
            const html = projectBarHtml(d);
            if (html === projectBarSig) return;
            projectBarSig = html;
            const bar = document.getElementById('projectBar');
            if (bar) bar.innerHTML = html;
        }
        async function refreshProjectBar() {
            const sel = selectedProject;
            const param = sel === ALL_PROJECTS ? 'all' : sel;
            try {
                const res = await fetch('/api/project-stats?project=' + encodeURIComponent(param), {cache: 'no-store'});
                const d = await res.json();
                if (selectedProject !== sel) return;  // 过期响应丢弃（快速切项目防串台）
                renderProjectBar(d);
                // #4 切项目联动：抽屉所开项目与当前项目不一致 → 自动关闭（防串台）
                if (robotProject && robotProject !== sel) closeRobotDrawer();
            } catch (e) { /* 拉取失败保留上次渲染，下一轮再试（fail-safe） */ }
        }

        // ==================== #5.2 项目页 Git 状态条 / 拉取 / 推送 ====================
        // 状态按需加载：首次加载与切换项目各拉一次（不随 15s 轮询刷新，避免无谓拉取）；
        // 拉取 / 推送成功后主动刷新一次。整体失败保留错误文案与按钮，仍允许手动重试。
        let projectGitRepos = null;      // 最近一次 POST /git/status 的 repos
        let projectGitError = '';        // 状态整体错误文案（逐仓库错误在 repos[].error 里）
        let projectGitLoading = false;
        let projectGitBusy = false;

        function renderProjectGitBar() {
            const el = document.getElementById('projectGitBar');
            if (!el) return;
            if (selectedProject === ALL_PROJECTS) {
                el.hidden = true;
                el.innerHTML = '';
                return;
            }
            el.hidden = false;
            if (projectGitLoading && !projectGitRepos) {
                el.innerHTML = '<div class="pb-git"><span class="pb-git-label">Git</span>'
                    + '<span class="pb-git-repo">状态加载中…</span></div>';
                return;
            }
            el.innerHTML = gitStatusBarHtml(projectGitRepos || [], projectGitError, {busy: projectGitBusy});
        }

        async function refreshProjectGitStatus() {
            if (selectedProject === ALL_PROJECTS) {
                projectGitRepos = null;
                projectGitError = '';
                renderProjectGitBar();
                return;
            }
            const sel = selectedProject;
            projectGitLoading = true;
            renderProjectGitBar();
            try {
                const res = await fetch('/api/projects/' + encodeURIComponent(sel) + '/git/status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}',
                });
                const d = await res.json().catch(() => ({}));
                if (selectedProject !== sel) return;   // 过期响应丢弃（快速切项目防串台）
                if (!res.ok) {
                    projectGitRepos = null;
                    projectGitError = d.error || ('状态加载失败（HTTP ' + res.status + '）');
                } else {
                    projectGitRepos = Array.isArray(d.repos) ? d.repos : [];
                    projectGitError = d.ok === false ? '部分仓库状态获取失败，详见上方仓库提示' : '';
                }
            } catch (e) {
                if (selectedProject !== sel) return;
                projectGitRepos = null;
                projectGitError = '状态加载失败：网络错误';
            } finally {
                if (selectedProject === sel) {
                    projectGitLoading = false;
                    renderProjectGitBar();
                }
            }
        }

        async function runProjectGitOp(kind) {
            if (selectedProject === ALL_PROJECTS || projectGitBusy) return;
            const project = selectedProject;
            const push = kind === 'push';
            // push 必须先二次确认；用户取消 = 静默返回（不弹 toast、不发请求）
            if (push && !window.confirm(gitPushConfirmText(project, projectGitRepos || []))) return;
            projectGitBusy = true;
            renderProjectGitBar();
            let res, d;
            try {
                res = await fetch('/api/projects/' + encodeURIComponent(project) + '/git/' + (push ? 'push' : 'pull'), {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(push ? {confirm: true} : {}),
                });
                d = await res.json().catch(() => ({}));
            } catch (e) {
                projectGitBusy = false;
                renderProjectGitBar();
                showToast((push ? '推送' : '拉取') + '失败：网络错误', true);
                return;
            }
            projectGitBusy = false;
            if (selectedProject !== project) { renderProjectGitBar(); return; }
            // 请求级失败（404 项目不存在 / 400 body 非法 / push 缺 confirm）才走这里
            if (!res.ok) {
                const msg = d.error || ('HTTP ' + res.status);
                projectGitError = msg;
                renderProjectGitBar();
                showToast((push ? '推送' : '拉取') + '失败：' + msg, true);
                return;
            }
            const out = gitOpResultText(kind, d);
            projectGitError = out.ok ? '' : out.text;
            renderProjectGitBar();
            showToast(out.text, !out.ok);
            await refreshProjectGitStatus();   // 无论成败都刷新，最终状态以服务端为准
        }

        // ==================== #5 项目管理（新增/编辑/移除接入） ====================
        // 后端三写端点 + GET 预填；pm 纯函数（pmNameValid/pmPathsToArray/projectStatsText/
        // projectRemoveBlocked/projectChecklistHtml）供 node harness 断言，接线函数不纳入 harness。
        let pmEditingName = null;   // null = 新增；字符串 = 编辑中的项目名（键不可改）
        let pmBaseMapping = false;  // 编辑态 base_branch 为 dict 映射形态（#369）→ 只读提示、提交不含该字段
        let pmRemovingName = null;

        // 项目名校验：非空 / ≤maxLen / ^[a-z0-9][a-z0-9-]*$ / 不在既有键集（existing = /api/auto-projects 键）
        function pmNameValid(name, existing, maxLen) {
            const v = String(name == null ? '' : name).trim();
            if (!v || v.length > (maxLen || 64)) return false;
            if (!/^[a-z0-9][a-z0-9-]*$/.test(v)) return false;
            return !(Array.isArray(existing) && existing.includes(v));
        }

        // paths 多行文本 → 数组（按行拆分、trim、去空行）
        function pmPathsToArray(text) {
            return String(text == null ? '' : text).split(/\r?\n/).map(s => s.trim()).filter(Boolean);
        }

        // 移除确认弹窗统计文案：共 N 张 + 各状态计数（已知状态按 PM_STATUS_ORDER 排序、未知状态
        // 追加尾部兜底；标签取后端注入的 STATUS_LABEL，缺失回退原文）
        const PM_STATUS_ORDER = ['doing', 'open', 'later', 'done', 'dropped'];
        function projectStatsText(byStatus) {
            const by = byStatus || {};
            const keys = Object.keys(by).filter(k => (Number(by[k]) || 0) > 0);
            const total = keys.reduce((n, k) => n + (Number(by[k]) || 0), 0);
            const ordered = PM_STATUS_ORDER.filter(s => keys.includes(s))
                .concat(keys.filter(s => !PM_STATUS_ORDER.includes(s)));
            const parts = ordered.map(s => `${(STATUS_LABEL && STATUS_LABEL[s]) || s} ${by[s]}`);
            return `共 ${total} 张${parts.length ? '：' + parts.join(' / ') : ''}`;
        }

        // doing 卡阻断判定（PRD：存在进行中的卡时禁止移除）
        function projectRemoveBlocked(byStatus) {
            return !!((byStatus || {}).doing > 0);
        }

        // 接入成功 checklist：AGENTS.md 协作约定段模板（一键复制）+ kimi 目录信任命令
        function projectChecklistHtml(project, firstPath) {
            const p = String(project || '');
            const agentsMd = [
                '## 需求看板协作约定',
                '',
                '本项目的需求看板运行在 http://127.0.0.1:8899（若未运行可跳过，不要报错）。',
                '',
                '1. 暂缓需求建卡：POST /api/requirements，project 填本项目目录名，status 必须传 "later"。',
                '2. 开始开发标进行中：PATCH 状态为 doing。',
                '3. 完成标已完成：按标题语义匹配后 PATCH 为 done；匹配不确定必须先问用户。',
                `4. 改状态带请求头 X-Actor: agent:${p}:<需求号>。`,
                '5. notes 只写要做什么，不写讨论过程。',
            ].join('\n');
            // kimi 目录信任命令：路径加单引号包裹（含空格安全，本机项目路径即含空格）；内嵌单引号按
            // shell 惯例转义拼接。引号/反斜杠用 fromCharCode 构造（沿 board.py 内嵌 JS 先例）：
            // 三引号转义层与 harness 提取层在此写法下结果一致。
            const SQ = String.fromCharCode(39), BS = String.fromCharCode(92);
            const q = String(firstPath || '').split(SQ).join(SQ + BS + SQ + SQ);
            const cmd = `cd '${q}' && KIMI_CODE_HOME=~/.kimi-code-deepseek kimi`;
            return `<h3>接入成功</h3>
                <p class="pm-check-lead">把协作约定段加进项目 AGENTS.md，并按需完成一次性目录信任：</p>
                <div class="pm-check-block">
                    <div class="pm-check-head"><span>AGENTS.md 协作约定段</span>
                        <button type="button" class="btn" id="pmCopyBtn">复制</button></div>
                    <pre id="pmAgentsMd">${escapeHtml(agentsMd)}</pre>
                </div>
                <div class="pm-check-block">
                    <div class="pm-check-head"><span>kimi 目录信任（一次性）</span></div>
                    <pre>${escapeHtml(cmd)}</pre>
                </div>
                <div class="modal-btns"><button class="btn btn-primary" id="pmDone">完成</button></div>`;
        }

        // ---------- #5.1 Git 配置 / 项目页拉取推送纯函数（node harness 断言；接线函数不纳入 harness） ----------
        // git_sync 三个枚举的规范值（与 dispatcher 消费值逐字一致；「不拉取 / 不推送」统一是 off，非 none）
        const PM_GIT_SYNC_VALUES = {
            before_card: ['off', 'fetch', 'pull'],
            on_done: ['off', 'auto', 'manual'],
            diverge: ['ff-only', 'overlap-check', 'stop'],
        };
        const PM_GIT_SYNC_DEFAULTS = {before_card: 'off', on_done: 'off', diverge: 'overlap-check', timeout: 120};

        // 「选择文件夹」成功后把路径追加进 paths 文本（去空行 / 去重，每行一条绝对路径）
        function pmPathsAppend(text, newPath) {
            const path = String(newPath == null ? '' : newPath).trim();
            const lines = String(text == null ? '' : text).split(/\r?\n/).map(s => s.trim()).filter(Boolean);
            if (path && !lines.includes(path)) lines.push(path);
            return lines.join('\n');
        }

        // git-detect 响应 → 表单回填（首个 git 仓库的当前分支 / 远端 / URL + 每仓库当前分支 + 非 git 提示）
        function pmDetectFill(repos) {
            const rows = Array.isArray(repos) ? repos.filter(r => r && typeof r === 'object') : [];
            const gitRows = rows.filter(r => r.is_git);
            const first = gitRows.length ? gitRows[0] : null;
            const remotes = (first && Array.isArray(first.remotes)) ? first.remotes.filter(r => r && r.name) : [];
            const current = first ? String(first.current_branch || '').trim() : '';
            const base = first ? (String(first.base_branch || '').trim() || current) : '';
            const lines = rows.map(r => {
                const name = String(r.name || r.path || '').trim() || '未命名仓库';
                if (!r.is_git) return `${name}：非 git 仓库（请在路径中填写含 .git 的仓库根目录）`;
                const branch = String(r.current_branch || r.base_branch || '').trim();
                return `${name}：当前分支 ${branch || '未识别'}`;
            });
            const nonGit = rows.filter(r => !r.is_git).map(r => String(r.path || r.name || '').trim()).filter(Boolean);
            const errors = rows.filter(r => r.error).map(r => String(r.error));
            return {
                base_branch: base,
                current_branch: current,
                remote: remotes.length ? String(remotes[0].name) : '',
                url: remotes.length ? String(remotes[0].url || '') : '',
                remotes: remotes.map(r => ({name: String(r.name), url: String(r.url || '')})),
                line: lines.join('\n'),
                non_git: nonGit,
                errors: errors,
                ok: gitRows.length > 0,
            };
        }

        // 同步策略表单值 → git_sync 提交对象（枚举白名单校验；非法/缺失回落编辑前值与默认值；timeout 保序）
        function pmSyncForm(values, base) {
            const v = values || {};
            const b = (base && typeof base === 'object') ? base : {};
            const alias = {none: 'off'};   // 兼容别名：none ≡ off（对外契约统一写 off）
            const norm = x => {
                const s = (x == null) ? '' : String(x);
                return alias[s] || s;
            };
            const pick = key => {
                const allowed = PM_GIT_SYNC_VALUES[key] || [];
                const fromForm = norm(v[key]);
                if (allowed.includes(fromForm)) return fromForm;
                const fromBase = norm(b[key]);
                if (allowed.includes(fromBase)) return fromBase;
                return PM_GIT_SYNC_DEFAULTS[key];
            };
            const rawTimeout = (v.timeout != null && v.timeout !== '') ? v.timeout : b.timeout;
            const t = Math.round(Number(rawTimeout));
            const timeout = (isFinite(t) && t > 0) ? t : PM_GIT_SYNC_DEFAULTS.timeout;
            return {before_card: pick('before_card'), on_done: pick('on_done'), diverge: pick('diverge'), timeout: timeout};
        }

        // 凭据表单 → POST /git/credential body（有新密钥才 save；已配置且留空 = 只 test；密码绝不回填旧值）
        function pmCredentialBody(kind, username, password, sshKey, url, configured) {
            const k = (kind === 'ssh' || kind === 'system') ? kind : 'token';
            const body = {kind: k, url: String(url == null ? '' : url).trim()};
            const user = String(username == null ? '' : username).trim();
            const pw = String(password == null ? '' : password);
            const key = String(sshKey == null ? '' : sshKey).trim();
            if (k === 'token' && user) body.username = user;
            if (k === 'ssh' && key) body.ssh_key = key;
            if (k === 'system') body.action = 'test';                 // 系统凭据无密钥：保存动作退化为连通性测试
            else if (pw || key) body.action = 'save';                 // 有新密钥才 save
            else body.action = configured ? 'test' : 'save';          // 留空且已配置 = 测试；未配置交给后端报缺失
            if (k === 'token' && pw) body.password = pw;
            return body;
        }

        // 凭据状态文案（GET /git/credential 的 state；永不包含密码）
        function pmCredentialStateText(state) {
            const st = state || {};
            if (!st.configured) return '未配置（使用本机 git 配置）';
            const kind = st.kind === 'ssh' ? 'SSH key' : (st.kind === 'system' ? '系统凭据' : '用户名+令牌');
            const who = (st.kind === 'token' && st.username) ? '（' + st.username + '）' : '';
            return '已保存：' + kind + who;
        }

        // 单仓库状态文案：仓库名 + 分支 + ↑ahead ↓behind + 未提交数 + 远端（null 一律降级显示，不抛）
        function gitRepoStatusText(repo) {
            const r = repo || {};
            const name = String(r.name || '').trim();
            const prefix = name ? name + '：' : '';
            if (r.error) return prefix + String(r.error);
            const branch = String(r.branch || r.current_branch || '').trim() || '未知分支（detached）';
            const num = v => (typeof v === 'number' && isFinite(v)) ? Math.max(0, Math.round(v)) : null;
            const ahead = num(r.ahead);
            const behind = num(r.behind);
            const ab = (ahead === null && behind === null)
                ? '无远端跟踪'
                : '↑' + (ahead === null ? '—' : ahead) + ' ↓' + (behind === null ? '—' : behind);
            let dirtyText = '工作区干净';
            if (typeof r.dirty === 'boolean') dirtyText = r.dirty ? '有未提交改动' : '工作区干净';
            else {
                const d = num(r.dirty);
                if (d) dirtyText = d + ' 处未提交';
            }
            const remote = String(r.remote || '').trim() || '未绑定远端';
            return prefix + [branch, ab, dirtyText, remote].join(' · ');
        }

        // 项目页 Git 状态条 HTML（每仓库一行状态 + 拉取 / 推送按钮；errorText 为整体错误，busy 时按钮禁用）
        function gitStatusBarHtml(repos, errorText, opts) {
            const o = opts || {};
            const rows = Array.isArray(repos) ? repos.filter(Boolean) : [];
            const items = rows.map(r => {
                const warn = r.error || r.dirty || (typeof r.ahead === 'number' && r.ahead > 0)
                    || (typeof r.behind === 'number' && r.behind > 0);
                const name = String(r.name || '');
                return `<span class="pb-git-repo${warn ? ' pb-git-warn' : ''}"`
                    + `${name ? ` data-git-repo="${escapeAttr(name)}"` : ''}>${escapeHtml(gitRepoStatusText(r))}</span>`;
            }).join('');
            const err = errorText ? `<span class="pb-git-err">${escapeHtml(errorText)}</span>` : '';
            const busy = !!o.busy;
            const actions = o.actions === false ? '' : '<span class="pb-git-actions">'
                + `<button type="button" class="btn btn-sm" data-git-op="pull"${busy ? ' disabled' : ''} title="拉取当前项目全部仓库（按项目同步策略）">拉取</button>`
                + `<button type="button" class="btn btn-sm" data-git-op="push"${busy ? ' disabled' : ''} title="推送当前项目全部仓库（需二次确认）">推送</button>`
                + '</span>';
            return '<div class="pb-git"><span class="pb-git-label">Git</span>'
                + (items || '<span class="pb-git-repo">暂无仓库状态</span>') + err + actions + '</div>';
        }

        // push 二次确认文案（含项目名 / 仓库清单 / 未提交提醒；confirm=false 时调用方静默返回）
        function gitPushConfirmText(project, repos) {
            const rows = Array.isArray(repos) ? repos.filter(Boolean) : [];
            const list = rows.map(r => String(r.name || '')).filter(Boolean);
            const name = String(project || '').trim() || '当前项目';
            const target = list.length ? list.join('、') : '全部仓库';
            const dirty = rows.some(r => (typeof r.dirty === 'number' && r.dirty > 0) || r.dirty === true);
            return `确认把项目「${name}」的 ${target} 推送到远端？`
                + (dirty ? '\n\n注意：存在未提交改动，未提交内容不会被推送。' : '')
                + '\n\n推送会改写远端分支，请确认本地已通过测试。';
        }

        // 拉取 / 推送响应 → toast 文案（HTTP 200 + ok:false 的逐仓库失败也在此展示，不当请求级错误）
        function gitOpResultText(kind, d) {
            const push = kind === 'push';
            const label = push ? '推送' : '拉取';
            const rows = Array.isArray(d && d.repos) ? d.repos.filter(Boolean) : [];
            if (d && d.error) return {ok: false, text: label + '失败：' + String(d.error)};
            const failedStatus = ['failed', 'rejected', 'conflict', 'behind'];
            const failed = rows.filter(r => failedStatus.includes(String(r.status || '')));
            const conflicts = rows.filter(r => r.status === 'conflict'
                || (Array.isArray(r.conflicts) && r.conflicts.length > 0));
            if (failed.length || conflicts.length) {
                const seen = [];
                const detail = failed.concat(conflicts.filter(r => !failed.includes(r))).map(r => {
                    const nm = String(r.name || '仓库');
                    if (seen.includes(nm)) return '';
                    seen.push(nm);
                    return nm + '：' + String(r.message || r.error || '失败');
                }).filter(Boolean).join('；');
                const guide = conflicts.length ? '请先解决冲突后重试。' : '请检查远端地址 / 凭据与本地改动后重试。';
                return {ok: false, text: label + '失败：' + detail + ' ' + guide};
            }
            if (d && d.ok === false) return {ok: false, text: label + '失败：后端返回失败'};
            const done = rows.map(r => {
                const nm = String(r.name || '仓库');
                return nm + '：' + (String(r.message || '').trim() || String(r.status || '完成'));
            }).join('；');
            return {ok: true, text: label + '完成' + (done ? '：' + done : '')};
        }

        function pmSetError(msg) {
            const el = document.getElementById('pmError');
            el.textContent = msg || '';
            el.hidden = !msg;
        }

        function pmCopyAgentsMd() {
            const el = document.getElementById('pmAgentsMd');
            const text = el ? el.textContent : '';
            const fail = () => showToast('复制失败，请手动选择文本复制', true);
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(() => showToast('已复制'), fail);
            } else {
                fail();
            }
        }

        // ---------- #5.1 Git 配置接线（表单填充 / 选择文件夹 / 探测仓库 / 凭据保存并测试） ----------
        let pmGitBase = {};     // 编辑态项目 git 配置（remote/url/credential/ssh_key）
        let pmSyncBase = {};    // 编辑态 git_sync（用于 timeout 与非法值回落）
        let pmCredState = {configured: false};   // GET /git/credential 的安全视图（永不含密码）

        function pmFillRemoteOptions(names, selected) {
            const sel = document.getElementById('pmRemote');
            const list = [];
            (names || []).forEach(n => {
                const v = String(n || '').trim();
                if (v && !list.includes(v)) list.push(v);
            });
            if (!list.length) list.push('origin');
            if (selected && !list.includes(selected)) list.unshift(selected);
            sel.innerHTML = list.map(n => `<option value="${escapeAttr(n)}">${escapeHtml(n)}</option>`).join('');
            sel.value = (selected && list.includes(selected)) ? selected : list[0];
        }

        function pmSyncCredRows() {
            const kind = document.getElementById('pmCredKind').value;
            document.getElementById('pmCredTokenRow').hidden = kind !== 'token';
            document.getElementById('pmCredSshRow').hidden = kind !== 'ssh';
        }

        function pmApplySyncSelects(sync) {
            const v = pmSyncForm({}, sync);
            document.getElementById('pmSyncBefore').value = v.before_card;
            document.getElementById('pmSyncOnDone').value = v.on_done;
            document.getElementById('pmSyncDiverge').value = v.diverge;
        }

        // 表单 → 项目 git 配置（remote 默认 origin；url 留空不提交；ssh_key 仅 ssh 类型提交）
        function pmCollectGitConfig() {
            const kind = document.getElementById('pmCredKind').value;
            const remote = document.getElementById('pmRemote').value.trim() || 'origin';
            const url = document.getElementById('pmGitUrl').value.trim();
            const sshKey = document.getElementById('pmCredSsh').value.trim();
            const git = {remote: remote, credential: kind, ssh_key: kind === 'ssh' ? sshKey : ''};
            if (url) git.url = url;
            return git;
        }

        // 表单 → git_sync（白名单校验 + timeout 沿用编辑前值，避免覆盖成默认 120）
        function pmCollectSync() {
            return pmSyncForm({
                before_card: document.getElementById('pmSyncBefore').value,
                on_done: document.getElementById('pmSyncOnDone').value,
                diverge: document.getElementById('pmSyncDiverge').value,
            }, pmSyncBase);
        }

        // 凭据状态（只读取「已保存 / 未配置」，服务端永不含密码）
        async function pmLoadCredentialState(name) {
            try {
                const res = await fetch('/api/projects/' + encodeURIComponent(name) + '/git/credential', {cache: 'no-store'});
                const d = await res.json().catch(() => ({}));
                if (pmEditingName !== name) return;   // 弹窗已切换 / 关闭：丢弃旧响应
                if (res.ok && d.state) {
                    pmCredState = d.state;
                    document.getElementById('pmCredState').textContent = pmCredentialStateText(pmCredState);
                }
            } catch (e) { /* 拉取失败保持「未配置」展示；保存时以服务端结果为准 */ }
        }

        // 「选择文件夹」：成功追加一行路径并自动探测；取消（cancelled）静默返回；不支持（501）隐藏按钮并提示手输
        async function pmPickFolder() {
            const btn = document.getElementById('pmPickFolder');
            btn.disabled = true;
            try {
                const res = await fetch('/api/system/pick-folder', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
                });
                const d = await res.json().catch(() => ({}));
                if (res.status === 501 || d.unsupported) {
                    // 当前环境没有系统选择框（Linux 无图形会话/未装 zenity、Docker 等）：隐藏按钮，提示直接输入路径
                    btn.hidden = true;
                    pmSetError(d.error || '当前环境不支持系统文件夹选择，请直接输入仓库绝对路径');
                    document.getElementById('pmPaths').focus();
                    return;
                }
                if (!res.ok) { pmSetError(d.error || '选择文件夹失败'); return; }
                if (d.cancelled || !d.path) return;   // 用户取消：静默
                document.getElementById('pmPaths').value = pmPathsAppend(document.getElementById('pmPaths').value, d.path);
                await pmDetectRepos();   // 路径变化后自动探测
            } catch (e) { pmSetError('网络错误，选择文件夹失败'); }
            finally { btn.disabled = false; }
        }

        // 「探测仓库」：POST git-detect，回填 base_branch / 远端下拉 / URL，逐仓库显示当前分支
        async function pmDetectRepos() {
            const btn = document.getElementById('pmDetect');
            const paths = pmPathsToArray(document.getElementById('pmPaths').value);
            if (!paths.length) { pmSetError('请先填写至少一条仓库绝对路径'); return; }
            btn.disabled = true;
            pmSetError('');
            try {
                const res = await fetch('/api/system/git-detect', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({paths: paths})
                });
                const d = await res.json().catch(() => ({}));
                if (!res.ok) { pmSetError(d.error || '探测仓库失败'); return; }
                const fill = pmDetectFill(d.repos);
                if (fill.base_branch && !pmBaseMapping) {
                    document.getElementById('pmBase').value = fill.base_branch;   // 检测到当前分支
                }
                pmFillRemoteOptions(fill.remotes.map(r => r.name), fill.remote || 'origin');
                if (fill.url) document.getElementById('pmGitUrl').value = fill.url;
                const box = document.getElementById('pmDetectResult');
                box.textContent = fill.line;
                box.hidden = !fill.line;
                box.classList.toggle('warn', !fill.ok);
                if (!fill.ok) pmSetError('未检测到 git 仓库：请确认路径指向含 .git 的仓库根目录');
            } catch (e) { pmSetError('网络错误，探测仓库失败'); }
            finally { btn.disabled = false; }
        }

        // 保存凭据并测试（新增模式在项目创建成功后复用；密码只提交、保存后立即清空输入不回显）
        async function pmSaveCredential(name) {
            if (!name) return {ok: false, message: '项目尚未创建'};
            const body = pmCredentialBody(
                document.getElementById('pmCredKind').value,
                document.getElementById('pmCredUser').value,
                document.getElementById('pmCredToken').value,
                document.getElementById('pmCredSsh').value,
                document.getElementById('pmGitUrl').value,
                !!(pmCredState && pmCredState.configured)
            );
            let res, d;
            try {
                res = await fetch('/api/projects/' + encodeURIComponent(name) + '/git/credential', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
                });
                d = await res.json().catch(() => ({}));
            } catch (e) { return {ok: false, message: '网络错误，凭据保存失败'}; }
            if (!res.ok) return {ok: false, message: d.error || ('凭据保存失败（HTTP ' + res.status + '）')};
            pmCredState = d.state || pmCredState || {configured: false};
            document.getElementById('pmCredState').textContent = pmCredentialStateText(pmCredState);
            document.getElementById('pmCredToken').value = '';   // 密码不回显：保存后立即清空
            const test = d.test || null;
            if (d.ok === false) {
                // save 后自动测试失败仍保凭据（后端语义）：文案区分「已保存但测试失败」
                const tail = (test && test.message) ? '：' + test.message : '';
                return {ok: false, message: (body.action === 'save' ? '凭据已保存，但连通性测试失败' : '凭据测试失败') + tail};
            }
            return {ok: true, message: (test && test.message)
                || (body.action === 'save' ? '凭据已保存并测试通过' : '凭据测试通过')};
        }

        // 「保存并测试」：新增模式先创建项目（创建成功后自动保存凭据）；编辑模式直接保存并测试
        async function pmSaveCredentialAndTest() {
            if (!pmEditingName) { await submitProjectModal(); return; }
            const btn = document.getElementById('pmCredSaveTest');
            btn.disabled = true;
            pmSetError('');
            const project = pmEditingName;
            try {
                // 先把远端 / 凭据类型 PATCH 进项目配置，保证 dispatcher 读到与凭据一致的配置
                let res, d;
                try {
                    res = await fetch('/api/projects/' + encodeURIComponent(project), {
                        method: 'PATCH', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({git: pmCollectGitConfig()}),
                    });
                    d = await res.json().catch(() => ({}));
                } catch (e) { pmSetError('网络错误，保存 Git 配置失败'); return; }
                if (!res.ok) { pmSetError(d.error || '保存 Git 配置失败'); return; }
                const out = await pmSaveCredential(project);
                if (out.ok) showToast(out.message || '凭据已保存并测试通过');
                else pmSetError(out.message);
            } finally { btn.disabled = false; }
        }

        // 打开新增（name 空）或编辑模态：编辑先 GET 预填（失败不打开，fail-closed）
        async function openProjectModal(name) {
            // 防御：看板进程未重启时旧模板没有 Git 表单节点 → 明确提示并放弃，避免半初始化异常
            if (!document.getElementById('pmRemote')) {
                showToast('项目 Git 配置界面需重启看板后生效，请稍后再试', true);
                return;
            }
            pmEditingName = name || null;
            pmBaseMapping = false;
            pmGitBase = {};
            pmSyncBase = {};
            pmCredState = {configured: false};
            pmSetError('');
            document.getElementById('projectModalForm').hidden = false;
            document.getElementById('projectModalChecklist').hidden = true;
            document.getElementById('projectModalTitle').textContent = pmEditingName ? '编辑项目' : '新增项目';
            document.getElementById('pmSubmit').textContent = pmEditingName ? '保存' : '创建';
            const nameEl = document.getElementById('pmName');
            const pathsEl = document.getElementById('pmPaths');
            const baseEl = document.getElementById('pmBase');
            const goalEl = document.getElementById('pmGoal');
            nameEl.disabled = !!pmEditingName;
            baseEl.disabled = false;
            document.getElementById('pmBaseHint').hidden = true;
            // Git 表单复位（新增 = 默认值；编辑 = 下方预填覆盖）
            pmFillRemoteOptions(['origin'], 'origin');
            document.getElementById('pmGitUrl').value = '';
            document.getElementById('pmCredKind').value = 'system';
            document.getElementById('pmCredUser').value = '';
            document.getElementById('pmCredToken').value = '';
            document.getElementById('pmCredSsh').value = '';
            document.getElementById('pmCredState').textContent = '未配置';
            pmSyncCredRows();
            pmApplySyncSelects({});
            const detectBox = document.getElementById('pmDetectResult');
            detectBox.textContent = '';
            detectBox.hidden = true;
            detectBox.classList.remove('warn');
            if (!pmEditingName) {
                nameEl.value = '';
                pathsEl.value = '';
                baseEl.value = '';
                goalEl.value = '';
            } else {
                let d = null;
                try {
                    const res = await fetch('/api/projects/' + encodeURIComponent(pmEditingName), {cache: 'no-store'});
                    d = await res.json().catch(() => ({}));
                    if (!res.ok) { showToast(d.error || '加载项目失败', true); return; }
                } catch (e) { showToast('加载项目失败', true); return; }
                nameEl.value = pmEditingName;
                pathsEl.value = (d.paths || []).join('\n');
                if (d.base_branch && typeof d.base_branch === 'object') {
                    pmBaseMapping = true;   // 映射形态：不进输入框、提交不含该字段
                    baseEl.value = '';
                    baseEl.disabled = true;
                    document.getElementById('pmBaseHint').hidden = false;
                } else {
                    baseEl.value = d.base_branch || '';
                }
                goalEl.value = d.goal || '';
                // Git 远端 / 凭据类型 / 同步策略预填（字段缺失一律回落默认值）
                const git = (d.git && typeof d.git === 'object') ? d.git : {};
                pmGitBase = {remote: git.remote || 'origin', url: git.url || '',
                             credential: git.credential || 'system', ssh_key: git.ssh_key || ''};
                pmFillRemoteOptions([pmGitBase.remote], pmGitBase.remote);
                document.getElementById('pmGitUrl').value = pmGitBase.url || '';
                const credKind = ['system', 'token', 'ssh'].includes(pmGitBase.credential)
                    ? pmGitBase.credential : 'system';
                document.getElementById('pmCredKind').value = credKind;
                document.getElementById('pmCredSsh').value = pmGitBase.ssh_key || '';
                pmSyncCredRows();
                pmSyncBase = (d.git_sync && typeof d.git_sync === 'object') ? d.git_sync : {};
                pmApplySyncSelects(pmSyncBase);
                pmLoadCredentialState(pmEditingName);   // 只显示「已保存 / 未配置」，不回显密码
            }
            document.getElementById('projectModal').classList.add('show');
        }

        function closeProjectModal() {
            document.getElementById('projectModal').classList.remove('show');
            pmEditingName = null;
            pmBaseMapping = false;
            pmGitBase = {};
            pmSyncBase = {};
            pmCredState = {configured: false};
        }

        async function submitProjectModal() {
            pmSetError('');
            const name = document.getElementById('pmName').value.trim();
            const paths = pmPathsToArray(document.getElementById('pmPaths').value);
            const base = document.getElementById('pmBase').value.trim();
            const goal = document.getElementById('pmGoal').value.trim();
            if (!pmEditingName && !pmNameValid(name, autoProjects, 64)) {
                pmSetError('项目名非法或已存在：仅小写字母/数字/连字符、以字母或数字开头、≤64 字符、不可重名');
                return;
            }
            if (!paths.length) { pmSetError('请填写至少一条仓库绝对路径（每行一条）'); return; }
            const body = {paths: paths, goal: goal, git: pmCollectGitConfig(), git_sync: pmCollectSync()};
            // base_branch 留空 = 不提交该键：新增走后端默认 main；编辑保持原值（不可清空，恢复自动探测走文件专家路径）
            if (!pmBaseMapping && base) body.base_branch = base;
            let res, d;
            try {
                res = await fetch(pmEditingName ? '/api/projects/' + encodeURIComponent(pmEditingName) : '/api/projects', {
                    method: pmEditingName ? 'PATCH' : 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(pmEditingName ? body : Object.assign({name: name}, body)),
                });
                d = await res.json().catch(() => ({}));
            } catch (e) { pmSetError('网络错误，提交失败'); return; }
            if (!res.ok) { pmSetError(d.error || '提交失败'); return; }
            if (pmEditingName) {
                closeProjectModal();
                showToast('已保存');
                refreshProjectBar();        // goal/paths 信息条即时更新
                refreshProjectGitStatus();  // 远端 / 凭据类型变更后状态条同步刷新
                return;
            }
            // 创建成功（后续凭据失败不回滚、不丢项目）→ 保存凭据 → 切 checklist 视图
            const created = (d && d.project) || name;
            const kind = document.getElementById('pmCredKind').value;
            const hasSecret = document.getElementById('pmCredToken').value || document.getElementById('pmCredSsh').value;
            let credMsg = '';
            if (kind !== 'system' || hasSecret) {
                const out = await pmSaveCredential(created);
                if (out.ok) credMsg = out.message || '凭据已保存并测试通过';
                else {
                    credMsg = '凭据保存失败：' + out.message + '（项目已创建，可在编辑项目中重试）';
                    showToast(credMsg, true);
                }
            }
            document.getElementById('projectModalForm').hidden = true;
            const box = document.getElementById('projectModalChecklist');
            box.innerHTML = '<div class="pm-check">' + projectChecklistHtml(name, paths[0])
                + (credMsg ? `<div class="pm-check-note">${escapeHtml(credMsg)}</div>` : '')
                + '<div class="pm-check-note">新项目已热接入，关闭后出现在左列表（无需重启看板）。</div></div>';
            box.hidden = false;
            document.getElementById('pmCopyBtn').addEventListener('click', pmCopyAgentsMd);
            document.getElementById('pmDone').addEventListener('click', async () => {
                closeProjectModal();
                await renderNav();
            });
        }

        // 打开移除确认模态：拉该项目需求自行聚合计数（不扩 /api/project-stats；拉取失败禁用移除）
        async function openRemoveProjectModal(name) {
            pmRemovingName = name || null;
            document.getElementById('removeProjectName').textContent = displayProjectName(name);
            document.getElementById('removeProjectStats').textContent = '正在统计…';
            document.getElementById('removeProjectWarn').hidden = true;
            document.getElementById('removeProjectOk').disabled = true;
            document.getElementById('removeProjectModal').classList.add('show');
            try {
                const res = await fetch('/api/requirements?project=' + encodeURIComponent(name), {cache: 'no-store'});
                const rows = await res.json();
                if (pmRemovingName !== name) return;   // 过期响应丢弃（快速切换防串台）
                if (!res.ok || !Array.isArray(rows)) throw new Error('stats failed');
                const by = {};
                rows.forEach(r => { by[r.status] = (by[r.status] || 0) + 1; });
                document.getElementById('removeProjectStats').textContent = projectStatsText(by);
                if (projectRemoveBlocked(by)) {
                    document.getElementById('removeProjectWarn').hidden = false;
                } else {
                    document.getElementById('removeProjectOk').disabled = false;
                }
            } catch (e) {
                if (pmRemovingName !== name) return;
                document.getElementById('removeProjectStats').textContent = '统计失败，已禁用移除（可稍后重试）';
            }
        }

        function closeRemoveProjectModal() {
            document.getElementById('removeProjectModal').classList.remove('show');
            pmRemovingName = null;
        }

        async function confirmRemoveProject() {
            if (!pmRemovingName) return;
            const name = pmRemovingName;
            const okBtn = document.getElementById('removeProjectOk');
            okBtn.disabled = true;
            let res, d;
            try {
                res = await fetch('/api/projects/' + encodeURIComponent(name), {method: 'DELETE'});
                d = await res.json().catch(() => ({}));
            } catch (e) { showToast('网络错误，移除失败', true); okBtn.disabled = false; return; }
            if (res.status === 409) {   // doing 阻断：前端统计后状态变化才会走到这里
                document.getElementById('removeProjectWarn').hidden = false;
                showToast(d.error || '存在进行中的卡，已阻断', true);
                return;
            }
            if (!res.ok && res.status !== 404) {
                showToast(d.error || '移除失败', true);
                okBtn.disabled = false;
                return;
            }
            closeRemoveProjectModal();
            showToast('已移除接入');
            await renderNav();
            render();
            refreshProjectGitStatus();   // #5.2 移除后选中项可能回退「所有项目」：状态条随之隐藏/切换
        }

