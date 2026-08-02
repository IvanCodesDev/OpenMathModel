# ADR-0001：采用按部署单元和领域能力划分的 Monorepo

- 状态：Accepted
- 日期：2026-08-02

## 背景

产品同时包含 Web、Tauri、Python API、异步 Worker、Agent 能力、共享协议和数据资产。早期需要快速调整跨端协议，也必须防止 UI、服务与 Agent 逻辑混成一个目录。

## 决策

采用 Monorepo，并按以下维度划分：

- `apps/`：面向用户的可运行客户端。
- `services/`：可部署的服务进程。
- `agents/`：与数学建模 Agent 直接相关的领域能力。
- `packages/`：跨模块共享库与协议。
- `datasets/`：数据清单、样例与处理配方。
- `infra/`、`tests/`、`tools/`、`docs/`：横切工程能力。

Web 和 Desktop 共享 UI 与协议；API 与 Worker 分离；Agent 核心保持框架无关。

## 结果

优点：跨端重构原子化、协议一致、代码发现容易、MVP 运维简单。代价：需要统一 CI、清晰依赖检查，并避免把 Monorepo 误用成无边界的大包。

## 后续约束

只有满足独立扩容、独立权限边界或独立团队所有权之一时，才把模块拆成新服务。
