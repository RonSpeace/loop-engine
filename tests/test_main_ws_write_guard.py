#!/usr/bin/env python3
"""req-336 写角色 Bash 命令机制防护（主工作区路径+写操作拦截）测试：
L0 main_ws_write_hit 纯函数正反例（拦截/放行用例集，含「主路径出现但写目标合法」放行）/
L2 _auto_approve 审批桩（dev/accept 越界写被拒 + 事件留痕，既有放行语义不变）。
req-344：解析失败降级（写动词字样+主路径字样共现才拦）、resume 审批文本（多段 content
含撇号描述段）放行、toolCall.rawInput 结构化 command 优先于拼接文本。

运行（worktree 内，用 main 工作区的 .venv，含 lark_oapi 等依赖）：
    .venv/bin/python -m unittest discover -s tests -v

import dispatcher 需要 REPO_DIR/projects.json 存在（gitignore，worktree 无）；本测试 setUpModule 自动补一份。
"""
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402（桩实例用 dispatcher 导出的 ACPSession）

MAIN = "/Users/dev/project/req-board"          # 主工作区（真实仓库路径）
WT = "/Users/dev/.req-board/worktrees/req-board/dev/req-336/req-board"  # 本需求 worktree
ART = "/Users/dev/.req-board/artifacts/req-board/req-336"               # 产物目录
ROOTS = [MAIN]
PREFIX = "Requesting approval to Running: "


class L0MainWsWriteHit(unittest.TestCase):
    """main_ws_write_hit 纯函数：拦截用例集"""

    def test_disabled_without_roots(self):
        """main_roots 未配置（None/空）→ 护栏未启用，任何命令不拦（向后兼容）"""
        for roots in (None, []):
            self.assertIsNone(ac.main_ws_write_hit(f"echo x > {MAIN}/board.py", roots, base=WT), roots)

    def test_non_string_and_blank(self):
        self.assertIsNone(ac.main_ws_write_hit(None, ROOTS, base=WT))
        self.assertIsNone(ac.main_ws_write_hit("", ROOTS, base=WT))
        self.assertIsNone(ac.main_ws_write_hit("   ", ROOTS, base=WT))

    def test_hit_redirect(self):
        for cmd in (f"echo x > {MAIN}/board.py",
                    f"echo x >> {MAIN}/notes.md",
                    f"cat a > {MAIN}/x.log",
                    f"python3 -c 'print(1)' > {MAIN}/out.txt",
                    f"grep foo board.py 2> {MAIN}/err.log",
                    f"echo hi > {MAIN}/a && echo ok",
                    f"echo ok && echo hi >> {MAIN}/b"):
            self.assertTrue(ac.main_ws_write_hit(cmd, ROOTS, base=WT), cmd)

    def test_hit_heredoc_redirect(self):
        cmd = f"cat <<'EOF' > {MAIN}/new.py\nprint(1)\nEOF"
        self.assertTrue(ac.main_ws_write_hit(cmd, ROOTS, base=WT))

    def test_hit_tee(self):
        for cmd in (f"echo x | tee {MAIN}/f",
                    f"echo x | tee -a {MAIN}/f",
                    f"echo x | tee /tmp/ok {MAIN}/f"):
            self.assertTrue(ac.main_ws_write_hit(cmd, ROOTS, base=WT), cmd)

    def test_hit_git_c_write_subcmd(self):
        """git -C 路径 + 写子命令双条件同时满足才拦"""
        for cmd in (f"git -C {MAIN} add .",
                    f"git -C {MAIN} commit -m x",
                    f"git -C {MAIN} commit -m 'fix: bug'",
                    f"git -C {MAIN} push origin main",
                    f"git -C {MAIN} checkout -b f-x",
                    f"git -C {MAIN} reset --hard HEAD",
                    f"git -C {MAIN} merge f-x",
                    f"git -C {WT} -C {MAIN} commit -m x"):
            self.assertTrue(ac.main_ws_write_hit(cmd, ROOTS, base=WT), cmd)

    def test_hit_rm_mv_chmod(self):
        for cmd in (f"rm {MAIN}/board.py",
                    f"rm -rf {MAIN}",
                    f"mv /tmp/x {MAIN}/",
                    f"mv {MAIN}/a /tmp/",
                    f"chmod 755 {MAIN}/x.sh",
                    f"chmod -R 755 {MAIN}/scripts"):
            self.assertTrue(ac.main_ws_write_hit(cmd, ROOTS, base=WT), cmd)

    def test_hit_relative_escape(self):
        """相对路径以会话 workdir（worktree）解析，../ 逃逸到主工作区同样拦截"""
        cmd = "echo x > ../../../../../../project/req-board/board.py"
        self.assertTrue(ac.main_ws_write_hit(cmd, ROOTS, base=WT), cmd)

    def test_hit_symlink(self):
        """软链指向主工作区的路径 resolve 后同样命中（#303 软链绕过防护）"""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)  # 防 $TMPDIR 泄漏
        real = tmp / "real"
        real.mkdir()
        link = tmp / "link"
        link.symlink_to(real)
        hit = ac.main_ws_write_hit(f"echo x > {link}/f", [str(real)], base=WT)
        self.assertTrue(hit)

    def test_hit_parse_failure_fail_closed(self):
        """解析失败拦截（#344 降级后语义）：引号不平衡且写动词字样+主路径字样共现 → 拦；
        git -C 缺参数属解析歧义 → 仍 fail-closed 保守拦截"""
        self.assertTrue(ac.main_ws_write_hit(f"echo 'x > {MAIN}", ROOTS, base=WT))
        self.assertTrue(ac.main_ws_write_hit("git -C", ROOTS, base=WT))


