"""Tests for ticket #47: list_comments ordering / tail / since / pagination."""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.github import GitHubProvider
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import comments as comment_tools


# ---------- shared helpers (mirrors test_comments.py patterns) --------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={"issues": {"create": True, "modify": True}},
    )


def _comment(id_: int, body: str, created_at: str = "2024-01-01T00:00:00Z") -> dict:
    return {
        "id": id_,
        "user": {"login": "alice"},
        "body": body,
        "html_url": f"https://github.com/acme/backend/issues/1#issuecomment-{id_}",
        "created_at": created_at,
    }


def _json_response(payload, status_code: int = 200, headers: dict | None = None):
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_mock(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers={"Accept": "application/vnd.github+json"},
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools(monkeypatch, project: ProjectConfig):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp",
        )
    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(comment_tools, "load_projects", fake_load_projects)
    stub = _StubMCP()
    comment_tools.register(stub)
    return stub.tools


# ---------- provider-level: since / page / has_more -------------------------


def test_provider_list_comments_forwards_since(monkeypatch):
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["since"] = req.url.params.get("since", "")
        captured["page"] = req.url.params.get("page", "")
        return _json_response([_comment(1, "x")])

    _install_mock(monkeypatch, handler)
    rows, has_more = GitHubProvider().list_comments(
        _project(), "t", "42", limit=10, since="2025-01-01T00:00:00Z", page=2,
    )
    assert captured["since"] == "2025-01-01T00:00:00Z"
    assert captured["page"] == "2"
    assert has_more is False
    assert [c.id for c in rows] == ["1"]


def test_provider_list_comments_detects_next_page(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            [_comment(1, "x"), _comment(2, "y")],
            headers={
                "Link": (
                    '<https://api.github.com/x?page=2>; rel="next", '
                    '<https://api.github.com/x?page=5>; rel="last"'
                ),
            },
        )

    _install_mock(monkeypatch, handler)
    rows, has_more = GitHubProvider().list_comments(_project(), "t", "42", limit=2)
    assert has_more is True
    assert len(rows) == 2


def test_provider_list_comments_no_next_link_means_no_more(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            [_comment(1, "x")],
            headers={
                "Link": '<https://api.github.com/x?page=1>; rel="last"',
            },
        )

    _install_mock(monkeypatch, handler)
    rows, has_more = GitHubProvider().list_comments(_project(), "t", "42")
    assert has_more is False


# ---------- tool-level: order / page / has_more in response shape -----------


def test_tool_default_order_is_asc(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    project = _project()
    tools = _register_tools(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response([
            _comment(1, "first",  created_at="2024-01-01T00:00:00Z"),
            _comment(2, "second", created_at="2024-01-02T00:00:00Z"),
            _comment(3, "third",  created_at="2024-01-03T00:00:00Z"),
        ])

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](project_id="acme", ticket_id="42")
    assert "error" not in result, result
    assert [c["body"] for c in result["comments"]] == ["first", "second", "third"]
    assert result["page"] == 1
    assert result["has_more"] is False


def test_tool_order_desc_reverses(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response([
            _comment(1, "first",  created_at="2024-01-01T00:00:00Z"),
            _comment(2, "second", created_at="2024-01-02T00:00:00Z"),
            _comment(3, "third",  created_at="2024-01-03T00:00:00Z"),
        ])

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42", order="desc",
    )
    assert [c["body"] for c in result["comments"]] == ["third", "second", "first"]


def test_tool_tail_use_case(monkeypatch):
    """order='desc' + limit=2 returns the last 2 comments newest-first
    on a single-page thread (the documented tail recipe)."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["per_page"] = req.url.params.get("per_page", "")
        return _json_response([
            _comment(1, "first",  created_at="2024-01-01T00:00:00Z"),
            _comment(2, "second", created_at="2024-01-02T00:00:00Z"),
        ])

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42", order="desc", limit=2,
    )
    assert captured["per_page"] == "2"
    assert [c["body"] for c in result["comments"]] == ["second", "first"]


def test_tool_since_passes_through(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["since"] = req.url.params.get("since", "")
        return _json_response([_comment(2, "second", created_at="2025-06-01T00:00:00Z")])

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42", since="2025-01-01T00:00:00Z",
    )
    assert captured["since"] == "2025-01-01T00:00:00Z"
    assert [c["body"] for c in result["comments"]] == ["second"]


def test_tool_page_arg_and_has_more(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["page"] = req.url.params.get("page", "")
        return _json_response(
            [_comment(3, "third"), _comment(4, "fourth")],
            headers={"Link": '<x?page=3>; rel="next"'},
        )

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42", page=2,
    )
    assert captured["page"] == "2"
    assert result["page"] == 2
    assert result["has_more"] is True


def test_tool_desc_plus_since_plus_limit(monkeypatch):
    """Combo: order='desc' + since=<iso> + limit=N — the docstring
    explicitly calls this combination out as the typical tail use-case."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["since"] = req.url.params.get("since", "")
        captured["per_page"] = req.url.params.get("per_page", "")
        return _json_response([
            _comment(5, "fifth",  created_at="2025-02-01T00:00:00Z"),
            _comment(6, "sixth",  created_at="2025-02-02T00:00:00Z"),
        ])

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42",
        order="desc", since="2025-01-01T00:00:00Z", limit=2,
    )
    assert captured["since"] == "2025-01-01T00:00:00Z"
    assert captured["per_page"] == "2"
    assert [c["body"] for c in result["comments"]] == ["sixth", "fifth"]


