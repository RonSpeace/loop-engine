#!/usr/bin/env python3
"""unittest 线的 conftest 等价物：跑 unittest 时也能拿到确定性项目基线。

背景：`tests/conftest.py` 只有 pytest 会加载，`python -m unittest …` 看不见它。在
projects.json 里没有 `req-board` 的机器（或 worktree——其 projects.json 是主工作区那份的软链）
上直跑 unittest 会 KeyError: 'req-board'（环境噪声，与改动无关）。

钉桩实现在 `tests/_baseline.py`（单一源，与 conftest 共用）。本 runner 在跑之前调用 `pin()`，
因此走本 runner 的 unittest 线与 pytest 线拿到同一份基线。

用法（在仓库根）：
    # 全量（等价 pytest tests -q）
    python tests/run_unittest_with_fixture.py --discover
    # 点名复跑若干模块（worktree 内常用）
    python tests/run_unittest_with_fixture.py tests.test_done_auto_restart [更多模块…]

注意：本 runner **不清 REQ_BOARD_SUPERVISOR**（pytest 侧的 conftest autouse 会清）——需要
「无标记」基线的文件靠自身模块级 pop 硬护栏，故可携标记复跑。
退出码 = unittest 结果（0 全绿 / 1 有失败 / 2 用法错误）。
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    loader = unittest.TestLoader()
    if args[0] in ("--discover", "-d"):
        # 与白名单形态 `python -m unittest discover -s tests` 逐字等价：top_level_dir 不另设
        # （tests/ 无 __init__.py，设成仓库根会 ImportError: not importable），模块名按顶层导入。
        start = args[1] if len(args) > 1 else str(REPO / "tests")
        suite = loader.discover(start_dir=start)
    else:
        suite = unittest.TestSuite(loader.loadTestsFromName(n) for n in args)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
