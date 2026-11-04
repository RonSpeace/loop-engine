#!/usr/bin/env python3
"""测试实例确定性种子数据（视觉闸门 / 浏览器级自验用）。

用法:
    python scripts/seed_test_db.py --data-dir /tmp/req_board_test_8931_data [--force]
    python scripts/seed_test_db.py --print-freeze-time     # 打印默认冻结时刻（test_instance.sh 取用）

往 `<data-dir>/board.db` 写一份**固定**数据集（先调 board.init_db() 建表/迁移，再插固定行），
并在同目录写配套 `projects.json`（3 个演示项目）与 `dispatcher_state.json`（会话信息栏 + 一条
待确认）；**不写心跳文件**（种子实例由 `scripts/test_instance.sh --seed` 带 `REQ_BOARD_NO_FEISHU=1`
启动，页头角标因此恒为确定性的「显式停用」，不是真实调度器状态）。

确定性约定：
- 所有时间戳由冻结时刻（--freeze-time，默认 DEFAULT_FREEZE_TIME，按 REQ_BOARD_TZ 解释）倒推，不读系统时钟；
- 自增主键只由插入顺序决定；同一份 flows.json 下两次落库的逻辑内容完全一致（见 dump_db）；
- 会话/对话类行只种终态（idle/failed/毒会话），绝不种 running/interrupted——看板启动时的恢复逻辑
  会对这两种状态尝试拉起 kimi。

安全红线：拒绝写入本仓库（及其主工作区）的 data/ 与 ~/.req-board 下任何路径；目标库已有数据时
须显式 --force（先删库再重建）。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# 默认冻结时刻（按 REQ_BOARD_TZ 解释）：种子数据的「现在」，test_instance.sh --seed 与 visual_gate.py 同取此值
DEFAULT_FREEZE_TIME = "2026-01-15 10:30:00"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage_time import parse_storage_time
TS_FMT = "%Y-%m-%d %H:%M:%S"
STATUSES = ("later", "open", "doing", "done", "dropped")
PROJECTS = ("demo-alpha", "demo-beta", "demo-gamma")


class SeedRefused(Exception):
    """安全闸门拒绝（禁区路径 / 目标库非空）。"""


# ==================== 安全闸门 ====================
def _main_workspace_root():
    """worktree 的主工作区根（.git 文件里的 gitdir 指向 <主工作区>/.git/worktrees/<名>）；
    非 worktree / 解析失败返回 None。"""
    git = REPO_ROOT / ".git"
    try:
        if git.is_file():
            line = git.read_text(encoding="utf-8").strip()
            if line.startswith("gitdir:"):
                gitdir = Path(line.split(":", 1)[1].strip())
                for parent in [gitdir, *gitdir.parents]:
                    if parent.name == ".git":
                        return parent.parent
    except OSError:
        pass
    return None


def forbidden_roots():
    """禁止写入的目录根（绝对路径，已解析软链）：本仓库 data/、主工作区 data/、
    projects.json 软链指向的真实仓库 data/、~/.req-board。"""
    roots = [REPO_ROOT / "data", Path.home() / ".req-board"]
    main = _main_workspace_root()
    if main:
        roots.append(main / "data")
    pj = REPO_ROOT / "projects.json"
    if pj.is_symlink():
        roots.append(Path(os.path.realpath(pj)).parent / "data")
    out = []
    for r in roots:
        real = Path(os.path.realpath(r))
        if real not in out:
            out.append(real)
    return out


def _inside(path, root):
    return path == root or root in path.parents


def check_target(data_dir, force=False, roots=None):
    """校验目标数据目录：命中禁区或库非空（且未 --force）抛 SeedRefused。
    纯检查、不创建任何文件/目录；返回解析后的绝对路径。"""
    target = Path(os.path.realpath(Path(data_dir).expanduser()))
    for root in (forbidden_roots() if roots is None else roots):
        if _inside(target, Path(os.path.realpath(root))):
            raise SeedRefused(f"拒绝写入受保护路径: {target}（位于 {root} 内；种子数据只能写临时目录）")
    db = target / "board.db"
    if db.exists() and not force and _db_has_rows(db):
        raise SeedRefused(f"目标库已有数据: {db}（确认可覆盖请加 --force）")
    return target


def _db_has_rows(db_path):
    """库里任一用户表有行即 True；打不开/损坏按「有数据」处理（fail-closed）。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return any(conn.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone() for t in tables)
        finally:
            conn.close()
    except sqlite3.Error:
        return True


