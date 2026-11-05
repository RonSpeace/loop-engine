"""技能 v2：技能发现（全局 / 本机覆盖 / 项目三个作用域，含旧平铺 md）、SKILL.md 极简 frontmatter 解析、
按角色与卡片的选择（always / auto / on_demand / manual）、校验（lint）与 Claude Code 运行体落盘。

格式对齐 Anthropic Agent Skills：`<名>/SKILL.md`（头部 `---` 包住的 frontmatter + 正文），可附
references/、scripts/ 等文件。旧平铺 `<名>.md` 继续有效 = activation always、status active、
roles 取自点名处（flows.json 角色 / 项目清单）。

作用域优先级（同名整份覆盖，不合并内容）：项目 `<仓库>/.loop/skills/` > 本机覆盖
`prompts.local/skills/`（环境变量 REQ_BOARD_PROMPTS_LOCAL 可改根目录）> 全局 `prompts/skills/`。
同一作用域内目录形态 `<名>/SKILL.md` 优先于平铺 `<名>.md`。

本模块不 import acp_shared（避免循环）；需要 flows.json 角色点名 / 项目清单时在函数内晚导入。
由 acp_shared 以显式名单回挂公开名字。
"""
import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("acp_shared")

REPO_DIR = Path(__file__).resolve().parent.parent
GLOBAL_SKILLS_DIR = REPO_DIR / "prompts" / "skills"
SKILL_FILE = "SKILL.md"
SKILL_MAX_CHARS = 4000            # 单个技能正文进 prompt 的字符上限（与项目指引同口径）
SKILL_DESC_MAX = 200              # description 上限（进技能目录，agent 靠它判断要不要读）
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SKILL_ACTIVATIONS = ("always", "auto", "on_demand", "manual")
SKILL_STATUSES = ("draft", "active", "deprecated")
SKILL_SCOPES = ("project", "local", "global")          # 优先级从高到低
SKILL_META_KEYS = ("name", "description", "roles", "activation", "paths", "keywords",
                   "required", "owner", "status")
_LIST_KEYS = ("roles", "paths", "keywords")
_MATERIALIZED_MARK = ".loop-materialized"
_MATERIALIZE_MAX_BYTES = 1 << 20  # 附属文件单个上限 1MB（超出不复制）


def prompts_local_dir():
    """本机覆盖根目录（不进 git）：REQ_BOARD_PROMPTS_LOCAL 优先，否则仓库根 prompts.local/。调用期现读。"""
    env = os.environ.get("REQ_BOARD_PROMPTS_LOCAL")
    return Path(env).expanduser() if env else REPO_DIR / "prompts.local"


def local_skills_dir():
    return prompts_local_dir() / "skills"


def valid_skill_name(name):
    return isinstance(name, str) and bool(_SKILL_NAME_RE.match(name))


# ---------------- frontmatter：手写极简 YAML 子集 ----------------

_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$")
_FM_ITEM_RE = re.compile(r"^\s*-\s+(.*)$|^\s*-\s*$")
_BOOL_WORDS = {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}


def _strip_comment(val):
    """去掉行内注释：引号外、前面是空白的 `#` 起算。"""
    quote = None
    for i, ch in enumerate(val):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or val[i - 1] in " \t"):
            return val[:i].rstrip()
    return val.strip()


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in _BOOL_WORDS:
        return _BOOL_WORDS[low]
    return s


def _split_flow_list(inner):
    """`[a, "b, c", 'd']` 的内部按引号外逗号切分。"""
    out, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [_unquote(x) for x in out if x.strip()]


