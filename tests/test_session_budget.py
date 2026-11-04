#!/usr/bin/env python3
"""C2 会话预算硬上限测试。

- role_budget：角色默认步数上限 + projects.json 项目级 budgets 覆盖（max_steps / max_minutes）。
- 步数守卫：本轮新工具调用数达 step_budget → 触发守卫（kind=budget_steps），每轮 prompt 重新计数；
  step_budget 为空 / 守卫关闭 → 零行为。
- run_agent：步数截停后同会话给一轮收尾（限步数）写 result.json；写成 → 正常收尾，未写成 →
  aborted 带结构化原因 `budget_exceeded: steps`。
- 时间：角色 max_minutes 收紧单轮时限（notes 显式 timeout 优先），超时归因带 `budget_exceeded: time`。

隔离：桩会话 + run_agent 依赖全部打桩（同 test_output_guard），不起 kimi 进程。
"""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402
import tests.test_output_guard as tog  # noqa: E402  模块引用：避免其用例被本文件重复收集


def _tool_call(tid):
    return {"sessionUpdate": "tool_call", "toolCallId": tid, "title": "Read", "kind": "read"}


class RoleBudget(unittest.TestCase):

    def _pj(self, data):
        t = tempfile.mkdtemp()
        p = Path(t) / "projects.json"
        p.write_text(json.dumps(data))
        self.addCleanup(shutil.rmtree, t, True)
        return mock.patch.object(ac, "PROJECTS_FILE", p)

    def test_defaults(self):
        with self._pj({}):
            self.assertEqual(ac.role_budget("dev", "x")["max_steps"], ac.DEFAULT_ROLE_MAX_STEPS["dev"])
            self.assertIsNone(ac.role_budget("dev", "x")["max_minutes"])
            self.assertIsNone(ac.role_budget("unknown-role", "x")["max_steps"])

    def test_project_override(self):
        with self._pj({"p": {"paths": ["/x"], "budgets": {"dev": {"max_steps": 50, "max_minutes": 30}}}}):
            b = ac.role_budget("dev", "p")
            self.assertEqual((b["max_steps"], b["max_minutes"]), (50, 30))
            self.assertEqual(ac.role_budget("accept", "p")["max_steps"], ac.DEFAULT_ROLE_MAX_STEPS["accept"])

    def test_invalid_values_ignored(self):
        with self._pj({"p": {"paths": ["/x"], "budgets": {"dev": {"max_steps": "many", "max_minutes": -1}}}}):
            b = ac.role_budget("dev", "p")
            self.assertEqual(b["max_steps"], ac.DEFAULT_ROLE_MAX_STEPS["dev"])
            self.assertIsNone(b["max_minutes"])


class StepGuard(unittest.TestCase):

    def test_trips_at_budget(self):
        s = tog._stub()
        s.step_budget = 3
        for i in range(2):
            s._on_update(_tool_call(f"t{i}"))
        self.assertEqual(s.guard_calls, [])
        s._on_update(_tool_call("t2"))
        self.assertEqual(len(s.guard_calls), 1)
        self.assertEqual(s.guard_calls[0][0], "budget_steps")
        self.assertEqual(s.cancel_count, 1)

    def test_duplicate_tool_call_ids_not_double_counted(self):
        s = tog._stub()
        s.step_budget = 2
        s._on_update(_tool_call("t0"))
        s._on_update(_tool_call("t0"))
        self.assertEqual(s.guard_calls, [])

    def test_no_budget_no_trip(self):
        s = tog._stub()
        s.step_budget = None
        for i in range(50):
            s._on_update(_tool_call(f"t{i}"))
        self.assertEqual(s.guard_calls, [])

    def test_guard_off_zero_behaviour(self):
        s = tog._stub(guard=False)
        s.step_budget = 1
        s._on_update(_tool_call("t0"))
        s._on_update(_tool_call("t1"))
        self.assertEqual(s.cancel_count, 0)

    def test_count_resets_each_prompt(self):
        s = tog._stub()
        s.step_budget = 3
        s._on_update(_tool_call("a"))
        s._on_update(_tool_call("b"))
        s._rearm_guard_for_prompt()
        s._on_update(_tool_call("c"))
        s._on_update(_tool_call("d"))
        self.assertEqual(s.guard_calls, [])


class BudgetSession(tog.FakeSession):
    """FakeSession + 守卫 tripped 形态（run_agent 据此区分预算截停与输出守卫）"""

    def __init__(self, behaviours=None, tripped_kind="budget_steps"):
        super().__init__(behaviours)
        self._guard = {"tripped": {"kind": tripped_kind, "detail": "本轮工具调用达 3 步上限"}}
        self.step_budget = None
        self.budgets_seen = []

    def prompt(self, text, timeout):
        self.budgets_seen.append(self.step_budget)
        return super().prompt(text, timeout)


