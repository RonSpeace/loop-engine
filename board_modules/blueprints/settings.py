"""settings 蓝图：设置抽屉、模型设置、飞书设置与可插拔通知渠道（Webhook / Slack）。

#32 Phase 6（卡 #39）由 board.py 原样迁入：@app.route 改为本蓝图 bp.route，留驻 board.py 的名字经 board_modules.runtime.resolve 晚绑定。
"""


from board_modules.runtime import resolve


import html
import json
import os
import re
import threading
import urllib.parse


import notifiers


from flask import Blueprint, jsonify, request


from acp_shared import CONTEXT_SIZE_DEFAULTS
from acp_shared import HARNESS_PROVIDER_TYPES
from acp_shared import context_size_problem
from acp_shared import flows
from acp_shared import model_alias_usable
from acp_shared import role_meta
from acp_shared import role_prompt
from board_modules.services.assistant_runtime import ASSISTANT_CAP_FILE_TOOLS
from board_modules.services.assistant_runtime import ASSISTANT_CAP_GIT_OPS
from board_modules.services.assistant_runtime import ASSISTANT_CAP_REQ_CARDS
from board_modules.services.assistant_runtime import ASSISTANT_CAP_STATUS_WRITE
from board_modules.services.assistant_runtime import ASSISTANT_VISION_KEY
from board_modules.services.dispatcher_ops import _log_global_agent_event
from board_modules.services.harness_config import _CfgReject
from board_modules.services.harness_config import _cfg_loopback_equal
from board_modules.services.harness_config import _harness_cfg_apply
from board_modules.services.harness_config import _harness_cfg_pool
from board_modules.services.harness_config import _harness_cfg_strict_read
from board_modules.services.harness_config import _harness_cfg_write_atomic
from board_modules.services.model_pool import _assistant_model_groups
from board_modules.services.presentation import _AGENT_OVERVIEW
from board_modules.services.presentation import _effective_model_label
from board_modules.services.presentation import _flows_table_html
from board_modules.services.presentation import _role_artifact_label
from board_modules.services.presentation import _role_display
from board_modules.services.presentation import _role_prompt_details_html
from board_modules.services.presentation import _runtime_table_html


bp = Blueprint("settings", __name__)


_HARNESS_CFG_LOCK = threading.Lock()  # 非重入：整条「读-校验-改-拼接-回环-落盘」在锁内（#6 红线 8）


FEISHU_APP_ID_RE = re.compile(r'^cli_[A-Za-z0-9]{4,100}$')  # 飞书自建应用 app_id 形态（cli_ 前缀）


FEISHU_RESTART_HINT = '已保存；凭据变更需重启调度器生效（scripts/dispatcher-restart.sh <pid> config-change）'


FEISHU_TENANT_TOKEN_URL = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'


def _feishu_restart_execute(source):
    """#23 飞书设置页重启调度器的**唯一执行入口**（可测性钩子）：默认直接复用既有
    `_restart_dispatcher(source)`（launchd/run_local 分流、来源标记、健康核对全部复用）。
    测试实例以 REQ_BOARD_FEISHU_RESTART_STUB=1（或 monkeypatch 本函数）替换为桩，
    保证全程零真实 `launchctl kickstart` / `os.kill`。"""
    if os.environ.get('REQ_BOARD_FEISHU_RESTART_STUB') == '1':
        return {"restarted": True, "label": "stub",
                "health": {"ok": True, "age": 0}, "heartbeat_updated": True,
                "detail": "测试桩：未执行真实重启（REQ_BOARD_FEISHU_RESTART_STUB=1）"}
    return resolve("_restart_dispatcher")(source)


