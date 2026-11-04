#!/usr/bin/env python3
"""i18n 词条抽取：列出前端与服务端渲染片段里会出现在界面上的中文短语。

用法：
    .venv/bin/python scripts/i18n_extract.py            # 打印缺英文翻译的短语（按出现文件分组）
    .venv/bin/python scripts/i18n_extract.py --all      # 打印全部候选短语
    .venv/bin/python scripts/i18n_extract.py --json     # 输出 JSON（供脚本处理）

做法：取源码里含中文的字符串字面量，剥掉 HTML 标签、模板插值（${...} / {...} / %s）后按插值与
标签切成片段；每个片段就是 static/i18n.js 在 DOM 上做短语替换的单位。注释（// /* # 开头）不抽。
退出码：--check 模式下有缺失译文 → 1。
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCES = [
    "static/board.js",
    "templates/board.html",
    "board_modules/services/presentation.py",
]
HAN = re.compile(r"[一-鿿]")
LIT = re.compile(r"""(['"`])((?:(?!\1)[^\\\n]|\\.)*?)\1""")
SPLIT = re.compile(r"\$\{[^}]*\}|<[^>]*>|\{[a-zA-Z_][^}]*\}|%[sd]|\\n")


def _strip_comments(src, path):
    out = []
    for line in src.splitlines():
        s = line.lstrip()
        if s.startswith(("//", "#", "*", "/*")):
            continue
        out.append(line)
    return "\n".join(out)


def candidates():
    found = {}
    for rel in SOURCES:
        src = _strip_comments((REPO / rel).read_text(encoding="utf-8"), rel)
        if rel.endswith(".html"):
            texts = re.findall(r">([^<>]*[一-鿿][^<>]*)<", src)
            texts += [m[1] for m in re.findall(r'(title|placeholder|aria-label)="([^"]*[一-鿿][^"]*)"', src)]
        else:
            texts = [m[1] for m in LIT.findall(src) if HAN.search(m[1])]
        for t in texts:
            # 属性值（title/placeholder/aria-label）单独成条，再把残留标签/属性片段剔掉
            for attr in re.findall(r'(?:title|placeholder|aria-label)="([^"]*)"', t):
                for piece in SPLIT.split(attr):
                    piece = piece.strip()
                    if HAN.search(piece):
                        found.setdefault(piece, rel)
            t = re.sub(r'[\w-]+="[^"]*"', " ", t)
            for piece in SPLIT.split(t):
                piece = re.sub(r'^[^<>]*>|<[^<>]*$', "", piece).strip().strip('"').strip()
                if HAN.search(piece):
                    found.setdefault(piece, rel)
    return found


def main():
    sys.path.insert(0, str(REPO / "scripts"))
    import i18n_en_source
    have = set(i18n_en_source.PHRASES)
    found = candidates()
    show = found if "--all" in sys.argv else {k: v for k, v in found.items() if k not in have}
    if "--json" in sys.argv:
        print(json.dumps(show, ensure_ascii=False, indent=1))
    else:
        for k, v in sorted(show.items(), key=lambda kv: (kv[1], kv[0])):
            print(f"{v}\t{k}")
        print(f"# {len(show)} / {len(found)}", file=sys.stderr)
    if "--check" in sys.argv and show:
        sys.exit(1)


if __name__ == "__main__":
    main()
