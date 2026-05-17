"""list_projects / find_projects — discovery tools for the agent.

These tool responses intentionally do NOT reveal where projects are
configured or how permissions are stored. The agent only needs to know
which projects exist and what it may do with them; the location of the
underlying configuration is a privileged detail the user manages.

**Diagnostic fields (ticket #15)**

`list_projects` and `find_projects` carry a top-level `runtime` block
plus a per-project `token_error` field so agents can diagnose setup
problems without a separate tool:

    "runtime": {
      "os":                    "windows" | "linux",
      "config_files_searched": [...] | null,   # only when debug-mode
      "config_file_loaded":    "..." | null,    # only when debug-mode
    }

The `config_files_searched` / `config_file_loaded` paths are
**redacted by default** — without them an agent could read the
loaded YAML to discover which env vars and flags drive its own
permissions, which is exactly the privilege boundary the rest of the
plugin defends. Set `PROJECT_ISSUES_DEBUG=1` (or `true`/`yes`/`on`) at
server start to expose the absolute paths.

`token_error` is one of:
- `None` — token is set and non-empty.
- `"env_var_unset"` — `token_env` is set but the env var is not.
- `"env_var_empty"` — env var is set but value is empty.
- `"no_token_env"` — project has no `token_env` configured (e.g. an
  auto-discovered project that needs `GITHUB_TOKEN` and didn't get
  one). This is also surfaced when the var name is empty/None.

Field is always emitted (it's a status enum, not a path leak).

**Token-derived permissions for auto-discovered projects (ticket #32)**

When a project has `source == "git-remote"` (no explicit YAML entry)
AND a usable token is present, the provider is asked to probe the
token's effective capabilities against the repo and the result is used
in place of the hardcoded-False default. The probe is cached for 5
minutes per `(provider, path, token-fingerprint)` so a single
`list_projects` burst doesn't hammer the API.

Two extra per-project fields document the source:

- `permissions_source`:
  - `"config"`     — permissions came from the YAML entry.
  - `"token-probe"` — permissions came from a successful probe.
  - `"default"`    — no probe was possible (no token, or probe failed)
                      and the all-False default applies.
- `permissions_probe_error`: stable `TokenCapabilities.reason` string
  when a probe failed (`"bad_credentials"`, `"repo_invisible_to_token"`,
  `"network_error"`, `"permissions_field_missing"`, ...), else `None`.
"""
from __future__ import annotations

import os
import sys
import time

from mcp.server.fastmcp import FastMCP

from project_issues_plugin.config import (
    LoadResult,
    ProjectConfig,
    load_projects,
    resolve_token,
)
from project_issues_plugin.providers.base import TokenCapabilities

# Env var that flips debug mode on. Truthy values enable the raw-path
# fields in the `runtime` block. Anything else (unset, "0", "false",
# "", ...) hides them.
_DEBUG_ENV = "PROJECT_ISSUES_DEBUG"