class L0ParseFailureDegrade(unittest.TestCase):
    """#344 解析失败降级：写动词字样与主工作区路径字样共现才拦，否则放行"""

    def test_resume_text_with_apostrophe_allowed(self):
        """复现 #343 现场：命令段 + 含撇号描述段拼接 → 引号计数为奇解析失败，但无
        主路径字样 → 放行（需求原文复现用例）"""
        texts = (
            f'{PREFIX}git commit -m "[auto] #343 fix" The user\'s request',
            f"{PREFIX}grep -n TODO board.py session's output",
            f"{PREFIX}git add board.py it's fine",
            f"{PREFIX}echo done && ls the user's files",
        )
        for txt in texts:
            self.assertIsNone(ac.main_ws_write_hit(txt, ROOTS, base=WT), txt)

    def test_degrade_still_blocks_write_with_main_path(self):
        """降级不放水：解析失败但写动词字样（>/rm/git）与主路径字样共现 → 仍拦"""
        for txt in (f"echo 'x > {MAIN}/board.py",
                    f"rm -rf '{MAIN}",
                    f"git -C '{MAIN} commit -m x"):
            self.assertTrue(ac.main_ws_write_hit(txt, ROOTS, base=WT), txt)

    def test_degrade_allows_readonly_and_no_path(self):
        """纯读命令带主路径字样 / 写动词字样无路径 → 放行"""
        self.assertIsNone(ac.main_ws_write_hit(f"cat '{MAIN}/board.py", ROOTS, base=WT))
        self.assertIsNone(ac.main_ws_write_hit("echo 'unclosed > /tmp/x", ROOTS, base=WT))
        self.assertIsNone(ac.main_ws_write_hit("echo 'it's", ROOTS, base=WT))

    # ---- 放行用例集：主工作区路径出现但写目标合法 / 纯 worktree 操作 ----
    def test_allow_main_path_but_legal_target(self):
        """主路径出现但写目标合法（验收标准明列放行）"""
        for cmd in (f"{MAIN}/.venv/bin/python -m unittest discover -s tests -v",
                    f"{MAIN}/.venv/bin/python -m pytest tests/ > /tmp/out.log",
                    f"git -C {MAIN} log --oneline -5",
                    f"git -C {MAIN} status",
                    f"git -C {MAIN} diff --stat",
                    f"git -C {MAIN} branch --show-current",
                    f"git -C {MAIN} rev-parse HEAD",
                    f"git -C {MAIN} reflog",
                    f"cd {MAIN} && git log -1",
                    f"ls {MAIN}",
                    f"cat {MAIN}/board.py | head -20",
                    f"cat {MAIN}/board.py"):
            self.assertIsNone(ac.main_ws_write_hit(cmd, ROOTS, base=WT), cmd)

    def test_allow_worktree_and_tmp_targets(self):
        """worktree 内正常操作与 /tmp/产物目录写入零影响"""
        for cmd in ("echo x > /tmp/x.log",
                    "echo x > result.json",
                    f"echo x > {ART}/result.json",
                    f"python3 -c 'import json; print(json.dumps({{\"s\": 1}}))' > {ART}/result.json",
                    f"echo x > {WT}/board.py",
                    f"echo x | tee {WT}/f",
                    "git add . && git commit -m x",
                    f"git -C {WT} commit -m x",
                    "rm -rf /tmp/req_board_test",
                    "mv board.py board.py.bak",
                    "chmod +x scripts/test_instance.sh",
                    "tee /tmp/x",
                    'echo "a > b"',
                    "echo ok 2>&1 | tail -1"):
            self.assertIsNone(ac.main_ws_write_hit(cmd, ROOTS, base=WT), cmd)


