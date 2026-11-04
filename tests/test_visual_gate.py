#!/usr/bin/env python3
"""视觉闸门（scripts/visual_gate.py）：纯函数单测 + 一个浏览器集成测试。

纯函数（不需要浏览器）：
- V1 颜色解析 / alpha 合成 / 相对亮度 / 对比度（对照 WCAG 已知值）/ 大字判定
- V2 有效背景：半透明层逐层合成、渐变或无法解析 → None（undetermined，绝不当通过）
- V3 contrast 闸门：阈值、放行（选择器 / 令牌色）、undetermined 计数
- V4 令牌差异 + 放行；包围盒差异容差 / 新增缺失 / 排序
- V5 computed_style：焦点指示、hover 零位移、基线差异、按探针键放行
- V6 native_controls：仅深色皮肤、漏白与 color-scheme
- V7 契约校验（未知顶层键拒绝）、参数解析、报告组装、退出码、HTML 报告转义

浏览器集成（找不到 Chromium 时跳过）：
- V8 http.server 起两张静态页（干净基线 / 注入缺陷），断言每类缺陷被对应闸门报出、干净页全绿

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest -q tests/test_visual_gate.py

隔离：产物全部落临时目录；浏览器由 visual_gate.Browser 自己拉起并经进程句柄收尾，不按名字杀进程。
"""
import functools
import http.server
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

sys.path.insert(0, str(REPO / "scripts"))
import seed_test_db  # noqa: E402
import visual_gate as vg  # noqa: E402

WHITE, BLACK = (255.0, 255.0, 255.0), (0.0, 0.0, 0.0)


def _layer(color, img=False, op=1.0):
    return {"c": color, "img": img, "op": op}


