"""req-396 自动合并冲突由调度器拉起 merge_resolve agent 解决 测试。

三层覆盖：
1. acp_shared 审批路由（git_merge 策略）：Bash 仅放行受限 git 形态（terminal 层 fail-closed
   复核拒绝 push/reset/rebase 等），Write/Edit 限写白名单内，主工作区写仍拒；
2. dispatcher finalize_done 冲突 → 先拉起 merge_resolve（配额 2 轮），耗尽/失败转现有
   飞书 y/n + 网页按钮人工通道（pending_confirm kind=merge 语义与 #386 前一致）；
3. handle_result merge_resolve 收尾：done → 恢复 main 现场（git merge --abort）→ 重试合并
   → _set_done；need_confirm/blocked/进程级失败 → 重试或兜底人工通道（fail-closed）。
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

import acp_shared as ac  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 防测试误连生产 8899

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "set_req", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "merge_dev_branches", "cleanup_dev_worktrees",
    "fetch_req", "_set_done", "spawn_agent", "changed_repos", "_annotate_extra_repos",
    "run_project_restart", "_touches_dispatcher", "has_code_changes", "merge_conflict_write",
    "ARTIFACTS_ROOT")}


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fresh_state():
    return {"chat_id": "oc_req396_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}, "req_repos": {},
            "held_procs": {}, "pending_restart": {}, "flow_review_rounds": {},
            "merge_resolve": {}}


def fake_req(rid=396, title="自动合并冲突", mode="auto", phase=None, flow=None):
    req = {"id": rid, "project": "req-board", "title": title, "mode": mode,
           "created_at": "2026-09-01 00:00:00"}
    if phase:
        req["phase"] = phase
    if flow:
        req["flow"] = flow
    return req


def _req_permission_params(text, title="Bash", tool_call_id=None):
    tc = {"title": title,
          "content": [{"type": "content",
                       "content": {"type": "text", "text": text}}]}
    if tool_call_id:
        tc["toolCallId"] = tool_call_id
    return {"options": [{"optionId": "approve_once", "kind": "allow_once"},
                        {"optionId": "reject", "kind": "reject_once"}],
            "toolCall": tc}


def _stub_session(role="analysis", workdir=None, artifact_root=None, allow_git_commit=False,
                  allow_git_merge=False, write_roots=None, main_ws_roots=None):
    s = ac.ACPSession.__new__(ac.ACPSession)
    s.req_id = 99999
    s.role = role
    s.workdir = Path(workdir) if workdir else Path(tempfile.gettempdir())
    s.artifact_root = Path(artifact_root) if artifact_root else Path(tempfile.gettempdir()) / "art"
    s.project = "req-board"
    s.allow_git_commit = allow_git_commit
    s.allow_git_merge = allow_git_merge
    s.write_roots = [Path(r).resolve() for r in (write_roots or [])] or None
    s.main_ws_roots = main_ws_roots
    s._tool_cmds = {}
    s.emitted = []
    s._emit = lambda kind, content, session_id=None: s.emitted.append((kind, content))
    return s


class MergeResolveApprovalTest(unittest.TestCase):
    """acp_shared git_merge Bash 策略（#396）：terminal 层 fail-closed，受限形态放行/其余拒"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req396-l2-"))
        self.art = self.tmp / "art"
        self.art.mkdir()
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.main_ws = Path(tempfile.mkdtemp(prefix="req396-main-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.main_ws, ignore_errors=True)

    def _approved(self, outcome):
        return outcome.get("outcome", {}).get("outcome") == "selected"

    def _session(self):
        return _stub_session(role="merge_resolve", workdir=self.wt, artifact_root=self.art,
                             allow_git_commit=True, allow_git_merge=True,
                             write_roots=[str(self.wt), str(self.art), "/tmp"],
                             main_ws_roots=[str(self.main_ws)])

    def test_git_merge_forms_allowed(self):
        """验收：受限 git 形态（merge/add/commit/checkout/switch/stash + 只读子命令）放行"""
        s = self._session()
        for cmd in ("git status -s",
                    "git diff --name-only",
                    "git ls-files -u",
                    "git merge main",
                    f"cd {self.wt} && git merge main",
                    "git add -A",
                    "git add a.txt b/xx.txt",
                    "git commit -m 'merge main into dev'",
                    "git checkout main",
                    "git stash list"):
            self._approved(s._auto_approve(_req_permission_params(f"Requesting approval to Running: {cmd}")))
            s._terminal_approve(cmd)  # 无异常即放行

    def test_git_merge_denied_forms_terminal(self):
        """验收：push/reset/rebase/rm/mv/频率外命令一律拒（terminal 层机制 fail-closed）"""
        s = self._session()
        for cmd in ("git push origin main",
                    "git reset --hard HEAD",
                    "git rebase main",
                    "git rm a.txt",
                    "git mv a b",
                    "git cherry-pick abc123",
                    "git stash drop",
                    "git checkout -b new-branch",
                    "echo hello > /tmp/x.txt",
                    "ls /tmp",
                    "python3 -c 'print(1)'"):
            self.assertTrue(self._approved(s._auto_approve(_req_permission_params(
                f"Requesting approval to Running: {cmd}"))))  # 预检未命中仍 approve（#357）
            with self.assertRaises(PermissionError, msg=cmd):
                s._terminal_approve(cmd)

    def test_main_ws_write_guard_kept(self):
        """验收：merge_resolve 写主工作区命令仍被 #336 护栏拦截"""
        s = self._session()
        with self.assertRaises(PermissionError):
            s._terminal_approve(f"git -C {self.main_ws} add -A")

    def test_write_edit_in_worktree_allowed(self):
        """验收：内置 Write/Edit 限写白名单内（worktree/产物/tmp），主工作区外拒绝"""
        s = self._session()
        f = self.wt / "src" / "a.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self._approved(s._auto_approve(
            _req_permission_params(f"Writing {f}", title="Write"))))
        out = self.main_ws / "evil.txt"
        self.assertFalse(self._approved(s._auto_approve(
            _req_permission_params(f"Editing {out}", title="Edit"))))  # 主工作区写拒绝（fail-closed）
        self.assertFalse(ac.Path(out).exists())


