#!/usr/bin/env python3
"""req-300 done 后自动重启项目本地服务：projects.json restart 配方测试。

改动：load_projects 透传 restart 字段（cmd/verify/timeout，畸形配置降级 None）；
新增 run_project_restart——本地合并成功后按配方执行 cmd（shell、cwd=项目首个仓库路径、
带执行超时）并按 verify 轮询就绪，成功发 kind=merge 事件、失败返回原因字符串；
_set_done 新增 restart_failed 参数（summary + 时间线 kind=merge + done 卡片红字三处留痕，
不阻断 done）；finalize_done 个人合并成功路径与 on_message 人工合并重试成功路径均接入；
company 项目不本地合并、天然不触发。

四类验收用例：配置命中（cmd 执行 + verify 轮询 + 成功事件 + _set_done 正常推进）、
命令失败/verify 超时（done 不阻断 + 三处留痕）、无配置（行为与现状一致）、
company 跳过（不执行 restart）。

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

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

# 各测试类会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "_kickstart_board_if_own", "merge_dev_branches",
    "cleanup_dev_worktrees", "push_company_branches", "fetch_req", "_set_done",
    "_wait_url_up", "run_project_restart")}
_REAL_SUBPROCESS_RUN = d.subprocess.run
_REAL_LOG = d.log


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)
    d.subprocess.run = _REAL_SUBPROCESS_RUN
    d.log = _REAL_LOG


def fresh_state():
    return {"chat_id": "oc_req300_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


def fake_msg(text):
    """构造 on_message 入参（仅用 message_type/content/chat_id 三字段）"""
    content = json.dumps({"text": text}) if text else "{}"
    return types.SimpleNamespace(event=types.SimpleNamespace(
        message=types.SimpleNamespace(message_type="text", content=content, chat_id="oc_req300_test")))


class RunProjectRestartTest(unittest.TestCase):
    """run_project_restart：配置命中（cmd 执行 + verify 轮询 + 成功事件）、
    失败（非零退出/超时/verify 超时）返回原因、无配置 no-op"""

    RESTART = {"cmd": "./start_dev.sh", "verify": "http://127.0.0.1:9999/health", "timeout": 30}

    def setUp(self):
        self.events = []   # post_dispatcher_event 记录 (rid, kind, content)
        self.warnings = []
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((rid, kind, content))
        d.log = mock.Mock()
        d.log.info = lambda *a, **k: None
        d.log.warning = lambda *a, **k: self.warnings.append(a[0])

    def tearDown(self):
        _restore_d()

    def _pj(self, restart="__KEEP__"):
        """PROJECTS mock；restart="__KEEP__" 用默认配方，None 表示未配置"""
        cfg = {"paths": ["/x/repo"]}
        if restart != "__KEEP__":
            cfg["restart"] = restart
        elif restart is not None:
            cfg["restart"] = self.RESTART
        return mock.patch.object(d, "PROJECTS", {"p": cfg})

    def test_t1_success_with_verify(self):
        """验收：配置命中 → cmd 按配方执行（shell、cwd=项目路径、超时），verify 轮询，成功发 merge 事件"""
        r = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with self._pj(), mock.patch.object(d.subprocess, "run", return_value=r) as run, \
             mock.patch.object(d, "_wait_url_up", return_value=True) as wait:
            reason = d.run_project_restart("p", 300)
        self.assertEqual(reason, "")
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], "./start_dev.sh")
        self.assertEqual(kwargs["cwd"], "/x/repo")
        self.assertTrue(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 30)
        wait.assert_called_once_with("http://127.0.0.1:9999/health", timeout=30)
        self.assertEqual(len(self.events), 1)
        rid, kind, content = self.events[0]
        self.assertEqual((rid, kind), (300, "merge"))
        self.assertIn("已自动重启项目服务", content)
        self.assertIn("就绪校验通过", content)

    def test_t2_success_without_verify(self):
        """未配 verify：cmd 成功即视为就绪，不发就绪校验文案、不轮询"""
        cfg = {"paths": ["/x/repo"], "restart": {"cmd": "echo ok"}}
        r = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(d, "PROJECTS", {"p": cfg}), \
             mock.patch.object(d.subprocess, "run", return_value=r), \
             mock.patch.object(d, "_wait_url_up") as wait:
            reason = d.run_project_restart("p", 300)
        self.assertEqual(reason, "")
        wait.assert_not_called()
        self.assertEqual(len(self.events), 1)
        self.assertNotIn("就绪校验", self.events[0][2])

    def test_t3_cmd_nonzero_exit(self):
        """cmd 非零退出（不抛异常）→ 返回失败原因，不抛、不发成功事件"""
        r = mock.Mock(returncode=3, stdout=b"", stderr=b"service not found\n")
        with self._pj(), mock.patch.object(d.subprocess, "run", return_value=r), \
             mock.patch.object(d, "_wait_url_up") as wait:
            reason = d.run_project_restart("p", 300)
        self.assertIn("退出码 3", reason)
        self.assertIn("service not found", reason)
        wait.assert_not_called()
        self.assertEqual(self.events, [])

    def test_t4_cmd_timeout(self):
        """cmd 执行超时（常驻脚本挂起）→ 返回超时原因（不阻塞调度器主循环）"""
        with self._pj(), \
             mock.patch.object(d.subprocess, "run",
                               side_effect=d.subprocess.TimeoutExpired("./start_dev.sh", 30)):
            reason = d.run_project_restart("p", 300)
        self.assertIn("超时", reason)
        self.assertIn("30", reason)
        self.assertEqual(self.events, [])

    def test_t5_cmd_exception(self):
        """cmd 执行抛异常 → 降级返回异常原因"""
        with self._pj(), mock.patch.object(d.subprocess, "run", side_effect=OSError("no exec")):
            reason = d.run_project_restart("p", 300)
        self.assertIn("no exec", reason)
        self.assertEqual(self.events, [])

    def test_t6_verify_timeout(self):
        """cmd 成功但 verify 超时未就绪 → 返回未就绪原因"""
        r = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with self._pj(), mock.patch.object(d.subprocess, "run", return_value=r), \
             mock.patch.object(d, "_wait_url_up", return_value=False):
            reason = d.run_project_restart("p", 300)
        self.assertIn("未就绪", reason)
        self.assertIn("9999", reason)
        self.assertEqual(self.events, [])

    def test_t7_no_config_noop(self):
        """无 restart 配置 → 返回 ""，不执行任何命令、不发事件（与现状一致）"""
        with self._pj(restart=None), mock.patch.object(d.subprocess, "run") as run, \
             mock.patch.object(d, "_wait_url_up") as wait:
            reason = d.run_project_restart("p", 300)
        self.assertEqual(reason, "")
        run.assert_not_called()
        wait.assert_not_called()
        self.assertEqual(self.events, [])

    def test_t8_malformed_config_treated_as_none(self):
        """load_projects 归一化：restart 非 dict / 缺 cmd → None（helper 层再兜底一次）"""
        with self._pj(restart={}), mock.patch.object(d.subprocess, "run") as run:
            reason = d.run_project_restart("p", 300)
        self.assertEqual(reason, "")
        run.assert_not_called()
        self.assertEqual(self.events, [])


class SetDoneRestartFailedTest(unittest.TestCase):
    """#300：_set_done(restart_failed) 三处留痕（summary + 时间线 kind=merge + 卡片红字），
    done 不阻断；未失败无提醒（与旧行为一致）"""

    def setUp(self):
        d.state = fresh_state()
        d._set_done = _D_REALS["_set_done"]   # 恢复真实函数
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

    def tearDown(self):
        _restore_d()

    def test_restart_failed_three_places(self):
        """restart 失败：summary + 时间线（kind=merge）+ 卡片红字提醒手动重启；done 状态照常推进"""
        d._set_done({"id": 300, "project": "req-board", "title": "T"}, restart_failed="重启命令执行失败（退出码 3）")
        self.assertEqual(self.status, [(300, "done")])   # 失败不阻断 done
        self.assertTrue(any("自动重启失败" in s and "退出码 3" in s and "需人工重启" in s for s in self.summaries))
        self.assertTrue(any(k == "merge" and "自动重启失败" in c and "退出码 3" in c and "需人工重启" in c
                            for k, c in self.events))
        self.assertEqual(len(self.card_notifies), 1)
        kw = self.card_notifies[0]
        self.assertIn("自动重启失败", kw["reminder"])
        self.assertIn("需人工重启", kw["reminder"])
        self.assertIn("退出码 3", kw["reminder"])

    def test_restart_failed_combines_with_dispatcher_reminder(self):
        """restart 失败 + dispatcher 命中：卡片同时含两条提醒（互不覆盖）"""
        d._set_done({"id": 300, "project": "req-board", "title": "T"},
                    dispatcher_touched=True, restart_failed="超时")
        kw = self.card_notifies[0]
        self.assertIn("自动重启", kw["reminder"])
        self.assertIn("需人工重启", kw["reminder"])
        self.assertLess(kw["reminder"].index("自动重启"), kw["reminder"].index("需人工重启"))

    def test_restart_ok_no_reminder(self):
        """restart 成功（restart_failed=None）→ 无额外提醒，与旧行为一致"""
        d._set_done({"id": 300, "project": "req-board", "title": "T"})
        self.assertEqual(self.card_notifies[0]["reminder"], "")
        self.assertEqual(self.summaries, [])
        self.assertEqual(self.events, [])


class FinalizeDoneRestartTest(unittest.TestCase):
    """finalize_done 接入：本地合并项目合并成功 → 触发 restart 配方，失败原因透传 _set_done；
    company 项目不触发"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["300"] = {"project": "p", "branch": "f-20260813-300", "worktrees": []}
        self.done_calls = []      # _set_done 收到的 (dispatcher_touched, restart_failed)
        self.restart_calls = []   # run_project_restart 收到的 (project, req_id)
        d.save_state = lambda: None
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.push_company_branches = lambda req, branch, repos: set()
        d._annotate_extra_repos = lambda *a, **k: None

        def _record_restart(project, req_id):
            self.restart_calls.append((project, req_id))
            return "boom reason"

        d.run_project_restart = _record_restart

        def _record_set_done(req, dispatcher_touched=False, summary=None, push_failed=None, restart_failed=None,
                             restart_ok=False, restart_warn=""):
            self.done_calls.append((dispatcher_touched, restart_failed, restart_ok))

        d._set_done = _record_set_done

    def tearDown(self):
        _restore_d()

    def test_personal_merge_success_triggers_restart(self):
        """本地合并项目合并成功 → run_project_restart(project, req_id)，失败原因透传 _set_done"""
        with mock.patch.object(d, "PROJECTS", {"p": {"paths": ["/x/repo"]}}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False):
            d.finalize_done({"id": 300, "project": "p", "title": "T"})
        self.assertEqual(self.restart_calls, [("p", "300")])
        self.assertEqual(self.done_calls, [(False, "boom reason", False)])

    def test_company_skips_restart(self):
        """company 项目（不本地合并）：不触发 restart 配方"""
        with mock.patch.object(d, "PROJECTS", {"p": {"paths": ["/x/repo"], "company": True,
                                                     "confirm": True}}), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False):
            d.finalize_done({"id": 300, "project": "p", "title": "T"})
        self.assertEqual(self.restart_calls, [])
        self.assertEqual(self.done_calls, [(False, None, False)])


