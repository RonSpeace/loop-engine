#!/usr/bin/env python3
"""#4 board 模型调参端点测试（GET/PUT /api/projects/<name>/models + GET model-stats）。

C1~C15：结构 / 404/400 对齐 / 写入与清除 / 幂等 / .bak / 字段保留 / 拒绝矩阵零改动 /
别名池降级与矩阵（不泄 api_key）/ 原子性 / 统计分组与边界 / 并发 / 软链落点。

隔离：临时 DB（沿 tests/test_project_stats.py 先例）+ 临时 projects.json（board/acp_shared
两处常量同钉一份），绝不触真实 data/board.db 与真实 projects.json（软链穿透红线）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_project_models_api.py -q
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import acp_shared as ac  # noqa: E402
import board as b  # noqa: E402


def _pool_patch(items, home="/h", warning="", providers=None, efforts=None):
    """#9：池数据源单一化后，GET（_model_alias_pool_view）与 PUT 校验都经
    _model_alias_pool_entries —— 统一 patch 该函数（providers 可选指定每别名的厂商归属）。
    efforts（#50 起无消费方）：保留形参供旧用例注入 support_efforts，不影响断言。"""
    entries = [{"alias": m, "provider": (providers or {}).get(m, ""),
                "support_efforts": (efforts or {}).get(m)} for m in items]
    return mock.patch.object(b, "_model_alias_pool_entries", return_value=(entries, home, warning))


class ModelApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req4-api-"))
        cls._orig_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.pj = self.tmp / f"projects-{self._testMethodName}.json"
        self._orig_pj = b.PROJECTS_FILE
        self._orig_ac_pj = ac.PROJECTS_FILE
        b.PROJECTS_FILE = self.pj
        ac.PROJECTS_FILE = self.pj
        self.addCleanup(self._restore)
        self._write({"p1": {"paths": ["/p1"], "goal": "G1"},
                     "toy": "/path/short"})

    def _restore(self):
        b.PROJECTS_FILE = self._orig_pj
        ac.PROJECTS_FILE = self._orig_ac_pj

    def _write(self, data):
        self.pj.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _raw(self):
        return json.loads(self.pj.read_text(encoding="utf-8"))

    def _put(self, name, body):
        return self.client.put(f"/api/projects/{name}/models", json=body)

    def _get(self, name):
        return self.client.get(f"/api/projects/{name}/models")

    def _sig(self):
        st = self.pj.stat()
        return (st.st_mtime_ns, st.st_size)

    def _assert_zero_write(self, before_sig, before_bytes):
        self.assertEqual(self.pj.read_bytes(), before_bytes)
        self.assertEqual(self._sig(), before_sig)
        self.assertFalse((self.pj.parent / (self.pj.name + ".bak")).exists())
        self.assertFalse((self.pj.parent / (self.pj.name + ".tmp")).exists())


