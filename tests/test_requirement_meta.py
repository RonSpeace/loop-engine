#!/usr/bin/env python3
"""req-303 建卡来源/模型/推理强度元数据测试：
requirements 表 4 新列迁移（source/model/reasoning_effort/source_session_id）、
POST/PATCH/GET 接受与返回新字段（向后兼容）、板内聊天建卡写 source=board-chat+会话模型、
create_card.py DSH_SESSION_JSONL 自动检测与显式参数覆盖（build_meta）。

运行：
    .venv/bin/python -m unittest tests.test_requirement_meta -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board as b  # noqa: E402
import sqlite3  # noqa: E402

# create_card.py 在 skill 目录（req-board 仓库内 skills/req-board-card/scripts/）
SKILL_SCRIPTS = REPO / "skills" / "req-board-card" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
import create_card as cc  # noqa: E402


class RequirementMetaApiTest(unittest.TestCase):
    """迁移 + POST/PATCH/GET 新字段（用临时 DB 隔离，不碰真实 data/board.db）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-meta-api-"))
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_migration_columns(self):
        """init_db 后 requirements 表含 4 个新列（可空）"""
        with b.get_db() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(requirements)")}
        for col in ("source", "model", "reasoning_effort", "source_session_id"):
            self.assertIn(col, cols)

    def test_post_with_meta_fields(self):
        r = self.client.post("/api/requirements", json={
            "title": "来源元数据卡", "project": "req-board", "status": "later",
            "source": "dsh", "model": "deepseek-v4-flash", "reasoning_effort": "max",
            "source_session_id": "sess-123",
        })
        self.assertEqual(r.status_code, 201)
        item = r.get_json()
        self.assertEqual(item["source"], "dsh")
        self.assertEqual(item["model"], "deepseek-v4-flash")
        self.assertEqual(item["reasoning_effort"], "max")
        self.assertEqual(item["source_session_id"], "sess-123")

    def test_post_without_meta_backward_compat(self):
        r = self.client.post("/api/requirements", json={
            "title": "无元数据卡", "project": "req-board", "status": "later",
        })
        self.assertEqual(r.status_code, 201)
        item = r.get_json()
        self.assertIsNone(item["source"])
        self.assertIsNone(item["model"])
        self.assertIsNone(item["reasoning_effort"])
        self.assertIsNone(item["source_session_id"])

    def test_post_meta_truncated(self):
        r = self.client.post("/api/requirements", json={
            "title": "超长字段卡", "project": "req-board", "status": "later",
            "source": "x" * 500, "reasoning_effort": "y" * 500,
        })
        self.assertEqual(r.status_code, 201)
        item = r.get_json()
        self.assertEqual(len(item["source"]), 100)
        self.assertEqual(len(item["reasoning_effort"]), 20)

    def test_patch_meta_fields(self):
        created = self.client.post("/api/requirements", json={
            "title": "PATCH 卡", "project": "req-board", "status": "later",
        }).get_json()
        r = self.client.patch(f"/api/requirements/{created['id']}", json={
            "source": "codex", "model": "gpt-5", "reasoning_effort": "high",
            "source_session_id": "sess-patch",
        })
        self.assertEqual(r.status_code, 200)
        item = r.get_json()
        self.assertEqual(item["source"], "codex")
        self.assertEqual(item["model"], "gpt-5")
        self.assertEqual(item["reasoning_effort"], "high")
        self.assertEqual(item["source_session_id"], "sess-patch")
        # 未传字段保持不变
        r2 = self.client.patch(f"/api/requirements/{created['id']}", json={"source": "kimi-code"})
        item2 = r2.get_json()
        self.assertEqual(item2["source"], "kimi-code")
        self.assertEqual(item2["model"], "gpt-5")

    def test_get_returns_meta_fields(self):
        self.client.post("/api/requirements", json={
            "title": "GET 卡", "project": "req-board", "status": "later",
            "source": "board-chat", "model": "kimi-k2",
        })
        r = self.client.get("/api/requirements?project=req-board&status=later")
        items = [x for x in r.get_json() if x["title"] == "GET 卡"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "board-chat")
        self.assertEqual(items[0]["model"], "kimi-k2")


