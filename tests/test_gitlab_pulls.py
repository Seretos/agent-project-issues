"""Tests for the GitLab provider's merge-request (PR) surface.

Covers list_prs, get_pr, create_pr, update_pr, add_pr_comment, merge_pr.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import gitlab as gitlab_mod
from project_issues_plugin.providers.base import PRFilters
from project_issues_plugin.providers.gitlab import GitLabProvider


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme", provider="gitlab", path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        headers = {"Accept": "application/json", "User-Agent": "test"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        return httpx.Client(
            base_url=gitlab_mod._base_url(project),
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(gitlab_mod, "_client", fake_client)
    return seen


def _json(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _mr_payload(iid: int, **overrides) -> dict:
    base = {
        "iid": iid,
        "title": f"MR {iid}",
        "description": "body",
        "state": "opened",
        "draft": False,
        "author": {"username": "alice"},
        "assignees": [],
        "reviewers": [],
        "labels": [],
        "source_branch": "feat/x",
        "target_branch": "main",
        "sha": "abc123",
        "web_url": f"https://gitlab.com/acme/backend/-/merge_requests/{iid}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "detailed_merge_status": "mergeable",
    }
    base.update(overrides)
    return base


# ---------- list_prs ---------------------------------------------------------


def test_list_prs_default_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "acme%2Fbackend/merge_requests" in str(req.url)
        assert req.url.params.get("state") == "opened"
        assert req.url.params.get("per_page") == "30"
        return _json([_mr_payload(1), _mr_payload(2)])

    _install_mock(monkeypatch, handler)
    prs = GitLabProvider().list_prs(_project(), "t", PRFilters())
    assert [p.id for p in prs] == ["1", "2"]


def test_list_prs_branch_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("source_branch") == "feat/x"
        assert req.url.params.get("target_branch") == "main"
        return _json([])

    _install_mock(monkeypatch, handler)
    GitLabProvider().list_prs(
        _project(), "t", PRFilters(head="feat/x", base="main"),
    )


def test_list_prs_state_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("state"))
        return _json([])

    _install_mock(monkeypatch, handler)
    p = _project()
    GitLabProvider().list_prs(p, "t", PRFilters(status="open"))
    GitLabProvider().list_prs(p, "t", PRFilters(status="closed"))
    GitLabProvider().list_prs(p, "t", PRFilters(status="any"))
    assert seen == ["opened", "closed", "all"]


# ---------- get_pr -----------------------------------------------------------


def test_get_pr_returns_pr_and_filtered_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("merge_requests/5"):
            return _json(_mr_payload(5))
        if "merge_requests/5/notes" in url:
            return _json([
                {"id": 1, "body": "comment", "system": False,
                 "author": {"username": "a"}, "created_at": "2024-01-01T00:00:00Z"},
                {"id": 2, "body": "approved", "system": True,
                 "author": {"username": "a"}, "created_at": "2024-01-01T00:01:00Z"},
            ])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr, comments = GitLabProvider().get_pr(_project(), "t", "5")
    assert pr.id == "5"
    assert len(comments) == 1  # system note filtered
    assert comments[0].body == "comment"


# ---------- create_pr --------------------------------------------------------


def test_create_pr_applies_markers_and_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "merge_requests" in str(req.url):
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(7, description=captured["body"]["description"]))
        if req.method == "GET" and req.url.path == "/api/v4/users":
            return _json([])
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().create_pr(
        _project(), "t",
        title="New MR", body="x", head="feat/x", base="main",
        draft=True, labels=["enhancement"],
    )
    assert pr.id == "7"
    assert captured["body"]["title"] == "New MR"
    assert captured["body"]["source_branch"] == "feat/x"
    assert captured["body"]["target_branch"] == "main"
    assert captured["body"]["draft"] is True
    assert captured["body"]["description"].startswith("#ai-generated")
    assert "ai-generated" in captured["body"]["labels"]
    assert "enhancement" in captured["body"]["labels"]


def test_create_pr_draft_omitted_when_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "merge_requests" in str(req.url):
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(1))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().create_pr(
        _project(), "t", title="t", body="b", head="x", base="main",
    )
    assert "draft" not in captured["body"]


# ---------- update_pr -------------------------------------------------------


def test_update_pr_status_close(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_mr_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, state="closed"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().update_pr(_project(), "t", "5", status="closed")
    assert pr.status == "closed"
    assert captured["body"]["state_event"] == "close"


def test_update_pr_rejects_merged_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status="merged"` must point to merge_pr — refuse it here."""
    _install_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(ValueError, match="merge_pr"):
        GitLabProvider().update_pr(_project(), "t", "5", status="merged")


