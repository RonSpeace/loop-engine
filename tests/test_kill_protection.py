#!/usr/bin/env python3
"""req-108 开发进程安全（禁杀自 PID/任何 kimi 进程）测试：L0 检测纯函数 / L1 注入文本 /
L2 审批硬拒（_auto_approve）/ L3 事件流兜底（_on_update 检测 + dev cancel）。

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试 setUpModule 自动补一份。
"""
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402（共享函数/常量直接引用共享模块）
import dispatcher as d  # noqa: E402（桩实例用 dispatcher 导出的 ACPSession）

SELF_PID = 12345


class L0KillRedlineHit(unittest.TestCase):
    """kill_redline_hit 纯函数正反例（对应 tech.md 6.1 L0）"""

    def test_p_pkill_kimi(self):
        for cmd in ("pkill -f kimi", "pkill kimi-chat-acp", "pkill -9 -f 'kimi acp'",
                    "killall kimi", "pkill -f kimi-chat-acp && sleep 1"):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)
            self.assertTrue(ac.kill_redline_hit(cmd, None), cmd)  # self_pid=None 桩防御仍命中

    def test_p_kill_self_pid(self):
        for cmd in ("kill 12345", "kill -9 12345", "kill -TERM -- 12345",
                    "killpg 12345"):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_p_kill_pipe_kimi(self):
        for cmd in ("kill $(pgrep -f kimi)", "ps aux | grep kimi | xargs kill",
                    "kill -9 $(pgrep -f 'kimi acp')"):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_n_non_kill_commands(self):
        for cmd in ("kill 54321", "kill -9 54321", "pkill -f pytest", "killall pytest",
                    "echo hello", "ls -la", "git status", "pytest", "", "   ",
                    "skill 12345"):  # skill 单词内 kill 无词边界
            self.assertIsNone(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_n_self_pid_none_non_kill(self):
        self.assertIsNone(ac.kill_redline_hit("kill 54321", None))
        self.assertIsNone(ac.kill_redline_hit("pkill -f pytest", None))

    def test_n_non_str(self):
        self.assertIsNone(ac.kill_redline_hit(None, SELF_PID))
        self.assertIsNone(ac.kill_redline_hit(123, SELF_PID))


class L0StructureDataWriteExempt(unittest.TestCase):
    """#113 v5 结构解析版：A 组——数据写入豁免（heredoc/JSON 摘要/说明文字含红线词不误伤，验收 1）"""

    def test_a1_heredoc_data_write_no_hit(self):
        cmd = "cat <<'EOF' > self-test.md\nkill -9 12345\nkimi\nEOF"
        self.assertIsNone(ac.kill_redline_hit(cmd, SELF_PID))
        self.assertIsNone(ac.kill_redline_hit(cmd, None))

    def test_a2_echo_redirect_data_no_hit(self):
        self.assertIsNone(ac.kill_redline_hit('echo "pkill kimi" >> notes.md', SELF_PID))

    def test_a3_python_json_dumps_no_hit(self):
        cmd = "python3 -c 'import json; json.dumps({\"s\": \"kill kimi\"})' > result.json"
        self.assertIsNone(ac.kill_redline_hit(cmd, SELF_PID))

    def test_a4_prose_summary_no_hit(self):
        for t in ('"summary": "清理残留不要用 pkill -f kimi，用 test_instance.sh"',
                  "写 result.json 用 json.dumps，禁止手工拼字符串",
                  "python3 -c 'print(\"hello\")' > out.txt"):
            self.assertIsNone(ac.kill_redline_hit(t, SELF_PID), t)


class L0StructureIntercept(unittest.TestCase):
    """#113 v5 结构解析版：B 组——真实进程控制拦截（验收 2，含 v2/v3/v4/v5 反馈用例）"""

    def test_b1_pkill_quoted_kimi(self):
        for cmd in ("pkill -9 -f 'kimi acp'", "pkill kimi-chat-acp", "pkill -f kimi && sleep 1"):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)
            self.assertTrue(ac.kill_redline_hit(cmd, None), cmd)  # self_pid=None 桩防御仍命中

    def test_b2_python_c_subprocess_kill(self):
        cmd = "python3 -c 'subprocess.run([\"pkill\",\"-f\",\"kimi\"])'"
        self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID))
        self.assertTrue(ac.kill_redline_hit(cmd, None))

    def test_b3_heredoc_write_then_exec(self):
        cmd = "cat > /tmp/x <<EOF\npkill kimi\nEOF\nbash /tmp/x"
        self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID))

    def test_b4_redirect_write_then_exec(self):
        cmd = 'echo "pkill kimi" > /tmp/x && bash /tmp/x'
        self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID))

    def test_b5_kill_self_pid_quoted(self):
        for cmd in ("kill 12345", 'kill "12345"'):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_b6_kill_neg_pid_process_group(self):
        """自身进程组（负 PID）自杀式清理保守命中"""
        for cmd in ("kill -12345", "kill -- -12345"):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_b6a_kill_signal_option_then_self(self):
        """v5 修正：-9 按信号选项跳过，self_pid 才是目标（不得把 -9 当目标 PID 漏检）"""
        self.assertTrue(ac.kill_redline_hit("kill -9 12345", SELF_PID))

    def test_b6b_kill_term_dashdash_self(self):
        self.assertTrue(ac.kill_redline_hit("kill -TERM -- 12345", SELF_PID))

    def test_b6c_kill_signal_other_pid_no_hit(self):
        self.assertIsNone(ac.kill_redline_hit("kill -9 54321", SELF_PID))

    def test_b7_killall_kimi_and_pgrep(self):
        for cmd in ("killall kimi", "kill $(pgrep -f kimi)", "kill -9 $(pgrep -f 'kimi acp')"):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_b8_pipeline_xargs_kill(self):
        self.assertTrue(ac.kill_redline_hit("ps aux | grep kimi | xargs kill", SELF_PID))

    def test_b9_exec_chain_sudo(self):
        self.assertTrue(ac.kill_redline_hit("sudo kill 12345", SELF_PID))
        self.assertIsNone(ac.kill_redline_hit("sudo kill 54321", SELF_PID))


