#!/usr/bin/env python3
"""req-279 done 通知自动展示 agent 声明的遗留待办（todos，红字）测试。

改动：result.json 新增可选字段 todos（字符串数组）——dev/accept agent 声明遗留人工操作；
handle_result 解析 result.json 时提取（解析成功即删文件，须在此暂存 state["req_todos"]）；
_set_done 消费：逐条写入 summary 留档 + done 卡片逐条红字（<font color='red'>⚠️ 待办：…</font>）
渲染在「产物」行下方（reminder 之后）+ 降级纯文本同样包含；消费后清理防 state 膨胀。
semi 通道 dev 的 todos 暂存跨阶段保留，accept 有 todos 覆盖、无则保留 dev 的。
兼容旧 result.json（无 todos 字段不报错、不渲染）。

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

# 各测试类会替换多个 dispatcher 模块级函数；统一保存真实引用，tearDown 恢复防跨文件污染
_D_REALS = {name: getattr(d, name) for name in (
    "state", "save_state", "set_status", "append_summary", "post_dispatcher_event",
    "notify", "notify_req_card", "_kickstart_board_if_own", "merge_dev_branches",
    "cleanup_dev_worktrees", "fetch_req", "_set_done", "finalize_done",
    "set_req", "_spawn_safe", "clear_retries", "has_code_changes", "artifact_dir")}


def _restore_d():
    for name, value in _D_REALS.items():
        setattr(d, name, value)


def fresh_state():
    return {"chat_id": "oc_req279_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


def fake_req(rid=279, title="T", mode=None):
    req = {"id": rid, "project": "req-board", "title": title}
    if mode:
        req["mode"] = mode
    return req


def art_dir(req):
    """mock artifact_dir：落在 artifacts 根内（req-board/req-<id>），供短路径断言"""
    return d.ARTIFACTS_ROOT / req["project"] / f"req-{req['id']}"


class ExtractTodosTest(unittest.TestCase):
    """_extract_todos 纯函数：兼容旧格式宁缺勿滥"""

    def test_missing_or_non_dict_returns_none(self):
        self.assertIsNone(d._extract_todos(None))
        self.assertIsNone(d._extract_todos({}))
        self.assertIsNone(d._extract_todos("x"))

    def test_non_list_ignored(self):
        self.assertIsNone(d._extract_todos({"status": "done", "todos": "abc"}))
        self.assertIsNone(d._extract_todos({"status": "done", "todos": 123}))

    def test_normal_list_kept(self):
        self.assertEqual(d._extract_todos({"todos": ["合并 f-x 分支", "重启 5002 服务"]}),
                         ["合并 f-x 分支", "重启 5002 服务"])

    def test_empty_list_kept_as_explicit_none(self):
        """显式空数组 = 声明无待办（可覆盖 dev 声明），非 None"""
        self.assertEqual(d._extract_todos({"todos": []}), [])

    def test_invalid_elements_filtered(self):
        self.assertEqual(d._extract_todos({"todos": ["ok", 123, None, "", "  "]}), ["ok"])

    def test_all_invalid_returns_none(self):
        """有元素但全部非法 → 视为未声明（不覆盖已声明的值）"""
        self.assertIsNone(d._extract_todos({"todos": [123, None]}))

    def test_whitespace_stripped(self):
        self.assertEqual(d._extract_todos({"todos": ["  合并分支  "]}), ["合并分支"])


class HandleResultTodosTest(unittest.TestCase):
    """handle_result 提取 todos 并暂存 state["req_todos"]（result 文件解析成功即删，须在此时捕获）"""

    def setUp(self):
        d.state = fresh_state()
        d.save_state = lambda: None
        d.clear_retries = lambda *a, **k: None
        d.set_req = lambda *a, **k: None
        d.set_status = lambda *a, **k: None  # #279 教训：漏 mock 会真实 PATCH 看板（279 曾被置 review）
        d.fetch_req = lambda rid: {"id": rid, "project": "req-board", "title": "T", "mode": "semi"}
        d.append_summary = lambda *a, **k: None
        d.post_dispatcher_event = lambda *a, **k: None
        d._spawn_safe = lambda *a, **k: None
        d.finalize_done = mock.Mock()
        d.notify_req_card = lambda *a, **k: None
        d.has_code_changes = lambda *a, **k: True
        self.tmp = Path(tempfile.mkdtemp(prefix="req279-"))
        d.ARTIFACTS_ROOT = self.tmp

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_result(self, req, obj):
        f = d.artifact_dir(req) / "result.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def _write_accept_md(self, req):
        """accept 角色 valid_done 前置：产物 accept.md 存在且非空（artifact_ok 校验）"""
        f = d.artifact_dir(req) / "accept.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("验收通过", encoding="utf-8")

    def test_auto_dev_todos_stored(self):
        req = fake_req(mode="auto")
        self._write_result(req, {"status": "done", "summary": "S", "todos": ["合并 f-x 分支", "重启服务"]})
        d.handle_result(req, "dev", 0, False, time.time())
        self.assertEqual(d.state.get("req_todos", {}).get("279"), ["合并 f-x 分支", "重启服务"])
        # #419：auto dev done 不再直接合并——快照推进到 review 节点并拉起只读审查会话，
        # todos 留待评审通过后的 finalize_done 消费
        d.finalize_done.assert_not_called()

    def test_semi_dev_todos_stored_for_later(self):
        """semi dev 完成进入 accept：todos 跨阶段暂存（验收通过后才展示）"""
        req = fake_req(mode="semi")
        self._write_result(req, {"status": "done", "summary": "S", "todos": ["重启 5002 服务"]})
        d.handle_result(req, "dev", 0, False, time.time())
        self.assertEqual(d.state.get("req_todos", {}).get("279"), ["重启 5002 服务"])
        d.finalize_done.assert_not_called()  # 未直接 done

    def test_no_todos_nothing_stored(self):
        """旧 result.json 无 todos 字段 → 不暂存（验收标准：不带 todos 与现状完全一致）"""
        req = fake_req(mode="auto")
        self._write_result(req, {"status": "done", "summary": "S"})
        d.handle_result(req, "dev", 0, False, time.time())
        self.assertNotIn("req_todos", d.state)

    def test_invalid_todos_ignored(self):
        """todos 非数组（旧格式/坏数据）→ 不报错、不暂存"""
        req = fake_req(mode="auto")
        self._write_result(req, {"status": "done", "summary": "S", "todos": "not-a-list"})
        d.handle_result(req, "dev", 0, False, time.time())
        self.assertNotIn("req_todos", d.state)

    def test_accept_overrides_dev_todos(self):
        """accept 声明 todos → 覆盖 dev 的"""
        d.state.setdefault("req_todos", {})["279"] = ["dev 待办"]
        req = fake_req()
        self._write_accept_md(req)
        self._write_result(req, {"status": "done", "summary": "S", "todos": ["accept 待办"]})
        d.handle_result(req, "accept", 0, False, time.time())
        self.assertEqual(d.state["req_todos"]["279"], ["accept 待办"])

    def test_accept_without_todos_keeps_dev(self):
        """accept 未声明 todos → 保留 dev 的"""
        d.state.setdefault("req_todos", {})["279"] = ["dev 待办"]
        req = fake_req()
        self._write_accept_md(req)
        self._write_result(req, {"status": "done", "summary": "S"})
        d.handle_result(req, "accept", 0, False, time.time())
        self.assertEqual(d.state["req_todos"]["279"], ["dev 待办"])

    def test_accept_empty_todos_clears_dev(self):
        """accept 显式空数组 = 声明无待办 → 覆盖清空 dev 的"""
        d.state.setdefault("req_todos", {})["279"] = ["dev 待办"]
        req = fake_req()
        self._write_accept_md(req)
        self._write_result(req, {"status": "done", "summary": "S", "todos": []})
        d.handle_result(req, "accept", 0, False, time.time())
        self.assertEqual(d.state["req_todos"]["279"], [])


class SetDoneTodosTest(unittest.TestCase):
    """_set_done 消费 state["req_todos"]：summary 逐条留档 + 卡片传 todos + 消费后清理"""

    def setUp(self):
        d.state = fresh_state()
        d._set_done = _D_REALS["_set_done"]  # 用真实函数
        self.status = []
        self.summaries = []
        self.events = []
        self.card_notifies = []
        d.save_state = lambda: None
        d.set_status = lambda rid, st: self.status.append((rid, st))
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda rid, content, kind="action": self.events.append((kind, content))
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append(k)
        d._kickstart_board_if_own = lambda *a, **k: None

    def tearDown(self):
        _restore_d()

    def test_todos_passed_to_card_summary_and_cleared(self):
        """验收标准：done 卡片带逐条 todos；summary 逐条留档；消费后清理 state"""
        d.state["req_todos"] = {"279": ["待办A", "待办B"]}
        d._set_done(fake_req())
        self.assertEqual(self.status, [(279, "done")])
        self.assertEqual(len(self.card_notifies), 1)
        self.assertEqual(self.card_notifies[0]["todos"], ["待办A", "待办B"])
        self.assertFalse(self.card_notifies[0]["company"])
        self.assertTrue(any("待办：待办A" in s for s in self.summaries))
        self.assertTrue(any("待办：待办B" in s for s in self.summaries))
        self.assertNotIn("279", d.state["req_todos"])  # 消费后清理（防 state 膨胀）
        self.assertTrue(any("遗留待办 2 条" in c for _, c in self.events))

    def test_no_todos_keeps_behavior(self):
        """无 todos → 卡片 todos 为空、不写 summary/事件（与现状完全一致）"""
        d._set_done(fake_req())
        self.assertEqual(len(self.card_notifies), 1)
        self.assertEqual(self.card_notifies[0]["todos"], [])
        self.assertEqual(self.summaries, [])
        self.assertEqual(self.events, [])
        self.assertNotIn("279", d.state.get("req_todos", {}))

    def test_reminder_and_todos_coexist(self):
        """与 dispatcher 自动重启提醒并存：reminder 与 todos 同时传给卡片"""
        d.state["req_todos"] = {"279": ["合并分支"]}
        d._set_done(fake_req(), dispatcher_touched=True)
        kw = self.card_notifies[0]
        self.assertIn("dispatcher.py", kw["reminder"])
        self.assertEqual(kw["todos"], ["合并分支"])
        self.assertTrue(any("自动重启" in s for s in self.summaries))
        self.assertTrue(any("待办：合并分支" in s for s in self.summaries))

    def test_append_summary_failure_does_not_block_card(self):
        """summary 写失败不阻断 done 卡片（与 #266 提醒一致）"""
        d.state["req_todos"] = {"279": ["待办A"]}
        d.append_summary = mock.Mock(side_effect=RuntimeError("board down"))
        d._set_done(fake_req())
        self.assertEqual(len(self.card_notifies), 1)
        self.assertEqual(self.card_notifies[0]["todos"], ["待办A"])


