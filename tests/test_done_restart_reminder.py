#!/usr/bin/env python3
"""req-99 生效方式显式化：done 流程「需手动重启调度器」提醒测试。

改动：_set_done 新增 dispatcher_touched 参数，命中时 summary/飞书通知/时间线三处附
「改动涉及 dispatcher.py，需手动重启调度器（kill <pid>）」提醒；新增 _touches_dispatcher
检测 dev 分支相对各仓库当前分支的变更是否含 dispatcher.py；finalize_done（个人合并成功/
仅推分支项目）与 on_message 人工合并重试成功路径均传参；未命中保持原 done 行为不变。

req-106 修复：检测时机从「合并后」挪到「合并前」（--no-ff 合并后三点 diff 恒空，提醒
从未触发）；TouchesBeforeMergeOrderTest 用 recording mock 断言「先检测、后合并」，防回归。

req-264 新增：_kickstart_board_if_own 增加 req_id 参数，重启动作落调度器时间线——成功/
失败（异常或非零退出码）均 post_dispatcher_event(kind=merge)，失败保留 log.warning；
KickstartBoardEventTest 覆盖成功/失败/非自身项目路径与 _set_done 传参。

req-380 新增：命中 dispatcher.py 时 done 通知文案改为「调度器将自动重启」（自动重启机制
见 test_done_auto_restart.py），「需手动重启（kill <pid>）」仅作自动重启多次失败的兜底提醒。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899（#343）
# #8 标记隔离：钉「无标记=launchd 旧路径」基线（ambient 标记随进程树进入测试进程；
# KickstartBoardEventTest 断言 launchctl 原路径）。pytest 侧另有 conftest autouse 兜底。
os.environ.pop("REQ_BOARD_SUPERVISOR", None)

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

# 测试类间共享模块级 mock：前面的类会把 _set_done 换成记录器，这里保存真实引用供各 setUp 恢复
_REAL_SET_DONE = d._set_done
_REAL_NOTIFY = d.notify
_REAL_CARD_NOTIFY = d.notify_req_card

# 各测试类会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "_kickstart_board_if_own", "merge_dev_branches",
    "cleanup_dev_worktrees", "push_company_branches", "fetch_req", "_set_done")}


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fresh_state():
    return {"chat_id": "oc_req99_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


def fake_msg(text):
    """构造 on_message 入参（仅用 message_type/content/chat_id 三字段）"""
    content = json.dumps({"text": text}) if text else "{}"
    return types.SimpleNamespace(event=types.SimpleNamespace(
        message=types.SimpleNamespace(message_type="text", content=content, chat_id="oc_req99_test")))


class SetDoneReminderTest(unittest.TestCase):
    """_set_done dispatcher_touched 提醒：notify/summary/时间线 三处附提醒；未命中保持原行为"""

    def setUp(self):
        d.state = fresh_state()
        d._set_done = _REAL_SET_DONE   # 恢复真实函数（可能被前面的测试类替换成记录器）
        self.status = []      # set_status 记录
        self.summaries = []   # append_summary 记录
        self.events = []      # post_dispatcher_event 记录
        self.notifies = []    # notify 记录
        self.card_notifies = []  # notify_req_card 记录（kwargs；done 通知改走卡片后此处断言）
        d.save_state = lambda: None
        d.set_status = lambda rid, st: self.status.append((rid, st))
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append(k)
        d._kickstart_board_if_own = lambda *a, **k: None

    def tearDown(self):
        # 恢复全部被替换的模块级函数，防污染按字母序后跑的测试文件
        _restore_d()

    def test_t1_hit_reminder(self):
        """验收标准：命中 dispatcher.py → done 卡片通知附「调度器将自动重启」（#380 起不再要求手动 kill）"""
        d._set_done({"id": 99, "project": "req-board", "title": "T"}, dispatcher_touched=True)
        self.assertEqual(self.status, [(99, "done")])
        self.assertEqual(self.notifies, [])  # done 通知改走卡片，不再走文本 notify
        self.assertEqual(len(self.card_notifies), 1)
        kw = self.card_notifies[0]
        self.assertIn("dispatcher.py", kw["reminder"])
        self.assertIn("自动重启", kw["reminder"])
        self.assertNotIn("kill <pid>", kw["reminder"])
        self.assertFalse(kw["company"])
        self.assertTrue(any("dispatcher.py" in s and "自动重启" in s for s in self.summaries))
        self.assertTrue(any("dispatcher.py" in c for _, c in self.events))

    def test_t2_no_hit_keep_behavior(self):
        """验收标准：未涉及长驻进程 → 卡片无提醒，不写 summary/时间线"""
        d._set_done({"id": 98, "project": "req-board", "title": "T"})
        self.assertEqual(self.status, [(98, "done")])
        self.assertEqual(self.notifies, [])
        self.assertEqual(len(self.card_notifies), 1)
        self.assertEqual(self.card_notifies[0]["reminder"], "")
        self.assertEqual(self.summaries, [])
        self.assertEqual(self.events, [])

    def test_t2b_kickstart_last(self):
        """#266：done 流程先写 summary/时间线/发卡片，最后才重启看板（避免重启窗口吞掉写入）"""
        order = []
        d.append_summary = lambda rid, line: order.append("summary")
        d.post_dispatcher_event = lambda rid, content, kind="action": order.append("event")
        d.notify_req_card = lambda event, req, **k: order.append("card")
        d._kickstart_board_if_own = lambda project, req_id: order.append("kickstart")
        d._set_done({"id": 99, "project": "req-board", "title": "T"}, dispatcher_touched=True)
        self.assertEqual(order, ["summary", "event", "card", "kickstart"])

    def test_t2c_summary_write_failure_does_not_block_notify(self):
        """#266：append_summary 失败（如看板重启窗口）不阻断时间线事件与 done 卡片"""
        d.append_summary = mock.Mock(side_effect=RuntimeError("board down"))
        d._set_done({"id": 99, "project": "req-board", "title": "T"}, dispatcher_touched=True)
        self.assertEqual(len(self.card_notifies), 1)  # done 卡片仍发出
        self.assertTrue(any("自动重启" in c for _, c in self.events))

    def test_t1b_company_hit_reminder(self):
        """仅推分支项目 + 命中：卡片 company 标记 + 自动重启提醒"""
        with mock.patch.object(d, "PROJECTS", {"req-board": {"paths": [str(REPO)], "company": True}}):
            d._set_done({"id": 99, "project": "req-board", "title": "T"}, dispatcher_touched=True)
        kw = self.card_notifies[0]
        self.assertTrue(kw["company"])
        self.assertIn("dispatcher.py", kw["reminder"])
        self.assertIn("自动重启", kw["reminder"])


