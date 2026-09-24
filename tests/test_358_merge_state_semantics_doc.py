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

# Per-row expected content, grounded in the plan's literal table — so a
# table with the right nine labels but a wrong, blank, or garbage
# provider cell fails, not just a table missing a label (test-critic
# F1: a bare label-presence check "would pass even with wrong or empty
# provider cells").
EXPECTED_GITHUB_CELL_TOKENS: dict[str, frozenset[str]] = {
    "mergeable": frozenset({"clean"}),
    "not computed yet": frozenset({"unknown"}),
    "conflict": frozenset({"dirty"}),
    "behind": frozenset({"behind"}),
    "gate open: CI running/failing": frozenset({"blocked", "unstable"}),
    "gate open: review missing": frozenset({"blocked"}),
    "gate open: draft": frozenset({"draft"}),
    "gate open: other": frozenset({"has_hooks"}),
    "not open": frozenset(),
}

EXPECTED_GITLAB_CELL_TOKENS: dict[str, frozenset[str]] = {
    "mergeable": frozenset({"mergeable"}),
    "not computed yet": frozenset({"unchecked", "checking", "preparing"}),
    "conflict": frozenset({"conflict"}),
    "behind": frozenset({"need_rebase"}),
    "gate open: CI running/failing": frozenset({"ci_must_pass", "ci_still_running"}),
    "gate open: review missing": frozenset({"not_approved", "discussions_not_resolved"}),
    "gate open: draft": frozenset({"draft_status"}),
    "gate open: other": frozenset({
        "blocked_status", "broken_status", "commits_status",
        "jira_association_missing", "not_mergeable",
    }),
    "not open": frozenset({"not_open"}),
}

# Azure's cell is not always backticked — "(merge succeeds)", "not
# verified", "already merged" and "—" are prose notes, not literal
# `merge_pr` error fragments (only 5 of the 9 rows carry a real
# backticked fragment). Compared as plain text with backticks stripped.
EXPECTED_AZURE_CELL_TEXT: dict[str, str] = {
    "mergeable": "(merge succeeds)",
    "not computed yet": "merge in progress",
    "conflict": "merge has conflicts",
    "behind": "—",
    "gate open: CI running/failing": "merge rejected by branch policy",
    "gate open: review missing": "merge rejected by branch policy",
    "gate open: draft": "not verified",
    "gate open: other": "merge failed",
    "not open": "already merged",
}


def _azure_cell_text(row: str, col: int) -> str:
    """Return a table row's Azure cell as plain text, backticks and
    surrounding whitespace stripped — works for both backticked literal
    fragments (e.g. "`merge failed`") and prose cells (e.g.
    "(merge succeeds)")."""
    return _row_cells(row)[col].strip().strip("`").strip()


