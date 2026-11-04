#!/usr/bin/env python3
"""req-78/85 只读角色受限工具（HTTP MCP server）测试：L0 校验函数 / L1 工具实现 / L2 审批路由不变性。

req-85：新增 git_log 工具（按文件查提交历史，固定 argv 执行，count 上限，fail-closed）。
运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试 setUpModule 自动补一份。
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

# 本机 macOS 系统代理会拦截 urllib 对 127.0.0.1 的请求（getproxies_macosx_sysconf），
# 本地 HTTP 冒烟一律走显式空代理 opener（#419 自验环境暴露，与业务逻辑无关）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tests._frontend_src import backend_text  # noqa: E402

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402
import acp_shared as ac  # noqa: E402（黑名单新函数未从 dispatcher 导出，直接引用共享模块）


def _req_permission_params(text, title="Bash", tool_call_id=None):
    tc = {"title": title,
          "content": [{"type": "content",
                       "content": {"type": "text", "text": text}}]}
    if tool_call_id:
        tc["toolCallId"] = tool_call_id
    return {"options": [{"optionId": "approve_once", "kind": "allow_once"},
                        {"optionId": "reject", "kind": "reject_once"}],
            "toolCall": tc}


def _stub_session(role="analysis", workdir=None, artifact_root=None, allow_git_commit=False,
                  allow_git_merge=False):
    s = d.ACPSession.__new__(d.ACPSession)
    s.req_id = 99999
    s.role = role
    s.workdir = Path(workdir) if workdir else Path(tempfile.gettempdir())
    s.artifact_root = Path(artifact_root) if artifact_root else Path(tempfile.gettempdir()) / "art"
    s.project = "req-board"
    s.allow_git_commit = allow_git_commit
    s.allow_git_merge = allow_git_merge
    s._tool_cmds = {}  # #357 对齐真实会话：tool_call_update 完整命令缓存
    s.emitted = []  # #379 审批留痕断言用：system 事件 spy（参照 _l3_stub）
    s._emit = lambda kind, content, session_id=None: s.emitted.append((kind, content))
    return s


class L0Validation(unittest.TestCase):
    """resolve_path_safe / dbq_sql_safe 纯函数正反例"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req78-l0-")).resolve()
        (self.tmp / "a.txt").write_text("hello")
        (self.tmp / "sub").mkdir()
        self.roots = [self.tmp]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_p_resolve_in_root(self):
        self.assertEqual(d.resolve_path_safe("a.txt", self.tmp, self.roots), self.tmp / "a.txt")
        self.assertEqual(d.resolve_path_safe(str(self.tmp / "a.txt"), self.tmp, self.roots), self.tmp / "a.txt")
        self.assertEqual(d.resolve_path_safe("sub", self.tmp, self.roots), self.tmp / "sub")
        self.assertEqual(d.resolve_path_safe(".", self.tmp, self.roots), self.tmp)

    def test_n_resolve_escape(self):
        self.assertIsNone(d.resolve_path_safe("/etc/passwd", self.tmp, self.roots))
        self.assertIsNone(d.resolve_path_safe("../../etc/hosts", self.tmp, self.roots))
        self.assertIsNone(d.resolve_path_safe("", self.tmp, self.roots))
        self.assertIsNone(d.resolve_path_safe(None, self.tmp, self.roots))
        self.assertIsNone(d.resolve_path_safe(123, self.tmp, self.roots))
        self.assertIsNone(d.resolve_path_safe("   ", self.tmp, self.roots))

    def test_n_resolve_symlink_escape(self):
        _out = Path(tempfile.mkdtemp(prefix="req78-out-"))
        self.addCleanup(shutil.rmtree, _out, ignore_errors=True)  # 防 $TMPDIR 泄漏
        outside = _out / "secret.txt"
        outside.write_text("secret")
        link = self.tmp / "link.txt"
        link.symlink_to(outside)
        # symlink 指向 roots 外：resolve 后判定越权
        self.assertIsNone(d.resolve_path_safe("link.txt", self.tmp, self.roots))

    def test_p_sql_read_only(self):
        for sql in ("SELECT * FROM requirements", "select id from t",
                    "PRAGMA table_info(t)", "EXPLAIN SELECT 1",
                    "WITH x AS (SELECT 1) SELECT * FROM x",
                    "VALUES (1)", "SELECT 1;", "  SELECT 1;  "):
            self.assertTrue(d.dbq_sql_safe(sql), sql)

    def test_n_sql_write_and_injection(self):
        for sql in ("DELETE FROM requirements", "INSERT INTO t VALUES(1)",
                    "UPDATE t SET x=1", "DROP TABLE t", "ALTER TABLE t ADD x",
                    "ATTACH 'x' AS y", "SELECT 1; DROP TABLE x",
                    "SELECT 1; SELECT 2;", "SELECT 1 -- ; DROP TABLE x",
                    "", None, 123, "   "):
            self.assertFalse(d.dbq_sql_safe(sql), repr(sql))


class L0SensitivePath(unittest.TestCase):
    """#110 敏感文件黑名单（sensitive_path_blocked）纯函数正反例"""

    def test_p_blocked(self):
        for name in (".env", ".env.local", ".env.production", ".ENV",
                     "id_rsa", "id_ed25519", "deploy_rsa", "mykey_ecdsa",
                     "cert.pem", "priv.key", "password.txt", "api_secret.json",
                     "TOKEN.conf", "credential.ini"):
            self.assertTrue(ac.sensitive_path_blocked(name), name)

    def test_n_allowed(self):
        for name in ("a.txt", "id_rsa.pub", "README.md", "keys.txt",
                     "monkey.txt", "sub/.gitignore", "config.yaml"):
            self.assertFalse(ac.sensitive_path_blocked(name), name)


class L0ChatRoots(unittest.TestCase):
    """#110：board 建卡会话只读范围放开至家目录（_chat_roots 追加 home root + resolve 越权语义）"""

    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b

    def test_chat_roots_contains_home(self):
        r = self.b._chat_roots("req-board", "/tmp/work")
        self.assertIn("/tmp/work", r)
        self.assertIn(str(Path.home()), r)

    def test_home_root_resolve_semantics(self):
        """模拟「家目录 root」：roots 含 home-like 时其内放行、其外拒绝（fail-closed 机制不变）"""
        home_like = Path(tempfile.mkdtemp(prefix="req110-home-")).resolve()
        outside = Path(tempfile.mkdtemp(prefix="req110-out-"))
        try:
            (home_like / "note.md").write_text("note")
            (outside / "x.txt").write_text("x")
            roots = [home_like]
            self.assertEqual(d.resolve_path_safe("note.md", home_like, roots), home_like / "note.md")
            self.assertIsNone(d.resolve_path_safe(str(outside / "x.txt"), home_like, roots))
            self.assertIsNone(d.resolve_path_safe("/etc/passwd", home_like, roots))
        finally:
            import shutil
            shutil.rmtree(home_like, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)


