#!/usr/bin/env python3
"""#27 确认项停泊条目「键与内容同源」三层闸门测试。

改动：dispatcher.py——
- 写入期：唯一落表出口 `_put_pending_confirm`（裁剪 req 快照 + 回读校验），8 个写入路径
  （`_register_need_confirm` + 4 个重挂点）统一走它；键/内容不同源即 log.error + 事件留痕；
- 启动期：`reconcile_pending_confirm_source()`（先于 `reconcile_ghost_pending`）按键修正 +
  内容卡现取终态清账；`reconcile_ghost_pending` / `_purge_ghost_confirm` 判据改内容 id 权威；
- 消费期：三处消费点（飞书 y/n、网页确认动作、网页 merge 决策）锁内纯判定「命中不 pop」，
  出锁后 `_pending_rehang` 按内容归属归位/清理，不改动任何卡的状态。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_pending_confirm_source -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；_baseline.pin() 自动补。
"""
import json
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

_PRIVATE = ("state", "save_state", "STATE_FILE", "_state_loaded", "api", "api_board", "fetch_req",
            "notify", "post_dispatcher_event", "set_status", "set_req", "append_summary",
            "notify_req_card", "merge_conflict_clear", "confirm_decision_clear", "merge_conflict_write",
            "merge_dev_branches", "cleanup_dev_worktrees", "_set_done", "_execute_confirm_action",
            "_feishu_greet_once", "_feishu_seen_once", "_kickstart_board_if_own")
_D_REALS = {name: getattr(d, name) for name in _PRIVATE}

SNAPSHOT_KEYS = {"id", "title", "project", "status", "flow", "flow_name", "node_index"}


def fake_req(rid, status="doing", **kw):
    r = {"id": rid, "title": f"req{rid}", "project": "req-board", "status": status,
         "mode": "auto", "phase": "dev"}
    r.update(kw)
    return r


def fake_msg(text, msg_type="text"):
    content = json.dumps({"text": text}) if msg_type == "text" else "{}"
    return types.SimpleNamespace(event=types.SimpleNamespace(
        message=types.SimpleNamespace(message_type=msg_type, content=content, chat_id="oc_req27_test")))


def fresh_state():
    return {"chat_id": "oc_req27_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "pending_merge_review": {}, "sessions": {}, "worktrees": {},
            "confirm_rounds": {}, "feishu_seen": {}, "feishu_greeted": {}}


class Base(unittest.TestCase):
    """公共 mock：状态落盘走真实 save_state（tmp STATE_FILE），其余外呼全部收进 calls。"""

    def setUp(self):
        d.state = fresh_state()
        self._dir = Path(tempfile.mkdtemp(prefix="req27-state-"))
        d.STATE_FILE = self._dir / "dispatcher_state.json"
        d._state_loaded = True
        self.events = []          # (rid, kind, content)
        self.notifies = []        # notify 文本
        self.status = []          # set_status
        self.set_reqs = []        # set_req
        self.sums = []            # append_summary
        self.cards = []           # notify_req_card
        self.merge_clears = []    # merge_conflict_clear
        self.confirm_clears = []  # confirm_decision_clear
        self.execs = []           # _execute_confirm_action
        self.merges = []          # merge_dev_branches
        self.req_map = {}         # fetch_req 数据源
        self.reqs = []            # api GET 返回
        self.confirms = []        # api_board GET /confirmations
        self.conflicts = []       # api_board GET /merge-conflicts
        d.api = lambda method, path, body=None: self.reqs
        d.api_board = lambda method, path, body=None: (
            self.conflicts if "merge-conflicts" in path else self.confirms)
        d.fetch_req = lambda rid: self.req_map.get(int(rid))
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((rid, kind, content))
        d.set_status = lambda rid, s: self.status.append((rid, s))
        d.set_req = lambda rid, body: self.set_reqs.append((rid, body))
        d.append_summary = lambda rid, line: self.sums.append((rid, line))
        d.notify_req_card = lambda event, req, **k: self.cards.append((event, req.get("id"), k))
        d.merge_conflict_clear = lambda rid, reason="": self.merge_clears.append((rid, reason))
        d.confirm_decision_clear = lambda rid, reason="": self.confirm_clears.append((rid, reason))
        d.merge_conflict_write = lambda *a, **k: None
        d.merge_dev_branches = lambda req: (self.merges.append(req) or (True, []))
        d.cleanup_dev_worktrees = lambda *a, **k: None
        d._execute_confirm_action = lambda *a, **k: self.execs.append(a)
        d._set_done = lambda *a, **k: None
        d._kickstart_board_if_own = lambda *a, **k: None
        d._feishu_greet_once = lambda cid: False
        d._feishu_seen_once = lambda cid, mid: True

    def tearDown(self):
        for name, value in _D_REALS.items():
            setattr(d, name, value)
        shutil.rmtree(self._dir, ignore_errors=True)

    def event_texts(self):
        return [e[2] for e in self.events]


