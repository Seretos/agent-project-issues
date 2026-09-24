"""Regression tests for ticket #181: five undocumented cross-provider PR
quirks/side-effects found during an E2E sweep of PR-lifecycle/review flows.

None of these are wrapper bugs — the underlying provider behavior is
correct/native — but the tool docstrings in
`project_issues_plugin/tools/pulls.py` didn't warn agents about them, so an
agent could misinterpret provider responses. This is a docstring-only fix;
no behavior changed.

  (a) GitHub and Azure DevOps can both populate `merge_commit_sha` (with
      `mergeable: true`) speculatively right after PR creation — a native
      pre-merge preview, not proof a merge happened. Documented on both
      `get_pr` and `create_pr`.
  (b) Merging a PR does NOT add the merging user to `requested_reviewers`
      (an earlier report of this was retracted as unreproducible);
      merge mutates only PR-state fields. Documented on `merge_pr`.
  (c) GitLab's `base.sha` is `null` immediately after `create_pr` and only
      populates on a later fetch. Documented on both `get_pr` and
      `create_pr`.
  (d) GitHub hard-blocks self-approval (`Can not approve your own pull
      request`); GitLab allows it. Documented on `submit_pr_review`.
  (e) `detailed_merge_status` is GitLab-only; GitHub/Azure DevOps always
      return `null`. Documented on `get_pr`, with the GitLab enum values
      enumerated.

Follows the `_StubMCP` / module-level `register()` pattern used by
`tests/test_180_docstring_behavior.py`.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import re
from typing import Callable

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool

from lib_python_projects import ProjectConfig
from lib_python_projects.providers import gitlab as gitlab_provider
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider
from project_issues_plugin.tools import pulls as pull_tools


class _StubMCP:
    """Minimal FastMCP stub that records registered tool callables."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register(module) -> dict[str, Callable]:
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


def _normalize_ws(text: str) -> str:
    """Collapse all whitespace runs (including the newline + indentation
    between wrapped docstring lines) to a single space.

    CPython 3.13+ strips the common leading whitespace from multi-line
    docstrings at compile time (`__doc__` comes back dedented); older
    versions (e.g. the 3.12 CI pin) do not, so a wrapped docstring line
    is followed by `\\n` plus the source's indentation instead of a bare
    `\\n`. Comparing against whitespace-normalized text keeps these
    assertions correct on both.
    """
    return " ".join(text.split())


_pull_tools = _register(pull_tools)


# ---------------------------------------------------------------------------
# Item (a) — Azure DevOps speculative merge_commit_sha, on get_pr AND
# create_pr.
# ---------------------------------------------------------------------------


def test_get_pr_docstring_documents_ado_speculative_merge_commit_sha():
    doc = _normalize_ws(_pull_tools["get_pr"].__doc__ or "")
    assert "Azure DevOps" in doc
    assert "merge_commit_sha" in doc
    assert "speculative" in doc or "pre-merge" in doc
    assert "merged" in doc and "status" in doc


def test_create_pr_docstring_documents_ado_speculative_merge_commit_sha():
    doc = _normalize_ws(_pull_tools["create_pr"].__doc__ or "")
    assert "Azure DevOps" in doc
    assert "merge_commit_sha" in doc
    assert "speculative" in doc or "pre-merge" in doc
    assert "merged" in doc and "status" in doc


# ---------------------------------------------------------------------------
# Item (c) — GitLab base.sha null-then-populates, on get_pr AND create_pr.
# ---------------------------------------------------------------------------


def test_get_pr_docstring_documents_gitlab_base_sha_null_quirk():
    doc = _normalize_ws(_pull_tools["get_pr"].__doc__ or "")
    assert "GitLab" in doc
    assert "base.sha" in doc
    assert "null" in doc
    assert "later" in doc or "fetch" in doc.lower()


def test_create_pr_docstring_documents_gitlab_base_sha_null_quirk():
    doc = _normalize_ws(_pull_tools["create_pr"].__doc__ or "")
    assert "GitLab" in doc
    assert "base.sha" in doc
    assert "null" in doc
    assert "later" in doc or "fetch" in doc.lower()


# ---------------------------------------------------------------------------
# Item (b) — Azure DevOps merge does NOT mutate requested_reviewers, on
# merge_pr.
# ---------------------------------------------------------------------------


