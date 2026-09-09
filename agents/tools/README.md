# Agent Tools

统一封装文件读写、代码执行、数据查询、图表渲染、资料检索和 Artifact 发布；工具返回结构化结果与可追踪元数据。

## 执行工具（ADR-0021）

- `code_run`（`code_run.py`）：多语言统一入口，`arguments = {code, language}` 按任务卡语言分发到 `runners.py` 里的 `LanguageSpec`（今天 `python` / `r`）；语言无执行器或运行时未装 → 显式失败并列出可用语言，不静默换语言。
- `python_run`（`python_runner.py`）：过渡别名，只跑 Python；行为、文案、输出键集与 H2 以来一致，账本按名字连续。
- `runners.py`：共用的子进程执行核（脚本落 `steps/<step>/`、cwd 工作区根、环境白名单、**超时杀整棵进程树**、输出截断、产物按后缀采集）、可执行文件解析（PATH → 安装目录兜底）、`probe_language`（版本 + 可用包，进程内缓存）。
- `sandbox_tools.py`：`ws_list / ws_read / ws_write / env_probe`；`env_probe` 顶层是 Python 指纹，`languages` 块逐语言报可用 / 版本 / 可执行文件 / 可用包 / deps_hash（ExecutorProfile 数据源）。
- `run_memo.py`：运行记忆化原语（§7.5）——`(代码, 语言运行时, 输入数据哈希, 参数)` 指纹与内存 / 目录两种存取实现；命中后怎么用归节点侧。

本机没有 R 时，`agents/tools/tests` 里的真 R 用例按 `Rscript not installed on this executor` skip；CI 装 R 真跑。
