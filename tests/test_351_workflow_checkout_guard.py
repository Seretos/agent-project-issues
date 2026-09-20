"""#351: any workflow job whose step runs a repo file (e.g. `bash .github/scripts/x.sh`)
must check the repo out first.

Regression: #349 moved the release smoke check into .github/scripts/ but the
`smoke` job in release.yml only downloaded artifacts, so the run died with
`No such file or directory` (exit 127) at release time. test.yml's copy of the
job had a checkout, so PR CI stayed green and never showed it.
"""
from __future__ import annotations

import re

import pytest

from tests.test_349_smoke_cli_resolution import WORKFLOWS, _load_workflow

_REPO_FILE = re.compile(r"(^|\s)(\./)?\.github/scripts/", re.MULTILINE)


def _code(run: str) -> str:
    """Shell text without comment-only lines (they may mention the path)."""
    return "\n".join(ln for ln in run.splitlines() if not ln.lstrip().startswith("#"))


def _is_checkout(step: dict) -> bool:
    return str(step.get("uses", "")).startswith("actions/checkout@")


def _job_cases() -> list[tuple[str, str]]:
    cases = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for jid in _load_workflow(wf.name).get("jobs", {}):
            cases.append((wf.name, jid))
    return cases


@pytest.mark.parametrize("wf,jid", _job_cases())
def test_job_running_repo_scripts_checks_out_first(wf: str, jid: str) -> None:
    steps = _load_workflow(wf)["jobs"][jid].get("steps", [])
    checked_out = False
    for step in steps:
        if _is_checkout(step):
            checked_out = True
        elif _REPO_FILE.search(_code(str(step.get("run", "")))):
            assert checked_out, (
                f"{wf}: job '{jid}' step '{step.get('name')}' runs a repo script "
                "before any actions/checkout step"
            )