def parse_frontmatter(text):
    """解析 SKILL.md：返回 (meta: dict, body: str, errors: list[str])。绝不抛。
    支持：`key: value`、`key: [a, b]`、`key:` 后跟 `- item` 行、布尔（true/false/yes/no）、引号字符串、
    `#` 行内注释与注释行。其它 YAML 语法（嵌套映射、多行字符串等）记错误。
    没有 frontmatter（首行不是 `---`）→ meta 为空、body 为全文。"""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return {}, text, []
    end = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            end = i
            break
    if end is None:
        return {}, text, ["frontmatter 未闭合（缺少结束的 --- 行）"]
    meta, errors, cur_list = {}, [], None
    for ln, raw in enumerate(lines[1:end], start=2):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m_item = _FM_ITEM_RE.match(line)
        if m_item:
            if cur_list is None:
                errors.append(f"第 {ln} 行：列表项前没有对应的键")
                continue
            val = _strip_comment(m_item.group(1) or "")
            if val:
                meta[cur_list].append(_unquote(val))
            continue
        if line[:1] in (" ", "\t"):
            errors.append(f"第 {ln} 行：不支持的缩进写法（只支持 key: value 与 - 列表项）")
            continue
        m = _FM_KEY_RE.match(line)
        if not m:
            errors.append(f"第 {ln} 行：无法解析（应为 key: value）")
            cur_list = None
            continue
        key, val = m.group(1), _strip_comment(m.group(2))
        if key in meta:
            errors.append(f"第 {ln} 行：重复的键 {key}")
        cur_list = None
        if val == "":
            meta[key] = []           # 等待后续 `- item`；无列表项时视为空列表 / 空值
            cur_list = key
        elif val.startswith("["):
            if not val.endswith("]"):
                errors.append(f"第 {ln} 行：{key} 的 [ 列表未闭合")
                meta[key] = []
            else:
                meta[key] = _split_flow_list(val[1:-1])
        elif val[:1] in ("{", "|", ">", "&", "*", "!"):
            errors.append(f"第 {ln} 行：{key} 用了不支持的 YAML 语法")
        else:
            meta[key] = _scalar(val)
    body = "\n".join(lines[end + 1:])
    return meta, body, errors


def _fm_scalar_text(v):
    s = str(v)
    if s == "" or s.strip() != s or any(c in s for c in ":#[]{},\"'") or s.lower() in _BOOL_WORDS:
        return '"' + s.replace('"', "'") + '"'
    return s


def render_frontmatter(meta):
    """按 SKILL_META_KEYS 顺序生成 frontmatter 文本（`---` 包裹，末尾换行）；未知键追加在后。"""
    out = ["---"]
    keys = [k for k in SKILL_META_KEYS if k in meta] + [k for k in meta if k not in SKILL_META_KEYS]
    for k in keys:
        v = meta[k]
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (list, tuple)):
            out.append(f"{k}: [" + ", ".join(_fm_scalar_text(x) for x in v) + "]")
        else:
            out.append(f"{k}: {_fm_scalar_text(v)}")
    out.append("---")
    return "\n".join(out) + "\n"


def compose_skill_file(meta, body):
    """frontmatter + 正文 → SKILL.md 全文（正文前空一行，末尾保证换行）。"""
    body = (body or "").strip("\n")
    return render_frontmatter(meta) + "\n" + body + "\n"


# ---------------- 技能模型 ----------------

@dataclass
class Skill:
    name: str
    scope: str                       # project | local | global
    path: Path                       # SKILL.md 或平铺 md 的路径
    legacy: bool = False             # 旧平铺 <名>.md
    repo: str = None                 # 项目技能所在仓库
    description: str = ""
    roles: list = field(default_factory=list)
    activation: str = "always"
    paths: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    required: bool = False
    owner: str = ""
    status: str = "active"
    body: str = ""                   # 去首尾空白后的完整正文（未截断）
    meta: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    @property
    def dir(self):
        return None if self.legacy else self.path.parent

    @property
    def text(self):
        """进 prompt 的正文：超上限截断并留全文路径（与旧 _read_capped 逐字一致）。"""
        if len(self.body) > SKILL_MAX_CHARS:
            return (self.body[:SKILL_MAX_CHARS]
                    + f"\n……（超过 {SKILL_MAX_CHARS} 字符已截断，完整内容见 {self.path}）")
        return self.body

    @property
    def truncated(self):
        return max(0, len(self.body) - SKILL_MAX_CHARS)

    @property
    def chars(self):
        return len(self.text)

    @property
    def hash(self):
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]

    @property
    def ok(self):
        return not self.errors

    @property
    def rel_path(self):
        """展示 / 技能目录用路径：项目技能相对仓库根，全局相对本仓库根，本机覆盖给绝对路径。"""
        try:
            if self.scope == "project" and self.repo:
                return str(self.path.relative_to(Path(self.repo)))
            return str(self.path.relative_to(REPO_DIR))
        except ValueError:
            return str(self.path)

    def to_dict(self):
        return {"name": self.name, "scope": self.scope, "path": str(self.path), "rel_path": self.rel_path,
                "legacy": self.legacy, "repo": self.repo, "description": self.description,
                "roles": list(self.roles), "activation": self.activation, "paths": list(self.paths),
                "keywords": list(self.keywords), "required": self.required, "owner": self.owner,
                "status": self.status, "chars": self.chars, "truncated": self.truncated,
                "hash": self.hash, "errors": list(self.errors)}


