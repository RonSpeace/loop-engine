"""#32 Phase 2：首页前端取源单一入口。#33 起 CSS/JS 外置到 static/：
模板（HTML 结构）、static/board.js（脚本）、static/board.css（样式）三条取源在此统一提供。"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "board.html"
STATIC_JS = REPO / "static" / "board.js"
STATIC_CSS = REPO / "static" / "board.css"


def frontend_text() -> str:
    """templates/board.html 全文（模板与正文 DOM，不含已外置的 CSS/JS；Jinja 占位未渲染）。"""
    return TEMPLATE.read_text(encoding="utf-8")


def frontend_js() -> str:
    """static/board.js 全文（#33 外置的主脚本，节点 harness 按函数名提取的取源）。"""
    return STATIC_JS.read_text(encoding="utf-8")


def frontend_css() -> str:
    """static/board.css 全文（#33 外置的样式与令牌块）。"""
    return STATIC_CSS.read_text(encoding="utf-8")


def frontend_src() -> str:
    """模板全文 + static/board.js + static/board.css（跨 HTML/JS/CSS 的正负向断言用，覆盖面只增不减）。"""
    return "\n".join([frontend_text(), frontend_js(), frontend_css()])


def backend_text() -> str:
    """#32 Phase 3：后端 Python 源码全集 = board.py + board_modules/**/*.py（按路径排序拼接）。
    函数搬到 board_modules/ 后，按源码文本断言后端的测试改读此处；负向断言覆盖面只增不减。"""
    files = [REPO / "board.py"] + sorted((REPO / "board_modules").rglob("*.py"))
    return "\n".join(p.read_text(encoding="utf-8") for p in files)
