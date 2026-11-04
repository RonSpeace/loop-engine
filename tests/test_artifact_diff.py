#!/usr/bin/env python3
"""#381 页面查看 dev 分支 diff（diff.patch）+#53 历史产物根兼容：产物聚合与内容端点测试。

覆盖：
  1. /api/artifacts 每需求含「diff」条目（role=diff, filename=diff.patch），存在/缺失时
     exists/mtime 正确（与主产物同规则）；
  2. GET /api/requirements/<id>/artifacts/diff.patch 200 text/plain（.patch 加入
     ARTIFACT_TEXT_EXTS 生效），含 <script> 等字符原样返回（转义是前端渲染层职责）；
  3. 产物缺失时聚合 exists=false + 端点 404（前端据 exists=false 显示缺失提示）；
  4. 安全四件套不回归：非法文件名/路径穿越形态 404、非白名单扩展名 404；
  5. 既有 .md/.svg 文本返回不回归；
  6. #53 legacy 产物根（REQ_BOARD_ARTIFACTS_LEGACY）：A 聚合命中/当前根优先/双缺/未配置/
     legacy 根不存在/diff 命中，B 内容端点 legacy 命中/优先级/穿越与符号链接不放宽，
     C 配置解析（分隔/去重/去空/~ 展开/与当前根同项剔除）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_artifact_diff -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402


class ArtifactDiffApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="artifact-diff-"))
        cls._old_db, cls._old_art, cls._old_art_legacy = (
            b.DB_PATH, b.ARTIFACTS_ROOT, b.ARTIFACTS_LEGACY_ROOTS)
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        b.ARTIFACTS_ROOT = cls.tmp / "artifacts"
        b.ARTIFACTS_LEGACY_ROOTS = []
        cls.client = b.app.test_client()
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO requirements (title, project, status, notes) VALUES (?, ?, 'open', ?)",
                ("diff 视图测试需求", "req-board", "notes"))
            conn.commit()
            cls.req_id = cur.lastrowid
        cls.req_dir = b.ARTIFACTS_ROOT / "req-board" / f"req-{cls.req_id}"
        cls.req_dir.mkdir(parents=True)
        # 模拟调度器 _gen_review_diff 形态：仓库头（可带截断说明）+ 标准 git diff 输出（含 XSS 样例）
        cls.patch_content = (
            "===== 仓库 req-board（git diff 123abc...f-20260828-381）=====\n"
            "（diff 过长已截断：共 3 行，仅显示前 2000 行；可用 git_diff --stat 定位改动文件后 Read 补看）\n"
            "diff --git a/a.py b/a.py\n"
            "index 1234567..89abcde 100644\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line-a\n"
            "-old-<script>alert(1)</script>\n"
            "+new\n"
            "+<img src=x onerror=alert(1)>\n"
            "\\ No newline at end of file\n")
        (cls.req_dir / "diff.patch").write_text(cls.patch_content, encoding="utf-8")
        (cls.req_dir / "self-test.md").write_text("# 自测\n- 命令\n", encoding="utf-8")
        (cls.req_dir / "diagram.svg").write_text(
            "<svg xmlns=\"http://www.w3.org/2000/svg\"><script>alert(1)</script></svg>", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH, b.ARTIFACTS_ROOT, b.ARTIFACTS_LEGACY_ROOTS = (
            cls._old_db, cls._old_art, cls._old_art_legacy)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_artifacts_aggregate_has_diff_entry(self):
        res = self.client.get("/api/artifacts")
        self.assertEqual(res.status_code, 200)
        entries = res.get_json()[str(self.req_id)]
        diff = next((e for e in entries if e["role"] == "diff"), None)
        self.assertIsNotNone(diff, "聚合应含 diff 条目")
        self.assertEqual(diff["filename"], "diff.patch")
        self.assertTrue(diff["exists"])
        self.assertIsInstance(diff["mtime"], int)
        self.assertTrue(diff["path"].endswith(f"req-board/req-{self.req_id}/diff.patch"))
        self.assertTrue(diff["short"].endswith(f"req-board/req-{self.req_id}/diff.patch"))
        # 主产物条目照旧存在
        dev = next((e for e in entries if e["role"] == "dev"), None)
        self.assertIsNotNone(dev)
        self.assertTrue(dev["exists"])

    def test_diff_content_endpoint_text_plain(self):
        res = self.client.get(f"/api/requirements/{self.req_id}/artifacts/diff.patch")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/plain", res.headers.get("Content-Type", ""))
        body = res.get_data(as_text=True)
        # 后端原样返回（含 <script> 原文）；转义/防 XSS 由前端 renderDiff 渲染层负责（见 test_diff_render.py）
        self.assertIn("<script>alert(1)</script>", body)

    def test_diff_missing_aggregate_false_and_404(self):
        with b.get_db() as conn:
            cur = conn.execute("INSERT INTO requirements (title, project) VALUES ('无产物需求', 'req-board')")
            conn.commit()
            rid = cur.lastrowid
        entries = self.client.get("/api/artifacts").get_json()[str(rid)]
        diff = next(e for e in entries if e["role"] == "diff")
        self.assertFalse(diff["exists"])
        self.assertIsNone(diff["mtime"])
        res = self.client.get(f"/api/requirements/{rid}/artifacts/diff.patch")
        self.assertEqual(res.status_code, 404)

    def test_path_traversal_and_bad_name_404(self):
        # 安全四件套不回归：路径穿越/非法文件名形态（%2F 等在 werkzeug 解码后同样被白名单正则拦截）
        for fn in ("..%2Fdiff.patch", "..%5Cdiff.patch", "abc/def", "diff.patch.exe", "diff.patch%00.md"):
            res = self.client.get(f"/api/requirements/{self.req_id}/artifacts/{fn}")
            self.assertEqual(res.status_code, 404, fn)

    def test_svg_md_still_text(self):
        cases = (("self-test.md", "自测"), ("diagram.svg", "<script>alert(1)</script>"))
        for fn, expected in cases:
            res = self.client.get(f"/api/requirements/{self.req_id}/artifacts/{fn}")
            self.assertEqual(res.status_code, 200, fn)
            self.assertIn(expected, res.get_data(as_text=True))

    def test_unknown_ext_404(self):
        (self.req_dir / "x.bin").write_bytes(b"data")
        self.assertEqual(
            self.client.get(f"/api/requirements/{self.req_id}/artifacts/x.bin").status_code, 404)


class ArtifactLegacyApiTest(unittest.TestCase):
    """#53 legacy 产物根：当前根优先 → legacy 只读兜底；未配置时行为与改动前一致。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="artifact-legacy-"))
        cls._old = (b.DB_PATH, b.ARTIFACTS_ROOT, b.ARTIFACTS_LEGACY_ROOTS)
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        b.ARTIFACTS_ROOT = cls.tmp / "current"
        b.ARTIFACTS_LEGACY_ROOTS = [cls.tmp / "legacy"]
        cls.client = b.app.test_client()
        cls.ids = {}
        with b.get_db() as conn:
            for key in ("legacy", "current", "missing", "dir_escape", "file_escape"):
                cur = conn.execute(
                    "INSERT INTO requirements (title, project, status, notes)"
                    " VALUES (?, 'req-board', 'open', '')", (f"legacy 产物测试 {key}",))
                cls.ids[key] = cur.lastrowid
            # project 含 .. 的构造库：聚合与内容端点都不得读到根外文件
            cur = conn.execute(
                "INSERT INTO requirements (title, project, status, notes)"
                " VALUES ('legacy 产物测试 traversal', '../evil', 'open', '')")
            cls.ids["traversal"] = cur.lastrowid
            conn.commit()
        rid = cls.ids["legacy"]
        cls._write(cls.legacy_req(rid), "analysis.md", "legacy analysis <script>alert(1)</script>")
        cls._write(cls.legacy_req(rid), "diff.patch", "legacy diff\n")
        rid = cls.ids["current"]
        cls._write(cls.current_req(rid), "analysis.md", "current analysis")
        cls._write(cls.legacy_req(rid), "analysis.md", "legacy analysis")
        # 符号链接逃逸 1：legacy 需求目录整体指向根外
        outside = cls.tmp / "outside" / f"req-{cls.ids['dir_escape']}"
        cls._write(outside, "analysis.md", "outside")
        (cls.tmp / "legacy" / "req-board").mkdir(parents=True, exist_ok=True)
        (cls.tmp / "legacy" / "req-board" / f"req-{cls.ids['dir_escape']}").symlink_to(outside)
        # 符号链接逃逸 2：需求目录内文件指向根外
        file_req = cls.legacy_req(cls.ids["file_escape"])
        file_req.mkdir(parents=True)
        outside_file = cls.tmp / "outside-file.md"
        outside_file.write_text("outside file", encoding="utf-8")
        (file_req / "analysis.md").symlink_to(outside_file)

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH, b.ARTIFACTS_ROOT, b.ARTIFACTS_LEGACY_ROOTS = cls._old
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _write(cls, directory, name, text):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")

    @classmethod
    def current_req(cls, rid):
        return b.ARTIFACTS_ROOT / "req-board" / f"req-{rid}"

    @classmethod
    def legacy_req(cls, rid):
        return b.ARTIFACTS_LEGACY_ROOTS[0] / "req-board" / f"req-{rid}"

    def _entry(self, rid, role):
        entries = self.client.get("/api/artifacts").get_json()[str(rid)]
        return next(e for e in entries if e["role"] == role)

    def test_a1_legacy_hit_aggregate(self):
        rid = self.ids["legacy"]
        e = self._entry(rid, "analysis")
        self.assertTrue(e["exists"])
        self.assertEqual(e["source"], "legacy")
        self.assertEqual(Path(e["path"]), self.legacy_req(rid) / "analysis.md")
        self.assertEqual(e["short"], f"req-board/req-{rid}/analysis.md")
        self.assertIsInstance(e["mtime"], int)

    def test_a2_current_root_priority(self):
        rid = self.ids["current"]
        e = self._entry(rid, "analysis")
        self.assertTrue(e["exists"])
        self.assertEqual(e["source"], "current")
        self.assertEqual(Path(e["path"]), self.current_req(rid) / "analysis.md")

    def test_a3_both_missing_and_unconfigured(self):
        rid = self.ids["missing"]
        e = self._entry(rid, "analysis")
        self.assertFalse(e["exists"])
        self.assertIsNone(e["mtime"])
        self.assertIsNone(e["source"])
        self.assertEqual(Path(e["path"]), self.current_req(rid) / "analysis.md")
        self.assertEqual(e["short"], f"req-board/req-{rid}/analysis.md")
        old = b.ARTIFACTS_LEGACY_ROOTS
        b.ARTIFACTS_LEGACY_ROOTS = []
        try:
            self.assertEqual(self._entry(rid, "analysis"), e)   # 未配置 legacy：输出与改动前一致
        finally:
            b.ARTIFACTS_LEGACY_ROOTS = old

    def test_a5_nonexistent_legacy_root(self):
        old = b.ARTIFACTS_LEGACY_ROOTS
        b.ARTIFACTS_LEGACY_ROOTS = [self.tmp / "no-such-root"]
        try:
            e = self._entry(self.ids["legacy"], "analysis")
        finally:
            b.ARTIFACTS_LEGACY_ROOTS = old
        self.assertFalse(e["exists"])
        self.assertIsNone(e["source"])

    def test_a6_diff_legacy_hit_and_missing(self):
        rid = self.ids["legacy"]
        e = self._entry(rid, "diff")
        self.assertTrue(e["exists"])
        self.assertEqual(e["source"], "legacy")
        self.assertEqual(Path(e["path"]), self.legacy_req(rid) / "diff.patch")
        res = self.client.get(f"/api/requirements/{rid}/artifacts/diff.patch")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/plain", res.headers.get("Content-Type", ""))
        self.assertEqual(res.get_data(as_text=True), "legacy diff\n")
        missing = self._entry(self.ids["missing"], "diff")
        self.assertFalse(missing["exists"])
        self.assertEqual(
            self.client.get(f"/api/requirements/{self.ids['missing']}/artifacts/diff.patch").status_code, 404)

    def test_a7_project_dotdot_rejected(self):
        """project 含 .. 的构造库：根外确有同名文件也不得出现在聚合或内容端点。"""
        rid = self.ids["traversal"]
        outside = self.tmp / "evil" / f"req-{rid}" / "analysis.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("outside evil", encoding="utf-8")
        e = self._entry(rid, "analysis")
        self.assertFalse(e["exists"])
        self.assertIsNone(e["source"])
        self.assertIsNone(e["mtime"])
        self.assertEqual(
            self.client.get(f"/api/requirements/{rid}/artifacts/analysis.md").status_code, 404)

    def test_b1_content_from_legacy(self):
        rid = self.ids["legacy"]
        res = self.client.get(f"/api/requirements/{rid}/artifacts/analysis.md")
        self.assertEqual(res.status_code, 200)
        self.assertIn("legacy analysis <script>alert(1)</script>", res.get_data(as_text=True))

    def test_b2_content_current_priority(self):
        res = self.client.get(f"/api/requirements/{self.ids['current']}/artifacts/analysis.md")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_data(as_text=True), "current analysis")

    def test_b3_traversal_not_loosened(self):
        rid = self.ids["current"]
        for fn in ("..%2Fanalysis.md", "..%5Canalysis.md", "abc/def", "analysis.md.exe",
                   "analysis.md%00.md"):
            self.assertEqual(
                self.client.get(f"/api/requirements/{rid}/artifacts/{fn}").status_code, 404, fn)

    def test_b4_symlink_escape_404(self):
        for key in ("dir_escape", "file_escape"):
            rid = self.ids[key]
            self.assertEqual(
                self.client.get(f"/api/requirements/{rid}/artifacts/analysis.md").status_code, 404, key)

    def test_c1_parse_separator_order_dedup(self):
        sep = os.pathsep
        a, bb = self.tmp / "a", self.tmp / "b"
        roots = b.parse_artifact_legacy_roots(f"{a}{sep}{bb}{sep}{a}", self.tmp / "current")
        self.assertEqual([p.name for p in roots], ["a", "b"])
        self.assertEqual(b.parse_artifact_legacy_roots(None, self.tmp / "current"), [])
        self.assertEqual(b.parse_artifact_legacy_roots("", self.tmp / "current"), [])

    def test_c2_parse_tilde_and_current_excluded(self):
        sep = os.pathsep
        current = self.tmp / "current"
        roots = b.parse_artifact_legacy_roots(f"~{sep}{current}", current)
        self.assertEqual(roots, [Path.home().resolve()])

    def test_c3_parse_blank_items(self):
        sep = os.pathsep
        self.assertEqual(
            b.parse_artifact_legacy_roots(f"{sep}   {sep}{sep} ", self.tmp / "current"), [])


if __name__ == "__main__":
    unittest.main()