def _settings_drawer_html():
    """#3 全局设置抽屉四分组 HTML（工作流/角色/提示词/运行时环境，只读）。
    分组级 fail-safe：任一分组渲染异常降级为该组 warn 文案，其余分组照常（端点恒 200）。
    单一源：流程/提示词/运行时与 /guide 同码渲染（共享 helper），角色表经
    _role_display/_role_artifact_label/_effective_model_label/_AGENT_OVERVIEW 派生。"""
    def esc(s):
        return html.escape(s, quote=False)

    def group(title, inner):
        return f'<div class="sd-group"><h3>{esc(title)}</h3>{inner}</div>'

    def warn(title, e):
        return (f'<div class="sd-group"><h3>{esc(title)}</h3>'
                f'<div class="sd-warn">该分组渲染失败（{esc(str(e))}），不影响其余内容；'
                '修复 flows.json / prompts/ 后重启看板刷新。</div></div>')

    groups = []
    # 1. 工作流（与 /guide 同码渲染；配置缺失/非法时 flows() 为空，附降级提示）
    try:
        note = ('单一源 = 仓库根 <code>flows.json</code>（改配置并重启看板后生效；'
                '进行中的卡按卡上流程快照推进）。')
        if not flows():
            note += '<br><b>当前无可用流程</b>（flows.json 缺失或校验失败，明细见看板启动日志）。'
        groups.append(group('工作流', f'<p class="sd-note">{note}</p>' + _flows_table_html()))
    except Exception as e:  # noqa: BLE001 - 分组级展示降级
        groups.append(warn('工作流', e))
    # 2. 角色（生效模型/权限档位单一源派生，与 /guide 角色表同源；权限档位 .get 兜底防 KeyError）
    try:
        role_rows = []
        for role in sorted(resolve("ROLE_CAPABILITIES"), key=lambda r: (role_meta(r, "order") or 99)):
            artifact = _role_artifact_label(role, (_AGENT_OVERVIEW.get(role) or (None, None))[1])
            perm = (_AGENT_OVERVIEW.get(role) or ("—", None))[0]
            order = role_meta(role, "order")
            role_rows.append(
                f'<tr><td><code>{esc(role)}</code></td><td>{esc(_role_display(role))}</td>'
                f'<td>{esc(str(order) if order is not None else "")}</td>'
                f'<td><code>{esc(artifact)}</code></td>'
                f'<td>{esc(_effective_model_label(role))}</td>'
                f'<td>{esc(perm)}</td></tr>')
        groups.append(group('角色', (
            '<p class="sd-note">生效模型 = flows.json 角色配置 → 环境变量 → 全局默认；'
            '权限/安全档位单一源 = acp_shared.ROLE_CAPABILITIES。'
            '项目级模型覆盖见项目信息条最右端的机器人抽屉（选中具体项目后打开）。</p>'
            '<table class="sd-roles">\n<tr><th>角色</th><th>中文名</th><th>顺序</th><th>产物文件</th>'
            '<th>生效模型（配置 → 环境变量 → 全局）</th><th>权限档位</th></tr>\n'
            + "\n".join(role_rows) + '\n</table>')))
    except Exception as e:  # noqa: BLE001
        groups.append(warn('角色', e))
    # 3. 提示词（全文折叠默认收起；正文转义渲染；缺提示词的角色降级提示、其余照常）
    try:
        blocks, missing = _role_prompt_details_html(role_prompt)
        warn_html = ''
        if missing:
            warn_html = ('<div class="sd-warn">以下角色提示词为空（flows.json prompt_file 指向的文件'
                         '内容为空或缺失），其余照常展示：' + esc('、'.join(missing)) + '</div>')
        groups.append(group('提示词', (
            '<p class="sd-note">单一源 = <code>prompts/*.md</code>（经 flows.json '
            '<code>roles.&lt;id&gt;.prompt_file</code> 装载）；折叠默认收起，点标题展开全文；'
            '只读，编辑不在本期。</p>' + warn_html + blocks)))
    except Exception as e:  # noqa: BLE001
        groups.append(warn('提示词', e))
    # 4. 运行时环境与工具（与 /guide 同码渲染；外层 sd-runtime 仅用于抽屉列宽，不改共享 helper 输出）
    try:
        groups.append(group('运行时环境与工具',
                            '<div class="sd-runtime">' + _runtime_table_html() + '</div>'))
    except Exception as e:  # noqa: BLE001
        groups.append(warn('运行时环境与工具', e))
    return "\n".join(groups)


@bp.route('/api/settings-drawer', methods=['GET'])
def api_settings_drawer():
    """#3 全局设置抽屉（只读）：服务端渲染四分组 HTML 片段 {"html": ...}（转义单点在服务端），
    前端懒加载一次 + 内存缓存；分组级 fail-safe 见 _settings_drawer_html。"""
    return jsonify({"html": resolve("_settings_drawer_html")()})


