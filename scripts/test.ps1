# Canonical local test entrypoint.
#
# Syncs the lib-python-* deps to the refs in pyproject.toml (lib-python-config
# floats on release/0.x — this surfaces any branch drift locally rather than
# only in CI; lib-python-projects is tag-pinned at v0.1.7 — this overrides any
# local editable-install shadow), then runs pytest. Extra args go to pytest.
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
