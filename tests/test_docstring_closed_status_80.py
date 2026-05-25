"""Regression tests for ticket #80 — update_ticket docstring must not list bare `closed`.

The bare `closed` status value was incorrectly included as a valid GitHub status in the
update_ticket docstring. The valid set is `open`, `closed:completed`, `closed:not_planned`.
These tests guard against the invalid value reappearing.
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


def test_update_ticket_docstring_does_not_mention_bare_closed():
    """update_ticket docstring must not contain bare `closed` (without a colon suffix).

    The invalid value `closed` (bare) was once listed alongside the valid values
    `closed:completed` and `closed:not_planned`. This regression test ensures
    the bare form is absent while the valid colon-suffixed forms remain.
    """
    tools = _register(ticket_tools)
    doc = tools["update_ticket"].__doc__ or ""

    # The bare form with a comma: `` `closed`, `` — the invalid listing pattern
    assert "`closed`," not in doc, (
        "update_ticket docstring contains '`closed`,' — the bare 'closed' value "
        "is not a valid GitHub status and must not be listed"
    )

    # More broadly: no backtick-closed-backtick that is NOT followed by a colon
    # This catches `` `closed` `` at end of sentence or with other punctuation.
    bare_closed = re.search(r"`closed`(?!:)", doc)
    assert bare_closed is None, (
        f"update_ticket docstring contains bare '`closed`' at position "
        f"{bare_closed.start()}: {doc[max(0, bare_closed.start()-20):bare_closed.end()+20]!r}"
    )

    # Valid values must still be present
    assert "`closed:completed`" in doc, (
        "update_ticket docstring is missing valid status '`closed:completed`'"
    )
    assert "`closed:not_planned`" in doc, (
        "update_ticket docstring is missing valid status '`closed:not_planned`'"
    )


def test_update_ticket_docstring_retains_valid_github_statuses():
    """update_ticket docstring retains both valid closed statuses for GitHub."""
    tools = _register(ticket_tools)
    doc = tools["update_ticket"].__doc__ or ""

    assert "`closed:completed`" in doc, (
        "update_ticket docstring must list '`closed:completed`' as a valid GitHub status"
    )
    assert "`closed:not_planned`" in doc, (
        "update_ticket docstring must list '`closed:not_planned`' as a valid GitHub status"
    )
