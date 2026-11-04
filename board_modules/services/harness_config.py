"""#32 Phase 3：harness_config 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import HARNESS_PROVIDER_TYPES
from acp_shared import HARNESS_PROVIDER_TYPES_HINT
from acp_shared import context_size_problem
import copy
import os
import re

from board_modules.runtime import resolve
from board_modules.services.model_pool import _harness_config_path


class _CfgReject(Exception):
    """校验/形态拒绝 → 400（fail-closed，零写入）；message 不含任何密钥值。"""


def _harness_cfg_pool(raw):
    """别名池口径（#6，与 _model_alias_pool 的池部分同源）：models 表键 ∪ providers.<p>.model
    合成别名（p/upstream），保序去重。default_model 候选与该池同一口径。"""
    items = []
    models = raw.get("models")
    if isinstance(models, dict):
        items.extend(str(k) for k in models)
    providers = raw.get("providers")
    if isinstance(providers, dict):
        for pname, pcfg in providers.items():
            if isinstance(pcfg, dict) and isinstance(pcfg.get("model"), str) and pcfg["model"].strip():
                items.append(f"{pname}/{pcfg['model'].strip()}")
    seen, out = set(), []
    for m in items:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _harness_cfg_strict_read():
    """严格读（GET/POST 共用；POST 在锁内调用）。返回 (path, home, orig, text, raw, mode, error)。
    error ∈ {None, (404, payload), (400, payload)}：无文件 404（附候选 home 列表），
    不可读/非 UTF-8/tomllib 缺失/解析失败/顶层非表 400（只读不写、原文件不动）。"""
    path, home = _harness_config_path()
    if path is None:
        return None, None, None, None, None, None, (404, {
            "error": "未找到 config.toml（候选 harness home 均无该文件），拒绝写入",
            "homes": [str(h) for h in resolve("session_homes")()]})
    try:
        orig = path.read_bytes()
        st = path.stat()
    except OSError as e:
        return path, home, None, None, None, None, (400, {"error": f"config.toml 不可读：{e}"})
    try:
        text = orig.decode("utf-8")
    except UnicodeDecodeError:
        return path, home, None, None, None, None, (400, {"error": "config.toml 非 UTF-8 编码，拒绝处理"})
    try:
        import tomllib
    except ImportError:
        return path, home, None, None, None, None, (400, {"error": "当前 Python 无 tomllib（需 ≥3.11），拒绝处理"})
    try:
        raw = tomllib.loads(text)
    except Exception as e:  # noqa: BLE001 - 错误只带行列号，不带字段值
        return path, home, None, None, None, None, (400, {"error": f"config.toml 解析失败：{e}"})
    if not isinstance(raw, dict):
        return path, home, None, None, None, None, (400, {"error": "config.toml 结构不符（顶层非表），拒绝处理"})
    return path, home, orig, text, raw, st.st_mode & 0o7777, None


def _toml_render_string(s):
    """TOML 基本字符串渲染：转义 \\ / " 与控制字符（\\u00XX）；非 ASCII 原样。"""
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 32 or ord(ch) == 0x7f:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_render_value(v):
    """值渲染（#18）：int（非 bool）裸整数输出（max_context_size 必须为 TOML 整数，写成字符串
    会被 harness 拒绝）；其余一律走字符串渲染，与改动前逐字一致。"""
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return _toml_render_string(v)


def _toml_render_key(k):
    """键渲染：裸键（[A-Za-z0-9_-]+）原样，其余用双引号键（含 / . 空格等）。"""
    if re.fullmatch(r'[A-Za-z0-9_-]+', k):
        return k
    return _toml_render_string(k)


_CFG_KEYLINE_RE = re.compile(
    r'^\s*(?P<k>[A-Za-z0-9_\-]+|"(?:[^"\\]|\\.)*"|\'[^\']*\')\s*=')


def _cfg_decode_key(raw):
    """行内键原文（裸/双引号/单引号）→ 实际键名；解析不出 → None。"""
    if raw.startswith('"'):
        try:
            import tomllib
            d = tomllib.loads(raw + " = 0")
            return next(iter(d)) if d else None
        except Exception:  # noqa: BLE001
            return None
    if raw.startswith("'"):
        return raw[1:-1]
    return raw


