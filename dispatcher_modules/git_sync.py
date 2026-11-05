"""项目级 Git 同步 / 推送（git_sync v1）：主干同步、worktree 创建与清理、合并与推送。"""

import os
import re
import time
from pathlib import Path

import git_ops
import prompt_store
from acp_shared import is_run_local_supervisor

from dispatcher_modules.workspace import _REAL_DEV_WORKTREE_PATH, _REAL_WORKTREES_ROOT, in_test_env
from dispatcher_modules.runtime import D, log, subprocess


# 职责：按项目 git_sync 配置在主工作区做 fetch/快进/分叉判定（before_card + 收尾前），
# 以及 done 合并后按 on_done 推送主干（auto）或只统计待推送（manual）。
# 硬约束：全部 git 远端操作走 git_ops（git_env 禁交互 + repo_lock 跨进程串行 + run_git）；
# 未配置 git_sync（None）时本段所有入口零动作（不发任何 git 命令），既有行为逐字不变；
# 任何失败只 log.warning + post_dispatcher_event(kind=merge)，绝不抛、不阻断需求推进。

def _project_git_cfg(project):
    """项目 git 段（归一后 dict）或 None。"""
    cfg = (D.PROJECTS.get(project) or {}).get("git")
    return cfg if isinstance(cfg, dict) else None


def _project_git_sync(project):
    """项目 git_sync 段（归一后 dict）或 None。None = 未配置 = 零行为变化。"""
    cfg = (D.PROJECTS.get(project) or {}).get("git_sync")
    return cfg if isinstance(cfg, dict) else None


def _git_sync_remote(project):
    return str((_project_git_cfg(project) or {}).get("remote") or "origin")


def _git_sync_timeout(sync):
    try:
        timeout = int((sync or {}).get("timeout") or D.GIT_SYNC_DEFAULTS["timeout"])
    except (TypeError, ValueError):
        timeout = D.GIT_SYNC_DEFAULTS["timeout"]
    return timeout if timeout > 0 else D.GIT_SYNC_DEFAULTS["timeout"]


def _repo_paths(req):
    cfg = D.PROJECTS.get(req.get("project") or "") or {}
    return [str(p) for p in (cfg.get("paths") or []) if p]


