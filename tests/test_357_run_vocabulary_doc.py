"""Driving tests for ticket #357: document the `status`/`conclusion`
vocabulary on `list_pipeline_runs` / `get_pipeline_run`.

An agent that sees only the served tool descriptions must be able to
answer, in one step and without source access:
  - R1 (Q1): which `status`/`conclusion` values each provider returns.
  - R2 (Q2): whether they are normalized (partially).
  - R3 (Q3): what "CI is green" means, plus the `wait-pipeline` CLI
    pointer.

Every test reads the text FastMCP actually serves — `FastMCP("t")` +
`pipelines.register(mcp)` + `asyncio.run(mcp.list_tools())` +
`.description` — the same path `server.py` uses in production (no
wrapper, no `description=` override).

R1 additionally drives the pinned lib's real per-provider mappers
(`github._map_run`, `gitlab._map_pipeline_run`,
`azuredevops._map_build_run`) so the documented table cannot drift
from the actual mapping behaviour.

Limitation (per plan): every pass-through branch below checks only the
raw values this file declares, not a live provider API call. That
covers GitHub's status and conclusion, GitLab's non-terminal status,
and Azure DevOps's status and unknown results.
"""
from __future__ import annotations

import asyncio
import itertools
import re

import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers.azuredevops import _map_build_run
from lib_python_projects.providers.github import _map_run
from lib_python_projects.providers.gitlab import _map_pipeline_run
from mcp.server.fastmcp import FastMCP

from project_issues_plugin.tools import pipelines as pipeline_tools

TOOL_NAMES = ("list_pipeline_runs", "get_pipeline_run")


# ---------- served-description helpers ---------------------------------------


def _served_descriptions() -> dict[str, str]:
    mcp = FastMCP("t")
    pipeline_tools.register(mcp)
    tools = asyncio.run(mcp.list_tools())
    return {t.name: (t.description or "") for t in tools}


_BACKTICK_RE = re.compile(r"`([^`]*)`")


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


def _cell_tokens(row: str, column_index: int) -> set[str | None]:
    """column_index: 0 = provider, 1 = status, 2 = conclusion."""
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    cell = cells[column_index]
    tokens = _BACKTICK_RE.findall(cell)
    return {None if t == "null" else t for t in tokens}


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


# ---------- GitLab mapper inputs ----------------------------------------------

GITLAB_STATUSES = [
    "success", "failed", "canceled", "skipped",
    "created", "waiting_for_resource", "preparing", "pending", "running",
    "manual", "scheduled", "",
]


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


# ---------- Azure DevOps mapper inputs -----------------------------------------

