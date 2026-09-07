# ADR-0015：按任务类型把模型调用定向到指定接口（智能路由做实）

- 状态：Accepted
- 日期：2026-09-05
- 关联：ADR-0010（附件模态感知）、ADR-0014 决策 11（智能路由候选改为已保存接口）、[Web 页面基线与前后端对接规范](../development/web-ui-baseline-and-api-integration.md)

## 背景

设置中心 → 模型厂商 → 智能路由有一个「启用模型智能路由」开关和四个下拉（编程与 Agent / 深度研究 / 长文写作 / 视觉理解）。ADR-0014 决策 11 把四个下拉的候选改成了用户已保存的接口并做到即时刷新，但它们的取值仍只随整张设置表落在浏览器 `localStorage`，服务端没有任何一处读取——选了等于没选。

服务端现有的模型选择只有两层：

- 对话（`/api/chat`）：输入框模型选择器决定 `Auto`（难度判定 × 能力权重路由）或钉住某条接口；
- 任务执行（`engine_glue` → `EngineLlmPort`）：六个阶段的全部调用一律走 `LlmConfig.chain()`——主接口在前、其余接口按序备用。

这意味着用户配了「写代码强的模型」与「写论文强的模型」两条接口之后，任务的实验阶段与论文阶段仍然都打给主接口，另一条只在主接口故障时才被用到。产品裁定（IvanCodesDev，2026-09-05）：让这四项真正生效。

## 决策

### 1. 契约：`users.llm_config` 增加 `task_routes` 与 `smart_routing`

```json
{
  "endpoints": [...],
  "active_endpoint_id": "ep_a",
  "allow_proxy": true,
  "stream": true,
  "fallback": true,
  "smart_routing": true,
  "task_routes": { "coding": "ep_b", "research": null, "writing": "ep_c", "vision": null }
}
```

- 四个任务类型固定为 `coding` / `research` / `writing` / `vision`（对应面板的编程与 Agent / 深度研究 / 长文写作 / 视觉理解）。
- 值是已保存接口的 `id`，`null` = 自动（沿用既有行为）。`GET /api/account/llm-config` 永远回全四个键；`PUT` 接受部分键，缺省为 `null`。
- **服务端只认存在的接口**：保存与解析两处都会把指向已删除接口的定向丢成 `null`，删接口不需要前端补一次保存。
- `smart_routing` 是四项定向的总开关（默认 `true`）：关闭后 `task_routes` 原样保留但不生效，全部调用回到既有行为。面板上「启用模型智能路由」开关即此字段，文案改为如实描述：「开启后按下方任务类型把调用定向到指定接口；关闭则全部走主接口链」。`Auto` 难度路由不受这个开关影响。

### 2. 语义：定向 = 换主接口，不是排他

`LlmConfig.chain_for(kind)`：该类型有定向且开关开启 → `chain_from(定向接口)`（定向接口在前，其余已保存接口按「失败时自动切换备用接口」开关决定是否跟在后面）；否则 → `chain()`。定向只改变「先打谁」，回退、中转站门控、预算硬限制、用量记账全部沿用既有路径。

### 3. 任务执行：按提示词归类

`EngineLlmPort` 的每次调用都带 `prompt_id`；新增 `prompt_id → 任务类型` 映射（`engine_glue._PROMPT_TASK_KINDS`），端口据此取链：

| 任务类型 | 提示词 |
| --- | --- |
| `research`（深度研究） | `problem_analysis.default`、`data_preparation.default`、`model_planning.default` / `.proposer` / `.reduce` / `.formalize` |
| `coding`（编程与 Agent） | `data_cleaning.sandbox`、`experiment_code.default` / `.sandbox`、`validating.default` / `.sandbox` |
| `writing`（长文写作） | `paper_outline.default`、`paper_section.default`、`paper_finalize.default`、`paper_writing.default` |

数据准备阶段被拆开：出清洗方案（`data_preparation.default`）是分析，沙盒里写码跑码（`data_cleaning.sandbox`）是编程。映射表之外的提示词（以及接待判定、Auto 难度判定这两类刻意选最轻模型的调用）不受定向影响。`llm_call` 过程事件附带 `task_kind` 与是否命中定向（`pinned`），工作台执行轨迹与 evals 都能核对「这一步打给了谁」。

### 4. 对话：只有携图时用 `vision` 定向

对话的模型由输入框选择器显式控制，不按对话模式套用 `research`。`vision` 定向只在消息携带直通图片时介入：

- 选择器为 `Auto`（或旧客户端未带路由参数）且请求携图 → 服务端改走 `chain_for("vision")`，meta 里 `route.mode = "task"`、`route.kind = "vision"`；
- 选择器显式钉住某接口 → 尊重用户选择，不改；
- 前端 `resolveSelectedModality("auto")` 同步改为「有 `vision` 定向就按定向接口判定模态」——托盘位图是否直通、`pinEndpointId` 钉谁，都以定向接口为准（ADR-0010 的直通阶梯不变，只是「生效模型」的解析多了一层）。

### 5. 前端保存与回填

- 「保存更改」把四个下拉（`endpoint-<id>` → `id`，`自动选择` → `null`）与开关一起随 `PUT /api/account/llm-config` 推送；「保存为新接口」「保存修改」同样携带（与三个行为开关同一纪律）。
- 打开面板时以服务端为准回填四个下拉与开关（覆盖本机残留）；面板内增删接口触发的候选刷新保留用户尚未保存的当前选择（ADR-0014 决策 11 的即时刷新不变）。
- `localStorage.openmathmodelSettings` 里仍保留这几个键（整张设置表的一部分），但服务端值是唯一生效来源。

## 结果

- 用户为不同阶段配不同模型的意图第一次能落到执行上：实验阶段打编程模型、论文阶段打写作模型、题意解析与方案设计打研究模型、携图对话打视觉模型。
- 不改任何页面结构、路由与页面契约；`llm-config` 契约向后兼容（缺键即缺省），旧数据无需迁移。
- 代价：多一层「先打谁」的判断入口，排障时需看 `llm_call.task_kind/pinned`；四类映射是产品口径，新增提示词时要顺手归类，漏归类的提示词按主接口链处理（安全方向）。

## 验收要求

1. `PUT /api/account/llm-config` 携带 `task_routes` / `smart_routing` 后 `GET` 原样回读；指向不存在接口的定向回读为 `null`；删除被定向的接口后该项自动回落 `null`。
2. 配两条接口 A（主）、B，把 `coding` 定向到 B：任务的 `experiment_code.*` / `validating.*` / `data_cleaning.sandbox` 调用先打 B（`llm_call.pinned = true`），`problem_analysis.default` 等仍先打 A；`smart_routing = false` 后全部先打 A。
3. 携图对话、选择器为 `Auto`、`vision` 定向到视觉接口：请求打到该接口，meta `route.mode = "task"`；选择器显式选了别的接口时不改。
4. 设置中心：打开面板四个下拉与开关以服务端为准；改动后「保存更改」→ 重新打开面板仍是改后的值；删除被定向的接口后再打开，该下拉为「自动选择」。
5. `pytest backend/api/tests`、`export_openapi.py --check`、`npm run check`、`npm run build`、`node --test` 通过。
