#!/usr/bin/env python3
"""#12 建卡对话上传本机 html 参考文件：后端上传/校验/注入/建卡收口 + 前端接线回归。

覆盖方案验收映射：
- 三入口上传 + chip → 用例 1/2/23/24/25（前端行为断言见 tests/chat_material_harness.js）
- 同轮 agent 读取：prompt 含参考文件绝对路径（用例 13/17/18；真实 kimi 链路见自测报告浏览器验证）
- 非 html/超限/非法名被拒且不发请求 → 用例 3-8/12/14/15/27
- 上传失败不阻塞纯文本发送、移除后 prompt 不含其路径 → 用例 16/26

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_chat_material_upload -v
"""
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests._frontend_src import frontend_css, frontend_js, frontend_src
from tests.test_process_markdown_merge import _extract_function

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402

MB = 1024 * 1024


class DummyLog:
    def close(self):
        pass


class FakeProc:
    pid = 424242

    def poll(self):
        return None


class FakeSession:
    """不发真实 kimi 的会话桩：prompt 立即返回并记录文本。"""

    def __init__(self):
        self.proc = FakeProc()
        self.prompts = []

    def prompt(self, text, timeout=None):
        self.prompts.append(text)

    def _flush_message(self):
        pass

    def cancel(self):
        pass

    def close(self, kill=False):
        pass


class FakeCreateServer:
    """tool_create_requirement 的 self 桩（不建真实 HTTP MCP server、不占端口）。"""

    def __init__(self, chat_id, project):
        self.chat_id = chat_id
        self.project = project


class ChatMaterialBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-chat-material-"))
        cls._old_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._old_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.materials = self.tmp / f"materials-{self._testMethodName}"
        self.artifacts = self.tmp / f"artifacts-{self._testMethodName}"
        self._old_materials, self._old_artifacts = b.MATERIALS_ROOT, b.ARTIFACTS_ROOT
        b.MATERIALS_ROOT, b.ARTIFACTS_ROOT = self.materials, self.artifacts
        self._old_paths = b.PROJECT_PATHS
        b.PROJECT_PATHS = {"req-board": [str(REPO)]}
        with b.get_db() as conn:
            conn.execute("DELETE FROM chat_events")
            conn.execute("DELETE FROM chat_sessions")
            conn.execute("DELETE FROM requirements")
            conn.commit()
        with b.chat_lock:
            b.chat_runtime.clear()
            b.chat_booting.clear()
        self.client = b.app.test_client()

    def tearDown(self):
        b.MATERIALS_ROOT, b.ARTIFACTS_ROOT = self._old_materials, self._old_artifacts
        b.PROJECT_PATHS = self._old_paths
        with b.chat_lock:
            b.chat_runtime.clear()
            b.chat_booting.clear()

    # ---- 桩与工具 ----

    def _insert(self, **fields):
        vals = {"project": "req-board", "status": "idle", "session_id": "s-1",
                "draft_title": None, "draft_notes": None, "interrupt_source": None,
                "created_at": b.now_str(), "updated_at": b.now_str()}
        vals.update(fields)
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO chat_sessions (project, status, session_id, draft_title, draft_notes, "
                "interrupt_source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (vals["project"], vals["status"], vals["session_id"], vals["draft_title"],
                 vals["draft_notes"], vals["interrupt_source"], vals["created_at"], vals["updated_at"]))
            chat_id = cur.lastrowid
            conn.commit()
        return chat_id

    def _upload(self, cid, items, **kw):
        data = {"file": [(io.BytesIO(payload), name) for payload, name in items]}
        return self.client.post(f"/api/chat/{cid}/materials", data=data,
                                content_type="multipart/form-data", **kw)

    def _saved(self, cid, project="req-board"):
        d = b._chat_materials_dir(project, cid)
        return sorted(p.name for p in d.glob("*") if p.is_file()) if d.is_dir() else []

    def _events(self, cid):
        with b.get_db() as conn:
            return [r["content"] for r in conn.execute(
                "SELECT content FROM chat_events WHERE chat_id = ? ORDER BY id", (cid,)).fetchall()]

    def _requirements(self):
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM requirements ORDER BY id").fetchall()]

    def _prompt_with_live_session(self, cid, payload):
        """挂一个活会话桩并走真实 /prompt 端点；返回 (响应, session)。"""
        s = FakeSession()
        with b.chat_lock:
            b.chat_runtime[cid] = {"session": s, "logf": DummyLog()}
        resp = self.client.post(f"/api/chat/{cid}/prompt", json=payload)
        for _ in range(300):
            if resp.status_code != 202 or s.prompts:
                break
            time.sleep(0.01)
        return resp, s


