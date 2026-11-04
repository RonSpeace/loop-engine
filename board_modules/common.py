"""共享纯函数与状态常量（#32 拆分 Phase 1）。

FREEZE_* / NODE_TYPES / ROLE_CAPABILITIES 经 runtime.resolve 晚绑定读取 board 当前值。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from storage_time import format_storage_time, parse_storage_time
from pathlib import Path

from flask import request

from board_modules.runtime import resolve


def _now_ts():
    """服务端「现在」的 epoch 秒：冻结时钟开启时返回固定时刻，否则 time.time()。"""
    return resolve("FREEZE_TS") if resolve("FREEZE_TS") is not None else time.time()

def now_str():
    """按存储时区生成时间字符串，与 SQLite CURRENT_TIMESTAMP 格式一致"""
    if resolve("FREEZE_TIME_STR"):
        return resolve("FREEZE_TIME_STR")
    return format_storage_time()

def _ts_ms(ts):
    """存储时区字符串（now_str 格式）→ epoch 毫秒；缺失/解析失败返回 None"""
    if not ts:
        return None
    try:
        return int(parse_storage_time(ts).timestamp() * 1000)
    except Exception:
        return None

def _meta_str(v, limit=100):
    """建卡来源元数据（source/model/reasoning_effort/source_session_id）清洗：
    None/空 → None；字符串化 + 截断，透传自由字段不做枚举校验（harness 无关）"""
    if v is None:
        return None
    s = str(v).strip()
    return s[:limit] or None

def _header_str(v):
    """HTTP 头进入 WSGI environ 时按 latin-1 解码，UTF-8 中文会显示成乱码；
    尝试按字节还原 UTF-8（纯 ASCII 原样；非 UTF-8 字节序列/真中文 str 均原样返回）。
    浏览器 fetch 禁止发送非 ISO-8859-1 头值（中文 X-Actor 会直接抛 TypeError），
    前端约定中文部分 encodeURIComponent 后再发，此处对含 % 的值追加百分号解码还原中文"""
    s = (v or '').strip()
    try:
        s = s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    if '%' in s:
        try:
            s = urllib.parse.unquote(s)
        except (UnicodeDecodeError, ValueError):
            pass
    return s

def _status_log_meta():
    """#375 变更来源解析：X-Actor 头（dispatcher / manual:<操作名> / agent:<来源>）→ actor 桶 = 冒号前段，
    无头归 api。detail 记录头完整值与 User-Agent 供兜底识别（本机无鉴权，头可伪造，属约定+快速定位机制）"""
    x_actor = _header_str(request.headers.get('X-Actor'))[:200]
    ua = _header_str(request.headers.get('User-Agent'))[:300]
    actor = x_actor.split(':', 1)[0].strip()[:50] if x_actor else 'api'
    parts = []
    if x_actor:
        parts.append(f"X-Actor={x_actor}")
    if ua:
        parts.append(f"UA={ua}")
    return actor, '; '.join(parts)

_SESSION_ORIGINS = ('manual', 'agent', 'api', 'dispatcher')

def _origin_bucket(actor):
    """actor 桶 → 注册行 origin：命中封闭枚举原样返回，其余（含无头 'api'、未知桶）一律 'api'。"""
    a = (actor or '').strip()
    return a if a in _SESSION_ORIGINS else 'api'

STATUS_LABEL_CN = {"later": "暂缓", "open": "待开发", "route": "通道待确认", "doing": "进行中",
                   "review": "评审中", "done": "已完成", "dropped": "已废弃"}

def _nodes_snapshot_error(raw):
    """#419：校验 /api PATCH 传入的 nodes_json 快照（JSON 数组；元素 role ∈ 配置 roles、
    node_type ∈ resolve("NODE_TYPES")、terminal 唯一且末位）。返回错误文案，None = 合法。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return 'nodes_json 不是合法 JSON'
    if not isinstance(raw, list) or not raw:
        return 'nodes_json 必须是非空数组'
    terminals = 0
    for i, node in enumerate(raw):
        if not isinstance(node, dict):
            return f'nodes_json[{i}] 必须是对象'
        ntype = node.get('node_type')
        if ntype not in resolve("NODE_TYPES"):
            return f'nodes_json[{i}].node_type 非法: {ntype!r}'
        if ntype == 'human':
            return f'nodes_json[{i}] human 节点本期未实现（仅预留枚举位）'
        if ntype == 'terminal':
            terminals += 1
            if i != len(raw) - 1:
                return 'nodes_json terminal 必须是末节点'
            if node.get('role'):
                return f'nodes_json[{i}] terminal 是调度器动作节点，不得配 role'
            continue
        role = node.get('role')
        if not role:
            return f'nodes_json[{i}].role 缺失（非 terminal 节点必须指定 agent 角色）'
        if role not in resolve("ROLE_CAPABILITIES"):
            return f'nodes_json[{i}].role 非法: {role!r}'
    if terminals != 1:
        return f'nodes_json 必须恰有一个 terminal 节点，实际 {terminals} 个'
    return None

