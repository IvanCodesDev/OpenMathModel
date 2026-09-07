# ADR-0014：页面默认空态，演示内容退到 `?demo=1` 之后

- 状态：Accepted
- 日期：2026-09-05
- 关联：ADR-0006、ADR-0007、ADR-0009、[Web 页面基线与前后端对接规范](../development/web-ui-baseline-and-api-integration.md)

## 背景

14 个页面的原型标记里内置了一整套虚构演示任务——「2026 国赛 A 题 / 城市共享单车调度优化」：确认页的四个假附件与三组静态清单、任务执行页的四条假执行步骤与「XGBoost + 混合整数规划 / 2.5~3.5 小时」推荐路线、数据页的 12,480 条记录 / 2.7% 缺失、方案页的方案 A/B/C、实验页的 1,842,596 vs 2,033,414（−9.38%）、论文页的七章大纲与整段正文、成果页的六个假交付文件、项目页的七个假项目、侧栏四条假最近任务、用户区的「Ivan · 个人工作区」。

这些内容原本是给静态原型看的。接入真实后端后它们变成三类问题：

1. **默认观感失真**：不带 `run_id` 打开任何页面，看到的都是一套完整、精确、然而完全编造的建模结果——用户（IvanCodesDev）明确表达这是干扰。
2. **真假混排**：真实运行下控制器只替换部分槽位。典型如实验页与成果页的七个子分页：产物表已经换成真实文件，上方的「关键指标 / 主要结论 / 复现命令」仍是演示夹具的原文，两者拼在同一页里无法区分。
3. **口径不可信**：论文页「引用」菜单固定给出三条不存在的来源（`Run #04 · 结果表 2` 等），插进正文就成了假引用。

同时，演示内容对截图、界面回归对照仍有价值，业主选择保留但要显式开启。

## 决策

### 1. 默认空态

页面模板默认（无 `run_id`、无 `demo=1`）只渲染骨架与空态说明，不渲染任何虚构数据。空槽位给一句「××将在××阶段完成后显示」，而不是留一片无法解释的空白。

### 2. 演示态显式开启

演示夹具统一由 `apps/web/src/integration/demo-mode.ts` 的 `demoMode()`（URL 携带 `?demo=1`）门控，与工作台控制器既有的 `demo=1` 语义一致（demo 下不解析 `run_id`、不请求任何 API）。`demoMode()` 为真时，页面渲染与本 ADR 之前完全相同的演示内容。

### 3. 骨架不动

所有控制器用于定位填充点的稳定选择器一律保留：`.focused-conclusion-strip`、`.focused-metrics`、`.focused-section.compact`（含 `h2` 与 `table`）、`.raw-preview-section`、`.focused-plan-list`、`.focused-plan-detail`、`.focused-report-conclusion`、`.focused-experiment-notes article`（顺序敏感，`[0]` 稳健性 / `[1]` 实现思路）、`.focused-run-meta`、`.deliverables`、`.complete-project-name`、`.result-summary`、`[data-agent-summary]`、`[data-agent-cta]`。空态是「骨架在、内容空」，不是「删掉整块」。

空态骨架里默认 `hidden` 的槽位由渲染器在填入真实内容时放出（`stage-content.ts` 的 `renderDataPanel` / `renderExperimentsPanel` 各补一处 `hidden = false`）。

### 4. 永不真实化的内容直接删除

以下块没有、也不会有真实数据源，直接删除而不是留空骨架：

- 确认页「任务目标 / 输出要求 / 执行方式」三组静态清单；
- 数据页「原始数据 / 清洗数据」两个纯演示分页（默认不渲染分页入口与面板，`?demo=1` 下保留）。

