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
            comments = provider.list_comments(project, token, ticket_id, limit=limit)
            return {
                "project_id": project.id,
                "ticket_id": ticket_id,
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

        `ticket_id` is accepted for surface symmetry with `add_comment`
        but is not used by the underlying request — GitHub comment ids
        are repo-wide and look up the comment directly.

        Read-only: requires a token only if the repo is private.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            comment = provider.get_comment(project, token, comment_id)
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

        `ticket_id` is accepted for surface symmetry with `add_comment`
        but is not used by the underlying request (comment ids are
        repo-wide). The body is automatically prefixed with
        `#ai-generated\\n\\n` if it doesn't already carry the marker —
        do not add that prefix yourself.

        Requires the project's `issues.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            comment = provider.update_comment(project, token, comment_id, body)
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)
