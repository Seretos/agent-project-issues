"""Guard test for ticket #223 — `add_relation`'s docstring put its `Returns:`
block last, after five verbose sections (direction, kind enum, target forms,
cross-project rejection, symmetry, provider-specific notes). ToolSearch
truncates the docstring before reaching it, hiding the return-shape info
callers need most. Locks in that the `Returns:` block (and its
`relation.ticket_id` / `fully hydrated` / `resolved` explanation) now appears
early, ahead of the low-priority provider/cross-project prose, while
verifying no information was lost in the move.
"""
from __future__ import annotations

from typing import Callable

from project_issues_plugin.tools import relations as relation_tools


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


_tools = _register(relation_tools)


def test_add_relation_docstring_returns_block_precedes_low_priority_prose():
    doc = _tools["add_relation"].__doc__ or ""
    assert doc.index("Returns:") < doc.index("on GitHub and GitLab"), repr(doc)


def test_add_relation_docstring_ticket_id_disambiguation_precedes_low_priority_prose():
    doc = _tools["add_relation"].__doc__ or ""
    assert doc.index("relation.ticket_id") < doc.index("on GitHub and GitLab"), repr(doc)


def test_add_relation_docstring_returns_block_precedes_symmetry_and_cross_project():
    doc = _tools["add_relation"].__doc__ or ""
    assert doc.index("Returns:") < doc.index("Symmetry:"), repr(doc)
    assert doc.index("Returns:") < doc.index("Cross-project references are rejected"), repr(doc)


def test_add_relation_docstring_no_information_lost_in_move():
    doc = _tools["add_relation"].__doc__ or ""
    for substring in (
        "Returns:",
        "relation.ticket_id",
        "fully hydrated",
        "same shape",
        "resolved",
        "get_ticket(ticket_id,",
        "include_relations=True).relations[]",
        "could not be normalised",
    ):
        assert substring in doc, repr(substring)
