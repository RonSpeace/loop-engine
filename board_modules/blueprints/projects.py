"""projects 蓝图：项目配置、自动建项、项目统计与模型池。

#32 Phase 6（卡 #39）由 board.py 原样迁入：@app.route 改为本蓝图 bp.route，留驻 board.py 的名字经 board_modules.runtime.resolve 晚绑定。
"""


from board_modules.runtime import resolve


import datetime
from storage_time import storage_datetime
import json
import os
import platform
from pathlib import Path


from flask import Blueprint, jsonify, request


import git_ops
from acp_shared import role_meta
from acp_shared import role_model_detail
from board_modules.services.model_pool import _model_alias_pool
from board_modules.services.model_pool import _model_alias_pool_view
from board_modules.services.model_pool import _model_format_ok
from board_modules.services.model_pool import _model_source_label
from board_modules.services.presentation import _role_display
from board_modules.services.projects import _doing_count
from board_modules.services.projects import _load_projects_for_write
from board_modules.services.projects import _project_base_branch_parse
from board_modules.services.projects import _project_git_parse
from board_modules.services.projects import _project_git_sync_parse
from board_modules.services.projects import _GIT_URL_PASSWORD_RE
from board_modules.services.projects import _project_git_view
from board_modules.services.projects import _project_goal_parse
from board_modules.services.projects import _project_name_error
from board_modules.services.projects import _project_paths_parse
from board_modules.services.projects import _project_worktree_parse
from board_modules.services.projects import _read_projects_raw
from board_modules.services.projects import _save_projects_atomic
from board_modules.services.projects import _write_project_override
from board_modules.services.requirements import project_list


bp = Blueprint("projects", __name__)


@bp.route('/api/projects', methods=['GET'])
def api_projects():
    """已有项目名列表（供自动补全和 Agent 匹配项目名）"""
    return jsonify(project_list())


@bp.route('/api/auto-projects', methods=['GET'])
def api_auto_projects():
    """已接入自动开发的项目名列表（来自 projects.json，供录入区平铺选择）"""
    return jsonify(list(resolve("_auto_projects_config")().keys()))


def _stats_zero():
    """统计零值骨架（#3 fail-safe 降级口径，字段与正常返回一致）"""
    return {"total": 0, "doing": 0, "done": 0, "dropped": 0,
            "success_rate": None, "reject": 0, "ctx_tokens": 0}


@bp.route('/api/project-stats', methods=['GET'])
def api_project_stats():
    """#3 项目信息条统计（只读聚合；fail-safe：任何异常返回零值 200，不 500）。
    project=项目名 → 该项目；缺省/空串/all → 全库聚合 + project_count=projects.json 键数。
    口径：total=全状态计数；success_rate=done/(done+dropped)，分母 0 → null（前端显示 —）；
    reject=req_reviews.verdict='reject' JOIN requirements 计数（孤儿记录不计）；
    ctx_tokens=Σ COALESCE(agent_runs.context_tokens,0)（上下文峰值口径，非完整消耗）。"""
    project = (request.args.get('project') or '').strip()
    is_all = project in ('', 'all')
    cfg = resolve("_auto_projects_config")()
    resp = {"project": "all" if is_all else project, "goal": "", "paths": [],
            "project_count": len(cfg) if is_all else None, "stats": resolve("_stats_zero")()}
    if not is_all:
        entry = cfg.get(project) or {}
        resp["goal"] = entry.get("goal") or ""
        resp["paths"] = list(entry.get("paths") or [])
    try:
        with resolve("get_db")() as conn:
            if is_all:
                counts = conn.execute(
                    "SELECT status, COUNT(*) AS c FROM requirements GROUP BY status").fetchall()
            else:
                counts = conn.execute(
                    "SELECT status, COUNT(*) AS c FROM requirements WHERE project = ? GROUP BY status",
                    (project,)).fetchall()
            by = {row["status"]: row["c"] for row in counts}
            done, dropped = by.get("done", 0), by.get("dropped", 0)
            closed = done + dropped
            if is_all:
                reject = conn.execute(
                    "SELECT COUNT(*) AS c FROM req_reviews rv JOIN requirements r ON r.id = rv.req_id "
                    "WHERE rv.verdict = 'reject'").fetchone()["c"]
                ctx = conn.execute(
                    "SELECT COALESCE(SUM(ar.context_tokens), 0) AS s FROM agent_runs ar "
                    "JOIN requirements r ON r.id = ar.req_id").fetchone()["s"]
            else:
                reject = conn.execute(
                    "SELECT COUNT(*) AS c FROM req_reviews rv JOIN requirements r ON r.id = rv.req_id "
                    "WHERE rv.verdict = 'reject' AND r.project = ?", (project,)).fetchone()["c"]
                ctx = conn.execute(
                    "SELECT COALESCE(SUM(ar.context_tokens), 0) AS s FROM agent_runs ar "
                    "JOIN requirements r ON r.id = ar.req_id WHERE r.project = ?", (project,)).fetchone()["s"]
            resp["stats"] = {"total": sum(by.values()), "doing": by.get("doing", 0),
                             "done": done, "dropped": dropped,
                             "success_rate": (done / closed) if closed else None,
                             "reject": reject or 0, "ctx_tokens": int(ctx or 0)}
    except Exception:  # noqa: BLE001 - 展示层 fail-safe：统计失败降级零值，主页/信息条不受影响
        pass
    return jsonify(resp)


