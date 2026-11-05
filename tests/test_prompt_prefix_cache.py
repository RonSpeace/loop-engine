#!/usr/bin/env python3
"""#42（req-42）提示词前缀缓存测试：固定段逐字稳定 + 卡片输入块隔离 + {card_inputs} 契约。

P1 固定段逐字稳定（不 mock worktree 层，真 git 仓 + WORKTREES_ROOT 指临时目录）
P2 notes/timeout/repos 覆盖变化不改写固定段；repo_map 提示行在卡片块；goal 留固定段
P3 评审反馈/extra/merge_ctx 变化不改写固定段
P4 A2 三层顺序不回归（工作约束 < 项目约定 < 场景技能）
P5 卡片字段全部出现在场景技能之后
P6 前置契约在可变尾之后、同卡逐字相等、随流程目录变化；kill 红线仍在最末
P7 模板缺 {card_inputs} 标记 → warning + 整段可变回退
P8 静态 COMMON_TAIL 无占位符、合法单层花括号；全 8 角色构建无 KeyError；卡片块含时限/路径
P9 8 个流程模板必须含且仅含一个 {card_inputs} 标记
P10 /guide 渲染冒烟（占位符表 + 静态公共尾）
P11 固定段占位符白名单扫描（防按卡值回流固定段）

运行（worktree 内，用主工作区 .venv）：
    .venv/bin/python -m pytest tests/test_prompt_prefix_cache.py -q
"""
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

ROLES = ("analysis", "proposal", "tech", "tech_review", "dev", "code_review", "accept", "merge_resolve")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, timeout=60)


