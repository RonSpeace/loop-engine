#!/usr/bin/env python3
"""上下文编译器逐字节回归（context-design 第一期验收）。

冻结夹具 tests/fixtures/context_prompt_baseline.json 记录「搬迁前」build_prompt 的输出：
全部 8 角色 × （前置阶段 / 流程内）× 项目形态（单仓库、多仓库 + goal + repo_map、仅推分支、
自动重启、带 .loop 清单 + AGENTS.md + 项目技能）× （有无 summary、有无 extra）。
编译器改造后 build_prompt（= compile_context(...).prompt）必须与夹具逐字节一致。

隔离：ensure_dev_worktrees / _gen_review_diff / _default_branch 全部替换为纯函数，
WORKTREES_ROOT / ARTIFACTS_ROOT 指向假路径，不建 worktree、不跑 git、不写产物目录；
项目仓库是临时目录（只放 .loop 清单与 AGENTS.md），输出里的临时路径归一成 <BASE>。

有意的提示词改动（改 prompts/*.md、二期行为变更）后重新生成夹具：
    .venv/bin/python tests/test_context_compiler.py --regen
并在提交说明里逐条解释差异。
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "context_prompt_baseline.json"
ROLES = ("analysis", "proposal", "tech", "tech_review", "dev", "code_review", "accept", "merge_resolve")
FAKE_ROOT = Path("/__ctx__")


def _write_manifest_repo(root):
    """带 .loop 清单的假仓库：命令 + 测试实例 + 按角色指引 + 项目技能 + AGENTS.md。"""
    (root / ".loop" / "guides").mkdir(parents=True, exist_ok=True)
    (root / ".loop" / "skills").mkdir(parents=True, exist_ok=True)
    (root / ".loop" / "guides" / "inst.md").write_text("起实例用 scripts/x.sh {port}\n", encoding="utf-8")
    (root / ".loop" / "guides" / "all.md").write_text("全部角色可见的指引。\n", encoding="utf-8")
    (root / ".loop" / "skills" / "proj-skill.md").write_text("项目技能正文 {花括号}。\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# 规范\n", encoding="utf-8")
    (root / ".loop" / "project.json").write_text(json.dumps({
        "version": 1,
        "commands": {"test": "pytest -q", "lint": "ruff ."},
        "instance": {"start": "x.sh start <端口>", "stop": "x.sh stop <端口>", "port_range": [9000, 9010]},
        "guides": {"inst": {"file": ".loop/guides/inst.md", "roles": ["dev", "accept"]},
                   "all": {"file": ".loop/guides/all.md"}},
        "skills": {"dev": ["proj-skill"], "code_review": ["proj-skill", "missing-skill"]},
    }, ensure_ascii=False), encoding="utf-8")


class CaseEnv:
    """把 dispatcher 钉进确定性环境；用作上下文管理器。"""

    def __enter__(self):
        self.base = Path(tempfile.mkdtemp(prefix="ctx-baseline-"))
        r1 = self.base / "repo-one"
        r2 = self.base / "repo-two"
        rm = self.base / "repo-man"
        for r in (r1, r2):
            r.mkdir(parents=True)
        rm.mkdir(parents=True)
        _write_manifest_repo(rm)
        base_cfg = {"confirm": False, "company": False, "repo_map": "", "restart": None,
                    "auto_restart_after_merge": False, "base_branch": None, "goal": ""}
        self.projects = {
            "plain": dict(base_cfg, paths=[str(r1)]),
            "multi": dict(base_cfg, paths=[str(r1), str(r2)], goal="让看板更好用",
                          repo_map="/docs/repo-map.md"),
            "bonly": dict(base_cfg, paths=[str(r1)], company=True),
            "restart": dict(base_cfg, paths=[str(r1)], restart={"cmd": "./start.sh"}),
            "both": dict(base_cfg, paths=[str(r1)], company=True, restart={"cmd": "./start.sh"}),
            "manifest": dict(base_cfg, paths=[str(rm)]),
        }

        def _ensure(req):
            paths = self.projects[req["project"]]["paths"]
            return (d.dev_workdir_for(req),
                    {Path(p).name: str(d.dev_worktree_path(req, p)) for p in paths})

        self._patches = [
            mock.patch.object(d, "PROJECTS", self.projects),
            mock.patch.object(d, "WORKTREES_ROOT", FAKE_ROOT / "wt"),
            mock.patch.object(d, "ARTIFACTS_ROOT", FAKE_ROOT / "art"),
            mock.patch.object(d, "save_state", lambda: None),
            mock.patch.object(d, "ensure_dev_worktrees", _ensure),
            mock.patch.object(d, "_gen_review_diff", lambda req: None),
            mock.patch.object(d, "_default_branch", lambda p, cfg: ("master", "abc123")),
        ]
        for p in self._patches:
            p.start()
        self._state = d.state
        d.state = {"merge_resolve": {}, "worktrees": {}, "sessions": {}, "req_repos": {}}
        return self

    def __exit__(self, *exc):
        d.state = self._state
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def norm(self, text):
        return text.replace(str(self.base), "<BASE>")


def _heavy_nodes():
    return [dict(n) for n in d.flow_by_id("heavy")["nodes"]]


def _req(project, rid, *, in_flow_index=None, summary=None, notes="要点：做 X\nrepos: repo-one"):
    r = {"id": rid, "project": project, "title": f"卡{rid} 标题 {{含花括号}}", "notes": notes,
         "created_at": "2026-10-15 10:00:00"}
    if summary:
        r["summary"] = summary
    if in_flow_index is not None:
        r.update(flow="heavy", nodes_json=json.dumps(_heavy_nodes(), ensure_ascii=False),
                 node_index=in_flow_index, mode="auto")
    return r


def _flow_index_for(role):
    roles = [n.get("role") for n in _heavy_nodes()]
    return roles.index(role) if role in roles else roles.index("dev")


def iter_cases():
    """(case_name, req, role, extra, merge_failed)。"""
    rid = 500
    for project in ("plain", "multi", "bonly", "restart", "both", "manifest"):
        for role in ROLES:
            for stage in ("pre", "flow"):
                for variant in ("base", "summary_extra"):
                    if variant == "summary_extra" and project not in ("plain", "manifest"):
                        continue
                    rid += 1
                    summary = ("[auto] 代码审查打回：缺测试\n[auto] 第二次：补齐" if variant != "base" else None)
                    extra = "上次尝试失败原因：超时。先自查" if variant != "base" else ""
                    req = _req(project, rid, summary=summary,
                               in_flow_index=(_flow_index_for(role) if stage == "flow" else None))
                    yield f"{project}/{role}/{stage}/{variant}", req, role, extra


def capture():
    out = {}
    with CaseEnv() as env:
        for name, req, role, extra in iter_cases():
            d.state["merge_resolve"][str(req["id"])] = {"failed": [env.projects[req["project"]]["paths"][0]]}
            out[name] = env.norm(d.build_prompt(req, role, extra))
    return out


class ContextCompilerBaseline(unittest.TestCase):
    """build_prompt（编译器薄壳）输出与冻结夹具逐字节一致。"""

    def test_prompts_byte_identical_to_fixture(self):
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        got = capture()
        self.assertEqual(sorted(got), sorted(expected), "用例集合与夹具不一致（需 --regen 并说明）")
        for name in sorted(expected):
            self.assertEqual(got[name], expected[name], f"{name}: 提示词与夹具不一致")


if __name__ == "__main__":
    if "--regen" in sys.argv:
        data = capture()
        FIXTURE.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                           encoding="utf-8")
        print(f"已写入 {FIXTURE}（{len(data)} 个用例）")
    else:
        unittest.main()
