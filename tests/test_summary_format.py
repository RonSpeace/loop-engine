#!/usr/bin/env python3
"""req-84 调度器写开发总结按要点分行测试：format_summary_points 纯函数 L0 单测（T1-T12）。

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试 setUpModule 自动补一份。
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

# #80 真实完成总结样本（固化快照，取 data/board.db 中 req 80 summary 的「完成总结：」之后文本）
SAMPLE80 = (
    "实现看板左侧菜单栏折叠功能（仅改 board.py，纯前端）：1) 新增折叠按钮 #navToggle（nav-head 内），"
    "CSS 新增 .workspace.nav-collapsed 折叠态规则——nav-col 收成 40px 窄条（transition .2s 平滑过渡、"
    "保留 16px gap），项目列表/标题隐藏，「+ 添加需求」收成 + 图标；list-col 保持 540px flex-shrink:0 不动，"
    "detail-col flex:1 自动加宽。2) JS 新增 navCollapsed 状态（localStorage 键 navCollapsed，'1'=折叠）+ "
    "toggleNav()/applyNavCollapsed()，初始化与点击均应用 class，刷新保持。验证：worktree 内 "
    "REQ_BOARD_PORT=8901 独立端口启动测试实例（未跑 ./start.sh），python3 -m py_compile OK，curl HTTP 200；"
    "Playwright 实测——默认展开 nav 172px/list 540px/detail 408px；点击折叠后 nav 40px、list 保持 540px "
    "左移 132px、detail 加宽至 540px、gap 16px；刷新后折叠状态保持；展开恢复正常；折叠态点击 + 图标仍能打开"
    "添加表单；控制台 0 error。自测报告已写 artifacts/req-board/req-80/self-test.md。已 git commit"
    "（f-20260805-80, da7d3c5），未 push。"
)

# #85 实证样本（固化快照，取 data/board.db req 85 summary 的「- 改动：① …」到「- 约束落点（…）…白名单。」5 行，
# 每行去掉行首 `- ` 后拼接为一行；147+198+280+41+77 = 743 字符，含 5 个断句点：①尾`；` ②尾`；` ③尾`；` ④尾`。` 行尾`。`）。
# 测试运行时不读 DB（worktree 无 data/board.db）；test_t13 内置前置自检（len==743 且单行调用拆 5 行）兜底样本失效。
GOLD85 = (
    "改动：① dispatcher.py 常量区新增 GIT_LOG_*（默认 count 15/上限 100/超时 30s/单行截断 200/空匹配提示/固定格式 %h %ad %an %s），READ"
    "ONLY_TOOLS 补 git_log，READONLY_TOOL_GUIDE 追加说明；② resolve_path_safe 后新增纯函数 git_log_repo_ok（.git 用 .exi"
    "sts() 兼容 worktree gitfile）/git_log_path_filter（相对 repo 根 resolve + '-' 前缀拒绝）/git_log_count_ok/git_lo"
    "g_env（剥离全部 GIT_* 变量、置 GIT_TERMINAL_PROMPT=0）；③ ReadonlyMcpServer 新增 tool_git_log：固定 argv 执行 git log（"
    "-c alias.log=log / -c color.ui=never / -c i18n.logOutputEncoding=utf-8 / --no-pager，不暴露 diff/-p/--ou"
    "tput/自定义 format），path 经 '--' 分隔传 git，未知参数 key/越权 repo/非法 path/count 超限一律 ValueError → MCP error（fail"
    "-closed），超时/非零退出一律 error；④ tools/list 与 tools/call 两处注册点补 git_log。约束落点（READONLY_CMDS/readonly_cmd_sa"
    "fe/_auto_approve/Bash 分支）逐字节未动，不扩 Bash 白名单。"
)


class L0SummaryFormat(unittest.TestCase):
    """format_summary_points 纯函数正反例（T1-T12）"""

    # ---- T1：空输入安全 ----
    def test_t1_empty_returns_empty(self):
        for v in (None, "", "   ", "\n  \n"):
            self.assertEqual(d.format_summary_points(v), "", repr(v))

    # ---- T2：多行文本原样透传 ----
    def test_t2_multiline_passthrough(self):
        text = "a\nb\nc"
        self.assertEqual(d.format_summary_points(text), text)

    # ---- T3：短单行不拆 ----
    def test_t3_short_line_unchanged(self):
        text = "进程退出码 0，未提交结果"
        self.assertLessEqual(len(text), d.SUMMARY_LONG_LINE_MIN)
        self.assertEqual(d.format_summary_points(text), text)

    # ---- T4：超长单行含 1) 2) 3) 编号 → 引导行 + 1./2./3. ----
    def test_t4_num_marks_split(self):
        head = "本需求改造调度器写入开发总结的格式：" * 8
        text = head + "1) 新增折叠按钮，" + "x" * 30 + "；2) 引入状态变量，" + "y" * 30 + "。3) 刷新保持。"
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        lines = out.split("\n")
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], head)
        self.assertTrue(lines[1].startswith("1. 新增折叠按钮，"))
        self.assertTrue(lines[2].startswith("2. 引入状态变量，"))
        self.assertTrue(lines[3].startswith("3. 刷新保持。"))

    # ---- T5：1. 2. 编号 → 统一 1. 风格 ----
    def test_t5_dot_marks_unified(self):
        text = "改造说明：" + "x" * 60 + " 1. 第一步，完成甲。" + "y" * 60 + " 2. 第二步，完成乙。"
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        lines = out.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "改造说明：" + "x" * 60)
        self.assertEqual(lines[1], "1. 第一步，完成甲。" + "y" * 60)
        self.assertEqual(lines[2], "2. 第二步，完成乙。")

    # ---- T6：无编号、中文断句 → `- ` 列表，标点保留行尾 ----
    def test_t6_cn_sentence_split(self):
        text = "实现左侧折叠功能" + "x" * 60 + "。新增按钮。" + "y" * 60 + "；保持布局。"
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        lines = out.split("\n")
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("- 实现左侧折叠功能") and lines[0].endswith("。"))
        self.assertEqual(lines[1], "- 新增按钮。")
        self.assertTrue(lines[2].startswith("- ") and lines[2].endswith("；"))
        self.assertEqual(lines[3], "- 保持布局。")

    # ---- T7：英文 `. ` 断句 → `- ` 列表 ----
    def test_t7_en_sentence_split(self):
        text = "alpha" + "x" * 60 + ". beta" + "y" * 60 + ". gamma"
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        lines = out.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("- alpha") and lines[0].endswith("."))
        self.assertTrue(lines[1].startswith("- beta") and lines[1].endswith("."))
        self.assertTrue(lines[2].startswith("- gamma"))

    # ---- T8：防误拆 token（路径/命令/样式值/端口）完整不切断 ----
    def test_t8_tokens_not_split(self):
        tokens = ["#navToggle", "./start.sh", "transition .2s", "README.md",
                  "REQ_BOARD_PORT=8901", "python3 -m py_compile"]
        text = ("检查 token 完整性：" + "x" * 30 + "；#navToggle 折叠按钮；" + "y" * 30 + "；./start.sh 不运行；"
                + "z" * 30 + "；transition .2s 平滑；" + "w" * 30 + "；README.md 文档；" + "v" * 30
                + "；REQ_BOARD_PORT=8901 端口；" + "u" * 30 + "；python3 -m py_compile 编译。")
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        joined = "\n".join(out.split("\n"))
        for tok in tokens:
            self.assertIn(tok, joined, tok)

    # ---- T9：行数上限：超限并入最后一段，不丢内容 ----
    def test_t9_max_points_no_loss(self):
        text = "。".join("段落%d" % i for i in range(1, 30)) + "。"
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        lines = out.split("\n")
        self.assertLessEqual(len(lines), d.SUMMARY_MAX_POINTS)
        self.assertEqual(len(lines), d.SUMMARY_MAX_POINTS)
        joined = "".join(lines)
        for i in range(1, 30):
            self.assertIn("段落%d" % i, joined, "第 %d 段丢失" % i)

    # ---- T10：阈值：超长但拆分点不足 2 个 → 不拆原样 ----
    def test_t10_single_split_point_unchanged(self):
        text1 = "仅一个编号" + "x" * 150 + " 1) 第一步"
        self.assertGreater(len(text1), d.SUMMARY_LONG_LINE_MIN)
        self.assertEqual(d.format_summary_points(text1), text1)
        text2 = "仅一个断句" + "x" * 150 + "。结束"
        self.assertGreater(len(text2), d.SUMMARY_LONG_LINE_MIN)
        self.assertEqual(d.format_summary_points(text2), text2)

    # ---- T11：真实样本 #80 黄金测试 ----
    def test_t11_sample80(self):
        self.assertGreater(len(SAMPLE80), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(SAMPLE80)
        lines = out.split("\n")
        self.assertGreater(len(lines), 1)
        self.assertTrue(any(l.startswith("1. ") for l in lines))
        # 引导行独立成行
        self.assertEqual(lines[0], "实现看板左侧菜单栏折叠功能（仅改 board.py，纯前端）：")
        joined = "\n".join(lines)
        for tok in ("#navToggle", "./start.sh", "REQ_BOARD_PORT=8901", "toggleNav()",
                    "applyNavCollapsed()", "Playwright 实测", "da7d3c5"):
            self.assertIn(tok, joined, tok)

    # ---- T12：append_summary 拼接组合（[auto] 前缀行独立，其后为列表行） ----
    def test_t12_append_summary_shape(self):
        old = "[auto] analysis 建议 auto（免确认），直接进入开发。"
        fmt = d.format_summary_points(SAMPLE80)
        combined = (old + "\n[auto] " + "完成总结：" + fmt).strip()
        lines = combined.split("\n")
        self.assertTrue(lines[0].startswith("[auto] analysis"))
        # [auto] 前缀行独立成行；引导行紧随「完成总结：」同行（tech.md §1.4 形态）
        self.assertTrue(lines[1].startswith("[auto] 完成总结：实现看板左侧菜单栏折叠功能（仅改 board.py，纯前端）："))
        self.assertTrue(lines[2].startswith("1. "))
        self.assertTrue(lines[3].startswith("2. "))
        # 延续行（列表行）不再带 [auto] 前缀
        self.assertFalse(any("[auto]" in l for l in lines[2:]))

    # ---- T13：#85 实证「改动：」段重构行黄金测试（多行包裹，逐字符一致） ----
    def test_t13_gold85_multiline(self):
        self.assertEqual(len(GOLD85), 743)  # 样本前置自检
        gold_lines = d.format_summary_points(GOLD85).split("\n")
        self.assertEqual(len(gold_lines), 5)  # 单行调用恰拆 5 行 `- `（样本失效即暴露）
        self.assertTrue(all(l.startswith("- ") for l in gold_lines))
        text = "前言段落\n\n" + GOLD85 + "\n\n收尾段落"
        out = d.format_summary_points(text)
        expected = ["前言段落", ""] + gold_lines + ["", "收尾段落"]
        self.assertEqual(out.split("\n"), expected)  # 段间结构与 GOLD85 段拆分逐字符一致

    # ---- T14：多行 + 段内长行（内联 ①②③ 断句）→ 拆 `- ` 列表，空行与其他段原样 ----
    def test_t14_multiline_long_inline_segment(self):
        short_a = "开头短段落"
        long_seg = "改动：① 实现甲功能" + "x" * 60 + "；② 引入乙能力" + "y" * 60 + "；③ 完成丙验证。"
        short_b = "结尾短段落"
        self.assertGreater(len(long_seg), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(short_a + "\n\n" + long_seg + "\n\n" + short_b)
        lines = out.split("\n")
        self.assertEqual(lines[0], short_a)
        self.assertEqual(lines[1], "")
        self.assertEqual(len(lines), 7)
        self.assertTrue(lines[2].startswith("- 改动：① 实现甲功能") and lines[2].endswith("；"))
        self.assertTrue(lines[3].startswith("- ② 引入乙能力") and lines[3].endswith("；"))
        self.assertTrue(lines[4].startswith("- ③ 完成丙验证") and lines[4].endswith("。"))
        self.assertEqual(lines[5], "")
        self.assertEqual(lines[6], short_b)

    # ---- T15：段首单个 `- ` 前缀拆分时剥离，不产生 `- - ` 双前缀 ----
    def test_t15_dash_prefix_no_double(self):
        inner = "改动：① 实现甲" + "x" * 60 + "；② 引入乙" + "y" * 60 + "；③ 验证丙。"
        self.assertGreater(len("- " + inner), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points("- " + inner)
        lines = out.split("\n")
        self.assertGreater(len(lines), 1)
        self.assertFalse(any(l.startswith("- - ") for l in lines))
        # 输出与「去前缀内容单行调用」逐字符一致（A5）
        self.assertEqual(out, d.format_summary_points(inner))

    # ---- T16：行数上限按段独立——两段各拆 12 行，合计 24 行 > 全局上限 ----
    def test_t16_line_cap_per_segment(self):
        def make(prefix):
            return "。".join(prefix + "段%d" % i + "x" * 15 for i in range(1, 13)) + "。"
        seg1, seg2 = make("甲"), make("乙")
        self.assertGreater(len(seg1), d.SUMMARY_LONG_LINE_MIN)
        self.assertGreater(len(seg2), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(seg1 + "\n" + seg2)
        lines = out.split("\n")
        self.assertEqual(len(lines), 24)  # 两段各自 12 行，互不挤占（总行数可 >20）
        self.assertLessEqual(len(lines[0:12]), d.SUMMARY_MAX_POINTS)
        self.assertLessEqual(len(lines[12:24]), d.SUMMARY_MAX_POINTS)
        joined = "\n".join(lines)
        for i in range(1, 13):
            self.assertIn("甲段%d" % i, joined)
            self.assertIn("乙段%d" % i, joined)

    # ---- T17：空行/段落边界逐字符保留 ----
    def test_t17_blank_lines_preserved(self):
        self.assertEqual(d.format_summary_points("a\n\nb"), "a\n\nb")
        short1 = "第一段"
        long_seg = "改动：① 甲" + "x" * 60 + "；② 乙" + "y" * 60 + "；③ 丙。"
        short2 = "第二段"
        out = d.format_summary_points(short1 + "\n\n" + long_seg + "\n\n" + short2)
        lines = out.split("\n")
        self.assertEqual(lines[0], short1)
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[-2], "")
        self.assertEqual(lines[-1], short2)

    # ---- T18：T2 兼容——全短段多行文本仍原样透传 ----
    def test_t18_multiline_short_compat(self):
        text = "a\nb\nc"
        self.assertEqual(d.format_summary_points(text), text)

    # ---- T19：①②③④ 无编号识别，由断句拆分兜底且不丢内容 ----
    def test_t19_circle_marks_fallback_no_loss(self):
        text = "改动：① 完成甲项工作" + "x" * 50 + "；② 完成乙项工作" + "y" * 50 + "；③ 完成丙项工作。"
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        lines = out.split("\n")
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(l.startswith("- ") for l in lines))
        # 不丢内容：输出去 `- ` 前缀拼接后的非空白序列 == 输入 strip 后非空白序列
        body = "".join(l[2:] for l in lines)
        self.assertEqual("".join(body.split()), "".join(text.strip().split()))

    # ---- T20：自指字面「1. 」边界——各短行原样返回、不重排 ----
    def test_t20_selfref_dot_marks_short_lines(self):
        text = "引导行\n1. 编号行\n2. 编号行"
        self.assertEqual(d.format_summary_points(text), text)

    # ---- T21：编号分支作用于段（多行样本中一段超长含 1) 2)） ----
    def test_t21_num_branch_per_segment(self):
        head = "改造说明：" + "x" * 60
        long_seg = head + " 1) 第一步甲。" + "y" * 60 + " 2) 第二步乙。"
        self.assertGreater(len(long_seg), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(long_seg + "\n\n收尾段")
        lines = out.split("\n")
        self.assertEqual(lines[0], head)  # 引导行独立
        self.assertEqual(lines[1], "1. 第一步甲。" + "y" * 60)
        self.assertEqual(lines[2], "2. 第二步乙。")
        self.assertEqual(lines[3], "")
        self.assertEqual(lines[4], "收尾段")

    # ---- T22：短段 strip 语义（段首尾空白 strip，与单行路径一致） ----
    def test_t22_segment_strip(self):
        self.assertEqual(d.format_summary_points("a\n b \n c"), "a\nb\nc")

    # ---- T23：rgb 色值内部的 `\d+\)` 不是编号，不得拆碎（#428 现场） ----
    def test_t23_rgb_values_not_num_marks(self):
        text = ("浏览器验证：简单 rgb(240,253,244)/(21,128,61)、标准 rgb(238,242,255)/(67,56,202)、"
                "复杂 rgb(255,241,242)/(190,18,60)、无快照 rgb(248,250,252)/(100,116,139)")
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        self.assertEqual(d.format_summary_points(text), text)   # 无编号可拆 → 原样返回

    # ---- T24：rgb 色值与真编号共存——编号照拆、色值完整（前缀收紧不误伤编号识别） ----
    def test_t24_rgb_with_real_num_marks(self):
        head = "验收证据：" * 12
        text = (head + "1) 简单 rgb(240,253,244)/(21,128,61)；"
                "2) 标准 rgb(238,242,255)/(67,56,202)、复杂 rgb(255,241,242)/(190,18,60)。")
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        lines = d.format_summary_points(text).split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], head)
        self.assertTrue(lines[1].startswith("1. 简单 rgb(240,253,244)/(21,128,61)"))
        self.assertTrue(lines[2].startswith("2. 标准 rgb(238,242,255)/(67,56,202)"))
        for tok in ("rgb(240,253,244)", "(21,128,61)", "rgb(238,242,255)",
                    "(67,56,202)", "rgb(255,241,242)", "(190,18,60)"):
            self.assertIn(tok, "\n".join(lines), tok)

    # ---- T25：值内数字不算编号（OK(skipped=17)、PORT=8901) 这类）----
    def test_t25_value_digits_not_num_marks(self):
        text = ("回归证据：定向 64 例 OK、全量 1450 例 OK(skipped=17)，"
                "端口检查 REQ_BOARD_PORT=8901) 通过，无异常。" + "x" * 60)
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        flat = "".join(out.split())
        self.assertIn("OK(skipped=17)", flat)
        self.assertIn("=8901)", flat)
        self.assertFalse(any(l.startswith("1. ") for l in out.split("\n")))  # 无真编号可拆

    # ---- T26：编号前缀白名单——`，1)`/`、1)` 不被识别（漏识别不拆，原文无损）----
    def test_t26_prefix_whitelist_no_false_positive(self):
        text = ("说明" + "x" * 80 + "，1) 甲项" + "y" * 40 + "、2) 乙项" + "z" * 40)
        self.assertGreater(len(text), d.SUMMARY_LONG_LINE_MIN)
        out = d.format_summary_points(text)
        flat = "".join(out.split())
        self.assertNotIn("1. 甲项", out)          # 未被误判为编号列表
        self.assertIn("，1)甲项", flat)           # 原文完整保留
        self.assertIn("、2)乙项", flat)


if __name__ == "__main__":
    unittest.main()
