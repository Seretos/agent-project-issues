# Canonical local test entrypoint.
#
# Refreshes the floating release/0.x libs to their branch HEAD (so local
# results match CI and any lib drift surfaces here, not only in the
# pipeline), then runs pytest. Any extra args are forwarded to pytest.
#
# Usage (from anywhere):
#   pwsh -File scripts/test.ps1
#   pwsh -File scripts/test.ps1 tests/test_pulls.py -k labels -q
#
# Prefer this over a bare `python -m pytest`: a bare run uses whatever lib
# happens to be installed (possibly a stale wheel or a local editable
# checkout), which can hide release/0.x drift. See AGENTS.md.

$root = (Resolve-Path "$PSScriptRoot/..").Path

& "$PSScriptRoot/sync-libs.ps1"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$py = $null
foreach ($cand in @("python", "python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
}
if (-not $py) { Write-Host "ERROR: No python/python3 on PATH." -ForegroundColor Red; exit 1 }

Push-Location $root
try {
    & $py -m pytest @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