class OnMessageMergeRetryRestartTest(unittest.TestCase):
    """on_message 人工合并重试成功：同样触发 restart 配方（与 finalize_done 一致）"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["300"] = {"project": "p", "branch": "f-20260813-300", "worktrees": []}
        d.state["pending_confirm"]["300"] = {"req": None, "question": "Q", "kind": "merge"}
        self.done_calls = []
        self.restart_calls = []
        d.save_state = lambda: None
        d.notify = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.fetch_req = lambda rid: {"id": rid, "project": "p", "title": "T"}
        d.run_project_restart = lambda project, req_id: self.restart_calls.append((project, req_id)) or ""
        d._set_done = lambda req, dispatcher_touched=False, summary=None, push_failed=None, restart_failed=None, \
            restart_ok=False, restart_warn="": self.done_calls.append((dispatcher_touched, restart_failed, restart_ok))

    def tearDown(self):
        _restore_d()

    def test_merge_retry_success_triggers_restart(self):
        """人工合并重试成功 → restart 触发，结果透传 _set_done"""
        with mock.patch.object(d, "_touches_dispatcher", return_value=False):
            d.on_message(fake_msg("y 300"))
        self.assertEqual(self.restart_calls, [("p", 300)])
        self.assertEqual(self.done_calls, [(False, None, False)])


class LoadProjectsRestartNormalizeTest(unittest.TestCase):
    """load_projects：restart 字段透传归一化（cmd 必填，timeout 默认 30、非法降级），
    非 dict/缺 cmd 视为未配置"""

    def test_normalize(self):
        raw = {"a": {"paths": ["/x/a"], "restart": {"cmd": " ./go.sh ", "verify": " http://x "}},
               "b": {"paths": ["/x/b"], "restart": {"cmd": "go", "timeout": "10"}},
               "c": {"paths": ["/x/c"], "restart": {"verify": "http://x"}},
               "d": {"paths": ["/x/d"], "restart": "not-a-dict"},
               "e": "/x/e"}
        fake_file = mock.Mock()
        fake_file.read_text.return_value = json.dumps(raw)
        with mock.patch.object(d, "PROJECTS_FILE", fake_file):
            out = d.load_projects()
        self.assertEqual(out["a"]["restart"],
                         {"cmd": "./go.sh", "verify": "http://x", "timeout": 30})
        self.assertEqual(out["b"]["restart"],
                         {"cmd": "go", "verify": "", "timeout": 10})
        self.assertIsNone(out["c"]["restart"])   # 缺 cmd → 未配置
        self.assertIsNone(out["d"]["restart"])   # 非 dict → 未配置
        self.assertIsNone(out["e"]["restart"])   # 未配置


class ReconcileRestartTodosTest(unittest.TestCase):
    """#318：_reconcile_restart_todos 纯函数——纯重启条目剔除、混合条目标注、
    人工条目保留（restart 配方已成功执行时的对账规则）"""

    def test_pure_restart_dropped(self):
        """纯重启条目（重启 5002 服务 / 英文 restart）→ 剔除"""
        self.assertEqual(d._reconcile_restart_todos(["重启 5002 服务", "restart web server"]), [])

    def test_human_todo_kept(self):
        """非重启条目（浏览器验收等纯人工事项）→ 原样保留"""
        self.assertEqual(d._reconcile_restart_todos(["浏览器验收首页对齐"]), ["浏览器验收首页对齐"])

    def test_mixed_annotated(self):
        """混合条目「重启 5002 服务后浏览器验收」→ 剥掉重启片段，标注后保留人工部分"""
        self.assertEqual(d._reconcile_restart_todos(["重启 5002 服务后浏览器验收"]),
                         ["浏览器验收（服务已由调度器自动重启）"])

    def test_mixed_parenthetical_annotated(self):
        """#317 现场：重启片段带括号说明（pkill/launchd）再接人工动作 → 标注保留人工部分"""
        self.assertEqual(
            d._reconcile_restart_todos(
                ["重启 com.example.web Web 服务（pkill -f src.web，launchd 自动拉起）后浏览器验收对齐"]),
            ["浏览器验收对齐（服务已由调度器自动重启）"])

    def test_restart_method_remainder_dropped(self):
        """重启片段剔除后剩余仅为重启方式说明（括号/pkill 等）→ 仍视为纯重启剔除"""
        self.assertEqual(
            d._reconcile_restart_todos(
                ["重启 com.example.web Web 服务（pkill -f src.web，launchd 自动拉起）"]),
            [])

    def test_input_unchanged(self):
        """不修改入参"""
        todos = ["重启 5002 服务", "浏览器验收"]
        d._reconcile_restart_todos(todos)
        self.assertEqual(todos, ["重启 5002 服务", "浏览器验收"])


