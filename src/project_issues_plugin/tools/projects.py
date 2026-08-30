"""list_projects / search_projects — discovery tools for the agent.

These tool responses intentionally do NOT reveal where projects are
configured or how permissions are stored. The agent only needs to know
which projects exist and what it may do with them; the location of the
underlying configuration is a privileged detail the user manages.

**Diagnostic fields (ticket #15)**

`list_projects` and `search_projects` carry a top-level `runtime` block
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
  - `"config"`          — permissions came from the YAML entry.
  - `"token-discovery"` — permissions came verbatim from the
                           token-discovery result (pre-populated by
                           the lib; no additional probe is issued).
  - `"token-probe"`     — permissions came from a successful live
                           API probe.
  - `"default"`         — no probe was possible (no token, or probe
                           failed) and the all-False default applies.

**Verification (ticket #303)**

Every project's `permissions` block also carries `verified` (bool) and
`reason` (`str | None`) — the lib's own `Permissions.verified` /
`Permissions.reason` computed fields, passed through (for `"config"`
and `"token-discovery"` sources) or derived from this module's own
git-remote probe (for `"token-probe"`/`"default"`):

- `verified: true` **iff** the emitted `issues`/`pulls` flags came from
  a clean live probe of the token — either the lib's own token-
  discovery probe, or this module's git-remote probe with a clean
  (`reason is None`) result. `reason` is then `None` too.
- `verified: false` with `reason: "not_probed"` — the default, never-
  probed state (e.g. a `"config"` project, or a `"git-remote"` project
  with no usable token).
- `verified: false` with `reason: "<probe failure code>"` (e.g.
  `"bad_credentials"`, `"repo_invisible_to_token"`, `"network_error"`,
  `"work_items_unavailable"`) — a probe was attempted and did not come
  back fully clean; the `issues`/`pulls` flags are left at the all-False
  default, never partially widened.

`token_env` names the configured/derived environment variable; it says
nothing about whether that variable is set. `token_available` /
`token_error` only report whether a token **string** is present in the
environment — they say nothing about whether the provider actually
accepted it. `permissions.verified` / `permissions.reason` are the
fields that answer that question; treat `token_available: true` as "a
token was found to try", not as proof of write access.

Note: `state="ok"` can occur without a config file being present when
token-discovery returns projects from a provider token.  When
`result.discovery_truncated` is `True`, the response `hint` field
explains that the token-discovery result list was capped.
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Literal

# Compiled separator pattern reused by `_score_match()` / `_score()` for
# sub-token splitting. Splits on any run of non-alphanumeric characters
# (hyphens, underscores, dots, slashes, etc.) so that e.g. "proj-iss" and
# "agent-project-issues" share common sub-tokens.
_TOKEN_SEP = re.compile(r"[^a-z0-9]+")

from mcp.server.fastmcp import FastMCP

from lib_python_projects import (
    ProjectConfig,
    ProjectsLoadResult,
    load_projects,
    resolve_token,
)
from lib_python_projects.providers.base import TokenCapabilities

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
    # `perm_verified`/`perm_reason` surface the lib's `Permissions.verified`/
    # `.reason` computed fields (ticket #303). Invariant: `perm_verified is
    # True` iff the emitted issues/pulls flags above came from a clean live
    # probe (either the lib's own token-discovery probe, or this module's
    # git-remote probe below with `caps.reason is None`).
    perm_verified: bool
    perm_reason: str | None

    if p.source == "config":
        # YAML-defined projects are authoritative — never override.
        permissions_source = "config"
        perm_verified = p.permissions.verified
        perm_reason = p.permissions.reason
    elif p.source == "token-discovery":
        # Token-discovery projects have pre-populated permissions from the
        # lib; use them verbatim — no additional probe call is needed or
        # wanted.
        issues_create = p.permissions.issues.create
        issues_modify = p.permissions.issues.modify
        pulls_create = p.permissions.pulls.create
        pulls_modify = p.permissions.pulls.modify
        pulls_merge = p.permissions.pulls.merge
        permissions_source = "token-discovery"
        perm_verified = p.permissions.verified
        perm_reason = p.permissions.reason
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
                perm_verified = True
                perm_reason = None
            else:
                permissions_source = "default"
                perm_verified = False
                perm_reason = caps.reason
        else:
            permissions_source = "default"
            perm_verified = False
            perm_reason = p.permissions.reason

    return {
        "id": p.id,
        "description": p.description,
        "provider": p.provider,
        "path": p.display_path,
        "base_url": p.base_url,
        "web_url": p.web_url,
        "source": p.source,
        "local_path": p.local_path,
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
            "board": {
                "manage": p.permissions.board.manage,
            },
            "verified": perm_verified,
            "reason": perm_reason,
        },
        "permissions_source": permissions_source,
        "token_env": p.token_env,
        "token_available": resolve_token(p) is not None,
        "token_error": _token_error(p),
    }


def _project_to_light(p: ProjectConfig) -> dict:
    """Return the minimal project representation for ``fields="light"``.

    Contains only ``id`` and ``provider`` — just enough for the agent to
    identify a project and pass it to other tools.  The ``runtime`` block
    and all permission / token fields are omitted.
    """
    return {"id": p.id, "provider": p.provider}


def _runtime_block(result: ProjectsLoadResult) -> dict:
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


# Ticket #258: structured confidence label alongside the raw numeric score.
# Derived from the scoring *branch* that set the base score, never from the
# numeric total, so it doesn't reproduce the `~100`-`~300` gray-zone
# ambiguity of the plain score.
MatchConfidence = Literal["exact", "id", "path", "description", "weak"]


def _score_match(
    query: str, project: ProjectConfig,
) -> tuple[int, MatchConfidence | None]:
    """Substring-based scoring against id, path, description, plus a
    structured `MatchConfidence` label derived from whichever base-score
    branch fired (never from the numeric total).

    Returns `(score, match_confidence)`. `match_confidence is None` iff
    `score == 0` (empty/whitespace query, or nothing matched at all).
    """
    q = query.lower().strip()
    if not q:
        return 0, None
    id_lc = project.id.lower()
    desc_lc = project.description.lower()
    path_lc = project.display_path.lower()
    score = 0
    confidence: MatchConfidence | None = None
    # Highest-precedence base branch first; bases are strictly ordered
    # (1000 > 500 > 300 > 200 > 100) so `score = max(score, ...)` and label
    # assignment never disagree on which branch "won".
    if q == id_lc:
        score = max(score, 1000)
        confidence = "exact"
    if id_lc.startswith(q):
        score = max(score, 500)
        if confidence is None:
            confidence = "id"
    if q in id_lc:
        score = max(score, 300)
        if confidence is None:
            confidence = "id"
    if q in path_lc:
        score = max(score, 200)
        if confidence is None:
            confidence = "path"
    if q in desc_lc:
        score = max(score, 100)
        if confidence is None:
            confidence = "description"
    for token in q.split():
        if len(token) < 3:
            continue
        if token in id_lc:
            score += 30
        if token in desc_lc:
            score += 15
        if token in path_lc:
            score += 10
    # F19: sub-token matching for hyphenated / compound queries and ids.
    # Split both query and candidate fields on non-alphanumeric separators,
    # keeping only parts of length >= 3 to avoid noise from short tokens.
    q_parts = [p for p in _TOKEN_SEP.split(q) if len(p) >= 3]
    if q_parts:
        id_parts = [p for p in _TOKEN_SEP.split(id_lc) if len(p) >= 3]
        path_parts = [p for p in _TOKEN_SEP.split(path_lc) if len(p) >= 3]
        desc_parts = [p for p in _TOKEN_SEP.split(desc_lc) if len(p) >= 3]
        for qp in q_parts:
            for cp in id_parts:
                if qp in cp or cp in qp:
                    score += 50
            for cp in path_parts:
                if qp in cp or cp in qp:
                    score += 20
            for cp in desc_parts:
                if qp in cp or cp in qp:
                    score += 10
    if score > 0 and confidence is None:
        # Score built entirely from word-token / sub-token bonuses — no
        # base branch fired. Weak: incidental, not a real match.
        confidence = "weak"
    return score, confidence


def _score(query: str, project: ProjectConfig) -> int:
    """Substring-based scoring against id, path, description. 0 = no match.

    Thin wrapper over `_score_match()` for callers that only need the raw
    numeric score.
    """
    return _score_match(query, project)[0]


_STATE_HINTS = {
    "ok": None,
    "config_empty": (
        "No projects are currently defined. Ask the user to add at least "
        "one before continuing."
    ),
    "no_config": (
        "No config file found — if a provider token is set, accessible "
        "repositories may appear automatically via token discovery; "
        "otherwise configure at least one project."
    ),
    "config_error": (
        "Project configuration failed to load. Ask the user to inspect "
        "their setup — the server's stderr log contains the technical "
        "details (visible to the user, not to you)."
    ),
}


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_projects(
        fields: Literal["full", "light"] = "full",
    ) -> dict:
        """List projects available to this server.

        All configured projects are always returned — this tool is not
        paginated and the response carries no `total` or `truncated`
        fields. When you need a relevance-ranked subset or want to apply
        a `limit`, use `search_projects` instead.

        Each entry has an `id`, `provider`, `path`, `web_url`, and
        `permissions`. A project with `source="git-remote"` was inferred
        from the local git repository and is read-only.

        Inspect `permissions.issues`, `permissions.pulls`, and
        `permissions.board` separately. Read is always implicit
        (token-gated):

            "permissions": {
              "read":     true,
              "issues":   {"create": ..., "modify": ...},
              "pulls":    {"create": ..., "modify": ..., "merge": ...},
              "board":    {"manage": ...},
              "verified": true | false,
              "reason":   "<code>" | null
            }

        `permissions.verified` / `permissions.reason` are the trustworthy
        signal for whether the flags above reflect a real, live check of
        the token — `verified: true` (`reason: null`) only when a clean
        probe confirmed them; `verified: false` otherwise, with `reason`
        naming why: `"not_probed"` (the default — never probed, e.g. a
        config-sourced project or a git-remote project with no usable
        token) or a probe-failure code (e.g. `"bad_credentials"`,
        `"repo_invisible_to_token"`). Do NOT use `token_available` /
        `token_error` for this — those only report whether a token
        string is present in the environment, unverified: they say
        nothing about whether the provider actually accepted it.

        `permissions.board.manage` gates `ensure_board_column` — check it
        before calling that tool to avoid learning the restriction via a
        failed write. It is always sourced from config verbatim (never
        derived by the token-probe path, which has no board concept, so
        auto-discovered projects always report `board.manage: false`).

        Inspect `state` before reporting to the user:
          - "ok":           use `projects` as-is.  Note: `state="ok"`
                            no longer implies a config file was found —
                            token-discovery may return projects without
                            one.
          - "config_empty": no projects are defined yet — tell the user
                            to add one. Do NOT claim none exist when the
                            user expects some.
          - "no_config":    no config file found and no token-discovery
                            results; project management is not set up.
          - "config_error": configuration failed to load — ask the user
                            to check it (details are in the server log,
                            not in this response).

        Permissions are authoritative — if a namespace flag is false,
        the corresponding operation is not allowed.

        For auto-discovered projects (`source == "git-remote"`) with a
        usable token, the permissions reflect what GitHub says the
        token may actually do (see `permissions_source` and
        `permissions.verified` / `permissions.reason`).

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
              `"config"` (from YAML), `"token-discovery"` (permissions
              pre-populated by the lib's token-discovery pass — no
              additional probe is issued), `"token-probe"` (derived
              from a live API probe of the token), or `"default"` (no
              probe was possible — the all-False default applies).
          - Per project, `permissions.verified` / `permissions.reason`:
              `verified: true` (`reason: null`) only after a clean live
              probe; otherwise `verified: false` with `reason` set to
              `"not_probed"` (never probed) or a stable failure
              identifier (e.g. `"bad_credentials"`,
              `"repo_invisible_to_token"`, `"network_error"`).

        When the token-discovery result list was capped, the top-level
        `hint` field explains the truncation and notes that tuning the
        cap requires a future lib-side limit parameter.

        Raw config-paths are hidden by default to keep the agent from
        learning the location of the permissions file. Start the
        server with `PROJECT_ISSUES_DEBUG=1` to expose them.

        Token-cheap knob:
          - `fields="light"`: return only ``{id, provider}`` per project
            (dropping `description` / `path` / `web_url` / `permissions`)
            and omit the ``runtime`` block. Useful for quickly obtaining
            a list of project IDs to pass to other tools. Prefer this
            over `search_projects(query="", fields="light")` when you just
            want every project's id cheaply: `list_projects` returns the
            full set in one shot with no relevance scoring and no
            pagination. Use `search_projects` only when you actually want
            ranking or a bounded `limit`.
          - `fields="full"` (default): full behaviour as described above.
        """
        result = load_projects(
            config_filename="projects.yml",
            config_filename_alt="projects.yaml",
        )
        _discovery_truncated_hint = (
            "Token-discovery returned a partial project list — the result "
            "was capped by the lib. To raise the cap, a future lib-side "
            "limit parameter will be required."
        ) if result.discovery_truncated else None
        if fields == "light":
            return {
                "projects": [_project_to_light(p) for p in result.projects],
                "state": result.state,
                "hint": _discovery_truncated_hint or _STATE_HINTS.get(result.state),
            }
        return {
            "projects": [_project_to_dict(p) for p in result.projects],
            "state": result.state,
            "hint": _discovery_truncated_hint or _STATE_HINTS.get(result.state),
            "runtime": _runtime_block(result),
        }

    @mcp.tool()
    def search_projects(
        query: str,
        limit: int = 10,
        fields: Literal["full", "light"] = "full",
    ) -> dict:
        """Fuzzy-search the available projects by id / description / path.

        Use whenever the user names a project naturally ("the mobile
        app"). Returns up to `limit` matches sorted by relevance. `limit`
        must be `>= 1`; `limit < 1` returns
        `{"error": "limit must be a positive integer, got <limit>"}`
        without loading or scoring any projects.

        **Query behavior:**
          - Empty or whitespace-only query returns **all** projects
            (alphabetical by id), each with `score: 0` and
            `match_confidence: null`. Use this to enumerate without a
            separate `list_projects` call — though for a plain unranked
            dump of every project, `list_projects` (non-paginated, no
            `limit`) is the simpler choice. Reach for `search_projects`
            when you want relevance ranking or a bounded `limit` over a
            large set.
          - Non-empty query → fuzzy match by id / description / path,
            sorted by relevance descending.

        **Interpreting `match_confidence` (this is a FUZZY matcher — read
        before treating a match as real):** each match carries a
        `match_confidence` string alongside the raw `score`, derived from
        *which rule* matched rather than from the numeric total — that is
        what avoids the gray-zone ambiguity a bare score threshold has.
        One of:
          - `"exact"` — the query equals the id exactly.
          - `"id"` — the query is a prefix of, or a substring of, the id.
          - `"path"` — the query is a substring of the path (no id hit).
          - `"description"` — the query is a substring of the description
            only (no id / path hit).
          - `"weak"` — no id / path / description substring matched at
            all; the score is built entirely from incidental word-token or
            sub-token overlap (e.g. the query and an unrelated project
            sharing a common sub-token like "project"). Treat with
            suspicion: probably not a real match.
          - `null` — `score` is `0` (nothing matched; only occurs for the
            empty-query enumeration case, since non-matches are filtered
            out of `matches`).
        `match_confidence` is never upgraded/downgraded by the additive
        bonuses: e.g. a description-only match whose accumulated score
        happens to exceed 300 is still reported as `"description"`, not
        `"id"`.

        For a plain "does project X exist?" check, prefer an exact-id
        comparison (or enumerate via `list_projects`) instead of relying
        on `matches` being empty or non-empty — even `"weak"` matches are
        included in `matches` (see the `score` footnote below). If you do
        use `matches` for an existence check, filter out results where
        `match_confidence == "weak"` first — they are not reliable
        evidence the project exists.

        Case sensitivity: the `id` matched/returned here is
        **case-insensitive** (e.g. querying `"ACME"` matches a project
        whose id is `"acme"`). This is the opposite of `project_id` on
        every other tool (`list_releases`, `get_ticket`, and anything
        else resolved via `_resolve`), which is exact and
        **case-sensitive**. Always pass the `id` value from a
        `search_projects` match on to other tools verbatim, exactly as
        reported — do not re-case it.

        *Footnote — raw `score` thresholds (superseded by
        `match_confidence` above, kept for reference):* higher is more
        confident: `>= 300` means the query is a substring of the id;
        `>= 200` a substring of the path; a score `< 100` is usually an
        incidental description / sub-token hit rather than a real match.
        The whole `~100`–`~300` band (below the `>= 300` id-substring
        floor) is a gray zone: a score in that range is only a path
        (`>= 200`) or description (`>= 100`) substring hit, or an
        accumulated sub-token/word-token score built from several partial
        hits — it is NOT reliably a real match either way. So "no real
        match" does NOT reliably show up as an empty `matches` list, and a
        non-empty `matches` list in this band does NOT reliably mean a
        real match. Use `match_confidence` instead of re-deriving this
        banding yourself.

        **Pagination fields (always present):**
          - `total: int` — total number of candidates before the `limit`
            cap is applied (all projects for an empty query; all projects
            that scored > 0 for a non-empty query).
          - `truncated: bool` — `true` when `total > limit`, meaning
            some matches were omitted. Increase `limit` or refine the
            query to see more.

        If `matches` is empty, INSPECT `state` first — do not say "the
        project doesn't exist" when the cause is missing or broken
        configuration:
          - "ok":           no project matched the query; suggest
                            `list_projects` to the user.
          - "config_empty" / "no_config": no projects are defined at all.
          - "config_error": configuration is broken — surface that.

        Same diagnostic fields as `list_projects` (`runtime.os`,
        debug-gated `runtime.config_files_searched` /
        `config_file_loaded`, per-match `token_error`,
        per-match `permissions_source` including `"token-discovery"`,
        and per-match `permissions.verified` / `permissions.reason`),
        including the `permissions.board.manage` field described there.

        When the token-discovery result list was capped, the top-level
        `hint` field explains the truncation and notes that tuning the
        cap requires a future lib-side limit parameter.

        Token-cheap knob:
          - `fields="light"`: return only
            ``{id, provider, score, match_confidence}`` per match and omit
            the ``runtime`` block. Useful when you only need project IDs.
            Prefer `list_projects(fields="light")` when you want every
            project cheaply without ranking.
          - `fields="full"` (default): full behaviour as described above.
        """
        if limit < 1:
            return {"error": f"limit must be a positive integer, got {limit}"}
        result = load_projects(
            config_filename="projects.yml",
            config_filename_alt="projects.yaml",
        )
        _discovery_truncated_hint = (
            "Token-discovery returned a partial project list — the result "
            "was capped by the lib. To raise the cap, a future lib-side "
            "limit parameter will be required."
        ) if result.discovery_truncated else None
        q_trimmed = (query or "").strip()
        if fields == "light":
            if not q_trimmed:
                sorted_projects = sorted(result.projects, key=lambda p: p.id.lower())
                total = len(sorted_projects)
                results = [
                    {**_project_to_light(p), "score": 0, "match_confidence": None}
                    for p in sorted_projects[:limit]
                ]
            else:
                scored_light: list[tuple[int, MatchConfidence | None, ProjectConfig]] = []
                for p in result.projects:
                    s, conf = _score_match(query, p)
                    if s > 0:
                        scored_light.append((s, conf, p))
                scored_light.sort(key=lambda triple: triple[0], reverse=True)
                total = len(scored_light)
                results = [
                    {**_project_to_light(p), "score": s, "match_confidence": conf}
                    for s, conf, p in scored_light[:limit]
                ]
            truncated = total > limit
            hint = _discovery_truncated_hint or _STATE_HINTS.get(result.state)
            if not _discovery_truncated_hint:
                if result.state == "ok" and not results and q_trimmed:
                    hint = (
                        "No projects matched the query. "
                        "Use list_projects to see all available projects."
                    )
                if result.state == "ok" and truncated:
                    hint = (
                        "Results were truncated — increase `limit` or use "
                        "`list_projects` to see all projects."
                    )
            return {
                "query": query,
                "matches": results,
                "total": total,
                "truncated": truncated,
                "state": result.state,
                "hint": hint,
            }
        if not q_trimmed:
            sorted_projects = sorted(result.projects, key=lambda p: p.id.lower())
            total = len(sorted_projects)
            results = [
                {**_project_to_dict(p), "score": 0, "match_confidence": None}
                for p in sorted_projects[:limit]
            ]
        else:
            scored: list[tuple[int, MatchConfidence | None, ProjectConfig]] = []
            for p in result.projects:
                s, conf = _score_match(query, p)
                if s > 0:
                    scored.append((s, conf, p))
            scored.sort(key=lambda triple: triple[0], reverse=True)
            total = len(scored)
            results = [
                {**_project_to_dict(p), "score": s, "match_confidence": conf}
                for s, conf, p in scored[:limit]
            ]
        truncated = total > limit
        # When the config loaded fine but nothing matched the query,
        # the global `_STATE_HINTS["ok"]` is None (no hint is right for
        # `list_projects` in the ok case). Override locally so the agent
        # gets a useful nudge rather than `hint: null` (ticket #63 item 5).
        hint = _discovery_truncated_hint or _STATE_HINTS.get(result.state)
        if not _discovery_truncated_hint:
            if result.state == "ok" and not results and q_trimmed:
                hint = (
                    "No projects matched the query. "
                    "Use list_projects to see all available projects."
                )
            if result.state == "ok" and truncated:
                hint = (
                    "Results were truncated — increase `limit` or use "
                    "`list_projects` to see all projects."
                )
        return {
            "query": query,
            "matches": results,
            "total": total,
            "truncated": truncated,
            "state": result.state,
            "hint": hint,
            "runtime": _runtime_block(result),
        }
