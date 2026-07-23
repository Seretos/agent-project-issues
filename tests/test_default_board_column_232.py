"""Tests for ticket #232: default board-column assignment on create_ticket.

Before this fix, a GitHub Projects-v2 `board:` binding left a new ticket
off the board unless the caller already knew to pass
`custom_fields={"Status": <native>}` — the gap that left
agent-worktree#93/#94 invisible for 3 days. `create_ticket` now defaults
a new ticket onto the board's first configured logical column
(`project.board.columns[0]`) when the caller omits the board-column
value, opt-outable via `off_board=True`, best-effort (a resolution
failure never blocks ticket creation).

Mirrors the fixtures in tests/test_board_columns_169_170.py (`_StubMCP`,
monkeypatched `providers_mod.load_projects`, a fake provider injected
into `providers_mod._PROVIDERS`) and the fake `create_ticket` signature
from tests/test_ticket_fields_167_168.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from lib_python_projects import (
    AzureBoardsBinding,
    Board,
    GithubProjectsV2Binding,
    IssuesPermissions,
    Permissions,
    ProjectConfig,
    ProjectsLoadResult,
)
from lib_python_projects.providers.base import BoardColumnSpec, Ticket
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import tickets as ticket_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _project(
    project_id: str = "acme",
    provider: str = "github",
    board: Board | None = None,
) -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(issues=IssuesPermissions(create=True)),
        board=board,
    )


def _github_board(
    columns: list[str] | None = None, status_field: str = "Status",
) -> Board:
    return Board(
        columns=columns or ["Todo", "Approved", "Doing", "Done"],
        binding=GithubProjectsV2Binding(
            kind="github-projects-v2", owner="acme", project_number=7,
            status_field=status_field,
        ),
    )


def _azure_board() -> Board:
    return Board(
        columns=["New", "Approved", "Doing", "Done"],
        binding=AzureBoardsBinding(kind="azure-boards", team="Web Team", board="Stories"),
    )


def _full_ticket(**overrides) -> Ticket:
    base = dict(
        id="42",
        title="some title",
        body="some body",
        status="open",
        author="alice",
        assignees=[],
        labels=[],
        url="https://example.test/issues/42",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
    )
    base.update(overrides)
    return Ticket(**base)


class _MockCreateTicketProvider:
    """Fake provider capturing create_ticket's custom_fields, plus a
    configurable list_board_columns (mirrors _MockBoardProvider in
    tests/test_board_columns_169_170.py)."""

    def __init__(
        self,
        ticket: Ticket | None = None,
        columns: list[BoardColumnSpec] | None = None,
        list_columns_error: Exception | None = None,
    ) -> None:
        self._ticket = ticket or _full_ticket()
        self._columns = columns if columns is not None else []
        self._list_columns_error = list_columns_error
        self.captured_custom_fields: dict | None = "UNSET"  # sentinel
        self.list_board_columns_called = False

    def create_ticket(self, project_, token, title, body, labels, assignees, *, status=None, custom_fields=None):
        self.captured_custom_fields = custom_fields
        return self._ticket

    def list_board_columns(self, project_, token):
        self.list_board_columns_called = True
        if self._list_columns_error is not None:
            raise self._list_columns_error
        return self._columns


def _register(
    monkeypatch: pytest.MonkeyPatch, provider_instance, project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)
    monkeypatch.setenv(project.token_env, "tok")

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


# ---------------------------------------------------------------------------
# Behaviour: defaults onto columns[0] when the caller omits the board column
# ---------------------------------------------------------------------------


def test_create_defaults_board_column_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(board=_github_board())
    provider = _MockCreateTicketProvider(columns=[
        BoardColumnSpec(logical="Todo", native="Todo", option_id="opt-1"),
        BoardColumnSpec(logical="Approved", native="Approved", option_id="opt-2"),
    ])
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](project_id="acme", title="t")

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.captured_custom_fields == {"Status": "Todo"}
    assert "board_warning" not in out


def test_create_defaults_board_column_non_default_status_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(board=_github_board(status_field="Column"))
    provider = _MockCreateTicketProvider(columns=[
        BoardColumnSpec(logical="Todo", native="Todo", option_id="opt-1"),
    ])
    tools = _register(monkeypatch, provider, project)

    tools["create_ticket"](project_id="acme", title="t")

    assert provider.captured_custom_fields == {"Column": "Todo"}


def test_create_defaults_board_column_native_differs_from_logical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(board=_github_board())
    provider = _MockCreateTicketProvider(columns=[
        BoardColumnSpec(logical="Todo", native="Backlog", option_id="opt-1"),
    ])
    tools = _register(monkeypatch, provider, project)

    tools["create_ticket"](project_id="acme", title="t")

    assert provider.captured_custom_fields == {"Status": "Backlog"}


def test_create_defaults_board_column_falls_back_when_no_logical_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """columns[0] ("Todo") doesn't resolve in the live board's columns —
    fall back to the first returned BoardColumnSpec rather than erroring."""
    project = _project(board=_github_board())
    provider = _MockCreateTicketProvider(columns=[
        BoardColumnSpec(logical="Backlog", native="Backlog", option_id="opt-1"),
    ])
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](project_id="acme", title="t")

    assert "error" not in out
    assert provider.captured_custom_fields == {"Status": "Backlog"}


# ---------------------------------------------------------------------------
# Behaviour: an explicit board-column value from the caller is respected
# ---------------------------------------------------------------------------


def test_create_respects_explicit_status(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(board=_github_board())
    provider = _MockCreateTicketProvider(columns=[
        BoardColumnSpec(logical="Todo", native="Todo", option_id="opt-1"),
    ])
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](
        project_id="acme", title="t", custom_fields={"Status": "In Progress"},
    )

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.captured_custom_fields == {"Status": "In Progress"}
    assert provider.list_board_columns_called is False


def test_create_merges_status_default_with_other_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied non-status custom field doesn't suppress the
    default; the status key is merged in and the caller's own key is
    preserved verbatim."""
    project = _project(board=_github_board())
    provider = _MockCreateTicketProvider(columns=[
        BoardColumnSpec(logical="Todo", native="Todo", option_id="opt-1"),
    ])
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](
        project_id="acme", title="t", custom_fields={"SomeOtherField": "x"},
    )

    assert "error" not in out
    assert provider.captured_custom_fields == {"SomeOtherField": "x", "Status": "Todo"}


