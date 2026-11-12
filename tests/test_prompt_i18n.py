#!/usr/bin/env python3
"""英文提示词：语言选择、英文文案齐全、英文模式下发给 agent 的文本不漏中文、中文模式逐字不变。

E1 语言来源：项目 prompt_language → LOOP_PROMPT_LANG → zh；非法值回退
E2 英文文件齐全：注册表条目 / 角色模板 / 全局技能都有 prompts/en/ 版本，占位符与锁定句一致
E3 代码短句表：EN 每条与中文键的占位符集合一致
E4 英文模式编译全部 8 个角色（多仓库 + 项目清单 + 前置契约 + 评审反馈折叠 + 冲突解决）：
   去掉 <card-data> 围栏内的卡片数据后不含中文
E5 运行时文案（续跑、限步收尾、红线、重试、打回返工、上下文更新）英文模式不含中文
E6 默认（未配置语言）走中文：与改造前逐字一致由其它提示词测试守护，这里只验证选到中文

运行：.venv/bin/python -m pytest tests/test_prompt_i18n.py -q
"""
import json
import os
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

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402
import acp_shared as ac  # noqa: E402
import prompt_i18n  # noqa: E402
import prompt_store  # noqa: E402
from acp_modules import skills as skills_mod  # noqa: E402

ROLES = ("analysis", "proposal", "tech", "tech_review", "dev", "code_review", "accept", "merge_resolve")
CJK_RE = re.compile(r"[　-〿㐀-鿿＀-￯]")
CARD_DATA_RE = re.compile(r'<card-data source="[^"]*">.*?</card-data>', re.S)
PH_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def cjk_outside_card_data(text):
    stripped = CARD_DATA_RE.sub("<card-data/>", text)
    out = []
    for i, line in enumerate(stripped.splitlines(), 1):
        if CJK_RE.search(line):
            out.append(f"{i}: {line[:160]}")
    return out


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, timeout=60)