def _git_err(err, limit=200):
    """git stderr → 单行摘要（事件/日志用，不泄露凭据：run_git 的 stderr 只含 git 自身输出）。"""
    lines = [ln.strip() for ln in str(err or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "未知错误")[:limit]


def _git_sync_event(req, content):
    """git_sync 失败/需人工留痕：warn + 时间线事件；上报失败不阻断（post_dispatcher_event 已兜底）。"""
    log.warning("需求 #%s git_sync：%s", req.get("id"), content)
    try:
        D.post_dispatcher_event(int(req["id"]), content, kind="merge")
    except Exception:  # noqa: BLE001 - 时间线留痕失败不得阻断收尾
        pass


def _local_base_branch(repo, cfg):
    """主干分支的本地名 + 起点 sha。_default_branch 可能返回 origin/<branch>（本地主干缺失时
    的远端探测结果），这里剥掉远端前缀取本地分支名——后续 merge/push 都用本地名。"""
    ref, sha = D._default_branch(repo, cfg)
    if not ref:
        return None, sha
    if "/" in ref:
        head, tail = ref.split("/", 1)
        try:
            if head in git_ops.remote_names(repo):
                return tail, sha
        except Exception:  # noqa: BLE001 - 远端探测失败按原样使用，不阻断
            pass
    return ref, sha


def _sync_main_worktrees(req, sync, mode=None):
    """主工作区同步（fetch / pull 两档）。返回汇总 dict（测试与调用方只读）：

    {"mode": 实际档位, "fetched": [仓库名], "fast_forwarded": [仓库名],
     "merged": [仓库名]（分叉 overlap 空→自动合并成功）, "diverged": [仓库名],
     "skipped": [仓库名], "failed": [仓库名]}

    规则（逐仓库、with repo_lock 串行）：
    1) git fetch --prune <remote>；
    2) mode=fetch 到此为止（只更新远端引用，不动工作区）；
    3) mode=pull：仅当「主工作区干净」且「当前分支 == 主干」才慢进，否则跳过（绝不 stash/切分支）；
    4) 本地仅落后 → git merge --ff-only <remote>/<base>；
    5) 分叉（本地领先+落后）：diverge=ff-only 跳过；stop 报事件转人工；overlap-check 用
       merge-base 两侧 diff --name-only 求交集，空 → merge --no-edit <remote>/<base>，
       非空 → 跳过 + 事件转人工（不制造半自动冲突现场）。
    所有异常/失败只 warn+事件，返回 failed，不抛。"""
    mode = mode or (sync or {}).get("before_card") or "off"
    result = {"mode": mode, "fetched": [], "fast_forwarded": [], "merged": [],
              "diverged": [], "skipped": [], "failed": []}
    if mode not in ("fetch", "pull"):
        return result
    sync = sync or {}
    for repo in _repo_paths(req):
        try:
            _sync_one_main_repo(req, repo, sync, mode, result)
        except Exception as e:  # noqa: BLE001 - 单个仓库任何异常都不得阻断需求
            name = Path(repo).name
            result["failed"].append(name)
            _git_sync_event(req, f"仓库 {name} 同步异常（{e}），跳过本次自动同步，不影响需求推进")
    return result


def _sync_one_main_repo(req, repo, sync, mode, result):
    """单仓库主工作区同步（_sync_main_worktrees 的子步骤，异常由调用方兜底）。"""
    name = Path(repo).name
    project = req.get("project")
    remote = _git_sync_remote(project)
    timeout = _git_sync_timeout(sync)
    if not Path(repo).exists():
        log.warning("需求 #%s 仓库路径不存在，跳过 git_sync 同步：%s", req.get("id"), repo)
        result["skipped"].append(name)
        return
    if remote not in git_ops.remote_names(repo):
        # 本地纯项目属正常（无远端不改）；不算失败、不刷事件
        log.info("需求 #%s 仓库 %s 无远端 %s，跳过 git_sync 同步", req.get("id"), name, remote)
        result["skipped"].append(name)
        return
    with git_ops.git_env(project) as env:
        with git_ops.repo_lock(repo, timeout=timeout):
            rc, _out, err = git_ops.run_git(repo, ["fetch", "--prune", remote],
                                            env=env, timeout=timeout)
            if rc != 0:
                result["failed"].append(name)
                _git_sync_event(req, f"仓库 {name} fetch {remote} 失败（{_git_err(err)}），"
                                     f"跳过本次自动同步，不影响需求推进")
                return
            result["fetched"].append(name)
            if mode == "fetch":
                return
            _fast_forward_main_repo(req, repo, sync, remote, timeout, env, result, name)


def _fast_forward_main_repo(req, repo, sync, remote, timeout, env, result, name):
    """pull 档：干净 + 在主干才快进/按 diverge 处理分叉（_sync_main_worktrees 的子步骤）。"""
    cfg = D.PROJECTS.get(req.get("project") or "") or {}
    base, _ = _local_base_branch(repo, cfg)
    if not base:
        log.warning("需求 #%s 仓库 %s 主干探测失败，跳过快进", req.get("id"), name)
        result["skipped"].append(name)
        return
    current = git_ops.current_branch(repo)
    if current != base:
        # 不在主干（含 detached HEAD）：不切分支、不 stash，只跳过——主工作区现场由人工掌控
        log.info("需求 #%s 仓库 %s 主工作区当前分支 %r 不是主干 %r，跳过快进",
                 req.get("id"), name, current, base)
        result["skipped"].append(name)
        return
    st_rc, st_out, _st_err = git_ops.run_git(repo, ["status", "--porcelain"],
                                             env=env, timeout=timeout)
    if st_rc != 0 or st_out.strip():
        log.info("需求 #%s 仓库 %s 主工作区有未提交改动，跳过快进（不 stash、不改现场）",
                 req.get("id"), name)
        result["skipped"].append(name)
        return
    ahead, behind = git_ops.ahead_behind(repo, base, remote=remote, env=env)
    if behind is None or ahead is None:
        log.info("需求 #%s 仓库 %s 远端无 %s/%s 分支，无内容可快进",
                 req.get("id"), name, remote, base)
        result["skipped"].append(name)
        return
    if behind == 0:
        return  # 已最新（或仅本地领先，分叉在下面单独判定）
    if ahead == 0:
        rc, _out, err = git_ops.run_git(repo, ["merge", "--ff-only", f"{remote}/{base}"],
                                        env=env, timeout=timeout)
        if rc != 0:
            result["failed"].append(name)
            _git_sync_event(req, f"仓库 {name} 快进合并 {remote}/{base} 失败"
                                 f"（{_git_err(err)}），跳过本次自动同步，不影响需求推进")
        else:
            result["fast_forwarded"].append(name)
            log.info("需求 #%s 仓库 %s 主工作区已快进到 %s/%s", req.get("id"), name, remote, base)
        return
    # 分叉：本地领先 ahead、落后 behind
    diverge = sync.get("diverge") or D.GIT_SYNC_DEFAULTS["diverge"]
    if diverge == "ff-only":
        log.info("需求 #%s 仓库 %s 主工作区分叉（本地 %s / 远端 %s），diverge=ff-only 跳过",
                 req.get("id"), name, ahead, behind)
        result["diverged"].append(name)
        return
    if diverge == "stop":
        _git_sync_event(req, f"仓库 {name} 主工作区分叉（本地领先 {ahead}、落后 {behind}），"
                             f"按 diverge=stop 停止自动同步，转人工处理")
        result["diverged"].append(name)
        return
    # overlap-check：merge-base 两侧改动文件交集为空才允许自动 merge（避免替人做冲突决策）
    mb_rc, mb_out, mb_err = git_ops.run_git(repo, ["merge-base", "HEAD", f"{remote}/{base}"],
                                            env=env, timeout=timeout)
    merge_base = mb_out.strip()
    if mb_rc != 0 or not merge_base:
        result["failed"].append(name)
        _git_sync_event(req, f"仓库 {name} 分叉判定失败（merge-base 不可用：{_git_err(mb_err)}），"
                             f"跳过自动合并，不影响需求推进")
        return
    _lrc, local_out, _lerr = git_ops.run_git(
        repo, ["diff", "--name-only", f"{merge_base}..HEAD"], env=env, timeout=timeout)
    _rrc, remote_out, _rerr = git_ops.run_git(
        repo, ["diff", "--name-only", f"{merge_base}..{remote}/{base}"], env=env, timeout=timeout)
    local_files = {ln for ln in local_out.splitlines() if ln}
    remote_files = {ln for ln in remote_out.splitlines() if ln}
    overlap = sorted(local_files & remote_files)
    if overlap:
        sample = "、".join(overlap[:5]) + ("…" if len(overlap) > 5 else "")
        _git_sync_event(req, f"仓库 {name} 主工作区分叉（本地领先 {ahead}、落后 {behind}），"
                             f"与远端改动重叠文件 {sample}，跳过自动合并转人工处理")
        result["diverged"].append(name)
        return
    rc, _out, err = git_ops.run_git(repo, ["merge", "--no-edit", f"{remote}/{base}"],
                                    env=env, timeout=timeout)
    if rc != 0:
        git_ops.run_git(repo, ["merge", "--abort"], env=env, timeout=timeout)
        result["failed"].append(name)
        _git_sync_event(req, f"仓库 {name} 分叉自动合并 {remote}/{base} 失败"
                             f"（{_git_err(err)}），已 abort，跳过本次同步，不影响需求推进")
        return
    result["merged"].append(name)
    D.post_dispatcher_event(int(req["id"]),
                          f"仓库 {name} 主工作区分叉但改动文件无重叠，已自动合并 {remote}/{base}"
                          f"（本地领先 {ahead}、落后 {behind}）", kind="merge")


def _merge_preflight(req, repo):
    """git_sync 主干守卫（合并前校验）：主工作区当前分支必须是本项目主干 base。
    返回 None=可合并；str=不合并原因（走现有失败/人工通道，绝不在错分支上落合并）。
    fail-open：未配置 base_branch / 探测失败 / 探测异常一律返回 None（维持旧行为——守卫不得
    把原本能合并的卡拦死）；只有确实读到非主干分支才拦截。"""
    try:
        cfg = D.PROJECTS.get(req.get("project") or "") or {}
        base, _ = _local_base_branch(str(repo), cfg)
        if not base:
            return None
        current = git_ops.current_branch(str(repo))
        if not current:
            return f"主工作区分支不可读（detached HEAD），主干应为 {base}"
        if current != base:
            return f"主工作区当前分支 {current}，主干应为 {base}"
    except Exception as e:  # noqa: BLE001 - 守卫探测失败降级为不拦截（不阻断合并主流程）
        log.warning("需求 #%s 仓库 %s 主干守卫校验失败（按可合并继续）: %s", req.get("id"), repo, e)
    return None


def _merge_guard_blockers(req, failed):
    """合并失败仓库复核主干守卫：{仓库路径: 原因}；git_sync 未启用时恒 {}（失败路径零开销）。"""
    if not _project_git_sync(req.get("project")):
        return {}
    out = {}
    for repo in failed or []:
        try:
            why = _merge_preflight(req, repo)
        except Exception as e:  # noqa: BLE001 - 复核失败按非守卫原因处理（沿用旧文案）
            log.warning("需求 #%s 仓库 %s 主干守卫复核失败: %s", req.get("id"), repo, e)
            continue
        if why:
            out[str(repo)] = why
    return out


def _branch_guard_text(guard, limit=3):
    """{仓库: 原因} → 「仓库名：主工作区当前分支 x，主干应为 y」一行文本。"""
    parts = [f"{Path(p).name}：{why}" for p, why in (guard or {}).items()]
    more = "…" if len(parts) > limit else ""
    return "；".join(parts[:limit]) + more


def _push_main_once(repo, remote, base, env, timeout):
    """推送主干（只推 <base> 一个 ref，绝不 --force）；返回 (rc, stderr)。"""
    rc, _out, err = git_ops.run_git(repo, ["push", remote, base], env=env, timeout=timeout)
    return rc, err


def _push_done_worktrees(req, sync):
    """done 合并成功后按 git_sync.on_done 处理主干远端。返回 (push_failed, pending_push)：
    - auto：逐仓库 with repo_lock `git push <remote> <base>`；非快进先 fetch + ff-only 重试一次，
      仍失败记入 push_failed（不阻断 done，本地合并保留）；
    - manual：不推送，统计「领先远端提交数」写入 pending_push（事件 + _set_done 三处提示）；
    - off/None：零动作（不碰远端）。
    push_failed={仓库名}；pending_push={仓库名: 领先提交数 | None(无法统计)}。"""
    if not sync:
        return set(), {}
    on_done = sync.get("on_done") or D.GIT_SYNC_DEFAULTS["on_done"]
    if on_done not in ("auto", "manual"):
        return set(), {}
    if not _repo_paths(req):
        return set(), {}
    failed, pending = set(), {}
    for repo in _repo_paths(req):
        name = Path(repo).name
        try:
            _push_done_one_repo(req, repo, sync, on_done, failed, pending)
        except Exception as e:  # noqa: BLE001 - 推送失败不得阻断 done
            log.warning("需求 #%s 仓库 %s done 推送异常: %s", req.get("id"), name, e)
            if on_done == "auto":
                failed.add(name)
            else:
                pending[name] = None
            _git_sync_event(req, f"仓库 {name} done 收尾远端操作异常（{e}），"
                                 f"本地合并已保留，不阻断需求完成")
    return failed, pending


def _push_done_one_repo(req, repo, sync, on_done, failed, pending):
    """单仓库 done 推送/统计（_push_done_worktrees 的子步骤）。"""
    name = Path(repo).name
    project = req.get("project")
    cfg = D.PROJECTS.get(project or "") or {}
    base, _ = _local_base_branch(repo, cfg)
    remote = _git_sync_remote(project)
    timeout = _git_sync_timeout(sync)
    if not base:
        log.warning("需求 #%s 仓库 %s 主干探测失败，跳过 done %s", req.get("id"), name, on_done)
        if on_done == "auto":
            failed.add(name)
        return
    if remote not in git_ops.remote_names(repo):
        log.info("需求 #%s 仓库 %s 无远端 %s，跳过 done %s（本地项目属正常）",
                 req.get("id"), name, remote, on_done)
        return
    with git_ops.git_env(project) as env:
        with git_ops.repo_lock(repo, timeout=timeout):
            if on_done == "auto":
                rc, err = _push_main_once(repo, remote, base, env, timeout)
                if rc == 0:
                    D.post_dispatcher_event(int(req["id"]),
                                          f"仓库 {name} 主干 {base} 已自动推送到 {remote}", kind="merge")
                    return
                log.warning("需求 #%s 仓库 %s push %s 失败（%s），尝试 fetch+ff 重试一次",
                            req.get("id"), name, base, _git_err(err))
                frc, _fout, ferr = git_ops.run_git(repo, ["fetch", "--prune", remote],
                                                   env=env, timeout=timeout)
                if frc != 0:
                    failed.add(name)
                    _git_sync_event(req, f"仓库 {name} 主干 push 失败且 fetch 重试失败"
                                         f"（{_git_err(ferr)}），本地合并已保留，请手动推送")
                    return
                mrc, _mout, merr = git_ops.run_git(
                    repo, ["merge", "--ff-only", f"{remote}/{base}"], env=env, timeout=timeout)
                if mrc != 0:
                    failed.add(name)
                    _git_sync_event(req, f"仓库 {name} 主干 push 非快进且无法快进合并"
                                         f"（{_git_err(merr)}），本地合并已保留，请手动推送")
                    return
                rc2, err2 = _push_main_once(repo, remote, base, env, timeout)
                if rc2 != 0:
                    failed.add(name)
                    _git_sync_event(req, f"仓库 {name} 主干 fetch+ff 后再次 push 仍失败"
                                         f"（{_git_err(err2)}），本地合并已保留，请手动推送")
                else:
                    D.post_dispatcher_event(int(req["id"]),
                                          f"仓库 {name} 主干 {base} 先快进再推送成功（{remote}）",
                                          kind="merge")
                return
            # manual：不推，只统计领先远端提交数（fetch 只读，保证数字可信）
            frc, _fout, ferr = git_ops.run_git(repo, ["fetch", "--prune", remote],
                                               env=env, timeout=timeout)
            if frc != 0:
                log.warning("需求 #%s 仓库 %s manual fetch 失败（%s），按现有远端引用统计",
                            req.get("id"), name, _git_err(ferr))
            ahead, _behind = git_ops.ahead_behind(repo, base, remote=remote, env=env)
            if ahead is None:
                pending[name] = None
                _git_sync_event(req, f"仓库 {name} 远端无主干 {base} 分支，"
                                     f"待手动推送（git push {remote} {base}）")
            elif ahead > 0:
                pending[name] = ahead
                D.post_dispatcher_event(int(req["id"]),
                                      f"合并完成，仓库 {name} 主干领先 {remote}/{base} {ahead} 个提交，"
                                      f"按 on_done=manual 待手动推送（git push {remote} {base}）",
                                      kind="merge")


def ensure_dev_worktrees(req):
    """analysis 起全部角色：在每个涉及仓库创建独立 worktree（分支 f-<req_id>，幂等），
    分支显式从主干默认分支拉取，保证调研/开发基线 = 主干，不随主工作区 checkout 漂移。
    同一需求跨重试/评审复用同一分支与 worktree。返回 (workdir, {子目录名: worktree 路径})

    #420：测试进程 + 路径未隔离 → 短路为「只返回形状」（不 mkdir / 不跑 git / 不改 state）：
    未隔离用例的假 req id 会真建 f-<日期>-<假id> 分支与 ~/.req-board/worktrees 工作树；
    已隔离用例（WORKTREES_ROOT 或 dev_worktree_path 被 patch）判据不成立，照走真实逻辑。"""
    cfg = D.PROJECTS[req["project"]]
    if (in_test_env() and D.WORKTREES_ROOT == _REAL_WORKTREES_ROOT
            and D.dev_worktree_path is _REAL_DEV_WORKTREE_PATH):
        log.warning("测试进程跳过真实 worktree 创建（#420 短路）: req=%s project=%s",
                    req.get("id"), req.get("project"))
        return D.dev_workdir_for(req), {Path(p).name: D.dev_worktree_path(req, p) for p in cfg["paths"]}
    # git_sync.before_card：仅当本轮确有 worktree 需新建时同步一次主工作区
    # （fetch 档只更新远端引用；pull 档才在干净且同主干时快进/判定分叉）。
    # 已有 worktree 的复用/重试路径不重复同步——避免每次 spawn 都打扰主工作区。
    sync = _project_git_sync(req.get("project"))
    if sync and sync.get("before_card") in ("fetch", "pull"):
        pending_repos = [p for p in cfg["paths"]
                         if not (D.dev_worktree_path(req, p) / ".git").exists()]
        if pending_repos:
            D._sync_main_worktrees(req, sync)
    branch = D.dev_branch(req)
    repo_map = {}
    base = {}
    for p in cfg["paths"]:
        repo = Path(p)
        wt = D.dev_worktree_path(req, p)
        wt.parent.mkdir(parents=True, exist_ok=True)
        if not (wt / ".git").exists():
            has_branch = subprocess.run(["git", "rev-parse", "--verify", "-q", branch],
                                        cwd=repo, capture_output=True, timeout=D.DIAG_TIMEOUT).returncode == 0
            start, start_sha = D._default_branch(repo, cfg)
            # worktree 目录异常丢失时 git 簿记可能残留（分支仍标记已检出），prune 后重试一次
            for attempt in (1, 2):
                try:
                    if has_branch:
                        subprocess.run(["git", "worktree", "add", str(wt), branch],
                                       cwd=repo, check=True, capture_output=True,
                                       timeout=D.GIT_WORKTREE_TIMEOUT)
                    elif start:
                        subprocess.run(["git", "worktree", "add", str(wt), "-b", branch, start],
                                       cwd=repo, check=True, capture_output=True,
                                       timeout=D.GIT_WORKTREE_TIMEOUT)
                    else:
                        subprocess.run(["git", "worktree", "add", str(wt), "-b", branch],
                                       cwd=repo, check=True, capture_output=True,
                                       timeout=D.GIT_WORKTREE_TIMEOUT)
                    break
                except subprocess.CalledProcessError:
                    subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True,
                                   timeout=D.GIT_WORKTREE_TIMEOUT)
                    if attempt == 2:
                        raise
            # 基线记录：分支 HEAD（新建分支 == 主干起点 sha），供 diff 检测与收口使用
            r = subprocess.run(["git", "rev-parse", "--verify", "-q", branch],
                               cwd=repo, capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
            base[str(p)] = (r.stdout.strip() if r.returncode == 0 else (start_sha or ""))
        else:
            # 幂等复用（跨重试/评审）：仅补记缺失的基线，不覆盖既有记录（#343 现场：
            # dev commit 后 resume 会话再次进入本分支，旧实现无条件用分支当前 HEAD
            # 覆盖 base，has_code_changes 判「无代码变更」死循环）。补记用
            # merge-base(默认分支, 分支) = 分叉点 = 主干起点，与新建分支时的语义一致
            existing = D.state.get("worktrees", {}).get(str(req["id"]), {}).get("base", {})
            recorded = existing.get(str(p)) if isinstance(existing, dict) else None
            if recorded:
                base[str(p)] = recorded
            else:
                start, start_sha = D._default_branch(repo, cfg)
                mb = ""
                if start:
                    r = subprocess.run(["git", "merge-base", start, branch],
                                       cwd=repo, capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
                    if r.returncode == 0:
                        mb = r.stdout.strip()
                base[str(p)] = mb or start_sha or ""
        D._link_repo_local_config(wt)   # 本地配置与主工作区同源（单仓库测试语义一致，见函数注释）
        repo_map[Path(p).name] = wt
    with D.state_lock:
        rec = D.state.setdefault("worktrees", {}).get(str(req["id"])) or {}
        rec.update({
            "project": req["project"], "branch": branch, "base": base,
            # (归属仓库, worktree 路径)：删除 worktree 必须在归属仓库 cwd 下执行；
            # 注意每个 p 重新计算 wt（勿用循环残留变量，否则所有条目指向最后一个仓库）
            "worktrees": [(str(Path(p)), str(D.dev_worktree_path(req, p))) for p in cfg["paths"]]})
        D.state["worktrees"][str(req["id"])] = rec
    D.save_state()
    return D.dev_workdir_for(req), repo_map

def dev_repo_listing(req, repo_map, focus=None):
    wd = D.dev_workdir_for(req)
    branch = D.dev_branch(req)
    if len(repo_map) == 1:
        name, wt = next(iter(repo_map.items()))
        return f"工作目录：{wd}（独立 worktree，分支 {branch}）"
    lines = [f"工作目录：{wd}（dev 聚合目录，非 git 仓库；各仓库 worktree 子目录），分支 {branch}",
             "仓库清单（子目录 -> worktree）："]
    focus = set(focus or [])
    for name, wt in repo_map.items():
        mark = "（本次相关）" if focus and name in focus else ""
        lines.append(f"- {wd / name} -> {wt}{mark}")
    if focus:
        lines.append(f"说明：工作目录包含全部 {len(repo_map)} 个仓库的 worktree；"
                     f"本次需求相关仓库 {len(focus)} 个（analysis/notes 识别，指引用）。"
                     f"清单外仓库确有需要也可直接修改，完成后系统按实际改动识别涉及仓库。")
    return "\n".join(lines)

def cleanup_dev_worktrees(req_id, delete_branch=True, keep_repos=None):
    """清理需求的 dev worktrees（可选删分支）。done 合并后、人工接管、启动兜底时调用。
    delete_branch: True=全删分支；False=全留；"changed"=只删「未实际改动」仓库的分支
    （keep_repos=有改动仓库名集合，用于仅推分支项目收口：有改动的分支保留到发版，空分支删除）。"""
    with D.state_lock:
        rec = D.state.get("worktrees", {}).pop(str(req_id), None)
    if not rec:
        return
    # 每个 worktree 必须在它归属的仓库 cwd 下删除，否则 git 找不到对应仓库
    for repo, wt in rec.get("worktrees") or []:
        r = subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=repo, capture_output=True,
                           timeout=D.GIT_WORKTREE_TIMEOUT)
        if r.returncode != 0:
            log.warning(f"清理 worktree {wt} 失败: {r.stderr.decode(errors='replace')[:200]}")
    if delete_branch:
        keep = set(keep_repos or []) if delete_branch == "changed" else None
        for p in D.PROJECTS.get(rec["project"], {}).get("paths", []):
            if keep is not None and Path(p).name in keep:
                continue
            r = subprocess.run(["git", "branch", "-D", rec["branch"]], cwd=p, capture_output=True,
                               timeout=D.DIAG_TIMEOUT)
            if r.returncode != 0:
                log.warning(f"删除分支 {rec['branch']}@{p} 失败: {r.stderr.decode(errors='replace')[:200]}")
    D.save_state()

def push_company_branches(req, branch, repos):
    """#288：仅推分支项目（branch_only）done 前把有改动仓库的 f-分支 push 到 origin。
    只 push <branch> 分支本身（不 --force、不推其他 refs）；仓库无 origin 远端属正常
    （本地纯项目），跳过不算失败。返回 push 失败仓库名集合——失败不阻断 done，
    由 _set_done（push_failed）转 summary/时间线/done 卡片红字三处提醒手动 push。"""
    failed = set()
    for p in D.PROJECTS[req["project"]]["paths"]:
        name = Path(p).name
        if name not in repos:
            continue
        try:
            has_origin = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=p,
                                        capture_output=True, text=True, timeout=D.DIAG_TIMEOUT).returncode == 0
            if not has_origin:
                log.info(f"需求 #{req['id']} 仓库 {name} 无 origin 远端，跳过 push")
                continue
            r = subprocess.run(["git", "push", "origin", branch], cwd=p,
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                log.warning(f"需求 #{req['id']} push {branch}@{name} 失败: "
                            f"{r.stderr.decode(errors='replace')[:200]}")
                failed.add(name)
        except Exception as e:
            log.warning(f"需求 #{req['id']} push {branch}@{name} 异常: {e}")
            failed.add(name)
    return failed


def merge_dev_branches(req):
    """把 dev 分支 --no-ff 合并回各仓库 main（main worktree）。返回 (ok, 失败仓库列表)
    #412（C）：补 timeout + TimeoutExpired 时尽力 git merge --abort（清理超时残留的
    MERGE_HEAD 现场）并列入 failed；正常冲突（returncode!=0）保留现场语义不变
    （merge_resolve 依赖冲突现场）。
    git_sync 主干守卫：项目配置了 git_sync 时，合并前校验主工作区当前分支 == 本项目主干
    （_merge_preflight）；不一致不执行 merge（否则会把 dev 落进错误分支），记入 failed 并留
    kind=merge 事件，结论由调用方走现有失败/人工通道（文案见 _merge_conflict_fallback(guard)，
    不得误报「工作区脏」）。未配置 git_sync = 零额外校验、行为逐字不变。"""
    branch = D.dev_branch(req)
    failed = []
    sync_enabled = bool(_project_git_sync(req.get("project")))
    for p in D.PROJECTS[req["project"]]["paths"]:
        if sync_enabled:
            why = _merge_preflight(req, p)
            if why:
                log.warning("需求 #%s 仓库 %s 未合并（git_sync 主干守卫）：%s", req["id"], p, why)
                D.post_dispatcher_event(int(req["id"]),
                                      f"自动合并跳过：{Path(p).name} {why}；请在主干上重试合并",
                                      kind="merge")
                failed.append(str(p))
                continue
        try:
            r = subprocess.run(["git", "merge", "--no-ff", branch, "-m", f"auto merge req #{req['id']}"],
                               cwd=p, capture_output=True, timeout=D.GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning(f"需求 #{req['id']} 仓库 {p} 合并超时（>{D.GIT_TIMEOUT}s），尝试 abort 清理残留")
            try:
                subprocess.run(["git", "merge", "--abort"], cwd=p, capture_output=True,
                               timeout=D.GIT_TIMEOUT)
            except Exception as e:
                log.warning(f"需求 #{req['id']} 仓库 {p} 合并超时后 abort 失败: {e}")
            failed.append(str(p))
            continue
        if r.returncode != 0:
            failed.append(str(p))
    return (not failed), failed


def _merge_dirty_blockers(req, failed):
    """合并失败后区分「main 工作区未提交改动挡住合并」与真冲突（仅失败路径调用，成功路径零开销）。
    git 预检拒绝（local changes / untracked files would be overwritten）不会留下 MERGE_HEAD，
    此时取工作区未提交 + 未跟踪文件 ∩ dev 分支改动文件。merge_resolve 会话改的是 dev 分支，
    解决不了 main 工作区的脏现场，调用方据此跳过 merge_resolve 并给出准确提示。
    返回 {仓库路径: [文件...]}，只含被挡住的仓库；探测异常的仓库按真冲突处理（fail-closed）。"""
    branch = D.dev_branch(req)
    blockers = {}
    for p in failed:
        try:
            if subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=p,
                              capture_output=True, timeout=D.DIAG_TIMEOUT).returncode == 0:
                continue  # 有 MERGE_HEAD = 真冲突现场
            st = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                                cwd=p, capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
            df = subprocess.run(["git", "diff", "--name-only", "-z", f"HEAD...{branch}"],
                                cwd=p, capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
            if st.returncode != 0 or df.returncode != 0:
                continue
            dirty, fields = set(), st.stdout.split("\0")
            i = 0
            while i < len(fields):
                ent = fields[i]
                i += 1
                if len(ent) < 4:
                    continue
                dirty.add(ent[3:])
                if ent[0] in "RC":
                    i += 1  # -z 下重命名/复制条目后跟原路径字段
            hit = sorted(dirty & {f for f in df.stdout.split("\0") if f})
            if hit:
                blockers[str(p)] = hit
        except Exception as e:
            log.warning(f"需求 #{req['id']} 仓库 {p} 合并失败原因探测失败（按真冲突处理）: {e}")
    return blockers


def _dirty_blockers_text(blockers, limit=5):
    """{仓库: [文件]} → 「my-app：a.py、b.html」一行文本（每仓库最多列 limit 个文件）"""
    parts = []
    for p, files in blockers.items():
        more = f" 等 {len(files)} 个" if len(files) > limit else ""
        parts.append(f"{Path(p).name}：{'、'.join(files[:limit])}{more}")
    return "；".join(parts)


def _merge_fail_text(failed, blockers):
    """合并重试失败的提示文本：工作区脏现场给出文件与处理方式，否则沿用「冲突（仓库）」口径"""
    if blockers:
        return (f"被 main 工作区未提交改动挡住，非代码冲突（{_dirty_blockers_text(blockers)}）"
                "——请先提交或 stash 这些改动再重试")
    return f"仍冲突（{', '.join(failed)}）"

def _wait_url_up(url, timeout=10, interval=0.5):
    """轮询 url 直到返回 200（服务重启窗口内连接会被拒），返回是否就绪。
    #300：从 _wait_board_up 参数化而来，供项目 restart 配方的 verify 就绪校验复用"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with D._opener.open(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _wait_board_up(timeout=10):
    """kickstart 后轮询看板 API 直到可访问（重启窗口内连接会被拒），返回是否就绪"""
    return D._wait_url_up(D.BOARD_API, timeout=timeout)


def _kickstart_board_if_own(project, req_id):
    """项目仓库就是看板自身（REPO_DIR）时，合并后 kickstart launchd 托管看板让新代码生效。
    #264：重启动作落调度器时间线——成功/失败（异常或非零退出码）均发 kind=merge 事件，失败保留 log.warning。
    #8：run_local 直跑模式无 launchd（kickstart 必然失败）→ 不调 launchctl，降级为「事件 +
    # 心跳 board_restart_pending 标记」（看板经 /api/dispatcher-health 透出、页头角标提示人工重启），
    # 不产生「失败」事件；launchd 分支逐字不变。"""
    try:
        paths = [str(Path(p).resolve()) for p in D.PROJECTS[project]["paths"]]
        if str(D.REPO_DIR.resolve()) in paths:
            if is_run_local_supervisor():
                msg = "配置已合并，重启看板后生效；直跑模式请手动重启（Ctrl-C 后重跑 run_local.sh）"
                try:
                    D.write_heartbeat({"board_restart_pending": {"at": time.time(), "req_id": int(req_id)}})
                except Exception as e:
                    # 写失败只告警不误报失败：事件本身仍是有效提示通道
                    log.warning(f"写入看板待重启心跳标记失败: {e}")
                D.post_dispatcher_event(int(req_id), msg, kind="merge")
                log.info(f"需求合并后看板待重启（run_local 直跑模式，已提示人工处理）")
                return
            r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/local.req-board"],
                               capture_output=True, timeout=D.LAUNCHCTL_TIMEOUT)
            if r.returncode != 0:
                # 非零退出码不会抛异常，需显式视为失败（否则失败路径无事件）
                reason = (r.stderr or b"").decode(errors="replace").strip() \
                    or f"launchctl kickstart 退出码 {r.returncode}"
                raise RuntimeError(reason)
            log.info(f"需求合并后已重启看板（local.req-board）")
            # #266：kickstart 后看板端口尚未就绪，立即写事件会被 ConnectionRefused 吞掉；
            # 等就绪再写，超时按失败处理（失败事件 + 告警）
            if D._wait_board_up():
                D.post_dispatcher_event(int(req_id), "合并后已重启看板（local.req-board），新代码生效", kind="merge")
            else:
                raise RuntimeError("看板重启后 10s 内未就绪")
    except Exception as e:
        log.warning(f"重启看板失败: {e}")
        D.post_dispatcher_event(int(req_id), f"看板自动重启失败: {e}，需人工处理", kind="merge")


def run_project_restart(project, req_id):
    """#300：done 流程本地合并成功后，按 projects.json 的 restart 配方自动重启项目本地服务。
    配方 {cmd, verify?, timeout?}：先执行 cmd（shell、cwd=项目首个仓库路径、带执行超时），
    再轮询 verify 直到返回 200（未配 verify 则 cmd 成功即视为就绪）；成功发 kind=merge 事件留痕。
    返回失败原因字符串（"" 表示成功或未配置），失败由调用方传 _set_done(restart_failed) 落
    summary + 时间线 + done 卡片红字三处留痕，一律不阻断 done。
    任何异常（cmd 超时/非零退出/verify 超时/配置畸形）全部降级为原因字符串，不抛出。
    仅推分支（company/branch_only）项目不本地合并、天然走不到这里；req-board 保留 _kickstart_board_if_own 特例，
    不建议同时配 restart（避免双重重启）。"""
    cfg = D.PROJECTS.get(project, {}).get("restart")
    if not cfg:
        return ""
    cmd = str(cfg.get("cmd") or "").strip()
    if not cmd:
        return ""
    path = (D.PROJECTS[project].get("paths") or [None])[0]
    verify = str(cfg.get("verify") or "").strip()
    timeout = max(1, int(cfg.get("timeout") or 30))
    try:
        # shell=True：配方命令可能含 $(id -u) 等 shell 语法（launchctl kickstart）；
        # projects.json 为本机可信数据，无注入面
        r = subprocess.run(cmd, shell=True, cwd=path, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or b"").decode(errors="replace").strip()[:200] \
                or f"退出码 {r.returncode}"
            return f"重启命令执行失败（退出码 {r.returncode}）：{detail}"
    except subprocess.TimeoutExpired:
        return f"重启命令执行超时（>{timeout}s）"
    except Exception as e:
        return f"重启命令执行异常：{e}"
    if verify and not D._wait_url_up(verify, timeout=timeout):
        return f"服务 {timeout}s 内未就绪（verify {verify} 未返回 200）"
    log.info(f"需求 #{req_id} 已自动重启项目服务（{project}：{cmd[:100]}"
             + (f"，就绪校验 {verify}" if verify else "") + "）")
    D.post_dispatcher_event(int(req_id),
                          f"已自动重启项目服务（{project}）"
                          + (f"，就绪校验通过：{verify}" if verify else ""),
                          kind="merge")
    return ""

# #318：重启类待办对账（restart 配方已成功执行时）：纯重启条目剔除，混合条目标注保留人工部分。
# 匹配「重启/restart + 目标（服务/进程/实例/应用/server）」短语，如「重启 com.example.web Web 服务」；
# 不匹配「重启看板/调度器」等（restart 配方不负责这些，误剔会吞掉真实人工事项）
_RESTART_TODO_ACT_RE = re.compile(
    r"(?:重启|restart)[^。\n]{0,80}?(?:服务|进程|实例|应用|server)", re.IGNORECASE)
# 重启片段剔除后剩余内容可能带重启方式说明（括号块，如「（pkill -f src.web，launchd 自动拉起）」），
# 先剥离再判人工信号，避免说明性残留被误判为人工事项
_RESTART_TODO_BRACKET_RE = re.compile(r"^[（(【\[][^)）】\]]*[)）】\]]")
# 人工部分前置的承接片段（「）后」「然后」「并」等），标注前剥掉保持语义干净
_RESTART_TODO_LEAD_RE = re.compile(
    r"^(?:[、，,；;。\s]*(?:然后|再|并|后|and|then)[、，,；;。\s]*)")
# 剩余内容的人工动作信号（承接词/人工动作词）：有此信号才保留并标注
_RESTART_TODO_HUMAN_RE = re.compile(
    r"后|然后|再|并|验收|检查|验证|确认|测试|查看|核对|verify|check|test", re.IGNORECASE)


def _reconcile_restart_todos(todos):
    """restart 配方已成功执行时的 todos 对账（#318）：
    非重启条目原样保留；重启条目剔除重启动作片段（含括号内的重启方式说明）后，剩余仍含
    人工事项（浏览器验收等）则标注「（服务已由调度器自动重启）」保留，纯重启条目直接剔除。
    返回对账后的新列表（不修改入参）。"""
    out = []
    for todo in todos:
        if not _RESTART_TODO_ACT_RE.search(todo):
            out.append(todo)
            continue
        rest = _RESTART_TODO_ACT_RE.sub("", todo).strip()
        rest = _RESTART_TODO_BRACKET_RE.sub("", rest).strip()
        rest = _RESTART_TODO_LEAD_RE.sub("", rest).strip(" \t，,；;。")
        if not rest or not _RESTART_TODO_HUMAN_RE.search(rest):
            continue  # 纯重启条目（剩余仅为重启方式说明）：剔除
        out.append(f"{rest}（服务已由调度器自动重启）")
    return out

# 改动涉及调度器自身模块（dispatcher.py / acp_shared.py）时 done 通知/总结附带的「需手动重启调度器」提醒文案（#380 起为兜底文案）
DISPATCHER_RESTART_HINT = ("改动涉及调度器模块（dispatcher.py / acp_shared.py 及其拆分模块），已合并；"
                           "需手动重启调度器生效（kill <pid>，launchd KeepAlive 自动拉起）")
# #8：run_local 直跑模式兜底提示（无 KeepAlive，人工 Ctrl-C 后重跑 run_local.sh）
DISPATCHER_RESTART_HINT_RUN_LOCAL = ("改动涉及调度器模块（dispatcher.py / acp_shared.py 及其拆分模块），已合并；"
                                     "直跑模式请手动重启：Ctrl-C 后重跑 run_local.sh")
# #8：来源标记新鲜度上限（秒）——at 距今超过 TTL 视为陈旧，不触发 exec 自重启
# （防残留标记在窗口外被随后 Ctrl-C 的 kill 0 SIGTERM 命中而劫持停止语义）
RESTART_SOURCE_TTL_SEC = 300
# #380：done 流程自动重启调度器（调度器模块改动免手动重启）——命中时写入 pending_restart，
# 主循环择机 kill -TERM 自身（KeepAlive 拉起），重启成功由新进程启动自证补发通知。
DISPATCHER_AUTO_RESTART_PENDING = "改动涉及调度器模块（dispatcher.py / acp_shared.py 及其拆分模块），已合并；调度器将自动重启生效（无需手动操作）"
DISPATCHER_AUTO_RESTART_DELAYED = "改动涉及调度器模块（dispatcher.py / acp_shared.py 及其拆分模块），已合并；将在并行 agent 结束后自动重启调度器生效"
DISPATCHER_AUTO_RESTART_OK = "调度器已自动重启，调度器模块（dispatcher.py / acp_shared.py 及其拆分模块）改动已生效"
# #380 自动重启判定文件名清单（#423）：调度器进程内加载的模块改动都要计入，否则只改 acp_shared.py
# 时静默不生效（旧清单只认 dispatcher.py）
RESTART_TRIGGER_FILES = frozenset({"dispatcher.py", "acp_shared.py", "git_ops.py", "storage_time.py"})
# 调度器进程内加载的包目录（仓库根下一级）：dispatcher.py / acp_shared.py 拆出的模块与通知渠道
RESTART_TRIGGER_DIRS = frozenset({"dispatcher_modules", "acp_modules", "notifiers"})


def is_restart_trigger(path):
    """仓库内相对路径是否属于调度器进程加载的代码（改动合并后需重启调度器才生效）。"""
    p = Path(path)
    return p.name in D.RESTART_TRIGGER_FILES or (len(p.parts) > 1 and p.parts[0] in D.RESTART_TRIGGER_DIRS)

MAX_AUTO_RESTART_ATTEMPTS = 3  # 自动重启 kill 重试上限（超过转手动提醒兜底）

def _branch_base(req, repo_path):
    """dev 分支三点 diff 基线：优先取建 worktree 时记录的主干起点 commit sha；
    无记录（存量 state/兜底）时回退主工作区当前分支 HEAD。返回空串表示无法确定基线。"""
    rec = D.state.get("worktrees", {}).get(str(req["id"]))
    base_sha = ((rec or {}).get("base") or {}).get(str(repo_path)) or ""
    if base_sha:
        return base_sha
    try:
        return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path,
                              capture_output=True, text=True, timeout=D.DIAG_TIMEOUT).stdout.strip()
    except Exception:
        return ""

