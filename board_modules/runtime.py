"""运行时兼容桥（#32 拆分）。

子模块不缓存 board 顶层可补丁名，改由 resolve() 在调用期读取 board 模块当前值——
使 patch.object(board, name) / board.name = ... 对新模块内的调用点同样生效。
board.py 启动时执行 sys.modules.setdefault("board", sys.modules[__name__])，
使 `python board.py`（__main__）与测试进程 `import board` 两条路径都解析到同一模块。
resolve() 的查找顺序（方案第 4 节）：board → __main__ → 调用方模块自身（当前模块）。
"""
from __future__ import annotations

import sys


def board_module():
    mod = sys.modules.get("board")
    if mod is not None and hasattr(mod, "__dict__"):
        return mod
    return sys.modules.get("__main__")


def caller_module():
    """resolve 调用方所在模块（「当前模块自身」回退：名字只定义在子模块里时取子模块的值）。"""
    try:
        frame = sys._getframe(2)   # caller_module ← resolve ← 调用方
    except ValueError:             # 栈深度不足（异常路径）
        return None
    return sys.modules.get(frame.f_globals.get("__name__"))


def resolve(name):
    """读取可补丁名的当前值（不缓存）。查找顺序：board → __main__ → 调用方模块（当前模块自身）。"""
    candidates = (sys.modules.get("board"), sys.modules.get("__main__"), caller_module())
    for mod in candidates:
        if mod is not None and hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError("board 模块无 %s" % name)


def bump(name, delta=1):
    """就地自增 board 模块上的计数属性并返回新值（int 无法经 resolve 写回）。"""
    mod = board_module()
    setattr(mod, name, getattr(mod, name) + delta)
    return getattr(mod, name)
