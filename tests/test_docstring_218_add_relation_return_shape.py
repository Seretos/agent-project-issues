"""Guard test for ticket #218 finding 2 — `add_relation`'s docstring already
disambiguated `relation.ticket_id` (the target/other ticket) from the
request-side `ticket_id` parameter (#197), but did not state that the whole
returned `relation` object is itself an echo of this call's own target/kind
inputs, framed from the source ticket outward — not a
`get_ticket(...).relations[]` entry. Locks in the added clarification.
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


def test_add_relation_docstring_states_relation_mirrors_call_inputs():
    doc = _tools["add_relation"].__doc__ or ""
    assert "echo of this" in doc, repr(doc)
    assert "target" in doc and "kind" in doc


def test_add_relation_docstring_disclaims_get_ticket_relations_entry():
    doc = _tools["add_relation"].__doc__ or ""
    assert "get_ticket(...).relations[]" in doc, repr(doc)
    assert "NOT an entry" in doc or "not itself one of that list" in doc, repr(doc)
