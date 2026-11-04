#!/usr/bin/env python3
"""#40 doctor 推荐入口与通过口径（U1/U2）。

- U1 文档入口：`AGENTS.md` 快速参考、`scripts/doctor.py` 模块 docstring、`docs/quickstart.md`
  三处推荐命令都是 `.venv/bin/python scripts/doctor.py`，都不再把裸 `python3 scripts/doctor.py`
  写成推荐入口，且通过口径（退出码 0 且输出「硬失败 0 项」）有落点。
- U2 通过口径实跑：主工作区 `.venv` 解释器跑 doctor → 退出码 0 + stdout 含「硬失败 0 项」+
  无「非推荐」警告；裸 `python3`（不在虚拟环境）跑 → 含「非推荐」警告，且该警告只记 ⚠️、
  不计入 ❌ 硬失败计数。环境前置不满足（venv 缺依赖 / kimi 不在 PATH / harness home 缺失 /
  projects.json 路径不存在等）时本用例降级 skip 并打印命中项——记为环境问题，不判改动失败。

运行（worktree 内）：
    .venv/bin/python \\
        -m pytest tests/test_doctor_entry.py -q
"""
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# 主工作区 venv：可用环境变量 REQ_BOARD_VENV_PYTHON 覆盖，缺省取仓库根 .venv
MAIN_VENV_PYTHON = Path(os.environ.get("REQ_BOARD_VENV_PYTHON") or REPO / ".venv" / "bin" / "python")
DOCTOR = REPO / "scripts" / "doctor.py"
RECOMMENDED = ".venv/bin/python scripts/doctor.py"


def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


class DoctorEntryDocsTest(unittest.TestCase):
    """U1：三处推荐入口一致；裸 python3 不再是推荐命令。"""

    def test_u1_recommended_entry_in_docs(self):
        for rel in ("AGENTS.md", "scripts/doctor.py", "docs/quickstart.md"):
            with self.subTest(rel=rel):
                self.assertIn(RECOMMENDED, _read(rel), rel)
        self.assertNotIn("| `python3 scripts/doctor.py`", _read("AGENTS.md"))
        docstring = _read("scripts/doctor.py")[:800]
        self.assertIn("非推荐", docstring)
        self.assertLess(docstring.index(RECOMMENDED), docstring.index("python3 scripts/doctor.py"))

    def test_u1_pass_criteria_documented(self):
        self.assertIn("硬失败 0 项", _read("AGENTS.md"))
        self.assertIn("硬失败 0 项", _read("docs/quickstart.md"))


class DoctorRunTest(unittest.TestCase):
    """U2：通过口径实跑（venv 绿 + 裸解释器只告警不计硬失败）。"""

    def _run(self, exe):
        return subprocess.run([str(exe), str(DOCTOR)], capture_output=True, text=True,
                              cwd=str(REPO), timeout=180)

    def _bare_python(self):
        """找一个确实不在虚拟环境里的解释器（`sys.prefix == sys.base_prefix`）。"""
        for cand in (shutil.which("python3"), "/usr/bin/python3"):
            if not cand or not Path(cand).exists():
                continue
            probe = subprocess.run([cand, "-c", "import sys; print(int(sys.prefix == sys.base_prefix))"],
                                   capture_output=True, text=True, timeout=60)
            if probe.returncode == 0 and probe.stdout.strip() == "1":
                return cand
        return None

    def tearDown(self):
        # doctor 的 check_dirs_and_port 会在仓库根建 data/（worktree 下为新建空目录）：测完删掉
        data = REPO / "data"
        try:
            if data.is_dir() and not any(data.iterdir()):
                data.rmdir()
        except OSError:
            pass

    def test_u2_venv_entry_passes(self):
        if not MAIN_VENV_PYTHON.exists():
            self.skipTest(f"主工作区 .venv 不存在（环境前置）：{MAIN_VENV_PYTHON}")
        proc = self._run(MAIN_VENV_PYTHON)
        if proc.returncode != 0:
            self.skipTest("doctor 硬失败（环境前置未满足，非本次改动失败）：\n" + proc.stdout)
        self.assertIn("硬失败 0 项", proc.stdout)
        self.assertNotIn("非推荐", proc.stdout)     # 主工作区 .venv 跑不打非推荐警告

    def test_u2_bare_python3_warns_not_hard_fail(self):
        exe = self._bare_python()
        if not exe:
            self.skipTest("未找到不在虚拟环境里的 python3（无法复现非推荐警告）")
        proc = self._run(exe)
        self.assertIn("非推荐入口", proc.stdout)
        fails = [ln for ln in proc.stdout.splitlines() if ln.startswith("❌")]
        self.assertFalse(any("非推荐" in ln for ln in fails), "非推荐警告不得记成硬失败")
        if "硬失败 0 项" in proc.stdout:
            self.assertEqual(fails, [])
        else:
            m = re.search(r"共 (\d+) 项硬失败", proc.stdout)
            self.assertIsNotNone(m, proc.stdout)
            self.assertEqual(int(m.group(1)), len(fails))


if __name__ == "__main__":
    unittest.main()
