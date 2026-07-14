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

import re
from typing import Callable

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
# Hygiene guard — mirrors test_tool_docstring_hygiene.py's pattern: none of
# the new prose may contain the literal string "ticket #" (case-insensitive).
# ---------------------------------------------------------------------------

_TICKET_PATTERN = re.compile(r"ticket\s+#", re.IGNORECASE)


def test_new_docstrings_contain_no_internal_ticket_references():
    violations: list[str] = []
    for name in ("get_pr", "create_pr", "merge_pr", "submit_pr_review"):
        doc = _pull_tools[name].__doc__ or ""
        if _TICKET_PATTERN.search(doc):
            violations.append(name)
    assert not violations, (
        f"internal ticket references found in docstrings: {violations}"
    )
