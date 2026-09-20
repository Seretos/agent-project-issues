"""#349: the release smoke check (CLI name resolution) is one shared script,
run by both release.yml and test.yml (every PR).

The script is executed for real under bash with `#!/bin/sh` stubs for the two
binaries and a fake `uname`, so both OS branches run on any host. Original bug:
the inline step ran under `bash -e` and the bare `--help` probe exits 4, so the
script died before the 126/127 retry and the `--sha` assertion were reached.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_315_portable_plugin_layout import _bash, needs_bash

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "smoke-cli-resolution.sh"
WORKFLOWS = ROOT / ".github" / "workflows"

_USAGE_OK = "usage: project-issues wait-pipeline [-h] --project P --sha SHA"

# (fake uname output, name the script picks first)
_FIRST_CHOICE = [("MINGW64_NT-10.0", "project-issues.exe"), ("Linux", "project-issues")]


def _other(name: str) -> str:
    return "project-issues" if name == "project-issues.exe" else "project-issues.exe"


def _stub(bindir: Path, name: str, body: str) -> None:
    f = bindir / name
    f.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8", newline="\n")
    f.chmod(0o755)


def _working_body(wait_help: str = f"echo '{_USAGE_OK}'") -> str:
    """Real-CLI-like: bare `--help` exits 4 (unknown/required subcommand)."""
    return (
        'if [ "$1" = "wait-pipeline" ] && [ "$2" = "--help" ]; then\n'
        f"  {wait_help}\n"
        "  exit 0\n"
        "fi\n"
        "echo 'error: a subcommand is required' >&2\n"
        "exit 4"
    )


def _run(
    tmp_path: Path,
    uname_out: str,
    bodies: dict[str, str | None],
) -> subprocess.CompletedProcess[str]:
    """`bodies` maps binary name -> stub body (None = file not created)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "uname", f"echo {uname_out}")
    for name, body in bodies.items():
        if body is not None:
            _stub(bindir, name, body)
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        [_bash(), SCRIPT.as_posix()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _both(first: str, first_body: str | None, other_body: str | None = None) -> dict[str, str | None]:
    return {first: first_body, _other(first): _working_body() if other_body is None else other_body}


@needs_bash
@pytest.mark.parametrize("uname_out,first", _FIRST_CHOICE)
def test_bare_help_exit_4_does_not_abort_the_script(tmp_path: Path, uname_out: str, first: str) -> None:
    """R1 (driving): a binary whose bare `--help` exits 4 must not abort the
    script under `set -e`; the run completes with rc exactly 0."""
    proc = _run(tmp_path, uname_out, {first: _working_body(), _other(first): _working_body()})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"resolved PI={first}" in proc.stdout


@needs_bash
@pytest.mark.parametrize("uname_out,first", _FIRST_CHOICE)
@pytest.mark.parametrize("mode", ["rc126", "rc127"])
def test_switches_to_other_name_when_first_choice_fails(
    tmp_path: Path, uname_out: str, first: str, mode: str
) -> None:
    """R2 (driving): first-choice name exits 126/127 -> the script switches to
    the other name and the `wait-pipeline --help` assertion runs against it."""
    proc = _run(tmp_path, uname_out, _both(first, f"exit {mode[2:]}"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"resolved PI={_other(first)}" in proc.stdout


@needs_bash
def test_fails_when_both_names_are_unrunnable(tmp_path: Path) -> None:
    """R3 extra: both names 126/127 -> non-zero (never a silent success)."""
    proc = _run(tmp_path, "Linux", {"project-issues": "exit 126", "project-issues.exe": "exit 127"})
    assert proc.returncode not in (0, 127), proc.stdout + proc.stderr


@needs_bash
@pytest.mark.parametrize(
    "wait_help, expect",
    [
        ("echo 'usage: project-issues wait-pipeline [-h] --project P'", "--sha missing from wait-pipeline --help"),
        ("echo boom >&2; exit 2", "wait-pipeline --help exited 2"),
    ],
    ids=["omits-sha", "help-fails"],
)
def test_fails_when_wait_pipeline_help_fails_or_omits_sha(tmp_path: Path, wait_help: str, expect: str) -> None:
    """R3 (driving): a broken CLI contract fails loudly with an ::error:: naming
    the cause (and is not the missing-script rc 127)."""
    if wait_help.endswith("exit 2"):
        body = (
            'if [ "$1" = "wait-pipeline" ] && [ "$2" = "--help" ]; then echo boom >&2; exit 2; fi\n'
            "exit 4"
        )
    else:
        body = _working_body(wait_help)
    proc = _run(tmp_path, "Linux", {"project-issues": body, "project-issues.exe": body})
    out = proc.stdout + proc.stderr
    assert proc.returncode not in (0, 127), out
    assert "::error::" in out and expect in out, out


@needs_bash
@pytest.mark.parametrize("missing", ["project-issues", "project-issues.exe"])
def test_fails_when_a_binary_is_missing(tmp_path: Path, missing: str) -> None:
    """R3 (driving): bin/ lacking either name -> non-zero with an ::error::
    naming the missing file."""
    proc = _run(tmp_path, "Linux", {_other(missing): _working_body(), missing: None})
    out = proc.stdout + proc.stderr
    assert proc.returncode not in (0, 127), out
    assert "::error::" in out and missing in out, out


def _workflow_steps(name: str) -> list[dict]:
    from ruamel.yaml import YAML  # transitive dep via lib-python-projects

    doc = YAML(typ="safe").load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return [step for job in doc["jobs"].values() for step in job.get("steps", [])]


def test_both_workflows_call_the_shared_smoke_script() -> None:
    """R4 (driving): release.yml and test.yml each have exactly one step calling
    the shared script, and neither keeps an inline copy of the resolution body."""
    call = re.compile(r"^\s*bash\s+\.github/scripts/smoke-cli-resolution\.sh\s*$", re.MULTILINE)
    residual = re.compile(r"uname\s+-s|PI=project-issues|wait-pipeline\s+--help")
    for wf in ("release.yml", "test.yml"):
        steps = _workflow_steps(wf)
        callers = [s for s in steps if call.search(str(s.get("run", "")))]
        assert len(callers) == 1, f"{wf}: expected one step calling the smoke script, found {len(callers)}"
        for s in steps:
            assert not residual.search(str(s.get("run", ""))), (
                f"{wf}: step {s.get('name')!r} still holds the inline resolution body"
            )
