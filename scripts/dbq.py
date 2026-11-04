#!/usr/bin/env python3
"""只读数据库查询（read-only 角色专用）。

用法: python3 dbq.py <db文件> "<只读SQL>"

安全设计：
- 连接强制 SQLite URI 只读模式（mode=ro）+ PRAGMA query_only=ON，任何写操作都会失败；
- 前置校验 SQL 只能以 SELECT/PRAGMA/EXPLAIN/WITH/VALUES 开头（友好报错，最终兜底是只读连接）；
- 结果限量输出（默认最多 200 行、单元格截断 120 字符），避免刷屏。
"""
import re
import sqlite3
import sys

MAX_ROWS = 200
MAX_CELL = 120

READ_VERB = re.compile(r"^\s*(SELECT|PRAGMA|EXPLAIN|WITH|VALUES)\b", re.I)


def main():
    if len(sys.argv) != 3:
        print('用法: python3 dbq.py <db文件> "<只读SQL>"', file=sys.stderr)
        return 2
    db_path, sql = sys.argv[1], sys.argv[2].strip()
    if not sql:
        print("错误: SQL 为空", file=sys.stderr)
        return 2
    sql = sql.rstrip(";")
    if not READ_VERB.match(sql):
        first = sql.split()[0][:40]
        print(f"错误: 仅允许只读查询（SELECT/PRAGMA/EXPLAIN/WITH），当前: {first}", file=sys.stderr)
        return 2
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"错误: 无法打开数据库 {db_path}: {e}", file=sys.stderr)
        return 1
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        if not cols:
            print("(无结果集)")
            return 0
        print(" | ".join(cols))
        n = 0
        for row in cur:
            n += 1
            if n > MAX_ROWS:
                print(f"...（结果超过 {MAX_ROWS} 行，已截断，请加 LIMIT 缩小范围）")
                break
            cells = [str(c)[:MAX_CELL] if c is not None else "NULL" for c in row]
            print(" | ".join(cells))
        if n <= MAX_ROWS:
            print(f"({n} 行)")
        return 0
    except sqlite3.Error as e:
        print(f"SQL 错误: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
