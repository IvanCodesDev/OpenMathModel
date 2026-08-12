# 前后端与 Agent 工作台对接规范

> 版本：2026-08-11  
> 适用范围：新任务、任务确认、`/task/running`、数据准备、建模方案、实验结果、论文编辑、最终成果。  
> 事实来源：代码、JSON Schema、OpenAPI 与自动化测试；规划项单独标记为“待实现”。

## 1. 目标与非目标

本规范解决四个一致性问题：

1. 首页输入如何经登录续接直接创建真实 Project/TaskRun（`/confirm` 作为直接访问入口共用同一流程）；
2. 后端 TaskRun 当前阶段如何恢复为可导航的页面语义；
3. Agent 左栏的步骤、摘要和按钮如何与右侧页面保持一致；
4. Artifact 如何进入现有实验/成果文件表，而不破坏视觉基线。

本批次不重做页面，不把所有演示正文一次性替换为真实结果，也不把 Agent 文本当成页面结构化数据。

## 2. 当前实现清单

| 层 | 已实现 | 事实文件 |
|---|---|---|
| 契约 | `ModelingWorkspaceView` JSON Schema、Fixture、Python/TS 生成类型 | `packages/contracts/schemas/v1/modeling-workspace-view.schema.json` |
| API | `GET /api/v1/task-runs/{run_id}/workspace` | `backend/api/omm_api/routers/workspace.py` |
| 投影 | 节点→页面、页面状态、Agent 动作、Artifact 映射 | `backend/api/omm_api/workspace_view.py` |
| Web API | Project/TaskRun 创建、workspace 查询、真实 actions、错误信封、幂等键 | `apps/web/src/integration/modeling-workspace-api.ts` |
| 新任务控制器 | 草稿、直接创建、登录续接、幂等重试、demo 隔离 | `apps/web/src/integration/task-start-controller.ts`、`task-start-state.ts` |
| Web 控制器 | URL 恢复、DOM 渲染、SSE、清理、跨页身份、Artifact 文件行 | `apps/web/src/integration/modeling-workspace-controller.ts` |
| 页面接线 | `activateScreen()` 末尾挂载；现有 DOM 增加语义槽位 | `apps/web/src/legacy/openmathmodel-ui.ts` |
| 开发代理 | `/api` 默认 8000，可用 `OMM_API_PROXY_TARGET` 覆盖 | `apps/web/vite.config.ts` |

### 2.1 当前两个已接通切片

新任务切片覆盖：首页保存结构化草稿，发送时直接完成登录续接、创建 Project、以稳定幂等键创建 TaskRun，并携带 `run_id/project_id` 进入现有执行页。`/confirm` 不在首页发送链路上，保留为直接访问时的草稿复核入口，启动时执行同一套提交流程；无草稿确认页显式进入 demo，不创建后端资源。

工作台切片以**已存在且归属当前用户的 `run_id`** 为入口，覆盖：workspace 快照、六阶段状态、Agent 时间线/摘要/主操作、模型方案 Approval、SSE 通知后刷新、真实 Artifact 文件清单，以及 READY 文件下载。

附件切片覆盖：输入框拖拽/粘贴/点击三入口、浏览器内即时解析、二进制随任务创建上传为项目产物，以及服务端按需抽取的权威正文（见 2.3）。

当前尚未覆盖：五类页面详细正文契约、数据清洗确认/采用实验结果/论文完成交付等业务动作、可见暂停入口、独立 Worker 调度和完整 Skills 节点。当前详细正文继续使用页面模板，不应据此宣称 Agent 已生成真实指标、图表或论文。

### 2.2 新任务控制协议

首页草稿使用 `openmathmodel.taskDraft.v1` 保存：

```text
description / task_type / selected_model
attachments[] = name / size / type / last_modified
             + format / parse_status / characters / excerpt / artifact_id?
project_id? / run_request_token?
```

草稿只带受控长度的 `excerpt`（单文件 4000 字、合计 24000 字）：它落在 sessionStorage 里、还会随任务参数进数据库和事件流，几十万字的正文塞进去会直接把草稿写失败。完整正文留在服务端，由 2.3 的接口提供。

首页发送与确认页“开始任务”共用同一套提交流程，按固定顺序执行：

1. 校验草稿（确认页先从 sessionStorage 恢复草稿；无草稿时显示 demo 说明）；
2. `fetchMe(true)` 确认 Cookie 身份；未登录或 401 时停在当前页打开现有登录模态，成功回调重新进入同一启动函数；
3. `POST /api/v1/projects`，将返回的 `proj_<32hex>` 写回草稿；
4. 等待浏览器内解析结束后逐个 `POST /api/v1/projects/{id}/artifacts` 上传附件二进制，把返回的 `art_<32hex>` 写回草稿；已有 `artifact_id` 的跳过，因此失败重试不会重复上传，任何一个附件上传失败都中止创建；
5. `POST /api/v1/task-runs`，使用草稿内稳定 `run_request_token` 生成 `Idempotency-Key`；
6. 任务参数包含 `task_type`、`selected_model` 与 `attachment_metadata`，并按实际结果写入 `attachment_upload_state`（`none` / `uploaded` / `partial` / `metadata_only`）；
7. 校验 Project/TaskRun 身份和归属后写入 active sessionStorage，再进入运行页。

附件必须赶在第 5 步之前落地：`auto_start` 的任务一创建 Agent 就开跑，晚到的附件进不了第一轮上下文。