@bp.route('/api/model-settings', methods=['GET'])
def api_model_settings_get():
    """#6 模型设置读取（严格）：无文件 404（附候选 home）、解析失败 400、只读零写入。
    响应不含 api_key 字样与密钥原文（厂商只回 key_configured）；含 default_model_candidates
    （models 别名 ∪ providers.<p>.model 合成，保序去重）与 default_model_dangling 供前端控件渲染；
    #13 另含 provider_types（harness type 合法词表，单一源）与 model 行 protocol；
    #18 另含顶层 context_defaults（预填规则单一源）与 model 行 max_context_size/incomplete_reason。"""
    path, home, _orig, _text, raw, _mode, err = _harness_cfg_strict_read()
    if err:
        return jsonify(err[1]), err[0]
    providers, warnings = [], []
    prov = raw.get("providers")
    if prov is not None and not isinstance(prov, dict):
        warnings.append("providers 不是表结构，已跳过")
    elif isinstance(prov, dict):
        for name, cfg in prov.items():
            if not isinstance(cfg, dict):
                warnings.append(f"providers.{name} 不是表结构，已跳过")
                continue
            providers.append({
                "name": str(name),
                "base_url": cfg["base_url"] if isinstance(cfg.get("base_url"), str) else "",
                "type": cfg["type"] if isinstance(cfg.get("type"), str) else "",
                "key_configured": bool(isinstance(cfg.get("api_key"), str) and cfg.get("api_key")),
                "model_count": 0,
            })
    models = []
    mdl = raw.get("models")
    if mdl is not None and not isinstance(mdl, dict):
        warnings.append("models 不是表结构，已跳过")
    elif isinstance(mdl, dict):
        for alias, cfg in mdl.items():
            if not isinstance(cfg, dict):
                warnings.append(f"models.{alias} 不是表结构，已跳过")
                continue
            # #18：max_context_size 回读（int|None，非整数一律 None）；incomplete_reason 逐条给出
            # 缺哪个字段 / 哪个值非法（合法为空串），与判定层共用 context_size_problem 单一源。
            mctx = cfg.get("max_context_size")
            models.append({
                "alias": str(alias),
                "provider": cfg["provider"] if isinstance(cfg.get("provider"), str) else "",
                "model": cfg["model"] if isinstance(cfg.get("model"), str) else "",
                "protocol": cfg["protocol"] if isinstance(cfg.get("protocol"), str) else "",
                "max_context_size": (mctx if isinstance(mctx, int) and not isinstance(mctx, bool)
                                     else None),
                "incomplete_reason": context_size_problem(mctx),
            })
    by_prov = {}
    for m in models:
        if m["provider"]:
            by_prov[m["provider"]] = by_prov.get(m["provider"], 0) + 1
    for p in providers:
        p["model_count"] = by_prov.get(p["name"], 0)
    dm_raw = raw.get("default_model")
    dm = dm_raw.strip() if isinstance(dm_raw, str) else ""
    candidates = _harness_cfg_pool(raw)
    # 悬空标记：键存在但为空值（或非字符串）同样标记异常——POST 侧对此状态任何未修正保存一律 400，
    # 前端需先警示（评审建议 4），避免用户改别的字段被拒却无预警。
    return jsonify({
        "path": str(path), "home": home,
        "default_model": dm,
        "default_model_dangling": ("default_model" in raw) and not (dm and dm in candidates),
        "default_model_candidates": candidates,
        "provider_types": list(HARNESS_PROVIDER_TYPES),
        "context_defaults": [dict(r) for r in CONTEXT_SIZE_DEFAULTS],
        "providers": providers, "models": models, "warnings": warnings,
    })


@bp.route('/api/model-settings', methods=['POST'])
def api_model_settings_post():
    """#6 模型设置保存（整单提交，全有或全无）：fail-closed 校验 + 行级最小拼接 +
    tomllib 回环校验 + tmp(fsync)/.bak/os.replace 原子替换 + 原权限保持；任一失败零写入。
    200 {ok, changed, path}；400 校验/形态失败；404 无 config.toml；500 IO/回环不符（不泄值）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "请求体需为 JSON 对象"}), 400
    with resolve("_HARNESS_CFG_LOCK"):
        path, _home, orig, text, raw, mode, err = _harness_cfg_strict_read()
        if err:
            return jsonify(err[1]), err[0]
        try:
            want, intents = _harness_cfg_apply(raw, body)
        except _CfgReject as e:
            return jsonify({"error": str(e)}), 400
        if _cfg_loopback_equal(want, raw):
            return jsonify({"ok": True, "changed": False, "path": str(path)})
        try:
            new_text = resolve("_harness_cfg_splice")(text, intents)
        except _CfgReject as e:
            return jsonify({"error": str(e)}), 400
        try:
            import tomllib
            check = tomllib.loads(new_text)
        except Exception:  # noqa: BLE001 - 回环兜底：解析不出 = 拒绝落盘
            check = None
        if not isinstance(check, dict) or not _cfg_loopback_equal(want, check):
            return jsonify({"error": "写前回环校验失败（生成文本与期望不一致），已拒绝写入，原文件未动"}), 500
        werr = _harness_cfg_write_atomic(path, orig, new_text, mode)
        if werr:
            return jsonify({"error": werr}), 500
        return jsonify({"ok": True, "changed": True, "path": str(path)})


@bp.route('/api/feishu-settings', methods=['GET'])
def api_feishu_settings_get():
    """#22 飞书凭据读取：200 {path, app_id, app_secret_configured, warnings, runtime, restart_required,
    assistant_model, assistant_models}。文件缺失也 200 空态（app_id='', app_secret_configured=false）；
    **永不回密钥原文，也不回完整指纹**。
    #23：runtime = 调度器真实运行态（心跳 + 当前文件指纹比对），restart_required = 是否需重启才生效。
    #24：assistant_model = 助手模型别名（未配置 null）；assistant_models = 按供应商分组的**可用**别名。
    #52：assistant_vision = 读图（多模态）开关，**缺省开启**（只有显式 false 才关闭）。
    #54：assistant_git_ops = 代码拉取与提交开关（缺省 false）。"""
    app_id, app_secret, warnings, assistant_model = resolve("_feishu_json_read")()
    runtime = resolve("_feishu_runtime_status")()
    cap = resolve("_feishu_json_raw")() or {}
    return jsonify({"path": str(resolve("FEISHU_JSON_PATH")), "app_id": app_id,
                    "app_secret_configured": bool(app_secret), "warnings": warnings,
                    "runtime": runtime, "restart_required": bool(runtime.get('restart_available')),
                    "assistant_model": assistant_model,
                    "assistant_models": _assistant_model_groups(),
                    # #26 看板助手能力开关：只在 POST 显式提供时才写盘，缺省 = 保持旧值/关闭
                    "assistant_file_tools": cap.get(ASSISTANT_CAP_FILE_TOOLS) is True,
                    "assistant_status_write": cap.get(ASSISTANT_CAP_STATUS_WRITE) is True,
                    "assistant_req_cards": cap.get(ASSISTANT_CAP_REQ_CARDS) is True,
                    # #54 代码拉取与提交：缺省关闭（fail-closed）
                    "assistant_git_ops": cap.get(ASSISTANT_CAP_GIT_OPS) is True,
                    # #52 读图（多模态）：缺省开启，只有显式 false 才关闭
                    "assistant_vision": cap.get(ASSISTANT_VISION_KEY) is not False})


