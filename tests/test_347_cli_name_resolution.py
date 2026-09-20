"""#347: the documented CLI name must actually run from bash on Windows.

`bin/` ships the Linux ELF at the extensionless `project-issues` next to
`project-issues.exe`; bash on Windows never falls back to `.exe`, so the bare
name dies with rc 126. The docs must therefore give an OS-detecting resolution
snippet and name `project-issues.exe` for Windows / Git Bash.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from project_issues_plugin import cli
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


def _run_snippet(snippet: str, tmp_path: Path) -> tuple[str, dict[str, str], str]:
    """Run a documented resolution snippet under real bash with the ELF and the
    .exe side by side on PATH. Returns (expected_name, printed vars, stdout).
    Lines invoking `"$PI" wait-pipeline ...` (usage lines) are not executed."""
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

    runnable = "\n".join(ln for ln in snippet.splitlines() if "wait-pipeline" not in ln)
    script = tmp_path / "resolve.sh"
    script.write_text(
        runnable
        + '\nprintf "PI=%s\\n" "$PI"\n'
        # directory `command -v "$PI"` resolves into, as a native path when the
        # shell offers `pwd -W` (MSYS/Git Bash), else plain `pwd`
        + 'CVP="$(command -v "$PI")"\n'
        + 'printf "CVD=%s\\n" "$(cd "$(dirname "$CVP")" && { pwd -W 2>/dev/null || pwd; })"\n'
        + 'printf "CVB=%s\\n" "$(basename "$CVP")"\n',
        encoding="utf-8",
        newline="\n",
    )
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    proc = subprocess.run(
        [_bash(), str(script)], capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = dict(
        ln.split("=", 1) for ln in proc.stdout.splitlines() if ln.startswith(("PI=", "CVD=", "CVB="))
    )
    return expected, out, str(bindir)


def _norm_dir(p: str) -> str:
    """Normalise Windows / MSYS (`/c/Users/...`) / POSIX path spellings."""
    p = p.strip().replace("\\", "/")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m and sys.platform == "win32":
        p = f"{m.group(1)}:/{m.group(2)}"
    return os.path.normcase(os.path.realpath(p)).replace("\\", "/").rstrip("/")


def _assert_selects_runnable_name(expected: str, out: dict[str, str], bindir: str) -> None:
    assert out["PI"] == expected
    # `command -v "$PI"` (captured from the run) resolves to the intended file in
    # the dir holding both names, not to a same-named binary elsewhere on PATH.
    assert out["CVB"].lower() == expected.lower(), out
    assert _norm_dir(out["CVD"]) == _norm_dir(bindir), (out["CVD"], bindir)


@needs_bash
def test_documented_snippet_resolves_windows_binary(tmp_path: Path) -> None:
    """R1 (driving): with the ELF and the .exe side by side on PATH, the
    documented snippet selects the name that really runs on this OS."""
    _assert_selects_runnable_name(*_run_snippet(_resolution_snippet(), tmp_path))


_BARE_RESOLVE = re.compile(
    r"(command\s+-v|which|type|hash)\s+[\"']?project-issues[\"']?(?![.\w-])"
)


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


def _assert_usage_line_parses(text: str) -> None:
    """Every documented `"$PI" wait-pipeline` usage line carries --project and
    --sha and is accepted by the real CLI parser (as test_339 does)."""
    lines = [ln for ln in text.splitlines() if re.search(r"\"\$PI\"\s+wait-pipeline", ln)]
    assert lines, 'no `"$PI" wait-pipeline ...` usage line'
    parser = cli._build_parser()
    for ln in lines:
        m = re.search(r"\"\$PI\"\s+wait-pipeline[^\n`]*", ln)
        cmd = m.group(0).replace('"$PI"', "project-issues")
        cmd = re.sub(r"<[^>]*>", "x1", cmd).replace("[", "").replace("]", "")
        argv = shlex.split(cmd)[2:]  # drop "project-issues wait-pipeline"
        assert "--project" in argv and "--sha" in argv, ln
        parser.parse_args(argv)  # every shown flag must be accepted


def _run_retry_case(snippet: str, tmp_path: Path, uname_out: str, first: str, mode: str) -> tuple[int, str]:
    """Execute the SKILL.md snippet under real bash. CONTRACT: the snippet picks
    PI by `uname`, runs `"$PI" --help` and, when that exits 126/127, switches PI
    to the other name (`project-issues.exe` <-> `project-issues`) and re-checks;
    it leaves the final name in `$PI` and itself exits 0.
    Fixture: a fake `uname` (printing `uname_out`) makes the snippet's first
    choice `first`; that name is broken per `mode` (ok / rc126 / rc127 /
    absent); the other name is a working stub printing usage.
    Returns (snippet_rc, final PI)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def stub(name: str, body: str) -> None:
        f = bindir / name
        f.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8", newline="\n")
        f.chmod(0o755)

    stub("uname", f"echo {uname_out}")
    other = "project-issues" if first == "project-issues.exe" else "project-issues.exe"
    stub(other, "echo 'usage: project-issues [-h] {wait-pipeline}'")
    if mode == "ok":
        stub(first, "echo 'usage: project-issues [-h] {wait-pipeline}'")
    elif mode in ("rc126", "rc127"):
        stub(first, f"exit {mode[2:]}")
    # mode == "absent": the first-choice name does not exist -> bash rc 127

    runnable = "\n".join(
        ln for ln in snippet.splitlines() if "wait-pipeline" not in ln or "--help" in ln
    )
    script = tmp_path / "retry.sh"
    # bash's startup files may re-order PATH (Git Bash puts /usr/bin first) and a
    # real project-issues may sit elsewhere on PATH: pin PATH inside the script to
    # the fixture dir plus the system dirs, so "absent" is truly absent.
    q = shlex.quote(str(bindir).replace("\\", "/"))
    pin = f'BIN="$(cygpath -u {q} 2>/dev/null || echo {q})"\nPATH="$BIN:/usr/bin:/bin"\n'
    script.write_text(
        pin + runnable + '\nSNIPRC=$?\nprintf "SNIPRC=%s\\nPI=%s\\n" "$SNIPRC" "$PI"\n',
        encoding="utf-8",
        newline="\n",
    )
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    proc = subprocess.run([_bash(), str(script)], capture_output=True, text=True, env=env, timeout=60)
    out = dict(ln.split("=", 1) for ln in proc.stdout.splitlines() if ln.startswith(("SNIPRC=", "PI=")))
    assert "PI" in out, proc.stdout + proc.stderr
    return int(out["SNIPRC"]), out["PI"].strip()


