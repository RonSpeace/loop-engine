#!/usr/bin/env python3
"""req-109 调度超时与重试韧性测试：
1. dev/chat prompt 模板含时间提醒文案（date 关注耗时 / 约剩 5 分钟保底 / commit 当前进度）
2. 失败原因持久化：retries[req_id] = {count, last_failure}，旧 int 值兼容
3. 续跑注入上次失败原因：resume_failure_hint（主循环重捞场景）+ 进程内重试 extra
4. per-requirement 超时预算：默认 60 分钟，notes `timeout: <分钟>` 覆盖（clamp 15~120），prompt 文案动态化
5. #345 API 并发限流（403）错误分类与重试退避：日志特征归类限流（与进程异常退出区分）、
   限流失败重试延迟 RATE_LIMIT_RETRY_DELAY（60~120），非限流保持原退避

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
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

import dispatcher as d  # noqa: E402

# 测试会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "post_dispatcher_event", "notify",
    "notify_req_card", "spawn_agent", "append_summary", "mark_session_poisoned", "api",
    "_rate_limit_failure_reason")}


def fresh_state():
    return {"chat_id": "oc_req109_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


class TimeAwarePromptTest(unittest.TestCase):
    """验收 1：dev/chat prompt 模板含时间提醒文案"""

    def test_common_tail_renders_time_hint(self):
        """#42 静态化：COMMON_TAIL 常量含时间感知骨架（date/约剩 5 分钟/时间保底）且无 format 占位符；
        时限数字按卡变化 → 改挂 build_prompt 产物（见 test_build_prompt_dev_contains_time_management）"""
        rendered = d.COMMON_TAIL
        for kw in ("`date`", "约剩 5 分钟", "时间保底"):
            self.assertIn(kw, rendered, kw)
        for ph in ("{timeout_minutes}", "{result_json}", "{max_steps}", "{artifact_root}"):
            self.assertNotIn(ph, rendered)  # 静态常量不再按卡插值
        self.assertIn("见文末卡片输入", rendered)  # 时限/路径指路句指向卡片输入块

    def test_build_prompt_dev_contains_time_management(self):
        """build_prompt dev 角色渲染含时间管理要求（commit 当前进度 + result.json 保底）"""
        req = {"id": 109, "project": "req-board", "title": "T", "mode": "auto", "notes": "N"}
        wd = Path("/tmp/req109-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(req, "dev")
        for kw in ("时间管理", "git add + git commit", "240 分钟", "约剩 5 分钟", "result.json 保底"):
            self.assertIn(kw, prompt, kw)
        self.assertIn("240 分钟时限", prompt)  # COMMON_TAIL 同步动态化

    def test_build_prompt_dev_timeout_override_in_prompt(self):
        """notes timeout 覆盖：dev prompt 时限文案随 requirement_timeout 动态变化"""
        req = {"id": 109, "project": "req-board", "title": "T", "mode": "auto",
               "notes": "大需求\n timeout: 45"}
        wd = Path("/tmp/req109-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(req, "dev")
        self.assertIn("45 分钟时限", prompt)
        self.assertIn("45 分钟", prompt)

    def test_agents_md_time_awareness_section(self):
        """AGENTS.md 含时间感知约定（与 prompt 文案同步）"""
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("**时间感知", text)
        for kw in ("timeout", "约剩 5 分钟", "`result.json` 保底", "失败原因"):
            self.assertIn(kw, text, kw)


class ChatTimeAwarePromptTest(unittest.TestCase):
    """验收 1：建卡会话（chat）系统提示词含时间提示"""

    def test_chat_system_prompt_has_time_hint(self):
        import board as b
        prompt = b._chat_system_prompt("req-board")
        for kw in ("10 分钟超时", "`date`", "```draft 草稿块", "保底"):
            self.assertIn(kw, prompt, kw)

    def test_chat_system_prompt_has_no_interactive_terminal_hint(self):
        """req-292：建卡会话系统提示词含无交互终端说明（禁止提问工具、消息文本提问）"""
        import board as b
        prompt = b._chat_system_prompt("req-board")
        for kw in ("无交互终端", "提问类工具", "dismiss", "禁止使用", "消息文本里提问"):
            self.assertIn(kw, prompt, kw)
        # 原有文字提问澄清语义保留
        self.assertIn("一轮结束时用文字向用户提问澄清", prompt)


class RequirementTimeoutTest(unittest.TestCase):
    """验收 3（预算项）：notes timeout 字段解析与 clamp"""

    def test_default_timeout(self):
        # 默认预算锁定为 240 分钟（长任务可连续跑；AGENT_TIMEOUT 单一事实源，prompt 文案同源）
        self.assertEqual(d.AGENT_TIMEOUT, 240 * 60)
        self.assertEqual(d.requirement_timeout({"id": 1, "notes": "普通需求"}), 240)
        self.assertEqual(d.requirement_timeout({"id": 1, "notes": None}), 240)
        self.assertEqual(d.requirement_timeout({"id": 1, "notes": ""}), 240)

    def test_override(self):
        self.assertEqual(d.requirement_timeout({"id": 1, "notes": "背景 timeout: 45 验收"}), 45)
        self.assertEqual(d.requirement_timeout({"id": 1, "notes": "中文冒号 timeout：60"}), 60)

    def test_clamp(self):
        self.assertEqual(d.requirement_timeout({"id": 1, "notes": "timeout: 5"}), 15)
        self.assertEqual(d.requirement_timeout({"id": 1, "notes": "timeout: 999"}), 480)


class FailureReasonPersistenceTest(unittest.TestCase):
    """验收 2：失败原因持久化 + 重试/续跑注入"""

    def setUp(self):
        d.state = fresh_state()
        self._sleep = mock.patch.object(d.time, "sleep")
        self._sleep.start()
        self._orig_artifact_dir = d.artifact_dir
        self.art = Path(tempfile.mkdtemp(prefix="req109-art-"))
        d.artifact_dir = lambda req: self.art
        self.events = []
        self.spawns = []
        d.save_state = lambda: None
        d.set_status = lambda rid, st: None
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify = lambda *a, **k: None
        d.notify_req_card = lambda *a, **k: None  # #115：六类关键通知改走卡片，测试环境静默
        d.spawn_agent = lambda req, role=None, extra="", resume=False: self.spawns.append((role, extra, resume))
        d.append_summary = lambda *a, **k: None
        d.mark_session_poisoned = lambda *a, **k: None
        # #345：失败链默认不读真实 /tmp 日志（隔离测试环境，限流用例内显式覆盖返回值）
        d._rate_limit_failure_reason = lambda rid: None
        # #113 红线归因新增事件查询（GET /<id>/events）：默认返回无红线事件，测试完全离线化
        d.api = lambda method, path, body=None: []

    def tearDown(self):
        d.artifact_dir = self._orig_artifact_dir
        self._sleep.stop()
        for name, value in _D_REALS.items():
            setattr(d, name, value)
        shutil.rmtree(self.art, ignore_errors=True)

    def _req(self, rid=109, mode="auto"):
        return {"id": rid, "project": "req-board", "title": "T", "mode": mode}

    def test_timed_out_persists_reason(self):
        """运行超时：last_failure 持久化为「运行超时」，重试 extra 含原因"""
        d.handle_result(self._req(), "dev", returncode=0, timed_out=True, started_at=time.time())
        rec = d.state["retries"]["109"]
        self.assertEqual(rec["count"], 1)
        self.assertTrue(rec["last_failure"].startswith("运行超时"))       # C2：带 budget_exceeded: time 结构化标注
        self.assertIn("budget_exceeded: time", rec["last_failure"])
        role, extra, resume = self.spawns[0]
        self.assertTrue(resume)
        self.assertIn("运行超时", extra)
        self.assertIn("第 1 次", extra)

    def test_exit_code_reason_persisted(self):
        """进程异常退出：last_failure 带退出码"""
        d.handle_result(self._req(), "dev", returncode=-9, timed_out=False, started_at=time.time())
        self.assertEqual(d.state["retries"]["109"]["count"], 1)
        self.assertIn("退出码 -9", d.state["retries"]["109"]["last_failure"])

    def test_rate_limit_reason_classified(self):
        """#345：403 限流归因——异常退出且日志含限流特征 → 归因「API 并发限流」而非「进程异常退出」"""
        d._rate_limit_failure_reason = lambda rid: "API 并发限流（403：并发位被占），请求被拒绝，会话被终止"
        d.handle_result(self._req(), "dev", returncode=-15, timed_out=False, started_at=time.time())
        rec = d.state["retries"]["109"]
        self.assertEqual(rec["count"], 1)
        self.assertIn("API 并发限流", rec["last_failure"])
        self.assertNotIn("退出码 -15", rec["last_failure"])
        role, extra, resume = self.spawns[0]
        self.assertTrue(resume)
        self.assertIn("API 并发限流", extra)
        self.assertIn("延迟重试", extra)

    def test_rate_limit_retry_delay(self):
        """#345：限流失败重试前存在延迟等待——sleep 参数 = RATE_LIMIT_RETRY_DELAY（60~120），非 30 秒立即重发"""
        d._rate_limit_failure_reason = lambda rid: "API 并发限流（403：并发位被占），请求被拒绝，会话被终止"
        d.handle_result(self._req(), "dev", returncode=-15, timed_out=False, started_at=time.time())
        calls = [c.args[0] for c in d.time.sleep.call_args_list]
        self.assertTrue(calls, "失败路径应调用 time.sleep 延迟重试")
        self.assertEqual(calls[-1], d.RATE_LIMIT_RETRY_DELAY)
        self.assertGreaterEqual(d.RATE_LIMIT_RETRY_DELAY, 60)
        self.assertLessEqual(d.RATE_LIMIT_RETRY_DELAY, 120)

    def test_non_rate_limit_delay_unchanged(self):
        """#345：非限流失败保持原退避 min(retries*30, 120)，行为不变"""
        d.handle_result(self._req(), "dev", returncode=-9, timed_out=False, started_at=time.time())
        calls = [c.args[0] for c in d.time.sleep.call_args_list]
        self.assertTrue(calls)
        self.assertEqual(calls[-1], 30)  # 第 1 次非限流失败 = min(1*30, 120) = 30 秒

    def test_legacy_int_retry_count_compatible(self):
        """兼容旧状态文件：retries[req_id] 为 int 时按 count 读取并升级（#273 起 MAX_RETRIES=1）"""
        d.state["retries"]["109"] = 0  # 升级前旧格式（int 计数），未失败过
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(d.state["retries"]["109"]["count"], 1)
        self.assertIn("第 1 次", self.spawns[0][1])
        # _retry_rec 直接兼容
        self.assertEqual(d._retry_rec("109")["count"], 1)
        self.assertTrue(d._retry_rec("109")["last_failure"])

    def test_resume_failure_hint(self):
        """主循环重捞注入：有失败记录 → hint 含原因；无记录 → 空串"""
        self.assertEqual(d.resume_failure_hint(109), "")
        d.state["retries"]["109"] = {"count": 1, "last_failure": "运行超时"}
        hint = d.resume_failure_hint(109)
        self.assertIn("运行超时", hint)
        self.assertIn("上次尝试失败原因", hint)
        # 旧 int 格式（无原因记录）不注入
        d.state["retries"]["109"] = 1
        self.assertEqual(d.resume_failure_hint(109), "")

    def test_exhausted_clears_retry_record(self):
        """重试耗尽转 later（#113）：retries 记录清除"""
        d.state["retries"]["109"] = {"count": d.MAX_RETRIES, "last_failure": "环境依赖装不上"}
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertNotIn("109", d.state["retries"])


