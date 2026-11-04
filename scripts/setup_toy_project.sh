#!/bin/bash
# 一键创建玩具项目（docs/quickstart.md 第 4.1/4.2 节配套脚本）
# 用法（在你的 Mac 终端）：
#     bash scripts/setup_toy_project.sh
# 可用 TOY_DIR=~/somewhere 覆盖默认位置 ~/projects/toy-project。
# 幂等：目标已是 git 仓库则跳过创建。
# 兼容 macOS 自带 bash 3.2。
set -eu

TOY_DIR="${TOY_DIR:-$HOME/projects/toy-project}"

if [ -d "$TOY_DIR/.git" ]; then
    echo "[toy] 已是 git 仓库，跳过：$TOY_DIR"
    exit 0
fi
if [ -e "$TOY_DIR" ] && [ ! -d "$TOY_DIR" ]; then
    echo "[toy] 路径存在且不是目录，请处理后再试：$TOY_DIR" >&2
    exit 1
fi

mkdir -p "$TOY_DIR"
cd "$TOY_DIR"

# git init（-b 需 git>=2.28，老版本走 fallback）
git init -b main >/dev/null 2>&1 || { git init >/dev/null && git branch -m main; }

cat > calc.py <<'PYEOF'
"""toy-project：极简计算器（闭环冒烟基线，mul 留给 dev 角色实现）。"""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b
PYEOF

cat > test_calc.py <<'PYEOF'
import unittest

from calc import add, sub


class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)
        self.assertEqual(add(0, 0), 0)

    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
        self.assertEqual(sub(-2, -3), 1)


if __name__ == "__main__":
    unittest.main()
PYEOF

cat > AGENTS.md <<'MDEOF'
# toy-project

玩具项目：闭环冒烟用。测试命令：`python3 -m unittest discover -s . -p 'test_*.py' -v`（或 `python3 test_calc.py`）。

## 需求看板协作约定

本项目的需求看板运行在 http://127.0.0.1:8899（若未运行可跳过，不要报错）。

1. 暂缓需求建卡：POST /api/requirements，project 填 toy-project，status 必须传 "later"。
2. 开始开发标进行中：PATCH 状态为 doing。
3. 完成标已完成：按标题语义匹配后 PATCH 为 done；匹配不确定必须先问用户。
4. 改状态带请求头 X-Actor: agent:toy-project:<需求号>。
5. notes 只写要做什么，不写讨论过程。
MDEOF

# 基线自测（系统本身要求 python3，此处必可用）
python3 -m unittest discover -s . -p 'test_*.py' >/dev/null
echo "[toy] 基线自测通过（add/sub）"

# 首提交：未配置全局 git 身份时用一次性身份兜底，不改全局配置
git add -A
if [ -z "$(git config user.email)" ]; then
    git -c user.name="toy" -c user.email="toy@local" \
        commit -q -m "init: calc add/sub + tests + AGENTS.md"
else
    git commit -q -m "init: calc add/sub + tests + AGENTS.md"
fi

echo "[toy] 完成：${TOY_DIR}（分支 main，首提交 $(git rev-parse --short HEAD)）"
echo "[toy] 下一步（一次性目录信任，接受后退出即可）："
echo "      cd \"$TOY_DIR\" && KIMI_CODE_HOME=~/.kimi-code-deepseek kimi"
