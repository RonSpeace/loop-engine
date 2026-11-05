"""流程推进：节点完成后进入下一节点、评审打回与升级、收尾合并。"""

import json
import os
import time

import prompt_store
from acp_shared import ACP_MODEL, RESULT_FILENAME, _pool_lookup, default_flow_id, enabled_flows, load_project_manifest, pre_role, role_meta

from dispatcher_modules.workspace import outside_repos, validate_repos
from dispatcher_modules.runtime import D, log


def _write_flow_snapshot(req, flow_id, index=0):
    """写卡流程快照（flow/flow_name/nodes_json/node_index）并**写后回读校验**（§1.4/窗口 B）：
    未生效（旧看板对未知字段静默忽略）→ 事件留痕 + 返回 None，调用方暂停该卡推进
    （不重复拉起前置环节，等看板升级/人工处理）。成功返回刷新后的卡。"""
    f = D.flow_by_id(flow_id)
    if not f:
        return None
    payload = {"flow": flow_id, "flow_name": f["name"],
               "nodes_json": json.dumps(f["nodes"], ensure_ascii=False), "node_index": index}
    D.set_req(req["id"], payload)
    fresh = D.fetch_req(req["id"])
    ok = False
    if fresh:
        try:
            ok = ((fresh.get("nodes_json") or "") == payload["nodes_json"]
                  and (fresh.get("flow") or "") == flow_id
                  and int(fresh.get("node_index")) == index)
        except (TypeError, ValueError):
            ok = False
    if not ok:
        log.error(f"需求 #{req['id']} 流程快照写入未生效（看板未升级/写入被忽略），暂停推进")
        D.post_dispatcher_event(int(req["id"]),
                              "流程快照写入未生效（看板可能未升级），暂停该卡推进（不重复拉起前置环节）",
                              kind="phase")
        return None
    return fresh


def _adopt_flow_and_spawn(req, flow_id, extra=""):
    """采纳流程（前置环节产出或人工确认的结果）：写快照 index=0 → 拉起首节点（terminal 首节点直接收尾）"""
    req_id = str(req["id"])
    f = D.flow_by_id(flow_id)
    if not f:
        log.error(f"需求 #{req_id} 采纳流程失败：配置中无 flow {flow_id!r}")
        return False
    fresh = _write_flow_snapshot(req, flow_id, 0)
    if fresh is None:
        return False
    first = f["nodes"][0]
    D.post_dispatcher_event(int(req_id),
                          f"采纳流程「{f['name']}」（{flow_id}），进入首节点「{D.node_display(first)}」",
                          kind="phase")
    D.append_summary(req_id, f"采用流程 {f['name']}（{flow_id}）。")
    if (first.get("node_type") or "") == "terminal":
        D._run_terminal(fresh)
    else:
        D._spawn_safe(fresh, first.get("role"), extra=extra)
    return True


def _pre_stage_done(req, result, todos):
    """前置槽位完成（§1.4）：repos 结论入库 → 推荐流程（缺失/非法 → default_flow + 留痕）→
    免确认项目直通采纳；清单外仓库 / confirm 项目 → need_confirm 确认推荐流程（y 采纳、n 转人工，
    即原 route 两条人工把关的等价迁移）。"""
    req_id = str(req["id"])
    if todos is not None:
        with D.state_lock:
            D.state.setdefault("req_todos", {})[req_id] = todos
        D.save_state()
    raw_repos = (result or {}).get("repos") or []
    repos = validate_repos(req["project"], raw_repos)
    with D.state_lock:
        D.state.setdefault("req_repos", {})[req_id] = repos
    D.save_state()
    suggested = (result or {}).get("suggested_flow")
    fid = suggested if (D.flow_by_id(suggested) and D.flow_enabled(suggested)) else None
    source = "模型推荐"
    if fid is None:
        fid = default_flow_id() or ((enabled_flows() or [{}])[0].get("id") or "standard")
        source = "兜底默认"
        f = D.flow_by_id(fid)
        # 推荐了被软禁用的流程 → 与非法 id 同路径静默兜底，但留痕文案区分「已禁用 / 不存在」
        why = (f"推荐流程 {suggested!r} 已禁用（enabled: false）" if suggested and D.flow_by_id(suggested)
               else f"前置环节未产出有效流程推荐（{suggested!r}）")
        D.post_dispatcher_event(int(req_id),
                              f"{why}，按默认流程「{f['name'] if f else fid}」推进",
                              kind="flow_fallback")
        D.append_summary(req_id, f"{why}，兜底默认流程 {fid}。")
    repos_hint = f"，识别涉及仓库 {len(repos)} 个" if repos else "（未识别涉及仓库）"
    pre_name = role_meta(pre_role(), "display") or pre_role()
    D.post_dispatcher_event(int(req_id), f"「{pre_name}」完成{repos_hint}，推荐流程 {fid}", kind="phase")
    # #419 选流程留档（供复盘/离线评估筛样本）：理由由前置环节模型自述，只落事件、不进 summary（#342）
    chosen = D.flow_by_id(fid)
    chosen_name = chosen["name"] if chosen else fid
    if source == "模型推荐":
        choice_txt = (f"推荐流程 {fid}（{chosen_name}）｜理由：{D._flow_reason_text(result) or '（未给理由）'}"
                      "｜来源：模型推荐")
    else:
        choice_txt = (f"推荐流程缺失、非法或已禁用（{suggested!r}），兜底流程 {fid}（{chosen_name}）"
                      "｜来源：兜底默认")
    D.post_dispatcher_event(int(req_id), choice_txt, kind="flow_choice")
    outside = outside_repos(req["project"], raw_repos)
    if outside or D.PROJECTS[req["project"]]["confirm"]:
        why = (f"识别到清单外仓库 {', '.join(outside)}（疑似建错项目或项目仓库清单缺配置）"
               if outside else "本项目配置为进流程前人工确认")
        names = "、".join(f"{fx['id']}（{fx['name']}）" for fx in enabled_flows())
        question = (f"前置环节完成{repos_hint}，推荐流程「{fid}」；{why}。可选流程：{names}。"
                    "回复 y 采纳推荐流程并继续；回复 n 转人工处理。")
        entry = {"req": req, "question": question, "flow_id": fid}
        rounds = D._register_need_confirm(req_id, entry, pre_role())
        D._notify_need_confirm(req, question, pre_role(), rounds)
        D.post_dispatcher_event(int(req_id), f"「{pre_name}」需要确认：{question}", kind="confirm")
        return
    D._adopt_flow_and_spawn(req, fid)


