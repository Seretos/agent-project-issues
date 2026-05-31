"""Response-shape contract for the `update_ticket` tool (ticket #118).

The MCP tool wrapper now returns the full ticket object, matching the
`create_ticket` contract. `body` and `title` ARE echoed back from the
provider's post-update dataclass.

Provider behaviour (`GitHubProvider.update_ticket`) is unchanged and
returns a full `Ticket` dataclass; the tool-layer now passes all fields
through via `asdict(ticket)`.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import Ticket
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import tickets as ticket_tools


def _project() -> ProjectConfig:
    # Permissions: issues.modify=True is required for update_ticket.
    from lib_python_projects import IssuesPermissions, Permissions
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
        ),
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools_with_mock_provider(
    monkeypatch: pytest.MonkeyPatch,
    returned_ticket: Ticket,
) -> dict[str, Callable]:
    """Wire up update_ticket against a mock provider so we can pin the
    response shape without touching httpx."""
    project = _project()

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "ghp_token")

    captured_kwargs: dict = {}

    class _MockProvider:
        def update_ticket(self, project_, token, ticket_id, **kwargs):
            captured_kwargs.update(kwargs)
            captured_kwargs["_ticket_id"] = ticket_id
            captured_kwargs["_token"] = token
            return returned_ticket

    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _MockProvider())

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools, captured_kwargs


def _full_ticket(body: str = "x" * 5000) -> Ticket:
    """Build a Ticket with a deliberately-large body."""
    return Ticket(
        id="5",
        title="updated title",
        body=body,
        status="closed:completed",
        author="alice",
        assignees=["bob"],
        labels=["ai-generated", "ai-modified"],
        url="https://example.test/issues/5",
        created_at="2026-05-18T10:00:00Z",
        updated_at="2026-05-18T20:36:48Z",
    )


# ---------- ticket #118: full echo response shape ----------------------------


def test_update_ticket_response_includes_title_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool-level response must echo back `title` and `body` (fix #118)."""
    tools, _ = _register_tools_with_mock_provider(monkeypatch, _full_ticket())
    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", status="closed:completed",
    )
    assert "ticket" in out
    ticket = out["ticket"]
    assert "title" in ticket
    assert "body" in ticket


def test_update_ticket_response_matches_full_ticket_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool response ticket dict must contain all fields from asdict(ticket)."""
    source_ticket = _full_ticket()
    tools, _ = _register_tools_with_mock_provider(monkeypatch, source_ticket)
    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", status="closed:completed",
    )
    ticket = out["ticket"]
    expected_fields = set(asdict(source_ticket).keys())
    for field in expected_fields:
        assert field in ticket, f"field '{field}' missing from update_ticket response"


def test_update_ticket_response_keeps_useful_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response keeps all fields agents actually need:
    id, title, body, status, labels, assignees, url, updated_at."""
    tools, _ = _register_tools_with_mock_provider(monkeypatch, _full_ticket())
    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", status="closed:completed",
    )
    ticket = out["ticket"]
    assert ticket["id"] == "5"
    assert ticket["title"] == "updated title"
    assert ticket["body"] == "x" * 5000
    assert ticket["status"] == "closed:completed"
    assert ticket["labels"] == ["ai-generated", "ai-modified"]
    assert ticket["assignees"] == ["bob"]
    assert ticket["url"] == "https://example.test/issues/5"
    assert ticket["updated_at"] == "2026-05-18T20:36:48Z"


def test_update_ticket_passes_through_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: the tool still forwards every argument to the
    provider. The response echo is purely on the response leg."""
    tools, captured = _register_tools_with_mock_provider(
        monkeypatch, _full_ticket(),
    )
    tools["update_ticket"](
        project_id="acme",
        ticket_id="5",
        title="new title",
        body="new body",
        status="closed:completed",
        labels_add=["bug"],
        labels_remove=["wontfix"],
        assignees_add=["alice"],
        assignees_remove=["bob"],
    )
    assert captured["_ticket_id"] == "5"
    assert captured["_token"] == "ghp_token"
    assert captured["title"] == "new title"
    assert captured["body"] == "new body"
    assert captured["status"] == "closed:completed"
    assert captured["labels_add"] == ["bug"]
    assert captured["labels_remove"] == ["wontfix"]
    assert captured["assignees_add"] == ["alice"]
    assert captured["assignees_remove"] == ["bob"]


def test_update_ticket_reflects_server_mutated_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server-mutated labels (e.g. the auto-added `ai-modified` marker)
    MUST flow through to the response — that's a real signal the caller
    didn't supply."""
    # Caller passed no labels_add, but the server-side provider may have
    # auto-added `ai-modified`. The full response reflects that.
    server_returned = _full_ticket()
    # `server_returned.labels == ["ai-generated", "ai-modified"]` —
    # ai-modified was injected by the provider, not the caller.
    tools, _ = _register_tools_with_mock_provider(monkeypatch, server_returned)
    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", status="closed:completed",
    )
    assert "ai-modified" in out["ticket"]["labels"]
