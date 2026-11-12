"""命令与工具审批策略（纯函数）：Playwright/TAPD 白名单、进程清理红线、主工作区写护栏、
git / 受限 python 白名单、只读命令白名单。ACPSession 的审批层调用这里的判定函数。

由 acp_shared.py 拆出；acp_shared 以显式名单回挂全部名字（含下划线名）。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from acp_modules.limits import *  # noqa: F401,F403
from acp_modules.limits import _READ_VERB_RE  # noqa: F401


# ---- #268 建卡会话 Playwright 调研放行（保底安全）----
# 建卡会话（board.py _chat_boot extra_allow_prefixes 注入）与 dev/accept 会话（#379 dispatcher 注入）
# 放行；dispatcher 只读角色会话（analysis/proposal/tech/review）无此前缀，
# L2 对 mcp__playwright__* 维持原拒绝、L3 URL 兜底不启用（行为零变化）。
# 放行/拒绝双清单 + URL 协议校验集中于此，便于集中审计与单测断言（fail-closed：清单外一律拒绝）。
PLAYWRIGHT_TOOL_PREFIX = "mcp__playwright__"   # MCP server 名 = playwright
PLAYWRIGHT_ALLOWED_TOOLS = frozenset({
    "browser_navigate", "browser_navigate_back", "browser_click", "browser_type",
    "browser_fill_form", "browser_select_option", "browser_hover", "browser_press_key",
    "browser_drag", "browser_tabs", "browser_close", "browser_resize",
    "browser_handle_dialog", "browser_wait_for",
    "browser_snapshot", "browser_find", "browser_take_screenshot",
    "browser_console_messages", "browser_network_requests", "browser_network_request",
})
PLAYWRIGHT_DENIED_TOOLS = frozenset({
    "browser_run_code_unsafe", "browser_evaluate", "browser_file_upload", "browser_drop",
})

# ---- tapd MCP 只读白名单（建卡会话/需求对话调研 TAPD 需求用）----
# 与 mcp-server-tapd 8.0.81 暴露的工具清单对齐：仅放行 get_* 只读工具（查需求/缺陷/评论/
# 迭代/用户/工作流等），create/update/comment/send 等写工具与未知工具一律拒绝（fail-closed）。
# 由 board.py _chat_boot/_reqchat_boot 经 extra_allow_prefixes 注入；dispatcher 只读角色未注入前缀，
# 行为与现状一致（tapd 工具仍拒绝）。审批分支与 Playwright 双清单同构，便于集中审计与单测断言。
TAPD_TOOL_PREFIX = "mcp__tapd__"
TAPD_READONLY_TOOLS = frozenset({
    "get_user_participant_projects",
    "get_stories_or_tasks",
    "get_story_or_task_count",
    "get_bug",
    "get_bug_count",
    "get_comments",
    "get_image",
    "get_entity_attachments",
    "get_entity_custom_fields",
    "get_workflows_all_transitions",
    "get_workflows_status_map",
    "get_workflows_last_steps",
    "get_workitem_types",
    "get_stories_fields_lable",
    "get_stories_fields_info",
    "get_workspace_info",
    "get_sub_workspaces",
    "get_iterations",
    "get_related_bugs",
    "get_entity_relations",
    "get_tcases",
    "get_wiki",
    "get_todo",
    "get_timesheets",
    "get_commit_msg",
    "get_release_info",
    "get_workspace_users",
})
HTTP_URL_RE = re.compile(r"^https?://", re.I)
_URL_TOKEN_RE = re.compile(r"(?:https?://|file://|data:|javascript:|chrome://)[^\s\"'<>]+", re.I)


def _url_protocol_violation(text):
    """扫描文本中的 URL 形态 token，存在非 http(s) 协议返回违规 token 描述，无 URL 返回 None。
    #268 L2 best-effort 校验：审批文本可能不含参数（kimi acp toolCall 不带参数），无 URL 不拦（L3 兜底）。"""
    if not text:
        return None
    for tok in _URL_TOKEN_RE.findall(text):
        if not HTTP_URL_RE.match(tok):
            return tok
    return None


# ---- 进程安全红线（#108）：禁杀自 PID/任何 kimi 进程。检测与注入文本共享（P1d/P1e/dispatcher/board 共用），
# 两层判定一致、单测一次覆盖（AGENTS.md：共享模块校验函数统一从 acp_shared.py 取） ----
KILL_REDLINE_TMPL = (
    "【进程安全红线】你的 ACP 驱动进程 PID={pid}（进程组）。\n"
    "禁止 kill/killpg/pkill 该 PID、其进程组及任何 kimi 进程（含 kimi-chat-acp）。\n"
    "清理残留进程必须使用项目约定的实例脚本或精确 PID，"
    "禁止 pkill -f kimi 类路径猜测（会误杀他人会话与交互式 kimi）。\n"
    "禁止自行编写 trap 清理命令；禁止 kill/killpg/pkill 清理 ACP 驱动进程或其进程组。"
)
# dev L3 取消信号前缀（#113）：发射端（_on_update L3）与检测端（dispatcher 失败归因）共用一处，防文案漂移
REDLINE_CANCEL_SIGNAL = "自杀式进程清理被拦截"
_KILL_VERB_RE = re.compile(r"\b(?:pkill|killall|killpg|kill)\b")
_KILL_PID_RE = re.compile(r"\b(?:kill|killpg)(?:\s+-{1,2}[A-Za-z0-9]+)?(?:\s+--)?\s+(\d+)")

# ---- #113 v5 结构解析版红线检测：区分「数据写入」vs「实际执行」，安全不确定时兜底保守拒绝 ----
# 内容/参数中出现这些词即视为「含红线词」（写文件内容、pkill 参数、exec 调用参数等场景统一判定）
_REDLINE_WORD_RE = re.compile(r"kimi|\b(?:pkill|killall|killpg|kill)\b", re.I)
# L2 审批输入前缀（ACP 审批文本为完整「Requesting approval to Running: <command>」，
# 检测前必须剥离前缀提取真实命令，杜绝首动词=Requesting 被跳过的漏检；#113 v3 否决反馈）
_APPROVAL_PREFIX_RE = re.compile(r"^\s*Requesting approval to Running:\s*", re.I)
_PROC_CTRL_VERBS = ("pkill", "killall", "killpg", "kill")
_EXEC_ENTRY_VERBS = ("python", "python3", "python2", "bash", "sh", "zsh", "dash",
                     "perl", "ruby", "node", "php", "lua")
_EXEC_CHAIN_VERBS = ("xargs", "sudo", "env", "nohup", "timeout")
_DATA_VERBS = ("echo", "printf", "cat", "tee")
# 解释器 -c 代码中真正的进程执行调用（subprocess/os），普通 json.dumps/print 不命中（#113 v2 否决反馈：
# 需拦截 python3 -c 'subprocess.run(["pkill","-f","kimi"])'，同时放行写 JSON 摘要的 heredoc/-c 数据）
_PY_EXEC_CALL_RE = re.compile(
    r"\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)|os\.(?:system|kill(?:pg)?|popen))\s*\(", re.I)
# 常见信号名 + 数字信号 1-64：kill/killpg 参数解析时识别并跳过信号选项（-9/-TERM/-s TERM），
# 避免把 -9 当目标 PID 造成 self_pid 漏检（#113 v4 否决反馈，v5 修正）
_SIG_NAMES = {"HUP", "INT", "QUIT", "ILL", "ABRT", "FPE", "KILL", "SEGV", "PIPE", "ALRM",
              "TERM", "USR1", "USR2", "CHLD", "CONT", "STOP", "TSTP", "TTIN", "TTOU", "BUS",
              "POLL", "PROF", "SYS", "TRAP", "URG", "VTALRM", "XCPU", "XFSZ", "WINCH", "IO",
              "PWR", "INFO", "0"} | {str(n) for n in range(1, 65)}
