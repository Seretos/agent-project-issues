# Build the project-issues-plugin MCP server into a single-file Windows .exe.
#
# Usage (from plugin root):
#   pwsh -File scripts/build.ps1
#   pwsh -File scripts/build.ps1 -Clean      # remove dist/ build/ first
#   pwsh -File scripts/build.ps1 -Package    # also produce dist/project-issues-plugin-<ver>.zip
#
# Requires: Python 3.11+ on PATH (via py.exe -3 launcher).
# Installs pyinstaller into the user-site or current env if missing.

[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Package
)

$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root

# Note: do NOT set $ErrorActionPreference = "Stop" globally. PowerShell 5.1
# wraps native-command stderr as ErrorRecord, which trips Stop semantics for
# tools like PyInstaller that log heavily to stderr. We check $LASTEXITCODE
# after each native call instead.

function Write-Step($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Fail($msg) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

# 1. Verify Python.
# In CI ($env:CI = "true") prefer python.exe on PATH so we get the version
# that actions/setup-python installed — py.exe consults the registry and can
# pick a different Python (e.g. one from the toolcache) than setup-python
# placed on PATH, which would silently change the bundled interpreter.
# Locally, prefer py.exe -3 because that's what Windows users normally have.
Write-Step "Checking Python"
$script:PyCmd = $null
$script:PyArgs = @()

$preferPython = ($env:CI -eq "true")

if (-not $preferPython -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    $verRaw = & py.exe -3 --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "py.exe"
        $script:PyArgs = @("-3")
        Write-Host "    $verRaw (via py.exe)"
    }
}
if (-not $script:PyCmd -and (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    $verRaw = & python.exe --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "python.exe"
        $note = if ($preferPython) { "via python.exe, CI mode" } else { "via python.exe" }
        Write-Host "    $verRaw ($note)"
    }
}
if (-not $script:PyCmd -and $preferPython -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    # Final fallback inside CI: py.exe if python.exe wasn't found.
    $verRaw = & py.exe -3 --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "py.exe"
        $script:PyArgs = @("-3")
        Write-Host "    $verRaw (via py.exe, CI fallback)"
    }
}
if (-not $script:PyCmd) {
    Fail "No usable Python found. Install Python 3.11+ from https://www.python.org/downloads/ (with the py launcher option)."
}

function Invoke-Py {
    & $script:PyCmd @script:PyArgs @args
}

# 2. Ensure plugin + build deps are installed.
Write-Step "Ensuring dependencies (plugin + pyinstaller)"
Invoke-Py -m pip install --quiet --disable-pip-version-check -e ".[build]"
if ($LASTEXITCODE -ne 0) {
    Fail "pip install failed."
}

# 3. Clean previous build artifacts if requested.
if ($Clean) {
    Write-Step "Cleaning dist/ and build/"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue dist, build
}

# 4. Run PyInstaller.
Write-Step "Running PyInstaller"
Invoke-Py -m PyInstaller project-issues.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) {
    Fail "PyInstaller build failed."
}

$exe = Join-Path $root "dist\project-issues.exe"
if (-not (Test-Path $exe)) {
    Fail "Expected dist/project-issues.exe not produced."
}
$exeSize = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "    dist/project-issues.exe (${exeSize} MB)"

# 5. Copy into bin/ where plugin.json expects it.
# Retries the copy because Defender briefly locks freshly-emitted .exe files.
# If the lock turns out to be a running project-issues.exe (i.e. the dev's own
# Claude Code session has the plugin loaded), surface that clearly.
Write-Step "Copying to bin/project-issues.exe"
New-Item -ItemType Directory -Force -Path "bin" | Out-Null
$copied = $false
for ($i = 0; $i -lt 5; $i++) {
    try {
        Copy-Item -Force $exe "bin/project-issues.exe" -ErrorAction Stop
        $copied = $true
        break
    } catch [System.IO.IOException] {
        Write-Host "    file locked (try $($i+1)/5), retrying..." -ForegroundColor Yellow
        Start-Sleep -Milliseconds 800
    }
}
if (-not $copied) {
    $running = @(Get-Process -Name project-issues -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        $pids = ($running | ForEach-Object { $_.Id }) -join ", "
        Write-Host "    project-issues.exe is still running (PID: $pids)." -ForegroundColor Yellow
        Write-Host "    A Claude Code session likely has the plugin's MCP server loaded."
        Write-Host "    Close it (or run '/mcp' and disconnect 'project-issues') and re-run the build."
        Write-Host "    To kill it now without that:   Stop-Process -Name project-issues -Force"
    }
    Fail "Could not copy dist/project-issues.exe to bin/ -- file remained locked."
}

