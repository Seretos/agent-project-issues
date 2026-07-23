"""Regression tests for ticket agent-project-issues#194 — board feature
documentation/discoverability gaps.

`lib-python-projects@v0.3.0` (pinned in pyproject.toml) added `custom_fields`
support to `GitHubProvider.update_ticket` (symmetric with `create_ticket`).
This repo's dynamic capability detection (`inspect.signature`) already
handled that correctly at runtime — these tests guard the previously-stale
static docs/error string that still claimed GitHub was unsupported, plus the
newly added cross-references, cascade warning, and short-description
discoverability improvements.

Follows the `_StubMCP` + module-level `register()` pattern and the
`func_metadata(...).arg_model.model_json_schema()` approach used by
`tests/test_tool_schema_descriptions.py` and
`tests/test_183_ux_polish_docstrings.py`.
"""
from __future__ import annotations

from typing import Callable

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import bulk as bulk_tools
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


def _param_description(fn: Callable, param: str) -> str:
    schema = func_metadata(fn).arg_model.model_json_schema()
    return schema.get("properties", {}).get(param, {}).get("description", "")


def _summary(doc: str) -> str:
    """Return the first paragraph (up to the first blank line) of a
    docstring — the "short description" region an agent sees when just
    scanning tool names/summaries, not the full detailed body."""
    doc = doc or ""
    return doc.split("\n\n", 1)[0]


_ticket_tools = _register(ticket_tools)
_bulk_tools = _register(bulk_tools)


# ---------------------------------------------------------------------------
# Item 1 — update_ticket's custom_fields docs no longer falsely claim GitHub
# is unsupported, and cross-reference create_ticket.
# ---------------------------------------------------------------------------


def test_update_ticket_custom_fields_description_no_longer_claims_azure_only():
    desc = _param_description(_ticket_tools["update_ticket"], "custom_fields")
    assert "Only supported by Azure DevOps" not in desc, (
        f"stale claim still present in description: {desc!r}"
    )


def test_update_ticket_custom_fields_description_mentions_github_board_support():
    desc = _param_description(_ticket_tools["update_ticket"], "custom_fields")
    assert "GitHub" in desc
    assert "board" in desc


def test_update_ticket_custom_fields_description_still_mentions_azure():
    """Guards the pre-existing test_custom_fields_param_description_mentions_azure
    assertion — Azure DevOps semantics must remain documented."""
    desc = _param_description(_ticket_tools["update_ticket"], "custom_fields")
    assert "Azure" in desc


def test_update_ticket_docstring_cross_references_create_ticket():
    doc = _ticket_tools["update_ticket"].__doc__ or ""
    assert "create_ticket" in doc


# ---------------------------------------------------------------------------
# create_ticket's custom_fields references update_ticket sharing the same
# board-write semantics (mutual cross-reference).
# ---------------------------------------------------------------------------


def test_create_ticket_custom_fields_description_references_update_ticket():
    desc = _param_description(_ticket_tools["create_ticket"], "custom_fields")
    assert "update_ticket" in desc


def test_create_ticket_docstring_references_update_ticket():
    doc = _ticket_tools["create_ticket"].__doc__ or ""
    assert "update_ticket" in doc


# ---------------------------------------------------------------------------
# Item 2 — cascade warning: writing a GitHub board column can auto-trigger a
# status change via provider-side workflow automation.
# ---------------------------------------------------------------------------


def test_create_ticket_custom_fields_warns_about_status_cascade():
    desc = _param_description(_ticket_tools["create_ticket"], "custom_fields")
    doc = _ticket_tools["create_ticket"].__doc__ or ""
    combined = desc + " " + doc
    assert "GitHub" in combined
    assert "column" in combined
    assert "close" in combined.lower() or "status" in combined.lower()


def test_update_ticket_custom_fields_warns_about_status_cascade():
    desc = _param_description(_ticket_tools["update_ticket"], "custom_fields")
    doc = _ticket_tools["update_ticket"].__doc__ or ""
    combined = desc + " " + doc
    assert "GitHub" in combined
    assert "column" in combined
    assert "close" in combined.lower() or "status" in combined.lower()


# ---------------------------------------------------------------------------
# agent-project-issues#226 — the cascade note must go further than generic
# "confirm the resulting status" advice: it must explicitly warn that this
# call's OWN returned status/updated_at can still be pre-cascade/stale, and
# push the caller to re-`get_ticket` for guaranteed-fresh state.
# ---------------------------------------------------------------------------


def test_create_ticket_cascade_note_warns_own_response_may_be_stale():
    desc = _param_description(_ticket_tools["create_ticket"], "custom_fields")
    doc = _ticket_tools["create_ticket"].__doc__ or ""
    combined = desc + " " + doc
    assert "stale" in combined.lower() or "pre-cascade" in combined.lower()
    assert "get_ticket" in combined
    assert "guaranteed" in combined.lower()