# 执行链（xargs/sudo/env/nohup/timeout）的「裸选项 + 跟随参数」选项；附着形式（-n1 / -I{} /
# -uroot / --kill-after=5）自带参数，单 token 跳过即可。按外层动词分别维护，避免 sudo -n（无参数）
# 被误当成 xargs -n（有参数）后跳过真正的内层命令（#113 accept 问题 1 修复）。
_CHAIN_SHORT_OPT_WITH_ARG = {
    "sudo": {"-C", "-D", "-g", "-h", "-p", "-R", "-r", "-t", "-T", "-u"},
    "env": {"-a", "-C", "-S", "-u"},
    "timeout": {"-k", "-s"},
    "xargs": {"-a", "-E", "-I", "-L", "-n", "-P", "-s"},
    "nohup": set(),
}
_CHAIN_LONG_OPT_WITH_ARG = {
    "sudo": {"--chdir", "--close-from", "--group", "--host", "--prompt", "--role", "--type", "--user"},
    "env": {"--argv0", "--chdir", "--split-string", "--unset"},
    "timeout": {"--kill-after", "--signal"},
    "xargs": {"--arg-file", "--eof", "--max-args", "--max-chars", "--max-lines", "--max-procs", "--replace"},
    "nohup": set(),
}
_TIMEOUT_DURATION_RE = re.compile(r"^(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[smhd])?|inf(?:inity)?)$", re.I)


def _bare_tokens(s):
    """引号感知切词，返回去引号后的 token 列表；引号不平衡返回 None（上层按保守处理）。
    单引号/双引号内内容保留（空格、特殊字符均按字面量），引号本身剥离。"""
    toks, cur = [], ""
    in_sq = in_dq = False
    for ch in s:
        if in_dq:
            if ch == '"':
                in_dq = False
            else:
                cur += ch
        elif in_sq:
            if ch == "'":
                in_sq = False
            else:
                cur += ch
        elif ch == '"':
            in_dq = True
        elif ch == "'":
            in_sq = True
        elif ch in " \t":
            if cur:
                toks.append(cur)
                cur = ""
        else:
            cur += ch
    if in_sq or in_dq:
        return None
    if cur:
        toks.append(cur)
    return toks


def _strip_quoted_token(s):
    """提取 -c 后紧跟的代码 token：引号包裹时取引号内内容，否则取到下一个空白。
    引号未闭合返回取到末尾（引号不平衡已在 _split_exec_units 整体拦截兜底）。"""
    s = s.lstrip(" \t")
    if not s:
        return ""
    if s[0] in ("'", '"'):
        q = s[0]
        for j in range(1, len(s)):
            if s[j] == q:
                return s[1:j]
        return s[1:]
    for j, ch in enumerate(s):
        if ch in " \t":
            return s[:j]
    return s


def _exec_code_raw(raw):
    """在原始单元文本中找解释器 -c 选项并提取其后的代码（引号感知）。
    返回 (code|None, ok)；ok=False 表示 -c 存在但无代码（歧义，上层保守命中）。"""
    i, n = 0, len(raw)
    in_sq = in_dq = False
    while i < n:
        ch = raw[i]
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif not in_sq and not in_dq and raw[i:i + 2] == "-c" and (i + 2 >= n or raw[i + 2] in " \t"):
            j = i + 2
            while j < n and raw[j] in " \t":
                j += 1
            if j >= n:
                return None, False
            return _strip_quoted_token(raw[j:]), True
        i += 1
    return None, True


def _subprocess_call_args(code, pos):
    """取 _PY_EXEC_CALL_RE 匹配后调用括号内的参数文本（括号配平扫描）。"""
    depth = 0
    for j in range(pos, len(code)):
        c = code[j]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return code[pos:j]
            depth -= 1
    return code[pos:]


def _split_exec_units(text):
    """按 ; / && / || / | / & / 换行 拆执行单元（引号与 heredoc 正文内不分隔），参数保留原样。
    返回 (units, records)：
    - units: [(verb, args, raw)]——verb=首 token（去引号/路径取 basename），args=首 token 后的原样剩余
    - records: 写入记录 [{target, redline}]——heredoc 块（<<DELIM 内容 + 重定向目标）与
      echo/printf/cat/tee 重定向写入（参数含红线词）统一记录，供「写脚本后执行」比对
    分割异常（引号不平衡 / heredoc 未终止）→ 返回 (None, None)（上层走全文本兜底，保守）。"""
    units, records = [], []
    buf = []
    i, n = 0, len(text)
    in_sq = in_dq = False
    heredoc = None            # 处于 heredoc 正文：{"delim", "target", "redline"}
    pending_heredoc = None    # 本行已见 <<DELIM：{"delim", "target"}
    cur_targets = []          # 当前单元的重定向目标

    def flush_unit():
        nonlocal cur_targets
        raw = "".join(buf).strip()
        buf.clear()
        if not raw:
            cur_targets = []
            return
        toks = _bare_tokens(raw)
        verb = os.path.basename(toks[0]) if toks else None
        args = raw
        if toks:
            # 首 token 在原串中的结束位置：按引号感知扫描定位
            end = 0
            q1 = q2 = False
            while end < len(raw) and not (raw[end] in " \t" and not q1 and not q2):
                if raw[end] == "'" and not q2:
                    q1 = not q1
                elif raw[end] == '"' and not q1:
                    q2 = not q2
                end += 1
            args = raw[end:].strip()
        units.append((verb, args, raw))
        if verb in _DATA_VERBS and cur_targets:
            redline = bool(_REDLINE_WORD_RE.search(raw))
            for t in cur_targets:
                records.append({"target": t, "redline": redline})
        cur_targets = []

    while i < n:
        ch = text[i]
        if heredoc is not None:
            line_end = text.find("\n", i)
            if line_end == -1:
                # heredoc 终止符为最后一行（无尾随换行）：仍按整行判定
                line = text[i:]
                if line.strip() == heredoc["delim"]:
                    records.append({"target": heredoc["target"], "redline": heredoc["redline"]})
                    heredoc = None
                    i = n
                    continue
                return None, None            # heredoc 未终止 → 解析失败（保守兜底）
            line = text[i:line_end]
            if line.strip() == heredoc["delim"]:
                records.append({"target": heredoc["target"], "redline": heredoc["redline"]})
                heredoc = None
            else:
                heredoc["redline"] = heredoc["redline"] or bool(_REDLINE_WORD_RE.search(line))
            i = line_end + 1
            continue
        if pending_heredoc is not None and ch == "\n":
            flush_unit()                     # << 行作为普通单元处理（verb 正常路由，如 kill ... <<EOF）
            heredoc = {"delim": pending_heredoc["delim"], "target": pending_heredoc["target"],
                       "redline": False}
            pending_heredoc = None
            i += 1
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            buf.append(ch)
            i += 1
            continue
        if in_sq or in_dq:
            buf.append(ch)
            i += 1
            continue
        if ch in ";&|\r\n":                  # 引号外：单元分隔符
            flush_unit()
            j = i
            while j < n and text[j] in ";&|\r\n \t":
                j += 1
            i = j
            continue
        if ch == "<" and text[i:i + 2] == "<<" and text[i:i + 3] != "<<<":
            m = re.match(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*|\"[^\"]*\"|'[^']*')", text[i:])
            if m:
                delim = m.group(1).strip("\"'")
                target = cur_targets[-1] if cur_targets else None
                pending_heredoc = {"delim": delim, "target": target}
                buf.append(text[i:i + m.end()])
                i += m.end()
                continue
            buf.append(ch)                   # 非 heredoc 的 <<（比较/位移等）：并入缓冲
            i += 1
            continue
        if ch == ">":
            m = re.match(r">>?\s*([^\s;&|<>]+)", text[i:])
            if m:
                t = m.group(1).strip("\"'")
                cur_targets.append(t)
                if pending_heredoc is not None:
                    pending_heredoc["target"] = t
                buf.append(text[i:i + m.end()])
                i += m.end()
                continue
            buf.append(ch)
            i += 1
            continue
        buf.append(ch)
        i += 1
    if in_sq or in_dq or pending_heredoc is not None or heredoc is not None:
        return None, None
    flush_unit()
    return units, records


