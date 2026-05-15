"""Tests for the comment tools (list/get/update_comment).

Uses `httpx.MockTransport` to intercept HTTP calls; the provider is
monkey-patched so `_client(token)` returns a client backed by the mock
transport. The tool-level permission gate and `_safe` wrapper are
exercised via the `tools/comments.py` register entrypoint.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
from project_issues_plugin.providers.github import GitHubProvider
from project_issues_plugin.tools import comments as comment_tools


# ---------- helpers ----------------------------------------------------------


def _project(*, modify: bool = True) -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={
            "issues": {"create": True, "modify": modify},
        },
    )


def _comment_payload(comment_id: int, body: str = "hello", **overrides) -> dict:
    base = {
        "id": comment_id,
        "user": {"login": "alice"},
        "body": body,
        "html_url": f"https://github.com/acme/backend/issues/1#issuecomment-{comment_id}",
        "created_at": "2024-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _install_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Replace `github._client` so calls go through MockTransport."""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
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


# ---------- provider-level tests --------------------------------------------


def test_list_comments_honors_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/issues/42/comments":
            captured["per_page"] = req.url.params.get("per_page", "")
            return _json([
                _comment_payload(1, body="first"),
                _comment_payload(2, body="second"),
            ])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    comments = provider.list_comments(_project(), token="t", ticket_id="42", limit=5)
    assert [c.id for c in comments] == ["1", "2"]
    assert [c.body for c in comments] == ["first", "second"]
    assert captured["per_page"] == "5"


def test_list_comments_caps_limit_at_100(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/issues/42/comments":
            captured["per_page"] = req.url.params.get("per_page", "")
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    provider.list_comments(_project(), token="t", ticket_id="42", limit=500)
    assert captured["per_page"] == "100"


def test_get_comment_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/issues/comments/777":
            return _json(_comment_payload(777, body="hello world"))
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    comment = provider.get_comment(_project(), token="t", comment_id="777")
    assert comment.id == "777"
    assert comment.body == "hello world"
    assert comment.author == "alice"


def test_update_comment_adds_ai_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "PATCH"
            and req.url.path == "/repos/acme/backend/issues/comments/777"
        ):
            captured["body"] = json.loads(req.content)
            return _json(_comment_payload(777, body=captured["body"]["body"]))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    comment = provider.update_comment(
        _project(), token="t", comment_id="777", body="new content"
    )
    assert captured["body"] == {"body": "#ai-generated\n\nnew content"}
    assert comment.body.startswith("#ai-generated\n\n")


def test_update_comment_keeps_existing_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body that already starts with `#ai-generated` must NOT be re-prefixed."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "PATCH"
            and req.url.path == "/repos/acme/backend/issues/comments/777"
        ):
            captured["body"] = json.loads(req.content)
            return _json(_comment_payload(777, body=captured["body"]["body"]))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    provider = GitHubProvider()
    already = "#ai-generated\n\nfollow-up edit"
    comment = provider.update_comment(
        _project(), token="t", comment_id="777", body=already
    )
    # The prefix must appear exactly once.
    sent_body = captured["body"]["body"]
    assert sent_body == already
    assert sent_body.count("#ai-generated") == 1
    assert comment.body.count("#ai-generated") == 1


# ---------- tool-level tests (permissions, _safe wrapping) ------------------


class _StubMCP:
    """Capture functions registered via `@mcp.tool()` so tests can call them."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools_with(monkeypatch: pytest.MonkeyPatch, project: ProjectConfig):
    """Register `tools/comments.py` against a stub MCP and stub project resolution.

    Returns the dict of registered tool callables.
    """
    from project_issues_plugin import config as cfg_mod

    def fake_load_projects(cwd=None):
        return cfg_mod.LoadResult(
            projects=[project],
            state="ok",
            search_root="/tmp",
        )

    # Patch both the config module and the symbol already imported by
    # `tools/comments.py` (it was bound at import time).
    monkeypatch.setattr(cfg_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(comment_tools, "load_projects", fake_load_projects)

    stub = _StubMCP()
    comment_tools.register(stub)
    return stub.tools


def test_update_comment_tool_requires_modify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `permissions.modify`, the tool returns an error dict via `_safe`."""
    project = _project(modify=False)
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["update_comment"](
        project_id="acme",
        ticket_id="42",
        comment_id="777",
        body="hello",
    )
    assert "error" in result
    assert "modify" in result["error"]


def test_update_comment_tool_succeeds_with_modify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `permissions.modify=True` and a token, the update goes through and
    the AI-prefix is applied to a previously-non-AI comment."""
    project = _project(modify=True)
    tools = _register_tools_with(monkeypatch, project)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "PATCH"
            and req.url.path == "/repos/acme/backend/issues/comments/777"
        ):
            captured["body"] = json.loads(req.content)
            return _json(_comment_payload(777, body=captured["body"]["body"]))
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["update_comment"](
        project_id="acme",
        ticket_id="42",
        comment_id="777",
        body="updated text",
    )
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    assert result["comment"]["body"].startswith("#ai-generated\n\n")
    assert captured["body"]["body"] == "#ai-generated\n\nupdated text"


def test_list_comments_tool_does_not_require_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read tools don't gate on permissions; they work for public repos
    without a token (`resolve_token` returns None)."""
    project = _project(modify=False)
    tools = _register_tools_with(monkeypatch, project)
    monkeypatch.delenv("GITHUB_TOKEN_ACME", raising=False)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/issues/42/comments":
            return _json([_comment_payload(1)])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["list_comments"](project_id="acme", ticket_id="42")
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    assert result["ticket_id"] == "42"
    assert [c["id"] for c in result["comments"]] == ["1"]


def test_get_comment_tool_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(modify=False)
    tools = _register_tools_with(monkeypatch, project)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/issues/comments/777":
            return _json(_comment_payload(777, body="from get"))
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["get_comment"](
        project_id="acme", ticket_id="42", comment_id="777"
    )
    assert "error" not in result, result
    assert result["comment"]["id"] == "777"
    assert result["comment"]["body"] == "from get"
