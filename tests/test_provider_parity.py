"""Tests for ticket #49 — GitHub vs GitLab provider parity fixes.

Covers the 11 findings beyond what existing tests already exercise:
status vocab + pipeline status kwarg + url canonicalisation + sigil +
atomic add_relation + timestamp normalisation + label sort.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
from project_issues_plugin.providers import gitlab as gitlab_provider
from project_issues_plugin.providers.base import normalize_timestamp
from project_issues_plugin.providers.github import GitHubProvider
from project_issues_plugin.providers.gitlab import GitLabProvider, _canonical_url


def _github_project(path: str = "Seretos/agent-project-issues") -> ProjectConfig:
    return ProjectConfig(id="github-tests", provider="github", path=path)


def _gitlab_project(path: str = "Seredos/gitlab-tests") -> ProjectConfig:
    return ProjectConfig(id="gitlab-tests", provider="gitlab", path=path)


def _resp(payload, status_code: int = 200, headers: dict | None = None):
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_gitlab_mock(monkeypatch, handler):
    def wrapped(req):
        return handler(req)
    transport = httpx.MockTransport(wrapped)

    def fake_client(project, token):
        return httpx.Client(
            base_url=f"{(project.base_url or 'https://gitlab.com').rstrip('/')}/api/v4",
            headers={"Accept": "application/json"},
            transport=transport,
        )
    monkeypatch.setattr(gitlab_provider, "_client", fake_client)


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


# ---------- finding 1: GitLab pipeline status kwarg + tuple return ----------


def test_gitlab_list_runs_for_branch_accepts_status_kwarg(monkeypatch):
    """Was a TypeError crash — see ticket #49 finding 1. Now `status`
    is accepted and maps to GitLab's `scope` param."""
    captured: dict = {}

    def handler(req):
        captured["scope"] = req.url.params.get("scope", "")
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    GitLabProvider().list_runs_for_branch(
        _gitlab_project(), "t", "main", status="completed",
    )
    assert captured["scope"] == "finished"


def test_gitlab_list_runs_for_branch_status_all_omits_scope(monkeypatch):
    captured: dict = {}

    def handler(req):
        captured["scope"] = req.url.params.get("scope", None)
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    GitLabProvider().list_runs_for_branch(
        _gitlab_project(), "t", "main", status="all",
    )
    # `all` maps to None → no scope query param at all.
    assert captured["scope"] in (None, "")


def test_gitlab_list_runs_for_ticket_returns_tuple(monkeypatch):
    """Was `list[PipelineRun]`, now `(runs, resolved_refs)` to mirror GitHub."""
    def handler(req):
        if req.url.path.endswith("/related_merge_requests"):
            return _resp([{"iid": 7}])
        if "/merge_requests/7/pipelines" in req.url.path:
            return _resp([])
        return _resp([])

    _install_gitlab_mock(monkeypatch, handler)
    runs, refs = GitLabProvider().list_runs_for_ticket(
        _gitlab_project(), "t", "5", status="completed",
    )
    assert runs == []
    assert refs == ["!7"]


# ---------- finding 3 + 4: GitLab URL canonicalisation ----------------------


def test_canonical_url_lowercases_project_path():
    p = _gitlab_project(path="Seredos/gitlab-tests")
    out = _canonical_url(
        "https://gitlab.com/Seredos/gitlab-tests/-/issues/5", p,
    )
    assert out == "https://gitlab.com/seredos/gitlab-tests/-/issues/5"


def test_canonical_url_rewrites_work_items_to_issues():
    p = _gitlab_project(path="seredos/gitlab-tests")
    out = _canonical_url(
        "https://gitlab.com/seredos/gitlab-tests/-/work_items/5", p,
    )
    assert out == "https://gitlab.com/seredos/gitlab-tests/-/issues/5"


def test_canonical_url_handles_anchor():
    p = _gitlab_project(path="Seredos/gitlab-tests")
    out = _canonical_url(
        "https://gitlab.com/Seredos/gitlab-tests/-/issues/5#note_99", p,
    )
    assert out == "https://gitlab.com/seredos/gitlab-tests/-/issues/5#note_99"


def test_canonical_url_combined_lowercase_and_rewrite():
    p = _gitlab_project(path="Seredos/gitlab-tests")
    out = _canonical_url(
        "https://gitlab.com/Seredos/gitlab-tests/-/work_items/12", p,
    )
    assert out == "https://gitlab.com/seredos/gitlab-tests/-/issues/12"


def test_canonical_url_noop_when_url_empty():
    assert _canonical_url("", _gitlab_project()) == ""


def test_canonical_url_noop_when_path_already_lowercase():
    p = _gitlab_project(path="seredos/gitlab-tests")
    url = "https://gitlab.com/seredos/gitlab-tests/-/issues/5"
    assert _canonical_url(url, p) == url


def test_gitlab_map_issue_canonicalises_ticket_url(monkeypatch):
    """End-to-end: a get_ticket response returns a canonicalised URL."""
    issue = {
        "iid": 5, "title": "T", "description": "",
        "state": "opened", "author": {"username": "a"},
        "assignees": [], "labels": [],
        "web_url": "https://gitlab.com/Seredos/gitlab-tests/-/work_items/5",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }

    def handler(req):
        if req.url.path.endswith("/issues/5/notes"):
            return _resp([])
        if req.url.path.endswith("/issues/5"):
            return _resp(issue)
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    ticket, _comments, _rels, _trunc = GitLabProvider().get_ticket(
        _gitlab_project(), "t", "5", include_relations=False,
    )
    assert ticket.url == (
        "https://gitlab.com/seredos/gitlab-tests/-/issues/5"
    )


