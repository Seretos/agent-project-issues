"""Regression tests for ticket #212: the `get_pr` / `create_pr` docstrings
mislabeled the speculative pre-merge `merge_commit_sha` (with
`mergeable: true`) population as an Azure DevOps-exclusive quirk, and
falsely contrasted it with GitHub "correctly" returning `mergeable: null`
pre-merge. A cross-provider E2E re-sweep confirmed GitHub exhibits the
identical speculative "test-merge-commit" preview shortly after
`create_pr`, with a preview sha that differs from the eventual
post-merge sha. This is a docstring-only fix; no behavior changed.

Follows the `_StubMCP` / module-level `register()` pattern and the
`_normalize_ws` whitespace helper from
`tests/test_181_docstring_pr_cross_provider.py`.
"""
from __future__ import annotations

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
# get_pr — speculative pre-merge preview note names BOTH providers, and the
# false GitHub contrast is gone.
# ---------------------------------------------------------------------------


def test_get_pr_docstring_documents_speculative_preview_as_cross_provider():
    doc = _normalize_ws(_pull_tools["get_pr"].__doc__ or "")
    idx = doc.lower().index("speculative")
    window = doc[idx: idx + 300]
    assert "GitHub" in window
    assert "Azure DevOps" in window


def test_get_pr_docstring_no_longer_falsely_contrasts_with_github():
    doc = _pull_tools["get_pr"].__doc__ or ""
    assert "correctly returns" not in doc


# ---------------------------------------------------------------------------
# create_pr — speculative pre-merge preview note names BOTH providers.
# This is the red-to-green case: today's docstring only names Azure DevOps.
# ---------------------------------------------------------------------------


def test_create_pr_docstring_documents_speculative_preview_as_cross_provider():
    doc = _normalize_ws(_pull_tools["create_pr"].__doc__ or "")
    assert "GitHub" in doc
    assert "Azure DevOps" in doc
    assert "merge_commit_sha" in doc
    assert "speculative" in doc or "pre-merge" in doc
    assert "merged" in doc and "status" in doc