创建请求失败时保留草稿与已写回的项目 ID：重新发送未修改的同一份草稿视为重试，沿用 `project_id` 与 `run_request_token`，不重复创建 Project/TaskRun；修改任务内容后发送视为新提交，两个标识重置。`?demo=1` 是显式演示身份，优先于历史 sessionStorage 中的 active run。

### 2.3 附件解析：浏览器即时预览 + 服务端权威结果

两侧各司其职，**Agent 与论文环节一律以服务端结果为准**：

| | 浏览器（`apps/web/src/attachments/`） | 服务端（`omm_api/doc_text.py`） |
|---|---|---|
| 目的 | 拖进来立刻看到页数、字数与解析状态 | 供 Agent 消费的完整正文 |
| 触发 | 加入附件时 | 首次 `GET /api/v1/artifacts/{id}/text`，结果落 `artifact_texts` 长期复用 |
| 依赖 | pdfjs-dist（PDF）+ fflate（OOXML/ODF/zip 解压） | 标准库 `zipfile`/`ElementTree` + `pypdf`；旧版格式与 OCR 走可选依赖 |
| 上限 | 单文件 16MB、正文 20 万字、PDF 200 页 | 单文件 32MB、正文 40 万字、PDF 500 页 |

解析状态是五档，`empty` / `unsupported` / `failed` 同样是正常响应——调用方需要的是原因而不是错误码：

- `ready` 完整抽出；`partial` 触顶截断；
- `empty` 文件正常但没有文字（扫描版 PDF 落在这里）；
- `unsupported` 缺少可选依赖或格式不支持；`failed` 文件损坏或抽取出错。

浏览器解不动的（旧版 `.doc`/`.xls`/`.ppt`、RTF、图片 OCR、超限文件）在卡片上显示为“等待服务端解析”而不是失败。服务端的可选依赖：`legacy-docs` 附加项提供 `.doc`/`.xls`/RTF，`ocr` 附加项加上系统 Tesseract 才能识别图片；缺失时接口如实返回 `unsupported` 并说明原因。

## 3. 工作台契约

### 3.1 顶层字段

| 字段 | 含义 | 消费者规则 |
|---|---|---|
| `run_id` | 运行身份 | URL 与所有动作的主键 |
| `project_id/project_name` | 项目上下文 | 顶部项目名、跨页参数 |
| `goal` | 本次运行目标 | 后续任务详情与恢复 |
| `workflow_version` | 工作流定义版本 | 未知版本必须容忍新节点 |
| `run_status` | 生命周期轴 | QUEUED/RUNNING/WAITING_APPROVAL/PAUSED/COMPLETED/FAILED/CANCELLED |
| `active_node` | 领域阶段轴 | 与生命周期状态分开解释 |
| `active_page` | 当前页面语义 key | 由后端唯一映射 |
| `suggested_route` | 当前阶段建议路由 | 仅作为导航目标；控制器不自动跳转，也不由此执行阶段动作 |
| `agent` | 左栏 ViewModel | 文本只能按纯文本渲染 |
| `pages` | 六个页面状态 | 同时驱动时间线和右侧状态 |
| `artifacts` | 带状态的真实文件元数据 | 只有 READY 且存储引用完整时提供下载 URL；下载仍由 API 做所有权与哈希校验 |
| `pending_approval` | 当前待审批项 | 只在 PENDING 时出现 |
| `latest_event_sequence` | 已投影事件水位 | SSE 从该 sequence 之后订阅 |
| `updated_at` | 快照更新时间 | 展示与诊断，不做并发控制 |

### 3.2 Agent 字段

```json
{
  "state": "WAITING_APPROVAL",
  "title": "确认建模方案后继续实验",
  "summary": "确认后，Agent 将从当前检查点继续执行。",
  "current_step": "等待确认：确认建模方案后继续实验",
  "action": {
    "kind": "approve",
    "label": "确认并继续",
    "target_route": "/workspace/model-plan",
    "approval_id": "appr_<32hex>",
    "option_id": "approve"
  }
}
```

约束：

- `summary` 是可显示纯文本，不允许内嵌 HTML。
- `action` 是服务端允许动作，不是视觉按钮状态的猜测。
- `action` 使用按 `kind` 判别的联合契约：`navigate` 必须有目标路由且没有审批字段；`approve` 必须有目标路由和审批 ID；`none` 的路由、审批与 option 字段都必须为空。
- 只有待审批项恰好存在一个非 `reject` 选项时，后端才自动填入 `option_id`；存在多个候选时保持为空，由接入真实 `PlanProposal` 的页面显式提交用户选择，禁止静默采用第一项。
- 取消或不可操作终态使用 `none`，前端禁用按钮。

### 3.3 页面字段

页面状态枚举：

```text
PENDING | RUNNING | WAITING_APPROVAL | PAUSED | SUCCEEDED | FAILED | CANCELLED
```

固定页面 key：

```text
running | data | model | experiments | editor | complete
```

`experiments` 同时承接 `EXPERIMENTING` 与 `VALIDATING`。页面状态由当前节点、运行状态和每节点最新 attempt 共同推导，重试的旧失败 attempt 不覆盖新成功 attempt。

## 4. 节点、页面和内容映射

