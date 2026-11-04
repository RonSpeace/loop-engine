#!/usr/bin/env python3
"""
在需求看板（req-board）上为当前项目创建需求卡。

用法:
    python3 create_card.py --title "标题" [--project 项目名] [--status later|open|done] [--notes "备注"]
                          [--source dsh|codex|kimi-code|...] [--model 模型名] [--reasoning-effort low|high|max]
                          [--user-request "用户明确要求开发的原话"]

    #350：kimi-code 环境不传 --source/--model 也会自动探测（读 KIMI_CONFIG 的
    default_model.display_name），显式传参仍逐字段优先。

环境变量:
    REQ_BOARD_URL  看板地址，默认 http://127.0.0.1:8899（worktree 测试实例可覆盖）
    DSH_SESSION_JSONL   DeepSeek Harness 会话 JSONL 路径（#303）：存在时自动解析 request/header
                        事件的 config.provider/model/reasoningEffort，上报 source=dsh + 模型 +
                        推理强度 + source_session_id（DSH_SESSION_ID）；支持 zstd 压缩（.zstd
                        后缀自动解压，DSH 实际落盘为 session.jsonl.zstd）；无 DSH 变量、无显式参数
                        时不携带来源元数据（行为与现状一致，不报错）
    DSH_SESSION_ID      DSH 会话 id，随 DSH_SESSION_JSONL 一并上报
    KIMI_CONFIG         kimi-code 配置路径，默认 ~/.kimi-code/config.toml（#350 自动探测模型名用）

约定（与 req-board AGENTS.md 一致）:
    - 默认 status=later（暂缓，调度器不开发）；仅用户明确要求开发时才用 open。
    - 「建卡/记到看板/走调度器流程」只定路径、不定时机（意思是别绕开调度器直接改 main），
      不构成开发指令。自动开发项目用 open 必须 --user-request 引用用户明确要求开发的原话；
      引用不出原话 = 本来就没有该指令，改用 later（建卡 #423 时的现场教训）。
    - status=done：已实现/已完成留档卡（终态，调度器不会捞），notes 建议首行带 [已实现] 声明。
    - status 始终显式传入，避免 API 缺省 open 被自动开发调度器立刻捞走。
    - 已实现的需求禁止用 open（实现已在 main 时 worktree 基线=实现提交，dev 报 done 必判
      「无代码变更」失败——#395/397/398 现场教训），除非用户明确要求开发。
"""

import argparse
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8899"
ALLOWED_STATUS = ("later", "open", "done")


def requires_user_request(status, auto_list, project):
    """status=open 时是否必须带 --user-request（用户明确要求开发的原话）。

    auto_list=None 表示无法判定项目是否接入自动开发（接口异常/返回异常）→ 按接入处理
    （fail-closed：宁可多要一次确认，不可静默把卡丢给调度器）。非自动开发项目的 open
    不会被捞走，不做要求。
    """
    if status != "open":
        return False
    return auto_list is None or project in auto_list


