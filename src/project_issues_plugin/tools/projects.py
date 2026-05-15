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
"""
from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP

from project_issues_plugin.config import (
    LoadResult,
    ProjectConfig,
    load_projects,
    resolve_token,
)

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


def _project_to_dict(p: ProjectConfig) -> dict:
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
                "create": p.permissions.issues.create,
                "modify": p.permissions.issues.modify,
            },
            "pulls": {
                "create": p.permissions.pulls.create,
                "modify": p.permissions.pulls.modify,
                "merge": p.permissions.pulls.merge,
            },
        },
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

        Diagnostic fields:

          - `runtime.os` — `"windows"` or `"linux"`.
          - `runtime.config_files_searched` — list of candidate paths
            the resolver inspected, or `null` outside debug mode.
          - `runtime.config_file_loaded` — winning path, or `null`
            outside debug mode.
          - Per project, `token_error`:
              `null` (token present), `"env_var_unset"`,
              `"env_var_empty"`, or `"no_token_env"`.

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