class MergeResolveDispatcherTest(unittest.TestCase):
    """dispatcher finalize_done 冲突 → merge_resolve 拉起/配额/兜底（人工通道语义不变）"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["396"] = {"project": "req-board", "branch": "f-20260901-396",
                                       "worktrees": []}
        self.dones = []        # _set_done 记录
        self.status = []       # set_status 记录
        self.sums = []         # append_summary 记录
        self.events = []       # post_dispatcher_event 记录
        self.card_notifies = []  # notify_req_card kwargs 记录
        self.n_writes = []     # merge_conflict_write 记录
        self.spawns = []       # spawn_agent 记录 (role, kwargs)
        d.save_state = lambda: None
        d.set_status = lambda rid, st: self.status.append((rid, st))
        d.append_summary = lambda rid, line: self.sums.append(line)
        d.post_dispatcher_event = lambda *a, **k: None
        d.fetch_req = lambda rid: fake_req(rid)
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append((event, k))
        d._set_done = lambda *a, **k: self.dones.append((a, k))
        d.merge_conflict_write = lambda rid, action=None: self.n_writes.append((rid, action))
        d.spawn_agent = lambda req, role=None, **k: self.spawns.append((role, k))

    def tearDown(self):
        _restore_d()

    def test_role_allowed_gated(self):
        """验收：仅 state["merge_resolve"] 有登记时放行（防绕过拉起）"""
        self.assertFalse(d.role_allowed(fake_req(), "merge_resolve"))
        d.state["merge_resolve"]["396"] = {"failed": ["req-board"]}
        self.assertTrue(d.role_allowed(fake_req(), "merge_resolve"))
        d.state["merge_resolve"].pop("396")
        self.assertFalse(d.role_allowed(fake_req(), "merge_resolve"))
        # 其它角色不受影响（仅卡当前节点为 dev 时放行 dev）
        self.assertTrue(d.role_allowed(fake_req(mode="semi", phase="dev", flow="standard"), "dev"))

    def test_finalize_done_conflict_spawns_resolve(self):
        """验收：冲突 → 登记状态 + 拉起 merge_resolve（不立即转人工）"""
        d.merge_dev_branches = lambda req: (False, ["req-board"])
        d.finalize_done(fake_req())
        self.assertEqual(self.spawns, [("merge_resolve", {})])
        entry = d.state["merge_resolve"]["396"]
        self.assertEqual(entry["failed"], ["req-board"])
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(d.state["pending_confirm"], {})   # 未提前转人工
        self.assertEqual(self.card_notifies, [])

    def test_finalize_done_quota_exhausted_fallback(self):
        """验收：配额耗尽（2 轮后再次冲突）→ 转现有飞书/网页通道（语义与 #386 前一致）"""
        d.state["merge_resolve"]["396"] = {"failed": ["req-board"], "attempts": 2}
        d.merge_dev_branches = lambda req: (False, ["req-board"])
        d.finalize_done(fake_req())
        self.assertEqual(self.spawns, [])
        self.assertEqual(d.state["pending_confirm"]["396"]["kind"], "merge")
        self.assertEqual(self.card_notifies[0][0], "merge_conflict")
        self.assertEqual(self.n_writes, [("396", None)])

    def test_fallback_keeps_branch_worktree_and_question(self):
        """验收：need_confirm 兜底 — question 透传、分支/worktree 保留、红卡 + 按钮登记"""
        d.merge_dev_branches = lambda req: (False, ["req-board"])
        d._merge_conflict_fallback(fake_req(), {"branch": "f-20260901-396"}, ["req-board"],
                                   question="语义冲突：两边都新增了同名函数")
        self.assertEqual(d.state["pending_confirm"]["396"]["kind"], "merge")
        self.assertIn("语义冲突", d.state["pending_confirm"]["396"]["question"])
        self.assertTrue(any("已保留分支" in s for s in self.sums))
        self.assertEqual(self.card_notifies[0][0], "merge_conflict")
        self.assertEqual(self.n_writes, [("396", None)])
        self.assertEqual(d.state["merge_resolve"], {})   # 兜底后清状态