class GetModelsTest(ModelApiBase):
    def test_c1_get_structure(self):
        with _pool_patch(["m1", "m2"]):
            d = self._get("p1").get_json()
        self.assertEqual(d["project"], "p1")
        roles = d["roles"]
        self.assertEqual([r["role"] for r in roles],
                         sorted(b.ROLE_CAPABILITIES, key=lambda r: (b.role_meta(r, "order") or 99)))
        for r in roles:
            self.assertEqual(set(r), {"role", "display", "order", "model", "source",
                                      "source_label", "override"})
            self.assertTrue(r["model"])
        dev = next(r for r in roles if r["role"] == "dev")
        self.assertEqual(dev["override"], "")
        self.assertNotIn("thinking_levels", d)
        self.assertNotIn("thinking", dev)
        self.assertEqual(d["aliases"], {"items": ["m1", "m2"],
                                        "groups": [{"label": "（未归属）", "items": ["m1", "m2"]}],
                                        "home": "/h", "warning": ""})

    def test_d2_get_groups_matrix(self):
        """#9：厂商分组解析（provider 命中/悬空/缺失/合成别名/default_model），items 与 groups 同源"""
        toml = ('default_model = "prov/a"\n\n[models]\n"prov/a" = {provider = "prov"}\n'
                '"solo/b" = {provider = "ghost"}\n"plain/c" = {}\n\n[providers.prov]\n'
                'type = "openai"\nbase_url = "http://x"\nmodel = "upstream-x"\n'
                'api_key = "SECRETTOKEN"\n')
        home = self.tmp / f"home-{self._testMethodName}"
        home.mkdir()
        (home / "config.toml").write_text(toml, encoding="utf-8")
        with mock.patch.object(b, "session_homes", return_value=[home]):
            resp = self._get("p1")
            d = resp.get_json()
        self.assertEqual(d["aliases"]["items"],
                         ["prov/a", "solo/b", "plain/c", "prov/upstream-x"])
        self.assertEqual([g["label"] for g in d["aliases"]["groups"]], ["prov", "（未归属）"])
        self.assertEqual(d["aliases"]["groups"][0]["items"], ["prov/a", "prov/upstream-x"])
        self.assertEqual(d["aliases"]["groups"][1]["items"], ["solo/b", "plain/c"])
        self.assertNotIn("SECRETTOKEN", resp.get_data(as_text=True))
        self.assertNotIn("api_key", resp.get_data(as_text=True))

    def test_c2_get_errors(self):
        with _pool_patch([], home=None, warning="w"):
            self.assertEqual(self._get("ghost").status_code, 404)
        raw = self._raw()
        self.pj.unlink()
        self.assertEqual(self._get("p1").status_code, 404)
        self.pj.write_text("{bad", encoding="utf-8")
        self.assertEqual(self._get("p1").status_code, 400)
        self.pj.write_text(json.dumps([1, 2]), encoding="utf-8")
        self.assertEqual(self._get("p1").status_code, 400)
        self.pj.write_text(json.dumps(raw), encoding="utf-8")


