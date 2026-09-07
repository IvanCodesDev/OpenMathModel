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

浏览器解不动的（旧版 `.doc`/`.xls`/`.ppt`、RTF、图片 OCR、超限文件）在卡片上显示为“等待服务端解析”而不是失败。服务端的可选依赖与配置：`legacy-docs` 附加项提供 `.doc`/`.xls`/RTF；图片与扫描件 PDF 的识别走远程 OCR（讯飞星辰 MaaS 上的 PaddleOCR，OpenAI 兼容协议，配置 `OMM_OCR_API_KEY` 启用；本地 paddle 栈已于 2026-08-30 移除），输出逐页 Markdown（公式→LaTeX、表格→表格标记，`engine="paddleocr-api"`），扫描件 PDF 另需 `pdf-ocr` 附加项（pypdfium2 逐页渲染）；未配置 key 时图片回落 `ocr` 附加项 + 系统 Tesseract。缺失时接口如实返回 `unsupported`/`empty` 并说明原因。

### 2.4 图片计数与单模态提醒（ADR-0010 批次一）

正文抽取只拿得到文字层，文档里的图对纯文本模型是不可见的。两侧解析因此都统计**图片数**并如实展示：

- 服务端 `Extraction.images` / 接口字段 `images`（`artifact_texts.images` 缓存列）：PDF 按页扫 `/Resources//XObject` 数 `/Image` 并按间接引用去重（Form 不递归，近似值）；docx/pptx 数 `word/media/*`、`ppt/media/*`（精确）；独立图片附件恒为 1；其余格式与计数失败为 `null`。计数绝不拖垮正文抽取。
- 浏览器侧 `ParseOutcome.images`：PDF 为字节扫描 `/Subtype /Image` 的近似值（压缩对象流内的字典扫不到，卡片显示「约 N 张图」），OOXML 按 media 条目精确计数；随草稿进入任务参数（`TaskAttachmentDraft.images`）。
- **单模态提醒**：附件含图且生效模型判定为纯文本时，composer 附件托盘内显示提醒行（`integration/model-modality.ts` 按模型名模式分类；`auto`/`endpoint-<id>` 经 llm-config 解析主接口模型；未登录、未配置或模态 unknown 一律沉默——宁可漏报不可误报）。

后续批次（视觉解析后端、消息桥 image 内容块）见 [ADR-0010](../adr/0010-attachment-modality-awareness.md)。

### 2.5 对话附件：即席解析并入消息（ADR-0010 批次三）

任务页对话区输入框的附件走独立于 §2.2 的轻量链路——**不建产物、不落库**：对话历史本就只保存在页面内存，随消息提供的附件保持同样的隐私姿态。

- 服务端 `POST /api/v1/artifacts/parse`（需登录）：multipart 单文件，同步返回 `AttachmentParseResult`（与 `ArtifactText` 同一套 status/engine/characters/images/text 语义）；超过大小上限返回 `unsupported` 并说明原因。抽取链路与产物正文完全相同，含可选 OCR/VL，图片与扫描件因此能转成 Markdown+LaTeX。
- 发送消息时 `attachments/conversation-context.ts` 把附件折算成上下文块并入用户消息（单附件 8000 字、合计 20000 字预算）：浏览器已抽到文字的直接用；解不动的（图片/扫描件/旧格式/自动解析被关）现场调即席解析，权威结果如实回写附件卡片。
- `agent-chat.ts` 的 `sendConversationTurn({ attachmentContext })` 只把上下文块并入请求内容，不进气泡展示；用户气泡下方以纸夹徽标展示附件名。发送成功后清空托盘，失败保留以便重试。
- §2.4 的单模态提醒在对话框内同样生效。首页新建任务的附件仍走 §2.2 上传建产物链路，两条链路互不影响。
- **任务附件同样进入对话**：`attachments/task-attachment-context.ts` 读取工作台 `artifacts` 中无 `producer_node` 的 READY 产物，取 `GET /artifacts/{id}/text` 权威正文（与随消息附件同预算），按解析就绪进度逐轮并入运行页对话（含开场分析）；发送成功才标记已注入，未就绪的附件在上下文中如实标注「仍在本机解析中」。图片/扫描件的解析全部发生在本机 API 进程（可选 VL），不出网。

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

### 5.6 对话轮：服务端托管生成与记录（2026-09-05，ADR-0016）

有归属（任务运行 `run_…` / 首页对话 `chat_…`）的对话不再走无状态的 `/api/chat`，而是**服务端托管轮**：一轮 = `chat_turns` 表的一条记录 + API 进程内的一个后台生成线程（`omm_api/chat_turns.py` `ChatTurnHub`）。页面只是观众——建轮即返回，经 SSE 附着直播，断了按序号续接；切任务、刷新、关标签页都不影响生成。

接口（`routers/chat.py`，全部要求登录）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/chat/turns` | 建轮，202 `{turn}`。body = `ChatRequest` + `scope_id` / `text` / `opening` / `attachments`；同一归属已有 `running` 轮 → 409 `CHAT_TURN_IN_PROGRESS`；`run_…` 归属校验运行所有权 |
| `GET` | `/api/chat/turns/{id}/events?after=N` | SSE：`id: <seq>` / `data: {...,"seq"}`；事件与 `/api/chat` 同形（`meta → delta*/reasoning* → done | error`），`: ping` 心跳；非直播中的轮只回一个合成终态事件 |
| `POST` | `/api/chat/turns/{id}/stop` | 停止生成，半截正文保留（`stopped`） |
| `GET` / `PATCH` | `/api/chat/turns/{id}` | 轮视图 / 补写页面侧执行轨迹行 `{trace}` |
| `GET` | `/api/chat/scopes/{scope}/turns` | 该归属全部轮（按时间；`running` 的带半截 `reply/reasoning` 与 `last_seq`） |
| `DELETE` | `/api/chat/scopes/{scope}` | 删该归属全部轮，运行中的先停 |

轮视图字段：`id, scope_id, status(running|completed|failed|stopped|interrupted), opening, text, attachments, reply, reasoning, meta{host,model,route,usage,elapsed_ms,error?}, error{code,message}?, trace, last_seq, live, persisted, created_at, updated_at, ended_at`。

服务端行为：正文/思考每秒批量回写库；终态后事件缓冲保留 10 分钟供续接；`stop` 立即定格并关闭上游连接；进程启动时把遗留的 `running` 标成 `interrupted`（服务重启是唯一会中断生成的情形）；用量记账只在正常完成时发生。

「保存任务历史」：开启 → 落库；关闭 → 只在内存托管（同样的直播窗口，重启即无）；由开到关 → 服务端删该用户全部轮。删除项目级联删其运行的轮；删除首页对话前端先 `DELETE /scopes/{chat}`。

页面侧（`integration/agent-chat.ts` / `chat-turns-api.ts`）：

- `sendConversationTurn` 有归属时 `startChatTurn` → `onTurnStarted(turn)` → 附着 `events`；`AbortSignal` 触发服务端 `stop`（暂停键真的停生成）；SSE 断线按 `after=seq` 重连（最多 5 次），失败则拉一次轮视图补齐正文；
- 重进：`modeling-workspace-controller` 在首次 `renderWorkspace` **之前** `hydrateConversation(run)` 并广播 `omm:conversation-restore{runId, goal, turns}`（保证规划阶段的 `omm:run-planning` 不重复发起开场分析）；页面层先渲染本机旧记录（只读兜底），再按时间重建服务端的轮，`running` 的那一轮原位续接直播（`attachConversationTurn`：半截先上屏、随后接实时增量、暂停键照常生效）。首页 `restoreHomeChat` 同理；
- 模型看到的上下文（history）仍由页面维护并随每次发起携带：托管轮只托管「这一轮」的生成与记录；`running` 的与无正文的轮不进上下文（保住 user/assistant 严格交替）；
- 本机 localStorage 记录（`tasks/conversation-log.ts`）降级为只读兜底，不再写入；上一轮的「在途轮」（`chatPending`）与 `pagehide` 落定机制已移除。

没有归属的页面（演示态）仍走无状态 `POST /api/chat`，行为不变。非目标：不把整页导航改成软路由（界面基线不动）。

### 5.7 模型目录：厂商在售型号与单价由服务端同步（2026-09-06，ADR-0017）

设置中心「模型厂商」卡片的型号、「配置」一键填入的默认模型、「默认模型 ID」补全与「用量监控」的单价，不再写死在前端 / 后端代码里，而是服务端 `omm_api/model_catalog.py` `ModelCatalog` 从公共目录 [models.dev](https://models.dev)（`api.json`，无需密钥）按 TTL（默认 6 h）同步、裁剪、落文件缓存（`data/model-catalog.json`）后提供：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/llm/catalog` | 目录视图，永不阻塞在出网上：`source(catalog|builtin)`, `enabled`, `catalog_url`, `synced_at`, `stale`, `refreshing`, `error`, `providers[]` |
| `POST` | `/api/llm/catalog/refresh` | 立即同步后返回新视图；拉不到 502 `MODEL_CATALOG_UNREACHABLE`（旧数据保留）；服务端关闭同步 409 `MODEL_CATALOG_DISABLED`；两次强制刷新至少间隔 60 s |

`providers[]` 按厂商预设顺序（openai / anthropic / google / deepseek / qwen / kimi / zhipu / xai / ollama）：`id, label, logo, protocol, base_url, alt_hosts, subtitle, source(catalog|builtin|none), highlights[]（最新三款正式版）, models[]{id, name, release_date, reasoning, vision, context, status("" | beta | preview), input_usd, output_usd, alias}`。裁剪规则与配置项见 ADR-0017。

页面侧：`integration/model-catalog.ts`（一次会话拉一次，refresh 后替换缓存）、`integration/model-catalog-view.ts`（纯函数：按厂商取亮点 / 型号、新鲜度文案、目录模态）。`legacy/openmathmodel-ui.ts` 打开设置时填卡片副标题与「型号目录同步于 … 来源 models.dev」状态行，新增「同步型号」按钮；「配置」默认模型取亮点首位；「默认模型 ID」补全先铺目录再合并接口自报清单。`integration/llm-providers.ts` 只保留品牌骨架（名字 / 图标 / 协议 / 地址 / 备用域名），不再含模型名。`model-modality.ts` 的模态判定先查目录 `vision`，未收录再回落命名规则。后端 `usage.model_pricing` 先按模型 ID 精确命中目录单价 × `OMM_USD_CNY_RATE`，未收录再走手写 `PRICING` 前缀表。

### 5.8 对话即控制面：任务页一句话真正驱动运行（2026-09-06，ADR-0018）

任务归属（`run_…`）的托管轮在调用模型**之前**先经运行控制步骤（`omm_api/run_control.py` `run_control_step`）：读运行状态 → 按状态机算出**合法动作集**（与按钮同一套规则：FAILED→retry；WAITING_APPROVAL→approve / reject / cancel；PAUSED→resume / cancel；RUNNING / QUEUED→pause / cancel；COMPLETED 且修订未满 3 轮→revision；CANCELLED→无）→ 两级意图判定（本地词表规则先判；拿不准、≤200 字且带与合法动作相关的命令气味时，用接口池里**最弱**的模型做一次 10 s 限时 JSON 判定；判定异常 / 结果不在合法集内一律回落为普通对话，**绝不误执行**）→ 走既有路径执行（`actions.execute_action` / `engine_glue.accept_revision` / 运行备注）→ 产出 `action` 事件并把【当前运行状态】【本轮已执行的操作】注入系统提示词，再生成回复。

