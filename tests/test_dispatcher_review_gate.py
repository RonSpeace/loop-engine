#!/usr/bin/env python3
"""req-337 合并前只读 diff 审查闸门测试（#419 起为流程内的 review 节点，#423 后评审
角色统一为 code_review）。

改动：dev valid_done 后不再直接 finalize_done 合并，而是按卡上快照推进到评审节点，
拉起只读 code_review 会话（READONLY_ROLES 档位）审 dev 分支相对基线 diff：
- 审查通过（done + verdict=pass + code_review.md 产物）→ finalize_done 放行合并；
- 发现问题（verdict=reject）→ 自动打回 dev 复修（#419 起不再走人工三档裁决）；
- 审查会话自身失败 → 走通用重试（role=code_review）→ 耗尽转 later，全程不直通合并（fail-closed）。
role_allowed 按快照当前节点放行（#279 角色守卫）。

#343 防复发：REQ_BOARD_API 缺省值改不可达地址（不再默认连生产 8899）；GateBase 钉
api/api_board sentinel，任何未 mock 干净的看板外呼路径直接 AssertionError（fail-closed）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_review_gate -v

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

# 各测试类会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "set_req", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "fetch_req", "_spawn_safe", "spawn_agent", "finalize_done",
    "clear_retries", "has_code_changes", "artifact_dir", "mark_session_poisoned",
    "_gen_review_diff", "api", "api_board")}


def _forbid_real_api(*a, **k):
    """sentinel（#343）：测试期间禁止任何真实看板外呼——看板请求全部收敛于 api/api_board，
    未 mock 干净的路径一旦命中直接 AssertionError（fail-closed），不会向看板发真实请求"""
    raise AssertionError("测试命中未 mock 的看板外呼路径（api/api_board），禁止真实请求")


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fresh_state():
    return {"chat_id": "oc_req337_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "pending_merge_review": {}, "sessions": {}, "worktrees": {}}


# #419 卡上流程快照（dev → code_review → terminal 两节点流程；评审已是普通节点，#423 起
# 两条内置流程共用 code_review 角色与 code_review.md 产物）
AUTO_NODES = [{"role": "dev", "node_type": "code"},
              {"role": "code_review", "node_type": "review", "artifact": "code_review.md"},
              {"node_type": "terminal"}]


def fake_req(rid=337000, mode="auto", phase=None, snapshot=None, flow=None):
    """snapshot=(nodes, index) 写卡上流程快照；否则按 mode/phase + flow 走历史懒映射"""
    req = {"id": rid, "project": "req-board", "title": "T", "status": "doing",
           "nodes_json": None, "node_index": None}
    if mode:
        req["mode"] = mode
    if phase:
        req["phase"] = phase
    if flow:
        req["flow"] = flow
    if snapshot is not None:
        nodes, idx = snapshot
        req["nodes_json"] = json.dumps(nodes, ensure_ascii=False)
        req["node_index"] = idx
        req.pop("mode", None)
        req.pop("phase", None)
    return req


class GateBase(unittest.TestCase):
    """公共 mock：看板写入/通知/事件全部收进 self.calls，产物目录落临时目录；
    api/api_board 钉 sentinel（#343）：任何未 mock 干净的看板外呼路径直接 AssertionError"""

    def setUp(self):
        d.state = fresh_state()
        self.calls = []
        self.tmp = tempfile.TemporaryDirectory()
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
        _restore_d()
        self.tmp.cleanup()

    def live(self, req):
        """set_req 写卡 + fetch_req 回读反映同一张卡（模拟真实看板的写后回读校验）"""
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


class RoleAllowedReviewTest(GateBase):
    """#419：review 已是普通评审节点——放行按卡上快照当前节点（#337 待审标记特判退役）"""

    def test_review_allowed_only_at_review_node(self):
        at_dev = fake_req(snapshot=(AUTO_NODES, 0))
        self.assertFalse(d.role_allowed(at_dev, "code_review"))
        at_review = fake_req(snapshot=(AUTO_NODES, 1))
        self.assertTrue(d.role_allowed(at_review, "code_review"))

    def test_other_roles_rejected_at_review_node(self):
        at_review = fake_req(snapshot=(AUTO_NODES, 1))
        self.assertFalse(d.role_allowed(at_review, "dev"))
        self.assertFalse(d.role_allowed(at_review, "accept"))
        at_dev = fake_req(snapshot=(AUTO_NODES, 0))
        self.assertTrue(d.role_allowed(at_dev, "dev"))


