# ADR-0010：附件图片计数与模型模态感知

日期：2026-08-16 · 状态：已接受（批次一、二、三均已落地）

## 背景

题面附件（PDF/Word/PPT）中的图表、公式截图和示意图经常承载关键约束——本仓库题库样本里就有大量「见图 1 / 见图 2」的题面。当前附件解析管线（[对接规范 §2.3](../development/frontend-backend-agent-integration.md)）只抽取文字层：

1. 服务端 `doc_text.py` 用 pypdf 抽 PDF 文本层，**内嵌图片被静默丢弃**，`artifact_texts` 里没有任何「此文件含 N 张图」的记录；
2. 用户在设置中心配置的模型接口（`llm-config`）**没有模态元数据**，五协议消息桥是纯文本（`{role, content}`），即使配了视觉模型也送不了图；
3. 用户选择纯文本模型（如 DeepSeek 系）上传含图题面时，**没有任何提醒**，图片信息丢失是不可见的。

独立图片附件已有半覆盖：走服务端 OCR（可选 Tesseract），缺依赖时如实 `unsupported`。问题集中在「文档内嵌图」与「模态感知」。

## 决策：分层摄入阶梯 + 三处诚实提醒

原则：**先把「诚实」做到（让用户知道丢了什么），再把「能力」补上（让丢的东西变少）**。

摄入阶梯（按能力降级，每层如实上报）：

1. 文本层抽取（已有）——所有模型可用；
2. 主模型为视觉模型 → 内嵌图直接走多模态内容块（需扩展五协议映射，**远期**）；
3. 主模型纯文本 → 视觉辅助通道把图变文字：PaddleOCR-VL 作为可选文档解析后端（公式→LaTeX、表格→Markdown，**批次二**）；
4. 无任何视觉能力 → 如实告知「K 张图片将被忽略」并引导（**批次一，本 ADR**）。

### 批次一范围（本次落地）

| # | 改动 | 位置 |
|---|---|---|
| 1 | `Extraction.images`：PDF 按页扫 `/Resources//XObject` 计数 `/Image`（Form XObject 不递归，近似值）；docx 数 `word/media/*`；pptx 数 `ppt/media/*`；独立图片恒为 1 | `omm_api/doc_text.py` |
| 2 | `artifact_texts.images` 可空整数列（迁移 0011，additive、SQLite 兼容） | `orm.py` + alembic |
| 3 | `ArtifactText.images` 响应字段（可空；OpenAPI 基线随之刷新） | `api_models.py`、`routers/artifacts.py` |
| 4 | 浏览器侧近似计数：PDF 字节扫描 `/Subtype /Image`（对象流内的字典扫不到，标注「约」）；docx/pptx 数 zip media 条目（准确）；独立图片为 1 | `apps/web/src/attachments/` |
| 5 | 附件卡片显示图片数；草稿 `TaskAttachmentDraft.images` 随任务参数落库 | `composer-attachments.ts`、`draft.ts`、`task-start-state.ts` |
| 6 | 模型模态分类：单一源按名称模式识别（`gpt-5`/`claude-`/`gemini-`/`-vl`/`vision` 等 → 视觉；`deepseek-`/`qwen` 文本线/`kimi-k` → 纯文本；其余 unknown 不打扰） | 新增 `integration/model-modality.ts` |
| 7 | 发送前提醒：附件含图 && 生效模型判定为纯文本 → 附件托盘内显示提醒行（composer-attachments 模块自有 DOM 槽位，不动页面骨架）；`auto` 模式取主接口模型判定，未登录/未配置时保持沉默 | `composer-attachments.ts` |

### 批次二（已落地）

PaddleOCR-VL 作为可选文档解析后端，接入 `doc_text` 的可选依赖降级机制：

- 安装面：`backend/api` 新增 `vl` 附加项（`paddleocr[doc-parser]`）；缺依赖时行为与之前完全一致（扫描件 `empty`、图片回落 Tesseract 或 `unsupported`），如实说明启用途径。
- 触发面：扫描件 PDF（文字层为空）与图片附件优先走 VL，输出逐页 Markdown（公式→LaTeX、表格→表格标记），`engine="paddleocr-vl"`；识别不出内容时 `empty` 并注明。
- 进程模型：惰性单例，首次调用加载模型（可能下载权重）后复用。**开发链在 API 进程内直跑；生产隔离不再做独立守护进程，而是随「API 只排队、Worker 执行长任务」的既定架构迁移到执行面**——重活进 Worker 是仓库既有方向，为 OCR 单开一条进程通道属重复建设。
- 测试面：以假 `paddleocr` 模块注入（`sys.modules`）验证接入契约与单例复位，真实推理不进 CI。

