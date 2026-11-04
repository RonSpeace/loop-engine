#!/usr/bin/env python3
"""req-388 重启对账「采纳合法结果」：reconcile_adopt_results 单测。

现场（#386/#387，2026-08-30）：analysis 产物（analysis.md + result.json）已合法落盘，
但回合收尾丢失（调度器 23:03:44 自动重启杀 run_agent 线程，finally/handle_result 不执行），
重启后 reconcile_startup 把 doing 退 open 重跑整轮——合法结果被丢弃 + doing/open 状态
闪变两次 + agent_sessions 残留 running 僵尸（#279 现场复刻）。

修复（#388，仅 dispatcher.py）：
- reconcile_adopt_results 在 reconcile_startup 之前执行：doing 卡 + result.json 存在且
  JSON 合法 + status ∈ {done, blocked, need_confirm} → 直接 handle_result 采纳推进
  （不退回 open、不重跑）；缺失/非法/非收尾状态不动（reconcile_startup 退 open 行为不变）；
- 采纳后把本需求 role 的 running 会话行标 idle（中间栏无僵尸）；
- handle_result 解析成功后 unlink result.json 天然防重复采纳。

本测试为 L0 单测（mock 副作用，沿用 tests/test_dispatcher_reconcile_startup.py 风格）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_reconcile_adopt -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import shutil
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


class L0ReconcileAdopt(unittest.TestCase):
    """L0 单测：mock 副作用，校验采纳合法结果 / 缺失退回 / 非法跳过 / pending 保护 / 降级"""

    # 本类会替换的 dispatcher 模块属性，tearDown 统一恢复（防污染同进程后续测试）
    _MUTATED = ("state", "PROJECTS", "get_doing_reqs", "handle_result", "post_dispatcher_event",
                "api_board", "log", "artifact_dir", "artifact_ok", "set_status")

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in self._MUTATED}
        d.state = {"pending_confirm": {}, "pending_route": {}, "pending_review": {}}
        d.PROJECTS = {"req-board": str(REPO)}
        d.log = mock.Mock()
        self.art = Path(tempfile.mkdtemp(prefix="req388-adopt-art-"))
        d.artifact_dir = lambda req: self.art
        # #388（审查问题 B）：产物契约预检 mock 成通过，各用例按需替换（见问题 B 用例）
        d.artifact_ok = lambda req, role: (True, "")
        self.results = []  # handle_result 调用记录 (req, role, returncode, timed_out, started_at)
        self.posted = []   # post_dispatcher_event 调用记录 (req_id, content, kwargs)
        self.session_posts = []  # api_board POST 记录
        self.session_rows = []   # GET /agent-sessions 返回
        d.handle_result = lambda req, role, returncode, timed_out, started_at: self.results.append(
            (req, role, returncode, timed_out, started_at))
        d.post_dispatcher_event = lambda rid, content, **kw: self.posted.append((rid, content, kw))

        def fake_board(method, path, body=None):
            if method == "POST":
                self.session_posts.append((path, body))
                return {"id": body["id"]}
            return self.session_rows

        d.api_board = fake_board
        d.get_doing_reqs = lambda project: self._doing()

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)
        d._adopted_rids.clear()  # 模块级采纳记录防跨用例污染
        shutil.rmtree(self.art, ignore_errors=True)
        self.session_rows = []

    def _doing(self, rid=388, project="req-board", status="doing"):
        return [{"id": rid, "project": project, "status": status, "phase": None, "notes": ""}]

    def _write_result(self, payload):
        (self.art / "result.json").write_text(payload if isinstance(payload, str)
                                              else json.dumps(payload))

    # ---- 验收 1：running 僵尸 + 合法 result.json → 直接采纳，不重跑 ----
    def test_valid_result_adopted_without_rollback(self):
        """doing 卡 + 合法 result.json → handle_result 采纳（rc=0/timed_out=False），
        不调用 set_status 退 open（reconcile_startup 不回退 = 不重跑）；running 行标 idle"""
        req = self._doing()[0]
        self._write_result({"status": "done", "summary": "分析完成", "suggested_mode": "auto"})
        self.session_rows = [{"id": 901, "req_id": 388, "role": "analysis",
                              "session_id": "session_adopt", "status": "running"}]
        d.reconcile_adopt_results()
        self.assertEqual(len(self.results), 1)
        got_req, role, rc, tout, started = self.results[0]
        self.assertEqual(role, "analysis")
        self.assertEqual(rc, 0)
        self.assertFalse(tout)
        self.assertIsNotNone(started)
        # 未退 open：无 set_status 调用（handle_result 被 mock，不推进状态即「未回退」语义）
        # ；且 running 会话行被标 idle（#421：走 _close_session_row，带 req_id/role + turn_step=''）
        self.assertEqual(self.session_posts,
                         [("/agent-sessions", {"req_id": 388, "role": "analysis", "id": 901,
                                               "status": "idle", "turn_step": ""})])
        # 采纳事件留痕
        self.assertEqual([c for _, c, _ in self.posted],
                         ["调度器重启，采纳上次合法收尾结果（需求分析，不重跑）"])
        self.assertTrue((self.art / "result.json").exists())  # mock handle_result 不 unlink，语义由实现保证

    def test_missing_result_not_adopted(self):
        """doing 卡 + 无 result.json → adopt 不动作（handle_result 不被调用），
        退回 open 重跑的规则保留 TODO（由 reconcile_startup 回归测试覆盖）"""
        d.reconcile_adopt_results()
        self.assertEqual(self.results, [])
        self.assertEqual(self.posted, [])
        self.assertEqual(self.session_posts, [])

    # ---- 非法/非契约值：不采纳（保留现场，交退 open）----
    def test_invalid_json_skipped(self):
        """result.json 非法 JSON → 跳过采纳（handle_result 不调用，.invalid 备份留给正常路径）"""
        self._write_result('{"status": "done", "summary": "坏\n换行"}')
        d.reconcile_adopt_results()
        self.assertEqual(self.results, [])
        self.assertTrue((self.art / "result.json").exists())  # 未动原文件（保留现场）

    def test_non_terminal_status_skipped(self):
        """status 非 done/blocked/need_confirm（如 {"status":"xxx"}）→ 跳过采纳（防启动期
        handle_result 失败通道 sleep 阻塞）"""
        self._write_result({"status": "in_progress", "summary": "未结束"})
        d.reconcile_adopt_results()
        self.assertEqual(self.results, [])

    def test_adopt_uses_running_row_role_for_review(self):
        """#388 问题 3：auto 通道 review 会话残留（行 role=review）→ 采纳角色取行 role
        （role_for 恒返回 dev 会错配，正确归因应走 review 分支 finalize_done）"""
        self._write_result({"status": "done", "summary": "审查通过", "verdict": "pass"})
        self.session_rows = [{"id": 910, "req_id": 388, "role": "review",
                              "session_id": "session_review_z", "status": "running"}]
        d.reconcile_adopt_results()
        self.assertEqual([r[1] for r in self.results], ["review"])
        # 采纳后该 review 行被标 idle（#421：走 _close_session_row，带 req_id/role + turn_step=''）
        self.assertEqual(self.session_posts,
                         [("/agent-sessions", {"req_id": 388, "role": "review", "id": 910,
                                               "status": "idle", "turn_step": ""})])

    # ---- #388（审查问题 A）：采纳后 reconcile_startup 不闪变退 open ----
    def test_adopt_prevents_startup_rollback(self):
        """adopt 采纳的 done 卡（免确认 auto 直接进 dev，status 保持 doing）→ reconcile_startup
        不再退 open（消除 doing/open 闪变 + 伪事件，验收标准 1）；未采纳的 doing 卡维持退 open"""
        self._write_result({"status": "done", "summary": "ok", "suggested_mode": "auto"})
        calls = []
        d.set_status = lambda rid, status: calls.append((rid, status))
        d.reconcile_adopt_results()
        d.reconcile_startup()
        self.assertEqual(calls, [])  # 已采纳卡不退 open
        # 无结果的 doing 卡：adopt 不采纳 → reconcile_startup 维持退 open（回归不变）
        d.get_doing_reqs = lambda project: [{"id": 389, "project": "req-board", "status": "doing",
                                             "phase": None, "notes": ""}]
        d.reconcile_startup()
        self.assertEqual(calls, [(389, "open")])

    # ---- #388（审查问题 B）：status=done 但产物契约不满足 → 不采纳 ----
    def test_done_but_artifact_invalid_not_adopted(self):
        """status=done 但 artifact_ok 失败（缺 analysis.md）→ 预检不采纳，交 reconcile_startup
        退 open 重跑（避免 adopt 把失败通道 sleep+spawn 带进启动序列）"""
        self._write_result({"status": "done", "summary": "ok", "suggested_mode": "auto"})
        d.artifact_ok = lambda req, role: (False, "缺少产物 analysis.md")
        d.reconcile_adopt_results()
        self.assertEqual(self.results, [])

    def test_done_but_missing_artifact_not_adopted(self):
        """#419：status=done 但角色产物缺失 → 预检不采纳（与 handle_result valid_done 判定一致：
        artifact/review/前置槽位节点要求产物非空；suggested_mode 契约已退役）"""
        self.art.joinpath("analysis.md").unlink(missing_ok=True)
        d.artifact_ok = lambda req, role: (False, "缺少产物 analysis.md")
        self._write_result({"status": "done", "summary": "ok"})
        d.reconcile_adopt_results()
        self.assertEqual(self.results, [])

    def test_done_with_artifact_adopted(self):
        """#419：analysis done + 产物在 → 预检放行，采纳进 handle_result（推荐流程缺失按默认兜底）"""
        self._write_result({"status": "done", "summary": "已实现"})
        d.reconcile_adopt_results()
        self.assertEqual([r[1] for r in self.results], ["analysis"])

    def test_blocked_and_need_confirm_adopted(self):
        """blocked/need_confirm 契约值同样采纳（语义 = 上次收尾结果合法）"""
        self._write_result({"status": "blocked", "summary": "依赖外部修复"})
        d.reconcile_adopt_results()
        self.assertEqual([r[1] for r in self.results], ["analysis"])
        self.results.clear()
        self._write_result({"status": "need_confirm", "summary": "", "question": "选哪个？"})
        d.reconcile_adopt_results()
        self.assertEqual([r[1] for r in self.results], ["analysis"])

    # ---- pending 保护：与 reconcile_startup 一致的保守边界 ----
    def test_pending_protected_card_not_adopted(self):
        """pending_confirm/route/review 中的 doing 卡跳过（不采纳、无事件）"""
        for key in ("pending_confirm", "pending_route", "pending_review"):
            d.state[key] = {"388": True}
        self._write_result({"status": "done", "summary": "ok", "suggested_mode": "auto"})
        d.reconcile_adopt_results()
        self.assertEqual(self.results, [])
        self.assertEqual(self.posted, [])

    # ---- 降级：看板不可达 / 采纳抛异常不崩 ----
    def test_board_unreachable_skips_without_crash(self):
        """get_doing_reqs 抛异常（看板不可达）→ 该项目跳过，不崩不误写"""
        def flaky(project):
            raise RuntimeError("board down")
        d.get_doing_reqs = flaky
        d.reconcile_adopt_results()  # 不抛异常即通过
        self.assertEqual(self.results, [])

    def test_handle_result_failure_degrades_to_next_card(self):
        """单卡 handle_result 抛异常（看板瞬时不可达）→ 该卡跳过不崩，其余卡继续"""
        d.get_doing_reqs = lambda project: [{"id": 388, "project": "req-board", "status": "doing"},
                                            {"id": 389, "project": "req-board", "status": "doing"}]
        d.artifact_dir = lambda req: self.art / f"req-{req['id']}"
        for rid in (388, 389):
            (self.art / f"req-{rid}").mkdir()
            (self.art / f"req-{rid}" / "result.json").write_text(
                json.dumps({"status": "done", "summary": "ok", "suggested_mode": "auto"}))

        def flaky_handle(req, role, returncode, timed_out, started_at):
            self._flaky_called.append(req["id"])
            if req["id"] == 388:
                raise RuntimeError("board down")
            self.results.append((req, role, returncode, timed_out, started_at))
        self._flaky_called = []
        d.handle_result = flaky_handle
        d.reconcile_adopt_results()  # 不抛异常即通过
        # 388 确实进入 handle_result 分支（而非「无文件跳过」）——修审查问题 C：388 的
        # result.json 此前漏写导致 flaky 分支不可达，断言 [389] 只验证了「无文件跳过」路径
        self.assertEqual(self._flaky_called, [388, 389])
        self.assertEqual([r[0]["id"] for r in self.results], [389])

    def test_mark_rows_idle_failure_degrades(self):
        """采纳后 running 行置 idle 失败（看板瞬挂）→ 仅日志，不阻断采纳结果与事件
        （#421：写入走 _close_session_row → sync_agent_session，失败告警由后者统一发出）"""
        self._write_result({"status": "done", "summary": "ok", "suggested_mode": "auto"})
        self.session_rows = [{"id": 901, "req_id": 388, "role": "analysis",
                              "session_id": "s1", "status": "running"}]

        def flaky_board(method, path, body=None):
            if method == "POST":
                raise RuntimeError("board down")
            return self.session_rows
        d.api_board = flaky_board
        d.reconcile_adopt_results()
        self.assertEqual([r[1] for r in self.results], ["analysis"])  # 采纳本身成功
        self.assertEqual([c for _, c, _ in self.posted],
                         ["调度器重启，采纳上次合法收尾结果（需求分析，不重跑）"])
        self.assertEqual(len([m for m in d.log.warning.call_args_list
                              if "agent_sessions 同步失败" in m[0][0]]), 1)


if __name__ == "__main__":
    unittest.main()
