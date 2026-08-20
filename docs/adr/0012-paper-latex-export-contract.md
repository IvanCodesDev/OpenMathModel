# ADR-0012：论文 LaTeX/PDF 导出契约（提案）

日期：2026-08-20 · 状态：提议（待业主评审；通过后按本案修改 `packages/contracts` 与后端，再接前端菜单）

## 背景

论文编辑器已在浏览器本地把正文序列化为可编译的 `.tex`（公式以 `data-tex` 原始 LaTeX 进入 `equation` 环境），并提供 Word/HTML/LaTeX 源/打印四种本机导出。但数学建模赛事的最终交付以 LaTeX 编译的 PDF 为事实标准，浏览器打印无法达到该排版质量。

三条既有原则约束方案：

1. **离线与隐私**：Web/桌面端要求离线与内网可用，论文正文不应离开部署边界——排除第三方 LaTeX 编译/渲染 API；
2. **契约先行**：Schema 变更先于调用方变更；
3. **重活进执行面**：API 只排队、Worker 执行长任务是既定边界（PaddleOCR-VL 已有先例：开发链在 API 进程内直跑，生产随执行面迁移）。

现状：论文正文尚无服务端持久化契约（README 既定待办），浏览器草稿只存 localStorage。

## 决策（草案）

### 1. 分两阶段

- **阶段 A（本 ADR）**：无状态编译导出——客户端提交 `.tex` 源，服务端排队编译，产物落 Artifact Store。不依赖论文正文持久化契约，与 `POST /v1/artifacts/parse`（即席解析）同一交互谱系，但结果作为正式交付物持久化。
- **阶段 B（另案）**：论文正文页面契约落地后，导出请求改为引用服务端论文版本（`paper_version_id`），客户端不再回传原文。接口形状预留该演进（`source_tex` 与 `paper_version_id` 二选一）。

### 2. 接口契约（OpenAPI v1 增量）

- `POST /api/v1/paper-exports`（需登录；需 `Idempotency-Key`）

  请求体：

  ```json
  {
    "project_id": "string，必填，归属校验",
    "run_id": "string | null，可选，关联工作台运行",
    "format": "pdf | tex",
    "title": "string ≤ 300",
    "source_tex": "string ≤ 2MB，完整 .tex 文档"
  }
  ```

  响应 `202 PaperExport`：`{ id, project_id, run_id, format, status: "QUEUED", artifact_id: null, detail: null, created_at }`

- `GET /api/v1/paper-exports/{id}`（需登录，归属校验）

  响应 `PaperExport`：`status ∈ QUEUED | RUNNING | READY | FAILED | UNSUPPORTED`，完成时带 `artifact_id`，异常时带 `detail`。

- **产物完全复用现有 Artifact 契约，零新增枚举**：
  - `.tex` 源在受理时即落为 `kind="paper"`、`media_type="application/x-tex"` 的 Artifact（READY）——即使编译失败，源文件仍可下载排查；
  - PDF 完成时落为 `kind="paper"`、`media_type="application/pdf"`，且 `inputs=[tex_artifact_id]`——血缘字段是现成的，可复现性直接成立；
  - `format="tex"` 时只落源产物，导出记录直接 READY；
  - 下载沿用现有 `/v1/artifacts/{id}/download`。

- **通知**：带 `run_id` 的导出沿现有 run 事件流追加 `agent_event`（`type="paper.export.finished"`，payload 含 `export_id/status/artifact_id`），工作台可原位提示；无 `run_id` 时前端轮询 GET。

### 3. 数据模型

新表 `paper_exports`：`id, project_id(FK), run_id(FK 可空), format, status, artifact_id(可空), source_sha256, detail(≤500), created_at, started_at, ended_at`。`.tex` 本体不进关系库，按内容寻址走 blobstore（与产物同仓）。迁移保持 additive、SQLite 兼容。

### 4. 执行面与编译器选型

- 编译器选 **Tectonic**（单一可执行、XeTeX 引擎、按需拉包且有本地缓存、输出确定性好）。部署以 `settings.tectonic_path` 配置，默认探测 PATH。
  - 备选对比：完整 TeX Live 体积大、安装重；latexmk 依赖系统 TeX 发行版。Tectonic 与「自托管、离线可用」的项目姿态最契合。
- 开发链：沿 RunnerThread 模式由 API 进程内后台线程消费队列，子进程调用 `tectonic --untrusted`（禁 shell-escape）、独立临时工作目录、超时强杀（`settings.paper_export_timeout_seconds`，默认 120s）。
- 目标态：迁移到 `backend/worker` 队列消费，API 只排队；接口契约不变。**不为编译单开进程通道**——与 ADR-0010 批次二对 VL 的结论一致，重活统一随执行面迁移。

### 5. 诚实降级

- 未安装/找不到 Tectonic：任务落 `UNSUPPORTED`，`detail` 注明启用途径，不假装成功（与 `doc_text` 可选依赖姿态一致）；
- 编译失败：`FAILED`，`detail` 携带日志尾部（截断 ≤ 500 字），tex 源产物仍在；
- 离线部署：Tectonic 首次编译需联网拉宏包，离线方案是部署镜像预热包缓存，文档如实说明。

### 6. 安全与限额

- 登录 + 项目归属校验（复用 AuthContext 与项目 owner 校验先例）；
- `source_tex` ≤ 2MB；每用户同时编译 1 个、全局队列上限可配；
- 子进程隔离：`--untrusted`、独立 tmp 目录、超时强杀；资源配额交由部署层。

## 不做什么

- 不接第三方 LaTeX 编译/渲染 API（隐私、离线、依赖面均不可接受）；
- 公式预览维持 KaTeX 本地渲染，不改为服务端出图；
- 不在本案落论文正文持久化（阶段 B 另案）；
- 不新增 Artifact kind/status 枚举；
- 不动页面骨架：前端入口复用编辑器现有「导出」菜单追加菜单项。

## 后果

- 正向：最终交付获得真 LaTeX 排版 PDF；tex 源与 PDF 同存且有 `inputs` 血缘，可复现；契约面最小（1 张表、2 个端点、0 个新枚举）。
- 代价：API 进程内编译是过渡形态（与 RunnerThread/VL 同类债务），Worker 接线时一并迁移；Tectonic 二进制成为部署新前置（缺失时诚实降级，不阻断其他功能）。

## 实施时的验收要求

1. contracts：`openapi.api.json` 与 `schemas/v1` 增量落地，`validate.py`、`check_compat.py`、双语言生成检查全部通过；
2. 后端：pytest 覆盖排队/成功/失败/未安装/幂等重放/跨用户归属拒绝；
3. 前端：编辑器导出菜单新增「导出 PDF（服务端编译）」，状态轮询与完成提示原位接入，浏览器视觉验收；
4. 文档：`backend/api/README` 补 Tectonic 配置与离线预热说明。

## 待业主确认

1. 端点命名取 `/v1/paper-exports`（与 `task-runs` 同为 kebab 复数）还是挂在 `/v1/papers/exports`；
2. 阶段 A 由客户端直传 `.tex` 是否接受（阶段 B 收敛为服务端论文版本引用）；
3. Tectonic 作为编译器选型是否认可。