def test_merge_pr_docstring_documents_no_requested_reviewers_side_effect():
    doc = _normalize_ws(_pull_tools["merge_pr"].__doc__ or "").lower()
    assert "azure devops" in doc
    assert "requested_reviewers" in doc
    # The corrected note negates the previously-claimed side effect. Check
    # for "not add" specifically (not a bare "does not" substring, which
    # false-positives on the unrelated "does not itself merge" rebase
    # sentence already present in this docstring before the fix).
    assert "not add" in doc
    # Merge mutates only PR-state fields, per the correction.
    assert "merged" in doc and "status" in doc


# ---------------------------------------------------------------------------
# Item (d) — self-approval policy divergence, on submit_pr_review.
# ---------------------------------------------------------------------------


def test_submit_pr_review_docstring_documents_self_approval_divergence():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "Can not approve your own pull request" in doc, (
        "submit_pr_review docstring should quote GitHub's exact "
        "passthrough self-approval error message"
    )
    assert "GitLab" in doc
    assert "allows self-approval" in doc or "self-approval outright" in doc


def test_submit_pr_review_docstring_documents_ado_reviewers_side_effect():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "Azure DevOps" in doc
    # approve / request_changes land in the scored reviewers collection...
    assert "reviewers" in doc
    # ...while comment surfaces only as a transient requested_reviewers entry.
    assert "requested_reviewers" in doc
    assert "transient" in doc


# ---------------------------------------------------------------------------
# Item (e) — detailed_merge_status is GitLab-only, with enumerated values,
# on get_pr.
# ---------------------------------------------------------------------------


def test_get_pr_docstring_states_detailed_merge_status_is_gitlab_only():
    doc = _normalize_ws(_pull_tools["get_pr"].__doc__ or "")
    assert "detailed_merge_status" in doc
    idx = doc.index("detailed_merge_status")
    window = doc[idx: idx + 300]
    assert "GitLab-only" in window or "GitLab" in window
    assert "null" in window


def test_get_pr_docstring_enumerates_detailed_merge_status_sample_values():
    doc = _pull_tools["get_pr"].__doc__ or ""
    assert "preparing" in doc
    assert "not_open" in doc


def test_get_pr_surfaces_github_specific_fields_still_covers_detailed_merge_status_drift():
    """Drift guard: `tests/test_pulls.py` already asserts
    `pr["detailed_merge_status"] is None` on a GitHub payload (around the
    response-shape-inventory block) — this is the ground truth the
    docstring's "GitHub always returns null" claim must match. Confirm the
    dataclass default stays `None` so the docstring claim can't silently
    drift from behavior.
    """
    from dataclasses import fields

    from lib_python_projects.providers.base import PullRequest

    field_map = {f.name: f for f in fields(PullRequest)}
    assert field_map["detailed_merge_status"].default is None


# ---------------------------------------------------------------------------
# #383 R1 — submit_pr_review fits the ~2000-char client truncation cliff
# (test_284_docstring_front_loading.py's `_CLIFF`) without losing any
# caveat's meaning.
#
# Uses the served text (FastMCP("t") + register + list_tools, mirroring
# tests/test_358_merge_state_semantics_doc.py:52-56) rather than the
# `_StubMCP`-captured raw `__doc__` above: the ~2000-char cliff is a
# property of what the MCP client is actually served.
# ---------------------------------------------------------------------------


def _served_tools() -> dict[str, MCPTool]:
    mcp = FastMCP("t")
    pull_tools.register(mcp)
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


_CLIFF = 2000  # copied from tests/test_284_docstring_front_loading.py


def _norm(s: str) -> str:
    """Collapse whitespace runs to a single space. Copied (not imported —
    `tests` is not an importable package) from
    tests/test_284_docstring_front_loading.py / test_358's `_norm`."""
    return re.sub(r"\s+", " ", s).strip()


def test_submit_pr_review_served_description_fits_cliff():
    """R1 driving test. RED today: the served `submit_pr_review`
    description normalises to ~3,766 chars — well past the 2000-char
    client truncation cliff `test_284_docstring_front_loading.py` pins as
    `_CLIFF`, cutting the text mid-sentence before the Azure reviewer
    side-effect paragraph. GREEN once the docstring is reordered and
    tightened to <=2000 while keeping every caveat's meaning (see the
    `_CAVEATS` guard below)."""
    served = _served_tools()["submit_pr_review"].description or ""
    length = len(_norm(served))
    assert length <= _CLIFF, (
        f"submit_pr_review's served description is {length} normalised "
        f"chars, past the {_CLIFF}-char client truncation cliff"
    )