QUICK_FLOW_ID = "quick"
QUICK_UPGRADE_FLOW_ID = "standard"


def _quick_high_risk_prefixes(req):
    """项目清单声明的高风险路径前缀（各仓库 .loop/project.json 的 high_risk_paths 合并）。"""
    out = []
    for repo in D._project_repos(req.get("project")):
        out.extend(load_project_manifest(repo)["high_risk_paths"])
    return out


def _path_under(path, prefix):
    pre = str(prefix).strip().rstrip("/")
    return bool(pre) and (path == pre or path.startswith(pre + "/"))


def _quick_upgrade(req, node, nodes, nxt_i):
    """B2 quick 流程升级闸：quick 卡的 code 节点完成、下一步即收尾时，改动命中高风险范围 → 快照
    改写为标准流程并进入其同角色节点之后的节点（代码审查），返回 True（调用方不再推进）；
    未命中返回 False。高风险范围 = 调度器模块（is_restart_trigger）∪ 项目清单
    high_risk_paths（路径前缀）∪ `.loop/`（会进后续所有 prompt）；改动清单取不到按命中处理。
    fail-closed：需要升级但标准流程不可用 → 转暂缓等人工，绝不按 quick 无审查收尾。
    必须先于指针 +1 判定：指针一旦指向 terminal，重启对账/主循环兜底会直接收尾。
    仅 quick 流程采集改动清单（其它流程零新增 git 调用）。"""
    req_id = str(req["id"])
    if (req.get("flow") or "") != QUICK_FLOW_ID:
        return False
    nxt = nodes[nxt_i] if nxt_i < len(nodes) else None
    if nxt is not None and (nxt.get("node_type") or "") != "terminal":
        return False
    paths = D._collect_merge_changed_paths(req, D.dev_branch(req))
    if paths is None:
        why = "改动文件清单采集失败，按保守处理"
    else:
        prefixes = D._quick_high_risk_prefixes(req)
        hit = sorted({f for f in paths
                      if D.is_restart_trigger(f) or _path_under(f, ".loop")
                      or any(_path_under(f, pre) for pre in prefixes)})
        if not hit:
            return False
        why = "改动触及高风险路径 " + "、".join(hit[:5]) + (" 等" if len(hit) > 5 else "")
    target = D.flow_by_id(QUICK_UPGRADE_FLOW_ID) if D.flow_enabled(QUICK_UPGRADE_FLOW_ID) else None
    t_nodes = (target or {}).get("nodes") or []
    same = next((i for i, n in enumerate(t_nodes) if n.get("role") == node.get("role")), None)
    if not target or same is None or same + 1 >= len(t_nodes):
        log.error(f"需求 #{req_id} quick 流程需升级（{why}）但标准流程不可用，转暂缓")
        D.set_status(req["id"], "later")
        D.append_summary(req_id, f"{D.node_display(node)} 完成；{why}，快速流程应升级但标准流程不可用，转暂缓等人工处理。")
        D.post_dispatcher_event(int(req_id), f"快速流程应升级（{why}）但标准流程不可用，需求转暂缓",
                              kind="flow_choice")
        D.notify(f"需求 #{req_id} 走快速流程但{why}，标准流程不可用无法自动升级，已转暂缓，请人工处理。")
        return True
    fresh = _write_flow_snapshot(req, target["id"], same + 1)
    if fresh is None:
        return True      # 快照写入未生效已留痕：暂停推进，不按 quick 直接收尾
    nxt_node = t_nodes[same + 1]
    D.append_summary(req_id, f"{D.node_display(node)} 完成；{why}，流程由快速升级为{target['name']}，"
                           f"进入{D.node_display(nxt_node)}。")
    D.post_dispatcher_event(int(req_id),
                          f"流程升级 quick → {target['id']}（{target['name']}）｜理由：{why}｜来源：调度器",
                          kind="flow_choice")
    if (nxt_node.get("node_type") or "") == "terminal":
        D._run_terminal(fresh)
    else:
        D._spawn_safe(fresh, nxt_node.get("role"))
    return True


def _advance_after_node(req, node):
    """agent 节点完成 → 快照指针 +1（写后回读）→ 下一节点：terminal 同回合内联执行收尾，
    否则拉起下一节点角色（§1.5 推进链内联，不依赖只捞 open 的主循环）。"""
    req_id = str(req["id"])
    _fid, nodes, idx = D.flow_snapshot(req)
    if nodes is None:
        log.error(f"需求 #{req_id} 推进失败：卡无流程快照（{D.node_display(node)} 完成）")
        return
    if (node.get("node_type") or "") == "code":
        # #384：code 节点完成即代写 diff.patch（相对基线三点 diff），供后续评审/验收节点读取；
        # 生成失败不阻断推进（评审节点 build_prompt 时会再试）
        try:
            D._gen_review_diff(req)
        except Exception as e:
            log.warning(f"需求 #{req_id} 代写 diff.patch 失败（不阻断推进）: {e}")
    nxt_i = idx + 1
    if (node.get("node_type") or "") == "code" and _quick_upgrade(req, node, nodes, nxt_i):
        return
    D.set_req(req["id"], {"node_index": nxt_i})
    fresh = D.fetch_req(req["id"]) or req
    nxt = nodes[nxt_i] if nxt_i < len(nodes) else None
    if nxt is None or (nxt.get("node_type") or "") == "terminal":
        D.append_summary(req_id, f"{D.node_display(node)} 完成，进入收尾。")
        D.post_dispatcher_event(int(req_id), f"「{D.node_display(node)}」完成，进入流程收尾（terminal）",
                              kind="phase")
        D._run_terminal(fresh)
        return
    D.append_summary(req_id, f"{D.node_display(node)} 完成，进入{D.node_display(nxt)}。")
    D.post_dispatcher_event(int(req_id), f"「{D.node_display(node)}」完成，进入「{D.node_display(nxt)}」",
                          kind="phase")
    D._spawn_safe(fresh, nxt.get("role"))

