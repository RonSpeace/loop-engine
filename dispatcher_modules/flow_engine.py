"""流程引擎（配置驱动 + 卡上快照）：流程快照、当前节点、节点类型分派与轮次计数。"""

import json
from pathlib import Path

import prompt_store
from acp_shared import READONLY_TOOL_GUIDE, default_flow_id, enabled_flows, load_project_manifest, pre_role, role_meta, skill_text

from dispatcher_modules.agent_run import _steps_text
from dispatcher_modules.workspace import effective_repos
from dispatcher_modules.runtime import D, log


# 单一源 = flows.json（结构）+ prompts/*.md（提示词正文）；卡上快照 flow/flow_name/nodes_json/node_index
# 一经写入即冻结——改配置（删流程/改名/复用 id/增删节点）不影响进行中卡与历史卡。
# 无快照的历史卡按 mode/phase 懒映射到当前配置（与 backfill_flow_snapshots 同源映射，§4.2），
# 首次采纳流程即写快照冻结，此后推进只读快照。

def flow_of(req):
    """卡所在流程 id：flow 列命中配置 → 采用；否则 default_flow 兜底（存量卡零行为变化）"""
    fid = (req or {}).get("flow")
    return fid if D.flow_by_id(fid) else (default_flow_id() or "standard")

def flow_nodes(flow_id):
    f = D.flow_by_id(flow_id)
    return list(f["nodes"]) if f else []

def _snapshot_raw(req):
    """卡上快照原始解析：([nodes], index)；无/非法 → (None, None)"""
    raw, idx = (req or {}).get("nodes_json"), (req or {}).get("node_index")
    if not raw or idx is None:
        return None, None
    try:
        nodes = json.loads(raw)
        idx = int(idx)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(nodes, list) or not nodes or not 0 <= idx < len(nodes):
        return None, None
    return nodes, idx

def _legacy_position(req):
    """历史卡（无快照）按 mode/phase 懒映射到配置：(flow_id, node_index)；未入流程 → None。
    映射与 backfill_flow_snapshots(§4.2) 同源：auto → auto/0；semi → flow/phase 下标（phase 缺失 → 0）。
    已显式写过 node_index（推进中间态）时优先采信，避免映射把推进结果抹回起点。"""
    def _idx_or(fallback):
        raw = (req or {}).get("node_index")
        try:
            return int(raw) if raw is not None else fallback
        except (TypeError, ValueError):
            return fallback
    mode = (req or {}).get("mode")
    if mode == "auto":
        # auto 流程已随流程目录精简删除（自动通道 #419 后无产生路径：pending_route 无写入方），
        # flow_by_id("auto") 恒为假 → 此处恒取 flow_of(req)（= 兜底默认流程）；
        # 无快照的 mode=auto 卡因此落到默认流程首节点（实测无此类在途卡）
        fid = "auto" if D.flow_by_id("auto") else flow_of(req)
        return fid, _idx_or(0)
    if mode == "semi":
        fid = flow_of(req)
        phase = (req or {}).get("phase")
        if phase:
            for i, n in enumerate(flow_nodes(fid)):
                if n.get("role") == phase:
                    return fid, i
        return fid, _idx_or(0)
    return None

def flow_snapshot(req):
    """卡当前流程位置：(flow_id, nodes, node_index)；前置阶段（未入流程）→ (None, None, None)"""
    nodes, idx = _snapshot_raw(req)
    if nodes is not None:
        return ((req or {}).get("flow"), nodes, idx)
    pos = _legacy_position(req)
    if not pos:
        return (None, None, None)
    fid, idx = pos
    f = D.flow_by_id(fid)
    return (fid, list(f["nodes"]), idx) if f else (None, None, None)

def nodes_of(req):
    """卡当前节点序列（快照优先；历史卡懒映射）；前置阶段 → None"""
    return D.flow_snapshot(req)[1]

def node_index_of(req):
    return D.flow_snapshot(req)[2]

def current_node(req):
    """卡当前节点；前置阶段 → None"""
    _fid, nodes, idx = D.flow_snapshot(req)
    return nodes[idx] if nodes else None

def in_pre_stage(req):
    """卡是否处于前置环节（未入流程、无快照）——前置槽位契约按此判定（与角色名解耦）"""
    return nodes_of(req) is None

