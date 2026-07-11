"""Regression tests for ticket #180: three MCP tool docstrings in this repo
contradicted observed live behavior.

  1. `get_ticket`'s relations section grouped `duplicate_of` /
     `duplicated_by` under "(GitHub + Azure DevOps)" only, but GitLab
     supports both kinds too (confirmed live and by
     `test_gitlab_add_relation_duplicate_of...` in
     `tests/test_relations_write.py`, and by `list_relation_kinds`'s
     dynamically-built `provider_support` matrix, which is driven by the
     lib's `GitLabProvider._SUPPORTED_RELATION_KINDS`).
  2. `list_custom_fields`'s docstring example used
     `work_item_type="Bug"`, but the sandbox Azure DevOps process
     template has no "Bug" work item type — following the doc's own
     example 404s.
  3. `create_label`'s docstring / `color` Field description claimed
     GitLab strictly requires `'#RRGGBB'`, but live testing shows
     bare-hex (`"ff00ff"`) is silently accepted and normalized to
     `"#ff00ff"` — not rejected.

Follows the `_StubMCP` / module-level `register()` pattern used by
`tests/test_docstring_94_conventions.py` and `tests/test_labels.py`.
"""
from __future__ import annotations

from typing import Callable

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import labels as label_tools
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


_ticket_tools = _register(ticket_tools)
_label_tools = _register(label_tools)
_relation_tools = _register(relation_tools)


def _param_description(fn: Callable, param: str) -> str:
    """Same helper as `tests/test_docstring_119_schema_clarity.py` /
    `tests/test_tool_schema_descriptions.py`: pull a parameter's
    `Field(description=...)` off the tool's generated JSON schema."""
    schema = func_metadata(fn).arg_model.model_json_schema()
    return schema.get("properties", {}).get(param, {}).get("description", "")


# ---------------------------------------------------------------------------
# Item 1 — get_ticket: duplicate_of / duplicated_by are GitHub + GitLab +
# Azure DevOps, not just "GitHub + Azure DevOps".
# ---------------------------------------------------------------------------


def test_get_ticket_docstring_does_not_confine_duplicate_of_to_github_and_ado():
    """Before the fix, the docstring grouped `duplicate_of`/`duplicated_by`
    together with `blocks`/`blocked_by` under a single trailing
    "(GitHub + Azure DevOps)" clause, implying GitLab lacks
    duplicate-of support. That's wrong — GitLab supports it."""
    doc = _ticket_tools["get_ticket"].__doc__ or ""

    old_wrong_clause = (
        "`duplicate_of`, `duplicated_by`, `mentions`,\n"
        "`mentioned_by`, `blocks`, `blocked_by` (GitHub + Azure DevOps)"
    )
    assert old_wrong_clause not in doc, (
        "get_ticket docstring still confines duplicate_of/duplicated_by to "
        "GitHub + Azure DevOps only"
    )


def test_get_ticket_docstring_states_duplicate_of_supported_on_gitlab():
    """The relations section must now say duplicate_of/duplicated_by are
    supported on GitHub + GitLab + Azure DevOps."""
    doc = _ticket_tools["get_ticket"].__doc__ or ""
    assert "duplicate_of" in doc
    assert "duplicated_by" in doc

    # Find the sentence/clause mentioning duplicate_of and confirm GitLab
    # is named in the same breath.
    idx = doc.index("`duplicate_of`")
    # Look at a window around the mention for the provider clause.
    window = doc[idx: idx + 200]
    assert "GitLab" in window, (
        f"expected GitLab to be named alongside duplicate_of, got: {window!r}"
    )
    assert "GitHub + GitLab + Azure DevOps" in doc or (
        "GitHub" in window and "GitLab" in window and "Azure DevOps" in window
    )


def test_get_ticket_docstring_leaves_blocks_grouping_untouched():
    """blocks/blocked_by must remain GitHub + Azure DevOps only (unchanged)."""
    doc = _ticket_tools["get_ticket"].__doc__ or ""
    assert "`blocks`,\n`blocked_by` (GitHub + Azure DevOps)" in doc