| workflow node | page key | 路由 | Agent 重点 | 右侧最终数据来源 |
|---|---|---|---|---|
| `CREATED` | `running` | `/task/running` | 任务已创建/排队 | TaskRun + 输入 |
| `PROBLEM_ANALYSIS` | `running` | `/task/running` | 目标、约束、子问题 | 待扩展 ProblemFrame |
| `DATA_PREPARATION` | `data` | `/workspace/data` | 数据质量与清洗 | 待实现 `DatasetProfile` |
| `MODEL_PLANNING` | `model` | `/workspace/model-plan` | 方案比较、审批 | 待实现 `PlanProposal` |
| `EXPERIMENTING` | `experiments` | `/workspace/experiments` | 运行与产物 | 待实现 `ExperimentSummary` |
| `VALIDATING` | `experiments` | `/workspace/experiments` | 指标与稳健性 | 同上 |
| `PAPER_WRITING` | `editor` | `/workspace/paper-editor` | 论文生成与检查 | 待实现 `DocumentDraft` |
| `COMPLETED` | `complete` | `/task/complete` | 交付完整性 | 待实现 `DeliveryManifest` |

未知节点回退 `running`，Agent 摘要显示节点名但页面保持可用。增加节点时先改契约/投影测试，再改页面。

## 5. 首屏恢复协议

### 5.1 运行身份解析

优先级：

```text
URL.searchParams.demo=1
  > URL.searchParams.run_id
  > sessionStorage.openmathmodel.activeRunId
  > demo 模式
```

合法格式为 `run_<32hex>`。实际分支如下：

- URL 显式 `demo=1`：进入 demo 状态，不读取历史 active run；
- URL 中存在且合法：使用 URL 值，并写入 `sessionStorage.openmathmodel.activeRunId`；
- URL 没有 `run_id` 参数：读取同一标签页中格式合法的 sessionStorage 值；
- URL 显式存在但不合法：不读取 sessionStorage，不请求 workspace API，进入 demo 状态；
- 两处都没有合法值：不请求 workspace API，进入 demo 状态。

成功快照把 `run_id/project_id` 写回 sessionStorage，并装饰流程页链接。

### 5.2 启动时序

```mermaid
sequenceDiagram
  participant Page as 现有流程页面
  participant Controller as WorkspaceController
  participant API as FastAPI
  participant Events as SSE

  Page->>Controller: activateScreen(screen)
  Controller->>Controller: 解析 run_id / 创建 AbortController
  Controller->>API: GET /task-runs/{run_id}/workspace
  API-->>Controller: ModelingWorkspaceView
  Controller->>Page: 同步渲染项目/Agent/页面状态/Artifact
  Controller->>Events: GET /events?after=latest_event_sequence
  Events-->>Controller: run/step/approval/artifact event
  Controller->>Controller: 80ms 合并刷新
  Controller->>API: GET workspace
  API-->>Controller: 新快照
  Controller->>Page: 原位更新
```

### 5.3 清理

进入其他页面或 `pagehide` 时：

- `AbortController.abort()`；
- `EventSource.close()`；
- 清除刷新定时器；
- 移除 root 捕获监听器。

同一时间只允许一个工作台控制器实例。

### 5.4 错页语义

当前浏览器路由不会因 `suggested_route` 自动跳转。若当前 `screen` 与快照的 `active_page` 不同：

1. 仍在当前页面渲染同一运行的项目名、Agent 时间线、摘要、页面状态和适用的 Artifact 清单；
2. `[data-agent-cta]` 被转换为 `navigate`，目标为 `suggested_route`；
3. 只有用户点击导航按钮后才带 `run_id/project_id` 进入目标页；
4. 错页加载与导航本身都不调用 `/actions`，不审批、不暂停、不恢复、不重试，也不推进状态机。

### 5.5 合并工作台内的阶段导航（2026-08-12，ADR-0009）

五个阶段面板同存于一个合并工作台页面，阶段间跳转是软切换而非整页导航：

- 切换由模板层 `showWorkspaceStage` 完成（面板显隐、顶栏返回键、`history.pushState` 别名 URL、标题更新）；控制器经 `omm:show-stage`/`omm:stage-shown` 事件与其协作，SSE 连接与工作台快照跨切换存活；
- 左栏时间线的非当前阶段行带 `data-go`：点击或键盘触发纯导航软切换，不调用 `/actions`；
- `agent.action` 为 `navigate` 且目标是工作台路由时软切换；目标为 `/task/running` 或首页时整页导航；
- 方案确认（`approve` 且 `option_id ≠ reject`）成功后自动软切换到实验面板——这是用户显式确认动作的延续，§5.4 禁止的仍是"加载时自动跳页"；
- `popstate` 在工作台路径之间换面板，路径离开工作台时整页导航兜底；
- 顶部返回箭头统一指向任务执行页；成果面板例外指向首页。

## 6. SSE 规则

当前事件类型：

```text
run.created
run.status_changed
run.node_changed
run.log
step.started
step.succeeded
step.failed
approval.requested
approval.resolved
artifact.published
```

- SSE `id` 等于 `AgentEvent.sequence`。
- 显式 `after` 优先于 `Last-Event-ID`。
- heartbeat 是注释 `: ping`，不进入历史。
- 终态无增量时发送 `stream.end`；Web 最后刷新一次并关闭连接。
- 事件只作为“快照可能变化”的通知，不直接把 payload 注入页面正文。

未来增加 `stage.output.updated` 时，payload 只带 `stage/version/content_hash`，完整对象仍从读接口获取。

## 7. 动作协议

### 7.1 审批

```mermaid
sequenceDiagram
  participant User as 用户
  participant Web as 现有方案页
  participant API as /actions
  participant Runner as 状态机

  User->>Web: 点击“确认并继续”
  Web->>Web: 禁用按钮，显示提交中
  Web->>API: approve + approval_id + option_id + Idempotency-Key
  API->>Runner: 解决审批并从检查点恢复
  API-->>Web: TaskRun
  Web->>API: GET workspace
  Runner-->>Web: approval.resolved / node_changed SSE
  Web->>API: GET workspace
  Web-->>User: 真实后端状态
```

