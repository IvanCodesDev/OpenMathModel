# ADR-0021：多语言 Runner——`code_run(language)` 统一入口、语言随方案确认钉死、R 先行

- 状态：Accepted（地基层已落地：`agents/tools` 的 Runner / `code_run` / `env_probe` 多语言探测，harness 执行体的语言路由，skills 的非 Python 诚实降级与 R 沙盒模板；节点 / API / worker 的接线与 `IMPLEMENTATION_LANGUAGES` 解锁待下一刀）
- 日期：2026-09-09
- 关联：ADR-0011（编排选型：有界循环与节点注册表）、ADR-0013（终态可返工）、设计文档 §7「执行体：沙盒 Agent 与多语言运行」（§7.2 沙箱分级、§7.3 可复现性、§7.4 多语言 Runner 与 v3.42 语言范围拍板、§7.5 运行记忆化）、§13.3 本地执行器与能力路由

## 背景

沙盒执行体（§7.1）今天只有一个执行器：`python_run` = `python -I main.py` 的 Tier0 子进程沙箱。方案阶段早已让归约人在「当前可用的实现语言」里给每张方案卡选 `language` 并随 G1 一并确认（别名归一、越界回缺省记警告），但可用列表只有 `("python",)`，语言字段是死的。业主拍板（设计文档 v3.42，2026-09-08）：**四语言全要——R、MATLAB、Octave、北太天元**；顺位 R 先行（`Rscript --vanilla` 与 `python -I` 同构、免费、CI 可回归、云 / 本地双栈，不依赖本地执行代理），MATLAB 挂本地执行代理之后，Octave 随 MATLAB 同批兼作 CI 替身，北太天元殿后。

同时有三条既有纪律不能让多语言冲垮：**验收以确定性断言为准、不接受自述**；**无匹配执行器显式失败、不静默换语言**（§7.4）；**如实标未执行、不装作查过**（R-C）。

## 决策

### 1. 一个工具名、按 `language` 分发；`python_run` 保留为过渡别名

`agents/tools` 新增 `code_run`（`CodeRunSandbox`，tier `execute`）：`arguments = {code, language?, timeout_s?}`，`language` 缺省 `python`，别名归一（`python3 / py → python`、`rscript → r`、`北太天元 → baltamatica`，与 skills 的方案卡别名表逐字同口径，测试锁对照）。每种语言一个 `LanguageSpec`（脚本名、启动参数模板、可执行文件候选、额外环境透传、探测方式），共用同一个 `SubprocessRunner` 执行核——脚本落 `steps/<step>/<main.ext>`、cwd = 工作区根、环境白名单、超时、stdout / stderr 截断、新建文件按后缀采集为产物；指标协议语言中立（`OMM_METRICS_JSON:` 行 / `metrics.json`）。

路由三条硬纪律：语言已登记但本部署无执行器（MATLAB / Octave / 北太天元 / Julia）→ `failed` + 文案「尚无执行器…当前可用：python、r。不会自动换用其它语言运行」；语言认得但未启用或运行时未安装 → `failed` + 可行动的安装提示；认不出 → `[E210]` 参数错误。**绝不**替模型换语言。

`python_run`（`PythonSandbox`）改为同一执行核的薄壳：名字、构造签名、输出键集、文案逐字不变——预算账本（`tool == python_run` 计沙箱次数）、评测金轨迹、既有装配点继续工作，迁移到 `code_run` 是装配层的事。

### 2. 语言是任务卡的事实，不是模型的选择

harness `SandboxTask` 新增 `language`（缺省 `python`）与 `run_tool`（缺省 `python_run`），非 Python 语言必须走 `code_run`（构造期校验）。执行体的运行跟踪器把 `python_run` 与 `code_run` 同计 R2 预算与证据，并把语言钉在任务卡上：`code_run` 漏传 `language` → 补任务卡语言；传了别的语言、或语言不是 python 却调 `python_run` → 退回一条观察（不执行、不计预算），大小写 / 别名归一后相同则改写成契约标识放行。别名归一由调用方注入（`run_sandbox_task(normalize_language=)`），harness 不 import tools / skills、不存第三份别名表。波次提示词对 `code_run` 任务卡点明「language 固定为 `<lang>`，不要换用其它语言」；`python_run` 任务卡措辞与 H2 以来逐字相同。

### 3. R Runner：`Rscript --vanilla`，路径类环境透传，安装目录兜底

`R_SPEC`：`Rscript --vanilla main.R`（不读 `.Rprofile` / `.Renviron`，冷启动可复现），可执行文件先 PATH `which`，Windows 安装器不改 PATH 时按 `Program Files\R\R-*\bin\Rscript.exe` 版本倒序兜底；装配方可用 `executables={"r": path}` 点名（点名即只认它、不回落）。环境 = 公共白名单 + R 需要的**路径类**变量（`PATH / HOME / USERPROFILE / HOMEDRIVE / HOMEPATH / LOCALAPPDATA / APPDATA / R_HOME / R_USER / R_LIBS* / TMPDIR`）——实测缺 `LOCALAPPDATA` 时 `R_LIBS_USER` 变成 `/R/win-library/4.x`，用户装的包（jsonlite）找不到；密钥类变量一律不透传。基础 R 没有 jsonlite：指标行由模板教模型用 `sprintf` 手写 JSON，`env_probe` 如实报可用包。产物后缀表补 `.r / .m / .jl → code`、`.rds / .rdata / .rda → dataset`；**`.pdf` 有意不入 `figure`**——论文管线把 figure 当可内嵌图片，Rscript 默认设备落的 `Rplots.pdf` 归 other，模板要求显式 `png()` / `svg()`。

