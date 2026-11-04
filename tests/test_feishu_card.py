#!/usr/bin/env python3
"""req-115 飞书关键通知升级为卡片样式：卡片构造/事件映射/转义截断/发送降级测试。

L0 单测（mock _send_feishu / notify / feishu 客户端）：
1. 事件映射：六类事件 → template 颜色、header 标题格式、wide_screen_mode
2. 卡片结构：elements 顺序 [fields div, 标题 div, 正文段落 div…, hr, 尾部 div]；fields 双栏 is_short lark_md
3. esc_md：& < > 转义；None/空值安全
4. truncate：超限截断加 …、边界不截断、先截断后转义
5. artifact_short：artifacts 根内相对短路径；越界退化 req-<id>
6. 成功路径：_send_feishu True → 不降级不重复发送
7. 失败降级：_send_feishu False → notify 收到与原文案一致的降级文本；log.error 含 resp.code/msg
8. 未连接（feishu None / chat_id 空）：仅日志不降级不抛错
9. notify 兼容：文本回执走 _send_feishu("text", ...)，签名不变

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试自动补一份。
"""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防测试误连生产 8899（#343）

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

# 字母序在前的测试文件可能 mock 后未恢复 d.notify / d.notify_req_card（#115 新增），
# 此处保存 import 时的真实引用，供测试强制使用，防跨文件污染
_REAL_NOTIFY = d.notify
_REAL_CARD_NOTIFY = d.notify_req_card


def fresh_state():
    return {"chat_id": "oc_req115_test", "retries": {}, "pending_confirm": {}, "pending_route": {},
            "pending_review": {}, "sessions": {}, "worktrees": {}}


def fake_req(rid=115, title="T"):
    return {"id": rid, "project": "req-board", "title": title}


def art_dir(req):
    """mock artifact_dir：落在 artifacts 根内（req-board/req-<id>），供短路径断言"""
    return d.ARTIFACTS_ROOT / req["project"] / f"req-{req['id']}"


class CardEventMappingTest(unittest.TestCase):
    """验收 1：六类事件 → template 颜色 / header 标题 / wide_screen_mode"""

    def setUp(self):
        self.orig_artifact_dir = d.artifact_dir
        d.artifact_dir = art_dir

    def tearDown(self):
        d.artifact_dir = self.orig_artifact_dir

    def test_template_and_title(self):
        expected = {
            "route": ("blue", "需求 #115 通道待确认"),
            "review": ("orange", "需求 #115 阶段待评审"),
            "need_confirm": ("orange", "需求 #115 需要确认"),
            "done": ("green", "需求 #115 开发完成"),
            "failed": ("red", "需求 #115 自动开发失败"),
            "merge_conflict": ("red", "需求 #115 自动合并冲突"),
        }
        for event, (template, title) in expected.items():
            card = d.build_req_card(event, fake_req())
            self.assertEqual(card["header"]["template"], template, event)
            self.assertEqual(card["header"]["title"], {"tag": "plain_text", "content": title}, event)
            self.assertEqual(card["config"]["wide_screen_mode"], True, event)


