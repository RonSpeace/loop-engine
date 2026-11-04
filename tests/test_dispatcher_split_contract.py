"""dispatcher.py 拆分契约。

拆到 dispatcher_modules/ 的代码必须晚绑定读取测试会替换的名字（经 runtime.D），否则
对 dispatcher 打补丁 对拆出去的调用点失效、测试悄悄测不到真实路径。

- 名单：scripts/dispatcher_patch_names.py 的扫描结果与 tests/fixtures/dispatcher_patch_names.json 一致。
- 函数体：用 symtable（与搬迁工具的 AST 作用域分析是不同实现）找出每个函数里解析到模块全局的名字，
  名单内的名字不得裸用；`log` / `subprocess` / `threading` 必须来自 runtime 的转发代理。
- 本包内不得出现 global 声明：可变状态留在 dispatcher 上，经 `D.x = …` 写回。
"""
import ast
import json
import subprocess
import symtable
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "dispatcher_modules"
FIXTURE = REPO / "tests" / "fixtures" / "dispatcher_patch_names.json"
FORWARDED = {"log", "subprocess", "threading"}
PATCHED = set(json.loads(FIXTURE.read_text(encoding="utf-8"))["names"])


def _modules():
    return sorted(p for p in PKG.glob("*.py") if p.name not in ("__init__.py", "runtime.py"))


def _function_globals(table, out):
    for child in table.get_children():
        if child.get_type() == "function":
            for sym in child.get_symbols():
                if sym.is_global() and sym.is_referenced() or sym.is_declared_global():
                    out.append((child.get_name(), child.get_lineno(), sym.get_name(), sym.is_declared_global()))
        _function_globals(child, out)


class DispatcherSplitContract(unittest.TestCase):
    def test_patch_name_fixture_in_sync(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "dispatcher_patch_names.py"), "--check"],
                           capture_output=True, text=True, cwd=REPO)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr +
                         "\n测试改写 dispatcher 的名字有变化：运行 scripts/dispatcher_patch_names.py 重新生成 fixture，"
                         "并确认 dispatcher_modules/ 里对新名字经 D.<name> 读取。")

    def test_modules_exist(self):
        self.assertTrue(_modules(), "dispatcher_modules/ 下应有拆出的模块")

    def test_patched_names_are_late_bound(self):
        problems = []
        for path in _modules():
            text = path.read_text(encoding="utf-8")
            hits = []
            _function_globals(symtable.symtable(text, str(path), "exec"), hits)
            for fn, line, name, declared in hits:
                if declared:
                    problems.append(f"{path.name}:{line} {fn}() 声明了 global {name}（状态应留在 dispatcher 上，用 D.{name} 写回）")
                elif name in PATCHED and name not in FORWARDED:
                    problems.append(f"{path.name}:{line} {fn}() 直接读取 {name}，应改为 D.{name}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_forwarded_names_come_from_runtime(self):
        problems = []
        for path in _modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} & FORWARDED
            from_runtime = set()
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module == "dispatcher_modules.runtime":
                    from_runtime |= {a.asname or a.name for a in node.names}
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if (a.asname or a.name) in FORWARDED:
                            problems.append(f"{path.name}: `import {a.name}` 会绕过测试对 dispatcher.{a.name} 的替换")
            for name in sorted(used - from_runtime):
                problems.append(f"{path.name}: {name} 应从 dispatcher_modules.runtime 导入")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_main_entry_shares_module_object(self):
        src = (REPO / "dispatcher.py").read_text(encoding="utf-8")
        self.assertIn('sys.modules.setdefault("dispatcher", sys.modules[__name__])', src)


if __name__ == "__main__":
    unittest.main()
