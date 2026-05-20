"""GitLab provider — REST v4 implementation.

Counterpart to `providers/github.py`. Maps GitLab REST API v4 onto the
provider-agnostic dataclasses defined in `providers/base.py`. The
caller (`tools/*`) never sees GitLab-isms leak through.

GitLab vs GitHub — naming map:
  - GitHub "issue"        ↔ GitLab "issue" (uses `iid`, not `id`)
  - GitHub "pull request" ↔ GitLab "merge request" (`PullRequest`)
  - GitHub "comment"      ↔ GitLab "note" (`Comment`)
  - GitHub "workflow run" ↔ GitLab "pipeline" (`PipelineRun`)

GitLab does not split `state` and `state_reason` the way GitHub does;
closed issues are simply `closed`. The marker label
`ai-closed-not-planned` (from `markers.py`) is the stand-in agents can
use to express "won't do" semantics — applied by the caller, not by
this provider.

Auth: `PRIVATE-TOKEN: <pat>` header. OAuth-Bearer flow is not in the
initial pass — callers needing it can set `base_url` to a proxy that
rewrites the header.

Project addressing: the GitLab REST API accepts an URL-encoded project
path (`group/sub/project` → `group%2Fsub%2Fproject`) as the `:id`
segment everywhere. `_project_path()` centralises that.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.markers import (
    AI_GENERATED_LABEL,
    AI_MODIFIED_LABEL,
    apply_body_marker,
    ensure_body_prefix,
    ensure_comment_prefix,
    has_ai_generated_marker,
)
from project_issues_plugin.providers.base import (
    Comment,
    FailingJob,
    PipelineFailure,
    PipelineRun,
    PRFilters,
    PullRequest,
    Relation,
    Status,
    StatusSpec,
    Ticket,
    TicketFilters,
    TokenCapabilities,
    TokenCapabilityProvider,
)

log = logging.getLogger("project-issues.gitlab")

USER_AGENT = "claude-code-project-issues-plugin/0.1.0"
DEFAULT_BASE_URL = "https://gitlab.com"


class GitLabError(RuntimeError):
    """Raised on any non-success response from the GitLab REST API.

    Mirrors `GitHubError` so `tools/_providers.py::_safe` can translate
    both into the same `{"error": "<message>"}` shape.
    """

    def __init__(self, status: int, message: str):
        super().__init__(f"GitLab {status}: {message}")
        self.status = status
        self.message = message


# ---------- client / request helpers -----------------------------------------


def _base_url(project: ProjectConfig) -> str:
    """Resolve the GitLab REST root for a project.

    Honours `project.base_url` for self-hosted instances. Strips any
    trailing slash so concatenation stays predictable.
    """
    base = (project.base_url or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/api/v4"


def _client(project: ProjectConfig, token: str | None) -> httpx.Client:
    """Build a configured httpx client for a GitLab project.

    The token is sent via the `PRIVATE-TOKEN` header — GitLab's
    preferred form for PATs. Unset token is fine for public-project
    read calls; write calls error out at the API.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["PRIVATE-TOKEN"] = token
    return httpx.Client(
        base_url=_base_url(project),
        headers=headers,
        timeout=30.0,
    )


def _check(resp: httpx.Response) -> None:
    """Raise `GitLabError` for any non-2xx response.

    GitLab error payloads come in three shapes:
      - `{"message": "..."}` for most errors
      - `{"error": "...", "error_description": "..."}` for OAuth
      - `{"message": {"field": ["err"]}}` for validation failures

    We collapse all three into one string so the caller gets a single
    consistent message.
    """
    if resp.is_success:
        return
    msg: str
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            raw = payload.get("message") or payload.get("error") or ""
            if isinstance(raw, dict):
                # Validation failures: {"message": {"field": ["err"]}}
                parts = [f"{k}: {v}" for k, v in raw.items()]
                msg = "; ".join(parts) or resp.reason_phrase
            else:
                msg = str(raw) or resp.reason_phrase
            extra = payload.get("error_description")
            if extra:
                msg = f"{msg} ({extra})"
        else:
            msg = resp.reason_phrase or "request failed"
    except Exception:
        msg = resp.reason_phrase or "request failed"
    raise GitLabError(resp.status_code, msg)


def _project_path(project: ProjectConfig) -> str:
    """URL-encoded project identifier for use as the `:id` path segment.

    GitLab accepts either the numeric project id or the URL-encoded
    namespace path. We always use the path form because it round-trips
    cleanly from the YAML config.
    """
    if not project.path:
        raise ValueError(
            f"project '{project.id}' has no 'path' configured — "
            f"GitLab requires a namespace path (e.g. 'group/sub/project')"
        )
    # `safe=""` so slashes get encoded — GitLab requires that.
    return quote(project.path, safe="")


# ---------- mappers ----------------------------------------------------------


def _map_issue(raw: dict) -> Ticket:
    """Translate a GitLab issue payload into a `Ticket`.

    Status mapping:
      - GitLab `opened`/`reopened` → `"open"`
      - GitLab `closed`            → `"closed"`

    GitLab does not have a `state_reason` equivalent; the
    `ai-closed-not-planned` LABEL is the agent-side convention for
    "won't do" semantics (see `markers.py`).
    """
    state = raw.get("state", "opened")
    if state in ("opened", "reopened"):
        status: Status = "open"
    else:
        status = "closed"
    author = raw.get("author") or {}
    return Ticket(
        id=str(raw["iid"]),  # IID — project-scoped; matches user-visible URL
        title=raw.get("title") or "",
        body=raw.get("description") or "",
        status=status,
        author=author.get("username", ""),
        assignees=[
            a.get("username", "") for a in (raw.get("assignees") or [])
        ],
        labels=list(raw.get("labels") or []),
        url=raw.get("web_url") or "",
        created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or "",
    )


