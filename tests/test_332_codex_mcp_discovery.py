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
    # The declared string stays the extensionless name; only on-disk resolution tolerates .exe.
    assert command == "./bin/project-issues", f"declared command changed: {command!r}"
    command_path = _resolve_in(staged, command)
    # Pinned to the staged bin/ dir, not merely any existing file in the package.
    assert command_path.is_relative_to((staged / "bin").resolve()), (
        f"command {command!r} resolves to {command_path}, outside the staged bin/ directory"
    )
    # Windows hosts resolve the extensionless name through its .exe sibling.
    assert command_path.is_file() or command_path.with_name(command_path.name + ".exe").is_file()
    assert (staged / "bin" / "project-issues").is_file()
    assert (staged / "bin" / "project-issues.exe").is_file()


@needs_bash
def test_staged_codex_server_forwards_provider_token_env_vars(staged: Path) -> None:
    """#344: Codex hands the bundled server a curated env; tokens must be forwarded by name."""
    manifest = json.loads((staged / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads(_resolve_in(staged, manifest["mcpServers"]).read_text(encoding="utf-8"))
    server = mcp["mcpServers"]["project-issues"]

    forwarded = server["env_vars"]
    assert set(forwarded) >= {"GITHUB_TOKEN", "GITLAB_TOKEN", "AZURE_DEVOPS_TOKEN"}

    # Codex semantics: the child environment is the parent filtered by the declared names.
    parent = {"GITHUB_TOKEN": "x-token", "UNRELATED": "y"}
    child = {k: v for k, v in parent.items() if k in forwarded}
    assert child.get("GITHUB_TOKEN")
    assert "UNRELATED" not in child

    # By reference only: no literal env map, every entry a bare variable name.
    assert "env" not in server
    for name in forwarded:
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name), f"not a bare env var name: {name!r}"


@needs_bash
def test_staged_package_has_no_unread_root_manifests(staged: Path) -> None:
    # Reached only after the Codex chain exists, so a missing chain cannot pass this.
    assert (staged / ".codex-plugin" / "plugin.json").is_file()
    assert not (staged / "plugin.json").exists()
    assert not (staged / "mcp.json").exists()
    # The repository itself must not carry them either (not merely dropped from the copy list).
    assert not (ROOT / "plugin.json").exists(), "root plugin.json still in the repo"
    assert not (ROOT / "mcp.json").exists(), "root mcp.json still in the repo"


@needs_bash
def test_codex_and_claude_manifests_agree_and_declare_skills(staged: Path) -> None:
    codex = json.loads((staged / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((staged / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert codex["name"] == claude["name"]
    assert codex["version"] == claude["version"]
    skills = _resolve_in(staged, codex["skills"])
    assert skills == (staged / "skills").resolve(), f"skills pointer resolves to {skills}, not the staged skills/"
    assert skills.is_dir()


def _stamp_and_upload_steps() -> tuple[dict, dict]:
    from ruamel.yaml import YAML

    doc = YAML(typ="safe").load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        steps = job.get("steps", [])
        stamp = [i for i, s in enumerate(steps) if "for manifest in" in s.get("run", "")]
        if not stamp:
            continue
        assert len(stamp) == 1
        uploads = [
            i for i, s in enumerate(steps) if str(s.get("with", {}).get("name", "")) == "stamped-manifests"
        ]
        assert len(uploads) == 1
        assert stamp[0] < uploads[0], "manifests must be stamped before they are uploaded"
        assert job.get("if") is None
        assert steps[stamp[0]].get("if") is None and steps[uploads[0]].get("if") is None
        return steps[stamp[0]], steps[uploads[0]]
    raise AssertionError("no job contains the manifest stamp loop")


def test_release_stamp_step_covers_exactly_the_shipped_plugin_manifests() -> None:
    expected = {".claude-plugin/plugin.json", ".codex-plugin/plugin.json"}
    # The repo carries exactly these manifests (no stale root one).
    on_disk = {p for p in (*expected, "plugin.json") if (ROOT / p).exists()}
    assert on_disk == expected

    stamp, upload = _stamp_and_upload_steps()
    run = stamp["run"]
    m = re.search(r"for manifest in ([^;\n]+);\s*do\b(.*?)\bdone\b", run, re.DOTALL)
    assert m, "stamp loop is not a for/do/done loop"
    assert set(m.group(1).split()) == expected
    body = m.group(2)
    # The body must really write the stamped version back onto the manifest itself.
    assert re.search(r"\bjq\b[^\n]*\.version\s*=", body) or re.search(r"\bsed\s+-i\b[^\n]*\$manifest", body)
    assert "/dev/null" not in body
    assert re.search(r"\bmv\b[^\n]*\"?\$manifest\"?", body) or "sed -i" in body
    uploaded = set(str(upload["with"]["path"]).split())
    assert expected <= uploaded
    assert "plugin.json" not in uploaded and "mcp.json" not in uploaded


_HAS_STAMP_TOOLS = bool(_bash() and shutil.which("jq"))


@pytest.mark.skipif(not _HAS_STAMP_TOOLS, reason="bash and jq are required to execute the release stamp step")
def test_release_stamp_step_executes_and_stamps_every_shipped_manifest() -> None:
    import subprocess
    import tempfile

    expected = {".claude-plugin/plugin.json", ".codex-plugin/plugin.json"}
    stamp, _ = _stamp_and_upload_steps()
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "pyproject.toml").write_text(
            "[project]" + chr(10) + 'version = "0.0.0"' + chr(10), encoding="utf-8"
        )
        for rel in expected:
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, work / rel)
        script = stamp["run"].replace("${{ inputs.version }}", "9.9.9")
        res = subprocess.run([_bash(), "-c", script], cwd=work, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        for rel in expected:
            assert json.loads((work / rel).read_text(encoding="utf-8"))["version"] == "9.9.9"


def _package_copy_statements() -> tuple[list[tuple[list[str], str]], str]:
    """Parse (sources, destination) of every live Copy-Item in build.ps1's Package block."""
    text = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    block = text[text.index("if ($Package)") :]
    live = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))
    stage_def = re.search(r'\$stage\s*=\s*Join-Path\s+\$root\s+"([^"]+)"', live)
    assert stage_def, "Package block defines no $stage staging directory"
    stmts = []
    for line in live.splitlines():
        m = re.match(r"\s*Copy-Item\b(.*)$", line)
        if not m:
            continue
        toks = re.findall(r'"[^"]*"(?:\s*,\s*"[^"]*")*|\$\w+|-\w+', m.group(1))
        pos, skip = [], False
        for t in toks:
            if skip:
                skip = False
            elif t == "-ErrorAction":
                skip = True
            elif not t.startswith("-"):
                pos.append(t)
        assert len(pos) == 2, f"cannot parse Copy-Item line: {line!r}"
        stmts.append((re.findall(r'"([^"]*)"', pos[0]), pos[1]))
    return stmts, live


@needs_bash
def test_build_package_block_structurally_lists_every_staged_member(staged: Path) -> None:
    """Structural parity guard only: parses build.ps1's Copy-Item block for every staged member.

    It does NOT prove the built ZIP's contents (that needs PyInstaller via
    `build.ps1 -Package`); that is covered by the plan's live substitute execution.
    """
    stmts, live = _package_copy_statements()
    # Every live Copy-Item targets the staging directory that is then zipped.
    assert stmts and all(dest == "$stage" for _, dest in stmts)
    assert re.search(r"\$pyZipScript[^\n]*\$stage|\$stage[^\n]*\$zipPath|\$zipPath[^\n]*\$stage", live)
    copied = {Path(src).parts[0] for srcs, _ in stmts for src in srcs}
    optional = {"README.md", "LICENSE", "description.md", "assets"}
    members = {p.name for p in staged.iterdir()} - optional
    missing = sorted(m for m in members if m not in copied)
    assert not missing, f"build.ps1 -Package does not copy staged members: {missing}"
