#!/usr/bin/env python3
"""req-92 调度时间线新增「用户飞书回复」事件测试。

改动：on_message 成功匹配到待确认需求后、处理结果之前，追加一条时间线事件
（post_dispatcher_event），内容「用户飞书回复：同意/否决 - <原因>（<原文>）」，kind=confirm。

- L0（默认运行）：mock post_dispatcher_event / spawn 等副作用，覆盖四类回复
  （need_confirm / route 通道确认 / review 评审 / merge 合并接管）× 同意/否决，
  以及未匹配不记录、非文本消息、首条消息三个负例。
- E2E（测试板实例在跑时才运行）：真实 post_dispatcher_event → 真实看板落库，
  GET /dispatcher-events 校验事件内容与「需要确认 → 回复 → 拉起会话」顺序。

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

E2E 需先启动测试板实例（测试端口，避免与 main 8899 冲突）：
    REQ_BOARD_PORT=8902 .venv/bin/python board.py

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import socket
import sys
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

E2E_PORT = int(os.environ.get("REQ92_E2E_PORT", "8902"))
os.environ.setdefault("REQ_BOARD_API", f"http://127.0.0.1:{E2E_PORT}/api/requirements")

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402
import acp_shared as ac  # noqa: E402

# 测试会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "fetch_req", "post_dispatcher_event", "notify",
    "set_status", "append_summary", "set_req", "_spawn_safe", "spawn_agent",
    "merge_dev_branches", "cleanup_dev_worktrees", "_set_done", "finalize_done")}
_REAL_API = d.api
_REAL_POST_DISPATCHER_EVENT = d.post_dispatcher_event


def fake_msg(text, msg_type="text"):
    """构造 on_message 入参（仅用 message_type/content/chat_id 三字段）"""
    content = json.dumps({"text": text}) if msg_type == "text" else "{}"
    return types.SimpleNamespace(event=types.SimpleNamespace(
        message=types.SimpleNamespace(message_type=msg_type, content=content, chat_id="oc_req92_test")))


def fresh_state():
    return {"chat_id": "oc_req92_test", "retries": {}, "pending_confirm": {},
            "pending_route": {}, "pending_review": {}, "sessions": {}, "worktrees": {}}


class L0FeishuReplyEvent(unittest.TestCase):
    """L0 单测：mock 副作用，校验事件内容/kind 与各分支覆盖"""

    def setUp(self):
        d.state = fresh_state()
        self.events = []      # post_dispatcher_event 记录 [(kind, content), ...]
        self.spawns = []      # spawn_agent 记录 (req_id, resume)
        self.status = []      # set_status 记录
        self.notifies = []    # notify 记录
        self.events.append(("confirm", "agent 需要确认：Q?"))  # 预置时间线前序事件
        d.save_state = lambda: None
        d.fetch_req = lambda rid: {"id": rid, "project": "req-board", "mode": "auto"}
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.set_status = lambda rid, status: self.status.append((rid, status))
        d.append_summary = lambda *a, **k: None
        d.set_req = lambda *a, **k: None
        d._spawn_safe = lambda *a, **k: None
        d.spawn_agent = self._fake_spawn
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d._set_done = lambda *a, **k: None
        d.finalize_done = lambda *a, **k: None

    def _fake_spawn(self, req, extra="", resume=False):
        self.spawns.append((req["id"], resume))
        self.events.append(("spawn", f"拉起会话（{'续跑' if resume else '新会话'}）"))

    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)

    def reply_event(self):
        """返回本次回复事件（confirm kind 且内容以「用户飞书回复：」开头）"""
        for i, (kind, content) in enumerate(self.events):
            if kind == "confirm" and content.startswith("用户飞书回复："):
                return i, kind, content
        return None

    # ---- 同意 ----
    def test_t1_approve_confirm_full_loop(self):
        """验收标准 1：y 后出现「用户飞书回复：同意（y 90）」，位于需要确认与拉起会话之间"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg("y 90"))
        i, kind, content = self.reply_event()
        self.assertIsNotNone(i)
        self.assertEqual(kind, "confirm")
        self.assertEqual(content, "用户飞书回复：同意（y 90）")
        # 顺序：agent 需要确认 → 用户飞书回复 → 拉起会话（续跑）
        seq = [c for _, c in self.events]
        self.assertEqual(seq,
                         ["agent 需要确认：Q?", "用户飞书回复：同意（y 90）", "拉起会话（续跑）"])
        self.assertEqual(self.spawns, [(90, True)])  # resume=True 续跑

    def test_t2_approve_route(self):
        d.state["pending_route"]["90"] = {"req": None, "suggested_mode": "auto"}
        d.on_message(fake_msg("yes 90"))
        i, kind, content = self.reply_event()
        self.assertEqual((kind, content), ("confirm", "用户飞书回复：同意（yes 90）"))

    def test_t3_approve_review(self):
        d.state["pending_review"]["90"] = {"req": None, "phase": "tech"}
        d.on_message(fake_msg("同意 90"))
        i, kind, content = self.reply_event()
        self.assertEqual((kind, content), ("confirm", "用户飞书回复：同意（同意 90）"))

    def test_t4_approve_merge(self):
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        d.on_message(fake_msg("y 90"))
        i, kind, content = self.reply_event()
        self.assertEqual((kind, content), ("confirm", "用户飞书回复：同意（y 90）"))

    # ---- 否决（带原因 / 无原因）----
    def test_t5_reject_confirm_with_reason(self):
        """验收标准 2：n 带原因显示「用户飞书回复：否决 - <原因>（n 90 <原因>）」"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg("n 90 用 B 方案"))
        i, kind, content = self.reply_event()
        self.assertEqual((kind, content),
                         ("confirm", "用户飞书回复：否决 - 用 B 方案（n 90 用 B 方案）"))
        self.assertEqual(self.status, [(90, "open")])  # 否决退回待开发

    def test_t6_reject_confirm_no_reason(self):
        """否决无原因：沿用既有约定 reason or '未说明'"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg("n 90"))
        i, kind, content = self.reply_event()
        self.assertEqual((kind, content), ("confirm", "用户飞书回复：否决 - 未说明（n 90）"))

    def test_t7_reject_route(self):
        d.state["pending_route"]["90"] = {"req": None, "suggested_mode": "auto"}
        d.on_message(fake_msg("n 90 太复杂"))
        i, kind, content = self.reply_event()
        self.assertEqual((kind, content), ("confirm", "用户飞书回复：否决 - 太复杂（n 90 太复杂）"))

    def test_t8_reject_review(self):
        d.state["pending_review"]["90"] = {"req": None, "phase": "tech"}
        d.on_message(fake_msg("拒绝 90"))
        i, kind, content = self.reply_event()
        self.assertEqual((kind, content), ("confirm", "用户飞书回复：否决 - 未说明（拒绝 90）"))

    def test_t9_reject_merge(self):
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?", "kind": "merge"}
        d.on_message(fake_msg("n 90 人工处理"))
        i, kind, content = self.reply_event()
        self.assertEqual((kind, content), ("confirm", "用户飞书回复：否决 - 人工处理（n 90 人工处理）"))
        self.assertEqual(self.status, [(90, "later")])  # merge 接管置暂缓

    # ---- 负例 ----
    def test_t10_unmatched_not_recorded(self):
        """验收标准：未匹配到待确认项的回复不记录（首条问候会先追加一条文本）"""
        d.on_message(fake_msg("y 90"))  # pending 全空
        self.assertIsNone(self.reply_event())
        self.assertIn("没有待确认的问题", self.notifies[-1])

    def test_t11_non_text_message_ignored(self):
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg("y 90", msg_type="image"))
        self.assertIsNone(self.reply_event())

    def test_t12_first_message_greets_then_handles_same_message(self):
        """#24：首条消息问候后**继续处理本条**（不再被整条吞掉）——y 无 pending 仍回提示，
        但不记录回复事件；同一 chat 只问候一次。"""
        d.state = fresh_state()
        d.state["chat_id"] = None
        d.on_message(fake_msg("y 90"))
        self.assertIsNone(self.reply_event())
        self.assertIn("已连接", self.notifies[0])
        self.assertIn("没有待确认的问题", self.notifies[-1])

    def test_t12b_first_plain_text_greets_then_routes(self):
        """#24：首条普通文本问候后继续走路由（矩阵见 test_feishu_assistant_router），
        仍不产生「用户飞书回复」事件。"""
        d.state = fresh_state()
        d.state["chat_id"] = None
        with mock.patch.object(d, "_feishu_route_text") as route:
            d.on_message(fake_msg("帮我看看有哪些在跑"))
        self.assertIsNone(self.reply_event())
        self.assertIn("已连接", self.notifies[0])
        # #44：路由第三参 = message_id（该桩报文缺该字段 → None，不贴表情、业务照常）
        route.assert_called_once_with("oc_req92_test", "帮我看看有哪些在跑", None)

    def test_t12c_first_yn_with_pending_proceeds_after_greeting(self):
        """#24：首条 y/n 且有 pending → 问候后仍走确认路径（矩阵 TC-02）"""
        d.state = fresh_state()
        d.state["chat_id"] = None
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg("y 90"))
        self.assertIn("已连接", self.notifies[0])
        i, kind, content = self.reply_event()
        self.assertEqual(content, "用户飞书回复：同意（y 90）")
        self.assertEqual(self.spawns, [(90, True)])

    def test_t13_stripped_raw_text(self):
        """原文为 strip 后的完整消息"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg("  y 90  "))
        i, kind, content = self.reply_event()
        self.assertEqual(content, "用户飞书回复：同意（y 90）")

    # ---- 答复留档（summary = prompt 通道，会话重开后新 agent 也能看到这条澄清）----
    def test_t14_plain_confirm_approve_records_summary(self):
        d.state["pending_confirm"]["90"] = {"req": None, "question": "是指导出速度还是格式？"}
        summaries = []
        d.append_summary = lambda rid, line, **kw: summaries.append((rid, line))
        d.on_message(fake_msg("y 90 指导出速度，超过 5 万行就卡"))
        self.assertEqual(summaries,
                         [(90, "人工答复（问题：是指导出速度还是格式？）：指导出速度，超过 5 万行就卡。")])
        self.assertEqual(self.spawns, [(90, True)])      # 留档不影响照常续跑

    def test_t15_approve_without_reason_records_agree(self):
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?"}
        summaries = []
        d.append_summary = lambda rid, line, **kw: summaries.append((rid, line))
        d.on_message(fake_msg("y 90"))
        self.assertEqual(summaries, [(90, "人工答复（问题：Q?）：同意。")])

    def test_t16_flow_confirm_not_double_recorded(self):
        """前置流程确认（pend.flow_id）走自己的留档分支，不写「人工答复」行"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "推荐流程「复杂」…", "flow_id": "heavy"}
        summaries = []
        d.append_summary = lambda rid, line, **kw: summaries.append((rid, line))
        with mock.patch.object(d, "flow_by_id", lambda fid: {"id": fid, "name": "复杂"}), \
                mock.patch.object(d, "_adopt_flow_and_spawn", lambda req, fid: True):
            d.on_message(fake_msg("y 90"))
        self.assertEqual(summaries, [])


