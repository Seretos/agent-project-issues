"""Doc-sweep guards and behaviour tests for the tool-surface cleanup
covering GitHub issues #95, #96, #102, #103.

Two flavours:
  - Behavioural tests for the two code changes:
      * `update_comment` empty/whitespace body is rejected with the SAME
        wording `add_comment` uses (#102), before any HTTP call.
      * `create_label` / `update_label` validate the GitHub hex color and
        the label name in the wrapper, returning a plain documented rule
        instead of leaking GitHub's raw 422 field names (#102).
  - Docstring / schema guards that lock the clarity edits in place so a
    later refactor can't silently drop them.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import bulk as bulk_tools
from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import labels as label_tools
from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools import projects as project_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import relations as relation_tools
from project_issues_plugin.tools import tickets as ticket_tools


# ---------------------------------------------------------------------------
# Shared stubs (same pattern as the other tool tests)
# ---------------------------------------------------------------------------


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


def _github_project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={"issues": {"create": True, "modify": True}},
    )


def _register_with_project(monkeypatch, module) -> dict[str, Callable]:
    project = _github_project()

    def fake_load_projects(*_a, **_k):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    if hasattr(module, "load_projects"):
        monkeypatch.setattr(module, "load_projects", fake_load_projects)
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


def _install_github_mock(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def fake_client(token):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "test"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE, headers=headers, transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)


def _json_resp(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


# Tool tables that need no project context (docstring-only assertions).
_ticket_tools = _register(ticket_tools)
_pull_tools = _register(pull_tools)
_comment_tools = _register(comment_tools)
_label_tools = _register(label_tools)
_relation_tools = _register(relation_tools)
_pipeline_tools = _register(pipeline_tools)
_project_tools = _register(project_tools)
_bulk_tools = _register(bulk_tools)


# ===========================================================================
# #102 — code: update_comment empty-body wording matches add_comment
# ===========================================================================


def test_update_comment_empty_body_rejected_no_http(monkeypatch):
    """update_comment(body="") returns the add_comment wording, no HTTP call."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_with_project(monkeypatch, comment_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["update_comment"](project_id="acme", comment_id="5", body="")
    assert "error" in result
    assert result["error"] == "comment body must be non-empty"


