#!/usr/bin/env python3
"""req-422 会话头部折叠：会话记录目录解析（board 侧 glob 反查）与 GET 计算字段测试。

覆盖：
  A1-A8  resolve_session_dir：候选 home 顺序回退 / 双形态 sid / 两种目录布局容错 /
         负结果 TTL / 命中值失效复验 / 非法 sid fail-closed（全部隔离在 tmp fixture home，
         不读真实 ~/.kimi-code*）；
  A9-A11 GET /api/agent-sessions 的 session_dir 计算字段（临时库隔离）、无 schema 变更、
         POST 不接受该字段（不落库）；
  W1-W6  前端 wiring：头部统一 sessHeadHtml（两入口）、折叠态 map 回填、复制兜底、
         折叠箭头 CSS 契约（font: inherit）、路径行可点样式。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_session_dir_resolve -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402
from tests._frontend_src import backend_text, frontend_src  # noqa: E402

_baseline.pin()

import board as b  # noqa: E402

SID = "session_5966f2b3-4538-aaba-f2f4213765cb"
UUID = "5966f2b3-4538-aaba-f2f4213765cb"
MISSING = "session_00000000-0000-0000-0000-000000000000"


class SessionDirResolveTest(unittest.TestCase):
    """A1-A8：解析器（patch session_homes → tmp fixture home，真实 home 零接触）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req422-home-"))
        self.h1, self.h2 = self.tmp / "h1", self.tmp / "h2"
        for h in (self.h1, self.h2):
            (h / "sessions").mkdir(parents=True)
        b._SESSION_DIR_CACHE.clear()
        self.addCleanup(b._SESSION_DIR_CACHE.clear)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(b, "session_homes", lambda: [self.h1, self.h2])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a1_hit_first_home(self):
        """A1 命中共存目录：sessions/wd_<hash>/session_<sid>/ → 绝对路径（无尾斜杠）"""
        want = self.h1 / "sessions" / "wd_x" / SID
        want.mkdir(parents=True)
        self.assertEqual(str(want), b.resolve_session_dir(SID))

    def test_a2_fallback_second_home(self):
        """A2 多 home 顺序回退：仅在第二个候选 home 命中"""
        want = self.h2 / "sessions" / "wd_y" / SID
        want.mkdir(parents=True)
        self.assertEqual(str(want), b.resolve_session_dir(SID))

    def test_a3_miss_returns_none(self):
        """A3 未命中：返回 None 且不抛异常"""
        self.assertIsNone(b.resolve_session_dir(SID))

    def test_a4_bare_uuid_both_forms(self):
        """A4 双形态 sid：目录 session_<uuid>，入参裸 uuid / 带前缀均命中"""
        want = self.h1 / "sessions" / "wd_z" / ("session_" + UUID)
        want.mkdir(parents=True)
        self.assertEqual(str(want), b.resolve_session_dir(UUID))
        self.assertEqual(str(want), b.resolve_session_dir("session_" + UUID))

    def test_a5_flat_layout_tolerated(self):
        """A5 布局容错：目录直接位于 sessions/session_<sid>/（无 wd_<hash> 中间层）"""
        want = self.h1 / "sessions" / SID
        want.mkdir(parents=True)
        self.assertEqual(str(want), b.resolve_session_dir(SID))

    def test_a6_miss_negative_ttl(self):
        """A6 负结果 TTL：TTL 内不重复扫描；TTL 过期后命中新目录"""
        self.assertIsNone(b.resolve_session_dir(SID))
        want = self.h1 / "sessions" / "wd_x" / SID
        want.mkdir(parents=True)
        self.assertIsNone(b.resolve_session_dir(SID), "TTL 内应维持未命中（不重复扫描）")
        with mock.patch.object(b, "_SESSION_DIR_MISS_TTL", 0):
            self.assertEqual(str(want), b.resolve_session_dir(SID))

    def test_a7_hit_revalidated_when_removed(self):
        """A7 命中值失效复验：目录被清理后即时回 None（不返回已删路径）"""
        want = self.h1 / "sessions" / "wd_x" / SID
        want.mkdir(parents=True)
        self.assertEqual(str(want), b.resolve_session_dir(SID))
        shutil.rmtree(str(want))
        self.assertIsNone(b.resolve_session_dir(SID))

    def test_a8_malicious_sid_fail_closed(self):
        """A8 非法 sid fail-closed：含 / \\ .. 的形态一律 None，无越界读写"""
        for bad in ("../../etc", "/abs/path", "a/b", "a\\b", "", None, "..", "sid/../x", "x" * 201):
            self.assertIsNone(b.resolve_session_dir(bad), "不应命中: %r" % (bad,))

    def test_a9_no_real_home_touched(self):
        """隔离自证：解析全程只走 session_homes() 提供的候选（真实 home 不可达）"""
        with mock.patch.object(b, "session_homes", lambda: []):
            self.assertIsNone(b.resolve_session_dir(SID))