def node_display(node):
    """节点显示名：节点自身 name → 角色 display → 角色原文；terminal → 「收尾」"""
    if not node:
        return ""
    if node.get("name"):
        return str(node["name"])
    role = node.get("role")
    if not role:
        return "收尾"
    return role_meta(role, "display") or D.ROLE_NAMES.get(role) or role

def stage_of_review(req, review_role):
    """评审节点角色 → 被评审的前驱节点角色（tech_review→tech / code_review→dev）；无则 None"""
    nodes = nodes_of(req) or flow_nodes(flow_of(req))
    for i, n in enumerate(nodes):
        if n.get("role") == review_role and n.get("node_type") == "review":
            return nodes[i - 1].get("role") if i > 0 else None
    return None

def next_stage(req, phase):
    """phase（节点角色）的下一节点角色；末位（terminal/无后继）→ None"""
    nodes = nodes_of(req) or flow_nodes(flow_of(req))
    for i, n in enumerate(nodes):
        if n.get("role") == phase:
            nxt = nodes[i + 1] if i + 1 < len(nodes) else None
            return nxt.get("role") if nxt and nxt.get("role") else None
    return None

def role_for(req):
    """#419：前置阶段（无快照）→ pre.role（共享槽位，可配）；有快照 → 当前节点角色；
    terminal 节点不拉起 agent → None（收尾由推进链内联/启动扫描/主循环兜底执行，§1.5）"""
    node = D.current_node(req)
    if node is None:
        return pre_role()
    if (node.get("node_type") or "") == "terminal":
        return None
    return node.get("role")

def at_terminal_node(req):
    """收尾判据（#430）：当前节点存在且 node_type == 'terminal'。不可用「无角色」代替——
    配置降级时 role_for 恒为空串，会把未入流程的卡误判成收尾（PATCH done + 合分支不可回滚）。"""
    node = D.current_node(req)
    return bool(node) and (node.get("node_type") or "") == "terminal"

def role_allowed(req, role):
    """role 是否与卡当前节点匹配（#279 现场防复发：dev agent 直接调用 run_agent(req,"accept")
    绕过调度器拉起其它角色会话，会话无收尾、注册表残留 running 僵尸记录）。
    #419：放行 = 卡当前节点角色（无快照 → pre.role，含 reject 打回后指针已回退到前驱的情形）；
    terminal 行不拉起 agent。
    #430：空角色 fail-closed——降级下 role_for 恒为空串，`role == role_for(req)` 会把空角色
    当合法放行。
    #396：merge_resolve 仍按 state["merge_resolve"] 闸门放行（防 #279 式绕过直接拉起）。"""
    if not role:
        return False
    if role == "merge_resolve":
        return str(req["id"]) in D.state.get("merge_resolve", {})
    return role == D.role_for(req)