def make_git_repo(root):
    """真 git 仓（master + 初始提交）：P1 不 mock ensure_dev_worktrees，走真实 worktree 创建
    （照 #420 隔离判据：patch WORKTREES_ROOT 即视为已隔离，短路口不触发）。"""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "master", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("hello\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return root


class PrefixCacheBase(unittest.TestCase):
    """隔离底座：真 git 仓 + WORKTREES_ROOT 指临时目录。不 mock ensure_dev_worktrees /
    dev_repo_listing——repo_listing 的逐字差异（req-<id> 路径、f-YYYYMMDD-<id> 分支名）正是
    P1 的断言对象；mock 掉它们会让 P1 退化成同义反复。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="req42-prefix-"))
        self.r1 = make_git_repo(self.base / "repo-one")
        self.r2 = make_git_repo(self.base / "repo-two")
        self.projects = {
            "proj": {"paths": [str(self.r1), str(self.r2)], "confirm": False, "company": False,
                     "repo_map": "", "restart": None, "auto_restart_after_merge": False,
                     "base_branch": None, "goal": ""},
        }
        self._patches = [
            mock.patch.object(d, "PROJECTS", self.projects),
            mock.patch.object(d, "WORKTREES_ROOT", self.base / "wt"),
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

    def parts(self, req, role, extra=""):
        return d.build_prompt_parts(req, role, extra=extra)

    def req(self, rid, notes="N", title="T", summary=None):
        r = {"id": rid, "project": "proj", "title": title, "notes": notes,
             "mode": "auto", "created_at": "2026-10-15 10:00:00"}
        if summary:
            r["summary"] = summary
        return r


class P1FixedPartStable(PrefixCacheBase):
    """验收 1：同一角色、同一项目连续请求的固定前缀逐字稳定；卡片内容变化不改写固定段。
    覆盖全部 8 角色（code_review 第 1 轮打回项：stage_input 类模板 tech/code_review/accept
    的固定段含「阶段输入」指路句，须证明按卡路径不回流固定段）"""

    def test_fixed_part_identical_across_cards_all_roles(self):
        req_a = self.req(42, notes="要点甲42", title="标题甲")
        req_b = self.req(43, notes="要点乙43", title="标题乙")
        for role in ROLES:
            d.state.setdefault("merge_resolve", {})["42"] = {"failed": [str(self.r1)]}
            pa = self.parts(req_a, role)
            pb = self.parts(req_b, role)
            self.assertEqual(pa[0], pb[0], f"{role}: 固定段随卡变化")       # 固定段逐字全等
            self.assertEqual(pa[1], pb[1], f"{role}: 项目约定随卡变化")     # 项目约定不随卡变
            self.assertEqual(pa[2], pb[2], f"{role}: 场景技能随卡变化")     # 场景技能不随卡变
            self.assertNotEqual(pa[3], pb[3], f"{role}: 卡片输入块未随卡变")  # 卡片输入块随卡变
            # 按卡 worktree 路径/分支/标题/notes 不得回流固定段（含 stage_input 指路句模板）
            self.assertNotIn("req-42", pa[0], f"{role}: 固定段含按卡产物路径")
            self.assertNotIn("req-43", pa[0], f"{role}: 固定段含按卡产物路径")
            self.assertNotIn(d.dev_branch(req_a), pa[0], f"{role}: 固定段含按卡分支名")
            self.assertNotIn("标题甲", pa[0], f"{role}: 固定段含按卡标题")
            self.assertNotIn("要点甲42", pa[0], f"{role}: 固定段含按卡 notes")
            # 按卡输入（含 stage_input 的 req-<id> 上游产物路径）只落在卡片输入块
            self.assertIn("req-42", pa[3], f"{role}: 卡片块缺按卡输入")
            self.assertIn(d.dev_branch(req_a), pa[3], f"{role}: 卡片块缺按卡分支")
            self.assertIn("标题甲", pa[3], f"{role}: 卡片块缺按卡标题")

    def test_card_block_header_marks_variable_tail(self):
        card = self.parts(self.req(42), "dev")[3]
        self.assertIn("本卡专有", card)


class P2NotesVariations(PrefixCacheBase):
    """P2：notes 变化（含 timeout/repos 覆盖）不改写固定段；时限数字只在卡片块；
    repo_map 提示行（按卡路径形态）不出现在固定段；goal 项目级常量留固定段"""

    def test_notes_and_overrides_do_not_touch_fixed(self):
        pa = self.parts(self.req(42, notes="基础需求"), "dev")
        pb = self.parts(self.req(42, notes="复杂需求\n timeout: 45\n repos: repo-one"), "dev")
        self.assertEqual(pa[0], pb[0])
        self.assertNotEqual(pa[3], pb[3])
        self.assertIn("45 分钟时限", pb[3])                  # 时限数字只由卡片输入块提供
        self.assertNotIn("45 分钟", pb[0])                   # 固定段（含静态公共尾）不含时限数字

    def test_repo_map_hint_in_card_block_goal_in_fixed(self):
        self.projects["proj"]["repo_map"] = "/tmp/artifacts/proj/req-42/repo-map.md"
        self.projects["proj"]["goal"] = "项目级目标 GOAL-42"
        pa = self.parts(self.req(42), "analysis")
        pb = self.parts(self.req(43), "analysis")
        self.assertEqual(pa[0], pb[0])
        hint = "仓库地图（跨仓库定位代码的第一来源，先读再分析）：/tmp/artifacts/proj/req-42/repo-map.md"
        self.assertNotIn(hint, pa[0])                        # {repo_map_hint} 整行不得回流固定段
        self.assertIn(hint, pa[3])
        self.assertIn("GOAL-42", pa[0])                      # goal 与卡无关 → 固定段
        self.assertNotIn("repo-map.md", pa[0])


class P3FeedbackVariations(PrefixCacheBase):
    """P3：打回意见/extra/merge_ctx 变化不改写固定段"""

    def test_review_feedback_change_only_in_card_block(self):
        pa = self.parts(self.req(42), "tech")
        bounced = self.req(42, summary="第 1 轮打回：缺浏览器验证")
        pb = self.parts(bounced, "tech")
        self.assertEqual(pa[0], pb[0])
        self.assertIn("第 1 轮打回", pb[3])
        self.assertNotIn("第 1 轮打回", pb[0])

    def test_company_fragment_in_stable_prefix(self):
        """上下文编译器二期：仅推分支是项目级条件片段（L3）→ 进稳定前缀（项目约定之后），不进卡片块"""
        pa = self.parts(self.req(42), "dev")
        self.projects["proj"]["company"] = True
        pb = self.parts(self.req(42), "dev")
        pc = self.parts(self.req(43), "dev")
        self.projects["proj"]["company"] = False
        self.assertEqual(pa[0], pb[0])
        self.assertIn("仅推分支项目", pb[1])
        self.assertNotIn("仅推分支项目", pb[3])
        self.assertEqual(pb[1], pc[1], "片段随项目不随卡")

    def test_merge_ctx_change_only_in_card_block(self):
        d.state.setdefault("merge_resolve", {})["42"] = {"failed": [str(self.r1)]}
        pa = self.parts(self.req(42), "merge_resolve")
        self.assertIn("冲突仓库（需解决）", pa[3])
        self.assertIn("主干: master", pa[3])                 # 主干探测按真实 git 仓给出
        d.state["merge_resolve"]["42"] = {"failed": []}
        pb = self.parts(self.req(42), "merge_resolve")
        self.assertEqual(pa[0], pb[0])
        self.assertNotEqual(pa[3], pb[3])


class P4LayerOrder(PrefixCacheBase):
    """P4：A2 三层顺序不回归——工作约束（固定段末）< 项目约定 < 场景技能"""

    def test_a2_layer_order_preserved(self):
        loop = self.r1 / ".loop"
        (loop / "guides").mkdir(parents=True, exist_ok=True)
        (loop / "skills").mkdir(parents=True, exist_ok=True)
        (loop / "guides" / "svc.md").write_text("服务指引 SVC-GUIDE-42", encoding="utf-8")
        (loop / "skills" / "toy-skill.md").write_text("TOY-SKILL-42", encoding="utf-8")
        (loop / "project.json").write_text(
            '{"commands": {"test": "make test"}, "guides": {"svc": ".loop/guides/svc.md"},'
            '"skills": {"dev": ["toy-skill"]}}', encoding="utf-8")
        p = d.build_prompt(self.req(42), "dev")
        i_common, i_proj, i_skill = p.index("工作约束："), p.index("项目约定（proj"), p.index("TOY-SKILL-42")
        self.assertLess(i_common, i_proj)
        self.assertLess(i_proj, i_skill)


class P5CardFieldsAfterSkills(PrefixCacheBase):
    """P5：卡片字段（title/notes/分支/评审反馈/extra/repo_listing）出现在场景技能之后"""

    def test_card_fields_in_variable_tail(self):
        p = "".join(self.parts(self.req(42, title="标题四二", notes="要点四二"), "dev"))
        i_skill = p.index("【场景技能：browser-verify】")     # flows.json dev 角色声明的全局技能
        for token in ("本卡专有", "看板需求 #42", "标题四二", "要点四二", "f-20261015-42",
                      "工作目录："):
            self.assertGreater(p.index(token), i_skill, token)


class P6PreSlotContract(PrefixCacheBase):
    """P6：前置契约仅 pre-stage 追加、位于可变尾之后、同卡逐字相等、随流程目录变化；
    kill 红线仍在最末且含当次 PID"""

    def test_contract_after_card_block_and_stable(self):
        req = self.req(42)
        del req["mode"]          # 无 mode/快照 = 前置态（mode:auto 会被历史卡懒映射进流程，非前置）
        p1 = d.build_prompt(req, "analysis")
        p2 = d.build_prompt(req, "analysis")
        i_card = p1.index("本卡专有")
        i_contract = p1.index("前置环节输出契约（本环节")   # 契约正文起点（固定段里另有段名提法，用 index 会撞上）
        self.assertGreater(i_contract, i_card)               # 契约在卡片输入块之后
        self.assertEqual(p1[i_contract:], p2[i_contract:])   # 同卡两次构建逐字相等
        self.assertEqual(p1[:i_contract], p2[:i_contract])
        with mock.patch.object(d, "_flow_catalog_text", lambda: "- other（其它）｜x｜a → b"):
            p3 = d.build_prompt(req, "analysis")
        self.assertNotEqual(p1[i_contract:], p3[i_contract:])   # 流程目录变化才变
        self.assertEqual(p1[:i_contract], p3[:i_contract])      # 其余部分不随目录变

    def test_kill_redline_still_last_with_pid(self):
        p = d.build_prompt(self.req(42), "dev")
        pr = d.append_kill_redline(p, 424242)
        self.assertIn("424242", pr[-2000:])                  # 红线含当次 PID
        self.assertEqual(pr, p + "\n\n" + ac.kill_redline_text(424242))


class P7MissingMarkerFallback(PrefixCacheBase):
    """P7：模板缺 {card_inputs} 标记 → warning + 整段可变回退，prompt 仍完整可发"""

    def test_template_without_marker_falls_back(self):
        tmpl = d.role_prompt("dev").replace(d.CARD_INPUTS_MARK + "\n", "")
        self.assertNotIn(d.CARD_INPUTS_MARK, tmpl)
        with mock.patch.object(d, "role_prompt", lambda role: tmpl):
            p = d.build_prompt(self.req(42), "dev")
        self.assertNotIn(d.CARD_INPUTS_MARK, p)
        self.assertIn("看板需求 #42", p)                     # 整段按可变段渲染，内容完整
        self.assertIn("工作约束：", p)                       # 静态公共尾仍在


class P8StaticCommonTail(PrefixCacheBase):
    """P8：静态 COMMON_TAIL 无残留 format 占位符、合法单层花括号；全 8 角色构建无 KeyError；
    卡片输入块渲染含时限数字与 result_json 路径"""

    def test_common_tail_static_constant(self):
        ct = d.COMMON_TAIL
        self.assertIn('{"status": "done|blocked|need_confirm"', ct)   # 合法单层花括号 JSON 示例
        self.assertNotIn("{{", ct)                                    # 无 {{ 残留
        self.assertFalse(PLACEHOLDER_RE.search(ct), PLACEHOLDER_RE.findall(ct))

    def test_all_roles_build_without_keyerror(self):
        d.state.setdefault("merge_resolve", {})["42"] = {"failed": []}
        for role in ROLES:
            p = d.build_prompt(self.req(42, notes="N timeout: 45"), role)
            self.assertNotIn(d.CARD_INPUTS_MARK, p, role)
            self.assertNotIn("{{", p, role)

    def test_card_block_keeps_per_card_values(self):
        req = self.req(42, notes="N timeout: 45")
        card = self.parts(req, "dev")[3]
        self.assertIn("45 分钟时限", card)
        self.assertIn(str(d.result_file_path(req)), card)
        self.assertIn("步数上限", card)


class P9MarkerChecklist(PrefixCacheBase):
    """P9：8 个流程模板必须含且仅含一个 {card_inputs} 标记（清单断言，防漏改）"""

    def test_all_flow_templates_have_exactly_one_marker(self):
        for role in ROLES:
            tmpl = d.role_prompt(role)
            self.assertEqual(tmpl.count(d.CARD_INPUTS_MARK), 1, role)


class P10GuideSmoke(PrefixCacheBase):
    """P10：/guide 渲染冒烟——占位符表含 {card_inputs}，静态公共尾渲染不炸"""

    def test_guide_renders_marker_and_static_tail(self):
        import board as b
        resp = b.app.test_client().get("/guide")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(d.CARD_INPUTS_MARK, html)
        self.assertIn("工作约束：", html)
        resp.close()


class P11FixedPartWhitelist(PrefixCacheBase):
    """P11：固定段占位符白名单——标记前只允许 {project}/{project_goal_hint}/
    {readonly_tool_guide}/{common} 四键（防 {repo_listing}/{repo_map_hint} 等回流固定段）"""

    def test_prefix_placeholders_within_whitelist(self):
        for role in ROLES:
            tmpl = d.role_prompt(role)
            fixed = tmpl.split(d.CARD_INPUTS_MARK, 1)[0]
            used = set(PLACEHOLDER_RE.findall(fixed))
            self.assertTrue(used <= d.FIXED_PART_PLACEHOLDERS,
                            f"{role}: 固定段出现白名单外占位符 {sorted(used - d.FIXED_PART_PLACEHOLDERS)}")


if __name__ == "__main__":
    unittest.main()
