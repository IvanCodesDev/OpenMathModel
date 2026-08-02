param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$path = Join-Path $root 'apps/web/src/styles.css'
$fixed = '#root, #app, .app-shell { width: 100%; height: 100%; }'
$baseline = '#app, .app-shell { width: 100%; height: 100%; }'
$content = Get-Content -LiteralPath $path -Raw

if (-not $content.Contains($fixed)) {
    throw 'Expected fixed viewport selector was not found.'
}

if (-not $Apply) {
    Write-Output "ROLLBACK_READY target=$path mode=preview"
    exit 0
}

$content = $content.Replace($fixed, $baseline)
$content = $content.Replace("/* React mounts into #root; keep the full viewport height that the static #app`r`n   container previously provided so every percentage-based product shell works. */`r`n", '')
Set-Content -LiteralPath $path -Value $content -Encoding utf8
Write-Output "ROLLED_BACK target=$path"
