"""static/board.js 由 static/board/*.js 分片拼接生成（scripts/build_board_js.py）：二者必须同步。"""
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class BoardJsPartsTest(unittest.TestCase):
    def test_generated_file_in_sync(self):
        proc = subprocess.run([sys.executable, str(REPO / "scripts" / "build_board_js.py"), "--check"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0,
                         "static/board.js 与分片不同步：改 static/board/*.js 后运行 scripts/build_board_js.py\n"
                         + proc.stderr)

    def test_parts_exist_and_marked(self):
        parts = sorted((REPO / "static" / "board").glob("*.js"))
        self.assertGreaterEqual(len(parts), 2)
        self.assertIn("scripts/build_board_js.py", parts[0].read_text(encoding="utf-8").splitlines()[1])


if __name__ == "__main__":
    unittest.main()
