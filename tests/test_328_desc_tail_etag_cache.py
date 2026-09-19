"""Acceptance tests for #326 (epic #328): `list_comments(order="desc", limit=N)`
must return the genuinely newest N comments even when the lib's ETag cache is
warm from an earlier identical call on a shorter thread.

Unlike tests/test_comments.py::_install_mock, the mock here sits BENEATH the
lib's real `ETagTransport` (mirroring production `github._client`, which wraps
`HTTPTransport` in the same cache), and the handler emulates GitHub: per-page
ETags computed from the page body only, live `Link` pagination headers, and a
real 304 (carrying ETag + Link) on `If-None-Match`. On lib v0.3.19 the 304 on
the page-1 tail probe replays the cached `Link rel="last"`, so the tail walk
starts at a stale page.

Growth steps must cross a page-count boundary (9 -> 14 comments at
per_page=3: 3 pages -> 5 pages); 14 -> 15 alone is green on v0.3.19.
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers._http_cache import ETagTransport, clear_etag_cache
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import comments as comment_tools


@pytest.fixture(autouse=True)
def _fresh_etag_cache():
    clear_etag_cache()
    yield
    clear_etag_cache()


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={"issues": {"create": True, "modify": True}},
    )


def _payload(cid: int) -> dict:
    return {
        "id": cid,
        "user": {"login": "alice"},
        "body": f"comment {cid}",
        "html_url": f"https://github.com/acme/backend/issues/1#issuecomment-{cid}",
        "created_at": "2024-01-01T00:00:00Z",
    }


class _Thread:
    """Mutable comment thread served by a GitHub-like handler."""

    def __init__(self, n: int) -> None:
        self.n = n

    def grow_to(self, n: int) -> None:
        self.n = n

    def handler(self, req: httpx.Request) -> httpx.Response:
        per_page = int(req.url.params.get("per_page", "30"))
        page = int(req.url.params.get("page", "1"))
        ids = list(range(1, self.n + 1))
        chunk = ids[(page - 1) * per_page: page * per_page]
        body = json.dumps([_payload(i) for i in chunk]).encode()
        # ETag from the page body only: page 1 keeps its ETag as the thread grows.
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        last = max(1, -(-self.n // per_page))
        base = str(req.url.copy_with(query=None))
        links = []
        if page < last:
            links.append(f'<{base}?per_page={per_page}&page={page + 1}>; rel="next"')
            links.append(f'<{base}?per_page={per_page}&page={last}>; rel="last"')
        if page > 1:
            links.append(f'<{base}?per_page={per_page}&page={page - 1}>; rel="prev"')
            links.append(f'<{base}?per_page={per_page}&page=1>; rel="first"')
        headers = {"ETag": etag}
        if links:
            headers["Link"] = ", ".join(links)
        if req.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers=headers)
        headers["Content-Type"] = "application/json"
        return httpx.Response(200, headers=headers, content=body)


def _install_cached_mock(monkeypatch: pytest.MonkeyPatch, thread: _Thread) -> None:
    transport = ETagTransport(httpx.MockTransport(thread.handler))

    def fake_client(token: str | None) -> httpx.Client:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "test-agent"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE, headers=headers, transport=transport
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _tools(monkeypatch: pytest.MonkeyPatch) -> dict:
    def fake_load_projects(*_a, **_k):
        return ProjectsLoadResult(projects=[_project()], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(comment_tools, "load_projects", fake_load_projects)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    stub = _StubMCP()
    comment_tools.register(stub)
    return stub.tools


def _call(tools: dict, ticket: str, limit: int) -> tuple[list[str], bool]:
    res = tools["list_comments"](
        project_id="acme", ticket_id=ticket, order="desc", limit=limit
    )
    assert "error" not in res, res
    return [str(c["id"]) for c in res["comments"]], res["has_more"]


def test_desc_tail_slice_is_fresh_after_thread_grows(monkeypatch) -> None:
    """Driving test (R1). RED on lib v0.3.19: second call returns 9,8,7
    because the 304 on the page-1 probe replays the cached Link rel=last=3."""
    thread = _Thread(9)
    _install_cached_mock(monkeypatch, thread)
    tools = _tools(monkeypatch)

    assert _call(tools, "101", 3)[0] == ["9", "8", "7"]

    thread.grow_to(14)
    ids, has_more = _call(tools, "101", 3)
    assert ids == ["14", "13", "12"]
    assert has_more is True

    thread.grow_to(15)
    assert _call(tools, "101", 3)[0] == ["15", "14", "13"]


def test_exact_multiple_after_growth(monkeypatch) -> None:
    thread = _Thread(6)
    _install_cached_mock(monkeypatch, thread)
    tools = _tools(monkeypatch)
    assert _call(tools, "102", 3)[0] == ["6", "5", "4"]
    thread.grow_to(12)
    ids, has_more = _call(tools, "102", 3)
    assert ids == ["12", "11", "10"]
    assert has_more is True


def test_limit_larger_than_thread_after_growth(monkeypatch) -> None:
    thread = _Thread(4)
    _install_cached_mock(monkeypatch, thread)
    tools = _tools(monkeypatch)
    assert _call(tools, "103", 50)[0] == ["4", "3", "2", "1"]
    thread.grow_to(5)
    ids, has_more = _call(tools, "103", 50)
    assert ids == ["5", "4", "3", "2", "1"]
    assert has_more is False


def test_has_more_false_on_replayed_304_page(monkeypatch) -> None:
    """Page-1 probe genuinely 304s (thread unchanged): has_more must stay
    False when the whole thread fits in the slice."""
    thread = _Thread(3)
    _install_cached_mock(monkeypatch, thread)
    tools = _tools(monkeypatch)
    assert _call(tools, "104", 3) == (["3", "2", "1"], False)
    assert _call(tools, "104", 3) == (["3", "2", "1"], False)
    assert _call(tools, "104", 50) == (["3", "2", "1"], False)
    assert _call(tools, "104", 50) == (["3", "2", "1"], False)


def test_has_more_true_on_replayed_304_page(monkeypatch) -> None:
    thread = _Thread(14)
    _install_cached_mock(monkeypatch, thread)
    tools = _tools(monkeypatch)
    assert _call(tools, "105", 3) == (["14", "13", "12"], True)
    assert _call(tools, "105", 3) == (["14", "13", "12"], True)