# ==================== 写入期（W） ====================
class WriteGateTest(Base):

    def test_w1_snapshot_crops_and_dealiases(self):
        req = fake_req(7, summary="很长的开发总结", notes="详情", nodes_json="[]")
        rounds = d._register_need_confirm("7", {"req": req, "question": "q"}, "dev")
        ent = d.state["pending_confirm"]["7"]
        self.assertEqual(set(ent["req"]), SNAPSHOT_KEYS)
        self.assertIsNot(ent["req"], req)
        req["title"] = "改了原对象"
        self.assertNotEqual(ent["req"]["title"], "改了原对象")
        self.assertEqual(rounds, 1)

    def test_w2_consistent_key_no_alarm(self):
        d._register_need_confirm("7", {"req": fake_req(7), "question": "q"}, "dev")
        self.assertIn("7", d.state["pending_confirm"])
        self.assertEqual(self.events, [])
        self.assertEqual(self.notifies, [])

    def test_w3_mismatch_not_silent(self):
        with self.assertLogs("dispatcher", level="ERROR") as cm:
            d._register_need_confirm("24", {"req": fake_req(25), "question": "q"}, "dev")
        self.assertTrue(any("不同源" in m for m in cm.output))
        self.assertTrue(any("不同源" in t for t in self.event_texts()))
        self.assertIn("24", d.state["pending_confirm"])   # 条目不丢

    def test_w4_req_none_fail_open(self):
        rounds = d._register_need_confirm("9", {"req": None, "question": "q"}, "dev")
        self.assertEqual(rounds, 1)
        self.assertIsNone(d.state["pending_confirm"]["9"]["req"])
        self.assertEqual(self.events, [])

    def test_w5_no_summary_in_snapshot(self):
        d._register_need_confirm("7", {"req": fake_req(7, summary="s"), "question": "q"}, "dev")
        self.assertNotIn("summary", d.state["pending_confirm"]["7"]["req"])

    def test_w6_put_pending_confirm_snapshot(self):
        """4 个重挂点共用的落表出口：裁剪快照 + 不动 confirm_rounds"""
        d.state["confirm_rounds"] = {"7": 4}
        entry = {"req": fake_req(7, summary="s"), "question": "q", "kind": "merge"}
        ok = d._put_pending_confirm("7", entry)
        self.assertTrue(ok)
        self.assertEqual(set(d.state["pending_confirm"]["7"]["req"]), SNAPSHOT_KEYS)
        self.assertEqual(d.state["confirm_rounds"], {"7": 4})

    def test_w7_put_pending_confirm_mismatch_returns_false(self):
        entry = {"req": fake_req(25), "question": "q", "kind": "merge"}
        self.assertFalse(d._put_pending_confirm("24", entry))
        self.assertIn("24", d.state["pending_confirm"])

    def test_w8_lock_scoped_pure_function(self):
        """持锁调用不阻塞（非重入死锁回归）+ 调用后未落盘 + 无留痕/外呼"""
        d.STATE_FILE.write_text(json.dumps({"marker": 1}))
        res = []

        def _run():
            with d.state_lock:
                res.append(d._put_pending_confirm("5", {"req": fake_req(5)}))
        t = threading.Thread(target=_run)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "持锁调用 _put_pending_confirm 阻塞（state_lock 非重入死锁）")
        self.assertEqual(res, [True])
        self.assertEqual(json.loads(d.STATE_FILE.read_text()), {"marker": 1})  # 未落盘
        self.assertEqual(self.events, [])
        self.assertEqual(self.notifies, [])


