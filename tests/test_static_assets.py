#!/usr/bin/env python3
"""#33 前端 CSS/JS 外置到 static/ 的契约锁（U01-U10，Flask test_client，不起真服务）。

- U01：Flask 默认静态路由可服务两文件，Content-Type 正确（零新增路由）。
- U02：两文件无 Jinja 占位；模板仍无 Jinja 语句/注释块。
- U03：首页外链恰好一条 CSS + 一条 JS，均带内容指纹 ?v=<sha256[:12]>；JS 外链无 defer/async/type。
- U04：执行顺序：skinBoot → CSS 外链 → 配置脚本 → </head>；JS 外链在 </body> 前且晚于配置脚本。
- U05：配置脚本按服务端常量渲染（布尔裸 true/false、整数裸数字）。
- U06：指纹随内容变化（临时目录副本，不写仓库 static/）。
- U07：static/board.js 四处赋值只读 window.__REQ_BOARD_CFG__、无模板语法；node --check 通过。
- U08：board.HTML 仍是模板全文（未拼接静态文件内容）。
- U09：冻结时钟开启时头前注入不变，仍先于 skinBoot 与两条外链。
- U10：负向断言取源抽查——frontend_src() 覆盖 JS/CSS，模板单源不覆盖（防取源退化恒真）。

运行（worktree 内用 main 工作区的 .venv）：
    ".venv/bin/python" -m pytest tests/test_static_assets.py -q
"""
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import board  # noqa: E402
from board_modules.blueprints.meta import _asset_href  # noqa: E402
from tests._frontend_src import frontend_css, frontend_js, frontend_src, frontend_text  # noqa: E402

STATIC = REPO / "static"


