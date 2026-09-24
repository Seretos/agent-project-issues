"""Driving tests for ticket #357 (generation 2 replan): document the
`status`/`conclusion` vocabulary on `list_pipeline_runs` / `get_pipeline_run`.

An agent that sees only the served tool descriptions must be able to
answer, in one step and without source access:
  - R1 (Q1): which `status`/`conclusion` values each provider returns,
    and which conclusion values count as green / red / no-verdict /
    pending — i.e. which `project-issues wait-pipeline` exit code
    (`cli._classify`) each one produces. `driving-test` evidence; the
    tests below are it.
  - R2 (Q2): whether the values are normalized (partially). Evidence
    kind `none` per the plan (documentation-completeness, reviewer
    judged) — no test here.
  - R3 (Q3): what "CI is green" means, plus the `wait-pipeline` CLI
    pointer. Evidence kind `none` per the plan — no test here; this
    module docstring's three-question framing is itself the artifact
    R3 asks to hold in one block.

Every test reads the text FastMCP actually serves — `FastMCP("t")` +
`pipelines.register(mcp)` + `asyncio.run(mcp.list_tools())` +
`.description` — the same path `server.py` uses in production (no
wrapper, no `description=` override).

R1 drives the pinned lib's real per-provider mappers (`github._map_run`,
`gitlab._map_pipeline_run`, `azuredevops._map_build_run`) AND the real
`cli._classify` (the `wait-pipeline` exit-code classifier) so the
documented table cannot drift from either: the table's `status` column
must equal the mapper's status output, and the table's four verdict
columns (`green (exit 0)` / `red (exit 1)` / `no verdict (exit 5)` /
`pending (exit 2)`) must equal the exact partition `cli._classify`
assigns to the mapper's conclusion output — derived from the real
function, never hand-transcribed, so a lib bump or a `_classify` change
fails this test naming the drifted value and column.

Limitation (per plan): every pass-through branch below checks only the
raw values this file declares, not a live provider API call. That
covers GitHub's status and conclusion, GitLab's non-terminal status,
and Azure DevOps's status and unknown results.
"""
from __future__ import annotations

import asyncio
import itertools
import re
from types import SimpleNamespace

import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers.azuredevops import _map_build_run
from lib_python_projects.providers.github import _map_run
from lib_python_projects.providers.gitlab import _map_pipeline_run
from mcp.server.fastmcp import FastMCP

from project_issues_plugin import cli
from project_issues_plugin.tools import pipelines as pipeline_tools

TOOL_NAMES = ("list_pipeline_runs", "get_pipeline_run")


# ---------- served-description helpers ---------------------------------------


def _served_descriptions() -> dict[str, str]:
    mcp = FastMCP("t")
    pipeline_tools.register(mcp)
    tools = asyncio.run(mcp.list_tools())
    return {t.name: (t.description or "") for t in tools}


_BACKTICK_RE = re.compile(r"`([^`]*)`")
_EXIT_CODE_RE = re.compile(r"exit\s+(\d+)")


def _header_row(description: str) -> str:
    """Return the `| Provider | status | ... |` header row text.

    Raises with the expected RED reason ("table header missing") when
    the run-vocabulary table is absent from the served description —
    which is the case today, before `_RUN_VOCABULARY_DOC` exists.
    """
    for line in description.splitlines():
        stripped = line.strip()
        if stripped.startswith("| Provider |"):
            return stripped
    raise AssertionError(
        "table header missing: no '| Provider |' row found in the served "
        f"description:\n{description}"
    )


def _provider_row(description: str, provider_label: str) -> str:
    """Return the raw `| <provider_label> | ... |` row text.

    Raises with the expected RED reason ("provider row missing") when
    the row is absent from the served description.
    """
    for line in description.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"| {provider_label} |"):
            return stripped
    raise AssertionError(
        f"provider row missing: no '| {provider_label} |' row found in "
        f"the served description:\n{description}"
    )


def _row_cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _cell_tokens(row: str, column_index: int) -> set[str | None]:
    """column_index: 0 = provider, others depend on the table shape."""
    cell = _row_cells(row)[column_index]
    tokens = _BACKTICK_RE.findall(cell)
    return {None if t == "null" else t for t in tokens}


def _column_index(header_row: str, label: str) -> int:
    """Locate a column by its header label (case-insensitive prefix match,
    so `"green"` matches the header cell `"green (exit 0)"`).

    Raises with a RED reason naming the missing column/label when the
    table doesn't declare it.
    """
    cells = [c.lower() for c in _row_cells(header_row)]
    for idx, cell in enumerate(cells):
        if cell.startswith(label.lower()):
            return idx
    raise AssertionError(
        f"no {label!r} column found in header row: {header_row!r}"
    )


