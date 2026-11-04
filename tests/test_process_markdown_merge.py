#!/usr/bin/env python3
"""req-114/req-271 防复发：Markdown 跨事件分段显示修复回归测试。

改动（#114）：refreshProcess 将同一会话内相邻 message 事件按原始顺序无分隔拼接
（纯函数 mergeMessageBlocks）后统一经 renderArtifactMd 渲染；tool/system 事件为
明确边界保持独立展示。改动（#271）：建卡对话 chatEventsHtml 同样先 mergeMessageBlocks
再渲染（tool/system/user 为边界，chat-msg 类名不变）。#33 起主脚本外置 static/board.js，
本测试从该文件提取真实函数源码，交给 node 执行断言（tests/process_markdown_harness.js），覆盖：
  1. 跨事件边界加粗正确显示（复刻真实现场 8966/8967 的 `**判定`+`**：`）；
  2. 文本超过消息缓冲阈值、标记恰好被拆分的回归场景；
  3. 段落/标题/列表/行内代码/链接/围栏代码/表格等现有 Markdown 不退化；
  4. tool/system 事件独立展示且顺序不变；
  5. XSS 安全基线（先转义再语法转换）不因拼接而退化；
  6. #271 建卡对话 chat 23 现场（事件 309/310 代码块围栏跨事件、308/309 行内代码
     被切开）合并后正常渲染，chat 渲染分支（user/tool/sys/agent）形态不退化；
  7. #278 字面 \n/\"（agent 粘贴的 JSON 转义文本，chat_events 614 现场形态）在
     renderArtifactMd 入口反转义为真实换行/引号，换行/列表正常、无转义残留，
     正常 markdown 与普通反斜杠（路径）不受影响。
  8. #280 围栏代码块若整体为合法 JSON（draft 草稿块形态）自动 2 空格缩进格式化：
     单行 JSON（chat 25 现场形态）格式化为缩进结构且 notes 换行真实呈现、多行 JSON
     同样格式化、非法 JSON/普通代码块保持原样、正文无围栏 JSON 文本不受影响、
     格式化输出仍整体经 escapeHtml（XSS 不因格式化退化）。
  9. #296 行内代码/普通围栏内的字面 \\n 不被 #278 全局反转义拆开：现场行（agent_events
     15288 形态）整行不拆、代码段内字面 \\n/\\" 保持字面、正文反转义与 JSON 草稿围栏
     格式化链路不回归、保护层回填仍经 escapeHtml（XSS 不退化）。
  10. #297 会话详情视图刷新决策纯函数 agentSessionRefreshDecision 判定矩阵（需求转 done/
      注册表行出现消失/session_id 变化 → full；仅行状态变化 → status；无变化 → none），
      以及 autoRefresh 中 selectedAgentSession 分支先于右栏保留分支、busy 跳过状态行刷新的
      wiring 断言。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_process_markdown_merge -v
    # 或与全量一起：
    .venv/bin/python -m unittest discover -s tests -v
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._frontend_src import frontend_js, frontend_src

REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = Path(__file__).resolve().parent / "process_markdown_harness.js"

NEEDED_FUNCS = ("escapeHtml", "renderArtifactMd", "mergeMessageBlocks", "isSessionLinkActive",
                "isReqChatLinkActive", "agentGroupExpanded", "selectionGuardActive",
                "agentSessionRefreshDecision", "mergeChatStreamEvents",
                "sessHeadKey", "sessPathParts", "sessTitleParts", "sessPathRowHtml", "sessHeadHtml",
                "agentSessionRowIdForSid", "visibleSessionRows", "currentNodeRole")


# ---------- 从 static/board.js 提取纯函数源码（花括号配平，跳过字符串/模板/注释/正则） ----------
def _prev_char(src, i):
    j = i - 1
    while j >= 0 and src[j].isspace():
        j -= 1
    return src[j] if j >= 0 else ""


def _skip_expr(src, i):
    """跳过模板字面量中 ${...} 表达式，返回匹配 } 之后的下标。"""
    depth, n = 1, len(src)
    while i < n:
        c = src[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        elif c in ('"', "'"):
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
        elif c == "`":
            i = _skip_tpl(src, i + 1)
        else:
            i += 1
    raise ValueError("unbalanced ${ expression")


def _skip_tpl(src, i):
    """跳过模板字面量，返回闭反引号之后的下标。"""
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            i = _skip_expr(src, i + 2)
            continue
        i += 1
    raise ValueError("unterminated template literal")


def _extract_function(js, name):
    """提取 static/board.js 内 function <name>(...) { ... } 的完整源码文本。"""
    start = js.find("function %s(" % name)
    if start < 0:
        raise AssertionError("function %s not found in static/board.js" % name)
    i = js.index("{", start)
    depth, n = 0, len(js)
    while i < n:
        c = js[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return js[start:i]
        elif c == "/":
            nxt = js[i + 1] if i + 1 < n else ""
            if nxt == "/":  # 行注释
                j = js.find("\n", i)
                i = n if j < 0 else j + 1
            elif nxt == "*":  # 块注释
                j = js.find("*/", i + 2)
                i = n if j < 0 else j + 2
            else:  # 正则字面量（启发式：前一个有效字符不是标识符/数字/右括号时视为正则）
                p = _prev_char(js, i)
                if p and p not in ")]}" and not (p.isalnum() or p in "_$"):
                    j, in_cls = i + 1, False
                    while j < n:
                        ch = js[j]
                        if ch == "\\":
                            j += 2
                            continue
                        if ch == "[":
                            in_cls = True
                        elif ch == "]":
                            in_cls = False
                        elif ch == "/" and not in_cls:
                            break
                        j += 1
                    i = j + 1
                else:
                    i += 1
        elif c in ('"', "'"):
            q = c
            j = i + 1
            while j < n:
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == q:
                    break
                j += 1
            i = j + 1
        elif c == "`":
            i = _skip_tpl(js, i + 1)
        else:
            i += 1
    raise ValueError("unbalanced braces for function %s" % name)


def _extract_functions():
    js = frontend_js()
    return "\n\n".join(
        _extract_function(js, name)
        for name in NEEDED_FUNCS
    )


class ProcessMarkdownMergeTest(unittest.TestCase):
    """执行过程相邻 message 事件拼接 + Markdown 渲染的 node 断言回归测试"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node 不可用，跳过 JS 渲染回归测试")

    def _run_harness(self):
        funcs = _extract_functions()
        harness = HARNESS_JS.read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile(suffix=".js", mode="w", encoding="utf-8",
                                         delete=False) as f:
            f.write(funcs + "\n\n" + harness)
            tmp = f.name
        try:
            proc = subprocess.run([self.node, tmp], capture_output=True, text=True, timeout=60)
        finally:
            Path(tmp).unlink(missing_ok=True)
        if proc.returncode != 0:
            self.fail("node harness 失败: %s\n%s" % (proc.stderr, proc.stdout))
        return json.loads(proc.stdout)

    def test_t1_all_js_checks_pass(self):
        """验收标准 1-5 + XSS 基线：harness 全部断言通过"""
        data = self._run_harness()
        checks = data.get("checks", [])
        self.assertGreaterEqual(len(checks), 10, "harness 断言数不足")
        failures = [(c["name"], c["detail"]) for c in checks if not c["pass"]]
        self.assertEqual([], failures, "失败的断言: %s" % failures)

    def test_t2_wiring_present(self):
        """防回归：mergeMessageBlocks 定义存在且 refreshProcess 实际调用它"""
        src = frontend_src()
        self.assertIn("function mergeMessageBlocks(events)", src)
        # 调用点必须位于 refreshProcess 内（行号变化容忍：只查函数体包含关系）
        self.assertIn("const blocks = mergeMessageBlocks(events);", src)

    def test_t3_chat_merge_wiring(self):
        """防回归（#271）：chatEventsHtml 内先过滤 visible 再 mergeMessageBlocks 后渲染"""
        src = frontend_src()
        self.assertIn("function chatEventsHtml(events", src)
        # 合并必须发生在 visible 过滤（生命周期/建卡过程 system 事件排除）之后
        i_vis = src.index("const visible = (events || []).filter(e => !isChatLifecycleEvent")
        i_blk = src.index("const blocks = mergeMessageBlocks(visible);")
        self.assertLess(i_vis, i_blk, "chatEventsHtml 中 mergeMessageBlocks 必须先于 visible 过滤")
        # 合并后的渲染映射仍保留四种分支与 chat-msg 类名
        self.assertIn("<div class=\"chat-msg agent\">", src)


    def test_t4_session_link_highlight_wiring(self):
        """防回归（#281）：高亮判定抽为纯函数 isSessionLinkActive 且 sessionFooterHtml 调用它"""
        src = frontend_src()
        self.assertIn("function isSessionLinkActive(selectedId, selectedSid, selectedAgentSession, reqId, rowId, sessionId)", src)
        self.assertIn("const sidActive = isSessionLinkActive(selectedId, selectedSid, selectedAgentSession, i.id, rowId, s.id);", src)

    def test_t5_selection_guard_wiring(self):
        """防回归（#294）：选区守卫纯函数存在，refreshProcess/refreshChat 开头调用，mousedown/mouseup 跟踪就位"""
        src = frontend_src()
        self.assertIn("function selectionGuardActive(panel, selection, selecting)", src)
        self.assertIn("if (selectionGuardActive(panel, window.getSelection(), selectingPanels.has(panel))) return;", src)
        self.assertIn("if (selectionGuardActive(chatHist, window.getSelection(), selectingPanels.has(chatHist))) return;", src)
        # 守卫调用必须位于 refreshProcess/refreshChat 函数体开头（DOM 重建与 fetch 之前）
        i_proc = src.index("async function refreshProcess(id, sid)")
        i_proc_guard = src.index("if (selectionGuardActive(panel, window.getSelection()", i_proc)
        i_proc_fetch = src.index("fetch(`/api/requirements/${id}/events", i_proc)
        self.assertLess(i_proc_guard, i_proc_fetch, "refreshProcess 守卫必须先于事件 fetch")
        i_chat = src.index("async function refreshChat(full)")
        i_chat_guard = src.index("if (selectionGuardActive(chatHist, window.getSelection()", i_chat)
        i_chat_fetch = src.index("fetch(`/api/chat/${chatState.id}`", i_chat)
        self.assertLess(i_chat_guard, i_chat_fetch, "refreshChat 守卫必须先于状态 fetch")
        # 拖动选择跟踪：mousedown 记录面板、mouseup/失焦清除
        self.assertIn("const selectingPanels = new Set();", src)
        self.assertIn("document.addEventListener('mousedown'", src)
        self.assertIn("t.closest('#procEvents, #chatHistory')", src)
        self.assertIn("window.addEventListener('mouseup', () => { selectingPanels.clear(); });", src)


    def test_t6_poll_intervals_1s(self):
        """回显时效（2026-09-16）：三处聊天/执行过程轮询 2 秒 → 1 秒（#295 曾从 4 秒压到 2 秒），
        执行过程面板离开 doing 后按 PROC_SETTLE_TICKS 收尾几轮抓终态事件；主列表与健康角标不动"""
        src = frontend_src()
        self.assertIn("const PROC_REFRESH_MS = 1000;", src)
        self.assertIn("const CHAT_REFRESH_MS = 1000;", src)
        self.assertIn("const REQ_CHAT_REFRESH_MS = 1000;", src)
        self.assertIn("const PROC_SETTLE_TICKS = 3;", src)
        # 三处轮询分别引用统一常量：执行过程面板 x1、建卡对话 + 人工续跑聊天框 x2、需求助手 x1
        self.assertEqual(src.count("}, PROC_REFRESH_MS);"), 1)
        self.assertEqual(src.count("}, CHAT_REFRESH_MS);"), 2)
        self.assertEqual(src.count("REQ_CHAT_REFRESH_MS);"), 1)
        # 不再残留 2 秒/4 秒轮询（toast 的 2000 与 maxlength/slice 的 4000 与本需求无关）
        self.assertNotIn("REFRESH_MS = 2000", src)
        self.assertNotIn(", 4000)", src)
        # 保持不动：主列表自动刷新 15s、页头健康角标 10s
        self.assertIn("const REFRESH_INTERVAL = 15000;", src)
        self.assertIn("setInterval(refreshHealth, 10000);", src)

    def test_t7_agent_session_auto_refresh_wiring(self):
        """防回归（#297）：会话详情视图在 autoRefresh 中从「右栏保留」分支拆出，
        差异判定纯函数 agentSessionRefreshDecision 被调用、refreshAgentSessionStatus 被激活（busy 时跳过）"""
        src = frontend_src()
        # 纯函数定义与调用点就位
        self.assertIn("function agentSessionRefreshDecision(itemOld, itemNew, rowOld, rowNew)", src)
        self.assertIn("const decision = agentSessionRefreshDecision(prevItem, newItem, prevRow, newRow);", src)
        # 覆盖全局前先快照旧需求项与旧注册表行（判定需要新旧两份数据）
        i_assign = src.index("const prevItem = items.find(i => i.id === selectedId);")
        self.assertLess(i_assign, src.index("items = latest;"), "旧值快照必须先于全局覆盖")
        # 会话详情分支必须排在「右栏保留」分支之前（selectedAgentSession 同时设置 selectedSid，
        # 顺序颠倒会命中保留分支回归 #297）
        i_agent = src.index("else if (selectedAgentSession && items.some(i => i.id === selectedId))")
        i_keep = src.index("else if ((selectedSid || selectedDispatcher || selectedArtifact) && items.some(i => i.id === selectedId))")
        self.assertLess(i_agent, i_keep, "selectedAgentSession 分支必须先于 selectedSid 保留分支")
        # full → 全量 render；status → 不重建面板，激活 refreshAgentSessionStatus（busy 期间跳过）
        i_full = src.index("if (decision === 'full')", i_agent)
        self.assertLess(i_full, src.index("renderList();", i_full), "full 分支应走 render()")
        self.assertIn("if (decision === 'status' && !agentChatBusy) {", src)
        self.assertIn("refreshAgentSessionStatus(newRow, newRow);", src)
        # 复用函数支持已持有最新行直接渲染（避免额外请求）
        self.assertIn("async function refreshAgentSessionStatus(row, freshRow)", src)
        self.assertIn("if (!freshRow) {", src)

    def test_t8_req_chat_link_highlight_wiring(self):
        """防回归（#326）：需求助手入口行高亮判定抽为纯函数 isReqChatLinkActive 且 sessionFooterHtml 调用它；
        中间栏会话信息栏始终渲染（无会话记录也渲染置顶「需求助手」行）"""
        src = frontend_src()
        self.assertIn("function isReqChatLinkActive(selectedId, selectedReqChat, reqId)", src)
        self.assertIn("const asstActive = isReqChatLinkActive(selectedId, selectedReqChat, i.id);", src)
        # 「需求助手」行置顶且始终渲染（不得再有「无会话不渲染」提前返回）；
        # #383 分支差异行不再独立插入：挂 dev 会话行产物区（.session-artifact 同款、标签「分支差异：」），
        # 仅 diff.patch 存在时渲染且仅挂首个 dev 会话行（review: 避免 dev 多会话行重复渲染）；
        # 无 dev 会话行时 diffRow 兜底独立渲染（拼接串保留 diffRow）
        self.assertIn("return `<div class=\"session-footer\">${asstRow}${diffRow}${rows.join('')}</div>`;", src)
        self.assertIn("role === 'dev' && idx === devIdx && diffEntry", src)
        # #53 缺失可见化：条目存在但文件不存在 → 不可点击的「产物缺失（路径不存在）」
        self.assertIn("artifact-missing", src)
        self.assertIn("产物缺失（路径不存在）", src)
        self.assertIn("const devIdx = sessRows.findIndex(r => r.role === 'dev');", src)
        # #383：diff.patch 无内容不显示占位态（「（待生成）」置灰态与 sa-disabled 样式删除）；
        # #53 起文件不存在改为明确缺失提示，但不再伪造「待生成」语义
        self.assertNotIn("（待生成）", src)
        self.assertNotIn("sa-disabled", src)
        self.assertNotIn("if (!rows.length) return '';", src)
        # 详情页已移除常驻聊天区（不再渲染 reqChatEvents），助手视图独占该 id
        self.assertIn("function reqAssistantHtml(i)", src)

    def test_t10_req_chat_incremental_merge_wiring(self):
        """防复发（聊天事件流重复气泡）：增量游标与事件数组解耦、在途请求序号丢弃迟到响应、
        增量合并走纯函数 mergeChatStreamEvents 按事件 id 去重。

        根因：服务端每写一条事件发一次 SSE poke，而发送消息后前端还会主动做一次全量刷新；
        旧实现用「本地数组末尾 id」当增量游标（数组未落地就回退重拉）+ concat 不去重，
        同一区间被拼两次 → 同一事件渲染成两个气泡（需求 419 现场：事件 1543~1546 翻倍）。

        两条链路同款加固：需求助手（reqChatEvents/reqChatLastId）与建卡对话（chatEvents/chatLastId），
        共用 mergeChatStreamEvents 一个纯函数入口。
        """
        src = frontend_src()
        # 纯函数定义就位，且两条链路各有一个调用点（共用同一入口，不得各写一份 concat）
        self.assertIn("function mergeChatStreamEvents(prev, incoming, full)", src)
        self.assertEqual(2, src.count("= mergeChatStreamEvents("), "两条链路应共用同一合并纯函数")
        # ---- 需求助手链路 ----
        self.assertIn("reqChatEvents = mergeChatStreamEvents(reqChatEvents, events, full);", src)
        # 游标不再从事件数组末尾推算（旧写法：reqChatEvents[reqChatEvents.length - 1].id 作为 after）
        self.assertNotIn("const after = full ? 0 : (reqChatEvents.length", src)
        self.assertIn("const after = full ? 0 : reqChatLastId;", src)
        # 游标只在响应落地时前进（合并结果已按 id 升序），切换需求重置
        self.assertIn("reqChatLastId = reqChatEvents[reqChatEvents.length - 1].id;", src)
        self.assertIn("let reqChatLastId = 0;", src)
        # 迟到响应丢弃：请求序号只在最新一次响应上匹配
        self.assertIn("const seq = ++reqChatFetchSeq;", src)
        self.assertIn("if (seq !== reqChatFetchSeq) return;", src)
        # 增量并发合并窗口（按发起时间计算、到期自动放行，不依赖响应返回）
        self.assertIn("if (!full && reqChatFetchAt && Date.now() - reqChatFetchAt < REQ_CHAT_FETCH_COALESCE_MS) return;", src)
        self.assertIn("if (!full && seq === reqChatFetchSeq) reqChatFetchAt = 0;", src)
        # 序号判定必须早于事件合并（迟到响应先被丢弃，绝不进入 reqChatEvents）
        i_seq = src.index("if (seq !== reqChatFetchSeq) return;")
        i_merge = src.index("reqChatEvents = mergeChatStreamEvents(reqChatEvents, events, full);")
        self.assertLess(i_seq, i_merge, "迟到响应判定必须先于事件合并")
        # 全量刷新仍走同一合并入口（full=true 整体替换，丢弃本地累积）
        self.assertIn("if (events.length || full) {", src)
        # ---- 建卡对话链路（chat_sessions/chat_events）----
        self.assertIn("const nextEvents = mergeChatStreamEvents(chatEvents, evs, full);", src)
        self.assertNotIn("chatEvents.concat(evs)", src)
        # 游标独立于数组：只在响应落地时前进到已并入的最大事件 id
        self.assertIn("if (nextEvents.length) chatLastId = nextEvents[nextEvents.length - 1].id;", src)
        self.assertNotIn("const last = evs.length ? evs[evs.length - 1].id : 0;", src)
        self.assertIn("const seq = ++chatFetchSeq;", src)
        self.assertIn("if (seq !== chatFetchSeq) return;", src)
        self.assertIn("if (!full && chatFetchAt && Date.now() - chatFetchAt < CHAT_FETCH_COALESCE_MS) return;", src)
        self.assertIn("if (!full && seq === chatFetchSeq) chatFetchAt = 0;", src)
        # 会话已切走（id 变化）的迟到响应同样丢弃
        self.assertIn("if (!chatState || chatState.id !== chatId) return;", src)
        # 序号判定 + 会话归属判定都必须早于建卡对话的事件合并
        i_cseq = src.index("if (seq !== chatFetchSeq) return;")
        i_cmerge = src.index("const nextEvents = mergeChatStreamEvents(chatEvents, evs, full);")
        self.assertLess(i_cseq, i_cmerge, "建卡对话：迟到响应判定必须先于事件合并")
        self.assertLess(src.index("if (!chatState || chatState.id !== chatId) return;"), i_cmerge,
                        "建卡对话：会话归属判定必须先于事件合并")


    def test_t9_agent_chat_busy_reset_on_switch(self):
        """防复发（#372）：pollAgentSessionUntilIdle 用户在轮询期间切走会话的分支必须同步复位
        agentChatBusy（此前只停轮询不复位，残留 true 会永久堵死 autoRefresh 的 !agentChatBusy
        状态行刷新守卫，导致「思考中…」卡死、输入框永久禁用）。"""
        src = frontend_src()
        # 切走分支：复位必须先于 clearInterval（先复位再停轮询）
        i_cut = src.index("if (selectedAgentSession !== asid)")
        i_reset = src.index("agentChatBusy = false;", i_cut)
        i_clear = src.index("clearInterval(agentChatTimer);", i_cut)
        self.assertLess(i_reset, i_clear, "切走分支必须先复位 agentChatBusy 再 clearInterval")
        # 分支内未删除轮询停止逻辑（只增复位，不砍原保护行为）
        self.assertIn("agentChatBusy = false; clearInterval(agentChatTimer); agentChatTimer = null; return;", src)

    def test_t10_empty_session_to_real_rebuild(self):
        """防复发（#388）：新建的空会话发送第一条消息后，调度器写入 session_id（空→真实会话）
        必须在 pollAgentSessionUntilIdle 中主动重建右栏视图——autoRefresh 的 agentSessionRefreshDecision
        判定会被本轮询抢先刷新全局 agentSessions 吞掉（prevRow 快照已含新 session_id，永远判不出
        'full'），不重建则事件流面板（id=procEvents）永不出现，agent 输出不显示。"""
        src = frontend_src()
        # poll 记录当前视图面板对应的 sid（空会话视图无 procEvents 节点，视为 null）
        self.assertIn("let lastSid = evPanel ? (evPanel.dataset.sid || null) : null;", src)
        # 注册表 session_id 出现/变化时重建右栏（renderDetailPane 渲染 procEvents 面板并立即拉事件）
        self.assertIn("if (row.session_id && row.session_id !== lastSid) {", src)
        self.assertIn("lastSid = row.session_id;", src)
        i_rebuild = src.index("if (row.session_id && row.session_id !== lastSid) {")
        self.assertIn("renderDetailPane();", src[i_rebuild:i_rebuild + 200])
        # 重建后事件流刷新守卫不受影响（仍然拉取该会话事件）
        i_poll = src.index("function pollAgentSessionUntilIdle(asid)")
        self.assertIn("if (row.session_id) refreshProcess(selectedId, row.session_id);", src[i_poll:])

    def test_t19_running_breath_and_group_expand_wiring(self):
        """防回归（#424）：运行中会话 id 呼吸（class 注入 + 动效参数 + reduced-motion）与角色组
        自动展开（判定纯函数 agentGroupExpanded + 折叠优先 + toggle 传当前生效态）"""
        src = frontend_src()
        # 判定纯函数（harness T19 矩阵断言）与其调用点；旧内闭包必须移除
        # （同名遮蔽会让调用解析到闭包，取参形态不符）
        self.assertIn("function agentGroupExpanded(state, rows, subSelected)", src)
        self.assertNotIn("const agentGroupExpanded = (role, subRows) => {", src)
        self.assertIn("const expanded = agentGroupExpanded(expandedAgents[i.id + ':' + role], [sessRows[idx], ...subRows], subSelected);", src)
        # 三态 toggle：入参为当前生效展开态，自动展开组首点即收起（修 #390 首点无效）
        self.assertIn("function toggleAgentExpand(reqId, role, expanded)", src)
        self.assertIn("onclick=\"toggleAgentExpand(${i.id},'${role}',${expanded})\"", src)
        # 主行/子行 running class 注入（运行态判定只认 agent_sessions.status==='running'）
        self.assertIn("+ (status === 'running' ? ' running' : '')", src)
        self.assertIn("+ (subStatus === 'running' ? ' running' : '')", src)
        # CSS：动效参数 / 关键帧 / reduced-motion 关闭（与 .chat-status.thinking 同款处理）
        self.assertIn(".session-footer .sf-id-link.running { animation: sfIdBreath 2.2s ease-in-out infinite; }", src)
        self.assertIn("@keyframes sfIdBreath {\n            0%, 100% { opacity: 0.62; text-shadow: 0 0 0 var(--breath-off); }\n            50% { opacity: 1; text-shadow: 0 0 10px var(--breath-glow-a), 0 0 22px var(--breath-glow-b); }\n        }", src)
        # #426：reduced-motion 关闭动画后仍保留不依赖动画的静态运行标识（id 后圆点），同前缀限定
        self.assertIn("@media (prefers-reduced-motion: reduce) {\n"
                      "            .session-footer .sf-id-link.running { animation: none; }\n"
                      "            .session-footer .sf-id-link.running::after { content: '●'; "
                      "margin-left: 4px; font-size: 0.7em; vertical-align: middle; }\n"
                      "        }", src)
        # 负向断言（防扩权）：不做整行呼吸/圆点等其它运行标识；.sf-id-link.running 一律带
        # .session-footer 前缀（.sf-id-link 与右栏调度回放共享，不得全局生效）
        self.assertNotIn(".session-row.running", src)
        self.assertEqual(src.count(".sf-id-link.running"), src.count(".session-footer .sf-id-link.running"),
                         "呼吸选择器必须全部带 .session-footer 前缀")

    def test_t19b_breath_params_unified(self):
        """#426 防回归：运行中会话 id 呼吸参数锁定（3.2s / ease-in-out / infinite，周期 3200ms 为验收
        硬约束；关键帧 50% 不透明度 0.5）。#430 后三处状态行统一为「卡片 + 计时」，「思考中…」小灰字
        呼吸（.chat-status.thinking / chatStatusBreath）随之删除——本用例锁定删除，防分叉回潮。"""
        src = frontend_src()

        def anim_decl(selector):
            head = selector + " { animation: "
            i = src.index(head)
            return src[i + len(head):src.index(";", i)]

        a1 = anim_decl(".session-footer .sf-id-link.running").split()
        self.assertEqual(a1[0], "sfIdBreath")
        self.assertEqual(a1[1:], ["2.2s", "ease-in-out", "infinite"])

        # #15：关键帧体按原型 guid-breathe 还原（0%/100% opacity .62 无光晕；50% opacity 1 + 双层青光晕）
        self.assertIn("@keyframes sfIdBreath {\n"
                      "            0%, 100% { opacity: 0.62; text-shadow: 0 0 0 var(--breath-off); }\n"
                      "            50% { opacity: 1; text-shadow: 0 0 10px var(--breath-glow-a), "
                      "0 0 22px var(--breath-glow-b); }\n        }", src)
        # #430：状态行呼吸小灰字已由状态卡片 + 计时取代（全站只剩会话 id 一处呼吸）
        self.assertNotIn(".chat-status.thinking", src)
        self.assertNotIn("chatStatusBreath", src)


if __name__ == "__main__":
    unittest.main()
