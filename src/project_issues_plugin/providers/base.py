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


@dataclass
class TicketFilters:
    status: ListStatus = "open"
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    search: str | None = None
    limit: int = 30
