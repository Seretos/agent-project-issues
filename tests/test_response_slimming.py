"""Tests for ticket #50: response-slimming knobs on list/get tools.

Covers `omit_body` / `body_max_chars` on list tools and
`include_comments` / `comments_limit` / `comments_order` /
`comments_body_max_chars` on `get_ticket` and `get_pr`. Also asserts
the default `limit_per_project` drop on `list_tickets_across_projects`
(30 -> 10).
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import bulk as bulk_tools
from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import tickets as ticket_tools
from project_issues_plugin.tools._slicing import apply_body_knobs


def _project(id_: str = "acme", repo: str = "backend") -> ProjectConfig:
    return ProjectConfig(
        id=id_,
        provider="github",
        path=f"acme/{repo}",
        token_env="GITHUB_TOKEN_ACME",
        permissions={"issues": {"create": True, "modify": True}},
    )


def _issue(id_: int, body: str = "issue body") -> dict:
    return {
        "number": id_,
        "title": f"issue {id_}",
        "body": body,
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/issues/{id_}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _pr(id_: int, body: str = "pr body") -> dict:
    return {
        "number": id_,
        "title": f"pr {id_}",
        "body": body,
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/pull/{id_}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "head": {"ref": "feat", "sha": "abc", "user": {"login": "alice"}, "repo": {"full_name": "acme/backend"}},
        "base": {"ref": "main", "sha": "def", "user": {"login": "alice"}, "repo": {"full_name": "acme/backend"}},
        "draft": False,
        "merged": False,
        "mergeable": None,
        "mergeable_state": "unknown",
        "requested_reviewers": [],
        "requested_teams": [],
    }


def _comment(id_: int, body: str = "c body") -> dict:
    return {
        "id": id_,
        "user": {"login": "alice"},
        "body": body,
        "html_url": f"https://github.com/acme/backend/issues/1#issuecomment-{id_}",
        "created_at": f"2024-01-0{id_}T00:00:00Z",
    }


def _json_response(payload, status_code: int = 200, headers: dict | None = None):
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_mock(monkeypatch, handler):
    def wrapped(req: httpx.Request) -> httpx.Response:
        return handler(req)
    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
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


def _register(monkeypatch, module, projects: list[ProjectConfig]):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=projects, state="ok", search_root="/tmp",
        )
    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    if hasattr(module, "load_projects"):
        monkeypatch.setattr(module, "load_projects", fake_load_projects)
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


# ---------- _slicing helper unit tests --------------------------------------


def test_apply_body_knobs_pass_through_when_no_args():
    rows = [{"id": "1", "body": "x"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=None)
    assert out is rows  # same reference — no-op


def test_apply_body_knobs_omit_drops_body_key():
    rows = [{"id": "1", "body": "x", "title": "t"}]
    out = apply_body_knobs(rows, omit_body=True, body_max_chars=None)
    assert "body" not in out[0]
    assert out[0]["title"] == "t"


def test_apply_body_knobs_truncates_and_marks():
    rows = [{"id": "1", "body": "abcdefghij"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=4)
    assert out[0]["body"] == "abcd"
    assert out[0]["body_truncated"] is True


def test_apply_body_knobs_does_not_mark_when_under_cap():
    rows = [{"id": "1", "body": "abc"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=10)
    assert out[0]["body"] == "abc"
    assert out[0]["body_truncated"] is False


def test_apply_body_knobs_omit_wins_over_truncate():
    rows = [{"id": "1", "body": "xxxxxxxxxx"}]
    out = apply_body_knobs(rows, omit_body=True, body_max_chars=2)
    assert "body" not in out[0]
    assert "body_truncated" not in out[0]


# ---------- list_tickets ----------------------------------------------------


def test_list_tickets_omit_body(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    def handler(req):
        return _json_response([_issue(1, body="long body"), _issue(2, body="other")])

    _install_mock(monkeypatch, handler)
    result = tools["list_tickets"](project_id="acme", omit_body=True)
    assert "error" not in result
    for row in result["tickets"]:
        assert "body" not in row


def test_list_tickets_body_max_chars(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    def handler(req):
        return _json_response([_issue(1, body="abcdefghij"), _issue(2, body="abc")])

    _install_mock(monkeypatch, handler)
    result = tools["list_tickets"](
        project_id="acme", body_max_chars=4,
    )
    assert result["tickets"][0]["body"] == "abcd"
    assert result["tickets"][0]["body_truncated"] is True
    assert result["tickets"][1]["body"] == "abc"
    assert result["tickets"][1]["body_truncated"] is False


def test_list_tickets_default_behaviour_unchanged(monkeypatch):
    """No knobs => no body trim, no body_truncated field."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    def handler(req):
        return _json_response([_issue(1, body="full body")])

    _install_mock(monkeypatch, handler)
    result = tools["list_tickets"](project_id="acme")
    assert result["tickets"][0]["body"] == "full body"
    assert "body_truncated" not in result["tickets"][0]


# ---------- list_prs --------------------------------------------------------


def test_list_prs_omit_body(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])

    def handler(req):
        return _json_response([_pr(1, body="full"), _pr(2, body="more")])

    _install_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme", omit_body=True)
    assert "error" not in result
    for row in result["prs"]:
        assert "body" not in row


def test_list_prs_body_max_chars(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])

    def handler(req):
        return _json_response([_pr(1, body="abcdefghij")])

    _install_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme", body_max_chars=5)
    assert result["prs"][0]["body"] == "abcde"
    assert result["prs"][0]["body_truncated"] is True


# ---------- list_comments ---------------------------------------------------