class ColorMathTest(unittest.TestCase):
    """V1"""

    def test_parse_color_forms(self):
        self.assertEqual(vg.parse_color("rgb(255, 0, 10)"), (255.0, 0.0, 10.0, 1.0))
        self.assertEqual(vg.parse_color("rgba(1, 2, 3, 0.5)"), (1.0, 2.0, 3.0, 0.5))
        self.assertEqual(vg.parse_color("rgb(1 2 3 / 25%)"), (1.0, 2.0, 3.0, 0.25))
        self.assertEqual(vg.parse_color("#fff"), (255.0, 255.0, 255.0, 1.0))
        self.assertEqual(vg.parse_color("#10203080")[:3], (16.0, 32.0, 48.0))
        self.assertAlmostEqual(vg.parse_color("#10203080")[3], 128 / 255)
        self.assertEqual(vg.parse_color("transparent"), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(vg.parse_color("color(srgb 1 0.5 0 / 0.5)"), (255.0, 127.5, 0.0, 0.5))

    def test_parse_color_unsupported_is_none(self):
        for bad in ("", None, "oklch(0.7 0.1 200)", "red", "#12", "rgb(1,2)", "linear-gradient(#000,#fff)"):
            self.assertIsNone(vg.parse_color(bad), bad)

    def test_contrast_known_values(self):
        self.assertAlmostEqual(vg.contrast_ratio(BLACK, WHITE), 21.0, places=6)
        self.assertAlmostEqual(vg.contrast_ratio(WHITE, WHITE), 1.0, places=6)
        # #767676 on white 是公认的 AA 临界色（≈4.54）；#777 略低于 4.5
        self.assertAlmostEqual(vg.contrast_ratio((118.0, 118.0, 118.0), WHITE), 4.54, places=2)
        self.assertLess(vg.contrast_ratio((119.0, 119.0, 119.0), WHITE), 4.5)
        self.assertAlmostEqual(vg.contrast_ratio((255.0, 0.0, 0.0), WHITE), 4.0, places=1)
        # 对称
        self.assertEqual(vg.contrast_ratio(BLACK, WHITE), vg.contrast_ratio(WHITE, BLACK))

    def test_relative_luminance_endpoints(self):
        self.assertAlmostEqual(vg.relative_luminance(WHITE), 1.0)
        self.assertAlmostEqual(vg.relative_luminance(BLACK), 0.0)
        self.assertAlmostEqual(vg.relative_luminance((0.0, 255.0, 0.0)), 0.7152)

    def test_composite(self):
        self.assertEqual(vg.composite((0.0, 0.0, 0.0, 0.5), WHITE), (127.5, 127.5, 127.5))
        self.assertEqual(vg.composite((10.0, 20.0, 30.0, 1.0), WHITE), (10.0, 20.0, 30.0))
        self.assertEqual(vg.composite((10.0, 20.0, 30.0, 0.0), WHITE), WHITE)

    def test_is_large_text(self):
        self.assertTrue(vg.is_large_text(24, 400))
        self.assertFalse(vg.is_large_text(23.9, 400))
        self.assertTrue(vg.is_large_text(18.66, 700))
        self.assertFalse(vg.is_large_text(18.66, 600))
        self.assertFalse(vg.is_large_text(18.5, 700))
        self.assertFalse(vg.is_large_text("abc", None))


class EffectiveBackgroundTest(unittest.TestCase):
    """V2"""

    def test_first_opaque_layer_wins(self):
        layers = [_layer("rgba(0, 0, 0, 0)"), _layer("rgb(10, 20, 30)"), _layer("rgb(255, 255, 255)")]
        self.assertEqual(vg.effective_background(layers), (10.0, 20.0, 30.0))

    def test_translucent_layers_composited_in_order(self):
        # 白底上 50% 黑 → 127.5 灰；再叠 50% 白 → 191.25
        layers = [_layer("rgba(255, 255, 255, 0.5)"), _layer("rgba(0, 0, 0, 0.5)"), _layer("rgb(255, 255, 255)")]
        self.assertEqual(vg.effective_background(layers), (191.25, 191.25, 191.25))

    def test_opacity_makes_layer_translucent(self):
        layers = [_layer("rgb(0, 0, 0)", op=0.5), _layer("rgb(255, 255, 255)")]
        self.assertEqual(vg.effective_background(layers), (127.5, 127.5, 127.5))

    def test_falls_back_to_canvas(self):
        self.assertEqual(vg.effective_background([_layer("rgba(0, 0, 0, 0)")]), WHITE)
        self.assertEqual(vg.effective_background([], canvas=vg.canvas_color("dark")), (18.0, 18.0, 18.0))
        self.assertEqual(vg.canvas_color("light dark"), (18.0, 18.0, 18.0))
        self.assertEqual(vg.canvas_color("normal"), WHITE)

    def test_gradient_or_unparsable_is_undetermined(self):
        self.assertIsNone(vg.effective_background([_layer("rgba(0, 0, 0, 0)", img=True), _layer("rgb(0, 0, 0)")]))
        self.assertIsNone(vg.effective_background([_layer("oklch(0.5 0.1 20)"), _layer("rgb(0, 0, 0)")]))
        # 不透明层之外的渐变不影响判定
        self.assertEqual(vg.effective_background([_layer("rgb(1, 2, 3)"), _layer("rgba(0,0,0,0)", img=True)]),
                         (1.0, 2.0, 3.0))


def _sample(color, bg="rgb(255, 255, 255)", size="14px", weight="400", key="p", allow=(), layers=None):
    return {"key": key, "text": "示例", "color": color, "font_size": size, "font_weight": weight,
            "layers": layers if layers is not None else [_layer("rgba(0, 0, 0, 0)"), _layer(bg)],
            "allow": list(allow)}


class ContrastGateTest(unittest.TestCase):
    """V3"""

    def test_pass_and_fail_thresholds(self):
        res = vg.evaluate_contrast([_sample("rgb(118, 118, 118)", key="ok"),
                                    _sample("rgb(119, 119, 119)", key="bad")])
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["counts"], {"sampled": 2, "passed": 1, "failed": 1, "undetermined": 0, "allowed": 0})
        f = res["failures"][0]
        self.assertEqual((f["key"], f["required"], f["background"]), ("bad", 4.5, "rgb(255, 255, 255)"))
        self.assertAlmostEqual(f["ratio"], 4.48, places=2)

    def test_large_text_uses_3(self):
        gray = "rgb(148, 148, 148)"     # ≈3.03:1
        self.assertEqual(vg.evaluate_contrast([_sample(gray, size="24px")])["status"], "pass")
        self.assertEqual(vg.evaluate_contrast([_sample(gray, size="19px", weight="700")])["status"], "pass")
        self.assertEqual(vg.evaluate_contrast([_sample(gray, size="19px")])["status"], "fail")

    def test_text_alpha_and_ancestor_opacity_reduce_contrast(self):
        self.assertEqual(vg.evaluate_contrast([_sample("rgb(0, 0, 0)")])["status"], "pass")
        self.assertEqual(vg.evaluate_contrast([_sample("rgba(0, 0, 0, 0.3)")])["status"], "fail")
        faded = [_layer("rgba(0, 0, 0, 0)", op=0.3), _layer("rgb(255, 255, 255)")]
        self.assertEqual(vg.evaluate_contrast([_sample("rgb(0, 0, 0)", layers=faded)])["status"], "fail")

    def test_undetermined_never_counts_as_pass(self):
        grad = [_layer("rgba(0, 0, 0, 0)", img=True), _layer("rgb(255, 255, 255)")]
        res = vg.evaluate_contrast([_sample("rgb(0, 0, 0)", layers=grad, key="g"),
                                    _sample("oklch(0.2 0 0)", key="c")])
        self.assertEqual(res["counts"]["undetermined"], 2)
        self.assertEqual(res["counts"]["passed"], 0)
        self.assertEqual(res["counts"]["failed"], 0)
        self.assertEqual({u["key"] for u in res["undetermined"]}, {"g", "c"})

    def test_allow_by_selector_and_token(self):
        low = "rgb(200, 200, 200)"
        res = vg.evaluate_contrast(
            [_sample(low, key="sel", allow=[".muted"]), _sample(low, key="tok"),
             _sample("rgb(210, 210, 210)", key="other")],
            allow_token_colors={"--text-3": "rgb(200, 200, 200)"})
        self.assertEqual(res["counts"]["allowed"], 2)
        self.assertEqual([f["key"] for f in res["failures"]], ["other"])

    def test_failures_sorted_worst_first_and_capped(self):
        samples = [_sample("rgb(%d, %d, %d)" % (v, v, v), key="k%03d" % v) for v in range(130, 130 + 70)]
        res = vg.evaluate_contrast(samples)
        self.assertEqual(res["counts"]["failed"], 70)
        self.assertEqual(len(res["failures"]), vg.FAILURE_CAP)
        self.assertEqual(res["truncated"], 70 - vg.FAILURE_CAP)
        ratios = [f["ratio"] for f in res["failures"]]
        self.assertEqual(ratios, sorted(ratios))
        self.assertEqual(res["failures"][0]["key"], "k199")


