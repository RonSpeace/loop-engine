"""提示词语言（board 与 dispatcher 共用）：发给 agent 的文案按项目选择中文或英文。

- 语言来源：projects.json 项目级 `prompt_language`（"zh" / "en"）→ 环境变量 `LOOP_PROMPT_LANG` → "zh"。
  非法值按 "zh" 处理并记日志（语言不涉及安全边界，不 fail-closed）。
- 文件类文案（角色模板、system/、fragments/、全局技能）：英文版在 `prompts/en/<同路径>`，见 prompt_store。
- 代码里拼进提示词的短句：`tr(lang, 中文模板, **值)`——中文模板本身就是键，"zh" 原样 format（与改造前
  f-string 逐字一致），"en" 查下方 EN 表；缺译文时退回中文并告警（tests/test_prompt_i18n.py 会拦住）。
- 角色 / 流程展示名与流程说明：`display(lang, 文本)` 复用界面英文词典 scripts/i18n_en_source.py 的整句精确命中，
  查不到（用户自定义名称）原样返回。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

LANGS = ("zh", "en")
DEFAULT_LANG = "zh"
REPO_DIR = Path(__file__).resolve().parent


def _norm(value):
    v = str(value or "").strip().lower()
    if v.startswith("zh"):
        return "zh"
    if v.startswith("en"):
        return "en"
    return None


def env_lang():
    raw = os.environ.get("LOOP_PROMPT_LANG", "")
    lang = _norm(raw)
    if raw.strip() and lang is None:
        log.warning("LOOP_PROMPT_LANG=%r 非法（只支持 zh / en），按 zh 处理", raw)
    return lang or DEFAULT_LANG


def _projects_file():
    return Path(os.environ.get("REQ_BOARD_PROJECTS") or (REPO_DIR / "projects.json"))


def prompt_lang(project=None):
    """项目的提示词语言（每次现读 projects.json，改完无需重启）。"""
    if project:
        try:
            raw = json.loads(_projects_file().read_text(encoding="utf-8"))
        except Exception:
            raw = None
        entry = raw.get(str(project)) if isinstance(raw, dict) else None
        val = entry.get("prompt_language") if isinstance(entry, dict) else None
        if val not in (None, ""):
            lang = _norm(val)
            if lang:
                return lang
            log.warning("项目 %s 的 prompt_language=%r 非法（只支持 zh / en），按全局设置处理", project, val)
    return env_lang()


# ---------------- 代码内短句 ----------------
# 键 = 中文模板（{name} 为 str.format 占位符）；值 = 英文模板，占位符集合必须一致。
EN = {
    # dispatcher_modules/git_sync.dev_repo_listing
    "工作目录：{wd}（独立 worktree，分支 {branch}）": "Working directory: {wd} (dedicated worktree, branch {branch})",
    "工作目录：{wd}（dev 聚合目录，非 git 仓库；各仓库 worktree 子目录），分支 {branch}":
        "Working directory: {wd} (dev aggregate directory, not a git repository; one worktree subdirectory per repository), branch {branch}",
    "仓库清单（子目录 -> worktree）：": "Repositories (subdirectory -> worktree):",
    "（本次相关）": " (relevant to this requirement)",
    "- {path} -> {wt}{mark}": "- {path} -> {wt}{mark}",
    "说明：工作目录包含全部 {total} 个仓库的 worktree；本次需求相关仓库 {focus} 个（analysis/notes 识别，指引用）。清单外仓库确有需要也可直接修改，完成后系统按实际改动识别涉及仓库。":
        "Note: the working directory holds worktrees for all {total} repositories; {focus} of them are relevant to this requirement (identified from analysis/notes, for guidance only). You may change other repositories if really needed; after you finish, the system detects the involved repositories from the actual changes.",
    # dispatcher_modules/flow_engine
    "收尾": "Finalize",
    "（未说明适用场景）": "(no usage note)",
    "- {id}（{name}）｜{desc}｜节点：{seq}": "- {id} ({name}) | {desc} | nodes: {seq}",
    "需求本身（title + notes）": "the requirement itself (title + notes)",
    "{name}产物 {path}": "{name} artifact {path}",
    "安装依赖": "install", "全量测试": "full test suite", "受影响测试": "affected tests", "lint": "lint",
    "类型检查": "type check", "构建": "build",
    "工作目录 {name}/AGENTS.md": "{name}/AGENTS.md in the working directory",
    "工作目录仓库根的 AGENTS.md": "AGENTS.md at the repository root of the working directory",
    "项目约定（{project}，来源 {path}；命令、端口、路径等项目事实与通用说明不一致时以此为准，权限与结果契约仍以上方说明为准）：":
        "Project conventions ({project}, from {path}; for project facts such as commands, ports and paths these win over the general instructions where they differ; permissions and the result contract still follow the instructions above):",
    "- 常用命令：": "- Common commands: ",
    "；": "; ",
    "{label} `{cmd}`": "{label} `{cmd}`",
    "启动": "start", "停止": "stop", "状态": "status", "健康检查": "health check",
    "端口段 {lo}-{hi}": "port range {lo}-{hi}",
    "- 测试实例：": "- Test instance: ",
    "｜": " | ",
    "测试实例": "test instance",
    "- 未配置：": "- Not configured: ",
    "、": ", ",
    "（需要时按仓库 AGENTS.md 约定；没有约定就如实说明，不要假设存在对应脚本或端口）":
        " (follow the repository's AGENTS.md when needed; if it says nothing, state that plainly; do not assume such a script or port exists)",
    "【项目指引：{name}】\n{text}": "[Project guide: {name}]\n{text}",
    "【场景技能：{name}】\n{text}": "[Scenario skill: {name}]\n{text}",
    "\n【可用技能目录】以下技能未内联正文，需要时用读文件工具读取对应路径：":
        "\n[Skill catalog] These skills are not inlined; when you need one, read its file with the file-reading tool:",
    "（无描述）": "(no description)",
    "- {name}：{desc}（{path}）": "- {name}: {desc} ({path})",
    "- 改 {dir}/ 下的文件前先读 {show}（依据：{why}）": "- Before changing files under {dir}/, read {show} (reason: {why})",
    "diff 改动 {path}": "changed in diff: {path}",
    "需求 notes 提到 {path}": "mentioned in requirement notes: {path}",
    "{label}列出 {path}": "listed in {label}: {path}",
    "需求分析": "Analysis", "产品方案": "Product spec", "技术方案": "Tech design",
    "- {name}（主干: {ref}）：git merge {ref}": "- {name} (main branch: {ref}): git merge {ref}",
    "仓库地图（跨仓库定位代码的第一来源，先读再分析）：{path}\n":
        "Repository map (first source for locating code across repositories; read it before analysing): {path}\n",
    "项目目标（goal）：{goal}\n": "Project goal: {goal}\n",
    "开发总结（评审反馈/历史记录）：\n{summary}\n": "Development summary (review feedback / history):\n{summary}\n",
    "无附加反馈（评审通过或尚无否决记录）。": "No additional feedback (review passed or nothing rejected yet).",
    "历史记录（前几次尝试的总结/评审反馈）：{summary}\n": "History (summaries / review feedback of earlier attempts): {summary}\n",
    "（无）": "(none)",
    "补充上下文：{extra}\n": "Additional context: {extra}\n",
    "作者回应（上一轮问题的逐条处理，{path}）：\n{data}\n":
        "Author responses (item-by-item handling of last round's issues, {path}):\n{data}\n",
    # dispatcher_modules/agent_run
    "约 200": "about 200",
    "本轮工具调用达上限": "this round's tool-call limit was reached",
    "本轮工具调用达 {n} 步上限": "this round reached the {n}-step tool-call limit",
    "\n\n用户消息：{text}": "\n\nUser message: {text}",
    "\n\n用户消息：\n": "\n\nUser message:\n",
    # dispatcher_modules/flow_advance._review_rework_extra
    " {where}：": " {where}: ",
    "（第 {n} 轮）": " (round {n})",
    "未说明原因": "no reason given",
    "⚠️「{name}」评审未通过，已打回本节点复修{tag}。\n评审意见：{reason}":
        "⚠️ \"{name}\" review did not pass; this node was sent back for rework{tag}.\nReview comments: {reason}",
    "\n……（产物超长已截断，完整内容见 {path}）": "\n... (artifact too long, truncated; full text at {path})",
    "\n\n评审产物（{art}）全文：\n{text}": "\n\nFull review artifact ({art}):\n{text}",
    "上次尝试失败（第 {n} 次）：{reason}。{hint}": "Previous attempt failed (attempt {n}): {reason}. {hint}",
    "上次失败为 API 并发限流（403）：并发位被占，本次已延迟重试等待并发位释放；若再遇 403 不要立即重发请求。":
        "The last failure was API concurrency throttling (403): all concurrency slots were taken, so this retry was delayed until a slot frees up. If you hit 403 again, do not resend the request immediately.",
    "上次会话因命中进程安全红线被拦截取消：先读被拦截命令自查（看板执行过程/agent_events），测试实例清理必须使用项目约定的实例脚本（PID 文件 + 精确 PID/后代清理），禁止自行编写 trap/kill/killpg/pkill 或清理 ACP 驱动进程/进程组命令。":
        "The last session was cancelled for crossing the process-safety red line: first read the blocked command and check yourself (board execution log / agent_events). Clean up test instances only with the project's instance script (PID file + exact PID / descendant cleanup); never write your own trap/kill/killpg/pkill commands or commands that clean up the ACP driver process or its process group.",
    "上次结果文件非法 JSON，原文件已改名保留在 {backup}，先读取自查避免重蹈覆辙；写 result.json 请用 python -c 'json.dumps(...)' 或脚本生成，summary 内换行必须转义为 \\n。":
        "The last result file was invalid JSON; the original was renamed and kept at {backup}. Read it first so you do not repeat the mistake. Generate result.json with python -c 'json.dumps(...)' or a script; newlines inside summary must be escaped as \\n.",
    "上次会话工具步数超预算被截停：先读 summary 与产物确认已有进度，缩小探索范围、直奔改动点，接近上限前先写 result.json 保底。":
        "The last session was stopped for exceeding its tool-step budget: first read the summary and artifacts to confirm existing progress, narrow your exploration and go straight to the change, and write a fallback result.json before nearing the limit.",
    "上次会话因输出复读/单次请求超长被输出守卫中止：避免重复输出同一段文本，若再次复读，自动重试耗尽后将转人工处理。":
        "The last session was aborted by the output guard for repeating itself or sending an oversized request: do not output the same text repeatedly. If it happens again, the card goes to a human once automatic retries run out.",
    "上次进程正常退出但未提交结果：先确认改动完好（git status / 产物目录）再前台跑验证确认无误，最后用 python json.dumps 补写 result.json（status/summary 必填）。":
        "The last process exited normally but produced no result: first confirm the changes are intact (git status / artifact directory), run the verification in the foreground, then write result.json with python json.dumps (status and summary are required).",
    "本次已改用全局兜底模型重试（该角色配置的模型运行期不可用）；若仍失败将按现有路径转人工处理。":
        " This retry uses the global fallback model (the model configured for this role was unavailable at run time); if it fails again, the card goes to a human as usual.",
    "先确认改动完好（git status / 冲突文件是否已解决）、worktree 内冲突是否仍在，再继续解决并提交；如属语义冲突需要人工决策，直接报 need_confirm。":
        "First confirm the changes are intact (git status / whether conflicted files are resolved) and whether the conflict is still present in the worktree, then continue resolving and commit. If it is a semantic conflict that needs a human decision, report need_confirm directly.",
    "用户对之前的问题「{question}」的答复：同意。{answer}": "The user's answer to your earlier question \"{question}\": approved. {answer}",
    "注意：analysis 识别本需求涉及清单外仓库 {repos}，本项目 worktree 内没有这些仓库的代码。如确认需改这些仓库：禁止写任何真实仓库主工作区，结束回合报 need_confirm 说明「需求可能属于其它项目/项目仓库清单缺配置」等待人工处理。":
        "Note: analysis found that this requirement involves repositories outside the project's list: {repos}. This project's worktree does not contain their code. If those repositories really need changes, never write to any real repository's main working tree; end the round with need_confirm explaining \"the requirement may belong to another project / the project's repository list is missing configuration\" and wait for a human.",
    # dispatcher_modules/context.resume_plan
    "平台策略": "platform policy", "角色模板": "role template", "新增": "added", "已变更": "changed",
    "\n……（超过 {n} 字已截断，完整内容见 {src}）": "\n... (truncated after {n} characters; full text at {src})",
    "--- {tag}：{title}（{key}，{layer}）---\n{text}": "--- {tag}: {title} ({key}, {layer}) ---\n{text}",
    "--- 已移除：{title}（{key}）——不再适用，忽略此前收到的这部分 ---":
        "--- removed: {title} ({key}) -- no longer applies; ignore this part from earlier messages ---",
    # dispatcher_modules/board_api.compact_summary
    "（更早 {folded} 条记录已折叠，其中评审打回 {rejects} 次；完整历史见看板卡片开发总结）":
        "({folded} earlier entries folded, including {rejects} review rejections; the full history is in the card's development summary on the board)",
}


def tr(lang, zh, **values):
    """按语言取短句并 format；"zh" 与改造前的 f-string 逐字一致。"""
    tmpl = zh
    if lang == "en":
        tmpl = EN.get(zh)
        if tmpl is None:
            log.warning("提示词短句缺英文译文，按中文发送: %r", zh[:60])
            tmpl = zh
    return tmpl.format(**values) if values else tmpl


# ---------------- 展示名（角色 / 流程 / 流程说明） ----------------
_phrases = None


def _ui_phrases():
    global _phrases
    if _phrases is None:
        try:
            spec = importlib.util.spec_from_file_location("_loop_i18n_en_source",
                                                          REPO_DIR / "scripts" / "i18n_en_source.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _phrases = dict(getattr(mod, "PHRASES", {}) or {})
        except Exception as e:     # 词典缺失只影响展示名译文，不阻断
            log.warning("界面英文词典加载失败，展示名按原文: %s", e)
            _phrases = {}
    return _phrases


def display(lang, text):
    """展示名 / 流程说明的英文：整句精确命中界面词典，否则原样。"""
    if lang != "en" or not text:
        return text
    return _ui_phrases().get(str(text), text)
