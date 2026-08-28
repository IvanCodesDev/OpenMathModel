# OpenMathModel 本地 PostgreSQL（免安装二进制，用户级，无系统服务）管理脚本
#
# 用法：
#   .\tools\pg-dev.ps1 init     # 首次初始化数据目录并启动、建库建用户（幂等）
#   .\tools\pg-dev.ps1 start    # 启动
#   .\tools\pg-dev.ps1 stop     # 停止
#   .\tools\pg-dev.ps1 status   # 状态
#
# 目录约定：$env:OMM_PG_HOME 指向部署根（默认 E:\Tools\pgsql-omm，本机约定，可覆盖）。
#   <PG_HOME>\pgsql\bin   PostgreSQL 二进制（EnterpriseDB binaries zip 解压产物）
#   <PG_HOME>\data        数据目录（initdb 生成）
#   <PG_HOME>\pg.log      服务日志
# 连接约定：127.0.0.1:5433，应用库 openmathmodel / 用户 openmathmodel（密码 openmathmodel，仅本地开发）。

param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "start", "stop", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"

$PgHome = if ($env:OMM_PG_HOME) { $env:OMM_PG_HOME } else { "E:\Tools\pgsql-omm" }
$Bin = Join-Path $PgHome "pgsql\bin"
$Data = Join-Path $PgHome "data"
$LogFile = Join-Path $PgHome "pg.log"
$Port = 5433
$SuperPassword = "openmathmodel-dev"

function Assert-Binaries {
    if (-not (Test-Path (Join-Path $Bin "pg_ctl.exe"))) {
        throw "未找到 PostgreSQL 二进制：$Bin。请先解压 binaries zip 到 $PgHome（包含 pgsql 目录），或设置 OMM_PG_HOME。"
    }
}

function Test-Running {
    & (Join-Path $Bin "pg_ctl.exe") status -D $Data 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Start-Pg {
    if (Test-Running) { Write-Output "PostgreSQL 已在运行（port $Port）"; return }
    & (Join-Path $Bin "pg_ctl.exe") start -D $Data -l $LogFile -w -t 60
    Write-Output "PostgreSQL 已启动（port $Port，日志 $LogFile）"
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
    "stop" {
        Assert-Binaries
        if (Test-Running) {
            & (Join-Path $Bin "pg_ctl.exe") stop -D $Data -m fast -w
            Write-Output "PostgreSQL 已停止"
        } else {
            Write-Output "PostgreSQL 未在运行"
        }
    }
    "status" {
        Assert-Binaries
        if (Test-Running) { Write-Output "运行中（port $Port，数据目录 $Data）" }
        else { Write-Output "未运行" }
    }
}