class SetDoneRestartTodosReconcileTest(unittest.TestCase):
    """#318：_set_done(restart_ok=True) 对账——summary 留档与卡片展示一致（过滤后）；
    restart_ok=False（未配置/自动重启失败）todos 原样，行为不变"""

    def setUp(self):
        d.state = fresh_state()
        d._set_done = _D_REALS["_set_done"]   # 用真实函数
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

    def tearDown(self):
        _restore_d()

    def test_reconcile_ok_filters_and_annotates(self):
        """验收：restart 已成功 → 纯重启条目剔除、混合条目标注、人工条目保留；
        summary 与卡片一致，不残留「待办：重启…」"""
        d.state["req_todos"] = {"318": [
            "重启 com.example.web Web 服务（pkill -f src.web，launchd 自动拉起）后浏览器验收对齐",
            "重启 5002 服务",
            "浏览器验收首页对齐"]}
        d._set_done({"id": 318, "project": "req-board", "title": "T"}, restart_ok=True)
        self.assertEqual(self.status, [(318, "done")])
        kw = self.card_notifies[0]
        self.assertEqual(kw["todos"], ["浏览器验收对齐（服务已由调度器自动重启）", "浏览器验收首页对齐"])
        self.assertTrue(any("待办：浏览器验收对齐（服务已由调度器自动重启）" in s for s in self.summaries))
        self.assertTrue(any("待办：浏览器验收首页对齐" in s for s in self.summaries))
        self.assertFalse(any("重启 5002" in s for s in self.summaries))   # 纯重启条目不进 summary
        self.assertTrue(any("剔除重启类待办 1 条" in c for _, c in self.events))
        self.assertTrue(any("遗留待办 2 条" in c for _, c in self.events))

    def test_reconcile_all_dropped(self):
        """全部为纯重启条目 → 剔除后卡片无待办、summary 无待办行、不展示「0 条」事件"""
        d.state["req_todos"] = {"318": ["重启 5002 服务", "重启 com.example.web Web 服务"]}
        d._set_done({"id": 318, "project": "req-board", "title": "T"}, restart_ok=True)
        kw = self.card_notifies[0]
        self.assertEqual(kw["todos"], [])
        self.assertEqual(self.summaries, [])
        self.assertTrue(any("剔除重启类待办 2 条" in c for _, c in self.events))
        self.assertFalse(any("展示" in c for _, c in self.events))

    def test_restart_failed_keeps_todos(self):
        """restart 失败（restart_failed 非空、restart_ok 默认 False）→ 重启类待办原样红字（行为不变）"""
        d.state["req_todos"] = {"318": ["重启 5002 服务", "浏览器验收"]}
        d._set_done({"id": 318, "project": "req-board", "title": "T"}, restart_failed="重启命令执行失败（退出码 3）")
        kw = self.card_notifies[0]
        self.assertEqual(kw["todos"], ["重启 5002 服务", "浏览器验收"])
        self.assertTrue(any("待办：重启 5002 服务" in s for s in self.summaries))
        self.assertNotIn("剔除重启类待办", [c for _, c in self.events])