class L0RouteFlowWhitelist(unittest.TestCase):
    """#425：route 确认回复的流程名白名单配置派生（flows.json 启用流程）——
    卡上 flow 只可能取配置中存在的启用流程 id，非法/未启用只提示不写入。"""

    RID = 91

    def setUp(self):
        d.state = fresh_state()
        self.fields = []      # set_req 记录（每轮覆盖）
        self.spawns = []      # _spawn_safe 记录 (req_id, role)
        self.notifies = []
        self.flow = None
        d.save_state = lambda: None
        d.fetch_req = lambda rid: {"id": rid, "project": "req-board", "mode": "semi",
                                   "status": "open", "phase": None, "flow": self.flow}
        d.post_dispatcher_event = lambda *a, **k: None
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.set_status = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.set_req = self._set_req
        d._spawn_safe = lambda req, role, *a, **k: self.spawns.append((req["id"], role))
        d.spawn_agent = lambda *a, **k: None
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d._set_done = lambda *a, **k: None
        d.finalize_done = lambda *a, **k: None

    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)

    def _set_req(self, rid, fields):
        self.fields.append(dict(fields))
        if fields.get("flow"):
            self.flow = fields["flow"]

    def reply(self, text, suggested_mode="semi"):
        """走 route 确认分支的一轮回复，返回本轮 set_req 的字段"""
        self.fields.clear()
        d.state["pending_route"][str(self.RID)] = {"req": None, "suggested_mode": suggested_mode}
        d.on_message(fake_msg(text))
        return self.fields[-1] if self.fields else {}

    def test_l1_unknown_flow_falls_to_default(self):
        """T-L1：首词为配置中不存在的流程名（lean）→ 不写该 id，落默认流程 + 提示"""
        fields = self.reply(f"y {self.RID} lean 其它文本")
        self.assertEqual(fields["flow"], d.default_flow_id())
        self.assertTrue(d.flow_by_id(fields["flow"]) and d.flow_enabled(fields["flow"]))
        self.assertNotIn("lean", [f.get("flow") for f in self.fields])
        self.assertIn(f"未识别流程「lean」，按{d.FLOW_NAMES[d.default_flow_id()]}流程处理",
                      "".join(self.notifies))
        self.assertEqual(self.spawns, [(self.RID, "tech")])   # 按默认流程首节点推进

    def test_l2_enabled_flow_selected(self):
        """T-L2：首词为配置中启用流程 → 采用该 id，无提示后缀"""
        fields = self.reply(f"y {self.RID} heavy")
        self.assertEqual(fields["flow"], "heavy")
        text = "".join(self.notifies)
        self.assertIn("半自动·复杂流程", text)
        self.assertNotIn("未识别", text)
        self.assertNotIn("未启用", text)
        self.assertEqual(self.spawns, [(self.RID, "tech")])

    def test_l3_disabled_flow_not_written(self):
        """T-L3：流程被软禁用 → 不写该 id，落默认流程 + 「未启用」提示
        （被禁样本取非默认流程：默认流程被禁用是加载期拦住的非法配置）"""
        flows_cfg = [dict(f) for f in d.flows()]
        for f in flows_cfg:
            if f["id"] == "standard":
                f["enabled"] = False
        with mock.patch.dict(ac.FLOW_CONFIG, {"flows": flows_cfg}):
            fields = self.reply(f"y {self.RID} standard")
        self.assertEqual(fields["flow"], d.default_flow_id())
        self.assertNotIn("standard", [f.get("flow") for f in self.fields])
        self.assertIn("未启用流程「standard」", "".join(self.notifies))

    def test_l4_written_flow_always_enabled(self):
        """T-L4：写入值恒通过 flow_by_id + flow_enabled（卡上 flow 只可能取启用 id）"""
        for text in (f"y {self.RID}", f"y {self.RID} auto", f"y {self.RID} heavy",
                     f"y {self.RID} lean", f"y {self.RID} standard"):
            fields = self.reply(text)
            if "flow" in fields:
                self.assertTrue(d.flow_by_id(fields["flow"]) and d.flow_enabled(fields["flow"]),
                                f"非法 flow 写入：{fields}")

    def test_l5_auto_mode_keeps_no_flow_field(self):
        """T-L5：suggested_mode=auto（带 reason）→ 不写 flow 键（现状回归）"""
        fields = self.reply(f"y {self.RID} 任意文本", suggested_mode="auto")
        self.assertNotIn("flow", fields)
        self.assertEqual((fields["mode"], fields["status"], fields["phase"]), ("auto", "open", None))

    def test_l6_whitespace_reason_no_error(self):
        """T-L6：无 reason / 尾随空白 → 不抛异常，flow 落默认流程"""
        self.assertEqual(self.reply(f"y {self.RID}")["flow"], d.default_flow_id())
        self.assertEqual(self.reply(f"y {self.RID} \u3000")["flow"], d.default_flow_id())


