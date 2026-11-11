#!/usr/bin/env python3
"""loop init：从零到第一张合并的卡的安装向导（只用标准库）。

步骤：检查运行体（OpenCode）→ 选模型服务商预设 → 填 API Key 并当场验证 → 选模型 → 写 .env
→ 接入项目（玩具项目或现有 git 仓库，合并进 projects.json）→ 跑 doctor →（可选）启动并建第一张卡。

交互式直接运行；无人值守（测试 / CI / 脚本）用参数，例如：
    scripts/loop_init.py --yes --provider glm --api-key-env MY_KEY --project toy --toy-dir /tmp/toy --no-start
API Key 只写进仓库根 .env（权限 600，已 gitignore），不打印、不写进任何其它文件。
"""
import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO   # .env / projects.json 所在目录（测试改写到临时目录）

# 服务商预设：base_url 为 OpenAI 兼容接口；models 为建议值（会和服务商 /models 列表核对，
# 不在列表里就让用户从列表挑）。cheap = 分析/评审/验收等多数角色，dev = 开发角色。
PRESETS = {
    "deepseek":   {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                   "cheap": "deepseek-chat", "dev": "deepseek-chat",
                   "key_url": "https://platform.deepseek.com/api_keys"},
    "glm":        {"label": "Zhipu GLM (open.bigmodel.cn)", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                   "cheap": "glm-5.3-flash", "dev": "glm-5.3",
                   "key_url": "https://open.bigmodel.cn/usercenter/apikeys"},
    "glm-coding": {"label": "Zhipu GLM Coding Plan", "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                   "cheap": "glm-5.3-flash", "dev": "glm-5.3",
                   "key_url": "https://open.bigmodel.cn/usercenter/apikeys"},
    "qwen":       {"label": "Qwen (Alibaba Cloud Model Studio, international)",
                   "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                   "cheap": "", "dev": "", "key_url": "https://modelstudio.console.alibabacloud.com/"},
    "moonshot":   {"label": "Moonshot Kimi", "base_url": "https://api.moonshot.ai/v1",
                   "cheap": "", "dev": "", "key_url": "https://platform.moonshot.ai/console/api-keys"},
    "openrouter": {"label": "OpenRouter (hundreds of models)", "base_url": "https://openrouter.ai/api/v1",
                   "cheap": "", "dev": "", "key_url": "https://openrouter.ai/keys"},
    "openai":     {"label": "OpenAI", "base_url": "https://api.openai.com/v1",
                   "cheap": "", "dev": "", "key_url": "https://platform.openai.com/api-keys"},
    "custom":     {"label": "Other OpenAI-compatible endpoint", "base_url": "", "cheap": "", "dev": "",
                   "key_url": ""},
}

# loop init 管理的 .env 键：重跑向导只替换这些行，用户的其它行原样保留
MANAGED_KEYS = ("LOOP_AGENT_RUNTIME", "LOOP_OPENAI_BASE_URL", "LOOP_OPENAI_API_KEY",
                "LOOP_OPENCODE_MODEL", "LOOP_OPENCODE_MODEL_DEV")

TOY_CARD = {
    "title": "Add a mul(a, b) function to calc.py",
    "notes": "Add mul(a, b) to calc.py that returns a * b.\n"
             "Add unit tests for it in test_calc.py (positive, negative and zero).\n"
             "Do not change add or sub.",
}


class InitError(Exception):
    pass


# ---------------- 输出与交互 ----------------

def say(msg=""):
    print(msg, flush=True)


def step(n, total, title):
    say(f"\n[{n}/{total}] {title}")


def ask(prompt, default=None, *, yes=False):
    """问一句；--yes 或非交互时直接取默认值（没有默认值则报错，不卡住）。"""
    if yes or not sys.stdin.isatty():
        if default is None:
            raise InitError(f"missing value for: {prompt} (pass it as an option)")
        return default
    suffix = f" [{default}]" if default not in (None, "") else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or (default or "")


def confirm(prompt, default=True, *, yes=False):
    if yes or not sys.stdin.isatty():
        return default
    val = input(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not val else val in ("y", "yes")


def choose(prompt, options, default_idx=0, *, yes=False):
    """options: [(value, label)]；返回 value。"""
    for i, (_, label) in enumerate(options, 1):
        say(f"    {i}. {label}")
    raw = ask(prompt, str(default_idx + 1), yes=yes)
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1][0]
    for value, _ in options:
        if raw == value:
            return value
    raise InitError(f"invalid choice: {raw}")


# ---------------- 运行体 ----------------

def find_opencode():
    cmd = shlex.split(os.environ.get("LOOP_AGENT_CMD_OPENCODE", ""))
    exe = cmd[0] if cmd else "opencode"
    path = shutil.which(exe)
    if not path:
        return None, None
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
        return path, (r.stdout.strip() or r.stderr.strip())[:40]
    except Exception:  # noqa: BLE001 - 版本探测失败不阻断
        return path, "?"


# ---------------- 服务商与模型 ----------------

def _request(url, key, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", "loop-engine-init")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def list_models(base_url, key):
    """GET /models：成功返回模型 id 列表；鉴权失败抛 InitError；接口不支持返回 None。"""
    try:
        _, payload = _request(base_url.rstrip("/") + "/models", key)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise InitError(f"the API key was rejected ({e.code}). Check the key and the endpoint.")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise InitError(f"cannot reach {base_url} ({getattr(e, 'reason', e)})")
    except ValueError:
        return None
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    return sorted({m.get("id") for m in items if isinstance(m, dict) and m.get("id")})


def ping_model(base_url, key, model):
    """最小一次补全，确认 Key + 模型真能用。返回 None 表示通过，否则返回错误描述。"""
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with: ok"}], "max_tokens": 8}
    try:
        _request(base_url.rstrip("/") + "/chat/completions", key, body, timeout=60)
        return None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return f"HTTP {e.code} {detail}".strip()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return str(getattr(e, "reason", e))


def pick_model(role_label, suggested, available, *, yes, fixed=None):
    """建议值在服务商列表里（或服务商不提供列表）→ 回车即用建议值；否则交互时从列表挑。
    无人值守时绝不从列表里瞎选：有建议值就用（随后试调用会验证），没有就报错要求显式传参。"""
    if fixed:
        return fixed
    if not available or suggested in available:
        return ask(f"Model for {role_label}", suggested or None, yes=yes)
    if yes or not sys.stdin.isatty():
        if suggested:
            return suggested
        raise InitError(f"choose a model for {role_label}: pass --model / --dev-model")
    shown = available[:40]
    say(f"  Models available on this endpoint ({len(available)}"
        f"{', first 40 shown — you can also type any name' if len(available) > 40 else ''}):")
    for i, m in enumerate(shown, 1):
        say(f"    {i}. {m}")
    raw = input(f"  Model for {role_label} (number or name): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(shown):
        return shown[int(raw) - 1]
    if raw:
        return raw
    raise InitError("no model chosen")


# ---------------- 文件写入 ----------------

def write_env(path, values):
    """替换 MANAGED_KEYS 对应的行，其它行原样保留；新键追加到末尾；权限 600。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen, out = set(), []
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=", line)
        key = m.group(1) if m else None
        if key in values:
            if key not in seen:
                out.append(f"{key}={values[key]}")
                seen.add(key)
            continue
        out.append(line)
    new = [k for k in values if k not in seen]
    if new:
        if out and out[-1].strip():
            out.append("")
        out.append("# ---- written by loop init ----")
        out += [f"{k}={values[k]}" for k in new]
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(path, 0o600)


def read_env(path):
    vals = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if m:
                vals[m.group(1)] = m.group(2).strip().strip("'\"")
    return vals


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)


def ensure_toy_project(toy_dir):
    env = dict(os.environ, TOY_DIR=str(toy_dir))
    r = subprocess.run(["bash", str(REPO / "scripts" / "setup_toy_project.sh")], env=env,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise InitError("could not create the toy project:\n" + (r.stderr or r.stdout)[-500:])
    ensure_git_identity(toy_dir)


def ensure_git_identity(repo):
    """自动合并要建合并提交：仓库没有提交身份时只在该仓库内设一个本地身份（不动全局配置）。"""
    if git(repo, "var", "GIT_COMMITTER_IDENT").returncode != 0:
        git(repo, "config", "user.name", "Loop Engine")
        git(repo, "config", "user.email", "loop-engine@localhost")
        say(f"  Set a repo-local git identity in {repo} (needed for automatic merges).")


def inspect_repo(path):
    p = Path(path).expanduser().resolve()
    if not (p / ".git").exists():
        raise InitError(f"{p} is not a git repository")
    if git(p, "rev-parse", "--verify", "HEAD").returncode != 0:
        raise InitError(f"{p} has no commits yet; make a first commit and run loop init again")
    branch = git(p, "symbolic-ref", "--short", "HEAD").stdout.strip() or "main"
    return p, branch


def write_project(projects_file, name, path, branch, goal):
    data = {}
    if projects_file.exists():
        try:
            data = json.loads(projects_file.read_text(encoding="utf-8") or "{}")
        except ValueError:
            raise InitError(f"{projects_file} is not valid JSON; fix or move it and run loop init again")
    entry = data.get(name) if isinstance(data.get(name), dict) else {}
    entry.update({"paths": [str(path)], "base_branch": branch})
    if goal:
        entry["goal"] = goal
    data[name] = entry
    projects_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------- 启动与第一张卡 ----------------

def board_url():
    env = read_env(CONFIG_DIR / ".env")
    host = os.environ.get("REQ_BOARD_HOST") or env.get("REQ_BOARD_HOST") or "127.0.0.1"
    port = os.environ.get("REQ_BOARD_PORT") or env.get("REQ_BOARD_PORT") or "8899"
    return f"http://{host}:{port}"


def wait_for_board(url, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "/api/dispatcher-health", timeout=5) as r:
                if json.loads(r.read().decode()).get("ok"):
                    return True
        except Exception:  # noqa: BLE001 - 启动中，继续等
            pass
        time.sleep(2)
    return False


def create_card(url, project, card, status):
    body = json.dumps({"project": project, "title": card["title"], "notes": card["notes"],
                       "status": status, "source": "loop-init"}).encode()
    req = urllib.request.Request(url + "/api/requirements", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Actor": "agent:loop-init"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def run_doctor():
    """跑 scripts/doctor.py；有硬失败（❌）就把那几行列出来并停下。"""
    py = REPO / ".venv" / "bin" / "python"
    r = subprocess.run([str(py if py.exists() else sys.executable), str(REPO / "scripts" / "doctor.py")],
                       capture_output=True, text=True, timeout=180, cwd=REPO)
    if r.returncode != 0:
        fails = [ln for ln in r.stdout.splitlines() if ln.lstrip().startswith("❌")]
        say("\n".join("  " + ln.strip() for ln in fails) or r.stdout[-800:])
        raise InitError("the self-check found problems (above). Fix them and run: loop doctor")


# ---------------- 主流程 ----------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="loop init", description="Set up Loop Engine: model, project, first card.")
    ap.add_argument("--yes", action="store_true", help="accept defaults, never prompt")
    ap.add_argument("--provider", choices=sorted(PRESETS))
    ap.add_argument("--base-url", help="OpenAI-compatible base URL (for --provider custom, or to override)")
    ap.add_argument("--api-key-env", help="read the API key from this environment variable")
    ap.add_argument("--model", help="model for most roles (analysis, review, acceptance)")
    ap.add_argument("--dev-model", help="model for the dev role (default: same as --model)")
    ap.add_argument("--skip-verify", action="store_true", help="do not call the endpoint to check the key")
    ap.add_argument("--project", help='"toy" for the demo project, or a path to an existing git repository')
    ap.add_argument("--project-name", help="name of the project on the board (default: directory name)")
    ap.add_argument("--toy-dir", default=str(Path.home() / "loop-projects" / "toy-project"))
    ap.add_argument("--start", dest="start", action="store_true", default=None, help="start Loop Engine at the end")
    ap.add_argument("--no-start", dest="start", action="store_false")
    ap.add_argument("--first-card", dest="first_card", action="store_true", default=None,
                    help="queue the demo card on the toy project after start")
    ap.add_argument("--no-first-card", dest="first_card", action="store_false")
    a = ap.parse_args(argv)
    yes, total = a.yes, 5

    try:
        say("Loop Engine setup — cheap models, strict gates, steady output.")

        # 1. 运行体
        step(1, total, "Agent runtime")
        oc_path, oc_ver = find_opencode()
        if oc_path:
            say(f"  OpenCode found: {oc_path} ({oc_ver})")
        else:
            say("  OpenCode is not installed. Install it with:  npm install -g opencode-ai")
            say("  (Node.js 18+ required — https://nodejs.org). You can finish setup now and install it later.")

        # 2. 服务商、Key、模型
        step(2, total, "Model provider")
        env_path = CONFIG_DIR / ".env"
        old = read_env(env_path)
        provider = a.provider or choose("Provider", [(k, v["label"]) for k, v in PRESETS.items()], 0, yes=yes)
        preset = PRESETS[provider]
        base_url = a.base_url or preset["base_url"] or ask("OpenAI-compatible base URL (…/v1)", None, yes=yes)
        if not re.match(r"^https?://", base_url or ""):
            raise InitError(f"invalid base URL: {base_url!r}")
        key = os.environ.get(a.api_key_env, "") if a.api_key_env else ""
        if a.api_key_env and not key:
            raise InitError(f"environment variable {a.api_key_env} is empty")
        if not key:
            reuse = old.get("LOOP_OPENAI_API_KEY") if old.get("LOOP_OPENAI_BASE_URL") == base_url else ""
            if reuse and confirm("Keep the API key already saved in .env?", True, yes=yes):
                key = reuse
            elif yes or not sys.stdin.isatty():
                raise InitError("no API key: pass --api-key-env NAME")
            else:
                if preset["key_url"]:
                    say(f"  Get a key at {preset['key_url']}")
                key = getpass.getpass("  API key (input hidden): ").strip()
        if not key:
            raise InitError("an API key is required")

        available = None
        if not a.skip_verify:
            say("  Checking the key…")
            available = list_models(base_url, key)
            say(f"  Key accepted. {len(available)} models available." if available is not None
                else "  This endpoint does not list models; the model will be checked with a test call.")
        model = pick_model("analysis, review and acceptance", preset["cheap"], available, yes=yes, fixed=a.model)
        dev_model = pick_model("development", preset["dev"] or model, available, yes=yes, fixed=a.dev_model)
        if not a.skip_verify:
            for m in dict.fromkeys([model, dev_model]):
                err = ping_model(base_url, key, m)
                if err:
                    raise InitError(f"test call to model {m!r} failed: {err}")
                say(f"  Model {m}: ok")
        values = {"LOOP_AGENT_RUNTIME": "opencode", "LOOP_OPENAI_BASE_URL": base_url,
                  "LOOP_OPENAI_API_KEY": key, "LOOP_OPENCODE_MODEL": model, "LOOP_OPENCODE_MODEL_DEV": dev_model}
        # 本次用环境变量指定了端口 → 记进 .env，之后的 loop start/stop/status 和调度器才会用同一个端口
        if os.environ.get("REQ_BOARD_PORT", "").strip():
            values["REQ_BOARD_PORT"] = os.environ["REQ_BOARD_PORT"].strip()
        write_env(env_path, values)
        say(f"  Saved to {env_path} (readable only by you; never committed).")

        # 3. 项目
        step(3, total, "Project")
        target = a.project
        if not target:
            target = choose("Which project should the agents work on?",
                            [("toy", "A small demo project (recommended for the first run)"),
                             ("path", "An existing git repository on this machine")], 0, yes=yes)
            if target == "path":
                target = ask("Path to the repository", None, yes=yes)
        is_toy = target == "toy"
        if is_toy:
            toy_dir = Path(a.toy_dir).expanduser()
            ensure_toy_project(toy_dir)
            path, branch = inspect_repo(toy_dir)
            name, goal = a.project_name or "toy-project", "A tiny calculator used to try the full loop."
        else:
            path, branch = inspect_repo(target)
            ensure_git_identity(path)
            name = a.project_name or re.sub(r"[^A-Za-z0-9._-]+", "-", path.name)
            goal = "" if yes else ask("One-line goal of this project (optional)", "", yes=yes)
        write_project(CONFIG_DIR / "projects.json", name, path, branch, goal)
        say(f"  Project {name!r}: {path} (branch {branch})")

        # 4. 自检
        step(4, total, "Self-check")
        run_doctor()
        say("  All required checks passed.")

        # 5. 启动与第一张卡
        step(5, total, "Start")
        start = a.start if a.start is not None else confirm("Start Loop Engine now?", True, yes=yes)
        url = board_url()
        if not start:
            say("  Start it any time with:  loop start")
        else:
            r = subprocess.run([str(REPO / "bin" / "loop"), "start"], cwd=REPO, timeout=60)
            if r.returncode != 0:
                raise InitError("loop start failed; see: loop logs")
            say("  Waiting for the board and the dispatcher…")
            if not wait_for_board(url):
                raise InitError("the dispatcher did not report in time; see: loop logs")
            want_card = a.first_card if a.first_card is not None else (
                is_toy and confirm("Queue a first card for the agents now?", True, yes=yes))
            if want_card and is_toy:
                card = create_card(url, name, TOY_CARD, "open")
                say(f"  Card #{card.get('id')} queued: {TOY_CARD['title']}")
                say("  Watch it go analysis → dev → review → acceptance → merge on the board.")
        say(f"\nDone. Open {url}")
        say("Commands: loop start | loop stop | loop status | loop logs | loop doctor")
        return 0
    except InitError as e:
        say(f"\nSetup stopped: {e}")
        return 1
    except (KeyboardInterrupt, EOFError):
        say("\nSetup cancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
