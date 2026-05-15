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

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
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
    from project_issues_plugin import config as cfg_mod

    def fake_load_projects(cwd=None):
        return cfg_mod.LoadResult(
            projects=projects,
            state="ok",
            search_root="/tmp",
        )

    monkeypatch.setattr(cfg_mod, "load_projects", fake_load_projects)
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

    result = tools["list_tickets_across_projects"]()

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


def test_bulk_none_resolves_to_all_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`project_ids=None` must fan out to every configured project."""
    a = _project("a", repo="repo-a")
    b = _project("b", repo="repo-b")
    tools = _register_tools_with(monkeypatch, [a, b])

    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path == "/repos/acme/repo-a/issues":
            return _json([_issue_payload(10)])
        if req.url.path == "/repos/acme/repo-b/issues":
            return _json([_issue_payload(20), _issue_payload(21)])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    result = tools["list_tickets_across_projects"](project_ids=None)

    assert set(result["results"].keys()) == {"a", "b"}
    assert result["project_count"] == 2
    assert result["total_tickets"] == 3
    assert result["errors"] == []
    # Both projects were queried.
    assert "/repos/acme/repo-a/issues" in seen_paths
    assert "/repos/acme/repo-b/issues" in seen_paths


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
