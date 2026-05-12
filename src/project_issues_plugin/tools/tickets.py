"""Ticket operations exposed to the agent.

Permission gating happens here, before any provider call. Markers
(ai-generated label, ai-modified label, #ai-generated comment prefix)
are applied transparently by the provider — the agent does NOT pass
them and MUST NOT add them manually.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from mcp.server.fastmcp import FastMCP

from project_issues_plugin.config import ProjectConfig, load_projects, resolve_token
from project_issues_plugin.providers.base import TicketFilters
from project_issues_plugin.providers.github import GitHubError, GitHubProvider


_PROVIDERS = {
    "github": GitHubProvider(),
}


def _resolve(project_id: str) -> ProjectConfig:
    result = load_projects()
    for p in result.projects:
        if p.id == project_id:
            return p
    raise LookupError(
        f"unknown project '{project_id}'. Use list_projects to see what is available."
    )


def _provider_for(project: ProjectConfig):
    impl = _PROVIDERS.get(project.provider)
    if impl is None:
        raise NotImplementedError(
            f"provider '{project.provider}' is not implemented yet"
        )
    return impl


def _require_token(project: ProjectConfig) -> str:
    token = resolve_token(project)
    if not token:
        raise PermissionError(
            f"project '{project.id}' has no API token available "
            f"(env var '{project.token_env}' is unset). Writes are not possible."
        )
    return token


def _require_create(project: ProjectConfig) -> None:
    if not project.permissions.create:
        raise PermissionError(
            f"project '{project.id}' does not permit creating tickets. "
            "Tell the user the project is configured without create permission."
        )


def _require_modify(project: ProjectConfig) -> None:
    if not project.permissions.modify:
        raise PermissionError(
            f"project '{project.id}' does not permit modifying tickets or "
            "adding comments. Tell the user the project is configured without "
            "modify permission."
        )


def _safe(call):
    """Execute `call()` and translate known errors to a dict with `error`."""
    try:
        return call()
    except (LookupError, PermissionError, NotImplementedError) as exc:
        return {"error": str(exc)}
    except GitHubError as exc:
        return {"error": str(exc)}


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_tickets(
        project_id: str,
        status: Literal["open", "closed", "any"] = "open",
        labels: list[str] | None = None,
        assignee: str | None = None,
        search: str | None = None,
        limit: int = 30,
    ) -> dict:
        """List tickets in a project. Default: open tickets, limit 30.

        `status` filters by state ("open", "closed", or "any"). `labels`,
        `assignee`, and free-text `search` further narrow the result.
        `limit` is capped at the provider's max page size.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)   # optional — public repos work without
            tickets = provider.list_tickets(
                project, token,
                TicketFilters(
                    status=status,
                    labels=labels or [],
                    assignee=assignee,
                    search=search,
                    limit=limit,
                ),
            )
            return {
                "project_id": project.id,
                "tickets": [asdict(t) for t in tickets],
            }
        return _safe(go)

    @mcp.tool()
    def get_ticket(project_id: str, ticket_id: str) -> dict:
        """Get a ticket's full details, including all comments."""
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            ticket, comments = provider.get_ticket(project, token, ticket_id)
            return {
                "project_id": project.id,
                "ticket": asdict(ticket),
                "comments": [asdict(c) for c in comments],
            }
        return _safe(go)

    @mcp.tool()
    def create_ticket(
        project_id: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict:
        """Create a new ticket.

        Just create what the user asked for — DO NOT pre-inspect the
        repository or codebase to "gather context" first. The user can
        always provide more detail if they want it; one-shot create
        actions stay one-shot.

        The label `ai-generated` is added automatically by the server.
        Do not pass it yourself. Requires the project's `create`
        permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_create(project)
            token = _require_token(project)
            provider = _provider_for(project)
            ticket = provider.create_ticket(
                project, token, title, body, labels or [], assignees or [],
            )
            return {"project_id": project.id, "ticket": asdict(ticket)}
        return _safe(go)

    @mcp.tool()
    def update_ticket(
        project_id: str,
        ticket_id: str,
        title: str | None = None,
        body: str | None = None,
        status: Literal["open", "completed", "not_planned"] | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
    ) -> dict:
        """Update an existing ticket. Only specified fields change.

        `status` semantics:
          - "open":        reopen the ticket
          - "completed":   close as 'done as planned'
          - "not_planned": close as 'not planned' / declined / out-of-scope

        Label and assignee changes are add/remove operations relative to
        the current set; pass arrays of names. The label `ai-modified`
        is added automatically when the ticket wasn't previously
        `ai-generated`. Do not pass the marker labels yourself.

        Requires the project's `modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            ticket = provider.update_ticket(
                project, token, ticket_id,
                title=title, body=body, status=status,
                labels_add=labels_add, labels_remove=labels_remove,
                assignees_add=assignees_add, assignees_remove=assignees_remove,
            )
            return {"project_id": project.id, "ticket": asdict(ticket)}
        return _safe(go)

    @mcp.tool()
    def add_comment(project_id: str, ticket_id: str, body: str) -> dict:
        """Add a comment to a ticket.

        The body is automatically prefixed with `#ai-generated\\n\\n`.
        Do not add that prefix yourself. Requires the project's
        `modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            comment = provider.add_comment(project, token, ticket_id, body)
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)
