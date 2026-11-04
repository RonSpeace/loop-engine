#!/usr/bin/env python3
"""req-400 网页确认交互 board 端点 L1 测试。

改动：board.py——
- confirm_decisions 表（CREATE TABLE IF NOT EXISTS）
- GET /api/confirmations：读 dispatcher_state.json 三 pending 字典（try/except 失败 200+[]），
  联 confirm_decisions 表透出 action（已提交待处理态）；role 推断（pending_confirm 落表 role /
  pending_route→analysis / pending_review→phase）
- POST /api/requirements/<id>/confirmations：action 白名单按 kind；防幽灵 404；409 幂等；
  confirm 的 pass/rework 仅评审轮次超限，stop_later 仅超限态
- DELETE /api/requirements/<id>/confirmations：幂等清除（调度器消费后调用）

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_confirmations_api -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402


class ConfirmationsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-confirmations-api-"))
        b.DB_PATH = cls.tmp / "board.db"
        b.DATA_DIR = cls.tmp
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            conn.execute("DELETE FROM confirm_decisions")
            conn.execute("DELETE FROM requirements")
            conn.commit()
        self.state_file = self.tmp / "dispatcher_state.json"
        if self.state_file.exists():
            self.state_file.unlink()

    def _add_req(self, status="doing", mode="auto"):
        r = b.req_add("#400 测试", "req-board", notes="n", status=status, mode=mode)
        return r["id"]

    def _write_state(self, rid, **entry):
        pending = {"pending_confirm": {}, "pending_route": {}, "pending_review": {}}
        if "kind" in entry or "question" in entry:
            pending["pending_confirm"][str(rid)] = entry
        else:
            pending["pending_route"][str(rid)] = entry
        self.state_file.write_text(json.dumps(pending))

    # ---- GET 基本 ----
    def test_get_empty_and_missing_state(self):
        self.assertEqual(self.client.get('/api/confirmations').get_json(), [])
        self.state_file.write_text("broken")
        self.assertEqual(self.client.get('/api/confirmations').get_json(), [])

    def test_get_confirm_row_fields(self):
        """普通 need_confirm：question/options/role/action=null 透出（action 无未消费行=null）"""
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm",
                          options=[{"label": "继续修", "hint": "继续"}], role="dev")
        rows = self.client.get('/api/confirmations').get_json()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["req_id"], r["kind"], r["question"], r["role"], r["action"])
                         == (rid, "confirm", "Q?", "dev", None), True)
        self.assertEqual(r["options"], [{"label": "继续修", "hint": "继续"}])
        self.assertEqual(r["status"], "doing")

    def test_get_route_review_role_inference(self):
        """route→analysis；review→phase（被评审完成角色）；问题兜底文案生成"""
        rid1 = self._add_req(status="route", mode="semi")
        rid2 = self._add_req(status="review", mode="semi")
        state = {"pending_confirm": {}, "pending_route": {str(rid1): {"req": {"id": rid1},
                                                                     "suggested_mode": "semi"}},
                 "pending_review": {str(rid2): {"req": {"id": rid2}, "phase": "tech"}}}
        self.state_file.write_text(json.dumps(state))
        rows = {r["req_id"]: r for r in self.client.get('/api/confirmations').get_json()}
        self.assertEqual(rows[rid1]["kind"], "route")
        self.assertEqual(rows[rid1]["role"], "analysis")
        self.assertIn("半自动", rows[rid1]["question"])
        self.assertEqual(rows[rid2]["kind"], "review")
        self.assertEqual(rows[rid2]["role"], "tech")
        self.assertIn("tech", rows[rid2]["question"])

    # ---- POST ----
    def test_post_review_gate_rework_202(self):
        """review_gate + rework → 202；普通 confirm 无 review_index 仍拒绝 rework"""
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="审查?", kind="review_gate")
        res = self.client.post(f'/api/requirements/{rid}/confirmations',
                               json={"action": "rework"})
        self.assertEqual(res.status_code, 202)
        with b.get_db() as conn:
            row = conn.execute("SELECT * FROM confirm_decisions WHERE req_id = ?", (rid,)).fetchone()
            self.assertEqual((row["kind"], row["action"]), ("review_gate", "rework"))
        rid2 = self._add_req()
        self._write_state(rid2, req={"id": rid2}, question="Q?", kind="confirm")
        self.assertEqual(self.client.post(f'/api/requirements/{rid2}/confirmations',
                                          json={"action": "rework"}).status_code, 400)

    def test_post_review_cap_confirm_pass_rework_202(self):
        """评审轮次超限 confirm：pass/rework 与飞书 y/n 对齐，GET 透出 review_index"""
        for action in ("pass", "rework"):
            rid = self._add_req()
            self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm",
                              review_index=1, role="tech_review")
            row = self.client.get('/api/confirmations').get_json()[0]
            self.assertEqual(row["review_index"], 1)
            res = self.client.post(
                f'/api/requirements/{rid}/confirmations',
                json={"action": action, "reason": "按评审意见重做" if action == "rework" else "同意"},
            )
            self.assertEqual(res.status_code, 202)
            with b.get_db() as conn:
                stored = conn.execute(
                    "SELECT * FROM confirm_decisions WHERE req_id = ?", (rid,)
                ).fetchone()
                self.assertEqual((stored["kind"], stored["action"]),
                                 ("confirm", action))

    def test_post_review_gate_pass_202(self):
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="审查?", kind="review_gate")
        res = self.client.post(f'/api/requirements/{rid}/confirmations',
                               json={"action": "pass"},
                               headers={"X-Actor": "manual:%E7%BD%91%E9%A1%B5%E7%A1%AE%E8%AE%A4-pass"})
        self.assertEqual(res.status_code, 202)
        with b.get_db() as conn:
            row = conn.execute("SELECT * FROM confirm_decisions WHERE req_id = ?", (rid,)).fetchone()
            self.assertEqual((row["kind"], row["action"]), ("review_gate", "pass"))
            # 真实浏览器 fetch 经 encodeURIComponent 发送编码串；_header_str 会按字节还原 + 百分号解码，
            # 落库为还原后的中文（#375 审计可读性——与直接设中文头等价）
            self.assertEqual(row["actor"], "manual:网页确认-pass")

    def test_get_no_kind_normalized_to_confirm(self):
        """真实落表形态（普通 need_confirm 无 kind 字段）→ GET 归一 kind=confirm：
        前端确认视图/按钮排及超限 stop_later 按钮均按 kind==='confirm' 渲染"""
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", over_limit=True)
        rows = self.client.get('/api/confirmations').get_json()
        self.assertEqual(rows[0]["kind"], "confirm")
        self.assertTrue(rows[0]["over_limit"])
        self.assertEqual(rows[0]["action"], None)

    def test_post_confirm_stop_later_requires_overlimit(self):
        """confirm 非超限态 stop_later → 400；超限态 → 202"""
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm")
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "stop_later"}).status_code, 400)
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm", over_limit=True)
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "stop_later"}).status_code, 202)

    def test_post_confirm_pass_rework_without_review_index_400(self):
        """普通 confirm 不能借网页按钮绕过角色会话"""
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm")
        for action in ("pass", "rework"):
            self.assertEqual(
                self.client.post(
                    f'/api/requirements/{rid}/confirmations',
                    json={"action": action},
                ).status_code,
                400,
            )

    def test_post_plain_confirm_proceed_202(self):
        """普通 confirm 可由网页 proceed 同意继续；评审超限 confirm 不允许 proceed"""
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="允许共享包吗？", kind="confirm")
        res = self.client.post(
            f'/api/requirements/{rid}/confirmations',
            json={"action": "proceed", "reason": "共享包已合入，允许继续"},
        )
        self.assertEqual(res.status_code, 202)
        self._write_state(rid, req={"id": rid}, question="评审超限", kind="confirm",
                          review_index=1)
        self.assertEqual(
            self.client.post(
                f'/api/requirements/{rid}/confirmations',
                json={"action": "proceed", "reason": "不能绕过评审"},
            ).status_code,
            400,
        )

    def test_post_illegal_action_400(self):
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm_override")
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "unknown"}).status_code, 400)
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm")
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "pass"}).status_code, 400)  # confirm 不接受 pass

    def test_post_ghost_404_and_missing_req_404(self):
        """无 pending（被飞书并发消费/需求存在但 state 无条目）→ 404；需求不存在 → 404"""
        # 需求存在但 state 无 pending 条目（防幽灵路径本体：按钮已消费后迟到点击）
        rid = self._add_req()
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "pass"}).status_code, 404)
        # 需求不存在（无论 state 有无条目）→ 404
        self.assertEqual(self.client.post('/api/requirements/12345/confirmations',
                                          json={"action": "pass"}).status_code, 404)

    def test_post_duplicate_409(self):
        """已有未消费 action → 409「已提交待处理」（防连点双发）"""
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm_override")
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "pass"}).status_code, 202)
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "reject"}).status_code, 409)

    def test_get_join_action_after_post(self):
        """POST 已写 action 后 GET 该行 action 非空（已提交态透出，跨刷新不丢）"""
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm_override")
        self.client.post(f'/api/requirements/{rid}/confirmations', json={"action": "pass"})
        rows = self.client.get('/api/confirmations').get_json()
        self.assertEqual(rows[0]["action"], "pass")

    # ---- DELETE ----
    # ---- #25 终态卡：展示面过滤 vs 对账保留 ----
    def _add_decision(self, rid, kind="confirm", action="pass"):
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO confirm_decisions (req_id, kind, action, reason, actor, created_at, updated_at)"
                " VALUES (?, ?, ?, '', 'test', ?, ?)",
                (rid, kind, action, b.now_str(), b.now_str()))
            conn.commit()

    def test_get_marks_terminal_card(self):
        """done/dropped 行仍返回且 terminal=True（调度器对账可见）；行不被展示过滤误删"""
        rid = self._add_req(status="done")
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm")
        rows = self.client.get('/api/confirmations').get_json()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["terminal"])
        self.assertEqual(rows[0]["status"], "done")

    def test_get_non_terminal_unchanged(self):
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm")
        rows = self.client.get('/api/confirmations').get_json()
        self.assertFalse(rows[0]["terminal"])
        self.assertEqual(rows[0]["status"], "doing")

    def test_terminal_action_join_visible_to_dispatcher(self):
        """终态卡有残留 action 时 GET 仍透出 action（对账靠它识别并清账，不被展示过滤）"""
        rid = self._add_req(status="done")
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm")
        self._add_decision(rid, kind="confirm", action="pass")
        rows = self.client.get('/api/confirmations').get_json()
        self.assertEqual(rows[0]["action"], "pass")
        self.assertTrue(rows[0]["terminal"])

    def test_post_terminal_card_404(self):
        """终态卡即使 state 残留 pending 也 404；改回 doing 后按既有白名单处理（202）"""
        rid = self._add_req(status="done")
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm_override")
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "pass"}).status_code, 404)
        with b.get_db() as conn:
            conn.execute("UPDATE requirements SET status = 'doing' WHERE id = ?", (rid,))
            conn.commit()
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "pass"}).status_code, 202)

    def test_post_terminal_without_pending_404(self):
        """终态且无 state 行 → 404"""
        rid = self._add_req(status="dropped")
        self.assertEqual(self.client.post(f'/api/requirements/{rid}/confirmations',
                                          json={"action": "pass"}).status_code, 404)

    def test_pending_rows_tool_filters_terminal(self):
        """助手工具与 pending_count 不包含终态卡；非终态行字段不变"""
        live = self._add_req(status="doing")
        dead = self._add_req(status="done")
        state = {"pending_confirm": {str(live): {"req": {"id": live}, "question": "Q?", "kind": "confirm"},
                                     str(dead): {"req": {"id": dead}, "question": "Q?", "kind": "confirm"}},
                 "pending_route": {}, "pending_review": {}}
        self.state_file.write_text(json.dumps(state))
        server = object.__new__(b.AssistantMcpServer)
        with b.get_db() as conn:
            rows = server._pending_rows(conn)
        self.assertEqual([r["req_id"] for r in rows], [live])
        payload = json.loads(server.tool_pending_confirmations({}))
        self.assertEqual(payload["count"], 1)
        self.assertEqual([r["req_id"] for r in payload["pending"]], [live])
        self.assertEqual(json.loads(server.tool_sessions_status({}))["pending_count"], 1)

    def test_delete_idempotent(self):
        rid = self._add_req()
        self._write_state(rid, req={"id": rid}, question="Q?", kind="confirm_override")
        self.client.post(f'/api/requirements/{rid}/confirmations', json={"action": "pass"})
        self.assertEqual(self.client.delete(f'/api/requirements/{rid}/confirmations').status_code, 200)
        self.assertEqual(self.client.delete(f'/api/requirements/{rid}/confirmations').status_code, 200)
        rows = self.client.get('/api/confirmations').get_json()
        self.assertEqual(rows[0]["action"], None)


if __name__ == "__main__":
    unittest.main()
