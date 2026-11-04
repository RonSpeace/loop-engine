#!/usr/bin/env python3
"""视觉闸门（第一版）：用无头 Chromium 实际渲染页面，把「看起来对不对」变成可机读的闸门结论。

用法:
    python scripts/visual_gate.py --url http://127.0.0.1:8931/ \\
        [--baseline-url http://127.0.0.1:8932/] [--contract contract.json] [--out DIR] \\
        [--skins dark,light] [--viewports 1440x900,390x844] [--freeze-time "YYYY-MM-DD HH:MM:SS"]

每个「皮肤 × 视口」加载一次页面（prefers-reduced-motion、禁用动画/过渡、隐藏 #fluidBg、冻结 Date），
等网络静默后跑 6 个闸门并截图：

    console          控制台 error / 未捕获异常 / 同源请求失败 → fail
    tokens           :root 上全部 CSS 自定义属性的解析值；有基线时列出取值不同的令牌
    computed_style   按钮/输入/下拉/文本域/卡片/链接的关键计算样式（常态 / 强制 :hover / 强制
                     :focus-visible）；可聚焦控件必须有可见焦点指示，hover 不得改变包围盒；有基线时列差异
    layout_shift     （需基线）同键元素包围盒 dx/dy/dw/dh 超容差（默认 0.5px）→ fail
    contrast         可见文本的 WCAG 对比度（4.5 / 大字 3.0）；背景无法判定的记 undetermined，绝不算通过；
                     禁用控件与整体在画布外的元素（收起的抽屉）不检查
    native_controls  深色皮肤下原生控件「漏白」：背景亮度高，或 color-scheme 不含 dark

产物: <out>/visual-report.json、<out>/visual-report.html（自包含）、每组一张截图 PNG。
退出码: 0 全部通过 / 1 有闸门失败 / 2 工具错误（找不到 Chromium、页面不可达、契约文件非法）。

契约文件（可选，v1）:
    {"allow": {"tokens": ["--accent"], "contrast": ["--text-3", ".muted"],
               "computed": ["#saveBtn"], "layout": ["#navList*"]},
     "probes": [{"key": "save", "selector": "#saveBtn"}]}
    allow.* 为放行清单（探针键 / 令牌名支持 fnmatch 通配；contrast 条目以 -- 开头视为颜色令牌，
    否则视为 CSS 选择器，命中元素自身或其祖先即放行）；probes 追加自定义探针。未知顶层键直接拒绝。

实现：纯 Python + Chrome DevTools Protocol（websockets.sync.client），不依赖 playwright/selenium。
纯函数（对比度、令牌/包围盒差异、报告组装、退出码）与浏览器驱动分离，前者可脱离浏览器单测。
"""
import argparse
import base64
import fnmatch
import glob
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 默认冻结时刻（北京时间），与 scripts/seed_test_db.py 的 DEFAULT_FREEZE_TIME 同值（测试钉死一致）
DEFAULT_FREEZE_TIME = "2026-01-15 10:30:00"
BEIJING_TZ = timezone(timedelta(hours=8))
GATES = ("console", "tokens", "computed_style", "layout_shift", "contrast", "native_controls")
FAILURE_CAP = 50              # 每个闸门写进报告的失败条目上限（总数仍记在 counts）
LAYOUT_TOLERANCE = 0.5        # 包围盒差异容差（px）
CONTRAST_NORMAL, CONTRAST_LARGE = 4.5, 3.0
LIGHT_LUMINANCE = 0.4         # 控件背景相对亮度高于此值视为「亮」
DARK_PAGE_LUMINANCE = 0.2     # 页面背景相对亮度低于此值视为深色页
EXIT_PASS, EXIT_FAIL, EXIT_TOOL_ERROR = 0, 1, 2
CONTRACT_KEYS = ("allow", "probes")
ALLOW_KEYS = ("tokens", "contrast", "computed", "layout")
# 默认探针：(名称, 选择器)。每个选择器最多取 PROBE_CAP 个可见元素（DOM 顺序，确定性）
DEFAULT_PROBES = (
    ("button", "button, [role=\"button\"]"),
    ("input", "input:not([type=\"hidden\"])"),
    ("select", "select"),
    ("textarea", "textarea"),
    ("link", "a[href]"),
    ("card", ".card, [class~=\"card\"], article"),
)
PROBE_CAP = 12
LAYOUT_ID_CAP = 400
CONTRAST_CAP = 400
STYLE_PROPS = ("color", "background-color", "border-top-color", "border-right-color",
               "border-bottom-color", "border-left-color", "font-size", "font-weight", "line-height",
               "padding-top", "padding-right", "padding-bottom", "padding-left",
               "outline-style", "outline-width", "outline-color", "outline-offset", "box-shadow")


class ToolError(Exception):
    """工具级错误（退出码 2）：找不到浏览器、页面不可达、契约非法等，与闸门失败区分。"""


# ====================================================================
# 纯函数：颜色 / 对比度
# ====================================================================
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?%?"


def _chan(tok, scale=255.0):
    """单个通道记号 → 0-255 浮点（百分比按 scale 折算）。"""
    if tok.endswith("%"):
        return float(tok[:-1]) / 100.0 * scale
    return float(tok)


def _alpha(tok):
    return max(0.0, min(1.0, float(tok[:-1]) / 100.0 if tok.endswith("%") else float(tok)))


def parse_color(text):
    """解析计算样式里的颜色 → (r, g, b, a)（rgb 为 0-255，a 为 0-1）；无法解析返回 None。
    支持 rgb()/rgba()（逗号或空格语法）、#hex、transparent、color(srgb r g b / a)。"""
    if not isinstance(text, str):
        return None
    s = text.strip().lower()
    if not s:
        return None
    if s == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if s.startswith("#"):
        h = s[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8) or re.fullmatch(r"[0-9a-f]+", h) is None:
            return None
        vals = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
        return (float(vals[0]), float(vals[1]), float(vals[2]), vals[3] / 255.0 if len(vals) == 4 else 1.0)
    m = re.fullmatch(r"rgba?\((.*)\)", s)
    if m:
        toks = re.findall(_NUM, m.group(1))
        if len(toks) not in (3, 4):
            return None
        r, g, b = (_chan(t) for t in toks[:3])
        return (r, g, b, _alpha(toks[3]) if len(toks) == 4 else 1.0)
    m = re.fullmatch(r"color\(srgb\s+(.*)\)", s)
    if m:
        toks = re.findall(_NUM, m.group(1))
        if len(toks) not in (3, 4):
            return None
        r, g, b = (float(t[:-1]) / 100.0 * 255.0 if t.endswith("%") else float(t) * 255.0 for t in toks[:3])
        return (r, g, b, _alpha(toks[3]) if len(toks) == 4 else 1.0)
    return None


def composite(fg, bg):
    """alpha 合成：前景 (r,g,b,a) 叠在**不透明**背景 (r,g,b) 上，返回不透明 (r,g,b)。"""
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1.0 - a) for i in range(3))


def relative_luminance(rgb):
    """WCAG 2.x 相对亮度（rgb 为 0-255）。"""
    def lin(v):
        c = max(0.0, min(255.0, v)) / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def contrast_ratio(a, b):
    """两个不透明颜色的 WCAG 对比度（1.0 ~ 21.0）。"""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def canvas_color(root_color_scheme):
    """无任何不透明背景时的画布色：color-scheme 含 dark → Chromium 深色画布 #121212，否则白。"""
    return (18.0, 18.0, 18.0) if "dark" in (root_color_scheme or "").split() else (255.0, 255.0, 255.0)


