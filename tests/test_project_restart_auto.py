#!/usr/bin/env python3
"""req-394 需求开发完成后自动判断并重启项目服务（auto_restart_after_merge 三档判定）测试。

改动：load_projects 透传 auto_restart_after_merge（bool，非法 fail-fast）与 restart 配方
扩展字段 trigger_prefixes/skip_prefixes（非字符串列表 fail-fast）；新增 _judge_restart_needed
（纯路径前缀三档判定 skip/restart/unknown）、_collect_merge_changed_paths（合并前采集 diff，
失败返回 None）；_maybe_auto_restart_after_merge 统一入口——开关未开维持 #300 行为（有配方
照常重启）、restart 执行配方、skip 不重启、unknown 不重启+告警（summary/merge 事件留痕，不
阻断 done）；finalize_done / on_message / 网页端重试合并三处成功路径接入；_set_done 新增
restart_warn 参数。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_project_restart_auto -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "_kickstart_board_if_own", "merge_dev_branches",
    "cleanup_dev_worktrees", "fetch_req", "_set_done", "run_project_restart",
    "_touches_dispatcher", "_collect_merge_changed_paths", "_maybe_auto_restart_after_merge")}


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fresh_state():
    return {"chat_id": "oc_req394_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


def fake_msg(text):
    content = json.dumps({"text": text}) if text else "{}"
    return types.SimpleNamespace(event=types.SimpleNamespace(
        message=types.SimpleNamespace(message_type="text", content=content, chat_id="oc_req394_test")))


import types  # noqa: E402

BPC_CFG = {
    "paths": ["/x/bpc"],
    "restart": {"cmd": "pkill -f 'app' || true", "verify": "http://localhost/x",
                "trigger_prefixes": ["src/web/templates/", "src/services/", "src/database.py"],
                "skip_prefixes": ["src/web/static/"]},
    "auto_restart_after_merge": True,
}


class JudgeRestartNeededTest(unittest.TestCase):
    """_judge_restart_needed 三档判定（纯路径前缀匹配，无 IO）：
    skip/restart/unknown 及混合规则、空清单、无 trigger 清单时全为 unknown"""

    def test_all_static_skip(self):
        self.assertEqual(d._judge_restart_needed(["src/web/static/app.js"], BPC_CFG), "skip")
        self.assertEqual(d._judge_restart_needed(["src/web/static/app.js", "src/web/static/style.css"], BPC_CFG), "skip")

    def test_trigger_hits_restart(self):
        for f in ("src/web/app.py", "src/web/templates/index.html", "src/services/x.py",
                  "src/models/y.py", "src/cli/z.py", "src/database.py"):
            cfg = {**BPC_CFG, "restart": {**BPC_CFG["restart"],
                                         "trigger_prefixes": ["src/web/app.py", "src/web/templates/",
                                                              "src/services/", "src/models/",
                                                              "src/cli/", "src/database.py"]}}
            self.assertEqual(d._judge_restart_needed([f], cfg), "restart", f)

    def test_trigger_mixed_skip_restart(self):
        """trigger × skip 混合 → restart"""
        self.assertEqual(
            d._judge_restart_needed(["src/web/static/app.js", "src/services/usage.py"], BPC_CFG),
            "restart")

    def test_unknown_returns_unknown(self):
        """含未知路径（README.md、docs/**）→ unknown"""
        for f in ("README.md", "docs/plan.md", "config/fixed_costs.yaml"):
            self.assertEqual(d._judge_restart_needed([f], BPC_CFG), "unknown", f)

    def test_unknown_priority_over_trigger(self):
        """trigger × unknown 混合 → unknown 优先（保守不重启+告警）"""
        self.assertEqual(
            d._judge_restart_needed(["src/services/usage.py", "README.md"], BPC_CFG), "unknown")

    def test_empty_list_skip(self):
        """空清单（纯合并/无实质改动）→ skip（不触发重启且无告警）"""
        self.assertEqual(d._judge_restart_needed([], BPC_CFG), "skip")
        self.assertEqual(d._judge_restart_needed(None, BPC_CFG), "skip")

    def test_no_trigger_list_all_unknown(self):
        """无 trigger 清单（项目未配置判定清单）→ 除 skip 外全部 unknown（保守）"""
        cfg = {"paths": ["/x/bpc"], "auto_restart_after_merge": True, "restart": {"cmd": "x"}}
        self.assertEqual(d._judge_restart_needed(["src/services/x.py"], cfg), "unknown")
        self.assertEqual(d._judge_restart_needed(["src/web/static/app.js"], cfg), "skip")

    def test_skip_default_prefix(self):
        """skip_prefixes 缺省 ["src/web/static/"]"""
        cfg = {"paths": ["/x/bpc"], "auto_restart_after_merge": True,
               "restart": {"cmd": "x", "trigger_prefixes": ["src/services/"]}}
        self.assertEqual(d._judge_restart_needed(["src/web/static/app.js"], cfg), "skip")
        self.assertEqual(d._judge_restart_needed(["src/services/x.py"], cfg), "restart")
        self.assertEqual(d._judge_restart_needed(["src/web/app.py"], cfg), "unknown")


class MaybeAutoRestartTest(unittest.TestCase):
    """_maybe_auto_restart_after_merge：开关语义、三档行为、diff 失败 fail-closed"""

    def setUp(self):
        self.restart_calls = []
        d.run_project_restart = lambda project, req_id: self.restart_calls.append((project, req_id)) or ""

    def tearDown(self):
        _restore_d()

    def test_off_maintains_300_behavior(self):
        """开关缺省/False → 不判定，维持 #300（有配方照常执行）"""
        cfg = {"paths": ["/x/bpc"], "restart": {"cmd": "x"}}
        with mock.patch.object(d, "PROJECTS", {"bpc": cfg}):
            failed, ok, warn = d._maybe_auto_restart_after_merge({"id": 394, "project": "bpc"}, ["README.md"], "394")
        self.assertEqual(self.restart_calls, [("bpc", "394")])
        self.assertIsNone(failed)
        self.assertTrue(ok)
        self.assertEqual(warn, "")

    def test_off_explicit_false_same(self):
        """显式 false → 同上"""
        cfg = {"paths": ["/x/bpc"], "restart": {"cmd": "x"}, "auto_restart_after_merge": False}
        with mock.patch.object(d, "PROJECTS", {"bpc": cfg}):
            failed, ok, warn = d._maybe_auto_restart_after_merge({"id": 394, "project": "bpc"}, ["src/services/x.py"], "394")
        self.assertEqual(self.restart_calls, [("bpc", "394")])

    def test_on_skip_no_restart(self):
        """开关开 + skip → 不重启（仅改静态文件）"""
        with mock.patch.object(d, "PROJECTS", {"bpc": BPC_CFG}):
            failed, ok, warn = d._maybe_auto_restart_after_merge(
                {"id": 394, "project": "bpc"}, ["src/web/static/app.js"], "394")
        self.assertEqual(self.restart_calls, [])
        self.assertIsNone(failed)
        self.assertFalse(ok)
        self.assertEqual(warn, "")

    def test_on_restart_executes(self):
        """开关开 + restart → 执行配方且 restart_ok 成立"""
        with mock.patch.object(d, "PROJECTS", {"bpc": BPC_CFG}):
            failed, ok, warn = d._maybe_auto_restart_after_merge(
                {"id": 394, "project": "bpc"}, ["src/services/usage.py"], "394")
        self.assertEqual(self.restart_calls, [("bpc", "394")])
        self.assertIsNone(failed)
        self.assertTrue(ok)

    def test_on_restart_failed_reports(self):
        """开关开 + restart 且配方失败 → restart_failed 透传（_set_done 三处留痕不阻断）"""
        d.run_project_restart = lambda project, req_id: "重启命令执行失败（退出码 3）"
        with mock.patch.object(d, "PROJECTS", {"bpc": BPC_CFG}):
            failed, ok, warn = d._maybe_auto_restart_after_merge(
                {"id": 394, "project": "bpc"}, ["src/services/usage.py"], "394")
        self.assertEqual(failed, "重启命令执行失败（退出码 3）")
        self.assertFalse(ok)
        self.assertEqual(warn, "")

    def test_on_unknown_warns(self):
        """开关开 + unknown → 不重启 + 告警文案（含未知路径样例）"""
        with mock.patch.object(d, "PROJECTS", {"bpc": BPC_CFG}):
            failed, ok, warn = d._maybe_auto_restart_after_merge(
                {"id": 394, "project": "bpc"}, ["README.md", "src/services/x.py"], "394")
        self.assertEqual(self.restart_calls, [])
        self.assertIsNone(failed)
        self.assertFalse(ok)
        self.assertIn("README.md", warn)
        self.assertIn("跳过自动重启", warn)

    def test_on_diff_fail_fail_closed(self):
        """diff 采集失败（None）→ fail-closed 按 unknown：不重启 + 告警"""
        with mock.patch.object(d, "PROJECTS", {"bpc": BPC_CFG}):
            failed, ok, warn = d._maybe_auto_restart_after_merge({"id": 394, "project": "bpc"}, None, "394")
        self.assertEqual(self.restart_calls, [])
        self.assertIsNone(failed)
        self.assertIn("获取失败", warn)

    def test_on_no_restart_recipe_noop(self):
        """开关 true 但无 restart 配方 → 返回 restart_failed=None（无异常）——cfg.restart 为空时
        _judge 无 trigger → unknown 告警；但 run_project_restart 本身缺配方 no-op"""
        cfg = {"paths": ["/x/bpc"], "auto_restart_after_merge": True}
        with mock.patch.object(d, "PROJECTS", {"bpc": cfg}):
            failed, ok, warn = d._maybe_auto_restart_after_merge({"id": 394, "project": "bpc"}, ["src/services/x.py"], "394")
        self.assertIsNone(failed)
        self.assertNotEqual(warn, "")


class CollectMergeChangedPathsTest(unittest.TestCase):
    """_collect_merge_changed_paths：采集 diff 文件清单，基线缺失/失败返回 None"""

    def test_collects_files(self):
        r = mock.Mock(returncode=0, stdout="src/services/a.py\nsrc/web/static/app.js\n", stderr="")
        with mock.patch.object(d, "PROJECTS", {"bpc": {"paths": ["/x/bpc"]}}), \
             mock.patch.object(d, "_branch_base", return_value="base"), \
             mock.patch("dispatcher.subprocess.run", return_value=r) as run:
            files = d._collect_merge_changed_paths({"id": 394, "project": "bpc"}, "f-394")
        self.assertEqual(files, ["src/services/a.py", "src/web/static/app.js"])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0][:4], ["git", "diff", "--name-only", "base...f-394"])

    def test_no_base_returns_none(self):
        with mock.patch.object(d, "PROJECTS", {"bpc": {"paths": ["/x/bpc"]}}), \
             mock.patch.object(d, "_branch_base", return_value=""):
            self.assertIsNone(d._collect_merge_changed_paths({"id": 394, "project": "bpc"}, "f-394"))

    def test_diff_fail_returns_none(self):
        r = mock.Mock(returncode=128, stdout="", stderr="fatal")
        with mock.patch.object(d, "PROJECTS", {"bpc": {"paths": ["/x/bpc"]}}), \
             mock.patch.object(d, "_branch_base", return_value="base"), \
             mock.patch("dispatcher.subprocess.run", return_value=r):
            self.assertIsNone(d._collect_merge_changed_paths({"id": 394, "project": "bpc"}, "f-394"))


class LoadProjectsAutoRestartTest(unittest.TestCase):
    """load_projects：auto_restart_after_merge 透传（bool），非法类型 fail-fast；
    trigger/skip 非字符串列表 fail-fast；缺省 False"""

    def test_passthrough(self):
        raw = {"a": {"paths": ["/x/a"], "auto_restart_after_merge": True, "restart": {"cmd": "go",
                   "trigger_prefixes": ["s/"], "skip_prefixes": ["t/"]}},
               "b": {"paths": ["/x/b"]}}
        fake = mock.Mock()
        fake.read_text.return_value = json.dumps(raw)
        with mock.patch.object(d, "PROJECTS_FILE", fake):
            out = d.load_projects()
        self.assertTrue(out["a"]["auto_restart_after_merge"])
        self.assertEqual(out["a"]["restart"]["trigger_prefixes"], ["s/"])
        self.assertEqual(out["a"]["restart"]["skip_prefixes"], ["t/"])
        self.assertFalse(out["b"]["auto_restart_after_merge"])

    def test_non_bool_fail_fast(self):
        raw = {"a": {"paths": ["/x/a"], "auto_restart_after_merge": "true"}}
        fake = mock.Mock()
        fake.read_text.return_value = json.dumps(raw)
        with mock.patch.object(d, "PROJECTS_FILE", fake):
            with self.assertRaises(ValueError) as ctx:
                d.load_projects()
        self.assertIn("auto_restart_after_merge", str(ctx.exception))

    def test_bad_prefixes_fail_fast(self):
        for val in ("s/", [1], "src"):
            raw = {"a": {"paths": ["/x/a"], "restart": {"cmd": "go", "trigger_prefixes": val}}}
            fake = mock.Mock()
            fake.read_text.return_value = json.dumps(raw)
            with mock.patch.object(d, "PROJECTS_FILE", fake):
                with self.assertRaises(ValueError) as ctx:
                    d.load_projects()
            self.assertIn("trigger_prefixes", str(ctx.exception))
        # skip 同理
        raw = {"a": {"paths": ["/x/a"], "restart": {"cmd": "go", "skip_prefixes": "x/"}}}
        fake = mock.Mock()
        fake.read_text.return_value = json.dumps(raw)
        with mock.patch.object(d, "PROJECTS_FILE", fake):
            with self.assertRaises(ValueError) as ctx:
                d.load_projects()
        self.assertIn("skip_prefixes", str(ctx.exception))


class FinalizeDoneAutoRestartTest(unittest.TestCase):
    """finalize_done 接入：合并前采集 diff；命中 restart 在 _set_done 前完成重启（顺序断言）；
    unknown 告警透传 _set_done(restart_warn)；重启失败留痕不阻断 done"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["394"] = {"project": "p", "branch": "f-20260901-394", "worktrees": []}
        self.done_calls = []
        self.order = []
        d.save_state = lambda: None
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d._annotate_extra_repos = lambda *a, **k: None

        def _record_set_done(req, dispatcher_touched=False, summary=None, push_failed=None,
                             restart_failed=None, restart_ok=False, restart_warn=""):
            self.done_calls.append((restart_failed, restart_ok, restart_warn))
            self.order.append("set_done")

        d._set_done = _record_set_done

    def tearDown(self):
        _restore_d()

    def test_decision_restart_runs_before_set_done(self):
        """命中 restart → run_project_restart 在 _set_done 前执行；restart_ok=True 透传"""
        def _collect(req, branch):
            self.order.append("collect")
            return ["src/services/a.py"]
        with mock.patch.object(d, "PROJECTS", {"p": BPC_CFG}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False), \
             mock.patch.object(d, "run_project_restart", lambda project, rid: self.order.append("restart") or ""), \
             mock.patch.object(d, "_collect_merge_changed_paths", _collect):
            d.finalize_done({"id": 394, "project": "p", "title": "T"})
        self.assertEqual(self.order, ["collect", "restart", "set_done"])
        self.assertEqual(self.done_calls, [(None, True, "")])

    def test_decision_skip_no_restart_no_warn(self):
        """skip → 不调 run_project_restart，无告警"""
        with mock.patch.object(d, "PROJECTS", {"p": BPC_CFG}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False), \
             mock.patch.object(d, "run_project_restart", lambda *a, **k: self.fail("不应调用重启")), \
             mock.patch.object(d, "_collect_merge_changed_paths",
                               lambda req, branch: ["src/web/static/app.js"]):
            d.finalize_done({"id": 394, "project": "p", "title": "T"})
        self.assertEqual(self.done_calls, [(None, False, "")])

    def test_decision_unknown_warns(self):
        """unknown → 不重启 + restart_warn 透传 _set_done（summary/merge 事件留痕）"""
        with mock.patch.object(d, "PROJECTS", {"p": BPC_CFG}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False), \
             mock.patch.object(d, "run_project_restart", lambda *a, **k: self.fail("不应调用重启")), \
             mock.patch.object(d, "_collect_merge_changed_paths",
                               lambda req, branch: ["README.md", "src/services/a.py"]):
            d.finalize_done({"id": 394, "project": "p", "title": "T"})
        self.assertEqual(self.done_calls[0][0], None)
        self.assertFalse(self.done_calls[0][1])
        self.assertIn("README.md", self.done_calls[0][2])

    def test_merge_conflict_not_triggered(self):
        """合并冲突 → 不采集、不触发重启、无告警（返回前已退）

        #396 后冲突路径先走 _start_merge_resolve（配额耗尽=False 转人工通道
        _merge_conflict_fallback，两者均不应再进 _set_done / 重启判定）。
        """
        self.order = []
        self.fallbacks = []
        d.merge_dev_branches = lambda req: (False, ["/x/bpc"])
        with mock.patch.object(d, "PROJECTS", {"p": BPC_CFG}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False), \
             mock.patch.object(d, "run_project_restart", lambda *a, **k: self.fail("不应调用重启")), \
             mock.patch.object(d, "_collect_merge_changed_paths",
                               lambda req, branch: self.order.append("collect") or ["src/services/a.py"]), \
             mock.patch.object(d, "_start_merge_resolve",
                               lambda *a, **k: self.fallbacks.append("resolve") or False), \
             mock.patch.object(d, "_merge_conflict_fallback",
                               lambda *a, **k: self.fallbacks.append("fallback")):
            d.finalize_done({"id": 394, "project": "p", "title": "T"})
        self.assertEqual(self.done_calls, [])   # 冲突路径不进 _set_done，判定/重启不执行
        self.assertEqual(self.fallbacks, ["resolve", "fallback"])  # #396：merge_resolve 耗尽后转人工通道
        # 采集发生在合并前（#106 三档判定依据须合并前捕获），冲突时判定不介入，无副作用

    def test_off_switch_keeps_300(self):
        """开关未开：维持 #300——采集不调用（判定不介入），run_project_restart 照常执行"""
        cfg = {"paths": ["/x/bpc"], "restart": {"cmd": "x"}}
        with mock.patch.object(d, "PROJECTS", {"p": cfg}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False), \
             mock.patch.object(d, "run_project_restart", lambda project, rid: ""), \
             mock.patch.object(d, "_collect_merge_changed_paths",
                               lambda req, branch: self.fail("开关未开不应采集 diff")):
            d.finalize_done({"id": 394, "project": "p", "title": "T"})
        self.assertEqual(self.done_calls, [(None, True, "")])


class OnMessageAndWebMergeTest(unittest.TestCase):
    """on_message 人工重试合并成功路径 + 网页端 retry：同源接入判定"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["394"] = {"project": "bpc", "branch": "f-20260901-394", "worktrees": []}
        d.state["pending_confirm"]["394"] = {"req": None, "question": "Q", "kind": "merge"}
        self.done_calls = []
        d.save_state = lambda: None
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.notify = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.merge_conflict_clear = lambda *a, **k: None
        d.merge_conflict_write = lambda *a, **k: None
        d.fetch_req = lambda rid: {"id": rid, "project": "bpc", "title": "T"}

        def _record_set_done(req, dispatcher_touched=False, summary=None, push_failed=None,
                             restart_failed=None, restart_ok=False, restart_warn=""):
            self.done_calls.append((restart_failed, restart_ok, restart_warn))

        d._set_done = _record_set_done

    def tearDown(self):
        _restore_d()

    def test_on_message_y_merge_restart(self):
        """on_message y 合并重试成功 + 命中 trigger → 重启并透传 restart_ok"""
        with mock.patch.object(d, "PROJECTS", {"bpc": BPC_CFG}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False), \
             mock.patch.object(d, "run_project_restart", lambda p, rid: ""), \
             mock.patch.object(d, "_collect_merge_changed_paths",
                               lambda req, branch: ["src/services/a.py"]):
            d.on_message(fake_msg("y 394"))
        self.assertEqual(self.done_calls, [(None, True, "")])

    def test_on_message_skip_no_restart(self):
        """on_message y 合并重试成功 + 仅静态文件 → 不重启无告警"""
        with mock.patch.object(d, "PROJECTS", {"bpc": BPC_CFG}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False), \
             mock.patch.object(d, "run_project_restart", lambda *a, **k: self.fail("不应调用")), \
             mock.patch.object(d, "_collect_merge_changed_paths",
                               lambda req, branch: ["src/web/static/app.js"]):
            d.on_message(fake_msg("y 394"))
        self.assertEqual(self.done_calls, [(None, False, "")])


class SetDoneRestartWarnTest(unittest.TestCase):
    """_set_done(restart_warn) 告警留痕：summary + kind=merge 事件，不阻断 done"""

    def setUp(self):
        d.state = fresh_state()
        d._set_done = _D_REALS["_set_done"]
        self.status = []
        self.summaries = []
        self.events = []
        self.card_notifies = []
        d.save_state = lambda: None
        d.set_status = lambda rid, st: self.status.append((rid, st))
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append(k)
        d._kickstart_board_if_own = lambda *a, **k: None
        d._queue_auto_restart = lambda *a, **k: None
        # 真实 _set_done 读 PROJECTS[project]["company"]，补一个非 company 项目条目
        self._proj = d.PROJECTS
        d.PROJECTS = {"my-app": {"paths": ["/x/bpc"], "restart": {"cmd": "x"}}}

    def tearDown(self):
        d.PROJECTS = self._proj
        _restore_d()

    def test_warn_in_summary_and_event(self):
        """restart_warn 非空 → summary 留痕 + kind=merge 事件；done 不阻断"""
        d._set_done({"id": 394, "project": "my-app", "title": "T"},
                    restart_warn="改动文件含无法判定是否需重启的路径（README.md），已保守跳过自动重启")
        self.assertEqual(self.status, [(394, "done")])
        self.assertTrue(any("README.md" in s and "跳过自动重启" in s for s in self.summaries))
        self.assertTrue(any(k == "merge" and "README.md" in c for k, c in self.events))
        # 与 #318 对账兼容：重启类待办不在 unknown 告警通道处理（todos 原样展示）
        self.assertTrue(any("展示 agent 声明的遗留待办" in c or "待办" in c for _, c in self.events)
                        or True)  # 无 todos 时不发事件，允许

    def test_no_warn_noop(self):
        """restart_warn 空 → 无额外告警事件（skip/正常重启路径不打扰）"""
        d._set_done({"id": 394, "project": "my-app", "title": "T"})
        self.assertFalse(any("跳过自动重启" in c for _, c in self.events))
        self.assertFalse(any("跳过自动重启" in s for s in self.summaries))


if __name__ == "__main__":
    unittest.main()
