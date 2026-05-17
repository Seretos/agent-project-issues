# Build the project-issues-plugin MCP server into a single-file
# native binary for the current host OS.
#
# Usage (from plugin root):
#   pwsh -File scripts/build.ps1
#   pwsh -File scripts/build.ps1 -Clean      # remove dist/ build/ first
#   pwsh -File scripts/build.ps1 -Package    # also produce dist/project-issues-plugin-<ver>.zip
#
# Requires PowerShell 7+ (Windows or Linux) and Python 3.11+:
#   - Windows: py.exe -3 / python.exe
#   - Linux:   python3 on PATH
# Installs pyinstaller into the user-site or current env if missing.
#
# Cross-platform notes (ticket #12):
#   - The Python launcher, the MCP smoke test (BOM workaround), and the
#     post-build file-lock retry loop are Windows-specific concerns and
#     are wrapped in `if ($IsWindows) { ... } else { ... }` guards.
#   - PyInstaller emits a platform-native artifact:
#       dist/project-issues.exe   on Windows
#       dist/project-issues       on Linux
#     The smoke test and the per-OS copy step pick the right name from
#     `$script:BinaryName`.

[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Package
)

$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

# PowerShell 5.1 compatibility shim: the $IsWindows / $IsLinux automatic
# variables only exist in PS 7+. Windows PowerShell 5.1 is Windows-only,
# so when they're undefined we know we're on Windows.
if (-not (Test-Path variable:IsWindows)) {
    $IsWindows = $true
    $IsLinux   = $false
}

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

# Platform-aware artifact naming.
#   Windows: project-issues.exe, copied into bin/windows-x86_64/.
#   Linux:   project-issues,     copied into bin/linux-x86_64/.
# The legacy bin/project-issues.exe location is also populated on
# Windows so the existing plugin.json (which points there today)
# keeps working — #13 will swap plugin.json to a wrapper script.
if ($IsWindows) {
    $script:BinaryName    = "project-issues.exe"
    $script:OsTriple      = "windows-x86_64"
} else {
    $script:BinaryName    = "project-issues"
    $script:OsTriple      = "linux-x86_64"
}
$script:DistBinary = Join-Path $root "dist/$($script:BinaryName)"
$script:OsBinDir   = Join-Path $root "bin/$($script:OsTriple)"

# 1. Verify Python.
# Windows: prefer py.exe -3 locally, python.exe in CI (so we get the
# version that actions/setup-python installed -- py.exe consults the
# registry and can pick a different Python).
# Linux: python3 is the only sensible launcher (no py.exe, the
# actions/setup-python toolchain places python3 on PATH).
Write-Step "Checking Python"
$script:PyCmd = $null
$script:PyArgs = @()

if ($IsWindows) {
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
} else {
    # Linux: hardcoded python3 (D1=A per plan-comment for ticket #12).
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        $verRaw = & python3 --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            $script:PyCmd = "python3"
            Write-Host "    $verRaw (via python3)"
        }
    }
    if (-not $script:PyCmd) {
        Fail "python3 not found on PATH. Install Python 3.11+ (e.g. `apt install python3 python3-pip python3-venv` on Ubuntu)."
    }
}

function Invoke-Py {
    & $script:PyCmd @script:PyArgs @args
}

# 1b. Isolate plugin + build deps in a project-local virtualenv.
# Modern Linux distros (Ubuntu 23.04+, Debian 12+, Fedora 38+) mark the
# system Python as PEP 668 externally-managed, which blocks `pip install`
# against it; a venv sidesteps that without the --break-system-packages
# override. On Windows the marker doesn't exist, but a venv keeps the
# build hermetic anyway. CI's actions/setup-python interpreter has no
# PEP 668 marker either, so the extra venv-create step there is cheap.
$venvDir = Join-Path $root ".venv"
if ($IsWindows) {
    $venvPy = Join-Path $venvDir "Scripts/python.exe"
} else {
    $venvPy = Join-Path $venvDir "bin/python"
}

if (-not (Test-Path $venvPy)) {
    Write-Step "Creating virtualenv at .venv/"
    Invoke-Py -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        if (-not $IsWindows) {
            Write-Host "    On Debian/Ubuntu, ensure python3-venv is installed:" -ForegroundColor Yellow
            Write-Host "      sudo apt install python3-venv" -ForegroundColor Yellow
        }
        Fail "Failed to create virtualenv at $venvDir."
    }
    if (-not (Test-Path $venvPy)) {
        Fail "venv was created but $venvPy is missing."
    }
}

# Rebind Python launcher to the venv. All subsequent Invoke-Py calls
# (pip install, PyInstaller) now run inside the venv.
$script:PyCmd = $venvPy
$script:PyArgs = @()
Write-Host "    Using $venvPy"

