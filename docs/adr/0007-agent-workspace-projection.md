# ADR-0007：以语义工作台投影连接 Agent 与现有流程页面

- 状态：Accepted
- 日期：2026-08-11
- 关联：ADR-0006

## 背景

OpenMathModel 已有稳定的建模流程页面，也已有 TaskRun、StepRun、Approval、AgentEvent 和 Artifact 控制面接口。但两侧此前没有共同的页面语义：

- 左侧 Agent 步骤、摘要和按钮由静态模板生成；
- 右侧数据、方案、实验、论文和交付页面也使用演示内容；
- 后端 `current_node`、审批和 Artifact 不能直接、安全地映射到现有 DOM；
- 若让 Agent 直接输出 HTML 或让前端解析日志文本，会把领域状态、内容和表现耦合在一起。

## 决策

### 1. 增加只读语义投影

增加 `ModelingWorkspaceView` 共享契约及：

```text
GET /api/v1/task-runs/{run_id}/workspace
```

该投影聚合运行、步骤、审批、事件序号和 Artifact，不创建新的持久化事实。数据库领域事件与控制面表仍是事实来源。

首个切片明确从已有 `run_id` 开始：它负责恢复工作台语义、提交模型审批、监听 SSE 并展示真实 Artifact 清单；它不负责首页创建运行、五类详细正文、可见暂停入口、阶段业务动作或生产 Worker/Skills 接线。

后续 Phase 0 新任务控制链已在该投影上游落地：`task-start-controller.ts` 从首页/确认页创建真实 Project 和 TaskRun，再把返回的 `run_id` 交给本 ADR 定义的恢复协议。该补充不扩大 `ModelingWorkspaceView` 的正文职责；附件当前仍以元数据进入 TaskRun 参数。

### 2. 后端不输出页面实现细节

工作台投影只含：

- 运行与项目身份；
- 当前节点与建议页面；
- Agent 纯文本摘要和允许动作；
- 页面状态；
- 审批与产物引用。

它不含 HTML、Markdown 片段、CSS 类名、DOM 选择器或坐标。页面结构和视觉样式继续由 Web 基线拥有。

### 3. 一份状态协调左右阶段语义，不替代右侧正文

`ModelingWorkspaceView` 必须同时用于：

- 左侧真实步骤时间线；
- Agent 当前摘要；
- 主按钮文案、禁用态和后端动作；
- 右侧页面的阶段身份与状态属性；
- 实验与交付页面的 Artifact 文件列表。

不得为 Agent 左栏与右侧页面分别推导当前阶段。数据指标、角色化方案、实验图表、论文正文和成果摘要不属于该投影；它们仍由第 7 节的独立版本化契约提供。将阶段状态写入右侧 `data-*` 属性不等同于详细正文已经真实接入。

### 4. 快照负责恢复，SSE 负责通知

首屏与刷新从 workspace 快照恢复。Web 以 `latest_event_sequence` 订阅 SSE；事件到达后重新获取快照。

不使用事件历史拼装长期页面正文，也不把大对象塞进 SSE。后续阶段输出只在事件中发送版本和内容哈希指针。

### 5. 运行身份随 URL 传播

`run_id` 是真实页面恢复的首要身份，`project_id` 用于上下文。URL 显式 `demo=1` 时优先进入演示状态并忽略历史 active run；否则，URL 中存在且合法的 `run_id` 优先并写入 sessionStorage，URL 没有该参数时才读取同一标签页的合法 sessionStorage 值，URL 显式提供非法值时不回退 sessionStorage 并进入 demo 状态。控制器把成功快照中的身份保留在所有流程页链接和阶段跳转中，不以本地状态取代服务端事实。

`suggested_route` 只是导航建议。当前页面与 `active_page` 不一致时不自动跳页，控制器只把 Agent 主操作转换为 `navigate`；错页加载及该导航都不提交 `/actions`，不改变运行状态。

### 6. 审批按钮提交真实动作

当 `agent.action.kind=approve` 时，前端提交：

```text
POST /api/v1/task-runs/{run_id}/actions
Idempotency-Key: <client token>
```

请求包含 `approval_id` 与 `option_id`。页面只在后端响应与后续快照确认状态后更新，不先行伪造“已审批”。底层 actions 与控制器支持暂停、恢复和重试的相同提交规则，但当前投影只产生 `resume` 与 `retry`，不产生 `pause`，页面也没有可见暂停入口。

`agent.action` 是按 `kind` 判别的联合契约。后端仅在审批只有一个非 `reject` 选项时预填 `option_id`；多个方案必须等真实 `PlanProposal` 页面显式提交选择，不能默认取第一项。

### 7. 页面详细内容采用独立版本化契约

`ModelingWorkspaceView` 不承载全部数据表、实验图表或论文正文。后续分别增加 `DatasetProfile`、`PlanProposal`、`ExperimentSummary`、`DocumentDraft` 和 `DeliveryManifest`，由现有页面控制器填入固定槽位。

### 8. Artifact 状态决定下载能力

workspace 投影保留 `PENDING | READY | STALE | DELETED` 状态。只有 READY 且 `uri/sha256` 完整的 Artifact 获得 `download_url`；Web 对其他状态保留真实文件行但禁用下载。完成页与实验页展示的是文件清单，底部操作导出 TXT 清单，不在浏览器中生成 ZIP 或合并归档。

## 结果

正向结果：

- Agent 和流程页使用同一阶段投影；错页仍保留当前模板，并通过显式导航前往建议页面；该投影当前只协调右侧阶段状态和 Artifact，不生成详细正文；
- URL 或同标签页 sessionStorage 中存在合法 `run_id` 时可恢复；
- 审批与 SSE 形成可验证闭环；
- 前端保持当前视觉，后端可继续替换执行实现；
- 未知节点仍可回退到任务执行页，不阻断页面。

代价与约束：

- 每个新增状态或页面都要更新共享映射和契约 Fixture；
- 快照刷新比纯事件局部更新多一次读取，但换来一致性与恢复简单性；
- 页面正文仍需后续结构化契约，不把当前模板数据误标为真实 Agent 结果。

## 验收要求

1. JSON Schema、生成的 Python/TypeScript 类型与 Fixture 同步。
2. 跨账户读取 workspace 返回 404。
3. `MODEL_PLANNING` 待审批映射到方案页和真实 approve 动作。
4. 审批后 SSE 能使 Agent 时间线进入下一阶段或终态。
5. Artifact 的名称、类型、大小和状态进入现有文件表；只有 READY 行获得可用下载按钮。
6. `demo=1` 显式绕过历史 active run；URL 未提供 `run_id` 且 sessionStorage 也无合法值时保持原演示基线；URL 显式非法时不回退 sessionStorage。
7. 错页打开运行时只呈现导航，不自动跳页、不调用 `/actions`、不改变 TaskRun。
8. 文件清单导出为 TXT，不把清单描述成已生成的交付压缩包。

## 当前已验证证据

已执行命令、测试数量与人工浏览器验收的唯一清单见[工作台对接规范 §13](../development/frontend-backend-agent-integration.md#13-当前已验证证据)，本 ADR 不维护重复副本。Web 控制器 DOM、SSE 重连、URL/sessionStorage 分支、错页导航和非 READY 下载禁用态尚无自动化浏览器测试；它们仍属于上节自动化验收要求。