def _read_raw(path):
    """读技能文件原文（单独成函数：测试替换它模拟读失败）。"""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _as_str_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [x.strip() for x in re.split(r"[,，]", v) if x.strip()]
    return []


def load_skill(path, *, scope, legacy=False, name=None, repo=None):
    """读一个技能文件 → Skill。绝不抛：读失败 / 解析失败 / 字段非法都记进 errors（选择时视为不可用）。"""
    path = Path(path)
    if name is None:
        name = path.stem if legacy else path.parent.name
    sk = Skill(name=name, scope=scope, path=path, legacy=legacy, repo=str(repo) if repo else None)
    try:
        raw = _read_raw(path)
    except Exception as e:      # noqa: BLE001 读不了就当不可用：绝不因技能文件把建 prompt 打断
        sk.errors.append(f"读取失败（{e.__class__.__name__}）")
        return sk
    if legacy:
        sk.body = raw.strip()
        return sk
    meta, body, errors = parse_frontmatter(raw)
    sk.meta, sk.body, sk.errors = meta, body.strip(), list(errors)
    if not raw.lstrip("﻿").startswith("---"):
        sk.errors.append("SKILL.md 缺少 frontmatter（首行应为 ---）")
    fm_name = meta.get("name")
    if fm_name is not None and fm_name != name:
        sk.errors.append(f"frontmatter name={fm_name!r} 与目录名 {name!r} 不一致")
    desc = meta.get("description", "")
    sk.description = desc if isinstance(desc, str) else ("" if desc == [] else str(desc))
    for key in _LIST_KEYS:
        v = meta.get(key)
        if v is None:
            continue
        if isinstance(v, list):
            setattr(sk, key, [str(x) for x in v if str(x).strip()])
        elif isinstance(v, str):
            setattr(sk, key, _as_str_list(v))
        else:
            sk.errors.append(f"{key} 必须是列表")
    act = meta.get("activation", "always")
    if act in SKILL_ACTIVATIONS:
        sk.activation = act
    else:
        sk.errors.append(f"activation={act!r} 非法（{'/'.join(SKILL_ACTIVATIONS)}）")
    status = meta.get("status", "active")
    if status in SKILL_STATUSES:
        sk.status = status
    else:
        sk.errors.append(f"status={status!r} 非法（{'/'.join(SKILL_STATUSES)}）")
    req = meta.get("required", False)
    if isinstance(req, bool):
        sk.required = req
    else:
        sk.errors.append("required 必须是 true/false")
        sk.required = str(req).strip().lower() not in ("", "0", "false", "no")   # 宽松认定：宁可停线
    owner = meta.get("owner", "")
    sk.owner = owner if isinstance(owner, str) else str(owner)
    return sk


# ---------------- 发现 ----------------

