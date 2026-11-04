#!/usr/bin/env python3
"""req-102 防复发：result.json 非法 JSON 时 handle_result 完整行为测试。

改动：handle_result 解析失败不再 unlink 现场，改为改名备份（result.json.invalid，
os.replace 覆盖旧备份防多次重试堆积）；错误信息含行/列（JSONDecodeError 自带）与文件路径；
失败原因区分「文件缺失 vs 文件非法」；重试 extra 提示先读备份自查。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import shutil
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

# 测试会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "spawn_agent", "mark_session_poisoned", "api")}

# summary 字段带未转义裸换行（Invalid control character），复刻需求 #99 失败现场
INVALID_JSON = '{"status": "done", "summary": "第一行\n裸换行非法", "question": null}'


def fresh_state():
    return {"chat_id": "oc_req102_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


class HandleResultInvalidJsonTest(unittest.TestCase):
    """handle_result 解析失败：保留现场（改名备份）、原因区分、错误含行/列与路径、自动重试"""

    def setUp(self):
        d.state = fresh_state()
        self._orig_artifact_dir = d.artifact_dir
        self.art = Path(tempfile.mkdtemp(prefix="req102-art-"))
        d.artifact_dir = lambda req: self.art  # 产物目录指向临时目录，避免写真实 ~/.req-board
        self.status = []
        self.summaries = []
        self.events = []
        self.notifies = []
        self.card_notifies = []   # notify_req_card 记录 (event, kwargs)（重试耗尽通知改走卡片）
        self.spawns = []
        self._orig_notify = d.notify
        self._orig_card = d.notify_req_card
        d.save_state = lambda: None
        d.set_status = lambda rid, st: self.status.append((rid, st))
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify = lambda text, *a, **k: self.notifies.append(text)
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append((event, k))
        d.spawn_agent = lambda req, role=None, extra="", resume=False: self.spawns.append((role, extra, resume))
        d.mark_session_poisoned = lambda *a, **k: None
        # #113 红线归因新增事件查询（GET /<id>/events）：默认返回无红线事件，测试完全离线化
        d.api = lambda method, path, body=None: []

    def tearDown(self):
        d.artifact_dir = self._orig_artifact_dir
        for name, value in _D_REALS.items():
            setattr(d, name, value)
        shutil.rmtree(self.art, ignore_errors=True)

    def _req(self, rid=102):
        return {"id": rid, "project": "req-board", "title": "T", "mode": "auto"}

    def _write_result(self, content):
        (self.art / "result.json").write_text(content)

    def test_t1_invalid_json_keep_scene_and_retry(self):
        """验收标准 2/3/4：解析失败 → 原文件改名保留、错误含行/列与路径、触发自动重试"""
        self._write_result(INVALID_JSON)
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        # 现场保留：result.json 改名，备份内容为原始非法内容
        self.assertFalse((self.art / "result.json").exists())
        backup = self.art / "result.json.invalid"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(), INVALID_JSON)
        # 触发自动重试（resume），原因区分：文案含「非法 JSON」与路径、行/列
        self.assertEqual(len(self.spawns), 1)
        role, extra, resume = self.spawns[0]
        self.assertTrue(resume)
        self.assertIn("非法 JSON", extra)
        self.assertIn("result.json", extra)          # 文件路径
        self.assertIn("line", extra)                 # JSONDecodeError 自带行号
        self.assertIn("column", extra)               # JSONDecodeError 自带列号
        self.assertIn("result.json.invalid", extra)  # 备份路径提示先读取自查
        self.assertIn("json.dumps", extra)           # 写入规范引导
        # 时间线事件也带原因
        self.assertTrue(any("非法 JSON" in c for _, c in self.events))

    def test_t2_retries_exhausted_kept_scene(self):
        """重试耗尽转 later（#113）：现场仍保留，飞书通知/summary 原因清晰（非法 JSON + 路径）"""
        d.state["retries"]["102"] = d.MAX_RETRIES
        self._write_result(INVALID_JSON)
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(self.status, [(102, "later")])
        self.assertTrue((self.art / "result.json.invalid").exists())
        self.assertFalse((self.art / "result.json").exists())
        self.assertTrue(any("非法 JSON" in s and "result.json" in s for s in self.summaries))
        # 重试耗尽通知改走 failed 卡片：reason 含「非法 JSON」
        failed_cards = [(e, k) for e, k in self.card_notifies if e == "failed"]
        self.assertTrue(failed_cards, self.card_notifies)
        self.assertIn("非法 JSON", failed_cards[0][1]["reason"])

    def test_t3_valid_json_blocked_no_retry(self):
        """#385 改写原回归：合法 result.json（blocked，正常退出）→ 分流——不重试直接转 later、
        文件删除无备份、通知/事件/summary 留痕（原 t3 断言 blocked 后重试，与收窄后的语义冲突）"""
        self._write_result(json.dumps({"status": "blocked", "summary": "环境依赖装不上"}))
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        self.assertFalse((self.art / "result.json").exists())
        self.assertFalse((self.art / "result.json.invalid").exists())
        self.assertEqual(self.spawns, [])                                        # 不重试（验收①）
        self.assertEqual(self.status, [(102, "later")])                          # 卡片转 later
        self.assertTrue(any("需求阻塞" in c for _, c in self.events))            # 事件留痕
        blocked_cards = [(e, k) for e, k in self.card_notifies if e == "blocked"]
        self.assertTrue(blocked_cards, self.card_notifies)                       # 通知走 blocked 卡片
        self.assertIn("环境依赖装不上", blocked_cards[0][1]["reason"])
        self.assertTrue(any("需求阻塞" in s and "环境依赖装不上" in s for s in self.summaries))

    def test_t4_legacy_invalid_kept(self):
        """旧约定兜底：工作目录根 .req-board-result.json 非法 → 同样改名保留现场"""
        with tempfile.TemporaryDirectory(prefix="req102-wd-") as wd:
            legacy = Path(wd) / d.RESULT_FILENAME
            legacy.write_text(INVALID_JSON)
            with mock.patch.object(d, "workdir_for", return_value=Path(wd)):
                d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
            self.assertFalse(legacy.exists())
            self.assertTrue((Path(wd) / (d.RESULT_FILENAME + ".invalid")).exists())

    def test_t5_repeat_failure_no_stacking(self):
        """多次失败：备份被 os.replace 覆盖，不堆积多个 .invalid 文件"""
        self._write_result(INVALID_JSON)
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        content2 = INVALID_JSON.replace("第一行", "第二行")
        self._write_result(content2)
        d.handle_result(self._req(), "dev", returncode=0, timed_out=False, started_at=time.time())
        invalid_files = [p for p in self.art.iterdir() if p.name.endswith(".invalid")]
        self.assertEqual(len(invalid_files), 1)
        self.assertEqual((self.art / "result.json.invalid").read_text(), content2)

    def test_t6_done_req_no_rollback(self):
        """#284 现场防御：需求已 done 后迟到的会话收尾（accept 续跑）不得回卷状态/重复通知/触发重试——
        现场文件已清理（解析成功即删），但不得 set_status(review)、不得再发评审/done 卡片"""
        self._write_result(json.dumps({"status": "done", "summary": "收尾总结"}))
        req = self._req()
        req["status"] = "done"
        req["mode"] = "semi"
        d.handle_result(req, "accept", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(self.status, [])          # 不 set review（不回卷）
        self.assertEqual(self.card_notifies, [])   # 不重复发通知
        self.assertEqual(self.spawns, [])          # 不触发重试
        self.assertFalse((self.art / "result.json").exists())  # 现场已清理
        self.assertFalse((self.art / "result.json.invalid").exists())

    def test_t7_review_state_retired_no_pending_review(self):
        """#419：review 中间态退役——accept 节点完成不再写 status=review / pending_review，
        而是按卡上快照推进（末节点前进了即收尾，原「重复通知」问题不复存在）"""
        (self.art / "accept.md").write_text("验收结论")  # accept valid_done 产物契约
        self._write_result(json.dumps({"status": "done", "summary": "问答收尾"}))
        req = self._req()
        req["status"] = "doing"
        req["mode"] = "semi"
        req["phase"] = "accept"          # standard：accept 节点下标 2，下一节点 = terminal(3)
        req["flow"] = "standard"         # 卡上流程 id（缺省会兜底默认流程 heavy）
        # 本文件 tearDown 只恢复 _D_REALS 内的名字 → 用 patch.object 保证自动还原，防跨文件污染
        with mock.patch.object(d, "set_req", lambda rid, fields: req.update(fields)), \
             mock.patch.object(d, "fetch_req", lambda rid: req), \
             mock.patch.object(d, "_spawn_safe", lambda *a, **k: None), \
             mock.patch.object(d, "finalize_done", lambda req2, summary=None: None):
            d.handle_result(req, "accept", returncode=0, timed_out=False, started_at=time.time())
        self.assertEqual(self.status, [])                        # 状态收缩 5 值：不写 review
        self.assertEqual(d.state.get("pending_review"), {})      # 无评审中间态
        self.assertEqual(self.card_notifies, [])
        self.assertEqual(req["node_index"], 3)                   # 推进到 terminal（收尾节点）


if __name__ == "__main__":
    unittest.main()
