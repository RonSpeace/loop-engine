"""测试基线隔离（单一源）：把 dispatcher 的项目配置钉到进程私有 fixture，与本机 projects.json 解耦。

背景（2026-10-03 现场，2026-10-10 补齐 unittest 线）：dispatcher 在 import 时读
`REPO/projects.json`（gitignore 的本地数据，各机器不同），而大量测试文件的用例硬编码
`project="req-board"`。本机这份文件里若没有 `req-board`（例如只有 loop-engine / toy-project），
`PROJECTS["req-board"]` 抛 KeyError 连锁失败（现场实测 178 个 error 全出此一处）。

原先的兜底就地写在各测试文件模块级：

    _PJ = REPO / "projects.json"
    if not _PJ.exists():
        _PJ.write_text(json.dumps({"req-board": str(REPO)}))

它只在「本机没有 projects.json」时才触发；一旦主工作区存在真实配置就形同虚设。而 pytest 侧的
`tests/conftest.py` 与 unittest 侧的 `tests/run_unittest_with_fixture.py` 各自又抄了一份钉桩逻辑
——三处漂移，且 conftest 只有 pytest 会加载，于是两条线结论相反：

| 运行器 | 结果 |
|:--|:--|
| `pytest tests -q` | 基线生效，全绿 |
| `python -m unittest discover -s tests` | 基线不生效，178 个 KeyError |

**本模块是唯一实现**：conftest（pytest 线）、夹具 runner（unittest 线）、各测试文件模块级都调
`pin()`。幂等，可重复调用。

**不反写主工作区数据**（#303 红线）：钉桩一律用临时目录里的 fixture；只有「仓库根 projects.json
既不存在也不是软链」时才会落一份最小文件——那是新克隆首跑的必要条件（dispatcher 在 import 期就会
`read_text` 它，不存在即 ImportError），且该路径本就在 .gitignore 内。worktree 里它是主工作区那份的
软链，命中 `is_symlink()` 一律不碰。
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_pinned = False


def pin():
    """把 dispatcher / acp_shared 的项目配置钉到只含 req-board 的 fixture。幂等。"""
    global _pinned
    if _pinned:
        return
    _pinned = True

    # 1) 先保证 dispatcher 能 import：新克隆无 projects.json 时其模块级 load_projects() 会
    #    FileNotFoundError。软链（含断链）一律不写——worktree 里它指向主工作区真实数据。
    repo_pj = REPO / "projects.json"
    if not repo_pj.exists() and not repo_pj.is_symlink():
        repo_pj.write_text(json.dumps({"req-board": str(REPO)}), encoding="utf-8")

    # 1b) 通知渠道隔离：notifiers.json 钉到进程私有临时目录（不存在 = 无 Webhook/Slack 渠道），
    #     防止本机真实配置让测试里的 notify / notify_req_card 往外发消息。须在 import 前设置。
    import os
    ntf_dir = Path(tempfile.mkdtemp(prefix="req-board-tests-ntf-"))
    os.environ.setdefault("REQ_BOARD_NOTIFIERS_JSON", str(ntf_dir / "notifiers.json"))
    # 1c) 提示词本机覆盖隔离：prompts.local 指向进程私有空目录，本机覆盖不影响提示词断言。
    os.environ.setdefault("REQ_BOARD_PROMPTS_LOCAL",
                          tempfile.mkdtemp(prefix="req-board-tests-prompts-local-"))

    # 2) import 之后一律改钉进程私有 fixture：不读也不改本机 projects.json。
    import dispatcher as d
    import acp_shared as ac

    d.NOTIFIERS.config_file = Path(os.environ["REQ_BOARD_NOTIFIERS_JSON"])
    d.NOTIFIERS.reload(force=True)

    fixture = Path(tempfile.mkdtemp(prefix="req-board-tests-")) / "projects.json"
    fixture.write_text(json.dumps({"req-board": str(REPO)}), encoding="utf-8")

    d.PROJECTS_FILE = fixture
    d.PROJECTS = d.load_projects()
    # 热重载基线与 fixture 同步：否则 import 期基线来自真实 projects.json，直调 tick 的用例
    # 会因 fixture 与基线不同而触发一次真重载。
    d._PROJECTS_SIG = None
    # acp_shared 的项目层同钉 fixture（四层链隔离）。
    ac.PROJECTS_FILE = fixture
