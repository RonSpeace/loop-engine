---
name: browser-verify
description: 改了 HTML/JS/CSS、请求头、表单交互或页面渲染时使用：浏览器级验证的做法，以及自测报告「浏览器验证」小节与呈现断言的写法。
roles: [dev]
activation: always
paths: ["static/**", "templates/**", "**/*.html", "**/*.css", "**/*.js"]
keywords: [页面, 样式, 浏览器, 前端]
required: false
owner: loop-engine
status: active
---

浏览器验证（自测报告必须单列「浏览器验证」小节）：
浏览器端改动（diff 涉及内嵌 HTML/JS/CSS、fetch/HTTP 请求头、表单交互、页面渲染逻辑等影响浏览器行为的改动）必须单列「浏览器验证」小节：单元测试、curl 端到端、JS 语法检查都不构成浏览器级验证，不得声称「已浏览器验证」——
先确认本会话工具列表里是否真有浏览器工具（mcp__playwright__browser_*，放行清单内）。有则必须真开浏览器走主路径：起测试实例（http://127.0.0.1:<测试端口>）→ browser_navigate 打开页面 → 关键交互（browser_click/browser_type/browser_fill_form 等按场景）→ browser_console_messages 确认 console 无报错 → 把测试实例地址/操作步骤/结果写进该小节；没有（前缀已放行但本机未注入 MCP 服务）时改用项目自带浏览器驱动（项目 Playwright/Chrome headless、CDP 脚本等）完成同样的主路径验证，并在小节写明驱动、版本与命令。`file://` 参考页不能用浏览器工具打开（审批仅放行 http(s)），参考页用脚本渲染或读源码对照。
改动涉及呈现层（颜色/图标/标签文案/状态样式等「同一元素的展示语义」，或验收标准里含此类要求）时，该小节还必须含至少一条**可判定的呈现断言**：要么 browser_take_screenshot 实际看过形态并写明「看到了什么、是否符合预期」，要么按项目约定的渲染断言方式对渲染结果做断言并引用用例名；只写「渲染正常」「显示正确」不算断言。改了某个标签/徽标的语义（来源、优先级、取值）时，同一元素上其它展示属性（颜色/图标/边框）是否还绑在旧字段上，必须一并核对并写明结论。
工具确实不可用（本地无浏览器环境/工具故障），或项目约定未提供测试实例时，该小节如实写「未做，需人工验收」，并在 result.json 写 todos 声明（如 "todos": ["浏览器打开 xx 页面，验证 xx"]），两处必须一致。
