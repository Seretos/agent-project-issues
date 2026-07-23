"""Shared helpers for tool modules.

Lifts the previously-duplicated `_PROVIDERS` / `_resolve` / `_provider_for`
/ `_safe` / `_require_token` / permission-gate scaffolding out of
`tools/tickets.py`, `tools/comments.py`, and `tools/bulk.py` so every
tool module routes through the same code paths.

The permission-gate helpers are split per namespace (issues / pulls) to
match the nested `Permissions` model:

  - `_require_issues_create`  / `_require_issues_modify`
  - `_require_pulls_create`   / `_require_pulls_modify`  / `_require_pulls_merge`
  - `_require_board_manage`

Library layering (agent-plugin-dev#5):
  - Domain models (`ProjectConfig`, `resolve_token`, `load_projects`)
    come from `lib_python_projects`.
  - Provider implementations (`GitHubProvider`, `GitLabProvider`,
    `AzureDevOpsProvider`) and their typed error classes come from
    `lib_python_projects.providers.*`.

`load_projects` is re-bound at this module's top level so tests can
monkey-patch `project_issues_plugin.tools._providers.load_projects`
to substitute a deterministic project list without hitting the disk.
"""
from __future__ import annotations

import re

from lib_python_projects import ProjectConfig, load_projects, resolve_token
from lib_python_projects.providers.azuredevops import (
    AzureDevOpsError,
    AzureDevOpsProvider,
)
from lib_python_projects.providers.github import GitHubError, GitHubProvider
from lib_python_projects.providers.gitlab import GitLabError, GitLabProvider

from project_issues_plugin import refs as _refs


# The plugin uses `projects.yml` (renamed from the legacy
# `project-issues.yml` as part of the lib refactor). The lib's default
# is still `project-issues.yml` for backwards-compat with the migrated
# loader tests, so we pass the new name through every load_projects()
# call from the plugin.
_CONFIG_FILENAME = "projects.yml"
_CONFIG_FILENAME_ALT = "projects.yaml"


def _load_projects():
    """Thin indirection around `load_projects` so tests can monkey-patch
    `project_issues_plugin.tools._providers.load_projects` to substitute
    a fake project list.

    Reads the module-level `load_projects` so the monkey-patch is
    honoured even though the import bound a reference at startup.
    """
    import sys
    mod = sys.modules[__name__]
    return mod.load_projects(
        config_filename=_CONFIG_FILENAME,
        config_filename_alt=_CONFIG_FILENAME_ALT,
    )


_PROVIDERS = {
    "github": GitHubProvider(),
    "gitlab": GitLabProvider(),
    "azuredevops": AzureDevOpsProvider(),
}


def _resolve(project_id: str) -> ProjectConfig:
    # Access `load_projects` via this module so monkey-patches on
    # `project_issues_plugin.tools._providers.load_projects` are honoured.
    result = _load_projects()
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


# --------- issues namespace --------------------------------------------------


def _require_issues_create(project: ProjectConfig) -> None:
    if not project.permissions.issues.create:
        raise PermissionError(
            f"project '{project.id}' does not permit creating tickets. "
            "Tell the user the project is configured without issues.create permission."
        )


def _require_issues_modify(project: ProjectConfig) -> None:
    if not project.permissions.issues.modify:
        raise PermissionError(
            f"project '{project.id}' does not permit modifying tickets or "
            "adding comments. Tell the user the project is configured without "
            "issues.modify permission."
        )


# --------- pulls namespace ---------------------------------------------------


def _require_pulls_create(project: ProjectConfig) -> None:
    if not project.permissions.pulls.create:
        raise PermissionError(
            f"project '{project.id}' does not permit creating pull requests. "
            "Tell the user the project is configured without pulls.create permission."
        )


def _require_pulls_modify(project: ProjectConfig) -> None:
    if not project.permissions.pulls.modify:
        raise PermissionError(
            f"project '{project.id}' does not permit modifying pull requests or "
            "adding PR comments. Tell the user the project is configured without "
            "pulls.modify permission."
        )


def _require_pulls_merge(project: ProjectConfig) -> None:
    if not project.permissions.pulls.merge:
        raise PermissionError(
            f"project '{project.id}' does not permit merging pull requests. "
            "Tell the user the project is configured without pulls.merge permission."
        )


# --------- board namespace ----------------------------------------------------


def _require_board_manage(project: ProjectConfig) -> None:
    if not project.permissions.board.manage:
        raise PermissionError(
            f"project '{project.id}' does not permit managing board columns. "
            "Tell the user the project is configured without board.manage permission."
        )


