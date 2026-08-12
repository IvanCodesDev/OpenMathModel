# 批次 C（契约统一 / 资源鉴权 / 三屏真实化 / 限速升级）· 验证记录

> **历史记录，非当前产品基线（2026-08-08）**：本记录中的 C3“三屏真实化”前端实现未提交并已回滚；不得把它当作待恢复功能，也不得重新创建替代页面。C1、C2、C4 的后端与契约结论仍可作为历史证据。当前前端必须保留既有 14 页面，并按 ADR-0006 在现有 DOM 槽位中对接 API。

- 日期：2026-08-05
- 范围：`packages/contracts`（归属统一）、`services/api`（鉴权/限速/迁移）、`apps/web`（三屏真实链路）
- 结论：**后端双数据库 45/45 全绿；前端 check+build 全绿；端到端冒烟通过**

## 1. 交付内容

### C1 契约归属统一 + 孤儿清理
- 事实来源收敛为 `schemas/v1` + 生成式 `src/omm_contracts`（并行架构师方案，本批采纳并配套完成）。
- TS 类型 `src/index.ts` 同步 v1 语义（status 生命周期/current_node 双轴、新事件类型、owner 必填）。
- OpenAPI 全部引用改指 `schemas/v1` 并内联枚举；contracts README 重写为统一布局。
- 归档（不删除）至 `artifacts/legacy-20260805/`：旧版扁平 schemas/examples/手写 Python 包、`omm_api/routes/`、`simulator.py`、`services/api/src/` 存根。
- 修复 contracts `package.json` exports：补 `./src/ts/v1` 子路径与通配（exports 映射不做目录 index 解析，曾短暂阻断深导入）。

### C2 资源登录鉴权
- `/api/v1` 项目/任务/事件（含 SSE 与 history）全部要求登录（Cookie 会话），匿名 401 `AUTH_REQUIRED`。
- `Project.owner` 写入真实用户 ID；列表按 owner 过滤；他人资源一律 404（不泄露存在性）；任务/事件经 project→owner 连带校验。
- 审批 resolution.actor 记录真实用户邮箱。
- 兼容：既有 `local-dev` 归属的旧演示数据不再对登录用户可见（属预期，见 §4）。

### C3 三屏真实化
- 首页 composer 输入目标 → `/confirm` 复核（真实目标注水 + 附件占位标注）→「开始任务」真实创建项目+任务（登录守卫，未登录跳 `/login?next=`）→ `/task/running?run_id=…`。
- 运行屏 `TaskLiveScreen`（并行架构师已建，本批打通入口）：SSE 实时时间线、步骤表、WAITING_APPROVAL 审批卡（选项+备注）、暂停/恢复/取消/重试。
- 挂接方式为 activateScreen 后注水（`workbench/live-flow.ts`），legacy 模板零改动。

### C4 登录限速升级
- 进程内内存限速 → `login_attempts` 表实现（`rate_limit.DbLoginRateLimiter`，多实例窗口一致），经 `app.state.login_limiter` 注入；迁移 `0004_login_attempts`。
- 接口（allow/record_failure/reset）保持不变，后续可等价切 Redis。

## 2. 验证结果

| 验证项 | 结果 |
|---|---|
| 后端全量 pytest（SQLite 临时库） | **45/45 passed** |
| 后端全量 pytest（真实 PostgreSQL，`OMM_TEST_DATABASE_URL`） | **45/45 passed**（连续两轮） |
| 前端 `npm run check` + `build` | typecheck + eslint + vite build 全绿（已代码分包） |
| Alembic 对 PG 实跑 | 0003（含漂移修复后 stamp）+ 0004 通过，11 表就位 |
| 端到端冒烟（真实进程 8000 + PG） | 匿名 v1→401 ✓；注册→owner=用户ID ✓；任务自动推进→WAITING_APPROVAL→审批（approval_id+option）→COMPLETED ✓；28 条事件 ✓ |

新增测试：`test_v1_auth_guard.py`（匿名拒绝、跨用户隔离 404、owner 归属、审批 actor 记录）。

## 3. 修复记录

- **PG 开发库漂移**：`openmathmodel` 库曾被 `create_all` 抢先建表（idempotency_records），且缺 v1 列（owner/params/failure_message/step created_at），导致 0003 迁移撞表。处理：外科补列（`ADD COLUMN IF NOT EXISTS`）→ `alembic stamp 0003` → `upgrade head`。修复脚本存档 `artifacts/repair-pg-drift.py`。教训：开发库也应统一走迁移，`create_all` 仅限一次性临时库（测试库）。
- **contracts exports 阻断**：本批新增 exports 字段后目录式深导入（`/src/ts/v1`）失效，已补显式子路径修复。
- PG 首轮全量出现 1 例顺序耦合失败（旧纪元残留 schema），重建后连续两轮全绿。

## 4. NOT RUN 与已知风险

| 项 | 状态 | 说明 |
|---|---|---|
| 浏览器人工过一遍三屏链路 | NOT RUN | 已由端到端 API 冒烟覆盖等价路径；建议实际体验 `/` → `/confirm` → 运行屏 |
| 旧演示数据可见性 | 预期变化 | C2 前创建的 `owner=local-dev` 项目对登录用户不可见；如需保留可手工改 owner |
| 确认屏附件区 | 占位 | 附件上传/解析在后续批次接入（已在界面标注） |
| 限速表清理 | 已内置 | allow() 顺带删过期行；量级极小无需独立任务 |

## 5. 回滚

改动未提交 Git。回滚 = 丢弃工作树改动；PG `alembic downgrade 0003` 可撤销 login_attempts；`artifacts/legacy-20260805/` 内容可原样移回。
