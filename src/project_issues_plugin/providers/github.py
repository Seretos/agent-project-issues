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


class GitHubProvider:
    def list_tickets(
        self,
        project: ProjectConfig,
        token: str | None,
        filters: TicketFilters,
    ) -> list[Ticket]:
        per_page = min(max(1, filters.limit), 100)
        with _client(token) as client:
            if filters.search:
                qual_parts = [
                    f"repo:{project.owner}/{project.repo}",
                    "is:issue",
                ]
                if filters.status in ("open", "closed"):
                    qual_parts.append(f"is:{filters.status}")
                if filters.assignee:
                    qual_parts.append(f"assignee:{filters.assignee}")
                for lbl in filters.labels:
                    qual_parts.append(f'label:"{lbl}"')
                q = f"{filters.search} {' '.join(qual_parts)}"
                r = client.get("/search/issues", params={"q": q, "per_page": per_page})
                _check(r)
                items = r.json().get("items", [])
            else:
                params: dict[str, Any] = {
                    "per_page": per_page,
                    "state": filters.status if filters.status in ("open", "closed") else "all",
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