# --------- id normalisation (ticket #46) -------------------------------------


def _normalize_id(project: ProjectConfig, raw):
    """Normalise a ticket / PR id input before dispatching to the provider.

    Delegates to `refs.normalize_id`; kept as a tool-layer wrapper so
    individual tool modules don't import `refs` directly. Raises
    `ValueError` for unparseable input — `_safe` translates that to
    `{"error": ...}`.
    """
    return _refs.normalize_id(raw, project)


def _normalize_target(project: ProjectConfig, raw):
    """Same as `_normalize_id` but tolerant of cross-repo relation targets."""
    return _refs.normalize_target(raw, project)


_LABEL_404_RE = re.compile(r"label .+ does not exist", re.IGNORECASE)


def _rewrap_404(exc, *, project_id: str, kind: str, ident: str):
    """Rewrap a provider 404 to include project + id context.

    Ticket #48 finding 6: bare `"GitHub 404: Not Found"` is hostile to
    agents because it doesn't say which lookup failed. Wrappers around
    `get_ticket` / `get_comment` / `get_pr` / `update_ticket` /
    `add_comment` / `get_pipeline_run` catch the raw 404, run it through
    this helper, and re-raise with the context the agent needs to fix
    the call (or report it to the user).

    For non-404 errors the original exception is returned unchanged so
    callers can `raise _rewrap_404(exc, ...)` unconditionally.

    Ticket #217: a 404 raised by the lib's `_assert_labels_exist` (e.g.
    `update_ticket(labels_add=["nonexistent-label"])`) is NOT a
    bad-ticket-id lookup failure — it's a distinguishable, already
    actionable "label '...' does not exist in <project>" message. This
    helper's only gate is `status == 404`, so without this check it
    would clobber that message into a misleading "ticket '<id>#<n>' not
    found", even though the ticket id itself is fine. Skip (pass
    through unchanged) so `_rewrap_label_404` gets a chance to handle it
    instead — mirrors the disjoint-gate pattern the other `_rewrap_*`
    helpers use (e.g. `_rewrap_404` vs `_rewrap_422_assignee` are
    disjoint on status; here both match 404 but are disjoint on
    message content).
    """
    if not hasattr(exc, "status") or exc.status != 404:
        return exc
    message = getattr(exc, "message", str(exc))
    if _LABEL_404_RE.search(message):
        return exc
    provider_name = type(exc).__name__.replace("Error", "")
    return type(exc)(
        404,
        f"{kind} '{project_id}#{ident}' not found ({provider_name} 404)",
    )


def _rewrap_work_item_type_404(exc, *, project_id: str, work_item_type: str | None):
    """Rewrap an Azure DevOps 404 raised for an invalid `work_item_type`.

    Ticket #182 finding 1: `list_custom_fields(work_item_type="Bug")`
    against an invalid type currently surfaces Azure's raw 404 body
    verbatim, which embeds an internal Azure project GUID the agent has
    no use for. Callers catch the raw exception and run it through this
    helper, which raises a fresh exception (same type, NEW message — the
    original `str(exc)` is deliberately not embedded, so the GUID never
    reaches the agent) naming the caller's own `project_id` /
    `work_item_type` and pointing at the recovery path: call
    `list_custom_fields` without `work_item_type`, then read the allowed
    values under `System.WorkItemType.allowed_values`.

    The `work_item_type is not None` gate means an unscoped 404 (no
    sensible recovery hint to give) falls through unchanged, same as
    `_rewrap_404` does for non-404s — callers can
    `raise _rewrap_work_item_type_404(exc, ...)` unconditionally.
    """
    if not hasattr(exc, "status") or exc.status != 404 or work_item_type is None:
        return exc
    return type(exc)(
        404,
        f"work_item_type '{work_item_type}' not found for project '{project_id}'. "
        "Call list_custom_fields without work_item_type to see the allowed values "
        "under the 'System.WorkItemType' field's 'allowed_values'.",
    )


# --------- error translation rewrap helpers (ticket #195) --------------------


_ASSIGNEE_LOGIN_RE = re.compile(r"assignees=['\"]?([^'\"\s)]+)")