失败时保持原页面，Agent 摘要区显示统一错误文案；不执行下一页跳转。

### 7.2 暂停、恢复和重试

底层 `/actions`、共享契约与 Web 控制器支持 `pause`、`resume`、`retry`，并复用幂等键。当前 workspace 投影只会在 `PAUSED` 时产生 `resume`、在 `FAILED` 时产生 `retry`；运行中不会产生 `pause`，现有页面也没有可见暂停入口。因此暂停是接口能力，不是当前工作台已经交付的用户操作。运行状态确认前不改页面步骤为成功。`navigate` 不调用后端，但必须保留 `run_id/project_id`。

API 层 `/actions` 另支持 `cancel`（按状态机校验）；当前 workspace 投影与页面主操作不暴露该动作，前端也不自行构造。

### 7.3 当前尚缺业务动作

数据清洗确认、采用实验结果、论文完成交付目前没有独立后端动作。生产接入前需决定：

- 是否作为新的 Approval decision_type；
- 是否属于具体阶段输出保存接口；
- 或继续由自动工作流推进。

在决策前，前端不自创新 action 字符串。

## 8. DOM 渲染合同

### 8.1 左侧 Agent

| 数据 | DOM | 渲染规则 |
|---|---|---|
| `pages[]` | `[data-agent-steps]` | 重建六行，状态映射图标与文字 |
| `agent.title/summary` | `[data-agent-summary]` | 使用 `textContent`，防止注入 |
| `agent.action` | `[data-agent-cta]` | 移除演示 `data-go`，绑定真实动作 |
| `project_name` | `[data-bind="project-name"]` | 原位替换标题 |

完成步骤使用当前绿色完成样式；当前、审批、暂停和失败步骤使用现有 current 样式，不另造颜色体系。

六个流程页面当前都提供 Agent 时间线与摘要槽位；`/task/running` 和五个聚焦工作台页面也都提供 `[data-agent-cta]`。主操作必须经过 `actionForScreen()`：错页只能得到 `navigate`，同页才允许消费后端投影的 `approve/resume/retry` 等动作。

### 8.2 右侧页面

工作台根节点与右侧阶段容器分别写入：

```text
[data-modeling-shell] data-active-page=<active_page>
[data-modeling-shell] data-stage-status=<viewed_page.status>
.focused-stage-pane/.modeling-stage-pane data-workspace-page=<current_screen>
.focused-stage-pane/.modeling-stage-pane data-stage-status=<viewed_page.status>
```

因此后端活动阶段与用户当前查看页面不会混为一个属性：错页时左侧仍显示全局真实进度，右侧仍保留当前模板并呈现该页面自己的阶段状态。页签仍由原有 `bindScreen()` 切换；控制器不重新创建页签或替换正文结构。

这里的“右侧渲染”目前仅指 `data-workspace-page`、`data-stage-status` 和适用的 Artifact 文件行。数据质量指标、角色化方案、实验图表、论文正文和成果摘要尚未由 `ModelingWorkspaceView` 提供。它们必须等待五类独立正文契约，不可用阶段状态属性冒充真实内容接入。

### 8.3 Artifact 文件行

真实文件行沿用 `.deliverable`：

- `data-artifact-id` 标识真实 Artifact；
- 名称来自投影 `name`；
- 类型来自 `kind`；
- 大小按 B/KB/MB/GB 格式化；
- 下载按钮使用 `data-artifact-download`。

状态行为：

- `READY` 且 `download_url` 存在：显示“可下载”，按钮可用；
- `PENDING`、`STALE`、`DELETED`：显示对应真实状态，下载按钮禁用且不设置 `data-artifact-download`；
- workspace API 仅在 Artifact 为 READY 且 `uri/sha256` 完整时返回 `download_url`。

面板分组：

| 页面面板 | kind |
|---|---|
| 实验/结果图表 | `figure` |
| 实验/结果表 | `table`, `dataset` |
| 实验/运行日志 | `log` |
| 实验/模型代码 | `code`, `model` |
| 完成/最终成果 | 全部 |
| 完成/论文文件 | `paper`, `report` |
| 完成/数据与代码 | `dataset`, `code`, `model` |
| 完成/交付记录 | `log`, `other` |

没有匹配文件时显示“该阶段尚未发布产物”，不回退到演示文件。底部“导出文件清单”导出名称、类型、状态、大小与下载地址的 TXT 清单；它不是 ZIP、归档包或多文件合并下载。

### 8.4 集成状态标记

控制器在根节点上维护机器可读的接入状态，浏览器测试应以这些属性为断言锚点：

- `[data-modeling-shell]` 的 `data-integration-state`：`loading → ready / error / demo` 四值生命周期；快照成功时同时写入 `data-workspace-source="api"`。
- `[data-task-start-root]` 的 `data-task-start-state`：`draft / ready / loading / auth-required / created / error / demo`；`data-task-start-source` 标记 `local / draft / demo` 来源。

## 9. API 投影算法

`build_modeling_workspace_view()` 每次请求：

1. 已由路由完成运行归属校验；
2. 查询全部 StepRun，按 node 选最新 attempt；
3. 查询 run Artifact，并用 producer step 反查 producer node；
4. 查询最新 PENDING Approval；
5. 查询 AgentEvent 最大 sequence；
6. 由当前节点和生命周期计算六页状态；
7. 生成 Agent 摘要与允许动作；
8. 由 Pydantic 生成类型校验响应，再交给 FastAPI response_model。