选 PaddleOCR-VL 而非增强 Tesseract 的理由：公式/表格/图表/中文文档是数学建模高频形态，Tesseract 均弱；LaTeX 输出对纯文本推理模型几乎无损。

### 批次三（已落地）：对话附件接线

此前全仓库只有首页新建任务接了附件消费链路（上传建产物，随任务参数进入首轮上下文）；任务页对话区的输入框有附件 UI 却没有消费方，消息发出后附件被静默丢弃。批次三打通这条链路，并保持对话的隐私姿态（对话历史只存页面内存、服务端无状态）：

| # | 改动 | 位置 |
|---|---|---|
| 1 | `POST /api/v1/artifacts/parse` 即席解析（需登录）：不建产物、不落库，解析一次、返回文本、什么都不留；抽取链路与产物正文完全相同（含可选 OCR/VL），OpenAPI 基线随之刷新 | `routers/artifacts.py`、`api_models.py` |
| 2 | 附件折算成随消息发送的上下文块（单附件 8000 字、合计 20000 字预算）：浏览器解析结果优先，图片/扫描件/旧格式现场走即席解析并把权威结果回写附件卡片 | 新增 `attachments/conversation-context.ts`、`attachments/adhoc-parse.ts` |
| 3 | `sendConversationTurn({ attachmentContext })`：上下文块只并入请求内容、不进气泡展示；发送成功清空托盘，失败保留以便重试 | `integration/agent-chat.ts` |
| 4 | 用户气泡下方以纸夹徽标如实展示随消息发送的附件名；发送处理器把 composer 传给对话轮以取到附件集合 | `legacy/openmathmodel-ui.ts`、`attachments/attachments.css` |
| 5 | 任务附件并入对话：运行页对话（含开场分析）读取工作台产物列表中无 `producer_node` 的 READY 产物，取 `GET /artifacts/{id}/text` 权威正文（全部本机解析），按解析就绪进度逐轮并入；发送成功才标记已注入，未就绪的在上下文中如实标注「仍在本机解析中」 | 新增 `attachments/task-attachment-context.ts`、`integration/agent-chat.ts` |

批次一的单模态提醒在对话框内同样生效：附件含图且生效模型纯文本时，发送前即可看到「图片不会被模型看到」。

首次真机使用暴露三处运行缺陷，已随批次三一并加固：① 即席解析在 async 端点里直跑同步 VL 推理（实测首载 50~140 秒）会冻结整个 API 事件循环——`extract_text` 移入线程池，VL 单例与推理加线程锁；② SQLite 默认回滚日志 + 5 秒锁超时在 RunnerThread 高频写库时把并发请求打成 `database is locked`（页面表现为对话 500）——dev 引擎启用 WAL 并把忙等待提到 30 秒；③ 页面 SSE 长连接永不排空，`--reload` 的优雅停机无限等待导致改代码后 API 失联——dev 启动命令统一加 `--timeout-graceful-shutdown 5`（README 手动命令同步）。前端即席解析加 180 秒超时，超时按「解析不可用」如实降级。

### 远期

五协议映射扩展 image 内容块（视觉模型直通）；智能路由按「附件含图」偏向视觉接口；附件摄入报告进 Agent 时间线（依赖 Worker 真实消费附件后落地）。

## 不做什么

- 不在批次一里给 `llm-config` 增加服务端字段：模态判定先放前端（分类是启发式，放前端可快速迭代；等批次二有真实消费再考虑服务端化）。
- 不修改受保护入口与页面骨架；提醒行放在 composer-attachments 模块自建的托盘槽位内。
- unknown 模态不弹提醒：宁可漏报不可误报，误报会训练用户忽略提醒。

## 后果

- 用户在发送前就知道「这个模型看不到图」，可换模型或知情继续；任务参数与 `artifact_texts` 留下图片计数，为批次二的效果评估提供基线。
- 浏览器 PDF 计数是近似值（压缩对象流内的图字典扫不到会漏、极端构造会多），以服务端 pypdf 结果为权威——与既有「浏览器即时预览 + 服务端权威结果」分工一致。
- `deepseek-v4-*` 判为纯文本依据当前产品事实；厂商能力变化时更新 `model-modality.ts` 一处即可。
