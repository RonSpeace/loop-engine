Conflicting repos (to resolve): {failed}
Main branch ref of each repo (merge target; dispatcher main-branch detection: base_branch config → master → main → origin/HEAD):
{branch_lines}
How to resolve: in each repo's worktree run the `git merge <that repo's main branch>` above (merge the main branch into the current dev branch) → resolve conflicts → commit; the dispatcher then restores the main working tree's merge state and automatically retries merging the dev branch back into each repo's main branch; you do not need to handle finalize.