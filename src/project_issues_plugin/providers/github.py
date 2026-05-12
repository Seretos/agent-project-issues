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
    ) -> tuple[Ticket, list[Comment]]:
        with _client(token) as client:
            r = client.get(f"{_repo_path(project)}/issues/{ticket_id}")
            _check(r)
            ticket = _map_issue(r.json())
            c = client.get(
                f"{_repo_path(project)}/issues/{ticket_id}/comments",
                params={"per_page": 100},
            )
            _check(c)
            comments = [_map_comment(it) for it in c.json()]
        return ticket, comments

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