_FIRST_CHOICE = [("MINGW64_NT-10.0", "project-issues.exe"), ("Linux", "project-issues")]


@needs_bash
@pytest.mark.parametrize("uname_out,first", _FIRST_CHOICE)
@pytest.mark.parametrize("mode", ["rc126", "rc127", "absent"])
def test_snippet_switches_to_other_name_when_first_choice_fails(
    tmp_path: Path, uname_out: str, first: str, mode: str
) -> None:
    """R5a (driving): first-choice name fails with 126/127 -> the executed
    snippet ends on the other, working name and exits 0."""
    if mode == "absent" and first == "project-issues" and sys.platform == "win32":
        pytest.skip("MSYS resolves a missing `project-issues` to `project-issues.exe` itself")
    other = "project-issues" if first == "project-issues.exe" else "project-issues.exe"
    rc, pi = _run_retry_case(_resolution_snippet(), tmp_path, uname_out, first, mode)
    assert rc == 0
    assert pi == other


@needs_bash
@pytest.mark.parametrize("uname_out,first", _FIRST_CHOICE)
def test_snippet_keeps_first_choice_when_it_works(tmp_path: Path, uname_out: str, first: str) -> None:
    """R5a: first-choice name works -> PI unchanged, snippet exits 0."""
    rc, pi = _run_retry_case(_resolution_snippet(), tmp_path, uname_out, first, "ok")
    assert rc == 0
    assert pi == first


def test_skill_docs_do_not_resolve_bare_name_and_usage_lines_parse() -> None:
    """R5a: SKILL.md never resolves the bare name (incl. quoted forms) and every
    `"$PI" wait-pipeline` usage line is accepted by the real CLI parser."""
    skill = _pipelines_section()
    assert not _BARE_RESOLVE.search(skill), "SKILL.md resolves the bare name (command -v/which/type project-issues)"
    _assert_usage_line_parses(skill)


@needs_bash
def test_readme_cli_section_gives_windows_resolution(tmp_path: Path) -> None:
    """R5b (driving): README's CLI section carries a fenced uname block that,
    EXECUTED under real bash, selects the name that runs on this OS, and the
    `"$PI" wait-pipeline` usage line is in that block or right after it."""
    readme = _readme_cli_section()
    spans = [
        (m.group(1), m.end())
        for m in re.finditer(r"```[a-z]*\n(.*?)```", readme, re.DOTALL)
        if "uname" in m.group(1)
    ]
    assert spans, "README CLI section has no fenced uname block"
    block, end = spans[0]
    assert _selects_exe_on_windows(block), (
        "README uname block does not select project-issues.exe under MINGW*|MSYS*|CYGWIN*"
    )
    usage = r"\"\$PI\"\s+wait-pipeline"
    after = readme[end:].lstrip("\n").split("\n", 1)[0]
    assert re.search(usage, block) or re.search(usage, after), (
        'README `"$PI" wait-pipeline` usage is neither in the PI= block nor on the line right after it'
    )
    _assert_usage_line_parses(block + "\n" + after)
    _assert_selects_runnable_name(*_run_snippet(block, tmp_path))
    assert not _BARE_RESOLVE.search(readme), "README resolves the bare name (command -v/which/type project-issues)"
