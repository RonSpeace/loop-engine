#!/usr/bin/env python3
"""#6 模型设置端点测试（GET/POST /api/model-settings，harness config.toml 全量维护）。

M1~M24：结构 / 不泄密钥 / 只读失败面 / 编辑与 key 三态 / 未编辑零损失 / 厂商与模型增删 /
default_model 矩阵（含悬空拒绝）/ 改名联动 / 拒绝矩阵零写入 / 原子性与回环 / 权限 / 并发 /
落点同源 / 软链 / 形态边界 / 幂等 / 多 op 同单。
M25~M26：同单 add+update 一致落盘 / key 首尾空白 strip 存储。
M27~M37（#13）：provider type 词表校验（大小写敏感）+ models_add 可选 protocol +
models_update 设/改/清 protocol 与拒绝矩阵零写入 / 密钥不泄 / protocol 不受词表约束。

隔离：临时 home（mock.patch session_homes 指向 tmp）+ 临时 DB；绝不触真实 harness home
（~/.kimi-code-deepseek/config.toml）与真实 data/board.db。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_model_settings_api.py -q
"""
import difflib
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

import board as b  # noqa: E402

FIXTURE = '''default_model = "prov-a/m1"
# 顶层注释保留

[providers.prov-a]
base_url = "https://a.example.com/v1"
type = "openai"
api_key = "SECRET-AAA"

[providers.prov-b]
base_url = "https://b.example.com"
type = "openai"

[providers.prov-c]
type = "openai"
base_url = "https://c.example.com"
model = "up-mc"

[models."prov-a/m1"]
provider = "prov-a"
model = "m1"
max_context_size = 128000
max_output_size = 8192
capabilities = ["tool_use", "thinking"]
display_name = "M1"
reasoning_key = "reasoning_content"
support_efforts = ["low", "high"]
x_unknown_field = "keep-me"

[models."prov-b/m2"]
provider = "prov-b"
model = "m2"
'''


def _loads(text):
    import tomllib
    return tomllib.loads(text)


def _diff(before, after):
    d = difflib.ndiff(before.splitlines(), after.splitlines())
    return [l for l in d if l.startswith("- ") or l.startswith("+ ")]


class MSBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req6-ms-"))
        cls._orig_db = b.DB_PATH
        b.DB_PATH = cls.tmp / "board.db"
        b.init_db()
        cls.client = b.app.test_client()

    @classmethod
    def tearDownClass(cls):
        b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.counter = 0
        self.homes = []
        self._patcher = mock.patch.object(b, "session_homes", side_effect=lambda: list(self.homes))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.home = self._mkhome(FIXTURE)

    def _mkhome(self, text=None, mode=0o600):
        self.counter += 1
        home = self.tmp / f"h{self.counter}-{self._testMethodName}"
        home.mkdir()
        if text is not None:
            p = home / "config.toml"
            p.write_text(text, encoding="utf-8")
            os.chmod(p, mode)
        self.homes = [home]
        return home

    def _get(self, home=None):
        return self.client.get("/api/model-settings")

    def _post(self, body, home=None):
        return self.client.post("/api/model-settings", json=body)

    def _text(self, home=None):
        return (home or self.home).joinpath("config.toml").read_text(encoding="utf-8")

    def _sig(self, home=None):
        st = (home or self.home).joinpath("config.toml").stat()
        return (st.st_mtime_ns, st.st_size, st.st_mode & 0o777)

    def _snapshot(self, home=None):
        home = home or self.home
        p = home / "config.toml"
        bak = home / "config.toml.bak"
        bak_state = (bak.read_bytes(), bak.stat().st_mtime_ns) if bak.exists() else None
        return (p.read_bytes(), self._sig(home), bak_state)

    def _assert_zero_write(self, snap, home=None):
        home = home or self.home
        p = home / "config.toml"
        bak = home / "config.toml.bak"
        self.assertEqual(p.read_bytes(), snap[0])
        self.assertEqual(self._sig(home), snap[1])
        bak_state = (bak.read_bytes(), bak.stat().st_mtime_ns) if bak.exists() else None
        self.assertEqual(bak_state, snap[2])
        self.assertFalse((home / "config.toml.tmp").exists())


class GetModelSettingsTest(MSBase):
    def test_m1_get_structure(self):
        d = self._get().get_json()
        self.assertEqual(set(d), {"path", "home", "default_model", "default_model_dangling",
                                  "default_model_candidates", "provider_types", "context_defaults",
                                  "providers", "models", "warnings"})
        self.assertEqual(d["home"], str(self.home))
        self.assertEqual(d["path"], os.path.realpath(self.home / "config.toml"))
        self.assertEqual(d["provider_types"], list(b.HARNESS_PROVIDER_TYPES))
        self.assertEqual(d["context_defaults"], [dict(r) for r in b.CONTEXT_SIZE_DEFAULTS])
        self.assertEqual([p["name"] for p in d["providers"]], ["prov-a", "prov-b", "prov-c"])
        for p in d["providers"]:
            self.assertEqual(set(p), {"name", "base_url", "type", "key_configured", "model_count"})
        self.assertEqual(d["providers"][0]["model_count"], 1)
        self.assertEqual(d["providers"][2]["model_count"], 0)
        self.assertEqual([m["alias"] for m in d["models"]], ["prov-a/m1", "prov-b/m2"])
        for m in d["models"]:
            self.assertEqual(set(m), {"alias", "provider", "model", "protocol",
                                      "max_context_size", "incomplete_reason"})
            self.assertEqual(m["protocol"], "")   # 无 protocol 键回读为空串
        # #18：字段完整 → 回读整数 + 空原因；缺失 → None + 逐条原因（缺哪个字段）
        by = {m["alias"]: m for m in d["models"]}
        self.assertEqual((by["prov-a/m1"]["max_context_size"], by["prov-a/m1"]["incomplete_reason"]),
                         (128000, ""))
        self.assertIsNone(by["prov-b/m2"]["max_context_size"])
        self.assertIn("缺少必填字段 max_context_size", by["prov-b/m2"]["incomplete_reason"])
        self.assertEqual(d["default_model"], "prov-a/m1")
        self.assertFalse(d["default_model_dangling"])
        self.assertEqual(d["default_model_candidates"], ["prov-a/m1", "prov-b/m2", "prov-c/up-mc"])
        self.assertEqual(d["warnings"], [])

    def test_m2_get_no_key_leak(self):
        raw = self._get().get_data(as_text=True)
        self.assertNotIn("SECRET-AAA", raw)
        self.assertNotIn("api_key", raw)
        d = self._get().get_json()
        self.assertTrue(d["providers"][0]["key_configured"])
        self.assertFalse(d["providers"][1]["key_configured"])
        # 错误路径同样不泄：GET 解析失败文案不含密钥
        (self.home / "config.toml").write_text('default_model = "SECRET-BBB"\n[bad', encoding="utf-8")
        resp = self._get()
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("SECRET-BBB", resp.get_data(as_text=True))

    def test_m3_get_failure_readonly(self):
        empty = self.tmp / f"empty-{self._testMethodName}"
        empty.mkdir()
        self.homes = [empty]
        resp = self._get()
        self.assertEqual(resp.status_code, 404)
        self.assertIn("homes", resp.get_json())
        self.assertEqual(resp.get_json()["homes"], [str(empty)])
        self.assertFalse((empty / "config.toml").exists())
        self.homes = [self.home]
        (self.home / "config.toml").write_text("{bad toml", encoding="utf-8")
        self.assertEqual(self._get().status_code, 400)
        # 解析失败后原文件不动
        self.assertEqual((self.home / "config.toml").read_text(), "{bad toml")