该接口不新增表，不改变状态机，不持久化派生页面状态。

## 10. 安全、错误与兼容

- workspace 与 actions 都使用同源 Cookie 身份。
- 非本人运行与不存在运行统一 404。
- Agent 文案按纯文本渲染。
- 下载仍走现有 Artifact endpoint，服务端重新计算 SHA-256。
- 未知 `workflow_version/current_node` 不使前端崩溃。
- 401/404/409/网络错误保留原页面，并在 Agent 区原位呈现。
- workspace 返回 404（运行不存在或非本人）时，控制器同时清除 sessionStorage 中的活动 `run_id/project_id`：当前页保留错误提示，其余流程页回到演示态自愈，避免本地数据库重置后整个标签页持续报错。
- URL 显式提供非法 `run_id`，或 URL 未提供且 sessionStorage 也没有合法值时，不发 workspace 请求，支持 UI 独立预览与视觉回归。

## 11. 开发环境

完整联调从仓库根启动：

```powershell
npm run dev
```

统一入口启动或复用 `127.0.0.1:8000` 的 API；健康检查必须返回成功状态码（2xx）、JSON 且 `status: ok`，随后才启动 Web。登录、账户设置与带 `run_id` 的工作台都需要 API；`npm run dev:web` 只启动 Vite，主要用于静态页面预览或与手工启动的 API 配合。Vite 参数通过 `npm run dev -- --host <HOST> --port <PORT>` 传入。

默认 Vite 代理：

```text
/api → http://127.0.0.1:8000
```

隔离联调可设置：

```powershell
$env:OMM_API_PROXY_TARGET='http://127.0.0.1:8010'
npm run dev --workspace @openmathmodel/web -- --port 5175
```

该变量只影响开发/预览代理，不进入浏览器构建产物。

统一入口只会自动启动本机 loopback HTTP API；`OMM_API_PROXY_TARGET` 指向远程或非 loopback 地址时，目标必须已经健康，脚本不会在本机冒充该服务。

开发/预览服务器还内置两个中间件，均不进入生产构建产物：

- `GET /api/account/me`：无会话 Cookie 或 API 不可达时由 Vite 直接降级返回 401 访客态 JSON，不产生代理错误。单独运行 `npm run dev:web` 时页面因此显示访客而不是报错；调试登录问题时先确认 API 是否在运行。
- `/paper-files/<年份>/<题组>/<文件>.pdf`：优先读取本地 `datasets/raw/sources/github/zhanwen-MathModel/papers` 缓存，未命中再按固定 revision 回源 GitHub raw。论文阅读页依赖该路由；脱离 Vite 的静态部署需要另行提供同路径静态服务。

端到端诊断顺序固定为：

```text
GET /api/health
  → 登录并保留 Cookie
  → POST /api/v1/projects
  → POST /api/v1/task-runs（记录返回的 run_id）
  → GET /api/v1/task-runs/{run_id}/workspace
  → 在现有页面 URL 携带 run_id/project_id
```

