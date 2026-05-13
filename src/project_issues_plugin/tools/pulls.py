"""Pull-request operations exposed to the agent.

Mirrors `tools/tickets.py`:
  - read-only ops (`list_prs`, `get_pr`) only require a token when the
    repo is private; no permission flag is needed.
  - `create_pr` is gated by `pulls.create`.
  - `update_pr` and `add_pr_comment` are gated by `pulls.modify`.
  - `merge_pr` is gated by `pulls.merge` (new namespace, no flat-form
    equivalent — existing configs cannot merge without an explicit
    opt-in).

Markers (ai-generated label, ai-modified label, #ai-generated comment
prefix) are applied transparently by the provider.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from mcp.server.fastmcp import FastMCP

from project_issues_plugin.config import load_projects, resolve_token  # noqa: F401
from project_issues_plugin.providers.base import PRFilters
from project_issues_plugin.tools._providers import (
    _provider_for,
    _require_pulls_create,
    _require_pulls_merge,
    _require_pulls_modify,
    _require_token,
    _resolve,
    _safe,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_prs(
        project_id: str,
        status: Literal["open", "closed", "any"] = "open",
        labels: list[str] | None = None,
        assignee: str | None = None,
        head: str | None = None,
        base: str | None = None,
        search: str | None = None,
        limit: int = 30,
    ) -> dict:
        """List pull requests in a project. Default: open PRs, limit 30.

        Filter args:
          - `status`: "open" (default), "closed", or "any".
          - `labels`: only PRs carrying ALL of these labels.
          - `assignee`: only PRs assigned to this user.
          - `head`: filter by source branch (`branch` or `owner:branch`).
          - `base`: filter by target branch.
          - `search`: free-text query (GitHub search syntax, scoped to PRs).
          - `limit`: capped at the provider's max page size (100).

        Routing caveat: when `labels`, `assignee`, or `search` are set
        the provider switches from the cheap `/repos/.../pulls` endpoint
        to GitHub's Search API (`/search/issues`, `is:pr` qualifier),
        which has its own rate-limit bucket (30 req/min). The
        default-fast path stays on the cheap endpoint.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            prs = provider.list_prs(
                project, token,
                PRFilters(
                    status=status,
                    labels=labels or [],
                    assignee=assignee,
                    head=head,
                    base=base,
                    search=search,
                    limit=limit,
                ),
            )
            return {
                "project_id": project.id,
                "pull_requests": [asdict(pr) for pr in prs],
            }
        return _safe(go)

    @mcp.tool()
    def get_pr(project_id: str, pr_id: str) -> dict:
        """Get a pull request's details plus its discussion comments.

        The `comments` array contains issue-style comments only
        (`/issues/{n}/comments`). Inline code-review comments live on a
        separate endpoint and are not surfaced here.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            pr, comments = provider.get_pr(project, token, pr_id)
            return {
                "project_id": project.id,
                "pull_request": asdict(pr),
                "comments": [asdict(c) for c in comments],
            }
        return _safe(go)

    @mcp.tool()
    def create_pr(
        project_id: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict:
        """Create a pull request.

        Just create what the user asked for — DO NOT pre-inspect the
        repository or codebase to "gather context" first.

        `head` is the source branch (`feature/x` or `owner:branch` for
        cross-fork PRs); `base` is the target branch. The label
        `ai-generated` is added automatically by the server — do not
        pass it yourself. Requires the project's `pulls.create`
        permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_create(project)
            token = _require_token(project)
            provider = _provider_for(project)
            pr = provider.create_pr(
                project, token, title, body, head, base,
                draft=draft, labels=labels or [], assignees=assignees or [],
            )
            return {"project_id": project.id, "pull_request": asdict(pr)}
        return _safe(go)

    @mcp.tool()
    def update_pr(
        project_id: str,
        pr_id: str,
        title: str | None = None,
        body: str | None = None,
        status: Literal["open", "closed"] | None = None,
        base: str | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
    ) -> dict:
        """Update an existing pull request. Only specified fields change.

        `status` accepts `"open"` (reopen) or `"closed"` (close without
        merging). To merge a PR call `merge_pr` — passing
        `status="merged"` is rejected.

        Label and assignee changes are add/remove operations relative to
        the current set. The label `ai-modified` is added automatically
        when the PR wasn't previously `ai-generated`.

        Requires the project's `pulls.modify` permission.
        """
        # Tool-layer guard for the disallowed value — keeps the contract
        # explicit at the surface so the agent doesn't have to read the
        # provider source. `status: Literal["open", "closed"]` already
        # catches it at the MCP boundary; this is a defence-in-depth
        # check for direct callers (tests, in-process invocation).
        if status is not None and status not in ("open", "closed"):
            return {
                "error": (
                    f"status='{status}' is not supported by update_pr. "
                    "To merge a PR call merge_pr; to close it pass status='closed'."
                )
            }

        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            pr = provider.update_pr(
                project, token, pr_id,
                title=title, body=body, status=status, base=base,
                labels_add=labels_add, labels_remove=labels_remove,
                assignees_add=assignees_add, assignees_remove=assignees_remove,
            )
            return {"project_id": project.id, "pull_request": asdict(pr)}
        return _safe(go)

    @mcp.tool()
    def add_pr_comment(project_id: str, pr_id: str, body: str) -> dict:
        """Add a discussion comment to a pull request.

        Uses the shared issue-comments endpoint; the body is
        automatically prefixed with `#ai-generated\\n\\n`. Inline
        code-review comments are not supported by this tool.

        Requires the project's `pulls.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            comment = provider.add_pr_comment(project, token, pr_id, body)
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)

    @mcp.tool()
    def merge_pr(
        project_id: str,
        pr_id: str,
        merge_method: Literal["merge", "squash", "rebase"] = "merge",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> dict:
        """Merge a pull request.

        `merge_method` controls the merge style on GitHub:
          - "merge":  create a merge commit (default).
          - "squash": squash all commits into one and merge.
          - "rebase": rebase the head branch onto base.

        Requires the project's `pulls.merge` permission. This flag has
        no flat-form equivalent and defaults to False on existing
        configs — the user must explicitly opt in.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_merge(project)
            token = _require_token(project)
            provider = _provider_for(project)
            pr = provider.merge_pr(
                project, token, pr_id,
                merge_method=merge_method,
                commit_title=commit_title,
                commit_message=commit_message,
            )
            return {"project_id": project.id, "pull_request": asdict(pr)}
        return _safe(go)