class CardStructureTest(unittest.TestCase):
    """验收 2：统一结构 [fields 双栏, 标题, 正文段落…, hr, 尾部]；字段名与取值正确"""

    def setUp(self):
        self.orig_artifact_dir = d.artifact_dir
        d.artifact_dir = art_dir

    def tearDown(self):
        d.artifact_dir = self.orig_artifact_dir

    def _fields_of(self, card):
        return card["elements"][0]["fields"]

    def _body_of(self, card):
        return "\n".join(e["text"]["content"] for e in card["elements"][1:-2])

    def _footer_of(self, card):
        return card["elements"][-1]["text"]["content"]

    def test_element_order_and_field_shape(self):
        cases = {
            "route": {"suggested": "auto"},
            "review": {"phase": "tech"},
            "need_confirm": {"question": "Q"},
            "done": {"summary": "S"},
            "failed": {"reason": "R"},
            "merge_conflict": {},
        }
        for event, kw in cases.items():
            card = d.build_req_card(event, fake_req(), **kw)
            tags = [e.get("tag") for e in card["elements"]]
            self.assertEqual(tags, ["div", "div", "div", "hr", "div"], event)
            for f in self._fields_of(card):
                self.assertTrue(f["is_short"], event)
                self.assertEqual(f["text"]["tag"], "lark_md", event)

    def test_route_fields_body_footer(self):
        card = d.build_req_card("route", fake_req(title="标题 <A>"), suggested="auto", artifact="analysis.md")
        self.assertEqual([f["text"]["content"] for f in self._fields_of(card)],
                         ["**项目**\nreq-board", "**建议通道**\n自动"])
        self.assertIn("**标题 &lt;A&gt;**", self._body_of(card))
        self.assertIn("证据清单：req-board/req-115/analysis.md", self._body_of(card))
        self.assertEqual(self._footer_of(card), "回复 y 115 确认，n 115 <原因> 否决。")

    def test_review_fields_body_footer(self):
        card = d.build_req_card("review", fake_req(), phase="tech", artifact_dir=str(art_dir(fake_req())))
        self.assertEqual([f["text"]["content"] for f in self._fields_of(card)],
                         ["**项目**\nreq-board", f"**待评审阶段**\n{d.ROLE_NAMES['tech']}"])
        self.assertIn("产物目录：req-board/req-115", self._body_of(card))
        self.assertEqual(self._footer_of(card), "回复 y 115 通过，n 115 <原因> 否决。")

    def test_need_confirm_fields_body_footer(self):
        card = d.build_req_card("need_confirm", fake_req(), question="方案 A 还是 B？", phase="dev")
        self.assertEqual([f["text"]["content"] for f in self._fields_of(card)],
                         ["**项目**\nreq-board", f"**当前阶段**\n{d.ROLE_NAMES['dev']}"])
        self.assertIn("方案 A 还是 B？", self._body_of(card))
        self.assertEqual(self._footer_of(card), "回复 y 115 同意，n 115 <原因> 否决。")

    def test_done_fields_body_footer(self):
        card = d.build_req_card("done", fake_req(), summary="完成总结 S", company=False,
                                reminder="\n⚠️ 改动涉及 dispatcher.py，已合并；需手动重启调度器生效（kill <pid>）")
        self.assertEqual([f["text"]["content"] for f in self._fields_of(card)],
                         ["**项目**\nreq-board", "**合并方式**\n开发分支已合并回 main"])
        self.assertIn("完成总结 S", self._body_of(card))
        self.assertIn("产物：req-board/req-115/result.json", self._footer_of(card))
        self.assertIn("⚠️ 改动涉及 dispatcher.py", self._footer_of(card))
        # #266：需手动重启的提醒用红字突出，且仍在「产物」行下方
        footer = self._footer_of(card)
        self.assertIn("<font color='red'>", footer)
        self.assertLess(footer.index("产物："), footer.index("需手动重启调度器"))
        # 仅推分支项目：合并方式文案不同，且带 reminder 时正文不重复 ⚠️
        card2 = d.build_req_card("done", fake_req(), summary="", company=True, reminder="")
        self.assertIn("**合并方式**\n开发分支已自动 push 到远端（待发版合并）",
                      [f["text"]["content"] for f in self._fields_of(card2)][1])
        self.assertIn("需求已完成", self._body_of(card2))
        self.assertNotIn("⚠️", self._footer_of(card2))

    def test_done_summary_split_into_paragraphs(self):
        """多段 summary：标题与每段各占独立 div，`- ` 列表标记转 `• `，不再挤成一行"""
        card = d.build_req_card(
            "done",
            fake_req(title="调度器与 board 拉起 ACP 会话时默认固定 thinking effort 为 max"),
            summary="- 用户决策（需求 #116 summary）：保持默认模型 deepseek/v4-flash\n"
                    "- 实现（commit 920023b）：acp_shared.py 新增 ACP_THINKING 常量\n"
                    "1. 复测（真实 kimi 会话，kimi 0.34.0）：会话推理深度已固定 max",
            company=False, reminder="")
        body_divs = [e for e in card["elements"][1:-2] if "text" in e]
        self.assertEqual(len(body_divs), 4)  # 标题 + 3 个正文段落
        contents = [e["text"]["content"] for e in body_divs]
        self.assertIn("**调度器与 board 拉起 ACP 会话时默认固定 thinking effort 为 max**", contents[0])
        self.assertIn("• 用户决策（需求 #116 summary）：", contents[1])
        self.assertNotIn("- 用户决策", contents[1])
        self.assertIn("• 实现（commit 920023b）：", contents[2])
        self.assertIn("1. 复测（真实 kimi 会话，kimi 0.34.0）：", contents[3])

    def test_card_paragraphs_skip_blank_and_truncate(self):
        """段落拆分：空行跳过、无序标记转换、编号保留；先截断再拆"""
        self.assertEqual(d._card_paragraphs("- 一\n\n- 二\n\n3. 三", 500),
                         ["• 一", "• 二", "3. 三"])
        self.assertEqual(d._card_paragraphs(None, 500), [])
        long = "x" * 501
        paras = d._card_paragraphs(long, 500)
        self.assertEqual(len(paras), 1)
        self.assertTrue(paras[0].endswith("…"))

    def test_failed_fields_body_footer(self):
        card = d.build_req_card("failed", fake_req(), retries=3, reason="环境依赖装不上")
        self.assertEqual([f["text"]["content"] for f in self._fields_of(card)],
                         ["**项目**\nreq-board", "**失败次数**\n3 次"])
        self.assertIn("环境依赖装不上", self._body_of(card))
        self.assertEqual(self._footer_of(card), "请人工处理后把状态改回 open 恢复调度。")

    def test_merge_conflict_fields_body_footer(self):
        card = d.build_req_card("merge_conflict", fake_req(), failed=["repo-a", "repo-b"], branch="f-20260807-115")
        self.assertEqual([f["text"]["content"] for f in self._fields_of(card)],
                         ["**项目**\nreq-board", "**冲突仓库**\nrepo-a, repo-b"])
        self.assertIn("已保留分支 f-20260807-115 与 worktree", self._body_of(card))
        self.assertEqual(self._footer_of(card), "请解决后回复 y 115 重试，或 n 115 人工接管。")