def test_update_ticket_cascade_note_warns_own_response_may_be_stale():
    desc = _param_description(_ticket_tools["update_ticket"], "custom_fields")
    doc = _ticket_tools["update_ticket"].__doc__ or ""
    combined = desc + " " + doc
    assert "stale" in combined.lower() or "pre-cascade" in combined.lower()
    assert "get_ticket" in combined
    assert "guaranteed" in combined.lower()


# ---------------------------------------------------------------------------
# Item 3 — no code change expected here; guard this repo's own `column`
# docs (list_tickets / list_tickets_across_projects) already mention Azure
# DevOps/Azure Boards, so they never regress toward the lib's
# GitHub-only-sounding wording (lib-side ticket filed separately for the
# lib's own GitLab error string).
# ---------------------------------------------------------------------------


def test_list_tickets_column_description_mentions_azure_boards():
    # `column` has no Annotated Field(...) of its own — its detailed docs live
    # in the docstring prose (the `- `column`: ...` bullet), not the JSON
    # schema description. Anchor on that bullet specifically.
    doc = _ticket_tools["list_tickets"].__doc__ or ""
    idx = doc.index("`column`: filter by logical board column")
    window = doc[idx: idx + 500]
    assert "Azure" in window
    assert "Boards" in window or "DevOps" in window


def test_list_tickets_across_projects_column_description_mentions_azure():
    doc = _bulk_tools["list_tickets_across_projects"].__doc__ or ""
    assert "column" in doc
    assert "list_tickets" in doc


# ---------------------------------------------------------------------------
# Item 4 — short-description discoverability: the first line/summary of
# list_tickets and list_tickets_across_projects mentions column/board
# filtering, not just the detailed column param doc.
# ---------------------------------------------------------------------------


def test_list_tickets_summary_mentions_column_filtering():
    summary = _summary(_ticket_tools["list_tickets"].__doc__ or "")
    assert "column" in summary, f"summary region: {summary!r}"


def test_list_tickets_across_projects_summary_mentions_column_filtering():
    summary = _summary(_bulk_tools["list_tickets_across_projects"].__doc__ or "")
    assert "column" in summary, f"summary region: {summary!r}"


# ---------------------------------------------------------------------------
# ticket agent-project-issues#230 — `ensure_board_column` must be documented
# in both its own docstring and README (permissions table, bullet list, and
# a note in the "Board columns (optional)" section).
# ---------------------------------------------------------------------------


def test_ensure_board_column_docstring_mentions_board_manage():
    doc = _ticket_tools["ensure_board_column"].__doc__ or ""
    assert "board.manage" in doc


def test_ensure_board_column_docstring_mentions_idempotent_create():
    doc = _ticket_tools["ensure_board_column"].__doc__ or ""
    assert "idempotent" in doc.lower() or "no-op" in doc.lower()


def test_ensure_board_column_docstring_mentions_provider_support():
    doc = _ticket_tools["ensure_board_column"].__doc__ or ""
    assert "GitHub" in doc
    assert "Azure" in doc
    assert "GitLab" in doc


def _readme_text() -> str:
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    return (repo_root / "README.md").read_text(encoding="utf-8")


def test_readme_permissions_table_documents_board_manage():
    readme = _readme_text()
    assert "board.manage" in readme
    assert "ensure_board_column" in readme


def test_readme_board_columns_section_documents_ensure_board_column():
    readme = _readme_text()
    idx = readme.index("#### Board columns (optional)")
    section = readme[idx: idx + 4000]
    assert "ensure_board_column" in section
    assert "board.manage" in section


# ---------------------------------------------------------------------------
# Hygiene guard — mirrors test_tool_docstring_hygiene.py: none of the new
# prose introduced here may contain the literal substring "ticket #".
# ---------------------------------------------------------------------------


def test_new_prose_contains_no_internal_ticket_references():
    texts = {
        "update_ticket.__doc__": _ticket_tools["update_ticket"].__doc__ or "",
        "update_ticket.custom_fields": _param_description(
            _ticket_tools["update_ticket"], "custom_fields"
        ),
        "create_ticket.__doc__": _ticket_tools["create_ticket"].__doc__ or "",
        "create_ticket.custom_fields": _param_description(
            _ticket_tools["create_ticket"], "custom_fields"
        ),
        "list_tickets.__doc__": _ticket_tools["list_tickets"].__doc__ or "",
        "list_tickets_across_projects.__doc__": (
            _bulk_tools["list_tickets_across_projects"].__doc__ or ""
        ),
        "ensure_board_column.__doc__": _ticket_tools["ensure_board_column"].__doc__ or "",
    }
    violations = [name for name, text in texts.items() if "ticket #" in text.lower()]
    assert not violations, f"internal ticket references found in: {violations}"
