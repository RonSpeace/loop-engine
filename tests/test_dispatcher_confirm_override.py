#!/usr/bin/env python3
"""req-399 收尾校验失败转人工仲裁（#395 教训）测试。

改动（仅 dispatcher.py + docs）：
- handle_result：done + 进程正常退出 + 收尾校验失败（has_code_changes 无变更 / artifact_ok 失败 /
  suggested_mode 非法）+ result.json 带 waive_gate（非空字符串）→ 转 pending_confirm(kind=confirm_override)
  人工裁决，不计 retries、不自动重试；未申报 = 视为谎报走原失败通道（fail-closed 不变）；
- on_message：confirm_override 消费——y=红字留档 + todos 暂存 + finalize_done 统一收口；
  n=退 open(phase=None)+清 retries（原因 later 开头 → 转 later，分支/worktree 保留）；
- analysis 快通道：suggested_mode=manual_close（已实现/验证记录类）→ 转 route 人工直判
  （y=finalize_done 收口 / n=退 open 重分析），不进 dev、恒不免确认；
- reconcile_adopt_results 预检三值化；COMMON_TAIL/analysis 模板/续接文案同步。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_confirm_override -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899（#343）

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402
import acp_shared as ac  # noqa: E402

# 各测试类会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "set_req", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "fetch_req", "_spawn_safe", "spawn_agent", "finalize_done",
    "clear_retries", "has_code_changes", "artifact_dir", "mark_session_poisoned",
    "PROJECTS", "api", "api_board")}


def _forbid_real_api(*a, **k):
    """sentinel（#343）：测试期间禁止任何真实看板外呼——看板请求全部收敛于 api/api_board，
    未 mock 干净的路径一旦命中直接 AssertionError（fail-closed）"""
    raise AssertionError("测试命中未 mock 的看板外呼路径（api/api_board），禁止真实请求")


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fresh_state():
    return {"chat_id": "oc_req399_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "pending_merge_review": {}, "sessions": {}, "worktrees": {}}


# #419 卡上流程快照：角色节点 + 收尾节点（finish=False 时不含 terminal）
DEV_NODES = [{"role": "dev", "node_type": "code"}, {"node_type": "terminal"}]


def fake_req(rid=399000, mode="semi", snapshot=None):
    """snapshot=(nodes, index) 写卡上流程快照（新引擎按快照推进）；默认无快照（前置阶段）"""
    req = {"id": rid, "project": "req-board", "title": "T", "status": "doing",
           "nodes_json": None, "node_index": None}
    if mode:
        req["mode"] = mode
    if snapshot is not None:
        nodes, idx = snapshot
        req["nodes_json"] = json.dumps(nodes, ensure_ascii=False)
        req["node_index"] = idx
        req.pop("mode", None)
    return req


class Base(unittest.TestCase):
    """公共 mock：看板写入/通知/事件全部收进 self.calls，产物目录落临时目录；
    api/api_board 钉 sentinel（#343）；失败通道的同步 sleep 统一 patch（#109 退避不真实等待）"""

    def setUp(self):
        d.state = fresh_state()
        self.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self._sleep = mock.patch.object(d.time, "sleep")
        self._sleep.start()
        d.save_state = lambda: None
        d.api = _forbid_real_api
        d.api_board = _forbid_real_api
        d.set_status = lambda rid, s: self.calls.append(("set_status", s))
        d.set_req = lambda *a, **k: self.calls.append(("set_req", a, k))
        d.append_summary = lambda *a, **k: self.calls.append(("append_summary", a, k))
        d.post_dispatcher_event = lambda *a, **k: self.calls.append(("event", a, k))
        d.notify = lambda *a, **k: self.calls.append(("notify", a, k))
        d.notify_req_card = lambda *a, **k: self.calls.append(("card", a, k))
        d.fetch_req = lambda rid: None
        d._spawn_safe = lambda req, role, **k: self.calls.append(("spawn", role))
        d.spawn_agent = lambda req, role=None, **k: self.calls.append(("spawn_agent", role))
        d.finalize_done = lambda req, summary=None: self.calls.append(("finalize", summary))
        d.clear_retries = lambda rid: self.calls.append(("clear_retries", rid))
        d.mark_session_poisoned = lambda *a, **k: self.calls.append(("poison", a, k))
        d.artifact_dir = lambda req: Path(self.tmp.name)

    def tearDown(self):
        self._sleep.stop()
        _restore_d()
        self.tmp.cleanup()

    def live(self, req):
        """让 set_req 写卡 + fetch_req 回读反映同一张卡（模拟真实看板的写后回读校验）"""
        self.cur_req = req
        d.fetch_req = lambda rid: self.cur_req
        def _set_req(rid, fields, **kw):
            self.calls.append(("set_req", (rid, fields), kw))
            self.cur_req.update(fields)
        d.set_req = _set_req
        return req

    def write_result(self, payload):
        (Path(self.tmp.name) / "result.json").write_text(json.dumps(payload, ensure_ascii=False))

    def kinds(self, kind):
        return [c for c in self.calls if c[0] == kind]