# ==================== 启动期（S） ====================
class StartupGateTest(Base):

    def test_s1_mismatched_key_rekeyed(self):
        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "Q?"}
        self.req_map[25] = fake_req(25, status="doing")
        fixed, cleared = d.reconcile_pending_confirm_source()
        self.assertEqual((fixed, cleared), (1, 0))
        self.assertNotIn("24", d.state["pending_confirm"])
        self.assertIn("25", d.state["pending_confirm"])
        self.assertTrue(any("不同源" in t for t in self.event_texts()))

    def test_s1b_rekey_keeps_confirm_rounds(self):
        d.state["confirm_rounds"] = {"24": 1, "25": 3}
        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "Q?"}
        self.req_map[25] = fake_req(25, status="doing")
        d.reconcile_pending_confirm_source()
        self.assertEqual(d.state["confirm_rounds"], {"24": 1, "25": 3})

    def test_s2_target_key_occupied_keeps_original(self):
        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "错源"}
        d.state["pending_confirm"]["25"] = {"req": fake_req(25), "question": "在途"}
        self.req_map[25] = fake_req(25, status="doing")
        fixed, cleared = d.reconcile_pending_confirm_source()
        self.assertEqual((fixed, cleared), (0, 0))
        self.assertEqual(d.state["pending_confirm"]["24"]["question"], "错源")
        self.assertEqual(d.state["pending_confirm"]["25"]["question"], "在途")
        self.assertTrue(any("保留原键" in t for t in self.event_texts()))

    def test_s3_content_done_cleared_all_kinds(self):
        for kind in ("confirm", "confirm_override", None):
            d.state["pending_confirm"] = {"31": {"req": fake_req(31), "question": "q", "kind": kind}}
            self.req_map[31] = fake_req(31, status="done")
            fixed, cleared = d.reconcile_pending_confirm_source()
            self.assertEqual((fixed, cleared), (0, 1), f"kind={kind}")
            self.assertNotIn("31", d.state["pending_confirm"])

    def test_s4_content_dropped_cleared(self):
        d.state["pending_confirm"]["41"] = {"req": fake_req(41), "question": "q"}
        self.req_map[41] = fake_req(41, status="dropped")
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 1))

    def test_s5_content_doing_kept(self):
        d.state["pending_confirm"]["51"] = {"req": fake_req(51), "question": "q"}
        self.req_map[51] = fake_req(51, status="doing")
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 0))
        self.assertIn("51", d.state["pending_confirm"])
        self.assertEqual(self.events, [])

    def test_s6_snapshot_done_but_live_doing_kept(self):
        d.state["pending_confirm"]["61"] = {"req": fake_req(61, status="done"), "question": "q"}
        self.req_map[61] = fake_req(61, status="doing")
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 0))
        self.assertIn("61", d.state["pending_confirm"])

    def test_s7_snapshot_doing_but_live_done_cleared(self):
        d.state["pending_confirm"]["71"] = {"req": fake_req(71, status="doing"), "question": "q"}
        self.req_map[71] = fake_req(71, status="done")
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 1))
        self.assertNotIn("71", d.state["pending_confirm"])

    def test_s8_req_missing_fail_open(self):
        d.state["pending_confirm"]["81"] = {"req": None, "question": "q"}
        d.state["pending_confirm"]["82"] = {"question": "无 req"}
        with self.assertLogs("dispatcher", level="WARNING"):
            self.assertEqual(d.reconcile_pending_confirm_source(), (0, 0))
        self.assertIn("81", d.state["pending_confirm"])
        self.assertIn("82", d.state["pending_confirm"])
        self.assertEqual(self.events, [])

    def test_s9_board_unreachable_skips(self):
        d.state["pending_confirm"]["91"] = {"req": fake_req(91), "question": "q"}
        d.fetch_req = lambda rid: (_ for _ in ()).throw(RuntimeError("board down"))
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 0))
        self.assertIn("91", d.state["pending_confirm"])
        self.assertEqual(self.events, [])
        d.fetch_req = lambda rid: None
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 0))
        self.assertIn("91", d.state["pending_confirm"])

    def test_s10_idempotent(self):
        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "q"}
        self.req_map[25] = fake_req(25, status="doing")
        d.reconcile_pending_confirm_source()
        before = json.dumps(d.state["pending_confirm"], sort_keys=True)
        n_events = len(self.events)
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 0))
        self.assertEqual(json.dumps(d.state["pending_confirm"], sort_keys=True), before)
        self.assertEqual(len(self.events), n_events)

    def test_s11_consistent_non_terminal_no_event(self):
        d.state["pending_confirm"]["111"] = {"req": fake_req(111), "question": "q"}
        self.req_map[111] = fake_req(111, status="doing")
        d.reconcile_pending_confirm_source()
        self.assertEqual(self.events, [])
        self.assertEqual(self.notifies, [])

    def test_s12_entry_not_dict_skipped(self):
        d.state["pending_confirm"] = {"295": True}
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 0))
        self.assertIs(d.state["pending_confirm"]["295"], True)
        self.assertEqual(self.events, [])

    def test_s13_old_functions_boundary(self):
        """#394 红线边界：新函数清终态内容条目；老函数对 confirm/confirm_override 仍不清理"""
        d.state["pending_confirm"]["131"] = {"req": fake_req(131), "question": "q"}
        d.state["pending_confirm"]["132"] = {"req": fake_req(132), "question": "q",
                                            "kind": "confirm_override"}
        self.req_map[131] = fake_req(131, status="done")
        self.req_map[132] = fake_req(132, status="done")
        self.assertEqual(d.reconcile_pending_confirm_source(), (0, 2))
        self.assertEqual(d.state["pending_confirm"], {})
        # 老函数（reconcile_ghost_pending）：done 卡 + confirm/confirm_override 仍保留
        d.state["pending_confirm"]["133"] = {"req": fake_req(133), "question": "q"}
        d.state["pending_confirm"]["134"] = {"req": fake_req(134), "question": "q",
                                             "kind": "confirm_override"}
        d.reqs = [fake_req(133, status="done"), fake_req(134, status="done")]
        d.reconcile_ghost_pending()
        self.assertIn("133", d.state["pending_confirm"])
        self.assertIn("134", d.state["pending_confirm"])

    def test_s14_order_regression(self):
        """键=#24(done)、内容=#25(doing)、kind=merge、目标键 #25 已占用 → 两函数跑完条目仍在"""
        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "错源", "kind": "merge"}
        d.state["pending_confirm"]["25"] = {"req": fake_req(25), "question": "在途", "kind": "merge"}
        self.req_map[25] = fake_req(25, status="doing")
        d.reqs = [fake_req(24, status="done"), fake_req(25, status="doing")]
        d.reconcile_pending_confirm_source()
        d.reconcile_ghost_pending()
        self.assertIn("24", d.state["pending_confirm"])
        self.assertIn("25", d.state["pending_confirm"])

    def test_s15_set_done_keeps_mismatched_entry(self):
        """_set_done（卡 A 收尾）不清内容属在途卡 B 的 merge 条目；反向：键==内容仍清"""
        d._set_done = _D_REALS["_set_done"]
        d.state["pending_confirm"]["141"] = {"req": fake_req(142), "question": "q", "kind": "merge"}
        d._set_done(fake_req(141, status="doing"))
        self.assertIn("141", d.state["pending_confirm"])
        self.assertTrue(any("不一致" in t for t in self.event_texts()))
        self.assertFalse(d._purge_ghost_confirm("141", "直接调用"))
        self.assertIn("141", d.state["pending_confirm"])
        # 反向：键 == 内容、kind=merge、卡 done → 仍被清（既有行为不变）
        d.state["pending_confirm"]["143"] = {"req": fake_req(143), "question": "q", "kind": "merge"}
        d._set_done(fake_req(143, status="doing"))
        self.assertNotIn("143", d.state["pending_confirm"])
        self.assertTrue(any(c[0] == "143" for c in self.merge_clears))


