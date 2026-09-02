# ADR-0013：跑完之后还能接着改——修订回合复用评审门，不派生新运行

- 状态：Accepted（切片 1、2 已落地并通过全量回归；切片 3 的第 12/13/14/15 项已落地，第 16/17 项前端待做）
- 日期：2026-08-31
- 关联：ADR-0011（编排状态机与有界循环）、ADR-0007、[系统架构](../architecture/system-overview.md)、设计 §11.3（运行备注）

## 背景

用户报障（2026-08-31）：「在一个对话页面里，只能让 agent 执行一次整个过程，只做了一次规划。
后面还在这个页面接着提问、想让它继续修改，它就只输出纯文字，不会真正执行操作了。」

这条报障拆开是两个不同的缺口，同日已修掉第一个、剩下第二个是本 ADR 的题目：

| 缺口 | 场景 | 状态 |
|---|---|---|
| 运行**进行中**的追问不影响执行 | 任务还在跑，用户补一句要求 | 已修：前端接上既有 `POST /task-runs/{id}/notes`，落库后注入后续每次节点执行的提示词 |
| 运行**已结束**后无法要求返工 | 任务已 COMPLETED，用户说「目标函数改成加权总成本」 | **本 ADR**。服务端 409 `RUN_FINISHED`，前端只能如实答「按问答处理，请另起新任务」 |

### 引擎现状（本轮逐行核实，是设计的事实基础）

盘点的结论出人意料：**修订回合所需的机制绝大部分已经存在**，而且当初就是按「退回重做」预留的。

