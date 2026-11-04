#!/usr/bin/env python3
"""flow-probe：离线生成「每条需求自己的流程建议」的只读观察工具（#432）。

零写入契约（逐条说明见 README.md）：
- data/board.db 只读打开（URI mode=ro + PRAGMA query_only=ON）；
- 唯一写入根 = --out（默认 scripts/flow-probe/out/），不写 data/、~/.req-board/artifacts/、data/dispatcher_state.json；
- 不调 board API、不启动 ACP 会话（只 kimi -p 一次性调用）、不 import 仓库内任何生产模块（仅标准库）。

删除整个 scripts/flow-probe/ 目录即彻底移除。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11：模型名降级为 unknown，其余功能不变
    tomllib = None

TOOL_VERSION = "1.1"   # 1.1 = #432 代码审查后续：失败隔离补全 / 渲染后 prompt 落盘 / --out 守卫 / 差异含顺序
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_OUT = SCRIPT_DIR / "out"
DEFAULT_DB = REPO_ROOT / "data" / "board.db"
DEFAULT_ARTIFACTS_ROOT = Path.home() / ".req-board" / "artifacts"
DEFAULT_KIMI_HOME = Path.home() / ".kimi-code-deepseek"
DEFAULT_TIMEOUT = 180
DEFAULT_INTERVAL = 3.0
# 节点池排除项：analysis = 前置环节槽位；merge_resolve = 调度器合并冲突专用
EXCLUDED_ROLES = ("analysis", "merge_resolve")
# prompt.md 占位符 = 输入白名单，两者必须一致（不含 flow / flow_name / nodes_json / suggested_flow）
INPUT_KEYS = ("project", "id", "title", "notes", "repos", "analysis_md", "node_pool")
PLACEHOLDER_RE = re.compile(r"\{(project|id|title|notes|repos|analysis_md|node_pool)\}")
LEAK_KEYS = ("flow", "flow_name", "nodes_json", "node_index", "suggested_flow")
# prompt 走 argv（kimi 2.0.1 的 `-p -` 实测不读 stdin）。kimi 启动后会把进程名改写为 kimi-code，
# 全文 argv 仅在启动瞬间可见（2026-09-19 实测约 0.12s / 单次调用 43s）——仍可能与调度器
# pgrep -f "kimi acp"（#380 自动重启安全阀，15s 节奏）撞上，故渲染结果命中时提示一次
ARGV_COLLISION_LITERAL = "kimi acp"

# node_type 与角色语义（语义句出处 = prompts/<role>.md 首段，键集由 build_node_pool 对 flows.json 断言）
ROLE_SPEC = {
    "proposal": {"node_type": "artifact",
                 "semantic": "把需求落成目标/范围/验收口径（只读角色）"},
    "tech": {"node_type": "artifact",
             "semantic": "定技术路线、改动点、风险与测试方案（只读角色）"},
    "tech_review": {"node_type": "review",
                    "semantic": "审技术方案挑错，reject 打回 tech（只读，作者-评审者分离）"},
    "dev": {"node_type": "code",
            "semantic": "在独立 worktree 改代码并自测、提交"},
    "code_review": {"node_type": "review",
                    "semantic": "对照需求审 dev diff 挑错，reject 打回 dev（只读）"},
    "accept": {"node_type": "artifact",
               "semantic": "独立测试验证，对照需求与产物挑错（作者-评审者分离）"},
}


class FlowProbeError(Exception):
    """工具级错误（配置/schema/路径），fail-fast 直接退出。"""


# ==================== 小工具 ====================

def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="flow_probe.py",
        description="离线生成「每条需求自己的流程建议」（只读观察工具，仅记录不执行）")
    p.add_argument("--ids", default="", help="显式需求 id 列表（逗号分隔，覆盖 --last 选择集）")
    p.add_argument("--last", type=int, default=30, help="选择集 = 已入流程卡按 id 倒序前 N（默认 30）")
    p.add_argument("--limit", type=int, default=30, help="本次处理上限（默认 30）")
    p.add_argument("--force", action="store_true", help="已生成 req-<id>.json 时重跑覆盖")
    p.add_argument("--model", default="", help="透传 kimi -m（默认取 KIMI_CODE_HOME/config.toml 的 default_model）")
    p.add_argument("--dry-run", action="store_true", help="只渲染 prompt，不调用模型")
    p.add_argument("--out", default=str(DEFAULT_OUT), help=f"输出根（默认 {DEFAULT_OUT}）")
    p.add_argument("--db", default=str(DEFAULT_DB), help="board.db 路径（worktree 内自验须指主工作区）")
    p.add_argument("--artifacts-root", default=str(DEFAULT_ARTIFACTS_ROOT), help="analysis.md 根目录")
    p.add_argument("--self-check", action="store_true", help="内建自检（无模型、无网络、不读实库）")
    return p.parse_args(argv)


def parse_ids(raw):
    """--ids 解析（fail-fast）：含非数字项即报错，不把 ValueError 抛到 main 之外。"""
    ids = [x for x in re.split(r"[,，\s]+", raw or "") if x.strip()]
    bad = [x for x in ids if not x.isdigit()]
    if bad:
        raise FlowProbeError(f"--ids 含非数字项：{'、'.join(bad)}")
    return ids


def guard_out_root(out_root):
    """--out 守卫（零写入契约）：拒绝落在仓库 data/、.git/、~/.req-board/artifacts/ 之下或仓库根本身。"""
    root = Path(out_root).expanduser().resolve()
    for banned in (REPO_ROOT / "data", REPO_ROOT / ".git", DEFAULT_ARTIFACTS_ROOT):
        banned = Path(banned).resolve()
        if root == banned or banned in root.parents:
            raise FlowProbeError(f"--out 不允许落在 {banned}（零写入契约）：{root}")
    if root == REPO_ROOT.resolve():
        raise FlowProbeError(f"--out 不允许是仓库根目录：{root}")
    return root


def _resolve_num_env(name, default, cast, minimum=0):
    """数值型环境变量解析：非法值回落默认并提示，不抛异常中断整批。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = cast(str(raw).strip())
        if val < minimum:
            raise ValueError
        return val
    except ValueError:
        print(f"警告：{name}={raw!r} 非法，回落默认 {default}", file=sys.stderr)
        return default


