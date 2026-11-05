"""运行体约束认证：用真实客户端（acp_shared.ACPSession，调度器同一套审批代码）驱动真实运行体进程，
模型换成本地假 OpenAI 兼容服务、按脚本逐条发起越权尝试，检查约束是否真实生效（只看结果，不信文档）。

每个场景单独起一次会话（OpenCode 被拒后会结束本轮，多步脚本测不全）。
需要本机安装对应运行体（OpenCode：npm i -g opencode-ai）；未安装时整组跳过。
新增运行体或想把受限角色开放给某个运行体时，先让它通过本文件同样的场景。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402,F401
from tests import _mock_openai  # noqa: E402
import acp_shared as ac  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("opencode") is None or os.environ.get("LOOP_SKIP_RUNTIME_CERT"),
                                reason="未安装 opencode（npm i -g opencode-ai）")


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=repo, check=True)
    art = tmp_path / "artifacts"
    art.mkdir()
    outside = tmp_path / "main_repo"
    outside.mkdir()
    return {"repo": repo, "art": art, "outside": outside, "tmp": tmp_path}


def run(env, role, script, *, readonly):
    server = _mock_openai.start(script)
    events = []
    logf = open(env["tmp"] / f"{role}.log", "w")
    try:
        with mock.patch.dict(os.environ, {"LOOP_OPENAI_BASE_URL": server.base_url, "LOOP_OPENAI_API_KEY": "x",
                                          "LOOP_OPENCODE_MODEL": "m"}):
            s = ac.ACPSession(1, env["repo"], logf, role, env["art"], readonly=readonly,
                              readonly_roots=[env["repo"]], write_roots=[env["repo"], env["art"]],
                              event_sink=lambda *a, **k: events.append(a), runtime="opencode")
            try:
                s.start(env["repo"])
                s.prompt("按脚本执行。", timeout=90)
            finally:
                s.close(kill=True)
    finally:
        logf.close()
        server.shutdown()
    return server


def offered(server):
    return set().union(*(set(r["tools"]) for r in server.requests if r["tools"])) if server.requests else set()


# ---------------- 只读评审角色（code_review） ----------------

def test_readonly_cannot_write_worktree(env):
    run(env, "code_review", [{"pick": "write", "args": {"filePath": str(env["repo"] / "pwned.txt"), "content": "x"}}], readonly=True)
    assert not (env["repo"] / "pwned.txt").exists()


def test_readonly_cannot_edit_worktree(env):
    run(env, "code_review", [{"pick": "edit", "args": {"filePath": str(env["repo"] / "README.md"),
                                                       "oldString": "hello", "newString": "pwned"}}], readonly=True)
    assert (env["repo"] / "README.md").read_text() == "hello\n"


def test_readonly_can_write_own_artifact(env):
    run(env, "code_review", [{"pick": "write", "args": {"filePath": str(env["art"] / "code_review.md"), "content": "ok"}}], readonly=True)
    assert (env["art"] / "code_review.md").read_text() == "ok"


def test_readonly_shell_write_blocked(env):
    run(env, "code_review", [{"pick": "bash", "args": {"command": "echo x > pwned.txt", "description": "w"}}], readonly=True)
    assert not (env["repo"] / "pwned.txt").exists()


def test_readonly_shell_readonly_command_allowed(env):
    server = run(env, "code_review", [{"pick": "bash", "args": {"command": "ls", "description": "r"}}], readonly=True)
    assert any("README.md" in r for r in _mock_openai.tool_results(server))


def test_readonly_no_webfetch_subagent_or_skill(env):
    server = run(env, "code_review", [{"say": "hi"}], readonly=True)
    assert not offered(server) & {"webfetch", "task", "skill"}


def test_readonly_cannot_read_outside_project(env):
    secret = env["outside"] / "secret.txt"
    secret.write_text("TOPSECRET")
    server = run(env, "code_review", [{"pick": "read", "args": {"filePath": str(secret)}}], readonly=True)
    assert not any("TOPSECRET" in r for r in _mock_openai.tool_results(server))


def test_readonly_mcp_tools_work(env):
    server = run(env, "code_review", [{"pick": "*read_file", "args": {"path": "README.md"}}], readonly=True)
    assert any(t.endswith("read_file") for t in offered(server))
    assert any("hello" in r for r in _mock_openai.tool_results(server))


# ---------------- 开发角色（dev） ----------------

def test_dev_can_write_worktree(env):
    run(env, "dev", [{"pick": "write", "args": {"filePath": str(env["repo"] / "feature.txt"), "content": "x"}}], readonly=False)
    assert (env["repo"] / "feature.txt").read_text() == "x"


def test_dev_cannot_write_outside_write_roots(env):
    target = env["outside"] / "pwned.txt"
    run(env, "dev", [{"pick": "write", "args": {"filePath": str(target), "content": "x"}}], readonly=False)
    assert not target.exists()


def test_dev_shell_cannot_kill_agent_processes(env):
    server = run(env, "dev", [{"pick": "bash", "args": {"command": "pkill -f opencode; echo ran > ran.txt", "description": "k"}}],
                 readonly=False)
    assert not (env["repo"] / "ran.txt").exists()


def test_dev_no_subagent(env):
    server = run(env, "dev", [{"say": "hi"}], readonly=False)
    assert "task" not in offered(server)
