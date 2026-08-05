param([switch]$Purge)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $root 'infra/docker/compose.dev.yaml'

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Output 'DEV_STACK_DOWN_BLOCKED {"reason":"docker_not_found"}'
    exit 2
}

$composeArgs = @('compose', '-f', $compose, 'down')
if ($Purge) { $composeArgs += '--volumes' }
& docker @composeArgs
if ($LASTEXITCODE -ne 0) {
    Write-Output "DEV_STACK_DOWN_FAILED {`"compose_exit`":$LASTEXITCODE}"
    exit 1
}

Write-Output ('DEV_STACK_DOWN_OK {"purge":' + ([bool]$Purge).ToString().ToLower() + '}')
exit 0
