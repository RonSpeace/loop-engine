#!/usr/bin/env python3
"""#40 健康角标渲染断言（node harness）：服务端 state 五态 → 文案 + 档位（颜色语义类）。

#40 把角标语义从「ok/age」改为「state」（唯一判定在服务端 /api/dispatcher-health）后，
同一元素上其它展示属性（`.health.ok/.bad/.warn` 档位）必须一并按新语义绑定，不能残留
`!h.ok`/`age` 旧字段。本用例按 test_diff_render 模式从 static/board.js 提取 refreshHealth
（含 shutdownText）交给 node 执行，桩掉 fetch/document，逐个断言 textContent 与 className。

运行（worktree 内，需要 node；用 main 工作区的 .venv）：
    .venv/bin/python \\
        -m pytest tests/test_board_health_badge.py -q
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._frontend_src import frontend_js  # noqa: E402


def _extract_badge_js(js):
    """从 `function shutdownText(h)` 截到 `refreshHealth();` 首次调用之前（不锁固定长度窗口）。"""
    start = js.index("function shutdownText(h)")
    end = js.index("refreshHealth();", js.index("async function refreshHealth()"))
    return js[start:end]


class HealthBadgeRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.badge_js = _extract_badge_js(frontend_js())

    def _run_cases(self, payloads):
        if not self.node:
            self.skipTest("node 不可用")
        script = "\n".join([
            "const els = {healthBadge: {className:'', title:'', textContent:''},",
            "              healthText: {className:'', title:'', textContent:''}};",
            "global.document = {hidden: false, getElementById: id => els[id] || null,",
            "                   addEventListener: () => {}};",
            "const payloads = %s;" % json.dumps(payloads),
            "async function main() {",
            "  const out = [];",
            "  for (const p of payloads) {",
            "    global.fetch = async () => ({json: async () => p});",
            "    await refreshHealth();",
            "    out.push({cls: els.healthBadge.className, text: els.healthText.textContent,",
            "              title: els.healthBadge.title});",
            "  }",
            "  console.log(JSON.stringify(out));",
            "}",
            self.badge_js,
            "main();",
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
            f.write(script)
            tmp = f.name
        try:
            proc = subprocess.run([self.node, tmp], capture_output=True, text=True, timeout=60)
        finally:
            Path(tmp).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_h1_five_states_label_and_style(self):
        """五态 + unknown 的文案与档位：已连接=ok / 凭据未加载、显式停用、凭据未配置=warn /
        心跳过期、网络连接失败、调度器未上报=bad；档位不绑旧 ok 字段。"""
        payloads = [
            {"state": "connected", "reason": None, "age": 3, "ok": True,
             "feishu": {"state": "connected", "pid_status": "ok", "reason": None}},
            {"state": "not_loaded", "reason": "调度器尚未加载当前凭据", "age": 3, "ok": True,
             "feishu": {"state": "not_loaded", "pid_status": "missing"}},
            {"state": "disabled", "reason": "REQ_BOARD_NO_FEISHU=1（显式停用飞书通道）",
             "age": 3, "ok": True, "feishu": {"state": "disabled", "pid_status": "ok"}},
            {"state": "disabled", "reason": "feishu.json 不可读，请检查文件权限",
             "age": 3, "ok": True, "feishu": {"state": "disabled", "pid_status": "ok"}},
            {"state": "stale", "reason": "最后心跳 400s 前，上次上报 phase=connected",
             "age": 400, "ok": False, "feishu": {"state": "stale", "pid_status": "dead"}},
            {"state": "failed", "reason": "连接失败：TCP 超时", "age": 3, "ok": True,
             "feishu": {"state": "failed", "pid_status": "ok"}},
            {"age": None, "ok": False, "feishu": {"state": "unknown", "pid_status": "missing"}},
        ]
        want = [("health ok", "已连接"),
                ("health warn", "凭据未加载"),
                ("health warn", "显式停用"),
                ("health warn", "凭据未配置"),
                ("health bad", "心跳过期"),
                ("health bad", "网络连接失败"),
                ("health bad", "调度器未上报")]
        out = self._run_cases(payloads)
        for got, (cls, label) in zip(out, want):
            with self.subTest(label=label):
                self.assertEqual(got["cls"], cls)
                self.assertIn(label, got["text"])
        # 显式停用与凭据未配置互不冒充
        self.assertNotIn("凭据未配置", out[2]["text"])
        self.assertNotIn("显式停用", out[3]["text"])

    def test_h2_pid_note_and_pending_branch(self):
        """pid 说明只在服务端写进 reason 时附在角标后（无心跳证据不注）；connected + board_restart_pending 走琥珀待重启。"""
        payloads = [
            {"state": "connected", "reason": "心跳 pid 非当前调度器进程（pid 已不是调度器进程）",
             "age": 3, "ok": True,
             "feishu": {"state": "connected", "pid_status": "dead",
                        "reason": "心跳 pid 非当前调度器进程（pid 已不是调度器进程）"}},
            {"state": "connected", "reason": None, "age": 3, "ok": True,
             "board_restart_pending": {"at": 1, "req_id": 8},
             "feishu": {"state": "connected", "pid_status": "ok"}},
            {"state": "disabled", "reason": "REQ_BOARD_NO_FEISHU=1（显式停用飞书通道）",
             "age": None, "ok": False,
             "feishu": {"state": "disabled", "pid_status": "missing"}},
        ]
        out = self._run_cases(payloads)
        self.assertEqual(out[0]["cls"], "health ok")
        self.assertIn("已连接", out[0]["text"])
        self.assertIn("心跳 pid 非当前调度器进程", out[0]["text"])
        self.assertNotIn("心跳过期", out[0]["text"])
        self.assertEqual(out[1]["cls"], "health warn")
        self.assertIn("看板有新改动待重启", out[1]["text"])
        self.assertIn("Ctrl-C 后重跑 run_local.sh", out[1]["text"])
        # board_env 显式停用（无心跳 → reason 无 pid 说明）：角标只显示状态名，不注 pid
        self.assertEqual(out[2]["cls"], "health warn")
        self.assertIn("显式停用", out[2]["text"])
        self.assertNotIn("心跳 pid 非当前调度器进程", out[2]["text"])


if __name__ == "__main__":
    unittest.main()
