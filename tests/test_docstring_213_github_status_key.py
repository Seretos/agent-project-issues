"""Tests for #213 gap 2: the GitHub board-column write KEY ("Status")
must be discoverable from the tool surface for both `create_ticket`
and `update_ticket`, not just from `list_board_columns`.

Registers `tickets.py` against a stub MCP (same pattern as
`test_tool_docstring_hygiene.py` / `test_tool_schema_descriptions.py`)
and inspects the captured callables' `__doc__` and the `custom_fields`
parameter's `Field(description=...)` (via `__pydantic_fields__` /
the function's `Annotated` metadata) for the "Status" guidance.
"""
from __future__ import annotations

import re
from typing import Callable

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import tickets as ticket_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register() -> dict[str, Callable]:
    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


def _field_description(fn: Callable, param_name: str) -> str:
    """Return the JSON-schema description for one parameter of a tool."""
    schema = func_metadata(fn).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get(param_name, {})
    return prop.get("description", "")


_TICKET_HASH_PATTERN = re.compile(r"ticket\s+#", re.IGNORECASE)


def test_create_ticket_docstring_names_github_status_key() -> None:
    tools = _register()
    doc = tools["create_ticket"].__doc__ or ""

    assert "Status" in doc, "create_ticket docstring must name the GitHub 'Status' key"
    assert not _TICKET_HASH_PATTERN.search(doc), (
        "create_ticket docstring must not reintroduce an internal "
        "'ticket #N' reference"
    )


def test_update_ticket_docstring_names_github_status_key() -> None:
    tools = _register()
    doc = tools["update_ticket"].__doc__ or ""

    assert "Status" in doc, "update_ticket docstring must name the GitHub 'Status' key"
    assert not _TICKET_HASH_PATTERN.search(doc), (
        "update_ticket docstring must not reintroduce an internal "
        "'ticket #N' reference"
    )


def test_create_ticket_custom_fields_field_description_names_status() -> None:
    tools = _register()
    description = _field_description(tools["create_ticket"], "custom_fields")

    assert "Status" in description


def test_update_ticket_custom_fields_field_description_names_status() -> None:
    tools = _register()
    description = _field_description(tools["update_ticket"], "custom_fields")

    assert "Status" in description
