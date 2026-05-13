"""Tests for relation enrichment on the GitHub `get_ticket` path.

We use `httpx.MockTransport` to intercept HTTP calls and return canned
responses; the provider is monkey-patched so `_client(token)` returns a
client backed by our mock transport.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
from project_issues_plugin.providers.github import GitHubProvider


# ---------- helpers ----------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        owner="acme",
        repo="backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _issue_payload(number: int, **overrides) -> dict:
    base = {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/issues/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _install_mock(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """Replace `github._client` so calls go through MockTransport.

    Returns a list that will be populated with every intercepted request,
    for assertion convenience.
    """
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        # Mirror the real headers so anything the provider inspects works.
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "test-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


# ---------- tests ------------------------------------------------------------


def test_no_relations(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ticket with no parent, no children, and an empty timeline yields []."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    ticket, comments, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert ticket.id == "42"
    assert comments == []
    assert relations == []
    assert truncated is False


def test_parent_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue payload's `parent` field surfaces as a `parent` relation."""

    parent_payload = {
        "number": 7,
        "title": "Epic",
        "state": "open",
        "html_url": "https://github.com/acme/backend/issues/7",
        "repository": {"full_name": "acme/backend"},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42, parent=parent_payload))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert truncated is False
    assert len(relations) == 1
    rel = relations[0]
    assert rel.kind == "parent"
    assert rel.ticket_id == "#7"
    assert rel.title == "Epic"
    assert rel.url == "https://github.com/acme/backend/issues/7"
    assert rel.state == "open"
    assert rel.is_pull_request is False


def test_child_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-issues surface as `child` relations."""

    child_a = _issue_payload(101, title="Sub A")
    child_b = _issue_payload(102, title="Sub B", state="closed")
    # `repository` is included in sub_issues responses; mimic that.
    child_a["repository"] = {"full_name": "acme/backend"}
    child_b["repository"] = {"full_name": "acme/backend"}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([child_a, child_b])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert truncated is False
    kinds = [r.kind for r in relations]
    assert kinds == ["child", "child"]
    assert {r.ticket_id for r in relations} == {"#101", "#102"}
    closed_child = next(r for r in relations if r.ticket_id == "#102")
    assert closed_child.state == "closed"


def test_pr_closes_via_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `connected` timeline event whose source is a merged PR yields `closed_by`."""

    pr_source = {
        "number": 55,
        "title": "Fix bug",
        "state": "closed",
        "merged_at": "2024-02-01T12:00:00Z",
        "html_url": "https://github.com/acme/backend/pull/55",
        "pull_request": {"url": "https://api.github.com/repos/acme/backend/pulls/55"},
        "repository": {"full_name": "acme/backend"},
    }
    timeline = [
        {
            "event": "connected",
            "source": {"type": "issue", "issue": pr_source},
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json(timeline)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="42")
    assert len(relations) == 1
    rel = relations[0]
    assert rel.kind == "closed_by"
    assert rel.ticket_id == "#55"
    assert rel.title == "Fix bug"
    assert rel.state == "merged"
    assert rel.is_pull_request is True


def test_duplicate_via_marked_as_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `marked_as_duplicate` event resolves direction from `canonical`/`dupe`."""

    canonical = _issue_payload(9, title="Canonical")
    canonical["repository"] = {"full_name": "acme/backend"}
    # This issue (42) is marked as duplicate of #9. The event on 42's
    # timeline therefore has `canonical=#9` and `dupe=#42` (this one).
    timeline = [
        {
            "event": "marked_as_duplicate",
            "canonical": canonical,
            "dupe": _issue_payload(42),
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json(timeline)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="42")
    assert len(relations) == 1
    rel = relations[0]
    assert rel.kind == "duplicate_of"
    assert rel.ticket_id == "#9"


def test_cross_repo_cross_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-referenced event from a different repo yields `owner/repo#N`."""

    source = {
        "number": 3,
        "title": "Mentioned over here",
        "state": "open",
        "html_url": "https://github.com/other-org/other-repo/issues/3",
        "repository": {"full_name": "other-org/other-repo"},
    }
    timeline = [
        {
            "event": "cross-referenced",
            "source": {"type": "issue", "issue": source},
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json(timeline)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, _ = provider.get_ticket(_project(), token="t", ticket_id="42")
    assert len(relations) == 1
    rel = relations[0]
    assert rel.kind == "mentioned_by"
    assert rel.ticket_id == "other-org/other-repo#3"
    assert rel.url == "https://github.com/other-org/other-repo/issues/3"
    assert rel.is_pull_request is False


def test_truncation_flag_when_link_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeline response that advertises `rel=\"next\"` sets relations_truncated=True."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json([])
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json(
                [],
                headers={
                    "Link": (
                        '<https://api.github.com/repos/acme/backend/issues/42/'
                        'timeline?page=2>; rel="next", '
                        '<https://api.github.com/repos/acme/backend/issues/42/'
                        'timeline?page=5>; rel="last"'
                    )
                },
            )
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert relations == []
    assert truncated is True


def test_sub_issues_404_falls_back_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 from `/sub_issues` (older GHES) is silently treated as empty."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        if path == "/repos/acme/backend/issues/42/sub_issues":
            return _json({"message": "Not Found"}, status_code=404)
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42"
    )
    assert relations == []
    assert truncated is False


def test_include_relations_false_skips_extra_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """`include_relations=False` avoids the sub-issues and timeline requests."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json(_issue_payload(42))
        if path == "/repos/acme/backend/issues/42/comments":
            return _json([])
        # If we reach this branch with include_relations=False, the test
        # should fail loudly.
        raise AssertionError(
            f"unexpected extra request when include_relations=False: {req.url}"
        )

    seen = _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    _, _, relations, truncated = provider.get_ticket(
        _project(), token="t", ticket_id="42", include_relations=False
    )
    assert relations == []
    assert truncated is False
    # We expect exactly two calls: the issue and the comments.
    paths = [r.url.path for r in seen]
    assert paths == [
        "/repos/acme/backend/issues/42",
        "/repos/acme/backend/issues/42/comments",
    ]
