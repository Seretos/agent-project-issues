"""Tests for the bulk-listing tool (`list_tickets_across_projects`).

Uses `httpx.MockTransport` to intercept HTTP calls and a stubbed
`load_projects` so the tool sees a deterministic project set. The
partial-failure semantics are the primary focus: one bad project must
not poison the rest of the response.
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


# ---------- helpers ----------------------------------------------------------


def _project(
    project_id: str,
    *,
    owner: str = "acme",
    repo: str | None = None,
    modify: bool = True,
    token_env: str | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        id=project_id,
        provider="github",
        path=f"{owner}/{repo or project_id}",
        token_env=token_env or f"GITHUB_TOKEN_{project_id.upper()}",
        permissions={
            "issues": {"create": True, "modify": modify},
        },
    )


def _issue_payload(issue_id: int, title: str = "issue", **overrides) -> dict:
    base = {
        "number": issue_id,
        "title": title,
        "body": "",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/repo/issues/{issue_id}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
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


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools_with(
    monkeypatch: pytest.MonkeyPatch, projects: list[ProjectConfig]
):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=projects,
            state="ok",
            search_root="/tmp",
        )

    # `_providers.load_projects` is the module-level name `_resolve`
    # reads through (via the indirection shim). Patching it routes every
    # tool call through the fake list. `bulk.list_tickets_across_projects`
    # calls `bulk.load_projects` directly, so patch that too.
    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(bulk_tools, "load_projects", fake_load_projects)

    stub = _StubMCP()
    bulk_tools.register(stub)
    return stub.tools


# ---------- tests ------------------------------------------------------------


def test_bulk_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three projects: one with tickets, one 403, one empty.

    The successful + empty projects must contribute to `results`,
    `total_tickets` must be the sum of successful tickets only, and the
    403 project must show up in BOTH `results[pid]["error"]` and the
    top-level `errors` list.
    """
    proj_ok = _project("proj-ok", repo="repo-ok")
    proj_fail = _project("proj-fail", repo="repo-fail")
    proj_empty = _project("proj-empty", repo="repo-empty")

    tools = _register_tools_with(monkeypatch, [proj_ok, proj_fail, proj_empty])

    # Tokens are optional for read; set them so the path is exercised.
    monkeypatch.setenv("GITHUB_TOKEN_PROJ-OK", "tok-ok")
    monkeypatch.setenv("GITHUB_TOKEN_PROJ-FAIL", "tok-fail")
    monkeypatch.setenv("GITHUB_TOKEN_PROJ-EMPTY", "tok-empty")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/repo-ok/issues":
            return _json([
                _issue_payload(1, title="first"),
                _issue_payload(2, title="second"),
            ])
        if path == "/repos/acme/repo-fail/issues":
            return _json(
                {"message": "Resource not accessible"},
                status_code=403,
            )
        if path == "/repos/acme/repo-empty/issues":
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["list_tickets_across_projects"](
        project_ids=["proj-ok", "proj-fail", "proj-empty"]
    )

    # All three projects appear in results.
    assert set(result["results"].keys()) == {"proj-ok", "proj-fail", "proj-empty"}

    # Successful project: 2 tickets, no error.
    assert result["results"]["proj-ok"]["error"] is None
    assert len(result["results"]["proj-ok"]["tickets"]) == 2
    assert [t["id"] for t in result["results"]["proj-ok"]["tickets"]] == ["1", "2"]

    # Empty project: tickets=[], error=None.
    assert result["results"]["proj-empty"]["error"] is None
    assert result["results"]["proj-empty"]["tickets"] == []

    # Failing project: tickets=[], error set.
    assert result["results"]["proj-fail"]["error"] is not None
    assert result["results"]["proj-fail"]["tickets"] == []

    # Top-level errors carries exactly the failing project.
    assert len(result["errors"]) == 1
    assert result["errors"][0]["project_id"] == "proj-fail"
    assert isinstance(result["errors"][0]["error"], str)

    # total_tickets only counts successful projects.
    assert result["total_tickets"] == 2
    # project_count counts all attempted, including failed.
    assert result["project_count"] == 3


def test_bulk_none_project_ids_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`project_ids=None` (the default) must return an error, not fan out."""
    a = _project("a", repo="repo-a")
    b = _project("b", repo="repo-b")
    tools = _register_tools_with(monkeypatch, [a, b])

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["list_tickets_across_projects"](project_ids=None)

    assert "error" in result
    assert "project_ids" in result["error"]
    # No results key — the guard fires before any provider call.
    assert "results" not in result


def test_bulk_tickets_carry_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each ticket row in a bulk response must have `project_id` equal to
    its containing project key (ticket #118 fix 5)."""
    proj_a = _project("proj-a", repo="repo-a")
    proj_b = _project("proj-b", repo="repo-b")

    tools = _register_tools_with(monkeypatch, [proj_a, proj_b])

    monkeypatch.setenv("GITHUB_TOKEN_PROJ-A", "tok-a")
    monkeypatch.setenv("GITHUB_TOKEN_PROJ-B", "tok-b")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/repo-a/issues":
            return _json([
                _issue_payload(10, title="alpha"),
                _issue_payload(11, title="beta"),
            ])
        if path == "/repos/acme/repo-b/issues":
            return _json([
                _issue_payload(20, title="gamma"),
            ])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["list_tickets_across_projects"](
        project_ids=["proj-a", "proj-b"]
    )

    for ticket in result["results"]["proj-a"]["tickets"]:
        assert ticket["project_id"] == "proj-a", (
            f"expected project_id='proj-a', got {ticket.get('project_id')!r}"
        )
    for ticket in result["results"]["proj-b"]["tickets"]:
        assert ticket["project_id"] == "proj-b", (
            f"expected project_id='proj-b', got {ticket.get('project_id')!r}"
        )


def test_bulk_unknown_project_id_surfaces_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown project id must produce an `unknown project` error
    entry rather than raising."""
    a = _project("a", repo="repo-a")
    tools = _register_tools_with(monkeypatch, [a])

    def handler(req: httpx.Request) -> httpx.Response:
        # No HTTP call expected for the bogus id; the real project also
        # isn't queried here because we only ask for the missing one.
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["list_tickets_across_projects"](project_ids=["nonexistent"])

    assert set(result["results"].keys()) == {"nonexistent"}
    entry = result["results"]["nonexistent"]
    assert entry["tickets"] == []
    assert entry["error"] == "unknown project"
    assert result["total_tickets"] == 0
    assert result["project_count"] == 1
    assert result["errors"] == [
        {"project_id": "nonexistent", "error": "unknown project"}
    ]