class SetDonePushFailedTest(unittest.TestCase):
    """#288：_set_done(push_failed) 仅推分支项目 f-分支自动 push 失败提醒：
    summary 留档 + 时间线事件 + done 卡片红字三处，done 不阻断"""

    def setUp(self):
        d.state = fresh_state()
        d._set_done = _REAL_SET_DONE   # 恢复真实函数
        self.summaries = []
        self.events = []
        self.card_notifies = []
        d.save_state = lambda: None
        d.set_status = lambda rid, st: None
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append(k)
        d._kickstart_board_if_own = lambda *a, **k: None

    def tearDown(self):
        _restore_d()

    def test_push_failed_three_places(self):
        """push 失败：summary + 时间线（kind=merge）+ 卡片红字提醒手动 push"""
        d._set_done({"id": 99, "project": "req-board", "title": "T"}, push_failed={"repo-a", "repo-b"})
        self.assertTrue(any("手动 push" in s and "repo-a" in s and "repo-b" in s for s in self.summaries))
        self.assertTrue(any(k == "merge" and "push 失败" in c for k, c in self.events))
        self.assertEqual(len(self.card_notifies), 1)
        kw = self.card_notifies[0]
        self.assertIn("手动 push", kw["reminder"])
        self.assertIn("repo-b", kw["reminder"])
        self.assertIn("git push origin", kw["reminder"])

    def test_push_failed_combines_with_dispatcher_reminder(self):
        """push 失败 + dispatcher 命中：卡片同时含两条提醒（互不覆盖）"""
        d._set_done({"id": 99, "project": "req-board", "title": "T"},
                    dispatcher_touched=True, push_failed={"repo-a"})
        kw = self.card_notifies[0]
        self.assertIn("自动重启", kw["reminder"])
        self.assertIn("手动 push", kw["reminder"])
        self.assertLess(kw["reminder"].index("自动重启"), kw["reminder"].index("手动 push"))

    def test_push_ok_no_reminder(self):
        """push 全部成功（push_failed 为空）→ 无提醒，与旧行为一致"""
        d._set_done({"id": 99, "project": "req-board", "title": "T"}, push_failed=set())
        self.assertEqual(self.card_notifies[0]["reminder"], "")
        self.assertEqual(self.summaries, [])
        self.assertEqual(self.events, [])