「字段说明 / 实现计划 / 模型假设 / 符号表」四个分页有真实数据源，默认入口先隐藏，由 `revealWorkspaceTab` 在内容就绪后放出。其中「模型假设 / 符号表」在本 ADR 成文当日随 H3 切片 2 从纯演示分页转为可填充分页：数据源是 `plan-proposal` 契约新增的可选字段 `assumptions` / `symbols`（方案阶段归约之后由 `model_planning.formalize` 规范化产出，也是论文「模型假设」「符号说明」两节的底稿）；字段为 null 时（切片 2 之前的运行、规范化失败、无监督者的单次调用路径）入口保持隐藏，演示表格仍只在 `?demo=1` 下出现。

### 5. 论文引用改取真实产物

`PAPER_SOURCES` 三条硬编码来源改为 `paperSourceOptions()`：从本次运行真实发布的产物行（`.deliverable[data-artifact-id]`）取名字；没有产物时点击「引用」提示「暂无可引用的产物」，不再插入假来源。`?demo=1` 下仍给原来的三条演示来源。

### 6. 账户与列表默认未登录态

侧栏与设置中心的「Ivan · 个人工作区」改为「未登录」，由 `hydrateAccountUi` / `hydrateAccountCard` 用真实账户覆盖；项目页默认渲染空态行（「还没有项目，去首页发起第一个建模任务」）而不是七行假项目；侧栏最近任务默认「暂无最近任务」（与 `recent-tasks.ts` 自己渲染的空态同一份文案与类名）。

### 7. 模型选择器只列真实可用的模型

输入框上的模型选择器默认给出 `Qwen3.8-Max / DeepSeek-V4-Pro / GPT-5.6 Sol / Claude Sonnet 5` 四个「官方服务」选项，但 `agent-chat.ts` 的 `routeSelection()` 对这些历史遗留值不携带任何路由参数——选中后请求照旧走默认主接口链，选择是无效的。这类「点了不生效」的选项比空列表更有害，因此：

- 有已保存接口时：`Auto` + 每条真实接口（不变）；
- 没有已保存接口时：只列 `Auto`，外加本机设置里**确实填过**的自定义 API；
- 那四条「官方服务」与 `gpt-5.6-sol` 占位名退到 `?demo=1` 之后。

配套：`hydrateModelPickers` 由「接口池为空就整个跳过」改为「只有配置拉不到（离线/未登录）才跳过」，否则照常按 `composerModelOptions(config)` 重渲染，这样设置里刚填的自定义 API 能立刻出现在选择器里；设置保存时若把自定义模型名清空，直接移除该选项并退回 `Auto`，不再用 `gpt-5.6-sol` 兜底。

### 8. 运行态路由的登录闸门

`/task/running` 与五条工作台路由都靠 `run_id` 找后端要快照，未登录时后端一律 401。清空演示内容后这条路径暴露出一个更难看的终点：页面渲染成功、骨架全空、首气泡里只剩控制器 `renderError` 写下的「状态同步需要处理 · 请先登录」——用户面对的是一个报错的空壳，而不是一句「先登录」。

新增 `apps/web/src/integration/auth-guard.ts`，在 `activateScreen` 最前面同步执行：

- 六条运行态路由（`running` / `data` / `model` / `experiments` / `editor` / `complete`）未登录时不进入，直接 `window.location.replace("/login?next=<原地址>")`，登录成功后由 `hydrateAccountUi` 既有的 `next` 逻辑送回原地址；
- 会话中途失效（进页面时缓存还有效、401 才暴露）走同一条路：`renderError` 遇到 401 不再写残页，`invalidateMe()` 后跳登录；
- 不拦 `?demo=1`（演示态不碰接口），也不拦后端不可用（`fetchMe` 抛非 401 时保留页面，让既有错误提示说话）；
- 校验窗口内 `body[data-auth-gate]` 让 `.main` `visibility: hidden`，杜绝残页闪帧；`/me` 超过 2.5 秒未回则放行，避免正文区一直白着。

配套：`fetchMe` 增加在途请求去重，路由闸门与侧栏用户区不再在同一次冷启动里各打一次 `/me`。

