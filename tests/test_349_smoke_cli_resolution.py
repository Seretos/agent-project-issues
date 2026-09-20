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
        ("echo 'usage: project-issues wait-pipeline [-h] --project P'", r"::error::[^\n]*--sha"),
        ("echo boom >&2; exit 2", r"::error::[^\n]*wait-pipeline[^\n]*\b2\b"),
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
    assert re.search(expect, out), out


@needs_bash
@pytest.mark.parametrize("missing", ["project-issues", "project-issues.exe"])
def test_fails_when_a_binary_is_missing(tmp_path: Path, missing: str) -> None:
    """R3 (driving): bin/ lacking either name -> non-zero with an ::error::
    naming the missing file."""
    proc = _run(tmp_path, "Linux", {_other(missing): _working_body(), missing: None})
    out = proc.stdout + proc.stderr
    assert proc.returncode not in (0, 127), out
    # exact filename tokens: `project-issues` must not match inside `project-issues.exe`
    token = r"(?<![\w.-])" + re.escape(missing) + r"(?![\w.-])"
    errors = [ln for ln in out.splitlines() if "::error::" in ln]
    assert any(re.search(token, ln) for ln in errors), out
    # and no error line may blame the file that IS present
    present = _other(missing)
    present_token = r"(?<![\w.-])" + re.escape(present) + r"(?![\w.-])"
    assert not any(re.search(present_token, ln) for ln in errors), out


def _load_workflow(name: str) -> dict:
    from ruamel.yaml import YAML  # transitive dep via lib-python-projects

    return YAML(typ="safe").load((WORKFLOWS / name).read_text(encoding="utf-8"))


_CALL = re.compile(r"^\s*bash\s+\.github/scripts/smoke-cli-resolution\.sh\s*$", re.MULTILINE)
_RESIDUAL = re.compile(r"uname\s+-s|PI=project-issues|wait-pipeline\s+--help")


def _callers(doc: dict) -> list[tuple[str, dict, dict]]:
    return [
        (jid, job, step)
        for jid, job in doc["jobs"].items()
        for step in job.get("steps", [])
        if _CALL.search(str(step.get("run", "")))
    ]


def _needs(job: dict) -> list[str]:
    n = job.get("needs", [])
    return [n] if isinstance(n, str) else list(n)


@pytest.mark.parametrize("wf", ["release.yml", "test.yml"])
def test_both_workflows_call_the_shared_smoke_script(wf: str) -> None:
    """R4 (driving): each workflow has exactly one enabled step calling the
    shared script, in a job that runs after a build producing both bin
    artifacts, downloads them, and cannot be disabled; no inline copy remains."""
    doc = _load_workflow(wf)
    callers = _callers(doc)
    assert len(callers) == 1, f"{wf}: expected one step calling the smoke script, found {len(callers)}"
    jid, job, step = callers[0]

    # the script the step calls exists
    assert SCRIPT.is_file(), f"{SCRIPT} missing"

    # nothing may disable the guard
    assert "if" not in job, f"{wf}: job {jid} has a job-level `if`"
    assert "if" not in step, f"{wf}: smoke step has a step-level `if`"
    assert job.get("continue-on-error") in (None, False), f"{wf}: job {jid} continue-on-error"
    assert step.get("continue-on-error") in (None, False), f"{wf}: step continue-on-error"

    # runs against built artifacts: needs a build job, downloads both bin artifacts
    needs = _needs(job)
    assert needs, f"{wf}: job {jid} has no `needs` (no built binary)"
    for n in needs:
        assert n in doc["jobs"], f"{wf}: needs unknown job {n}"
    build_text = " ".join(str(doc["jobs"][n]) for n in needs)
    assert "upload-artifact" in build_text or any(
        "upload-artifact" in str(doc["jobs"][n]) for n in doc["jobs"]
    ), f"{wf}: no artifact producer"
    downloads = [
        str(st.get("with", {}).get("name", ""))
        for st in job.get("steps", [])
        if str(st.get("uses", "")).startswith("actions/download-artifact")
    ]
    assert "bin-windows" in downloads and "bin-linux" in downloads, (
        f"{wf}: smoke job must download bin-windows and bin-linux, got {downloads}"
    )
    # both OSes are exercised
    smoke_text = str(job)
    assert "windows" in smoke_text and ("ubuntu" in smoke_text or "linux" in smoke_text), (
        f"{wf}: smoke job does not cover both OSes"
    )

    # no inline copy of the resolution body anywhere
    for j in doc["jobs"].values():
        for st in j.get("steps", []):
            assert not _RESIDUAL.search(str(st.get("run", ""))), (
                f"{wf}: step {st.get('name')!r} still holds the inline resolution body"
            )


def test_release_needs_smoke_and_test_runs_on_pull_request() -> None:
    """R4: release publishing waits on the smoke job; test.yml (every PR)
    triggers on pull_request and builds/smokes on windows and ubuntu-22.04."""
    rel = _load_workflow("release.yml")
    rjid = _callers(rel)[0][0]
    assert rjid in _needs(rel["jobs"]["release"]), "release must need the smoke job"

    doc = _load_workflow("test.yml")
    triggers = doc.get("on", doc.get(True))
    assert "pull_request" in triggers, f"test.yml triggers: {triggers}"
    jid, job, _ = _callers(doc)[0]
    for n in _needs(job):
        m = str(doc["jobs"][n].get("strategy", {}))
        assert "windows-latest" in m and "ubuntu-22.04" in m, f"build job {n} matrix: {m}"
    sm = str(job.get("strategy", {})) + str(job.get("runs-on", ""))
    assert "windows-latest" in sm and "ubuntu-22.04" in sm, f"smoke job {jid} matrix: {sm}"
