"""Driving tests for work package #365: label-based fallback board state
for a project whose `board:` block has `columns` but no `binding` (e.g.
GitLab, or GitHub without a Projects-v2 binding) -- ticket symptom
(verbatim): "a consumer that tracks ticket state through the board calls
(list_board_columns, list_tickets(column=...), create/update_ticket with
custom_fields={"Status": ...}) cannot run on a project without a board
binding [...]: the calls return no columns or raise".

Phase = tests (round 1): none of the label-mode branches exist yet in
`tools/tickets.py` / `tools/bulk.py` / `tools/_label_board.py` (the last
is a compile-level skeleton only -- see that module's docstring). Every
assertion below is written against the *target* (GREEN) behaviour and
is confirmed RED against the unmodified code for the reason documented
on each test group; GREEN lands in the next dispatch.

Fixtures mirror `tests/test_default_board_column_232.py` (`_StubMCP`,
`load_projects` monkeypatch, `_PROVIDERS` injection) and
`tests/test_board_columns_169_170.py` (GitHub/Azure board methods that
raise vs. GitLab lacking them entirely) -- per the plan's fixture note.
Board fixture: `Board(columns=["Todo", "Doing", "Review", "Done"],
label_map={"Review": "status:in-review"}, closed_column="Done")`.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import (
    Board,
    BoardPermissions,
    IssuesPermissions,
    Permissions,
    ProjectConfig,
    ProjectsLoadResult,
)
from lib_python_projects.providers.base import Label, StatusSpec, Ticket
from project_issues_plugin.tools import _label_board
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import bulk as bulk_tools
from project_issues_plugin.tools import tickets as ticket_tools

PROVIDER_NAMES = ["github", "gitlab", "azuredevops"]


# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _label_board_fixture() -> Board:
    return Board(
        columns=["Todo", "Doing", "Review", "Done"],
        label_map={"Review": "status:in-review"},
        closed_column="Done",
    )


def _label_project(
    provider: str,
    *,
    board: Board | None = None,
    issues_create: bool = False,
    issues_modify: bool = False,
    board_manage: bool = False,
    project_id: str = "acme",
) -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(
            issues=IssuesPermissions(create=issues_create, modify=issues_modify),
            board=BoardPermissions(manage=board_manage),
        ),
        board=board if board is not None else _label_board_fixture(),
    )


def _full_ticket(**overrides) -> Ticket:
    base = dict(
        id="42", title="some title", body="some body", status="open",
        author="alice", assignees=[], labels=[],
        url="https://example.test/issues/42",
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-02T00:00:00Z",
    )
    base.update(overrides)
    return Ticket(**base)


def _ticket_with_labels(labels: list[str], status: str = "open") -> Ticket:
    return _full_ticket(labels=list(labels), status=status)


def _statuses(provider_name: str) -> StatusSpec:
    """A minimal-but-plausible per-provider status spec -- enough to
    exercise the `hints.terminal_completed` / `hints.default_open` reads
    the plan's write path (R2/R5) needs."""
    if provider_name == "github":
        return StatusSpec(
            values=["open", "closed:completed", "closed:not_planned"],
            transitions={},
            hints={
                "default_open": "open",
                "terminal": ["closed:completed", "closed:not_planned"],
                "terminal_completed": "closed:completed",
                "terminal_declined": "closed:not_planned",
            },
        )
    if provider_name == "gitlab":
        return StatusSpec(
            values=["opened", "closed"],
            transitions={},
            hints={
                "default_open": "opened",
                "terminal": ["closed"],
                "terminal_completed": "closed",
                "terminal_declined": "closed",
            },
        )
    return StatusSpec(
        values=["New", "Active", "Closed"],
        transitions={},
        hints={
            "default_open": "New",
            "terminal": ["Closed"],
            "terminal_completed": "Closed",
            "terminal_declined": "Closed",
        },
    )


