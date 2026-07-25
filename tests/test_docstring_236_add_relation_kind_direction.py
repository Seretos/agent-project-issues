"""Guard tests for ticket #236 — add_relation/remove_relation `kind`
parameter description direction clarity.

`add_relation`'s `kind` parameter description (the string most MCP clients
render in truncated tool-list views) listed the valid kinds but said
nothing about parent/child direction, so a user could invert the
relation. The full tool docstring already documents direction correctly;
this locks in that the parameter description itself is self-sufficient by
appending a direct direction rule using the existing "ticket_id VERB
target" framing.

`remove_relation` carries the same inversion risk and receives the
identical addition, so the two `kind` descriptions must stay
byte-identical.

Uses the same _StubMCP + func_metadata pattern as
tests/test_tool_schema_descriptions.py and
tests/test_docstring_197_relations_comments_clarity.py.
"""
from __future__ import annotations

from typing import Callable

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

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


def _param_description(fn: Callable, param: str) -> str:
    schema = func_metadata(fn).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get(param, {})
    return prop.get("description", "")


_relation_tools = _register(relation_tools)


def test_add_relation_kind_description_states_parent_direction():
    desc = _param_description(_relation_tools["add_relation"], "kind")
    assert "kind='parent' means ticket_id is the parent of target" in desc, repr(desc)


def test_add_relation_kind_description_states_child_direction():
    desc = _param_description(_relation_tools["add_relation"], "kind")
    assert "kind='child' means ticket_id is a child of target" in desc, repr(desc)


def test_add_relation_kind_description_does_not_state_contradictory_blanket_rule():
    desc = _param_description(_relation_tools["add_relation"], "kind")
    assert "ticket_id is the parent and target is the child" not in desc, repr(desc)


def test_add_relation_kind_description_preserves_kind_list():
    desc = _param_description(_relation_tools["add_relation"], "kind")
    assert "list_relation_kinds" in desc, repr(desc)
    for kind in ("parent", "child", "blocks", "blocked_by", "duplicate_of", "relates_to"):
        assert kind in desc, repr(desc)


def test_remove_relation_kind_description_states_parent_direction():
    desc = _param_description(_relation_tools["remove_relation"], "kind")
    assert "kind='parent' means ticket_id is the parent of target" in desc, repr(desc)


def test_remove_relation_kind_description_states_child_direction():
    desc = _param_description(_relation_tools["remove_relation"], "kind")
    assert "kind='child' means ticket_id is a child of target" in desc, repr(desc)


def test_remove_relation_kind_description_does_not_state_contradictory_blanket_rule():
    desc = _param_description(_relation_tools["remove_relation"], "kind")
    assert "ticket_id is the parent and target is the child" not in desc, repr(desc)


def test_add_and_remove_relation_kind_descriptions_identical():
    add_desc = _param_description(_relation_tools["add_relation"], "kind")
    remove_desc = _param_description(_relation_tools["remove_relation"], "kind")
    assert add_desc == remove_desc, (add_desc, remove_desc)
