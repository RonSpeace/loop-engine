#!/usr/bin/env python3
"""需求对话维护工具（MaintainMcpServer）校验：#302
读 = 需求字段/产物/调度器执行事件；写 = 改卡片字段（白名单）/写产物（防穿越+扩展名白名单）/
TAPD 评论（token 读 kimi-code 配置、短 ID 补长、HTTP mock）。全部 fail-closed。

运行：
    .venv/bin/python -m unittest tests.test_req_chat_mcp -v
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request

# 本机 macOS 系统代理会拦截 urllib 对 127.0.0.1 的请求（getproxies_macosx_sysconf），
# 本地 HTTP 冒烟一律走显式空代理 opener（#419 自验环境暴露，与业务逻辑无关）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# #8 标记隔离：钉「无标记=launchd 旧路径」基线（ambient 标记随进程树进入测试进程；
# restart_dispatcher 工具断言 kickstart 命令与预检文案）。pytest 侧另有 conftest autouse 兜底。
os.environ.pop("REQ_BOARD_SUPERVISOR", None)

import board as b  # noqa: E402


class FakeResp:
    def __init__(self, data=b"{}"):
        self.data = data

    def read(self, n=-1):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeCompleted:
    """subprocess.run 结果替身（善后工具测试用）"""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class MaintainMcpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req-chat-mcp-"))
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.artifacts = cls.tmp / "artifacts"
        cls.artifacts.mkdir(parents=True)
        b.ARTIFACTS_ROOT = cls.artifacts

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with b.get_db() as conn:
            for table in ("req_chat_events", "req_chats", "requirements", "req_reviews",
                          "req_status_log", "dispatcher_events", "agent_events"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        self.req = b.req_add("需求", "req-board", notes="背景", status="later")
        self.rid = self.req["id"]
        self.chat_id = b._reqchat_create(self.rid)
        self.srv = b.MaintainMcpServer(self.chat_id, self.rid)
        self.req_dir = self.artifacts / "req-board" / f"req-{self.rid}"

    def tearDown(self):
        self.srv.close()

    def _call(self, name, args):
        return json.loads(getattr(self.srv, f"tool_{name}")(args))

    def _raises(self, name, args, fragment=None):
        with self.assertRaises(ValueError) as ctx:
            getattr(self.srv, f"tool_{name}")(args)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    # ---- 读：需求/产物/执行事件 ----
    def test_get_requirement(self):
        out = self._call("get_requirement", {})
        self.assertEqual(out["id"], self.rid)
        self.assertEqual(out["title"], "需求")
        self.assertEqual(out["notes"], "背景")
        self.assertEqual(out["status"], "later")

    def test_list_and_read_artifact(self):
        self.req_dir.mkdir(parents=True, exist_ok=True)
        (self.req_dir / "analysis.md").write_text("# 分析\n内容")
        out = self._call("list_artifacts", {})
        self.assertEqual([f["name"] for f in out["files"]], ["analysis.md"])
        out = self._call("read_artifact", {"filename": "analysis.md"})
        self.assertEqual(out["content"], "# 分析\n内容")

    def test_read_artifact_rejections(self):
        self._raises("read_artifact", {"filename": "../board.py"}, "非法文件名")
        self._raises("read_artifact", {"filename": "a/b.md"}, "非法文件名")
        self._raises("read_artifact", {"filename": "missing.md"}, "产物不存在")
        self.req_dir.mkdir(parents=True, exist_ok=True)
        (self.req_dir / "pic.png").write_bytes(b"x")
        self._raises("read_artifact", {"filename": "pic.png"}, "不支持的文件类型")

    def test_list_exec_events(self):
        with b.get_db() as conn:
            conn.execute("INSERT INTO agent_events (req_id, kind, content) VALUES (?, 'message', 'm1')", (self.rid,))
            conn.execute("INSERT INTO agent_events (req_id, kind, content) VALUES (?, 'tool', 't1')", (self.rid,))
            conn.commit()
        out = self._call("list_exec_events", {})
        self.assertEqual([(e["kind"], e["content"]) for e in out["events"]],
                         [("message", "m1"), ("tool", "t1")])
        out = self._call("list_exec_events", {"limit": 1})
        self.assertEqual([e["content"] for e in out["events"]], ["t1"])  # 最近 1 条

    # ---- 写：新建需求卡 ----
    def test_create_requirement_ok(self):
        out = self._call("create_requirement",
                         {"title": "新卡", "project": "req-board", "notes": "衍生需求", "status": "later"})
        self.assertTrue(out["created"])
        self.assertEqual(out["title"], "新卡")
        self.assertEqual(out["project"], "req-board")
        self.assertEqual(out["status"], "later")
        with b.get_db() as conn:
            row = conn.execute("SELECT * FROM requirements WHERE id = ?", (out["id"],)).fetchone()
            chat = conn.execute("SELECT created_req_ids FROM req_chats WHERE id = ?",
                                (self.chat_id,)).fetchone()
            ev = conn.execute("SELECT content FROM req_chat_events WHERE kind = 'system' AND "
                              "content LIKE '已建卡 #%'").fetchone()
        self.assertEqual(row["project"], "req-board")
        self.assertEqual(row["status"], "later")
        self.assertEqual(row["source"], "board-reqchat")
        self.assertEqual(chat["created_req_ids"], str(out["id"]))
        self.assertIsNotNone(ev)

    def test_create_requirement_open_and_model(self):
        b._reqchat_set(self.chat_id, model="kimi-k2")
        out = self._call("create_requirement", {"title": "开放卡", "project": "req-board",
                                                "notes": "", "status": "open"})
        self.assertTrue(out["created"])
        with b.get_db() as conn:
            row = conn.execute("SELECT status, model, source FROM requirements WHERE id = ?",
                               (out["id"],)).fetchone()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["model"], "kimi-k2")
        self.assertEqual(row["source"], "board-reqchat")

    def test_create_requirement_rejections(self):
        self._raises("create_requirement", [], "必须为对象")
        self._raises("create_requirement", {"title": "", "project": "req-board"}, "title 必填")
        self._raises("create_requirement", {"title": "x" * 201, "project": "req-board"}, "上限")
        self._raises("create_requirement", {"title": "t", "project": "req-board",
                                            "notes": "x" * 2001}, "上限")
        self._raises("create_requirement", {"title": "t", "project": "other", "notes": ""},
                     "project 必须为所属项目")
        self._raises("create_requirement", {"title": "t", "project": "req-board",
                                            "status": "doing"}, "status 仅支持")
        with b.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM requirements").fetchone()["n"]
        self.assertEqual(n, 1)  # 只有 setUp 的原卡，无越权/非法建卡

    def test_create_requirement_idempotent_same_title(self):
        # #382：同标题幂等——第二次调用返回已有卡，不重复建
        out1 = self._call("create_requirement", {"title": "幂等卡", "project": "req-board"})
        out2 = self._call("create_requirement", {"title": "幂等卡", "project": "req-board"})
        self.assertTrue(out1["created"])
        self.assertFalse(out2["created"])
        self.assertEqual(out2["id"], out1["id"])
        with b.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM requirements").fetchone()["n"]
        self.assertEqual(n, 2)  # setUp 原卡 + 1 张新卡

    def test_create_requirement_multi_different_titles(self):
        # #382：不同标题可建多张——第二张新建成功，清单追加
        out1 = self._call("create_requirement", {"title": "第一张", "project": "req-board"})
        out2 = self._call("create_requirement", {"title": "第二张", "project": "req-board"})
        self.assertTrue(out1["created"])
        self.assertTrue(out2["created"])
        self.assertNotEqual(out2["id"], out1["id"])
        with b.get_db() as conn:
            chat = conn.execute("SELECT created_req_ids FROM req_chats WHERE id = ?",
                                (self.chat_id,)).fetchone()
        self.assertEqual(chat["created_req_ids"], f"{out1['id']},{out2['id']}")

    def test_create_requirement_same_title_after_multi(self):
        # #382：建多张后再建已存在的标题——仍幂等返回该卡
        out1 = self._call("create_requirement", {"title": "重复卡", "project": "req-board"})
        out2 = self._call("create_requirement", {"title": "另一张", "project": "req-board"})
        out3 = self._call("create_requirement", {"title": "重复卡", "project": "req-board"})
        self.assertTrue(out1["created"])
        self.assertTrue(out2["created"])
        self.assertFalse(out3["created"])
        self.assertEqual(out3["id"], out1["id"])

    # ---- 写：改卡片字段 ----
    def test_update_requirement(self):
        out = self._call("update_requirement", {"notes": "新详情", "title": "新标题"})
        self.assertEqual(sorted(out["updated"]), ["notes", "title"])
        with b.get_db() as conn:
            row = conn.execute("SELECT notes, title, summary FROM requirements WHERE id = ?", (self.rid,)).fetchone()
        self.assertEqual(row["notes"], "新详情")
        self.assertEqual(row["title"], "新标题")
        self.assertIsNone(row["summary"])

    def test_update_requirement_rejections(self):
        self._raises("update_requirement", {}, "至少提供")
        self._raises("update_requirement", {"title": "x" * 201}, "上限")
        self._raises("update_requirement", {"notes": "x" * 10001}, "上限")
        # #434 状态写面只开 later/open：doing/done 属调度器流转，写了会与推进打架
        for bad in ("doing", "done", "dropped", "review"):
            self._raises("update_requirement", {"status": bad}, "仅限")
        with b.get_db() as conn:
            row = conn.execute("SELECT status FROM requirements WHERE id = ?", (self.rid,)).fetchone()
        self.assertEqual(row["status"], "later")  # 非法状态整单拒绝，未落部分更新

    def test_update_requirement_status_later_open(self):
        """#434 评审终审「置 open」：状态写入 + req_status_log 审计行 + 回放 status 事件"""
        out = self._call("update_requirement", {"status": "open", "notes": "修订后详情"})
        self.assertEqual(sorted(out["updated"]), ["notes", "status"])
        self.assertEqual(out["status"], "open")
        with b.get_db() as conn:
            row = conn.execute("SELECT status, notes FROM requirements WHERE id = ?", (self.rid,)).fetchone()
            logs = conn.execute("SELECT * FROM req_status_log WHERE req_id = ? ORDER BY id",
                                (self.rid,)).fetchall()
            evs = conn.execute("SELECT content FROM dispatcher_events WHERE req_id = ? AND kind = 'status'",
                               (self.rid,)).fetchall()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["notes"], "修订后详情")
        self.assertEqual([(r["old_status"], r["new_status"], r["actor"]) for r in logs],
                         [("later", "open", "agent")])
        self.assertIn("agent:reqchat", logs[0]["detail"])
        self.assertEqual(len(evs), 1)
        # 置回 later 同样留痕；状态没变则不新增审计行
        self._call("update_requirement", {"status": "later"})
        self._call("update_requirement", {"status": "later"})
        with b.get_db() as conn:
            logs = conn.execute("SELECT old_status, new_status FROM req_status_log WHERE req_id = ? "
                                "ORDER BY id", (self.rid,)).fetchall()
        self.assertEqual([(r["old_status"], r["new_status"]) for r in logs],
                         [("later", "open"), ("open", "later")])

    # ---- 写：评审记录（#434 需求助手跑通 req-board-review 的写回面） ----
    def test_post_review_round_and_reviewer_server_side(self):
        with b.reqchat_lock:
            b.reqchat_runtime[self.chat_id] = {"session": mock.Mock(model_name="DeepSeek V4.1 Flash")}
        try:
            out = self._call("post_review", {"content": "■ 结论\n可进开发", "verdict": "pass"})
        finally:
            with b.reqchat_lock:
                b.reqchat_runtime.pop(self.chat_id, None)
        # round 服务端自增、reviewer 服务端按实时模型填、notes_snapshot 服务端快照
        self.assertEqual((out["round"], out["reviewer"], out["verdict"]), (1, "DeepSeek V4.1 Flash", "pass"))
        with b.get_db() as conn:
            row = conn.execute("SELECT * FROM req_reviews WHERE req_id = ?", (self.rid,)).fetchone()
            ev = conn.execute("SELECT content FROM req_chat_events WHERE kind = 'system' AND "
                              "content LIKE '已追加评审记录%'").fetchone()
        self.assertEqual(row["round"], 1)
        self.assertEqual(row["reviewer"], "DeepSeek V4.1 Flash")
        self.assertEqual(row["content"], "■ 结论\n可进开发")
        self.assertEqual(row["notes_snapshot"], "背景")
        self.assertIn("第 1 轮", ev["content"])
        # 第二轮：round 自增，reviewer 回落 unknown-model（运行时会话已回收、model 列未写）；verdict 省略即空
        out2 = self._call("post_review", {"content": "■ 结论\n第二轮"})
        self.assertEqual((out2["round"], out2["reviewer"], out2["verdict"]), (2, "unknown-model", None))
        with b.get_db() as conn:
            rounds = [r["round"] for r in conn.execute(
                "SELECT round FROM req_reviews WHERE req_id = ? ORDER BY id", (self.rid,)).fetchall()]
        self.assertEqual(rounds, [1, 2])   # append-only：只追加，不改不删

    def test_post_review_rejections(self):
        self._raises("post_review", {}, "content 必填")
        self._raises("post_review", {"content": "   "}, "content 必填")
        self._raises("post_review", {"content": "x" * 20001}, "上限")
        self._raises("post_review", {"content": "ok", "verdict": "approve"}, "verdict 仅限")
        with b.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM req_reviews").fetchone()["c"], 0)

    def test_post_review_binds_own_req(self):
        """req_id 绑定本对话需求：不信任 agent 参数，别的卡的评审不会写进来"""
        other = b.req_add("另一张卡", "req-board", notes="别的详情", status="later")
        self._call("post_review", {"content": "只写本卡", "req_id": other["id"]})
        with b.get_db() as conn:
            mine = conn.execute("SELECT COUNT(*) c FROM req_reviews WHERE req_id = ?",
                                (self.rid,)).fetchone()["c"]
            theirs = conn.execute("SELECT COUNT(*) c FROM req_reviews WHERE req_id = ?",
                                  (other["id"],)).fetchone()["c"]
        self.assertEqual((mine, theirs), (1, 0))

    def test_tools_list_has_post_review(self):
        """#434 tools/list 注册 post_review 与 status 写面（枚举与常量同源）"""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        req = urllib.request.Request(self.srv.url, data=body,
                                     headers={"Content-Type": "application/json"})
        with _NO_PROXY_OPENER.open(req, timeout=5) as resp:
            tools = {t["name"]: t for t in json.loads(resp.read())["result"]["tools"]}
        self.assertIn("post_review", tools)
        self.assertEqual(tools["post_review"]["inputSchema"]["properties"]["verdict"]["enum"],
                         list(b.REQ_CHAT_REVIEW_VERDICTS))
        self.assertEqual(tools["post_review"]["inputSchema"]["required"], ["content"])
        self.assertEqual(tools["update_requirement"]["inputSchema"]["properties"]["status"]["enum"],
                         list(b.REQ_CHAT_STATUS_WRITE))
        self.assertIn("只追加", tools["post_review"]["description"])

    def test_reqchat_write_roots_include_tmp(self):
        """#434 需求助手写白名单 = 项目工作目录 + /tmp + 系统 TMPDIR（与 dev_write_roots 同款）"""
        roots = [Path(r).resolve() for r in b._reqchat_write_roots(str(REPO))]
        self.assertIn(REPO.resolve(), roots)
        self.assertIn(Path("/tmp").resolve(), roots)
        self.assertIn(Path(tempfile.gettempdir()).resolve(), roots)

    def test_reqchat_prompt_review_writeback(self):
        """#434 提示词：新写工具 + 实时模型名 + 会话临时目录 + 沙箱边界（无 curl/不跑脚本）"""
        prompt = b._reqchat_system_prompt(self.rid, model="DeepSeek V4.1 Flash", req_chat_id=self.chat_id)
        for frag in ("post_review", "update_requirement：修改需求详情(notes)",
                     "本会话底层模型：DeepSeek V4.1 Flash",
                     f"/tmp/req_board_reqchat_{self.chat_id}/", "没有 curl", "一律被拒"):
            self.assertIn(frag, prompt)
        # 模型名未知时如实显示，不编造
        self.assertIn("本会话底层模型：未知", b._reqchat_system_prompt(self.rid))

    # ---- 写：产物 ----
    def test_write_artifact(self):
        out = self._call("write_artifact", {"filename": "analysis.md", "content": "# 修订"})
        self.assertTrue(out["written"])
        self.assertEqual((self.req_dir / "analysis.md").read_text(), "# 修订")

    def test_write_artifact_rejections(self):
        self._raises("write_artifact", {"filename": "../escape.md", "content": "x"}, "非法文件名")
        self._raises("write_artifact", {"filename": "a/b.md", "content": "x"}, "非法文件名")
        self._raises("write_artifact", {"filename": "run.sh", "content": "x"}, "不支持的文件类型")
        self._raises("write_artifact", {"filename": "a.md", "content": "x" * 50001}, "上限")

    # ---- 写：TAPD 评论 ----
    def _write_tapd_cfg(self, token="tok123", base="https://api.tapd.cn"):
        cfg = self.tmp / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"tapd": {"env": {
            "TAPD_ACCESS_TOKEN": token, "TAPD_API_BASE_URL": base}}}}))
        b.KIMI_CODE_MCP_JSON = cfg

    @mock.patch("urllib.request.urlopen")
    def test_tapd_comment_posts_with_long_id(self, mock_open):
        self._write_tapd_cfg()
        mock_open.return_value = FakeResp(b'{"status": 1}')
        out = self._call("tapd_comment", {"workspace_id": "456", "entry_id": "123", "comment": "完成"})
        self.assertTrue(out["posted"])
        # 短 ID 补长：云端前缀 11 + workspace_id + 9 位补零
        self.assertEqual(out["entry_id"], "11456000000123")
        req = mock_open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.tapd.cn/comments?s=mcp")
        self.assertEqual(req.headers["Authorization"], "Bearer tok123")
        body = json.loads(req.data)
        self.assertEqual(body["entry_id"], "11456000000123")
        self.assertEqual(body["comment"], "完成")

    @mock.patch("urllib.request.urlopen")
    def test_tapd_comment_long_id_passthrough_and_http_error(self, mock_open):
        self._write_tapd_cfg()
        mock_open.side_effect = RuntimeError("network down")
        self._raises("tapd_comment", {"workspace_id": "1", "entry_id": "11456000000123",
                                      "comment": "c"}, "TAPD 评论失败")
        # 长 ID（>9 位）原样透传
        mock_open.side_effect = None
        mock_open.return_value = FakeResp(b"{}")
        out = self._call("tapd_comment", {"workspace_id": "1", "entry_id": "11456000000123", "comment": "c"})
        self.assertEqual(out["entry_id"], "11456000000123")

    def test_tapd_comment_rejections(self):
        b.KIMI_CODE_MCP_JSON = self.tmp / "no-such-mcp.json"
        self._raises("tapd_comment", {"workspace_id": "456", "entry_id": "123", "comment": "c"},
                     "未找到 TAPD 访问令牌")
        self._write_tapd_cfg()
        self._raises("tapd_comment", {"workspace_id": "x", "entry_id": "123", "comment": "c"}, "workspace_id")
        self._raises("tapd_comment", {"workspace_id": "456", "entry_id": "x", "comment": "c"}, "entry_id")
        self._raises("tapd_comment", {"workspace_id": "456", "entry_id": "123", "comment": ""}, "comment 必填")
        self._raises("tapd_comment", {"workspace_id": "456", "entry_id": "123", "comment": "x" * 2001}, "上限")

    # ---- #324 善后：read_logs（白名单/tail/过滤/截断） ----
    def test_log_whitelist_allowed(self):
        for p in ("/tmp/req_board_dispatcher.log", "/tmp/req_board_agent_12.log"):
            self.assertEqual(b._reqchat_log_allowed(p), str(Path(p).resolve()))

    def test_log_whitelist_automation(self):
        with mock.patch.object(b, "PROJECT_PATHS", {"automation": ["/Users/dev/project/automation"]}):
            for p in ("/Users/dev/project/automation/data/req-board.log",
                      "/Users/dev/project/automation/data/req-board-dispatcher.log",
                      "/Users/dev/project/automation/data/req-board-dispatcher.err.log"):
                self.assertEqual(b._reqchat_log_allowed(p), str(Path(p).resolve()))

    def test_log_whitelist_launchd_dir(self):
        """launchd 日志目录（REQ_BOARD_LOG_DIR）下 req-board*.log 及其轮转档放行，其余拒。"""
        d = self.tmp / "logs"
        d.mkdir()
        with mock.patch.dict(os.environ, {"REQ_BOARD_LOG_DIR": str(d)}):
            for name in ("req-board.log", "req-board.err.log", "req-board.log.1",
                         "req-board-dispatcher.log", "req-board-dispatcher.err.log",
                         "req-board-dispatcher.log.2"):
                self.assertEqual(b._reqchat_log_allowed(str(d / name)), str((d / name).resolve()), name)
            for name in ("dispatcher.log", "req-board.log.bak", "req-board-dispatcher.log.bak",
                         "other.log", "req-board"):  # 不在闭集内一律拒
                self.assertIsNone(b._reqchat_log_allowed(str(d / name)), name)
            # 子目录不放行（只认目录本身的名字闭集）
            sub = d / "sub"
            sub.mkdir()
            self.assertIsNone(b._reqchat_log_allowed(str(sub / "req-board.log")))
            # 目录不存在时判定不依赖文件存在性（只看路径形态）
            self.assertEqual(b._reqchat_log_allowed(str(d / "req-board.err.log")),
                             str((d / "req-board.err.log").resolve()))

    def test_log_whitelist_rejections(self):
        with mock.patch.object(b, "PROJECT_PATHS", {"automation": ["/Users/dev/project/automation"]}):
            bad = ("/etc/passwd", "/tmp/req_board_agent_x.log", "/tmp/other.log",
                   "/Users/dev/project/automation/data/req-board.err.log",  # 需求原文白名单外
                   "/Users/dev/project/automation/data/dispatcher.log",
                   "/Users/dev/project/automation/data/req-board-dispatcher.log.bak",
                   "relative/path.log",
                   "/tmp/req_board_agent_1.log/../../etc/passwd")  # resolve 后越权
            for p in bad:
                self.assertIsNone(b._reqchat_log_allowed(p), p)
        with mock.patch.object(b, "PROJECT_PATHS", {}):  # automation 未配置 → automation 段 fail-closed
            self.assertIsNone(b._reqchat_log_allowed("/Users/dev/project/automation/data/req-board.log"))

    def test_read_logs_tail_and_keyword(self):
        log = self.tmp / "req_board_dispatcher.log"
        log.write_text("\n".join(f"line{i}" for i in range(150)))
        with mock.patch.object(b, "_reqchat_log_allowed", return_value=str(log)):
            out = self._call("read_logs", {"path": "/tmp/req_board_dispatcher.log", "lines": 20})
            self.assertEqual(out["returned"], 20)
            self.assertEqual(out["content"].splitlines()[0], "line130")
            self.assertFalse(out["truncated"])
            out = self._call("read_logs", {"path": "/tmp/req_board_dispatcher.log", "keyword": "line14"})
            self.assertEqual(out["returned"], 10)  # tail 100 行内命中 line140-149
            self.assertIn("line14", out["content"].splitlines()[0])
            out = self._call("read_logs", {"path": "/tmp/req_board_dispatcher.log", "lines": 0})  # 非法回退默认
            self.assertEqual(out["returned"], 100)

    def test_read_logs_truncate_long(self):
        log = self.tmp / "req_board_dispatcher.log"
        log.write_text("x" * (b.REQ_CHAT_READ_MAX + 5000))
        with mock.patch.object(b, "_reqchat_log_allowed", return_value=str(log)):
            out = self._call("read_logs", {"path": "/tmp/req_board_dispatcher.log"})
            self.assertTrue(out["truncated"])
            self.assertLessEqual(len(out["content"]), b.REQ_CHAT_READ_MAX + 50)

    def test_read_logs_rejections(self):
        with mock.patch.object(b, "_reqchat_log_allowed", return_value=None):
            self._raises("read_logs", {"path": "/etc/passwd"}, "不在白名单")
            self._raises("read_logs", {"path": ""}, "不在白名单")
        with mock.patch.object(b, "_reqchat_log_allowed", return_value=str(self.tmp / "missing.log")):
            self._raises("read_logs", {"path": "/tmp/req_board_dispatcher.log"}, "不存在")

    # ---- #324 善后：restart_dispatcher（预检/拒绝/kickstart/健康核对/留痕） ----
    def _events(self):
        with b.get_db() as conn:
            return [r["content"] for r in conn.execute(
                "SELECT content FROM req_chat_events WHERE req_chat_id = ?", (self.chat_id,)).fetchall()]

    def test_restart_dispatcher_ok(self):
        with mock.patch.object(b, "_dispatcher_kimi_agents", return_value=[]), \
             mock.patch.object(b, "_reqchat_dispatcher_health", return_value={
                 "heartbeat_updated": True, "health": {"ok": True, "age": 3},
                 "detail": "心跳文件已更新；/api/dispatcher-health ok（age 3s）"}), \
             mock.patch("board.subprocess.run", return_value=FakeCompleted()) as mrun:
            out = self._call("restart_dispatcher", {})
        self.assertTrue(out["restarted"])
        self.assertTrue(out["health"]["ok"])
        self.assertEqual(out["label"], "local.automation.req-board-dispatcher")
        mrun.assert_called_once()
        self.assertIn("local.automation.req-board-dispatcher", " ".join(mrun.call_args.args[0]))
        self.assertTrue(any("已重启调度器" in e and "心跳" in e for e in self._events()))

    def test_restart_dispatcher_refuses_when_agent_running(self):
        with mock.patch.object(b, "_dispatcher_kimi_agents", return_value=["kimi acp -- /tmp/x"]), \
             mock.patch("board.subprocess.run") as mrun:
            self._raises("restart_dispatcher", {}, "运行中 kimi agent")
        mrun.assert_not_called()  # 预检拒绝时不执行任何命令
        self.assertTrue(any("拒绝重启调度器" in e for e in self._events()))

    def test_restart_dispatcher_kickstart_fail(self):
        with mock.patch.object(b, "_dispatcher_kimi_agents", return_value=[]), \
             mock.patch("board.subprocess.run", return_value=FakeCompleted(returncode=1, stderr=b"boom")):
            self._raises("restart_dispatcher", {}, "重启调度器失败")
        self.assertTrue(any("重启调度器失败" in e for e in self._events()))

    def test_dispatcher_kimi_agents_children_detected(self):
        ps_out = (
            "4242  1  python3 /Users/dev/project/req-board/dispatcher.py\n"
            "5001  4242  kimi acp --model x\n"
            "5002  4242  kimi acp --model y\n"
            "9001  999  kimi-chat-acp\n")
        with mock.patch("board.subprocess.run", side_effect=[
                FakeCompleted(stdout=b"\tpid = 4242\n"),  # 真实 launchctl print 带 tab 缩进
                FakeCompleted(stdout=ps_out.encode())]):
            hit = b._dispatcher_kimi_agents()
        self.assertEqual(len(hit), 2)
        self.assertTrue(all("kimi" in h for h in hit))

    def test_dispatcher_kimi_agents_nested_and_self_excluded(self):
        # 嵌套子进程（bash 包装层命令行含 kimi 同样命中，属保守拒绝）
        ps_out = (
            "4242  1  python3 dispatcher.py\n"
            "5100  4242  /bin/bash -c kimi acp\n"
            "5101  5100  kimi acp\n")
        with mock.patch("board.subprocess.run", side_effect=[
                FakeCompleted(stdout=b"\tpid = 4242\n"),
                FakeCompleted(stdout=ps_out.encode())]):
            self.assertEqual(len(b._dispatcher_kimi_agents()), 2)
        # 对话自身（kimi-chat-acp，父进程=board 非 dispatcher）不误判
        ps_out2 = ("4242  1  python3 dispatcher.py\n" "9001  999  kimi-chat-acp\n")
        with mock.patch("board.subprocess.run", side_effect=[
                FakeCompleted(stdout=b"\tpid = 4242\n"),
                FakeCompleted(stdout=ps_out2.encode())]):
            self.assertEqual(b._dispatcher_kimi_agents(), [])
        # 调度器服务不在运行（launchctl print 失败）→ 视为无 agent
        with mock.patch("board.subprocess.run", return_value=FakeCompleted(returncode=1)):
            self.assertEqual(b._dispatcher_kimi_agents(), [])

    def test_dispatcher_health_check(self):
        with mock.patch.object(b, "_reqchat_heartbeat_ts", side_effect=[100, 200]), \
             mock.patch("urllib.request.urlopen", return_value=FakeResp(b'{"ok": true, "age": 3}')):
            out = b._reqchat_dispatcher_health()
        self.assertTrue(out["heartbeat_updated"])
        self.assertTrue(out["health"]["ok"])
        self.assertIn("ok", out["detail"])

    def test_dispatcher_health_stale(self):
        with mock.patch.object(b, "_reqchat_heartbeat_ts", return_value=100), \
             mock.patch.object(b, "REQ_CHAT_HEALTH_WAIT", 0.2), \
             mock.patch("urllib.request.urlopen", side_effect=RuntimeError("board down")):
            out = b._reqchat_dispatcher_health()
        self.assertFalse(out["heartbeat_updated"])
        self.assertFalse(out["health"]["ok"])
        self.assertIn("查询失败", out["detail"])

    # ---- #324 善后：restart_project_service（配方白名单/执行/就绪校验/留痕） ----
    def test_restart_project_service_ok(self):
        with mock.patch.object(b, "PROJECT_RESTART",
                               {"test-svc": {"cmd": "./start.sh", "verify": "http://127.0.0.1:9/x", "timeout": 5}}), \
             mock.patch.object(b, "PROJECT_PATHS", {"test-svc": ["/tmp"]}), \
             mock.patch("board.subprocess.run", return_value=FakeCompleted()) as mrun, \
             mock.patch.object(b, "_reqchat_wait_url_up", return_value=True):
            out = self._call("restart_project_service", {"service": "test-svc"})
        self.assertTrue(out["restarted"])
        self.assertTrue(out["ready"])
        self.assertEqual(mrun.call_args.args[0], "./start.sh")
        self.assertEqual(mrun.call_args.kwargs["cwd"], "/tmp")
        self.assertTrue(any("重启项目服务" in e and "就绪校验" in e for e in self._events()))

    def test_restart_project_service_not_ready(self):
        # 命令成功但 verify 未通过：返回 ready=False（不抛错，agent 可再查日志）
        with mock.patch.object(b, "PROJECT_RESTART",
                               {"test-svc": {"cmd": "./start.sh", "verify": "http://127.0.0.1:9/x", "timeout": 5}}), \
             mock.patch.object(b, "PROJECT_PATHS", {"test-svc": ["/tmp"]}), \
             mock.patch("board.subprocess.run", return_value=FakeCompleted()), \
             mock.patch.object(b, "_reqchat_wait_url_up", return_value=False):
            out = self._call("restart_project_service", {"service": "test-svc"})
        self.assertTrue(out["restarted"])
        self.assertFalse(out["ready"])

    def test_restart_project_service_rejections(self):
        self._raises("restart_project_service", {"service": "req-board"}, "不在重启白名单")
        self._raises("restart_project_service", {"service": "unknown"}, "不在重启白名单")
        with mock.patch.object(b, "PROJECT_RESTART", {"svc": {"cmd": "true", "verify": "", "timeout": 5}}), \
             mock.patch.object(b, "PROJECT_PATHS", {}):
            self._raises("restart_project_service", {"service": "svc"}, "未配置本地路径")
        with mock.patch.object(b, "PROJECT_RESTART", {"svc": {"cmd": "true", "verify": "", "timeout": 5}}), \
             mock.patch.object(b, "PROJECT_PATHS", {"svc": ["/tmp"]}), \
             mock.patch("board.subprocess.run", return_value=FakeCompleted(returncode=1, stderr=b"boom")):
            self._raises("restart_project_service", {"service": "svc"}, "重启 svc 失败")
        with mock.patch.object(b, "PROJECT_RESTART", {"svc": {"cmd": "true", "verify": "", "timeout": 5}}), \
             mock.patch.object(b, "PROJECT_PATHS", {"svc": ["/tmp"]}), \
             mock.patch("board.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self._raises("restart_project_service", {"service": "svc"}, "超时")

    def test_projects_restart_config_parse(self):
        proj = self.tmp / "projects.json"
        proj.write_text(json.dumps({
            "a": "/Users/x/a",
            "b": {"paths": ["/Users/x/b"], "restart": {"cmd": "./s.sh", "verify": "http://127.0.0.1:9"}},
            "c": {"paths": ["/Users/x/c"], "restart": {"cmd": "x", "timeout": "bad"}},
            "d": {"paths": ["/Users/x/d"], "restart": {"verify": "http://x"}},
        }))
        with mock.patch.object(b, "BASE_DIR", self.tmp):
            cfg = b._projects_restart_config()
        self.assertEqual(list(cfg), ["b"])
        self.assertEqual(cfg["b"]["cmd"], "./s.sh")
        self.assertEqual(cfg["b"]["verify"], "http://127.0.0.1:9")
        self.assertEqual(cfg["b"]["timeout"], 30)

    # ---- #324 善后：tools/list 注册 + 系统提示词 ----
    def test_tools_list_has_housekeeping(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        req = urllib.request.Request(self.srv.url, data=body,
                                     headers={"Content-Type": "application/json"})
        with _NO_PROXY_OPENER.open(req, timeout=5) as resp:
            tools = {t["name"]: t for t in json.loads(resp.read())["result"]["tools"]}
        for name in ("restart_dispatcher", "read_logs", "restart_project_service"):
            self.assertIn(name, tools)
        self.assertIn("【写·善后】", tools["restart_dispatcher"]["description"])
        self.assertIn("【读·善后】", tools["read_logs"]["description"])
        self.assertIn("【写·善后】", tools["restart_project_service"]["description"])
        self.assertIn("required", tools["read_logs"]["inputSchema"])

    def test_reqchat_prompt_mentions_housekeeping(self):
        prompt = b._reqchat_system_prompt(self.rid)
        for frag in ("restart_dispatcher", "restart_project_service", "read_logs", "默认只读纪律不变"):
            self.assertIn(frag, prompt)


class ReqChatMigrationTest(unittest.TestCase):
    """验收：#382 旧库（created_req_id 单卡字段）init_db 迁移为 created_req_ids 多卡清单"""

    def test_migrate_created_req_ids_from_created_req_id(self):
        tmp = Path(tempfile.mkdtemp(prefix="req382-mig-"))
        orig_db = b.DB_PATH
        try:
            b.DB_PATH = tmp / "board.db"
            # 造旧结构：req_chats 为 9 列旧版 + created_req_id，无 created_req_ids
            with sqlite3.connect(b.DB_PATH) as conn:
                conn.execute("""
                    CREATE TABLE req_chats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        req_id INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'created',
                        session_id TEXT, model TEXT, error TEXT,
                        interrupt_source TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        created_req_id INTEGER
                    )""")
                conn.execute("INSERT INTO req_chats (req_id, status, created_req_id) VALUES (1, 'closed', 381)")
                conn.commit()
            b.init_db()
            with b.get_db() as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(req_chats)")}
                self.assertIn("created_req_ids", cols)
                self.assertIn("context_pct", cols)  # #383 头部 used 展示列
                row = conn.execute("SELECT created_req_ids FROM req_chats WHERE id = 1").fetchone()
            self.assertEqual(row["created_req_ids"], "381")
        finally:
            b.DB_PATH = orig_db
            shutil.rmtree(tmp, ignore_errors=True)

    def test_chat_api_returns_context_pct(self):
        """#383：/chat GET 透传 context_pct（前端「used xx%」头部展示数据源）"""
        tmp = Path(tempfile.mkdtemp(prefix="req383-api-"))
        orig_db = b.DB_PATH
        try:
            b.DB_PATH = tmp / "board.db"
            b.init_db()
            req = b.req_add("需求", "req-board", notes="背景", status="later")
            chat_id = b._reqchat_create(req["id"])
            b._reqchat_set(chat_id, context_pct="35%")
            with b.app.test_client() as c:
                r = c.get(f"/api/requirements/{req['id']}/chat")
                self.assertEqual(r.status_code, 200)
                chat = r.get_json()["chat"]
                self.assertEqual(chat["context_pct"], "35%")
        finally:
            b.DB_PATH = orig_db
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