def _snapshot_node(req):
    """#419：卡上流程快照的当前节点（nodes_json + node_index）。未入流程/快照缺失或非法 → None。
    只读卡片数据、不查配置（推进只认快照，流程改名/删除不影响进行中的卡）。"""
    try:
        raw, idx = req["nodes_json"], req["node_index"]
    except (KeyError, IndexError, TypeError):
        return None
    if not raw or idx is None:
        return None
    try:
        nodes = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(nodes, list) or not isinstance(idx, int) or not 0 <= idx < len(nodes):
        return None
    node = nodes[idx]
    return node if isinstance(node, dict) else None

def _status_label_cn(s):
    """状态 → 「中文(原文)」，用于回放 status 事件 content；未知值原样返回兜底"""
    if s in STATUS_LABEL_CN:
        return f"{STATUS_LABEL_CN[s]}({s})"
    return str(s)

# ==================== #53 历史产物根（只读兜底） ====================

def parse_artifact_legacy_roots(raw, current):
    """解析 REQ_BOARD_ARTIFACTS_LEGACY：os.pathsep 分隔多根，支持 ~，去空白/空项/重复，
    与当前根（resolve 后）相同的项剔除、保持顺序。None/空串 → []（关闭兜底，行为与改动前一致）。
    不存在的目录保留为候选：逐文件判定，不在启动期阻断。"""
    if not raw:
        return []
    try:
        current_key = Path(current).expanduser().resolve()
    except (OSError, RuntimeError):
        current_key = None
    out = []
    for item in str(raw).split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        root = Path(item).expanduser()
        try:
            key = root.resolve()
        except (OSError, RuntimeError):
            key = root
        if current_key is not None and key == current_key:
            continue
        if key in out:
            continue
        out.append(key)
    return out

def artifact_candidates(project, req_id, filename):
    """产物候选 [(root, path, source)]：当前根 → legacy 根（#53，当前根优先）。
    project 为绝对路径/含 .. 时不产出（聚合读的廉价防护；内容端点的完整校验在 resolve_artifact）。"""
    if not project or not filename:
        return []
    project = str(project)
    if os.path.isabs(project) or '..' in Path(project).parts:
        return []
    rel = Path(project) / f"req-{req_id}" / str(filename)
    roots = [(resolve("ARTIFACTS_ROOT"), "current")]
    roots += [(root, "legacy") for root in (resolve("ARTIFACTS_LEGACY_ROOTS") or [])]
    out, seen = [], set()
    for root, source in roots:
        try:
            key = Path(root).resolve()
        except (OSError, RuntimeError):
            key = Path(root)
        if key in seen:
            continue
        seen.add(key)
        out.append((Path(root), Path(root) / rel, source))
    return out

def resolve_artifact(project, req_id, filename):
    """安全级产物解析：逐候选根 resolve() + 需求目录/目标文件双 is_relative_to，
    返回首个存在的 (命中根, 目标绝对路径, "current"|"legacy")；全不命中 → (None, 当前根期望路径, None)。
    内容端点复用，新增 legacy 根不放宽路径安全面。"""
    fallback = None
    for root, path, source in artifact_candidates(project, req_id, filename):
        if fallback is None:
            fallback = path
        try:
            root_r = root.resolve()
            req_dir = (root / str(project) / f"req-{req_id}").resolve()
            if not req_dir.is_relative_to(root_r):
                continue
            target = (req_dir / str(filename)).resolve()
            if not target.is_relative_to(req_dir):
                continue
        except (OSError, RuntimeError):
            continue
        if target.exists():
            return root_r, target, source
    return None, fallback, None
