"""Driving tests for ticket #358: `get_pr`'s served description documents
*when* `mergeable_state` / `detailed_merge_status` are `null`, but not
*what their values mean*. This adds one provider-neutral mapping table
(meaning <-> GitHub `mergeable_state` <-> GitLab `detailed_merge_status`
<-> Azure DevOps `merge_pr` error text) so a consumer can classify a merge
failure from the served description alone, without keeping its own table.

Every test reads the text FastMCP actually serves — `FastMCP("t")` +
`pulls.register(mcp)` + `asyncio.run(mcp.list_tools())` + `.description` —
the same path `server.py` uses in production, mirroring
`tests/test_357_run_vocabulary_doc.py`'s style.

- R1: the table exists in `get_pr`'s description and covers all nine
  provider-neutral meaning rows.
- R2: GitHub values sit in the expected row(s) (never a "Gate offen"
  value inside the `mergeable` row), and the GitLab column is an exact
  partition of the enum sentence parsed from the same docstring, grounded
  against the real `gitlab._map_mergeable`.
- R3: Azure DevOps has no `mergeable_state` — its classifier is the real
  `merge_pr` error text. Driven against the real pinned-lib
  `AzureDevOpsProvider.merge_pr` (HTTP mocked via `httpx.MockTransport`),
  never hand-transcribed.

All of R1-R3 are expected to fail RED for the same reason before
`pulls.py` gains the table: "table header missing: no '| Meaning |' row".
"""
from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import azuredevops
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsError,
    AzureDevOpsProvider,
)
from lib_python_projects.providers.gitlab import _map_mergeable
from mcp.server.fastmcp import FastMCP

from project_issues_plugin.tools import pulls as pulls_tools

TOOL_NAMES = ("get_pr", "merge_pr")


# ---------- served-description helpers ---------------------------------------


def _served_descriptions() -> dict[str, str]:
    mcp = FastMCP("t")
    pulls_tools.register(mcp)
    tools = asyncio.run(mcp.list_tools())
    return {t.name: (t.description or "") for t in tools}


_BACKTICK_RE = re.compile(r"`([^`]*)`")


def _header_row(description: str) -> str:
    """Return the `| Meaning | GitHub | GitLab | Azure DevOps |` header row.

    Raises with the expected RED reason ("table header missing") when the
    merge-state table is absent from the served description — which is
    the case today, before the table exists.
    """
    for line in description.splitlines():
        stripped = line.strip()
        if stripped.startswith("| Meaning |"):
            return stripped
    raise AssertionError(
        "table header missing: no '| Meaning |' row found in the served "
        f"description:\n{description}"
    )


def _row_cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _cell_tokens(row: str, column_index: int) -> set[str]:
    cell = _row_cells(row)[column_index]
    return set(_BACKTICK_RE.findall(cell))


def _column_index(header_row: str, label: str) -> int:
    """Locate a column by its header label (case-insensitive prefix
    match), raising with a RED reason naming the missing label."""
    cells = [c.lower() for c in _row_cells(header_row)]
    for idx, cell in enumerate(cells):
        if cell.startswith(label.lower()):
            return idx
    raise AssertionError(f"no {label!r} column found in header row: {header_row!r}")


