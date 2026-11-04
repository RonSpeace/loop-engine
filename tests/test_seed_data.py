#!/usr/bin/env python3
"""测试实例确定性种子数据（scripts/seed_test_db.py）与配套的 board 环境开关。

覆盖：
- S1 确定性：两次落到两个空目录 → 全表逻辑转储完全一致；配套 projects.json / state 文件字节一致
- S2 覆盖面：每个状态 × flows.json 每个流程都有卡；3 个项目；长/短标题、多行 [auto] 总结；
  会话只有终态（无 running/interrupted）且含失败与毒会话；评审含 pass 与 reject；不写心跳文件
- S3 安全闸门：~/.req-board 内、仓库 data/ 内一律拒绝且不创建任何东西；非空库无 --force 拒绝、--force 重建
- S4 REQ_BOARD_DATA / REQ_BOARD_PROJECTS：子进程 import board 后 DB_PATH 与 project_list() 跟随环境变量
- S5 REQ_BOARD_FREEZE_TIME：server_now / now_str 固定、首页注入冻结脚本；非法值拒绝启动
- S6 缺省零变化：未设环境变量时不冻结、首页不含冻结脚本、数据目录仍是仓库根 data/

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m pytest -q tests/test_seed_data.py

隔离：全部落临时目录；需要「带环境变量 import board」的用例一律走子进程（环境变量在 import 期
生效，进程内改会污染其它测试）；绝不写生产 data/board.db，不拉起任何服务进程。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

sys.path.insert(0, str(REPO / "scripts"))
import seed_test_db as seed  # noqa: E402
import board as b  # noqa: E402

SEED_SCRIPT = REPO / "scripts" / "seed_test_db.py"
FREEZE = "2026-01-15 10:30:00"
from storage_time import parse_storage_time
FREEZE_TS = parse_storage_time(FREEZE).timestamp()


def _clean_env(**extra):
    """子进程环境：去掉会改变 board import 行为的 ambient 变量，再叠加用例指定值。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("REQ_BOARD_DATA", "REQ_BOARD_PROJECTS", "REQ_BOARD_FREEZE_TIME")}
    env["REQ_BOARD_NO_FEISHU"] = "1"
    env.update(extra)
    return env


def _run_seed(*args):
    return subprocess.run([sys.executable, str(SEED_SCRIPT), *args], cwd=str(REPO), env=_clean_env(),
                          capture_output=True, text=True, timeout=120)


def _run_board_snippet(code, **env):
    """子进程里 import board 后执行片段；片段须把结果 print 成最后一行 JSON。"""
    proc = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=str(REPO), env=_clean_env(**env),
                          capture_output=True, text=True, timeout=120)
    return proc


