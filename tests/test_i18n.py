"""界面中英切换（static/i18n.js DOM 翻译层 + scripts/i18n_en_source.py 词典单一源）。

- I1 生成物与源同步：static/i18n.en.js 必须等于 i18n_en_source.py 重新生成的内容（改词典忘了重新生成会挂）。
- I2 模板全覆盖：templates/board.html 里的中文界面文案都有英文。
- I3 运行时翻译：组合句式、最长优先、单字不做子串替换、中文模式原样返回。
- I4 跳过区：用户内容容器（卡片标题、需求详情、agent 输出等）写在 SKIP 选择器里。
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import i18n_en_source  # noqa: E402
import i18n_extract  # noqa: E402

NODE = shutil.which("node")

HARNESS = r"""
global.window = {};
// node -e：process.argv = [node, lang, repo, cases]
global.navigator = {language: process.argv[1]};
global.localStorage = {getItem: () => process.argv[1], setItem() {}};
global.document = {readyState: 'loading', addEventListener() {}};
require(process.argv[2] + '/static/i18n.en.js');
require(process.argv[2] + '/static/i18n.js');
const cases = JSON.parse(process.argv[3]);
console.log(JSON.stringify(cases.map(s => window.loopT(s))));
"""


def _translate(lang, cases):
    out = subprocess.run([NODE, "-e", HARNESS, lang, str(REPO), json.dumps(cases, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


class I18nDictionaryTest(unittest.TestCase):
    def test_I1_generated_file_in_sync(self):
        js = (REPO / "static" / "i18n.en.js").read_text(encoding="utf-8")
        body = js.split("window.LOOP_I18N_EN = ", 1)[1].rstrip().rstrip(";")
        self.assertEqual(json.loads(body)["phrases"], i18n_en_source.PHRASES,
                         "static/i18n.en.js 与词典源不同步：运行 .venv/bin/python scripts/i18n_en_source.py")
        self.assertEqual(json.loads(body)["labels"], i18n_en_source.LABELS)

    def test_I2_template_fully_covered(self):
        missing = [k for k, src in i18n_extract.candidates().items()
                   if src == "templates/board.html" and k not in i18n_en_source.PHRASES]
        self.assertEqual(missing, [], "模板中文文案缺英文：%s" % missing)

    def test_I2b_no_empty_translation(self):
        empty = [k for k, v in i18n_en_source.PHRASES.items() if not str(v).strip()]
        self.assertEqual(empty, [])


@unittest.skipUnless(NODE, "需要 node")
class I18nRuntimeTest(unittest.TestCase):
    def test_I3_composed_strings(self):
        got = _translate("en", ["第 3 轮 · 结论：", "创建于 01-13 05:15 · 编号：45 ·", "近 7 天",
                                "需求开发（开发） → 收尾（收尾）", "会话已中断，可继续会话（保留上下文）"])
        self.assertEqual(got, ["Round 3 · Verdict: ", "Created 01-13 05:15 · ID: 45 ·", "Last 7 days",
                               "Development (dev) → Finalize (finalize)",
                               "Session interrupted; you can continue (context kept)"])

    def test_I3b_single_char_only_exact(self):
        # 单字「项」只在整节点精确命中时翻译，不在「项目」之类的其它词里做子串替换
        got = _translate("en", ["项", "某项未知文案"])
        self.assertEqual(got[0], "Item")
        self.assertNotIn("Item", got[1])

    def test_I3c_chinese_mode_passthrough(self):
        self.assertEqual(_translate("zh", ["第 3 轮 · 结论："]), ["第 3 轮 · 结论："])


class I18nSkipTest(unittest.TestCase):
    def test_I4_user_content_containers_skipped(self):
        src = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
        skip = src.split("var SKIP = [", 1)[1].split("]", 1)[0]
        for sel in (".card-title", ".detail-title", ".detail-notes:not(.empty)", ".chat-msg.user",
                    ".chat-msg.agent", ".ev-item.ev-msg", ".ev-item.ev-user", ".ev-item.ev-tool",
                    ".artifact-body", ".diff-view", "pre", "textarea"):
            self.assertIn("'%s'" % sel, skip, "用户内容容器未跳过：%s" % sel)

    def test_I4b_template_wires_scripts_before_board_js(self):
        html = (REPO / "templates" / "board.html").read_text(encoding="utf-8")
        a, b, c = (html.index("{{ i18n_dict_href }}"), html.index("{{ i18n_js_href }}"),
                   html.index("{{ board_js_href }}"))
        self.assertLess(a, b)
        self.assertLess(b, c)
        self.assertIn('id="langToggle"', html)


if __name__ == "__main__":
    unittest.main()
