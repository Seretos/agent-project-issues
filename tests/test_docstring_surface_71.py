"""Regression and guard tests for the docstring / return-shape nits addressed
in the surface-audit work.

Regression tests (items 1-5 in the plan):
  1-3. get_ticket with include_relations=False / include_comments=False /
       comments_limit=0 produces the new `*_fetched` keys rather than
       empty lists.
  4.   get_pr with include_comments=False produces the new shape.
  5.   list_relation_kinds includes read_only_kinds.

Guard tests (must pass both before and after):
  6-8. Default calls include the full data and the `*_fetched: True` flags.
  9.   kinds and read_only_kinds are disjoint.
 10.   list_projects docstring mentions "not paginated" or absence of
       total/truncated.
 11.   add_pr_review_comment docstring references add_pr_comment.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import projects as project_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import relations as relation_tools
from project_issues_plugin.tools import tickets as ticket_tools


# ---------- shared helpers ---------------------------------------------------


def _project(
    id_: str = "acme",
    repo: str = "backend",
    issues_modify: bool = True,
) -> ProjectConfig:
    return ProjectConfig(
        id=id_,
        provider="github",
        path=f"acme/{repo}",
        token_env="GITHUB_TOKEN_ACME",
        permissions={"issues": {"create": True, "modify": issues_modify}},
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
        "head": {
            "ref": "feat", "sha": "abc",
            "user": {"login": "alice"},
            "repo": {"full_name": "acme/backend"},
        },
        "base": {
            "ref": "main", "sha": "def",
            "user": {"login": "alice"},
            "repo": {"full_name": "acme/backend"},
        },
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


def _json_response(payload, status_code: int = 200):
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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


def _register(monkeypatch, module, projects: list[ProjectConfig] | None = None):
    if projects is None:
        projects = [_project()]

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


# A minimal handler that serves a ticket with no timeline/comments/relations.
def _ticket_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/repos/acme/backend/issues/1":
        return _json_response(_issue(1))
    # comments, timeline, mentions scan — all empty
    return _json_response([])


# A minimal handler that serves a PR with no comments/review_comments.
def _pr_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/repos/acme/backend/pulls/5":
        return _json_response(_pr(5))
    return _json_response([])


# ---------- regression tests (must fail on old code, pass after fix) ---------


def test_get_ticket_include_relations_false_omits_relations_keys(monkeypatch):
    """include_relations=False: no 'relations'/'relations_truncated', relations_fetched is False."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)
    _install_mock(monkeypatch, _ticket_handler)

    result = tools["get_ticket"](
        project_id="acme", ticket_id="1",
        include_relations=False,
        include_comments=False,
    )
    assert "error" not in result
    assert "relations" not in result
    assert "relations_truncated" not in result
    assert result["relations_fetched"] is False


def test_get_ticket_include_comments_false_omits_comments_key(monkeypatch):
    """include_comments=False: no 'comments' key, comments_fetched is False."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)
    _install_mock(monkeypatch, _ticket_handler)

    result = tools["get_ticket"](
        project_id="acme", ticket_id="1",
        include_comments=False,
        include_relations=False,
    )
    assert "error" not in result
    assert "comments" not in result
    assert result["comments_fetched"] is False


def test_get_ticket_comments_limit_zero_omits_comments_key(monkeypatch):
    """comments_limit=0 (alias for include_comments=False): no 'comments', fetched=False."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)
    _install_mock(monkeypatch, _ticket_handler)

    result = tools["get_ticket"](
        project_id="acme", ticket_id="1",
        comments_limit=0,
        include_relations=False,
    )
    assert "error" not in result
    assert "comments" not in result
    assert result["comments_fetched"] is False


def test_get_pr_include_comments_false_omits_comments_key(monkeypatch):
    """get_pr include_comments=False: no 'comments' key, comments_fetched is False."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools)
    _install_mock(monkeypatch, _pr_handler)

    result = tools["get_pr"](
        project_id="acme", pr_id="5",
        include_comments=False,
    )
    assert "error" not in result
    assert "comments" not in result
    assert result["comments_fetched"] is False
    # review_comments must still be present
    assert "review_comments" in result


def test_list_relation_kinds_includes_read_only_kinds(monkeypatch):
    """list_relation_kinds returns read_only_kinds with the expected entries."""
    tools = _register(monkeypatch, relation_tools)
    result = tools["list_relation_kinds"]()
    assert "read_only_kinds" in result
    read_only = result["read_only_kinds"]
    for expected in ("mentions", "mentioned_by", "closed_by", "duplicated_by", "closes"):
        assert expected in read_only, f"expected '{expected}' in read_only_kinds"


# ---------- guard tests (must pass both before and after) --------------------


def test_get_ticket_include_relations_true_returns_relations_list(monkeypatch):
    """Default include_relations=True: 'relations' present, relations_fetched=True."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)
    _install_mock(monkeypatch, _ticket_handler)

    result = tools["get_ticket"](
        project_id="acme", ticket_id="1",
        include_comments=False,
    )
    assert "error" not in result
    assert "relations" in result
    assert isinstance(result["relations"], list)
    assert result["relations_fetched"] is True


def test_get_ticket_include_comments_true_returns_comments_list(monkeypatch):
    """Default include_comments=True: 'comments' present, comments_fetched=True."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/issues/1":
            return _json_response(_issue(1))
        if "/issues/1/comments" in req.url.path:
            return _json_response([_comment(1, "hello")])
        return _json_response([])

    _install_mock(monkeypatch, handler)

    result = tools["get_ticket"](
        project_id="acme", ticket_id="1",
        include_relations=False,
    )
    assert "error" not in result
    assert "comments" in result
    assert isinstance(result["comments"], list)
    assert result["comments_fetched"] is True


def test_get_pr_include_comments_true_returns_comments_list(monkeypatch):
    """Default get_pr: 'comments' present, comments_fetched=True."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/pulls/5":
            return _json_response(_pr(5))
        if req.url.path == "/repos/acme/backend/issues/5/comments":
            return _json_response([_comment(1, "hello")])
        return _json_response([])

    _install_mock(monkeypatch, handler)

    result = tools["get_pr"](project_id="acme", pr_id="5")
    assert "error" not in result
    assert "comments" in result
    assert isinstance(result["comments"], list)
    assert result["comments_fetched"] is True


def test_list_relation_kinds_writable_and_readonly_disjoint(monkeypatch):
    """kinds and read_only_kinds must not overlap."""
    tools = _register(monkeypatch, relation_tools)
    result = tools["list_relation_kinds"]()
    writable = set(result["kinds"])
    read_only = set(result["read_only_kinds"])
    overlap = writable & read_only
    assert not overlap, f"overlap between kinds and read_only_kinds: {overlap}"


def test_list_projects_docstring_not_paginated_note(monkeypatch):
    """list_projects docstring mentions absence of pagination."""
    tools = _register(monkeypatch, project_tools)
    doc = tools["list_projects"].__doc__ or ""
    assert (
        "not paginated" in doc
        or ("total" in doc and "truncated" in doc)
        or "search_projects" in doc
    ), "list_projects docstring should mention that it is not paginated or reference search_projects"


def test_add_pr_review_comment_docstring_mentions_add_pr_comment(monkeypatch):
    """add_pr_review_comment docstring should reference add_pr_comment."""
    tools = _register(monkeypatch, pull_tools)
    doc = tools["add_pr_review_comment"].__doc__ or ""
    assert "add_pr_comment" in doc, (
        "add_pr_review_comment docstring should reference 'add_pr_comment' "
        "to guide users toward the discussion-level comment tool"
    )