AZURE_STATUSES = [
    "notStarted", "inProgress", "cancelling", "postponed", "none", "completed",
]
AZURE_RESULTS: list[str | None] = [
    "succeeded", "failed", "partiallySucceeded", "canceled", "none", None,
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


PROVIDER_CASES = {
    "github": ("GitHub", _github_mapped_sets),
    "gitlab": ("GitLab", _gitlab_mapped_sets),
    "azure": ("Azure DevOps", _azure_mapped_sets),
}


# ---------- R1 (Q1): table matches mapper output, per column, per tool -------


@pytest.mark.parametrize("provider_key", ["github", "gitlab", "azure"])
def test_table_matches_mapper_output(provider_key: str) -> None:
    provider_label, mapped_sets_fn = PROVIDER_CASES[provider_key]
    expected_statuses, expected_conclusions = mapped_sets_fn()
    descriptions = _served_descriptions()

    for tool_name in TOOL_NAMES:
        row = _provider_row(descriptions[tool_name], provider_label)
        documented_statuses = _cell_tokens(row, 1)
        documented_conclusions = _cell_tokens(row, 2)
        assert documented_statuses == expected_statuses, (
            f"{tool_name}: documented status column for {provider_label} "
            f"{documented_statuses} does not match mapper output "
            f"{expected_statuses}"
        )
        assert documented_conclusions == expected_conclusions, (
            f"{tool_name}: documented conclusion column for {provider_label} "
            f"{documented_conclusions} does not match mapper output "
            f"{expected_conclusions}"
        )


# ---------- R2 (Q2): partial-normalization claim ------------------------------


def test_partial_normalization_claim_holds() -> None:
    # Mapper-level facts the partial-normalization claim rests on — these
    # already hold today against the pinned lib, independent of the new
    # docstring text.
    github_run = _map_run(
        {
            "id": 1, "status": "completed", "conclusion": "success",
            "head_branch": "main", "head_sha": "deadbeef", "event": "push",
            "html_url": "https://github.com/acme/backend/actions/runs/1",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T01:00:00Z", "run_attempt": 1,
        }
    )
    assert github_run.status == "completed"
    assert github_run.conclusion == "success"

    gitlab_success = _map_pipeline_run(
        {
            "id": 2, "ref": "main", "sha": "deadbeef", "source": "push",
            "status": "success",
            "web_url": "https://gitlab.com/acme/backend/-/pipelines/2",
            "created_at": "2024-01-01T00:00:00Z",
            "finished_at": "2024-01-01T01:00:00Z",
        }
    )
    assert gitlab_success.status == "completed"
    assert gitlab_success.conclusion == "success"

    gitlab_failed = _map_pipeline_run(
        {
            "id": 3, "ref": "main", "sha": "deadbeef", "source": "push",
            "status": "failed",
            "web_url": "https://gitlab.com/acme/backend/-/pipelines/3",
            "created_at": "2024-01-01T00:00:00Z",
            "finished_at": "2024-01-01T01:00:00Z",
        }
    )
    assert gitlab_failed.status == "completed"
    # GitLab's raw spelling ("failed") stays distinct from GitHub's
    # ("failure") — the "partial" normalization does not paper over this.
    assert gitlab_failed.conclusion == "failed"
    assert gitlab_failed.conclusion != "failure"

    project = _azure_project()
    azure_succeeded = _map_build_run(
        {
            "id": 4, "definition": {"name": "CI"},
            "sourceBranch": "refs/heads/main", "sourceVersion": "deadbeef",
            "reason": "individualCI", "status": "completed",
            "result": "succeeded",
            "queueTime": "2024-01-01T00:00:00Z",
            "finishTime": "2024-01-01T01:00:00Z",
        },
        project,
    )
    assert azure_succeeded.status == "completed"
    assert azure_succeeded.conclusion == "success"

    azure_failed = _map_build_run(
        {
            "id": 5, "definition": {"name": "CI"},
            "sourceBranch": "refs/heads/main", "sourceVersion": "deadbeef",
            "reason": "individualCI", "status": "completed",
            "result": "failed",
            "queueTime": "2024-01-01T00:00:00Z",
            "finishTime": "2024-01-01T01:00:00Z",
        },
        project,
    )
    assert azure_failed.conclusion == "failure"
    assert azure_failed.conclusion != "failed"

    # The text half of the claim — this is the part expected RED today.
    descriptions = _served_descriptions()
    for tool_name in TOOL_NAMES:
        text = descriptions[tool_name]
        assert "partial" in text.lower(), (
            f"{tool_name}: served description has no partial-normalization "
            "statement"
        )
        assert "provider-native" in text, (
            f"{tool_name}: served description does not say the other "
            "spellings stay provider-native"
        )


# ---------- R3 (Q3): green condition + wait-pipeline pointer ------------------


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_green_condition_served(tool_name: str) -> None:
    text = _served_descriptions()[tool_name]
    assert 'status == "completed"' in text, (
        f"{tool_name}: served description lacks the status=='completed' "
        "green predicate"
    )
    assert 'conclusion == "success"' in text, (
        f"{tool_name}: served description lacks the conclusion=='success' "
        "green predicate"
    )
    assert "project-issues wait-pipeline" in text, (
        f"{tool_name}: served description lacks the wait-pipeline pointer"
    )