def _run_terminal(req):
    """terminal 收尾（调度器动作节点，不拉起 agent）：本地合并项目合并分支 / 仅推分支项目 push f-分支
    → 成功置 done 并清理 worktree 与分支；冲突拉起 merge_resolve（配额 2 轮/单轮 15 分钟），
    耗尽转 need_confirm（以上分支 finalize_done 内部已覆盖）。幂等：已 done 直接跳过。"""
    req_id = str(req["id"])
    fresh = D.fetch_req(req_id) or req
    if (fresh.get("status") or req.get("status")) == "done":
        return
    flow_label = fresh.get("flow_name") or fresh.get("flow") or ""
    D.post_dispatcher_event(int(req_id), f"流程收尾（terminal）：开始合并/接测（{flow_label}）", kind="merge")
    D.finalize_done(fresh, summary=f"流程「{flow_label}」走完，进入收尾。")


def _apply_model_override(resolved, model_override):
    """run_agent 本轮模型覆盖：str = #11 运行期兜底（全局模型、记降级）；dict = C3 评审升级
    （{model, source="escalation", degraded, reason}，保留 configured 供「配置 X → 实际 Y」留痕）；
    空 = 原样。"""
    if not model_override:
        return resolved
    if isinstance(model_override, dict):
        return dict(resolved, **model_override)
    return dict(resolved, model=model_override, source="global", degraded=True,
                reason="运行期模型不可用，本轮改用全局兜底模型")


def review_escalation_override(req_id, role):
    """C3：本卡该角色处于评审升级期时返回 run_agent 的 dict 覆盖，否则 None。"""
    with D.state_lock:
        model = ((D.state.get("review_escalation") or {}).get(str(req_id)) or {}).get(role)
    if not model:
        return None
    return {"model": model, "source": "escalation", "degraded": False,
            "reason": "评审连续打回，作者模型升级"}


def _escalation_candidate(req, role):
    """C3 升级目标校验：返回 (model, None) 或 (None, 不升级原因)。
    项目未配置 / 与作者当前模型相同 / 不在别名池 / 在池但不可用 → 不升级；池不可用 fail-open。"""
    model = D.project_escalation_model(req.get("project"))
    if not model:
        return None, "项目未配置 escalation_model"
    current = (D.resolve_role_model(role, project=req.get("project")) or {}).get("model")
    if model == current:
        return None, f"作者模型已是 {model}"
    pool, _home, _warning = D.model_alias_pool()
    if pool is not None:
        in_pool, usable, why = _pool_lookup(pool, model)
        if not in_pool:
            return None, f"escalation_model {model} 不在别名池"
        if not usable:
            return None, f"escalation_model {model} 不可用（{why or '未说明'}）"
    return model, None


def _clear_review_escalation(req_id, rounds_key, role):
    """C3：评审节点通过 / 人工放行 → 清该节点的各轮要点与作者角色的升级记录。"""
    with D.state_lock:
        (D.state.get("review_round_points") or {}).get(str(req_id), {}).pop(rounds_key, None)
        if role:
            (D.state.get("review_escalation") or {}).get(str(req_id), {}).pop(role, None)


# C4 打回 extra 内联评审产物的长度上限（超出截断并给完整路径，评审方可把细节留在产物文件）
REVIEW_EXTRA_MAX_CHARS = 6000


def _review_bounce_extra(req, node, reason, round_no=None):
    """评审打回时注入上游节点续跑上下文的 extra（#419/#342）：评审结论只进 req_reviews 表，
    送达上游靠 extra——resume 路径 build_prompt 产物被丢弃、仅 `{extra}` 进入续跑消息，
    不注入则上游看不到审查发现；附评审产物全文（上游只读产物目录，注入不依赖其主动读取）。"""
    tag = f"（第 {round_no} 轮）" if round_no else ""
    extra = (f"⚠️「{D.node_display(node)}」评审未通过，已打回本节点复修{tag}。\n"
             f"评审意见：{reason or '未说明原因'}")
    art = node.get("artifact") or role_meta(node.get("role"), "artifact")
    if art:
        try:
            p = D.artifact_dir(req) / art
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if len(text) > REVIEW_EXTRA_MAX_CHARS:
                    text = (text[:REVIEW_EXTRA_MAX_CHARS]
                            + f"\n……（产物超长已截断，完整内容见 {p}）")
                if text:
                    extra += f"\n\n评审产物（{art}）全文：\n{text}"
        except Exception as e:
            log.warning(f"需求 #{req['id']} 读取评审产物 {art} 失败（{e}），extra 仅含意见摘要")
    return extra


ISSUE_SEVERITIES = ("P1", "P2", "P3", "high", "medium", "low")
REVIEW_ISSUES_MAX = 30


def review_issues(result):
    """评审 result.json 的 issues（打回时必填）：[{id, severity, where, what}]。
    宽容解析：非列表 → []；元素须为 dict 且 what 非空；id 缺省按序号补 I<n>；字段截断防膨胀。"""
    raw = (result or {}).get("issues")
    if not isinstance(raw, list):
        return []
    out = []
    for i, it in enumerate(raw[:REVIEW_ISSUES_MAX], 1):
        if not isinstance(it, dict):
            continue
        what = " ".join(str(it.get("what") or "").split())[:500]
        if not what:
            continue
        out.append({"id": str(it.get("id") or f"I{i}")[:20],
                    "severity": str(it.get("severity") or "")[:10],
                    "where": " ".join(str(it.get("where") or "").split())[:200],
                    "what": what})
    return out