def _cfg_header_keys(line):
    """表头行 → 键路径 tuple；非表头/解析不出 → None；数组表头 [[x]] 返回哨兵（仅作段边界）。"""
    s = line.lstrip()
    if not s.startswith('['):
        return None
    if s.startswith('[['):
        return ('__array__',)
    try:
        import tomllib
        parsed = tomllib.loads(line)
    except Exception:  # noqa: BLE001
        return None
    keys, node = [], parsed
    while isinstance(node, dict) and len(node) == 1:
        k = next(iter(node))
        keys.append(k)
        node = node[k]
    return tuple(keys) if keys else None


def _cfg_find_section(lines, keys):
    """定位段 [keys]：返回 (start, end) 行区间（含段头、不含下一段头）；未找到 → None。"""
    start = None
    for i, line in enumerate(lines):
        k = _cfg_header_keys(line)
        if k is None:
            continue
        if start is None:
            if k == keys:
                start = i
        else:
            return start, i
    return (start, len(lines)) if start is not None else None


def _cfg_find_key_line(lines, start, end, key):
    """段内定位键行（裸键/引号键的单行 'key =' 形态，不匹配 dotted-key/跨行值）；未找到 → None。"""
    for i in range(start + 1, end):
        m = _CFG_KEYLINE_RE.match(lines[i])
        if m and _cfg_decode_key(m.group('k')) == key:
            return i
    return None


def _cfg_ok_value(v, max_len, allow_space=False):
    """strip 后校验并返回规范化值：非空、长度 ≤ max_len；allow_space=False 时禁空白与控制字符，
    否则仅禁控制字符。非法 → None。"""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) > max_len:
        return None
    if allow_space:
        if any(ord(ch) < 32 for ch in s):
            return None
    else:
        if any(ch.isspace() or ord(ch) < 32 for ch in s):
            return None
    return s


def _cfg_ok_pos_int(v):
    """max_context_size 入参校验（#18）：整数（非 bool）且 ≥1 → 返回 int；否则 None。
    专属函数，不改只收字符串的 _cfg_ok_value 语义。"""
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if v >= 1 else None


def _cfg_ok_key(v):
    """API KEY 值校验：**首尾空白去除后写入**（防误贴空白密钥覆盖有效 key 导致认证失败，
    评审建议 5 二选一取 strip 存储）；strip 后须非空、长度 ≤4096、无控制字符。非法 → None。"""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) > 4096:
        return None
    if any(ord(ch) < 32 for ch in s):
        return None
    return s


