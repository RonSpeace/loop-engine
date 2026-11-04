"""#54 需求助手 -> 项目代码拉取/提交服务（隔离、冲突保留、可取消）。

分层契约（#32）：本模块只暴露 `ProjectGit` 一个顶层符号；常量与辅助函数挂在类属性上。
可补丁名（PROJECT_PATHS / PROJECTS_FILE / subprocess / now_str）一律经 runtime.resolve
在调用期读取，不裸读、不 import board/blueprints。
"""
from pathlib import Path
import contextlib
import hashlib
import json
import os
import re
import signal
import threading
import time

from acp_shared import git_log_env

import git_ops

from board_modules.runtime import resolve


class ProjectGit:
    """项目代码仓库操作（状态 / 拉取 / 冲突处理 / 提交预览与落地）。

    隔离：`project` 只按 projects.json 清单 key 精确命中；`repo` 只在该项目的 paths 内
    精确匹配（绝对路径或唯一目录名），缺省/未知/多候选一律拒绝并返回候选清单。
    """

    class GitCancelled(Exception):
        """取消事件触发（用户取消/会话回收）：git 子进程已终止。"""

    class GitTimedOut(Exception):
        """deadline/单命令预算耗尽：git 子进程已终止。"""

    UNMERGED_CODES = ("DD", "AU", "UD", "UA", "DU", "AA", "UU")
    RENAME_CODES = ("R", "C")
    TOOL_BUDGET_SEC = 60           # 单次工具调用预算上限
    BUDGET_RESERVE_SEC = 5         # 轮次 deadline 预留
    FETCH_MAX_SEC = 45             # fetch 单命令上限
    FETCH_DEFAULT_SEC = 30         # 未配置 git_sync.timeout 时的 fetch 上限
    LOCAL_CMD_MAX_SEC = 15         # 本地 git 命令上限
    TOKEN_TTL_SEC = 600            # 提交确认 token 有效期
    MAX_FILES = 100                # 状态/预览清单条数上限
    MAX_UNTRACKED = 50             # include_untracked 条数上限
    MAX_PATH_LEN = 500
    MAX_MESSAGE_LEN = 500
    MAX_DIFF_FINGERPRINT_BYTES = 10 * 1024 * 1024
    MAX_UNTRACKED_SCAN_BYTES = 1024 * 1024
    MAX_FINDINGS = 20
    CANDIDATE_LIMIT = 50
    ENV_MARKERS = (("merge", "MERGE_HEAD"), ("rebase", "REBASE_HEAD"),
                   ("cherry-pick", "CHERRY_PICK_HEAD"), ("revert", "REVERT_HEAD"))
    SECRET_PATTERNS = (
        ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
        ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
        ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
        ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
        ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
        ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    )
    SENSITIVE_SUFFIXES = (".pem", ".key", ".p12")
    SENSITIVE_NAMES = ("id_rsa", "id_ed25519", "id_ecdsa",
                       "credentials", "credentials.json", "secrets.json")
    ENV_TEMPLATE_SEGMENTS = ("example", "sample", "template")

    _op_locks = {}
    _op_locks_guard = threading.Lock()

    def __init__(self, deadline=None, cancel_event=None):
        self.deadline = deadline
        self.cancel_event = cancel_event or threading.Event()

    # ---- 预算与取消 ----
    def _budget(self):
        if self.deadline is None:
            return self.TOOL_BUDGET_SEC
        return min(self.TOOL_BUDGET_SEC,
                   max(0.0, self.deadline - time.monotonic() - self.BUDGET_RESERVE_SEC))

    def budget_error(self):
        """预算不足时返回统一结果（不启动任何 git）；足够返回 None。"""
        if self._budget() < self.BUDGET_RESERVE_SEC:
            return {"status": "failed", "error": "本轮剩余时间不足，请重发",
                    "timed_out": False, "cancelled": False}
        return None

    @contextlib.contextmanager
    def op_lock(self, repo):
        """同一仓库串行化（进程内）；跨进程靠中间态与 index.lock 失败关闭。"""
        key = str(repo)
        with self._op_locks_guard:
            lock = self._op_locks.setdefault(key, threading.Lock())
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def kill_active(self):
        """置取消标记：正在跑的 git 子进程在下一个轮询周期被终止。"""
        self.cancel_event.set()

    # ---- 项目 / 仓库定位 ----
    @staticmethod
    def _projects_map():
        try:
            paths = resolve("PROJECT_PATHS")
        except AttributeError:
            return {}
        return paths if isinstance(paths, dict) else {}

    @staticmethod
    def _projects_raw():
        try:
            raw = json.loads(Path(resolve("PROJECTS_FILE")).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 配置损坏按空配置处理（候选清单为空）
            return {}
        return raw if isinstance(raw, dict) else {}

    @classmethod
    def project_candidates(cls):
        out = []
        projects = cls._projects_map()
        for name in sorted(projects)[:cls.CANDIDATE_LIMIT]:
            paths = [Path(p).name for p in (projects.get(name) or [])]
            out.append({"project": name, "paths": paths})
        return out

    @classmethod
    def _candidate_text(cls, items):
        return "、".join(items) if items else "（无）"

    def _require_project(self, project):
        projects = self._projects_map()
        names = [f"{c['project']}（{'、'.join(c['paths']) or '无路径'}）"
                 for c in self.project_candidates()]
        text = self._candidate_text(names)
        if not isinstance(project, str) or not project.strip():
            raise ValueError(f"project 必填；候选项目：{text}（可先用 project_find 定位）")
        project = project.strip()
        if project not in projects:
            raise ValueError(f"未知项目: {project}；候选项目：{text}")
        paths = [str(p) for p in (projects.get(project) or []) if p]
        if not paths:
            raise ValueError(f"项目 {project} 无可用路径")
        return project, paths

    def repo_candidates(self, project):
        _project, paths = self._require_project(project)
        return [{"repo": p, "name": Path(p).name} for p in paths[:self.CANDIDATE_LIMIT]]

    def _repo_error(self, project, paths):
        items = [f"{Path(p).name}（{p}）" for p in paths[:self.CANDIDATE_LIMIT]]
        return (f"项目 {project} 有多个仓库，请指定 repo；候选仓库："
                f"{self._candidate_text(items)}")

    def resolve_repo(self, project, repo=None):
        project, paths = self._require_project(project)
        candidates = [Path(p) for p in paths]
        if repo is None or (isinstance(repo, str) and not repo.strip()):
            if len(candidates) == 1:
                chosen = candidates[0]
            else:
                raise ValueError(self._repo_error(project, paths))
        else:
            if not isinstance(repo, str) or len(repo) > 1000:
                raise ValueError(f"repo 参数非法: {repo!r}")
            given = repo.strip()
            if given.startswith("-"):
                raise ValueError("repo 不得以 '-' 开头")
            matched = []
            if Path(given).is_absolute():
                try:
                    target = Path(given).resolve()
                except OSError:
                    target = None
                for cand in candidates:
                    try:
                        if target is not None and cand.resolve() == target:
                            matched.append(cand)
                    except OSError:
                        continue
            else:
                for cand in candidates:
                    if cand.name == given or str(cand) == given:
                        matched.append(cand)
            if not matched:
                items = [f"{Path(p).name}（{p}）" for p in paths[:self.CANDIDATE_LIMIT]]
                raise ValueError(f"repo 无法唯一定位: {given!r}；候选仓库："
                                 f"{self._candidate_text(items)}")
            if len(matched) > 1:
                items = [f"{Path(p).name}（{p}）" for p in paths[:self.CANDIDATE_LIMIT]]
                raise ValueError(f"repo 无法唯一定位: {given!r}；候选仓库："
                                 f"{self._candidate_text(items)}")
            chosen = matched[0]
        try:
            chosen = chosen.resolve()
        except OSError:
            raise ValueError(f"仓库路径不可用: {chosen}")
        if not (chosen / ".git").exists():
            raise ValueError(f"仓库不存在或不是 git 仓库根: {chosen}")
        return chosen

    def _project_cfg(self, project):
        raw = self._projects_raw().get(project)
        return raw if isinstance(raw, dict) else {}

    def _base_branch(self, project, repo):
        cfg = self._project_cfg(project)
        base = cfg.get("base_branch")
        if isinstance(base, dict):
            for key in (str(repo), str(Path(repo)), repo.name):
                if key in base:
                    return base.get(key)
            return None
        return base if isinstance(base, str) and base.strip() else None

    def _git_sync(self, project):
        cfg = self._project_cfg(project).get("git_sync")
        return cfg if isinstance(cfg, dict) else {}

    def _git_cfg(self, project):
        """项目级 git 配置段（v1）：{remote,url,credential,ssh_key}；非 dict 按空处理。"""
        cfg = self._project_cfg(project).get("git")
        return cfg if isinstance(cfg, dict) else {}

    def _configured_remote(self, project):
        """配置的远端名：git.remote 优先，旧位置 git_sync.remote 兜底（默认 origin 由调用方补）。"""
        for section in (self._git_cfg(project), self._git_sync(project)):
            name = section.get("remote")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return ""

    # ---- 子进程执行（可取消 / 超时 / 环境消毒） ----
    def _spawn(self, argv, cwd, env=None):
        """启动 git 子进程；env 缺省 = git_log_env()（消毒），传入即原样使用（凭据注入场景）。

        远端操作（fetch/pull/push/ls-remote）必须传 git_ops.git_env(project) 产出的 env：
        GIT_ASKPASS / GIT_SSH_COMMAND 属 GIT_* 前缀，会被 git_log_env() 剥离；本地只读命令
        仍走默认消毒环境（行为不变）。"""
        sp = resolve("subprocess")
        return sp.Popen(argv, cwd=str(cwd), stdout=sp.PIPE, stderr=sp.PIPE,
                        env=env if env is not None else git_log_env(), start_new_session=True)

    @staticmethod
    def _terminate(proc):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.communicate(timeout=1)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.communicate(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    def run(self, argv, repo, cap=None, timeout=None, env=None):
        """固定 argv 执行 git；返回 (returncode, stdout_bytes, stderr_bytes)。
        超时 = min(工具预算剩余, cap/timeout)；取消事件或超时都会终止整个进程组。
        env 非 None 时原样注入（见 _spawn：远端操作凭据环境），缺省走 git_log_env() 消毒。"""
        budget = self._budget()
        if budget <= 0:
            raise self.GitTimedOut("本轮剩余时间不足")
        limit = min(budget, cap if cap is not None else self.LOCAL_CMD_MAX_SEC)
        if timeout is not None:
            limit = min(limit, timeout)
        if limit <= 0:
            raise self.GitTimedOut("本轮剩余时间不足")
        if self.cancel_event.is_set():
            raise self.GitCancelled("操作已取消")
        # env 缺省时保持旧调用形态（_spawn(argv, cwd)）：既有测试用两参 lambda 替换 _spawn 的探针不破。
        if env is None:
            proc = self._spawn([str(a) for a in argv], repo)
        else:
            proc = self._spawn([str(a) for a in argv], repo, env=env)
        end = time.monotonic() + limit
        while True:
            try:
                out, err = proc.communicate(timeout=0.1)
                return proc.returncode, out or b"", err or b""
            except Exception as exc:  # subprocess.TimeoutExpired
                if not isinstance(exc, resolve("subprocess").TimeoutExpired):
                    self._terminate(proc)
                    raise
                if self.cancel_event.is_set():
                    self._terminate(proc)
                    raise self.GitCancelled("操作已取消")
                if time.monotonic() >= end:
                    self._terminate(proc)
                    raise self.GitTimedOut(f"git 命令超时（>{int(limit)}s）")

    def _text(self, data):
        return data.decode("utf-8", errors="replace")

    def _git(self, repo, *args, cap=None, ok_codes=(0,), env=None):
        rc, out, err = self.run(["git", *args], repo, cap=cap, env=env)
        if rc not in ok_codes:
            raise RuntimeError(self.redact_urls(self._text(err).strip()[:300]) or f"git 退出码 {rc}")
        return self._text(out)

    # ---- 中间态 / 状态 ----
    def _git_dir(self, repo):
        try:
            out = self._git(repo, "rev-parse", "--git-dir")
        except Exception:  # noqa: BLE001
            return None
        p = Path(out.strip())
        return (p if p.is_absolute() else (repo / p)).resolve()

    def midstate(self, repo):
        gd = self._git_dir(repo)
        marks = []
        if gd is not None:
            for name, marker in self.ENV_MARKERS:
                if (gd / marker).exists() and name not in marks:
                    marks.append(name)
            if (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
                if "rebase" not in marks:
                    marks.append("rebase")
        return marks

    def _attached_branch(self, repo):
        rc, out, _err = self.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], repo)
        if rc != 0:
            return None
        name = self._text(out).strip()
        return name or None

    def current_branch(self, project, repo=None):
        """当前分支名；detached HEAD 返回 None（调用方 fail-closed）。"""
        return self._attached_branch(self.resolve_repo(project, repo))

    def _head_sha(self, repo):
        return self._git(repo, "rev-parse", "HEAD").strip()

    def _head_summary(self, repo):
        try:
            sha = self._head_sha(repo)
            short = self._git(repo, "rev-parse", "--short", "HEAD").strip()
            subject = self._git(repo, "log", "-1", "--format=%s", "HEAD").strip()
        except Exception:  # noqa: BLE001
            return {"short": None, "subject": None, "sha": None}
        return {"short": short, "subject": subject[:200], "sha": sha}

    @classmethod
    def _parse_porcelain(cls, raw):
        """`git status --porcelain=v1 -z` → [{code,path}]（重命名记录吃掉第二个路径）。"""
        records = raw.split("\0")
        entries = []
        i = 0
        while i < len(records):
            rec = records[i]
            i += 1
            if not rec:
                continue
            code = rec[:2]
            path = rec[3:]
            if code and code[0] in cls.RENAME_CODES:
                if i < len(records):
                    i += 1
            entries.append({"code": code, "path": path})
        return entries

    def _entries(self, repo):
        rc, out, err = self.run(["git", "status", "--porcelain=v1", "-z",
                                 "--untracked-files=all"], repo)
        if rc != 0:
            raise RuntimeError(self.redact_urls(self._text(err).strip()[:300]))
        return self._parse_porcelain(self._text(out))

    @classmethod
    def _conflicts(cls, entries):
        return [e for e in entries if e["code"] in cls.UNMERGED_CODES]

    @staticmethod
    def _tracked(entries):
        return [e for e in entries if e["code"] != "??"]

    @staticmethod
    def _untracked(entries):
        return [e["path"] for e in entries if e["code"] == "??"]

    def status(self, project, repo=None):
        err = self.budget_error()
        if err:
            return err
        repo_path = self.resolve_repo(project, repo)
        with self.op_lock(repo_path):
            return self._status(repo_path, project)

    def _status(self, repo_path, project):
        row = {"status": "ok", "project": project, "repo": str(repo_path),
               "repo_name": repo_path.name}
        branch = self._attached_branch(repo_path)
        head = self._head_summary(repo_path)
        row["head"] = head
        if branch is None:
            row.update({"status": "error", "branch": None,
                        "error": "当前处于 detached HEAD，无法确定分支。请先在项目仓库切回目标分支"
                                 "（例如 git switch <branch>）后再查询或拉取。"})
            return row
        row["branch"] = branch
        base = self._base_branch(project, repo_path)
        row["base_branch"] = base
        if base and base != branch:
            row["base_branch_hint"] = (f"当前分支 {branch} 与项目配置的起点分支 {base} 不一致；"
                                       "拉取仍以当前分支为目标，不会切换分支")
        upstream = None
        rc, out, _err = self.run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                                  "@{u}"], repo_path)
        if rc == 0:
            upstream = self._text(out).strip() or None
        row["upstream"] = upstream
        row["ahead"] = row["behind"] = None
        if upstream:
            rc, out, _err = self.run(["git", "rev-list", "--left-right", "--count",
                                      "HEAD...@{u}"], repo_path)
            if rc == 0:
                parts = self._text(out).split()
                if len(parts) == 2:
                    row["ahead"], row["behind"] = int(parts[0]), int(parts[1])
        entries = self._entries(repo_path)
        tracked = self._tracked(entries)
        untracked = self._untracked(entries)
        conflicts = self._conflicts(entries)
        row["dirty"] = {"changed": len(tracked), "untracked": len(untracked),
                        "deleted": sum(1 for e in tracked if "D" in e["code"])}
        row["changed_files"] = [{"code": e["code"], "path": e["path"]}
                                for e in tracked[:self.MAX_FILES]]
        row["untracked_files"] = untracked[:self.MAX_FILES]
        row["conflicts"] = [{"code": e["code"], "path": e["path"]}
                            for e in conflicts[:self.MAX_FILES]]
        row["unmerged"] = bool(conflicts)
        row["midstate"] = self.midstate(repo_path)
        row["truncated"] = (len(tracked) > self.MAX_FILES
                            or len(untracked) > self.MAX_FILES)
        return row

    # ---- 拉取 ----
    def _remote_for(self, repo_path, project, branch, remote=None):
        remote = (remote or "").strip() or None
        remotes = [x.strip() for x in
                   self._git(repo_path, "remote").splitlines() if x.strip()]
        if remote:
            if remote not in remotes:
                raise ValueError(f"未知远端: {remote}；候选远端：{self._candidate_text(remotes)}")
            return remote
        # 配置来源优先级：git.remote（v1 单一事实源）→ 旧位置 git_sync.remote
        cfg_remote = self._configured_remote(project) or None
        if cfg_remote:
            if cfg_remote not in remotes:
                raise ValueError(f"项目配置的远端 {cfg_remote} 不存在；候选远端："
                                 f"{self._candidate_text(remotes)}")
            return cfg_remote
        rc, out, _err = self.run(["git", "config", "--get",
                                  f"branch.{branch}.remote"], repo_path)
        if rc == 0:
            configured = self._text(out).strip()
            if configured and configured in remotes:
                return configured
        if "origin" in remotes:
            return "origin"
        raise ValueError(f"无法确定远端；候选远端：{self._candidate_text(remotes)}")

    def _fetch_cap(self, project):
        try:
            configured = float(self._git_sync(project).get("timeout") or 0)
        except (TypeError, ValueError):
            configured = 0
        if configured <= 0:
            configured = self.FETCH_DEFAULT_SEC
        return min(configured, self.FETCH_MAX_SEC)

    def pull(self, project, repo=None, branch=None, remote=None):
        err = self.budget_error()
        if err:
            return err
        repo_path = self.resolve_repo(project, repo)
        with self.op_lock(repo_path):
            return self._pull(repo_path, project, branch, remote)

    def _pull(self, repo_path, project, branch, remote):
        current = self._attached_branch(repo_path)
        if current is None:
            raise ValueError("当前处于 detached HEAD，拒绝拉取；请先切回目标分支（不会自动切换）")
        if branch and str(branch).strip() and str(branch).strip() != current:
            raise ValueError(f"显式 branch={branch!r} 与当前分支 {current} 不一致；"
                             "本工具不切换分支，请在当前分支操作或先手工切换")
        mid = self.midstate(repo_path)
        if mid:
            return {"status": "failed", "error": f"仓库处于中间态（{', '.join(mid)}），请先处理",
                    "midstate": mid,
                    "guidance": "先用 project_git_status 查看现场；merge 冲突可 resolve/abort，"
                                "rebase/cherry-pick 请手工完成后重试"}
        entries = self._entries(repo_path)
        tracked = self._tracked(entries)
        if tracked:
            return {"status": "failed",
                    "error": "工作区有未提交的已跟踪改动，已拒绝拉取（不自动 stash/覆盖）",
                    "changed_files": [{"code": e["code"], "path": e["path"]}
                                      for e in tracked[:self.MAX_FILES]],
                    "untracked_files": self._untracked(entries)[:self.MAX_FILES],
                    "guidance": "先提交或手工处理这些改动，再重试拉取"}
        remote_name = self._remote_for(repo_path, project, current, remote)
        before = self._head_summary(repo_path)
        ref = f"refs/remotes/{remote_name}/{current}"
        try:
            # 远端操作注入项目凭据环境（token/ssh 才能拿到）：git_env 退出时删除 askpass 临时脚本
            with git_ops.git_env(project) as genv:
                rc, _out, errtxt = self.run(
                    ["git", "fetch", "--prune", remote_name,
                     f"+refs/heads/{current}:{ref}"],
                    repo_path, cap=self._fetch_cap(project), env=genv)
        except self.GitTimedOut as exc:
            return {"status": "failed", "error": f"fetch 超时: {exc}", "timed_out": True,
                    "cancelled": False, "may_be_in_progress": False, "before": before}
        except self.GitCancelled:
            return {"status": "failed", "error": "操作已取消", "timed_out": False,
                    "cancelled": True, "may_be_in_progress": False, "before": before}
        if rc != 0:
            return {"status": "failed",
                    "error": self.redact_urls(self._text(errtxt).strip()[:300]) or "fetch 失败",
                    "timed_out": False, "cancelled": False, "may_be_in_progress": False,
                    "before": before}
        rc, out, _err = self.run(["git", "rev-parse", ref], repo_path)
        if rc != 0:
            return {"status": "failed", "error": f"远端不存在分支 {current}（{remote_name}）",
                    "before": before, "remote": remote_name}
        remote_sha = self._text(out).strip()
        local_sha = before.get("sha")
        base = {"project": project, "repo": str(repo_path), "branch": current,
                "remote": remote_name, "before": before}
        if remote_sha == local_sha:
            base.update({"status": "up_to_date", "after": before})
            return base
        behind = self._is_ancestor(repo_path, local_sha, remote_sha)
        ahead = self._is_ancestor(repo_path, remote_sha, local_sha)
        if ahead and not behind:
            base.update({"status": "ahead", "after": before,
                         "guidance": "本地领先远端，无需拉取；如需推送请手工执行"})
            return base
        merge_argv = (["git", "merge", "--ff-only", f"{remote_name}/{current}"]
                      if behind else ["git", "merge", "--no-edit", f"{remote_name}/{current}"])
        try:
            rc, _out, errtxt = self.run(merge_argv, repo_path)
        except self.GitTimedOut as exc:
            base.update({"status": "failed", "error": f"merge 超时: {exc}", "timed_out": True,
                         "cancelled": False, "may_be_in_progress": True})
            return base
        except self.GitCancelled:
            base.update({"status": "failed", "error": "操作已取消", "timed_out": False,
                         "cancelled": True, "may_be_in_progress": True})
            return base
        after = self._head_summary(repo_path)
        if rc == 0:
            base.update({"status": "pulled", "after": after})
            return base
        mid = self.midstate(repo_path)
        entries = self._entries(repo_path)
        conflicts = self._conflicts(entries)
        if "merge" in mid or conflicts:
            base.update({
                "status": "conflict", "in_progress": True,
                "files": [{"code": e["code"], "path": e["path"]}
                          for e in conflicts[:self.MAX_FILES]],
                "options": ["保留我方(ours)", "保留对方(theirs)",
                            "手动处理后重试", "放弃本次合并(abort)"],
                "guidance": "合并现场已保留，本地提交未丢。请选择："
                            "① 对每个冲突文件调用 project_git_pull action=resolve side=ours/theirs；"
                            "② 手工编辑后 git add 并重试；③ action=abort 放弃本次合并。"
                            "解决完所有冲突后调用 project_git_commit 完成合并提交。",
                "after": after,
                "error": self.redact_urls(self._text(errtxt).strip()[:300]) or "merge 冲突"})
            return base
        base.update({"status": "failed",
                     "error": self.redact_urls(self._text(errtxt).strip()[:300]) or "merge 失败",
                     "timed_out": False, "cancelled": False,
                     "may_be_in_progress": bool(mid), "after": after})
        return base

    def _is_ancestor(self, repo, maybe_ancestor, descendant):
        if not maybe_ancestor or not descendant:
            return False
        rc, _out, _err = self.run(["git", "merge-base", "--is-ancestor",
                                   maybe_ancestor, descendant], repo)
        return rc == 0

    # ---- 推送（v1：只推 base_branch、禁 force、非快进 fetch+ff 重试一次） ----
    @staticmethod
    def _non_fast_forward(text):
        low = (text or "").lower()
        return any(mark in low for mark in (
            "non-fast-forward", "non-fast forward", "fetch first",
            "updates were rejected because the tip"))

    @staticmethod
    def _push_rejected(text):
        low = (text or "").lower()
        return any(mark in low for mark in (
            "rejected", "denied", "pre-receive", "pre receive", "protected",
            "hook", "not allowed", "permission"))

    def _remote_counts(self, repo, branch, remote, env=None):
        """本地相对远端分支的 (ahead, behind)；远端引用缺失/命令失败 → (None, None)。只读。"""
        ref = f"refs/remotes/{remote}/{branch}"
        rc, _out, _err = self.run(["git", "rev-parse", "--verify", "-q", ref], repo, env=env)
        if rc != 0:
            return None, None
        rc, out, _err = self.run(["git", "rev-list", "--left-right", "--count",
                                  f"{ref}...{branch}"], repo, env=env)
        if rc != 0:
            return None, None
        try:
            behind, ahead = (int(x) for x in self._text(out).split())
        except (TypeError, ValueError):
            return None, None
        return ahead, behind

    def _push_frame(self, repo_path, project, status, message, **extra):
        frame = {"status": status, "project": project, "repo": str(repo_path),
                 "repo_name": repo_path.name, "message": message,
                 "ahead": None, "behind": None, "before": None, "after": None,
                 "branch": None, "base_branch": None, "remote": None}
        frame.update(extra)
        if status in ("failed", "rejected"):
            frame["error"] = message
        return frame

    def push(self, project, repo=None):
        """把 base_branch 推到项目配置的远端（禁 force；只推 base_branch）。

        前置 fail-closed：detached HEAD / 当前分支≠base_branch / 中间态 / 冲突 / 工作区不干净
        （未提交改动或未跟踪文件）一律拒绝。非快进先 fetch + merge --ff-only 重试一次；
        仍不可快进 → status=behind，服务端拒绝 → rejected，其它 → failed。返回统一帧（不抛业务错）。
        """
        err = self.budget_error()
        if err:
            return err
        repo_path = self.resolve_repo(project, repo)
        with self.op_lock(repo_path):
            return self._push(repo_path, project)

    def _push(self, repo_path, project):
        current = self._attached_branch(repo_path)
        if current is None:
            return self._push_frame(repo_path, project, "failed",
                                    "当前处于 detached HEAD，拒绝推送；请先切回目标分支")
        base = self._base_branch(project, repo_path)
        if not base:
            return self._push_frame(repo_path, project, "failed",
                                    "项目未配置 base_branch，无法确定推送目标分支",
                                    branch=current)
        if current != base:
            return self._push_frame(
                repo_path, project, "failed",
                f"当前分支 {current} 与项目配置的 base_branch {base} 不一致；"
                "只允许推送 base_branch，请先切回目标分支",
                branch=current, base_branch=base)
        mid = self.midstate(repo_path)
        if mid:
            return self._push_frame(repo_path, project, "failed",
                                    f"仓库处于中间态（{', '.join(mid)}），拒绝推送",
                                    branch=current, base_branch=base)
        entries = self._entries(repo_path)
        conflicts = self._conflicts(entries)
        tracked = self._tracked(entries)
        untracked = self._untracked(entries)
        if conflicts:
            return self._push_frame(repo_path, project, "failed",
                                    "存在未解决的冲突，拒绝推送", branch=current,
                                    base_branch=base)
        if tracked:
            return self._push_frame(
                repo_path, project, "failed",
                "工作区有未提交的已跟踪改动，拒绝推送（请先提交或处理这些改动）",
                branch=current, base_branch=base,
                error_files=[{"code": e["code"], "path": e["path"]}
                             for e in tracked[:self.MAX_FILES]])
        if untracked:
            return self._push_frame(
                repo_path, project, "failed",
                "工作区有未跟踪文件，拒绝推送（请先提交、清理或加入 .gitignore）",
                branch=current, base_branch=base,
                untracked_files=untracked[:self.MAX_FILES])
        try:
            remote_name = self._remote_for(repo_path, project, base, None)
        except ValueError as exc:
            return self._push_frame(repo_path, project, "failed", str(exc),
                                    branch=current, base_branch=base)
        before = self._head_summary(repo_path)
        base_frame = {"branch": current, "base_branch": base, "remote": remote_name,
                      "before": before}
        cap = self._fetch_cap(project)
        try:
            with git_ops.git_env(project) as genv:
                rc, _out, errtxt = self.run(
                    ["git", "fetch", "--prune", remote_name,
                     f"+refs/heads/{base}:refs/remotes/{remote_name}/{base}"],
                    repo_path, cap=cap, env=genv)
                if rc != 0:
                    text = self.redact_urls(self._text(errtxt).strip()[:300]) or "fetch 失败"
                    return self._push_frame(repo_path, project, "failed",
                                            f"推送前 fetch 失败：{text}", **base_frame)
                ahead, behind = self._remote_counts(repo_path, base, remote_name, env=genv)
                if ahead == 0:
                    status = "behind" if behind else "up_to_date"
                    msg = (f"本地 {base} 落后远端 {behind} 个提交；没有可推送的提交，请先拉取"
                           if behind else "本地已与远端一致，无需推送")
                    return self._push_frame(repo_path, project, status, msg,
                                            **base_frame, ahead=ahead, behind=behind)
                if behind:
                    return self._push_frame(
                        repo_path, project, "behind",
                        f"本地与远端已分叉（领先 {ahead}、落后 {behind}），"
                        "请先拉取并合并后再推送",
                        **base_frame, ahead=ahead, behind=behind)
                rc, _out, errtxt = self.run(
                    ["git", "push", remote_name, f"refs/heads/{base}:refs/heads/{base}"],
                    repo_path, cap=cap, env=genv)
                if rc == 0:
                    return self._push_frame(
                        repo_path, project, "pushed",
                        f"已推送 {base} 到 {remote_name}（{ahead} 个提交）",
                        **base_frame, ahead=ahead, behind=0,
                        after=self._head_summary(repo_path))
                text = self.redact_urls(self._text(errtxt).strip()[:300]) or "推送失败"
                if not self._non_fast_forward(text):
                    status = "rejected" if self._push_rejected(text) else "failed"
                    return self._push_frame(repo_path, project, status, text,
                                            **base_frame, ahead=ahead, behind=behind,
                                            after=self._head_summary(repo_path))
                # 非快进：fetch + merge --ff-only 重试一次（只允许快进，不产生合并提交）
                rc, _out, errtxt = self.run(
                    ["git", "fetch", "--prune", remote_name,
                     f"+refs/heads/{base}:refs/remotes/{remote_name}/{base}"],
                    repo_path, cap=cap, env=genv)
                if rc != 0:
                    text = self.redact_urls(self._text(errtxt).strip()[:300]) or "fetch 失败"
                    return self._push_frame(repo_path, project, "failed",
                                            f"非快进后 fetch 失败：{text}", **base_frame)
                rc, _out, errtxt = self.run(
                    ["git", "merge", "--ff-only", f"{remote_name}/{base}"], repo_path)
                if rc != 0:
                    ahead2, behind2 = self._remote_counts(repo_path, base, remote_name,
                                                          env=genv)
                    return self._push_frame(
                        repo_path, project, "behind",
                        "推送被拒绝（非快进）且无法快进合并；请先拉取并手工处理分叉",
                        **base_frame, ahead=ahead2, behind=behind2,
                        after=self._head_summary(repo_path))
                rc, _out, errtxt = self.run(
                    ["git", "push", remote_name, f"refs/heads/{base}:refs/heads/{base}"],
                    repo_path, cap=cap, env=genv)
                if rc == 0:
                    return self._push_frame(
                        repo_path, project, "pushed",
                        f"已 fetch + 快进后重试推送 {base} 到 {remote_name}",
                        **base_frame, ahead=ahead, behind=0,
                        after=self._head_summary(repo_path))
                text = self.redact_urls(self._text(errtxt).strip()[:300]) or "推送失败（重试一次后）"
                status = ("behind" if self._non_fast_forward(text)
                          else "rejected" if self._push_rejected(text) else "failed")
                return self._push_frame(repo_path, project, status,
                                        f"重试推送仍失败：{text}", **base_frame,
                                        after=self._head_summary(repo_path))
        except self.GitTimedOut as exc:
            return self._push_frame(repo_path, project, "failed",
                                    f"推送超时: {exc}", **base_frame, timed_out=True)
        except self.GitCancelled:
            return self._push_frame(repo_path, project, "failed",
                                    "操作已取消", **base_frame, cancelled=True)

    def abort_merge(self, project, repo=None):
        err = self.budget_error()
        if err:
            return err
        repo_path = self.resolve_repo(project, repo)
        with self.op_lock(repo_path):
            if "merge" not in self.midstate(repo_path):
                raise ValueError("当前没有进行中的 merge，无法 abort")
            rc, _out, errtxt = self.run(["git", "merge", "--abort"], repo_path)
            if rc != 0:
                return {"status": "failed",
                        "error": self.redact_urls(self._text(errtxt).strip()[:300])
                        or "git merge --abort 失败"}
            return {"status": "aborted", "project": project, "repo": str(repo_path),
                    "head": self._head_summary(repo_path),
                    "message": "已放弃本次合并，工作区恢复到拉取前"}

    def resolve_conflicts(self, project, repo=None, side=None):
        err = self.budget_error()
        if err:
            return err
        if side not in ("ours", "theirs"):
            raise ValueError("action=resolve 时 side 必填，且只支持 ours / theirs")
        repo_path = self.resolve_repo(project, repo)
        with self.op_lock(repo_path):
            mid = self.midstate(repo_path)
            if "merge" not in mid:
                raise ValueError("当前没有进行中的 merge；rebase/cherry-pick 中间态请手工处理，"
                                 "本工具不会自动覆盖")
            entries = self._entries(repo_path)
            conflicts = self._conflicts(entries)
            if not conflicts:
                return {"status": "resolved", "remaining_conflicts": [],
                        "resolved": [], "manual": [],
                        "message": "没有未合并条目，可直接调用 project_git_commit 完成合并"}
            resolved, manual = [], []
            for e in conflicts:
                if e["code"] in ("UU", "AA"):
                    rc, _out, errtxt = self.run(
                        ["git", "--literal-pathspecs", "checkout", f"--{side}",
                         "--", e["path"]], repo_path)
                    if rc != 0:
                        manual.append({"code": e["code"], "path": e["path"],
                                       "suggestion": self.redact_urls(
                                           self._text(errtxt).strip()[:200])})
                        continue
                    rc, _out, errtxt = self.run(
                        ["git", "--literal-pathspecs", "add", "--", e["path"]], repo_path)
                    if rc != 0:
                        manual.append({"code": e["code"], "path": e["path"],
                                       "suggestion": self.redact_urls(
                                           self._text(errtxt).strip()[:200])})
                        continue
                    resolved.append({"code": e["code"], "path": e["path"], "side": side})
                else:
                    suggestion = (f"手工处理后 git add {e['path']}；删除选择用 git rm {e['path']}"
                                  if "D" in e["code"] else
                                  f"手工编辑后 git add {e['path']}")
                    manual.append({"code": e["code"], "path": e["path"],
                                   "suggestion": suggestion})
            remaining = [{"code": e["code"], "path": e["path"]}
                         for e in self._conflicts(self._entries(repo_path))]
            return {"status": "manual_required" if (manual or remaining) else "resolved",
                    "project": project, "repo": str(repo_path), "side": side,
                    "resolved": resolved, "manual": manual,
                    "remaining_conflicts": remaining,
                    "next": "确认冲突已全部解决后调用 project_git_commit 完成合并提交"}

    # ---- 提交 ----
    def _validate_message(self, message):
        if not isinstance(message, str):
            raise ValueError("message 必填且为字符串")
        message = message.strip()
        if not message:
            raise ValueError("message 必填（1..500 字符、单行）")
        if len(message) > self.MAX_MESSAGE_LEN:
            raise ValueError(f"message 超过 {self.MAX_MESSAGE_LEN} 字符上限")
        if "\n" in message or "\r" in message:
            raise ValueError("message 必须为单行")
        if any(ord(ch) < 32 for ch in message):
            raise ValueError("message 不能包含控制字符")
        return message

    def _validate_include_untracked(self, repo_path, include_untracked, untracked_paths):
        if include_untracked is None:
            return []
        if not isinstance(include_untracked, list):
            raise ValueError("include_untracked 必须为路径数组")
        if len(include_untracked) > self.MAX_UNTRACKED:
            raise ValueError(f"include_untracked 最多 {self.MAX_UNTRACKED} 条")
        untracked_set = set(untracked_paths)
        out = []
        for raw in include_untracked:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"include_untracked 需为非空字符串: {raw!r}")
            if len(raw) > self.MAX_PATH_LEN:
                raise ValueError(f"include_untracked 路径超长: {raw[:80]}…")
            text = raw.strip()
            if text.startswith("-"):
                raise ValueError(f"include_untracked 路径不得以 '-' 开头: {text!r}")
            p = Path(text)
            if p.is_absolute():
                try:
                    p = p.resolve()
                    rel = p.relative_to(repo_path)
                except (OSError, ValueError):
                    raise ValueError(f"include_untracked 越界（不在仓库内）: {text!r}")
            else:
                if any(part == ".." for part in p.parts):
                    raise ValueError(f"include_untracked 不得包含 '..': {text!r}")
                if not text or text == ".":
                    raise ValueError(f"include_untracked 非法路径: {text!r}")
                rel = Path(text)
            rel_str = rel.as_posix()
            if rel_str not in untracked_set:
                raise ValueError(f"include_untracked 不是当前未跟踪文件: {rel_str}")
            rc, _out, _err = self.run(["git", "--literal-pathspecs", "check-ignore",
                                       "-q", "--", rel_str], repo_path)
            if rc == 0:
                raise ValueError(f"include_untracked 命中 .gitignore: {rel_str}")
            if rel_str not in out:
                out.append(rel_str)
        return out

    @classmethod
    def sensitive_name_blocked(cls, path):
        name = Path(path).name
        low = name.lower()
        if re.match(r"^\.env(\.|$)", name, re.I):
            if low in (".env.example", ".env.sample", ".env.template"):
                return False
            tail = low[len(".env"):].lstrip(".")
            return tail not in cls.ENV_TEMPLATE_SEGMENTS
        if low in cls.SENSITIVE_NAMES:
            return True
        if low.endswith(cls.SENSITIVE_SUFFIXES):
            return True
        return False

    @classmethod
    def secret_findings(cls, text, label):
        found = []
        for lineno, line in enumerate(text.splitlines(), 1):
            for rule, pattern in cls.SECRET_PATTERNS:
                if pattern.search(line):
                    found.append({"path": label, "line": lineno, "rule": rule})
                    break
        return found

    def _diff_added_findings(self, repo_path):
        try:
            rc, out, _err = self.run(["git", "--no-pager", "diff", "HEAD",
                                      "--no-ext-diff", "--no-textconv",
                                      "--unified=0"], repo_path)
        except (self.GitTimedOut, self.GitCancelled):
            raise
        if rc != 0:
            return []
        findings = []
        current = None
        new_line = 0
        for line in self._text(out).splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
            elif line.startswith("@@"):
                m = re.search(r"\+(\d+)", line)
                new_line = int(m.group(1)) if m else 0
            elif line.startswith("+") and not line.startswith("+++"):
                if current:
                    for rule, pattern in self.SECRET_PATTERNS:
                        if pattern.search(line[1:]):
                            findings.append({"path": current, "line": new_line,
                                             "rule": rule})
                            break
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                continue
            elif line.startswith(" "):
                new_line += 1
            if len(findings) >= self.MAX_FINDINGS:
                break
        return findings

    def _untracked_findings(self, repo_path, paths):
        findings = []
        for rel in paths:
            try:
                p = (repo_path / rel).resolve()
                if not p.is_relative_to(repo_path) or not p.is_file():
                    continue
                if p.stat().st_size > self.MAX_UNTRACKED_SCAN_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            findings.extend(self.secret_findings(data.decode("utf-8", errors="replace"), rel))
            if len(findings) >= self.MAX_FINDINGS:
                break
        return findings[:self.MAX_FINDINGS]

    def fingerprint(self, repo_path):
        head = self._head_sha(repo_path)
        rc, status_out, err = self.run(["git", "status", "--porcelain=v1", "-z",
                                        "--untracked-files=all"], repo_path)
        if rc != 0:
            raise RuntimeError(self.redact_urls(self._text(err).strip()[:300]))
        rc, diff_out, _err = self.run(["git", "--no-pager", "diff", "HEAD",
                                       "--no-ext-diff", "--no-textconv"], repo_path)
        if rc != 0:
            diff_out = b""
        if len(diff_out) > self.MAX_DIFF_FINGERPRINT_BYTES:
            diff_part = (b"len:%d:" % len(diff_out)
                         + hashlib.sha256(diff_out[:self.MAX_DIFF_FINGERPRINT_BYTES]).digest())
        else:
            diff_part = diff_out
        untracked = []
        for rel in self._untracked(self._parse_porcelain(self._text(status_out))):
            try:
                st = (repo_path / rel).stat()
                untracked.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}".encode())
            except OSError:
                untracked.append(f"{rel}:missing".encode())
        h = hashlib.sha256()
        h.update(head.encode())
        h.update(b"\0")
        h.update(status_out)
        h.update(b"\0")
        h.update(diff_part)
        h.update(b"\0")
        h.update(b"\n".join(untracked))
        return h.hexdigest()

    def _commit_preconditions(self, repo_path):
        """返回 (branch, midstate, entries)；不满足前置一律 raise / 返回失败帧。"""
        branch = self._attached_branch(repo_path)
        if branch is None:
            raise ValueError("当前处于 detached HEAD，拒绝提交；请先切回目标分支")
        mid = self.midstate(repo_path)
        for name in ("rebase", "cherry-pick", "revert"):
            if name in mid:
                raise ValueError(f"仓库处于 {name} 中间态，拒绝提交；请先手工完成或放弃该操作")
        entries = self._entries(repo_path)
        conflicts = self._conflicts(entries)
        if "merge" in mid and conflicts:
            raise ValueError("仍有未解决的冲突，请先调用 project_git_pull action=resolve 处理")
        if "merge" not in mid and conflicts:
            raise ValueError("存在未合并条目且不在 merge 中，拒绝提交；请先处理冲突")
        return branch, mid, entries

    def prepare_commit(self, project, repo=None, message=None, include_untracked=None,
                       allow_sensitive=None, allow_secret_findings=False):
        """预览：不 stage、不 commit；返回待提交清单、安全门结果与工作区指纹。"""
        err = self.budget_error()
        if err:
            return err
        message = self._validate_message(message)
        if not isinstance(allow_secret_findings, bool):
            raise ValueError("allow_secret_findings 必须为布尔值")
        if allow_sensitive is None:
            allow_sensitive = []
        if not isinstance(allow_sensitive, list) or any(not isinstance(x, str)
                                                        for x in allow_sensitive):
            raise ValueError("allow_sensitive 必须为路径数组")
        repo_path = self.resolve_repo(project, repo)
        with self.op_lock(repo_path):
            branch, mid, entries = self._commit_preconditions(repo_path)
            tracked = self._tracked(entries)
            untracked_all = self._untracked(entries)
            inc = self._validate_include_untracked(repo_path, include_untracked, untracked_all)
            if not tracked and not inc and "merge" not in mid:
                return {"status": "blocked", "project": project, "repo": str(repo_path),
                        "branch": branch, "error": "没有可提交的改动（已跟踪修改为空，"
                                                   "且未指定 include_untracked）"}
            sensitive = [p for p in inc if self.sensitive_name_blocked(p)]
            blocked = [p for p in sensitive if p not in allow_sensitive]
            if blocked:
                return {"status": "blocked", "project": project, "repo": str(repo_path),
                        "branch": branch, "reason": "sensitive_path",
                        "sensitive_untracked": sensitive, "blocked_files": blocked,
                        "error": "待提交清单包含敏感文件；确认无误后把这些路径同时列入 "
                                 "allow_sensitive 再重新预览"}
            findings = self._diff_added_findings(repo_path)
            findings += self._untracked_findings(repo_path, inc)
            findings = findings[:self.MAX_FINDINGS]
            if findings and not allow_secret_findings:
                return {"status": "blocked", "project": project, "repo": str(repo_path),
                        "branch": branch, "reason": "secret_findings",
                        "secret_findings": findings,
                        "error": "新增内容命中高置信凭证格式；确认无误后传 "
                                 "allow_secret_findings=true 重新预览"}
            try:
                diffstat = self._git(repo_path, "--no-pager", "diff", "HEAD", "--stat")
            except Exception:  # noqa: BLE001
                diffstat = ""
            head = self._head_summary(repo_path)
            fingerprint = self.fingerprint(repo_path)
            return {
                "status": "preview", "project": project, "repo": str(repo_path),
                "repo_name": repo_path.name, "branch": branch,
                "base_branch": self._base_branch(project, repo_path),
                "head": head,
                "tracked_changes": [{"code": e["code"], "path": e["path"]}
                                    for e in tracked[:self.MAX_FILES]],
                "untracked_files": untracked_all[:self.MAX_FILES],
                "include_untracked": inc,
                "sensitive_untracked": sensitive,
                "allowed_sensitive": [p for p in sensitive if p in allow_sensitive],
                "diffstat": diffstat.strip()[:3000],
                "counts": {"tracked": len(tracked), "include_untracked": len(inc)},
                "secret_findings": findings if allow_secret_findings else [],
                "allow_secret_findings": allow_secret_findings,
                "merge_in_progress": "merge" in mid,
                "fingerprint": fingerprint,
                "expires_in": self.TOKEN_TTL_SEC,
            }

    def apply_commit(self, project, repo=None, message=None, include_untracked=None,
                     allow_sensitive=None, allow_secret_findings=False,
                     expected_fingerprint=None, expected_head=None, expected_branch=None):
        """凭预览 token 落地提交：重校验前置 + 分支 + 指纹 + HEAD，再 stage/commit。"""
        err = self.budget_error()
        if err:
            return err
        message = self._validate_message(message)
        if allow_sensitive is None:
            allow_sensitive = []
        if not isinstance(allow_sensitive, list) or any(not isinstance(x, str)
                                                        for x in allow_sensitive):
            raise ValueError("allow_sensitive 必须为路径数组")
        if not isinstance(allow_secret_findings, bool):
            raise ValueError("allow_secret_findings 必须为布尔值")
        repo_path = self.resolve_repo(project, repo)
        with self.op_lock(repo_path):
            current_branch = self._attached_branch(repo_path)
            if expected_branch is not None:
                if current_branch is None:
                    return {"status": "failed",
                            "error": "当前处于 detached HEAD，请切回预览时分支后重新预览"}
                if current_branch != expected_branch:
                    return {"status": "failed",
                            "error": f"当前分支已从 {expected_branch} 切换为 {current_branch}，"
                                     "请重新预览并确认目标分支"}
            if expected_head and self._head_sha(repo_path) != expected_head:
                return {"status": "failed", "error": "HEAD 在预览后发生变化，请重新预览再生效"}
            if not expected_fingerprint or self.fingerprint(repo_path) != expected_fingerprint:
                return {"status": "failed",
                        "error": "工作区在预览后发生了变化，请重新预览并确认"}
            branch, mid, entries = self._commit_preconditions(repo_path)
            tracked = self._tracked(entries)
            untracked_all = self._untracked(entries)
            inc = self._validate_include_untracked(repo_path, include_untracked, untracked_all)
            if not tracked and not inc and "merge" not in mid:
                return {"status": "blocked", "error": "没有可提交的改动"}
            sensitive = [p for p in inc if self.sensitive_name_blocked(p)]
            blocked = [p for p in sensitive if p not in allow_sensitive]
            if blocked:
                return {"status": "blocked", "reason": "sensitive_path",
                        "blocked_files": blocked, "error": "敏感文件未获显式放行"}
            findings = self._diff_added_findings(repo_path)
            findings += self._untracked_findings(repo_path, inc)
            findings = findings[:self.MAX_FINDINGS]
            if findings and not allow_secret_findings:
                return {"status": "blocked", "reason": "secret_findings",
                        "secret_findings": findings, "error": "新增内容命中高置信凭证格式"}
            rc, _out, errtxt = self.run(["git", "add", "-u"], repo_path)
            if rc != 0:
                return {"status": "failed",
                        "error": self.redact_urls(self._text(errtxt).strip()[:300])
                        or "git add -u 失败"}
            for rel in inc:
                rc, _out, errtxt = self.run(["git", "--literal-pathspecs", "add", "--", rel],
                                            repo_path)
                if rc != 0:
                    return {"status": "failed",
                            "error": self.redact_urls(self._text(errtxt).strip()[:300])
                            or f"git add 失败: {rel}"}
            rc, _out, errtxt = self.run(["git", "commit", "-m", message], repo_path)
            if rc != 0:
                return {"status": "failed",
                        "error": self.redact_urls(self._text(errtxt).strip()[:300])
                        or "git commit 失败（已暂存内容保持）"}
            after = self._head_summary(repo_path)
            return {"status": "committed", "project": project, "repo": str(repo_path),
                    "branch": branch, "commit": after,
                    "changed_count": len(tracked) + len(inc),
                    "merge_completed": "merge" in mid,
                    "message": message}

    # ---- 工具 ----
    @staticmethod
    def redact_urls(text):
        if not isinstance(text, str):
            return text
        return re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1***@", text)