def _kill_pid_targets(args):
    """按参数顺序解析 kill/killpg 的目标 PID（含负号=进程组信号；#113 v5 修正：
    不用单个正则提取，明确跳过 -9/-TERM/-s TERM 等信号选项后检查所有 PID 目标）。
    返回 PID 列表（可为空）；解析歧义/异常返回 None（上层保守命中）。"""
    toks = _bare_tokens(args)
    if toks is None:
        return None
    targets = []
    after_ddash = False
    i = 0
    while i < len(toks):
        t = toks[i]
        if after_ddash:
            if t.isdigit() or (t.startswith("-") and t[1:].isdigit() and len(t) > 1):
                targets.append(int(t))
            i += 1
            continue
        if t == "--":
            after_ddash = True
            i += 1
            continue
        if t in ("-s", "-n"):
            if i + 1 >= len(toks):
                return None                 # 歧义：-s/-n 缺信号参数
            i += 2
            continue
        if t.startswith("-") and len(t) > 1:
            val = t[1:]
            if val in _SIG_NAMES:
                i += 1
                continue
            if val.isdigit():
                targets.append(-int(val))   # 数字非信号名（>64）→ 负 PID 目标（进程组）
                i += 1
                continue
            i += 1                          # 其它选项（-l 等）忽略
            continue
        if t.isdigit() or (t.startswith("+") and t[1:].isdigit()):
            targets.append(int(t))
        i += 1
    return targets


def _chain_inner(verb, args):
    """执行链（xargs/sudo/env/nohup/timeout）后续命令的首个动词与其参数（#113 accept 问题 1 修复：
    按 tech.md §3.1.3「对后续参数（去选项）递归」定位内层命令动词，杜绝首 token 是选项/时长/赋值导致递归中断）。
    依次：-- 之后全部视为命令参数；跳过 env 的 VAR=x 赋值；按外层动词跳过纯选项与已知裸选项
    的跟随参数；timeout 额外跳过一个时长参数；首个剩余 token 即内层命令动词。
    任何解析歧义（已知裸选项缺跟随参数/无内层命令/无法判断语义）→ 返回 None，
    上层对整链做 _kill_redline_hit_legacy 全文本兜底（保守，宁可多拦）。"""
    toks = _bare_tokens(args)
    if not toks:
        return None
    i, n = 0, len(toks)
    timeout_duration_seen = False
    short_with_arg = _CHAIN_SHORT_OPT_WITH_ARG.get(verb, set())
    long_with_arg = _CHAIN_LONG_OPT_WITH_ARG.get(verb, set())
    while i < n:
        t = toks[i]
        if t == "--":                       # -- 之后全部为命令参数，首个 token 即内层命令动词
            i += 1
            break
        if verb == "env" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
            continue
        if verb == "timeout" and not timeout_duration_seen and _TIMEOUT_DURATION_RE.match(t):
            timeout_duration_seen = True
            i += 1
            continue
        if t.startswith("-"):
            if t in short_with_arg or t in long_with_arg:
                if i + 1 >= n:
                    return None              # 缺跟随参数 → 歧义，保守兜底
                i += 2
                continue
            i += 1                           # 纯选项/附着选项/长选项：自含，单 token 跳过
            continue
        break                                # 首个非选项非数字 → 内层命令动词
    if i >= n:
        return None
    return os.path.basename(toks[i]), " ".join(toks[i + 1:])


def _ambiguous_chain_hit(raw, self_pid):
    """执行链无法确定内层命令时先沿用 legacy 判定；若仍未命中但出现进程控制词，按不确定即拒绝。"""
    hit = _kill_redline_hit_legacy(raw, self_pid)
    if hit:
        return hit
    if _REDLINE_WORD_RE.search(raw):
        return "执行链解析歧义且含进程控制词（保守命中）"
    return None


def _stdin_redline(text):
    """管道/heredoc/here-string 喂入 shell 且数据含红线词（stdin 执行保守命中）。
    覆盖：`echo x | bash`、`bash <<EOF ... EOF`、`bash <<< "x"` 形态。"""
    return (_REDLINE_WORD_RE.search(text) is not None
            and re.search(r"(?:\||<<<?)\s*(?:bash|sh|zsh)\b|(?:bash|sh|zsh)\b\s*<<<?", text) is not None)


def _analyze_unit(verb, args, raw, records, self_pid, text, depth=0):
    """单个执行单元判定（纯函数）：命中返回原因字符串，未命中返回 None。"""
    if verb in _PROC_CTRL_VERBS:
        if re.search(r"kimi", args, re.I):
            return "pkill/killall 命中 kimi" if verb in ("pkill", "killall") else "kill/killpg 文本含 kimi"
        pids = _kill_pid_targets(args)
        if pids is None:
            return "kill/killpg 参数解析歧义（保守命中）"
        if self_pid is not None and any(abs(p) == self_pid for p in pids):
            return f"kill/killpg 目标为本会话驱动进程/进程组（PID={self_pid}）"
        if any(p == 0 for p in pids):
            # PID 0 = 当前进程组信号（潜在自杀向量），与 kill -0 <pid> 存在性检查不同（-0 已被跳过）
            return "kill/killpg 目标为当前进程组（PID 0，保守命中）"
        return None
    if verb in _EXEC_ENTRY_VERBS:
        if re.search(r"(?:^|\s)-c(?:\s|$)", args):
            code, ok = _exec_code_raw(raw)
            if not ok:
                return "解释器 -c 代码提取歧义（保守命中）"
            if code:
                return _exec_code_check(code, self_pid, depth + 1)
        if records:
            red_targets = {r["target"] for r in records if r.get("redline") and r.get("target")}
            if red_targets:
                toks = _bare_tokens(args)
                if toks is None:
                    return "解释器参数解析歧义（保守命中）"
                red_names = {os.path.basename(t) for t in red_targets}
                for t in toks:
                    if t in red_targets or os.path.basename(t) in red_names:
                        return "执行写有红线内容的脚本文件（写脚本后执行）"
        return None
    if verb in _EXEC_CHAIN_VERBS:
        if depth > 6:
            return _ambiguous_chain_hit(raw, self_pid)
        inner = _chain_inner(verb, args)
        if inner is None:
            return _ambiguous_chain_hit(raw, self_pid)
        iv, iargs = inner
        hit = _analyze_unit(iv, iargs, raw, records, self_pid, text, depth + 1)
        if hit:
            return hit
        # 保守：执行链调用进程控制命令且整链含 kimi（如 ps | grep kimi | xargs kill）；
        # 不含 kimi 的 kill 非 self PID（sudo kill 54321）不误伤
        if iv in _PROC_CTRL_VERBS and re.search(r"kimi", text, re.I):
            return f"{verb} 执行链调用进程控制命令"
        # 内层命令未知（非任何动词类别：包装脚本/别名/时长单位如 5s 等）→ 整链 legacy 全文本兜底
        if iv not in _PROC_CTRL_VERBS and iv not in _EXEC_ENTRY_VERBS \
                and iv not in _EXEC_CHAIN_VERBS and iv not in _DATA_VERBS:
            return _ambiguous_chain_hit(raw, self_pid)
        return None
    return None


def _exec_code_check(code, self_pid, depth=0):
    """解释器 -c 代码递归检测：python 代码查 subprocess/os 执行调用（subprocess.* 参数含进程控制动词；
    os.kill/os.killpg 直杀目标 PID==self / PID 0 进程组 / 参数含 kimi → 命中，accept 问题 2 修复）；
    通用 shell 结构递归；递归过深/解析失败 → 原三层规则兜底（保守）。"""
    if depth > 4:
        return "解释器 -c 代码嵌套过深（保守命中）"
    if _PY_EXEC_CALL_RE.search(code):
        for m in _PY_EXEC_CALL_RE.finditer(code):
            call_args = _subprocess_call_args(code, m.end())
            fn = m.group(0)
            if fn.startswith("os.kill"):
                # accept 问题 2 修复：os.kill/os.killpg 参数是 PID 数字（非动词），须按目标判定——
                # 首个数字参数为目标 PID：abs==self_pid（含负 PID 进程组）或 PID 0（当前进程组）→ 命中；
                # 参数含 kimi → 保守命中
                m2 = re.search(r"[-+]?\d+", call_args or "")
                if m2:
                    pid = int(m2.group(0))
                    if pid == 0 or (self_pid is not None and abs(pid) == self_pid):
                        return "解释器代码 os.kill/os.killpg 直杀本会话驱动进程/进程组"
                if re.search(r"kimi", call_args or "", re.I):
                    return "解释器代码 os.kill/os.killpg 目标含 kimi（保守命中）"
            elif re.search(r"\b(?:pkill|killall|killpg|kill)\b", call_args or ""):
                return "解释器代码调用进程控制命令（subprocess/os）"
    units, records = _split_exec_units(code)
    if units is None:
        return _kill_redline_hit_legacy(code, self_pid)
    for verb, args, raw in units:
        hit = _analyze_unit(verb, args, raw, records, self_pid, code, depth)
        if hit:
            return hit
    return None


