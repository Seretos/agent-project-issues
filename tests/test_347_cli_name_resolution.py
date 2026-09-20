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
    assert m, "README has no '### CLI: wait-pipeline' section"
    return m.group(1)


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
        + 'printf "RESOLVED=%s\\n" "$(command -v "$PI")"\n'
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
        ln.split("=", 1) for ln in proc.stdout.splitlines() if ln.startswith(("PI=", "RESOLVED="))
    )
    assert out["PI"] == expected
    assert out["RESOLVED"], "command -v \"$PI\" resolved nothing"
    assert Path(out["RESOLVED"].replace("\\", "/")).name.lower() in (expected, expected.lower())
    if sys.platform != "win32":
        assert "ran-posix-binary" in proc.stdout  # the resolved name actually executed


def test_docs_name_the_windows_binary() -> None:
    """R5 (driving): both docs name project-issues.exe for Git Bash on Windows,
    never tell the caller to resolve the bare name, and SKILL.md carries the
    --help sanity check that reads rc 126/127 as 'wrong name'."""
    skill = _pipelines_section()
    readme = _readme_cli_section()
    for label, doc in (("SKILL.md Pipelines section", skill), ("README CLI section", readme)):
        assert "project-issues.exe" in doc, f"{label} does not name project-issues.exe"
        assert re.search(r"Git Bash|MINGW", doc), f"{label} does not mention Git Bash / MINGW"
        assert not re.search(r"command -v project-issues\b", doc), (
            f"{label} still resolves the bare name with `command -v project-issues`"
        )
    assert "126" in skill and "127" in skill, "SKILL.md does not treat rc 126/127 as wrong name"
    assert re.search(r"wrong name", skill, re.IGNORECASE)
    assert re.search(r"\"\$PI\"\s+(wait-pipeline\s+)?--help|\$PI\"? --help", skill), (
        "SKILL.md lacks the `\"$PI\" --help` sanity check"
    )
