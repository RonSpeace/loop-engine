#!/bin/bash
# 双机同步 Loop Engine 引擎代码（远端 origin/main）
# 用法:
#   scripts/sync.sh              # 拉取 + 合并 + 推送（日常一键）
#   scripts/sync.sh --pull-only  # 只拉取/合并，不推送
#   scripts/sync.sh --push-only  # 只推送，不拉取
#
# 设计要点（完整手工见 docs/two-machine-sync.md）：
#   - 只同步引擎代码：data/、.env、projects.json、feishu.json、.venv/、~/.req-board/ 都不入 git，
#     运行时项目与状态各机独立。
#   - 拉取用 merge 而不是 rebase：调度器会把需求分支 --no-ff 合并进本地 main，这些合并提交
#     一旦推送就是共享历史；rebase 会改写它，导致另一台机器下次同步对不上。
#   - 有未提交的已跟踪改动时拒绝执行（先 commit 或 stash），避免把运行时改动卷进同步。
#   - 同步后按改动文件打印重启建议；不自动重启、不自动 commit。
#   - 冲突时保留现场并给出处理步骤，退出码非 0。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

REMOTE="${SYNC_REMOTE:-origin}"
BRANCH="${SYNC_BRANCH:-main}"
MODE="both"

case "${1:-}" in
    -h|--help)
        sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    --pull-only) MODE="pull" ;;
    --push-only) MODE="push" ;;
    "") ;;
    *)
        echo "用法: $0 [--pull-only|--push-only]" >&2
        exit 2
        ;;
esac

say() { echo "[sync] $*"; }
die() { echo "❌ [sync] $*" >&2; exit 1; }

# --- 0. 前置检查 ---------------------------------------------------------------
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "当前目录不是 git 仓库：$REPO"
git remote get-url "$REMOTE" >/dev/null 2>&1 \
    || die "未配置远端 ${REMOTE}。先 git remote add ${REMOTE} <仓库地址>（见 docs/two-machine-sync.md）"

for marker in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
    if [ -e ".git/$marker" ]; then
        die "仓库处在合并/变基/拣选中间态（.git/${marker}），先处理完再同步"
    fi
done
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    die "仓库处在 rebase 中间态，先处理完再同步"
fi

DIRTY="$(git status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
    echo "❌ 有未提交的已跟踪改动，同步会把它卷进来。先提交或暂存："
    echo "$DIRTY" | sed 's/^/   /'
    die "已中止"
fi

UNTRACKED="$(git ls-files --others --exclude-standard)"
if [ -n "$UNTRACKED" ]; then
    say "⚠️  有未跟踪文件（通常不影响合并，除非远端新增同路径文件）："
    echo "$UNTRACKED" | sed 's/^/   /'
fi

# 同一冲突第二次出现时自动套用上次解法（两台机器反复改同一处时很有用）
if [ -z "$(git config --get rerere.enabled || true)" ]; then
    git config rerere.enabled true
    say "已开启 rerere（冲突解法自动复用，只写本机 .git/config）"
fi

if pgrep -f "python.*dispatcher\.py" >/dev/null 2>&1; then
    say "⚠️  调度器在运行，可能正在把需求分支合并进 main。"
    say "    建议确认没有在跑的 agent 再同步；若报 index.lock，等调度器空闲后重试。"
fi

OLD_HEAD="$(git rev-parse HEAD)"

# --- 1. 拉取 + 合并 ------------------------------------------------------------
if [ "$MODE" != "push" ]; then
    say "fetch $REMOTE/$BRANCH ..."
    # 显式 refspec：即使远端配置不完整也保证 origin/<branch> 跟踪引用被更新
    git fetch --prune "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
    REMOTE_HEAD="$(git rev-parse "$REMOTE/$BRANCH")"
    if [ "$OLD_HEAD" = "$REMOTE_HEAD" ]; then
        say "本地与 $REMOTE/$BRANCH 一致"
    elif git merge-base --is-ancestor "$REMOTE_HEAD" HEAD; then
        say "本地领先 ${REMOTE}/${BRANCH}，无需拉取"
    elif git merge-base --is-ancestor HEAD "$REMOTE_HEAD"; then
        say "快进到 $REMOTE/$BRANCH ..."
        git merge --ff-only "$REMOTE/$BRANCH"
    else
        say "两台都有新提交，合并 ${REMOTE}/${BRANCH}（保留双方合并提交）..."
        if ! git merge --no-edit "$REMOTE/$BRANCH"; then
            echo
            echo "❌ 合并冲突，冲突文件："
            git diff --name-only --diff-filter=U | sed 's/^/   /'
            echo
            echo "   处理步骤："
            echo "     1) 手工解决冲突（或 git checkout --theirs/--ours <文件>）"
            echo "     2) git add <文件> && git commit --no-edit"
            echo "     3) 重跑本脚本（${0}，会继续推送）"
            die "已保留冲突现场，未推送"
        fi
    fi
fi

# --- 2. 推送 -------------------------------------------------------------------
if [ "$MODE" != "pull" ]; then
    say "push $REMOTE/$BRANCH ..."
    if ! git push "$REMOTE" "$BRANCH"; then
        die "推送被拒绝（远端又有新提交）。去掉 --push-only 重跑本脚本（${0}）：先合并再推送"
    fi
fi

# --- 3. 同步结果 + 重启建议 -----------------------------------------------------
NEW_HEAD="$(git rev-parse HEAD)"
if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
    say "✅ 同步完成，本地没有新增提交"
    exit 0
fi

CHANGED="$(git diff --name-only "$OLD_HEAD".."$NEW_HEAD")"
echo
say "✅ 本次同步的改动："
echo "$CHANGED" | sed 's/^/   /'
echo
say "按改动决定是否重启（脚本不会自动执行）："

RESTART=0
if echo "$CHANGED" | grep -qE '^plists/'; then
    echo "   - plists/ 变了 → scripts/install-launchd.sh（会重启看板+调度器，挑调度器空闲时做）"
    RESTART=1
fi
if echo "$CHANGED" | grep -qE '^(dispatcher\.py|acp_shared\.py)$'; then
    echo "   - 调度器代码变了 → scripts/dispatcher-restart.sh <pid> sync"
    RESTART=1
fi
if echo "$CHANGED" | grep -qE '^(board\.py|board_modules/|templates/|static/)'; then
    echo "   - 看板代码变了 → ./start.sh（并 curl http://127.0.0.1:8899 验证 200）"
    RESTART=1
fi
if echo "$CHANGED" | grep -qE '^(flows\.json|prompts/)'; then
    echo "   - 流程/提示词变了 → ./start.sh + scripts/dispatcher-restart.sh <pid> config-change"
    RESTART=1
fi
if echo "$CHANGED" | grep -qE '^scripts/launchd-run\.sh$'; then
    echo "   - launchd 入口脚本变了 → scripts/install-launchd.sh（或重启两个服务）"
    RESTART=1
fi
if [ "$RESTART" = "0" ]; then
    echo "   - 只动了文档/测试/普通脚本，通常无需重启"
fi
