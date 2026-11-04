"""meta 蓝图：首页/指南页、应用元信息、调度器健康、产物列表与 SSE 流。

#32 Phase 6（卡 #39）由 board.py 原样迁入：@app.route 改为本蓝图 bp.route，留驻 board.py 的名字经 board_modules.runtime.resolve 晚绑定。
"""


from board_modules.runtime import resolve


import hashlib
import json
import queue
import threading
import time
from pathlib import Path


from flask import Blueprint, Response, current_app, jsonify, render_template, request, stream_with_context


from acp_shared import DEFAULT_FLOW_COLOR
from acp_shared import FLOW_CONFIG_ERROR
from acp_shared import NODE_TYPE_LABELS
from acp_shared import default_flow_id
from acp_shared import flows
from acp_shared import pre_role
from acp_shared import role_meta
from board_modules.common import STATUS_LABEL_CN
from board_modules.common import _now_ts
from board_modules.common import _ts_ms
from board_modules.common import artifact_candidates
from board_modules.services.presentation import AGENTS_SECTION_ANCHOR
from board_modules.services.presentation import _builtin_agents_section
from board_modules.sse import _SSEStream
from board_modules.sse import _sse_acquire
from board_modules.sse import _sse_peer_gone
from board_modules.sse import _sse_stats_snapshot


bp = Blueprint("meta", __name__)


# #33 静态资源指纹缓存：realpath -> (mtime_ns, size, href)。board.py 的 app 是 threaded=True，
# index() 可并发渲染，读写都走锁；内容未变不重复哈希，内容一变 mtime/size 失配即重算新地址。
_ASSET_HREF_CACHE = {}
_ASSET_HREF_LOCK = threading.Lock()


