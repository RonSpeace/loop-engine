Continue the earlier {role} work on board requirement #{req_id} (role {role}).
User message: {user_text}
{extra}
{resume_style}
Reminder: before finishing you must write the result JSON to result.json in the artifact directory (status: done/blocked/need_confirm; contract unchanged).
Tool calls have a per-round step limit (about 200 steps); when nearing it, first write the result JSON to result.json as a fallback result (contract unchanged; may be overwritten with the final result), then continue the remaining work, so there is a result to collect if the session is cut off.