def test_get_ticket_docstring_leaves_relates_to_grouping_untouched():
    """relates_to must remain GitLab + Azure DevOps only (unchanged)."""
    doc = _ticket_tools["get_ticket"].__doc__ or ""
    assert "plus `relates_to`\n(GitLab + Azure DevOps)" in doc


def test_list_relation_kinds_provider_support_includes_gitlab_duplicate_of():
    """Drift guard: the dynamically-built provider_support matrix (driven
    by the lib's GitLabProvider._SUPPORTED_RELATION_KINDS) must actually
    list duplicate_of under gitlab — this is the ground truth the
    docstring above must match."""
    result = _relation_tools["list_relation_kinds"]()
    assert "duplicate_of" in result["provider_support"]["gitlab"], (
        f"gitlab provider_support missing duplicate_of: "
        f"{result['provider_support']['gitlab']}"
    )


# ---------------------------------------------------------------------------
# Item 2 — list_custom_fields: the work_item_type example must not be
# "Bug" (not present in the sandbox ADO process template), and the
# docstring should point agents at discovering valid types first.
# ---------------------------------------------------------------------------


def test_list_custom_fields_docstring_no_longer_uses_bug_example():
    doc = _ticket_tools["list_custom_fields"].__doc__ or ""
    assert "Bug" not in doc, (
        f"list_custom_fields docstring still references the 'Bug' work "
        f"item type, which doesn't exist in the sandbox ADO process "
        f"template: {doc!r}"
    )


def test_list_custom_fields_docstring_mentions_task_example():
    doc = _ticket_tools["list_custom_fields"].__doc__ or ""
    assert "Task" in doc


def test_list_custom_fields_docstring_notes_discovery_via_unscoped_call():
    doc = _ticket_tools["list_custom_fields"].__doc__ or ""
    assert "process template" in doc, (
        "docstring should note valid work_item_type values vary by "
        "process template"
    )
    assert "work_item_type=None" in doc, (
        "docstring should point agents at an unscoped call "
        "(work_item_type=None) to discover valid types first"
    )


# ---------------------------------------------------------------------------
# Item 3 — create_label: GitLab tolerates bare-hex and normalizes it to
# #RRGGBB, rather than strictly requiring the '#' prefix.
# ---------------------------------------------------------------------------


def test_create_label_docstring_mentions_gitlab_bare_hex_tolerance():
    doc = _label_tools["create_label"].__doc__ or ""
    assert "ff00ff" in doc, (
        "create_label docstring should give a bare-hex example GitLab "
        "accepts"
    )
    assert "normalized" in doc, (
        "create_label docstring should state GitLab normalizes bare-hex "
        "to #RRGGBB rather than rejecting it"
    )


def test_create_label_color_field_mentions_gitlab_bare_hex_tolerance():
    desc = _param_description(_label_tools["create_label"], "color")
    assert "ff00ff" in desc
    assert "normalized" in desc


def test_update_label_color_field_mentions_gitlab_bare_hex_tolerance():
    desc = _param_description(_label_tools["update_label"], "color")
    assert "ff00ff" in desc
    assert "normalized" in desc


# ---------------------------------------------------------------------------
# Constraint guard — existing literal-based tests
# (test_docstring_94_conventions.py T3, test_docstring_119_schema_clarity.py)
# assert on these literals; confirm they all survived the reword.
# ---------------------------------------------------------------------------


def test_literals_required_by_existing_tests_still_present():
    create_doc = _label_tools["create_label"].__doc__ or ""
    list_doc = _label_tools["list_labels"].__doc__ or ""
    create_color_desc = _param_description(_label_tools["create_label"], "color")
    update_color_desc = _param_description(_label_tools["update_label"], "color")

    assert "ededed" in create_doc
    assert "ededed" in list_doc
    assert "#RRGGBB" in create_doc or "#ff0000" in create_doc
    assert "ededed" in create_color_desc or "without '#'" in create_color_desc
    assert "#RRGGBB" in create_color_desc or "GitLab" in create_color_desc
    assert "ededed" in update_color_desc or "without '#'" in update_color_desc