def _rewrap_422_assignee(exc, *, assignees_add: list[str] | None):
    """Rewrap a GitHub 422 caused by an invalid `assignees_add` login.

    Ticket #195 finding 1: `update_ticket(assignees_add=["baduser"])`
    against a non-collaborator surfaces GitHub's raw validation body
    verbatim, e.g. `"GitHub 422: Validation Failed: Issue.assignees=
    'baduser' (invalid)"` — internal field/resource jargon the agent has
    no use for. Gated narrowly (status == 422 AND the message names the
    `assignees` field, mirroring how `_rewrap_work_item_type_404` gates
    on `work_item_type is not None`) so unrelated 422s (e.g. an invalid
    label) pass through untouched.

    When the offending login can be parsed out of GitHub's message it is
    named directly; otherwise the message falls back to naming the full
    `assignees_add` input list so the agent still knows what to check.

    The atomic rollback of the whole update (e.g. a bundled
    `labels_remove` not being applied when the assignee write fails)
    happens entirely in the provider/lib layer before this exception is
    raised — rewrapping the surfaced message here does not touch that
    behavior.

    For non-matching errors the original exception is returned unchanged
    so callers can `raise _rewrap_422_assignee(exc, ...)` unconditionally,
    matching the `_rewrap_404` / `_rewrap_work_item_type_404` contract.
    """
    if not hasattr(exc, "status") or exc.status != 422:
        return exc
    message = getattr(exc, "message", str(exc))
    if "assignees" not in message:
        return exc
    match = _ASSIGNEE_LOGIN_RE.search(message)
    if match:
        login = match.group(1)
        return type(exc)(
            422,
            f"assignee '{login}' is not a valid GitHub user/collaborator",
        )
    names = ", ".join(assignees_add) if assignees_add else "(unknown)"
    return type(exc)(
        422,
        "one or more of the requested assignees are not valid GitHub "
        f"users/collaborators: {names}",
    )


# --------- error translation rewrap helpers (ticket #217) --------------------


_LABEL_NAME_RE = re.compile(r"label ['\"]([^'\"]+)['\"] does not exist")


def _rewrap_label_404(exc, *, labels_add: list[str] | None):
    """Rewrap a GitHub 404 caused by a nonexistent `labels_add` name.

    Ticket #217: `update_ticket(labels_add=["nonexistent-label"])`
    raises a distinguishable `GitHubError(404, "label '...' does not
    exist in <project.id>")` from the lib's `_assert_labels_exist`, but
    `_rewrap_404` (gated only on `status == 404`) used to clobber it
    into a misleading "ticket '<id>#<n>' not found" — as if the ticket
    id itself were bad. `_rewrap_404` now skips messages matching
    `_LABEL_404_RE` so this helper gets a chance to run instead. Gated
    narrowly (status == 404 AND the message matches `_LABEL_404_RE`,
    mirroring how `_rewrap_422_assignee` gates on the `assignees` field)
    so an unrelated 404 (e.g. a genuine bad ticket id) passes through
    untouched and still gets `_rewrap_404`'s "not found" treatment.

    When the offending label name can be parsed out of the lib's
    message it is named directly; otherwise the message falls back to
    naming the full `labels_add` input list so the agent still knows
    what to check.

    For non-matching errors the original exception is returned
    unchanged so callers can `raise _rewrap_label_404(exc, ...)`
    unconditionally, matching the `_rewrap_404` / `_rewrap_422_assignee`
    contract.
    """
    if not hasattr(exc, "status") or exc.status != 404:
        return exc
    message = getattr(exc, "message", str(exc))
    if not _LABEL_404_RE.search(message):
        return exc
    match = _LABEL_NAME_RE.search(message)
    if match:
        label = match.group(1)
        return type(exc)(
            404,
            f"label '{label}' does not exist; create it first or check "
            "existing labels",
        )
    names = ", ".join(labels_add) if labels_add else "(unknown)"
    return type(exc)(
        404,
        "one or more of the requested labels do not exist; create them "
        f"first or check existing labels: {names}",
    )


_AZURE_BAD_BASE_RE = re.compile(r"TF401398|target branch|base branch", re.IGNORECASE)


def _rewrap_azure_bad_base(exc, *, base: str):
    """Rewrap an Azure DevOps 400 caused by an unusable `base` branch.

    Ticket #195 finding 2: `create_pr` against a non-existent base
    branch surfaces Azure's raw activation-failure body verbatim, e.g.
    `"Azure DevOps 400: TF401398: The pull request cannot be
    activated..."`. Gated narrowly (status == 400 AND the message
    signals a base/target-branch activation problem via Azure's
    TF401398 code or a "target branch"/"base branch" phrase) so
    unrelated 400s pass through untouched.

    The replacement message is built from our own `base` input already
    in scope, not by echoing Azure's raw body — so no internal Azure
    identifiers leak through.

    For non-matching errors the original exception is returned unchanged
    so callers can `raise _rewrap_azure_bad_base(exc, ...)` unconditionally,
    matching the `_rewrap_404` / `_rewrap_work_item_type_404` contract.
    """
    if not hasattr(exc, "status") or exc.status != 400:
        return exc
    message = getattr(exc, "message", str(exc))
    if not _AZURE_BAD_BASE_RE.search(message):
        return exc
    return type(exc)(
        400,
        f"base branch '{base}' cannot be used for this pull request — "
        "verify the branch exists in the repository",
    )


