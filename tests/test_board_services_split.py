"""#32 Phase 3+4+5：services 拆分契约（晚绑定 / 依赖方向 / 行数 / 启动序列）。"""
import ast
import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import board as b  # noqa: E402

SVC = REPO / "board_modules" / "services"
MODS = ("presentation", "model_pool", "harness_config", "projects", "requirements", "session_runtime",
        "dispatcher_ops", "chat_runtime", "mcp_servers", "reqchat_runtime",
        "assistant_card", "assistant_runtime", "assistant_mcp", "project_git", "context_preview")
PATCHED = set(json.loads((REPO / "tests/fixtures/board_patch_names.json").read_text(encoding="utf-8"))["names"])


def _tree(m):
    return ast.parse((SVC / f"{m}.py").read_text(encoding="utf-8"))


class StaticContract(unittest.TestCase):
    def test_s1_modules_exist_and_within_budget(self):
        for m in MODS:
            n = len((SVC / f"{m}.py").read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(n, 1500, m)

    def test_s2_no_direct_use_of_patched_names(self):
        """补丁名单名字在 services 内只能经 resolve("名") 读取：不得裸读、不得 import、不得模块级绑定。"""
        for m in MODS:
            for x in ast.walk(_tree(m)):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
                    self.assertNotIn(x.id, PATCHED, f"{m}:{x.lineno} 裸读 {x.id}")
                if isinstance(x, ast.ImportFrom):
                    for a in x.names:
                        self.assertNotIn(a.asname or a.name, PATCHED, f"{m}:{x.lineno}")
            for n in _tree(m).body:
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        self.assertNotIn(getattr(t, "id", None), PATCHED, f"{m} 模块级绑定")

    def test_s3_dependency_direction(self):
        """services 不 import board / blueprints；services 之间只允许下列边（均指向更早实施的模块，故无环）。"""
        allowed = {"harness_config": {"model_pool"}, "presentation": {"model_pool"},
                   "mcp_servers": {"requirements", "dispatcher_ops", "chat_runtime"},
                   "chat_runtime": {"chat_prompts_en"},
                   "reqchat_runtime": {"chat_runtime", "mcp_servers", "chat_prompts_en"},
                   "assistant_runtime": {"assistant_card", "chat_runtime"},
                   "assistant_mcp": {"assistant_card", "assistant_runtime", "requirements"}}
        for m in MODS:
            for x in ast.walk(_tree(m)):
                names = [x.module or ""] if isinstance(x, ast.ImportFrom) else \
                        [a.name for a in x.names] if isinstance(x, ast.Import) else []
                for s in names:
                    self.assertNotEqual(s, "board", m)
                    self.assertFalse(s.startswith("board_modules.blueprints"), m)
                    if s.startswith("board_modules.services."):
                        self.assertIn(s.rsplit(".", 1)[1], allowed.get(m, set()), f"{m} → {s}")

    def test_s4_each_module_imports_alone(self):
        """全新解释器单独 import 每个模块：无循环 import，且不触发 import board。"""
        for m in MODS:
            code = f"import sys; import board_modules.services.{m}; assert 'board' not in sys.modules"
            r = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO)!r}); {code}"],
                               capture_output=True, text=True, cwd=tempfile.gettempdir())
            self.assertEqual(r.returncode, 0, f"{m}: {r.stderr[-500:]}")

    def test_s5_board_reexports_service_names(self):
        """board 顶层仍能取到每个搬走的名字，且与 services 内是同一对象（兼容导出无第二份状态）。"""
        import importlib
        for m in MODS:
            mod = importlib.import_module(f"board_modules.services.{m}")
            for n in _tree(m).body:
                for k in ([n.name] if hasattr(n, "name") else [getattr(t, "id", None) for t in getattr(n, "targets", [])]):
                    if k:
                        self.assertIs(getattr(b, k), getattr(mod, k), f"board.{k}")


    def test_s6_startup_sequence_unchanged(self):
        """board.py 的 __main__ 块仍按 init_db → _recover_chat_sessions → _recover_req_chats →
        _start_chat_recovery_worker 顺序直接调用（四者都经兼容导出可在 board 上取到）。"""
        tree = ast.parse((REPO / "board.py").read_text(encoding="utf-8"))
        main = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]
        self.assertEqual(len(main), 1)
        calls = [n.value.func.id for n in main[0].body
                 if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)]
        self.assertEqual(calls[:4], ["init_db", "_recover_chat_sessions", "_recover_req_chats",
                                     "_start_chat_recovery_worker"])


