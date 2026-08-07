# ADR-0003：以 workspace 根统一依赖，不新增 `backend/` 目录

- 状态：Accepted；**标题结论与 §1 目录名部分已由 ADR-0005 取代**（2026-08-05 `services/` 更名为 `backend/`）
- 日期：2026-08-04
- 关联：ADR-0001（Monorepo 边界）、ADR-0002（本地开发底座与工具链版本基线）、ADR-0005（取代本 ADR 的目录名结论）

> 以下正文保留 2026-08-04 的原始决策记录，不改写。本 ADR 关于 workspace 根、src 布局、`omm-*`
> 命名与端口倒置依赖方向的决策**全部继续有效**；仅"不新增 `backend/`"这一条已被 ADR-0005 取代。
> ADR-0005 的更名是把 `services/` 重命名，`agents/*`、`packages/contracts`、`datasets/recipes`
> 仍按本 ADR §1 的三条理由原地保留。正文中的 `services/api`、`services/worker` 现读作
> `backend/api`、`backend/worker`。

## 背景

仓库按 ADR-0001 划分为 `apps/`、`services/`、`agents/`、`packages/`、`datasets/` 等顶层目录。开发中出现两种感受：

1. Python 代码分散在 `services/api`、`services/worker`、`agents/*` 和 `datasets/recipes`，看起来"后端很散"，因此提出新增 `backend/` 收拢。
2. 仓库内没有任何 `pyproject.toml`、`requirements.txt`、`uv.lock`，根目录也没有 `package.json`。没有统一虚拟环境、共享 lint/类型/测试配置和单一安装入口；`datasets/recipes` 的脚本靠 `Path(__file__).resolve().parents[2]` 反向推导仓库根来定位文件。

两种感受指向的是同一个症状，但不是同一个原因。ADR-0002 已把 Python 服务代码的目标版本冻结为 3.12+ 并约定"`services/api` 落地时由 uv 锁定"，本 ADR 落实这一条。

## 决策

### 1. 不新增 `backend/`

按"语言/端"分组会与现有按"部署单元 + 依赖方向"分组的语义冲突：

- `agents/core` 是框架无关的领域库，需要能被 Worker、评测、CLI 和桌面端 sidecar 引用；放进 `backend/` 会暗示它是服务端专属。
- `packages/contracts` 是跨语言事实来源，Web 与 Python 同时消费；放进 `backend/` 会让前端出现指向后端目录的导入。
- `datasets/recipes` 是数据管道脚本，既不属于 API 也不属于 Worker。

只有在确定要拆成前后端两个仓库，或团队规模大到前后端不再互相阅读代码时，才重新评估。

### 2. 新增 workspace 根，目录树保持不变

- 根 `pyproject.toml`：uv workspace 虚拟根（`package = false`），承载共享 ruff / mypy / pytest 配置与 dev 依赖组。
- 根 `.python-version` 固定为 `3.12`，与 ADR-0002 的版本基线一致。
- Python member 采用 src 布局，发行名 `omm-*`，导入名 `omm_*`：

  | 目录 | 发行名 | 导入名 |
  |---|---|---|
  | `packages/contracts` | `omm-contracts` | `omm_contracts` |
  | `agents/core` | `omm-agent-core` | `omm_agent_core` |
  | `agents/skills` | `omm-agent-skills` | `omm_agent_skills` |
  | `agents/tools` | `omm-agent-tools` | `omm_agent_tools` |
  | `agents/evals` | `omm-agent-evals` | `omm_agent_evals` |
  | `services/api` | `omm-api` | `omm_api` |
  | `services/worker` | `omm-worker` | `omm_worker` |

- 根 `package.json`：npm workspaces 根，当前仅含 `apps/web`；`packages/ui`、`packages/domain`、`packages/config` 在真正产生代码时再登记为 member。
- npm 配置上移到根 `.npmrc`：workspace member 内的 `.npmrc` 会被 npm 忽略并每次告警，因此 `apps/web/.npmrc` 的 `fund=false` / `audit=false` 迁到根目录。

### 3. Python 包依赖方向采用端口倒置

`omm-agent-core` 只依赖 `omm-contracts` 与 pydantic，位于依赖图最底层；`omm-agent-tools` 与 `omm-agent-skills` 反向依赖内核并实现其端口，由 `omm-worker` 在运行时装配。

这与 `PROJECT_STRUCTURE.md` 中 `AGENT --> TOOLS / SKILLS` 的概念箭头方向相反：那张图描述的是"编排时会调用"，本 ADR 约定的是包级导入方向。保持内核在底层才能满足 ADR-0001 依赖规则第 4 条（内核框架无关、便于测试和嵌入）。

### 4. 不动的东西

- `datasets/recipes/*.py` 保持原路径。`tools/verify-data-collection.ps1` 与 `tools/rollback-data-collection-wave-a.ps1` 硬编码了这四个脚本路径，移动会让已通过的采集验证门禁失效。该目录暂不登记为 workspace member，继续按 ADR-0002 以 `compileall` 语法门禁覆盖并保持兼容 3.10+。
- `apps/web/package-lock.json` 保留。`tools/verify-react-migration.ps1` 把它列为必需文件。根目录执行 `npm install` 后它会变成冗余文件，但删除会让迁移验证门禁失败。
- `agents/prompts` 是提示模板资源，不是 Python 包，不登记为 member。

## 结果

优点：一条 `uv sync` 建好全部后端环境；member 之间用发行名互相导入，不再靠相对路径反推仓库根；lint、类型检查、测试规则全仓一致，可直接接上 ADR-0002 约定的 CI 升级路径；目录语义与 ADR-0001 和《全流程开发规划》§18.3 完全一致。

代价：根目录多出 `pyproject.toml`、`package.json`、`.npmrc`、`.python-version` 四个文件；新增 Python 包时需要同时登记 member 与 `tool.uv.sources`。

## 后续约束

1. 新增 Python 包必须同时更新根 `[tool.uv.workspace] members` 与依赖方 `[tool.uv.sources]`，否则 uv 会去公网解析同名包。
2. 新增 JS 包必须登记进根 `package.json` 的 `workspaces`。
3. `omm-agent-core` 不得依赖 `omm-api`、`omm-worker`、`omm-agent-tools` 或 `omm-agent-skills`。
4. 拆分 `services/knowledge` 或引入 `backend/` 之前另写 ADR。
5. 本 ADR 只声明配置，尚未执行 `uv lock` / `uv sync`（本机未安装 uv）；首次执行后须把 `uv.lock` 一并提交，并回填 ADR-0002 的工具链现状表。
