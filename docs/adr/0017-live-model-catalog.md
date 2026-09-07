# ADR-0017：厂商在售型号与单价由服务端从公共目录同步，不再写死在前端

- 状态：Accepted
- 日期：2026-09-06
- 关联：ADR-0014（决策 11：智能路由候选 = 用户已保存接口）、ADR-0015（服务端任务类型路由）、[前后端与 Agent 对接日志 §5.7](../development/frontend-backend-agent-integration.md)、[Web 页面基线与前后端对接规范](../development/web-ui-baseline-and-api-integration.md)

## 背景

设置中心「模型厂商」的九张卡片副标题（`gpt-6-astra / gpt-5.6-sol / …`）、「配置」一键填入的默认模型、「默认模型 ID」的补全列表，以及「用量监控」的费用估算单价表，此前都是写死在代码里的快照：前端 `integration/llm-providers.ts` 的 `PROVIDER_PRESETS[].models`、后端 `usage.py` 的 `PRICING`。每次厂商上新都要改代码（2026-08-13、08-26、09-03 三次手工更新），改完还要等下一次发版。

2026-09-06 用户指出卡片仍显示 `claude-fable-5 / gemini-3.6-flash`，而 Anthropic 与 Google 已分别发布新型号：「难道每次都得重新写前端吗？不能实时同步上吗」。这不是某一行数据过期，而是把「随时间变化的外部事实」当成了产品常量。

## 决策

### 1. 数据源：公共模型目录 models.dev，服务端定时同步

