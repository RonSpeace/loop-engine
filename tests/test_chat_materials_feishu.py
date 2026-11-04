#!/usr/bin/env python3
"""#22 飞书建卡素材端点测试（/feishu-materials、prompt 侧白名单分流、收口文案、feishu-events）。

对应技术方案用例 32-44、96-100、107、108：
32 飞书素材白名单落盘；33 非法扩展名零落盘；34 单文件 >20MB → 413；35 单轮 >5 → 400；
36 web 白名单未放松；37 两端点互斥 400；38 created 409；39 路径穿越 400；
40 content_length 早退 / chunked 按解码字节；41 非法 base64 → 400；42 feishu-events 通道校验；
43 超长 content 截断；44 prompt 注入文案分流；96 飞书素材进 prompt；97 web .png → 400；
98 白名单按行 channel；99 飞书 .exe → 400；100 不存在存储名 → 400 不发起轮次；
107 建卡收口 notes 文案分流；108 failed 可见 / over_read_limit 口径 / 20MB 仍可上传。

隔离：临时 DB + 临时 MATERIALS_ROOT/ARTIFACTS_ROOT + 临时 PROJECT_PATHS；打桩会话不起真实 kimi。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_chat_materials_feishu -v
"""
import base64
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

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
    def __init__(self, chat_id, project):
        self.chat_id = chat_id
        self.project = project


class FeishuMaterialBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req22-feishu-mat-"))
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
        vals = {"project": "req-board", "status": "idle", "channel": "feishu",
                "session_id": "s-1", "created_at": b.now_str(), "updated_at": b.now_str()}
        vals.update(fields)
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO chat_sessions (project, status, channel, session_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (vals["project"], vals["status"], vals["channel"], vals["session_id"],
                 vals["created_at"], vals["updated_at"]))
            chat_id = cur.lastrowid
            conn.commit()
        return chat_id

    def _upload_feishu(self, cid, items, body=None):
        """items = [(name, bytes)]；body 可覆盖请求体（用于异常形态）。"""
        if body is None:
            body = {"files": [{"name": n, "content_b64": base64.b64encode(d).decode()}
                              for n, d in items]}
        return self.client.post(f"/api/chat/{cid}/feishu-materials", json=body)

    def _upload_web(self, cid, items):
        data = {"file": [(io.BytesIO(payload), name) for payload, name in items]}
        return self.client.post(f"/api/chat/{cid}/materials", data=data,
                                content_type="multipart/form-data")

    def _saved(self, cid, project="req-board"):
        d = b._chat_materials_dir(project, cid)
        return sorted(p.name for p in d.glob("*") if p.is_file()) if d.is_dir() else []

    def _events(self, cid):
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT kind, content FROM chat_events WHERE chat_id = ? ORDER BY id", (cid,)).fetchall()]

    def _requirements(self):
        with b.get_db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM requirements ORDER BY id").fetchall()]

    def _prompt_with_live_session(self, cid, payload):
        s = FakeSession()
        with b.chat_lock:
            b.chat_runtime[cid] = {"session": s, "logf": DummyLog()}
        resp = self.client.post(f"/api/chat/{cid}/prompt", json=payload)
        for _ in range(300):
            if resp.status_code != 202 or s.prompts:
                break
            time.sleep(0.01)
        return resp, s


# ==================== 一、飞书素材上传端点 ====================

