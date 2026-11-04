"""只读角色受限工具：路径/SQL 校验、敏感文件黑名单、只读语义纯函数，以及本地 HTTP MCP server
（ReadonlyMcpServer，会话启动时注入给只读角色）。

由 acp_shared.py 拆出；acp_shared 以显式名单回挂全部名字（含下划线名）。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from acp_modules.limits import *  # noqa: F401,F403
from acp_modules.limits import _READ_VERB_RE  # noqa: F401
from acp_modules.command_policy import *  # noqa: F401,F403


# ---- 只读角色受限工具：路径/SQL 校验纯函数 + 本地 HTTP MCP server ----

# 只读角色提示词引导（三个只读模板共用）：MCP 受限工具说明
READONLY_TOOL_GUIDE = (
    "只读数据工具（工具调用，不经 Bash）：`list_dir`（目录浏览，path 可选默认工作目录）、"
    "`read_file`（读文件或元信息，path 必填，offset/max_lines 分页，meta=true 只看元信息）、"
    "`db_query`（只读查库，db 可选默认看板库 data/board.db，sql 限 SELECT/PRAGMA/EXPLAIN/WITH/VALUES "
    "单语句，写操作/多语句/越权路径会被拒绝）、"
    "`git_log`（查仓库提交历史，repo 可选默认工作目录（须为 git 仓库根），path 可选按文件过滤，"
    "count 可选默认 15 上限 100）、"
    "`search_text`（按关键词搜仓库文件内容，keyword 必填，path 可选默认工作目录，"
    "max_matches 可选默认 50；跳过 .git/二进制/敏感文件/超大文件）、"
    "`git_status`（工作区状态，repo 可选默认工作目录，只读固定参数）、"
    "`git_diff`（工作区/暂存区差异，repo 可选默认工作目录，args 仅限只读参数白名单"
    "（--stat/--name-only/--name-status/--numstat/--shortstat/--summary/--cached/--no-color），"
    "path 可选限 roots 内单文件；禁止提交/写类操作）。"
    "TAPD MCP（mcp__tapd__get_*）：可只读查询 TAPD 需求/缺陷/评论/迭代/用户等"
    "（仅 get_* 只读白名单，写工具会被拒绝）。"
    "产物目录内的文件写入（含 result.json）一律用 Write/fs 工具，不要用 Bash/python 脚本写文件"
    "（Bash 只读白名单不含写操作；白名单命令支持 `;`/`|` 拼接，但每段仍须为白名单内单条只读命令"
    "且路径限会话 roots（工作目录/项目仓库/产物目录），任何一段不合法整体拒绝；"
    "`>/dev/null`/`1>/dev/null`/`2>/dev/null`/`2>&1` 丢弃型重定向已放行（其余重定向仍拒）；"
    "命令开头的 `cd <roots 内路径> &&` 前缀会被剥离后再校验（kimi 的 Bash 均带此前缀），"
    "cd 目标越界或形态畸形整体拒；评审角色（review/tech_review/code_review）另可跑受限 python 自验："
    "`<roots 内 .venv>/bin/python -m unittest discover [白名单 flag]` 与 "
    "`python3 -m py_compile <worktree 内路径>`，其余 python 形态（-c/任意脚本/pytest 等）一律拒；"
    "目录列举/文件查找优先用上述内置工具）。"
)

# ---- 敏感文件黑名单（#110：只读范围放开至家目录后的补偿防线） ----
# read_file 命中即拒绝（list_dir 仍可列出，读不读由用户决定）；board 建卡会话与
# dispatcher 只读角色经共享 ReadonlyMcpServer 统一生效。规则：
# .env 系（.env/.env.*）、SSH 私钥（*_rsa/*_ed25519/*_ecdsa）、证书密钥（*.pem/*.key）、
# 凭据类命名（文件名含 password/secret/token/credential，不区分大小写）。
SENSITIVE_ENV_RE = re.compile(r"^\.env(\.|$)", re.I)
SENSITIVE_SSH_KEY_RE = re.compile(r"(^|_)(rsa|ed25519|ecdsa)$", re.I)
SENSITIVE_SUFFIXES = (".pem", ".key")
SENSITIVE_KEYWORDS = ("password", "secret", "token", "credential")


def sensitive_path_blocked(path):
    """敏感文件黑名单匹配：按 resolve 后文件 basename 判定（不区分大小写）。
    命中 = read_file 应拒绝；list_dir 不受影响。"""
    name = Path(path).name
    if SENSITIVE_ENV_RE.match(name):
        return True
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    if SENSITIVE_SSH_KEY_RE.search(name):
        return True
    low = name.lower()
    return any(k in low for k in SENSITIVE_KEYWORDS)


def resolve_path_safe(path_str, base, roots):
    """路径解析与校验：相对路径以 base（= 会话工作目录）为基准；resolve 后必须落在 roots 内。
    返回 resolve 后的 Path；非法（非字符串/空/越权/逃逸，含 ../ 与 symlink 指向 roots 外）返回 None。fail-closed。"""
    if not isinstance(path_str, str) or not path_str.strip():
        return None
    p = Path(path_str)
    if not p.is_absolute():
        p = base / p
    try:
        p = p.resolve()
    except OSError:
        return None
    rlist = [Path(r).resolve() for r in roots]
    return p if any(p == r or r in p.parents for r in rlist) else None


# ---- #26 只读语义纯函数（ReadonlyMcpServer 与飞书看板助手共用：跳过规则/输出形态单一源） ----
def ro_read_lines(p, offset, max_lines):
    """按 offset/max_lines 读文件行（不含表头与行号）；**到 EOF 即停**，不垫空行
    （调用方按 offset+i 编行号，垫空行会编出文件里不存在的行号）。参数合法性由调用方负责校验。"""
    out = []
    with open(p, errors="replace") as f:
        for _ in range(offset):
            if not f.readline():
                break
        for _ in range(max_lines):
            line = f.readline()
            if line == "":
                break
            out.append(line.rstrip("\n"))
    return out


def ro_read_continuation(offset, n_read, has_more):
    """C6：read_file/file_read 未读完时的续读提示（读到 EOF 返回空串）。"""
    return f"……（未读完：继续读用 offset={offset + n_read}）" if has_more else ""


def ro_fit_lines(lines, budget):
    """C6：按整行累计到 budget 字符为止（至少 1 行），返回实际保留行数——read_file 据此给真实
    表头行数与续读 offset，避免总长限幅在行中截断后表头/offset 与实际内容错位。"""
    used = 0
    for i, ln in enumerate(lines):
        used += len(ln) + 1
        if used > budget and i > 0:
            return i
    return len(lines)


def cap_tool_output(text, limit=None):
    """C6：工具单次输出总长限幅——超过 limit 截断并给总长与缩小范围的方式；非 str 原样返回。"""
    limit = TOOL_OUTPUT_MAX_CHARS if limit is None else limit
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return (text[:limit] + f"\n……（输出 {len(text)} 字符超过单次上限 {limit} 已截断："
            "请缩小路径/关键词范围，或用 offset/max_lines 分段读取）")


def ro_search_hits(p, keyword, max_matches, budget=SEARCH_MAX_SCAN, deadline=None):
    """在目录 p 下按 keyword 搜文件内容（大小写不敏感；跳过 .git / 符号链接 / 敏感文件黑名单 /
    二进制（NUL 探测）/ 超 SEARCH_MAX_BYTES 大文件）。
    返回 (lines, scanned, hits, truncated)：lines = f"{相对 p 的路径}:{行号}: {行内容截断 SEARCH_LINE_MAX}"；
    scanned = 实际读取的文件数（**计入前先判预算，故 ≤ budget**）；hits = 命中行数；
    truncated = 是否因命中数上限 / 扫描预算 / 时限提前停止。表头与截断提示由调用方组装。"""
    kwl = str(keyword).lower()
    lines, scanned, hits, truncated = [], 0, 0, False
    for root, dirs, files in os.walk(p):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for fname in sorted(files):
            if scanned >= budget:
                truncated = True
                break
            if deadline is not None and time.monotonic() > deadline:
                truncated = True
                break
            fp = Path(root) / fname
            if fp.is_symlink() or sensitive_path_blocked(fp):
                continue
            try:
                if fp.stat().st_size > SEARCH_MAX_BYTES:
                    continue
                raw = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue
            scanned += 1
            for i, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if kwl in line.lower():
                    lines.append(f"{fp.relative_to(p)}:{i}: {line[:SEARCH_LINE_MAX]}")
                    hits += 1
                    if hits >= max_matches:
                        return lines, scanned, hits, False
        if hits >= max_matches or truncated:
            break
    return lines, scanned, hits, truncated


def ro_glob_paths(base, pattern, limit, budget, deadline=None):
    """在 base 下按 glob 模式展开（支持 **）。跳过 .git / 符号链接 / 敏感文件 / 非普通文件。
    返回 (paths, scanned, truncated)：paths = 相对 base 的 posix 串列表（最多 limit 条）；
    scanned = 计入的候选文件数（**计入前先判预算，故 ≤ budget**）；truncated = 是否因条数/预算/时限提前停止。"""
    base = Path(base)
    paths, scanned, truncated = [], 0, False
    for fp in base.glob(pattern):
        if len(paths) >= limit or scanned >= budget:
            truncated = True
            break
        if deadline is not None and time.monotonic() > deadline:
            truncated = True
            break
        if ".git" in fp.parts:
            continue
        if fp.is_symlink() or sensitive_path_blocked(fp) or not fp.is_file():
            continue
        scanned += 1
        paths.append(fp.relative_to(base).as_posix())
    return paths, scanned, truncated


def git_log_repo_ok(repo_str, workdir, roots):
    """git_log repo 参数校验：空 → workdir（须为 git 仓库根）；非空 → resolve_path_safe 落 roots 内 + .git 存在。
    .git 用 .exists()（worktree/submodule 的 .git 是文件而非目录）。返回 resolve 后的仓库根 Path，非法返回 None。"""
    p = resolve_path_safe(repo_str, workdir, roots) if repo_str else Path(workdir).resolve()
    if p is None:
        return None
    if not (p / ".git").exists():
        return None
    return p


def git_log_path_filter(path_str, repo_root):
    """git_log path 过滤器校验：相对 repo_root 解析，resolve 后必须落 repo 根内；'-' 前缀拒绝（防 flag 注入）。
    返回相对 repo 根的 posix 路径字符串（供 '--' 分隔传 git），非法返回 None。复用 resolve_path_safe。"""
    if path_str.startswith("-"):
        return None
    p = resolve_path_safe(path_str, repo_root, [repo_root])
    if p is None:
        return None
    try:
        rel = p.relative_to(repo_root)
    except ValueError:
        return None
    return "." if str(rel) == "." else rel.as_posix()


def git_log_count_ok(count):
    """git_log count 校验：int（非 bool）且 1..GIT_LOG_MAX_COUNT。"""
    return (isinstance(count, int) and not isinstance(count, bool)
            and 1 <= count <= GIT_LOG_MAX_COUNT)


def git_log_env(env=None):
    """git_log 子进程环境消毒：剥离全部 GIT_* 重定向类变量（GIT_DIR/GIT_CONFIG_* 等），置 GIT_TERMINAL_PROMPT=0。
    仅影响 git_log 子进程 env，不影响调度器主进程环境。"""
    env = dict(env if env is not None else os.environ)
    for k in [k for k in env if k.startswith("GIT_")]:
        del env[k]
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def dbq_sql_safe(sql):
    """只读 SQL 前置校验（与 scripts/dbq.py READ_VERB 同款动词白名单）：
    非空字符串；去尾部一个分号后不含其他分号（禁多语句拼接）；以只读动词开头；
    保守拒注释注入（`--` 后接分号）。形状异常返回 False。执行层由 dbq.py 的
    SQLite mode=ro + PRAGMA query_only 兜底，任何写操作仍会被数据库拒绝。"""
    if not isinstance(sql, str) or not sql.strip():
        return False
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1]
    if ";" in s:
        return False
    if not _READ_VERB_RE.match(sql):
        return False
    if re.search(r"--[^\n]*;", sql):
        return False
    return True


def _read_env_file(path):
    """解析 .env（KEY=VALUE，忽略空行/注释/export 前缀）。
    仅供服务端读取连接参数，返回值绝不进入 agent 上下文。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    out[key] = val
    except OSError:
        pass
    return out