def test_update_comment_whitespace_body_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_with_project(monkeypatch, comment_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["update_comment"](project_id="acme", comment_id="5", body="   ")
    assert "error" in result
    assert "non-empty" in result["error"]


def test_update_comment_wording_matches_add_comment(monkeypatch):
    """The two empty-body rejections must use identical wording (#102)."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    comment_t = _register_with_project(monkeypatch, comment_tools)
    ticket_t = _register_with_project(monkeypatch, ticket_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    add = ticket_t["add_comment"](project_id="acme", ticket_id="5", body="")
    upd = comment_t["update_comment"](project_id="acme", comment_id="5", body="")
    assert add["error"] == upd["error"] == "comment body must be non-empty"


# ===========================================================================
# #102 — code: label color / name validation in the wrapper
# ===========================================================================


def test_create_label_bad_github_color_rejected_no_http(monkeypatch):
    """A non-hex GitHub color is rejected locally with the documented rule."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_with_project(monkeypatch, label_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["create_label"](project_id="acme", name="bug", color="not-a-color")
    assert "error" in result
    # The documented rule, not a leaked GitHub field name.
    assert "6-digit hex" in result["error"]
    assert "ededed" in result["error"]
    assert "Label.color" not in result["error"]


def test_create_label_hash_prefixed_color_rejected(monkeypatch):
    """`#ededed` (GitLab form) is rejected on GitHub — it wants bare hex."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_with_project(monkeypatch, label_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["create_label"](project_id="acme", name="bug", color="#ededed")
    assert "error" in result
    assert "without '#'" in result["error"]


def test_create_label_empty_name_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_with_project(monkeypatch, label_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["create_label"](project_id="acme", name="   ")
    assert "error" in result
    assert "label name must be non-empty" in result["error"]


def test_create_label_valid_color_still_reaches_provider(monkeypatch):
    """Regression: a valid bare-hex color passes the guard and hits the API."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_with_project(monkeypatch, label_tools)

    def handler(req):
        assert req.method == "POST"
        return _json_resp({"name": "triage", "color": "e4e669", "description": ""})

    _install_github_mock(monkeypatch, handler)
    result = tools["create_label"](project_id="acme", name="triage", color="e4e669")
    assert "error" not in result, result
    assert result["label"]["color"] == "e4e669"


def test_update_label_bad_github_color_rejected_no_http(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_with_project(monkeypatch, label_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["update_label"](project_id="acme", name="bug", color="xyz123zzz")
    assert "error" in result
    assert "6-digit hex" in result["error"]


# ===========================================================================
# #95 / #103 — get_ticket relations `resolved` semantics (R3, R6)
# ===========================================================================


def test_get_ticket_docstring_defines_resolved_semantics():
    doc = _ticket_tools["get_ticket"].__doc__ or ""
    assert "resolved" in doc
    # Must clarify it is NOT issue-closed state and that empty != failure.
    assert "not fetched" in doc.lower() or "intentionally not fetched" in doc.lower()
    assert "fetch failed" in doc.lower()


# ===========================================================================
# #95 — list_ticket_statuses null hints (T6)
# ===========================================================================


def test_list_ticket_statuses_docstring_explains_null_hints():
    doc = _ticket_tools["list_ticket_statuses"].__doc__ or ""
    assert "terminal_declined" in doc
    assert "null" in doc.lower()


# ===========================================================================
# #95 / #102 — list_labels description "" sentinel (T7)
# ===========================================================================


def test_list_labels_docstring_documents_empty_description_sentinel():
    doc = _label_tools["list_labels"].__doc__ or ""
    assert "description" in doc
    assert "sentinel" in doc.lower() or "absent" in doc.lower()
    assert "Azure" in doc


# ===========================================================================
# #95 / #102 — list_prs closed includes merged (P3)
# ===========================================================================


def test_list_prs_docstring_notes_closed_includes_merged():
    doc = _pull_tools["list_prs"].__doc__ or ""
    assert "merged" in doc.lower()
    assert "closed" in doc.lower()


# ===========================================================================
# #95 — submit_pr_review commit_sha GitLab semantics (P7)
# ===========================================================================


def test_submit_pr_review_docstring_clarifies_gitlab_commit_sha():
    doc = _pull_tools["submit_pr_review"].__doc__ or ""
    assert "commit_sha: null" in doc or "commit_sha` always" in doc or (
        "GitLab" in doc and "null" in doc
    )
    # The old, potentially-misleading phrasing must be gone.
    assert "surface symmetry" not in doc


# ===========================================================================
# #102 — get_pr.mergeable null-after-create documented
# ===========================================================================


def test_get_pr_docstring_documents_mergeable_null_after_create():
    doc = _pull_tools["get_pr"].__doc__ or ""
    assert "mergeable" in doc
    assert "null" in doc.lower()
    assert "create_pr" in doc or "asynchronous" in doc.lower() or "computed" in doc.lower()


# ===========================================================================
# #102 — merge_pr return shape documented
# ===========================================================================


def test_merge_pr_docstring_documents_return_shape():
    doc = _pull_tools["merge_pr"].__doc__ or ""
    assert "pull_request" in doc
    assert "Returns" in doc


# ===========================================================================
# #102 — remove_relation return shape documented
# ===========================================================================


def test_remove_relation_docstring_documents_return_shape():
    doc = _relation_tools["remove_relation"].__doc__ or ""
    assert "removed" in doc
    assert "Returns" in doc


# ===========================================================================
# #96 / #103 — add_relation references list_relation_kinds (R7) + direction (R8)
# ===========================================================================


def test_add_relation_docstring_references_list_relation_kinds():
    doc = _relation_tools["add_relation"].__doc__ or ""
    assert "list_relation_kinds" in doc


def test_add_relation_docstring_clarifies_direction():
    doc = _relation_tools["add_relation"].__doc__ or ""
    # The from/to mapping for an asymmetric kind must be explicit.
    assert "blocks `target`" in doc or "blocks target" in doc
    assert "parent of `target`" in doc or "parent of target" in doc


# ===========================================================================
# #103 — list_relation_kinds documents global / not project-scoped
# ===========================================================================


def test_list_relation_kinds_docstring_notes_global():
    doc = _relation_tools["list_relation_kinds"].__doc__ or ""
    assert "global" in doc.lower()
    assert "not project-scoped" in doc.lower() or "no `project_id`" in doc.lower()


# ===========================================================================
# #103 — add_pr_comment cross-references add_pr_review_comment
# ===========================================================================


def test_add_pr_comment_docstring_references_review_comment():
    doc = _pull_tools["add_pr_comment"].__doc__ or ""
    assert "add_pr_review_comment" in doc


# ===========================================================================
# #96 / #103 — list_projects <-> find_projects boundary (D2, D3) and
# find_projects score semantics (D1)
# ===========================================================================


def test_list_projects_docstring_cross_references_find_projects():
    doc = _project_tools["list_projects"].__doc__ or ""
    assert "find_projects" in doc


def test_find_projects_docstring_cross_references_list_projects():
    doc = _project_tools["find_projects"].__doc__ or ""
    assert "list_projects" in doc


def test_find_projects_docstring_explains_score_bands():
    doc = _project_tools["find_projects"].__doc__ or ""
    assert "score" in doc.lower()
    # Must warn that fuzzy matching yields incidental matches / non-empty.
    assert "incidental" in doc.lower() or "sub-token" in doc.lower()


# ===========================================================================
# #96 — pipeline cross-documentation (PL8) + run_id quoting (#103)
# ===========================================================================


def test_list_pipeline_runs_docstring_references_get_pipeline_run():
    doc = _pipeline_tools["list_pipeline_runs"].__doc__ or ""
    assert "get_pipeline_run" in doc


def test_get_pipeline_run_docstring_references_list_pipeline_runs():
    doc = _pipeline_tools["get_pipeline_run"].__doc__ or ""
    assert "list_pipeline_runs" in doc


def test_get_pipeline_run_run_id_description_says_quote_it():
    desc = _param_description(_pipeline_tools["get_pipeline_run"], "run_id")
    assert "quoted" in desc.lower() or "never" in desc.lower()


# ===========================================================================
# #103 — update_label name vs new_name (schema + docstring)
# ===========================================================================


def test_update_label_name_description_clarifies_vs_new_name():
    desc = _param_description(_label_tools["update_label"], "name")
    assert "new_name" in desc
    assert "not changed" in desc.lower() or "look it up" in desc.lower()


# ===========================================================================
# #103 — list_tickets_across_projects has no aggregate limit
# ===========================================================================


def test_bulk_docstring_documents_no_aggregate_limit():
    doc = _bulk_tools["list_tickets_across_projects"].__doc__ or ""
    assert "aggregate" in doc.lower()
    assert "limit_per_project" in doc


# ===========================================================================
# #96 — list_tickets omit_nulls names the nullable-field situation (D6)
# ===========================================================================


def test_list_tickets_omit_nulls_docstring_names_fields():
    doc = _ticket_tools["list_tickets"].__doc__ or ""
    assert "omit_nulls" in doc
    # Should name where it actually helps (PR-style nullable fields).
    assert "mergeable" in doc or "no-op" in doc