def _touches_dispatcher(req, branch):
    """检测 dev 分支相对各仓库基线（主干起点，见 _branch_base）的变更文件是否命中
    调度器进程内加载的代码（is_restart_trigger：RESTART_TRIGGER_FILES 按文件名 + RESTART_TRIGGER_DIRS 包目录）。
    命中说明需求改动长驻进程（调度器自身），合并后需重启才生效。
    提醒为附加信息不阻断：任何仓库检测失败/无该分支时静默返回 False（宁缺勿滥）。"""
    files = set()
    for p in D.PROJECTS[req["project"]]["paths"]:
        try:
            base = D._branch_base(req, p)
            if not base:
                continue
            r = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=p,
                               capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
            if r.returncode == 0:
                files.update(f.strip() for f in r.stdout.splitlines() if f.strip())
        except Exception as e:
            log.warning(f"检测需求 #{req['id']} dev 分支变更文件失败（{p}）: {e}")
    return any(is_restart_trigger(f) for f in files)

# #394：合并改动路径三档判定（skip/restart/unknown）与 diff 采集。判定仅按路径前缀匹配，
# 无 IO；skip 判断静态文件（按请求读盘不重启），trigger 为需重启的服务端目录/文件，
# 未知路径（不在 skip ∪ trigger）保守不自动重启 + 告警人工确认（fail-closed：diff 获取
# 失败同样按 unknown 处理）。判定清单按项目存于 projects.json（按各项目目录
# 结构特化），skip_prefixes 缺省 ["src/web/static/"]。
DEFAULT_RESTART_SKIP_PREFIXES = ("src/web/static/",)