def _mysql_connector():
    """懒加载 MySQL 驱动；未安装返回 None（工具报错，不阻断会话其它工具）。"""
    try:
        import mysql.connector
        return mysql.connector
    except Exception:
        return None


class ReadonlyMcpServer:
    """只读角色受限工具（list_dir/read_file/db_query/mysql_query/git_log/search_text/git_status/git_diff）的
    本地 HTTP MCP server（Streamable Transport）。
    会话粒度：ACPSession 只读角色 start 时创建（127.0.0.1 随机端口），close 时销毁。
    安全：工具内部校验一律 fail-closed（resolve 后路径判定 + SQL 只读前置 + dbq.py mode=ro 兜底 +
    固定 argv 执行 git 子命令 + 输出限量），不依赖任何审批；越权/非法一律 error 返回，绝不返回部分数据。"""

    def __init__(self, workdir, roots, db_default=BOARD_DB, mysql_config=None):
        self.workdir = Path(workdir).resolve()
        self.roots = [Path(r).resolve() for r in roots]
        self.db_default = Path(db_default)
        # 可选的每项目 MySQL 只读查询配置：仅显式配置时暴露 mysql_query 工具。
        # 服务端读取项目 .env 连接（凭据不进入 agent 上下文），fail-closed 默认关闭。
        self.mysql_env_file = None
        if isinstance(mysql_config, str) and mysql_config.strip():
            self.mysql_env_file = str(Path(mysql_config.strip()).expanduser().resolve())
        elif isinstance(mysql_config, dict):
            env_file = str(mysql_config.get("env_file") or "").strip()
            if env_file:
                self.mysql_env_file = str(Path(env_file).expanduser().resolve())
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _readonly_mcp_handler(self))
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass

    # ---- 工具实现（返回文本；校验失败 raise ValueError -> error 返回，fail-closed） ----
    def tool_list_dir(self, args):
        path_str = args.get("path") or ""
        p = resolve_path_safe(path_str, self.workdir, self.roots) if path_str else self.workdir
        if p is None:
            raise ValueError(f"越权/非法路径: {path_str!r}")
        if not p.is_dir():
            raise ValueError(f"不是目录: {p}")
        entries, n = [], 0
        for e in sorted(os.scandir(p), key=lambda x: x.name):
            n += 1
            if n > DIR_MAX_ENTRIES:
                entries.append(f"...（超过 {DIR_MAX_ENTRIES} 条，截断）")
                break
            st = e.stat(follow_symlinks=True)
            kind = "dir" if e.is_dir() else "file"
            entries.append(f"{e.name}\t{kind}\t{st.st_size}\t"
                           f"{datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}")
        return f"目录: {p}\n" + "\n".join(entries)

    def tool_read_file(self, args):
        path_str = args.get("path")
        p = resolve_path_safe(path_str, self.workdir, self.roots) if path_str else None
        if p is None:
            raise ValueError(f"越权/非法路径: {path_str!r}")
        if not p.is_file():
            raise ValueError(f"不是文件: {p}")
        if sensitive_path_blocked(p):
            raise ValueError(f"敏感文件禁止读取（黑名单）: {p.name}")
        st = p.stat()
        meta = args.get("meta", False)
        if not isinstance(meta, bool):
            raise ValueError(f"meta 必须为布尔: {meta!r}")
        if meta:
            raw = p.read_bytes()[:4096]
            ftype = "text" if b"\x00" not in raw else "binary"
            try:
                lines = len(p.read_text(errors="replace").splitlines())
            except OSError:
                lines = -1
            return (f"{p}\ttype={ftype}\tsize={st.st_size}\t"
                    f"mtime={datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')}\tlines={lines}")
        if st.st_size > FILE_MAX_BYTES:
            raise ValueError(f"文件超过 {FILE_MAX_BYTES} 字节上限: {st.st_size}")
        offset = args.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError(f"offset 必须为非负整数: {offset!r}")
        max_lines = args.get("max_lines", FILE_MAX_LINES)
        if not isinstance(max_lines, int) or isinstance(max_lines, bool) \
                or max_lines < 1 or max_lines > FILE_MAX_LINES_LIMIT:
            raise ValueError(f"max_lines 必须为 1..{FILE_MAX_LINES_LIMIT} 的整数: {max_lines!r}")
        out = ro_read_lines(p, offset, max_lines + 1)       # C6：多读一行判断是否未读完
        has_more = len(out) > max_lines
        out = out[:max_lines]
        keep = ro_fit_lines(out, TOOL_OUTPUT_MAX_CHARS - 600)   # 留表头/续读提示余量
        if keep < len(out):
            out, has_more = out[:keep], True
        tail = ro_read_continuation(offset, len(out), has_more)
        return (f"文件: {p} (offset={offset}, lines={len(out)})\n" + "\n".join(out)
                + (f"\n{tail}" if tail else ""))

    def tool_db_query(self, args):
        db_str = args.get("db") or ""
        if db_str:
            p = resolve_path_safe(db_str, self.workdir, self.roots)
            if p is None or p.suffix != ".db":
                raise ValueError(f"db 路径越权或非 .db 文件: {db_str!r}")
            db = p
        else:
            db = self.db_default
        sql = args.get("sql")
        if not dbq_sql_safe(sql):
            raise ValueError(f"SQL 非只读/非法: {sql!r}")
        r = subprocess.run([sys.executable, str(DBQ_SCRIPT), str(db), sql],
                           capture_output=True, text=True, timeout=DBQ_TIMEOUT)
        if r.returncode != 0:
            raise ValueError(f"dbq 执行失败: {(r.stderr or '').strip()[:300]}")
        return r.stdout

    def tool_mysql_query(self, args):
        """只读查询该项目已配置的 MySQL 生产库。
        连接参数由服务端从项目 .env 读取（DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD），
        agent 看不到凭据；SQL 复用 db_query 只读白名单，连接层 SET SESSION TRANSACTION
        READ ONLY + 永不 commit，行数/单元格/超时全部限量。"""
        if not self.mysql_env_file:
            raise ValueError("该项目未配置 mysql 只读查询")
        sql = args.get("sql")
        if not dbq_sql_safe(sql):
            raise ValueError(f"SQL 非只读/非法: {sql!r}")
        sql = sql.strip().rstrip(";")
        env = _read_env_file(self.mysql_env_file)
        host = env.get("DB_HOST") or "localhost"
        try:
            port = int(env.get("DB_PORT") or 3306)
        except (TypeError, ValueError):
            port = 3306
        db = env.get("DB_NAME") or ""
        user = env.get("DB_USER") or ""
        password = env.get("DB_PASSWORD") or ""
        if not db or not user:
            raise ValueError(f"MySQL 配置不完整（缺少 DB_NAME/DB_USER）: {Path(self.mysql_env_file).name}")
        conn_mod = _mysql_connector()
        if conn_mod is None:
            raise ValueError("MySQL 驱动不可用（mysql-connector-python 未安装）")
        conn = None
        try:
            conn = conn_mod.connect(
                host=host, port=port, database=db, user=user, password=password,
                connection_timeout=MYSQL_CONNECT_TIMEOUT, autocommit=False)
            cur = conn.cursor()
            try:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            except Exception:
                pass
            try:
                cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {MYSQL_QUERY_TIMEOUT_MS}")
            except Exception:
                pass
            cur.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            if not cols:
                return "(无结果集)"
            out = [" | ".join(cols)]
            n = 0
            for row in cur:
                n += 1
                if n > MYSQL_MAX_ROWS:
                    out.append(f"...（结果超过 {MYSQL_MAX_ROWS} 行，已截断，请加 LIMIT 缩小范围）")
                    break
                out.append(" | ".join(
                    "NULL" if c is None else str(c)[:MYSQL_MAX_CELL] for c in row))
            out.append(f"({n} 行)")
            return "\n".join(out)
        except Exception as e:
            msg = str(e)
            if password:
                msg = msg.replace(password, "***")
            raise ValueError(f"MySQL 查询失败: {msg[:300]}")
        finally:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass

    def tool_git_log(self, args):
        """查仓库提交历史（只读）。repo 可选默认工作目录（须为 git 仓库根）；path 可选按文件过滤
        （相对 repo 根解析）；count 可选默认 15 上限 100。
        固定 argv 执行 git log：-c alias.log=log / --no-pager 中和仓库配置驱动的执行面
        （恶意 alias/pager/diff 驱动），env 消毒剥离 GIT_* 重定向变量，超时/非零退出一律 error（fail-closed）。"""
        unknown = set(args) - {"repo", "path", "count"}
        if unknown:
            raise ValueError(f"未知参数: {sorted(unknown)}")
        repo = git_log_repo_ok(args.get("repo") or "", self.workdir, self.roots)
        if repo is None:
            raise ValueError(f"repo 越权或非 git 仓库根: {args.get('repo')!r}")
        count = args.get("count", GIT_LOG_DEFAULT_COUNT)
        if not git_log_count_ok(count):
            raise ValueError(f"count 必须为 1..{GIT_LOG_MAX_COUNT} 的整数: {count!r}")
        argv = ["git", "-c", "alias.log=log", "-c", "color.ui=never",
                "-c", "i18n.logOutputEncoding=utf-8", "--no-pager",
                "log", "-n", str(count), "--date=short", f"--format={GIT_LOG_FORMAT}"]
        path_str = args.get("path") or ""
        if path_str:
            rel = git_log_path_filter(path_str, repo)
            if rel is None:
                raise ValueError(f"path 越权/非法: {path_str!r}")
            argv += ["--", rel]
        try:
            r = subprocess.run(argv, cwd=repo, capture_output=True, text=True,
                               encoding="utf-8", timeout=GIT_LOG_TIMEOUT, env=git_log_env())
        except subprocess.TimeoutExpired:
            raise ValueError(f"git_log 超时（>{GIT_LOG_TIMEOUT}s）")
        if r.returncode != 0:
            raise ValueError(f"git log 执行失败: {(r.stderr or '').strip()[:300]}")
        lines = [ln[:GIT_LOG_LINE_MAX] for ln in r.stdout.splitlines()]
        return "\n".join(lines) if lines else GIT_LOG_EMPTY_HINT

    def tool_search_text(self, args):
        """按关键词搜仓库文件内容（只读）。keyword 必填非空；path 可选默认工作目录（须在 roots 内）；
        max_matches 可选默认 50 上限 50。跳过 .git 目录/符号链接/敏感文件黑名单/二进制（NUL 探测）/
        超 SEARCH_MAX_BYTES 大文件；扫描文件数上限 SEARCH_MAX_SCAN；命中行截断 SEARCH_LINE_MAX。"""
        unknown = set(args) - {"keyword", "path", "max_matches"}
        if unknown:
            raise ValueError(f"未知参数: {sorted(unknown)}")
        keyword = args.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError(f"keyword 必须为非空字符串: {keyword!r}")
        kw = keyword.strip()
        path_str = args.get("path") or ""
        p = resolve_path_safe(path_str, self.workdir, self.roots) if path_str else self.workdir
        if p is None:
            raise ValueError(f"越权/非法路径: {path_str!r}")
        if not p.is_dir():
            raise ValueError(f"不是目录: {p}")
        max_matches = args.get("max_matches", SEARCH_MAX_MATCHES)
        if not isinstance(max_matches, int) or isinstance(max_matches, bool) \
                or max_matches < 1 or max_matches > SEARCH_MAX_MATCHES:
            raise ValueError(f"max_matches 必须为 1..{SEARCH_MAX_MATCHES} 的整数: {max_matches!r}")
        out, scanned, hits, truncated = ro_search_hits(p, kw, max_matches, SEARCH_MAX_SCAN)
        by_max = hits >= max_matches and not truncated
        if truncated:
            out = out + [f"...（扫描文件超 {SEARCH_MAX_SCAN} 个，请缩小 path 范围）"]
            hits += 1
        if not out:
            return f"关键词: {kw}（无匹配）"
        if by_max:
            return f"关键词: {kw}（命中 {hits} 处）\n" + "\n".join(out)
        return f"关键词: {kw}（命中 {hits} 处，扫描 {scanned} 个文件）\n" + "\n".join(out)

    def tool_git_status(self, args):
        """工作区状态（只读）。repo 可选默认工作目录（须为 git 仓库根）。
        固定 argv：-c alias.status=status 中和别名 + -c core.fsmonitor=false 防 fsmonitor hook 执行
        + --no-pager + status --short --branch（紧凑每文件一行）；env 消毒复用 git_log_env；
        超时/非零退出一律 error（fail-closed）。"""
        unknown = set(args) - {"repo"}
        if unknown:
            raise ValueError(f"未知参数: {sorted(unknown)}")
        repo = git_log_repo_ok(args.get("repo") or "", self.workdir, self.roots)
        if repo is None:
            raise ValueError(f"repo 越权或非 git 仓库根: {args.get('repo')!r}")
        argv = ["git", "-c", "alias.status=status", "-c", "core.fsmonitor=false",
                "-c", "color.ui=never", "--no-pager", "status", "--short", "--branch"]
        try:
            r = subprocess.run(argv, cwd=repo, capture_output=True, text=True,
                               encoding="utf-8", timeout=GIT_TOOL_TIMEOUT, env=git_log_env())
        except subprocess.TimeoutExpired:
            raise ValueError(f"git_status 超时（>{GIT_TOOL_TIMEOUT}s）")
        if r.returncode != 0:
            raise ValueError(f"git status 执行失败: {(r.stderr or '').strip()[:300]}")
        all_lines = r.stdout.splitlines()
        lines = [ln[:GIT_LOG_LINE_MAX] for ln in all_lines][:GIT_STATUS_MAX_LINES]
        if len(all_lines) > GIT_STATUS_MAX_LINES:          # C6：截断显式标注（原为静默截断）
            lines.append(f"……（共 {len(all_lines)} 行，超过 {GIT_STATUS_MAX_LINES} 行已截断）")
        return f"工作区状态: {repo}\n" + ("\n".join(lines) if lines else "（无输出）")

    def tool_git_diff(self, args):
        """工作区/暂存区差异（只读）。repo 可选默认工作目录（须为 git 仓库根）；args 限只读参数
        白名单（GIT_DIFF_ALLOWED_ARGS 枚举，禁 -o/--output 写文件面与 --ext-diff/commit 范围等执行面）；
        path 可选限 roots 内单文件（相对 repo 根）。
        固定 argv：-c alias.diff=diff + --no-ext-diff + --no-textconv + -c core.fsmonitor=false
        （封死外部 diff 驱动/textconv/fsmonitor hook 执行面）+ env 消毒；超时/非零退出一律 error。"""
        unknown = set(args) - {"repo", "args", "path"}
        if unknown:
            raise ValueError(f"未知参数: {sorted(unknown)}")
        repo = git_log_repo_ok(args.get("repo") or "", self.workdir, self.roots)
        if repo is None:
            raise ValueError(f"repo 越权或非 git 仓库根: {args.get('repo')!r}")
        argv = ["git", "-c", "alias.diff=diff", "-c", "core.fsmonitor=false",
                "--no-pager", "diff", "--no-ext-diff", "--no-textconv"]
        raw_args = args.get("args") or []
        if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
            raise ValueError(f"args 必须为字符串数组: {raw_args!r}")
        for a in raw_args:
            if a not in GIT_DIFF_ALLOWED_ARGS:
                raise ValueError(f"args 含白名单外参数: {a!r}")
            argv.append(a)
        path_str = args.get("path") or ""
        if path_str:
            rel = git_log_path_filter(path_str, repo)
            if rel is None:
                raise ValueError(f"path 越权/非法: {path_str!r}")
            argv += ["--", rel]
        try:
            r = subprocess.run(argv, cwd=repo, capture_output=True, text=True,
                               encoding="utf-8", timeout=GIT_TOOL_TIMEOUT, env=git_log_env())
        except subprocess.TimeoutExpired:
            raise ValueError(f"git_diff 超时（>{GIT_TOOL_TIMEOUT}s）")
        if r.returncode != 0:
            raise ValueError(f"git diff 执行失败: {(r.stderr or '').strip()[:300]}")
        lines = [ln[:GIT_LOG_LINE_MAX] for ln in r.stdout.splitlines()]
        if len(lines) > GIT_DIFF_MAX_LINES:
            lines = lines[:GIT_DIFF_MAX_LINES] + [f"...（输出超 {GIT_DIFF_MAX_LINES} 行，截断）"]
        return f"差异: {repo}\n" + ("\n".join(lines) if lines else "（无差异）")