@bp.route('/api/feishu-settings', methods=['POST'])
def api_feishu_settings_post():
    """#22 飞书凭据保存：body {app_id, app_secret?, clear_secret?, assistant_model?,
    assistant_file_tools?, assistant_status_write?}。
    先全校验后落盘（零写入）：校验失败一律 400 且文件字节不变；成功 200 {ok, changed, path, restart_hint}。
    原子写 tmp→.bak→os.replace + chmod 0600（复用 _harness_cfg_write_atomic）；IO 失败 500、原文件完好。
    secret 保留语义：app_secret 缺省/空串 = 保留旧值；clear_secret=true = 置空。
    #24 assistant_model：缺省 = 保留旧值；空串 = 清除（未配置）；非空必须命中全局别名池且可用
    （池不可用不放行），否则 400 零写入。写盘保留未知字段与 app_id/app_secret。
    #26/#30/#54 能力开关（assistant_file_tools / assistant_status_write / assistant_req_cards /
    assistant_git_ops）：可选布尔，缺省 = 不写（保留旧值）；非布尔 400 零写入；保存后看板按
    调用实时读取，**保存即生效、无需重启**（不进凭据指纹）。
    #52 读图开关 assistant_vision：同口径可选布尔；未配置时按**开启**处理（显式 false 才关闭）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "请求体需为 JSON 对象"}), 400
    unknown = set(body) - {"app_id", "app_secret", "clear_secret", "assistant_model",
                           ASSISTANT_CAP_FILE_TOOLS, ASSISTANT_CAP_STATUS_WRITE,
                           ASSISTANT_CAP_REQ_CARDS, ASSISTANT_CAP_GIT_OPS,
                           ASSISTANT_VISION_KEY}
    if unknown:
        return jsonify({"error": f"未知字段: {', '.join(sorted(unknown))}"}), 400
    app_id = body.get("app_id")
    if not isinstance(app_id, str) or not app_id.strip():
        return jsonify({"error": "app_id 必填且需为非空字符串"}), 400
    app_id = app_id.strip()
    if not resolve("FEISHU_APP_ID_RE").match(app_id):
        return jsonify({"error": "app_id 形态非法（应为 cli_ 开头的字母数字串）"}), 400
    clear_secret = body.get("clear_secret", False)
    if not isinstance(clear_secret, bool):
        return jsonify({"error": "clear_secret 必须为布尔值"}), 400
    secret_in = body.get("app_secret")
    if secret_in is not None and not isinstance(secret_in, str):
        return jsonify({"error": "app_secret 必须为字符串"}), 400
    if isinstance(secret_in, str):
        secret_in = secret_in.strip()
        if any(ord(ch) < 32 for ch in secret_in):
            return jsonify({"error": "app_secret 含非法控制字符"}), 400
        if len(secret_in) > 512:
            return jsonify({"error": "app_secret 超长"}), 400
    if secret_in and clear_secret:
        return jsonify({"error": "不能同时填写新 app_secret 与勾选「清除已配置 secret」"}), 400
    # #24 助手模型：缺省 = 保留旧值；'' = 清除；非空 = 必须在池内且可用，否则 400 零写入
    assistant_in = body.get("assistant_model")
    if assistant_in is not None and not isinstance(assistant_in, str):
        return jsonify({"error": "assistant_model 必须为字符串"}), 400
    assistant_in = assistant_in.strip() if isinstance(assistant_in, str) else None
    if assistant_in:
        entries, _home, _warning = resolve("_model_alias_pool_entries")()
        entry = next((e for e in entries if e.get("alias") == assistant_in), None)
        if entry is None:
            return jsonify({"error": f"assistant_model 别名不在模型池内: {assistant_in}"}), 400
        usable, why = model_alias_usable(entry)
        if not usable:
            return jsonify({"error": f"assistant_model 别名不可用: {assistant_in}（{why}）"}), 400
    _old_id, old_secret, _warns, old_assistant = resolve("_feishu_json_read")()
    # #22：文件存在但解析失败/结构非法 → old_secret is None。此时**绝不**用空 secret 覆盖原文件：
    # 只有请求同时给出 app_id 与非空 app_secret（重建）才放行，否则 400 并让用户先修复/删除文件。
    if old_secret is None and not secret_in:
        return jsonify({"error": "feishu.json 解析失败，请同时填写 APP ID 与 APP SECRET 以重建，"
                                 "或先手工修复/删除该文件"}), 400
    if clear_secret:
        new_secret = ""
    elif secret_in:
        new_secret = secret_in
    else:
        new_secret = old_secret   # 缺省/空串 = 保留旧值（文件缺失时 old_secret=''）
    new_assistant = old_assistant if assistant_in is None else (assistant_in or None)
    # #26/#52 能力开关：仅显式提供才写（缺省 = 由旧文件合并循环保留旧值）；非布尔一律 400 零写入
    caps = {}
    for f in (ASSISTANT_CAP_FILE_TOOLS, ASSISTANT_CAP_STATUS_WRITE, ASSISTANT_CAP_REQ_CARDS,
              ASSISTANT_CAP_GIT_OPS, ASSISTANT_VISION_KEY):
        if f in body:
            if not isinstance(body[f], bool):
                return jsonify({"error": f"{f} 必须为布尔值"}), 400
            caps[f] = body[f]
    try:
        orig = resolve("FEISHU_JSON_PATH").read_bytes()
    except OSError:
        orig = None
    # 落盘结构：app_id/app_secret 固定在前，assistant_model 次之（有值才写），再合并旧文件未知字段
    # （保留未知字段与既有配置，不因保存凭据而丢失）
    out = {"app_id": app_id, "app_secret": new_secret}
    if new_assistant:
        out["assistant_model"] = new_assistant
    for k, v in (resolve("_feishu_json_raw")() or {}).items():
        if k not in ("app_id", "app_secret", "assistant_model"):
            out[k] = v
    out.update(caps)   # 显式提供的开关覆盖旧值（缺省时上面已保留旧值）
    new_text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
    if orig is not None and orig == new_text.encode("utf-8"):
        runtime = resolve("_feishu_runtime_status")()
        return jsonify({"ok": True, "changed": False, "path": str(resolve("FEISHU_JSON_PATH")),
                        "restart_hint": resolve("FEISHU_RESTART_HINT"),
                        "runtime": runtime,
                        "restart_required": bool(runtime.get('restart_available'))})
    werr = _harness_cfg_write_atomic(resolve("FEISHU_JSON_PATH"), orig or b"", new_text, 0o600)
    if werr:
        return jsonify({"error": werr}), 500
    # #23：按**写后当前文件**重新判定运行态——保存后调度器仍未加载新凭据时 restart_required 为 true
    runtime = resolve("_feishu_runtime_status")()
    return jsonify({"ok": True, "changed": True, "path": str(resolve("FEISHU_JSON_PATH")),
                    "restart_hint": resolve("FEISHU_RESTART_HINT"),
                    "runtime": runtime,
                    "restart_required": bool(runtime.get('restart_available'))})


@bp.route('/api/feishu-settings/restart', methods=['POST'])
def api_feishu_settings_restart():
    """#23「重启调度器使其生效」入口：body {confirm: true}（二次确认）。
    预检调度器进程树中的运行中 kimi agent：有则 409 拒绝（不中断在途需求）；进程树读取异常
    fail-closed 拒绝。通过后复用既有重启执行体（来源标识 feishu-settings），成功/拒绝/失败均写
    agent_events(req_id=0) 全局留痕。**有意不复用 #421 红态双静默守卫**——本入口重启的是健康进程。
    执行体经 _feishu_restart_execute 间接调用，测试可打桩（零真实 launchctl/os.kill）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get('confirm') is not True:
        return jsonify({'error': '缺少二次确认（confirm 必须为 true）'}), 400
    try:
        agents = resolve("_dispatcher_kimi_agents")()
    except Exception as e:  # noqa: BLE001 - 无法确认时 fail-closed 拒绝，不强行重启
        _log_global_agent_event(f"拒绝重启调度器（无法确认运行中 agent，fail-closed）：{e}")
        return jsonify({'error': f'无法确认调度器是否有运行中 agent，拒绝重启：{e}'}), 502
    if agents:
        detail = "；".join(a[:100] for a in agents[:3])
        reason = (f'检测到运行中 kimi agent {len(agents)} 个，拒绝重启调度器'
                  f'（否则重启后孤儿 agent 改一半代码）：{detail}')
        _log_global_agent_event(f"拒绝重启调度器（feishu-settings）：运行中 kimi agent "
                                f"{len(agents)} 个，不中断在途需求")
        return jsonify({'error': reason, 'agents': len(agents)}), 409
    try:
        res = resolve("_feishu_restart_execute")('feishu-settings')
    except ValueError as e:
        _log_global_agent_event(f"重启调度器失败（feishu-settings）：{e}")
        return jsonify({'error': str(e)}), 502
    runtime = resolve("_feishu_runtime_status")()
    _log_global_agent_event(f"用户保存飞书凭据后经网页触发重启调度器（来源 feishu-settings）："
                            f"{res.get('detail') or ''}")
    payload = dict(res)
    payload['runtime'] = runtime
    return jsonify(payload)


