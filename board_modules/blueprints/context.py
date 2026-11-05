"""context 蓝图：上下文编译器的看板入口（context-design 第六~八节）。

- 执行档案：GET /api/requirements/<id>/context/runs[/<run_key>[/prompt]]（读产物目录 runs/*/manifest.json，
  状态关联 agent_runs.manifest_path）。
- 注入预览：GET /api/context/preview（子进程跑 scripts/context_preview.py，与调度器同一编译器 dry 编译）。
- 提示词：GET /api/prompts、GET/PUT/DELETE /api/prompts/<id>（本机覆盖 prompts.local/，写操作仅限本机来源）。
  项目约定条目 project/<项目>/<仓库>/... 只读展示（改动属于项目仓库主干，第一期不在页面上改）。
"""


from board_modules.runtime import resolve


import json
import re
from pathlib import Path


from flask import Blueprint, Response, jsonify, request


import prompt_store
from acp_shared import agents_md_all, load_project_manifest
from board_modules.common import artifact_candidates


bp = Blueprint("context", __name__)


RUN_KEY_RE = re.compile(r"^\d{8}-\d{6}-[a-z_]{1,40}-[a-z0-9]{4}$")


# ==================== 执行档案 ====================

def _req_row(req_id):
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT * FROM requirements WHERE id = ?", (req_id,)).fetchone()
    return dict(row) if row else None


def _runs_dirs(project, req_id):
    """runs 目录候选（当前产物根优先，legacy 根其后）；只返回存在的目录。"""
    out = []
    for _root, path, _src in artifact_candidates(project, req_id, "runs"):
        if path.is_dir():
            out.append(path)
    return out