# ---------------------------------------------------------------------------
# Behaviour: off_board opts out of the default entirely
# ---------------------------------------------------------------------------


def test_create_off_board_skips_default(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(board=_github_board())
    provider = _MockCreateTicketProvider(columns=[
        BoardColumnSpec(logical="Todo", native="Todo", option_id="opt-1"),
    ])
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](project_id="acme", title="t", off_board=True)

    assert "error" not in out
    assert provider.captured_custom_fields is None
    assert provider.list_board_columns_called is False
    assert "board_warning" not in out


# ---------------------------------------------------------------------------
# Behaviour: no default when there's nothing to default onto / wrong provider
# ---------------------------------------------------------------------------


def test_create_no_board_binding_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(board=None)
    provider = _MockCreateTicketProvider()
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](project_id="acme", title="t")

    assert "error" not in out
    assert provider.captured_custom_fields is None
    assert provider.list_board_columns_called is False


def test_create_azure_project_no_autodefault(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(project_id="ado", provider="azuredevops", board=_azure_board())
    provider = _MockCreateTicketProvider()
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](project_id="ado", title="t")

    assert "error" not in out
    assert provider.captured_custom_fields is None
    assert provider.list_board_columns_called is False


def test_create_gitlab_no_autodefault(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(project_id="gl", provider="gitlab", board=None)
    provider = _MockCreateTicketProvider()
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](project_id="gl", title="t")

    assert "error" not in out
    assert provider.captured_custom_fields is None
    assert provider.list_board_columns_called is False


# ---------------------------------------------------------------------------
# Behaviour: board-resolution failures are best-effort — never block creation
# ---------------------------------------------------------------------------


def test_create_board_resolution_failure_still_creates_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(board=_github_board())
    provider = _MockCreateTicketProvider(
        list_columns_error=ValueError("boom: could not reach GitHub GraphQL API"),
    )
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](project_id="acme", title="t")

    assert "error" not in out, f"unexpected error: {out}"
    assert "ticket" in out
    assert provider.captured_custom_fields is None  # no board write attempted
    assert "board_warning" in out
    assert "board" in out["board_warning"].lower()


def test_create_board_resolution_empty_columns_still_creates_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(board=_github_board())
    provider = _MockCreateTicketProvider(columns=[])
    tools = _register(monkeypatch, provider, project)

    out = tools["create_ticket"](project_id="acme", title="t")

    assert "error" not in out
    assert "ticket" in out
    assert provider.captured_custom_fields is None
    assert "board_warning" in out


# ---------------------------------------------------------------------------
# Docstring / Field-description regression coverage
# ---------------------------------------------------------------------------


def test_create_custom_fields_description_updated() -> None:
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    stub = _StubMCP()
    ticket_tools.register(stub)
    fn = stub.tools["create_ticket"]

    schema = func_metadata(fn).arg_model.model_json_schema()
    description = schema.get("properties", {}).get("custom_fields", {}).get("description", "")
    doc = fn.__doc__ or ""

    assert "always a no-op" not in description, (
        f"stale phrasing still present: {description!r}"
    )
    assert "off_board" in description
    assert "off_board" in doc
    assert "columns[0]" in description or "first configured column" in description
    off_board_prop = schema.get("properties", {}).get("off_board", {})
    assert off_board_prop, "off_board parameter must be registered on create_ticket"


# ---------------------------------------------------------------------------
# README regression — mirrors tests/test_194_board_docs.py's style of
# asserting the doc prose names the new behavior, applied to the README's
# "Board columns" section instead of a tool docstring.
# ---------------------------------------------------------------------------


def test_readme_board_columns_section_documents_autodefault_and_off_board() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    readme = (repo_root / "README.md").read_text(encoding="utf-8")

    idx = readme.index("#### Board columns (optional)")
    next_idx = readme.index("\n## ", idx)  # next top-level section
    section = readme[idx:next_idx]

    assert "columns[0]" in section, "README Board columns section must name columns[0]"
    assert "off_board" in section, "README Board columns section must name off_board"
    assert "board_warning" in section, (
        "README Board columns section must mention the best-effort board_warning"
    )
