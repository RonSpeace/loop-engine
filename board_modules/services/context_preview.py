"""注入预览服务：子进程运行 scripts/context_preview.py（与调度器同一上下文编译器 dry 编译）。

子进程隔离调度器模块状态（import dispatcher 的副作用不进看板进程）；超时 20 秒。
"""


from board_modules.runtime import resolve


import json
import os
import sys
from pathlib import Path


PREVIEW_TIMEOUT_SEC = 20
PREVIEW_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "context_preview.py"


def _context_preview_run(project, role, req):
    """dry 编译；返回 (HTTP 状态码, payload)。项目清单/产物根跟随看板当前配置（测试实例一致）。"""
    env = dict(os.environ)
    env["REQ_BOARD_PROJECTS"] = str(resolve("PROJECTS_FILE"))
    env["REQ_BOARD_ARTIFACTS"] = str(resolve("ARTIFACTS_ROOT"))
    env.setdefault("REQ_BOARD_NO_FEISHU", "1")
    sp = resolve("subprocess")
    try:
        r = sp.run([sys.executable, str(PREVIEW_SCRIPT), "--project", project, "--role", role,
                    "--req-json", "-"], input=json.dumps(req or {}, ensure_ascii=False),
                   capture_output=True, text=True, timeout=PREVIEW_TIMEOUT_SEC, env=env,
                   cwd=str(PREVIEW_SCRIPT.parent.parent))
    except sp.TimeoutExpired:
        return 504, {"error": f"注入预览超时（>{PREVIEW_TIMEOUT_SEC}s）"}
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return 500, {"error": "注入预览失败", "detail": (r.stderr or "")[-800:]}
    if "error" in payload:
        return 400, payload
    return 200, payload