class ArbitrationRouteTest(Base):
    """A. 仲裁分流（handle_result）：done+进程正常+校验失败+waive_gate 申报 → 人工裁决，不计 retries"""

    def _dev_done_no_changes(self, payload):
        d.has_code_changes = lambda req, since: False
        req = fake_req(snapshot=(DEV_NODES, 0))   # #419：dev = code 节点
        self.write_result(payload)
        d.handle_result(req, "dev", 0, False, time.time())
        return req

    def test_dev_done_no_changes_with_waive_goes_arbitration(self):
        """#395 复现：dev done 无变更 + waive_gate → pending_confirm(kind=confirm_override)，
        question 含校验原因与豁免理由；不重试、不续跑、不计 retries、不 finalize"""
        self._dev_done_no_changes({"status": "done", "summary": "实现已由人工提交 main",
                                   "waive_gate": "实现已由人工提交 main（e390d6d）"})
        entry = d.state["pending_confirm"]["399000"]
        self.assertEqual(entry["kind"], "confirm_override")
        self.assertIn("无代码变更", entry["question"])
        self.assertIn("实现已由人工提交 main（e390d6d）", entry["question"])
        self.assertEqual(self.kinds("spawn_agent"), [])
        self.assertEqual(self.kinds("spawn"), [])
        self.assertEqual(self.kinds("finalize"), [])
        self.assertEqual(d.state["retries"], {})

    def test_dev_done_no_changes_without_waive_fails_as_before(self):
        """回归·真谎报：无变更 + 无申报 → 原失败通道（retries+1 + resume 重试），不仲裁"""
        self._dev_done_no_changes({"status": "done", "summary": "完成了 X"})
        self.assertEqual(d.state["retries"]["399000"]["count"], 1)
        self.assertEqual(self.kinds("spawn_agent"), [("spawn_agent", "dev")])
        self.assertEqual(d.state["pending_confirm"], {})

    def test_artifact_done_fail_with_waive_goes_arbitration(self):
        """code_review done 缺 code_review.md + waive_gate → 仲裁（question 含缺少产物）"""
        req = fake_req()
        self.write_result({"status": "done", "summary": "审查通过但产物漏提交",
                           "waive_gate": "产物已由人工维护"})
        d.handle_result(req, "code_review", 0, False, time.time())
        entry = d.state["pending_confirm"]["399000"]
        self.assertEqual(entry["kind"], "confirm_override")
        self.assertIn("缺少产物 code_review.md", entry["question"])
        self.assertEqual(self.kinds("spawn_agent"), [])
        self.assertEqual(d.state["retries"], {})

    def test_artifact_done_fail_without_waive_retries(self):
        """回归：同 3 无申报 → spawn_agent(code_review) 重试，不仲裁"""
        req = fake_req()
        self.write_result({"status": "done", "summary": "s"})
        d.handle_result(req, "code_review", 0, False, time.time())
        self.assertEqual(self.kinds("spawn_agent"), [("spawn_agent", "code_review")])
        self.assertEqual(d.state["pending_confirm"], {})

    def test_waive_empty_or_nonstring_ignored(self):
        """fail-closed：waive_gate="" / 123 / 缺字段 → 均走原失败通道（不仲裁）"""
        for payload in ({"status": "done", "summary": "s", "waive_gate": ""},
                        {"status": "done", "summary": "s", "waive_gate": 123},
                        {"status": "done", "summary": "s"}):
            d.state = fresh_state()
            self.calls.clear()
            self._dev_done_no_changes(payload)
            self.assertEqual(d.state["retries"]["399000"]["count"], 1, payload)
            self.assertEqual(self.kinds("spawn_agent"), [("spawn_agent", "dev")], payload)
            self.assertNotIn("399000", d.state["pending_confirm"], payload)

    def test_done_timed_out_not_arbitrated(self):
        """回归：status=done + timed_out=True + waive_gate → 仍走超时失败通道（不仲裁）"""
        d.has_code_changes = lambda req, since: False
        req = fake_req()
        self.write_result({"status": "done", "summary": "s", "waive_gate": "理由"})
        d.handle_result(req, "dev", 0, True, time.time())
        self.assertEqual(d.state["retries"]["399000"]["count"], 1)
        self.assertEqual(d.state["pending_confirm"], {})

    def test_done_nonzero_exit_not_arbitrated(self):
        """回归：status=done + returncode=-9 → 走进程异常失败通道（不仲裁）"""
        d.has_code_changes = lambda req, since: False
        req = fake_req()
        self.write_result({"status": "done", "summary": "s", "waive_gate": "理由"})
        d.handle_result(req, "dev", -9, False, time.time())
        self.assertEqual(d.state["retries"]["399000"]["count"], 1)
        self.assertEqual(d.state["pending_confirm"], {})

    def test_waive_store_summary_todos_in_entry(self):
        """entry 携带 summary/todos（y 分支透传/展示依据）"""
        self._dev_done_no_changes({"status": "done", "summary": "完成了 X", "todos": ["浏览器验收"],
                                   "waive_gate": "理由"})
        entry = d.state["pending_confirm"]["399000"]
        self.assertEqual(entry["summary"], "完成了 X")
        self.assertEqual(entry["todos"], ["浏览器验收"])

    def test_analysis_missing_suggested_flow_falls_back_default(self):
        """#419：analysis done + 产物存在 + 无 suggested_flow → 兜底默认流程 + flow_fallback 留痕（不仲裁）"""
        (Path(self.tmp.name) / "analysis.md").write_text("需求理解")
        req = self.live(fake_req(mode=None))   # 前置阶段（未入流程）
        self.write_result({"status": "done", "summary": "分析完成", "waive_gate": "验证记录类需求"})
        d.handle_result(req, "analysis", 0, False, time.time())
        self.assertEqual(d.state["pending_confirm"], {})
        self.assertEqual(req["flow"], "heavy")
        self.assertTrue(any(c[2].get("kind") == "flow_fallback" for c in self.kinds("event")))
        choices = [c[1][1] for c in self.kinds("event") if c[2].get("kind") == "flow_choice"]
        self.assertEqual(len(choices), 1, choices)
        self.assertIn("来源：兜底默认", choices[0])
        self.assertEqual(self.kinds("spawn"), [("spawn", "tech")])

    def test_analysis_flow_choice_records_reason(self):
        """#419 选流程留档：模型推荐 + 理由 → flow_choice 事件带单行理由与来源；理由不进 summary（#342）"""
        (Path(self.tmp.name) / "analysis.md").write_text("需求理解")
        req = self.live(fake_req(mode=None))
        self.write_result({"status": "done", "summary": "分析完成", "suggested_flow": "heavy",
                           "suggested_flow_reason": "跨仓库改动且动长驻进程，需两道 agent 评审\n第二行并入"})
        d.handle_result(req, "analysis", 0, False, time.time())
        self.assertEqual(req["flow"], "heavy")
        choices = [c[1][1] for c in self.kinds("event") if c[2].get("kind") == "flow_choice"]
        self.assertEqual(len(choices), 1, choices)
        self.assertIn("推荐流程 heavy", choices[0])
        self.assertIn("来源：模型推荐", choices[0])
        self.assertIn("跨仓库改动且动长驻进程，需两道 agent 评审 第二行并入", choices[0])   # 换行压成空格
        self.assertNotIn("\n", choices[0])
        self.assertFalse(any("跨仓库改动" in str(c) for c in self.kinds("append_summary")),
                         "理由不得写入 summary（prompt 通道 #342）")

    def test_arbitration_no_set_status_change(self):
        """仲裁期间卡状态不变（仍 doing，不加 set_status/set_req 调用）"""
        self._dev_done_no_changes({"status": "done", "summary": "s", "waive_gate": "理由"})
        self.assertEqual(self.kinds("set_status"), [])
        self.assertEqual(self.kinds("set_req"), [])


