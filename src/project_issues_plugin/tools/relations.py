"""Write-side support for typed ticket relations (ticket #41).

`get_ticket(include_relations=True)` already returns a typed
`relations[]` list (`parent`, `blocks`, `duplicate_of`, ...). This
module exposes the missing write side: `add_relation` and
`remove_relation`.

Provider mapping is opaque to the agent — the same `kind` vocabulary
is accepted on both providers, and kinds that the underlying provider
cannot model natively surface as `RelationKindUnsupported`
(translated to `{"error": "..."}` by the generic `_safe` wrapper).
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from lib_python_projects.providers.azuredevops import (
    SUPPORTED_RELATION_KINDS as _AZURE_SUPPORTED_RELATION_KINDS,
)
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.base import READ_ONLY_RELATION_KINDS, WRITABLE_RELATION_KINDS
from lib_python_projects.providers.github import GitHubError, GitHubProvider
from lib_python_projects.providers.gitlab import GitLabError, GitLabProvider
from project_issues_plugin.tools._providers import (
    _normalize_id,
    _normalize_target,
    _provider_for,
    _require_issues_modify,
    _require_token,
    _resolve,
    _rewrap_404,
    _rewrap_azure_single_parent,
    _safe,
    resolve_token,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def add_relation(
        project_id: str,
        ticket_id: str,
        kind: Annotated[str, Field(description="Relation kind. One of: parent, child, blocks, blocked_by, duplicate_of, relates_to. Call list_relation_kinds for provider-specific support matrix.")],
        target: Annotated[str, Field(description="Target issue reference. Preferred form: '#N' (e.g. '#7'). Also accepts a bare integer ('7') or a full issue URL. Cross-repo 'owner/repo#N' references are rejected by the provider.")],
    ) -> dict:
        """Create a typed relation from `ticket_id` (the source) to
        `target` (the destination).

        Returns:

        ```
        {
          "project_id": str,
          "relation": {
            "kind":             str,
            "ticket_id":        str,
            "title":            str,
            "url":              str,
            "state":            str,
            "is_pull_request":  bool,
            "resolved":         bool | null
          }
        }
        ```

        `relation.ticket_id` is the **target/other** ticket's id (the
        "to" end resolved from `target`) — distinct from the
        `ticket_id` request parameter above, which identifies the
        source ticket.

        The returned `relation` object is **fully hydrated** — when
        `resolved` is `true` it was fetched live from the provider, so
        `title` / `state` / `url` are real — and has the **same shape
        as an entry in `get_ticket(ticket_id,
        include_relations=True).relations[]`** (and as `list_hierarchy`'s
        `parent`/`children` entries): `kind`, `ticket_id`, `title`,
        `url`, `state`, `is_pull_request`, `resolved`. It describes only
        the single relation just created, framed from the source
        `ticket_id` outward (`relation.ticket_id` is the target); to see
        the source ticket's full relations list, call
        `get_ticket(ticket_id, include_relations=True)`.

        `resolved` documents how the relation metadata was obtained:
          - `true`  — target was fetched from the provider API; title /
            state / url are live.
          - `false` — target was inferred from body or text scanning;
            title / state may be empty.
          - `null`  — liveness is unknown (provider did not indicate).

        Direction matters for the asymmetric kinds. `ticket_id` is
        always the "from" end and `target` the "to" end:
          - `blocks`:     `ticket_id` blocks `target`.
          - `blocked_by`: `ticket_id` is blocked by `target`.
          - `parent`:     `ticket_id` is the parent of `target`.
          - `child`:      `ticket_id` is a child of `target`.
        (`relates_to` and `duplicate_of` read the same either way.)

        `kind` is one of: `parent`, `child`, `blocks`, `blocked_by`,
        `duplicate_of`, `relates_to`. Not every provider models every
        kind — call `list_relation_kinds` for the per-provider support
        matrix before adding one, rather than discovering gaps via
        failed calls. The read-only inverse kinds
        (`closed_by`, `duplicated_by`, `mentioned_by`, `mentions`) are
        not directly settable — they emerge from the other side of a
        write or from body content scanned by the read path.

        `target` is a same-project issue reference. Accepted forms:
          - Bare integer: `7`
          - Hash-prefixed: `#7`
          - Full provider issue/PR URL (e.g.
            `https://github.com/acme/backend/issues/7`)

        Cross-project references are rejected at two levels depending
        on their shape:
          - `owner/repo#N` or `owner/repo!N` (contains `/` and `#`/`!`,
            the GitLab MR form): passes through `_normalize_target`
            unchanged, then the provider rejects it with a
            `NotImplementedError` indicating cross-project/cross-repo
            targets are not yet supported.
          - Inputs with a `/` but no `#` or `!` (e.g. `owner/repo`):
            rejected earlier by `_normalize_target` with `ValueError:
            "target 'owner/repo' could not be normalised — expected a bare
            number, '#N', a full issue/PR URL, or a cross-repo reference
            ('owner/repo#N', 'group/project#N', or 'group/project!N')"`.

        Symmetry: `add_relation(A, kind=parent, target=B)` is a logical
        alias for `add_relation(B, kind=child, target=A)`. Pick the
        form that reads most naturally for the source ticket.

        Provider-specific notes:
          - GitHub `relates_to` → unsupported (no native typed link).
          - GitLab `parent` / `child` → backed by the Work Items GraphQL
            `hierarchyWidget` (see `_gitlab_add_hierarchy_relation` /
            `_gitlab_remove_hierarchy_relation`); both ends are resolved
            to GraphQL work-item ids first, raising `RelationNotFound`
            if either side can't be resolved.
          - `duplicate_of` triggers a body-edit and closes the source
            on GitHub and GitLab — the marker line `#ai-generated` /
            `#ai-modified` is preserved correctly via the shared
            marker helper. (Azure DevOps' `duplicate_of` side-effect
            is implemented in the `lib-python-projects` lib and is
            not independently verified from this repo.)

        Requires the project's `issues.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_ticket = _normalize_id(project, ticket_id)
            normalized_target = _normalize_target(project, target)
            try:
                relation = provider.add_relation(
                    project, token, normalized_ticket, kind, normalized_target,
                )
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_azure_single_parent(
                    exc, ticket_id=normalized_ticket, target=normalized_target,
                    kind=kind,
                )
            return {
                "project_id": project.id,
                "relation": asdict(relation),
            }
        return _safe(go)

    @mcp.tool()
    def remove_relation(
        project_id: str,
        ticket_id: str,
        kind: Annotated[str, Field(description="Relation kind. One of: parent, child, blocks, blocked_by, duplicate_of, relates_to. Call list_relation_kinds for provider-specific support matrix.")],
        target: str,
    ) -> dict:
        """Remove a typed relation between `ticket_id` and `target`.

        Inverse of `add_relation`. `kind` must match the same
        vocabulary; the operation is best-effort: removing a relation
        that doesn't exist raises `{"error": "..."}` rather than a
        silent success so the agent can branch on the failure.

        For `duplicate_of`, removal reopens the source ticket and the
        `Duplicate of #N` line is removed from the body automatically.

        `kind` accepts the same vocabulary as `add_relation` — call
        `list_relation_kinds` for the per-provider support matrix.

        Requires the project's `issues.modify` permission.

        Returns `{"project_id": str, "kind": str, "target": str, "removed": true}`
        on success. (A relation that did not exist surfaces as
        `{"error": "..."}` rather than a silent `removed: true`.)
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_ticket = _normalize_id(project, ticket_id)
            normalized_target = _normalize_target(project, target)
            result = provider.remove_relation(
                project, token, normalized_ticket, kind, normalized_target,
            )
            return {
                "project_id": project.id,
                "kind": kind,
                "target": normalized_target,
                **result,
            }
        return _safe(go)

    @mcp.tool()
    def list_relation_kinds() -> dict:
        """List all relation kinds the write-side tools accept.

        Global — not project-scoped, and takes no arguments. The
        universal `kinds` vocabulary is identical for every project; the
        per-provider differences live in `provider_support`. There is no
        `project_id` parameter.

        Response shape:
        ```
        {
          "kinds": [kind, ...],
          "read_only_kinds": [kind, ...],
          "provider_support": {
            "github":      [kind, ...],
            "gitlab":      [kind, ...],
            "azuredevops": [kind, ...],
          }
        }
        ```

        `kinds` is the universal write-side vocabulary — these are the
        values accepted by `add_relation` and `remove_relation`.

        `read_only_kinds` lists relation kinds that appear in
        `get_ticket` responses but cannot be passed to `add_relation`
        or `remove_relation` — they are derived automatically from body
        content or from the inverse side of a write operation (e.g.
        `mentions`, `mentioned_by`, `closed_by`, `duplicated_by`,
        `closes`).

        `provider_support` tells the agent which kinds each provider
        actually accepts before it ever calls `add_relation` — no need
        to learn provider quirks via failed calls.
        """
        return {
            "kinds": list(WRITABLE_RELATION_KINDS),
            "read_only_kinds": list(READ_ONLY_RELATION_KINDS),
            "provider_support": {
                "github": list(GitHubProvider._SUPPORTED_RELATION_KINDS),
                "gitlab": list(GitLabProvider._SUPPORTED_RELATION_KINDS),
                "azuredevops": list(_AZURE_SUPPORTED_RELATION_KINDS),
            },
        }

    @mcp.tool()
    def list_hierarchy(project_id: str, ticket_id: str) -> dict:
        """Read a ticket's parent/child (epic) hierarchy in one call.

        A one-call alternative to fetching `get_ticket` for a candidate
        and hand-filtering its `relations` list for `parent`/`child`
        entries. Makes exactly the same single provider call `get_ticket`
        makes internally (`include_relations=True`), then projects the
        returned relations — it does not resolve or fetch anything
        beyond what that one call already returns.

        `parent` is the single `parent` relation (an epic or containing
        issue), or `null` when `ticket_id` has no parent. `children` is
        the list of all `child` relations, or `[]` when there are none.
        Each entry has the same shape as an item in `get_ticket`'s
        `relations` list: `kind`, `ticket_id` (the other ticket's id),
        `title`, `url`, `state`, `is_pull_request`, `resolved`.

        `relations_truncated` mirrors `get_ticket`'s field of the same
        name — `true` when the underlying timeline had more pages than
        were fetched, meaning `children` may be incomplete.

        Read-only: no permission flag required beyond a valid token
        (public repos work token-less, same as `get_ticket`).

        Returns:

        ```
        {
          "project_id": str,
          "ticket_id": str,
          "parent": <relation dict> | null,
          "children": [<relation dict>, ...],
          "relations_truncated": bool
        }
        ```
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            normalized_id = _normalize_id(project, ticket_id)
            try:
                _ticket, _comments, relations, truncated = provider.get_ticket(
                    project, token, normalized_id,
                    include_relations=True,
                    include_custom_fields=False,
                )
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="ticket",
                    ident=normalized_id,
                )
            parent = None
            children = []
            for rel in relations:
                if rel.kind == "parent":
                    parent = asdict(rel)
                elif rel.kind == "child":
                    children.append(asdict(rel))
            return {
                "project_id": project.id,
                "ticket_id": normalized_id,
                "parent": parent,
                "children": children,
                "relations_truncated": bool(truncated),
            }
        return _safe(go)
