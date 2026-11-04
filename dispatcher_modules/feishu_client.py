"""飞书机器人：凭据读取、客户端初始化、状态上报。"""

import hashlib
import json
import os
from pathlib import Path

import notifiers

from dispatcher_modules.feishu_assistant import _feishu_assistant_reconcile
from dispatcher_modules.heartbeat import _update_feishu_status
from dispatcher_modules.runtime import D, log


feishu = None
# #23：进程内最近加载的 app_secret，**仅用于错误文案脱敏**（不落盘、不入日志、不进心跳）
_FEISHU_SECRET = None


def _sanitize_feishu_error(text, secret=None):
    """#23 错误文案脱敏：替换 app_secret 明文、去掉换行、截断 300 字。异常文本一律经此出口。"""
    s = "" if text is None else str(text)
    for sec in (secret, D._FEISHU_SECRET):
        if sec:
            s = s.replace(sec, "***")
    return s.replace("\r", " ").replace("\n", " ")[:300]

# #22 飞书凭据单一源：仓库根 feishu.json = {"app_id","app_secret"}（0600，gitignore，设置抽屉维护）。
# REQ_BOARD_FEISHU_JSON 可覆盖（测试隔离）。dispatcher 仅启动时读一次，改凭据须重启调度器。
FEISHU_CONFIG_FILE = (Path(os.environ["REQ_BOARD_FEISHU_JSON"]).expanduser()
                      if os.environ.get("REQ_BOARD_FEISHU_JSON") else D.REPO_DIR / "feishu.json")
# #22：会话 boot 完成后才置 idle（board `_chat_boot` 异步），建会话后须轮询等待该窗口
FEISHU_BOOT_WAIT_SEC = int(os.environ.get("REQ_BOARD_FEISHU_BOOT_WAIT_SEC", "180"))
# #22：飞书建卡单轮发送上限（与 board 侧建卡会话轮次超时对齐；等待原语 D8‴/D11 复用）
CHAT_TIMEOUT = int(os.environ.get("REQ_BOARD_CHAT_TIMEOUT_SEC", "600"))
# #22：空闲飞书建卡会话兜底释放（第三条释放路径）；0 = 关闭兜底；巡检节流间隔（秒）
FEISHU_CHAT_IDLE_SEC = int(os.environ.get("REQ_BOARD_FEISHU_CHAT_IDLE_SEC", "86400"))
FEISHU_IDLE_SWEEP_INTERVAL = int(os.environ.get("REQ_BOARD_FEISHU_IDLE_SWEEP_SEC", "300"))
FEISHU_TEXT_CHUNK_SIZE = int(os.environ.get("REQ_BOARD_FEISHU_TEXT_CHUNK", "2000"))  # 单条飞书文本上限
FEISHU_TEXT_MAX_CHUNKS = 5                                                            # 最多分几条
FEISHU_PROMPT_MAX = 4000   # board 侧 /prompt 的 text 上限（含抓来的文档正文），超限先截断
# #23 首连超时兜底：SDK 异常路径卡住时仍要给出「连接失败：原因」（`_feishu_connect_watchdog`）
FEISHU_CONNECT_TIMEOUT_SEC = int(os.environ.get("REQ_BOARD_FEISHU_CONNECT_TIMEOUT_SEC", "60"))
# #24 飞书看板助手：单条消息的总 deadline（worker 起表 → 传剩余秒数给 board；board 权威计时收尾，
# dispatcher 只做超时兜底与明确回复）。与建卡 CHAT_TIMEOUT（600s）语义完全隔离。
FEISHU_ASSISTANT_DEADLINE_SEC = int(os.environ.get("REQ_BOARD_FEISHU_ASSISTANT_DEADLINE_SEC", "90"))
# 轮询间隔（事件 + 状态）
FEISHU_ASSISTANT_POLL_SEC = float(os.environ.get("REQ_BOARD_FEISHU_ASSISTANT_POLL_SEC", "1.5"))
# per-chat 已处理 message_id 的保留窗口（去重键 = feishu_chat_id + message_id）
FEISHU_ASSISTANT_SEEN_WINDOW = int(os.environ.get("REQ_BOARD_FEISHU_ASSISTANT_SEEN", "64"))
# 90s deadline 到点、board 未收尾时的固定回发文案（同轮只发一次）
FEISHU_ASSISTANT_TIMEOUT_TEXT = "这轮没查完，重发一次或到网页看。"
# 助手链路不可用/失败时的降级提示前缀（不静默丢消息）
FEISHU_ASSISTANT_DOWN_HINT = "看板助手暂时不可用，稍后重发一次"
FEISHU_ASSISTANT_BUSY_HINT = "助手上一轮仍在收尾，请重发一次"
# #24 无活跃旧建卡会话时「结束建卡」「确认建卡」的状态提示（只回提示，不启动任何会话）
FEISHU_NO_SESSION_END_HINT = "当前没有进行中的建卡会话。"
FEISHU_NO_SESSION_CONFIRM_HINT = "还没有草稿。回复『新建建卡』开始建卡会话。"
# #24 无活跃旧会话时「新建建卡」的起手提示（随后未定项目由旧链路追问项目清单）
FEISHU_NEW_BUILD_HINT = "好的，请把需求内容发过来；已确定项目时直接回复项目名（或序号）。"
# #24 助手模型配置（feishu.json 的可选 assistant_model）：未配置/非法 = None（走 board 四层默认），
# 解析原因（非敏感）留痕，供日志与测试断言；app_secret 永不进入这两个全局。
FEISHU_ASSISTANT_MODEL = None
FEISHU_ASSISTANT_MODEL_REASON = None