@bp.route('/api/projects/<name>/models', methods=['GET'])
def api_project_models(name):
    """#4 机器人抽屉主表数据：各角色四层链生效模型 + 来源 + 项目级覆盖 + 别名池
    （含厂商分组）。响应码与 PUT 对齐：projects.json 缺失/不可读 404、存在但解析失败 400
    （只读不写）；未知项目 404。roles 排序 = 配置 order。"""
    raw, err = _read_projects_raw()
    if err == "missing":
        return jsonify({"error": "projects.json 不存在或不可读"}), 404
    if err == "invalid":
        return jsonify({"error": "projects.json 解析失败"}), 400
    if name not in raw:
        return jsonify({"error": f"项目 {name} 不在 projects.json 中"}), 404
    entry = raw.get(name)
    models = entry.get("models") if isinstance(entry, dict) else None
    if not isinstance(models, dict):
        models = {}
    items, groups, home, warning = _model_alias_pool_view()
    roles = []
    for role in sorted(resolve("ROLE_CAPABILITIES"), key=lambda r: (role_meta(r, "order") or 99)):
        model, source = role_model_detail(role, project=name)
        override = models.get(role)
        roles.append({"role": role, "display": _role_display(role),
                      "order": role_meta(role, "order"),
                      "model": model, "source": source,
                      "source_label": _model_source_label(role, source),
                      "override": override if isinstance(override, str) else ""})
    return jsonify({"project": name, "roles": roles,
                    "aliases": {"items": items, "groups": groups, "home": home, "warning": warning}})