def _extract_command(text):
    """从 ACP 审批输入提取真实命令（L2 前置，#113 v3 反馈）：
    - 结构字段（dict 含 command）→ 直接取 command
    - 完整审批文本「Requesting approval to Running: <command>」→ 剥离前缀
    - 无前缀纯命令 → 原样返回（L3/测试直调双向兼容）"""
    if isinstance(text, dict):
        return text.get("command") or ""
    return _APPROVAL_PREFIX_RE.sub("", text).strip()


def _kill_redline_hit_legacy(text, self_pid):
    """原三层文本近似规则（#108 保守兜底）：结构解析失败时全文本判定，宁可多拦。"""
    if not isinstance(text, str) or not text.strip():
        return None
    has_kimi = re.search(r"kimi", text, re.I) is not None
    if re.search(r"\b(?:pkill|killall)\b", text) and has_kimi:
        return "pkill/killall 命中 kimi"
    if _KILL_VERB_RE.search(text):
        m = _KILL_PID_RE.search(text)
        if m and self_pid is not None and int(m.group(1)) == self_pid:
            return f"kill/killpg 目标为本会话驱动进程（PID={self_pid}）"
        if has_kimi:
            return "kill/killpg 文本含 kimi"
    return None


def kill_redline_text(pid, lang="zh"):
    """渲染禁杀红线文本（P1a 注入用）。正文取提示词存储 system/kill-redline（本机覆盖优先，
    KILL_REDLINE_TMPL 为默认文本的兼容常量，与 prompts/system/kill-redline.md 逐字一致）。"""
    import prompt_store
    return prompt_store.render("system/kill-redline", lang=lang, pid=pid)


def append_kill_redline(prompt, pid, lang="zh"):
    """把红线追加到 prompt 末尾（纯函数，dispatcher/board 共用，便于单测）"""
    return prompt + "\n\n" + kill_redline_text(pid, lang)


def kill_redline_hit(text, self_pid):
    """进程清理红线检测（#113 v5 结构解析版）：命中返回原因字符串，未命中返回 None。
    设计（tech.md §3.1，评审 v2/v3/v4/v5 反馈吸收）：
    1. 命令结构解析（_split_exec_units）：按 ;/&&/||/|/换行 拆执行单元，引号与 heredoc 正文内不分隔；
       heredoc 块与 echo/printf/cat/tee 重定向写入统一记录 {target, redline}。
    2. 动词路由（_analyze_unit）：进程控制动词（pkill/killall/killpg/kill）→ 参数含 kimi 或目标 PID
       == self_pid（含带引号、负 PID 进程组、跳过 -9/-TERM/-s TERM 等信号选项）或 PID 0（当前进程组）
       → 命中；解释器 -c 代码递归（python 查 subprocess/os 执行调用，os.kill 直杀按 PID 判定）→ 命中；
       写脚本后执行（记录比对）→ 命中；xargs/sudo 等执行链 → 跳过选项/数字/赋值定位内层命令后递归，
       解析歧义或内层命令未知 → 整链 legacy 全文本兜底（accept 问题 1 修复）+ 整链含 kimi 保守命中；
       数据动词（echo/printf/cat/tee）→ 跳过。
    3. 纯数据写入（heredoc/JSON 摘要/说明文字含 kill/kimi 词）不命中（A1-A4）；
       结构解析失败（引号不平衡/heredoc 未终止）→ 回退全文本原三层规则（保守，宁可多拦）。
    self_pid 为 None（桩/防御）时跳过 PID 规则，kimi 规则仍生效。"""
    if not isinstance(text, str) or not text.strip():
        return None
    units, records = _split_exec_units(text)
    if units is None:
        return _kill_redline_hit_legacy(text, self_pid)
    for verb, args, raw in units:
        hit = _analyze_unit(verb, args, raw, records, self_pid, text)
        if hit:
            return hit
        if verb in ("bash", "sh", "zsh") and _stdin_redline(text):
            return "管道/heredoc/重定向喂入 shell 执行（数据含红线词）"
    return None


# ---- #336 写角色 Bash 护栏：写操作目标落在主工作区（各仓库真实路径）即拒绝 ----
# 定位：防手滑护栏而非机制保证——可绕过（base64/变量拼接/cd 后相对路径/解释器 -c 与 heredoc
# 正文内的写操作/sudo 等执行链），AGENTS.md 与 docs/dispatcher.md 如实写明拦什么、不拦什么。
# 解析写目标而非文本共现：>/>> 重定向目标、tee 参数路径、git -C 路径+写子命令（双条件同时
# 满足才拦）、rm/mv/chmod 目标路径；主工作区路径出现但写目标合法的放行（<主工作区>/.venv/bin/
# python 跑测试重定向到 /tmp、git -C <主工作区> log 只读诊断）。fail-closed：解析歧义（tee/rm/mv/
# chmod 参数引号不平衡、git -C 缺路径）拦截；#344 起整体结构解析失败降级为「写动词字样+
# 主工作区路径字样共现才拦」（修复 resume 审批文本拼接含撇号描述段致误拦）。
GIT_WRITE_SUBCMDS = frozenset({  # 与 COMMIT_RE 写子命令集对齐（add/commit/push/...）
    "add", "commit", "push", "reset", "rebase", "checkout", "switch", "stash",
    "merge", "cherry-pick", "revert", "clean", "restore", "mv", "rm"})
_MAIN_WS_PATH_VERBS = ("rm", "mv", "chmod")  # 参数为目标路径的写命令
# git 全局选项中带值形态（-C 单独处理；--git-dir=<p> 等附着形态不在护栏范围）
_GIT_GLOBAL_OPT_WITH_ARG = ("-c", "--git-dir", "--work-tree", "--namespace")


def _redirect_targets(raw):
    """从执行单元原文提取 > / >> 重定向目标（引号感知，引号内不识别；2>&1 等 fd 复制不匹配）。
    与 _split_exec_units 内同款正则，对齐既有 shell 感知解析。"""
    targets = []
    i, n = 0, len(raw)
    in_sq = in_dq = False
    while i < n:
        ch = raw[i]
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif in_sq or in_dq:
            pass
        elif ch == ">":
            m = re.match(r">>?\s*([^\s;&|<>]+)", raw[i:])
            if m:
                targets.append(m.group(1).strip("\"'"))
                i += m.end()
                continue
        i += 1
    return targets


def _main_ws_path_hit(p, roots, base):
    """写目标路径是否落在主工作区根内：相对路径以 base（会话 workdir=worktree）解析，
    绝对路径 resolve（软链 → 真实路径）后比对；路径异常无法判定 → fail-closed 视为命中"""
    p = os.path.expanduser(str(p).strip())
    if not p or p == "-":
        return False
    try:
        pp = Path(p)
        if not pp.is_absolute():
            pp = base / pp
        pp = pp.resolve()
    except Exception:
        return True
    return any(pp == r or r in pp.parents for r in roots)


def _git_c_write_hit(args, roots, base):
    """git -C <路径> + 写子命令（双条件同时满足才拦）；无 -C（目标=当前目录=worktree）与
    只读子命令（git -C <主工作区> log/status/diff 只读诊断）放行。"""
    toks = _bare_tokens(args)
    if toks is None:
        return "git 参数解析失败（引号不平衡），保守拦截"
    c_paths = []
    sub = None
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t == "-C":
            if i + 1 >= n:
                return "git -C 缺路径参数，保守拦截"
            c_paths.append(toks[i + 1])
            i += 2
            continue
        if t in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        sub = t
        break
    if sub is None or sub not in GIT_WRITE_SUBCMDS or not c_paths:
        return None
    for p in c_paths:
        if _main_ws_path_hit(p, roots, base):
            return f"git -C 写子命令 {sub} 目标落在主工作区: {p[:100]}"
    return None