class DiffTest(unittest.TestCase):
    """V4"""

    def test_token_diff_with_allowlist(self):
        base = {"--a": "#000", "--b": "1px", "--gone": "x", "--text-3": "#888"}
        cand = {"--a": "#111", "--b": "1px", "--new": "y", "--text-3": "#999"}
        diffs, allowed = vg.diff_tokens(base, cand, allow=["--text-*"])
        self.assertEqual(diffs, [{"token": "--a", "baseline": "#000", "candidate": "#111"},
                                 {"token": "--gone", "baseline": "x", "candidate": None},
                                 {"token": "--new", "baseline": None, "candidate": "y"}])
        self.assertEqual(allowed, [{"token": "--text-3", "baseline": "#888", "candidate": "#999"}])

    def test_tokens_gate(self):
        self.assertEqual(vg.evaluate_tokens({"--a": "1"})["status"], "pass")          # 无基线：仅采集
        self.assertEqual(vg.evaluate_tokens({"--a": "1"}, {"--a": "1"})["status"], "pass")
        res = vg.evaluate_tokens({"--a": "2"}, {"--a": "1"})
        self.assertEqual((res["status"], res["counts"]["changed"]), ("fail", 1))
        self.assertEqual(vg.evaluate_tokens({"--a": "2"}, {"--a": "1"}, allow=["--a"])["status"], "pass")

    def test_bbox_tolerance(self):
        base = {"#a": {"x": 0, "y": 0, "w": 100, "h": 20}, "#b": {"x": 0, "y": 30, "w": 100, "h": 20},
                "#c": {"x": 0, "y": 60, "w": 100, "h": 20}}
        cand = {"#a": {"x": 0.5, "y": 0, "w": 100, "h": 20},      # 恰在容差内
                "#b": {"x": 0, "y": 30.6, "w": 100, "h": 20},     # 超容差
                "#c": {"x": 0, "y": 60, "w": 96, "h": 20}}
        diffs, allowed = vg.diff_bboxes(base, cand, tolerance=0.5)
        self.assertEqual([d["key"] for d in diffs], ["#c", "#b"])        # 最严重在前
        self.assertEqual(diffs[0]["dw"], -4)
        self.assertEqual(diffs[1]["dy"], 0.6)
        self.assertEqual(allowed, 0)

    def test_bbox_added_missing_and_allow(self):
        box = {"x": 0, "y": 0, "w": 1, "h": 1}
        diffs, allowed = vg.diff_bboxes({"#old": box, "#x": box}, {"#new": box, "#x": dict(box, x=9)},
                                        allow=["#x"])
        self.assertEqual({(d["key"], d["change"]) for d in diffs}, {("#old", "missing"), ("#new", "added")})
        self.assertEqual(allowed, 1)
        json.dumps(diffs)      # 可序列化（无 inf）

    def test_layout_gate_needs_baseline(self):
        self.assertEqual(vg.evaluate_layout({"#a": {"x": 0, "y": 0, "w": 1, "h": 1}})["status"], "skipped")
        same = {"#a": {"x": 0, "y": 0, "w": 1, "h": 1}}
        self.assertEqual(vg.evaluate_layout(same, dict(same))["status"], "pass")