class TouchesDispatcherTest(unittest.TestCase):
    """_touches_dispatcher：按 dev 分支变更文件名判定；检测失败/无该分支静默返回 False"""

    def _run(self, stdout_lines, returncode=0):
        """mock subprocess.run：先 rev-parse（当前分支）后 diff --name-only"""
        calls = [
            mock.Mock(stdout="main\n", returncode=0),
            mock.Mock(stdout="\n".join(stdout_lines) + ("\n" if stdout_lines else ""), returncode=returncode),
        ]
        with mock.patch.object(d.subprocess, "run", side_effect=calls):
            return d._touches_dispatcher({"id": 99, "project": "req-board"}, "f-20260806-99")

    def test_t3_hit_dispatcher(self):
        """变更含 dispatcher.py → True"""
        self.assertTrue(self._run(["dispatcher.py", "AGENTS.md"]))

    def test_t3b_hit_acp_shared(self):
        """#423：变更含 acp_shared.py（调度器进程内加载模块）→ True（旧清单漏判即静默不生效）"""
        self.assertTrue(self._run(["acp_shared.py"]))
        self.assertTrue(self._run(["acp_shared.py", "dispatcher.py", "board.py"]))

    def test_t4_no_hit(self):
        """变更不含调度器模块 → False"""
        self.assertFalse(self._run(["board.py", "AGENTS.md"]))

    def test_t5_git_error_silent_false(self):
        """git diff 失败（如无该分支）→ 静默 False（提醒不阻断）"""
        self.assertFalse(self._run(["dispatcher.py"], returncode=1))


class FinalizeDoneTest(unittest.TestCase):
    """finalize_done 传参：本地合并项目合并成功/仅推分支项目路径都检测并传给 _set_done"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["99"] = {"project": "req-board", "branch": "f-20260806-99", "worktrees": []}
        self.done_calls = []      # _set_done 收到的 dispatcher_touched
        self.push_failed_calls = []  # _set_done 收到的 push_failed（#288）
        d.save_state = lambda: None
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.push_company_branches = lambda req, branch, repos: set()

        def _record_set_done(req, dispatcher_touched=False, summary=None, push_failed=None, restart_failed=None,
                             restart_ok=False, restart_warn=""):
            self.done_calls.append(dispatcher_touched)
            self.push_failed_calls.append(push_failed)

        d._set_done = _record_set_done

    def tearDown(self):
        _restore_d()

    def test_t6_auto_merge_hit(self):
        """本地合并项目合并成功且涉及 dispatcher.py → _set_done(dispatcher_touched=True)"""
        with mock.patch.object(d, "_touches_dispatcher", return_value=True):
            d.finalize_done({"id": 99, "project": "req-board", "title": "T"})
        self.assertEqual(self.done_calls, [True])

    def test_t7_auto_merge_no_hit(self):
        """本地合并项目合并成功未涉及 dispatcher.py → False"""
        with mock.patch.object(d, "_touches_dispatcher", return_value=False):
            d.finalize_done({"id": 99, "project": "req-board", "title": "T"})
        self.assertEqual(self.done_calls, [False])

    def test_t8_company_project_detects(self):
        """仅推分支项目（不合并 main、分支保留）同样检测并传参；push 成功 → push_failed 为空"""
        with mock.patch.object(d, "PROJECTS", {"req-board": {"paths": [str(REPO)], "company": True,
                                                             "confirm": True}}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=True):
            d.finalize_done({"id": 99, "project": "req-board", "title": "T"})
        self.assertEqual(self.done_calls, [True])
        self.assertEqual(self.push_failed_calls, [set()])

    def test_t8b_company_push_failure_passed_through(self):
        """#288：仅推分支项目 push 失败 → push_failed 传给 _set_done（不阻断 done）"""
        with mock.patch.object(d, "PROJECTS", {"req-board": {"paths": [str(REPO)], "company": True,
                                                             "confirm": True}}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=True), \
             mock.patch.object(d, "push_company_branches", return_value={"req-board"}):
            d.finalize_done({"id": 99, "project": "req-board", "title": "T"})
        self.assertEqual(self.done_calls, [True])
        self.assertEqual(self.push_failed_calls, [{"req-board"}])


