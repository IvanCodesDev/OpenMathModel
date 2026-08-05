# services/api — OpenMathModel 控制面

FastAPI + SQLAlchemy 2 + Alembic。契约见 `packages/contracts`（事实来源 `schemas/v1`，本服务响应模型直接使用 `omm_contracts` 生成模型）。

## 本地运行

```powershell
# 1) 首次：安装依赖（仓库根目录；契约包为 packages/contracts 的 v1 事实来源）
.venv\Scripts\python -m pip install -e packages/contracts -e "services/api[dev]"

# 2) 启动（默认 SQLite：services/api/data/dev.db，启动时 create_all 自动建表）
cd services/api
..\..\.venv\Scripts\python -m uvicorn omm_api.asgi:app --reload --port 8000
```

- 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/health

### 切 PostgreSQL（两条路径任选）

```powershell
# A. 免安装用户级 PG（tools/pg-dev.ps1，port 5433）
.\tools\pg-dev.ps1 init      # 之后日常 .\tools\pg-dev.ps1 start
$env:OMM_DATABASE_URL="postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:5433/openmathmodel"

# B. Docker 底座（tools/dev-up.ps1，port 5432，见 infra/docker/compose.dev.yaml）
$env:OMM_DATABASE_URL="postgresql+psycopg://openmathmodel:openmathmodel-dev@127.0.0.1:5432/openmathmodel"

# PostgreSQL 部署路径使用 Alembic 迁移
cd services/api
..\..\.venv\Scripts\python -m alembic upgrade head
```

## 环境变量（前缀 OMM_）

| 变量 | 默认 | 说明 |
|---|---|---|
| `OMM_DATABASE_URL` | `sqlite:///<repo>/services/api/data/dev.db` | 切 PostgreSQL 见上文两条路径 |
| `OMM_SECRET_KEY` | `dev-secret-change-me` | 2FA 挑战令牌签名密钥，生产必须覆盖 |
| `OMM_RUNNER_ENABLED` | `true` | 内嵌模拟推进线程（T5 起由 agents/core 驱动） |
| `OMM_RUNNER_TICK_SECONDS` | `1.2` | 推进节奏 |

## 测试

```powershell
# 默认 SQLite 临时库（快，确定性 tick 驱动）
.venv\Scripts\python -m pytest services/api/tests -q

# 对真实 PostgreSQL 实跑同一套件（每用例独立 schema，用完清理）
$env:OMM_TEST_DATABASE_URL="postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:5433/openmathmodel_test"
.venv\Scripts\python -m pytest services/api/tests -q
```

## 鉴权

`/api/v1` 全部资源要求登录（httpOnly Cookie 会话）：先 `POST /api/auth/register` 或 `login`。
项目/任务按 `owner`（用户 ID）隔离，他人资源一律 404。登录限速为数据库实现（`login_attempts` 表，多实例一致）。

## 设计要点

- **推进器 = agents/core 引擎**（B2 换脑）：`run_domain_events` 表是执行事实来源（append-only 领域事件，重放即恢复），v1 行（task_runs/step_runs/artifacts/approvals/agent_events）是其投影，同一事务提交；胶水层见 `omm_api/engine_glue.py`。一次 tick 完成一个阶段步骤；sim 节点将在 T5 由 agents/skills 真实节点替换。
- 契约对齐 `schemas/v1`：status 是生命周期枚举（QUEUED/RUNNING/WAITING_APPROVAL/...），`current_node` 是领域阶段（PROBLEM_ANALYSIS/...），两轴分离（规划 §12.3）。
- 统一错误信封 `{code, message, request_id, details}`；`X-Request-Id` 响应头贯穿日志。
- `agent_events` 是 UI 时间线唯一事实来源；`(run_id, sequence)` 唯一、单调递增。
- SSE `GET /api/v1/task-runs/{id}/events` 支持 `Last-Event-ID`/`after` 断线补拉，终态自动 `stream.end`；历史补拉走 `/events/history`。
- 写操作幂等：创建与动作支持 `Idempotency-Key` 请求头（同键同体重放首响，异体 409）；`approve` 另支持 `client_token`。
- 动作 `approve/pause/resume/cancel/retry` 按状态机校验；approve 的 `reject` 选项退回重做 MODEL_PLANNING 并再次请求确认。
- 失败注入：`params.fail_at` / `params.fail_attempts`（兼容 goal 含 `[fail:experiment]`），用于验证 FAILED → retry 链路。
- **Artifact 存储闭环（B4）**：二进制内容按 sha256 内容寻址存放在 `data/artifacts/`（协议可替换，MinIO/S3 待底座就绪）；上传 `POST /api/v1/projects/{id}/artifacts`（multipart，服务端重算哈希），下载 `GET /api/v1/artifacts/{id}/download`（下载即核验，哈希不一致返回 `ARTIFACT_CORRUPTED`）；模拟工作流产物经同一存储端口真实落盘、可下载。
