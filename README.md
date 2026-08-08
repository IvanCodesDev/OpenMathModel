# OpenMathModel

面向数学建模全流程的 Agent 工作台：从读题、数据处理和模型实验，一路推进到结果解释、论文写作与可复现交付。

> [!IMPORTANT]
> 项目仍在持续开发中。当前 14 个 Web 页面及其视觉、布局和交互是产品界面基线；后续开发只在现有页面中接入真实数据，不另建替代页面。登录与账号安全已有 API 对接，项目、任务、步骤、审批、SSE 事件和 Artifact API 已实现；建模主流程的页面级数据绑定仍在按契约逐步接入。

## 产品预览

<img width="2559" height="1347" alt="image" src="https://github.com/user-attachments/assets/e45c5851-5678-4283-a681-177bf9a5b25a" />
<img width="2527" height="1347" alt="image" src="https://github.com/user-attachments/assets/ac557a3f-5a7e-461f-96ec-88a58f23282f" />
<img width="2559" height="1347" alt="image" src="https://github.com/user-attachments/assets/9afdebc1-3a7a-4a6e-9b3e-dc90ef692736" />


## 为什么是 OpenMathModel

- **一条任务贯穿全流程**：题目理解、数据准备、方案比较、代码实验、稳健性验证、论文撰写和成果导出不再散落在多个工具里。
- **Agent 过程可见、可控**：使用显式状态机推进任务，支持人工确认、暂停、恢复、重试和事件回放。
- **实验可复现**：数据版本、参数、随机种子、指标、日志和产物都纳入统一运行记录。
- **专业知识可复用**：赛题库、优秀论文库、方法库和数据 Recipe 为后续任务提供结构化上下文。
- **面向本地与云端**：Web、API 和 Worker 已有工程实现，桌面端按 Tauri 2 + 本地执行桥接方向建设。

## 当前实现

| 模块 | 状态 | 已有能力 |
|---|---|---|
| Web | 可运行 | React + TypeScript + Vite，使用现有路由表、页面模板和 DOM 适配器承载 14 个产品页面；账号能力已接 API，建模主流程将原位接入任务、审批、SSE 和 Artifact 数据 |
| API | 可运行 | FastAPI、SQLAlchemy 2、Alembic、会话鉴权、项目与任务、动作幂等、SSE 事件、Artifact 校验与下载 |
| Agent | 基础闭环已实现 | 显式状态机、领域事件、回放恢复、结构化节点、Prompt 注册与输出校验 |
| Worker / Tools | 基础闭环已实现 | 文件队列、租约、事件日志、隔离工作区、工具允许列表和 Python 子进程执行 |
| Contracts | 可校验与生成 | OpenAPI + JSON Schema，生成 Python 模型与 TypeScript 类型，提供兼容性检查 |
| Data | 管线已建立 | 来源注册、采集、校验、内容寻址、结构化题面与前端知识库快照 |
| Desktop | 设计与骨架阶段 | 规划复用 Web UI，补充本地文件、密钥库、Python sidecar、离线模式和桌面通知 |

## 建模主流程

```mermaid
flowchart LR
    A["输入题目与附件"] --> B["解析目标、约束与子问题"]
    B --> C["数据画像、清洗与版本化"]
    C --> D["候选方法比较与人工确认"]
    D --> E["代码生成与实验运行"]
    E --> F["评估、稳健性检查与迭代"]
    F --> G["论文写作、图表与成果导出"]
```

## 快速开始

### 1. 启动 Web

前置条件：Node.js `>= 20`、npm。

```powershell
git clone git@github.com:IvanCodesDev/OpenMathModel.git
cd OpenMathModel
npm install
npm run dev:web
```

浏览器访问 [http://localhost:5173](http://localhost:5173)。生产构建可运行：

```powershell
npm run check
npm run build
```

### 2. 启动 API（可选）

前置条件：Python `3.12`。默认使用本地 SQLite，因此无需先启动数据库服务。

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e packages/contracts -e agents/core -e "backend/api[dev]"
.venv\Scripts\python -m uvicorn omm_api.asgi:app --app-dir backend/api --reload --port 8000
```

- API 文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- 健康检查：[http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health)
- 更完整的配置与 PostgreSQL 说明见 [`backend/api/README.md`](./backend/api/README.md)

### 3. 启动开发基础设施（可选）

安装 Docker Desktop 或兼容 Docker Compose v2 的运行时后：

```powershell
.\tools\dev-up.ps1
.\tools\verify-dev-stack.ps1
```

这会启动 PostgreSQL（pgvector）、Redis 和 MinIO。端口、凭据与关闭方式见 [`infra/README.md`](./infra/README.md)。

## 系统架构

![OpenMathModel 系统架构](./docs/assets/readme-system-architecture.png)

_架构图基于当前仓库模块与调用关系生成，视觉语言与 OpenMathModel Web 产品保持一致。_

设计重点是：API 负责控制面，Worker 负责长任务执行，Agent 内核保持框架无关，跨模块协议由 Contracts 统一约束。

## 仓库导航

| 路径 | 职责 |
|---|---|
| [`apps/web`](./apps/web) | React Web 产品与 14 个页面 |
| [`apps/desktop`](./apps/desktop) | Tauri 桌面端入口与原生能力边界 |
| [`backend/api`](./backend/api) | HTTP、鉴权、任务控制、事件与 Artifact API |
| [`backend/worker`](./backend/worker) | 队列消费、租约、事件日志与执行运行时 |
| [`agents`](./agents) | Agent 内核、技能、工具、Prompt 与评测 |
| [`packages/contracts`](./packages/contracts) | OpenAPI、JSON Schema 和跨语言生成类型 |
| [`datasets`](./datasets) | 数据集目录规范、采集 Recipe、Schema 与样例 |
| [`infra`](./infra) | 本地开发基础设施、部署和可观测性 |
| [`docs`](./docs) | 架构、ADR、实现记录与产品路线图 |

更完整的目录与依赖边界见 [`PROJECT_STRUCTURE.md`](./PROJECT_STRUCTURE.md)。

## 开发校验

```powershell
# Web：类型检查、Lint 与生产构建
npm run check
npm run build

# API 测试
.venv\Scripts\python -m pytest backend/api/tests -q

# 契约自检与生成物一致性
.venv\Scripts\python packages/contracts/validate.py
.venv\Scripts\python packages/contracts/check_compat.py
.venv\Scripts\python packages/contracts/scripts/generate_python.py --check
```

GitHub Actions 还会校验 Web、Python、Agent 运行时与生成的 TypeScript 契约。

## 进一步阅读

- [Web 页面基线与前后端对接规范](./docs/development/web-ui-baseline-and-api-integration.md)
- [系统架构](./docs/architecture/system-overview.md)
- [产品路线图](./docs/product/roadmap.md)
- [仓库结构与依赖边界](./PROJECT_STRUCTURE.md)
- [数据集与采集规范](./datasets/README.md)
- [开发基础设施](./infra/README.md)
- [ADR-0001：Monorepo 边界](./docs/adr/0001-monorepo-boundaries.md)
- [ADR-0006：保留现有 Web 界面并原位接入 API](./docs/adr/0006-preserve-web-ui-and-integrate-api.md)
