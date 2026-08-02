$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$required = @(
    'apps/web', 'apps/desktop',
    'services/api', 'services/worker',
    'agents/core', 'agents/skills', 'agents/tools', 'agents/prompts', 'agents/evals',
    'packages/ui', 'packages/contracts', 'packages/domain', 'packages/config',
    'datasets/catalog', 'datasets/samples', 'datasets/recipes',
    'tests/contract', 'tests/integration', 'tests/e2e', 'tests/fixtures',
    'infra/docker', 'infra/migrations', 'infra/deploy', 'infra/observability',
    'docs/architecture/system-overview.md', 'docs/product/roadmap.md',
    'docs/adr/0001-monorepo-boundaries.md', 'PROJECT_STRUCTURE.md'
)

$missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $root $_)) })
if ($missing.Count -gt 0) {
    $missing | ForEach-Object { Write-Output "MISSING $_" }
    exit 1
}

$ignore = Get-Content -LiteralPath (Join-Path $root '.gitignore') -Raw
foreach ($pattern in @('datasets/raw/**', 'datasets/interim/**', 'datasets/processed/**', '.env')) {
    if (-not $ignore.Contains($pattern)) {
        Write-Output "MISSING_IGNORE $pattern"
        exit 1
    }
}

$preserved = @{
    'audit-current/migration-backups/demo-source-before-react.zip' = 'F93FD810E2B89903E42C915788F48D40A1B7C2B672EDB42F3D31CB8B3420AB01'
}
foreach ($entry in $preserved.GetEnumerator()) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $root $entry.Key)).Hash
    if ($actual -ne $entry.Value) {
        Write-Output "BASELINE_CHANGED $($entry.Key) $actual"
        exit 1
    }
}

Write-Output "PASS required=$($required.Count) preserved=$($preserved.Count) dataset_ignore=PASS"
exit 0