# 上下文编译器第一期：引擎文案搬进 prompts/system/*.md、prompts/fragments/*.md（提示词存储 prompt_store，
# 本机覆盖 prompts.local/ 优先）。下列模块级常量保留为「默认文本」的兼容视图（测试与 /guide 在用），
# 必须与对应文件逐字节一致（tests/test_prompt_store.py 守护）；实际拼装一律经 prompt_store.read/render。
# #42 前缀缓存：公共尾静态化——常量不再过 str.format（build_prompt 常量直注 {common}），
# 时限数字/步数/结果路径等按卡值全部移到模板 {card_inputs} 之后的卡片输入块；JSON 示例改单层花括号。
# 8 条条目职责与措辞骨架不变（test_common_tail_* 等断言的静态文案全数保留）。
COMMON_TAIL = """
工作约束：
1. 时间：本次运行有每轮时限（具体分钟数见文末卡片输入），超时强制终止；优先最小可用实现，用 `date` 关注实际耗时。时间保底：约剩 5 分钟时先写结果 JSON result.json 保底（路径见文末卡片输入；可覆盖为最终结果）再收尾。
2. 步数：每轮工具调用有步数上限（具体步数见文末卡片输入；调度器强制，达到即取消本轮、只给一次限步收尾）；接近上限同样先写保底。
3. 结束前把结果 JSON 写入 result.json（路径见文末卡片输入）：{"status": "done|blocked|need_confirm", "summary": "做了什么/验证方式及结果/失败原因", "question": "仅 need_confirm 时填"}
   - done = 已完成并验证通过（只读角色 = 产物已写好）；blocked = 无法继续，summary 写原因；need_confirm = 需求有多种解释且影响方向或缺关键信息，带倾向建议追问
   - 可选字段 "todos"（字符串数组）：需人工处理的遗留待办，仅展示不自动执行；合并分支由调度器自动执行，不要声明；无则省略
   - 可选字段 "waive_gate"（字符串）：已完成但收尾校验预计不通过（如实现已由人工提交、分支相对基线无改动）时写豁免理由，非空即转人工裁决；不申报按校验失败处理
   - 可选字段 "options"（仅 need_confirm）：label/hint 文本数组（≤5 个），不得含 action 类字段
   - 可执行命令的角色用脚本或 python json.dumps 生成，禁止手工拼 JSON；只读角色用 Write/fs 工具写入后回读校验；字符串内换行写 \\n，裸换行 = 非法 JSON
4. 结束回合前必须过一遍检查单：a. 没有运行中或等待中的后台任务（含 run_in_background/nohup），有就先终止；b. 关键验证（全量测试、构建、启动自验）已在前台跑完，没跑就前台分块执行，禁止空轮询；c. result.json 已写入并回读可解析。禁止以「等待后台任务通知」结束回合——回合结束即会话结束，后台任务随进程丢失。
5. 重试/续跑场景（有上次会话或残留 result.json）先确认改动完好、无残留 result.json，再继续验证。
6. 状态与会话由调度器管理：不要调用看板 API 改需求状态；不得自行创建/驱动 ACP 会话（import dispatcher 调 run_agent/spawn_agent，或运行 kimi acp/kimi-chat-acp），会被拦截。
7. 本环境是无头 ACP 会话，没有交互终端：AskUserQuestion 等提问类工具会被直接 dismiss，不要使用；需要决策时结束回合，写 status: "need_confirm" + question（带倾向建议）。
8. 路径白名单：只读工具与只读 Bash 的可读 roots = 工作目录 + 项目各真实仓库根（main 工作区，用绝对路径）+ 本需求产物目录，worktree 父目录等其它路径会被拒；查其它需求用 `git worktree list` 或 db_query 查 requirements 表。写工具只能写 worktree、产物目录、/tmp，写主工作区会被取消本轮；目标代码不在 worktree 内时报 need_confirm，说明「需求可能建错项目/项目仓库清单缺配置」。

输出风格：
- 对话回复：结论先行，一般两三百字内；禁止开场白、客套与复述需求；多用 `-` 列表。
- 产物文档（analysis/prd/tech/self-test/accept）：每要点一行，删除过程叙述与铺垫；验收标准、证据清单、跨仓库改动清单必须完整保留，不得因精简缺失。
- result.json summary：结论 + 验证命令与关键结果，`- ` 列表逐行，一般不超过 10 行。
"""

# dev_auto/dev_semi 共享的要求第 2-8 条 + {common} 挂载点（#339 去重：改 dev 约束只改这里，
# 两模板仅开头输入说明与第 1 条范围表述不同）。拼接结果必须与原逐字模板字节一致
# （test_prompt_*/test_common_tail_* 等直接断言 prompt 文本）。
# #419：提示词正文单一源 = prompts/*.md（flows.json roles.<id>.prompt_file 指向，role_prompt() 读取）。
# 本表为派生兼容视图（引用点：/guide 提示词全文、若干测试）+ 内容均取自配置，不构成第二源；
# 新增/改提示词只改 prompts/*.md。dev_auto/dev_semi 是通道时代的旧键，与 dev 同指向 dev.md——
# 仅旧测试按键断言提示词内容时用，guide 只渲染 dev 一块（#419 后 dev 不再按通道拆行）。
ROLE_PROMPTS = {key: D.role_prompt(role) for key, role in (
    ("analysis", "analysis"), ("proposal", "proposal"), ("tech", "tech"),
    ("dev", "dev"), ("dev_auto", "dev"), ("dev_semi", "dev"), ("accept", "accept"),
    ("tech_review", "tech_review"),
    ("code_review", "code_review"), ("merge_resolve", "merge_resolve"),
)}

