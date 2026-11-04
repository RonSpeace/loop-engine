"""#32 Phase 6（卡 #39）：board.py 路由拆成蓝图的契约测试（结构 / 依赖方向 / 晚绑定 / 路由零变化）。

与 `test_board_services_split.py`（Phase 3–5）同构：静态契约 + 晚绑定探针；名字口径一律取
`tests/fixtures/board_patch_names.json`（测试在 board 上改写过的名字，拆分后必须仍晚绑定）。
"""
import ast
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

BP_DIR = REPO / "board_modules" / "blueprints"
REPO_MODS = ("requirements", "projects", "chats", "settings", "assistant", "sessions", "meta")
# 设计 §3 的路线分组（口径以 board_routes_baseline.json 裁决，见 test_board_split_contract.py）
ROUTE_COUNTS = {"requirements": 21, "projects": 16, "chats": 22, "settings": 7,
                "assistant": 7, "sessions": 10, "meta": 9}
PATCHED = set(json.loads((REPO / "tests/fixtures/board_patch_names.json").read_text(encoding="utf-8"))["names"])


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _top_names(tree: ast.Module) -> list:
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(n.name)
        elif isinstance(n, ast.Assign):
            out += [t.id for t in n.targets if isinstance(t, ast.Name)]
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.append(n.target.id)
    return out


def _route_names(tree: ast.Module) -> list:
    """顶层 `@bp.route(...)` 装饰的函数名。"""
    out = []
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "route":
                out.append(n.name)
    return out


class StaticContract(unittest.TestCase):
    def test_b1_line_budget(self):
        """board.py ≤530 行（#40 人工裁决：允许 py 文件超出行数，判定函数按方案留在入口文件）；
        board_modules/** 每个 Python 文件 ≤1500 行。"""
        n = len((REPO / "board.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 530, f"board.py {n} 行")
        for p in sorted((REPO / "board_modules").rglob("*.py")):
            m = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(m, 1500, f"{p.relative_to(REPO)} {m} 行")

    def test_b2_blueprint_package_shape(self):
        """7 个蓝图模块 + __init__.ALL（注册顺序）齐备；每个模块自建同名 Blueprint 对象。"""
        for m in REPO_MODS:
            p = BP_DIR / f"{m}.py"
            self.assertTrue(p.exists(), f"缺 {p.name}")
            src = p.read_text(encoding="utf-8")
            self.assertIn(f'Blueprint("{m}", __name__)', src, m)
        from board_modules import blueprints
        self.assertEqual(tuple(x.bp.name for x in blueprints.ALL), REPO_MODS)

    def test_b3_board_keeps_entry_only(self):
        """board.py 只剩入口：无 @app.route、有蓝图注册与 compat facade 挂载、__main__ 唯一。"""
        src = (REPO / "board.py").read_text(encoding="utf-8")
        self.assertNotIn("@app.route", src)
        self.assertIn("from board_modules.facade import *", src)
        self.assertIn("register_blueprint", src)
        tree = _tree(REPO / "board.py")
        mains = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]
        self.assertEqual(len(mains), 1)

    def test_b4_dependency_direction(self):
        """蓝图不 import board / 不 import 兄弟蓝图；settings/requirements/meta 三者互不 import。"""
        for m in REPO_MODS:
            mods = set()
            for x in ast.walk(_tree(BP_DIR / f"{m}.py")):
                if isinstance(x, ast.ImportFrom) and x.module:
                    mods.add(x.module)
                elif isinstance(x, ast.Import):
                    mods |= {a.name for a in x.names}
            for s in mods:
                self.assertNotEqual(s, "board", f"{m} import board")
                self.assertFalse(s.startswith("board_modules.blueprints"), f"{m} → {s}")
        trio = {"settings", "requirements", "meta"}
        for m in trio:
            src = (BP_DIR / f"{m}.py").read_text(encoding="utf-8")
            for other in trio - {m}:
                self.assertNotIn(f"blueprints.{other}", src, f"{m} → {other}")

    def test_b5_route_functions_live_in_exactly_one_blueprint(self):
        """86 条路由按设计分组各归一个蓝图，且分组计数与基线一致。"""
        seen: dict = {}
        for m in REPO_MODS:
            names = _route_names(_tree(BP_DIR / f"{m}.py"))
            self.assertEqual(len(names), ROUTE_COUNTS[m], f"{m} 路由数")
            for n in names:
                self.assertNotIn(n, seen, f"{n} 出现在 {seen.get(n)} 与 {m}")
                seen[n] = m
        self.assertEqual(len(seen), sum(ROUTE_COUNTS.values()))

    def test_b12_no_python312_only_fstring_nesting(self):
        """源码不得含仅 PEP 701（Python 3.12+）可解析的同引号嵌套 f-string——仓库声明支持 3.11+。

        搬迁把 f-string 里的 `{NAME}` 改写成 `resolve("NAME")`：外层 f-string 若用双引号、内层
        沿用双引号即成为 3.12 专属语法，3.11 下 `python board.py` 直接 SyntaxError（启动方式被破坏）。
        用 3.12 的 f-string 分词令牌（FSTRING_START/END）检出「嵌套字符串与外层 f-string 引号相同」
        的形态；<3.12 环境无该令牌（且那时源码本会在导入期报错），检查自动跳过。
        """
        import io
        import tokenize

        def quote_of(literal: str):
            i = 0
            while i < len(literal) and literal[i] not in "\"'":
                i += 1
            rest = literal[i:]
            for q in ('"""', "'''", '"', "'"):
                if rest.startswith(q):
                    return q
            return None

        fstring_start = getattr(tokenize, "FSTRING_START", None)
        fstring_end = getattr(tokenize, "FSTRING_END", None)
        if fstring_start is None:
            self.skipTest("当前 Python 无 f-string 分词令牌（<3.12）")
        targets = [REPO / "board.py"] + sorted((REPO / "board_modules").rglob("*.py"))
        for p in targets:
            stack: list = []
            for tok in tokenize.generate_tokens(io.StringIO(p.read_text(encoding="utf-8")).readline):
                if tok.type == fstring_start:
                    stack.append(quote_of(tok.string))
                elif tok.type == fstring_end:
                    if stack:
                        stack.pop()
                elif tok.type == tokenize.STRING and stack:
                    q = quote_of(tok.string)
                    if q and q == stack[-1]:
                        self.fail(f"{p.relative_to(REPO)}:{tok.start[0]} 同引号嵌套 f-string"
                                  f"（仅 3.12+ 可解析）: {tok.string[:40]}")


class ComplianceTest(unittest.TestCase):
    def test_b6_patched_names_only_via_resolve(self):
        """补丁名单名字在蓝图里只能经 resolve("名") 读取：不得裸读、import、模块级绑定。"""
        for m in REPO_MODS:
            tree = _tree(BP_DIR / f"{m}.py")
            for x in ast.walk(tree):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
                    self.assertNotIn(x.id, PATCHED, f"{m}:{x.lineno} 裸读 {x.id}")
                if isinstance(x, ast.ImportFrom):
                    for a in x.names:
                        self.assertNotIn(a.asname or a.name, PATCHED, f"{m}:{x.lineno}")
            for n in tree.body:
                if isinstance(n, (ast.Assign, ast.AnnAssign)):
                    tgt = n.target if isinstance(n, ast.AnnAssign) else None
                    names = [tgt.id] if isinstance(tgt, ast.Name) else \
                            [t.id for t in getattr(n, "targets", []) if isinstance(t, ast.Name)]
                    for k in names:
                        self.assertNotIn(k, PATCHED, f"{m} 模块级绑定 {k}")

    def test_b7_each_blueprint_imports_alone(self):
        """全新解释器单独 import 每个蓝图：无循环 import，且不触发 import board。"""
        for m in REPO_MODS:
            code = (f"import sys; import board_modules.blueprints.{m} as x; "
                    f"assert x.bp.name == '{m}'; assert 'board' not in sys.modules")
            r = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO)!r}); {code}"],
                               capture_output=True, text=True, cwd=tempfile.gettempdir())
            self.assertEqual(r.returncode, 0, f"{m}: {r.stderr[-500:]}")