class PushCompanyBranchesTest(unittest.TestCase):
    """#288：push_company_branches——只 push 有改动仓库、无 origin 跳过、失败返回集合"""

    def _pj(self, paths, company=True):
        return mock.patch.object(d, "PROJECTS", {"p": {"paths": paths, "company": company}})

    def _run_mock(self, results):
        """results: [(returncode, stdout)] 按 subprocess.run 调用顺序消费"""
        side_effect = [mock.Mock(returncode=rc, stdout=out, stderr=b"err") for rc, out in results]
        return mock.patch.object(d.subprocess, "run", side_effect=side_effect)

    def test_push_only_changed_repos(self):
        """只 push changed 集合内仓库：无改动仓库不执行任何 git 命令"""
        req = {"id": 99, "project": "p"}
        with self._pj(["/x/repo-a", "/x/repo-b"]), \
             self._run_mock([(0, "https://x/repo-a.git"), (0, "")]) as run, \
             mock.patch.object(d.log, "info"), mock.patch.object(d.log, "warning"):
            failed = d.push_company_branches(req, "f-20260806-99", {"repo-a"})
        self.assertEqual(failed, set())
        calls = run.call_args_list
        self.assertEqual([c.args[0] for c in calls], [
            ["git", "config", "--get", "remote.origin.url"],
            ["git", "push", "origin", "f-20260806-99"],
        ])
        self.assertTrue(all(c.kwargs["cwd"] == "/x/repo-a" for c in calls))

    def test_no_origin_skipped_not_failed(self):
        """仓库无 origin 远端：跳过 push，不算失败（本地纯项目属正常）"""
        req = {"id": 99, "project": "p"}
        with self._pj(["/x/repo-a"]), \
             self._run_mock([(1, "")]) as run, \
             mock.patch.object(d.log, "info"), mock.patch.object(d.log, "warning"):
            failed = d.push_company_branches(req, "f-20260806-99", {"repo-a"})
        self.assertEqual(failed, set())
        self.assertEqual(len(run.call_args_list), 1)  # 仅 config 检查，未 push

    def test_push_failure_returned(self):
        """push 非零退出码（网络/权限）→ 失败仓库名返回，不抛异常"""
        req = {"id": 99, "project": "p"}
        with self._pj(["/x/repo-a", "/x/repo-b"]), \
             self._run_mock([(0, "https://x/a.git"), (128, ""), (0, "https://x/b.git"), (0, "")]) as run, \
             mock.patch.object(d.log, "info"), mock.patch.object(d.log, "warning"):
            failed = d.push_company_branches(req, "f-20260806-99", {"repo-a", "repo-b"})
        self.assertEqual(failed, {"repo-a"})
        push_calls = [c for c in run.call_args_list if c.args[0][1] == "push"]
        self.assertEqual(len(push_calls), 2)

    def test_push_exception_returned(self):
        """push 抛异常（超时等）→ 同样记失败"""
        req = {"id": 99, "project": "p"}

        def side_effect(*a, **k):
            if a[0][1] == "config":
                return mock.Mock(returncode=0, stdout="url", stderr=b"")
            raise d.subprocess.TimeoutExpired("git push", 60)

        with self._pj(["/x/repo-a"]), \
             mock.patch.object(d.subprocess, "run", side_effect=side_effect), \
             mock.patch.object(d.log, "info"), mock.patch.object(d.log, "warning"):
            failed = d.push_company_branches(req, "f-20260806-99", {"repo-a"})
        self.assertEqual(failed, {"repo-a"})


