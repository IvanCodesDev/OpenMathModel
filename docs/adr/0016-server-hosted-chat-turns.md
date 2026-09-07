# ADR-0016：对话生成改为服务端托管的后台作业，记录落库

- 状态：Accepted
- 日期：2026-09-05
- 关联：ADR-0013（终态可返工）、[前后端与 Agent 对接日志 §5.6](../development/frontend-backend-agent-integration.md)、[Web 页面基线与前后端对接规范](../development/web-ui-baseline-and-api-integration.md)

## 背景

任务页与首页的对话此前走 `POST /api/chat`：服务端是一个无状态的 SSE 代理，把上游模型的增量转发给浏览器，不落库；对话记录只存浏览器 `localStorage`（`tasks/conversation-log`），是否写入由「数据与隐私 → 保存任务历史」决定。

这套结构有两个绑在一起的缺陷，2026-09-05 用户两次报障都指向它们：

1. **生成会被页面导航掐断。** 侧栏「最近任务」、任务页与阶段页之间是整页跳转，页面卸载时浏览器中止 `fetch`，Starlette 随之取消生成器、关闭上游流——模型没生成完就停了。用户「提问 → 思考中 → 切到别的任务 → 回来」看到的是一句「回复在离开页面时中断」，而不是回复。
2. **记录不可靠。** 浏览器存储不是产品数据：换设备/换浏览器/清缓存都没有，多标签页互相看不到，也没有服务端的一致性保证。

上一轮修复只做到「把半截正文和一句中断说明落到 localStorage」（§3.7.6），用户明确驳回：「不能光靠一句子来解决。你数据没有存好也没有做到持续性。」产品裁定：生成不能因为页面离开而中断，记录必须存服务端。

## 决策

### 1. 一轮对话 = 服务端的一条 `chat_turns` 记录 + 一个后台作业

新增表 `chat_turns`（迁移 `0019_chat_turns`）：`id`（`cturn_…`）、`user_id`、`scope_id`（`run_…` 任务运行或 `chat_…` 首页对话）、`status`（`running / completed / failed / stopped / interrupted`）、`opening`（是否系统自动发起的开场分析）、`text`、`attachments`、`reply`、`reasoning`、`meta`（实际接口/模型/Auto 路由/用量/耗时）、`error_code / error_message`、`trace`（页面侧执行轨迹行）、`created_at / updated_at / ended_at`。索引 `(scope_id, created_at)`。

`ChatTurnHub`（`omm_api/chat_turns.py`，进程内单例，与 `RunnerThread` 同样假定单进程 API）负责每一轮的生命周期：

- `start` 建行（`running`）后在**守护线程**里跑 `llm.stream_events`，事件按 `seq` 顺序缓存在内存；正文与思考每秒批量回写数据库；
- 终态后事件缓冲保留 10 分钟供迟到的观众续接，之后只剩数据库里的定格记录；
- `stop` 立即定格（`stopped`，保留半截正文），工作线程随后丢弃上游迟到事件并关闭连接；
- 服务启动时 `recover_interrupted()` 把上次进程留下的 `running` 行标成 `interrupted`——服务重启是唯一会真正中断生成的情形，记录里如实写明。

### 2. 页面只是观众：建轮即返回，SSE 附着直播，断了再接

```
POST   /api/chat/turns                      → 202 {turn}      建轮（body = ChatRequest + scope_id/text/opening/attachments）
GET    /api/chat/turns/{id}/events?after=N  → SSE              从 seq N 之后续播（id: seq / data: {...,"seq"}）
POST   /api/chat/turns/{id}/stop            → {turn}           停止生成（保留半截）
GET    /api/chat/turns/{id}                 → {turn}
PATCH  /api/chat/turns/{id}                 → {turn}           补写页面侧执行轨迹行 {trace}
GET    /api/chat/scopes/{scope}/turns       → {items}          该归属全部轮（running 的带半截正文与 last_seq）
DELETE /api/chat/scopes/{scope}             → {deleted}        删除该归属全部轮（运行中的先停）
```