# #419 前置槽位输出契约：前置环节是全部后置流程共享的槽位（角色可换），强制产出
# suggested_flow（流程 id）与 repos（涉及仓库清单）。契约由槽位承载——build_prompt 在
# 「卡处于前置阶段（无快照）」时追加本段，与角色模板内容/角色名解耦（换 pre.role 也生效）。
# #419：可选流程目录由 _flow_catalog_text() 从配置生成（id｜适用场景｜节点序列），判据不再
# 写死在前置角色提示词里——增删流程/改 description/改节点序列都在配置侧一处生效。
def _flow_catalog_text():
    """可选流程目录（#419）：每条 = id（名称）｜description｜节点序列。
    description 是人工写的适用场景；节点序列是配置派生的硬判据（增删节点自动跟随，永不失真）。
    软禁用：只列启用流程——被软禁用（enabled: false）的流程不进目录，模型看不到也就不会推荐。"""
    lines = []
    for f in enabled_flows():
        seq = " → ".join(node_display(n) for n in (f.get("nodes") or []))
        desc = (f.get("description") or "").strip() or "（未说明适用场景）"
        lines.append(f"- {f['id']}（{f['name']}）｜{desc}｜节点：{seq}")
    return "\n".join(lines) or default_flow_id()


def _flow_reason_text(result, limit=200):
    """前置环节给的选流程理由（#419）：清洗成单行短文本——换行/多空格压成一个空格、截断到 limit 字；
    缺失或非字符串 → 空串（调用方显示「（未给理由）」）。只用于留档展示，不参与任何校验。"""
    raw = (result or {}).get("suggested_flow_reason")
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:limit]


PRE_SLOT_CONTRACT = """
前置环节输出契约（本环节为全部后置流程共享的槽位，要求与角色无关，由调度器强制）：
结束前把结果 JSON 写入 {result_json}，除 status/summary 外必须带：
- "suggested_flow": 推荐的后置流程 id（从下方可选流程中选一条**够用的最省**流程；产出缺失/非法时调度器按默认流程 {default_flow} 兜底并留痕，不阻塞）
- "suggested_flow_reason": 一句话说明命中了所选流程的哪条适用场景（≤200 字，不复述需求内容；可选）
- "repos": 本需求实际涉及改动的仓库目录名数组（名字须取自仓库清单；含仅需读不需改的仓库；识别不出可省略）
可选流程（id（名称）｜适用场景｜节点序列）：
{flow_catalog}
注意：repos 含仓库清单之外的目录名时转人工确认把关（疑似建错项目），不会直接进入开发。
"""


def pre_slot_contract_text(result_json="{result_json}"):
    """前置槽位契约全文：build_prompt 注入与 /guide 展示共用同一份格式化（避免两处各自
    format 后漂移）；可选流程目录取自配置里的启用流程（软禁用流程不出现）。"""
    return prompt_store.render(
        "system/pre-contract",
        result_json=result_json,
        flow_catalog=D._flow_catalog_text(),
        default_flow=default_flow_id())


def stage_input_for(req):
    """#419 输入规则（取代按角色名硬编码的映射与 prd.md 存在性探测）：
    - 前置槽位 = 序列第 0 个节点，输入 = 需求本身（title + notes，模板已内联）；
    - 第 N 个节点输入 = 所有已完成上游节点（第 0..N-1，含前置槽位）产物的「显示名 + 路径」清单；
      只按快照/配置列路径，不做存在性探测、不做内容判断（agent 自行按需读取）。"""
    _fid, nodes, idx = D.flow_snapshot(req)
    art = D.artifact_dir(req)
    pre_art = role_meta(pre_role(), "artifact")
    pre_label = f"{role_meta(pre_role(), 'display') or pre_role()}产物 {art / pre_art}" if pre_art else None
    # 前置槽位（序列第 0 节点）的产物：其输入 = 需求本身（title + notes）
    if nodes is None:
        return "需求本身（title + notes）"
    parts = ["需求本身（title + notes）"]
    if pre_label:
        parts.append(pre_label)
    for n in nodes[:idx]:
        fn = n.get("artifact")
        if fn:
            parts.append(f"{node_display(n)}产物 {art / fn}")
    return " + ".join(parts)

_COMMAND_LABELS = {"install": "安装依赖", "test": "全量测试", "test_affected": "受影响测试", "lint": "lint",
                   "typecheck": "类型检查", "build": "构建"}


