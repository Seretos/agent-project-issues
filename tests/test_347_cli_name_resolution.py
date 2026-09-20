"""#347: the documented CLI name must actually run from bash on Windows.

`bin/` ships the Linux ELF at the extensionless `project-issues` next to
`project-issues.exe`; bash on Windows never falls back to `.exe`, so the bare
name dies with rc 126. The docs must therefore give an OS-detecting resolution
snippet and name `project-issues.exe` for Windows / Git Bash.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_315_portable_plugin_layout import _bash, needs_bash
from tests.test_339_wait_pipeline_skill_and_hint import _pipelines_section

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def _fenced_blocks(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"```[a-z]*\n(.*?)```", text, re.DOTALL)]


def _readme_cli_section() -> str:
    text = README.read_text(encoding="utf-8")
    m = re.search(r"^### CLI: wait-pipeline\s*$(.*?)(?=^#{1,3} )", text, re.DOTALL | re.MULTILINE)
    if m is None:  # last section of the file
        m = re.search(r"^### CLI: wait-pipeline\s*$(.*)", text, re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""  # empty -> the content asserts below fail


def _resolution_snippet() -> str:
    blocks = [b for b in _fenced_blocks(_pipelines_section()) if "uname" in b]
    assert blocks, "no OS-detecting snippet (fenced bash block using `uname`) in the Pipelines section"
    return blocks[0]


@needs_bash
def test_documented_snippet_resolves_windows_binary(tmp_path: Path) -> None:
    """R1 (driving): with the ELF and the .exe side by side on PATH, the
    documented snippet selects the name that really runs on this OS."""
    snippet = _resolution_snippet()

    bindir = tmp_path / "bin"
    bindir.mkdir()
    elf = bindir / "project-issues"
    exe = bindir / "project-issues.exe"
    if sys.platform == "win32":
        # The shadowing condition of the bug: a Linux ELF that cannot run here.
        elf.write_bytes(b"\x7fELF" + b"\0" * 60)
        exe.write_bytes(b"MZ" + b"\0" * 60)
        expected = "project-issues.exe"
    else:
        elf.write_text("#!/bin/sh\necho ran-posix-binary\n", encoding="utf-8")
        elf.chmod(0o755)
        exe.write_bytes(b"MZ" + b"\0" * 60)
        expected = "project-issues"

    script = tmp_path / "resolve.sh"
    script.write_text(
        snippet
        + '\nprintf "PI=%s\\n" "$PI"\n'
        + ('"$PI" --help\n' if sys.platform != "win32" else ""),
        encoding="utf-8",
        newline="\n",
    )
    import os

    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    proc = subprocess.run(
        [_bash(), str(script)], capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = dict(
        ln.split("=", 1) for ln in proc.stdout.splitlines() if ln.startswith("PI=")
    )
    assert out["PI"] == expected
    assert (bindir / out["PI"]).is_file()  # the selected name is the file placed next to the shadowing ELF
    if sys.platform != "win32":
        assert "ran-posix-binary" in proc.stdout  # the resolved name actually executed


_BARE_RESOLVE = re.compile(r"\b(command\s+-v|which|type|hash)\s+project-issues(?![.\w-])")


def _blocks(text: str) -> list[str]:
    """Blank-line-separated paragraphs / list items (fenced blocks stay whole)."""
    return [b for b in re.split(r"\n\s*\n", text) if b.strip()]


def _selects_exe_on_windows(block: str) -> bool:
    """A uname-driven selection: MINGW/MSYS/CYGWIN branch assigning PI=...exe."""
    return bool(
        re.search(r"uname", block)
        and re.search(r"MINGW\*?\s*\|\s*MSYS\*?|MINGW\*|MSYS\*|CYGWIN\*", block)
        and re.search(r"PI=[\"']?project-issues\.exe", block)
        and re.search(r"PI=[\"']?project-issues[\"';\s]", block)
    )


def test_skill_snippet_selects_exe_and_documents_126_127_retry() -> None:
    """R5a (driving): the SKILL.md snippet R1 executes selects the .exe on
    Windows shells, and ONE paragraph pairs the `"$PI" --help` sanity check with
    'rc 126/127 = wrong name -> retry the other name'."""
    skill = _pipelines_section()
    assert _selects_exe_on_windows(_resolution_snippet()), (
        "SKILL.md snippet does not assign PI=project-issues.exe under MINGW*|MSYS*|CYGWIN* "
        "(and PI=project-issues otherwise)"
    )
    assert not _BARE_RESOLVE.search(skill), "SKILL.md resolves the bare name (command -v/which/type project-issues)"
    hits = [
        b for b in _blocks(skill)
        if re.search(r"\"\$PI\"\s+(wait-pipeline\s+)?--help", b)
        and re.search(r"\b126\b[^\n]{0,40}\b127\b|\b127\b[^\n]{0,40}\b126\b", b)
        and re.search(r"wrong name", b, re.IGNORECASE)
        and re.search(r"(retry|try)[^.\n]{0,40}other (name|binary)", b, re.IGNORECASE)
    ]
    assert hits, (
        "no single SKILL.md paragraph pairs the `\"$PI\" --help` check with "
        "'126/127 = wrong name, retry the other name'"
    )


def test_readme_cli_section_gives_windows_resolution() -> None:
    """R5b (driving): README's CLI section carries a fenced uname-driven
    selection of project-issues.exe on Windows / Git Bash, used via "$PI",
    and no bare-name resolution."""
    readme = _readme_cli_section()
    fenced = [b for b in _fenced_blocks(readme) if _selects_exe_on_windows(b)]
    assert fenced, "README CLI section has no fenced uname block selecting project-issues.exe on MINGW*/MSYS*/CYGWIN*"
    assert re.search(r"\"\$PI\"\s+wait-pipeline", readme), 'README CLI usage does not invoke "$PI" wait-pipeline'
    assert not _BARE_RESOLVE.search(readme), "README resolves the bare name (command -v/which/type project-issues)"
