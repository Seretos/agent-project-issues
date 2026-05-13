"""Shared helpers for tool modules.

Lifts the previously-duplicated `_PROVIDERS` / `_resolve` / `_provider_for`
/ `_safe` / `_require_token` / permission-gate scaffolding out of
`tools/tickets.py`, `tools/comments.py`, and `tools/bulk.py` so every
tool module routes through the same code paths.

The permission-gate helpers are split per namespace (issues / pulls) to
match the nested `Permissions` model:

  - `_require_issues_create`  / `_require_issues_modify`
  - `_require_pulls_create`   / `_require_pulls_modify`  / `_require_pulls_merge`
"""
from __future__ import annotations

from project_issues_plugin import config as _cfg_mod
from project_issues_plugin.config import ProjectConfig, resolve_token
from project_issues_plugin.providers.github import GitHubError, GitHubProvider


_PROVIDERS = {
    "github": GitHubProvider(),
}


def _resolve(project_id: str) -> ProjectConfig:
    # Access `load_projects` via the config module (not a top-level import)
    # so existing tests that monkey-patch `config.load_projects` keep working
    # after we moved the helper here from the individual tool modules.
    result = _cfg_mod.load_projects()
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


# --------- error translation -------------------------------------------------


def _safe(call):
    """Execute `call()` and translate known errors to a dict with `error`."""
    try:
        return call()
    except (LookupError, PermissionError, NotImplementedError) as exc:
        return {"error": str(exc)}
    except GitHubError as exc:
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
    "_safe",
]