@bp.route('/api/projects/<name>/models', methods=['PUT'])
def api_project_model_put(name):
    """#4 写项目级模型覆盖：body {role, model}；空串/纯空白 = 清除覆盖。
    #50：思考档位统一模型默认，body 含 thinking → 400（不静默忽略）。
    校验按序短路、任一失败零写入（400/404）；成功返回既有键 + 写后四层链解析。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("role"), str):
        return jsonify({"error": "body 需为 JSON 对象且 role 为字符串"}), 400
    role = body["role"]
    raw, err = _read_projects_raw()
    if err == "missing":
        return jsonify({"error": "projects.json 不存在或不可读"}), 404
    if err == "invalid":
        return jsonify({"error": "projects.json 解析失败，拒绝写入"}), 400
    if name not in raw:
        return jsonify({"error": f"项目 {name} 不在 projects.json 中"}), 404
    if role not in resolve("ROLE_CAPABILITIES"):
        return jsonify({"error": f"未知角色 {role}"}), 400
    if "thinking" in body:
        return jsonify({"error": "thinking 已下线（思考档位统一模型默认）"}), 400
    if "model" not in body:
        return jsonify({"error": "body 需包含 model 字段"}), 400
    warning = ""
    model = body.get("model")
    if not isinstance(model, str):
        return jsonify({"error": "model 需为字符串（空串=清除覆盖）"}), 400
    val = model.strip()
    if val:
        if not _model_format_ok(val):
            return jsonify({"error": "模型名格式非法（禁止空白/控制字符，长度 ≤128）"}), 400
        pool, _home, pool_warn = _model_alias_pool()
        if pool_warn:
            warning = pool_warn
        elif val not in pool:
            return jsonify({"error": f"未知模型别名 {val}（不在 harness config.toml 别名池中）"}), 400
    status, payload = _write_project_override(name, role, model=val)
    if status != 200:
        return jsonify(payload), status
    effective, source = role_model_detail(role, project=name)
    resp = {"ok": True, "project": name, "role": role,
            "model": val, "changed": payload["changed"], "effective": effective, "source": source}
    if warning:
        resp["warning"] = warning
    return jsonify(resp)


def _day_epoch_bounds(s):
    """#42：YYYY-MM-DD → (当日 00:00, 次日 00:00) 的本地 epoch 边界；非法返回 None。
    时间过滤只认 agent_runs.ended_at（epoch REAL）；agent_sessions.created_at 是本地时间文本，
    不参与换算（tech.md 定案，防文本/epoch 串窗）。"""
    try:
        d = datetime.datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return storage_datetime(d).timestamp(), storage_datetime(d + datetime.timedelta(days=1)).timestamp()


def _model_stats_group(role, model):
    """#42：model-stats 分组骨架。cache_states 恒三键齐全（hit/unknown/na，存量 NULL 行经
    COALESCE 归 unknown，不能只数新写入值）；_sids 仅窗内 runs 路径用于去重 session 数。"""
    return {"role": role, "model": model, "sessions": 0, "ctx_tokens": 0, "tool_count": 0,
            "cached_tokens": 0, "input_tokens": 0,
            "cache_states": {"hit": 0, "unknown": 0, "na": 0}, "_sids": set(), "req_status": {}}


@bp.route('/api/projects/<name>/model-stats', methods=['GET'])
def api_project_model_stats(name):
    """#4 角色×模型分组统计（只读 SELECT，不写 board.db；fail-safe：DB 异常 → 200 空 groups）。
    口径：会话集 = agent_sessions JOIN requirements（r.project=?）；分组 = (role, model 显示名；
    空/NULL → 「（未记录）」)；sessions 按行计（含 session_id 为空的存量行）；ctx_tokens/tool_count
    按 session_id 关联 agent_runs 求和（COALESCE；关联不到计 0，不剔除毒/失败会话）；
    statuses = 组内去重 req_id 的 requirements.status 计数。排序：角色 order → sessions desc → model。
    #42 缓存命中可见性（上下文峰值永不冒充命中）：
    - 组内新增 cached_tokens/input_tokens（agent_runs 求和，NULL 按 0）、cache_states{hit,unknown,na}
      （COALESCE(cache_state,'unknown') 口径，存量 NULL 行归 unknown 桶）、cache_hit_rate
      （Σinput 有正值即返回 Σcached/Σinput，否则 null——显式 0 命中出 0%，分母缺失不出数字）。
    - 可选 since/until（YYYY-MM-DD，含头含尾；非法 400）：带参数时聚合主集 = 窗内 agent_runs
      （ended_at epoch 落在 [since 00:00, until+1 00:00) 本地边界内）：role 取 run 自带列、model 经
      session_id 关联 agent_sessions 标注（关联不到=「（未记录）」）；sessions = 窗内 runs 关联去重
      session 数；ctx/cached/input/三态计数全部只用同一批窗内 runs，窗外 run 不进任何求和。
      缺省 = 现行为不变（sessions 行计 + 该 session 全部 runs 求和）。
      命中率口径（code_review 打回复修）：cache_hit_rate 仅要求分母 Σinput > 0——显式 0 命中是
      真实返回（报 0 也是 hit），照出 0%；供应商未返回 input 的运行没有分母数据，天然不进求和
      （不是被排除，是无从计入），注记写明「仅计返回了字段的运行」。"""
    raw, err = _read_projects_raw()
    if err == "missing":
        return jsonify({"error": "projects.json 不存在或不可读"}), 404
    if err == "invalid":
        return jsonify({"error": "projects.json 解析失败"}), 400
    if name not in raw:
        return jsonify({"error": f"项目 {name} 不在 projects.json 中"}), 404
    since_raw, until_raw = request.args.get("since") or "", request.args.get("until") or ""
    since_b = _day_epoch_bounds(since_raw) if since_raw else None
    if since_raw and since_b is None:
        return jsonify({"error": "since 需为 YYYY-MM-DD"}), 400
    until_b = _day_epoch_bounds(until_raw) if until_raw else None
    if until_raw and until_b is None:
        return jsonify({"error": "until 需为 YYYY-MM-DD"}), 400
    groups = {}
    try:
        with resolve("get_db")() as conn:
            if since_b or until_b:
                cond, args = ["r.project = ?", "ar.ended_at IS NOT NULL"], [name]
                if since_b:
                    cond.append("ar.ended_at >= ?")
                    args.append(since_b[0])
                if until_b:
                    cond.append("ar.ended_at < ?")
                    args.append(until_b[1])
                rows = conn.execute(
                    "SELECT ar.id AS run_id, ar.role AS role, ar.session_id AS session_id,"
                    " ar.req_id AS req_id, ar.context_tokens AS ctx, ar.tool_count AS tools,"
                    " ar.cached_tokens AS cached, ar.input_tokens AS input,"
                    " COALESCE(ar.cache_state, 'unknown') AS cstate,"
                    " COALESCE((SELECT s.model FROM agent_sessions s WHERE s.session_id = ar.session_id"
                    "   AND s.model IS NOT NULL AND TRIM(s.model) <> '' ORDER BY s.id LIMIT 1),"
                    "  '（未记录）') AS model, r.status AS status"
                    " FROM agent_runs ar JOIN requirements r ON r.id = ar.req_id"
                    " WHERE " + " AND ".join(cond), args).fetchall()
                for row in rows:
                    role = row["role"] or ""
                    model = (row["model"] or "").strip() or "（未记录）"
                    g = groups.setdefault((role, model), _model_stats_group(role, model))
                    g["_sids"].add(row["session_id"] or f"run:{row['run_id']}")
                    g["ctx_tokens"] += int(row["ctx"] or 0)
                    g["tool_count"] += int(row["tools"] or 0)
                    g["cached_tokens"] += int(row["cached"] or 0)
                    g["input_tokens"] += int(row["input"] or 0)
                    g["cache_states"][row["cstate"]] = g["cache_states"].get(row["cstate"], 0) + 1
                    if row["req_id"] is not None:
                        g["req_status"][row["req_id"]] = row["status"] or ""
                for g in groups.values():
                    g["sessions"] = len(g["_sids"])
            else:
                sess = conn.execute(
                    "SELECT s.role AS role, s.model AS model, s.session_id AS session_id, "
                    "s.req_id AS req_id, r.status AS status "
                    "FROM agent_sessions s JOIN requirements r ON r.id = s.req_id "
                    "WHERE r.project = ?", (name,)).fetchall()
                sids = sorted({row["session_id"] for row in sess if row["session_id"]})
                run_agg = {}
                if sids:
                    marks = ",".join("?" * len(sids))
                    for row in conn.execute(
                            f"SELECT session_id, SUM(COALESCE(context_tokens,0)) AS ctx, "
                            f"SUM(COALESCE(tool_count,0)) AS tools, "
                            f"SUM(COALESCE(cached_tokens,0)) AS cached, "
                            f"SUM(COALESCE(input_tokens,0)) AS input, "
                            f"SUM(CASE WHEN COALESCE(cache_state,'unknown')='hit' THEN 1 ELSE 0 END) AS n_hit, "
                            f"SUM(CASE WHEN COALESCE(cache_state,'unknown')='unknown' THEN 1 ELSE 0 END) AS n_unknown, "
                            f"SUM(CASE WHEN COALESCE(cache_state,'unknown')='na' THEN 1 ELSE 0 END) AS n_na "
                            f"FROM agent_runs WHERE session_id IN ({marks}) GROUP BY session_id", sids):
                        run_agg[row["session_id"]] = row
                for row in sess:
                    role = row["role"] or ""
                    model = (row["model"] or "").strip() or "（未记录）"
                    g = groups.setdefault((role, model), _model_stats_group(role, model))
                    g["sessions"] += 1
                    agg = run_agg.get(row["session_id"]) if row["session_id"] else None
                    if agg is not None:
                        g["ctx_tokens"] += int(agg["ctx"] or 0)
                        g["tool_count"] += int(agg["tools"] or 0)
                        g["cached_tokens"] += int(agg["cached"] or 0)
                        g["input_tokens"] += int(agg["input"] or 0)
                        g["cache_states"]["hit"] += agg["n_hit"]
                        g["cache_states"]["unknown"] += agg["n_unknown"]
                        g["cache_states"]["na"] += agg["n_na"]
                    if row["req_id"] is not None:
                        g["req_status"][row["req_id"]] = row["status"] or ""
        for g in groups.values():
            statuses = {}
            for status in g.pop("req_status").values():   # 去重 req_id 后按状态计数
                statuses[status] = statuses.get(status, 0) + 1
            g["statuses"] = statuses
            g.pop("_sids", None)
            cached, inp = g["cached_tokens"], g["input_tokens"]
            # 命中率只看分母：Σinput>0 即出数字（显式 0 命中出 0%，与「报 0 也是 hit」一致）
            g["cache_hit_rate"] = (cached / inp) if inp > 0 else None

        def _order(g):
            o = role_meta(g["role"], "order")
            return o if isinstance(o, int) else 99
        out = sorted(groups.values(), key=lambda g: (_order(g), -g["sessions"], g["model"]))
    except Exception:  # noqa: BLE001 - 展示层 fail-safe：统计失败降级空表，抽屉不炸主页
        out = []
    return jsonify({"project": name, "groups": out})


@bp.route('/api/projects/<name>', methods=['GET'])
def api_project_detail(name):
    """#5 编辑表单预填（只读）：{project, paths, base_branch, goal} + 项目级 Git 配置归一视图
    （v1：{git, worktree, git_sync}，恒含默认值）。base_branch 缺失 → ""；非字符串的 dict 映射形态
    （#369）原样透传（不 400 不崩溃，前端转只读提示）；条目结构不符 400。"""
    raw, err = _read_projects_raw()
    if err == "missing":
        return jsonify({"error": "projects.json 不存在或不可读"}), 404
    if err == "invalid":
        return jsonify({"error": "projects.json 解析失败"}), 400
    if name not in raw:
        return jsonify({"error": f"项目 {name} 不在 projects.json 中"}), 404
    entry = raw[name]
    if isinstance(entry, str):
        paths, base_branch, goal = [entry], "", ""
    elif isinstance(entry, dict):
        raw_paths = entry.get("paths") or ([entry["path"]] if entry.get("path") else [])
        paths = [str(p) for p in raw_paths if p]
        base_branch = entry.get("base_branch")
        if not isinstance(base_branch, (str, dict)):
            base_branch = ""
        goal = entry.get("goal") if isinstance(entry.get("goal"), str) else ""
    else:
        return jsonify({"error": f"项目 {name} 配置结构不符"}), 400
    resp = {"project": name, "paths": paths, "base_branch": base_branch, "goal": goal}
    resp.update(_project_git_view(entry))     # git / worktree / git_sync（含默认值，非法值回退默认）
    return jsonify(resp)


