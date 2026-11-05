#!/usr/bin/env python3
"""技能 v2（acp_modules/skills.py + /api/skills）：

- frontmatter 极简 YAML 子集解析的边界；
- 作用域优先级：项目 > 本机覆盖 > 全局，目录形态优先于旧平铺，草稿回退；
- 激活方式 always / auto / on_demand / manual，卡片 notes `skills:` / `-skills:`，必需缺失；
- lint；Claude Code 落盘；
- 兼容：本仓库现有配置下 select_skills 的 always 段与 scenario_skills_text 逐字一致、正文与搬迁前逐字节一致；
- 看板 API：列表 / 详情 / 写本机覆盖 / 新建草稿 / 删除，项目技能只读，写操作仅限回环来源。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402
from acp_modules import skills as sk  # noqa: E402

# 搬迁前 prompts/skills/browser-verify.md 去首尾空白后的 sha256（逐字节不变的验收锚点）
BROWSER_VERIFY_OLD_SHA = "bc3ac535460baabd0d5e7e5a9bd4b2dc45b75f2d9b2dd8ebb641149a3714275e"
RENDER_ASSERT_OLD_SHA = "2a46730216acfb93f0f8e7895175c6062d2592813577983c6697b467752dff21"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def skill_md(name, body="正文", **meta):
    meta = {"name": name, "description": f"{name} 的描述", **meta}
    return sk.compose_skill_file(meta, body)


class TempDirs(unittest.TestCase):
    """每个用例独立的 全局 / 本机覆盖 / 项目仓库 目录。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="skills-v2-"))
        self.g = self.root / "global"
        self.l = self.root / "local" / "skills"
        self.repo = self.root / "repo"
        for p in (self.g, self.l, self.repo / ".loop" / "skills"):
            p.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def sel(self, role="dev", req=None, *, role_skills=(), manifest_skills=(), **kw):
        return sk.select_skills("toy", role, req or {}, [str(self.repo)], role_skills=list(role_skills),
                                manifest_skills=list(manifest_skills), global_dir=self.g, local_dir=self.l, **kw)

    @staticmethod
    def names(items):
        return [(x[0] if isinstance(x, tuple) else x).name for x in items]


class FrontmatterTest(unittest.TestCase):

    def test_scalars_lists_bools_comments(self):
        text = ("---\nname: a\ndescription: \"含: 冒号\"  # 注释\nroles: [dev, 'accept']\n"
                "paths:\n  - static/**\n  - \"templates/**\"\nrequired: yes\nowner: x # tail\n"
                "# 整行注释\n\nkeywords: []\n---\n\n正文\n")
        meta, body, errors = sk.parse_frontmatter(text)
        self.assertEqual(errors, [])
        self.assertEqual(meta["description"], "含: 冒号")
        self.assertEqual(meta["roles"], ["dev", "accept"])
        self.assertEqual(meta["paths"], ["static/**", "templates/**"])
        self.assertIs(meta["required"], True)
        self.assertEqual(meta["owner"], "x")
        self.assertEqual(meta["keywords"], [])
        self.assertEqual(body.strip(), "正文")

    def test_no_frontmatter_is_whole_body(self):
        meta, body, errors = sk.parse_frontmatter("只有正文\n---\n")
        self.assertEqual((meta, body, errors), ({}, "只有正文\n---\n", []))

    def test_unclosed_and_unsupported(self):
        _, _, errs = sk.parse_frontmatter("---\nname: a\n正文")
        self.assertTrue(any("未闭合" in e for e in errs))
        _, _, errs = sk.parse_frontmatter("---\nname: a\nmeta:\n  k: v\nx: |\n- orphan\nroles: [a\n---\n")
        joined = "\n".join(errs)
        for token in ("缩进", "不支持的 YAML", "[ 列表未闭合"):
            self.assertIn(token, joined)
        _, _, errs = sk.parse_frontmatter("---\n- orphan\nname: a\nname: b\n---\n")
        self.assertTrue(any("列表项前没有对应的键" in e for e in errs))
        self.assertTrue(any("重复的键" in e for e in errs))

    def test_quoted_commas_bom_and_crlf(self):
        meta, body, errors = sk.parse_frontmatter("﻿---\r\nkeywords: [\"a, b\", c]\r\n---\r\nB")
        self.assertEqual(errors, [])
        self.assertEqual(meta["keywords"], ["a, b", "c"])
        self.assertEqual(body, "B")

    def test_render_roundtrip(self):
        meta = {"name": "x", "description": "说明: 带冒号", "roles": ["dev"], "required": False,
                "activation": "auto", "paths": ["static/**"], "keywords": ["页面, 样式"]}
        meta2, body, errors = sk.parse_frontmatter(sk.compose_skill_file(meta, "正文"))
        self.assertEqual(errors, [])
        self.assertEqual(meta2, meta)
        self.assertEqual(body.strip(), "正文")


