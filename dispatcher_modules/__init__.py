"""dispatcher.py 拆出的功能模块。

约定（与 board_modules 的兼容桥同一思路）：
- 可变状态、配置与测试会替换的名字仍挂在 `dispatcher` 模块上；本包内一律经
  `runtime.D.<name>` 在调用期读取（写回同样经 `D.<name> = …`），不在模块里缓存副本。
  这样 `mock.patch.object(dispatcher, "x")` / `dispatcher.x = …` 对拆出去的代码依旧生效。
- dispatcher.py 在原位置 `from dispatcher_modules.<模块> import …` 回挂本模块定义的名字，
  外部调用方与测试继续 `import dispatcher` 使用，初始化顺序与拆分前一致。
- 本包内禁止 `global`（状态在 dispatcher 上）；需要晚绑定的名字清单见
  tests/fixtures/dispatcher_patch_names.json，tests/test_dispatcher_split_contract.py 校验。
"""