def _exit_code_for_column(header_row: str, column_index: int) -> int:
    """Read the `exit N` annotation the header itself declares for a
    verdict column, e.g. `"green (exit 0)"` -> 0. This ties the test to
    whatever exit code the docstring CLAIMS for that column, rather than
    a value hard-coded in this test file — so a mismatch between the
    claimed exit code and `cli._classify`'s real one is exactly what
    `test_table_matches_mappers_and_verdict` below is checking for.
    """
    cell = _row_cells(header_row)[column_index]
    match = _EXIT_CODE_RE.search(cell)
    if not match:
        raise AssertionError(
            f"header column {column_index} ({cell!r}) does not declare "
            "an 'exit N' code"
        )
    return int(match.group(1))


def _classify_one(conclusion: str | None) -> int:
    """Exit code `cli._classify` (the `wait-pipeline` verdict function)
    assigns to a single run carrying this conclusion. `_classify` only
    reads `run.conclusion` (see `cli.py` around lines 88-112) — no other
    attribute is required on the run stand-in.
    """
    code, _state = cli._classify([SimpleNamespace(conclusion=conclusion)])
    return code


# ---------- GitHub mapper inputs ----------------------------------------------

GITHUB_STATUSES = [
    "requested", "queued", "pending", "waiting", "in_progress", "completed",
]
GITHUB_CONCLUSIONS = [
    "success", "failure", "cancelled", "skipped", "timed_out", "neutral",
    "action_required", "stale", "startup_failure", None,
]


def _github_mapped_sets() -> tuple[set[str], set[str | None]]:
    statuses: set[str] = set()
    conclusions: set[str | None] = set()
    for i, (status, conclusion) in enumerate(
        itertools.product(GITHUB_STATUSES, GITHUB_CONCLUSIONS)
    ):
        run = _map_run(
            {
                "id": i,
                "name": "CI",
                "head_branch": "main",
                "head_sha": "deadbeef",
                "event": "push",
                "status": status,
                "conclusion": conclusion,
                "html_url": "https://github.com/acme/backend/actions/runs/1",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T01:00:00Z",
                "run_attempt": 1,
            }
        )
        statuses.add(run.status)
        conclusions.add(run.conclusion)
    return statuses, conclusions


def _github_terminal_runs() -> list:
    """Every run at GitHub's terminal raw status (`"completed"`), across
    every conclusion GitHub can report at that status (excludes `None`,
    which GitHub only reports for a run that has NOT finished)."""
    return [
        _map_run(
            {
                "id": i,
                "name": "CI",
                "head_branch": "main",
                "head_sha": "deadbeef",
                "event": "push",
                "status": "completed",
                "conclusion": conclusion,
                "html_url": "https://github.com/acme/backend/actions/runs/1",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T01:00:00Z",
                "run_attempt": 1,
            }
        )
        for i, conclusion in enumerate(GITHUB_CONCLUSIONS)
        if conclusion is not None
    ]


# ---------- GitLab mapper inputs ----------------------------------------------

GITLAB_STATUSES = [
    "success", "failed", "canceled", "skipped",
    "created", "waiting_for_resource", "preparing", "pending", "running",
    "manual", "scheduled", "",
]
GITLAB_TERMINAL_STATUSES = ["success", "failed", "canceled", "skipped"]


def _gitlab_mapped_sets() -> tuple[set[str], set[str | None]]:
    statuses: set[str] = set()
    conclusions: set[str | None] = set()
    for i, status in enumerate(GITLAB_STATUSES):
        run = _map_pipeline_run(
            {
                "id": i,
                "ref": "main",
                "sha": "deadbeef",
                "source": "push",
                "status": status,
                "web_url": "https://gitlab.com/acme/backend/-/pipelines/1",
                "created_at": "2024-01-01T00:00:00Z",
                "finished_at": "2024-01-01T01:00:00Z",
            }
        )
        statuses.add(run.status)
        conclusions.add(run.conclusion)
    return statuses, conclusions


def _gitlab_terminal_runs() -> list:
    """Every run at one of GitLab's terminal raw statuses — the ones the
    lib folds into `status="completed"` (see `_TERMINAL_PIPELINE_STATUSES`
    in `lib_python_projects.providers.gitlab`)."""
    return [
        _map_pipeline_run(
            {
                "id": i,
                "ref": "main",
                "sha": "deadbeef",
                "source": "push",
                "status": status,
                "web_url": "https://gitlab.com/acme/backend/-/pipelines/1",
                "created_at": "2024-01-01T00:00:00Z",
                "finished_at": "2024-01-01T01:00:00Z",
            }
        )
        for i, status in enumerate(GITLAB_TERMINAL_STATUSES)
    ]


