param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $root 'audit-current/migration-backups/demo-source-before-react.zip'
$demo = Join-Path $root 'demo'

if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    throw 'Migration source archive is missing.'
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archive)
$entries = $zip.Entries.Count
$zip.Dispose()

if (-not $Apply) {
    Write-Output "ROLLBACK_READY archive_entries=$entries target=$demo mode=preview"
    exit 0
}

if (Test-Path -LiteralPath $demo) {
    throw 'Rollback target already exists.'
}
New-Item -ItemType Directory -Path $demo | Out-Null
[IO.Compression.ZipFile]::ExtractToDirectory($archive, $demo)
Write-Output "ROLLED_BACK archive_entries=$entries target=$demo"