@bp.route('/api/feishu-settings/test', methods=['POST'])
def api_feishu_settings_test():
    """#22 飞书凭据连通性测试：body {app_id?, app_secret?}（缺省用已存文件值）。
    200 {ok, code, msg, expire?}：成功 {ok:true, code:0, msg:'ok', expire:<int>}；
    业务失败 {ok:false, code:<int>, msg:<原文>}；网络异常 {ok:false, code:null, msg:'网络错误: …'}。
    调 tenant_access_token/internal（urllib + timeout 10s）；board 不引入 lark_oapi；不回显 secret。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    app_id = body.get("app_id")
    app_secret = body.get("app_secret")
    stored_id, stored_secret, _warns, _assistant = resolve("_feishu_json_read")()
    app_id = app_id.strip() if isinstance(app_id, str) and app_id.strip() else stored_id
    app_secret = app_secret.strip() if isinstance(app_secret, str) and app_secret.strip() else stored_secret
    if not app_id or not app_secret:
        return jsonify({"ok": False, "code": None,
                        "msg": "未配置 app_id 或 app_secret（请先在「通知渠道」tab 的「飞书机器人」小节保存凭据）"})
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(resolve("FEISHU_TENANT_TOKEN_URL"), data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(200000).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - 网络异常不 500，按业务失败返回
        return jsonify({"ok": False, "code": None, "msg": f"网络错误: {e}"})
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return jsonify({"ok": False, "code": None, "msg": f"飞书响应解析失败: {raw[:200]}"})
    if not isinstance(d, dict):
        return jsonify({"ok": False, "code": None, "msg": f"飞书响应格式异常: {raw[:200]}"})
    code = d.get("code")
    if code == 0:
        return jsonify({"ok": True, "code": 0, "msg": "ok", "expire": d.get("expire")})
    return jsonify({"ok": False, "code": code, "msg": str(d.get("msg") or "")})


# ==================== 可插拔通知渠道（notifiers.json：Webhook / Slack） ====================
# 飞书仍走上方 /api/feishu-settings（feishu.json，向后兼容）；本组端点只管配置驱动的出站渠道。
# 密钥口径：Webhook secret 与 Slack webhook URL（URL 即凭据）永不回显，只回 *_configured + 脱敏 host；
# Webhook URL 同样只回脱敏 host（查询串/路径里常带令牌）。调度器按文件 mtime 自动重载，保存即生效。
NOTIFIER_TEST_TIMEOUT = 10.0
_NOTIFIER_URL_KEY = {"webhook": "url", "slack": "webhook_url"}
_NOTIFIER_LOCK = threading.Lock()


def _notifier_board_url_default():
    return f"http://127.0.0.1:{os.environ.get('REQ_BOARD_PORT') or '8899'}"


def _notifier_json_load():
    """→ (raw_dict|None, error|None)。缺失 = ({}, None)；损坏 = (None, 固定文案)（不回显异常原文）。"""
    raw, err = notifiers.read_config(resolve("NOTIFIERS_JSON_PATH"))
    if err:
        return None, err
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "notifiers.json 结构不符（顶层非对象），请检查文件格式"
    return raw, None


def _notifier_channel_view(ctype, craw, error):
    """单渠道 GET 视图（不含任何密钥原文/完整 URL）。"""
    craw = craw if isinstance(craw, dict) else {}
    url = craw.get(_NOTIFIER_URL_KEY[ctype])
    url = url if isinstance(url, str) else ""
    events = craw.get("events")
    events = ([e for e in events if isinstance(e, str)] if isinstance(events, list)
              else list(notifiers.EVENT_KINDS))
    timeout = craw.get("timeout")
    view = {"enabled": craw.get("enabled") is True and not error,
            "configured_enabled": craw.get("enabled") is True,
            "url_configured": bool(url), "url_hint": notifiers.mask_url(url),
            "timeout": (timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                        else notifiers.DEFAULT_TIMEOUT),
            "events": events, "error": error}
    if ctype == "webhook":
        sec = craw.get("secret")
        view["secret_configured"] = bool(isinstance(sec, str) and sec)
    return view


@bp.route('/api/notifier-settings', methods=['GET'])
def api_notifier_settings_get():
    """通知渠道读取：200 {path, board_url, board_url_default, warnings, events, channels:{webhook, slack}}。
    文件缺失 = 空态；文件损坏 = 200 + warnings（渠道全部按停用展示），**永不回密钥原文与完整 URL**。"""
    raw, err = resolve("_notifier_json_load")()
    warnings = [err] if err else []
    parsed = notifiers.parse_config(raw) if raw is not None else {"errors": {}, "board_url": None}
    errors = parsed["errors"]
    for k in ("_", "board_url"):
        if k in errors:
            warnings.append(errors[k])
    raw = raw or {}
    chans = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
    top_err = err or errors.get("_")
    channels = {c: _notifier_channel_view(c, chans.get(c), top_err or errors.get(c))
                for c in notifiers.CHANNEL_TYPES}
    bu = raw.get("board_url")
    return jsonify({"path": str(resolve("NOTIFIERS_JSON_PATH")),
                    "board_url": bu if isinstance(bu, str) else "",
                    "board_url_default": _notifier_board_url_default(),
                    "warnings": warnings,
                    "events": [{"id": e, "label": notifiers.EVENT_LABELS[e]}
                               for e in notifiers.UI_EVENT_KINDS],
                    "channels": channels})


def _notifier_str(body, key, limit=2048):
    """可选字符串字段：缺省 → (None, None)；非字符串/控制字符/超长 → (None, 原因)；否则 strip 后返回。"""
    v = body.get(key)
    if v is None:
        return None, None
    if not isinstance(v, str):
        return None, f"{key} 必须为字符串"
    v = v.strip()
    if any(ord(ch) < 32 for ch in v):
        return None, f"{key} 含非法控制字符"
    if len(v) > limit:
        return None, f"{key} 超长"
    return v, None


def _notifier_merge_channel(ctype, cur, body):
    """把请求体合并进单渠道旧配置（就地改 cur）→ 错误文案|None。缺省/空串 = 保留旧值。"""
    url_key = _NOTIFIER_URL_KEY[ctype]
    for flag in ("enabled", "clear_url", "clear_secret"):
        if flag in body and not isinstance(body[flag], bool):
            return f"{flag} 必须为布尔值"
    url_in, why = _notifier_str(body, "url")
    if why:
        return why
    if url_in and body.get("clear_url"):
        return "不能同时填写新地址与勾选清除地址"
    if url_in:
        if not notifiers.valid_http_url(url_in, https_only=(ctype == "slack")):
            return ("Slack webhook URL 必须为 https 地址" if ctype == "slack"
                    else "Webhook URL 必须为 http(s) 地址")
        cur[url_key] = url_in
    elif body.get("clear_url"):
        cur.pop(url_key, None)
    if ctype == "webhook":
        sec_in, why = _notifier_str(body, "secret", limit=512)
        if why:
            return why
        if sec_in and body.get("clear_secret"):
            return "不能同时填写新 secret 与勾选清除 secret"
        if sec_in:
            cur["secret"] = sec_in
        elif body.get("clear_secret"):
            cur.pop("secret", None)
    for k in ("enabled", "timeout", "events"):
        if k in body:
            cur[k] = body[k]
    return None


@bp.route('/api/notifier-settings', methods=['POST'])
def api_notifier_settings_post():
    """通知渠道保存（按渠道逐个保存）：body {channel: "webhook"|"slack", enabled?, url?, clear_url?,
    secret?, clear_secret?（仅 webhook）, timeout?, events?} 或 {channel: "general", board_url}。
    先全校验后落盘（零写入）：未知字段/形态非法/启用但缺地址/文件损坏一律 400、文件字节不变；
    url/secret 缺省或空串 = 保留旧值。原子写 tmp→.bak→os.replace + chmod 0600；保留未知字段。
    成功 200 {ok, changed, path}；调度器按文件变化自动重载，**保存即生效、无需重启**。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "请求体需为 JSON 对象"}), 400
    ctype = body.get("channel")
    if ctype not in notifiers.CHANNEL_TYPES and ctype != "general":
        return jsonify({"error": "channel 必须为 webhook / slack / general"}), 400
    if ctype == "general":
        allowed = {"channel", "board_url"}
    else:
        allowed = {"channel", "enabled", "url", "clear_url", "timeout", "events"}
        if ctype == "webhook":
            allowed |= {"secret", "clear_secret"}
    unknown = set(body) - allowed
    if unknown:
        return jsonify({"error": f"未知字段: {', '.join(sorted(unknown))}"}), 400
    with _NOTIFIER_LOCK:
        raw, err = resolve("_notifier_json_load")()
        if err:
            return jsonify({"error": f"{err}；请先手工修复或删除该文件后再保存"}), 400
        out = dict(raw)
        if ctype == "general":
            bu, why = _notifier_str(body, "board_url")
            if why:
                return jsonify({"error": why}), 400
            if bu:
                if not notifiers.valid_http_url(bu):
                    return jsonify({"error": "board_url 必须为 http(s) 地址"}), 400
                out["board_url"] = bu.rstrip("/")
            else:
                out.pop("board_url", None)
        else:
            chans = out.get("channels", {})
            if not isinstance(chans, dict):
                return jsonify({"error": "notifiers.json 的 channels 不是对象，请先手工修复"}), 400
            cur = dict(chans[ctype]) if isinstance(chans.get(ctype), dict) else {}
            why = _notifier_merge_channel(ctype, cur, body)
            if not why:
                _spec, why = notifiers.parse_channel(ctype, cur)
            if why:
                return jsonify({"error": why}), 400
            chans = dict(chans)
            chans[ctype] = cur
            out["channels"] = chans
        path = resolve("NOTIFIERS_JSON_PATH")
        new_text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
        try:
            orig = path.read_bytes()
        except OSError:
            orig = None
        if orig is not None and orig == new_text.encode("utf-8"):
            return jsonify({"ok": True, "changed": False, "path": str(path)})
        werr = _harness_cfg_write_atomic(path, orig or b"", new_text, 0o600)
        if werr:
            return jsonify({"error": werr}), 500
        return jsonify({"ok": True, "changed": True, "path": str(path)})