class EditFieldsTest(MSBase):
    def test_m4_edit_base_url_and_type(self):
        before = self._text()
        resp = self._post({"providers_update": [
            {"name": "prov-a", "base_url": "https://new.example.com/v2", "type": "anthropic"}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], True)
        after = self._text()
        self.assertIn('base_url = "https://new.example.com/v2"', after)
        self.assertIn('type = "anthropic"', after)
        self.assertEqual(_diff(before, after), [
            '- base_url = "https://a.example.com/v1"', '+ base_url = "https://new.example.com/v2"',
            '- type = "openai"', '+ type = "anthropic"'])
        raw = _loads(after)
        self.assertEqual(raw["providers"]["prov-a"]["base_url"], "https://new.example.com/v2")
        self.assertEqual(raw["providers"]["prov-a"]["api_key"], "SECRET-AAA")   # 未编辑字段保留
        self.assertEqual(self.home.joinpath("config.toml.bak").read_text(), before)
        self.assertEqual(oct(self.home.joinpath("config.toml.bak").stat().st_mode & 0o777), "0o600")
        d = self._get().get_json()
        self.assertEqual(d["providers"][0]["base_url"], "https://new.example.com/v2")
        self.assertEqual(d["providers"][0]["type"], "anthropic")

    def test_m5_key_replace(self):
        resp = self._post({"providers_update": [{"name": "prov-b", "key": "NEW-SECRET-BBB"}]})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("NEW-SECRET-BBB", body)
        self.assertNotIn("SECRET-AAA", body)
        after = self._text()
        self.assertIn('api_key = "NEW-SECRET-BBB"', after)
        d = self._get().get_json()
        self.assertTrue(d["providers"][1]["key_configured"])
        # 失败路径不泄：删厂商被引用拒绝的文案不含密钥
        bad = self._post({"providers_delete": ["prov-a"]})
        self.assertEqual(bad.status_code, 400)
        self.assertNotIn("SECRET", bad.get_data(as_text=True))

    def test_m6_key_blank_keeps_bytes(self):
        snap = self._snapshot()
        resp = self._post({"providers_update": [{"name": "prov-a", "key": ""}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        self._assert_zero_write(snap)
        resp = self._post({"providers_update": [{"name": "prov-a", "base_url": "https://a.example.com/v1",
                                                 "key": "SECRET-AAA"}]})
        self.assertEqual(resp.get_json()["changed"], False)   # 同值提交 = 语义无变化
        self._assert_zero_write(snap)

    def test_m7_key_clear(self):
        snap = self._snapshot()
        resp = self._post({"providers_update": [{"name": "prov-a", "clear_key": True,
                                                 "base_url": "https://a.example.com/v9"}]})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        self.assertNotIn("api_key", after)
        self.assertNotIn("SECRET-AAA", after)
        self.assertIn('base_url = "https://a.example.com/v9"', after)   # 与字段同单一次落盘
        self.assertFalse(self._get().get_json()["providers"][0]["key_configured"])
        self.assertTrue(self.home.joinpath("config.toml.bak").exists())
        self.assertEqual(self.home.joinpath("config.toml.bak").read_bytes(), snap[0])

    def test_m8_untouched_zero_loss(self):
        before = self._text()
        resp = self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        self.assertEqual(_diff(before, after),
                         ['- type = "openai"', '+ type = "anthropic"'])
        raw = _loads(after)
        m1 = raw["models"]["prov-a/m1"]
        self.assertEqual(m1["max_context_size"], 128000)
        self.assertEqual(m1["max_output_size"], 8192)
        self.assertEqual(m1["capabilities"], ["tool_use", "thinking"])
        self.assertEqual(m1["display_name"], "M1")
        self.assertEqual(m1["reasoning_key"], "reasoning_content")
        self.assertEqual(m1["support_efforts"], ["low", "high"])
        self.assertEqual(m1["x_unknown_field"], "keep-me")
        self.assertIn("# 顶层注释保留", after)
        expect = _loads(before)
        expect["providers"]["prov-b"]["type"] = "anthropic"
        self.assertEqual(raw, expect)


class ProviderModelOpsTest(MSBase):
    def test_m9_add_provider(self):
        resp = self._post({"providers_add": [
            {"name": "prov-new", "base_url": "https://new/", "type": "openai", "key": "K-1"}]})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        self.assertIn("[providers.prov-new]", after)
        raw = _loads(after)
        self.assertEqual(raw["providers"]["prov-new"],
                         {"base_url": "https://new/", "type": "openai", "api_key": "K-1"})
        self.assertTrue(self._get().get_json()["providers"][-1]["key_configured"])
        # key 可选：空 key 不写键
        resp = self._post({"providers_add": [{"name": "prov-noKey", "base_url": "https://n/", "type": "openai", "key": ""}]})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("prov-noKey]\napi_key", self._text())
        self.assertFalse(self._get().get_json()["providers"][-1]["key_configured"])
        # 拒绝矩阵零写入
        cases = [
            {"providers_add": [{"name": "prov-a", "base_url": "https://x/", "type": "openai"}]},   # 重复名
            {"providers_add": [{"name": "a b", "base_url": "https://x/", "type": "t"}]},      # 名含空白
            {"providers_add": [{"name": "x" * 129, "base_url": "https://x/", "type": "t"}]},  # 名超长
            {"providers_add": [{"name": "ok-n", "base_url": "", "type": "t"}]},               # base_url 空
            {"providers_add": [{"name": "ok-n", "base_url": "https://x/", "type": ""}]},      # type 空
            {"providers_add": [{"name": "ok-n", "base_url": "https://x/", "type": "t", "x": 1}]},  # 未知键
        ]
        for body in cases:
            snap = self._snapshot()
            resp = self._post(body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn("error", resp.get_json())
            self._assert_zero_write(snap)

    def test_m10_add_alias(self):
        # 单独新增（provider 已存在）；#18 起 max_context_size 必填
        resp = self._post({"models_add": [{"alias": "prov-b/m3", "provider": "prov-b", "model": "m3",
                                           "max_context_size": 200000}]})
        self.assertEqual(resp.status_code, 200)
        raw = _loads(self._text())
        self.assertEqual(raw["models"]["prov-b/m3"],
                         {"provider": "prov-b", "model": "m3", "max_context_size": 200000})
        # 同单先建厂商再挂别名（provider 按最终态校验）
        resp = self._post({"providers_add": [{"name": "prov-d", "base_url": "https://d/", "type": "openai"}],
                           "models_add": [{"alias": "prov-d/m9", "provider": "prov-d", "model": "m9",
                                           "max_context_size": 100000}]})
        self.assertEqual(resp.status_code, 200)
        raw = _loads(self._text())
        self.assertEqual(raw["models"]["prov-d/m9"]["provider"], "prov-d")
        # 拒绝矩阵零写入
        cases = [
            {"models_add": [{"alias": "prov-a/m1", "provider": "prov-b", "model": "x"}]},   # 别名重复
            {"models_add": [{"alias": "a b", "provider": "prov-b", "model": "x"}]},         # 别名非法
            {"models_add": [{"alias": "ok/x", "provider": "ghost", "model": "x"}]},         # provider 不存在
            {"models_add": [{"alias": "ok/y", "provider": "prov-b", "model": ""}]},         # model 空
            {"models_add": [{"alias": "ok/z", "provider": "prov-b", "model": "m", "k": 1}]},
        ]
        for body in cases:
            snap = self._snapshot()
            resp = self._post(body)
            self.assertEqual(resp.status_code, 400, body)
            self._assert_zero_write(snap)

    def test_m11_delete_alias(self):
        before = self._text()
        resp = self._post({"models_delete": ["prov-b/m2"]})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        self.assertNotIn("prov-b/m2", after)
        self.assertEqual(_diff(before, after),
                         ['- [models."prov-b/m2"]', '- provider = "prov-b"', '- model = "m2"'])
        self.assertNotIn("prov-b/m2", _loads(after)["models"])
        # 删 default_model 所指且未改 → 400
        snap = self._snapshot()
        resp = self._post({"models_delete": ["prov-a/m1"]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("default_model", resp.get_json()["error"])
        self._assert_zero_write(snap)
        # 同单改 default_model → 通过
        resp = self._post({"models_delete": ["prov-a/m1"], "default_model": "prov-c/up-mc"})
        self.assertEqual(resp.status_code, 200)
        raw = _loads(self._text())
        self.assertEqual(raw["default_model"], "prov-c/up-mc")

    def test_m12_default_model_matrix(self):
        # 改到合法别名（含合成别名）→ 落盘
        resp = self._post({"default_model": "prov-c/up-mc"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('default_model = "prov-c/up-mc"', self._text())
        # 不存在 → 400；空串 → 400
        for val in ("ghost/x", ""):
            snap = self._snapshot()
            resp = self._post({"default_model": val})
            self.assertEqual(resp.status_code, 400)
            self._assert_zero_write(snap)
        # 原文件悬空 + 保存（未提交 dm）→ 400；提交修正 → 200
        home2 = self._mkhome(FIXTURE.replace('default_model = "prov-a/m1"', 'default_model = "ghost/x"'))
        snap = self._snapshot(home2)
        resp = self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("default_model", resp.get_json()["error"])
        self._assert_zero_write(snap, home2)
        d = self._get(home2).get_json()
        self.assertTrue(d["default_model_dangling"])
        self.assertEqual(d["default_model_candidates"], ["prov-a/m1", "prov-b/m2", "prov-c/up-mc"])
        resp = self._post({"default_model": "prov-b/m2"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._get(home2).get_json()["default_model_dangling"])
        # 原无该键且未提交 → 通过
        home3 = self._mkhome("\n".join(l for l in FIXTURE.splitlines()
                                       if not l.startswith("default_model")).lstrip("\n"))
        resp = self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        self.assertEqual(resp.status_code, 200)
        d = self._get(home3).get_json()
        self.assertEqual(d["default_model"], "")
        self.assertFalse(d["default_model_dangling"])
        # 存在但为空值 → GET 标记悬空（评审建议 4）；未修正保存 400 零写入
        home4 = self._mkhome(FIXTURE.replace('default_model = "prov-a/m1"', 'default_model = ""'))
        d = self._get(home4).get_json()
        self.assertTrue(d["default_model_dangling"])
        self.assertEqual(d["default_model"], "")
        snap = self._snapshot(home4)
        resp = self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]}, home4)
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(snap, home4)


    def test_m13_provider_rename(self):
        # 改名：段头 + 引用模型 provider 行同步；无 dm 联动时 dm 仍有效（models 别名）
        before = self._text()
        resp = self._post({"providers_update": [{"name": "prov-a", "new_name": "prov-a2"}]})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        raw = _loads(after)
        self.assertIn("prov-a2", raw["providers"])
        self.assertEqual(raw["models"]["prov-a/m1"]["provider"], "prov-a2")
        self.assertEqual(raw["default_model"], "prov-a/m1")     # 别名本身未变，仍有效
        self.assertEqual(_diff(before, after),
                         ['- [providers.prov-a]', '+ [providers.prov-a2]',
                          '- provider = "prov-a"', '+ provider = "prov-a2"'])
        # 合成别名联动：dm == old/u 且改名后失效、new/u 有效 → 自动改写
        home2 = self._mkhome('default_model = "prov-c/up-mc"\n\n[providers.prov-c]\n'
                             'type = "openai"\nbase_url = "https://c/"\nmodel = "up-mc"\n\n'
                             '[models."prov-b/m2"]\nprovider = "prov-b"\nmodel = "m2"\n\n'
                             '[providers.prov-b]\nbase_url = "https://b/"\ntype = "openai"\n')
        resp = self._post({"providers_update": [{"name": "prov-c", "new_name": "prov-c2"}]}, home2)
        self.assertEqual(resp.status_code, 200)
        after = self._text(home2)
        self.assertIn('[providers.prov-c2]', after)
        self.assertIn('default_model = "prov-c2/up-mc"', after)
        self.assertNotIn("prov-c/up-mc", after)
        # 同单显式提交旧值 → 400（显式优先、悬空拒绝）
        home3 = self._mkhome('default_model = "prov-c/up-mc"\n\n[providers.prov-c]\n'
                             'type = "openai"\nbase_url = "https://c/"\nmodel = "up-mc"\n\n'
                             '[models."prov-b/m2"]\nprovider = "prov-b"\nmodel = "m2"\n\n'
                             '[providers.prov-b]\nbase_url = "https://b/"\ntype = "openai"\n')
        snap = self._snapshot(home3)
        resp = self._post({"providers_update": [{"name": "prov-c", "new_name": "prov-c2"}],
                           "default_model": "prov-c/up-mc"}, home3)
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(snap, home3)
        # 目标名冲突 / 未知厂商 → 400
        resp = self._post({"providers_update": [{"name": "prov-a", "new_name": "prov-b"}]})
        self.assertEqual(resp.status_code, 400)
        resp = self._post({"providers_update": [{"name": "ghost", "new_name": "x"}]})
        self.assertEqual(resp.status_code, 400)
        # 引用为内联形态 → 400 提示手动编辑
        home4 = self._mkhome('default_model = "prov-a/m1"\n\n[providers.prov-a]\n'
                             'base_url = "https://a/"\ntype = "openai"\n\n[models]\n'
                             '"prov-a/m1" = {provider = "prov-a", model = "m1"}\n')
        snap = self._snapshot(home4)
        resp = self._post({"providers_update": [{"name": "prov-a", "new_name": "prov-a2"}]}, home4)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("直接编辑", resp.get_json()["error"])
        self._assert_zero_write(snap, home4)

    def test_m14_provider_delete(self):
        # 最终态仍被引用 → 400（报引用数/名）
        snap = self._snapshot()
        resp = self._post({"providers_delete": ["prov-a"]})
        self.assertEqual(resp.status_code, 400)
        err = resp.get_json()["error"]
        self.assertIn("prov-a/m1", err)
        self.assertIn("1", err)
        self._assert_zero_write(snap)
        # 同单删厂商 + 删其全部别名 + 改 dm → 通过（按最终 want 判定）
        resp = self._post({"providers_delete": ["prov-a"], "models_delete": ["prov-a/m1"],
                           "default_model": "prov-b/m2"})
        self.assertEqual(resp.status_code, 200)
        raw = _loads(self._text())
        self.assertNotIn("prov-a", raw.get("providers", {}))
        self.assertNotIn("prov-a/m1", raw.get("models", {}))
        self.assertEqual(raw["default_model"], "prov-b/m2")
        # 无引用 → 段移除（其余字节不变）
        before = self._text()
        resp = self._post({"providers_delete": ["prov-c"]})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        self.assertNotIn("[providers.prov-c]", after)
        self.assertEqual(_diff(before, after),
                         ['- [providers.prov-c]', '- type = "openai"',
                          '- base_url = "https://c.example.com"', '- model = "up-mc"', '- '])
        # 删除致 default_model 悬空（合成别名）→ 400
        home2 = self._mkhome('default_model = "prov-c/up-mc"\n\n[providers.prov-c]\n'
                             'type = "openai"\nbase_url = "https://c/"\nmodel = "up-mc"\n')
        snap = self._snapshot(home2)
        resp = self._post({"providers_delete": ["prov-c"]}, home2)
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(snap, home2)


    def test_m25_add_then_update_same_submission(self):
        """评审必改 2 回归：同单 providers_add + providers_update（改名/字段/挂别名）一致落盘、回环通过。"""
        # add + rename：追加段落名与 want 一致
        resp = self._post({"providers_add": [{"name": "prov-x", "base_url": "https://x/", "type": "openai"}],
                           "providers_update": [{"name": "prov-x", "new_name": "prov-y"}]})
        self.assertEqual(resp.status_code, 200)
        raw = _loads(self._text())
        self.assertIn("prov-y", raw["providers"])
        self.assertNotIn("prov-x", raw["providers"])
        self.assertIn("[providers.prov-y]", self._text())
        # add + 字段更新（含 key）：追加段落取最终值
        resp = self._post({"providers_add": [{"name": "prov-z", "base_url": "https://z/", "type": "openai"}],
                           "providers_update": [{"name": "prov-z", "base_url": "https://z2/", "key": " K9 "}]})
        self.assertEqual(resp.status_code, 200)
        raw = _loads(self._text())
        self.assertEqual(raw["providers"]["prov-z"]["base_url"], "https://z2/")
        self.assertEqual(raw["providers"]["prov-z"]["api_key"], "K9")
        # add + rename + 同单挂别名（引用新名）：全链路一致
        resp = self._post({"providers_add": [{"name": "prov-w", "base_url": "https://w/", "type": "openai"}],
                           "providers_update": [{"name": "prov-w", "new_name": "prov-w2"}],
                           "models_add": [{"alias": "prov-w2/m9", "provider": "prov-w2", "model": "m9",
                                           "max_context_size": 200000}]})
        self.assertEqual(resp.status_code, 200)
        raw = _loads(self._text())
        self.assertEqual(raw["models"]["prov-w2/m9"]["provider"], "prov-w2")
        self.assertIn("[providers.prov-w2]", self._text())
        self.assertIn('[models."prov-w2/m9"]', self._text())

    def test_m26_key_whitespace_stored_stripped(self):
        """评审建议 5（取 strip 存储）：key 首尾空白去除后写入；纯空白 key 拒绝且零写入。"""
        resp = self._post({"providers_update": [{"name": "prov-a", "key": "  NEW-KEY-AAA  "}]})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('api_key = "NEW-KEY-AAA"', self._text())
        snap = self._snapshot()
        resp = self._post({"providers_update": [{"name": "prov-a", "key": "   "}]})
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(snap)
        resp = self._post({"providers_add": [{"name": "prov-k", "base_url": "https://k/", "type": "openai",
                                              "key": " K-2 "}]})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('api_key = "K-2"', self._text())


class RejectionAtomicityTest(MSBase):
    def test_m15_rejection_matrix_zero_write(self):
        cases = [
            ("body 非对象", [1]),
            ("顶层未知键", {"bogus": 1}),
            ("providers_add 非数组", {"providers_add": {}}),
            ("providers_add 元素非对象", {"providers_add": [1]}),
            ("providers_update 元素非对象", {"providers_update": ["x"]}),
            ("providers_delete 元素非法", {"providers_delete": [1]}),
            ("对象内未知键", {"providers_update": [{"name": "prov-b", "whatever": 1}]}),
            ("key+clear_key 同传", {"providers_update": [{"name": "prov-b", "key": "k", "clear_key": True}]}),
            ("clear_key 非布尔", {"providers_update": [{"name": "prov-b", "clear_key": "yes"}]}),
            ("未知更新目标", {"providers_update": [{"name": "ghost", "type": "t"}]}),
            ("未知删除目标", {"providers_delete": ["ghost"]}),
            ("未知别名删除", {"models_delete": ["ghost"]}),
            ("列表内重复目标", {"providers_delete": ["prov-c", "prov-c"]}),
            ("key 超长", {"providers_update": [{"name": "prov-b", "key": "k" * 4097}]}),
            ("key 含控制字符", {"providers_update": [{"name": "prov-b", "key": "k\x01"}]}),
            ("default_model 非字符串", {"default_model": 3}),
        ]
        for name, body in cases:
            snap = self._snapshot()
            resp = self._post(body)
            self.assertIn(resp.status_code, (400, 404), name)
            self.assertEqual(resp.status_code, 400, name)
            self._assert_zero_write(snap)

    def test_m16_write_atomicity(self):
        snap = self._snapshot()
        with mock.patch.object(os, "replace", side_effect=OSError("disk boom")):
            resp = self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(self.home.joinpath("config.toml").read_bytes(), snap[0])
        self.assertFalse((self.home / "config.toml.tmp").exists())
        # .bak 写失败 → 500 原文件不变
        snap = self._snapshot()
        orig_write = Path.write_bytes

        def fake_write(self, data):  # noqa: ANN001
            if self.name.endswith(".bak"):
                raise OSError("bak boom")
            return orig_write(self, data)

        with mock.patch.object(Path, "write_bytes", fake_write):
            resp = self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("anthropic", self._text())
        self.assertEqual(self.home.joinpath("config.toml").read_bytes(), snap[0])
        self.assertFalse((self.home / "config.toml.tmp").exists())

    def test_m17_loopback_guard(self):
        snap = self._snapshot()
        with mock.patch.object(b, "_harness_cfg_splice", side_effect=lambda text, intents: text):
            resp = self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        self.assertEqual(resp.status_code, 500)
        self._assert_zero_write(snap)


class LandingAndIoTest(MSBase):
    def test_m18_permissions(self):
        self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        p = self.home / "config.toml"
        bak = self.home / "config.toml.bak"
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(bak.stat().st_mode & 0o777), "0o600")
        self.assertFalse((self.home / "config.toml.tmp").exists())

    def test_m19_concurrent_updates(self):
        results = []

        def worker(body):
            with b.app.test_client() as c:
                results.append(c.post("/api/model-settings", json=body).status_code)

        t1 = threading.Thread(target=worker,
                              args=({"providers_update": [{"name": "prov-b", "type": "anthropic"}]},))
        t2 = threading.Thread(target=worker,
                              args=({"providers_update": [{"name": "prov-c", "base_url": "https://c9/"}]},))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(results, [200, 200])
        raw = _loads(self._text())
        self.assertEqual(raw["providers"]["prov-b"]["type"], "anthropic")
        self.assertEqual(raw["providers"]["prov-c"]["base_url"], "https://c9/")

    def test_m20_home_resolution(self):
        home_a = self.home
        home_b = self._mkhome(FIXTURE.replace("SECRET-AAA", "SECRET-ZZZ"))
        self.homes = [home_a, home_b]
        d = self._get().get_json()
        self.assertEqual(d["home"], str(home_a))
        self.assertEqual(d["path"], os.path.realpath(home_a / "config.toml"))
        self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        self.assertIn("anthropic", self._text(home_a))
        self.assertNotIn("anthropic", self._text(home_b))
        # 全无文件 → 404 且不创建
        empty = self.tmp / f"none-{self._testMethodName}"
        empty.mkdir()
        self.homes = [home_a, empty]
        empty_idx_home = empty
        (home_a / "config.toml").unlink()
        resp = self._get()
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._post({"default_model": "x"}).status_code, 404)
        self.assertFalse((empty_idx_home / "config.toml").exists())

    def test_m21_symlink_target(self):
        real_dir = self.tmp / f"real-{self._testMethodName}"
        link_dir = self.tmp / f"link-{self._testMethodName}"
        real_dir.mkdir(); link_dir.mkdir()
        real = real_dir / "config.toml"
        real.write_text(FIXTURE, encoding="utf-8")
        os.chmod(real, 0o600)
        os.symlink(real, link_dir / "config.toml")
        self.homes = [link_dir]
        d = self._get().get_json()
        self.assertEqual(d["path"], os.path.realpath(real))
        resp = self._post({"providers_update": [{"name": "prov-b", "type": "anthropic"}]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.islink(link_dir / "config.toml"))
        self.assertIn("anthropic", real.read_text())
        self.assertTrue((real_dir / "config.toml.bak").exists())
        self.assertEqual(sorted(p.name for p in link_dir.iterdir()), ["config.toml"])

    def test_m22_form_boundaries(self):
        # [models] 父表下内联键值：删条目支持
        home2 = self._mkhome('default_model = "prov-a/m1"\n\n[providers.prov-a]\n'
                             'base_url = "https://a/"\ntype = "openai"\n\n[models]\n'
                             '"inline/x" = {provider = "prov-a", model = "ix"}\n\n'
                             '[models."prov-a/m1"]\nprovider = "prov-a"\nmodel = "m1"\n')
        resp = self._post({"models_delete": ["inline/x"]}, home2)
        self.assertEqual(resp.status_code, 200)
        after = self._text(home2)
        self.assertNotIn("inline/x", after)
        self.assertIn('[models."prov-a/m1"]', after)
        self.assertNotIn("inline/x", _loads(after).get("models", {}))
        # dotted-key 形式 provider 编辑 → 400 提示手动编辑
        home3 = self._mkhome('default_model = "prov-a/m1"\n'
                             'providers.prov-a.base_url = "https://a/"\n'
                             'providers.prov-a.type = "openai"\n\n'
                             '[models."prov-a/m1"]\nprovider = "prov-a"\nmodel = "m1"\n')
        snap = self._snapshot(home3)
        resp = self._post({"providers_update": [{"name": "prov-a", "base_url": "https://b/"}]}, home3)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("直接编辑", resp.get_json()["error"])
        self._assert_zero_write(snap, home3)

    def test_m23_idempotent(self):
        snap = self._snapshot()
        resp = self._post({"providers_update": [{"name": "prov-a", "base_url": "https://a.example.com/v1"}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        self._assert_zero_write(snap)
        # 空对象 = 零改动
        resp = self._post({})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        self._assert_zero_write(snap)

    def test_m24_multi_op_single_write(self):
        before = self._text()
        resp = self._post({
            "providers_update": [{"name": "prov-b", "type": "anthropic"}],
            "models_add": [{"alias": "prov-b/m3", "provider": "prov-b", "model": "m3",
                            "max_context_size": 200000}],
            "models_delete": ["prov-b/m2"],
            "default_model": "prov-b/m3"})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        self.assertEqual(self.home.joinpath("config.toml.bak").read_text(), before)   # .bak = 写前字节
        raw = _loads(after)
        self.assertEqual(raw["providers"]["prov-b"]["type"], "anthropic")
        self.assertEqual(raw["models"]["prov-b/m3"],
                         {"provider": "prov-b", "model": "m3", "max_context_size": 200000})
        self.assertNotIn("prov-b/m2", raw["models"])
        self.assertEqual(raw["default_model"], "prov-b/m3")
        # 其它未编辑内容逐字节保留
        self.assertIn("# 顶层注释保留", after)
        self.assertIn("x_unknown_field", after)


class ProtocolAndTypeVocabTest(MSBase):
    """#13：provider type 词表校验（M27~M29）与 model protocol 文本面增/改/清（M30~M37）。

    词表单一源 = acp_shared.HARNESS_PROVIDER_TYPES（经 board 命名空间暴露）；
    protocol 不做词表校验（显式决策）——任意合法语法值可保存。
    """

    def test_m27_providers_add_type_vocab(self):
        """providers_add 的 type 词表校验（大小写敏感精确匹配）：非法 → 400 零写入。"""
        for tv in ("OpenAI", "OPENAI", "zzz"):
            snap = self._snapshot()
            resp = self._post({"providers_add": [
                {"name": "pv-add", "base_url": "https://x/", "type": tv}]})
            self.assertEqual(resp.status_code, 400, tv)
            err = resp.get_json()["error"]
            self.assertIn(tv, err)
            self.assertIn(b.HARNESS_PROVIDER_TYPES_HINT, err)
            self._assert_zero_write(snap)
        # 含内部空白：已在前置「无空白」校验被拒（未到词表比对），同样 400 零写入
        snap = self._snapshot()
        resp = self._post({"providers_add": [
            {"name": "pv-sp", "base_url": "https://x/", "type": "open ai"}]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("type", resp.get_json()["error"])
        self.assertNotIn(b.HARNESS_PROVIDER_TYPES_HINT, resp.get_json()["error"])
        self._assert_zero_write(snap)

    def test_m28_providers_update_type_vocab(self):
        """providers_update 词表校验：存量条目分支 + 同单 pending 新增条目分支，均 400 零写入。"""
        snap = self._snapshot()
        resp = self._post({"providers_update": [{"name": "prov-b", "type": "zzz"}]})
        self.assertEqual(resp.status_code, 400)
        err = resp.get_json()["error"]
        self.assertIn("zzz", err)
        self.assertIn(b.HARNESS_PROVIDER_TYPES_HINT, err)
        self._assert_zero_write(snap)
        # 同单新增（合法）后再 update 成非法 type：最终态非法，整单拒绝、零写入
        snap = self._snapshot()
        resp = self._post({"providers_add": [{"name": "pv-x", "base_url": "https://x/", "type": "openai"}],
                           "providers_update": [{"name": "pv-x", "type": "OpenAI"}]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("OpenAI", resp.get_json()["error"])
        self._assert_zero_write(snap)

    def test_m29_type_case_sensitive(self):
        """词表精确匹配：合法值通过（落盘），非法大小写/非词表值拒绝。"""
        for i, tv in enumerate(("openai", "anthropic", "openai_responses", "google-genai", "kimi")):
            name = f"vocab-{i}"
            resp = self._post({"providers_add": [{"name": name, "base_url": "https://x/", "type": tv}]})
            self.assertEqual(resp.status_code, 200, tv)
            self.assertEqual(_loads(self._text())["providers"][name]["type"], tv)
        for i, tv in enumerate(("OpenAI", "Openai", "ANTHROPIC", "openai-compatible")):
            snap = self._snapshot()
            resp = self._post({"providers_add": [{"name": f"bad-{i}", "base_url": "https://x/",
                                                  "type": tv}]})
            self.assertEqual(resp.status_code, 400, tv)
            self.assertIn(b.HARNESS_PROVIDER_TYPES_HINT, resp.get_json()["error"])
            self._assert_zero_write(snap)

    def test_m30_models_add_protocol(self):
        """models_add 可选 protocol：非空落盘追加段含四行；不带 → 段内无 protocol 行。"""
        before = self._text()
        resp = self._post({"models_add": [
            {"alias": "prov-b/m3", "provider": "prov-b", "model": "m3",
             "protocol": "brand-new-proto", "max_context_size": 200000}]})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        # 未编辑行逐字节保留（仅在文件尾追加）
        self.assertTrue(after.startswith(before))
        self.assertEqual(after[len(before):],
                         '\n[models."prov-b/m3"]\nprovider = "prov-b"\nmodel = "m3"\n'
                         'max_context_size = 200000\nprotocol = "brand-new-proto"\n\n')
        raw = _loads(after)
        self.assertEqual(raw["models"]["prov-b/m3"],
                         {"provider": "prov-b", "model": "m3", "max_context_size": 200000,
                          "protocol": "brand-new-proto"})
        self.assertEqual([m for m in self._get().get_json()["models"]
                          if m["alias"] == "prov-b/m3"][0]["protocol"], "brand-new-proto")
        # 不带 protocol → 追加段无 protocol 行（除新增的 max_context_size 行外与改动前逐字一致）
        resp = self._post({"models_add": [{"alias": "prov-b/m4", "provider": "prov-b", "model": "m4",
                                           "max_context_size": 200000}]})
        self.assertEqual(resp.status_code, 200)
        seg4 = self._text().split('[models."prov-b/m4"]', 1)[1]
        self.assertEqual(seg4, '\nprovider = "prov-b"\nmodel = "m4"\nmax_context_size = 200000\n\n')
        self.assertEqual(_loads(self._text())["models"]["prov-b/m4"],
                         {"provider": "prov-b", "model": "m4", "max_context_size": 200000})

    def test_m31_models_update_set_protocol(self):
        """models_update 给存量条目设 protocol：段内新增一行，diff 仅该行，其余语义零丢失。"""
        before = self._text()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "protocol": "brand-new-proto"}]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["changed"])
        after = self._text()
        self.assertEqual(_diff(before, after), ['+ protocol = "brand-new-proto"'])
        # 新行紧贴段头下一行（provider 之前）
        seg = after.split('[models."prov-a/m1"]', 1)[1].splitlines()
        self.assertEqual(seg[0], "")
        self.assertEqual(seg[1], 'protocol = "brand-new-proto"')
        self.assertEqual(seg[2], 'provider = "prov-a"')
        # 前后对照：语义快照仅多出 protocol
        raw = _loads(after)
        self.assertEqual(raw["models"]["prov-a/m1"]["x_unknown_field"], "keep-me")
        self.assertEqual(raw["models"]["prov-a/m1"]["max_context_size"], 128000)
        self.assertIn("# 顶层注释保留", after)
        expect = _loads(before)
        expect["models"]["prov-a/m1"]["protocol"] = "brand-new-proto"
        self.assertEqual(raw, expect)

    def test_m32_models_update_change_and_idempotent(self):
        """同值 → changed=false 零写入无 diff；异值 → 行级替换，其余行逐字节不变。"""
        self._post({"models_update": [{"alias": "prov-a/m1", "protocol": "p1"}]})
        snap = self._snapshot()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "protocol": "p1"}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        self._assert_zero_write(snap)
        before = self._text()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "protocol": "p2"}]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["changed"])
        after = self._text()
        self.assertEqual(_diff(before, after), ['- protocol = "p1"', '+ protocol = "p2"'])
        self.assertEqual(_loads(after)["models"]["prov-a/m1"]["protocol"], "p2")
        self.assertIn('max_context_size = 128000', after)
        self.assertIn('x_unknown_field = "keep-me"', after)

    def test_m33_models_update_clear_protocol(self):
        """clear_protocol：删行成功；原本无该键 / clear_protocol=false → 幂等零写入。"""
        self._post({"models_update": [{"alias": "prov-a/m1", "protocol": "p1"}]})
        before = self._text()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "clear_protocol": True}]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["changed"])
        after = self._text()
        self.assertEqual(_diff(before, after), ['- protocol = "p1"'])
        self.assertNotIn("protocol", _loads(after)["models"]["prov-a/m1"])
        # 原本无该键 → 幂等零写入
        snap = self._snapshot()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "clear_protocol": True}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        self._assert_zero_write(snap)
        # false → 幂等零写入（不删）
        snap = self._snapshot()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "clear_protocol": False}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        self._assert_zero_write(snap)

    def test_m34_models_update_reject_matrix(self):
        """models_update 非法输入：全部 400 且零写入；非标准段形态 → 400 提示直接编辑。"""
        cases = [
            ("alias 不存在", {"models_update": [{"alias": "ghost/x", "protocol": "p"}]}),
            ("protocol+clear 同传", {"models_update": [
                {"alias": "prov-a/m1", "protocol": "p", "clear_protocol": True}]}),
            ("未知键", {"models_update": [{"alias": "prov-a/m1", "foo": 1}]}),
            ("protocol 含空格", {"models_update": [{"alias": "prov-a/m1", "protocol": "a b"}]}),
            ("protocol 含控制字符", {"models_update": [{"alias": "prov-a/m1", "protocol": "a\x01"}]}),
            ("protocol 超长", {"models_update": [{"alias": "prov-a/m1", "protocol": "x" * 65}]}),
            ("protocol 空串", {"models_update": [{"alias": "prov-a/m1", "protocol": ""}]}),
            ("clear_protocol 非布尔", {"models_update": [{"alias": "prov-a/m1", "clear_protocol": "yes"}]}),
            ("alias 非字符串", {"models_update": [{"alias": 1, "protocol": "p"}]}),
        ]
        for name, body in cases:
            snap = self._snapshot()
            resp = self._post(body)
            self.assertEqual(resp.status_code, 400, name)
            self._assert_zero_write(snap)
        # inline table（无 [models.<alias>] 段）→ 400 提示直接编辑
        # （#18：该条目字段完整——否则先被最终态完整性校验拦下，测不到段形态分支）
        home2 = self._mkhome('default_model = "prov-a/m1"\n\n[providers.prov-a]\n'
                             'base_url = "https://a/"\ntype = "openai"\n\n[models]\n'
                             '"prov-a/m1" = {provider = "prov-a", model = "m1", '
                             'max_context_size = 128000}\n')
        snap = self._snapshot(home2)
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "protocol": "p"}]}, home2)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不支持在线编辑", resp.get_json()["error"])
        self._assert_zero_write(snap, home2)

    def test_m35_vocab_and_protocol_zero_write_matrix(self):
        """扩展零写入矩阵：M27~M34 的非法 body 混编，逐一 400 且文件字节不变。"""
        cases = [
            ("add type 大写非法", {"providers_add": [{"name": "z1", "base_url": "https://x/", "type": "OpenAI"}]}),
            ("add type 未知", {"providers_add": [{"name": "z2", "base_url": "https://x/", "type": "zzz"}]}),
            ("add type 含空格", {"providers_add": [{"name": "z3", "base_url": "https://x/", "type": "open ai"}]}),
            ("update type 非法", {"providers_update": [{"name": "prov-b", "type": "Openai"}]}),
            ("update 新增条 type 非法", {
                "providers_add": [{"name": "z4", "base_url": "https://x/", "type": "openai"}],
                "providers_update": [{"name": "z4", "type": "ANTHROPIC"}]}),
            ("add alias protocol 含空格", {"models_add": [
                {"alias": "prov-b/m9", "provider": "prov-b", "model": "m9", "protocol": "a b"}]}),
            ("add alias protocol 超长", {"models_add": [
                {"alias": "prov-b/m9", "provider": "prov-b", "model": "m9", "protocol": "x" * 65}]}),
            ("update protocol 含控制字符", {"models_update": [{"alias": "prov-a/m1", "protocol": "a\x01"}]}),
            ("update clear 非布尔", {"models_update": [{"alias": "prov-a/m1", "clear_protocol": 1}]}),
            ("update alias 不存在", {"models_update": [{"alias": "ghost/x", "protocol": "p"}]}),
            ("update 未知键", {"models_update": [{"alias": "prov-a/m1", "foo": 1}]}),
            ("update protocol+clear 同传", {"models_update": [
                {"alias": "prov-a/m1", "protocol": "p", "clear_protocol": True}]}),
        ]
        for name, body in cases:
            snap = self._snapshot()
            resp = self._post(body)
            self.assertEqual(resp.status_code, 400, name)
            self._assert_zero_write(snap)

    def test_m36_protocol_faces_no_key_leak(self):
        """protocol 相关错误文案与 GET 回读正文均不含 api_key 字样与夹具密钥原文。"""
        texts = []
        for body in (
            {"providers_add": [{"name": "l1", "base_url": "https://x/", "type": "zzz"}]},
            {"models_update": [{"alias": "prov-a/m1", "protocol": "a b"}]},
            {"models_update": [{"alias": "ghost/x", "protocol": "p"}]},
            {"models_add": [{"alias": "prov-b/m9", "provider": "prov-b", "model": "m9",
                             "protocol": "a b"}]},
        ):
            resp = self._post(body)
            self.assertEqual(resp.status_code, 400)
            texts.append(resp.get_data(as_text=True))
        texts.append(self._get().get_data(as_text=True))
        for text in texts:
            self.assertNotIn("api_key", text)
            self.assertNotIn("SECRET-AAA", text)
        # 写入 protocol 后 GET 回读：有 protocol 值但不泄密钥
        self._post({"models_update": [{"alias": "prov-a/m1", "protocol": "brand-new-proto"}]})
        d = self._get()
        body = d.get_data(as_text=True)
        self.assertNotIn("api_key", body)
        self.assertNotIn("SECRET-AAA", body)
        self.assertEqual([m for m in d.get_json()["models"]
                          if m["alias"] == "prov-a/m1"][0]["protocol"], "brand-new-proto")

    def test_m37_protocol_not_vocab_constrained(self):
        """显式决策锁定：protocol 不受 HARNESS_PROVIDER_TYPES 约束，任意合法语法值可保存。"""
        self.assertNotIn("brand-new-proto", b.HARNESS_PROVIDER_TYPES)
        for val in ("brand-new-proto", "Google-GenAI", "x"):
            home = self._mkhome(FIXTURE)
            resp = self._post({"models_update": [{"alias": "prov-a/m1", "protocol": val}]}, home)
            self.assertEqual(resp.status_code, 200, val)
            self.assertEqual(_loads(self._text(home))["models"]["prov-a/m1"]["protocol"], val)
        home = self._mkhome(FIXTURE)
        resp = self._post({"models_add": [{"alias": "prov-b/m9", "provider": "prov-b", "model": "m9",
                                           "protocol": "brand-new-proto",
                                           "max_context_size": 200000}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_loads(self._text(home))["models"]["prov-b/m9"]["protocol"], "brand-new-proto")


class ContextSizeRequiredTest(MSBase):
    """#18：max_context_size 必填校验 + 整数裸写 + 最终态（读法 A）。

    M38 新增必填矩阵（缺失/0/-1/字符串/布尔/小数）→ 400 零写入；M39 合法新增 → 裸整数落盘 + GET 回读 int；
    M40 models_update 设/改值（行级最小拼接 + 未编辑字段零损失）；M41 设值非法矩阵；
    M42 最终态：对缺该字段的条目做 protocol-only update → 400（读法 A）；
    M43 同单 add + update 改值 → 追加段取最终值；M44 同单新增又删除 → 最终态校验跳过（不 KeyError）。
    """

    def test_m38_models_add_requires_context_size(self):
        for bad in ("__MISSING__", None, 0, -1, "128000", True, 1.5):
            item = {"alias": "prov-b/m9", "provider": "prov-b", "model": "m9"}
            if bad != "__MISSING__":
                item["max_context_size"] = bad
            snap = self._snapshot()
            resp = self._post({"models_add": [item]})
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn("max_context_size", resp.get_json()["error"], bad)
            self._assert_zero_write(snap)

    def test_m39_models_add_renders_bare_integer(self):
        resp = self._post({"models_add": [{"alias": "prov-b/m9", "provider": "prov-b", "model": "m9",
                                           "max_context_size": 200000}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._text().split('[models."prov-b/m9"]', 1)[1],
                         '\nprovider = "prov-b"\nmodel = "m9"\nmax_context_size = 200000\n\n')
        self.assertNotIn('max_context_size = "', self._text())
        row = [m for m in self._get().get_json()["models"] if m["alias"] == "prov-b/m9"][0]
        self.assertEqual((row["max_context_size"], row["incomplete_reason"]), (200000, ""))

    def test_m40_models_update_set_context_size(self):
        # 缺失 → 补上：段内紧贴段头下一行插入；diff 仅该行
        before = self._text()
        resp = self._post({"models_update": [{"alias": "prov-b/m2", "max_context_size": 65536}]})
        self.assertEqual(resp.status_code, 200)
        after = self._text()
        self.assertEqual(_diff(before, after), ['+ max_context_size = 65536'])
        seg = after.split('[models."prov-b/m2"]', 1)[1].splitlines()
        self.assertEqual(seg[1], 'max_context_size = 65536')
        self.assertEqual(_loads(after)["models"]["prov-b/m2"],
                         {"provider": "prov-b", "model": "m2", "max_context_size": 65536})
        # 改值 → 行级替换，未编辑字段（含 x_unknown_field / capabilities）零损失
        before2 = self._text()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "max_context_size": 200000}]})
        self.assertEqual(resp.status_code, 200)
        after2 = self._text()
        self.assertEqual(_diff(before2, after2),
                         ['- max_context_size = 128000', '+ max_context_size = 200000'])
        m1 = _loads(after2)["models"]["prov-a/m1"]
        self.assertEqual(m1["max_context_size"], 200000)
        self.assertEqual((m1["max_output_size"], m1["capabilities"], m1["x_unknown_field"]),
                         (8192, ["tool_use", "thinking"], "keep-me"))
        # 同值 → 幂等零写入
        snap = self._snapshot()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "max_context_size": 200000}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["changed"], False)
        self._assert_zero_write(snap)

    def test_m41_models_update_context_size_reject_matrix(self):
        for bad in (None, 0, -1, "128000", True, 1.5):
            snap = self._snapshot()
            resp = self._post({"models_update": [{"alias": "prov-a/m1", "max_context_size": bad}]})
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn("max_context_size", resp.get_json()["error"], bad)
            self._assert_zero_write(snap)
        # 清空语义不存在（必填不可清）→ 未知键 400
        snap = self._snapshot()
        resp = self._post({"models_update": [{"alias": "prov-a/m1", "clear_max_context_size": True}]})
        self.assertEqual(resp.status_code, 400)
        self._assert_zero_write(snap)

    def test_m42_final_state_requires_context_size(self):
        """读法 A：对缺该字段的条目只改 protocol → 400（零写入），文案指向 max_context_size。"""
        snap = self._snapshot()
        resp = self._post({"models_update": [{"alias": "prov-b/m2", "protocol": "openai"}]})
        self.assertEqual(resp.status_code, 400)
        err = resp.get_json()["error"]
        self.assertIn("prov-b/m2", err)
        self.assertIn("max_context_size", err)
        self._assert_zero_write(snap)
        # 同单补齐 + 改 protocol → 通过（最终态完整）
        resp = self._post({"models_update": [{"alias": "prov-b/m2", "protocol": "openai",
                                              "max_context_size": 32000}]})
        self.assertEqual(resp.status_code, 200)
        m2 = _loads(self._text())["models"]["prov-b/m2"]
        self.assertEqual((m2["protocol"], m2["max_context_size"]), ("openai", 32000))
        # 未触碰的存量不完整条目不阻断整单保存（只改 default_model）
        home2 = self._mkhome(FIXTURE)
        resp = self._post({"default_model": "prov-c/up-mc"}, home2)
        self.assertEqual(resp.status_code, 200)

    def test_m43_add_then_update_context_same_submission(self):
        resp = self._post({"models_add": [{"alias": "prov-b/m9", "provider": "prov-b", "model": "m9",
                                           "max_context_size": 1000}],
                           "models_update": [{"alias": "prov-b/m9", "max_context_size": 4096}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_loads(self._text())["models"]["prov-b/m9"]["max_context_size"], 4096)
        self.assertEqual(self._text().split('[models."prov-b/m9"]', 1)[1],
                         '\nprovider = "prov-b"\nmodel = "m9"\nmax_context_size = 4096\n\n')

    def test_m44_add_then_delete_skips_final_check(self):
        """评审意见 3：同单新增又删除的别名在最终态校验中被 .get 跳过（不 KeyError→500）。"""
        resp = self._post({"models_add": [{"alias": "prov-b/m9", "provider": "prov-b", "model": "m9",
                                           "max_context_size": 1000}],
                           "models_delete": ["prov-b/m9"]})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("prov-b/m9", _loads(self._text())["models"])


if __name__ == "__main__":
    unittest.main()