def _collect_merge_changed_paths(req, branch):
    """#394：采集 dev 分支相对各仓库基线（_branch_base）的改动文件相对路径列表（保留完整
    相对路径以做前缀匹配，区别于 _touches_dispatcher 只取 basename）。任一仓库基线缺失/
    diff 失败 → 返回 None（调用方按 unknown 保守处理，不静默）。"""
    files = []
    for p in D.PROJECTS[req["project"]]["paths"]:
        try:
            base = D._branch_base(req, p)
            if not base:
                return None
            r = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=p,
                               capture_output=True, text=True, timeout=D.DIAG_TIMEOUT)
            if r.returncode != 0:
                return None
            files.extend(f.strip() for f in r.stdout.splitlines() if f.strip())
        except Exception as e:
            log.warning(f"需求 #{req['id']} 采集合并改动文件清单失败（{p}）: {e}")
            return None
    return files


def _judge_restart_needed(changed_paths, project_cfg):
    """#394：按改动文件路径三档判定（纯前缀匹配，无 IO）。
    返回 "skip" | "restart" | "unknown"：
    - 全部改动 ∈ skip_prefixes（或空清单/纯合并无改动）→ "skip"（不重启无告警）；
    - 命中 trigger_prefixes 且无未知路径 → "restart"（执行 restart 配方）；
    - 存在未知路径（任一路径 ∉ skip ∪ trigger）→ "unknown"（未知优先，保守不重启+告警）。
    changed_paths 为 None（采集失败）时调用方已按 unknown 处理，函数按空清单返回 skip，
    但调用方不应依赖该行为。"""
    cfg = project_cfg or {}
    restart = cfg.get("restart") or {}
    trigger = tuple(str(p) for p in (restart.get("trigger_prefixes") or ()) if str(p).strip())
    skip = tuple(str(p) for p in (restart.get("skip_prefixes")
                                  or DEFAULT_RESTART_SKIP_PREFIXES) if str(p).strip())
    paths = [str(p) for p in (changed_paths or []) if str(p).strip()]
    if not paths:
        return "skip"

    def _hit(path, prefixes):
        return any(path.startswith(t) for t in prefixes)

    unknown = [p for p in paths if not (_hit(p, trigger) or _hit(p, skip))]
    if unknown:
        return "unknown"
    return "restart" if any(_hit(p, trigger) for p in paths) else "skip"