class DoneCardTodosTest(unittest.TestCase):
    """build_req_card done：todos 逐条红字渲染在「产物」行下方；无 todos 与现状一致"""

    def setUp(self):
        self.orig_artifact_dir = d.artifact_dir
        d.artifact_dir = art_dir

    def tearDown(self):
        d.artifact_dir = self.orig_artifact_dir

    def _footer_of(self, card):
        return card["elements"][-1]["text"]["content"]

    def test_done_card_todos_red_lines_below_products(self):
        """验收标准：带 todos 的 done 卡片底部逐条红字展示，位于「产物」行下方"""
        card = d.build_req_card("done", fake_req(), summary="S", company=False,
                                todos=["合并 f-20260303-bill 分支并重启 5002 服务", "人工回归 3 条"])
        footer = self._footer_of(card)
        self.assertIn("产物：req-board/req-279/result.json", footer)
        self.assertIn("<font color='red'>⚠️ 待办：合并 f-20260303-bill 分支并重启 5002 服务</font>", footer)
        self.assertIn("<font color='red'>⚠️ 待办：人工回归 3 条</font>", footer)
        self.assertLess(footer.index("产物："), footer.index("待办：合并"))
        self.assertLess(footer.index("待办：合并"), footer.index("待办：人工回归"))  # 逐条按序

    def test_done_card_no_todos_unchanged(self):
        """不带 todos → 卡片与现状完全一致（只有产物行，无红字待办）"""
        card = d.build_req_card("done", fake_req(), summary="S", company=False)
        footer = self._footer_of(card)
        self.assertEqual(footer, f"产物：{d.artifact_short(fake_req(), 'result.json')}")
        self.assertNotIn("待办", footer)

    def test_done_card_reminder_then_todos(self):
        """reminder（dispatcher 重启提醒）在上、todos 逐条在下"""
        card = d.build_req_card("done", fake_req(), summary="S", company=False,
                                reminder="\n⚠️ 改动涉及 dispatcher.py，需手动重启调度器",
                                todos=["待办A"])
        footer = self._footer_of(card)
        self.assertLess(footer.index("需手动重启调度器"), footer.index("待办：待办A"))

    def test_done_card_todos_escaped_and_truncated(self):
        """todos 内容不可信：先截断后转义，<font> 标签硬编码不被转义"""
        todo = "合并 <a> & b"
        card = d.build_req_card("done", fake_req(), summary="S", company=False, todos=[todo])
        footer = self._footer_of(card)
        self.assertIn("<font color='red'>⚠️ 待办：合并 &lt;a&gt; &amp; b</font>", footer)
        long_todo = "x" * 600
        card2 = d.build_req_card("done", fake_req(), summary="S", company=False, todos=[long_todo])
        self.assertIn("⚠️ 待办：" + "x" * 500 + "…", self._footer_of(card2))


