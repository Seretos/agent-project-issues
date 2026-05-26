"""Tests for ticket #93 — schema constraints and description surface fixes.

Verifies that:
  - PL1: list_pipeline_runs addressing params describe the one-of constraint.
  - R4:  add_relation/remove_relation `kind` param names real relation kinds
         and references list_relation_kinds.
  - P6:  add_pr_review_comment path/line/commit_sha describe new-thread mode;
         in_reply_to describes reply mode.

Uses the same `_param_description` / `func_metadata` pattern as
tests/test_tool_schema_descriptions.py.
"""
from __future__ import annotations

from typing import Callable

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import relations as relation_tools


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


# ---------------------------------------------------------------------------
# PL1 — list_pipeline_runs addressing parameters carry one-of constraint
# ---------------------------------------------------------------------------


def test_list_pipeline_runs_branch_describes_one_of():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "branch")
    assert "one" in desc.lower(), f"Expected 'one' in description, got: {desc!r}"


def test_list_pipeline_runs_branch_mentions_sibling_params():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "branch")
    # Should mention at least one sibling addressing param.
    assert any(
        sibling in desc for sibling in ("tag", "commit_sha", "ticket_id")
    ), f"Expected sibling param reference in description, got: {desc!r}"


def test_list_pipeline_runs_tag_describes_one_of():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "tag")
    assert "one" in desc.lower(), f"Expected 'one' in description, got: {desc!r}"


def test_list_pipeline_runs_tag_mentions_sibling_params():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "tag")
    assert any(
        sibling in desc for sibling in ("branch", "commit_sha", "ticket_id")
    ), f"Expected sibling param reference in description, got: {desc!r}"


def test_list_pipeline_runs_commit_sha_describes_one_of():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "commit_sha")
    assert "one" in desc.lower(), f"Expected 'one' in description, got: {desc!r}"


def test_list_pipeline_runs_commit_sha_mentions_sibling_params():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "commit_sha")
    assert any(
        sibling in desc for sibling in ("branch", "tag", "ticket_id")
    ), f"Expected sibling param reference in description, got: {desc!r}"


def test_list_pipeline_runs_ticket_id_describes_one_of():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "ticket_id")
    assert "one" in desc.lower(), f"Expected 'one' in description, got: {desc!r}"


def test_list_pipeline_runs_ticket_id_mentions_sibling_params():
    tools = _register(pipeline_tools)
    desc = _param_description(tools["list_pipeline_runs"], "ticket_id")
    assert any(
        sibling in desc for sibling in ("branch", "tag", "commit_sha")
    ), f"Expected sibling param reference in description, got: {desc!r}"


# ---------------------------------------------------------------------------
# R4 — add_relation / remove_relation `kind` carries enum guidance
# ---------------------------------------------------------------------------


def test_add_relation_kind_mentions_parent():
    tools = _register(relation_tools)
    desc = _param_description(tools["add_relation"], "kind")
    assert "parent" in desc, f"Expected 'parent' in description, got: {desc!r}"


def test_add_relation_kind_mentions_list_relation_kinds():
    tools = _register(relation_tools)
    desc = _param_description(tools["add_relation"], "kind")
    assert "list_relation_kinds" in desc, (
        f"Expected 'list_relation_kinds' in description, got: {desc!r}"
    )


def test_remove_relation_kind_mentions_parent():
    tools = _register(relation_tools)
    desc = _param_description(tools["remove_relation"], "kind")
    assert "parent" in desc, f"Expected 'parent' in description, got: {desc!r}"


def test_remove_relation_kind_mentions_list_relation_kinds():
    tools = _register(relation_tools)
    desc = _param_description(tools["remove_relation"], "kind")
    assert "list_relation_kinds" in desc, (
        f"Expected 'list_relation_kinds' in description, got: {desc!r}"
    )


# ---------------------------------------------------------------------------
# P6 — add_pr_review_comment path/line/commit_sha describe new-thread mode;
#       in_reply_to describes reply mode
# ---------------------------------------------------------------------------


def test_add_pr_review_comment_path_mentions_new_thread():
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "path")
    assert "new" in desc.lower() or "thread" in desc.lower(), (
        f"Expected 'new' or 'thread' in description, got: {desc!r}"
    )


def test_add_pr_review_comment_line_mentions_new_thread():
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "line")
    assert "new" in desc.lower() or "thread" in desc.lower(), (
        f"Expected 'new' or 'thread' in description, got: {desc!r}"
    )


def test_add_pr_review_comment_commit_sha_mentions_new_thread():
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "commit_sha")
    assert "new" in desc.lower() or "thread" in desc.lower(), (
        f"Expected 'new' or 'thread' in description, got: {desc!r}"
    )


def test_add_pr_review_comment_in_reply_to_mentions_reply_mode():
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "in_reply_to")
    assert "reply" in desc.lower(), (
        f"Expected 'reply' in description, got: {desc!r}"
    )
