#!/usr/bin/env python3
"""req-113 进程安全红线失败归因测试：
1. dev 红线取消信号（自杀式进程清理被拦截 → cancel 未写 result.json）→ last_failure 明确归因为
   「命中进程安全红线」而非泛化「进程正常退出但未提交结果」，事件 content 带出便于自查
2. 续跑 extra 注入针对性修复指引（scripts/test_instance.sh + 禁 trap/kill/killpg/pkill）
3. 误判防护：无红线事件 / api 异常 / 非 dev 一律退化现有文案；成功路径零干扰
4. 提示词/常量/发射端文案一致性断言（验收 1/2，防漂移）

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
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

import acp_shared as ac  # noqa: E402（共享常量直接引用共享模块）
import dispatcher as d  # noqa: E402

SELF_PID = 12345


def fresh_state():
    return {"chat_id": "oc_req113_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


def redline_event(hit="kill/killpg 目标为本会话驱动进程（PID=12345）"):
    """构造 dev 红线取消信号事件（与 acp_shared L3 发射文案同构）"""
    return {"kind": "system", "content": f"{d.REDLINE_CANCEL_SIGNAL}（{hit}），会话已取消"}


class _Base(unittest.TestCase):
    """公共 mock 环境：产物目录指向临时目录，看板/状态写入全部接管，d.api 默认返回无红线事件"""

    def setUp(self):
        names = ("state", "artifact_dir", "api", "save_state", "set_status", "set_req",
                 "append_summary", "post_dispatcher_event", "notify", "notify_req_card",
                 "spawn_agent", "_spawn_safe", "fetch_req", "has_code_changes",
                 "mark_session_poisoned")
        self._originals = {name: getattr(d, name) for name in names}
        d.state = fresh_state()
        self._sleep = mock.patch.object(d.time, "sleep")
        self._sleep.start()
        self.art = Path(tempfile.mkdtemp(prefix="req113-art-"))
        d.artifact_dir = lambda req: self.art
        self.api_events = []
        d.api = lambda method, path, body=None: self.api_events  # 事件查询桩：默认无红线事件
        self.status = []
        self.events = []
        self.spawns = []
        self.req_sets = []
        self.summaries = []
        self.notifies = []
        self.card_notifies = []   # notify_req_card 记录 (event, kwargs)（重试耗尽通知改走卡片）
        d.save_state = lambda: None
        d.set_status = lambda rid, st: self.status.append((rid, st))
        d.set_req = lambda rid, fields: self.req_sets.append((rid, fields))
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append((event, k))
        d.spawn_agent = lambda req, role=None, extra="", resume=False: self.spawns.append((role, extra, resume))
        d._spawn_safe = lambda req, role, extra="", resume=False: self.spawns.append((role, extra, resume))
        d.fetch_req = lambda rid: {"id": rid, "project": "req-board", "title": "T", "mode": "auto"}
        d.has_code_changes = lambda req, since: True
        d.mark_session_poisoned = lambda *a, **k: None

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(d, name, value)
        self._sleep.stop()
        shutil.rmtree(self.art, ignore_errors=True)

    def _req(self, rid=113, mode="auto", phase=None, flow=None):
        req = {"id": rid, "project": "req-board", "title": "T", "mode": mode, "phase": phase}
        if flow:
            req["flow"] = flow
        return req


class RedlineAttributionTest(_Base):
    """验收 2：dev 红线取消 → 失败原因明确归因 + 持久化 + 续跑提示注入"""

    def test_t1_redline_cancel_persists_reason(self):
        """T1：红线取消事件 + 无 result.json + rc=0 → last_failure 含「命中进程安全红线」与事件 content"""
        self.api_events = [redline_event()]
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        rec = d.state["retries"]["113"]
        self.assertEqual(rec["count"], 1)
        self.assertIn("命中进程安全红线", rec["last_failure"])
        self.assertIn("kill/killpg 目标为本会话驱动进程（PID=12345）", rec["last_failure"])  # 事件 content 带出
        # _retry_rec 兼容读取（升级后仍为 {count, last_failure} 格式）
        self.assertEqual(d._retry_rec("113")["count"], 1)
        self.assertIn("命中进程安全红线", d._retry_rec("113")["last_failure"])

    def test_t2_retry_extra_has_redline_hint(self):
        """T2：续跑 extra 含红线归因 + 针对性修复指引（test_instance.sh / 禁 trap/kill/killpg/pkill）"""
        self.api_events = [redline_event()]
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(len(self.spawns), 1)
        role, extra, resume = self.spawns[0]
        self.assertEqual(role, "dev")
        self.assertTrue(resume)
        self.assertIn("命中进程安全红线", extra)
        self.assertIn("项目约定的实例脚本", extra)
        self.assertIn("禁止自行编写 trap/kill/killpg/pkill", extra)
        self.assertIn("第 1 次", extra)
        # 看板事件带红线归因与角色
        self.assertTrue(any("命中进程安全红线" in c and "role=dev" in c for _, c in self.events))

    def test_t3_no_redline_event_falls_back(self):
        """T3：无红线事件（d.api 返回 []）+ rc=0 → 维持现有泛化文案，不误判"""
        self.api_events = []
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertIn("进程正常退出但未提交结果", d.state["retries"]["113"]["last_failure"])
        self.assertNotIn("命中进程安全红线", d.state["retries"]["113"]["last_failure"])

    def test_t4_api_exception_falls_back(self):
        """T4：d.api 抛异常 → 退化现有文案，不崩溃、仍正常触发重试"""
        d.api = mock.Mock(side_effect=RuntimeError("board down"))
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertIn("进程正常退出但未提交结果", d.state["retries"]["113"]["last_failure"])
        self.assertEqual(len(self.spawns), 1)
        self.assertTrue(self.spawns[0][2])  # resume=True

    def test_t5_non_dev_not_attributed(self):
        """T5：非 dev（analysis）红线事件「已被审批拒绝」→ 不归因为红线（不 cancel 语义不同）"""
        self.api_events = [{"kind": "system",
                            "content": "进程清理红线命令（kill/killpg 目标为本会话驱动进程（PID=12345）），已被审批拒绝"}]
        d.handle_result(self._req(), "analysis", returncode=0, timed_out=False, started_at=time.time())
        self.assertNotIn("命中进程安全红线", d.state["retries"]["113"]["last_failure"])
        self.assertIn("进程正常退出但未提交结果", d.state["retries"]["113"]["last_failure"])

    def test_t6_exhausted_notify_has_redline(self):
        """T6：重试耗尽（retries 预置 MAX_RETRIES）+ 红线事件 → 转 later 停止自动调度，summary/卡片通知含红线归因"""
        d.state["retries"]["113"] = {"count": d.MAX_RETRIES, "last_failure": "旧原因"}
        self.api_events = [redline_event()]
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(self.status, [(113, "later")])
        self.assertNotIn("113", d.state["retries"])  # 耗尽清除
        self.assertTrue(any("命中进程安全红线" in s for s in self.summaries))
        # 失败耗尽通知改走 failed 卡片：event=failed、reason 含红线归因、retries=MAX_RETRIES+1
        failed_cards = [(e, k) for e, k in self.card_notifies if e == "failed"]
        self.assertTrue(failed_cards, self.card_notifies)
        self.assertIn("命中进程安全红线", failed_cards[0][1]["reason"])
        self.assertEqual(failed_cards[0][1]["retries"], d.MAX_RETRIES + 1)
        self.assertTrue(any("等待人工重新开放" in s for s in self.summaries))

    def test_t7_valid_done_not_disturbed(self):
        """T7：正常收尾回归——无红线事件 + 合法 result.json（dev semi done）→ 走成功推进，红线归因不干扰"""
        self.api_events = []
        (self.art / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))
        d.handle_result(self._req(mode="semi", phase="dev", flow="standard"), "dev",
                        returncode=0, timed_out=False, started_at=time.time())
        self.assertNotIn("113", d.state["retries"])  # clear_retries
        self.assertIn((113, {"node_index": 1}), self.req_sets)   # standard：dev(0) → code_review(1)
        self.assertEqual(self.spawns[-1][0], "code_review")  # 无 dev 重试 spawn
        self.assertFalse(any(r == "dev" for r, _, _ in self.spawns))


class ExhaustedToLaterTest(_Base):
    """#113 验收 4：重试耗尽停止自动调度（转 later，不再退回 open 被自动重捞）"""

    def test_e1_exhausted_sets_later_with_marks(self):
        """E1：耗尽 → set_status (id, later)；summary 含「等待人工重新开放」；failed 卡片通知；
        post_dispatcher_event 含「转 later」"""
        d.state["retries"]["113"] = {"count": d.MAX_RETRIES, "last_failure": "旧原因"}
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(self.status, [(113, "later")])
        self.assertTrue(any("等待人工重新开放" in s for s in self.summaries), self.summaries)
        self.assertTrue(any(e == "failed" for e, _ in self.card_notifies), self.card_notifies)
        self.assertTrue(any("停止自动调度转 later" in c and "role=dev" in c for _, c in self.events), self.events)

    def test_e2_exhausted_clears_retries_and_marks_poisoned(self):
        """E2：转 later 后 retries 已 pop；mark_session_poisoned（标记毒会话，不删记录）被调"""
        poisoned = []
        d.mark_session_poisoned = lambda rid, role=None: poisoned.append((rid, role))
        d.state["retries"]["113"] = {"count": d.MAX_RETRIES, "last_failure": "旧原因"}
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertNotIn("113", d.state["retries"])
        self.assertEqual(poisoned, [("113", "dev")])

    def test_e3_get_open_reqs_skips_later(self):
        """E3：主循环只捞 open——get_open_reqs 只查 status=open（later 卡不在该查询结果里，天然不捞）"""
        calls = []

        def fake_api(method, path, body=None):
            calls.append(path)
            # 模拟看板服务端：status=open 查询只返回 open 卡（later 不在此查询结果）
            if "status=open" in path:
                return [{"id": 113, "project": "req-board", "title": "T", "status": "open",
                         "updated_at": "2026-08-07 00:00:00"}]
            return []
        d.api = fake_api
        got = d.get_open_reqs("req-board")
        self.assertTrue(any("status=open" in p for p in calls), calls)
        self.assertTrue(got, "open 卡应被捞取")
        self.assertTrue(all(r["status"] == "open" for r in got))
        # later 卡不在 status=open 查询结果中 → 不会被捞（后续断言由服务端过滤语义保证）


