"""Pure helpers for the label-based board-state fallback (ticket #365).

A project whose `board:` block has `columns` but no `binding` is
"label mode": there is no live provider board to resolve, so the board
state a consumer cares about (`list_board_columns`, `list_tickets(column=
...)`, `create_ticket`/`update_ticket` with `custom_fields={"Status":
...}`, `ensure_board_column`) is tracked entirely through issue labels
instead. `lib_python_projects.Board` validates and stores `label_map` /
`closed_column` but deliberately leaves them inert (library reports,
plugin decides — see AGENTS.md); this module is where the plugin
actually resolves them.

Every column-to-label rule lives here, once, so the five call sites that
need it (`list_tickets`, `list_tickets_across_projects`, `create_ticket`,
`update_ticket`, `list_board_columns`, `ensure_board_column`) can't drift
apart on the slug rule or the closed-column special case.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any


def label_board(project: Any) -> Any | None:
    """Return `project.board` when it is a label-mode board, else `None`.

    A board is label mode when it is configured (`project.board is not
    None`), carries no live provider binding (`board.binding is None`),
    AND has at least one configured column. The third condition guards
    against an empty `columns` list being mistaken for label mode — note
    that `lib_python_projects.Board` itself already rejects
    `columns=[]` at construction (`ValidationError`, verified), so this
    guard only matters for a `project.board`-shaped object built some
    other way (e.g. a duck-typed stand-in in a test, or a future caller
    that doesn't route through the pydantic model).
    """
    board = getattr(project, "board", None)
    if board is None:
        return None
    if getattr(board, "binding", None) is not None:
        return None
    if not getattr(board, "columns", None):
        return None
    return board


def column_label(board: Any, col: str) -> str | None:
    """Return the label representing logical column `col` on `board`, or
    `None` when `col` carries no status label at all.

    `None` for two columns specifically:
      - `board.columns[0]` — the first column is the "no status label
        yet" state; a ticket with none of the catalogue's labels reads
        as being in it.
      - `board.closed_column` — the closed column is tracked via the
        ticket's native open/closed state, never a label.

    Every other column resolves to `board.label_map[col]` when present,
    else a derived slug: `"status:" + <col, lowercased, whitespace runs
    collapsed to a single '-'>`. E.g. "In Progress" -> "status:in-progress",
    not "status:in progress" — label names with spaces don't belong in
    filters or URLs. A caller who wants the spaced form sets it
    explicitly via `label_map`.
    """
    if board.columns and col == board.columns[0]:
        return None
    if board.closed_column is not None and col == board.closed_column:
        return None
    label_map = board.label_map or {}
    if col in label_map:
        return label_map[col]
    slug = re.sub(r"\s+", "-", col.strip().lower())
    return f"status:{slug}"


def catalogue(board: Any) -> list[str]:
    """Every non-`None` `column_label` for `board`, in column order."""
    labels = []
    for col in board.columns:
        label = column_label(board, col)
        if label is not None:
            labels.append(label)
    return labels


def require_column(board: Any, col: str) -> None:
    """Raise `ValueError` naming the configured columns unless `col` is
    one of `board.columns` (exact match)."""
    if col not in board.columns:
        raise ValueError(
            f"unknown board column {col!r}; configured columns: "
            f"{list(board.columns)!r}"
        )


def rewrite_filters(board: Any, filters: Any) -> Any:
    """Rewrite a `TicketFilters`' `board_column`/`status`/`states` into
    label-mode terms, via `dataclasses.replace`.

    A no-op (returns `filters` unchanged) when `filters.board_column` is
    `None` — there is nothing to rewrite. Otherwise:
      - the first column: `board_column=None`, `not_labels` gains every
        catalogue label, `status="open"`, `states=[]`;
      - the closed column: `board_column=None`, `status="closed"`,
        `states=[]`, no label condition added;
      - any other column: `board_column=None`, `labels` gains that
        column's label, `status="open"`, `states=[]`.

    In label mode the column decides open vs. closed and overrides the
    caller's own `status`/`states` — a label can't tell the closed
    column from the first column, only the native state can.

    Raises `ValueError` (via `require_column`) when `filters.board_column`
    doesn't match a configured column.
    """
    column = filters.board_column
    if column is None:
        return filters
    require_column(board, column)
    is_closed = board.closed_column is not None and column == board.closed_column
    if is_closed:
        return replace(filters, board_column=None, status="closed", states=[])
    if board.columns and column == board.columns[0]:
        return replace(
            filters,
            board_column=None,
            status="open",
            states=[],
            not_labels=[*filters.not_labels, *catalogue(board)],
        )
    label = column_label(board, column)
    return replace(
        filters,
        board_column=None,
        status="open",
        states=[],
        labels=[*filters.labels, label],
    )


def ensure_label(provider: Any, project: Any, token: Any, name: str) -> bool:
    """Create label `name` on GitHub if it doesn't already exist in the
    project's label catalogue; return whether it created one.

    GitLab and Azure DevOps always return `False`, without any provider
    call — by design: GitLab creates a label the first time it's
    applied to a ticket, and Azure tags are freeform, so neither has a
    catalogue to pre-create against.
    """
    if project.provider != "github":
        return False
    existing = {label.name for label in provider.list_labels(project, token)}
    if name in existing:
        return False
    provider.create_label(project, token, name)
    return True