### 4. 超时杀整棵进程树（顺手修的既有 bug）

`Rscript.exe → cmd.exe → Rterm.exe`。`subprocess.run(timeout)` 只杀直接子进程，孙进程握着 stdout 管道，`communicate()` 要等脚本自然跑完——实测 `Sys.sleep(12)` 超时 2 s 的调用 12.46 s 才返回且脚本跑完了，「超时」名存实亡；Python 脚本自己 spawn 子进程同病。执行核改为 `Popen + communicate(timeout)`，超时后 Windows `taskkill /F /T`、POSIX 新会话 + `killpg(SIGKILL)`，再收一次管道取回被杀前的部分输出（实测 2.4 s 返回、无残留进程）。附带：`stdin` 改 `DEVNULL`，脚本里的 `input()` 立即 EOFError 而不是挂到超时。探测子进程走同一条路。

### 5. `env_probe` 是 ExecutorProfile 的数据源

`env_probe` 输出顶层 `runtime / version / deps_hash / available_packages`（Python，形状与算法不变）之外新增 `languages`：逐语言 `runtime / version / deps_hash（该语言可用包清单哈希）/ available / executable / available_packages / detail`，与 `available_languages` 顺位表。探测跑在与 `code_run` 相同的环境里（`-I` / `--vanilla` + 白名单），否则「探到了、跑不了」会把不存在的能力写进 ExecutorProfile；任何失败收成 `available=False` + 可行动 detail，从不抛。接线刀据此把 `IMPLEMENTATION_LANGUAGES` 从常量改成随探测解锁（R 可用 = `("python", "r")`），并按任务卡语言取 `languages[lang]` 三键填 SandboxRunReport 的 `env_fingerprint`（契约不变）。

### 6. 非 Python 的确定性检查如实「未执行」

静态检查与符号核验都是 Python `ast` 实现。`run_static_checks(..., language)` 对非 Python 返回一条 `not_run` 信息项（severity `info`，不阻断），而不是空列表——空列表在审稿材料里读作「0 项发现 = 查过没问题」，与「没查」是两件事；`check_symbols(..., language)` 返回 `skipped` + 记号总数，记号既不算命中也不算未命中，材料与质量警告写「未执行」。审稿环照常（审稿人读码不依赖 ast）。

### 7. 沙盒模板按语言分 variant

`<stage>.sandbox.<lang>`：`experiment_code / data_cleaning / validating / paper_figures` 各有 `.sandbox.r` 变体，与 Python 卡同阶段、同占位符、同必填输出键（节点换模板不换变量与终答校验），正文换成 R 纪律（`code_run` + language 固定、`--vanilla` 相对路径、只准基础 R / recommended / 可用包且禁 `install.packages()`、`set.seed`、`sprintf` 指标行、显式 `png()/svg()` + `dev.off()`、`tryCatch` 隔离画图、`stop()` 非零退出）。节点按任务卡语言选 variant、`experiment.R` 路径、`engine_glue` 的 prompt → 节点 / 任务类型映射补四个 id 归接线刀。

### 8. 明确不做（本阶段）

- MATLAB / Octave / 北太天元 Runner：按 v3.42 顺位挂本地执行代理（§13.3）之后；今天只登记为「已知语言、尚无执行器」让路由文案对得上 G1。
- Tier-L 资源限额 / 禁网：随本地执行代理批次；Tier0 的诚实边界不变（网络与孙进程未拦截，只是超时现在能杀干净）。
- 不给模型「Python or R？」的选择权：语言在方案阶段由归约人在可用列表里选、G1 确认，执行阶段只执行。
- 不用 PDF 做图件、不引入 AI 生成示意图：图件必须是真实数据画出的 png / svg。

## 结果

- `code_run` 在两种语言上跑通统一契约（同一执行核、同一产物采集、同一指标协议）；本机与 CI（`r-lib/actions/setup-r`）都有真 R 用例，无 R 时用例如实 skip 而不是假绿。
- 方案阶段确认的语言到执行阶段不会被模型或系统悄悄换掉；换语言的每一次尝试都留下一条可审计的退回观察。
- 沙箱超时对多进程运行时真正生效，孙进程不再拖住管道。
- 代价与边界：R 冷启动约 1 s / 次；探测首次调用起子进程（进程内缓存）；非 Python 脚本的静态检查与符号核验为空档，靠审稿人与人补位，材料里明写。
- 接线刀清单（未做即不算完成）：节点任务卡带语言与 `code_run`、模板按语言选 variant、API / worker 注册 `code_run` 并给 `env_probe` 接入 `code_run.probes`、账本并计 `code_run`、`IMPLEMENTATION_LANGUAGES` 随探测解锁、静态检查 / 符号核验传 `language`、评测脚本化工具认 `code_run`。
