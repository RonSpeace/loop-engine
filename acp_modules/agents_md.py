"""嵌套 AGENTS.md 发现（上下文编译器 L2 项目约定 / 看板只读条目 / doctor 共用，纯读）。

AGENTS.md 是项目自己的规则：仓库根一份，子目录可再放一份，离改动越近越优先。Loop Engine 只给 agent
指路并记录读的是哪一版（内容 hash），不内联正文。由 acp_shared 显式回挂。
"""
from pathlib import Path

AGENTS_MD = "AGENTS.md"
WALK_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build",
                  ".mypy_cache", ".pytest_cache", ".claude", ".idea", ".vscode"}
WALK_MAX_DEPTH = 8
WALK_MAX_FILES = 200


def _safe_rel(path):
    """仓库内相对路径（拒绝绝对路径与 ..）；非法 → None。"""
    s = str(path or "").strip()
    while s.startswith("./"):
        s = s[2:]
    p = Path(s)
    if not str(p) or p.is_absolute() or ".." in p.parts:
        return None
    return p


def _inside(base, full):
    try:
        return Path(full).resolve().is_relative_to(Path(base).resolve())
    except (OSError, RuntimeError):
        return False


def discover_for_paths(base, repo_name, paths_hint=()):
    """按改动路径找 AGENTS.md：仓库根（存在即入选）+ 每个路径从仓库根往下各级目录里的 AGENTS.md。

    base：读取目录（worktree 优先，dry 用主工作区）；paths_hint：[(path, why)] 或 [path]，
    多仓库项目的路径可带 `<仓库名>/` 前缀。返回 [{"rel", "path", "why", "root"}]，根在前、其余按路径排序。"""
    base = Path(base)
    out = {}
    root = base / AGENTS_MD
    if root.is_file():
        out["AGENTS.md"] = {"rel": AGENTS_MD, "path": str(root), "why": "仓库根", "root": True}
    for entry in paths_hint or ():
        raw, why = (entry if isinstance(entry, (tuple, list)) else (entry, f"涉及 {entry}"))[:2]
        rel = _safe_rel(raw)
        if rel is None:
            continue
        if len(rel.parts) > 1 and rel.parts[0] == repo_name:
            rel = Path(*rel.parts[1:])
        parts = rel.parts[:-1] if (base / rel).is_file() or Path(rel.name).suffix else rel.parts
        cur = Path()
        for seg in parts:
            cur = cur / seg
            f = base / cur / AGENTS_MD
            key = str(cur / AGENTS_MD)
            if key in out or not f.is_file() or not _inside(base, f):
                continue
            out[key] = {"rel": key, "path": str(f), "why": why, "root": False}
    return sorted(out.values(), key=lambda x: (not x["root"], x["rel"]))


def discover_all(repo):
    """仓库内全部 AGENTS.md（看板只读条目 / doctor 用）：跳过依赖与构建目录，深度与数量有上限。"""
    repo = Path(repo)
    found = []
    if not repo.is_dir():
        return found

    def walk(d, depth):
        if depth > WALK_MAX_DEPTH or len(found) >= WALK_MAX_FILES:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for e in entries:
            if e.name == AGENTS_MD and e.is_file():
                found.append(str(e.relative_to(repo)))
        for e in entries:
            if e.is_dir() and not e.is_symlink() and e.name not in WALK_SKIP_DIRS and not e.name.startswith("."):
                walk(e, depth + 1)
    walk(repo, 0)
    return found[:WALK_MAX_FILES]


def repeated_root_lines(root_text, nested_text, *, min_chars=12):
    """嵌套 AGENTS.md 与根文件逐字重复的非平凡行（去首尾空白、≥min_chars、非标题/分隔线）。"""
    def lines(t):
        out = []
        for ln in (t or "").splitlines():
            s = ln.strip()
            if len(s) >= min_chars and not s.startswith("#") and set(s) - set("-=*|: "):
                out.append(s)
        return out
    root = set(lines(root_text))
    seen, rep = set(), []
    for s in lines(nested_text):
        if s in root and s not in seen:
            seen.add(s)
            rep.append(s)
    return rep
