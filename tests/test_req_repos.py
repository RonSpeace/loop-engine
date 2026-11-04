#!/usr/bin/env python3
"""需求涉及仓库清单（repos）机制测试：notes 人工声明 / analysis 产物 → state；
dev prompt 聚焦清单；done 收口按实际 diff 保留分支（仅推分支项目）+ 清单外改动标注。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_req_repos -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899（#343）

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "PROJECTS", "subprocess", "set_status", "set_req",
    "fetch_req", "append_summary", "post_dispatcher_event", "notify_req_card",
    "_spawn_safe", "artifact_ok", "artifact_dir", "api", "changed_repos",
    "effective_repos", "cleanup_dev_worktrees", "_set_done", "_annotate_extra_repos",
    "_touches_dispatcher")}


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def _install_multi_project():
    """多仓库项目：repo-a / repo-b 在临时目录（validate 仅比 basename，目录不必真实存在）"""
    tmp = Path(tempfile.mkdtemp(prefix="req-repos-"))
    d.PROJECTS = {
        "multi": {"paths": [str(tmp / "repo-a"), str(tmp / "repo-b"), str(tmp / "repo-c")],
                  "confirm": False, "company": False, "repo_map": ""},
        "req-board": {"paths": [str(REPO)], "confirm": False, "company": False, "repo_map": ""},
    }
    return tmp


class ParseNotesReposTest(unittest.TestCase):
    def test_plain_list(self):
        self.assertEqual(d.parse_notes_repos("背景：xx\nrepos: incentive-core, bff-vip-api\n验收：yy"),
                         ["incentive-core", "bff-vip-api"])

    def test_no_declaration(self):
        self.assertEqual(d.parse_notes_repos("背景：xx\n验收：yy"), [])

    def test_json_array(self):
        self.assertEqual(d.parse_notes_repos('repos: ["incentive-core", "bff-vip-api"]'),
                         ["incentive-core", "bff-vip-api"])

    def test_empty_and_noise(self):
        self.assertEqual(d.parse_notes_repos("repos:  "), [])
        self.assertEqual(d.parse_notes_repos("repositary: x\nrepos: a"), ["a"])  # 相似前缀不误判


class ValidateReposTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _install_multi_project()

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_filter_invalid_and_dedupe(self):
        self.assertEqual(d.validate_repos("multi", ["repo-b", "nope", "repo-a", "repo-b"]),
                         ["repo-b", "repo-a"])

    def test_all_invalid(self):
        self.assertEqual(d.validate_repos("multi", ["nope", "also-nope"]), [])

    def test_empty(self):
        self.assertEqual(d.validate_repos("multi", None), [])
        self.assertEqual(d.validate_repos("multi", []), [])


class EffectiveReposTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _install_multi_project()
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {}}

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _req(self, notes=None, rid=777001):
        r = {"id": rid, "project": "multi", "title": "T"}
        if notes is not None:
            r["notes"] = notes
        return r

    def test_notes_wins(self):
        d.state["req_repos"]["777001"] = ["repo-a"]
        self.assertEqual(d.effective_repos(self._req("repos: repo-b")), ["repo-b"])

    def test_analysis_fallback(self):
        d.state["req_repos"]["777001"] = ["repo-a", "repo-b"]
        self.assertEqual(d.effective_repos(self._req()), ["repo-a", "repo-b"])

    def test_empty_fallback(self):
        self.assertEqual(d.effective_repos(self._req()), [])

    def test_notes_invalid_filtered(self):
        d.state["req_repos"]["777001"] = ["repo-a"]
        self.assertEqual(d.effective_repos(self._req("repos: nope, repo-b")), ["repo-b"])


class AnalysisResultReposTest(unittest.TestCase):
    """handle_result 的 analysis 完成路径：repos 写入 state、事件带识别提示"""

    def setUp(self):
        self._tmp = _install_multi_project()
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {}}
        self.art = Path(tempfile.mkdtemp(prefix="req-repos-art-"))
        d.artifact_dir = lambda req: self.art
        d.save_state = lambda: None
        d.artifact_ok = lambda req, role: (True, "")
        d.api = lambda method, path, body=None: []
        self.summaries = []
        self.events = []
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        # 免确认路径（confirm=False + suggested auto）：直接进 dev
        self.req_set = []
        d.set_req = lambda rid, patch: self.req_set.append(patch)
        d.fetch_req = lambda rid: None
        d._spawn_safe = lambda req, role, **k: None
        self.card = []
        d.notify_req_card = lambda event, req, **k: self.card.append((event, k))

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self.art, ignore_errors=True)

    def _run_analysis(self, result_body):
        (self.art / "result.json").write_text(json.dumps(result_body, ensure_ascii=False))
        req = {"id": 777002, "project": "multi", "title": "T", "notes": "背景",
               "mode": None, "phase": None, "summary": ""}
        d.handle_result(req, "analysis", 0, False, time.time())

    def test_repos_saved_to_state(self):
        self._run_analysis({"status": "done", "summary": "s",
                            "suggested_flow": "standard", "repos": ["repo-a", "repo-b"]})
        self.assertEqual(d.state["req_repos"]["777002"], ["repo-a", "repo-b"])
        # #419：免确认 → 采纳推荐流程（写卡快照 flow=standard/首节点 dev）
        self.assertEqual(self.req_set[0]["flow"], "standard")
        self.assertEqual(self.req_set[0]["node_index"], 0)
        self.assertEqual(self.req_set[0]["flow_name"], "标准")
        self.assertTrue(any("识别涉及仓库 2 个" in c for _, c in self.events))

    def test_invalid_repos_filtered(self):
        self._run_analysis({"status": "done", "summary": "s",
                            "suggested_mode": "auto", "repos": ["repo-a", "nope"]})
        self.assertEqual(d.state["req_repos"]["777002"], ["repo-a"])

    def test_missing_repos(self):
        self._run_analysis({"status": "done", "summary": "s", "suggested_mode": "auto"})
        self.assertEqual(d.state["req_repos"]["777002"], [])
        self.assertTrue(any("未识别涉及仓库" in c for _, c in self.events))

    def test_outside_repos_goes_need_confirm(self):
        """#303 防复发（#419 机制等价迁移）：analysis repos 含清单外仓库名（疑似建错项目）→
        免确认也不直通，转 need_confirm 人工确认推荐流程，卡片/事件带清单外警示"""
        self._run_analysis({"status": "done", "summary": "s",
                            "suggested_flow": "standard", "repos": ["repo-a", "req-board"]})
        # 清单内名字照常入 state；不采纳流程（无快照写入）
        self.assertEqual(d.state["req_repos"]["777002"], ["repo-a"])
        self.assertEqual(self.req_set, [])
        entry = d.state["pending_confirm"]["777002"]
        self.assertEqual(entry["flow_id"], "standard")
        self.assertIn("清单外仓库", entry["question"])
        self.assertTrue(any("清单外仓库" in c for _, c in self.events))

    def test_no_outside_still_direct(self):
        """回归：repos 全在清单内 + 免确认 → 采纳推荐流程直通（#303 拦截不误伤）"""
        self._run_analysis({"status": "done", "summary": "s",
                            "suggested_flow": "standard", "repos": ["repo-a"]})
        self.assertEqual(self.req_set[0]["flow"], "standard")
        self.assertEqual(self.card, [])


class DevRepoListingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _install_multi_project()
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {}}
        self.req = {"id": 777003, "project": "multi", "title": "T", "notes": None,
                    "created_at": "2026-08-01T00:00:00"}
        self.repo_map = {name: Path("/wt") / name for name in ("repo-a", "repo-b", "repo-c")}

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_focus_marks_only_related(self):
        wd = d.dev_workdir_for(self.req)
        out = d.dev_repo_listing(self.req, self.repo_map, focus=["repo-b"])
        self.assertIn(f"- {wd}/repo-b -> /wt/repo-b（本次相关）", out)
        self.assertNotIn("（本次相关）", out.split("\n")[2])   # repo-a 行无标注
        self.assertIn("全部 3 个仓库的 worktree", out)
        self.assertIn("清单外仓库确有需要也可直接修改", out)

    def test_no_focus_lists_all(self):
        out = d.dev_repo_listing(self.req, self.repo_map, focus=[])
        self.assertEqual(out.count("（本次相关）"), 0)
        self.assertNotIn("清单外仓库", out)

    def test_effective_repos_drives_focus(self):
        d.state["req_repos"]["777003"] = ["repo-a", "repo-c"]
        out = d.dev_repo_listing(self.req, self.repo_map, focus=d.effective_repos(self.req))
        self.assertIn("（本次相关）", out.split("\n")[2])      # repo-a
        self.assertIn("（本次相关）", out.split("\n")[4])      # repo-c
        self.assertNotIn("（本次相关）", out.split("\n")[3])   # repo-b


class ChangedReposTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _install_multi_project()
        self.req = {"id": 777004, "project": "multi", "title": "T"}

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_returns_changed_basenames(self):
        def fake_run(cmd, **kw):
            cwd = str(kw.get("cwd") or "")
            if cmd[0:2] == ["git", "rev-parse"]:
                return mock.Mock(returncode=0, stdout="main\n")
            # git diff --name-only
            changed = "repo-a" in cwd or "repo-c" in cwd
            return mock.Mock(returncode=0, stdout="a.py\n" if changed else "")
        with mock.patch.object(d.subprocess, "run", side_effect=fake_run):
            self.assertEqual(d.changed_repos(self.req, "f-x-1"), {"repo-a", "repo-c"})

    def test_has_code_changes_reuses(self):
        def fake_run(cmd, **kw):
            cwd = str(kw.get("cwd") or "")
            if cmd[0:2] == ["git", "rev-parse"]:
                return mock.Mock(returncode=0, stdout="main\n")
            return mock.Mock(returncode=0, stdout="a.py\n" if "repo-b" in cwd else "")
        with mock.patch.object(d.subprocess, "run", side_effect=fake_run):
            self.assertTrue(d.has_code_changes(self.req, time.time()))
        with mock.patch.object(d.subprocess, "run",
                               side_effect=lambda cmd, **kw: mock.Mock(returncode=0, stdout="")):
            self.assertFalse(d.has_code_changes(self.req, time.time()))


class CleanupKeepChangedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _install_multi_project()
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {}}
        d.state["worktrees"]["777005"] = {
            "project": "multi", "branch": "f-20260801-777005",
            "worktrees": [(str(self._tmp / "repo-a"), "/wt/a"),
                          (str(self._tmp / "repo-b"), "/wt/b"),
                          (str(self._tmp / "repo-c"), "/wt/c")]}
        d.save_state = lambda: None
        self.removed, self.branch_deleted = [], []
        self.proc = mock.Mock(returncode=0)

        def fake_run(cmd, **kw):
            if cmd[0:3] == ["git", "worktree", "remove"]:
                self.removed.append(cmd[4])
            if cmd[0:2] == ["git", "branch"]:
                self.branch_deleted.append(str(kw.get("cwd") or ""))
            return self.proc
        d.subprocess = mock.Mock()
        d.subprocess.run.side_effect = fake_run

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_changed_mode_keeps_only_changed(self):
        d.cleanup_dev_worktrees(777005, delete_branch="changed",
                                keep_repos={"repo-a", "repo-c"})
        # 只删除未改动仓库（repo-b）的分支
        self.assertEqual(len(self.branch_deleted), 1)
        self.assertTrue(str(self.branch_deleted[0]).endswith("repo-b"))
        # worktree 全部清理
        self.assertEqual(len(self.removed), 3)
        self.assertEqual(d.state.get("worktrees", {}).get("777005"), None)

    def test_true_deletes_all(self):
        d.cleanup_dev_worktrees(777005, delete_branch=True)
        self.assertEqual(len(self.branch_deleted), 3)


class FinalizeDoneCompanyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _install_multi_project()
        d.PROJECTS["multi"]["company"] = True
        d.state = {"chat_id": None, "retries": {}, "pending_confirm": {}, "pending_route": {},
                   "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {}}
        d.state["worktrees"]["777006"] = {
            "project": "multi", "branch": "f-20260801-777006",
            "worktrees": [(str(self._tmp / "repo-a"), "/wt/a")]}
        d.state["req_repos"]["777006"] = ["repo-a"]
        d.save_state = lambda: None
        d._touches_dispatcher = lambda req, branch: False
        self.cleanups = []
        d.cleanup_dev_worktrees = lambda rid, delete_branch=True, keep_repos=None: \
            self.cleanups.append((rid, delete_branch, keep_repos))
        self.done_calls = []
        d._set_done = lambda req, **k: self.done_calls.append(k)
        self.annotated = []
        d._annotate_extra_repos = lambda req, changed: self.annotated.append(changed)

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_company_keeps_changed_branches(self):
        d.changed_repos = lambda req, branch: {"repo-a", "repo-b"}  # 清单外 repo-b 有改动
        req = {"id": 777006, "project": "multi", "title": "T", "notes": None}
        d.finalize_done(req)
        self.assertEqual(self.cleanups, [(777006, "changed", {"repo-a", "repo-b"})])
        self.assertEqual(len(self.done_calls), 1)
        # 清单外改动进入标注（反馈闭环）
        self.assertEqual(self.annotated, [{"repo-a", "repo-b"}])

    def test_company_no_extra_repos(self):
        d.changed_repos = lambda req, branch: {"repo-a"}
        req = {"id": 777006, "project": "multi", "title": "T", "notes": None}
        d.finalize_done(req)
        self.assertEqual(self.cleanups, [(777006, "changed", {"repo-a"})])
        self.assertEqual(self.annotated, [{"repo-a"}])  # 无清单外，标注函数内自己判断为空


class DevBranchTapdTest(unittest.TestCase):
    """dev 分支名规则：f-YYYYMMDD-<req_id>（创建日），notes 含 TAPD 链接时追加 -tapd<ID>"""

    def test_no_tapd_link_keeps_plain_name(self):
        req = {"id": 284, "created_at": "2026-08-10 23:00:15", "notes": "普通需求"}
        self.assertEqual(d.dev_branch(req), "f-20260810-284")

    def test_stories_link_appends_tapd_id(self):
        req = {"id": 284, "created_at": "2026-08-10 23:00:15",
               "notes": "背景：TAPD Bug转需求，链接 https://www.tapd.cn/55306211/prong/stories/view/1155306211001645897"}
        self.assertEqual(d.dev_branch(req), "f-20260810-284-tapd1155306211001645897")

    def test_bugs_link_appends_tapd_id(self):
        req = {"id": 284, "created_at": "2026-08-10 23:00:15",
               "notes": "链接 https://www.tapd.cn/55306211/bugtrace/bugs/view/1155306211001645897 是缺陷"}
        self.assertEqual(d.dev_branch(req), "f-20260810-284-tapd1155306211001645897")

    def test_missing_created_at_falls_back_to_today(self):
        req = {"id": 284, "notes": "https://www.tapd.cn/55306211/prong/stories/view/1155306211001645897"}
        branch = d.dev_branch(req)
        self.assertTrue(branch.startswith("f-"), branch)
        self.assertTrue(branch.endswith("-284-tapd1155306211001645897"), branch)

    def test_notes_without_link_no_suffix(self):
        req = {"id": 284, "created_at": "2026-08-10 23:00:15", "notes": None}
        self.assertEqual(d.dev_branch(req), "f-20260810-284")


if __name__ == "__main__":
    unittest.main()