def _readonly_mcp_handler(server):
    """绑定 ReadonlyMcpServer 实例的 HTTP handler（Streamable Transport：SSE 或纯 JSON 响应）。"""
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            accept = self.headers.get("Accept", "")
            if "text/event-stream" in accept:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(b"event: message\ndata: " + body + b"\n\n")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("MCP-Protocol-Version", "2025-11-25")
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n)
            rid = None
            try:
                try:
                    rid = json.loads(body).get("id")
                except Exception:
                    pass
                msg = json.loads(body)
                method, i = msg.get("method"), msg.get("id")
                if method == "initialize":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {
                        "protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": READONLY_TOOL_SERVER, "version": "1.0"}}}
                elif method == "ping":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {}}
                elif method == "tools/list":
                    obj = {"jsonrpc": "2.0", "id": i, "result": {"tools": [
                        {"name": "list_dir", "description": "列目录条目（只读，path 可选默认工作目录）",
                         "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
                        {"name": "read_file", "description": "读文件内容或元信息（只读；offset/max_lines 分页，meta=true 只看元信息）",
                         "inputSchema": {"type": "object", "properties": {
                             "path": {"type": "string"}, "offset": {"type": "integer"},
                             "max_lines": {"type": "integer"}, "meta": {"type": "boolean"}},
                             "required": ["path"]}},
                        {"name": "db_query", "description": "只读查库（db 可选默认看板库 data/board.db；sql 限 SELECT/PRAGMA/EXPLAIN/WITH/VALUES 单语句）",
                         "inputSchema": {"type": "object", "properties": {
                             "db": {"type": "string"}, "sql": {"type": "string"}},
                             "required": ["sql"]}},
                        {"name": "git_log", "description": "查仓库提交历史（repo 可选默认工作目录须为 git 仓库根，path 可选按文件过滤，count 可选默认 15 上限 100）",
                         "inputSchema": {"type": "object", "properties": {
                             "repo": {"type": "string"}, "path": {"type": "string"},
                             "count": {"type": "integer"}}}},
                        {"name": "search_text", "description": "按关键词搜仓库文件内容（keyword 必填，path 可选默认工作目录，max_matches 可选默认 50；跳过 .git/二进制/敏感文件/超大文件）",
                         "inputSchema": {"type": "object", "properties": {
                             "keyword": {"type": "string"}, "path": {"type": "string"},
                             "max_matches": {"type": "integer"}},
                             "required": ["keyword"]}},
                        {"name": "git_status", "description": "工作区状态（repo 可选默认工作目录须为 git 仓库根，只读固定参数）",
                         "inputSchema": {"type": "object", "properties": {
                             "repo": {"type": "string"}}}},
                        {"name": "git_diff", "description": "工作区/暂存区差异（repo 可选默认工作目录，args 限只读参数白名单 --stat/--name-only/--name-status/--numstat/--shortstat/--summary/--cached/--no-color，path 可选限 roots 内单文件）",
                         "inputSchema": {"type": "object", "properties": {
                             "repo": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}},
                             "path": {"type": "string"}}}},
                    ] + ([{"name": "mysql_query", "description": "只读查询该项目已配置的 MySQL 生产库（sql 限 SELECT/EXPLAIN/WITH/VALUES 单语句，最多 200 行；凭据由服务端读取，不暴露）",
                           "inputSchema": {"type": "object", "properties": {
                               "sql": {"type": "string"}},
                               "required": ["sql"]}}]
                       if getattr(server, "mysql_env_file", None) else [])}}
                elif method == "tools/call":
                    p = msg.get("params") or {}
                    name, args = p.get("name"), p.get("arguments") or {}
                    fn = {"list_dir": server.tool_list_dir, "read_file": server.tool_read_file,
                          "db_query": server.tool_db_query, "git_log": server.tool_git_log,
                          "search_text": server.tool_search_text,
                          "git_status": server.tool_git_status,
                          "git_diff": server.tool_git_diff,
                          "mysql_query": server.tool_mysql_query}.get(name)
                    if fn is None:
                        raise ValueError(f"未知工具: {name!r}")
                    if not isinstance(args, dict):
                        raise ValueError(f"arguments 必须为对象: {args!r}")
                    out = cap_tool_output(fn(args))      # C6 单次输出总长限幅
                    obj = {"jsonrpc": "2.0", "id": i,
                           "result": {"content": [{"type": "text", "text": out}]}}
                elif i is None:
                    # notifications（initialized/cancelled 等）：空 202
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    raise ValueError(f"method not found: {method}")
                self._reply(obj)
            except Exception as e:
                self._reply({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32603, "message": str(e)}})

    return _Handler