def _maybe_auto_restart_after_merge(req, merge_files, req_id):
    """#394：合并成功后按项目开关与判定结果处理自动重启（finalize_done / on_message /
    网页端重试合并三处共用）。merge_files 为合并前采集的改动文件清单（None=采集失败）。
    返回 (restart_failed, restart_ok)：
    - 开关未开：维持 #300 行为（走 run_project_restart，有配方则执行）；
    - 开关开 + "restart"：执行 run_project_restart；
    - "skip"：不执行（不重启无告警）；
    - "unknown"（含采集失败）：不执行 + 告警（summary/merge 事件，调用方传 _set_done
      restart_warn 留痕，不阻断 done）。
    仅推分支（company/branch_only）项目不本地合并、天然不走这里。"""
    cfg = D.PROJECTS.get(req["project"], {}) or {}
    if not cfg.get("auto_restart_after_merge"):
        restart_failed = D.run_project_restart(req["project"], req_id) or None
        return restart_failed, restart_failed is None and bool(cfg.get("restart")), ""
    verdict = _judge_restart_needed(merge_files, cfg) if merge_files is not None else "unknown"
    if verdict == "restart":
        restart_failed = D.run_project_restart(req["project"], req_id) or None
        return restart_failed, restart_failed is None, ""
    if verdict == "unknown":
        unknown = [p for p in (merge_files or []) if p]
        sample = "、".join(sorted(unknown)[:5]) or "（合并改动文件清单获取失败）"
        warn = (f"改动文件含无法判定是否需重启的路径（{sample}），已按未知路径保守跳过自动重启；"
                f"若确认需要请人工重启（{(cfg.get('restart') or {}).get('cmd', '')[:80] or '重启服务'}）")
        return None, False, warn
    return None, False, ""  # skip：不重启无告警


