"""跨进程 git 操作公共件（dispatcher / board 共用，不进任何 agent 提示词）。

职责边界（单一事实源，勿在调用方重复实现）：
- git_env(project)：git 子进程环境（禁交互提示；按项目凭据注入 GIT_ASKPASS / SSH key）
- repo_lock(repo)：同一仓库的 fetch/pull/push/merge 跨进程串行（dispatcher 与 board 是两个进程）
- 凭据存取：<repo>/data/git_credentials.json（0600，gitignore，明文仅存本机；API 永不回读密码）
- 远端探测/状态小工具：remote_url、current_branch、ahead_behind

安全约定：
- 密码只在内存与本机 0600 文件；不进 argv、不写日志、不出现在任何 API 响应。
- GIT_ASKPASS 临时脚本由 git_env 上下文管理器创建并保证删除（即使抛异常）。
- SSH 只存 key 路径，不存私钥内容。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SECRETS_PATH = REPO_ROOT / "data" / "git_credentials.json"
LOCKS_DIR = Path.home() / ".req-board" / "locks"
DEFAULT_TIMEOUT = 120
LOCK_DEFAULT_TIMEOUT = 90.

# ---- 凭据文件（data/git_credentials.json） ----
# 结构：{project: {"kind": "token"|"ssh", "url": str, "username": str, "password": str, "ssh_key": str}}
# 只允许本机读写（0600）；读取失败/损坏一律视为未配置（fail-open），绝不抛。


def _read_secrets():
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_secrets(data):
    """原子写 0600：tmp（同目录）→ os.replace；失败清理 tmp 并抛出。"""
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".git_credentials.", dir=str(SECRETS_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, SECRETS_PATH)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def credential_for(project):
    """项目凭据（不含 URL 回显）。未配置/损坏 → None。"""
    if not isinstance(project, str) or not project:
        return None
    entry = _read_secrets().get(project)
    if not isinstance(entry, dict):
        return None
    if entry.get("kind") == "ssh":
        key = str(entry.get("ssh_key") or "").strip()
        return {"kind": "ssh", "ssh_key": key} if key else None
    pw = entry.get("password")
    if not isinstance(pw, str) or not pw:
        return None
    return {"kind": "token", "username": str(entry.get("username") or ""), "password": pw,
            "url": str(entry.get("url") or "")}


def credential_state(project):
    """给 UI 用的安全视图（绝不返回密码）：{kind, username, url, has_secret, ssh_key}。"""
    entry = _read_secrets().get(project)
    if not isinstance(entry, dict):
        return {"configured": False}
    kind = entry.get("kind") if entry.get("kind") in ("token", "ssh") else "token"
    return {
        "configured": bool(entry.get("password")) if kind == "token" else bool(entry.get("ssh_key")),
        "kind": kind,
        "username": str(entry.get("username") or ""),
        "url": str(entry.get("url") or ""),
        "ssh_key": str(entry.get("ssh_key") or ""),
    }


def save_credential(project, *, kind="token", url="", username="", password="", ssh_key=""):
    """保存项目凭据（0600）。kind=token 需 password；kind=ssh 需 ssh_key。返回安全视图。"""
    if not isinstance(project, str) or not project:
        raise ValueError("project 必填")
    kind = "ssh" if kind == "ssh" else "token"
    data = _read_secrets()
    if kind == "ssh":
        key = str(ssh_key or "").strip()
        if not key:
            raise ValueError("ssh_key 必填")
        if not Path(key).expanduser().is_file():
            raise ValueError("ssh key 文件不存在")
        data[project] = {"kind": "ssh", "ssh_key": str(Path(key).expanduser()), "url": str(url or "")}
    else:
        pw = str(password or "")
        if not pw:
            raise ValueError("password 必填")
        data[project] = {"kind": "token", "url": str(url or ""),
                         "username": str(username or ""), "password": pw}
    _write_secrets(data)
    return credential_state(project)


def delete_credential(project):
    data = _read_secrets()
    if project in data:
        data.pop(project, None)
        _write_secrets(data)
    return credential_state(project)


# ---- git 子进程环境 ----

def _write_askpass(cred):
    """写 GIT_ASKPASS 临时脚本（0600 目录 + 文件），返回 (script_path, cleanup_dir)。

    密码/用户名各落一个 0600 文件，脚本只 cat 文件（不把明文写进脚本、不进 argv、不依赖
    base64 等外部命令——macOS/BSD/GNU 行为差异下最稳）。调用方必须删除 cleanup_dir。
    """
    import shlex
    d = Path(tempfile.mkdtemp(prefix=".git_askpass."))
    try:
        u = d / "u"
        p = d / "p"
        u.write_text(cred.get("username") or "", encoding="utf-8")
        p.write_text(cred.get("password") or "", encoding="utf-8")
        os.chmod(u, 0o600)
        os.chmod(p, 0o600)
        script = d / "askpass.sh"
        script.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            f"  *sername*) cat {shlex.quote(str(u))} ;;\n"
            f"  *) cat {shlex.quote(str(p))} ;;\n"
            "esac\n", encoding="utf-8")
        os.chmod(script, 0o700)
        return script, d
    except Exception:
        import shutil
        with contextlib.suppress(OSError):
            shutil.rmtree(d, ignore_errors=True)
        raise


@contextlib.contextmanager
def git_env(project=None, extra=None):
    """git 子进程环境上下文：禁交互 + 按项目凭据注入认证；退出时删除 askpass 临时脚本。

    用法：
        with git_env(project) as env:
            subprocess.run(["git", "fetch", "origin"], cwd=repo, env=env, ...)
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    for k in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH_COMMAND"):
        env.pop(k, None)
    askpass = None
    askpass_dir = None
    try:
        cred = credential_for(project) if project else None
        if cred and cred.get("kind") == "ssh":
            import shlex
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {shlex.quote(cred['ssh_key'])} -o BatchMode=yes "
                "-o StrictHostKeyChecking=accept-new")
        elif cred and cred.get("password"):
            askpass, askpass_dir = _write_askpass(cred)
            env["GIT_ASKPASS"] = str(askpass)
            env["GIT_TERMINAL_PROMPT"] = "0"
        if extra:
            env.update({str(k): str(v) for k, v in extra.items()})
        yield env
    finally:
        if askpass_dir:
            import shutil
            with contextlib.suppress(OSError):
                shutil.rmtree(askpass_dir, ignore_errors=True)


