"""技能库服务（技能 v2 看板侧）：列表 / 详情 / 本机覆盖读写 / 近 30 天加载次数。

发现、解析、校验的单一实现在 acp_modules/skills.py（经 acp_shared 回挂）；本模块只做看板视图拼装与
本机覆盖目录（prompts.local/skills/）的写入。全局目录在调用期读 acp_shared.GLOBAL_SKILLS_DIR，
产物根经 resolve("ARTIFACTS_ROOT")——测试替换这两处即可隔离。
"""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import acp_shared
from board_modules.runtime import resolve
from board_modules.services.projects import _projects_config

USAGE_WINDOW_SEC = 30 * 86400
_EDIT_FIELDS = ("description", "roles", "activation", "paths", "keywords", "required", "owner", "status")


class SkillReject(Exception):
    """写操作拒绝：(HTTP 状态码, 载荷)。"""

    def __init__(self, status, payload):
        super().__init__(payload)
        self.status, self.payload = status, payload


def _global_dir():
    return Path(acp_shared.GLOBAL_SKILLS_DIR)


def _local_dir():
    return acp_shared.local_skills_dir()


def flow_roles():
    """[{id, display}]，按 flows.json order 排序（展示名一律取自 flows.json）。"""
    roles = ((acp_shared.FLOW_CONFIG or {}).get("roles") or {})
    items = sorted(roles.items(), key=lambda kv: (kv[1].get("order") or 0, kv[0]))
    return [{"id": k, "display": v.get("display") or k} for k, v in items]


def _mounts(projects):
    """{name: [{role, source}]}：flows.json 角色点名与各项目清单 skills.<角色> 点名。"""
    out = {}
    roles = ((acp_shared.FLOW_CONFIG or {}).get("roles") or {})
    for role, entry in roles.items():
        for n in entry.get("skills") or []:
            out.setdefault(n, []).append({"role": role, "source": "flows.json"})
    for pname, paths in projects.items():
        for repo in paths:
            for role, names in acp_shared.load_project_manifest(repo)["skills"].items():
                for n in names:
                    item = {"role": role, "source": f"project:{pname}"}
                    if item not in out.setdefault(n, []):
                        out[n].append(item)
    return out


def usage_counts(now=None):
    """近 30 天各技能加载次数：扫 <产物根>/*/req-*/runs/*/manifest.json（按文件 mtime 取窗口），
    items 里 id 以 `skill:` 开头的条目按名字计数（同一份 manifest 同名只计一次）。坏文件跳过。"""
    root = Path(resolve("ARTIFACTS_ROOT"))
    cutoff = (now or time.time()) - USAGE_WINDOW_SEC
    counts = {}
    try:
        files = list(root.glob("*/req-*/runs/*/manifest.json"))
    except OSError:
        return counts
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seen = set()
        for it in (data.get("items") or []) if isinstance(data, dict) else []:
            iid = it.get("id") if isinstance(it, dict) else None
            if isinstance(iid, str) and iid.startswith("skill:"):
                name = iid[len("skill:"):]
                if name and name not in seen:
                    seen.add(name)
                    counts[name] = counts.get(name, 0) + 1
    return counts


def _view(sk, *, project=None, chain=(), mounts=None, uses=None, known_roles=None):
    d = sk.to_dict()
    lint = acp_shared.skill_lint(sk, known_roles=known_roles)
    mounted = [m for m in (mounts or {}).get(sk.name, [])
               if project is None or m["source"] in ("flows.json", f"project:{project}")]
    d.update({
        "project": project,
        "editable": sk.scope in ("global", "local"),
        "override_of": next((c.scope for c in chain[1:] if c.scope != sk.scope), None)
        if chain and chain[0].path == sk.path else None,
        "shadowed": [{"scope": c.scope, "path": str(c.path), "status": c.status}
                     for c in chain if c is not sk],
        "lint": lint,
        "lint_level": acp_shared.skill_lint_level(lint),
        "mounted": mounted,
        "effective_roles": sorted(set(sk.roles) | {m["role"] for m in mounted}),
        "uses_30d": (uses or {}).get(sk.name, 0),
    })
    return d