# 6. Smoke-test: MCP initialize handshake.
# PowerShell 5.1's Process StreamWriter prepends a UTF-8 BOM that MCP rejects.
# Work around it by staging the request in a temp file and using
# Start-Process -RedirectStandardInput, which pipes raw OS bytes.
Write-Step "Smoke-testing the binary (MCP initialize)"
$initMsg = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"build-smoke","version":"1"}}}'
$inFile = [System.IO.Path]::GetTempFileName()
$outFile = [System.IO.Path]::GetTempFileName()
$errFile = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllBytes($inFile, [System.Text.Encoding]::UTF8.GetBytes($initMsg + "`n"))
$proc = Start-Process -FilePath "bin\project-issues.exe" `
    -RedirectStandardInput $inFile `
    -RedirectStandardOutput $outFile `
    -RedirectStandardError $errFile `
    -NoNewWindow -PassThru
if (-not $proc.WaitForExit(8000)) { $proc.Kill(); Start-Sleep -Milliseconds 200 }
$stdout = (Get-Content -Raw -ErrorAction SilentlyContinue $outFile)
$stderrText = (Get-Content -Raw -ErrorAction SilentlyContinue $errFile)
Remove-Item -ErrorAction SilentlyContinue $inFile, $outFile, $errFile
if ($stdout -match '"result"' -and $stdout -match '"protocolVersion"') {
    Write-Host "    handshake OK" -ForegroundColor Green
} else {
    Write-Host "    stdout: $stdout" -ForegroundColor Yellow
    Write-Host "    stderr: $stderrText" -ForegroundColor Yellow
    Fail "Handshake failed -- see output above."
}

# 7. Optional: produce a release zip.
if ($Package) {
    Write-Step "Packaging release zip"
    $version = (Select-String -Path ".claude-plugin/plugin.json" -Pattern '"version"\s*:\s*"([^"]+)"').Matches[0].Groups[1].Value
    $zipName = "project-issues-plugin-$version.zip"
    $zipPath = Join-Path $root "dist\$zipName"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    $stage = Join-Path $root "build\stage\project-issues-plugin"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root "build\stage")
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item -Recurse -Force ".claude-plugin" $stage
    Copy-Item -Recurse -Force "bin" $stage
    if (Test-Path "skills") {
        Copy-Item -Recurse -Force "skills" $stage
    }
    Copy-Item -Force "README.md", "LICENSE" $stage -ErrorAction SilentlyContinue

    # Build the zip via Python's zipfile so we can stamp Unix mode bits into
    # the central directory. Compress-Archive (and the .NET ZipFile API it
    # wraps) emits create_system=0 (FAT) with no external_attr, so unzip on
    # Linux falls back to umask defaults (0644) -- which strips the exec bit
    # off bin/project-issues.exe and breaks installation on Linux/WSL2.
    #
    # The script is staged to a temp file rather than passed via `python -c`
    # because PowerShell's native-command argument parser strips quote
    # characters from the script body, corrupting string literals.
    $pyZipScript = @'
import os, sys, time, zipfile

stage = sys.argv[1]
zip_path = sys.argv[2]
exe_rel = "bin/project-issues.exe"

# 0x8000 == stat.S_IFREG; the high 16 bits of external_attr hold the Unix
# mode when create_system == 3 (Unix). unzip honors this on Linux/macOS.
EXE_ATTR = (0o755 << 16) | 0x8000
REG_ATTR = (0o644 << 16) | 0x8000

with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for dirpath, dirnames, filenames in os.walk(stage):
        dirnames.sort()
        for name in sorted(filenames):
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, stage).replace(os.sep, "/")
            st = os.stat(abs_path)
            mtime = time.localtime(st.st_mtime)[:6]
            zi = zipfile.ZipInfo(filename=rel, date_time=mtime)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3  # Unix
            zi.external_attr = EXE_ATTR if rel == exe_rel else REG_ATTR
            with open(abs_path, "rb") as fh:
                zf.writestr(zi, fh.read())

print(f"wrote {zip_path} ({os.path.getsize(zip_path)} bytes)")
'@
    $pyScriptFile = [System.IO.Path]::GetTempFileName() + ".py"
    [System.IO.File]::WriteAllText($pyScriptFile, $pyZipScript, (New-Object System.Text.UTF8Encoding($false)))
    try {
        Invoke-Py $pyScriptFile $stage $zipPath
        if ($LASTEXITCODE -ne 0) {
            Fail "Python zip-build step failed."
        }
    } finally {
        Remove-Item -ErrorAction SilentlyContinue $pyScriptFile
    }
    $zipSize = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Host "    dist/$zipName (${zipSize} MB)"
}

Write-Step "Done."
Write-Host "bin/project-issues.exe is ready. Plugin manifest already points at it."