class EscMdTest(unittest.TestCase):
    """验收 3：lark_md 特殊字符转义"""

    def test_escapes_special_chars(self):
        self.assertEqual(d.esc_md("a & b"), "a &amp; b")
        self.assertEqual(d.esc_md("<tag>"), "&lt;tag&gt;")
        self.assertEqual(d.esc_md("混合 <a> & 'x' > b"), "混合 &lt;a&gt; &amp; 'x' &gt; b")

    def test_plain_and_none_safe(self):
        self.assertEqual(d.esc_md("普通文本"), "普通文本")
        self.assertEqual(d.esc_md(""), "")
        self.assertEqual(d.esc_md(None), "")
        self.assertEqual(d.esc_md(123), "123")


class TruncateTest(unittest.TestCase):
    """验收 4：超长截断加 …、边界不截断、先截断后转义"""

    def test_limits(self):
        self.assertEqual(d.truncate("x" * 80, 80), "x" * 80)           # 边界不截断
        self.assertEqual(d.truncate("x" * 79, 80), "x" * 79)
        s = d.truncate("x" * 81, 80)
        self.assertEqual(s, "x" * 80 + "…")
        self.assertEqual(len(s), 81)

    def test_body_limit(self):
        s = d.truncate("长" * 501, 500)
        self.assertEqual(s, "长" * 500 + "…")

    def test_none_safe(self):
        self.assertEqual(d.truncate(None, 10), "")

    def test_truncate_before_escape(self):
        """先截断后转义：标题 100 个 < 先截为 80 再转义，转义后长度 80*4+1 不超字段上限"""
        title = "<" * 100
        card = d.build_req_card("route", fake_req(title=title), suggested="auto", artifact="analysis.md")
        body = card["elements"][1]["text"]["content"]
        head = body.split("**", 2)[1]
        self.assertEqual(head, "&lt;" * 80 + "…")