class CompatTest(unittest.TestCase):
    def test_b8_board_reexports_blueprint_names(self):
        """board 顶层仍能取到搬走的路由函数与成员，且与蓝图内是同一对象（无第二份状态）。"""
        import importlib
        for m in REPO_MODS:
            mod = importlib.import_module(f"board_modules.blueprints.{m}")
            for k in _top_names(_tree(BP_DIR / f"{m}.py")):
                if k == "bp":              # 蓝图对象本身只属于蓝图模块，不导出到 board
                    continue
                self.assertIs(getattr(b, k), getattr(mod, k), f"board.{k}")

    def test_b9_endpoints_carry_blueprint_prefix(self):
        """url_map 里除 static 外每条规则的 endpoint 前缀都是蓝图名，且视图函数来自该蓝图模块。"""
        prefixes = set()
        for rule in b.app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            prefix, _, _ = rule.endpoint.partition(".")
            prefixes.add(prefix)
            self.assertIn(prefix, REPO_MODS, rule.endpoint)
            fn = b.app.view_functions[rule.endpoint]
            self.assertEqual(fn.__module__, f"board_modules.blueprints.{prefix}", rule.endpoint)
        self.assertEqual(prefixes, set(REPO_MODS))

    def test_b10_pages_still_served(self):
        """`/` 与 `/guide` 仍可服务（拆分后入口路由完好）。"""
        c = b.app.test_client()
        for url in ("/", "/guide"):
            self.assertEqual(c.get(url).status_code, 200, url)

    def test_b11_docs_api_endpoints_all_exist(self):
        """docs/api.md 表格所列每个 (方法, 路径) 在 url_map 里都命中（逐条回归；`<名>` 视为单段参数）。"""
        import re
        txt = (REPO / "docs" / "api.md").read_text(encoding="utf-8")
        rows = [(m.group(1), m.group(2)) for ln in txt.splitlines()
                if (m := re.match(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`", ln))]
        self.assertGreater(len(rows), 60)
        rules = [(r.rule, r.methods) for r in b.app.url_map.iter_rules() if r.endpoint != "static"]

        def as_regex(doc: str) -> "re.Pattern":
            out, i = "", 0
            for m in re.finditer(r"<[^>]+>", doc):
                out += re.escape(doc[i:m.start()]) + r"[^/]+"
                i = m.end()
            return re.compile("^" + out + re.escape(doc[i:]) + "$")
        missing = []
        for meth, doc in rows:
            p = as_regex(doc.split("?")[0])
            if not any(p.match(rule) and meth in methods for rule, methods in rules):
                missing.append(f"{meth} {doc}")
        self.assertEqual(missing, [], f"docs/api.md 列了 url_map 里不存在的端点: {missing[:6]}")


class LateBinding(unittest.TestCase):
    """改写 board.<名> 后，搬到蓝图的调用点观察到改写值（mock.patch.object 已经过 board）。"""

    def test_l13_data_dir_seen_by_sessions(self):
        """sessions 蓝图的 /api/sessions 经 resolve("DATA_DIR") 读盘：改写即生效。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dispatcher_state.json").write_text(
                json.dumps({"sessions": {"7": "sess-probe", "8": {"id": "x", "model": "m"}}}), encoding="utf-8")
            with mock.patch.object(b, "DATA_DIR", Path(d)), b.app.test_request_context("/api/sessions"):
                self.assertEqual(b.api_sessions().get_json(),
                                 {"7": {"id": "sess-probe"}, "8": {"id": "x", "model": "m"}})

    def test_l14_heartbeat_file_seen_by_meta(self):
        """meta 蓝图的 /api/dispatcher-health 经 resolve("HEARTBEAT_FILE") 读心跳。"""
        import time
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hb.json"
            p.write_text(json.dumps({"last_poll_at": time.time(), "auto_restart_ok": "probe"}),
                         encoding="utf-8")
            with mock.patch.object(b, "HEARTBEAT_FILE", p), b.app.test_request_context("/api/dispatcher-health"):
                resp = b.api_dispatcher_health().get_json()
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["auto_restart_ok"], "probe")

    def test_l15_board_started_at_seen_by_meta(self):
        """meta 蓝图比对心跳里的重启标记时经 resolve("BOARD_STARTED_AT")：改成未来时刻即判失效。"""
        import time
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hb.json"
            p.write_text(json.dumps({"last_poll_at": time.time(),
                                     "board_restart_pending": {"at": time.time()}}), encoding="utf-8")
            with mock.patch.object(b, "HEARTBEAT_FILE", p), b.app.test_request_context("/api/dispatcher-health"):
                self.assertIsNotNone(b.api_dispatcher_health().get_json()["board_restart_pending"])
                with mock.patch.object(b, "BOARD_STARTED_AT", time.time() + 3600):
                    self.assertIsNone(b.api_dispatcher_health().get_json()["board_restart_pending"])

    def test_l16_auto_projects_config_seen_by_projects(self):
        """projects 蓝图经 resolve("_auto_projects_config") 取项目配置；get_db 失败即零值降级。"""
        cfg = {"zz-probe": {"goal": "g", "paths": ["/p"]}}
        with mock.patch.object(b, "_auto_projects_config", lambda: cfg), \
                mock.patch.object(b, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("probe"))), \
                b.app.test_request_context("/api/project-stats?project=zz-probe"):
            resp = b.api_project_stats().get_json()
        self.assertEqual((resp["goal"], resp["paths"]), ("g", ["/p"]))

    def test_l17_feishu_json_path_seen_by_settings(self):
        """settings 蓝图经 resolve("FEISHU_JSON_PATH") 回显凭据文件路径：改写即生效。"""
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "feishu.json"
            with mock.patch.object(b, "FEISHU_JSON_PATH", fake), \
                    b.app.test_request_context("/api/feishu-settings"):
                self.assertEqual(b.api_feishu_settings_get().get_json()["path"], str(fake))

    def test_l18_get_db_seen_by_requirements(self):
        """requirements 蓝图里 board 上的 get_db 是调用时读取：改写后 req_list 直接用改写值。"""
        calls = []

        import contextlib

        @contextlib.contextmanager
        def fake():
            calls.append(1)
            raise RuntimeError("probe")
            yield  # noqa
        from board_modules.blueprints import requirements as req_bp
        with mock.patch.object(b, "get_db", fake):
            with self.assertRaises(RuntimeError):
                req_bp.req_list()
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