class LoadAndScopeTest(TempDirs):

    def test_field_validation_errors(self):
        p = write(self.g / "bad" / "SKILL.md", skill_md("bad", activation="sometimes", status="x", required="maybe"))
        s = sk.load_skill(p, scope="global")
        joined = "\n".join(s.errors)
        for token in ("activation", "status", "required"):
            self.assertIn(token, joined)
        self.assertTrue(s.required)            # 非法 required 宽松认定为必需（宁可停线）
        p2 = write(self.g / "nm" / "SKILL.md", skill_md("other"))
        self.assertTrue(any("不一致" in e for e in sk.load_skill(p2, scope="global").errors))
        p3 = write(self.g / "nofm" / "SKILL.md", "没有 frontmatter")
        self.assertTrue(any("缺少 frontmatter" in e for e in sk.load_skill(p3, scope="global").errors))

    def test_precedence_project_local_global(self):
        write(self.g / "s" / "SKILL.md", skill_md("s", "G"))
        write(self.l / "s" / "SKILL.md", skill_md("s", "L"))
        write(self.repo / ".loop" / "skills" / "s.md", "P")
        chain = sk.skill_chain("s", [str(self.repo)], global_dir=self.g, local_dir=self.l)
        self.assertEqual([c.scope for c in chain], ["project", "local", "global"])
        self.assertEqual(sk.find_skill("s", [str(self.repo)], global_dir=self.g, local_dir=self.l).body, "P")
        self.assertEqual(sk.find_skill("s", [], global_dir=self.g, local_dir=self.l).body, "L")

    def test_dir_form_beats_flat_in_same_scope(self):
        write(self.g / "s.md", "FLAT")
        write(self.g / "s" / "SKILL.md", skill_md("s", "DIR"))
        found = sk.find_skill("s", global_dir=self.g, local_dir=self.l)
        self.assertEqual((found.body, found.legacy), ("DIR", False))

    def test_legacy_flat_defaults(self):
        write(self.g / "old.md", "  旧正文  \n")
        s = sk.find_skill("old", global_dir=self.g, local_dir=self.l)
        self.assertEqual((s.legacy, s.activation, s.status, s.roles, s.body), (True, "always", "active", [], "旧正文"))

    def test_draft_override_falls_back_unless_requested(self):
        write(self.g / "s" / "SKILL.md", skill_md("s", "G"))
        write(self.l / "s" / "SKILL.md", skill_md("s", "DRAFT", status="draft"))
        self.assertEqual(sk.find_skill("s", global_dir=self.g, local_dir=self.l).body, "G")
        self.assertEqual(sk.find_skill("s", drafts=["s"], global_dir=self.g, local_dir=self.l).body, "DRAFT")

    def test_project_symlink_escape_ignored(self):
        outside = write(self.root / "outside" / "secret.md", "SECRET")
        (self.repo / ".loop" / "skills" / "leak.md").symlink_to(outside)
        self.assertIsNone(sk.find_skill("leak", [str(self.repo)], global_dir=self.g, local_dir=self.l))

    def test_text_capped_like_legacy_reader(self):
        write(self.g / "big.md", "字" * (sk.SKILL_MAX_CHARS + 10))
        s = sk.find_skill("big", global_dir=self.g, local_dir=self.l)
        self.assertEqual(s.truncated, 10)
        self.assertEqual(s.text, ac._read_capped(s.path))        # 截断口径与旧 _read_capped 逐字一致

    def test_discover_lists_all_scopes(self):
        write(self.g / "a" / "SKILL.md", skill_md("a"))
        write(self.l / "a" / "SKILL.md", skill_md("a"))
        write(self.repo / ".loop" / "skills" / "b.md", "B")
        write(self.g / "Bad_Name.md", "x")
        got = [(s.name, s.scope) for s in sk.discover([str(self.repo)], global_dir=self.g, local_dir=self.l)]
        self.assertEqual(got, [("a", "local"), ("a", "global"), ("b", "project")])

    def test_prompts_local_env(self):
        with mock.patch.dict(os.environ, {"REQ_BOARD_PROMPTS_LOCAL": str(self.root / "pl")}):
            self.assertEqual(sk.local_skills_dir(), self.root / "pl" / "skills")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REQ_BOARD_PROMPTS_LOCAL", None)
            self.assertEqual(sk.prompts_local_dir(), REPO / "prompts.local")


