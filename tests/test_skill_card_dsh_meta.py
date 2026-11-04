"""create_card.py 建卡来源元数据检测（#303）回归测试。

覆盖：DSH_SESSION_JSONL 为普通 JSONL / zstd 压缩（.zstd 后缀）两种形态；
config 挂载位置（顶层旧假设 / data.header.config DSH 实测 / data 直挂兜底）；
异常场景（无变量、文件不存在、损坏 zstd、乱字节）一律返回 None 不抛异常。
zstd 用例依赖系统 zstd 命令，缺失时跳过。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "req-board-card" / "scripts" / "create_card.py"

spec = importlib.util.spec_from_file_location("create_card", SCRIPT)
create_card = importlib.util.module_from_spec(spec)
spec.loader.exec_module(create_card)

HAS_ZSTD = shutil.which("zstd") is not None

SID = "session-1234-test"


class DetectDshMetaTest(unittest.TestCase):
    def _call(self, path=None, sid=None, home=None):
        old = dict(os.environ)
        for k in ("DSH_SESSION_JSONL", "DSH_SESSION_ID", "DSH_HOME"):
            os.environ.pop(k, None)
        if path is not None:
            os.environ["DSH_SESSION_JSONL"] = path
        if sid is not None:
            os.environ["DSH_SESSION_ID"] = sid
        if home is not None:
            os.environ["DSH_HOME"] = home
        try:
            return create_card.detect_dsh_meta()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def _write(self, tmpdir, name, content_bytes):
        p = Path(tmpdir) / name
        p.write_bytes(content_bytes)
        return str(p)

    def _zstd(self, src):
        out = src + ".zstd"
        subprocess.run(["zstd", "-q", "-f", src, "-o", out], check=True)
        return out

    def test_no_env_var(self):
        """三个 DSH 信号都没有 → None，调用方才允许回退 kimi-code 探测"""
        self.assertIsNone(self._call(path=None))

    def test_missing_file(self):
        """给了会话文件路径但文件不存在：仍在 DSH 环境 → source=dsh，不猜模型"""
        self.assertEqual(self._call(path="/tmp/definitely-not-exists-317.jsonl"),
                         {"source": "dsh", "source_session_id": None})

    def test_env_signal_without_session_file(self):
        """#350 现场：DSH 里跑 bash 工具时只有 DSH_SESSION_ID（无会话文件路径）→ source 必须正确、model 留空"""
        self.assertEqual(self._call(sid=SID), {"source": "dsh", "source_session_id": SID})

    def test_env_signal_dsh_home_only(self):
        """仅 DSH_HOME 兜底信号 → 同样认定 DSH 环境"""
        self.assertEqual(self._call(home="/Users/x/.dsh"),
                         {"source": "dsh", "source_session_id": None})

    def test_dsh_env_suppresses_kimi_fallback(self):
        """DSH 环境命中时返回值非空 → main 的 `if not dsh` 守卫不会触发 kimi-code 探测（防误标 source=kimi-code）"""
        self.assertTrue(self._call(sid=SID, home="/Users/x/.dsh"))

    def test_plain_jsonl_dsh_structure(self):
        """DSH 实测结构：config 挂在 data.header.config 下"""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "session.jsonl", (
                '{"type":"session","version":0}\n'
                '{"type":"request/header","data":{"header":{"config":'
                '{"provider":"deepseek-official","model":"deepseek-v4-pro",'
                '"reasoningEffort":"max"}}}}\n'
            ).encode())
            self.assertEqual(self._call(p, sid=SID), {
                "source": "dsh",
                "model": "deepseek-v4-pro",
                "reasoning_effort": "max",
                "source_session_id": SID,
            })

    def test_plain_jsonl_top_level_config(self):
        """旧假设：config 挂在事件顶层"""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "session.jsonl", (
                '{"type":"request/header","config":'
                '{"provider":"p","model":"m1","reasoningEffort":"high"}}\n'
            ).encode())
            meta = self._call(p)
            self.assertEqual(meta["model"], "m1")
            self.assertEqual(meta["reasoning_effort"], "high")

    def test_request_context_data_fallback(self):
        """兜底：request/context 的 provider/model 直接挂在 data 下"""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "session.jsonl", (
                '{"type":"request/context","data":{"provider":"deepseek-official",'
                '"model":"deepseek-v4-pro"}}\n'
            ).encode())
            meta = self._call(p)
            self.assertEqual(meta["model"], "deepseek-v4-pro")
            self.assertIsNone(meta["reasoning_effort"])

    def test_no_request_event(self):
        """会话文件里没有 request/header 事件：仍在 DSH 环境 → source=dsh、model 留空（不猜）"""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "session.jsonl",
                            b'{"type":"session","version":0}\n{"type":"sandbox/mode","data":{}}\n')
            self.assertEqual(self._call(p), {"source": "dsh", "source_session_id": None})

    @unittest.skipUnless(HAS_ZSTD, "系统无 zstd 命令")
    def test_zstd_file(self):
        """DSH 实际落盘形态：session.jsonl.zstd"""
        with tempfile.TemporaryDirectory() as td:
            plain = self._write(td, "session.jsonl", (
                '{"type":"session","version":0}\n'
                '{"type":"request/header","data":{"header":{"config":'
                '{"provider":"deepseek-official","model":"deepseek-v4-pro",'
                '"reasoningEffort":"max"}}}}\n'
            ).encode())
            p = self._zstd(plain)
            self.assertEqual(self._call(p, sid=SID), {
                "source": "dsh",
                "model": "deepseek-v4-pro",
                "reasoning_effort": "max",
                "source_session_id": SID,
            })

    @unittest.skipUnless(HAS_ZSTD, "系统无 zstd 命令")
    def test_corrupt_zstd(self):
        """损坏的 .zstd 文件：降级为「环境命中但无模型信息」，不抛异常（修复前的崩溃现场）"""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "session.jsonl.zstd", b"\xb5\x00\x00not-zstd-data")
            self.assertEqual(self._call(p), {"source": "dsh", "source_session_id": None})

    def test_binary_plain(self):
        """无 .zstd 后缀的乱字节文件：同样降级不抛异常"""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "session.jsonl", b"\xb5\x00\x01binary")
            self.assertEqual(self._call(p), {"source": "dsh", "source_session_id": None})


if __name__ == "__main__":
    unittest.main()