class FallbackTextTodosTest(unittest.TestCase):
    """fallback_text done：卡片发送失败降级纯文本同样包含逐条待办（验收标准 3）"""

    def test_done_fallback_contains_todos(self):
        text = d.fallback_text("done", fake_req(title="需求标题"), summary="S", company=False,
                               reminder="", todos=["待办A", "待办B"])
        self.assertIn("✅ 需求 #279 已完成：需求标题", text)
        self.assertIn("\n⚠️ 待办：待办A", text)
        self.assertIn("\n⚠️ 待办：待办B", text)

    def test_done_fallback_company_reminder_and_todos(self):
        text = d.fallback_text("done", fake_req(title="需求标题"), summary="S", company=True,
                               reminder="\n⚠️ 需重启", todos=["合并分支"])
        self.assertIn("（开发分支已自动 push 到远端，待发版合并）", text)
        self.assertIn("需重启", text)
        self.assertIn("\n⚠️ 待办：合并分支", text)
        # 顺序：reminder 在待办之前
        self.assertLess(text.index("需重启"), text.index("待办：合并分支"))

    def test_done_fallback_no_todos_unchanged(self):
        self.assertEqual(d.fallback_text("done", fake_req(title="需求标题"), summary="S", company=False,
                                         reminder=""),
                         "✅ 需求 #279 已完成：需求标题")
        self.assertEqual(d.fallback_text("done", fake_req(title="需求标题"), summary="S", company=True,
                                         reminder="\n⚠️ 重启提醒"),
                         "✅ 需求 #279 已完成：需求标题（开发分支已自动 push 到远端，待发版合并）\n⚠️ 重启提醒")