class PutModelsTest(ModelApiBase):
    def test_c3_put_success(self):
        with _pool_patch(["m1"]):
            resp = self._put("p1", {"role": "dev", "model": "m1"})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["model"], "m1")
        self.assertEqual(d["changed"], True)
        self.assertEqual(d["source"], "project")
        self.assertEqual(d["effective"], "m1")
        # acp_shared 直读同文件 → 四层链层①命中（写后即时生效）
        self.assertEqual(ac.role_model_detail("dev", "p1"), ("m1", "project"))
        self.assertEqual(self._raw()["p1"]["models"]["dev"], "m1")

    def test_c4_put_clear(self):
        self._write({"p1": {"paths": ["/p1"], "models": {"dev": "m0", "accept": "m0"}}})
        with _pool_patch(["m1"]):
            resp = self._put("p1", {"role": "dev", "model": ""})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertTrue(d["changed"])
        self.assertEqual(d["source"], "global")
        self.assertEqual(self._raw()["p1"]["models"], {"accept": "m0"})
        # 清除无覆盖 → changed=false 且不写盘
        before = self._sig()
        with _pool_patch(["m1"]):
            resp2 = self._put("p1", {"role": "dev", "model": "  "})
        self.assertEqual(resp2.get_json()["changed"], False)
        self.assertEqual(self._sig(), before)

    def test_c5_idempotent(self):
        with _pool_patch(["m1"]):
            self.assertEqual(self._put("p1", {"role": "dev", "model": "m1"}).status_code, 200)
            bytes1, sig1 = self.pj.read_bytes(), self._sig()
            bak = self.pj.parent / (self.pj.name + ".bak")
            bak_sig = (bak.stat().st_mtime_ns, bak.stat().st_size)
            resp = self._put("p1", {"role": "dev", "model": "m1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        self.assertEqual(self.pj.read_bytes(), bytes1)
        self.assertEqual(self._sig(), sig1)
        self.assertEqual((bak.stat().st_mtime_ns, bak.stat().st_size), bak_sig)

    def test_c6_bak_semantics(self):
        before = self.pj.read_bytes()
        with _pool_patch(["m1", "m2"]):
            self.assertEqual(self._put("p1", {"role": "dev", "model": "m1"}).status_code, 200)
            bak = self.pj.parent / (self.pj.name + ".bak")
            self.assertEqual(bak.read_bytes(), before)          # 首次写入：.bak = 写前字节
            mid = self.pj.read_bytes()
            self.assertEqual(self._put("p1", {"role": "dev", "model": "m2"}).status_code, 200)
            self.assertEqual(bak.read_bytes(), mid)             # 二次写入：.bak 更新为上一版本

    def test_c7_field_preservation(self):
        data = {"p1": {"paths": ["/p1"], "goal": "G1", "x_custom": {"a": [1, 2], "b": None}},
                "toy": "/path/short", "other": {"paths": ["/o"], "unknown": "keep"}}
        self._write(data)
        with _pool_patch(["m1"]):
            self.assertEqual(self._put("toy", {"role": "dev", "model": "m1"}).status_code, 200)
        raw = self._raw()
        self.assertEqual(raw["p1"], data["p1"])                 # 其它项目逐键原样
        self.assertEqual(raw["other"], data["other"])
        self.assertEqual(raw["toy"]["paths"], ["/path/short"])  # 字符串简写归一
        self.assertEqual(raw["toy"]["models"], {"dev": "m1"})
        self.assertEqual(set(raw), set(data))

    def test_c8_rejection_matrix(self):
        cases = [
            ("未知项目", "ghost", {"role": "dev", "model": "m1"}, 404),
            ("role 非字符串", "p1", {"role": 3, "model": "m1"}, 400),
            ("body 非对象", "p1", ["x"], 400),
            ("未知角色", "p1", {"role": "nonexistent", "model": "m1"}, 400),
            ("model 非字符串", "p1", {"role": "dev", "model": 3}, 400),
            ("model 含空白", "p1", {"role": "dev", "model": "a b"}, 400),
            ("model 含控制字符", "p1", {"role": "dev", "model": "a\x01b"}, 400),
            ("model 超长", "p1", {"role": "dev", "model": "x" * 129}, 400),
            ("未知别名", "p1", {"role": "dev", "model": "nope"}, 400),
        ]
        for name, project, body, want in cases:
            before, before_bytes = self._sig(), self.pj.read_bytes()
            with _pool_patch(["m1"]):
                resp = self._put(project, body)
            self.assertEqual(resp.status_code, want, name)
            self.assertIn("error", resp.get_json(), name)
            self._assert_zero_write(before, before_bytes)
        # 既有文件不可解析 → 400 零写入
        self.pj.write_text("{bad", encoding="utf-8")
        before_bytes = self.pj.read_bytes()
        with _pool_patch(["m1"]):
            resp = self._put("p1", {"role": "dev", "model": "m1"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.pj.read_bytes(), before_bytes)
        # 文件缺失 → 404
        self.pj.unlink()
        with _pool_patch(["m1"]):
            self.assertEqual(self._put("p1", {"role": "dev", "model": "m1"}).status_code, 404)


class ThinkingRetiredTest(ModelApiBase):
    """#50：思考档位统一模型默认——thinking 键下线、既有 thinking 段原样保留。"""

    def test_b3_put_thinking_only_400_zero_write(self):
        for bad in ("high", "", 3, ["high"]):
            before, before_bytes = self._sig(), self.pj.read_bytes()
            resp = self._put("p1", {"role": "dev", "thinking": bad})
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn("已下线", resp.get_json()["error"])
            self._assert_zero_write(before, before_bytes)

    def test_b4_put_model_plus_thinking_400_zero_write(self):
        before, before_bytes = self._sig(), self.pj.read_bytes()
        with _pool_patch(["m1"]):
            resp = self._put("p1", {"role": "dev", "model": "m1", "thinking": "high"})
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(before, before_bytes)   # 不部分写 model

    def test_b5_write_model_preserves_existing_thinking_section(self):
        self._write({"p1": {"paths": ["/p1"], "thinking": {"dev": "high", "accept": "low"}}})
        with _pool_patch(["m1"]):
            resp = self._put("p1", {"role": "dev", "model": "m1"})
        self.assertEqual(resp.status_code, 200)
        raw = self._raw()
        self.assertEqual(raw["p1"]["models"], {"dev": "m1"})
        self.assertEqual(raw["p1"]["thinking"], {"dev": "high", "accept": "low"})   # 原样保留

    def test_b7_put_no_model_400(self):
        before, before_bytes = self._sig(), self.pj.read_bytes()
        resp = self._put("p1", {"role": "dev"})
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(before, before_bytes)


class AliasPoolTest(ModelApiBase):
    def _home(self, text=None):
        home = self.tmp / f"home-{len(list(self.tmp.glob('home-*')))}"
        home.mkdir()
        if text is not None:
            (home / "config.toml").write_text(text, encoding="utf-8")
        return home

    def test_c9_pool_failure_degrades(self):
        home = self._home("{bad toml =")
        with mock.patch.object(b, "session_homes", return_value=[home]):
            items, _h, warning = b._model_alias_pool()
            self.assertEqual(items, [])
            self.assertTrue(warning)
            resp = self._put("p1", {"role": "dev", "model": "任意别名"})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["warning"])
            self.assertEqual(self._raw()["p1"]["models"]["dev"], "任意别名")
            resp_bad = self._put("p1", {"role": "dev", "model": "has space"})
            self.assertEqual(resp_bad.status_code, 400)
            d = self._get("p1").get_json()
            self.assertEqual(d["aliases"]["items"], [])
            self.assertTrue(d["aliases"]["warning"])

    def test_c10_pool_matrix_and_no_key(self):
        toml = ('default_model = "prov/a"\n\n[models]\n"prov/a" = {provider = "prov"}\n'
                '"prov/b" = {provider = "prov"}\n\n[providers.prov]\ntype = "openai"\n'
                'base_url = "http://x"\nmodel = "upstream-x"\napi_key = "SECRETTOKEN"\n')
        home = self._home(toml)
        with mock.patch.object(b, "session_homes", return_value=[home]):
            items, h, warning = b._model_alias_pool()
            self.assertEqual(items, ["prov/a", "prov/b", "prov/upstream-x"])
            self.assertEqual(warning, "")
            self.assertEqual(h, str(home))
            resp = self._get("p1")
            self.assertNotIn("SECRETTOKEN", resp.get_data(as_text=True))
            self.assertNotIn("api_key", resp.get_data(as_text=True))
            # 未知别名拒绝文案不泄 key
            resp_bad = self._put("p1", {"role": "dev", "model": "prov/nope"})
            self.assertEqual(resp_bad.status_code, 400)
            self.assertNotIn("SECRETTOKEN", resp_bad.get_data(as_text=True))
        # 无 config.toml → warning + 放行
        with mock.patch.object(b, "session_homes", return_value=[self._home()]):
            items2, _h2, warn2 = b._model_alias_pool()
            self.assertEqual(items2, [])
            self.assertTrue(warn2)
            self.assertEqual(self._put("p1", {"role": "dev", "model": "whatever"}).status_code, 200)

    def test_c13_incomplete_entry_still_listed(self):
        """#18：缺 max_context_size 的表条目在看板池视图里照常出现（池视图不因该字段变化）。"""
        toml = ('[models]\n"prov/a" = {provider = "prov"}\n'
                '"prov/b" = {provider = "prov", max_context_size = 200000}\n\n'
                '[providers.prov]\ntype = "openai"\nbase_url = "http://x"\n')
        home = self._home(toml)
        with mock.patch.object(b, "session_homes", return_value=[home]):
            items, groups, h, warning = b._model_alias_pool_view()
            self.assertEqual(items, ["prov/a", "prov/b"])
            self.assertEqual([i for g in groups for i in g["items"]], ["prov/a", "prov/b"])
            self.assertEqual((h, warning), (str(home), ""))
            d = self._get("p1").get_json()
            self.assertEqual(d["aliases"]["items"], ["prov/a", "prov/b"])

    def test_c11_atomicity(self):
        before = self.pj.read_bytes()
        with _pool_patch(["m1"]), \
                mock.patch.object(os, "replace", side_effect=OSError("disk boom")):
            resp = self._put("p1", {"role": "dev", "model": "m1"})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.pj.read_bytes(), before)
        self.assertFalse((self.pj.parent / (self.pj.name + ".tmp")).exists())


class ModelStatsTest(ModelApiBase):
    def _req(self, project, status):
        with b.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO requirements (title, project, status, notes) VALUES ('t', ?, ?, '')",
                (project, status))
            conn.commit()
            return cur.lastrowid

    def _session(self, req_id, role, model, sid=None):
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO agent_sessions (req_id, role, model, session_id) VALUES (?, ?, ?, ?)",
                (req_id, role, model, sid))
            conn.commit()

    def _run(self, req_id, sid, ctx, tools):
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO agent_runs (req_id, role, session_id, context_tokens, tool_count) "
                "VALUES (?, 'dev', ?, ?, ?)", (req_id, sid, ctx, tools))
            conn.commit()

    def test_c12_grouping_matrix(self):
        self._write({"p1": {"paths": ["/p1"]}, "c12": {"paths": ["/c12"]},
                     "c12-other": {"paths": ["/c12o"]}})
        r1 = self._req("c12", "done")
        r2 = self._req("c12", "later")
        r3 = self._req("c12", "done")
        other = self._req("c12-other", "done")                 # 跨项目不串
        self._req("c12", "doing")
        # dev × m-a：r1 两个会话（同一 req 去重后状态只计 1）；r2 一个会话
        self._session(r1, "dev", "m-a", "s1")
        self._session(r1, "dev", "m-a", "s2")
        self._session(r2, "dev", "m-a", "s3")
        self._run(r1, "s1", 100, 2)
        self._run(r1, "s2", 50, 1)
        self._run(r2, "s3", 7, 0)
        # dev × m-b：r3；accept × m-a：空 model 归「（未记录）」的对照组
        self._session(r3, "dev", "m-b", "s4")
        self._run(r3, "s4", 1000, 9)
        self._session(r3, "accept", None, "s5")
        self._session(r3, "accept", "  ", "")                  # session_id 为空的存量空行：按行计入
        self._session(other, "dev", "m-a", "s9")               # 其它项目：不计
        d = self.client.get("/api/projects/c12/model-stats").get_json()
        groups = {(g["role"], g["model"]): g for g in d["groups"]}
        self.assertIn(("dev", "m-a"), groups)
        self.assertIn(("dev", "m-b"), groups)
        ga = groups[("dev", "m-a")]
        self.assertEqual(ga["sessions"], 3)
        self.assertEqual(ga["ctx_tokens"], 157)
        self.assertEqual(ga["tool_count"], 3)
        self.assertEqual(ga["statuses"], {"done": 1, "later": 1})     # 去重 req_id
        gb = groups[("dev", "m-b")]
        self.assertEqual((gb["sessions"], gb["ctx_tokens"], gb["tool_count"]), (1, 1000, 9))
        self.assertEqual(gb["statuses"], {"done": 1})
        self.assertIn(("accept", "（未记录）"), groups)
        self.assertEqual(groups[("accept", "（未记录）")]["sessions"], 2)
        self.assertEqual(groups[("accept", "（未记录）")]["ctx_tokens"], 0)   # 空 sid 关联不到 runs
        # 排序：dev(order 5) 在 accept(order 7) 前；同角色 sessions desc
        self.assertEqual([(g["role"]) for g in d["groups"]][:2], ["dev", "dev"])
        self.assertEqual(d["groups"][0]["model"], "m-a")
        # 其它项目数据不串（c12-other 只有自己的 1 个会话）
        d_other = self.client.get("/api/projects/c12-other/model-stats").get_json()
        self.assertEqual([(g0["role"], g0["model"], g0["sessions"]) for g0 in d_other["groups"]],
                         [("dev", "m-a", 1)])

    def test_c13_stats_boundaries(self):
        self.assertEqual(self.client.get("/api/projects/ghost/model-stats").status_code, 404)
        self.pj.unlink()
        self.assertEqual(self.client.get("/api/projects/p1/model-stats").status_code, 404)
        self.pj.write_text("{bad", encoding="utf-8")
        self.assertEqual(self.client.get("/api/projects/p1/model-stats").status_code, 400)
        self._write({"p1": {"paths": ["/p1"]}})
        d = self.client.get("/api/projects/p1/model-stats").get_json()
        self.assertEqual(d["project"], "p1")
        self.assertEqual(d["groups"], [])
        # 无 runs 关联的会话计 0
        rid = self._req("p1", "open")
        self._session(rid, "dev", "m-x", "no-run")
        d = self.client.get("/api/projects/p1/model-stats").get_json()
        self.assertEqual(d["groups"][0]["ctx_tokens"], 0)
        self.assertEqual(d["groups"][0]["tool_count"], 0)
        # DB 异常 → 200 空 groups（fail-safe）
        with mock.patch.object(b, "get_db", side_effect=RuntimeError("db gone")):
            resp = self.client.get("/api/projects/p1/model-stats")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["groups"], [])

    # ---- #42 缓存命中聚合（S1-S5）----

    def _run_at(self, req_id, sid, ctx, tools, ended, cached=None, inp=None, state=None):
        """带 ended_at（epoch）与缓存字段的运行记录（S3 时间窗用）"""
        with b.get_db() as conn:
            conn.execute(
                "INSERT INTO agent_runs (req_id, role, session_id, context_tokens, tool_count,"
                " ended_at, cached_tokens, input_tokens, cache_state) VALUES (?, 'dev', ?, ?, ?, ?, ?, ?, ?)",
                (req_id, sid, ctx, tools, ended, cached, inp, state))
            conn.commit()

    def test_s1_s2_cache_aggregation_and_null_coalesce(self):
        """S1/S2：组内 cached/input 按 session 关联求和；COALESCE(cache_state,'unknown') 口径，
        存量 NULL 行归 unknown 桶；命中率 = Σcached/Σinput（均有正值才非 null）"""
        self._write({"w1": {"paths": ["/w1"]}})   # 类内共享 DB：S 组逐用例独立项目名隔离
        r1 = self._req("w1", "done")
        self._session(r1, "dev", "m-a", "w-sa")
        self._run_at(r1, "w-sa", 10, 1, 1000.0, cached=100, inp=200, state="hit")
        self._run_at(r1, "w-sa", 20, 2, 2000.0, cached=None, inp=None, state="unknown")
        self._run_at(r1, "w-sa", 30, 3, 3000.0, cached=None, inp=None, state=None)   # 存量 NULL 行
        g = self.client.get("/api/projects/w1/model-stats").get_json()["groups"][0]
        self.assertEqual(g["cached_tokens"], 100)
        self.assertEqual(g["input_tokens"], 200)
        self.assertEqual(g["cache_states"], {"hit": 1, "unknown": 2, "na": 0})
        self.assertEqual(g["ctx_tokens"], 60)               # 既有 ctx 求和不受影响
        self.assertAlmostEqual(g["cache_hit_rate"], 0.5)

    def test_s4_hit_rate_null_rules(self):
        """S4（打回复修后口径）：全 unknown → null；显式 0 命中且 Σinput>0 → 出 0%（报 0 也是 hit，
        不再被吞成 null）；cached/input 双零（无真实返回）→ 分母缺失 null"""
        self._write({"w4": {"paths": ["/w4"]}})
        r1 = self._req("w4", "done")
        self._session(r1, "dev", "m-a", "w4-sa")
        self._run_at(r1, "w4-sa", 1, 1, 1000.0, state="unknown")
        g = self.client.get("/api/projects/w4/model-stats").get_json()["groups"][0]
        self.assertIsNone(g["cache_hit_rate"])
        r2 = self._req("w4", "open")
        self._session(r2, "dev", "m-a", "w4-sb")
        self._run_at(r2, "w4-sb", 1, 1, 1000.0, cached=0, inp=500, state="hit")
        g = self.client.get("/api/projects/w4/model-stats").get_json()["groups"][0]
        self.assertAlmostEqual(g["cache_hit_rate"], 0.0)     # 真实零命中出 0%，不出 null

    def test_s3_time_window_filters_runs_by_ended_at(self):
        """S3：since/until 只滤 agent_runs.ended_at（本地日 epoch 边界，含头含尾）；
        窗外 run 不进任何求和；sessions=窗内 runs 去重 session 数；非法日期 400"""
        self._write({"w3": {"paths": ["/w3"]}})
        r1 = self._req("w3", "done")
        self._session(r1, "dev", "m-a", "w3-sa")
        d10 = datetime.datetime(2026, 10, 10).timestamp()
        self._run_at(r1, "w3-sa", 10, 1, d10 + 1, cached=7, inp=10, state="hit")          # 窗内头
        self._run_at(r1, "w3-sa", 20, 2, d10 + 86399, cached=3, inp=10, state="hit")      # 窗内尾
        self._run_at(r1, "w3-sa", 40, 4, d10 + 90000, cached=999, inp=999, state="hit")   # 窗外（10-11）
        day = "2026-10-10"
        g = self.client.get(f"/api/projects/w3/model-stats?since={day}&until={day}").get_json()["groups"][0]
        self.assertEqual(g["ctx_tokens"], 30)               # 窗外 run 不进求和
        self.assertEqual(g["cached_tokens"], 10)
        self.assertEqual(g["input_tokens"], 20)
        self.assertEqual(g["cache_states"], {"hit": 2, "unknown": 0, "na": 0})
        self.assertEqual(g["sessions"], 1)                  # 窗内 runs 关联去重 session 数
        self.assertEqual(g["statuses"], {"done": 1})
        g_all = self.client.get("/api/projects/w3/model-stats").get_json()["groups"][0]
        self.assertEqual(g_all["ctx_tokens"], 70)           # 缺省 = 全时段，现行为不变
        self.assertEqual(g_all["cached_tokens"], 1009)
        self.assertEqual(self.client.get("/api/projects/w3/model-stats?since=bad").status_code, 400)
        self.assertEqual(self.client.get("/api/projects/w3/model-stats?until=2026-13-99").status_code, 400)
        g_u = self.client.get("/api/projects/w3/model-stats?until=2026-10-10").get_json()["groups"][0]
        self.assertEqual(g_u["ctx_tokens"], 30)             # until 单独使用同样只认 ended_at

    def test_s5_existing_fields_unchanged(self):
        """S5：既有字段（sessions/ctx_tokens/tool_count/statuses）与模型标注零变化"""
        self._write({"w5": {"paths": ["/w5"]}})
        r1 = self._req("w5", "open")
        self._session(r1, "dev", "m-a", "w5-sa")
        self._run(r1, "w5-sa", 100, 2)
        g = self.client.get("/api/projects/w5/model-stats").get_json()["groups"][0]
        for k in ("role", "model", "sessions", "ctx_tokens", "tool_count", "statuses"):
            self.assertIn(k, g)
        self.assertEqual((g["sessions"], g["ctx_tokens"], g["tool_count"]), (1, 100, 2))
        self.assertEqual(g["model"], "m-a")

    def test_c14_concurrent_puts(self):
        results = []
        with _pool_patch(["m1", "m2"]):
            def worker(role, model):
                with b.app.test_client() as c:
                    results.append(c.put("/api/projects/p1/models",
                                         json={"role": role, "model": model}).status_code)
            threads = [threading.Thread(target=worker, args=("dev", "m1")),
                       threading.Thread(target=worker, args=("accept", "m2"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(results), [200, 200])
        raw = self._raw()
        self.assertEqual(raw["p1"]["models"], {"dev": "m1", "accept": "m2"})


class SymlinkWriteTest(ModelApiBase):
    def test_c15_symlink_target(self):
        real_dir = self.tmp / "c15-real"
        link_dir = self.tmp / "c15-link"
        real_dir.mkdir()
        link_dir.mkdir()
        real = real_dir / "projects.json"
        link = link_dir / "projects.json"
        initial = {"p1": {"paths": ["/p1"], "goal": "G"}}
        real.write_text(json.dumps(initial), encoding="utf-8")
        os.symlink(real, link)
        b.PROJECTS_FILE = link
        ac.PROJECTS_FILE = link
        with _pool_patch(["m1"]):
            resp = self._put("p1", {"role": "dev", "model": "m1"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.islink(link))                    # 软链保持原样
        self.assertEqual(os.readlink(link), str(real))
        self.assertEqual(json.loads(real.read_text())["p1"]["models"]["dev"], "m1")
        self.assertEqual((real_dir / "projects.json.bak").read_text(),
                         json.dumps(initial))                    # .bak 在真实文件旁
        self.assertEqual(sorted(p.name for p in link_dir.iterdir()), ["projects.json"])
        expected = {"p1": {"paths": ["/p1"], "goal": "G", "models": {"dev": "m1"}}}
        self.assertEqual(json.loads(real.read_text()), expected)


if __name__ == "__main__":
    unittest.main()