@bp.route('/api/projects', methods=['POST'])
def api_project_create():
    """#5 新增项目：body {name, paths, base_branch?, goal?, git?, worktree?, git_sync?}。
    校验按序短路、任一失败零写入（400/404/409）；未提交的可选段不写盘（GET 时回填默认值）；
    name 判重在写锁同一临界区内进行（读-判重-写原子，同名并发恰一成功）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body 需为 JSON 对象"}), 400
    name = body.get("name")
    err = _project_name_error(name)
    if err:
        return jsonify({"error": err}), 400
    name = name.strip()
    paths, err = _project_paths_parse(body.get("paths"))
    if err:
        return jsonify({"error": err}), 400
    base_branch, err = _project_base_branch_parse(body.get("base_branch"), default="main")
    if err:
        return jsonify({"error": err}), 400
    goal, err = _project_goal_parse(body.get("goal"))
    if err:
        return jsonify({"error": err}), 400
    git = worktree = git_sync = None
    if "git" in body:
        git, err = _project_git_parse(body.get("git"))
        if err:
            return jsonify({"error": err}), 400
    if "worktree" in body:
        worktree, err = _project_worktree_parse(body.get("worktree"))
        if err:
            return jsonify({"error": err}), 400
    if "git_sync" in body:
        git_sync, err = _project_git_sync_parse(body.get("git_sync"))
        if err:
            return jsonify({"error": err}), 400
    with resolve("_MODEL_WRITE_LOCK"):
        target, orig, norm, err = _load_projects_for_write()
        if err:
            return jsonify({"error": err[1]}), err[0]
        if name in norm:
            return jsonify({"error": f"项目 {name} 已存在"}), 409
        entry = {"paths": paths, "base_branch": base_branch}
        if goal:
            entry["goal"] = goal
        if git is not None:
            entry["git"] = git
        if worktree is not None:
            entry["worktree"] = worktree
        if git_sync is not None:
            entry["git_sync"] = git_sync
        norm[name] = entry
        werr = _save_projects_atomic(target, orig, norm)
    if werr:
        return jsonify({"error": werr}), 500
    resolve("_refresh_project_paths_snapshot")()
    return jsonify({"ok": True, "project": name, "created": True}), 201


@bp.route('/api/projects/<name>', methods=['PATCH'])
def api_project_patch(name):
    """#5 编辑项目（名称不可改——键即身份）：body ⊆ {paths, base_branch, goal, git, worktree,
    git_sync}，仅有键被覆盖（git/git_sync 按整对象替换并补默认）、至少 1 键；goal 显式空串 =
    清空该键；归一后无变化 changed=false（不写盘/不生成 .bak/不动 mtime）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body 需为 JSON 对象"}), 400
    editable = ("paths", "base_branch", "goal", "git", "worktree", "git_sync")
    if not any(k in body for k in editable):
        return jsonify({"error": "至少需提交一个可编辑字段"
                                 "（paths/base_branch/goal/git/worktree/git_sync）"}), 400
    paths = base_branch = goal = git = worktree = git_sync = None
    if "paths" in body:
        paths, err = _project_paths_parse(body.get("paths"))
        if err:
            return jsonify({"error": err}), 400
    if "base_branch" in body:
        base_branch, err = _project_base_branch_parse(body.get("base_branch"))
        if err:
            return jsonify({"error": err}), 400
    if "goal" in body:
        goal, err = _project_goal_parse(body.get("goal"))
        if err:
            return jsonify({"error": err}), 400
    if "git" in body:
        git, err = _project_git_parse(body.get("git"))
        if err:
            return jsonify({"error": err}), 400
    if "worktree" in body:
        worktree, err = _project_worktree_parse(body.get("worktree"))
        if err:
            return jsonify({"error": err}), 400
    if "git_sync" in body:
        git_sync, err = _project_git_sync_parse(body.get("git_sync"))
        if err:
            return jsonify({"error": err}), 400
    with resolve("_MODEL_WRITE_LOCK"):
        target, orig, norm, err = _load_projects_for_write()
        if err:
            return jsonify({"error": err[1]}), err[0]
        entry = norm.get(name)
        if entry is None:
            return jsonify({"error": f"项目 {name} 不在 projects.json 中"}), 404
        if not isinstance(entry, dict):
            return jsonify({"error": f"项目 {name} 配置结构不符，拒绝写入"}), 400
        before = json.loads(json.dumps(norm, ensure_ascii=False))
        if "paths" in body:
            entry["paths"] = paths
        if "base_branch" in body:
            entry["base_branch"] = base_branch
        if "goal" in body:
            if goal:
                entry["goal"] = goal
            else:
                entry.pop("goal", None)   # 显式空串 = 清空该键（删除 goal 字段）
        if "git" in body:
            entry["git"] = git            # 整对象替换（缺失键补默认；不保留旧子键）
        if "worktree" in body:
            entry["worktree"] = worktree
        if "git_sync" in body:
            entry["git_sync"] = git_sync  # 整对象替换并补默认（旧布尔折算为新枚举，不落旧键）
        if norm == before:
            return jsonify({"ok": True, "project": name, "changed": False})
        werr = _save_projects_atomic(target, orig, norm)
    if werr:
        return jsonify({"error": werr}), 500
    resolve("_refresh_project_paths_snapshot")()
    return jsonify({"ok": True, "project": name, "changed": True})


