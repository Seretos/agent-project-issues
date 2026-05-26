"""Docstring-inspection tests for the ticket-lifecycle docstring gaps addressed
in the ticket-99 polish pass.

Pure docstring-assertion tests — no monkeypatch, no HTTP mocking.
Every assertion is a regression guard: it will fail on the old docstrings
and pass after the fix.
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


_tools = _register(ticket_tools)


# ---------------------------------------------------------------------------
# Item 1 — escape-sequence note on create_ticket and update_ticket
# ---------------------------------------------------------------------------


def test_create_ticket_docstring_body_escape_note():
    """create_ticket docstring must mention 'real newline' (escape-sequence note)."""
    doc = _tools["create_ticket"].__doc__ or ""
    assert "real newline" in doc, (
        "create_ticket docstring is missing the escape-sequence note about real newlines"
    )


def test_update_ticket_docstring_body_escape_note():
    """update_ticket docstring must mention 'real newline' (escape-sequence note)."""
    doc = _tools["update_ticket"].__doc__ or ""
    assert "real newline" in doc, (
        "update_ticket docstring is missing the escape-sequence note about real newlines"
    )


# ---------------------------------------------------------------------------
# Item 2 — #ai-generated body prefix note on create_ticket
# ---------------------------------------------------------------------------


def test_create_ticket_docstring_body_marker_note():
    """create_ticket docstring must mention '#ai-generated' body prefix behaviour."""
    doc = _tools["create_ticket"].__doc__ or ""
    assert "#ai-generated" in doc, (
        "create_ticket docstring is missing the '#ai-generated' body-prefix note"
    )


# ---------------------------------------------------------------------------
# Item 3 — asymmetry callout on update_ticket
# ---------------------------------------------------------------------------


def test_update_ticket_docstring_asymmetry_note():
    """update_ticket docstring must reference 'create_ticket' (asymmetry callout)."""
    doc = _tools["update_ticket"].__doc__ or ""
    assert "create_ticket" in doc, (
        "update_ticket docstring is missing the asymmetry callout referencing create_ticket"
    )


# ---------------------------------------------------------------------------
# Item 4 — confirmation-signal note on get_ticket
# ---------------------------------------------------------------------------


def test_get_ticket_docstring_fetched_false_confirmation_note():
    """get_ticket docstring must mention 'confirmation' for the *_fetched: false signal."""
    doc = _tools["get_ticket"].__doc__ or ""
    assert "confirmation" in doc, (
        "get_ticket docstring is missing the 'confirmation signal' note for *_fetched: false"
    )


# ---------------------------------------------------------------------------
# Hygiene guard — no 'ticket #N' pattern in any edited docstring
# ---------------------------------------------------------------------------

_TICKET_REF_PATTERN = re.compile(r"ticket\s+#\d", re.IGNORECASE)

_EDITED_TOOLS = ("create_ticket", "update_ticket", "get_ticket")


def test_no_new_ticket_refs_introduced():
    """None of the edited tool docstrings may contain 'ticket #N' references."""
    violations: list[str] = []
    for name in _EDITED_TOOLS:
        doc = _tools[name].__doc__ or ""
        for lineno, line in enumerate(doc.splitlines(), 1):
            if _TICKET_REF_PATTERN.search(line):
                violations.append(
                    f"{name} line {lineno}: {line.strip()!r}"
                )
    assert not violations, (
        "Internal ticket references found in edited tool docstrings:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
