继续之前看板需求 #{req_id} 的开发工作（角色 {role}）。
{extra}
{resume_style}
若上次会话中途结束：先确认改动完好（git status 等）、产物目录无残留 result.json，再继续验证。
提醒：结束前必须把结果 JSON 写入产物目录 result.json（status: done/blocked/need_confirm，契约不变）。
工具调用步数有每轮上限（{max_steps} 步），接近上限时先把结果 JSON 写入 result.json 保底（契约不变，可覆盖为最终结果），再继续剩余工作，避免会话被截断时无结果可采。