def _debug_enabled() -> bool:
    return os.environ.get(_DEBUG_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _os_label() -> str:
    """`"windows"` or `"linux"` based on `sys.platform`. macOS and
    other Unixes fall through to `"linux"` because the OS-default
    config-path set is the same."""
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _token_error(p: ProjectConfig) -> str | None:
    """Diagnose why a token is missing, if it is.

    Returns one of `"no_token_env"`, `"env_var_unset"`,
    `"env_var_empty"`, or `None` when a non-empty token is present.
    """
    if not p.token_env:
        return "no_token_env"
    value = os.environ.get(p.token_env)
    if value is None:
        return "env_var_unset"
    if value == "":
        return "env_var_empty"
    return None


# ----- token-capability probe cache (ticket #32) -----------------------------
#
# Same TTL pattern as `_STATUS_CACHE_TTL_SECONDS` in tools/tickets.py (no
# separate cache module, by design). Permissions on a token can change
# when a user rotates org membership or a fine-grained PAT's scopes are
# edited, so the TTL is shorter than the status-cache TTL (5 minutes vs
# 1 hour).
_PROBE_CACHE_TTL_SECONDS = 5 * 60
_probe_cache: dict[tuple[str, str | None, str], tuple[float, TokenCapabilities]] = {}


def _probe_cache_clear() -> None:
    """Test-only hook — clears the module-level probe cache."""
    _probe_cache.clear()


def _token_fingerprint(token: str) -> str:
    """Stable, short fingerprint for cache-keying tokens without
    storing them verbatim in the cache. Uses the last 8 chars (after
    rejecting empty input). This is enough to invalidate on rotation
    while keeping the in-process cache content non-secret-revealing.
    """
    return token[-8:] if len(token) >= 8 else token


def _probe_capabilities(p: ProjectConfig, token: str) -> TokenCapabilities:
    """Run (or replay from cache) the provider's token-capabilities
    probe for `p` using `token`.

    Caches by `(provider, path, token-fingerprint)` for
    `_PROBE_CACHE_TTL_SECONDS`. Provider errors are returned as
    `TokenCapabilities(reason=...)` (the provider's own contract), not
    raised, so a failed probe still produces a usable result.
    """
    # Imported lazily to avoid a circular import at module load time
    # (tools/projects.py is imported very early, tools/_providers pulls
    # in the github provider which itself imports from base).
    from project_issues_plugin.tools._providers import _PROVIDERS

    key = (p.provider, p.display_path, _token_fingerprint(token))
    now = time.monotonic()
    cached = _probe_cache.get(key)
    if cached is not None and (now - cached[0]) < _PROBE_CACHE_TTL_SECONDS:
        return cached[1]
    impl = _PROVIDERS.get(p.provider)
    if impl is None or not hasattr(impl, "probe_token_capabilities"):
        # No provider implementation -> treat as a "no probe possible"
        # outcome so the caller falls back to `permissions_source="default"`.
        result = TokenCapabilities(reason="provider_unsupported")
    else:
        try:
            result = impl.probe_token_capabilities(p, token)
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            result = TokenCapabilities(reason=f"probe_raised:{type(exc).__name__}")
    _probe_cache[key] = (now, result)
    return result


def _project_to_dict(p: ProjectConfig) -> dict:
    # Default: use the YAML-configured permissions verbatim.
    issues_create = p.permissions.issues.create
    issues_modify = p.permissions.issues.modify
    pulls_create = p.permissions.pulls.create
    pulls_modify = p.permissions.pulls.modify
    pulls_merge = p.permissions.pulls.merge
    permissions_source: str
    permissions_probe_error: str | None = None

    if p.source == "config":
        # YAML-defined projects are authoritative — never override.
        permissions_source = "config"
    else:
        # Auto-discovered (git-remote) project. If a token is available,
        # ask the provider what the token can actually do; otherwise
        # keep the all-False default (the existing safe behavior).
        token = resolve_token(p)
        if token:
            caps = _probe_capabilities(p, token)
            if caps.reason is None:
                issues_create = caps.issues_create
                issues_modify = caps.issues_modify
                pulls_create = caps.pulls_create
                pulls_modify = caps.pulls_modify
                pulls_merge = caps.pulls_merge
                permissions_source = "token-probe"
            else:
                permissions_source = "default"
                permissions_probe_error = caps.reason
        else:
            permissions_source = "default"

    return {
        "id": p.id,
        "description": p.description,
        "provider": p.provider,
        "path": p.display_path,
        "base_url": p.base_url,
        "web_url": p.web_url,
        "source": p.source,
        "permissions": {
            "read": True,
            "issues": {
                "create": issues_create,
                "modify": issues_modify,
            },
            "pulls": {
                "create": pulls_create,
                "modify": pulls_modify,
                "merge": pulls_merge,
            },
        },
        "permissions_source": permissions_source,
        "permissions_probe_error": permissions_probe_error,
        "token_env": p.token_env,
        "token_available": resolve_token(p) is not None,
        "token_error": _token_error(p),
    }


def _runtime_block(result: LoadResult) -> dict:
    """Top-level diagnostic block.

    `config_files_searched` and `config_file_loaded` are absent
    (or `None`) outside debug mode — see the module docstring for the
    rationale.
    """
    block: dict = {"os": _os_label()}
    if _debug_enabled():
        block["config_files_searched"] = list(result.searched_paths)
        block["config_file_loaded"] = result.config_file
    else:
        block["config_files_searched"] = None
        block["config_file_loaded"] = None
    return block


def _score(query: str, project: ProjectConfig) -> int:
    """Substring-based scoring against id, path, description. 0 = no match."""
    q = query.lower().strip()
    if not q:
        return 0
    id_lc = project.id.lower()
    desc_lc = project.description.lower()
    path_lc = project.display_path.lower()
    score = 0
    if q == id_lc:
        score = max(score, 1000)
    if id_lc.startswith(q):
        score = max(score, 500)
    if q in id_lc:
        score = max(score, 300)
    if q in path_lc:
        score = max(score, 200)
    if q in desc_lc:
        score = max(score, 100)
    for token in q.split():
        if token and token in id_lc:
            score += 30
        if token and token in desc_lc:
            score += 15
        if token and token in path_lc:
            score += 10
    return score


_STATE_HINTS = {
    "ok": None,
    "config_empty": (
        "No projects are currently defined. Ask the user to add at least "
        "one before continuing."
    ),
    "no_config": (
        "Project management is not set up for this directory. Ask the "
        "user to configure at least one project."
    ),
    "config_error": (
        "Project configuration failed to load. Ask the user to inspect "
        "their setup — the server's stderr log contains the technical "
        "details (visible to the user, not to you)."
    ),
}


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_projects() -> dict:
        """List projects available to this server.

        Each entry has an `id`, `provider`, `path`, `web_url`, and
        `permissions`. A project with `source="git-remote"` was inferred
        from the local git repository and is read-only.

        Inspect `permissions.issues` and `permissions.pulls` separately.
        Read is always implicit (token-gated):

            "permissions": {
              "read":   true,
              "issues": {"create": ..., "modify": ...},
              "pulls":  {"create": ..., "modify": ..., "merge": ...}
            }

        Inspect `state` before reporting to the user:
          - "ok":           use `projects` as-is.
          - "config_empty": no projects are defined yet — tell the user
                            to add one. Do NOT claim none exist when the
                            user expects some.
          - "no_config":    project management is not set up here.
          - "config_error": configuration failed to load — ask the user
                            to check it (details are in the server log,
                            not in this response).

        Permissions are authoritative — if a namespace flag is false,
        the corresponding operation is not allowed.

        For auto-discovered projects (`source == "git-remote"`) with a
        usable token, the permissions reflect what GitHub says the
        token may actually do (see `permissions_source` /
        `permissions_probe_error`).

        Diagnostic fields:

          - `runtime.os` — `"windows"` or `"linux"`.
          - `runtime.config_files_searched` — list of candidate paths
            the resolver inspected, or `null` outside debug mode.
          - `runtime.config_file_loaded` — winning path, or `null`
            outside debug mode.
          - Per project, `token_error`:
              `null` (token present), `"env_var_unset"`,
              `"env_var_empty"`, or `"no_token_env"`.
          - Per project, `permissions_source`:
              `"config"` (from YAML), `"token-probe"` (derived from a
              live API probe of the token), or `"default"` (no probe
              was possible — the all-False default applies).
          - Per project, `permissions_probe_error`: stable failure
              identifier (e.g. `"bad_credentials"`,
              `"repo_invisible_to_token"`, `"network_error"`) when a
              probe was attempted and failed, else `null`.

        Raw config-paths are hidden by default to keep the agent from
        learning the location of the permissions file. Start the
        server with `PROJECT_ISSUES_DEBUG=1` to expose them.
        """
        result = load_projects()
        return {
            "projects": [_project_to_dict(p) for p in result.projects],
            "state": result.state,
            "hint": _STATE_HINTS.get(result.state),
            "runtime": _runtime_block(result),
        }

    @mcp.tool()
    def find_projects(query: str, limit: int = 10) -> dict:
        """Fuzzy-search the available projects by id / description / path.

        Use whenever the user names a project naturally ("the mobile
        app"). Returns up to `limit` matches sorted by relevance.

        If `matches` is empty, INSPECT `state` first — do not say "the
        project doesn't exist" when the cause is missing or broken
        configuration:
          - "ok":           no project matched the query; suggest
                            `list_projects` to the user.
          - "config_empty" / "no_config": no projects are defined at all.
          - "config_error": configuration is broken — surface that.

        Same diagnostic fields as `list_projects` (`runtime.os`,
        debug-gated `runtime.config_files_searched` /
        `config_file_loaded`, per-match `token_error`).
        """
        result = load_projects()
        scored: list[tuple[int, ProjectConfig]] = []
        for p in result.projects:
            s = _score(query, p)
            if s > 0:
                scored.append((s, p))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        cap = max(1, limit)
        results = [{**_project_to_dict(p), "score": s} for s, p in scored[:cap]]
        return {
            "query": query,
            "matches": results,
            "state": result.state,
            "hint": _STATE_HINTS.get(result.state),
            "runtime": _runtime_block(result),
        }