# ---------- Azure DevOps mapper inputs -----------------------------------------

AZURE_STATUSES = [
    "notStarted", "inProgress", "cancelling", "postponed", "none", "completed",
]
AZURE_RESULTS: list[str | None] = [
    "succeeded", "failed", "partiallySucceeded", "canceled", "none", None,
]
AZURE_TERMINAL_RESULTS: list[str] = [
    "succeeded", "failed", "partiallySucceeded", "canceled", "none",
]


def _azure_project() -> ProjectConfig:
    return ProjectConfig(id="acme", provider="azuredevops", path="org/project/repo")


def _azure_mapped_sets() -> tuple[set[str], set[str | None]]:
    project = _azure_project()
    statuses: set[str] = set()
    conclusions: set[str | None] = set()
    i = 0
    for status in AZURE_STATUSES:
        for result in AZURE_RESULTS:
            i += 1
            raw: dict = {
                "id": i,
                "definition": {"name": "CI"},
                "sourceBranch": "refs/heads/main",
                "sourceVersion": "deadbeef",
                "reason": "individualCI",
                "status": status,
                "queueTime": "2024-01-01T00:00:00Z",
                "finishTime": "2024-01-01T01:00:00Z",
            }
            if result is not None:
                raw["result"] = result
            run = _map_build_run(raw, project)
            statuses.add(run.status)
            conclusions.add(run.conclusion)
    return statuses, conclusions


def _azure_terminal_runs() -> list:
    """Every run at Azure DevOps's terminal raw status (`"completed"`),
    across every `result` value the lib recognises (a `result` of
    `None`, i.e. the key absent, is deliberately excluded: that raw shape
    means "completed but no result yet", not a finished/graded run)."""
    project = _azure_project()
    return [
        _map_build_run(
            {
                "id": i,
                "definition": {"name": "CI"},
                "sourceBranch": "refs/heads/main",
                "sourceVersion": "deadbeef",
                "reason": "individualCI",
                "status": "completed",
                "result": result,
                "queueTime": "2024-01-01T00:00:00Z",
                "finishTime": "2024-01-01T01:00:00Z",
            },
            project,
        )
        for i, result in enumerate(AZURE_TERMINAL_RESULTS)
    ]


PROVIDER_CASES = {
    "github": ("GitHub", _github_mapped_sets),
    "gitlab": ("GitLab", _gitlab_mapped_sets),
    "azure": ("Azure DevOps", _azure_mapped_sets),
}

TERMINAL_RUN_CASES = {
    "github": ("GitHub", _github_terminal_runs),
    "gitlab": ("GitLab", _gitlab_terminal_runs),
    "azure": ("Azure DevOps", _azure_terminal_runs),
}

VERDICT_LABELS = ("green", "red", "no verdict", "pending")


# ---------- R1 (Q1): table matches mapper output + cli._classify partition ---


@pytest.mark.parametrize("provider_key", ["github", "gitlab", "azure"])
def test_table_matches_mappers_and_verdict(provider_key: str) -> None:
    """The driving test for R1: the served table's `status` column must
    equal the mapper's real status output, and its four verdict columns
    (green/red/no-verdict/pending) must equal the exact partition
    `cli._classify` assigns to the mapper's real conclusion output —
    computed from `cli._classify` itself, never hand-transcribed here.
    """
    provider_label, mapped_sets_fn = PROVIDER_CASES[provider_key]
    expected_statuses, expected_conclusions = mapped_sets_fn()

    expected_by_exit: dict[int, set[str | None]] = {}
    for conclusion in expected_conclusions:
        code = _classify_one(conclusion)
        expected_by_exit.setdefault(code, set()).add(conclusion)

    descriptions = _served_descriptions()
    for tool_name in TOOL_NAMES:
        description = descriptions[tool_name]
        header = _header_row(description)
        row = _provider_row(description, provider_label)

        status_idx = _column_index(header, "status")
        documented_statuses = _cell_tokens(row, status_idx)
        assert documented_statuses == expected_statuses, (
            f"{tool_name}: documented status column for {provider_label} "
            f"{documented_statuses} does not match mapper output "
            f"{expected_statuses}"
        )

        documented_by_exit: dict[int, set[str | None]] = {}
        for label in VERDICT_LABELS:
            col_idx = _column_index(header, label)
            exit_code = _exit_code_for_column(header, col_idx)
            tokens = _cell_tokens(row, col_idx)
            if tokens:
                documented_by_exit.setdefault(exit_code, set()).update(tokens)

        assert documented_by_exit == expected_by_exit, (
            f"{tool_name}: documented verdict-column partition for "
            f"{provider_label} {documented_by_exit} does not match the "
            f"partition cli._classify actually assigns to the mapped "
            f"conclusions {expected_by_exit}"
        )


