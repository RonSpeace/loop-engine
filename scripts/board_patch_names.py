#!/usr/bin/env python3
"""枚举测试在 `board` 模块上改写/替换的名字（#32 拆分兼容桥基线）。

两条互补的扫描路径（任一路径命中即收录，避免单一正则漏检）：

1. AST（赋值/删除目标，覆盖缩进 / `self.b` / 元组解包等形态）：
   - `b.<name> = …` / `board.<name> = …`（含 `+=` 等增强赋值、注解赋值）
   - `self.b.<name> = …` / `cls.b.<name> = …`
   - 元组/列表解包：`b.a, b.<name> = …`
   - 删除：`del b.<name>` / `del b.__dict__["<name>"]`
   - 动态改写：`b.__dict__["<name>"] = …` / `vars(b)["<name>"] = …`
2. API 形态（AST 调用 + 正则，双路径任命中）：
   - `patch.object(b, "<name>"` / `patch.object(b, '<name>'`
   - `patch("board.<name>…")` / `patch('board.<name>…')`
   - `setattr(b, "<name>"` / `setattr(board, "<name>"` / `delattr(b, "<name>"`
   - `patch.multiple(b, <name>=…)` / `patch.dict(vars(b), {"<name>": …})`

输出排序去重的名单到 `tests/fixtures/board_patch_names.json`，供 U38 断言枚举结果与
fixture 一致；拆分后新模块对这些名字必须晚绑定（runtime.resolve 或薄壳），否则补丁失效。

用法：`python3 scripts/board_patch_names.py [--check]`
  --check：不落盘，仅校验现有 fixture 与实测一致（不一致退出码 1）。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
FIXTURE = TESTS / "fixtures" / "board_patch_names.json"

# board 模块在测试里的别名根：`import board as b` / `import board`；`self.b` / `cls.b` 亦识别。
_ALIAS_ROOTS = ("b", "board")
_SELF_ROOTS = ("self", "cls")

_ALIAS_RE = r"(?:(?:self|cls)\.)?(?:b|board)"

_PATTERNS = (
    re.compile(r"patch\.object\(\s*" + _ALIAS_RE + r"\s*,\s*['\"]([A-Za-z_][A-Za-z_0-9]*)['\"]"),
    re.compile(r"patch\(\s*['\"]board\.([A-Za-z_][A-Za-z_0-9]*)"),
    re.compile(r"setattr\(\s*" + _ALIAS_RE + r"\s*,\s*['\"]([A-Za-z_][A-Za-z_0-9]*)['\"]"),
)


def _is_alias(node: ast.AST) -> bool:
    """节点本身是否是 board 模块的别名引用（`b`/`board` 或 `self.b`/`cls.b`）。"""
    if isinstance(node, ast.Name):
        return node.id in _ALIAS_ROOTS
    if isinstance(node, ast.Attribute) and node.attr == "b":
        return (isinstance(node.value, ast.Name) and node.value.id in _SELF_ROOTS)
    return False


def _str_const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _alias_dict(node: ast.AST) -> bool:
    """board 别名的 __dict__：`b.__dict__` / `vars(b)` / `vars(self.b)`。"""
    if isinstance(node, ast.Attribute) and node.attr == "__dict__" and _is_alias(node.value):
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "vars" and len(node.args) == 1 and _is_alias(node.args[0]))


def _subscript_name(node: ast.AST) -> str | None:
    """动态改写形态 `b.__dict__["name"]` / `vars(b)["name"]` 的下标字符串键。"""
    if isinstance(node, ast.Subscript) and _alias_dict(node.value):
        return _str_const(node.slice)
    return None


def _collect_target(node: ast.AST, out: set[str]) -> None:
    """递归收集赋值/删除目标里「直接挂在 board 别名上」的名字（元组/列表解包逐项展开）。"""
    if isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _collect_target(elt, out)
    elif isinstance(node, ast.Attribute) and _is_alias(node.value):
        out.add(node.attr)
    else:
        key = _subscript_name(node)
        if key and key.isidentifier():
            out.add(key)


def _collect_call(node: ast.Call, out: set[str]) -> None:
    """非赋值 API 形态里带别名的改写：delattr / patch.multiple / patch.dict。"""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "multiple" and node.args and _is_alias(node.args[0]):
        for kw in node.keywords:
            if kw.arg and kw.arg.isidentifier():
                out.add(kw.arg)
    elif isinstance(func, ast.Attribute) and func.attr == "dict" and node.args and _alias_dict(node.args[0]):
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Dict):
            for k in node.args[1].keys:
                key = _str_const(k)
                if key and key.isidentifier():
                    out.add(key)
    elif isinstance(func, ast.Name) and func.id == "delattr" and len(node.args) >= 2 and _is_alias(node.args[0]):
        key = _str_const(node.args[1])
        if key and key.isidentifier():
            out.add(key)


def _scan_ast(text: str) -> set[str]:
    names: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                _collect_target(tgt, names)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            _collect_target(node.target, names)
        elif isinstance(node, ast.Delete):
            for tgt in node.targets:
                _collect_target(tgt, names)
        elif isinstance(node, ast.Call):
            _collect_call(node, names)
    return names


def _scan_regex(text: str) -> set[str]:
    names: set[str] = set()
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            names.add(m.group(1))
    return names


def scan() -> list[str]:
    names: set[str] = set()
    for path in sorted(TESTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        names |= _scan_ast(text)
        names |= _scan_regex(text)
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    names = scan()
    payload = {"names": names, "count": len(names)}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        existing = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if existing.get("names") != names:
            print("board_patch_names.json 与实测不一致", file=sys.stderr)
            return 1
        print(f"OK: {len(names)} 个名字与 fixture 一致")
        return 0
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(rendered, encoding="utf-8")
    print(f"写入 {FIXTURE.relative_to(REPO)}：{len(names)} 个名字")
    for n in names:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
