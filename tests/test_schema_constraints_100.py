"""Tests for ticket #100 — schema call-constraint descriptions.

Verifies that:
  - update_pr.status carries enum guidance in its Field description.
  - submit_pr_review.state carries enum guidance in its Field description.
  - delete_comment.ticket_id carries per-provider semantics in its description.
  - list_pipeline_runs.recent carries one-of constraint in its description.

Uses the same `_StubMCP` / `func_metadata(fn).arg_model.model_json_schema()` /
`_param_description` pattern as tests/test_schema_constraints_93.py.
"""
from __future__ import annotations

from typing import Callable

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools import pulls as pull_tools


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
    """Return the Field description for a parameter, or '' if absent."""
    schema = func_metadata(fn).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get(param, {})
    return prop.get("description", "")


def _param_schema_prop(fn: Callable, param: str) -> dict:
    """Return the raw JSON schema property dict for a parameter."""
    schema = func_metadata(fn).arg_model.model_json_schema()
    props = schema.get("properties", {})
    prop = props.get(param, {})
    # Handle anyOf / allOf wrappers that Pydantic may emit for Optional types.
    if "anyOf" in prop:
        # Return the merged view — for enum check, look at each sub-schema.
        return prop
    return prop


# ---------------------------------------------------------------------------
# Group 1 — update_pr.status carries enum in description
# ---------------------------------------------------------------------------


def test_update_pr_status_description_mentions_open():
    tools = _register(pull_tools)
    desc = _param_description(tools["update_pr"], "status")
    assert "open" in desc, f"Expected 'open' in description, got: {desc!r}"


def test_update_pr_status_description_mentions_closed():
    tools = _register(pull_tools)
    desc = _param_description(tools["update_pr"], "status")
    assert "closed" in desc, f"Expected 'closed' in description, got: {desc!r}"


def test_update_pr_status_schema_type_is_not_literal_enum():
    tools = _register(pull_tools)
    schema = func_metadata(tools["update_pr"]).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get("status", {})
    # Check neither the top-level nor any anyOf sub-schema contains "enum".
    assert "enum" not in prop, (
        f"Expected no 'enum' key in status schema property, got: {prop!r}"
    )
    for sub in prop.get("anyOf", []):
        assert "enum" not in sub, (
            f"Expected no 'enum' key in status anyOf sub-schema, got: {sub!r}"
        )


# ---------------------------------------------------------------------------
# Group 2 — submit_pr_review.state carries enum in description
# ---------------------------------------------------------------------------


def test_submit_pr_review_state_description_mentions_approve():
    tools = _register(pull_tools)
    desc = _param_description(tools["submit_pr_review"], "state")
    assert "approve" in desc, f"Expected 'approve' in description, got: {desc!r}"


def test_submit_pr_review_state_description_mentions_request_changes():
    tools = _register(pull_tools)
    desc = _param_description(tools["submit_pr_review"], "state")
    assert "request_changes" in desc, (
        f"Expected 'request_changes' in description, got: {desc!r}"
    )


def test_submit_pr_review_state_description_mentions_comment_state():
    tools = _register(pull_tools)
    desc = _param_description(tools["submit_pr_review"], "state")
    assert "comment" in desc, f"Expected 'comment' in description, got: {desc!r}"


def test_submit_pr_review_state_description_mentions_body_required():
    tools = _register(pull_tools)
    desc = _param_description(tools["submit_pr_review"], "state")
    assert "body" in desc.lower() or "required" in desc.lower(), (
        f"Expected 'body' or 'required' (case-insensitive) in description, got: {desc!r}"
    )


def test_submit_pr_review_state_schema_type_is_not_literal_enum():
    tools = _register(pull_tools)
    schema = func_metadata(tools["submit_pr_review"]).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get("state", {})
    assert "enum" not in prop, (
        f"Expected no 'enum' key in state schema property, got: {prop!r}"
    )
    for sub in prop.get("anyOf", []):
        assert "enum" not in sub, (
            f"Expected no 'enum' key in state anyOf sub-schema, got: {sub!r}"
        )


# ---------------------------------------------------------------------------
# Group 3 — delete_comment.ticket_id carries per-provider semantics
# ---------------------------------------------------------------------------


def test_delete_comment_ticket_id_description_mentions_gitlab():
    tools = _register(comment_tools)
    desc = _param_description(tools["delete_comment"], "ticket_id")
    assert "GitLab" in desc, f"Expected 'GitLab' in description, got: {desc!r}"


def test_delete_comment_ticket_id_description_mentions_azure():
    tools = _register(comment_tools)
    desc = _param_description(tools["delete_comment"], "ticket_id")
    assert "Azure" in desc, f"Expected 'Azure' in description, got: {desc!r}"


# ---------------------------------------------------------------------------
# Group 4 — list_pipeline_runs.recent carries one-of constraint
# ---------------------------------------------------------------------------


def test_list_pipeline_runs_recent_describes_one_of():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "recent")
    assert "one" in desc.lower(), f"Expected 'one' in description, got: {desc!r}"


def test_list_pipeline_runs_recent_mentions_sibling_params():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "recent")
    assert any(
        sibling in desc for sibling in ("branch", "tag", "commit_sha", "ticket_id")
    ), f"Expected sibling param reference in description, got: {desc!r}"