def effective_background(layers, canvas=(255.0, 255.0, 255.0)):
    """由元素自身到根的背景层（[{c: 背景色, img: 是否有背景图, op: opacity}...]）求有效背景色。
    自内向外找到第一层不透明色后把途中半透明层逐层合成上去；途中遇到背景图（渐变/图片）或
    无法解析的颜色 → 返回 None（无法判定，调用方记 undetermined）。走到根仍无不透明层则落画布色。"""
    pending = []
    base = None
    for layer in layers or []:
        if layer.get("img"):
            return None
        col = parse_color(layer.get("c"))
        if col is None:
            return None
        op = layer.get("op")
        op = 1.0 if op is None else max(0.0, min(1.0, float(op)))
        alpha = col[3] * op
        if alpha <= 0.0:
            continue
        if alpha >= 0.999:
            base = col[:3]
            break
        pending.append((col[0], col[1], col[2], alpha))
    if base is None:
        base = tuple(canvas)
    for col in reversed(pending):
        base = composite(col, base)
    return base


def opacity_product(layers):
    """元素到根的 opacity 连乘（文本前景的整体透明度）。"""
    out = 1.0
    for layer in layers or []:
        op = layer.get("op")
        out *= 1.0 if op is None else max(0.0, min(1.0, float(op)))
    return out


def is_large_text(font_size_px, font_weight):
    """WCAG 大字：≥ 24px，或 ≥ 18.66px 且粗体（≥ 700）。"""
    try:
        size, weight = float(font_size_px), float(font_weight)
    except (TypeError, ValueError):
        return False
    return size >= 24.0 or (size >= 18.66 and weight >= 700)


def _px(value):
    try:
        return float(str(value).replace("px", "").strip())
    except ValueError:
        return 0.0


def _allowed(key, patterns):
    return any(key == p or fnmatch.fnmatchcase(key, p) for p in patterns or ())


def _rgb_text(rgb):
    return "rgb(%d, %d, %d)" % tuple(int(round(v)) for v in rgb)


def _capped(failures, counts, status=None, note=None):
    """闸门结果统一形状：失败清单截断到 FAILURE_CAP，总数留在 counts。"""
    out = {"status": status or ("fail" if failures else "pass"), "counts": counts,
           "failures": failures[:FAILURE_CAP], "truncated": max(0, len(failures) - FAILURE_CAP)}
    if note:
        out["note"] = note
    return out


def _skipped(note):
    return {"status": "skipped", "counts": {}, "failures": [], "truncated": 0, "note": note}


# ====================================================================
# 纯函数：各闸门判定
# ====================================================================
def evaluate_console(entries):
    """console 闸门：采集到的任何条目（console.error / 未捕获异常 / 同源请求失败）即失败。"""
    by = {}
    for e in entries:
        by[e.get("type", "error")] = by.get(e.get("type", "error"), 0) + 1
    return _capped(list(entries), {"errors": len(entries), **by})


def diff_tokens(base, cand, allow=()):
    """令牌差异：返回 (未放行差异, 已放行差异)，每项 {token, baseline, candidate}；
    一侧缺失记为 None。按令牌名排序。"""
    diffs, allowed = [], []
    for name in sorted(set(base) | set(cand)):
        b, c = base.get(name), cand.get(name)
        if b == c:
            continue
        item = {"token": name, "baseline": b, "candidate": c}
        (allowed if _allowed(name, allow) else diffs).append(item)
    return diffs, allowed


def evaluate_tokens(cand_tokens, base_tokens=None, allow=()):
    if base_tokens is None:
        return _capped([], {"tokens": len(cand_tokens)}, note="未提供基线，仅采集令牌取值")
    diffs, allowed = diff_tokens(base_tokens, cand_tokens, allow)
    return _capped(diffs, {"tokens": len(cand_tokens), "changed": len(diffs), "allowed": len(allowed)})


def diff_bboxes(base, cand, tolerance=LAYOUT_TOLERANCE, allow=()):
    """包围盒差异：base/cand 为 {键: {x,y,w,h}}。返回 (差异, 放行数)；差异项含 dx/dy/dw/dh 与
    max_delta，按 max_delta 降序（最严重的在前）。仅一侧存在的键记 change=missing/added。"""
    diffs, allowed = [], 0
    for key in sorted(set(base) | set(cand)):
        b, c = base.get(key), cand.get(key)
        if b is None or c is None:
            item = {"key": key, "change": "added" if b is None else "missing", "max_delta": float("inf")}
        else:
            d = {"d" + k: round(c[k] - b[k], 2) for k in ("x", "y", "w", "h")}
            worst = max(abs(v) for v in d.values())
            if worst <= tolerance:
                continue
            item = {"key": key, "change": "moved", **d, "max_delta": worst}
        if _allowed(key, allow):
            allowed += 1
        else:
            diffs.append(item)
    diffs.sort(key=lambda i: (-i["max_delta"], i["key"]))
    for item in diffs:
        if item["max_delta"] == float("inf"):
            item["max_delta"] = None
    return diffs, allowed


def evaluate_layout(cand_boxes, base_boxes=None, tolerance=LAYOUT_TOLERANCE, allow=()):
    if base_boxes is None:
        return _skipped("需要 --baseline-url")
    diffs, allowed = diff_bboxes(base_boxes, cand_boxes, tolerance, allow)
    return _capped(diffs, {"elements": len(cand_boxes), "shifted": len(diffs), "allowed": allowed})


def has_focus_indicator(normal, focus):
    """强制 :focus-visible 后是否有可见焦点指示：outline 宽度 > 0 且样式非 none 且颜色非透明，
    或 box-shadow 相对常态发生变化（且不为 none）。"""
    col = parse_color(focus.get("outline-color"))
    if (focus.get("outline-style", "none") != "none" and _px(focus.get("outline-width", "0")) > 0
            and (col is None or col[3] > 0)):
        return True
    shadow = focus.get("box-shadow", "none")
    return shadow != "none" and shadow != normal.get("box-shadow", "none")


def bbox_delta(a, b):
    return max(abs(a[k] - b[k]) for k in ("x", "y", "w", "h"))


def diff_props(base, cand):
    """两份计算样式的差异：{属性: [基线, 候选]}。"""
    return {p: [base.get(p), cand.get(p)] for p in sorted(set(base) | set(cand)) if base.get(p) != cand.get(p)}