可直接复制的 PowerShell 请求见[根 README 的“用真实 run_id 验证工作台”](../../README.md#4-用真实-run_id-验证工作台)。若健康检查失败，先修复 API 启动；Vite 代理错误不作为页面渲染问题处理。

## 12. 验收要求

| 层 | 用例 | 预期 |
|---|---|---|
| Schema | valid fixture | JSON Schema 通过 |
| 生成物 | TS/Python check | 与 Schema 完全同步 |
| API | 待审批 workspace | active=model，action=approve |
| API | 完成 workspace | 六页完成，含实验/论文 Artifact |
| API | 跨账户 | 404 |
| Web | 首页发送真实启动 | 登录续接后 Project/TaskRun 只创建一次，直接进入执行页且 URL 携带合法 `run_id/project_id`；创建过程中发送键禁用 |
| Web | 首页未登录发送 | 停在首页并打开现有登录模态；登录成功后续接同一次创建 |
| Web | 失败后重复发送 | 未修改内容时沿用 `project_id` 与幂等 token，不重复创建；修改内容后视为新提交 |
| Web | 确认页直接访问 | 草稿、任务类型、模型与附件元数据恢复；“开始任务”走同一提交流程且重试不重复创建 |
| Web | 无草稿确认页 | 显式 demo，不请求创建接口、不复用旧 active run |
| Web | workspace 404 | 当前页原位报错并清除活动运行；其余流程页回到演示态 |
| Web | URL 与 sessionStorage 都无合法 run_id | demo 状态，无 API 请求错误 |
| Web | URL 显式非法 run_id | 不回退 sessionStorage，保持 demo |
| Web | 待审批 run | 六阶段真实时间线，按钮可提交 |
| Web | 点击审批 | 后端动作成功，SSE/快照刷新 |
| Web | 错页打开 run | 只提供导航；不自动跳页、不请求 `/actions`、不改变运行状态 |
| Web | 跨页 | 查询参数保留 |
| Web | 工作台内切换阶段 | 无整页重载；URL、标题与投影同步更新 |
| Web | 浏览器后退/前进 | 工作台面板间穿梭不重载；离开工作台整页导航 |
| Web | 方案确认成功 | 自动软切换至实验面板 |
| Web | 论文页 | 只保留大纲、工具栏、正文 |
| Web | 完成页 | 真实 Artifact 或空状态；仅 READY 可下载 |
| Web | 导出文件清单 | 生成 TXT 清单，不生成压缩包 |
| Build | typecheck/check/build | 全部退出码 0 |

## 13. 当前已验证证据

> 记录日期：2026-08-11；2026-08-12 增量见本节末尾。标题不携带日期，保证跨文档锚点稳定。

以下命令/测试已经实际执行并通过：

| 证据 | 结果 |
|---|---|
| `npm run check --workspace @openmathmodel/contracts` | 通过 |
| `npm run check --workspace @openmathmodel/web` | TypeScript 与 ESLint 通过 |
| `npm run build --workspace @openmathmodel/web` | 生产构建通过，Vite 转换 88 个模块 |
| `python packages/contracts/validate.py` | 8 个 Schema、28 个 Fixture 通过 |
| `python packages/contracts/check_compat.py` | 兼容性检查通过 |
| `python packages/contracts/scripts/export_openapi.py --check` | OpenAPI 基线通过；公共 `Timestamp` component 保持兼容 |
| `pytest backend/api/tests -q` | 66 个用例通过；其中 workspace 覆盖排队、待审批、动作字段不变量、审批选项歧义、完成聚合、非 READY 与存储可读性、跨项目异常关联、跨账户与 OpenAPI 兼容（2026-08-12 增至 75 个，见下） |
| `node --test apps/web/src/integration/task-start-state.test.mjs` | 4 个新任务状态纯函数用例通过：草稿解析、输入归一化、项目名派生、运行 URL |
| 新任务相关 API 回归 | Project、TaskRun 幂等创建和 workspace 投影共 14 个相关用例通过 |
| 关键流程人工浏览器验收 | 首页→确认、草稿恢复、访客登录拦截、无草稿 demo 隔离，以及 `/task/running`、方案审批、跨页身份、论文页、完成页与真实 Artifact 行通过；控制台无错误 |

这些证据验证了契约、类型、生产构建、任务启动状态函数、workspace API 核心投影及关键人工流程。真实登录浏览器中的最终 Project/TaskRun 写入尚未在本轮 IAB 会话落库；两个创建接口、幂等语义和 workspace 投影已有后端回归覆盖。Web 控制器 DOM、SSE 重连、错页导航、其余 URL/sessionStorage 分支与非 READY 下载禁用态仍缺自动化浏览器覆盖，全部 14 条路由的自动视觉回归也尚待补齐。

### 2026-08-12 增量

行为与代码变化：

- 首页发送链路由「跳 `/confirm`」改为「直接创建并进入执行页」；`/confirm` 退出首页发送链路，保留为直接访问的草稿复核入口，与首页共用同一套提交流程；
- 修复重试幂等：失败后重新发送未修改的草稿（首页）或再次点击“开始任务”（确认页）沿用已写回的 `project_id` 与 `run_request_token`，不再重复创建；任一内容变化即重置两个标识，避免旧幂等键携带新内容触发 409；
- 修复 workspace 404 自愈：清除 sessionStorage 活动运行，当前页原位报错、其余流程页回到演示态；
- 被中止的动作请求不再向 Agent 摘要区渲染错误文案；
- `agent-event.schema.json` 描述由“PostgreSQL 事件表”修正为“数据库事件表（当前默认 SQLite，目标部署 PostgreSQL）”，并重新生成 TypeScript/Python 模型与 OpenAPI 基线。

当日重跑并通过：契约 check、Web check、Web 生产构建（88 个模块）、`validate.py`（8 Schema/28 Fixture）、`check_compat.py`、`export_openapi.py --check`、`pytest backend/api/tests`（66 个用例）、`node --test` 新任务状态用例（4 个）。

尚未重新验收：第 12 节中“首页发送真实启动”“首页未登录发送”“失败后重复发送”“确认页直接访问”“workspace 404”等浏览器用例需要在真实浏览器中重新执行；上表 2026-08-11 的“首页→确认”人工验收记录对应旧链路，不再代表当前行为。

### 2026-08-12 账户头像

设置中心「账户与安全 → 编辑资料」支持更换和移除头像：

- 后端新增 `POST/DELETE/GET /api/account/avatar`；内容按 sha256 存入独立头像目录，`users` 表只存引用；格式按文件魔数判定（PNG/JPEG/WebP/GIF），读取按当前会话返回本人头像并带 `nosniff`；`user.avatar_url` 携带摘要查询串做缓存失效。
- 前端交互在账户与安全面板的头像本身：鼠标悬停（或键盘聚焦）时头像浮出相机图标，点击直接选图，本地居中裁剪压缩到 256×256 后立即上传并就地预览，完成后以服务端快照收尾；已设置头像时身份区提供“移除头像”。编辑资料弹窗保持原有的名称/邮箱/密码三项，不承载头像。侧栏、设置账户卡与安全面板三处头像统一由 `avatar_url` 渲染，未设置时回落姓名首字母。
- SQLite 开发库在启动时补齐模型新增可空列，已有 `dev.db` 无需删库（详见 [API README](../../backend/api/README.md#设计要点)）。

当日执行并通过：`pytest backend/api/tests`（75 个用例，含 8 个头像用例与 1 个开发库补列用例）、`npm run check --workspace @openmathmodel/web`、`npm run check --workspace @openmathmodel/contracts`、`npm run build --workspace @openmathmodel/web`、`export_openapi.py --check`（基线已随新路由刷新）、对真实 uvicorn 实例的 12 项 HTTP 链路检查（注册→上传→读取→越权→移除，含伪装成 PNG 的 SVG 被拒）。

尚未验收：换头像的浏览器视觉走查（悬停遮罩、三处头像同步、暗色主题、侧栏折叠态）需在真实浏览器中执行并留存截图。

### 2026-08-12 高级设置：做实两项、如实标注四项

设置中心「高级设置 · 网络与运行」原有六个控件全部只有外观。本轮按可行性分级处理：

- **做实「请求超时」**：新增 `apps/web/src/preferences/network-preferences.ts` 读取器（默认 120 秒，夹紧到 5–600 秒），工作台客户端与账户客户端的全部 JSON 请求统一挂 `AbortSignal.timeout`，超时报出可操作的中文提示；SSE 长连接与附件上传刻意豁免。
- **做实「最大并发任务」**：上限存服务端而非浏览器——`users` 表新增可空列 `max_concurrent_runs`（迁移 0009，NULL＝默认 3），新增 `GET/PUT /api/account/preferences`；创建任务时后端只统计**排队与执行中**的运行（等待审批/已暂停不占位），超限返回 409 `CONCURRENCY_LIMIT`。前端打开设置面板用服务端值回填显示，保存时异步推送，未登录时提示登录后生效。
- **如实标注其余四项**：代理两项注明"将随模型服务接入后生效"（后端目前没有任何出站调用）；下载目录与临时文件目录是桌面端形态，网页版禁用并注明由浏览器/部署配置管理。

当日执行并通过：`pytest backend/api/tests`（97 个用例，含 9 个偏好与并发闸门用例）、`export_openapi.py --check`（基线随新路由刷新）、`npm run check --workspace @openmathmodel/web`、`npm run build --workspace @openmathmodel/web`、`node --test`（34 项，含超时读取器与并发解析 7 项）。

尚未验收：高级设置面板的浏览器走查（提示文案排版、禁用态样式、并发上限回填与 409 提示、暗色主题）。

### 2026-08-12 界面英文与桌面通知

设置中心的「界面语言」与「桌面通知」两个此前只有外观的控件已经真正生效：

- 界面语言新增 `apps/web/src/i18n/`（词典 + DOM 适配层 + locale 存取），策略与边界见 [ADR-0008](../adr/0008-interface-localization.md)。切换即时生效、未保存关闭即还原、启动前应用、`<html lang>` 同步；真实数据与 `contenteditable` 正文不参与翻译。
- 桌面通知新增 `apps/web/src/notifications/desktop-notifications.ts`，接入工作台快照的状态变化：待确认、完成、失败各提醒一次，首屏不提醒，用户正注视页面时不打扰；权限在开关的点击手势里申请，被拒绝时开关自动拨回。

当日执行并通过：`node --test apps/web/src/i18n/en-US.test.mjs`（5 项词典门禁：非空、键已修剪、无原样返回、无残留中文、无重复键）、`npm run check --workspace @openmathmodel/web`、`npm run build --workspace @openmathmodel/web`。词典对已抽取的 1232 条界面文案覆盖 1202 条（97.6%）；未覆盖的 30 条是源码注释与由变量拼接的提示词片段，其中工作台运行元信息一行已改为逐段 `t()`。

尚未验收：中英切换与桌面通知的浏览器走查（见 [Web 页面基线 §10.3](./web-ui-baseline-and-api-integration.md#103-浏览器)）。方法库与知识库的中文内容数据按 ADR-0008 保持原语言，不属于漏译。

### 2026-08-12 外观与显示：删两项、做实三项

设置中心「外观与显示」原有六个控件，其中只有主题真正生效。本轮按产品判断做了取舍：

- **删除**「界面密度」与「代码字体」。前者是持续税——每新增页面都要维护三档间距；后者是从编辑器类产品照搬的选项，在任务型工具里几乎无人使用。分区标题相应从「显示密度」改为「正文与可读性」，侧栏副标题也同步修正，避免文案继续宣传已不存在的功能。
- **做实**「正文字号」「减少动态效果」「增强文字对比度」。新增 `apps/web/src/accessibility.css`（表现）与 `apps/web/src/preferences/display-preferences.ts`（状态），在 `main.tsx` 中最后引入；两张受保护样式表零改动，仅被叠加覆盖。状态以 `--omm-text-scale`、`data-reduce-motion="on"`、`data-contrast="high"` 写在 `<html>`，与主题 `data-theme` 同一机制，并同样支持即时预览、未保存还原、启动应用。

三项的实现边界：

- 减少动效**无条件跟随系统 `prefers-reduced-motion`**，应用内开关只加强、不取消系统偏好；过渡时长压到 `0.001ms` 而非 `none`，`transitionend`/`animationend` 仍会触发，依赖这些事件的交互不受影响。
- 正文字号作用于继承 `body` 的阅读文本，外加 Agent 摘要与论文编辑器正文两处硬编码 px 的主要阅读面；标题、标签、徽章保持设计尺寸（与 Slack、Notion 的字号设置同一取向）。滑块基准由 15 修正为 14，与样式表实际基准一致。
- 增强对比度修正的是真实缺陷：默认调色板中 `--faint`(#a1a19d) 与白底对比度约 2.6:1、`--muted`(#777773) 约 4.5:1，均未达 WCAG AA 对正文的 4.5:1 要求。开关打开后重定义 `--ink/--muted/--faint/--line`，浅色与暗色各一套。

当日执行并通过：`node --test apps/web/src/preferences/display-preferences.test.mjs`（5 项取值规范化用例，其中一项当场抓出空字符串被 `Number("")` 变成 0 再夹成最小字号 13px 的缺陷并已修复）、`node --test apps/web/src/i18n/en-US.test.mjs`（5 项）、`npm run check` 与 `npm run build --workspace @openmathmodel/web`，并确认三项的 CSS 均已进入构建产物。

尚未验收：三项的浏览器走查，以及暗色主题下高对比度的观感。

补记：2026-08-12 下午曾把「正文字号」改为基于 `#root` zoom 的「界面缩放」，当日按产品决定回滚，恢复上述正文字号方案；`--omm-text-scale`、设置文案与 JS 定位逻辑均已还原，无残留。

### 2026-08-12 任务与文件：做实三个开关

设置中心「任务与文件」的三项此前只是落盘的布尔值，本轮全部接上真实行为。读取器集中在 `apps/web/src/preferences/task-preferences.ts`，与面板初始状态一致（未保存过设置视为开启），每次使用时重新读取，改动后无需刷新即可生效。

- **自动保存任务**（`autoSave`）：`apps/web/src/tasks/task-autosave.ts`，`activateScreen` 末尾挂载。工作台六屏（running/data/model/experiments/editor/complete）每 30 秒落盘两类现场：论文编辑器正文（localStorage，按 `project_id` 区分，跨会话），工作台输入框未发送的对话草稿（sessionStorage，按屏幕与 `run_id` 区分）；`pagehide` 时补一次落盘，回到页面时自动恢复，编辑器顶栏芯片显示「已自动保存 HH:MM」。首页输入框不归它管——新任务草稿在 task-start-controller 里逐键即时保存。
- **启动时恢复上次任务**（`restoreSession`）：workspace 每次成功渲染真实运行时把 `run_id/project_id` 写入 `openmathmodel.lastTask.v1`（`tasks/last-task-record.ts`），运行 404 时清除；`main.tsx` 在本标签页会话的首次加载落在 `/` 时读取记录并 `location.replace` 到运行工作台（`tasks/restore-last-task.ts`），已跳转则跳过首页渲染。同会话内再回首页不拦截（sessionStorage 一次性标记）。
- **自动解析上传文件**（`autoOpenFiles`）：`attachments/store.ts` 的解析入口按开关短路——关闭时不在浏览器里抽取内容，附件直接置为「等待服务端解析」，`settled()`、上传与草稿流程不受影响，服务端解析后仍出权威结果。

当日执行并通过：`node --test apps/web/src/tasks/last-task-record.test.mjs`（3 项记录解析用例）、`npm run check --workspace @openmathmodel/web`、`npm run build --workspace @openmathmodel/web`。

尚未验收（需浏览器）：编辑论文正文离开再回来是否恢复并显示保存时间；新标签页打开 `/` 是否直达最近任务、点击 Logo 回首页不再被拦；关闭自动解析后附件卡片是否显示「等待服务端解析」且发送流程正常。

### 2026-08-12 下线主题切换

深色主题长期只覆盖 styles.css（约 320 条规则），workflow-refresh.css 的聚焦工作台五个页面从未有深色规则，夜间模式实际不可用。当日下午曾尝试以补全皮肤（theme-dark.css，约 700 行）修复，因整体配色质量不达标、且每个新组件都要持续维护深色对应，按产品决定改为**整体下线主题切换功能**，界面固定浅色：

- 设置中心「外观与显示」移除「界面主题」分区，侧栏副标题同步改为「正文字号与可读性」；正文字号、减少动效、增强对比度三项保留不变。
- 移除 `applyTheme/normalizeTheme/savedTheme` 与主题的保存/回填/即时预览/恢复默认逻辑；`openmathmodelSettings` 里历史残留的 `theme` 键读取时静默跳过。`theme-dark.css` 已删除。
- 保留而未清理的部分：styles.css 与 attachments.css 中既有的 `html[data-theme="dark"]` 规则成为不可达死代码（受保护基线，未做大规模删除）；`renderCharts` 的按主题取色分支保留（`data-theme` 永不再置位，恒走浅色）。如后续重启深色主题，从这两处加上 git 历史里的 theme-dark.css 可恢复。
- `initInterfaceLocale` 之外不再有主题相关启动逻辑；增强对比度的暗色变体选择器（accessibility.css）同样不可达，保留。

当日执行并通过：`node --test`（en-US 词典 5 项、显示偏好 5 项、任务记录 3 项）、`npm run check --workspace @openmathmodel/web`、`npm run build --workspace @openmathmodel/web`。

尚未验收（需浏览器）：设置中心外观分区只剩可读性三项、保存/恢复默认不再改动主题、历史保存过深色偏好的浏览器打开后界面为浅色。

### P1：新任务控制链（已落地，继续补端到端自动化）

- 首页与确认页已使用现有 DOM 创建 Project/TaskRun；
- 草稿携带任务类型、模型选择和附件元数据；
- 返回的 `run_id/project_id` 已进入任务执行页；
- 401 已打开现有登录模态并在成功后恢复动作；
- 后续补充真实浏览器登录后的创建断言、重复点击幂等断言和二进制附件上传。

### P2：阶段输出持久化

- 增加 StageOutput 版本、哈希、来源 Artifact 与 producer step；
- 保留历史版本；
- `STEP_SUCCEEDED` 不再丢弃 outputs/metrics；
- SSE 增加轻量 output-updated 指针事件。

### P3：五类页面正文

按 `DatasetProfile → PlanProposal → ExperimentSummary → DocumentDraft → DeliveryManifest` 顺序实现。每类都要先发布 Schema、Fixture 和生成类型，再接 API 与 DOM。

### P4：真实执行面

- 用节点注册表替换 `SIM_NODES`；
- 补齐 DATA/EXPERIMENT/VALIDATION/PAPER 技能；
- 统一 API 与 `backend/worker` 的队列、租约、事件和 Artifact Store；
- 保持本规范的 workspace 接口与页面合同稳定。