- 事件契约与 `/api/chat` 同形（`meta → delta*/reasoning* → done | error`），只多一个 `seq`；非直播中的轮请求 events 时服务端合成唯一一个终态事件（`done` / `done{stopped}` / `error GENERATION_STOPPED|GENERATION_INTERRUPTED|…`）。
- 同一归属同时只允许一轮 `running`（409 `CHAT_TURN_IN_PROGRESS`）。`run_…` 归属校验运行归属权，`chat_…` 归属按用户隔离。
- 页面侧（`integration/agent-chat.ts`）：有归属的对话一律走托管轮——`startChatTurn` 拿到轮 id 后再附着 `events`；`AbortSignal` 触发的是服务端 `stop`（暂停键真的停生成），而不是本页不看了；SSE 断线按 `after=seq` 重连，重连失败则拉一次轮视图补齐缺失正文。
- 重进页面：控制器在首次渲染工作台**之前**拉齐 `GET /scopes/{run}/turns` 并广播 `omm:conversation-restore`，页面层按时间顺序重建全部轮，`running` 的那一轮在原位续接直播（半截正文/思考先上屏，随后接实时增量）。首页对话（`restoreHomeChat`）同理。
- 没有归属的页面（演示态）仍走无状态 `POST /api/chat`，行为不变。

### 3. 「保存任务历史」的语义

- 开启（默认）：轮落库；关闭：轮只在服务端内存里托管（`persist=false`），进程内保留同样的 10 分钟直播窗口，重启即无——继续保证「切页不中断」，只是不留记录。
- 由开到关时服务端删掉该用户全部 `chat_turns`（`PUT /api/account/privacy-settings`），与既有「清空本机记录」联动。
- 删除项目 → 级联删掉其运行的轮（`privacy._delete_runs`）；删除首页对话 → 前端先 `DELETE /scopes/{chat}` 再清本机目录。

### 4. 本机 localStorage 记录降级为只读兜底

`tasks/conversation-log` 不再写入新条目；重进任务时先渲染其中的旧记录（托管轮上线前的历史），再接服务端的轮。上一轮引入的「在途轮」（`chatPending`）机制与 `pagehide` 落定整体移除。

## 结果

- 生成中切任务、刷新、关标签页、换标签页打开，回来看到的都是完整回复或仍在进行的直播；暂停键真正停止服务端生成；服务重启是唯一会中断生成的情形，记录里如实标注。
- 对话记录成为服务端数据，随任务/项目删除级联，随「保存任务历史」开关整体清空；多标签页看到同一份。
- 不改任何页面结构、路由与 DOM 槽位；旧 `/api/chat` 保留供演示态与旧客户端使用。
- 代价：API 进程内多了一类后台线程与内存缓冲（每轮事件上限 20 万条、终态后 10 分钟释放）；单进程假定与 `RunnerThread` 一致，多进程部署前需要把事件缓冲外移。

## 验收要求

1. 任务页追问 → 「思考中」→ 点侧栏其他任务 → 点回来：看到该轮的完整回复，或仍在直播的半截 + 后续增量；刷新页面同理；同一运行在另一标签页打开也能看到。
2. 生成中点暂停：服务端轮变 `stopped`，半截正文保留，`GET /events` 立即返回 `done{stopped:true}`；此后不再产生用量记录。
3. 关闭「保存任务历史」：库中该用户 `chat_turns` 清空，新一轮不落库但切页仍不中断。
4. 删除项目 / 删除首页对话后，对应归属 `GET /scopes/{scope}/turns` 为空。
5. 服务重启后遗留的 `running` 轮变 `interrupted`，页面显示半截正文与「服务重启，本轮生成中断」说明。
6. `pytest backend/api/tests`（含 `test_chat_turns.py`、`test_migrations.py`）、`npm run check`、`npm run build`、`node --test` 通过。