分级：retry（同事务把这句话落成 global 备注注入重跑阶段）/ resume / pause / approve（点名选项：判别性标签片段、「方案 B」、「第二个」、已知别名；不点名的「同意 / 确认 / 继续」≤12 字且非问句时选**预选项**——唯一 recommended 或唯一正向）/ reject / revision（已完成运行上**点名阶段 + 修改动词**本地直判，否则交判定；受理后照开 ADR-0013 修订门，点名阶段标 recommended 预选，下一句「确认」即重跑）直接执行；**cancel 先发提案**（`status:"proposed"`），只对紧接着的下一轮有效：「确认 / 好 / 取消吧」执行，「算了 / 不要 / 不取消」→ `dismissed`，其它文本作废提案按新意图处理。问句（为什么 / 吗 / ？…）不算命令；带命令词的客气问句（「能不能修复一下？」）按命令。无动作且运行非终态、非问句的文本照旧静默落成备注（不发事件）。`chat_…` 归属与开场轮不经控制步骤。

SSE / 轮视图新增：

```text
{"type":"action","seq":1,"kind":"retry","status":"executed","stage":"EXPERIMENTING","stage_label":"实验运行","note_id":"note_…","message":"已重试「实验运行」阶段，并把你的要求作为备注注入该阶段的执行提示词"}
{"type":"action","seq":1,"kind":"cancel","status":"proposed","message":"要取消这个任务吗？…","confirm_hint":"回复「确认」或点下方按钮执行；回复其它内容则不取消"}
{"type":"action","seq":1,"kind":"approve","status":"executed","approval_id":"appr_…","approval_title":"确认建模方案","option_id":"adopt:plan_b","option_label":"…"}
{"type":"action","seq":1,"kind":"revision","status":"executed","round":1,"approval_id":"appr_…","note_id":"note_…","stage":"DATA_PREPARATION","stage_label":"数据准备"}
{"type":"action","seq":1,"kind":"cancel","status":"rejected","code":"INVALID_ACTION","message":"…"}
```

`action` 事件先于 `meta` / 正文；同时并入轮视图 `meta.actions[]` 随轮落库。`status ∈ executed | proposed | rejected | dismissed`。判定模型的用量按 `source="route"` 记账。

页面侧：`chat-turns-api.ts` `RunControlAction` / `ChatMeta.actions`；`agent-chat.ts` `ChatHandlers.onAction`（`applyEvent` 处理 `action`，重进续接时按视图 `meta.actions` 逐条重放；`entryFromTurn` 在轮没来得及补写 `trace` 时由回执重建轨迹行）；`integration/run-control-view.ts` 纯函数把回执变成轨迹行（静态标题「已重试阶段」「已选定审批选项」「已受理修改要求」「等待你确认操作」「当前状态不允许该操作」「已放弃提案」…，阶段 / 选项名进后缀，服务端说明进详情）；`legacy/openmathmodel-ui.ts` 的回复呈现器渲染回执行、`proposed` 行下挂「确认执行」按钮（等价于发一句「确认」）、`executed` 后广播 `omm:run-reopened` 让工作台控制器立即重拉快照并重接事件流；恢复态最后一轮仍留待确认提案时按钮照挂。**页面不再**在发送前 `POST /notes`，也撤下了「任务已结束，本条按问答处理 / 按这条要求继续修改」那条死路轨迹行；`/notes` 与 `/revisions` HTTP 端点保留（与控制面共用 `record_run_note` / `accept_revision`）。

**动作生效后运行事件的落点（2026-09-07 补）**：`executed` 回执让控制器重接事件流后，重跑阶段的 `run.node_changed / run.log / step.*` 紧跟着到达，此刻对话末尾正是这条（往往还在生成中的）回复块。控制器 `tailTraceHost` 的三条规则：尾部是轨迹块 → 续写；**尾部是对话回复块 → 在该回复块内部**（正文与复制按钮之后）挂「收起执行步骤」折叠头 + `.agent-stream.run-trace` 继续写，不另起带署名的 Agent 块；尾部是用户消息等其它元素 → 另起一个与首条 Agent 消息同构的轨迹块。回复与它触发的执行步骤是同一轮的两半，页面上是同一条 Agent 消息；用户再发消息时后续事件按时间顺序落到新的尾部。配套：回复的复制按钮改为紧跟 `.analysis-copy` 插入（步骤常比回复先到）；`toggle-activity` 折叠头优先折叠紧随其后的列表（回复块里同时有 `.reply-trace` 与 `.run-trace`）。**回复口径**随之分三档（`run_control.reply_rules`）：本轮有 retry / resume / redo / approve / reject / revision 已执行 → 交接口径（两三句告知已做什么、运行接下来做什么、进度看执行步骤，**不在对话里替运行算题 / 建模 / 写论文**）；有待确认提案 → 说明代价请用户确认；其余（无动作 / rejected / dismissed / pause / cancel）→ 原口径「告知操作再回答问题」。`run.log{kind:"user_note"}` 在活动流里改为叙述行（原先落进原始 JSON 兜底）。

非目标（见 ADR-0018 §5）：不给对话模型开工具调用；~~不改引擎状态机（失败运行仍只能重试当前阶段，「失败运行选起点重做」留待下一刀）~~——已由 §5.9 / ADR-0019 修订。

### 5.9 任意状态下从选定阶段重做；判定以模型为主、规则只做快路径（2026-09-06，ADR-0019）

用户追问（上一刀之后）：「你现在只是通过固定的关键词来识别判断吗？agent 自己没有相关的意图识别吗？我希望不管说什么，只要是需要执行的，都会出现真正的修改，而且这个修改显示的进度，也不一定要接着上一次的显示」。拍板：气味词只做第一层门槛，模型识别必须有；修改要求落成**同一运行从选定阶段重做**（上游成果保留、进度接原时间线）；重做先发提案、确认后执行。

**判定顺序（替换 §5.8 的两级判定）**：① 上一轮留有提案 → 只看确认 / 放弃词；② 本地规则命中且原文 ≤12 字 → 直判（省一次调用）；③ 其余一律送判定模型（≤800 字；不再有气味词 / 问句门槛），提示词带运行状态、六阶段进度（已完成 / 失败于此 / 等待确认 / 暂停于此 / 正在执行 / 未开始）、失败原因、待确认选项、合法动作、**最近三轮对话**（用户 / 助手各截断）与这句原话；④ 模型答 none 或异常 → 回落规则结果（长句里的「继续想办法」仍是 retry）；>800 字只落备注。判定的 `redo` 若点名尚未开始的阶段 → 视为 none（没有东西可重做，静默落备注，该阶段执行时读到）；点名的正是 FAILED 的失败阶段 → 折成 retry（直接执行）。代价如实说明：任务页每句非短句对话前多一次弱模型调用（10 s 限时，`source="route"` 记账）。

**redo 动作**：合法状态 RUNNING / PAUSED / WAITING_APPROVAL（节点自提的闸门；ADR-0013 修订门上六个起点已是选项，不开 redo）/ FAILED；COMPLETED 仍走 revision；QUEUED / CANCELLED 不开。流程：提案 `{"type":"action","kind":"redo","status":"proposed","stage":"MODEL_PLANNING","stage_label":"建模方案","text":"<用户原话>","message":"要从「建模方案」重做吗？…","confirm_hint":"…"}` → 下一句「确认」/ 点按钮 → `engine_glue.redo_run`：原话落 global 备注 → 引擎 `RUN_REDO` 事件（reducer 关掉在途 RUNNING 步骤为 CANCELLED、清 review / failure / paused、状态回到目标阶段并 `force_rerun`、丢弃目标及下游旧产出）→ 投影：待审批门 CANCELLED（契约 `resolution` 留空，作废原因记内部 `evidence.superseded_by`）、在途步骤行 CANCELLED（detail「已被「从「X」重做」取代」）、清 `failure_*` / `paused_from_status` / `ended_at`、`current_node` 回目标阶段、状态 RUNNING → 回执 `{"kind":"redo","status":"executed","stage","stage_label","note_id","message"}`，run.log 记 `redo_requested`。「不 / 算了」→ `dismissed`（「已放弃从「X」重做，运行状态不变。」）。

**在途让位**：RUNNING 下确认重做时，正在执行的节点不会被打断（单推进线程、无协作式中断），其收尾写事件会撞 `run_domain_events(run_id, seq)` 唯一约束——推进器 `WorkflowAdvancer.advance` 只把**这一种**完整性错误当作让位契约（warning「in-flight step superseded by a control action」并丢弃结果），其它 IntegrityError 照常抛出；下一 tick 重放日志从目标阶段起跑，被取代的步骤不会再被 `heal_interrupted` 判成 executor lost。

页面侧只加文案：`run-control-view.ts` 新增 `redo` 的标题「已从阶段重做」与类别「从阶段重做」（提案 / 放弃行后缀带回到的阶段名），`RunControlAction.text` 字段；提案按钮沿用 `status:"proposed"` 通道，无 DOM 结构变化。

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
- 终态之后运行再次离开终态（`retry` 把 FAILED 置回 RUNNING、修订受理把 COMPLETED 置回 WAITING_APPROVAL），Web 必须重新连接（`after=` 最后序号只接增量）：控制器在任何一次快照刷新看到非终态而流已收尾时重连，不依赖是哪个动作触发的。
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
| API | 删除 / 清扫链路（PostgreSQL） | 凡改动 `privacy.py` 的删除逻辑、或新增带外键指向 `task_runs` / `projects` / `artifacts` 的表，本地必须用 `OMM_TEST_DATABASE_URL=postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:5433/openmathmodel_test` 指向 `tools/pg-dev.ps1` 的测试库再跑一遍相关用例；并在用例里**逐表数行**断言从属行已清空 |

### 12.1 为什么删除链路不能只看 SQLite 绿

测试夹具默认用 SQLite，而 SQLite **默认不检查外键**、时间列取回是 naive。两类只有 PostgreSQL 才会暴露的缺陷因此在本地全绿、到线上才炸：

- **外键漏删**（2026-09-02，B8）：`stage_outputs`（0017）、`run_notes`（0018）、`paper_exports`（0016）三张表加入后，`privacy._delete_runs` 一直没有连带删除；SQLite 上 `DELETE /projects` 照样 204，PostgreSQL 上则 `ForeignKeyViolation`——侧栏「删除任务」与「任务保留」清扫对任何有阶段产出 / 追问备注 / 论文导出的运行一律 500。修复后新增的用例除了断言 204，还逐表数行，让 SQLite 也能抓到漏删；
- **时区比较**（此前 CI `api-postgres` 作业翻车）：`ended_at` 在 SQLite 取回 naive、PostgreSQL 取回 aware，裸比较在 PG 抛 TypeError，见 `privacy._expired_run_ids` 的 `as_utc`。

CI 的 `api-postgres` 作业会在真实 PostgreSQL 上跑全量 API 测试，但它只能拦住**有用例覆盖**的路径；新增带外键的表时，删除链路的用例要一并补上，否则 CI 也是假绿。

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

### 2026-08-12 设置中心「诊断」做实

高级设置的诊断分区此前三个按钮全是假动作（定时器伪装诊断、toast 假装打开目录、剪贴板写死字符串）。本轮全部接真：

