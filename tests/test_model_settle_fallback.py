#!/usr/bin/env python3
"""#11 角色模型未落定修复 + 模型不可用时降级到全局兜底测试。

分组（与 tech.md §6.2 对应）：
- A 别名池与静态校验（resolve_role_model / model_alias_entries_from_config）
- B 落定回读判定（ACPSession._apply_model / _model_settle_of）
- C start() 全链路（new / resume）
- D dispatcher 接线（resolve 取模 / 事件 / state["model_fallback"] / summary 汇总 / spawn 透传）
- E 运行期不可用归因与单次兜底重跑（_model_unavailable_reason / handle_result）
- F 数据模型与展示（迁移两列 / API / 前端 modelMetaText node harness）
- G 日志挂载与落盘（_mount_file_logger 幂等挂 acp_shared）

运行（worktree 内，用 main 工作区的 .venv）：
    <main 工作区>/.venv/bin/python -m unittest tests.test_model_settle_fallback -v

隔离（#420）：临时目录 + 接管 dispatcher/board 的写盘与网络面，绝不写生产 data/board.db、不拉 ACP 进程。
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("REQ_BOARD_API", "http://127.0.0.1:1/api/requirements")

from tests import _baseline  # noqa: E402
from tests._frontend_src import frontend_js, frontend_src  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402
import board as b  # noqa: E402

# 导入期捕获真实引用：部分既有测试文件在运行期替换 dispatcher 模块属性（隔离缺口，非本卡范围），
# 捕获可保证本文件的断言不受其它文件的 stub 泄漏影响
_REAL_CLEAR_RETRIES = d.clear_retries

PROD_LOG = "/tmp/req_board_dispatcher.log"


# ==================== A 组：别名池与静态校验 ====================

class AResolveRoleModel(unittest.TestCase):
    """A1-A14：池命中判定 / 逐层回落 / fail-open / 规则单源与委托边界"""

    POOL = {"glm-5.2", "grok-4.7", "deepseek/v4.1-flash", "deepseek/deepseek-flash"}

    def setUp(self):
        self._pj = ac.PROJECTS_FILE
        self._flow = ac.FLOW_CONFIG
        self._env = {k: os.environ.get(k) for k in ("KIMI_MODEL_ANALYSIS", "KIMI_MODEL_DEV")}
        self.tmp = Path(tempfile.mkdtemp(prefix="req11-pool-"))
        ac.PROJECTS_FILE = self.tmp / "projects.json"

    def tearDown(self):
        ac.PROJECTS_FILE = self._pj
        ac.FLOW_CONFIG = self._flow
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _projects(self, models=None):
        ac.PROJECTS_FILE.write_text(json.dumps({"loop-engine": {"models": models or {}}}))

    def _roles(self, model=None):
        ac.FLOW_CONFIG = {"roles": {"analysis": {"model": model} if model else {}}}

    def _resolve(self, role="analysis", pool=None):
        return ac.resolve_role_model(role, "loop-engine",
                                     pool=self.POOL if pool is None else pool)

    def test_a1_project_hit(self):
        self._projects({"analysis": "glm-5.2"})
        r = self._resolve()
        self.assertEqual((r["model"], r["source"], r["degraded"]), ("glm-5.2", "project", False))

    def test_a2_project_bad_role_ok(self):
        self._projects({"analysis": "bad-model"})
        self._roles("grok-4.7")
        r = self._resolve()
        self.assertEqual((r["model"], r["source"]), ("grok-4.7", "role"))
        self.assertTrue(r["degraded"])
        self.assertIn("bad-model", r["reason"])

    def test_a3_project_bad_role_empty_env_ok(self):
        self._projects({"analysis": "bad-model"})
        self._roles(None)
        os.environ["KIMI_MODEL_ANALYSIS"] = "grok-4.7"
        r = self._resolve()
        self.assertEqual((r["model"], r["source"]), ("grok-4.7", "env"))

    def test_a4_all_bad_falls_to_global(self):
        self._projects({"analysis": "bad-model"})
        self._roles("worse-model")
        os.environ["KIMI_MODEL_ANALYSIS"] = "worst-model"
        r = self._resolve()
        self.assertEqual((r["model"], r["source"], r["degraded"]),
                         (ac.ACP_MODEL, "global", True))
        self.assertEqual(r["reason"], "配置值不在别名池")

    def test_a5_global_also_absent(self):
        self._projects({"analysis": "bad-model"})
        self._roles(None)
        r = self._resolve(pool={"nothing-else"})
        self.assertEqual(r["model"], ac.ACP_MODEL)
        self.assertTrue(r["degraded"])
        self.assertIn("池可能过期", r["reason"])

    def test_a6_pool_unavailable_fail_open(self):
        self._projects({"analysis": "glm-5.2"})
        with mock.patch.object(ac, "model_alias_pool", return_value=(None, None, "无池")):
            r = ac.resolve_role_model("analysis", "loop-engine")
        self.assertEqual((r["model"], r["source"], r["degraded"]), ("glm-5.2", "project", False))

    def test_a7_no_config_uses_global_without_degrade(self):
        """未配置 + 池命中全局值 → 不算降级（默认会话零误报）"""
        self._projects({})
        self._roles(None)
        r = self._resolve()
        self.assertEqual((r["model"], r["source"], r["degraded"]), (ac.ACP_MODEL, "global", False))

    def test_a8_entries_rule_and_no_api_key(self):
        raw = {"default_model": "deepseek/deepseek-flash",
               "models": {"glm-5.2": {"provider": "zhipu"}, "x": {"api_key": "SECRET"}},
               "providers": {"zhipu": {"model": "glm-5-2"}}}
        entries = ac.model_alias_entries_from_config(raw)
        aliases = [e["alias"] for e in entries]
        self.assertEqual(aliases, ["glm-5.2", "x", "deepseek/deepseek-flash", "zhipu/glm-5-2"])
        self.assertEqual(entries[0]["provider"], "zhipu")
        self.assertEqual(entries[3]["provider"], "zhipu")
        self.assertNotIn("SECRET", json.dumps(entries))

    def test_a9_synthetic_alias_in_pool(self):
        self._projects({"analysis": "deepseek/deepseek-flash"})
        r = self._resolve()
        self.assertEqual((r["model"], r["source"], r["degraded"]),
                         ("deepseek/deepseek-flash", "project", False))

    def test_a10_role_model_detail_unchanged(self):
        self._projects({"analysis": "glm-5.2"})
        self.assertEqual(ac.role_model_detail("analysis", "loop-engine"), ("glm-5.2", "project"))
        ac.PROJECTS_FILE.write_text(json.dumps({"loop-engine": {"models": {"analysis": "  "}}}))
        self._roles("grok-4.7")
        self.assertEqual(ac.role_model_detail("analysis", "loop-engine"), ("grok-4.7", "role"))

    def test_a11_projects_hot_reload(self):
        self._projects({"analysis": "glm-5.2"})
        self.assertEqual(self._resolve()["model"], "glm-5.2")
        self._projects({"analysis": "grok-4.7"})
        self.assertEqual(self._resolve()["model"], "grok-4.7")

    def test_a12_env_layer(self):
        self._projects({})
        self._roles(None)
        os.environ["KIMI_MODEL_ANALYSIS"] = "grok-4.7"
        self.assertEqual(self._resolve()["source"], "env")
        os.environ["KIMI_MODEL_ANALYSIS"] = "not-in-pool"
        self.assertEqual((self._resolve()["source"], self._resolve()["degraded"]), ("global", True))

    def test_a13_board_patch_surface_untouched(self):
        """A13：board 的 session_homes / _harness_config_path / 返回三元组与 warning 文案逐字不变"""
        home = self.tmp / "fake-home"
        home.mkdir()
        (home / "config.toml").write_text('[models]\n"glm-5.2" = {}\n')
        with mock.patch.object(b, "session_homes", return_value=[home]):
            self.assertEqual(b._harness_config_path()[1], str(home))
            entries, h, warning = b._model_alias_pool_entries()
            self.assertEqual([e["alias"] for e in entries], ["glm-5.2"])
            self.assertEqual((h, warning), (str(home), ""))
        # 不可用分支的 warning 文案保持原文
        with mock.patch.object(b, "session_homes", return_value=[self.tmp / "nope"]):
            self.assertEqual(b._model_alias_pool_entries(),
                             ([], None, "未找到 config.toml（harness home 无配置），已按格式校验放行"))

    def test_a14_board_and_acp_rules_single_source(self):
        """A14：board 规则段与 acp_shared.model_alias_entries_from_config 对同一 raw 产出相同"""
        import tomllib
        raw = tomllib.loads('[models]\nglm-5.2 = { provider = "zhipu", support_efforts = ["high"] }\n'
                            'default_model = "deepseek/deepseek-flash"\n'
                            '[providers.zhipu]\nmodel = "glm-5-2"\n')
        shared = ac.model_alias_entries_from_config(raw)
        home = self.tmp / "src"
        home.mkdir()
        (home / "config.toml").write_text('[models]\nglm-5.2 = { provider = "zhipu", support_efforts = ["high"] }\n'
                                          'default_model = "deepseek/deepseek-flash"\n'
                                          '[providers.zhipu]\nmodel = "glm-5-2"\n')
        with mock.patch.object(b, "session_homes", return_value=[home]):
            board_entries = b._model_alias_pool_entries()[0]
        self.assertEqual(board_entries, shared)

    def test_a15_pool_reader_fail_open(self):
        """池不可用（无文件/解析失败/空池）→ (None, home, warning)，不抛"""
        with mock.patch.object(ac, "harness_config_path", return_value=(None, None)):
            self.assertIsNone(ac.model_alias_pool()[0])
        p = self.tmp / "bad.toml"
        p.write_text("this is not toml = = =")
        with mock.patch.object(ac, "harness_config_path", return_value=(p, str(self.tmp))):
            aliases, home, warning = ac.model_alias_pool()
            self.assertIsNone(aliases)
            self.assertIn("解析失败", warning)

    # ---- A16-A24（#13）：词表 / 条目富化 / 可用性判定 / 前移回落 / 兼容注入 / 单源锁定 ----

    def test_a16_provider_types_vocab(self):
        """A16：HARNESS_PROVIDER_TYPES 非空、全小写、无重复、HINT 由其派生"""
        vocab = ac.HARNESS_PROVIDER_TYPES
        self.assertTrue(vocab)
        self.assertEqual(len(set(vocab)), len(vocab))
        for t in vocab:
            self.assertEqual(t, t.lower(), t)
            self.assertRegex(t, r"^[a-z0-9_-]+$")
        self.assertEqual(ac.HARNESS_PROVIDER_TYPES_HINT, " / ".join(vocab))

    def test_a17_entry_type_protocol_enrichment(self):
        """A17：条目富化出 type（所属厂商）与 protocol（模型声明）；缺键为 ''，不含密钥"""
        raw = {"models": {"glm-5.2": {"provider": "zhipu", "protocol": "anthropic"},
                          "x": {"api_key": "SECRET"}},
               "providers": {"zhipu": {"type": "anthropic", "model": "glm-5-2"}}}
        entries = ac.model_alias_entries_from_config(raw)
        by = {e["alias"]: e for e in entries}
        self.assertEqual((by["glm-5.2"]["type"], by["glm-5.2"]["protocol"]),
                         ("anthropic", "anthropic"))
        self.assertEqual((by["zhipu/glm-5-2"]["type"], by["zhipu/glm-5-2"]["protocol"]),
                         ("anthropic", ""))
        self.assertEqual((by["x"]["type"], by["x"]["protocol"]), ("", ""))
        self.assertNotIn("SECRET", json.dumps(entries))

    def test_a18_model_alias_usable_matrix(self):
        """A18：protocol 非空即可用；type ∈ 词表可用；大小写敏感拒非法；provider/type 空不可用"""
        u = ac.model_alias_usable
        self.assertEqual(u({"provider": "p", "type": "OpenAI", "protocol": "openai"}), (True, ""))
        for t in ac.HARNESS_PROVIDER_TYPES:
            self.assertEqual(u({"provider": "p", "type": t, "protocol": ""}), (True, ""))
        for bad in ("OpenAI", "OPENAI", "OpenAi"):
            ok, why = u({"provider": "OpenAI", "type": bad, "protocol": ""})
            self.assertFalse(ok)
            self.assertIn(bad, why)
            self.assertIn(ac.HARNESS_PROVIDER_TYPES_HINT, why)
        self.assertFalse(u({"provider": "", "type": "openai", "protocol": ""})[0])
        self.assertFalse(u({"provider": "p", "type": "", "protocol": ""})[0])

    def test_a19_in_pool_unusable_falls_to_global(self):
        """A19：配置值在池但不可用 → 回落全局，reason 带不可用原因"""
        self._projects({"analysis": "gpt-5.6-sol"})
        self._roles(None)
        os.environ.pop("KIMI_MODEL_ANALYSIS", None)
        pool = {"gpt-5.6-sol": {"provider": "OpenAI", "type": "OpenAI", "protocol": "",
                                "usable": False,
                                "reason": ('厂商 OpenAI 的 type "OpenAI" 非法'
                                           f"（合法值：{ac.HARNESS_PROVIDER_TYPES_HINT}）")},
                "deepseek/deepseek-flash": {"provider": "", "type": "", "protocol": "openai",
                                            "usable": True, "reason": ""}}
        r = self._resolve(pool=pool)
        self.assertEqual((r["model"], r["source"], r["degraded"]),
                         (ac.ACP_MODEL, "global", True))
        self.assertIn('不可用（厂商 OpenAI 的 type "OpenAI" 非法（合法值：', r["reason"])

    def test_a20_falls_through_unusable_middle_layer(self):
        """A20：project 不可用 + role 可用 → 取 role，reason 含 project 配置值与「已逐层回落」"""
        self._projects({"analysis": "bad-x"})
        self._roles("ok-role")
        os.environ.pop("KIMI_MODEL_ANALYSIS", None)
        pool = {"bad-x": {"provider": "px", "type": "OpenAI", "protocol": "", "usable": False,
                          "reason": '厂商 px 的 type "OpenAI" 非法'},
                "ok-role": {"provider": "", "type": "", "protocol": "openai", "usable": True,
                            "reason": ""}}
        r = self._resolve(pool=pool)
        self.assertEqual((r["model"], r["source"], r["degraded"]), ("ok-role", "role", True))
        self.assertIn("bad-x", r["reason"])
        self.assertIn("已逐层回落", r["reason"])

    def test_a21_set_pool_membership_all_usable(self):
        """A21：pool 传 set（旧口径）→ 成员一律可用，零误判"""
        self._projects({"analysis": "glm-5.2"})
        self._roles(None)
        r = self._resolve(pool={"glm-5.2"})
        self.assertEqual((r["model"], r["source"], r["degraded"]), ("glm-5.2", "project", False))

    def test_a22_both_layers_unusable_keeps_global(self):
        """A22：配置值与 ACP_MODEL 均不可用 → 仍返回 ACP_MODEL，reason 含「亦不可用」，不抛"""
        self._projects({"analysis": "cfg-bad"})
        self._roles(None)
        os.environ.pop("KIMI_MODEL_ANALYSIS", None)
        pool = {"cfg-bad": {"usable": False, "reason": "X"},
                ac.ACP_MODEL: {"usable": False, "reason": "Y"}}
        r = self._resolve(pool=pool)
        self.assertEqual(r["model"], ac.ACP_MODEL)
        self.assertTrue(r["degraded"])
        self.assertIn("亦不可用", r["reason"])

    def test_a23_not_in_pool_wording_unchanged(self):
        """A23：纯「不在池」文案逐字不回归（沿用 A4/A5 的 pool 形态）"""
        self._projects({"analysis": "bad-model"})
        self._roles("worse-model")
        os.environ["KIMI_MODEL_ANALYSIS"] = "worst-model"
        r = self._resolve()
        self.assertEqual(r["reason"], "配置值不在别名池")
        r2 = self._resolve(pool={"nothing-else"})
        self.assertIn("配置值与全局兜底值均不在别名池", r2["reason"])
        self.assertIn("池可能过期", r2["reason"])

    def test_a24_board_and_acp_single_source_keywise(self):
        """A24：board _model_alias_pool_entries 与 acp_shared 对同一 raw 逐键相等（含 type/protocol）"""
        import tomllib
        cfg = ('[models]\n'
               '"glm-5.2" = { provider = "zhipu", protocol = "openai", support_efforts = ["high"] }\n'
               'default_model = "deepseek/deepseek-flash"\n'
               '[providers.zhipu]\n'
               'type = "anthropic"\n'
               'model = "glm-5-2"\n')
        shared = ac.model_alias_entries_from_config(tomllib.loads(cfg))
        home = self.tmp / "src24"
        home.mkdir()
        (home / "config.toml").write_text(cfg)
        with mock.patch.object(b, "session_homes", return_value=[home]):
            entries, h, warning = b._model_alias_pool_entries()
        self.assertEqual((h, warning), (str(home), ""))
        self.assertEqual(entries, shared)
        by = {e["alias"]: e for e in entries}
        self.assertEqual((by["glm-5.2"]["type"], by["glm-5.2"]["protocol"]),
                         ("anthropic", "openai"))
        # 三元组形态与 warning 文案逐字不变（缺文件分支）
        with mock.patch.object(b, "session_homes", return_value=[self.tmp / "nope24"]):
            self.assertEqual(b._model_alias_pool_entries(),
                             ([], None, "未找到 config.toml（harness home 无配置），已按格式校验放行"))

    # ---- A17b/A18b/A25（#18）：max_context_size 富化 / 必填判定 / 前置回落 ----

    def test_a17b_context_size_enrichment(self):
        """A17b（#18）：表条目富化 has_model_entry 与 max_context_size（缺失/非整数 → None）；
        合成别名与非表条目仍为 False/None；序列化不含密钥。"""
        raw = {"default_model": "spawn",
               "models": {"spawn": {"provider": "zhipu"},
                          "n": {"provider": "zhipu", "max_context_size": "128000", "api_key": "SECRET"},
                          "b": {"provider": "zhipu", "max_context_size": True},
                          "ok": {"provider": "zhipu", "max_context_size": 200000}},
               "providers": {"zhipu": {"type": "anthropic", "model": "u"}}}
        by = {e["alias"]: e for e in ac.model_alias_entries_from_config(raw)}
        self.assertEqual((by["spawn"]["has_model_entry"], by["spawn"]["max_context_size"]), (True, None))
        self.assertEqual((by["n"]["has_model_entry"], by["n"]["max_context_size"]), (True, None))
        self.assertEqual((by["b"]["has_model_entry"], by["b"]["max_context_size"]), (True, None))
        self.assertEqual((by["ok"]["has_model_entry"], by["ok"]["max_context_size"]), (True, 200000))
        # 合成别名（providers.<p>.model 拼出的 p/upstream）无表语义
        self.assertEqual((by["zhipu/u"]["has_model_entry"], by["zhipu/u"]["max_context_size"]),
                         (False, None))
        # default_model 指向非表条目 → 同样 False/None
        raw2 = {"default_model": "ghost", "models": {}, "providers": {}}
        by2 = {e["alias"]: e for e in ac.model_alias_entries_from_config(raw2)}
        self.assertEqual((by2["ghost"]["has_model_entry"], by2["ghost"]["max_context_size"]), (False, None))
        self.assertNotIn("SECRET", json.dumps(by))

    def test_a18b_usable_matrix_context_size(self):
        """A18b（#18）：表条目缺/非法 max_context_size → 不可用（置前于 protocol/type 判定）；
        合法整数（protocol 路与 type 路）→ 可用；合成别名与无该键的旧调用零回归。"""
        u = ac.model_alias_usable
        ok, why = u({"provider": "zhipu", "type": "anthropic", "protocol": "openai",
                     "has_model_entry": True, "max_context_size": None})
        self.assertFalse(ok)
        self.assertIn("缺少必填字段 max_context_size", why)
        for bad in (0, -1):
            ok2, why2 = u({"provider": "zhipu", "type": "anthropic", "protocol": "",
                           "has_model_entry": True, "max_context_size": bad})
            self.assertFalse(ok2, bad)
            self.assertIn("正整数", why2)
        self.assertEqual(u({"provider": "zhipu", "type": "anthropic", "protocol": "",
                            "has_model_entry": True, "max_context_size": 1}), (True, ""))
        self.assertEqual(u({"provider": "zhipu", "type": "anthropic", "protocol": "openai",
                            "has_model_entry": True, "max_context_size": 200000}), (True, ""))
        # 纯函数矩阵（含非整数文案，富化层已把非整数收敛为 None）
        self.assertEqual(ac.context_size_problem(None), "缺少必填字段 max_context_size")
        self.assertEqual(ac.context_size_problem(True), "max_context_size 需为正整数（当前非整数：bool）")
        self.assertEqual(ac.context_size_problem("128000"),
                         "max_context_size 需为正整数（当前非整数：str）")
        self.assertEqual(ac.context_size_problem(0), "max_context_size 需为正整数（≥1，当前为 0）")
        self.assertEqual(ac.context_size_problem(1), "")
        # 旧行为零回归：合成别名（has_model_entry False）与无该键的旧调用不因该字段判不可用
        self.assertEqual(u({"provider": "zhipu", "type": "anthropic", "protocol": "",
                            "has_model_entry": False, "max_context_size": None}), (True, ""))
        self.assertEqual(u({"provider": "zhipu", "type": "anthropic", "protocol": ""}), (True, ""))

    def test_a25_missing_context_size_falls_back(self):
        """A25（#18）：缺 max_context_size 的表条目 → 前置判不可用并回落全局（reason 带原因）；
        补全后同一路径不再回落（字段完整 → 允许进入 session/set_config_option）。"""
        raw = {"default_model": ac.ACP_MODEL,
               "models": {"spawn": {"provider": "zhipu"},
                          ac.ACP_MODEL: {"provider": "zhipu", "max_context_size": 262144}},
               "providers": {"zhipu": {"type": "anthropic", "model": "u"}}}

        def _pool(cfg):
            out = {}
            for e in ac.model_alias_entries_from_config(cfg):
                usable, reason = ac.model_alias_usable(e)
                out[e["alias"]] = {"provider": e["provider"], "type": e["type"],
                                   "protocol": e["protocol"], "usable": usable, "reason": reason}
            return out

        self._projects({"analysis": "spawn"})
        self._roles(None)
        os.environ.pop("KIMI_MODEL_ANALYSIS", None)
        r = self._resolve(pool=_pool(raw))
        self.assertEqual((r["model"], r["source"], r["degraded"]), (ac.ACP_MODEL, "global", True))
        self.assertIn("缺少必填字段 max_context_size", r["reason"])
        self.assertIn("已逐层回落", r["reason"])
        # 补全 → 条目可用，不再回落（model = 配置值、reason 为空）
        raw["models"]["spawn"]["max_context_size"] = 200000
        r2 = self._resolve(pool=_pool(raw))
        self.assertEqual((r2["model"], r2["source"], r2["degraded"]), ("spawn", "project", False))
        self.assertIsNone(r2["reason"])
        # 0 / 负数同样前置判不可用并回落
        for bad in (0, -1):
            raw["models"]["spawn"]["max_context_size"] = bad
            r3 = self._resolve(pool=_pool(raw))
            self.assertEqual((r3["model"], r3["source"]), (ac.ACP_MODEL, "global"), bad)
            self.assertIn("正整数", r3["reason"])
            self.assertIn("已逐层回落", r3["reason"])


# ==================== B 组：落定回读判定 ====================

def _model_opt(current, options, oid="model"):
    return {"id": oid, "category": "model", "currentValue": current, "options": options}


class BSettleJudge(unittest.TestCase):
    """B1-B12：_model_settle_of / _apply_model 的判定与留痕"""

    R = "glm-5.2"

    def _sess(self, model=None, result=None, raises=None):
        s = ac.ACPSession.__new__(ac.ACPSession)
        s.req_id = 99001
        s.session_id = "s1"
        s.role = "dev"
        s.model = model or self.R
        s.model_requested = s.model
        s.model_source = None
        s.model_settle = None
        s.model_effective = None
        s.model_name = None
        s.events = []

        def _req(method, params, timeout=30):
            if raises:
                raise RuntimeError(raises)
            return result
        s.request = _req
        s._apply_config = lambda res: ac.ACPSession._apply_config(s, res)
        s._emit = lambda kind, content, session_id=None: s.events.append((kind, content))
        return s

    def _apply(self, result, raises=None, model=None):
        s = self._sess(model=model, result=result, raises=raises)
        with mock.patch.object(ac.log, "warning") as w:
            ac.ACPSession._apply_model(s)
        return s, [str(c.args[0]) for c in w.call_args_list]

    def test_b1_settled_by_value(self):
        s, warns = self._apply({"configOptions": [_model_opt(self.R, [{"value": self.R, "name": "GLM"}])]})
        self.assertEqual(s.model_settle, "settled")
        self.assertEqual(s.events, [])
        self.assertEqual(warns, [])

    def test_b2_settled_by_name(self):
        s, _ = self._apply({"configOptions": [_model_opt("GLM 5.2", [{"value": self.R, "name": "GLM 5.2"}])]})
        self.assertEqual(s.model_settle, "settled")

    def test_b3_echo_settled_even_if_not_listed(self):
        """B3（P4 优先序）：options 不含 R 但 currentValue == R（原样回显）→ settled（零误报）"""
        s, warns = self._apply({"configOptions": [_model_opt(self.R, [{"value": "other", "name": "Other"}])]})
        self.assertEqual(s.model_settle, "settled")
        self.assertEqual(warns, [])

    def test_b4_mismatch_when_cur_hits_other_option(self):
        """B4（P4）：options 不含 R 且 currentValue 命中其它项 → mismatch（而非 not_in_options）"""
        s, warns = self._apply({"configOptions": [_model_opt(
            "other", [{"value": "other", "name": "Other"}])]})
        self.assertEqual(s.model_settle, "mismatch")
        self.assertTrue(warns)

    def test_b5_mismatch_listed(self):
        s, warns = self._apply({"configOptions": [_model_opt(
            "other", [{"value": self.R, "name": "GLM"}, {"value": "other", "name": "Other"}])]})
        self.assertEqual(s.model_settle, "mismatch")
        self.assertTrue(any(self.R in m for m in warns))
        self.assertTrue(s.events and "未落定" in s.events[0][1])

    def test_b6_unverified_synthetic_shape(self):
        """B6：options 含 R，回读为合成形态（不属于任何 option）→ unverified，不判 mismatch"""
        s, warns = self._apply({"configOptions": [_model_opt(
            "deepseek/deepseek-flash", [{"value": self.R, "name": "GLM"}])]})
        self.assertEqual(s.model_settle, "unverified")
        self.assertTrue(warns)

    def test_b7_not_in_options(self):
        s, warns = self._apply({"configOptions": [_model_opt(
            "zzz", [{"value": "yyy", "name": "Y"}])]})
        self.assertEqual(s.model_settle, "not_in_options")
        self.assertTrue(warns)

    def test_b8_unreadable(self):
        s, warns = self._apply({"configOptions": [{"id": "thinking", "currentValue": "high"}]})
        self.assertEqual(s.model_settle, "unreadable")
        self.assertTrue(warns)

    def test_b9_rejected_does_not_raise(self):
        s, warns = self._apply(None, raises="boom")
        self.assertEqual(s.model_settle, "rejected")
        self.assertTrue(warns)
        self.assertTrue(s.events and "rejected" in s.events[0][1])

    def test_b10_request_value_not_overwritten(self):
        s, _ = self._apply({"configOptions": [_model_opt("GLM 5.2", [{"value": self.R, "name": "GLM 5.2"}])]})
        self.assertEqual(s.model, self.R)
        self.assertEqual(s.model_effective, "GLM 5.2")

    def test_b11_unverified_logged_once(self):
        opt = {"configOptions": [_model_opt("deepseek/deepseek-flash", [{"value": self.R, "name": "GLM"}])]}
        s = self._sess(result=opt)
        with mock.patch.object(ac.log, "warning") as w:
            ac.ACPSession._apply_model(s)
            ac.ACPSession._apply_model(s)
        self.assertEqual(w.call_count, 1)

    def test_b12_default_model_zero_noise(self):
        """B12：R == ACP_MODEL 且回读为合成形态不属于任何 option → 不误报（unverified 之外零事件）"""
        s, warns = self._apply(
            {"configOptions": [_model_opt(ac.ACP_MODEL, [{"value": ac.ACP_MODEL, "name": "D"}])]},
            model=ac.ACP_MODEL)
        self.assertEqual(s.model_settle, "settled")
        self.assertEqual((warns, s.events), ([], []))

    def test_b13_model_error_recorded(self):
        """B13：_apply_model 异常分支写 model_error（≤200，含 harness 原文关键串）；settled 置 None"""
        long_msg = "must declare a wire protocol for provider: " + "x" * 400
        s, _ = self._apply(None, raises=long_msg)
        self.assertEqual(s.model_settle, "rejected")
        self.assertIsNotNone(s.model_error)
        self.assertLessEqual(len(s.model_error), 200)
        self.assertIn("must declare a wire protocol", s.model_error)
        s2, _ = self._apply({"configOptions": [_model_opt(self.R, [{"value": self.R, "name": "GLM"}])]})
        self.assertEqual(s2.model_settle, "settled")
        self.assertIsNone(s2.model_error)


# ==================== C 组：start() 全链路 ====================

class CStartChain(unittest.TestCase):
    """C1-C3：new / resume 两路径均走 _apply_model 回读；未落定不抛"""

    def _sess(self, cfg_model):
        s = ac.ACPSession.__new__(ac.ACPSession)
        s.req_id = 99002
        s.role = "dev"
        s.session_id = None
        s.model = "glm-5.2"
        s.model_requested = "glm-5.2"
        s.model_source = None
        s.model_settle = None
        s.model_effective = None
        s.model_name = None
        s.thinking = None
        s._extra_mcp_servers = []
        s.events = []
        s._emit = lambda kind, content, session_id=None: s.events.append((kind, content))
        s._readonly_mcp_servers = lambda workdir: []

        def _req(method, params, timeout=120):
            if method == "initialize":
                return {}
            if method == "session/new":
                return {"sessionId": "sid-new"}
            if method == "session/resume":
                return {"sessionId": params["sessionId"]}
            if method == "session/set_config_option":
                cid = params["configId"]
                if cid == "model":
                    return {"configOptions": [_model_opt(
                        cfg_model, [{"value": s.model, "name": "GLM"},
                                    {"value": "other-model", "name": "Other"}])]}
                return {"configOptions": [{"id": "thinking", "currentValue": "high"}]}
            raise AssertionError(method)
        s.request = _req
        return s

    def test_c1_new_path(self):
        s = self._sess("glm-5.2")
        self.assertFalse(ac.ACPSession.start(s, Path("/tmp")))
        self.assertEqual(s.model_settle, "settled")

    def test_c2_resume_path_also_reads_back(self):
        """C2（P8）：兜底重跑走 resume，模型重设与回读在这条路径上同样发生"""
        s = self._sess("glm-5.2")
        self.assertTrue(ac.ACPSession.start(s, Path("/tmp"), resume_session_id="old-sid"))
        self.assertEqual(s.model_settle, "settled")

    def test_c3_unsettled_does_not_raise(self):
        s = self._sess("other-model")
        self.assertFalse(ac.ACPSession.start(s, Path("/tmp")))
        self.assertEqual(s.model_settle, "mismatch")
        self.assertTrue(s.events)


# ==================== D 组：dispatcher 接线 ====================

def _req(req_id=11):
    return {"id": req_id, "project": "req-board", "title": "t", "status": "open",
            "mode": "semi", "phase": None, "notes": ""}


class FakeSession:
    """run_agent 用会话桩：不起子进程，记录构造参数并暴露 run_agent 需要的面"""

    settle = "settled"
    effective = None
    last_kwargs = None
    model_error = None    # #13：落定失败时 harness 原始报错摘要（D13 用例注入）

    def __init__(self, *a, **kw):
        self.args, self.kwargs = a, kw
        FakeSession.last_kwargs = kw
        self.req_id = a[0] if a else kw.get("req_id")
        self.model = kw.get("model") or ac.ACP_MODEL
        self.model_requested = self.model
        self.model_source = None
        self.model_settle = type(self).settle
        self.model_effective = type(self).effective or self.model
        self.thinking = kw.get("thinking")
        self.model_name = "display:" + str(self.model)
        self.session_id = "sess-fake"
        self.proc = types.SimpleNamespace(pid=4242, returncode=0)
        self.activity_sink = kw.get("activity_sink")

    def start(self, workdir, resume_session_id=None):
        return False

    def prompt(self, text, timeout=None):
        pass

    def collect_usage(self):
        return None

    def _flush_message(self):
        pass

    def close(self, kill=False):
        pass

    def cancel(self):
        pass


class DRunAgentWiring(unittest.TestCase):
    """D1/D2/D3/D5/D7/D9：run_agent 取模走 resolve、事件、state 记录、summary 汇总、spawn 透传"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req11-wiring-"))
        self._orig = {n: getattr(d, n) for n in (
            "state", "save_state", "artifact_dir", "dev_workdir_for", "role_allowed",
            "readonly_tool_roots", "dev_write_roots", "kimi_version", "update_session_info",
            "sync_agent_session", "post_event", "backfill_spawn_session", "post_dispatcher_event",
            "append_summary", "result_state", "dev_branch", "_close_session_row", "post_run_record",
            "handle_result", "_swept", "ACPSession", "spawn_agent", "build_prompt", "role_for",
            "_note_review_model", "find_resume_sid", "threading", "resolve_role_model", "set_req")}
        d.state = {"retries": {}, "sessions": {}, "model_fallback": {}, "worktrees": {}}
        d.save_state = lambda: None
        d.artifact_dir = lambda req: self.tmp
        d.dev_workdir_for = lambda req: self.tmp
        d.role_allowed = lambda req, role: True
        d.readonly_tool_roots = lambda *a: []
        d.dev_write_roots = lambda req: []
        d.kimi_version = lambda: None
        d.update_session_info = lambda *a, **k: None
        d.post_event = lambda *a, **k: None
        d.backfill_spawn_session = lambda *a, **k: None
        d.result_state = lambda req: ("ok", "")
        d.dev_branch = lambda req: "f-test"
        d._close_session_row = lambda *a, **k: None
        d.post_run_record = lambda *a, **k: None
        d.handle_result = lambda *a, **k: None
        d._swept = lambda row_id: False
        d.ACPSession = FakeSession
        d.set_req = lambda *a, **k: None
        self.events, self.summaries, self.rows = [], [], []
        d.post_dispatcher_event = lambda rid, content, kind="action", session_id=None: (
            self.events.append((rid, kind, content)))
        d.append_summary = lambda rid, line: self.summaries.append((rid, line))
        d.sync_agent_session = lambda *a, **k: (self.rows.append(k), 7)[1]

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(d, n, v)
        FakeSession.last_kwargs = None
        FakeSession.model_error = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, resolved, role="dev", model_override=None, settle="settled"):
        FakeSession.settle, FakeSession.effective = settle, None
        d.resolve_role_model = lambda r, project=None: resolved
        d.run_agent(_req(uuid.uuid4().int % 100000), role, "p", 0.0, model_override=model_override)
        return self.events, self.summaries, self.rows

    def test_d1_model_from_resolve(self):
        self._run({"model": "glm-5.2", "source": "project", "configured": "glm-5.2",
                   "configured_source": "project", "degraded": False, "reason": None})
        self.assertEqual(FakeSession.last_kwargs["model"], "glm-5.2")
        self.assertEqual(self.rows[0]["model_requested"], "glm-5.2")
        self.assertEqual(self.rows[0]["model_source"], "project")
        self.assertEqual(self.rows[0]["model"], "display:glm-5.2")

    def test_d2_degraded_event(self):
        events, _, _ = self._run({"model": ac.ACP_MODEL, "source": "global", "configured": "bad",
                                  "configured_source": "project", "degraded": True,
                                  "reason": "配置值不在别名池"})
        models = [e for e in events if e[1] == "model"]
        self.assertTrue(models, events)
        self.assertIn("模型降级", models[0][2])
        self.assertIn("bad", models[0][2])
        self.assertIn("配置值不在别名池", models[0][2])

    def test_d3_unsettled_event(self):
        events, _, _ = self._run({"model": "glm-5.2", "source": "project", "configured": "glm-5.2",
                                  "configured_source": "project", "degraded": False, "reason": None},
                                 settle="mismatch")
        models = [e for e in events if e[1] == "model"]
        self.assertTrue(any("模型未落定" in e[2] and "mismatch" in e[2] for e in models), models)

    def test_d5_model_override_uses_acp_model(self):
        self._run({"model": "glm-5.2", "source": "project", "configured": "glm-5.2",
                   "configured_source": "project", "degraded": False, "reason": None},
                  model_override=ac.ACP_MODEL)
        self.assertEqual(self.rows[0]["model_requested"], ac.ACP_MODEL)
        self.assertEqual(self.rows[0]["model_source"], "global")

    def test_d7_state_model_fallback_written(self):
        self._run({"model": ac.ACP_MODEL, "source": "global", "configured": "bad",
                   "configured_source": "role", "degraded": True, "reason": "配置值不在别名池"})
        entry = d.state["model_fallback"][str(self._req_id)]
        self.assertEqual(entry["role"], "dev")
        self.assertEqual(entry["source"], "global")
        self.assertFalse(entry["used"])
        # 降级事实已记入 degraded_facts 并经收尾汇总；degraded_roles 汇总后清空（防跨轮重复）
        self.assertIn("dev", entry["degraded_facts"])
        self.assertEqual(entry["degraded_roles"], [])

    def test_d9_summary_flush_and_clear(self):
        self._run({"model": ac.ACP_MODEL, "source": "global", "configured": "bad",
                   "configured_source": "role", "degraded": True, "reason": "配置值不在别名池"})
        self.assertTrue(self.summaries)
        self.assertIn("模型降级", self.summaries[-1][1])
        self.assertIn("配置值不在别名池", self.summaries[-1][1])
        self.assertEqual(d.state["model_fallback"][str(self._req_id)]["degraded_roles"], [])
        # 第二轮无降级 → 不再追加
        n = len(self.summaries)
        self._run({"model": "glm-5.2", "source": "project", "configured": "glm-5.2",
                   "configured_source": "project", "degraded": False, "reason": None})
        self.assertEqual(len(self.summaries), n)

    def test_d10_spawn_agent_passes_model_override(self):
        captured = {}

        class FakeThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                captured["target"] = target
                captured["kwargs"] = kwargs or {}

            def start(self):
                pass
        d.threading = types.SimpleNamespace(Thread=FakeThread)
        d.role_for = lambda req: "dev"
        d.build_prompt = lambda req, role, extra: "p"
        d._note_review_model = lambda req, role: None
        d.find_resume_sid = lambda req, role: None
        d.set_req = lambda *a, **k: None
        d.spawn_agent(_req(), role="dev", model_override="deepseek/v4.1-flash")
        self.assertEqual(captured["kwargs"], {"model_override": "deepseek/v4.1-flash"})

    def test_d11_spawn_passes_model_configured(self):
        """D11：spawn 透传 model_configured（= resolved['configured']）；未降级不写 model_reason"""
        self._run({"model": "glm-5.2", "source": "project", "configured": "glm-5.2",
                   "configured_source": "project", "degraded": False, "reason": None})
        row = self.rows[0]
        self.assertEqual(row["model_configured"], "glm-5.2")
        self.assertIn(row.get("model_reason"), (None, ""))

    def test_d12_degraded_reason_recorded_both_channels(self):
        """D12：degraded=True → model_reason=resolved['reason']，两列同进 update/sync；requested 仍为回落值"""
        captured = []
        d.update_session_info = lambda *a, **k: captured.append(k)
        resolved = {"model": ac.ACP_MODEL, "source": "global", "configured": "bad",
                    "configured_source": "project", "degraded": True,
                    "reason": '配置值 bad 不可用（厂商 X 的 type "OpenAI" 非法），已逐层回落'}
        self._run(resolved)
        row = self.rows[0]
        self.assertEqual(row["model_configured"], "bad")
        self.assertEqual(row["model_reason"], resolved["reason"])
        self.assertEqual(row["model_requested"], ac.ACP_MODEL)
        self.assertTrue(captured, "update_session_info 未收到透传 kwargs")
        self.assertEqual(captured[0]["model_configured"], "bad")
        self.assertEqual(captured[0]["model_reason"], resolved["reason"])

    def test_d13_unsettled_reason_recorded(self):
        """D13：settle 非 settled → model_reason 以 '<settle>：' 开头且含 session.model_error 摘要"""
        FakeSession.model_error = "must declare a wire protocol: model gpt-5.6-sol missing protocol"
        try:
            self._run({"model": "glm-5.2", "source": "project", "configured": "glm-5.2",
                       "configured_source": "project", "degraded": False, "reason": None},
                      settle="mismatch")
        finally:
            FakeSession.model_error = None
        reason = self.rows[0]["model_reason"]
        self.assertTrue(reason.startswith("mismatch："), reason)
        self.assertIn("must declare a wire protocol", reason)
        FakeSession.model_error = "must declare a wire protocol: rejected by harness"
        try:
            self._run({"model": "glm-5.2", "source": "project", "configured": "glm-5.2",
                       "configured_source": "project", "degraded": False, "reason": None},
                      settle="rejected")
        finally:
            FakeSession.model_error = None
        self.assertTrue(self.rows[-1]["model_reason"].startswith("rejected："),
                        self.rows[-1]["model_reason"])
        self.assertIn("must declare a wire protocol", self.rows[-1]["model_reason"])

    def test_d14_event_wording(self):
        """D14：降级事件含「配置 X → 请求 Y → 实际 Z」与原因；未落定事件含 settle 与原因"""
        events, _, _ = self._run({"model": ac.ACP_MODEL, "source": "global", "configured": "cfg-x",
                                  "configured_source": "project", "degraded": True,
                                  "reason": "配置值 cfg-x 不可用（厂商 X 的 type 非法），已逐层回落"})
        dm = [e for e in events if e[1] == "model" and "模型降级" in e[2]]
        self.assertTrue(dm, events)
        self.assertIn(f"配置 cfg-x → 请求 {ac.ACP_MODEL} → 实际", dm[0][2])
        self.assertIn("已逐层回落", dm[0][2])
        FakeSession.model_error = "must declare a wire protocol: harness raw"
        try:
            events2, _, _ = self._run({"model": "glm-5.2", "source": "project",
                                       "configured": "glm-5.2", "configured_source": "project",
                                       "degraded": False, "reason": None}, settle="rejected")
        finally:
            FakeSession.model_error = None
        um = [e for e in events2 if e[1] == "model" and "模型未落定" in e[2]]
        self.assertTrue(um, events2)
        self.assertIn("rejected", um[0][2])
        self.assertIn("must declare a wire protocol", um[0][2])

    # 供 test_d7/d9 复用 req_id
    @property
    def _req_id(self):
        return d.state["model_fallback"] and next(iter(d.state["model_fallback"]))