class SeedDatasetTest(unittest.TestCase):
    """S1/S2：落两份种子数据，比对确定性并检查覆盖面"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="seed-data-"))
        cls.dirs = [cls.tmp / "a", cls.tmp / "b"]
        for d in cls.dirs:
            proc = _run_seed("--data-dir", str(d))
            assert proc.returncode == 0, proc.stderr
        cls.dump = seed.dump_db(cls.dirs[0] / "board.db")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_s1_two_runs_identical(self):
        other = seed.dump_db(self.dirs[1] / "board.db")
        self.assertEqual(self.dump, other)
        for name in ("projects.json", "dispatcher_state.json"):
            self.assertEqual((self.dirs[0] / name).read_bytes(), (self.dirs[1] / name).read_bytes(), name)

    def test_s1_no_wall_clock_in_rows(self):
        """所有时间戳都由冻结时刻倒推：没有任何一行晚于冻结时刻"""
        for table, rows in self.dump.items():
            for row in rows:
                for col, val in row.items():
                    if isinstance(val, str) and len(val) == 19 and val[4] == "-" and val[13] == ":":
                        self.assertLessEqual(val, FREEZE, f"{table}.{col}")
                    if col.endswith("_at") and isinstance(val, float):
                        self.assertLessEqual(val, FREEZE_TS, f"{table}.{col}")

    def test_s2_status_flow_project_coverage(self):
        import acp_shared
        flow_ids = {f["id"] for f in acp_shared.flows()}
        self.assertTrue(flow_ids)
        cards = self.dump["requirements"]
        self.assertEqual({c["project"] for c in cards}, set(seed.PROJECTS))
        self.assertEqual(len(seed.PROJECTS), 3)
        combos = {(c["status"], c["flow"]) for c in cards}
        for status in ("later", "open", "doing", "done", "dropped"):
            for fid in flow_ids:
                self.assertIn((status, fid), combos)
        for c in cards:   # 快照自洽：node_index 落在节点序列内
            nodes = json.loads(c["nodes_json"])
            self.assertTrue(0 <= c["node_index"] < len(nodes), c["id"])

    def test_s2_text_variety(self):
        cards = self.dump["requirements"]
        lengths = [len(c["title"]) for c in cards]
        self.assertGreater(max(lengths), 60)
        self.assertLess(min(lengths), 30)
        self.assertTrue(any((c["notes"] or "").count("\n") > 5 for c in cards))
        self.assertTrue(any(not c["notes"] for c in cards))
        self.assertTrue(any((c["summary"] or "").count("[auto] ") >= 3 and "\n  - " in c["summary"]
                            for c in cards))

    def test_s2_sessions_terminal_only(self):
        sessions = self.dump["agent_sessions"]
        statuses = {s["status"] for s in sessions}
        self.assertLessEqual(statuses, {"idle", "failed"})
        self.assertEqual(statuses, {"idle", "failed"})
        self.assertTrue(any(s["poisoned"] for s in sessions))
        # 看板启动恢复只看这两张表的 running/interrupted：种子数据一行都不种
        self.assertEqual(self.dump["chat_sessions"], [])
        self.assertEqual(self.dump["req_chats"], [])

    def test_s2_related_tables_populated(self):
        self.assertEqual(len(self.dump["agent_runs"]), len(self.dump["agent_sessions"]))
        self.assertTrue(self.dump["agent_events"])
        kinds = {e["kind"] for e in self.dump["dispatcher_events"]}
        self.assertGreaterEqual(kinds, {"spawn", "phase", "merge", "retry", "confirm", "flow_choice", "model"})
        self.assertGreaterEqual({r["verdict"] for r in self.dump["req_reviews"]}, {"pass", "reject"})
        log = self.dump["req_status_log"]
        baseline_ids = {r["req_id"] for r in log if r["actor"] == "baseline"}
        self.assertEqual(baseline_ids, {c["id"] for c in self.dump["requirements"]})
        self.assertTrue(any(r["actor"] != "baseline" for r in log))

    def test_s2_fixture_files(self):
        d = self.dirs[0]
        self.assertEqual(set(json.loads((d / "projects.json").read_text())), set(seed.PROJECTS))
        self.assertFalse((d / "dispatcher_heartbeat.json").exists())
        state = json.loads((d / "dispatcher_state.json").read_text())
        doing = {str(c["id"]) for c in self.dump["requirements"] if c["status"] == "doing"}
        self.assertEqual(set(state["sessions"]), doing)
        self.assertLessEqual(set(state["pending_confirm"]), doing)

    def test_s2_init_db_adds_no_wall_clock_rows(self):
        """看板启动会再跑一次 init_db：种子已自带 baseline 行，不得被补写带系统时钟的行"""
        work = self.tmp / "reinit"
        shutil.copytree(self.dirs[0], work)
        orig = b.DB_PATH
        b.DB_PATH = work / "board.db"
        try:
            b.init_db()
        finally:
            b.DB_PATH = orig
        self.assertEqual(seed.dump_db(work / "board.db"), self.dump)


class SeedSafetyTest(unittest.TestCase):
    """S3：安全闸门"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seed-safety-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_s3_refuses_req_board_home(self):
        target = Path.home() / ".req-board" / "seed-test-must-not-exist-7f3a"
        self.assertFalse(target.exists())
        with self.assertRaises(seed.SeedRefused):
            seed.check_target(target)
        with self.assertRaises(seed.SeedRefused):
            seed.seed(target, force=True)
        self.assertFalse(target.exists())

    def test_s3_refuses_repo_data_dirs(self):
        roots = seed.forbidden_roots()
        self.assertIn(Path(os.path.realpath(REPO / "data")), roots)
        self.assertIn(Path(os.path.realpath(Path.home() / ".req-board")), roots)
        for root in roots:
            for target in (root, root / "nested" / "dir"):
                with self.assertRaises(seed.SeedRefused):
                    seed.check_target(target, force=True)

    def test_s3_main_workspace_data_protected_in_worktree(self):
        """worktree 里 projects.json 是指向主工作区的软链：主工作区 data/ 必须在禁区内"""
        pj = REPO / "projects.json"
        if not pj.is_symlink():
            self.skipTest("非 worktree（projects.json 不是软链）")
        main_data = Path(os.path.realpath(pj)).parent / "data"
        self.assertIn(main_data, seed.forbidden_roots())

    def test_s3_symlink_into_forbidden_root_refused(self):
        protected = self.tmp / "protected"
        protected.mkdir()
        link = self.tmp / "link"
        link.symlink_to(protected)
        with self.assertRaises(seed.SeedRefused):
            seed.check_target(link / "x", roots=[protected])

    def test_s3_cli_refusal_exit_code(self):
        proc = _run_seed("--data-dir", str(REPO / "data"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("拒绝写入受保护路径", proc.stderr)

    def test_s3_nonempty_db_needs_force(self):
        d = self.tmp / "d"
        self.assertEqual(_run_seed("--data-dir", str(d)).returncode, 0)
        first = seed.dump_db(d / "board.db")
        again = _run_seed("--data-dir", str(d))
        self.assertEqual(again.returncode, 2)
        self.assertIn("--force", again.stderr)
        self.assertEqual(seed.dump_db(d / "board.db"), first)
        self.assertEqual(_run_seed("--data-dir", str(d), "--force").returncode, 0)
        self.assertEqual(seed.dump_db(d / "board.db"), first)   # 重建后内容一致（不是追加）


class BoardEnvOverrideTest(unittest.TestCase):
    """S4/S5：环境变量在 import 期生效 → 子进程验证"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="seed-env-"))
        cls.data = cls.tmp / "data"
        proc = _run_seed("--data-dir", str(cls.data))
        assert proc.returncode == 0, proc.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _json(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_s4_data_and_projects_env(self):
        out = self._json(_run_board_snippet(
            "import json, board as b\n"
            "print(json.dumps({'db': str(b.DB_PATH), 'hb': str(b.HEARTBEAT_FILE),"
            " 'projects': b.project_list(), 'auto': list(b._auto_projects_config())}))",
            REQ_BOARD_DATA=str(self.data), REQ_BOARD_PROJECTS=str(self.data / "projects.json")))
        self.assertEqual(Path(out["db"]), self.data / "board.db")
        self.assertEqual(Path(out["hb"]).parent, self.data)
        # project_list 不再串读仓库根 projects.json（worktree 里那是主工作区真实注册表的软链）
        self.assertEqual(out["projects"], sorted(seed.PROJECTS))
        self.assertEqual(out["auto"], list(seed.PROJECTS))

    def test_s4_data_dir_created_with_parents(self):
        target = self.tmp / "deep" / "nested" / "data"
        out = self._json(_run_board_snippet(
            "import json, board as b\nprint(json.dumps({'db': str(b.DB_PATH)}))",
            REQ_BOARD_DATA=str(target)))
        self.assertEqual(Path(out["db"]), target / "board.db")
        self.assertTrue(target.is_dir())

    def test_s5_freeze_time_payloads(self):
        out = self._json(_run_board_snippet(
            "import json, time, board as b\n"
            "c = b.app.test_client()\n"
            "rows = c.get('/api/agent-sessions').get_json()\n"
            "time.sleep(0.05)\n"
            "rows2 = c.get('/api/agent-sessions').get_json()\n"
            "page = c.get('/').get_data(as_text=True)\n"
            "health = c.get('/api/dispatcher-health').get_json()\n"
            "print(json.dumps({'now': sorted({r['server_now'] for r in rows + rows2}), 'n': len(rows),"
            " 'now_str': b.now_str(), 'ts': b._now_ts(), 'head': page[:400],"
            " 'count': page.count('__REQ_BOARD_FROZEN_MS__'), 'age': health['age']}))",
            REQ_BOARD_DATA=str(self.data), REQ_BOARD_PROJECTS=str(self.data / "projects.json"),
            REQ_BOARD_FREEZE_TIME=FREEZE))
        self.assertGreater(out["n"], 0)
        self.assertEqual(out["now"], [FREEZE_TS])
        self.assertEqual(out["now_str"], FREEZE)
        self.assertEqual(out["ts"], FREEZE_TS)
        self.assertEqual(out["count"], 1)
        # 冻结脚本须在页面任何脚本之前：紧跟 <head>
        self.assertIn("<head><script>(function(){var T=%d," % int(FREEZE_TS * 1000), out["head"])
        self.assertIsNone(out["age"])   # 种子数据不写心跳

    def test_s5_freeze_time_health_age_fixed(self):
        hb_dir = self.tmp / "hb"
        hb_dir.mkdir()
        (hb_dir / "dispatcher_heartbeat.json").write_text(json.dumps({"last_poll_at": FREEZE_TS - 42}))
        out = self._json(_run_board_snippet(
            "import json, board as b\n"
            "print(json.dumps(b.app.test_client().get('/api/dispatcher-health').get_json()))",
            REQ_BOARD_DATA=str(hb_dir), REQ_BOARD_FREEZE_TIME=FREEZE))
        self.assertEqual(out["age"], 42)
        self.assertTrue(out["ok"])

    def test_s5_invalid_freeze_time_rejected(self):
        proc = _run_board_snippet("import board", REQ_BOARD_DATA=str(self.tmp / "bad"),
                                  REQ_BOARD_FREEZE_TIME="tomorrow")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REQ_BOARD_FREEZE_TIME 非法", proc.stderr)


class DefaultUnchangedTest(unittest.TestCase):
    """S6：未设环境变量时行为零变化（本进程即缺省 import）"""

    def test_s6_defaults(self):
        if any(os.environ.get(k) for k in ("REQ_BOARD_DATA", "REQ_BOARD_FREEZE_TIME")):
            self.skipTest("环境里显式设置了覆盖变量")
        # 数据目录缺省值走子进程验证：全量跑时别的测试会把本进程的 b.DATA_DIR 临时改钉
        proc = _run_board_snippet("import board as b\nprint(b.DATA_DIR == b.BASE_DIR / 'data', b.FREEZE_TS)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "True None")
        self.assertIsNone(b.FREEZE_TS)
        self.assertIsNone(b.FREEZE_TIME_STR)
        page = b.app.test_client().get("/").get_data(as_text=True)
        self.assertNotIn("__REQ_BOARD_FROZEN_MS__", page)
        self.assertNotIn("__REQ_BOARD_FROZEN_MS__", b.HTML)
        # 真实时钟仍在走
        import time
        self.assertAlmostEqual(b._now_ts(), time.time(), delta=5)
        self.assertNotEqual(b.now_str(), FREEZE)


if __name__ == "__main__":
    unittest.main()
