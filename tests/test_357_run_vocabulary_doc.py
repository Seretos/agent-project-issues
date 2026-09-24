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

# ---------- golden-string requirements (R2, R3) -------------------------------
#
# Rounds 1-3 tried regex/proximity/negation-window heuristics to pin down R2
# and R3 without pinning exact wording; each round's checks were beaten by a
# more elaborate adversarial docstring that still passed while denying or
# omitting the claim. Strategy for round 4: require one complete, literal
# sentence per requirement, chosen so the sentence's mere presence
# structurally entails the claim — no adversarial docstring can contain this
# exact sentence while meaning the opposite. `pipelines.py`'s
# `_RUN_VOCABULARY_DOC` constant (written in the implement phase) must
# contain each sentence verbatim.

R2_GOLDEN_SENTENCE = (
    'Normalization is partial: terminal status is always "completed" and '
    'green conclusion is always "success", but other conclusion spellings '
    "stay provider-native and are not unified across providers."
)

R3_GOLDEN_SENTENCE = (
    'Every run for the commit must have status == "completed" and '
    'conclusion == "success" to count as CI green for that commit; use the '
    "bundled project-issues wait-pipeline CLI as the ready-made verdict "
    "instead of hand-rolled polling or comparison."
)


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
    assert gitlab_failed.conclusion == "failed"

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
    # "failure" (GitHub/Azure DevOps spelling) and "failed" (GitLab
    # spelling) both represent a failed run but stay distinct raw
    # strings across providers — the actual claim from the plan, not a
    # tautology about a single value never equaling a different literal.
    assert gitlab_failed.conclusion != azure_failed.conclusion

    # The text half of the claim — this is the part expected RED today.
    #
    # Rounds 1-3's regex/proximity/negation-window heuristics were each
    # beaten by a more elaborate adversarial docstring that passed the
    # checks while still denying or omitting the claim. Round 4 requires
    # the served description to contain the golden sentence verbatim: the
    # sentence's own content IS the claim (it names both concrete
    # guarantees, the word "partial", and "provider-native" in one
    # unambiguous statement), so no adversarial rewording can satisfy an
    # exact-substring check while saying the opposite.
    descriptions = _served_descriptions()
    for tool_name in TOOL_NAMES:
        text = descriptions[tool_name]
        assert R2_GOLDEN_SENTENCE in text, (
            f"{tool_name}: served description does not contain the "
            f"required partial-normalization sentence verbatim:\n"
            f"{R2_GOLDEN_SENTENCE!r}"
        )


# ---------- R3 (Q3): green condition + wait-pipeline pointer ------------------


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_green_condition_served(tool_name: str) -> None:
    # Rounds 1-3's regex/proximity/negation-window heuristics were each
    # beaten by a more elaborate adversarial docstring that passed the
    # checks while still denying the green condition or dropping the
    # quantifier. Round 4 requires the served description to contain the
    # golden sentence verbatim: the sentence's own content IS the claim (it
    # joins both predicates with "and", carries the "every run for the
    # commit" quantifier, and names "project-issues wait-pipeline" as the
    # ready-made verdict in one unambiguous statement), so no adversarial
    # rewording can satisfy an exact-substring check while saying the
    # opposite.
    text = _served_descriptions()[tool_name]
    assert R3_GOLDEN_SENTENCE in text, (
        f"{tool_name}: served description does not contain the required "
        f"green-condition sentence verbatim:\n{R3_GOLDEN_SENTENCE!r}"
    )
