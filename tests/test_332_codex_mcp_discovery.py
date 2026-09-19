"""Tests for ticket #332: the staged package carries a Codex-readable MCP chain.

Codex reads `.codex-plugin/plugin.json` and follows its `mcpServers` pointer to
a dot-prefixed `.mcp.json`; root `plugin.json` / `mcp.json` are read by no host.
The tests stage the real repo files through the real staging script (into a
destination containing spaces) and follow pointer -> server -> command on disk.

RED before the change: `.codex-plugin/plugin.json` and `.mcp.json` do not exist,
so nothing is staged for them and reading the pointer raises FileNotFoundError;
`scripts/build.ps1 -Package` also copies fewer members than the staging script.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.test_315_portable_plugin_layout import (
    ROOT,
    _bash,
    _release_steps,
    _stage,
    needs_bash,
)

# Union of old and new layout: copy whichever exists in the repo so the staging
# script itself succeeds both before and after the change.
SOURCE_FILES = [
    "plugin.json",
    "mcp.json",
    ".mcp.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "hooks/hooks.json",
    "hooks/security_hint.mjs",
    "skills/project-issues/SKILL.md",
    "bin/project-issues",
    "bin/project-issues.exe",
]


def _build_source(src: Path) -> None:
    for rel in SOURCE_FILES:
        target = src / rel
        if rel.startswith("bin/"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"placeholder")
            continue
        real = ROOT / rel
        if real.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(real, target)


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


def _resolve_in(staged: Path, ref: str) -> Path:
    path = (staged / ref).resolve()
    assert path.is_relative_to(staged.resolve()), f"{ref!r} escapes the staged package"
    return path


@needs_bash
def test_staged_codex_chain_resolves_end_to_end(staged: Path) -> None:
    manifest_path = staged / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    mcp_path = _resolve_in(staged, manifest["mcpServers"])
    assert mcp_path.is_file(), f"mcpServers pointer {manifest['mcpServers']!r} does not resolve"

    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "${" not in json.dumps(manifest)
    assert "${" not in json.dumps(mcp)

    servers = mcp["mcpServers"]
    assert list(servers) == ["project-issues"]
    command = servers["project-issues"]["command"]
    command_path = _resolve_in(staged, command)
    # Windows hosts resolve the extensionless name through its .exe sibling.
    assert command_path.is_file() or command_path.with_name(command_path.name + ".exe").is_file()
    assert (staged / "bin" / "project-issues").is_file()
    assert (staged / "bin" / "project-issues.exe").is_file()


@needs_bash
def test_staged_package_has_no_unread_root_manifests(staged: Path) -> None:
    # Reached only after the Codex chain exists, so a missing chain cannot pass this.
    assert (staged / ".codex-plugin" / "plugin.json").is_file()
    assert not (staged / "plugin.json").exists()
    assert not (staged / "mcp.json").exists()


@needs_bash
def test_codex_and_claude_manifests_agree_and_declare_skills(staged: Path) -> None:
    codex = json.loads((staged / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((staged / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert codex["name"] == claude["name"]
    assert codex["version"] == claude["version"]
    assert _resolve_in(staged, codex["skills"]).is_dir()


def test_release_stamp_step_covers_exactly_the_shipped_plugin_manifests() -> None:
    expected = {".claude-plugin/plugin.json", ".codex-plugin/plugin.json"}
    # The repo carries exactly these manifests (no stale root one).
    on_disk = {p for p in (*expected, "plugin.json") if (ROOT / p).exists()}
    assert on_disk == expected

    pattern = re.compile(r"(?<![\w./-])((?:\.[\w-]+/)?plugin\.json)\b")
    steps = _release_steps()

    stamp = [s for s in steps if "for manifest in" in s.get("run", "")]
    assert len(stamp) == 1
    loop = re.search(r"for manifest in ([^;\n]+)", stamp[0]["run"]).group(1)
    assert set(pattern.findall(loop)) == expected

    artifact = [s for s in steps if str(s.get("with", {}).get("name", "")) == "stamped-manifests"]
    assert len(artifact) == 1
    listed = str(artifact[0]["with"]["path"]).split()
    assert set(pattern.findall(" ".join(listed))) == expected


@needs_bash
def test_build_package_block_copies_every_staged_member(staged: Path) -> None:
    text = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    block = text[text.index("Packaging release zip") :]
    copied = set()
    for line in block.splitlines():
        if "Copy-Item" in line:
            copied.update(re.findall(r'"([^"]+)"', line.split("Copy-Item", 1)[1]))
    optional = {"README.md", "LICENSE", "description.md", "assets"}
    members = {p.name for p in staged.iterdir()} - optional
    missing = sorted(m for m in members if m not in copied)
    assert not missing, f"build.ps1 -Package does not copy staged members: {missing}"