# ==================== 一、上传端点（验收：三入口 / 类型 / 大小 / 数量 / 非法名） ====================

class ChatMaterialUploadApiTest(ChatMaterialBase):

    def test_01_single_html_upload_ok(self):
        cid = self._insert()
        payload = "<html><body>hi</body></html>".encode("utf-8")
        r = self._upload(cid, [(payload, "原型.html")])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(len(body["files"]), 1)
        f = body["files"][0]
        self.assertEqual(f["filename"], "原型.html")
        self.assertEqual(f["size"], len(payload))
        self.assertTrue(Path(f["path"]).is_absolute())
        self.assertFalse(f["over_read_limit"])
        self.assertIn(str(b.MATERIALS_ROOT / "req-board" / f"chat-{cid}"), f["path"])
        self.assertEqual(self._saved(cid), ["原型.html"])
        # 未建卡时 notes/需求不因上传产生副作用
        self.assertEqual(self._requirements(), [])

    def test_02_multi_files_upload_ok(self):
        cid = self._insert()
        r = self._upload(cid, [(b"<p>a</p>", "a.html"), (b"<p>b</p>", "b.htm"), (b"<p>c</p>", "c.HTML")])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._saved(cid), ["a.html", "b.htm", "c.HTML"])

    def test_03_non_html_rejected(self):
        cid = self._insert()
        for name in ("a.txt", "a.png", "a.html.bak", "a"):
            with self.subTest(name=name):
                r = self._upload(cid, [(b"x", name)])
                self.assertEqual(r.status_code, 400, name)
                self.assertEqual(self._saved(cid), [], "非 html 必须零落盘")

    def test_04_oversize_by_read_bytes(self):
        cid = self._insert()
        r = self._upload(cid, [(b"a" * (10 * MB + 1), "big.html")])
        self.assertEqual(r.status_code, 413, r.get_data(as_text=True))
        self.assertEqual(self._saved(cid), [], "超限必须零落盘")
        # 边界：恰好 10MB 通过（读入字节判定，非 content_length）
        r2 = self._upload(cid, [(b"a" * (10 * MB), "exact.html")])
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self._saved(cid), ["exact.html"])

    def test_04b_no_content_length_not_fail_open(self):
        """content_length 缺失（chunked/未知长度）时仍按读入字节判定 413，不 fail-open。"""
        from werkzeug.test import EnvironBuilder
        cid = self._insert()
        env = EnvironBuilder(method="POST", path=f"/api/chat/{cid}/materials",
                             data={"file": (io.BytesIO(b"a" * (10 * MB + 1)), "big.html")},
                             content_type="multipart/form-data").get_environ()
        env.pop("CONTENT_LENGTH", None)
        env["wsgi.input_terminated"] = True
        env["HTTP_TRANSFER_ENCODING"] = "chunked"
        result = {}
        with b.app.request_context(env):
            resp = b.api_chat_materials(cid)
            result["status"] = resp[1] if isinstance(resp, tuple) else resp.status_code
        self.assertEqual(result["status"], 413, "无 content_length 不得放行超限文件")
        self.assertEqual(self._saved(cid), [])

    def test_05_too_many_files_rejected(self):
        cid = self._insert()
        r = self._upload(cid, [(b"x", f"f{i}.html") for i in range(6)])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._saved(cid), [], "超数量必须零落盘")

    def test_06_illegal_names_rejected(self):
        for name in ("../x.html", "../../x.html", "a/b.html", "..", "a..html", "...html"):
            with self.subTest(name=name):
                cid = self._insert()
                r = self._upload(cid, [(b"x", name)])
                self.assertEqual(r.status_code, 400, name)
                self.assertEqual(self._saved(cid), [])
        # 反斜杠：werkzeug 3.1 在解析阶段就把 `a\b.html` 归一成 `ab.html`（到不了服务端），
        # 故该形态只能在 helper 单元层断言（防御纵深，不改 HTTP 行为）
        self.assertFalse(b._material_name_ok("a\\b.html"))
        self.assertTrue(b._material_name_ok("a-b.html"))

    def test_06b_percent_encoded_extended_filename_rejected(self):
        """RFC2231 扩展形 filename* 会被 werkzeug 解码成 ../x.html → 必须拒（防穿越）。"""
        cid = self._insert()
        body = (b"--B\r\n"
                b"Content-Disposition: form-data; name=\"file\"; filename*=UTF-8''%2e%2e%2fx.html\r\n"
                b"Content-Type: text/html\r\n\r\n<p>x</p>\r\n--B--\r\n")
        r = self.client.post(f"/api/chat/{cid}/materials", data=body,
                             content_type="multipart/form-data; boundary=B")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._saved(cid), [])
        # 对照：普通形 filename 不做百分号解码，字面名按安全逻辑落到会话目录内（resolve 兜底）
        r2 = self._upload(cid, [(b"x", "%2e%2e%2fx.html")])
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self._saved(cid), ["%2e%2e%2fx.html"])
        self.assertTrue(Path(r2.get_json()["files"][0]["path"]).is_relative_to(
            (b.MATERIALS_ROOT / "req-board" / f"chat-{cid}").resolve()))

    def test_07_unicode_and_space_names_accepted(self):
        cid = self._insert()
        r = self._upload(cid, [(b"x", "我的 原型(v2).html")])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._saved(cid), ["我的 原型(v2).html"])

    def test_08_bad_names_rejected(self):
        cid = self._insert()
        for name in ("", "   ", "a\nb.html", "a\tb.html", ("x" * 200) + ".html"):
            with self.subTest(name=repr(name)):
                r = self._upload(cid, [(b"x", name)])
                self.assertEqual(r.status_code, 400, repr(name))
                self.assertEqual(self._saved(cid), [])

    def test_09_missing_session_404_created_409(self):
        r = self._upload(999999, [(b"x", "a.html")])
        self.assertEqual(r.status_code, 404)
        cid = self._insert(status="created", draft_title="t")
        r2 = self._upload(cid, [(b"x", "a.html")])
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(self._saved(cid), [])

    def test_10_duplicate_name_suffix(self):
        cid = self._insert()
        self._upload(cid, [(b"one", "a.html")])
        r = self._upload(cid, [(b"two", "a.html")])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["files"][0]["filename"], "a-1.html")
        self.assertEqual(self._saved(cid), ["a-1.html", "a.html"])
        self.assertEqual((b._chat_materials_dir("req-board", cid) / "a.html").read_bytes(), b"one")
        self.assertEqual((b._chat_materials_dir("req-board", cid) / "a-1.html").read_bytes(), b"two")

    def test_11_project_traversal_confined(self):
        cid = self._insert(project="../evil")
        r = self._upload(cid, [(b"x", "a.html")])
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertFalse((self.materials.parent / "evil").exists(), "project 带 .. 不得逃出 MATERIALS_ROOT")
        paths, err = b._chat_material_paths("../evil", cid, ["a.html"])
        self.assertIsNone(paths)
        self.assertIn("非法", err)

    def test_12_body_limit_early_exit(self):
        """content_length 已大于整包上限 → 早退 413（拼装体大小不参与判定，直调视图构造超大长度）。"""
        from werkzeug.test import EnvironBuilder
        cid = self._insert()
        env = EnvironBuilder(method="POST", path=f"/api/chat/{cid}/materials",
                             data={"file": (io.BytesIO(b"x"), "a.html")},
                             content_type="multipart/form-data").get_environ()
        env["CONTENT_LENGTH"] = str(b.CHAT_MATERIAL_BODY_LIMIT + 1)
        with b.app.request_context(env):
            resp = b.api_chat_materials(cid)
        self.assertEqual(resp[1] if isinstance(resp, tuple) else resp.status_code, 413)
        self.assertEqual(self._saved(cid), [])

    def test_12b_empty_request_rejected(self):
        cid = self._insert()
        r = self.client.post(f"/api/chat/{cid}/materials", data={},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)