def feishu_credential_fingerprint(app_id, app_secret):
    """#23 凭据指纹（board/dispatcher **逐字同源**的唯一定值，见 tech §4.1）：
    sha256(app_id + b"\\x00" + app_secret).hexdigest()[:16]。**不做 strip、不做大小写归一化**；
    任一侧缺失/为空/非字符串 → None（不产出摘要）。给定值两侧逐字节相等。"""
    if not isinstance(app_id, str) or not isinstance(app_secret, str):
        return None
    if not app_id or not app_secret:
        return None
    return hashlib.sha256(app_id.encode("utf-8") + b"\x00"
                          + app_secret.encode("utf-8")).hexdigest()[:16]


def _feishu_credentials_source():
    """#23 凭据来源标识（非敏感，供界面显示加载自哪个文件）。"""
    return ("feishu.json（REQ_BOARD_FEISHU_JSON 覆盖）"
            if os.environ.get("REQ_BOARD_FEISHU_JSON") else "feishu.json")


def _read_feishu_config():
    """#23 统一凭据解析（唯一实现）：返回 (cfg|None, reason|None)。
    reason ∈ {missing, unreadable, invalid}——**「没读到凭据」绝不写成 env**（否则 board 会
    误判为用户显式停用，保存新凭据后进不了「尚未加载」态）。cfg={"app_id","app_secret"}。"""
    path = D.FEISHU_CONFIG_FILE
    try:
        if not path.exists():
            return None, "missing"
        text = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - 权限/IO 失败一律按不可读降级
        log.warning(f"飞书凭据读取失败（{path}）：{e}")
        return None, "unreadable"
    try:
        cfg = json.loads(text)
    except Exception:
        log.warning(f"飞书凭据解析失败（{path}）：JSON 非法")
        return None, "invalid"
    if not isinstance(cfg, dict):
        log.warning(f"飞书凭据结构不符（{path}）：顶层非对象")
        return None, "invalid"
    app_id = cfg.get("app_id")
    app_secret = cfg.get("app_secret")
    if (not isinstance(app_id, str) or not isinstance(app_secret, str)
            or not app_id or not app_secret):
        log.warning(f"飞书凭据不完整（{path}）：需要非空的 app_id 与 app_secret")
        return None, "invalid"
    return {"app_id": app_id, "app_secret": app_secret}, None


def load_feishu_config():
    """#22 读 feishu.json（REQ_BOARD_FEISHU_JSON 可覆盖，测试隔离）→ {app_id, app_secret}；
    缺失/非法 JSON/字段不全 → None（fail-soft，调用方降级为「飞书未启用」，绝不抛）。"""
    cfg, _reason = D._read_feishu_config()
    return cfg


