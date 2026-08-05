$ErrorActionPreference = 'Stop'

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Output 'DEV_STACK_VERIFY_BLOCKED {"reason":"docker_not_found","hint":"安装 Docker Desktop 后先运行 tools/dev-up.ps1"}'
    exit 2
}

$failures = @()
$health = @{}

foreach ($name in @('omm-dev-postgres', 'omm-dev-redis', 'omm-dev-minio')) {
    $status = docker inspect --format '{{.State.Health.Status}}' $name 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $status) {
        $health[$name] = 'missing'
        $failures += "${name}:not_running"
        continue
    }
    $health[$name] = ($status | Out-String).Trim()
    if ($health[$name] -ne 'healthy') { $failures += "${name}:$($health[$name])" }
}

if ($health['omm-dev-postgres'] -eq 'healthy') {
    $pgUser = if ($env:OMM_PG_USER) { $env:OMM_PG_USER } else { 'openmathmodel' }
    $pgDb = if ($env:OMM_PG_DB) { $env:OMM_PG_DB } else { 'openmathmodel' }
    docker exec omm-dev-postgres pg_isready -U $pgUser -d $pgDb *> $null
    if ($LASTEXITCODE -ne 0) { $failures += 'postgres:pg_isready_failed' }
}

if ($health['omm-dev-redis'] -eq 'healthy') {
    $pong = docker exec omm-dev-redis redis-cli ping 2>$null
    if (($pong | Out-String).Trim() -ne 'PONG') { $failures += 'redis:ping_failed' }
}

if ($health['omm-dev-minio'] -eq 'healthy') {
    $minioPort = if ($env:OMM_MINIO_PORT) { $env:OMM_MINIO_PORT } else { '9000' }
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$minioPort/minio/health/live" -UseBasicParsing -TimeoutSec 10
        if ($resp.StatusCode -ne 200) { $failures += "minio:health_http_$($resp.StatusCode)" }
    }
    catch { $failures += 'minio:health_unreachable' }
}

if ($failures.Count -gt 0) {
    Write-Output ('DEV_STACK_VERIFY_FAILED ' + (@{ failures = $failures } | ConvertTo-Json -Compress))
    exit 1
}

$summary = [ordered]@{
    postgres = $health['omm-dev-postgres']
    redis    = $health['omm-dev-redis']
    minio    = $health['omm-dev-minio']
}
Write-Output ('DEV_STACK_VERIFY_OK ' + ($summary | ConvertTo-Json -Compress))
exit 0
