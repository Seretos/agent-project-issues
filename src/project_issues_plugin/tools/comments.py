"""Comment operations exposed to the agent.

Mirrors the shape of `tools/tickets.py`:
  - read-only ops (`list_comments`, `get_comment`) only require a token
    when the repo is private; no permission flag is needed.
  - write ops (`update_comment`) are gated by the project's `modify`
    permission, the same flag that gates `update_ticket` and
    `add_comment`.

The AI-marker prefix on `update_comment` is applied transparently by the
provider — the agent must NOT pass `#ai-generated` itself.

NOTE: `_PROVIDERS`, `_resolve`, `_provider_for`, `_require_token`,
`_require_modify`, and `_safe` are intentionally duplicated from
`tools/tickets.py` for now. Plan 2 (later) will lift this to a shared
`tools/_providers.py` module; until then keep these in sync with the
ticket-module copies.
"""
from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from project_issues_plugin.config import ProjectConfig, load_projects, resolve_token
from project_issues_plugin.providers.github import GitHubError, GitHubProvider


# Intentional duplication — see module docstring.
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

        Requires the project's `modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            comment = provider.update_comment(project, token, comment_id, body)
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)