class L0GitLogValidation(unittest.TestCase):
    """git_log 纯函数（repo/path/count 校验 + env 消毒）正反例"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req85-l0g-")).resolve()
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.work / ".git").mkdir()          # 普通 git 根（.git 为目录）
        (self.work / "a.txt").write_text("x")
        (self.work / "sub").mkdir()
        (self.work / "sub" / "b.txt").write_text("y")
        self.nonrepo = self.tmp / "nonrepo"   # roots 内但非 git 根
        self.nonrepo.mkdir()
        self.roots = [self.work]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_p_repo_ok(self):
        self.assertEqual(d.git_log_repo_ok("", self.work, self.roots), self.work)
        self.assertEqual(d.git_log_repo_ok(".", self.work, self.roots), self.work)
        self.assertEqual(d.git_log_repo_ok(str(self.work), self.work, self.roots), self.work)

    def test_p_repo_ok_gitfile(self):
        """worktree/submodule 形态：.git 为文件（.exists() 语义而非 is_dir）"""
        gt = self.tmp / "wt"
        gt.mkdir()
        (gt / ".git").write_text("gitdir: ../work/.git\n")
        (gt / "f.txt").write_text("z")
        self.assertEqual(d.git_log_repo_ok(str(gt), self.work, [self.work, gt]), gt)

    def test_n_repo_ok(self):
        self.assertIsNone(d.git_log_repo_ok("/etc", self.work, self.roots))
        self.assertIsNone(d.git_log_repo_ok(str(self.tmp / "outside"), self.work, self.roots))
        self.assertIsNone(d.git_log_repo_ok(str(self.nonrepo), self.work, self.roots))
        self.assertIsNone(d.git_log_repo_ok("", self.nonrepo, [self.nonrepo]))  # workdir 非 git 根

    def test_p_path_filter(self):
        self.assertEqual(d.git_log_path_filter("a.txt", self.work), "a.txt")
        self.assertEqual(d.git_log_path_filter("sub/b.txt", self.work), "sub/b.txt")
        self.assertEqual(d.git_log_path_filter(str(self.work / "a.txt"), self.work), "a.txt")
        self.assertEqual(d.git_log_path_filter(".", self.work), ".")

    def test_n_path_filter(self):
        for p in ("../../etc/hosts", "/etc/passwd", "-foo", "", "   "):
            self.assertIsNone(d.git_log_path_filter(p, self.work), repr(p))

    def test_n_path_filter_symlink_escape(self):
        _out = Path(tempfile.mkdtemp(prefix="req85-out-"))
        self.addCleanup(shutil.rmtree, _out, ignore_errors=True)  # 防 $TMPDIR 泄漏
        outside = _out / "s.txt"
        outside.write_text("s")
        link = self.work / "link.txt"
        link.symlink_to(outside)
        self.assertIsNone(d.git_log_path_filter("link.txt", self.work))

    def test_p_count_ok(self):
        for c in (1, 15, 100):
            self.assertTrue(d.git_log_count_ok(c), c)

    def test_n_count_ok(self):
        for c in (0, 101, -1, "15", True, None, 15.5, 1.0):
            self.assertFalse(d.git_log_count_ok(c), repr(c))

    def test_p_env_sanitize(self):
        env = {"GIT_DIR": "/x", "GIT_CONFIG_COUNT": "1", "PATH": "/usr/bin", "HOME": "/home/t"}
        out = d.git_log_env(env)
        self.assertEqual(out, {"PATH": "/usr/bin", "HOME": "/home/t", "GIT_TERMINAL_PROMPT": "0"})
        self.assertEqual(env, {"GIT_DIR": "/x", "GIT_CONFIG_COUNT": "1",
                               "PATH": "/usr/bin", "HOME": "/home/t"})  # 原 env 不被修改

    def test_p_env_sanitize_default(self):
        self.assertEqual(d.git_log_env({}), {"GIT_TERMINAL_PROMPT": "0"})


def _git(repo, *args):
    """在临时仓库执行 git（user.name/email 走 -c，不依赖全局配置）"""
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=str(repo), check=True, capture_output=True, text=True)


class L1ReadonlyMcpServer(unittest.TestCase):
    """ReadonlyMcpServer 工具实现正反例（tempdir 假数据，不拉起 kimi）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req78-l1-")).resolve()
        (self.tmp / "a.txt").write_text("line1\nline2\nline3\n")
        (self.tmp / "big.txt").write_text("x" * (d.FILE_MAX_BYTES + 10))
        (self.tmp / "sub").mkdir()
        self.db = self.tmp / "t.db"
        conn = sqlite3.connect(str(self.db))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO t (name) VALUES (?)", [("a",), ("b",)])
        conn.commit()
        conn.close()
        self.srv = d.ReadonlyMcpServer(self.tmp, [self.tmp], db_default=self.db)
        # git_log 无参冒烟：workdir 须为 git 仓库根且非空（空仓库 git log exit 128）
        _git(self.tmp, "init", "-q")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-q", "-m", "l1 fixture")

    def tearDown(self):
        self.srv.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- list_dir ----
    def test_p_list_dir(self):
        out = self.srv.tool_list_dir({})
        self.assertIn("a.txt", out)
        self.assertIn("sub", out)
        self.assertIn("t.db", out)

    def test_p_list_dir_explicit(self):
        self.assertIn("a.txt", self.srv.tool_list_dir({"path": str(self.tmp)}))

    def test_n_list_dir_escape(self):
        with self.assertRaises(ValueError):
            self.srv.tool_list_dir({"path": "/etc"})
        with self.assertRaises(ValueError):
            self.srv.tool_list_dir({"path": "../.."})

    # ---- read_file ----
    def test_p_read_file(self):
        out = self.srv.tool_read_file({"path": "a.txt"})
        self.assertIn("line1", out)
        self.assertIn("line3", out)

    def test_p_read_file_paging(self):
        out = self.srv.tool_read_file({"path": "a.txt", "offset": 1, "max_lines": 1})
        self.assertIn("line2", out)
        self.assertNotIn("line1", out)

    def test_p_read_file_meta(self):
        out = self.srv.tool_read_file({"path": "a.txt", "meta": True})
        self.assertIn("type=text", out)
        self.assertIn("lines=3", out)
        self.assertIn("size=", out)

    def test_n_read_file_escape(self):
        for p in ("/etc/passwd", "../../etc/hosts", "nonexist.txt"):
            with self.assertRaises(ValueError):
                self.srv.tool_read_file({"path": p})

    def test_n_read_file_params(self):
        with self.assertRaises(ValueError):
            self.srv.tool_read_file({"path": "a.txt", "offset": -1})
        with self.assertRaises(ValueError):
            self.srv.tool_read_file({"path": "a.txt", "max_lines": 5000})
        with self.assertRaises(ValueError):
            self.srv.tool_read_file({"path": "a.txt", "max_lines": 0})
        with self.assertRaises(ValueError):
            self.srv.tool_read_file({"path": "a.txt", "meta": "yes"})
        with self.assertRaises(ValueError):
            self.srv.tool_read_file({"path": "big.txt"})  # 超 1MB 上限

    # ---- #110 敏感文件黑名单（read_file 命中即拒绝，含 meta；list_dir 仍可列出） ----
    def test_n_read_sensitive_files(self):
        for name in (".env", ".env.local", "id_rsa", "id_ed25519", "deploy_rsa",
                     "cert.pem", "priv.key", "password.txt", "api_secret.json",
                     "TOKEN.conf", "credential.ini"):
            p = self.tmp / name
            p.write_text("topsecret\n")
            with self.assertRaises(ValueError, msg=name):
                self.srv.tool_read_file({"path": name})
            with self.assertRaises(ValueError, msg=f"meta {name}"):
                self.srv.tool_read_file({"path": name, "meta": True})

    def test_p_read_public_key_and_plain(self):
        """公钥（id_rsa.pub）与普通命名文件仍可读"""
        (self.tmp / "id_rsa.pub").write_text("ssh-rsa AAAAB3...\n")
        self.assertIn("ssh-rsa", self.srv.tool_read_file({"path": "id_rsa.pub"}))
        self.assertIn("line1", self.srv.tool_read_file({"path": "a.txt"}))

    def test_p_list_dir_shows_sensitive_names(self):
        """list_dir 不受黑名单影响：黑名单文件仍可列出（读不读由用户决定）"""
        (self.tmp / ".env").write_text("KEY=value\n")
        out = self.srv.tool_list_dir({})
        self.assertIn(".env", out)
        self.assertIn("a.txt", out)

    # ---- db_query ----
    def test_p_db_query(self):
        out = self.srv.tool_db_query({"sql": "SELECT id,name FROM t ORDER BY id"})
        self.assertIn("a", out)
        self.assertIn("b", out)

    def test_p_db_query_explicit_db(self):
        out = self.srv.tool_db_query({"db": str(self.db), "sql": "SELECT COUNT(*) AS c FROM t"})
        self.assertIn("2", out)

    def test_n_db_query_write(self):
        for sql in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "INSERT INTO t VALUES(9,'x')",
                    "UPDATE t SET name='x'"):
            with self.assertRaises(ValueError):
                self.srv.tool_db_query({"sql": sql})

    def test_n_db_query_bad_db(self):
        with self.assertRaises(ValueError):
            self.srv.tool_db_query({"db": "/etc/hosts", "sql": "SELECT 1"})
        with self.assertRaises(ValueError):
            self.srv.tool_db_query({"db": "a.txt", "sql": "SELECT 1"})  # 非 .db
        with self.assertRaises(ValueError):
            self.srv.tool_db_query({"db": str(Path(tempfile.gettempdir()) / "out.db"), "sql": "SELECT 1"})

    # ---- HTTP 冒烟（Streamable Transport：initialize/tools/list/tools/call） ----
    def test_http_end_to_end(self):
        def call(payload):
            req = urllib.request.Request(self.srv.url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "application/json, text/event-stream"})
            with _NO_PROXY_OPENER.open(req, timeout=10) as r:
                body = r.read().decode()
            # SSE 格式剥壳：取 "data: {...}" 行的 JSON
            data_line = next(l for l in body.splitlines() if l.startswith("data: "))
            return json.loads(data_line[6:])

        init = call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}}})
        self.assertEqual(init["result"]["serverInfo"]["name"], d.READONLY_TOOL_SERVER)
        tl = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in tl["result"]["tools"]]
        self.assertEqual(names, ["list_dir", "read_file", "db_query", "git_log",
                                 "search_text", "git_status", "git_diff"])
        ok = call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "read_file", "arguments": {"path": "a.txt"}}})
        self.assertIn("line1", ok["result"]["content"][0]["text"])
        err = call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "read_file", "arguments": {"path": "/etc/passwd"}}})
        self.assertIn("error", err)
        unk = call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "exec", "arguments": {"command": "ls"}}})
        self.assertIn("error", unk)
        # git_log 冒烟：workdir 即 git 仓库根时无参可查；越权 repo 一律 error
        gl = call({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                   "params": {"name": "git_log", "arguments": {}}})
        self.assertIn("l1 fixture", gl["result"]["content"][0]["text"])
        gerr = call({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": "git_log", "arguments": {"repo": "/etc"}}})
        self.assertIn("error", gerr)


class L1SearchText(unittest.TestCase):
    """#119 search_text 工具正反例（tempdir 假数据，含 .git/二进制/敏感文件跳过）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req119-l1s-")).resolve()
        (self.tmp / "a.txt").write_text("hello world\nline2 with hello again\n")
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "b.txt").write_text("nothing here\nHELLO UPPER\n")
        (self.tmp / "bin.dat").write_bytes(b"\x00\x01hello\x00")
        (self.tmp / ".env").write_text("SECRET=hello\n")
        (self.tmp / ".git").mkdir()  # 模拟 .git 目录（跳过）
        (self.tmp / ".git" / "x.txt").write_text("hello in git\n")
        self.srv = d.ReadonlyMcpServer(self.tmp, [self.tmp])

    def tearDown(self):
        self.srv.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_p1_find_hits(self):
        out = self.srv.tool_search_text({"keyword": "hello"})
        self.assertIn("a.txt:1: hello world", out)
        self.assertIn("a.txt:2: line2 with hello again", out)
        self.assertIn("sub/b.txt:2: HELLO UPPER", out)  # 大小写不敏感
        self.assertNotIn(".git", out)     # .git 跳过
        self.assertNotIn(".env", out)     # 敏感文件跳过
        self.assertNotIn("bin.dat", out)  # 二进制跳过

    def test_p2_max_matches_limit(self):
        out = self.srv.tool_search_text({"keyword": "hello", "max_matches": 1})
        self.assertIn("命中 1 处", out)

    def test_p3_path_scope(self):
        out = self.srv.tool_search_text({"keyword": "nothing", "path": "sub"})
        self.assertIn("b.txt:1: nothing here", out)  # 相对搜索起点 sub/
        self.assertNotIn("a.txt", out)

    def test_n1_bad_params(self):
        for kw in ("", None, 123, "   "):
            with self.assertRaises(ValueError):
                self.srv.tool_search_text({"keyword": kw})
        for p in ("/etc", "../.."):
            with self.assertRaises(ValueError):
                self.srv.tool_search_text({"keyword": "x", "path": p})
        with self.assertRaises(ValueError):
            self.srv.tool_search_text({"keyword": "x", "path": "a.txt"})  # 非目录
        for m in (0, 51, -1, "5", True, None):
            with self.assertRaises(ValueError):
                self.srv.tool_search_text({"keyword": "x", "max_matches": m})
        with self.assertRaises(ValueError):
            self.srv.tool_search_text({"keyword": "x", "evil": 1})

    def test_n2_no_match(self):
        out = self.srv.tool_search_text({"keyword": "zzz_nothing"})
        self.assertIn("无匹配", out)


class L1GitStatusDiff(unittest.TestCase):
    """#119 git_status / git_diff 工具正反例（临时 git 仓库夹具 + 执行面安全）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req119-l1g-")).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / "a.txt").write_text("a1\n")
        (self.repo / "b.txt").write_text("b1\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "c1")
        self.srv = d.ReadonlyMcpServer(self.repo, [self.repo])

    def tearDown(self):
        self.srv.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        for f in Path("/tmp").glob("req119-pwned*"):
            f.unlink(missing_ok=True)

    # ---- git_status ----
    def test_s1_clean(self):
        out = self.srv.tool_git_status({})
        self.assertIn("工作区状态", out)

    def test_s2_modified_and_untracked(self):
        (self.repo / "a.txt").write_text("a2\n")
        (self.repo / "new.txt").write_text("n\n")
        out = self.srv.tool_git_status({})
        self.assertIn(" M a.txt", out)
        self.assertIn("?? new.txt", out)

    def test_s3_repo_explicit_and_escape(self):
        out = self.srv.tool_git_status({"repo": str(self.repo)})
        self.assertIn("工作区状态", out)
        for r in ("/etc", str(self.tmp)):
            with self.assertRaises(ValueError):
                self.srv.tool_git_status({"repo": r})
        with self.assertRaises(ValueError):
            self.srv.tool_git_status({"evil": 1})

    # ---- git_diff 正例 ----
    def test_d1_worktree_diff(self):
        (self.repo / "a.txt").write_text("a2\n")
        out = self.srv.tool_git_diff({})
        self.assertIn("a.txt", out)
        self.assertIn("-a1", out)
        self.assertIn("+a2", out)

    def test_d2_cached_diff(self):
        (self.repo / "a.txt").write_text("a2\n")
        _git(self.repo, "add", "a.txt")
        out = self.srv.tool_git_diff({"args": ["--cached"]})
        self.assertIn("-a1", out)
        self.assertIn("+a2", out)

    def test_d3_name_only_stat(self):
        (self.repo / "a.txt").write_text("a2\n")
        out = self.srv.tool_git_diff({"args": ["--name-only"]})
        self.assertIn("a.txt", out)
        out2 = self.srv.tool_git_diff({"args": ["--stat"]})
        self.assertIn("a.txt", out2)

    def test_d4_path_filter(self):
        (self.repo / "a.txt").write_text("a2\n")
        (self.repo / "b.txt").write_text("b2\n")
        out = self.srv.tool_git_diff({"path": "a.txt"})
        self.assertIn("a.txt", out)
        self.assertNotIn("b.txt", out)

    def test_d5_no_diff(self):
        out = self.srv.tool_git_diff({})
        self.assertIn("无差异", out)

    # ---- git_diff 反例（fail-closed） ----
    def test_n1_bad_args(self):
        for args in (["--output", "/tmp/x"], ["-o", "/tmp/x"], ["--ext-diff"],
                     ["--no-index"], ["--", "a.txt"], ["HEAD"], ["-p"], ["--binary"],
                     ["--output=/tmp/x"], ["--no-textconv"]):
            with self.assertRaises(ValueError):
                self.srv.tool_git_diff({"args": args})

    def test_n2_args_type(self):
        for a in ("--stat", [1, 2], [None], {"a": 1}):
            with self.assertRaises(ValueError):
                self.srv.tool_git_diff({"args": a})

    def test_n3_repo_escape(self):
        for r in ("/etc", str(self.tmp)):
            with self.assertRaises(ValueError):
                self.srv.tool_git_diff({"repo": r})

    def test_n4_path_illegal(self):
        for p in ("../../etc/passwd", "/etc/passwd", "-x"):
            with self.assertRaises(ValueError):
                self.srv.tool_git_diff({"path": p})
        with self.assertRaises(ValueError):
            self.srv.tool_git_diff({"evil": 1})

    # ---- 执行面安全（/tmp/req119-pwned* 一律不得产生） ----
    def test_e1_diff_external_and_textconv(self):
        _git(self.repo, "config", "diff.external", "touch /tmp/req119-pwned1")
        _git(self.repo, "config", "diff.txt.textconv", "touch /tmp/req119-pwned1b")
        (self.repo / ".gitattributes").write_text("*.txt diff=txt\n")
        (self.repo / "a.txt").write_text("a2\n")
        out = self.srv.tool_git_diff({})
        self.assertIn("a.txt", out)
        self.assertFalse(Path("/tmp/req119-pwned1").exists())
        self.assertFalse(Path("/tmp/req119-pwned1b").exists())

    def test_e2_alias_diff(self):
        _git(self.repo, "config", "alias.diff", "!touch /tmp/req119-pwned2")
        (self.repo / "a.txt").write_text("a2\n")
        out = self.srv.tool_git_diff({})
        self.assertIn("a.txt", out)
        self.assertFalse(Path("/tmp/req119-pwned2").exists())

    def test_e3_fsmonitor(self):
        _git(self.repo, "config", "core.fsmonitor", "/bin/touch /tmp/req119-pwned3")
        (self.repo / "a.txt").write_text("a2\n")
        out = self.srv.tool_git_diff({})
        self.assertIn("a.txt", out)
        self.assertFalse(Path("/tmp/req119-pwned3").exists())

    def test_e4_status_fsmonitor(self):
        _git(self.repo, "config", "core.fsmonitor", "/bin/touch /tmp/req119-pwned4")
        (self.repo / "a.txt").write_text("a2\n")
        out = self.srv.tool_git_status({})
        self.assertIn("a.txt", out)
        self.assertFalse(Path("/tmp/req119-pwned4").exists())


class L1GitLog(unittest.TestCase):
    """git_log 工具实现正反例（临时 git 仓库夹具 + 恶意仓库执行面安全用例）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req85-l1g-")).resolve()
        self.repoA = self.tmp / "repoA"
        self.repoA.mkdir()
        _git(self.repoA, "init", "-q")
        (self.repoA / "a.txt").write_text("a1\n")
        _git(self.repoA, "add", "a.txt")
        _git(self.repoA, "commit", "-q", "-m", "c1: add a.txt")
        (self.repoA / "b.txt").write_text("b1\n")
        (self.repoA / "a.txt").write_text("a2\n")
        _git(self.repoA, "add", "a.txt", "b.txt")
        _git(self.repoA, "commit", "-q", "-m", "c2: 中文 message a+b")
        (self.repoA / "sub").mkdir()
        self.repoB = self.tmp / "repoB"
        self.repoB.mkdir()
        _git(self.repoB, "init", "-q")
        (self.repoB / "x.txt").write_text("x\n")
        _git(self.repoB, "add", "x.txt")
        _git(self.repoB, "commit", "-q", "-m", "b1: x.txt")
        self.srv = d.ReadonlyMcpServer(self.repoA, [self.repoA, self.repoB])

    def tearDown(self):
        self.srv.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        for f in Path("/tmp").glob("req85-pwned*"):
            f.unlink(missing_ok=True)

    def _fmt_re(self):
        return re.compile(r"^[0-9a-f]{7,} \d{4}-\d{2}-\d{2} \S+ .+$")

    def _make_bad_repo(self, msg="x1"):
        """构造恶意仓库 + 独立 server（roots 含该仓库，使工具能访问以测试执行面）"""
        repo = self.tmp / "repoX"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "x.txt").write_text("x\n")
        _git(repo, "add", "x.txt")
        _git(repo, "commit", "-q", "-m", msg)
        return repo, d.ReadonlyMcpServer(repo, [repo])

    # ---- 正例 ----
    def test_p1_default_history(self):
        out = self.srv.tool_git_log({})
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)  # 默认 count=15，repoA 共 2 笔
        for ln in lines:
            self.assertRegex(ln, self._fmt_re())
        self.assertIn("中文", out)  # UTF-8 中文 message 正常

    def test_p2_path_filter(self):
        out_a = self.srv.tool_git_log({"path": "a.txt"})
        self.assertEqual(len(out_a.splitlines()), 2)  # c1、c2 都动过 a.txt
        out_b = self.srv.tool_git_log({"path": "b.txt"})
        self.assertEqual(len(out_b.splitlines()), 1)  # 仅 c2 动过 b.txt

    def test_p3_count(self):
        lines = self.srv.tool_git_log({"count": 1}).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("c2", lines[0])  # 最新提交

    def test_p4_repo_explicit(self):
        lines = self.srv.tool_git_log({"repo": str(self.repoB)}).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("b1", lines[0])

    def test_p5_path_no_match(self):
        self.assertEqual(self.srv.tool_git_log({"path": "nope.txt"}), d.GIT_LOG_EMPTY_HINT)

    # ---- 反例（fail-closed，一律 error） ----
    def test_n1_repo_escape(self):
        outside = Path(tempfile.mkdtemp(prefix="req85-out-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)  # 防 $TMPDIR 泄漏
        for r in ("/etc", str(self.tmp), str(outside)):
            with self.assertRaises(ValueError):
                self.srv.tool_git_log({"repo": r})

    def test_n2_repo_non_git(self):
        nonrepo = self.tmp / "nonrepo"
        nonrepo.mkdir()
        with self.assertRaises(ValueError):
            self.srv.tool_git_log({"repo": str(nonrepo)})

    def test_n3_path_illegal(self):
        for p in ("../../etc/passwd", "/etc/passwd", "-x"):
            with self.assertRaises(ValueError):
                self.srv.tool_git_log({"path": p})
        _out = Path(tempfile.mkdtemp(prefix="req85-out-"))
        self.addCleanup(shutil.rmtree, _out, ignore_errors=True)  # 防 $TMPDIR 泄漏
        outside = _out / "s.txt"
        outside.write_text("s")
        link = self.repoA / "evil-link.txt"
        link.symlink_to(outside)
        try:
            with self.assertRaises(ValueError):
                self.srv.tool_git_log({"path": "evil-link.txt"})
        finally:
            link.unlink(missing_ok=True)

    def test_n4_count_illegal(self):
        for c in (0, 101, -1, "15", True, None, 15.5):
            with self.assertRaises(ValueError):
                self.srv.tool_git_log({"count": c})

    def test_n5_unknown_key(self):
        with self.assertRaises(ValueError):
            self.srv.tool_git_log({"repo": str(self.repoA), "evil": 1})

    def test_n6_workdir_not_git_root(self):
        nonrepo = self.tmp / "nonrepo2"
        nonrepo.mkdir()
        srv2 = d.ReadonlyMcpServer(nonrepo, [nonrepo])
        try:
            with self.assertRaises(ValueError):
                srv2.tool_git_log({})
        finally:
            srv2.close()

    def test_p6_repo_subdir(self):
        with self.assertRaises(ValueError):
            self.srv.tool_git_log({"repo": str(self.repoA / "sub")})

    # ---- 恶意仓库执行面安全（/tmp/req85-pwned* 一律不得产生） ----
    def test_s1_alias_log(self):
        repo, srv = self._make_bad_repo()
        try:
            _git(repo, "config", "alias.log", "!touch /tmp/req85-pwned")
            out = srv.tool_git_log({"repo": str(repo)})
            self.assertIn("x1", out)
            self.assertFalse(Path("/tmp/req85-pwned").exists())
        finally:
            srv.close()

    def test_s2_pager(self):
        repo, srv = self._make_bad_repo()
        try:
            _git(repo, "config", "core.pager", "!touch /tmp/req85-pwned2")
            env = dict(os.environ)
            env["GIT_PAGER"] = "!touch /tmp/req85-pwned2b"
            try:
                out = srv.tool_git_log({"repo": str(repo)})
            finally:
                os.environ.pop("GIT_PAGER", None)
            self.assertIn("x1", out)
            self.assertFalse(Path("/tmp/req85-pwned2").exists())
            self.assertFalse(Path("/tmp/req85-pwned2b").exists())
        finally:
            srv.close()

    def test_s3_diff_external(self):
        repo, srv = self._make_bad_repo()
        try:
            _git(repo, "config", "diff.external", "touch /tmp/req85-pwned3")
            _git(repo, "config", "diff.txt.textconv", "touch /tmp/req85-pwned3b")
            (repo / ".gitattributes").write_text("*.txt diff=txt\n")
            (repo / "x.txt").write_text("x2\n")
            _git(repo, "add", ".gitattributes", "x.txt")
            _git(repo, "commit", "-q", "-m", "x2")
            out = srv.tool_git_log({"repo": str(repo)})
            self.assertIn("x2", out)
            self.assertFalse(Path("/tmp/req85-pwned3").exists())
            self.assertFalse(Path("/tmp/req85-pwned3b").exists())
        finally:
            srv.close()

    def test_s4_fsmonitor(self):
        repo, srv = self._make_bad_repo()
        try:
            _git(repo, "config", "core.fsmonitor", "/bin/touch /tmp/req85-pwned4")
            out = srv.tool_git_log({"repo": str(repo)})
            self.assertIn("x1", out)
            self.assertFalse(Path("/tmp/req85-pwned4").exists())
        finally:
            srv.close()

    def test_s5_git_dir_env(self):
        """进程环境 GIT_DIR 指向外部仓库：env 消毒后工具仍查目标仓库"""
        os.environ["GIT_DIR"] = str(self.repoB / ".git")
        try:
            out = self.srv.tool_git_log({"repo": str(self.repoA)})
        finally:
            os.environ.pop("GIT_DIR", None)
        self.assertIn("中文", out)
        self.assertNotIn("b1: x.txt", out)  # 按提交 message 判断，不依赖随机 hash（hash 可能碰巧含 "b1"）

    def test_s6_output_limit(self):
        repo, srv = self._make_bad_repo()
        try:
            (repo / "x.txt").write_text("x2\n")
            _git(repo, "add", "x.txt")
            _git(repo, "commit", "-q", "-m", "L" * 300)  # 单行超 200 字符
            lines = srv.tool_git_log({"repo": str(repo), "count": 100}).splitlines()
            self.assertLessEqual(len(lines), 100)
            self.assertTrue(all(len(ln) <= d.GIT_LOG_LINE_MAX for ln in lines))
        finally:
            srv.close()

    def test_s7_empty_repo(self):
        repo = self.tmp / "repoEmpty"
        repo.mkdir()
        _git(repo, "init", "-q")
        with self.assertRaises(ValueError):  # git exit 128，fail-closed
            self.srv.tool_git_log({"repo": str(repo)})


class ReadonlyPipelineSafeTest(unittest.TestCase):
    """readonly_pipeline_safe / _split_shell：拼接命令逐段校验，任何一段不合法整体拒绝"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req-pipe-")).resolve()
        (self.tmp / "a;b").write_text("x")   # 带分号文件名（引号内分号不切段的验证用）

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_p_simple_pipe_and_seq(self):
        # 分号拼接：两段各自路径在 roots
        self.assertTrue(ac.readonly_pipeline_safe("ls -la; ls b", [self.tmp], base=self.tmp))
        self.assertTrue(ac.readonly_pipeline_safe("date; pwd; ls", [self.tmp], base=self.tmp))
        self.assertTrue(ac.readonly_pipeline_safe(
            'echo "---"; ls "a;b" 2>/dev/null; pwd', [self.tmp], base=self.tmp))
        self.assertTrue(ac.readonly_pipeline_safe(
            "ls -la 2>&1 | head -5", [self.tmp], base=self.tmp))
        self.assertTrue(ac.readonly_pipeline_safe("pwd 1>/dev/null", [self.tmp], base=self.tmp))
        # 管道：右侧 head 无路径 stdin 模式
        self.assertTrue(ac.readonly_pipeline_safe("ls | head", [self.tmp], base=self.tmp))
        self.assertTrue(ac.readonly_pipeline_safe("ls -la | head -5", [self.tmp], base=self.tmp))
        self.assertTrue(ac.readonly_pipeline_safe("find . -type d | head -20", [self.tmp], base=self.tmp))
        # 引号内分号不切段：单段 cat，路径含分号仍在 roots
        self.assertTrue(ac.readonly_pipeline_safe('cat "a;b"', [self.tmp], base=self.tmp))
        # 单段兼容：与 readonly_cmd_safe 等价
        self.assertTrue(ac.readonly_pipeline_safe("date", [self.tmp], base=self.tmp))

    def test_n_non_whitelist_or_escape(self):
        # 任一段不合法 → 整体拒绝
        self.assertFalse(ac.readonly_pipeline_safe("ls; rm -rf /", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls; cat /etc/passwd", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("cat /etc/passwd | head", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls | grep x", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls | cat", [self.tmp], base=self.tmp))  # cat 不放 stdin
        # 重定向/通配符/&&：不在切分符内，段内判不合法
        self.assertFalse(ac.readonly_pipeline_safe("ls > /tmp/f", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls 2>/tmp/f", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls 2>/dev/null >/tmp/f", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("echo $(whoami)", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("echo `whoami`", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("pwd /tmp", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls /tmp 2>/dev/null", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls *.md", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls && ls", [self.tmp], base=self.tmp))
        # 空段/未闭合引号
        self.assertFalse(ac.readonly_pipeline_safe("ls |", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("ls;;ls", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe("| ls", [self.tmp], base=self.tmp))
        self.assertFalse(ac.readonly_pipeline_safe('ls "a', [self.tmp], base=self.tmp))
        # find 段内禁令保留
        self.assertFalse(ac.readonly_pipeline_safe("find . -exec rm", [self.tmp], base=self.tmp))
        # 空/非命令
        self.assertFalse(ac.readonly_pipeline_safe("", [self.tmp], base=self.tmp))

    def test_allow_stdin_gate(self):
        # 仅管道右侧段放行 head/tail/wc 无路径；cat/file 等维持需路径；ls 无需路径不受影响
        self.assertFalse(ac.readonly_cmd_safe("head", [self.tmp], base=self.tmp))
        self.assertTrue(ac.readonly_cmd_safe("head", [self.tmp], base=self.tmp, allow_stdin=True))
        self.assertTrue(ac.readonly_cmd_safe("tail -5", [self.tmp], base=self.tmp, allow_stdin=True))
        self.assertTrue(ac.readonly_cmd_safe("wc -l", [self.tmp], base=self.tmp, allow_stdin=True))
        self.assertFalse(ac.readonly_cmd_safe("cat", [self.tmp], base=self.tmp, allow_stdin=True))
        self.assertFalse(ac.readonly_cmd_safe("file", [self.tmp], base=self.tmp, allow_stdin=True))
        self.assertTrue(ac.readonly_cmd_safe("ls", [self.tmp], base=self.tmp))


class GitCmdSafeTest(unittest.TestCase):
    """git_cmd_safe（#302 需求对话放开 add/commit）纯函数正反例：
    只读子命令 + add/commit 放行；push/checkout/reset/rebase 等写子命令、-C/--git-dir 重定向、
    越权路径、拼接一律拒绝（fail-closed）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req302-git-")).resolve()
        (self.tmp / "src").mkdir()
        self.roots = [self.tmp]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_p_readonly_subcmds(self):
        for cmd in ("git status", "git status -s", "git status --porcelain",
                    "git log --oneline -5", "git log -1 -- src/", "git diff",
                    "git diff --stat HEAD", "git show HEAD", "git ls-files -o",
                    "git check-ignore .env", "git grep foo", "git blame src/a.py",
                    "git rev-parse --show-toplevel", "git branch", "git branch -a",
                    "git branch -vv", "git branch --merged", "git tag", "git tag -l 'v*'",
                    "git worktree list", "git worktree list --porcelain",
                    "git remote -v", "git remote get-url origin", "git remote show origin",
                    "git config user.name", "git config --get user.email",
                    "git --version", "git --help"):
            self.assertTrue(ac.git_cmd_safe(cmd, self.roots, base=self.tmp), cmd)

    def test_p_add_commit(self):
        for cmd in ("git add .", "git add src", "git add src/a.py", "git add -A",
                    "git add -u", "git add -A src", "git add -- src/a.py",
                    "git add -n .", "git commit -m 'fix: x'", "git commit -am 'fix: x'",
                    "git commit -a -m 'fix'", "git commit --no-verify -m 'x'",
                    "git commit --allow-empty -m 'x'", "git commit -m 'x' -- src/a.py",
                    "git commit --allow-empty-message", "git commit -s -m 'x'"):
            self.assertTrue(ac.git_cmd_safe(cmd, self.roots, base=self.tmp), cmd)

    def test_p_cd_and_pipeline_form(self):
        """kimi 实战形态（#302 反馈：agent 习惯 cd <项目> && git ...）：cd 目标在 roots 内
        的 && 拼接放行（多段 git 也可），路径校验仍以 base=workdir 为准不扩大"""
        wd = str(self.tmp)
        for cmd in (f"cd {wd} && git status", f"cd {wd} && git status -s",
                    f"cd {wd} && git add .", f"cd {wd} && git add src/a.py",
                    f"cd {wd} && git commit -m 'fix: x'",
                    f"cd {wd} && git add src/a.py && git commit -m 'fix: x'",
                    f"cd {wd} && git status && git add src && git commit -am 'fix'",
                    f"cd src && git add a.py", f"cd {wd} && git worktree list",
                    f"cd '{wd}' && git log --oneline -3",
                    "git add . && git commit -m x", "git status && git add -A"):
            self.assertTrue(ac.git_cmd_safe(cmd, self.roots, base=self.tmp), cmd)

    def test_n_cd_and_pipeline_form(self):
        """cd 越权/echo 等其它拼接段/危险 git 段 → 整体拒绝"""
        wd = str(self.tmp)
        for cmd in (f"cd /etc && git status", f"cd {wd}/.. && git status",
                    f"cd {wd} && echo x && git status", f"cd {wd} && git push",
                    f"cd {wd} && git commit --amend -m x", f"cd {wd} && git add -f .env",
                    "cd && git status", f"cd {wd}; git status", f"cd {wd} | git status"):
            self.assertFalse(ac.git_cmd_safe(cmd, self.roots, base=self.tmp), cmd)

    def test_n_write_subcmds(self):
        for cmd in ("git push", "git push origin main", "git checkout main",
                    "git switch main", "git reset --hard", "git rebase main",
                    "git merge main", "git cherry-pick abc", "git revert abc",
                    "git clean -fd", "git restore src", "git rm src/a.py",
                    "git mv a b", "git stash", "git stash list", "git fetch origin",
                    "git pull", "git clone https://x", "git init",
                    "git branch new", "git branch -d old", "git branch -m new",
                    "git tag v1", "git tag -d v1", "git worktree add ../wt",
                    "git worktree remove wt", "git remote add o x", "git remote set-url o x",
                    "git config user.name x", "git config --add x y"):
            self.assertFalse(ac.git_cmd_safe(cmd, self.roots, base=self.tmp), cmd)

    def test_n_force_amend_redirect_escape(self):
        for cmd in ("git add -f .env", "git add -p", "git commit --amend -m x",
                    "git commit -F msg.txt", "git commit --no-edit", "git commit -C abc",
                    "git commit -S -m x", "git -C /etc status", "git --git-dir=/x status",
                    "git --work-tree=/x status", "git add /etc/passwd", "git add ../outside",
                    "git add ~/x", "git log --output=/tmp/x", "git status; rm -rf /",
                    "git status | grep x", "git commit -m x > /tmp/f", "git commit -m",
                    "git commit", "git add", "git", "", "ls", "git push; ls"):
            self.assertFalse(ac.git_cmd_safe(cmd, self.roots, base=self.tmp), cmd)

    def test_p_merge_subcmds(self):
        """#355 需求助手合并授权（allow_merge=True）：merge/checkout/switch/stash 受限形态放行"""
        for cmd in ("git merge main", "git merge f-20260810-284", "git merge origin/main",
                    "git merge --no-ff main", "git merge --no-edit --no-ff origin/main",
                    "git merge --abort", "git merge --continue --no-edit",
                    "git checkout main", "git checkout f-20260810-284",
                    "git switch main", "git switch f-x/y",
                    "git stash", "git stash list", "git stash show", "git stash push",
                    "git stash push -m 'temp'", "git stash pop", "git stash apply"):
            self.assertTrue(ac.git_cmd_safe(cmd, self.roots, base=self.tmp, allow_merge=True), cmd)

    def test_n_merge_subcmds(self):
        """#xxx 合并授权下的危险/无效形态仍拒（fail-closed）；默认（无授权）时合并子命令维持全拒"""
        for cmd in ("git merge", "git merge --no-ff", "git merge -s ours main",
                    "git merge -X theirs main", "git merge main dev", "git merge --no-edit --no-ff",
                    "git checkout -b new", "git checkout -f main", "git checkout -- src/a.py",
                    "git checkout main --", "git checkout main src/a.py", "git checkout -q main",
                    "git switch -c new", "git switch --discard-changes main", "git switch -f main",
                    "git stash drop", "git stash clear", "git stash push -u", "git stash push -m",
                    "git stash pop stash@{0}", "git stash branch b", "git stash store"):
            self.assertFalse(ac.git_cmd_safe(cmd, self.roots, base=self.tmp, allow_merge=True), cmd)
        # 默认（allow_merge=False）时合并子命令全部拒绝（既有反例语义维持，不因新开关扩大）
        for cmd in ("git merge main", "git checkout main", "git switch main",
                    "git stash", "git stash list"):
            self.assertFalse(ac.git_cmd_safe(cmd, self.roots, base=self.tmp), cmd)


class L2ApprovalUnchanged(unittest.TestCase):
    """_auto_approve / _handle_request 行为不变性（桩实例，不起 kimi 子进程）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req78-l2-"))
        self.art = self.tmp / "art"
        self.art.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _approved(self, outcome):
        return outcome.get("outcome", {}).get("outcome") == "selected"

    def test_b1_dev_bash_allow(self):
        s = _stub_session(role="dev")
        self.assertTrue(self._approved(s._auto_approve(_req_permission_params("Requesting approval to Running: echo hi"))))

    def test_b2_accept_git_reject(self):
        s = _stub_session(role="accept")
        for cmd in ("Requesting approval to Running: git commit -m x", "Requesting approval to Running: git push origin main"):
            self.assertFalse(self._approved(s._auto_approve(_req_permission_params(cmd))))

    def test_b3_accept_pytest_allow(self):
        s = _stub_session(role="accept")
        self.assertTrue(self._approved(s._auto_approve(_req_permission_params("Requesting approval to Running: pytest"))))

    def test_b6_readonly_bash_whitelist(self):
        """#119 只读角色 Bash 审批：剥离审批前缀后白名单内命令（roots 内）放行；
        拼接命令逐段校验（每段白名单内即放行）；越权路径/非白名单命令（git log）仍拒绝"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        # 白名单命令 + 带审批前缀：放行（#119 修复：前缀剥离后再校验）
        for cmd in ("Requesting approval to Running: ls data/",
                    "Requesting approval to Running: cat AGENTS.md",
                    "Requesting approval to Running: wc -l AGENTS.md",
                    "Requesting approval to Running: head -3 AGENTS.md",
                    "Requesting approval to Running: date",
                    "Requesting approval to Running: date -u",
                    "Requesting approval to Running: pwd",
                    "Requesting approval to Running: echo ---",
                    # 简单只读拼接（逐段白名单校验）：放行
                    "Requesting approval to Running: ls data/; ls data/",
                    "Requesting approval to Running: date; pwd; ls",
                    "Requesting approval to Running: ls data/ 2>/dev/null",
                    "Requesting approval to Running: ls data/ 2>&1",
                    "Requesting approval to Running: cat AGENTS.md | head -3",
                    "Requesting approval to Running: find . -type f | head -10"):
            self.assertTrue(self._approved(s._auto_approve(_req_permission_params(cmd))), cmd)
        # dbq 绝对路径形态（prefix 为绝对路径）：放行
        abs_dbq = f"Requesting approval to Running: python3 {ac.DBQ_SCRIPT} data/board.db \"SELECT 1\""
        self.assertTrue(self._approved(s._auto_approve(_req_permission_params(abs_dbq))))
        # 相对路径形态 / 越权 / 拼接（#357 v2：审批层预检未命中 → approve，
        # 安全由 terminal 层复核 fail-closed 保证——以下全部在 terminal 层拒绝）
        for cmd, tcmd in (("Requesting approval to Running: python3 scripts/dbq.py data/board.db \"SELECT 1\"",
                           "python3 scripts/dbq.py data/board.db \"SELECT 1\""),
                          # 非白名单命令（git，#85 约束不扩）
                          ("Requesting approval to Running: git log --oneline -15 -- dispatcher.py",
                           "git log --oneline -15 -- dispatcher.py"),
                          ("Requesting approval to Running: git log -1", "git log -1"),
                          # 越权路径仍拒绝
                          ("Requesting approval to Running: cat /etc/passwd", "cat /etc/passwd"),
                          ("Requesting approval to Running: ls ~/xx", "ls ~/xx"),
                          # 拼接中任一段不合法 → 整体拒绝（&& 非切分符，段内 & 判不合法）
                          ("Requesting approval to Running: ls data/ && cat AGENTS.md", "ls data/ && cat AGENTS.md"),
                          ("Requesting approval to Running: cat AGENTS.md | grep xxx", "cat AGENTS.md | grep xxx"),
                          ("Requesting approval to Running: cat AGENTS.md > /tmp/x", "cat AGENTS.md > /tmp/x"),
                          ("Requesting approval to Running: ls data/ 2>/tmp/x", "ls data/ 2>/tmp/x"),
                          ("Requesting approval to Running: echo $(whoami)", "echo $(whoami)"),
                          ("Requesting approval to Running: pwd /tmp", "pwd /tmp"),
                          ("Requesting approval to Running: ls *.md", "ls *.md"),
                          ("Requesting approval to Running: cat /etc/passwd | head -3", "cat /etc/passwd | head -3"),
                          ("Requesting approval to Running: ls data/; rm -rf data/", "ls data/; rm -rf data/")):
            self.assertTrue(self._approved(s._auto_approve(_req_permission_params(cmd))), cmd)
            with self.assertRaises(PermissionError):
                s._terminal_approve(tcmd)
        # 白名单层也不放行 git（#85 验收：Bash git log 仍被拒，只读约束不放松）
        self.assertFalse(d.readonly_cmd_safe("git log --oneline -15 -- dispatcher.py", [self.tmp]))
        self.assertFalse(d.readonly_cmd_safe("git log -1", [self.tmp]))

    def test_reqchat_git_commit_allow(self):
        """#302 需求对话会话（allow_git_commit=True）Bash git 只读子命令 + add/commit 放行；
        push/改写历史/强制 add 等仍拒绝；默认会话（开关缺省 False）git 仍全拒（#85 不放松）"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art, allow_git_commit=True)
        for cmd in ("Requesting approval to Running: git status -s",
                    "Requesting approval to Running: git diff",
                    "Requesting approval to Running: git log --oneline -5",
                    "Requesting approval to Running: git branch",
                    "Requesting approval to Running: git worktree list",
                    "Requesting approval to Running: git add .",
                    "Requesting approval to Running: git add src/a.py",
                    "Requesting approval to Running: git commit -m 'fix: 字段对齐'",
                    "Requesting approval to Running: git commit -am 'fix'",
                    # #302 实战形态：cd <项目目录> && git ... 拼接
                    f"Requesting approval to Running: cd {self.tmp} && git add src/a.py",
                    f"Requesting approval to Running: cd {self.tmp} && git add src/a.py && git commit -m 'fix'"):
            self.assertTrue(self._approved(s._auto_approve(_req_permission_params(cmd))), cmd)
        # #357 v2：审批层预检未命中 → approve；安全由 terminal 层复核（fail-closed）保证
        for cmd, tcmd in (("Requesting approval to Running: git push origin main", "git push origin main"),
                          ("Requesting approval to Running: git checkout main", "git checkout main"),
                          ("Requesting approval to Running: git reset --hard", "git reset --hard"),
                          ("Requesting approval to Running: git rebase main", "git rebase main"),
                          ("Requesting approval to Running: git commit --amend -m x", "git commit --amend -m x"),
                          ("Requesting approval to Running: git add -f .env", "git add -f .env"),
                          ("Requesting approval to Running: git -C /etc status", "git -C /etc status"),
                          ("Requesting approval to Running: cd /etc && git status", "cd /etc && git status")):
            self.assertTrue(self._approved(s._auto_approve(_req_permission_params(cmd))), cmd)
            with self.assertRaises(PermissionError):
                s._terminal_approve(tcmd)
        # 默认会话（无开关）：审批层预检 miss → approve，但 terminal 层 git 仍全拒（fail-closed）
        s2 = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        for cmd in ("git status", "git commit -m x"):
            self.assertTrue(self._approved(s2._auto_approve(_req_permission_params(
                f"Requesting approval to Running: {cmd}"))), cmd)
            with self.assertRaises(PermissionError):
                s2._terminal_approve(cmd)

    def test_reqchat_git_merge_allow(self):
        """#355 需求助手合并授权（allow_git_merge=True）：Bash git merge/checkout/switch/stash
        受限形态 + cd 拼接放行；push/reset 等与危险形态仍拒；fs 写放行 workdir 内（解决合并冲突），
        越界仍拒"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art,
                          allow_git_commit=True, allow_git_merge=True)
        for cmd in ("Requesting approval to Running: git merge main",
                    "Requesting approval to Running: git merge --abort",
                    "Requesting approval to Running: git merge --continue --no-edit",
                    "Requesting approval to Running: git checkout main",
                    "Requesting approval to Running: git switch f-x/y",
                    "Requesting approval to Running: git stash",
                    "Requesting approval to Running: git stash push -m 'temp'",
                    "Requesting approval to Running: git stash pop",
                    f"Requesting approval to Running: cd {self.tmp} && git merge main",
                    "Requesting approval to Running: git add src/a.py && git commit -m 'merge fix'"):
            self.assertTrue(self._approved(s._auto_approve(_req_permission_params(cmd))), cmd)
        # #357 v2：审批层预检未命中 → approve；terminal 层完整 script 复核拒绝（fail-closed）
        for cmd in ("git push origin main", "git checkout -b new", "git checkout -f main",
                    "git merge -s ours main", "git merge main dev", "git stash drop",
                    "git stash pop stash@{0}", "git reset --hard"):
            self.assertTrue(self._approved(s._auto_approve(_req_permission_params(
                f"Requesting approval to Running: {cmd}", tool_call_id="t_merge"))), cmd)
            with self.assertRaises(PermissionError):
                s._terminal_approve(cmd)
        # fs 写：合并授权下 workdir 内路径放行（本进程代办落地）；workdir 外仍拒
        s.allow_fs_write = True
        s.write_roots = [self.tmp.resolve()]  # 对齐 ACPSession 构造时的 resolve（macOS /var→/private/var 软链）
        sent = []
        s._send = lambda *a, **k: sent.append(a[0])
        s._handle_request({"id": 1, "method": "fs/write_text_file",
                           "params": {"path": str(self.tmp / "conflict.md"), "content": "x"}})
        self.assertTrue((self.tmp / "conflict.md").exists(), "workdir 内 fs 写应放行")
        self.assertNotIn("error", sent[0])
        s._handle_request({"id": 2, "method": "fs/write_text_file",
                           "params": {"path": "/etc/req-merge-blocked", "content": "x"}})
        self.assertIn("error", sent[1])

    def test_reqchat_merge_write_roots(self):
        """#355 合并授权：_in_workdir 判定（workdir 内/自身/越界）"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art,
                          allow_git_commit=True, allow_git_merge=True)
        self.assertTrue(s._in_workdir(self.tmp / "a.py"))
        self.assertTrue(s._in_workdir(self.tmp))
        self.assertFalse(s._in_workdir(self.tmp.parent / "outside.py"))

    def test_reqchat_bash_truncated_cmd_uses_cache(self):
        """#357 v2 审批预检：缓存完整命令命中 → approve；无缓存（kimi 完整参数流在审批
        响应后才发，审批层等待即死锁）→ 也 approve，安全由 terminal 层复核（fail-closed）
        保证——危险命令在 terminal 层仍拒"""
        wd = str(self.tmp)
        tid = "3:call_test"
        # 截断文本：kimi 把审批展示文本截到 ~85 字符（含前缀），长命令尾部丢失（git statu…）
        truncated = f"Requesting approval to Running: cd {wd} && git statu\u2026"
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art,
                          allow_git_commit=True, allow_git_merge=True)
        s.readonly_roots = [self.tmp]
        # 无缓存（参数流未到）→ 预检未命中也 approve（terminal 层复核；同形态完整命令会放行）
        self.assertTrue(self._approved(s._auto_approve(_req_permission_params(truncated, tool_call_id=tid))))
        # 缓存完整命令命中 → approve
        s._tool_cmds[tid] = f"cd {wd} && git status -s"
        self.assertTrue(self._approved(s._auto_approve(_req_permission_params(truncated, tool_call_id=tid))))
        # 危险命令：审批层预检未命中（截断）→ approve，但 terminal 层完整 script 复核拒绝
        s._tool_cmds[tid] = f"cd {wd} && git push origin main"
        self.assertTrue(self._approved(s._auto_approve(_req_permission_params(truncated, tool_call_id=tid))))
        with self.assertRaises(PermissionError):
            s._terminal_approve(f"cd {wd} && git push origin main")

    def test_wait_tool_cmd(self):
        """#357 _wait_tool_cmd：缓存命中立即返回；无缓存等待超时返回 None（调用方回退文本）；
        空 toolCallId 直接 None"""
        s = _stub_session()
        s._tool_cmds = {"t1": "git status -s"}
        self.assertEqual(s._wait_tool_cmd("t1"), "git status -s")
        self.assertIsNone(s._wait_tool_cmd("t2", timeout=0.05))
        self.assertIsNone(s._wait_tool_cmd("", timeout=0.05))

    def test_terminal_approve_readonly_same_source(self):
        """#355/#357 terminal 委托层（upstream#351）只读分支与 _auto_approve Bash 白名单同源：
        git 只读/合并授权命令与只读命令/管道放行；push/reset 等与越权仍拒；
        无授权会话 git 仍拒（fail-closed）"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art,
                          allow_git_commit=True, allow_git_merge=True)
        s.readonly_roots = [self.tmp]
        for script in ("git status -s", "git merge --abort", "git merge main",
                       f"cd {self.tmp} && git status -s",
                       "ls -la", "date; pwd; ls", "echo ---",
                       "ls data/ 2>/dev/null; pwd 2>&1",
                       "ls data/; ls data/", "cat AGENTS.md | head -3"):
            s._terminal_approve(script)  # 无异常即放行
        for script in ("git push origin main", "git reset --hard", "git checkout -b x",
                       "git stash drop", "rm -rf /", "git -C /etc status"):
            with self.assertRaises(PermissionError):
                s._terminal_approve(script)
        # 无授权会话（allow_git_commit=False）：git 仍拒
        s2 = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        s2.readonly_roots = [self.tmp]
        for script in ("git status", "git commit -m x"):
            with self.assertRaises(PermissionError):
                s2._terminal_approve(script)
        # 非只读角色（dev）：不过白名单（全放行路径，仅红线/主工作区护栏）
        s3 = _stub_session(role="dev", workdir=self.tmp, artifact_root=self.art)
        s3.main_ws_roots = [self.tmp]
        s3._terminal_approve("git commit -m x")
        with self.assertRaises(PermissionError):
            s3._terminal_approve(f"git -C {self.tmp} push origin main")

    def test_readonly_bash_denied_event_is_actionable(self):
        """白名单拒绝不能只让 agent 自述“Bash 被限制”：事件流要给出原因与可用工具。"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        s.readonly_roots = [self.tmp]
        with self.assertRaises(PermissionError):
            s._terminal_approve("pnpm test")
        self.assertEqual(len(s.emitted), 1)
        kind, content = s.emitted[0]
        self.assertEqual(kind, "system")
        self.assertIn("pnpm test", content)
        self.assertIn("ls/cat/head/tail/wc/file/stat/find/date/echo/pwd", content)
        self.assertIn("2>/dev/null", content)
        # 同会话多次被拒只留痕一次，避免重试刷屏
        with self.assertRaises(PermissionError):
            s._terminal_approve("rm -rf /")
        self.assertEqual(len(s.emitted), 1)

    def test_terminal_script_unwrap(self):
        """#357 _terminal_script：kimi 实际发送 command=/bin/bash（绝对路径）时正确剥离
        -c 脚本，不再走 join 兜底把引号语义弄丢（只读角色 Bash 全灭根因）"""
        s = _stub_session()
        self.assertEqual(
            s._terminal_script({"command": "/bin/bash",
                                "args": ["-c", "cd '/Users/dev/project/req-board' && git status -s"]}),
            "cd '/Users/dev/project/req-board' && git status -s")
        self.assertEqual(s._terminal_script({"command": "bash", "args": ["-c", "git status -s"]}),
                         "git status -s")
        self.assertEqual(s._terminal_script({"command": "git", "args": ["status", "-s"]}),
                         "git status -s")
        # 还原后的 -c 脚本形态在 terminal 层白名单放行（cd 目标在 roots 内）
        s2 = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art,
                           allow_git_commit=True, allow_git_merge=True)
        s2.readonly_roots = [self.tmp]
        s2._terminal_approve(f"cd '{self.tmp}' && git status -s")  # 无异常即放行

    def test_b4_b5_readonly_write_artifact_gate(self):
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        inner = _req_permission_params(f"Requesting approval to Writing {self.art / 'x.md'}", title="Write")
        self.assertTrue(self._approved(s._auto_approve(inner)))
        outer = _req_permission_params("Requesting approval to Writing /tmp/x.md", title="Write")
        self.assertFalse(self._approved(s._auto_approve(outer)))

    def test_b7_fs_write_artifact_gate(self):
        """fs/write_text_file 白名单逻辑不变（_in_artifact）"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        self.assertTrue(s._in_artifact(self.art / "a.md"))
        self.assertTrue(s._in_artifact(self.art / "result.json"))
        self.assertFalse(s._in_artifact(self.tmp / "x.md"))

    # ---- #434 三处闸门同源：显式 write_roots 在审批层/fs 落地层同样生效 ----
    def test_b4_b5_write_roots_alignment(self):
        """只读会话显式配了 write_roots（需求助手 = 项目工作目录 + /tmp）时：
        白名单内的 Write 审批放行、fs 落地放行；白名单外仍拒。
        此前两处只听 _in_artifact（#107 遗留），与 #303 的 write_roots 各说各话——
        2026-09-19 需求助手 /tmp 探针即因此被拒并取消整轮。"""
        tmp_root = Path(tempfile.mkdtemp(prefix="req434-tmp-"))
        self.addCleanup(shutil.rmtree, tmp_root, ignore_errors=True)
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        s.write_roots = [self.tmp.resolve(), tmp_root.resolve()]
        inside = _req_permission_params(f"Requesting approval to Writing {tmp_root / 'probe.md'}", title="Write")
        self.assertTrue(self._approved(s._auto_approve(inside)))
        outside = _req_permission_params("Requesting approval to Writing /etc/req434-probe.md", title="Write")
        self.assertFalse(self._approved(s._auto_approve(outside)))
        # fs 落地层同源：白名单内写落盘、白名单外返回错误
        s.allow_fs_write = True
        s.sent = []
        s._send = lambda msg: s.sent.append(msg)
        s._handle_request({"id": 1, "method": "fs/write_text_file",
                           "params": {"path": str(tmp_root / "probe.md"), "content": "probe"}})
        self.assertTrue((tmp_root / "probe.md").read_text() == "probe")
        s._handle_request({"id": 2, "method": "fs/write_text_file",
                           "params": {"path": "/etc/req434-blocked.md", "content": "x"}})
        self.assertIn("error", s.sent[1])

    def test_b4_b5_write_roots_absent_keeps_artifact_only(self):
        """未配 write_roots 的会话（建卡等）默认不变：审批与 fs 都只认产物目录（fail-closed）"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        self.assertFalse(getattr(s, "write_roots", None))
        self.assertFalse(self._approved(s._auto_approve(
            _req_permission_params("Requesting approval to Writing /tmp/x.md", title="Write"))))
        s.allow_fs_write = True
        s.sent = []
        s._send = lambda msg: s.sent.append(msg)
        s._handle_request({"id": 3, "method": "fs/write_text_file",
                           "params": {"path": "/tmp/req434-should-not-land.md", "content": "x"}})
        self.assertIn("error", s.sent[0])
        self.assertFalse(Path("/tmp/req434-should-not-land.md").exists())

    def test_b9_readonly_mcp_servers_ok(self):
        """只读角色 _readonly_mcp_servers 返回可用的 MCP 条目；dev 角色不创建"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        try:
            entries = s._readonly_mcp_servers(self.tmp)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["type"], "http")
            self.assertEqual(entries[0]["name"], d.READONLY_TOOL_SERVER)
            self.assertIn("127.0.0.1", entries[0]["url"])
        finally:
            if s._mcp:
                s._mcp.close()
        s2 = _stub_session(role="dev", workdir=self.tmp, artifact_root=self.art)
        self.assertEqual(s2._readonly_mcp_servers(self.tmp), [])

    def test_b10_readonly_mcp_tools_allowed(self):
        """#82/#119 只读角色 mcp__reqboard-tools__* 工具调用放行（七个只读工具）"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        for tool in ("list_dir", "read_file", "db_query", "git_log",
                     "search_text", "git_status", "git_diff"):
            p = _req_permission_params(f"Requesting approval to {tool}",
                                       title=f"mcp__{d.READONLY_TOOL_SERVER}__{tool}")
            self.assertTrue(self._approved(s._auto_approve(p)), tool)
        for role in ("proposal", "tech"):
            s2 = _stub_session(role=role, workdir=self.tmp, artifact_root=self.art)
            p = _req_permission_params("Requesting approval to db_query",
                                       title=f"mcp__{d.READONLY_TOOL_SERVER}__db_query")
            self.assertTrue(self._approved(s2._auto_approve(p)), role)

    def test_b11_foreign_mcp_tool_still_rejected(self):
        """#82 非本 server 的 MCP 工具（mcp__<other>__*）仍拒绝，前缀严格带尾双下划线"""
        s = _stub_session(role="analysis", workdir=self.tmp, artifact_root=self.art)
        for title in ("mcp__github__pr_list", "mcp__reqboard-tools_evil__list_dir",
                      "mcp__reqboard-tools", "mcp__other__write"):
            p = _req_permission_params("Requesting approval to x", title=title)
            self.assertFalse(self._approved(s._auto_approve(p)), title)
        # dev 角色不因新分支受影响：非本 server MCP 工具仍全放行（dev 全放行语义不变）
        s2 = _stub_session(role="dev", workdir=self.tmp, artifact_root=self.art)
        p = _req_permission_params("Requesting approval to x", title="mcp__github__pr_list")
        self.assertTrue(self._approved(s2._auto_approve(p)))


class PlaywrightChatAllowance(unittest.TestCase):
    """#268 建卡会话 Playwright 调研放行（保底安全）：
    L2 双清单放行/硬拒 + URL 协议校验；L3 事件流 URL 兜底取消；dispatcher 会话行为零变化。"""

    CHAT_PREFIXES = ["mcp__reqboard-tools__", "mcp__reqboard-create__", "mcp__playwright__"]

    def _approved(self, outcome):
        return outcome.get("outcome", {}).get("outcome") == "selected"

    def _chat_session(self):
        s = _stub_session(role="analysis")
        s._allow_prefixes = list(self.CHAT_PREFIXES)
        return s

    def _l3_stub(self, allow_prefixes):
        """L3 用例桩（参照 test_chat_self_heal.FakeSession）：不发真实 kimi，记录 _emit/cancel 调用。"""
        s = ac.ACPSession.__new__(ac.ACPSession)
        s.role = "analysis"
        s.req_id = 99999
        s.session_id = "stub-session"
        s._allow_prefixes = list(allow_prefixes)
        s._tool_posted = set()
        s._tool_calls = {}
        s._msg_buf = []
        s._buf_lock = threading.Lock()
        s._capture = None
        s.emitted = []
        s.cancelled = 0
        s._emit = lambda kind, content, session_id=None: s.emitted.append((kind, content))
        s.cancel = lambda: setattr(s, "cancelled", s.cancelled + 1)
        return s

    def test_constants_whitelist_denylist(self):
        """#268 常量：前缀/双清单/计数防漂移（ALLOWED 恰 20 项，DENIED 4 项，无交集）"""
        self.assertEqual(ac.PLAYWRIGHT_TOOL_PREFIX, "mcp__playwright__")
        self.assertEqual(len(ac.PLAYWRIGHT_ALLOWED_TOOLS), 20)
        self.assertEqual(len(ac.PLAYWRIGHT_DENIED_TOOLS), 4)
        self.assertTrue(ac.PLAYWRIGHT_ALLOWED_TOOLS.isdisjoint(ac.PLAYWRIGHT_DENIED_TOOLS))
        for tool in ac.PLAYWRIGHT_DENIED_TOOLS:
            self.assertTrue(tool.startswith("browser_"), tool)

    def test_url_protocol_violation_pure(self):
        """#268 _url_protocol_violation 正反例：http(s) 放行，file/data/javascript/chrome 违规"""
        self.assertIsNone(ac._url_protocol_violation(None))
        self.assertIsNone(ac._url_protocol_violation(""))
        self.assertIsNone(ac._url_protocol_violation("http://127.0.0.1:8899"))
        self.assertIsNone(ac._url_protocol_violation("https://example.com/a?b=1"))
        self.assertIsNotNone(ac._url_protocol_violation("file:///etc/passwd"))
        self.assertIsNotNone(ac._url_protocol_violation("data:text/html,x"))
        self.assertIsNotNone(ac._url_protocol_violation("javascript:alert(1)"))
        self.assertIsNotNone(ac._url_protocol_violation("chrome://settings"))

    def test_l2_allowed_tools_approved(self):
        """#268 建卡会话桩：放行清单 20 项逐一放行"""
        s = self._chat_session()
        for tool in sorted(ac.PLAYWRIGHT_ALLOWED_TOOLS):
            p = _req_permission_params(f"Requesting approval to {tool}",
                                       title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}{tool}")
            self.assertTrue(self._approved(s._auto_approve(p)), tool)

    def test_l2_denied_tools_rejected(self):
        """#268 建卡会话桩：4 项危险工具逐一硬拒"""
        s = self._chat_session()
        for tool in sorted(ac.PLAYWRIGHT_DENIED_TOOLS):
            p = _req_permission_params(f"Requesting approval to {tool}",
                                       title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}{tool}")
            self.assertFalse(self._approved(s._auto_approve(p)), tool)

    def test_l2_unknown_tool_rejected(self):
        """#268 清单外工具拒绝（fail-closed：playwright 工具集新增工具默认不放行）"""
        s = self._chat_session()
        p = _req_permission_params("Requesting approval to browser_future_tool",
                                   title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}browser_future_tool")
        self.assertFalse(self._approved(s._auto_approve(p)))

    def test_l2_url_check(self):
        """#268 L2 best-effort URL 校验：文本含非 http(s) URL 拒绝；http(s)/无 URL 放行"""
        s = self._chat_session()
        title = f"{ac.PLAYWRIGHT_TOOL_PREFIX}browser_navigate"
        self.assertFalse(self._approved(s._auto_approve(
            _req_permission_params("Requesting approval to navigate file:///etc/passwd", title=title))))
        self.assertTrue(self._approved(s._auto_approve(
            _req_permission_params("Requesting approval to navigate http://127.0.0.1:8899", title=title))))
        self.assertTrue(self._approved(s._auto_approve(
            _req_permission_params("Requesting approval to browser_navigate", title=title))))

    # ---- #379：dispatcher dev/accept 会话注入 playwright 前缀后的审批路由（与建卡会话同闸门）----

    ROLE_PREFIXES = ["mcp__reqboard-tools__", "mcp__playwright__"]

    def _role_session(self, role):
        s = _stub_session(role=role)
        s._allow_prefixes = list(self.ROLE_PREFIXES)
        return s

    def _assert_rejected_with_log_and_event(self, s, tool):
        """断言 playwright 工具被拒：rejected + log.warning + system 事件留痕（「已被审批拦截」）"""
        p = _req_permission_params(f"Requesting approval to {tool}",
                                   title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}{tool}")
        with self.assertLogs(ac.log, level="WARNING") as cm:
            outcome = s._auto_approve(p)
        self.assertFalse(self._approved(outcome), tool)
        self.assertTrue(any("审批拒绝 Playwright" in m for m in cm.output), tool)
        self.assertTrue(any(k == "system" and "已被审批拦截" in c for k, c in s.emitted), tool)

    def test_t1_dev_allowed_tools_approved(self):
        """#379 T1：dev 桩（注入前缀）放行清单 20 项逐一放行"""
        s = self._role_session("dev")
        for tool in sorted(ac.PLAYWRIGHT_ALLOWED_TOOLS):
            p = _req_permission_params(f"Requesting approval to {tool}",
                                       title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}{tool}")
            self.assertTrue(self._approved(s._auto_approve(p)), tool)

    def test_t2_accept_allowed_tools_approved(self):
        """#379 T2：accept 桩（注入前缀）放行清单 20 项逐一放行"""
        s = self._role_session("accept")
        for tool in sorted(ac.PLAYWRIGHT_ALLOWED_TOOLS):
            p = _req_permission_params(f"Requesting approval to {tool}",
                                       title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}{tool}")
            self.assertTrue(self._approved(s._auto_approve(p)), tool)

    def test_t3_dev_denied_tools_rejected(self):
        """#379 T3：dev 桩拒绝清单 4 项——rejected + log.warning + system 事件留痕"""
        s = self._role_session("dev")
        for tool in sorted(ac.PLAYWRIGHT_DENIED_TOOLS):
            self._assert_rejected_with_log_and_event(s, tool)

    def test_t4_accept_denied_tools_rejected(self):
        """#379 T4：accept 桩拒绝清单 4 项——rejected + log.warning + system 事件留痕"""
        s = self._role_session("accept")
        for tool in sorted(ac.PLAYWRIGHT_DENIED_TOOLS):
            self._assert_rejected_with_log_and_event(s, tool)

    def test_t5_dev_unknown_tool_rejected(self):
        """#379 T5：dev 桩清单外未知工具（browser_future_tool）拒绝（fail-closed）"""
        s = self._role_session("dev")
        self._assert_rejected_with_log_and_event(s, "browser_future_tool")

    def test_t6_dev_accept_url_protocol_check(self):
        """#379 T6：URL 协议正反例——file:/// 拒；http(s)/无 URL 放行（dev/accept 各一份）"""
        title = f"{ac.PLAYWRIGHT_TOOL_PREFIX}browser_navigate"
        for role in ("dev", "accept"):
            s = self._role_session(role)
            self.assertFalse(self._approved(s._auto_approve(
                _req_permission_params("Requesting approval to navigate file:///etc/passwd", title=title))), role)
            self.assertTrue(self._approved(s._auto_approve(
                _req_permission_params("Requesting approval to navigate http://127.0.0.1:8899", title=title))), role)
            self.assertTrue(self._approved(s._auto_approve(
                _req_permission_params("Requesting approval to browser_navigate", title=title))), role)

    def test_t7_dev_without_prefix_still_rejected(self):
        """#379 T7：dev 桩不设 playwright 前缀（对照）——清单内工具仍拒（前缀是唯一放行通道）"""
        s = _stub_session(role="dev")  # 不设 _allow_prefixes
        p = _req_permission_params("Requesting approval to browser_navigate",
                                   title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}browser_navigate")
        with self.assertLogs(ac.log, level="WARNING"):
            self.assertFalse(self._approved(s._auto_approve(p)))
        self.assertTrue(any(k == "system" and "已被审批拦截" in c for k, c in s.emitted))

    def test_t8_accept_without_prefix_still_rejected(self):
        """#379 T8：accept 桩不设前缀（对照）——同上，未注入一律拒"""
        s = _stub_session(role="accept")
        p = _req_permission_params("Requesting approval to browser_navigate",
                                   title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}browser_navigate")
        self.assertFalse(self._approved(s._auto_approve(p)))

    def test_dispatcher_session_unchanged(self):
        """#268 dispatcher 只读桩（无 playwright 前缀）：playwright 工具仍拒绝，只读工具仍放行"""
        s = _stub_session(role="analysis")
        p = _req_permission_params("Requesting approval to browser_navigate",
                                   title=f"{ac.PLAYWRIGHT_TOOL_PREFIX}browser_navigate")
        self.assertFalse(self._approved(s._auto_approve(p)))
        p2 = _req_permission_params("Requesting approval to db_query",
                                    title=f"mcp__{ac.READONLY_TOOL_SERVER}__db_query")
        self.assertTrue(self._approved(s._auto_approve(p2)))

    def test_l3_url_cancel(self):
        """#268 L3 兜底（建卡会话）：rawInput.url 非 http(s) → system 事件 + cancel，不发 tool 摘要；
        http(s) → 不拦截、正常 emit tool"""
        s = self._l3_stub(self.CHAT_PREFIXES)
        s._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                      "rawInput": {"url": "file:///etc/passwd"}})
        self.assertEqual(s.cancelled, 1)
        self.assertEqual(len(s.emitted), 1)
        self.assertEqual(s.emitted[0][0], "system")
        self.assertIn("已被拦截", s.emitted[0][1])
        self.assertNotIn("tool", [k for k, _ in s.emitted])

        s2 = self._l3_stub(self.CHAT_PREFIXES)
        s2._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "t2",
                       "rawInput": {"url": "http://127.0.0.1:8899"}})
        self.assertEqual(s2.cancelled, 0)
        self.assertEqual([k for k, _ in s2.emitted], ["tool"])
        self.assertIn("http://127.0.0.1:8899", s2.emitted[0][1])

    def test_l3_not_enabled_for_dispatcher(self):
        """#268 L3 仅建卡会话启用：无 playwright 前缀的只读桩不 cancel、正常 emit tool（dispatcher 零变化）"""
        s = self._l3_stub([f"mcp__{ac.READONLY_TOOL_SERVER}__"])
        s._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "t3",
                      "rawInput": {"url": "file:///etc/passwd"}})
        self.assertEqual(s.cancelled, 0)
        self.assertEqual([k for k, _ in s.emitted], ["tool"])


class TapdChatAllowance(unittest.TestCase):
    """tapd MCP 只读白名单：建卡会话/需求对话放行 get_* 只读工具，
    写工具（create/update/comment/send 等）与未知工具一律拒绝（fail-closed）；
    未注入 tapd 前缀的会话（dispatcher 只读角色）tapd 工具仍拒绝，行为零变化。"""

    CHAT_PREFIXES = [f"mcp__{ac.READONLY_TOOL_SERVER}__", "mcp__reqboard-create__", "mcp__tapd__"]

    def _approved(self, outcome):
        return outcome.get("outcome", {}).get("outcome") == "selected"

    def _chat_session(self):
        s = _stub_session(role="analysis")
        s._allow_prefixes = list(self.CHAT_PREFIXES)
        return s

    def test_tapd_constants(self):
        """#xxx 常量：前缀精确、白名单非空且全部为 get_* 只读工具"""
        self.assertEqual(ac.TAPD_TOOL_PREFIX, "mcp__tapd__")
        self.assertGreaterEqual(len(ac.TAPD_READONLY_TOOLS), 20)
        for tool in ac.TAPD_READONLY_TOOLS:
            self.assertTrue(tool.startswith("get_"), tool)

    def test_tapd_readonly_tools_approved(self):
        """建卡会话桩：tapd get_* 只读清单逐一放行"""
        s = self._chat_session()
        for tool in sorted(ac.TAPD_READONLY_TOOLS):
            p = _req_permission_params(f"Requesting approval to {tool}",
                                       title=f"{ac.TAPD_TOOL_PREFIX}{tool}")
            self.assertTrue(self._approved(s._auto_approve(p)), tool)

    def test_tapd_write_and_unknown_tools_rejected(self):
        """建卡会话桩：tapd 写工具与未知工具逐一硬拒"""
        s = self._chat_session()
        for tool in ("create_story_or_task", "update_story_or_task", "create_bug", "update_bug",
                     "create_comments", "update_comments", "create_iteration", "update_iteration",
                     "create_wiki", "update_wiki", "add_timesheets", "update_timesheets",
                     "send_qiwei_message", "program_bind_entities", "create_tcases_batch",
                     "create_or_update_tcases", "entity_relations", "get_future_tool"):
            p = _req_permission_params(f"Requesting approval to {tool}",
                                       title=f"{ac.TAPD_TOOL_PREFIX}{tool}")
            self.assertFalse(self._approved(s._auto_approve(p)), tool)

    def test_tapd_without_prefix_rejected(self):
        """dispatcher 只读角色/旧会话未注入 tapd 前缀：tapd 只读工具仍拒绝"""
        s = _stub_session(role="analysis")
        p = _req_permission_params("Requesting approval to get_stories_or_tasks",
                                   title=f"{ac.TAPD_TOOL_PREFIX}get_stories_or_tasks")
        self.assertFalse(self._approved(s._auto_approve(p)))


class ChatBuiltinWriteGuard(unittest.TestCase):
    """建卡会话内置 Edit/Write 硬拦截（安全修复）：
    Kimi 对 git 工作目录内的 Edit/Write 自动放行、不发审批请求，因此建卡会话在
    tool_call 事件到达时立即取消本轮；dispatcher 只读角色不受影响（deny_builtin_write=False）。"""

    def _stub(self, deny):
        s = ac.ACPSession.__new__(ac.ACPSession)
        s.role = "analysis"
        s.req_id = 99999
        s.session_id = "stub-session"
        s.deny_builtin_write = deny
        s._tool_posted = set()
        s._tool_calls = {}
        s._msg_buf = []
        s._buf_lock = threading.Lock()
        s._capture = None
        s.emitted = []
        s.cancelled = 0
        s._emit = lambda kind, content, session_id=None: s.emitted.append((kind, content))
        s.cancel = lambda: setattr(s, "cancelled", s.cancelled + 1)
        return s

    def test_tool_call_edit_cancels(self):
        s = self._stub(True)
        s._on_update({"sessionUpdate": "tool_call", "title": "Edit", "toolCallId": "t1"})
        self.assertEqual(s.cancelled, 1)
        self.assertEqual(s.emitted[0][0], "system")
        self.assertIn("禁止文件写入", s.emitted[0][1])
        self.assertNotIn("t1", s._tool_calls)

    def test_tool_call_write_cancels(self):
        s = self._stub(True)
        s._on_update({"sessionUpdate": "tool_call", "title": "Write", "toolCallId": "t2"})
        self.assertEqual(s.cancelled, 1)
        self.assertEqual(s.emitted[0][0], "system")
        self.assertIn("禁止文件写入", s.emitted[0][1])

    def test_raw_input_fallback_cancels(self):
        s = self._stub(True)
        s._tool_calls["t3"] = "Edit"
        s._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "t3",
                      "rawInput": {"path": "/tmp/x.txt"}})
        self.assertEqual(s.cancelled, 1)
        self.assertNotIn("t3", s._tool_posted)

    def test_dispatcher_readonly_not_cancelled(self):
        s = self._stub(False)
        s._on_update({"sessionUpdate": "tool_call", "title": "Edit", "toolCallId": "t4"})
        self.assertEqual(s.cancelled, 0)
        self.assertEqual(s._tool_calls.get("t4"), "Edit")

    def test_chat_boot_wiring(self):
        src = backend_text()
        self.assertIn("deny_builtin_write=True", src)


