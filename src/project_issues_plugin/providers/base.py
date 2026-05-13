"""Provider abstraction — common types shared by GitHub/GitLab."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Status = Literal["open", "completed", "not_planned"]
ListStatus = Literal["open", "closed", "any"]


@dataclass
class Ticket:
    id: str               # provider-native id (issue.number / iid) as string
    title: str
    body: str
    status: Status
    author: str
    assignees: list[str]
    labels: list[str]
    url: str
    created_at: str       # ISO-8601 string
    updated_at: str


@dataclass
class Comment:
    id: str
    author: str
    body: str
    url: str
    created_at: str


RelationKind = Literal[
    "parent",
    "child",
    "closes",
    "closed_by",
    "duplicate_of",
    "duplicated_by",
    "mentions",
    "mentioned_by",
    # Reserved for GitLab; not currently emitted by GitHub.
    "relates_to",
    "blocks",
    "blocked_by",
]


@dataclass
class Relation:
    """A typed link between this ticket and another ticket / PR.

    `ticket_id` is `"#N"` for references within the same repository and
    `"owner/repo#N"` for cross-repo references. `state` is `"open"`,
    `"closed"`, `"merged"`, or `""` when the provider didn't report one.
    `is_pull_request` is true when the other side is a PR/MR. `title`
    is best-effort and may be empty if the provider didn't return it.
    """

    kind: str
    ticket_id: str
    title: str
    url: str
    state: str
    is_pull_request: bool


SortBy = Literal["created", "updated", "comments"]
SortOrder = Literal["asc", "desc"]


@dataclass
class TicketFilters:
    status: ListStatus = "open"
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    search: str | None = None
    limit: int = 30
    not_labels: list[str] = field(default_factory=list)
    author: str | None = None
    created_after: str | None = None
    created_before: str | None = None
    updated_after: str | None = None
    updated_before: str | None = None
    sort_by: SortBy = "created"
    sort_order: SortOrder = "desc"