class AutoDoneFullPathTest(unittest.TestCase):
    """auto 流程完成全链路（#419 快照驱动）：result.json todos → handle_result 暂存 →
    dev 完成推进到 review 节点（拉起只读审查）→ 评审 pass 推进到 terminal → 内联收尾 finalize_done
    → _set_done 消费 → done 卡片带 todos + summary 留档（真实 _set_done/_extract_todos 消费逻辑）"""

    def setUp(self):
        d.state = fresh_state()
        d.save_state = lambda: None
        d.set_status = lambda *a, **k: None
        d.fetch_req = lambda rid: None
        d._spawn_safe = lambda *a, **k: None  # #337：dev done 后拉起 review 会话，不在本链路实测
        self.summaries = []
        self.card_notifies = []
        d.append_summary = lambda rid, line: self.summaries.append(line)
        d.post_dispatcher_event = lambda *a, **k: None
        d.notify_req_card = lambda event, req, **k: self.card_notifies.append(k)
        d._kickstart_board_if_own = lambda *a, **k: None
        d.has_code_changes = lambda *a, **k: True
        self.tmp = Path(tempfile.mkdtemp(prefix="req279-"))
        d.ARTIFACTS_ROOT = self.tmp

    def tearDown(self):
        _restore_d()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_path_dev_to_terminal_done(self):
        # auto 流程已删除：用等价三节点快照（dev → code_review → terminal）复现「开发→审查→收尾」全链路
        req = fake_req(mode="semi")
        req.update({"flow": "x", "node_index": 0, "nodes_json": json.dumps(
            [{"role": "dev", "node_type": "code"},
             {"role": "code_review", "node_type": "review", "artifact": "code_review.md"},
             {"node_type": "terminal"}], ensure_ascii=False)})
        # #419：set_req 写卡 + fetch_req 回读同一张卡（快照写后回读校验需要）
        d.set_req = lambda rid, fields: req.update(fields)
        d.fetch_req = lambda rid: req
        f = d.artifact_dir(req) / "result.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"status": "done", "summary": "S", "todos": ["合并分支并重启服务"]}),
                     encoding="utf-8")
        d.handle_result(req, "dev", 0, False, time.time())
        # #419：dev（code 节点）完成 → 快照推进到评审节点并拉起只读审查，未直接 done 通知
        self.assertEqual(req["node_index"], 1)
        self.assertEqual(self.card_notifies, [])
        # 评审 pass（done + verdict + code_review.md 产物）→ 推进 terminal → 内联 finalize_done → done 卡片带 todos
        (d.artifact_dir(req) / "code_review.md").write_text("审查通过", encoding="utf-8")
        f.write_text(json.dumps({"status": "done", "summary": "审查覆盖点", "verdict": "pass"}),
                     encoding="utf-8")
        d.handle_result(req, "code_review", 0, False, time.time())
        kw = self.card_notifies[0]
        self.assertEqual(kw["todos"], ["合并分支并重启服务"])
        self.assertTrue(any("待办：合并分支并重启服务" in s for s in self.summaries))
        self.assertNotIn("279", d.state.get("req_todos", {}))  # 已消费清理
        self.assertEqual(req["node_index"], 2)   # 已推进到 terminal（收尾内联执行）