@bp.route('/api/projects/<name>', methods=['DELETE'])
def api_project_delete(name):
    """#5 移除接入（仅删 projects.json 键，不删任何卡/数据）：先判存在性（未注册一律 404，与其他端点
    口径一致），再查 doing——存在进行中的卡时 409 阻断（零写入）；历史卡保留、「所有项目」可见、
    重新接入同名即完整恢复；board.db 只 SELECT 零写入。"""
    with resolve("_MODEL_WRITE_LOCK"):
        target, orig, norm, err = _load_projects_for_write()
        if err:
            return jsonify({"error": err[1]}), err[0]
        if name not in norm:
            return jsonify({"error": f"项目 {name} 不在 projects.json 中"}), 404
        doing = _doing_count(name)
        if doing > 0:
            return jsonify({"error": f"项目 {name} 有 {doing} 张进行中的卡，请先处理后再移除",
                            "doing": doing}), 409
        del norm[name]
        werr = _save_projects_atomic(target, orig, norm)
    if werr:
        return jsonify({"error": werr}), 500
    resolve("_refresh_project_paths_snapshot")()
    return jsonify({"ok": True, "project": name, "removed": True})


# ==================== 项目级 Git（v1）：探测 / 状态 / 拉取 / 推送 / 凭据 ====================
# 分层：配置解析与归一在 services/projects.py；git 执行在 services/project_git.py（ProjectGit）
# 与共享件 git_ops.py（git_env 凭据注入 / repo_lock 跨进程锁 / credential_*）。
# 安全：凭据只落 data/git_credentials.json（0600），API 永不回读密码；错误文案经 redact_urls，
# 测试连接的 password 绝不进日志/响应。
_GIT_PULL_OK = ("up_to_date", "pulled", "ahead")
_GIT_PUSH_OK = ("pushed", "up_to_date")