def _inside(root, full):
    try:
        return Path(full).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _scope_candidates(base, name, *, confine=None):
    """某作用域目录下 name 的候选文件（目录形态优先）：[(path, legacy)]。
    confine：项目作用域的仓库根——解析软链后必须仍在仓库内（防 .loop/ 软链把仓库外内容带进 prompt）。"""
    out = []
    for p, legacy in ((Path(base) / name / SKILL_FILE, False), (Path(base) / f"{name}.md", True)):
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if confine is not None and not _inside(confine, p):
            log.warning(f"技能 {name} 文件 {p} 解析后越出仓库（软链），已忽略")
            continue
        out.append((p, legacy))
    return out


def _scope_names(base):
    """作用域目录下的全部技能名（目录含 SKILL.md 或平铺 .md，名字须合法），排序。"""
    names = set()
    try:
        for p in Path(base).iterdir():
            if p.is_dir() and (p / SKILL_FILE).is_file() and valid_skill_name(p.name):
                names.add(p.name)
            elif p.is_file() and p.suffix == ".md" and valid_skill_name(p.stem):
                names.add(p.stem)
    except OSError:
        pass
    return sorted(names)


def _dirs(global_dir, local_dir):
    return (Path(global_dir) if global_dir is not None else GLOBAL_SKILLS_DIR,
            Path(local_dir) if local_dir is not None else local_skills_dir())


def skill_chain(name, repos=(), *, global_dir=None, local_dir=None):
    """name 在各作用域的全部版本，按优先级排列：各仓库（登记顺序）> 本机覆盖 > 全局。
    每个作用域只取一份（目录形态优先于平铺）。名字非法 → []。"""
    if not valid_skill_name(name):
        return []
    gdir, ldir = _dirs(global_dir, local_dir)
    chain = []
    for repo in repos or ():
        base = Path(repo) / ".loop" / "skills"
        cands = _scope_candidates(base, name, confine=repo)
        if cands:
            p, legacy = cands[0]
            chain.append(load_skill(p, scope="project", legacy=legacy, name=name, repo=repo))
    for scope, base in (("local", ldir), ("global", gdir)):
        cands = _scope_candidates(base, name)
        if cands:
            p, legacy = cands[0]
            chain.append(load_skill(p, scope=scope, legacy=legacy, name=name))
    return chain


def find_skill(name, repos=(), *, drafts=(), include_drafts=False, global_dir=None, local_dir=None):
    """真实会话用的有效版本：优先级链上第一个非草稿版本（草稿只在显式带入 drafts 或 include_drafts 时生效，
    否则跳过它退回下一作用域）。找不到 → None。解析失败的版本照样返回（errors 非空，由调用方判定）。"""
    for sk in skill_chain(name, repos, global_dir=global_dir, local_dir=local_dir):
        if sk.status == "draft" and not include_drafts and name not in set(drafts or ()):
            continue
        return sk
    return None


def discover(repos=(), *, global_dir=None, local_dir=None):
    """各作用域全部技能（不去重，供列表 / 校验）：[Skill]，按 名字 → 作用域优先级 排列。"""
    gdir, ldir = _dirs(global_dir, local_dir)
    names = set(_scope_names(gdir)) | set(_scope_names(ldir))
    for repo in repos or ():
        names |= set(_scope_names(Path(repo) / ".loop" / "skills"))
    out = []
    for n in sorted(names):
        out.extend(skill_chain(n, repos, global_dir=gdir, local_dir=ldir))
    return out


# ---------------- 卡片 notes 指令 ----------------

_NOTES_SKILLS_RE = re.compile(r"^\s*(-?)skills\s*[:：]\s*(.*)$", re.IGNORECASE)


def parse_notes_skills(notes):
    """卡片 notes 行 `skills: a, b`（强制加载）与 `-skills: x`（排除）→ (include, exclude)，去重保序。
    分隔符：逗号（中英文）、顿号、空白；非法名忽略。"""
    include, exclude = [], []
    for line in (notes or "").splitlines():
        m = _NOTES_SKILLS_RE.match(line)
        if not m:
            continue
        target = exclude if m.group(1) else include
        for n in re.split(r"[,，、\s]+", m.group(2).strip()):
            n = n.strip()
            if valid_skill_name(n) and n not in target:
                target.append(n)
    return include, exclude


