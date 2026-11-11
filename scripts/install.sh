#!/bin/bash
# Loop Engine 一键安装：
#   curl -fsSL https://raw.githubusercontent.com/RonSpeace/loop-engine/main/scripts/install.sh | bash
# 做的事：检查 Python 3.11+ / git / Node.js → 拉代码（或更新）→ 建 .venv 装依赖 → 装 OpenCode
# → 把 loop 命令链到 ~/.local/bin → 进入 loop init 向导。
# 可选环境变量：
#   LOOP_HOME       安装目录（默认 ~/loop-engine；在已克隆的仓库里运行本脚本则用该仓库）
#   LOOP_REPO_URL   仓库地址（默认 GitHub 公开仓库）
#   LOOP_REF        分支或标签（默认 main）
#   LOOP_NO_INIT=1  只安装，不进入向导
#   LOOP_NO_OPENCODE=1  不自动安装 OpenCode
#   LOOP_NO_UV=1    系统 Python 不可用时也不自动安装 uv
# 兼容 macOS 自带 bash 3.2；不用 sudo，不改全局 git 配置。
set -eu

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

REPO_URL="${LOOP_REPO_URL:-https://github.com/RonSpeace/loop-engine.git}"
REF="${LOOP_REF:-main}"

# 在已克隆的仓库里直接运行本脚本时，就地安装
SCRIPT_DIR=""
case "${BASH_SOURCE[0]:-}" in
    */*) SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || SCRIPT_DIR="" ;;
esac
if [ -z "${LOOP_HOME:-}" ] && [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../board.py" ]; then
    LOOP_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
LOOP_HOME="${LOOP_HOME:-$HOME/loop-engine}"

# ---- 1. 依赖 ----
say "Checking requirements"
command -v git >/dev/null 2>&1 || die "git is required: https://git-scm.com/downloads"
PYTHON=""
for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON="$(command -v "$c")"; break
    fi
done
# 系统 Python 能自己建带 pip 的 venv 就用它；否则（版本太低，或 Debian/Ubuntu 缺 python3-venv）改用 uv，
# uv 会在需要时自带 Python 3.12，不用 sudo。
USE_UV=0
if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null; then
    USE_UV=1
fi
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then UV="$(command -v uv)"; return; fi
    if [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; return; fi
    [ "${LOOP_NO_UV:-}" = "1" ] && die "Python 3.11+ with venv support is required (or allow uv: unset LOOP_NO_UV)"
    say "Installing uv (Python package manager, into ~/.local/bin)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
    else
        wget -qO- https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
    fi
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || die "uv install failed. Install Python 3.11+ with venv support and run this again."
}
if [ "$USE_UV" = "1" ]; then
    if [ -n "$PYTHON" ]; then say "Python $("$PYTHON" -V 2>&1 | awk '{print $2}') has no venv support; using uv"
    else say "Python 3.11+ not found; using uv to provide it"; fi
else
    say "Python: $("$PYTHON" --version 2>&1)"
fi
say "git: $(git --version | awk '{print $3}')"

# ---- 2. 代码 ----
if [ -d "$LOOP_HOME/.git" ]; then
    say "Using $LOOP_HOME"
    if [ -z "$(git -C "$LOOP_HOME" status --porcelain --untracked-files=no)" ]; then
        git -C "$LOOP_HOME" pull --ff-only -q || warn "could not update $LOOP_HOME (continuing with the current version)"
    else
        warn "$LOOP_HOME has local changes; skipping update"
    fi
elif [ -e "$LOOP_HOME" ] && [ -n "$(ls -A "$LOOP_HOME" 2>/dev/null)" ]; then
    die "$LOOP_HOME exists and is not a Loop Engine checkout. Set LOOP_HOME to another directory."
else
    say "Downloading Loop Engine to $LOOP_HOME"
    git clone -q --branch "$REF" "$REPO_URL" "$LOOP_HOME"
fi
cd "$LOOP_HOME"

# ---- 3. Python 环境 ----
if [ ! -x .venv/bin/python ]; then
    say "Creating the Python environment"
    if [ "$USE_UV" = "1" ]; then
        ensure_uv
        "$UV" venv -q --python "${PYTHON:-3.12}" .venv
    else
        "$PYTHON" -m venv .venv
    fi
fi
say "Installing Python packages"
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    .venv/bin/python -m pip install -q -r requirements.txt
else
    ensure_uv   # 由 uv 建的 venv 不带 pip
    "$UV" pip install -q --python .venv/bin/python -r requirements.txt
fi

# ---- 4. OpenCode（默认 agent 运行体，接任意 OpenAI 兼容模型）----
if command -v opencode >/dev/null 2>&1 || [ -x "$HOME/.local/bin/opencode" ]; then
    say "OpenCode: already installed"
elif [ "${LOOP_NO_OPENCODE:-}" = "1" ]; then
    warn "Skipping OpenCode (LOOP_NO_OPENCODE=1)"
elif command -v npm >/dev/null 2>&1; then
    say "Installing OpenCode (npm install -g opencode-ai)"
    if ! npm install -g opencode-ai >/dev/null 2>&1; then
        say "Global npm folder is not writable; installing into ~/.local instead"
        npm install -g --prefix "$HOME/.local" opencode-ai >/dev/null 2>&1 ||
            warn "OpenCode install failed. Install it later with: npm install -g opencode-ai"
    fi
else
    warn "Node.js is not installed, so OpenCode was not installed."
    warn "Install Node.js 18+ (https://nodejs.org), then run: npm install -g opencode-ai"
fi

# ---- 5. loop 命令 ----
mkdir -p "$HOME/.local/bin"
ln -sf "$LOOP_HOME/bin/loop" "$HOME/.local/bin/loop"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) say "Command installed: loop" ;;
    *) warn "Add ~/.local/bin to your PATH to use the loop command:"
       warn "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile && export PATH=\"\$HOME/.local/bin:\$PATH\""
       export PATH="$HOME/.local/bin:$PATH" ;;
esac

# ---- 6. 向导 ----
if [ "${LOOP_NO_INIT:-}" = "1" ]; then
    say "Installed. Next: loop init"
    exit 0
fi
if [ -t 0 ]; then
    exec "$LOOP_HOME/bin/loop" init
elif [ -r /dev/tty ] && (exec </dev/tty) 2>/dev/null; then
    exec "$LOOP_HOME/bin/loop" init </dev/tty   # curl | bash：从终端读输入
else
    say "Installed. Next: loop init"
fi