class OnMessageOverrideTest(Base):
    """B. 飞书 y/n 消费（on_message confirm_override 分支）"""

    def setUp(self):
        super().setUp()
        self.req = fake_req()
        rid = str(self.req["id"])
        d.state["pending_confirm"][rid] = {
            "req": self.req, "question": "收尾校验未通过：dev 分支相对基线无代码变更。agent 申报豁免：已由人工提交。",
            "kind": "confirm_override", "summary": "完成后总结", "todos": ["浏览器验收"]}
        d.fetch_req = lambda rid2: self.req

    def reply(self, text):
        msg = mock.Mock()
        msg.message_type = "text"
        msg.chat_id = "oc_req399_test"
        msg.content = json.dumps({"text": text})
        data = mock.Mock()
        data.event.message = msg
        d.on_message(data)

    def test_y_override_finalizes_with_redline(self):
        """y → finalize_done + summary 红字「人工确认豁免」+ 校验原因、事件 kind=merge、
        pending 清理、todos 暂存 req_todos"""
        self.reply(f"y {self.req['id']}")
        self.assertEqual(self.kinds("finalize"), [("finalize", "完成后总结")])
        self.assertNotIn(str(self.req["id"]), d.state["pending_confirm"])
        summaries = [c[1][1] for c in self.kinds("append_summary")]
        self.assertTrue(any("人工确认豁免" in s and "无代码变更" in s for s in summaries))
        self.assertEqual(d.state["req_todos"][str(self.req["id"])], ["浏览器验收"])
        self.assertTrue(any(c[2].get("kind") == "merge" for c in self.kinds("event")))

    def test_n_override_backs_to_open(self):
        """n → set_req(status=open, phase=None) + clear_retries + summary「用户否决豁免」+ 不 finalize"""
        self.reply(f"n {self.req['id']} 实现不完整")
        self.assertEqual(self.kinds("finalize"), [])
        self.assertIn(("set_req", (399000, {"status": "open", "phase": None}), {}), self.kinds("set_req"))
        self.assertEqual(self.kinds("clear_retries"), [("clear_retries", 399000)])
        summaries = [c[1][1] for c in self.kinds("append_summary")]
        self.assertTrue(any("用户否决豁免" in s and "实现不完整" in s for s in summaries))
        self.assertNotIn(str(self.req["id"]), d.state["pending_confirm"])

    def test_n_override_later_keyword(self):
        """n <id> later 人工收口 → set_status(later) + summary 留痕（分支/worktree 保留语义）"""
        self.reply(f"n {self.req['id']} later 人工收口")
        self.assertEqual(self.kinds("finalize"), [])
        self.assertIn(("set_status", "later"), self.kinds("set_status"))
        summaries = [c[1][1] for c in self.kinds("append_summary")]
        self.assertTrue(any("人工否决豁免（later）" in s and "人工收口" in s and "worktree" in s
                            for s in summaries))


