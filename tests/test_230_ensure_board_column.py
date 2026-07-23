"""Tests for ticket agent-project-issues#230: the new `ensure_board_column`
gated write tool.

Mirrors the `_StubMCP` + `register(...)` + `_PROVIDERS` monkey-patch pattern
used by `tests/test_board_columns_169_170.py` (read-only board tool) and
`tests/test_pulls.py` (permission-gated write tool, e.g. `merge_pr`). The
lib itself already covers the GitHub/Azure column-creation logic — this
module only proves the tool layer gates on `board.manage`, dispatches to
the provider with the right arguments, and translates provider-shaped
results/errors into `{"error": ...}` without ever leaking a traceback.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.base import BoardColumnSpec
from lib_python_projects.providers.github import GitHubError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import tickets as ticket_tools


def _project(
    project_id: str = "acme",
    provider: str = "github",
    *,
    board_manage: bool | None = False,
    no_board_block: bool = False,
) -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    permissions: dict = {
        "issues": {"create": True, "modify": True},
        "pulls": {"create": True, "modify": True, "merge": True},
    }
    if not no_board_block:
        permissions["board"] = {"manage": bool(board_manage)}
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=permissions,
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register(
    monkeypatch: pytest.MonkeyPatch, provider_instance, project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)
    monkeypatch.setenv(f"TOKEN_{project.id.upper()}", "tok")

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


class _MockEnsureBoardProvider:
    """Provider stub exposing `ensure_board_column` with a configurable
    return value or raised error, mirroring `_MockBoardProvider` in
    test_board_columns_169_170.py."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.called_with: tuple | None = None

    def ensure_board_column(self, project_, token, column_name):
        self.called_with = (project_, token, column_name)
        if self._error is not None:
            raise self._error
        return self._result


class _NoBoardSupportProvider:
    """A provider stub with no `ensure_board_column` method at all —
    mirrors GitLab's lack of a board concept."""


# ---------------------------------------------------------------------------
# Behaviour 1 — dispatches to the provider when board.manage is true.
# ---------------------------------------------------------------------------


def test_ensure_board_column_dispatches_when_manage_true_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockEnsureBoardProvider(result=True)
    project = _project(board_manage=True)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.called_with == (project, "tok", "Approved")
    assert out["project_id"] == "acme"
    assert out["provider"] == "github"
    assert out["column_name"] == "Approved"
    assert out["created"] is True


def test_ensure_board_column_dispatches_when_manage_true_azure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = BoardColumnSpec(
        logical="Approved", native="Approved", option_id="col-2",
        states=("Approved",), is_split=False,
    )
    provider = _MockEnsureBoardProvider(result=spec)
    project = _project(provider="azuredevops", board_manage=True)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.called_with == (project, "tok", "Approved")
    assert out["column"] == {
        "logical": "Approved", "native": "Approved", "option_id": "col-2",
        "states": ["Approved"], "is_split": False,
    }


# ---------------------------------------------------------------------------
# Behaviour 2 — denied when board.manage is false, or entirely absent.
# ---------------------------------------------------------------------------


def test_ensure_board_column_denied_when_manage_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockEnsureBoardProvider(result=True)
    project = _project(board_manage=False)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "board.manage" in out["error"]
    assert provider.called_with is None


def test_ensure_board_column_denied_when_no_board_permissions_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `permissions.board:` block at all resolves to the same
    manage=False default — must be rejected identically."""
    provider = _MockEnsureBoardProvider(result=True)
    project = _project(no_board_block=True)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "board.manage" in out["error"]
    assert provider.called_with is None


# ---------------------------------------------------------------------------
# Behaviour 3 — unsupported provider (GitLab has no board concept).
# ---------------------------------------------------------------------------


def test_ensure_board_column_unsupported_provider_surfaces_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(provider="gitlab", board_manage=True)
    tools = _register(monkeypatch, _NoBoardSupportProvider(), project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "not support" in out["error"].lower()


# ---------------------------------------------------------------------------
# Behaviour 4 — a lib provider error surfaces as data, never a traceback.
# ---------------------------------------------------------------------------


def test_ensure_board_column_github_error_surfaces_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockEnsureBoardProvider(
        error=GitHubError(422, "field is not a single-select field"),
    )
    project = _project(board_manage=True)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "single-select" in out["error"]


def test_ensure_board_column_azure_error_surfaces_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockEnsureBoardProvider(
        error=AzureDevOpsError(400, "board PUT rejected"),
    )
    project = _project(provider="azuredevops", board_manage=True)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "board PUT rejected" in out["error"]


def test_ensure_board_column_value_error_surfaces_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError from the provider (e.g. no `board` configured)
    surfaces as {"error": ...} via _safe, not a traceback."""
    provider = _MockEnsureBoardProvider(
        error=ValueError(
            "project 'acme' has no 'board' configuration — add one to "
            "projects.yml before calling ensure_board_column"
        )
    )
    project = _project(board_manage=True)
    tools = _register(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "board" in out["error"]
