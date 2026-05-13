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

from project_issues_plugin.config import resolve_token
from project_issues_plugin.providers.base import TicketFilters
from project_issues_plugin.tools._providers import (
    _provider_for,
    _require_issues_create,
    _require_issues_modify,
    _require_token,
    _resolve,
    _safe,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_tickets(
        project_id: str,
        status: Literal["open", "closed", "any"] = "open",
        labels: list[str] | None = None,
        assignee: str | None = None,
        search: str | None = None,
        limit: int = 30,
        not_labels: list[str] | None = None,
        author: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
        sort_by: Literal["created", "updated", "comments"] = "created",
        sort_order: Literal["asc", "desc"] = "desc",
    ) -> dict:
        """List tickets in a project. Default: open tickets, limit 30.

        Filter args:
          - `status`: "open" (default), "closed", or "any".
          - `labels`: only tickets carrying ALL of these labels.
          - `not_labels`: exclude tickets carrying ANY of these labels
            (e.g. `["test"]` filters out test issues).
          - `assignee`: only tickets assigned to this user.
          - `author`: only tickets opened by this user.
          - `search`: free-text query (substring + GitHub search syntax).
          - `created_after` / `created_before`: ISO date (`YYYY-MM-DD`
            or full ISO-8601 timestamp); inclusive bounds on `created_at`.
          - `updated_after` / `updated_before`: same, for `updated_at`.
          - `sort_by`: `"created"` (default), `"updated"`, or `"comments"`.
          - `sort_order`: `"desc"` (default) or `"asc"`.
          - `limit`: capped at the provider's max page size (100).

        Routing caveat: when any of `not_labels`, `author`,
        `created_*`, `updated_*`, or `search` is set, the provider
        switches from the cheap `/repos/.../issues` endpoint to GitHub's
        Search API (`/search/issues`), which has its own rate-limit
        bucket (30 requests/minute). The default-fast path stays on the
        cheap endpoint.
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
                    not_labels=not_labels or [],
                    author=author,
                    created_after=created_after,
                    created_before=created_before,
                    updated_after=updated_after,
                    updated_before=updated_before,
                    sort_by=sort_by,
                    sort_order=sort_order,
                ),
            )
            return {
                "project_id": project.id,
                "tickets": [asdict(t) for t in tickets],
            }
        return _safe(go)

    @mcp.tool()
    def get_ticket(
        project_id: str,
        ticket_id: str,
        include_relations: bool = True,
    ) -> dict:
        """Get a ticket's full details, including all comments and relations.

        When `include_relations` is True (default), the response also
        includes a `relations` list describing typed links to other
        tickets / PRs. Relation kinds: `parent`, `child`, `closes`,
        `closed_by`, `duplicate_of`, `duplicated_by`, `mentions`,
        `mentioned_by` (GitHub) plus `relates_to`, `blocks`,
        `blocked_by` (GitLab, reserved). Each relation carries
        `ticket_id` (`"#N"` for same-repo, `"owner/repo#N"` for cross-repo),
        best-effort `title`, `url`, `state`
        (`"open"`/`"closed"`/`"merged"`/`""`), and `is_pull_request`.
        The boolean `relations_truncated` is true when the underlying
        timeline had more pages than we fetched.
        Set `include_relations=False` to save two API calls per request
        when relation context is not needed.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            ticket, comments, relations, truncated = provider.get_ticket(
                project, token, ticket_id, include_relations=include_relations,
            )
            return {
                "project_id": project.id,
                "ticket": asdict(ticket),
                "comments": [asdict(c) for c in comments],
                "relations": [asdict(rel) for rel in relations],
                "relations_truncated": truncated,
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
        Do not pass it yourself. Requires the project's `issues.create`
        permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_create(project)
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

        Requires the project's `issues.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
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
        `issues.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            comment = provider.add_comment(project, token, ticket_id, body)
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)