def _harness_cfg_apply(raw, body):
    """校验请求 + 施加 ops → (want, intents)。非法一律 raise _CfgReject（400）。
    ops 固定顺序：providers_add → providers_update → models_add → models_update → models_delete
    → providers_delete → default_model（#13 在 models_add 后插入 models_update）
    → default_model；删除引用守卫与 default_model 悬空判定按当时最终态求值（显式 default_model 最后应用、
    覆盖改名联动改写）。intents 供行级拼接定位，锚点 = 文件中的段名（改名前的旧名）。"""
    want = copy.deepcopy(raw)
    allowed = ("providers_add", "providers_update", "providers_delete",
               "models_add", "models_update", "models_delete", "default_model")
    unknown = [k for k in body if k not in allowed]
    if unknown:
        raise _CfgReject(f"未知字段：{', '.join(sorted(str(k) for k in unknown))}")

    def _list_of(key, kind):
        v = body.get(key)
        if v is None:
            return []
        if not isinstance(v, list):
            raise _CfgReject(f"{key} 需为数组")
        for item in v:
            if kind == 'obj' and not isinstance(item, dict):
                raise _CfgReject(f"{key} 的元素需为对象")
            if kind == 'str' and (not isinstance(item, str) or not item.strip()):
                raise _CfgReject(f"{key} 的元素需为非空字符串")
        return v

    def _keys_ok(obj, allowed_keys, what):
        extra = [k for k in obj if k not in allowed_keys]
        if extra:
            raise _CfgReject(f"{what} 含未知字段：{', '.join(sorted(str(k) for k in extra))}")

    def _dup_targets(names, what):
        seen = set()
        for n in names:
            if n in seen:
                raise _CfgReject(f"{what} 存在重复目标 {n}")
            seen.add(n)

    def _prov_table():
        prov = want.get("providers")
        if prov is None:
            want["providers"] = prov = {}
        if not isinstance(prov, dict):
            raise _CfgReject("providers 非表结构，拒绝在线编辑")
        return prov

    def _models_table():
        models = want.get("models")
        if models is None:
            want["models"] = models = {}
        if not isinstance(models, dict):
            raise _CfgReject("models 非表结构，拒绝在线编辑")
        return models

    intents = []
    added_providers = []          # [{name, base_url, type, key}] 待文件尾追加（可被同单改名/字段更新原地改写）
    added_models = []             # [{alias, provider, model, max_context_size}]
    pending_prov = {}             # name -> added_providers 里的条目
    pending_model = {}            # alias -> added_models 里的条目
    touched_models = set()        # #18：被 models_add / models_update 作用过的别名（最终态完整性校验用）
    text_names = {}               # want 名 -> 文件中的原段名（改名后仍指旧段头）
    dm_intent_value = None        # 有值 = 需要落盘的 default_model 新值

    def _text_name(name):
        return text_names.get(name, name)

    # 1) providers_add
    _dup_targets([str(it.get("name")) for it in _list_of("providers_add", 'obj')], "providers_add")
    for item in body.get("providers_add") or []:
        _keys_ok(item, {"name", "base_url", "type", "key"}, "providers_add")
        name = _cfg_ok_value(item.get("name"), 128)
        base = _cfg_ok_value(item.get("base_url"), 512, allow_space=True)
        ptype = _cfg_ok_value(item.get("type"), 64)
        if name is None:
            raise _CfgReject("新增厂商名称非法（非空、无空白/控制字符、≤128）")
        if base is None:
            raise _CfgReject(f"厂商 {name} 的 base_url 非法（非空、无控制字符、≤512）")
        if ptype is None:
            raise _CfgReject(f"厂商 {name} 的 type 非法（非空、无空白、≤64）")
        if ptype not in HARNESS_PROVIDER_TYPES:
            raise _CfgReject(f'厂商 {name} 的 type "{ptype}" 非法'
                             f"（合法值：{HARNESS_PROVIDER_TYPES_HINT}）")
        keyv = None
        if "key" in item and item["key"] not in ("", None):
            keyv = _cfg_ok_key(item["key"])
            if keyv is None:
                raise _CfgReject(f"厂商 {name} 的 key 非法（非空、≤4096、无控制字符）")
        prov = _prov_table()
        if name in prov:
            raise _CfgReject(f"厂商 {name} 已存在")
        entry = {"base_url": base, "type": ptype}
        if keyv is not None:
            entry["api_key"] = keyv
        prov[name] = entry
        added = {"name": name, "base_url": base, "type": ptype, "key": keyv}
        added_providers.append(added)
        pending_prov[name] = added

    # 2) providers_update（先改名后字段；pending 条目原地改写，不产生文本 intent）
    _dup_targets([str(it.get("name", "")).strip() for it in _list_of("providers_update", 'obj')], "providers_update")
    for item in body.get("providers_update") or []:
        _keys_ok(item, {"name", "new_name", "base_url", "type", "key", "clear_key"}, "providers_update")
        rawname = item.get("name")
        if not isinstance(rawname, str) or not rawname.strip():
            raise _CfgReject("providers_update.name 需为非空字符串")
        name = rawname.strip()
        prov = want.get("providers")
        if not isinstance(prov, dict) or name not in prov:
            raise _CfgReject(f"厂商 {name} 不存在")
        new_name = None
        if "new_name" in item:
            new_name = _cfg_ok_value(item["new_name"], 128)
            if new_name is None:
                raise _CfgReject("改名目标非法（非空、无空白/控制字符、≤128）")
        if "key" in item and "clear_key" in item:
            raise _CfgReject("key 与 clear_key 不能同时提交")
        # 改名
        if new_name is not None and new_name != name:
            target = prov.get(new_name)
            if target is not None:
                raise _CfgReject(f"厂商 {new_name} 已存在，改名冲突")
            renamed = {}
            for k2, v2 in prov.items():
                renamed[new_name if k2 == name else k2] = v2
            want["providers"] = prov = renamed
            is_pending = name in pending_prov
            text_names[new_name] = _text_name(name)
            if is_pending:
                pending_prov[new_name] = pending_prov.pop(name)
                pending_prov[new_name]["name"] = new_name   # 追加文本条目同步改名（与 want 一致）
            else:
                intents.append({"kind": "provider_rename", "anchor": text_names[new_name],
                                "new_name": new_name})
            # ① [models.*].provider == old → new（pending 条目原地改，存量条目走文本 intent）
            for alias, mcfg in (want.get("models") or {}).items() if isinstance(want.get("models"), dict) else []:
                if isinstance(mcfg, dict) and mcfg.get("provider") == name:
                    mcfg["provider"] = new_name
                    if alias in pending_model:
                        pending_model[alias]["provider"] = new_name
                    else:
                        intents.append({"kind": "model_provider_set", "alias": alias, "provider": new_name})
            # ② default_model == old/u 且改名后 old/u 失效、new/u 有效 → 同步改写（显式 op 最后覆盖）
            block = prov.get(new_name)
            u = block.get("model") if isinstance(block, dict) and isinstance(block.get("model"), str) else None
            if u and u.strip():
                old_alias, new_alias = f"{name}/{u.strip()}", f"{new_name}/{u.strip()}"
                if want.get("default_model") == old_alias:
                    pool_now = _harness_cfg_pool(want)
                    if old_alias not in pool_now and new_alias in pool_now:
                        want["default_model"] = new_alias
                        dm_intent_value = new_alias
            name_for_fields = new_name
        else:
            name_for_fields = name
        # 字段更新
        pend = pending_prov.get(name_for_fields)
        block = prov.get(name_for_fields)
        if pend is not None:
            if "base_url" in item:
                base = _cfg_ok_value(item["base_url"], 512, allow_space=True)
                if base is None:
                    raise _CfgReject(f"厂商 {name} 的 base_url 非法")
                pend["base_url"] = base
                block["base_url"] = base
            if "type" in item:
                tv = _cfg_ok_value(item["type"], 64)
                if tv is None:
                    raise _CfgReject(f"厂商 {name} 的 type 非法")
                if tv not in HARNESS_PROVIDER_TYPES:
                    raise _CfgReject(f'厂商 {name} 的 type "{tv}" 非法'
                                     f"（合法值：{HARNESS_PROVIDER_TYPES_HINT}）")
                pend["type"] = tv
                block["type"] = tv
            if "key" in item and item["key"] not in ("", None):
                kv = _cfg_ok_key(item["key"])
                if kv is None:
                    raise _CfgReject(f"厂商 {name} 的 key 非法")
                pend["key"] = kv
                block["api_key"] = kv
            if item.get("clear_key") is True:
                pend["key"] = None
                block.pop("api_key", None)
            elif "clear_key" in item and not isinstance(item["clear_key"], bool):
                raise _CfgReject("clear_key 需为布尔值")
        else:
            if not isinstance(block, dict):
                raise _CfgReject(f"厂商 {name} 结构不符（非表），拒绝在线编辑")
            anchor = _text_name(name_for_fields)
            if "base_url" in item:
                base = _cfg_ok_value(item["base_url"], 512, allow_space=True)
                if base is None:
                    raise _CfgReject(f"厂商 {name} 的 base_url 非法")
                if block.get("base_url") != base:
                    block["base_url"] = base
                    intents.append({"kind": "provider_field_set", "anchor": anchor,
                                    "field": "base_url", "value": base})
            if "type" in item:
                tv = _cfg_ok_value(item["type"], 64)
                if tv is None:
                    raise _CfgReject(f"厂商 {name} 的 type 非法")
                if tv not in HARNESS_PROVIDER_TYPES:
                    raise _CfgReject(f'厂商 {name} 的 type "{tv}" 非法'
                                     f"（合法值：{HARNESS_PROVIDER_TYPES_HINT}）")
                if block.get("type") != tv:
                    block["type"] = tv
                    intents.append({"kind": "provider_field_set", "anchor": anchor,
                                    "field": "type", "value": tv})
            if "key" in item and item["key"] not in ("", None):
                kv = _cfg_ok_key(item["key"])
                if kv is None:
                    raise _CfgReject(f"厂商 {name} 的 key 非法")
                if block.get("api_key") != kv:
                    block["api_key"] = kv
                    intents.append({"kind": "provider_field_set", "anchor": anchor,
                                    "field": "api_key", "value": kv})
            if "clear_key" in item:
                ck = item["clear_key"]
                if not isinstance(ck, bool):
                    raise _CfgReject("clear_key 需为布尔值")
                if ck and "api_key" in block:
                    block.pop("api_key", None)
                    intents.append({"kind": "provider_field_delete", "anchor": anchor, "field": "api_key"})

    # 3) models_add（provider 以施加 add/update 后的 want 为准：同单先建厂商再挂别名成立）
    # #18：max_context_size 必填（官方唯一必填字段）——缺失/非正整数一律 400 零写入。
    _dup_targets([str(it.get("alias", "")).strip() for it in _list_of("models_add", 'obj')], "models_add")
    for item in body.get("models_add") or []:
        _keys_ok(item, {"alias", "provider", "model", "protocol", "max_context_size"}, "models_add")
        alias = _cfg_ok_value(item.get("alias"), 128)
        provider = _cfg_ok_value(item.get("provider"), 128)
        model = _cfg_ok_value(item.get("model"), 128)
        if alias is None:
            raise _CfgReject("新增别名非法（非空、无空白/控制字符、≤128）")
        if provider is None:
            raise _CfgReject(f"模型 {alias} 的 provider 非法（非空、无空白/控制字符、≤128）")
        if model is None:
            raise _CfgReject(f"模型 {alias} 的上游 model 非法（非空、无空白/控制字符、≤128）")
        ctx = _cfg_ok_pos_int(item.get("max_context_size"))
        if ctx is None:
            raise _CfgReject(f"模型 {alias} 的 max_context_size 非法（必填，需为 ≥1 的整数）")
        proto = None
        if "protocol" in item and item["protocol"] not in ("", None):
            proto = _cfg_ok_value(item["protocol"], 64)
            if proto is None:
                raise _CfgReject(f"模型 {alias} 的 protocol 非法（非空、无空白/控制字符、≤64）")
        prov = want.get("providers")
        if not isinstance(prov, dict) or provider not in prov:
            raise _CfgReject(f"厂商 {provider} 不存在（请先创建厂商）")
        models = _models_table()
        if alias in models:
            raise _CfgReject(f"别名 {alias} 已存在")
        block = {"provider": provider, "model": model, "max_context_size": ctx}
        if proto is not None:
            block["protocol"] = proto
        models[alias] = block
        added = {"alias": alias, "provider": provider, "model": model, "max_context_size": ctx}
        if proto is not None:
            added["protocol"] = proto
        added_models.append(added)
        pending_model[alias] = added
        touched_models.add(alias)

    # 3.5) models_update（#13：给别名设/改/清 protocol；#18：设 max_context_size（必填，无清空语义）；
    #      文本面仅支持标准 [models.<alias>] 段形态）
    _dup_targets([str(it.get("alias", "")).strip() for it in _list_of("models_update", 'obj')], "models_update")
    for item in body.get("models_update") or []:
        _keys_ok(item, {"alias", "protocol", "clear_protocol", "max_context_size"}, "models_update")
        rawname = item.get("alias")
        if not isinstance(rawname, str) or not rawname.strip():
            raise _CfgReject("models_update.alias 需为非空字符串")
        alias = rawname.strip()
        models = want.get("models")
        block = models.get(alias) if isinstance(models, dict) else None
        if not isinstance(block, dict):
            raise _CfgReject(f"别名 {alias} 不存在")
        if "protocol" in item and "clear_protocol" in item:
            raise _CfgReject("protocol 与 clear_protocol 不能同时提交")
        pend = pending_model.get(alias)
        touched_models.add(alias)
        if "max_context_size" in item:
            ctx = _cfg_ok_pos_int(item["max_context_size"])
            if ctx is None:
                raise _CfgReject(f"模型 {alias} 的 max_context_size 非法（必填，需为 ≥1 的整数）")
            if block.get("max_context_size") != ctx:
                block["max_context_size"] = ctx
                if pend is not None:
                    pend["max_context_size"] = ctx
                else:
                    intents.append({"kind": "model_field_set", "alias": alias,
                                    "field": "max_context_size", "value": ctx})
        if "clear_protocol" in item:
            ck = item["clear_protocol"]
            if not isinstance(ck, bool):
                raise _CfgReject("clear_protocol 需为布尔值")
            if not ck or "protocol" not in block:
                continue
            block.pop("protocol", None)
            if pend is not None:
                pend.pop("protocol", None)
            else:
                intents.append({"kind": "model_field_delete", "alias": alias, "field": "protocol"})
            continue
        if "protocol" not in item:
            continue
        proto = _cfg_ok_value(item.get("protocol"), 64)
        if proto is None:
            raise _CfgReject(f"模型 {alias} 的 protocol 非法（非空、无空白/控制字符、≤64）")
        if block.get("protocol") == proto:
            continue
        block["protocol"] = proto
        if pend is not None:
            pend["protocol"] = proto
        else:
            intents.append({"kind": "model_field_set", "alias": alias,
                            "field": "protocol", "value": proto})

    # 4) models_delete
    _dup_targets(list(_list_of("models_delete", 'str')), "models_delete")
    for alias in body.get("models_delete") or []:
        alias = alias.strip()
        models = want.get("models")
        if not isinstance(models, dict) or alias not in models:
            raise _CfgReject(f"别名 {alias} 不存在")
        del models[alias]
        intents = [it for it in intents
                   if not (it.get("kind") in ("model_provider_set", "model_field_set",
                                              "model_field_delete")
                           and it.get("alias") == alias)]
        if alias in pending_model:
            added_models.remove(pending_model.pop(alias))   # 同单新增又删除 → 撤销新增
        else:
            intents.append({"kind": "model_delete", "alias": alias})

    # 5) providers_delete（最终态无引用才可删：models_delete 先于本步，满足「同单删厂商+删全部别名」）
    _dup_targets(list(_list_of("providers_delete", 'str')), "providers_delete")
    for name in body.get("providers_delete") or []:
        name = name.strip()
        prov = want.get("providers")
        if not isinstance(prov, dict) or name not in prov:
            raise _CfgReject(f"厂商 {name} 不存在")
        models = want.get("models") or {}
        refs = sorted(str(a) for a, m in models.items()
                      if isinstance(m, dict) and m.get("provider") == name) if isinstance(models, dict) else []
        if refs:
            raise _CfgReject(f"厂商 {name} 仍被 {len(refs)} 个模型引用（{'、'.join(refs[:5])}），"
                             "请先删除其模型别名")
        del prov[name]
        if name in pending_prov:
            added_providers.remove(pending_prov.pop(name))
        else:
            anchor = _text_name(name)
            intents = [it for it in intents
                       if not (it.get("kind") in ("provider_rename", "provider_field_set",
                                                  "provider_field_delete") and it.get("anchor") == anchor)]
            intents.append({"kind": "provider_delete", "anchor": anchor})

    # 6) default_model（显式提交优先于改名联动改写；悬空/空值一律拒绝，按最终 want 求值）
    if "default_model" in body:
        dv = body["default_model"]
        if not isinstance(dv, str):
            raise _CfgReject("default_model 需为字符串")
        dv = dv.strip()
        if not dv:
            raise _CfgReject("default_model 不能为空")
        if dv not in _harness_cfg_pool(want):
            raise _CfgReject(f"default_model {dv} 不在别名池（models 别名 ∪ 厂商 model 合成别名），已拒绝")
        want["default_model"] = dv
        dm_intent_value = dv

    # 最终不变量（D7）：存在且非空 ⇒ 必须 ∈ 别名池；悬空/空值 → 整单拒绝（含原文件已悬空的兜底）
    dm_final = want.get("default_model")
    if dm_final is not None:
        if not isinstance(dm_final, str) or not dm_final.strip():
            raise _CfgReject("default_model 为空/悬空，已拒绝保存（请修正后重试）")
        if dm_final not in _harness_cfg_pool(want):
            raise _CfgReject(f"default_model {dm_final} 不在别名池（models 别名 ∪ 厂商 model 合成别名），"
                             "已拒绝保存（请修正后重试）")

    # #18 最终不变量（读法 A）：被 models_add / models_update 作用过的条目必须字段完整
    # （max_context_size 缺失/非正整数 → 整单拒绝、零写入）。未触碰的存量不完整条目不阻断整单保存。
    # 同单新增又删除的别名（step 4 已移除）用 .get 跳过，不 KeyError（评审意见 3）。
    models_final = want.get("models")
    for alias in sorted(touched_models):
        blk = models_final.get(alias) if isinstance(models_final, dict) else None
        if not isinstance(blk, dict):
            continue
        problem = context_size_problem(blk.get("max_context_size"))
        if problem:
            raise _CfgReject(f"条目 {alias} 配置不完整：{problem}（请先补全 max_context_size 再保存）")

    if dm_intent_value is not None and dm_intent_value != raw.get("default_model"):
        intents.append({"kind": "default_model_set", "value": dm_intent_value})
    for p in added_providers:
        intents.append({"kind": "append_providers", **p})
    for m in added_models:
        intents.append({"kind": "append_models", **m})
    return want, intents