# ==================== 二、prompt 注入（验收：同轮可读 / 移除后不含 / 超限拒） ====================

class ChatMaterialPromptTest(ChatMaterialBase):

    def test_13_prompt_injects_absolute_paths_and_event_prefix(self):
        cid = self._insert()
        self._upload(cid, [("<html>原型</html>".encode("utf-8"), "原型.html")])
        resp, s = self._prompt_with_live_session(cid, {"text": "看下这个原型", "attachments": ["原型.html"]})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(s.prompts), 1)
        prompt = s.prompts[0]
        abs_path = str((b.MATERIALS_ROOT / "req-board" / f"chat-{cid}" / "原型.html").resolve())
        self.assertIn(abs_path, prompt)
        self.assertIn("read_file", prompt)
        self.assertIn("看下这个原型", prompt)
        evs = self._events(cid)
        user_ev = [e for e in evs if "看下这个原型" in e][0]
        self.assertTrue(user_ev.startswith("📎 参考文件："), user_ev[:60])
        self.assertIn("原型.html", user_ev)
        # 正文不内联：html 内容不得出现在 prompt 里
        self.assertNotIn("<html>原型</html>", prompt)

    def test_14_invalid_attachment_rejected_without_prompting(self):
        cid = self._insert()
        s = FakeSession()
        with b.chat_lock:
            b.chat_runtime[cid] = {"session": s, "logf": DummyLog()}
        for bad in (["../x.html"], ["a/b.html"], ["nope.html"], ["a.txt"], [""]):
            with self.subTest(bad=bad):
                r = self.client.post(f"/api/chat/{cid}/prompt", json={"text": "hi", "attachments": bad})
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(s.prompts, [], "非法附件不得发起请求")
        self.assertEqual(self._events(cid), [], "拒绝时不得写 user 事件")

    def test_15_cumulative_attachment_cap_server_side(self):
        """单轮上限的服务端闸：无论前端怎么分次上传，一次 prompt 带 >5 个一律 400。"""
        cid = self._insert()
        names = [f"f{i}.html" for i in range(6)]
        self._upload(cid, [(b"x", n) for n in names[:3]])   # 分两次各 3 个（绕不过累计口径）
        self._upload(cid, [(b"x", n) for n in names[3:]])
        self.assertEqual(self._saved(cid), sorted(names))
        s = FakeSession()
        with b.chat_lock:
            b.chat_runtime[cid] = {"session": s, "logf": DummyLog()}
        r = self.client.post(f"/api/chat/{cid}/prompt", json={"text": "hi", "attachments": names})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(s.prompts, [])
        # 恰好 5 个放行
        resp, s2 = self._prompt_with_live_session(cid, {"text": "hi", "attachments": names[:5]})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(s2.prompts), 1)
        self.assertTrue(s2.prompts[0].count("- ") >= 5)

    def test_16_no_attachments_unchanged_and_upload_failure_not_blocking(self):
        """无 attachments 走现状逐字；上传端点失败不影响纯文本发送（两个独立请求）。"""
        cid = self._insert()
        bad = self._upload(cid, [(b"x", "a.txt")])
        self.assertEqual(bad.status_code, 400)
        resp, s = self._prompt_with_live_session(cid, {"text": "只有文字"})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(s.prompts[0].strip().split("\n")[-1], "只有文字")
        user_ev = [e for e in self._events(cid) if "只有文字" in e][0]
        self.assertEqual(user_ev, "只有文字", "无附件时事件内容逐字不变")
        # attachments 非字符串数组 → 400
        r = self.client.post(f"/api/chat/{cid}/prompt", json={"text": "x", "attachments": [1, 2]})
        self.assertEqual(r.status_code, 400)

    def test_17_first_round_system_prompt_then_attachment_block(self):
        cid = self._insert()
        self._upload(cid, [(b"x", "a.html")])
        resp, s = self._prompt_with_live_session(cid, {"text": "首轮", "attachments": ["a.html"]})
        self.assertEqual(resp.status_code, 202)
        prompt = s.prompts[0]
        self.assertIn("你是需求看板（req-board）的建卡助手", prompt)
        self.assertLess(prompt.index("你是需求看板"), prompt.index("本轮参考文件"))
        self.assertLess(prompt.index("用户消息："), prompt.index("本轮参考文件"))
        # 系统提示词含参考文件使用约定
        self.assertIn("不要内联", prompt)

    def test_18_long_text_event_keeps_manifest_prefix(self):
        cid = self._insert()
        self._upload(cid, [(b"x", "a.html")])
        text = "长" * 4100
        r = self.client.post(f"/api/chat/{cid}/prompt", json={"text": text, "attachments": ["a.html"]})
        self.assertEqual(r.status_code, 400)   # 正文上限保持 4000（现状不变）
        long_ok = "长" * 3900
        with b.chat_lock:
            b.chat_runtime[cid] = {"session": FakeSession(), "logf": DummyLog()}
        r2 = self.client.post(f"/api/chat/{cid}/prompt", json={"text": long_ok, "attachments": ["a.html"]})
        self.assertEqual(r2.status_code, 202)
        for _ in range(200):
            ev = [e for e in self._events(cid) if "参考文件" in e]
            if ev:
                break
            time.sleep(0.01)
        ev = [e for e in self._events(cid) if "📎 参考文件：" in e][0]
        self.assertLessEqual(len(ev), 4000)
        self.assertTrue(ev.startswith("📎 参考文件：a.html"), "清单须前置，不被 [:4000] 吞掉")

    def test_26_removed_attachment_absent_from_prompt(self):
        """移除（前端不再回传该名）后，该轮 prompt 与事件都不含它的路径/名。"""
        cid = self._insert()
        self._upload(cid, [(b"x", "keep.html"), (b"y", "drop.html")])
        resp, s = self._prompt_with_live_session(cid, {"text": "hi", "attachments": ["keep.html"]})
        self.assertEqual(resp.status_code, 202)
        self.assertIn("keep.html", s.prompts[0])
        self.assertNotIn("drop.html", s.prompts[0])
        ev = [e for e in self._events(cid) if "参考文件" in e][0]
        self.assertNotIn("drop.html", ev)
        # 源码接线：body 用 pendingChatMaterials 的 filename 列表（移除即从数组剔除）
        js = frontend_js()
        body = _extract_function(js, "sendChat")
        self.assertIn("attachments: pendingChatMaterials.map((m) => m.filename)", body)
        rm = _extract_function(js, "removeChatMaterial")
        self.assertIn("pendingChatMaterials.filter((m) => m.filename !== name)", rm)


