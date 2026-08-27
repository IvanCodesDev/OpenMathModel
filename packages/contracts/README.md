# @openmathmodel/contracts

OpenMathModel 跨语言契约包，是 Web、Desktop、API、Worker、Agent 之间的**唯一事实来源**。

## 目录（v1 体系，老板仲裁为准）

```text
schemas/v1/          JSON Schema（draft 2020-12），字段与语义的权威定义
fixtures/v1/         正/反两路契约回归夹具（validate.py 消费）
baseline/            兼容性基线（check_compat.py 消费，破坏性变更须显式重冻结）
openapi/v1/          openapi.api.json：从 backend/api 真实应用导出的接口基线（禁止手改）
src/ts/v1/           生成的 TypeScript 类型（generate-ts.mjs，禁止手改）
src/omm_contracts/   Python 包：v1/ 生成的 Pydantic 模型（禁止手改）+ enums/inputs 稳定表面
scripts/             generate-ts.mjs / generate_python.py / export_openapi.py
validate.py          Schema 合法性 + fixtures 双路 + 共享 $defs 漂移自检
check_compat.py      向后兼容门禁（消费者视角只增不破坏）
```

> 遗留待处置（老板批准后删除/并入）：`schemas/*.schema.json` 扁平套、`openapi/openmathmodel.v1.yaml`、`python/`、`examples/`。

## 首批对象（v1）

`Project`、`TaskRun`、`StepRun`、`AgentEvent`、`Artifact`、`ApprovalRequest`，以及统一错误信封 `ErrorEnvelope`。

## 第二批对象（v1，六阶段真实节点 → 页面正文）

`DatasetProfile`、`PlanProposal`、`ExperimentSummary`、`DocumentDraft`、`DeliveryManifest`：数据准备/建模方案/实验与验证/论文编辑/最终成果五类页面的正文投影，数据源是 `run_domain_events` 中 `STEP_SUCCEEDED` 事件 `payload.outputs`（六阶段真实 LLM 节点的最新成功输出），由 `GET /api/v1/task-runs/{run_id}/stage-outputs` 端点承载；阶段未完成时对应字段为 `null`。

## 演进规则

1. 协议先于调用方变更；先改本包，再同步生产者与消费者。
2. 字段只增不破坏；删除/改名必须走弃用期与版本并存（`check_compat.py` 在 CI 拦截破坏性变更）。
3. 枚举只增不改；消费者必须安全处理未知枚举值（保留原值或走默认分支，不崩溃、不静默吞掉）。
4. 错误码只增不改，不复用旧码表达新语义。
5. ID 恒为带前缀 32 位 hex 字符串（`proj_/run_/step_/evt_/art_/appr_`），大整数不跨 JSON 边界。
6. 时间戳 UTC ISO-8601 且以 `Z` 结尾（含 1-6 位小数秒），展示层做时区转换。
7. **资源响应 `additionalProperties: false`**：响应面精确、防止内部字段（如 `paused_from_status`）泄漏；
   自由载荷（`AgentEvent.payload`、`TaskRun.params`）保持开放对象，消费方容忍未知字段。
8. 生成物永不手改：TS/Python 模型与 OpenAPI 基线全部由脚本生成，CI 校验与源同步。

## 校验与生成

```powershell
# Schema 合法性 + fixtures 双路 + $defs 漂移
python packages/contracts/validate.py

# 向后兼容门禁（有意破坏性变更：--freeze 重冻结并过评审）
python packages/contracts/check_compat.py

# TypeScript 类型（--check 供 CI 防过期）
npm run generate --workspace @openmathmodel/contracts
npm run check --workspace @openmathmodel/contracts

# Pydantic 模型（工具链锁版见 requirements-dev.txt；--check / --verify 供 CI）
packages/contracts/.venv/Scripts/python scripts/generate_python.py

# OpenAPI 基线（从 backend/api 导出；--check 供 CI 防漂移）
python packages/contracts/scripts/export_openapi.py
```

## 待办（后续批次）

- 认证/账户端点契约化（与账户属主协同）。
- `ExperimentRun`、`DatasetVersion`、`EvidenceRecord`、`Claim` 等 Phase 4/5 对象。
- OpenAPI → 生成式客户端（Web），替换手写薄客户端。