- 后端新增 `GET /api/system`（ops，免登录，同 `/api/health`）：返回服务名、版本、Python 版本、数据库方言名、runner 开关与服务器时间；刻意不暴露连接串、路径与凭据（`test_system_endpoint.py` 断言序列化结果不含 `://` 与文件路径）。OpenAPI 基线已重新导出并通过 `--check`。
- 前端新增 `apps/web/src/diagnostics/system-diagnostics.ts`：并发探测 `/api/health`（连通性与延迟）、`/api/system`（后端信息，404 归为提示以兼容旧后端）、`/api/account/me`（登录态，401 视为接口正常），5 秒超时；报告文本汇总前端环境（UA/语言/视口/在线状态）与后端信息。
- 「运行网络诊断」把三项结果渲染为分区内的状态行（通过/提示/异常三色图标）；「打开日志目录」浏览器内无真实语义，改为「导出诊断报告」（下载 txt）；「复制系统信息」复制同一份报告，剪贴板不可用时提示改用导出。三个按钮执行期间禁用并显示进行中文案。
- 图标排版修正：`.diagnostic-actions button` 改 inline-flex 居中 + 7px 间距，图标统一 15px，不再随文字基线漂移。
- 英文词典同步：移除“打开日志目录”等失效词条，新增诊断结果与提示语翻译。

当日执行并通过：`pytest backend/api/tests -q`（98 个用例）、`export_openapi.py --check`、`node --test`（en-US 词典 5 项）、`npm run check` 与 `npm run build --workspace @openmathmodel/web`；对运行中的后端实测 `/api/system` 返回 200 与预期字段。

尚未验收（需浏览器）：三个按钮的交互与状态行视觉、导出的 txt 内容、未登录/后端停机两种场景下的诊断结论展示。

### 2026-08-13 自定义 API 全链路做实（对话回复 + 任务执行）

设置中心「自定义 API」此前整页是摆设：表单只落 localStorage、「测试连接」是定时器假动作、已保存接口是写死的演示行、三个开关无消费方。本轮全部接真，对话回复与任务执行统一按该配置在服务端出网调用模型（密钥不下发页面、无浏览器跨域问题）：

- 后端新增 `users.llm_config`（迁移 0010，JSON 可空列兼容 SQLite 补列）：已保存接口列表（名称/协议/Base URL/密钥/模型/组织/自定义请求头/路径前缀）、主接口与三个行为开关；`GET/PUT /api/account/llm-config` 仅本人可读写。
- 新增 `omm_api/llm.py`：接口协议下拉的五个选项全部可用——OpenAI Compatible / Ollama / 自定义 REST 走 Chat Completions 形状（Base URL 为裸域名时自动补 `/v1/chat/completions`，已带路径则原样拼接，显式路径前缀最优先），Anthropic Messages（x-api-key + anthropic-version）、Gemini generateContent（key 走查询串）各自映射；流式 SSE 逐协议解析、用量归一并输出结构化用量日志（「记录接口用量」）。「允许使用第三方中转站」关闭时仅放行官方域名与本机地址（403 PROXY_DISABLED）；「失败时自动切换备用接口」仅在超时、网络层失败或 HTTP 429 时按保存顺序切换，模型侧 4xx/5xx 属配置问题不重试；流式一旦开始输出不再回退，避免内容重复。`httpx` 升为运行时依赖；测试经 `_transport_factory` 注入 MockTransport，全程不出网。
- 新增 `POST /api/chat`（需登录）：无状态代理，对话历史随请求携带、服务端不落库；按「流式输出」开关走 SSE（meta/delta/done/error 事件）或一次性 JSON。`POST /api/llm/test`：用表单当前值做最小补全，返回时延、实际模型与回复摘录，验证地址/密钥/模型 ID 全链路。
- 任务执行换脑（engine_glue）：按 run → project.owner → `users.llm_config` 装配节点，配置可用时「问题分析」「建模方案」两个阶段走 `omm-agent-skills` 真实 LLM 节点（goal→problem_statement 适配；JSON Schema 校验 + 一次修复重试；方案 A/B 产出后停在审批），未配置或提示词缺失时整链回落 sim-0.1；其余四阶段仍为模拟节点，待后续批次替换。`omm-agent-skills` 的 requires-python 放宽至 3.10（与 core 同理：纯标准库 + 惰性注解，backend venv 当前为 3.10）。
- 前端：对话页发送后的假回复（650ms 定时器固定文案）改为真实流式渲染（`integration/agent-chat.ts`），完成后标注实际域名、模型与是否切换备用（第三方中转站按开关文案显示实际域名），未配置/未登录时提供「前往设置中心配置模型接口」入口；设置保存时同步 llm-config（更新主接口或创建首个；example.com 示例占位视为未配置，避免把演示值当真实接口）；「测试连接」「保存为新接口」接真；已保存接口列表从服务端渲染，菜单支持设为主接口/删除；面板打开时表单、三个开关与协议下拉均以服务端为准回填（`integration/llm-settings.ts`）。密钥提示文案改为「密钥仅保存在本机后端」。
- OpenAPI 基线重导出（新增 3 个路径）；英文词典删除假动作词条、新增对话与接口管理文案。

当日执行并通过：`pytest backend/api/tests -q`（126 个用例，含 llm-config 读写、五协议映射、流式/回退/门控、任务换脑与修复重试等 28 个新用例；本机需 `PYTHONUTF8=1`——alembic.ini 是 UTF-8 而系统 locale 为 GBK，属环境问题与本轮改动无关）、`pytest agents/skills/tests`（3.10 兼容 26 用例）、`export_openapi.py --check`、`node --test`（en-US 词典 5 项）、`npm run check` 与 `npm run build --workspace @openmathmodel/web`。

尚未验收（需浏览器 + 真实厂商接口）：设置面板的回填/测试连接/接口列表交互、对话页流式渲染与错误提示视觉、真实 OpenAI 兼容网关与 Ollama 的实测（本轮上游全部为 MockTransport 模拟）。

### 2026-08-13（追加）主流厂商官方 API 预设与「模型厂商」页更新

实测反馈驱动的三轮加固与厂商预设落地：

- 错误兜底：`llm.py` 把网络层异常全部归一为可执行错误信封（超时 504 LLM_TIMEOUT、连接失败 502 LLM_UNREACHABLE、重定向 LLM_REDIRECTED 提示改 Base URL、非 JSON 响应 LLM_BAD_RESPONSE 展示响应开头），不再向页面裸露 500；超时/网络失败与 429 一样触发备用接口切换；「测试连接」不再限制 max_tokens（推理模型思考预算可能远超小额上限），读超时放宽至 45 秒。
- 路径与域名护栏：OpenAI 兼容协议在 Base URL 为裸域名时自动补 `/v1/chat/completions`（带路径原样拼接、显式前缀最优先）；填知名厂商官网/聊天页域名（deepseek.com、chatgpt.com、claude.ai、kimi.com、x.ai、bigmodel.cn 等）时直接提示正确 API 地址（400 LLM_WEBSITE_URL）。前端把我们自己后端的 404 映射为「后端尚未加载新接口，请重启后端服务」。
- 厂商预设（`integration/llm-providers.ts`）：九家主流厂商官方 API 一键填入——OpenAI（gpt-5.6-sol/terra/luna）、Anthropic（claude-opus-5/sonnet-5/fable-5/haiku-4-5）、Google Gemini（gemini-3.6-flash/3.5-flash/3.5-flash-lite）、DeepSeek（deepseek-v4-pro/flash，旧 deepseek-chat 已于 2026-07-24 停用）、通义千问（qwen3.8-max，DashScope 兼容模式）、Kimi（kimi-k3）、智谱（glm-5.2/5/5-turbo）、xAI（grok-4.6/4.5）、本地 Ollama。模型名与接入地址采集自各厂商官方文档（2026-08-13）。
- 「模型厂商」页：卡片改由预设生成（副标题=当前在售模型），「配置」跳转自定义 API 并自动填入协议/地址/模型（用户只补 Key），「已连接」状态按已保存接口的域名真实判定；智能路由下拉与输入区模型选择器同步为最新型号；无品牌资源的厂商 logo 回落首字母标。官方域名白名单扩充（api.x.ai、火山方舟、百度千帆、腾讯混元、阶跃、硅基流动等），不受「允许第三方中转站」开关影响。
- 新增测试：网络异常信封 5 例、官网域名护栏 1 例、官方域名门控 1 例（累计 41 个 LLM 相关用例）。词典同步增删。
- 思考过程展示：后端按协议提取推理内容（OpenAI 系 `reasoning_content`/`reasoning`、Anthropic `thinking` 内容块、Gemini `thought: true` 部件），流式经 SSE `reasoning` 事件转发、非流式随响应 `reasoning` 字段返回；前端在回答上方渲染思考块——流式期间「思考中…」shimmer 标签 + 180px 封顶自动跟随视口（超高时上下渐隐遮罩），回答开始后折叠为「已思考 N 秒」可点击展开回看；思考内容只展示、不回写对话历史；适配深色主题与「减少动态效果」偏好。无思考内容的模型在等待首个回答增量时同样显示扫光「思考中…」占位；思考头部按钮覆盖全局 button 悬停底色保持裸文本观感；回复完成后右下角提供复制按钮（复制 Markdown 原文，成功后短暂切换对勾图标）。
- 执行步骤时间线：完成态图标由绿钩改为黑色圆形白色对钩（19px，深色主题反白），与单色设计语言一致。
- 细粒度活动流（参考图节奏：思考/工具/叙述交替）：真实 LLM 节点的每次模型调用产出过程事件进 `run.log`（`EngineLlmPort.on_event` → 同事务 append_event）——`thinking`（思考内容截断至 6000 字 + 耗时）与 `llm_call`（模型/接口/耗时/tokens 摘要）；工作台回复区在摘要之后渲染 `.agent-stream`：`run.log` 过程行（深度思考=sparkle、调用模型=lightning，右侧真实耗时，点开浅灰 16px 圆角容器看思考全文或调用参数）、`artifact.published`（写入 X，可展开）、`approval.requested/resolved`（等待确认行实时走秒、落定显示等待时长）、`node_changed/status_changed` 作为叙述行穿插；按 sequence 去重，SSE 首连回放历史、重连不重复；`step.*` 事件归上方阶段时间线不重复展示。follow-tail 浮标按产品反馈移除。配套后端测试断言 thinking/llm_call 过程事件入流。
- Agentic 执行轨迹（融合改造，参考 Codex 语义时间线/RemCodex 统一纵向流的公开设计）：真实运行下（`data-workspace-source="api"`）把运行页的六个步骤行去卡片化为时间线行——左侧动作图标（分析/数据/方案/实验/写作/交付各一，进行中呼吸脉冲，完成保持既定的黑底白钩，失败红叉）、中间阶段名（重试时加「第 n 次尝试」徽标）、右侧真实耗时（数据来自 `/task-runs/{id}/steps` 控制面接口，与快照并行拉取；完成/失败=服务端起止差，进行中每 500ms 走秒）；行点击展开的详情区为浅灰 16px 圆角低权重容器（失败时显示失败原因，进行中显示 current_step）；细左轨 + 垂直留白建立时间线关系；「回到最新」follow-tail 浮标（上翻 160px 出现，不抢滚动位置）；输入框上方运行状态条（spinner + 状态 · 当前阶段 + n/6，等待确认暂停旋转，规划期与终态不显示）。演示页面完全不受影响（样式全部按 api 来源作用域隔离）。
- 任务执行节奏（按 ADR-0007 组件语义实现，不新增容器）：任务创建后、首个步骤启动前（QUEUED 或 RUNNING 且全部页面 PENDING）为规划阶段，控制器广播 `omm:run-planning`，页面层在**同一条 Agent 消息内**（注入步骤块头部之后，不拆成第二条对话）发起一次**真实的开场分析**（走 /api/chat：思考块流式 → 折叠「已思考 N 秒」→ Markdown 分析正文，未配置接口/未登录时整块静默消失，sessionStorage 按 run 防重、刷新不重复扣费）。严格顺序出现：开场分析落定前（`data-opening-state="pending"`）计划部分（折叠开关/步骤列表/阶段摘要/CTA）完全不渲染也不占位——包括「任务正在排队」摘要与「等待任务开始」按钮；回复结束（成功/失败/静默移除均算）后置为 done 并广播 `omm:opening-analysis-done`，控制器立即重渲染，六个阶段按 140ms 级联缓速揭示（等待期间刻意不构建时间线，保留 planning 标记，放行时才播放级联；之后的 SSE 刷新与中途进页不重播）。注意 `hidden` 属性对带作者级 `display:flex/inline-flex` 的元素无效，这些元素需显式 `[hidden] { display: none !important }`，否则会出现「已隐藏却仍显示」。
- 对话页不暴露接口信息：回复正文不再附带实际域名、模型名与 Auto 难度判定（流式期间的「正在调用 <host>」行一并移除）；「允许使用第三方中转站」的透明化只体现为设置中心的本机用量记录，关闭时连记录也不留。
- 对话回复 Markdown 渲染（`text/markdown.ts`，零依赖）：转义优先的受限子集——标题/列表/引用/表格/分隔线/粗斜体/行内代码/代码块/http(s) 链接；`$…$`、`$$…$$`、`\(…\)`、`\[…\]` 公式输出为 `data-tex` 节点（行内加 `data-tex-inline`），复用方法库的 KaTeX 懒加载器在全文到齐后排版，加载前保留 LaTeX 源码回退；「$5 和 $10」这类金额不会误判为公式，`javascript:` 链接保持纯文本。流式期间逐增量重渲染，未闭合代码块随增量增长。配套 `markdown.test.mjs` 10 个用例（XSS 转义、注入路径、表格、流式半截代码块等）。