class WriteRootsGuard(unittest.TestCase):
    """#303 防复发：dispatcher 角色的内置 Edit/Write 与 fs 写绝对路径白名单（禁写主工作区）。
    write_roots=None（建卡会话等）行为不变；相对路径基于会话 cwd（worktree）放行；
    绝对路径 resolve 后必须落在白名单根内（软链指向主工作区同样被拦）。"""

    def _stub(self, roots=None):
        s = ac.ACPSession.__new__(ac.ACPSession)
        s.role = "dev"
        s.req_id = 99999
        s.session_id = "stub-session"
        s.allow_fs_write = True
        s.deny_builtin_write = False
        s.write_roots = [Path(r).resolve() for r in roots] if roots else None
        s._tool_posted = set()
        s._tool_calls = {}
        s._msg_buf = []
        s._buf_lock = threading.Lock()
        s._capture = None
        s.emitted = []
        s.cancelled = 0
        s.sent = []
        s._emit = lambda kind, content, session_id=None: s.emitted.append((kind, content))
        s.cancel = lambda: setattr(s, "cancelled", s.cancelled + 1)
        s._send = lambda msg: s.sent.append(msg)
        return s

    def test_path_judgement(self):
        wt = Path(tempfile.mkdtemp(prefix="wr-guard-"))
        self.addCleanup(shutil.rmtree, wt, ignore_errors=True)  # 防 $TMPDIR 泄漏
        s = self._stub([wt, Path(tempfile.gettempdir()), Path("/tmp")])
        self.assertTrue(s._in_write_roots(wt / "a.py"))          # worktree 内
        self.assertTrue(s._in_write_roots("relative.py"))        # 相对路径放行（基于 worktree cwd）
        self.assertTrue(s._in_write_roots("/tmp/x.log"))         # /tmp 软链 resolve 后仍在白名单
        self.assertFalse(s._in_write_roots("/Users/dev/project/req-board/board.py"))  # 主工作区

    def test_symlink_to_repo_blocked(self):
        """软链指向真实仓库（如 ~/.agents/skills/req-board-card → repo/skills/req-board-card）必须被拦"""
        tmp = Path(tempfile.mkdtemp(prefix="wr-sym-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)  # 防 $TMPDIR 泄漏
        repo = tmp / "repo"
        repo.mkdir()
        (repo / "f.py").write_text("x")
        link = tmp / "skill"
        link.symlink_to(repo)
        s = self._stub([tmp / "wt"])
        self.assertFalse(s._in_write_roots(link / "f.py"))

    def test_tool_call_update_outside_cancels(self):
        _ok = Path(tempfile.mkdtemp(prefix="wr-ok-"))
        self.addCleanup(shutil.rmtree, _ok, ignore_errors=True)  # 防 $TMPDIR 泄漏
        s = self._stub([_ok])
        s._tool_calls["t1"] = "Edit"
        s._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                      "rawInput": {"file_path": "/Users/dev/project/req-board/board.py"}})
        self.assertEqual(s.cancelled, 1)
        self.assertEqual(s.emitted[0][0], "system")
        self.assertIn("白名单", s.emitted[0][1])
        self.assertNotIn("t1", s._tool_posted)

    def test_multiedit_first_path_outside_cancels(self):
        _ok = Path(tempfile.mkdtemp(prefix="wr-ok-"))
        self.addCleanup(shutil.rmtree, _ok, ignore_errors=True)  # 防 $TMPDIR 泄漏
        s = self._stub([_ok])
        s._tool_calls["t1b"] = "MultiEdit"
        s._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "t1b",
                      "rawInput": {"edits": [{"file_path": "/Users/dev/project/req-board/board.py"}]}})
        self.assertEqual(s.cancelled, 1)

    def test_tool_call_update_inside_passes(self):
        ok = Path(tempfile.mkdtemp(prefix="wr-ok2-"))
        self.addCleanup(shutil.rmtree, ok, ignore_errors=True)  # 防 $TMPDIR 泄漏
        s = self._stub([ok])
        s._tool_calls["t2"] = "Write"
        s._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "t2",
                      "rawInput": {"path": str(ok / "a.txt")}})
        self.assertEqual(s.cancelled, 0)
        self.assertIn("t2", s._tool_posted)

    def test_no_roots_unchanged(self):
        """未配置白名单（建卡会话/旧调用方）→ 行为不变"""
        s = self._stub(None)
        s._tool_calls["t3"] = "Edit"
        s._on_update({"sessionUpdate": "tool_call_update", "toolCallId": "t3",
                      "rawInput": {"file_path": "/Users/dev/project/req-board/board.py"}})
        self.assertEqual(s.cancelled, 0)
        self.assertIn("t3", s._tool_posted)

    def test_fs_write_outside_rejected_before_landing(self):
        """fs 写为本进程代办，可在落地前拒绝（返回错误给 agent，不取消整轮）"""
        _ok = Path(tempfile.mkdtemp(prefix="wr-ok3-"))
        self.addCleanup(shutil.rmtree, _ok, ignore_errors=True)  # 防 $TMPDIR 泄漏
        s = self._stub([_ok])
        s._handle_request({"id": 7, "method": "fs/write_text_file",
                           "params": {"path": "/Users/dev/project/req-board/x.py", "content": "x"}})
        self.assertEqual(len(s.sent), 1)
        self.assertIn("error", s.sent[0])
        self.assertIn("白名单", s.sent[0]["error"]["message"])
        self.assertEqual(s.cancelled, 0)

    def test_fs_write_inside_passes(self):
        ok = Path(tempfile.mkdtemp(prefix="wr-ok4-"))
        self.addCleanup(shutil.rmtree, ok, ignore_errors=True)  # 防 $TMPDIR 泄漏
        s = self._stub([ok])
        s._handle_request({"id": 8, "method": "fs/write_text_file",
                           "params": {"path": str(ok / "a.txt"), "content": "hi"}})
        self.assertEqual(s.sent[0].get("result"), {})
        self.assertTrue((ok / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
