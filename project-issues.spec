# PyInstaller spec for the project-issues-plugin MCP server.
#
# Produces a single-file .exe under dist/project-issues.exe that contains the
# Python interpreter, all dependencies (mcp, httpx, pydantic), and the
# package itself.
#
# Build:    py -3 -m PyInstaller project-issues.spec --clean --noconfirm
# Output:   dist\project-issues.exe
# Copy to:  bin\project-issues.exe  (handled by scripts/build.ps1)

# ruff: noqa
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
ROOT = Path(SPECPATH)

# `mcp.cli` pulls optional `typer`/`rich` deps we don't ship for the server.
# Collect mcp manually, filtering out the CLI subpackage so PyInstaller doesn't
# fail trying to import it.
def _not_cli(name: str) -> bool:
    return not name.startswith("mcp.cli")

mcp_hiddenimports = collect_submodules("mcp", filter=_not_cli)
httpx_datas, httpx_binaries, httpx_hiddenimports = collect_all("httpx")
httpcore_datas, httpcore_binaries, httpcore_hiddenimports = collect_all("httpcore")
certifi_datas, certifi_binaries, certifi_hiddenimports = collect_all("certifi")

extra_hidden = [
    "anyio",
    "pydantic",
    "pydantic_core",
    "starlette",
    "h11",
    "idna",
    "sniffio",
]
extra_hidden += collect_submodules("project_issues_plugin")

a = Analysis(
    ["src/project_issues_plugin/__main__.py"],
    pathex=[str(ROOT / "src")],
    binaries=httpx_binaries + httpcore_binaries + certifi_binaries,
    datas=httpx_datas + httpcore_datas + certifi_datas,
    hiddenimports=(
        mcp_hiddenimports
        + httpx_hiddenimports
        + httpcore_hiddenimports
        + certifi_hiddenimports
        + extra_hidden
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "PIL",
        "test",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="project-issues",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # don't compress — slower startup, no real size win on stdio binaries
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,         # MUST be console=True for stdio MCP transport
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
