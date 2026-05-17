"""Tests for the new provider-native status discovery surface
(ticket #7).

Covers:
- `list_ticket_statuses` returns the GitHub-static StatusSpec with
  internally consistent hints.
- Cache TTL: a second call within the TTL window does not invoke the
  provider again.
- `update_ticket` accepts the new suffix-encoded GitHub statuses and
  rejects legacy enum values (`completed`, `not_planned`) with a
  clear error.
- `_map_issue` round-trips the new status strings.
"""
from __future__ import annotations

from typing import Callable

import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
from project_issues_plugin.providers.base import StatusSpec
from project_issues_plugin.providers.github import GitHubProvider
from project_issues_plugin.tools import tickets as ticket_tools


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools(monkeypatch: pytest.MonkeyPatch, project: ProjectConfig):
    from project_issues_plugin import config as cfg_mod

    def fake_load_projects(cwd=None):
        return cfg_mod.LoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(cfg_mod, "load_projects", fake_load_projects)
    ticket_tools._status_cache_clear()
    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


# ---------- GitHubProvider.list_statuses -------------------------------------


def test_github_list_statuses_is_static_and_self_consistent() -> None:
    spec = GitHubProvider().list_statuses(_project(), token=None)
    assert isinstance(spec, StatusSpec)
    # Every hint value must be present in the `values` list.
    assert spec.hints["default_open"] in spec.values
    assert spec.hints["terminal_completed"] in spec.values
    assert spec.hints["terminal_declined"] in spec.values
    for v in spec.hints["terminal"]:
        assert v in spec.values
    # Transitions reference only known values on both sides.
    for src, dsts in spec.transitions.items():
        assert src in spec.values
        for dst in dsts:
            assert dst in spec.values


# ---------- list_ticket_statuses tool + TTL cache ----------------------------


def test_list_ticket_statuses_tool_payload_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools(monkeypatch, _project())
    out = tools["list_ticket_statuses"](project_id="acme")
    assert out["project_id"] == "acme"
    assert out["provider"] == "github"
    assert "closed:completed" in out["values"]
    assert "closed:not_planned" in out["values"]
    assert out["hints"]["terminal_completed"] == "closed:completed"
    assert out["hints"]["terminal_declined"] == "closed:not_planned"


def test_list_ticket_statuses_caches_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools(monkeypatch, _project())
    calls: dict[str, int] = {"n": 0}

    real = GitHubProvider.list_statuses

    def counting(self, project, token):
        calls["n"] += 1
        return real(self, project, token)

    monkeypatch.setattr(GitHubProvider, "list_statuses", counting)

    tools["list_ticket_statuses"](project_id="acme")
    tools["list_ticket_statuses"](project_id="acme")
    tools["list_ticket_statuses"](project_id="acme")
    assert calls["n"] == 1


def test_list_ticket_statuses_refreshes_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools(monkeypatch, _project())
    calls: dict[str, int] = {"n": 0}
    real = GitHubProvider.list_statuses

    def counting(self, project, token):
        calls["n"] += 1
        return real(self, project, token)

    monkeypatch.setattr(GitHubProvider, "list_statuses", counting)

    # Move "now" past the TTL by patching `time.time` used by the tool.
    base = 1_000_000.0
    state = {"t": base}
    monkeypatch.setattr(ticket_tools.time, "time", lambda: state["t"])
    tools["list_ticket_statuses"](project_id="acme")
    state["t"] = base + ticket_tools._STATUS_CACHE_TTL_SECONDS + 1
    tools["list_ticket_statuses"](project_id="acme")
    assert calls["n"] == 2


# ---------- _map_issue round-trip --------------------------------------------


def test_map_issue_emits_new_status_strings() -> None:
    open_t = github_provider._map_issue(
        {"number": 1, "state": "open", "title": "x", "body": "",
         "html_url": "", "created_at": "", "updated_at": ""}
    )
    assert open_t.status == "open"

    closed_completed = github_provider._map_issue(
        {"number": 2, "state": "closed", "state_reason": "completed",
         "title": "x", "body": "", "html_url": "",
         "created_at": "", "updated_at": ""}
    )
    assert closed_completed.status == "closed:completed"

    closed_decl = github_provider._map_issue(
        {"number": 3, "state": "closed", "state_reason": "not_planned",
         "title": "x", "body": "", "html_url": "",
         "created_at": "", "updated_at": ""}
    )
    assert closed_decl.status == "closed:not_planned"


# ---------- update_ticket rejects legacy enum --------------------------------


def test_update_ticket_provider_rejects_legacy_status() -> None:
    """The new string API rejects the old `completed`/`not_planned`
    enum values — agents must migrate to `closed:completed` /
    `closed:not_planned` or call `list_ticket_statuses` for hints.
    """
    import httpx

    def handler(request):
        if request.method == "GET" and "/issues/1" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "number": 1, "title": "x", "body": "",
                    "state": "open", "user": {"login": "alice"},
                    "assignees": [],
                    # Has ai-generated already so no extra label
                    # ensure/POST happens before the status parse.
                    "labels": [{"name": "ai-generated"}],
                    "html_url": "", "created_at": "", "updated_at": "",
                },
            )
        if request.method == "PATCH":
            raise AssertionError(
                "PATCH must not happen — ValueError should fire first"
            )
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)

    def fake_client(token):
        return httpx.Client(
            base_url=github_provider.API_BASE, transport=transport,
        )

    import unittest.mock as _m
    with _m.patch.object(github_provider, "_client", fake_client):
        with pytest.raises(ValueError, match="unsupported status"):
            GitHubProvider().update_ticket(
                _project(), token=None, ticket_id="1",
                status="completed",  # legacy value
            )


def test_update_ticket_provider_accepts_new_suffix_status() -> None:
    """`closed:not_planned` flows through to GitHub as
    `state=closed`, `state_reason=not_planned`.
    """
    import httpx

    sent: dict[str, dict] = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "number": 5, "title": "x", "body": "",
                    "state": "open", "user": {"login": "alice"},
                    "assignees": [],
                    "labels": [{"name": "ai-generated"}],
                    "html_url": "", "created_at": "", "updated_at": "",
                },
            )
        if request.method == "PATCH":
            import json as _j
            sent["payload"] = _j.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "number": 5, "title": "x", "body": "",
                    "state": "closed", "state_reason": "not_planned",
                    "user": {"login": "alice"},
                    "assignees": [],
                    "labels": [{"name": "ai-modified"}],
                    "html_url": "", "created_at": "", "updated_at": "",
                },
            )
        raise AssertionError(f"unexpected: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)

    def fake_client(token):
        return httpx.Client(
            base_url=github_provider.API_BASE, transport=transport,
        )

    import unittest.mock as _m
    with _m.patch.object(github_provider, "_client", fake_client):
        ticket = GitHubProvider().update_ticket(
            _project(), token="t", ticket_id="5",
            status="closed:not_planned",
        )

    assert sent["payload"]["state"] == "closed"
    assert sent["payload"]["state_reason"] == "not_planned"
    assert ticket.status == "closed:not_planned"
