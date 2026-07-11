"""Tests for tickets #169 / #170: the new `list_board_columns` tool and
the `column` filter on `list_tickets` / `list_tickets_across_projects`.

Uses mock providers registered into `_providers._PROVIDERS` (mirrors
test_ticket_fields_167_168.py / test_list_filters_171_172.py) — the lib
itself already covers the GraphQL/WIQL column-resolution logic; the
tool layer only needs to prove it calls through correctly, handles a
provider with no board concept (GitLab) gracefully, and surfaces
provider errors as data.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import BoardColumnSpec
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


def _register_tickets(
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
# list_board_columns
# ---------------------------------------------------------------------------


class _MockBoardProvider:
    def __init__(self, columns=None, error: Exception | None = None) -> None:
        self._columns = columns or []
        self._error = error
        self.called_with: tuple | None = None

    def list_board_columns(self, project_, token):
        self.called_with = (project_, token)
        if self._error is not None:
            raise self._error
        return self._columns


def test_list_board_columns_azure_returns_resolved_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockBoardProvider(columns=[
        BoardColumnSpec(
            logical="Approved", native="Approved", option_id="col-2",
            states=("Approved",), is_split=False,
        ),
        BoardColumnSpec(
            logical="Doing", native="Doing", option_id="col-3",
            states=("Committed", "In Progress"), is_split=True,
        ),
    ])
    tools = _register_tickets(monkeypatch, provider, _project())

    out = tools["list_board_columns"](project_id="acme")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["provider"] == "azuredevops"
    assert len(out["columns"]) == 2
    first = out["columns"][0]
    assert first == {
        "logical": "Approved", "native": "Approved", "option_id": "col-2",
        "states": ["Approved"], "is_split": False,
    }
    assert out["columns"][1]["is_split"] is True
    assert out["columns"][1]["states"] == ["Committed", "In Progress"]


def test_list_board_columns_github_states_always_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub columns carry no System.State mapping — states=[], is_split=False."""
    provider = _MockBoardProvider(columns=[
        BoardColumnSpec(logical="Approved", native="Approved", option_id="opt-1"),
    ])
    tools = _register_tickets(
        monkeypatch, provider, _project(provider="github"),
    )

    out = tools["list_board_columns"](project_id="acme")

    assert out["columns"][0]["states"] == []
    assert out["columns"][0]["is_split"] is False


def test_list_board_columns_gitlab_has_no_method_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider with no list_board_columns method (GitLab) must not
    raise AttributeError — it's a stable 'no board concept' fact."""
    class _NoBoardProvider:
        pass

    tools = _register_tickets(
        monkeypatch, _NoBoardProvider(), _project(provider="gitlab"),
    )

    out = tools["list_board_columns"](project_id="acme")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["provider"] == "gitlab"
    assert out["columns"] == []


def test_list_board_columns_missing_board_config_surfaces_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError from the provider (no board configured) surfaces as
    {"error": ...} via _safe, not a traceback."""
    provider = _MockBoardProvider(
        error=ValueError(
            "project 'acme' has no 'board' configuration — add one to "
            "projects.yml before calling list_board_columns"
        )
    )
    tools = _register_tickets(monkeypatch, provider, _project())

    out = tools["list_board_columns"](project_id="acme")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "board" in out["error"]


# ---------------------------------------------------------------------------
# list_tickets — column filter
# ---------------------------------------------------------------------------


class _CapturingTicketsProvider:
    def __init__(self, error: Exception | None = None) -> None:
        self.captured_filters = None
        self._error = error

    def list_tickets(self, project_, token, filters):
        self.captured_filters = filters
        if self._error is not None:
            raise self._error
        return [], False


def test_list_tickets_column_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingTicketsProvider()
    tools = _register_tickets(monkeypatch, provider, project)

    out = tools["list_tickets"](project_id="acme", column="Approved")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.captured_filters.board_column == "Approved"


def test_list_tickets_column_defaults_none(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingTicketsProvider()
    tools = _register_tickets(monkeypatch, provider, project)

    tools["list_tickets"](project_id="acme")

    assert provider.captured_filters.board_column is None


def test_list_tickets_column_on_gitlab_surfaces_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """column on GitLab raises ValueError in the lib; the tool must
    surface it as {"error": ...}, not a traceback."""
    project = _project(provider="gitlab")
    provider = _CapturingTicketsProvider(
        error=ValueError(
            "board_column is not supported on GitLab — it is a GitHub "
            "Projects v2 board filter"
        )
    )
    tools = _register_tickets(monkeypatch, provider, project)

    out = tools["list_tickets"](project_id="acme", column="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "GitLab" in out["error"]


# ---------------------------------------------------------------------------
# list_tickets_across_projects — column filter, per-project isolation
# ---------------------------------------------------------------------------


def test_bulk_column_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project()
    provider = _CapturingTicketsProvider()
    tools = _register_bulk(monkeypatch, provider, [project])

    out = tools["list_tickets_across_projects"](
        project_ids=["acme"], column="Approved",
    )

    assert out["results"]["acme"]["error"] is None
    assert provider.captured_filters.board_column == "Approved"


def test_bulk_column_error_is_per_project_not_top_level_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError raised by one project's provider (column on GitLab)
    must land in that project's `error` entry, not abort the batch."""
    project = _project(provider="gitlab")
    provider = _CapturingTicketsProvider(
        error=ValueError(
            "board_column is not supported on GitLab — it is a GitHub "
            "Projects v2 board filter"
        )
    )
    tools = _register_bulk(monkeypatch, provider, [project])

    out = tools["list_tickets_across_projects"](
        project_ids=["acme"], column="Approved",
    )

    assert out["results"]["acme"]["error"] is not None
    assert "GitLab" in out["results"]["acme"]["error"]
    assert out["results"]["acme"]["tickets"] == []
    assert out["errors"] == [
        {"project_id": "acme", "error": out["results"]["acme"]["error"]}
    ]
