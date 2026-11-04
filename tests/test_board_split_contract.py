#!/usr/bin/env python3
"""#34 board.py 拆分 1/6：拆分兼容合约（路由基线 / 兼容名单 / 晚绑定）。

- 路由基线：`app.url_map` 的 (rule, methods-{HEAD,OPTIONS}) 集合必须与
  `tests/fixtures/board_routes_baseline.json` 逐条相等——**不比较 endpoint**（蓝图注册后
  endpoint 会带 `<蓝图名>.` 前缀，含 endpoint 必挂）。
- 兼容名单：`scripts/board_patch_names.py` 的扫描结果必须与
  `tests/fixtures/board_patch_names.json` 一致；再用**与脚本不同实现**的独立提取器复核
  「测试实际改写 board 的名字」全部落在 fixture 内（防止脚本与 fixture 一起漏检），
  并逐个断言名字在 board 模块上存在。
- 晚绑定：`init_db` 内部经 `board.get_db` / `board.now_str` 取值（替换 board 上的名字同样生效）；
  `runtime.resolve` 在无 board/__main__ 命中时回退到调用方模块自身。
"""
import ast
import contextlib
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import board  # noqa: E402
import board_patch_names  # noqa: E402
from board_modules import runtime as rt  # noqa: E402

ROUTES_FIXTURE = REPO / "tests" / "fixtures" / "board_routes_baseline.json"
_ALIASES = {"b", "board"}
_SELF_ALIASES = {"self", "cls"}


def route_pairs(app):
    """(rule, methods - {HEAD, OPTIONS}) 集合：url_map 的拆分兼容基线口径。"""
    return {(rule.rule, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
            for rule in app.url_map.iter_rules()}


def _is_alias(node):
    if isinstance(node, ast.Name):
        return node.id in _ALIASES
    return (isinstance(node, ast.Attribute) and node.attr == "b"
            and isinstance(node.value, ast.Name) and node.value.id in _SELF_ALIASES)


def _str_literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _alias_dict(node):
    if isinstance(node, ast.Attribute) and node.attr == "__dict__" and _is_alias(node.value):
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "vars" and len(node.args) == 1 and _is_alias(node.args[0]))


def _target_names(node, out):
    if isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _target_names(elt, out)
    elif isinstance(node, ast.Attribute) and _is_alias(node.value):
        out.add(node.attr)
    elif isinstance(node, ast.Subscript) and _alias_dict(node.value):
        lit = _str_literal(node.slice)
        if lit and lit.isidentifier():
            out.add(lit)


def _independent_board_names():
    """独立实现（纯 AST）：测试实际改写 board 的名字。

    与 `scripts/board_patch_names.py`（正则 + AST）不同，本函数只走 AST，覆盖赋值/删除目标
    （含 `self.b.`、元组解包、`__dict__`/`vars` 动态改写）与 `patch.object`/`patch("board.…")`/
    `patch.multiple`/`patch.dict`/`setattr`/`delattr` 字面量形态。
    """
    names = set()
    for path in sorted((REPO / "tests").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for tgt in targets:
                    _target_names(tgt, names)
            elif isinstance(node, ast.Delete):
                for tgt in node.targets:
                    _target_names(tgt, names)
            elif isinstance(node, ast.Call):
                func, args = node.func, node.args
                fname = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else None)
                if fname in ("object", "setattr", "delattr") and len(args) >= 2 and _is_alias(args[0]):
                    lit = _str_literal(args[1])
                    if lit and lit.isidentifier():
                        names.add(lit)
                elif fname == "patch" and args:
                    lit = _str_literal(args[0])
                    if lit and lit.startswith("board."):
                        head = lit[len("board."):].split(".", 1)[0].split("[", 1)[0]
                        if head.isidentifier():
                            names.add(head)
                elif fname == "multiple" and args and _is_alias(args[0]):
                    names.update(kw.arg for kw in node.keywords if kw.arg and kw.arg.isidentifier())
                elif fname == "dict" and args and _alias_dict(args[0]):
                    if len(args) >= 2 and isinstance(args[1], ast.Dict):
                        for k in args[1].keys:
                            lit = _str_literal(k)
                            if lit and lit.isidentifier():
                                names.add(lit)
    return names


class RouteBaselineTest(unittest.TestCase):

    def test_url_map_matches_baseline(self):
        expected = {(rule, tuple(methods))
                    for rule, methods in json.loads(
                        ROUTES_FIXTURE.read_text(encoding="utf-8"))["routes"]}
        self.assertEqual(route_pairs(board.app), expected)

    def test_baseline_count_consistent(self):
        data = json.loads(ROUTES_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(data["routes"]), data["count"])
        self.assertEqual(len(route_pairs(board.app)), data["count"])