# Each row: (label, [condition, consequence, ...], max char gap applied
# between each successive pair in the chain). Asserts the consequence
# follows its condition within one short clause, so a caveat can't be
# "kept" by leaving its condition dangling far from its payoff. Rows 8a-c
# are the plan's single numbered row 8, split into its three ;-separated
# chains so a failure can name which one broke.
_CAVEATS: list[tuple[str, list[str], int]] = [
    ("1", [r"GitLab", r"separate note"], 120),
    ("2", [r"GitHub", r"`Can not approve your own pull request`"], 150),
    ("3", [r"GitLab", r"self-approv"], 60),
    ("4", [r"approve-then-merge", r"GitHub"], 120),
    ("5", [r"GitLab", r"`unapprove`"], 80),
    ("6", [r"without changing", r"approval state"], 10),
    ("7", [r"`approve_with_suggestions`", r"`wait_for_author`", r'\{"error"'], 250),
    ("8a", [r'"approve"', r"approved", r"\+10"], 30),
    ("8b", [r'"request_changes"', r"rejected", r"-10"], 30),
    ("8c", [r'"comment"', r"no vote"], 30),
    ("9", [r'"request_changes"', r"(add|update)", r"`reviewers`"], 100),
    ("10", [r'"comment"', r"does not", r"transient", r"requested_reviewers"], 250),
    ("11", [r"recorded", r"`reviewers`", r"not", r"requested_reviewers"], 80),
    ("12", [r"GitLab", r"ignores `commit_sha`", r"commit_sha: null"], 200),
    ("13", [r"`commit_sha`", r"GitHub", r"commit_id"], 120),
    ("14", [r'response="light"', r"submitted_at"], 80),
    ("15", [r"(?i:not prepend)", r"#ai-generated"], 20),
    (
        "16",
        [
            r"pulls\.modify",
            r"reviewing is treated as modifying the PR",
            r"no separate review flag",
        ],
        200,
    ),
]


def test_submit_pr_review_caveats_keep_condition_and_consequence():
    """R1 additional coverage. Already passes on TODAY's (pre-rewrite)
    text — proving every caveat's condition+consequence pairing already
    holds — and must stay green after the cliff-fitting rewrite (R1's
    Approach only reorders/tightens prose; it must never separate a
    caveat's condition from its consequence by more than the row's own
    gap). A failure names the row."""
    doc = _norm(_served_tools()["submit_pr_review"].description or "")
    for row_id, parts, gap in _CAVEATS:
        pattern = f".{{0,{gap}}}?".join(parts)
        assert re.search(pattern, doc, re.S), (
            f"row {row_id}: expected {parts!r} within {gap} chars of each "
            f"other in submit_pr_review's served description"
        )


# ---------------------------------------------------------------------------
# #383 R2 — the update_pr draft note is true for the pinned lib
# (lib-python-projects v0.3.24, per pyproject.toml) and sits before the
# cliff.
# ---------------------------------------------------------------------------


def test_update_pr_draft_note_states_prefix_stripped_title_before_cliff():
    """R2 driving test. RED today: the `draft` paragraph says "GitLab
    manipulates the title prefix (`Draft: `)" — no `stripped` token, and
    no cross-reference telling the caller to read the `draft` field
    instead of `title`. GREEN once the paragraph states GitLab writes the
    `Draft: ` prefix, that the returned `title` is prefix-stripped, and
    that callers should read the `draft` field — relocated so it sits
    well before the ~2000-char client truncation cliff."""
    doc = _norm(_served_tools()["update_pr"].description or "")
    start = doc.index("`draft` toggles")
    window = doc[start : start + 700]
    assert "GitLab" in window
    assert "`Draft: `" in window
    assert "stripped" in window
    assert "`draft` field" in window

    last_token_end = start + max(
        window.index("stripped") + len("stripped"),
        window.index("`draft` field") + len("`draft` field"),
    )
    assert last_token_end <= _CLIFF, (
        f"update_pr's draft note ends at {last_token_end} normalised "
        f"chars, past the {_CLIFF}-char client truncation cliff"
    )

    merge_doc = _served_tools()["merge_pr"].description or ""
    assert "Draft:" not in merge_doc, (
        "merge_pr's docstring must stay free of any draft/title-prefix claim"
    )


