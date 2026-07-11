"""Tests for tickets #171 / #172: `area_path`/`area_path_recursive` and
`states` filter parameters wired into `list_tickets` and
`list_tickets_across_projects`.

Uses a mock provider registered into `_providers._PROVIDERS` (mirrors
test_ticket_fields_167_168.py) rather than httpx mocking — the tool
layer only needs to prove it builds the right `TicketFilters` and
propagates provider errors correctly; the lib itself already has its
own coverage for WIQL/REST translation.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import bulk as bulk_tools
from project_issues_plugin.tools import tickets as ticket_tools


def _project(project_id: str = "acme", provider: str = "azuredevops") -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class _CapturingProvider:
    """Records the TicketFilters it was called with; optionally raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.captured_filters = None
        self._error = error

    def list_tickets(self, project_, token, filters):
        self.captured_filters = filters
        if self._error is not None:
            raise self._error
        return [], False


def _register_list_tickets(
    monkeypatch: pytest.MonkeyPatch, provider_instance, project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


def _register_bulk(
    monkeypatch: pytest.MonkeyPatch, provider_instance, projects: list[ProjectConfig],
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=projects, state="ok", search_root="/tmp")

    monkeypatch.setattr(bulk_tools, "load_projects", fake_load_projects)
    for p in projects:
        monkeypatch.setitem(providers_mod._PROVIDERS, p.provider, provider_instance)

    stub = _StubMCP()
    bulk_tools.register(stub)
    return stub.tools


# ---------------------------------------------------------------------------
# list_tickets — #172 states
# ---------------------------------------------------------------------------


def test_list_tickets_states_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingProvider()
    tools = _register_list_tickets(monkeypatch, provider, project)

    out = tools["list_tickets"](project_id="acme", states=["New", "Approved"])

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.captured_filters.states == ["New", "Approved"]


def test_list_tickets_states_defaults_to_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingProvider()
    tools = _register_list_tickets(monkeypatch, provider, project)

    tools["list_tickets"](project_id="acme")

    assert provider.captured_filters.states == []


def test_list_tickets_unknown_state_surfaces_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ValueError from the provider (unknown native state) surfaces as
    {"error": ...} via _safe, not a traceback."""
    project = _project()
    provider = _CapturingProvider(
        error=ValueError("unsupported status 'Bogus' for Azure DevOps — use list_ticket_statuses")
    )
    tools = _register_list_tickets(monkeypatch, provider, project)

    out = tools["list_tickets"](project_id="acme", states=["Bogus"])

    assert "error" in out, f"expected error dict; got: {out}"
    assert "list_ticket_statuses" in out["error"]


# ---------------------------------------------------------------------------
# list_tickets — #171 area_path / area_path_recursive
# ---------------------------------------------------------------------------


def test_list_tickets_area_path_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingProvider()
    tools = _register_list_tickets(monkeypatch, provider, project)

    out = tools["list_tickets"](
        project_id="acme", area_path="Proj\\TeamA", area_path_recursive=False,
    )

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.captured_filters.area_path == "Proj\\TeamA"
    assert provider.captured_filters.area_path_recursive is False


def test_list_tickets_area_path_recursive_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingProvider()
    tools = _register_list_tickets(monkeypatch, provider, project)

    tools["list_tickets"](project_id="acme", area_path="Proj\\TeamA")

    assert provider.captured_filters.area_path_recursive is True


def test_list_tickets_area_path_on_github_surfaces_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """area_path on a non-Azure-DevOps provider raises ValueError in the
    lib; the tool must surface it as {"error": ...}, not a traceback."""
    project = _project(provider="github")
    provider = _CapturingProvider(
        error=ValueError(
            "area_path is not supported on GitHub — it is an Azure DevOps "
            "System.AreaPath filter"
        )
    )
    tools = _register_list_tickets(monkeypatch, provider, project)

    out = tools["list_tickets"](project_id="acme", area_path="Proj\\TeamA")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "Azure DevOps" in out["error"]


def test_list_tickets_area_path_none_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingProvider()
    tools = _register_list_tickets(monkeypatch, provider, project)

    tools["list_tickets"](project_id="acme")

    assert provider.captured_filters.area_path is None


# ---------------------------------------------------------------------------
# list_tickets_across_projects — same params, per-project error tolerance
# ---------------------------------------------------------------------------


def test_bulk_states_and_area_path_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingProvider()
    tools = _register_bulk(monkeypatch, provider, [project])

    out = tools["list_tickets_across_projects"](
        project_ids=["acme"], states=["Active"], area_path="Proj\\TeamB",
        area_path_recursive=False,
    )

    assert out["results"]["acme"]["error"] is None
    assert provider.captured_filters.states == ["Active"]
    assert provider.captured_filters.area_path == "Proj\\TeamB"
    assert provider.captured_filters.area_path_recursive is False


def test_bulk_area_path_error_is_per_project_not_top_level_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError raised by one project's provider (e.g. area_path on a
    non-AzDO project) must land in that project's `error` entry — the
    call must not raise and abort the whole batch."""
    project = _project(provider="github")
    provider = _CapturingProvider(
        error=ValueError(
            "area_path is not supported on GitHub — it is an Azure DevOps "
            "System.AreaPath filter"
        )
    )
    tools = _register_bulk(monkeypatch, provider, [project])

    out = tools["list_tickets_across_projects"](
        project_ids=["acme"], area_path="Proj\\TeamA",
    )

    assert out["results"]["acme"]["error"] is not None
    assert "Azure DevOps" in out["results"]["acme"]["error"]
    assert out["results"]["acme"]["tickets"] == []
    assert out["errors"] == [
        {"project_id": "acme", "error": out["results"]["acme"]["error"]}
    ]
    assert out["total_tickets"] == 0
