#!/usr/bin/env python3
"""req-board 本机环境自检（quickstart 配套，stdlib-only，零副作用只读检查）。

用法（推荐入口 = 项目 .venv；依赖检查按启动本脚本的解释器环境进行）：
    .venv/bin/python scripts/doctor.py   # 推荐：主工作区 .venv（通过口径 = 退出码 0 且输出「硬失败 0 项」）
    python3 scripts/doctor.py            # 非推荐：裸解释器，依赖可能不在该环境（告警，不计硬失败）

输出：逐项 ✅/⚠️/❌；存在 ❌ 时退出码 1。检查项与 docs/quickstart.md 一一对应。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARD_FAIL = []


def ok(msg):
    print(f"✅ {msg}")


def warn(msg):
    print(f"⚠️  {msg}")


def fail(msg):
    print(f"❌ {msg}")
    HARD_FAIL.append(msg)


def check_python():
    v = sys.version_info
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro}（{sys.executable}）")
    else:
        fail(f"Python 版本过低：{v.major}.{v.minor}（建议 3.11+，最低 3.10）")


def check_venv():
    """#40 推荐入口自检：不在虚拟环境（sys.prefix == sys.base_prefix）时按「非推荐入口」告警。
    判据是「没在虚拟环境里」，不是「解释器路径不等于本仓库 .venv」——worktree 没有 .venv，
    用主工作区 .venv/bin/python 跑同样通过。告警不计硬失败（与 docs/quickstart.md 口径一致）。"""
    if sys.prefix == sys.base_prefix:
        warn(f"非推荐入口：当前解释器不在虚拟环境（{sys.executable}），依赖检查按该解释器环境进行"
             "——推荐用 .venv/bin/python scripts/doctor.py")
    else:
        ok(f"虚拟环境解释器：{sys.prefix}")


def check_repo_files():
    for name in ("board.py", "dispatcher.py", "acp_shared.py", "flows.json", "prompts", "scripts/kimi-chat-acp"):
        p = REPO / name
        if p.exists():
            ok(f"仓库文件存在：{name}")
        else:
            fail(f"仓库文件缺失：{name}")


def _import(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def check_deps():
    # 注意：doctor 自身用哪个解释器跑，检查的就是那个环境的依赖
    py = sys.executable
    if _import("flask"):
        ok(f"flask 可用（{py}）")
    else:
        fail(f"flask 未安装：{py} -m pip install flask")
    if _import("lark_oapi"):
        ok("lark_oapi 可用（飞书通道）")
    else:
        warn(f"lark_oapi 未安装（可选：仅启用飞书通道才需要，未装时调度器自动停用飞书）："
             f"{py} -m pip install lark-oapi")
    if _import("certifi"):
        ok("certifi 可用")
    else:
        warn(f"certifi 未安装（可选：飞书长连接 SSL 证书链）：{py} -m pip install certifi")
    if _import("mysql.connector"):
        ok("mysql-connector 已装（可选）")
    else:
        warn("mysql-connector 未装（可选：仅项目配了 mysql 只读查询才需要）")


def check_git():
    if shutil.which("git"):
        ok("git 可用")
    else:
        fail("git 未安装")


def kimi_bin():
    return os.environ.get("KIMI_BIN") or shutil.which("kimi") or str(Path.home() / ".kimi-code" / "bin" / "kimi")


def check_kimi():
    binp = kimi_bin()
    if Path(binp).exists() or shutil.which(binp):
        try:
            r = subprocess.run([binp, "--version"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                ok(f"kimi CLI 可用：{binp}（{r.stdout.strip()[:60] or r.stderr.strip()[:60]}）")
            else:
                warn(f"kimi CLI 存在但 --version 退出码 {r.returncode}：{binp}")
        except Exception as e:
            warn(f"kimi CLI 探测失败（{e}）：{binp}")
    else:
        fail(f"kimi CLI 未找到（{binp}）：安装 kimi-code，或设 KIMI_BIN 指向二进制")


def harness_home():
    return Path(os.environ.get("KIMI_CODE_HOME") or str(Path.home() / ".kimi-code-deepseek"))


def check_model_config():
    home = harness_home()
    if not home.is_dir():
        fail(f"harness home 不存在：{home}（后台 agent 从这里读模型配置；见 quickstart 第 2 节）")
        return
    ok(f"harness home 存在：{home}")
    cfg = home / "config.toml"
    if cfg.is_file():
        text = cfg.read_text(errors="replace")
        ok("config.toml 存在")
        if "api_key" in text or "api-key" in text.lower():
            ok("config.toml 含 api_key 配置（值是否正确需实际跑一次会话验证）")
        else:
            warn("config.toml 未见 api_key 字样——确认 provider 凭据已配置")
    else:
        fail(f"缺少 {cfg}（provider/API key/模型名都配在这里）")
    model = os.environ.get("KIMI_MODEL", "deepseek/v4.1-flash（默认）")
    warn(f"模型名当前为：{model}——须与 config.toml 的 default_model 全名一致")


def check_mcp():
    home = harness_home()
    mcp = home / "mcp.json"
    if not mcp.is_file():
        # 交互主配置软链共享是常见形态，顺带看一眼
        main_mcp = Path.home() / ".kimi-code" / "mcp.json"
        if main_mcp.is_file() and not mcp.exists():
            warn(f"{mcp} 不存在（~/.kimi-code/mcp.json 存在——可软链共享：ln -s ~/.kimi-code/mcp.json {mcp}）")
        else:
            warn(f"{mcp} 不存在（只读工具不受影响；dev/accept 浏览器验证需要 playwright）")
        return
    try:
        servers = json.loads(mcp.read_text()).get("mcpServers", {})
    except Exception as e:
        fail(f"mcp.json 解析失败：{e}")
        return
    ok(f"mcp.json 可解析（servers：{', '.join(sorted(servers)) or '无'}）")
    if "playwright" in servers:
        ok("playwright MCP 已配置（浏览器级验证可用）")
    else:
        warn("mcp.json 未配名为 playwright 的 server（dev/accept 浏览器级验证会降级为人工验收）")


def check_projects():
    pf = REPO / "projects.json"
    if not pf.is_file():
        fail(f"缺少 {pf}（模板 projects.example.json，见 quickstart 第 4 节）")
        return
    try:
        raw = json.loads(pf.read_text())
    except Exception as e:
        fail(f"projects.json 解析失败：{e}")
        return
    if not raw:
        warn("projects.json 为空（没有接入任何项目）")
        return
    for name, cfg in raw.items():
        if isinstance(cfg, str):
            paths = [cfg]
        elif isinstance(cfg, dict):
            paths = cfg.get("paths") or ([cfg["path"]] if "path" in cfg else [])
        else:
            paths = []
        if not paths:
            warn(f"项目 {name}：未配 paths")
            continue
        for p in paths:
            pp = Path(str(p)).expanduser()
            if not pp.is_dir():
                fail(f"项目 {name} 路径不存在：{pp}")
            elif not (pp / ".git").exists():
                fail(f"项目 {name} 不是 git 仓库（无 .git）：{pp}")
            else:
                ok(f"项目 {name}：{pp}（git 仓库）")
                # 自动合并要创建合并提交：没有 git 提交身份时 done 收尾会合并失败
                try:
                    ident = subprocess.run(["git", "var", "GIT_COMMITTER_IDENT"], cwd=pp, capture_output=True, timeout=10)
                    if ident.returncode != 0:
                        warn(f"项目 {name}：{pp} 没有 git 提交身份（user.name / user.email），自动合并会失败；"
                             f"在该仓库执行 git config user.name \"…\" 与 git config user.email \"…\"（或配置全局身份）")
                except Exception as e:  # noqa: BLE001 - 体检不因探测失败中断
                    warn(f"项目 {name}：检查 git 提交身份失败：{e}")


def check_env_feishu():
    if os.environ.get("REQ_BOARD_NO_FEISHU") == "1":
        ok("REQ_BOARD_NO_FEISHU=1（跳过飞书，确认走网页按钮）")
        return
    ff = Path(os.environ.get("REQ_BOARD_FEISHU_JSON") or (REPO / "feishu.json"))
    creds = {}
    if ff.is_file():
        try:
            creds = json.loads(ff.read_text())
        except Exception as e:
            fail(f"{ff.name} 解析失败：{e}（设置抽屉 → 飞书机器人 重新保存）")
            return
    if creds.get("app_id") and creds.get("app_secret"):
        ok("feishu.json 已配飞书凭据")
    elif not ff.is_file():
        warn("无 feishu.json（设置抽屉 → 飞书机器人）")
    else:
        warn("feishu.json 凭据不全（设置抽屉 → 飞书机器人）")


def check_dirs_and_port():
    data = REPO / "data"
    try:
        data.mkdir(exist_ok=True)
        ok(f"data 目录可写：{data}")
    except Exception as e:
        fail(f"data 目录不可写：{e}")
    rb = Path.home() / ".req-board"
    try:
        rb.mkdir(parents=True, exist_ok=True)
        ok(f"产物/worktree 根目录可写：{rb}")
    except Exception as e:
        fail(f"~/.req-board 不可写：{e}")
    port = int(os.environ.get("REQ_BOARD_PORT", "8899"))
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        warn(f"端口 {port} 已被占用（看板可能已在运行；若非预期请先停掉）")
    except OSError:
        ok(f"端口 {port} 空闲")
    finally:
        s.close()


def check_project_conventions():
    """A3/A4：各项目仓库的 .loop/project.json 清单校验 + AGENTS.md 长度检查（仓库根与各子目录 AGENTS.md，
    子目录文件另查与根逐字重复；均为告警，不阻断启动）。"""
    sys.path.insert(0, str(REPO))
    try:
        import acp_shared
        raw = json.loads((REPO / "projects.json").read_text())
    except Exception as e:
        warn(f"项目约定检查跳过：{e}")
        return
    for name, cfg in (raw or {}).items():
        if isinstance(cfg, str):
            paths = [cfg]
        elif isinstance(cfg, dict):
            paths = cfg.get("paths") or ([cfg["path"]] if "path" in cfg else [])
        else:
            paths = []
        for p in paths:
            if not Path(p).is_dir():
                continue
            m = acp_shared.load_project_manifest(p)
            if not m["path"]:
                warn(f"项目 {name}：{p} 未提供 .loop/project.json（提示词「项目约定」层为空，"
                     "模板 docs/templates/project.example.json）")
            elif m["errors"]:
                for e in m["errors"]:
                    warn(f"项目 {name}：{m['path']} {e}")
            else:
                ok(f"项目 {name}：清单 {m['path']} 校验通过"
                   f"（命令 {len(m['commands'])} 项，指引 {len(m['guides'])} 个）")
            problems = acp_shared.agents_md_problems(p, m)
            for msg in problems:
                warn(f"项目 {name}：{msg}")
            if not problems:
                ok(f"项目 {name}：{Path(p) / 'AGENTS.md'} 长度在上限内")
            nested = acp_shared.agents_md_nested_problems(p, m)
            for msg in nested:
                warn(f"项目 {name}：{msg}")


def check_flow_config():
    sys.path.insert(0, str(REPO))
    try:
        import acp_shared  # noqa: F401  stdlib-only，import 无副作用
    except Exception as e:
        fail(f"acp_shared 导入失败：{e}")
        return
    try:
        acp_shared.load_flow_config()
        ok("flows.json 校验通过（流程/角色/提示词文件齐全）")
    except Exception as e:
        fail(f"flows.json 校验失败：{e}")


def check_skills():
    """技能 v2：全局 / 本机覆盖 / 各项目技能逐个 lint（只告警，不计硬失败）；
    flows.json 与项目清单点名却找不到的技能一并提示。"""
    sys.path.insert(0, str(REPO))
    try:
        import acp_shared
    except Exception as e:
        warn(f"技能校验跳过：{e}")
        return
    try:
        raw = json.loads((REPO / "projects.json").read_text())
    except Exception:
        raw = {}
    groups = [("全局", [])]
    for name, cfg in (raw or {}).items():
        paths = [cfg] if isinstance(cfg, str) else (
            (cfg.get("paths") or ([cfg["path"]] if "path" in cfg else [])) if isinstance(cfg, dict) else [])
        groups.append((f"项目 {name}", [str(p) for p in paths if Path(str(p)).is_dir()]))
    roles = set(((acp_shared.FLOW_CONFIG or {}).get("roles") or {}))
    seen, n_ok = set(), 0
    for label, repos in groups:
        for sk in acp_shared.discover(repos):
            if (sk.scope != "project" and label != "全局") or sk.path in seen:
                continue
            seen.add(sk.path)
            items = acp_shared.skill_lint(sk, known_roles=roles or None)
            bad = [i for i in items if i["level"] in ("error", "warn")]
            for i in bad:
                warn(f"技能 {sk.name}（{label} · {sk.rel_path}）{'✕' if i['level'] == 'error' else '!'} {i['message']}")
            if not bad:
                n_ok += 1
        for role in sorted(roles):
            names = list((acp_shared.role_meta(role, "skills") or [])) if label == "全局" else []
            for repo in repos:
                names += acp_shared.load_project_manifest(repo)["skills"].get(role) or []
            for n in dict.fromkeys(names):
                if acp_shared.find_skill(n, repos) is None:
                    warn(f"技能 {n}（{label} 点名给 {role}）找不到文件，运行时会跳过")
    ok(f"技能校验：{n_ok} 个技能无告警（本机覆盖目录 {acp_shared.local_skills_dir()}）")


def main():
    print(f"== req-board doctor：{REPO} ==\n")
    print("-- 基础 --");         check_python(); check_venv(); check_repo_files(); check_deps(); check_git()
    print("\n-- agent 运行体 --"); check_kimi()
    print("\n-- 模型配置 --");    check_model_config(); check_mcp()
    print("\n-- 项目接入 --");    check_projects(); check_project_conventions()
    print("\n-- 渠道与本地 --");  check_env_feishu(); check_dirs_and_port()
    print("\n-- 流程配置 --");    check_flow_config()
    print("\n-- 技能库 --");      check_skills()
    print()
    if HARD_FAIL:
        print(f"共 {len(HARD_FAIL)} 项硬失败，处理后再启动（docs/quickstart.md 有对应步骤）")
        return 1
    print("硬失败 0 项（⚠️ 为可选项，按需处理）。可以 ./scripts/run_local.sh 启动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
