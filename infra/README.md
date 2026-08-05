# Infrastructure

- `docker/`：本地开发镜像与组合环境。
- `migrations/`：数据库迁移。
- `deploy/`：部署声明。
- `observability/`：日志、指标、追踪和告警配置。

## 本地开发底座

前置条件：Docker Desktop（Windows/macOS）或兼容 `docker compose` v2 的运行时。

```powershell
# 启动 PostgreSQL(pgvector) + Redis + MinIO 并等待健康
.\tools\dev-up.ps1

# 独立健康验证（输出 DEV_STACK_VERIFY_OK / FAILED / BLOCKED）
.\tools\verify-dev-stack.ps1

# 停止（保留数据卷）；加 -Purge 连数据卷一起清除
.\tools\dev-down.ps1
```

默认端口与凭据（可用 `OMM_*` 环境变量覆盖，仅限本地开发）：

| 服务 | 端口 | 账号 / 密码 |
|---|---|---|
| PostgreSQL 16 (pgvector) | 5432 | openmathmodel / openmathmodel-dev |
| Redis 7.4 | 6379 | 无（仅本地） |
| MinIO S3 API / 控制台 | 9000 / 9001 | openmathmodel / openmathmodel-dev |

版本冻结与供应链说明见 [ADR-0002](../docs/adr/0002-dev-stack-baseline.md)。
