"""Guard tests for ticket #119 — docstring & schema clarity.

Verifies every Field description and docstring prose change introduced in
the #119 pass, so a later refactor cannot silently drop the guidance.

Uses the same _StubMCP + func_metadata + _param_description pattern as
tests/test_tool_schema_descriptions.py.
"""
from __future__ import annotations

from typing import Callable

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import labels as label_tools
from project_issues_plugin.tools import projects as project_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import relations as relation_tools
from project_issues_plugin.tools import tickets as ticket_tools


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
    return schema.get("properties", {}).get(param, {}).get("description", "")


# Pre-register tool tables once at module load (no project context needed
# for schema/docstring assertions).
_ticket_tools = _register(ticket_tools)
_comment_tools = _register(comment_tools)
_pull_tools = _register(pull_tools)
_relation_tools = _register(relation_tools)
_label_tools = _register(label_tools)
_project_tools = _register(project_tools)


# ===========================================================================
# A1 — create_ticket.body Field
# ===========================================================================


def test_create_ticket_body_field_warns_real_newlines():
    desc = _param_description(_ticket_tools["create_ticket"], "body")
    assert "U+000A" in desc or "newline" in desc.lower(), repr(desc)


def test_create_ticket_body_field_warns_backslash_n():
    desc = _param_description(_ticket_tools["create_ticket"], "body")
    assert "\\n" in desc, repr(desc)


# ===========================================================================
# A2 — update_ticket.body Field
# ===========================================================================


def test_update_ticket_body_field_warns_real_newlines():
    desc = _param_description(_ticket_tools["update_ticket"], "body")
    assert "U+000A" in desc or "newline" in desc.lower(), repr(desc)


# ===========================================================================
# A3 — update_comment.body Field
# ===========================================================================


def test_update_comment_body_field_warns_no_ai_generated():
    desc = _param_description(_comment_tools["update_comment"], "body")
    assert "#ai-generated" in desc, repr(desc)


def test_update_comment_body_field_warns_real_newlines():
    desc = _param_description(_comment_tools["update_comment"], "body")
    assert "U+000A" in desc or "newline" in desc.lower(), repr(desc)


# ===========================================================================
# A4 — list_comments.body_max_chars Field
# ===========================================================================


def test_list_comments_body_max_chars_field_mentions_marker_overhead():
    desc = _param_description(_comment_tools["list_comments"], "body_max_chars")
    assert "~15" in desc or "marker" in desc.lower(), repr(desc)


# ===========================================================================
# A5 — list_prs.status Field
# ===========================================================================


def test_list_prs_status_field_mentions_merged():
    desc = _param_description(_pull_tools["list_prs"], "status")
    assert "merged" in desc.lower(), repr(desc)


# ===========================================================================
# A6 — submit_pr_review.state Field
# ===========================================================================


def test_submit_pr_review_state_field_says_lowercase():
    desc = _param_description(_pull_tools["submit_pr_review"], "state")
    assert "lowercase" in desc.lower(), repr(desc)


# ===========================================================================
# A7 — add_pr_review_comment.line Field
# ===========================================================================


def test_add_pr_review_comment_line_field_says_absolute():
    desc = _param_description(_pull_tools["add_pr_review_comment"], "line")
    assert "absolute" in desc.lower() or "1-based" in desc, repr(desc)


def test_add_pr_review_comment_line_field_not_diff_hunk():
    # The description must steer agents AWAY from the diff-hunk/diff-position
    # reading — it does so by explicitly negating it ("NOT a diff-hunk
    # position"), so assert that disclaimer is present rather than that the
    # word "hunk" is absent.
    desc = _param_description(_pull_tools["add_pr_review_comment"], "line").lower()
    assert "not a diff" in desc, repr(desc)


# ===========================================================================
# A8 — add_relation.target Field
# ===========================================================================


