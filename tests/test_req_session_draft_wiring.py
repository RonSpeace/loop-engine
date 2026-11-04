#!/usr/bin/env python3
"""req-431 前端接线锁定（从 static/board.js 提取函数源码做静态断言）。

覆盖：新建聊天零 fetch/零落库、首条消息走新端点、中间栏先过滤再分组、草稿 localStorage
三件套与刷新恢复、互斥与重绘保护（repaintForAgentSessions 草稿分支）、角色取快照、
负向锁「明确不做」（无会话删除/接管 UI 入口、中间栏不显示来源标记；#5 起项目移除接入
为唯一放行的新增 DELETE 入口，用例 25 的 DELETE 计数断言同步收窄到会话面）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_req_session_draft_wiring -v
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests._frontend_src import frontend_js  # noqa: E402
from tests.test_process_markdown_merge import _extract_function  # noqa: E402


def _script_block():
    """#33：主脚本已外置 static/board.js，直接读该文件（模板里只剩 skinBoot/配置脚本/外链）。"""
    return frontend_js()


class DraftFrontendWiringTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = _script_block()

    def _fn(self, name):
        return _extract_function(self.js, name)

    # ---- 用例 21：新建聊天零 fetch、旧端点零引用 ----
    def test_new_chat_no_db_call(self):
        fn = self._fn("newAgentSession")
        self.assertNotIn("fetch(", fn, "newAgentSession 不得发请求（草稿态纯前端）")
        self.assertIn("agentDraft = {reqId: id}", fn)
        self.assertIn("render()", fn)
        # 负向：旧 URL 形态（以反引号收尾的 `/sessions`）全页零引用（新端点中间是 /messages 不命中）
        self.assertIsNone(re.search(r"/\/sessions`/", self.js),
                          "仍存在旧空会话端点 URL 字面量 `/sessions`（反引号收尾）")
        self.assertNotIn("'/sessions'", self.js)

    # ---- 用例 22：发送首条消息走新端点 ----
    def test_send_first_message_uses_new_endpoint(self):
        fn = self._fn("sendAgentChat")
        for needle in ("/sessions/messages", "X-Actor", "manual:", "clearAgentDraft(",
                       "selectedAgentSession = data.id", "pollAgentSessionUntilIdle(",
                       "await refreshAgentSessions()"):
            self.assertIn(needle, fn, "sendAgentChat 缺少 %s" % needle)

    # ---- 用例 23：中间栏先过滤再分组 ----
    def test_middle_column_filters_before_grouping(self):
        flt = self._fn("visibleSessionRows")
        self.assertIn("filter(", flt)
        self.assertIn("r.s.id", flt)
        footer = self._fn("sessionFooterHtml")
        self.assertIn("visibleSessionRows(sessionList(i))", footer)
        self.assertLess(footer.index("visibleSessionRows(sessionList(i))"),
                        footer.index("mainIdx"), "过滤必须发生在分组（mainIdx）之前")
        self.assertNotIn("filter(", self._fn("sessionList"), "sessionList 语义不变（不在取数层过滤）")
        self.assertIn("sessionList(i)", self._fn("findSessionRow"))

    # ---- 用例 24：草稿 localStorage 三件套 + 刷新恢复 ----
    def test_draft_localstorage_and_restore(self):
        self.assertIn("const AGENT_DRAFT_KEY = 'reqBoardAgentDrafts';", self.js)
        for name in ("getAgentDrafts", "saveAgentDraft", "clearAgentDraft", "restoreAgentDraft"):
            self.assertIn("function %s(" % name, self.js)
        save = self._fn("saveAgentDraft")
        self.assertIn("delete drafts[String(reqId)]", save, "空文本必须删键（刷新不再进草稿态）")
        restore = self._fn("restoreAgentDraft")
        self.assertIn("addEventListener('input'", restore)
        self.assertIn("input.value = agentDraftText(reqId)", restore)
        # 初始化预置：有非空草稿才进草稿态
        self.assertIn("if (selectedId === draftReqId && agentDraftText(draftReqId)) agentDraft = {reqId: draftReqId};",
                      self.js)

    # ---- 用例 25：负向锁「明确不做」 ----
    def test_no_new_ui_entries(self):
        self.assertNotIn("origin", self._fn("sessionFooterHtml"), "中间栏不得新增来源标记")
        self.assertNotIn("origin", self._fn("sessionList"))
        # 页面 DELETE 入口仅两处：需求删除（既有）+ #5 项目移除接入；仍禁止任何会话删除 UI
        deletes = [m.start() for m in re.finditer(r"method: 'DELETE'", self.js)]
        self.assertEqual(len(deletes), 2,
                         "页面 DELETE 入口数量变化（#5 起项目移除接入 /api/projects/<name> 为唯一新增）")
        idx = self.js.index("fetch(`/api/requirements/${id}`")
        self.assertIn("{method: 'DELETE'}", self.js[idx:idx + 80])
        self.assertNotIn("/sessions/", self.js[idx:idx + 80])
        proj = self._fn("confirmRemoveProject")
        self.assertIn("/api/projects/", proj)
        self.assertNotIn("/sessions/", proj, "项目移除接入不得触碰会话端点")
        for word in ("删除会话", "移除会话", "关闭草稿", "delSession"):
            self.assertNotIn(word, self.js)

    # ---- 用例 26：草稿角色取卡上快照 ----
    def test_draft_role_from_snapshot(self):
        fn = self._fn("currentNodeRole")
        self.assertIn("i.nodes_json", fn)
        self.assertIn("i.node_index", fn)
        self.assertIn("n.role", fn)
        self.assertIn("PRE_ROLE = d.pre_role || ''", self.js)
        self.assertIn("role: currentNodeRole(i) || PRE_ROLE", self._fn("agentDraftRow"))
        # 前端不传 role（服务端裁决）
        self.assertNotIn("role:", self._fn("sendAgentChat").split("/sessions/messages")[1][:400])

    # ---- 用例 27：互斥 + 重绘保护 ----
    def test_draft_view_mutual_exclusion_and_repaint_guard(self):
        detail = self._fn("renderDetailPane")
        self.assertIn("agentDraft && agentDraft.reqId === item.id", detail)
        for guard in ("!selectedSid", "!selectedDispatcher", "!selectedArtifact", "!selectedReqChat"):
            self.assertIn(guard, detail, "草稿分支守卫缺少 %s" % guard)
        self.assertIn("item.status !== 'done'", detail)
        # 草稿态复用共享状态卡（row=null → P6 档，可输入 + 「新建聊天」动作位）
        self.assertIn("renderAgentChatStatus(null, agentChatBusy)", detail)
        self.assertIn("agentSessionHtml(item, agentDraftRow(item))", detail)
        for name in ("selectReq", "selectSession", "selectAgentSession", "selectDispatcher",
                     "selectArtifact", "selectReqChat"):
            self.assertIn("agentDraft = null;", self._fn(name), "%s 未清草稿态" % name)
        auto = self._fn("ensureAutoSelect")
        self.assertIn("agentDraft = null", auto)
        new_fn = self._fn("newAgentSession")
        self.assertIn("agentChatBusy = false", new_fn)
        self.assertIn("clearInterval(agentChatTimer)", new_fn)
        # 重绘保护：草稿分支必须在最终 else（兜底 render()）之前，且只 renderList()
        repaint = self._fn("repaintForAgentSessions")
        draft_idx = repaint.index("agentDraft && agentDraft.reqId === selectedId")
        else_idx = repaint.rindex("} else {")   # 最终兜底分支
        self.assertLess(draft_idx, else_idx, "草稿分支必须落在兜底 render() 之前")
        self.assertIn("renderList();", repaint[draft_idx:else_idx])
        self.assertNotIn("render();", repaint[draft_idx:else_idx])


if __name__ == "__main__":
    unittest.main()