def _read_feishu_assistant_model():
    """#24：读 feishu.json 的可选 assistant_model（**只取该字段**，app_secret 明文不离开本函数、
    不进日志/状态）。文件缺失/不可读/JSON 非法/字段缺失 → None（fail-soft，调用方按未配置处理）。"""
    try:
        if not D.FEISHU_CONFIG_FILE.exists():
            return None
        cfg = json.loads(D.FEISHU_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 读取/解析失败一律降级为未配置
        log.warning(f"飞书助手模型读取失败（{D.FEISHU_CONFIG_FILE}）：{e}")
        return None
    if not isinstance(cfg, dict):
        return None
    return cfg.get("assistant_model")


def _feishu_assistant_model_resolve(raw):
    """#24：assistant_model 静态校验（与角色模型同一别名池口径，只新增判定不改进既有权衡）。
    返回 (model|None, reason|None)：未配置 = (None, None)；非法/不在池/在池但不可用 = (None, 原因)；
    池不可用（无 config.toml / 解析失败）→ fail-open 原样采用 + 原因留痕（与 resolve_role_model 一致）。"""
    if raw is None:
        return None, None
    if not isinstance(raw, str) or not raw.strip():
        return None, "assistant_model 非字符串或为空，按未配置处理"
    alias = raw.strip()
    pool, _home, warning = D.model_alias_pool()
    if pool is None:
        return alias, f"别名池不可用，未校验 assistant_model（{warning}）"
    entry = pool.get(alias)
    if entry is None:
        return None, f"assistant_model「{alias}」不在模型别名池，按未配置处理"
    if not entry.get("usable", True):
        return None, f"assistant_model「{alias}」不可用（{entry.get('reason') or '未说明'}），按未配置处理"
    return alias, None


def init_feishu():
    """#22/#23 fail-soft：无凭据 / 凭据非法 → 返回 None + log.warning（不抛），并把
    enabled=false + disabled_reason（missing/unreadable/invalid）写入心跳飞书状态。成功返回
    {"app_id","app_secret"}、建好全局 feishu 客户端，并写 enabled=true/connected=false/connecting
    + 已加载凭据指纹。REQ_BOARD_NO_FEISHU=1 时直接跳过（不读文件），写 disabled_reason=env。
    #24：凭据可用时顺带解析可选 assistant_model（fail-soft + 原因留痕），并按 chat_id 对账
    持久化的助手本地状态（清理僵尸 running/cancelling，保留 session_id）。"""
    src = _feishu_credentials_source()
    if D.NO_FEISHU:
        log.warning("REQ_BOARD_NO_FEISHU=1，显式停用飞书通道（不建立长连接）" if D.LARK_AVAILABLE
                    else "未安装飞书 SDK（lark_oapi），停用飞书通道（不建立长连接）")
        _update_feishu_status(enabled=False, disabled_reason="env", connected=False,
                              phase="disabled", credentials_source=src,
                              credentials_fingerprint=None, last_error=None,
                              connected_at=None)
        return None
    cfg, reason = D._read_feishu_config()
    if not cfg:
        log.warning(f"飞书凭据未配置（{D.FEISHU_CONFIG_FILE}）：{reason}，不建立长连接")
        _update_feishu_status(enabled=False, disabled_reason=reason or "missing",
                              connected=False, phase="disabled", credentials_source=src,
                              credentials_fingerprint=None, last_error=None,
                              connected_at=None)
        return None
    D._FEISHU_SECRET = cfg["app_secret"]
    D.FEISHU_ASSISTANT_MODEL, D.FEISHU_ASSISTANT_MODEL_REASON = _feishu_assistant_model_resolve(
        _read_feishu_assistant_model())
    if D.FEISHU_ASSISTANT_MODEL_REASON:
        log.warning(f"飞书助手模型未生效：{D.FEISHU_ASSISTANT_MODEL_REASON}")
    elif D.FEISHU_ASSISTANT_MODEL:
        log.info(f"飞书助手模型：{D.FEISHU_ASSISTANT_MODEL}")
    D.feishu = D.lark.Client.builder().app_id(cfg["app_id"]).app_secret(cfg["app_secret"]).build()
    _update_feishu_status(enabled=True, disabled_reason=None, connected=False,
                          phase="connecting", credentials_source=src,
                          credentials_fingerprint=feishu_credential_fingerprint(
                              cfg["app_id"], cfg["app_secret"]),
                          last_error=None, connected_at=None)
    _feishu_assistant_reconcile()
    return cfg

# 需求卡片长度限制（#115）：先截断后转义，保证转义对象不超字段上限
CARD_TITLE_LIMIT = 80    # 卡片正文需求标题
CARD_BODY_LIMIT = 500    # 卡片正文动态内容（问题/摘要/失败原因）
# #30 需求卡清单卡片：渲染侧硬上限（防未来 board 放宽或脏数据把一页塞爆飞书卡片元素数）
ASSISTANT_CARD_MAX_ITEMS = 20    # 单页最多渲染的需求卡区块数

# 七类关键状态通知事件映射：template 颜色 + 事件标题（#115/#385）
# 事件标题单一源 = notifiers.EVENT_LABELS（飞书卡片、Webhook/Slack、设置页订阅项同源）
CARD_EVENTS = {
    "route": {"template": "blue", "title": notifiers.EVENT_LABELS["route"]},
    "review": {"template": "orange", "title": notifiers.EVENT_LABELS["review"]},
    "need_confirm": {"template": "orange", "title": notifiers.EVENT_LABELS["need_confirm"]},
    "done": {"template": "green", "title": notifiers.EVENT_LABELS["done"]},
    "failed": {"template": "red", "title": notifiers.EVENT_LABELS["failed"]},
    "blocked": {"template": "orange", "title": notifiers.EVENT_LABELS["blocked"]},
    "merge_conflict": {"template": "red", "title": notifiers.EVENT_LABELS["merge_conflict"]},
}