class ArtifactOkReviewTest(GateBase):
    def test_missing_artifact_rejected(self):
        ok, why = d.artifact_ok(fake_req(), "code_review")
        self.assertFalse(ok)
        self.assertIn("code_review.md", why)

    def test_artifact_present_ok(self):
        (Path(self.tmp.name) / "code_review.md").write_text("审查通过")
        ok, _ = d.artifact_ok(fake_req(), "code_review")
        self.assertTrue(ok)


class DevDoneGateTest(GateBase):
    def test_dev_done_auto_advances_to_review_node(self):
        """卡上快照 dev（code 节点）done：不直接合并——快照推进到 review 节点并拉起 review 会话"""
        d.has_code_changes = lambda req, since: True
        req = self.live(fake_req(snapshot=(AUTO_NODES, 0)))
        self.write_result({"status": "done", "summary": "完成了 X"})
        d.handle_result(req, "dev", 0, False, time.time())
        self.assertEqual(self.kinds("finalize"), [])
        self.assertEqual(req["node_index"], 1)          # #419：按快照推进，非 pending_merge_review 标记
        self.assertEqual(self.kinds("spawn"), [("spawn", "code_review")])

    def test_dev_done_standard_advances_to_code_review(self):
        """standard（标准流程）dev done → 推进到 code_review 节点（#384 起仅补写 diff.patch，不拦合并）"""
        d.has_code_changes = lambda req, since: True
        d._gen_review_diff = lambda r: self.calls.append(("gen_diff",))
        req = self.live(fake_req(mode="semi", phase="dev", flow="standard"))
        self.write_result({"status": "done", "summary": "s"})
        d.handle_result(req, "dev", 0, False, time.time())
        self.assertEqual(self.kinds("spawn"), [("spawn", "code_review")])
        self.assertEqual(req["node_index"], 1)          # standard：dev(0) → code_review(1)
        self.assertEqual(self.kinds("gen_diff"), [("gen_diff",)])