当日执行并通过：`pytest backend/api/tests/test_llm_*.py test_task_runs_llm_nodes.py`、`node --test`（词典 5 项）、`npm run check` 与 `npm run build --workspace @openmathmodel/web`。真实接口对 ainb.plus 网关探针验证连通（假密钥返回上游 401 信封）；DeepSeek 官方等真实密钥实测留给浏览器验收。

### 2026-08-20 执行步骤揭示时机：规划真正完成后才显示

产品反馈驱动：此前「首个步骤一启动」就揭示六个执行步骤，而题意解析几乎在任务创建后立即启动，体感等于“随便发点什么都马上弹出全部步骤”。本次把揭示时机推迟到规划真正完成：

- `isPlanningPhase`（`modeling-workspace-controller.ts`）判定窗口延长——`QUEUED`，或 `RUNNING` 且问题分析页仍为 `PENDING/RUNNING` 且后续页面均未启动，都属于规划阶段；问题分析页落定（成功/失败）或任一后续阶段启动才视为规划完成。运行离开 `QUEUED/RUNNING`（暂停、待审批、失败、终态）时需要展示状态行，同样揭示。
- 规划阶段「收起/查看执行步骤」折叠头与步骤列表一起等待揭示（新增 `.activity-summary` 的规划期隐藏，两种布局通用；`[hidden]` 的 `!important` 还原规则此前已存在）。此期间用户看到的是：开场分析（若已配置接口）→「正在思考并规划执行步骤…」扫光行 + 活动流的「深度思考 · 题意解析」过程行；规划完成后六个阶段仍按 140ms 级联揭示，SSE 刷新与中途进页不重播。
- 附带修复：此前若首个快照到达时题意解析已在运行（常态时序），`omm:run-planning` 不会广播、开场分析整体被跳过且步骤立即可见；判定窗口延长后该竞态消除，开场分析可靠触发（sessionStorage 按 run 防重不变）。
- 不动契约、后端与页面结构：判定完全基于既有 `ModelingWorkspaceView.pages[].status` 语义（问题分析页在 `PROBLEM_ANALYSIS` 推进到后续节点时由投影算法翻为 `SUCCEEDED`）。

### 2026-08-20（追加）输入框附件托盘折叠样式

附件托盘（`composer-attachments` 模块自有槽位，覆盖首页/确认页/对话页所有 `.composer`）加可伸缩折叠，动效语言参考 aicss.dev To-do List、按产品黑白灰体系以原生 DOM 重写：

- 折叠头：回形针图标 + 「附件」 + 计数徽标；悬停图标交叉淡出为箭头，收起时箭头旋转 -90°，`aria-expanded` 随动；点击收展，收展为 `grid-template-rows 1fr ↔ 0fr` + 透明度过渡。
- 计数徽标「已解析/总数」（已解析 = phase 非 parsing/uploading）：逐字符槽位滚动更新（旧字上移、新字滚入，350ms），内容为纯数字斜杠，语言中立不进词典；徽标带 `aria-label`。
- 新附件按加入顺序级联入场（50ms 步进）；解析进度等重渲染不重播动画（已入场 id 集合防重）。收起状态下新增附件自动展开；清空后回到展开态。
- 单模态提醒行（ADR-0010）与错误状态行保持在折叠区外，收起时不会藏住需要知情的内容。
- 修正 `.composer-attachments[hidden]`：托盘是作者级 `display:flex`，会压过 UA 的 `[hidden]`，此前托盘为空时留白不可见，现在有常驻折叠头必须显式还原 `display:none`。
- 全部动效尊重 `prefers-reduced-motion`；暗色主题按 `html[data-theme="dark"]` 补齐配色。

### 2026-08-27 厂商预设、模态识别与费用估算刷新到当代型号

按各厂商官方文档重新采集（2026-08-27），只改数据与识别规则，不动页面结构与接口契约：

- 厂商预设（`integration/llm-providers.ts`）：智谱换成 glm-5.3 / glm-5.3-flash / glm-5.2（GLM-5.3 于 2026-08-14 发布、08-19 开放 API，与 5.2 共用基座同价）；通义千问补 qwen3.8-flash（2026-08-26 上线）；Kimi 换成 kimi-k3 / kimi-k2.7-code / kimi-k2.6；Anthropic 把能力最强的 claude-fable-5 提到首位（Mythos 5 仅限 Project Glasswing，不入预设）。OpenAI、Gemini、DeepSeek、xAI 经核对仍是当前在售型号，保持不变。
- 预设新增可选 `altHosts`：同一厂商的多个官方入口（智谱 `api.z.ai`、Kimi `api.moonshot.ai`）共用一张卡片的「已连接」判定与品牌标识，新增 `presetMatchesHost()` 供卡片状态与模型选择器共用。
- 官方域名白名单（`llm.py`）补 `api.z.ai`、`api.moonshot.ai`；官网→API 提示补 z.ai / chat.z.ai / www.bigmodel.cn。
- Auto 路由的能力推断：新一代档位记号入表——强档补 `fable`、`-sol`，轻档补 `-luna`（带连字符避免误伤 solar 等词根）。命中不了的型号仍按中位 5，用户填写的权重永远优先。
- 模态识别（`integration/model-modality.ts`，ADR-0010）：修正 Kimi K 系列误判——K2.5 起均支持图片输入，此前被当作纯文本会误报「图片不会被看到」，现改判视觉；GLM 视觉线模式放宽到 `glm-Nv`（覆盖 glm-5v-turbo），并单列原生多模态的 GLM-5.3-Flash；GLM-5.3 官方声明仅文本，精确入纯文本表（不做家族级推断，未知型号继续沉默）。
- 「默认模型 ID」补全（新增 `POST /api/llm/models`）：预设表是快照、写下来那天就开始过期，因此让输入框直接问接口本身要模型列表——OpenAI 兼容家族 `GET {base}/v1/models`（裸域名补 `/v1`，不套「路径前缀」，那是对话补全用的）、Anthropic `GET {base}/v1/models`（x-api-key + anthropic-version）、Gemini `GET {base}/v1beta/models?pageSize=200&key=`（去掉条目名的 `models/` 前缀）。密钥仍只在服务端使用，不产生 token、不记用量，因而也不受预算闸门约束；上限 300 条，网关未实现（404/405）时给 `LLM_MODELS_UNSUPPORTED` 并指向「手填模型 ID」。前端在现有输入框上挂原生 `<datalist>`（UA 默认 `display:none`，零布局改动）：先按 Base URL 域名秒填预设型号，聚焦时再拉真实清单合并（`fetchEndpointModels` 按「协议+地址+密钥」缓存，失败不留缓存以便重试），拉不到就只留预设，不打断填写。新增 8 个后端用例覆盖三种协议的路径与头、404 指引、中转站门控与登录校验。
- 费用估算表（`usage.py`，影响预算硬闸门）：按「本地零费用 → 当代型号 → 上一代型号 → 家族兜底」重新分组，补 GPT-5.6 三档、Fable/Opus 5、Gemini 3.6 Flash、DeepSeek V4 Pro/Flash（2026-08-16 起峰谷两价，按峰价保守估）、Qwen3.8-Flash、GLM-5.3/5.2、Grok 4.5/4.6 的实际单价；顺带修掉 `qwen3:` 本地标签被 qwen 家族兜底价抢先命中的失效条目。未收录的新型号仍落家族兜底，不会算不出钱。

### 2026-08-27 执行过程事件即时可见（修复「阶段静默后整批闪现」）

真实节点的一次模型调用动辄一两分钟，此前 `EngineLlmPort.on_event` 产出的 `thinking`/`llm_call` 过程事件只挂在推进事务里、要等节点结束的下一次 checkpoint 才随 `STEP_SUCCEEDED` 一起提交——SSE 是轮询已提交行的模式，结果是每个阶段执行期间工作台完全静默，结束时整批过程行同时闪现。本批修复：

- 推进线程（checkpoint 模式）下，每条过程事件在发生的当下独立提交并立即对 SSE 可见；HTTP 动作路径保持整请求一个事务不变。
- 新增 `llm_call_started` 过程事件（`kind`/`prompt_id`/`repair`）：每次模型调用开始时发出。前端把它渲染为带实时走秒的「深度思考 · 阶段」行，`thinking` 到达时整体替换为含思考全文的最终行，无思考内容的模型由 `llm_call` 就地落定（活动流仍不展示模型与接口名）。
- 用户刷新页面时若某次调用仍在进行，SSE 首连回放会重建这条走秒行，落点仍在首气泡活动流。

### 2026-08-28 接待判定纳入附件内容（修复「无关文件也会启动六阶段」）

带附件的发送此前在接待判定（`POST /v1/task-intake`）里被启发式直接放行（假设「附件≈题面」），内容与建模无关的文件照常创建任务，最后由问题分析节点的 viability 门以「题意解析执行失败」收场——语义上是输入问题，却被渲染成带重试按钮的工作流故障。本批修复：

- 契约：`TaskIntakeInput` 新增可选 `attachments[]`（`name`/`excerpt`/`characters`，单摘录上限 2000 字符、最多 20 个），OpenAPI 基线已重导出。
- 服务端（`intake.py`）：有正文摘录的附件必须进模型判定——提示词逐个列出文件名与摘录（每个截 500 字、最多 5 个），附件内容与输入正文同权；解析不出文字的附件（纯图片/扫描件/关闭自动解析/确认页仅元数据）维持放行，由第二层 viability 门兜底。长题面（≥200 字）仍启发式放行，不为附件多烧判定调用；判定异常一律放行的总原则不变。
- 前端（`task-start-controller.ts`）：发送前先等浏览器解析完成（`store.settled()`），把每个附件的文件名与前 1200 字摘录随判定请求上送；@ 引用赛题仍视为带着题面来的，直接放行。判定不放行时沿用既有「首页对话」路径原地回应，不建项目、不出现执行计划。
- 测试：接待判定套件 7→10 个用例（附件摘录必进判定且意图透传、无摘录附件启发式放行不出网、长题面优先于附件判定）。

当日执行并通过：`pytest backend/api/tests`（255 个用例，其中 task-intake 10 个）、`npm run check --workspace @openmathmodel/web`（typecheck + eslint）、Web 生产构建（139 个模块）、`export_openapi.py` 基线重导出。