def _load_manifests(project, req_id):
    """run_key → (manifest dict, 目录)；同 run_key 多根时当前根优先。坏文件跳过。"""
    out = {}
    for d in _runs_dirs(project, req_id):
        for sub in d.iterdir():
            if sub.name in out or not RUN_KEY_RE.match(sub.name):
                continue
            try:
                m = json.loads((sub / "manifest.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 档案坏了不阻断列表
                continue
            if isinstance(m, dict):
                out[sub.name] = (m, sub)
    return out


def _run_statuses(req_id):
    """agent_runs：manifest_path → status（未结束的会话无行 → 调用方按 running 处理）。"""
    with resolve("get_db")() as conn:
        rows = conn.execute("SELECT manifest_path, status FROM agent_runs WHERE req_id = ? "
                            "AND manifest_path IS NOT NULL", (req_id,)).fetchall()
    return {r["manifest_path"]: r["status"] for r in rows}


def _status_of(run_key, manifests, statuses):
    m, d = manifests[run_key]
    path = str(d / "manifest.json")
    if path in statuses:
        return statuses[path]
    parent = m.get("parent_run_key")
    if parent and parent in manifests:
        pm, pd = manifests[parent]
        return statuses.get(str(pd / "manifest.json"), "running")
    return "running"


def _summary(run_key, m, status):
    model = m.get("model") or {}
    return {"run_key": run_key, "role": m.get("role"), "kind": m.get("kind"), "created_at": m.get("created_at"),
            "status": status, "chars": m.get("chars"), "prompt_hash": m.get("prompt_hash"),
            "prefix_hash": m.get("prefix_hash"), "warnings": len(m.get("warnings") or []),
            "model": model.get("id") if isinstance(model, dict) else model, "runtime": m.get("runtime"),
            "node_index": m.get("node_index"), "flow": m.get("flow"),
            "context_update": m.get("context_update")}


def manifest_diff(prev, cur):
    """两份清单按条目 id 对比：hash 变了 = changed；新出现 = added；消失 = removed。"""
    a = {i.get("id"): i.get("hash") for i in (prev or {}).get("items") or [] if isinstance(i, dict)}
    b = {i.get("id"): i.get("hash") for i in (cur or {}).get("items") or [] if isinstance(i, dict)}
    return {"changed": [k for k in b if k in a and a[k] != b[k]],
            "added": [k for k in b if k not in a],
            "removed": [k for k in a if k not in b]}


@bp.route('/api/requirements/<int:req_id>/context/runs', methods=['GET'])
def api_context_runs(req_id):
    """本卡全部执行档案（新 → 旧）。"""
    req = _req_row(req_id)
    if not req:
        return jsonify({"error": "需求不存在"}), 404
    manifests = _load_manifests(req["project"], req_id)
    statuses = _run_statuses(req_id)
    runs = [_summary(k, manifests[k][0], _status_of(k, manifests, statuses))
            for k in sorted(manifests, reverse=True)]
    return jsonify({"runs": runs})


def _manifest_or_404(req_id, run_key):
    if not RUN_KEY_RE.match(run_key or ""):
        return None, None, (jsonify({"error": "run_key 非法"}), 400)
    req = _req_row(req_id)
    if not req:
        return None, None, (jsonify({"error": "需求不存在"}), 404)
    manifests = _load_manifests(req["project"], req_id)
    if run_key not in manifests:
        return None, None, (jsonify({"error": "执行档案不存在"}), 404)
    return manifests, req, None


@bp.route('/api/requirements/<int:req_id>/context/runs/<run_key>', methods=['GET'])
def api_context_run(req_id, run_key):
    """单份清单 + 同角色上一轮 run_key + 与上一轮的条目差异。"""
    manifests, _req, err = _manifest_or_404(req_id, run_key)
    if err:
        return err
    m, _d = manifests[run_key]
    same_role = sorted(k for k, (mm, _) in manifests.items() if mm.get("role") == m.get("role") and k < run_key)
    prev_key = same_role[-1] if same_role else None
    out = dict(m)
    out["status"] = _status_of(run_key, manifests, _run_statuses(req_id))
    out["prev_run_key"] = prev_key
    out["diff"] = manifest_diff(manifests[prev_key][0] if prev_key else None, m) if prev_key else \
        {"changed": [], "added": [], "removed": []}
    return jsonify(out)


@bp.route('/api/requirements/<int:req_id>/context/runs/<run_key>/prompt', methods=['GET'])
def api_context_run_prompt(req_id, run_key):
    """实际发出的提示词全文（text/plain）。"""
    manifests, _req, err = _manifest_or_404(req_id, run_key)
    if err:
        return err
    _m, d = manifests[run_key]
    try:
        text = (d / "prompt.txt").read_text(encoding="utf-8")
    except OSError:
        return jsonify({"error": "prompt.txt 不存在"}), 404
    return Response(text, mimetype="text/plain; charset=utf-8")


# ==================== 注入预览 ====================

@bp.route('/api/context/preview', methods=['GET'])
def api_context_preview():
    """注入预览：?project=&role=&req=（req 可选；给了 req 而没给 project 时取卡片所在项目）。"""
    role = (request.args.get("role") or "").strip()
    project = (request.args.get("project") or "").strip()
    req_arg = (request.args.get("req") or "").strip()
    req = None
    if req_arg:
        if not req_arg.isdigit():
            return jsonify({"error": "req 必须是需求编号"}), 400
        req = _req_row(int(req_arg))
        if not req:
            return jsonify({"error": "需求不存在"}), 404
        project = project or req["project"]
        if req["project"] != project:
            return jsonify({"error": "需求不属于该项目"}), 400
        req = {k: req.get(k) for k in ("id", "project", "title", "notes", "summary", "created_at", "flow",
                                       "flow_name", "nodes_json", "node_index", "mode", "phase")}
    if not role or not project:
        return jsonify({"error": "project 与 role 必填"}), 400
    code, payload = resolve("_context_preview_run")(project, role, req)
    return jsonify(payload), code



# ==================== 提示词 ====================

def _write_guard():
    """写操作守卫（单一入口）：统一走 board_modules.common.write_guard。返回 None = 放行，否则 (响应, 状态码)。"""
    from board_modules.common import write_guard
    return write_guard()


def _project_prompt_items():
    """项目约定只读条目：project/<项目>/<仓库>/manifest、project/<项目>/<仓库>/guide/<名>、
    project/<项目>/agents-md/<仓库>/<相对路径>（仓库内全部 AGENTS.md）。"""
    out = []
    for project, paths in sorted((resolve("_projects_config")() or {}).items()):
        for repo in paths:
            rname = Path(repo).name
            # 仓库内全部 AGENTS.md（根 + 子目录）：只读，提示词里只指路不内联
            for rel in agents_md_all(repo):
                full = str(Path(repo) / rel)
                out.append({"id": f"{prompt_store.PROJECT_PREFIX}{project}/agents-md/{rname}/{rel}", "layer": "L2",
                            "title": f"{project} · {rname}/{rel}", "file": full, "src": full, "readonly": True,
                            "status": "project", "kind": "project",
                            "used_by": ("全部角色（项目约定层指路）" if rel == "AGENTS.md"
                                        else "改动落在该目录下的卡（卡片输入块指路）"),
                            "when": None, "placeholders": [], "required": [], "locked": []})
            m = load_project_manifest(repo)
            if not m["path"]:
                continue
            base = f"{prompt_store.PROJECT_PREFIX}{project}/{rname}"
            out.append({"id": f"{base}/manifest", "layer": "L2", "title": f"{project} · {rname} 项目清单",
                        "file": m["path"], "src": m["path"], "readonly": True, "status": "project",
                        "kind": "project", "used_by": f"项目 {project} 全部角色", "when": None,
                        "placeholders": [], "required": [], "locked": []})
            for name, g in m["guides"].items():
                out.append({"id": f"{base}/guide/{name}", "layer": "L2", "title": f"{project} · 指引 {name}",
                            "file": str(Path(repo) / g["file"]), "src": str(Path(repo) / g["file"]),
                            "readonly": True, "status": "project", "kind": "project",
                            "used_by": ("全部角色" if g["roles"] is None else "角色 " + "/".join(g["roles"])),
                            "when": None, "placeholders": [], "required": [], "locked": []})
    return out


def _project_item_detail(item_id):
    for it in _project_prompt_items():
        if it["id"] == item_id:
            try:
                text = Path(it["src"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            d = dict(it)
            d.update(default_text=text, override_text=None, text=text, base_text=None)
            return d
    return None


@bp.route('/api/prompts', methods=['GET'])
def api_prompts():
    """可编辑条目（引擎文案 + 角色模板，带默认/覆盖/默认已更新状态）+ 只读项目约定条目。"""
    items = [dict(it, readonly=False) for it in prompt_store.items()]
    return jsonify({"items": items + _project_prompt_items(),
                    "local_dir": str(prompt_store.local_dir())})


@bp.route('/api/prompts/<path:item_id>', methods=['GET'])
def api_prompt_get(item_id):
    if item_id.startswith(prompt_store.PROJECT_PREFIX):
        d = _project_item_detail(item_id)
        return (jsonify(d), 200) if d else (jsonify({"error": "未知条目"}), 404)
    try:
        return jsonify(prompt_store.describe(item_id))
    except KeyError:
        return jsonify({"error": "未知条目"}), 404


@bp.route('/api/prompts/<path:item_id>', methods=['PUT'])
def api_prompt_put(item_id):
    """保存本机覆盖 {text, rebase}；校验失败 400 {errors}。写操作经 _write_guard。"""
    denied = _write_guard()
    if denied:
        return denied
    if item_id.startswith(prompt_store.PROJECT_PREFIX):
        return jsonify({"error": "项目约定只读：请在项目仓库 .loop/ 中修改"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return jsonify({"error": "请求体需为 {text, rebase}"}), 400
    try:
        prompt_store.save_override(item_id, body["text"], rebase=bool(body.get("rebase")))
    except KeyError:
        return jsonify({"error": "未知条目"}), 404
    except ValueError as e:
        errors = e.args[0] if e.args and isinstance(e.args[0], list) else [{"code": "invalid", "message": str(e)}]
        return jsonify({"errors": errors}), 400
    return jsonify(prompt_store.describe(item_id))


@bp.route('/api/prompts/<path:item_id>', methods=['DELETE'])
def api_prompt_delete(item_id):
    """恢复默认（删除本机覆盖与 .base）。写操作经 _write_guard。"""
    denied = _write_guard()
    if denied:
        return denied
    if item_id.startswith(prompt_store.PROJECT_PREFIX):
        return jsonify({"error": "项目约定只读"}), 403
    try:
        removed = prompt_store.delete_override(item_id)
    except KeyError:
        return jsonify({"error": "未知条目"}), 404
    d = prompt_store.describe(item_id)
    d["removed"] = removed
    return jsonify(d)
