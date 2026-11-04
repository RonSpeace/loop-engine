#!/usr/bin/env python3
"""仅推分支项目（branch_only，旧名 company）配置归一 + 飞书 SDK 可选依赖。

A1 load_projects：branch_only / company 任一为真 → 内部 company=True 且强制 confirm；均缺省 → False。
A2 未安装 lark_oapi 时 dispatcher 仍可 import，飞书通道按 REQ_BOARD_NO_FEISHU=1 同口径停用
   （子进程内屏蔽 lark_oapi 导入，不影响本进程已加载的 SDK）。

隔离：A1 用临时 projects.json 钉 d.PROJECTS_FILE；A2 子进程经 tests/_baseline.pin() 钉项目 fixture，
REQ_BOARD_DATA 指向临时目录，不触真实 projects.json / board.db。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import dispatcher as d  # noqa: E402


class LoadProjectsBranchOnlyTest(unittest.TestCase):
    def test_a1_branch_only_alias(self):
        tmp = Path(tempfile.mkdtemp(prefix="branch-only-"))
        pj = tmp / "projects.json"
        orig = d.PROJECTS_FILE
        d.PROJECTS_FILE = pj
        try:
            pj.write_text(json.dumps({
                "new": {"paths": ["/p"], "branch_only": True},
                "old": {"paths": ["/p"], "company": True},
                "plain": {"paths": ["/p"]},
            }), encoding="utf-8")
            out = d.load_projects()
            for name in ("new", "old"):
                self.assertTrue(out[name]["company"], name)
                self.assertTrue(out[name]["confirm"], name)
            self.assertFalse(out["plain"]["company"])
            self.assertFalse(out["plain"]["confirm"])
            self.assertNotIn("mars", out["new"])
        finally:
            d.PROJECTS_FILE = orig
            shutil.rmtree(tmp, ignore_errors=True)


_NO_LARK_SCRIPT = r"""
import builtins, sys
_real = builtins.__import__
def _imp(name, *a, **k):
    if name == "lark_oapi" or name.startswith("lark_oapi."):
        raise ImportError("blocked by test")
    return _real(name, *a, **k)
builtins.__import__ = _imp
sys.path.insert(0, sys.argv[1])
from tests import _baseline
_baseline.pin()
import dispatcher as d
assert d.LARK_AVAILABLE is False
assert d.NO_FEISHU is True
assert d.lark is None
print("OK")
"""


class OptionalLarkTest(unittest.TestCase):
    def test_a2_dispatcher_imports_without_lark(self):
        tmp = tempfile.mkdtemp(prefix="no-lark-")
        try:
            env = dict(os.environ, REQ_BOARD_DATA=tmp)
            env.pop("REQ_BOARD_NO_FEISHU", None)
            r = subprocess.run([sys.executable, "-c", _NO_LARK_SCRIPT, str(REPO)],
                               cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            self.assertIn("OK", r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