def _harness_cfg_splice(text, intents):
    """行级最小拼接：按 intents 生成新文本（未编辑行逐字节保留，行尾风格沿用文件）。
    目标锚点无法定位（非标准段/键形态）→ _CfgReject（400，提示直接编辑文件）。"""
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if "\r\n" in text else "\n"
    edits = []    # (start, end, [replacement lines])
    appends = []

    def _find_or_reject(keys, label):
        rng = _cfg_find_section(lines, keys)
        if rng is None:
            raise _CfgReject(f"条目 {label} 的文本形态不支持在线编辑（未找到对应段），请直接编辑 config.toml")
        return rng

    for it in intents:
        kind = it.get("kind")
        if kind == "provider_rename":
            rng = _find_or_reject(("providers", it["anchor"]), it["anchor"])
            new_line = f"[providers.{_toml_render_key(it['new_name'])}]" + eol
            edits.append((rng[0], rng[0] + 1, [new_line]))
        elif kind == "provider_field_set":
            rng = _find_or_reject(("providers", it["anchor"]), it["anchor"])
            line = f"{it['field']} = {_toml_render_string(it['value'])}" + eol
            idx = _cfg_find_key_line(lines, rng[0], rng[1], it["field"])
            if idx is None:
                edits.append((rng[0] + 1, rng[0] + 1, [line]))
            else:
                edits.append((idx, idx + 1, [line]))
        elif kind == "provider_field_delete":
            rng = _find_or_reject(("providers", it["anchor"]), it["anchor"])
            idx = _cfg_find_key_line(lines, rng[0], rng[1], it["field"])
            if idx is not None:
                edits.append((idx, idx + 1, []))
        elif kind == "provider_delete":
            rng = _find_or_reject(("providers", it["anchor"]), it["anchor"])
            edits.append((rng[0], rng[1], []))
        elif kind == "model_delete":
            alias = it["alias"]
            rng = _cfg_find_section(lines, ("models", alias))
            if rng is not None:
                edits.append((rng[0], rng[1], []))
            else:
                parent = _cfg_find_section(lines, ("models",))
                idx = _cfg_find_key_line(lines, parent[0], parent[1], alias) if parent else None
                if idx is None:
                    raise _CfgReject(f"条目 {alias} 的文本形态不支持在线编辑（未找到对应段），"
                                     "请直接编辑 config.toml")
                edits.append((idx, idx + 1, []))
        elif kind == "model_provider_set":
            rng = _find_or_reject(("models", it["alias"]), it["alias"])
            idx = _cfg_find_key_line(lines, rng[0], rng[1], "provider")
            if idx is None:
                raise _CfgReject(f"条目 {it['alias']} 的文本形态不支持在线编辑（provider 非单行键值），"
                                 "请直接编辑 config.toml")
            line = f"provider = {_toml_render_string(it['provider'])}" + eol
            edits.append((idx, idx + 1, [line]))
        elif kind == "model_field_set":
            rng = _find_or_reject(("models", it["alias"]), it["alias"])
            line = f"{it['field']} = {_toml_render_value(it['value'])}" + eol
            idx = _cfg_find_key_line(lines, rng[0], rng[1], it["field"])
            if idx is None:
                edits.append((rng[0] + 1, rng[0] + 1, [line]))
            else:
                edits.append((idx, idx + 1, [line]))
        elif kind == "model_field_delete":
            rng = _find_or_reject(("models", it["alias"]), it["alias"])
            idx = _cfg_find_key_line(lines, rng[0], rng[1], it["field"])
            if idx is not None:
                edits.append((idx, idx + 1, []))
        elif kind == "default_model_set":
            hdr = None
            for i, line in enumerate(lines):
                if _cfg_header_keys(line) is not None:
                    hdr = i
                    break
            region_end = hdr if hdr is not None else len(lines)
            idx = _cfg_find_key_line(lines, -1, region_end, "default_model")
            line = f"default_model = {_toml_render_string(it['value'])}" + eol
            if idx is None:
                pos = hdr if hdr is not None else len(lines)
                edits.append((pos, pos, [line]))
            else:
                edits.append((idx, idx + 1, [line]))
        elif kind in ("append_providers", "append_models"):
            appends.append(it)

    edits.sort(key=lambda e: (e[0], e[1]))
    prev_end = 0
    for s, en, _repl in edits:
        if s < prev_end:
            raise _CfgReject("编辑目标区间重叠，拒绝写入（请直接编辑 config.toml）")
        prev_end = en
    out, prev = [], 0
    for s, en, repl in edits:
        out.extend(lines[prev:s])
        out.extend(repl)
        prev = en
    out.extend(lines[prev:])
    new_text = "".join(out)
    if appends:
        block_lines = []
        for it in appends:
            if it["kind"] == "append_providers":
                block_lines.append(f"[providers.{_toml_render_key(it['name'])}]")
                block_lines.append(f"base_url = {_toml_render_string(it['base_url'])}")
                block_lines.append(f"type = {_toml_render_string(it['type'])}")
                if it.get("key") is not None:
                    block_lines.append(f"api_key = {_toml_render_string(it['key'])}")
            else:
                block_lines.append(f"[models.{_toml_render_key(it['alias'])}]")
                block_lines.append(f"provider = {_toml_render_string(it['provider'])}")
                block_lines.append(f"model = {_toml_render_string(it['model'])}")
                if it.get("max_context_size") is not None:
                    block_lines.append(f"max_context_size = {_toml_render_value(it['max_context_size'])}")
                if it.get("protocol"):
                    block_lines.append(f"protocol = {_toml_render_string(it['protocol'])}")
            block_lines.append("")
        block = "".join(l + eol for l in block_lines)
        if new_text:
            if not new_text.endswith("\n"):
                new_text += eol
            if not new_text.endswith(eol + eol):
                new_text += eol
        new_text += block
    return new_text


