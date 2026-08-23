"""Regression tests for ticket #256: docstring inaccuracies.

  (1) `submit_pr_review`'s Azure DevOps paragraphs (vote mapping,
      reviewer side effect) said nothing about Azure's self-review
      policy, unlike the `approve`/`request_changes` bullets which
      already document GitHub's 422 self-review block (#250). This is
      the real gap #256 closes: a new "Azure DevOps self-review"
      paragraph documented on `submit_pr_review` in
      `project_issues_plugin/tools/pulls.py`.
  (2) `create_ticket` / `update_ticket`'s GitLab label-catalog wording
      was already corrected by ticket #248 (GitLab creates unknown
      labels on the fly, rather than the previously stale "presumably"
      claim that it enforced a catalog like GitHub). #256 only adds an
      anti-drift regression test here so that correction cannot regress
      silently.

Both are documentation-only fixes — no behavior/lib/permission-gate
changes; the actual behavior lives in the external `lib-python-projects`
package, not in this repo.

Follows the `_StubMCP` / module-level `register()` / `_normalize_ws()`
pattern used by `tests/test_250_docstring_relation_review_side_effects.py`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import tickets as ticket_tools


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
_ticket_tools = _register(ticket_tools)


# ---------------------------------------------------------------------------
# Requirement A — Azure self-review is documented (the ticket's real gap).
# ---------------------------------------------------------------------------


def _azure_self_review_slice(doc: str) -> str:
    """Slice the normalized docstring between the `"Azure DevOps
    self-review"` marker and the `` "`commit_sha`" `` paragraph that
    follows it, so neighbouring GitHub self-review bullets' similar text
    cannot make the assertion pass without the new paragraph existing.
    """
    start = doc.index("Azure DevOps self-review")
    end = doc.index("`commit_sha`", start)
    return doc[start:end]


def test_submit_pr_review_docstring_documents_azure_self_review_policy():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    window = _azure_self_review_slice(doc)
    assert "Azure" in window
    assert "no platform-level" in window
    assert '"approve"' in window
    assert '"request_changes"' in window
    assert "recorded" in window
    assert "reviewers" in window
    assert "policy" in window
    assert (
        "Prohibit the most recent pusher from approving their own changes"
        in window
    )
    # The vote-submission call itself has no self-review-policy handling
    # (it only special-cases TF401181, an inactive-PR error) — a branch
    # policy blocking self-approval is not an error from this call.
    assert "no self-review handling either" in window
    assert "not from this call" in window
    # The actual enforcement point is the required-reviewer/approval gate
    # at merge time, mirroring merge_pr's `rejectedByPolicy` handling.
    assert "required-reviewer" in window or "approval gate" in window
    assert "rejectedByPolicy" in window
    assert "merge_pr" in window
    assert "self-approval fails on that policy" not in window


def test_submit_pr_review_docstring_azure_self_review_contrasts_with_github_422():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    window = _azure_self_review_slice(doc)
    assert "422" in window
    assert "GitHub" in window


def test_submit_pr_review_docstring_azure_vote_mapping_intact():
    """Regression guard: the pre-existing Azure vote-mapping paragraph
    must still be present — guards against accidentally clobbering the
    neighboring paragraph while inserting the new one."""
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "approve_with_suggestions" in doc
    assert "wait_for_author" in doc
    assert "no vote" in doc


def test_submit_pr_review_docstring_azure_reviewer_side_effect_intact():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "requested_reviewers" in doc


def test_submit_pr_review_docstring_github_self_review_blocks_intact():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "Can not approve your own pull request" in doc
    assert "Can not request changes on your own pull request" in doc
    assert "GitHub platform restriction" in doc


def test_submit_pr_review_docstring_commit_sha_paragraph_still_follows():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "`commit_sha`, when set, pins the review" in doc


# ---------------------------------------------------------------------------
# Requirement B — GitLab label-catalog wording anti-drift (already
# satisfied today; characterization/anti-drift guard, not the red driver).
# ---------------------------------------------------------------------------


def test_create_ticket_and_update_ticket_docstrings_state_gitlab_creates_labels_on_the_fly():
    for name in ("create_ticket", "update_ticket"):
        doc = _normalize_ws(_ticket_tools[name].__doc__ or "")
        assert "presumably" not in doc
        assert "GitHub" in doc
        assert "404" in doc
        assert "catalog" in doc
        assert "GitLab does NOT share this restriction" in doc
        assert "created on the fly" in doc


def test_skill_md_has_no_stale_gitlab_label_catalog_claim():
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "project-issues"
        / "SKILL.md"
    )
    text = skill_path.read_text(encoding="utf-8")
    assert "presumably" not in text.lower()
    # Stale claim would assert GitLab enforces a pre-existing catalog like
    # GitHub (i.e. does NOT allow create-on-the-fly). Guard against that
    # regressing back in.
    assert not re.search(
        r"GitLab does\s+(?!NOT)\S*\s*share this restriction", text
    )
    assert "GitLab does NOT share this restriction" in text


# ---------------------------------------------------------------------------
# Cross-cutting guards.
# ---------------------------------------------------------------------------

_TICKET_PATTERN = re.compile(r"ticket\s+#|#256|ticket\s+256", re.IGNORECASE)


def test_edited_docstrings_have_no_internal_ticket_references():
    violations: list[str] = []
    doc = _pull_tools["submit_pr_review"].__doc__ or ""
    if _TICKET_PATTERN.search(doc):
        violations.append("submit_pr_review")
    doc = _ticket_tools["create_ticket"].__doc__ or ""
    if _TICKET_PATTERN.search(doc):
        violations.append("create_ticket")
    doc = _ticket_tools["update_ticket"].__doc__ or ""
    if _TICKET_PATTERN.search(doc):
        violations.append("update_ticket")
    assert not violations, (
        f"internal ticket references found in docstrings: {violations}"
    )


# ---------------------------------------------------------------------------
# Behaviour-unchanged guards (proves this is docs-only).
# ---------------------------------------------------------------------------


def test_submit_pr_review_still_rejects_unsupported_azure_state():
    result = _pull_tools["submit_pr_review"](
        project_id="does-not-matter",
        pr_id="1",
        state="approve_with_suggestions",
    )
    assert "error" in result


def test_submit_pr_review_still_requires_body_for_request_changes():
    result = _pull_tools["submit_pr_review"](
        project_id="does-not-matter",
        pr_id="1",
        state="request_changes",
    )
    assert "error" in result
    assert "body" in result["error"]


def test_submit_pr_review_unresolvable_project_returns_error_not_raise():
    result = _pull_tools["submit_pr_review"](
        project_id="__nonexistent_project__",
        pr_id="1",
        state="approve",
    )
    assert "error" in result