class PreStageFlowTest(Base):
    """C. #419 前置槽位完成：推荐流程采纳 / 兜底留痕 / confirm 项目人工确认
    （取代原 route 快通道与 manual_close——人工把关等价迁移为 need_confirm，§1.4）"""

    def _analysis_done(self, result, confirm=False):
        (Path(self.tmp.name) / "analysis.md").write_text("需求理解")
        req = self.live(fake_req(mode=None))   # 前置阶段（未入流程）
        if confirm:
            d.PROJECTS = dict(d.PROJECTS)
            d.PROJECTS["req-board"] = dict(d.PROJECTS["req-board"], confirm=True)
        self.write_result(result)
        d.handle_result(req, "analysis", 0, False, time.time())
        return req

    def reply(self, text):
        msg = mock.Mock()
        msg.message_type = "text"
        msg.chat_id = "oc_req399_test"
        msg.content = json.dumps({"text": text})
        data = mock.Mock()
        data.event.message = msg
        d.on_message(data)

    def test_suggested_flow_adopted_and_spawns_first_node(self):
        """analysis 产出 suggested_flow=heavy → 写卡快照（flow/flow_name/nodes_json/node_index=0）+ 拉起首节点"""
        req = self._analysis_done({"status": "done", "summary": "ok",
                                   "suggested_flow": "heavy", "repos": []})
        self.assertEqual(req["flow"], "heavy")
        self.assertEqual(req["flow_name"], d.flow_by_id("heavy")["name"])
        self.assertEqual(req["node_index"], 0)
        self.assertEqual(json.loads(req["nodes_json"])[0]["role"], "tech")
        self.assertEqual(self.kinds("spawn"), [("spawn", "tech")])
        self.assertEqual(d.state["pending_confirm"], {})

    def test_legacy_suggested_mode_ignored_falls_back_default(self):
        """旧字段 suggested_mode/manual_close 已退役 → 视作无推荐，兜底默认流程 + flow_fallback 留痕"""
        req = self._analysis_done({"status": "done", "summary": "ok", "suggested_mode": "manual_close"})
        self.assertEqual(req["flow"], d.default_flow_id())
        self.assertTrue(any(c[2].get("kind") == "flow_fallback" for c in self.kinds("event")))
        self.assertEqual(self.kinds("spawn"), [("spawn", "tech")])

    def test_invalid_suggested_flow_falls_back_default(self):
        req = self._analysis_done({"status": "done", "summary": "ok", "suggested_flow": "not-a-flow"})
        self.assertEqual(req["flow"], d.default_flow_id())
        self.assertEqual(self.kinds("spawn"), [("spawn", "tech")])

    def test_confirm_project_goes_need_confirm_then_y_adopts(self):
        """confirm 项目 → need_confirm 确认推荐流程（不直接 spawn）；飞书 y → 采纳并推进"""
        req = self._analysis_done({"status": "done", "summary": "ok", "suggested_flow": "heavy"},
                                  confirm=True)
        self.assertEqual(self.kinds("spawn"), [])
        entry = d.state["pending_confirm"]["399000"]
        self.assertEqual(entry["flow_id"], "heavy")
        self.assertIn("人工确认", entry["question"])
        self.reply(f"y {req['id']}")
        self.assertEqual(req["flow"], "heavy")
        self.assertEqual(self.kinds("spawn"), [("spawn", "tech")])
        self.assertNotIn("399000", d.state["pending_confirm"])

    def test_confirm_project_n_goes_later(self):
        """n → 转人工处理（置暂缓），不采纳流程、不 spawn"""
        req = self._analysis_done({"status": "done", "summary": "ok", "suggested_flow": "heavy"},
                                  confirm=True)
        self.reply(f"n {req['id']} 先人工评估")
        self.assertEqual(self.kinds("spawn"), [])
        self.assertIn(("set_status", "later"), self.kinds("set_status"))
        self.assertNotIn("399000", d.state["pending_confirm"])