尚未验收：真实浏览器中发送「短正文 + 内容无关的附件」应停留首页并收到对话式回应（不出现执行页与计划条）的视觉走查。

### 2026-08-28（追加）任务创建过场、开场分析附件兜底、执行计划条目压缩

三项用户实测反馈的修复，不动页面骨架、路由与契约结构：

- 任务创建过场遮罩（新增 `integration/task-launch-overlay.ts` + `task-start-controller.ts` 接线）：此前发送后只有状态行一句「任务已创建，正在进入运行工作台…」。现在接待判定放行后全屏遮罩接管：「创建项目 → 上传附件 → 启动 Agent 工作流」随真实阶段逐步打勾（`SubmitOptions.onPhase`，无附件时不显示上传步骤），成功后主视觉环收拢成对勾、定格一拍再导航；判定不放行（首页对话）、未登录、出错时遮罩不出现或立即淡出，错误仍走状态行。遮罩 aria-hidden，读屏通道保持状态行（role=status）；动效尊重 `prefers-reduced-motion`，暗色主题补齐配色；层级 230（压过设置 180 与二级弹框 220，让位 toast/菜单 260）。
- 开场分析拿不到附件的修复（`attachments/task-attachment-context.ts` 重写）：用户实测「上传了 PDF，首条回复却说没收到题面」。原因是任务附件上下文对服务端正文只等 12 秒、超时只留一句「仍在解析中」，且工作台清单拉取失败会被永久缓存成「无附件」。现在：① 任务创建时把浏览器解析摘录按 run 交接到 sessionStorage（`persistTaskAttachmentExcerpts`，单条 4000 字/合计 24000 字，与草稿摘要上限一致），服务端正文没赶上等待预算时以摘录顶上（标注「浏览器初步解析、完整正文稍后自动并入」），权威全文就绪后仍并入后续轮次（摘录已覆盖全文的短附件不重复注入）；② 工作台清单拉取失败或为空不再缓存，下一轮重试，失败当轮直接用交接摘录兜底；③ 正文请求失败立即重试并计入「解析中」；④「解析中」提示列出文件名并明确指示模型「勿断言缺少题面、勿要求用户补充材料」。
- 执行计划第 4 条压缩（`workspace_view.py` `_plan_texts`）：实验与验证条目此前两处发胖——初稿把 EXPERIMENTING 与 VALIDATING 两句拼接，方案确认后又细化为「按方案「名称」实施：步骤1；步骤2；步骤3」（上限 240 字），远长于其他条目（12~24 字）。现在多阶段页面只取首个阶段的短句；细化只保留「按方案「名称」实施」（方案名截 18 字，整条截 40 字兜底），步骤全文仍在建模方案页与「实现计划」分页。对应测试断言同步更新（所有 plan_text ≤ 40 字）。

当日执行并通过：`pytest backend/api/tests`（完整套件）、`npm run check --workspace @openmathmodel/web`（typecheck + eslint）、Web 生产构建（140 个模块）。

尚未验收：真实浏览器中的三项视觉走查——发送后过场遮罩的完整节奏（含无附件/出错/未登录分支）、带附件任务的开场分析首条回复内容、执行计划面板各条目长度。

### 2026-09-05 智能路由候选改为已保存接口；GPT-6 Astra 适配

产品反馈：设置中心 → 模型厂商 → 智能路由的四个下拉「问题不是写不写死，而是要看用户自己配了什么」，并且要能即时更新；OpenAI 新旗舰 GPT-6 Astra 需要适配。不动页面结构、路由与接口契约（ADR-0014 决策 11）：

- 智能路由下拉（`openmathmodel-ui.ts`）：删掉四组写死的厂商型号，改为 `routingSelectOptions(config)` 生成——`自动选择` + 每条已保存接口（取值 `endpoint-<id>`，与输入框模型选择器同一套标识；显示「模型 · 接口名」，主接口再标一下）。新增 `renderLlmConfigViews` 把服务端接口配置的三处投影（已保存接口列表、厂商卡片状态、智能路由候选）一起刷，`hydrateLlmPanel` 打开面板、「保存为新接口」「保存修改」、菜单里的设为主接口/删除/调整权重之后都走它，用户不必关掉设置再打开。刷新保留当前选择，接口被删回落 `自动选择`；首次打开时选择从本机设置表读取（那时原生 select 只有 Auto 一项，`restoreSettings` 写不进去）。池子为空/未登录时四个下拉只剩 `自动选择`，下方 `[data-routing-note]` 一行说明指向「自定义 API」或登录（新增 `.settings-field-note`，revision 37）。
- 自定义下拉增强（`enhanceSettingsSelect`）从 `openSettingsCenter` 闭包提为可重复调用的模块级函数：重建前先拆掉旧的 `.settings-custom-select`，选项集合动态变化时原生 `select` 与自定义下拉不再分叉。
- 边界如实标注：这四项在本轮只随整张设置表落本机 `localStorage`，服务端不读取它们；服务端链路由同日的下一条记录（ADR-0015）补上。
- 明确保留：设置中心「自定义 API」表单的预填示例（`api.example.com` / 示例密钥 / `gpt-5.6-sol`），`endpointFromForm` 对 `example.com` 直接判为未配置。
- GPT-6 Astra（2026-09-03 分批 GA，API id `gpt-6-astra`，1.05M 上下文、文本+图像输入，标准价 $10/$50，>272K 输入整笔 2×/1.5×）：厂商预设（`llm-providers.ts`）OpenAI 首位改为 `gpt-6-astra`，「配置」一键填入即为它；模态表（`model-modality.ts`）`/^gpt-5/` 放宽为 `/^gpt-[5-9]/`，Astra 判为视觉；Auto 路由能力推断（`llm.py`）强档记号新增 `-astra`（否则旗舰按中位 5 使用）；费用估算表（`usage.py`）新增 `gpt-6-astra` / `gpt-6` 70/350 元（此前会落到 4/12 元的兜底价，预算闸门对旗舰放行十几倍）。
- 词典：删「根据任务类型、速度与费用自动选择模型。」，增智能路由说明与两条空态提示。

当日执行并通过：`npm run check --workspace @openmathmodel/web`、`npm run build --workspace @openmathmodel/web`、`node --test src/i18n/en-US.test.mjs`（5 项）、`pytest backend/api/tests/test_llm_chat.py test_usage.py test_budget_guard.py`（79 个用例，含新增 `test_endpoint_strength_recognizes_current_flagships`、`test_model_pricing_covers_gpt6_astra`）。

尚未验收（需浏览器）：登录后在「自定义 API」保存/删除接口时切到「模型厂商」看四个下拉是否即时跟随；未登录与零接口两种空态提示；OpenAI 卡片副标题与「配置」默认模型为 `gpt-6-astra`。

### 2026-09-05 智能路由做实：按任务类型把模型调用定向到指定接口（ADR-0015）

业主确认要让四个下拉真正生效。契约与语义见 [ADR-0015](../adr/0015-task-kind-endpoint-routing.md)，落点：

- **契约**（`schemas.py` / `routers/account.py`）：`LlmConfigUpdateRequest` 增 `smart_routing: bool = True` 与 `task_routes: TaskRoutesModel`（`coding` / `research` / `writing` / `vision` 四个可空 id）；`PUT` 用 `normalize_task_routes` 只存指向本次保存里现存接口的项，`GET`（`llm_config_payload`）永远回四键齐全、指向已删除接口的项读出为 `null`。OpenAPI 基线已重导出（`export_openapi.py --check` 通过）。
- **解析与取链**（`llm.py`）：`LlmConfig` 增 `smart_routing` / `task_routes`，`route_for(kind)`（开关关闭、未定向、接口不存在 → None）与 `chain_for(kind)`（有定向 → `chain_from(定向接口)`，否则 `chain()`）；预算硬限制把付费接口过滤掉后指向它们的定向自然失效（`find` 找不到）。
- **任务执行**（`engine_glue.py` / `EngineLlmPort`）：新增 `_PROMPT_TASK_KINDS`（与 `_PROMPT_NODE_IDS` 同键：题意解析 / 数据准备方案 / 方案设计四条 → `research`；清洗沙盒 / 实验代码 / 检验 → `coding`；论文四条 → `writing`），端口按 `prompt_id` 取 `chain_for(kind)` 传给 `stream_complete_with_fallback` / `complete_with_fallback`；`llm_call` 过程事件附 `task_kind` 与 `pinned`，工作台执行轨迹与 evals 可核对「这一步打给了谁」。接待判定与 Auto 难度判定不受影响。
- **对话**（`routers/chat.py`）：携图且未显式 `endpoint_id` 时优先走 `route_for("vision")`，meta `route = {mode: "task", kind: "vision", endpoint_id}`；用户显式钉接口不改。顺手修掉 `route_meta["difficulty"]` 的硬取键（非 Auto 路由的 meta 没有该键会 KeyError）。
- **前端**：新增 `integration/task-routes.ts`（下拉值 ↔ 接口 id、`taskRoutesFromForm`、`normalizeTaskRoutes`、`resolveTaskRoute`，纯函数，`task-routes.test.mjs` 5 项）；`auth/api.ts` `LlmConfig` 增可选 `smart_routing` / `task_routes`；`llm-settings.ts` 新增 `routingFromForm`，「保存更改」「保存为新接口」与三个行为开关同一纪律一并携带（否则整体替换式 PUT 会把服务端已存的定向抹成缺省）；`model-modality.ts` 的 `resolveSelectedModality("auto")` 先看 `vision` 定向再看主接口，并暴露 `invalidateModalityConfig()`，`hydrateModelPickers` 每次接口/路由变更都作废缓存；设置面板 `renderRoutingSelects(backdrop, config, { fromServer })`——打开面板以服务端 `task_routes` 回填、面板内刷新保留未保存选择，`hydrateLlmPanel` 同时回填 `smartRouting` 开关；开关文案改为如实描述（「开启后按下方任务类型把调用定向到指定接口；关闭则全部走主接口链」），区块说明补一句「保存后任务的对应阶段与携图对话按此定向」。
- 新增 `backend/api/tests/test_task_routes.py`（10 个用例：规范化与解析、`chain_for`、提示词全覆盖归类、llm-config 往返与删接口清理、端口按任务类型选链与开关关闭、携图对话的 vision 定向 / 显式接口优先 / 流式 meta）。

当日执行并通过：`pytest backend/api/tests`（320 通过、2 跳过；4 个失败全部位于 `test_stage_outputs` / `test_task_runs_llm_nodes` 的稳健性复跑与 G3 结果闸门，属于工作树里另一路在进行中的 H3 验证节点改动——`test_task_runs_llm_nodes.py`、`stage_outputs.py`、`validating.*.prompt.md` 均处于修改态——与路由无关）、`export_openapi.py --check`、`npm run check`、`npm run build`、`node --test src/i18n/en-US.test.mjs src/integration/task-routes.test.mjs`（10 项）。

尚未验收（需浏览器）：ADR-0015 验收 4（打开面板以服务端为准回填、保存后重开仍是改后值、删除被定向接口后回落自动）；验收 2/3 需要两条真实接口（可用两个本地 Ollama 模型）跑一次任务与一次携图对话，看 run.log 的 `llm_call.pinned` 与对话 meta 的 `route.mode`。

### 2026-09-05 报障修复：「重试当前阶段」后页面冻结；中转站自身故障一次判死

用户报障：题意解析失败后点「重试当前阶段」，页面停在「进行中 + 正在思考并规划…」再无变化。直接查本地库对账（`run_e761cc88`）：22:03:05 FAILED → 22:03:23 `RUN_RETRIED` → 22:03:25 attempt 2 → 22:03:53 再次 FAILED，服务端整个过程正常，页面一条都没收到。