# ---------------- auto 命中 ----------------

def _glob_to_re(pattern):
    """glob → 正则：`**/` 任意层目录（可为零层）、`**` 任意字符、`*` 段内任意、`?` 段内单字符。"""
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _norm_path(p):
    p = str(p).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def glob_match(pattern, path):
    """路径是否命中 glob：不含 `/` 的模式另按文件名匹配（`*.css` 命中任意层的 css）；
    以 `/` 结尾的模式视为目录前缀（`static/` 等价 `static/**`）。"""
    pattern, path = _norm_path(pattern), _norm_path(path)
    if not pattern or not path:
        return False
    if pattern.endswith("/"):
        pattern += "**"
    rx = _glob_to_re(pattern)
    if rx.match(path):
        return True
    return "/" not in pattern and bool(rx.match(path.rsplit("/", 1)[-1]))


_PATH_TOKEN_RE = re.compile(r"(?<![\w/.:-])((?:[\w.-]+/)+[\w.-]+|[\w-]+\.[A-Za-z][\w]{0,7})(?![\w/])")


def paths_from_text(text):
    """从 notes / 上游产物里提取疑似仓库内路径（供编译器构造 paths_hint）：含 `/` 的相对路径或带扩展名的文件名。
    排除 URL、绝对路径与版本号形态；去重保序。"""
    out = []
    for m in _PATH_TOKEN_RE.finditer(text or ""):
        tok = m.group(1).rstrip(".")
        if not tok or tok.startswith(("http", "www.")) or re.fullmatch(r"[\d.]+", tok):
            continue
        if tok not in out:
            out.append(tok)
    return out


def auto_hit(skill, paths_hint=(), text=""):
    """auto 技能命中依据：路径 glob 命中优先，其次关键词（不区分大小写，对 title + notes）。
    返回说明串（写进清单的 why）或 None。"""
    for g in skill.paths:
        for p in paths_hint or ():
            if glob_match(g, p):
                return f"路径 {_norm_path(p)} 命中 {g}"
    low = (text or "").lower()
    for k in skill.keywords:
        if k and k.lower() in low:
            return f"关键词「{k}」命中"
    return None


# ---------------- 选择 ----------------

@dataclass
class Selection:
    """select_skills 结果。always / catalog 的入选依据在 why[name]；card 的依据随条目给出。"""
    always: list = field(default_factory=list)            # [Skill]：L4 常驻（稳定前缀）
    card: list = field(default_factory=list)              # [(Skill, why)]：L6 卡片段（auto 命中 / notes 点名）
    catalog: list = field(default_factory=list)           # [Skill]：L5 技能目录（on_demand，只给索引）
    skipped: list = field(default_factory=list)           # [(name, why)]
    missing_required: list = field(default_factory=list)  # [name]：必需技能缺失或不可用 → 停线
    why: dict = field(default_factory=dict)               # {name: why}（always / catalog）
    warnings: list = field(default_factory=list)          # [{code, id, message}]（废弃仍被引用等）

    def loaded(self):
        """本次可用的全部技能（always + card + catalog），供 materialize_for_claude。"""
        return list(self.always) + [s for s, _ in self.card] + list(self.catalog)


def _role_skill_names(role):
    import acp_shared   # 晚导入：本模块被 acp_shared 回挂，模块级导入会循环
    names = acp_shared.role_meta(role, "skills")
    return list(names) if isinstance(names, list) else []


def _manifest_skill_names(role, repos):
    import acp_shared
    out = []
    for repo in repos or ():
        for n in (acp_shared.load_project_manifest(repo)["skills"].get(role) or []):
            if n not in out:
                out.append(n)
    return out