class L0StructureChainOptions(unittest.TestCase):
    """#113 accept 问题 1 修复：执行链动词带选项/参数时漏拦（11 种链变体，legacy 均命中）
    覆盖 accept.md 第六/七节复核的 sudo -u / timeout 5 / env -i / xargs -n1 / sudo -- /
    xargs -I{} / env VAR=x / timeout --kill-after=5 / env -i python3 全部变体"""

    def test_chain_option_hits(self):
        for cmd in ("sudo -u root kill 12345",
                    "sudo -n kill 12345",
                    "timeout 5 bash -c 'kill 12345'",
                    "timeout 5s bash -c 'kill 12345'",
                    "timeout --signal TERM 5 bash -c 'kill 12345'",
                    "timeout 5 bash -c 'pkill kimi'",
                    "env -i bash -c 'kill 12345'",
                    "env --unset PATH bash -c 'kill 12345'",
                    "env --unset PATH python3 -c 'import os; os.kill(12345, 9)'",
                    "xargs -n1 kill 12345",
                    "xargs --max-args 1 kill 12345",
                    "timeout 5 kill 12345",
                    "sudo -- bash -c 'kill 12345'",
                    "xargs -I{} sh -c 'pkill kimi'",
                    "env VAR=x bash -c 'kill 12345'",
                    "timeout --kill-after=5 10 bash -c 'kill 12345'",
                    "env -i python3 -c 'subprocess.run([\"pkill\",\"-f\",\"kimi\"])'"):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_chain_option_non_hits(self):
        """回归防误伤：非 self PID / 无红线词不命中"""
        for cmd in ("sudo -u root kill 54321",
                    "sudo -n kill 54321",
                    "timeout 5 bash -c 'kill 54321'",
                    "timeout --signal TERM 5 bash -c 'kill 54321'",
                    "env -i bash -c 'kill 54321'",
                    "env --unset PATH bash -c 'kill 54321'",
                    "xargs -n1 kill 54321",
                    "xargs --max-args 1 kill 54321",
                    "sudo -- bash -c 'kill 54321'",
                    "env VAR=x bash -c 'kill 54321'"):
            self.assertIsNone(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_chain_data_write_via_chain_no_hit(self):
        """链上数据写入豁免不回归：sudo/env 包 echo 写文件（含红线词）不误伤"""
        for cmd in ('sudo echo "pkill kimi" >> notes.md',
                    "env -i cat <<'EOF' > self-test.md\nkill -9 12345\nkimi\nEOF"):
            self.assertIsNone(ac.kill_redline_hit(cmd, SELF_PID), cmd)


class L0OsKillDirect(unittest.TestCase):
    """#113 accept 问题 2 修复：python os.kill/os.killpg 直杀 self（含负 PID 进程组、PID 0）命中"""

    def test_os_kill_self_pid_hit(self):
        for cmd in ("python3 -c 'import os; os.kill(12345, 9)'",
                    "python3 -c 'import os; os.kill(-12345, 9)'",   # 负 PID = 进程组
                    "python3 -c 'import os; os.killpg(12345, 9)'",
                    "python3 -c 'import os; os.kill(0, 9)'"):        # PID 0 = 当前进程组
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)
        self.assertTrue(ac.kill_redline_hit("python3 -c 'import os; os.kill(0, 9)'", None))

    def test_os_kill_other_pid_no_hit(self):
        self.assertIsNone(ac.kill_redline_hit("python3 -c 'import os; os.kill(54321, 9)'", SELF_PID))
        self.assertIsNone(ac.kill_redline_hit("python3 -c 'import os; os.getpid()'", SELF_PID))


