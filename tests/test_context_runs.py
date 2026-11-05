#!/usr/bin/env python3
"""上下文编译器清单 + 执行档案 + 看板 context/prompts/preview 接口。

- compile_context：清单分层/来源/警告；dry 不建 worktree；正文 = build_prompt。
- run_agent：新会话 / 续接轮写 runs/<run_key>/manifest.json + prompt.txt，运行记录带 manifest_path/hash。
- 看板：/api/requirements/<id>/context/runs[...]、/api/context/preview、/api/prompts[...]（临时库 + 临时产物根）。
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import dispatcher as d  # noqa: E402
import prompt_store as ps  # noqa: E402
from dispatcher_modules import context as ctxmod  # noqa: E402
from tests.test_context_compiler import CaseEnv, _req  # noqa: E402


class CompileManifest(unittest.TestCase):
    def test_items_layers_and_warnings(self):
        with CaseEnv() as env:
            req = _req("manifest", 901, in_flow_index=3, summary="[auto] 打回：缺测试")
            c = d.compile_context(req, "code_review", "上次失败：超时")
            self.assertEqual(c.prompt, d.build_prompt(req, "code_review", "上次失败：超时"))
            self.assertEqual(c.prefix, "".join(c.parts[:3]))
            ids = [i["id"] for i in c.items]
            for want in ("system/common", "role/code_review", "system/readonly-tools",
                         "project:manifest/manifest", "guide:repo-man/all", "skill:proj-skill",
                         "card:title-notes", "card:summary", "card:stage-input", "card:extra",
                         "runtime:card-frame"):
                self.assertIn(want, ids)
            layers = {i["id"]: i["layer"] for i in c.items}
            self.assertEqual(layers["system/common"], "L0")
            self.assertEqual(layers["role/code_review"], "L1")
            self.assertEqual(layers["guide:repo-man/all"], "L2")
            self.assertEqual(layers["skill:proj-skill"], "L4")
            self.assertEqual(layers["card:summary"], "L6")
            self.assertNotIn("guide:repo-man/inst", ids, "inst 指引只给 dev/accept")
            self.assertEqual([x["id"] for x in c.skipped], ["skill:missing-skill"])
            self.assertTrue(c.skipped[0]["why"].startswith("未找到"))
            self.assertEqual([w["code"] for w in c.warnings], ["missing_skill"])
            for it in c.items:
                self.assertEqual(len(it["hash"]), 12)
                self.assertIn(it["origin"], ("default", "override", "project", "engine", "card", "runtime"))
            # 卡片数据不进前缀
            self.assertNotIn("打回：缺测试", c.prefix)

    def test_fragments_and_precontract_items(self):
        with CaseEnv():
            c = d.compile_context(_req("both", 902), "dev")
            ids = [i["id"] for i in c.items]
            self.assertIn("fragment/branch-only-dev", ids)
            self.assertIn("fragment/auto-restart", ids)
            self.assertIn("system/pre-contract", ids)   # 前置阶段卡
            self.assertIn("system/project-missing", ids)
            c2 = d.compile_context(_req("plain", 903, in_flow_index=2), "dev")
            self.assertNotIn("system/pre-contract", [i["id"] for i in c2.items])

    def test_dry_does_not_create_worktree(self):
        with CaseEnv() as env:
            req = _req("multi", 904, in_flow_index=3)
            real = d.compile_context(req, "code_review").prompt
            with mock.patch.object(d, "ensure_dev_worktrees", side_effect=AssertionError("dry 不得建 worktree")), \
                    mock.patch.object(d, "_gen_review_diff", side_effect=AssertionError("dry 不得写 diff")):
                c = d.compile_context(req, "code_review", dry=True)
            self.assertEqual(c.prompt, real)
            self.assertIs(d.last_compiled(904, "code_review", pop=False).prompt, real, "dry 不覆盖最近编译结果")

    def test_stale_override_warning(self):
        local = Path(tempfile.mkdtemp(prefix="ctx-local-"))
        try:
            with mock.patch.dict(os.environ, {"REQ_BOARD_PROMPTS_LOCAL": str(local)}), CaseEnv():
                ps.save_override("system/common", ps.default_text("system/common") + "\n本机补充。\n")
                (local / "system" / "common.md.base").write_text("旧默认", encoding="utf-8")
                c = d.compile_context(_req("plain", 905), "analysis")
                self.assertIn("本机补充。", c.prompt)
                self.assertIn("stale_override", [w["code"] for w in c.warnings])
                common = [i for i in c.items if i["id"] == "system/common"][0]
                self.assertEqual(common["origin"], "override")
        finally:
            shutil.rmtree(local, ignore_errors=True)


class FakeProc(types.SimpleNamespace):
    pass


class FakeSession:
    def __init__(self, write_result_on=None, art=None):
        self.session_id = "sess-ctx"
        self.model_name = "m-test"
        self.model_source = "project"
        self.runtime_id = "kimi"
        self.thinking = None
        self.proc = FakeProc(pid=4242, returncode=0)
        self.prompts = []
        self._tool_calls = {}
        self.write_result_on = write_result_on
        self.art = art

    def start(self, workdir, resume_session_id=None):
        return bool(resume_session_id)

    def prompt(self, text, timeout):
        self.prompts.append(text)
        if self.write_result_on == len(self.prompts):
            (self.art / "result.json").write_text(json.dumps({"status": "done", "summary": "ok"}))
        return {}

    def collect_usage(self):
        return None

    def cache_summary(self):
        return None

    def cancel(self):
        pass

    def close(self, kill=False):
        pass

    def _flush_message(self):
        pass


class RunRecords(unittest.TestCase):
    """run_agent 写执行档案：新会话 + 无结果续接轮（followup 指向父档案），运行记录带 run_meta。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="ctx-runs-"))
        self.runs = []
        self.patches = [mock.patch.object(d, n, v) for n, v in {
            "ARTIFACTS_ROOT": self.base / "art", "WORKTREES_ROOT": self.base / "wt",
            "post_event": lambda *a, **k: None, "post_dispatcher_event": lambda *a, **k: None,
            "update_session_info": lambda *a, **k: None, "sync_agent_session": lambda *a, **k: 1,
            "backfill_spawn_session": lambda *a, **k: None, "kimi_version": lambda: None,
            "handle_result": lambda *a, **k: None, "_close_session_row": lambda *a, **k: None,
            "post_model_events": lambda *a, **k: None, "_note_model_fallback": lambda *a, **k: None,
            "_flush_model_degraded_summary": lambda *a, **k: None,
            "post_run_record": lambda *a, **k: self.runs.append(k),
            "role_allowed": lambda req, role: True, "save_state": lambda: None,
        }.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def _run(self, session, req, prompt, resume=False):
        art = d.artifact_dir(req)
        art.mkdir(parents=True, exist_ok=True)
        session.art = art
        with mock.patch.object(d, "ACPSession", lambda *a, **k: session):
            d.run_agent(req, "dev", prompt, 0.0, extra="", resume=resume,
                        resume_session_id=("old-sid" if resume else None))
        return art

    def test_new_and_followup_records(self):
        req = {"id": 951, "project": "req-board", "title": "T", "notes": "N", "created_at": "2026-10-15"}
        compiled = d.Compiled(prompt="PROMPT", items=[{"id": "system/common", "hash": "x" * 12}],
                              prefix="PRE")
        with mock.patch.dict(ctxmod._last_compiled, {("951", "dev"): compiled}, clear=False):
            art = self._run(FakeSession(write_result_on=2), req, "PROMPT")
        runs = sorted((art / "runs").iterdir())
        self.assertEqual(len(runs), 2)
        manifests = [json.loads((r / "manifest.json").read_text()) for r in runs]
        kinds = sorted(m["kind"] for m in manifests)
        self.assertEqual(kinds, ["followup", "new"])
        new = [m for m in manifests if m["kind"] == "new"][0]
        fu = [m for m in manifests if m["kind"] == "followup"][0]
        self.assertEqual(fu["parent_run_key"], new["run_key"])
        sent = (art / "runs" / new["run_key"] / "prompt.txt").read_text()
        self.assertTrue(sent.startswith("PROMPT\n\n【进程安全红线】"))
        self.assertIn("PID=4242", sent)
        self.assertEqual(new["prompt_hash"], ps.short_hash(sent))
        self.assertEqual(new["prefix_hash"], ps.short_hash("PRE"))
        self.assertEqual(new["model"]["id"], "m-test")
        self.assertIsInstance(new["model"]["source"], str)
        self.assertEqual(new["runtime"], "kimi")
        self.assertEqual([i["id"] for i in new["items"]], ["system/common", "system/kill-redline"])
        self.assertEqual(self.runs[0]["run_meta"]["manifest_path"],
                         str(art / "runs" / new["run_key"] / "manifest.json"))
        self.assertEqual(self.runs[0]["run_meta"]["prompt_hash"], new["prompt_hash"])

    def test_resume_record_kind(self):
        req = {"id": 952, "project": "req-board", "title": "T", "notes": "N", "created_at": "2026-10-15"}
        art = self._run(FakeSession(write_result_on=1), req, "PROMPT", resume=True)
        (m,) = [json.loads((r / "manifest.json").read_text()) for r in (art / "runs").iterdir()]
        self.assertEqual(m["kind"], "resume")
        self.assertIn("system/resume-auto", [i["id"] for i in m["items"]])
        self.assertIsNone(m["prefix_hash"], "无编译结果时只记全文")

    def test_records_disabled_when_artifacts_not_isolated(self):
        with mock.patch.object(d, "ARTIFACTS_ROOT", ctxmod.REAL_ARTIFACTS_ROOT):
            self.assertIsNone(d.write_run_record({"id": 1, "project": "p"}, {"run_key": "k"}, "x"))


class BoardContextApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b
        cls.tmp = Path(tempfile.mkdtemp(prefix="ctx-board-"))
        cls._orig = (b.DB_PATH, b.ARTIFACTS_ROOT)
        b.DB_PATH = cls.tmp / "test.db"
        b.ARTIFACTS_ROOT = cls.tmp / "art"
        b.init_db()
        b.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        cls.b.DB_PATH, cls.b.ARTIFACTS_ROOT = cls._orig
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.c = self.b.app.test_client()
        with self.b.get_db() as conn:
            conn.execute("DELETE FROM agent_runs")
            conn.execute("DELETE FROM requirements")
            conn.execute("INSERT INTO requirements (id, project, title, notes, status, created_at, updated_at) "
                         "VALUES (61, 'proj', 'T', 'N', 'doing', '2026-10-15 10:00:00', '2026-10-15 10:00:00')")
            conn.commit()
        self.runs = self.tmp / "art" / "proj" / "req-61" / "runs"
        shutil.rmtree(self.runs, ignore_errors=True)
        self.local = Path(tempfile.mkdtemp(prefix="ctx-board-local-"))
        self._env = mock.patch.dict(os.environ, {"REQ_BOARD_PROMPTS_LOCAL": str(self.local)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.local, ignore_errors=True)

    def _manifest(self, run_key, role="dev", items=(), **kw):
        d_ = self.runs / run_key
        d_.mkdir(parents=True, exist_ok=True)
        m = {"run_key": run_key, "req": 61, "project": "proj", "role": role, "kind": "new",
             "runtime": "kimi", "model": {"id": "m1", "source": "role"}, "created_at": "2026-11-05 10:00:00",
             "prefix_hash": "p" * 12, "prompt_hash": "h" * 12, "chars": 10,
             "items": list(items), "skipped": [], "warnings": [{"code": "truncated"}], **kw}
        (d_ / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        (d_ / "prompt.txt").write_text(f"全文 {run_key}", encoding="utf-8")
        return str(d_ / "manifest.json")

    def test_runs_list_detail_prompt_diff(self):
        k1, k2, k3 = "20261105-100000-dev-aaaa", "20261105-110000-dev-bbbb", "20261105-120000-dev-cccc"
        p1 = self._manifest(k1, items=[{"id": "a", "hash": "1"}, {"id": "b", "hash": "1"}])
        self._manifest(k2, items=[{"id": "a", "hash": "2"}, {"id": "c", "hash": "1"}])
        self._manifest(k3, kind="followup", parent_run_key=k1)
        self._manifest("20261105-090000-code_review-dddd", role="code_review")
        self.c.post("/api/requirements/61/runs", json={"role": "dev", "status": "done", "manifest_path": p1,
                                                       "prompt_hash": "h" * 12, "prefix_hash": "p" * 12})
        runs = self.c.get("/api/requirements/61/context/runs").get_json()["runs"]
        self.assertEqual([r["run_key"] for r in runs][:3], [k3, k2, k1])
        st = {r["run_key"]: r["status"] for r in runs}
        self.assertEqual((st[k1], st[k2], st[k3]), ("done", "running", "done"))
        self.assertEqual(runs[0]["warnings"], 1)
        self.assertEqual(runs[0]["model"], "m1")
        det = self.c.get(f"/api/requirements/61/context/runs/{k2}").get_json()
        self.assertEqual(det["prev_run_key"], k1)
        self.assertEqual(det["diff"], {"changed": ["a"], "added": ["c"], "removed": ["b"]})
        first = self.c.get(f"/api/requirements/61/context/runs/{k1}").get_json()
        self.assertIsNone(first["prev_run_key"])
        r = self.c.get(f"/api/requirements/61/context/runs/{k2}/prompt")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content_type.startswith("text/plain"))
        self.assertEqual(r.get_data(as_text=True), f"全文 {k2}")
        self.assertEqual(self.c.get("/api/requirements/61/context/runs/..%2Fetc").status_code, 404)
        self.assertEqual(self.c.get("/api/requirements/61/context/runs/bad-key").status_code, 400)
        self.assertEqual(self.c.get("/api/requirements/999/context/runs").status_code, 404)
        with self.b.get_db() as conn:
            row = conn.execute("SELECT manifest_path, prompt_hash, prefix_hash FROM agent_runs").fetchone()
        self.assertEqual(tuple(row), (p1, "h" * 12, "p" * 12))

    def test_prompts_api(self):
        items = self.c.get("/api/prompts").get_json()["items"]
        ids = {i["id"] for i in items}
        self.assertIn("system/common", ids)
        self.assertIn("role/dev", ids)
        one = self.c.get("/api/prompts/fragment/auto-restart").get_json()
        self.assertEqual(one["status"], "default")
        self.assertEqual(one["text"], one["default_text"])
        self.assertEqual(self.c.get("/api/prompts/system/nope").status_code, 404)
        bad = self.c.put("/api/prompts/system/kill-redline", json={"text": "删掉红线"})
        self.assertEqual(bad.status_code, 400)
        self.assertTrue(any(e["code"] == "locked_removed" for e in bad.get_json()["errors"]))
        ok = self.c.put("/api/prompts/fragment/auto-restart", json={"text": "新说明"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["status"], "override")
        self.assertEqual((self.local / "fragments" / "auto-restart.md").read_text(encoding="utf-8"), "新说明")
        # 写守卫单一入口：拒绝时 PUT/DELETE 都不落盘
        import board_modules.blueprints.context as ctx_bp
        with mock.patch.object(ctx_bp, "_write_guard", lambda: (__import__("flask").jsonify({"error": "x"}), 403)):
            self.assertEqual(self.c.put("/api/prompts/fragment/auto-restart", json={"text": "x"}).status_code, 403)
            self.assertEqual(self.c.delete("/api/prompts/fragment/auto-restart").status_code, 403)
        self.assertEqual((self.local / "fragments" / "auto-restart.md").read_text(encoding="utf-8"), "新说明")
        de = self.c.delete("/api/prompts/fragment/auto-restart").get_json()
        self.assertEqual((de["status"], de["removed"]), ("default", True))
        self.assertEqual(self.c.put("/api/prompts/project/x/y/manifest", json={"text": "x"}).status_code, 403)

    def test_project_prompt_items_readonly(self):
        repo = self.tmp / "repo-a"
        (repo / ".loop" / "guides").mkdir(parents=True, exist_ok=True)
        (repo / ".loop" / "guides" / "g.md").write_text("指引正文", encoding="utf-8")
        (repo / ".loop" / "project.json").write_text(json.dumps({"guides": {"g": ".loop/guides/g.md"}}))
        with mock.patch.object(self.b, "_projects_config", lambda: {"proj": [str(repo)]}):
            items = self.c.get("/api/prompts").get_json()["items"]
            proj = [i for i in items if i["id"].startswith("project/")]
            self.assertEqual({i["id"] for i in proj}, {"project/proj/repo-a/manifest", "project/proj/repo-a/guide/g"})
            self.assertTrue(all(i["readonly"] for i in proj))
            g = self.c.get("/api/prompts/project/proj/repo-a/guide/g").get_json()
            self.assertEqual(g["text"], "指引正文")

    def test_preview_api_args_and_runner(self):
        seen = []

        def fake(project, role, req):
            seen.append((project, role, req and req["id"]))
            return 200, {"prompt": "P", "items": []}
        with mock.patch.object(self.b, "_context_preview_run", fake):
            self.assertEqual(self.c.get("/api/context/preview?role=dev").status_code, 400)
            r = self.c.get("/api/context/preview?role=dev&req=61")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(seen[-1], ("proj", "dev", 61))
            self.assertEqual(self.c.get("/api/context/preview?role=dev&req=61&project=other").status_code, 400)
            self.assertEqual(self.c.get("/api/context/preview?role=dev&req=404").status_code, 404)
            self.c.get("/api/context/preview?role=dev&project=proj")
            self.assertEqual(seen[-1], ("proj", "dev", None))

    def test_preview_script_real_dry_compile(self):
        """真子进程：占位卡 dry 编译，输出与 manifest 同结构 + prompt；不建 worktree（WORKTREES 不出现目录）。"""
        repo = self.tmp / "preview-repo"
        repo.mkdir(exist_ok=True)
        pj = self.tmp / "projects.json"
        pj.write_text(json.dumps({"proj": {"paths": [str(repo)]}}), encoding="utf-8")
        with mock.patch.object(self.b, "PROJECTS_FILE", pj):
            r = self.c.get("/api/context/preview?project=proj&role=dev")
            body = r.get_json()
            self.assertEqual(r.status_code, 200, body)
            self.assertNotIn("run_key", body)
            self.assertEqual(body["kind"], "preview")
            self.assertTrue(body["placeholder_card"])
            self.assertIn("示例卡片", body["prompt"])
            self.assertIn("role/dev", [i["id"] for i in body["items"]])
            bad = self.c.get("/api/context/preview?project=nope&role=dev")
            self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