def select_skills(project, role, req, repos, *, paths_hint=(), draft_skills=(),
                  role_skills=None, manifest_skills=None, global_dir=None, local_dir=None):
    """按角色 / 卡片选技能。

    挂载集合（保序去重）：flows.json `roles.<role>.skills` 点名 → 项目清单 `skills.<role>` 点名 →
    技能自身 frontmatter `roles` 含本角色的（按名字排序）→ 卡片 notes `skills:` 点名。
    每个技能取作用域优先级链上第一个非草稿版本（`draft_skills` 里的名字允许草稿，供注入预览）。

    - notes `-skills: x` 排除（任何激活方式，含 always）。
    - always → always；notes 点名的非 always 技能 → card（manual 也加载）；auto 命中 → card，未命中 → skipped；
      on_demand → catalog；manual 未点名 → skipped。
    - notes 点名但技能声明了 roles 且不含本角色 → skipped（点名不跨越技能自己声明的角色边界）。
    - 找不到：notes 点名的 → missing_required（卡片明确要求）；角色 / 清单点名的 → skipped（与旧行为一致）。
    - 解析失败：required 或 notes 点名 → missing_required；否则 skipped。
    - 废弃技能照常加载并记 warnings。

    role_skills / manifest_skills 缺省时现读 flows.json 与各仓库清单（测试可直接传入）。
    `project` 只用于日志与告警文案。"""
    req = req or {}
    sel = Selection()
    if role_skills is None:
        role_skills = _role_skill_names(role)
    if manifest_skills is None:
        manifest_skills = _manifest_skill_names(role, repos)
    include, exclude = parse_notes_skills(req.get("notes"))
    drafts = set(draft_skills or ())
    gdir, ldir = _dirs(global_dir, local_dir)
    order, source = [], {}

    def add(n, src):
        if valid_skill_name(n) and n not in source:
            order.append(n)
            source[n] = src

    for n in role_skills or ():
        add(n, f"flows.json 点名 {role}")
    for n in manifest_skills or ():
        add(n, f"项目清单点名 {role}")
    resolved = {}
    names = set(_scope_names(gdir)) | set(_scope_names(ldir))
    for repo in repos or ():
        names |= set(_scope_names(Path(repo) / ".loop" / "skills"))
    for n in sorted(names - set(source)):
        sk = find_skill(n, repos, drafts=drafts, global_dir=gdir, local_dir=ldir)
        resolved[n] = sk
        if sk is not None and not sk.errors and role in sk.roles:
            add(n, f"技能声明 roles 含 {role}")
    for n in include:
        add(n, "卡片 notes 点名")
    text = f"{req.get('title') or ''}\n{req.get('notes') or ''}"

    for n in order:
        if n in exclude:
            sel.skipped.append((n, "卡片 notes 排除（-skills）"))
            continue
        forced = n in include
        sk = resolved[n] if n in resolved else find_skill(n, repos, drafts=drafts, global_dir=gdir, local_dir=ldir)
        if sk is None:
            if skill_chain(n, repos, global_dir=gdir, local_dir=ldir):
                sel.skipped.append((n, "草稿：只在注入预览里可试用，不进真实会话"))
                if forced:
                    sel.missing_required.append(n)
                continue
            log.warning(f"项目 {project} 角色 {role} 声明的技能 {n} 未找到（{source[n]}）")
            sel.skipped.append((n, f"未找到（{source[n]}）"))
            if forced:
                sel.missing_required.append(n)
            continue
        if sk.errors:
            sel.skipped.append((n, "不可用：" + "；".join(sk.errors)))
            if sk.required or forced:
                sel.missing_required.append(n)
            continue
        if forced and sk.roles and role not in sk.roles and source[n] == "卡片 notes 点名":
            sel.skipped.append((n, f"卡片点名，但技能只挂载到 {'、'.join(sk.roles)}"))
            continue
        if sk.status == "deprecated":
            sel.warnings.append({"code": "deprecated_skill", "id": f"skill:{n}",
                                 "message": f"技能 {n} 已废弃，仍被引用（{source[n]}）"})
        if sk.activation == "always":
            sel.always.append(sk)
            sel.why[n] = f"always · {source[n]}"
        elif forced:
            sel.card.append((sk, f"{sk.activation}：卡片 notes 点名"))
        elif sk.activation == "auto":
            hit = auto_hit(sk, paths_hint, text)
            if hit:
                sel.card.append((sk, f"auto：{hit}"))
            else:
                sel.skipped.append((n, "auto：未命中路径或关键词"))
        elif sk.activation == "on_demand":
            sel.catalog.append(sk)
            sel.why[n] = f"on_demand · {source[n]}"
        else:
            sel.skipped.append((n, "manual：卡片 notes 未点名"))
    return sel


