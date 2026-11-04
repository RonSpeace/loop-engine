# req-board API（建卡相关）

看板：http://127.0.0.1:8899（本机应用，无鉴权；`REQ_BOARD_URL` 可覆盖，worktree 测试实例用不同端口）。

## 状态语义（建卡只用 later / open）

- `later` 暂缓：只记录，调度器不捞取。
- `open` 待开发：调度器只捞这个状态，捞到即开发。
- doing / done / dropped：进行中 / 已完成 / 已废弃（建卡不传）。
- route（通道待确认）/ review（评审中）为历史值（#419 前的人工中间态）：已弃用——新流程不再产生，仅历史状态日志回放；过渡窗口内照存的原值由调度器启动回填统一转换（route → open 重走前置环节、review 按已通过推进），建卡/改卡不要使用这两个值。

## 接口

### GET /api/projects
已有项目名列表。返回 `["req-board", "my-app", ...]`。

### GET /api/auto-projects
已接入自动开发的项目名列表（来自 projects.json）。

### POST /api/requirements
新建需求。Body：

```json
{"title": "标题", "project": "项目名", "notes": "背景/方案要点/验收标准/生效方式", "status": "later",
 "source": "dsh", "model": "deepseek-v4-flash", "reasoning_effort": "max", "source_session_id": "sess-123"}
```

- `title`、`project` 必填；`status` 缺省为 `open`（自动开发项目会被调度器立刻捞走），建暂缓卡必须显式传 `later`。
- 建卡来源元数据（#303，均可空、向后兼容）：`source`（自由字符串：dsh/codex/kimi-code/board-chat…，不做显示名映射）、`model`（建卡模型）、`reasoning_effort`（推理强度，约定 low/high/max，不枚举校验）、`source_session_id`（来源会话 id）。板内聊天建卡由服务端写 `source=board-chat` + 会话模型。
- 返回 `201` + 卡片对象（含 `id` 与上述字段）。
- `append_to` 已废弃，传入返回 400。

### GET /api/requirements?project=X&status=Y
查询需求（status 可逗号分隔）。返回卡片对象数组（id/title/project/status/notes/created_at/updated_at 等）。

### PATCH /api/requirements/<id>
更新 `{title?, project?, notes?, summary?, status?, flow?, flow_name?, nodes_json?, node_index?, mode?, phase?, source?, model?, reasoning_effort?, source_session_id?}`（其它项目 agent 标记进行中/完成时用；#303 起支持更新建卡来源元数据，传 None 不更新，空串可清除；#419 新增流程快照字段 `flow`/`flow_name`/`nodes_json`/`node_index`，由调度器写入、建卡与人工一般只动 status；`mode`/`phase` 为历史只读列、过渡期保留可写）。

### DELETE /api/requirements/<id>
删除卡片；doing/done 禁止删除（400），later/open/dropped 可删。

## 约定

- 项目名必须与看板项目列表精确匹配。
- 研发流程由仓库根 `flows.json` 配置（前置槽位 + 各后置流程的节点序列），流程快照由调度器在前置环节完成后写入卡片并按快照逐节点推进；建卡不传流程字段，mode/phase 为历史字段（不再手写）。
