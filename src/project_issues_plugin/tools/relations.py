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

from mcp.server.fastmcp import FastMCP

from lib_python_projects.providers.azuredevops import (
    SUPPORTED_RELATION_KINDS as _AZURE_SUPPORTED_RELATION_KINDS,
)
from lib_python_projects.providers.base import WRITABLE_RELATION_KINDS
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider
from project_issues_plugin.tools._providers import (
    _normalize_id,
    _normalize_target,
    _provider_for,
    _require_issues_modify,
    _require_token,
    _resolve,
    _safe,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def add_relation(
        project_id: str,
        ticket_id: str,
        kind: str,
        target: str,
    ) -> dict:
        """Create a typed relation from `ticket_id` to `target`.

        `kind` is one of: `parent`, `child`, `blocks`, `blocked_by`,
        `duplicate_of`, `relates_to`. The read-only inverse kinds
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
            "id 'owner/repo' could not be normalised — expected a bare
            number, '#N', or a full issue/PR URL"`.

        Symmetry: `add_relation(A, kind=parent, target=B)` is a logical
        alias for `add_relation(B, kind=child, target=A)`. Pick the
        form that reads most naturally for the source ticket.

        Provider-specific notes:
          - GitHub `relates_to` → unsupported (no native typed link).
          - GitLab `parent` / `child` → unsupported pending Work Items
            GraphQL bridge (follow-up).
          - `duplicate_of` triggers a body-edit and closes the source
            on both providers — the marker line `#ai-generated` /
            `#ai-modified` is preserved correctly via the shared
            marker helper.

        Requires the project's `issues.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_issues_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_ticket = _normalize_id(project, ticket_id)
            normalized_target = _normalize_target(project, target)
            relation = provider.add_relation(
                project, token, normalized_ticket, kind, normalized_target,
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
        kind: str,
        target: str,
    ) -> dict:
        """Remove a typed relation between `ticket_id` and `target`.

        Inverse of `add_relation`. `kind` must match the same
        vocabulary; the operation is best-effort: removing a relation
        that doesn't exist raises `{"error": "..."}` rather than a
        silent success so the agent can branch on the failure.

        For `duplicate_of`, removal reopens the source ticket but does
        NOT strip the `Duplicate of #N` line from the body — history
        is preserved deliberately. If the caller wants to clean up the
        body, follow up with `update_ticket(body=...)`.

        Requires the project's `issues.modify` permission.
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
                **result,
            }
        return _safe(go)

    @mcp.tool()
    def list_relation_kinds() -> dict:
        """List all relation kinds the write-side tools accept.

        Response shape:
        ```
        {
          "kinds": [kind, ...],
          "provider_support": {
            "github":      [kind, ...],
            "gitlab":      [kind, ...],
            "azuredevops": [kind, ...],
          }
        }
        ```

        `kinds` is the universal write-side vocabulary. `provider_support`
        (ticket #48 finding 5) tells the agent which kinds each provider
        actually accepts before it ever calls `add_relation` — no need
        to learn provider quirks via failed calls.
        """
        return {
            "kinds": list(WRITABLE_RELATION_KINDS),
            "provider_support": {
                "github": list(GitHubProvider._SUPPORTED_RELATION_KINDS),
                "gitlab": list(GitLabProvider._SUPPORTED_RELATION_KINDS),
                "azuredevops": list(_AZURE_SUPPORTED_RELATION_KINDS),
            },
        }
