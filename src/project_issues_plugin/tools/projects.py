"""list_projects / find_projects — discovery tools for the agent.

These tool responses intentionally do NOT reveal where projects are
configured or how permissions are stored. The agent only needs to know
which projects exist and what it may do with them; the location of the
underlying configuration is a privileged detail the user manages.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from project_issues_plugin.config import ProjectConfig, load_projects, resolve_token


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
            "create": p.permissions.create,
            "modify": p.permissions.modify,
        },
        "token_env": p.token_env,
        "token_available": resolve_token(p) is not None,
    }


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
        `permissions` (read is always true; create/modify may be false).
        A project with `source="git-remote"` was inferred from the local
        git repository and is read-only.

        Inspect `state` before reporting to the user:
          - "ok":           use `projects` as-is.
          - "config_empty": no projects are defined yet — tell the user
                            to add one. Do NOT claim none exist when the
                            user expects some.
          - "no_config":    project management is not set up here.
          - "config_error": configuration failed to load — ask the user
                            to check it (details are in the server log,
                            not in this response).

        Permissions are authoritative from the `permissions` field — if
        `create` or `modify` is false, the operation is not allowed.
        """
        result = load_projects()
        return {
            "projects": [_project_to_dict(p) for p in result.projects],
            "state": result.state,
            "hint": _STATE_HINTS.get(result.state),
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
        }