class SemiDevDoneDiffTest(GateBase):
    """#384：semi 通道 dev done 后与 auto 对齐——调度器代写 diff.patch（三点 diff），
    写入发生在 phase=accept 推进（合并收口）之前；无实际 diff 时不写文件（#383 语义复用）"""

    def _run_semi_dev_done(self):
        d.has_code_changes = lambda req, since: True
        req = self.live(fake_req(mode="semi", phase="dev", flow="standard"))
        self.write_result({"status": "done", "summary": "s"})
        real = d._gen_review_diff

        def tracker(r):
            self.calls.append(("gen_diff",))
            return real(r)

        d._gen_review_diff = tracker
        with mock.patch.object(d, "_branch_base", return_value="base123"), \
             mock.patch.object(d, "dev_branch", return_value="f-x"), \
             mock.patch.object(d.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="@@ -1 +1 @@\n+semi done")
            d.handle_result(req, "dev", 0, False, time.time())

    def test_semi_dev_done_writes_diff_patch(self):
        """semi dev done 后产物目录出现 diff.patch，内容为 dev 分支相对基线三点 diff"""
        self._run_semi_dev_done()
        f = Path(self.tmp.name) / "diff.patch"
        self.assertTrue(f.exists())
        text = f.read_text()
        self.assertIn("base123...f-x", text)
        self.assertIn("semi done", text)
        self.assertEqual(self.kinds("spawn"), [("spawn", "code_review")])

    def test_diff_written_before_next_node_advance(self):
        """写入时机在快照推进到下一节点（code_review）之前（相对基线存在时）——验收通过后 finalize_done 合并，diff 恒有效"""
        self._run_semi_dev_done()
        idx_gen = next(i for i, c in enumerate(self.calls) if c[0] == "gen_diff")
        idx_adv = next(i for i, c in enumerate(self.calls)
                       if c[0] == "set_req" and c[1][1].get("node_index") == 1)
        self.assertLess(idx_gen, idx_adv)

    def test_semi_dev_done_no_diff_no_file(self):
        """#383 回归：无实际 diff（parts 空）时不写 diff.patch，semi 同样不落空文件"""
        d.has_code_changes = lambda req, since: True
        req = self.live(fake_req(mode="semi", phase="dev", flow="standard"))
        self.write_result({"status": "done", "summary": "s"})
        with mock.patch.object(d, "_branch_base", return_value="base123"), \
             mock.patch.object(d, "dev_branch", return_value="f-x"), \
             mock.patch.object(d.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            d.handle_result(req, "dev", 0, False, time.time())
        self.assertFalse((Path(self.tmp.name) / "diff.patch").exists())
        self.assertEqual(self.kinds("spawn"), [("spawn", "code_review")])


class ReviewResultTest(GateBase):
    """#419：review 节点完成 → pass 前进（末节点则内联收尾 finalize）；verdict 缺失按 reject"""

    def setUp(self):
        super().setUp()
        self.req = self.live(fake_req(snapshot=(AUTO_NODES, 1)))   # 卡在 review 节点
        d.state["pending_merge_review"][str(self.req["id"])] = {"req": self.req, "summary": "dev 摘要"}

    def test_review_pass_finalize(self):
        """审查通过（done + verdict=pass + code_review.md）→ 推进到 terminal → 内联收尾 finalize"""
        (Path(self.tmp.name) / "code_review.md").write_text("审查通过：范围覆盖/无夹带")
        self.write_result({"status": "done", "summary": "审查覆盖点 …", "verdict": "pass"})
        d.handle_result(self.req, "code_review", 0, False, time.time())
        self.assertEqual(self.kinds("finalize")[0][0], "finalize")
        self.assertEqual(self.req["node_index"], 2)     # 已推进到 terminal（收尾内联执行）

    def test_review_done_without_artifact_rejected(self):
        """声称 done 但缺 code_review.md → 按失败处理（重试 role=code_review），不直通合并"""
        self.write_result({"status": "done", "summary": "没写产物"})
        with mock.patch("time.sleep", lambda s: None):
            d.handle_result(self.req, "code_review", 0, False, time.time())
        self.assertEqual(self.kinds("finalize"), [])
        self.assertEqual(self.kinds("spawn_agent"), [("spawn_agent", "code_review")])

    def test_review_reject_auto_bounces_to_dev(self):
        """#419 评审语义：发现问题 = done + verdict=reject → 自动打回上游 dev（resume），
        不落 review_gate 人工裁决、不合并"""
        (Path(self.tmp.name) / "code_review.md").write_text("结论：打回\na.py 夹带改动")
        self.write_result({"status": "done", "summary": "夹带改动：a.py 顺手重构", "verdict": "reject"})
        d.handle_result(self.req, "code_review", 0, False, time.time())
        rid = str(self.req["id"])
        self.assertEqual(self.kinds("finalize"), [])
        self.assertNotIn(rid, d.state["pending_confirm"])            # 不转人工
        self.assertEqual(d.state["flow_review_rounds"][rid], {"1": 1})   # 轮次键 = 评审节点下标
        self.assertEqual(self.req["node_index"], 0)                  # 指针回退到 dev（先回退再 resume）
        self.assertEqual(self.kinds("spawn"), [("spawn", "dev")])
        self.assertTrue(d.role_allowed(self.req, "dev"))

    def test_review_need_confirm_blocks_merge(self):
        """评审自身无法完成（need_confirm）→ 普通 confirm（不再写入 review_gate kind），不合并"""
        self.write_result({"status": "need_confirm", "question": "发现夹带改动：a.py 顺手重构"})
        d.handle_result(self.req, "code_review", 0, False, time.time())
        self.assertEqual(self.kinds("finalize"), [])
        entry = d.state["pending_confirm"][str(self.req["id"])]
        self.assertIn("夹带", entry["question"])
        self.assertIsNone(entry.get("kind"))

    def test_review_session_failure_fail_closed(self):
        """审查会话自身失败（无结果文件）：自动重试 role=review；耗尽转 later，全程不直通合并"""
        with mock.patch("time.sleep", lambda s: None):
            d.handle_result(self.req, "code_review", 1, False, time.time())  # 第 1 次失败 → 重试
            self.assertEqual(self.kinds("spawn_agent"), [("spawn_agent", "code_review")])
            self.assertEqual(self.kinds("finalize"), [])
            d.handle_result(self.req, "code_review", 1, False, time.time())  # 第 2 次失败 → 耗尽
        self.assertEqual(self.kinds("finalize"), [])
        self.assertIn(("set_status", "later"), self.kinds("set_status"))
        self.assertTrue(self.kinds("poison"))


class OnMessageReviewGateTest(GateBase):
    def setUp(self):
        super().setUp()
        self.req = fake_req()
        rid = str(self.req["id"])
        d.state["pending_merge_review"][rid] = {"req": self.req, "summary": "dev 摘要"}
        d.state["pending_confirm"][rid] = {"req": self.req, "question": "发现夹带改动",
                                           "kind": "review_gate"}
        d.fetch_req = lambda rid2: self.req

    def reply(self, text):
        msg = mock.Mock()
        msg.message_type = "text"
        msg.chat_id = "oc_req337_test"
        msg.content = json.dumps({"text": text})
        data = mock.Mock()
        data.event.message = msg
        d.on_message(data)

    def test_approve_merges_with_findings_logged(self):
        """y → 人工确认仍合并：finalize_done 放行 + 审查发现红字留档，标记清理"""
        self.reply(f"y {self.req['id']}")
        self.assertEqual(self.kinds("finalize"), [("finalize", "dev 摘要")])
        self.assertNotIn(str(self.req["id"]), d.state["pending_merge_review"])
        summaries = [c[1][1] for c in self.kinds("append_summary")]
        self.assertTrue(any("人工确认仍合并" in s and "夹带" in s for s in summaries))

    def test_reject_blocks_merge_to_later(self):
        """n → 否决合并转 later，分支/worktree 保留，finalize 不调用，标记清理"""
        self.reply(f"n {self.req['id']} 需要修复夹带")
        self.assertEqual(self.kinds("finalize"), [])
        self.assertIn(("set_status", "later"), self.kinds("set_status"))
        self.assertNotIn(str(self.req["id"]), d.state["pending_merge_review"])
        summaries = [c[1][1] for c in self.kinds("append_summary")]
        self.assertTrue(any("人工否决合并" in s for s in summaries))

    def test_rework_reply_resumes_dev_with_feedback(self):
        """#411：n <id> 转开发 → 保持 doing（无状态变更）、pop pending_merge_review、
        resume 原 dev 会话且 extra 含 merge_review.md 审查反馈、summary+事件各留痕一条"""
        (Path(self.tmp.name) / "merge_review.md").write_text("审查发现夹带改动 a.py 需拆除")
        d._spawn_safe = lambda req, role, **k: self.calls.append(("spawn", role, k))
        self.reply(f"n {self.req['id']} 转开发：修复夹带")
        self.assertEqual(self.kinds("finalize"), [])
        self.assertNotIn(("set_status", "later"), self.kinds("set_status"))  # 保持 doing
        spawn_calls = self.kinds("spawn")
        self.assertEqual(len(spawn_calls), 1)
        self.assertEqual(spawn_calls[0][1], "dev")
        self.assertTrue(spawn_calls[0][2].get("resume"))
        self.assertIn("夹带改动 a.py", spawn_calls[0][2].get("extra", ""))
        self.assertNotIn(str(self.req["id"]), d.state["pending_merge_review"])
        summaries = [c[1][1] for c in self.kinds("append_summary")]
        self.assertTrue(any("人工转开发复修" in s for s in summaries))
        events = [c[1][1] for c in self.kinds("event")]
        self.assertTrue(any("转开发" in e for e in events))

    def test_rework_reply_without_review_md_injects_question_only(self):
        """#411：merge_review.md 缺失（产物异常）→ 降级仅注入问题文本，不崩"""
        d._spawn_safe = lambda req, role, **k: self.calls.append(("spawn", role, k))
        self.reply(f"n {self.req['id']} 转开发")
        spawn_calls = self.kinds("spawn")
        self.assertEqual(spawn_calls[0][1], "dev")
        self.assertIn("发现夹带改动", spawn_calls[0][2].get("extra", ""))
        self.assertNotIn("merge_review.md）：", spawn_calls[0][2].get("extra", ""))


class OnMessageReviewRoundsDecisionTest(GateBase):
    """#419 评审轮次超限人工裁决：n 打回前驱时人工原因 + 评审产物经 extra 注入前驱续跑会话
    （resume 路径 build_prompt 被丢弃，仅 extra 生效——不注入则前驱看不到打回原因）"""

    REVIEW_NODES = [{"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                    {"role": "tech_review", "node_type": "review", "artifact": "tech_review.md"},
                    {"node_type": "terminal"}]

    def setUp(self):
        super().setUp()
        self.req = self.live(fake_req(snapshot=(self.REVIEW_NODES, 1)))
        rid = str(self.req["id"])
        d.state["flow_review_rounds"] = {rid: {"1": d.FLOW_REVIEW_ROUNDS_MAX}}
        d.state["pending_confirm"][rid] = {"req": self.req,
                                           "question": f"连续 {d.FLOW_REVIEW_ROUNDS_MAX} 轮评审未通过",
                                           "review_index": 1}
        (Path(self.tmp.name) / "tech_review.md").write_text("结论：打回\n- 缺 X 证据")
        self.board_posts = []
        d.api_board = lambda method, path, body=None: (
            self.board_posts.append((method, path, body)), {"ok": True})[1]
        self.spawns = []
        d._spawn_safe = lambda req, role, extra="", resume=False: self.spawns.append(
            (str(req["id"]), role, resume, extra))

    def reply(self, text):
        msg = mock.Mock()
        msg.message_type = "text"
        msg.chat_id = "oc_req419_test"
        msg.content = json.dumps({"text": text})
        data = mock.Mock()
        data.event.message = msg
        d.on_message(data)

    def test_manual_reject_bounces_with_reason_and_artifact(self):
        self.reply(f"n {self.req['id']} 缺证据补齐")
        self.assertEqual(self.req["node_index"], 0)          # 指针先回退前驱（tech）
        self.assertEqual(len(self.spawns), 1)
        rid, role, resume, extra = self.spawns[0]
        self.assertEqual((rid, role, resume), (str(self.req["id"]), "tech", True))
        self.assertIn("缺证据补齐", extra)                    # 人工原因送达前驱会话
        self.assertIn("结论：打回", extra)                    # 评审产物全文送达
        self.assertIn(f"第 {d.FLOW_REVIEW_ROUNDS_MAX} 轮", extra)   # 轮次文案随上限走，不写死数字


class GenReviewDiffTest(GateBase):
    def test_diff_written_and_truncated(self):
        """_gen_review_diff：调度器代写 diff.patch；超 REVIEW_DIFF_MAX_LINES 截断并如实标注"""
        req = fake_req()
        big = "@@ -1 +1 @@\n" + "\n".join(f"+line{i}" for i in range(d.REVIEW_DIFF_MAX_LINES + 50))
        with mock.patch.object(d, "_branch_base", return_value="base123"), \
             mock.patch.object(d, "dev_branch", return_value="f-x"), \
             mock.patch.object(d.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=big)
            d._gen_review_diff(req)
        f = Path(self.tmp.name) / "diff.patch"
        self.assertTrue(f.exists())
        text = f.read_text()
        self.assertIn("base123...f-x", text)
        self.assertIn("已截断", text)
        self.assertLessEqual(len(text.splitlines()), d.REVIEW_DIFF_MAX_LINES + 5)

    def test_no_diff_does_not_write_file(self):
        """#383：无实际 diff（parts 为空）时不写 diff.patch——语义收敛为「文件在=有内容」，
        前端按 exists 渲染分支差异产物行，文件不存在时显示「产物缺失（路径不存在）」提示（#53）；
        存量占位文件不回改"""
        req = fake_req()
        with mock.patch.object(d, "_branch_base", return_value="base123"), \
             mock.patch.object(d, "dev_branch", return_value="f-x"), \
             mock.patch.object(d.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            d._gen_review_diff(req)
        self.assertFalse((Path(self.tmp.name) / "diff.patch").exists())


class NoRealApiSentinelTest(GateBase):
    def test_sentinel_blocks_real_board_calls(self):
        """#343 防复发：sentinel 生效——未 mock 的看板外呼（set_status / agent-sessions 注册
        等收敛于 api/api_board 的路径）直接 AssertionError，不会向看板发任何真实请求"""
        with self.assertRaises(AssertionError):
            d.api("PATCH", "/279", {"status": "doing"})
        with self.assertRaises(AssertionError):
            d.api_board("POST", "/agent-sessions", {})


class ReadonlyRoleTest(unittest.TestCase):
    def test_review_in_readonly_roles(self):
        import acp_shared
        self.assertIn("code_review", acp_shared.READONLY_ROLES)
        self.assertIn("code_review", d.ROLE_NAMES)
        # #423 合并：原 review 角色已删除，只读评审只剩 tech_review / code_review
        self.assertNotIn("review", acp_shared.READONLY_ROLES)


if __name__ == "__main__":
    unittest.main()
