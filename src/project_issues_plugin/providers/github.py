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
    ensure_comment_prefix,
)
from project_issues_plugin.providers.base import (
    Comment,
    PRFilters,
    PullRequest,
    Relation,
    Status,
    Ticket,
    TicketFilters,
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
    if state == "open":
        status: Status = "open"
    elif state_reason == "not_planned":
        status = "not_planned"
    else:
        status = "completed"
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
    """Create the label if it doesn't exist. Idempotent — label-create failures
    are not fatal; we log and continue (the issue will be tagged with whatever
    labels the API accepts at PATCH/POST time)."""
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
    log.warning(
        "could not create label '%s' on %s/%s: %d %s",
        name, project.owner, project.repo, resp.status_code, resp.text[:200],
    )


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


class GitHubProvider:
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
        # Deduplicate while preserving order, ensure ai-generated is present.
        merged = list(dict.fromkeys([*labels, AI_GENERATED_LABEL]))
        with _client(token) as client:
            _ensure_label(client, project, AI_GENERATED_LABEL)
            payload: dict[str, Any] = {
                "title": title,
                "body": body,
                "labels": merged,
            }
            if assignees:
                payload["assignees"] = assignees
            r = client.post(f"{_repo_path(project)}/issues", json=payload)
            _check(r)
            return _map_issue(r.json())

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
            if AI_GENERATED_LABEL not in current_labels:
                if AI_MODIFIED_LABEL not in new_labels:
                    _ensure_label(client, project, AI_MODIFIED_LABEL)
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
                if status == "open":
                    payload["state"] = "open"
                elif status == "completed":
                    payload["state"] = "closed"
                    payload["state_reason"] = "completed"
                elif status == "not_planned":
                    payload["state"] = "closed"
                    payload["state_reason"] = "not_planned"
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
        """Create a pull request, applying the AI-generated label.

        Labels and assignees are applied in follow-up `POST /issues/{n}/...`
        calls because the `POST /pulls` endpoint doesn't accept them
        inline. We deduplicate `labels` while ensuring `ai-generated` is
        included, mirroring `create_ticket`.
        """
        merged_labels = list(dict.fromkeys([*(labels or []), AI_GENERATED_LABEL]))
        with _client(token) as client:
            _ensure_label(client, project, AI_GENERATED_LABEL)
            payload: dict[str, Any] = {
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": draft,
            }
            r = client.post(f"{_repo_path(project)}/pulls", json=payload)
            _check(r)
            pr_raw = r.json()
            pr_number = pr_raw["number"]
            # Apply labels via the issues endpoint (PRs share it).
            lbl_resp = client.post(
                f"{_repo_path(project)}/issues/{pr_number}/labels",
                json={"labels": merged_labels},
            )
            _check(lbl_resp)
            # Reflect the new labels back into the PR payload so the
            # returned dataclass advertises them.
            pr_raw["labels"] = lbl_resp.json()
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
            if AI_GENERATED_LABEL not in current_labels:
                if AI_MODIFIED_LABEL not in new_labels:
                    _ensure_label(client, project, AI_MODIFIED_LABEL)
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
