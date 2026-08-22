"""Regression test for ticket #253: `create_pr`'s docstring didn't document
a cross-provider behavioral divergence when the `head` branch has zero diff
vs. `base` — GitHub rejects the create (422: "No commits between <base> and
<branch>") while GitLab silently creates the MR anyway with an empty diff.

Neither behavior is a wrapper bug — both are native/correct for their
platform — but the tool docstring didn't warn agents about the divergence.
This is a docstring-only fix; no behavior changed.

Follows the `_StubMCP` / module-level `register()` pattern used by
`tests/test_181_docstring_pr_cross_provider.py`.
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


def test_create_pr_docstring_documents_zero_diff_branch_divergence():
    doc = _normalize_ws(_pull_tools["create_pr"].__doc__ or "")
    assert "GitHub" in doc
    assert "GitLab" in doc
    assert "422" in doc
    assert "No commits between" in doc
    idx = doc.index("No commits between")
    window = doc[max(0, idx - 300): idx + 300]
    assert "head" in window
    assert "base" in window


def test_create_pr_docstring_advises_caller_side_zero_diff_precheck():
    doc = _normalize_ws(_pull_tools["create_pr"].__doc__ or "")
    assert "ahead of" in doc
    assert (
        "before calling" in doc
        or "confirm" in doc
        or "uniform" in doc
    )


_TICKET_PATTERN = re.compile(r"ticket\s+#|#253|ticket\s+253", re.IGNORECASE)


def test_create_pr_docstring_has_no_internal_ticket_references():
    doc = _pull_tools["create_pr"].__doc__ or ""
    assert not _TICKET_PATTERN.search(doc), (
        "internal ticket references found in create_pr docstring"
    )