class FeishuMaterialUploadTest(FeishuMaterialBase):

    def test_32_feishu_whitelist_upload(self):
        cid = self._insert()
        r = self._upload_feishu(cid, [("图.png", b"PNG"), ("文档.pdf", b"PDF"), ("需求.docx", b"DOC")])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        names = [f["filename"] for f in r.get_json()["files"]]
        self.assertEqual(names, ["图.png", "文档.pdf", "需求.docx"])
        self.assertEqual(self._saved(cid), sorted(["图.png", "文档.pdf", "需求.docx"]))
        for f in r.get_json()["files"]:
            self.assertTrue(Path(f["path"]).is_absolute())
            self.assertIn(str(b.MATERIALS_ROOT / "req-board" / f"chat-{cid}"), f["path"])

    def test_33_bad_ext_zero_write(self):
        cid = self._insert()
        for name in ("a.exe", "noext", "a.bin"):
            with self.subTest(name=name):
                r = self._upload_feishu(cid, [(name, b"x")])
                self.assertEqual(r.status_code, 400, name)
                self.assertEqual(self._saved(cid), [])

    def test_34_oversize_decoded_413(self):
        cid = self._insert()
        r = self._upload_feishu(cid, [("big.png", b"a" * (20 * MB + 1))])
        self.assertEqual(r.status_code, 413, r.get_data(as_text=True))
        self.assertEqual(self._saved(cid), [])

    def test_35_too_many_files(self):
        cid = self._insert()
        r = self._upload_feishu(cid, [(f"f{i}.png", b"x") for i in range(6)])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._saved(cid), [])

    def test_36_web_whitelist_unchanged(self):
        cid = self._insert(channel="web")
        r = self._upload_web(cid, [(b"x", "a.png")])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._saved(cid), [])
        r2 = self._upload_web(cid, [(b"<p>hi</p>", "a.html")])
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(self._saved(cid), ["a.html"])

    def test_37_endpoint_channel_exclusive(self):
        w = self._insert(channel="web")
        f = self._insert(channel="feishu")
        self.assertEqual(self._upload_feishu(w, [("a.png", b"x")]).status_code, 400)
        self.assertEqual(self._upload_web(f, [(b"x", "a.html")]).status_code, 400)

    def test_38_created_session_409(self):
        w = self._insert(channel="web", status="created")
        f = self._insert(channel="feishu", status="created")
        self.assertEqual(self._upload_web(w, [(b"x", "a.html")]).status_code, 409)
        self.assertEqual(self._upload_feishu(f, [("a.png", b"x")]).status_code, 409)

    def test_39_path_traversal_rejected(self):
        cid = self._insert()
        for name in ("../x.png", "../../x.png", "a/b.png", "..", "a..png"):
            with self.subTest(name=name):
                r = self._upload_feishu(cid, [(name, b"x")])
                self.assertEqual(r.status_code, 400, name)
                self.assertEqual(self._saved(cid), [])
        self.assertFalse(b._material_name_ok("../x.png", b.FEISHU_MATERIAL_EXTS))

    def test_40_body_limit_and_chunked(self):
        from werkzeug.test import EnvironBuilder
        cid = self._insert()
        # content_length 早退
        body = json.dumps({"files": [{"name": "a.png", "content_b64": "aGk="}]})
        env = EnvironBuilder(method="POST", path=f"/api/chat/{cid}/feishu-materials",
                             data=body, content_type="application/json").get_environ()
        env["CONTENT_LENGTH"] = str(b.FEISHU_MATERIAL_BODY_LIMIT + 1)
        with b.app.request_context(env):
            resp = b.api_chat_feishu_materials(cid)
        self.assertEqual(resp[1] if isinstance(resp, tuple) else resp.status_code, 413)
        self.assertEqual(self._saved(cid), [])
        # chunked（无 content_length）→ 按解码后字节判定，不 fail-open
        big = json.dumps({"files": [{"name": "big.png",
                                     "content_b64": base64.b64encode(b"a" * (20 * MB + 1)).decode()}]})
        env2 = EnvironBuilder(method="POST", path=f"/api/chat/{cid}/feishu-materials",
                              data=big, content_type="application/json").get_environ()
        env2.pop("CONTENT_LENGTH", None)
        env2["wsgi.input_terminated"] = True
        env2["HTTP_TRANSFER_ENCODING"] = "chunked"
        with b.app.request_context(env2):
            resp2 = b.api_chat_feishu_materials(cid)
        self.assertEqual(resp2[1] if isinstance(resp2, tuple) else resp2.status_code, 413)
        self.assertEqual(self._saved(cid), [])

    def test_41_invalid_base64(self):
        cid = self._insert()
        r = self._upload_feishu(cid, None, body={"files": [{"name": "a.png", "content_b64": "!!!!"}]})
        self.assertEqual(r.status_code, 400)
        r2 = self._upload_feishu(cid, None, body={"files": [{"name": "a.png"}]})
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(self._saved(cid), [])


# ==================== 二、feishu-events 回放端点 ====================