class DisabledFlowTest(Base):
    """E. 流程软禁用：不进推荐目录与可选列表；被禁 id 静默兜底默认流程 + 留痕区分「已禁用」

    用例统一以 standard（非默认流程）为「被禁用」样本、heavy 为「仍启用」样本——不依赖生产配置里
    恰好是哪条被禁用，也不用默认流程（默认流程被禁用属加载期拦住的非法配置）。"""

    def _disable(self, fid):
        flows = [dict(f, enabled=False) if f["id"] == fid else f for f in d.flows()]
        return mock.patch.dict(ac.FLOW_CONFIG, {"flows": flows})

    def _analysis_done(self, result, confirm=False):
        (Path(self.tmp.name) / "analysis.md").write_text("需求理解")
        req = self.live(fake_req(mode=None))   # 前置阶段（未入流程）
        if confirm:
            d.PROJECTS = dict(d.PROJECTS)
            d.PROJECTS["req-board"] = dict(d.PROJECTS["req-board"], confirm=True)
        self.write_result(result)
        d.handle_result(req, "analysis", 0, False, time.time())
        return req

    def test_disabled_flow_not_in_catalog(self):
        with self._disable("standard"):
            txt = d._flow_catalog_text()
        self.assertNotIn("- standard", txt)
        self.assertIn("- heavy", txt)             # 其余流程照旧在目录里

    def test_disabled_suggested_flow_falls_back_with_reason(self):
        """模型仍推荐被禁流程 → 与非法 id 同路径兜底默认流程，但事件文案标明「已禁用」"""
        with self._disable("standard"):
            req = self._analysis_done({"status": "done", "summary": "ok",
                                      "suggested_flow": "standard", "repos": []})
        self.assertEqual(req["flow"], d.default_flow_id())
        self.assertEqual(self.kinds("spawn"), [("spawn", "tech")])
        texts = [c[1][1] for c in self.kinds("event")
                 if c[2].get("kind") in ("flow_fallback", "flow_choice") and len(c[1]) > 1]
        self.assertTrue(any("已禁用" in t for t in texts), texts)

    def test_disabled_flow_absent_from_confirm_choices(self):
        """need_confirm 的「可选流程」只列启用项"""
        with self._disable("standard"):
            self._analysis_done({"status": "done", "summary": "ok",
                                 "suggested_flow": "heavy", "repos": []}, confirm=True)
        question = d.state["pending_confirm"]["399000"]["question"]
        self.assertIn("heavy", question)
        self.assertNotIn("standard", question)

    def test_enabled_flow_still_adopted(self):
        """对照：未禁用的推荐流程照常采纳（防过滤把正常路径也拦掉）"""
        req = self._analysis_done({"status": "done", "summary": "ok",
                                   "suggested_flow": "standard", "repos": []})
        self.assertEqual(req["flow"], "standard")
        self.assertEqual(self.kinds("spawn"), [("spawn", "dev")])