class L0KillZeroProcessGroup(unittest.TestCase):
    """#113 accept 问题 3：kill 0（当前进程组信号，潜在自杀向量）保守命中；kill -0 <other> 存在性检查不误伤"""

    def test_kill_zero_pg_hit(self):
        for cmd in ("kill 0", "kill -9 0", "kill -TERM 0"):
            self.assertTrue(ac.kill_redline_hit(cmd, SELF_PID), cmd)

    def test_kill_dash_zero_check_no_hit(self):
        self.assertIsNone(ac.kill_redline_hit("kill -0 54321", SELF_PID))
        self.assertIsNone(ac.kill_redline_hit("kill -0 54321", None))


class L0StructureConservative(unittest.TestCase):
    """#113 v5：C 组——安全不确定即拒绝（stdin 执行/解释器 -c/解析异常兜底保守）"""

    def test_c1_pipe_to_bash(self):
        self.assertTrue(ac.kill_redline_hit('echo "pkill kimi" | bash', SELF_PID))

    def test_c1b_heredoc_feed_bash(self):
        self.assertTrue(ac.kill_redline_hit("bash <<EOF\npkill kimi\nEOF", SELF_PID))

    def test_c2_bash_c_kill_self(self):
        self.assertTrue(ac.kill_redline_hit('bash -c "kill 12345"', SELF_PID))
        self.assertIsNone(ac.kill_redline_hit('bash -c "kill 54321"', SELF_PID))

    def test_c3_unbalanced_quote_fallback(self):
        """引号不平衡 → 兜底全文本规则：含红线词保守命中，无红线词不误伤"""
        self.assertTrue(ac.kill_redline_hit("unbalanced 'kill 12345", SELF_PID))
        self.assertIsNone(ac.kill_redline_hit("unbalanced 'quote", SELF_PID))


class L2PrefixExtraction(unittest.TestCase):
    """#113 v5：D 组——L2 完整前缀输入（先剥离「Requesting approval to Running: 」再检测，验收 1/2）"""

    def test_d1_prefix_pkill_hit(self):
        self.assertTrue(ac.kill_redline_hit(
            ac._extract_command("Requesting approval to Running: pkill -9 -f 'kimi acp'"), SELF_PID))

    def test_d2_prefix_heredoc_write_no_hit(self):
        cmd = "Requesting approval to Running: cat <<'EOF' > 产物.md\nkill 12345\nEOF"
        self.assertIsNone(ac.kill_redline_hit(ac._extract_command(cmd), SELF_PID))

    def test_d3_prefix_python_c_hit(self):
        cmd = "Requesting approval to Running: python3 -c 'subprocess.run([\"pkill\",\"-f\",\"kimi\"])'"
        self.assertTrue(ac.kill_redline_hit(ac._extract_command(cmd), SELF_PID))

    def test_d_extract_command_variants(self):
        self.assertEqual(ac._extract_command("ls -la"), "ls -la")
        self.assertEqual(ac._extract_command("Requesting approval to Running: kill 12345"), "kill 12345")
        self.assertEqual(ac._extract_command({"command": "pkill -f kimi"}), "pkill -f kimi")