def _sha12(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


class StaticAssetsTest(unittest.TestCase):
    def test_u01_static_files_served(self):
        c = board.app.test_client()
        r = c.get("/static/board.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/css", r.headers.get("Content-Type", ""))
        r = c.get("/static/board.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r.headers.get("Content-Type", ""))

    def test_u02_no_jinja_tokens(self):
        for name in ("board.css", "board.js"):
            text = (STATIC / name).read_text(encoding="utf-8")
            self.assertNotIn("{{", text, name)
            self.assertNotIn("{%", text, name)
        self.assertNotIn("{%", frontend_text())
        self.assertNotIn("{#", frontend_text())

    def test_u03_external_refs_with_fingerprint(self):
        body = board.app.test_client().get("/").get_data(as_text=True)
        self.assertEqual(body.count('<link rel="stylesheet" href="/static/board.css?v='), 1)
        self.assertEqual(body.count('<script src="/static/board.js?v='), 1)
        m = re.search(r'<link rel="stylesheet" href="/static/board\.css\?v=([0-9a-f]{12})">', body)
        self.assertIsNotNone(m, "CSS 外链缺 12 位内容指纹")
        self.assertEqual(m.group(1), _sha12(STATIC / "board.css"))
        m = re.search(r'<script src="/static/board\.js\?v=([0-9a-f]{12})"></script>', body)
        self.assertIsNotNone(m, "JS 外链缺 12 位内容指纹")
        self.assertEqual(m.group(1), _sha12(STATIC / "board.js"))
        tag = re.search(r"<script[^>]*src=[^>]*>", body).group(0)
        for bad in ("defer", "async", "type="):
            self.assertNotIn(bad, tag, "外链脚本禁止 %s（会改变与内联脚本一致的执行序）" % bad)

    def test_u04_execution_order(self):
        body = board.app.test_client().get("/").get_data(as_text=True)
        i_skin = body.index('<script id="skinBoot">')
        i_css = body.index('<link rel="stylesheet" href="/static/board.css?v=')
        i_cfg = body.index("<script>window.__REQ_BOARD_CFG__=")
        i_head_end = body.index("</head>")
        i_js = body.index('<script src="/static/board.js?v=')
        i_body_end = body.index("</body>")
        self.assertLess(i_skin, i_css, "防闪脚本须先于样式（外链）")
        self.assertLess(i_css, i_cfg, "配置脚本在 CSS 外链之后")
        self.assertLess(i_cfg, i_head_end, "配置脚本须在 <head> 内（先于主脚本执行）")
        self.assertLess(i_cfg, i_js, "配置对象须先于 main 脚本")
        self.assertLess(i_js, i_body_end, "JS 外链在 </body> 之前")

    def test_u05_config_script_matches_server_constants(self):
        body = board.app.test_client().get("/").get_data(as_text=True)
        m = re.search(r"<script>window\.__REQ_BOARD_CFG__=(\{.*?\});</script>", body)
        self.assertIsNotNone(m, "首页缺配置脚本")
        cfg = m.group(1)
        self.assertIn("sseEnabled:%s" % ("true" if board._SSE_ENABLED else "false"), cfg)
        self.assertIn("stallSec:%d" % board.AGENT_STALL_SEC, cfg)
        self.assertIn("stallFirstSec:%d" % board.AGENT_STALL_FIRST_FRAME_SEC, cfg)
        self.assertIn("thoughtFramesAbsent:%s" % ("true" if board.THOUGHT_FRAMES_ABSENT else "false"), cfg)
        self.assertNotIn('"', cfg, "布尔/数字须裸值渲染（引号会改变类型语义）")

    def test_u06_fingerprint_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("board.css", "board.js"):
                shutil.copyfile(STATIC / name, Path(tmp) / name)
            v1 = _asset_href("board.css", root=tmp)
            j1 = _asset_href("board.js", root=tmp)
            self.assertEqual(v1, "/static/board.css?v=" + _sha12(Path(tmp) / "board.css"))
            self.assertEqual(j1, "/static/board.js?v=" + _sha12(Path(tmp) / "board.js"))
            p = Path(tmp) / "board.js"
            p.write_text(p.read_text(encoding="utf-8") + "// fingerprint probe\n", encoding="utf-8")
            j2 = _asset_href("board.js", root=tmp)
            self.assertNotEqual(j1, j2, "内容变化后指纹须变")
            self.assertEqual(j2, "/static/board.js?v=" + _sha12(p))
            # 同内容重复取 → 命中缓存、结果稳定（不误变）
            self.assertEqual(j2, _asset_href("board.js", root=tmp))

    def test_u07_board_js_reads_config_only(self):
        js = frontend_js()
        for left, field in (("SSE_SERVER_ENABLED", "sseEnabled"),
                            ("ACTIVITY_STALL_SEC", "stallSec"),
                            ("ACTIVITY_STALL_FIRST_SEC", "stallFirstSec"),
                            ("THOUGHT_FRAMES_ABSENT", "thoughtFramesAbsent")):
            needle = "const %s = window.__REQ_BOARD_CFG__.%s;" % (left, field)
            self.assertEqual(js.count(needle), 1, "static/board.js 缺唯一赋值：%s" % needle)
        self.assertNotIn("{{", js)
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用，跳过 static/board.js 语法检查")
        proc = subprocess.run([node, "--check", str(STATIC / "board.js")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_u08_board_html_is_template_only(self):
        self.assertEqual(board.HTML, frontend_text())
        self.assertNotIn("function renderList", board.HTML, "board.HTML 不得拼接 JS 文件内容")
        self.assertNotIn(":root {", board.HTML, "board.HTML 不得拼接 CSS 文件内容")

    def test_u09_freeze_script_still_first(self):
        with mock.patch.object(board, "FREEZE_TS", 1234567890.0):
            body = board.app.test_client().get("/").get_data(as_text=True)
        i_freeze = body.index("<script>(function(){var T=1234567890000,")
        i_skin = body.index('<script id="skinBoot">')
        i_css = body.index('<link rel="stylesheet" href="/static/board.css?v=')
        i_js = body.index('<script src="/static/board.js?v=')
        self.assertLess(i_freeze, i_skin, "冻结脚本须先于 skinBoot")
        self.assertLess(i_freeze, i_css)
        self.assertLess(i_freeze, i_js)
        self.assertEqual(body.count("__REQ_BOARD_FROZEN_MS__"), 1)

    def test_u10_source_wiring_sanity(self):
        src = frontend_src()
        html = frontend_text()
        self.assertIn("function renderList", src)
        self.assertIn(":root {", src)
        self.assertNotIn("function renderList", html)
        self.assertNotIn(":root {", html)
        self.assertIn("function renderList", frontend_js())
        self.assertIn(":root {", frontend_css())


if __name__ == "__main__":
    unittest.main()
