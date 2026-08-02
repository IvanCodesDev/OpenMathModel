$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$required = @(
    'apps/web/package.json', 'apps/web/package-lock.json', 'apps/web/index.html',
    'apps/web/vite.config.js', 'apps/web/src/main.jsx', 'apps/web/src/App.jsx',
    'apps/web/src/screens.jsx', 'apps/web/src/components/OpenMathModelScreen.jsx',
    'apps/web/src/legacy/openmathmodel-ui.js', 'apps/web/src/styles.css',
    'apps/web/src/workflow-refresh.css', 'apps/web/public/assets/OpenMathModel_IP_Crop.png',
    'apps/web/dist/index.html', 'audit-current/migration-backups/demo-source-before-react.zip'
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $root $_) -PathType Leaf) })
if ($missing.Count) {
    $missing | ForEach-Object { Write-Output "MISSING $_" }
    exit 1
}

if (Test-Path -LiteralPath (Join-Path $root 'demo')) {
    Write-Output 'LEGACY_DEMO_PRESENT'
    exit 1
}

$screens = Get-Content -LiteralPath (Join-Path $root 'apps/web/src/screens.jsx') -Raw
$screenCount = ([regex]::Matches($screens, 'export const \w+Screen')).Count
if ($screenCount -ne 14) {
    Write-Output "SCREEN_COUNT_INVALID $screenCount"
    exit 1
}

$legacy = Get-Content -LiteralPath (Join-Path $root 'apps/web/src/legacy/openmathmodel-ui.js') -Raw
if ($legacy.Contains('.html"') -or $legacy.Contains('src="assets/')) {
    Write-Output 'LEGACY_ROUTE_OR_ASSET_REFERENCE_FOUND'
    exit 1
}

$styles = Get-Content -LiteralPath (Join-Path $root 'apps/web/src/styles.css') -Raw
if (-not $styles.Contains('#root, #app, .app-shell { width: 100%; height: 100%; }')) {
    Write-Output 'REACT_ROOT_VIEWPORT_RULE_MISSING'
    exit 1
}

Write-Output "PASS react_screens=$screenCount legacy_demo=ABSENT build=FOUND assets=FOUND root_viewport=PASS"
exit 0