def _map_note(raw: dict) -> Comment:
    """Translate a GitLab note (comment) payload into a `Comment`.

    System notes (state changes, label edits, etc.) carry
    `"system": true`. They are NOT filtered here — callers that want
    to skip system notes do so at the list-comments call site.
    """
    author = raw.get("author") or {}
    return Comment(
        id=str(raw["id"]),
        author=author.get("username", ""),
        body=raw.get("body") or "",
        url=raw.get("web_url") or "",
        created_at=raw.get("created_at") or "",
    )


def _map_mergeable(raw: dict) -> bool | None:
    """Translate GitLab's merge-status field into a tri-state bool.

    GitLab returns one of `detailed_merge_status` (preferred, GitLab
    13.0+) or the legacy `merge_status`. Mapping:
      - `mergeable`, `can_be_merged`              → True
      - any `cannot_be_merged*` value             → False
      - `checking`, `unchecked`, missing, unknown → None
    """
    raw_status = raw.get("detailed_merge_status") or raw.get("merge_status")
    if not raw_status:
        return None
    if raw_status in ("mergeable", "can_be_merged"):
        return True
    if raw_status.startswith("cannot_be_merged"):
        return False
    return None


def _map_mr(raw: dict) -> PullRequest:
    """Translate a GitLab merge-request payload into a `PullRequest`.

    Status mapping:
      - GitLab `opened`/`reopened` → `"open"`
      - GitLab `closed`            → `"closed"`
      - GitLab `merged`            → `"merged"`
      - GitLab `locked`            → `"closed"` (treat as terminal)

    `head` / `base` use GitLab's `source_branch` / `target_branch`. The
    SHA comes from `sha` on the MR root. `repo_full_name` is the
    target project path; cross-fork sources are not resolved into a
    full name here (would require an extra round-trip — defer).
    """
    state = raw.get("state", "opened")
    if state in ("opened", "reopened"):
        status: str = "open"
    elif state == "merged":
        status = "merged"
    else:
        status = "closed"
    merged = state == "merged" or bool(raw.get("merged_at"))
    author = raw.get("author") or {}
    # Reviewers: GitLab MR `reviewers` is the assigned list. There's no
    # "submitted vs requested" split — surface both under the same data.
    reviewer_usernames = [
        r.get("username", "") for r in (raw.get("reviewers") or [])
    ]
    head = {
        "ref": raw.get("source_branch", "") or "",
        "sha": raw.get("sha", "") or "",
        # Project full name is the target path by default; cross-fork
        # callers can re-resolve via source_project_id if needed.
        "repo_full_name": "",
    }
    base = {
        "ref": raw.get("target_branch", "") or "",
        # GitLab doesn't expose target-base SHA on the MR root —
        # callers reading this can compare against `diff_refs.base_sha`
        # but we don't surface that to keep the shape uniform.
        "sha": "",
    }
    return PullRequest(
        id=str(raw["iid"]),
        number=int(raw["iid"]),
        title=raw.get("title") or "",
        body=raw.get("description") or "",
        status=status,  # type: ignore[arg-type]
        draft=bool(raw.get("draft") or raw.get("work_in_progress")),
        author=author.get("username", ""),
        assignees=[
            a.get("username", "") for a in (raw.get("assignees") or [])
        ],
        reviewers=list(reviewer_usernames),
        requested_reviewers=list(reviewer_usernames),
        labels=list(raw.get("labels") or []),
        head=head,
        base=base,
        merged=merged,
        mergeable=_map_mergeable(raw),
        url=raw.get("web_url") or "",
        created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or "",
    )


# GitLab pipeline statuses we treat as terminal — anything else is
# in-flight and yields `conclusion=None` on the common dataclass.
_TERMINAL_PIPELINE_STATUSES = {"success", "failed", "canceled", "skipped"}


def _map_pipeline_run(raw: dict) -> PipelineRun:
    """Translate a GitLab pipeline payload into a `PipelineRun`.

    Mapping nuances vs GitHub `workflow_run`:
      - `name`: GitLab pipelines have no single "workflow name"; fall
        back to `f"pipeline-{id}"` so the field is always populated.
      - `event`: GitLab's `source` field is the closest equivalent.
      - `status` / `conclusion`: GitLab statuses that mean "terminal"
        (success/failed/canceled/skipped) are folded into
        `status="completed", conclusion=<that_value>` so the response
        shape matches GitHub. Non-terminal values pass through.
      - `run_attempt`: GitLab has no per-pipeline attempt counter.
        Defaults to 1; not retrievable from the REST root.
    """
    raw_status = raw.get("status") or ""
    if raw_status in _TERMINAL_PIPELINE_STATUSES:
        status = "completed"
        conclusion: str | None = raw_status
    else:
        status = raw_status or "unknown"
        conclusion = None
    pipeline_id = raw.get("id")
    return PipelineRun(
        id=str(pipeline_id) if pipeline_id is not None else "",
        name=f"pipeline-{pipeline_id}" if pipeline_id is not None else "",
        branch=raw.get("ref", "") or "",
        head_sha=raw.get("sha", "") or "",
        event=raw.get("source", "") or "",
        status=status,
        conclusion=conclusion,
        url=raw.get("web_url") or "",
        created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or raw.get("finished_at") or "",
        run_attempt=1,
        failure=None,
    )


# ---------- helpers used by GitLabProvider methods ---------------------------


def _status_to_state_event(status: Status) -> str:
    """Map common status string → GitLab `state_event`.

    GitLab issue/MR updates take `state_event=close|reopen`, not
    `state=closed`. The provider-agnostic status string carries GitHub's
    `closed:completed` / `closed:not_planned` distinction, but GitLab
    has no `state_reason`, so both collapse to `close`. Agents wanting
    "not planned" semantics apply the `ai-closed-not-planned` label
    (see `markers.py`).
    """
    if status == "open":
        return "reopen"
    if status in ("closed", "closed:completed", "closed:not_planned"):
        return "close"
    raise ValueError(
        f"unsupported status {status!r} for GitLab — accepted: "
        f"open, closed, closed:completed, closed:not_planned"
    )


