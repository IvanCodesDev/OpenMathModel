# OpenMathModel 本地 PostgreSQL（免安装二进制，用户级，无系统服务）管理脚本
#
# 用法（一行只写一条命令；Action 只接受下列一个动作，两条粘在一行会报 ValidateSet 错）：
#   .\tools\pg-dev.ps1 init      # 首次初始化数据目录并启动、建库建用户（幂等）
#   .\tools\pg-dev.ps1 start     # 启动（已在运行则原样返回，不重启）
#   .\tools\pg-dev.ps1 stop      # 停止（fast 模式，等到退出）
#   .\tools\pg-dev.ps1 restart   # 停止后重新启动
#   .\tools\pg-dev.ps1 status    # 状态（含 PID）
#
# 目录约定：$env:OMM_PG_HOME 指向部署根（默认 E:\Tools\pgsql-omm，本机约定，可覆盖）。
#   <PG_HOME>\pgsql\bin   PostgreSQL 二进制（EnterpriseDB binaries zip 解压产物）
#   <PG_HOME>\data        数据目录（initdb 生成）
#   <PG_HOME>\pg.log      服务日志
# 连接约定：127.0.0.1:5433，应用库 openmathmodel / 用户 openmathmodel（密码 openmathmodel，仅本地开发）。
#
# 控制台隔离（2026-09-03 起）：postgres 经 Start-Process 在独立的隐藏控制台里启动，不再继承调用者
# （用户终端 / uvicorn 子进程 / npm run dev）的控制台。否则 Windows 的 CTRL_C_EVENT 会广播给同一
# 控制台的全部进程——uvicorn --reload 每次重启子进程、在起过 PG 的终端里按 Ctrl+C、关掉那个终端，
# postgres 都会把它当 SIGINT 执行 fast shutdown，表现为「API 一 reload 库就没了」。

param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "start", "stop", "restart", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"

$PgHome = if ($env:OMM_PG_HOME) { $env:OMM_PG_HOME } else { "E:\Tools\pgsql-omm" }
$Bin = Join-Path $PgHome "pgsql\bin"
$Data = Join-Path $PgHome "data"
$LogFile = Join-Path $PgHome "pg.log"
$Port = 5433
$SuperPassword = "openmathmodel-dev"
# pg_ctl start -w -t 60 的等待上限，再留 PowerShell / 崩溃恢复的余量
$StartTimeoutMs = 90000

function Assert-Binaries {
    if (-not (Test-Path (Join-Path $Bin "pg_ctl.exe"))) {
        throw "未找到 PostgreSQL 二进制：$Bin。请先解压 binaries zip 到 $PgHome（包含 pgsql 目录），或设置 OMM_PG_HOME。"
    }
}