# ---- 跨进程仓库锁 ----

@contextlib.contextmanager
def repo_lock(repo, timeout=LOCK_DEFAULT_TIMEOUT):
    """同一仓库的 git 远端/合并操作串行（fcntl 跨进程；超时抛 TimeoutError）。

    锁粒度 = 仓库真实路径（worktree 与其归属仓库指向同一 .git 时会解析到同一锁文件？
    不会：worktree 路径不同。约定只对「主工作区仓库路径」加锁，worktree 内 dev commit 无需锁）。
    """
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(str(Path(repo).resolve()).encode()).hexdigest()[:16]
    path = LOCKS_DIR / f"{key}.lock"
    fh = open(path, "a+")
    try:
        deadline = time.time() + float(timeout)
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise TimeoutError("仓库正被其它操作占用（git 锁等待超时）")
                time.sleep(0.2)
        yield path
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ---- 只读小工具（供 board 项目页/探测端点复用） ----

def run_git(repo, args, *, env=None, timeout=DEFAULT_TIMEOUT):
    """执行 git，返回 (returncode, stdout_text, stderr_text)。不抛（超时返回 124）。"""
    try:
        r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True,
                           timeout=float(timeout), env=env or git_env_plain())
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "git 超时"
    except OSError as e:
        return 127, "", str(e)


def git_env_plain():
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    for k in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH_COMMAND"):
        env.pop(k, None)
    return env


def remote_url(repo, remote="origin"):
    rc, out, _ = run_git(repo, ["remote", "get-url", remote], env=git_env_plain())
    return out.strip() if rc == 0 else ""


def remote_names(repo):
    rc, out, _ = run_git(repo, ["remote"], env=git_env_plain())
    return [line.strip() for line in out.splitlines() if line.strip()] if rc == 0 else []


def current_branch(repo):
    rc, out, _ = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], env=git_env_plain())
    return out.strip() if rc == 0 else ""


def ahead_behind(repo, branch, remote="origin", env=None):
    """返回 (ahead, behind)；远端引用不存在 → (None, None)。只读，不 fetch。"""
    ref = f"{remote}/{branch}"
    rc, _, _ = run_git(repo, ["rev-parse", "--verify", "-q", ref], env=env or git_env_plain())
    if rc != 0:
        return None, None
    rc, out, _ = run_git(repo, ["rev-list", "--left-right", "--count", f"{ref}...{branch}"],
                         env=env or git_env_plain())
    if rc != 0:
        return None, None
    try:
        behind, ahead = (int(x) for x in out.split())
        return ahead, behind
    except (ValueError, TypeError):
        return None, None
