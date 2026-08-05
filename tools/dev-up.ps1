$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $root 'infra/docker/compose.dev.yaml'

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Output 'DEV_STACK_UP_BLOCKED {"reason":"docker_not_found","hint":"安装 Docker Desktop（或兼容 docker compose 的运行时）后重试，见 infra/README.md"}'
    exit 2
}

docker compose -f $compose up -d --wait
if ($LASTEXITCODE -ne 0) {
    Write-Output "DEV_STACK_UP_FAILED {`"compose_exit`":$LASTEXITCODE}"
    exit 1
}

Write-Output 'DEV_STACK_UP_OK {"postgres":5432,"redis":6379,"minio":9000,"minio_console":9001}'
exit 0
