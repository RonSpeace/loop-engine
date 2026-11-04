#!/usr/bin/env python3
"""建卡聊天 agent 按项目只读查询 MySQL（mysql_query）：
未配置项目不暴露工具、SQL 只读白名单、连接层只读会话、凭据不出现在输出/报错。

运行：
    .venv/bin/python -m unittest tests.test_mysql_tool -v
"""
import json
import shutil
import sys
import tempfile
import unittest
import urllib.request

# 本机 macOS 系统代理会拦截 urllib 对 127.0.0.1 的请求（getproxies_macosx_sysconf），
# 本地 HTTP 冒烟一律走显式空代理 opener（#419 自验环境暴露，与业务逻辑无关）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402
import acp_shared as a  # noqa: E402


class FakeCur:
    def __init__(self):
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)

    @property
    def description(self):
        return [("id",), ("name",)]

    def __iter__(self):
        return iter([(1, "a"), (2, "b")])


class FakeConn:
    def __init__(self):
        self.cur = FakeCur()
        self.rolled = False
        self.closed = False

    def cursor(self):
        return self.cur

    def rollback(self):
        self.rolled = True

    def close(self):
        self.closed = True


class FakeMysql:
    def __init__(self, conn=None, error=None):
        self.conn = conn
        self.error = error
        self.kw = None

    def connect(self, **kw):
        self.kw = kw
        if self.error:
            raise self.error
        return self.conn


class MysqlToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-mysql-"))
        cls.env = cls.tmp / ".env"
        cls.env.write_text(
            "DB_HOST=localhost\nDB_PORT=3306\nDB_NAME=testdb\n"
            "DB_USER=appuser\nDB_PASSWORD=supersecret\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _server(self, mysql_config=None):
        return a.ReadonlyMcpServer(
            self.tmp, [self.tmp], mysql_config=mysql_config)

    def _list_tools(self, server):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        req = urllib.request.Request(
            server.url, data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        with _NO_PROXY_OPENER.open(req, timeout=5) as r:
            return json.loads(r.read().decode())["result"]["tools"]

    def test_unconfigured_project_has_no_mysql_tool(self):
        server = self._server()
        try:
            names = [t["name"] for t in self._list_tools(server)]
            self.assertNotIn("mysql_query", names)
            with self.assertRaises(ValueError) as ctx:
                server.tool_mysql_query({"sql": "SELECT 1"})
            self.assertIn("未配置", str(ctx.exception))
        finally:
            server.close()

    def test_configured_project_exposes_mysql_tool(self):
        server = self._server({"env_file": str(self.env)})
        try:
            names = [t["name"] for t in self._list_tools(server)]
            self.assertIn("mysql_query", names)
        finally:
            server.close()

    def test_mysql_config_plain_string_supported(self):
        # 兼容直接传 .env 路径字符串（_chat_boot 早期版本传字符串导致工具未暴露的回归防护）
        server = self._server(str(self.env))
        try:
            names = [t["name"] for t in self._list_tools(server)]
            self.assertIn("mysql_query", names)
            self.assertEqual(server.mysql_env_file, str(self.env.resolve()))
        finally:
            server.close()

    def test_non_readonly_sql_rejected(self):
        server = self._server({"env_file": str(self.env)})
        try:
            with self.assertRaises(ValueError) as ctx:
                server.tool_mysql_query({"sql": "DELETE FROM t"})
            self.assertIn("非只读", str(ctx.exception))
        finally:
            server.close()

    def test_mysql_query_readonly_flow(self):
        conn = FakeConn()
        fake = FakeMysql(conn=conn)
        old = a._mysql_connector
        a._mysql_connector = lambda: fake
        server = self._server({"env_file": str(self.env)})
        try:
            out = server.tool_mysql_query({"sql": "SELECT id, name FROM t LIMIT 2"})
            self.assertIn("id | name", out)
            self.assertIn("1 | a", out)
            self.assertIn("(2 行)", out)
            self.assertEqual(fake.kw["database"], "testdb")
            self.assertEqual(fake.kw["user"], "appuser")
            self.assertEqual(fake.kw["password"], "supersecret")
            self.assertIs(fake.kw["autocommit"], False)
            self.assertEqual(conn.cur.executed[0], "SET SESSION TRANSACTION READ ONLY")
            self.assertTrue(conn.rolled)
            self.assertTrue(conn.closed)
        finally:
            a._mysql_connector = old
            server.close()

    def test_mysql_error_redacts_password(self):
        fake = FakeMysql(error=RuntimeError("access denied for supersecret"))
        old = a._mysql_connector
        a._mysql_connector = lambda: fake
        server = self._server({"env_file": str(self.env)})
        try:
            with self.assertRaises(ValueError) as ctx:
                server.tool_mysql_query({"sql": "SELECT 1"})
            msg = str(ctx.exception)
            self.assertNotIn("supersecret", msg)
            self.assertIn("***", msg)
        finally:
            a._mysql_connector = old
            server.close()

    def test_project_mysql_env_default_and_override(self):
        old_cfg = b.PROJECT_MYSQL
        old_paths = b.PROJECT_PATHS
        try:
            b.PROJECT_PATHS = {"demo": ["/tmp/demo"]}
            b.PROJECT_MYSQL = {"demo": {"env_file": None}}
            self.assertEqual(b._project_mysql_env("demo"), "/tmp/demo/.env")
            b.PROJECT_MYSQL = {"demo": {"env_file": "/tmp/custom.env"}}
            self.assertEqual(b._project_mysql_env("demo"), "/tmp/custom.env")
            b.PROJECT_MYSQL = {}
            self.assertIsNone(b._project_mysql_env("demo"))
        finally:
            b.PROJECT_MYSQL = old_cfg
            b.PROJECT_PATHS = old_paths

    def test_system_prompt_hint_conditional(self):
        old_cfg = b.PROJECT_MYSQL
        old_paths = b.PROJECT_PATHS
        try:
            b.PROJECT_PATHS = {"demo": ["/tmp/demo"]}
            b.PROJECT_MYSQL = {"demo": {"env_file": "/tmp/demo/.env"}}
            self.assertIn("mysql_query", b._chat_system_prompt("demo"))
            b.PROJECT_MYSQL = {}
            self.assertNotIn("mysql_query", b._chat_system_prompt("demo"))
        finally:
            b.PROJECT_MYSQL = old_cfg
            b.PROJECT_PATHS = old_paths


if __name__ == "__main__":
    unittest.main()