# #344 解析失败降级判定用的写动词字样（对齐 #336 护栏动词范围：重定向/tee/git/rm/mv/chmod）
_LOOSE_WRITE_HINT_RE = re.compile(r">>?|\b(?:rm|mv|chmod|tee|git)\b")


def _main_ws_loose_hit(text, roots):
    """#344 解析失败降级：不再一刀切拦截——仅当文本同时命中写动词字样与主工作区
    路径字样才拦（保守语义保留：真实越界写在解析失败时仍拦得住）；
    无路径字样（如 resume 审批文本拼接的含撇号描述段）或纯读命令放行。"""
    if not _LOOSE_WRITE_HINT_RE.search(text):
        return None
    for r in roots:
        if str(r) in text:
            return "命令解析失败（引号/heredoc 不完整）且含写操作与主工作区路径字样，保守拦截"
    return None


def main_ws_write_hit(command, main_roots, base=None):
    """#336 写角色（dev/accept）Bash 护栏：解析命令的写操作目标，目标落在主工作区根内
    返回原因字符串，否则 None。
    - main_roots 为 None/空 → 护栏未启用（向后兼容：未注入清单的调用方/桩行为不变）；
    - base = 命令实际执行目录（会话 workdir=worktree），相对路径以此 resolve；
    - 结构解析失败（引号不平衡/heredoc 未终止）→ #344 降级为「写动词字样+主工作区
      路径字样共现才拦」（_main_ws_loose_hit），不再一律保守拦截。"""
    if not main_roots:
        return None
    if not isinstance(command, str) or not command.strip():
        return None
    roots = [Path(r).resolve() for r in main_roots]
    base = Path(base).resolve() if base is not None else Path.cwd()
    units, _records = _split_exec_units(command)
    if units is None:
        return _main_ws_loose_hit(command, roots)
    for verb, args, raw in units:
        for t in _redirect_targets(raw):
            if _main_ws_path_hit(t, roots, base):
                return f"重定向写目标落在主工作区: {t[:100]}"
        if verb is None:
            continue
        if verb == "tee":
            toks = _bare_tokens(args)
            if toks is None:
                return "tee 参数解析失败（引号不平衡），保守拦截"
            for t in toks:
                if not t.startswith("-") and _main_ws_path_hit(t, roots, base):
                    return f"tee 写目标落在主工作区: {t[:100]}"
        elif verb == "git":
            hit = _git_c_write_hit(args, roots, base)
            if hit:
                return hit
        elif verb in _MAIN_WS_PATH_VERBS:
            toks = _bare_tokens(args)
            if toks is None:
                return f"{verb} 参数解析失败（引号不平衡），保守拦截"
            skip_mode = verb == "chmod"   # chmod 首个非选项 token 是模式（755/u+x），非路径
            for t in toks:
                if t.startswith("-"):
                    continue
                if skip_mode:
                    skip_mode = False
                    continue
                if _main_ws_path_hit(t, roots, base):
                    return f"{verb} 目标落在主工作区: {t[:100]}"
    return None


def dbq_command_safe(text):
    """只读数据库查询命令白名单（shell 感知）：仅允许
    `python3 <DBQ_SCRIPT> <db路径> "<只读SQL>"` 形式。
    带引号内容按 shell 语义放行（双引号内仅禁止 $、反引号、\\、换行），
    引号外禁止一切命令拼接/重定向/变量展开/通配符。SQL 本身由 dbq.py 在
    SQLite mode=ro 下执行，任何写操作都会被数据库拒绝。"""
    prefix = f"python3 {DBQ_SCRIPT} "
    if not text.startswith(prefix):
        return False
    rest = text[len(prefix):]
    if not rest.strip():
        return False
    in_sq = in_dq = False
    for ch in rest:
        if in_dq:
            if ch == '"':
                in_dq = False
            elif ch in "$`\\\n":
                return False
        elif in_sq:
            if ch == "'":
                in_sq = False
        else:
            if ch == '"':
                in_dq = True
            elif ch == "'":
                in_sq = True
            elif ch in ";&|<>*(){}`$\\\n":
                return False
    return not (in_sq or in_dq)


def _split_shell(text):
    """引号感知切分 shell 拼接命令：按引号外的 `;`/`|` 切成段（供逐段白名单校验）。
    引号内 `;|` 不切（`cat "a;b"` 为单段）；引号未闭合或产生空段（`ls;;ls`/`ls |`/`| ls`）
    返回 None（不合法）。其余非法字符（`<>*(){}` 等）由每段再跑 _shell_tokens 兜底拒绝。"""
    segs, cur, in_sq, in_dq = [], "", False, False
    for ch in text:
        if in_dq:
            cur += ch
            if ch == '"':
                in_dq = False
            elif ch in "$`\\\n":
                return None
        elif in_sq:
            cur += ch
            if ch == "'":
                in_sq = False
        else:
            if ch in ";|":
                segs.append(cur)
                cur = ""
            elif ch == '"':
                in_dq = True
                cur += ch
            elif ch == "'":
                in_sq = True
                cur += ch
            else:
                cur += ch
    if in_sq or in_dq:
        return None
    if cur or segs:
        segs.append(cur)
    if any(not seg.strip() for seg in segs):
        return None
    return segs


def _shell_tokens(text):
    """按 shell 引号规则切词，同时拒绝引号外的命令拼接/重定向/展开/通配符。
    返回 (tokens, ok)；tokens 为 [{"value": str, "quoted": bool}, ...]。
    双引号内只禁止 $、反引号、\\、换行；单引号内全字面量；引号未闭合视为不合法。"""
    tokens, cur, quoted = [], "", False
    in_sq = in_dq = False
    for ch in text:
        if in_dq:
            if ch == '"':
                in_dq = False
                quoted = True
            elif ch in "$`\\\n":
                return [], False
            else:
                cur += ch
        elif in_sq:
            if ch == "'":
                in_sq = False
                quoted = True
            else:
                cur += ch
        else:
            if ch in " \t":
                if cur:
                    tokens.append({"value": cur, "quoted": quoted})
                    cur, quoted = "", False
            elif ch == '"':
                in_dq = True
                quoted = True
            elif ch == "'":
                in_sq = True
                quoted = True
            elif ch in ";&|<>*(){}`$\\\n~!":
                return [], False
            else:
                cur += ch
    if in_sq or in_dq:
        return [], False
    if cur or quoted:
        tokens.append({"value": cur, "quoted": quoted})
    return tokens, True


def strip_null_redirects(text):
    """引号感知剥离只读命令中的「丢弃输出」型重定向（readonly_cmd_safe 入口专用）。

    仅剥离 `[1|2]>/dev/null` 与 `2>&1`（操作符两侧允许空格），剥离后其余文本照常走
    _shell_tokens 校验；`> file`/`>>`/`&>`/`2>/tmp/x` 等写入型或未知重定向不剥离，
    仍由 token 层判非法（fail-closed，写边界不放松）。
    引号内同形文本不剥离（照常作为参数/路径参与 roots 校验）；双引号内
    `$`/反引号/反斜杠/换行、引号未闭合 → 返回 None（与 _shell_tokens 同口径）。
    """
    out, i, n = [], 0, len(text)
    in_sq = in_dq = False
    while i < n:
        ch = text[i]
        if in_dq:
            if ch == '"':
                in_dq = False
            elif ch in "$`\\\n":
                return None
            out.append(ch)
            i += 1
            continue
        if in_sq:
            if ch == "'":
                in_sq = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dq = True
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            in_sq = True
            out.append(ch)
            i += 1
            continue
        # 只在整个重定向词的开头（行首或空白后）识别，避免把 `foo2>/dev/null` 之类别名/文件名误剥离
        if i == 0 or text[i - 1] in " \t":
            m = NULL_REDIRECT_RE.match(text, i)
            # `2>&1` 仅在显式 fd 2 时放行（裸 `>&1` 是「重定向两个流」的旧形态，不剥离）
            if m and (m.group(2) == "/dev/null" or m.group(1) == "2"):
                out.append(" ")
                i = m.end()
                continue
        out.append(ch)
        i += 1
    if in_sq or in_dq:
        return None
    return "".join(out)