class SelectTest(TempDirs):

    def test_named_order_and_dedupe(self):
        write(self.g / "a.md", "A")
        write(self.g / "b" / "SKILL.md", skill_md("b", "B"))
        write(self.repo / ".loop" / "skills" / "c.md", "C")
        s = self.sel(role_skills=["b", "a"], manifest_skills=["a", "c"])
        self.assertEqual(self.names(s.always), ["b", "a", "c"])
        self.assertIn("flows.json", s.why["b"])
        self.assertIn("项目清单", s.why["c"])

    def test_self_declared_roles_union(self):
        write(self.g / "z" / "SKILL.md", skill_md("z", roles=["dev"]))
        write(self.g / "y" / "SKILL.md", skill_md("y", roles=["dev", "accept"]))
        write(self.g / "x" / "SKILL.md", skill_md("x", roles=["accept"]))
        s = self.sel(role_skills=["z"])
        self.assertEqual(self.names(s.always), ["z", "y"])     # 点名在前，自声明按名排序追加
        self.assertEqual(self.names(self.sel("accept").always), ["x", "y"])

    def test_auto_by_paths_and_keywords(self):
        write(self.g / "fe" / "SKILL.md", skill_md("fe", roles=["dev"], activation="auto",
                                                   paths=["static/**", "*.css"], keywords=["页面"]))
        s = self.sel(paths_hint=["static/board/50-x.js"])
        self.assertEqual(self.names(s.card), ["fe"])
        self.assertIn("static/**", s.card[0][1])
        self.assertEqual(self.names(self.sel(paths_hint=["deep/dir/a.css"]).card), ["fe"])
        s = self.sel(req={"title": "改页面", "notes": ""})
        self.assertIn("关键词", s.card[0][1])
        s = self.sel(paths_hint=["dispatcher.py"])
        self.assertEqual(s.card, [])
        self.assertEqual(s.skipped, [("fe", "auto：未命中路径或关键词")])

    def test_on_demand_goes_to_catalog(self):
        write(self.g / "ref" / "SKILL.md", skill_md("ref", roles=["dev"], activation="on_demand"))
        s = self.sel()
        self.assertEqual((self.names(s.catalog), s.always, s.card), (["ref"], [], []))
        self.assertIn("ref：ref 的描述", sk.catalog_block(s.catalog))

    def test_manual_only_when_notes_name_it(self):
        write(self.g / "op" / "SKILL.md", skill_md("op", roles=["dev"], activation="manual"))
        self.assertEqual(self.sel().skipped, [("op", "manual：卡片 notes 未点名")])
        s = self.sel(req={"notes": "做点事\nskills: op"})
        self.assertEqual(self.names(s.card), ["op"])
        self.assertIn("notes 点名", s.card[0][1])

    def test_notes_force_respects_declared_roles(self):
        write(self.g / "op" / "SKILL.md", skill_md("op", roles=["dev"], activation="manual"))
        write(self.g / "any.md", "ANY")
        s = self.sel("accept", req={"notes": "skills: op, any"})
        self.assertEqual(self.names(s.always), ["any"])        # 无 roles 声明的旧平铺 → 任何角色
        self.assertTrue(any(n == "op" and "只挂载到" in w for n, w in s.skipped))

    def test_notes_exclude(self):
        write(self.g / "a.md", "A")
        s = self.sel(req={"notes": "-skills: a"}, role_skills=["a"])
        self.assertEqual(s.always, [])
        self.assertEqual(s.skipped, [("a", "卡片 notes 排除（-skills）")])
        self.assertEqual(sk.parse_notes_skills("Skills：a、b c\n-skills: d,Bad!\nskills: a"),
                         (["a", "b", "c"], ["d"]))

    def test_missing_named_is_skipped_but_notes_named_is_required(self):
        s = self.sel(req={"notes": "skills: ghost"}, role_skills=["nope"])
        self.assertIn(("nope", "未找到（flows.json 点名 dev）"), s.skipped)
        self.assertEqual(s.missing_required, ["ghost"])

    def test_required_but_broken_stops(self):
        write(self.g / "req" / "SKILL.md", "---\nname: req\ndescription: d\nrequired: true\nactivation: bogus\n---\nX")
        s = self.sel(role_skills=["req"])
        self.assertEqual(s.missing_required, ["req"])
        write(self.g / "opt" / "SKILL.md", "---\nname: opt\ndescription: d\nactivation: bogus\n---\nX")
        s = self.sel(role_skills=["opt"])
        self.assertEqual(s.missing_required, [])
        self.assertTrue(s.skipped[0][1].startswith("不可用"))

    def test_drafts_only_in_preview(self):
        write(self.l / "new" / "SKILL.md", skill_md("new", roles=["dev"], status="draft"))
        self.assertEqual(self.sel().always, [])
        s = self.sel(role_skills=["new"])
        self.assertTrue(s.skipped[0][1].startswith("草稿"))
        self.assertEqual(self.names(self.sel(draft_skills=["new"]).always), ["new"])

    def test_deprecated_loads_with_warning(self):
        write(self.g / "old" / "SKILL.md", skill_md("old", status="deprecated"))
        s = self.sel(role_skills=["old"])
        self.assertEqual(self.names(s.always), ["old"])
        self.assertEqual(s.warnings[0]["code"], "deprecated_skill")

    def test_glob_and_path_extraction(self):
        for pat, path, hit in (("static/**", "static/a/b.js", True), ("static/", "static/x", True),
                               ("**/*.css", "a.css", True), ("*.css", "x/y/a.css", True),
                               ("templates/*.html", "templates/a/b.html", False), ("a?.md", "ab.md", True)):
            self.assertEqual(sk.glob_match(pat, path), hit, (pat, path))
        self.assertEqual(sk.paths_from_text("改 static/board/50-x.js 与 board.css，见 https://x.io/a 和 1.2.3"),
                         ["static/board/50-x.js", "board.css"])


