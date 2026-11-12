
Pre-stage output contract (this stage is a slot shared by all downstream flows; the requirements are role-independent and enforced by the dispatcher):
Before finishing, write the result JSON to {result_json}; besides status/summary it must include:
- "suggested_flow": the recommended downstream flow id (pick the **cheapest sufficient** flow from the options below; if missing/invalid, the dispatcher falls back to the default flow {default_flow} and logs it, without blocking)
- "suggested_flow_reason": one sentence on which applicability scenario of the chosen flow was matched (≤200 characters, do not restate the requirement; optional)
- "repos": array of repo directory names this requirement actually changes (names must come from the repo list; include repos that only need reading, not changes; omit if they cannot be identified)
Available flows (id (name) | applicability | node sequence):
{flow_catalog}
Note: if repos contains a directory name outside the repo list, it goes to a human for confirmation (possibly filed under the wrong project) and does not enter development directly.