def _asset_href(rel, root=None):
    """首页静态资源地址（#33 CSS/JS 外置）：/static/<rel>?v=<文件字节 sha256 前 12 位>。
    root 缺省 = 当前应用的 static 目录；测试可传临时目录做指纹变化断言（U06）。"""
    path = Path(root if root is not None else current_app.static_folder) / rel
    stat = path.stat()
    key = str(path.resolve())
    with _ASSET_HREF_LOCK:
        hit = _ASSET_HREF_CACHE.get(key)
        if hit is not None and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
            return hit[2]
    href = "/static/%s?v=%s" % (rel, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
    with _ASSET_HREF_LOCK:
        _ASSET_HREF_CACHE[key] = (stat.st_mtime_ns, stat.st_size, href)
    return href


def _freeze_time_script():
    """冻结时钟的页面内联脚本（仅 REQ_BOARD_FREEZE_TIME 设置时注入首页 head 最前）：
    无参 new Date() 与 Date.now() 固定为冻结时刻，带参构造/静态方法原样透传。
    标签名刻意拆开拼接：大量测试按源码文本定位首个 script 标签来抽取页面 JS，本函数位于 HTML 之前。"""
    ms = int(round(resolve("FREEZE_TS") * 1000))
    tag = "script"
    return ("<" + tag + ">(function(){var T=%d,R=Date;function F(){var a=arguments;"
            "if(!(this instanceof F))return new R(T).toString();"
            "if(a.length===0)return new R(T);"
            "return new (Function.prototype.bind.apply(R,[null].concat([].slice.call(a))))();}"
            "F.prototype=R.prototype;F.now=function(){return T;};F.parse=R.parse;F.UTC=R.UTC;"
            "window.Date=F;window.__REQ_BOARD_FROZEN_MS__=T;})();</" + tag + ">") % ms


@bp.route('/api/meta', methods=['GET'])
def api_meta():
    """#419 前端元数据注入（替代前端硬编码 STATUS_LABEL/MODE_LABEL/FLOW_LABEL）：
    流程/节点来自 flows.json（默认流程 + 每条流程的节点序列含 node_type 中文与角色 display），
    status/mode 标签供历史回放与过渡期兼容展示；配置加载失败时 flows 为空并带 config_error
    （前端显示原文标签，不白屏）。"""
    flows_meta = []
    for f in flows():
        nodes = []
        for n in f.get("nodes") or []:
            role = n.get("role")
            ntype = n.get("node_type")
            nodes.append({"role": role, "node_type": ntype,
                          "node_type_label": NODE_TYPE_LABELS.get(ntype, ntype),
                          "name": n.get("name") or (role_meta(role, "display") if role else "收尾"),
                          "display": role_meta(role, "display") if role else None,
                          "artifact": n.get("artifact")})
        flows_meta.append({"id": f["id"], "name": f["name"],
                           "description": f.get("description") or "",
                           "enabled": f.get("enabled", True),
                           "color": f.get("color") or DEFAULT_FLOW_COLOR,
                           "default": f["id"] == default_flow_id(), "nodes": nodes})
    return jsonify({"default_flow": default_flow_id(), "pre_role": pre_role(),
                    "flows": flows_meta, "status_labels": STATUS_LABEL_CN,
                    "legacy_status_labels": {k: STATUS_LABEL_CN[k] for k in ("route", "review")},
                    "mode_labels": {"auto": "自动", "semi": "半自动"},
                    "node_type_labels": NODE_TYPE_LABELS,
                    "config_error": FLOW_CONFIG_ERROR})


def _feishu_health_payload():
    """#23 健康响应里的飞书运行态子对象（#23 状态判定单一入口；失败降级 None，不影响健康主字段）。
    #40：子对象含 state/reason/state_source/updated_at/pid/pid_status，接口顶层同步透出这几个键。"""
    try:
        return resolve("_feishu_runtime_status")()
    except Exception:  # noqa: BLE001
        return None


@bp.route('/api/dispatcher-health', methods=['GET'])
def api_dispatcher_health():
    """调度器健康状态：读心跳文件计算距今秒数，超过 HEALTH_STALE_SEC 视为疑似故障（进程挂死但保活的场景）。
    #373/#380：心跳扩展字段 last_shutdown（上次退出 {at, signal, source?}）与 auto_restart_ok 仍透出，
    仅内部运维记录（心跳文件/启动日志有留档），前端角标不再消费（#408）。
    #8：board_restart_pending（直跑模式看板待重启提示，{at, req_id}）仅当 at > 本进程启动时刻才透出
    ——看板重启后自动隐藏（重启已发生），无需清理写入；两处 jsonify 分支都带该键（缺省 null）。
    #40：顶层另透出 state/reason/state_source/updated_at/pid/pid_status（判定唯一源 = 飞书运行态
    子对象，即同一份心跳）。`ok`/`age` 语义不变（年龄 < 180 为 ok=true，不看 pid）；无心跳文件时
    ok=false、age=null，但 state 走兜底（unknown/disabled 之一）而不是省略，pid_status=missing。"""
    feishu = resolve("_feishu_health_payload")()
    payload = feishu if isinstance(feishu, dict) else {}
    state_fields = {"state": payload.get("state"), "reason": payload.get("reason"),
                    "state_source": payload.get("state_source"),
                    "updated_at": payload.get("updated_at"), "pid": payload.get("pid"),
                    "pid_status": payload.get("pid_status") or "missing"}
    try:
        heartbeat = json.loads(resolve("HEARTBEAT_FILE").read_text())
        last_poll_at = heartbeat["last_poll_at"]
        age = max(0, int(_now_ts() - last_poll_at))
        last_shutdown = heartbeat.get("last_shutdown")
        pending = heartbeat.get("board_restart_pending")
        if not (isinstance(pending, dict) and isinstance(pending.get("at"), (int, float))
                and pending["at"] > resolve("BOARD_STARTED_AT")):
            pending = None
    except Exception:
        return jsonify({"ok": False, "age": None, "last_shutdown": None, "auto_restart_ok": None,
                        "board_restart_pending": None, "feishu": feishu, **state_fields})
    return jsonify({"ok": age < resolve("HEALTH_STALE_SEC"), "age": age, "last_shutdown": last_shutdown,
                    "auto_restart_ok": heartbeat.get("auto_restart_ok"),
                    "board_restart_pending": pending,
                    "feishu": feishu, **state_fields})


@bp.route('/api/dispatcher/restart', methods=['POST'])
def api_dispatcher_restart():
    """#421 网页端「疑似卡死 → 重启调度器」入口：服务端按红态口径复核（fail-closed），
    通过后写 agent_events 留痕并 kickstart 调度器。body {req_id, agent_session_id, confirm: true}。
    与需求助手 tool_restart_dispatcher 的有意差异：**不做**「有运行中 kimi agent 即拒」预检——
    卡死现场恰有一个活着的 kimi 进程，预检会让入口永远不可用；安全阀 = 服务端复核红态双条件
    （心跳与事件流双静默）+ 前端二次确认 + 事件留痕。已知代价：kickstart -k 不回收孤儿 agent，
    会中断并行会话（#374 下次启动清算为 interrupted，可人工续跑），人工处置见 docs/dispatcher.md。"""
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify({'error': '缺少二次确认（confirm 必须为 true）'}), 400
    try:
        req_id = int(data.get('req_id'))
        asid = int(data.get('agent_session_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'req_id / agent_session_id 必填且为整数'}), 400
    with resolve("get_db")() as conn:
        row = conn.execute("SELECT * FROM agent_sessions WHERE id = ? AND req_id = ?",
                           (asid, req_id)).fetchone()
        if row is None:
            return jsonify({'error': '会话不存在或不属于该需求'}), 404
        # 最后事件取 DB 最新一条（服务端权威，不受前端 500 条窗口限制）
        ev = conn.execute(
            "SELECT ts FROM agent_events WHERE req_id = ? AND (session_id = ? OR session_id IS NULL)"
            " ORDER BY id DESC LIMIT 1", (req_id, row['session_id'])).fetchone()
    now = time.time()
    if row['status'] != 'running':
        return jsonify({'error': f"会话状态为 {row['status']}（非运行中），无需重启"}), 400
    if (row['turn_step'] or '') != 'llm':
        return jsonify({'error': '会话不在等模型响应阶段（工具执行中不判卡死），拒绝重启'}), 400
    beats = [row[k] for k in ('last_llm_request_at', 'last_llm_response_at',
                              'last_thought_at', 'last_tool_started_at') if row[k] is not None]
    if not beats:
        return jsonify({'error': '会话心跳缺失（历史行），无法证明卡死，拒绝重启'}), 400
    ages = [now - float(b) for b in beats]
    ev_ms = _ts_ms(ev['ts']) if ev else None
    if ev_ms is not None:
        ages.append(now - ev_ms / 1000.0)
    quiet = min(ages)   # 最近一条活性证据（心跳/事件流）距现在
    if quiet <= resolve("AGENT_STALL_SEC"):
        return jsonify({'error': f'最近活性证据在 {int(quiet)}s 前'
                                 f'（阈值 {resolve("AGENT_STALL_SEC")}s），未判定卡死，拒绝重启'}), 400
    try:
        res = resolve("_restart_dispatcher")('web')
    except ValueError as e:
        return jsonify({'error': str(e)}), 502
    # 留痕写「重启发起成功之后」：本事件本身会被下一次守卫的「最近事件」读到，写在前面会让
    # 失败尝试后 180s 内的重试被自己的留痕拒掉（且前端事件流证据变新鲜、红态按钮消失）
    with resolve("get_db")() as conn:
        conn.execute("INSERT INTO agent_events (req_id, kind, content, session_id, ts)"
                     " VALUES (?, 'system', ?, ?, ?)",
                     (req_id, f"网页触发重启调度器（>{resolve('AGENT_STALL_SEC')}s 无响应/无输出/无思考"
                              f"且事件流静默；会话 {row['session_id'] or asid}）",
                      row['session_id'], resolve("now_str")()))
        conn.commit()
    resolve("_sse_publish")('proc', req_id=req_id)   # #416 发布点：事件流新增
    return jsonify(res)


@bp.route('/api/artifacts', methods=['GET'])
def api_artifacts():
    """各需求阶段主产物 + diff 元数据 {req_id: [{role, filename, short, path, exists, mtime, source}]}，
    供卡片会话信息栏产物行渲染（diff=#383 页面查看 dev 分支 diff：作为 dev 会话产物行挂在其会话行下）。
    纯只读；path 为命中根绝对路径（悬停 title 用），short 恒为 project/req-<id>/<文件名>（产物行展示用）。
    #53 候选顺序：当前根 → legacy 根（REQ_BOARD_ARTIFACTS_LEGACY，只读兜底）；source=current|legacy，
    缺失为 null 且 path 回落当前根期望路径（缺失提示用），前端不自行拼路径/不逐文件探测"""
    with resolve("get_db")() as conn:
        rows = conn.execute("SELECT id, project FROM requirements").fetchall()
    out = {}
    for r in rows:
        entries = []
        for role, fn in resolve("ARTIFACT_FILES").items():
            source, p = None, None
            for _root, cand, cand_source in artifact_candidates(r["project"], r["id"], fn):
                try:
                    if cand.exists():
                        source, p = cand_source, cand
                        break
                except OSError:
                    continue
            if source is None:
                p = resolve("ARTIFACTS_ROOT") / r["project"] / f"req-{r['id']}" / fn
            # #287 产物创建时间：取磁盘文件 mtime（Unix 秒）；文件不存在/stat 竞态失败（删文件）时为 null
            try:
                mtime = int(p.stat().st_mtime) if source else None
            except OSError:
                mtime = None
            entries.append({"role": role, "filename": fn,
                            "short": f"{r['project']}/req-{r['id']}/{fn}",
                            "path": str(p), "exists": source is not None,
                            "mtime": mtime, "source": source})
        out[str(r["id"])] = entries
    return jsonify(out)


@bp.route('/api/stream')
def api_stream():
    """页面事件流。参数 req（当前查看的需求 id）决定 proc/reqchat 的订阅过滤；session（执行过程
    面板当前会话 sid）当前不参与过滤：URL 形态保留（前端 sseStreamUrl 仍携带，服务端忽略）、订阅只记 req。
    帧格式 `data: {"topic": "init|proc|sessions|chat|reqchat", "req_id", "chat_id", "seq", "ts"}`（req_id/chat_id 按发布方传入可选携带）；
    每 _SSE_HEARTBEAT_SEC 秒发注释行 `: ping` 保活。503 = 服务端开关关闭或连接数超上限（前端降级为 1 秒轮询）。"""
    if not resolve("_SSE_ENABLED"):
        return jsonify({'error': 'SSE 已关闭（REQ_BOARD_SSE=0）'}), 503
    try:
        req_id = int(request.args.get('req')) if request.args.get('req') else None
    except (TypeError, ValueError):
        req_id = None
    sock = request.environ.get('werkzeug.socket')   # 非标准 WSGI 键（werkzeug 注入）；取不到 → None
    conn = _sse_acquire(req_id, sock)
    if conn is None:
        return jsonify({'error': f'SSE 连接数已达上限（{resolve("_SSE_MAX_CONNS")}）'}), 503

    def gen():
        reason = None   # 释放原因（'probe' = fd 探测发现断开），供 /api/sse-stats 区分计数
        try:
            yield 'retry: 3000\n\n'   # 瞬时失败由浏览器按 3s 自动重连（验收 3 的 ≤3s 通道）
            yield 'data: ' + json.dumps({'topic': 'init', 'seq': 0, 'ts': time.time()}) + '\n\n'
            last_ping = time.monotonic()
            while True:
                try:
                    payload = conn.queue.get(timeout=resolve("_SSE_PROBE_SEC"))
                except queue.Empty:
                    # 空转片（#417）：每 _SSE_PROBE_SEC 醒一次做不写流的 fd 级探测——对端 FIN/RST 即结束
                    # 生成器、finally 释放槽位，不必等一个心跳周期。缩短 queue.get 超时只是加密探测节拍，
                    # 超时本身不写流、发现不了断开。已知限制：半开连接（无 FIN/RST）的释放依赖下一次
                    # yield——写入失败（poke 帧或心跳注释行；注释行受 _SSE_HEARTBEAT_SEC 闸门，非每片都
                    # yield）才走 finally，释放窗口上限一个心跳周期。前端 30s 退避 + 1 秒轮询降级兜底不变。
                    if _sse_peer_gone(sock):
                        reason = 'probe'
                        return
                    if time.monotonic() - last_ping >= resolve("_SSE_HEARTBEAT_SEC"):
                        last_ping = time.monotonic()
                        yield ': ping\n\n'   # 心跳注释行（非 data 帧，前端忽略）
                    continue
                yield 'data: ' + json.dumps(payload) + '\n\n'
        finally:
            conn.release(reason)   # 幂等；与 _SSEStream.close() 双路径兜底

    resp = Response(stream_with_context(_SSEStream(gen(), conn)), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Accel-Buffering'] = 'no'   # 防中间层缓冲
    return resp


@bp.route('/api/sse-stats')
def api_sse_stats():
    """SSE 订阅状态只读快照（#417）：当前槽位/上限/心跳与探测节拍/发布序号/累计建立·释放·拒绝数
    与按订阅需求的槽位分布；供验收与排障读取（不占槽、不写 DB、无鉴权同全站约定）。"""
    return jsonify(_sse_stats_snapshot())


@bp.route('/')
def index():
    page = render_template('board.html', sse_enabled=resolve("_SSE_ENABLED"),
                                  agent_stall_sec=resolve("AGENT_STALL_SEC"),
                                  agent_stall_first_sec=resolve("AGENT_STALL_FIRST_FRAME_SEC"),
                                  thought_frames_absent=resolve("THOUGHT_FRAMES_ABSENT"),
                                  # #33 外链地址带内容指纹：改一字节重启后地址必变，旧缓存自然失效
                                  board_css_href=_asset_href("board.css"),
                                  board_js_href=_asset_href("board.js"),
                                  i18n_js_href=_asset_href("i18n.js"),
                                  i18n_dict_href=_asset_href("i18n.en.js"))
    if resolve("FREEZE_TS") is not None:
        # 仅冻结时钟开启时注入（缺省响应逐字节不变）：须在页面任何脚本前执行
        page = page.replace("<head>", "<head>" + resolve("_freeze_time_script")(), 1)
    return page


@bp.route('/guide/diagrams/<name>')
def guide_diagram(name):
    """/guide「架构与调度流程图」tab 的 iframe 源：archify 生成的独立 HTML（docs/diagrams/，源 JSON 在其 src/）。
    白名单固定文件名：本服务无鉴权，不开放目录读取。"""
    if name not in ("loop-architecture.html", "loop-dispatch-flow.html"):
        return "图不存在", 404
    path = Path(resolve("GUIDE_HTML")).parent / "diagrams" / name
    if not path.is_file():
        return "图不存在", 404
    return Response(path.read_bytes(), mimetype="text/html")


@bp.route('/guide')
def guide():
    """操作指南：读 docs/guide.html 并在「九、内置 agent 定义」锚点处注入动态版块（#269）。
    提示词动态读取（单一事实来源）：调度器角色提示词 import dispatcher 取 ROLE_PROMPTS/COMMON_TAIL，
    建卡对话提示词取本进程 _chat_system_prompt；import 失败时版块降级为错误提示，页面其它内容不受影响"""
    if not resolve("GUIDE_HTML").exists():
        return "操作指南不存在", 404
    page_html = resolve("GUIDE_HTML").read_text()
    page_html = page_html.replace(AGENTS_SECTION_ANCHOR, _builtin_agents_section())
    return Response(page_html, mimetype="text/html")
