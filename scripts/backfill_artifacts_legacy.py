#!/usr/bin/env python3
"""#53 一次性回填历史产物：把 legacy 产物根下的 <project>/req-* 复制到当前产物根。

默认 dry-run（只打印待复制清单，不写文件）；--apply 才真正复制。
规则：不覆盖当前根已有文件（含悬空符号链接）、不复制符号链接、源 resolve 后必须仍在
legacy 根内（拒绝越界）、保留 mtime（copy2）、自动建父目录；可重复执行（第二次 0 复制）。

用法（脚本只读进程环境变量，手工 python3 不会加载 .env；用环境变量前先
`set -a; . ./.env; set +a`，或始终显式传 --legacy）：
    python3 scripts/backfill_artifacts_legacy.py --legacy /old/.req-board/artifacts
    python3 scripts/backfill_artifacts_legacy.py \
        --legacy /old/.req-board/artifacts --dest ~/.req-board/artifacts --project loop-engine --apply
    set -a; . ./.env; set +a
    python3 scripts/backfill_artifacts_legacy.py --apply        # 环境变量已导出时

环境变量：REQ_BOARD_ARTIFACTS_LEGACY（多个根用 os.pathsep 分隔，取第一个）、
REQ_BOARD_ARTIFACTS（缺省 ~/.req-board/artifacts，与 board.py 一致）。
退出码：0 正常（含无待复制）/ 1 有复制失败 / 2 配置错误（根不存在、非目录、未指定 legacy、参数非法）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def _default_legacy() -> str:
    raw = (os.environ.get("REQ_BOARD_ARTIFACTS_LEGACY") or "").strip()
    return next((item.strip() for item in raw.split(os.pathsep) if item.strip()), "")


def _default_dest() -> str:
    return os.environ.get("REQ_BOARD_ARTIFACTS") or str(Path.home() / ".req-board" / "artifacts")


def _parse_args(argv):
    p = argparse.ArgumentParser(description="把 legacy 产物根复制到当前产物根（默认 dry-run）")
    p.add_argument("--legacy", default=_default_legacy(),
                   help="旧产物根（默认 REQ_BOARD_ARTIFACTS_LEGACY 第一项）")
    p.add_argument("--dest", default=_default_dest(),
                   help="当前产物根（默认 REQ_BOARD_ARTIFACTS 或 ~/.req-board/artifacts）")
    p.add_argument("--project", default=None, help="仅处理指定项目目录名")
    p.add_argument("--apply", action="store_true", help="真正复制（缺省仅打印清单）")
    return p.parse_args(argv)


def _iter_sources(legacy: Path, project_filter):
    """产出 legacy 下 <project>/req-*/** 的常规文件；不跟随/不复制符号链接，越界项跳过。"""
    legacy_r = legacy.resolve()
    projects = sorted((p for p in legacy.iterdir() if p.is_dir() and not p.is_symlink()),
                      key=lambda p: p.name)
    for project_dir in projects:
        if project_filter and project_dir.name != project_filter:
            continue
        for req_dir in sorted(project_dir.glob("req-*"), key=lambda p: p.name):
            if not req_dir.is_dir() or req_dir.is_symlink():
                continue
            for root, dirs, files in os.walk(req_dir, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(root) / d).is_symlink())
                for name in sorted(files):
                    src = Path(root) / name
                    if src.is_symlink() or not src.is_file():
                        continue
                    try:
                        src_resolved = src.resolve()
                    except (OSError, RuntimeError):
                        continue
                    if not src_resolved.is_relative_to(legacy_r):
                        continue
                    yield src


def main(argv=None) -> int:
    args = _parse_args(argv)
    legacy = Path(args.legacy).expanduser()
    dest = Path(args.dest).expanduser()

    if not args.legacy.strip():
        print("配置错误：未指定 --legacy，且 REQ_BOARD_ARTIFACTS_LEGACY 未配置", file=sys.stderr)
        return 2
    if not legacy.is_dir():
        print(f"配置错误：legacy 根不存在或不是目录: {legacy}", file=sys.stderr)
        return 2
    if dest.exists() and not dest.is_dir():
        print(f"配置错误：目标根不是目录: {dest}", file=sys.stderr)
        return 2
    if args.project is not None:
        name = args.project.strip()
        if not name or name in (".", "..") or os.sep in name or (os.altsep and os.altsep in name):
            print(f"配置错误：--project 非法: {args.project!r}", file=sys.stderr)
            return 2
        args.project = name

    if legacy.resolve() == dest.resolve():
        print(f"legacy 根与目标根相同（{legacy}），无需回填")
        print("扫描: 0，待复制: 0，已存在跳过: 0，复制成功: 0，失败: 0")
        return 0

    scanned = pending = skipped = copied = failed = 0
    mode = "copy" if args.apply else "dry-run"
    for src in _iter_sources(legacy, args.project):
        scanned += 1
        target = dest / src.relative_to(legacy)
        if target.exists() or target.is_symlink():
            skipped += 1
            continue
        pending += 1
        print(f"[{mode}] {src} -> {target}")
        if not args.apply:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        except OSError as exc:
            failed += 1
            print(f"复制失败: {src} -> {target}: {exc}", file=sys.stderr)
        else:
            copied += 1

    print(f"扫描: {scanned}，待复制: {pending}，已存在跳过: {skipped}，"
          f"复制成功: {copied}，失败: {failed}")
    if not args.apply:
        print("dry-run 未写任何文件；确认后加 --apply 执行")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
