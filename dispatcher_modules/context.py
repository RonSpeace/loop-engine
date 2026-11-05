"""上下文编译器（context-design 第三、四、七节）：把提示词拼装升级为「编译」。

compile_context(req, role, extra, dry=False) → Compiled：
- prompt：发给 agent 的提示词正文（不含进程红线/续跑文案，二者在 run_agent 拼）；
  第一期必须与搬迁前 build_prompt 输出逐字节一致（tests/test_context_compiler.py 冻结夹具守护）。
- items：注入清单，每一项记层、来源、默认/覆盖、内容 hash、字数、入选理由。
- skipped / warnings：未入选项与告警（缺技能、截断、覆盖基于旧默认）。
- prefix：稳定前缀（固定段 + 项目约定 + 场景技能），prefix_hash 用它算。

清单只是旁路记录：正文仍由 flow_engine.build_prompt_parts 拼，清单项由同一套选择逻辑派生，
记录失败不影响正文。dry=True 给注入预览：不建 worktree、不写 diff.patch、不改任何状态。

执行档案（第七节）：每次拉起会话（新会话 / 续跑 / 续接轮）在 <产物目录>/runs/<run_key>/ 写
manifest.json + prompt.txt（实际发出的全文）。
"""
import json
import os
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path

import acp_shared
import prompt_store
from acp_shared import GLOBAL_SKILLS_DIR, MANIFEST_GUIDE_MAX_CHARS, _SKILL_NAME_RE, _repo_rel_file, \
    load_project_manifest, role_meta

from dispatcher_modules.flow_engine import (CARD_INPUTS_MARK, build_prompt_parts, in_pre_stage,
                                             node_index_of, nodes_of, pre_slot_contract_text,
                                             AGENTS_MD_NATIVE_RUNTIMES, fragments_text, selected_fragments,
                                             split_role_template)
from dispatcher_modules.workspace import in_test_env
from dispatcher_modules.runtime import D, log, threading

REAL_ARTIFACTS_ROOT = Path.home() / ".req-board" / "artifacts"
RUNS_DIRNAME = "runs"
_TRUNC_MARK = "已截断，完整内容见"
REWORK_MARK = "返工输入：未关闭问题"


class MissingRequiredContext(Exception):
    """必需件缺失（必需技能缺失/不可用）：不拉会话，卡转暂缓并写明缺什么（fail-closed）。"""

    def __init__(self, names):
        self.names = list(names)
        super().__init__("必需技能缺失或不可用：" + "、".join(self.names))


@dataclass
class Compiled:
    prompt: str
    items: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    prefix: str = ""
    parts: list = field(default_factory=list)


# 最近一次（非 dry）编译结果：(req_id, role) → Compiled。run_agent 据此写执行档案
# （spawn_agent → build_prompt → compile_context → 线程 run_agent，中间不经其它编译）。
_last_lock = threading.Lock()
_last_compiled = {}


def last_compiled(req_id, role, pop=True):
    with _last_lock:
        key = (str(req_id), role)
        return _last_compiled.pop(key, None) if pop else _last_compiled.get(key)


def _item(item_id, layer, title, text, *, origin, src, why, truncated=0, chars=None):
    """清单项；"_text" 只留在内存（续跑补发用），落盘/接口前由 public_items 剥掉。"""
    return {"id": item_id, "layer": layer, "title": title, "src": src, "origin": origin,
            "hash": prompt_store.short_hash(text), "chars": len(text) if chars is None else chars,
            "why": why, "truncated": int(truncated or 0), "_text": text}


def public_items(items):
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]


def _store_item(item_id, layer, why, text=None, *, chars=None):
    """提示词存储条目的清单项（默认/覆盖来源取自存储）。"""
    try:
        meta = prompt_store._entry(item_id)
        title = meta.get("title") or item_id
        org = prompt_store.origin(item_id)
        src = prompt_store.src(item_id)
        if text is None:
            text = prompt_store.read(item_id)
    except Exception:
        title, org, src = item_id, "default", ""
        text = text or ""
    return _item(item_id, layer, title, text, origin=org, src=src, why=why, chars=chars)