def test_tool_back_compat_default_call_unchanged(monkeypatch):
    """Sanity: calling list_comments with no new args still works
    exactly as before (the new args have defaults that preserve
    the old surface, modulo the added page/has_more fields)."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response([_comment(1, "x")])

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](project_id="acme", ticket_id="42")
    assert "error" not in result
    assert [c["body"] for c in result["comments"]] == ["x"]
    # new fields exist; defaults are safe
    assert result["page"] == 1
    assert result["has_more"] is False


def test_tool_tail_use_case_multipage_thread(monkeypatch):
    """Regression: thread with 6 comments, limit=2 → must return the
    LAST 2 (newest-first), not the first 2 reversed (ticket #47
    follow-up after test-agent live-verify).

    Algorithm: per_page=2 means last_page=3; tail-fetch fires page 1
    probe (to read Link rel=last), then page 3 (the actual tail), and
    returns the last page reversed.
    """
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())

    all_comments = [
        _comment(1, "first",  created_at="2024-01-01T00:00:00Z"),
        _comment(2, "second", created_at="2024-01-02T00:00:00Z"),
        _comment(3, "third",  created_at="2024-01-03T00:00:00Z"),
        _comment(4, "fourth", created_at="2024-01-04T00:00:00Z"),
        _comment(5, "fifth",  created_at="2024-01-05T00:00:00Z"),
        _comment(6, "sixth",  created_at="2024-01-06T00:00:00Z"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        per_page = int(req.url.params.get("per_page", "30"))
        start = (page - 1) * per_page
        end = start + per_page
        page_rows = all_comments[start:end]
        # Build a minimal Link header that advertises the last page.
        last_page = (len(all_comments) + per_page - 1) // per_page
        link = f'<https://api.github.com/x?page={last_page}>; rel="last"'
        if page < last_page:
            link = (
                f'<https://api.github.com/x?page={page + 1}>; rel="next", '
                + link
            )
        return _json_response(page_rows, headers={"Link": link})

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42",
        order="desc", limit=2,
    )
    # MUST be the last two comments, not the first two reversed.
    assert [c["body"] for c in result["comments"]] == ["sixth", "fifth"]
    # Older pages still exist (the older 4 comments).
    assert result["has_more"] is True


def test_tool_tail_use_case_partial_last_page(monkeypatch):
    """Tail with limit larger than last-page size walks backwards to
    fill `limit` items. 7 comments, per_page=3 → last_page=3 has 1
    item, walk back to page 2 (3 items) to get 4 total, slice tail
    of `limit` newest-first."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())

    all_comments = [
        _comment(i, f"c{i}", created_at=f"2024-01-{i:02d}T00:00:00Z")
        for i in range(1, 8)  # 7 comments
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        per_page = int(req.url.params.get("per_page", "30"))
        start = (page - 1) * per_page
        end = start + per_page
        page_rows = all_comments[start:end]
        last_page = (len(all_comments) + per_page - 1) // per_page
        link = f'<https://api.github.com/x?page={last_page}>; rel="last"'
        if page < last_page:
            link = (
                f'<https://api.github.com/x?page={page + 1}>; rel="next", '
                + link
            )
        return _json_response(page_rows, headers={"Link": link})

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42",
        order="desc", limit=3,
    )
    # Last 3 newest-first: c7, c6, c5.
    assert [c["body"] for c in result["comments"]] == ["c7", "c6", "c5"]
    assert result["has_more"] is True


def test_tool_tail_use_case_single_page_unchanged(monkeypatch):
    """When the whole thread fits in one page, tail just reverses page 1."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            [_comment(1, "a"), _comment(2, "b")],
            headers={"Link": '<https://api.github.com/x?page=1>; rel="last"'},
        )

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42",
        order="desc", limit=5,
    )
    assert [c["body"] for c in result["comments"]] == ["b", "a"]
    assert result["has_more"] is False


def test_tool_desc_with_explicit_page_falls_back_to_simple_reverse(monkeypatch):
    """Tail-fetch only triggers when page=1 and no since. An explicit
    page=N stays in the regular ascending fetch + client-side reverse
    so paginated callers get predictable behaviour."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["page"] = req.url.params.get("page", "")
        return _json_response([_comment(1, "a"), _comment(2, "b")])

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42",
        order="desc", limit=2, page=2,
    )
    # Only one fetch (no probe), and `page=2` was honoured verbatim.
    assert captured["page"] == "2"
    # Reversed via the client-side helper (no smart tail).
    assert [c["body"] for c in result["comments"]] == ["b", "a"]


def test_invalid_since_propagates_as_error(monkeypatch):
    """A bad ISO timestamp from the agent reaches GitHub's API and
    typically gets a 422; the existing _safe wrapper translates it."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register_tools(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "Validation Failed", "errors": [{"field": "since"}]},
            status_code=422,
        )

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](
        project_id="acme", ticket_id="42", since="not-a-date",
    )
    assert "error" in result
