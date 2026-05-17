"""GitHub provider — REST v3 implementation."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.markers import (
    AI_GENERATED_LABEL,
    AI_MODIFIED_LABEL,
    LABEL_COLORS,
    LABEL_DESCRIPTIONS,
    ensure_body_prefix,
    ensure_comment_prefix,
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
)

log = logging.getLogger("project-issues.github")

USER_AGENT = "claude-code-project-issues-plugin/0.1.0"
ACCEPT = "application/vnd.github+json"
API_BASE = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message


def _client(token: str | None) -> httpx.Client:
    headers = {
        "Accept": ACCEPT,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API_BASE, headers=headers, timeout=30.0)


def _check(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    try:
        payload = resp.json()
        msg = payload.get("message") or resp.reason_phrase
        errs = payload.get("errors")
        if errs:
            msg = f"{msg}: {errs}"
    except Exception:
        msg = resp.reason_phrase or "request failed"
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset", "?")
        msg = f"rate-limited (reset unix={reset}); {msg}"
    raise GitHubError(resp.status_code, msg)


def _repo_path(project: ProjectConfig) -> str:
    return f"/repos/{project.owner}/{project.repo}"


def _map_issue(raw: dict) -> Ticket:
    state = raw.get("state", "open")
    state_reason = raw.get("state_reason")
    # New encoding (see `Status` doc in providers/base.py): `open`,
    # `closed:completed`, or `closed:not_planned`. The `state_reason`
    # is preserved verbatim so the round-trip through
    # `list_ticket_statuses().hints` is lossless.
    if state == "open":
        status: Status = "open"
    elif state_reason == "not_planned":
        status = "closed:not_planned"
    else:
        # GitHub returns `state_reason="completed"` for "completed" and
        # null/unknown for legacy issues — both map to `closed:completed`.
        status = "closed:completed"
    return Ticket(
        id=str(raw["number"]),
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        status=status,
        author=(raw.get("user") or {}).get("login", ""),
        assignees=[a["login"] for a in (raw.get("assignees") or [])],
        labels=[lbl["name"] for lbl in (raw.get("labels") or [])],
        url=raw.get("html_url") or "",
        created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or "",
    )


def _map_comment(raw: dict) -> Comment:
    return Comment(
        id=str(raw["id"]),
        author=(raw.get("user") or {}).get("login", ""),
        body=raw.get("body") or "",
        url=raw.get("html_url") or "",
        created_at=raw.get("created_at") or "",
    )


def _map_pr(raw: dict) -> PullRequest:
    """Translate a GitHub pull-request payload into a `PullRequest`.

    Handles two payload shapes:
      - `GET /repos/{o}/{r}/pulls/{n}` — full PR object with `head`/`base`,
        `merged`, `mergeable`, `draft`, `requested_reviewers`, etc.
      - `GET /search/issues` items where `pull_request` is a small stub
        and the top-level fields look like an issue. For that shape the
        body/head/base/draft/merged fields aren't all present; we fall
        back to safe defaults so the dataclass is always constructable.
    """
    state = raw.get("state", "open")
    merged = bool(raw.get("merged") or raw.get("merged_at"))
    if state == "open":
        status: str = "open"
    elif merged:
        status = "merged"
    else:
        status = "closed"

    # Some payloads (notably `/search/issues` PR results) lack the full
    # `head` / `base` blocks — coerce to safe empty refs so the dataclass
    # is always populated with the documented keys.
    head_raw = raw.get("head") or {}
    base_raw = raw.get("base") or {}
    head_repo = (head_raw.get("repo") or {}) if head_raw else {}
    head = {
        "ref": head_raw.get("ref", "") if head_raw else "",
        "sha": head_raw.get("sha", "") if head_raw else "",
        "repo_full_name": head_repo.get("full_name", "") if head_repo else "",
    }
    base = {
        "ref": base_raw.get("ref", "") if base_raw else "",
        "sha": base_raw.get("sha", "") if base_raw else "",
    }

    number = raw.get("number") or 0
    return PullRequest(
        id=str(number),
        number=int(number),
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        status=status,  # type: ignore[arg-type]
        draft=bool(raw.get("draft", False)),
        author=(raw.get("user") or {}).get("login", ""),
        assignees=[a["login"] for a in (raw.get("assignees") or [])],
        reviewers=[],  # populated by a follow-up /reviews call when needed
        requested_reviewers=[
            r["login"] for r in (raw.get("requested_reviewers") or [])
        ],
        labels=[lbl["name"] for lbl in (raw.get("labels") or [])],
        head=head,
        base=base,
        merged=merged,
        mergeable=raw.get("mergeable"),  # may be None when GitHub hasn't computed
        url=raw.get("html_url") or "",
        created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or "",
    )


def _issue_state(raw: dict) -> str:
    """Translate a GitHub issue/PR payload to one of "open"/"closed"/"merged"/""."""
    state = raw.get("state")
    if state == "open":
        return "open"
    if state == "closed":
        # PRs report `merged` separately; if the source was a merged PR,
        # `merged_at` is non-null. Some endpoints also include `pull_request.merged_at`.
        pr_info = raw.get("pull_request") or {}
        if raw.get("merged_at") or pr_info.get("merged_at"):
            return "merged"
        return "closed"
    return ""


def _ref_for(issue_raw: dict, project: ProjectConfig) -> tuple[str, bool]:
    """Build a `ticket_id` string and detect whether the referenced item is a PR.

    Returns ("#N", is_pr) for same-repo refs and ("owner/repo#N", is_pr)
    for cross-repo refs. Falls back to URL parsing when `repository` is
    absent from the payload.
    """
    number = issue_raw.get("number")
    is_pr = bool(issue_raw.get("pull_request"))
    repo = issue_raw.get("repository") or {}
    full_name = repo.get("full_name")
    if not full_name:
        # Older payloads omit `repository`; derive from the html/api url.
        url = issue_raw.get("html_url") or issue_raw.get("url") or ""
        # html_url: https://github.com/owner/repo/(issues|pull)/N
        # api url:  https://api.github.com/repos/owner/repo/issues/N
        parts = url.replace("https://api.github.com/repos/", "").replace(
            "https://github.com/", ""
        ).split("/")
        if len(parts) >= 2:
            full_name = f"{parts[0]}/{parts[1]}"
    same_repo = full_name == f"{project.owner}/{project.repo}"
    if same_repo or not full_name:
        return f"#{number}", is_pr
    return f"{full_name}#{number}", is_pr


def _map_relation_from_sub_issue(raw: dict, project: ProjectConfig, kind: str) -> Relation:
    """Map a sub-issue (or the issue's own `parent` field) into a Relation."""
    ticket_id, is_pr = _ref_for(raw, project)
    return Relation(
        kind=kind,
        ticket_id=ticket_id,
        title=raw.get("title") or "",
        url=raw.get("html_url") or "",
        state=_issue_state(raw),
        is_pull_request=is_pr,
    )


def _map_relation_from_timeline(
    event: dict, project: ProjectConfig, *, self_id: str
) -> Relation | None:
    """Map a GitHub timeline event to a Relation, or None if not relevant.

    Handles three timeline event types:
      - cross-referenced (`mentions` / `mentioned_by`)
      - connected (`closes` / `closed_by`)
      - marked_as_duplicate (`duplicate_of` / `duplicated_by`)
    """
    etype = event.get("event")
    if etype == "cross-referenced":
        source = (event.get("source") or {}).get("issue")
        if not source:
            return None
        # The cross-reference direction: GitHub records the event on the
        # side that was mentioned; `source.issue` is the OTHER side that
        # did the mentioning. So from the current ticket's POV we were
        # `mentioned_by` source.
        return _map_relation_from_sub_issue(source, project, "mentioned_by")
    if etype == "connected" or etype == "disconnected":
        # `connected`: another issue/PR linked itself as closing this one.
        # The `source.issue` is the closer. Direction: we are `closed_by` it.
        source = (event.get("source") or {}).get("issue")
        if not source:
            return None
        if etype == "disconnected":
            # A previously-connected ref was removed; skip it.
            return None
        return _map_relation_from_sub_issue(source, project, "closed_by")
    if etype == "marked_as_duplicate":
        # Recorded on both sides. `source.issue` is the OTHER side; the
        # event itself doesn't disclose which side is canonical, so we
        # report `duplicate_of` from this ticket's POV when the source
        # is the canonical, and `duplicated_by` when the source is the dup.
        # The GitHub payload exposes `source.type` == "issue" plus
        # `event.actor` etc, but not the direction explicitly. Convention:
        # the side that was MARKED as duplicate gets `duplicate_of` →
        # source.issue. The canonical side gets `duplicated_by` → source.
        # The `event` payload's `source.issue.state` helps but isn't
        # reliable; we use the convention that the timeline of the
        # "duplicate" issue contains a marked_as_duplicate event whose
        # `dupe.issue` is THIS issue. GitHub returns either a `dupe` or
        # `canonical` field on the event itself.
        dupe = event.get("dupe") or {}
        canonical = event.get("canonical") or {}
        # Pull whichever side is NOT self.
        dupe_id = str(dupe.get("number", "")) if dupe else ""
        canonical_id = str(canonical.get("number", "")) if canonical else ""
        if canonical and canonical_id != self_id:
            return _map_relation_from_sub_issue(canonical, project, "duplicate_of")
        if dupe and dupe_id != self_id:
            return _map_relation_from_sub_issue(dupe, project, "duplicated_by")
        # Fall back to source.issue if `dupe`/`canonical` are absent.
        source = (event.get("source") or {}).get("issue")
        if source:
            return _map_relation_from_sub_issue(source, project, "duplicate_of")
        return None
    return None


def _has_next_link(link_header: str | None) -> bool:
    """Detect whether an HTTP `Link` header advertises a `rel="next"` page."""
    if not link_header:
        return False
    # The header is comma-separated; each entry looks like:
    #   <https://api.github.com/...?page=2>; rel="next"
    for part in link_header.split(","):
        if 'rel="next"' in part.replace("'", '"'):
            return True
    return False


def _fetch_relations(
    client: httpx.Client,
    project: ProjectConfig,
    ticket_id: str,
    issue_payload: dict,
) -> tuple[list[Relation], bool]:
    """Collect all relation links for a ticket. Returns (relations, truncated).

    `truncated` is True when the timeline response advertised a `rel="next"`
    page that we didn't follow (caller can re-query with pagination if
    they care about completeness).
    """
    relations: list[Relation] = []
    truncated = False

    # parent (from primary issue payload, if the sub-issue API is enabled)
    parent = issue_payload.get("parent")
    if parent:
        relations.append(
            _map_relation_from_sub_issue(parent, project, "parent")
        )

    # children via /sub_issues; 404/410 means feature not available on this host
    sub_r = client.get(
        f"{_repo_path(project)}/issues/{ticket_id}/sub_issues",
        params={"per_page": 100},
    )
    if sub_r.status_code in (404, 410):
        pass
    else:
        _check(sub_r)
        for sub in sub_r.json() or []:
            relations.append(
                _map_relation_from_sub_issue(sub, project, "child")
            )

    # timeline for cross-referenced / connected / marked_as_duplicate
    tl_r = client.get(
        f"{_repo_path(project)}/issues/{ticket_id}/timeline",
        params={"per_page": 100},
        headers={"Accept": "application/vnd.github+json"},
    )
    _check(tl_r)
    truncated = _has_next_link(tl_r.headers.get("Link"))
    for event in tl_r.json() or []:
        mapped = _map_relation_from_timeline(event, project, self_id=str(ticket_id))
        if mapped is not None:
            relations.append(mapped)

    return relations, truncated


def _ensure_label(client: httpx.Client, project: ProjectConfig, name: str) -> None:
    """Create the label on the target repo if it doesn't already exist.

    Hard-fails (raises `GitHubError`) when GitHub refuses the create call —
    most notably 403 for tokens without `push` permission on the target.
    The historical "log and continue" behaviour caused two production
    failure modes for `ai-generated` (see ticket
    Seretos/agent-marketplace#15): Mode A silent label-drop on the
    follow-up `POST /issues` (the label vanished from the response and
    the caller never knew) and Mode B hard 403 on the same POST when the
    label didn't yet exist on the target.

    Idempotent: 422 ("already_exists") is treated as success. Callers
    that can tolerate a missing label — e.g. `create_ticket` /
    `create_pr` / `update_ticket` / `update_pr`, all of which carry the
    marker in the body prefix as the canonical source of truth — should
    wrap this call in `_ensure_label_best_effort` so the operation
    proceeds without the label rather than aborting.
    """
    payload = {
        "name": name,
        "color": LABEL_COLORS.get(name, "ededed"),
        "description": LABEL_DESCRIPTIONS.get(name, ""),
    }
    resp = client.post(f"{_repo_path(project)}/labels", json=payload)
    if resp.status_code in (200, 201):
        return
    if resp.status_code == 422:
        return  # already exists
    _check(resp)


def _ensure_label_best_effort(
    client: httpx.Client, project: ProjectConfig, name: str
) -> bool:
    """Best-effort wrapper around `_ensure_label`.

    Returns True when the label is known to exist on the repo (created
    or already-present), False when the repo refused creation (typically
    403 for tokens that lack `push`). The False case lets the caller
    drop the label from the subsequent POST payload so the issue / PR is
    still created — the body-prefix marker is the canonical source of
    truth and survives a missing label.
    """
    try:
        _ensure_label(client, project, name)
        return True
    except GitHubError as exc:
        log.warning(
            "could not ensure label '%s' on %s/%s: %s; falling back to "
            "body-prefix marker only",
            name, project.owner, project.repo, exc,
        )
        return False


def _label_present(payload: dict, name: str) -> bool:
    """True iff GitHub's response payload includes a label named `name`.

    GitHub silently drops labels from `POST /issues` when the caller has
    no `triage` (Mode A in ticket #15): the request succeeds with 201 but
    the resulting `labels` array is empty. This helper lets callers detect
    that case after the fact and warn — the body-prefix marker still gives
    machine-grep-able attribution, but the label is gone.
    """
    labels = payload.get("labels") or []
    for entry in labels:
        if isinstance(entry, str) and entry == name:
            return True
        if isinstance(entry, dict) and entry.get("name") == name:
            return True
    return False


def _quote_label(name: str) -> str:
    """Wrap a label name in double quotes for the Search qualifier if it
    contains whitespace; otherwise return as-is. The Search API treats
    `label:"foo bar"` as one qualifier with a space-bearing value.
    """
    if any(ch.isspace() for ch in name):
        return f'"{name}"'
    return name


def _requires_search(filters: TicketFilters) -> bool:
    """Return True iff any filter forces us off the cheap `/issues` path
    and onto `/search/issues`.

    `search` (free-text) ALSO requires the search endpoint, but the
    legacy code-path already handled that. This helper specifically
    captures the NEW filters added in Plan 7.
    """
    if filters.not_labels:
        return True
    if filters.author:
        return True
    if filters.created_after or filters.created_before:
        return True
    if filters.updated_after or filters.updated_before:
        return True
    return False


def _list_via_search(
    client: httpx.Client,
    project: ProjectConfig,
    filters: TicketFilters,
) -> list[dict]:
    """Hit `GET /search/issues` and return the raw `items` list.

    Builds a `q=` string that mirrors the legacy `/issues` semantics plus
    the new Plan-7 filters (`not_labels`, `author`, date ranges). Sort is
    expressed as a `sort:<key>-<order>` qualifier appended to `q` (NOT a
    separate `sort=` param — that's the legacy endpoint's convention).
    """
    per_page = min(max(1, filters.limit), 100)
    qual_parts: list[str] = [
        "is:issue",
        f"repo:{project.owner}/{project.repo}",
    ]
    # state qualifier: search supports `open`/`closed` only — omit for "any".
    if filters.status in ("open", "closed"):
        qual_parts.append(f"state:{filters.status}")
    if filters.assignee:
        qual_parts.append(f"assignee:{filters.assignee}")
    if filters.author:
        qual_parts.append(f"author:{filters.author}")
    for lbl in filters.labels:
        qual_parts.append(f"label:{_quote_label(lbl)}")
    for lbl in filters.not_labels:
        qual_parts.append(f"-label:{_quote_label(lbl)}")
    if filters.created_after:
        qual_parts.append(f"created:>={filters.created_after}")
    if filters.created_before:
        qual_parts.append(f"created:<={filters.created_before}")
    if filters.updated_after:
        qual_parts.append(f"updated:>={filters.updated_after}")
    if filters.updated_before:
        qual_parts.append(f"updated:<={filters.updated_before}")
    qual_parts.append(f"sort:{filters.sort_by}-{filters.sort_order}")
    pieces = [filters.search] if filters.search else []
    pieces.extend(qual_parts)
    q = " ".join(pieces)
    r = client.get("/search/issues", params={"q": q, "per_page": per_page})
    _check(r)
    return r.json().get("items", [])


def _map_permissions_to_capabilities(perms: dict) -> TokenCapabilities:
    """Translate the `permissions` block GitHub returns on `GET /repos`
    into a `TokenCapabilities`.

    GitHub's permission ladder (low -> high): `pull`, `triage`, `push`,
    `maintain`, `admin`. The mapping:

    - `pull`     -> read only (no write flags).
    - `triage+`  -> `issues.modify` (label/assign/state on existing issues).
    - `push+`    -> `issues.create`, `pulls.create`, `pulls.modify`.
    - `maintain+` -> `pulls.merge` (matches GitHub's branch-protection
      semantics where merge requires maintain-equivalent rights).
    - `admin`    -> everything.

    `pull` alone never grants any write capability.
    """
    admin = bool(perms.get("admin"))
    maintain = bool(perms.get("maintain")) or admin
    push = bool(perms.get("push")) or maintain
    triage = bool(perms.get("triage")) or push
    # `pull` is the read flag; not needed for any of the write bits
    # because triage/push/maintain/admin all imply read.
    return TokenCapabilities(
        issues_create=push,
        issues_modify=triage,
        pulls_create=push,
        pulls_modify=push,
        pulls_merge=maintain,
        reason=None,
    )


class GitHubProvider:
    def probe_token_capabilities(
        self, project: ProjectConfig, token: str
    ) -> TokenCapabilities:
        """Probe `GET /repos/{owner}/{repo}` to learn what `token` may
        do against `project`.

        Failure modes are returned (not raised) so the caller can pass
        the result through to `_project_to_dict` unconditionally. See
        `TokenCapabilities.reason` for the stable failure identifiers.
        """
        try:
            with _client(token) as client:
                r = client.get(_repo_path(project))
        except httpx.HTTPError:
            return TokenCapabilities(reason="network_error")
        if r.status_code == 401:
            return TokenCapabilities(reason="bad_credentials")
        if r.status_code == 404:
            return TokenCapabilities(reason="repo_invisible_to_token")
        if not r.is_success:
            # Treat other unexpected statuses the same way as a missing
            # field: don't grant any write capability, but record what
            # happened so a caller can debug.
            return TokenCapabilities(
                reason=f"http_{r.status_code}"
            )
        try:
            body = r.json()
        except ValueError:
            return TokenCapabilities(reason="permissions_field_missing")
        perms = body.get("permissions") if isinstance(body, dict) else None
        if not isinstance(perms, dict):
            return TokenCapabilities(reason="permissions_field_missing")
        return _map_permissions_to_capabilities(perms)

    def list_tickets(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> list[Ticket]:
        per_page = min(max(1, filters.limit), 100)
        # Normalize `not_labels=[]` (truthy-but-empty containers) to "not set".
        if not filters.not_labels:
            filters.not_labels = []
        with _client(token) as client:
            if filters.search or _requires_search(filters):
                items = _list_via_search(client, project, filters)
            else:
                params: dict[str, Any] = {
                    "per_page": per_page,
                    "state": filters.status if filters.status in ("open", "closed") else "all",
                    "sort": filters.sort_by,
                    "direction": filters.sort_order,
                }
                if filters.labels:
                    params["labels"] = ",".join(filters.labels)
                if filters.assignee:
                    params["assignee"] = filters.assignee
                r = client.get(f"{_repo_path(project)}/issues", params=params)
                _check(r)
                items = r.json()
            # The /issues endpoints include PRs; filter them out.
            return [_map_issue(it) for it in items if "pull_request" not in it]

    def get_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        *,
        include_relations: bool = True,
    ) -> tuple[Ticket, list[Comment], list[Relation], bool]:
        """Fetch a single ticket with its comments and (optionally) relations.

        Returns `(ticket, comments, relations, relations_truncated)`.
        When `include_relations` is False, returns `([], False)` for the
        relation fields and skips the extra API calls.
        """
        with _client(token) as client:
            r = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
            _check(r)
            issue_raw = r.json()
            ticket = _map_issue(issue_raw)
            c = client.get(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                params={"per_page": 100},
            )
            _check(c)
            comments = [_map_comment(it) for it in c.json()]
            if include_relations:
                relations, truncated = _fetch_relations(
                    client, project, ticket_id, issue_raw
                )
            else:
                relations, truncated = [], False
        return ticket, comments, relations, truncated

    def create_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        title: str,
        body: str,
        labels: list[str],
        assignees: list[str],
    ) -> Ticket:
        """Create an issue with the `ai-generated` AI-attribution marker.

        Marker policy (ticket Seretos/agent-marketplace#15):
          - Body prefix `#ai-generated\\n\\n` is the canonical source of
            truth and is always applied (idempotent).
          - The `ai-generated` LABEL is best-effort. If the caller cannot
            create or apply the label (typically tokens without
            `push` / `triage` on the target repo), the label is dropped
            from the POST payload so the issue is still created. Mode A
            (silent label-drop in the response despite a successful POST)
            is detected after the call and logged.
        """
        # Deduplicate while preserving order, ensure ai-generated is present.
        merged = list(dict.fromkeys([*labels, AI_GENERATED_LABEL]))
        prefixed_body = ensure_body_prefix(body)
        with _client(token) as client:
            label_ok = _ensure_label_best_effort(
                client, project, AI_GENERATED_LABEL
            )
            payload: dict[str, Any] = {
                "title": title,
                "body": prefixed_body,
            }
            if label_ok:
                payload["labels"] = merged
            else:
                # Drop only the AI marker; keep any caller-supplied labels
                # so an unrelated `bug` / `enhancement` label still lands.
                other = [lbl for lbl in merged if lbl != AI_GENERATED_LABEL]
                if other:
                    payload["labels"] = other
            if assignees:
                payload["assignees"] = assignees
            r = client.post(f"{_repo_path(project)}/issues", json=payload)
            _check(r)
            raw = r.json()
            if label_ok and not _label_present(raw, AI_GENERATED_LABEL):
                log.warning(
                    "ticket #%s created on %s/%s without '%s' label "
                    "(GitHub silently dropped it — caller likely lacks "
                    "triage permission); body-prefix marker remains",
                    raw.get("number"), project.owner, project.repo,
                    AI_GENERATED_LABEL,
                )
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
        with _client(token) as client:
            r0 = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
            _check(r0)
            current = r0.json()
            current_labels = {lbl["name"] for lbl in (current.get("labels") or [])}
            current_assignees = {a["login"] for a in (current.get("assignees") or [])}

            new_labels = set(current_labels)
            if labels_add:
                new_labels.update(labels_add)
            if labels_remove:
                new_labels.difference_update(labels_remove)

            # If this ticket wasn't created by us, mark it as AI-modified.
            # Label application is best-effort (see ticket #15): if the
            # caller can't create or apply the label, proceed without it
            # rather than blocking the legitimate update. The body is
            # passed through unchanged on `update_ticket` because the
            # caller has explicit edit intent — we don't re-stamp an
            # already-existing body with a marker on every PATCH.
            if AI_GENERATED_LABEL not in current_labels:
                if AI_MODIFIED_LABEL not in new_labels:
                    if _ensure_label_best_effort(
                        client, project, AI_MODIFIED_LABEL
                    ):
                        new_labels.add(AI_MODIFIED_LABEL)
                else:
                    new_labels.add(AI_MODIFIED_LABEL)

            new_assignees = set(current_assignees)
            if assignees_add:
                new_assignees.update(assignees_add)
            if assignees_remove:
                new_assignees.difference_update(assignees_remove)

            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if body is not None:
                payload["body"] = body
            if status is not None:
                # New provider-native string API (see ticket #7).
                # Accepted GitHub values:
                #   - "open"
                #   - "closed"                  (same as "closed:completed")
                #   - "closed:completed"
                #   - "closed:not_planned"
                # The legacy 3-value enum is no longer accepted — agents
                # must call `list_ticket_statuses` to discover valid
                # values and use the `hints` to choose a target.
                if status == "open":
                    payload["state"] = "open"
                elif status in ("closed", "closed:completed"):
                    payload["state"] = "closed"
                    payload["state_reason"] = "completed"
                elif status == "closed:not_planned":
                    payload["state"] = "closed"
                    payload["state_reason"] = "not_planned"
                else:
                    raise ValueError(
                        f"unsupported status {status!r} for GitHub — "
                        f"use list_ticket_statuses to discover valid "
                        f"values. Accepted: open, closed, "
                        f"closed:completed, closed:not_planned."
                    )
            if new_labels != current_labels:
                payload["labels"] = sorted(new_labels)
            if new_assignees != current_assignees:
                payload["assignees"] = sorted(new_assignees)

            if not payload:
                # Nothing to do — return the current state.
                return _map_issue(current)

            r = client.patch(f"{_repo_path(project)}/issues/{ticket_id}", json=payload)
            _check(r)
            return _map_issue(r.json())

    def list_statuses(
        self,
        project: ProjectConfig,  # noqa: ARG002 — kept for provider-agnostic signature
        token: str | None,        # noqa: ARG002 — same
    ) -> StatusSpec:
        """Return the GitHub-static status spec.

        GitHub's state-space is fixed (`open` / `closed`) but we expose
        the `state_reason` distinction through suffix-encoded values so
        agents can choose between "done as planned" vs "not planned"
        terminal states. The state-space is identical for every GitHub
        project, so this is a static return value — no API call.
        """
        return StatusSpec(
            values=["open", "closed:completed", "closed:not_planned"],
            transitions={
                "open": ["closed:completed", "closed:not_planned"],
                "closed:completed": ["open"],
                "closed:not_planned": ["open"],
            },
            hints={
                "default_open": "open",
                "terminal": ["closed:completed", "closed:not_planned"],
                "terminal_completed": "closed:completed",
                "terminal_declined": "closed:not_planned",
            },
        )

    def add_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        body: str,
    ) -> Comment:
        prefixed = ensure_comment_prefix(body)
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                json={"body": prefixed},
            )
            _check(r)
            return _map_comment(r.json())

    def list_comments(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        limit: int = 30,
    ) -> list[Comment]:
        """List comments on a ticket (most recent up to `limit`, capped at 100)."""
        per_page = min(max(1, limit), 100)
        with _client(token) as client:
            r = client.get(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                params={"per_page": per_page},
            )
            _check(r)
            return [_map_comment(it) for it in r.json()]

    def get_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
    ) -> Comment:
        """Fetch a single comment by its repo-wide comment id."""
        with _client(token) as client:
            r = client.get(
                f"{_repo_path(project)}/issues/comments/{comment_id}",
            )
            _check(r)
            return _map_comment(r.json())

    def update_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        comment_id: str,
        body: str,
    ) -> Comment:
        """Update a comment's body. Always re-applies the AI-marker prefix.

        Behavior chosen here (matches the plan's "conservative" option):
        any update we issue runs the new body through `ensure_comment_prefix`,
        so any AI edit is unambiguously labelled. If the existing comment
        body didn't carry an `#ai-generated` marker, the prefix is added —
        this mirrors `update_ticket`, which adds the `ai-modified` label when
        the ticket wasn't originally AI-created. (A future variant could emit
        a distinct `#ai-modified` prefix; we keep the single `#ai-generated`
        marker for now to avoid introducing a new convention mid-plan.)
        """
        prefixed = ensure_comment_prefix(body)
        with _client(token) as client:
            r = client.patch(
                f"{_repo_path(project)}/issues/comments/{comment_id}",
                json={"body": prefixed},
            )
            _check(r)
            return _map_comment(r.json())

    # ---------- pull requests ------------------------------------------------

    def list_prs(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: PRFilters,
    ) -> list[PullRequest]:
        """List pull requests for a project.

        Routing mirrors `list_tickets`: when `labels`, `assignee`, or
        `search` are set, switch from the cheap `/pulls` endpoint to
        `/search/issues` with `is:pr` so the additional filters can be
        expressed as Search qualifiers. The `head`/`base` filters work on
        both paths (Search via the `head:`/`base:` qualifiers).
        """
        per_page = min(max(1, filters.limit), 100)
        use_search = bool(
            filters.labels or filters.assignee or filters.search
        )
        with _client(token) as client:
            if use_search:
                qual_parts: list[str] = [
                    "is:pr",
                    f"repo:{project.owner}/{project.repo}",
                ]
                if filters.status in ("open", "closed"):
                    qual_parts.append(f"state:{filters.status}")
                if filters.assignee:
                    qual_parts.append(f"assignee:{filters.assignee}")
                for lbl in filters.labels:
                    qual_parts.append(f"label:{_quote_label(lbl)}")
                if filters.head:
                    qual_parts.append(f"head:{filters.head}")
                if filters.base:
                    qual_parts.append(f"base:{filters.base}")
                pieces = [filters.search] if filters.search else []
                pieces.extend(qual_parts)
                q = " ".join(pieces)
                r = client.get("/search/issues", params={"q": q, "per_page": per_page})
                _check(r)
                items = r.json().get("items", [])
            else:
                params: dict[str, Any] = {
                    "per_page": per_page,
                    "state": (
                        filters.status if filters.status in ("open", "closed") else "all"
                    ),
                    "sort": "created",
                    "direction": "desc",
                }
                if filters.head:
                    params["head"] = filters.head
                if filters.base:
                    params["base"] = filters.base
                r = client.get(f"{_repo_path(project)}/pulls", params=params)
                _check(r)
                items = r.json()
        return [_map_pr(it) for it in items]

    def get_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
    ) -> tuple[PullRequest, list[Comment]]:
        """Fetch a single PR plus its issue-style comments.

        Returns `(pr, comments)`. Code-review threads live on a different
        endpoint (`/pulls/{n}/comments`) and aren't merged in here — the
        plan scopes PR comments to the issue-shared `/issues/{n}/comments`
        endpoint, which is what `add_pr_comment` posts to.
        """
        with _client(token) as client:
            r = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
            _check(r)
            pr = _map_pr(r.json())
            c = client.get(
                f"{_repo_path(project)}/issues/{pr_id}/comments",
                params={"per_page": 100},
            )
            _check(c)
            comments = [_map_comment(it) for it in c.json()]
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
        """Create a pull request, applying the AI-generated marker.

        Marker policy mirrors `create_ticket` (see ticket
        Seretos/agent-marketplace#15): body prefix is the canonical
        source of truth; the `ai-generated` LABEL is best-effort. When
        the caller lacks permission to create or apply the label, the
        PR is still created and the follow-up labels POST is skipped (or
        restricted to caller-supplied labels). Mode A silent-drop on the
        labels POST is detected and logged.

        Labels and assignees are applied in follow-up
        `POST /issues/{n}/...` calls because the `POST /pulls` endpoint
        doesn't accept them inline.
        """
        merged_labels = list(dict.fromkeys([*(labels or []), AI_GENERATED_LABEL]))
        prefixed_body = ensure_body_prefix(body)
        with _client(token) as client:
            label_ok = _ensure_label_best_effort(
                client, project, AI_GENERATED_LABEL
            )
            payload: dict[str, Any] = {
                "title": title,
                "body": prefixed_body,
                "head": head,
                "base": base,
                "draft": draft,
            }
            r = client.post(f"{_repo_path(project)}/pulls", json=payload)
            _check(r)
            pr_raw = r.json()
            pr_number = pr_raw["number"]
            # Apply labels via the issues endpoint (PRs share it). Skip
            # the call entirely when there's nothing to apply — including
            # the case where `ai-generated` couldn't be ensured and the
            # caller didn't supply any other labels.
            labels_to_apply = (
                merged_labels
                if label_ok
                else [lbl for lbl in merged_labels if lbl != AI_GENERATED_LABEL]
            )
            if labels_to_apply:
                lbl_resp = client.post(
                    f"{_repo_path(project)}/issues/{pr_number}/labels",
                    json={"labels": labels_to_apply},
                )
                _check(lbl_resp)
                applied_raw = lbl_resp.json()
                # Reflect the new labels back into the PR payload so the
                # returned dataclass advertises them.
                pr_raw["labels"] = applied_raw
                if label_ok and not _label_present(
                    {"labels": applied_raw}, AI_GENERATED_LABEL
                ):
                    log.warning(
                        "PR #%s created on %s/%s without '%s' label "
                        "(GitHub silently dropped it — caller likely "
                        "lacks triage permission); body-prefix marker "
                        "remains",
                        pr_number, project.owner, project.repo,
                        AI_GENERATED_LABEL,
                    )
            if assignees:
                a_resp = client.post(
                    f"{_repo_path(project)}/issues/{pr_number}/assignees",
                    json={"assignees": assignees},
                )
                _check(a_resp)
                # The /assignees endpoint returns the issue payload with
                # the updated assignee list; mirror it.
                pr_raw["assignees"] = a_resp.json().get("assignees") or []
            return _map_pr(pr_raw)

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
        """Update a PR's title/body/state/base, plus label/assignee deltas.

        `status` accepts `"open"` or `"closed"` only. To merge a PR call
        `merge_pr` — `status="merged"` is rejected by the tool layer.

        Applies the `ai-modified` label (mirroring `update_ticket`) when
        the PR wasn't originally created by us.
        """
        with _client(token) as client:
            r0 = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
            _check(r0)
            current = r0.json()
            current_labels = {lbl["name"] for lbl in (current.get("labels") or [])}
            current_assignees = {a["login"] for a in (current.get("assignees") or [])}

            new_labels = set(current_labels)
            if labels_add:
                new_labels.update(labels_add)
            if labels_remove:
                new_labels.difference_update(labels_remove)
            # `ai-modified` is best-effort (see ticket #15): if we can't
            # ensure the label exists, proceed without it rather than
            # failing the legitimate update.
            if AI_GENERATED_LABEL not in current_labels:
                if AI_MODIFIED_LABEL not in new_labels:
                    if _ensure_label_best_effort(
                        client, project, AI_MODIFIED_LABEL
                    ):
                        new_labels.add(AI_MODIFIED_LABEL)
                else:
                    new_labels.add(AI_MODIFIED_LABEL)

            new_assignees = set(current_assignees)
            if assignees_add:
                new_assignees.update(assignees_add)
            if assignees_remove:
                new_assignees.difference_update(assignees_remove)

            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if body is not None:
                payload["body"] = body
            if status is not None:
                # The tool layer already restricts `status` to open/closed;
                # accept those here and ignore anything else (the layer
                # raised before us when the value was "merged").
                if status in ("open", "closed"):
                    payload["state"] = status
            if base is not None:
                payload["base"] = base

            # PATCH /pulls only takes pull-request scoped fields; labels
            # and assignees are managed via the issues endpoint.
            if payload:
                pr_resp = client.patch(
                    f"{_repo_path(project)}/pulls/{pr_id}", json=payload
                )
                _check(pr_resp)
                current = pr_resp.json()

            if new_labels != current_labels:
                lbl_resp = client.put(
                    f"{_repo_path(project)}/issues/{pr_id}/labels",
                    json={"labels": sorted(new_labels)},
                )
                _check(lbl_resp)
                current["labels"] = lbl_resp.json()

            if new_assignees != current_assignees:
                # `assignees_add`/`remove` map to two separate endpoints.
                to_add = new_assignees - current_assignees
                to_remove = current_assignees - new_assignees
                if to_add:
                    a_resp = client.post(
                        f"{_repo_path(project)}/issues/{pr_id}/assignees",
                        json={"assignees": sorted(to_add)},
                    )
                    _check(a_resp)
                if to_remove:
                    a_resp = client.request(
                        "DELETE",
                        f"{_repo_path(project)}/issues/{pr_id}/assignees",
                        json={"assignees": sorted(to_remove)},
                    )
                    _check(a_resp)
                # Re-fetch so the returned PR reflects the final state.
                r_final = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
                _check(r_final)
                current = r_final.json()

            return _map_pr(current)

    def add_pr_comment(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        body: str,
    ) -> Comment:
        """Add a discussion comment to a PR (NOT a code-review comment).

        Uses the shared `/issues/{n}/comments` endpoint; the AI-marker
        prefix is applied via `ensure_comment_prefix`.
        """
        prefixed = ensure_comment_prefix(body)
        with _client(token) as client:
            r = client.post(
                f"{_repo_path(project)}/issues/{pr_id}/comments",
                json={"body": prefixed},
            )
            _check(r)
            return _map_comment(r.json())

    def merge_pr(
        self,
        project: ProjectConfig,
        token: str | None,
        pr_id: str,
        merge_method: str = "merge",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> PullRequest:
        """Merge a PR. `merge_method` is one of "merge", "squash", "rebase".

        Translates the GitHub merge-not-allowed 405 into a `GitHubError`
        via `_check`. After the merge succeeds, re-fetches the PR so the
        returned dataclass advertises the merged state.
        """
        if merge_method not in ("merge", "squash", "rebase"):
            raise GitHubError(400, f"invalid merge_method '{merge_method}'")
        payload: dict[str, Any] = {"merge_method": merge_method}
        if commit_title is not None:
            payload["commit_title"] = commit_title
        if commit_message is not None:
            payload["commit_message"] = commit_message
        with _client(token) as client:
            r = client.put(
                f"{_repo_path(project)}/pulls/{pr_id}/merge", json=payload
            )
            _check(r)
            # Re-fetch so the response carries the merged state/timestamp.
            r2 = client.get(f"{_repo_path(project)}/pulls/{pr_id}")
            _check(r2)
            return _map_pr(r2.json())

    # ---------- pipelines / CI runs -----------------------------------------

    def list_runs_for_branch(
        self,
        project: ProjectConfig,
        token: str | None,
        branch: str,
        status: str = "all",
        limit: int = 10,
    ) -> list[PipelineRun]:
        """List Actions workflow runs filtered by branch."""
        with _client(token) as client:
            runs = _list_runs_for_branch(client, project, branch, status, limit)
            return [_map_run(r) for r in runs]

    def list_runs_for_commit(
        self,
        project: ProjectConfig,
        token: str | None,
        sha: str,
        status: str = "all",
        limit: int = 10,
    ) -> list[PipelineRun]:
        """List runs whose `head_sha` matches `sha`."""
        with _client(token) as client:
            runs = _list_runs_for_commit(client, project, sha, status, limit)
            return [_map_run(r) for r in runs]

    def list_runs_for_tag(
        self,
        project: ProjectConfig,
        token: str | None,
        tag: str,
        status: str = "all",
        limit: int = 10,
    ) -> tuple[list[PipelineRun], list[str]]:
        """Resolve `tag` -> commit SHA -> runs filtered by head_sha.

        Returns `(runs, resolved_refs)` where `resolved_refs` lists the
        single SHA we resolved to (handy for telling the caller which
        commit was actually queried).
        """
        with _client(token) as client:
            sha = _resolve_tag_sha(client, project, tag)
            if not sha:
                return [], []
            runs = _list_runs_for_commit(client, project, sha, status, limit)
            return [_map_run(r) for r in runs], [sha]

    def list_runs_for_ticket(
        self,
        project: ProjectConfig,
        token: str | None,
        ticket_id: str,
        status: str = "all",
        limit: int = 10,
    ) -> tuple[list[PipelineRun], list[str]]:
        """Resolve a ticket -> linked PR head_shas -> runs.

        Returns `(runs, resolved_refs)`. `resolved_refs` is the de-duped
        list of head_shas we queried. When the ticket has no linked PR
        or branch reference, both lists are empty (the tool layer turns
        this into a `hint`).
        """
        with _client(token) as client:
            shas = _resolved_refs_for_ticket(client, project, ticket_id)
            if not shas:
                return [], []
            # Aggregate by run id so multiple SHAs that share a run don't
            # produce duplicates.
            by_id: dict[str, dict] = {}
            for sha in shas:
                for r in _list_runs_for_commit(
                    client, project, sha, status, limit
                ):
                    rid = str(r.get("id", ""))
                    if rid and rid not in by_id:
                        by_id[rid] = r
            # Sort by created_at desc and cap to `limit`.
            raws = sorted(
                by_id.values(),
                key=lambda r: r.get("created_at", ""),
                reverse=True,
            )[:limit]
            return [_map_run(r) for r in raws], shas

    def get_run(
        self,
        project: ProjectConfig,
        token: str | None,
        run_id: str,
        include_failure_excerpt: bool = True,
    ) -> PipelineRun:
        """Fetch a single workflow run, optionally with failure context.

        When `include_failure_excerpt` is True AND the run concluded as
        failed, populates `run.failure` with per-failing-job annotations
        and a small log excerpt. In-progress runs (`conclusion=None`)
        never trigger the failure-context fetch.
        """
        with _client(token) as client:
            r = client.get(
                f"{_repo_path(project)}/actions/runs/{run_id}"
            )
            _check(r)
            raw = r.json()
            run = _map_run(raw)
            if (
                include_failure_excerpt
                and run.conclusion == "failure"
                and run.status == "completed"
            ):
                run.failure = _get_failure_excerpt(client, project, token, run_id)
            return run


# ---------- pipeline helpers (module-level so providers can reuse) ----------


_RUN_STATUS_FILTERS = {"queued", "in_progress", "completed"}


def _map_run(raw: dict) -> PipelineRun:
    """Translate a GitHub `workflow_run` payload into a `PipelineRun`."""
    return PipelineRun(
        id=str(raw.get("id", "")),
        name=raw.get("name") or raw.get("display_title") or "",
        branch=raw.get("head_branch") or "",
        head_sha=raw.get("head_sha") or "",
        event=raw.get("event") or "",
        status=raw.get("status") or "",
        conclusion=raw.get("conclusion"),
        url=raw.get("html_url") or "",
        created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or "",
        run_attempt=int(raw.get("run_attempt") or 1),
    )


def _runs_params(status: str, limit: int) -> dict[str, Any]:
    per_page = min(max(1, limit), 100)
    params: dict[str, Any] = {"per_page": per_page}
    if status and status != "all" and status in _RUN_STATUS_FILTERS:
        params["status"] = status
    return params


def _list_runs_for_branch(
    client: httpx.Client,
    project: ProjectConfig,
    branch: str,
    status: str,
    limit: int,
) -> list[dict]:
    params = _runs_params(status, limit)
    params["branch"] = branch
    r = client.get(f"{_repo_path(project)}/actions/runs", params=params)
    _check(r)
    return (r.json() or {}).get("workflow_runs", [])


def _list_runs_for_commit(
    client: httpx.Client,
    project: ProjectConfig,
    sha: str,
    status: str,
    limit: int,
) -> list[dict]:
    params = _runs_params(status, limit)
    params["head_sha"] = sha
    r = client.get(f"{_repo_path(project)}/actions/runs", params=params)
    _check(r)
    return (r.json() or {}).get("workflow_runs", [])


def _resolve_tag_sha(
    client: httpx.Client,
    project: ProjectConfig,
    tag: str,
) -> str | None:
    """Resolve a tag name to a commit SHA.

    Annotated tags point at a `tag` object whose `object.sha` is the
    commit; lightweight tags point directly at the commit. Both shapes
    use `object.sha` on the ref response — GitHub doesn't dereference
    annotated tags through this endpoint, so for annotated tags we
    follow the second hop via `/git/tags/{sha}`.
    """
    r = client.get(f"{_repo_path(project)}/git/refs/tags/{tag}")
    if r.status_code in (404, 422):
        return None
    _check(r)
    obj = (r.json() or {}).get("object") or {}
    sha = obj.get("sha")
    if not sha:
        return None
    if obj.get("type") == "tag":
        # Annotated tag — follow the second hop.
        r2 = client.get(f"{_repo_path(project)}/git/tags/{sha}")
        if r2.status_code in (404, 422):
            return sha
        _check(r2)
        inner = (r2.json() or {}).get("object") or {}
        return inner.get("sha") or sha
    return sha


_BRANCH_HINT_RE = None  # set lazily below


def _resolved_refs_for_ticket(
    client: httpx.Client,
    project: ProjectConfig,
    ticket_id: str,
) -> list[str]:
    """Collect unique head_shas from PRs linked to a ticket.

    Sources (deduped by SHA, in discovery order):
      1. Timeline `cross-referenced` events whose source is a PR — we
         fetch the PR to read its `head.sha`.
      2. `search/issues` for PRs in this repo whose body mentions the
         ticket number — same: fetch each PR's head.sha.
      3. Best-effort `branch:foo` regex in the ticket body — resolve
         the branch ref to a SHA.

    The timeline `source.issue` object only includes a marker
    (`pull_request`) that signals "this is a PR", NOT the head_sha —
    the SHA always requires the PR fetch.
    """
    global _BRANCH_HINT_RE
    if _BRANCH_HINT_RE is None:
        import re
        _BRANCH_HINT_RE = re.compile(
            r"(?:^|\s)branch:\s*([A-Za-z0-9._\-/]+)",
            re.IGNORECASE,
        )

    seen: set[str] = set()
    out: list[str] = []

    # Fetch the ticket once to read its body (for the `branch:foo` hint)
    # and to bail early on a 404.
    issue_r = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
    if issue_r.status_code == 404:
        return []
    _check(issue_r)
    issue_body = (issue_r.json() or {}).get("body") or ""

    # Linked PR numbers we should fetch for their head.sha.
    pr_numbers: list[int] = []

    # (1) Timeline cross-references that point at PRs.
    tl = client.get(
        f"{_repo_path(project)}/issues/{ticket_id}/timeline",
        params={"per_page": 100},
        headers={"Accept": "application/vnd.github+json"},
    )
    _check(tl)
    for event in tl.json() or []:
        if event.get("event") not in ("cross-referenced", "connected"):
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if not source.get("pull_request"):
            continue
        # Same-repo only — cross-repo PRs would need a per-repo fetch,
        # which falls outside the scope of this plan.
        full = ((source.get("repository") or {}).get("full_name")) or ""
        url = source.get("html_url") or source.get("url") or ""
        same_repo = (
            full == f"{project.owner}/{project.repo}"
            or f"/{project.owner}/{project.repo}/" in url
            or f"/repos/{project.owner}/{project.repo}/" in url
        )
        if not same_repo:
            continue
        n = source.get("number")
        if isinstance(n, int):
            pr_numbers.append(n)

    # (2) `search/issues` for PRs in this repo that mention the ticket.
    try:
        ticket_n = int(ticket_id)
    except (TypeError, ValueError):
        ticket_n = None
    if ticket_n is not None:
        q = f"is:pr repo:{project.owner}/{project.repo} {ticket_n} in:body"
        sr = client.get("/search/issues", params={"q": q, "per_page": 30})
        # Search may rate-limit (403) — degrade silently.
        if sr.is_success:
            for it in (sr.json() or {}).get("items", []) or []:
                n = it.get("number")
                if isinstance(n, int) and n != ticket_n:
                    pr_numbers.append(n)

    # Dedup PR numbers, then fetch each to read head.sha.
    seen_pr: set[int] = set()
    for n in pr_numbers:
        if n in seen_pr:
            continue
        seen_pr.add(n)
        pr_r = client.get(f"{_repo_path(project)}/pulls/{n}")
        if pr_r.status_code == 404:
            continue
        if not pr_r.is_success:
            # Skip individual fetch failures rather than aborting.
            continue
        head = (pr_r.json() or {}).get("head") or {}
        sha = head.get("sha")
        if sha and sha not in seen:
            seen.add(sha)
            out.append(sha)

    # (3) `branch:foo` hint in the ticket body.
    m = _BRANCH_HINT_RE.search(issue_body) if issue_body else None
    if m:
        branch = m.group(1)
        ref_r = client.get(
            f"{_repo_path(project)}/git/refs/heads/{branch}"
        )
        if ref_r.is_success:
            obj = (ref_r.json() or {}).get("object") or {}
            sha = obj.get("sha")
            if sha and sha not in seen:
                seen.add(sha)
                out.append(sha)

    return out


def _extract_log_excerpt(
    text: str,
    *,
    max_lines: int = 30,
    failed_step: str | None = None,
    annotations: list[dict] | None = None,
) -> str:
    """Pick the most useful slice of a job log.

    Anchor strategy (in order):

    1. **Step-header anchor** — GitHub Actions logs contain
       ``##[group]Run <step-name>`` / ``##[endgroup]`` markers. When
       ``failed_step`` matches a group whose body fits this run, the
       excerpt is clamped to that group (``##[group]`` .. ``##[endgroup]``,
       inclusive). If the clamped block is shorter than ``max_lines``,
       trailing context from after ``##[endgroup]`` is appended up to
       ``max_lines``.
    2. **Annotation-line anchor** (fallback) — when at least one
       ``failure``-level annotation carries a ``start_line``, the
       excerpt is built around that line in the same way as the legacy
       substring scan. This helps composite-action / docker-in-runner
       jobs whose logs don't emit ``##[group]Run <name>`` markers.
    3. **Substring scan** (last resort) — first occurrence of
       ``error|failed|##[error]`` (case-insensitive) within the
       sub-sequence **after** the first ``##[group]`` marker (or, if
       none, the whole text). Restricting to the post-first-group
       region prevents template ``echo "::error::..."`` lines inside
       earlier setup steps from hijacking the anchor.
    4. **Tail** — last ``max_lines`` lines.

    The behaviour change vs. the previous implementation fixes
    `agent-project-issues#6` where an unexecuted template ``echo
    "::error::..."`` in a setup step's bash ``if`` block was matched
    by the naive substring scan and the excerpt was centred far away
    from the real failing step.
    """
    import re

    lines = text.splitlines()
    if not lines:
        return ""

    # --- 1) Step-header anchor ------------------------------------------------
    group_pattern = re.compile(r"^.*##\[group\]Run\s+(?P<name>.+?)\s*$")
    endgroup_pattern = re.compile(r"^.*##\[endgroup\]\s*$")
    # Build a list of (start_idx, name, end_idx) for every group block.
    groups: list[tuple[int, str, int]] = []
    open_idx: int | None = None
    open_name: str | None = None
    for idx, line in enumerate(lines):
        m = group_pattern.match(line)
        if m:
            # If a previous group never closed, record it ending here.
            if open_idx is not None and open_name is not None:
                groups.append((open_idx, open_name, idx - 1))
            open_idx = idx
            open_name = (m.group("name") or "").strip()
            continue
        if open_idx is not None and endgroup_pattern.match(line):
            groups.append((open_idx, open_name or "", idx))
            open_idx = None
            open_name = None
    # Unterminated trailing group: extend to end of log.
    if open_idx is not None and open_name is not None:
        groups.append((open_idx, open_name, len(lines) - 1))

    def _clamp(start: int, end: int) -> str:
        start = max(0, start)
        end = min(len(lines) - 1, end)
        block_len = end - start + 1
        if block_len < max_lines:
            # Pad with trailing context outside the group.
            end = min(len(lines) - 1, start + max_lines - 1)
        return "\n".join(lines[start : end + 1])

    if failed_step:
        target = failed_step.strip().casefold()
        for start_idx, name, end_idx in groups:
            if name.casefold() == target:
                return _clamp(start_idx, end_idx)

    # --- 2) Annotation-line anchor -------------------------------------------
    for ann in annotations or []:
        if (ann.get("annotation_level") or "").lower() != "failure":
            continue
        line_no = ann.get("start_line") or ann.get("end_line")
        if not isinstance(line_no, int) or line_no <= 0:
            continue
        # GitHub annotation line numbers refer to a source file, not the
        # raw log — but when annotations carry an explicit position we
        # use it as a *log-relative* anchor only when the value lies
        # within the log range. Otherwise fall through.
        if 1 <= line_no <= len(lines):
            idx = line_no - 1
            start = max(0, idx - 2)
            end = min(len(lines), idx + max_lines)
            return "\n".join(lines[start:end])

    # --- 3) Substring scan, but only AFTER the first group header -----------
    pattern = re.compile(r"(error|failed|##\[error\])", re.IGNORECASE)
    scan_offset = 0
    if groups:
        scan_offset = groups[0][0] + 1
    for idx in range(scan_offset, len(lines)):
        if pattern.search(lines[idx]):
            start = max(scan_offset, idx - 2)
            end = min(len(lines), idx + max_lines)
            return "\n".join(lines[start:end])

    # --- 4) Tail fallback ----------------------------------------------------
    return "\n".join(lines[-max_lines:])


def _fetch_job_log(token: str | None, log_url: str) -> str | None:
    """Fetch a job log via the 302-redirect signed-URL flow.

    GitHub responds with a 302 to a short-lived signed URL on a
    different host (typically blob.core.windows.net for hosted runners).
    httpx removes the Authorization header on cross-host redirects,
    which is the correct behavior — the signed URL carries its own auth.
    Returns `None` on 403/404 so the caller can mark logs as unavailable.

    Uses `follow_redirects=True` ONLY for this call (the default `_client`
    leaves it False, which is correct for the JSON API calls).
    """
    headers = {
        "Accept": ACCEPT,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(
        base_url=API_BASE,
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
    ) as c:
        r = c.get(log_url)
        if r.status_code in (403, 404):
            return None
        if not r.is_success:
            return None
        # Cap the read to ~256 KB so a runaway log doesn't blow memory.
        content = r.content[: 256 * 1024]
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return None


def _get_failure_excerpt(
    client: httpx.Client,
    project: ProjectConfig,
    token: str | None,
    run_id: str,
) -> PipelineFailure:
    """Build a `PipelineFailure` for a failed run.

    Walks the run's jobs, picks the failed ones, then for each:
      - reads check-run annotations (when `check_run_url` is present)
      - reads the job log via the 302 redirect flow and extracts an excerpt

    A 403/404 on either side leaves `annotations=[]` or
    `log_excerpt=None`; an overall `note` is set when at least one job
    had unavailable logs.
    """
    jobs_r = client.get(
        f"{_repo_path(project)}/actions/runs/{run_id}/jobs",
        params={"filter": "latest"},
    )
    _check(jobs_r)
    jobs = (jobs_r.json() or {}).get("jobs", []) or []
    failing: list[FailingJob] = []
    logs_missing = False
    for job in jobs:
        if (job.get("conclusion") or "") != "failure":
            continue
        # Pick the first failed step to surface as `failed_step`.
        failed_step = ""
        for step in job.get("steps") or []:
            if (step.get("conclusion") or "") == "failure":
                failed_step = step.get("name") or ""
                break

        # Annotations live on the check-run associated with the job.
        annotations: list[dict] = []
        check_url = job.get("check_run_url") or ""
        if check_url:
            # `check_run_url` is absolute; httpx accepts that when we pass it
            # directly (base_url is ignored for absolute URLs).
            ann_r = client.get(f"{check_url}/annotations")
            if ann_r.is_success:
                annotations = ann_r.json() or []
            elif ann_r.status_code not in (403, 404):
                _check(ann_r)

        # Log excerpt via the 302 redirect.
        log_excerpt: str | None = None
        job_id = job.get("id")
        if job_id is not None:
            log_text = _fetch_job_log(
                token, f"{_repo_path(project)}/actions/jobs/{job_id}/logs"
            )
            if log_text is None:
                logs_missing = True
            else:
                log_excerpt = _extract_log_excerpt(
                    log_text,
                    failed_step=failed_step or None,
                    annotations=annotations,
                )

        failing.append(
            FailingJob(
                name=job.get("name") or "",
                url=job.get("html_url") or "",
                failed_step=failed_step,
                annotations=annotations,
                log_excerpt=log_excerpt,
            )
        )
    note = "logs unavailable" if logs_missing else None
    return PipelineFailure(failing_jobs=failing, note=note)