def _stale_warnings(item_ids):
    out = []
    for iid in item_ids:
        try:
            if prompt_store.status(iid) == "stale":
                out.append({"code": "stale_override", "id": iid,
                            "message": f"{iid} 的本机覆盖基于旧版默认（默认已更新），编译仍用覆盖"})
        except Exception:
            pass
    return out


def _truncated_by(text, path):
    """_read_capped 截断量：正文带截断标记时按原文件长度计算。"""
    if _TRUNC_MARK not in (text or ""):
        return 0
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
        return max(0, len(raw) - MANIFEST_GUIDE_MAX_CHARS)
    except Exception:
        return 1


def _convention_items(project, role, conv_text):
    """L2 项目约定：goal（projects.json）、各仓库清单框架、按角色入选的指引；无清单 → 兜底文案。"""
    items, warnings = [], []
    guide_chars = 0
    manifests = []
    for repo in D._project_repos(project):
        m = load_project_manifest(repo)
        if not m["path"]:
            continue
        manifests.append(m["path"])
        rname = Path(repo).name
        for name, g in m["guides"].items():
            if g["roles"] is None or role in g["roles"]:
                full = str(Path(repo) / g["file"])
                cut = _truncated_by(g["text"], full)
                piece = f"【项目指引：{name}】\n{g['text']}"
                guide_chars += len(piece) + 1
                gid = f"guide:{rname}/{name}"
                items.append(_item(gid, "L2", f"项目指引 {name}", g["text"], origin="project", src=full,
                                   why=("全部角色" if g["roles"] is None else f"roles 含 {role}"),
                                   truncated=cut, chars=len(piece)))
                if cut:
                    warnings.append({"code": "truncated", "id": gid,
                                     "message": f"指引 {name} 超过 {MANIFEST_GUIDE_MAX_CHARS} 字，截断 {cut} 字（全文 {full}）"})
    if manifests:
        frame = conv_text
        items.insert(0, _item(f"project:{project}/manifest", "L2", "项目约定（.loop/project.json）", frame,
                              origin="project", src="；".join(manifests), why="项目清单",
                              chars=max(0, len(conv_text) - guide_chars)))
    else:
        items.insert(0, _store_item("system/project-missing", "L2", "项目各仓库均无 .loop/project.json",
                                    text=conv_text))
    return items, warnings


def _skill_items(sel, role, claude_native=False):
    """技能清单项（技能 v2 Selection）：always → L4、on_demand → L5（技能目录）、auto/点名 → L6。
    id 一律 skill:<名>（技能库按此前缀统计加载次数）。claude-code 运行体下 L5/L6 技能落到
    .claude/skills/ 由其原生加载，清单项标 native。"""
    items, skipped, warnings = [], [], []

    def add(sk, layer, why, native=False):
        it = _item(f"skill:{sk.name}", layer, f"场景技能 {sk.name}", sk.text,
                   origin=("project" if sk.scope == "project" else "override" if sk.scope == "local" else "default"),
                   src=str(sk.path), why=why, truncated=(sk.truncated or 0),
                   chars=(len(f"【场景技能：{sk.name}】\n") + len(sk.text)) if layer != "L5" else len(sk.description or ""))
        if native:
            it["native"] = True
        items.append(it)
        if sk.truncated:
            warnings.append({"code": "truncated", "id": f"skill:{sk.name}",
                             "message": f"技能 {sk.name} 超过上限，截断 {sk.truncated} 字（全文 {sk.path}）"})
    for sk in sel.always:
        add(sk, "L4", sel.why.get(sk.name) or "always")
    for sk in sel.catalog:
        add(sk, "L5", sel.why.get(sk.name) or "on_demand", native=claude_native)
    for sk, why in sel.card:
        add(sk, "L6", why, native=claude_native)
    for name, why in sel.skipped:
        skipped.append({"id": f"skill:{name}", "why": why})
        if why.startswith("未找到"):
            warnings.append({"code": "missing_skill", "id": f"skill:{name}",
                             "message": f"角色 {role} 声明的技能 {name} 未找到（已跳过）"})
    for name in sel.missing_required:
        warnings.append({"code": "missing_required", "id": f"skill:{name}",
                         "message": f"必需技能 {name} 缺失或不可用（停线）"})
    warnings += list(sel.warnings or [])
    return items, skipped, warnings