def _open_session_lines(path):
    """把 DSH 会话文件打开为逐行文本流（.zstd 后缀先解压）。
    优先用 Python zstandard 包流式解压；无包回退系统 zstd 命令（stdout 流式读）；
    两者都不可用或文件不可读 → 返回 (None, None)，调用方按「无会话文件」处理。
    返回 (stream, cleanup)：cleanup 为收尾函数（命令行解压流需在消费后收进程），可为 None。"""
    p = Path(path)
    if p.suffix.lower() != ".zstd":
        try:
            stream = open(path, "r", encoding="utf-8", errors="replace")
            return stream, stream.close
        except OSError:
            return None, None
    if not p.is_file():
        return None, None
    try:
        import zstandard
    except ImportError:
        zstandard = None
    if zstandard is not None:
        try:
            reader = zstandard.ZstdDecompressor().stream_reader(open(path, "rb"))
            return io.TextIOWrapper(reader, encoding="utf-8", errors="replace"), None
        except Exception:
            return None, None
    try:
        proc = subprocess.Popen(["zstd", "-dc", str(p)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None, None
    stream = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")

    def cleanup():
        try:
            stream.close()
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return stream, cleanup


def _extract_config(ev):
    """从会话事件里取 config dict，兼容 DSH 实际结构与旧假设：
    - DSH 实测：request/header 的 config 挂在 data.header.config 下；
    - 旧假设：config 直接挂在事件顶层；
    - 兜底：request/context 等事件 provider/model 直接挂在 data 下。
    取不到返回 None。"""
    cfg = ev.get("config")
    if isinstance(cfg, dict):
        return cfg
    data = ev.get("data")
    if isinstance(data, dict):
        header = data.get("header")
        if isinstance(header, dict) and isinstance(header.get("config"), dict):
            return header["config"]
        if data.get("model") or data.get("provider"):
            return data
    return None


# DSH（DeepSeek Harness）环境信号：任一存在即认定「当前进程在 DSH 会话内」。
# DSH_SESSION_JSONL 指向会话文件（可解析出模型/推理强度），DSH_SESSION_ID 是会话级变量，
# DSH_HOME 是兜底信号——实测在 DSH 里跑 bash 工具时只有 DSH_SESSION_ID/DSH_HOME/DSH_SHELL/DSH_WEB_URL，
# 会话文件路径未必注入，只认 DSH_SESSION_JSONL 会把 DSH 会话误判成 kimi-code（#350 现场）。
_DSH_ENV_SIGNALS = ("DSH_SESSION_JSONL", "DSH_SESSION_ID", "DSH_HOME")


def _dsh_env_present():
    """是否处于 DSH 环境（只看环境变量，不依赖会话文件是否可读）"""
    return any(os.environ.get(k) for k in _DSH_ENV_SIGNALS)


def detect_dsh_meta():
    """#303/#350 DeepSeek Harness 自动检测：环境命中即认定 DSH，能解析会话文件再补模型信息。

    - DSH_SESSION_JSONL / DSH_SESSION_ID / DSH_HOME 任一存在 → 在 DSH 会话内，返回
      {source:'dsh'}（带 source_session_id，若 DSH_SESSION_ID 有值）；
    - 会话文件可读时再解析 request/header 事件的 config，补 model / reasoning_effort
      （支持普通 JSONL 与 zstd 压缩，DSH 实际落盘为 session.jsonl.zstd）；
    - 只有环境信号、拿不到会话文件（未注入路径 / 不存在 / 解压或解析失败）→ **不猜模型**，
      model 留空，交由显式参数或调用方补；
    - 完全不在 DSH 环境 → 返回 None（调用方的 `if not dsh` 守卫此时才允许回退 kimi-code 探测）。
    检测为「尽力而为」：任何异常一律降级为「环境命中但无模型信息」，不抛出、不阻断建卡。"""
    env_hit = _dsh_env_present()
    sid = os.environ.get("DSH_SESSION_ID") or None
    minimal = {"source": "dsh", "source_session_id": sid}

    path = os.environ.get("DSH_SESSION_JSONL")
    if not path:
        return minimal if env_hit else None
    stream, cleanup = _open_session_lines(path)
    if stream is None:
        return minimal if env_hit else None
    try:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = str(ev.get("kind") or ev.get("type") or "")
            if "request" not in kind and "header" not in kind:
                continue
            cfg = _extract_config(ev)
            if cfg is None:
                continue
            model = cfg.get("model")
            effort = cfg.get("reasoningEffort")
            if not (model or effort or cfg.get("provider")):
                continue
            return {
                "source": "dsh",
                "model": model or None,
                "reasoning_effort": effort or None,
                "source_session_id": sid,
            }
    except Exception:
        return minimal if env_hit else None
    finally:
        if cleanup:
            try:
                cleanup()
            except Exception:
                pass
    return minimal if env_hit else None


def detect_kimi_meta():
    """#350 kimi-code 自动检测：读 ~/.kimi-code/config.toml（KIMI_CONFIG 可覆盖）的
    default_model，取其模型段的 display_name（如 deepseek/v4.1-flash →
    DeepSeek V4.1 Flash）作为 model 上报，source 固定 kimi-code。
    无配置文件 / tomllib 不可用（<3.11）/ 解析失败 / 缺字段 → 返回 None（尽力而为，不报错）。"""
    path = os.environ.get("KIMI_CONFIG", str(Path.home() / ".kimi-code" / "config.toml"))
    if not os.path.exists(path):
        return None
    try:
        import tomllib
    except ImportError:
        return None
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except Exception:
        return None
    model_key = cfg.get("default_model")
    if not model_key:
        return None
    model_cfg = (cfg.get("models") or {}).get(model_key) or {}
    name = model_cfg.get("display_name") or model_cfg.get("model") or model_key
    return {"source": "kimi-code", "model": str(name)}


def build_meta(explicit, kimi, dsh):
    """#303/#350 合并建卡来源元数据，优先级：显式参数（--source/--model/--reasoning-effort，
    逐字段独立）> kimi-code 自动探测 > DSH 自动检测；缺失字段依次向下一来源回退。

    Args:
        explicit: 显式参数 dict（{source/model/reasoning_effort}）
        kimi: kimi-code 探测结果（{source/model}），仅非 DSH 环境且显式 source 为空或为
              kimi-code 时传入，否则传 {}
        dsh: DSH 自动检测结果（{source/model/reasoning_effort/source_session_id}）

    全部缺失 → 返回空 dict（不上报任何元数据，行为与旧版一致）。"""
    meta = {}
    if explicit.get("source"):
        meta["source"] = explicit["source"]
    if explicit.get("model"):
        meta["model"] = explicit["model"]
    if explicit.get("reasoning_effort"):
        meta["reasoning_effort"] = explicit["reasoning_effort"]
    for key in ("source", "model", "reasoning_effort", "source_session_id"):
        if key not in meta and kimi.get(key):
            meta[key] = kimi[key]
    for key in ("source", "model", "reasoning_effort", "source_session_id"):
        if key not in meta and dsh.get(key):
            meta[key] = dsh[key]
    return meta


def request(url, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw
    except urllib.error.URLError as err:
        return None, str(err)


def main():
    parser = argparse.ArgumentParser(description="在 req-board 上创建需求卡")
    parser.add_argument("--title", required=True, help="需求标题（必填）")
    parser.add_argument("--project", default=Path.cwd().name,
                        help="项目名，默认当前目录名")
    parser.add_argument("--status", choices=ALLOWED_STATUS, default="later",
                        help="状态，默认 later（暂缓）；仅用户明确要求开发时用 open；done=已实现留档（终态）")
    parser.add_argument("--notes", default="", help="需求详情：背景/方案要点/验收标准/生效方式")
    parser.add_argument("--user-request", default=None,
                        help="用户明确要求开发的原话（自动开发项目用 --status open 时必填）；"
                             "引用不出原话说明未获开发指令，请改用 later")
    parser.add_argument("--source", default=None,
                        help="建卡来源（dsh/codex/kimi-code/board-chat…），显式指定覆盖自动检测（DSH/kimi-code）")
    parser.add_argument("--model", default=None,
                        help="建卡模型（如 deepseek-v4-flash），显式指定覆盖自动探测（DSH/kimi-code）；"
                             "识别不出时省略，不得编造")
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default=None,
                        help="推理强度，显式指定覆盖 DSH 自动检测")
    args = parser.parse_args()

    base = os.environ.get("REQ_BOARD_URL", DEFAULT_URL).rstrip("/")

    # 1. 看板可用性
    status, body = request(f"{base}/api/projects")
    if status != 200:
        print(f"[ERROR] 需求看板不可用（{base}）：{body}")
        sys.exit(2)
    projects = body if isinstance(body, list) else []
    if args.project not in projects:
        print(f"[WARN] 项目名 '{args.project}' 不在看板项目列表中（{projects}），仍按指定名称建卡")

    # 2. 自动开发项目 + open 硬闸（建卡 #423 时的现场教训：把「建卡/走流程」误当开发指令，卡被置 open）
    # 只 WARN 不拦属护栏而非机制保证（#336 口径），故要求引用用户明确要求开发的原话；
    # 判定不了是否接入自动开发（接口异常）时按接入处理（fail-closed）。
    status, auto = request(f"{base}/api/auto-projects")
    auto_list = auto if (status == 200 and isinstance(auto, list)) else None
    if requires_user_request(args.status, auto_list, args.project):
        if not (args.user_request or "").strip():
            why = (f"'{args.project}' 已接入自动开发" if auto_list is not None else
                   f"无法确认 '{args.project}' 是否接入自动开发（/api/auto-projects 不可用，按接入处理）")
            print(f"[ERROR] {why}，status=open 会被调度器立刻捞走开发"
                  f"（下一轮轮询即建 worktree、起 agent 会话）。\n"
                  f"        仅当用户明确要求开发时才用 open，且必须用 --user-request \"<用户原话>\" 引用该指令；\n"
                  f"        引用不出原话说明本来就没有开发指令——请改用 --status later（默认）。")
            sys.exit(3)
        print(f"[INFO] status=open 已确认：用户原话 = {args.user_request.strip()}")
    if args.status == "done":
        print("[INFO] status=done：已实现/已完成留档卡（终态，调度器不会捞）；notes 建议首行带 [已实现] 声明。")

    # 3. 建卡（status 始终显式传入，避免 API 缺省 open）
    payload = {"title": args.title, "project": args.project, "status": args.status}
    if args.notes:
        payload["notes"] = args.notes
    # #303/#350 建卡来源元数据，优先级：显式参数 > kimi-code 自动探测 > DSH 自动检测；
    # kimi-code 探测仅在非 DSH 环境且未显式指定其它来源（source 为空或 kimi-code）时启用
    dsh = detect_dsh_meta() or {}
    kimi = {}
    if not dsh and args.source in (None, "kimi-code"):
        kimi = detect_kimi_meta() or {}
    meta = build_meta({
        "source": args.source,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
    }, kimi, dsh)
    payload.update(meta)
    if meta:
        shown = " · ".join(f"{k}={v}" for k, v in meta.items())
        print(f"[INFO] 建卡来源元数据: {shown}")
    # notes 排版提示：超长单行建议分段（看板详情按 markdown 渲染，单行会挤成一坨；只提示不自动改写）
    if args.notes and "\n" not in args.notes and len(args.notes) > 200:
        print(f"[WARN] notes 为超长单行（{len(args.notes)} 字符、无换行），建议按「背景 / 方案要点 / 验收标准 / 生效方式」"
              f"空行分段、要点用 `- ` / `1. ` 列表分行；本次按原文建卡（不自动改写）。")
    status, card = request(f"{base}/api/requirements", method="POST", payload=payload)
    if status != 201 or not isinstance(card, dict) or not card.get("id"):
        print(f"[ERROR] 建卡失败（HTTP {status}）：{card}")
        sys.exit(3)

    # 4. 验证
    q = f"{base}/api/requirements?project={args.project}&status={args.status}"
    status, items = request(q)
    found = [c for c in items if c.get("id") == card["id"]] if isinstance(items, list) else []
    if not found:
        print(f"[WARN] 建卡返回成功但查询验证未找到（id={card['id']}），请人工确认")

    print(f"[OK] 已建卡 id={card['id']} project={args.project} status={args.status}")
    print(f"     title: {card.get('title')}")
    notes = card.get("notes") or ""
    if notes:
        print(f"     notes: {notes[:80]}{'…' if len(notes) > 80 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