# ---------------- 渲染辅助（编译器接线用） ----------------

def skills_block(skills):
    """技能正文块（与旧 scenario_skills_text 逐字一致）：`\\n【场景技能：名】\\n正文\\n\\n…\\n`；空 → ""。
    skills 可为 [Skill] 或 [(Skill, why)]。"""
    parts = []
    for s in skills or ():
        sk = s[0] if isinstance(s, tuple) else s
        parts.append(f"【场景技能：{sk.name}】\n{sk.text}")
    return ("\n" + "\n\n".join(parts) + "\n") if parts else ""


def catalog_block(skills):
    """技能目录（L5）：每个技能一行「名：描述（路径）」；空 → ""。"""
    if not skills:
        return ""
    lines = ["\n【可用技能目录】以下技能未内联正文，需要时用读文件工具读取对应路径："]
    for sk in skills:
        lines.append(f"- {sk.name}：{sk.description or '（无描述）'}（{sk.rel_path}）")
    return "\n".join(lines) + "\n"


# ---------------- 校验 ----------------

_ABS_PATH_RE = re.compile(r"(?:/home/|/Users/|~/)[\w.\-/]+")
_REL_FILE_RE = re.compile(r"(?<![\w/.:-])((?:[\w.-]+/)+[\w-]+\.[A-Za-z]\w{0,7})(?![\w/])")
_ATTACH_RE = re.compile(r"(?<![\w/.-])((?:references|scripts|assets|templates|examples)/[\w./-]+)")


def lint(skill, *, known_roles=None):
    """技能校验：[{level: ok|warn|error, message}]。error = 保存应拒绝 / doctor 告警；ok 条目供界面展示通过项。"""
    out = []

    def add(level, msg):
        out.append({"level": level, "message": msg})

    if valid_skill_name(skill.name):
        add("ok", "名称合法")
    else:
        add("error", f"名称 {skill.name!r} 非法（小写字母/数字/连字符，≤40 字符，字母或数字开头）")
    for e in skill.errors:
        add("error", e)
    if skill.legacy:
        add("warn", f"旧平铺格式 {skill.path.name}：没有 description / 激活方式，建议迁移为 {skill.name}/SKILL.md")
    else:
        unknown = [k for k in skill.meta if k not in SKILL_META_KEYS]
        if unknown:
            add("warn", "未知的 frontmatter 键：" + "、".join(unknown))
        d = (skill.description or "").strip()
        if not d:
            add("error", "description 必填（进技能目录，agent 靠它判断要不要用）")
        elif len(d) > SKILL_DESC_MAX:
            add("error", f"description {len(d)} 字，超过 {SKILL_DESC_MAX} 字")
        else:
            add("ok", f"描述 {len(d)} 字")
        if skill.activation == "auto" and not skill.paths and not skill.keywords:
            add("warn", "activation=auto 但没有声明 paths / keywords，永远不会自动命中")
        if known_roles is not None:
            bad = [r for r in skill.roles if r not in known_roles]
            if bad:
                add("warn", "roles 含未知角色：" + "、".join(bad))
    if not skill.body:
        if not any(o["level"] == "error" and o["message"].startswith("读取失败") for o in out):
            add("error", "正文为空")
    elif len(skill.body) > SKILL_MAX_CHARS:
        add("error", f"正文 {len(skill.body)} 字，超过 {SKILL_MAX_CHARS} 字（进 prompt 时会被截断）")
    else:
        add("ok", f"正文 {len(skill.body)} 字，未超 {SKILL_MAX_CHARS}")
    attach = []
    if skill.dir is not None:
        for m in _ATTACH_RE.finditer(skill.body):
            rel = m.group(1).rstrip(".,;:)）」")
            # references/ 一律按附属文件核对；scripts/ 等常见目录名只有技能目录里确有该子目录时才算
            # （否则多半是在说仓库里的同名目录，如 scripts/test_instance.sh）
            if rel in attach or (not rel.startswith("references/")
                                 and not (skill.dir / rel.split("/", 1)[0]).is_dir()):
                continue
            attach.append(rel)
            full = skill.dir / rel
            if ".." in Path(rel).parts or not full.exists():
                add("error", f"引用的附属文件 {rel} 不存在")
            else:
                add("ok", f"附属文件 {rel} 存在")
    if skill.status == "deprecated":
        add("warn", "已废弃：仍被引用时会告警")
    elif skill.status == "draft":
        add("warn", "草稿：只在注入预览里可试用，不进真实会话")
    if skill.scope in ("global", "local"):
        hits = []
        for rx in (_ABS_PATH_RE, _REL_FILE_RE):
            for m in rx.finditer(f"{skill.description}\n{skill.body}"):
                tok = m.group(m.lastindex or 0)
                if any(tok.startswith(a) for a in attach) or tok in hits:
                    continue
                hits.append(tok)
        if hits:
            add("warn", "全局技能里出现具体文件路径（" + "、".join(hits[:5])
                + ("等" if len(hits) > 5 else "") + "），应避免写单个项目的专有内容")
        else:
            add("ok", "不含单个项目的专有路径")
    return out