def _review_rework_extra(req, node, issues, round_no=None):
    """评审打回（带 issues）注入作者返工会话的 extra：未关闭问题清单 + 评审产物路径
    （prompts/system/rework-issues.md）。不读 req_reviews；summary 不再兼做反馈通道。"""
    lines = []
    for it in issues:
        sev = f"[{it['severity']}]" if it["severity"] else ""
        where = f" {it['where']}：" if it["where"] else " "
        lines.append(f"- {it['id']}{sev}{where}{it['what']}")
    art = node.get("artifact") or role_meta(node.get("role"), "artifact")
    art_path = str(D.artifact_dir(req) / art) if art else "（无）"
    return prompt_store.render("system/rework-issues", review_name=D.node_display(node),
                               round_tag=(f"（第 {round_no} 轮）" if round_no else ""),
                               issues="\n".join(lines), artifact=art_path)


def _review_node_done(req, node, result, summary):
    """评审节点完成（§1.4）：verdict pass → 前进（清轮次）；reject → 打回前驱节点复修
    （**先回退指针再 resume**，否则 #279 守卫按当前节点校验会拦截致卡死；打回意见经 extra 注入
    上游续跑会话，评审结论本身只进 req_reviews 表、不写 notes/summary——#342）；轮次键 = 评审节点下标；
    轮次 > FLOW_REVIEW_ROUNDS_MAX → need_confirm（人工唯一通道）。"""
    req_id = str(req["id"])
    role = node.get("role")
    verdict = (result or {}).get("verdict")
    if verdict not in ("pass", "reject"):
        log.warning(f"需求 #{req_id} 评审节点 {role} verdict 缺失/非法（{verdict!r}），fail-closed 按打回处理")
        D.post_dispatcher_event(int(req_id),
                              f"「{D.node_display(node)}」评审结论缺失（verdict={verdict!r}），按打回处理",
                              kind="phase")
        verdict = "reject"
    # 评审留档（req_reviews，append-only）：结论摘要 + 产物路径；agent 只读无权写，调度器代写
    try:
        D.api_board("POST", f"/requirements/{req_id}/reviews",
                  {"reviewer": f"agent:{role}", "verdict": verdict,
                   "content": f"[{D.node_display(node)}] {D.format_summary_points(summary) or '（无意见摘要）'}"
                              f"\n产物：{D.artifact_short(req, role_meta(role, 'artifact'))}"})
    except Exception as e:
        log.warning(f"需求 #{req_id} 评审留档 req_reviews 失败: {e}")
    _fid, nodes, idx = D.flow_snapshot(req)
    rounds_key = str(idx)
    prev = nodes[idx - 1] if idx > 0 else None
    if verdict == "pass":
        with D.state_lock:
            D.state.get("flow_review_rounds", {}).get(req_id, {}).pop(rounds_key, None)
        D._clear_review_escalation(req_id, rounds_key, prev.get("role") if prev else None)
        D.save_state()
        D.append_summary(req_id, f"{D.node_display(node)}通过。")
        D.post_dispatcher_event(int(req_id), f"「{D.node_display(node)}」评审通过", kind="phase")
        D._advance_after_node(req, node)
        return

    points = (D.format_summary_points(summary) or "未说明")[:300]
    with D.state_lock:
        rounds = D.state.setdefault("flow_review_rounds", {}).setdefault(req_id, {})
        rounds[rounds_key] = int(rounds.get(rounds_key, 0)) + 1
        cur = rounds[rounds_key]
        # C3 各轮打回要点（转人工时对比展示；只进 state 与确认问题，不进 notes/summary——#342）
        D.state.setdefault("review_round_points", {}).setdefault(req_id, {}).setdefault(
            rounds_key, []).append({"round": cur, "points": points})
    D.save_state()
    if prev and cur <= D.FLOW_REVIEW_ROUNDS_MAX:
        esc_tag = ""
        if cur == D.FLOW_REVIEW_ROUNDS_MAX:
            # C3 熔断第一级：作者模型升级后再打回（记录随卡 × 角色，spawn_agent 自动带上）
            model, why = D._escalation_candidate(req, prev.get("role"))
            if model:
                with D.state_lock:
                    D.state.setdefault("review_escalation", {}).setdefault(req_id, {})[prev.get("role")] = model
                D.save_state()
                esc_tag = f"，作者模型升级为 {model}"
                D.post_dispatcher_event(int(req_id),
                                      f"「{D.node_display(node)}」连续 {cur} 轮未通过，"
                                      f"「{D.node_display(prev)}」作者模型升级为 {model}", kind="model")
            else:
                D.post_dispatcher_event(int(req_id),
                                      f"「{D.node_display(node)}」连续 {cur} 轮未通过，作者模型未升级（{why}）",
                                      kind="model")
        # 顺序敏感（§1.4）：先回退指针到前驱（写后回读）再 resume，否则 #279 守卫按当前节点拦截致卡死
        D.set_req(req["id"], {"node_index": idx - 1})
        fresh = D.fetch_req(req["id"]) or req
        D.append_summary(req_id, f"{D.node_display(node)}未通过，打回{D.node_display(prev)}重做（第 {cur} 轮{esc_tag}）。")
        D.post_dispatcher_event(int(req_id),
                              f"「{D.node_display(node)}」未通过，打回「{D.node_display(prev)}」重做（第 {cur} 轮）",
                              kind="phase")
        issues = review_issues(result)
        if issues:
            bounce = _review_rework_extra(fresh, node, issues, cur)
        else:
            # fail-closed：打回未带合法 issues 照常打回，返工输入回退旧口径（意见摘要 + 评审产物全文）并留痕
            log.warning(f"需求 #{req_id} 评审节点 {role} 打回未带 issues，返工输入回退为意见摘要 + 产物全文")
            D.post_dispatcher_event(int(req_id),
                                  f"「{D.node_display(node)}」打回未提供 issues 问题清单，返工输入回退为意见摘要",
                                  kind="action")
            bounce = _review_bounce_extra(fresh, node, D.format_summary_points(summary), cur)
        D._spawn_safe(fresh, prev.get("role"), extra=bounce, resume=True)
        return
    with D.state_lock:
        history = list(((D.state.get("review_round_points") or {}).get(req_id) or {}).get(rounds_key) or [])
        escalated = ((D.state.get("review_escalation") or {}).get(req_id) or {}).get(
            prev.get("role") if prev else None)
    compare = "\n".join(f"- 第 {h['round']} 轮：{h['points']}" for h in history[-4:])
    esc_note = f"（已升级作者模型 {escalated}）" if escalated else ""
    D.append_summary(req_id, f"「{D.node_display(node)}」连续 {cur} 轮未通过{esc_note}，转人工裁决。")
    question = (f"「{D.node_display(node)}」连续 {cur} 轮评审未通过{esc_note}。各轮打回要点：\n{compare}\n"
                "回复 y 放行进入下一节点；回复 n <原因> 打回前驱节点再修一轮。")
    entry = {"req": req, "question": question, "review_index": idx}
    rounds_n = D._register_need_confirm(req_id, entry, role)
    D._notify_need_confirm(req, question, role, rounds_n)
    D.post_dispatcher_event(int(req_id), f"「{D.node_display(node)}」需要确认：{question}", kind="confirm")


