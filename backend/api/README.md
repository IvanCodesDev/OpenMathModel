# backend/api — OpenMathModel API 与当前本地运行面

FastAPI + SQLAlchemy 2 + Alembic。契约见 `packages/contracts`（事实来源 `schemas/v1`，本服务响应模型直接使用 `omm_contracts` 生成模型）。

> 当前边界：API 负责鉴权、项目、TaskRun、审批、事件、Artifact 与工作台投影；本地默认还由 API 进程内 `RunnerThread` 推进 `agents/core` 状态机和 `SimStageNode`。`backend/worker` 是尚未接入 API 调度链的独立执行面原型。

## 本地运行

### 完整 Web + API 联调（推荐）

首次在仓库根安装依赖后，统一启动入口会自动确保本地 PostgreSQL 在运行（未运行则拉起），再启动或复用 API，健康检查成功后启动 Web：

```powershell
# 仓库根
npm run dev
```

登录和工作台需要 API；`npm run dev:web` 只启动 Vite。完整安装、健康检查和登录→Project→TaskRun→`run_id` 的可复制验证流程见[根 README](../../README.md#快速开始)。

### 单独启动 API

```powershell
# 首次：仓库根目录
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e packages/contracts -e agents/core -e agents/skills -e "backend/api[dev]"

# 启动：数据库为 PostgreSQL（见下节）。启动时先探库，本地 pg-dev 实例没起会自动 start 一次；
# --reload-dir 必带——不加时 --reload 监视整个 cwd（含 backend/api/data/），沙盒每写一个 .py
# 就把 API 重启一次；--timeout-graceful-shutdown 让 SSE 长连接不拖死重载
.venv\Scripts\python -m uvicorn omm_api.asgi:app --app-dir backend/api --reload --reload-dir backend/api/omm_api --reload-dir agents --timeout-graceful-shutdown 5 --port 8000
```

- 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/health

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

数据库为本地 PostgreSQL（默认 `127.0.0.1:5433`），Artifact 存放于 `backend/api/data/artifacts/`。

### 数据库：限定 PostgreSQL

`OMM_DATABASE_URL` 的代码默认值即路径 A 的本地实例，正常情况无需设置任何环境变量。SQLite 不再是任何默认路径（仅测试夹具的临时隔离库使用，见下文「测试」）。

```powershell
# A. 免安装用户级 PG（tools/pg-dev.ps1，port 5433；默认连接目标，无 Docker 即可用）
.\tools\pg-dev.ps1 init      # 首次建库；之后 npm run dev 与 API 启动探库都会自动 start（OMM_LOCAL_PG_AUTOSTART=false 可关）

# B. Docker 底座（tools/dev-up.ps1，port 5432，见 infra/docker/compose.dev.yaml）——需显式覆盖连接串
$env:OMM_DATABASE_URL="postgresql+psycopg://openmathmodel:openmathmodel-dev@127.0.0.1:5432/openmathmodel"

# schema 以 Alembic 迁移为准（启动时 create_all 仅兜底建缺失表）
cd backend/api
..\..\.venv\Scripts\python -m alembic upgrade head
```

路径 A 已在 Windows PowerShell 5.1 下端到端验证：init → Alembic 迁移 → API 连接 → 全量测试套件通过。历史 SQLite 数据可用 `tools/migrate-sqlite-to-pg.py` 整库迁入（含 timestamptz 补时区与孤儿行过滤）。

## 环境变量（前缀 OMM_）

| 变量 | 默认 | 说明 |
|---|---|---|
| `OMM_DATABASE_URL` | `postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:5433/openmathmodel` | 数据库限定 PostgreSQL；仅端口/凭据不同（如 Docker 底座 5432）时覆盖。PG 单次建连上限固定 5 秒，库没起时快速报错而不是拖到驱动超时 |
| `OMM_LOCAL_PG_AUTOSTART` | `true` | 启动探库失败且目标是 `tools/pg-dev.ps1` 管的本地实例（Windows、127.0.0.1/localhost:5433）时自动 `start` 一次；Docker 5432、远端库、非 Windows 不触发 |
| `OMM_SECRET_KEY` | `dev-secret-change-me` | 2FA 挑战令牌签名密钥，生产必须覆盖 |
| `OMM_RUNNER_ENABLED` | `true` | API 进程内推进线程；当前由 `agents/core` 与 `SimStageNode` 驱动 |
| `OMM_RUNNER_TICK_SECONDS` | `1.2` | 推进节奏 |
| `OMM_AVATARS_DIR` | `backend/api/data/avatars` | 用户头像内容存储根，与运行产物目录分开 |
| `OMM_AVATAR_MAX_BYTES` | `2097152` | 单个头像上限；前端会先压到 256×256，这是服务端兜底 |
| `OMM_ATTACHMENT_TEXT_MAX_BYTES` | `33554432` | 正文抽取上限，比上传上限更严；超过的附件只留原文件不抽正文 |
| `OMM_OCR_LANGUAGES` | `chi_sim+eng` | 图片 OCR 语言包（Tesseract 回落路径）；未安装 Tesseract 时不生效 |
| `OMM_OCR_API_KEY` | 空 | 远程 OCR（讯飞星辰 MaaS · PaddleOCR，OpenAI 兼容协议）的 API key；留空 = 功能关闭。敏感项，放 `backend/api/.env` 或环境变量 |
| `OMM_OCR_API_BASE_URL` | `https://maas-api.cn-huabei-1.xf-yun.com/v2` | 远程 OCR 的 OpenAI 兼容 Base URL |
| `OMM_OCR_API_MODEL` | `xoppaddleocrv16` | 远程 OCR 的 Model ID |
| `OMM_OCR_API_TIMEOUT_SECONDS` | `60` | 单次识别调用（每页一次）的超时 |

## 测试

```powershell
# 测试夹具默认用 SQLite 临时库（仅测试隔离用途，快且零依赖；产品运行限定 PostgreSQL）
.venv\Scripts\python -m pytest backend/api/tests -q

# 对真实 PostgreSQL 实跑同一套件（每用例独立 schema，用完清理）
$env:OMM_TEST_DATABASE_URL="postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:5433/openmathmodel_test"
.venv\Scripts\python -m pytest backend/api/tests -q
```

## 鉴权

`/api/v1` 全部资源要求登录（httpOnly Cookie 会话）：先 `POST /api/auth/register` 或 `login`。
项目/任务按 `owner`（用户 ID）隔离，他人资源一律 404。登录限速为数据库实现（`login_attempts` 表，多实例一致）。

## 设计要点

- **当前推进器 = API 内嵌 RunnerThread + agents/core 引擎**：`run_domain_events` 表是执行事实来源（append-only 领域事件，重放即恢复），v1 行（task_runs/step_runs/artifacts/approvals/agent_events）是其投影，同一事务提交；胶水层见 `omm_api/engine_glue.py`。一次 tick 完成一个阶段步骤；完整真实 Skills 节点与独立 Worker 接线仍在后续阶段。
- 契约对齐 `schemas/v1`：status 是生命周期枚举（QUEUED/RUNNING/WAITING_APPROVAL/...），`current_node` 是领域阶段（PROBLEM_ANALYSIS/...），两轴分离（规划 §12.3）。
- 统一错误信封 `{code, message, request_id, details}`；`X-Request-Id` 响应头贯穿日志。
- `agent_events` 是 UI 时间线唯一事实来源；`(run_id, sequence)` 唯一、单调递增。
- SSE `GET /api/v1/task-runs/{id}/events` 支持 `Last-Event-ID`/`after` 断线补拉，终态自动 `stream.end`；历史补拉走 `/events/history`。
- 写操作幂等：创建与动作支持 `Idempotency-Key` 请求头（同键同体重放首响，异体 409）；`approve` 另支持 `client_token`。
- 动作 `approve/pause/resume/cancel/retry` 按状态机校验；approve 的 `reject` 选项退回重做 MODEL_PLANNING 并再次请求确认。
- 失败注入：`params.fail_at` / `params.fail_attempts`（兼容 goal 含 `[fail:experiment]`），用于验证 FAILED → retry 链路。
- **Artifact 存储闭环（B4）**：二进制内容按 sha256 内容寻址存放在 `data/artifacts/`（协议可替换，MinIO/S3 待底座就绪）；上传 `POST /api/v1/projects/{id}/artifacts`（multipart，服务端重算哈希），下载 `GET /api/v1/artifacts/{id}/download`（下载即核验，哈希不一致返回 `ARTIFACT_CORRUPTED`）；模拟工作流产物经同一存储端口真实落盘、可下载。
- **附件正文抽取**：`GET /api/v1/artifacts/{id}/text` 返回 Agent 可读的纯文本。抽取放在读取时而不是上传时——上传要对用户即时响应，而几十兆的 PDF 抽一遍要好几秒；产物内容寻址、字节不可变，因此结果缓存在 `artifact_texts` 表里长期复用，服务端补装依赖后用 `?refresh=true` 重跑。`status` 五档：`ready`/`partial`/`empty`/`unsupported`/`failed`，后三档也是 200，调用方要的是原因而不是错误码。docx/pptx/xlsx/ODF/压缩包/纯文本全部用标准库 `zipfile` + `ElementTree` 解（零额外依赖），PDF 用 `pypdf`；旧版 `.doc`（按 FIB 分片表抽正文）、`.xls`、RTF 需要 `pip install -e "backend/api[legacy-docs]"`。图片与扫描件 PDF 的识别走**远程 OCR**（讯飞星辰 MaaS 上的 PaddleOCR，OpenAI 兼容协议，配置 `OMM_OCR_API_KEY` 启用；输出 Markdown、公式为 LaTeX、`engine="paddleocr-api"`），其中扫描件 PDF 还需 `[pdf-ocr]` 附加项（pypdfium2 逐页渲染成图片再上送）；未配置 key 时图片回落本地 Tesseract（`[ocr]` 附加项 + 系统 Tesseract 与语言包）。缺依赖/未配置时返回 `unsupported`/`empty` 并说明原因，不抛 500。
- **用户头像**：`POST /api/account/avatar`（multipart）、`DELETE /api/account/avatar`、`GET /api/account/avatar`。内容走与 Artifact 相同的内容寻址实现但独立目录 `data/avatars/`（归属与回收边界不同），`users` 表只存 `avatar_sha256` 与服务端识别的 `avatar_media_type`。格式按**文件魔数**判定（PNG/JPEG/WebP/GIF），声明的 Content-Type 不作数——头像以同源 URL 回给浏览器，放行 SVG 等同于同源脚本注入；响应固定带 `X-Content-Type-Options: nosniff`。读取只按当前会话返回本人头像，不提供按 user_id 的公开地址。`user_payload.avatar_url` 带内容摘要查询串，换图后 URL 自动变化。
- **SQLite 补列机制（仅显式 SQLite 路径生效）**：`create_all` 只建新表、不改已存在的表，因此对 SQLite 库启动时额外补齐模型新增的**可空**列（`omm_api/db.py`）。数据库已限定 PostgreSQL（以 Alembic 为准）后，该机制只服务测试夹具与应急排查场景；两种方言的 schema 一致性由 `tests/test_migrations.py` 守住。

## 当前与目标边界

| 维度 | 当前默认 | 目标演进 |
|---|---|---|
| 数据库 | PostgreSQL（已限定，开发与部署统一；SQLite 仅测试夹具） | PostgreSQL |
| 工作流推进 | API 进程内 `RunnerThread` | API 发布幂等任务，独立 Worker 消费 |
| 阶段节点 | `agents/core` + `SimStageNode` | 版本化真实 Skills 节点 |
| 文件存储 | 本地内容寻址目录 | S3 兼容对象存储 |

API 路径、共享契约和页面工作台投影在迁移期间保持稳定。最新系统级事实见[系统架构](../../docs/architecture/system-overview.md)。