# ==================== 逻辑转储（确定性比对用） ====================
def dump_db(db_path):
    """全库逻辑转储：{表名: [行 dict...]}，表按名排序、行按主键（rowid）排序。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        return {t: [dict(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid')]
                for t in tables}
    finally:
        conn.close()


# ==================== 数据集 ====================
class _Clock:
    """由冻结时刻倒推时间戳：s(分钟前) → 存储时区字符串；e(分钟前) → epoch 秒。"""

    def __init__(self, freeze_time):
        self.now = parse_storage_time(freeze_time)

    def s(self, minutes_ago):
        return (self.now - timedelta(minutes=minutes_ago)).strftime(TS_FMT)

    def e(self, minutes_ago):
        return (self.now - timedelta(minutes=minutes_ago)).timestamp()


LONG_TITLE = ("超长标题：跨仓库重构会话生命周期管理并同步调整调度器重试、超时与人工接管语义，"
              "同时保持历史卡片回放兼容（用于验证卡片标题换行与截断表现）")
SHORT_TITLES = ("修复登录页错位", "补充导出按钮", "调整轮询间隔", "清理过期缓存", "文案统一")
LONG_NOTES = ("## 背景\n\n线上反馈列表页在窄屏下操作列被挤压，按钮文字换行。\n\n"
              "## 要求\n\n- 窄屏（≤ 390px）操作列折叠为「更多」菜单\n- 保持键盘可达与 `focus-visible` 样式\n"
              "- 不改动既有接口字段\n\n## 验收\n\n1. 1440 与 390 两档视口截图对比\n2. 暗色 / 亮色皮肤均无白底泄漏\n\n"
              "```bash\nscripts/test_instance.sh start 8931 --seed\n```\n")
AUTO_SUMMARY = ("人工前言：本卡用于演示多行开发总结。\n"
                "[auto] 技术方案完成：拆为前端布局与接口字段两部分\n"
                "[auto] 方案评审打回（第 1 轮）：\n  - 缺少窄屏回退策略\n  - 未说明历史数据迁移\n"
                "[auto] 方案评审通过（第 2 轮）\n"
                "[auto] 开发完成，自测通过（12 passed）\n"
                "[auto] 代码审查通过；验收通过，已合并到 main")


def _flows():
    """当前 flows.json 定义的流程（动态探测，含 quick 等后续新增流程）：[{id,name,nodes}...]。"""
    import acp_shared
    return [{"id": f["id"], "name": f.get("name") or f["id"], "nodes": list(f.get("nodes") or [])}
            for f in acp_shared.flows()]


def _node_roles(nodes):
    return [n.get("role") for n in nodes if n.get("role")]


def projects_fixture():
    """配套 projects.json：路径刻意指向不存在的临时路径（看板对缺失目录宽容，仅展示）。"""
    return {
        "demo-alpha": {"paths": ["/tmp/req-board-seed/demo-alpha"], "company": False,
                       "base_branch": "main", "goal": "演示项目 A：前端看板与视觉规范"},
        "demo-beta": {"paths": ["/tmp/req-board-seed/demo-beta/api", "/tmp/req-board-seed/demo-beta/web"],
                      "company": False, "base_branch": "main",
                      "goal": "演示项目 B：多仓库服务，目标描述较长用于验证项目信息条的换行与截断表现"},
        "demo-gamma": "/tmp/req-board-seed/demo-gamma",
    }


def _insert_dataset(conn, clock, flows):
    """按固定顺序插入全部行（顺序即主键，改顺序 = 改数据集）。"""
    n = 0   # 卡序号（决定各字段取值的唯一变量）
    total = len(PROJECTS) * len(STATUSES) * len(flows)   # 卡片总数随流程条数变（新增流程不改这里）
    sessions_state = {}
    pending_confirm = {}
    for pi, project in enumerate(PROJECTS):
        for si, status in enumerate(STATUSES):
            for fi, flow in enumerate(flows):
                n += 1
                # 创建时刻：距冻结时刻的分钟数（越早的卡越大）；最后一张卡也在 2 天前，
                # 给后续流转/会话/事件的偏移留足余量，保证没有任何时间戳晚于冻结时刻
                base = 60 * 24 * (total + 2 - n) + n * 7
                nodes = flow["nodes"]
                roles = _node_roles(nodes)
                last = len(nodes) - 1
                node_index = {"later": 0, "open": 0, "doing": (pi + fi) % max(last, 1),
                              "done": last, "dropped": min(1, last)}[status]
                title = LONG_TITLE if (n % 7 == 3) else f"{SHORT_TITLES[n % len(SHORT_TITLES)]}（{flow['name']}流程 · {status}）"
                notes = LONG_NOTES if n % 3 == 0 else ("" if n % 5 == 0 else f"简短备注：{project} 的第 {n} 张演示卡。")
                summary = AUTO_SUMMARY if status == "done" else (
                    "[auto] 运行失败（超时），已转人工" if status == "dropped" else None)
                started = clock.s(base - 30) if status in ("doing", "done", "dropped") else None
                completed = clock.s(base - 300) if status == "done" else None
                updated = clock.s(base - 300 if status in ("done", "dropped") else base - 45
                                  if status == "doing" else base)
                phase = (nodes[node_index].get("role") if status == "doing" else None)
                cur = conn.execute(
                    "INSERT INTO requirements (title, project, status, notes, summary, created_at, updated_at,"
                    " started_at, completed_at, mode, phase, flow, flow_name, nodes_json, node_index,"
                    " source, model, reasoning_effort, source_session_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (title, project, status, notes, summary, clock.s(base), updated, started, completed,
                     "semi", phase, flow["id"], flow["name"], json.dumps(nodes, ensure_ascii=False), node_index,
                     ("board-chat", "codex", "dsh")[n % 3], ("deepseek/v4.1-flash", "kimi-k2", None)[n % 3],
                     ("high", None, "max")[n % 3], f"seed-src-{n:03d}"))
                rid = cur.lastrowid
                # —— 状态审计：baseline + 实际流转 ——
                log = [(None, "open", "baseline", "上线快照", base)]
                if status == "later":
                    log.append(("open", "later", "manual", "manual:网页-暂缓", base - 5))
                elif status in ("doing", "done", "dropped"):
                    log.append(("open", "doing", "dispatcher", "dispatcher:自动捞取", base - 30))
                    if status == "done":
                        log.append(("doing", "done", "dispatcher", "dispatcher:验收通过合并", base - 300))
                    elif status == "dropped":
                        log.append(("doing", "dropped", "manual", "manual:网页-废弃", base - 300))
                for old, new, actor, detail, ago in log:
                    conn.execute("INSERT INTO req_status_log (req_id, old_status, new_status, actor, detail, ts)"
                                 " VALUES (?,?,?,?,?,?)", (rid, old, new, actor, detail, clock.s(ago)))
                if status in ("later", "open"):
                    continue
                # —— 会话 / 运行 / 事件：已走过的节点各一条会话（均为终态） ——
                done_roles = roles[:max(1, min(node_index + 1, len(roles)))]
                conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                             (rid, "flow_choice", f"前置分析建议走「{flow['name']}」流程", clock.s(base - 29), None))
                for ri, role in enumerate(done_roles):
                    sid = f"seed-{rid:03d}-{role}"
                    t0 = base - 31 - ri * 40                 # 该会话开始时刻（分钟前）
                    is_last = ri == len(done_roles) - 1
                    failed = is_last and status == "dropped"
                    poisoned = 1 if (failed and n % 2 == 0) else 0
                    sess_status = "failed" if failed else "idle"
                    model = ("deepseek/v4.1-flash", "kimi-k2")[(n + ri) % 2]
                    conn.execute(
                        "INSERT INTO agent_sessions (req_id, role, session_id, model, thinking, harness, context_pct,"
                        " branch, workdir, poisoned, status, created_at, updated_at, turn_step, turn_step_detail,"
                        " last_llm_request_at, last_llm_response_at, last_thought_at, last_tool_started_at,"
                        " origin, origin_detail, model_requested, model_source, model_configured, model_reason,"
                        " thinking_requested, thinking_configured, thinking_reason)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, role, sid, model, "high", "kimi-code 2.1.1", f"{18 + (n * 7 + ri * 11) % 70}%",
                         f"req-{rid}", f"/tmp/req-board-seed/{project}/.worktrees/req-{rid}", poisoned, sess_status,
                         clock.s(t0), clock.s(t0 - 25), None, None,
                         clock.e(t0 - 20), clock.e(t0 - 21), clock.e(t0 - 22), clock.e(t0 - 23),
                         "dispatcher", "seed", model, ("role", "global", "project")[ri % 3], model,
                         "模型不可用，已回落全局默认" if (n + ri) % 6 == 0 else None,
                         "high", "high", None))
                    conn.execute(
                        "INSERT INTO agent_runs (req_id, role, session_id, started_at, ended_at, status, tool_count,"
                        " context_pct, context_tokens, max_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, role, sid, clock.e(t0), clock.e(t0 - 25), "failed" if failed else "done",
                         3 + (n + ri) % 9, float(18 + (n * 7 + ri * 11) % 70),
                         20000 + 1500 * ((n * 3 + ri) % 40), 128000))
                    events = [("system", f"agent 启动（{role}），ACP 会话已建立"),
                              ("system", "会话已建立，开始执行"),
                              ("user", f"请按角色「{role}」处理需求 #{rid}：{title[:20]}"),
                              ("message", f"已阅读需求，先梳理改动面。\n\n- 涉及文件：`board.py`\n- 风险：**低**"),
                              ("tool", "read_file board.py（L1-L200）"),
                              ("tool", "失败: run_command pytest -q（exit 1）" if failed else "run_command pytest -q（exit 0）"),
                              ("message", "本轮处理完成，结果已写入产物。" if not failed else "运行超时，未能完成。"),
                              ("system", f"运行结束（{'failed' if failed else 'done'}）")]
                    for ei, (kind, content) in enumerate(events):
                        conn.execute("INSERT INTO agent_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                                     (rid, kind, content, clock.s(t0 - ei * 3), sid))
                    conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                                 (rid, "spawn", f"拉起会话（role={role}，model={model}）", clock.s(t0), sid))
                    conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                                 (rid, "model", f"模型解析：{model}（来源 {('role', 'global', 'project')[ri % 3]}）",
                                  clock.s(t0), None))
                    if failed:
                        conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                                     (rid, "retry", f"运行失败（超时），第 1 次自动重试（role={role}）", clock.s(t0 - 26), None))
                    else:
                        conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                                     (rid, "phase", f"「{role}」完成，推进到下一节点", clock.s(t0 - 26), None))
                    if is_last and status == "doing":
                        sessions_state[str(rid)] = {"id": sid, "model": model, "thinking": "high",
                                                    "context": f"{18 + (n * 7 + ri * 11) % 70}%",
                                                    "branch": f"req-{rid}", "harness": "kimi-code 2.1.1"}
                # —— 评审记录：打回 + 通过 ——
                reviews = []
                if status == "done":
                    reviews = [("reject", "## 结论\n\n打回：缺少窄屏回退策略。\n\n- [ ] 补充 390px 布局\n- [ ] 说明数据迁移"),
                               ("pass", "## 结论\n\n通过。两项问题均已修复，自测记录完整。")]
                elif status == "dropped":
                    reviews = [("reject", "打回：方案与现有调度语义冲突，建议废弃。")]
                elif n % 2 == 0:
                    reviews = [("pass", "通过：改动面清楚，可直接进入开发。")]
                for rnd, (verdict, content) in enumerate(reviews, 1):
                    conn.execute(
                        "INSERT INTO req_reviews (req_id, round, reviewer, verdict, content, notes_snapshot, created_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (rid, rnd, "kimi-k2", verdict, content, notes, clock.s(base - 60 - rnd * 20)))
                if status == "done":
                    conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                                 (rid, "merge", f"分支 req-{rid} 已合并到 main，worktree 已清理", clock.s(base - 300), None))
                    conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                                 (rid, "action", "需求收尾完成", clock.s(base - 300), None))
                if status == "doing" and not pending_confirm:
                    conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts, session_id) VALUES (?,?,?,?,?)",
                                 (rid, "confirm", "agent 请求人工确认：是否同时调整移动端布局？", clock.s(base - 46), None))
                    pending_confirm[str(rid)] = {"question": "是否同时调整移动端布局？",
                                                 "options": ["一并调整", "仅桌面端"],
                                                 "role": done_roles[-1], "phase": done_roles[-1]}
    return {"sessions": sessions_state, "pending_confirm": pending_confirm,
            "pending_route": {}, "pending_review": {}}


def seed(data_dir, force=False, freeze_time=DEFAULT_FREEZE_TIME, roots=None):
    """落种子数据到 data_dir，返回 {data_dir, db, projects, cards}。安全闸门不过抛 SeedRefused。"""
    target = check_target(data_dir, force=force, roots=roots)
    clock = _Clock(freeze_time)
    target.mkdir(parents=True, exist_ok=True)
    db = target / "board.db"
    for stale in (db, target / "board.db-wal", target / "board.db-shm"):
        if stale.exists():
            stale.unlink()          # 仅 --force 或空库能走到这里
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import board
    # init_db 经 get_db 现读模块级 DB_PATH：临时指向目标库建表（board 可能已被以别的数据目录 import）
    saved = board.DB_PATH
    board.DB_PATH = db
    try:
        board.init_db()
    finally:
        board.DB_PATH = saved
    flows = _flows()
    conn = sqlite3.connect(str(db))
    try:
        state = _insert_dataset(conn, clock, flows)
        cards = conn.execute("SELECT COUNT(*) FROM requirements").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    projects = target / "projects.json"
    projects.write_text(json.dumps(projects_fixture(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "dispatcher_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hb = target / "dispatcher_heartbeat.json"
    if hb.exists():
        hb.unlink()                 # 不留心跳：配合实例脚本的 REQ_BOARD_NO_FEISHU=1，角标恒为「显式停用」
    return {"data_dir": str(target), "db": str(db), "projects": str(projects), "cards": cards}


def main(argv=None):
    ap = argparse.ArgumentParser(description="往测试数据目录写确定性种子数据（仅限临时目录）")
    ap.add_argument("--data-dir", help="目标数据目录（必填；写入 board.db / projects.json / dispatcher_state.json）")
    ap.add_argument("--force", action="store_true", help="目标库已有数据时删库重建")
    ap.add_argument("--freeze-time", default=DEFAULT_FREEZE_TIME,
                    help=f"冻结时刻（存储时区 YYYY-MM-DD HH:MM:SS，默认 {DEFAULT_FREEZE_TIME}）")
    ap.add_argument("--print-freeze-time", action="store_true", help="只打印默认冻结时刻后退出")
    args = ap.parse_args(argv)
    if args.print_freeze_time:
        print(DEFAULT_FREEZE_TIME)
        return 0
    if not args.data_dir:
        ap.error("--data-dir 必填")
    try:
        datetime.strptime(args.freeze_time, TS_FMT)
        target = check_target(args.data_dir, force=args.force)
    except ValueError:
        print(f"--freeze-time 非法: {args.freeze_time!r}（需为 YYYY-MM-DD HH:MM:SS）", file=sys.stderr)
        return 2
    except SeedRefused as e:
        print(str(e), file=sys.stderr)
        return 2
    # board 在 import 期就会创建数据目录：先把环境指向目标目录，避免在仓库根生成 data/
    os.environ["REQ_BOARD_DATA"] = str(target)
    os.environ.setdefault("REQ_BOARD_NO_FEISHU", "1")
    info = seed(target, force=args.force, freeze_time=args.freeze_time)
    print(f"种子数据已写入: {info['db']}（{info['cards']} 张卡；projects={info['projects']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
