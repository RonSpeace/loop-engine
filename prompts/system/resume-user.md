继续之前看板需求 #{req_id} 的 {role} 工作（角色 {role}）。
用户消息：{user_text}
{extra}
{resume_style}
提醒：结束前必须把结果 JSON 写入产物目录 result.json（status: done/blocked/need_confirm，契约不变）。
工具调用步数有每轮上限（约 200 步），接近上限时先把结果 JSON 写入 result.json 保底（契约不变，可覆盖为最终结果），再继续剩余工作，避免会话被截断时无结果可采。