选 [models.dev](https://models.dev) 的 `api.json`（无需密钥、开源社区维护，收录各厂商模型的发布日期、输入/输出模态、上下文、美元单价、状态）作为「厂商在售型号」的来源。实测本机可达（约 1 s，4.5 MB），且已收录用户提到的 `claude-fable-5-1`（2026-09-01）、`gemini-3.8-flash`（2026-09-02）。

不选的方案：

- **只同步已配置厂商**（用用户密钥调各家官方 `/models`）：解决不了「未配置的 Claude / Gemini 卡片过期」——用户正是在配置前看卡片决定配哪家。官方 `/models` 仍作为第二层：`POST /api/llm/models` 已存在，「默认模型 ID」补全把接口自报的清单排在目录之前（那才是这个账号真能用的）。
- **由前端直接拉 models.dev**：跨域、每个浏览器各拉一份 4.5 MB、密钥无关但缓存无处可放；服务端一份缓存全体共享，也便于离线回落与后续换源。

### 2. 服务端 `ModelCatalog`（`omm_api/model_catalog.py`）

- 厂商预设 `PROVIDERS`（品牌名 / 图标键 / 协议 / 官方地址 / 备用域名 / 目录 provider 键 / ID 前缀过滤 / 内置兜底型号）成为**唯一**的型号事实来源；前端 `llm-providers.ts` 只保留品牌骨架（名字、图标、协议、地址），不再含任何模型名。
- 裁剪规则：只留可对话模型——过滤向量 / 语音 / 实时 / 图像视频生成 / 审核 / 翻译专用等 ID 记号与非文本模态，去掉 `deprecated`；`*-latest` 别名与带日期的快照 ID 保留在补全列表但不进卡片亮点；DashScope 只收 `qwen|qwq|qvq`（它还代售 DeepSeek / GLM），Google 只收 `gemini`（同一 API 下挂着 Gemma / Lyria / Veo）。
- 卡片亮点 = 最新三款正式版（`beta` / `preview` 垫后）。曾试「每个家族各取最新一款」以保证旗舰 / 主力 / 轻量各占一席，但目录的 `family` 标注不一致，真实数据上把三代前的轻量档顶进了卡片；按发布时间取最新更可预期，亮点在读取时现算、不随缓存落盘。
- 单价索引：全目录「模型 ID → (美元输入价, 美元输出价)」，官方目录优先、其余平台只补缺（约 3 千条）。
- 刷新：后台守护线程按 `OMM_MODEL_CATALOG_TTL_SECONDS`（默认 6 小时）拉取，拉取失败保留上一份并记录错误，从未成功过则用内置快照；结果落文件缓存 `data/model-catalog.json`（`CACHE_VERSION` 变化即作废重拉），进程重启先读缓存、不等网络。`POST /catalog/refresh` 强制刷新，两次至少间隔 60 s。
- 配置项：`OMM_MODEL_CATALOG_ENABLED`（关闭 = 不出网，只用缓存 / 内置快照；测试夹具关闭）、`OMM_MODEL_CATALOG_URL`（可指向自建镜像，同一 JSON 格式）、`OMM_MODEL_CATALOG_TIMEOUT_SECONDS`、`OMM_MODEL_CATALOG_CACHE_PATH`、`OMM_USD_CNY_RATE`（默认 7.0）。

### 3. 接口

```
GET  /api/llm/catalog          → 目录视图（永不阻塞在出网上）
POST /api/llm/catalog/refresh  → 立即同步后返回新视图；拉不到 502 MODEL_CATALOG_UNREACHABLE（旧数据保留）；服务端关闭同步时 409 MODEL_CATALOG_DISABLED
```

视图：`source`（`catalog` / `builtin`）、`enabled`、`catalog_url`、`synced_at`、`stale`、`refreshing`、`error`，以及 `providers[]`（`id / label / logo / protocol / base_url / alt_hosts / subtitle / source / highlights[] / models[]`），`models[]` 每项含 `id / name / release_date / reasoning / vision / context / status / input_usd / output_usd / alias`。两条都要求登录（与 `/api/llm/*` 一致）。

### 4. 消费方

- **模型厂商卡片**（`legacy/openmathmodel-ui.ts`）：打开设置时拉一次目录，副标题填该厂商亮点；区块标题下如实标注新鲜度（「型号目录同步于 3 分钟前，来源 models.dev」/「尚未同步，型号为内置快照」等），新增「同步型号」按钮调 refresh。都在既有区块与卡片的 DOM 槽位内完成，不改页面结构。
- **「配置」一键填入**：默认模型 = 目录亮点首位（该厂商最新正式版）。
- **「默认模型 ID」补全**：先按 Base URL 域名铺目录型号，聚焦时再合并接口自报清单（接口自报排前）。
- **用量监控费用估算**（`usage.model_pricing`）：先按模型 ID 精确命中目录单价 × 汇率，未收录再走手写前缀表，仍未命中走兜底价。`PRICING` 手写表从「主数据」降级为「目录没有时的兜底」（中转站自定义名、本地标签）。
- **携图对话的模态判定**（`integration/model-modality.ts`）：目录明确收录的型号按目录 `vision`，其余回落命名规则——新型号不再需要改模式表。

### 5. 明确不做

- 不用目录改写 Auto 路由的能力推断（`llm.endpoint_strength`）：那是路由行为，单价 / 上下文能否代表「强弱」需要单独裁定。
- 目录不可达时不阻塞任何页面：卡片显示内置快照并标注过期，功能照常。

## 结果

- 厂商上新后最迟 6 小时内卡片、一键填入、补全与费用估算自动跟上，用户可点「同步型号」立即刷新；前端代码里不再有任何模型名。
- 实机：本机 `uvicorn --reload` 开发后端启动后约 1 s 完成首次同步，`GET /api/llm/catalog` 显示 Anthropic `claude-fable-5-1 / claude-opus-5 / claude-sonnet-5`、Google `gemini-3.8-flash / gemini-3.7-flash / gemini-3.5-flash-lite`、通义 `qwen3.8-flash / qwen3.8-max / qwen3.7-plus`、智谱 `glm-5.3-flash / glm-5.3 / glm-5.2`，单价索引 3170 条。
- 代价：API 进程多一个后台线程与一份约 150 KB 的缓存文件；对第三方公共目录的依赖用「文件缓存 + 内置快照 + 可换源」兜底。费用估算的口径从「手写峰价」变为「目录标价 × 固定汇率」，仍是估算值。

## 验收要求

1. 登录后打开设置中心 → 模型厂商：Anthropic / Google 卡片副标题为目录里最新的正式版，标题下显示「型号目录同步于 … 来源 models.dev」。
2. 点「同步型号」：按钮进入「同步中…」，完成后提示「型号目录已同步到最新」，副标题与时间刷新；断网时提示同步失败、副标题保持上一份。
3. 点 Anthropic「配置」：默认模型 ID 为该厂商目录首位；聚焦「默认模型 ID」输入框可见目录全部型号，填入密钥后再聚焦会合并接口自报清单。
4. 「用量监控」中目录收录型号的费用按目录单价 × 汇率估算；未收录型号沿用手写表。
5. `OMM_MODEL_CATALOG_ENABLED=false` 启动：卡片显示内置快照并标注「服务端已关闭模型目录同步」，「同步型号」返回 409 提示。
6. `pytest backend/api/tests`（含 `test_model_catalog.py`）、`npm run check`、`npm run build`、`node --test` 通过。