class PromptTodosGuidanceTest(unittest.TestCase):
    """prompt 注入引导：dev/accept 角色渲染后含 todos 声明要求；COMMON_TAIL 契约说明同步
    （#291：示例不再诱导写合并，dev prompt 含调度器自动收口动作清单并禁止声明合并分支）"""

    def test_common_tail_mentions_todos_field(self):
        """#42 静态化：COMMON_TAIL 常量直接含 todos 契约说明（不再 format）"""
        rendered = d.COMMON_TAIL
        self.assertIn('"todos"', rendered)
        self.assertIn("遗留待办", rendered)
        self.assertIn("合并分支由调度器自动执行", rendered)
        self.assertNotIn("合并 f-xxx 分支并重启服务", rendered)

    def test_dev_auto_prompt_requires_todos(self):
        req = fake_req(mode="auto")
        wd = Path("/tmp/req279-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(req, "dev")
        self.assertIn("todos", prompt)
        self.assertIn("遗留待办", prompt)
        self.assertIn("仅展示不自动执行", prompt)
        # #291：调度器自动收口动作清单（区分本地合并/仅推分支项目）+ 禁止声明合并分支
        self.assertIn("调度器自动收口动作清单", prompt)
        self.assertIn("--no-ff 合并分支回 main", prompt)
        self.assertNotIn("基于已推送的 f-分支完成测试验证", prompt)  # #365：非仅推分支项目不注入该待办句式
        self.assertIn("禁止声明合并分支", prompt)

    def test_dev_semi_prompt_requires_todos(self):
        req = fake_req(mode="semi")
        wd = Path("/tmp/req279-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(req, "dev")
        self.assertIn("todos", prompt)
        self.assertIn("调度器自动收口动作清单", prompt)
        self.assertIn("禁止声明合并分支", prompt)

    def test_accept_prompt_mentions_todos(self):
        wd = Path("/tmp/req279-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"req-board": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(fake_req(), "accept")
        self.assertIn("todos", prompt)
        self.assertIn("覆盖 dev 声明的", prompt)
        self.assertNotIn("基于已推送的 f-分支完成测试验证", prompt)  # #365：非仅推分支项目不注入该待办句式
        # #433：accept 不声明 todos（声明会覆盖 dev 声明的，且拿不到 dev 待办）→ 待办归 dev 声明
        self.assertIn("不声明", prompt)
        self.assertIn("由 dev 声明", prompt)

    def test_accept_template_no_branch_only_sentence(self):
        """#365：accept 基线模板不含仅推分支项目待办句式（#363 教训）"""
        self.assertNotIn("基于已推送的 f-分支", d.ROLE_PROMPTS["accept"])

    def test_accept_prompt_injected_for_branch_only(self):
        """#365：仅推分支项目 accept 经 build_prompt 注入待办句式，不回归"""
        req = {"id": 365, "project": "p", "title": "T"}
        wd = Path("/tmp/req365-wd")
        with mock.patch.object(d, "PROJECTS", {"p": {"paths": ["/tmp/req365-wd"], "company": True}}), \
                mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"p": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            prompt = d.build_prompt(req, "accept")
        self.assertIn("仅推分支项目", prompt)
        self.assertIn("基于已推送的 f-分支完成测试验证", prompt)
        self.assertIn("todos", prompt)

    def test_agents_md_result_contract_mentions_todos(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("todos", text)
        self.assertIn("遗留待办", text)
        # #291：示例不再诱导写合并，明确合并由调度器自动执行
        self.assertIn("重启 xxx 服务并浏览器验收", text)
        self.assertIn("合并分支由调度器自动执行", text)
        self.assertNotIn("合并 f-xxx 分支并重启服务", text)


class PromptRestartInjectionTest(unittest.TestCase):
    """#318：dev prompt 注入「调度器 done 时按 restart 配方自动重启服务，todos 不要声明重启服务」——
    配置 restart 的项目命中（auto/semi 均覆盖）；未配置不注入；与 company 注入独立共存"""

    def _pj(self, restart="__KEEP__"):
        cfg = {"paths": ["/tmp/req318-wd"]}
        if restart == "__KEEP__":
            cfg["restart"] = {"cmd": "./go.sh", "verify": "http://127.0.0.1:9999"}
        elif restart is not None:
            cfg["restart"] = restart
        return mock.patch.object(d, "PROJECTS", {"p": cfg})

    def _build(self, mode="auto"):
        req = {"id": 318, "project": "p", "title": "T", "mode": mode}
        wd = Path("/tmp/req318-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"p": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value=f"工作目录：{wd}（独立 worktree）"):
            return d.build_prompt(req, "dev")

    def test_restart_configured_injected_auto(self):
        """验收：配置 restart 配方 → dev(auto) prompt 含「调度器自动重启」「不要声明重启服务」"""
        with self._pj():
            prompt = self._build()
        self.assertIn("调度器自动重启", prompt)
        self.assertIn("不要声明重启服务", prompt)
        self.assertIn("浏览器验收", prompt)

    def test_restart_configured_injected_semi(self):
        """semi 通道 dev 同样注入"""
        with self._pj():
            prompt = self._build(mode="semi")
        self.assertIn("调度器自动重启", prompt)
        self.assertIn("不要声明重启服务", prompt)

    def test_no_restart_not_injected(self):
        """未配置 restart → 不注入（行为不变）"""
        with self._pj(restart=None):
            prompt = self._build()
        self.assertNotIn("调度器自动重启", prompt)
        self.assertNotIn("不要声明重启服务", prompt)

    def test_company_and_restart_both_injected(self):
        """company + restart 同时命中 → 两条注入独立共存"""
        with mock.patch.object(d, "PROJECTS", {"p": {"paths": ["/tmp/req318-wd"], "company": True,
                                                     "restart": {"cmd": "go"}}}):
            prompt = self._build()
        self.assertIn("仅推分支项目", prompt)
        self.assertIn("基于已推送的 f-分支完成测试验证", prompt)  # #365：仅推分支项目 dev 保留待办句式
        self.assertIn("调度器自动重启", prompt)


if __name__ == "__main__":
    unittest.main()
