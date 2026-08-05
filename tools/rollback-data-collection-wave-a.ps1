param(
    [switch]$IncludeRuntimeData
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
$baseline = Join-Path $root 'artifacts/data-collection-wave-a/baseline/datasets-README.md'
$frontendBaseline = Join-Path $root 'artifacts/data-collection-wave-a/baseline/openmathmodel-ui.ts'
$stylesBaseline = Join-Path $root 'artifacts/data-collection-wave-a/baseline/styles.css'

if (Test-Path -LiteralPath $baseline -PathType Leaf) {
    Copy-Item -LiteralPath $baseline -Destination (Join-Path $root 'datasets/README.md') -Force
} else {
    $original = git show HEAD:datasets/README.md
    if ($LASTEXITCODE -ne 0) { throw 'Could not restore datasets/README.md from baseline or HEAD' }
    [IO.File]::WriteAllText((Join-Path $root 'datasets/README.md'), (($original -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
}

$stylesTarget = Join-Path $root 'apps/web/src/styles.css'
if (Test-Path -LiteralPath $stylesBaseline -PathType Leaf) {
    Copy-Item -LiteralPath $stylesBaseline -Destination $stylesTarget -Force
} else {
    $stylesOriginal = git show HEAD:apps/web/src/styles.css
    if ($LASTEXITCODE -ne 0) { throw 'Could not restore frontend styles from baseline or HEAD' }
    [IO.File]::WriteAllText($stylesTarget, (($stylesOriginal -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
}

$frontendTarget = Join-Path $root 'apps/web/src/legacy/openmathmodel-ui.ts'
if (Test-Path -LiteralPath $frontendBaseline -PathType Leaf) {
    Copy-Item -LiteralPath $frontendBaseline -Destination $frontendTarget -Force
} else {
    $frontendOriginal = git show HEAD:apps/web/src/legacy/openmathmodel-ui.ts
    if ($LASTEXITCODE -ne 0) { throw 'Could not restore frontend library UI from baseline or HEAD' }
    [IO.File]::WriteAllText($frontendTarget, (($frontendOriginal -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
}

$newFiles = @(
    'datasets/catalog/source-registry.json',
    'datasets/catalog/source-registry.schema.json',
    'datasets/catalog/source-snapshot.schema.json',
    'datasets/catalog/knowledge-library.schema.json',
    'datasets/recipes/collect_official_problems.py',
    'datasets/recipes/build_knowledge_library.py',
    'datasets/recipes/ingest_mathmodel_full_problems.py',
    'datasets/recipes/ingest_full_problem_archives.py',
    'apps/web/src/data/knowledge-library.json',
    'docs/implementation/data-collection/wave-a-plan.md',
    'docs/implementation/data-collection/wave-a.patch',
    'docs/implementation/data-collection/verification-record.md',
    'tools/verify-data-collection.ps1',
    'tools/rollback-data-collection-wave-a.ps1'
)
foreach ($relative in $newFiles) {
    $path = Join-Path $root $relative
    if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path -Force }
}

$assetRoot = Join-Path $root 'apps/web/public/problem-assets'
$assetFull = [IO.Path]::GetFullPath($assetRoot)
if (-not $assetFull.StartsWith($root + [IO.Path]::DirectorySeparatorChar)) { throw "Cleanup target escaped workspace: $assetFull" }
if (Test-Path -LiteralPath $assetFull) { Remove-Item -LiteralPath $assetFull -Recurse -Force }
foreach ($relative in @('apps/web/public/problem-pages', 'apps/web/public/problem-figures', 'apps/web/public/problem-files')) {
    $generatedRoot = Join-Path $root $relative
    $generatedFull = [IO.Path]::GetFullPath($generatedRoot)
    if (-not $generatedFull.StartsWith($root + [IO.Path]::DirectorySeparatorChar)) { throw "Cleanup target escaped workspace: $generatedFull" }
    if (Test-Path -LiteralPath $generatedFull) { Remove-Item -LiteralPath $generatedFull -Recurse -Force }
}

if ($IncludeRuntimeData) {
    foreach ($relative in @('datasets/raw/objects', 'datasets/raw/snapshots', 'datasets/raw/sources/github', 'datasets/raw/sources/full-problem-archives', 'datasets/interim/comap_mcm_icm', 'datasets/interim/cmathc_cpmcm', 'datasets/interim/apmcm_problems', 'datasets/interim/github_zhanwen_mathmodel', 'datasets/interim/full_problem_sources')) {
        $candidate = Join-Path $root $relative
        $full = [IO.Path]::GetFullPath($candidate)
        if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar)) { throw "Cleanup target escaped workspace: $full" }
        if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
    }
}

Write-Output "Rolled back Wave A files. Runtime data removed: $IncludeRuntimeData"