- **前端（`modeling-workspace-controller.ts`）**：运行到终态时服务端发 `stream.end`，控制器置 `streamEnded` 并关闭 EventSource、不再重连；`retry` 成功后 `refresh()` 拿到 RUNNING 却没有重接事件流，attempt 2 的 `step.started / llm_call_* / 再次 FAILED` 全部丢失。ADR-0013 修订重开已为同一问题修过一次（`omm:run-reopened`），retry 这条路漏了。修法：抽出 `reopenStream()`，`refresh()` 在快照为非终态且流已收尾时重连（`after=lastSequence` 只接增量，`replayThrough` 不变所以新事件按实时落位），`onRunReopened` 复用同一函数。§6 SSE 规则补了这一条。顺带修 `.todo-count[hidden]`（`display:inline-flex` 压过 UA 的 `[hidden]`，思考态下旧的「0/6」一直挂着）。
- **后端（`llm.py` / `engine_glue.py`）**：这次两轮失败的正文分别是 HTTP 401「鉴权服务请求失败: … context deadline exceeded (Client.Timeout …)」与 HTTP 500「failed to connect to `user=postgres …`: FATAL: sorry, too many clients already」——中转站自家鉴权微服务超时、数据库连接池打满，与密钥、模型、请求内容无关。旧逻辑把非 402/429 的上游 4xx/5xx 一律归 `LLM_UPSTREAM_ERROR`（确定性）：不切备用接口、引擎整次调用不退避重试，题意解析一次就把任务判死，指引还让用户「检查接口配置」。现在 `_upstream_error` 按状态码 502/503/504 或正文命中网关故障特征词（`_UPSTREAM_TRANSIENT_HINTS`：deadline exceeded / client.timeout / timed out / timeout / too many clients|connections / connection refused|reset / temporarily unavailable / service unavailable / overloaded / server is busy / 鉴权服务请求失败 / 请求超时 / 服务繁忙 / 系统繁忙）归为新码 **`LLM_UPSTREAM_UNAVAILABLE`**（HTTP 502，文案「接口 X 暂时不可用（HTTP 原状态码）：正文」），同时进 `_FALLBACK_CODES`（对话与任务都会切备用接口）与 `_TRANSIENT_CODES`（引擎整次调用 2s/5s 退避重试）；`_API_ERROR_GUIDANCE` 新增对症指引（与余额、密钥无关，已自动重试仍失败→稍后再试或加备用接口），`_FAILURE_CLASS_RULES` 补「暂时不可用」。「invalid key」式 401、「boom」式 500、404/400 仍是确定性失败，既有测试语义不变；普通 500 只有正文命中特征词才按瞬态处理。
- 新增 `backend/api/tests/test_llm_upstream_transient.py`（19 项：两组参数化分类、402 独立码、失败归类与指引、非流式/流式链内回退、密钥错不烧备用接口、单接口配置下引擎退避重试恢复）。

当日执行并通过：`pytest` 新文件 19/19；`test_llm_chat.py`、`test_llm_engine_retry.py`、`test_llm_config.py`、`test_task_routes.py`、`test_budget_guard.py`、`test_task_intake.py` 共 104 通过；`test_task_runs_llm_nodes.py -k "failure or failed or retry"` 5 通过；`npm run check`、`npm run build`（index 723.91 kB）。浏览器复现验收（失败 → 点重试 → 页面出现第二次尝试行与最终状态）待用户；后端进程需重启才会加载新的瞬态判定（`npm run dev` 启动的 uvicorn 不带 `--reload`）。

### 2026-09-05 报障修复：生成中切页，提问与半截回复整轮消失

用户报障：在任务页追问后没点暂停就切到别的页面（或从有历史的任务切到另一条记录再回来），那一轮的提问、已生成/已思考的部分全部不见；没完成过一次对话的任务页上只剩阶段失败摘要，看起来像"凭空冒出一句话"。

- **根因**：站内除工作台五个阶段面板之间是软切换外，所有跳转（`go()` → `window.location.href`、侧栏「最近任务」的裸 `<a href>`）都是整页导航，浏览器直接掐断在途的 `POST /api/chat` 流并销毁内存；而本机对话记录（`tasks/conversation-log.ts`）只在一轮回复**完整收到后**才成对写入，`agent-chat` 的在途登记（`pendingTurns` + `omm:chat-turn-committed/failed`）又是纯内存的，只救得了软切换。失败的一轮（接口报错 / 后台失败）同样不落盘。
- **修法（不改导航方式）**：在途轮做成可恢复的持久状态。
  - `conversation-log.ts`：新增按归属隔离的**在途轮记录** `openmathmodel.chatPending.v1.<scope>`（`PendingTurnRecord`：owner 标签页 id、用户消息、附件名、半截 reply/reasoning、时间戳），`save/load/clearPendingTurn`；`settleInterruptedTurn()` 把它落定为正式条目：用户消息 + 一条 **`interrupted: true`** 的回复（保留半截正文与思考，`note` 记原因，≤300 字）。`parseConversationLog` 只对 interrupted 回复放行空正文。`clearConversationLog / clearAllConversationLogs` 一并清在途记录。
  - `agent-chat.ts`：`sendConversationTurn` 发起即写在途记录（「保存任务历史」开启时），delta/reasoning 节流 500ms 回写；**`pagehide`** 那一刻把所有在途轮同步落定为「回复在离开页面时中断，以上为离开前已生成的部分…」/「…尚未收到内容；请重新发送。」；成功清除；失败落定（note=「回复生成中断：<错误>」，用户暂停且一字未收 =「已暂停生成。」）。`configureConversation` 绑定归属时 `settleStalePendingTurn()`：同标签页留下的或超过 15 分钟无心跳的在途记录落定（软切换回来仍活着的、别的标签页正在生成的不碰）。history 重建：被打断且无正文的一对不进模型上下文（保住 user/assistant 严格交替），有半截正文的照常进入。开场分析被打断只清记录不留条目（规划阶段重进会按既有防重逻辑重发）。
  - 页面层：任务页 `appendRestoredReply` 与首页 `restoreHomeChat` 识别 `interrupted` 条目——半截正文照渲染、思考照回看盒、下方一行 `.muted.reply-interrupted` 灰字说明（`workflow-refresh.css`：有正文时虚线隔开），无正文不挂复制按钮；`restoreHomeChat` 读记录前先 `settleStalePendingTurn`。en-US 词典补两句。
- 契约与后端不动：`/api/chat` 依旧无状态，服务端不知道也不需要知道这轮被掐断。

当日执行并通过：`node --test src/tasks/conversation-log.test.mjs`（15 项，新增 interrupted 解析、在途记录往返、落定追问/开场、清理联动）与 `src/i18n/en-US.test.mjs`；`npm run check`；`npm run build`（index 728.06 kB）。浏览器复现验收待用户：任务页追问 → 思考/生成中点侧栏另一个任务 → 回来应看到自己的提问 + 半截回复（或空回复）+ 灰字说明；首页对话同理。

> 用户驳回（同日）：「不能光靠一句子来解决。你数据没有存好也没有做到持续性。」——上面这套「留痕」方案已被下一条整体替换；`chatPending` 在途记录与 `pagehide` 落定机制已移除，本机记录降级为只读兜底。

### 2026-09-05 用户驳回后重做：对话生成改为服务端托管的后台作业，记录落库（ADR-0016）

用户要求的是**持续性**（切走再回来，回复应该完整或仍在生成）与**数据存好**（不是浏览器内存/localStorage），不是一句中断说明。根因是「浏览器是生成的宿主」：`/api/chat` 无状态转发，页面一卸载生成就死。重做为：

- **后端**
  - `orm.ChatTurnRow` / 迁移 `0019_chat_turns`：`chat_turns(id, user_id, scope_id, status, opening, text, attachments, reply, reasoning, meta, error_code, error_message, trace, created_at, updated_at, ended_at)`，索引 `(scope_id, created_at)`；`test_migrations` 的 ORM↔迁移一致性照常约束。
  - `omm_api/chat_turns.py` **`ChatTurnHub`**（`app.state.chat_turns`，进程内单例）：`start` 建行后在守护线程里跑 `llm.stream_events`，事件按 `seq` 缓冲、正文/思考每秒批量回写库、终态后缓冲保留 10 分钟；`stream_live(after)` 用条件变量推事件 + `: ping` 心跳；`stop` 立即定格为 `stopped`（工作线程丢弃上游迟到事件并关连接）；`recover_interrupted()` 在 lifespan 启动时把遗留 `running` 标 `interrupted`（文案「服务重启，本轮生成中断；需要完整回答请重新发送」）；`persist=False` 时只在内存托管（「保存任务历史」关闭）；`delete_scope(s)` / `delete_user_turns` 供删除与隐私联动；用量只在正常完成时记一次（partial/stopped 不记）。
  - `routers/chat.py`：把配置/预算/视觉/端点校验抽成 `_prepare_call`，Auto 路由判定延后到后台线程里（`_resolve_chain`），用量记账钩子 `_usage_hooks` 用独立会话——旧 `POST /api/chat` 复用同一套、行为不变（67 项既有测试通过）。新增 `POST /turns`（202）、`GET /scopes/{scope}/turns`、`DELETE /scopes/{scope}`、`GET /turns/{id}`、`GET /turns/{id}/events?after=`、`POST /turns/{id}/stop`、`PATCH /turns/{id}`（trace）。`schemas.ChatTurnStartRequest`（`scope_id` 形如 `^(run|chat)_[A-Za-z0-9]{6,40}$`）与 `ChatTurnTraceRequest`。SSE 生成器不再触碰请求作用域的 DB 会话（FastAPI 在流式开始前就会关闭依赖会话）。
  - 联动：`privacy._delete_runs` 先删 `chat_turns`（项目删除级联）；`PUT /api/account/privacy-settings` 由开到关时 `delete_user_turns`。
  - 新增 `backend/api/tests/test_chat_turns.py`（17 项：后台完成落库 + 单条用量、无观众照常完成、`after=` 续播、停止冻结半截并关上游、停止且一字未收 → `GENERATION_STOPPED`、上游 500 → failed、409 并发、开场轮空 text、trace 往返、非本人运行 404、`chat_` 归属按用户隔离、非法归属 422、历史关闭 → 只在内存、关闭历史删库、删归属停生成、删项目级联、重启标 interrupted）。