class PatchNamesTest(unittest.TestCase):

    def test_scan_matches_fixture(self):
        fixture = json.loads(board_patch_names.FIXTURE.read_text(encoding="utf-8"))
        names = board_patch_names.scan()
        self.assertEqual(names, fixture["names"])
        self.assertEqual(len(names), fixture["count"])

    def test_scanner_covers_future_patch_forms(self):
        """扫描器对未来改写形态的覆盖面（评审非阻断观察）：delattr/patch.multiple/patch.dict/
        __dict__ & vars 动态改写、del、self.b、元组解包均须命中。"""
        # 拼接构造，避免本用例源码里出现完整的正则形态（否则会被扫描器当成真实改写点）
        po, pt, sa = "patch" + ".object", "patch", "setattr"
        snippet = "\n".join([
            "import board as b",
            "from unittest import mock",
            "b.PLAIN = 1",
            "self.b.SELF_ATTR = 2",
            "b.A, b.B = 1, 2",
            "del b.DELETED",
            "b.__dict__['DYN'] = 3",
            "vars(b)['DYN2'] = 4",
            "mock.%s(b, 'PATCHED')" % po,
            "mock.%s('board.BYPATH.x')" % pt,
            "mock.patch.multiple(b, MULTI=1)",
            "mock.patch.dict(vars(b), {'DICTPATCH': 1})",
            "%s(b, 'SETATTR')" % sa,
            "delattr(b, 'DELATTR')",
        ])
        found = board_patch_names._scan_ast(snippet) | board_patch_names._scan_regex(snippet)
        for name in ("PLAIN", "SELF_ATTR", "A", "B", "DELETED", "DYN", "DYN2", "PATCHED",
                     "BYPATH", "MULTI", "DICTPATCH", "SETATTR", "DELATTR"):
            self.assertIn(name, found, f"扫描器漏检未来形态: {name}")

    def test_independent_scan_covered_by_fixture(self):
        """独立提取器复核：脚本与 fixture 一起漏检的名字必须在这里暴露。"""
        fixture = set(json.loads(board_patch_names.FIXTURE.read_text(encoding="utf-8"))["names"])
        missing = sorted(_independent_board_names() - fixture)
        self.assertEqual(missing, [], f"兼容名单漏检测试实际改写点: {missing}")

    def test_every_name_exists_on_board(self):
        for name in board_patch_names.scan():
            self.assertTrue(hasattr(board, name), f"board 模块缺 {name}（兼容桥漏名）")


class LateBindingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="req34-binding-")
        self._orig_db = board.DB_PATH
        board.DB_PATH = Path(self.tmp) / "t.db"
        board.init_db()

    def tearDown(self):
        board.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_db_routes_through_resolved_get_db(self):
        """替换 board.get_db 必须影响 init_db 内部连接（不得缓存本模块自己的 get_db）。"""
        calls = []
        real = board.get_db

        @contextlib.contextmanager
        def fake():
            calls.append(1)
            with real() as conn:
                yield conn

        with mock.patch.object(board, "get_db", fake):
            board.init_db()
        self.assertEqual(len(calls), 1, "init_db 未经 board.get_db（晚绑定失效）")

    def test_init_db_routes_through_resolved_now_str(self):
        """替换 board.now_str 必须影响 init_db 写入的 baseline 时间（不得缓存 common.now_str）。"""
        with board.get_db() as conn:
            conn.execute("INSERT INTO requirements (title, project, status) VALUES ('t','p','open')")
            conn.commit()
        seen = []

        def fake_now_str():
            seen.append(1)
            return "2020-01-01 00:00:00"

        with mock.patch.object(board, "now_str", fake_now_str):
            board.init_db()
        self.assertTrue(seen, "init_db 未经 board.now_str（晚绑定失效）")

    def test_resolve_falls_back_to_caller_module(self):
        """无 board/__main__ 命中时回退调用方模块自身（方案第 4 节查找契约）。"""
        fake = types.ModuleType("req34_caller_probe")
        fake.ONLY_IN_CALLER_34 = "sentinel-34"
        sys.modules["req34_caller_probe"] = fake
        try:
            code = ("from board_modules.runtime import resolve\n"
                    "probe = resolve('ONLY_IN_CALLER_34')\n")
            exec(compile(code, "<req34_caller_probe>", "exec"), fake.__dict__)
            self.assertEqual(fake.probe, "sentinel-34")
        finally:
            sys.modules.pop("req34_caller_probe", None)

    def test_resolve_prefers_board_module(self):
        """board 模块上有该名字时不走回退（正常路径不受影响）。"""
        self.assertIs(rt.resolve("get_db"), board.get_db)


if __name__ == "__main__":
    unittest.main()