def _perm_params(text, title="Bash", extra_texts=None, raw_input=None):
    """审批桩：extra_texts 模拟 resume/续跑会话审批 toolCall 的附加描述段（多段 content）；
    raw_input 模拟 toolCall.rawInput 结构化参数（dict 含 command）"""
    content = [{"type": "content",
                "content": {"type": "text", "text": text}}]
    for t in extra_texts or []:
        content.append({"type": "content",
                        "content": {"type": "text", "text": t}})
    tc = {"title": title, "content": content}
    if raw_input is not None:
        tc["rawInput"] = raw_input
    return {"options": [{"optionId": "approve_once", "kind": "allow_once"},
                        {"optionId": "reject", "kind": "reject_once"}],
            "toolCall": tc}


def _approval_stub(role, main_ws=None):
    """_auto_approve 桩：不 Popen；event_sink 收集事件到 s.events"""
    s = d.ACPSession.__new__(d.ACPSession)
    s.req_id = 99999
    s.role = role
    s.readonly = None  # 走角色兜底判断
    s.proc = types.SimpleNamespace(pid=12345)
    s.workdir = Path(WT)
    s.artifact_root = Path(ART)
    s.session_id = "test-session"
    s.main_ws_roots = [Path(r).resolve() for r in (main_ws or [])] or None
    s.events = []

    def sink(req_id, kind, content, session_id=None):
        s.events.append((kind, content))

    s.event_sink = sink
    return s


class L2ApprovalMainWsGuard(unittest.TestCase):
    """_auto_approve 写主工作区护栏（dev/accept 越界写被拒 + 事件留痕；既有语义不变）"""

    def _approved(self, outcome):
        return outcome.get("outcome", {}).get("outcome") == "selected"

    def test_dev_write_main_ws_reject(self):
        """dev 越界写主工作区：重定向/tee/git -C 写/rm 全拒，且拦截事件留痕"""
        s = _approval_stub("dev", [MAIN])
        for cmd in (f"echo x > {MAIN}/board.py",
                    f"echo x | tee {MAIN}/f",
                    f"git -C {MAIN} commit -m x",
                    f"rm {MAIN}/board.py"):
            out = s._auto_approve(_perm_params(PREFIX + cmd))
            self.assertFalse(self._approved(out), cmd)
        self.assertTrue(any(k == "system" and "写主工作区命令已被审批拦截" in c
                            for k, c in s.events))

    def test_dev_main_path_legal_target_allow(self):
        """dev 主路径出现但写目标合法 → 放行（.venv python 重定向 /tmp、git -C log 只读诊断）"""
        s = _approval_stub("dev", [MAIN])
        for cmd in (f"{MAIN}/.venv/bin/python -m pytest tests/ > /tmp/out.log",
                    f"git -C {MAIN} log --oneline -5",
                    f"git -C {MAIN} status",
                    "git add . && git commit -m x",
                    "echo x > /tmp/x.log",
                    "pytest"):
            out = s._auto_approve(_perm_params(PREFIX + cmd))
            self.assertTrue(self._approved(out), cmd)
        self.assertEqual(s.events, [])

    def test_dev_no_roots_guard_off(self):
        """main_ws_roots 未注入 → 护栏未启用，dev 全放行语义与现状一致"""
        s = _approval_stub("dev")
        for cmd in (f"echo x > {MAIN}/board.py",
                    "kill 12345",
                    "pkill -f kimi"):
            self.assertTrue(self._approved(s._auto_approve(_perm_params(PREFIX + cmd))), cmd)

    def test_dev_kill_redline_unaffected(self):
        """回归 test_dev_full_allow_unchanged：kill 非写主工作区操作，护栏启用后仍放行"""
        s = _approval_stub("dev", [MAIN])
        self.assertTrue(self._approved(s._auto_approve(_perm_params(PREFIX + "kill 12345"))))
        self.assertTrue(self._approved(s._auto_approve(_perm_params(PREFIX + "pkill -f kimi"))))

    def test_accept_write_main_ws_reject(self):
        s = _approval_stub("accept", [MAIN])
        for cmd in (f"echo x > {MAIN}/board.py",
                    f"git -C {MAIN} push",
                    f"mv /tmp/x {MAIN}/"):
            out = s._auto_approve(_perm_params(PREFIX + cmd))
            self.assertFalse(self._approved(out), cmd)
        self.assertTrue(any(k == "system" for k, _ in s.events))

    def test_accept_existing_semantics_unchanged(self):
        """accept 既有语义不变：COMMIT_RE 提交类仍拒；合法命令（含 git -C 主路径只读）放行"""
        s = _approval_stub("accept", [MAIN])
        self.assertFalse(self._approved(s._auto_approve(_perm_params(PREFIX + "git commit -m x"))))
        for cmd in (f"git -C {MAIN} log",
                    f"{MAIN}/.venv/bin/python -m pytest",
                    "pytest"):
            self.assertTrue(self._approved(s._auto_approve(_perm_params(PREFIX + cmd))), cmd)

    def test_readonly_route_unaffected(self):
        """只读角色（analysis）不走本护栏：#357 v2 审批预检放行后由 terminal 层复核
        （fail-closed 语义不变，越界写仍被拒）"""
        s = _approval_stub("analysis", [MAIN])
        self.assertTrue(self._approved(s._auto_approve(_perm_params(PREFIX + f"echo x > {MAIN}/board.py"))))
        self.assertFalse(any("写主工作区命令已被审批拦截" in c for _, c in s.events))
        with self.assertRaises(PermissionError):
            s._terminal_approve(f"echo x > {MAIN}/board.py")