def parse_usage(text):
    """从 /usage 返回文本解析 Context: X / Y tokens (Z%) 三值（token 数与百分比），失败返回 None。
    kimi 0.33.0 起格式为 `Context: X / Y tokens (Z%)`（0.32.x 无 tokens 字样），
    用 [^()]* 兼容新旧格式并抗格式漂移；失配/异常由调用方兜底，不阻断会话"""
    m = re.search(r"Context:\s*([\d,]+)\s*/\s*([\d,]+)\s*[^()]*\(([\d.]+%)\)", text)
    if not m:
        return None
    return {"context_tokens": int(m.group(1).replace(",", "")),
            "max_tokens": int(m.group(2).replace(",", "")),
            "context_pct": m.group(3)}


# #42 缓存命中指标：供应商 usage 字段名超集（camelCase/snake_case/常见别名全收，逐个探测取首个合法值）。
# 拿不到 = None（unknown），不猜测不补 0——上下文峰值（context_tokens）永不冒充缓存命中。
_USAGE_NUM_KEYS = {
    "cached": ("cachedTokens", "cached_tokens", "cache_read_tokens", "cacheReadInputTokens",
               "prompt_cache_hit_tokens", "promptCacheHitTokens"),
    "input": ("inputTokens", "input_tokens", "prompt_tokens", "promptTokens"),
}


