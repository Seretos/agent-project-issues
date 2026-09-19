"""Tests for ticket #315: portable Agent Plugins 1.0 layout.

Codex on Windows failed to start the MCP server (os error 3) because the
legacy `.codex-plugin/plugin.json` declared `${PLUGIN_ROOT}/bin/project-issues`.
The package now ships a root `plugin.json` + `mcp.json` (`type: stdio`,
plugin-relative `./bin/project-issues`) next to `.claude-plugin/`, `hooks/`,
`skills/` and `bin/`, staged by `.github/scripts/stage-plugin-payload.sh
<src> <dest>` (extracted from the two duplicated blocks in release.yml).

The tests run the real staging script as a subprocess into a destination whose
path contains spaces, then assert against the staged package layout.

RED today: neither `mcp.json`, root `plugin.json` nor the staging script exist,
so `bash <missing-script>` exits 127 and the root-manifest read raises
FileNotFoundError.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _bash() -> str | None:
    if sys.platform == "win32":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        return str(candidate) if candidate.exists() else None
    return shutil.which("bash")


needs_bash = pytest.mark.skipif(_bash() is None, reason="a real bash is not on PATH")

ROOT = Path(__file__).resolve().parent.parent
STAGE_SCRIPT = ROOT / ".github" / "scripts" / "stage-plugin-payload.sh"

MEMBERS = [
    ".mcp.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "hooks/hooks.json",
    "hooks/security_hint.mjs",
    "skills/project-issues/SKILL.md",
    "bin/project-issues",
    "bin/project-issues.exe",
]


def _build_source(src: Path, skip: tuple[str, ...] = ()) -> None:
    """Fixture source tree: real repo manifests/hooks/skills + placeholder
    binaries (`/bin/` is gitignored, so it is absent from a checkout)."""
    for rel in MEMBERS:
        if rel in skip:
            continue
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.startswith("bin/"):
            target.write_bytes(b"placeholder")
            continue
        real = ROOT / rel
        if real.exists():  # a missing real file simply stays missing (RED)
            shutil.copyfile(real, target)


def _stage(src: Path, dest: Path) -> subprocess.CompletedProcess:
    dest.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [_bash(), STAGE_SCRIPT.as_posix(), src.as_posix(), dest.as_posix()],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    src = tmp_path / "source tree"
    dest = tmp_path / "installed plugin dir"  # spaces are load-bearing
    _build_source(src)
    result = _stage(src, dest)
    assert result.returncode == 0, (
        f"staging failed rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return dest


@needs_bash
def test_staged_package_declares_resolvable_stdio_command(staged: Path) -> None:
    # #332: the server lives in the dot-prefixed .mcp.json Codex reaches via
    # .codex-plugin/plugin.json; no $schema / type keys (unobserved in Codex).
    mcp = json.loads((staged / ".mcp.json").read_text(encoding="utf-8"))
    assert "$schema" not in mcp

    server = mcp["mcpServers"]["project-issues"]
    assert "type" not in server
    assert server["command"] == "./bin/project-issues"
    assert server["args"] == []
    assert "${" not in json.dumps(mcp)

    # The plugin-relative command resolves inside the staged package.
    assert (staged / server["command"]).is_file()
    assert (staged / "bin" / "project-issues.exe").is_file()


@needs_bash
def test_staged_package_contains_every_member(staged: Path) -> None:
    for rel in MEMBERS:
        assert (staged / rel).is_file(), f"missing from staged package: {rel}"


@needs_bash
def test_staging_fails_when_required_member_missing(tmp_path: Path) -> None:
    src = tmp_path / "source tree"
    _build_source(src, skip=(".mcp.json",))
    result = _stage(src, tmp_path / "dest dir")
    assert result.returncode != 0
    assert result.returncode != 127, "script must exist and fail loudly, not be missing"


def _release_steps() -> list[dict]:
    from ruamel.yaml import YAML  # transitive dep via lib-python-projects

    doc = YAML(typ="safe").load(ROOT.joinpath(".github", "workflows", "release.yml").read_text(encoding="utf-8"))
    return [step for job in doc["jobs"].values() for step in job.get("steps", [])]


def test_release_workflow_uses_shared_staging_script_in_both_steps() -> None:
    steps = _release_steps()
    zip_steps = [s for s in steps if "zip" in str(s.get("name", "")).lower()]
    branch_steps = [s for s in steps if "release branch" in str(s.get("name", "")).lower()]
    assert len(zip_steps) == 1, f"expected one ZIP-staging step, found {len(zip_steps)}"
    assert len(branch_steps) == 1, f"expected one release-branch step, found {len(branch_steps)}"

    call = re.compile(r"^\s*bash\s+\.github/scripts/stage-plugin-payload\.sh[ \t]+(\S+)[ \t]+(\S+)[ \t]*$", re.MULTILINE)
    residual = re.compile(r"\bcp\s+-a\b[^\n]*(skills|hooks|\.claude-plugin|bin)\b")
    for step in (zip_steps[0], branch_steps[0]):
        run = step.get("run", "")
        assert call.search(run), f"step {step['name']!r} does not call the shared staging script with <src> <dest>"
        assert not residual.search(run), f"step {step['name']!r} still copies package members inline"


def test_codex_and_claude_manifests_agree() -> None:
    codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert codex["name"] == claude["name"]
    assert codex["version"] == claude["version"]
    assert codex["mcpServers"] == "./.mcp.json"