def test_gitlab_update_pr_draft_uses_title_prefix(monkeypatch: pytest.MonkeyPatch):
    """R2 additional coverage: behavioural drift guard driven against the
    REAL pinned `GitLabProvider.update_pr` (lib-python-projects v0.3.24)
    via an `httpx.MockTransport`, mirroring
    tests/test_359_pr_close_syntax_doc.py's `_client`-monkeypatch pattern
    (l.428-436). Proves `update_pr` still *calls* the title-prefix path —
    not just that `_apply_draft_prefix` exists as dead code — so a future
    lib bump to a native `draft` write flag fails this guard and forces
    R2's docstring wording to be revised rather than silently going stale.
    """
    version = importlib.metadata.version("lib-python-projects")
    project = ProjectConfig(id="acme", provider="gitlab", path="group/proj")

    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith(
            "/merge_requests/1"
        ):
            return httpx.Response(
                200,
                json={
                    "iid": 1,
                    "title": "T",
                    "draft": False,
                    "state": "opened",
                    "labels": [],
                    "source_branch": "feat",
                    "target_branch": "main",
                    "description": "",
                },
            )
        if request.method == "PUT" and request.url.path.endswith(
            "/merge_requests/1"
        ):
            payload = json.loads(request.content)
            captured["payload"] = payload
            return httpx.Response(
                200,
                json={
                    "iid": 1,
                    "title": payload.get("title", "T"),
                    "draft": True,
                    "state": "opened",
                    "labels": [],
                    "source_branch": "feat",
                    "target_branch": "main",
                    "description": "",
                },
            )
        raise AssertionError(f"unexpected GitLab request: {request.method} {request.url}")

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url="https://gitlab.example.com/api/v4",
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(gitlab_provider, "_client", fake_client)

    pr = GitLabProvider().update_pr(project, "tok", "1", draft=True)

    assert captured["payload"]["title"] == "Draft: T", (
        f"lib-python-projects {version}: expected GitLab update_pr to send "
        f"a 'Draft: ' title prefix; got {captured['payload']!r}"
    )
    assert "draft" not in captured["payload"], (
        f"lib-python-projects {version}: expected no native `draft` key in "
        f"the PUT payload — the prefix path must still be the only "
        f"mechanism; got {captured['payload']!r}"
    )
    assert pr.title == "T", (
        f"lib-python-projects {version}: expected the returned title to "
        f"have the `Draft: ` prefix stripped; got {pr.title!r}"
    )
    assert pr.draft is True, (
        f"lib-python-projects {version}: expected `draft` to be read from "
        f"the response's `draft` field; got {pr.draft!r}"
    )


# ---------------------------------------------------------------------------
# #383 R3 — add_pr_review_comment names the Azure line-range limitation.
# ---------------------------------------------------------------------------


def test_add_pr_review_comment_states_azure_line_ranges_null():
    """R3 driving test. RED today: `supports_line_ranges` is absent from
    `add_pr_review_comment`'s served description entirely."""
    tool = _served_tools()["add_pr_review_comment"]
    doc = tool.description or ""
    idx = doc.index("supports_line_ranges")
    window = doc[max(0, idx - 300) : idx + 300]
    assert "Azure DevOps" in window
    assert "line_ranges: null" in window
    assert "false" in window
    assert idx + 300 <= _CLIFF

    line_desc = tool.inputSchema["properties"]["line"]["description"]
    assert "Azure DevOps" in line_desc
    assert "null" in line_desc


def test_supports_line_ranges_flag_matches_providers():
    """R3 additional coverage (mirrors pulls.py:437's
    `getattr(provider, "SUPPORTS_DIFF_LINE_RANGES", False)` read): the
    Azure flag is falsy, GitHub's and GitLab's are truthy. May already
    pass today — this just grounds the new docstring claim in the real
    provider classes rather than a hand-transcribed fact."""
    assert not getattr(AzureDevOpsProvider, "SUPPORTS_DIFF_LINE_RANGES", False)
    assert getattr(GitHubProvider, "SUPPORTS_DIFF_LINE_RANGES", False)
    assert getattr(GitLabProvider, "SUPPORTS_DIFF_LINE_RANGES", False)


# ---------------------------------------------------------------------------
# Hygiene guard — mirrors test_tool_docstring_hygiene.py's pattern: none of
# the new prose may contain the literal string "ticket #" (case-insensitive).
# ---------------------------------------------------------------------------

_TICKET_PATTERN = re.compile(r"ticket\s+#", re.IGNORECASE)


def test_new_docstrings_contain_no_internal_ticket_references():
    violations: list[str] = []
    for name in (
        "get_pr",
        "create_pr",
        "merge_pr",
        "submit_pr_review",
        "update_pr",
        "add_pr_review_comment",
    ):
        doc = _pull_tools[name].__doc__ or ""
        if _TICKET_PATTERN.search(doc):
            violations.append(name)
    assert not violations, (
        f"internal ticket references found in docstrings: {violations}"
    )
