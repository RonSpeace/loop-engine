#!/usr/bin/env python3
"""A3 项目清单 `.loop/project.json` + A2 提示词三层注入（通用协议 → 项目约定 → 场景技能）。

- load_project_manifest：归一化 commands/instance/guides/skills/high_risk_paths，宽容降级 + errors 清单
  （缺文件 = 未配置，不是错误）；指引文件必须是仓库内相对路径、限长。
- project_conventions_text：有清单 → 列命令/实例/指引（按角色过滤），缺项显式写「未配置」；
  无清单 → 明确声明未配置，不出现任何其它项目的命令/端口。
- scenario_skills_text：技能片段按 flows.json roles.<id>.skills + 清单 skills.<角色> 选取，
  项目技能（.loop/skills/）优先于全局（prompts/skills/）；新增技能不改 Python。
- build_prompt：三层顺序固定——通用协议（COMMON_TAIL）在前，项目约定其后，场景技能最后。
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402


def make_repo(manifest=None, files=None):
    root = Path(tempfile.mkdtemp(prefix="manifest-"))
    if manifest is not None:
        (root / ".loop").mkdir()
        (root / ".loop" / "project.json").write_text(
            manifest if isinstance(manifest, str) else json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for rel, text in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


FULL = {
    "version": 1,
    "commands": {"test": "make test", "lint": "make lint", "build": ""},
    "instance": {"start": "bin/dev start {port}", "stop": "bin/dev stop {port}",
                 "port_range": [9100, 9199], "healthcheck": "http://127.0.0.1:{port}/health"},
    "guides": {"instance": {"file": ".loop/guides/instance.md", "roles": ["dev", "accept"]},
               "service": ".loop/guides/service.md"},
    "skills": {"dev": ["toy-skill"]},
    "high_risk_paths": ["core/engine.py"],
}


class LoadManifest(unittest.TestCase):

    def setUp(self):
        self.repos = []

    def tearDown(self):
        for r in self.repos:
            shutil.rmtree(r, ignore_errors=True)

    def repo(self, *a, **k):
        r = make_repo(*a, **k)
        self.repos.append(r)
        return r

    def test_missing_file_is_unconfigured_not_error(self):
        m = ac.load_project_manifest(self.repo())
        self.assertIsNone(m["path"])
        self.assertEqual(m["errors"], [])
        self.assertEqual(m["commands"], {})

    def test_full_manifest_normalized(self):
        r = self.repo(FULL, {".loop/guides/instance.md": "实例指引正文", ".loop/guides/service.md": "服务指引正文"})
        m = ac.load_project_manifest(r)
        self.assertEqual(m["errors"], [])
        self.assertEqual(m["commands"], {"test": "make test", "lint": "make lint"})   # 空串视为未配置
        self.assertEqual(m["instance"]["port_range"], [9100, 9199])
        self.assertEqual(m["guides"]["instance"]["roles"], ["dev", "accept"])
        self.assertEqual(m["guides"]["instance"]["text"], "实例指引正文")
        self.assertIsNone(m["guides"]["service"]["roles"])                              # 字符串简写 = 全角色
        self.assertEqual(m["skills"], {"dev": ["toy-skill"]})
        self.assertEqual(m["high_risk_paths"], ["core/engine.py"])

    def test_invalid_json_reported(self):
        m = ac.load_project_manifest(self.repo("{not json"))
        self.assertTrue(m["errors"])
        self.assertEqual(m["commands"], {})

    def test_bad_fields_degrade_with_errors(self):
        bad = {"commands": {"test": 3, "weird": "x"}, "instance": {"port_range": [9, "a"]},
               "guides": {"esc": "../outside.md", "abs": "/etc/passwd", "gone": ".loop/guides/none.md"},
               "skills": {"dev": "not-a-list"}, "high_risk_paths": "x"}
        m = ac.load_project_manifest(self.repo(bad))
        self.assertEqual(m["commands"], {})
        self.assertNotIn("port_range", m["instance"])
        self.assertEqual(m["guides"], {})
        self.assertEqual(m["skills"], {})
        self.assertEqual(m["high_risk_paths"], [])
        joined = "\n".join(m["errors"])
        for token in ("commands.test", "commands.weird", "port_range", "guides.esc", "guides.abs",
                      "guides.gone", "skills.dev", "high_risk_paths"):
            self.assertIn(token, joined)

    # ---- 评审复修：绝不抛 + 不越出仓库 ----
    def test_symlink_escape_rejected(self):
        outside = Path(tempfile.mkdtemp(prefix="outside-"))
        self.repos.append(outside)
        (outside / "secret.md").write_text("SECRET-CONTENT", encoding="utf-8")
        r = self.repo({"guides": {"leak": ".loop/guides/leak.md"}})
        (r / ".loop" / "guides").mkdir()
        (r / ".loop" / "guides" / "leak.md").symlink_to(outside / "secret.md")
        m = ac.load_project_manifest(r)
        self.assertEqual(m["guides"], {})
        self.assertIn("guides.leak", "\n".join(m["errors"]))
        (r / ".loop" / "skills").mkdir()
        (r / ".loop" / "skills" / "leak.md").symlink_to(outside / "secret.md")
        self.assertIsNone(ac.skill_text("leak", [r]))

    def test_never_raises_on_hostile_input(self):
        r = self.repo({"guides": {"long": "a" * 400 + ".md", "dir": ".loop"}, "skills": {"dev": ["x"]}})
        m = ac.load_project_manifest(r)              # 超长文件名 / 指向目录 → 不抛，记 errors
        self.assertEqual(m["guides"], {})
        self.assertEqual(len([e for e in m["errors"] if e.startswith("guides.")]), 2)
        self.assertEqual(ac.load_project_manifest("/no/such/repo")["path"], None)
        # 指引读取走 acp_shared._read_capped；技能读取走定义所在模块 acp_modules.skills._read_raw
        with mock.patch.object(ac, "_read_capped", side_effect=PermissionError("denied")), \
                mock.patch("acp_modules.skills._read_raw", side_effect=PermissionError("denied")):
            r2 = self.repo({"guides": {"g": ".loop/g.md"}}, {".loop/g.md": "x", ".loop/skills/s.md": "y"})
            m2 = ac.load_project_manifest(r2)
            self.assertEqual(m2["guides"], {})
            self.assertIn("guides.g", "\n".join(m2["errors"]))
            self.assertIsNone(ac.skill_text("s", [r2]))


    def test_guide_text_capped(self):
        r = self.repo({"guides": {"big": ".loop/guides/big.md"}},
                      {".loop/guides/big.md": "字" * (ac.MANIFEST_GUIDE_MAX_CHARS + 100)})
        text = ac.load_project_manifest(r)["guides"]["big"]["text"]
        self.assertLess(len(text), ac.MANIFEST_GUIDE_MAX_CHARS + 300)
        self.assertIn("已截断", text)


class ConventionsAndSkills(unittest.TestCase):

    def setUp(self):
        self.full = make_repo(FULL, {".loop/guides/instance.md": "实例指引正文 PORT-GUIDE",
                                     ".loop/guides/service.md": "服务指引正文 SVC-GUIDE",
                                     ".loop/skills/toy-skill.md": "项目技能正文 TOY-SKILL",
                                     "AGENTS.md": "# toy"})
        self.bare = make_repo()
        self.projects = {
            "toy": {"paths": [str(self.full)], "confirm": False, "company": False, "repo_map": "",
                    "restart": None, "auto_restart_after_merge": False,
                    "base_branch": None, "goal": ""},
            "bare": {"paths": [str(self.bare)], "confirm": False, "company": False, "repo_map": "",
                     "restart": None, "auto_restart_after_merge": False,
                     "base_branch": None, "goal": ""},
            "nopaths": {"paths": []},
        }
        self._p = mock.patch.object(d, "PROJECTS", self.projects)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.full, ignore_errors=True)
        shutil.rmtree(self.bare, ignore_errors=True)

    def test_conventions_with_manifest(self):
        text = d.project_conventions_text("toy", "dev")
        for token in ("项目约定", "make test", "make lint", "bin/dev start {port}", "9100-9199",
                      "PORT-GUIDE", "SVC-GUIDE", "AGENTS.md"):
            self.assertIn(token, text)
        self.assertIn("未配置", text)                       # build/typecheck 等缺项显式声明
        self.assertIn("构建", text)

    def test_guides_filtered_by_role(self):
        text = d.project_conventions_text("toy", "analysis")
        self.assertNotIn("PORT-GUIDE", text)                # instance 指引只给 dev/accept
        self.assertIn("SVC-GUIDE", text)

    def test_conventions_without_manifest_states_unconfigured(self):
        text = d.project_conventions_text("bare", "accept")
        self.assertIn("项目约定", text)
        self.assertIn("未提供", text)
        for leaked in ("test_instance.sh", "8899", "8902", "REQ_BOARD_PYTHON", "launchd", "board.py"):
            self.assertNotIn(leaked, text)

    def test_conventions_tolerates_odd_project_shapes(self):
        self.assertIn("未提供", d.project_conventions_text("nopaths", "dev"))
        self.assertIn("未提供", d.project_conventions_text("unknown-project", "dev"))

    def test_skills_from_manifest_and_role_config(self):
        text = d.scenario_skills_text("toy", "dev")
        self.assertIn("TOY-SKILL", text)
        self.assertNotIn("TOY-SKILL", d.scenario_skills_text("toy", "accept"))
        self.assertNotIn("TOY-SKILL", d.scenario_skills_text("bare", "dev"))

    def test_project_skill_overrides_global_same_name(self):
        gdir = Path(tempfile.mkdtemp(prefix="gskills-"))
        (gdir / "toy-skill.md").write_text("全局技能正文 GLOBAL-SKILL", encoding="utf-8")
        (gdir / "only-global.md").write_text("全局独有 ONLY-GLOBAL", encoding="utf-8")
        try:
            with mock.patch.object(ac, "GLOBAL_SKILLS_DIR", gdir), \
                 mock.patch.object(d, "role_skill_names", lambda role: ["only-global"] if role == "dev" else []):
                text = d.scenario_skills_text("toy", "dev")
            self.assertIn("TOY-SKILL", text)
            self.assertNotIn("GLOBAL-SKILL", text)
            self.assertIn("ONLY-GLOBAL", text)
        finally:
            shutil.rmtree(gdir, ignore_errors=True)

    def test_missing_skill_file_is_noted_not_fatal(self):
        with mock.patch.object(d, "role_skill_names", lambda role: ["no-such-skill"]):
            text = d.scenario_skills_text("bare", "dev")
        self.assertNotIn("Traceback", text)

    def test_build_prompt_layer_order(self):
        wd = Path("/tmp/layers-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"toy": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value="工作目录：/tmp/layers-wd"):
            p = d.build_prompt({"id": 3, "project": "toy", "title": "T", "notes": "N"}, "dev")
        i_common, i_proj, i_skill = p.index("工作约束："), p.index("项目约定（toy"), p.index("TOY-SKILL")
        self.assertLess(i_common, i_proj)
        self.assertLess(i_proj, i_skill)

    def test_build_prompt_other_project_has_no_foreign_specifics(self):
        wd = Path("/tmp/layers-wd")
        with mock.patch.object(d, "ensure_dev_worktrees", return_value=(wd, {"bare": wd})), \
                mock.patch.object(d, "dev_repo_listing", return_value="工作目录：/tmp/layers-wd"):
            for role in ("analysis", "tech", "dev", "code_review", "accept"):
                p = d.build_prompt({"id": 3, "project": "bare", "title": "T", "notes": "N"}, role)
                for leaked in ("req-board 用", "board.py", "dispatcher.py", "test_instance.sh", "8899", "./start.sh",
                               "REQ_BOARD_PYTHON", "launchd", "test_diff_render"):
                    self.assertNotIn(leaked, p, f"{role} 提示词泄漏 {leaked}")


class RoleSkillsConfig(unittest.TestCase):

    def test_role_skill_names_from_flow_config(self):
        self.assertIsInstance(d.role_skill_names("dev"), list)
        self.assertEqual(d.role_skill_names("no-such-role"), [])

    def test_declared_global_skills_exist(self):
        for role in ac.ROLE_CAPABILITIES:
            for name in d.role_skill_names(role):
                self.assertTrue((ac.GLOBAL_SKILLS_DIR / name / "SKILL.md").is_file()
                                or (ac.GLOBAL_SKILLS_DIR / f"{name}.md").is_file(), f"{role}: {name}")


class AgentsMdLimit(unittest.TestCase):
    """A4：AGENTS.md 长度上限（默认 150 行 / 12000 字符，清单可调），doctor 只告警不阻断"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="agentsmd-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_missing_file_reported(self):
        self.assertIn("不存在", ac.agents_md_problems(self.root)[0])

    def test_within_limits_no_problem(self):
        (self.root / "AGENTS.md").write_text("# x\n" * 20, encoding="utf-8")
        self.assertEqual(ac.agents_md_problems(self.root), [])

    def test_over_lines_and_chars(self):
        (self.root / "AGENTS.md").write_text("行\n" * (ac.AGENTS_MD_MAX_LINES + 1), encoding="utf-8")
        self.assertTrue(any("行，超过上限" in m for m in ac.agents_md_problems(self.root)))
        (self.root / "AGENTS.md").write_text("字" * (ac.AGENTS_MD_MAX_CHARS + 1), encoding="utf-8")
        msgs = ac.agents_md_problems(self.root)
        self.assertTrue(any("字符，超过上限" in m for m in msgs))
        self.assertTrue(any("下沉" in m for m in msgs))          # 给出处置方式

    def test_manifest_overrides_limits(self):
        (self.root / "AGENTS.md").write_text("行\n" * 30, encoding="utf-8")
        self.assertTrue(ac.agents_md_problems(self.root, {"agents_md": {"max_lines": 10}}))
        self.assertEqual(ac.agents_md_problems(self.root, {"agents_md": {"max_lines": "x"}}), [])

    def test_templates_shipped_and_valid(self):
        tmpl = REPO / "docs" / "templates" / "AGENTS.template.md"
        text = tmpl.read_text(encoding="utf-8")
        for section in ("## 架构概览", "## 目录约定", "## 编码规范", "## 业务红线", "## 注意事项"):
            self.assertIn(section, text)
        example = json.loads((REPO / "docs" / "templates" / "project.example.json").read_text(encoding="utf-8"))
        self.assertTrue(set(example["commands"]) <= set(ac.MANIFEST_COMMAND_KEYS))

    def test_repo_own_manifest_is_clean(self):
        """本仓库自己的 .loop/project.json 无校验错误，指引/技能文件齐全"""
        m = ac.load_project_manifest(REPO)
        self.assertIsNotNone(m["path"])
        self.assertEqual(m["errors"], [])
        for role, names in m["skills"].items():
            for n in names:
                self.assertIsNotNone(ac.skill_text(n, [REPO]), f"{role}: {n}")

    def test_doctor_reports_conventions(self):
        import contextlib
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("doctor_under_test", REPO / "scripts" / "doctor.py")
        doctor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doctor)
        repo = make_repo({"commands": {"test": 1}}, {"AGENTS.md": "行\n" * 500})
        fake_root = Path(tempfile.mkdtemp(prefix="doctor-root-"))
        try:
            (fake_root / "projects.json").write_text(json.dumps({"p": str(repo), "gone": "/no/such/dir"}))
            buf = io.StringIO()
            with mock.patch.object(doctor, "REPO", fake_root), contextlib.redirect_stdout(buf):
                doctor.check_project_conventions()
            out = buf.getvalue()
            self.assertIn("commands.test", out)
            self.assertIn("超过上限", out)
            self.assertEqual(doctor.HARD_FAIL, [])              # 只告警，不算硬失败
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(fake_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
