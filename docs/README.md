# OpenMathModel 开发文档

> 文档事实基线：2026-08-11。阅读顺序为“当前事实 → 开发合同 → 决策依据 → 路线图 → 历史证据”。代码、Schema、OpenAPI 与已执行测试优先于文字摘要。

## 快速导航

| 目的 | 首选文档 | 说明 |
|---|---|---|
| 了解现在实际运行什么 | [系统架构](./architecture/system-overview.md) | 明确区分当前 API 内嵌 Runner、独立 Worker 原型和目标部署架构 |
| 了解目录与依赖 | [项目结构](../PROJECT_STRUCTURE.md) | npm/Python workspace、包布局与当前依赖方向 |
| 启动完整本地联调 | [根 README](../README.md#快速开始) | 推荐 `npm run dev` 同时管理 API 与 Web |
| 配置 API 环境与数据库 | [`backend/api/README.md`](../backend/api/README.md) | `OMM_*` 环境变量、SQLite/PostgreSQL 切换与测试方式 |
| 对接 Web、API 与 Agent | [工作台对接规范](./development/frontend-backend-agent-integration.md) | 运行身份、快照、SSE、动作、DOM 投影和正文契约顺序 |
| 修改现有 Web 页面 | [Web 页面基线](./development/web-ui-baseline-and-api-integration.md) | 14 条路由、稳定槽位、视觉与浏览器验收门禁 |
| 查看下一阶段 | [产品路线图](./product/roadmap.md) | Phase 状态、退出标准和当前优先级 |
| 理解决策原因 | [ADR 状态表](#adr-状态) | 当前有效、部分取代与已取代的决策 |
| 查阅某次验证 | [`implementation/`](./implementation/) | 按日期冻结的历史证据，不代表当前产品状态 |

## 当前开发状态

| 能力 | 状态 | 事实边界 |
|---|---|---|
| 14 个 Web 页面与视觉基线 | 已建立 | 页面、路由与交互顺序保持稳定 |
| 账户与安全 API | 已接入 | Web 登录依赖 API；完整联调必须同时运行 Web 与 API |
| Project / TaskRun / Step / Approval / SSE / Artifact API | 已实现 | 使用 Cookie 会话与 owner 隔离 |
| 新任务控制链 | 已接入 | 首页草稿→发送即登录续接并创建 Project/TaskRun→携带 `run_id/project_id` 进入执行页；`/confirm` 为直接访问的草稿复核入口；附件当前为元数据 |
| `ModelingWorkspaceView` | 首切片已接通 | 驱动项目名、Agent 时间线/摘要/动作、阶段状态、Artifact 元数据与下载 |
| 右侧阶段详细正文 | 模板为主 | 数据指标、角色化方案、实验图表、论文正文与成果摘要等待五类版本化契约 |
| 当前执行链 | 可运行的模拟闭环 | API 进程内 `RunnerThread` + `agents/core` + `SimStageNode` |
| 独立 Worker | 原型已验证、尚未接线 | 文件队列、租约、恢复、沙箱和产物能力未进入 API 请求链 |
| 目标数据面 | 待迁移 | 默认 SQLite + 本地 Artifact Store；目标为 PostgreSQL + 队列 + S3 兼容存储 |

## 事实来源优先级

同一信息出现差异时按以下顺序处理：

1. JSON Schema、OpenAPI、数据库模型与可执行代码；
2. 当前自动化测试与实际运行结果；
3. [系统架构](./architecture/system-overview.md)与两份 `development/` 开发合同；
4. Accepted ADR；若 ADR 已被取代，以取代它的 ADR 为准；
5. 路线图；
6. `implementation/` 历史验证快照。

页面内容归属也遵循固定边界：后端提供领域语义和版本化数据，前端拥有 HTML、CSS、DOM 与交互表现。Agent 输出不直接携带整页实现。

## 术语

| 术语 | 定义 |
|---|---|
| `Project` | 持续存在的建模项目与所有权边界 |
| `TaskRun` | 一次可恢复的工作流运行；`run_id` 是工作台恢复主身份 |
| `StepRun` | 某个领域节点的一次执行尝试 |
| `AgentEvent` | 运行内按 `sequence` 单调递增的事件信封，也是 SSE 历史来源 |
| `Artifact` | 带类型、状态、大小、URI 和 SHA-256 的运行产物 |
| `ModelingWorkspaceView` | 聚合 TaskRun、Step、Approval、Event 水位和 Artifact 的只读页面语义投影 |
| 阶段输出契约 | `DatasetProfile`、`PlanProposal`、`ExperimentSummary`、`DocumentDraft`、`DeliveryManifest` 等后续正文数据合同 |
| 当前 Runner | API 进程内的 `RunnerThread`，当前调用 `agents/core` 与模拟阶段节点 |
| 独立 Worker | `backend/worker` 中已验证但尚未由 API 调度的执行面原型 |
| 目标架构 | PostgreSQL、队列、独立 Worker 池和 S3 兼容存储组成的演进方向，不表示当前默认运行链 |
| UI 基线 | 已确认的 14 页面、布局、路由、DOM 槽位与交互顺序 |

## ADR 状态

| ADR | 状态 | 当前解释 |
|---|---|---|
| [0001 Monorepo 边界](./adr/0001-monorepo-boundaries.md) | Accepted | 目录职责继续有效；其中 `services/` 名称按 ADR-0005 读取为 `backend/` |
| [0002 本地底座与工具链](./adr/0002-dev-stack-baseline.md) | Accepted，事实表已形成历史快照 | 版本与基础设施原则继续有效；当前路径、workspace 成员和默认 SQLite 以最新架构文档为准 |
| [0003 Workspace 根](./adr/0003-workspace-roots.md) | Partially superseded | workspace、包命名和端口倒置有效；“不新增 backend”由 ADR-0005 取代 |
| [0004 Web 路由与数据层](./adr/0004-web-routing-and-data-layer.md) | Superseded | 替代页面、React Router 和 TanStack Query 方案未进入当前基线；由 ADR-0006 取代 |
| [0005 backend 目录](./adr/0005-backend-directory.md) | Accepted | `services/` 已更名为 `backend/` |
| [0006 保留 Web UI 并接 API](./adr/0006-preserve-web-ui-and-integrate-api.md) | Accepted | 当前 Web 对接总原则 |
| [0007 Agent 工作台投影](./adr/0007-agent-workspace-projection.md) | Accepted | 当前运行快照、动作和 Artifact 映射合同 |
| [0008 界面本地化](./adr/0008-interface-localization.md) | Accepted | 以 DOM 适配层翻译界面文案；真实数据与用户内容不参与翻译 |
| [0009 合并建模工作台](./adr/0009-merged-modeling-workspace.md) | Accepted | 五个阶段路由渲染同一工作台，面板软切换；URL 保留为面板别名 |
| [0010 附件图片计数与模型模态感知](./adr/0010-attachment-modality-awareness.md) | Accepted | 附件解析统计图片数并如实展示；纯文本模型配图片附件时发送前提醒；视觉解析与对话附件按批次落地 |
| [0011 编排选型：状态机与有界循环](./adr/0011-orchestration-state-machine-and-bounded-loops.md) | Accepted | 运行拓扑唯一由显式状态机定义，P4 用节点注册表替换模拟节点；循环分层有界并事件化；不引入通用图编排 |

## 当前文档与历史记录的边界

- `architecture/`：描述当前物理事实和目标演进。
- `development/`：可执行的开发与验收合同。
- `adr/`：记录当时的决策、取代关系和持续约束。
- `product/`：阶段目标、状态与退出标准。
- `implementation/`：特定日期、特定工作树的命令与输出快照；路径、测试数量和默认配置可能已经变化。
- `assets/`：README 和 UI 基线使用的截图、架构图。

历史记录保留原始路径和结果以维持证据完整性。需要当前命令时，从根 README、系统架构或对应模块 README 获取。

## 文档维护规则

1. 已实现、部分接入、原型和目标态必须分别标记。
2. 页面正文接入先更新 Contracts 与 API，再更新现有 DOM 槽位；不得把模板值描述成真实 Agent 结果。
3. 新 ADR 要更新本页状态表；取代旧 ADR 时同时补充双向关联。
4. 运行数字只写入“当前已验证证据”并附日期；后续结果变化时更新当前文档，不改写历史快照。
5. 相对链接、Markdown fence、构建和相关测试应在提交前校验。