class RateLimitClassifierTest(unittest.TestCase):
    """#345 验收 1：错误文本分类——_rate_limit_failure_reason 特征匹配（宁可漏判不可误判）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req345-rl-"))
        self.logf = self.tmp / "agent.log"
        self._fmt = mock.patch.object(d, "RATE_LIMIT_LOG_FMT", str(self.logf))
        self._fmt.start()

    def tearDown(self):
        self._fmt.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_request_limit_phrase(self):
        """403 现场原文短语命中 → 归类 API 并发限流"""
        self.logf.write_text("...You have reached your concurrent request limit...")
        reason = d._rate_limit_failure_reason(345)
        self.assertIsNotNone(reason)
        self.assertIn("API 并发限流", reason)

    def test_403_status_code(self):
        """HTTP 403 状态码命中 → 归类 API 并发限流"""
        self.logf.write_text("...HTTP 403 Forbidden...")
        self.assertIn("API 并发限流", d._rate_limit_failure_reason(345) or "")

    def test_normal_log_no_classify(self):
        """正常日志（无 403/限流特征）→ 不归类"""
        self.logf.write_text("...normal exit, result written...")
        self.assertIsNone(d._rate_limit_failure_reason(345))

    def test_missing_log_no_classify(self):
        """日志文件不存在 → 不归类（容错：文件读失败返回 None）"""
        self.assertIsNone(d._rate_limit_failure_reason(345))

    def test_403_word_boundary(self):
        """\b403\b 词边界：1403/4030 不命中，宁可漏判不可误判"""
        self.logf.write_text("port 14030, code 4030")
        self.assertIsNone(d._rate_limit_failure_reason(345))


if __name__ == "__main__":
    unittest.main()