# ==================== 三、建卡收口（三步序 / 预算截断 / 降级） ====================

class ChatMaterialCreateTest(ChatMaterialBase):

    def _create(self, cid, notes, title="测试需求"):
        srv = FakeCreateServer(cid, "req-board")
        return b.CreateRequirementMcpServer.tool_create_requirement(
            srv, {"title": title, "project": "req-board", "notes": notes})

    def test_19_create_copies_materials_and_appends_notes(self):
        cid = self._insert()
        self._upload(cid, [(b"<p>1</p>", "a.html"), (b"<p>2</p>", "b.html")])
        out = json.loads(self._create(cid, "背景\n\n验收标准"))
        reqs = self._requirements()
        self.assertEqual(len(reqs), 1)
        rid = out["id"]
        dst = b.ARTIFACTS_ROOT / "req-board" / f"req-{rid}" / "materials"
        self.assertEqual(sorted(p.name for p in dst.glob("*")), ["a.html", "b.html"])
        self.assertIn(str(dst), reqs[0]["notes"], "notes 须写入参考文件绝对路径")
        self.assertTrue(reqs[0]["notes"].startswith("背景"), "原 notes 保持在前（追加在后）")
        self.assertIn("2 个 html 原型", reqs[0]["notes"])

    def test_20_create_without_materials_no_update(self):
        cid = self._insert()
        calls = []
        old = b.req_update
        b.req_update = lambda *a, **kw: (calls.append((a, kw)), old(*a, **kw))[1]
        try:
            self._create(cid, "只有文字")
        finally:
            b.req_update = old
        self.assertEqual(calls, [], "无附件不得回写 notes")
        self.assertFalse((b.ARTIFACTS_ROOT / "req-board" / "req-1" / "materials").exists())

    def test_21_create_notes_budget_truncated(self):
        cid = self._insert()
        self._upload(cid, [(b"x", "a.html")])
        self._create(cid, "长" * 1990)
        notes = self._requirements()[0]["notes"]
        self.assertLessEqual(len(notes), 2000)
        self.assertIn("参考文件：", notes)
        self.assertTrue(notes.endswith("个 html 原型）"), "预算截断后参考行必须完整")

    def test_22_create_copy_failure_degrades(self):
        cid = self._insert()
        self._upload(cid, [(b"x", "a.html")])
        old = b.ARTIFACTS_ROOT
        b.ARTIFACTS_ROOT = Path("/dev/null/not-a-dir")   # mkdir 必失败
        try:
            out = json.loads(self._create(cid, "背景"))
        finally:
            b.ARTIFACTS_ROOT = old
        self.assertTrue(out["created"])
        self.assertEqual(self._requirements()[0]["notes"], "背景", "复制失败不得追加参考行")
        self.assertTrue(any("参考文件复制失败" in e for e in self._events(cid)), "降级须记 system 事件")

    def test_22b_notes_over_2000_still_rejected(self):
        cid = self._insert()
        self._upload(cid, [(b"x", "a.html")])
        with self.assertRaises(ValueError):
            self._create(cid, "长" * 2001)
        self.assertEqual(self._requirements(), [])


