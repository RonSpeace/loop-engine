#!/usr/bin/env python3
"""launchd plist 模板化 + 双机同步脚本的防回归测试。

背景：两台机器（笔记本 / 台式机）共用同一个仓库，plist 里若写死 `/Users/<用户名>/...`
的仓库/日志绝对路径，同步就会互相覆盖、换机还会起不来。因此：
- `plists/` 只保留 `*.plist.tmpl`，用 `{{REPO}}` / `{{LOG_DIR}}` 占位；
- `scripts/install-launchd.sh` 安装时按本机路径渲染成 `~/Library/LaunchAgents/<label>.plist`；
- `scripts/sync.sh` 负责两台机器之间的 git 同步。

本用例断言模板不含机器绝对路径、渲染结果合法且是真实文件、两个脚本语法通过。
运行（worktree 内，用主工作区 .venv）：
    .venv/bin/python -m pytest tests/test_plists_templates.py -q
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLISTS_DIR = REPO / "plists"
INSTALLER = REPO / "scripts" / "install-launchd.sh"
SYNC = REPO / "scripts" / "sync.sh"
LABELS = (
    "local.req-board",
    "local.automation.req-board-dispatcher",
    "local.automation.log-rotate",
)


class PlistTemplateTest(unittest.TestCase):
    """模板里不能有机器绝对路径；安装脚本要按本机路径渲染。"""

    def test_templates_exist_and_are_machine_agnostic(self):
        for label in LABELS:
            with self.subTest(label=label):
                tmpl = PLISTS_DIR / f"{label}.plist.tmpl"
                self.assertTrue(tmpl.is_file(), f"缺模板 {tmpl}")
                text = tmpl.read_text(encoding="utf-8")
                self.assertIn("{{REPO}}", text)
                self.assertNotIn("/Users/", text, f"{tmpl.name} 仍含机器绝对路径")
                self.assertNotIn("/home/", text, f"{tmpl.name} 仍含机器绝对路径")

    def test_no_legacy_plist_left_in_repo(self):
        leftovers = sorted(p.name for p in PLISTS_DIR.glob("*.plist"))
        self.assertEqual(leftovers, [], f"plists/ 下不应再提交渲染后的 plist：{leftovers}")

    def test_installer_renders_machine_paths(self):
        if sys.platform != "darwin":
            self.skipTest("plutil 仅在 macOS 可用")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            log_dir = home / "Library" / "Logs" / "req-board"
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["REQ_BOARD_LOG_DIR"] = str(log_dir)
            r = subprocess.run(
                ["bash", str(INSTALLER), "--no-load"],
                env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(r.returncode, 0, f"渲染失败：\n{r.stdout}\n{r.stderr}")

            dest_dir = home / "Library" / "LaunchAgents"
            for label in LABELS:
                with self.subTest(label=label):
                    dst = dest_dir / f"{label}.plist"
                    self.assertTrue(dst.is_file(), f"未生成 {dst}")
                    self.assertFalse(dst.is_symlink(), f"{dst} 应是渲染后的真实文件，不是软链")
                    text = dst.read_text(encoding="utf-8")
                    self.assertIn(str(REPO), text, "渲染结果应写入本机仓库路径")
                    # 只有用到 {{LOG_DIR}} 的模板才应出现日志目录（log-rotate 输出落 /tmp）
                    tmpl_text = (PLISTS_DIR / f"{label}.plist.tmpl").read_text(encoding="utf-8")
                    if "{{LOG_DIR}}" in tmpl_text:
                        self.assertIn(str(log_dir), text, "渲染结果应写入本机日志目录")
                    self.assertNotIn("{{", text, "渲染后不应残留占位符")
                    lint = subprocess.run(
                        ["plutil", "-lint", str(dst)],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)


class SyncScriptTest(unittest.TestCase):
    """同步脚本存在且语法通过（不实际 push）。"""

    def test_bash_syntax(self):
        for script in (INSTALLER, SYNC):
            with self.subTest(script=script.name):
                r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_sync_uses_origin_main_and_merge_not_rebase(self):
        text = SYNC.read_text(encoding="utf-8")
        self.assertIn("origin", text)
        self.assertIn("main", text)
        self.assertIn("git merge --no-edit", text)
        self.assertNotIn("git pull --rebase", text)