def list_skills():
    """GET /api/skills 载荷：全局层（本机覆盖优先于全局）每个名字一条 + 每个项目的项目技能各一条。"""
    projects = _projects_config()
    mounts = _mounts(projects)
    uses = usage_counts()
    known = {r["id"] for r in flow_roles()}
    gdir, ldir = _global_dir(), _local_dir()
    out = []
    for name in sorted({sk.name for sk in acp_shared.discover((), global_dir=gdir, local_dir=ldir)}):
        # 被本机覆盖遮住的全局版本不单列，只出现在有效版本的 shadowed 里
        chain = acp_shared.skill_chain(name, (), global_dir=gdir, local_dir=ldir)
        out.append(_view(chain[0], chain=chain, mounts=mounts, uses=uses, known_roles=known))
    for pname, paths in projects.items():
        seen = set()
        for sk in acp_shared.discover(paths, global_dir=gdir, local_dir=ldir):
            if sk.scope != "project" or sk.name in seen:
                continue
            seen.add(sk.name)
            chain = acp_shared.skill_chain(sk.name, paths, global_dir=gdir, local_dir=ldir)
            out.append(_view(sk, project=pname, chain=chain, mounts=mounts, uses=uses, known_roles=known))
    return {"skills": out, "roles": flow_roles(), "projects": sorted(projects),
            "local_dir": str(ldir)}


def _chain_for(name, project):
    projects = _projects_config()
    repos = projects.get(project, []) if project else []
    if project and project not in projects:
        raise SkillReject(404, {"error": f"项目 {project} 不存在"})
    return acp_shared.skill_chain(name, repos, global_dir=_global_dir(), local_dir=_local_dir()), projects


