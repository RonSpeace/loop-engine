"""loop init 向导与 doctor 运行体感知（假 OpenAI 兼容服务 + 临时目录，不碰真实 .env / projects.json）。"""
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import doctor  # noqa: E402
import loop_init  # noqa: E402

GOOD_KEY = "sk-test-good"


class FakeOpenAI(BaseHTTPRequestHandler):
    models = ["cheap-1", "strong-1"]
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if self.headers.get("Authorization") != f"Bearer {GOOD_KEY}":
            self._send(401, {"error": "bad key"})
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        if self.path == "/v1/models":
            self._send(200, {"data": [{"id": m} for m in self.models]})
        else:
            self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self._authed():
            return
        FakeOpenAI.calls.append(body.get("model"))
        if body.get("model") in self.models:
            self._send(200, {"choices": [{"message": {"content": "ok"}}]})
        else:
            self._send(404, {"error": "model not found"})


class LoopInitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        FakeOpenAI.calls = []
        p = mock.patch.object(loop_init, "CONFIG_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        d = mock.patch.object(loop_init, "run_doctor", lambda: None)
        d.start()
        self.addCleanup(d.stop)

    def run_init(self, *args, key=GOOD_KEY):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TEST_LOOP_KEY": key}), redirect_stdout(out):
            code = loop_init.main(["--yes", "--no-start", "--api-key-env", "TEST_LOOP_KEY", *args])
        return code, out.getvalue()

    # ---- .env / projects.json ----

    def test_write_env_keeps_user_lines_and_is_private(self):
        env = self.tmp / ".env"
        env.write_text("# mine\nREQ_BOARD_PORT=9000\nLOOP_OPENCODE_MODEL=old\nLOOP_OPENCODE_MODEL=older\n")
        loop_init.write_env(env, {"LOOP_OPENCODE_MODEL": "new", "LOOP_OPENAI_API_KEY": "k"})
        text = env.read_text()
        self.assertIn("# mine\nREQ_BOARD_PORT=9000\n", text)
        self.assertEqual(text.count("LOOP_OPENCODE_MODEL="), 1)
        self.assertIn("LOOP_OPENCODE_MODEL=new", text)
        self.assertIn("LOOP_OPENAI_API_KEY=k", text)
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)

    def test_write_project_merges_existing_projects(self):
        pf = self.tmp / "projects.json"
        pf.write_text(json.dumps({"other": {"paths": ["/x"], "runtimes": {"dev": "codex"}}}))
        loop_init.write_project(pf, "toy", Path("/t"), "main", "goal")
        data = json.loads(pf.read_text())
        self.assertEqual(data["other"]["runtimes"], {"dev": "codex"})
        self.assertEqual(data["toy"], {"paths": ["/t"], "base_branch": "main", "goal": "goal"})

    # ---- 服务商校验 ----

    def test_list_models_and_bad_key(self):
        self.assertEqual(loop_init.list_models(self.base, GOOD_KEY), ["cheap-1", "strong-1"])
        with self.assertRaises(loop_init.InitError):
            loop_init.list_models(self.base, "sk-wrong")
        self.assertIsNone(loop_init.ping_model(self.base, GOOD_KEY, "cheap-1"))
        self.assertIn("404", loop_init.ping_model(self.base, GOOD_KEY, "missing"))

    # ---- 端到端（无人值守）----

    def test_full_run_with_toy_project(self):
        toy = self.tmp / "toy"
        code, out = self.run_init("--provider", "custom", "--base-url", self.base, "--model", "cheap-1",
                                  "--dev-model", "strong-1", "--project", "toy", "--toy-dir", str(toy))
        self.assertEqual(code, 0, out)
        env = loop_init.read_env(self.tmp / ".env")
        self.assertEqual(env["LOOP_AGENT_RUNTIME"], "opencode")
        self.assertEqual(env["LOOP_OPENAI_BASE_URL"], self.base)
        self.assertEqual(env["LOOP_OPENCODE_MODEL"], "cheap-1")
        self.assertEqual(env["LOOP_OPENCODE_MODEL_DEV"], "strong-1")
        self.assertEqual(FakeOpenAI.calls, ["cheap-1", "strong-1"])   # 两个模型都试调用过
        self.assertNotIn(GOOD_KEY, out)                                 # Key 不出现在输出里
        proj = json.loads((self.tmp / "projects.json").read_text())["toy-project"]
        self.assertEqual(proj["paths"], [str(toy.resolve())])
        self.assertTrue((toy / ".git").is_dir())
        ident = subprocess.run(["git", "var", "GIT_COMMITTER_IDENT"], cwd=toy, capture_output=True)
        self.assertEqual(ident.returncode, 0)

    def test_port_from_environment_is_saved(self):
        with mock.patch.dict(os.environ, {"REQ_BOARD_PORT": "8941"}):
            code, out = self.run_init("--provider", "custom", "--base-url", self.base, "--model", "cheap-1",
                                      "--project", "toy", "--toy-dir", str(self.tmp / "toy"))
        self.assertEqual(code, 0, out)
        self.assertEqual(loop_init.read_env(self.tmp / ".env")["REQ_BOARD_PORT"], "8941")

    def test_prompt_language_follows_flag_then_locale(self):
        with mock.patch.dict(os.environ, {"LANG": "zh_CN.UTF-8", "LC_ALL": "", "LC_MESSAGES": ""}):
            code, out = self.run_init("--provider", "custom", "--base-url", self.base, "--model", "cheap-1",
                                      "--project", "toy", "--toy-dir", str(self.tmp / "toy"))
        self.assertEqual(code, 0, out)
        self.assertEqual(loop_init.read_env(self.tmp / ".env")["LOOP_PROMPT_LANG"], "zh")
        code, out = self.run_init("--provider", "custom", "--base-url", self.base, "--model", "cheap-1",
                                  "--lang", "en", "--project", "toy", "--toy-dir", str(self.tmp / "toy"))
        self.assertEqual(code, 0, out)
        self.assertEqual(loop_init.read_env(self.tmp / ".env")["LOOP_PROMPT_LANG"], "en")
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "", "LC_MESSAGES": ""}):
            self.assertEqual(loop_init.default_prompt_lang(), "en")

    def test_rejected_key_stops_before_writing(self):
        code, out = self.run_init("--provider", "custom", "--base-url", self.base, "--model", "cheap-1",
                                  "--project", "toy", "--toy-dir", str(self.tmp / "toy"), key="sk-wrong")
        self.assertEqual(code, 1)
        self.assertIn("rejected", out)
        self.assertFalse((self.tmp / ".env").exists())

    def test_unknown_model_is_caught(self):
        code, out = self.run_init("--provider", "custom", "--base-url", self.base, "--model", "nope",
                                  "--project", "toy", "--toy-dir", str(self.tmp / "toy"))
        self.assertEqual(code, 1)
        self.assertIn("nope", out)
        self.assertFalse((self.tmp / ".env").exists())

    def test_unattended_without_model_does_not_guess(self):
        code, out = self.run_init("--provider", "custom", "--base-url", self.base,
                                  "--project", "toy", "--toy-dir", str(self.tmp / "toy"))
        self.assertEqual(code, 1)
        self.assertIn("--model", out)

    def test_existing_repo_without_commits_is_refused(self):
        repo = self.tmp / "empty"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        code, out = self.run_init("--provider", "custom", "--base-url", self.base, "--model", "cheap-1",
                                  "--project", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("no commits", out)

    def test_presets_are_well_formed(self):
        for name, p in loop_init.PRESETS.items():
            self.assertEqual(set(p), {"label", "base_url", "cheap", "dev", "key_url"}, name)
            if name != "custom":
                self.assertTrue(p["base_url"].startswith("https://"), name)


class DoctorRuntimeTest(unittest.TestCase):
    def test_used_runtimes_follows_env(self):
        with mock.patch.dict(os.environ, {"LOOP_AGENT_RUNTIME": "opencode"}):
            self.assertIn("opencode", doctor.used_runtimes())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOOP_AGENT_RUNTIME", None)
            self.assertIn("kimi", doctor.used_runtimes())

    def test_opencode_missing_key_is_hard_failure(self):
        env = {"LOOP_OPENAI_BASE_URL": "https://example.invalid/v1", "LOOP_OPENAI_API_KEY": "",
               "LOOP_OPENCODE_MODEL": "m", "LOOP_AGENT_CMD_OPENCODE": shlex.quote(sys.executable)}
        with mock.patch.dict(os.environ, env), mock.patch.object(doctor, "HARD_FAIL", []), \
                redirect_stdout(io.StringIO()) as out:
            doctor.check_opencode()
            self.assertEqual(len(doctor.HARD_FAIL), 1)
        self.assertIn("LOOP_OPENAI_API_KEY", out.getvalue())

    def test_opencode_ok_never_prints_key(self):
        env = {"LOOP_OPENAI_BASE_URL": "https://example.invalid/v1", "LOOP_OPENAI_API_KEY": "sk-secret-123",
               "LOOP_OPENCODE_MODEL": "m", "LOOP_AGENT_CMD_OPENCODE": shlex.quote(sys.executable)}
        with mock.patch.dict(os.environ, env), mock.patch.object(doctor, "HARD_FAIL", []), \
                redirect_stdout(io.StringIO()) as out:
            doctor.check_opencode()
            self.assertEqual(doctor.HARD_FAIL, [])
        self.assertNotIn("sk-secret-123", out.getvalue())


class LoopCliTest(unittest.TestCase):
    def test_help_and_unknown_command(self):
        r = subprocess.run([str(REPO / "bin" / "loop"), "help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("loop init", r.stdout)
        r = subprocess.run([str(REPO / "bin" / "loop"), "nope"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)

    def test_scripts_parse(self):
        for f in ("bin/loop", "scripts/install.sh"):
            r = subprocess.run(["bash", "-n", str(REPO / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f + r.stderr)


if __name__ == "__main__":
    unittest.main()