def resolve_timeout():
    return _resolve_num_env("FLOW_PROBE_TIMEOUT", DEFAULT_TIMEOUT, int, minimum=1)


def resolve_interval():
    return _resolve_num_env("FLOW_PROBE_INTERVAL", DEFAULT_INTERVAL, float, minimum=0)


def warn_argv_collision(out_root, req_id):
    """注入文本含字面「kimi acp」时提示一次（实测：全文 argv 仅在调用启动瞬间可见）。

    子进程以 `kimi -p <全文>` 启动，kimi 随即把进程名改写为 `kimi-code`（argv 随之不可见）——
    2026-09-19 实测：单次调用 43s，全文 argv 可见窗口约 0.12s。该瞬时窗口内调度器 #380 自动重启
    安全阀（`pgrep -f "kimi acp"`，15s 节奏，仅在有 pending_restart 时执行）可能命中本子进程，
    命中即本轮自动重启顺延；人工 pgrep 检查同理。不影响本工具输出。"""
    msg = ("注入文本含字面「kimi acp」：调用启动瞬间（实测约 0.1s）kimi -p 子进程 argv 含全文，"
           "该窗口内调度器 #380 自动重启安全阀（pgrep -f \"kimi acp\"，15s 节奏）可能命中并顺延一轮；"
           "人工 pgrep 检查同理。不影响本工具输出。")
    print(f"警告：{msg}", file=sys.stderr)
    append_log(out_root, req_id, "warn", msg)


def _safe_path(out_root, rel):
    """写入路径必须落在 out_root 内（零写入契约的机制保证）。"""
    root = Path(out_root).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise FlowProbeError(f"输出路径越界：{target}")
    return target


