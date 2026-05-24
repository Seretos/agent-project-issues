"""Tests for ticket #48 — remaining UX audit findings.

Findings 1, 2, 3, 7 already closed by #46 and #49. This file covers:
  - 4: update_ticket with no fields → error
  - 5: list_relation_kinds provider_support
  - 6: get_* 404 echoes id + project
  - 8: list_prs `prs` alias key
  - 9: update_pr(status="merged") rejected with merge_pr hint
  - 10: GitHub 422 errors formatted as one-line summary
  - 11: limit cap exposed via applied_limit echo
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.github import (
    GitHubProvider,
    _format_github_validation_errors,
)
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import relations as relation_tools
from project_issues_plugin.tools import tickets as ticket_tools


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={"issues": {"create": True, "modify": True},
                     "pulls": {"create": True, "modify": True, "merge": True}},
    )


def _resp(payload, status_code: int = 200):
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _install_github_mock(monkeypatch, handler):
    def wrapped(req):
        return handler(req)
    transport = httpx.MockTransport(wrapped)

    def fake_client(token):
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers={"Accept": "application/vnd.github+json"},
            transport=transport,
        )
    monkeypatch.setattr(github_provider, "_client", fake_client)


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register(monkeypatch, module):
    project = _project()

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp",
        )
    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    if hasattr(module, "load_projects"):
        monkeypatch.setattr(module, "load_projects", fake_load_projects)
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


# ---------- finding 4: update_ticket no-op rejection ------------------------


def test_update_ticket_empty_call_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    # No HTTP call should fire — the early return triggers first.
    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["update_ticket"](project_id="acme", ticket_id="5")
    assert "error" in result
    assert "no update fields" in result["error"]


def test_update_ticket_empty_lists_treated_as_no_change(monkeypatch):
    """labels_add=[] / assignees_add=[] etc. count as no-action too."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["update_ticket"](
        project_id="acme", ticket_id="5",
        labels_add=[], labels_remove=[],
        assignees_add=[], assignees_remove=[],
    )
    assert "error" in result