class L2ResumeApprovalText(L2ApprovalMainWsGuard):
    """#344 resume/续跑会话审批形态：多段 content（命令段 + 含撇号描述段）不再误拦"""

    def test_dev_resume_text_allowed(self):
        """复现 #343 误拦命令集（grep/git add/git commit/echo+ls）+ 撇号描述段 → 全放行"""
        s = _approval_stub("dev", [MAIN])
        desc = "The user's request continues the session's work"
        for cmd in ("grep -n TODO board.py",
                    "git add acp_shared.py tests/test_main_ws_write_guard.py",
                    'git commit -m "[auto] #344 fix guard"',
                    "echo done && ls"):
            out = s._auto_approve(_perm_params(PREFIX + cmd, extra_texts=[desc]))
            self.assertTrue(self._approved(out), cmd)
        self.assertEqual(s.events, [])

    def test_dev_resume_text_real_write_still_rejected(self):
        """描述段干扰下真实越界写仍拦：解析失败降级判定写动词+主路径共现"""
        s = _approval_stub("dev", [MAIN])
        out = s._auto_approve(_perm_params(PREFIX + f"echo x > {MAIN}/board.py",
                                           extra_texts=["The user's request"]))
        self.assertFalse(self._approved(out))
        self.assertTrue(any(k == "system" and "写主工作区命令已被审批拦截" in c
                            for k, c in s.events))

    def test_rawinput_structured_command_preferred(self):
        """#344 结构化输入优先：rawInput.command 为准，拼接文本（撇号+主路径字样）不参与判定"""
        s = _approval_stub("dev", [MAIN])
        # 拼接文本同时含撇号、`>` 与主路径字样（描述段），但结构化命令合法 → 放行
        out = s._auto_approve(_perm_params(
            PREFIX + "git commit",
            extra_texts=[f"writes like > {MAIN}/x are blocked; it's the user's call"],
            raw_input={"command": 'git commit -m "[auto] #344 fix"'}))
        self.assertTrue(self._approved(out))
        # 结构化命令本身越界写主工作区 → 拦截
        out = s._auto_approve(_perm_params(
            PREFIX + "echo x", extra_texts=["it's fine"],
            raw_input={"command": f"echo x > {MAIN}/board.py"}))
        self.assertFalse(self._approved(out))

    def test_accept_resume_text_kill_redline_structured(self):
        """#344 accept 禁杀红线同用结构化输入：描述段含 kill/kimi 词 + 撇号不再误拦"""
        s = _approval_stub("accept", [MAIN])
        out = s._auto_approve(_perm_params(
            PREFIX + "git status",
            extra_texts=["don't kill the kimi session"],
            raw_input={"command": "git status"}))
        self.assertTrue(self._approved(out))


if __name__ == "__main__":
    unittest.main()
