# Sync the lib-python-* dependencies to the refs declared in pyproject.toml.
#
# Both libs are pinned to exact immutable tags (see pyproject.toml). A tag
# never moves, so version drift is impossible; the remaining hazard is a local
# `pip install -e <lib>` checkout, which can shadow the released package
# entirely and make pytest run green locally while CI runs against the real pin.
#
# This script force-reinstalls EXACTLY the specs declared in pyproject.toml
# for both libs (no local checkout, no editable), so a local test run uses
# the same packages as CI.
#
# Usage (from anywhere; paths resolve off the repo root):
#   pwsh -File scripts/sync-libs.ps1
#   pwsh -File scripts/sync-libs.ps1 -Python C:\path\to\.venv\Scripts\python.exe
#
# -Python lets a caller (e.g. build.ps1) target a specific interpreter --
# typically its own project-local .venv -- instead of whatever `python` is
# ambient on PATH. This matters for build.ps1: its .venv can persist across
# runs, and a previously-installed local editable checkout of a lib would
# otherwise keep shadowing the pinned release inside that venv forever.
#
# Runs on Windows PowerShell 5.1 and PowerShell 7+ (Windows / Linux).

[CmdletBinding()]
param(
    [string]$Python
)

$root = (Resolve-Path "$PSScriptRoot/..").Path

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# Locate a Python launcher. Use the caller-supplied interpreter if given;
# otherwise fall back to whatever `python`/`python3` is ambient on PATH
# (setup-python in CI, or a typical local install).
$py = $Python
if ($py) {
    if (-not (Test-Path $py) -and -not (Get-Command $py -ErrorAction SilentlyContinue)) {
        Fail "Specified -Python '$py' not found."
    }
} else {
    foreach ($cand in @("python", "python3")) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
    }
    if (-not $py) { Fail "No python/python3 found on PATH." }
}

# Single source of truth: pull the lib specs straight from pyproject.toml
# so this script never drifts from the declared dependency (tag, URL).
$pyproject = Join-Path $root "pyproject.toml"
if (-not (Test-Path $pyproject)) { Fail "pyproject.toml not found at $pyproject." }

$specs = Select-String -Path $pyproject -Pattern '"(lib-python-[^"]+@[^"]+)"' `
    | ForEach-Object { $_.Matches[0].Groups[1].Value }

if (-not $specs -or $specs.Count -eq 0) {
    Fail "No 'lib-python-* @ git+...' specs found in pyproject.toml."
}

Write-Step "Force-reinstalling lib-python-* from pyproject.toml specs"
$specs | ForEach-Object { Write-Host "    $_" }

# --force-reinstall + --no-cache-dir: re-fetch the pinned tag even though
# a same-version package may already be installed (e.g. an editable shadow). --no-deps: only bump the two libs; the
# full dependency tree is resolved by the normal `pip install -e ".[test]"`.
& $py -m pip install --force-reinstall --no-cache-dir --no-deps @specs
if ($LASTEXITCODE -ne 0) { Fail "pip install (lib refresh) failed." }

Write-Step "Libs synced to the exact tags pinned in pyproject.toml."
