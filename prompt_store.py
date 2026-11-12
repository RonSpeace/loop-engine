"""提示词存储（board 与 dispatcher 共用）：发给 agent 的每一段引擎文案都是文件。

- 默认版本随代码发布在 `prompts/`；本机覆盖在 `prompts.local/`（不进 git，目录结构与 prompts/ 一一对应；
  环境变量 `REQ_BOARD_PROMPTS_LOCAL` 可改路径，测试指向临时目录）。编译时有覆盖用覆盖，没有用默认。
- 注册表 `prompts/registry.json` 声明引擎文案条目（层、占位符、必需占位符、锁定句、生效条件）；
  角色模板不进注册表，由 flows.json 推导出 `role/<id>` 条目（L1，模板仍走 str.format）。
- 占位符替换只替换注册表声明的 `{name}`（单遍正则，不用 str.format）：文中其它花括号、
  以及填进去的值里的花括号都原样保留。
- 读取按文件 mtime/size 缓存：改覆盖文件无需重启进程。
- 覆盖的「基于版本」：保存时把当时的默认文本写到 `prompts.local/<file>.base`；当前默认与之不同 → stale
  （编译仍用覆盖，界面提示三方对比）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)
REPO_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = REPO_DIR / "prompts"
REGISTRY_FILE = PROMPTS_DIR / "registry.json"
MAX_CHARS = 20000
ROLE_PREFIX = "role/"
PROJECT_PREFIX = "project/"

# 角色模板可用占位符（= flow_engine.build_prompt_parts 的 format kwargs；二期新增键在此登记）
ROLE_PLACEHOLDERS = (
    "project", "id", "title", "notes", "repo_listing", "repo_map_hint", "project_goal_hint", "branch",
    "timeout_minutes", "max_steps", "analysis_md", "prd_md", "tech_md", "self_test_md", "accept_md",
    "diff_patch", "tech_review_md", "code_review_md", "artifact_root", "result_json", "review_feedback",
    "stage_input", "history", "extra", "merge_ctx", "readonly_tool_guide", "common", "card_inputs",
)
# 固定段（{card_inputs} 之前）只允许这几个项目级常量（与 flow_engine.FIXED_PART_PLACEHOLDERS 一致）
ROLE_FIXED_PLACEHOLDERS = ("project", "project_goal_hint", "readonly_tool_guide", "common")
_PH_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

_lock = threading.Lock()
_cache = {}   # path(str) -> (mtime_ns, size, text)


def local_dir() -> Path:
    env = os.environ.get("REQ_BOARD_PROMPTS_LOCAL")
    return Path(env) if env else REPO_DIR / "prompts.local"


def sha256(text) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def short_hash(text) -> str:
    return sha256(text)[:12]


def _read_file(path):
    """按 mtime/size 缓存读文件；不存在 → None。"""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        with _lock:
            _cache.pop(str(p), None)
        return None
    key = (st.st_mtime_ns, st.st_size)
    with _lock:
        hit = _cache.get(str(p))
        if hit and hit[:2] == key:
            return hit[2]
    text = p.read_text(encoding="utf-8")
    with _lock:
        _cache[str(p)] = (key[0], key[1], text)
    return text


def _registry():
    raw = _read_file(REGISTRY_FILE)
    if raw is None:
        return {}
    data = json.loads(raw)
    return {it["id"]: dict(it) for it in data.get("items") or []}


def _role_entries():
    """flows.json 推导的角色模板条目（acp_shared 晚导入，避免循环）。"""
    try:
        import acp_shared as ac
    except Exception:
        return {}
    roles = ((ac.FLOW_CONFIG or {}).get("roles") or {})
    out = {}
    for rid, entry in roles.items():
        pf = str(entry.get("prompt_file") or "")
        rel = pf[len("prompts/"):] if pf.startswith("prompts/") else Path(pf).name
        default = entry.get("prompt") or ""
        used = sorted(set(_PH_RE.findall(default)) & set(ROLE_PLACEHOLDERS))
        out[ROLE_PREFIX + rid] = {
            "id": ROLE_PREFIX + rid, "file": rel, "layer": "L1",
            "title": entry.get("display") or rid,
            "placeholders": list(ROLE_PLACEHOLDERS), "used_placeholders": used,
            "required": ["card_inputs"], "locked": [],
            "used_by": f"角色 {entry.get('display') or rid}（{rid}）", "when": None,
            "kind": "role", "_default": default,
        }
    return out


def _entry(item_id):
    if item_id.startswith(ROLE_PREFIX):
        e = _role_entries().get(item_id)
    else:
        e = _registry().get(item_id)
    if e is None:
        raise KeyError(f"未知提示词条目: {item_id}")
    return e


def _clean(e):
    return {k: v for k, v in e.items() if not k.startswith("_")}


def _lang_rel(e, lang):
    """语言版本的相对路径：中文 = 注册的 file，英文 = en/<file>。"""
    return e["file"] if lang == "zh" else f"{lang}/{e['file']}"


def default_text(item_id, lang="zh") -> str:
    """默认文本。英文版文件缺失时退回中文并告警（tests/test_prompt_i18n.py 保证随代码发布的条目齐全）。"""
    e = _entry(item_id)
    if lang != "zh":
        text = _read_file(PROMPTS_DIR / _lang_rel(e, lang))
        if text is not None:
            return text
        log.warning("提示词条目 %s 缺 %s 版本 prompts/%s，按中文发送", item_id, lang, _lang_rel(e, lang))
    if e.get("kind") == "role":
        return e["_default"]
    text = _read_file(PROMPTS_DIR / e["file"])
    if text is None:
        raise FileNotFoundError(f"默认提示词文件缺失: prompts/{e['file']}")
    return text


def _local_path(e, lang="zh"):
    return local_dir() / _lang_rel(e, lang)


def override_text(item_id, lang="zh"):
    return _read_file(_local_path(_entry(item_id), lang))


def base_text(item_id, lang="zh"):
    """覆盖保存时的默认文本（旁路 .base 文件）；无 → None。"""
    e = _entry(item_id)
    return _read_file(Path(str(_local_path(e, lang)) + ".base"))


def read(item_id, lang="zh") -> str:
    """有覆盖用覆盖，否则默认（按语言各自一套：prompts.local/en/ 覆盖英文版）。"""
    ov = override_text(item_id, lang)
    return ov if ov is not None else default_text(item_id, lang)


def origin(item_id, lang="zh") -> str:
    return "override" if override_text(item_id, lang) is not None else "default"


def status(item_id, lang="zh") -> str:
    if override_text(item_id, lang) is None:
        return "default"
    base = base_text(item_id, lang)
    if base is not None and sha256(base) != sha256(default_text(item_id, lang)):
        return "stale"
    return "override"


def src(item_id, lang="zh") -> str:
    """当前生效文本的来源路径（展示用）。"""
    e = _entry(item_id)
    lp = _local_path(e, lang)
    if lp.is_file():
        return str(lp)
    if lang != "zh" and (PROMPTS_DIR / _lang_rel(e, lang)).is_file():
        return f"prompts/{_lang_rel(e, lang)}"
    return f"prompts/{e['file']}"


def render(item_id, *, lang="zh", **values) -> str:
    """读条目并替换声明的占位符（单遍：值里的花括号不再展开）；未传的声明占位符原样保留。"""
    text = read(item_id, lang)
    declared = set(_entry(item_id).get("placeholders") or ())

    def _sub(m):
        name = m.group(1)
        if name in declared and name in values:
            return str(values[name])
        return m.group(0)
    return _PH_RE.sub(_sub, text)


def validate(item_id, text, lang="zh") -> list:
    """保存前校验：未声明占位符、缺必需占位符、缺锁定句、超长；角色模板另查 format 可用与固定段占位符。"""
    e = _entry(item_id)
    errors = []
    if not isinstance(text, str):
        return [{"code": "invalid", "message": "text 必须是字符串"}]
    if len(text) > MAX_CHARS:
        errors.append({"code": "too_long", "message": f"超过 {MAX_CHARS} 字（当前 {len(text)}）"})
    declared = set(e.get("placeholders") or ())
    used = _PH_RE.findall(text)
    unknown = sorted({n for n in used if n not in declared})
    if unknown:
        errors.append({"code": "unknown_placeholder",
                       "message": "使用了未声明的占位符：" + "、".join("{%s}" % n for n in unknown)})
    missing = [n for n in (e.get("required") or ()) if "{%s}" % n not in text]
    if missing:
        errors.append({"code": "missing_required",
                       "message": "缺少必需占位符：" + "、".join("{%s}" % n for n in missing)})
    for s in e.get("locked" if lang == "zh" else f"locked_{lang}") or ():
        if s not in text:
            errors.append({"code": "locked_removed", "message": f"锁定句被删改：{s}"})
    if e.get("kind") == "role":
        try:
            text.format(**{k: "" for k in ROLE_PLACEHOLDERS})
        except (KeyError, IndexError, ValueError) as ex:
            errors.append({"code": "format_error",
                           "message": f"模板无法渲染（花括号需成对转义 {{{{ }}}}）：{ex}"})
        if "{card_inputs}" in text:
            fixed = text.split("{card_inputs}", 1)[0]
            bad = sorted({n for n in _PH_RE.findall(fixed) if n not in ROLE_FIXED_PLACEHOLDERS})
            if bad:
                errors.append({"code": "card_placeholder_in_fixed",
                               "message": "固定段（{card_inputs} 之前）只允许 "
                               + "/".join(ROLE_FIXED_PLACEHOLDERS) + "，出现了："
                               + "、".join("{%s}" % n for n in bad)})
    return errors


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_override(item_id, text, rebase=False, lang="zh"):
    """写本机覆盖；校验不过抛 ValueError(errors)。首次保存或 rebase 时把当前默认写进 .base。"""
    errors = validate(item_id, text, lang)
    if errors:
        raise ValueError(errors)
    e = _entry(item_id)
    path = _local_path(e, lang)
    base = Path(str(path) + ".base")
    if rebase or not base.is_file():
        _atomic_write(base, default_text(item_id, lang))
    _atomic_write(path, text)


def delete_override(item_id, lang="zh"):
    """恢复默认 = 删除覆盖文件与 .base。返回是否删了东西。"""
    e = _entry(item_id)
    path = _local_path(e, lang)
    hit = False
    for p in (path, Path(str(path) + ".base")):
        try:
            p.unlink()
            hit = True
        except FileNotFoundError:
            pass
    return hit


def items() -> list:
    """全部可编辑条目（注册表 + 角色模板），带当前状态。"""
    out = []
    for e in list(_registry().values()) + list(_role_entries().values()):
        d = _clean(e)
        d.setdefault("kind", "system" if e["id"].startswith("system/") else "fragment")
        try:
            d["status"] = status(e["id"])
        except FileNotFoundError:
            d["status"] = "missing"
        out.append(d)
    return out


def describe(item_id, lang="zh") -> dict:
    """单条目详情（API 用）。"""
    e = _entry(item_id)
    d = _clean(e)
    d.setdefault("kind", "system" if item_id.startswith("system/") else "fragment")
    dt = default_text(item_id, lang)
    ov = override_text(item_id, lang)
    st = status(item_id, lang)
    d.update(default_text=dt, override_text=ov, text=(ov if ov is not None else dt), status=st,
             base_text=(base_text(item_id, lang) if st == "stale" else None), readonly=False, lang=lang)
    return d


def when_matches(item_id, *, role, flags) -> bool:
    """片段生效条件：when = {"roles": [...], "flags": ["project.branch_only", ...]}；None = 恒生效。
    flags 全部为真且（无 roles 限定或角色命中）才生效。"""
    w = _entry(item_id).get("when")
    if not w:
        return True
    roles = w.get("roles")
    if roles and role not in roles:
        return False
    return all(flags.get(f) for f in (w.get("flags") or ()))