def _git_redact(text):
    """错误文案脱敏：去掉 URL 内嵌 userinfo（token 不进 API 响应）。"""
    return resolve("ProjectGit").redact_urls(str(text or ""))


def _project_entry_for_git(name):
    """取 projects.json 目标条目：返回 (entry, error_response)。500 一律沿用既有端点口径
    （缺失/不可读 404、解析失败 400、未知项目 404、条目结构不符 400）。"""
    raw, err = _read_projects_raw()
    if err == "missing":
        return None, (jsonify({"error": "projects.json 不存在或不可读"}), 404)
    if err == "invalid":
        return None, (jsonify({"error": "projects.json 解析失败"}), 400)
    if name not in raw:
        return None, (jsonify({"error": f"项目 {name} 不在 projects.json 中"}), 404)
    entry = raw[name]
    if not isinstance(entry, (dict, str)):
        return None, (jsonify({"error": f"项目 {name} 配置结构不符"}), 400)
    return entry, None


def _entry_paths(entry):
    """项目条目 → 仓库绝对路径列表（str 简写 / paths 数组 / path 单值三形态兼容）。"""
    if isinstance(entry, str):
        return [entry] if entry else []
    if isinstance(entry, dict):
        raw_paths = entry.get("paths")
        if isinstance(raw_paths, list):
            return [str(p) for p in raw_paths if p]
        if entry.get("path"):
            return [str(entry["path"])]
    return []


def _git_repo_targets(name, entry, repo):
    """解析本次操作的仓库目标：返回 ([path...], error_response)。repo 缺省 = 项目全部仓库；
    显式 repo 经 ProjectGit.resolve_repo 精确命中（非法/未知 → 400，避免遍历中模糊匹配）。"""
    paths = _entry_paths(entry)
    if repo:
        try:
            return [str(resolve("ProjectGit")().resolve_repo(name, repo))], None
        except ValueError as exc:
            return None, (jsonify({"error": _git_redact(exc)}), 400)
    return paths, None