class BoardChatSourceTest(unittest.TestCase):
    """板内聊天建卡（create_requirement 工具）：source=board-chat + 会话模型"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-meta-chat-"))
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _chat(self, model=None):
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO chat_sessions (project, status, model) VALUES (?, 'idle', ?)",
                ("req-board", model))
            conn.commit()
            return cur.lastrowid

    def test_create_requirement_writes_board_chat_and_model(self):
        chat_id = self._chat(model="kimi-k2-thinking")
        srv = b.CreateRequirementMcpServer(chat_id, "req-board")
        try:
            out = json.loads(srv.tool_create_requirement({
                "title": "聊天建卡卡", "project": "req-board", "notes": "n", "status": "later",
            }))
        finally:
            srv.close()
        self.assertTrue(out["created"])
        with b.get_db() as conn:
            row = conn.execute("SELECT * FROM requirements WHERE id = ?", (out["id"],)).fetchone()
        self.assertEqual(row["source"], "board-chat")
        self.assertEqual(row["model"], "kimi-k2-thinking")
        self.assertIsNone(row["reasoning_effort"])

    def test_create_requirement_model_none(self):
        chat_id = self._chat(model=None)
        srv = b.CreateRequirementMcpServer(chat_id, "req-board")
        try:
            out = json.loads(srv.tool_create_requirement({
                "title": "无模型聊天卡", "project": "req-board", "notes": "n", "status": "later",
            }))
        finally:
            srv.close()
        with b.get_db() as conn:
            row = conn.execute("SELECT * FROM requirements WHERE id = ?", (out["id"],)).fetchone()
        self.assertEqual(row["source"], "board-chat")
        self.assertIsNone(row["model"])


class DshDetectTest(unittest.TestCase):
    """create_card.py DSH 环境自动检测（#303/#350）与显式参数覆盖"""

    DSH_ENV_KEYS = ("DSH_SESSION_JSONL", "DSH_SESSION_ID", "DSH_HOME")

    def _write_jsonl(self, lines):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", encoding="utf-8", delete=False)
        for line in lines:
            f.write(line + "\n")
        f.close()
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        return f.name

    def _clear_dsh_env(self):
        """清空三个 DSH 信号：保证用例不受外部环境（例如在 DSH 会话里跑测试时 DSH_HOME/DSH_SESSION_ID
        天然存在）影响，结果与在 launchd 里跑一致"""
        for k in self.DSH_ENV_KEYS:
            os.environ.pop(k, None)

    def test_no_env_returns_none(self):
        """三个信号都没有 → None（调用方此时才允许回退 kimi-code 探测）"""
        self._clear_dsh_env()
        self.assertIsNone(cc.detect_dsh_meta())

    def test_parse_request_event(self):
        path = self._write_jsonl([
            json.dumps({"kind": "session/new", "config": {"provider": "dsh", "model": "deepseek-v4-flash"}}),
            json.dumps({"kind": "request", "config": {"provider": "dsh", "model": "deepseek-v4-flash",
                                                      "reasoningEffort": "max"}}),
        ])
        self._clear_dsh_env()
        os.environ["DSH_SESSION_JSONL"] = path
        os.environ["DSH_SESSION_ID"] = "sess-dsh-1"
        self.addCleanup(os.environ.pop, "DSH_SESSION_JSONL", None)
        self.addCleanup(os.environ.pop, "DSH_SESSION_ID", None)
        meta = cc.detect_dsh_meta()
        self.assertEqual(meta["source"], "dsh")
        self.assertEqual(meta["model"], "deepseek-v4-flash")
        self.assertEqual(meta["reasoning_effort"], "max")
        self.assertEqual(meta["source_session_id"], "sess-dsh-1")

    def test_unreadable_or_invalid_file_degrades_to_source_only(self):
        """文件不存在/内容非法：仍在 DSH 环境 → 只报 source=dsh（model 留空）、不抛异常"""
        self._clear_dsh_env()
        os.environ["DSH_SESSION_JSONL"] = "/nonexistent/dsh-session.jsonl"
        self.addCleanup(os.environ.pop, "DSH_SESSION_JSONL", None)
        self.assertEqual(cc.detect_dsh_meta(), {"source": "dsh", "source_session_id": None})
        path = self._write_jsonl(["not-json", "{broken"])
        os.environ["DSH_SESSION_JSONL"] = path
        self.assertEqual(cc.detect_dsh_meta(), {"source": "dsh", "source_session_id": None})

    def test_build_meta_explicit_overrides_dsh(self):
        dsh = {"source": "dsh", "model": "deepseek-v4-flash", "reasoning_effort": "max",
               "source_session_id": "sess-dsh"}
        meta = cc.build_meta({"source": "codex", "model": None, "reasoning_effort": None}, {}, dsh)
        self.assertEqual(meta["source"], "codex")            # 显式覆盖
        self.assertEqual(meta["model"], "deepseek-v4-flash")  # 缺失字段回退 DSH
        self.assertEqual(meta["reasoning_effort"], "max")
        self.assertEqual(meta["source_session_id"], "sess-dsh")

    def test_build_meta_empty_when_no_input(self):
        self.assertEqual(cc.build_meta({"source": None, "model": None, "reasoning_effort": None}, {}, {}), {})

    def test_build_meta_kimi_auto_detect(self):
        """kimi-code 自动探测（#350）：无显式参数且无 DSH 时补 source=kimi-code + model"""
        kimi = {"source": "kimi-code", "model": "DeepSeek V4 Flash Vision Exp"}
        meta = cc.build_meta({"source": None, "model": None, "reasoning_effort": None}, kimi, {})
        self.assertEqual(meta, {"source": "kimi-code", "model": "DeepSeek V4 Flash Vision Exp"})
        # 显式 --model 覆盖探测值
        meta2 = cc.build_meta({"source": "kimi-code", "model": "GLM-5.3", "reasoning_effort": None}, kimi, {})
        self.assertEqual(meta2, {"source": "kimi-code", "model": "GLM-5.3"})

    def test_build_meta_priority_explicit_kimi_dsh(self):
        """三源优先级：显式 > kimi 探测 > DSH（逐字段回退）"""
        kimi = {"source": "kimi-code", "model": "DeepSeek V4 Flash Vision Exp"}
        dsh = {"source": "dsh", "model": "deepseek-v4-flash", "reasoning_effort": "max"}
        meta = cc.build_meta({"source": None, "model": None, "reasoning_effort": None}, kimi, dsh)
        self.assertEqual(meta["source"], "kimi-code")          # kimi 优先于 DSH
        self.assertEqual(meta["model"], "DeepSeek V4 Flash Vision Exp")
        self.assertEqual(meta["reasoning_effort"], "max")      # 仅 DSH 有的字段仍回退补齐
        meta2 = cc.build_meta({"source": "codex", "model": None, "reasoning_effort": None}, kimi, dsh)
        self.assertEqual(meta2["source"], "codex")             # 显式 source 覆盖
        self.assertEqual(meta2["model"], "DeepSeek V4 Flash Vision Exp")


if __name__ == "__main__":
    unittest.main()