| 能力 | 代码事实 | 位置 |
|---|---|---|
| 往回退是**合法转换** | `NEEDS_REVIEW → 任意工作状态` 已允许，注释写明 "reviewer may send the run backwards" | `states.py: can_transition` |
| 退回后**级联重做下游** | `advance` 在 `STATE_CHANGED` 后立即起步骤；每完成一段就顺延到下一段并再起步骤，一路重跑到底 | `engine.py: advance` |
| 重做**不覆盖历史** | `stage_outputs` 按 (run, node) 版本化：新版 `current`、旧版 `superseded`；表注释原文「重试/**退回重做**的历史版本因此可审计、可回溯」 | `orm.py: StageOutputRow`、`engine_glue.py: _record_stage_output` |
| 步骤**分趟记账** | `step_runs` 唯一键 (run_id, node, attempt)，重跑即 attempt+1，无需改表 | `orm.py: StepRunRow` |
| 闸门**重新决策** | 步骤重启时 `review_decisions.pop(state)`，旧决策随旧产出一并失效 | `reducer.py: _on_step_started` |
| 强制重跑当前状态 | `force_rerun` 标志（`RUN_RETRIED` 置位），压过「最近步骤已成功则顺延」规则 | `reducer.py`、`engine.py: _select_target` |

真正缺的只有三处，其中第一处是唯一被硬堵死的地方：

1. **`COMPLETED` 是全封闭终态**——`can_transition` 末行 `return False  # COMPLETED is fully terminal.`，没有任何事件能把运行带离终态；
2. **回退落地时不会真的重做**——`_select_target` 看到目标状态最近步骤已 `SUCCEEDED` 就顺延到下一段。
   forward-only 语义正是靠这条实现的（`resume_state` 恒等于请求评审的那个状态）。
   因此回退必须显式置 `force_rerun`，否则「退回数据准备」的效果是**直接跳到建模方案**，等于什么都没重做；
3. **快照产出会串轮**——`snapshot.outputs[state]` 是 `bucket.update(...)` 合并写入，第二轮若少写某个键，
   上一轮的陈旧值会残留；且回退瞬间下游各段仍挂着第一轮产出，节点读 `prior_outputs` 会读到过期结果。

`engine.py: resolve_review` 的注释把这件事记在案：「Sending a run BACKWARDS to redo earlier stages is
intentionally out of scope until pass-aware step tracking exists.」本 ADR 就是补上这个 pass-aware 缺口。

## 决策

### 1. 修订在**原运行内**开新一轮，不派生新运行

一次任务 = 一个 run = 一个对话页，修订是这个 run 的第 N 轮。理由：

- 用户的心智就是「在这个页面接着说」；派生新运行会让人看到「又多了一个任务」，与诉求错位；
- 成果、附件、事件、对话记录天然留在同一处，前后轮可直接比对（`stage_outputs` 的 v1/v2 已经是这个形状）；
- 派生方案需要给 `task_runs` 加 `parent_run_id`（现无此列）、还要在新 run 里伪造「已继承、不必执行」的步骤，
  否则引擎必然从头跑——成本高于在原运行内开轮。

**代价（如实记录）**：`COMPLETED` 不再是硬终态。这一条与 ADR-0011「当前编排事实」里的「终态冻结」相冲突，
需按本 ADR §4 修订那句表述。但 ADR-0011 的**三条承重不变量逐条不破**：

| 承重不变量 | 是否受影响 | 依据 |
|---|---|---|
| 金标轨迹回归 | 否 | 新事件只出现在新流程；既有事件载荷逐字节不变（沿用 `gate` 键那套「不带时载荷不变」的写法） |
| 单投影驱动双栏 | 否 | 仍是一份 `ModelingWorkspaceView`，节点→页面映射表不动 |
| 回放等于快照 | 否 | 新事件在 reducer 内确定性归约，`replay(events) == live snapshot` 照常断言 |

### 2. 修订入口复用 `NEEDS_REVIEW` 评审门，只新增一条出边

矩阵里只加 `COMPLETED → NEEDS_REVIEW` **一条边**，回退到六个工作状态的边本来就合法。
落地链路全部走既有设施：

```
运行已 COMPLETED
  └─ 用户在对话框继续提要求
      └─ POST /v1/task-runs/{id}/revisions {text}
          ├─ 记一条 run note（scope=global，即既有的提示词注入源）
          ├─ 算出建议重做起点（§3）
          └─ emit REVISION_REQUESTED
              └─ reducer: COMPLETED → NEEDS_REVIEW，review.resume_state = 建议起点
                  └─ glue 投影出审批行（六个阶段为选项，建议项 recommended:true）
                      └─ 用户在既有审批卡上确认或改选起点
                          └─ approve → resolve_review(approved, resume_state=所选, rerun=true)
                              └─ 落到该阶段 + force_rerun → 逐段级联重做到底 → 再次 COMPLETED
```

「让用户确认从哪一段重做」不是额外负担，而是**必要的知情同意**：从问题分析重做和从论文撰写重做，
花费和耗时差一个数量级。既有审批卡（`options` + `recommended` + CTA 预选）刚好是这个交互，无需新 UI 范式。

**撤回这道门 = 回到原本的终态，不是失败**（落地时补的决策）。既有的
`REVIEW_RESOLVED{approved:false}` 语义是「评审否决 → 运行判负」，直接复用会把
「用户改主意」记成运行失败——可什么都没跑坏。因此归约器按 `review.revision_round > 0`
分流：修订门被拒 → 恢复 `COMPLETED`；节点自提的闸门被拒 → 仍然 `FAILED`（行为不变）。
代价是矩阵要多放行一条 `NEEDS_REVIEW → COMPLETED`，静态守卫弱化为归约器守卫；
`revision_round` 不随撤回回退，它记的是**发起过几轮**，防的是反复开关刷额度。

### 3. 重做起点由「服务端提议 + 用户确认」，分诊不是新阶段

ADR-0011 约束新增领域阶段要走全链路，因此**不为分诊增设节点或状态**：建议起点是
`POST /revisions` 生成审批选项时算出来的一个字段。分两步走：

- **v1（本批次）**：关键词启发式（命中「数据/清洗/缺失」→ 数据准备；「模型/目标函数/约束/决策变量」→ 建模方案；
  「图表/排版/字数」→ 论文撰写；一句话同时点到多个阶段时取**最早**的那个——下游本来就会一并重跑），
  **并明确告诉用户这是建议、可改选**。落地时把「无把握」的默认值定为**论文撰写**而不是原稿写的建模方案：
  那是花费最小的解释，猜大了要用户白付一次全链路的钱，猜小了他在审批门里往前挪一格即可
  （`engine_glue.suggest_revision_stage`，2026-09-02 修订注记：词表补上了 ADR 点名却漏写的
  「目标函数 / 约束 / 决策变量」——此前背景里的用户原话「目标函数改成加权总成本」一个词都不中，会被建议成从论文撰写重做）；
- **v2（后续）**：换成一次有界 LLM 调用（读本轮成果摘要 + 这条要求，输出起点与理由）。
  接口形状不变，只换 `recommended` 的算法与 `description` 的措辞。

### 3.1 费用闸门：按轮追加配额 + 每 run 三轮封顶

修订天然会重跑一整段链路，费用问题必须在设计里正面处理，不能留给运行时撞顶。定下两条：

**按轮追加配额**。每批准一轮修订，给该 run 追加一份与首轮同量的配额。这样花费与用户的
每一次明确批准一一对应，不会出现「只补了一句话就烧掉一倍额度」而事先毫不知情。
被否掉的两个替代方案值得记下来，免得日后重提：

- *沿用首轮预算*：一行不用改，但首轮通常已用掉大半配额，修订会在跑到一半时撞顶，
  用户看到的是「点了继续修改，然后任务失败了」——比现在如实说「请另起新任务」更糟；
- *修订不计预算*：体验最顺，但一次「把图重画一下」就可能触发从建模方案往后的全链路重跑，
  且没有任何闸门拦得住反复修订，与本文风险表「费用失控」一条直接冲突。

**每 run 最多 3 轮**。在 `/revisions` 端点校验 `snapshot.revision_round`，耗尽后引导另起新任务。
3 轮覆盖绝大多数「改目标函数 / 换张图 / 调措辞」的真实来回，又能挡住无限返工把一个 run
拖成永不落地。**撤回的轮次同样计数**（见 §2）：否则反复「发起 → 撤回」就能绕开上限。
不做成配置项——在没有真实使用数据之前，默认值也只能拍 3，多一处配置只是多一处解释成本。

### 4. 需要落地的改动清单

**agents/core（切片 1，最小可测，不依赖 API）—— 已落地，`agents` 全量 265 passed**

| # | 文件 | 改动 |
|---|---|---|
| 1 | `states.py` | `can_transition`：`COMPLETED → NEEDS_REVIEW` 放行；另加 `NEEDS_REVIEW → COMPLETED`（仅供撤回修订，见 §2） |
| 2 | `engine.py` | 新增 `request_revision(snapshot, target_state, note)` → emit `REVISION_REQUESTED{target_state, note_id, round}`；`resolve_review` 增可选形参 `resume_state`（缺省沿用 `review.resume_state`，**既有调用与载荷不变**），回退时在载荷加 `rerun: true`（不回退时不写该键 → 金轨迹逐字节稳定） |
| 3 | `reducer.py` | `_on_revision_requested`：转 `NEEDS_REVIEW`、建 `ReviewRequest(resume_state=建议起点)`、`revision_round += 1`；`_on_review_resolved`：按载荷 `rerun` 置 `force_rerun`，并**清空 target 及其下游各段的 `outputs` 与 `review_decisions`**（消灭串轮）；拒绝分支按 `revision_round` 分流（§2） |
| 4 | `models.py` | `TaskRunSnapshot.revision_round: int = 0`（由事件确定性推导）；`StepRun` 沿用 `attempt`，不加字段 |
| 5 | 测试 | 新增 11 条：金轨迹「跑完 → 修订 → 从建模方案重做 → 再完成」、两条 `replay == snapshot`、串轮防护、上游闸门决策存活、撤回恢复终态、连轮台账、两条载荷形状（金轨迹逐字节稳定），以及**一条回归护栏**——手动关掉 `force_rerun` 后「退回建模方案」实际跳到实验，证明那行不是冗余 |

落地时核实的一个前提：`force_rerun` 不进 `to_dict`，本以为跨进程会丢。查证
`engine_glue.py` 与 `worker/runtime.py` **都用 `replay_events` 从事件日志重建快照**、不走
`from_dict`，故该标志由事件确定性推导，无丢失风险。切片 1 对既有路径是**严格空操作**：
`rerun` 仅由 `resolve_review` 在 `revision_round > 0` 或显式改 `resume_state` 时写入，
而全仓无任何调用方传 `resume_state`（`engine_glue` 与 `worker/runtime` 都只传 `approved`/`reason`）。

**backend/api（切片 2）—— 已落地，`agents + backend + tests` 全量 581 passed**

| # | 文件 | 改动 |
|---|---|---|
| 6 | `routers/task_runs.py` | `POST /v1/task-runs/{id}/revisions`：锁行、校验 `COMPLETED`、查轮次上限、写 global run note、驱动引擎、回执带轮次/审批 id/建议起点 |
| 7 | `actions.py` | **无需改动**：`_approve` 本就把 `option_id` 原样透传给 `resolve_approval`，阶段选项的识别放在 glue 层即可 |
| 8 | `engine_glue.py` | `_project` 增 `REVISION_REQUESTED` 分支（投影审批行：六阶段选项 + 唯一 recommended + 要求原文作 evidence，并清 `ended_at`）；`REVIEW_RESOLVED` 分支按 `revision_round` 分流（批准→摆正 `current_node` 后置 RUNNING；撤回→回填 `ended_at` 并置回 COMPLETED）；`resolve_approval` 识别 `redo:<STATE>` 选项传 `resume_state`，撤回分支跳过 `retry`；新增 `suggest_revision_stage` / `revision_rounds` |
| 9 | 预算 | **已落地：按轮追加配额**。`_build_budget_governor` 按 `revision_rounds()` 把 run 级与 node 级上限各乘一份——账本是全 run 累计的，不追加第二轮必然撞首轮的顶。取舍见 §3.1 |
| 9b | 轮上限 | **已落地：每 run 最多 3 轮**（`MAX_REVISION_ROUNDS`）。`/revisions` 端点数 `REVISION_REQUESTED` 事件，耗尽返回 409 `REVISION_LIMIT_REACHED`。**撤回也计数**，防的是反复开关刷额度 |
| 10 | `engine.py`（补切片 1） | `resolve_review` 在 `revision_round > 0` 时于载荷加 `revision_round`：撤回修订要恢复 COMPLETED、撤回节点自提闸门要判 FAILED，而状态对本身分不出是哪道门，投影层需要这个标记。非修订门不写该键，既有载荷逐字节不变 |
| 11 | 测试 | 新增 13 条（`test_run_revisions.py`）：重开后 `ended_at` 归空、七个选项且建议项唯一、按选定起点重做且上游 attempt 不动、从建模方案重做会重新过方案门、撤回恢复终态且一步不再跑、三轮封顶、未完成运行 409、越权 404、审批行过 v1 契约校验、建议起点取最早触及的阶段、配额按轮追加 |

落地时发现的一个必要设计（原清单未写）：修改要求正文**必须同时落成一条 global run note**。
重做阶段的节点靠 `EngineLlmPort` 读 `run_notes` 才知道「要改什么」；不落的话重跑一遍
只是把同样的输入再算一次，产出与上一轮无异——功能看着通了，实际什么都没改。

**apps/web（切片 3）**

| # | 文件 | 改动 |
|---|---|---|
| 12 | `openmathmodel-ui.ts` | **已落地**（`f87bbf4`）：409 `RUN_FINISHED` 从「死路」改成 CTA「按这条要求继续修改」，点击调 `/revisions`；超 2000 字不给按钮并当场说明，三种 409 分别给话。如实备案：web 包没有 DOM 测试栈，点击路径未经真实浏览器验收 |
| 13 | `modeling-workspace-api.ts` | **已落地**（`f87bbf4`）：新增 `postRunRevision(runId, text)` 与回执类型 `RunRevisionReceipt`，导出 `RUN_REVISION_TEXT_LIMIT=2000` 与服务端对齐 |
| 14 | 审批卡 | **已落地**（`15817bf`）：修订门（正向选项 >1）在 CTA 上方摆出全部选项供改选，点选只记选择不提交；策略拆到 `approval-options.ts` 以便单测（8 例）。浏览器实机点选验收未做 |
| 15 | `workspace_view.py` | **已落地**：修订门不再沿用「确认后，Agent 将从当前检查点继续执行」。`_revision_round` 以 `evidence["revision_round"]` 为判据（不是选项 id 的 `redo:` 前缀——那只是命名约定，日后同名选项会被误判），`_revision_gate_summary` 写明起点、影响面与「另计一份运行配额」，按钮改称「确认重做起点」；推荐不唯一致预选缺席时如实请用户自选。节点自提闸门逐字不变，`backend/api` 全量 287 passed |
| 16 | 时间线 | 同一阶段出现多趟，按轮分组并标「第 2 轮」，避免看起来是重复卡片。**开工前先看**：`modeling-workspace-controller.ts` 第 785~791 行对同一 `llm:<prompt_id>` 复用行并改写标题为「（第 N 次尝试）」，第 2 轮重跑必然命中同一 prompt_id，会把用户主动要求的修订轮显示成系统失败重试——得先按 `revision_round` 拆 key 或标题，否则加了分组标题仍是错的 |
| 17 | 状态接管 | 运行状态由 COMPLETED 变回 RUNNING，列表筛选、通知需能接住。**已核实**：`recent-tasks.ts` 的 `TERMINAL_STATUSES` 归桶跟着 status 走，无需改；`restore-last-task.ts` 全文无状态判断，本项不涉及该文件。**通知去重已修**（2026-09-02）：`notifications/desktop-notifications.ts` 原 `tag: omm-run-{runId}-{current}` 在第二轮完成时逐字节相同，模块级 `delivered` 与浏览器同 tag 去重会吞掉第 2 轮的「任务已完成」——且这个缺陷**不依赖修订功能**：G2 数据闸门与 G1 方案门在同一运行先后 WAITING_APPROVAL，第二道门的提醒同样被吞。现在 tag 由 `runStatusNotificationTag` 生成：等待确认带审批 id、其余状态带最新事件序号（同一次进入的重复快照 tag 相同，去重语义保留），`desktop-notifications.test.mjs` 6 例覆盖 |

### 5. 明确不做

- **不派生新运行**、不加 `parent_run_id`（理由见 §1）；
- **不做增量复用**（只重跑受影响的段、其余段复用产出）——依赖尚未建立的段间依赖图，本批次一律**从起点顺序重做到底**；
- **不引入图编排**（ADR-0011 立场不变，本 ADR 只在既有状态机上加一条边）；
- **FAILED / CANCELLED 的修订入口本批次不做**：`FAILED` 已有 `retry` 语义（重入失败状态），
  引擎侧取消也投影为 `RUN_FAILED`，先让 `COMPLETED` 这条主路走通。

## 结果

正向：

- 用户在同一页面接着提要求即可真正返工，报障中「只能执行一次」的核心诉求被正面解决；
- 改动集中：引擎侧四个文件、矩阵只加一条边，其余全是既有机制的复用；
- 多轮产出可审计：v1/v2 版本、attempt 分趟、闸门逐轮重新决策，都是既有行为白送。

代价与风险：

| 风险 | 说明 | 应对 |
|---|---|---|
| 终态不再冻结 | 下游消费方若假定 COMPLETED 不可变会出错 | 全量排查 `TERMINAL_*` 的使用点；本 ADR §4 第 14 项 |
| 费用失控 | 一次修订可能触发全链路重跑 | 已定策：按轮追加配额 + 审批卡如实告知消耗（§3.1、§4 第 9 项） |
| 无限修订 | 用户反复修订导致运行永不落地 | 已定策：每 run 3 轮封顶，撤回也计数（§3.1、§4 第 9b 项） |
| 与并行开发冲突 | 会话 016c77d4（H3）正在改 `nodes.py` / `engine_glue.py` 节点接线 | 切片 1 只碰 `states/engine/reducer/models`，与之基本不相交；切片 2 的 `engine_glue._project` 需与其对齐后再动 |

## 对 ADR-0011 的修订

ADR-0011「当前编排事实」中「终态冻结」一句，应改述为：
**「终态冻结，`COMPLETED` 仅可经显式 `REVISION_REQUESTED` 进入评审门开启新一轮，不存在其它出边」**。
三条承重不变量与「不引入通用图编排」的立场均不受影响。

## 重新评估条件

1. 出现「只重做受影响阶段」的强需求（需先建立段间依赖图，届时重开 §5 第 2 条）；
2. 修订轮需要跨 run 比对或分支（A/B 两个修订方向并存），届时重新评估派生运行方案；
3. 修订轮数上限被证明是产品瓶颈。
