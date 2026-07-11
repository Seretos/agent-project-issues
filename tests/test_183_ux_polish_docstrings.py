"""Regression tests for ticket #183: three low-severity, non-functional UX
polish items from an E2E sweep (companion to #182, merged). All are
documentation/wording-only fixes — no behavior changes.

  1. `get_ticket`'s `custom_fields` docstring section didn't warn that the
     Azure DevOps payload is the entire raw work-item `fields` dict, and
     therefore carries identity/PII fields (author/changed-by display name,
     unique name/email, avatar URL) — not just custom/picklist fields.
  2. `list_pipeline_runs`'s `recent`-mode empty-result hint implied GitHub
     Actions specifically ("no CI workflows configured"); reworded to be
     provider-neutral while keeping the "CI"/"workflow" tokens the existing
     `tests/test_pipelines.py` assertion depends on.
  3. `add_comment`'s docstring didn't warn that the returned bare note id is
     not self-contained on GitLab/Azure DevOps — the caller must pass it
     together with `ticket_id`, or use the composite `"<iid>/<note_id>"`
     form, to later call `get_comment`/`update_comment`/`delete_comment`.

Follows the `_StubMCP` / module-level `register()` pattern used by
`tests/test_181_docstring_pr_cross_provider.py`.
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


def _normalize_ws(text: str) -> str:
    """Collapse all whitespace runs (including the newline + indentation
    between wrapped docstring lines) to a single space.

    CPython 3.13+ strips the common leading whitespace from multi-line
    docstrings at compile time (`__doc__` comes back dedented); older
    versions (e.g. the 3.12 CI pin) do not, so a wrapped docstring line
    is followed by `\\n` plus the source's indentation instead of a bare
    `\\n`. Comparing against whitespace-normalized text keeps these
    assertions correct on both.
    """
    return " ".join(text.split())


_ticket_tools = _register(ticket_tools)


# ---------------------------------------------------------------------------
# Item 1 — get_ticket's custom_fields section must warn about Azure DevOps
# identity/PII fields.
# ---------------------------------------------------------------------------


def test_get_ticket_docstring_warns_about_ado_identity_pii_fields():
    doc = _normalize_ws(_ticket_tools["get_ticket"].__doc__ or "")
    assert "custom_fields" in doc
    assert "Azure DevOps" in doc
    # Anchor on the custom_fields section (not the earlier
    # acceptance_criteria paragraph, which also mentions "Azure DevOps"),
    # then find the "Azure DevOps" bullet within it.
    section_idx = doc.index("custom_fields` (`dict")
    idx = doc.index("Azure DevOps", section_idx)
    window = doc[idx: idx + 600]
    assert "email" in window
    assert "avatar" in window
    assert "identity" in window or "display name" in window


# ---------------------------------------------------------------------------
# Item 3 — add_comment's docstring must explain the forward-reference id
# shape (ticket_id pairing or composite "<iid>/<note_id>" form).
# ---------------------------------------------------------------------------


def test_add_comment_docstring_documents_forward_reference_id_shape():
    doc = _normalize_ws(_ticket_tools["add_comment"].__doc__ or "")
    assert "ticket_id" in doc
    assert "get_comment" in doc
    assert "update_comment" in doc
    assert "delete_comment" in doc
    assert '"<iid>/<note_id>"' in doc or "<iid>/<note_id>" in doc


# ---------------------------------------------------------------------------
# Hygiene guard — mirrors test_tool_docstring_hygiene.py's pattern: none of
# the new prose may contain the literal string "ticket #" (case-insensitive).
# ---------------------------------------------------------------------------

_TICKET_PATTERN = re.compile(r"ticket\s+#", re.IGNORECASE)


def test_new_docstrings_contain_no_internal_ticket_references():
    violations: list[str] = []
    for name in ("get_ticket", "add_comment"):
        doc = _ticket_tools[name].__doc__ or ""
        if _TICKET_PATTERN.search(doc):
            violations.append(name)
    assert not violations, (
        f"internal ticket references found in docstrings: {violations}"
    )
