# ADR-0002：本地开发底座与工具链版本基线

- 状态：Accepted
- 日期：2026-08-04

> **当前实现说明（2026-08-11）**：本文中的“本机现状”、`services/api`、workspace 成员、CI 待办与默认数据库描述是决策当日快照。当前路径使用 `backend/*`，npm workspaces 已登记 `apps/web`、`packages/config`、`packages/contracts`，API 默认使用 SQLite；PostgreSQL/Redis/MinIO 是可选兼容底座和目标部署组件。最新运行事实见[系统架构](../architecture/system-overview.md)与[项目结构](../../PROJECT_STRUCTURE.md)。工具链与基础设施版本治理原则继续有效。

## 背景

按开发路线进入契约与服务开发（Phase 0→1）前，仓库缺少三件事：可复现的本地基础设施、统一的工具链版本口径、以及从第一天就能拦截错误的 CI 门禁。当前开发机为 Windows 11，未安装容器运行时（Docker/Podman 均缺失，WSL 无发行版）。

## 决策

### 工具链与运行时版本冻结

| 层 | 冻结口径 | 本机现状 |
|---|---|---|
| Node.js | 24.x（活跃 LTS），CI 与本地同版本 | 24.13.0 |
| npm | 随 Node 24 发行的 11.x | 11.6.2 |
| TypeScript | 6.x（`apps/web` 已锁 `^6.0.3`） | 随项目 |
| Python（服务/脚本） | 服务代码目标 3.12+，落地 `services/api` 时由 uv 锁定；`datasets/recipes` 保持兼容 3.10+ | 3.10.11 |
| PostgreSQL | 16，镜像 `pgvector/pgvector:pg16`（预含 pgvector，为 MVP 检索预留） | 未安装 |
| Redis | 7.4（alpine），仅缓存/限流/Pub-Sub，本地关闭持久化 | 未安装 |
| 对象存储 | MinIO `RELEASE.2025-09-07T16-13-09Z`（仅限本地开发，见下方供应链记录） | 未安装 |
| 容器运行时 | Docker Desktop（或兼容 `docker compose` v2 的运行时） | 未安装，待确认 |
| Temporal | 本批不引入；Phase 2 做 Spike 后另立 ADR | N/A |
| Rust 工具链 | 桌面端阶段（Phase 10）再冻结 | N/A |

### 本地底座形态

- Compose 定义：`infra/docker/compose.dev.yaml`；一键脚本 `tools/dev-up.ps1` / `tools/dev-down.ps1` / `tools/verify-dev-stack.ps1`。
- PostgreSQL 与 MinIO 使用命名卷持久化；Redis 显式关闭持久化，对齐“Redis 不保存唯一状态”的事实来源划分。
- 默认端口 5432/6379/9000/9001，凭据与端口可用 `OMM_*` 环境变量覆盖；默认凭据仅限本地回环开发，禁止用于任何对外部署。

### 依赖安装基线（npm workspaces）

- 仓库根 `package.json` + `package-lock.json` 定义 workspaces（`apps/web`、`packages/config`），共享 TS 配置经 `@openmathmodel/config` 解析。该组文件当前为未提交的工作区改动（前序批次引入，本批完成安装闭环）。
- 统一在仓库根执行 `npm ci`；workspace 模式下依赖与 `.bin` 全部落在根 `node_modules`，`apps/web` 不再持有本地 `node_modules`，禁止在子目录单独 `npm install`。
- `apps/web/package-lock.json`（已提交的独立锁文件）在 workspace 模式下不生效，待根 workspace 文件合入时一并清理。

### CI 门禁（`.github/workflows/ci.yml`）

- web 作业：Node 24，`npm ci` → `npm run check`（tsc + eslint）→ `npm run build`。当前面向已提交树（`apps/web` 独立锁文件）；根 workspace 文件合入后，本作业切换为仓库根 `npm ci` + `npm run check` / `npm run build`，并同步更新缓存路径。
- python 作业：Python 3.12，`compileall` 语法门禁覆盖 `datasets/recipes`；`services/api` 落地时升级为 ruff + mypy + pytest。

## 供应链风险记录（MinIO）

- 上游 `minio/minio` GitHub 仓库已于 2026-04 归档为只读；Docker Hub 社区镜像停更于 `RELEASE.2025-09-07T16-13-09Z`，其后的安全修复（如 2025-10 的 CVE 修复版）仅以 dl.min.io 二进制或自建镜像方式提供。
- 本地开发风险评估：可接受——仅监听回环端口、不承载真实用户数据、不对外暴露。
- 复评条件（任一触发即重新选型并出 ADR）：进入部署或多人共享环境；需要 2025-09 之后的安全修复；对象存储开始承载真实用户数据。候选路径：自建 MinIO 镜像（锁定安全 tag）、Garage / SeaweedFS 等开源替代、或直接使用云 S3。

## 结果

优点：新环境两条命令起底座并可独立验证健康；版本口径统一，CI 从第一天对类型与语法错误红灯。代价：需要安装 Docker Desktop（待用户确认后底座验证才能闭环）；MinIO 版本冻结需按复评条件跟踪。

## 后续约束

- 任何底座镜像升级必须同步更新本 ADR，并重跑 `tools/verify-dev-stack.ps1` 留档。
- `services/api` 落地（T3）时补充 uv 锁定的 Python 3.12 工具链与 lint/type/test 门禁，并回填本表。
