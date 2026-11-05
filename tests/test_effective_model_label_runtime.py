"""设置/指南里的「生效模型」标注：OpenCode 显示按角色实际注入的模型；
「受限角色暂不可用」只对确实不支持受限角色的运行体显示。"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import _baseline  # noqa: E402,F401
import board  # noqa: E402,F401
from board_modules.services import presentation as p  # noqa: E402


class EffectiveModelLabelTest(unittest.TestCase):
    def _label(self, role, runtime, **env):
        with mock.patch.dict(os.environ, {"LOOP_AGENT_RUNTIME": runtime, **env}, clear=False), \
                mock.patch.object(p, "role_runtime_detail", lambda r, proj=None: (runtime, "env")):
            for k in [k for k in os.environ if k.startswith("LOOP_OPENCODE_MODEL") and k not in env]:
                os.environ.pop(k)
            return p._effective_model_label(role)

    def test_opencode_shows_role_model_and_no_restricted_caveat(self):
        env = {"LOOP_OPENAI_BASE_URL": "http://x/v1", "LOOP_OPENCODE_MODEL": "cheap", "LOOP_OPENCODE_MODEL_DEV": "strong"}
        self.assertTrue(self._label("dev", "opencode", **env).startswith("strong · OpenCode"))
        review = self._label("code_review", "opencode", **env)
        self.assertTrue(review.startswith("cheap · OpenCode"))
        self.assertNotIn("受限角色暂不可用", review)

    def test_opencode_without_model_falls_back_to_adapter_default(self):
        self.assertIn("适配器默认模型", self._label("dev", "opencode"))

    def test_runtime_without_restricted_support_keeps_caveat(self):
        self.assertIn("受限角色暂不可用", self._label("code_review", "codex"))
        self.assertNotIn("受限角色暂不可用", self._label("dev", "codex"))


if __name__ == "__main__":
    unittest.main()