def readonly_cmd_safe(text, roots, base=None, allow_stdin=False):
    """只读检查命令白名单（read-only 角色专用）：
    ls / cat / head / tail / wc / file / stat / find / date / echo / pwd
    （#119 新增 date；echo/pwd 为 #55 实测补入，无文件副作用），
    路径参数必须落在 roots（工作目录/产物目录）内。
    - 所有参数经 shell 引号感知切词，引号外禁拼接/写入型重定向/展开/通配符/`~`；
      `>/dev/null`/`1>/dev/null`/`2>/dev/null`/`2>&1` 丢弃型重定向先经
      strip_null_redirects 剥离，其余重定向仍拒；
    - 相对路径以 base（= 会话 workdir，命令实际执行目录）为基准 resolve 后判定（#119：
      修复前按进程 cwd resolve，agent 的相对路径命令在调度器进程 cwd 下全部误判越权）；
      base 为 None 时用进程 cwd（向后兼容）；
    - find 禁用 -exec/-execdir/-ok/-okdir/-delete 及 -fprint* 写文件；
    - head/tail 禁 stdin 空参数（防挂起），tail 禁 -f（跟随会阻塞）；
    - allow_stdin=True（管道右侧段）：head/tail/wc 无路径合法（stdin 来自左侧白名单命令
      输出，读完即退不挂起）；cat 等维持需路径。
    写操作/任意代码（python3 -c 等）不在白名单，一律拒绝。"""
    text = strip_null_redirects(text)
    if text is None:
        return False
    tokens, ok = _shell_tokens(text)
    if not ok or not tokens:
        return False
    cmd, args = tokens[0]["value"], tokens[1:]
    if cmd not in READONLY_CMDS:
        return False
    # echo：纯输出命令，参数只是文本（写入型重定向已被 strip_null_redirects 前的 token 层拒绝），
    # 不做路径/flag 校验；白名单内其余命令的参数仍逐 token 校验。
    if cmd == "echo":
        return True
    roots = [Path(r).resolve() for r in roots]
    base = Path(base).resolve() if base is not None else Path.cwd()

    def in_roots(p):
        p = Path(p)
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
        return any(p == r or r in p.parents for r in roots)

    def flag_ok(tok):
        return tok == "--" or bool(READONLY_FLAG_RE.fullmatch(tok))

    if cmd == "find":
        seen_path = False
        i = 0
        while i < len(args):
            tok = args[i]["value"]
            if not seen_path and not tok.startswith("-"):
                if not in_roots(tok):
                    return False
                seen_path = True
            elif tok in FIND_BANNED or tok.startswith("-fprint"):
                return False
            elif tok == "-newer" and i + 1 < len(args):
                if not in_roots(args[i + 1]["value"]):
                    return False
                i += 1
            i += 1
        return True

    need_path = cmd not in ("ls", "date", "pwd")
    has_path = False
    i = 0
    while i < len(args):
        tok = args[i]["value"]
        if cmd in ("head", "tail"):
            if tok in ("-n", "-c"):
                if i + 1 >= len(args) or not NUM_RE.fullmatch(args[i + 1]["value"]):
                    return False
                i += 1
            elif tok.startswith("-"):
                if tok != "--" and not re.fullmatch(r"-[A-Za-z]*\d*[kKmM]?", tok):
                    return False
                if cmd == "tail" and "f" in re.sub(r"\d+[kKmM]?$", "", tok.lstrip("-")):
                    return False
            else:
                if not in_roots(tok):
                    return False
                has_path = True
        elif cmd == "stat":
            if tok.startswith("-"):
                if not flag_ok(tok):
                    return False
                if tok in ("-f", "-t") and i + 1 < len(args):
                    i += 1  # 格式字符串不是路径
            else:
                if not in_roots(tok):
                    return False
                has_path = True
        else:
            if tok.startswith("-"):
                if not flag_ok(tok):
                    return False
                if cmd == "file" and "C" in tok.lstrip("-"):
                    return False
            else:
                if not in_roots(tok):
                    return False
                has_path = True
        i += 1
    if need_path and not has_path:
        # 无路径 = stdin 模式：仅管道右侧段（allow_stdin）放行 head/tail/wc（读完即退），其余拒绝
        return allow_stdin and cmd in ("head", "tail", "wc")
    return True


def readonly_pipeline_safe(text, roots, base=None):
    """只读检查命令拼接白名单：`;`/`|` 切段后逐段复用 readonly_cmd_safe，
    任何一段不合法整体拒绝（fail-closed 不放松）；单段时与 readonly_cmd_safe 等价。
    管道右侧段（i>0）允许 head/tail/wc 无路径从 stdin 读（左侧为白名单命令输出，不挂起）。"""
    segs = _split_shell(text)
    if not segs:
        return False
    for i, seg in enumerate(segs):
        if not readonly_cmd_safe(seg, roots, base=base, allow_stdin=(i > 0)):
            return False
    return True


def _next_top_level_and(text, start):
    """找 start 起首个引号外 `&&` 的下标（无则 -1）；引号未闭合时永不返回（调用方按畸形整体拒）。"""
    in_sq = in_dq = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_dq:
            if ch == '"':
                in_dq = False
        elif in_sq:
            if ch == "'":
                in_sq = False
        elif ch == '"':
            in_dq = True
        elif ch == "'":
            in_sq = True
        elif ch == "&" and text.startswith("&&", i):
            return i
    return -1


def strip_cd_prefix(text, roots, base=None):
    """剥离 kimi 惯用前缀「开头连续 `cd <路径> && `」（#423）。

    kimi 每次 Bash 都以 `cd '<会话 cwd>' && <命令>` 下发（实测 2699/2699 条 terminal/create），
    只读白名单出口不剥离则 100% 被拒（`_shell_tokens` 对引号外 `&&` 直接判非法，含裸 `date`）。
    - 仅剥离开头连续 cd 段，每段必须是 `cd <单一路径>`（支持 `'…'` / `"…"` / 裸路径；
      引号感知切段，引号内 `&&` 不切）；
    - 每个 cd 目标按当前生效 base resolve（首段相对 base，其后相对前一段目标——与 shell 语义一致），
      resolve 后必须落在 roots 内（含 `..`/软链展开），任一段越界即失败；
    - 返回三态：无 cd 前缀 → (text, base) 原样返回（既有行为零变化）；
      剥离成功 → (rest, eff_base)，eff_base = 最后一段 cd 目标（剩余部分相对路径的解析基准）；
      出现 cd 前缀但语法畸形 / 任一段越界 / 剩余为空（仅 cd）→ None
      （fail-closed：调用方整体拒，不回落原文）。
    """
    roots = [Path(r).resolve() for r in roots]
    base = Path(base).resolve() if base is not None else Path.cwd()
    pos, eff, stripped = 0, base, False
    while True:
        j = pos
        while j < len(text) and text[j] in " \t":
            j += 1
        if not text.startswith("cd", j):
            break
        k = j + 2
        if k >= len(text) or text[k] not in " \t":
            break  # `cdrom ...` 等非 cd 命令：不算前缀，原样返回
        end = _next_top_level_and(text, k)
        seg = text[j:] if end < 0 else text[j:end]
        toks, ok = _shell_tokens(seg)
        if not ok or len(toks) != 2 or toks[0]["value"] != "cd" or not toks[1]["value"]:
            return None
        if not _git_path_in_roots(toks[1]["value"], roots, eff):
            return None
        target = Path(toks[1]["value"])
        eff, stripped = (target if target.is_absolute() else eff / target).resolve(), True
        if end < 0:
            pos = len(text)
            break
        pos = end + 2
    if not stripped:
        return text, base
    rest = text[pos:].strip()
    if not rest:
        return None
    return rest, eff


# ---- git 白名单（#302 需求对话专用：放开 add/commit 本地提交，仍拒绝改写历史/触达远程） ----
# 只读子命令：参数多为 commit-ish/pattern（非路径），不校验；仅拒绝写文件类 flag
GIT_RO_SUBCMDS = ("status", "log", "diff", "show", "ls-files", "check-ignore", "grep", "blame", "rev-parse")
# add 允许的 flag（-f/--force 强制加入忽略文件、交互/补丁形态一律拒绝）
GIT_ADD_OK_FLAGS = ("-A", "--all", "-u", "--update", "-v", "--verbose", "-n", "--dry-run")
# commit 允许的 flag（--amend/-F/-C/--no-edit/-S/--author 等改写/注入形态一律拒绝）
GIT_COMMIT_OK_FLAGS = ("-a", "--all", "-q", "--quiet", "--no-verify", "--allow-empty", "-s", "--signoff")


