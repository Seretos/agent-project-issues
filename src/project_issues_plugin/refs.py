"""Provider-aware normalisation of ticket / PR id inputs (ticket #46).

The user often hands an agent an id in a form that the provider's REST
API won't accept verbatim — `"#12"`, a whitespace-padded `"  #12  "`,
or a full issue URL copied from the browser. Without this module those
ids would reach the provider as-is and surface as `404 Not Found`,
which the agent then chases through a series of retry / list calls.

`normalize_id()` accepts the loose forms and returns the bare numeric
id the provider expects. URL parsing is provider-specific (GitHub
`/issues/N` vs GitLab `/-/issues/N` vs `/-/work_items/N`), so each
provider contributes a `RefParser` and the central entry dispatches
on `project.provider`.

Cross-project references (`"owner/repo#N"`) are passed through as-is
by `normalize_target()` — `add_relation` upstream rejects them today
with `NotImplementedError`, but rewriting them here would erase the
context the upstream check needs.
"""
from __future__ import annotations

from typing import Protocol
from urllib.parse import urlsplit

from project_issues_plugin.config import ProjectConfig


class _RefParser(Protocol):
    def parse_url(self, url: str, project: ProjectConfig) -> str | None:
        """Return the bare ticket/PR id encoded in `url`, or None if the URL
        does not match a known issue/PR shape for this provider.

        Raises `ValueError` if the URL is parseable but belongs to a
        different repo than `project` (cross-project lookup guard).
        """
        ...


class GitHubRefParser:
    """Parses `https://github.com/owner/repo/{issues,pull}/N` URLs."""

    def parse_url(self, url: str, project: ProjectConfig) -> str | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or "github" not in parts.netloc:
            return None
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) < 4:
            return None
        owner, repo, kind, ident = segs[0], segs[1], segs[2], segs[3]
        if kind not in ("issues", "pull", "pulls"):
            return None
        if not ident.isdigit():
            return None
        if project.path and f"{owner}/{repo}".lower() != project.path.lower():
            raise ValueError(
                f"id URL points to '{owner}/{repo}', but project_id "
                f"'{project.id}' is configured for '{project.path}'."
            )
        return ident


class GitLabRefParser:
    """Parses GitLab issue / work-item / merge-request URLs.

    GitLab exposes three URL families for issues:
      - `/-/issues/N`        (stable, REST + legacy UI)
      - `/-/work_items/N`    (Work Items beta — same numeric id)
      - `/-/merge_requests/N` (MRs — counterpart to GitHub PRs)
    """

    def parse_url(self, url: str, project: ProjectConfig) -> str | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None
        path = parts.path
        # GitLab paths look like /<group>/<...subgroups>/<project>/-/<kind>/<id>
        if "/-/" not in path:
            return None
        proj_path, _, tail = path.lstrip("/").partition("/-/")
        tail_segs = [s for s in tail.split("/") if s]
        if len(tail_segs) < 2:
            return None
        kind, ident = tail_segs[0], tail_segs[1]
        if kind not in ("issues", "work_items", "merge_requests"):
            return None
        if not ident.isdigit():
            return None
        if project.path and proj_path.lower() != project.path.lower():
            raise ValueError(
                f"id URL points to '{proj_path}', but project_id "
                f"'{project.id}' is configured for '{project.path}'."
            )
        return ident


_PARSERS: dict[str, _RefParser] = {
    "github": GitHubRefParser(),
    "gitlab": GitLabRefParser(),
}


def _looks_like_url(s: str) -> bool:
    return "://" in s


def normalize_id(raw: str | int | None, project: ProjectConfig) -> str | None:
    """Normalise a ticket / PR / comment id input to its bare numeric form.

    Returns `None` when `raw` is `None` (the caller likely passed an
    optional arg). Raises `ValueError` with a helpful message when the
    input is non-empty but cannot be parsed.

    Accepted shapes:
      - bare numeric: `12`, `"12"`              → `"12"`
      - hash-prefixed: `"#12"`, `"  #12  "`     → `"12"`
      - GitHub issue/PR URL                      → `"12"`
      - GitLab issue / work-item / MR URL        → `"12"`
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        return str(raw)
    if not isinstance(raw, str):
        raise ValueError(
            f"id must be a string or integer, got {type(raw).__name__}"
        )
    stripped = raw.strip()
    if not stripped:
        raise ValueError("id is empty after trimming whitespace")
    if _looks_like_url(stripped):
        parser = _PARSERS.get(project.provider)
        if parser is None:
            raise ValueError(
                f"cannot parse URL for provider '{project.provider}'"
            )
        result = parser.parse_url(stripped, project)
        if result is None:
            raise ValueError(
                f"URL {stripped!r} does not look like a "
                f"{project.provider} issue / PR / comment URL"
            )
        return result
    if stripped.startswith("#"):
        stripped = stripped[1:].strip()
    if not stripped.isdigit():
        raise ValueError(
            f"id {raw!r} could not be normalised — expected a bare number, "
            "'#N', or a full issue/PR URL"
        )
    return stripped


def normalize_target(raw: str | int | None, project: ProjectConfig) -> str | None:
    """Normalise a relation `target` input.

    Same rules as `normalize_id`, plus a pass-through for the
    cross-repo forms `owner/repo#N` (GitHub) and `group/project#N` /
    `group/project!N` (GitLab). The upstream `add_relation` handler is
    responsible for rejecting these with `NotImplementedError` until
    cross-project relations land — we keep the raw form intact so its
    rejection message stays informative.
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        return str(raw)
    if not isinstance(raw, str):
        raise ValueError(
            f"target must be a string or integer, got {type(raw).__name__}"
        )
    stripped = raw.strip()
    if not stripped:
        raise ValueError("target is empty after trimming whitespace")
    # Cross-repo form: owner/repo#N or owner/repo!N — pass through unchanged.
    if "/" in stripped and ("#" in stripped or "!" in stripped):
        return stripped
    return normalize_id(stripped, project)


__all__ = [
    "GitHubRefParser",
    "GitLabRefParser",
    "normalize_id",
    "normalize_target",
]