def _props(**over):
    base = {"color": "rgb(0, 0, 0)", "outline-style": "none", "outline-width": "0px",
            "outline-color": "rgb(0, 0, 0)", "box-shadow": "none", "font-size": "14px"}
    base.update({k.replace("_", "-"): v for k, v in over.items()})
    return base


def _probe(key="#btn", focusable=True, normal=None, hover=None, focus=None, hover_box=None):
    box = {"x": 10, "y": 10, "w": 80, "h": 30}
    states = {"normal": {"props": normal or _props(), "bbox": dict(box)},
              "hover": {"props": hover or _props(), "bbox": hover_box or dict(box)}}
    if focusable:
        states["focus"] = {"props": focus or _props(outline_style="solid", outline_width="2px"), "bbox": dict(box)}
    return {"key": key, "selector": "button", "focusable": focusable, "states": states}


class ComputedGateTest(unittest.TestCase):
    """V5"""

    def test_focus_indicator_rules(self):
        n = _props()
        self.assertTrue(vg.has_focus_indicator(n, _props(outline_style="solid", outline_width="2px")))
        self.assertFalse(vg.has_focus_indicator(n, _props(outline_style="none", outline_width="2px")))
        self.assertFalse(vg.has_focus_indicator(n, _props(outline_style="solid", outline_width="0px")))
        self.assertFalse(vg.has_focus_indicator(n, _props(outline_style="solid", outline_width="2px",
                                                          outline_color="rgba(0, 0, 0, 0)")))
        ring = "rgb(0, 100, 255) 0px 0px 0px 2px"
        self.assertTrue(vg.has_focus_indicator(n, _props(box_shadow=ring)))
        self.assertFalse(vg.has_focus_indicator(_props(box_shadow=ring), _props(box_shadow=ring)))   # 没变化

    def test_focus_missing_fails(self):
        res = vg.evaluate_computed([_probe(focus=_props())])
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["counts"]["focus_missing"], 1)
        self.assertEqual(res["failures"][0]["rule"], "focus_visible")

    def test_non_focusable_not_checked(self):
        self.assertEqual(vg.evaluate_computed([_probe(focusable=False)])["status"], "pass")

    def test_hover_shift(self):
        res = vg.evaluate_computed([_probe(hover_box={"x": 10, "y": 10, "w": 84, "h": 30})])
        self.assertEqual(res["counts"]["hover_shift"], 1)
        self.assertEqual(res["failures"][0]["delta"], 4)
        ok = vg.evaluate_computed([_probe(hover_box={"x": 10.4, "y": 10, "w": 80, "h": 30})])
        self.assertEqual(ok["status"], "pass")

    def test_baseline_diff_and_allow(self):
        base = [_probe()]
        cand = [_probe(normal=_props(font_size="13px"))]
        res = vg.evaluate_computed(cand, base)
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["failures"][0]["diff"], {"font-size": ["14px", "13px"]})
        self.assertEqual(res["failures"][0]["state"], "normal")
        self.assertEqual(vg.evaluate_computed(cand, base, allow=["#btn"])["status"], "pass")
        self.assertEqual(vg.evaluate_computed(cand, base, allow=["#b*"])["counts"]["allowed"], 1)
        self.assertEqual(vg.evaluate_computed(base, [_probe()])["status"], "pass")