class LintTest(TempDirs):

    def lint(self, path, scope="global"):
        s = sk.load_skill(path, scope=scope, legacy=path.name != "SKILL.md")
        return s, sk.lint(s, known_roles={"dev", "accept"})

    def test_clean_skill(self):
        _, items = self.lint(write(self.g / "ok" / "SKILL.md", skill_md("ok", roles=["dev"])))
        self.assertEqual(sk.lint_level(items), "ok")

    def test_errors_and_warnings(self):
        long_desc = "长" * (sk.SKILL_DESC_MAX + 1)
        p = write(self.g / "bad" / "SKILL.md",
                  sk.compose_skill_file({"name": "bad", "description": long_desc, "roles": ["ghost"],
                                         "activation": "auto", "colour": "red"},
                                        "看 references/missing.md 与 /home/me/x.py 及 docs/api.md"))
        _, items = self.lint(p)
        msgs = {i["level"]: [] for i in items}
        for i in items:
            msgs[i["level"]].append(i["message"])
        err, warn = "\n".join(msgs.get("error", [])), "\n".join(msgs.get("warn", []))
        self.assertIn("超过 200 字", err)
        self.assertIn("references/missing.md", err)
        for token in ("未知角色", "未知的 frontmatter 键", "永远不会自动命中", "/home/me/x.py", "docs/api.md"):
            self.assertIn(token, warn)

    def test_missing_description_body_and_too_long(self):
        _, items = self.lint(write(self.g / "e" / "SKILL.md", "---\nname: e\n---\n"))
        joined = "\n".join(i["message"] for i in items if i["level"] == "error")
        self.assertIn("description 必填", joined)
        self.assertIn("正文为空", joined)
        _, items = self.lint(write(self.g / "f" / "SKILL.md", skill_md("f", "字" * (sk.SKILL_MAX_CHARS + 1))))
        self.assertEqual(sk.lint_level(items), "error")

    def test_attachment_present_and_project_scope_paths_allowed(self):
        write(self.repo / ".loop" / "skills" / "p" / "references" / "a.md", "ref")
        p = write(self.repo / ".loop" / "skills" / "p" / "SKILL.md",
                  skill_md("p", "读 references/a.md；改 board.py/x.py；跑仓库的 scripts/run.sh"))
        _, items = self.lint(p, scope="project")
        self.assertEqual(sk.lint_level(items), "ok")      # 技能目录没有 scripts/ → 说的是仓库脚本，不算附属文件
        write(self.repo / ".loop" / "skills" / "p" / "scripts" / "other.sh", "x")
        _, items = self.lint(p, scope="project")
        self.assertTrue(any("scripts/run.sh" in i["message"] for i in items if i["level"] == "error"))

    def test_legacy_warns_migrate(self):
        _, items = self.lint(write(self.g / "old.md", "x"))
        self.assertTrue(any("旧平铺" in i["message"] for i in items if i["level"] == "warn"))


