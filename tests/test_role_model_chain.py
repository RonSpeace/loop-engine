#!/usr/bin/env python3
"""#4 四层兜底链测试：acp_shared.role_model_detail / role_model（临时 projects.json）。

A 组：顺序矩阵（项目级 > 角色级 > 环境变量 > 全局）、层①跳过条件、来源标注、fail-safe、
      原子替换热生效、环境变量形态。

隔离：PROJECTS_FILE patch 到临时文件（绝不写真实 projects.json）；FLOW_CONFIG mock；环境变量 mock；
      不建 worktree、不拉起进程。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest tests/test_role_model_chain.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import acp_shared as ac  # noqa: E402

ROLES = {"dev": {"display": "需求开发", "order": 5, "artifact": None,
                 "prompt_file": "prompts/dev.md"}}


class RoleModelChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="req4-chain-"))
        cls.pj = cls.tmp / "projects.json"
        cls._orig_pj = ac.PROJECTS_FILE
        ac.PROJECTS_FILE = cls.pj

    @classmethod
    def tearDownClass(cls):
        ac.PROJECTS_FILE = cls._orig_pj
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if self.pj.exists():
            self.pj.unlink()
        self._cfg = mock.patch.dict(ac.FLOW_CONFIG, {"roles": ROLES})
        self._cfg.start()
        self.addCleanup(self._cfg.stop)

    def _write(self, data):
        self.pj.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _replace(self, data):
        """原子替换（模拟 board 写路径）：新文件同目录 + os.replace"""
        new = self.tmp / "next.json"
        new.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(new, self.pj)

    # ---- A1：四层顺序矩阵（逐层建立即赢） ----
    def test_a1_order_matrix(self):
        with mock.patch.dict("os.environ", {"KIMI_MODEL_DEV": "env-model"}):
            # 仅项目级
            self._write({"p1": {"models": {"dev": "proj-model"}}})
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("proj-model", "project"))
            # 项目级 > 角色级 > env
            roles = dict(ROLES, dev=dict(ROLES["dev"], model="role-model"))
            with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
                self.assertEqual(ac.role_model_detail("dev", "p1"), ("proj-model", "project"))
                self.assertEqual(ac.role_model_detail("dev", "other"), ("role-model", "role"))
            # 仅 env
            self._write({})
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("env-model", "env"))
            # 全空 → 全局兜底（恒非空）
            clean = {k: v for k, v in os.environ.items() if k != "KIMI_MODEL_DEV"}
            with mock.patch.dict("os.environ", clean, clear=True):
                self.assertEqual(ac.role_model_detail("dev"), (ac.ACP_MODEL, "global"))

    # ---- A2：层①跳过条件 ----
    def test_a2_layer1_skip_conditions(self):
        roles = dict(ROLES, dev=dict(ROLES["dev"], model="role-model"))
        cases = [
            ("项目 None", None, {"p1": {"models": {"dev": "proj"}}}),
            ("项目空串", "", {"p1": {"models": {"dev": "proj"}}}),
            ("未知项目", "ghost", {"p1": {"models": {"dev": "proj"}}}),
            ("字符串简写条目", "p1", {"p1": "/some/path"}),
            ("models 非 dict", "p1", {"p1": {"models": ["proj"]}}),
            ("值为 int", "p1", {"p1": {"models": {"dev": 3}}}),
            ("值为空白串", "p1", {"p1": {"models": {"dev": "   "}}}),
        ]
        with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
            for name, project, data in cases:
                self._write(data)
                self.assertEqual(ac.role_model_detail("dev", project), ("role-model", "role"), name)

    # ---- A3：来源标注与值视图恒等 ----
    def test_a3_source_labels_and_view(self):
        with mock.patch.dict("os.environ", {"KIMI_MODEL_DEV": "env-model"}):
            self._write({"p1": {"models": {"dev": "proj-model"}}})
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("proj-model", "project"))
            self.assertEqual(ac.role_model("dev", "p1"), "proj-model")
            self._write({})
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("env-model", "env"))
            self.assertEqual(ac.role_model("dev", "p1"), "env-model")
        self._write({})
        model, source = ac.role_model_detail("dev")
        self.assertEqual(source, "global")
        self.assertTrue(model)                       # 恒非空（层④兜底）
        self.assertEqual(ac.role_model("dev"), model)
        self.assertEqual(ac.role_model("nonexistent-role"), ac.ACP_MODEL)

    # ---- A4：fail-safe（缺失/非法 JSON/不可读字节/顶层非对象） ----
    def test_a4_fail_safe(self):
        roles = dict(ROLES, dev=dict(ROLES["dev"], model="role-model"))
        with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
            self.assertFalse(self.pj.exists())                       # 文件缺失
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("role-model", "role"))
            self.pj.write_text("{not json", encoding="utf-8")        # 非法 JSON
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("role-model", "role"))
            self.pj.write_bytes(b"\xff\xfe\x00bad")                  # 不可读字节
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("role-model", "role"))
            self._write([1, 2])                                      # 顶层非对象
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("role-model", "role"))

    # ---- A5：热生效（原子替换后下一次调用即见新值，无缓存失效动作） ----
    def test_a5_hot_effective(self):
        self._write({"p1": {"models": {"dev": "m-old"}}})
        self.assertEqual(ac.role_model("dev", "p1"), "m-old")
        self._replace({"p1": {"models": {"dev": "m-new"}}})
        self.assertEqual(ac.role_model_detail("dev", "p1"), ("m-new", "project"))
        self._replace({"p1": {"models": {"dev": ""}}})   # 空串 = 无层①
        model, source = ac.role_model_detail("dev", "p1")
        self.assertNotEqual(source, "project")
        self.assertTrue(model)

    # ---- A6：环境变量形态（角色名大写化；角色级非空时 env 不参与） ----
    def test_a6_env_form(self):
        with mock.patch.dict("os.environ", {"KIMI_MODEL_DEV": "env-dev"}):
            self._write({})
            self.assertEqual(ac.role_model_detail("dev", "p1"), ("env-dev", "env"))
            roles = dict(ROLES, dev=dict(ROLES["dev"], model="role-dev"))
            with mock.patch.dict(ac.FLOW_CONFIG, {"roles": roles}):
                self.assertEqual(ac.role_model_detail("dev", "p1"), ("role-dev", "role"))


if __name__ == "__main__":
    unittest.main()