def test_list_comments_omit_body(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, comment_tools, [_project()])

    def handler(req):
        return _json_response([_comment(1, body="a"), _comment(2, body="b")])

    _install_mock(monkeypatch, handler)
    result = tools["list_comments"](
        project_id="acme", ticket_id="42", omit_body=True,
    )
    for row in result["comments"]:
        assert "body" not in row


def test_list_comments_body_max_chars(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, comment_tools, [_project()])

    def handler(req):
        return _json_response([_comment(1, body="abcdef")])

    _install_mock(monkeypatch, handler)
    result = tools["list_comments"](
        project_id="acme", ticket_id="42", body_max_chars=3,
    )
    assert result["comments"][0]["body"] == "abc"
    assert result["comments"][0]["body_truncated"] is True


# ---------- list_tickets_across_projects ------------------------------------


def test_bulk_default_limit_lowered_to_10(monkeypatch):
    """Default `limit_per_project` is 10 (was 30 — ticket #50)."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, bulk_tools, [_project()])
    captured: dict[str, str] = {}

    def handler(req):
        captured["per_page"] = req.url.params.get("per_page", "")
        return _json_response([_issue(1)])

    _install_mock(monkeypatch, handler)
    _ = tools["list_tickets_across_projects"]()
    assert captured["per_page"] == "10"


def test_bulk_explicit_limit_per_project_overrides(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, bulk_tools, [_project()])
    captured: dict[str, str] = {}

    def handler(req):
        captured["per_page"] = req.url.params.get("per_page", "")
        return _json_response([_issue(1)])

    _install_mock(monkeypatch, handler)
    _ = tools["list_tickets_across_projects"](limit_per_project=50)
    assert captured["per_page"] == "50"


def test_bulk_omit_body_drops_across_projects(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, bulk_tools, [_project()])

    def handler(req):
        return _json_response([_issue(1, body="payload")])

    _install_mock(monkeypatch, handler)
    result = tools["list_tickets_across_projects"](omit_body=True)
    for entry in result["results"].values():
        for row in entry["tickets"]:
            assert "body" not in row


# ---------- get_ticket ------------------------------------------------------


def test_get_ticket_include_comments_false(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    def handler(req):
        if "/issues/1/comments" in req.url.path:
            return _json_response([_comment(1, "x"), _comment(2, "y")])
        if req.url.path == "/repos/acme/backend/issues/1":
            return _json_response(_issue(1, body="t"))
        if "/issues/1/timeline" in req.url.path:
            return _json_response([])
        if "/issues/1/lock" in req.url.path:
            return _json_response({})
        # mentions / closes scan endpoints
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_ticket"](
        project_id="acme", ticket_id="1", include_comments=False,
        include_relations=False,
    )
    assert "error" not in result
    assert "comments" not in result
    assert result["comments_fetched"] is False


def test_get_ticket_comments_limit_zero_aliases_include_false(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    def handler(req):
        if "/issues/1/comments" in req.url.path:
            return _json_response([_comment(1, "x")])
        if req.url.path == "/repos/acme/backend/issues/1":
            return _json_response(_issue(1))
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_ticket"](
        project_id="acme", ticket_id="1", comments_limit=0,
        include_relations=False,
    )
    assert "comments" not in result
    assert result["comments_fetched"] is False


def test_get_ticket_comments_limit_and_order_tail(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    def handler(req):
        if "/issues/1/comments" in req.url.path:
            return _json_response([
                _comment(1, "first"),
                _comment(2, "second"),
                _comment(3, "third"),
            ])
        if req.url.path == "/repos/acme/backend/issues/1":
            return _json_response(_issue(1))
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_ticket"](
        project_id="acme", ticket_id="1",
        comments_limit=2, comments_order="desc",
        include_relations=False,
    )
    assert [c["body"] for c in result["comments"]] == ["third", "second"]


def test_get_ticket_comments_body_max_chars(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    def handler(req):
        if "/issues/1/comments" in req.url.path:
            return _json_response([_comment(1, "abcdefghij")])
        if req.url.path == "/repos/acme/backend/issues/1":
            return _json_response(_issue(1))
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_ticket"](
        project_id="acme", ticket_id="1",
        comments_body_max_chars=4,
        include_relations=False,
    )
    assert result["comments"][0]["body"] == "abcd"
    assert result["comments"][0]["body_truncated"] is True


# ---------- get_pr ----------------------------------------------------------


def test_get_pr_include_comments_false(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])

    def handler(req):
        if req.url.path == "/repos/acme/backend/pulls/5":
            return _json_response(_pr(5))
        if req.url.path == "/repos/acme/backend/issues/5/comments":
            return _json_response([_comment(1, "x")])
        if req.url.path == "/repos/acme/backend/pulls/5/comments":
            return _json_response([])
        if req.url.path == "/repos/acme/backend/pulls/5/reviews":
            return _json_response([])
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_pr"](
        project_id="acme", pr_id="5", include_comments=False,
    )
    assert "comments" not in result
    assert result["comments_fetched"] is False
    # review_comments not affected by the comment knobs
    assert "review_comments" in result


def test_get_pr_comments_tail(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])

    def handler(req):
        if req.url.path == "/repos/acme/backend/pulls/5":
            return _json_response(_pr(5))
        if req.url.path == "/repos/acme/backend/issues/5/comments":
            return _json_response([
                _comment(1, "first"), _comment(2, "second"), _comment(3, "third"),
            ])
        if req.url.path == "/repos/acme/backend/pulls/5/comments":
            return _json_response([])
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_pr"](
        project_id="acme", pr_id="5",
        comments_limit=2, comments_order="desc",
    )
    assert [c["body"] for c in result["comments"]] == ["third", "second"]
