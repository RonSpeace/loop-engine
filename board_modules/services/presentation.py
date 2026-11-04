"""#32 Phase 3：presentation 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import ACP_MODEL
from acp_shared import KIMI, agent_runtime, role_runtime_detail, role_capability
from acp_shared import NODE_TYPE_LABELS
from acp_shared import PLAYWRIGHT_ALLOWED_TOOLS
from acp_shared import READONLY_TOOL_SERVER
from acp_shared import default_flow_id
from acp_shared import flows
from acp_shared import pre_role
from acp_shared import role_meta
from acp_shared import role_model_detail
from pathlib import Path
import html
import json
import os

from board_modules.runtime import resolve
from board_modules.services.model_pool import _model_source_label


def _chat_system_prompt_template():
    """建卡对话提示词展示模板（guide「九、内置 agent 定义」用）：复用 _chat_system_prompt 拼接逻辑
    （展示与函数体逐字一致），仅把 project 与 workdir 占位化为 {project}/{workdir}——
    实际值由建卡会话按绑定项目（_chat_workdir = 项目首仓库路径）填充"""
    return resolve("_chat_system_prompt")("{project}").replace(str(Path.home()), "{workdir}")

# ==================== 操作指南：内置 agent 定义（#269） ====================
# guide 页「内置 agent 定义」版块注入锚点（docs/guide.html 与 docs/guide.en.html 的指南页签末尾）
AGENTS_SECTION_ANCHOR = "<!-- builtin-agents-section -->"

# 总览元数据：角色键 -> (权限档位, 产物展示名兜底)。提示词本体动态取 dispatcher.ROLE_PROMPTS
# （单一事实来源），中文名由 _role_display() 从 flows.json roles.<id>.display 派生，产物名优先
# 取 roles.<id>.artifact——配置里为空的两个角色（dev 的产物契约是代码变更 + self-test.md、
# merge_resolve 无产物契约）在此给展示兜底。
# #419 后不再有通道/阶段两列：通道确认中间态取消、dev 不再按 auto/semi 拆行，角色与流程节点的
# 对应关系由「九、流程」表与各流程节点序列表达（旧 mode/phase 仅历史回放）。
_AGENT_OVERVIEW = {
    "analysis": ("只读：受限工具 + Read/Grep/Glob，Bash 白名单（只读命令放行，其余拒绝）", None),
    "proposal": ("只读：受限工具 + Read/Grep/Glob，Bash 白名单（只读命令放行，其余拒绝）", None),
    "tech": ("只读：受限工具 + Read/Grep/Glob，Bash 白名单（只读命令放行，其余拒绝）", None),
    "dev": ("开发：独立 worktree，可执行命令", "self-test.md + 代码变更"),
    "accept": ("可执行命令（跑测试）；禁止改源码 / git 提交", None),
    "tech_review": ("只读：受限工具 + Read/Grep/Glob，Bash 白名单（只读命令放行，其余拒绝）", None),
    "code_review": ("只读：受限工具 + Read/Grep/Glob，Bash 白名单（只读命令放行，其余拒绝）", None),
    # #403：merge_resolve（#396 自动合并冲突解决）非流程节点角色，finalize_done 合并冲突时调度器拉起；无产物契约
    "merge_resolve": ("受限 git 命令（只读 + add/commit/merge 等），仅 worktree/产物/临时目录内写", "—"),
}


def _role_display(role):
    """guide 展示用角色中文名（#404/#419）：单一源 = flows.json roles.<id>.display，
    未知角色返回原文（防御路径）"""
    return role_meta(role, "display") or role


def _role_artifact_label(role, fallback=None):
    """guide 展示用产物名：配置 artifacts 优先，配置为空时用 _AGENT_OVERVIEW 的兜底展示名"""
    return role_meta(role, "artifact") or fallback or "—"


def _effective_model_label(role, project=None):
    """展示用生效模型 + 来源标注（#419 三层 → #4 四层）：判定唯一源 =
    acp_shared.role_model_detail（项目级 projects.json → roles.<id>.model → 环境变量 → 全局）。
    缺省 project（guide/设置抽屉）不评估项目层；命中项目层标注「（项目级 projects.json）」。"""
    model, source = role_model_detail(role, project)
    try:
        runtime_id, _runtime_source = role_runtime_detail(role, project)
        runtime_label = agent_runtime(runtime_id)["display_name"] + f" ({runtime_id})"
        if runtime_id != "kimi":
            return f"{runtime_label} · 适配器默认模型" + ("（受限角色暂不可用）" if role_capability(role, "readonly")
                                                         or role_capability(role, "bash") != "allow" else "")
    except ValueError as exc:
        return f"运行体配置错误：{exc}"
    return f"{model}{_model_source_label(role, source)} · {runtime_label}"


def _runtime_env_rows():
    """guide「运行时环境与工具」表行（展示层，全部 fail-safe：单项读取失败降级为提示文案，不抛错）。
    数据源 = 本进程环境（run_local.sh 启动时导出的 .env）+ harness home 的 mcp.json。
    内置只读工具为系统自建 MCP（acp_shared.ReadonlyMcpServer，会话级随机端口，零配置），
    工具清单与 ReadonlyMcpServer tools/list 保持同步（此处为展示名清单）"""
    def _mcp_cell():
        home = os.environ.get("KIMI_CODE_HOME") or str(Path.home() / ".kimi-code-deepseek")
        try:
            servers = json.loads((Path(home) / "mcp.json").read_text()).get("mcpServers", {})
            names = sorted(servers)
        except Exception:
            return home, "未配置（只读工具不受影响；dev/accept 浏览器级验证降级为人工验收）"
        if not names:
            return home, "（空）"
        return home, "、".join(n + ("（浏览器验证工具，必配）" if n == "playwright" else "")
                               for n in names)

    harness_home, mcp_cell = _mcp_cell()
    # #23：飞书行改用调度器真实运行态（心跳 + 当前凭据指纹比对），删除仅凭文件可读就输出
    # 「已接飞书」的旧判据——与设置抽屉「通知渠道」tab「飞书机器人」小节共用同一状态模型。
    try:
        # #23：状态文案可能含调度器上报的 last_error（外部文本），整条转义后再进 HTML 单元格
        # （该项无 <code> 标记，整串转义安全；不转义时含 `<` 的原因会拆表）
        feishu = html.escape(_feishu_runtime_text())
    except Exception as e:  # noqa: BLE001 - 展示层 fail-safe
        feishu = html.escape(f"状态读取失败（{e}）")
    readonly_tools = ("list_dir、read_file、db_query、git_log、git_status、git_diff、search_text"
                      "（+ mysql_query，仅项目配了 mysql 时暴露）")
    playwright_tools = "、".join(sorted(PLAYWRIGHT_ALLOWED_TOOLS))
    return [
        ("全局模型 ACP_MODEL",
         f"<code>{html.escape(ACP_MODEL)}</code>（角色未配专属模型时的兜底；"
         "改 .env 的 KIMI_MODEL 后重启生效）"),
        ("kimi CLI 路径", f"<code>{html.escape(str(KIMI))}</code>（KIMI_BIN 可覆盖）"),
        ("harness home",
         f"<code>{html.escape(harness_home)}</code>（KIMI_CODE_HOME 可覆盖；"
         "config.toml 配 provider/API key/模型别名，mcp.json 配 MCP server）"),
        ("MCP server（mcp.json）", mcp_cell),
        (f"内置只读工具（server 名 <code>{READONLY_TOOL_SERVER}</code>，零配置）", readonly_tools),
        (f"playwright 浏览器工具白名单（{len(PLAYWRIGHT_ALLOWED_TOOLS)} 个，前缀放行）",
         f"<code>{html.escape(playwright_tools)}</code>"),
        ("飞书渠道", feishu),
    ]

# 提示词占位符说明表：调度器角色占位符由 dispatcher.build_prompt 按需求/项目填充
# （build_prompt 的 .format 参数即占位符全集），建卡对话占位符由 board 按绑定项目填充
_PLACEHOLDER_ROWS = [
    ("{project}", "项目名（看板 project 字段）", "build_prompt() / board 建卡会话"),
    ("{id}", "看板需求编号", "build_prompt()"),
    ("{title}", "需求标题", "build_prompt()"),
    ("{notes}", "需求详情（notes 字段，无则「（无）」）", "build_prompt()"),
    ("{repo_listing}", "项目仓库清单（路径 + 分支/变更概况）", "build_prompt()：dev_repo_listing()"),
    ("{branch}", "dev 独立 worktree 分支名（f-YYYYMMDD-<编号>）", "build_prompt()：dev_branch()"),
    ("{timeout_minutes}", "单轮运行时限（分钟，默认 240，notes 可传 timeout: 覆盖 15~480）", "build_prompt()：requirement_timeout()；#42 起在卡片输入块消费（静态公共尾不再插值）"),
    ("{analysis_md}", "需求分析产物路径", "build_prompt()：artifact_dir(req)"),
    ("{prd_md}", "产品方案（PRD）产物路径", "build_prompt()：artifact_dir(req)"),
    ("{tech_md}", "技术方案产物路径", "build_prompt()：artifact_dir(req)"),
    ("{self_test_md}", "自测报告产物路径", "build_prompt()：artifact_dir(req)"),
    ("{accept_md}", "验收报告产物路径", "build_prompt()：artifact_dir(req)"),
    ("{artifact_root}", "产物目录（~/.req-board/artifacts/<项目>/req-<编号>/）", "build_prompt()：artifact_dir(req)"),
    ("{result_json}", "结果 JSON 文件路径（结果契约，见「七、产物」）", "build_prompt()：result_file_path()；#42 起在卡片输入块消费（静态公共尾不再插值）"),
    ("{history}", "历史记录（前几次尝试的总结/评审反馈，有则填）", "build_prompt()：req['summary']"),
    ("{review_feedback}", "评审反馈（summary 开发总结全文内联；无则「无附加反馈（评审通过或尚无否决记录）」）", "build_prompt()：req['summary']，仅 dev/tech 模板引用"),
    ("{extra}", "补充上下文（通道确认答复/仅推分支项目说明等，有则填）", "build_prompt()：调用方传入"),
    ("{readonly_tool_guide}", "只读工具使用指南（acp_shared.READONLY_TOOL_GUIDE）", "build_prompt() 注入"),
    ("{repo_map_hint}", "仓库地图提示（项目配置了 repo_map 时点明跨仓库定位第一来源；无则空）", "build_prompt()：repo_map_hint"),
    ("{project_goal_hint}", "项目目标提示（项目配置了 goal 时注入分析上下文，一句「项目目标（goal）：…」；无则整行不渲染）", "build_prompt()：project_goal_hint；board 建卡会话同文案注入"),
    ("{diff_patch}", "dev 完成后的范围 diff 补丁（diff.patch，只读 reviewer 无范围 diff，调度器代写）", "build_prompt()：_gen_review_diff()"),
    ("{tech_review_md}", "方案评审产物路径（tech_review.md）", "build_prompt()：artifact_dir(req)，仅 tech_review 模板"),
    ("{code_review_md}", "代码审查产物路径（code_review.md）", "build_prompt()：artifact_dir(req)，仅 code_review 模板"),
    ("{stage_input}", "阶段输入 = 需求本身 + 上游已完成节点产物清单（按卡上快照派生，含前置槽位；不同流程产物集合不同）", "build_prompt()：stage_input_for()"),
    ("{card_inputs}", "固定段/卡片输入块切分标记（标记前 = 逐字稳定前缀，只允许 {project}/{project_goal_hint}/{readonly_tool_guide}/{common} 四键；标记后 = 本卡专有输入，注入空串）", "build_prompt()：split_role_template() 切分点"),
    ("{common}", "静态公共尾段 COMMON_TAIL（无按卡插值；时限/路径/步数在卡片输入块）", "build_prompt()：common 参数"),
    ("{workdir}", "建卡对话工作目录（项目首仓库路径）", "board 建卡会话：_chat_workdir(project)"),
]


def _flows_table_html():
    """#3：流程表 HTML（guide 与全局设置抽屉共享渲染，单一源；guide 输出逐字不变由 test_guide 锁定）"""
    def esc(s):
        return html.escape(s, quote=False)

    def _node_cell(node):
        role = node.get("role")
        ntype = node.get("node_type") or ""
        label = NODE_TYPE_LABELS.get(ntype, ntype)
        name = node.get("name") or (role_meta(role, "display") if role else "收尾")
        return f"{esc(name)}（{esc(label)}）"

    flow_rows = []
    for f in flows():
        seq = " → ".join(_node_cell(n) for n in (f.get("nodes") or []))
        # 软禁用（enabled: false）的定义仍在本表列出（可一键恢复）；本列即状态列：
        # 默认流程（校验保证必为启用）/ 已启用 / 已禁用
        if f["id"] == default_flow_id():
            mark = "<b>默认</b>"
        elif not f.get("enabled", True):
            mark = "<b>已禁用</b>"
        else:
            mark = "已启用"
        flow_rows.append(
            f'<tr><td><code>{esc(f["id"])}</code></td><td>{esc(f["name"])}</td>'
            f'<td>{esc((f.get("description") or "").strip() or "—")}</td><td>{mark}</td>'
            f'<td>{seq}</td></tr>')
    return ('<table class="flows">\n'
            '<tr><th>id</th><th>名称</th><th>适用场景</th><th>状态</th><th>节点序列（按顺序推进）</th></tr>\n'
            + "\n".join(flow_rows)
            + '\n</table>')