def _project_repos(project):
    """项目仓库路径列表（宽容：未知项目 / 旧字符串形态 / 无 paths → []）。"""
    cfg = D.PROJECTS.get(project) if isinstance(D.PROJECTS, dict) else None
    if isinstance(cfg, str):
        return [cfg]
    paths = cfg.get("paths") if isinstance(cfg, dict) else None
    return [str(p) for p in paths] if isinstance(paths, list) else []


def project_conventions_text(project, role):
    """A2 第二层「项目约定」：由各仓库 .loop/project.json 渲染——常用命令、测试实例、按角色过滤的
    指引正文、AGENTS.md 指路；缺项显式写「未配置」，没有清单则明说未提供（A3：不让 agent 套用
    其它项目的命令/端口/脚本）。纯数据拼接，指引正文不过 str.format（花括号安全）。"""
    blocks = []
    for repo in D._project_repos(project):
        m = load_project_manifest(repo)
        if not m["path"]:
            continue
        lines = [f"项目约定（{project}，来源 {m['path']}；与通用说明不一致时以此为准）："]
        cmds = [f"{_COMMAND_LABELS[k]} `{m['commands'][k]}`" for k in _COMMAND_LABELS if k in m["commands"]]
        if cmds:
            lines.append("- 常用命令：" + "；".join(cmds))
        inst = m["instance"]
        if inst:
            parts = [f"{label} `{inst[key]}`" for key, label in
                     (("start", "启动"), ("stop", "停止"), ("status", "状态"), ("healthcheck", "健康检查"))
                     if key in inst]
            if "port_range" in inst:
                parts.append(f"端口段 {inst['port_range'][0]}-{inst['port_range'][1]}")
            lines.append("- 测试实例：" + "｜".join(parts))
        if (Path(repo) / "AGENTS.md").is_file():
            lines.append("- 项目规范：先读工作目录仓库根的 AGENTS.md（改动目录下另有 AGENTS.md 时就近优先）")
        missing = [_COMMAND_LABELS[k] for k in _COMMAND_LABELS if k not in m["commands"]]
        if not inst:
            missing.append("测试实例")
        if missing:
            lines.append("- 未配置：" + "、".join(missing)
                         + "（需要时按仓库 AGENTS.md 约定；没有约定就如实说明，不要假设存在对应脚本或端口）")
        for name, g in m["guides"].items():
            if g["roles"] is None or role in g["roles"]:
                lines.append(f"【项目指引：{name}】\n{g['text']}")
        blocks.append("\n".join(lines))
    if not blocks:
        return prompt_store.render("system/project-missing", project=project)
    return "\n" + "\n\n".join(blocks) + "\n"


def role_skill_names(role):
    """flows.json roles.<id>.skills 声明的技能名（未知角色 / 未配置 → []）。"""
    names = role_meta(role, "skills")
    return list(names) if isinstance(names, list) else []


def scenario_skills_text(project, role):
    """A2 第三层「场景技能」：flows.json 角色声明 + 项目清单 skills.<角色> 点名的技能片段，
    项目技能（.loop/skills/）优先于全局同名（prompts/skills/）；找不到的技能记日志跳过。"""
    repos = D._project_repos(project)
    names = list(D.role_skill_names(role))
    for repo in repos:
        for n in (load_project_manifest(repo)["skills"].get(role) or []):
            if n not in names:
                names.append(n)
    parts = []
    for n in names:
        text = skill_text(n, repos)
        if text is None:
            log.warning(f"项目 {project} 角色 {role} 声明的技能 {n} 未找到（已跳过）")
            continue
        parts.append(f"【场景技能：{n}】\n{text}")
    return ("\n" + "\n\n".join(parts) + "\n") if parts else ""


# #42 前缀缓存：固定段/可变段切分标记（模板独占一行）与固定段占位符白名单。
# 标记之前只允许这四键；{repo_listing}/{repo_map_hint}/{id}/{notes}/{extra}/{merge_ctx} 等按卡值
# 一律放标记之后的卡片输入块（P11 静态扫描强制，防按卡值回流固定段）。
CARD_INPUTS_MARK = "{card_inputs}"
FIXED_PART_PLACEHOLDERS = {"project", "project_goal_hint", "readonly_tool_guide", "common"}