def _agents_md_items(files, runtime):
    """嵌套 AGENTS.md（L2）：只指路不内联；清单记内容 hash 与字数（读 agent 实际看到的 worktree 版本）。"""
    native = runtime in AGENTS_MD_NATIVE_RUNTIMES
    items = []
    for f in files:
        try:
            text = Path(f["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        it = _item(f"agents-md:{f['repo']}/{f['rel']}", "L2", f"AGENTS.md {f['show']}", text, origin="project",
                   src=f["path"], why=f["why"])
        if native:
            it["native"] = True
        items.append(it)
    return items


def _card_items(req, role, extra, card_text, merge_text):
    """L6 卡片数据 + L7 运行参数（卡片输入块按来源拆项；剩余为模板框架与运行参数）。"""
    items = []
    used = 0
    title_notes = f"{req.get('title') or ''}\n{req.get('notes') or '（无）'}"
    items.append(_item("card:title-notes", "L6", "标题与需求详情", title_notes, origin="card",
                       src=f"requirements#{req['id']}", why="本卡"))
    used += len(title_notes)
    summary = D.compact_summary(req.get("summary"))
    if summary:
        items.append(_item("card:summary", "L6", "开发总结（评审反馈/历史记录）", summary, origin="card",
                           src=f"requirements#{req['id']}.summary", why="卡片 summary 非空"))
        used += len(summary)
    if not in_pre_stage(req):
        si = D.stage_input_for(req)
        items.append(_item("card:stage-input", "L6", "上游产物指路", si, origin="card",
                           src=str(D.artifact_dir(req)), why=f"流程第 {node_index_of(req)} 个节点"))
        used += len(si)
    if extra and REWORK_MARK in extra:
        # 评审打回带 issues 的返工输入（prompts/system/rework-issues.md）：L6 数据
        items.append(_item("card:rework-issues", "L6", "返工输入：未关闭问题", extra, origin="card",
                           src="评审 result.json issues", why="评审打回"))
        used += len(extra)
    elif extra:
        items.append(_item("card:extra", "L7", "补充上下文", extra, origin="runtime", src="dispatcher",
                           why="调度器传入（失败重试原因等）"))
        used += len(extra)
    if merge_text:
        items.append(_store_item("fragment/merge-resolve", "L7", "冲突解决：按卡填冲突仓库", text=merge_text))
        used += len(merge_text)
    items.append(_item("runtime:card-frame", "L7", "卡片输入框架与运行参数", card_text, origin="engine",
                       src=f"role/{role}（{CARD_INPUTS_MARK} 之后）", why="分支/时限/步数/产物路径",
                       chars=max(0, len(card_text) - used)))
    return items


def _fragment_why(fid):
    try:
        w = prompt_store._entry(fid).get("when") or {}
    except Exception:
        return ""
    bits = []
    if w.get("roles"):
        bits.append("角色 " + "/".join(w["roles"]))
    bits += list(w.get("flags") or [])
    return "when: " + " · ".join(bits) if bits else "恒生效"


def compile_context(req, role, extra="", *, dry=False):
    """编译本次会话的提示词与注入清单。dry=True：注入预览（不建 worktree、不写 diff.patch、不改状态）。"""
    if not dry:
        # 只读评审节点拿不到范围 diff → 调度器代写 diff.patch（上游含 code 节点时才有 diff，§1.4）
        _node = D.current_node(req)
        if _node and _node.get("node_type") == "review":
            if any(n.get("node_type") == "code" for n in (nodes_of(req) or [])[:node_index_of(req) or 0]):
                D._gen_review_diff(req)
    # #42 前缀缓存拼装：固定段 → 项目约定 → 场景技能 → 卡片输入块（可变尾）
    meta = {}
    parts = build_prompt_parts(req, role, extra=extra, dry=dry, _out=meta)
    prompt = "".join(parts)
    contract = ""
    # #419 前置槽位契约：仅当卡处于前置阶段（未入流程、无快照）时追加（按槽位而非角色名判定）
    if in_pre_stage(req):
        contract = pre_slot_contract_text(D.result_file_path(req))
        prompt += contract
    compiled = Compiled(prompt=prompt, parts=list(parts), prefix="".join(parts[:3]))
    try:
        _fill_manifest(compiled, req, role, extra, parts, contract, meta)
    except Exception as e:      # 清单是旁路记录：失败只告警，绝不影响正文
        log.warning(f"需求 #{req.get('id')} 注入清单生成失败（不影响提示词）: {e}")
        compiled.warnings.append({"code": "manifest_error", "id": "", "message": str(e)})
    sel = meta.get("selection")
    if sel is not None and sel.missing_required and not dry:
        raise MissingRequiredContext(sel.missing_required)
    if not dry:
        if meta.get("claude_native") and sel is not None:
            try:
                acp_shared.materialize_for_claude(D.dev_workdir_for(req), sel.loaded())
            except Exception as e:
                log.warning(f"需求 #{req.get('id')} 技能落盘 .claude/skills 失败（不阻断）: {e}")
        with _last_lock:
            _last_compiled[(str(req["id"]), role)] = compiled
    return compiled


def _fill_manifest(compiled, req, role, extra, parts, contract, meta=None):
    tmpl = D.role_prompt(role)
    fixed_tmpl, _card_tmpl = split_role_template(tmpl)
    items, skipped, warnings = [], [], []
    common = prompt_store.read("system/common")
    used_ids = ["system/common", f"role/{role}"]
    items.append(_store_item("system/common", "L0", "全部角色", text=common))
    fixed_chars = len(parts[0]) - len(common)
    if "{readonly_tool_guide}" in fixed_tmpl:
        guide = prompt_store.read("system/readonly-tools")
        items.append(_store_item("system/readonly-tools", "L0", "角色模板引用 {readonly_tool_guide}", text=guide))
        used_ids.append("system/readonly-tools")
        fixed_chars -= len(guide)
    goal = (D.PROJECTS.get(req["project"]) or {}).get("goal") or ""
    if goal and "{project_goal_hint}" in fixed_tmpl:
        hint = f"项目目标（goal）：{goal}\n"
        items.append(_item(f"project:{req['project']}/goal", "L2", "项目目标（goal）", hint, origin="project",
                           src="projects.json", why="项目配置了 goal"))
        fixed_chars -= len(hint)
    role_item = _store_item(f"role/{role}", "L1", "当前节点角色", text=fixed_tmpl, chars=max(0, fixed_chars))
    items.insert(1, role_item)
    frags = selected_fragments(req["project"], role)
    frag_text = fragments_text(req["project"], role)
    conv_text = parts[1][:len(parts[1]) - len(frag_text)] if frag_text and parts[1].endswith(frag_text) else parts[1]
    conv_items, conv_warn = _convention_items(req["project"], role, conv_text)
    items += conv_items
    warnings += conv_warn
    if "system/project-missing" in [i["id"] for i in conv_items]:
        used_ids.append("system/project-missing")
    meta = meta or {}
    items += _agents_md_items(meta.get("agents_md") or [], meta.get("runtime"))
    sel = meta.get("selection")
    sk_items, sk_skipped, sk_warn = (_skill_items(sel, role, meta.get("claude_native"))
                                     if sel is not None else ([], [], []))
    items += sk_items
    skipped += sk_skipped
    warnings += sk_warn
    # L3 条件片段：稳定前缀（项目约定之后）
    for fid in frags:
        items.append(_store_item(fid, "L3", _fragment_why(fid)))
    used_ids += frags
    merge_text = _merge_text_of(parts[3]) if role == "merge_resolve" else ""
    if merge_text:
        used_ids.append("fragment/merge-resolve")
    items += _card_items(req, role, extra, parts[3], merge_text)
    if contract:
        items.append(_store_item("system/pre-contract", "L7", "卡处于前置环节（未入流程）", text=contract))
        used_ids.append("system/pre-contract")
    warnings += _stale_warnings(used_ids)
    compiled.items, compiled.skipped, compiled.warnings = items, skipped, warnings


def _merge_text_of(card_text):
    """merge_resolve 卡片块里的冲突上下文段（按片段首句定位，取到片段末句结束）。"""
    head = "冲突仓库（需解决）："
    i = card_text.find(head)
    if i < 0:
        return ""
    tail = "收尾无需你处理。"
    j = card_text.find(tail, i)
    return card_text[i:(j + len(tail)) if j >= 0 else len(card_text)]


# ==================== 执行档案（runs/<run_key>/manifest.json + prompt.txt） ====================

def new_run_key(role):
    rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{role}-{rnd}"


def runs_dir(req):
    return Path(D.artifact_dir(req)) / RUNS_DIRNAME


def _records_enabled():
    """测试进程且产物根未隔离 → 不写执行档案（与 #420 worktree 短路同一判据思路）。"""
    from dispatcher_modules import workspace
    if not in_test_env():
        return True
    return not (Path(D.ARTIFACTS_ROOT) == REAL_ARTIFACTS_ROOT and D.artifact_dir is workspace.artifact_dir)


def build_manifest(req, role, compiled, sent_text, *, kind, run_key=None, runtime=None, model=None,
                   extra_items=(), context_update=None, parent=None):
    """manifest 字典（不落盘）：compiled 可为 None（编译结果缺失时只记全文 hash 与字数）。"""
    flow = (req or {}).get("flow")
    items = public_items(list(compiled.items) if compiled else [])
    items += public_items(extra_items)
    m = {
        "run_key": run_key or new_run_key(role), "req": req.get("id"), "project": req.get("project"),
        "role": role, "flow": flow, "node_index": node_index_of(req), "kind": kind,
        "runtime": runtime, "model": model or {"id": None, "source": None},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prefix_hash": prompt_store.short_hash(compiled.prefix) if compiled else None,
        "prompt_hash": prompt_store.short_hash(sent_text), "chars": len(sent_text),
        "items": items,
        "skipped": list(compiled.skipped) if compiled else [],
        "warnings": list(compiled.warnings) if compiled else [],
        "context_update": context_update,
    }
    if parent:
        m["parent_run_key"] = parent
    return m


def write_run_record(req, manifest, sent_text):
    """落盘 runs/<run_key>/manifest.json + prompt.txt；返回 manifest 路径（失败/禁用 → None，不阻断会话）。"""
    if not _records_enabled():
        return None
    try:
        d = runs_dir(req) / manifest["run_key"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.txt").write_text(sent_text, encoding="utf-8")
        tmp = d / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, d / "manifest.json")
        return str(d / "manifest.json")
    except Exception as e:
        log.warning(f"需求 #{req.get('id')} 执行档案写入失败（不阻断会话）: {e}")
        return None


def redline_item(pid):
    """红线条目：版本 hash 取模板（PID 每个进程都不同，按渲染文本算 hash 会让续跑每次都误判
    「平台策略已变化」而改开新会话）；字数按实际发出的渲染文本。"""
    text = D.kill_redline_text(pid) if hasattr(D, "kill_redline_text") else prompt_store.render(
        "system/kill-redline", pid=pid)
    return _store_item("system/kill-redline", "L0", "每次发送末尾（本进程 PID）", chars=len(text))


def context_runtime_item(item_id, text, why):
    return _store_item(item_id, "runtime", why, text=text)


def record_session_start(req, role, sent_text, *, kind, session=None, compiled=None, extra_items=(),
                         context_update=None, parent=None, redline=True):
    """run_agent 拉起会话 / 续跑 / 续接轮时调用：写执行档案，返回 {run_key, manifest_path,
    prompt_hash, prefix_hash}（档案禁用时 manifest_path 为 None）。"""
    model = None
    runtime = None
    if session is not None:
        runtime = getattr(session, "runtime_id", None) or "kimi"
        mid = getattr(session, "model_name", None) or getattr(session, "model_requested", None)
        model = {"id": mid if isinstance(mid, str) else None,
                 "source": getattr(session, "model_source", None)
                 if isinstance(getattr(session, "model_source", None), str) else None}
    items = list(extra_items)
    pid = getattr(getattr(session, "proc", None), "pid", None)
    if redline and isinstance(pid, int):
        items.append(redline_item(pid))
    m = build_manifest(req, role, compiled, sent_text, kind=kind, runtime=runtime, model=model,
                       extra_items=items, context_update=context_update, parent=parent)
    sid = getattr(session, "session_id", None)
    if isinstance(sid, str) and sid:
        m["session_id"] = sid
    path = write_run_record(req, m, sent_text)
    return {"run_key": m["run_key"], "manifest_path": path, "prompt_hash": m["prompt_hash"],
            "prefix_hash": m["prefix_hash"], "manifest": m}


# ==================== 续跑补发（context-design 第七节） ====================
# 只比较 L0–L6 的指令/数据项；运行参数（L7 / runtime:*）、补充上下文（每轮由调度器重新给出）、
# 进程红线（PID 每轮不同）不参与比较。
DIFF_LAYERS = ("L0", "L1", "L2", "L3", "L4", "L5", "L6")
DIFF_SKIP_IDS = ("card:extra", "card:rework-issues")
UPDATE_ITEM_MAX_CHARS = 4000


def _diffable(items):
    return {i["id"]: i for i in items or []
            if i.get("layer") in DIFF_LAYERS and i.get("id") not in DIFF_SKIP_IDS}


def previous_manifest(req, role, session_id):
    """该会话上一次的清单（同角色、kind new/resume、session_id 命中，取最新）；找不到 → None。
    存量会话（档案上线前建立、清单无 session_id）一律找不到 → 按现行为发简短续跑文案。"""
    if not session_id:
        return None
    best = None
    try:
        root = runs_dir(req)
        if not root.is_dir():
            return None
        for sub in sorted(root.iterdir()):
            try:
                m = json.loads((sub / "manifest.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            if (isinstance(m, dict) and m.get("role") == role and m.get("kind") in ("new", "resume")
                    and m.get("session_id") == session_id):
                best = m
    except Exception as e:
        log.warning(f"需求 #{req.get('id')} 读取上一份执行档案失败（按无变化处理）: {e}")
        return None
    return best


def resume_plan(req, role, compiled, session_id):
    """续跑前对比本次编译清单与该会话上次清单：
    - none：无上次清单或无变化 → 照旧发简短续跑文案；
    - update：L2–L6 有变化 → 续跑消息前置「上下文更新」块（逐条列出并附新正文，要求回读）；
    - new_session：L0（平台策略）或 L1（角色模板）有变化 → 不续用旧会话，开新会话并附交接说明
      （与飞书助手 prompt_hash 不一致即重建同一口径）。"""
    plan = {"mode": "none", "changed": [], "block": "", "handoff": "", "prev_run_key": None}
    prev = previous_manifest(req, role, session_id)
    if not prev or compiled is None:
        return plan
    plan["prev_run_key"] = prev.get("run_key")
    old = _diffable(prev.get("items"))
    new = _diffable(compiled.items)
    changed = [k for k in new if k not in old or old[k].get("hash") != new[k].get("hash")]
    removed = [k for k in old if k not in new]
    plan["changed"] = changed + removed
    if not plan["changed"]:
        return plan
    layers = {(new.get(k) or old.get(k) or {}).get("layer") for k in plan["changed"]}
    if layers & {"L0", "L1"}:
        reasons = []
        if "L0" in layers:
            reasons.append("平台策略")
        if "L1" in layers:
            reasons.append("角色模板")
        plan["mode"] = "new_session"
        plan["handoff"] = prompt_store.render("system/resume-handoff", role=D.ROLE_NAMES.get(role, role),
                                              reason="、".join(reasons), changed="、".join(plan["changed"]))
        return plan
    blocks = []
    for k in changed:
        it = new[k]
        text = str(it.get("_text") or "")
        if len(text) > UPDATE_ITEM_MAX_CHARS:
            text = text[:UPDATE_ITEM_MAX_CHARS] + f"\n……（超过 {UPDATE_ITEM_MAX_CHARS} 字已截断，完整内容见 {it.get('src')}）"
        if k.startswith("card:"):
            text = f'<card-data source="{k}">{text}</card-data>'
        tag = "新增" if k not in old else "已变更"
        blocks.append(f"--- {tag}：{it.get('title') or k}（{k}，{it.get('layer')}）---\n{text}")
    for k in removed:
        blocks.append(f"--- 已移除：{(old[k].get('title') or k)}（{k}）——不再适用，忽略此前收到的这部分 ---")
    plan["mode"] = "update"
    plan["block"] = prompt_store.render("system/context-update", items="\n".join(blocks))
    return plan