class NativeControlsGateTest(unittest.TestCase):
    """V6"""
    PAGE = [_layer("rgb(16, 18, 24)")]

    def _ctrl(self, bg, scheme="dark", kind="control", **extra):
        return dict({"key": "#c", "kind": kind, "tag": "select", "type": "select-one", "color_scheme": scheme,
                     "layers": [_layer(bg), _layer("rgb(16, 18, 24)")]}, **extra)

    def test_light_skin_skipped(self):
        self.assertEqual(vg.evaluate_native_controls("light", [], self.PAGE, "light")["status"], "skipped")

    def test_non_dark_page_skipped(self):
        res = vg.evaluate_native_controls("dark", [self._ctrl("rgb(255, 255, 255)")],
                                          [_layer("rgb(250, 250, 250)")], "normal")
        self.assertEqual(res["status"], "skipped")

    def test_white_leak_detected(self):
        res = vg.evaluate_native_controls("dark", [self._ctrl("rgb(255, 255, 255)", scheme="normal"),
                                                   self._ctrl("rgb(30, 34, 44)")], self.PAGE, "dark")
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["counts"]["light_background"], 1)
        self.assertEqual(res["counts"]["color_scheme"], 1)
        self.assertEqual(res["failures"][0]["reasons"], ["light_background", "color_scheme"])

    def test_color_scheme_only(self):
        res = vg.evaluate_native_controls("dark", [self._ctrl("rgba(0, 0, 0, 0)", scheme="light")], self.PAGE, "dark")
        self.assertEqual(res["failures"][0]["reasons"], ["color_scheme"])

    def test_scroller_custom_scrollbar_ok(self):
        res = vg.evaluate_native_controls(
            "dark", [self._ctrl("rgba(0,0,0,0)", scheme="normal", kind="scroller", scrollbar_color="auto"),
                     self._ctrl("rgba(0,0,0,0)", scheme="normal", kind="scroller",
                                scrollbar_color="rgb(60, 60, 60) rgba(0, 0, 0, 0)")], self.PAGE, "dark")
        self.assertEqual(res["counts"]["color_scheme"], 1)