class FinalizeDoneRestartOkTest(unittest.TestCase):
    """#318：finalize_done 个人路径——restart 成功（返回 ""）且配置存在 → restart_ok=True
    透传 _set_done；失败 / 未配置 → False（对账不生效）"""

    def setUp(self):
        d.state = fresh_state()
        d.state["worktrees"]["318"] = {"project": "p", "branch": "f-20260814-318", "worktrees": []}
        self.done_calls = []
        d.save_state = lambda: None
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d._annotate_extra_repos = lambda *a, **k: None

        def _record_set_done(req, dispatcher_touched=False, summary=None, push_failed=None, restart_failed=None,
                             restart_ok=False, restart_warn=""):
            self.done_calls.append((restart_failed, restart_ok))

        d._set_done = _record_set_done

    def tearDown(self):
        _restore_d()

    def test_restart_success_ok_true(self):
        """配方执行成功（返回 ""）→ restart_ok=True（对账生效依据）"""
        with mock.patch.object(d, "PROJECTS", {"p": {"paths": ["/x/repo"], "restart": {"cmd": "go"}}}), \
             mock.patch.object(d, "run_project_restart", return_value=""), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False):
            d.finalize_done({"id": 318, "project": "p", "title": "T"})
        self.assertEqual(self.done_calls, [(None, True)])

    def test_restart_failed_ok_false(self):
        """配方失败（返回原因）→ restart_ok=False（不误标已自动重启）"""
        with mock.patch.object(d, "PROJECTS", {"p": {"paths": ["/x/repo"], "restart": {"cmd": "go"}}}), \
             mock.patch.object(d, "run_project_restart", return_value="退出码 3"), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False):
            d.finalize_done({"id": 318, "project": "p", "title": "T"})
        self.assertEqual(self.done_calls, [("退出码 3", False)])

    def test_no_config_ok_false(self):
        """未配置 restart → restart_ok=False（行为与现状一致）"""
        with mock.patch.object(d, "PROJECTS", {"p": {"paths": ["/x/repo"]}}), \
             mock.patch.object(d, "run_project_restart", return_value=""), \
             mock.patch.object(d, "_touches_dispatcher", return_value=False):
            d.finalize_done({"id": 318, "project": "p", "title": "T"})
        self.assertEqual(self.done_calls, [(None, False)])


if __name__ == "__main__":
    unittest.main()
