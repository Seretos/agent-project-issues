"""Tests for ticket #69 — tool parameter schema descriptions.

Verifies that each affected parameter has a Field description that lands
in the JSON schema emitted by func_metadata (the mechanism FastMCP uses
to build the published tool schema). Each assertion exercises one
required description keyword so that an accidental description removal
causes an immediate test failure.
"""
from __future__ import annotations

from typing import Callable

import pytest

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools import pulls as pull_tools
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
    """Return the description string for a single parameter, or '' if absent."""
    schema = func_metadata(fn).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get(param, {})
    return prop.get("description", "")


# ---------------------------------------------------------------------------
# comments.get_comment — ticket_id
# ---------------------------------------------------------------------------


def test_get_comment_ticket_id_description_mentions_gitlab():
    tools = _register(comment_tools)
    desc = _param_description(tools["get_comment"], "ticket_id")
    assert "GitLab" in desc, f"Expected 'GitLab' in description, got: {desc!r}"


def test_get_comment_ticket_id_description_mentions_azure():
    tools = _register(comment_tools)
    desc = _param_description(tools["get_comment"], "ticket_id")
    assert "Azure" in desc, f"Expected 'Azure' in description, got: {desc!r}"


# ---------------------------------------------------------------------------
# comments.update_comment — ticket_id
# ---------------------------------------------------------------------------


def test_update_comment_ticket_id_description_mentions_gitlab():
    tools = _register(comment_tools)
    desc = _param_description(tools["update_comment"], "ticket_id")
    assert "GitLab" in desc, f"Expected 'GitLab' in description, got: {desc!r}"


def test_update_comment_ticket_id_description_mentions_azure():
    tools = _register(comment_tools)
    desc = _param_description(tools["update_comment"], "ticket_id")
    assert "Azure" in desc, f"Expected 'Azure' in description, got: {desc!r}"


# ---------------------------------------------------------------------------
# comments.delete_comment — ticket_id
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
# comments — ticket_id description is unified (ticket #184): identical
# across get_comment/update_comment/delete_comment, and states the single
# "always pass it" rule rather than the old per-provider divergence framing.
# ---------------------------------------------------------------------------


def test_comment_tools_ticket_id_description_identical_across_tools():
    tools = _register(comment_tools)
    descs = {
        name: _param_description(tools[name], "ticket_id")
        for name in ("get_comment", "update_comment", "delete_comment")
    }
    assert descs["get_comment"] == descs["update_comment"] == descs["delete_comment"], (
        f"Expected byte-identical ticket_id descriptions, got: {descs!r}"
    )


def test_comment_tools_ticket_id_description_states_unified_rule():
    tools = _register(comment_tools)
    for name in ("get_comment", "update_comment", "delete_comment"):
        desc = _param_description(tools[name], "ticket_id")
        assert "Always" in desc, (
            f"Expected 'Always' in {name} ticket_id description, got: {desc!r}"
        )
        assert "optional for GitHub" not in desc, (
            f"Expected old divergence phrasing absent from {name} "
            f"ticket_id description, got: {desc!r}"
        )


# ---------------------------------------------------------------------------
# tickets.add_comment — body
# ---------------------------------------------------------------------------


def test_add_comment_body_description_mentions_ai_generated():
    tools = _register(ticket_tools)
    desc = _param_description(tools["add_comment"], "body")
    assert "ai-generated" in desc, (
        f"Expected 'ai-generated' in description, got: {desc!r}"
    )


# ---------------------------------------------------------------------------
# pulls.submit_pr_review — body
# ---------------------------------------------------------------------------


def test_submit_pr_review_body_description_mentions_request_changes():
    tools = _register(pull_tools)
    desc = _param_description(tools["submit_pr_review"], "body")
    assert "request_changes" in desc, (
        f"Expected 'request_changes' in description, got: {desc!r}"
    )


def test_submit_pr_review_body_description_mentions_required():
    tools = _register(pull_tools)
    desc = _param_description(tools["submit_pr_review"], "body")
    assert "required" in desc.lower(), (
        f"Expected 'required' (case-insensitive) in description, got: {desc!r}"
    )


# ---------------------------------------------------------------------------
# pulls.add_pr_review_comment — in_reply_to
# ---------------------------------------------------------------------------


def test_add_pr_review_comment_in_reply_to_description_is_opaque():
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "in_reply_to")
    assert "opaque" in desc.lower() or "verbatim" in desc.lower(), (
        f"Expected 'opaque' or 'verbatim' in description, got: {desc!r}"
    )


def test_add_pr_review_comment_in_reply_to_description_not_same_shape():
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "in_reply_to")
    assert "same shape" not in desc, (
        f"Expected 'same shape' to be absent from description, got: {desc!r}"
    )


# ---------------------------------------------------------------------------
# pipelines.get_pipeline_run — run_id
# ---------------------------------------------------------------------------


def test_get_pipeline_run_run_id_description_mentions_github():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["get_pipeline_run"], "run_id")
    assert "GitHub" in desc, f"Expected 'GitHub' in description, got: {desc!r}"


def test_get_pipeline_run_run_id_description_mentions_gitlab():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["get_pipeline_run"], "run_id")
    assert "GitLab" in desc, f"Expected 'GitLab' in description, got: {desc!r}"