def _git_path_in_roots(p, roots, base):
    p = Path(p)
    if not p.is_absolute():
        p = base / p
    p = p.resolve()
    return any(p == r or r in p.parents for r in roots)


# ---- #387 需求助手受限 python 自验白名单（仅 py_compile 与 venv unittest discover 两形态） ----
# unittest discover 白名单 flag（逐 token 精确匹配；其余 flag/位置参数/`=` 附着形态一律拒绝）
_UNITTEST_DISCOVER_NOARG_FLAGS = ("-h", "--help", "-v", "--verbose", "-q", "--quiet",
                                  "-f", "--failfast", "-c", "--catch", "-b", "--buffer")
_UNITTEST_DISCOVER_ARG_FLAGS = ("-s", "--start-directory", "-t", "--top-level-directory",
                                "-p", "--pattern", "-k", "--test-name-pattern")


def python_safe_cmd(text, roots, base=None, compile_roots=None):
    """受限 python 自验命令白名单（#387 需求助手专用，fail-closed）：仅放行两形态——
    A) `python3 -m py_compile <compile_roots 内路径>...`（裸解释器仅限 py_compile；py_compile 的
       -h/-q/-d/-o 等 flag 一律拒）；
    B) `<root>/.venv/bin/python -m unittest discover [白名单 flag]`（解释器按字面路径判定，
       须精确等于 root/.venv/bin/python，root ∈ roots；venv 的 bin/python 是 symlink 指向系统
       解释器属正常构造，故不做 symlink 展开；.venv 目录走私 symlink（指向 roots 外）单独
       检查命中即拒）。
    `python3 -c 任意代码 / 任意 python 脚本 / -m 其它模块 / 未知 flag / 位置参数` 一律拒绝；
    引号外拼接/重定向/变量展开/通配符由 _shell_tokens 统一拒绝（token 层机制，非 #336 护栏）；
    相对路径以 base（= 会话 workdir，命令实际执行目录）为基准 resolve 判定（fail-closed）。
    compile_roots（#423，可选）：形态 A 的目标落点根集，缺省回退 roots——py_compile 会在目标
    旁写 __pycache__，评审角色须收窄到 worktree/产物目录（不吃主工作区仓库根）。"""
    tokens, ok = _shell_tokens(text)
    if not ok or not tokens:
        return False
    roots = [Path(r).resolve() for r in roots]
    compile_roots = [Path(r).resolve() for r in (compile_roots or roots)]
    base = Path(base).resolve() if base is not None else Path.cwd()
    if tokens[0]["value"] == "python3":
        # 形态 A：python3 -m py_compile <路径>...（至少 1 个路径参数；模块名精确匹配）
        if len(tokens) < 4 or tokens[1]["value"] != "-m" or tokens[2]["value"] != "py_compile":
            return False
        for tok in tokens[3:]:
            v = tok["value"]
            if v.startswith("-") or not _git_path_in_roots(v, compile_roots, base):
                return False
        return True
    # 形态 B：<root>/.venv/bin/python -m unittest discover [白名单 flag]
    ip = Path(tokens[0]["value"])
    ip = ip if ip.is_absolute() else base / ip
    ip = Path(os.path.normpath(str(ip)))
    matched = False
    for r in roots:
        if ip != Path(os.path.normpath(str(r / ".venv" / "bin" / "python"))):
            continue
        if (r / ".venv").is_symlink():
            return False
        matched = True
    if not matched:
        return False
    if len(tokens) < 4 or tokens[1]["value"] != "-m" or tokens[2]["value"] != "unittest" \
            or tokens[3]["value"] != "discover":
        return False
    i = 4
    while i < len(tokens):
        v = tokens[i]["value"]
        if v in _UNITTEST_DISCOVER_NOARG_FLAGS:
            i += 1
            continue
        if v in _UNITTEST_DISCOVER_ARG_FLAGS:
            if i + 1 >= len(tokens) or tokens[i + 1]["value"].startswith("-"):
                return False
            val = tokens[i + 1]["value"]
            if v in ("-s", "--start-directory", "-t", "--top-level-directory") \
                    and not _git_path_in_roots(val, roots, base):
                return False
            i += 2
            continue
        return False
    return True


def _git_ro_free_args_safe(sub, args):
    """纯只读子命令参数校验：仅拒绝写文件类 flag（--output*；-o 仅 ls-files 的
    --others 合法，其余子命令无 -o 语义一律拒绝），参数本身不校验。"""
    for t in args:
        v = t["value"]
        if v.startswith("--output"):
            return False
        if v == "-o" and sub != "ls-files":
            return False
    return True


def _git_branch_safe(args):
    """branch 仅允许列列表形态（无参/-a/-r/-v/-vv/--list/--show-current/--merged[分支]/
    --no-merged[分支]/--format <fmt>/--sort <key>）；创建/删除/改名/跟踪等写形态一律拒绝。"""
    i, n = 0, len(args)
    while i < n:
        tok = args[i]["value"]
        if tok in ("--merged", "--no-merged"):
            i += 1
            if i < n and not args[i]["value"].startswith("-"):
                i += 1  # 分支名参数（只读，不校验）
            continue
        if tok in ("-a", "-r", "-v", "-vv", "--list", "--show-current", "--color", "--no-color"):
            i += 1
            continue
        if tok in ("--format", "--sort"):
            if i + 1 >= n:
                return False
            i += 2
            continue
        return False
    return True


def _git_tag_safe(args):
    """tag 仅允许列列表形态（无参/-l/--list/-n[n]/--sort/--contains/--merged/--no-merged/
    --format <fmt>）；非 flag 参数（= 创建标签）仅当显式 --list/-l 时放行。"""
    has_list = False
    nonflag = 0
    i, n = 0, len(args)
    while i < n:
        tok = args[i]["value"]
        if tok in ("-l", "--list"):
            has_list = True
            i += 1
            continue
        if tok in ("--sort", "--format"):
            if i + 1 >= n:
                return False
            i += 2
            continue
        if tok in ("--contains", "--merged", "--no-merged"):
            i += 1
            continue
        if tok == "-n" or re.fullmatch(r"-n\d+", tok):
            i += 1
            continue
        if tok.startswith("-"):
            return False
        nonflag += 1
        i += 1
    return has_list or nonflag == 0


def _git_worktree_safe(args):
    """worktree 仅允许无参列列表或 list 子命令（--porcelain/-v）；
    add/remove/prune/lock/unlock/move/repair 等写形态一律拒绝。"""
    if not args:
        return True
    if args[0]["value"] != "list":
        return False
    return all(t["value"] in ("--porcelain", "-v", "--verbose") for t in args[1:])


def _git_remote_safe(args):
    """remote 仅允许无参/-v 列列表、get-url <name>、show <name>（只读）；
    add/remove/set-url/rename/update/prune 等写形态一律拒绝。"""
    if not args:
        return True
    first = args[0]["value"]
    if first in ("-v", "--verbose"):
        return len(args) == 1
    if first in ("get-url", "show"):
        return len(args) == 2
    return False


def _git_config_safe(args):
    """config 仅允许只读查询（--get/--get-regexp/--show-origin/--show-scope/--local/-l/--list，
    非 flag 参数 ≤1 = 读 key）；写配置（2 个以上非 flag 参数）与 --add/--unset/--set/
    --replace-all/--global/--system 等一律拒绝。"""
    if not args:
        return True
    flags = ("--get", "--get-regexp", "--show-origin", "--show-scope", "--local", "-l", "--list")
    n_nonflag = 0
    for t in args:
        v = t["value"]
        if v.startswith("-"):
            if v not in flags:
                return False
        else:
            n_nonflag += 1
    return n_nonflag <= 1