function Test-Running {
    & (Join-Path $Bin "pg_ctl.exe") status -D $Data 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-PostmasterPid {
    $pidFile = Join-Path $Data "postmaster.pid"
    if (Test-Path $pidFile) { return (Get-Content $pidFile -TotalCount 1).Trim() }
    return "?"
}

function Show-LogTail {
    if (Test-Path $LogFile) {
        Write-Output "--- $LogFile（末 15 行）---"
        Get-Content $LogFile -Tail 15
    }
}

function Start-Pg {
    if (Test-Running) {
        Write-Output "PostgreSQL 已在运行（port $Port，PID $(Get-PostmasterPid)）"
        return
    }
    if (-not (Test-Path (Join-Path $Data "PG_VERSION"))) {
        throw "数据目录尚未初始化：$Data。首次使用请先执行 .\tools\pg-dev.ps1 init"
    }
    # pg_ctl 会经 cmd.exe 起 postgres 并一起驻留。-WindowStyle Hidden 走 ShellExecute → 新建一个隐藏
    # 控制台，pg_ctl→cmd→postgres 都挂在那上面，也不继承本进程的标准句柄（调用方若在捕获输出，管道
    # 不会被 postgres 长期持有）。不能用 -Wait：PowerShell 的 -Wait 会连子孙进程一起等，postgres 不退
    # 它就不退；改用 Process.WaitForExit 只等 pg_ctl 本身。
    $pgCtl = Join-Path $Bin "pg_ctl.exe"
    $arguments = @("start", "-D", "`"$Data`"", "-l", "`"$LogFile`"", "-w", "-t", "60")
    $proc = Start-Process -FilePath $pgCtl -ArgumentList $arguments -WindowStyle Hidden -PassThru
    if (-not $proc.WaitForExit($StartTimeoutMs)) {
        try { $proc.Kill() } catch { }
        Show-LogTail
        throw "pg_ctl start 超过 $($StartTimeoutMs / 1000) 秒未返回，已放弃等待；看日志 $LogFile"
    }
    if ($proc.ExitCode -ne 0 -or -not (Test-Running)) {
        Show-LogTail
        throw "PostgreSQL 启动失败（pg_ctl exit=$($proc.ExitCode)）。常见原因：端口 $Port 被占（netstat -ano | Select-String ':$Port'）、数据目录被另一实例占用、上次未正常关闭正在恢复；详情见上方日志。"
    }
    Write-Output "PostgreSQL 已启动（port $Port，PID $(Get-PostmasterPid)，日志 $LogFile）"
}

function Stop-Pg {
    if (Test-Running) {
        & (Join-Path $Bin "pg_ctl.exe") stop -D $Data -m fast -w
        if ($LASTEXITCODE -ne 0) { throw "PostgreSQL 停止失败（pg_ctl exit=$LASTEXITCODE）" }
        Write-Output "PostgreSQL 已停止"
    } else {
        Write-Output "PostgreSQL 未在运行"
    }
}

switch ($Action) {
    "init" {
        Assert-Binaries
        if (-not (Test-Path (Join-Path $Data "PG_VERSION"))) {
            $pwfile = Join-Path $env:TEMP "omm-pg-pw.txt"
            Set-Content -Path $pwfile -Value $SuperPassword -Encoding ascii -NoNewline
            & (Join-Path $Bin "initdb.exe") -D $Data -U postgres -E UTF8 --locale=C `
                -A scram-sha-256 --pwfile=$pwfile
            Remove-Item $pwfile -Force
            Add-Content -Path (Join-Path $Data "postgresql.conf") -Value "`nport = $Port`nlisten_addresses = '127.0.0.1'"
            Write-Output "数据目录初始化完成：$Data"
        } else {
            Write-Output "数据目录已存在，跳过 initdb"
        }
        Start-Pg
        $env:PGPASSWORD = $SuperPassword
        $psql = Join-Path $Bin "psql.exe"
        $roleExists = & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='openmathmodel'"
        if ($roleExists -ne "1") {
            & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -c "CREATE ROLE openmathmodel LOGIN PASSWORD 'openmathmodel'"
        }
        $dbExists = & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='openmathmodel'"
        if ($dbExists -ne "1") {
            & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -c "CREATE DATABASE openmathmodel OWNER openmathmodel"
        }
        $testDbExists = & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='openmathmodel_test'"
        if ($testDbExists -ne "1") {
            & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -c "CREATE DATABASE openmathmodel_test OWNER openmathmodel"
        }
        Remove-Item Env:\PGPASSWORD
        Write-Output "就绪：postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:$Port/openmathmodel"
    }
    "start" { Assert-Binaries; Start-Pg }
    "stop" { Assert-Binaries; Stop-Pg }
    "restart" { Assert-Binaries; Stop-Pg; Start-Pg }
    "status" {
        Assert-Binaries
        if (Test-Running) { Write-Output "运行中（port $Port，PID $(Get-PostmasterPid)，数据目录 $Data，日志 $LogFile）" }
        else { Write-Output "未运行（数据目录 $Data；启动：.\tools\pg-dev.ps1 start）" }
    }
}