class FeishuEventsTest(FeishuMaterialBase):

    def test_42_events_channel_check(self):
        w = self._insert(channel="web")
        r = self.client.post(f"/api/chat/{w}/feishu-events",
                             json={"events": [{"kind": "system", "content": "x"}]})
        self.assertEqual(r.status_code, 400)
        f = self._insert(channel="feishu")
        r2 = self.client.post(f"/api/chat/{f}/feishu-events",
                              json={"events": [{"kind": "user", "content": "[飞书] 用户：hi"},
                                               {"kind": "agent", "content": "[飞书] 机器人：选项目"}]})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["ok"])
        rows = self._events(f)
        self.assertEqual([(e["kind"], e["content"]) for e in rows],
                         [("user", "[飞书] 用户：hi"), ("agent", "[飞书] 机器人：选项目")])

    def test_42b_events_invalid_shape(self):
        f = self._insert(channel="feishu")
        for body in ({"events": "x"}, {"events": [{"kind": "bad", "content": "x"}]},
                     {"events": [1]}, {}):
            with self.subTest(body=body):
                r = self.client.post(f"/api/chat/{f}/feishu-events", json=body)
                self.assertEqual(r.status_code, 400)

    def test_43_long_content_truncated(self):
        f = self._insert(channel="feishu")
        r = self.client.post(f"/api/chat/{f}/feishu-events",
                             json={"events": [{"kind": "system", "content": "长" * 5000}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self._events(f)[0]["content"]), 4000)


# ==================== 三、prompt 侧白名单分流与注入文案 ====================

class FeishuPromptTest(FeishuMaterialBase):

    def test_44_prompt_text_by_channel(self):
        f = self._insert(channel="feishu")
        self._upload_feishu(f, [("图.png", b"PNG")])
        resp, s = self._prompt_with_live_session(f, {"text": "看这个", "attachments": ["图.png"]})
        self.assertEqual(resp.status_code, 202)
        self.assertIn("飞书上传的图片/文档", s.prompts[0])
        self.assertNotIn("本机 html 原型", s.prompts[0])
        w = self._insert(channel="web")
        self._upload_web(w, [(b"<p>x</p>", "a.html")])
        resp2, s2 = self._prompt_with_live_session(w, {"text": "看这个", "attachments": ["a.html"]})
        self.assertEqual(resp2.status_code, 202)
        self.assertIn("本机 html 原型", s2.prompts[0])

    def test_96_feishu_attachments_into_prompt(self):
        cid = self._insert(channel="feishu")
        self._upload_feishu(cid, [("图.png", b"PNG"), ("需求.docx", b"DOC")])
        resp, s = self._prompt_with_live_session(
            cid, {"text": "两个文件", "attachments": ["图.png", "需求.docx"]})
        self.assertEqual(resp.status_code, 202, resp.get_data(as_text=True))
        prompt = s.prompts[0]
        base = b.MATERIALS_ROOT / "req-board" / f"chat-{cid}"
        self.assertIn(str((base / "图.png").resolve()), prompt)
        self.assertIn(str((base / "需求.docx").resolve()), prompt)
        user_ev = [e["content"] for e in self._events(cid) if e["kind"] == "user"][0]
        self.assertTrue(user_ev.startswith("📎 参考文件："))
        self.assertIn("图.png", user_ev)

    def test_97_web_png_attachment_rejected(self):
        cid = self._insert(channel="web")
        (b._chat_materials_dir("req-board", cid)).mkdir(parents=True, exist_ok=True)
        (b._chat_materials_dir("req-board", cid) / "a.png").write_bytes(b"x")
        with b.chat_lock:
            b.chat_runtime[cid] = {"session": FakeSession(), "logf": DummyLog()}
        r = self.client.post(f"/api/chat/{cid}/prompt",
                             json={"text": "hi", "attachments": ["a.png"]})
        self.assertEqual(r.status_code, 400)

    def test_98_material_paths_by_channel(self):
        cid = self._insert(channel="feishu")
        d = b._chat_materials_dir("req-board", cid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.png").write_bytes(b"x")
        (d / "a.html").write_bytes(b"y")
        paths, err = b._chat_material_paths("req-board", cid, ["a.png"], b.FEISHU_MATERIAL_EXTS)
        self.assertIsNone(err)
        self.assertEqual(len(paths), 1)
        paths2, err2 = b._chat_material_paths("req-board", cid, ["a.png"], b.CHAT_MATERIAL_EXTS)
        self.assertIsNone(paths2)
        self.assertIn("非法文件名", err2)
        # 未知 channel 行回落 web 白名单：.png 400 / .html 放行
        weird = self._insert(channel="weird")
        dw = b._chat_materials_dir("req-board", weird)
        dw.mkdir(parents=True, exist_ok=True)
        (dw / "a.png").write_bytes(b"x")
        (dw / "a.html").write_bytes(b"y")
        with b.chat_lock:
            b.chat_runtime[weird] = {"session": FakeSession(), "logf": DummyLog()}
        self.assertEqual(self.client.post(f"/api/chat/{weird}/prompt",
                                          json={"text": "hi", "attachments": ["a.png"]}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/chat/{weird}/prompt",
                                          json={"text": "hi", "attachments": ["a.html"]}).status_code, 202)

    def test_99_feishu_exe_rejected(self):
        cid = self._insert(channel="feishu")
        (b._chat_materials_dir("req-board", cid)).mkdir(parents=True, exist_ok=True)
        (b._chat_materials_dir("req-board", cid) / "a.exe").write_bytes(b"x")
        with b.chat_lock:
            b.chat_runtime[cid] = {"session": FakeSession(), "logf": DummyLog()}
        r = self.client.post(f"/api/chat/{cid}/prompt",
                             json={"text": "hi", "attachments": ["a.exe"]})
        self.assertEqual(r.status_code, 400)

    def test_100_missing_storage_name_no_round(self):
        cid = self._insert(channel="feishu")
        s = FakeSession()
        with b.chat_lock:
            b.chat_runtime[cid] = {"session": s, "logf": DummyLog()}
        r = self.client.post(f"/api/chat/{cid}/prompt",
                             json={"text": "hi", "attachments": ["nope.png"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(s.prompts, [])


# ==================== 四、建卡收口文案 + 视图数据源 ====================

class FeishuCloseoutTest(FeishuMaterialBase):

    def _create(self, cid, notes, title="飞书需求"):
        srv = FakeCreateServer(cid, "req-board")
        return b.CreateRequirementMcpServer.tool_create_requirement(
            srv, {"title": title, "project": "req-board", "notes": notes})

    def test_107_closeout_notes_by_channel(self):
        f = self._insert(channel="feishu")
        self._upload_feishu(f, [("图.png", b"PNG"), ("文档.pdf", b"PDF")])
        out = json.loads(self._create(f, "背景\n\n验收标准"))
        notes = self._requirements()[-1]["notes"]
        self.assertIn("2 个参考文件，图片/文档", notes)
        self.assertNotIn("html 原型", notes)
        self.assertEqual(out["id"], self._requirements()[-1]["id"])
        # web 保持原文案
        w = self._insert(channel="web")
        self._upload_web(w, [(b"<p>1</p>", "a.html")])
        self._create(w, "背景")
        notes_w = self._requirements()[-1]["notes"]
        self.assertIn("1 个 html 原型", notes_w)

    def test_108_view_source_and_read_limit(self):
        # ① failed 会话在飞书视图数据源可见
        failed = self._insert(channel="feishu", status="failed")
        skipped = self._insert(channel="feishu", status="created")
        qs = "channel=feishu&status=created,closed,failed,idle,running,interrupted,suspended&limit=20"
        ids = {r["id"] for r in self.client.get(f"/api/chat?{qs}").get_json()}
        self.assertIn(failed, ids)
        self.assertIn(skipped, ids)
        # ② over_read_limit 口径（>1MB → true；≤1MB → false）
        cid = self._insert(channel="feishu")
        r_big = self._upload_feishu(cid, [("大.pdf", b"a" * (MB + 10))])
        self.assertTrue(r_big.get_json()["files"][0]["over_read_limit"])
        r_small = self._upload_feishu(cid, [("小.pdf", b"a" * (MB - 10))])
        self.assertFalse(r_small.get_json()["files"][0]["over_read_limit"])
        # ③ 20MB 文件仍可上传（上限未收紧）
        r_max = self._upload_feishu(cid, [("满.pdf", b"a" * (20 * MB))])
        self.assertEqual(r_max.status_code, 200, r_max.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