class _LabelModeProviderBase:
    """Records every call the label-mode branches will need to make,
    across the three providers. Subclasses vary only in the presence /
    absence of board methods and the `custom_fields` kwarg on
    `update_ticket`, mirroring each real provider's actual shape (per
    the plan's fixture note: "The GitLab fake lacks the board methods.
    The GitHub/Azure fakes raise ValueError('no binding') from them.")
    """

    def __init__(
        self,
        *,
        ticket: Ticket | None = None,
        current_ticket: Ticket | None = None,
        statuses: StatusSpec | None = None,
        labels: list[Label] | None = None,
    ) -> None:
        self._ticket = ticket if ticket is not None else _full_ticket()
        self._current_ticket = current_ticket if current_ticket is not None else _full_ticket()
        self._statuses = statuses
        self._labels: list[Label] = list(labels) if labels is not None else []
        self.list_tickets_calls: list = []
        self.create_ticket_calls: list[dict] = []
        self.update_ticket_calls: list[dict] = []
        self.get_ticket_calls: list = []
        self.list_labels_called = False
        self.create_label_calls: list[str] = []

    def list_tickets(self, project_, token, filters):
        self.list_tickets_calls.append(filters)
        return [], False

    def create_ticket(
        self, project_, token, title, body, labels, assignees, *,
        status=None, custom_fields=None,
    ):
        call = dict(
            title=title, body=body, labels=list(labels), assignees=list(assignees),
            status=status, custom_fields=custom_fields,
        )
        self.create_ticket_calls.append(call)
        return self._ticket

    def get_ticket(
        self, project_, token, ticket_id, *,
        include_relations=True, include_custom_fields=False,
    ):
        self.get_ticket_calls.append(ticket_id)
        return self._current_ticket, [], None, None

    def list_statuses(self, project_, token):
        return self._statuses

    def list_labels(self, project_, token):
        self.list_labels_called = True
        return list(self._labels)

    def create_label(self, project_, token, name, color=None, description=None):
        self.create_label_calls.append(name)
        self._labels.append(Label(name=name))
        return Label(name=name)


class _GithubLabelModeProvider(_LabelModeProviderBase):
    """`custom_fields`-capable `update_ticket`, plus board methods that
    raise "no binding" -- the real GitHub provider requires a live
    `github-projects-v2` binding to resolve a board, and this fixture's
    board has none."""

    def update_ticket(
        self, project_, token, ticket_id, *,
        title=None, body=None, status=None,
        labels_add=None, labels_remove=None,
        assignees_add=None, assignees_remove=None, custom_fields=None,
    ):
        call = dict(
            ticket_id=ticket_id, title=title, body=body, status=status,
            labels_add=labels_add, labels_remove=labels_remove,
            assignees_add=assignees_add, assignees_remove=assignees_remove,
            custom_fields=custom_fields,
        )
        self.update_ticket_calls.append(call)
        return self._ticket

    def list_board_columns(self, project_, token):
        raise ValueError("no binding")

    def ensure_board_column(self, project_, token, column_name):
        raise ValueError("no binding")


class _AzureLabelModeProvider(_GithubLabelModeProvider):
    """Same shape as GitHub for this fixture's purposes: `custom_fields`
    on `update_ticket`, and board methods that raise "no binding"."""


class _GitlabLabelModeProvider(_LabelModeProviderBase):
    """No board methods at all (GitLab has no board concept) and no
    `custom_fields` kwarg on `update_ticket` (GitLab doesn't support
    it -- mirrors the real provider's signature)."""

    def update_ticket(
        self, project_, token, ticket_id, *,
        title=None, body=None, status=None,
        labels_add=None, labels_remove=None,
        assignees_add=None, assignees_remove=None,
    ):
        call = dict(
            ticket_id=ticket_id, title=title, body=body, status=status,
            labels_add=labels_add, labels_remove=labels_remove,
            assignees_add=assignees_add, assignees_remove=assignees_remove,
            custom_fields=None,
        )
        self.update_ticket_calls.append(call)
        return self._ticket


_PROVIDER_CLASSES: dict[str, type[_LabelModeProviderBase]] = {
    "github": _GithubLabelModeProvider,
    "gitlab": _GitlabLabelModeProvider,
    "azuredevops": _AzureLabelModeProvider,
}


def _make_provider(provider_name: str, **kwargs) -> _LabelModeProviderBase:
    return _PROVIDER_CLASSES[provider_name](**kwargs)