class ContractAndReportTest(unittest.TestCase):
    """V7"""

    def test_contract_defaults(self):
        self.assertEqual(vg.validate_contract({}), {"allow": {k: [] for k in vg.ALLOW_KEYS}, "probes": []})
        self.assertEqual(vg.validate_contract(None)["probes"], [])

    def test_contract_valid(self):
        c = vg.validate_contract({"allow": {"tokens": ["--a"], "contrast": ["--text-3", ".muted"]},
                                  "probes": [{"key": "save", "selector": "#saveBtn"}]})
        self.assertEqual(c["allow"]["contrast"], ["--text-3", ".muted"])
        self.assertEqual(c["allow"]["layout"], [])
        self.assertEqual(c["probes"], [{"key": "save", "selector": "#saveBtn"}])

    def test_contract_rejects_unknown_and_malformed(self):
        bad = [({"allows": {}}, "未知顶层键: allows"),
               ({"allow": {"token": []}}, "allow 含未知键: token"),
               ({"allow": {"tokens": "x"}}, "allow.tokens"),
               ({"allow": {"tokens": [1]}}, "allow.tokens"),
               ({"allow": []}, "allow 必须是对象"),
               ({"probes": {}}, "probes 必须是数组"),
               ({"probes": [{"key": "a"}]}, "probes[0]"),
               ({"probes": [{"key": "a", "selector": "b", "x": 1}]}, "probes[0]"),
               ({"probes": [{"key": "a", "selector": "b"}, {"key": "a", "selector": "c"}]}, "key 重复"),
               ([], "顶层必须是 JSON 对象")]
        for obj, msg in bad:
            with self.assertRaises(vg.ToolError) as cm:
                vg.validate_contract(obj)
            self.assertIn(msg, str(cm.exception))

    def test_load_contract_errors(self):
        tmp = Path(tempfile.mkdtemp(prefix="vg-contract-"))
        try:
            with self.assertRaises(vg.ToolError):
                vg.load_contract(tmp / "missing.json")
            (tmp / "bad.json").write_text("{not json")
            with self.assertRaises(vg.ToolError):
                vg.load_contract(tmp / "bad.json")
            (tmp / "ok.json").write_text(json.dumps({"allow": {"layout": ["#x"]}}))
            self.assertEqual(vg.load_contract(tmp / "ok.json")["allow"]["layout"], ["#x"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_arg_parsers(self):
        self.assertEqual(vg.parse_viewports("1440x900, 390x844"), [(1440, 900), (390, 844)])
        self.assertEqual(vg.parse_skins("dark, light"), ["dark", "light"])
        for fn, bad in ((vg.parse_viewports, "1440"), (vg.parse_viewports, ""), (vg.parse_skins, "blue"),
                        (vg.parse_skins, ""), (vg.freeze_ms, "2026-13-01 00:00:00"), (vg.freeze_ms, None)):
            with self.assertRaises(vg.ToolError):
                fn(bad)
        from storage_time import parse_storage_time
        self.assertEqual(vg.freeze_ms("1970-01-01 08:00:01"),
                         int(parse_storage_time("1970-01-01 08:00:01").timestamp() * 1000))
        self.assertEqual(vg.DEFAULT_FREEZE_TIME, seed_test_db.DEFAULT_FREEZE_TIME)

    def _collection(self, **over):
        base = {"console": [], "tokens": {"--bg": "#101218"}, "root_color_scheme": "dark",
                "page_layers": [_layer("rgb(16, 18, 24)")], "probes": [_probe()],
                "layout": [{"key": "#main", "bbox": {"x": 0, "y": 0, "w": 800, "h": 600}}],
                "contrast": [_sample("rgb(230, 230, 230)", bg="rgb(16, 18, 24)")], "native": [], "notes": []}
        base.update(over)
        return base

    def test_evaluate_run_and_report(self):
        clean = self._collection()
        run = vg.evaluate_run("dark", (1440, 900), clean, clean)
        self.assertEqual(run["status"], "pass")
        self.assertEqual(set(run["gates"]), set(vg.GATES))
        self.assertEqual(run["viewport"], "1440x900")
        no_base = vg.evaluate_run("dark", (1440, 900), clean)
        self.assertEqual(no_base["gates"]["layout_shift"]["status"], "skipped")
        self.assertEqual(no_base["status"], "pass")
        broken = self._collection(console=[{"type": "console_error", "text": "boom"}],
                                  tokens={"--bg": "#000"})
        bad = vg.evaluate_run("dark", (1440, 900), broken, clean)
        self.assertEqual(bad["gates"]["console"]["status"], "fail")
        self.assertEqual(bad["gates"]["tokens"]["status"], "fail")
        self.assertEqual(bad["status"], "fail")
        meta = {"url": "u", "baseline_url": None, "skins": ["dark"], "viewports": ["1440x900"],
                "frozen_time": vg.DEFAULT_FREEZE_TIME, "chrome_version": "x"}
        good, failed = vg.assemble_report(meta, [run, no_base]), vg.assemble_report(meta, [run, bad])
        self.assertEqual((good["status"], vg.decide_exit_code(good)), ("pass", vg.EXIT_PASS))
        self.assertEqual((failed["status"], vg.decide_exit_code(failed)), ("fail", vg.EXIT_FAIL))
        self.assertEqual((vg.EXIT_PASS, vg.EXIT_FAIL, vg.EXIT_TOOL_ERROR), (0, 1, 2))
        self.assertEqual(set(good["meta"]), {"url", "baseline_url", "skins", "viewports", "frozen_time",
                                             "chrome_version"})
        json.dumps(failed)

    def test_contract_allow_applied_in_run(self):
        clean = self._collection()
        changed = self._collection(tokens={"--bg": "#000"})
        contract = vg.validate_contract({"allow": {"tokens": ["--bg"]}})
        self.assertEqual(vg.evaluate_run("dark", (1440, 900), changed, clean, contract)["status"], "pass")

    def test_html_report_self_contained_and_escaped(self):
        clean = self._collection()
        broken = self._collection(console=[{"type": "console_error", "text": "<script>alert(1)</script>"}])
        run = vg.evaluate_run("dark", (1440, 900), broken, clean)
        run["screenshot"], run["baseline_screenshot"] = "dark-1440x900.png", "dark-1440x900-baseline.png"
        report = vg.assemble_report({"url": "http://x/?a=<b>", "baseline_url": "http://y/", "frozen_time": "t",
                                     "chrome_version": "c"}, [run])
        page = vg.render_html(report, {"dark-1440x900.png": b"\x89PNG1", "dark-1440x900-baseline.png": b"\x89PNG2"})
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertEqual(page.count("data:image/png;base64,"), 2)
        self.assertLess(page.index("dark-1440x900-baseline.png"), page.rindex("dark-1440x900.png"))
        self.assertNotIn("http://x/?a=<b>", page)
        for gate in vg.GATES:
            self.assertIn(gate, page)

    def test_help_is_chinese_and_complete(self):
        text = vg.build_parser().format_help()
        for word in ("--url", "--baseline-url", "--contract", "--out", "--skins", "--viewports", "--freeze-time",
                     "退出码", "契约文件", "native_controls", "VISUAL_GATE_CHROME"):
            self.assertIn(word, text)


# ==================== V8 浏览器集成 ====================
_CLEAN_CSS = """
:root{--bg:#101218;--text:#e6e9ef;--accent:#4f8cff;color-scheme:dark}
body{margin:0;padding:24px;background:var(--bg);color:var(--text);font:14px/1.5 sans-serif}
button,input,select{font:inherit;color:var(--text);background:#1c2030;border:1px solid #39405a;padding:6px 12px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.card{background:#161a26;border:1px solid #2a3044;padding:12px;margin-top:12px;width:320px}
#go{margin-left:0}
"""
_BODY = """<p id="lead">正文文本，对比度充足。</p>
<button id="go" type="button">保存</button> <button id="more" type="button">更多</button>
<input id="name" value="输入内容"> <select id="pick"><option>选项一</option><option>选项二</option></select>
<div class="card" id="card1"><span id="note">卡片里的说明文字</span></div>"""
# 每条缺陷对应一个闸门：令牌改值 / 低对比度 / 元素位移 / :focus{outline:none} 压过 :focus-visible /
# hover 撑大包围盒 / 深色页上的亮色原生下拉 / console.error
_DEFECT_CSS = """
:root{--accent:#ff4f8c}
#note{color:#3a3f50}
#go{margin-left:6px}
input:focus{outline:none}
#more:hover{padding:8px 16px}
#pick{background:#fff;color:#111;color-scheme:light}
"""


def _page(css, script=""):
    return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><link rel="icon" href="data:,">'
            "<style>%s</style></head><body>%s%s</body></html>" % (css, _BODY, script))


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):   # 测试输出保持干净
        pass


