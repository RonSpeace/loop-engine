#!/usr/bin/env python3
"""C4 打回只传失败项：summary 进 prompt 时折叠早期记录、不重复注入；评审产物注入限长。

- compact_summary：按 [auto] 记录分组，保留末尾 SUMMARY_PROMPT_KEEP 条全文，更早的折成一行摘要；
  人工答复/裁决类记录（含「人工」）与首段人工前言永不折叠（防 agent 重复追问已澄清的问题）。
- build_prompt：模板已含 {review_feedback} 时 {history} 置空（tech/dev 原本全文注入两遍）。
- _review_bounce_extra：评审产物全文超 REVIEW_EXTRA_MAX_CHARS 截断并给完整路径。

隔离：build_prompt mock ensure_dev_worktrees / dev_repo_listing（不建 worktree）。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


def rounds_summary(n):
    lines = ["人工前言：需求背景补充。"]
    for i in range(1, n + 1):
        lines.append(f"[auto] 完成总结：第 {i} 次实现 xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        lines.append(f"[auto] 代码审查未通过，打回需求开发重做（第 {i} 轮）。")
    return "\n".join(lines)


class CompactSummary(unittest.TestCase):

    def test_short_summary_kept_verbatim(self):
        s = rounds_summary(2)
        self.assertEqual(d.compact_summary(s), s)

    def test_long_summary_folds_older_entries(self):
        s = rounds_summary(10)
        out = d.compact_summary(s)
        self.assertIn("人工前言", out)                          # 首段前言保留
        self.assertIn("第 10 轮", out)                          # 最近记录全文
        self.assertNotIn("第 1 次实现", out)                    # 最早记录已折叠
        self.assertIn("已折叠", out)
        self.assertIn("打回", out)                              # 摘要行点明打回次数
        self.assertEqual(out.count("[auto]"), d.SUMMARY_PROMPT_KEEP)

    def test_human_entries_never_folded(self):
        s = rounds_summary(10).replace(
            "[auto] 完成总结：第 2 次实现", "[auto] 人工答复（问题：用哪个端口）：用 8902。\n[auto] 完成总结：第 2 次实现")
        out = d.compact_summary(s)
        self.assertIn("人工答复（问题：用哪个端口）：用 8902。", out)

    def test_user_decision_entries_never_folded(self):
        """评审复修：人工裁决多以「用户…」开头（用户否决方案/用户否决评审/用户否决豁免…）"""
        s = rounds_summary(10).replace(
            "[auto] 完成总结：第 2 次实现", "[auto] 用户否决方案：不要改 board.py 的接口签名\n[auto] 完成总结：第 2 次实现")
        self.assertIn("用户否决方案：不要改 board.py 的接口签名", d.compact_summary(s))

    def test_multiline_entry_kept_whole(self):
        s = "[auto] 完成总结：\n- 要点一\n- 要点二\n" + "\n".join(
            f"[auto] 第 {i} 条" for i in range(d.SUMMARY_PROMPT_KEEP - 1))
        self.assertEqual(d.compact_summary(s), s.strip())


class BuildPromptHistory(unittest.TestCase):

    def render(self, role, summary):
        req = {"id": 7, "project": "demo", "title": "T", "summary": summary}
        projects = {"demo": {"paths": ["/p"], "confirm": False, "company": False, "repo_map": "",
                             "restart": None, "auto_restart_after_merge": False,
                             "base_branch": None, "goal": ""}}
        wd = Path("/tmp/c4-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"demo": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value="工作目录：/tmp/c4-wd"), \
                mock.patch.object(d, "PROJECTS", projects):
            return d.build_prompt(req, role)

    def test_dev_summary_injected_once(self):
        p = self.render("dev", "[auto] 唯一标记 MARK-C4")
        self.assertEqual(p.count("MARK-C4"), 1)

    def test_roles_without_review_feedback_still_get_history(self):
        p = self.render("code_review", "[auto] 唯一标记 MARK-C4")
        self.assertEqual(p.count("MARK-C4"), 1)

    def test_prompt_growth_bounded_by_rounds(self):
        small = len(self.render("dev", rounds_summary(4)))
        big = len(self.render("dev", rounds_summary(30)))
        self.assertLess(big - small, 400)


class BounceExtraLimit(unittest.TestCase):

    def test_long_artifact_truncated_with_path(self):
        node = {"role": "code_review", "node_type": "review", "artifact": "code_review.md"}
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "code_review.md").write_text("问题清单\n" + "x" * (d.REVIEW_EXTRA_MAX_CHARS * 2))
            with mock.patch.object(d, "artifact_dir", lambda req: Path(t)):
                extra = d._review_bounce_extra({"id": 7}, node, "要点", 2)
        self.assertLess(len(extra), d.REVIEW_EXTRA_MAX_CHARS + 500)
        self.assertIn("已截断", extra)
        self.assertIn(str(Path(t) / "code_review.md"), extra)

    def test_short_artifact_full(self):
        node = {"role": "code_review", "node_type": "review", "artifact": "code_review.md"}
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "code_review.md").write_text("问题清单：A 未处理空值")
            with mock.patch.object(d, "artifact_dir", lambda req: Path(t)):
                extra = d._review_bounce_extra({"id": 7}, node, "要点", 2)
        self.assertIn("A 未处理空值", extra)
        self.assertNotIn("已截断", extra)


if __name__ == "__main__":
    unittest.main()