def _register_tickets(
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


def _register_bulk(
    monkeypatch: pytest.MonkeyPatch, provider_instance, projects: list[ProjectConfig],
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=projects, state="ok", search_root="/tmp")

    monkeypatch.setattr(bulk_tools, "load_projects", fake_load_projects)
    for p in projects:
        monkeypatch.setitem(providers_mod._PROVIDERS, p.provider, provider_instance)
        monkeypatch.setenv(p.token_env, "tok")

    stub = _StubMCP()
    bulk_tools.register(stub)
    return stub.tools


# ---------------------------------------------------------------------------
# R1 -- list_board_columns returns labelled columns -- driving-test
# ---------------------------------------------------------------------------


_EXPECTED_LABEL_COLUMNS = [
    {"logical": "Todo", "native": "Todo", "option_id": "", "states": [], "is_split": False, "label": None, "source": "labels"},
    {"logical": "Doing", "native": "Doing", "option_id": "", "states": [], "is_split": False, "label": "status:doing", "source": "labels"},
    {"logical": "Review", "native": "Review", "option_id": "", "states": [], "is_split": False, "label": "status:in-review", "source": "labels"},
    {"logical": "Done", "native": "Done", "option_id": "", "states": [], "is_split": False, "label": None, "source": "labels"},
]


@pytest.mark.parametrize("provider_name", PROVIDER_NAMES)
def test_list_board_columns_label_mode(
    monkeypatch: pytest.MonkeyPatch, provider_name: str,
) -> None:
    """RED reason: GitHub/Azure currently call through to
    `provider.list_board_columns`, which raises the fake's "no binding"
    ValueError -> `_safe` turns that into `{"error": ...}`. GitLab has
    no `list_board_columns` attribute at all -> the existing `hasattr`
    branch returns `{"columns": []}`. Neither matches the 4 labelled
    rows expected once label mode is wired up."""
    project = _label_project(provider_name)
    provider = _make_provider(provider_name)
    tools = _register_tickets(monkeypatch, provider, project)

    out = tools["list_board_columns"](project_id="acme")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["provider"] == provider_name
    assert out["columns"] == _EXPECTED_LABEL_COLUMNS


def test_list_board_columns_label_mode_slug_hyphenates_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage: a column with no `label_map` entry
    slugs to `status:` + lowercase-hyphenated -- "In Progress" ->
    "status:in-progress", not "status:in progress". Single provider
    (github) is enough -- this pins `column_label`'s slug rule, which is
    provider-agnostic."""
    board = Board(columns=["Todo", "In Progress", "Done"])
    project = _label_project("github", board=board)
    provider = _make_provider("github")
    tools = _register_tickets(monkeypatch, provider, project)

    out = tools["list_board_columns"](project_id="acme")

    assert "error" not in out, f"unexpected error: {out}"
    row = next(c for c in out["columns"] if c["logical"] == "In Progress")
    assert row["label"] == "status:in-progress"


# ---------------------------------------------------------------------------
# R2 -- create lands in the first column, no status:* label -- driving-test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_name", PROVIDER_NAMES)
def test_create_ticket_label_mode(
    monkeypatch: pytest.MonkeyPatch, provider_name: str,
) -> None:
    """RED reason: `custom_fields={"Status": ...}` is forwarded verbatim
    to `provider.create_ticket` today -- `_default_board_custom_fields`
    is a no-op for a board with no `github-projects-v2` binding, so
    neither the "Status" key is stripped nor a status label is added."""
    project = _label_project(provider_name, issues_create=True)
    provider = _make_provider(provider_name, statuses=_statuses(provider_name))
    tools = _register_tickets(monkeypatch, provider, project)

    tools["create_ticket"](project_id="acme", title="t1", custom_fields={"Status": "Todo"})
    first = provider.create_ticket_calls[-1]
    assert first["custom_fields"] in (None, {}), first
    assert first["labels"] == []

    tools["create_ticket"](project_id="acme", title="t2", custom_fields={"Status": "Doing"})
    second = provider.create_ticket_calls[-1]
    assert second["custom_fields"] in (None, {}), second
    assert second["labels"] == ["status:doing"]

    # Additional edge-case coverage: no custom_fields -> no status label.
    # This assertion may already pass today -- the unmodified
    # _default_board_custom_fields is already a no-op for a board with
    # no github-projects-v2 binding, so `custom_fields` stays `None` and
    # no label is ever appended regardless of label-mode wiring.
    tools["create_ticket"](project_id="acme", title="t3")
    third = provider.create_ticket_calls[-1]
    assert third["custom_fields"] is None
    assert third["labels"] == []

    # Additional edge-case coverage: the closed column sets `status` to
    # the provider's terminal_completed hint and adds no label.
    tools["create_ticket"](project_id="acme", title="t4", custom_fields={"Status": "Done"})
    fourth = provider.create_ticket_calls[-1]
    assert fourth["status"] == _statuses(provider_name).hints["terminal_completed"]
    assert fourth["custom_fields"] in (None, {}), fourth
    assert fourth["labels"] == []


# ---------------------------------------------------------------------------
# R3 -- column filter maps to labels and state -- driving-test (both call sites)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_name", PROVIDER_NAMES)
def test_list_tickets_column_label_mode(
    monkeypatch: pytest.MonkeyPatch, provider_name: str,
) -> None:
    """RED reason: the recorded filters currently carry
    `board_column == "Todo"` (etc.) verbatim, empty label lists, and
    `status == "any"` unchanged -- `list_tickets` doesn't yet rewrite
    `column`/`status` into label-mode filters at all."""
    project = _label_project(provider_name)
    provider = _make_provider(provider_name)
    tools = _register_tickets(monkeypatch, provider, project)

    tools["list_tickets"](project_id="acme", column="Todo", status="any")
    todo_filters = provider.list_tickets_calls[-1]
    assert todo_filters.board_column is None
    assert {"status:doing", "status:in-review"} <= set(todo_filters.not_labels)
    assert todo_filters.status == "open"
    assert todo_filters.states == []

    tools["list_tickets"](project_id="acme", column="Doing", status="any")
    doing_filters = provider.list_tickets_calls[-1]
    assert doing_filters.labels == ["status:doing"]
    assert doing_filters.status == "open"

    tools["list_tickets"](project_id="acme", column="Done", status="any")
    done_filters = provider.list_tickets_calls[-1]
    assert done_filters.status == "closed"
    assert done_filters.labels == []
    assert done_filters.not_labels == []

    # Additional edge-case coverage: label mode overrides a caller-passed
    # `states` list on a column filter too.
    tools["list_tickets"](project_id="acme", column="Todo", states=["Closed"])
    assert provider.list_tickets_calls[-1].states == []


def test_bulk_column_label_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED reason: same as `test_list_tickets_column_label_mode`, via
    `list_tickets_across_projects`; the unknown-column edge case is also
    not yet refused per-project."""
    project = _label_project("github")
    provider = _make_provider("github")
    tools = _register_bulk(monkeypatch, provider, [project])

    out = tools["list_tickets_across_projects"](
        project_ids=["acme"], column="Doing", status="any",
    )
    assert out["results"]["acme"]["error"] is None, out["results"]["acme"]
    doing_filters = provider.list_tickets_calls[-1]
    assert doing_filters.labels == ["status:doing"]
    assert doing_filters.status == "open"

    # Additional edge-case coverage: an unknown column is refused
    # per-project, naming the configured columns, rather than silently
    # matching nothing.
    out2 = tools["list_tickets_across_projects"](project_ids=["acme"], column="Bogus")
    assert out2["results"]["acme"]["error"] is not None
    assert "Todo" in out2["results"]["acme"]["error"], out2["results"]["acme"]


# ---------------------------------------------------------------------------
# R4 -- move is one atomic provider call -- driving-test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_name", PROVIDER_NAMES)
def test_update_move_label_mode(
    monkeypatch: pytest.MonkeyPatch, provider_name: str,
) -> None:
    """RED reason: GitHub/Azure currently forward `custom_fields`
    verbatim with `labels_add`/`labels_remove` left `None` (still one
    call, but the wrong one). GitLab's `update_ticket` has no
    `custom_fields` parameter, so the existing "not supported" guard
    fires and makes zero provider calls."""
    project = _label_project(provider_name, issues_modify=True)
    provider = _make_provider(
        provider_name, current_ticket=_ticket_with_labels(["status:doing"]),
    )
    tools = _register_tickets(monkeypatch, provider, project)

    tools["update_ticket"](project_id="acme", ticket_id="42", custom_fields={"Status": "Review"})

    assert len(provider.update_ticket_calls) == 1, provider.update_ticket_calls
    call = provider.update_ticket_calls[0]
    assert call["labels_add"] == ["status:in-review"]
    assert call["labels_remove"] == ["status:doing"]
    assert call["custom_fields"] is None


def test_update_move_label_mode_add_only_remove_only_noop_and_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage (github only -- provider-agnostic
    logic): Todo->Doing only adds; Doing->Todo only removes; a move to
    the ticket's current column is a no-op (zero provider calls); a
    caller-supplied `labels_add` is merged with the target label."""
    project = _label_project("github", issues_modify=True)

    provider_add_only = _make_provider("github", current_ticket=_ticket_with_labels([]))
    tools_add_only = _register_tickets(monkeypatch, provider_add_only, project)
    tools_add_only["update_ticket"](project_id="acme", ticket_id="42", custom_fields={"Status": "Doing"})
    call = provider_add_only.update_ticket_calls[0]
    assert call["labels_add"] == ["status:doing"]
    assert call["labels_remove"] == []

    provider_remove_only = _make_provider("github", current_ticket=_ticket_with_labels(["status:doing"]))
    tools_remove_only = _register_tickets(monkeypatch, provider_remove_only, project)
    tools_remove_only["update_ticket"](project_id="acme", ticket_id="42", custom_fields={"Status": "Todo"})
    call2 = provider_remove_only.update_ticket_calls[0]
    assert call2["labels_add"] == []
    assert call2["labels_remove"] == ["status:doing"]

    provider_noop = _make_provider("github", current_ticket=_ticket_with_labels(["status:doing"]))
    tools_noop = _register_tickets(monkeypatch, provider_noop, project)
    tools_noop["update_ticket"](project_id="acme", ticket_id="42", custom_fields={"Status": "Doing"})
    assert provider_noop.update_ticket_calls == []

    provider_merge = _make_provider("github", current_ticket=_ticket_with_labels([]))
    tools_merge = _register_tickets(monkeypatch, provider_merge, project)
    tools_merge["update_ticket"](
        project_id="acme", ticket_id="42",
        custom_fields={"Status": "Doing"}, labels_add=["extra"],
    )
    call4 = provider_merge.update_ticket_calls[0]
    assert set(call4["labels_add"]) == {"status:doing", "extra"}


# ---------------------------------------------------------------------------
# R5 -- closed-mapped column closes the ticket -- driving-test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_name", PROVIDER_NAMES)
def test_update_to_closed_column(
    monkeypatch: pytest.MonkeyPatch, provider_name: str,
) -> None:
    """RED reason: as `test_update_move_label_mode` -- the closed-column
    move should set `status` to the provider's `terminal_completed` hint
    and remove any status label, but the current code neither derives
    `status` from the column nor touches labels at all."""
    project = _label_project(provider_name, issues_modify=True)
    statuses = _statuses(provider_name)
    provider = _make_provider(
        provider_name,
        current_ticket=_ticket_with_labels(["status:doing"]),
        statuses=statuses,
    )
    tools = _register_tickets(monkeypatch, provider, project)

    tools["update_ticket"](project_id="acme", ticket_id="42", custom_fields={"Status": "Done"})

    assert len(provider.update_ticket_calls) == 1, provider.update_ticket_calls
    call = provider.update_ticket_calls[0]
    assert call["status"] == statuses.hints["terminal_completed"]
    assert call["labels_add"] == []
    assert call["labels_remove"] == ["status:doing"]
    assert call["custom_fields"] is None


def test_update_reopen_from_closed_column_and_caller_status_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage (github only): moving out of a
    closed state sets `hints.default_open` plus the target column's
    label; a caller-supplied `status` always wins over the derived one
    (this second half may already pass today, since the tool's own
    `status` kwarg is forwarded unconditionally regardless of label-mode
    wiring)."""
    project = _label_project("github", issues_modify=True)
    statuses = _statuses("github")

    provider = _make_provider(
        "github", current_ticket=_ticket_with_labels([], status="closed:completed"),
        statuses=statuses,
    )
    tools = _register_tickets(monkeypatch, provider, project)
    tools["update_ticket"](project_id="acme", ticket_id="42", custom_fields={"Status": "Doing"})
    call = provider.update_ticket_calls[0]
    assert call["status"] == statuses.hints["default_open"]
    assert call["labels_add"] == ["status:doing"]

    provider2 = _make_provider(
        "github", current_ticket=_ticket_with_labels([], status="closed:completed"),
        statuses=statuses,
    )
    tools2 = _register_tickets(monkeypatch, provider2, project)
    tools2["update_ticket"](
        project_id="acme", ticket_id="42",
        custom_fields={"Status": "Doing"}, status="closed:not_planned",
    )
    call2 = provider2.update_ticket_calls[0]
    assert call2["status"] == "closed:not_planned"


# ---------------------------------------------------------------------------
# R6 -- GitHub creates a missing catalogue label -- driving-test
# ---------------------------------------------------------------------------


def test_github_autocreates_status_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED reason: 0 `create_label` calls today -- the write path never
    calls `ensure_label` because label mode isn't wired into
    `update_ticket` at all yet."""
    project = _label_project("github", issues_modify=True)
    provider = _make_provider("github", current_ticket=_ticket_with_labels([]), labels=[])
    tools = _register_tickets(monkeypatch, provider, project)

    tools["update_ticket"](project_id="acme", ticket_id="42", custom_fields={"Status": "Doing"})

    assert provider.create_label_calls == ["status:doing"]
    assert len(provider.update_ticket_calls) == 1


@pytest.mark.parametrize("provider_name", PROVIDER_NAMES)
def test_ensure_board_column_label_mode(
    monkeypatch: pytest.MonkeyPatch, provider_name: str,
) -> None:
    """RED reason: `ensure_board_column` raises the fake's "no binding"
    ValueError on GitHub/Azure (both have the method, just no live
    board) and `NotImplementedError` on GitLab (no method at all) --
    `_safe` turns either into `{"error": ...}` today, never the
    `created`/`source: "labels"` payload label mode should return."""
    project = _label_project(provider_name, board_manage=True)
    provider = _make_provider(provider_name, labels=[])
    tools = _register_tickets(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Doing")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["source"] == "labels"
    if provider_name == "github":
        assert out["created"] is True
        assert provider.create_label_calls == ["status:doing"]
    else:
        assert out["created"] is False
        assert provider.create_label_calls == []


def test_github_ensure_label_skips_existing_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage: an already-existing catalogue
    label is not re-created. Note this assertion is trivially true today
    too (zero label-mode wiring means zero create_label calls either
    way) -- its real protective value starts once GREEN lands."""
    project = _label_project("github", board_manage=True)
    provider = _make_provider("github", labels=[Label(name="status:doing")])
    tools = _register_tickets(monkeypatch, provider, project)

    out = tools["ensure_board_column"](project_id="acme", column_name="Doing")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["created"] is False
    assert provider.create_label_calls == []


def test_update_move_label_mode_requires_issues_modify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage: without `issues.modify`, the
    existing permission gate refuses the write before any provider call
    -- already passes today, since `_require_issues_modify` runs
    unconditionally ahead of any label-mode logic."""
    project = _label_project("github", issues_modify=False)
    provider = _make_provider("github", current_ticket=_ticket_with_labels(["status:doing"]))
    tools = _register_tickets(monkeypatch, provider, project)

    out = tools["update_ticket"](project_id="acme", ticket_id="42", custom_fields={"Status": "Review"})

    assert "error" in out
    assert provider.update_ticket_calls == []
    assert provider.create_label_calls == []


# ---------------------------------------------------------------------------
# _label_board.label_board -- pure-helper unit test (plan-critic round 2 note)
# ---------------------------------------------------------------------------


def test_label_board_requires_nonempty_columns() -> None:
    """Forwarded plan-critic round-2 note: `label_board(project)` should
    guard against an empty `columns` list, not just `binding is None` --
    a board with `binding=None, columns=[]` must not be treated as label
    mode.

    A real `lib_python_projects.Board` can never carry `columns=[]` --
    its own `_check_columns` validator rejects it at construction time
    (verified: `Board(columns=[], binding=None)` raises a pydantic
    `ValidationError`, so this scenario cannot occur through the normal
    `projects.yml` -> `load_projects` path). This test therefore exercises
    `label_board` directly with a lightweight duck-typed stand-in rather
    than a real `Board`, to pin the guard at the unit level regardless of
    how a caller's `project.board` was built.

    RED reason: `label_board` is not implemented yet -- both calls raise
    `NotImplementedError`."""
    class _FakeBoard:
        def __init__(self, columns, binding):
            self.columns = columns
            self.binding = binding

    class _FakeProject:
        def __init__(self, board):
            self.board = board

    empty_columns = _FakeProject(_FakeBoard(columns=[], binding=None))
    assert _label_board.label_board(empty_columns) is None

    nonempty_columns = _FakeProject(_FakeBoard(columns=["Todo"], binding=None))
    assert _label_board.label_board(nonempty_columns) is nonempty_columns.board