class OnMessageMergeRetryTest(unittest.TestCase):
    """on_message 人工处理合并重试成功：cleanup 前检测并传参 _set_done"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["99"] = {"project": "req-board", "branch": "f-20260806-99", "worktrees": []}
        d.state["pending_confirm"]["99"] = {"req": None, "question": "Q", "kind": "merge"}
        self.done_calls = []
        d.save_state = lambda: None
        d.notify = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.fetch_req = lambda rid: {"id": rid, "project": "req-board", "title": "T"}
        d._set_done = lambda req, dispatcher_touched=False, summary=None, push_failed=None, restart_failed=None, \
            restart_ok=False, restart_warn="": self.done_calls.append(dispatcher_touched)

    def tearDown(self):
        _restore_d()

    def test_t9_merge_retry_hit(self):
        """人工合并重试成功且涉及 dispatcher.py → _set_done(dispatcher_touched=True)"""
        with mock.patch.object(d, "_touches_dispatcher", return_value=True):
            d.on_message(fake_msg("y 99"))
        self.assertEqual(self.done_calls, [True])

    def test_t10_merge_retry_no_hit(self):
        """人工合并重试成功未涉及 dispatcher.py → False"""
        with mock.patch.object(d, "_touches_dispatcher", return_value=False):
            d.on_message(fake_msg("y 99"))
        self.assertEqual(self.done_calls, [False])


class KickstartBoardEventTest(unittest.TestCase):
    """#264：_kickstart_board_if_own 落调度器时间线——成功/失败（异常/非零退出码）均发事件"""

    def setUp(self):
        self.events = []      # post_dispatcher_event 记录 (req_id, kind, content)
        self.warnings = []    # log.warning 记录
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((rid, kind, content))
        self._real_log = d.log
        d.log = mock.Mock()
        d.log.info = lambda *a, **k: None
        d.log.warning = lambda *a, **k: self.warnings.append(a[0])

    def tearDown(self):
        d.log = self._real_log
        _restore_d()

    def _own_pj(self):
        return mock.patch.object(d, "PROJECTS", {"req-board": {"paths": [str(REPO)]}})

    def test_t13_set_done_passes_req_id(self):
        """验收：_set_done 调用 _kickstart_board_if_own 时传 req[id]"""
        d.state = fresh_state()
        d.save_state = lambda: None
        d.set_status = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.notify_req_card = lambda *a, **k: None
        got = []
        d._kickstart_board_if_own = lambda project, req_id: got.append((project, req_id))
        _REAL_SET_DONE({"id": 264, "project": "req-board", "title": "T"})
        self.assertEqual(got, [("req-board", 264)])

    def test_t14_success_posts_event(self):
        """kickstart 成功 → 时间线事件（kind=merge，文案符合验收标准）"""
        r = mock.Mock(returncode=0)
        with self._own_pj(), mock.patch.object(d.subprocess, "run", return_value=r), \
             mock.patch.object(d, "_wait_board_up", return_value=True):
            d._kickstart_board_if_own("req-board", 264)
        self.assertEqual(self.events, [(264, "merge", "合并后已重启看板（local.req-board），新代码生效")])

    def test_t18_success_but_board_not_ready_posts_failure_event(self):
        """#266：kickstart 成功但看板 10s 未就绪 → 失败事件（需人工处理）"""
        r = mock.Mock(returncode=0)
        with self._own_pj(), mock.patch.object(d.subprocess, "run", return_value=r), \
             mock.patch.object(d, "_wait_board_up", return_value=False):
            d._kickstart_board_if_own("req-board", 264)
        self.assertEqual(len(self.events), 1)
        rid, kind, content = self.events[0]
        self.assertEqual((rid, kind), (264, "merge"))
        self.assertIn("看板自动重启失败", content)
        self.assertIn("未就绪", content)

    def test_t15_exception_posts_failure_event(self):
        """kickstart 抛异常 → 失败事件（含原因、需人工处理）+ log.warning 保留"""
        with self._own_pj(), mock.patch.object(d.subprocess, "run", side_effect=RuntimeError("boom")):
            d._kickstart_board_if_own("req-board", 264)
        self.assertEqual(len(self.events), 1)
        rid, kind, content = self.events[0]
        self.assertEqual((rid, kind), (264, "merge"))
        self.assertIn("看板自动重启失败", content)
        self.assertIn("boom", content)
        self.assertIn("需人工处理", content)
        self.assertTrue(any("重启看板失败" in w for w in self.warnings))

    def test_t16_nonzero_exit_posts_failure_event(self):
        """launchctl 非零退出码（不抛异常）同样视为失败：事件 + 告警（analysis R2）"""
        r = mock.Mock(returncode=3, stderr=b"service not found\n")
        with self._own_pj(), mock.patch.object(d.subprocess, "run", return_value=r):
            d._kickstart_board_if_own("req-board", 264)
        self.assertEqual(len(self.events), 1)
        rid, kind, content = self.events[0]
        self.assertEqual((rid, kind), (264, "merge"))
        self.assertIn("看板自动重启失败", content)
        self.assertIn("service not found", content)
        self.assertTrue(any("重启看板失败" in w for w in self.warnings))

    def test_t17_not_own_project_no_event(self):
        """非看板自身项目：无重启动作、无事件"""
        r = mock.Mock(returncode=0)
        with mock.patch.object(d, "PROJECTS", {"other": {"paths": ["/tmp/other"]}}), \
             mock.patch.object(d.subprocess, "run", return_value=r) as run:
            d._kickstart_board_if_own("other", 264)
        run.assert_not_called()
        self.assertEqual(self.events, [])