def _notifier_test_send(ctype, spec, event):
    """测试发送执行体（可测性钩子：测试可 monkeypatch 本函数或 notifiers.webhook.post_json）。"""
    spec = dict(spec, enabled=True)
    n = notifiers.build_channels({"channels": {ctype: spec}}, "")[ctype]
    n.timeout = min(float(n.timeout or notifiers.DEFAULT_TIMEOUT), NOTIFIER_TEST_TIMEOUT)
    return n.deliver(event)


@bp.route('/api/notifier-settings/test', methods=['POST'])
def api_notifier_settings_test():
    """通知渠道发送测试：body {channel, url?, secret?}（缺省用已存值；不要求已启用）→ 同步发一条测试消息
    （超时上限 10s）→ 200 {ok, msg}；网络/HTTP 失败 ok=false 不 500；msg **不含 URL 与密钥**。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    ctype = body.get("channel")
    if ctype not in notifiers.CHANNEL_TYPES:
        return jsonify({"error": "channel 必须为 webhook / slack"}), 400
    raw, err = resolve("_notifier_json_load")()
    if err:
        return jsonify({"ok": False, "msg": err})
    chans = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
    cur = dict(chans[ctype]) if isinstance(chans.get(ctype), dict) else {}
    url_key = _NOTIFIER_URL_KEY[ctype]
    probe = {k: body[k] for k in ("url", "secret") if k in body and (ctype == "webhook" or k == "url")}
    why = _notifier_merge_channel(ctype, cur, probe)
    if why:
        return jsonify({"ok": False, "msg": why})
    if not cur.get(url_key):
        return jsonify({"ok": False, "msg": "未配置地址（请先填写地址）"})
    spec, why = notifiers.parse_channel(ctype, dict(cur, enabled=True))
    if why:
        return jsonify({"ok": False, "msg": why})
    label = {"webhook": "Webhook", "slack": "Slack"}[ctype]
    text = f"这是一条来自需求看板的测试通知（渠道：{label}）。收到即表示配置可用。"
    event = notifiers.NotifyEvent(kind="test", title="Loop Engine 测试通知", text=text,
                                  raw={"text": text})
    try:
        ok, info = resolve("_notifier_test_send")(ctype, spec, event)
    except Exception as e:  # noqa: BLE001 - 测试失败不 500，异常只报类型（不泄 URL/密钥）
        return jsonify({"ok": False, "msg": f"发送异常：{type(e).__name__}"})
    return jsonify({"ok": bool(ok), "msg": str(info or "")})