def get_skill(name, project=None):
    """GET /api/skills/<name> 载荷：有效版本 + 正文 + frontmatter + 各作用域版本（含默认正文，供「恢复默认」对比）。"""
    if not acp_shared.valid_skill_name(name):
        raise SkillReject(400, {"error": "技能名非法"})
    chain, projects = _chain_for(name, project)
    if not chain:
        raise SkillReject(404, {"error": f"技能 {name} 不存在"})
    sk = chain[0]
    d = _view(sk, project=project if sk.scope == "project" else None, chain=chain, mounts=_mounts(projects),
              uses=usage_counts(), known_roles={r["id"] for r in flow_roles()})
    try:
        raw = Path(sk.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    glob = next((c for c in chain if c.scope == "global"), None)
    d.update({"body": sk.body, "meta": sk.meta, "raw": raw,
              "default_body": glob.body if glob is not None else None,
              "versions": [{"scope": c.scope, "path": str(c.path), "status": c.status,
                            "repo": c.repo, "hash": c.hash} for c in chain]})
    return d


def _merge_payload(name, data, base):
    """请求体 → (meta, body)。支持 {"text": 原始 SKILL.md} 或表单字段（缺省沿用 base）。"""
    if not isinstance(data, dict):
        raise SkillReject(400, {"error": "请求体必须是 JSON 对象"})
    if isinstance(data.get("text"), str):
        meta, body, errors = acp_shared.parse_frontmatter(data["text"])
        if errors:
            raise SkillReject(400, {"errors": [{"level": "error", "message": e} for e in errors]})
        meta.setdefault("name", name)
        return meta, body
    meta = {"name": name}
    if base is not None and not base.legacy:
        meta.update({k: v for k, v in base.meta.items() if k != "name"})
    elif base is not None:
        meta.update({"description": base.description, "activation": "always"})
    for k in _EDIT_FIELDS:
        if k in data:
            meta[k] = data[k]
    for k in ("roles", "paths", "keywords"):
        if k in meta and not isinstance(meta[k], list):
            raise SkillReject(400, {"errors": [{"level": "error", "message": f"{k} 必须是列表"}]})
    if "required" in meta and not isinstance(meta["required"], bool):
        raise SkillReject(400, {"errors": [{"level": "error", "message": "required 必须是布尔值"}]})
    body = data.get("body") if isinstance(data.get("body"), str) else (base.body if base is not None else "")
    return meta, body


def _write_local(name, meta, body, *, copy_attachments_from=None):
    """校验后原子写 prompts.local/skills/<name>/SKILL.md；lint 有 error → 400 不落盘。"""
    text = acp_shared.compose_skill_file(meta, body)
    dest_dir = _local_dir() / name
    tmp_root = Path(tempfile.mkdtemp(prefix="skill-lint-"))
    try:
        probe_dir = tmp_root / name
        probe_dir.mkdir()
        (probe_dir / acp_shared.SKILL_FILE).write_text(text, encoding="utf-8")
        for src in (copy_attachments_from, dest_dir):        # 附属文件存在性按落盘后的样子校验
            if src is not None and Path(src).is_dir():
                for f in Path(src).rglob("*"):
                    rel = f.relative_to(src)
                    if f.is_file() and not f.is_symlink() and rel.as_posix() != acp_shared.SKILL_FILE:
                        (probe_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(f, probe_dir / rel)
        probe = acp_shared.load_skill(probe_dir / acp_shared.SKILL_FILE, scope="local", name=name)
        lint = acp_shared.skill_lint(probe, known_roles={r["id"] for r in flow_roles()})
        errors = [i for i in lint if i["level"] == "error"]
        if errors:
            raise SkillReject(400, {"errors": errors})
        dest_dir.mkdir(parents=True, exist_ok=True)
        if copy_attachments_from is not None and Path(copy_attachments_from).is_dir():
            for f in Path(copy_attachments_from).rglob("*"):
                rel = f.relative_to(copy_attachments_from)
                target = dest_dir / rel
                if (f.is_file() and not f.is_symlink() and rel.as_posix() != acp_shared.SKILL_FILE
                        and not target.exists()):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, target)
        fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".SKILL.md.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, dest_dir / acp_shared.SKILL_FILE)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def put_skill(name, data, project=None):
    """PUT：写本机覆盖（覆盖全局或改已有本机版本）。项目技能只读 → 403；全局/本机都没有 → 404。"""
    if not acp_shared.valid_skill_name(name):
        raise SkillReject(400, {"error": "技能名非法"})
    chain, _ = _chain_for(name, project)
    if chain and chain[0].scope == "project":
        raise SkillReject(403, {"error": f"技能 {name} 是项目技能（{chain[0].rel_path}），在看板上只读；请在项目仓库里修改"})
    base = next((c for c in chain if c.scope in ("local", "global")), None)
    if base is None:
        for pname, paths in _projects_config().items():
            owned = acp_shared.find_skill(name, paths, include_drafts=True,
                                          global_dir=_global_dir(), local_dir=_local_dir())
            if owned is not None and owned.scope == "project":
                raise SkillReject(403, {"error": f"技能 {name} 是项目 {pname} 的项目技能（{owned.rel_path}），"
                                                 "在看板上只读；请在项目仓库里修改"})
        raise SkillReject(404, {"error": f"技能 {name} 不存在（新建请用 POST /api/skills）"})
    meta, body = _merge_payload(name, data, base)
    attach_src = base.dir if (base.scope == "global" and base.dir is not None) else None
    _write_local(name, meta, body, copy_attachments_from=attach_src)
    return get_skill(name)


def create_skill(data):
    """POST：在本机覆盖目录新建草稿（status 缺省 draft）。同名已存在（本机或全局）→ 409。"""
    name = (data or {}).get("name") if isinstance(data, dict) else None
    if not acp_shared.valid_skill_name(name):
        raise SkillReject(400, {"error": "技能名非法（小写字母/数字/连字符，≤40 字符）"})
    if acp_shared.skill_chain(name, (), global_dir=_global_dir(), local_dir=_local_dir()):
        raise SkillReject(409, {"error": f"技能 {name} 已存在（修改请用 PUT /api/skills/{name}）"})
    meta, body = _merge_payload(name, data, None)
    meta.setdefault("activation", "on_demand")
    meta.setdefault("status", "draft")
    _write_local(name, meta, body)
    return get_skill(name)


def delete_skill(name):
    """DELETE：删除本机覆盖 / 草稿（恢复默认）。没有本机版本：全局 → 403（默认随代码发布不可删），不存在 → 404。"""
    if not acp_shared.valid_skill_name(name):
        raise SkillReject(400, {"error": "技能名非法"})
    ldir = _local_dir()
    targets = [p for p in (ldir / name, ldir / f"{name}.md") if p.exists()]
    if not targets:
        if acp_shared.skill_chain(name, (), global_dir=_global_dir(), local_dir=ldir):
            raise SkillReject(403, {"error": f"技能 {name} 没有本机覆盖，全局默认版本不能在看板上删除"})
        raise SkillReject(404, {"error": f"技能 {name} 不存在"})
    for p in targets:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    return {"ok": True, "deleted": [str(p) for p in targets]}
