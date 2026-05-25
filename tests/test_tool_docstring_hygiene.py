"""Regression test for ticket #70 — no internal ticket references in tool docstrings.

Verifies that no consumer-visible @mcp.tool() docstring contains a
parenthetical internal tracker reference like `(ticket #N)` or the bare
phrase `ticket #N`. These are meaningless noise to consumer agents.

The test registers every tool module against a stub MCP (same pattern as
`test_tool_schema_descriptions.py`), captures the registered callables,
and asserts that none of their __doc__ strings contain `ticket #`
(case-insensitive).
"""
from __future__ import annotations

import re
from typing import Callable

from project_issues_plugin.tools import bulk as bulk_tools
from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import labels as label_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import relations as relation_tools
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


_PATTERN = re.compile(r"ticket\s+#", re.IGNORECASE)

_ALL_MODULES = [
    ("tickets", ticket_tools),
    ("comments", comment_tools),
    ("bulk", bulk_tools),
    ("pulls", pull_tools),
    ("relations", relation_tools),
    ("labels", label_tools),
]


def test_no_ticket_refs_in_tool_docstrings():
    """No @mcp.tool() docstring should contain internal ticket references."""
    violations: list[str] = []
    for module_name, module in _ALL_MODULES:
        tools = _register(module)
        for tool_name, fn in tools.items():
            doc = fn.__doc__ or ""
            if _PATTERN.search(doc):
                # Find the offending lines for a helpful failure message
                for lineno, line in enumerate(doc.splitlines(), 1):
                    if _PATTERN.search(line):
                        violations.append(
                            f"{module_name}.{tool_name} line {lineno}: {line.strip()!r}"
                        )

    assert not violations, (
        "Internal ticket references found in @mcp.tool() docstrings "
        "(these leak to consumer agents):\n"
        + "\n".join(f"  {v}" for v in violations)
    )