def _runtime_table_html():
    """#3：运行时环境与工具表 HTML（guide 与设置抽屉共享渲染；单项 fail-safe 由 _runtime_env_rows 保证）"""
    def esc(s):
        return html.escape(s, quote=False)

    runtime_rows_html = "\n".join(
        f"<tr><td>{esc(name)}</td><td>{value}</td></tr>" for name, value in _runtime_env_rows())
    return ('<table>\n'
            '<tr><th>项</th><th>当前值（本进程环境实时读取）</th></tr>\n'
            + runtime_rows_html
            + '\n</table>')


def _role_prompt_details_html(prompt_getter):
    """#3：角色提示词折叠块（guide 与设置抽屉共享渲染，单一源）。
    返回 (html, missing)：getter(role) 返回假值（None/空串）的角色跳过并计入 missing，
    展示策略由调用方决定（guide 忽略 missing 维持既有输出；抽屉对 missing 追加降级提示）。"""
    def esc(s):
        return html.escape(s, quote=False)

    blocks, missing = [], []
    for role, (perm, artifact_fallback) in _AGENT_OVERVIEW.items():
        prompt = prompt_getter(role) if prompt_getter else None
        if not prompt:  # ROLE_PROMPTS/配置键变化时跳过，不硬编码块数
            missing.append(role)
            continue
        blocks.append(
            f'<details><summary><code>{esc(role)}</code> {esc(_role_display(role))}'
            f'（产物 {esc(_role_artifact_label(role, artifact_fallback))} · {esc(perm)}）</summary>\n'
            f'<pre>{esc(prompt.strip())}</pre>\n</details>')
    return "\n".join(blocks), missing


