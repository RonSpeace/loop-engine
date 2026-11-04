"""#32 Phase 3：model_pool 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""
from acp_shared import model_alias_entries_from_config
from acp_shared import model_alias_usable
from pathlib import Path
import os

from board_modules.runtime import resolve


def _model_source_label(role, source, env_var="KIMI_MODEL"):
    """模型来源中文标注（#4 四档；机器人抽屉/端点/guide 共用一个口径）。
    项目级标注仅在传入 project 且命中层①时出现；guide/设置抽屉不传 project，保持三层标注。"""
    if source == "project":
        return "（项目级 projects.json）"
    if source == "role":
        return "（角色配置）"
    if source == "env":
        return f"（环境变量 {env_var}_{str(role).upper()}）"
    if source == "escalation":
        return "（评审连续打回升级）"   # C3：dispatcher 本轮按 escalation_model 升级作者模型
    return "（全局默认）"


def _model_format_ok(model):
    """模型名格式校验（fail-closed 边界）：非空、无空白字符、无控制字符、长度 ≤128。"""
    if not isinstance(model, str) or not model or len(model) > 128:
        return False
    return not any(ch.isspace() or ord(ch) < 32 for ch in model)


def _harness_config_path():
    """#6 harness config.toml 读/写同源落点：session_homes() 候选序首个存在 config.toml 的 home。
    返回 (realpath_path, home_str) 或 (None, None)。home_str 保持原字符串（C9/C10 展示口径锁定）；
    realpath 仅用于实际读/写文件（软链不被替换）。"""
    for h in resolve("session_homes")():
        try:
            p = h / "config.toml"
            if p.is_file():
                return Path(os.path.realpath(p)), str(h)
        except OSError:
            continue
    return None, None


def _model_alias_pool_entries():
    """harness home config.toml 模型别名池条目（#4 池本体 / #9 分组单一数据源）：
    返回 (entries, home, warning)，entries = [{"alias", "provider"}] 保序去重。
    只读 [models] 表键、default_model、旧形态 providers.<p>.model（拼 p/<model>）三类字段，
    绝不读取/回传 api_key。provider 判定：models.<别名>.provider 命中现有 [providers] 段 →
    该厂商；合成别名 p/upstream → p；provider 缺失/悬空/无匹配 → ""（展示层归「（未归属）」）。
    不可用（无文件/解析失败/空池/tomllib 缺失）→ entries=[] + warning，调用方降级为仅格式校验
    放行（PRD §3）。#6 起路径解析复用 _harness_config_path（读写同源）。"""
    path, home = _harness_config_path()
    if not home:
        return [], None, "未找到 config.toml（harness home 无配置），已按格式校验放行"
    try:
        import tomllib
    except ImportError:  # Python < 3.11：无 tomllib，别名池不可用（不新增运行时依赖）
        return [], home, "当前 Python <3.11 无 tomllib，别名池不可用，已按格式校验放行"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 解析失败降级（PRD：仅格式校验 + warning）
        return [], home, f"config.toml 解析失败（{e}），已按格式校验放行"
    if not isinstance(raw, dict):
        return [], home, "config.toml 结构不符，别名池不可用，已按格式校验放行"
    # #11：规则段单一源 = acp_shared.model_alias_entries_from_config（本函数保留自身 IO/失败
    # 分支/返回三元组与 warning 文案，patch 面逐字不变）。
    entries = model_alias_entries_from_config(raw)
    if not entries:
        return [], home, "config.toml 中未找到模型别名，已按格式校验放行"
    return entries, home, ""


def _alias_groups(entries, empty_label="（未归属）"):
    """entries → [{label, items}] 保序分组（#9 下拉 optgroup）：provider 空归 empty_label，
    组按首次出现序、组内保序。"""
    groups, index = [], {}
    for e in entries:
        label = e.get("provider") or empty_label
        if label not in index:
            index[label] = []
            groups.append({"label": label, "items": index[label]})
        index[label].append(e["alias"])
    return groups


def _model_alias_pool():
    """harness home config.toml 模型别名池（#4）：返回 (items, home, warning)。
    #9 起 items 与分组同源（_model_alias_pool_entries）；签名与语义逐字不变。"""
    entries, home, warning = resolve("_model_alias_pool_entries")()
    return [e["alias"] for e in entries], home, warning


def _model_alias_pool_view():
    """GET /api/projects/<name>/models 用（#9）：单次读盘同源产出 (items, groups, home, warning)，
    避免 items 与 groups 两次读盘分叉（评审建议）。"""
    entries, home, warning = resolve("_model_alias_pool_entries")()
    return [e["alias"] for e in entries], _alias_groups(entries), home, warning


def session_homes():
    """候选 harness home（有序去重）：$KIMI_CODE_HOME（已设时）→ ~/.kimi-code-deepseek →
    ~/.kimi-code → 其余 ~/.kimi-code*（glob 兜未来新增 home）。测试 patch 本函数即完全隔离。"""
    homes = []
    env_home = os.environ.get('KIMI_CODE_HOME')
    if env_home:
        homes.append(Path(env_home).expanduser())
    homes.extend([Path.home() / '.kimi-code-deepseek', Path.home() / '.kimi-code'])
    try:
        homes.extend(sorted(Path.home().glob('.kimi-code*')))
    except OSError:
        pass
    seen, out = set(), []
    for h in homes:
        if str(h) not in seen:
            seen.add(str(h))
            out.append(h)
    return out


def _assistant_model_groups():
    """按供应商分组的**可用**助手模型别名（GET /api/feishu-settings 注入前端下拉用）。
    复用 _model_alias_pool_entries + _alias_groups 口径，只保留 model_alias_usable 为真的别名；
    池不可用（无 config.toml）→ 空分组（前端只显示「未配置」项，服务端保存时同样严格拒绝）。"""
    entries, _home, _warning = resolve("_model_alias_pool_entries")()
    usable = [e for e in entries if model_alias_usable(e)[0]]
    groups = _alias_groups(usable)
    return [{"label": g["label"], "items": g["items"]} for g in groups]
