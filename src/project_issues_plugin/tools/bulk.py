"""Bulk-listing tools that span multiple projects.

Read-only: like the other list/get tools, only requires a token per
project when the underlying repo is private. Errors on one project never
abort the call — each failure is surfaced both in the per-project entry
of `results` and in a top-level `errors` list.

Shared scaffolding (`_PROVIDERS`, `_provider_for`, `_safe`) lives in
`tools/_providers.py`. Bulk has its own `_resolve_local` because it
resolves against an already-loaded project list, not the global
`load_projects()` result.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from mcp.server.fastmcp import FastMCP

from lib_python_projects import ProjectConfig, load_projects, resolve_token
from lib_python_projects.providers.base import TicketFilters
from lib_python_projects.providers.github import GitHubError
from project_issues_plugin.tools._providers import _provider_for
from project_issues_plugin.tools._slicing import apply_body_knobs


def _resolve_local(project_id: str, projects: list[ProjectConfig]) -> ProjectConfig:
    for p in projects:
        if p.id == project_id:
            return p
    raise LookupError("unknown project")


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_tickets_across_projects(
        project_ids: list[str] | None = None,
        status: Literal["open", "closed", "any"] = "open",
        labels: list[str] | None = None,
        not_labels: list[str] | None = None,
        assignee: str | None = None,
        author: str | None = None,
        search: str | None = None,
        limit_per_project: int = 10,
        omit_body: bool = False,
        body_max_chars: int | None = None,
        states: list[str] | None = None,
        area_path: str | None = None,
        area_path_recursive: bool = True,
        column: str | None = None,
    ) -> dict:
        """List tickets across multiple projects in a single call.
        Supports board-column filtering via `column` — see `list_board_columns`.

        Restricts to the given `project_ids`; unknown ids are surfaced
        as `{"error": "unknown project"}` for that entry rather than
        raising. Use `list_projects` to discover available project IDs.

        Filters (`status`, `labels`, `not_labels`, `assignee`, `author`,
        `search`) and `limit_per_project` are applied per-project with
        the same semantics as `list_tickets`. `limit_per_project` caps
        the result count for each project independently.

        `states` and `area_path`/`area_path_recursive` carry the exact
        same semantics as `list_tickets` — see that tool's docstring.
        Because filters are shared across every project in `project_ids`,
        a non-empty `area_path` fails every non-Azure-DevOps project in
        the batch with a per-project error (not a top-level exception);
        mix providers in one call only when the filters you pass apply
        to all of them.

        `column` carries the same semantics as `list_tickets`' `column`
        filter — a logical board column, discovered per-project via
        `list_board_columns`. Since it's a single value applied to every
        project in the batch, mix providers/boards in one call only when
        the same logical column name is meaningful across all of them;
        GitLab projects in the batch fail that entry with a per-project
        "not supported" error (not a top-level exception).

        Default `limit_per_project` is `10` — the fan-out shape
        multiplies the body-row cost, so this is the conservative
        default. Bump it explicitly when you need more.

        There is no aggregate `limit` across projects — only the
        per-project cap. The total row count can therefore reach
        `len(project_ids) × limit_per_project`. Bound the fan-out
        yourself: keep `limit_per_project` low and the `project_ids`
        list short, and page a single project with `list_tickets` when
        you need more than the per-project cap from it.

        Token-cheap knobs:
          - `omit_body=True`: drop the `body` field from every row
            across every project. Recommended when enumerating titles
            / labels to decide which tickets to drill into.
          - `body_max_chars=N`: truncate each row's body to N chars
            and add `body_truncated: bool` per row.

        The call is partial-failure tolerant: when one project errors
        (missing token, permission denied, API failure, unknown id), the
        error is recorded in `results[project_id]["error"]` AND appended
        to the top-level `errors` list, and the remaining projects are
        still queried. `total_tickets` counts only successful projects;
        `project_count` is the number of projects attempted including
        failed ones.

        Each per-project result carries `has_more: bool` from the
        provider's pagination header, indicating whether additional
        pages are available beyond `limit_per_project`.
        """
        if project_ids is None:
            return {
                "error": (
                    "project_ids is required. Pass an explicit list of project IDs. "
                    "Use list_projects to discover available IDs."
                )
            }

        loaded = load_projects(
            config_filename="projects.yml",
            config_filename_alt="projects.yaml",
        )
        all_projects = loaded.projects

        target_ids = list(project_ids)

        results: dict[str, dict] = {}
        errors: list[dict] = []
        total_tickets = 0

        filters = TicketFilters(
            status=status,
            labels=labels or [],
            assignee=assignee,
            search=search,
            limit=limit_per_project,
            not_labels=not_labels or [],
            author=author,
            states=states or [],
            area_path=area_path,
            area_path_recursive=area_path_recursive,
            board_column=column,
        )

        for pid in target_ids:
            try:
                project = _resolve_local(pid, all_projects)
                provider = _provider_for(project)
                token = resolve_token(project)
                tickets, _has_more = provider.list_tickets(project, token, filters)
                ticket_dicts = [asdict(t) for t in tickets]
                ticket_dicts = apply_body_knobs(
                    ticket_dicts,
                    omit_body=omit_body,
                    body_max_chars=body_max_chars,
                )
                for row in ticket_dicts:
                    row["project_id"] = pid
                results[pid] = {
                    "tickets": ticket_dicts,
                    "has_more": _has_more,
                    "error": None,
                }
                total_tickets += len(ticket_dicts)
            except (LookupError, PermissionError, NotImplementedError, ValueError, GitHubError) as exc:
                msg = str(exc)
                results[pid] = {"tickets": [], "has_more": False, "error": msg}
                errors.append({"project_id": pid, "error": msg})

        return {
            "results": results,
            "total_tickets": total_tickets,
            "project_count": len(target_ids),
            "errors": errors,
        }
