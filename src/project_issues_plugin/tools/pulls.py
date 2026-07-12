"""Pull-request operations exposed to the agent.

Mirrors `tools/tickets.py`:
  - read-only ops (`list_prs`, `get_pr`) only require a token when the
    repo is private; no permission flag is needed.
  - `create_pr` is gated by `pulls.create`.
  - `update_pr` and `add_pr_comment` are gated by `pulls.modify`.
  - `merge_pr` is gated by `pulls.merge` (new namespace, no flat-form
    equivalent — existing configs cannot merge without an explicit
    opt-in).

Markers (ai-generated label, ai-modified label, #ai-generated comment
prefix) are applied transparently by the provider.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from lib_python_projects import load_projects, resolve_token  # noqa: F401
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.base import PRFilters
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools._providers import (
    _normalize_id,
    _provider_for,
    _require_pulls_create,
    _require_pulls_merge,
    _require_pulls_modify,
    _require_token,
    _resolve,
    _rewrap_404,
    _rewrap_azure_bad_base,
    _safe,
)
from project_issues_plugin.tools._slicing import (
    apply_body_knobs,
    apply_omit_nulls,
    apply_order,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_prs(
        project_id: str,
        status: Annotated[Literal["open", "closed", "any"], Field(description="'open' (default), 'closed', or 'any'. Note: 'closed' also returns merged PRs — GitHub treats merged as closed, so a matched row may carry status: 'merged'. There is no 'merged' filter; use 'closed' and filter by the merged field.")] = "open",
        labels: list[str] | None = None,
        assignee: str | None = None,
        head: str | None = None,
        base: str | None = None,
        search: str | None = None,
        limit: int = 30,
        omit_body: bool = False,
        body_max_chars: int | None = None,
        omit_nulls: bool = False,
    ) -> dict:
        """List pull requests in a project. Default: open PRs, limit 30.

        Filter args:
          - `status`: "open" (default), "closed", or "any". `"closed"`
            also returns merged PRs — GitHub treats a merged PR as a
            closed one — so a row matched by `status="closed"` may carry
            `status: "merged"` and `merged: true`. There is no
            `"merged"` filter value; for closed-but-not-merged only,
            request `"closed"` and drop rows where `merged` is true.
          - `labels`: only PRs carrying ALL of these labels.
          - `assignee`: only PRs assigned to this user.
          - `head`: filter by source branch (`branch` or `owner:branch`).
          - `base`: filter by target branch.
          - `search`: free-text query (GitHub search syntax, scoped to PRs).
          - `limit`: capped at the provider's max page size (100).

        Token-cheap knobs:
          - `omit_body=True`: drop the `body` field from every row.
          - `body_max_chars=N`: truncate each row's body to N chars
            and add `body_truncated: bool`.
            `body_max_chars=N` measures N chars of content after the
            `#ai-generated`/`#ai-modified` marker prefix (if present),
            so the total stored body may be up to ~15 chars longer than N.
          - `omit_nulls=True`: drop top-level keys whose value is ``None``
            from every row (shallow strip — nested dicts such as ``head``
            and ``base`` are preserved intact). Combine with
            ``omit_body=True`` for the minimum-payload recipe when
            scanning titles / labels only.

        Note: `mergeable` and `mergeable_state` are always `null` in
        list results — these fields are only computed on single-PR
        fetches. Use `get_pr` for authoritative mergeability.

        GitLab note: `approvals_required` and `approvals_received` may
        come back `null` here for a GitLab MR even though `get_pr` for
        that same MR returns `0` — the list path does not compute these
        fields. Read the list-path `null` as "not populated on the list
        path," not as "zero" or "no approvals data." Call `get_pr` for
        authoritative approval counts.

        Routing caveat: when `labels`, `assignee`, or `search` are set
        the provider switches from the cheap `/repos/.../pulls` endpoint
        to GitHub's Search API (`/search/issues`, `is:pr` qualifier),
        which has its own rate-limit bucket (30 req/min). The
        default-fast path stays on the cheap endpoint.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            prs, has_more = provider.list_prs(
                project, token,
                PRFilters(
                    status=status,
                    labels=labels or [],
                    assignee=assignee,
                    head=head,
                    base=base,
                    search=search,
                    limit=limit,
                ),
            )
            rows = [asdict(pr) for pr in prs]
            rows = apply_body_knobs(
                rows, omit_body=omit_body, body_max_chars=body_max_chars,
            )
            if omit_nulls:
                rows = apply_omit_nulls(rows)
            applied_limit = min(max(1, limit), 100)
            payload: dict = {
                "project_id": project.id,
                "prs": rows,
                "applied_limit": applied_limit,
                "has_more": has_more,
            }
            return payload
        return _safe(go)

    @mcp.tool()
    def get_pr(
        project_id: str,
        pr_id: str,
        include_comments: bool = True,
        comments_limit: int | None = None,
        comments_order: Literal["asc", "desc"] = "asc",
        comments_body_max_chars: int | None = None,
        include_review_comments: bool = True,
        review_comments_limit: int | None = None,
    ) -> dict:
        """Get a pull request's details, discussion, and inline review comments.

        The response carries three lists:
          - `pull_request` — the PR snapshot (status, draft, reviewers,
            mergeable_state, ...).
          - `comments` — issue-style discussion comments from
            `/issues/{n}/comments` (GitHub) or non-positional MR notes
            (GitLab).
          - `review_comments` — inline, diff-anchored code-review
            comments (`/pulls/{n}/comments` on GitHub, positional MR
            discussion notes on GitLab). Each carries `path`, `line`,
            `commit_sha`, and an `in_reply_to` for threaded replies.

        `mergeable` (and `mergeable_state`) can be `null` for a short
        window right after `create_pr` — and again after a push — because
        GitHub computes mergeability asynchronously. `null` means "not
        computed yet", NOT "not mergeable": re-fetch a moment later to
        get the real `true` / `false` instead of branching on the
        transient `null`. On Azure DevOps, `mergeable_state` stays
        permanently `null` — Azure has no async-resolve pattern like
        GitHub's transient-then-computed null, so re-fetching will never
        populate it. Do not poll waiting for it on Azure DevOps.

        Azure DevOps quirk: `merge_commit_sha` (and `mergeable: true`)
        can already be populated right after `create_pr`, before any
        merge has happened — Azure DevOps computes a speculative
        pre-merge preview commit natively, it is NOT proof that a merge
        occurred. Check `merged` / `status` to know whether the PR was
        actually merged; do not treat a populated `merge_commit_sha` as
        confirmation. Contrast with GitHub, which correctly returns
        `mergeable: null` pre-merge (see above).

        GitLab quirk: `base.sha` is `null` immediately after `create_pr`
        and only populates on a later fetch (e.g. `merge_pr`'s response
        or a subsequent `get_pr` call) — the same async-computation
        pattern as GitHub's `mergeable_state`, but for this field.
        Re-fetch rather than treating the transient `null` as a missing
        or error value.

        `detailed_merge_status` is GitLab-only — GitHub and Azure DevOps
        always return `null` for it. GitLab's enum (may not be
        exhaustive): `unchecked`, `checking`, `mergeable`,
        `not_mergeable`, `discussions_not_resolved`, `ci_must_pass`,
        `ci_still_running`, `not_open`, `broken_status`,
        `blocked_status`, `commits_status`, `preparing`, `draft_status`,
        `jira_association_missing`, `need_rebase`, `conflict`.

        GitLab note: `approvals_required` and `approvals_received` are
        populated here (`0` when none are required or received) — but
        the same fields may come back `null` from `list_prs` for this
        same MR, because the list path doesn't compute them. Don't read
        that list-path `null` as a difference in the MR's actual
        approval state; treat this (`get_pr`) response as authoritative.

        Comment-slicing knobs — apply to the discussion `comments`
        list only. They do not affect `review_comments` in any way:
          - `include_comments=False`: omits the `comments` key entirely
            and emits `comments_fetched: false`. Skips the per-comment
            body fetch. `comments_limit=0` is an alias.
          - `comments_limit=N`: cap to N (0 == include_comments=False).
          - `comments_order="asc"|"desc"`: reverse the list.
          - `comments_body_max_chars=N`: truncate each comment body.
            `comments_body_max_chars=N` measures N chars of content after
            the `#ai-generated`/`#ai-modified` marker prefix (if present),
            so the total stored body may be up to ~15 chars longer than N.

        When comments are fetched, the response includes
        `comments_fetched: true` alongside the `comments` list.
        When skipped, the `comments` key is absent and
        `comments_fetched: false` is emitted instead.

        Review-comment-slicing knobs — apply to the inline `review_comments`
        list only. They do not affect the discussion `comments`:
          - `include_review_comments=False`: omits the `review_comments`
            key entirely and emits `review_comments_fetched: false`.
            Skips the provider `list_pr_review_comments` call entirely.
            `review_comments_limit=0` is an alias for this flag — both
            forms skip the provider call and omit the key.
          - `review_comments_limit=N`: cap to N (0 == include_review_comments=False).

        When review comments are fetched, the response includes
        `review_comments_fetched: true` alongside the `review_comments`
        list. When skipped, the `review_comments` key is absent and
        `review_comments_fetched: false` is emitted instead.
        """
        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            normalized_pr = _normalize_id(project, pr_id)
            drop_review_comments = (
                (not include_review_comments) or review_comments_limit == 0
            )
            try:
                pr, comments = provider.get_pr(project, token, normalized_pr)
                if not drop_review_comments:
                    review_comments = provider.list_pr_review_comments(
                        project, token, normalized_pr,
                    )
                else:
                    review_comments = None
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="pr",
                    ident=normalized_pr,
                )
            drop_comments = (not include_comments) or comments_limit == 0
            if drop_comments:
                comments_block: dict = {"comments_fetched": False}
            else:
                ordered = apply_order(comments, comments_order)
                if comments_limit is not None and comments_limit > 0:
                    ordered = ordered[:comments_limit]
                comment_rows = [asdict(c) for c in ordered]
                comment_rows = apply_body_knobs(
                    comment_rows,
                    omit_body=False,
                    body_max_chars=comments_body_max_chars,
                )
                comments_block = {
                    "comments": comment_rows,
                    "comments_fetched": True,
                }
            if drop_review_comments:
                review_block: dict = {"review_comments_fetched": False}
            else:
                rc_list = list(review_comments)  # type: ignore[arg-type]
                if review_comments_limit is not None and review_comments_limit > 0:
                    rc_list = rc_list[:review_comments_limit]
                review_block = {
                    "review_comments": [asdict(c) for c in rc_list],
                    "review_comments_fetched": True,
                }
            return {
                "project_id": project.id,
                "pull_request": asdict(pr),
                **comments_block,
                **review_block,
            }
        return _safe(go)

    @mcp.tool()
    def create_pr(
        project_id: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        requested_reviewers: list[str] | None = None,
    ) -> dict:
        """Create a pull request.

        Just create what the user asked for — DO NOT pre-inspect the
        repository or codebase to "gather context" first.

        `head` is the source branch (`feature/x` or `owner:branch` for
        cross-fork PRs); `base` is the target branch. The label
        `ai-generated` is added automatically by the server — do not
        pass it yourself. The body is also automatically prefixed with
        `#ai-generated\\n\\n` — do not prepend it yourself. The server
        deduplicates the marker if you accidentally include it.

        `requested_reviewers` is a list of usernames to request a review
        from. Distinct from `assignees`: reviewers carry per-user review
        state (approved / changes-requested / commented); assignees
        don't. Requires the project's `pulls.create` permission.

        Azure DevOps quirk: the returned `merge_commit_sha` (with
        `mergeable: true`) can already be populated on this very
        create response, before any merge has happened — Azure DevOps
        computes a speculative pre-merge preview commit natively. Do
        not read it as proof of a merge; check `merged` / `status`
        instead.

        GitLab quirk: `base.sha` is `null` on this create response and
        only populates on a later fetch (e.g. via `merge_pr`'s response
        or a subsequent `get_pr` call) — don't treat the transient
        `null` as a missing or error value.
        """
        def go() -> dict:
            if head == base:
                return {"error": "head and base must differ"}
            project = _resolve(project_id)
            _require_pulls_create(project)
            token = _require_token(project)
            provider = _provider_for(project)
            try:
                pr = provider.create_pr(
                    project, token, title, body, head, base,
                    draft=draft, labels=labels or [], assignees=assignees or [],
                    requested_reviewers=requested_reviewers or [],
                )
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                # ticket #195 finding 2: normalize Azure's raw base-branch
                # activation-failure text; non-matching errors pass through
                # unchanged.
                raise _rewrap_azure_bad_base(exc, base=base)
            return {"project_id": project.id, "pull_request": asdict(pr)}
        return _safe(go)

    @mcp.tool()
    def update_pr(
        project_id: str,
        pr_id: str,
        title: str | None = None,
        body: str | None = None,
        # Plain `str` (not Literal) so the tool-layer guard below can
        # produce the merge_pr hint instead of pydantic's generic
        # literal_error wall-of-text (ticket #48 finding 9).
        status: Annotated[str | None, Field(description="Accepted values: 'open' (reopen) or 'closed' (close without merging). Must not be 'merged' — use merge_pr instead. Kept as str (not Literal) so invalid values return a friendly error rather than a Pydantic literal_error.")] = None,
        base: str | None = None,
        labels_add: list[str] | None = None,
        labels_remove: list[str] | None = None,
        assignees_add: list[str] | None = None,
        assignees_remove: list[str] | None = None,
        reviewers_add: list[str] | None = None,
        reviewers_remove: list[str] | None = None,
        draft: bool | None = None,
    ) -> dict:
        """Update an existing pull request. Only specified fields change.

        `status` accepts `"open"` (reopen) or `"closed"` (close without
        merging). To merge a PR call `merge_pr` — passing
        `status="merged"` is rejected.

        `draft` toggles the PR's draft state. `True` flips a ready PR
        into draft; `False` marks a draft as ready for review. Passing
        `None` (default) leaves the state untouched. GitHub uses
        GraphQL mutations behind the scenes; GitLab manipulates the
        title prefix (`Draft: `).

        Label, assignee, and reviewer changes are add/remove operations
        relative to the current set. Reviewers are tracked separately
        from assignees because they carry per-user review state (approved
        / changes-requested / commented) that assignees don't.

        The label `ai-modified` is added automatically when the PR
        wasn't previously `ai-generated`.

        When `body` is supplied, it is rewritten so the first line is
        exactly one `#ai-*` marker matching the PR's post-update label
        state — `#ai-generated` for AI-authored PRs, `#ai-modified` for
        the first AI touch of a human-authored PR. Callers should NOT
        prepend the marker themselves.

        Requires the project's `pulls.modify` permission.
        """
        # Tool-layer guard for the disallowed value — keeps the contract
        # explicit at the surface so the agent doesn't have to read the
        # provider source. `status: Literal["open", "closed"]` already
        # catches it at the MCP boundary; this is a defence-in-depth
        # check for direct callers (tests, in-process invocation).
        # Merge transitions live in `merge_pr` — keep them out of update.
        if status is not None and status not in ("open", "closed"):
            return {
                "error": (
                    f"status='{status}' is not supported by update_pr. "
                    "To merge a PR call merge_pr; to close it pass status='closed'."
                )
            }

        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_pr = _normalize_id(project, pr_id)
            pr = provider.update_pr(
                project, token, normalized_pr,
                title=title, body=body, status=status, base=base,
                labels_add=labels_add, labels_remove=labels_remove,
                assignees_add=assignees_add, assignees_remove=assignees_remove,
                reviewers_add=reviewers_add, reviewers_remove=reviewers_remove,
                draft=draft,
            )
            return {"project_id": project.id, "pull_request": asdict(pr)}
        return _safe(go)

    @mcp.tool()
    def add_pr_comment(project_id: str, pr_id: str, body: str) -> dict:
        """Add a discussion (issue-style) comment to a pull request.

        Uses the shared issue-comments endpoint; the body is
        automatically prefixed with `#ai-generated\\n\\n`. This posts a
        top-level discussion comment — it is NOT anchored to a diff
        line. To attach a comment to a specific file + line in the PR's
        diff, or to reply to an existing review thread, use
        `add_pr_review_comment` instead (the inline / code-review
        variant).

        Requires the project's `pulls.modify` permission.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_pr = _normalize_id(project, pr_id)
            comment = provider.add_pr_comment(project, token, normalized_pr, body)
            return {"project_id": project.id, "comment": asdict(comment)}
        return _safe(go)

    @mcp.tool()
    def add_pr_review_comment(
        project_id: str,
        pr_id: str,
        body: str,
        path: Annotated[str | None, Field(description="New-thread mode: file path in the diff (e.g. 'src/foo.py'). Set together with line and commit_sha. Leave unset in reply mode.")] = None,
        line: Annotated[int | None, Field(description="New-thread mode: absolute file line number (1-based), NOT a diff-hunk position. Set together with path and commit_sha. Leave unset in reply mode.")] = None,
        side: Literal["LEFT", "RIGHT"] = "RIGHT",
        commit_sha: Annotated[str | None, Field(description="New-thread mode: the commit SHA the comment is anchored to. Set together with path and line. Leave unset in reply mode.")] = None,
        in_reply_to: Annotated[str | None, Field(description="Reply mode: opaque discussion id from get_pr review_comments — pass back verbatim, do not parse or construct. Shape varies by provider (GitHub: numeric string; GitLab: 40-char SHA; Azure DevOps: short numeric). Leave path/line/commit_sha unset.")] = None,
    ) -> dict:
        """Add an inline code-review comment to a pull request.

        Distinct from `add_pr_comment`, which posts an issue-style
        discussion comment. This tool anchors the comment to a specific
        path + line in the PR's diff.

        Two modes — exactly one set of arguments must be provided:
          - **New thread**: pass `path`, `line`, and `commit_sha`. Leave
            `in_reply_to` unset.
          - **Reply**: pass `in_reply_to=<discussion_id>`. Leave the
            positional args unset. Read `discussion_id` off any
            `review_comment` you got from `get_pr` (or off the return
            value of a fresh `add_pr_review_comment` new-thread call);
            the identifier is opaque and provider-specific — pass it
            back verbatim without parsing or constructing it.

        `side` is `"RIGHT"` (default) for the post-change side of the
        diff or `"LEFT"` for the pre-change side. GitLab ignores it and
        uses the supplied `line` as the new-side anchor.

        Body is marker-prefixed automatically. Requires the project's
        `pulls.modify` permission. To post a discussion-level comment
        (not anchored to a diff line), use `add_pr_comment` instead.
        """
        new_thread = (path is not None) or (line is not None) or (
            commit_sha is not None
        )
        if in_reply_to is not None and new_thread:
            return {
                "error": (
                    "add_pr_review_comment: pass either positional args "
                    "(path/line/commit_sha) for a new thread OR "
                    "in_reply_to for a reply — not both."
                )
            }
        if in_reply_to is None and not (path and line and commit_sha):
            return {
                "error": (
                    "add_pr_review_comment: a new thread requires "
                    "path, line, and commit_sha."
                )
            }

        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_pr = _normalize_id(project, pr_id)
            rc = provider.add_pr_review_comment(
                project, token, normalized_pr,
                body=body,
                path=path, line=line, side=side,
                commit_sha=commit_sha, in_reply_to=in_reply_to,
            )
            return {"project_id": project.id, "review_comment": asdict(rc)}
        return _safe(go)

    @mcp.tool()
    def submit_pr_review(
        project_id: str,
        pr_id: str,
        state: Annotated[str, Field(description="Required. Lowercase only. One of: 'approve', 'request_changes', 'comment'. A non-empty body is required when state is 'request_changes' or 'comment'; optional for 'approve'. Kept as str (not Literal) so invalid values return a friendly error. Azure DevOps note: Azure natively has 5 reviewer votes but this tool only exposes 3 — 'approve_with_suggestions' and 'wait_for_author' are not supported values here (see docstring).")],
        body: Annotated[str | None, Field(description="Required when state is 'request_changes' or 'comment'; optional for 'approve'. Do not prepend '#ai-generated' — added automatically.")] = None,
        commit_sha: str | None = None,
    ) -> dict:
        """Submit a review on a pull request.

        `state` is one of:
          - `"approve"`         — approve the PR (a body is optional;
            on GitLab it's posted as a separate note so the rationale
            is captured). Self-approval policy diverges by provider:
            GitHub hard-blocks approving your own PR — the error
            message contains GitHub's underlying text `Can not approve
            your own pull request`, wrapped as `GitHub 422: ...` with
            `(GitHub platform restriction; use another account)`
            appended (not a bare passthrough) — while GitLab allows
            self-approval outright. Generic approve-then-merge logic
            must be prepared to handle/tolerate the GitHub error.
          - `"request_changes"` — request changes (a body is required;
            on GitLab this also issues a best-effort `unapprove`).
          - `"comment"`         — leave a review-level comment without
            changing approval state (a body is required).

        Azure DevOps note: Azure natively supports 5 reviewer votes, but
        this tool only exposes 3. The mapping is `"approve"` →
        `approved` (+10), `"request_changes"` → `rejected` (-10),
        `"comment"` → `no vote` (0). Azure's other two native votes,
        `approve_with_suggestions` (+5) and `wait_for_author` (-5), are
        normalized away and cannot be set through this tool — passing
        either as `state` returns `{"error": "..."}`.

        `commit_sha`, when set, pins the review to a specific commit on
        GitHub (`commit_id`). GitLab cannot pin a review to a commit: it
        ignores `commit_sha` and the returned review always has
        `commit_sha: null` regardless of what you pass. Only set it for
        GitHub commit-pinning — it has no effect on GitLab, so don't read
        the response `commit_sha` there as confirmation.

        The review body is marker-prefixed automatically — callers
        should NOT prepend `#ai-generated` themselves. Requires the
        project's `pulls.modify` permission.
        """
        if state not in ("approve", "request_changes", "comment"):
            return {
                "error": "state must be one of: approve, request_changes, comment"
            }
        if state in ("request_changes", "comment") and not body:
            return {
                "error": (
                    f"a review body is required when state='{state}'."
                )
            }

        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_modify(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_pr = _normalize_id(project, pr_id)
            review = provider.submit_pr_review(
                project, token, normalized_pr,
                state=state, body=body, commit_sha=commit_sha,
            )
            return {"project_id": project.id, "review": asdict(review)}
        return _safe(go)

    @mcp.tool()
    def merge_pr(
        project_id: str,
        pr_id: str,
        merge_method: Literal["merge", "squash", "rebase"] = "merge",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> dict:
        """Merge a pull request.

        `merge_method` controls the merge style. Accepted by both
        GitHub and GitLab providers:
          - "merge":  create a merge commit (default).
          - "squash": squash all commits into one and merge.
          - "rebase": rebase the head branch onto base. GitLab raises
            here because its rebase flow is a separate endpoint
            (PUT .../rebase) that does not itself merge — call rebase
            first, then merge with "merge".

        `commit_title` / `commit_message` are forwarded as the merge
        commit's title and body. GitLab has no separate title/body
        split so the provider joins them as `"<title>\\n\\n<message>"`
        into the appropriate `merge_commit_message` /
        `squash_commit_message` field.

        Requires the project's `pulls.merge` permission. This flag has
        no flat-form equivalent and defaults to False on existing
        configs — the user must explicitly opt in.

        Returns `{"project_id": str, "pull_request": {...}}` where
        `pull_request` is the full post-merge PR snapshot — the same
        shape `get_pr` returns — so `merged: true` and
        `merge_commit_sha` are available without a follow-up `get_pr`.

        Azure DevOps quirk: merging a PR adds the merging user to
        `requested_reviewers` as a native side effect of the merge
        operation itself — reviewer/assignee fields can mutate as a
        result of merging, not just PR-state fields like `merged` /
        `status`.
        """
        def go() -> dict:
            project = _resolve(project_id)
            _require_pulls_merge(project)
            token = _require_token(project)
            provider = _provider_for(project)
            normalized_pr = _normalize_id(project, pr_id)
            pr = provider.merge_pr(
                project, token, normalized_pr,
                merge_method=merge_method,
                commit_title=commit_title,
                commit_message=commit_message,
            )
            return {"project_id": project.id, "pull_request": asdict(pr)}
        return _safe(go)