class PromptConstantConsistencyTest(unittest.TestCase):
    """验收 1/2：提示词指引 + 共享常量防漂移 + 发射端文案一致性"""

    def test_t8_dev_prompts_have_cleanup_guide(self):
        """T8：dev_auto / dev_semi 提示词含测试脚本清理指引 + 禁 trap/kill/killpg/pkill + 红线后果"""
        for key in ("dev_auto", "dev_semi"):
            p = d.ROLE_PROMPTS[key]
            self.assertNotIn("scripts/test_instance.sh", p, key)   # A1：实例脚本名来自项目清单
            self.assertIn("项目约定的实例脚本", p, key)
            self.assertIn("禁止自行编写 trap/kill/killpg/pkill 命令", p, key)
            self.assertIn("禁止清理 ACP 驱动进程或其进程组", p, key)
            self.assertIn("命中进程安全红线会被拦截并取消会话", p, key)

    def test_t8b_repo_project_dev_prompt_names_instance_script(self):
        """A1：本仓库项目的 dev 最终 prompt 仍给出具体实例脚本（来自 .loop 指引）"""
        wd = Path("/tmp/a1-redline-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value="工作目录：/tmp/a1-redline-wd"):
            p = d.build_prompt({"id": 8, "project": "req-board", "title": "T", "notes": "N"}, "dev")
        self.assertIn("scripts/test_instance.sh", p)
        self.assertIn("./start.sh", p)

    def test_t9_constants_no_drift(self):
        """T9：REDLINE_CANCEL_SIGNAL 常量值 + dispatcher/acp_shared 引用一致（发射/检测共用）"""
        self.assertEqual(d.REDLINE_CANCEL_SIGNAL, "自杀式进程清理被拦截")
        self.assertEqual(ac.REDLINE_CANCEL_SIGNAL, d.REDLINE_CANCEL_SIGNAL)
        self.assertEqual(ac.REDLINE_CANCEL_SIGNAL, "自杀式进程清理被拦截")

    def test_t10_kill_redline_tmpl_enhanced(self):
        """T10：KILL_REDLINE_TMPL 含 trap / test_instance.sh / ACP 驱动进程禁令（注入文本增强）"""
        t = ac.KILL_REDLINE_TMPL
        self.assertIn("trap", t)
        self.assertIn("项目约定的实例脚本", t)
        self.assertNotIn("scripts/test_instance.sh", t)          # A1：全局红线文本项目无关
        self.assertIn("ACP 驱动进程或其进程组", t)
        self.assertIn("禁止 kill/killpg/pkill 该 PID", t)  # 既有语义保留


class L3EmitConsistencyTest(unittest.TestCase):
    """发射端文案一致性（回归 #108 L3：dev 命中红线 emit 取消信号 + cancel）"""

    def _update_stub(self, role):
        s = d.ACPSession.__new__(d.ACPSession)
        s.req_id = 99999
        s.role = role
        s.session_id = "test-session"
        s.proc = types.SimpleNamespace(pid=SELF_PID)
        s._msg_buf = []
        s._buf_lock = threading.Lock()
        s._tool_calls = {}
        s._tool_posted = set()
        s.events = []
        s.cancelled = []

        def emit(kind, content, session_id=None):
            s.events.append((kind, content))

        s._emit = emit
        s.cancel = lambda: s.cancelled.append(1)
        return s

    def _tool_update(self, cmd, tid="t1"):
        return {"sessionUpdate": "tool_call_update", "toolCallId": tid,
                "rawInput": {"command": cmd}, "title": f"Running: {cmd}"}

    def test_t11_emit_starts_with_shared_signal(self):
        """T11：L3 dev 命中红线 → 事件 content 以 REDLINE_CANCEL_SIGNAL 开头（发射/检测共用一个常量）"""
        s = self._update_stub("dev")
        s._on_update(self._tool_update(f"kill {SELF_PID}"))
        self.assertEqual(s.cancelled, [1])
        sys_events = [c for k, c in s.events if k == "system"]
        self.assertEqual(len(sys_events), 1)
        self.assertTrue(sys_events[0].startswith(ac.REDLINE_CANCEL_SIGNAL), sys_events[0])
        self.assertIn("会话已取消", sys_events[0])


if __name__ == "__main__":
    unittest.main()
