Continue the earlier development work on board requirement #{req_id} (role {role}).
{extra}
{resume_style}
If the previous session ended midway: first confirm the changes are intact (git status etc.) and no leftover result.json is in the artifact directory, then continue verifying.
Reminder: before finishing you must write the result JSON to result.json in the artifact directory (status: done/blocked/need_confirm; contract unchanged).
Tool calls have a per-round step limit ({max_steps} steps); when nearing it, first write the result JSON to result.json as a fallback result (contract unchanged; may be overwritten with the final result), then continue the remaining work, so there is a result to collect if the session is cut off.