- **前端**
  - 新增 `integration/chat-turns-api.ts`（类型化客户端 + SSE 解析 `readChatTurnEvents`）；`agent-chat.ts` 重写为托管轮路径：`sendConversationTurn` 有归属 → `runHostedTurn`（建轮 → `onTurnStarted` → `followTurn`：附着、`AbortSignal` → 服务端 `stop`、断线按 `after=seq` 重连 ≤5 次、失败拉视图补正文、`settleTurn` 统一收口），无归属 → 旧无状态路径；新增 `hydrateConversation(scope)`（拉全部轮，已定格的进 history）、`attachConversationTurn(turn, handlers, signal)`（续接 running 轮）、`entryFromTurn`（轮 → 页面条目：stopped/failed/interrupted/中途出错 → `interrupted + note`）。移除 `pendingTurns` / `pagehide` / localStorage 写入 / `omm:chat-turn-committed|failed`。
  - `modeling-workspace-controller.ts`：首个快照到手 → `configureConversation` → **`await hydrateConversation(run)`** → 广播 `omm:conversation-restore{runId, goal, turns}` → 才 `renderWorkspace`；并发刷新同样等这一步（`conversationReady` Promise），规划阶段不会重复发起开场分析。
  - `legacy/openmathmodel-ui.ts`：`omm:conversation-restore` 先渲染本机旧记录，再按时间重建服务端的轮（开场轮 → `restoreOpeningReply`；running 开场轮 → `resumeOpeningTurn` 原位续播并落防重标记；追问轮 → 用户气泡 + `appendRestoredReply(entryFromTurn)`；running 追问轮 → `resumeFollowUpTurn`）；抽出 `createReplyPresenter`（思考块 + 流式 Markdown + 域名透明行 + Auto 难度行）、`settleGeneratingRow`、`presentReplyFailure` 供首发 `streamAssistantReply` 与续接 `attachAssistantReply` 共用；轨迹行改为回复完成后 `PATCH /turns/{id}`（不再写 localStorage）；`appendReplyTraceRow` 支持 `startedAt`（续接的生成计时从服务端建轮时刻起算）；`omm:run-planning` 不再读在途记录，只看防重标记与页面上是否已有开场块。
  - `home-chat.ts`：`restoreHomeChat` 改为 async（本机旧记录 + `hydrateConversation(chat)`，running 轮续接直播），首发与续接共用 `presentReply`；`task-start-controller.ts` 相应改为 `.then(ok => !ok && resetHomeChat())`。`recent-tasks.ts` 删除首页对话时先 `DELETE /api/chat/scopes/{chat}`（文案改为「全部消息将被清除」）。
  - `tasks/conversation-log.ts` 精简为只读兜底（`parse/load/clear*`，导出 `sanitizeTrace`），删除 `PendingTurnRecord` 全家；测试 8 项。en-US 词典：删两句已废文案、补「已暂停生成，以上为暂停前已生成的部分。」「回复在生成中出错，以上为出错前已生成的部分。」「服务重启，本轮生成中断；需要完整回答请重新发送」「回复仍在服务端生成中，正在续接…」。

当日执行并通过：`pytest backend/api/tests` 全量 360 通过 / 2 跳过（含新文件 17/17、`test_migrations`、`test_llm_chat`）；`npm run check`；`npm run build`；`node --test "src/**/*.test.mjs"` 107/107。受保护的 React 入口未改。**后端进程需重启**（`npm run dev` 的 uvicorn 不带 `--reload`；开发库由 `create_all` 自动建 `chat_turns`，部署环境跑 `alembic upgrade head`）。浏览器验收待用户：任务页追问 → 思考中点侧栏另一个任务 → 点回来应看到完整回复或仍在直播的半截 + 后续增量；刷新同理；暂停键应真正停止服务端生成（`GET /turns/{id}` 状态 `stopped`）。

同日实机联调（对本机 `uvicorn --reload` 的开发后端 + PostgreSQL，用本机模拟 OpenAI 协议 SSE 服务和一次性用户，不入库的临时脚本）23/23 通过：观众 1.6s 后断开 → 无观众继续生成 → `after=seq` 续接只补尾段、拼起来等于完整回复 → 落库行一致；`stop` 保留半截且 ~1s 内关掉上游连接；运行中再建轮 409；touch `main.py` 触发 uvicorn 重载后遗留轮变 `interrupted`（半截保留、事件流只回 `GENERATION_INTERRUPTED`）；用量只记完成轮；`DELETE /scopes/{id}` 级联清空。开发库 `alembic_version` 由 0018 手动 `stamp` 到 0019（表已由 `create_all` 建出）。踩坑：Windows 系统代理会让 `httpx` 默认把 127.0.0.1 也走代理（502 空响应），测试客户端需 `trust_env=False`；后端 `llm._direct_mounts` 早已对本机目标绕过代理。

### 2026-09-06 模型厂商型号与单价改为服务端同步的模型目录（ADR-0017）

用户看到「模型厂商」卡片仍是 `claude-fable-5 / gemini-3.6-flash`，而 Claude、Gemini 都已上新：「难道每次都得重新写前端吗？不能实时同步上吗」。此前型号（`llm-providers.ts` `PROVIDER_PRESETS[].models`）与单价（`usage.py` `PRICING`）都是代码里的快照，上一次手工更新是 09-03。改为服务端从公共目录同步（数据源、裁剪规则与取舍见 ADR-0017，接口与字段见 §5.7）：

- **后端**
  - 新增 `omm_api/model_catalog.py`：厂商预设 `PROVIDERS`（唯一的型号事实来源，含内置兜底快照）、`reduce_catalog`（models.dev `api.json` → 8 家厂商的可对话型号 + 全目录单价索引）、`highlights_of`（最新三款正式版，读取时现算）、`ModelCatalog`（文件缓存 `data/model-catalog.json` + 后台线程按 TTL 刷新 + `view()` / `pricing_cny()` / `vision()`；失败保留旧数据并记 `error`，强制刷新最小间隔 60 s，`CACHE_VERSION` 变化即作废旧缓存）、进程级访问点 `set_current/current`。
  - `config.py` 新增 `model_catalog_enabled / model_catalog_url / model_catalog_ttl_seconds / model_catalog_timeout_seconds / model_catalog_cache_path / usd_cny_rate`；`main.py` 在 `create_app` 建目录并 `set_current`，lifespan 启停后台线程。
  - `routers/chat.py` `llm_router`：`GET /catalog`、`POST /catalog/refresh`（要求登录；关闭同步 409 `MODEL_CATALOG_DISABLED`；拉不到 502 `MODEL_CATALOG_UNREACHABLE`）。
  - `usage.model_pricing`：先按模型 ID 精确命中目录单价 × 汇率，再走手写前缀表，再兜底价；手写表降级为「目录没有时的兜底」。
  - 新增 `tests/test_model_catalog.py`（16 项：非对话 / 已下线过滤与新在前排序、Gemini 卡片只收 gemini 家族、亮点正式版优先 + 测试版垫后 + 别名不进亮点、DashScope 只收 qwen 家族、智谱备用键、单价索引官方优先 + 别的平台补缺 + 不过滤已下线、空 / 无关目录拒收、刷新写缓存 + 重启读缓存不出网、旧版本 / 损坏缓存忽略、失败保留旧快照并报错、TTL 与强制刷新间隔、单价换汇与大小写、`usage.model_pricing` 目录优先、接口未登录 401、关闭同步时内置快照 + 409、refresh 返回新视图）。`conftest` 夹具 `model_catalog_enabled=False` 且缓存路径指向 `tmp_path`。
- **前端**
  - `auth/api.ts` 新增 `CatalogModel / CatalogProvider / ModelCatalogView` 与 `getModelCatalog / refreshModelCatalog`；新增 `integration/model-catalog.ts`（会话内缓存、refresh 替换缓存、`currentModelCatalog` 同步读）与 `integration/model-catalog-view.ts`（纯函数：`providerHighlights / providerModels / catalogVision / catalogFreshnessText`）+ `model-catalog-view.test.mjs`（4 项）。
  - `integration/llm-providers.ts` 去掉全部模型名，只留品牌骨架；`legacy/openmathmodel-ui.ts`：卡片副标题占位「正在同步型号…」→ 打开设置时 `loadModelCatalog` 填亮点；区块标题下 `[data-catalog-status]` 新鲜度行；标题右侧「同步型号」按钮（`refresh-catalog`，`withBusyButton`，失败 toast 并保留上一份）；「配置」默认模型 = 亮点首位；`catalogModelsForHost` 取代 `seedModelOptions`，`refreshModelOptions` 合并接口自报清单；`hydrateLlmPanel` 回填时目录晚到会补一次补全。`styles.css` 新增 `.settings-heading-actions` 与状态行换行样式。
  - `integration/model-modality.ts`：`resolveSelectedModality` 两条分支先查目录 `vision`（`classifyModel`），未收录再回落命名规则。en-US 词典补 13 条。
- 不动：`App.tsx` / `screens.tsx` / `OpenMathModelScreen.tsx`；页面结构、路由、DOM 槽位；Auto 路由能力推断 `endpoint_strength`。

当日执行并通过：`pytest backend/api/tests` 全量 377 通过 / 2 跳过；`npm run check`；`npm run build`（index 735.61 kB）；`node --test "src/**/*.test.mjs"` 117/117。实机（本机 `uvicorn --reload` 开发后端自动重载）：启动后约 1 s 完成首次同步并落 `data/model-catalog.json`（≈156 KB，8 家厂商、3170 条单价）；一次性用户 `GET /api/llm/catalog` 显示 Anthropic `claude-fable-5-1 / claude-opus-5 / claude-sonnet-5`、Google `gemini-3.8-flash / gemini-3.7-flash / gemini-3.5-flash-lite`、通义 `qwen3.8-flash / qwen3.8-max / qwen3.7-plus`、智谱 `glm-5.3-flash / glm-5.3 / glm-5.2`、xAI `grok-4.6 / grok-4.5 / grok-4.3`；`POST /catalog/refresh` 200 并更新 `synced_at`；未登录两条都 401。踩坑：第一版按 `family` 取「每家族最新」在真实数据上把 `gemma-4-26b` / `qvq-max` / `glm-4.7-flash` 顶进卡片，改为按时间取最新 + Google 只收 `gemini` 前缀；亮点改为读取时现算，缓存文件版本升到 2 强制重拉。浏览器验收待用户（本环境无浏览器）：卡片副标题与状态行、「同步型号」按钮、「配置」默认模型、模型 ID 补全。

### 2026-09-06 报障修复：任务出问题后对话里说「继续 / 重做」只是普通聊天（ADR-0018）

截图现场：「机器人竞技攻击策略优化」实验运行阶段 FAILED，用户在聊天框说「怎么又失败了，快继续想办法」「继续啊」——前端只把消息 `POST /notes`（终态 409）、标「任务已结束，本条按问答处理」并挂一个对失败运行必死（`RUN_NOT_COMPLETED`）的「按这条要求继续修改」按钮，模型也不知道运行状态。根因：对话通道只有被动的备注注入、没有执行权，且 §11.3 旧决策明文「重做由人显式操作，绝不由备注文本触发」。用户拍板：分级执行（retry / resume / pause / 审批选项直接做；取消先确认）、已完成运行仍开修订门但点名阶段预选、失败运行的「选起点重做」留下一刀。改动（接口与页面行为见 §5.8，决策见 ADR-0018，设计文档 §11.3 已回写 v3.34）：

- **后端**
  - 新增 `omm_api/run_control.py`：`legal_actions`（与状态机 / ADR-0013 同判据）、`load_context`（待审批门选项、修订轮数、上一轮待确认提案）、`decide_locally`（词表规则：重试 / 恢复 / 暂停 / 取消 / 退回 / 点名选项 `match_option` / 点名阶段 `explicit_stage`；否定词、「继续说」类对话延续、问句标记）、`judge_intent`（池里最弱接口 `complete_once`，10 s / 200 token，只认合法集内结果）、`decide`（本地 → 出网门槛：≤200 字、带合法动作的气味词、非问句）、`execute`（走 `execute_action` / `accept_revision` / `record_run_note`；`ApiError` → `rejected` 回执）、`prompt_block`（状态块 + 「不得声称执行了未列出的操作」）、`run_control_step`（独立 session、一个事务；任何异常回落为无动作）。
  - `engine_glue.py` 抽出 `accept_revision(session, run, text, stage=None)`（`request_revision` 可点名推荐起点），`routers/task_runs.py` 的 `/revisions` 与 `/notes` 改为委托（`record_run_note` 落到 `run_control`）。
  - `routers/chat.py` `start_chat_turn`：`run_…` 归属且非开场的轮，producer 先跑控制步骤、`yield` `action` 事件、把状态块追到 system 提示词后再 `_resolve_chain` + `stream_events`；上一轮 `meta.actions` 作为 `previous_actions` 传入。`chat_turns.py` `_run` 把 `action` 事件累进 `live.meta["actions"]` 随轮落库。
  - 新增 `tests/test_run_control.py` 23 项（合法动作集；失败 / 暂停 / 执行中 / 审批门 / 修订门 / 已完成的本地规则与反例；字母 / 序数 / 别名选项点名；提案确认 / 放弃 / 作废；判定回复解析只认合法结果；出网门槛；提示词块；e2e：「怎么又失败了，快继续想办法」→ 运行 RUNNING + 备注落库 + 首事件 `action` + `meta.actions` 持久化 + 系统提示词带执行后状态且未出网判定；问句只聊不动；取消两轮确认 / 放弃 / 提案过期；「同意，按这个方案做」选定 G1；已完成运行「从数据准备重做，…」开门并预选 → 「确认」重跑；弱模型判定生效；判定 500 回落对话；`chat_` 归属不经控制）。e2e 钉 `OMM_AGENT_NODES=sim` 让引擎仍走模拟链、mock 只服务对话与判定。
