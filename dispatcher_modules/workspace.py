"""工作区与路径：项目工作目录、需求产物目录、per-requirement worktree 路径与分支名、需求涉及仓库解析。"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from storage_time import format_storage_time

from dispatcher_modules.runtime import D, log, subprocess


def workdir_for(project):
    """单 paths 项目直连仓库本体；多 paths 项目用聚合目录"""
    cfg = D.PROJECTS[project]
    if len(cfg["paths"]) == 1:
        return Path(cfg["paths"][0])
    return D.WORKTREES_ROOT / project

def artifact_dir(req):
    """评审/开发产物目录：统一在 ~/.req-board/artifacts/<project>/req-<id>/（不入任何仓库/worktree）"""
    return D.ARTIFACTS_ROOT / req["project"] / f"req-{req['id']}"

def result_file_path(req):
    """结果 JSON 按需求隔离（并发 agent 互不覆盖）：产物目录 result.json"""
    return D.artifact_dir(req) / "result.json"

def issue_responses_path(req, author_role):
    """作者对评审 issues 的逐条回应（result.json 的 issue_responses）落盘位置：result.json 读后即删，
    复审评审靠这份文件核对上一轮问题（按作者角色分文件，tech / dev 各自一份）。"""
    return D.artifact_dir(req) / f"issue_responses-{author_role}.json"

def parse_notes_repos(notes):
    """从需求 notes 提取人工声明的涉及仓库清单：`repos: a, b` 行（逗号/空白分隔），
    兼容 JSON 数组写法 `repos: ["a","b"]`。无声明返回 []。人工声明覆盖 analysis 识别。"""
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
        except Exception:
            pass
        return [x.strip() for x in re.split(r"[,，\s]+", value) if x.strip()]
    return []

def validate_repos(project, names):
    """过滤出属于项目 paths 的仓库名（basename 匹配），非法名忽略并警告；去重保序。"""
    valid = {Path(p).name for p in D.PROJECTS[project]["paths"]}
    out = []
    seen = set()
    for n in (names or []):
        n = str(n).strip()
        if not n or n in seen:
            continue
        if n not in valid:
            log.warning(f"项目 {project} 涉及仓库名 {n!r} 不在 paths 清单中，已忽略")
            continue
        seen.add(n)
        out.append(n)
    return out

def outside_repos(project, names):
    """#303 防复发：声明/识别的涉及仓库名中不在项目 paths 清单内的部分（目录名比对，去重保序）。
    清单外名字 = 疑似建错项目（需求实际属于其它项目）或项目仓库清单缺配置的信号，
    供 analysis→dev 闸门拦截（免确认 auto 也转通道确认让人工把关）。"""
    valid = {Path(p).name for p in D.PROJECTS[project]["paths"]}
    out, seen = [], set()
    for n in (names or []):
        n = str(n).strip()
        if n and n not in valid and n not in seen:
            seen.add(n)
            out.append(n)
    return out

def effective_repos(req):
    """需求实际生效的涉及仓库清单（目录名列表）：notes 人工声明 > analysis 产物 > []（全量兜底）。"""
    notes_repos = parse_notes_repos(req.get("notes"))
    if notes_repos:
        return validate_repos(req["project"], notes_repos)
    return list(D.state.get("req_repos", {}).get(str(req["id"]), []) or [])

# TAPD 链接形态（stories 与 bugs 两种）：https://www.tapd.cn/<workspace>/prong/stories/view/<id>
# 或 https://www.tapd.cn/<workspace>/bugtrace/bugs/view/<id>；提取末尾的需求/缺陷 ID
_TAPD_ID_RE = re.compile(r"tapd\.cn/\d+/(?:prong/stories/view|bugtrace/bugs/view)/(\d+)")

def tapd_id_from_notes(req):
    """从需求 notes 提取 TAPD 需求/缺陷 ID（Bug转需求类需求建卡时 notes 常带 TAPD 链接）；
    无链接返回 None。"""
    m = _TAPD_ID_RE.search(req.get("notes") or "")
    return m.group(1) if m else None

def dev_branch(req):
    """开发分支名（命名规范）：f-YYYYMMDD-<req_id>，日期取需求创建日；
    需求来自 TAPD（notes 含 TAPD 链接）时追加 -tapd<需求ID>，便于分支与 TAPD 需求对应。"""
    d = str(req.get("created_at") or "")[:10].replace("-", "")
    if not d:
        d = format_storage_time()[:10].replace("-", "")
    branch = f"f-{d}-{req['id']}"
    tid = tapd_id_from_notes(req)
    if tid:
        branch += f"-tapd{tid}"
    return branch

def dev_worktree_path(req, repo):
    """单个仓库的 dev worktree 路径"""
    return D.WORKTREES_ROOT / req["project"] / "dev" / f"req-{req['id']}" / Path(repo).name

# #420：测试进程短路判据的基准值（import 时捕获，与 scripts/test_isolation_audit.py 判据一致）。
# 用例把 WORKTREES_ROOT / dev_worktree_path 指到临时目录即视为「已隔离」→ 照走真实 git 逻辑
# （test_worktree_base_stability / test_worktree_local_config 是真实分支/基线语义的回归锁）。
# 判据用函数身份比较（is）：将来若包装/装饰 dev_worktree_path 必须同步这里，
# 否则短路静默失效（方向是回到真跑 git）。
_REAL_WORKTREES_ROOT = D.WORKTREES_ROOT
_REAL_DEV_WORKTREE_PATH = dev_worktree_path


def in_test_env():
    """#420：测试进程判定——REQ_BOARD_TEST=1（显式逃生/脚本用）或 unittest 已加载（覆盖全部套件）。
    生产进程两者皆假 → 恒 False（tests/test_state_isolation_guard.py T9 用真实 venv 子进程锁定）。"""
    return os.environ.get("REQ_BOARD_TEST") == "1" or "unittest" in sys.modules

def _default_branch(repo, cfg=None):
    """仓库主干起点探测（离线可用）：projects.json 项目可选配置 base_branch 优先
    （如某项目的开发主线 f-20260303-main——本地 master 落后主线上百个
    提交，走默认探测会拿到过时基线）→ 本地 master → main → origin/HEAD 短名 →
    当前 HEAD。base_branch 支持字符串（适用于该项目全部仓库）或 {仓库绝对路径: 分支}
    dict；配置的分支在仓库内不存在时回退默认探测并告警（fail-open）。
    返回 (起点引用, 起点 commit sha)；起点为 None 时由 git 默认取当前 HEAD（兜底，不传起点）。
    基线一致性的核心：dev worktree 分支从主干拉取，调研/开发基线不随主工作区 checkout 漂移。"""
    configured = None
    if isinstance(cfg, dict):
        configured = cfg.get("base_branch")
        if isinstance(configured, dict):
            configured = configured.get(str(repo))
    if configured:
        r = subprocess.run(["git", "rev-parse", "--verify", "-q", configured], cwd=repo,
                           capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
        if r.returncode == 0:
            return configured, r.stdout.strip()
        log.warning("base_branch 配置 %r 在 %s 不存在，回退默认分支探测", configured, repo)
    for cand in ("master", "main"):
        r = subprocess.run(["git", "rev-parse", "--verify", "-q", cand], cwd=repo,
                           capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
        if r.returncode == 0:
            return cand, r.stdout.strip()
    r = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                       cwd=repo, capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
    ref = r.stdout.strip() if r.returncode == 0 else ""
    if ref:
        r2 = subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=repo,
                            capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
        if r2.returncode == 0:
            return ref, r2.stdout.strip()
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                       timeout=D.DIAG_TIMEOUT)
    return None, (r.stdout.strip() if r.returncode == 0 else None)

def dev_workdir_for(req):
    """dev 角色工作目录：单仓库 = 该仓库 worktree；多仓库 = 各仓库 worktree 的聚合目录"""
    cfg = D.PROJECTS[req["project"]]
    if len(cfg["paths"]) == 1:
        return D.dev_worktree_path(req, cfg["paths"][0])
    return D.WORKTREES_ROOT / req["project"] / "dev" / f"req-{req['id']}"

def dev_write_roots(req):
    """#303 防复发：dispatcher 各角色的内置 Edit/Write 与 fs 写绝对路径白名单 =
    ① worktree 根（聚合目录或单仓库 worktree）② 各仓库 worktree 路径 ③ 产物目录
    ④ 临时目录（/tmp 与系统 TMPDIR）。主工作区（各仓库真实路径）不在其中 → 禁止写。"""
    roots = {Path(D.dev_workdir_for(req)).resolve(), Path(D.artifact_dir(req)).resolve(),
             Path("/tmp").resolve()}
    try:
        roots.add(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass
    with D.state_lock:
        rec = D.state.get("worktrees", {}).get(str(req["id"])) or {}
    for _, wt in rec.get("worktrees") or []:
        roots.add(Path(wt).resolve())
    return sorted(roots, key=str)

def _link_repo_local_config(wt: Path) -> None:
    """worktree 内补 projects.json（本地数据、gitignore）：软链到主工作区那份，
    使 worktree 与 main 的项目清单语义一致。

    #419 合并带出的现场：不补的后果是 50 个测试文件自带的 bootstrap 会写入一份**只有 1 个项目**
    的最小清单——于是 PROJECTS 在 worktree=1、main=7，凡遍历 PROJECTS 的次数/列表断言两边结论
    不同且不报错（worktree 全绿、合并到 main 转红）。软链而非复制：复制会随主工作区改配置变旧，
    又制造新的隐性差异。

    幂等（已是正确软链则跳过；旧的最小清单/失效软链重建）；任何失败只告警不抛——补本地配置
    不得阻断 worktree 创建（fail-open）。判断存在性用 `is_symlink() or exists()`：断链软链的
    exists() 为 False，否则无法自愈。"""
    src = D.PROJECTS_FILE
    dst = wt / "projects.json"
    if not src.exists():
        log.warning(f"主工作区 projects.json 不存在，跳过 worktree 本地配置补链: {wt}")
        return
    try:
        if dst.is_symlink():
            if Path(os.readlink(dst)) == src:
                return
            dst.unlink()
        elif dst.exists():
            log.warning(f"worktree 内 projects.json 为普通文件（历史最小清单），替换为主工作区软链: {dst}")
            dst.unlink()
        dst.symlink_to(src)
    except OSError as e:
        log.warning(f"补链 worktree projects.json 失败（跳过，不影响创建）: {dst}: {e}")


