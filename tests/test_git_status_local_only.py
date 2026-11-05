"""纯本地仓库（没有任何远端）是合法形态：状态行标 local_only、不报「远端不存在」错误；
有远端但配置的远端名不在其中时仍报错。"""
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import _baseline  # noqa: E402,F401
from board_modules.blueprints import projects as bp  # noqa: E402


def _repo(root):
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    return root


class LocalOnlyRepoTest(unittest.TestCase):
    def test_no_remote_is_local_only_not_error(self):
        with TemporaryDirectory() as d:
            row = bp._git_status_row(_repo(Path(d)), {"remote": "origin"})
            self.assertTrue(row.get("local_only"))
            self.assertIsNone(row["error"])

    def test_configured_remote_missing_still_errors(self):
        with TemporaryDirectory() as d:
            r = _repo(Path(d))
            subprocess.run(["git", "-C", str(r), "remote", "add", "upstream", "https://example.invalid/x.git"], check=True)
            row = bp._git_status_row(r, {"remote": "origin"})
            self.assertFalse(row.get("local_only"))
            self.assertIn("origin", row["error"] or "")


if __name__ == "__main__":
    unittest.main()