_GITHUB_BAD_BASE_RE = re.compile(r"PullRequest\.base", re.IGNORECASE)


def _rewrap_github_bad_base(exc, *, base: str):
    """Rewrap a GitHub 422 caused by an unusable `base` branch.

    Ticket #214: `create_pr` against a non-existent base branch surfaces
    GitHub's raw validation body verbatim, e.g. `"GitHub 422: Validation
    Failed: PullRequest.base (invalid)"`. Gated narrowly (status == 422
    AND the message names GitHub's `PullRequest.base` field, mirroring
    `_rewrap_azure_bad_base`'s #195 finding 2 counterpart) so unrelated
    422s (e.g. an invalid title/label/assignee) pass through untouched.

    The replacement message is built from our own `base` input already
    in scope, not by echoing GitHub's raw body — so no internal GitHub
    identifiers leak through.

    For non-matching errors the original exception is returned unchanged
    so callers can `raise _rewrap_github_bad_base(exc, ...)` unconditionally,
    matching the `_rewrap_404` / `_rewrap_azure_bad_base` contract.
    """
    if not hasattr(exc, "status") or exc.status != 422:
        return exc
    message = getattr(exc, "message", str(exc))
    if not _GITHUB_BAD_BASE_RE.search(message):
        return exc
    return type(exc)(
        422,
        f"base branch '{base}' cannot be used for this pull request — "
        "verify the branch exists in the repository",
    )


# --------- error translation rewrap helpers (ticket #218) --------------------


_LABEL_ALREADY_EXISTS_RE = re.compile(
    r"(?=.*Label\.name)(?=.*already_exists)", re.IGNORECASE | re.DOTALL
)


def _rewrap_label_already_exists(exc, *, new_name: str):
    """Rewrap a GitHub 422 caused by renaming a label to a name that
    already exists.

    Ticket #218 finding 3a: `update_label(name=..., new_name=<existing
    label>)` surfaces GitHub's raw validation body verbatim, e.g.
    `"GitHub 422: Validation Failed: Label.name (already_exists)"` —
    internal field/resource jargon the agent has no use for. Gated
    narrowly (status == 422 AND the message names both GitHub's
    `Label.name` field and its `already_exists` reason code, mirroring
    how `_rewrap_github_bad_base` gates on `PullRequest.base`) so an
    unrelated 422 (e.g. a bad color) passes through untouched.

    The replacement message is built from our own `new_name` input
    already in scope, not by echoing GitHub's raw body.

    For non-matching errors the original exception is returned
    unchanged so callers can `raise _rewrap_label_already_exists(exc,
    ...)` unconditionally, matching the other `_rewrap_*` helpers'
    contract.
    """
    if not hasattr(exc, "status") or exc.status != 422:
        return exc
    message = getattr(exc, "message", str(exc))
    if not _LABEL_ALREADY_EXISTS_RE.search(message):
        return exc
    return type(exc)(
        422,
        f"label '{new_name}' already exists; choose a different name or "
        "update the existing label",
    )


_AZURE_SINGLE_PARENT_RE = re.compile(r"TF201036", re.IGNORECASE)


def _rewrap_azure_single_parent(exc, *, ticket_id: str, target: str, kind: str):
    """Rewrap an Azure DevOps 400 caused by adding a second `parent`
    relation to a work item that already has one.

    Ticket #218 finding 3b: `add_relation(kind="parent", ...)` against a
    work item that already has a parent surfaces Azure's raw
    activation-failure body verbatim, e.g. `"Azure DevOps 400:
    TF201036: ... work items 66 and 59 ..."` — internal work-item ids
    that mean nothing to the agent (Azure DevOps models hierarchy as a
    single-parent tree; a second `parent` link is rejected outright,
    unlike GitHub/GitLab's simple non-exclusive link list). Gated
    narrowly (status == 400 AND Azure's `TF201036` code) so unrelated
    400s pass through untouched.

    The replacement message is built from our own `ticket_id` / `target`
    / `kind` inputs already in scope, not by echoing Azure's raw body —
    so the internal work-item ids never leak through.

    For non-matching errors the original exception is returned unchanged
    so callers can `raise _rewrap_azure_single_parent(exc, ...)`
    unconditionally, matching the `_rewrap_azure_bad_base` /
    `_rewrap_github_bad_base` contract.
    """
    if not hasattr(exc, "status") or exc.status != 400:
        return exc
    message = getattr(exc, "message", str(exc))
    if not _AZURE_SINGLE_PARENT_RE.search(message):
        return exc
    return type(exc)(
        400,
        f"cannot add '{kind}' relation from '{ticket_id}' to '{target}' — "
        "Azure DevOps work items can have only one parent; remove the "
        "existing parent relation before adding this one",
    )


