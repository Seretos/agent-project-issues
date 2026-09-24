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

Phase = tests (round 1): only the module skeleton lives here so the
round's driving tests can import real symbols and fail for a genuine
"behaviour missing" reason (`NotImplementedError`), never an
`ImportError`. The real logic (`column_label`'s slug/label_map/
closed_column resolution, `catalogue`, `require_column`, `ensure_label`)
lands in the `implement` phase, wired into `tools/tickets.py` and
`tools/bulk.py` per the approved plan.
"""
from __future__ import annotations

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

    Not yet implemented (ticket #365, phase=tests): raises
    `NotImplementedError` so every driving test that exercises this
    helper — directly, or indirectly through the tool surface once
    `tools/tickets.py`/`tools/bulk.py` import it — fails for the right
    reason ahead of the real implementation landing in phase=implement.
    """
    raise NotImplementedError("label_board is not implemented yet (ticket #365)")