def evaluate_computed(cand_probes, base_probes=None, tolerance=LAYOUT_TOLERANCE, allow=()):
    """computed_style 闸门。probes: [{key, selector, focusable, states: {normal|hover|focus: {props, bbox}}}]。
    规则：① 可聚焦控件强制 focus-visible 后须有可见焦点指示；② hover 不得改变包围盒；
    ③ 有基线时同键探针各状态的属性差异。allow 命中的探针键整体放行。"""
    failures, allowed = [], 0
    counts = {"probes": len(cand_probes), "focusable": 0, "focus_missing": 0, "hover_shift": 0, "changed": 0}
    base_by_key = {p["key"]: p for p in (base_probes or [])}
    for probe in cand_probes:
        key, states = probe["key"], probe.get("states") or {}
        normal = states.get("normal") or {}
        found = []
        if probe.get("focusable") and "focus" in states:
            counts["focusable"] += 1
            if not has_focus_indicator(normal.get("props") or {}, states["focus"].get("props") or {}):
                f = states["focus"].get("props") or {}
                found.append({"rule": "focus_visible", "key": key, "selector": probe.get("selector"),
                              "outline": "%s %s %s" % (f.get("outline-style"), f.get("outline-width"),
                                                       f.get("outline-color")),
                              "box_shadow": f.get("box-shadow")})
        if "hover" in states and normal.get("bbox") and states["hover"].get("bbox"):
            delta = bbox_delta(normal["bbox"], states["hover"]["bbox"])
            if delta > tolerance:
                found.append({"rule": "hover_shift", "key": key, "selector": probe.get("selector"),
                              "delta": round(delta, 2), "normal": normal["bbox"], "hover": states["hover"]["bbox"]})
        base = base_by_key.get(key)
        if base is not None:
            for state in ("normal", "hover", "focus"):
                if state in states and state in (base.get("states") or {}):
                    d = diff_props(base["states"][state].get("props") or {}, states[state].get("props") or {})
                    if d:
                        found.append({"rule": "baseline_diff", "key": key, "selector": probe.get("selector"),
                                      "state": state, "diff": d})
        if not found:
            continue
        if _allowed(key, allow):
            allowed += len(found)
            continue
        for f in found:
            counts[{"focus_visible": "focus_missing", "hover_shift": "hover_shift",
                    "baseline_diff": "changed"}[f["rule"]]] += 1
        failures.extend(found)
    counts["allowed"] = allowed
    note = None if base_probes is not None else "未提供基线，仅检查焦点指示与 hover 零位移"
    return _capped(failures, counts, note=note)


def evaluate_contrast(samples, canvas=(255.0, 255.0, 255.0), allow_token_colors=None):
    """contrast 闸门。samples: [{key, text, color, font_size, font_weight, layers, allow: [命中的放行选择器]}]。
    allow_token_colors: {令牌名: 该令牌解析出的颜色字符串}——文本色与之相同即放行。
    背景或前景无法判定 → undetermined（单独计数并使闸门不为 pass 之外的任何「通过」假象：
    undetermined 不计入 passed，也不计入 failed）。失败按对比度升序（最差在前）。"""
    token_rgba = {}
    for name, value in (allow_token_colors or {}).items():
        col = parse_color(value)
        if col is not None:
            token_rgba[name] = tuple(round(v, 3) for v in col)
    failures, undetermined = [], []
    counts = {"sampled": len(samples), "passed": 0, "failed": 0, "undetermined": 0, "allowed": 0}
    for s in samples:
        fg = parse_color(s.get("color"))
        bg = effective_background(s.get("layers"), canvas)
        if fg is None or bg is None:
            counts["undetermined"] += 1
            undetermined.append({"key": s.get("key"), "text": s.get("text"),
                                 "reason": "前景色无法解析" if fg is None else "背景含渐变/图片或无法解析"})
            continue
        alpha = fg[3] * opacity_product(s.get("layers"))
        ratio = contrast_ratio(composite((fg[0], fg[1], fg[2], alpha), bg), bg)
        required = CONTRAST_LARGE if is_large_text(_px(s.get("font_size")), s.get("font_weight")) else CONTRAST_NORMAL
        if ratio + 1e-9 >= required:
            counts["passed"] += 1
            continue
        hit = list(s.get("allow") or [])
        rounded = tuple(round(v, 3) for v in fg)
        hit += [name for name, col in sorted(token_rgba.items()) if col == rounded]
        if hit:
            counts["allowed"] += 1
            continue
        failures.append({"key": s.get("key"), "text": s.get("text"), "ratio": round(ratio, 2),
                         "required": required, "color": s.get("color"), "background": _rgb_text(bg),
                         "font_size": s.get("font_size"), "font_weight": s.get("font_weight")})
    counts["failed"] = len(failures)
    failures.sort(key=lambda f: (f["ratio"], f["key"] or ""))
    out = _capped(failures, counts)
    out["undetermined"] = undetermined[:FAILURE_CAP]
    return out


def evaluate_native_controls(skin, controls, page_layers, root_color_scheme):
    """native_controls 闸门（仅深色皮肤）。controls: [{key, kind: control|scroller, tag, type,
    color_scheme, scrollbar_color, layers}]。深色页上：表单控件有效背景亮度高 → light_background；
    控件 color-scheme 不含 dark → color_scheme（滚动容器若自定义了 scrollbar-color 则不报）。"""
    if skin != "dark":
        return _skipped("仅在 dark 皮肤检查")
    canvas = canvas_color(root_color_scheme)
    page_bg = effective_background(page_layers, canvas)
    if page_bg is None:
        return _skipped("页面背景无法判定（含渐变/图片）")
    page_lum = relative_luminance(page_bg)
    if page_lum >= DARK_PAGE_LUMINANCE:
        return _skipped("页面背景不是深色（相对亮度 %.2f）" % page_lum)
    failures = []
    counts = {"controls": len(controls), "light_background": 0, "color_scheme": 0, "undetermined": 0}
    for c in controls:
        reasons = []
        scheme = (c.get("color_scheme") or "normal").split()
        if c.get("kind") == "scroller":
            if "dark" not in scheme and (c.get("scrollbar_color") or "auto") == "auto":
                reasons.append("color_scheme")
        else:
            bg = effective_background(c.get("layers"), canvas)
            if bg is None:
                counts["undetermined"] += 1
            elif relative_luminance(bg) > LIGHT_LUMINANCE:
                reasons.append("light_background")
            if "dark" not in scheme:
                reasons.append("color_scheme")
        if not reasons:
            continue
        for r in reasons:
            counts[r] += 1
        bg = effective_background(c.get("layers"), canvas) if c.get("layers") else None
        failures.append({"key": c.get("key"), "kind": c.get("kind"), "tag": c.get("tag"), "type": c.get("type"),
                         "reasons": reasons, "color_scheme": c.get("color_scheme"),
                         "background": _rgb_text(bg) if bg else None})
    return _capped(failures, counts)


# ====================================================================
# 纯函数：契约 / 参数 / 报告
# ====================================================================
def validate_contract(obj):
    """校验并归一契约对象 → {"allow": {tokens,contrast,computed,layout: [str]}, "probes": [{key,selector}]}。
    非法抛 ToolError（消息含出错位置）。"""
    if obj is None:
        obj = {}
    if not isinstance(obj, dict):
        raise ToolError("契约文件顶层必须是 JSON 对象")
    unknown = sorted(set(obj) - set(CONTRACT_KEYS))
    if unknown:
        raise ToolError("契约文件含未知顶层键: %s（仅支持 %s）" % (", ".join(unknown), " / ".join(CONTRACT_KEYS)))
    allow_in = obj.get("allow", {})
    if not isinstance(allow_in, dict):
        raise ToolError("契约 allow 必须是对象")
    unknown = sorted(set(allow_in) - set(ALLOW_KEYS))
    if unknown:
        raise ToolError("契约 allow 含未知键: %s（仅支持 %s）" % (", ".join(unknown), " / ".join(ALLOW_KEYS)))
    allow = {}
    for key in ALLOW_KEYS:
        vals = allow_in.get(key, [])
        if not isinstance(vals, list) or not all(isinstance(v, str) and v.strip() for v in vals):
            raise ToolError("契约 allow.%s 必须是非空字符串组成的数组" % key)
        allow[key] = [v.strip() for v in vals]
    probes_in = obj.get("probes", [])
    if not isinstance(probes_in, list):
        raise ToolError("契约 probes 必须是数组")
    probes, seen = [], set()
    for i, p in enumerate(probes_in):
        if (not isinstance(p, dict) or set(p) != {"key", "selector"}
                or not all(isinstance(p[k], str) and p[k].strip() for k in ("key", "selector"))):
            raise ToolError("契约 probes[%d] 必须形如 {\"key\": \"...\", \"selector\": \"...\"}" % i)
        if p["key"] in seen:
            raise ToolError("契约 probes[%d] 的 key 重复: %s" % (i, p["key"]))
        seen.add(p["key"])
        probes.append({"key": p["key"].strip(), "selector": p["selector"].strip()})
    return {"allow": allow, "probes": probes}


