#!/usr/bin/env python3
"""枚举测试在 `dispatcher` 模块上改写/替换的名字（dispatcher 拆分兼容桥基线）。

扫描规则与 scripts/board_patch_names.py 相同，只换别名根（`d` / `dd` / `dispatcher`）与 fixture。
拆到 dispatcher_modules/ 的代码对名单里的名字必须经 `D.<name>` 晚绑定读取，否则测试补丁失效；
tests/test_dispatcher_split_contract.py 校验这一点。

用法：`python3 scripts/dispatcher_patch_names.py [--check]`
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_patch_names as scanner  # noqa: E402

scanner.configure("dispatcher", ("d", "dd", "dispatcher"),
                  scanner.TESTS / "fixtures" / "dispatcher_patch_names.json")
scan = scanner.scan

if __name__ == "__main__":
    raise SystemExit(scanner.main())