class TouchesBeforeMergeOrderTest(unittest.TestCase):
    """#106 时序回归：_touches_dispatcher 必须在 merge_dev_branches 之前执行。

    根因：--no-ff 合并后 branch 成为 main 祖先，三点 diff {base}...{branch} 恒空，
    合并后检测恒 False（提醒从未触发）。本测试用 recording mock 断言「先检测、后合并」，
    修复后若有人把检测挪回合并后，立即失败。"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["99"] = {"project": "req-board", "branch": "f-20260806-99", "worktrees": []}
        self.order = []   # 调用顺序记录
        d.save_state = lambda: None
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d._kickstart_board_if_own = lambda *a, **k: None
        d.notify = lambda *a, **k: None
        d.fetch_req = lambda rid: {"id": rid, "project": "req-board", "title": "T"}
        d._set_done = lambda *a, **k: None

    def tearDown(self):
        _restore_d()

    def _detect(self, req, branch):
        self.order.append("detect")
        return True

    def _merge(self, req):
        self.order.append("merge")
        return (True, [])

    def test_t11_finalize_done_detect_before_merge(self):
        """finalize_done 自动合并：检测先于合并（#106 核心时序）"""
        with mock.patch.object(d, "_touches_dispatcher", side_effect=self._detect), \
             mock.patch.object(d, "merge_dev_branches", side_effect=self._merge):
            d.finalize_done({"id": 99, "project": "req-board", "title": "T"})
        self.assertEqual(self.order, ["detect", "merge"])

    def test_t12_on_message_retry_detect_before_merge(self):
        """on_message 人工合并重试：检测先于合并"""
        d.state["pending_confirm"]["99"] = {"req": None, "question": "Q", "kind": "merge"}
        with mock.patch.object(d, "_touches_dispatcher", side_effect=self._detect), \
             mock.patch.object(d, "merge_dev_branches", side_effect=self._merge):
            d.on_message(fake_msg("y 99"))
        self.assertEqual(self.order, ["detect", "merge"])


if __name__ == "__main__":
    unittest.main()