def _retry_rec(req_id):
    """读重试记录，兼容旧 int 值（#109 升级前状态文件 retries[req_id] 为纯计数）。
    返回 dict：{"count": int, "last_failure": str|None}（last_failure 为最近一次失败原因）。"""
    rec = D.state["retries"].get(req_id)
    if isinstance(rec, dict):
        return {"count": rec.get("count", 0), "last_failure": rec.get("last_failure")}
    return {"count": rec if isinstance(rec, int) else 0, "last_failure": None}


def resume_failure_hint(req_id):
    """#109：续跑注入上次失败原因（state 无失败记录返回空串）。
    主循环重捞 resume 时拼进 extra，重试不再盲续；进程内重试走 handle_result 既有 extra。"""
    lf = _retry_rec(str(req_id)).get("last_failure")
    if not lf:
        return ""
    return prompt_store.render("system/retry-hint", last_failure=lf)


def clear_retries(req_id):
    """阶段成功推进时清零重试计数：语义为每阶段独立 MAX_RETRIES 次容错额度，
    analysis 失败计数不带到 dev（否则 dev 首次失败就显示「第 2 次」且只剩 1 次额度）。
    仅在成功推进路径调用（analysis 完成→dev/route、评审通过→下一阶段、dev→accept 等）；
    _set_done 另有 pop（幂等，保留）。
    #405：重做轮数（flow_review_rounds）由 clear_flow_rounds 管理（仅人工裁决/新工作周期清），
    本函数不再联动清——评审打回重做完成后轮数跨重做连续累计（#376 上限恢复生效）。"""
    with D.state_lock:
        D.state["retries"].pop(str(req_id), None)
        # #11：阶段成功推进=新的模型请求周期，清掉本轮降级记录（防跨阶段重复汇总）
        D.state.setdefault("model_fallback", {}).pop(str(req_id), None)
    D.save_state()