def test_update_ticket_with_actual_field_still_works(monkeypatch):
    """Sanity: passing a real field still hits the provider."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/issues/5"):
            return _resp({
                "number": 5, "title": "T", "body": "",
                "state": "open", "user": {"login": "a"},
                "assignees": [], "labels": [],
                "html_url": "https://github.com/acme/backend/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        if req.method == "PATCH":
            return _resp({
                "number": 5, "title": "new",
                "body": "#ai-modified",
                "state": "open", "user": {"login": "a"},
                "assignees": [], "labels": [],
                "html_url": "https://github.com/acme/backend/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
            })
        return _resp({}, 404)

    _install_github_mock(monkeypatch, handler)
    result = tools["update_ticket"](
        project_id="acme", ticket_id="5", title="new",
    )
    assert "error" not in result, result


# ---------- finding 5: list_relation_kinds provider_support -----------------


def test_list_relation_kinds_carries_provider_support(monkeypatch):
    tools = _register(monkeypatch, relation_tools)
    result = tools["list_relation_kinds"]()
    assert "kinds" in result
    assert "provider_support" in result
    assert "github" in result["provider_support"]
    assert "gitlab" in result["provider_support"]
    assert "blocks" in result["provider_support"]["github"]
    assert "relates_to" in result["provider_support"]["gitlab"]
    # GitHub does NOT support relates_to natively (per provider impl).
    assert "relates_to" not in result["provider_support"]["github"]


# ---------- finding 6: 404 echoes id + project ------------------------------


def test_get_ticket_404_echoes_id_and_project(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        return _resp({"message": "Not Found"}, status_code=404)

    _install_github_mock(monkeypatch, handler)
    result = tools["get_ticket"](
        project_id="acme", ticket_id="999999",
    )
    assert "error" in result
    assert "acme#999999" in result["error"]
    assert "GitHub 404" in result["error"]


def test_get_comment_404_echoes_id(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, comment_tools)

    def handler(req):
        return _resp({"message": "Not Found"}, status_code=404)

    _install_github_mock(monkeypatch, handler)
    result = tools["get_comment"](
        project_id="acme", ticket_id="5", comment_id="99999999",
    )
    assert "error" in result
    assert "99999999" in result["error"]
    assert "GitHub 404" in result["error"]


def test_get_pr_404_echoes_id(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools)

    def handler(req):
        return _resp({"message": "Not Found"}, status_code=404)

    _install_github_mock(monkeypatch, handler)
    result = tools["get_pr"](project_id="acme", pr_id="9999")
    assert "error" in result
    assert "acme#9999" in result["error"]
    assert "GitHub 404" in result["error"]


# ---------- finding 8: list_prs `prs` alias key -----------------------------


def test_list_prs_only_returns_prs_key(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme")
    # The legacy `pull_requests` alias key (ticket #48 finding 8) has
    # been removed — only the canonical `prs` key remains.
    assert "prs" in result
    assert "pull_requests" not in result


# ---------- finding 9: update_pr(status="merged") rejected with hint --------


def test_update_pr_status_merged_rejected_with_hint(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools)

    def handler(req):
        raise AssertionError("no HTTP call expected — guard fires first")

    _install_github_mock(monkeypatch, handler)
    result = tools["update_pr"](
        project_id="acme", pr_id="5", status="merged",
    )
    assert "error" in result
    assert "merge_pr" in result["error"]
    # Not a pydantic literal_error wall-of-text:
    assert "literal_error" not in result["error"]


# ---------- finding 10: GitHub 422 formatter --------------------------------


def test_format_github_validation_errors_compact_summary():
    errs = [
        {"resource": "Issue", "field": "assignees",
         "code": "invalid", "value": "ghost"},
        {"resource": "Issue", "field": "labels", "code": "missing"},
    ]
    out = _format_github_validation_errors(errs)
    assert "Issue.assignees='ghost' (invalid)" in out
    assert "Issue.labels (missing)" in out
    # No single-quoted-dict repr.
    assert "{'resource'" not in out


def test_format_github_validation_errors_uses_free_form_message():
    errs = [{"message": "human-readable explanation"}]
    out = _format_github_validation_errors(errs)
    assert "human-readable explanation" in out


def test_format_github_validation_errors_empty():
    assert _format_github_validation_errors([]) == "validation failed"


def test_github_422_error_uses_summary_formatter(monkeypatch):
    """End-to-end: a 422 from GitHub flows through _check and surfaces
    the compact summary in the GitHubError message."""
    def handler(req):
        return _resp({
            "message": "Validation Failed",
            "errors": [{
                "resource": "Issue",
                "field": "assignees",
                "code": "invalid",
                "value": "ghost",
            }],
        }, status_code=422)

    _install_github_mock(monkeypatch, handler)
    from lib_python_projects.providers.github import GitHubError
    with pytest.raises(GitHubError) as excinfo:
        # Trigger a write that goes through _check.
        GitHubProvider().update_ticket(
            _project(), "tok", "5", assignees_add=["ghost"],
        )
    msg = str(excinfo.value)
    assert "Issue.assignees='ghost' (invalid)" in msg


# ---------- finding 11: limit cap echoed ------------------------------------


def test_list_tickets_applied_limit_echoed_when_clamped(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_tickets"](project_id="acme", limit=9999)
    assert result.get("applied_limit") == 100


def test_list_tickets_applied_limit_always_present(monkeypatch):
    """applied_limit is always returned, even when no clamping occurs (ticket #62)."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_tickets"](project_id="acme", limit=20)
    assert result.get("applied_limit") == 20


def test_list_prs_applied_limit_echoed(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme", limit=500)
    assert result.get("applied_limit") == 100


def test_list_comments_applied_limit_echoed(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, comment_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_comments"](
        project_id="acme", ticket_id="5", limit=200,
    )
    assert result.get("applied_limit") == 100


def test_list_prs_applied_limit_always_present(monkeypatch):
    """applied_limit is always returned for list_prs, even when no clamping occurs (ticket #62)."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme", limit=15)
    assert result.get("applied_limit") == 15


def test_list_comments_applied_limit_always_present(monkeypatch):
    """applied_limit is always returned for list_comments, even when no clamping occurs (ticket #62)."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, comment_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_comments"](
        project_id="acme", ticket_id="5", limit=10,
    )
    assert result.get("applied_limit") == 10


# ---------- ticket #62: add_comment empty body rejection --------------------


def test_add_comment_empty_body_rejected(monkeypatch):
    """add_comment with body="" returns an error, not a silent AI-marker-only post."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["add_comment"](project_id="acme", ticket_id="5", body="")
    assert "error" in result
    assert "non-empty" in result["error"]


def test_add_comment_whitespace_body_rejected(monkeypatch):
    """add_comment with body of only whitespace is also rejected."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["add_comment"](project_id="acme", ticket_id="5", body="   ")
    assert "error" in result
    assert "non-empty" in result["error"]


def test_add_comment_valid_body_succeeds(monkeypatch):
    """Regression guard: a non-empty body still reaches the provider and succeeds."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        if req.method == "POST" and "/comments" in req.url.path:
            return _resp({
                "id": 42,
                "body": "#ai-generated\n\nThis is a real comment.",
                "user": {"login": "bot"},
                "html_url": "https://github.com/acme/backend/issues/5#issuecomment-42",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        raise AssertionError(f"unexpected HTTP call: {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["add_comment"](
        project_id="acme", ticket_id="5", body="This is a real comment.",
    )
    assert "error" not in result, result
    assert "comment" in result


# ---------- ticket #59: 404 echoes id+project for update/add_comment, pipeline run ----


def test_update_ticket_404_echoes_id_and_project(monkeypatch):
    """update_ticket 404 surfaces project#id context in the error message."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        return _resp({"message": "Not Found"}, status_code=404)

    _install_github_mock(monkeypatch, handler)
    result = tools["update_ticket"](
        project_id="acme", ticket_id="999999", title="new title",
    )
    assert "error" in result
    assert "acme#999999" in result["error"]
    assert "GitHub 404" in result["error"]


def test_add_comment_404_echoes_id_and_project(monkeypatch):
    """add_comment 404 surfaces project#id (ticket id) context in the error message."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        return _resp({"message": "Not Found"}, status_code=404)

    _install_github_mock(monkeypatch, handler)
    result = tools["add_comment"](
        project_id="acme", ticket_id="999999", body="some comment",
    )
    assert "error" in result
    assert "acme#999999" in result["error"]
    assert "GitHub 404" in result["error"]


def test_get_pipeline_run_404_echoes_run_id(monkeypatch):
    """get_pipeline_run 404 surfaces run_id context in the error message."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def fake_load_projects(*_args, **_kwargs):
        from lib_python_projects import ProjectsLoadResult
        return ProjectsLoadResult(
            projects=[_project()], state="ok", search_root="/tmp",
        )
    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)

    stub = _StubMCP()
    pipeline_tools.register(stub)
    tools = stub.tools

    def handler(req):
        return _resp({"message": "Not Found"}, status_code=404)

    _install_github_mock(monkeypatch, handler)
    result = tools["get_pipeline_run"](project_id="acme", run_id="99999999999")
    assert "error" in result
    assert "99999999999" in result["error"]
    assert "GitHub 404" in result["error"]


def test_get_comment_invalid_id_local_short_circuit(monkeypatch):
    """get_comment with a non-numeric comment_id short-circuits locally, no HTTP."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, comment_tools)

    def handler(req):
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_github_mock(monkeypatch, handler)
    result = tools["get_comment"](
        project_id="acme", comment_id="not-a-number", ticket_id="5",
    )
    assert "error" in result