# ==================== E 组：运行期兜底重跑 ====================

class EUnavailableReason(unittest.TestCase):
    """E1-E4：_model_unavailable_reason 归因（窄特征 / 不误判）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req11-tail-"))
        self._fmt = d.RATE_LIMIT_LOG_FMT
        d.RATE_LIMIT_LOG_FMT = str(self.tmp / "agent-{req_id}.log")

    def tearDown(self):
        d.RATE_LIMIT_LOG_FMT = self._fmt
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reason(self, text):
        (self.tmp / "agent-1.log").write_text(text)
        return d._model_unavailable_reason("1")

    def test_e2_quota(self):
        self.assertIn("欠费", self._reason("Error: insufficient balance for model") or "")

    def test_e3_decommission(self):
        self.assertIn("下线", self._reason("model not found: glm-5.2") or "")

    def test_e4_repeated_5xx(self):
        self.assertIn("5xx", self._reason("503\n502\n500\n") or "")

    def test_e4b_no_feature_returns_none(self):
        self.assertIsNone(self._reason("some totally normal failure"))
        d.RATE_LIMIT_LOG_FMT = str(self.tmp / "missing-{req_id}.log")
        self.assertIsNone(d._model_unavailable_reason("1"))

    def test_e4c_single_5xx_not_enough(self):
        self.assertIsNone(self._reason("503 Service Unavailable"))


class EHandleResultFallback(unittest.TestCase):
    """E5-E9：handle_result 既有重试分支追加 model_override（不新增分支，限流延迟/hint 逐字保留）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="req11-hr-"))
        self._orig = {n: getattr(d, n) for n in (
            "state", "save_state", "artifact_dir", "workdir_for", "post_dispatcher_event",
            "spawn_agent", "set_status", "append_summary", "mark_session_poisoned",
            "notify_req_card", "current_node", "RATE_LIMIT_LOG_FMT")}
        d.state = {"retries": {}, "model_fallback": {}, "req_todos": {}}
        d.save_state = lambda: None
        d.artifact_dir = lambda req: self.tmp
        d.workdir_for = lambda proj: self.tmp
        d.set_status = lambda *a, **k: None
        d.append_summary = lambda *a, **k: None
        d.mark_session_poisoned = lambda *a, **k: None
        d.notify_req_card = lambda *a, **k: None
        d.current_node = lambda req: {"node_type": "artifact"}
        d.RATE_LIMIT_LOG_FMT = str(self.tmp / "agent-{req_id}.log")
        self.events, self.spawns = [], []
        d.post_dispatcher_event = lambda rid, content, kind="action", session_id=None: (
            self.events.append((kind, content)))
        d.spawn_agent = lambda req, role=None, extra="", resume=False, model_override=None: (
            self.spawns.append({"role": role, "extra": extra, "model_override": model_override}))
        self._sleep = mock.patch.object(d.time, "sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        for n, v in self._orig.items():
            setattr(d, n, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fb(self, source="project", used=False, role="dev"):
        d.state["model_fallback"] = {"11": {"role": role, "source": source,
                                            "requested": "glm-5.2", "used": used,
                                            "degraded_roles": [], "degraded_facts": {}}}

    def test_e5_rate_limit_fallback_keeps_345_semantics(self):
        self._fb()
        (self.tmp / "agent-11.log").write_text("403 concurrent request limit")
        with mock.patch.object(d.time, "sleep") as slept:
            d.handle_result(_req(11), "dev", -15, False, 0.0)
        self.assertEqual(self.spawns[0]["model_override"], ac.ACP_MODEL)
        self.assertIn("全局兜底模型", self.spawns[0]["extra"])
        self.assertTrue(any(k == "retry" and "全局兜底模型" in c for k, c in self.events))
        # #345 限流延迟逐字保留
        slept.assert_called_once_with(d.RATE_LIMIT_RETRY_DELAY)
        self.assertTrue(d.state["model_fallback"]["11"]["used"])

    def test_e6_quota_fallback_generic_branch(self):
        self._fb()
        (self.tmp / "agent-11.log").write_text("insufficient quota")
        with mock.patch.object(d.time, "sleep") as slept:
            d.handle_result(_req(11), "dev", 1, False, 0.0)
        self.assertEqual(self.spawns[0]["model_override"], ac.ACP_MODEL)
        slept.assert_called_once_with(min(1 * 30, 120))

    def test_e7_source_global_no_fallback(self):
        self._fb(source="global")
        (self.tmp / "agent-11.log").write_text("insufficient quota")
        d.handle_result(_req(11), "dev", 1, False, 0.0)
        self.assertIsNone(self.spawns[0]["model_override"])

    def test_e8_used_once_no_second_fallback(self):
        self._fb(used=True)
        (self.tmp / "agent-11.log").write_text("insufficient quota")
        d.handle_result(_req(11), "dev", 1, False, 0.0)
        self.assertIsNone(self.spawns[0]["model_override"])

    def test_e9_other_failure_unchanged(self):
        self._fb()
        (self.tmp / "agent-11.log").write_text("plain crash")
        d.handle_result(_req(11), "dev", 1, False, 0.0)
        self.assertIsNone(self.spawns[0]["model_override"])

    def test_e10_clear_retries_pops_fallback(self):
        self._fb()
        _REAL_CLEAR_RETRIES("11")
        self.assertNotIn("11", d.state.get("model_fallback", {}))


# ==================== F 组：数据模型与展示 ====================

class FMigrationAndApi(unittest.TestCase):
    """F1-F3：迁移两列 / 白名单落库 / GET 返回"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="req11-db-")
        self._orig_db = b.DB_PATH
        b.DB_PATH = Path(self.tmp) / "legacy.db"
        conn = sqlite3.connect(str(b.DB_PATH))
        conn.execute("""CREATE TABLE agent_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, req_id INTEGER NOT NULL, role TEXT NOT NULL,
            session_id TEXT, model TEXT, thinking TEXT, harness TEXT, context_pct TEXT,
            branch TEXT, workdir TEXT, poisoned INTEGER DEFAULT 0, status TEXT DEFAULT 'created',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("INSERT INTO agent_sessions (req_id, role, session_id, model)"
                     " VALUES (1, 'dev', 's_old', 'old-model')")
        conn.commit()
        conn.close()
        b.init_db()
        b.app.config["TESTING"] = True
        self.client = b.app.test_client()

    def tearDown(self):
        b.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cols(self):
        conn = sqlite3.connect(str(b.DB_PATH))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_sessions)")}
        conn.close()
        return cols

    def test_f1_migration_adds_two_columns_keeps_rows(self):
        self.assertIn("model_requested", self._cols())
        self.assertIn("model_source", self._cols())
        conn = sqlite3.connect(str(b.DB_PATH))
        row = conn.execute("SELECT model, model_requested FROM agent_sessions WHERE session_id='s_old'").fetchone()
        conn.close()
        self.assertEqual(row, ("old-model", None))
        b.init_db()  # 幂等

    def test_f2_whitelist_roundtrip(self):
        self.assertIn("model_requested", b._AGENT_SESSION_FIELDS)
        self.assertIn("model_source", b._AGENT_SESSION_FIELDS)
        resp = self.client.post("/api/agent-sessions", json={
            "req_id": 1, "role": "analysis", "session_id": "s_new",
            "model": "deepseek/v4.1-flash", "model_requested": "glm-5.2", "model_source": "project"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        rowid = resp.get_json()["id"]
        conn = sqlite3.connect(str(b.DB_PATH))
        row = conn.execute("SELECT model, model_requested, model_source FROM agent_sessions WHERE id=?",
                           (rowid,)).fetchone()
        conn.close()
        self.assertEqual(row, ("deepseek/v4.1-flash", "glm-5.2", "project"))
        # 按 id 更新路径同样不被静默丢弃
        resp2 = self.client.post("/api/agent-sessions", json={"id": rowid, "model_source": "global"})
        self.assertEqual(resp2.status_code, 200)
        conn = sqlite3.connect(str(b.DB_PATH))
        self.assertEqual(conn.execute("SELECT model_source FROM agent_sessions WHERE id=?",
                                      (rowid,)).fetchone()[0], "global")
        conn.close()

    def test_f3_get_returns_columns(self):
        self.client.post("/api/agent-sessions", json={
            "req_id": 1, "role": "dev", "session_id": "s_get",
            "model": "x", "model_requested": "glm-5.2", "model_source": "project"})
        resp = self.client.get("/api/agent-sessions")
        self.assertEqual(resp.status_code, 200)
        rows = [r for r in resp.get_json() if r.get("session_id") == "s_get"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["model_requested"], "glm-5.2")
        self.assertEqual(rows[0]["model_source"], "project")

    def test_f9_migration_adds_config_reason_idempotent(self):
        """F9（#13）：迁移新增 model_configured/model_reason 两列；连跑 init_db 不报错；
        缺列旧表迁移后两列为 NULL 且既有行数据不丢。"""
        cols = self._cols()
        self.assertIn("model_configured", cols)
        self.assertIn("model_reason", cols)
        b.init_db()   # setUp 已跑过一次 → 本次为第二次，仍须不报错
        b.init_db()   # 第三次，幂等
        conn = sqlite3.connect(str(b.DB_PATH))
        row = conn.execute(
            "SELECT model, model_requested, model_configured, model_reason"
            " FROM agent_sessions WHERE session_id='s_old'").fetchone()
        conn.close()
        self.assertEqual(row, ("old-model", None, None, None))

    def test_f10_configured_reason_whitelist_and_roundtrip(self):
        """F10（#13）：白名单含两列；带两字段落库、不带为 NULL；GET 返回两字段。"""
        self.assertIn("model_configured", b._AGENT_SESSION_FIELDS)
        self.assertIn("model_reason", b._AGENT_SESSION_FIELDS)
        resp = self.client.post("/api/agent-sessions", json={
            "req_id": 2, "role": "dev", "session_id": "s_cfg",
            "model": "glm-5.2", "model_requested": "glm-5.2",
            "model_configured": "gpt-5.6-sol",
            "model_reason": "配置值不在别名池，已回落"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        rowid = resp.get_json()["id"]
        resp2 = self.client.post("/api/agent-sessions", json={
            "req_id": 2, "role": "dev", "session_id": "s_none", "model": "glm-5.2"})
        self.assertEqual(resp2.status_code, 201, resp2.get_data(as_text=True))
        rowid2 = resp2.get_json()["id"]
        conn = sqlite3.connect(str(b.DB_PATH))
        row = conn.execute("SELECT model_configured, model_reason FROM agent_sessions WHERE id=?",
                           (rowid,)).fetchone()
        none_row = conn.execute("SELECT model_configured, model_reason FROM agent_sessions WHERE id=?",
                                (rowid2,)).fetchone()
        conn.close()
        self.assertEqual(row, ("gpt-5.6-sol", "配置值不在别名池，已回落"))
        self.assertEqual(none_row, (None, None))
        got = self.client.get("/api/agent-sessions")
        self.assertEqual(got.status_code, 200)
        rows = [r for r in got.get_json() if r.get("session_id") == "s_cfg"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["model_configured"], "gpt-5.6-sol")
        self.assertEqual(rows[0]["model_reason"], "配置值不在别名池，已回落")
        rows2 = [r for r in got.get_json() if r.get("session_id") == "s_none"]
        self.assertTrue(rows2)
        self.assertEqual((rows2[0]["model_configured"], rows2[0]["model_reason"]), (None, None))

    def test_f5_chat_sessions_untouched(self):
        """F5（P5 反向断言）：chat_sessions 不加列、buildChatSessionFooter 未引入模型请求值"""
        conn = sqlite3.connect(str(b.DB_PATH))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
        conn.close()
        self.assertNotIn("model_requested", cols)
        src = frontend_src()
        m = re.search(r"function buildChatSessionFooter\([\s\S]*?\n        \}", src)
        self.assertTrue(m)
        self.assertNotIn("model_req", m.group(0))


class FFrontendHarness(unittest.TestCase):
    """F4/F6：前端 modelMetaText node harness + served JS 语法"""

    def test_f4_model_meta_harness(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用")
        from tests.test_process_markdown_merge import _extract_function
        js = frontend_js()
        source = "\n\n".join(_extract_function(js, n)
                             for n in ("escapeHtml", "modelMetaText"))
        harness = (Path(__file__).resolve().parent / "model_meta_harness.js").read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(source + "\n\n" + harness)
            path = f.name
        try:
            proc = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        checks = json.loads(proc.stdout or "{}").get("checks", [])
        self.assertTrue(checks)
        self.assertTrue(all(c["pass"] for c in checks), [c for c in checks if not c["pass"]])

    def _run_node_harness(self, funcs):
        """提取 static/board.js 函数拼装 model_meta_harness.js 交 node 跑，返回 checks 列表。
        未注入的函数其检查组按 harness 内 typeof 守卫整体跳过（F4/F11/F12 共用同一文件）。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用")
        from tests.test_process_markdown_merge import _extract_function
        js = frontend_js()
        source = "\n\n".join(_extract_function(js, n) for n in funcs)
        harness = (Path(__file__).resolve().parent / "model_meta_harness.js").read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(source + "\n\n" + harness)
            path = f.name
        try:
            proc = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        checks = json.loads(proc.stdout or "{}").get("checks", [])
        self.assertTrue(checks)
        self.assertTrue(all(c["pass"] for c in checks), [c for c in checks if not c["pass"]])
        return checks

    def test_f11_model_alert_text_harness(self):
        """F11（#13）：modelAlertText 文案矩阵（node 跑真实函数）——空/相等/不等/仅原因/皆有/
        请求缺失不误报/标记字符原样返回。"""
        checks = self._run_node_harness(("escapeHtml", "modelMetaText", "modelAlertText"))
        names = [c["name"] for c in checks]
        f11 = [n for n in names if n.startswith("F11_")]
        self.assertGreaterEqual(len(f11), 7, names)

    def test_f12_session_footer_wiring(self):
        """F12（#13）：中间栏 sessionFooterHtml 接线——源码含琥珀行结构与 modelAlertText 调用；
        node 跑真实函数：有提示渲染 .session-model-warn、无提示/无会话不渲染（负向）。"""
        from tests.test_process_markdown_merge import _extract_function
        js = frontend_js()
        body = _extract_function(js, "sessionFooterHtml")
        self.assertIn("session-model-warn", body)
        self.assertIn("smw-label", body)
        self.assertIn("modelAlertText(s)", body)
        # 顺序：主行 → 子行 → 模型回落提示 → 会话产物 → 分支差异
        self.assertIn("${subHtml}${alertLine}${artifactLine}${diffLine}", body)
        # 负向：alertLine 由 alertText 三元控制（无提示 → ''）
        self.assertIn("const alertLine = alertText", body)
        checks = self._run_node_harness((
            "escapeHtml", "escapeAttr", "modelMetaText", "modelAlertText",
            "roleLabel",
            "isSessionLinkActive", "isReqChatLinkActive", "visibleSessionRows",
            "agentGroupExpanded", "sessionList", "sessionFooterHtml"))
        names = [c["name"] for c in checks]
        f12 = [n for n in names if n.startswith("F12_")]
        self.assertGreaterEqual(len(f12), 5, names)



    def test_f13_session_head_wiring(self):
        """F13（#13）：右栏 sessHeadHtml 常驻行尾注入 .sess-model-alert；
        processHtml 与 agentSessionHtml 两处均传 alert。"""
        from tests.test_process_markdown_merge import _extract_function
        src = frontend_js()
        head = _extract_function(src, "sessHeadHtml")
        self.assertIn("sess-model-alert", head)
        self.assertIn("info.alert", head)
        self.assertIn("escapeAttr(info.alert)", head)
        self.assertIn("escapeHtml(info.alert)", head)
        self.assertNotIn("sess-thinking-alert", head)
        self.assertNotIn("alertThink", head)
        self.assertEqual(src.count("alert: modelAlertText("), 2,
                         "processHtml 与 agentSessionHtml 两处均须传 alert")


# ==================== F14：既有面板回归锁 ====================

class FPanelRegressionLock(unittest.TestCase):
    """F14：镜像 test_p7_layout_ui C7（157-164）与 test_process_markdown_merge T19（430-455）
    的负向锁/选择器约束，确认 #13 改动未破坏它们（两文件本身不改）。"""

    def setUp(self):
        self.src = frontend_src()
        self.js = frontend_js()

    def test_f14_p7_negative_locks_hold(self):
        from tests.test_process_markdown_merge import _extract_function
        card = _extract_function(self.js, "cardHtml")
        self.assertEqual(card.count("card-collapse"), 1)
        self.assertNotIn("i.status === 'done' ? `<span class=\"card-collapse", card,
                         "折叠图标必须对所有状态可用（#49）")
        self.assertIn('<span class="card-collapse', card)
        sf = _extract_function(self.js, "sessionFooterHtml")
        self.assertNotIn("collapse", sf)
        self.assertNotIn("cardExpandState", sf)
        self.assertNotIn("dblclick", self.src)

    def test_f14_merge_selector_locks_hold(self):
        s = self.src
        self.assertIn(
            ".session-footer .sf-id-link.running { animation: sfIdBreath 2.2s ease-in-out infinite; }", s)
        self.assertIn("@keyframes sfIdBreath {\n            0%, 100% { opacity: 0.62; text-shadow: 0 0 0 var(--breath-off); }\n            50% { opacity: 1; text-shadow: 0 0 10px var(--breath-glow-a), 0 0 22px var(--breath-glow-b); }\n        }", s)
        self.assertIn(".session-footer .sf-id-link.running { animation: none; }", s)
        self.assertNotIn(".session-row.running", s)
        self.assertEqual(s.count(".sf-id-link.running"),
                         s.count(".session-footer .sf-id-link.running"),
                         "呼吸选择器必须全部带 .session-footer 前缀")
        self.assertNotIn(".chat-status.thinking", s)
        self.assertNotIn("chatStatusBreath", s)


# ==================== G 组：日志挂载与落盘 ====================

class GLoggerMount(unittest.TestCase):
    """G1-G3：acp_shared logger 挂生产 FileHandler（幂等）+ WARNING 落盘"""

    def _handlers(self, name=None):
        lg = logging.getLogger(name) if name else logging.getLogger()
        return [h for h in lg.handlers
                if isinstance(h, logging.FileHandler) and h.baseFilename.endswith("req_board_dispatcher.log")]

    def tearDown(self):
        for name in (None, "dispatcher", "acp_shared"):
            for h in self._handlers(name):
                logging.getLogger(name).removeHandler(h)
                h.close()

    def test_g1_mount_idempotent_includes_acp_shared(self):
        d._mount_file_logger()
        d._mount_file_logger()
        self.assertEqual(len(self._handlers("dispatcher")), 1)
        self.assertEqual(len(self._handlers("acp_shared")), 1)
        self.assertEqual(self._handlers("acp_shared")[0].baseFilename, PROD_LOG)

    def test_g2_acp_shared_warning_written(self):
        d._mount_file_logger()
        ac.log.warning("#11 probe 模型未落定（mismatch）")
        for h in logging.getLogger("acp_shared").handlers:
            h.flush()
        text = Path(PROD_LOG).read_text(encoding="utf-8", errors="replace")
        self.assertIn("#11 probe 模型未落定（mismatch）", text)


if __name__ == "__main__":
    unittest.main()
