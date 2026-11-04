#!/usr/bin/env python3
"""req-273 防复发：回合结束但未提交结果时的「无结果续接」护栏测试。

改动：dispatcher.run_agent 在 session.prompt 正常返回后检查 result.json（不存在/解析失败），
同一会话续接一轮（上限 1 次，剩余预算不足 1 分钟不续接）；dev/accept 强调终止后台任务 + 前台验证，
只读角色仅要求补写 result.json；COMMON_TAIL 第 8 条升级为结束前检查单。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_no_result_followup -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_D_REALS = {name: getattr(d, name) for name in (
    "ACPSession", "artifact_dir", "workdir_for", "dev_workdir_for", "post_event",
    "post_run_record", "handle_result", "update_session_info", "backfill_spawn_session",
    "kimi_version", "readonly_tool_roots", "dev_branch", "owner_key",
    "api_board",
)}


class FakeProc(types.SimpleNamespace):
    pass


class FakeSession:
    """ACPSession 替身：记录 prompt 调用，支持按顺序注入行为（写结果/超时/保持缺失）。"""

    def __init__(self, behaviours=None):
        self.session_id = "session_followup_test"
        self.model_name = "test"
        self.thinking = "low"
        self.proc = FakeProc(pid=12345, returncode=0)
        self.prompts = []
        self.behaviours = behaviours or []
        self.cancelled = 0
        self.closed = False
        self.closed_kill = None
        self.usage_called = False

    def start(self, workdir, resume_session_id=None):
        return False

    def prompt(self, text, timeout):
        self.prompts.append((text, timeout))
        idx = len(self.prompts) - 1
        if idx < len(self.behaviours) and self.behaviours[idx]:
            self.behaviours[idx](self)
        return {}

    def collect_usage(self):
        self.usage_called = True
        return None

    def cancel(self):
        self.cancelled += 1

    def close(self, kill=False):
        self.closed = True
        self.closed_kill = kill

    def _flush_message(self):
        pass


class ResultStateTest(unittest.TestCase):
    """result_state：产物目录 / legacy 兜底 / 非法 JSON 三态"""

    def setUp(self):
        self._orig_artifact_dir = d.artifact_dir
        self._orig_workdir_for = d.workdir_for
        self.art = Path(tempfile.mkdtemp(prefix="req273-art-"))
        self.wd = Path(tempfile.mkdtemp(prefix="req273-wd-"))
        d.artifact_dir = lambda req: self.art
        d.workdir_for = lambda project: self.wd

    def tearDown(self):
        d.artifact_dir = self._orig_artifact_dir
        d.workdir_for = self._orig_workdir_for
        shutil.rmtree(self.art, ignore_errors=True)
        shutil.rmtree(self.wd, ignore_errors=True)

    def _req(self):
        return {"id": 273001, "project": "req-board", "title": "T", "notes": ""}

    def test_missing(self):
        state, detail = d.result_state(self._req())
        self.assertEqual(state, "missing")
        self.assertIn("不存在", detail)

    def test_ok(self):
        (self.art / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))
        state, _ = d.result_state(self._req())
        self.assertEqual(state, "ok")

    def test_invalid(self):
        (self.art / "result.json").write_text('{"status": "done", "summary": "坏\n换行"}')
        state, detail = d.result_state(self._req())
        self.assertEqual(state, "invalid")
        self.assertIn("解析失败", detail)

    def test_legacy_fallback(self):
        (self.wd / d.RESULT_FILENAME).write_text(json.dumps({"status": "done", "summary": "ok"}))
        state, _ = d.result_state(self._req())
        self.assertEqual(state, "ok")


class FollowupPromptTest(unittest.TestCase):
    """build_followup_prompt：dev/accept 与只读角色文案分叉 + #388 产物目录现状注入"""

    def setUp(self):
        self._orig_artifact_dir = d.artifact_dir
        self.art = Path(tempfile.mkdtemp(prefix="req388-followup-art-"))
        d.artifact_dir = lambda req: self.art

    def tearDown(self):
        d.artifact_dir = self._orig_artifact_dir
        shutil.rmtree(self.art, ignore_errors=True)

    def _req(self):
        return {"id": 388, "project": "req-board", "title": "T", "notes": ""}

    def test_dev_accept_emphasize_background_and_foreground(self):
        for role in ("dev", "accept"):
            p = d.build_followup_prompt(role, "missing", self._req())
            for kw in ("后台任务", "前台分块", "json.dumps", "result.json", "回读校验"):
                self.assertIn(kw, p, (role, kw))

    def test_readonly_only_rewrite_result(self):
        for role in ("analysis", "proposal", "tech"):
            p = d.build_followup_prompt(role, "missing", self._req())
            for kw in ("Write/fs", "result.json", "回读校验"):
                self.assertIn(kw, p, (role, kw))
            self.assertNotIn("后台任务", p, role)

    def test_invalid_state_reason(self):
        p = d.build_followup_prompt("dev", "invalid", self._req())
        self.assertIn("JSON 解析失败", p)
        p = d.build_followup_prompt("dev", "missing", self._req())
        self.assertIn("不存在", p)

    # ---- #388：续接轮注入产物目录现状清单 + 强化写入失败必须重写 ----
    def test_artifact_ls_injected_with_filename_list(self):
        """产物目录有文件 → prompt 含「产物目录现状」+ 文件名清单（含 result.json 自身）"""
        (self.art / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))
        (self.art / "analysis.md").write_text("# a")
        p = d.build_followup_prompt("analysis", "missing", self._req())
        self.assertIn("产物目录现状", p)
        self.assertIn("result.json", p)
        self.assertIn("analysis.md", p)

    def test_artifact_ls_empty_and_missing_dir_degrades(self):
        """空目录 → 「（空）」；artifact_dir 抛异常（不可读）→ 降级「（空）」不崩"""
        p = d.build_followup_prompt("analysis", "missing", self._req())
        self.assertIn("（空）", p)
        d.artifact_dir = lambda req: (_ for _ in ()).throw(OSError("no access"))
        p = d.build_followup_prompt("analysis", "missing", self._req())
        self.assertIn("（空）", p)

    def test_strict_write_reminder(self):
        """强化文案：写入失败必须重写、禁止乐观声明（仅写一次未校验即声称成功）"""
        p = d.build_followup_prompt("dev", "missing", self._req())
        self.assertIn("必须重写后再回读校验", p)
        self.assertIn("禁止声称已写入但实际未校验", p)

    def test_common_tail_checklist(self):
        """#42 静态化：COMMON_TAIL 常量直接含收尾检查单（不再 format）"""
        rendered = d.COMMON_TAIL
        for kw in ("结束回合前必须过一遍检查单", "等待后台任务通知", "前台分块", "空轮询",
                   "run_in_background", "回读可解析"):
            self.assertIn(kw, rendered, kw)


