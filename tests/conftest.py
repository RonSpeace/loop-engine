"""pytest 侧测试基线：项目配置钉桩 + #8 标记隔离。

项目钉桩的实现在 `tests/_baseline.py`（单一源，pytest 与 unittest 两条线共用）——本文件只在
收集测试模块之前调用 `pin()`。为什么必须共用：conftest.py **只有 pytest 会加载**，
`python -m unittest discover` 看不见它，于是两条线曾给出相反结论（pytest 全绿 / unittest
178 个 KeyError 'req-board'，详见 _baseline.py 文档字符串）。

注意：需要 PROJECTS 里有其它项目名的用例，仍应按惯例用 mock.patch.object(d, "PROJECTS", ...)
在用例内自行替换——本文件只负责让「没显式替换」的用例拿到确定性基线。
"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))   # 须在 from tests import … 之前：pytest 默认只把 tests/ 放进 sys.path

from tests import _baseline  # noqa: E402


def _isolate_git_config():
    """git 全局配置隔离：测试里 `git init` 的临时仓库假定默认分支为 main 且有提交身份。
    本机 ~/.gitconfig 未必如此（CI 与新机器默认 master、无 user.name），会让 push/merge 类用例
    因「src refspec main does not match any」或「Author identity unknown」误报失败。
    用 GIT_CONFIG_GLOBAL 指向一份临时配置，测试结果不再依赖开发者的个人 git 配置。"""
    import tempfile
    cfg = Path(tempfile.mkdtemp(prefix="loop-engine-gitcfg-")) / "gitconfig"
    cfg.write_text("[init]\n\tdefaultBranch = main\n"
                   "[user]\n\tname = Loop Engine Test\n\temail = test@example.invalid\n"
                   "[commit]\n\tgpgsign = false\n", encoding="utf-8")
    os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
    os.environ.pop("GIT_CONFIG_NOSYSTEM", None)


_isolate_git_config()


@pytest.fixture(autouse=True)
def _no_supervisor_marker():
    """#8 标记隔离：钉「无标记 = launchd/未知旧路径」的测试基线。

    REQ_BOARD_SUPERVISOR 会随进程树进入测试进程（run_local 部署后 ambient 恒在，acp_shared
    Popen 全量继承）。本 fixture 每个 pytest 用例前移除、用例后还原；需要 run_local 的用例在
    mock.patch.dict(os.environ, {...}) 块内显式置值（块内可见、块尾自动还原）。
    注意：mock.patch.dict 置值/置空不会删除 ambient 变量，「无标记」场景必须靠 pop。
    unittest 单跑线（不经 pytest）由四个受影响文件模块级 pop 硬护栏覆盖（见 tests/*.py 注释）。
    """
    marker = os.environ.pop("REQ_BOARD_SUPERVISOR", None)
    yield
    if marker is not None:
        os.environ["REQ_BOARD_SUPERVISOR"] = marker


# 项目配置钉桩（实现见 tests/_baseline.py，与 unittest 线共用同一份）。
_baseline.pin()