class ArtifactShortTest(unittest.TestCase):
    """验收 5：路径降级——artifacts 根内短路径、不含绝对路径前缀"""

    def test_relative_short_path(self):
        with mock.patch.object(d, "artifact_dir", art_dir):
            self.assertEqual(d.artifact_short(fake_req()), "req-board/req-115")
            self.assertEqual(d.artifact_short(fake_req(), "analysis.md"), "req-board/req-115/analysis.md")
            self.assertNotIn(str(d.ARTIFACTS_ROOT), d.artifact_short(fake_req(), "result.json"))

    def test_out_of_root_fallback(self):
        """产物目录越界（如测试临时目录）→ 退化 req-<id>，不抛错"""
        with mock.patch.object(d, "artifact_dir", lambda req: Path("/tmp/nonexistent/req-x")):
            self.assertEqual(d.artifact_short(fake_req(), "analysis.md"), "req-115/analysis.md")


class _SendBase(unittest.TestCase):
    """公共环境：state 带 chat_id、feishu 用 fake 客户端、notify/_send_feishu 记录"""

    def setUp(self):
        d.state = fresh_state()
        d.notify_req_card = _REAL_CARD_NOTIFY  # 强制恢复真实函数（防前序文件 mock 残留污染）
        self.orig_artifact_dir = d.artifact_dir
        d.artifact_dir = art_dir
        self.orig_feishu = d.feishu
        self.orig_send = d._send_feishu
        self.orig_notify = d.notify
        self.sent = []        # _send_feishu 记录 (msg_type, content_obj)
        self.notifies = []    # notify 记录
        d.notify = lambda text, *a, **k: self.notifies.append(text)

    def tearDown(self):
        d.artifact_dir = self.orig_artifact_dir
        d.feishu = self.orig_feishu
        d._send_feishu = self.orig_send
        d.notify = self.orig_notify
        d.notify_req_card = _REAL_CARD_NOTIFY

    def _fake_client(self, success=True):
        """fake lark 客户端：message.create 返回可判 success 的 resp"""
        client = mock.Mock()
        resp = types.SimpleNamespace(success=lambda: success, code=999 if not success else 0,
                                     msg="boom" if not success else "")
        client.im.v1.message.create.return_value = resp
        return client


class SendSuccessTest(_SendBase):
    """验收 6：发送成功 → 不降级不重复发送；interactive + JSON 序列化卡片"""

    def test_success_no_fallback(self):
        d._send_feishu = lambda msg_type, content_obj: self.sent.append((msg_type, content_obj)) or True
        d.notify_req_card("done", fake_req(), summary="S", company=False, reminder="")
        self.assertEqual(self.notifies, [])
        self.assertEqual(len(self.sent), 1)
        msg_type, card = self.sent[0]
        self.assertEqual(msg_type, "interactive")
        parsed = json.loads(json.dumps(card, ensure_ascii=False))  # content 可 JSON 序列化
        self.assertEqual(parsed["header"]["template"], "green")

    def test_notify_text_path(self):
        """notify 兼容：文本回执走 _send_feishu("text", ...)，签名不变"""
        d._send_feishu = lambda msg_type, content_obj: self.sent.append((msg_type, content_obj)) or True
        _REAL_NOTIFY("需求 #90 已在网页端确认走自动通道。")
        self.assertEqual(self.sent, [("text", {"text": "需求 #90 已在网页端确认走自动通道。"})])