# ==================== 四、前端接线（源码断言 / node harness） ====================

def b_board_src():
    return frontend_src()


class ChatMaterialFrontendTest(ChatMaterialBase):

    # node harness 需前置注入的真实函数（顺序无所谓，均为 static/board.js 顶层 function 声明）
    HARNESS_FUNCS = ["escapeHtml", "escapeAttr", "chatMaterialInputEnabled", "chatMaterialNameError",
                     "chatMaterialAddFiles", "uploadChatMaterials", "removeChatMaterial",
                     "renderChatAttachBar", "bindChatMaterialInputs"]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.src = b_board_src()
        cls.js = frontend_js()

    def test_23_add_form_contains_attach_controls(self):
        form = _extract_function(self.js, "addFormHtml")
        self.assertIn('id="chatAttachBar"', form)
        self.assertIn('id="chatAttachBtn"', form)
        self.assertIn('id="chatFileInput" accept=".html,.htm" multiple', form)
        bar = form[form.index('id="chatAttachBar"'):form.index("</div>", form.index('id="chatAttachChips"'))]
        self.assertIn('id="chatAttachBtn"', bar)
        self.assertIn('id="chatFileInput"', bar)
        self.assertIn('id="chatAttachChips"', bar)

    def test_24_bind_form_and_send_wiring(self):
        bind = _extract_function(self.js, "bindAddForm")
        self.assertIn("bindChatMaterialInputs(input, document.getElementById('chatAttachBar'))", bind,
                      "bindAddForm 须调用附件接线函数（三入口唯一挂载点）")
        wire = _extract_function(self.js, "bindChatMaterialInputs")
        for needle in ("'dragenter', 'dragover'", "addEventListener('drop'", "addEventListener('paste'",
                       "addEventListener('change'", "closest('#chatAttachBtn')",
                       "e.target.value = ''"):
            self.assertIn(needle, wire, "bindChatMaterialInputs 缺少 %s" % needle)
        self.assertIn("input.closest('.chat-input-row')", wire, "拖拽须绑恒有高度的 .chat-input-row")
        self.assertIn("attachBar.classList.toggle('drag-over'", wire)
        self.assertNotIn("inputRow.classList.add('drag-over'", wire)
        send = _extract_function(self.js, "sendChat")
        self.assertIn("attachments: pendingChatMaterials.map((m) => m.filename)", send)
        self.assertIn("pendingChatMaterials = [];", send)
        render = _extract_function(self.js, "renderChatPane")
        self.assertIn("chatMaterialReset(chatState && chatState.id)", render)

    def test_28_css_grid_and_chip_styles(self):
        css = frontend_css()
        def rule(sel):
            m = re.search(r"(?m)^[ \t]*" + re.escape(sel) + r"[ \t]*\{([^}]*)\}", css)
            return m.group(1) if m else None
        self.assertIn("grid-row: 1", rule(".chat-status"))
        bar = rule(".chat-attach-bar")
        self.assertIn("grid-row: 2", bar)
        self.assertIn("grid-column: 1 / -1", bar)
        self.assertIn("grid-row: 3", rule(".chat-input-row textarea"))
        self.assertIn("grid-row: 3", rule(".chat-input-row button"))
        # 被逐字锁的两行原文未动
        self.assertIn(".no-input .chat-input-row > textarea,\n"
                      "        .no-input .chat-input-row > button { display: none; }", css)
        self.assertIn(".no-input .chat-input-row > .chat-attach-bar { display: none; }", css)
        self.assertIn("var(--accent)", rule(".chat-attach-btn:hover"))
        self.assertIn("var(--warn-fg)", rule(".chat-attach-chip.warn"))

    def test_28b_drag_highlight_target_consistent(self):
        """拖拽高亮：类名挂载元素必须与 CSS 选择器同一元素（否则高亮永不出现）。
        选择器侧 = CSS 规则 `.chat-attach-bar.drag-over`；挂载侧 = JS 在 attachBar 上 toggle
        （行为侧由 harness 的 drag-highlight-on-attach-bar 断言实际落到 #chatAttachBar）。"""
        self.assertRegex(frontend_css(), r"(?m)^[ \t]*\.chat-attach-bar\.drag-over[ \t]*\{")
        self.assertNotRegex(frontend_css(), r"(?m)^[ \t]*\.chat-input-row\.drag-over[ \t]*\{")
        wire = _extract_function(self.js, "bindChatMaterialInputs")
        self.assertIn("attachBar.classList.toggle('drag-over'", wire)
        self.assertIn("document.getElementById('chatAttachBar')", _extract_function(self.js, "bindAddForm"))
        with self.subTest("harness 行为断言"):
            checks = self._run_harness(self.HARNESS_FUNCS)
            by_name = {c["name"]: c for c in checks}
            for name in ("drag-highlight-on-attach-bar", "drag-highlight-matches-css-selector"):
                self.assertTrue(by_name.get(name, {}).get("pass"), name)

    def test_28c_chip_attribute_escaping(self):
        """chip 属性位必须用 escapeAttr（escapeHtml 不转引号）：文件名含 `"` 时不得逃出
        data-name / title 属性（werkzeug 会把 multipart 的 %22 还原成 `"`，文件名策略放行 `"`）。"""
        src = _extract_function(self.js, "renderChatAttachBar")
        self.assertIn('data-name="${escapeAttr(m.filename)}"', src)
        self.assertIn('title="${escapeAttr(tip)}"', src)
        self.assertNotIn('data-name="${escapeHtml(', src)
        self.assertIn("function escapeAttr(", self.js)
        self.assertIn("escapeHtml(s).replace(", self.js)
        checks = self._run_harness(self.HARNESS_FUNCS)
        by_name = {c["name"]: c for c in checks}
        for name in ("chip-name-escaped", "chip-attr-quotes-escaped"):
            self.assertTrue(by_name.get(name, {}).get("pass"), name)

    def test_28d_attribute_escape_mutation_guard(self):
        """变异保护：把 chip 属性位改回 escapeHtml，harness 必须失败且**恰好**是
        chip-attr-quotes-escaped —— 证明该断言对缺陷敏感、不是假阳性。"""
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        self._run_harness(self.HARNESS_FUNCS, mutate_attr_escape=True,
                          expect_failures=["chip-attr-quotes-escaped"])

    def _js_sources(self, names, mutate_attr_escape=False):
        """提取真实函数源码；`async function x()` 的 extract 只从 `function` 起，需补回 async 前缀。
        mutate_attr_escape：把 chip 属性位从 escapeAttr 改回 escapeHtml（变异保护用）。"""
        out = []
        for n in names:
            src = _extract_function(self.js, n)
            start = self.js.find("function %s(" % n)
            if self.js[max(0, start - 6):start] == "async ":
                src = "async " + src
            if mutate_attr_escape and n == "renderChatAttachBar":
                src = (src.replace("escapeAttr(m.filename)", "escapeHtml(m.filename)")
                          .replace("escapeAttr(tip)", "escapeHtml(tip)"))
            out.append(src)
        return out

    def _run_harness(self, names, mutate_attr_escape=False, expect_failures=None):
        prefix = "\n\n".join(self._js_sources(names, mutate_attr_escape))
        harness = REPO / "tests" / "chat_material_harness.js"
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(prefix + "\n\n" + harness.read_text(encoding="utf-8"))
            tmp = f.name
        try:
            proc = subprocess.run([node, tmp], capture_output=True, text=True, timeout=60)
        finally:
            Path(tmp).unlink(missing_ok=True)
        checks = json.loads(proc.stdout)["checks"]
        failed = sorted(c["name"] for c in checks if not c["pass"])
        if expect_failures is None:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(failed, [], "harness 断言失败: %s" % failed)
        else:
            self.assertEqual(proc.returncode, 1, "变异体本应失败: " + proc.stdout)
            self.assertEqual(failed, sorted(expect_failures), "变异体失败项不符: %s" % failed)
        return checks
        return checks

    def test_25_26_27_node_harness_behavior(self):
        """事件级断言（方案用例 25/26/27）：把 drop / paste / change / click 真实派发给
        bindChatMaterialInputs 绑定的监听器 → 断言到「发起上传 fetch / chip 出现 / × 移除」
        这一层（uploadChatMaterials 用真实实现 + fetch 桩，不再被替换）。"""
        checks = self._run_harness(self.HARNESS_FUNCS)
        names = {c["name"] for c in checks}
        for required in ("listeners-bound", "drop-triggers-upload", "drop-chip-rendered",
                         "paste-triggers-upload", "paste-chip-rendered", "change-triggers-upload",
                         "change-resets-value", "attach-btn-opens-file-input", "remove-only-target",
                         "cumulative-reject", "disabled-blocks-drop-and-paste",
                         "upload-500-toast-no-chip", "chip-attr-quotes-escaped"):
            self.assertIn(required, names, "harness 缺少断言 %s" % required)
        self.assertGreaterEqual(len(checks), 20, "harness 断言数量异常: %d" % len(checks))


if __name__ == "__main__":
    unittest.main()