class RunAgentFollowupTest(unittest.TestCase):
    """run_agent 无结果续接集成行为：触发 / 续接超时 / 剩余预算不足跳过"""

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in _D_REALS}
        self.art = Path(tempfile.mkdtemp(prefix="req273-run-art-"))
        self.wd = Path(tempfile.mkdtemp(prefix="req273-run-wd-"))
        self.events = []
        self.results = []
        d.artifact_dir = lambda req: self.art
        d.workdir_for = lambda project: self.wd
        d.dev_workdir_for = lambda req: self.wd
        d.post_event = lambda req_id, kind, content, session_id=None: self.events.append((kind, content))
        d.post_run_record = lambda *a, **k: None
        d.update_session_info = lambda *a, **k: None
        d.backfill_spawn_session = lambda *a, **k: None
        d.kimi_version = lambda: "test"
        d.readonly_tool_roots = lambda *a, **k: []
        d.dev_branch = lambda req: "f-followup"
        d.owner_key = lambda req: "owner"
        d.api_board = lambda method, path, body=None: {"id": 1}  # 单测不写真实看板库
        d.handle_result = lambda req, role, returncode, timed_out, started_at, aborted=False: self.results.append(
            (role, returncode, timed_out))

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(d, name, value)
        d.active_runs.pop("273002", None)  # #388：对象匹配用例可能遗留新登记，清理防污染
        shutil.rmtree(self.art, ignore_errors=True)
        shutil.rmtree(self.wd, ignore_errors=True)

    def _req(self):
        # 卡当前节点为 dev（standard 流程首节点）；auto 流程已删除，不再用 mode=auto 表达
        return {"id": 273002, "project": "req-board", "title": "T",
                "mode": "semi", "phase": "dev", "flow": "standard", "notes": ""}

    def _run(self, fake, req, started_at=None, active_elapsed=None):
        """active_elapsed：模拟本次运行已消耗的活跃时长（秒）。
        remaining 自 2026-09-17 起按单调时钟记账（睡眠穿透修复：wall clock 会把系统睡眠
        算进已用预算，唤醒后误判预算耗尽），故用 mock 推进 monotonic 而非回退 wall clock
        来模拟「预算将尽」。"""
        d.ACPSession = lambda *a, **k: fake
        if active_elapsed is None:
            d.run_agent(req, "dev", "初始 prompt", started_at=started_at or time.time())
            return
        base = time.monotonic()
        calls = {"n": 0}

        def _fake_mono():
            calls["n"] += 1
            return base if calls["n"] == 1 else base + active_elapsed   # 首调=起点，其后=已消耗

        with mock.patch.object(d.time, "monotonic", _fake_mono):
            d.run_agent(req, "dev", "初始 prompt", started_at=started_at or time.time())

    def test_missing_result_triggers_followup_and_recovers(self):
        fake = FakeSession(behaviours=[None, lambda s: (
            d.artifact_dir(self._req()) / "result.json").write_text(
                json.dumps({"status": "done", "summary": "续接补写"}))])
        self._run(fake, self._req())
        self.assertEqual(len(fake.prompts), 2)
        self.assertIn("续接一轮", " ".join(c for _, c in self.events))
        self.assertTrue((self.art / "result.json").exists())
        self.assertTrue(fake.usage_called)
        self.assertEqual(self.results, [("dev", 0, False)])

    def test_followup_timeout_marks_timed_out(self):
        fake = FakeSession(behaviours=[None, lambda s: (_ for _ in ()).throw(TimeoutError())])
        self._run(fake, self._req())
        self.assertEqual(len(fake.prompts), 2)
        self.assertEqual(fake.cancelled, 1)
        self.assertTrue(fake.closed)
        self.assertTrue(fake.closed_kill)
        self.assertFalse(fake.usage_called)
        self.assertEqual(self.results, [("dev", 0, True)])
        self.assertTrue(any("续接超时" in c for _, c in self.events))

    def test_remaining_budget_below_minute_skips_followup(self):
        fake = FakeSession(behaviours=[None])
        # 与默认预算解耦：已用 =（默认单轮预算 - 50 秒），剩 50 秒 < 1 分钟 → 不续接
        elapsed = d.requirement_timeout(self._req()) * 60 - 50
        self._run(fake, self._req(), active_elapsed=elapsed)
        self.assertEqual(len(fake.prompts), 1)
        self.assertTrue(fake.usage_called)
        self.assertEqual(self.results, [("dev", 0, False)])
        self.assertFalse(any("续接一轮" in c for _, c in self.events))

    def test_invalid_result_triggers_followup(self):
        fake = FakeSession(behaviours=[lambda s: (
            d.artifact_dir(self._req()) / "result.json").write_text('{"status": "bad"'), None])
        self._run(fake, self._req())
        self.assertEqual(len(fake.prompts), 2)
        self.assertTrue(any("解析失败" in c for _, c in self.events))
        self.assertEqual(self.results, [("dev", 0, False)])

    def test_finally_pop_is_object_matched(self):
        """#388 问题 2：会话期间 active_runs 被换为新对象（sweep 摘除旧登记/失败通道新会话）
        → finally 的对象匹配 pop 不误删新登记"""
        req = self._req()
        newer = {"proc": "new_proc", "role": "dev", "started_at": 2000.0, "session": "new_sess"}
        fake = FakeSession(behaviours=[lambda s: d.active_runs.__setitem__(
            "273002", newer)])
        self._run(fake, req)
        self.assertIs(d.active_runs["273002"], newer)


if __name__ == "__main__":
    unittest.main()