def test_add_relation_target_field_mentions_preferred_form():
    desc = _param_description(_relation_tools["add_relation"], "target")
    assert "#N" in desc or "preferred" in desc.lower(), repr(desc)


# ===========================================================================
# A9 — create_label.color and update_label.color Fields
# ===========================================================================


def test_create_label_color_field_mentions_github_bare_hex():
    desc = _param_description(_label_tools["create_label"], "color")
    assert "ededed" in desc or "without '#'" in desc, repr(desc)


def test_create_label_color_field_mentions_gitlab_format():
    desc = _param_description(_label_tools["create_label"], "color")
    assert "#RRGGBB" in desc or "GitLab" in desc, repr(desc)


def test_update_label_color_field_mentions_github_bare_hex():
    desc = _param_description(_label_tools["update_label"], "color")
    assert "ededed" in desc or "without '#'" in desc, repr(desc)


# ===========================================================================
# C1 — update_label rename name → current_name
# ===========================================================================


def test_update_label_current_name_param_exists_in_schema():
    desc = _param_description(_label_tools["update_label"], "name")
    assert desc != "", "name must have a Field description"


def test_update_label_name_param_gone_from_schema():
    desc = _param_description(_label_tools["update_label"], "current_name")
    assert desc == "", f"old 'current_name' param must not appear in schema; got: {desc!r}"


# ===========================================================================
# C2 — search_projects registered, find_projects gone
# ===========================================================================


def test_search_projects_tool_registered():
    assert "search_projects" in _project_tools, (
        "search_projects must be a registered tool"
    )


def test_find_projects_tool_gone():
    assert "find_projects" not in _project_tools, (
        "find_projects must be renamed to search_projects"
    )


# ===========================================================================
# B1 — list_tickets.status docstring cross-references update_ticket
# ===========================================================================


def test_list_tickets_status_docstring_cross_references_update_ticket():
    doc = _ticket_tools["list_tickets"].__doc__ or ""
    assert "update_ticket" in doc, repr(doc[:300])
    assert "open" in doc, repr(doc[:300])


# ===========================================================================
# B2 — add_comment docstring: warning is early, mentions strip + update_comment
# ===========================================================================


def test_add_comment_docstring_ai_generated_warning_is_early():
    doc = _ticket_tools["add_comment"].__doc__ or ""
    assert "#ai-generated" in doc[:400], (
        "#ai-generated warning must appear in first 400 chars"
    )
    assert "strip" in doc[:400].lower() or "do not" in doc[:400].lower() or "do NOT" in doc[:400], (
        "strip or do-not guidance must appear early"
    )


def test_add_comment_docstring_mentions_read_modify_write():
    doc = _ticket_tools["add_comment"].__doc__ or ""
    assert "strip" in doc.lower(), repr(doc[:400])
    assert "update_comment" in doc, repr(doc[:400])


# ===========================================================================
# B3 — update_comment docstring: warning is early, mentions strip
# ===========================================================================


def test_update_comment_docstring_ai_generated_warning_is_early():
    doc = _comment_tools["update_comment"].__doc__ or ""
    assert "#ai-generated" in doc[:300], (
        "#ai-generated warning must appear in first 300 chars"
    )


def test_update_comment_docstring_mentions_strip():
    doc = _comment_tools["update_comment"].__doc__ or ""
    assert "strip" in doc.lower(), repr(doc[:400])


# ===========================================================================
# B4 — create_pr docstring mentions body auto-prefix
# ===========================================================================


def test_create_pr_docstring_mentions_body_ai_prefix():
    doc = _pull_tools["create_pr"].__doc__ or ""
    assert "#ai-generated" in doc, repr(doc[:400])
    assert "body" in doc.lower(), repr(doc[:400])


# ===========================================================================
# C1 — update_label docstring contains current_name
# ===========================================================================


def test_update_label_current_name_in_docstring():
    doc = _label_tools["update_label"].__doc__ or ""
    assert "name" in doc, repr(doc[:400])