# ==================== 消费期（C） ====================
class ConsumeGateTest(Base):

    def _mismatch_setup(self, content_status="doing", target_occupied=False, text="n 24",
                        entry=None):
        if entry is None:
            entry = {"req": fake_req(25), "question": "Q25?", "role": "tech"}
        d.state["pending_confirm"]["24"] = entry
        if target_occupied:
            d.state["pending_confirm"]["25"] = {"req": fake_req(25), "question": "在途"}
        self.req_map[25] = fake_req(25, status=content_status)
        d.on_message(fake_msg(text))

    def test_c1_feishu_n_mismatch_rejected(self):
        self._mismatch_setup()
        self.assertEqual(self.status, [])
        self.assertEqual(self.set_reqs, [])
        self.assertEqual(self.sums, [])
        self.assertTrue(any("不一致" in t for t in self.notifies))
        self.assertTrue(any("不一致" in t for t in self.event_texts()))
        self.assertNotIn("24", d.state["pending_confirm"])
        self.assertIn("25", d.state["pending_confirm"])

    def test_c2_feishu_y_mismatch_rejected(self):
        self._mismatch_setup(text="y 24")
        self.assertEqual(self.status, [])
        self.assertEqual(self.sums, [])
        self.assertIn("25", d.state["pending_confirm"])

    def test_c3_mismatch_content_done_cleared(self):
        self._mismatch_setup(content_status="done")
        self.assertEqual(self.status, [])
        self.assertNotIn("24", d.state["pending_confirm"])
        self.assertNotIn("25", d.state["pending_confirm"])
        self.assertTrue(any("已清理" in t for t in self.notifies))

    def test_c4_mismatch_target_occupied_keeps_and_counts(self):
        self._mismatch_setup(target_occupied=True)
        self.assertEqual(d.state["pending_confirm"]["24"]["src_conflict_hits"], 1)
        self.assertEqual(d.state["pending_confirm"]["25"]["question"], "在途")
        self.assertTrue(any("保留原键" in t for t in self.notifies))

    def test_c5_req_none_fail_open_regression(self):
        """{"req": None} 注入：n 退 open、y 走原分支（与改动前逐字一致）"""
        d.state["pending_confirm"]["90"] = {"req": None, "question": "Q?"}
        d.on_message(fake_msg("n 90"))
        self.assertEqual(self.status, [(90, "open")])
        self.assertNotIn("90", d.state["pending_confirm"])

    def test_c6_web_confirm_mismatch(self):
        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "Q?"}
        self.req_map[25] = fake_req(25, status="doing")
        self.confirms = [{"req_id": 24, "action": "stop_later", "kind": "confirm"}]
        d.reconcile_confirmations()
        self.assertEqual(self.status, [])
        self.assertEqual(self.execs, [])
        self.assertTrue(any(c[0] == "24" for c in self.confirm_clears))
        self.assertIn("25", d.state["pending_confirm"])
        self.assertTrue(any("不一致" in t for t in self.event_texts()))

    def test_c7_web_merge_mismatch(self):
        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "Q?", "kind": "merge"}
        self.req_map[25] = fake_req(25, status="doing")
        self.reqs = [fake_req(24, status="doing"), fake_req(25, status="doing")]
        self.conflicts = [{"req_id": 24, "action": "retry"}]
        d.reconcile_decisions()
        self.assertEqual(self.merges, [])
        self.assertEqual(self.status, [])
        self.assertTrue(any(c[0] == "24" for c in self.merge_clears))
        self.assertIn("25", d.state["pending_confirm"])
        self.assertTrue(any("不一致" in t for t in self.event_texts()))

    def test_c8_rekeyed_entry_consumable(self):
        d.state["pending_confirm"]["25"] = {"req": fake_req(25), "question": "Q?"}
        self.req_map[25] = fake_req(25, status="doing")
        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "Q?"}
        d.on_message(fake_msg("n 24"))            # 归位到 25
        self.assertIn("25", d.state["pending_confirm"])
        d.on_message(fake_msg("n 25"))            # 归位后正确编号可消费
        self.assertEqual(self.status, [(25, "open")])
        self.assertNotIn("25", d.state["pending_confirm"])

    def test_c9_three_strikes_convergence(self):
        entry = {"req": fake_req(25), "question": "Q25?", "role": "tech"}
        for _ in range(3):
            self._mismatch_setup(target_occupied=True, entry=entry)
        self.assertNotIn("24", d.state["pending_confirm"])
        last = self.notifies[-1]
        self.assertIn("Q25?", last)
        self.assertIn("#25", last)
        self.assertIn("已清理", last)
        n_before = len(self.notifies)
        d.on_message(fake_msg("n 24"))   # 第 4 次：条目已清，走「没有待确认」分支
        self.assertIn("没有待确认", self.notifies[-1])
        self.assertGreater(len(self.notifies), n_before)

    def test_c10_mismatch_helper_fail_open(self):
        self.assertEqual(d._pending_key_mismatch(True, "295"), (False, None))
        self.assertEqual(d._pending_key_mismatch({"req": None}, "90"), (False, None))
        self.assertEqual(d._pending_key_mismatch({"req": {"id": "x"}}, "90"), (False, None))
        self.assertEqual(d._pending_key_mismatch({"req": {"id": 25}}, "24"), (True, 25))

    def test_c11_no_window_between_judge_and_rehang(self):
        """判定→归位期间条目仍在 state（并发消费者看到的是「有条目」），归位后盘上已是新键"""
        probed = []

        def probe(rid):
            probed.append(d.state["pending_confirm"].get("24") is not None)
            return self.req_map.get(int(rid))

        d.state["pending_confirm"]["24"] = {"req": fake_req(25), "question": "Q?"}
        self.req_map[25] = fake_req(25, status="doing")
        d.fetch_req = probe
        d.on_message(fake_msg("n 24"))
        self.assertEqual(probed, [True])
        on_disk = json.loads(d.STATE_FILE.read_text())["pending_confirm"]
        self.assertIn("25", on_disk)
        self.assertNotIn("24", on_disk)


