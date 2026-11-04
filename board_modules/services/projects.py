"""#32 Phase 3：projects 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from pathlib import Path
import json
import os
import re

from board_modules.runtime import resolve



def _read_projects_raw():
    """读 projects.json 原始 JSON（读路径天然跟随软链）。返回 (raw, error)；
    error ∈ {None, "missing"（缺失/不可读）, "invalid"（存在但解析失败/非对象）}。"""
    try:
        text = resolve("PROJECTS_FILE").read_text(encoding="utf-8")
    except OSError:
        return None, "missing"
    try:
        raw = json.loads(text)
    except Exception:  # noqa: BLE001
        return None, "invalid"
    if not isinstance(raw, dict):
        return None, "invalid"
    return raw, None


def _normalize_projects_raw(raw):
    """写回形态归一（#4）：字符串简写条目 → {"paths": [原值]}；其余条目原样、键序保持。"""
    out = {}
    for k, v in raw.items():
        out[k] = {"paths": [v]} if isinstance(v, str) else v
    return out


# ---- #5 项目配置读-改-写共用件（锁由调用方获取，helper 自身不加锁——非重入 Lock，防嵌套死锁） ----

def _load_projects_for_write():
    """写路径统一读入口：realpath 读真实文件（worktree 软链不被替换）→ JSON → 归一。
    返回 (target, orig, norm, error)；error 为 None 或 (status, message)。"""
    target = Path(os.path.realpath(resolve("PROJECTS_FILE")))
    try:
        orig = target.read_bytes()
    except OSError:
        return None, None, None, (404, "projects.json 不存在或不可读")
    try:
        raw = json.loads(orig.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, None, None, (400, "projects.json 解析失败，拒绝写入")
    if not isinstance(raw, dict):
        return None, None, None, (400, "projects.json 解析失败，拒绝写入")
    return target, orig, _normalize_projects_raw(raw), None


def _save_projects_atomic(target, orig, norm):
    """原子写 projects.json（调用方已持 _MODEL_WRITE_LOCK 并完成校验/变更判定）：.bak（写前字节）
    → 同目录 tmp（projects.json.tmp，同文件系统保原子 rename）→ os.replace 原子替换；失败清理 tmp
    返回错误串（原文件不动），成功返回 None。本 helper 不加锁（锁归属见上）。"""
    try:
        (target.parent / (target.name + ".bak")).write_bytes(orig)
        tmp = target.parent / (target.name + ".tmp")
        tmp.write_text(json.dumps(norm, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except Exception as e:  # noqa: BLE001 - 写失败回滚：tmp 清理、原文件不动
        try:
            (target.parent / (target.name + ".tmp")).unlink()
        except OSError:
            pass
        return f"写入失败：{e}"
    return None


_WRITE_UNSET = object()  # 写路径哨兵：字段未传（保持原值、不触碰对应字典）


def _write_project_override(name, role, model=_WRITE_UNSET):
    """#4 写路径（调用方已过校验）：读真实文件 → 归一 → 内存改 models（按需增删，
    单字段读-改-写）→ 变更判定（归一后 dict 深比较，键序无关；无变化不写盘/
    不生成 .bak/不动 mtime）→ .bak（写前字节）→ 同目录 tmp（projects.json.tmp，同文件系统保原子
    rename）→ os.replace 原子替换。返回 (status, payload)：200 {changed} / 404 / 400 / 500。
    进程内锁串行化。既有 thinking 段原样保留（#50 起不再读取/写入）。"""
    target = Path(os.path.realpath(resolve("PROJECTS_FILE")))
    with resolve("_MODEL_WRITE_LOCK"):
        try:
            orig = target.read_bytes()
        except OSError:
            return 404, {"error": "projects.json 不存在或不可读"}
        try:
            raw = json.loads(orig.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return 400, {"error": "projects.json 解析失败，拒绝写入"}
        if not isinstance(raw, dict):
            return 400, {"error": "projects.json 解析失败，拒绝写入"}
        norm = _normalize_projects_raw(raw)
        entry = norm.get(name)
        if entry is None:
            return 404, {"error": f"项目 {name} 不在 projects.json 中"}
        if not isinstance(entry, dict):
            return 400, {"error": f"项目 {name} 配置结构不符，拒绝写入"}
        before = json.loads(json.dumps(norm, ensure_ascii=False))
        if model is not _WRITE_UNSET:
            current = entry.get("models")
            current = dict(current) if isinstance(current, dict) else None
            val = model.strip()
            if val:
                entry["models"] = {**(current or {}), role: val}
            elif current is not None and role in current:
                current.pop(role)
                entry["models"] = current
        if norm == before:
            return 200, {"changed": False}
        werr = _save_projects_atomic(target, orig, norm)
        if werr:
            return 500, {"error": werr}
        return 200, {"changed": True}


# ==================== #5 项目中心 P3：项目管理（新增/编辑/移除接入） ====================
# 数据源唯一 = projects.json（写路径三件套：fail-closed 校验 / .bak + tmp + os.replace 原子替换 /
# 读-改-写保留全部既有条目与未知字段，含 #4 的 models）；落点 = os.path.realpath(PROJECTS_FILE)
# 真实文件（worktree 软链不被替换）。写锁 = _MODEL_WRITE_LOCK（与 #4 模型覆盖共用同一临界区）。
_PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PROJECT_GOAL_MAX = 500
_PROJECT_BASE_BRANCH_MAX = 128


def _project_name_error(name):
    """项目名校验（fail-closed）：非串/空/超 64/不匹配 ^[a-z0-9][a-z0-9-]*$ 返回错误文案，合法 None。"""
    if not isinstance(name, str):
        return "项目名需为字符串"
    name = name.strip()
    if not name:
        return "项目名不能为空"
    if len(name) > 64:
        return "项目名过长（≤64 字符）"
    if not _PROJECT_NAME_RE.match(name):
        return "项目名仅允许小写字母、数字、连字符，且以字母或数字开头"
    return None


def _project_paths_parse(value):
    """paths 校验（fail-closed）：非空数组、每项非空字符串、绝对路径、存在且为目录、含 .git
    （目录或文件，兼容 worktree/submodule，沿 acp_shared.git_log_repo_ok 口径）。返回 (paths, error)。"""
    if not isinstance(value, list) or not value:
        return None, "paths 需为非空数组（≥1 个仓库绝对路径）"
    out = []
    for p in value:
        if not isinstance(p, str) or not p.strip():
            return None, "paths 每项需为非空字符串"
        p = p.strip()
        if not os.path.isabs(p):
            return None, f"路径需为绝对路径：{p}"
        if not Path(p).is_dir():
            return None, f"路径不存在或不是目录：{p}"
        if not (Path(p) / ".git").exists():
            return None, f"路径不是 git 仓库（无 .git）：{p}"
        out.append(p)
    return out, None


def _project_base_branch_parse(value, default=None):
    """base_branch 校验（fail-closed）：None/空串取缺省（default 仅 POST 传 "main"）；字符串非空、
    无空白/控制字符、≤128；PATCH 传空串或非字符串（含 #369 dict 映射形态）一律 400。返回 (value, error)。"""
    if value is None or (value == "" and default is not None):
        if default is not None:
            return default, None
        return None, "base_branch 不能为空"
    if not isinstance(value, str):
        return None, "base_branch 需为字符串（多仓库映射形态请在 projects.json 中编辑）"
    if not value:
        return None, "base_branch 不能为空"
    if len(value) > _PROJECT_BASE_BRANCH_MAX:
        return None, f"base_branch 过长（≤{_PROJECT_BASE_BRANCH_MAX} 字符）"
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        return None, "base_branch 不能包含空白或控制字符"
    return value, None


def _project_goal_parse(value):
    """goal 校验（fail-closed）：None/缺失 → ""（POST 不写键；PATCH 空串 = 清空该键）；
    字符串 strip 后 ≤500、无控制字符（\\x00-\\x1f）。返回 (goal, error)。"""
    if value is None:
        return "", None
    if not isinstance(value, str):
        return None, "goal 需为字符串"
    goal = value.strip()
    if len(goal) > _PROJECT_GOAL_MAX:
        return None, f"goal 过长（≤{_PROJECT_GOAL_MAX} 字符）"
    if any(ord(ch) < 32 for ch in value):
        return None, "goal 不能包含控制字符"
    return goal, None


def _doing_count(project):
    """该项目 doing（进行中）卡数（只读 SELECT，board.db 零写入）。"""
    with resolve("get_db")() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM requirements WHERE project = ? AND status = 'doing'",
            (project,)).fetchone()
    return row["c"] if row else 0


def _projects_config():
    """projects.json（项目名 -> 本地路径列表，与 dispatcher load_projects 兼容归一）"""
    try:
        raw = json.loads(resolve("PROJECTS_FILE").read_text())
    except Exception:
        return {}
    out = {}
    for name, cfg in raw.items():
        if isinstance(cfg, str):
            paths = [cfg]
        elif isinstance(cfg, dict):
            paths = cfg.get("paths") or ([cfg["path"]] if cfg.get("path") else [])
        else:
            paths = []
        out[name] = [str(p) for p in paths if p]
    return out


def _auto_projects_config():
    """projects.json 展示层配置（#3，fail-safe）：{name: {"paths": [...], "goal": str}}。
    与 dispatcher load_projects 兼容归一（str 简写 / paths / path）；文件缺失/非法返回 {}（不抛）。
    单一源：#3 项目信息条 goal/paths 与 GET /api/auto-projects 共用。"""
    try:
        raw = json.loads(resolve("PROJECTS_FILE").read_text())
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, cfg in raw.items():
        if isinstance(cfg, str):
            paths, goal = [cfg], ""
        elif isinstance(cfg, dict):
            paths = cfg.get("paths") or ([cfg["path"]] if cfg.get("path") else [])
            goal = cfg.get("goal") if isinstance(cfg.get("goal"), str) else ""
        else:
            paths, goal = [], ""
        out[str(name)] = {"paths": [str(p) for p in paths if p],
                          "goal": goal or ""}
    return out


def _projects_restart_config():
    """projects.json 每项目 restart 配方（#300/#324，单一事实源）：{project: {cmd, verify, timeout}}。
    与 dispatcher load_projects 的 restart 解析兼容归一：非 dict/缺 cmd 视为未配置，
    timeout 非法回退默认 30s（配置为本地可信数据，宽容降级）。"""
    try:
        raw = json.loads((resolve("BASE_DIR") / "projects.json").read_text())
    except Exception:
        return {}
    out = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        r = cfg.get("restart")
        if isinstance(r, dict) and str(r.get("cmd") or "").strip():
            try:
                out[name] = {"cmd": str(r.get("cmd")).strip(),
                             "verify": str(r.get("verify") or "").strip(),
                             "timeout": max(1, int(r.get("timeout") or 30))}
            except (TypeError, ValueError):
                continue
    return out


# ==================== 项目级 Git 配置（v1）：读归一 / 写校验（fail-closed） ====================
# 数据模型（projects.json 每项目条目内的可选段，单一事实源；dispatcher 侧同口径）：
#   git: {remote: "origin", url: "", credential: "system|token|ssh", ssh_key: ""}
#   worktree: "per-requirement"（当前唯一支持值）
#   git_sync: {before_card: "off|fetch|pull", on_done: "off|auto|manual",
#              diverge: "overlap-check|ff-only|stop", timeout: 120}
# 读（GET）宽容：缺失/非法值一律回退默认，绝不 400；写路径 fail-closed：任一非法 400 零写入。
# 旧布尔兼容：fetch_before→before_card（true=pull）、push_after→on_done（true=auto），枚举优先；
# 写回时统一新枚举（旧键不落盘）。
PROJECT_GIT_DEFAULTS = {"remote": "origin", "url": "", "credential": "system", "ssh_key": ""}
PROJECT_WORKTREE_DEFAULT = "per-requirement"
PROJECT_GIT_SYNC_DEFAULTS = {"before_card": "off", "on_done": "off",
                             "diverge": "overlap-check", "timeout": 120}
_PROJECT_GIT_CREDENTIALS = ("system", "token", "ssh")
_PROJECT_GIT_BEFORE_CARD = ("off", "fetch", "pull")
_PROJECT_GIT_ON_DONE = ("off", "auto", "manual")
_PROJECT_GIT_DIVERGE = ("overlap-check", "ff-only", "stop")
_PROJECT_GIT_KEYS = ("remote", "url", "credential", "ssh_key")
_PROJECT_GIT_SYNC_KEYS = ("before_card", "on_done", "diverge", "timeout")
_PROJECT_GIT_SYNC_LEGACY = {"fetch_before": ("before_card", "pull"),
                            "push_after": ("on_done", "auto")}
_PROJECT_GIT_REMOTE_MAX = 128
_PROJECT_GIT_URL_MAX = 2048
_PROJECT_GIT_SSH_KEY_MAX = 1024
_PROJECT_GIT_SYNC_TIMEOUT_MIN = 1
_PROJECT_GIT_SYNC_TIMEOUT_MAX = 3600
# URL 内嵌密码形态（scheme://user:password@host）：凭据只允许落 data/git_credentials.json
_GIT_URL_PASSWORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]*:[^/@\s]*@")


def _git_token_ok(value):
    """remote 名合法性：非空、不以 '-' 开头、无空白/控制字符（防 argv 注入与歧义）。"""
    return (bool(value) and not value.startswith("-")
            and not any(ch.isspace() or ord(ch) < 32 for ch in value))


def _project_git_sync_normalize(raw):
    """git_sync 读归一（宽容）：返回恒含四键的 dict；旧布尔仅在对应新键缺失/非法时折算。"""
    sync = dict(PROJECT_GIT_SYNC_DEFAULTS)
    if not isinstance(raw, dict):
        return sync
    if raw.get("before_card") in _PROJECT_GIT_BEFORE_CARD:
        sync["before_card"] = raw["before_card"]
    elif isinstance(raw.get("fetch_before"), bool):
        sync["before_card"] = "pull" if raw["fetch_before"] else "off"
    if raw.get("on_done") in _PROJECT_GIT_ON_DONE:
        sync["on_done"] = raw["on_done"]
    elif isinstance(raw.get("push_after"), bool):
        sync["on_done"] = "auto" if raw["push_after"] else "off"
    if raw.get("diverge") in _PROJECT_GIT_DIVERGE:
        sync["diverge"] = raw["diverge"]
    timeout = raw.get("timeout")
    if (isinstance(timeout, int) and not isinstance(timeout, bool)
            and _PROJECT_GIT_SYNC_TIMEOUT_MIN <= timeout <= _PROJECT_GIT_SYNC_TIMEOUT_MAX):
        sync["timeout"] = timeout
    return sync


def _project_git_normalize(entry):
    """GET 详情归一：返回 (git, worktree, git_sync) 三元组（含默认值；非法值回退默认，不 400）。"""
    cfg = entry if isinstance(entry, dict) else {}
    git = dict(PROJECT_GIT_DEFAULTS)
    raw_git = cfg.get("git")
    if isinstance(raw_git, dict):
        remote = raw_git.get("remote")
        if isinstance(remote, str) and _git_token_ok(remote.strip()):
            git["remote"] = remote.strip()
        url = raw_git.get("url")
        if isinstance(url, str):
            git["url"] = url.strip()
        if raw_git.get("credential") in _PROJECT_GIT_CREDENTIALS:
            git["credential"] = raw_git["credential"]
        ssh_key = raw_git.get("ssh_key")
        if isinstance(ssh_key, str):
            git["ssh_key"] = ssh_key.strip()
    return git, PROJECT_WORKTREE_DEFAULT, _project_git_sync_normalize(cfg.get("git_sync"))


def _project_git_view(entry):
    """GET 详情追加字段：{git, worktree, git_sync}（与既有 paths/base_branch/goal 并列）。"""
    git, worktree, git_sync = _project_git_normalize(entry)
    return {"git": git, "worktree": worktree, "git_sync": git_sync}


def _project_git_parse(value):
    """git 段写路径校验（fail-closed）：必须对象、仅允许四键、缺失键补默认。

    返回 (git, error)；error 非 None 时调用方 400 且零写入。url 内嵌密码一律拒绝（凭据单一落点）。
    """
    if not isinstance(value, dict):
        return None, "git 需为对象"
    unknown = sorted(k for k in value if k not in _PROJECT_GIT_KEYS)
    if unknown:
        return None, f"git 含未知字段：{'、'.join(unknown)}"
    git = dict(PROJECT_GIT_DEFAULTS)
    if "remote" in value:
        remote = value["remote"]
        if not isinstance(remote, str) or not remote.strip():
            return None, "git.remote 需为非空字符串"
        remote = remote.strip()
        if len(remote) > _PROJECT_GIT_REMOTE_MAX:
            return None, f"git.remote 过长（≤{_PROJECT_GIT_REMOTE_MAX} 字符）"
        if not _git_token_ok(remote):
            return None, "git.remote 不能包含空白/控制字符，且不得以 '-' 开头"
        git["remote"] = remote
    if "url" in value:
        url = value["url"]
        if not isinstance(url, str):
            return None, "git.url 需为字符串"
        url = url.strip()
        if len(url) > _PROJECT_GIT_URL_MAX:
            return None, f"git.url 过长（≤{_PROJECT_GIT_URL_MAX} 字符）"
        if any(ord(ch) < 32 for ch in url):
            return None, "git.url 不能包含控制字符"
        if _GIT_URL_PASSWORD_RE.match(url):
            return None, "git.url 不得内嵌密码；请改用凭据配置（token / SSH key）"
        git["url"] = url
    if "credential" in value:
        if value["credential"] not in _PROJECT_GIT_CREDENTIALS:
            return None, "git.credential 仅支持 system / token / ssh"
        git["credential"] = value["credential"]
    if "ssh_key" in value:
        ssh_key = value["ssh_key"]
        if not isinstance(ssh_key, str):
            return None, "git.ssh_key 需为字符串（SSH 私钥路径）"
        ssh_key = ssh_key.strip()
        if len(ssh_key) > _PROJECT_GIT_SSH_KEY_MAX:
            return None, f"git.ssh_key 过长（≤{_PROJECT_GIT_SSH_KEY_MAX} 字符）"
        if any(ord(ch) < 32 for ch in ssh_key):
            return None, "git.ssh_key 不能包含控制字符"
        git["ssh_key"] = ssh_key
    return git, None


def _project_worktree_parse(value):
    """worktree 写路径校验：当前唯一支持 per-requirement（fail-closed）。返回 (value, error)。"""
    if not isinstance(value, str):
        return None, "worktree 需为字符串"
    if value.strip() != PROJECT_WORKTREE_DEFAULT:
        return None, "worktree 仅支持 per-requirement（当前唯一实现）"
    return PROJECT_WORKTREE_DEFAULT, None


def _project_git_sync_parse(value):
    """git_sync 写路径校验（fail-closed）：必须对象、允许新枚举 + 旧布尔（新枚举优先）。

    返回 (git_sync, error)；成功结果恒含四键（缺失键补默认），旧布尔折算为新枚举、不落旧键。
    """
    if not isinstance(value, dict):
        return None, "git_sync 需为对象"
    allowed = set(_PROJECT_GIT_SYNC_KEYS) | set(_PROJECT_GIT_SYNC_LEGACY)
    unknown = sorted(k for k in value if k not in allowed)
    if unknown:
        return None, f"git_sync 含未知字段：{'、'.join(unknown)}"
    sync = dict(PROJECT_GIT_SYNC_DEFAULTS)
    if "before_card" in value:
        if value["before_card"] not in _PROJECT_GIT_BEFORE_CARD:
            return None, "git_sync.before_card 仅支持 off / fetch / pull"
        sync["before_card"] = value["before_card"]
    elif "fetch_before" in value:
        if not isinstance(value["fetch_before"], bool):
            return None, "git_sync.fetch_before 需为布尔值"
        sync["before_card"] = "pull" if value["fetch_before"] else "off"
    if "on_done" in value:
        if value["on_done"] not in _PROJECT_GIT_ON_DONE:
            return None, "git_sync.on_done 仅支持 off / auto / manual"
        sync["on_done"] = value["on_done"]
    elif "push_after" in value:
        if not isinstance(value["push_after"], bool):
            return None, "git_sync.push_after 需为布尔值"
        sync["on_done"] = "auto" if value["push_after"] else "off"
    if "diverge" in value:
        if value["diverge"] not in _PROJECT_GIT_DIVERGE:
            return None, "git_sync.diverge 仅支持 overlap-check / ff-only / stop"
        sync["diverge"] = value["diverge"]
    if "timeout" in value:
        timeout = value["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            return None, "git_sync.timeout 需为整数秒"
        if not _PROJECT_GIT_SYNC_TIMEOUT_MIN <= timeout <= _PROJECT_GIT_SYNC_TIMEOUT_MAX:
            return None, (f"git_sync.timeout 需在 {_PROJECT_GIT_SYNC_TIMEOUT_MIN}~"
                          f"{_PROJECT_GIT_SYNC_TIMEOUT_MAX} 秒之间")
        sync["timeout"] = timeout
    return sync, None
