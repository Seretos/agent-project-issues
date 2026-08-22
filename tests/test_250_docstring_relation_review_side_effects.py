"""Regression tests for ticket #250: two tool docstrings omitted real
side-effects/restrictions their operations trigger.

  (1) `add_relation`'s `duplicate_of` bullet said only that it "triggers a
      body-edit and closes the source on GitHub and GitLab" — vague about
      the body edit, and no cross-reference to `remove_relation` as the
      reversal. Documented on `add_relation` in
      `project_issues_plugin/tools/relations.py`.
  (2) `submit_pr_review`'s `request_changes` bullet didn't mention that
      GitHub applies the identical self-review block that the `approve`
      bullet documents in detail (the underlying check is state-agnostic).
      Documented on `submit_pr_review` in
      `project_issues_plugin/tools/pulls.py`.

Both are documentation-only fixes — no behavior/lib/permission-gate
changes; the actual behavior lives in the external `lib-python-projects`
package, not in this repo.

Follows the `_StubMCP` / module-level `register()` pattern used by
`tests/test_253_docstring_create_pr_zero_diff.py` /
`tests/test_181_docstring_pr_cross_provider.py`.
"""
from __future__ import annotations

import re
from typing import Callable

from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import relations as relation_tools


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


_relation_tools = _register(relation_tools)
_pull_tools = _register(pull_tools)


# ---------------------------------------------------------------------------
# (1) add_relation's duplicate_of bullet.
# ---------------------------------------------------------------------------


def test_add_relation_docstring_documents_duplicate_of_body_edit_and_close():
    doc = _normalize_ws(_relation_tools["add_relation"].__doc__ or "")
    assert "Duplicate of #N" in doc
    assert "remove_relation" in doc
    assert "closes" in doc or "closed" in doc


def test_add_relation_docstring_still_names_github_and_gitlab_verbatim():
    """Regression guard for ticket #197: keep the exact "GitHub and GitLab"
    phrase and the absence of the vaguer "both providers" phrase."""
    doc = _normalize_ws(_relation_tools["add_relation"].__doc__ or "")
    assert "GitHub and GitLab" in doc
    assert "both providers" not in doc


def test_add_relation_docstring_still_mentions_azure_devops():
    doc = _normalize_ws(_relation_tools["add_relation"].__doc__ or "")
    assert "Azure DevOps" in doc


# ---------------------------------------------------------------------------
# (2) submit_pr_review's request_changes bullet.
# ---------------------------------------------------------------------------


def _request_changes_slice(doc: str) -> str:
    """Slice the normalized docstring between the `"request_changes"`
    bullet marker and the next `"comment"` bullet marker. Asserting only
    within this slice (rather than the whole docstring) prevents the
    neighboring `approve` bullet's similar text from making the assertion
    pass without the `request_changes` bullet actually being edited.
    """
    start = doc.index('"request_changes"')
    end = doc.index('"comment"', start)
    return doc[start:end]


def test_submit_pr_review_docstring_documents_request_changes_self_review_block():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    window = _request_changes_slice(doc)
    assert "own pull request" in window
    assert "422" in window
    assert "GitHub platform restriction" in window


def test_submit_pr_review_docstring_request_changes_slice_mentions_gitlab():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    window = _request_changes_slice(doc)
    assert "GitLab" in window


def test_submit_pr_review_docstring_approve_bullet_unchanged():
    """Regression guard: the approve bullet's pinned text (asserted by
    tests/test_181_docstring_pr_cross_provider.py) must still be present —
    guards against accidentally touching the neighboring bullet.
    """
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "Can not approve your own pull request" in doc
    assert "self-approval" in doc


# ---------------------------------------------------------------------------
# Hygiene guard — mirrors test_tool_docstring_hygiene.py's pattern.
# ---------------------------------------------------------------------------

_TICKET_PATTERN = re.compile(r"ticket\s+#|#250|ticket\s+250", re.IGNORECASE)


def test_edited_docstrings_have_no_internal_ticket_references():
    violations: list[str] = []
    doc = _relation_tools["add_relation"].__doc__ or ""
    if _TICKET_PATTERN.search(doc):
        violations.append("add_relation")
    doc = _pull_tools["submit_pr_review"].__doc__ or ""
    if _TICKET_PATTERN.search(doc):
        violations.append("submit_pr_review")
    assert not violations, (
        f"internal ticket references found in docstrings: {violations}"
    )
