# ADR-0005：将 `services/` 更名为 `backend/`

- 状态：Accepted
- 日期：2026-08-05
- 关联：ADR-0001（Monorepo 边界）、ADR-0003（workspace 根；本 ADR 取代其标题结论与后续约束第 4 条）

## 背景

ADR-0003 决定"不新增 `backend/` 目录"，理由是按语言/端分组会与按部署单元分组的语义冲突，并列举了三个不该被收拢的目录：

- `agents/core` 是框架无关的领域库，需被 Worker、评测、CLI、桌面端 sidecar 引用；
- `packages/contracts` 是跨语言事实来源，`apps/web` 真实 import 其生成的 TS 类型；
- `datasets/recipes` 是数据管道脚本，不属于任何单个服务。

**这三条理由至今成立，本 ADR 不推翻它们。**

变化的是另一件事。ADR-0003 把"后端很散"当作纯粹的感受偏差处理，认为用 workspace 根解决依赖入口问题后症状即消失。实际使用中症状没有消失。复盘后确认两个此前未被识别的事实：

1. **`services/` 恒定只有两个成员**（`api`、`worker`）。ADR-0001 后续约束要求满足独立扩容、独立权限边界或独立团队所有权之一才拆新服务，因此短期内不会增加。一个恒定只有两项的分类层，其命名承担的区分作用有限。
2. **`services/` 这个名字没有表达它与其他顶层目录的实际差别**。`agents/*` 同样是服务端代码，`packages/contracts` 同样被服务端消费。真正只存在于该目录下的性质是"可独立部署的后端进程"，而 `backend/` 直接说出了这一点。

## 决策

### 1. `services/` 更名为 `backend/`，不新增目录层级

```
backend/
  api/       ← services/api
  worker/    ← services/worker
```

这是**重命名**，不是 ADR-0003 所拒绝的"新增 `backend/` 收拢后端代码"。区别是实质性的：

- 根目录条目数不变（`services/` 消失，`backend/` 出现），目录深度不变。
- `agents/*`、`packages/contracts`、`datasets/recipes` **全部原地不动**，ADR-0003 的三条理由未被触碰。
- 依赖方向、发行名（`omm-api` / `omm-worker`）、导入名（`omm_api` / `omm_worker`）全部不变。
- 通过 `git mv` 执行，文件历史完整保留。

ADR-0003 §1 拒绝的是"把 Python 代码按语言收拢到一处"；本 ADR 只是给已经存在的部署单元分类层换一个更准确的名字。

### 2. 保留 ADR-0003 的其余全部决策

workspace 根（根 `pyproject.toml` 作为 uv 虚拟根、根 `package.json` 作为 npm workspaces 根）、src 布局、`omm-*` 发行名约定、端口倒置的依赖方向、`datasets/recipes` 与 `agents/prompts` 不登记为 member —— 均不变。

本 ADR 仅取代 ADR-0003 的标题结论、§1 中关于目录名的决定，以及后续约束第 4 条中"引入 `backend/` 之前另写 ADR"这一要求（本 ADR 即为该要求的产物）。

### 3. 同步更新的功能性引用

以下位置硬编码了旧路径，随本次更名一并修改。漏改会导致安装失败或门禁失效：

| 位置 | 性质 |
|---|---|
| 根 `pyproject.toml` `[tool.uv.workspace] members` | uv 成员登记 |
| 根 `pyproject.toml` `[tool.mypy] files` | 类型检查范围 |
| `.gitignore`（`backend/api/data/`） | 本地数据库排除 |
| `.github/workflows/ci.yml` | `pip install -e`、pytest 路径 |
| `tools/verify-project-structure.ps1` | 必需目录清单 |
| `tools/verify-agent-runtime.ps1` | PYTHONPATH 与套件清单 |
| `docs/implementation/project-structure-files.txt` | rollback 脚本据此删文件 |

`services/api/alembic.ini` 用的是相对 `script_location`，`alembic/env.py` 按导入名引用 `omm_api`，二者无需改动。

已安装的可编辑包记录的是旧绝对路径，更名后必须重装：

```powershell
.venv\Scripts\python -m pip install -e packages\contracts -e agents\core -e "backend\api[dev]"
```

### 4. 不改写历史记录

ADR-0001/0002/0003 与 `docs/implementation/**/verification-record.md` 是特定日期的决策与验证快照。其正文中的 `services/api` 等路径保持原样 —— 改写会伪造历史。这些文档记录的是"当时是什么样"，本 ADR 记录"现在改成什么样"。

## 结果

优点：目录名与其实际语义一致（可独立部署的后端进程）；根目录条目数与深度不变；`git mv` 保留全部文件历史；ADR-0003 关于跨端消费目录不得被收拢的约束继续有效。

代价：一次全仓路径引用更新（7 处功能性配置）；需重装可编辑包；历史文档中的旧路径与当前结构不一致，靠本 ADR 的关联关系解释。

## 验证

更名前后运行同一组门禁，结果一致：

| 门禁 | 前 | 后 |
|---|---|---|
| `pytest backend/api/tests` | 53 passed | 53 passed |
| `export_openapi.py --check` | `CONTRACTS_OPENAPI_OK` | `CONTRACTS_OPENAPI_OK` |
| `validate.py` | 7 schema / 26 fixture | 7 schema / 26 fixture |
| `check_compat.py` | `CONTRACTS_COMPAT_OK` | `CONTRACTS_COMPAT_OK` |
| `verify-project-structure.ps1` | `PASS required=28` | `PASS required=28` |
| `uvicorn` 启动 + `/api/health` | `{"status":"ok"}` | `{"status":"ok"}` |

## 后续约束

1. 新增可独立部署的后端进程放在 `backend/` 下，并同步登记根 `[tool.uv.workspace] members` 与依赖方 `[tool.uv.sources]`。
2. `agents/*`、`packages/contracts`、`datasets/recipes` 不得迁入 `backend/`（ADR-0003 §1 的三条理由继续有效）。
3. `omm-agent-core` 不得依赖 `omm-api`、`omm-worker`、`omm-agent-tools` 或 `omm-agent-skills`（承自 ADR-0003 后续约束第 3 条）。
4. 拆分 `services/knowledge` 等新服务之前，仍须按 ADR-0001 后续约束评估并另写 ADR。
