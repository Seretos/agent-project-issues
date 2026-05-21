"""Comment operations exposed to the agent.

Mirrors the shape of `tools/tickets.py`:
  - read-only ops (`list_comments`, `get_comment`) only require a token
    when the repo is private; no permission flag is needed.
  - write ops (`update_comment`) are gated by the project's
    `issues.modify` permission, the same flag that gates `update_ticket`
    and `add_comment`.

The AI-marker prefix on `update_comment` is applied transparently by the
provider — the agent must NOT pass `#ai-generated` itself.

The shared `_PROVIDERS`/`_resolve`/`_safe`/permission helpers live in
`tools/_providers.py`.
"""
from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

# `load_projects` is re-exported here purely so tests that monkey-patch
# `tools.comments.load_projects` keep working. The runtime call path goes
# through `tools/_providers.py::_resolve` which reads via the config module.
from project_issues_plugin.config import load_projects, resolve_token  # noqa: F401
from project_issues_plugin.tools._providers import (
    _normalize_id,
    _provider_for,
    _require_issues_modify,
    _require_token,
    _resolve,
    _safe,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_comments(
        project_id: str,
        ticket_id: str,
        limit: int = 30,
    ) -> dict:
        """List comments on a ticket. Default limit 30 (capped at 100).

        Read-only: requires a token only if the repo is private.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            normalized_id = _normalize_id(project, ticket_id)
            comments = provider.list_comments(
                project, token, normalized_id, limit=limit,
            )
            return {
                "project_id": project.id,
                "ticket_id": normalized_id,
                "comments": [asdict(c) for c in comments],
            }
        return _safe(go)

    @mcp.tool()
    def get_comment(
        project_id: str,
        ticket_id: str,
        comment_id: str,
    ) -> dict:
        """Get a single comment by id.

        `ticket_id` carries consistent semantics across providers
        (ticket #41 addendum):
          - GitHub: unused (comment ids are repo-wide).
          - GitLab: required when `comment_id` is a bare note id (as
            returned by `add_comment`). Composite `"<iid>/<note_id>"`
            in `comment_id` keeps working too — `ticket_id` is then
            ignored.

        Read-only: requires a token only if the repo is private.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            normalized_ticket = _normalize_id(project, ticket_id)
            comment = provider.get_comment(
                project, token, comment_id, ticket_id=normalized_ticket,
            )
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)

    @mcp.tool()
    def update_comment(
        project_id: str,
        ticket_id: str,
        comment_id: str,
        body: str,
    ) -> dict:
        """Update an existing comment's body.

        `ticket_id` carries the same cross-provider semantics as in
        `get_comment` — on GitLab it's used to address the note when
        `comment_id` is a bare note id (ticket #41 addendum).

        The body is rewritten so the first line is exactly one `#ai-*`
        marker matching the comment's authorship: `#ai-generated` if
        the existing comment already carries that marker (we wrote it
        originally), `#ai-modified` otherwise (first AI edit of a
        human-authored comment). Callers should NOT prepend the marker
        themselves; if they do, the existing marker line is stripped
        and the correct one is prepended.

        Requires the project's `issues.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_ticket = _normalize_id(project, ticket_id)
            comment = provider.update_comment(
                project, token, comment_id, body, ticket_id=normalized_ticket,
            )
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)