def split_role_template(tmpl):
    """#42：按独占标记 {card_inputs} 把角色模板拆成（固定段, 卡片输入段）。
    标记缺失 → warning + 固定段=整模板、卡片段=空（回退现行为：功能不炸、逐字稳定失效；
    反方向「旧调度器 + 新模板」会在 format 缺 card_inputs 键时抛 KeyError——模板与代码必须同批生效）。"""
    if CARD_INPUTS_MARK not in tmpl:
        log.warning("角色模板缺 {card_inputs} 标记，整段按可变段回退（前缀缓存退化；"
                    "需更新 prompts/<role>.md 并重启调度器）")
        return tmpl, ""
    return tmpl.split(CARD_INPUTS_MARK, 1)


# 条件片段（L3）：注册表 when 声明生效条件，按序选取（顺序 = 搬迁前 if 拼接顺序）
FRAGMENT_ORDER = ("fragment/branch-only-dev", "fragment/branch-only-accept", "fragment/auto-restart")


def project_flags(project):
    """片段 when 条件可引用的项目开关。"""
    cfg = D.PROJECTS.get(project) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {"project.branch_only": bool(cfg.get("company")), "project.restart": bool(cfg.get("restart"))}


def selected_fragments(project, role):
    """按注册表 when 选出本角色在本项目生效的条件片段 id（有序）。"""
    flags = project_flags(project)
    return [fid for fid in FRAGMENT_ORDER if prompt_store.when_matches(fid, role=role, flags=flags)]