_AZURE_UNKNOWN_FIELD_RE = re.compile(r"TF51535|Cannot find field", re.IGNORECASE)
_AZURE_UNKNOWN_FIELD_NAME_RE = re.compile(
    r"field\s+(Custom\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)", re.IGNORECASE
)


def _rewrap_azure_unknown_field(exc, *, custom_fields: dict | None):
    """Rewrap an Azure DevOps 400 caused by an unrecognised `custom_fields`
    key.

    Ticket #218 finding 3c: `create_ticket(custom_fields={...})` /
    `update_ticket(custom_fields={...})` with a field reference name
    Azure DevOps does not recognise surfaces the raw body verbatim, e.g.
    `"Azure DevOps 400: TF51535: Cannot find field
    Custom.ThisFieldDoesNotExist."` Gated narrowly (status == 400 AND
    Azure's `TF51535` code or its "Cannot find field" phrase, mirroring
    `_rewrap_azure_bad_base`'s #195 finding 2 counterpart) so unrelated
    400s pass through untouched — in particular it is disjoint from
    `update_ticket`'s existing 404 (bad ticket id / bad label) and 422
    (bad assignee) rewraps, so chaining it alongside them is order-safe.

    When the offending `Custom.*` field name can be parsed out of
    Azure's message it is named directly; otherwise the message falls
    back to naming the caller's own `custom_fields` keys so the agent
    still knows what to check. Either way it points at
    `list_custom_fields` to discover valid field reference names.

    For non-matching errors the original exception is returned
    unchanged so callers can `raise _rewrap_azure_unknown_field(exc,
    ...)` unconditionally, matching the other `_rewrap_*` helpers'
    contract.
    """
    if not hasattr(exc, "status") or exc.status != 400:
        return exc
    message = getattr(exc, "message", str(exc))
    if not _AZURE_UNKNOWN_FIELD_RE.search(message):
        return exc
    match = _AZURE_UNKNOWN_FIELD_NAME_RE.search(message)
    if match:
        field = match.group(1)
        return type(exc)(
            400,
            f"custom field '{field}' is not recognised by Azure DevOps for "
            "this project; call list_custom_fields to discover valid field "
            "reference names",
        )
    names = ", ".join(custom_fields.keys()) if custom_fields else "(unknown)"
    return type(exc)(
        400,
        "one or more of the requested custom_fields keys are not "
        f"recognised by Azure DevOps for this project: {names}; call "
        "list_custom_fields to discover valid field reference names",
    )


# --------- error translation -------------------------------------------------


def _safe(call):
    """Execute `call()` and translate known errors to a dict with `error`.

    `TypeError` is caught as defence-in-depth so a provider-tool surface
    mismatch (e.g. ticket #49 finding 1: GitLab pipeline methods missing
    a `status` kwarg) is reported as a structured error instead of a
    raw Python traceback bubbling up to the agent.
    """
    try:
        return call()
    except (LookupError, PermissionError, NotImplementedError, ValueError) as exc:
        return {"error": str(exc)}
    except TypeError as exc:
        return {"error": f"provider call rejected its arguments: {exc}"}
    except GitHubError as exc:
        return {"error": str(exc)}
    except GitLabError as exc:
        return {"error": str(exc)}
    except AzureDevOpsError as exc:
        return {"error": str(exc)}


__all__ = [
    "_PROVIDERS",
    "_resolve",
    "_provider_for",
    "_require_token",
    "_require_issues_create",
    "_require_issues_modify",
    "_require_pulls_create",
    "_require_pulls_modify",
    "_require_pulls_merge",
    "_require_board_manage",
    "_normalize_id",
    "_normalize_target",
    "_rewrap_404",
    "_rewrap_work_item_type_404",
    "_rewrap_422_assignee",
    "_rewrap_label_404",
    "_rewrap_azure_bad_base",
    "_rewrap_github_bad_base",
    "_rewrap_label_already_exists",
    "_rewrap_azure_single_parent",
    "_rewrap_azure_unknown_field",
    "_safe",
    "load_projects",
    "resolve_token",
]