class PromptGuidanceTest(unittest.TestCase):
    """D. prompt 同步：COMMON_TAIL 含 waive_gate 申报说明；analysis 模板三值 + 已实现类识别指引"""

    def test_common_tail_has_waive_gate_guidance(self):
        """#42 静态化：COMMON_TAIL 常量直接含 waive_gate 申报说明（不再 format、无占位符）"""
        rendered = d.COMMON_TAIL
        self.assertIn("可选字段 \"waive_gate\"", rendered)
        self.assertIn("非空即转人工裁决", rendered)
        self.assertNotIn("{result_json}", rendered)  # 静态常量无 format 占位符

    def test_analysis_prompt_has_suggested_flow_guidance(self):
        """analysis 模板含 suggested_flow 说明与已实现类识别指引（build_prompt 渲染 → 契约段按槽位注入）；
        #419：可选流程目录带适用场景（description）与节点序列，理由字段进契约"""
        req = {"id": 399, "project": "req-board", "title": "T", "notes": ""}
        wd = Path("/tmp/req399-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=""):
            prompt = d.build_prompt(req, "analysis")
        self.assertIn('"suggested_flow"', prompt)                 # #419：推荐流程字段（取代 suggested_mode）
        self.assertIn("前置环节输出契约", prompt)                  # 契约段按槽位注入（不依赖角色模板）
        self.assertIn("已实现类识别", prompt)
        self.assertIn("waive_gate", prompt)  # 经 {common} 注入
        self.assertNotIn("manual_close", prompt)                  # 退役字段不再出现在提示词
        # #419：判据来自配置——每条流程注入 description 与节点序列
        self.assertIn('"suggested_flow_reason"', prompt)
        self.assertIn("｜", prompt)
        self.assertIn(d.flow_by_id("heavy")["description"], prompt)
        self.assertIn("节点：技术方案 → 方案评审", prompt)

    def test_disabled_flow_hidden_from_prompt_catalog(self):
        """被软禁用的流程不进前置环节的可选目录（模型看不到，也就不会推荐）"""
        flows = [dict(f, enabled=False) if f["id"] == "heavy" else f for f in d.flows()]
        req = {"id": 399, "project": "req-board", "title": "T", "notes": ""}
        wd = Path("/tmp/req399-wd")
        with mock.patch.dict(ac.FLOW_CONFIG, {"flows": flows}), \
                mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=""):
            prompt = d.build_prompt(req, "analysis")
        self.assertNotIn("- heavy（复杂）", prompt)                # 目录里不再有这条流程
        self.assertIn("- standard（标准）", prompt)                # 其余流程照旧在目录里

    def test_pre_role_prompt_template_has_no_flow_names(self):
        """#419 守卫：前置角色提示词模板不得写死流程名——判据唯一落点 = flows.json 的 description。
        只匹配「名称+流程」（模板里「不得因精简缺失」这类裸词是合法的）"""
        tmpl_path = REPO / d.role_meta(d.pre_role(), "prompt_file")
        tmpl = tmpl_path.read_text(encoding="utf-8")
        for f in d.flows():
            self.assertNotIn(f"{f['name']}流程", tmpl, f"{f['id']} 的流程名不应出现在 {tmpl_path.name}")


if __name__ == "__main__":
    unittest.main()