# Verify pip is present. On Ubuntu 24.04 without `python3.12-venv`
# installed, `python3 -m venv` succeeds but ensurepip can't find its
# bundled wheels — the resulting venv has no pip. Bootstrap it; if
# that also fails, surface the exact apt package to install.
Invoke-Py -m pip --version > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Step "Bootstrapping pip in venv (ensurepip)"
    Invoke-Py -m ensurepip --upgrade --default-pip
    if ($LASTEXITCODE -ne 0) {
        if (-not $IsWindows) {
            Write-Host "    The venv has no pip and ensurepip cannot bootstrap it." -ForegroundColor Yellow
            Write-Host "    On Debian/Ubuntu, install the per-version venv package, e.g.:" -ForegroundColor Yellow
            Write-Host "      sudo apt install python3.12-venv python3-pip" -ForegroundColor Yellow
            Write-Host "    Then remove the broken .venv/ and re-run:" -ForegroundColor Yellow
            Write-Host "      rm -rf .venv && pwsh scripts/build.ps1" -ForegroundColor Yellow
        }
        Fail "venv has no pip and ensurepip failed."
    }
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

if (-not (Test-Path $script:DistBinary)) {
    Fail "Expected $script:DistBinary not produced."
}
$exeSize = [math]::Round((Get-Item $script:DistBinary).Length / 1MB, 1)
Write-Host "    $($script:DistBinary | Resolve-Path -Relative) (${exeSize} MB)"

# 5. Copy the PyInstaller artifact into bin/<os>-x86_64/. The shell
# wrappers staged in step 5b are the actual plugin.json entry points;
# they dispatch into these per-OS directories at runtime.
Write-Step "Copying to bin/$($script:OsTriple)/$($script:BinaryName)"
New-Item -ItemType Directory -Force -Path $script:OsBinDir | Out-Null
$destOsBin = Join-Path $script:OsBinDir $script:BinaryName

if ($IsWindows) {
    # Defender briefly locks freshly-emitted .exe files. Retry the copy
    # for up to ~4 s (5 tries x 800 ms). If the lock is the dev's own
    # Claude Code session keeping the .exe alive, surface that clearly.
    $copied = $false
    for ($i = 0; $i -lt 5; $i++) {
        try {
            Copy-Item -Force $script:DistBinary $destOsBin -ErrorAction Stop
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
        Fail "Could not copy $($script:BinaryName) to $destOsBin -- file remained locked."
    }
} else {
    # Linux: no AV file-locking, but ensure the destination dir exists
    # and the binary is executable after the copy. PyInstaller emits
    # exec bits on the source but a copy via .NET can lose them on
    # some filesystems; chmod is a cheap safety net.
    Copy-Item -Force $script:DistBinary $destOsBin
    chmod +x $destOsBin
    if ($LASTEXITCODE -ne 0) {
        Fail "chmod +x on $destOsBin failed."
    }
}

# 5b. Stage the shell-wrapper pair from release/wrappers/ into bin/.
# These are the actual entry points referenced by plugin.json
# (`bin/project-issues`, extensionless): the POSIX shebang script runs
# on Linux/macOS; PATHEXT picks up the .cmd on Windows. Each wrapper
# dispatches into the matching bin/<os-triple>/ directory. release.yml
# does the same staging during publish — running it here too keeps the
# local checkout's bin/ structure in parity with the published ZIP, so
# `bin/project-issues` behaves the same locally as in the installed
# plugin.
Write-Step "Staging shell wrappers into bin/"
$wrapperSrc   = Join-Path $root "release/wrappers"
$posixWrapper = Join-Path $root "bin/project-issues"
$cmdWrapper   = Join-Path $root "bin/project-issues.cmd"
Copy-Item -Force (Join-Path $wrapperSrc "project-issues")     $posixWrapper
Copy-Item -Force (Join-Path $wrapperSrc "project-issues.cmd") $cmdWrapper
if (-not $IsWindows) {
    chmod +x $posixWrapper
    if ($LASTEXITCODE -ne 0) {
        Fail "chmod +x on $posixWrapper failed."
    }
}

# Clean up the obsolete legacy binary at bin/project-issues.exe if it
# survived from an older build. Without this, PATHEXT on Windows
# resolves `bin/project-issues` to the .exe (which appears before .cmd
# in the default PATHEXT order), silently bypassing the wrapper
# dispatch. If the file is locked (running MCP), warn instead of
# failing -- the user can clean it up after disconnecting the server.
$legacyExe = Join-Path $root "bin/project-issues.exe"
if (Test-Path $legacyExe) {
    try {
        Remove-Item -Force $legacyExe -ErrorAction Stop
        Write-Host "    Removed obsolete bin/project-issues.exe (superseded by .cmd wrapper)"
    } catch {
        Write-Host "    WARNING: could not remove obsolete bin/project-issues.exe (likely locked)." -ForegroundColor Yellow
        Write-Host "    PATHEXT will still resolve to the stale .exe over the .cmd wrapper." -ForegroundColor Yellow
        Write-Host "    Close any running MCP session and delete bin/project-issues.exe manually." -ForegroundColor Yellow
    }
}

# 6. Smoke-test: MCP initialize handshake.
# On Windows the older PowerShell 5.1 Process StreamWriter prepends a
# UTF-8 BOM that MCP rejects; we stage the request in a temp file and
# use Start-Process -RedirectStandardInput, which pipes raw OS bytes.
# On Linux that workaround is unnecessary -- `Get-Content` piped into
# the binary writes pristine UTF-8 stdin.
Write-Step "Smoke-testing the binary (MCP initialize)"
$initMsg = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"build-smoke","version":"1"}}}'