class AgentSessionsSessionDirApiTest(unittest.TestCase):
    """A9-A11：GET /api/agent-sessions 计算字段（临时库 + tmp home）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req422-board-"))
        cls._orig_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "test.db"
        b.init_db()
        b.app.config["TESTING"] = True
        cls.client = b.app.test_client()
        cls.req_id = cls.client.post("/api/requirements", json={
            "title": "T", "project": "req-board", "status": "later"}).get_json()["id"]

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            conn.execute("DELETE FROM agent_sessions")
            conn.commit()

    def _fixture_home(self):
        tmp = Path(tempfile.mkdtemp(prefix="req422-home-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "h"
        (home / "sessions" / "wd_a" / SID).mkdir(parents=True)
        b._SESSION_DIR_CACHE.clear()
        self.addCleanup(b._SESSION_DIR_CACHE.clear)
        return home

    def _insert(self, session_id, model, status):
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO agent_sessions (req_id, role, session_id, model, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.req_id, "dev", session_id, model, status,
                 "2026-09-18 03:00:00", "2026-09-18 03:00:00"))
            conn.commit()

    def test_a9_get_list_session_dir_field(self):
        """A9 GET 每行 session_dir：可解析→绝对路径，空会话/不可解析→null，既有字段不丢"""
        home = self._fixture_home()
        self._insert(SID, "m1", "idle")
        self._insert(None, None, "created")     # 空会话（无 sid）
        self._insert(MISSING, "m2", "idle")     # sid 存在但目录解析不到
        with mock.patch.object(b, "session_homes", lambda: [home]):
            rows = self.client.get("/api/agent-sessions").get_json()
        by_sid = {r["session_id"]: r for r in rows}
        self.assertEqual(3, len(rows))
        self.assertEqual(str(home / "sessions" / "wd_a" / SID), by_sid[SID]["session_dir"])
        self.assertIsNone(by_sid[None]["session_dir"])
        self.assertIsNone(by_sid[MISSING]["session_dir"])
        # 既有字段与会话行语义不变
        self.assertEqual("m1", by_sid[SID]["model"])
        self.assertEqual("idle", by_sid[SID]["status"])
        self.assertEqual("dev", by_sid[SID]["role"])
        self.assertEqual(self.req_id, by_sid[SID]["req_id"])

    def test_a10_no_schema_change(self):
        """A10 无 schema 变更：agent_sessions 不含 session_dir 列（纯计算字段）"""
        with b.get_db() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_sessions)")]
        self.assertNotIn("session_dir", cols)
        self.assertIn("session_id", cols)

    def test_a11_post_ignores_session_dir(self):
        """A11 POST 带 session_dir 字段被忽略：不写库、GET 仍以解析结果为准"""
        home = self._fixture_home()
        resp = self.client.post("/api/agent-sessions", json={
            "req_id": self.req_id, "role": "dev", "session_id": SID,
            "model": "m1", "session_dir": "/fake/path"})
        self.assertIn(resp.status_code, (200, 201))
        with mock.patch.object(b, "session_homes", lambda: [home]):
            rows = self.client.get("/api/agent-sessions").get_json()
        row = [r for r in rows if r["session_id"] == SID][0]
        self.assertEqual(str(home / "sessions" / "wd_a" / SID), row["session_dir"])
        self.assertNotEqual("/fake/path", row["session_dir"])


class SessHeadWiringTest(unittest.TestCase):
    """W：前端 wiring 静态断言（static/board.js 与 static/board.css）"""

    @classmethod
    def setUpClass(cls):
        cls.src = frontend_src()

    def test_w1_two_entries_share_sess_head(self):
        """W1 processHtml/agentSessionHtml 两入口共用 sessHeadHtml（头部三行 → 折叠头部）"""
        src = self.src
        self.assertIn("function sessHeadHtml(info)", src)
        self.assertIn("function sessPathRowHtml(kind, label, path)", src)
        self.assertEqual(2, src.count("const head = sessHeadHtml({"), "两个入口各调用一次")
        i_proc = src.index("function processHtml(i, sid)")
        i_agent = src.index("function agentSessionHtml(i, row)")
        self.assertIn("sessHeadHtml({", src[i_proc:i_agent])
        self.assertIn("sessHeadHtml({", src[i_agent:])

    def test_w2_session_dir_from_registry(self):
        """W2 会话目录取自 GET 计算字段：sessionList 映射 sessionDir、两入口传入 sessionDir"""
        src = self.src
        self.assertIn("sessionDir: r.session_dir || null,", src)
        self.assertIn("sessionDir: row && row.sessionDir,", src)
        self.assertIn("sessionDir: row.session_dir,", src)
        backend = backend_text()
        self.assertIn("d['session_dir'] = resolve_session_dir(d['session_id'])", backend)

    def test_w3_fold_state_map_survives_repaint(self):
        """W3 折叠态按 key 的模块级 map，渲染时决定 class（重绘不无条件重置）"""
        src = self.src
        self.assertIn("const sessHeadOpen = {};", src)
        self.assertIn("const open = !!sessHeadOpen[info.key];", src)
        self.assertIn("`<div class=\"sess-more${open ? ' open' : ''}\">`", src)
        self.assertIn("function sessHeadKey(row)", src)
        self.assertIn("function sessHeadToggle(btn)", src)
        # 点击只切 class 与字形，不重建 DOM
        i = src.index("function sessHeadToggle(btn)")
        body = src[i:i + 700]
        self.assertIn("classList.toggle('open', open)", body)
        self.assertIn("btn.textContent = open ? '▼' : '▶';", body)

    def test_w4_copy_wiring(self):
        """W4 点行复制：无视觉暗示的路径行 + 完整路径走 store + clipboard/execCommand 兜底 + toast"""
        src = self.src
        self.assertIn("onclick=\"copySessPath(this)\"", src)
        # 去掉全部可点击暗示：无 title 气泡、无手型（CSS 侧无 cursor 断言见 test_w6）
        self.assertNotIn('title="点击复制"', src)
        self.assertIn("async function copySessPath(el)", src)
        self.assertIn("navigator.clipboard && navigator.clipboard.writeText", src)
        self.assertIn("document.execCommand('copy')", src)
        self.assertIn("showToast('已复制：' + path)", src)
        self.assertIn("showToast('复制失败，请手动选择复制', true)", src)
        # 复制值来自 store（不进 data-* 属性），且是未经省略的完整路径
        self.assertIn("const sessPathStore = {};", src)
        self.assertIn("el.dataset.kind === 'session' ? st.sessionDir : st.workdir", src)

    def test_w5_arrow_css_contract(self):
        """W5 折叠箭头同款契约：纯字形实心三角 ▶/▼、无边框无底色、button 显式 font: inherit"""
        src = self.src
        i = src.index(".sess-fold {\n            font: inherit")
        rule = src[i:src.index("}", i)]
        self.assertIn("font: inherit", rule)
        self.assertIn("background: none", rule)
        self.assertIn("border: 0", rule)
        # 换字不旋转：无 transform/rotate 参与折叠箭头
        self.assertNotIn("rotate", rule)
        self.assertIn("${open ? '▼' : '▶'}", src)

    def test_w6_path_row_single_line_css(self):
        """W6 路径行恒定单行 + 中间省略 + 尾部 flex:none 完整（纯文本观感：无手型/无 hover 底色）"""
        src = self.src
        i = src.index(".sess-title, .sess-row {")
        self.assertIn("white-space: nowrap", src[i:src.index("}", i)])
        j = src.index(".sess-h {")
        rule = src[j:src.index("}", j)]
        self.assertIn("text-overflow: ellipsis", rule)
        self.assertIn("flex: 0 1000 auto", rule)   # 大收缩因子：可缩段先吃满省略
        k = src.index(".sess-label, .sess-k, .sess-created, .sess-t, .sess-fold")
        self.assertIn("flex: none", src[k:src.index("}", k)])
        # 尾部只在面板比「尾部+标签」还窄时收缩（极端宽度不溢出面板）；正常宽度保持完整
        n = src.index(".sess-row .sess-t {")
        self.assertIn("flex: 0 1 auto", src[n:src.index("}", n)])
        self.assertIn("text-overflow: ellipsis", src[n:src.index("}", n)])
        m = src.index(".sess-path {")
        rule = src[m:src.index("}", m)]
        # 纯文本观感：无手型光标、无 hover 底色（点击复制仍生效，反馈只有成功 toast）
        self.assertNotIn("cursor", rule)
        self.assertNotIn(".sess-path:hover", src)
        # #427 左轨对齐契约：hover 呼吸位 padding 保留，4px 由左负 margin 抵消（只补左侧）
        self.assertIn("padding: 1px 4px", rule)
        self.assertIn("margin-left: -4px", rule)
        # #427 标签列定宽：两行路径文字起点不因标签长度差异错位
        self.assertIn("min-width: 5em", src[src.index(".sess-k {"):src.index("}", src.index(".sess-k {"))])


if __name__ == "__main__":
    unittest.main()
