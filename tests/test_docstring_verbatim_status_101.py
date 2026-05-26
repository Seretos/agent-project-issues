"""Regression tests for ticket #101 — status strings must be passed verbatim.

list_ticket_statuses returns provider-literal status strings (e.g. Azure `"To Do"`,
GitHub `"open"`). Agents must not normalise casing or whitespace before passing them
to update_ticket.status / create_ticket.status. These tests guard that the verbatim
pass-through warning is present in all three relevant docstrings.
"""
from __future__ import annotations

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


def test_list_ticket_statuses_docstring_verbatim_warning():
    """list_ticket_statuses docstring must warn that values are provider-literal.

    Agents must be told that the strings in `values` carry exact casing and
    whitespace and must be passed verbatim without normalisation.
    """
    tools = _register(ticket_tools)
    doc = tools["list_ticket_statuses"].__doc__ or ""

    assert "verbatim" in doc, (
        "list_ticket_statuses docstring must contain 'verbatim' to warn agents "
        "not to normalise status strings before passing them to update_ticket / "
        "create_ticket"
    )
    assert "casing" in doc, (
        "list_ticket_statuses docstring must mention 'casing' to make the "
        "provider-literal contract explicit"
    )


def test_update_ticket_docstring_status_verbatim_warning():
    """update_ticket docstring must warn that status values must not be normalised.

    An agent that lowercases a status like "To Do" -> "to do" silently breaks on
    Azure DevOps. The docstring must carry an explicit normalisation warning.
    """
    tools = _register(ticket_tools)
    doc = tools["update_ticket"].__doc__ or ""

    assert "normalise" in doc or "normalize" in doc, (
        "update_ticket docstring must contain 'normalise'/'normalize' to warn "
        "agents not to alter status string casing or whitespace"
    )
    assert "casing" in doc, (
        "update_ticket docstring must mention 'casing' to make the verbatim "
        "pass-through contract explicit"
    )

    # Guard: the cross-reference to list_ticket_statuses must still be present
    assert "`list_ticket_statuses`" in doc, (
        "update_ticket docstring must still reference `list_ticket_statuses` "
        "so agents know where to discover valid status values"
    )


def test_create_ticket_docstring_status_verbatim_warning():
    """create_ticket docstring must warn that status values must not be normalised.

    Language must be parallel to the update_ticket warning so both write tools
    carry the same verbatim-pass-through contract.
    """
    tools = _register(ticket_tools)
    doc = tools["create_ticket"].__doc__ or ""

    assert "normalise" in doc or "normalize" in doc, (
        "create_ticket docstring must contain 'normalise'/'normalize' to warn "
        "agents not to alter status string casing or whitespace"
    )
    assert "casing" in doc or "whitespace" in doc, (
        "create_ticket docstring must mention 'casing' or 'whitespace' to make "
        "the verbatim pass-through contract explicit"
    )

    # Guard: the cross-reference to update_ticket.status must still be present
    assert "update_ticket.status" in doc, (
        "create_ticket docstring must still reference 'update_ticket.status' "
        "so agents know both write tools share the same status vocabulary"
    )