def load_contract(path):
    if not path:
        return validate_contract({})
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ToolError("契约文件不可读: %s（%s）" % (path, e))
    except ValueError as e:
        raise ToolError("契约文件不是合法 JSON: %s（%s）" % (path, e))
    return validate_contract(raw)


def parse_viewports(text):
    """'1440x900,390x844' → [(1440, 900), (390, 844)]；非法抛 ToolError。"""
    out = []
    for part in (text or "").split(","):
        m = re.fullmatch(r"\s*(\d{2,5})x(\d{2,5})\s*", part)
        if not m:
            raise ToolError("--viewports 非法: %r（形如 1440x900,390x844）" % text)
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def parse_skins(text):
    out = [s.strip() for s in (text or "").split(",") if s.strip()]
    bad = [s for s in out if s not in ("dark", "light")]
    if bad or not out:
        raise ToolError("--skins 非法: %r（仅支持 dark / light）" % text)
    return out


def freeze_ms(text):
    """冻结时刻字符串（北京时间）→ epoch 毫秒；非法抛 ToolError。"""
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
    except (TypeError, ValueError):
        raise ToolError("--freeze-time 非法: %r（需为 YYYY-MM-DD HH:MM:SS）" % (text,))
    return int(dt.timestamp() * 1000)


def boxes_of(collection):
    """采集结果 → {键: bbox}（探针 + 带 id 元素）。"""
    out = {p["key"]: p["states"]["normal"]["bbox"] for p in collection.get("probes", [])
           if (p.get("states") or {}).get("normal")}
    for item in collection.get("layout", []):
        out.setdefault(item["key"], item["bbox"])
    return out


def evaluate_run(skin, viewport, cand, base=None, contract=None, tolerance=LAYOUT_TOLERANCE):
    """一组「皮肤 × 视口」的全部闸门判定（纯函数：输入是浏览器采集结果，输出是可序列化结论）。"""
    allow = (contract or validate_contract({}))["allow"]
    canvas = canvas_color(cand.get("root_color_scheme"))
    gates = {
        "console": evaluate_console(cand.get("console", [])),
        "tokens": evaluate_tokens(cand.get("tokens", {}), None if base is None else base.get("tokens", {}),
                                  allow["tokens"]),
        "computed_style": evaluate_computed(cand.get("probes", []), None if base is None else base.get("probes", []),
                                            tolerance, allow["computed"]),
        "layout_shift": evaluate_layout(boxes_of(cand), None if base is None else boxes_of(base),
                                        tolerance, allow["layout"]),
        "contrast": evaluate_contrast(cand.get("contrast", []), canvas, cand.get("allow_token_colors")),
        "native_controls": evaluate_native_controls(skin, cand.get("native", []), cand.get("page_layers", []),
                                                    cand.get("root_color_scheme")),
    }
    status = "fail" if any(g["status"] == "fail" for g in gates.values()) else "pass"
    return {"skin": skin, "viewport": "%dx%d" % tuple(viewport), "status": status, "gates": gates,
            "tokens": cand.get("tokens", {}), "notes": list(cand.get("notes", []))}


def assemble_report(meta, runs):
    """组装最终报告：顶层 status = 任一 run 失败即 fail。"""
    return {"status": "fail" if any(r["status"] == "fail" for r in runs) else "pass",
            "meta": dict(meta), "runs": list(runs)}


def decide_exit_code(report):
    """0 = 全部通过；1 = 有闸门失败。（工具错误走 ToolError → 2，不经本函数。）"""
    return EXIT_FAIL if report.get("status") == "fail" else EXIT_PASS


_STATUS_CN = {"pass": "通过", "fail": "失败", "skipped": "跳过"}