def make_repo(root, manifest=True):
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "master", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "AGENTS.md").write_text("# Agents\nRun make test.\n", encoding="utf-8")
    (root / "web").mkdir()
    (root / "web" / "AGENTS.md").write_text("# Web\n", encoding="utf-8")
    (root / "web" / "page.js").write_text("x\n", encoding="utf-8")
    if manifest:
        (root / ".loop" / "guides").mkdir(parents=True)
        (root / ".loop" / "guides" / "instance.md").write_text("Start the instance with bin/dev.\n",
                                                               encoding="utf-8")
        (root / ".loop" / "project.json").write_text(json.dumps({
            "version": 1,
            "commands": {"test": "make test", "lint": "make lint"},
            "instance": {"start": "bin/dev start {port}", "stop": "bin/dev stop {port}",
                         "port_range": [9100, 9199], "healthcheck": "http://127.0.0.1:{port}/health"},
            "guides": {"instance": {"file": ".loop/guides/instance.md", "roles": ["dev", "accept"]}},
        }), encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return root


class LangResolution(unittest.TestCase):
    """E1"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prompt-i18n-"))
        self.pf = self.tmp / "projects.json"
        self.pf.write_text(json.dumps({"en_proj": {"prompt_language": "en"}, "zh_proj": {"prompt_language": "zh-CN"},
                                       "bad": {"prompt_language": "fr"}, "plain": {"paths": []}}))
        self.env = mock.patch.dict(os.environ, {"REQ_BOARD_PROJECTS": str(self.pf)})
        self.env.start()
        os.environ.pop("LOOP_PROMPT_LANG", None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_zh(self):
        self.assertEqual(prompt_i18n.prompt_lang("plain"), "zh")
        self.assertEqual(prompt_i18n.prompt_lang(None), "zh")
        self.assertEqual(prompt_i18n.prompt_lang("unknown"), "zh")

    def test_project_setting_wins(self):
        with mock.patch.dict(os.environ, {"LOOP_PROMPT_LANG": "zh"}):
            self.assertEqual(prompt_i18n.prompt_lang("en_proj"), "en")
        with mock.patch.dict(os.environ, {"LOOP_PROMPT_LANG": "en"}):
            self.assertEqual(prompt_i18n.prompt_lang("zh_proj"), "zh")
            self.assertEqual(prompt_i18n.prompt_lang("plain"), "en")

    def test_invalid_values_fall_back(self):
        with mock.patch.dict(os.environ, {"LOOP_PROMPT_LANG": "en"}):
            self.assertEqual(prompt_i18n.prompt_lang("bad"), "en")      # 项目值非法 → 全局
        with mock.patch.dict(os.environ, {"LOOP_PROMPT_LANG": "klingon"}):
            self.assertEqual(prompt_i18n.prompt_lang("plain"), "zh")


class EnglishFilesComplete(unittest.TestCase):
    """E2"""

    def test_registry_items_have_english_versions(self):
        reg = json.loads((REPO / "prompts" / "registry.json").read_text(encoding="utf-8"))
        for it in reg["items"]:
            zh = (REPO / "prompts" / it["file"]).read_text(encoding="utf-8")
            en_path = REPO / "prompts" / "en" / it["file"]
            self.assertTrue(en_path.is_file(), f"缺英文版 {en_path}")
            en = en_path.read_text(encoding="utf-8")
            self.assertFalse(CJK_RE.search(en), f"{it['id']} 英文版含中文")
            declared = set(it.get("placeholders") or ())
            self.assertEqual(sorted(n for n in PH_RE.findall(zh) if n in declared),
                             sorted(n for n in PH_RE.findall(en) if n in declared), it["id"])
            self.assertEqual(len(it.get("locked") or ()), len(it.get("locked_en") or ()), it["id"])
            for s in it.get("locked_en") or ():
                self.assertIn(s, en, f"{it['id']} 英文版缺锁定句")
            self.assertEqual(prompt_store.validate(it["id"], en, "en"), [], it["id"])

    def test_role_templates_have_english_versions(self):
        for role in ROLES:
            zh = ac.role_prompt(role)
            en = ac.role_prompt(role, "en")
            self.assertNotEqual(zh, en, f"{role} 英文模板缺失（退回了中文）")
            self.assertFalse(CJK_RE.search(en), f"{role} 英文模板含中文")
            self.assertEqual(sorted(PH_RE.findall(zh)), sorted(PH_RE.findall(en)), role)
            self.assertEqual(en.count("{card_inputs}"), 1, role)
            self.assertEqual(prompt_store.validate(f"role/{role}", en, "en"), [], role)

    def test_global_skills_have_english_versions(self):
        for path in sorted((REPO / "prompts" / "skills").glob("*/SKILL.md")):
            sk = skills_mod.load_skill(path, scope="global")
            en = skills_mod.localized(sk, "en")
            self.assertIsNot(en, sk, f"技能 {sk.name} 缺英文版")
            self.assertEqual(en.errors, [], sk.name)
            self.assertFalse(CJK_RE.search(en.text + en.description), f"技能 {sk.name} 英文版含中文")


class CodeStringTable(unittest.TestCase):
    """E3"""

    def test_placeholders_match(self):
        for zh, en in prompt_i18n.EN.items():
            self.assertEqual(sorted(PH_RE.findall(zh)), sorted(PH_RE.findall(en)), zh)
            self.assertFalse(CJK_RE.search(en), f"译文含中文：{en}")

    def test_missing_translation_falls_back_to_chinese(self):
        with self.assertLogs("prompt_i18n", "WARNING"):
            self.assertEqual(prompt_i18n.tr("en", "没有这条{x}", x=1), "没有这条1")
        self.assertEqual(prompt_i18n.tr("zh", "工作目录：{wd}（独立 worktree，分支 {branch}）", wd="/w", branch="b"),
                         "工作目录：/w（独立 worktree，分支 b）")


class EnglishCompileBase(unittest.TestCase):

    lang = "en"

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="prompt-i18n-compile-"))
        self.r1 = make_repo(self.base / "repo-one")
        self.r2 = make_repo(self.base / "repo-two", manifest=False)
        cfg = {"paths": [str(self.r1), str(self.r2)], "confirm": False, "company": True,
               "repo_map": str(self.base / "map.md"), "restart": "make restart",
               "auto_restart_after_merge": False, "base_branch": None, "goal": "Ship a calculator",
               "prompt_language": self.lang}
        self.projects = {"proj": cfg}
        pf = self.base / "projects.json"
        pf.write_text(json.dumps(self.projects), encoding="utf-8")
        self._patches = [
            mock.patch.dict(os.environ, {"REQ_BOARD_PROJECTS": str(pf),
                                         "REQ_BOARD_PROMPTS_LOCAL": str(self.base / "prompts.local")}),
            mock.patch.object(d, "PROJECTS", self.projects),
            mock.patch.object(d, "WORKTREES_ROOT", self.base / "wt"),
            mock.patch.object(d, "ARTIFACTS_ROOT", self.base / "art") if hasattr(d, "ARTIFACTS_ROOT")
            else mock.patch.object(d, "save_state", lambda: None),
            mock.patch.object(d, "save_state", lambda: None),
        ]
        for p in self._patches:
            p.start()
        self._state = d.state
        d.state = {"merge_resolve": {}, "worktrees": {}, "sessions": {}, "req_repos": {}}

    def tearDown(self):
        d.state = self._state
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def req(self, rid, *, flow=None, node_index=None, summary=None, notes="改 web/page.js 的按钮颜色"):
        r = {"id": rid, "project": "proj", "title": "按钮改成蓝色", "notes": notes,
             "mode": "auto", "created_at": "2026-11-12 10:00:00"}
        if flow:
            f = d.flow_by_id(flow)
            r.update(flow=flow, flow_name=f["name"], nodes_json=json.dumps(f["nodes"], ensure_ascii=False),
                     node_index=node_index)
        if summary:
            r["summary"] = summary
        return r


LONG_SUMMARY = "\n".join(f"[auto] 第 {i} 轮代码审查未通过：按钮颜色不对" for i in range(1, 10))


class EnglishCompileNoChinese(EnglishCompileBase):
    """E4"""

    def assertNoChinese(self, text, label):
        leaks = cjk_outside_card_data(text)
        self.assertEqual(leaks, [], f"{label} 英文提示词漏中文：\n" + "\n".join(leaks[:20]))

    def test_pre_stage_analysis_with_contract(self):
        req = self.req(1)
        req["mode"] = None                                    # 未入流程（前置环节）
        self.assertTrue(d.in_pre_stage(req))
        compiled = d.compile_context(req, "analysis", dry=True)
        self.assertIn("suggested_flow", compiled.prompt)
        self.assertIn("Standard", compiled.prompt)            # 流程目录用英文展示名
        self.assertNoChinese(compiled.prompt, "analysis(pre)")

    def test_all_roles_in_heavy_flow(self):
        d.state["merge_resolve"]["7"] = {"failed": [str(self.r1)]}
        nodes = d.flow_by_id("heavy")["nodes"]
        for role in ROLES:
            idx = next((i for i, n in enumerate(nodes) if n.get("role") == role), 2)
            req = self.req(7, flow="heavy", node_index=idx, summary=LONG_SUMMARY)
            compiled = d.compile_context(req, role, extra="Retry after timeout", dry=True)
            self.assertNoChinese(compiled.prompt, role)

    def test_skills_and_catalog_render_in_english(self):
        req = self.req(8, flow="standard", node_index=0, notes="fix the page style in the browser")
        parts = d.build_prompt_parts(req, "dev", dry=True)
        text = "".join(parts)
        self.assertIn("[Scenario skill: browser-verify]", text)
        self.assertNoChinese(text, "dev skills")

    def test_manifest_records_english_texts(self):
        compiled = d.compile_context(self.req(9, flow="standard", node_index=0), "dev", dry=True)
        common = next(i for i in compiled.items if i["id"] == "system/common")
        self.assertEqual(common["hash"], prompt_store.short_hash(prompt_store.read("system/common", "en")))


class EnglishRuntimeTexts(EnglishCompileBase):
    """E5"""

    def assertNoChinese(self, text, label):
        leaks = cjk_outside_card_data(text)
        self.assertEqual(leaks, [], f"{label} 漏中文：\n" + "\n".join(leaks[:20]))

    def test_resume_and_wrapup_and_redline(self):
        req = self.req(11, flow="standard", node_index=0)
        text, _ = d.resume_text(req, "dev", "")
        self.assertNoChinese(text, "resume-auto")
        text, _ = d.resume_text(req, "dev", "", user_text="please continue")
        self.assertNoChinese(text, "resume-user")
        self.assertNoChinese(ac.append_kill_redline("x", 123, lang="en"), "kill-redline")
        self.assertNoChinese(prompt_store.render("system/wrapup", lang="en", detail="d", wrapup_steps=5), "wrapup")

    def test_retry_hint_and_rework_and_bounce(self):
        req = self.req(12, flow="standard", node_index=1)
        d.state.setdefault("retries", {})["12"] = {"count": 1, "last_failure": "timeout"}
        self.assertNoChinese(d.resume_failure_hint(12, project="proj"), "retry-hint")
        node = d.flow_by_id("standard")["nodes"][1]
        issues = [{"id": "I1", "severity": "P1", "where": "web/page.js", "what": "wrong colour"}]
        from dispatcher_modules.flow_advance import _review_bounce_extra, _review_rework_extra
        self.assertNoChinese(_review_rework_extra(req, node, issues, round_no=2), "rework")
        self.assertIn("Rework input: open issues", _review_rework_extra(req, node, issues))
        self.assertNoChinese(_review_bounce_extra(req, node, "wrong colour", round_no=2), "bounce")

    def test_compact_summary_digest(self):
        out = d.compact_summary(LONG_SUMMARY, lang="en")
        self.assertIn("earlier entries folded", out)


class BoardChatPromptsEnglish(unittest.TestCase):
    """看板「聊天建卡」与卡片对话的英文系统提示词：不含中文，关键工具与约束齐全。"""

    def test_create_card_prompt(self):
        from board_modules.services.chat_prompts_en import create_card_prompt
        for mysql in (True, False):
            text = create_card_prompt("proj", "/w", "Ship it", mysql)
            self.assertFalse(CJK_RE.search(text))
            for must in ("```draft", "create_requirement", "later", "AskUserQuestion", "Project goal: Ship it"):
                self.assertIn(must, text)
        self.assertIn("mysql_query", create_card_prompt("proj", "/w", "", True))
        self.assertNotIn("mysql_query", create_card_prompt("proj", "/w", "", False))

    def test_req_chat_prompt(self):
        from board_modules.services.chat_prompts_en import req_chat_prompt
        req = {"title": "T", "status": "later", "mode": "auto", "notes": "", "summary": ""}
        text = req_chat_prompt(req_id=5, req=req, project="proj", workdir="/w", req_dir="/a", model=None,
                               tmp_dir="/tmp/x/")
        self.assertFalse(CJK_RE.search(text))
        for must in ("#5", "Deferred", "update_requirement", "post_review", "write_artifact", "/tmp/x/"):
            self.assertIn(must, text)


class DefaultStaysChinese(EnglishCompileBase):
    """E6：未配置语言 → 中文模板（逐字一致由 test_prompt_prefix_cache 等守护）"""

    lang = ""

    def test_default_compiles_chinese(self):
        compiled = d.compile_context(self.req(21, flow="standard", node_index=0), "dev", dry=True)
        self.assertIn(prompt_store.read("system/common"), compiled.prompt)
        self.assertIn("工作目录", compiled.prompt)


if __name__ == "__main__":
    unittest.main()