def normalize_provider_usage(obj):
    """#42：供应商 usage 对象 → {cached_tokens, input_tokens}（字段名超集映射）。
    非 dict / 无字段 / 负数 / 非数字（含 bool）→ 对应值为 None；不抛异常。"""
    out = {"cached_tokens": None, "input_tokens": None}

    def _num(keys):
        if not isinstance(obj, dict):
            return None
        for k in keys:
            v = obj.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if v >= 0:
                return int(v)
        return None

    out["cached_tokens"] = _num(_USAGE_NUM_KEYS["cached"])
    out["input_tokens"] = _num(_USAGE_NUM_KEYS["input"])
    return out


# #42 /usage 文本缓存行候选正则：命中「cache/缓存」字样且带 token 数的行（千分位/小数）。
# 刻意保守：含 % 的行（命中率/百分比）不认；失配一律 None（= unknown，不估算）。
_USAGE_CACHE_LINE_RE = re.compile(
    r"(?:cache|缓存)[^%\n]{0,80}?([\d,]+(?:\.\d+)?)\s*(?:tokens?|tok)?", re.IGNORECASE)


def parse_usage_cache(text):
    """#42：/usage 返回文本中缓存命中行的候选解析，返回 {cached_tokens, input_tokens}。
    文本只能给出缓存命中值（input 无从取得，恒 None）；无缓存行/失配 → 双 None，不猜测。
    含 % 的行（命中率/百分比）整行跳过，防「Cache hit rate: 12%」被读成 12 token。"""
    out = {"cached_tokens": None, "input_tokens": None}
    for line in (text or "").splitlines():
        if "%" in line:
            continue
        m = _USAGE_CACHE_LINE_RE.search(line)
        if not m:
            continue
        try:
            val = int(float(m.group(1).replace(",", "")))
        except (ValueError, TypeError):
            continue
        if val >= 0:
            out["cached_tokens"] = val
            break
    return out

def parse_usage_percent(text):
    """兼容旧接口：只取 context 用量百分比（Z），失败返回 None"""
    u = parse_usage(text)
    return u["context_pct"] if u else None

def pct_value(pct):
    """百分比字符串 '10%' -> 数值 10.0（agent_runs.context_pct 落库用）；空/异常返回 None"""
    try:
        return float(pct.rstrip("%"))
    except Exception:
        return None