def _git_add_safe(args, roots, base):
    """add 仅允许 stage 工作目录内文件（-A/-u/--all/--update/-v/-n 等 flag，路径须落 roots 内）；
    -f/--force（强制加入忽略文件）与交互/补丁形态一律拒绝。无路径时必须带 -A/-u 类全量 flag。"""
    has_bulk = has_path = False
    i, n = 0, len(args)
    while i < n:
        tok = args[i]["value"]
        if tok == "--":
            i += 1
            break
        if tok in GIT_ADD_OK_FLAGS:
            if tok in ("-A", "--all", "-u", "--update"):
                has_bulk = True
            i += 1
            continue
        if tok.startswith("-"):
            return False
        if not _git_path_in_roots(tok, roots, base):
            return False
        has_path = True
        i += 1
    while i < n:
        if not _git_path_in_roots(args[i]["value"], roots, base):
            return False
        has_path = True
        i += 1
    return has_bulk or has_path


def _git_commit_safe(args, roots, base):
    """commit 仅允许本地提交（-m/-a/-am/-q/--no-verify/--allow-empty/--allow-empty-message/-s，
    路径须落 roots 内）；必须带 -m/-am/--allow-empty-message（防编辑器挂起）；
    --amend/-F/-C/--no-edit/-S/--author/--fixup 等改写/注入形态一律拒绝。"""
    has_msg = False
    i, n = 0, len(args)
    while i < n:
        tok = args[i]["value"]
        if tok == "--":
            i += 1
            break
        if tok in ("-m", "--message"):
            if i + 1 >= n:
                return False
            has_msg = True
            i += 2
            continue
        if tok in GIT_COMMIT_OK_FLAGS:
            i += 1
            continue
        if tok == "--allow-empty-message":
            has_msg = True
            i += 1
            continue
        if re.fullmatch(r"-am", tok):  # 常见组合：-a -m
            if i + 1 >= n:
                return False
            has_msg = True
            i += 2
            continue
        if tok.startswith("-"):
            return False
        if not _git_path_in_roots(tok, roots, base):
            return False
        i += 1
    while i < n:
        if not _git_path_in_roots(args[i]["value"], roots, base):
            return False
        i += 1
    return has_msg


def git_cmd_safe(text, roots, base=None, allow_merge=False):
    """git 命令白名单（#302 需求对话专用）：只读子命令（status/log/diff/show/ls-files/
    check-ignore/grep/blame/rev-parse/branch 列列表/tag 列列表/worktree list/remote 列列表/
    config 只读查询）+ add/commit（路径限 roots 内）放行；push/checkout/switch/reset/rebase/
    merge/cherry-pick/revert/clean/restore/rm/mv/stash/fetch/pull/clone/init 等一律拒绝；
    -C/--git-dir/--work-tree 重定向拒绝；引号外拼接/重定向/通配符由 _shell_tokens 统一拒绝
    （fail-closed）。相对路径以 base（= 会话 workdir，命令实际执行目录）为基准 resolve 判定。
    allow_merge=True（#355 需求助手合并授权）：额外放行 merge/checkout/switch/stash 的受限形态
    （见 _git_merge_safe 等；仅切分支、禁 -b/-f/-- 路径形态、stash 禁 drop/clear）。

    支持 kimi 实战习惯的 `cd <项目目录> && git ...` 拼接形态（#302 实战反馈：agent 反复以
    cd && 开头被拒）：按 && 切段，每段必须是 `cd <roots 内路径>` 或白名单内 git 单命令；
    cd 目标 resolve 后必须落在 roots 内（最终 cwd ⊆ roots，git 段相对路径实际落点不越界；
    路径校验仍以 base=workdir 解析，放行范围不扩大）。echo/管道/分号等其它拼接一律拒绝。"""
    if "&&" in text:
        segs = [s.strip() for s in text.split("&&")]
        if not segs or any(not s for s in segs):
            return False
        roots = [Path(r).resolve() for r in roots]
        base = Path(base).resolve() if base is not None else Path.cwd()
        for seg in segs:
            if seg.startswith("cd "):
                toks, ok = _shell_tokens(seg)
                if not ok or len(toks) != 2 or toks[0]["value"] != "cd":
                    return False
                if not _git_path_in_roots(toks[1]["value"], roots, base):
                    return False
                continue
            if not _git_single_safe(seg, roots, base, allow_merge):
                return False
        return True
    return _git_single_safe(text, roots, base, allow_merge)


def _git_single_safe(text, roots, base, allow_merge=False):
    """单条 git 命令校验（git_cmd_safe 的非拼接形态主体；不含 &&/;/| 处理）"""
    tokens, ok = _shell_tokens(text)
    if not ok or not tokens:
        return False
    if tokens[0]["value"] != "git":
        return False
    i = 1
    # 全局选项：仅 --version/--help；-C/--git-dir/--work-tree 等重定向一律拒绝
    saw_global = False
    while i < len(tokens) and tokens[i]["value"].startswith("-"):
        if tokens[i]["value"] not in ("--version", "--help"):
            return False
        saw_global = True
        i += 1
    if i >= len(tokens):
        return saw_global  # 仅全局选项（git --version/--help）放行；裸 git 拒绝
    sub = tokens[i]["value"]
    args = tokens[i + 1:]
    roots = [Path(r).resolve() for r in roots]
    base = Path(base).resolve() if base is not None else Path.cwd()
    if sub in GIT_RO_SUBCMDS:
        return _git_ro_free_args_safe(sub, args)
    if sub == "branch":
        return _git_branch_safe(args)
    if sub == "tag":
        return _git_tag_safe(args)
    if sub == "worktree":
        return _git_worktree_safe(args)
    if sub == "remote":
        return _git_remote_safe(args)
    if sub == "config":
        return _git_config_safe(args)
    if sub == "add":
        return _git_add_safe(args, roots, base)
    if sub == "commit":
        return _git_commit_safe(args, roots, base)
    # #355 需求助手合并授权（allow_merge=True 时放开受限形态；默认一律拒绝）
    if allow_merge:
        if sub == "merge":
            return _git_merge_safe(args)
        if sub == "checkout":
            return _git_checkout_safe(args)
        if sub == "switch":
            return _git_switch_safe(args)
        if sub == "stash":
            return _git_stash_safe(args)
    return False


def _git_merge_safe(args):
    """merge 合并（#355 需求助手合并授权）：放行 --no-ff/--ff/--ff-only/--no-edit/--no-commit/
    --abort/--continue + 至多一个 ref 参数；无 ref 时必须是 --abort/--continue（裸 `git merge`
    或纯 flag 会开编辑器/悬空，拒绝）；-s/-X 策略、octopus 多 ref、-m 等一律拒绝"""
    refs = 0
    for a in args:
        v = a["value"]
        if v.startswith("-"):
            if v not in ("--no-ff", "--ff", "--ff-only", "--no-edit", "--no-commit",
                         "--abort", "--continue"):
                return False
        else:
            refs += 1
            if refs > 1:
                return False
    if refs > 0:
        return True
    return any(a["value"] in ("--abort", "--continue") for a in args)


def _git_checkout_safe(args):
    """checkout 仅切分支形态（#355 需求助手合并授权）：恰好一个 ref 参数（分支名/commit，
    不以 - 开头）；-b/-B/-f/--detach/--orphan/-q 等 flag 与 `-- <path>` 路径形态（覆盖/丢弃
    工作区文件）一律拒绝"""
    if len(args) != 1:
        return False
    return not args[0]["value"].startswith("-")


def _git_switch_safe(args):
    """switch 仅切分支形态（#355 需求助手合并授权）：恰好一个 ref 参数（分支名/commit，
    不以 - 开头）；-c/-C/-f/--detach/--discard-changes/--orphan 等一律拒绝"""
    if len(args) != 1:
        return False
    return not args[0]["value"].startswith("-")


def _git_stash_safe(args):
    """stash 暂存/恢复（#355 需求助手合并授权）：无参（=push）/list/show/pop/apply/
    push -m <消息> 放行；drop/clear/create/branch/apply-patch/store 等（丢弃/改动 stash 内容）
    一律拒绝；stash@{n} 引用含花括号被 _shell_tokens 拒（fail-closed：pop/apply 仅作用于
    栈顶 stash@{0}）"""
    n = len(args)
    if n == 0:
        return True
    sub = args[0]["value"]
    if sub in ("list", "show", "pop", "apply"):
        return n == 1
    if sub == "push":
        if n == 1:
            return True
        return n == 3 and args[1]["value"] == "-m"
    return False
