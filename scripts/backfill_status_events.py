#!/usr/bin/env python3
"""#410 一次性历史回填：把 req_status_log 非 baseline 行回填为 dispatcher_events kind=status 事件。

背景：调度回放（dispatcher_events）自 #410 起由看板写入 status 事件，历史变更（#410 上线前
已发生的）从 req_status_log 回填，使旧需求回放同样完整。

规则：
- 仅回填 old_status 非 NULL 的行（baseline 快照行非真实变更，不回填）；
- ts 取 req_status_log.ts（保留变更时刻，不显示为回填时刻）——该列为 SQLite
  DEFAULT CURRENT_TIMESTAMP（UTC）；未迁移的库转存储时区，已迁移的库原样写入；
- 幂等：某 req_id 已存在 kind=status 事件则整体跳过该需求（重复执行不产生重复行）；
- 已知局限：回填行按 id 追加在时间线末尾，历史上跨会话/阶段交错的多段状态变更相对顺序
  与真实时序不完全一致（多数单段流程顺序恰好正确），属可接受局限。

用法: python3 backfill_status_events.py [--db <board.db 路径>]
默认 db: 仓库根 data/board.db（main 实例）。执行前建议先备份 data/board.db。
"""
import argparse
import sqlite3
from datetime import datetime, timezone
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage_time import format_storage_time

# 与 board.STATUS_LABEL_CN 保持一致（脚本独立运行，不 import board，避免模块级副作用）
STATUS_LABEL_CN = {"later": "暂缓", "open": "待开发", "route": "通道待确认", "doing": "进行中",
                   "review": "评审中", "done": "已完成", "dropped": "已废弃"}


def label(s):
    """状态 → 「中文(原文)」（与 board._status_label_cn 一致）"""
    if s in STATUS_LABEL_CN:
        return f"{STATUS_LABEL_CN[s]}({s})"
    return str(s)


def to_local(ts):
    """req_status_log.ts（SQLite CURRENT_TIMESTAMP，UTC 无时区标记）→ 存储时区字符串，
    与 board.now_str() 一致；解析失败原样返回兜底"""
    if not ts:
        return format_storage_time()
    try:
        return format_storage_time(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return ts


def main():
    ap = argparse.ArgumentParser(description="回填 status 事件到 dispatcher_events（幂等，可重复执行）")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "data" / "board.db"),
                    help="board.db 路径（默认仓库根 data/board.db）")
    args = ap.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"错误: DB 不存在 {db_path}", file=__import__("sys").stderr)
        return 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # 已有 status 事件的 req_id 集合（幂等：重复执行跳过已回填的需求）
    done = {r["req_id"] for r in conn.execute(
        "SELECT DISTINCT req_id FROM dispatcher_events WHERE kind = 'status'")}
    rows = conn.execute(
        "SELECT req_id, old_status, new_status, ts FROM req_status_log "
        "WHERE old_status IS NOT NULL ORDER BY req_id, id").fetchall()
    skipped = sum(1 for r in rows if r["req_id"] in done)
    todo = {}
    for r in rows:
        if r["req_id"] not in done:
            todo.setdefault(r["req_id"], []).append((r["old_status"], r["new_status"], r["ts"]))
    migrated = conn.execute("PRAGMA user_version").fetchone()[0] >= 1
    inserted = 0
    with conn:
        for req_id, items in todo.items():
            for old, new, ts in items:
                conn.execute(
                    "INSERT INTO dispatcher_events (req_id, kind, content, ts) VALUES (?, 'status', ?, ?)",
                    (req_id, f"状态变更：{label(old)} → {label(new)}", (ts if migrated else to_local(ts))))
                inserted += 1
    print(f"回填完成: 插入 {inserted} 条（需求 {len(todo)} 个），跳过 {skipped} 条（已回填需求）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