### 9. 任务执行页的空壳一并收起

默认态下 `/task/running` 顶栏的运行状态徽标不再永远停在「加载中…」，首气泡也不再是一个只剩「Agent」头像行的空壳——两者都默认 `hidden`，由 `renderStatus` 拿到真实快照时放出；`renderError` 需要报真实故障时也会连壳一起放出。

### 10. 顺带清除的死代码

`taskDetailsDrawer()`（零调用点）、`modelingShell` 的非聚焦分支及其 `modelingHeader` / `modelingAgentPane` / `stageAgentCopy`（ADR-0009 合并工作台后不可达）、`experiments` 数组与 `.experiment-item` / `[data-experiment-tab]` / `[data-data-tab]` / `[data-data-file]` / `[data-drawer-tab]` 的绑定（对应标记均已不存在）、`#dailyChart` / `#hourChart` / `#resultMetricChart` / `#stabilityChart` 四个不存在画布的图表分支、`.experiment-titlebar` 的 CSS 残留。

Chart.js 只剩演示态实验页的对比柱状图一处用途，`initCharts` 相应收敛为 `demoMode()` 才加载。

### 11. 设置中心「智能路由」的候选改为用户已保存的接口

设置中心 → 模型厂商 → 智能路由的四个下拉（编程与 Agent / 深度研究 / 长文写作 / 视觉理解）此前各写死四五个厂商型号（GPT-5.6 Sol、Claude Opus 5、GLM-5.3……）。产品判断：这里的问题不是「写死」本身，而是候选**必须看用户自己配了什么**——一个没配 Anthropic 接口的用户，下拉里出现 Claude Opus 5 就是一条永远选不通的假选项；反过来，用户刚配好 `gpt-6-astra` 的接口，下拉里却还没有它。因此：

- 候选 = `自动选择` + 每条已保存接口（取值 `endpoint-<id>`，与输入框模型选择器同一套标识；显示「模型 · 接口名」，主接口再标一下）；不再内置任何厂商型号，新型号配成接口的那一刻就出现在这里，不用等预设表更新；
- **即时更新**：在「自定义 API」保存新接口、编辑、删除、设为主接口、调整权重之后，四个下拉与「已保存接口」列表、厂商卡片状态一起刷新（`renderLlmConfigViews`），不必关掉再打开设置；
- 刷新时保留当前选择；被删掉的接口回落 `自动选择`；本机设置里残留的旧文本值（如「GPT-5.6 Sol」）匹配不到即回落 `自动选择`；
- 池子为空或未登录时下拉只剩 `自动选择`，下方一行说明指向「自定义 API」；
- 为支持选项集合动态变化，自定义下拉的增强逻辑（`enhanceSettingsSelect`）提为可重复调用：重建前先拆掉旧的，避免原生 `select` 与自定义下拉显示分叉。

同一轮明确**保留**的内容：设置中心「自定义 API」表单的预填示例（`OpenAI 兼容中转站` / `https://api.example.com/v1` / 示例密钥 / `gpt-5.6-sol`）——它是示例占位而不是虚构结论，`endpointFromForm` 对 `example.com` 直接判为未配置，不会被当成真实接口同步上去。

边界（已由 ADR-0015 解决）：本决策落地时四个下拉的取值只随整张设置表落在本机 `localStorage`，服务端不读取。同日的 [ADR-0015](0015-task-kind-endpoint-routing.md) 补上了服务端契约（`llm-config.task_routes` / `smart_routing`）与执行链路：任务的六个阶段按提示词归入四类任务类型取链、携图对话走「视觉理解」定向；本决策只负责候选来源与即时刷新。

同一天顺带完成 OpenAI 新旗舰 GPT-6 Astra（2026-09-03 分批 GA，API id `gpt-6-astra`）的适配：厂商预设首位、模态表判定为视觉、Auto 路由能力推断新增 `-astra` 强档记号、费用估算表新增 `gpt-6-astra` / `gpt-6`（$10/$50 折 70/350 元）。这些是数据表更新，不改任何契约。