class L1KillRedlineText(unittest.TestCase):
    """kill_redline_text / append_kill_redline 注入文本（对应 tech.md 6.1 L1）"""

    def test_p_text_contains_pid_and_redline(self):
        t = ac.kill_redline_text(SELF_PID)
        self.assertIn(str(SELF_PID), t)
        self.assertIn("禁止 kill/killpg/pkill", t)
        self.assertIn("pkill -f kimi", t)
        self.assertIn("kimi-chat-acp", t)

    def test_p_append_semantics(self):
        base = "你好"
        out = ac.append_kill_redline(base, SELF_PID)
        self.assertTrue(out.startswith(base + "\n\n"))
        self.assertIn("【进程安全红线】", out)
        self.assertEqual(out, base + "\n\n" + ac.kill_redline_text(SELF_PID))


def _perm_params(text, title="Bash"):
    return {"options": [{"optionId": "approve_once", "kind": "allow_once"},
                        {"optionId": "reject", "kind": "reject_once"}],
            "toolCall": {"title": title,
                         "content": [{"type": "content",
                                      "content": {"type": "text", "text": text}}]}}


def _approval_stub(role):
    """_auto_approve 桩：不 Popen；proc 以真实 pid 形态挂载（供禁杀分支取 pid）"""
    s = d.ACPSession.__new__(d.ACPSession)
    s.req_id = 99999
    s.role = role
    s.readonly = None  # 走角色兜底判断
    s.proc = types.SimpleNamespace(pid=SELF_PID)
    s.workdir = Path(tempfile.gettempdir())
    s.artifact_root = Path(tempfile.gettempdir()) / "art"
    return s


class L2ApprovalKillRedline(unittest.TestCase):
    """_auto_approve 禁杀分支（对应 tech.md 6.1 L2；accept/只读硬拒，dev 全放行不变）"""

    def _approved(self, outcome):
        return outcome.get("outcome", {}).get("outcome") == "selected"

    def test_accept_kill_reject(self):
        s = _approval_stub("accept")
        for cmd in ("Requesting approval to Running: kill 12345",
                    "Requesting approval to Running: kill -9 12345",
                    "Requesting approval to Running: pkill -f kimi",
                    "Requesting approval to Running: kill $(pgrep -f kimi)"):
            self.assertFalse(self._approved(s._auto_approve(_perm_params(cmd))), cmd)

    def test_accept_legal_kill_allow(self):
        """合法清理不误伤（回归 :740-743 放行语义）"""
        s = _approval_stub("accept")
        for cmd in ("Requesting approval to Running: kill 54321",
                    "Requesting approval to Running: pkill -f pytest",
                    "Requesting approval to Running: pytest",
                    "Requesting approval to Running: ls -la"):
            self.assertTrue(self._approved(s._auto_approve(_perm_params(cmd))), cmd)
        # git 提交类仍被拒（既有 accept 语义不变）
        self.assertFalse(self._approved(s._auto_approve(_perm_params("Requesting approval to Running: git commit -m x"))))

    def test_readonly_kill_reject(self):
        """只读（analysis）fail-closed 语义不变：禁杀命中显式拒绝"""
        s = _approval_stub("analysis")
        for cmd in ("Requesting approval to Running: kill 12345",
                    "Requesting approval to Running: pkill -f kimi"):
            self.assertFalse(self._approved(s._auto_approve(_perm_params(cmd))), cmd)

    def test_dev_full_allow_unchanged(self):
        s = _approval_stub("dev")
        self.assertTrue(self._approved(s._auto_approve(_perm_params("Requesting approval to Running: kill 12345"))))
        self.assertTrue(self._approved(s._auto_approve(_perm_params("Requesting approval to Running: pkill -f kimi"))))


