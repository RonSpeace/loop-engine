冲突仓库（需解决）：{failed}
各仓库主干引用（merge 目标，调度器主干探测：base_branch 配置 → master → main → origin/HEAD）：
{branch_lines}
解决方向：在对应仓库 worktree 内执行上述 `git merge <该仓库主干>`（把主干合并进当前 dev 分支）→ 解冲突 → 提交；调度器随后恢复主工作区合并现场并自动重试合并 dev 分支回各仓库主干，收尾无需你处理。