def _cfg_loopback_equal(want, parsed):
    """语义等值比较（回环校验/变更判定共用）：顶层空表 models/providers 与「表不存在」等价——
    段删净后文本中不再出现该表，但 dict 里可能留着空表（与 tomllib 解析结果逐键比较会误报）。"""
    a, b_ = dict(want), dict(parsed)
    for k in ("models", "providers"):
        if a.get(k) == {}:
            a.pop(k, None)
        if b_.get(k) == {}:
            b_.pop(k, None)
    return a == b_


def _harness_cfg_write_atomic(target, orig_bytes, new_text, mode):
    """原子写 harness config.toml：tmp（fsync + 原权限 chmod）→ .bak（写前原字节，同权限）
    → os.replace。任一步失败清理 tmp、原文件不动；返回错误串（成功 None）。调用方已持 _HARNESS_CFG_LOCK。"""
    tmp = target.parent / (target.name + ".tmp")
    bak = target.parent / (target.name + ".bak")
    try:
        # 创建即按原权限（含密钥的配置不留宽权限窗口；umask 只会再收紧，之后 chmod 补精确位）
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            f = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        bak.write_bytes(orig_bytes)
        os.chmod(bak, mode)
        os.replace(tmp, target)
    except Exception as e:  # noqa: BLE001 - 写失败回滚：tmp 清理、原文件不动
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"写入失败：{e}"
    return None