@unittest.skipUnless(vg.find_chrome(), "未找到 Chromium（playwright headless shell / Google Chrome）")
class BrowserIntegrationTest(unittest.TestCase):
    """V8：真实浏览器跑闸门（两张静态页，临时端口）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vg-int-"))
        site = cls.tmp / "site"
        site.mkdir()
        (site / "clean.html").write_text(_page(_CLEAN_CSS), encoding="utf-8")
        (site / "defect.html").write_text(
            _page(_CLEAN_CSS + _DEFECT_CSS, "<script>console.error('vg-boom');</script>"), encoding="utf-8")
        handler = functools.partial(_QuietHandler, directory=str(site))
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, page, out, extra=()):
        argv = ["--url", "%s/%s" % (self.base, page), "--baseline-url", self.base + "/clean.html",
                "--skins", "dark", "--viewports", "800x600", "--out", str(self.tmp / out), *extra]
        code = vg.main(argv)
        report = json.loads((self.tmp / out / "visual-report.json").read_text(encoding="utf-8"))
        return code, report

    def test_v8_clean_page_passes(self):
        code, report = self._run("clean.html", "clean")
        gates = report["runs"][0]["gates"]
        self.assertEqual(code, 0, json.dumps(gates, ensure_ascii=False)[:2000])
        self.assertEqual(report["status"], "pass")
        self.assertEqual({g: gates[g]["status"] for g in vg.GATES}, {g: "pass" for g in vg.GATES})
        self.assertEqual(report["runs"][0]["tokens"]["--accent"], "#4f8cff")
        self.assertGreaterEqual(gates["computed_style"]["counts"]["focusable"], 4)
        self.assertGreaterEqual(gates["contrast"]["counts"]["passed"], 5)
        self.assertEqual(gates["contrast"]["counts"]["undetermined"], 0)
        self.assertTrue(report["runs"][0]["screenshot_identical"])    # 同页两次加载截图逐字节一致
        self.assertTrue(report["meta"]["chrome_version"])
        out = self.tmp / "clean"
        for name in ("visual-report.html", "dark-800x600.png", "dark-800x600-baseline.png"):
            self.assertTrue((out / name).stat().st_size > 0, name)
        self.assertEqual((out / "dark-800x600.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_v8_each_defect_reported_by_its_gate(self):
        code, report = self._run("defect.html", "defect")
        self.assertEqual(code, 1)
        gates = report["runs"][0]["gates"]
        self.assertEqual({g: gates[g]["status"] for g in vg.GATES}, {g: "fail" for g in vg.GATES})
        # console
        self.assertTrue(any("vg-boom" in f["text"] for f in gates["console"]["failures"]))
        # tokens
        self.assertEqual(gates["tokens"]["failures"],
                         [{"token": "--accent", "baseline": "#4f8cff", "candidate": "#ff4f8c"}])
        # contrast：低对比度文字（且只有它）
        self.assertEqual([f["key"] for f in gates["contrast"]["failures"]], ["#note"])
        self.assertLess(gates["contrast"]["failures"][0]["ratio"], 3)
        # layout：#go 右移 6px
        moved = {f["key"]: f for f in gates["layout_shift"]["failures"]}
        self.assertEqual(moved["#go"]["dx"], 6)
        # computed：特异性覆盖导致 #name 丢焦点环；#more hover 撑大
        rules = {(f["rule"], f["key"]) for f in gates["computed_style"]["failures"]}
        self.assertIn(("focus_visible", "#name"), rules)
        self.assertIn(("hover_shift", "#more"), rules)
        self.assertNotIn(("focus_visible", "#go"), rules)
        # native：亮色下拉
        leak = {f["key"]: f["reasons"] for f in gates["native_controls"]["failures"]}
        self.assertEqual(leak, {"#pick": ["light_background", "color_scheme"]})

    def test_v8_contract_allowlist_and_probe(self):
        contract = self.tmp / "contract.json"
        contract.write_text(json.dumps({
            "allow": {"tokens": ["--accent"], "contrast": ["#note"], "layout": ["#go", "#more", "#name", "#pick"],
                      "computed": ["#name", "#more", "#go", "#pick"]},
            "probes": [{"key": "lead", "selector": "#lead"}]}), encoding="utf-8")
        code, report = self._run("defect.html", "allowed", ["--contract", str(contract)])
        gates = report["runs"][0]["gates"]
        self.assertEqual(code, 1)      # console 与 native_controls 没有放行通道
        for g in ("tokens", "contrast", "computed_style"):
            self.assertEqual(gates[g]["status"], "pass", (g, gates[g]["failures"]))
        self.assertEqual(gates["contrast"]["counts"]["allowed"], 1)
        self.assertEqual({g for g in vg.GATES if gates[g]["status"] == "fail"} - {"layout_shift"},
                         {"console", "native_controls"})

    def test_v8_tool_errors_exit_2(self):
        out = str(self.tmp / "err")
        self.assertEqual(vg.main(["--url", "http://127.0.0.1:9/", "--skins", "dark",
                                  "--viewports", "800x600", "--out", out]), 2)
        self.assertEqual(vg.main(["--url", self.base + "/clean.html", "--skins", "neon", "--out", out]), 2)


if __name__ == "__main__":
    unittest.main()
