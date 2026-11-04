#!/usr/bin/env python3
"""#53 历史产物回填脚本测试（subprocess 调真实 CLI）。

覆盖：
  D1 dry-run 默认不写文件，stdout 含待复制清单与汇总；
  D2 --apply 复制缺失文件、内容与 mtime 保留、父目录自动创建；
  D3 不覆盖已存在文件（内容不同也保持原样）；
  D4 幂等：第二次 --apply 复制成功=0；
  D5 --legacy 与 --dest 相同 → no-op 退出 0；
  D6 配置错误：legacy 不存在/非目录 → 退出 2；--project 只处理指定项目；
  D7 符号链接不复制、目标为悬空链接视为已存在跳过；
  D8 源符号链接指向 legacy 根外 → 不复制。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_backfill_artifacts_legacy -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "backfill_artifacts_legacy.py"


class BackfillArtifactsLegacyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backfill-legacy-"))
        self.legacy = self.tmp / "legacy"
        self.dest = self.tmp / "dest"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _run(self, *args, env=None):
        e = dict(os.environ)
        e.pop("REQ_BOARD_ARTIFACTS", None)
        e.pop("REQ_BOARD_ARTIFACTS_LEGACY", None)
        if env:
            e.update(env)
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True, env=e, timeout=60)

    def test_d1_dry_run_writes_nothing(self):
        src = self.legacy / "demo" / "req-1" / "analysis.md"
        self._write(src, "legacy")
        proc = self._run("--legacy", str(self.legacy), "--dest", str(self.dest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[dry-run]", proc.stdout)
        self.assertIn(f"{src} -> {self.dest / 'demo' / 'req-1' / 'analysis.md'}", proc.stdout)
        self.assertIn("待复制: 1", proc.stdout)
        self.assertFalse(self.dest.exists())

    def test_d2_apply_copies_content_mtime_parents(self):
        src = self.legacy / "demo" / "req-1" / "sub" / "dev.md"
        self._write(src, "dev body")
        os.utime(src, (1_700_000_000, 1_700_000_000))
        proc = self._run("--legacy", str(self.legacy), "--dest", str(self.dest), "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dst = self.dest / "demo" / "req-1" / "sub" / "dev.md"
        self.assertEqual(dst.read_text(encoding="utf-8"), "dev body")
        self.assertEqual(int(dst.stat().st_mtime), 1_700_000_000)
        self.assertIn("复制成功: 1", proc.stdout)

    def test_d3_does_not_overwrite_existing(self):
        self._write(self.legacy / "demo" / "req-1" / "diff.patch", "legacy diff")
        self._write(self.dest / "demo" / "req-1" / "diff.patch", "current diff")
        proc = self._run("--legacy", str(self.legacy), "--dest", str(self.dest), "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.dest / "demo" / "req-1" / "diff.patch").read_text(encoding="utf-8"),
                         "current diff")
        self.assertIn("已存在跳过: 1", proc.stdout)
        self.assertIn("复制成功: 0", proc.stdout)

    def test_d4_second_apply_is_noop(self):
        self._write(self.legacy / "demo" / "req-1" / "analysis.md", "a")
        self._write(self.legacy / "demo" / "req-1" / "diff.patch", "d")
        first = self._run("--legacy", str(self.legacy), "--dest", str(self.dest), "--apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("复制成功: 2", first.stdout)
        second = self._run("--legacy", str(self.legacy), "--dest", str(self.dest), "--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("复制成功: 0", second.stdout)
        self.assertIn("已存在跳过: 2", second.stdout)

    def test_d5_same_root_noop(self):
        self._write(self.legacy / "demo" / "req-1" / "analysis.md", "a")
        proc = self._run("--legacy", str(self.legacy), "--dest", str(self.legacy), "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("无需回填", proc.stdout)
        self.assertIn("复制成功: 0", proc.stdout)

    def test_d6_config_errors_and_project_filter(self):
        self.assertEqual(self._run("--legacy", str(self.tmp / "nope"),
                                   "--dest", str(self.dest)).returncode, 2)
        plain_file = self.tmp / "file.txt"
        plain_file.write_text("x", encoding="utf-8")
        self.assertEqual(self._run("--legacy", str(plain_file), "--dest", str(self.dest)).returncode, 2)
        self.assertEqual(self._run("--legacy", str(self.legacy), "--dest", str(self.dest)).returncode, 2)
        self._write(self.legacy / "demo" / "req-1" / "analysis.md", "a")
        self._write(self.legacy / "other" / "req-2" / "analysis.md", "b")
        proc = self._run("--legacy", str(self.legacy), "--dest", str(self.dest),
                         "--project", "demo", "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.dest / "demo" / "req-1" / "analysis.md").exists())
        self.assertFalse((self.dest / "other").exists())

    def test_d7_symlinks_not_copied_and_dangling_target_skipped(self):
        outside = self.tmp / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        req = self.legacy / "demo" / "req-1"
        req.mkdir(parents=True)
        (req / "link.md").symlink_to(outside)
        (req / "analysis.md").write_text("source analysis", encoding="utf-8")
        (self.dest / "demo" / "req-1").mkdir(parents=True)
        (self.dest / "demo" / "req-1" / "analysis.md").symlink_to(self.tmp / "gone.md")
        proc = self._run("--legacy", str(self.legacy), "--dest", str(self.dest), "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.dest / "demo" / "req-1" / "link.md").exists())
        self.assertIn("已存在跳过: 1", proc.stdout)
        self.assertIn("复制成功: 0", proc.stdout)

    def test_d8_symlinked_dir_escape_not_copied(self):
        outside = self.tmp / "outside" / "req-1"
        self._write(outside / "secret.md", "secret")
        req = self.legacy / "demo" / "req-1"
        req.mkdir(parents=True)
        (req / "jump").symlink_to(outside, target_is_directory=True)
        self._write(req / "analysis.md", "a")
        proc = self._run("--legacy", str(self.legacy), "--dest", str(self.dest), "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.dest / "demo" / "req-1" / "analysis.md").exists())
        self.assertFalse((self.dest / "demo" / "req-1" / "jump").exists())


if __name__ == "__main__":
    unittest.main()
