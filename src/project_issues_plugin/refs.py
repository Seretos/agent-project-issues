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

from lib_python_projects import ProjectConfig


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


class AzureDevOpsRefParser:
    """Parses Azure DevOps work-item and pull-request URLs.

    Recognised shapes (host = `dev.azure.com` or `<org>.visualstudio.com`):
      - `/{org}/{project}/_workitems/edit/{id}`
      - `/{org}/{project}/_git/{repo}/pullrequest/{id}`

    The legacy `<org>.visualstudio.com` host omits the leading `/{org}`
    segment because the org is encoded in the subdomain — we normalise
    that into the modern form before comparing.
    """

    def parse_url(self, url: str, project: ProjectConfig) -> str | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None
        host = parts.netloc.lower()
        segs = [s for s in parts.path.split("/") if s]
        if host == "dev.azure.com":
            if len(segs) < 3:
                return None
            org, proj, kind = segs[0], segs[1], segs[2]
        elif host.endswith(".visualstudio.com"):
            if len(segs) < 2:
                return None
            org = host.split(".", 1)[0]
            proj, kind = segs[0], segs[1]
            segs = [org, *segs]
        else:
            return None

        # Work item: /{org}/{project}/_workitems/edit/{id}
        if kind == "_workitems":
            if len(segs) < 5 or segs[3] != "edit":
                return None
            ident = segs[4]
            if not ident.isdigit():
                return None
            self._guard_project_scope(project, org, proj, repo=None)
            return ident

        # Pull request: /{org}/{project}/_git/{repo}/pullrequest/{id}
        if kind == "_git":
            if len(segs) < 6 or segs[4] != "pullrequest":
                return None
            repo, ident = segs[3], segs[5]
            if not ident.isdigit():
                return None
            self._guard_project_scope(project, org, proj, repo=repo)
            return ident

        return None

    @staticmethod
    def _guard_project_scope(
        project: ProjectConfig,
        org: str,
        proj: str,
        repo: str | None,
    ) -> None:
        """Reject URLs that point at a different ADO project/repo than the
        configured one. Work-item URLs only check org+project (since work
        items are project-scoped); PR URLs additionally check the repo.
        """
        cfg_org, cfg_proj, cfg_repo = (
            project.organization,
            project.ado_project,
            project.repository,
        )
        if cfg_org and org.lower() != cfg_org.lower():
            raise ValueError(
                f"id URL points to organization '{org}', but project_id "
                f"'{project.id}' is configured for '{cfg_org}'."
            )
        if cfg_proj and proj.lower() != cfg_proj.lower():
            raise ValueError(
                f"id URL points to project '{proj}', but project_id "
                f"'{project.id}' is configured for '{cfg_proj}'."
            )
        if repo is not None and cfg_repo and repo.lower() != cfg_repo.lower():
            raise ValueError(
                f"id URL points to repository '{repo}', but project_id "
                f"'{project.id}' is configured for '{cfg_repo}'."
            )


_PARSERS: dict[str, _RefParser] = {
    "github": GitHubRefParser(),
    "gitlab": GitLabRefParser(),
    "azuredevops": AzureDevOpsRefParser(),
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
      - GitLab composite comment id `"5/123"`    → `"5/123"` (pass-through)
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
    # GitLab composite comment id "<iid>/<note_id>" (both numeric): pass it
    # through unchanged so the provider's composite parsing handles it —
    # get/update/delete_comment document this self-contained form. It is not
    # a valid ticket/PR id, but passing it through (rather than raising) is
    # harmless there: the provider just returns a clean 404.
    composite = [seg.strip() for seg in stripped.split("/")]
    if len(composite) == 2 and all(seg.isdigit() for seg in composite):
        return "/".join(composite)
    if not stripped.isdigit():
        raise ValueError(
            f"id {raw!r} could not be normalised — expected a bare number, "
            "'#N', a '<iid>/<note_id>' comment id, or a full issue/PR URL"
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
    "AzureDevOpsRefParser",
    "GitHubRefParser",
    "GitLabRefParser",
    "normalize_id",
    "normalize_target",
]