class MaterializeTest(TempDirs):

    def test_copy_dir_and_legacy_and_respect_existing(self):
        write(self.g / "a" / "SKILL.md", skill_md("a", "A 正文", activation="on_demand", owner="me"))
        write(self.g / "a" / "references" / "r.md", "R")
        (self.g / "a" / "link.md").symlink_to(self.g / "a" / "references" / "r.md")
        write(self.g / "b.md", "B 正文")
        wt = self.root / "wt"
        write(wt / ".claude" / "skills" / "c" / "SKILL.md", "仓库自带")
        write(self.g / "c.md", "C")
        skills = [sk.find_skill(n, global_dir=self.g, local_dir=self.l) for n in ("a", "b", "c")]
        out = sk.materialize_for_claude(wt, skills)
        self.assertEqual([p.name for p in out], ["a", "b"])
        a = wt / ".claude" / "skills" / "a"
        meta, body, errors = sk.parse_frontmatter((a / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual((meta, body.strip(), errors), ({"name": "a", "description": "a 的描述"}, "A 正文", []))
        self.assertEqual((a / "references" / "r.md").read_text(encoding="utf-8"), "R")
        self.assertFalse((a / "link.md").exists())
        self.assertEqual((a / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertIn("description: 场景技能 b", (wt / ".claude" / "skills" / "b" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual((wt / ".claude" / "skills" / "c" / "SKILL.md").read_text(encoding="utf-8"), "仓库自带")
        sk.materialize_for_claude(wt, [(skills[0], "why")])                 # 再次落盘覆盖自己生成的
        self.assertTrue((a / "SKILL.md").is_file())


class RepoCompatTest(unittest.TestCase):
    """本仓库现有配置（flows.json roles.*.skills + .loop/project.json skills）的兼容验收。"""

    def setUp(self):
        self.local = Path(tempfile.mkdtemp(prefix="skills-local-"))

    def tearDown(self):
        shutil.rmtree(self.local, ignore_errors=True)

    def test_every_role_matches_scenario_skills_text(self):
        repos = d._project_repos("req-board")
        self.assertEqual(repos, [str(REPO)])
        with mock.patch.dict(os.environ, {"REQ_BOARD_PROMPTS_LOCAL": str(self.local)}):
            for role in ac.FLOW_CONFIG["roles"]:
                s = sk.select_skills("req-board", role, {"title": "T", "notes": "N"}, repos)
                self.assertEqual((s.card, s.catalog, s.missing_required), ([], [], []), role)
                self.assertEqual(sk.skills_block(s.always), d.scenario_skills_text("req-board", role), role)
                legacy_names = list(d.role_skill_names(role))
                for n in ac.load_project_manifest(REPO)["skills"].get(role) or []:
                    if n not in legacy_names:
                        legacy_names.append(n)
                self.assertEqual([x.name for x in s.always], legacy_names, role)

    def test_expected_mounts_and_bodies_byte_identical(self):
        with mock.patch.dict(os.environ, {"REQ_BOARD_PROMPTS_LOCAL": str(self.local)}):
            got = {r: [x.name for x in sk.select_skills("req-board", r, {}, [str(REPO)]).always]
                   for r in ac.FLOW_CONFIG["roles"]}
            bv = sk.find_skill("browser-verify", [str(REPO)])
            ra = sk.find_skill("render-assert", [str(REPO)])
        self.assertEqual(got["dev"], ["browser-verify", "render-assert"])
        self.assertEqual(got["accept"], ["render-assert"])
        self.assertEqual(got["code_review"], ["render-assert"])
        for r in ("analysis", "tech", "tech_review", "proposal", "merge_resolve"):
            self.assertEqual(got.get(r, []), [], r)
        self.assertEqual((bv.scope, bv.legacy, _sha(bv.text)), ("global", False, BROWSER_VERIFY_OLD_SHA))
        self.assertEqual((ra.scope, ra.legacy, _sha(ra.text)), ("project", True, RENDER_ASSERT_OLD_SHA))
        self.assertEqual(ac.skill_text("browser-verify", [str(REPO)]), bv.text)

    def test_repo_skills_lint_clean_of_errors(self):
        for s in sk.discover([str(REPO)], local_dir=self.local):
            errs = [i for i in sk.lint(s, known_roles=set(ac.FLOW_CONFIG["roles"])) if i["level"] == "error"]
            self.assertEqual(errs, [], s.path)


class BoardApiTest(TempDirs):

    @classmethod
    def setUpClass(cls):
        import board
        cls.b = board

    def setUp(self):
        super().setUp()
        self.art = self.root / "artifacts"
        self.client = self.b.app.test_client()
        pj = write(self.root / "projects.json", json.dumps({"toy": {"paths": [str(self.repo)]}}))
        write(self.g / "gs" / "SKILL.md", skill_md("gs", "全局正文 读 references/r.md", roles=["dev"]))
        write(self.g / "gs" / "references" / "r.md", "R")
        write(self.g / "flat.md", "旧平铺")
        write(self.repo / ".loop" / "skills" / "proj" / "SKILL.md", skill_md("proj", "项目正文", roles=["accept"]))
        write(self.repo / ".loop" / "skills" / "gs.md", "项目覆盖全局")
        write(self.repo / ".loop" / "project.json", json.dumps({"skills": {"dev": ["proj"]}}))
        self._patches = [mock.patch.object(ac, "GLOBAL_SKILLS_DIR", self.g),
                         mock.patch.dict(os.environ, {"REQ_BOARD_PROMPTS_LOCAL": str(self.l.parent)}),
                         mock.patch.object(self.b, "ARTIFACTS_ROOT", self.art),
                         mock.patch.object(self.b, "PROJECTS_FILE", pj)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        super().tearDown()

    def manifest(self, run, ids, age=0):
        f = write(self.art / "toy" / "req-1" / "runs" / run / "manifest.json",
                  json.dumps({"items": [{"id": i} for i in ids]}))
        if age:
            t = time.time() - age
            os.utime(f, (t, t))

    def test_list_payload(self):
        self.manifest("r1", ["skill:gs", "skill:gs", "skill:proj", "role/dev"])
        self.manifest("r2", ["skill:gs"])
        self.manifest("old", ["skill:gs"], age=31 * 86400)
        write(self.art / "toy" / "req-2" / "runs" / "bad" / "manifest.json", "{not json")
        data = self.client.get("/api/skills").get_json()
        rows = {(r["name"], r["project"]): r for r in data["skills"]}
        self.assertEqual(set(rows), {("gs", None), ("flat", None), ("proj", "toy"), ("gs", "toy")})
        gs = rows[("gs", None)]
        self.assertEqual((gs["scope"], gs["uses_30d"], gs["editable"], gs["activation"]), ("global", 2, True, "always"))
        self.assertEqual(rows[("proj", "toy")]["uses_30d"], 1)
        self.assertFalse(rows[("proj", "toy")]["editable"])
        self.assertEqual(rows[("gs", "toy")]["override_of"], "global")
        self.assertIn({"role": "dev", "source": "project:toy"}, rows[("proj", "toy")]["mounted"])
        self.assertEqual(rows[("proj", "toy")]["effective_roles"], ["accept", "dev"])
        self.assertEqual(rows[("flat", None)]["lint_level"], "warn")
        self.assertEqual([r["id"] for r in data["roles"]][:2], ["analysis", "proposal"])
        self.assertEqual(data["projects"], ["toy"])

    def test_get_detail(self):
        r = self.client.get("/api/skills/gs")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["body"], "全局正文 读 references/r.md")
        r = self.client.get("/api/skills/gs?project=toy").get_json()
        self.assertEqual((r["scope"], r["body"]), ("project", "项目覆盖全局"))
        self.assertEqual([v["scope"] for v in r["versions"]], ["project", "global"])
        self.assertEqual(self.client.get("/api/skills/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/skills/Bad!").status_code, 400)
        self.assertEqual(self.client.get("/api/skills/gs?project=ghost").status_code, 404)

    def test_put_creates_local_override_and_delete_restores(self):
        r = self.client.put("/api/skills/gs", json={"roles": ["dev", "accept"], "activation": "auto",
                                                    "paths": ["static/**"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual((body["scope"], body["override_of"], body["roles"]), ("local", "global", ["dev", "accept"]))
        self.assertEqual(body["body"], "全局正文 读 references/r.md")          # 未传正文沿用默认
        self.assertTrue((self.l / "gs" / "references" / "r.md").is_file())     # 附属文件随覆盖复制
        self.assertEqual(sk.find_skill("gs", global_dir=self.g, local_dir=self.l).activation, "auto")
        r = self.client.delete("/api/skills/gs")
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.l / "gs").exists())
        self.assertEqual(self.client.delete("/api/skills/gs").status_code, 403)    # 全局默认不可删
        self.assertEqual(self.client.delete("/api/skills/none").status_code, 404)

    def test_put_raw_text_and_validation(self):
        r = self.client.put("/api/skills/flat", json={"text": skill_md("flat", "新正文", activation="manual")})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["activation"], "manual")
        r = self.client.put("/api/skills/flat", json={"description": ""})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("description" in e["message"] for e in r.get_json()["errors"]))
        r = self.client.put("/api/skills/flat", json={"text": "---\nname: flat\nx: |\n---\n"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.put("/api/skills/flat", json={"roles": "dev"}).status_code, 400)
        self.assertEqual(self.client.put("/api/skills/ghost", json={}).status_code, 404)

    def test_project_skill_read_only(self):
        self.assertEqual(self.client.put("/api/skills/proj", json={}).status_code, 403)
        self.assertEqual(self.client.put("/api/skills/gs?project=toy", json={}).status_code, 403)

    def test_post_creates_draft(self):
        r = self.client.post("/api/skills", json={"name": "fresh", "description": "新技能", "body": "做法",
                                                  "roles": ["dev"]})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual((r.get_json()["status"], r.get_json()["activation"]), ("draft", "on_demand"))
        self.assertIsNone(sk.find_skill("fresh", global_dir=self.g, local_dir=self.l))     # 草稿不进真实会话
        self.assertEqual(self.client.post("/api/skills", json={"name": "fresh", "description": "x",
                                                               "body": "y"}).status_code, 409)
        self.assertEqual(self.client.post("/api/skills", json={"name": "gs"}).status_code, 409)
        self.assertEqual(self.client.post("/api/skills", json={"name": "Bad!"}).status_code, 400)
        self.assertEqual(self.client.post("/api/skills", json={"name": "nodesc", "body": "x"}).status_code, 400)
        self.assertFalse((self.l / "nodesc").exists())

    def test_writes_are_loopback_only(self):
        env = {"REMOTE_ADDR": "10.1.2.3"}
        self.assertEqual(self.client.get("/api/skills", environ_base=env).status_code, 200)
        self.assertEqual(self.client.put("/api/skills/gs", json={}, environ_base=env).status_code, 403)
        self.assertEqual(self.client.post("/api/skills", json={"name": "zz"}, environ_base=env).status_code, 403)
        self.assertEqual(self.client.delete("/api/skills/gs", environ_base=env).status_code, 403)
        self.assertFalse(self.l.joinpath("gs").exists())


if __name__ == "__main__":
    unittest.main()
