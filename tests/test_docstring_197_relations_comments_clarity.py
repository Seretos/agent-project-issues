"""Guard tests for ticket #197 — relations/comments schema-doc clarity.

Locks in the three docstring clarifications from the #197 E2E sweep so a
later refactor cannot silently drop the guidance:

  1. `add_relation`'s "Provider-specific notes" no longer claims
     `duplicate_of` closes the source "on both providers" — it names GitHub
     and GitLab explicitly, staying silent on Azure DevOps (verified only in
     the external `lib-python-projects` lib).
  2. `add_relation`'s Returns block disambiguates the response-side
     `relation.ticket_id` (the target/other ticket) from the request-side
     `ticket_id` parameter (the source ticket).
  3. `get_ticket`'s relations-section docstring makes the same
     disambiguation for each relation's `ticket_id`.

Finding #3 from the sweep (`_TICKET_ID_DESCRIPTION` in
`tools/comments.py`) needed no change and has no guard test here.

Uses the same _StubMCP + func_metadata pattern as
tests/test_docstring_119_schema_clarity.py.
"""
from __future__ import annotations

from typing import Callable

from project_issues_plugin.tools import relations as relation_tools
from project_issues_plugin.tools import tickets as ticket_tools


class _StubMCP:
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


_relation_tools = _register(relation_tools)
_ticket_tools = _register(ticket_tools)


# ===========================================================================
# Finding #1 — add_relation duplicate_of provider scope
# ===========================================================================


def test_add_relation_docstring_names_github_and_gitlab_for_duplicate_of():
    doc = _relation_tools["add_relation"].__doc__ or ""
    assert "GitHub and GitLab" in doc, repr(doc)


def test_add_relation_docstring_no_longer_says_both_providers():
    doc = _relation_tools["add_relation"].__doc__ or ""
    assert "both providers" not in doc, repr(doc)


# ===========================================================================
# Finding #2 — add_relation Returns block ticket_id disambiguation
# ===========================================================================


def test_add_relation_docstring_disambiguates_response_ticket_id():
    doc = _relation_tools["add_relation"].__doc__ or ""
    assert "relation.ticket_id" in doc, repr(doc)
    assert "target/other" in doc or "target / other" in doc, repr(doc)
    assert "distinct from" in doc, repr(doc)


# ===========================================================================
# Finding #2 — get_ticket relations-section ticket_id disambiguation
# ===========================================================================


def test_get_ticket_docstring_disambiguates_relation_ticket_id():
    doc = _ticket_tools["get_ticket"].__doc__ or ""
    assert "other/linked" in doc or "other / linked" in doc, repr(doc)
    assert "distinct from this tool's own" in doc, repr(doc)
    assert "selects the ticket being queried" in doc, repr(doc)