def backfill_flow_snapshots():
    """#419 存量卡快照回填（dispatcher 启动，幂等；§4.2）：仅处理 nodes_json 为空且
    status ∈ {open, doing, route, review} 的卡——route → open 且清快照（重走前置环节）；
    mode 为空 = 未入流程（前置环节中/排队中）→ 不写快照，保持前置阶段（与 role_for/_legacy_position
    同源，防前置槽位被跳过、analysis 结果被当后置流程首节点产物采纳）；
    mode=auto → flow=auto/index=0；mode=semi → flow=flow||default，index=phase 下标（缺失 → 0）；
    status=review 视为所在节点已通过（下标 +1，落 terminal 的 open+terminal 卡由启动扫描收尾）；
    旧 flow id 不在配置 → default_flow + 留痕；done/dropped 有 flow 值的卡仅冻结 flow_name（展示用）。"""
    def _backfill_one(r):
        rid = str(r["id"])
        status = r.get("status")
        if r.get("nodes_json"):
            return
        if status in ("done", "dropped"):
            if r.get("flow") and not r.get("flow_name"):
                f = D.flow_by_id(r["flow"])
                if f:
                    D.set_req(r["id"], {"flow_name": f["name"]})
            return
        if status not in ("open", "doing", "route", "review"):
            return
        if status == "route":
            D.set_req(r["id"], {"status": "open", "flow": None, "flow_name": None,
                              "nodes_json": None, "node_index": None})
            D.post_dispatcher_event(int(rid), "启动回填：旧「通道待确认」卡转 open，重走前置环节", kind="phase")
            return
        # auto 流程已删除 → 本条件恒为假，mode=auto 的无快照卡掉进下方 else（视为未入流程、不写快照）；
        # 自动通道 #419 后无产生路径，实测无此类在途卡
        if (r.get("mode") or "") == "auto" and D.flow_by_id("auto"):
            fid, idx = "auto", 0
        elif (r.get("mode") or "") == "semi":
            raw_fid = r.get("flow")
            fid = raw_fid if D.flow_by_id(raw_fid) else (default_flow_id()
                                                       or ((enabled_flows() or [{}])[0].get("id") or "standard"))
            if raw_fid and not D.flow_by_id(raw_fid):
                D.post_dispatcher_event(int(rid),
                                      f"启动回填：旧流程 id {raw_fid!r} 已不在配置，改用默认流程 {fid}",
                                      kind="flow_fallback")
            nodes = D.flow_nodes(fid)
            idx = 0
            phase = r.get("phase")
            if phase:
                for i, n in enumerate(nodes):
                    if n.get("role") == phase:
                        idx = i
                        break
        else:
            # mode 为空 = 未入流程（前置环节中/排队中，§1.5「无快照 → 前置环节」）：
            # 不写快照、不钉到后置流程节点——否则前置槽位被跳过（suggested_flow/repos 契约丢失），
            # 前置产物还会被当流程首节点产物采纳
            if status == "review":
                # 无流程信息的遗留「评审中」卡无法定位节点 → 转 open 重走前置环节（与 route 同）
                D.set_req(r["id"], {"status": "open", "flow": None, "flow_name": None,
                                  "nodes_json": None, "node_index": None})
                D.post_dispatcher_event(int(rid),
                                      "启动回填：无流程信息的旧「评审中」卡转 open，重走前置环节",
                                      kind="phase")
            return
        if status == "review":
            idx = min(idx + 1, max(len(D.flow_nodes(fid)) - 1, 0))
            D.set_status(r["id"], "open")
        fresh = _write_flow_snapshot(r, fid, idx)
        if fresh is not None:
            D.post_dispatcher_event(int(rid),
                                  f"启动回填：流程快照 {fid} 节点 {idx}（status {status} → {fresh.get('status')}）",
                                  kind="phase")

    for project in D.PROJECTS:
        for st in ("open,doing,route,review",):
            try:
                rows = D.api("GET", f"?project={project}&status={st}")
            except Exception as e:
                log.warning(f"启动回填拉取 {project}/{st} 失败（跳过）: {e}")
                continue
            for r in rows or []:
                try:
                    _backfill_one(r)
                except Exception as e:
                    log.warning(f"启动回填需求 #{r.get('id')} 失败（跳过）: {e}")