def clear_flow_rounds(req_id):
    """#405：评审轮内重做计数清零——仅人工裁决/新工作周期调用。与 retries 语义拆分：
    retries 随阶段成功推进清（各阶段独立容错额度），flow_review_rounds 跨打回重做连续累计
    （第 1–3 次拒自动打回重做，第 4 次拒触发强制人工裁决，FLOW_REVIEW_ROUNDS_MAX=3）。
    幂等：无记录 no-op；旧 state 残留方向偏保守（更快强制人评），可接受。"""
    with D.state_lock:
        D.state.get("flow_review_rounds", {}).pop(str(req_id), None)
        D.state.get("review_escalation", {}).pop(str(req_id), None)    # C3 升级记录同周期清
        D.state.get("review_round_points", {}).pop(str(req_id), None)
    D.save_state()

def merge_conflict_write(req_or_id, action=None):
    """#386 merge 冲突决策通道表写（POST 登记/重置 action；None=等待用户操作，按钮可见）。
    写失败不阻断既有收尾：降级=网页按钮不可见，飞书 y/n 通道仍可用；事件留痕便于追溯。"""
    try:
        D.api("POST", f"/{req_or_id}/merge-conflict", {"action": action})
        return True
    except Exception as e:
        log.warning(f"需求 #{req_or_id} merge 冲突表登记(action={action})失败（降级飞书通道）: {e}")
        try:
            D.post_dispatcher_event(int(req_or_id),
                                  f"网页端 merge 决策按钮登记失败（{e}），降级飞书 y/n 通道", kind="merge")
        except Exception:
            pass
        return False


def merge_conflict_clear(req_or_id, reason=""):
    """#386 merge 冲突决策通道表清除（消费成功/人工接管/幽灵决策清理；幂等，失败不阻断）。"""
    try:
        D.api("DELETE", f"/{req_or_id}/merge-conflict")
    except Exception as e:
        log.warning(f"需求 #{req_or_id} merge 冲突表清除失败（{reason}）: {e}")


