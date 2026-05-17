"""Provider abstraction — common types shared by GitHub/GitLab."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Provider-native status as a free string.
#
# Historically a 3-value enum (`open`/`completed`/`not_planned`) was used
# here, but that model could not represent Azure-DevOps workflows
# (`Resolved`, `Committed`, custom states, etc.). The string now flows
# through unchanged; agents discover valid values + semantic hints via
# `list_ticket_statuses`. GitHub uses a `state:state_reason` suffix
# encoding to preserve the `closed:completed` vs `closed:not_planned`
# distinction.
Status = str
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


PRStatus = Literal["open", "closed", "merged"]
PRListStatus = Literal["open", "closed", "any"]


@dataclass
class PullRequest:
    """A pull-request snapshot mirroring `Ticket` but with PR-specific fields.

    `id` is the PR number as a string (mirrors `Ticket.id` style).
    `mergeable` is `None` when GitHub has not yet computed mergeability.
    """

    id: str
    number: int
    title: str
    body: str
    status: PRStatus
    draft: bool
    author: str
    assignees: list[str]
    reviewers: list[str]              # users who actually submitted a review
    requested_reviewers: list[str]
    labels: list[str]
    head: dict                        # {"ref", "sha", "repo_full_name"}
    base: dict                        # {"ref", "sha"}
    merged: bool
    mergeable: bool | None
    url: str
    created_at: str
    updated_at: str


@dataclass
class StatusSpec:
    """Result of `list_ticket_statuses` — discovery payload for the
    provider-native status state-space.

    `values` lists every accepted status string (including any GitHub
    `state:state_reason` suffix encodings). `transitions` maps each
    value to the values that can legally follow it. `hints` exposes
    semantic anchors so agents can act without provider-specific
    knowledge:

    - `default_open` — the value to use when reopening a ticket.
    - `terminal` — every value that ends the workflow.
    - `terminal_completed` — the terminal value meaning "done as planned".
    - `terminal_declined` — the terminal value meaning "won't do" /
      "not planned".

    For providers that don't distinguish completed-vs-declined (GitLab,
    most ADO templates) `terminal_completed` and `terminal_declined`
    may be the same value.
    """

    values: list[str]
    transitions: dict[str, list[str]]
    hints: dict[str, str | list[str]]


@dataclass
class PRFilters:
    status: PRListStatus = "open"
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    head: str | None = None           # branch name (`feat/x`) or `owner:branch`
    base: str | None = None
    search: str | None = None
    limit: int = 30


# ---------- pipelines / CI runs ---------------------------------------------


@dataclass
class FailingJob:
    """A single failing job within a pipeline run.

    `failed_step` is the name of the step that flipped the job red, when
    GitHub reports it. `annotations` is the list of GitHub Check-Run
    annotations attached to the job (typically the `failure` /
    `warning` items emitted by build tooling). `log_excerpt` is a small
    text excerpt around the failure (or `None` when logs were
    unavailable, e.g. 403/404 on the log endpoint).
    """

    name: str
    url: str
    failed_step: str
    annotations: list[dict]
    log_excerpt: str | None


@dataclass
class PipelineFailure:
    """Aggregated failure context for a single completed-failed run."""

    failing_jobs: list[FailingJob]
    note: str | None = None  # e.g. "logs unavailable"


@dataclass
class PipelineRun:
    """A CI/CD pipeline run (GitHub Actions workflow_run / GitLab pipeline).

    `conclusion` is `None` for in-progress runs. `failure` is only
    populated by `get_pipeline_run` when the caller asks for the
    failure excerpt AND the run actually concluded as failed.
    """

    id: str
    name: str
    branch: str
    head_sha: str
    event: str
    status: str
    conclusion: str | None
    url: str
    created_at: str
    updated_at: str
    run_attempt: int
    failure: PipelineFailure | None = None


# ---------- token capabilities (ticket #32) ---------------------------------


@dataclass
class TokenCapabilities:
    """Result of `probe_token_capabilities` — what a given token may do
    against a given project.

    Mirrors the nested `Permissions` model from `config.py` so the result
    can be substituted in directly for auto-discovered projects:

    - `issues_create` / `issues_modify` — issue write operations.
    - `pulls_create` / `pulls_modify` / `pulls_merge` — pull-request
      write operations.

    `reason` is `None` on the happy path. On any failure mode it carries
    a stable string identifier so the caller (and tests) can branch on
    it without parsing free-form text:

    - `"bad_credentials"`        — 401 from the provider.
    - `"repo_invisible_to_token"` — 404 from the provider (the token has
      no visibility into the repo, which is GitHub's privacy-preserving
      response for both "doesn't exist" and "exists but you can't see it").
    - `"network_error"`           — transport-level failure (DNS,
      connection refused, timeout, ...).
    - `"permissions_field_missing"` — request succeeded but GitHub
      didn't populate `permissions` on the response (classic PAT
      sometimes, or unexpected payload shape). Combined with all-False
      flags this preserves today's hardcoded-False default behavior.

    When `reason` is not `None`, all boolean flags should be False —
    the caller must not grant any operation based on a failed probe.
    """

    issues_create: bool = False
    issues_modify: bool = False
    pulls_create: bool = False
    pulls_modify: bool = False
    pulls_merge: bool = False
    reason: str | None = None


class TokenCapabilityProvider:
    """Mixin/interface: providers that can probe a token's effective
    capabilities against a single project implement this method.

    Implementations MUST NOT raise on expected failure modes (401, 404,
    network error, missing field) — they must return a `TokenCapabilities`
    with `reason` set and all flags False so the caller can degrade
    gracefully. Only programming errors (bad project shape, etc.) should
    propagate.
    """

    def probe_token_capabilities(
        self, project, token: str
    ) -> TokenCapabilities:
        raise NotImplementedError