def write_text(out_root, rel, text):
    target = _safe_path(out_root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def write_json_file(out_root, rel, obj):
    return write_text(out_root, rel, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append_log(out_root, req_id, kind, reason):
    line = f"{now_iso()}  req-{req_id}  {kind}  {reason}\n"
    target = _safe_path(out_root, "run.log")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line)


# ==================== 配置 / 节点池 ====================

def load_flows_config(path=None):
    path = Path(path) if path else REPO_ROOT / "flows.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FlowProbeError(f"读取流程配置失败 {path}: {e}")
    if not isinstance(cfg.get("roles"), dict):
        raise FlowProbeError(f"流程配置缺少 roles：{path}")
    return cfg


def build_node_pool(cfg, role_spec=None):
    """节点池 = flows.json.roles 去掉 analysis / merge_resolve，按 order 升序。

    display/artifact 取 flows.json（单一源），node_type/语义取工具内 ROLE_SPEC；
    键集不一致即 fail-fast（防静默漂移，提示更新工具）。
    """
    role_spec = ROLE_SPEC if role_spec is None else role_spec
    roles = cfg.get("roles") or {}
    expected = set(roles) - set(EXCLUDED_ROLES)
    if set(role_spec) != expected:
        missing = sorted(expected - set(role_spec))
        extra = sorted(set(role_spec) - expected)
        raise FlowProbeError(
            "ROLE_SPEC 与 flows.json.roles 键集不一致"
            f"（缺失 {missing} / 多余 {extra}）：请更新 scripts/flow-probe/flow_probe.py 的 ROLE_SPEC")
    pool = []
    for rid, meta in sorted(((r, m) for r, m in roles.items() if r in expected),
                            key=lambda kv: (kv[1].get("order") or 0, kv[0])):
        spec = role_spec[rid]
        artifact = meta.get("artifact") or None
        if spec["node_type"] in ("artifact", "review") and not artifact:
            raise FlowProbeError(f"角色 {rid} node_type={spec['node_type']} 但 flows.json 未配 artifact 产物契约")
        pool.append({"role": rid, "display": meta.get("display") or rid, "artifact": artifact,
                     "node_type": spec["node_type"], "semantic": spec["semantic"], "order": meta.get("order")})
    return pool


def render_node_pool_text(pool):
    lines = []
    for r in pool:
        artifact = f"｜产物 {r['artifact']}" if r["artifact"] else ""
        lines.append(f"- `{r['role']}`（{r['display']}，{r['node_type']}）{artifact}：{r['semantic']}")
    return "\n".join(lines)


def role_prompts_sha256(cfg):
    """六份角色提示词拼接哈希：语义副本/提示词漂移的追溯锚点。"""
    h = hashlib.sha256()
    for rid in sorted(build_node_pool(cfg), key=lambda r: r["role"]):
        pf = (cfg["roles"].get(rid["role"]) or {}).get("prompt_file") or ""
        path = REPO_ROOT / pf
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()


def read_prompt_template(path=None):
    path = Path(path) if path else SCRIPT_DIR / "prompt.md"
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as e:
        raise FlowProbeError(f"读取 prompt 模板失败 {path}: {e}")
    missing = [k for k in INPUT_KEYS if f"{{{k}}}" not in template]
    if missing:
        raise FlowProbeError(f"prompt 模板缺少占位符 {missing}：{path}")
    return template


# ==================== 数据库（只读） ====================

def open_ro(db_path):
    path = Path(db_path).expanduser()
    if not path.exists():
        raise FlowProbeError(f"看板库不存在：{path}（dev worktree 内自验请用 --db <主工作区>/data/board.db）")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def require_schema(conn):
    need = {"requirements": ("id", "project", "title", "notes", "flow", "flow_name"),
            "dispatcher_events": ("req_id", "kind", "content", "ts")}
    for table, cols in need.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            raise FlowProbeError(f"数据库缺少表 {table}（schema 漂移，请核对 --db）")
        missing = [c for c in cols if c not in existing]
        if missing:
            raise FlowProbeError(f"表 {table} 缺列 {missing}（schema 漂移，请核对 --db）")


def select_requirements(conn, ids, last, limit):
    """候选集：已入流程（flow 非空）卡，id 倒序；--ids 覆盖 --last；--limit 截断处理上限。"""
    cards = []
    if ids:
        for rid in sorted({int(x) for x in ids}, reverse=True):
            row = conn.execute(
                "SELECT id, project, title, notes, flow, flow_name FROM requirements WHERE id=?",
                (rid,)).fetchone()
            cards.append(dict(row) if row else {"id": rid, "missing": True})
    else:
        rows = conn.execute(
            "SELECT id, project, title, notes, flow, flow_name FROM requirements "
            "WHERE flow IS NOT NULL AND flow <> '' ORDER BY id DESC LIMIT ?", (int(last),)).fetchall()
        cards = [dict(r) for r in rows]
    return cards[: int(limit)]


def load_baseline(conn, req_id):
    """基线（卡上快照 + 最新 flow_choice 事件理由）——在模型调用之后读取，写入同一份输出。"""
    row = conn.execute("SELECT flow, flow_name FROM requirements WHERE id=?", (req_id,)).fetchone()
    flow = row["flow"] if row else None
    flow_name = row["flow_name"] if row else None
    reason = None
    choice_ts = None
    try:
        ev = conn.execute(
            "SELECT content, ts FROM dispatcher_events WHERE req_id=? AND kind='flow_choice' "
            "ORDER BY id DESC LIMIT 1", (req_id,)).fetchone()
        if ev:
            reason, choice_ts = ev["content"], ev["ts"]
    except sqlite3.Error:
        reason, choice_ts = None, None
    return {"flow": flow, "flow_name": flow_name, "reason": reason, "choice_ts": choice_ts}


# ==================== 输入构造与渲染 ====================

def parse_notes_repos(notes):
    """notes 中人工声明的 `repos: a, b` 行（与 dispatcher.parse_notes_repos 同口径）。"""
    for line in (notes or "").splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("repos:"):
            continue
        value = stripped[len("repos:"):].strip()
        if not value:
            continue
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except ValueError:
            pass
        return [x.strip() for x in re.split(r"[,，\s]+", value) if x.strip()]
    return []


def load_repos(state_path, req_id, notes):
    """涉及仓库：dispatcher_state.json 的 req_repos 优先（调度器落库口径），缺失回落 notes 声明。"""
    repos = []
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        repos = (state.get("req_repos") or {}).get(str(req_id)) or []
    except (OSError, ValueError):
        repos = []
    if not repos:
        repos = parse_notes_repos(notes)
    return [str(r) for r in repos if str(r).strip()]


def load_analysis(artifacts_root, project, req_id):
    path = Path(artifacts_root) / project / f"req-{req_id}" / "analysis.md"
    try:
        return path, path.read_text(encoding="utf-8")
    except OSError:
        return path, None


def build_input(card, repos, analysis_text, pool):
    """输入白名单唯一构造入口：不含 flow / flow_name / nodes_json / suggested_flow。"""
    ctx = {
        "project": card["project"],
        "id": card["id"],
        "title": card["title"],
        "notes": card.get("notes") or "",
        "repos": "、".join(repos) if repos else "（未识别/未声明）",
        "analysis_md": analysis_text,
        "node_pool": render_node_pool_text(pool),
    }
    leaked = [k for k in LEAK_KEYS if k in ctx] + [k for k in ctx if k not in INPUT_KEYS]
    if leaked:
        raise FlowProbeError(f"输入白名单违规：{leaked}")
    return ctx


def render_prompt(template, ctx):
    extra = [k for k in ctx if k not in INPUT_KEYS]
    if extra:
        raise FlowProbeError(f"渲染上下文含白名单外键：{extra}")
    return PLACEHOLDER_RE.sub(lambda m: str(ctx[m.group(1)]), template)


# ==================== 模型调用 ====================

def resolve_kimi():
    return os.environ.get("KIMI_BIN") or shutil.which("kimi") or str(Path.home() / ".kimi-code/bin/kimi")


def read_default_model(kimi_home):
    if tomllib is None:
        return "unknown"
    try:
        with open(Path(kimi_home) / "config.toml", "rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, ValueError):
        return "unknown"
    model = cfg.get("default_model")
    return str(model) if model else "unknown"


def call_model(kimi_bin, prompt, model, timeout, output_format, cwd):
    """kimi -p 一次性调用（非 ACP）。超时/失败由调用方记 invalid，不在本函数重试。"""
    cmd = [kimi_bin, "-p", prompt]
    if model:
        cmd += ["-m", model]
    if output_format == "stream-json":
        cmd += ["--output-format", "stream-json"]
    env = os.environ.copy()
    env.setdefault("KIMI_CODE_HOME", str(DEFAULT_KIMI_HOME))
    try:
        # start_new_session：子进程自成会话/进程组，超时可整组回收（不留 sleep 类孤儿孙进程）
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                errors="replace", start_new_session=True)
    except OSError as e:
        return {"ok": False, "reason": f"调用失败：{e}", "stdout": "", "stderr": ""}
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_child_session(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return {"ok": False, "reason": f"调用超时（>{timeout}s）",
                "stdout": out or "", "stderr": err or ""}
    return {"ok": True, "rc": proc.returncode, "stdout": out, "stderr": err}


def _kill_child_session(proc):
    """超时兜底：回收本次调用创建的独立会话（仅影响该子进程组，不触碰任何他人进程）。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


def _strip_bullets(text):
    return "\n".join(re.sub(r"^[•·]\s?", "", ln) for ln in text.splitlines()).strip()


def _try_load(text):
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _strip_fence(text):
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    return m.group(1).strip() if m else None


def _slice_braces(text):
    i, j = text.find("{"), text.rfind("}")
    return text[i:j + 1] if 0 <= i < j else None


def extract_json_stdout(stdout, output_format="text"):
    """三步提取：① 剥 `• ` 前缀整体解析 → ② 去 ``` 围栏 → ③ 首 { 到末 } 切片。失败返回 None。"""
    text = stdout
    if output_format == "stream-json":
        chunks = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("role") == "assistant" and isinstance(obj.get("content"), str):
                chunks.append(obj["content"])
        text = "\n".join(chunks)
    text = _strip_bullets(text or "")
    for candidate in (text, _strip_fence(text), _slice_braces(text)):
        parsed = _try_load(candidate)
        if parsed is not None:
            return parsed
    return None


# ==================== 干跑校验 ====================

def validate_proposal(prop, pool):
    """返回 (checks, structure_errors)。checks 四项 bool；structure_errors = JSON 结构问题清单。"""
    pool_roles = [r["role"] for r in pool]
    ntype = {r["role"]: r["node_type"] for r in pool}
    errors = []
    nodes = prop.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append("nodes 缺失或非列表")
        nodes = []
    roles = []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict) or not isinstance(node.get("role"), str) or not node["role"].strip():
            errors.append(f"nodes[{i}].role 非法")
            roles.append(None)
            continue
        if not isinstance(node.get("why"), str) or not node["why"].strip():
            errors.append(f"nodes[{i}].why 缺失")
        roles.append(node["role"].strip())
    if not isinstance(prop.get("reason"), str) or not prop["reason"].strip():
        errors.append("reason 缺失或非字符串")
    omitted = prop.get("omitted")
    if not isinstance(omitted, list) or not omitted:
        errors.append("omitted 缺失或为空（必填：需说明刻意省略的角色）")
    else:
        for i, item in enumerate(omitted):
            if (not isinstance(item, dict) or not str(item.get("role") or "").strip()
                    or not str(item.get("why_not") or "").strip()):
                errors.append(f"omitted[{i}] 需同时含 role 与 why_not")
    if not isinstance(prop.get("missing_capability", ""), str):
        errors.append("missing_capability 非字符串")
    assumptions = prop.get("assumptions", [])
    if not isinstance(assumptions, list) or any(not isinstance(a, str) for a in assumptions):
        errors.append("assumptions 非字符串数组")
    valid_roles = [r for r in roles if r]
    checks = {
        "roles_in_pool": bool(roles) and all(r is not None and r in pool_roles for r in roles),
        "no_dup_role": len(valid_roles) == len(set(valid_roles)),
        "count_1_5": 1 <= len(nodes) <= 5,
        "review_predecessor": all(
            ntype.get(roles[i]) != "review" or (i > 0 and ntype.get(roles[i - 1]) in ("artifact", "code"))
            for i in range(len(roles)) if roles[i]),
    }
    return checks, errors


def build_proposal(prop, pool):
    """校验通过后补齐 node_type/artifact 并在链尾追加唯一 terminal 节点。"""
    ntype = {r["role"]: r["node_type"] for r in pool}
    artifact = {r["role"]: r["artifact"] for r in pool}
    nodes = [{"role": n["role"], "node_type": ntype[n["role"]], "artifact": artifact.get(n["role"]),
              "why": n.get("why")} for n in prop["nodes"]]
    nodes.append({"role": None, "node_type": "terminal", "artifact": None, "why": None})
    return {"nodes": nodes, "raw_roles": [n["role"] for n in prop["nodes"]],
            "reason": prop.get("reason"), "omitted": prop.get("omitted"),
            "missing_capability": prop.get("missing_capability", ""),
            "assumptions": prop.get("assumptions", [])}


# ==================== 记录 / 汇总 ====================

def process_card(card, ctx, prompt_sha, res, checks, status, invalid_reason, proposal_out,
                 baseline, pool, meta):
    return {
        "req_id": card["id"],
        "status": status,
        "invalid_reason": invalid_reason,
        "proposal": proposal_out,
        "checks": checks,
        "inputs": {
            "project": card["project"],
            "title": card["title"],
            "notes_sha": sha256_text(ctx["notes"]),
            "analysis_path": str(meta["analysis_path"]),
            "analysis_sha": sha256_text(ctx["analysis_md"]),
            "repos": ctx["repos"],
            "node_pool": [{"role": r["role"], "display": r["display"], "artifact": r["artifact"],
                           "node_type": r["node_type"]} for r in pool],
        },
        "baseline": baseline,
        "meta": {
            "model": meta["model"],
            "model_source": meta["model_source"],
            "prompt_sha": prompt_sha,
            "prompt_file": f"req-{card['id']}.prompt.md",
            "template_sha": meta["template_sha"],
            "config_sha": meta["config_sha"],
            "role_prompts_sha": meta["role_prompts_sha"],
            "run_ts": meta["run_ts"],
            "duration_ms": meta["duration_ms"],
            "tool_version": TOOL_VERSION,
        },
        "raw": {"stdout_head": (res.get("stdout") or "")[:2000],
                "stderr_head": (res.get("stderr") or "")[:2000]},
    }


def format_baseline(flow, flow_name):
    if not flow:
        return "-"
    return f"{flow}（{flow_name}）" if flow_name else f"{flow}（-）"


def baseline_roles(cfg, flow_id):
    for f in cfg.get("flows") or []:
        if f.get("id") == flow_id:
            return [n.get("role") for n in (f.get("nodes") or []) if n.get("role")]
    return None


def diff_seq(base_roles, prop_roles):
    """差异口径：角色集合差 + 顺序差（顺序不同不再显示为「集合一致」——序列顺序正是观察对象）。"""
    if base_roles is None:
        return "基线流程已退役，无法对照"
    added = [r for r in prop_roles if r not in base_roles]
    removed = [r for r in base_roles if r not in prop_roles]
    if not added and not removed:
        if list(base_roles) == list(prop_roles):
            return "= 完全一致"
        return f"=（顺序不同：{'→'.join(base_roles)} vs {'→'.join(prop_roles)}）"
    parts = []
    if added:
        parts.append("+" + "、".join(added))
    if removed:
        parts.append("-" + "、".join(removed))
    return " ".join(parts)


def write_summary(out_root, records, meta, cfg):
    ok = sum(1 for r in records if r["status"] == "ok")
    lines = [
        "# flow-probe 运行汇总",
        "",
        f"- 运行时间：{meta['run_ts']}",
        f"- 样本数：{len(records)}（ok {ok} / invalid {len(records) - ok}）",
        f"- 模型：{meta['model']}（{meta['model_source']}）",
        f"- config_sha：`{meta['config_sha']}`　template_sha：`{meta['template_sha']}`",
        f"- role_prompts_sha：`{meta['role_prompts_sha']}`　tool_version：{TOOL_VERSION}",
        "",
        "| # | 项目 | 标题 | 基线 | 提议序列 | 差异 | 状态 |",
        "|:--|:--|:--|:--|:--|:--|:--|",
    ]
    for r in records:
        prop = r.get("proposal") or {}
        roles = prop.get("raw_roles") or []
        seq = "→".join(roles) if roles else "-"
        base = format_baseline((r.get("baseline") or {}).get("flow"), (r.get("baseline") or {}).get("flow_name"))
        diff = diff_seq(baseline_roles(cfg, (r.get("baseline") or {}).get("flow")), roles) if roles else "-"
        status = "ok" if r["status"] == "ok" else f"invalid（{r['invalid_reason']}）"
        title = (r["inputs"]["title"] or "").replace("|", "\\|")[:40]
        lines.append(f"| {r['req_id']} | {r['inputs']['project']} | {title} | {base} | {seq} | {diff} | {status} |")
    write_text(out_root, "SUMMARY.md", "\n".join(lines) + "\n")


def write_raw(out_root, rel, stdout, stderr):
    body = f"===== stdout =====\n{stdout}\n===== stderr =====\n{stderr}\n"
    return write_text(out_root, rel, body)


# ==================== 主流程 ====================

def do_dry_run(cards, args, out_root, pool, template):
    print(f"节点池（来自 flows.json，排除 {'/'.join(EXCLUDED_ROLES)}；config_sha={sha256_file(REPO_ROOT / 'flows.json')[:12]}）：")
    for r in pool:
        print(f"  {r['role']:12s} {r['display']:6s} {r['node_type']:8s} {r['artifact'] or '-'}")
    artifacts_root = Path(args.artifacts_root).expanduser()
    print(f"\n选择集（{len(cards)} 张，--last={args.last} --limit={args.limit} ids={args.ids or '-'}）：")
    for card in cards:
        if card.get("missing"):
            print(f"  req-{card['id']}  跳过：需求不存在")
            continue
        if not card.get("flow"):
            print(f"  req-{card['id']}  跳过：未进入流程（flow 为空）")
            continue
        path, text = load_analysis(artifacts_root, card["project"], card["id"])
        if text is None:
            print(f"  req-{card['id']}  跳过：无 analysis.md（{path}）")
            continue
        repos = load_repos(Path(args.db).expanduser().resolve().parent / "dispatcher_state.json",
                           card["id"], card.get("notes"))
        ctx = build_input(card, repos, text, pool)
        rendered = render_prompt(template, ctx)
        target = write_text(out_root, f"dry-run/req-{card['id']}.prompt.md", rendered)
        print(f"  req-{card['id']}  已渲染 → {target}")
        print(f"    inputs={','.join(INPUT_KEYS)}；渲染前断言：无 {'/'.join(LEAK_KEYS)} 泄漏")
    print("\n--dry-run 完成：未调用模型、未写 run.log / SUMMARY.md / req-<id>.json")
    return 0


def do_run(cards, conn, args, out_root, cfg, pool, template):
    meta_base = {
        "template_sha": sha256_text(template),
        "config_sha": sha256_file(REPO_ROOT / "flows.json"),
        "role_prompts_sha": role_prompts_sha256(cfg),
        "model": args.model or read_default_model(os.environ.get("KIMI_CODE_HOME", str(DEFAULT_KIMI_HOME))),
        "model_source": "--model" if args.model else "config-default",
        "run_ts": now_iso(),
    }
    kimi_bin = resolve_kimi()
    timeout = resolve_timeout()
    interval = resolve_interval()
    output_format = os.environ.get("FLOW_PROBE_OUTPUT_FORMAT", "text")
    state_path = Path(args.db).expanduser().resolve().parent / "dispatcher_state.json"
    artifacts_root = Path(args.artifacts_root).expanduser()
    records = []
    workdir = None
    # 未评估 ≠ 校验不过：调用失败 / 无 JSON 时四项全 None（未评估）；只有真跑了干跑校验才给 true/false。
    # 旧实现一律 False，会把「没评估」误读成「校验不过」（#432 代码审查第 5 条）。
    empty_checks = {k: None for k in ("roles_in_pool", "no_dup_role", "count_1_5", "review_predecessor")}
    warned_argv = False
    try:
        workdir = tempfile.mkdtemp(prefix="flow-probe-")
        first = True
        for card in cards:
            rid = card["id"]
            if card.get("missing"):
                append_log(out_root, rid, "skip", "需求不存在")
                print(f"req-{rid}  跳过  需求不存在")
                continue
            if not card.get("flow"):
                append_log(out_root, rid, "skip", "未进入流程（flow 为空）")
                print(f"req-{rid}  跳过  未进入流程")
                continue
            analysis_path, analysis_text = load_analysis(artifacts_root, card["project"], rid)
            if analysis_text is None:
                append_log(out_root, rid, "skip", f"无 analysis.md（{analysis_path}）")
                print(f"req-{rid}  跳过  无 analysis.md")
                continue
            if _safe_path(out_root, f"req-{rid}.json").exists() and not args.force:
                append_log(out_root, rid, "skip", "已生成（--force 覆盖）")
                print(f"req-{rid}  跳过  已生成")
                continue
            repos = load_repos(state_path, rid, card.get("notes"))
            ctx = build_input(card, repos, analysis_text, pool)
            prompt = render_prompt(template, ctx)
            prompt_sha = sha256_text(prompt)
            # 渲染后全文落盘（样本可复原）：notes/analysis 事后被编辑、产物被清理时仍能复现本次输入
            write_text(out_root, f"req-{rid}.prompt.md", prompt)
            if not warned_argv and ARGV_COLLISION_LITERAL in prompt:
                warned_argv = True
                warn_argv_collision(out_root, rid)
            if not first:
                time.sleep(interval)
            first = False
            started = time.time()
            res = call_model(kimi_bin, prompt, args.model, timeout, output_format, cwd=workdir)
            duration_ms = int((time.time() - started) * 1000)
            checks, invalid_reason, proposal_out = empty_checks, None, None
            if not res["ok"]:
                invalid_reason = res["reason"]
            elif res["rc"] != 0:
                invalid_reason = f"调用失败：kimi 退出码 {res['rc']}"
            else:
                parsed = extract_json_stdout(res["stdout"], output_format)
                if parsed is None:
                    invalid_reason = "输出非法 JSON"
                else:
                    checks, struct_errors = validate_proposal(parsed, pool)
                    failed = [k for k, v in checks.items() if not v]
                    if struct_errors or failed:
                        parts = []
                        if struct_errors:
                            parts.append("结构非法：" + "；".join(struct_errors))
                        if failed:
                            parts.append("干跑校验不过：" + ",".join(failed))
                        invalid_reason = "｜".join(parts)
                    else:
                        proposal_out = build_proposal(parsed, pool)
            status = "ok" if invalid_reason is None else "invalid"
            baseline = load_baseline(conn, rid)
            meta = dict(meta_base, analysis_path=analysis_path, model=args.model or meta_base["model"],
                        duration_ms=duration_ms)
            rec = process_card(card, ctx, prompt_sha, res, checks, status, invalid_reason,
                               proposal_out, baseline, pool, meta)
            write_json_file(out_root, f"req-{rid}.json", rec)
            raw_path = _safe_path(out_root, f"req-{rid}.raw.txt")
            if status != "ok":
                write_raw(out_root, f"req-{rid}.raw.txt", res.get("stdout") or "", res.get("stderr") or "")
                append_log(out_root, rid, "invalid", invalid_reason)
                print(f"req-{rid}  invalid  {invalid_reason}")
            else:
                if raw_path.exists():   # --force 重跑转 ok 后清掉上一轮残留（防旧现场误导）
                    raw_path.unlink()
                append_log(out_root, rid, "ok", "→".join(proposal_out["raw_roles"]))
                print(f"req-{rid}  ok  {'→'.join(proposal_out['raw_roles'])}")
            records.append(rec)
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    if records:
        write_summary(out_root, records, meta_base, cfg)
    ok = sum(1 for r in records if r["status"] == "ok")
    print(f"\n完成：处理 {len(records)} 张（ok {ok} / invalid {len(records) - ok}）→ {out_root}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    if args.self_check:
        return self_check()
    try:
        out_root = guard_out_root(args.out)
        ids = parse_ids(args.ids)
        cfg = load_flows_config()
        pool = build_node_pool(cfg)
        template = read_prompt_template()
    except FlowProbeError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    conn = None
    try:
        conn = open_ro(args.db)
        require_schema(conn)
        cards = select_requirements(conn, ids, args.last, args.limit)
    except (FlowProbeError, sqlite3.Error) as e:
        print(f"错误：{e}", file=sys.stderr)
        if conn:
            conn.close()
        return 2
    try:
        if args.dry_run:
            return do_dry_run(cards, args, out_root, pool, template)
        return do_run(cards, conn, args, out_root, cfg, pool, template)
    finally:
        conn.close()


# ==================== 内建自检（无模型 / 无网络 / 不读实库） ====================

def self_check():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001 - 自检需把任何断言失败收敛为 FAIL
            failures.append(name)
            print(f"FAIL  {name}: {e}")

    cfg = load_flows_config()
    pool = build_node_pool(cfg)
    template = read_prompt_template()

    def t_pool():
        roles = [r["role"] for r in pool]
        assert roles == ["proposal", "tech", "tech_review", "dev", "code_review", "accept"], roles
        orders = [r["order"] for r in pool]
        assert orders == sorted(orders), orders
        for r in pool:
            assert r["display"] == cfg["roles"][r["role"]]["display"], r
            assert r["artifact"] == cfg["roles"][r["role"]]["artifact"], r
        assert "analysis" not in roles and "merge_resolve" not in roles

    def t_role_spec_keyset():
        bad = dict(ROLE_SPEC, ghost={"node_type": "artifact", "semantic": "x"})
        try:
            build_node_pool(cfg, bad)
        except FlowProbeError:
            pass
        else:
            raise AssertionError("多余角色未 fail-fast")
        missing = {k: v for k, v in ROLE_SPEC.items() if k != "dev"}
        try:
            build_node_pool(cfg, missing)
        except FlowProbeError:
            pass
        else:
            raise AssertionError("缺失角色未 fail-fast")
        bad_type = dict(ROLE_SPEC, dev={"node_type": "artifact", "semantic": "x"})
        bad_cfg = json.loads(json.dumps(cfg))
        del bad_cfg["roles"]["dev"]["artifact"]
        try:
            build_node_pool(bad_cfg, bad_type)
        except FlowProbeError:
            pass
        else:
            raise AssertionError("node_type=artifact 无产物契约未 fail-fast")

    def valid_prop():
        return {"nodes": [{"role": "tech", "why": "定接口"}, {"role": "dev", "why": "实现"},
                          {"role": "code_review", "why": "审 diff"}, {"role": "accept", "why": "验证"}],
                "reason": "先对齐方案再实现", "omitted": [{"role": "proposal", "why_not": "无产品面"}],
                "missing_capability": "", "assumptions": ["不确定点"]}

    def t_validate_matrix():
        checks, errors = validate_proposal(valid_prop(), pool)
        assert all(checks.values()) and not errors, (checks, errors)
        bad = valid_prop()
        bad["nodes"][0]["role"] = "ghost"
        checks, _ = validate_proposal(bad, pool)
        assert checks["roles_in_pool"] is False, checks
        bad = valid_prop()
        bad["nodes"][1]["role"] = "tech"
        checks, _ = validate_proposal(bad, pool)
        assert checks["no_dup_role"] is False, checks
        bad = valid_prop()
        bad["nodes"] = valid_prop()["nodes"] * 2
        checks, _ = validate_proposal(bad, pool)
        assert checks["count_1_5"] is False, checks
        bad = valid_prop()
        bad["nodes"] = [{"role": "code_review", "why": "x"}] + valid_prop()["nodes"]
        checks, _ = validate_proposal(bad, pool)
        assert checks["review_predecessor"] is False, checks
        bad = valid_prop()
        bad["nodes"] = [{"role": "tech", "why": "x"}, {"role": "tech_review", "why": "y"},
                        {"role": "code_review", "why": "z"}]
        checks, _ = validate_proposal(bad, pool)
        assert checks["review_predecessor"] is False, checks
        bad = valid_prop()
        del bad["omitted"]
        checks, errors = validate_proposal(bad, pool)
        assert any("omitted" in e for e in errors), errors
        bad = valid_prop()
        del bad["nodes"][1]["why"]
        _, errors = validate_proposal(bad, pool)
        assert any("why" in e for e in errors), errors

    def t_extract():
        prop = json.dumps({"a": 1}, ensure_ascii=False)
        assert extract_json_stdout("• " + prop + "\n") == {"a": 1}
        assert extract_json_stdout("```json\n" + prop + "\n```\n") == {"a": 1}
        assert extract_json_stdout("思考中…\n" + prop + "\n以上。\n") == {"a": 1}
        assert extract_json_stdout("完全不是 JSON") is None
        assert extract_json_stdout("[1,2,3]") is None
        assert extract_json_stdout('{"role":"assistant","content":"' + prop.replace('"', '\\"') + '"}',
                                   "stream-json") == {"a": 1}

    def t_build_proposal():
        out = build_proposal(valid_prop(), pool)
        assert [n["node_type"] for n in out["nodes"]] == ["artifact", "code", "review", "artifact", "terminal"]
        assert [n["artifact"] for n in out["nodes"]] == ["tech.md", None, "code_review.md", "accept.md", None]
        assert out["nodes"][-1]["role"] is None and sum(1 for n in out["nodes"] if n["node_type"] == "terminal") == 1
        assert out["raw_roles"] == ["tech", "dev", "code_review", "accept"]

    def t_input_whitelist():
        card = {"id": 7, "project": "demo", "title": "t", "notes": "n",
                "flow": "heavy", "flow_name": "SENTINEL_FLOW_NAME", "nodes_json": "SENTINEL_NODES"}
        ctx = build_input(card, [], "ANALYSIS", pool)
        assert set(ctx) == set(INPUT_KEYS), set(ctx)
        rendered = render_prompt(template, ctx)
        assert "SENTINEL" not in rendered and "SENTINEL" not in json.dumps(ctx, ensure_ascii=False)
        assert "SENTINEL" not in ctx["node_pool"]
        try:
            render_prompt(template, dict(ctx, flow_name="x"))
        except FlowProbeError:
            pass
        else:
            raise AssertionError("白名单外键未拒绝")

    def t_repos_parse():
        assert parse_notes_repos("repos: a, b") == ["a", "b"]
        assert parse_notes_repos('repos: ["a","b"]') == ["a", "b"]
        assert parse_notes_repos("无声明") == []
        assert parse_notes_repos(None) == []

    def t_baseline_degrade():
        assert format_baseline("lean", None) == "lean（-）"
        assert format_baseline("standard", "标准") == "standard（标准）"
        assert format_baseline(None, None) == "-"
        assert baseline_roles(cfg, "no-such-flow") is None
        assert diff_seq(None, ["tech"]) == "基线流程已退役，无法对照"
        assert diff_seq(["tech", "dev"], ["tech", "dev"]) == "= 完全一致"
        assert diff_seq(["tech", "dev"], ["dev", "tech"]).startswith("=（顺序不同")
        assert diff_seq(["dev", "code_review"], ["tech", "dev", "accept"]) == "+tech、accept -code_review"

    def t_ids_and_out_guard():
        assert parse_ids("432, 431  430") == ["432", "431", "430"]
        assert parse_ids("") == []
        for bad in ("abc", "432,abc", "43a", "a b"):
            try:
                parse_ids(bad)
            except FlowProbeError:
                pass
            else:
                raise AssertionError(f"非法 --ids 未拒绝：{bad}")
        assert guard_out_root("/tmp/flow-probe-out") == Path("/tmp/flow-probe-out").resolve()
        assert guard_out_root(DEFAULT_OUT) == DEFAULT_OUT.resolve()
        for bad in (REPO_ROOT / "data", REPO_ROOT / "data" / "sub", REPO_ROOT, REPO_ROOT / ".git",
                    DEFAULT_ARTIFACTS_ROOT, DEFAULT_ARTIFACTS_ROOT / "x"):
            try:
                guard_out_root(bad)
            except FlowProbeError:
                pass
            else:
                raise AssertionError(f"越界 --out 未拒绝：{bad}")

    def t_env_fallback():
        keys = ("FLOW_PROBE_TIMEOUT", "FLOW_PROBE_INTERVAL")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["FLOW_PROBE_TIMEOUT"] = "abc"
            os.environ["FLOW_PROBE_INTERVAL"] = "-1"
            assert resolve_timeout() == DEFAULT_TIMEOUT
            assert resolve_interval() == DEFAULT_INTERVAL
            os.environ["FLOW_PROBE_TIMEOUT"] = "30"
            os.environ["FLOW_PROBE_INTERVAL"] = "0.5"
            assert resolve_timeout() == 30 and resolve_interval() == 0.5
            os.environ.pop("FLOW_PROBE_TIMEOUT")
            assert resolve_timeout() == DEFAULT_TIMEOUT
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def t_paths():
        root = Path(tempfile.mkdtemp(prefix="flow-probe-selfcheck-"))
        try:
            write_text(root, "a/b.txt", "x")
            assert (root / "a" / "b.txt").read_text() == "x"
            try:
                _safe_path(root, "../escape.txt")
            except FlowProbeError:
                pass
            else:
                raise AssertionError("越界路径未拒绝")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    check("节点池派生（6 角色 / order / display / artifact 与 flows.json 一致）", t_pool)
    check("ROLE_SPEC 键集 fail-fast（多余 / 缺失 / 产物契约缺失）", t_role_spec_keyset)
    check("干跑校验矩阵（合法 / 池外 / 重复 / 超 5 / review 前驱 / omitted 缺失 / why 缺失）", t_validate_matrix)
    check("extract_json 三分支 + stream-json + 垃圾输入", t_extract)
    check("node_type 映射与 terminal 唯一追加", t_build_proposal)
    check("输入白名单（SENTINEL 哨兵不进入渲染与上下文）", t_input_whitelist)
    check("notes repos 声明解析", t_repos_parse)
    check("基线降级（NULL flow_name / 退役流程 / 差异计算含顺序）", t_baseline_degrade)
    check("--ids 解析与 --out 零写入守卫（越界 fail-fast）", t_ids_and_out_guard)
    check("数值型环境变量非法值回落默认（不中断整批）", t_env_fallback)
    check("输出路径越界拒绝", t_paths)
    print(f"\n自检结果：{'全部通过' if not failures else '失败 ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