# ==================== 复现（R） ====================
class ReproduceTest(Base):

    def test_r1_two_cards_same_process(self):
        d._register_need_confirm("61", {"req": fake_req(61), "question": "Q61?"}, "dev")
        d._register_need_confirm("62", {"req": fake_req(62), "question": "Q62?"}, "dev")
        self.assertEqual(d.state["pending_confirm"]["61"]["req"]["id"], 61)
        self.assertEqual(d.state["pending_confirm"]["62"]["req"]["id"], 62)
        d.on_message(fake_msg("n 61"))
        self.assertEqual(self.status, [(61, "open")])
        self.assertNotIn("61", d.state["pending_confirm"])
        self.assertEqual(d.state["pending_confirm"]["62"]["req"]["id"], 62)

    def test_r2_mismatch_caught_at_write_and_consume(self):
        d._register_need_confirm("24", {"req": fake_req(25), "question": "Q25?"}, "dev")
        self.assertTrue(any("不同源" in t for t in self.event_texts()))   # 写入期
        self.req_map[25] = fake_req(25, status="doing")
        n = len(self.events)
        d.on_message(fake_msg("n 24"))
        self.assertEqual(self.status, [])                                 # 消费期：不改任何卡
        self.assertGreater(len(self.events), n)                           # 消费期留痕
        self.assertIn("25", d.state["pending_confirm"])