class SendFailFallbackTest(_SendBase):
    """验收 7：已连接发送失败 → 降级 notify 原文案文本；log.error 可定位（含 resp.code/msg）"""

    def test_real_send_failure_logs_error(self):
        """真实 _send_feishu：resp 失败 → log.error 含 code/msg，返回 False"""
        d.feishu = self._fake_client(success=False)
        with mock.patch.object(d.log, "error") as err:
            ok = d._send_feishu("interactive", {"x": 1})
        self.assertFalse(ok)
        self.assertTrue(any("999" in str(c) and "boom" in str(c) for c in err.call_args_list))

    def test_fallback_text_verbatim(self):
        """六类降级文本与升级前 notify 文案逐字一致"""
        req = fake_req(title="需求标题")
        rid = "115"
        cases = [
            ("route", dict(suggested="auto", artifact="analysis.md"),
             f"🧭 需求 #{rid}「需求标题」{d.ROLE_NAMES['analysis']} 建议通道：auto\n"
             f"证据清单：{art_dir(req) / 'analysis.md'}\n\n回复 y {rid} 确认，n {rid} <原因> 否决。"),
            ("review", dict(phase="tech", artifact_dir=str(art_dir(req))),
             f"📋 需求 #{rid}「需求标题」{d.ROLE_NAMES['tech']} 阶段产物待评审：\n"
             f"{art_dir(req)}\n\n回复 y {rid} 通过，n {rid} <原因> 否决。"),
            ("need_confirm", dict(question="方案 A 还是 B？", phase="dev"),
             f"❓ 需求 #{rid}「需求标题」需要确认：\n方案 A 还是 B？\n\n回复 y {rid} 同意，n {rid} <原因> 否决。"),
            ("done", dict(summary="S", company=False, reminder=""),
             f"✅ 需求 #{rid} 已完成：需求标题"),
            ("done", dict(summary="S", company=True, reminder="\n⚠️ 重启提醒"),
             f"✅ 需求 #{rid} 已完成：需求标题（开发分支已自动 push 到远端，待发版合并）\n⚠️ 重启提醒"),
            ("failed", dict(retries=3, reason="环境依赖装不上"),
             f"❌ 需求 #{rid}「需求标题」自动开发失败 3 次，已停止自动调度"
             f"（卡片转 later，等待人工重新开放）。\n最后原因：环境依赖装不上\n请人工处理后把状态改回 open 恢复调度。"),
            ("merge_conflict", dict(failed=["repo-a"], branch="f-20260807-115"),
             f"⚠️ 需求 #{rid}「需求标题」自动合并冲突（repo-a）。\n"
             f"已保留分支 f-20260807-115 与 worktree，请解决后回复 y {rid} 重试，或 n {rid} 人工接管。"),
        ]
        for event, kw, expected in cases:
            self.assertEqual(d.fallback_text(event, req, **kw), expected, event)

    def test_failure_falls_back_to_notify(self):
        d.feishu = self._fake_client()  # 已连接（发送被 mock 为失败）→ 触发降级
        d._send_feishu = lambda msg_type, content_obj: False
        d.notify_req_card("failed", fake_req(), retries=3, reason="R")
        self.assertEqual(len(self.notifies), 1)
        self.assertIn("自动开发失败 3 次", self.notifies[0])
        self.assertIn("最后原因：R", self.notifies[0])


class NotConnectedTest(_SendBase):
    """验收 8：未连接（feishu None / chat_id 空）→ 仅日志，不降级不抛错"""

    def test_feishu_none_no_fallback(self):
        d.feishu = None
        d.state = fresh_state()
        d.state["chat_id"] = "oc_x"
        d._send_feishu = lambda msg_type, content_obj: False
        with mock.patch.object(d.log, "info"):
            d.notify_req_card("route", fake_req(), suggested="auto", artifact="analysis.md")
        self.assertEqual(self.notifies, [])

    def test_chat_id_empty_no_fallback(self):
        d.feishu = self._fake_client()
        d.state["chat_id"] = ""
        d._send_feishu = lambda msg_type, content_obj: False
        d.notify_req_card("review", fake_req(), phase="tech", artifact_dir=str(art_dir(fake_req())))
        self.assertEqual(self.notifies, [])

    def test_real_send_not_connected_logs_only(self):
        """真实 _send_feishu：feishu None → 仅 log.info 记录，返回 False，不抛错"""
        d.feishu = None
        d.state["chat_id"] = "oc_x"
        with mock.patch.object(d.log, "info") as info:
            ok = d._send_feishu("interactive", {"x": 1})
        self.assertFalse(ok)
        self.assertTrue(any("仅记录" in str(c) for c in info.call_args_list))


if __name__ == "__main__":
    unittest.main()