# ---------- finding 5 + 6: status vocab single source of truth --------------


def test_gitlab_rejects_github_style_status_alias(monkeypatch):
    def handler(req):
        if req.method == "GET":
            return _resp({
                "iid": 5, "title": "T", "description": "",
                "state": "opened", "author": {"username": "a"},
                "assignees": [], "labels": ["ai-generated"],
                "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            })
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    with pytest.raises(ValueError, match="closed:not_planned"):
        GitLabProvider().update_ticket(
            _gitlab_project(), "t", "5", status="closed:not_planned",
        )


def test_github_rejects_bare_closed_alias():
    """`closed` is no longer silently coerced to `closed:completed`
    on GitHub — the agent must use an exact `list_statuses` value."""
    from project_issues_plugin.providers.github import _split_github_status
    with pytest.raises(ValueError, match="unsupported status 'closed'"):
        _split_github_status("closed")


def test_gitlab_status_error_mirrors_list_statuses():
    """Per #49 finding 6: the rejection message advertises exactly the
    `list_statuses` vocabulary, not a wider GitHub-style alias set."""
    from project_issues_plugin.providers.gitlab import _status_to_state_event
    with pytest.raises(ValueError) as excinfo:
        _status_to_state_event("bogus")
    msg = str(excinfo.value)
    assert "Accepted: open, closed." in msg
    # The GitHub-style aliases must NOT appear in the GitLab error.
    assert "closed:completed" not in msg
    assert "closed:not_planned" not in msg


# ---------- finding 9: marker body trailing-newline asymmetry ---------------


def test_marker_canonical_form_for_empty_body():
    """Empty body on both providers produces the bare marker line —
    no trailing `\\n\\n` to differ across GitHub/GitLab."""
    from project_issues_plugin.markers import apply_body_marker
    assert apply_body_marker(None, will_be_ai_generated=True) == "#ai-generated"
    assert apply_body_marker("", will_be_ai_generated=True) == "#ai-generated"


def test_marker_keeps_separator_for_nonempty_body():
    from project_issues_plugin.markers import apply_body_marker
    out = apply_body_marker("Hello.", will_be_ai_generated=True)
    assert out == "#ai-generated\n\nHello."


# ---------- finding 10: timestamp precision normalisation -------------------


def test_normalize_timestamp_strips_ms_with_z():
    assert normalize_timestamp("2026-05-20T23:07:59.507Z") == "2026-05-20T23:07:59Z"


def test_normalize_timestamp_strips_ms_with_offset():
    assert normalize_timestamp("2026-05-20T23:07:59.507+02:00") == "2026-05-20T23:07:59+02:00"


def test_normalize_timestamp_passthrough_seconds():
    assert normalize_timestamp("2026-05-20T23:07:48Z") == "2026-05-20T23:07:48Z"


def test_normalize_timestamp_passthrough_empty():
    assert normalize_timestamp("") == ""
    assert normalize_timestamp(None) == ""


def test_normalize_timestamp_passthrough_unknown_shape():
    # Doesn't match the pattern → returned as-is rather than mangled.
    assert normalize_timestamp("nonsense") == "nonsense"


def test_gitlab_ticket_timestamps_are_normalised(monkeypatch):
    issue = {
        "iid": 5, "title": "T", "description": "",
        "state": "opened", "author": {"username": "a"},
        "assignees": [], "labels": [],
        "web_url": "https://gitlab.com/seredos/gitlab-tests/-/issues/5",
        "created_at": "2026-05-20T23:07:59.507Z",
        "updated_at": "2026-05-20T23:08:01.123Z",
    }

    def handler(req):
        if req.url.path.endswith("/issues/5/notes"):
            return _resp([])
        if req.url.path.endswith("/issues/5"):
            return _resp(issue)
        return _resp({}, 404)

    _install_gitlab_mock(monkeypatch, handler)
    ticket, _c, _r, _t = GitLabProvider().get_ticket(
        _gitlab_project(), "t", "5", include_relations=False,
    )
    assert ticket.created_at == "2026-05-20T23:07:59Z"
    assert ticket.updated_at == "2026-05-20T23:08:01Z"


# ---------- finding 11: GitHub label ordering -------------------------------


def test_github_labels_sorted_alphabetically(monkeypatch):
    """Labels come back sorted regardless of API application order."""
    issue = {
        "number": 3,
        "title": "T",
        "body": "",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        # Intentional non-alphabetical order from the API.
        "labels": [
            {"name": "test-label"},
            {"name": "ai-generated"},
            {"name": "bug"},
        ],
        "html_url": "https://github.com/acme/backend/issues/3",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }

    def handler(req):
        if req.url.path.endswith("/issues/3/comments"):
            return _resp([])
        if req.url.path.endswith("/issues/3"):
            return _resp(issue)
        return _resp([])

    _install_github_mock(monkeypatch, handler)
    p = ProjectConfig(id="acme", provider="github", path="acme/backend")
    ticket, _c, _r, _t = GitHubProvider().get_ticket(
        p, "t", "3", include_relations=False,
    )
    assert ticket.labels == ["ai-generated", "bug", "test-label"]
