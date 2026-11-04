#!/usr/bin/env python3
"""#30 需求卡清单卡片（dispatcher 侧）：页 spec → 飞书 interactive 卡片渲染、发送与完整文本降级。

L0 单测（mock `_send_feishu` / `feishu` 客户端，不真发飞书）：
1. 每张需求一个独立 div；elements = [信息 div, 每卡 1 div, 页脚 div]
2. 卡片字段：编号/标题/中文状态/流程/更新时间；标题先截断后转义
3. header 含项目名与「第 p/N 页」；wide_screen_mode
4. truncated → 页脚红字含 remaining
5. items 缺失/非列表 → 不抛（页脚「（无条目）」）
6. 发送成功 → interactive 一次、不重复发文本；失败且已连接 → 该页**完整**文本降级
7. 未连接 → 仅日志、不伪造成功
8. 显式 chat_id（不用 state["chat_id"] y/n 通道）
9. send_text_lossless 全量分片（> 5 片），不改 send_text_chunked 语义

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m unittest tests.test_feishu_card_list -v
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

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")  # 缺省不可达，防误连生产

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402


def payload(n=2, **kw):
    items = [{"id": 30 + i, "title": f"需求标题 {i}", "status": "doing",
              "status_label": "进行中", "flow": "复杂", "updated_at": "2026-10-13 12:00:00"}
             for i in range(n)]
    base = {"v": 1, "type": "requirement_list", "project": "loop-engine", "page": 1, "pages": 1,
            "total_cards": n, "truncated": False, "remaining": 0, "items": items}
    base.update(kw)
    return base


class RenderTest(unittest.TestCase):
    """纯函数渲染：结构 / 字段 / 转义 / 页脚。"""

    def test_d1_each_requirement_one_block(self):
        card = d.build_req_list_card(payload(3))
        divs = [e for e in card["elements"] if e["tag"] == "div"]
        self.assertEqual(len(divs), 3 + 2)   # 信息 div + 每卡 1 div + 页脚 div

    def test_d2_fields_present_and_labeled(self):
        card = d.build_req_list_card(payload(1))
        body = json.dumps(card, ensure_ascii=False)
        for must in ("#30", "需求标题 0", "状态：进行中", "流程：复杂", "更新：2026-10-13 12:00:00"):
            self.assertIn(must, body)
        self.assertNotIn("doing", body)      # 只展示中文状态，不泄露英文码

    def test_d3_escaping_and_truncate_order(self):
        card = d.build_req_list_card(payload(1, items=[{
            "id": 1, "title": "<tag> & stuff", "status": "open", "status_label": "待开发",
            "flow": "标准", "updated_at": "t"}]))
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("&lt;tag&gt; &amp; stuff", body)
        self.assertNotIn("<tag>", body)
        # 超长标题：先 truncate 后 esc_md → 转义后的标题无裸 '<'
        long_card = d.build_req_list_card(payload(1, items=[{
            "id": 2, "title": "<" * 200, "status": "open", "status_label": "待开发",
            "flow": "标准", "updated_at": "t"}]))
        self.assertNotIn("<", json.dumps(long_card["elements"][1], ensure_ascii=False))

    def test_d4_header_project_and_page(self):
        card = d.build_req_list_card(payload(2, page=2, pages=3))
        self.assertEqual(card["config"], {"wide_screen_mode": True})
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["header"]["title"]["tag"], "plain_text")
        self.assertIn("loop-engine", card["header"]["title"]["content"])
        self.assertIn("第 2/3 页", card["header"]["title"]["content"])

    def test_d5_truncated_footer_warning(self):
        card = d.build_req_list_card(payload(1, truncated=True, remaining=7))
        footer = card["elements"][-1]["text"]["content"]
        self.assertIn("还有 7 张未展示", footer)
        self.assertIn("<font color='red'>", footer)

    def test_d6_malformed_items_fail_soft(self):
        for items in (None, "oops", dict(a=1)):
            card = d.build_req_list_card(payload(0, items=items))
            self.assertEqual(card["elements"][-1]["text"]["content"], "（无条目）")


class _SendBase(unittest.TestCase):
    """公共环境：state 带 y/n chat_id；mock _send_feishu / feishu，记录发送。"""

    def setUp(self):
        self.orig_state = d.state
        self.orig_feishu = d.feishu
        self.orig_send = d._send_feishu
        self.orig_lossless = d.send_text_lossless
        d.state = {"chat_id": "oc_yn_channel"}
        self.sent = []          # (msg_type, content_obj, chat_id)
        self.lossless = []      # (chat_id, text)

    def tearDown(self):
        d.state = self.orig_state
        d.feishu = self.orig_feishu
        d._send_feishu = self.orig_send
        d.send_text_lossless = self.orig_lossless

    def _fake_send(self, ok=True):
        def _send(msg_type, content_obj, chat_id=None):
            self.sent.append((msg_type, content_obj, chat_id))
            return ok
        d._send_feishu = _send


class SendPathTest(_SendBase):
    """发送 / 降级 / 未连接 / 显式 chat_id。"""

    def test_d7_send_success_no_text_duplicate(self):
        d.feishu = mock.Mock()
        self._fake_send(ok=True)
        d.send_text_lossless = lambda cid, text: self.lossless.append((cid, text))
        ok = d._feishu_send_assistant_card("oc_assistant", json.dumps(payload(2)))
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "interactive")
        self.assertEqual(self.lossless, [])          # 成功不发文本

    def test_d8_send_failure_falls_back_complete_text(self):
        d.feishu = mock.Mock()
        self._fake_send(ok=False)                         # 卡片与降级文本都记入 self.sent（真实 send_text_lossless）
        p = payload(3)
        ok = d._feishu_send_assistant_card("oc_assistant", json.dumps(p, ensure_ascii=False))
        self.assertFalse(ok)
        kinds = [mt for (mt, _o, _c) in self.sent]
        self.assertEqual(kinds, ["interactive", "text"])   # 先试卡片，失败降级文本
        texts = "".join(o.get("text", "") for (mt, o, _c) in self.sent if mt == "text")
        for it in p["items"]:
            self.assertIn(it["title"], texts)             # 逐条覆盖，无内容丢失
        self.assertIn("需求卡清单 · loop-engine", texts)

    def test_d9_not_connected_logs_only(self):
        d.feishu = None                                   # 走真实 _send_feishu（未连接 → 仅 log.info 返回 False）
        d.send_text_lossless = lambda cid, text: self.lossless.append((cid, text))
        ok = d._feishu_send_assistant_card("oc_assistant", json.dumps(payload(1)))
        self.assertFalse(ok)                              # 不伪造成功
        self.assertEqual(self.lossless, [])               # 未连接不发任何消息

    def test_d10_explicit_chat_id_not_state_channel(self):
        d.feishu = mock.Mock()
        self._fake_send(ok=True)
        d._feishu_send_assistant_card("oc_assistant", json.dumps(payload(1)))
        self.assertEqual(self.sent[0][2], "oc_assistant")   # 显式 chat_id
        self.assertNotEqual(self.sent[0][2], d.state["chat_id"])

    def test_d11_lossless_text_beyond_5_chunks(self):
        self._fake_send(ok=True)
        sent, failed = d.send_text_lossless("oc_assistant", "x" * (d.FEISHU_TEXT_CHUNK_SIZE * 6))
        self.assertEqual(failed, 0)
        self.assertEqual(sent, 6)                            # 6 片全发，未走 send_text_chunked 的 5 片上限
        self.assertEqual(len(self.sent), 6)
        self.assertTrue(all(mt == "text" and cid == "oc_assistant" for mt, _o, cid in self.sent))


if __name__ == "__main__":
    unittest.main()