def _update_stub(role):
    """_on_update 桩：_emit 收集到 s.events，cancel 打桩到 s.cancelled"""
    s = d.ACPSession.__new__(d.ACPSession)
    s.req_id = 99999
    s.role = role
    s.session_id = "test-session"
    s.proc = types.SimpleNamespace(pid=SELF_PID)
    s._msg_buf = []
    s._buf_lock = threading.Lock()
    s._tool_calls = {}
    s._tool_posted = set()
    s.events = []
    s.cancelled = []

    def emit(kind, content, session_id=None):
        s.events.append((kind, content))

    s._emit = emit
    s.cancel = lambda: s.cancelled.append(1)
    return s


def _tool_update(cmd, tid="t1"):
    return {"sessionUpdate": "tool_call_update", "toolCallId": tid,
            "rawInput": {"command": cmd}, "title": f"Running: {cmd}"}


class L3EventStreamDetection(unittest.TestCase):
    """_on_update 事件流兜底（对应 tech.md 6.1 L3：dev 命中 cancel；非 dev 仅记录）"""

    def test_dev_kill_self_pid_cancels(self):
        s = _update_stub("dev")
        s._on_update(_tool_update("kill 12345"))
        self.assertEqual(s.cancelled, [1])
        kinds = [k for k, _ in s.events]
        self.assertIn("system", kinds)
        self.assertTrue(any("自杀式进程清理被拦截" in c for k, c in s.events if k == "system"))

    def test_dev_pkill_kimi_cancels(self):
        s = _update_stub("dev")
        s._on_update(_tool_update("pkill -f kimi"))
        self.assertEqual(s.cancelled, [1])
        self.assertTrue(any("自杀式进程清理被拦截" in c for k, c in s.events if k == "system"))

    def test_dev_normal_commands_no_intercept(self):
        for cmd in ("pytest", "echo ok", "git status", "kill 54321"):
            s = _update_stub("dev")
            s._on_update(_tool_update(cmd))
            self.assertEqual(s.cancelled, [], cmd)
            self.assertFalse(any(k == "system" for k, _ in s.events), cmd)
            # 工具摘要事件照常上报
            self.assertTrue(any(k == "tool" for k, _ in s.events), cmd)

    def test_non_dev_kill_no_cancel_only_record(self):
        s = _update_stub("analysis")
        s._on_update(_tool_update("kill 12345"))
        self.assertEqual(s.cancelled, [])
        self.assertTrue(any("已被审批拒绝" in c for k, c in s.events if k == "system"))

    def test_dedup_no_repeat_detection(self):
        s = _update_stub("dev")
        s._on_update(_tool_update("kill 12345"))
        s._on_update(_tool_update("kill 12345"))  # 已上报 tid，去重直接跳过
        self.assertEqual(s.cancelled, [1])

    def test_dev_heredoc_json_write_no_cancel(self):
        """#113 D4：L3 dev 用 heredoc/json.dumps 写产物（含红线词）→ 不 cancel，无取消信号（验收 1）"""
        for cmd in ("cat <<'EOF' > self-test.md\nkill -9 12345\nkimi\nEOF",
                    "python3 -c 'import json; json.dumps({\"s\": \"kill kimi\"})' > result.json"):
            s = _update_stub("dev")
            s._on_update(_tool_update(cmd))
            self.assertEqual(s.cancelled, [], cmd)
            self.assertFalse(any("自杀式进程清理被拦截" in c for k, c in s.events if k == "system"), cmd)
            self.assertTrue(any(k == "tool" for k, _ in s.events), cmd)

    def test_dev_python_c_pkill_cancels(self):
        """#113 D5：L3 dev 解释器代码 pkill → cancel + 取消信号"""
        s = _update_stub("dev")
        s._on_update(_tool_update("python3 -c 'subprocess.run([\"pkill\",\"-f\",\"kimi\"])'"))
        self.assertEqual(s.cancelled, [1])
        self.assertTrue(any("自杀式进程清理被拦截" in c for k, c in s.events if k == "system"))

    def test_dev_write_then_exec_cancels(self):
        """#113 D5：L3 dev 写脚本后执行 → cancel + 取消信号"""
        s = _update_stub("dev")
        s._on_update(_tool_update('echo "pkill kimi" > /tmp/x && bash /tmp/x'))
        self.assertEqual(s.cancelled, [1])
        self.assertTrue(any("自杀式进程清理被拦截" in c for k, c in s.events if k == "system"))


if __name__ == "__main__":
    unittest.main()