def handle_result(req, role, returncode, timed_out, started_at, aborted=False):
    req_id = str(req["id"])
    workdir = D.workdir_for(req["project"])
    # 结果文件按需求隔离在产物目录；兼容旧约定（工作目录根 .req-board-result.json）做兜底
    result_file = D.artifact_dir(req) / "result.json"
    if not result_file.exists():
        legacy = workdir / RESULT_FILENAME
        if legacy.exists():
            result_file = legacy
    result = None
    parse_error = None
    backup_file = None
    redline_hit = False  # #113：dev 红线取消归因标记（仅 handle_result 内使用，不落 state）
    if result_file.exists():
        try:
            result = json.loads(result_file.read_text())
        except Exception as e:
            # 解析失败保留现场：改名备份（.invalid 后缀，os.replace 覆盖旧备份防多次重试堆积），
            # 错误信息含行/列（JSONDecodeError 自带 lineno/colno）与文件路径，便于排查
            parse_error = e
            backup_file = result_file.with_name(result_file.name + ".invalid")
            try:
                os.replace(result_file, backup_file)
            except OSError as e2:
                backup_file = None
                log.warning(f"需求 #{req_id} 结果文件非法 JSON（{result_file}: {e}），且备份失败: {e2}")
                result_file.unlink(missing_ok=True)
            else:
                log.warning(f"需求 #{req_id} 结果文件非法 JSON（{result_file}: {e}），已保留现场 {backup_file}")
        else:
            result_file.unlink(missing_ok=True)

    status = (result or {}).get("status")
    summary = (result or {}).get("summary", "")
    # #279：解析成功即 result 文件已删（上面 unlink），todos 在此提取，暂存 state 供 done 流程消费
    todos = D._extract_todos(result)

    # #284 防御：需求已完成（done）后，迟到的会话收尾（人工续跑/自动会话）不得回卷状态——
    # 结果现场已清理（上面 unlink/备份），直接跳过状态推进/通知/重试，避免 done 被改回 review/later
    if req.get("status") == "done":
        log.info(f"需求 #{req_id} 已处于 done，会话收尾跳过状态推进（role={role}）")
        return

    valid_done = False
    gate_fail_reason = None  # #399：内容仲裁分流信号（校验失败原因；进程异常路径不设，天然排除在仲裁外）
    _gate_node = D.current_node(req)
    # #419：完成门槛由节点类型决定（取代角色能力表的 done_gate 查表）——
    # code 节点要代码变更；artifact/review/前置槽位要产物非空（review 结论在推进处校验）
    _gate_type = (_gate_node or {}).get("node_type") or "artifact"
    if status == "done" and not timed_out and not aborted and returncode == 0:
        if _gate_type == "code":
            if D.has_code_changes(req, started_at):
                valid_done = True
            else:
                gate_fail_reason = "dev 分支相对基线无代码变更"
                summary = "声称完成但无代码变更或进程异常，按失败处理"
        else:
            ok, why = D.artifact_ok(req, role)
            if ok:
                valid_done = True
            else:
                gate_fail_reason = why
                summary = f"声称完成但产物校验失败（{gate_fail_reason}），按失败处理"
    elif status == "done":
        summary = summary or "声称完成但进程异常，按失败处理"

    if valid_done:
        # 成功推进：清零重试计数（每个节点独立容错额度）
        D.clear_retries(req_id)
        if role == "merge_resolve":
            # #396：agent 已解决冲突 → 恢复 main 现场 → 重试合并 → 成功完成/仍冲突转人工
            D._finish_merge_resolve(req, req_id, agent_summary=summary)
            return
        node = D.current_node(req)
        if node is None:
            # 前置槽位完成（§1.4）：推荐流程 + 涉及仓库 → 采纳流程（或人工确认）→ 拉起流程首节点
            _pre_stage_done(req, result, todos)
            return
        ntype = node.get("node_type")
        if ntype == "review":
            # 评审节点（§1.4）：pass 前进 / reject 打回前驱复修（轮次上限 → need_confirm）
            _review_node_done(req, node, result, summary)
            return
        # artifact / code 节点完成：todos 暂存 → 摘要留痕 → 推进下一节点（末个 agent 节点内联 terminal）
        if todos is not None:
            with D.state_lock:
                D.state.setdefault("req_todos", {})[req_id] = todos
            D.save_state()
        if summary:
            D.append_summary(req_id, f"完成总结：{D.format_summary_points(summary)}")
        D._advance_after_node(req, node)
        return

    if role == "merge_resolve":
        # #396：merge_resolve 非 done 收尾不外溢通用通道（通用通道按 dev 重试语义计数/毒会话）——
        # need_confirm（语义冲突）/blocked 直接转人工；进程级失败（超时/异常/未提交/非法 JSON）
        # 按 merge_resolve 独立配额重试，耗尽转人工（fail-closed，分支/worktree 现场保留）
        with D.state_lock:
            rec = D.state.get("worktrees", {}).get(req_id)
        failed = list((D._merge_resolve_entry(req_id) or {}).get("failed") or [])
        if status == "need_confirm" and not timed_out and returncode == 0:
            question = result.get("question") or "（agent 未说明问题）"
            D._merge_conflict_fallback(req, rec, failed, question=question)
            return
        if status == "blocked" and not timed_out and returncode == 0:
            D._merge_conflict_fallback(req, rec, failed, reason=summary or "agent 报告阻塞但未说明原因")
            return
        if parse_error:
            reason = f"结果文件非法 JSON（{result_file}: {parse_error}）"
        elif timed_out:
            reason = "运行超时"
        elif returncode == 0:
            reason = summary or "进程正常退出但未提交结果（会话提前结束或未写 result.json）"
        else:
            reason = summary or f"进程异常退出（退出码 {returncode}），未提交结果"
        if D._merge_resolve_retry(req, reason):
            return
        D._merge_conflict_fallback(req, rec, failed, reason=reason)
        return

    # #399：内容仲裁通道——进程正常退出 + status=done + 收尾校验不符 + result.json 带豁免申报
    # waive_gate（非空字符串）→ 不计 retries、不走进程失败重试，转 pending_confirm 人工裁决
    # （fail-closed：无人工显式 y 绝不放行）；未申报 = 视为谎报/契约缺失，保持原失败通道不变
    # （merge_resolve 非 done 已在上方独立通道拦截，此处仅覆盖其它角色的 done 谎报豁免）
    if gate_fail_reason and not valid_done:
        waive = (result or {}).get("waive_gate")
        if isinstance(waive, str) and waive.strip():
            question = (f"收尾校验未通过：{gate_fail_reason}。agent 申报豁免：{waive.strip()}。"
                        "回复 y 豁免完成（直接收口需求）/ n 退回待开发。")
            entry = {"req": req, "question": question, "kind": "confirm_override",
                     "summary": (result or {}).get("summary", ""), "todos": todos,
                     "options": None}
            rounds = D._register_need_confirm(req_id, entry, role)
            log.info(f"需求 #{req_id} 收尾校验未过（{gate_fail_reason}），agent 申报豁免，"
                     f"转人工裁决（role={role}，第 {rounds} 轮）")
            D._notify_need_confirm(req, question, role, rounds)
            # #411：confirm_override 事件也走提问格式（「X」需要确认：<问题全文>）——回放常驻卡片
            # 承载全文；原摘要式 content 无全文（未决期间网页端仅有横幅展示，横幅已移除故需此兜底载体）
            D.post_dispatcher_event(req_id, f"「{D.ROLE_NAMES.get(role, role)}」需要确认：{question}",
                                  kind="confirm")
            return

    if status == "need_confirm" and not timed_out and returncode == 0:
        question = result.get("question") or "（agent 未说明问题）"
        # #419：评审发现问题不再按角色名转人工三档裁决——评审节点按 verdict 驱动
        # （pass 前进 / reject 自动打回上游复修，见 _review_node_done）；agent 自判需要人工时
        # 走普通 need_confirm（对话类 options 解析）。kind=review_gate 不再新写入（消费分支
        # 保留仅为过渡窗口兼容历史条目）。
        entry = {"req": req, "question": question, "options": D._parse_confirm_options(result)}
        rounds = D._register_need_confirm(req_id, entry, role)
        log.info(f"需求 #{req_id} 待确认: {question}（第 {rounds} 轮）")
        D._notify_need_confirm(req, question, role, rounds)
        # #411 讨论稿：事件留档带角色名（board 回放解析「「X」需要确认：」前缀做摘要行 + 全文卡片）
        D.post_dispatcher_event(req_id, f"「{D.ROLE_NAMES.get(role, role)}」需要确认：{question}", kind="confirm")
        return

    # #385：blocked 分流——blocked 是内容性阻塞结论（agent 明确报告，多依赖外部修复，#376 accept 21:27
    # 现场实测：30s 重试轮只会复核后重报 blocked 再转 later，浪费一轮 LLM 会话）。进程正常退出时
    # 不进重试通道，直接人工收口：等效「重试耗尽」路径（later + 毒会话 + 通知，保留人工续跑入口），
    # 不经 retries 计数（计数语义收窄为纯进程级失败）；blocked 但进程异常（超时/非 0 退出码）仍走下方
    # 进程级失败重试通道
    if status == "blocked" and not timed_out and returncode == 0:
        reason = summary or "agent 报告阻塞但未说明原因"
        D.set_status(req["id"], "later")
        D.append_summary(req_id,
                       f"需求阻塞（已转 later 待人工处理，不再自动重试）：{D.format_summary_points(reason)}")
        with D.state_lock:
            # 对齐重试耗尽路径 pop：残留 last_failure 会经 resume_failure_hint 注入人工续跑 prompt 造成误导
            D.state["retries"].pop(req_id, None)
            # #11：转 later 停止自动调度，清模型降级记录（防残留跨周期误用）
            D.state.setdefault("model_fallback", {}).pop(req_id, None)
        D.save_state()
        D.mark_session_poisoned(req_id, role)
        log.info(f"需求 #{req_id} 报告阻塞（{reason}），直接转 later 待人工处理（role={role}）")
        D.notify_req_card("blocked", req, reason=reason, phase=role)
        D.post_dispatcher_event(req_id,
                              f"需求阻塞（{reason}），已转 later 待人工处理（不自动重试，role={role}）",
                              kind="retry")
        return

    rate_limited = False  # #345：API 并发限流（403）归因标记（仅 handle_result 内使用，不落 state）
    if parse_error:
        kept = f"，现场已备份为 {backup_file}" if backup_file else ""
        reason = f"结果文件非法 JSON（{result_file}: {parse_error}）{kept}，未提交结果"
    elif timed_out:
        reason = f"运行超时（budget_exceeded: time，上限 {D.effective_timeout_minutes(req, role)} 分钟）"
    elif aborted:
        # #28 输出守卫中止（须置于通用 else 之前，否则 aborted 的负退出码退化为「进程异常退出」）；
        # C2 预算截停传入结构化原因串
        reason = aborted if isinstance(aborted, str) else "本轮输出复读/单次请求超长，已中止会话，未提交结果"
    else:
        # #345：先做限流分类——限流是环境侧并发位被占，与进程行为无关，须与「进程异常退出」
        # 「正常退出未提交结果」区分（403 现场即落入「进程异常退出（退出码 -15）」的笼统归因）；
        # 覆盖正常/异常退出两类失败路径，parse_error/timed_out（有明确文件/调度证据）保持优先
        rate_limit_reason = D._rate_limit_failure_reason(req_id)
        if rate_limit_reason:
            reason = rate_limit_reason
            rate_limited = True
        elif returncode == 0:
            reason = summary or "进程正常退出但未提交结果（会话提前结束或未写 result.json）"
            # #113：dev 红线取消归因——仅"无结果文件 + 退出码 0 + dev"路径查询事件，其余失败路径零触发
            if not result and role == "dev":
                redline_evt = D._redline_cancel_reason(req_id)
                if redline_evt:
                    reason = f"命中进程安全红线，会话被拦截取消，未写 result.json（事件：{redline_evt}）"
                    redline_hit = True
        else:
            reason = summary or f"进程异常退出（退出码 {returncode}），未提交结果"
    with D.state_lock:
        rec = D._retry_rec(req_id)
        retries = rec.get("count", 0) + 1
        # #109：失败原因持久化（升级前为纯 int 计数，读取方用 _retry_rec 兼容），
        # 调度器重启/主循环重捞 resume 时据此注入续跑 prompt，重试不再盲续
        D.state["retries"][req_id] = {"count": retries, "last_failure": reason}
    D.save_state()
    if retries <= D.MAX_RETRIES:
        # #11 运行期模型不可用 → 单次兜底重跑（不新增失败分支，只给既有重试追加 model_override）：
        # 三重收敛——① 归因命中（限流 _rate_limit_failure_reason，或 _model_unavailable_reason）
        # ② 来源非全局 ③ 本轮未用过；#345 的延迟与限流 hint 逐字保留，仅让这次重试换模型。
        use_fallback = False
        with D.state_lock:
            fb = (D.state.get("model_fallback") or {}).get(req_id) or {}
            eligible = (fb.get("role") == role and fb.get("source") not in (None, "", "global")
                        and not fb.get("used"))
        if eligible and (rate_limited or D._model_unavailable_reason(req_id)):
            use_fallback = True
            with D.state_lock:
                D.state.setdefault("model_fallback", {}).setdefault(req_id, {})["used"] = True
            D.save_state()
        log.warning(f"需求 #{req_id} 第 {retries} 次失败（{reason}），自动重试（role={role}）"
                    + ("，本次改用全局兜底模型" if use_fallback else ""))
        D.post_dispatcher_event(req_id, f"运行失败（{reason}），第 {retries} 次自动重试（role={role}）"
                              + ("，本次改用全局兜底模型" if use_fallback else ""), kind="retry")
        # #345：限流失败延迟重试（并发位未释放前不立即重发，避免二次 403 烧光唯一重试），
        # 其它失败类型保持原退避 min(retries*30, 120)
        delay = D.RATE_LIMIT_RETRY_DELAY if rate_limited else min(retries * 30, 120)
        time.sleep(delay)
        retry_hint = ""
        if rate_limited:
            retry_hint = ("上次失败为 API 并发限流（403）：并发位被占，本次已延迟重试等待并发位释放；"
                          "若再遇 403 不要立即重发请求。")
        elif redline_hit:
            retry_hint = ("上次会话因命中进程安全红线被拦截取消：先读被拦截命令自查（看板执行过程/agent_events），"
                          "测试实例清理必须使用项目约定的实例脚本（PID 文件 + 精确 PID/后代清理），"
                          "禁止自行编写 trap/kill/killpg/pkill 或清理 ACP 驱动进程/进程组命令。")
        elif parse_error and backup_file:
            retry_hint = (f"上次结果文件非法 JSON，原文件已改名保留在 {backup_file}，先读取自查避免重蹈覆辙；"
                          f"写 result.json 请用 python -c 'json.dumps(...)' 或脚本生成，"
                          f"summary 内换行必须转义为 \\n。")
        elif isinstance(aborted, str) and aborted.startswith("budget_exceeded"):
            retry_hint = ("上次会话工具步数超预算被截停：先读 summary 与产物确认已有进度，"
                          "缩小探索范围、直奔改动点，接近上限前先写 result.json 保底。")
        elif aborted:
            retry_hint = ("上次会话因输出复读/单次请求超长被输出守卫中止：避免重复输出同一段文本，"
                          "若再次复读，自动重试耗尽后将转人工处理。")
        elif returncode == 0 and not result:
            retry_hint = ("上次进程正常退出但未提交结果：先确认改动完好（git status / 产物目录）"
                          "再前台跑验证确认无误，最后用 python json.dumps 补写 result.json（status/summary 必填）。")
        if use_fallback:
            retry_hint += ("本次已改用全局兜底模型重试（该角色配置的模型运行期不可用）；"
                           "若仍失败将按现有路径转人工处理。")
        # 非兜底路径逐字保持原调用形态（不给存量调用点/stub 引入新 kwarg）
        extra_kw = {"model_override": ACP_MODEL} if use_fallback else {}
        D.spawn_agent(req, role=role,
                    extra=f"上次尝试失败（第 {retries} 次）：{reason}。{retry_hint}",
                    resume=True, **extra_kw)
    else:
        # #113：重试耗尽不再退回 open（主循环只捞 open，否则一分钟静置后自动重捞形成无限循环），
        # 转 later 停止自动调度，等待人工明确改回 open 后才开始新的重试周期
        D.set_status(req["id"], "later")
        D.append_summary(req_id, f"自动开发失败 {retries} 次（已停止自动调度，等待人工重新开放），"
                               f"最后原因：{D.format_summary_points(reason)}")
        with D.state_lock:
            D.state["retries"].pop(req_id, None)
            # #11：重试耗尽转 later，清模型降级记录（防残留）
            D.state.setdefault("model_fallback", {}).pop(req_id, None)
        D.mark_session_poisoned(req_id, role)
        log.error(f"需求 #{req_id} 重试耗尽，停止自动调度转 later: {reason}")
        D.notify_req_card("failed", req, retries=retries, reason=reason)
        D.post_dispatcher_event(req_id, f"自动开发失败 {retries} 次，停止自动调度转 later（role={role}，{reason}）", kind="retry")