if ($IsWindows) {
    $inFile  = [System.IO.Path]::GetTempFileName()
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllBytes($inFile, [System.Text.Encoding]::UTF8.GetBytes($initMsg + "`n"))
    $proc = Start-Process -FilePath $destOsBin `
        -RedirectStandardInput $inFile `
        -RedirectStandardOutput $outFile `
        -RedirectStandardError $errFile `
        -NoNewWindow -PassThru
    if (-not $proc.WaitForExit(8000)) { $proc.Kill(); Start-Sleep -Milliseconds 200 }
    $stdout = (Get-Content -Raw -ErrorAction SilentlyContinue $outFile)
    $stderrText = (Get-Content -Raw -ErrorAction SilentlyContinue $errFile)
    Remove-Item -ErrorAction SilentlyContinue $inFile, $outFile, $errFile
} else {
    # On Linux, pipe the JSON-RPC request directly through stdin. The
    # binary is a stdio MCP server, so a one-shot pipe is enough --
    # the server reads the line, replies, and exits when stdin closes.
    $stdout = $initMsg | & $destOsBin 2>$null
    $stderrText = ""
}

if ($stdout -match '"result"' -and $stdout -match '"protocolVersion"') {
    Write-Host "    handshake OK" -ForegroundColor Green
} else {
    Write-Host "    stdout: $stdout" -ForegroundColor Yellow
    if ($stderrText) {
        Write-Host "    stderr: $stderrText" -ForegroundColor Yellow
    }
    Fail "Handshake failed -- see output above."
}

# 7. Optional: produce a release zip.
if ($Package) {
    Write-Step "Packaging release zip"
    $version = (Select-String -Path ".claude-plugin/plugin.json" -Pattern '"version"\s*:\s*"([^"]+)"').Matches[0].Groups[1].Value
    $zipName = "project-issues-plugin-$version.zip"
    $zipPath = Join-Path $root "dist/$zipName"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    $stage = Join-Path $root "build/stage/project-issues-plugin"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root "build/stage")
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item -Recurse -Force ".claude-plugin" $stage
    Copy-Item -Recurse -Force "bin" $stage
    if (Test-Path "skills") {
        Copy-Item -Recurse -Force "skills" $stage
    }
    Copy-Item -Force "README.md", "LICENSE" $stage -ErrorAction SilentlyContinue

    # Build the zip via Python's zipfile so we can stamp Unix mode bits
    # into the central directory. Compress-Archive (and the .NET
    # ZipFile API it wraps) emits create_system=0 (FAT) with no
    # external_attr, so unzip on Linux falls back to umask defaults
    # (0644) -- which strips the exec bit off our binaries and breaks
    # installation on Linux/WSL2.
    #
    # The script is staged to a temp file rather than passed via
    # `python -c` because PowerShell's native-command argument parser
    # strips quote characters from the script body, corrupting string
    # literals.
    $pyZipScript = @'
import os, sys, time, zipfile

stage = sys.argv[1]
zip_path = sys.argv[2]

# 0x8000 == stat.S_IFREG; the high 16 bits of external_attr hold the
# Unix mode when create_system == 3 (Unix). unzip honors this on
# Linux/macOS.
EXE_ATTR = (0o755 << 16) | 0x8000
REG_ATTR = (0o644 << 16) | 0x8000

# Any path under bin/ that looks like the plugin binary (with or
# without .exe) gets the executable mode bits. This covers both the
# legacy bin/project-issues.exe and the new
# bin/<os>-x86_64/project-issues[.exe] layout.
def is_binary(rel: str) -> bool:
    return (
        rel == "bin/project-issues.exe"
        or rel.startswith("bin/") and rel.rsplit("/", 1)[-1] in (
            "project-issues", "project-issues.exe",
        )
    )

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
            zi.external_attr = EXE_ATTR if is_binary(rel) else REG_ATTR
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
Write-Host "$($script:OsBinDir | Resolve-Path -Relative)/$($script:BinaryName) is ready."
