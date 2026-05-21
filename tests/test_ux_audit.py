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

from project_issues_plugin import config as cfg_mod
from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
from project_issues_plugin.providers.github import (
    GitHubProvider,
    _format_github_validation_errors,
)
from project_issues_plugin.tools import comments as comment_tools
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

    def fake_load_projects(cwd=None):
        return cfg_mod.LoadResult(
            projects=[project], state="ok", search_root="/tmp",
        )
    monkeypatch.setattr(cfg_mod, "load_projects", fake_load_projects)
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


def test_list_prs_has_prs_alias_key(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme")
    # Both keys present, pointing to the same list.
    assert "prs" in result
    assert "pull_requests" in result
    assert result["prs"] is result["pull_requests"]


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
    from project_issues_plugin.providers.github import GitHubError
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


def test_list_tickets_applied_limit_not_echoed_when_unchanged(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req):
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    result = tools["list_tickets"](project_id="acme", limit=20)
    assert "applied_limit" not in result


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
