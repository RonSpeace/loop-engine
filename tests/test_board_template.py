#!/usr/bin/env python3
"""#32 Phase 2：首页内嵌 HTML 迁到 templates/board.html 的等价性锁（T1-T5）。

- T1：兼容导出等价——board.HTML 与模板文件同字节（同源自证，非等价性证明本身）。
- T2：模板声明变量恰为 8 个（原 4 个 + #33 的 board_css_href/board_js_href + 中英切换的 i18n_js_href/i18n_dict_href），且无 Jinja 语句/注释块残留。
- T3：渲染等价——render_template 与 render_template_string 逐字节相同，且 test_client 首页一致。
- T4：模板路径不依赖 cwd（基于 app.root_path 绝对路径）。
- T5：模板里不存在连续两个反斜杠（无「粘贴源码文本」导致的转义残留）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from flask import render_template_string

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board  # noqa: E402
from board_modules.blueprints.meta import _asset_href  # noqa: E402
from tests._frontend_src import frontend_text  # noqa: E402

JINJA_VARS = {"sse_enabled", "agent_stall_sec", "agent_stall_first_sec", "thought_frames_absent",
              # #33 CSS/JS 外置：外链地址（带内容指纹），由 index() 注入
              "board_css_href", "board_js_href",
              # 界面中英切换：词典与 DOM 翻译层（static/i18n.en.js / static/i18n.js）
              "i18n_js_href", "i18n_dict_href"}


def _context(sse_enabled):
    # 与 index() 同源取地址（须在 app_context 内调用，两处用例均已满足）
    return {
        "sse_enabled": sse_enabled,
        "agent_stall_sec": board.AGENT_STALL_SEC,
        "agent_stall_first_sec": board.AGENT_STALL_FIRST_FRAME_SEC,
        "thought_frames_absent": board.THOUGHT_FRAMES_ABSENT,
        "board_css_href": _asset_href("board.css"),
        "board_js_href": _asset_href("board.js"),
        "i18n_js_href": _asset_href("i18n.js"),
        "i18n_dict_href": _asset_href("i18n.en.js"),
    }


class BoardTemplateTest(unittest.TestCase):
    def test_t1_html_compat_export_matches_template(self):
        self.assertEqual(board.HTML, frontend_text())

    def test_t2_jinja_variables_are_exactly_the_six(self):
        import jinja2.meta
        tree = board.app.jinja_env.parse(frontend_text())
        self.assertEqual(jinja2.meta.find_undeclared_variables(tree), JINJA_VARS)
        self.assertNotIn("{%", frontend_text())
        self.assertNotIn("{#", frontend_text())

    def test_t3_render_equivalence(self):
        with board.app.app_context():
            for enabled in (True, False):
                ctx = _context(enabled)
                self.assertEqual(board.render_template('board.html', **ctx),
                                 render_template_string(board.HTML, **ctx))

    def test_t3_test_client_body_equals_template_string(self):
        if board.FREEZE_TS is not None:
            self.skipTest("冻结时钟开启，注入脚本属另一路径，由实例字节比对覆盖")
        body = board.app.test_client().get('/').get_data(as_text=True)
        with board.app.app_context():
            expected = render_template_string(board.HTML, **_context(board._SSE_ENABLED))
        self.assertEqual(body, expected)

    def test_t4_cwd_independent(self):
        self.assertTrue(Path(board.app.root_path).is_absolute())
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                resp = board.app.test_client().get('/')
                self.assertEqual(resp.status_code, 200)
            finally:
                os.chdir(old)

    def test_t5_no_double_backslash(self):
        self.assertEqual(frontend_text().count("\\\\"), 0)


if __name__ == "__main__":
    unittest.main()
