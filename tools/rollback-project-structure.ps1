param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $root 'docs/implementation/project-structure-files.txt'
$files = Get-Content -LiteralPath $manifestPath | Where-Object { $_ -and -not $_.StartsWith('#') }

if (-not $Apply) {
    Write-Output "ROLLBACK_READY files=$($files.Count) mode=preview"
    exit 0
}

foreach ($relative in $files) {
    $path = [IO.Path]::GetFullPath((Join-Path $root $relative))
    if (-not $path.StartsWith($root + [IO.Path]::DirectorySeparatorChar)) {
        throw "Path escapes workspace: $relative"
    }
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Remove-Item -LiteralPath $path -Force
    }
}

Write-Output "ROLLED_BACK files=$($files.Count)"