class RunAgentBudget(tog.RunAgentGuardTest):

    def _write_result(self, _s):
        (self.art / "result.json").write_text(json.dumps({"status": "blocked", "summary": "- 进度：改了一半"}))

    def test_c2_steps_wrapup_writes_result_then_normal_finish(self):
        fake = BudgetSession(behaviours=[tog._raise(d.TurnAborted("budget")), self._write_result])
        with mock.patch.object(d, "result_file_path", lambda req: self.art / "result.json"):
            self._run(fake)
        self.assertEqual(len(fake.prompts), 2, "截停后给一轮收尾")
        self.assertIn("步数上限", fake.prompts[1][0])
        self.assertEqual(fake.budgets_seen[1], d.BUDGET_WRAPUP_STEPS)
        self.assertEqual(self.results[-1][3], False, "收尾写成结果 → 正常收尾")
        self.assertEqual(self.results[-1][1], 0, "returncode 置 0，handle_result 才会采信 result.json")

    def test_c2_steps_wrapup_without_result_aborts_with_reason(self):
        fake = BudgetSession(behaviours=[tog._raise(d.TurnAborted("budget")), None])
        with mock.patch.object(d, "result_file_path", lambda req: self.art / "result.json"):
            self._run(fake)
        aborted = self.results[-1][3]
        self.assertIsInstance(aborted, str)
        self.assertIn("budget_exceeded: steps", aborted)

    def test_c2_output_guard_abort_unchanged(self):
        fake = BudgetSession(behaviours=[tog._raise(d.TurnAborted("repeat"))], tripped_kind="repeat_output")
        self._run(fake)
        self.assertEqual(len(fake.prompts), 1, "输出守卫中止不给收尾轮")
        self.assertIs(self.results[-1][3], True)

    def test_c2_step_budget_applied_to_auto_runs(self):
        fake = BudgetSession(behaviours=[None])
        with mock.patch.object(d, "role_budget", lambda role, project=None: {"max_steps": 77, "max_minutes": None}):
            self._run(fake)
        self.assertEqual(fake.budgets_seen[0], 77)


for _name in dir(tog.RunAgentGuardTest):
    if _name.startswith("test_"):
        setattr(RunAgentBudget, _name, None)


class TimeBudget(unittest.TestCase):

    def test_role_max_minutes_tightens_default(self):
        with mock.patch.object(d, "role_budget", lambda role, project=None: {"max_steps": None, "max_minutes": 30}):
            self.assertEqual(d.effective_timeout_minutes({"notes": "", "project": "p"}, "dev"), 30)

    def test_explicit_notes_timeout_wins(self):
        with mock.patch.object(d, "role_budget", lambda role, project=None: {"max_steps": None, "max_minutes": 30}):
            self.assertEqual(d.effective_timeout_minutes({"notes": "timeout: 90", "project": "p"}, "dev"), 90)

    def test_no_role_limit_keeps_default(self):
        with mock.patch.object(d, "role_budget", lambda role, project=None: {"max_steps": None, "max_minutes": None}):
            self.assertEqual(d.effective_timeout_minutes({"notes": "", "project": "p"}, "dev"),
                             d.AGENT_TIMEOUT // 60)

    def test_merge_resolve_timebox_unchanged(self):
        self.assertEqual(d.effective_timeout_minutes({"notes": "timeout: 90", "project": "p"}, "merge_resolve"),
                         d.MERGE_RESOLVE_TIMEOUT_MIN)


class HandleResultBudgetReason(tog.HandleResultAborted):

    def test_c2_reason_strings(self):
        req = {"id": 28013, "project": "req-board", "title": "T", "status": "open",
               "mode": "semi", "phase": "dev", "flow": "standard", "notes": ""}
        with d.state_lock:
            d.state["retries"].pop("28013", None)
        d.handle_result(req, "dev", -9, False, time.time(),
                        aborted="budget_exceeded: steps（本轮工具调用达 300 步上限），收尾轮未提交结果")
        with d.state_lock:
            rec = d.state["retries"].pop("28013", None)
        self.assertIn("budget_exceeded: steps", rec["last_failure"])

    def test_c2_timeout_reason_labelled(self):
        req = {"id": 28014, "project": "req-board", "title": "T", "status": "open",
               "mode": "semi", "phase": "dev", "flow": "standard", "notes": ""}
        with d.state_lock:
            d.state["retries"].pop("28014", None)
        d.handle_result(req, "dev", -9, True, time.time())
        with d.state_lock:
            rec = d.state["retries"].pop("28014", None)
        self.assertIn("运行超时", rec["last_failure"])
        self.assertIn("budget_exceeded: time", rec["last_failure"])


for _name in dir(tog.HandleResultAborted):
    if _name.startswith("test_"):
        setattr(HandleResultBudgetReason, _name, None)


if __name__ == "__main__":
    unittest.main()
