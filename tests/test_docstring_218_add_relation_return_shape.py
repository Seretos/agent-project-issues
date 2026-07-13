"""Guard test for ticket #218 point b — `add_relation`'s docstring previously
claimed the returned `relation` object was an "echo of this call's own
target side... NOT an entry out of get_ticket(...).relations[]". That claim
was disproved on retest 2026-07-13: the returned object is in fact a fully
hydrated relation, structurally identical to a
`get_ticket(...).relations[]` entry. Locks in the corrected docstring and
guards against the disproved wording creeping back in.
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


def test_add_relation_docstring_does_not_claim_echo_or_disclaim_get_ticket_entry():
    doc = _tools["add_relation"].__doc__ or ""
    assert "echo of this" not in doc, repr(doc)
    assert "NOT an entry out of get_ticket" not in doc, repr(doc)


def test_add_relation_docstring_states_relation_matches_get_ticket_shape():
    doc = _tools["add_relation"].__doc__ or ""
    assert "fully hydrated" in doc, repr(doc)
    assert "same shape" in doc, repr(doc)
    assert "get_ticket(ticket_id," in doc and "include_relations=True).relations[]" in doc, repr(doc)


def test_add_relation_docstring_preserves_resolved_and_ticket_id_semantics():
    doc = _tools["add_relation"].__doc__ or ""
    assert "target/other" in doc
    assert "`resolved` is `true`" in doc or "resolved` documents how the relation metadata was obtained" in doc