class HandleResultMergeResolveTest(unittest.TestCase):
    """handle_result merge_resolve 收尾（结果文件驱动，mock artifact 根避免写真实产物目录）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req396-hr-"))
        d.state = fresh_state()
        d.state["worktrees"]["396"] = {"project": "req-board", "branch": "f-20260901-396",
                                       "worktrees": []}
        d.state["merge_resolve"]["396"] = {"req": fake_req(), "failed": ["req-board"],
                                           "attempts": 1, "branch": "f-20260901-396",
                                           "summary": "dev 完成摘要"}
        self.dones = []
        self.sums = []
        self.status = []
        self.spawns = []
        self.n_notify = []
        d.ARTIFACTS_ROOT = self.tmp
        d.save_state = lambda: None
        d.clear_retries = lambda *a, **k: None
        d.set_status = lambda rid, st: self.status.append((rid, st))
        d.set_req = lambda *a, **k: None
        d.fetch_req = lambda rid: {"id": rid, "project": "req-board", "title": "T", "mode": "auto"}
        d.append_summary = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.notify_req_card = lambda *a, **k: None
        d._spawn_safe = lambda *a, **k: None
        d.spawn_agent = lambda req, role=None, **k: self.spawns.append((role, k))
        d._set_done = lambda *a, **k: self.dones.append((a, k))  # 必须 mock：真实 _set_done 会 kickstart 看板
        d.has_code_changes = lambda *a, **k: True
        d._touches_dispatcher = lambda *a, **k: False
        d.changed_repos = lambda *a, **k: {"req-board"}
        d._annotate_extra_repos = lambda *a, **k: None
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.run_project_restart = lambda *a, **k: ""
        d.merge_conflict_write = lambda *a, **k: None
        self.aborts = []
        self._orig_sub_run = d.subprocess.run

        def _fake_run(cmd, *a, **k):
            if cmd[:3] == ["git", "merge", "--abort"]:
                self.aborts.append(cmd)
            return mock.Mock(returncode=0)

        d.subprocess.run = _fake_run

    def tearDown(self):
        d.subprocess.run = self._orig_sub_run
        _restore_d()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_result(self, obj):
        f = d.result_file_path(fake_req())
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def test_valid_done_finishes_merge(self):
        """验收：done → abort 现场 → 重试合并成功 → cleanup + _set_done（dev 摘要透传）"""
        merges = []
        d.merge_dev_branches = lambda req: (merges.append(1) or (True, []))
        self._write_result({"status": "done", "summary": "已把 main 并入 dev 并提交"})
        d.handle_result(fake_req(), "merge_resolve", 0, False, time.time())
        self.assertEqual(len(merges), 1)           # 重试合并执行
        self.assertEqual(self.aborts, [["git", "merge", "--abort"]])  # merge --abort 恢复现场
        self.assertEqual(len(self.dones), 1)
        self.assertEqual(self.dones[0][1]["summary"], "dev 完成摘要")
        self.assertEqual(d.state["merge_resolve"], {})   # 状态清理
        self.assertEqual(d.state["worktrees"].get("396"), {"project": "req-board",
                                                           "branch": "f-20260901-396",
                                                           "worktrees": []})  # cleanup mock 未清

    def test_valid_done_still_conflict_fallback(self):
        """验收：resolve 后重试仍冲突 → 转人工通道（fail-closed）"""
        d.merge_dev_branches = lambda req: (False, ["req-board"])
        self._write_result({"status": "done", "summary": "S"})
        d.handle_result(fake_req(), "merge_resolve", 0, False, time.time())
        self.assertEqual(self.dones, [])
        self.assertEqual(d.state["pending_confirm"]["396"]["kind"], "merge")
        self.assertEqual(d.state["merge_resolve"], {})

    def test_need_confirm_fallback_question(self):
        """验收：语义冲突 need_confirm → 转人工通道（question 透传、不重试）"""
        d.merge_dev_branches = lambda req: (False, ["req-board"])
        self._write_result({"status": "need_confirm",
                            "question": "两边都新增了同名工具函数，业务语义不明"})
        d.handle_result(fake_req(), "merge_resolve", 0, False, time.time())
        self.assertEqual(self.spawns, [])   # 不重试
        self.assertEqual(d.state["pending_confirm"]["396"]["kind"], "merge")
        self.assertIn("同名工具函数", d.state["pending_confirm"]["396"]["question"])

    def test_blocked_fallback_no_retry(self):
        d.merge_dev_branches = lambda req: (False, ["req-board"])
        self._write_result({"status": "blocked", "summary": "冲突文件被个人配置改动覆盖，需人工"})
        d.handle_result(fake_req(), "merge_resolve", 0, False, time.time())
        self.assertEqual(self.spawns, [])
        self.assertEqual(d.state["pending_confirm"]["396"]["kind"], "merge")

    def test_process_failure_retry_within_quota(self):
        """验收：进程级失败（无结果文件）按独立配额重试（不污染 dev retries）"""
        d.merge_dev_branches = lambda req: (False, ["req-board"])
        d.handle_result(fake_req(), "merge_resolve", 0, False, time.time())  # 未写结果
        self.assertEqual(self.spawns[0][0], "merge_resolve")
        self.assertIs(self.spawns[0][1]["resume"], True)
        self.assertIn("失败", self.spawns[0][1]["extra"])
        # 配额耗尽 → 转人工
        d.state["merge_resolve"]["396"]["attempts"] = 2
        d.handle_result(fake_req(), "merge_resolve", 1, False, time.time())
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(d.state["pending_confirm"]["396"]["kind"], "merge")
        self.assertEqual(d.state["retries"], {})   # dev 重试计数未被污染

    def test_timeout_retry_within_quota(self):
        d.merge_dev_branches = lambda req: (False, ["req-board"])
        d.handle_result(fake_req(), "merge_resolve", 0, True, time.time())
        self.assertEqual(self.spawns[0][0], "merge_resolve")
        self.assertIs(self.spawns[0][1]["resume"], True)


class MergeResolvePromptTest(unittest.TestCase):
    """build_prompt merge_resolve 模板：注入冲突仓库上下文（主干探测，非硬编码）+ 15 分钟时间盒"""

    def setUp(self):
        d.state = fresh_state()
        # failed 必须是真实仓库路径（merge_dev_branches 返回绝对路径），主干探测走 _default_branch
        d.state["merge_resolve"]["396"] = {"failed": [str(REPO)], "attempts": 1}
        self.orig_ensure = d.ensure_dev_worktrees
        self.orig_listing = d.dev_repo_listing
        d.ensure_dev_worktrees = lambda req: (Path("/tmp/wt-396"), {"req-board": Path("/tmp/wt-396")})
        d.dev_repo_listing = lambda req, repo_map, focus=None: "工作目录：/tmp/wt-396（独立 worktree）"
        self.orig_projects = d.PROJECTS
        self.tmp_repo = None

    def tearDown(self):
        d.ensure_dev_worktrees = self.orig_ensure
        d.dev_repo_listing = self.orig_listing
        d.PROJECTS = self.orig_projects
        if self.tmp_repo:
            shutil.rmtree(self.tmp_repo, ignore_errors=True)
        _restore_d()

    def _git_repo(self, base_branch):
        """建临时 git 仓库（主干=base_branch），模拟配置了 base_branch 的项目"""
        repo = Path(tempfile.mkdtemp(prefix="req396-base-"))
        import subprocess as _sp
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        _sp.run(["git", "init", "-q", "-b", base_branch], cwd=repo, env=env)
        _sp.run(["git", "-C", str(repo), "config", "user.email", "t@t"])
        _sp.run(["git", "-C", str(repo), "config", "user.name", "t"])
        (repo / "f.txt").write_text("v1\n")
        _sp.run(["git", "-C", str(repo), "add", "."])
        _sp.run(["git", "-C", str(repo), "commit", "-qm", "base"])
        return repo

    def test_prompt_injects_detected_trunk(self):
        """验收：prompt 注入冲突仓库上下文（主干引用按探测输出）+ 15 分钟时间盒
        （本仓库无 master 有 main → 探测出 main，证明非硬编码 master 优先）"""
        p = d.build_prompt(fake_req(), "merge_resolve")
        self.assertIn("冲突仓库（需解决）：" + str(REPO), p)
        self.assertIn("各仓库主干引用（merge 目标，调度器主干探测：", p)
        self.assertIn(f"- {REPO.name}（主干: main）：git merge main", p)
        self.assertIn(f"约 {d.MERGE_RESOLVE_TIMEOUT_MIN} 分钟", p)
        self.assertIn("need_confirm", p)   # 语义冲突上报约束
        self.assertIn("git push", p)       # 权限边界说明

    def test_prompt_base_branch_injected_not_hardcoded(self):
        """验收（评审主问题 1）：项目配 base_branch 时 prompt 含该分支名、不再含硬编码 merge main"""
        # 原为 assertEqual(REPO.name, "req-board")：只是"跑在预期仓库"的前置校验，
        # 仓库目录改名（req-board → 其它目录名）后失效，与本节验收（base_branch 注入）无关，故移除
        repo = self._git_repo("f-20260303-bill")
        self.tmp_repo = repo
        d.state["merge_resolve"]["396"] = {"failed": [str(repo)], "attempts": 1}
        d.PROJECTS = {"req-board": {"paths": [str(repo)], "base_branch": "f-20260303-bill"}}
        p = d.build_prompt(fake_req(), "merge_resolve")
        self.assertIn(f"- {repo.name}（主干: f-20260303-bill）：git merge f-20260303-bill", p)
        self.assertNotIn("git merge main", p)
        self.assertNotIn("git merge master", p)
        self.assertIn(f"约 {d.MERGE_RESOLVE_TIMEOUT_MIN} 分钟", p)


if __name__ == "__main__":
    unittest.main()