def _meaning_rows(description: str) -> dict[str, str]:
    """Map each backticked `Meaning`-column label to its raw row text.

    Only matches data rows (first cell backticked) — excludes the header
    row and the `| --- | --- |...` separator row.
    """
    rows: dict[str, str] = {}
    for line in description.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| `"):
            continue
        first_cell = _row_cells(stripped)[0]
        tokens = _BACKTICK_RE.findall(first_cell)
        if tokens:
            rows[tokens[0]] = stripped
    return rows


def _gitlab_enum_tokens(description: str) -> set[str]:
    """Extract the `detailed_merge_status` enum values GitLab documents
    in the paragraph starting "GitLab's enum (may not be exhaustive):" —
    independent of the table, so it reflects the plan's requirement that
    `not_approved` is added to this sentence regardless of table state.
    """
    marker = "enum (may not be exhaustive):"
    idx = description.find(marker)
    if idx == -1:
        raise AssertionError(
            "GitLab detailed_merge_status enum sentence not found in "
            f"description:\n{description}"
        )
    tail = description[idx + len(marker):]
    end = tail.find(".\n\n")
    if end == -1:
        end = tail.find(".\n")
    segment = tail[:end] if end != -1 else tail
    tokens = set(_BACKTICK_RE.findall(segment))
    assert tokens, f"no backticked enum tokens found after {marker!r}"
    return tokens


EXPECTED_MEANINGS = (
    "mergeable",
    "not computed yet",
    "conflict",
    "behind",
    "gate open: CI running/failing",
    "gate open: review missing",
    "gate open: draft",
    "gate open: other",
    "not open",
)


# ---------- R1: table exists and covers the neutral meanings -----------------


def test_get_pr_has_merge_state_table() -> None:
    """Driving test for R1: `get_pr`'s description carries the
    provider-neutral merge-state table with all nine labelled rows."""
    description = _served_descriptions()["get_pr"]
    _header_row(description)
    rows = _meaning_rows(description)
    missing = [m for m in EXPECTED_MEANINGS if m not in rows]
    assert not missing, (
        f"merge-state table is missing meaning row(s) {missing} in "
        f"get_pr's description:\n{description}"
    )


# ---------- R2: GitHub row placement + GitLab column grounding ---------------


GITHUB_EXPECTED_ROWS: dict[str, set[str]] = {
    "clean": {"mergeable"},
    "unknown": {"not computed yet"},
    "dirty": {"conflict"},
    "behind": {"behind"},
    "blocked": {"gate open: CI running/failing", "gate open: review missing"},
    "unstable": {"gate open: CI running/failing"},
    "draft": {"gate open: draft"},
    "has_hooks": {"gate open: other"},
}

GATE_OPEN_GITHUB_VALUES = ("blocked", "draft", "unstable", "has_hooks")


def _github_value_rows(description: str, value: str) -> set[str]:
    header = _header_row(description)
    col = _column_index(header, "github")
    rows = _meaning_rows(description)
    return {
        meaning
        for meaning, row in rows.items()
        if value in _cell_tokens(row, col)
    }


@pytest.mark.parametrize("value", sorted(GITHUB_EXPECTED_ROWS))
def test_github_values_sit_in_expected_rows(value: str) -> None:
    """Driving test for R2: each GitHub `mergeable_state` value's actual
    set of table rows matches the ticket's declared row placement exactly
    — e.g. `blocked` sits in BOTH gate-open rows (CI and review), while
    `unstable` sits ONLY in the CI row, never in `mergeable`."""
    description = _served_descriptions()["get_pr"]
    actual = _github_value_rows(description, value)
    expected = GITHUB_EXPECTED_ROWS[value]
    assert actual == expected, (
        f"GitHub value {value!r} sits in rows {actual}, expected {expected} "
        f"in get_pr's description:\n{description}"
    )


def test_github_gate_open_values_never_in_mergeable_row() -> None:
    """Driving test for R2: the `mergeable` row's GitHub cell is exactly
    `{clean}`, and every one of the ticket's "Gate offen" values
    (`blocked`, `draft`, `unstable`, `has_hooks`) appears only in rows
    whose label starts with `gate open:` — never in `mergeable`."""
    description = _served_descriptions()["get_pr"]
    header = _header_row(description)
    col = _column_index(header, "github")
    rows = _meaning_rows(description)

    mergeable_row = rows.get("mergeable")
    assert mergeable_row is not None, "no 'mergeable' row in get_pr's description"
    mergeable_tokens = _cell_tokens(mergeable_row, col)
    assert mergeable_tokens == {"clean"}, (
        f"'mergeable' row's GitHub cell is {mergeable_tokens}, expected "
        "{'clean'} exactly"
    )

    for value in GATE_OPEN_GITHUB_VALUES:
        value_rows = _github_value_rows(description, value)
        assert value_rows, f"GitHub value {value!r} appears in no row"
        non_gate_rows = {m for m in value_rows if not m.startswith("gate open:")}
        assert not non_gate_rows, (
            f"GitHub value {value!r} appears in non-gate-open row(s) "
            f"{non_gate_rows} (only 'gate open:*' rows are allowed)"
        )


def test_gitlab_column_partitions_enum_and_matches_mapper() -> None:
    """Driving test for R2: the GitLab column is an exact partition of
    the `detailed_merge_status` enum sentence (which must include
    `not_approved`), and the real `gitlab._map_mergeable` returns `True`
    for the `mergeable` row's tokens, `None` for every other documented
    token — backing note (b) ("GitLab `mergeable` is `null` for every
    value except `mergeable`") against the real mapper, not an assumption.
    """
    description = _served_descriptions()["get_pr"]
    header = _header_row(description)
    col = _column_index(header, "gitlab")
    rows = _meaning_rows(description)

    enum_tokens = _gitlab_enum_tokens(description)

    seen: dict[str, str] = {}
    table_tokens: set[str] = set()
    for meaning, row in rows.items():
        tokens = _cell_tokens(row, col)
        for token in tokens:
            if token in seen and seen[token] != meaning:
                raise AssertionError(
                    f"GitLab value {token!r} appears in two rows: "
                    f"{seen[token]!r} and {meaning!r}"
                )
            seen[token] = meaning
        table_tokens |= tokens

    assert table_tokens == enum_tokens, (
        f"GitLab column tokens {table_tokens} do not match the enum "
        f"sentence tokens {enum_tokens} parsed from the same description"
    )

    mergeable_row = rows.get("mergeable")
    assert mergeable_row is not None, "no 'mergeable' row in get_pr's description"
    mergeable_tokens = _cell_tokens(mergeable_row, col)

    for token in enum_tokens:
        result = _map_mergeable({"detailed_merge_status": token})
        expected = True if token in mergeable_tokens else None
        assert result == expected, (
            f"gitlab._map_mergeable({{'detailed_merge_status': {token!r}}}) "
            f"returned {result!r}, expected {expected!r}"
        )


# ---------- R3: Azure DevOps classification from real merge_pr errors --------


def _azure_project() -> ProjectConfig:
    return ProjectConfig(id="acme", provider="azuredevops", path="org/project/repo")


def _azure_provider(
    monkeypatch: pytest.MonkeyPatch, get_payload: dict,
) -> AzureDevOpsProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        # Both the pre-merge handshake GET and every settle-loop GET (and
        # the completion PATCH, whose body is unread for light=False)
        # return the same fixed payload — sufficient because
        # `_MERGE_SETTLE_DELAYS_MS` is patched to a single zero-delay
        # attempt below, so only one settle read ever happens.
        return httpx.Response(200, json=get_payload)

    def fake_client(project, token, *, base_url=None):
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=base_url or "https://dev.azure.com",
        )

    monkeypatch.setattr(azuredevops, "_client", fake_client)
    provider = AzureDevOpsProvider()
    monkeypatch.setattr(provider, "_resolve_repository_id", lambda project, token: "repo")
    monkeypatch.setattr(provider, "_MERGE_SETTLE_DELAYS_MS", (0,))
    return provider


AZURE_CASES = [
    (
        "conflicts",
        {
            "status": "active",
            "mergeStatus": "conflicts",
            "lastMergeSourceCommit": {"commitId": "c"},
        },
        ["conflict"],
    ),
    (
        "rejectedByPolicy",
        {
            "status": "active",
            "mergeStatus": "rejectedByPolicy",
            "lastMergeSourceCommit": {"commitId": "c"},
        },
        ["gate open: CI running/failing", "gate open: review missing"],
    ),
    (
        "failure",
        {
            "status": "active",
            "mergeStatus": "failure",
            "lastMergeSourceCommit": {"commitId": "c"},
        },
        ["gate open: other"],
    ),
    (
        "queued_never_settles",
        {
            "status": "active",
            "mergeStatus": "queued",
            "lastMergeSourceCommit": {"commitId": "c"},
        },
        ["not computed yet"],
    ),
    (
        "already_completed",
        {
            "status": "completed",
            "mergeStatus": "succeeded",
            "lastMergeSourceCommit": {"commitId": "c"},
        },
        ["not open"],
    ),
]


@pytest.mark.parametrize(
    "case_id,get_payload,expected_meanings",
    AZURE_CASES,
    ids=[c[0] for c in AZURE_CASES],
)
def test_azure_error_text_matches_table(
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    get_payload: dict,
    expected_meanings: list[str],
) -> None:
    """Driving test for R3: drive the real pinned-lib
    `AzureDevOpsProvider.merge_pr` per outcome; the raised error's text
    must contain a backticked fragment from the documented row's Azure
    cell — grounded against real provider behaviour, never a hand-copied
    string. `rejectedByPolicy` must satisfy BOTH gate-open rows, since
    Azure's single error text can't distinguish a CI block from a review
    block (see plan note (c))."""
    description = _served_descriptions()["get_pr"]
    header = _header_row(description)
    col = _column_index(header, "azure")
    rows = _meaning_rows(description)

    provider = _azure_provider(monkeypatch, get_payload)
    project = _azure_project()

    with pytest.raises(AzureDevOpsError) as excinfo:
        provider.merge_pr(project, "tok", "42")
    error_text = str(excinfo.value)

    for meaning in expected_meanings:
        row = rows.get(meaning)
        assert row is not None, (
            f"{case_id}: expected meaning row {meaning!r} missing from "
            f"get_pr's description:\n{description}"
        )
        fragments = _cell_tokens(row, col)
        assert fragments, f"{case_id}: no backticked Azure fragment for row {meaning!r}"
        assert any(fragment in error_text for fragment in fragments), (
            f"{case_id}: error text {error_text!r} does not contain any "
            f"documented Azure fragment {fragments} for meaning {meaning!r}"
        )


def test_merge_pr_points_to_table() -> None:
    """Additional coverage for R3: `merge_pr`'s description names
    `get_pr` (the cross-reference the plan asks for) and lists all four
    Azure DevOps error-text fragments a caller needs to classify a failed
    merge without re-deriving them from the lib source."""
    description = _served_descriptions()["merge_pr"]
    assert "get_pr" in description, (
        f"merge_pr's description does not reference get_pr's merge-state "
        f"table:\n{description}"
    )
    for fragment in (
        "merge has conflicts",
        "merge rejected by branch policy",
        "merge failed",
        "merge in progress",
    ):
        assert fragment in description, (
            f"merge_pr's description is missing Azure fragment {fragment!r}:"
            f"\n{description}"
        )


def test_azure_map_merge_status_false_for_failure_statuses() -> None:
    """Additional coverage for R3 (already passing today, independent of
    the docstring change): every `_MERGE_FAILURE_STATUSES` member maps to
    `mergeable=False` via the real `_map_merge_status`, grounding the
    'Azure `mergeable` is only `true`/`false`/`null`' note (c)."""
    for status in azuredevops._MERGE_FAILURE_STATUSES:
        assert azuredevops._map_merge_status(status) is False, (
            f"_map_merge_status({status!r}) did not return False"
        )