def test_get_pr_has_merge_state_table() -> None:
    """Driving test for R1: `get_pr`'s description carries the
    provider-neutral merge-state table with all nine labelled rows, AND
    each row's GitHub/GitLab/Azure cells hold the documented values —
    not merely the right label over an empty or wrong cell."""
    description = _served_descriptions()["get_pr"]
    header = _header_row(description)
    github_col = _column_index(header, "github")
    gitlab_col = _column_index(header, "gitlab")
    azure_col = _column_index(header, "azure")
    rows = _meaning_rows(description)

    missing = [m for m in EXPECTED_MEANINGS if m not in rows]
    assert not missing, (
        f"merge-state table is missing meaning row(s) {missing} in "
        f"get_pr's description:\n{description}"
    )

    for meaning in EXPECTED_MEANINGS:
        row = rows[meaning]

        github_tokens = _cell_tokens(row, github_col)
        assert github_tokens == EXPECTED_GITHUB_CELL_TOKENS[meaning], (
            f"row {meaning!r}: GitHub cell tokens {github_tokens} != "
            f"expected {EXPECTED_GITHUB_CELL_TOKENS[meaning]} in get_pr's "
            f"description:\n{description}"
        )

        gitlab_tokens = _cell_tokens(row, gitlab_col)
        assert gitlab_tokens == EXPECTED_GITLAB_CELL_TOKENS[meaning], (
            f"row {meaning!r}: GitLab cell tokens {gitlab_tokens} != "
            f"expected {EXPECTED_GITLAB_CELL_TOKENS[meaning]} in get_pr's "
            f"description:\n{description}"
        )

        azure_text = _azure_cell_text(row, azure_col)
        assert azure_text == EXPECTED_AZURE_CELL_TEXT[meaning], (
            f"row {meaning!r}: Azure cell text {azure_text!r} != expected "
            f"{EXPECTED_AZURE_CELL_TEXT[meaning]!r} in get_pr's "
            f"description:\n{description}"
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


# The ticket's reference classification (plan lines 20-30) for every
# GitLab `detailed_merge_status` token — used to check row *placement*
# directly, independent of `gitlab._map_mergeable` (test-critic F4:
# `_map_mergeable` returns `None` for every non-`mergeable` value, so it
# cannot by itself tell `conflict` in the conflict row apart from
# `conflict` misplaced in the `behind` row — it only grounds the
# mergeable/not-mergeable boundary, checked separately below).
GITLAB_EXPECTED_ROWS: dict[str, str] = {
    "mergeable": "mergeable",
    "unchecked": "not computed yet",
    "checking": "not computed yet",
    "preparing": "not computed yet",
    "conflict": "conflict",
    "need_rebase": "behind",
    "ci_must_pass": "gate open: CI running/failing",
    "ci_still_running": "gate open: CI running/failing",
    "not_approved": "gate open: review missing",
    "discussions_not_resolved": "gate open: review missing",
    "draft_status": "gate open: draft",
    "blocked_status": "gate open: other",
    "broken_status": "gate open: other",
    "commits_status": "gate open: other",
    "jira_association_missing": "gate open: other",
    "not_mergeable": "gate open: other",
    "not_open": "not open",
}


def _gitlab_value_row(rows: dict[str, str], col: int, token: str) -> str | None:
    """Return the single Meaning-row label whose GitLab column contains
    `token`, or None if it appears in no row."""
    for meaning, row in rows.items():
        if token in _cell_tokens(row, col):
            return meaning
    return None


def test_gitlab_column_partitions_enum_and_matches_mapper() -> None:
    """Driving test for R2: the GitLab column is an exact partition of
    the `detailed_merge_status` enum sentence (which must include
    `not_approved` — test-critic F5), each token sits in the row the
    ticket's reference table names for it (test-critic F4, e.g.
    `conflict` must sit in the `conflict` row, never in `behind`), and
    the real `gitlab._map_mergeable` returns `True` for the `mergeable`
    row's tokens, `None` for every other documented token — backing
    note (b) ("GitLab `mergeable` is `null` for every value except
    `mergeable`") against the real mapper. `_map_mergeable` only grounds
    that mergeable/not-mergeable boundary; the reference-table check
    above is what grounds placement among the eight non-mergeable rows.
    """
    description = _served_descriptions()["get_pr"]
    header = _header_row(description)
    col = _column_index(header, "gitlab")
    rows = _meaning_rows(description)

    enum_tokens = _gitlab_enum_tokens(description)
    assert "not_approved" in enum_tokens, (
        "GitLab `detailed_merge_status` enum sentence is missing "
        f"'not_approved' (the ticket names it as missing today):\n{description}"
    )

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

    for token, expected_meaning in GITLAB_EXPECTED_ROWS.items():
        actual_meaning = _gitlab_value_row(rows, col, token)
        assert actual_meaning == expected_meaning, (
            f"GitLab value {token!r} sits in row {actual_meaning!r}, "
            f"expected {expected_meaning!r} per the ticket's reference "
            f"table, in get_pr's description:\n{description}"
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
    must contain the documented row's Azure fragment, grounded against
    real provider behaviour, never a hand-copied string. Also checks
    every OTHER row's fragment is absent from that same error text
    (test-critic F3: a substring-only check can't tell a specific
    fragment from a generic one that happens to be a substring of every
    error — e.g. every cell holding `merge`, or every cell listing all
    five fragments, would satisfy a same-row-only check). `rejectedByPolicy`
    must satisfy BOTH gate-open rows, since Azure's single error text
    can't distinguish a CI block from a review block (see plan note (c));
    those two rows are expected to share one fragment, so the
    cross-row check exempts fragments shared with an expected row."""
    description = _served_descriptions()["get_pr"]
    header = _header_row(description)
    col = _column_index(header, "azure")
    rows = _meaning_rows(description)

    # `_azure_cell_text` returns None-worthy "—" placeholders (rows with
    # no real classifier fragment, e.g. `behind`) as themselves; filter
    # those out below since "—" also occurs as a prose em-dash separator
    # inside several real error messages (e.g. "merge has conflicts —
    # resolve before retrying") and must never be treated as a fragment.
    fragment_by_meaning: dict[str, str | None] = {
        meaning: (
            None
            if (text := _azure_cell_text(row, col)) in ("", "—")
            else text
        )
        for meaning, row in rows.items()
    }

    provider = _azure_provider(monkeypatch, get_payload)
    project = _azure_project()

    with pytest.raises(AzureDevOpsError) as excinfo:
        provider.merge_pr(project, "tok", "42")
    error_text = str(excinfo.value)

    for meaning in expected_meanings:
        assert meaning in rows, (
            f"{case_id}: expected meaning row {meaning!r} missing from "
            f"get_pr's description:\n{description}"
        )
        fragment = fragment_by_meaning.get(meaning)
        assert fragment, (
            f"{case_id}: no real Azure classifier fragment documented for "
            f"row {meaning!r} in get_pr's description:\n{description}"
        )
        assert fragment in error_text, (
            f"{case_id}: error text {error_text!r} does not contain the "
            f"documented Azure fragment {fragment!r} for meaning {meaning!r}"
        )

    for other_meaning, other_fragment in fragment_by_meaning.items():
        if other_meaning in expected_meanings or not other_fragment:
            continue
        assert other_fragment not in error_text, (
            f"{case_id}: error text {error_text!r} unexpectedly also "
            f"contains row {other_meaning!r}'s Azure fragment "
            f"{other_fragment!r} — a documented fragment must uniquely "
            f"identify its own row(s), not merely appear somewhere in "
            f"the text (e.g. a generic fragment shared by every row)"
        )


def _cross_reference_paragraph(description: str) -> str:
    """Return the paragraph in a description that cross-references
    `get_pr`'s merge-state table — i.e. a paragraph mentioning both
    `get_pr` and the phrase "merge-state table" together, not merely a
    paragraph that happens to mention `get_pr` for an unrelated reason
    (test-critic F2: `merge_pr`'s existing "Returns" paragraph already
    says "without a follow-up `get_pr`", which a bare
    'get_pr' in description check would wrongly accept as the
    cross-reference)."""
    for paragraph in description.split("\n\n"):
        if "get_pr" in paragraph and "merge-state table" in paragraph:
            return paragraph
    raise AssertionError(
        "no paragraph cross-references get_pr's merge-state table "
        f"(a paragraph containing both 'get_pr' and 'merge-state table' "
        f"is required):\n{description}"
    )


def test_merge_pr_points_to_table() -> None:
    """Additional coverage for R3: `merge_pr`'s description carries one
    coherent cross-reference sentence/paragraph — naming `get_pr`'s
    merge-state table specifically, not just the word `get_pr` and the
    four Azure phrases scattered anywhere in the docstring — and that
    same paragraph lists all four Azure DevOps error-text fragments a
    caller needs to classify a failed merge without re-deriving them
    from the lib source."""
    description = _served_descriptions()["merge_pr"]
    paragraph = _cross_reference_paragraph(description)
    for fragment in (
        "merge has conflicts",
        "merge rejected by branch policy",
        "merge failed",
        "merge in progress",
    ):
        assert fragment in paragraph, (
            f"merge_pr's get_pr/merge-state-table cross-reference "
            f"paragraph is missing Azure fragment {fragment!r}:"
            f"\n{paragraph}"
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