def _resolve_assignee_ids(
    client: httpx.Client, usernames: list[str],
) -> list[int]:
    """Resolve a list of usernames → integer user ids.

    GitLab issue/MR endpoints accept `assignee_ids` (integer list) but
    not usernames. Resolution uses `/users?username=<name>` which
    returns a list — we take the first match. Unknown usernames are
    silently dropped (consistent with GitHub's behaviour of accepting
    unknown assignees without 4xx-ing the whole update).
    """
    resolved: list[int] = []
    for name in usernames:
        if not name:
            continue
        r = client.get("/users", params={"username": name})
        _check(r)
        matches = r.json()
        if matches:
            uid = matches[0].get("id")
            if isinstance(uid, int):
                resolved.append(uid)
    return resolved


_MENTION_PATTERN = re.compile(r"(?:(?P<scope>[\w./-]+)?#)(?P<n>\d+)\b")
_CLOSE_PATTERN = re.compile(
    r"(?i)\b(?:closes?|fixes?|resolves?|implements?)\s+"
    r"(?P<ref>(?:[\w./-]+)?#\d+)\b"
)
_DUPLICATE_PATTERN = re.compile(
    r"(?i)\bduplicate\s+of\s+(?P<ref>(?:[\w./-]+)?#\d+)\b"
)