def build_prompt_parts(req, role, extra="", *, dry=False):
    """#42 前缀缓存：build_prompt 的拆段核心。返回按序四段：
    [固定段（角色 prose + 静态公共尾）, 项目约定（A2 层2）, 场景技能（A2 层3）, 卡片输入块（可变尾）]。
    逐字稳定的比较单位 = 同一（角色, 项目, 生效清单/技能/约定文件状态）；卡片内容/评审反馈/上游产物/
    运行参数变化只落在卡片输入块。两段模板用同一 kwargs 各自 format（str.format 忽略未用键），
    占位符名与 kwargs 全部不变。前置槽位契约与 kill 红线不在此拼装（build_prompt / run_agent 追加，
    二者含按卡/按进程值，不参与固定段逐字稳定口径）。"""
    # analysis 起全部角色在同一 per-requirement worktree（从主干拉取，基线=主干）工作，
    # 调研基线 = 开发基线，不随主工作区 checkout 漂移；跨阶段幂等复用同一 worktree/分支
    # dry（注入预览）：不建 worktree，按配置预测各仓库 worktree 路径
    if dry:
        repo_map = {Path(p).name: str(D.dev_worktree_path(req, p))
                    for p in (D.PROJECTS[req["project"]].get("paths") or [])}
    else:
        _, repo_map = D.ensure_dev_worktrees(req)
    repo = D.dev_repo_listing(req, repo_map, focus=effective_repos(req))
    # #419：提示词模板来自配置 prompts/<role>.md（单一源；dev_auto/dev_semi 合一为 dev.md）
    tmpl = D.role_prompt(role)
    if not tmpl:
        raise ValueError(f"未知角色: {role}")
    # #419：节点输入 = 上游节点产物清单（含前置槽位），不做角色名映射/不做 prd.md 探测
    stage_input = stage_input_for(req)
    # 条件片段（注册表 when 声明）：仅推分支（dev/accept，#365）、服务自动重启（dev，#318：todos 只声明
    # 纯人工事项）。第一期仍拼进卡片块 extra（与搬迁前逐字节一致）
    for fid in selected_fragments(req["project"], role):
        extra = ((extra + "\n") if extra else "") + prompt_store.read(fid)
    # #396：merge_resolve 冲突上下文注入（冲突仓库清单 + 解决方向）；时间盒覆盖为 15 分钟。
    # 评审整改（主问题 1）：主干引用不再硬编码 main——逐冲突仓库复用 _default_branch 探测
    # （base_branch 配置 → master → main → origin/HEAD，master 优先），注入各仓库具体 merge 目标
    merge_ctx = ""
    if role == "merge_resolve":
        with D.state_lock:
            entry = D.state.get("merge_resolve", {}).get(str(req["id"])) or {}
        failed = entry.get("failed") or []
        cfg = D.PROJECTS[req["project"]]
        lines = []
        for p in failed:
            try:
                base, base_sha = D._default_branch(p, cfg)
            except Exception:
                base, base_sha = None, None
            ref = base or base_sha or "main"  # 探测全面失败回退 main（与原行为一致）
            lines.append(f"- {Path(p).name}（主干: {ref}）：git merge {ref}")
        merge_ctx = prompt_store.render("fragment/merge-resolve", failed=", ".join(failed),
                                        branch_lines="\n".join(lines))
    result_json = D.result_file_path(req)
    art_root = str(D.artifact_dir(req))
    repo_map_path = D.PROJECTS[req["project"]].get("repo_map") or ""
    repo_map_hint = (f"仓库地图（跨仓库定位代码的第一来源，先读再分析）：{repo_map_path}\n"
                     if repo_map_path else "")
    # #5 goal 注入：项目目标进 analysis 上下文（{project_goal_hint} 整行占位符，无 goal 不渲染；
    # goal 是 projects.json 项目级常量，与卡无关 → 留固定段）
    project_goal = (D.PROJECTS.get(req["project"]) or {}).get("goal") or ""
    project_goal_hint = (f"项目目标（goal）：{project_goal}\n" if project_goal else "")
    # 评审反馈直接内联（tech/dev 模板卡片输入块 {review_feedback}）：有 summary（否决原因/历史记录）给全文，
    # 无则显式声明不存在，避免 agent 自行探索数据库找「评审反馈」
    # C4：summary 折叠早期记录后注入；模板已有 {review_feedback} 时 {history} 置空（原 tech/dev 全文注入两遍）
    summary = D.compact_summary(req.get("summary"))
    review_feedback = (f"开发总结（评审反馈/历史记录）：\n{summary}\n" if summary
                       else "无附加反馈（评审通过或尚无否决记录）。")
    history = (f"历史记录（前几次尝试的总结/评审反馈）：{summary}\n"
               if summary and "{review_feedback}" not in tmpl else "")
    # #42：common 常量直注（静态化后不再按卡 format）；时限数字/步数/结果路径由卡片输入块消费
    kw = dict(
        project=req["project"], id=req["id"], title=req["title"],
        notes=req.get("notes") or "（无）",
        repo_listing=repo,
        repo_map_hint=repo_map_hint,
        project_goal_hint=project_goal_hint,
        branch=D.dev_branch(req),
        timeout_minutes=D.effective_timeout_minutes(req, role),
        max_steps=_steps_text(role, req),
        analysis_md=D.artifact_dir(req) / "analysis.md",
        prd_md=D.artifact_dir(req) / "prd.md",
        tech_md=D.artifact_dir(req) / "tech.md",
        self_test_md=D.artifact_dir(req) / "self-test.md",
        accept_md=D.artifact_dir(req) / "accept.md",
        diff_patch=D.artifact_dir(req) / "diff.patch",
        tech_review_md=D.artifact_dir(req) / "tech_review.md",
        code_review_md=D.artifact_dir(req) / "code_review.md",
        artifact_root=art_root,
        result_json=result_json,
        review_feedback=review_feedback,
        stage_input=stage_input,
        history=history,
        extra=(f"补充上下文：{extra}\n" if extra else ""),
        merge_ctx=merge_ctx,
        readonly_tool_guide=prompt_store.read("system/readonly-tools"),
        common=prompt_store.read("system/common"),
        card_inputs="",
    )
    # #42 拆段：两段用同一 kwargs 各自 format（str.format 忽略未用键），占位符语义不变
    fixed_tmpl, card_tmpl = split_role_template(tmpl)
    fixed = fixed_tmpl.format(**kw)
    card = card_tmpl.format(**kw)
    # A2 三层注入顺序保持：固定段（通用协议 + 静态公共尾）→「项目约定」→「场景技能」→ 卡片输入块
    return [fixed,
            project_conventions_text(req["project"], role),
            scenario_skills_text(req["project"], role),
            card]


def build_prompt(req, role, extra=""):
    """提示词正文 = 上下文编译器产物（dispatcher_modules/context.py compile_context）。
    编译器：评审节点代写 diff.patch → 固定段 → 项目约定 → 场景技能 → 卡片输入块 → 前置契约（仅前置阶段），
    同时生成注入清单供执行档案记录。"""
    return D.compile_context(req, role, extra).prompt