def lint_level(items):
    """lint 结果的最高级别：error > warn > ok。"""
    levels = {i["level"] for i in items}
    return "error" if "error" in levels else "warn" if "warn" in levels else "ok"


# ---------------- Claude Code 运行体落盘 ----------------

def materialize_for_claude(worktree, skills):
    """把本次可用技能复制到 `<worktree>/.claude/skills/<名>/`，交给 Claude Code 自带的按需加载。
    SKILL.md 重写为只含 name/description 的 frontmatter + 正文（旧平铺技能同样生成）；目录技能的附属文件
    原样复制（跳过软链、越界路径与超过 1MB 的文件）。每个目录放 `.gitignore`（`*`）防误提交，并放标记文件；
    目标目录已存在且不是本函数生成的（仓库自带同名技能）→ 不覆盖，跳过。返回写入的目录列表。"""
    root = Path(worktree) / ".claude" / "skills"
    written = []
    for s in skills or ():
        sk = s[0] if isinstance(s, tuple) else s
        if not valid_skill_name(sk.name) or sk.errors:
            continue
        dest = root / sk.name
        if dest.exists():
            if not (dest / _MATERIALIZED_MARK).is_file():
                log.warning(f"{dest} 已存在且不是调度器生成的技能目录，跳过落盘")
                continue
            shutil.rmtree(dest, ignore_errors=True)
        try:
            dest.mkdir(parents=True, exist_ok=True)
            if sk.dir is not None:
                src_root = sk.dir.resolve()
                for f in sorted(sk.dir.rglob("*")):
                    rel = f.relative_to(sk.dir)
                    if f.is_symlink() or not f.is_file() or rel.as_posix() == SKILL_FILE:
                        continue
                    if not f.resolve().is_relative_to(src_root) or f.stat().st_size > _MATERIALIZE_MAX_BYTES:
                        continue
                    target = dest / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, target)
            desc = (sk.description or "").strip() or f"场景技能 {sk.name}"
            (dest / SKILL_FILE).write_text(
                compose_skill_file({"name": sk.name, "description": desc}, sk.body), encoding="utf-8")
            (dest / ".gitignore").write_text("*\n", encoding="utf-8")
            (dest / _MATERIALIZED_MARK).write_text(f"{sk.scope}:{sk.path}\n", encoding="utf-8")
            written.append(dest)
        except OSError as e:
            log.warning(f"技能 {sk.name} 落盘到 {dest} 失败: {e}")
    return written
