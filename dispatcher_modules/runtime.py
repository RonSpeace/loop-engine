"""dispatcher 拆分的晚绑定桥。

`D.<name>` 在调用期读写 dispatcher 模块上的名字。`python dispatcher.py` 以 __main__ 运行时，
dispatcher.py 开头执行 `sys.modules.setdefault("dispatcher", sys.modules[__name__])`，
两条启动路径解析到同一个模块对象。

`log` / `subprocess` / `threading` 是属性转发代理：写法与原来一样（`log.info(...)`、
`subprocess.run(...)`），但每次取属性都读 dispatcher 上的当前对象——测试会整体替换这三个名字。
"""
from __future__ import annotations

import sys


def dispatcher_module():
    mod = sys.modules.get("dispatcher")
    if mod is None:
        mod = sys.modules["__main__"]
    return mod


class _Dispatcher:
    """dispatcher 模块命名空间的晚绑定视图。"""
    __slots__ = ()

    def __getattr__(self, name):
        return getattr(dispatcher_module(), name)

    def __setattr__(self, name, value):
        setattr(dispatcher_module(), name, value)

    def __repr__(self):
        return "<dispatcher late-binding view>"


D = _Dispatcher()


class _Forward:
    """把属性访问转发到 dispatcher.<name> 的当前对象。"""
    __slots__ = ("_name",)

    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attr):
        return getattr(getattr(dispatcher_module(), self._name), attr)

    def __repr__(self):
        return "<dispatcher.%s>" % self._name


log = _Forward("log")
subprocess = _Forward("subprocess")
threading = _Forward("threading")
