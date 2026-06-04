# Sync the lib-python-* dependencies to the refs declared in pyproject.toml.
#
# How the two libs are pinned:
#   lib-python-config  -- floats on `@release/0.x` (a moving branch). pip
#                         will NOT re-pull a branch dep whose version string
#                         is unchanged, so a local dev env can silently lag
#                         behind CI (which always starts with a clean env).
#   lib-python-projects -- pinned to an exact immutable tag (@v0.1.11). The
#                         tag never moves, so drift is impossible; however a
#                         local `pip install -e <lib>` checkout can still
#                         shadow the released package entirely, making pytest
#                         run green locally while CI runs against the real pin.
#
#   This script force-reinstalls EXACTLY the specs declared in pyproject.toml
#   for both libs (no local checkout, no editable), so a local test run uses
#   the same packages as CI. For lib-python-config this also surfaces any
#   release/0.x drift immediately -- locally, not only in the pipeline.
#
# Usage (from anywhere; paths resolve off the repo root):
#   pwsh -File scripts/sync-libs.ps1
#
# Runs on Windows PowerShell 5.1 and PowerShell 7+ (Windows / Linux).

$root = (Resolve-Path "$PSScriptRoot/..").Path

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# Locate a Python launcher. setup-python (CI) and typical local installs
# both expose `python`; fall back to `python3` on POSIX-only setups.
$py = $null
foreach ($cand in @("python", "python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
}
if (-not $py) { Fail "No python/python3 found on PATH." }

# Single source of truth: pull the lib specs straight from pyproject.toml
# so this script never drifts from the declared dependency (branch, URL).
$pyproject = Join-Path $root "pyproject.toml"
if (-not (Test-Path $pyproject)) { Fail "pyproject.toml not found at $pyproject." }

$specs = Select-String -Path $pyproject -Pattern '"(lib-python-[^"]+@[^"]+)"' `
    | ForEach-Object { $_.Matches[0].Groups[1].Value }

if (-not $specs -or $specs.Count -eq 0) {
    Fail "No 'lib-python-* @ git+...' specs found in pyproject.toml."
}

Write-Step "Force-reinstalling lib-python-* from pyproject.toml specs"
$specs | ForEach-Object { Write-Host "    $_" }

# --force-reinstall + --no-cache-dir: re-fetch the branch HEAD even though
# the version string is unchanged. --no-deps: only bump the two libs; the
# full dependency tree is resolved by the normal `pip install -e ".[test]"`.
& $py -m pip install --force-reinstall --no-cache-dir --no-deps @specs
if ($LASTEXITCODE -ne 0) { Fail "pip install (lib refresh) failed." }

Write-Step "Libs synced: lib-python-config to release/0.x HEAD; lib-python-projects pinned at v0.1.11."