class LateBinding(unittest.TestCase):
    """改写 board.<名> 后，搬到 services 的调用点观察到改写值。"""

    def test_l1_session_homes_seen_by_model_pool(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.toml").write_text("", encoding="utf-8")
            with mock.patch.object(b, "session_homes", lambda: [Path(d)]):
                self.assertEqual(b._harness_config_path()[1], d)

    def test_l2_projects_file_seen_by_projects(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "projects.json"
            p.write_text('{"zz-probe": "/tmp"}', encoding="utf-8")
            with mock.patch.object(b, "PROJECTS_FILE", p):
                self.assertEqual(b._read_projects_raw(), ({"zz-probe": "/tmp"}, None))

    def test_l3_get_db_seen_by_requirements(self):
        calls = []

        @contextlib.contextmanager
        def fake():
            calls.append(1)
            raise RuntimeError("probe")
            yield  # noqa
        with mock.patch.object(b, "get_db", fake):
            with self.assertRaises(RuntimeError):
                b.req_list()
        self.assertEqual(calls, [1])

    def test_l4_model_alias_pool_entries_seen_by_pool(self):
        with mock.patch.object(b, "_model_alias_pool_entries", lambda: ([{"alias": "probe"}], "/h", None)):
            self.assertEqual(b._model_alias_pool(), (["probe"], "/h", None))

    def test_l5_role_capabilities_seen_by_session_runtime(self):
        with mock.patch.object(b, "ROLE_CAPABILITIES", {"zz_probe_role": {}}), \
             mock.patch("board_modules.services.session_runtime.role_meta",
                        lambda r, k: "probe.md" if r == "zz_probe_role" else None):
            self.assertEqual(b._artifact_files().get("zz_probe_role"), "probe.md")

    def test_l6_get_db_seen_by_chat_recovery(self):
        calls = []

        @contextlib.contextmanager
        def fake():
            calls.append(1)
            raise RuntimeError("probe")
            yield  # noqa
        for fn in (b._recover_chat_sessions, b._recover_req_chats):
            with mock.patch.object(b, "get_db", fake), self.assertRaises(RuntimeError):
                fn()
        self.assertEqual(calls, [1, 1])

    def test_l7_heartbeat_file_seen_by_dispatcher_ops(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hb.json"
            p.write_text('{"last_poll_at": "probe"}', encoding="utf-8")
            with mock.patch.object(b, "HEARTBEAT_FILE", p):
                self.assertEqual(b._reqchat_heartbeat_ts(), "probe")

    def test_l8_kimi_code_mcp_json_seen_by_mcp_servers(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mcp.json"
            p.write_text('{"mcpServers": {"tapd": {"env": {"TAPD_ACCESS_TOKEN": "probe"}}}}', encoding="utf-8")
            with mock.patch.object(b, "KIMI_CODE_MCP_JSON", p):
                self.assertEqual(b._tapd_config()["token"], "probe")

    def test_l9_project_paths_seen_by_chat_runtime(self):
        with mock.patch.object(b, "PROJECT_PATHS", {"zz-probe": ["/probe"]}):
            self.assertEqual(b._chat_workdir("zz-probe"), "/probe")

    def test_l10_project_paths_seen_by_assistant_mcp(self):
        """#32 Phase 5：assistant_mcp 的 _assistant_roots 现读 board.PROJECT_PATHS，不缓存。"""
        with mock.patch.object(b, "PROJECT_PATHS", {"zz-probe": ["/probe"]}):
            self.assertEqual(b.AssistantMcpServer._assistant_roots(), [Path("/probe")])

    def test_l11_assistant_active_row_seen_by_assistant_runtime(self):
        """#32 Phase 5：assistant_runtime 的会话复用经 resolve("_assistant_active_row")，改写即生效。"""
        sentinel = {"id": 424242, "feishu_chat_id": "oc_probe", "status": "running"}
        with mock.patch.object(b, "_assistant_active_row", lambda cid: sentinel):
            self.assertEqual(b._assistant_get_or_create("oc_probe")[0], sentinel)

    def test_l13_project_git_seen_by_assistant_mcp(self):
        """#54：assistant_mcp 经 resolve("ProjectGit") 取服务类，改写 board.ProjectGit 即生效。"""
        sentinel = object()
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "feishu.json"
            cfg.write_text('{"assistant_git_ops": true}', encoding="utf-8")
            with mock.patch.object(b, "FEISHU_JSON_PATH", cfg), \
                    mock.patch.object(b, "ProjectGit", lambda **kw: sentinel):
                server = b.AssistantMcpServer(1, "oc_probe", turn_id=1)
                try:
                    self.assertIs(server._project_git(), sentinel)
                finally:
                    server.close()

    def test_l12_get_db_seen_by_assistant_runtime(self):
        """#32 Phase 5：assistant_runtime 的库连接经 resolve("get_db")（含其调用的 _assistant_active_row）。"""
        calls = []

        @contextlib.contextmanager
        def fake():
            calls.append(1)
            raise RuntimeError("probe")
            yield  # noqa
        with mock.patch.object(b, "get_db", fake):
            with self.assertRaises(RuntimeError):
                b._assistant_get_or_create("oc_probe")
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
