#!/usr/bin/env python3
"""把 static/board/*.js 按文件名顺序拼接成 static/board.js（看板实际加载、测试读取的那份）。

为什么拆分片：board.js 曾是 7500 行单文件。浏览器里多个经典脚本本就共享全局作用域，但看板只
外链一个 board.js（模板变量与资源指纹机制不变），因此源码按功能拆在 static/board/，由本脚本拼回。

用法：
    .venv/bin/python scripts/build_board_js.py          # 重新生成 static/board.js
    .venv/bin/python scripts/build_board_js.py --check  # 只校验是否同步（不同步退出码 1）

约束：分片按文件名排序拼接；分片里「加载时立即执行」的顶层语句不能引用后面分片才声明的名字
（函数声明只在单个文件内提升——虽然拼接后是同一文件，但保持这个约束可让分片将来直接独立加载）。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PARTS_DIR = REPO / "static" / "board"
TARGET = REPO / "static" / "board.js"


def build() -> str:
    parts = sorted(PARTS_DIR.glob("*.js"))
    if not parts:
        raise SystemExit(f"没有分片：{PARTS_DIR}")
    return "".join(p.read_text(encoding="utf-8") for p in parts)


def main():
    out = build()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != out:
            print("static/board.js 与 static/board/*.js 不同步：请改分片后运行 scripts/build_board_js.py",
                  file=sys.stderr)
            sys.exit(1)
        print("static/board.js 已同步")
        return
    TARGET.write_text(out, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO)} from {len(list(PARTS_DIR.glob('*.js')))} parts")


if __name__ == "__main__":
    main()