def test_update_pr_changes_target_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_mr_payload(5, labels=["ai-generated"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, target_branch="develop"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_pr(_project(), "t", "5", base="develop")
    assert captured["body"]["target_branch"] == "develop"


def test_update_pr_adds_ai_modified_when_not_ai_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return _json(_mr_payload(5, labels=["bug"]))
        if req.method == "PUT":
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().update_pr(_project(), "t", "5", title="renamed")
    assert "ai-modified" in captured["body"]["add_labels"]


# ---------- add_pr_comment ---------------------------------------------------


def test_add_pr_comment_applies_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            captured["body"] = json.loads(req.content.decode())
            return _json({
                "id": 1, "body": captured["body"]["body"],
                "author": {"username": "a"},
                "created_at": "2024-01-01T00:00:00Z",
            })
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    c = GitLabProvider().add_pr_comment(_project(), "t", "5", "review note")
    assert c.id == "1"
    assert captured["body"]["body"].startswith("#ai-generated")


# ---------- merge_pr ---------------------------------------------------------


def test_merge_pr_merge_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "PUT" and url.endswith("/merge"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, state="merged"))
        if req.method == "GET":
            return _json(_mr_payload(5, state="merged"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    pr = GitLabProvider().merge_pr(_project(), "t", "5", strategy="merge")
    assert pr.status == "merged"
    assert "squash" not in captured["body"]


def test_merge_pr_squash_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "PUT" and url.endswith("/merge"):
            captured["body"] = json.loads(req.content.decode())
            return _json(_mr_payload(5, state="merged"))
        if req.method == "GET":
            return _json(_mr_payload(5, state="merged"))
        return _json({}, status_code=404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().merge_pr(_project(), "t", "5", strategy="squash")
    assert captured["body"]["squash"] is True


def test_merge_pr_rebase_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """`strategy='rebase'` must surface a clear error pointing at the
    separate rebase endpoint."""
    _install_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(ValueError, match="rebase"):
        GitLabProvider().merge_pr(_project(), "t", "5", strategy="rebase")


def test_merge_pr_unknown_strategy_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, lambda r: _json({}, 200))
    with pytest.raises(ValueError, match="unsupported"):
        GitLabProvider().merge_pr(
            _project(), "t", "5", strategy="cherry-pick",
        )


def test_merge_pr_refetches_after_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror GitHub provider's pattern: after merge, do a fresh GET to
    pick up post-merge state mutations (merge_commit_sha, etc.)."""
    seen_methods: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        seen_methods.append(f"{req.method} {url.split('?')[0].split('/api/v4')[-1]}")
        if req.method == "PUT":
            return _json(_mr_payload(5, state="merged"))
        if req.method == "GET":
            return _json(_mr_payload(5, state="merged"))
        return _json({}, 404)

    _install_mock(monkeypatch, handler)
    GitLabProvider().merge_pr(_project(), "t", "5", strategy="merge")
    # Sequence: PUT /merge then GET /merge_requests/5
    assert seen_methods[0].startswith("PUT")
    assert seen_methods[0].endswith("/merge")
    assert seen_methods[1].startswith("GET")
    assert seen_methods[1].endswith("/merge_requests/5")


def test_merge_pr_commit_message_routed_per_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            captured.setdefault("bodies", []).append(
                json.loads(req.content.decode())
            )
            return _json(_mr_payload(5, state="merged"))
        return _json(_mr_payload(5, state="merged"))

    _install_mock(monkeypatch, handler)
    p = _project()
    GitLabProvider().merge_pr(p, "t", "5", strategy="merge", commit_message="m1")
    GitLabProvider().merge_pr(p, "t", "5", strategy="squash", commit_message="m2")
    assert captured["bodies"][0]["merge_commit_message"] == "m1"
    assert captured["bodies"][1]["squash_commit_message"] == "m2"
