# ADR-0009：建模流程五页合并为单一工作台（面板软切换）

- 状态：Accepted
- 日期：2026-08-12
- 关联：ADR-0006、ADR-0007、[Web 页面基线与前后端对接规范](../development/web-ui-baseline-and-api-integration.md)

## 背景

此前 `/workspace/data`、`/workspace/model-plan`、`/workspace/experiments`、`/workspace/paper-editor`、`/task/complete` 是五条独立整页路由。`App.tsx` 没有客户端路由器，只在加载时读取一次路径，所有阶段切换都通过 `window.location.href` 触发整页重载：白屏闪烁、SSE 断开重连、工作台快照重新拉取、左栏 Agent 区重建。

业主（IvanCodesDev）明确要求：进入正式建模工作台后，各阶段应存在于同一个页面内原地切换，而不是相互独立、来回整页跳转的页面。同时必须保留深链接、刷新恢复与 `run_id` 身份传播（ADR-0007 的 URL 身份模型），并保持五个阶段页的视觉基线不变。

对比过两个方案：A（保留整页路由，仅把跳转软化）与 B（合并为单一工作台，路由保留为面板别名）。业主选择 B；B 在实现上包含 A 的软导航机制，稳态结构更干净（壳挂载一次、阶段为纯内容面板），也与后续五类页面正文契约逐面板接入的方向一致。

## 决策

### 1. 合并渲染

五条路由渲染同一份"合并工作台"标记（`workspaceScreen(initialStage)`）：一次挂载的聚焦壳（顶栏 + 左栏 Agent + 可拖分隔条）+ 五个阶段面板（`.workspace-stage[data-stage-pane]`）同存于 DOM，按进入路径决定初始可见面板，其余面板 `hidden`。路由表与 URL 全部不变，六条 URL 成为面板的直达别名；`/task/running` 保持独立页面（对话主页兼总览 hub）。

### 2. 软切换协议

阶段切换由模板层 `showWorkspaceStage(stage, options)` 执行：面板显隐、壳上 `data-focused-stage/data-workspace-page` 同步、顶部返回键随阶段更新（成果面板例外为"返回首页"）、演示态左栏文案联动、`history.pushState` 写入面板别名 URL、`document.title` 更新，并派发 `resize` 让隐藏面板中的 Chart.js 画布在首次显示时自愈。`popstate` 在工作台路径之间换面板；路径离开工作台时整页导航兜底。

### 3. 控制器协作（真实运行）

工作台控制器保持单次挂载，内部维护 `currentScreen`。阶段切换经 `omm:show-stage` / `omm:stage-shown` 自定义事件与模板层解耦，避免模块循环依赖。SSE 连接与工作台快照跨面板切换持续存活；左栏时间线点击、`navigate` 类 CTA、方案确认（非退回）后进入实验面板、"完成交付"进入成果面板，全部软切换并保留 `run_id/project_id` 查询参数。

### 4. 受保护入口的最小触碰

`OpenMathModelScreen.tsx` 将原始 HTML 注入路径从 `editor` 扩展到五个工作台 ScreenId（合并标记在所有工作台路由都包含 contenteditable 论文编辑器）。`App.tsx` 与 `screens.tsx` 未改动，14 条路径原样。本次触碰经业主显式授权（ADR-0006 的例外条款）。

### 5. 导航语义

- 顶部返回箭头 = 返回任务执行页（hub）；成果面板例外 = 一键返回首页，且成果面板操作行提供"返回首页"按钮；
- 左栏六阶段时间线 = 阶段间导航：真实运行下非当前面板的行可点击/键盘触发，纯导航、不调用 `/actions`；
- 错页加载语义不变（ADR-0007）：加载时不自动跳页，`suggested_route` 仍只作为建议。

### 6. 不变式

五个面板的视觉基线、DOM 槽位与稳定选择器不变；`ModelingWorkspaceView` 契约、`active_page/suggested_route` 语义不变；后端零改动；演示模式（无 `run_id`）享受同样的软切换且不请求任何 API。

## 结果

正向结果：

- 阶段切换秒级、无白屏，左栏与 SSE 状态连续；
- URL、深链接、刷新恢复、跨页身份传播全部保留；
- 阶段正文后续按契约逐面板替换时，天然落在"一个壳 + 多个内容面板"的目标结构上。

代价与约束：

- 五面板同存增加单页 DOM 体积与首屏渲染量；
- 四个有交互阶段的绑定块在激活时一次性全绑定；
- `popstate` 离开工作台采用整页导航兜底，属于刻意的简单性取舍。

## 验收要求

1. 六条 URL 直接打开均渲染合并工作台且初始面板正确（演示与真实运行两种模式）。
2. 工作台内切换阶段无整页重载：document 不重载、SSE 不断连、URL 与标题随面板同步。
3. 浏览器后退/前进可在面板间穿梭；后退离开工作台时正常整页导航。
4. 方案确认（非退回重做）成功后自动软切换至实验面板；"完成交付"软切换至成果面板。
5. 成果面板顶部返回键与操作行"返回首页"按钮均回首页。
6. 左栏时间线在真实运行下可点击切换面板，全程不产生 `/actions` 调用。
7. 实验面板图表在首次切入后正确渲染。
8. 类型检查、ESLint、生产构建通过；固定视口截图与基线逐页对比。

## 当前已验证证据（2026-08-12）

- `npm run check --workspace @openmathmodel/web`（tsc + ESLint）通过；`npm run build` 生产构建通过。
- 受保护入口改动范围核实：仅 `OpenMathModelScreen.tsx` 注入路径条件一处；`App.tsx`、`screens.tsx` 未变，14 条路径原样。
- 上列验收要求 1–7 的浏览器人工验收由 IvanCodesDev 进行中；按基线治理规则，浏览器视觉验收完成前不得宣称界面交付完成。