# ---------- R1 additional coverage: terminal/green raw states ----------------


@pytest.mark.parametrize("provider_key", ["github", "gitlab", "azure"])
def test_terminal_state_and_green_conclusion_map_to_completed_status(
    provider_key: str,
) -> None:
    """Every finished (terminal) raw state, across all three providers,
    maps to `status == "completed"` — and since a green run (conclusion
    `"success"`) only ever occurs at a terminal raw state, this also
    covers "every green-conclusion run maps to status=='completed'".
    This grounds the two-conjunct green condition R3's docstring text
    states (status AND conclusion) against real mapper behaviour.

    This test is independent of the served docstring/table and already
    passes today against the pinned lib — it is additional coverage for
    R1, not the driving test (see `test_table_matches_mappers_and_verdict`
    for the part that is expected RED before `pipelines.py` is changed).
    """
    provider_label, terminal_runs_fn = TERMINAL_RUN_CASES[provider_key]
    runs = terminal_runs_fn()
    assert runs, f"{provider_label}: no terminal raw states constructed"

    for run in runs:
        assert run.status == "completed", (
            f"{provider_label}: terminal raw state (conclusion "
            f"{run.conclusion!r}) produced status {run.status!r}, "
            "expected 'completed'"
        )

    green_runs = [r for r in runs if r.conclusion == "success"]
    assert green_runs, (
        f"{provider_label}: no green (conclusion=='success') run found "
        "among the terminal-state runs"
    )
    for run in green_runs:
        assert run.status == "completed"


# ---------- R1 additional coverage: cli._classify aggregate + unknown --------


def test_classify_commit_aggregate_requires_every_run_green() -> None:
    """`cli._classify([green, x])` returns exit 0 (success) if and only
    if `x` is also green — a commit is CI-green only when EVERY run for
    it is green. Also covers the zero-run edge case explicitly per the
    plan-critic note: `_classify([])` does NOT return the plan's guessed
    no-verdict/exit-5 — reading `cli.py` shows a dedicated `EXIT_NO_RUNS`
    (3) branch for an empty run list (state `"no_runs"`), checked here
    against the real function rather than assumed.
    """
    green = SimpleNamespace(conclusion="success")
    red = SimpleNamespace(conclusion="failure")
    pending = SimpleNamespace(conclusion=None)
    unknown = SimpleNamespace(conclusion="totally_unrecognised_value")

    code, state = cli._classify([green, green])
    assert (code, state) == (cli.EXIT_SUCCESS, "success"), (
        f"_classify([green, green]) returned ({code}, {state!r}), "
        "expected (EXIT_SUCCESS, 'success')"
    )

    for other in (red, pending, unknown):
        code, state = cli._classify([green, other])
        assert code != cli.EXIT_SUCCESS, (
            f"_classify([green, {other.conclusion!r}]) returned exit "
            f"{code} ({state!r}) — a non-green run must not let the "
            "commit read as green"
        )

    # Zero-run edge case (plan-critic finding): verified against the real
    # function, not assumed. It is EXIT_NO_RUNS (3), "no_runs" — NOT
    # EXIT_NO_VERDICT (5) as the plan's paraphrase speculated.
    code, state = cli._classify([])
    assert (code, state) == (cli.EXIT_NO_RUNS, "no_runs"), (
        f"_classify([]) returned ({code}, {state!r}), expected "
        "(EXIT_NO_RUNS, 'no_runs') for the zero-run case"
    )


def test_classify_unknown_conclusion_is_no_verdict() -> None:
    """An unrecognised/unknown conclusion value maps to "no verdict"
    (exit 5) via `cli._classify` — never silently treated as green or
    red."""
    unknown = SimpleNamespace(conclusion="totally_unrecognised_value")
    code, state = cli._classify([unknown])
    assert (code, state) == (cli.EXIT_NO_VERDICT, "no_verdict"), (
        f"_classify([unrecognised]) returned ({code}, {state!r}), "
        "expected (EXIT_NO_VERDICT, 'no_verdict')"
    )