def _mentions_scan_depth() -> int:
    """Mirror the GitHub provider's `PROJECT_ISSUES_MENTIONS_SCAN_DEPTH`
    contract: `-1` = scan every comment, `0` = body only, `N` = first N.
    Default `0` (body only) so we don't fan out reads on big tickets."""
    raw = os.environ.get("PROJECT_ISSUES_MENTIONS_SCAN_DEPTH", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _make_relation(
    kind: str,
    ref: str,
    *,
    title: str = "",
    url: str = "",
    state: str = "",
    is_pull_request: bool = False,
) -> Relation:
    """Build a `Relation` with the canonical ticket-id format.

    `ref` is either `#N` (same-project) or `group/project#N`. Strip
    leading `#` only when present standalone; otherwise pass through.
    """
    return Relation(
        kind=kind,
        ticket_id=ref if ref.startswith("#") else ref,
        title=title,
        url=url,
        state=state,
        is_pull_request=is_pull_request,
    )


def _scan_refs(text: str, pattern: re.Pattern) -> list[str]:
    """Extract unique ticket references from a piece of text.

    Returns each match in its `[scope]#N` form, deduplicated, in
    source order. Used for outgoing relations (`mentions`, `closes`,
    `duplicate_of`).
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in pattern.finditer(text):
        # Pattern's `ref` group covers the full `[scope]#N` for the
        # close / duplicate scanners; the bare mention scanner exposes
        # `scope` and `n` separately.
        if "ref" in m.groupdict():
            ref = m.group("ref")
        else:
            scope = m.group("scope") or ""
            n = m.group("n")
            ref = f"{scope}#{n}" if scope else f"#{n}"
        if ref not in seen_set:
            seen.append(ref)
            seen_set.add(ref)
    return seen


def _fetch_relations(
    client: httpx.Client,
    project: ProjectConfig,
    ticket_id: str,
    *,
    ticket_body: str,
    comments: list[Comment],
) -> list[Relation]:
    """Build the relations list for a GitLab issue.

    Combines four sources:

    1. **Issue links** (`/projects/:id/issues/:iid/links`) — GitLab's
       first-class relation surface. Each link carries a `link_type`
       of `relates_to` / `blocks` / `is_blocked_by`; we map those into
       `relates_to` / `blocks` / `blocked_by` on the common
       `RelationKind` literal.

    2. **Closing MRs** (`/projects/:id/issues/:iid/closed_by`) — MRs
       that auto-closed this issue. Surfaced as `closed_by`.

    3. **Outgoing scans on body** — `closes`/`fixes`/`resolves` →
       `closes`; `duplicate of` → `duplicate_of`; plain `#N` references
       → `mentions`. Body-only by default; bump
       `PROJECT_ISSUES_MENTIONS_SCAN_DEPTH` to also scan comments.

    Returns the list in (kind, ticket_id) order for determinism.
    """
    relations: list[Relation] = []
    path = _project_path(project)

    # --- (1) issue links ---
    rl = client.get(f"/projects/{path}/issues/{ticket_id}/links")
    if rl.is_success:
        for link in rl.json():
            link_type = link.get("link_type") or "relates_to"
            kind_map = {
                "relates_to": "relates_to",
                "blocks": "blocks",
                "is_blocked_by": "blocked_by",
            }
            kind = kind_map.get(link_type, "relates_to")
            target_iid = link.get("iid")
            target_project = link.get("references", {}).get(
                "relative", f"#{target_iid}" if target_iid else ""
            )
            relations.append(_make_relation(
                kind=kind,
                ref=target_project,
                title=link.get("title", "") or "",
                url=link.get("web_url", "") or "",
                state="opened" if link.get("state") == "opened" else (
                    "closed" if link.get("state") == "closed" else ""
                ),
            ))

    # --- (2) closing MRs ---
    rc = client.get(f"/projects/{path}/issues/{ticket_id}/closed_by")
    if rc.is_success:
        for mr in rc.json():
            mr_iid = mr.get("iid")
            if mr_iid is None:
                continue
            relations.append(_make_relation(
                kind="closed_by",
                ref=f"#{mr_iid}",
                title=mr.get("title", "") or "",
                url=mr.get("web_url", "") or "",
                state=mr.get("state", "") or "",
                is_pull_request=True,
            ))

    # --- (3) outgoing scans ---
    scan_depth = _mentions_scan_depth()
    bodies_to_scan: list[str] = [ticket_body or ""]
    if scan_depth != 0 and comments:
        if scan_depth < 0:
            bodies_to_scan.extend(c.body for c in comments)
        else:
            bodies_to_scan.extend(c.body for c in comments[:scan_depth])
    full_text = "\n".join(bodies_to_scan)

    # Closing keywords → `closes`. Consume these refs first so the
    # plain-mention scanner doesn't double-count them.
    close_refs = _scan_refs(full_text, _CLOSE_PATTERN)
    close_ref_set = set(close_refs)
    for ref in close_refs:
        relations.append(_make_relation(kind="closes", ref=ref))

    # Duplicate-of detection.
    dup_refs = _scan_refs(full_text, _DUPLICATE_PATTERN)
    dup_ref_set = set(dup_refs)
    for ref in dup_refs:
        relations.append(_make_relation(kind="duplicate_of", ref=ref))

    # Plain mentions (filtered against the above two sets and self-ref).
    self_ref = f"#{ticket_id}"
    for ref in _scan_refs(full_text, _MENTION_PATTERN):
        if ref == self_ref or ref in close_ref_set or ref in dup_ref_set:
            continue
        relations.append(_make_relation(kind="mentions", ref=ref))

    return relations


def _list_pipelines(
    project: ProjectConfig,
    token: str | None,
    extra_params: dict[str, Any],
    limit: int,
) -> list[PipelineRun]:
    """Shared body for list_runs_for_branch/tag/commit.

    GitLab's `/projects/:id/pipelines` endpoint accepts `ref`, `sha`,
    `status`, `source`, etc. Callers pass the addressing param via
    `extra_params`; we add `per_page` and order.
    """
    per_page = min(max(1, limit), 100)
    params: dict[str, Any] = {
        "per_page": per_page,
        "order_by": "id",
        "sort": "desc",
        **extra_params,
    }
    with _client(project, token) as client:
        r = client.get(
            f"/projects/{_project_path(project)}/pipelines",
            params=params,
        )
        _check(r)
        return [_map_pipeline_run(it) for it in r.json()]


# Maximum trace tail size we surface in `FailingJob.log_excerpt`. Trace
# files can be megabytes; the agent only needs the last screenful or
# two to see the actual failure.
_TRACE_TAIL_LIMIT = 4096


def _fetch_pipeline_failure(
    client: httpx.Client,
    project: ProjectConfig,
    pipeline_id: str,
) -> PipelineFailure | None:
    """Build a `PipelineFailure` for a failed pipeline.

    Walks the pipeline's jobs, filters to `status == "failed"`, and
    fetches the trace (last `_TRACE_TAIL_LIMIT` bytes) for each. GitLab
    does not expose GitHub-style structured annotations; the
    `annotations` field is therefore always `[]`.

    Returns `None` if the jobs endpoint is unreachable — preserves
    the "best-effort" contract documented on `PipelineRun.failure`.
    """
    path = _project_path(project)
    r = client.get(
        f"/projects/{path}/pipelines/{pipeline_id}/jobs",
        params={"per_page": 100},
    )
    if not r.is_success:
        return PipelineFailure(failing_jobs=[], note="jobs endpoint unavailable")
    jobs = r.json()
    failing: list[FailingJob] = []
    note: str | None = None
    for job in jobs:
        if job.get("status") != "failed":
            continue
        job_id = job.get("id")
        if job_id is None:
            continue
        trace_excerpt: str | None = None
        tr = client.get(f"/projects/{path}/jobs/{job_id}/trace")
        if tr.is_success:
            text = tr.text
            if len(text) > _TRACE_TAIL_LIMIT:
                trace_excerpt = text[-_TRACE_TAIL_LIMIT:]
            else:
                trace_excerpt = text
        else:
            note = "trace endpoint unavailable"
        failing.append(FailingJob(
            name=job.get("name", "") or "",
            url=job.get("web_url", "") or "",
            failed_step=job.get("stage", "") or "",
            annotations=[],  # GitLab has no structured annotation surface
            log_excerpt=trace_excerpt,
        ))
    return PipelineFailure(failing_jobs=failing, note=note)


# ---------- provider ---------------------------------------------------------


class GitLabProvider(TokenCapabilityProvider):
    """GitLab REST v4 provider.

    Method bodies are filled in incrementally — see the task list in
    `~/.claude/plans/so-wir-haben-jetzt-snappy-deer.md`. Stubs raise
    `NotImplementedError` so the registry-level dispatch is exercised
    today even though individual operations aren't yet plumbed through.
    """

    # ---------- token capabilities (TokenCapabilityProvider) -----------------

    def probe_token_capabilities(
        self, project: ProjectConfig, token: str
    ) -> TokenCapabilities:
        """Probe a GitLab PAT's scopes via `/personal_access_tokens/self`.

        GitLab tokens don't split issues vs PR scopes the way GitHub's
        fine-grained PATs do. Coarsest mapping:
          - `api` scope (full read+write)              → all five flags True
          - `read_api` / read-only / unknown scopes    → all False (token
            still passes through for read, gated implicitly), reason set
          - 401                                        → "bad_credentials"
          - 404 on self-endpoint                       → "bad_credentials"
            (treat as invalid token rather than 404 from project, since
            `/self` succeeds on any valid token)
          - transport failure                          → "network_error"
          - response missing `scopes`                  → "permissions_field_missing"

        On any failure mode, all flags are False and `reason` is set so
        the caller can degrade gracefully (no operation granted on a
        failed probe).
        """
        try:
            with _client(project, token) as client:
                r = client.get("/personal_access_tokens/self")
        except httpx.HTTPError:
            return TokenCapabilities(reason="network_error")
        if r.status_code == 401:
            return TokenCapabilities(reason="bad_credentials")
        if r.status_code == 404:
            return TokenCapabilities(reason="bad_credentials")
        if not r.is_success:
            return TokenCapabilities(reason="network_error")
        try:
            payload = r.json()
        except Exception:
            return TokenCapabilities(reason="permissions_field_missing")
        scopes = payload.get("scopes")
        if not isinstance(scopes, list):
            return TokenCapabilities(reason="permissions_field_missing")
        if "api" in scopes:
            return TokenCapabilities(
                issues_create=True, issues_modify=True,
                pulls_create=True, pulls_modify=True, pulls_merge=True,
                reason=None,
            )
        return TokenCapabilities(reason="insufficient_scope")

    # ---------- issues -------------------------------------------------------

    def list_tickets(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> list[Ticket]:
        """List issues in a project.

        Filter mapping (GitLab REST `/projects/:id/issues`):
          - `status`: `open`→`opened`, `closed`→`closed`, `any`→`all`.
          - `labels`: comma-joined `labels=` param. `not_labels` → `not[labels]`.
          - `assignee` → `assignee_username`. `author` → `author_username`.
          - `created_after/before` / `updated_after/before` pass through.
          - `search` passes through as the GitLab `search` param.
          - `sort_by`: `created`→`created_at`, `updated`→`updated_at`,
            `comments`→`user_notes_count`. `sort_order` → `sort`.
          - `limit` is `per_page`, capped at 100. Single page only.
        """
        per_page = min(max(1, filters.limit), 100)
        sort_by_map = {
            "created": "created_at",
            "updated": "updated_at",
            "comments": "user_notes_count",
        }
        state_map = {"open": "opened", "closed": "closed", "any": "all"}
        params: dict[str, Any] = {
            "per_page": per_page,
            "state": state_map.get(filters.status, "opened"),
            "order_by": sort_by_map.get(filters.sort_by, "created_at"),
            "sort": filters.sort_order,
        }
        if filters.labels:
            params["labels"] = ",".join(filters.labels)
        if filters.not_labels:
            # GitLab's REST array syntax: `not[labels][]=foo` repeated, or
            # in a single comma-joined string. The single-string form is
            # supported on `/issues` since 12.x.
            params["not[labels]"] = ",".join(filters.not_labels)
        if filters.assignee:
            params["assignee_username"] = filters.assignee
        if filters.author:
            params["author_username"] = filters.author
        if filters.search:
            params["search"] = filters.search
        if filters.created_after:
            params["created_after"] = filters.created_after
        if filters.created_before:
            params["created_before"] = filters.created_before
        if filters.updated_after:
            params["updated_after"] = filters.updated_after
        if filters.updated_before:
            params["updated_before"] = filters.updated_before
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{_project_path(project)}/issues", params=params,
            )
            _check(r)
            return [_map_issue(it) for it in r.json()]

    def get_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        *,
        include_relations: bool = True,
    ) -> tuple[Ticket, list[Comment], list[Relation], bool]:
        """Fetch a single issue plus its non-system notes.

        Relations are populated by a separate code path (see
        `_fetch_gitlab_relations` once implemented in task #7); for now
        relations come back as `[]` regardless of `include_relations`
        so this method is usable for the basic "view ticket" flow.

        System notes (state changes, label edits) are filtered out —
        they're not user-facing comments.
        """
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(f"/projects/{path}/issues/{ticket_id}")
            _check(r)
            ticket = _map_issue(r.json())
            c = client.get(
                f"/projects/{path}/issues/{ticket_id}/notes",
                params={"per_page": 100, "sort": "asc", "order_by": "created_at"},
            )
            _check(c)
            comments = [
                _map_note(it) for it in c.json()
                if not it.get("system", False)
            ]
            relations: list[Relation] = []
            truncated = False
            if include_relations:
                relations = _fetch_relations(
                    client, project, ticket_id, ticket_body=ticket.body,
                    comments=comments,
                )
        return ticket, comments, relations, truncated

    def create_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        title: str,
        body: str,
        labels: list[str],
        assignees: list[str],
        *,
        status: Status | None = None,
    ) -> Ticket:
        """Create a GitLab issue with the AI-generated marker.

        Marker policy mirrors `GitHubProvider.create_ticket`:
          - The `#ai-generated` body prefix is the canonical attribution
            and is always applied (idempotent).
          - The `ai-generated` LABEL is also applied. Unlike GitHub,
            GitLab allows any project member to apply labels by name
            (no pre-create step required) — but if the label doesn't
            exist yet, GitLab silently creates it.

        Assignees are passed as usernames; GitLab requires user IDs on
        the POST. We resolve usernames → IDs via `/users?username=` so
        the caller doesn't have to.

        Optional `status` (ticket #42) accepts the same vocabulary as
        `update_ticket.status`. The GitLab `POST /issues` endpoint
        creates in `opened` state; non-`open` requests are landed via
        a follow-up PUT with `state_event=close`. Validation is
        performed up-front (`_status_to_state_event`) so an invalid
        value rejects before the POST.
        """
        # Validate `status` up-front. Pass None through; raise on
        # unknown values before POST commits an issue.
        state_event: str | None = None
        if status is not None:
            state_event = _status_to_state_event(status)
        merged_labels = list(dict.fromkeys([*labels, AI_GENERATED_LABEL]))
        prefixed_body = ensure_body_prefix(body)
        path = _project_path(project)
        with _client(project, token) as client:
            assignee_ids = _resolve_assignee_ids(client, assignees)
            payload: dict[str, Any] = {
                "title": title,
                "description": prefixed_body,
            }
            if merged_labels:
                payload["labels"] = ",".join(merged_labels)
            if assignee_ids:
                payload["assignee_ids"] = assignee_ids
            r = client.post(f"/projects/{path}/issues", json=payload)
            _check(r)
            raw = r.json()
            # Follow-up PUT for non-`open` initial status (state_event
            # is only `close` here — `reopen` is a no-op on a freshly
            # created issue).
            if state_event == "close":
                iid = raw.get("iid")
                pu = client.put(
                    f"/projects/{path}/issues/{iid}",
                    json={"state_event": "close"},
                )
                _check(pu)
                raw = pu.json()
            return _map_issue(raw)

    def update_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        status: Status | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
    ) -> Ticket:
        """Update an issue.

        Status mapping:
          - `"open"` (or `"reopen"` legacy) → `state_event=reopen`.
          - `"closed"` (or `closed:completed` / `closed:not_planned`) →
            `state_event=close`. GitLab has no `state_reason`; the
            distinction is lost server-side. Agents wanting the
            "not planned" semantics apply the `ai-closed-not-planned`
            label via `labels_add` (see `markers.py`).

        Label add/remove use GitLab's dedicated `add_labels` /
        `remove_labels` params — no fetch+diff needed.

        Assignees: GitLab only accepts a final `assignee_ids` list, so
        we fetch current assignees, apply the delta, resolve usernames
        to ids, and send the result.

        `ai-modified` is added when the issue wasn't tagged
        `ai-generated` originally — same heuristic as the GitHub
        provider but implemented with the GitLab params.
        """
        path = _project_path(project)
        with _client(project, token) as client:
            # Always fetch current — needed for the ai-modified marker
            # decision, and for assignee delta resolution.
            r0 = client.get(f"/projects/{path}/issues/{ticket_id}")
            _check(r0)
            current = r0.json()
            current_labels = set(current.get("labels") or [])

            will_be_ai_generated = AI_GENERATED_LABEL in current_labels

            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if body is not None:
                # Ticket #44: re-stamp body marker to match label state.
                payload["description"] = apply_body_marker(
                    body, will_be_ai_generated=will_be_ai_generated,
                )
            if status is not None:
                payload["state_event"] = _status_to_state_event(status)

            add_set = set(labels_add or [])
            remove_set = set(labels_remove or [])
            if (
                not will_be_ai_generated
                and AI_MODIFIED_LABEL not in current_labels
            ):
                add_set.add(AI_MODIFIED_LABEL)
            if add_set:
                payload["add_labels"] = ",".join(sorted(add_set))
            if remove_set:
                payload["remove_labels"] = ",".join(sorted(remove_set))

            if assignees_add or assignees_remove:
                current_assignees = {
                    a.get("username", "")
                    for a in (current.get("assignees") or [])
                }
                final_usernames = set(current_assignees)
                if assignees_add:
                    final_usernames.update(assignees_add)
                if assignees_remove:
                    final_usernames.difference_update(assignees_remove)
                # GitLab accepts an empty list to mean "unassigned"; pass
                # it through so explicit removal works.
                payload["assignee_ids"] = _resolve_assignee_ids(
                    client, sorted(final_usernames),
                )

            if not payload:
                return _map_issue(current)
            r = client.put(
                f"/projects/{path}/issues/{ticket_id}", json=payload,
            )
            _check(r)
            return _map_issue(r.json())

    def list_statuses(
        self,
        project: ProjectConfig,  # noqa: ARG002 — kept for provider-agnostic signature
        token: str | None,         # noqa: ARG002 — same
    ) -> StatusSpec:
        """Return the GitLab-static status spec.

        GitLab issues have a fixed state-space (`opened` ↔ `closed`).
        Unlike GitHub, GitLab has no `state_reason` field, so the
        distinction between "completed" and "not planned" is collapsed:
        both terminal hints point at the same `"closed"` value. Callers
        that need the distinction apply the `ai-closed-not-planned`
        label (see `markers.py`).
        """
        return StatusSpec(
            values=["open", "closed"],
            transitions={
                "open": ["closed"],
                "closed": ["open"],
            },
            hints={
                "default_open": "open",
                "terminal": ["closed"],
                "terminal_completed": "closed",
                "terminal_declined": "closed",
            },
        )

    # ---------- comments / notes --------------------------------------------

    def add_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        body: str,
    ) -> Comment:
        """Post a note on an issue. The AI-comment prefix is applied."""
        prefixed = ensure_comment_prefix(body)
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.post(
                f"/projects/{path}/issues/{ticket_id}/notes",
                json={"body": prefixed},
            )
            _check(r)
            return _map_note(r.json())

    def list_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        limit: int = 30,
    ) -> list[Comment]:
        """List user notes (non-system) on an issue, oldest first.

        System notes (state changes, label edits, milestone moves) are
        filtered out — they aren't user-facing comments.
        """
        per_page = min(max(1, limit), 100)
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{path}/issues/{ticket_id}/notes",
                params={
                    "per_page": per_page,
                    "sort": "asc",
                    "order_by": "created_at",
                },
            )
            _check(r)
            return [
                _map_note(it) for it in r.json()
                if not it.get("system", False)
            ]

    def get_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
    ) -> Comment:
        """Fetch a single note by id.

        GitLab notes are scoped to their parent issue/MR — unlike GitHub
        comment ids which are repo-wide. The note id alone is not enough
        to address a note; we'd need the parent issue_iid too. The
        tool-layer wrapper accepts `ticket_id` for this reason and
        passes it along via the `comment_id` parameter encoded as
        `"<issue_iid>/<note_id>"`. Callers using the raw note id pass
        the note id by itself and we attempt to dispatch through the
        merge-request notes endpoint as well — but the recommended form
        is the slash-encoded composite key.
        """
        # Composite-key form: "issue_iid/note_id"
        path = _project_path(project)
        if "/" in comment_id:
            issue_iid, note_id = comment_id.split("/", 1)
            with _client(project, token) as client:
                r = client.get(
                    f"/projects/{path}/issues/{issue_iid}/notes/{note_id}",
                )
                _check(r)
                return _map_note(r.json())
        # Plain id — not addressable on GitLab without context. Surface
        # a clear error rather than guessing.
        raise GitLabError(
            400,
            "GitLab notes are scoped to a parent issue/MR; pass the "
            "comment id as '<issue_iid>/<note_id>'",
        )

    def update_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        body: str,
    ) -> Comment:
        """Edit a note, re-stamping the AI-marker.

        Marker policy (ticket #44): same as `GitHubProvider.update_comment`
        — if the existing note carries `#ai-generated`, the edit
        preserves that marker; otherwise it stamps `#ai-modified`.

        Accepts the same composite-key form as `get_comment`.
        """
        path = _project_path(project)
        if "/" not in comment_id:
            raise GitLabError(
                400,
                "GitLab notes are scoped to a parent issue/MR; pass the "
                "comment id as '<issue_iid>/<note_id>'",
            )
        issue_iid, note_id = comment_id.split("/", 1)
        with _client(project, token) as client:
            r0 = client.get(
                f"/projects/{path}/issues/{issue_iid}/notes/{note_id}",
            )
            _check(r0)
            current_body = r0.json().get("body") or ""
            will_be_ai_generated = has_ai_generated_marker(current_body)
            prefixed = apply_body_marker(
                body, will_be_ai_generated=will_be_ai_generated,
            )
            r = client.put(
                f"/projects/{path}/issues/{issue_iid}/notes/{note_id}",
                json={"body": prefixed},
            )
            _check(r)
            return _map_note(r.json())

    # ---------- merge requests (PR surface) ----------------------------------

    def list_prs(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: PRFilters,
    ) -> list[PullRequest]:
        """List merge requests for a project.

        Filter mapping (GitLab REST `/projects/:id/merge_requests`):
          - `status`: `open`→`opened`, `closed`→`closed`, `any`→`all`.
            Note: GitLab can't filter MRs by `merged` via `state`;
            agents wanting only merged MRs filter post-fetch on
            `status == "merged"`.
          - `labels` → comma-joined `labels` param.
          - `assignee` → `assignee_username`.
          - `head` → `source_branch`. `base` → `target_branch`.
          - `search` → `search` (matches title + description).
          - `limit` → `per_page`, capped at 100.
        """
        per_page = min(max(1, filters.limit), 100)
        state_map = {"open": "opened", "closed": "closed", "any": "all"}
        params: dict[str, Any] = {
            "per_page": per_page,
            "state": state_map.get(filters.status, "opened"),
            "order_by": "created_at",
            "sort": "desc",
        }
        if filters.labels:
            params["labels"] = ",".join(filters.labels)
        if filters.assignee:
            params["assignee_username"] = filters.assignee
        if filters.head:
            params["source_branch"] = filters.head
        if filters.base:
            params["target_branch"] = filters.base
        if filters.search:
            params["search"] = filters.search
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{_project_path(project)}/merge_requests",
                params=params,
            )
            _check(r)
            return [_map_mr(it) for it in r.json()]

    def get_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> tuple[PullRequest, list[Comment]]:
        """Fetch a single MR plus its non-system notes."""
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(f"/projects/{path}/merge_requests/{pr_id}")
            _check(r)
            pr = _map_mr(r.json())
            c = client.get(
                f"/projects/{path}/merge_requests/{pr_id}/notes",
                params={"per_page": 100, "sort": "asc", "order_by": "created_at"},
            )
            _check(c)
            comments = [
                _map_note(it) for it in c.json()
                if not it.get("system", False)
            ]
        return pr, comments

    def create_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> PullRequest:
        """Create a merge request with the AI-generated marker.

        Body prefix + `ai-generated` label applied. `draft` translates
        to the GitLab `draft` param (supported 14.x+). Older GitLab
        instances ignored the param and required a `Draft: ` title
        prefix; we don't synthesize that prefix here.
        """
        merged_labels = list(dict.fromkeys([*(labels or []), AI_GENERATED_LABEL]))
        prefixed_body = ensure_body_prefix(body)
        path = _project_path(project)
        with _client(project, token) as client:
            assignee_ids = _resolve_assignee_ids(client, assignees or [])
            payload: dict[str, Any] = {
                "title": title,
                "description": prefixed_body,
                "source_branch": head,
                "target_branch": base,
            }
            if draft:
                payload["draft"] = True
            if merged_labels:
                payload["labels"] = ",".join(merged_labels)
            if assignee_ids:
                payload["assignee_ids"] = assignee_ids
            r = client.post(f"/projects/{path}/merge_requests", json=payload)
            _check(r)
            return _map_mr(r.json())

    def update_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        base: str | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
    ) -> PullRequest:
        """Update an MR's metadata, status, base branch, labels, assignees.

        `status` accepts only `"open"` / `"closed"`. Use `merge_pr` to
        merge — `status="merged"` is rejected. Reopening a merged MR is
        not possible in GitLab; the API rejects the call.

        `ai-modified` is added when the MR wasn't tagged `ai-generated`
        — mirrors `update_ticket`.
        """
        path = _project_path(project)
        if status not in (None, "open", "closed"):
            raise ValueError(
                f"unsupported PR status {status!r} — use merge_pr() to "
                f"merge; accepted: open, closed"
            )
        with _client(project, token) as client:
            r0 = client.get(f"/projects/{path}/merge_requests/{pr_id}")
            _check(r0)
            current = r0.json()
            current_labels = set(current.get("labels") or [])

            will_be_ai_generated = AI_GENERATED_LABEL in current_labels

            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if body is not None:
                # Ticket #44: re-stamp body marker to match label state.
                payload["description"] = apply_body_marker(
                    body, will_be_ai_generated=will_be_ai_generated,
                )
            if status == "open":
                payload["state_event"] = "reopen"
            elif status == "closed":
                payload["state_event"] = "close"
            if base is not None:
                payload["target_branch"] = base

            add_set = set(labels_add or [])
            remove_set = set(labels_remove or [])
            if (
                not will_be_ai_generated
                and AI_MODIFIED_LABEL not in current_labels
            ):
                add_set.add(AI_MODIFIED_LABEL)
            if add_set:
                payload["add_labels"] = ",".join(sorted(add_set))
            if remove_set:
                payload["remove_labels"] = ",".join(sorted(remove_set))

            if assignees_add or assignees_remove:
                current_assignees = {
                    a.get("username", "")
                    for a in (current.get("assignees") or [])
                }
                final_usernames = set(current_assignees)
                if assignees_add:
                    final_usernames.update(assignees_add)
                if assignees_remove:
                    final_usernames.difference_update(assignees_remove)
                payload["assignee_ids"] = _resolve_assignee_ids(
                    client, sorted(final_usernames),
                )

            if not payload:
                return _map_mr(current)
            r = client.put(
                f"/projects/{path}/merge_requests/{pr_id}", json=payload,
            )
            _check(r)
            return _map_mr(r.json())

    def add_pr_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        body: str,
    ) -> Comment:
        """Post a note on a merge request. AI-comment prefix applied."""
        prefixed = ensure_comment_prefix(body)
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.post(
                f"/projects/{path}/merge_requests/{pr_id}/notes",
                json={"body": prefixed},
            )
            _check(r)
            return _map_note(r.json())

    def merge_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        strategy: str = "merge",
        commit_message: str | None = None,
    ) -> PullRequest:
        """Merge a merge request.

        Strategy mapping:
          - `"merge"` → POST `.../merge` with no `squash` flag (true
            merge commit).
          - `"squash"` → POST `.../merge` with `squash=true`.
          - `"rebase"` → rejected. GitLab's rebase flow is a separate
            `PUT .../rebase` endpoint that doesn't perform the merge
            itself; agents wanting a rebase-first merge should call
            the rebase endpoint and then `merge_pr(strategy="merge")`.
            We surface a clear error here rather than silently doing
            something different.

        After the merge call, the MR is re-fetched so the response
        carries `merged_at`, `merge_commit_sha`, and the final
        `state="merged"`.
        """
        if strategy == "rebase":
            raise ValueError(
                "GitLab does not support 'rebase' as a merge strategy. "
                "Use a separate rebase flow (PUT .../rebase) then call "
                "merge_pr(strategy='merge')."
            )
        if strategy not in ("merge", "squash"):
            raise ValueError(
                f"unsupported merge strategy {strategy!r} — accepted: "
                f"merge, squash"
            )
        path = _project_path(project)
        payload: dict[str, Any] = {}
        if strategy == "squash":
            payload["squash"] = True
        if commit_message:
            # GitLab uses `merge_commit_message` for merge, and
            # `squash_commit_message` for squash. Send the appropriate one.
            if strategy == "squash":
                payload["squash_commit_message"] = commit_message
            else:
                payload["merge_commit_message"] = commit_message
        with _client(project, token) as client:
            r = client.put(
                f"/projects/{path}/merge_requests/{pr_id}/merge", json=payload,
            )
            _check(r)
            # Re-fetch so the response captures the post-merge state
            # (merged_at, merge_commit_sha, state=merged). The merge
            # endpoint returns the MR, but mirror GitHub's pattern of
            # an explicit re-fetch so any server-side post-merge
            # mutations (e.g. webhook-driven label edits) are reflected.
            r2 = client.get(f"/projects/{path}/merge_requests/{pr_id}")
            _check(r2)
            return _map_mr(r2.json())

    # ---------- pipelines / CI runs ------------------------------------------

    def list_runs_for_branch(
        self,
        project: ProjectConfig,
        token: str | None,
        branch: str,
        limit: int = 20,
    ) -> list[PipelineRun]:
        return _list_pipelines(project, token, {"ref": branch}, limit)

    def list_runs_for_commit(
        self,
        project: ProjectConfig,
        token: str | None,
        sha: str,
        limit: int = 20,
    ) -> list[PipelineRun]:
        return _list_pipelines(project, token, {"sha": sha}, limit)

    def list_runs_for_tag(
        self,
        project: ProjectConfig,
        token: str | None,
        tag: str,
        limit: int = 20,
    ) -> list[PipelineRun]:
        """GitLab does not distinguish branch/tag refs in the pipelines
        query — both go through the `ref` parameter. We pass through
        and document the gap rather than synthesize a tag filter that
        the API doesn't support.
        """
        return _list_pipelines(project, token, {"ref": tag}, limit)

    def list_runs_for_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        limit: int = 20,
    ) -> list[PipelineRun]:
        """Issues do not trigger pipelines directly. Strategy:
        1. Fetch MRs linked to the issue (`.../issues/:iid/related_merge_requests`).
        2. For each MR, fetch its pipelines (`.../merge_requests/:iid/pipelines`).
        3. Concatenate, sort by created_at desc, cap at `limit`.

        Returns `[]` if no related MRs / no pipelines exist.
        """
        path = _project_path(project)
        per_page = min(max(1, limit), 100)
        with _client(project, token) as client:
            r = client.get(
                f"/projects/{path}/issues/{ticket_id}/related_merge_requests",
            )
            _check(r)
            related = r.json()
            collected: list[dict] = []
            for mr in related:
                mr_iid = mr.get("iid")
                if mr_iid is None:
                    continue
                pr = client.get(
                    f"/projects/{path}/merge_requests/{mr_iid}/pipelines",
                    params={"per_page": per_page},
                )
                if pr.is_success:
                    collected.extend(pr.json())
        # Sort newest first, mirror GitHub's default.
        collected.sort(
            key=lambda r: r.get("created_at", ""), reverse=True,
        )
        return [_map_pipeline_run(it) for it in collected[:per_page]]

    def get_run(
        self,
        project: ProjectConfig,
        token: str | None,
        run_id: str,
        *,
        include_failure_context: bool = False,
    ) -> PipelineRun:
        """Fetch a single pipeline.

        When `include_failure_context=True` and the pipeline concluded
        as failed, also fetch the failing jobs and a trace excerpt for
        each. GitLab does not expose GitHub-style annotations; the
        `annotations` field on each `FailingJob` is therefore `[]`.
        """
        path = _project_path(project)
        with _client(project, token) as client:
            r = client.get(f"/projects/{path}/pipelines/{run_id}")
            _check(r)
            run = _map_pipeline_run(r.json())
            if include_failure_context and run.conclusion == "failed":
                run.failure = _fetch_pipeline_failure(
                    client, project, run_id,
                )
        return run


__all__ = [
    "GitLabError",
    "GitLabProvider",
    "DEFAULT_BASE_URL",
    "USER_AGENT",
]