def _feishu_runtime_text(status=None):
    """#23/#40 飞书运行态展示文案（guide 运行时环境表与设置抽屉同一状态模型、同一张表）：
    已连接 / 凭据未加载 / 网络连接失败 / 心跳过期 / 显式停用（凭据文件问题另作「凭据未配置」）。
    #40：过期不再另接「状态为最后一次上报」（此时 state 已是 stale）；pid 非 ok 的说明随 reason 透出。"""
    st = status or resolve("_feishu_runtime_status")()
    state = st.get('state')
    reason = st.get('reason')
    if state == 'connected':
        text = f"已连接（调度器长连接已建立，加载自 {st.get('source')}）"
    elif state == 'stale':
        text = f"心跳过期（{reason}）" if reason else "心跳过期"
    elif state == 'failed':
        text = f"网络连接失败（{reason}）" if reason else "网络连接失败"
    elif state == 'not_loaded':
        text = f"凭据未加载（{reason}；点「重启调度器使其生效」）"
    elif state == 'disabled':
        text = (f"显式停用（{reason}）" if 'REQ_BOARD_NO_FEISHU' in (reason or '')
                else f"凭据未配置（{reason}）")
    else:
        text = f"调度器未上报（{reason}）" if reason else "调度器未上报"
    return text


def _builtin_agents_section():
    """生成 guide「九、内置 agent 定义」版块 HTML（#269）。
    提示词单一事实来源：dispatcher.ROLE_PROMPTS/COMMON_TAIL 动态 import 读取，建卡对话提示词
    取 _chat_system_prompt_template()；import 失败时降级为错误提示块，不影响页面其余内容。
    提示词经 html.escape 转义后放入 <pre>（含 <、>、& 的模板文本，防破坏页面结构）。"""
    def esc(s):
        return html.escape(s, quote=False)

    try:
        import dispatcher  # 延迟 import：顶层仅 load_projects 无副作用（#269 风险对策，失败降级）
        role_prompts = dispatcher.ROLE_PROMPTS
        common_tail = dispatcher.COMMON_TAIL
    except Exception as e:  # noqa: BLE001 - 展示层降级：取不到提示词时页面其它内容不受影响
        return (
            '<h2 id="agents">内置 agent 定义</h2>\n'
            '<div class="warn">内置 agent 提示词读取失败（import dispatcher 异常：'
            f'{esc(str(e))}）。请直接查看 dispatcher.py 的 ROLE_PROMPTS / COMMON_TAIL'
            ' 与 board.py 的 _chat_system_prompt。</div>\n'
        )

    # #419：流程与角色按配置（flows.json）渲染，改配置即改本页展示
    # #3：流程表/运行时表/角色提示词块抽取为共享 helper（与全局设置抽屉同码渲染）
    role_rows = []
    for role in sorted(resolve("ROLE_CAPABILITIES"), key=lambda r: (role_meta(r, "order") or 99)):
        # 产物名与下方折叠块同源：配置 artifact 为空时用总览兜底展示名（dev = 代码变更 + 自测报告）
        artifact = _role_artifact_label(role, (_AGENT_OVERVIEW.get(role) or (None, None))[1])
        role_rows.append(
            f'<tr><td><code>{esc(role)}</code></td><td>{esc(_role_display(role))}</td>'
            f'<td>{esc(str(role_meta(role, "order") if role_meta(role, "order") is not None else ""))}</td>'
            f'<td><code>{esc(artifact)}</code></td>'
            f'<td>{esc(_effective_model_label(role))}</td></tr>')
    role_rows_html = "\n".join(role_rows)
    # 角色提示词折叠块（与设置抽屉共享 helper）；PRE_SLOT/COMMON_TAIL/chat 三块为 guide 专有
    role_details_html, _missing = _role_prompt_details_html(role_prompts.get)
    details = [role_details_html] if role_details_html else []
    details.append(
        '<details><summary><code>PRE_SLOT_CONTRACT</code> 前置环节输出契约'
        '（仅卡处于「前置阶段」时由 build_prompt 追加，与角色模板解耦——换 pre.role 也生效；'
        '可选流程目录取自配置的启用流程）</summary>\n'
        f'<pre>{esc(dispatcher.pre_slot_contract_text())}</pre>\n</details>')
    details.append(
        '<details><summary><code>COMMON_TAIL</code> 公共尾段'
        '（build_prompt 拼在各角色提示词末尾的通用约束）</summary>\n'
        f'<pre>{esc(common_tail.strip())}</pre>\n</details>')
    details.append(
        '<details><summary><code>chat</code> 建卡对话 agent'
        '（board.py <code>_chat_system_prompt</code>，按绑定项目填充）</summary>\n'
        f'<pre>{esc(_chat_system_prompt_template().strip())}</pre>\n</details>')

    placeholder_rows = "\n".join(
        f"<tr><td><code>{esc(p)}</code></td><td>{esc(desc)}</td><td>{esc(filler)}</td></tr>"
        for p, desc, filler in _PLACEHOLDER_ROWS)
    details_html = "\n".join(details)

    return f'''<!-- 九、内置 agent 定义（#269 动态生成；#419 流程与角色按 flows.json 配置渲染） -->
<h2 id="agents">内置 agent 定义</h2>
<p>研发流程与角色均为<b>配置数据</b>（仓库根 <code>flows.json</code> + <code>prompts/*.md</code>）：
本节流程表与角色表按当前配置渲染，改配置后需重启看板，刷新本页即见新内容（无热加载）；进行中的卡按卡上流程快照推进，不受配置变更影响。
流程可置 <code>"enabled": false</code> 软禁用：定义保留（改回 true 即恢复），但不再进入前置环节的推荐目录与可选流程列表，模型也不会推荐它。</p>
<h3>流程</h3>
{_flows_table_html()}
<p>前置环节为共享槽位（当前 <code>{esc(pre_role())}</code>），产出 <code>suggested_flow</code>（推荐流程 id）与
<code>repos</code>（涉及仓库）后进入推荐流程首节点；terminal 为调度器收尾动作节点（本地合并项目合并分支 /
仅推分支项目 push f-分支，不拉起 agent）。</p>
<h3>角色</h3>
<table>
<tr><th>角色</th><th>中文名</th><th>顺序</th><th>产物文件</th><th>生效模型（配置 → 环境变量 → 全局）</th></tr>
{role_rows_html}
</table>
<h3>运行时环境与工具</h3>
{_runtime_table_html()}
<p>给角色换模型：改 <code>flows.json</code> 的 <code>roles.&lt;id&gt;.model</code>（或 <code>.env</code> 加
<code>KIMI_MODEL_&lt;角色大写&gt;</code> 做临时覆盖，配置非空时优先配置）；项目级覆盖（仅该项目生效）在项目信息条
最右端的机器人抽屉里调整，保存后下一张卡生效、无需重启。模型别名须在 harness home 的
<code>config.toml</code> 可达（模型中转站可为同一入口配多个 provider 别名，指向不同上游模型），改配置后重启
看板与调度器生效。每个角色可用的工具与权限档位见上方折叠块和本表；MCP 工具配在 harness home 的
<code>mcp.json</code>。</p>
{details_html}
<h3>占位符说明</h3>
<p>提示词中的 <code>{{...}}</code> 占位符在拉起 agent 时填充：调度器角色由
<code>dispatcher.py build_prompt()</code> 按需求与项目填充（如 <code>{{project}}</code> / <code>{{id}}</code>
/ <code>{{notes}}</code> / <code>{{stage_input}}</code> / <code>{{timeout_minutes}}</code> / <code>{{history}}</code>
/ <code>{{extra}}</code>），建卡对话由 board 按会话绑定项目填充（<code>{{project}}</code> / <code>{{workdir}}</code>）。</p>
<table>
<tr><th>占位符</th><th>说明</th><th>填充者</th></tr>
{placeholder_rows}
</table>
'''