# ==================== 启动序列端到端（真实 state 文件往返） ====================
class StartupRoundTripTest(Base):

    def test_restart_roundtrip_fixes_history(self):
        """历史错键条目（键 #24 / 内容 #25）经真实 state 文件 load → 启动对账 → 落盘 → 可消费"""
        d.state = fresh_state()
        d.STATE_FILE.write_text(json.dumps({
            "chat_id": None, "retries": {}, "pending_route": {}, "pending_review": {},
            "sessions": {}, "worktrees": {}, "confirm_rounds": {"24": 1, "25": 3},
            "pending_confirm": {"24": {"req": {"id": 25, "title": "T", "project": "req-board",
                                               "status": "doing"}, "question": "Q25?",
                                      "kind": "merge"}}}))
        d.load_state()
        self.req_map[25] = fake_req(25, status="doing")
        d.reqs = [fake_req(24, status="done"), fake_req(25, status="doing")]
        d.reconcile_pending_confirm_source()
        d.reconcile_ghost_pending()
        on_disk = json.loads(d.STATE_FILE.read_text())
        self.assertNotIn("24", on_disk["pending_confirm"])
        self.assertIn("25", on_disk["pending_confirm"])
        self.assertEqual(on_disk["confirm_rounds"], {"24": 1, "25": 3})
        self.assertTrue(any("不同源" in t for t in self.event_texts()))
        # 归位后按正确编号可消费；按旧编号不再误改任何卡
        d.on_message(fake_msg("n 24"))
        self.assertIn("没有待确认", self.notifies[-1])
        d.on_message(fake_msg("n 25"))
        self.assertEqual(self.status, [(25, "later")])   # kind=merge 的 n = 人工接管置暂缓


if __name__ == "__main__":
    unittest.main()
