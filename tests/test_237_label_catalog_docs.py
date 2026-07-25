"""Regression test for #237: `create_ticket`/`update_ticket` accept label
names, but on GitHub (and presumably GitLab) each name must already exist
in the repository's label catalog — passing a not-yet-existing label 404s.
`create_label`'s docstring already documents that Azure DevOps has no such
restriction (freeform tags, created on the fly), which invites an agent to
over-generalize the absence of a catalog to GitHub/GitLab too.

Not a bug — this is a docstring-only fix mirroring the existing Azure
freeform-tag note onto `create_ticket` (`labels`) and `update_ticket`
(`labels_add`), pointing the reader at `create_label` first.

Follows the `_StubMCP` / module-level `register()` / `_normalize_ws()`
pattern used by `tests/test_196_azure_docs.py` and
`tests/test_docstring_213_github_status_key.py`.
"""
from __future__ import annotations

import re
from typing import Callable

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


_ticket_tools = _register(ticket_tools)

_TICKET_PATTERN = re.compile(r"ticket\s+#", re.IGNORECASE)


def test_create_ticket_docstring_documents_label_catalog_requirement():
    doc = _normalize_ws(_ticket_tools["create_ticket"].__doc__ or "")
    assert "create_label" in doc
    assert "catalog" in doc
    assert "GitHub" in doc
    assert "presumably GitLab" in doc
    assert not _TICKET_PATTERN.search(doc)


def test_update_ticket_docstring_documents_label_catalog_requirement():
    doc = _normalize_ws(_ticket_tools["update_ticket"].__doc__ or "")
    assert "create_label" in doc
    assert "catalog" in doc
    assert "labels_add" in doc
    assert "GitHub" in doc
    assert "presumably GitLab" in doc
    assert not _TICKET_PATTERN.search(doc)


def test_create_ticket_still_returns_error_dict_for_unresolvable_project():
    """Docs-only change must not widen/alter behavior — an unresolvable
    project_id still surfaces as {"error": ...}, not a traceback.
    """
    fn = _ticket_tools["create_ticket"]
    result = fn(project_id="does-not-exist", title="x", labels=["new-label"])
    assert "error" in result


def test_update_ticket_still_returns_error_dict_for_unresolvable_project():
    fn = _ticket_tools["update_ticket"]
    result = fn(
        project_id="does-not-exist", ticket_id="1", labels_add=["new-label"],
    )
    assert "error" in result
