$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$venv = Join-Path $root 'agents/.venv'
$python = Join-Path $venv 'Scripts/python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Output "BOOTSTRAP creating venv at agents/.venv (python 3.12)"
    py -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) { Write-Output 'FAIL venv_bootstrap'; exit 1 }
}

& $python -m pytest --version *> $null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install --quiet 'pytest>=8'
    if ($LASTEXITCODE -ne 0) { Write-Output 'FAIL pytest_install'; exit 1 }
}

$packages = @('agents/core', 'agents/tools', 'agents/skills', 'services/worker', 'agents/evals')
# src layout per ADR-0003; until `uv sync` lands, tests import via PYTHONPATH.
$env:PYTHONPATH = ($packages | ForEach-Object { Join-Path $root (Join-Path $_ 'src') }) -join ';'

$failed = @()
$summary = @()
foreach ($package in $packages) {
    Push-Location (Join-Path $root $package)
    try {
        $output = & $python -m pytest tests -q 2>&1 | Out-String
        $status = if ($LASTEXITCODE -eq 0) { 'PASS' } else { 'FAIL' }
        $lastLine = ($output.Trim() -split "`n")[-1].Trim()
        $summary += "$status $package :: $lastLine"
        if ($status -eq 'FAIL') {
            $failed += $package
            Write-Output $output
        }
    }
    finally {
        Pop-Location
    }
}

$summary | ForEach-Object { Write-Output $_ }
if ($failed.Count -gt 0) {
    Write-Output "FAIL packages=$($failed -join ',')"
    exit 1
}
Write-Output "PASS suites=$($packages.Count) runtime=agent-execution-plane"
exit 0
