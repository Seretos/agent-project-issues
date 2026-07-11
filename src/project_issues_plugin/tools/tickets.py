"""Ticket operations exposed to the agent.

Permission gating happens here, before any provider call. Markers
(ai-generated label, ai-modified label, #ai-generated comment prefix)
are applied transparently by the provider — the agent does NOT pass
them and MUST NOT add them manually.
"""
from __future__ import annotations

import inspect
import time
from dataclasses import asdict
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from lib_python_projects import resolve_token
from lib_python_projects.providers.base import TicketFilters
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools._providers import (
    _normalize_id,
    _provider_for,
    _require_issues_create,
    _require_issues_modify,
    _require_token,
    _resolve,
    _rewrap_404,
    _safe,
)
from project_issues_plugin.tools._slicing import (
    apply_body_knobs,
    apply_omit_nulls,
    apply_order,
)

# TTL cache for `list_ticket_statuses`. Status workflows are static for
# GitHub/GitLab and only change on ADO when a project admin edits the
# process template — refreshing every hour is the documented trade-off
# (plan-comment for ticket #7, D3 = Option B).
_STATUS_CACHE_TTL_SECONDS = 60 * 60
_status_cache: dict[tuple[str, str | None], tuple[float, dict[str, Any]]] = {}


def _status_cache_clear() -> None:
    """Test-only hook — clears the module-level status cache."""
    _status_cache.clear()


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
        omit_body: bool = False,
        body_max_chars: int | None = None,
        omit_nulls: bool = False,
        states: list[str] | None = None,
        area_path: str | None = None,
        area_path_recursive: bool = True,
    ) -> dict:
        """List tickets in a project. Default: open tickets, limit 30.

        Filter args:
          - `status`: "open" (default), "closed", or "any". This filter
            uses a normalised vocabulary that the server maps to
            provider-native states — do NOT pass Azure DevOps native
            state names (e.g. `"To Do"`, `"Active"`) here. Use
            `list_ticket_statuses` to understand the state-space mapping,
            and pass native values only to `update_ticket`. The values
            `open`/`closed`/`any` are normalised by this tool — they are
            NOT valid inputs for `update_ticket` or `create_ticket`, which
            require provider-native strings from `list_ticket_statuses`.
          - `states`: provider-native state values (e.g. Azure DevOps
            `["New", "Approved"]`, or GitHub/GitLab `["open"]`) — call
            `list_ticket_statuses(project_id)` first to discover the
            valid strings for a project; pass them exactly as returned,
            no casing/whitespace normalisation. Supported on all three
            providers. A non-empty `states` list takes full precedence
            over `status` (including `status="any"`). An unknown value
            raises an error naming the accepted values.
          - `area_path`: Azure-DevOps-only `System.AreaPath` filter. On
            GitHub/GitLab a non-empty value raises an explicit error
            ("not supported ... it is an Azure DevOps concept") rather
            than being silently ignored. `area_path_recursive` (default
            `True`) selects `UNDER` (the path and all descendants) vs
            an exact `=` match on the single path; ignored when
            `area_path` is unset. No validation against Azure's
            classification-node tree — an invalid path simply yields
            zero matches instead of an error.
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
          - `limit`: capped at the provider's max page size (100). The
            response carries `has_more: bool` from the provider's
            pagination header (GitHub `Link rel=next`, GitLab
            `X-Next-Page`) and `applied_limit: int` — the effective
            cap actually used (equal to `limit` when no clamping
            occurs, 100 when `limit` exceeded the cap). For Azure
            DevOps, `has_more` is a heuristic: `true` when the number
            of returned items equals `limit`, because Azure DevOps
            returns work-item IDs without a continuation header.

        Token-cheap knobs (for discovery passes — finding IDs / titles /
        labels before deciding which tickets to `get_ticket`):
          - `omit_body=True`: drop the `body` field from every row.
            Recommended as the default for discovery passes — the
            response is ~10x smaller for bodies in the 2-10 KB range.
          - `body_max_chars=N`: truncate each row's body to N chars
            and add `body_truncated: bool` so you can tell the body
            is a prefix. `body_max_chars=N` measures N chars of content
            after the `#ai-generated`/`#ai-modified` marker prefix (if
            present), so the total stored body may be up to ~15 chars
            longer than N.
          - `omit_nulls=True`: drop top-level keys whose value is ``None``
            from every row (shallow strip — nested dicts are preserved
            intact). Note: a ticket row's top-level fields (`id`, `title`,
            `body`, `status`, `author`, `assignees`, `labels`, `url`,
            `created_at`, `updated_at`) are normally all populated, so
            this knob is usually a no-op for `list_tickets` — `omit_body`
            is the lever that actually shrinks ticket payloads. It earns
            its keep on row shapes that carry genuinely optional fields
            (e.g. `list_prs`, whose `mergeable` / `mergeable_state` /
            `merge_commit_sha` can be `null`). Combine with
            `omit_body=True` for the minimum-payload recipe when scanning
            titles / labels only.

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
            tickets, has_more = provider.list_tickets(
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
                    states=states or [],
                    area_path=area_path,
                    area_path_recursive=area_path_recursive,
                ),
            )
            rows = [asdict(t) for t in tickets]
            rows = apply_body_knobs(
                rows, omit_body=omit_body, body_max_chars=body_max_chars,
            )
            if omit_nulls:
                rows = apply_omit_nulls(rows)
            # Always echo `applied_limit` so callers can see what cap was
            # applied, whether or not clamping occurred (ticket #62).
            applied_limit = min(max(1, limit), 100)
            payload: dict[str, Any] = {
                "project_id": project.id,
                "tickets": rows,
                "has_more": has_more,
                "applied_limit": applied_limit,
            }
            return payload
        return _safe(go)

    @mcp.tool()
    def get_ticket(
        project_id: str,
        ticket_id: str,
        include_relations: bool = True,
        include_comments: bool = True,
        comments_limit: int | None = None,
        comments_order: Literal["asc", "desc"] = "asc",
        comments_body_max_chars: int | None = None,
    ) -> dict:
        """Get a ticket's full details, including all comments and relations.

        When `include_relations` is True (default), the response also
        includes a `relations` list describing typed links to other
        tickets / PRs. Relation kinds: `parent`, `child`, `closes`,
        `closed_by`, `duplicate_of`, `duplicated_by`, `mentions`,
        `mentioned_by`, `blocks`, `blocked_by` (GitHub + Azure DevOps),
        plus `relates_to` (GitLab + Azure DevOps). Each relation carries
        `ticket_id` (`"#N"` for same-repo, `"owner/repo#N"` for cross-repo),
        best-effort `title`, `url`, `state`
        (`"open"`/`"closed"`/`"merged"`/`""`), and `is_pull_request`.
        For outgoing relations parsed from the queried ticket's own body
        (`mentions`, `closes`, `duplicate_of`) we don't fetch the target,
        so `title` may be empty and `state` may be `""`.

        Each relation carries `resolved` (`bool | null`), which records
        HOW the relation's metadata was obtained — it is NOT a flag for
        whether the linked ticket is closed:
          - `true`  — the target was fetched live from the provider, so
            `title` / `url` / `state` are populated and current.
          - `false` — the relation was inferred from body / text, so
            empty `title` / `url` / `state` are expected. Empty here
            means "intentionally not fetched", NOT "fetch failed".
          - `null`  — liveness is unknown (the provider didn't signal).
        So a body-parsed `duplicate_of` reading `resolved: false,
        state: ""` is the normal, complete answer — do not retry it as
        if the lookup had failed.
        The boolean `relations_truncated` is true when the underlying
        timeline had more pages than we fetched. The comment-scan depth
        for `mentions` / `closes` is controlled by the
        `PROJECT_ISSUES_MENTIONS_SCAN_DEPTH` env var (`-1` = all comments,
        `0` = body only, `N` = first N comments).
        Set `include_relations=False` to save two API calls per request
        when relation context is not needed.

        Comment-slicing knobs:
          - `include_comments=False`: canonical "header-only" flag —
            omits the `comments` key from the response entirely and
            emits `comments_fetched: false`. Use when you only need
            the ticket header. `comments_limit=0` is an alias for
            this flag (not the reverse).
          - `comments_limit=N`: cap the returned comments to N. Combined
            with `comments_order="desc"` gives the last N comments.
            `comments_limit=0` is an alias for `include_comments=False`.
          - `comments_order="asc"|"desc"`: reverse the comments list.
            `desc` returns newest-first; pair with `comments_limit=N`
            for the "give me the most recent N" recipe.
          - `comments_body_max_chars=N`: truncate each comment body to
            N chars and add `body_truncated: bool`. Highest-leverage
            saving for dense threads. **Unbounded by default** — dense
            threads can produce very large payloads; recommend e.g.
            `comments_body_max_chars=500` for summary reads.
            `comments_body_max_chars=N` measures N chars of content after
            the `#ai-generated`/`#ai-modified` marker prefix (if present),
            so the total stored body may be up to ~15 chars longer than N.

        When comments are fetched, the response includes
        `comments_fetched: true` alongside the `comments` list.
        When skipped (via `include_comments=False` or
        `comments_limit=0`), the `comments` key is absent and
        `comments_fetched: false` is emitted instead. The
        `comments_fetched: false` key is present even when the caller
        explicitly opted out via `include_comments=False` — an
        intentional confirmation signal, not an oversight.

        When `include_relations=False` (or the provider skips the
        relation fetch), the `relations` and `relations_truncated` keys
        are absent and `relations_fetched: false` is emitted. When
        `include_relations=True` (default), `relations_fetched: true`
        is present alongside the `relations` list. The
        `relations_fetched: false` key is present even when the caller
        explicitly opted out via `include_relations=False` — an
        intentional confirmation signal, not an oversight.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            normalized_id = _normalize_id(project, ticket_id)
            try:
                ticket, comments, relations, truncated = provider.get_ticket(
                    project, token, normalized_id,
                    include_relations=include_relations,
                )
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="ticket",
                    ident=normalized_id,
                )
            # Apply the comment-slicing knobs.
            drop_comments = (not include_comments) or comments_limit == 0
            if drop_comments:
                comments_block: dict = {"comments_fetched": False}
            else:
                ordered = apply_order(comments, comments_order)
                if comments_limit is not None and comments_limit > 0:
                    ordered = ordered[:comments_limit]
                comment_rows = [asdict(c) for c in ordered]
                comment_rows = apply_body_knobs(
                    comment_rows,
                    omit_body=False,
                    body_max_chars=comments_body_max_chars,
                )
                comments_block = {
                    "comments": comment_rows,
                    "comments_fetched": True,
                }
            # The lib returns `truncated=None` (not `relations=None`) when
            # include_relations=False — `None` signals "skipped", while
            # `False` means "fetched but empty". `relations` is always a
            # list. Use `truncated is None` to detect the skipped case.
            if truncated is None:
                relations_block: dict = {"relations_fetched": False}
            else:
                relations_block = {
                    "relations": [asdict(rel) for rel in relations],
                    "relations_truncated": bool(truncated),
                    "relations_fetched": True,
                }
            return {
                "project_id": project.id,
                "ticket": asdict(ticket),
                **comments_block,
                **relations_block,
            }
        return _safe(go)

    @mcp.tool()
    def create_ticket(
        project_id: str,
        title: str,
        body: Annotated[str, Field(description="Ticket body. Use real U+000A newlines — the literal \\n escape is NOT normalised by the server. The #ai-generated marker is prepended automatically; do not include it.")] = "",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        status: str | None = None,
    ) -> dict:
        """Create a new ticket.

        Just create what the user asked for — DO NOT pre-inspect the
        repository or codebase to "gather context" first. The user can
        always provide more detail if they want it; one-shot create
        actions stay one-shot.

        The label `ai-generated` is added automatically by the server.
        Do not pass it yourself.

        `body` is optional. When omitted, the ticket is created with an
        empty body. Provide it when the user has supplied a description.
        `body` must contain real newline characters (U+000A), not the
        two-character literal sequence `\n`; the server performs no
        escape-sequence normalisation.

        The server also prepends `#ai-generated\n\n` to the body
        automatically. Do not prepend it yourself; if you do, the marker
        is deduplicated (no stacking).

        `labels` sets the initial label set as a flat list on creation.
        To add or remove labels on an existing ticket after creation,
        use `update_ticket`'s `labels_add` / `labels_remove` parameters
        instead.

        `status` is optional. When omitted, the ticket lands in the
        project's `hints.default_open` state (the normal case). When
        supplied, it must be a value from the provider's state-space —
        the same vocabulary `update_ticket.status` accepts. Pass the
        value exactly as returned by `list_ticket_statuses` — do not
        normalise casing or whitespace. Use this when importing
        already-resolved tickets or filing documentation tickets that
        should be born closed, instead of a two-step create-then-close
        that emits a spurious `opened → closed` pair in the timeline.
        Unknown values raise the same error type as `update_ticket`
        with a hint to call `list_ticket_statuses`.

        Requires the project's `issues.create` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_create(project)
            token = _require_token(project)
            provider = _provider_for(project)
            ticket = provider.create_ticket(
                project, token, title, body, labels or [], assignees or [],
                status=status,
            )
            return {"project_id": project.id, "ticket": asdict(ticket)}
        return _safe(go)

    _UPDATE_TICKET_FIELDS = (
        "title", "body", "status",
        "labels_add", "labels_remove",
        "assignees_add", "assignees_remove",
        "custom_fields",
    )

    @mcp.tool()
    def update_ticket(
        project_id: str,
        ticket_id: str,
        title: str | None = None,
        body: Annotated[str | None, Field(description="New body text. Use real U+000A newlines — the literal \\n escape is NOT normalised by the server. The correct #ai-generated/#ai-modified marker is prepended automatically; do not include it.")] = None,
        status: str | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
        custom_fields: Annotated[
            dict[str, Any] | None,
            Field(description=(
                "Provider-specific field overrides as a key→value map. "
                "Only supported by Azure DevOps. On GitHub and GitLab it is "
                "silently dropped when combined with standard fields, but a "
                "custom_fields-only call returns an error. "
                "Use the full dotted field reference, e.g. "
                "{'Custom.ProcessState': 'Approved'} or {'System.Tags': 'release'}. "
                "Call list_custom_fields(project_id) to discover available field "
                "reference names and their allowed values before setting them."
            ))
        ] = None,
    ) -> dict:
        """Update an existing ticket. Only specified fields change.

        `status` is the **provider-native** status string. For GitHub
        the accepted values are `open`, `closed:completed`,
        and `closed:not_planned` (where the `state:state_reason`
        suffix carries GitHub's "done as planned" vs "not planned"
        distinction). For Azure DevOps the value is whatever the
        project's process template defines (Basic: `To Do`/`Doing`/`Done`;
        Agile: `New`/`Active`/`Resolved`/`Closed`/`Removed`; Scrum: ...).
        Call `list_ticket_statuses(project_id)` to enumerate the valid
        values for the current project. Pass these values exactly as
        returned by `list_ticket_statuses` — do not normalise casing
        or whitespace (e.g. `"To Do"` must not become `"to do"`).

        Agents that don't know the provider's state-space should call
        `list_ticket_statuses(project_id)` first and read
        `hints.terminal_completed` / `hints.terminal_declined` /
        `hints.default_open`. Only values returned by
        `list_ticket_statuses` are valid; unknown values raise an error
        so the agent is directed to re-discover the state-space rather
        than silently succeeding with the wrong outcome.

        Label and assignee changes are add/remove operations relative to
        the current set; pass arrays of names. The label `ai-modified`
        is added automatically when the ticket wasn't previously
        `ai-generated`. Do not pass the marker labels yourself. For
        initial label assignment at creation time, use `create_ticket`'s
        `labels` parameter (a flat list, not add/remove).

        When `body` is supplied, it is rewritten so the first line is
        exactly one `#ai-*` marker matching the ticket's post-update
        label state: `#ai-generated` for AI-authored tickets,
        `#ai-modified` for first AI touches on a human-authored ticket.
        Callers should NOT prepend the marker themselves; if they do,
        the existing marker line is stripped and the correct one is
        prepended (no stacking).

        **Response shape:** mirrors `create_ticket` — returns the full
        `ticket` object as echoed by the provider after the update:

        ```
        {
          "project_id": str,
          "ticket": {
            "id":         str,
            "title":      str,
            "body":       str,
            "status":     str,
            "author":     str,
            "labels":     [str, ...],
            "assignees":  [str, ...],
            "url":        str,
            "created_at": str,
            "updated_at": str,
          }
        }
        ```

        All fields from the provider's post-update `Ticket` dataclass
        are echoed back, including `title` and `body`. Server-side
        mutations (e.g. auto-prepended `#ai-modified` marker in `body`,
        auto-added `ai-modified` label) are reflected in the response.

        When `body` is supplied, it must contain real newline characters
        (U+000A), not the two-character literal sequence `\n`; the
        server performs no escape-sequence normalisation.

        Requires the project's `issues.modify` permission.

        `custom_fields` passes provider-specific `{field_ref: value}` overrides
        to the underlying provider. It is currently only supported by Azure DevOps,
        where full dotted field references such as `Custom.ProcessState` or
        `System.Tags` are used. On GitHub and GitLab the parameter is silently
        dropped when other standard fields are present; a `custom_fields`-only
        call on those providers returns a descriptive error rather than silently
        mutating nothing of what was asked. Passing `None` or an empty dict `{}`
        means "no custom fields" and is treated as though the argument were absent.
        """
        # Reject empty calls explicitly (ticket #48 finding 4 / #49 finding 4).
        # `labels_add=[]` etc. are treated as "no action" — only non-empty
        # collections and non-None scalars count.
        actionable = (
            title is not None
            or body is not None
            or status is not None
            or labels_add
            or labels_remove
            or assignees_add
            or assignees_remove
            or custom_fields
        )
        if not actionable:
            return {
                "error": (
                    "no update fields supplied; pass at least one of "
                    "title/body/status/labels_add/labels_remove/"
                    "assignees_add/assignees_remove/custom_fields."
                )
            }

        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_id = _normalize_id(project, ticket_id)
            cf_supported = "custom_fields" in inspect.signature(
                provider.update_ticket
            ).parameters
            has_other_fields = any([
                title is not None, body is not None, status is not None,
                labels_add, labels_remove, assignees_add, assignees_remove,
            ])
            if custom_fields and not cf_supported and not has_other_fields:
                return {
                    "error": (
                        f"custom_fields is not supported by the '{project.provider}' "
                        "provider; it is only available on Azure DevOps. Supply at least "
                        "one standard field (title/body/status/labels/assignees) or omit "
                        "custom_fields."
                    )
                }
            kwargs: dict[str, Any] = dict(
                title=title, body=body, status=status,
                labels_add=labels_add, labels_remove=labels_remove,
                assignees_add=assignees_add, assignees_remove=assignees_remove,
            )
            if custom_fields and cf_supported:
                kwargs["custom_fields"] = custom_fields
            try:
                ticket = provider.update_ticket(project, token, normalized_id, **kwargs)
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="ticket",
                    ident=normalized_id,
                )
            return {
                "project_id": project.id,
                "ticket": asdict(ticket),
            }
        return _safe(go)

    @mcp.tool()
    def list_ticket_statuses(project_id: str) -> dict:
        """Discover the provider-native status state-space.

        Returns:

        ```
        {
          "project_id": str,
          "provider": "github" | "gitlab" | "azuredevops",
          "values":      [str, ...],          # all valid `status` strings
          "transitions": {str: [str, ...]},   # legal next values per status
          "hints": {
            "default_open":       str,
            "terminal":           [str, ...],
            "terminal_completed": str,
            "terminal_declined":  str,
          }
        }
        ```

        Any `hints` value can be `null` when the workflow has no state
        that fills that role — e.g. an Azure DevOps process with no
        dedicated declined/abandoned terminal reports
        `terminal_declined: null`. A `null` here is a stable fact about
        the project's workflow ("this process has no such state"), not
        "unsupported", "misconfigured", or "retry later" — don't call
        again hoping it resolves. Pick a real value from `values` /
        `transitions` instead.

        Use this to discover what `update_ticket.status` accepts for a
        given project, especially when the provider has a customisable
        workflow (Azure DevOps, Jira). For GitHub the state-space is
        static so the response is identical for every GitHub project.
        The strings in `values` are provider-literal (exact casing and
        whitespace) and must be passed to `update_ticket` or
        `create_ticket` verbatim — do not normalise casing or whitespace.

        Results are cached server-side for ~1h per `(project_id, token)`
        pair (workflow definitions change rarely). Read-only: no
        permission flag required.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            cache_key = (project.id, token)
            now = time.time()
            cached = _status_cache.get(cache_key)
            if cached is not None and (now - cached[0]) < _STATUS_CACHE_TTL_SECONDS:
                return cached[1]
            spec = provider.list_statuses(project, token)
            payload = {
                "project_id": project.id,
                "provider": project.provider,
                "values": list(spec.values),
                "transitions": {k: list(v) for k, v in spec.transitions.items()},
                "hints": dict(spec.hints),
            }
            _status_cache[cache_key] = (now, payload)
            return payload
        return _safe(go)

    @mcp.tool()
    def list_custom_fields(project_id: str, work_item_type: str | None = None) -> dict:
        """Discover provider-native custom and work-item fields.

        Returns the structured field schema for the project's provider.
        Azure DevOps is the primary use case — it exposes a rich schema
        with typed fields, picklist constraints, and per-work-item-type
        scoping. GitHub and GitLab have no structured field schema and
        always return `"fields": []` (not an error — it is a stable fact
        about those providers, not "unsupported" or "retry later").

        The `work_item_type` parameter optionally scopes the field set to
        a specific Azure DevOps work-item type (e.g. `"Bug"`, `"Task"`).
        When omitted the provider returns all fields across all types.
        It is silently ignored by GitHub and GitLab.

        The `reference_name` and `allowed_values` values returned here
        feed directly into `update_ticket`'s `custom_fields` parameter —
        call this tool first to discover valid field references and their
        allowed picklist values before setting them.

        Returns:

        ```
        {
          "project_id": str,
          "provider": "github" | "gitlab" | "azuredevops",
          "fields": [
            {
              "reference_name":   str,        # provider-native id, e.g. "System.State"
              "display_name":     str,        # human-readable label
              "type":             str,        # field type, e.g. "string", "picklistString"
              "allowed_values":   [str] | null,  # picklist values; null when free-form
              "read_only":        bool,
              "always_required":  bool
            },
            ...
          ]
        }
        ```

        Read-only: no permission flag required beyond a valid token.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            fields = provider.list_fields(project, token, work_item_type=work_item_type)
            return {
                "project_id": project.id,
                "provider": project.provider,
                "fields": [asdict(f) for f in fields],
            }
        return _safe(go)

    @mcp.tool()
    def add_comment(project_id: str, ticket_id: str, body: Annotated[str, Field(description="Comment content. Do not include '#ai-generated' — the server prepends it automatically.")]) -> dict:
        """Add a comment to a ticket.

        CAUTION: do NOT include `#ai-generated` in `body` — the server
        prepends it automatically. If you are doing a read-modify-write
        loop (get body → edit → update), you must strip the marker from
        the stored body before passing it here; `update_comment`
        re-applies the correct marker and will deduplicate it, but
        relying on deduplication is fragile.

        The body is automatically prefixed with `#ai-generated\\n\\n`.
        Do not add that prefix yourself. Requires the project's
        `issues.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_id = _normalize_id(project, ticket_id)
            if not body or not body.strip():
                raise ValueError("comment body must be non-empty")
            try:
                comment = provider.add_comment(project, token, normalized_id, body)
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="ticket",
                    ident=normalized_id,
                )
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)