def _board_up():
    try:
        with socket.create_connection(("127.0.0.1", E2E_PORT), timeout=1):
            return True
    except OSError:
        return False


@unittest.skipUnless(_board_up(), f"测试板实例未运行（REQ_BOARD_PORT={E2E_PORT}），跳过 E2E")
class E2EFeishuReplyEvent(unittest.TestCase):
    """E2E：真实 on_message → 真实 post_dispatcher_event → 真实看板落库，GET 校验"""

    @classmethod
    def setUpClass(cls):
        cls.created = []  # 待清理的需求 id
        # 全量跑时前面的测试文件可能泄漏 mock（d.api / d.post_dispatcher_event）且
        # REQ_BOARD_API 可能被前置文件 setdefault 成不可达地址（#343 后不再指向 main 8899）；
        # 这里强制恢复真实函数并把目标钉死到本测试板，E2E 只测 8902、不误伤主看板
        cls._orig = {name: getattr(d, name) for name in (
            "state", "save_state", "notify", "spawn_agent", "merge_dev_branches",
            "cleanup_dev_worktrees", "_set_done", "finalize_done", "_spawn_safe",
            "api", "post_dispatcher_event", "BOARD_API")}
        d.BOARD_API = f"http://127.0.0.1:{E2E_PORT}/api/requirements"
        d.api = _REAL_API
        d.post_dispatcher_event = _REAL_POST_DISPATCHER_EVENT
        d.state = fresh_state()
        d.save_state = lambda: None
        d.notify = lambda text, *a, **k: None
        d.spawn_agent = cls._fake_spawn
        d.merge_dev_branches = lambda req: (True, [])
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d._set_done = lambda *a, **k: None
        d.finalize_done = lambda *a, **k: None
        d._spawn_safe = lambda *a, **k: None

    @classmethod
    def tearDownClass(cls):
        for rid in cls.created:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    d.BOARD_API + f"/{rid}", method="DELETE"), timeout=5)
            except Exception:
                pass
        for name, value in cls._orig.items():
            setattr(d, name, value)

    @classmethod
    def _fake_spawn(cls, req, extra="", resume=False):
        # 模拟续跑：走真实 post_dispatcher_event 追加「拉起会话（续跑）」事件
        d.post_dispatcher_event(req["id"], "拉起「开发」会话（续跑）", kind="spawn")

    def new_req(self):
        body = json.dumps({"title": "req92-e2e", "project": "req-board", "notes": "测试", "status": "open"})
        req = urllib.request.Request(d.BOARD_API, method="POST", data=body.encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        rid = data["id"]
        self.created.append(rid)
        return rid

    def get_events(self, rid):
        url = d.BOARD_API + f"/{rid}/dispatcher-events"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())

    def test_e1_approve_confirm_full_loop(self):
        """y 后事件落库，顺序：需要确认 → 用户飞书回复：同意（y <编号>） → 拉起会话（续跑）"""
        rid = self.new_req()
        d.post_dispatcher_event(rid, "agent 需要确认：Q?", kind="confirm")
        d.state["pending_confirm"][str(rid)] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg(f"y {rid}"))
        events = [e["content"] for e in self.get_events(rid)]
        expect = ["agent 需要确认：Q?",
                  f"用户飞书回复：同意（y {rid}）",
                  "拉起「开发」会话（续跑）"]
        self.assertEqual(events, expect)
        self.assertEqual([e["kind"] for e in self.get_events(rid)],
                         ["confirm", "confirm", "spawn"])

    def test_e2_reject_confirm_with_reason(self):
        rid = self.new_req()
        d.state["pending_confirm"][str(rid)] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg(f"n {rid} 用 B 方案"))
        contents = [e["content"] for e in self.get_events(rid)]
        self.assertEqual(contents, [f"用户飞书回复：否决 - 用 B 方案（n {rid} 用 B 方案）"])

    def test_e3_reject_route(self):
        rid = self.new_req()
        d.state["pending_route"][str(rid)] = {"req": None, "suggested_mode": "auto"}
        d.on_message(fake_msg(f"n {rid} 太复杂"))
        contents = [e["content"] for e in self.get_events(rid)]
        # 回复事件必须在分支后续事件（用户否决通道建议…）之前
        self.assertEqual(contents[0], f"用户飞书回复：否决 - 太复杂（n {rid} 太复杂）")
        self.assertGreater(len(contents), 1)

    def test_e4_reject_review(self):
        rid = self.new_req()
        d.state["pending_review"][str(rid)] = {"req": None, "phase": "tech"}
        d.on_message(fake_msg(f"n {rid}"))
        contents = [e["content"] for e in self.get_events(rid)]
        self.assertEqual(contents[0], f"用户飞书回复：否决 - 未说明（n {rid}）")
        self.assertGreater(len(contents), 1)

    def test_e5_reject_merge(self):
        rid = self.new_req()
        d.state["pending_confirm"][str(rid)] = {"req": None, "question": "Q?", "kind": "merge"}
        d.on_message(fake_msg(f"n {rid} 人工处理"))
        contents = [e["content"] for e in self.get_events(rid)]
        self.assertEqual(contents[0], f"用户飞书回复：否决 - 人工处理（n {rid} 人工处理）")
        self.assertGreater(len(contents), 1)

    def test_e6_unmatched_not_recorded(self):
        """未匹配到待确认项的回复：不产生任何事件"""
        rid = self.new_req()
        d.on_message(fake_msg(f"y {rid}"))  # pending 全空
        self.assertEqual(self.get_events(rid), [])


if __name__ == "__main__":
    unittest.main()
