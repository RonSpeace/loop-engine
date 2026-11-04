#!/usr/bin/env python3
"""req-105 失败处理优化测试：
1. 阶段成功推进清零重试计数（analysis 失败计数不带到 dev，dev 独立容错额度）
2. 退出码 0 未提交结果：场景化失败原因 + 看板事件带角色 + 续跑 extra 场景化提示
3. 日志隔离：import dispatcher 不写生产日志 /tmp/req_board_dispatcher.log（模块级只挂 stderr）

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
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

PROD_LOG = "/tmp/req_board_dispatcher.log"

# 测试会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "set_req", "append_summary",
    "post_dispatcher_event", "notify", "notify_req_card", "spawn_agent",
    "_spawn_safe", "fetch_req", "has_code_changes", "mark_session_poisoned", "api")}


def fresh_state():
    return {"chat_id": "oc_req105_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


class _Base(unittest.TestCase):
    """公共 mock 环境：产物目录指向临时目录，看板/状态写入全部接管"""

    def setUp(self):
        d.state = fresh_state()
        # 失败路径会 time.sleep(retries*30) 退避（真实等待拖慢测试），统一 patch 掉
        self._sleep = mock.patch.object(d.time, "sleep")
        self._sleep.start()
        self._orig_artifact_dir = d.artifact_dir
        self.art = Path(tempfile.mkdtemp(prefix="req105-art-"))
        d.artifact_dir = lambda req: self.art
        self.status = []
        self.events = []
        self.spawns = []
        self.req_sets = []
        self.summaries = []        # append_summary 记录（rid, line）（#385 验证 blocked 原因落 summary）
        self.card_notifies = []    # notify_req_card 记录（event, kwargs）
        self.poisoned = []         # mark_session_poisoned 记录（#385 验证毒会话标记）
        d.save_state = lambda: None
        d.set_status = lambda rid, st: self.status.append((rid, st))
        d.set_req = lambda rid, fields: self.req_sets.append((rid, fields))
        d.append_summary = lambda rid, line: self.summaries.append((rid, line))
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify = lambda *a, **k: None
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append((event, k))
        d.spawn_agent = lambda req, role=None, extra="", resume=False: self.spawns.append((role, extra, resume))
        d._spawn_safe = lambda req, role, extra="", resume=False: self.spawns.append((role, extra, resume))
        d.fetch_req = lambda rid: {"id": rid, "project": "req-board", "title": "T", "mode": "auto"}
        d.has_code_changes = lambda req, since: True  # dev 成功判定（代码变更检查）
        d.mark_session_poisoned = lambda req_id, role=None: self.poisoned.append((req_id, role))
        # #113 红线归因新增事件查询（GET /<id>/events）：默认返回无红线事件，测试完全离线化
        d.api = lambda method, path, body=None: []

    def tearDown(self):
        d.artifact_dir = self._orig_artifact_dir
        self._sleep.stop()
        for name, value in _D_REALS.items():
            setattr(d, name, value)
        shutil.rmtree(self.art, ignore_errors=True)

    def _req(self, rid=105, mode="auto", phase=None, flow=None):
        req = {"id": rid, "project": "req-board", "title": "T", "mode": mode, "phase": phase}
        if flow:
            req["flow"] = flow
        return req


class StageRetryClearTest(_Base):
    """验收 1：阶段成功推进清零 retries，dev 首次失败显示第 1 次、有独立容错额度"""

    def _live_card(self, **kw):
        """模拟真实看板：set_req 写卡 + fetch_req 回读同一张卡（#419 快照写后回读校验需要）"""
        card = {"id": 105, "project": "req-board", "title": "T", **kw}
        d.fetch_req = lambda rid: card
        d.set_req = lambda rid, fields: (self.req_sets.append((rid, fields)), card.update(fields))
        return card

    def test_analysis_fail_then_success_clears_dev_retry_count(self):
        """analysis 失败 1 次 → analysis 成功（免确认进 dev）→ dev 首次失败显示「第 1 次」"""
        # 1) analysis 首次失败（无 result.json，进程正常退出）：计数 +1（#109 起计数带失败原因）
        d.handle_result(self._req(), "analysis", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(d.state["retries"]["105"]["count"], 1)
        # 2) analysis 成功（推荐 standard 流程，免确认采纳）：retries 清零（analysis 成功需 analysis.md 产物）
        card = self._live_card()
        (self.art / "analysis.md").write_text("ok")
        (self.art / "result.json").write_text(json.dumps(
            {"status": "done", "summary": "ok", "suggested_flow": "standard"}))
        d.handle_result(self._req(mode=None), "analysis",
                        returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(d.state["retries"].get("105", 0), 0)
        self.assertEqual(card["flow"], "standard")       # #419：采纳流程写快照（取代 mode/phase 写入）
        self.assertEqual(card["node_index"], 0)
        self.assertEqual(self.spawns[-1][0], "dev")
        # 3) dev 首次失败：计数从 1 开始（第 1 次），事件带 role
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(d.state["retries"]["105"]["count"], 1)
        self.assertTrue(any("第 1 次" in c and "role=dev" in c for _, c in self.events))

    def test_analysis_adopt_flow_clears_retries(self):
        """#419：analysis 成功采纳流程（写快照 + 拉起首节点）同样清零 retries（原 route 路径退役）"""
        d.state["retries"]["105"] = 1
        card = self._live_card()
        (self.art / "analysis.md").write_text("ok")
        (self.art / "result.json").write_text(json.dumps(
            {"status": "done", "summary": "ok", "suggested_flow": "heavy"}))
        d.handle_result(self._req(mode=None), "analysis",
                        returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(d.state["retries"].get("105", 0), 0)
        self.assertEqual(card["flow"], "heavy")
        self.assertEqual(self.spawns[-1][0], "tech")
        self.assertEqual(self.status, [])

    def test_dev_to_code_review_clears_retries(self):
        """standard dev 成功推进到 code_review 时清零"""
        d.state["retries"]["105"] = 2
        (self.art / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))
        d.handle_result(self._req(mode="semi", phase="dev", flow="standard"), "dev",
                        returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(d.state["retries"].get("105", 0), 0)
        self.assertIn((105, {"node_index": 1}), self.req_sets)   # standard：dev(0) → code_review(1)
        self.assertEqual(self.spawns[-1][0], "code_review")

    def test_feishu_route_confirm_clears_retries(self):
        """飞书通道确认（route y 同意）清零计数后进入流程首节点
        （遗留 route 路径已无写入方：卡无 flow → 兜底默认流程 heavy 首节点 tech）"""
        d.state["retries"]["105"] = 1
        d.state["pending_route"]["105"] = {"req": self._req(), "suggested_mode": "auto"}

        class _Msg:
            message_type = "text"
            chat_id = "oc_x"
            content = json.dumps({"text": "y 105"})

        class _Evt:
            message = _Msg()

        class _Data:
            event = _Evt()

        d.on_message(_Data())
        self.assertEqual(d.state["retries"].get("105", 0), 0)
        self.assertEqual(self.spawns[-1][0], "tech")

    def test_reconcile_route_confirm_clears_retries(self):
        """决策对账：网页端已确认通道（open+mode）直接 spawn 前清零
        （遗留 route 路径已无写入方：卡无 flow → 兜底默认流程 heavy 首节点 tech）"""
        d.state["retries"]["105"] = 1
        d.state["pending_route"]["105"] = {"req": self._req(), "suggested_mode": "auto"}
        d.api = lambda method, path, body=None: [{"id": 105, "project": "req-board",
                                                  "title": "T", "status": "open", "mode": "auto"}]
        d.reconcile_decisions()
        self.assertEqual(d.state["retries"].get("105", 0), 0)
        self.assertEqual(self.spawns[-1][0], "tech")


class ScenarioReasonTest(_Base):
    """验收 2：退出码 0 未提交结果的场景化原因与操作提示"""

    def test_returncode0_no_result_scenario_reason(self):
        """无 result.json + 退出码 0：原因=进程正常退出但未提交结果；extra 含前台验证/补写结果提示"""
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        role, extra, resume = self.spawns[0]
        self.assertTrue(resume)
        self.assertIn("进程正常退出但未提交结果", extra)
        self.assertIn("前台", extra)            # 操作提示：前台跑验证
        self.assertIn("result.json", extra)    # 操作提示：补写结果
        self.assertNotIn("换个思路", extra)
        # 看板事件带场景原因 + 角色
        self.assertTrue(any("进程正常退出但未提交结果" in c and "role=dev" in c for _, c in self.events))

    def test_returncode_nonzero_no_result_reason(self):
        """无 result.json + 异常退出码：原因区分进程异常退出并带退出码"""
        d.handle_result(self._req(), "dev", returncode=-9, timed_out=False, started_at=time.time())
        role, extra, resume = self.spawns[0]
        self.assertIn("进程异常退出", extra)
        self.assertIn("退出码 -9", extra)

    def test_blocked_no_retry_direct_later(self):
        """#385 验收①：blocked（正常退出）→ 不重试、卡片转 later、retries 不增加、通知/事件留痕、毒会话"""
        d.state["retries"]["105"] = {"count": 2, "last_failure": "旧失败"}
        (self.art / "result.json").write_text(json.dumps(
            {"status": "blocked", "summary": "环境依赖装不上"}))
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(self.spawns, [])                                    # 不拉起重试会话
        self.assertEqual(self.status, [(105, "later")])                      # 卡片转 later
        # blocked 不经计数（未 +1 成 count:3），且对齐耗尽路径 pop 旧残留（防 resume_failure_hint 误导续跑）
        self.assertNotIn("105", d.state["retries"])
        self.assertEqual(self.poisoned, [("105", "dev")])                    # 毒会话，人工续跑入口保留
        self.assertIn("blocked", [e for e, _ in self.card_notifies])                             # blocked 卡片通知
        self.assertTrue(any("需求阻塞" in c for _, c in self.events))                            # 事件留痕
        self.assertTrue(any("需求阻塞" in s and "环境依赖装不上" in s for _, s in self.summaries))  # summary 落原因

    def test_blocked_accept_role_no_retry(self):
        """#385 验收③：#376 accept blocked 场景逻辑等价——accept 角色 blocked（正常退出）不再拉起第二会话"""
        (self.art / "result.json").write_text(json.dumps(
            {"status": "blocked", "summary": "dev 改动有缺陷，等待修复"}))
        d.handle_result(self._req(), "accept", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.status, [(105, "later")])

    def test_blocked_timed_out_still_retries(self):
        """#385 验收②回归：blocked 但进程超时 → 仍按进程级失败重试（分流仅限「blocked 且进程正常退出」）"""
        (self.art / "result.json").write_text(json.dumps({"status": "blocked", "summary": "x"}))
        d.handle_result(self._req(), "dev", returncode=0, timed_out=True, started_at=time.time())
        self.assertEqual(len(self.spawns), 1)
        self.assertTrue(self.spawns[0][2])
        self.assertIn("运行超时", self.spawns[0][1])

    def test_blocked_nonzero_exit_still_retries(self):
        """#385 验收②回归：blocked 但进程异常退出（非 0）→ 仍按进程级失败重试（分流仅限「正常退出」）。
        reason 取 summary 优先（既有失败分类行为），重点断言重试被触发"""
        (self.art / "result.json").write_text(json.dumps({"status": "blocked", "summary": "x"}))
        d.handle_result(self._req(), "dev", returncode=-9, timed_out=False, started_at=time.time())
        self.assertEqual(len(self.spawns), 1)
        self.assertTrue(self.spawns[0][2])
        self.assertIn("x", self.spawns[0][1])

    def test_exhausted_event_has_role(self):
        """重试耗尽停止自动调度转 later（#113）：事件带角色"""
        d.state["retries"]["105"] = d.MAX_RETRIES
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(self.status, [(105, "later")])
        self.assertTrue(any("停止自动调度转 later" in c and "role=dev" in c for _, c in self.events))


class HasCodeChangesDiffTest(unittest.TestCase):
    """#113 验收 5：dev done 校验 has_code_changes 改分支相对基线三点 diff（新会话接管旧 worktree
    认可已有未合并提交；无变更仍拒绝 done；多仓库任一有变更即认可）"""

    def _req(self, rid=113):
        return {"id": rid, "project": "req-board", "created_at": "2026-08-07 10:00:00"}

    def _patch_projects(self, paths):
        orig = d.PROJECTS
        d.PROJECTS = {"req-board": {"paths": paths, "confirm": False}}
        self.addCleanup(lambda: setattr(d, "PROJECTS", orig))

    def _run_stub(self, diff_stdout, diff_rc=0):
        def side(cmd, *a, **k):
            if "diff" in cmd:
                return types.SimpleNamespace(returncode=diff_rc, stdout=diff_stdout)
            return types.SimpleNamespace(returncode=0, stdout="main\n")
        return mock.patch.object(d.subprocess, "run", side_effect=side)

    def test_f1_branch_diff_has_files_true(self):
        """F1：mock 三点 diff 输出文件列表 → True；且以「当前分支 HEAD...dev 分支」形式 diff"""
        self._patch_projects(["/repo/a"])
        with self._run_stub("board.py\nacp_shared.py\n") as run:
            self.assertTrue(d.has_code_changes(self._req(), time.time()))
        diff_args = run.call_args_list[1][0][0]
        self.assertIn("diff", diff_args)
        self.assertTrue(diff_args[-1].endswith("...f-20260807-113"), diff_args)

    def test_f2_diff_empty_false(self):
        """F2：diff 无任何文件 → False（没有任何相对基线变更仍拒绝 done）"""
        self._patch_projects(["/repo/a"])
        with self._run_stub(""):
            self.assertFalse(d.has_code_changes(self._req(), time.time()))

    def test_f3_branch_missing_false(self):
        """F3：diff 非零（分支不存在）→ False"""
        self._patch_projects(["/repo/a"])
        with self._run_stub("", diff_rc=128):
            self.assertFalse(d.has_code_changes(self._req(), time.time()))

    def test_f4_multi_repo_any_true(self):
        """F4：多仓库任一有变更 → True（repo/a 空、repo/b 有）"""
        self._patch_projects(["/repo/a", "/repo/b"])
        side_calls = {"n": 0}

        def side(cmd, *a, **k):
            side_calls["n"] += 1
            if "diff" in cmd:
                return types.SimpleNamespace(returncode=0,
                                             stdout="x.py\n" if side_calls["n"] >= 4 else "")
            return types.SimpleNamespace(returncode=0, stdout="main\n")
        with mock.patch.object(d.subprocess, "run", side_effect=side):
            self.assertTrue(d.has_code_changes(self._req(), time.time()))

    def test_f5_normal_dev_session_true(self):
        """F5：正常 dev 会话内提交（三点 diff 非空）→ True（回归）"""
        self._patch_projects(["/repo/a"])
        with self._run_stub("tests/test_x.py\n"):
            self.assertTrue(d.has_code_changes(self._req(), time.time()))


class LogIsolationTest(unittest.TestCase):
    """验收 3：import dispatcher（测试/脚本）不写生产日志 /tmp/req_board_dispatcher.log"""

    def _prod_file_handlers(self, names=(None, "dispatcher", "acp_shared")):
        """#11：遍历口径含 acp_shared logger——_mount_file_logger 会把同一 FileHandler 增挂
        acp_shared，漏挂即红（模型落定/降级 WARNING 必须有落盘痕迹）。"""
        out = []
        for name in names:
            lg = logging.getLogger(name) if name else logging.getLogger()
            for h in lg.handlers:
                if isinstance(h, logging.FileHandler) and h.baseFilename.endswith("req_board_dispatcher.log"):
                    out.append((name, h))
        return out

    def _mount_names(self):
        return sorted(name or "root" for name, _ in self._prod_file_handlers())

    def test_import_no_prod_file_handler(self):
        """模块级只挂 stderr：import 后无 FileHandler 指向生产日志"""
        self.assertEqual(self._prod_file_handlers(), [])
        # root 挂的是 StreamHandler（basicConfig 生产日志只有 stderr 输出）
        self.assertTrue(any(isinstance(h, logging.StreamHandler) and
                            not isinstance(h, logging.FileHandler)
                            for h in logging.getLogger().handlers))

    def test_mount_file_logger_produces_handler(self):
        """main() 挂载后才有 FileHandler（生产入口专用），且格式与模块级一致；
        #11：dispatcher 与 acp_shared 两个 logger 各挂一份（幂等）"""
        d._mount_file_logger()
        hs = self._prod_file_handlers()
        self.assertEqual(len(hs), 2, self._mount_names())
        self.assertEqual(self._mount_names(), ["acp_shared", "dispatcher"])
        for _name, h in hs:
            self.assertEqual(h.baseFilename, PROD_LOG)
        for name in ("dispatcher", "acp_shared"):
            for _n, h in self._prod_file_handlers((name,)):
                logging.getLogger(name).removeHandler(h)
                h.close()  # 清理，不影响其它测试

    def test_subprocess_import_no_prod_log_write(self):
        """子进程模拟「脚本 import dispatcher 打日志」：不产生生产日志 FileHandler"""
        code = (
            "import sys, logging; sys.path.insert(0, %r); "
            "import dispatcher; dispatcher.log.info('req105 probe'); "
            "hs = [(type(h).__name__, getattr(h, 'baseFilename', ''))"
            " for lg in (logging.getLogger(), logging.getLogger('dispatcher')) for h in lg.handlers]; "
            "print('HANDLERS', hs); print('PROBE_OK')"
        ) % str(REPO)
        env = dict(os.environ)
        env.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防子进程误连生产 8899（#343）
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=str(REPO), env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PROBE_OK", r.stdout)
        self.assertNotIn("FileHandler", r.stdout)  # 无 FileHandler 挂载


if __name__ == "__main__":
    unittest.main()
