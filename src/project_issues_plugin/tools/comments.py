"""Comment operations exposed to the agent.

Mirrors the shape of `tools/tickets.py`:
  - read-only ops (`list_comments`, `get_comment`) only require a token
    when the repo is private; no permission flag is needed.
  - write ops (`update_comment`, `delete_comment`) are gated by the
    project's `issues.modify` permission, the same flag that gates
    `update_ticket` and `add_comment`.

The AI-marker prefix on `update_comment` is applied transparently by the
provider — the agent must NOT pass `#ai-generated` itself.

The shared `_PROVIDERS`/`_resolve`/`_safe`/permission helpers live in
`tools/_providers.py`.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP

# `load_projects` is re-exported here purely so tests that monkey-patch
# `tools.comments.load_projects` keep working. The runtime call path goes
# through `tools/_providers.py::_resolve` which reads via the lib loader.
from lib_python_projects import load_projects, resolve_token  # noqa: F401
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools._providers import (
    _normalize_id,
    _provider_for,
    _require_issues_modify,
    _require_token,
    _resolve,
    _rewrap_404,
    _safe,
)
from project_issues_plugin.tools._slicing import apply_body_knobs, apply_order


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_comments(
        project_id: str,
        ticket_id: str,
        limit: int = 30,
        order: Literal["asc", "desc"] = "asc",
        since: str | None = None,
        page: int = 1,
        omit_body: bool = False,
        body_max_chars: int | None = None,
    ) -> dict:
        """List comments on a ticket. Default: oldest-first, limit 30 (cap 100).

        Args:
          - `order`: `"asc"` (default, chronological) or `"desc"`
            (reverse). For the "give me the most recent N comments"
            use-case, pass `order="desc", limit=N` — the page is fetched
            ascending from the provider and reversed client-side. On
            threads longer than `limit`, the reversed slice covers only
            the FIRST page; pass an explicit `page` to walk older
            comments.
          - `since`: ISO-8601 timestamp. Comments with `created_at`
            (GitHub) / `updated_after` (GitLab) at or after this
            instant are returned. Useful for "what changed since my
            last check".
          - `page`: 1-based page number; combine with `limit` (=
            per_page). The response carries `has_more: bool` from the
            provider's pagination header (GitHub `Link rel=next`,
            GitLab `X-Next-Page`).

        Token-cheap knobs:
          - `omit_body=True`: drop the `body` field from every row.
          - `body_max_chars=N`: truncate each comment body to N chars
            and add `body_truncated: bool` per row.
            `body_max_chars=N` measures N chars of content after the
            `#ai-generated`/`#ai-modified` marker prefix (if present),
            so the total stored body may be up to ~15 chars longer than N.

        Read-only: requires a token only if the repo is private.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            normalized_id = _normalize_id(project, ticket_id)
            # Pass `order` down — the provider does a smart tail-fetch
            # for desc+page=1+no-since so `order="desc", limit=N` actually
            # returns the LAST N comments (ticket #47 follow-up). For
            # explicit `page=N` or `since=…`, the provider falls back to
            # the regular ascending fetch and we reverse client-side.
            comments, has_more = provider.list_comments(
                project, token, normalized_id,
                limit=limit, since=since, page=page, order=order,
            )
            tail_fetched = (order == "desc" and page == 1 and not since)
            ordered = comments if tail_fetched else apply_order(comments, order)
            rows = [asdict(c) for c in ordered]
            rows = apply_body_knobs(
                rows, omit_body=omit_body, body_max_chars=body_max_chars,
            )
            applied_limit = min(max(1, limit), 100)
            result: dict = {
                "project_id": project.id,
                "ticket_id": normalized_id,
                "comments": rows,
                "page": page,
                "has_more": has_more,
                "applied_limit": applied_limit,
            }
            return result
        return _safe(go)

    @mcp.tool()
    def get_comment(
        project_id: str,
        comment_id: str,
        ticket_id: Annotated[str | None, Field(description="Required for GitLab (bare note id) and Azure DevOps (work-item-scoped); optional for GitHub where comment ids are repo-wide. Alternatively encode the comment id as '<iid>/<note_id>' so it is self-contained.")] = None,
    ) -> dict:
        """Get a single comment by id.

        `ticket_id` carries consistent semantics across providers:
          - GitHub: unused (comment ids are repo-wide). May be omitted
            or set to `None`.
          - GitLab: required when `comment_id` is a bare note id (as
            returned by `add_comment`). Composite `"<iid>/<note_id>"`
            in `comment_id` keeps working too — `ticket_id` is then
            ignored.
          - Azure DevOps: always required — work-item comment ids are
            scoped to a work item (`workItems/{ticket_id}/comments/
            {comment_id}`); omitting it returns a structured error.

        Read-only: requires a token only if the repo is private.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            normalized_ticket = _normalize_id(project, ticket_id)
            normalized_comment = _normalize_id(project, comment_id)
            try:
                comment = provider.get_comment(
                    project, token, normalized_comment, ticket_id=normalized_ticket,
                )
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="comment",
                    ident=normalized_comment,
                )
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)

    @mcp.tool()
    def update_comment(
        project_id: str,
        comment_id: str,
        body: str,
        ticket_id: Annotated[str | None, Field(description="Required for GitLab (bare note id) and Azure DevOps (work-item-scoped); optional for GitHub where comment ids are repo-wide. Alternatively encode the comment id as '<iid>/<note_id>' so it is self-contained.")] = None,
    ) -> dict:
        """Update an existing comment's body.

        `ticket_id` carries consistent semantics across providers:
          - GitHub: unused (comment ids are repo-wide). May be omitted
            or set to `None`.
          - GitLab: required when `comment_id` is a bare note id (as
            returned by `add_comment`). Composite `"<iid>/<note_id>"`
            in `comment_id` keeps working too — `ticket_id` is then
            ignored.
          - Azure DevOps: always required — work-item comment ids are
            scoped to a work item (`workItems/{ticket_id}/comments/
            {comment_id}`); omitting it returns a structured error.

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

    @mcp.tool()
    def delete_comment(
        project_id: str,
        comment_id: str,
        ticket_id: Annotated[str | None, Field(description="Required for GitLab (bare note id) and Azure DevOps (work-item-scoped); optional for GitHub where comment ids are repo-wide. Alternatively encode the comment id as '<iid>/<note_id>' so it is self-contained.")] = None,
    ) -> dict:
        """Delete an existing comment by id.

        `ticket_id` carries consistent semantics across providers:
          - GitHub: unused (comment ids are repo-wide). May be omitted
            or set to `None`.
          - GitLab: required when `comment_id` is a bare note id (as
            returned by `add_comment`). Composite `"<iid>/<note_id>"`
            in `comment_id` keeps working too — `ticket_id` is then
            ignored.
          - Azure DevOps: always required — work-item comment ids are
            scoped to a work item (`workItems/{ticket_id}/comments/
            {comment_id}`); omitting it returns a structured error.

        Requires the project's `issues.modify` permission.

        Returns `{"project_id": ..., "deleted": True, "comment_id": ...}`
        on success. Raises a structured 404 error if the comment does not
        exist.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_ticket = _normalize_id(project, ticket_id)
            normalized_comment = _normalize_id(project, comment_id)
            try:
                provider.delete_comment(
                    project, token, normalized_comment, ticket_id=normalized_ticket,
                )
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="comment",
                    ident=normalized_comment,
                )
            return {"project_id": project.id, "deleted": True, "comment_id": normalized_comment}
        return _safe(go)
