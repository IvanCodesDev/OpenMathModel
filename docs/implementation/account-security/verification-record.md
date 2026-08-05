# 账户与安全批次 · 验证记录

- 日期：2026-08-04
- 范围：`services/api`（认证/账户安全）、`apps/web`（登录页、设置中心安全面板、侧栏登录态）、`packages/contracts`（未变更，回归确认）
- 结论：**后端与前端静态验证全绿；真实进程冒烟通过**

## 1. 交付内容

### 后端（services/api，包名 `omm_api`）

| 能力 | 端点 | 说明 |
|---|---|---|
| 注册 / 登录 / 登出 | `POST /api/auth/register` `login` `logout` | httpOnly 会话 Cookie；bcrypt 哈希；登录限速（邮箱+IP，10 次/5 分钟） |
| 2FA 登录第二步 | `POST /api/auth/login/2fa` | HMAC 签名挑战令牌（5 分钟有效）；支持 TOTP 或恢复代码 |
| 账户信息 / 资料 | `GET /api/account/me`、`PATCH /api/account/profile` | 改邮箱需当前密码 |
| 修改密码 | `POST /api/account/password` | 成功后撤销除当前设备外全部会话 |
| TOTP 双重验证 | `GET 2fa/setup`、`POST 2fa/enable` `2fa/disable` | RFC 6238 标准库实现（无第三方依赖），启用即发 10 个恢复代码 |
| 恢复代码 | `POST 2fa/recovery-codes` | 重新生成（旧码全部作废）；单码一次性 |
| 登录设备 | `GET /api/account/sessions`、`DELETE /{id}`、`POST revoke-others` | 每次登录=一条可撤销设备会话（UA 解析设备标签） |
| CSRF 基线 | OriginCheckMiddleware | 写方法拒绝陌生 Origin → 403 `ORIGIN_FORBIDDEN` |

集成方式：认证模块（models/schemas/security/deps/routers）**零改动**，由底座适配承接——config 认证常量、`get_db` 别名、兼容双签名 `ApiError`、统一 `/api` 前缀挂载、bcrypt 依赖、Alembic `0002_auth_tables` 迁移。

### 前端（apps/web）

- `/login`：登录 / 注册 / 2FA 三态一体页面（React 实现，复用设计 token）。
- 设置中心「账户与安全」面板：真实接口驱动（资料编辑、改密、2FA 启停、恢复码、设备管理、退出）。
- 侧栏用户信息随登录态同步；未登录点击跳转 `/login?next=…`。
- Vite 代理 `/api` → `127.0.0.1:8000`，同源无 CORS。

## 2. 验证结果

| 验证项 | 命令 | 结果 |
|---|---|---|
| 后端测试 | `.venv\Scripts\python -m pytest services/api/tests/test_projects.py test_task_runs_and_actions.py test_events_sse.py test_totp.py test_auth_flow.py -q` | **28 passed**（认证 E2E 8 + TOTP RFC 向量 3 + 项目 4 + 任务动作 8 + 事件 SSE 5） |
| 契约回归 | `pytest packages/contracts/python/tests -q` | 24 passed（本批未改契约） |
| 前端静态 | `npm run check --workspace @openmathmodel/web` | typecheck + eslint 通过 |
| 前端构建 | `npm run build --workspace @openmathmodel/web` | 成功（chunk >500kB 警告为题库 JSON 既有问题） |
| 真实进程冒烟 | uvicorn 启动后 PowerShell 实测 | `/api/health` ok；注册→Cookie 会话→`/me`→设备列表 1 条；陌生 Origin 写请求 403；任务运行 8 秒推进至 NEEDS_REVIEW，approve 后 COMPLETED，25 条事件 |

修复的真 Bug：事件序列取号在 `autoflush=False` 下未 flush 挂起行导致同 tick 撞号（`UNIQUE(run_id, sequence)` 拦截），已在 `omm_api/events.py::next_sequence` 修复并由全套测试回归。

## 3. PostgreSQL 落地（决策：数据库改用 PostgreSQL）

- 部署：本机无 Docker → EnterpriseDB **PostgreSQL 17.10** 免安装二进制（官方源，333,925,750 字节与 Content-Length 一致），用户级部署于 `E:\Tools\pgsql-omm`（无系统服务，删除目录即卸载）。管理脚本 `tools/pg-dev.ps1`（init/start/stop/status，端口 5433，部署位置可用 `OMM_PG_HOME` 覆盖）。
- 驱动：`psycopg[binary] 3.x`；默认 `OMM_DATABASE_URL` 已切为本地 PG，SQLite 保留为环境变量可选的零依赖路径。
- 验证结果：

| 验证项 | 结果 |
|---|---|
| `alembic upgrade head` 对 PG 实跑 | 0001 + 0002 干净通过，9 张业务表 + alembic_version 全部就位 |
| 全套 pytest 对 PG 实跑（`OMM_TEST_DATABASE_URL`，每用例重建 schema 隔离） | **28/28 passed**（行锁、JSON 列、唯一约束、SSE、认证全链路在真库成立） |
| uvicorn（PG 默认配置）服务冒烟 | health ✓ 注册+Cookie 会话 ✓ /me ✓ 任务闭环 NEEDS_REVIEW→approve→COMPLETED ✓ |

## 4. NOT RUN 与已知风险

| 项 | 状态 | 说明 |
|---|---|---|
| 浏览器端 2FA 全链路人工验证 | NOT RUN | 已由 TestClient E2E 覆盖等价逻辑；建议在浏览器人工过一遍 `/login` |
| 登录限速多实例一致性 | 已知限制 | 进程内 RateLimiter，多副本部署时需换 Redis（代码内已注明） |
| SECRET_KEY / 本地 PG 凭据 | 已知限制 | 开发默认值仅限本地；生产必须以 `OMM_SECRET_KEY`、`OMM_DATABASE_URL` 覆盖 |
| 共享 venv 拉锯 | 风险 | 并行 Agent 曾以同名发行包（contracts 根 src 布局的另一套 omm-contracts）覆盖本包 editable 安装导致导入错乱，已强制装回并复验；根治需统一契约包归属 |
| 孤儿代码 | 待仲裁 | `omm_api/{simulator,idempotency}.py`、`omm_api/routes/`、4 个旧测试、`services/api/src/`、`packages/contracts/{schemas/v1,fixtures,validate.py,src/omm_contracts,pyproject.toml}` 来自并行 Agent，本批未动未删，待归属方确认处置 |

## 5. 回滚

本批全部改动未提交 Git（按项目规则等待明确指示）。回滚 = 丢弃工作树对应文件改动；SQLite 开发库删除 `services/api/data/dev.db` 即重置；本地 PostgreSQL 停止后删除 `E:\Tools\pgsql-omm` 即完全移除（无系统服务与注册表残留）。