def render_html(report, images=None):
    """自包含 HTML 报告：汇总表 + 失败明细 + 截图（有基线时左右并排）。images: {文件名: PNG 字节}。"""
    esc = html_mod.escape
    images = images or {}
    meta = report["meta"]

    def img(name):
        data = images.get(name)
        if not data:
            return ""
        return '<figure><img src="data:image/png;base64,%s" alt="%s"><figcaption>%s</figcaption></figure>' % (
            base64.b64encode(data).decode("ascii"), esc(name), esc(name))

    rows = []
    for run in report["runs"]:
        cells = "".join('<td class="%s">%s<small>%s</small></td>' % (
            g["status"], _STATUS_CN[g["status"]],
            esc(" ".join("%s=%s" % kv for kv in sorted(g["counts"].items()))))
            for g in (run["gates"][name] for name in GATES))
        rows.append('<tr><th>%s · %s</th>%s</tr>' % (esc(run["skin"]), esc(run["viewport"]), cells))
    detail = []
    for run in report["runs"]:
        title = "%s · %s" % (run["skin"], run["viewport"])
        blocks = []
        for name in GATES:
            g = run["gates"][name]
            if g["status"] != "fail":
                continue
            items = "".join("<li><code>%s</code></li>" % esc(json.dumps(f, ensure_ascii=False)) for f in g["failures"])
            more = "<p>……另有 %d 条未列出</p>" % g["truncated"] if g["truncated"] else ""
            blocks.append("<h4>%s（%d 条）</h4><ul>%s</ul>%s" % (
                esc(name), len(g["failures"]) + g["truncated"], items, more))
        shots = img(run.get("baseline_screenshot")) + img(run.get("screenshot"))
        notes = "".join("<p class='note'>%s</p>" % esc(n) for n in run.get("notes", []))
        detail.append("<section><h3>%s — %s</h3>%s%s<div class='shots'>%s</div></section>" % (
            esc(title), _STATUS_CN[run["status"]], notes, "".join(blocks) or "<p>无失败项</p>", shots))
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>视觉闸门报告</title>
<style>
body{font-family:-apple-system,"PingFang SC",sans-serif;margin:24px;color:#1b1f27;background:#f6f7f9}
table{border-collapse:collapse;background:#fff}th,td{border:1px solid #d5d9e0;padding:6px 10px;text-align:left;vertical-align:top}
td.pass{background:#e6f6ea}td.fail{background:#fde8e8}td.skipped{background:#f0f1f3;color:#5b6472}
small{display:block;color:#4a5261;font-size:11px;max-width:220px}
code{font-size:12px;word-break:break-all}section{background:#fff;border:1px solid #d5d9e0;padding:12px 16px;margin:16px 0}
.shots{display:flex;gap:12px;flex-wrap:wrap}figure{margin:0;flex:1 1 420px}img{max-width:100%%;border:1px solid #d5d9e0}
figcaption{font-size:12px;color:#4a5261}.note{color:#8a5a00}.status-pass{color:#17803d}.status-fail{color:#c22727}
</style></head><body>
<h1>视觉闸门报告 <span class="status-%s">%s</span></h1>
<p>URL：<code>%s</code>　基线：<code>%s</code>　冻结时刻：%s　浏览器：%s</p>
<h2>汇总</h2>
<table><tr><th>皮肤 · 视口</th>%s</tr>%s</table>
<h2>明细</h2>
%s
</body></html>
""" % (report["status"], _STATUS_CN[report["status"]], esc(str(meta.get("url"))),
       esc(str(meta.get("baseline_url") or "无")), esc(str(meta.get("frozen_time"))),
       esc(str(meta.get("chrome_version"))), "".join("<th>%s</th>" % g for g in GATES),
       "".join(rows), "".join(detail))


# ====================================================================
# 浏览器驱动（CDP）
# ====================================================================
def find_chrome():
    """定位 Chromium：VISUAL_GATE_CHROME → playwright 缓存的 headless shell（取版本最高）→
    本机 Google Chrome。找不到返回 None。"""
    env = os.environ.get("VISUAL_GATE_CHROME")
    if env:
        return env if os.access(env, os.X_OK) else None
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    linux_cache = Path.home() / ".cache" / "ms-playwright"
    cands = []
    for root in (cache, linux_cache):
        cands += glob.glob(str(root / "chromium_headless_shell-*" / "chrome-headless-shell-*" / "chrome-headless-shell"))

    def rev(p):
        m = re.search(r"chromium_headless_shell-(\d+)", p)
        return int(m.group(1)) if m else 0
    for p in sorted(cands, key=rev, reverse=True):
        if os.access(p, os.X_OK):
            return p
    for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("chromium") or "", shutil.which("google-chrome") or ""):
        if p and os.access(p, os.X_OK):
            return p
    return None


class CDPError(Exception):
    pass


class Browser:
    """无头 Chromium 进程 + 一条 CDP 浏览器级 WebSocket（flatten 会话）。只管理自己拉起的进程：
    退出时经 Browser.close + 进程句柄 terminate/kill 收尾，不按进程名杀任何东西。"""

    def __init__(self, chrome_path=None):
        self.chrome = chrome_path or find_chrome()
        if not self.chrome:
            raise ToolError("找不到 Chromium（可用 VISUAL_GATE_CHROME 指定可执行文件路径）")
        self.proc = None
        self.ws = None
        self.profile = None
        self._id = 0
        self.events = []
        self.version = None

    def __enter__(self):
        try:
            self._launch()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def _launch(self):
        from websockets.sync.client import connect
        self.profile = tempfile.mkdtemp(prefix="visual-gate-chrome-")
        args = [self.chrome, "--remote-debugging-port=0", "--user-data-dir=" + self.profile,
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--force-color-profile=srgb", "--font-render-hinting=none", "--hide-scrollbars",
                "--mute-audio", "about:blank"]
        if "headless-shell" not in os.path.basename(self.chrome):
            args.insert(1, "--headless=new")
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise ToolError("Chromium 启动失败: %s" % e)
        port_file = Path(self.profile) / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        lines = []
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ToolError("Chromium 启动后立即退出（exit %s）" % self.proc.returncode)
            try:
                lines = port_file.read_text().split()
            except OSError:
                lines = []
            if len(lines) >= 2:
                break
            time.sleep(0.05)
        if len(lines) < 2:
            raise ToolError("Chromium 未在 20s 内给出 DevTools 端口")
        try:
            self.ws = connect("ws://127.0.0.1:%s%s" % (lines[0], lines[1]), max_size=None, open_timeout=10)
        except Exception as e:  # noqa: BLE001 - 连接失败统一归工具错误
            raise ToolError("连接 DevTools 失败: %s" % e)
        self.version = self.send("Browser.getVersion").get("product")

    def send(self, method, params=None, session=None, timeout=30.0):
        """发一条 CDP 命令并等到它的应答；途中收到的事件进 self.events。"""
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CDPError("CDP 命令超时: %s" % method)
            try:
                raw = self.ws.recv(timeout=left)
            except TimeoutError:
                raise CDPError("CDP 命令超时: %s" % method)
            m = json.loads(raw)
            if m.get("id") == mid:
                if "error" in m:
                    raise CDPError("%s: %s" % (method, m["error"].get("message")))
                return m.get("result", {})
            if "method" in m:
                self.events.append(m)

    def pump(self, duration):
        """收 duration 秒事件（不发命令）。"""
        deadline = time.monotonic() + duration
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                raw = self.ws.recv(timeout=left)
            except TimeoutError:
                return
            m = json.loads(raw)
            if "method" in m:
                self.events.append(m)

    def close(self):
        if self.ws is not None:
            try:
                self.send("Browser.close", timeout=3)
            except Exception:  # noqa: BLE001 - 收尾尽力而为
                pass
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self.proc = None
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)
            self.profile = None


def boot_script(skin, frozen_ms):
    """每个新文档最先执行的脚本：冻结 Date、设皮肤、注入禁动画样式、登记定时器（供采集前冻结）。"""
    css = ("*,*::before,*::after{animation:none!important;transition:none!important;"
           "scroll-behavior:auto!important;caret-color:transparent!important}"
           "#fluidBg{visibility:hidden!important}")
    return """(function(){
  var T=%d,R=Date;
  if(!window.__vgDate){
    var F=function(){var a=arguments;if(!(this instanceof F))return new R(T).toString();
      if(a.length===0)return new R(T);
      return new (Function.prototype.bind.apply(R,[null].concat([].slice.call(a))))();};
    F.prototype=R.prototype;F.now=function(){return T;};F.parse=R.parse;F.UTC=R.UTC;
    window.Date=F;window.__vgDate=true;
  }
  var skin=%s;
  try{localStorage.setItem('reqBoardSkin',skin);}catch(e){}
  var css=%s;
  function addStyle(){
    if(!document.documentElement||document.getElementById('__vg_style'))return;
    var s=document.createElement('style');s.id='__vg_style';s.textContent=css;
    (document.head||document.documentElement).appendChild(s);
  }
  function applySkin(){if(skin==='light'&&document.documentElement)document.documentElement.setAttribute('data-skin','light');}
  addStyle();
  document.addEventListener('DOMContentLoaded',function(){addStyle();applySkin();});
  var timers=[],st=window.setTimeout,si=window.setInterval,raf=window.requestAnimationFrame;
  window.setTimeout=function(){var id=st.apply(window,arguments);timers.push(['t',id]);return id;};
  window.setInterval=function(){var id=si.apply(window,arguments);timers.push(['i',id]);return id;};
  window.__vgRaf=raf?raf.bind(window):null;
  window.__vgFreezeTimers=function(){
    timers.forEach(function(t){(t[0]==='t'?clearTimeout:clearInterval)(t[1]);});timers=[];
    window.setTimeout=function(){return 0;};window.setInterval=function(){return 0;};
    window.requestAnimationFrame=function(){return 0;};
  };
})();""" % (frozen_ms, json.dumps(skin), json.dumps(css))


# 页面内采集脚本：一次性收集令牌 / 探针 / 版式键 / 对比度样本 / 原生控件，返回 JSON 字符串
COLLECT_JS = r"""(function(cfg){
  var PROPS=cfg.props;
  function vis(el){
    var r=el.getBoundingClientRect();
    if(r.width<=0||r.height<=0)return false;
    var cs=getComputedStyle(el);
    if(cs.visibility!=='visible'||cs.display==='none')return false;
    if(el.checkVisibility&&!el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
    return true;
  }
  // 整体落在画布外（如 transform 收起的抽屉）：肉眼不可见，不参与对比度 / 原生控件检查
  function offCanvas(el){
    var r=el.getBoundingClientRect(),w=Math.max(window.innerWidth,document.documentElement.scrollWidth);
    return r.right<=0||r.bottom<=0||r.left>=w;
  }
  function uniqId(el){
    if(!el.id)return false;
    try{return document.querySelectorAll('#'+CSS.escape(el.id)).length===1;}catch(e){return false;}
  }
  function keyOf(el){
    var parts=[],n=el;
    while(n&&n.nodeType===1&&n!==document.documentElement){
      if(uniqId(n)){parts.unshift('#'+n.id);break;}
      var i=1,s=n;
      while((s=s.previousElementSibling))if(s.tagName===n.tagName)i++;
      parts.unshift(n.tagName.toLowerCase()+':nth-of-type('+i+')');
      n=n.parentElement;
    }
    return parts.join('>');
  }
  function bbox(el){
    var r=el.getBoundingClientRect();
    function f(v){return Math.round(v*100)/100;}
    return {x:f(r.left+window.scrollX),y:f(r.top+window.scrollY),w:f(r.width),h:f(r.height)};
  }
  function props(el){var cs=getComputedStyle(el),o={};PROPS.forEach(function(p){o[p]=cs.getPropertyValue(p);});return o;}
  function layers(el){
    var out=[],n=el;
    while(n&&n.nodeType===1){
      var cs=getComputedStyle(n);
      out.push({c:cs.backgroundColor,img:cs.backgroundImage!=='none',op:parseFloat(cs.opacity)});
      n=n.parentElement;
    }
    return out;
  }
  window.__vgState=function(i){
    var el=document.querySelector('[data-vg-probe="'+i+'"]');
    if(!el)return 'null';
    return JSON.stringify({props:props(el),bbox:bbox(el)});
  };
  var out={};
  // —— 令牌：计算样式枚举 ∪ 样式表里 :root 规则声明的自定义属性 ——
  var root=document.documentElement,rcs=getComputedStyle(root),names={};
  for(var i=0;i<rcs.length;i++){if(rcs[i].indexOf('--')===0)names[rcs[i]]=1;}
  function scan(rules){
    for(var j=0;j<rules.length;j++){
      var r=rules[j];
      if(r.cssRules&&r.type!==1){try{scan(r.cssRules);}catch(e){}}
      if(r.style&&r.selectorText&&/(^|[\s,]):root|^html/.test(r.selectorText)){
        for(var k=0;k<r.style.length;k++){if(r.style[k].indexOf('--')===0)names[r.style[k]]=1;}
      }
    }
  }
  for(var s=0;s<document.styleSheets.length;s++){try{scan(document.styleSheets[s].cssRules);}catch(e){}}
  out.tokens={};
  Object.keys(names).sort().forEach(function(n){
    if(n.indexOf('--vg-')===0)return;
    out.tokens[n]=rcs.getPropertyValue(n).trim();
  });
  out.root_color_scheme=rcs.colorScheme||'normal';
  out.page_layers=document.body?layers(document.body):[];
  // —— 探针 ——
  var seen=new Map(),probes=[];
  cfg.probes.forEach(function(pc){
    var els;
    try{els=document.querySelectorAll(pc.selector);}catch(e){return;}
    var n=0;
    for(var i=0;i<els.length&&n<cfg.probeCap;i++){
      var el=els[i];
      if(seen.has(el)||!vis(el))continue;
      n++;
      var idx=probes.length;
      seen.set(el,idx);
      el.setAttribute('data-vg-probe',String(idx));
      var disabled=el.matches(':disabled')||el.getAttribute('aria-disabled')==='true';
      var focusable=!disabled&&el.matches('button,input,select,textarea,a[href],[tabindex]')&&el.tabIndex>=0;
      var key=pc.key?(n===1?pc.key:pc.key+'#'+n):keyOf(el);
      probes.push({index:idx,key:key,selector:pc.name,tag:el.tagName.toLowerCase(),focusable:focusable,
                   states:{normal:{props:props(el),bbox:bbox(el)}}});
    }
  });
  out.probes=probes;
  // —— 版式键：带唯一 id 的可见元素 ——
  out.layout=[];
  var ided=document.querySelectorAll('[id]');
  for(var i=0;i<ided.length&&out.layout.length<cfg.layoutCap;i++){
    var el=ided[i];
    if(el.id.indexOf('__vg')===0||!uniqId(el)||!vis(el))continue;
    out.layout.push({key:'#'+el.id,bbox:bbox(el)});
  }
  // —— 对比度样本：有直接文本的可见元素 + 有值的表单控件 ——
  var texts=[],tseen=new Set();
  if(document.body){
    var tw=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),t;
    while((t=tw.nextNode())){
      if(!t.nodeValue.trim())continue;
      var el=t.parentElement;
      if(!el||tseen.has(el))continue;
      tseen.add(el);
      if(/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|OPTION|TITLE)$/.test(el.tagName))continue;
      if(!vis(el))continue;
      texts.push(el);
    }
    document.querySelectorAll('input:not([type="hidden"]),textarea,select').forEach(function(el){
      if(tseen.has(el)||!vis(el))return;
      if(/^(checkbox|radio|range|color|file)$/.test(el.type||''))return;
      if(el.tagName!=='SELECT'&&!el.value)return;
      tseen.add(el);texts.push(el);
    });
  }
  out.contrast_total=texts.length;
  var disabledSkipped=0,offSkipped=0;
  texts=texts.filter(function(el){
    if(el.closest(':disabled,[aria-disabled="true"]')){disabledSkipped++;return false;}
    if(offCanvas(el)){offSkipped++;return false;}
    return true;
  });
  out.contrast_disabled=disabledSkipped;
  out.contrast_offcanvas=offSkipped;
  var picked=texts;
  if(texts.length>cfg.contrastCap){
    picked=[];
    for(var i=0;i<cfg.contrastCap;i++)picked.push(texts[Math.floor(i*texts.length/cfg.contrastCap)]);
  }
  function textOf(el){
    if(el.tagName==='SELECT')return (el.selectedOptions[0]&&el.selectedOptions[0].text||'').trim().slice(0,40);
    if(el.tagName==='INPUT'||el.tagName==='TEXTAREA')return String(el.value).trim().slice(0,40);
    var s='';
    el.childNodes.forEach(function(n){if(n.nodeType===3)s+=n.nodeValue;});
    return s.replace(/\s+/g,' ').trim().slice(0,40);
  }
  out.contrast=picked.map(function(el){
    var cs=getComputedStyle(el),hit=[];
    cfg.allowSelectors.forEach(function(sel){try{if(el.closest(sel))hit.push(sel);}catch(e){}});
    return {key:keyOf(el),text:textOf(el),color:cs.color,font_size:cs.fontSize,font_weight:cs.fontWeight,
            layers:layers(el),allow:hit};
  });
  out.allow_token_colors={};
  if(cfg.allowTokens.length&&document.body){
    var tmp=document.createElement('span');
    tmp.style.display='none';document.body.appendChild(tmp);
    cfg.allowTokens.forEach(function(tok){
      tmp.style.color='';tmp.style.color='var('+tok+')';
      if(rcs.getPropertyValue(tok).trim())out.allow_token_colors[tok]=getComputedStyle(tmp).color;
    });
    tmp.remove();
  }
  // —— 原生控件 + 可滚动容器 ——
  out.native=[];
  var ctrls=document.querySelectorAll('select,textarea,input:not([type="hidden"])');
  for(var i=0,n=0;i<ctrls.length&&n<cfg.nativeCap;i++){
    var el=ctrls[i];
    if(!vis(el)||offCanvas(el))continue;
    n++;
    var cs=getComputedStyle(el);
    out.native.push({key:keyOf(el),kind:'control',tag:el.tagName.toLowerCase(),type:el.type||null,
                     color_scheme:cs.colorScheme||'normal',appearance:cs.appearance,layers:layers(el)});
  }
  var all=document.body?document.body.querySelectorAll('*'):[];
  for(var i=0,n=0;i<all.length&&n<cfg.nativeCap;i++){
    var el=all[i];
    if(el.tagName==='SELECT'||el.tagName==='TEXTAREA'||el.tagName==='INPUT')continue;
    var cs=getComputedStyle(el);
    var sy=/(auto|scroll)/.test(cs.overflowY)&&el.scrollHeight>el.clientHeight+1;
    var sx=/(auto|scroll)/.test(cs.overflowX)&&el.scrollWidth>el.clientWidth+1;
    if(!(sx||sy)||!vis(el)||offCanvas(el))continue;
    n++;
    out.native.push({key:keyOf(el),kind:'scroller',tag:el.tagName.toLowerCase(),type:null,
                     color_scheme:cs.colorScheme||'normal',scrollbar_color:cs.scrollbarColor||'auto',
                     layers:layers(el)});
  }
  return JSON.stringify(out);
})"""


def _same_origin(url, origin):
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return "%s://%s" % (u.scheme, u.netloc) == origin


def collect_page(browser, url, skin, viewport, frozen_ms, contract, settle_timeout=20.0):
    """在全新浏览器上下文里加载 url 并采集一组数据（供 evaluate_run）。返回 (采集结果, 截图 PNG 字节)。
    页面不可达抛 ToolError。"""
    origin = "%s://%s" % urllib.parse.urlsplit(url)[:2]
    ctx = browser.send("Target.createBrowserContext")["browserContextId"]
    target = browser.send("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
    sid = browser.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    notes = []

    def cmd(method, params=None, timeout=30.0):
        return browser.send(method, params, session=sid, timeout=timeout)

    def evaluate(expr, await_promise=False):
        res = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": await_promise})
        if res.get("exceptionDetails"):
            raise CDPError("页面内脚本异常: %s" % json.dumps(res["exceptionDetails"], ensure_ascii=False)[:400])
        return res.get("result", {}).get("value")

    try:
        for domain in ("Page", "Runtime", "Network", "Log", "DOM", "CSS"):
            cmd(domain + ".enable")
        cmd("Emulation.setDeviceMetricsOverride",
            {"width": viewport[0], "height": viewport[1], "deviceScaleFactor": 1, "mobile": False})
        cmd("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-reduced-motion", "value": "reduce"},
            {"name": "prefers-color-scheme", "value": skin}]})
        try:
            cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})
        except CDPError:
            notes.append("时区覆盖失败，时间文案按本机时区渲染")
        cmd("Page.addScriptToEvaluateOnNewDocument", {"source": boot_script(skin, frozen_ms)})
        mark = len(browser.events)
        nav = cmd("Page.navigate", {"url": url})
        if nav.get("errorText"):
            raise ToolError("页面不可达: %s（%s）" % (url, nav["errorText"]))
        # —— 等 load + 网络静默 ——
        console, inflight, req_urls = [], {}, {}
        state = {"loaded": False, "last": time.monotonic(), "doc_status": None}

        def drain():
            nonlocal mark
            evs, mark = browser.events[mark:], len(browser.events)
            for ev in evs:
                if ev.get("sessionId") != sid:
                    continue
                m, p = ev["method"], ev.get("params", {})
                if m == "Page.loadEventFired":
                    state["loaded"] = True
                elif m == "Network.requestWillBeSent":
                    rurl = p["request"]["url"]
                    req_urls[p["requestId"]] = rurl
                    if p.get("type") != "EventSource" and not rurl.startswith("data:"):
                        inflight[p["requestId"]] = rurl
                    state["last"] = time.monotonic()
                elif m == "Network.responseReceived":
                    resp = p.get("response", {})
                    if p.get("type") == "Document" and state["doc_status"] is None:
                        state["doc_status"] = resp.get("status")
                    if p.get("type") == "EventSource":
                        inflight.pop(p["requestId"], None)
                    if resp.get("status", 0) >= 400 and _same_origin(resp.get("url", ""), origin):
                        console.append({"type": "http_error", "text": "HTTP %s" % resp.get("status"),
                                        "url": resp.get("url")})
                elif m == "Network.loadingFinished":
                    inflight.pop(p["requestId"], None)
                    state["last"] = time.monotonic()
                elif m == "Network.loadingFailed":
                    inflight.pop(p["requestId"], None)
                    state["last"] = time.monotonic()
                    rurl = req_urls.get(p["requestId"], "")
                    if not p.get("canceled") and _same_origin(rurl, origin):
                        console.append({"type": "request_failed", "text": p.get("errorText"), "url": rurl})
                elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                    text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
                    console.append({"type": "console_error", "text": text[:500]})
                elif m == "Runtime.exceptionThrown":
                    d = p.get("exceptionDetails", {})
                    text = (d.get("exception") or {}).get("description") or d.get("text") or ""
                    console.append({"type": "exception", "text": text[:500], "url": d.get("url"),
                                    "line": d.get("lineNumber")})
                elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                    e = p["entry"]
                    if e.get("source") == "network":
                        continue        # 请求失败已由 Network.* 事件按同源口径记录，避免重复
                    console.append({"type": "log_error", "text": str(e.get("text"))[:500], "url": e.get("url")})

        start = time.monotonic()
        while True:
            browser.pump(0.05)
            drain()
            now = time.monotonic()
            if state["loaded"] and not inflight and now - state["last"] >= 0.5:
                break
            if now - start > settle_timeout:
                if not state["loaded"]:
                    raise ToolError("页面在 %.0fs 内未完成加载: %s" % (settle_timeout, url))
                notes.append("网络未在 %.0fs 内静默（仍有 %d 个请求在途），按当前状态采集"
                             % (settle_timeout, len(inflight)))
                break
        if state["doc_status"] is not None and state["doc_status"] >= 500:
            raise ToolError("页面不可达: %s（HTTP %s）" % (url, state["doc_status"]))
        # 字体就绪 + 两帧，随后冻结定时器：采集期间页面不再自行重绘（探针标记不会被轮询重渲染冲掉）
        evaluate("(async()=>{try{await document.fonts.ready;}catch(e){}"
                 "const raf=window.__vgRaf||requestAnimationFrame;"
                 "await new Promise(r=>raf(()=>raf(r)));"
                 "if(window.__vgFreezeTimers)window.__vgFreezeTimers();return true;})()", await_promise=True)
        browser.pump(0.1)
        # captureBeyondViewport + clip 走整页重新光栅化：实测直接截视口时，分数像素行上的文字会在两次加载间
        # 随机偏 1px（版式数值完全相同，仅光栅取整不同），该路径 8/8 次逐字节一致
        shot = base64.b64decode(cmd("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": viewport[0], "height": viewport[1], "scale": 1}})["data"])
        allow_contrast = contract["allow"]["contrast"]
        cfg = {"props": list(STYLE_PROPS),
               "probes": [{"name": n, "selector": s} for n, s in DEFAULT_PROBES]
               + [{"name": p["key"], "selector": p["selector"], "key": p["key"]} for p in contract["probes"]],
               "probeCap": PROBE_CAP, "layoutCap": LAYOUT_ID_CAP, "contrastCap": CONTRAST_CAP, "nativeCap": 60,
               "allowSelectors": [a for a in allow_contrast if not a.startswith("--")],
               "allowTokens": [a for a in allow_contrast if a.startswith("--")]}
        data = json.loads(evaluate(COLLECT_JS + "(" + json.dumps(cfg) + ")"))
        # —— 逐探针强制伪类：hover、focus+focus-visible（键盘聚焦的真实组合，才能暴露 :focus{outline:none} 压过
        #    :focus-visible 的特异性问题）——
        doc = cmd("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        for probe in data["probes"]:
            node = cmd("DOM.querySelector", {"nodeId": doc,
                                             "selector": '[data-vg-probe="%d"]' % probe["index"]}).get("nodeId")
            if not node:
                continue
            for name, pseudo in (("hover", ["hover"]), ("focus", ["focus", "focus-visible"])):
                if name == "focus" and not probe["focusable"]:
                    continue
                cmd("CSS.forcePseudoState", {"nodeId": node, "forcedPseudoClasses": pseudo})
                st = json.loads(evaluate("window.__vgState(%d)" % probe["index"]))
                if st:
                    probe["states"][name] = st
            cmd("CSS.forcePseudoState", {"nodeId": node, "forcedPseudoClasses": []})
        browser.pump(0.05)
        drain()
        skipped = data.get("contrast_disabled", 0) + data.get("contrast_offcanvas", 0)
        if data.get("contrast_total", 0) > len(data["contrast"]) + skipped:
            notes.append("文本元素共 %d 个，按等距抽样检查 %d 个" % (data["contrast_total"], len(data["contrast"])))
        if skipped:
            notes.append("对比度未检查：禁用控件内 %d 个、画布外（收起的抽屉等）%d 个文本元素"
                         % (data.get("contrast_disabled", 0), data.get("contrast_offcanvas", 0)))
        data["console"] = console
        data["notes"] = notes
        return data, shot
    finally:
        for method, params in (("Target.closeTarget", {"targetId": target}),
                               ("Target.disposeBrowserContext", {"browserContextId": ctx})):
            try:
                browser.send(method, params, timeout=5)
            except Exception:  # noqa: BLE001 - 收尾尽力而为
                pass


# ====================================================================
# 编排 / CLI
# ====================================================================
def run_gate(url, baseline_url=None, contract=None, out_dir="visual-gate-out", skins=("dark", "light"),
             viewports=((1440, 900), (390, 844)), freeze_time=DEFAULT_FREEZE_TIME, chrome_path=None):
    """跑完整闸门并写报告，返回报告 dict。工具错误抛 ToolError。"""
    contract = contract or validate_contract({})
    frozen = freeze_ms(freeze_time)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs, images = [], {}
    with Browser(chrome_path) as browser:
        for skin in skins:
            for vp in viewports:
                try:
                    cand, shot = collect_page(browser, url, skin, vp, frozen, contract)
                    base = base_shot = None
                    if baseline_url:
                        base, base_shot = collect_page(browser, baseline_url, skin, vp, frozen, contract)
                except CDPError as e:
                    raise ToolError("浏览器采集失败（%s %dx%d）: %s" % (skin, vp[0], vp[1], e))
                run = evaluate_run(skin, vp, cand, base, contract)
                name = "%s-%dx%d.png" % (skin, vp[0], vp[1])
                images[name] = shot
                run["screenshot"] = name
                if base_shot is not None:
                    bname = "%s-%dx%d-baseline.png" % (skin, vp[0], vp[1])
                    images[bname] = base_shot
                    run["baseline_screenshot"] = bname
                    run["screenshot_identical"] = base_shot == shot
                runs.append(run)
        meta = {"url": url, "baseline_url": baseline_url, "skins": list(skins),
                "viewports": ["%dx%d" % tuple(v) for v in viewports], "frozen_time": freeze_time,
                "chrome_version": browser.version}
    report = assemble_report(meta, runs)
    for name, data in images.items():
        (out / name).write_bytes(data)
    (out / "visual-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "visual-report.html").write_text(render_html(report, images), encoding="utf-8")
    return report


def build_parser():
    ap = argparse.ArgumentParser(
        prog="visual_gate.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="视觉闸门：无头 Chromium 渲染页面并跑 console / tokens / computed_style / layout_shift / "
                    "contrast / native_controls 六个闸门，产出 JSON + HTML 报告与截图。",
        epilog="""闸门说明:
  console          控制台 error、未捕获异常、同源请求失败/4xx/5xx → 失败
  tokens           采集 :root 全部 CSS 自定义属性；有基线时取值不同即失败（allow.tokens 放行）
  computed_style   探针（按钮/输入/下拉/文本域/链接/卡片，每类最多 12 个）在常态、强制 :hover、
                   强制 :focus+:focus-visible 下的关键计算样式；可聚焦控件须有可见焦点指示
                   （outline 宽度>0 且非透明，或 box-shadow 变化），hover 不得改变包围盒；
                   有基线时列出属性差异（allow.computed 按探针键放行）
  layout_shift     需 --baseline-url：同键元素（id 优先，否则 CSS 路径）包围盒差异 > 0.5px 即失败
  contrast         可见文本对比度（正文 4.5 / 大字 3.0）；背景含渐变/图片记 undetermined，不算通过
                   （allow.contrast：选择器或 --令牌名）
  native_controls  仅 dark 皮肤：select/input/textarea 背景偏亮，或控件/滚动容器 color-scheme 不含 dark

契约文件示例:
  {"allow": {"tokens": ["--accent"], "contrast": ["--text-3", ".muted"],
             "computed": ["#saveBtn"], "layout": ["#navList*"]},
   "probes": [{"key": "save", "selector": "#saveBtn"}]}

退出码: 0 全部通过 / 1 有闸门失败 / 2 工具错误（找不到 Chromium、页面不可达、参数或契约非法）
浏览器: 默认找 playwright 缓存的 chromium headless shell，其次本机 Google Chrome；
        可用环境变量 VISUAL_GATE_CHROME 指定可执行文件。

典型用法（配合种子数据测试实例，候选与基线各起一个实例）:
  REQ_BOARD_PYTHON=<python> scripts/test_instance.sh start 8931 --seed
  <python> scripts/visual_gate.py --url http://127.0.0.1:8931/ --skins dark,light --viewports 1440x900
  REQ_BOARD_PYTHON=<python> scripts/test_instance.sh stop 8931
""")
    ap.add_argument("--url", required=True, help="候选页面 URL（必填）")
    ap.add_argument("--baseline-url", help="基线页面 URL；给出后启用令牌/计算样式/版式差异比对与并排截图")
    ap.add_argument("--contract", help="契约 JSON 文件路径（放行清单 + 自定义探针）")
    ap.add_argument("--out", default="visual-gate-out", help="产物目录（默认 ./visual-gate-out）")
    ap.add_argument("--skins", default="dark,light", help="皮肤列表，逗号分隔（默认 dark,light）")
    ap.add_argument("--viewports", default="1440x900,390x844", help="视口列表（默认 1440x900,390x844）")
    ap.add_argument("--freeze-time", default=DEFAULT_FREEZE_TIME,
                    help="冻结页面时钟到该北京时间（默认 %s，与种子数据一致）" % DEFAULT_FREEZE_TIME)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        contract = load_contract(args.contract)
        report = run_gate(args.url, args.baseline_url, contract, args.out,
                          parse_skins(args.skins), parse_viewports(args.viewports), args.freeze_time)
    except ToolError as e:
        print("视觉闸门工具错误: %s" % e, file=sys.stderr)
        return EXIT_TOOL_ERROR
    for run in report["runs"]:
        parts = ["%s=%s" % (name, run["gates"][name]["status"]) for name in GATES]
        print("[%s %s] %s  %s" % (run["skin"], run["viewport"], run["status"].upper(), " ".join(parts)))
    print("结论: %s  报告: %s" % (report["status"].upper(), Path(args.out) / "visual-report.html"))
    return decide_exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