## 结果

正向结果：

- 默认打开任何页面不再出现虚构的建模结论；真实运行下不再出现真假混排的段落；
- 「看到的都是真的」成为可检查的性质：任何非空内容要么来自契约，要么来自 `?demo=1`；
- 演示能力零损失，`?demo=1` 与改动前逐像素一致。

代价与约束：

- 默认态页面观感变化很大，界面基线截图需要重拍（默认空态 / `?demo=1` / 真实运行三套）；
- 演示夹具字符串仍留在主包内（约 40 KB），仅做了运行时门控；如需从包体里拿掉需另做动态 `import()` 拆分；
- 空态文案成为新的界面基线的一部分，后续修改同样受基线治理约束。

## 验收要求

1. 默认（无参数）打开 `/`、`/task/confirm`、`/task/running`、五条工作台路由与 `/projects`：无任何「共享单车 / 2026 国赛 A 题 / Run #04 / Ivan」字样。
2. 同上路由加 `?demo=1`：演示内容与改动前一致。
3. 真实 `run_id` 打开五条工作台路由：各阶段正文由契约填充；实验页与成果页七个子分页不再出现演示摘要行；产物表为真实产物或「该阶段尚未发布产物」。
4. 论文页「引用」在无产物时提示且不插入内容；有产物时列出真实产物名。
5. 未登录时侧栏用户区显示「未登录」，登录后显示真实用户名。
6. 未配置接口时模型选择器只有 `Auto`；在设置中心填入自定义 API 并保存后，选择器立刻出现该模型；清空模型名后该项消失且回落 `Auto`。
7. 未登录直接访问 `/task/running?run_id=…` 与五条工作台路由：不进入页面，跳到 `/login?next=…` 并弹登录框；登录成功后回到原地址且能正常加载；全程不出现「状态同步需要处理 · 请先登录」的残页。
8. 登录状态下访问同样路由：不被拦截，也不出现正文区空白闪烁。
9. 设置中心 → 模型厂商 → 智能路由：未登录时四个下拉只有「自动选择」并有一行登录提示；登录且无接口时只有「自动选择」并提示去「自定义 API」；在「自定义 API」点「保存为新接口」后**不关闭设置**切回「模型厂商」，四个下拉立刻出现该接口（显示「模型 · 接口名」）；删除该接口后选项消失，曾选中它的下拉回落「自动选择」；OpenAI 卡片副标题首位为 `gpt-6-astra`，点「配置」默认模型填入 `gpt-6-astra`。
10. 类型检查、ESLint、生产构建通过；三种状态的固定视口截图留存并与基线对比。

## 当前已验证证据（2026-09-05）

- `npm run check --workspace @openmathmodel/web`（tsc + ESLint）通过；`npm run build` 生产构建通过；`node --test src/i18n/en-US.test.mjs` 词典门禁 5 项通过。
- 决策 11 附带的后端数据表更新：`pytest backend/api/tests/test_llm_chat.py test_usage.py test_budget_guard.py` 79 个用例通过，其中新增 `test_endpoint_strength_recognizes_current_flagships`（`gpt-6-astra` 推断为强档 8、用户权重优先）与 `test_model_pricing_covers_gpt6_astra`（70/350 元，5.6 档位不受影响）。
- 受保护入口未触碰：`App.tsx`、`screens.tsx`、`OpenMathModelScreen.tsx` 均未改动，14 条路径原样。
- 上列验收要求 1–9 的浏览器人工验收待 IvanCodesDev 进行；按基线治理规则，浏览器视觉验收完成前不得宣称界面交付完成。
- 决策 8 的前提：`/login` 走 SPA history fallback（Vite dev server 默认支持；生产部署需保证未知路径回落 `index.html`）。既有的「退出登录 → `/login`」流程已依赖同一前提。
