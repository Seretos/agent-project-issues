"""Guard tests for ticket #249 — update_label.name schema/behavior mismatch.

Before this fix, `update_label`'s `name` parameter was declared
`str | None = None`, so its published JSON schema marked `name` as
optional/nullable (`anyOf[string, null]`, `default: null`, absent from
`required`) — but the function body enforced it as required at runtime,
returning a soft `{"error": "name is required"}` dict instead of raising.
This mismatched the schema FastMCP publishes to agents.

The fix makes `name` a required, non-nullable `str` parameter (matching
`create_label`/`delete_label`'s existing pattern) and removes the now-dead
runtime guard. These tests guard the schema shape and the guard's removal.

Uses the same _StubMCP + func_metadata pattern as
tests/test_docstring_119_schema_clarity.py.
"""
from __future__ import annotations

import anyio
from pathlib import Path
from typing import Callable

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import labels as label_tools


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


_label_tools = _register(label_tools)


def _update_label_name_schema() -> dict:
    schema = func_metadata(_label_tools["update_label"]).arg_model.model_json_schema()
    return schema


def test_update_label_name_is_required_in_schema():
    schema = _update_label_name_schema()
    assert "name" in schema.get("required", []), (
        f"expected 'name' in required list; got {schema.get('required')!r}"
    )


def test_update_label_name_schema_is_non_nullable_string():
    schema = _update_label_name_schema()
    name_schema = schema["properties"]["name"]
    assert name_schema.get("type") == "string", repr(name_schema)
    assert "anyOf" not in name_schema, repr(name_schema)
    assert "default" not in name_schema, repr(name_schema)


def test_update_label_missing_name_runtime_guard_removed_from_source():
    labels_py = (
        Path(__file__).resolve().parent.parent
        / "src" / "project_issues_plugin" / "tools" / "labels.py"
    )
    src = labels_py.read_text(encoding="utf-8")
    assert "name is required" not in src, (
        "the dead runtime guard 'name is required' must be removed from "
        "labels.py now that `name` is enforced at the schema boundary"
    )


def test_update_label_missing_name_via_real_mcp_dispatch_returns_structured_error():
    """Drives `update_label` through the *real* MCP dispatch path — the
    low-level server's registered ``CallToolRequest`` handler, i.e. what
    actually runs when an MCP client sends a ``tools/call`` request — with
    ``name`` omitted from the arguments.

    Making `name` a required Python parameter (no default, no runtime
    ``if name is None`` guard) raises an exception when the argument is
    missing, rather than returning the old soft ``{"error": ...}`` dict.
    A review flagged this as a possible regression: a raw exception
    reaching the MCP client instead of a structured error result.

    This test settles that empirically instead of by argument. The
    low-level server's ``call_tool`` handler
    (``mcp/server/lowlevel/server.py``) wraps the entire
    ``func(tool_name, arguments)`` call — which is where pydantic
    argument binding/validation happens — in a blanket
    ``except Exception as e: return self._make_error_result(str(e))``.
    So a missing required argument becomes a structured
    ``CallToolResult(isError=True, ...)`` before it ever reaches the
    client, exactly like any other tool-level error. This is the same
    real dispatch path used for every other tool call; it is not
    special-cased for `update_label`.

    If this test fails (an exception propagates out of the dispatch
    call instead of a structured error result coming back), that is a
    genuine regression, not a false positive, and must be fixed rather
    than the test being adjusted to hide it.
    """
    mcp = FastMCP("test-update-label-dispatch")
    label_tools.register(mcp)

    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(
            name="update_label",
            arguments={"project_id": "acme"},  # `name` deliberately omitted
        ),
    )

    # If the handler let an exception propagate instead of catching it,
    # anyio.run would raise here and this test would error (not fail an
    # assertion) — that outcome would itself demonstrate the leak.
    result = anyio.run(handler, request)

    assert isinstance(result, types.ServerResult)
    call_result = result.root
    assert isinstance(call_result, types.CallToolResult), repr(call_result)
    assert call_result.isError is True, (
        "expected a structured isError=True CallToolResult for the missing "
        f"required 'name' argument via the real dispatch path; got: {call_result!r}"
    )