def _git_status_row(path, git_cfg):
    """单仓库只读状态（不 fetch）：branch/ahead/behind/dirty/remote/url/error。"""
    p = Path(str(path))
    row = {"path": str(path), "name": p.name, "branch": None, "ahead": None,
           "behind": None, "dirty": 0, "remote": "", "url": "", "error": None}
    if not p.is_dir():
        row["error"] = "路径不存在或不是目录"
        return row
    if not (p / ".git").exists():
        row["error"] = "不是 git 仓库（无 .git）"
        return row
    rc, out, _err = git_ops.run_git(p, ["symbolic-ref", "--short", "-q", "HEAD"])
    row["branch"] = (out.strip() or None) if rc == 0 else None
    names = git_ops.remote_names(p)
    configured = str(git_cfg.get("remote") or "").strip()
    if configured and configured in names:
        remote = configured
    else:
        remote = "origin" if "origin" in names else (names[0] if names else "")
        if configured and configured not in names:
            row["error"] = f"配置的远端 {configured} 不存在"
    row["remote"] = remote
    row["url"] = (git_ops.remote_url(p, remote) if remote else "") or str(git_cfg.get("url") or "")
    rc, out, _err = git_ops.run_git(
        p, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if rc != 0:
        if row["error"] is None:
            row["error"] = "git status 失败"
        return row
    entries = resolve("ProjectGit")._parse_porcelain(out)
    tracked = [e for e in entries if e["code"] != "??"]
    untracked = [e for e in entries if e["code"] == "??"]
    row["dirty"] = len(entries)
    row["dirty_detail"] = {"changed": len(tracked), "untracked": len(untracked),
                           "deleted": sum(1 for e in tracked if "D" in e["code"])}
    if row["branch"] and row["remote"] and row["error"] is None:
        ahead, behind = git_ops.ahead_behind(p, row["branch"], row["remote"])
        row["ahead"], row["behind"] = ahead, behind
    return row


def _git_pull_row(path, res):
    """ProjectGit.pull 结果 → API 行 {name,status,before,after,conflicts?,message}。"""
    status = res.get("status") or "failed"
    if status == "pulled":
        short = (res.get("after") or {}).get("short") or ""
        message = f"已更新到 {short}" if short else "已更新"
    elif status == "up_to_date":
        message = "已是最新"
    elif status == "ahead":
        message = "本地领先远端，无需拉取"
    elif status == "conflict":
        files = res.get("files") or []
        message = f"合并冲突：{len(files)} 个文件，现场已保留，需手工处理"
    elif status == "failed":
        message = _git_redact(res.get("error") or "拉取失败")
    else:
        message = "拉取完成"
    row = {"name": Path(str(path)).name, "status": status,
           "before": res.get("before"), "after": res.get("after"), "message": message}
    if status == "conflict":
        row["conflicts"] = res.get("files") or []
    if status == "failed" and res.get("error"):
        row["error"] = _git_redact(res["error"])
    return row


def _credential_safe_state(name):
    """凭据安全视图：永不回传密码；url 内嵌 userinfo 也脱敏（存量文件亦兜底）。"""
    state = dict(git_ops.credential_state(name))
    if "url" in state:
        state["url"] = _git_redact(state.get("url") or "")
    return state


def _git_credential_url_error(url):
    """凭据 URL 不得内嵌密码（凭据单一落点 = data/git_credentials.json）。"""
    if _GIT_URL_PASSWORD_RE.match(url or ""):
        return "url 不得内嵌密码；请改用 token / SSH key 凭据"
    return None


def _git_credential_test(name, entry, url):
    """用当前项目凭据执行 `git ls-remote --heads <url>` 连通性测试（不写文件、不回显密码）。"""
    url = str(url or "").strip()
    if not url:
        return {"ok": False, "message": "缺少远端 URL，无法测试连接"}
    paths = [p for p in _entry_paths(entry) if Path(p).is_dir()]
    repo = paths[0] if paths else "/"
    try:
        with git_ops.git_env(name) as genv:
            rc, _out, err = git_ops.run_git(repo, ["ls-remote", "--heads", url],
                                            env=genv, timeout=30)
    except Exception:  # noqa: BLE001 - 测试层 fail-safe：内部异常不泄漏细节
        return {"ok": False, "message": "连接测试失败（内部错误）"}
    if rc == 0:
        return {"ok": True, "message": "连接成功"}
    if rc == 124:
        return {"ok": False, "message": "连接测试超时（30s）"}
    text = _git_redact((err or "").strip()[:300])
    return {"ok": False, "message": text or "连接失败（git 退出码非 0）"}


@bp.route('/api/system/pick-folder', methods=['POST'])
def api_system_pick_folder():
    """macOS 原生「选择文件夹」：{path} / {cancelled:true}；非 darwin 501、失败 500（不回显命令）。"""
    if platform.system() != "Darwin":
        return jsonify({"error": "当前系统不支持系统文件夹选择（仅 macOS）"}), 501
    argv = ["osascript", "-e", 'POSIX path of (choose folder with prompt "选择仓库目录")']
    try:
        r = resolve("subprocess").run(argv, capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001 - 超时/启动失败：不把内部命令与异常细节回给前端
        return jsonify({"error": "打开系统选择框失败，请手动输入路径"}), 500
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        if "-128" in err or "User canceled" in err or "用户已取消" in err:
            return jsonify({"cancelled": True})
        return jsonify({"error": "选择文件夹失败，请手动输入路径"}), 500
    if not out:
        return jsonify({"cancelled": True})
    path = Path(out)
    if not path.is_dir():
        return jsonify({"error": "所选路径不是有效目录"}), 500
    return jsonify({"path": str(path)})


def _git_detect_one(raw):
    """单路径探测（宽松）：非 git 也返回 is_git=false，不抛错。"""
    if not isinstance(raw, str) or not raw.strip():
        return {"path": "" if raw is None else str(raw), "name": "", "is_git": False,
                "current_branch": None, "branches": [], "remotes": [],
                "base_branch": "", "error": "路径需为非空字符串"}
    text = raw.strip()
    p = Path(text)
    row = {"path": text, "name": p.name, "is_git": False, "current_branch": None,
           "branches": [], "remotes": [], "base_branch": "", "error": None}
    if not os.path.isabs(text):
        row["error"] = "路径需为绝对路径"
        return row
    if not p.is_dir():
        row["error"] = "路径不存在或不是目录"
        return row
    if not (p / ".git").exists():
        return row
    row["is_git"] = True
    rc, out, _err = git_ops.run_git(p, ["symbolic-ref", "--short", "-q", "HEAD"])
    branch = (out.strip() or "") if rc == 0 else ""
    row["current_branch"] = branch or None
    rc, out, _err = git_ops.run_git(p, ["for-each-ref", "--format=%(refname:short)",
                                        "refs/heads"])
    branches = [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []
    row["branches"] = branches[:100]
    remotes = git_ops.remote_names(p)[:20]
    row["remotes"] = [{"name": n, "url": git_ops.remote_url(p, n)} for n in remotes]
    row["base_branch"] = row["current_branch"] or (row["branches"][0] if row["branches"] else "")
    return row


@bp.route('/api/system/git-detect', methods=['POST'])
def api_system_git_detect():
    """按路径批量探测 git 仓库（新增项目用）：body {paths:[绝对路径...]} →
    {ok, repos:[{path,name,is_git,current_branch,branches,remotes,base_branch,error}]}。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body 需为 JSON 对象"}), 400
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        return jsonify({"error": "paths 需为非空数组"}), 400
    if len(paths) > 50:
        return jsonify({"error": "paths 最多 50 条"}), 400
    return jsonify({"ok": True, "repos": [_git_detect_one(p) for p in paths]})


@bp.route('/api/projects/<name>/git/status', methods=['POST'])
def api_project_git_status(name):
    """项目各仓库只读状态（不 fetch）：{ok, repos:[{path,name,branch,ahead,behind,dirty,
    remote,url,error}]}；单仓库失败只置该条 error，ok = 全部无 error。"""
    entry, error = _project_entry_for_git(name)
    if error:
        return error
    cfg = _project_git_view(entry)["git"]
    repos = [_git_status_row(path, cfg) for path in _entry_paths(entry)]
    return jsonify({"ok": all(row["error"] is None for row in repos), "repos": repos})


@bp.route('/api/projects/<name>/git/pull', methods=['POST'])
def api_project_git_pull(name):
    """拉取项目仓库（body {repo?}；缺省 = 全部仓库）。每仓库 with repo_lock 串行，复用
    ProjectGit.pull（工作区脏/中间态拒绝、冲突保留现场）。{ok, repos:[{name,status,before,
    after,conflicts?,message}]}；请求级非法 400/404，仓库级失败 = 200 + ok:false。"""
    entry, error = _project_entry_for_git(name)
    if error:
        return error
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"error": "body 需为 JSON 对象"}), 400
    repo = body.get("repo")
    if repo is not None and not isinstance(repo, str):
        return jsonify({"error": "repo 需为字符串"}), 400
    targets, error = _git_repo_targets(name, entry, (repo or "").strip() or None)
    if error:
        return error
    git = resolve("ProjectGit")()
    rows = []
    for path in targets:
        try:
            with git_ops.repo_lock(path):
                res = git.pull(name, repo=str(path))
        except Exception as exc:  # noqa: BLE001 - 单仓库失败隔离，不炸整体
            res = {"status": "failed", "error": _git_redact(exc)}
        rows.append(_git_pull_row(path, res))
    return jsonify({"ok": all(row["status"] in _GIT_PULL_OK for row in rows), "repos": rows})


@bp.route('/api/projects/<name>/git/push', methods=['POST'])
def api_project_git_push(name):
    """推送 base_branch（body {repo?, confirm:true}）：confirm 必须为布尔 true；只推 base_branch、
    禁 force、非快进 fetch+ff 重试一次、推前校验工作区干净。{ok, repos:[{name,status,message,
    ahead}]}，status ∈ pushed|up_to_date|behind|rejected|failed；仓库级失败 = 200 + ok:false。"""
    entry, error = _project_entry_for_git(name)
    if error:
        return error
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body 需为 JSON 对象"}), 400
    if body.get("confirm") is not True:
        return jsonify({"error": "推送需显式确认（confirm:true）"}), 400
    repo = body.get("repo")
    if repo is not None and not isinstance(repo, str):
        return jsonify({"error": "repo 需为字符串"}), 400
    targets, error = _git_repo_targets(name, entry, (repo or "").strip() or None)
    if error:
        return error
    git = resolve("ProjectGit")()
    rows = []
    for path in targets:
        try:
            with git_ops.repo_lock(path):
                res = git.push(name, repo=str(path))
        except Exception as exc:  # noqa: BLE001 - 单仓库失败隔离，不炸整体
            res = {"status": "failed", "message": _git_redact(exc)}
        rows.append({"name": Path(str(path)).name,
                     "status": res.get("status") or "failed",
                     "message": _git_redact(res.get("message") or res.get("error")
                                            or "推送失败"),
                     "ahead": res.get("ahead")})
    return jsonify({"ok": all(row["status"] in _GIT_PUSH_OK for row in rows), "repos": rows})


@bp.route('/api/projects/<name>/git/credential', methods=['GET', 'POST'])
def api_project_git_credential(name):
    """项目 Git 凭据安全视图与保存/清除/测试（单一落点 data/git_credentials.json，0600）。

    GET → {ok, state:{configured,kind,username,url,ssh_key}}（永不含密码）；
    POST {action:save|clear|test, kind:token|ssh, username, password, ssh_key, url}：
      save = 保存后自动 ls-remote 测试（失败仍保留凭据但 ok=false，{ok,state,test}）；
      test = 用当前凭据测连接；clear = 删除凭据。密码只进不出、绝不写日志。"""
    entry, error = _project_entry_for_git(name)
    if error:
        return error
    if request.method == 'GET':
        return jsonify({"ok": True, "state": _credential_safe_state(name)})
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body 需为 JSON 对象"}), 400
    action = body.get("action")
    if action not in ("save", "clear", "test"):
        return jsonify({"error": "action 仅支持 save / clear / test"}), 400
    if action == "clear":
        git_ops.delete_credential(name)
        return jsonify({"ok": True, "state": _credential_safe_state(name)})
    cfg = _project_git_view(entry)["git"]
    url = body.get("url")
    if url is not None and not isinstance(url, str):
        return jsonify({"error": "url 需为字符串"}), 400
    if action == "save":
        kind = body.get("kind") or "token"
        if kind not in ("token", "ssh"):
            return jsonify({"error": "kind 仅支持 token / ssh"}), 400
        username = body.get("username") or ""
        password = body.get("password") or ""
        ssh_key = body.get("ssh_key") or ""
        for label, value in (("username", username), ("password", password),
                             ("ssh_key", ssh_key)):
            if not isinstance(value, str):
                return jsonify({"error": f"{label} 需为字符串"}), 400
        save_url = (url or cfg.get("url") or "").strip()
        url_error = _git_credential_url_error(save_url)
        if url_error:
            return jsonify({"error": url_error}), 400
        try:
            git_ops.save_credential(
                name, kind=kind, url=save_url, username=username.strip(),
                password=password, ssh_key=ssh_key.strip())
        except ValueError as exc:
            # save_credential 的报错不含密码；password 再做一次替换兜底（绝不回显）
            return jsonify({"error": _git_redact(str(exc)).replace(password, "***")
                            if password else _git_redact(str(exc))}), 400
        except OSError:
            return jsonify({"error": "凭据保存失败（无法写凭据文件）"}), 500
        test = _git_credential_test(name, entry, save_url)
        return jsonify({"ok": bool(test["ok"]), "state": _credential_safe_state(name),
                        "test": test})
    test_url = (url or cfg.get("url") or "").strip()
    url_error = _git_credential_url_error(test_url)
    if url_error:
        return jsonify({"error": url_error}), 400
    test = _git_credential_test(name, entry, test_url)
    return jsonify({"ok": bool(test["ok"]), "state": _credential_safe_state(name),
                    "test": test})
