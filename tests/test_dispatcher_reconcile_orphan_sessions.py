#!/usr/bin/env python3
"""req-374 孤儿会话清算：reconcile_orphan_sessions 启动时标记无持有者 running 为 interrupted。

现场（#373）：调度器 kill 重启后，被杀进程的 agent_sessions 记录永久停在 running（僵尸、
无持有者）；被覆盖键（review:370 被新会话覆盖）的旧会话同样无人认领。

修复（仅 dispatcher.py 新增 reconcile_orphan_sessions，main() reconcile_startup 后调用）：
- 扫 GET /api/agent-sessions 中 status='running' 的行；
- 持有者判定：state.sessions 中「role:req_id」键（含旧格式「req_id」键）存在且
  session_id_of(entry) == 行 session_id → 有持有者（resume 路径），不动；
- 键不存在 / id 不匹配（被覆盖键）→ POST {id, status:"interrupted"} + phase 调度事件
  「调度器重启，会话 X 中断清理」；
- 空 session_id 行跳过（无法比对持有者）；看板不可达整体跳过；单行失败降级继续。

本测试为 L0 单测（mock 副作用，沿用 tests/test_dispatcher_reconcile_startup.py 风格）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_dispatcher_reconcile_orphan_sessions -v
    # 或与全量一起：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


def row(rid, role, sid, status="running", db_id=None):
    return {"id": db_id or (rid * 10), "req_id": rid, "role": role,
            "session_id": sid, "status": status}


def closed_body(rid, role, db_id, status):
    """#421 后置状态写入改走 _close_session_row（sync_agent_session 形态的 POST 体：
    带 req_id/role + turn_step='' 清态）"""
    return {"req_id": rid, "role": role, "id": db_id, "status": status, "turn_step": ""}


class _AliveProc:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class L0ReconcileOrphanSessions(unittest.TestCase):
    """L0 单测：mock api_board/state/post_dispatcher_event，校验清算/持有者保护（#388 扩展：
    有持有者键但进程确死同样标 interrupted）/降级"""

    # 本类会替换的 dispatcher 模块属性，tearDown 统一恢复（防污染同进程后续测试）
    _MUTATED = ("state", "api_board", "post_dispatcher_event", "log", "active_runs")

    def setUp(self):
        self._orig = {name: getattr(d, name) for name in self._MUTATED}
        d.state = {"sessions": {}}
        d.active_runs = {}  # 空 = 重启场景（进程确死）
        d.log = mock.Mock()
        self.posts = []   # api_board POST 调用记录 (method, path, body)
        self.events = []  # post_dispatcher_event 调用记录 (req_id, content, kwargs)

        def fake_board(method, path, body=None):
            if method == "POST":
                self.posts.append((method, path, body))
                return {"id": body["id"]}
            return self.get_rows

        self.get_rows = []
        d.api_board = fake_board
        d.post_dispatcher_event = lambda rid, content, **kw: self.events.append((rid, content, kw))

    def tearDown(self):
        for name, val in self._orig.items():
            setattr(d, name, val)

    # ---- 验收 1：无持有键的 running → 标 interrupted + phase 事件 ----
    def test_orphan_running_marked_interrupted_with_event(self):
        """无持有者键的 running 行（如被打死收尾的 dev 会话）→ POST interrupted + 事件"""
        self.get_rows = [row(370, "dev", "session_dead", db_id=205)]
        d.reconcile_orphan_sessions()
        # #421：写体走 _close_session_row（带 req_id/role + turn_step=''）
        self.assertEqual(self.posts, [("POST", "/agent-sessions", closed_body(370, "dev", 205, "interrupted"))])
        self.assertEqual(len(self.events), 1)
        rid, content, kw = self.events[0]
        self.assertEqual(rid, 370)
        self.assertIn("session_dead", content)
        self.assertEqual(kw, {"kind": "phase"})

    # ---- 验收 2：合法持有者且进程存活不动（键存在且 id 匹配 + active_runs poll() is None）----
    def test_held_running_not_touched(self):
        """state 持有键存在且 session_id 匹配 + 进程存活（resume/正常运行）→ 不 POST 不事件"""
        self.get_rows = [row(374, "analysis", "session_39a063b0", db_id=213)]
        d.state["sessions"] = {"analysis:374": {"id": "session_39a063b0"}}
        d.active_runs = {"374": {"proc": _AliveProc(None)}}
        d.reconcile_orphan_sessions()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.events, [])

    def test_old_format_owner_key_matching(self):
        """旧格式持有者键（str(req_id) 纯数字键，role 为空）+ 进程存活 → 匹配不动"""
        self.get_rows = [row(370, "", "session_parent", db_id=205)]
        d.state["sessions"] = {"370": "session_parent"}
        d.active_runs = {"370": {"proc": _AliveProc(None)}}
        d.reconcile_orphan_sessions()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.events, [])

    # ---- #388 扩展：有持有者键但进程确死（重启残留）→ 同样标 interrupted ----
    def test_held_but_dead_process_interrupted(self):
        """有持有者键但 active_runs 无记录（重启残留僵尸）→ 标 interrupted + phase 事件"""
        self.get_rows = [row(386, "analysis", "session_dead_held", db_id=215)]
        d.state["sessions"] = {"analysis:386": {"id": "session_dead_held"}}
        d.reconcile_orphan_sessions()
        self.assertEqual(self.posts, [("POST", "/agent-sessions", closed_body(386, "analysis", 215, "interrupted"))])
        self.assertEqual([e[0] for e in self.events], [386])

    def test_held_but_poll_not_none_interrupted(self):
        """有持有者键且 active_runs 有记录但 proc.poll() 非 None（进程确死）→ 标 interrupted"""
        self.get_rows = [row(386, "analysis", "session_dead_held2", db_id=216)]
        d.state["sessions"] = {"analysis:386": {"id": "session_dead_held2"}}
        d.active_runs = {"386": {"proc": _AliveProc(1)}}
        d.reconcile_orphan_sessions()
        self.assertEqual(self.posts, [("POST", "/agent-sessions", closed_body(386, "analysis", 216, "interrupted"))])

    # ---- 被覆盖键：键存在但 session_id 不匹配 → 清（#370 现场）----
    def test_overwritten_key_orphan_cleaned(self):
        """被覆盖键：state 键存在但指向新会话（session_id 不匹配），旧 running 行无人认领 → 清"""
        self.get_rows = [row(370, "review", "session_4bbb1a35", db_id=205)]
        d.state["sessions"] = {"review:370": {"id": "session_0dca777e"}}
        d.reconcile_orphan_sessions()
        # #421：写体走 _close_session_row（带 req_id/role + turn_step=''）
        self.assertEqual(self.posts, [("POST", "/agent-sessions", closed_body(370, "review", 205, "interrupted"))])
        self.assertEqual(len(self.events), 1)

    # ---- 非 running / 空 session_id 不动 ----
    def test_non_running_untouched(self):
        """idle/created 行不参与清算"""
        self.get_rows = [row(370, "dev", "session_x", status="idle", db_id=205),
                         row(371, "dev", "session_y", status="created", db_id=206)]
        d.reconcile_orphan_sessions()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.events, [])

    def test_empty_session_id_running_skipped(self):
        """session_id 为空的异常 running 行无法比对持有者 → 跳过不误清"""
        self.get_rows = [row(370, "dev", None, db_id=205)]
        d.reconcile_orphan_sessions()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.events, [])

    # ---- 多行混合：只清无持有者/进程确死的 ----
    def test_mixed_rows_only_orphans_cleared(self):
        """混有持有者存活行/孤儿行/空闲行：仅孤儿行被标 interrupted，其余不动"""
        self.get_rows = [row(370, "review", "session_4bbb1a35", db_id=205),
                         row(370, "review", "session_0dca777e", db_id=207),
                         row(374, "analysis", "session_39a063b0", db_id=213),
                         row(375, "dev", "session_idle", status="idle", db_id=214)]
        d.state["sessions"] = {"review:370": {"id": "session_0dca777e"},
                               "analysis:374": {"id": "session_39a063b0"}}
        d.active_runs = {"370": {"proc": _AliveProc(None)},
                         "374": {"proc": _AliveProc(None)}}
        d.reconcile_orphan_sessions()
        self.assertEqual(self.posts, [("POST", "/agent-sessions", closed_body(370, "review", 205, "interrupted"))])
        self.assertEqual([e[0] for e in self.events], [370])

    # ---- 降级：看板不可达 / 单行 POST 失败 ----
    def test_board_unreachable_skips_without_crash(self):
        """GET /agent-sessions 抛异常（看板不可达）→ 整体跳过，不崩不误写"""
        def boom(method, path, body=None):
            raise RuntimeError("board down")
        d.api_board = boom
        d.reconcile_orphan_sessions()  # 不抛异常即通过
        self.assertEqual(self.posts, [])
        self.assertEqual(self.events, [])
        d.log.warning.assert_called_once()

    def test_post_failure_degrades_to_next_row(self):
        """单行清算写失败（看板瞬时不可达）→ 该行不崩，其余行继续；#421 起 phase 事件照发
        （_close_session_row 内部吞掉写失败，只降级告警——留痕不因一次写失败而丢）"""
        def flaky_board(method, path, body=None):
            if method == "GET":
                return self.get_rows
            self.posts.append((method, path, body))
            if body["id"] == 205:
                raise RuntimeError("board down")
            return {"id": body["id"]}
        d.api_board = flaky_board
        self.get_rows = [row(370, "dev", "session_a", db_id=205),
                         row(371, "dev", "session_b", db_id=206)]
        d.reconcile_orphan_sessions()  # 不抛异常即通过
        self.assertEqual(self.posts[1], ("POST", "/agent-sessions", closed_body(371, "dev", 206, "interrupted")))
        self.assertEqual([e[0] for e in self.events], [370, 371])


if __name__ == "__main__":
    unittest.main()
