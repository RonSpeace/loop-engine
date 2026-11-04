"""dispatcher 源码扫描用：dispatcher.py + dispatcher_modules/*.py（拆分后断言要覆盖全部源文件）。"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def dispatcher_source_files():
    return [REPO / "dispatcher.py"] + sorted((REPO / "dispatcher_modules").glob("*.py"))


def dispatcher_source():
    return "\n".join(p.read_text(encoding="utf-8") for p in dispatcher_source_files())