- **前端**
  - `chat-turns-api.ts` `RunControlAction` / `ChatMeta.actions`；`agent-chat.ts` `ChatHandlers.onAction`、`applyEvent` 处理 `action`、续接重放 `meta.actions`、`entryFromTurn` 回执兜底重建轨迹；新增 `integration/run-control-view.ts`（纯函数）+ `run-control-view.test.mjs`（4 项，含「所有标题静态且有英文词条」）。
  - `legacy/openmathmodel-ui.ts`：`presentRunControlAction`（回执 → 轨迹行，插在生成行之前；`proposed` 挂「确认执行」= 发一句「确认」；`executed` 广播 `omm:run-reopened`）；恢复态最后一轮的待确认提案照挂按钮（按落盘轨迹下标定位，不比文本——语言切换会翻标题）；删除发送前 `postRunNote` 与 `offerRevisionCta` 死路行；en-US 补 13 条。
- 不动：`App.tsx` / `screens.tsx` / `OpenMathModelScreen.tsx`；页面结构、路由、DOM 槽位；引擎状态机与 `/actions` 端点形状（OpenAPI 未变）。

当日执行并通过：`pytest backend/api/tests`（见下方全量结果）；`npm run check`；`npm run build`；`node --test "src/**/*.test.mjs"` 125/125。浏览器验收待用户（本环境无浏览器）：失败态说「继续」后轨迹行「已重试阶段 · 实验运行」+ 工作台状态即时变为执行中；「取消任务」→「等待你确认操作」行带按钮 → 点按钮或回「确认」→ 已取消；已完成态「从数据准备重做…」→ 待确认事项里数据准备预选。

### 2026-09-06（追加）用户追问：不能只靠关键词；任何要执行的修改都要真正回退重做（ADR-0019）

用户原话：「你现在只是通过固定的关键词来识别判断吗？agent 自己没有相关的意图识别吗？我希望不管说什么，只要是需要执行的，都会出现真正的修改，而且这个修改显示的进度，也不一定要接着上一次的显示啊」。如实答复的现状：规则先判、弱模型判定被 ≤200 字 / 气味词 / 非问句三道门槛拦住、判定看不到上文、动作被状态机锁死（FAILED 只能 retry，RUNNING 下的修改要求只静默落备注）。用户拍板：规则只做第一层门槛、模型识别必须有；同一运行从选定阶段重做（不另起新运行）；重做先提案再确认。改动（接口与行为见 §5.9，决策见 ADR-0019，ADR-0018 §1 / §2 / §5 标注已修订）：

- **引擎**（`agents/core`）：新增 `EventType.RUN_REDO`；reducer `_on_run_redo`（合法源 = 工作态 / FAILED / NEEDS_REVIEW；在途 RUNNING 步骤 → CANCELLED `superseded: redo from <target>`；清 review / failure / paused / cancel_requested；`state = target` + `force_rerun` + `_discard_from(target)`）；`TaskRunEngine.redo(snapshot, target_state, reason, note_id)`（COMPLETED / CREATED 拒绝）；`can_transition` 前向矩阵不放宽（`states.py` 注明回退边只在 RUN_REDO reducer 里）。`agents/evals` `CONTROL_FLOW_FIELDS` 登记 `RUN_REDO(target_state, from_state)`。测试：`test_engine.py` +3（FAILED 回退更早阶段、在途步骤被取代且丢弃评审、拒绝边界），`test_replay.py` +1（两次 redo 后重放 == 实时快照）；core 130 / evals 48 通过。
- **后端**：`engine_glue.py` `RUN_REDO` 投影 + `REDO_STATUSES` + `redo_run(session, run, stage, text)`（409 `RUN_NOT_REDOABLE` / 422 `INVALID_STAGE` / 422 `EMPTY_TEXT`；备注先 flush 再发事件；run.log `redo_requested`）；`runner.py` `_superseded_in_flight` + `WorkflowAdvancer.advance` 只吞 `run_domain_events` seq 唯一约束冲突并 warning，其它 IntegrityError 照抛；`run_control.py`：`ACTION_KINDS` + `redo`、`CONFIRM_REQUIRED = {cancel, redo}`、`legal_actions(..., revision_gate=)`（FAILED→{retry, redo}；RUNNING / PAUSED / 节点闸门 + redo；修订门不开）、本地规则「点名阶段 + 修改 / 重试词」→ redo（点名失败阶段折 retry）、`decide` 改为「提案回应 → ≤12 字规则直判 → 模型（≤800 字，带最近三轮对话）→ none 回落规则」、`_judge_prompt` 带阶段进度 / 最近对话、`_parse_judge_reply(user_text=)` 阶段缺失按正文推断 / 未开始阶段 → none、redo 提案 / 执行 / 放弃回执；`routers/chat.py` 从 `hub.list_scope` 取最近三轮 `{text, reply}` 传 `history`。
- **后端测试**：`test_run_control.py` 28 项（重写出网门槛用例为「快路径 + 全覆盖 + none 回落」；新增本地 redo 规则、判定 redo 解析边界、e2e：FAILED 上一句无词表词的「换成随机森林…」经模型判 redo → 提案（状态不变、不落备注）→ 确认 → RUNNING@建模方案、备注正文 = 原话、tick 后 attempt 2 到 G1；G1 门上「从数据准备重做」→ 确认 → 门 CANCELLED、数据准备 attempt 2；未开始阶段的要求静默落备注；「算了」放弃；判定提示词带「最近对话」）；新增 `test_run_redo.py` 5 项（`redo_run` 回退 + 备注 + run.log；状态 / 输入边界；`_superseded_in_flight` 两种方言；**真在途让位**：模拟节点执行中途从另一会话 `redo_run` → 本 tick 返回 None + warning、步骤行 CANCELLED、下一 tick 从目标阶段 attempt 2 起跑、不触发 executor lost；无关 IntegrityError 照抛）；`test_chat_turns.py` 的上游 mock 统一把判定请求答 none（判定不再被门槛拦后，阻塞流会被判定调用吃掉前几段）。全量 `pytest backend/api/tests` 419 通过 / 2 跳过（PG 专属）。
- **前端**：`run-control-view.ts` `redo` 标题「已从阶段重做」/ 类别「从阶段重做」，提案与放弃行后缀带阶段名；`chat-turns-api.ts` `RunControlAction.text`；`run-control-view.test.mjs` +1（5 项）；en-US +1。`npm run check` / `npm run build` / `node --test` 129/129 通过。
- 不动：受保护入口、页面结构与 DOM 槽位、`/actions` 端点形状（无新 HTTP 端点，重做只经对话确认触发）、契约 `Resolution` 形状。

浏览器验收待用户：失败态说一句不带任何固定词的修改要求（如「换成随机森林比较靠谱」）→ 轨迹行「等待你确认操作 · 从阶段重做 · 建模方案」带按钮 → 确认 → 「已从阶段重做 · 建模方案」+ 工作台进度回到建模方案继续、上游两段保留；执行中说「从题意解析重做」→ 确认 → 当前步骤标已取代、下一步从题意解析起。

### 2026-09-07 报障修复：对话触发重试后页面同时出现两个 Agent 气泡

用户实机走查 ADR-0018 路径的截图：实验运行 FAILED → 回「按典型参数继续」→ 上面一个 Agent 气泡「已重试阶段 · 实验运行 ✓ / 正在生成回复 / 思考中…」，下面又一个 Agent 气泡「收起执行步骤 / 重试失败阶段。/ 已记录补充要求…（JSON 行）/ ws_list / env_probe / 深度思考 · 实验执行」，两个同时在转；第二张截图里上面那个气泡的思考正文在逐项算分。读码根因：

1. 渲染：`tailTraceHost` 只认「尾部是轨迹块才续写，否则另起带署名的新块」。对话即控制面之后，动作由对话轮自己触发，`executed` 回执 → `omm:run-reopened` → 控制器重接 SSE，第二次尝试的事件到达时尾部正是还在生成的回复块，于是另起一块。
2. 内容：`prompt_block` 的回复要求「先告知已执行的操作，再回答用户的问题」把「按典型参数继续」当成了问题，模型在对话里替运行去算典型参数下的结果——聊天模型与运行里的实验节点并行解同一道题。
3. 顺带：`run.log{kind:"user_note"}` 没有专门分支，落进「其他 run.log 原样 JSON」兜底（terminal 图标 + 可展开的原始 JSON，含备注全文与 note_id）。

改动（用户拍板：三项一起做）：

- **前端** `integration/modeling-workspace-controller.ts`：抽 `activityHeader()`；新增 `replyRunTraceHost(replyBlock)`（回复块内只建一次的折叠头 + `.agent-stream.run-trace`）；`tailTraceHost` 加第二条规则「尾部是 `.follow-up-reply` → 写进该回复块」；`user_note` 并入「有现成人话 message 的运营事件 → 叙述行」分支。`legacy/openmathmodel-ui.ts`：`appendReplyActions` 改为紧跟 `.analysis-copy` 插入；`toggle-activity` 优先折叠折叠头紧随其后的列表。`workflow-refresh.css` revision 38：折叠头贴正文时 4px 上间距。
- **后端** `run_control.py`：`_HANDOFF_KINDS` / `reply_rules(actions)` 三档回复口径（交接 / 提案待确认 / 默认），`prompt_block` 末行改用它；导出 `reply_rules`。`tests/test_run_control.py` +1（`test_reply_rules_hand_off_to_the_run_after_executed_actions`：六种交接动作同一口径、提案口径、rejected / dismissed / pause / cancel 与无动作走默认、`prompt_block` 末行一致）。
- 不动：受保护入口、路由、DOM 槽位类名、`/actions` 与轮视图契约（`action` 事件形状不变）、刷新重进路径（历史事件仍回放到首气泡、对话轮在其后重建）。

当日执行并通过：`pytest backend/api/tests/test_run_control.py` 29 项；`npm run check`；`npm run build`（index 745.58 kB）；`node --test "src/**/*.test.mjs"` 137/137。浏览器验收待用户（本环境无浏览器）：失败态回一句让运行继续的话 → 只有**一个** Agent 气泡：轨迹行「已重试阶段 · 实验运行」→ 简短交接回复（不再自己算题）→ 复制按钮 → 「收起执行步骤」+ 第二次尝试的步骤在同一气泡里往下走；点折叠头只收起步骤区、回复轨迹行不受影响；「已记录补充要求…」为一行叙述、无 JSON 展开；再发一条消息后新的步骤落到新回复块里。已知但未动：首气泡封口后（题意解析完成）没有对话时，后续阶段的步骤仍按既有设计另起一个「收起执行步骤」轨迹块，与首气泡相邻——属 2026-08-21 起的既有形态，如需一并合并再议。

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
