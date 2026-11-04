#!/usr/bin/env python3
"""测试隔离审计：探针跑全量，列出会污染真实环境的用例（探针本身不产生任何真实副作用）。

背景（#419 清理现场）：`build_prompt` 首句就是 `ensure_dev_worktrees`，因此凡是调用
`build_prompt` / `spawn_agent` 而没做隔离的用例，会**真的**在 `~/.req-board/worktrees` 下建
worktree、在仓库里建 `f-<日期>-<id>` 分支；未 mock `save_state` 的用例还会把测试态写进真实
`data/dispatcher_state.json`。全量跑多了就攒出上百个残留目录（曾一次清掉 165 个），也会误改
真实调度器 state。

做法：把 `dispatcher.ensure_dev_worktrees` 与 `save_state` 换成探针（返回临时 workdir / 不落盘），
跑全量后按用例汇总：
  A. 走到**未隔离**的真实 `ensure_dev_worktrees` 的用例——判定依据是调用时 `WORKTREES_ROOT`
     与 `dev_worktree_path` 都仍是真实值；这些用例会真建 worktree + 真建分支。
  B. `save_state` 调用时 `STATE_FILE` 仍指向真实文件的用例——这些会覆盖真实调度器 state。
本就隔离地直接测 `ensure_dev_worktrees` 的用例（把 `dev_worktree_path`/`WORKTREES_ROOT` 指到
临时目录，如 tests/test_worktree_base_stability.py、tests/test_worktree_local_config.py）不算命中。

用法（worktree 内同样可用，解释器用 main 工作区的 .venv）：
    .venv/bin/python scripts/test_isolation_audit.py
退出码：有 A/B 命中 → 1；全隔离 → 0。

注意：探针替换了实现，故本轮**部分用例必然失败**（属预期，含 tests/test_state_isolation_guard.py
直接测真实 `save_state`/`ensure_dev_worktrees` 的用例），脚本只关心 A/B 清单。
#420 起 dispatcher 侧另有兜底（未 `load_state` 的进程 `save_state` 拒绝落盘；测试进程对未隔离路径的
`ensure_dev_worktrees` 短路），探针替换实现看不到这层，A/B 判据仍按本脚本口径。运行务必**裸跑**
（不设 `REQ_BOARD_STATE`）：带上 env 会让本可隔离的用例在 B 判据（调用时 STATE_FILE == 导入时值）下误报。
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import dispatcher as d  # noqa: E402

REAL_WT_ROOT = str(d.WORKTREES_ROOT)
REAL_DEV_WT_PATH = d.dev_worktree_path
REAL_STATE = str(d.STATE_FILE)

CURRENT = {"t": "<import>"}
wt_hits, ss_hits = [], []


def probe_ensure(req):
    """未隔离才记录；无论是否记录都返回假 workdir（保证探针本身零副作用）。"""
    if str(d.WORKTREES_ROOT) == REAL_WT_ROOT and d.dev_worktree_path is REAL_DEV_WT_PATH:
        wt_hits.append((CURRENT["t"], req.get("id"), req.get("project")))
    wd = Path(tempfile.mkdtemp(prefix="iso-wd-"))
    try:
        names = {Path(p).name: wd for p in d.PROJECTS[req["project"]]["paths"]}
    except Exception:
        names = {"req-board": wd}
    return wd, names


def probe_save():
    if str(d.STATE_FILE) == REAL_STATE:
        ss_hits.append(CURRENT["t"])
    return None


d.ensure_dev_worktrees = probe_ensure
d.save_state = probe_save


class _Result(unittest.TextTestResult):
    def startTest(self, test):
        CURRENT["t"] = test.id()
        super().startTest(test)


def main():
    suite = unittest.defaultTestLoader.discover(str(REPO / "tests"))
    res = unittest.TextTestRunner(verbosity=0, resultclass=_Result).run(suite)

    print("\n" + "=" * 72)
    print(f"A. 未隔离、会真建 worktree/分支的用例：{len({h[0] for h in wt_hits})} 个")
    for t in sorted({h[0] for h in wt_hits}):
        ids = sorted({str(h[1]) for h in wt_hits if h[0] == t})
        print(f"   {t}   假 id={ids}")
    print(f"\nB. 未隔离、会写真实 {REAL_STATE} 的用例：{len(set(ss_hits))} 个")
    for t in sorted(set(ss_hits)):
        print(f"   {t}")
    print(f"\n（探针轮 {res.testsRun} 例，failures={len(res.failures)} errors={len(res.errors)}"
          f"——替换实现导致，属预期）")
    bad = bool(wt_hits or ss_hits)
    print("\n结论：" + ("存在未隔离用例，见上" if bad else "全部隔离 ✓"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
