"""Shared row-slicing helpers for list/get tools.

Lifted out so the same `order` / `since` semantics serve `list_comments`
(ticket #47) AND `get_ticket` / `get_pr` comment slicing (ticket #50).
The body-trim helpers in this module are consumed by ticket #50.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Literal

# AI-attribution marker prefixes that `apply_body_knobs` strips before
# measuring `body_max_chars`.  The markers are always followed by a
# blank line (`\n\n`), giving two-character overhead per prefix.
_AI_MARKER_PREFIXES = ("#ai-generated\n\n", "#ai-modified\n\n")


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp tolerating the `Z` suffix.

    Returns a timezone-aware `datetime`. Raises `ValueError` for
    unparseable inputs — callers translate that to a user-facing error.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def filter_since(rows: Iterable[Any], since: str | None, *, attr: str = "created_at"):
    """Keep rows with `<attr>` strictly >= `since`. No-op when `since` is None.

    Operates on dataclass instances (uses `getattr`) — provider methods
    apply this AFTER mapping the raw API payload to a dataclass.
    """
    if not since:
        return list(rows)
    since_dt = _parse_iso(since)
    out = []
    for r in rows:
        ts = getattr(r, attr)
        if ts and _parse_iso(ts) >= since_dt:
            out.append(r)
    return out


def apply_order(rows: list, order: Literal["asc", "desc"]) -> list:
    """Return rows in the requested order, assuming the input is ascending."""
    if order == "desc":
        return list(reversed(rows))
    return rows


def apply_body_knobs(
    rows: list[dict[str, Any]],
    *,
    omit_body: bool,
    body_max_chars: int | None,
    body_attr: str = "body",
) -> list[dict[str, Any]]:
    """Apply body slimming knobs to a list of dicts (post-`asdict`).

    - `omit_body=True`: drop the body key entirely. A `body_truncated`
      sibling is NOT set (callers detect omission via `body_attr not in row`).
    - `body_max_chars=N`: truncate `body` to N characters and add a
      `f"{body_attr}_truncated": bool` sibling (e.g. `body_truncated`
      for the default `body_attr="body"`, `patch_truncated` for
      `body_attr="patch"`) so callers can tell the body is a prefix.
      When the body starts with an `#ai-generated` or `#ai-modified`
      marker prefix (followed by ``\\n\\n``), the cap is applied to the
      content *after* the marker, so the marker itself is always
      preserved.  The total stored body may therefore be up to ~15 chars
      longer than N.

    Defaults (`omit_body=False`, `body_max_chars=None`) are a pass-through.
    """
    if not omit_body and body_max_chars is None:
        return rows
    truncated_key = f"{body_attr}_truncated"
    out: list[dict[str, Any]] = []
    for row in rows:
        new = dict(row)
        if omit_body:
            new.pop(body_attr, None)
            out.append(new)
            continue
        body = new.get(body_attr)
        if body_max_chars is not None and isinstance(body, str):
            # Detect an AI-attribution marker prefix and measure the cap
            # against the content portion only, so the marker is preserved.
            marker = ""
            content = body
            for prefix in _AI_MARKER_PREFIXES:
                if body.startswith(prefix):
                    marker = prefix
                    content = body[len(prefix):]
                    break
            if len(content) > body_max_chars:
                new[body_attr] = marker + content[:body_max_chars]
                new[truncated_key] = True
            else:
                new[truncated_key] = False
        out.append(new)
    return out


def apply_omit_nulls(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop top-level keys whose value is ``None`` from each row.

    Shallow only — nested dicts (e.g. ``head``, ``base``) are left
    intact, including any ``None`` values they contain.  This avoids
    stripping structural fields that providers return as ``None`` rather
    than omitting entirely (e.g. ``head.sha`` before a push).
    """
    return [{k: v for k, v in row.items() if v is not None} for row in rows]


# --- light write responses (ticket #314) -------------------------------------
# Write tools return only these keys by default; `response="full"` keeps the
# complete object. Each set is a strict subset of the full vocabulary.
TICKET_LIGHT_KEYS = ("id", "url", "status", "labels", "custom_fields", "updated_at")
COMMENT_LIGHT_KEYS = ("id", "url", "created_at")
PR_LIGHT_KEYS = ("id", "url", "status", "merged", "mergeable_state", "head")
RELATION_LIGHT_KEYS = ("kind", "ticket_id")
REVIEW_LIGHT_KEYS = ("id", "url", "submitted_at")
REVIEW_COMMENT_LIGHT_KEYS = ("id", "url", "created_at", "discussion_id")

TICKET_RESPONSE_DESC = (
    "Response shape. Default `light`: the ticket carries exactly `id`, `url`, "
    "`status`, `labels`, `custom_fields`, `updated_at` (plus project_id and any "
    "warning). Values come from the write response with no reload, so `status` "
    "and `custom_fields` may be pre-cascade; read the settled values with "
    "`get_ticket(..., include_custom_fields=True)`. Labels such as ai-modified "
    "and column labels are still applied - light only shrinks the response. "
    "`custom_fields` is None when the provider returns none on a write. "
    "Light does not echo edited content: the title and body you changed are "
    "absent from this response, so pass response=full or re-read with "
    "get_ticket to confirm they landed. "
    'Pass `response="full"` for the full ticket (body, comments and review '
    "data)."
)

COMMENT_RESPONSE_DESC = (
    "Response shape. Default `light`: the comment carries exactly `id`, `url`, "
    "`created_at` (plus project_id). Values come from the write response with "
    "no reload. The marker prefix and any labels are still applied - light only "
    "shrinks the response. A field the provider does not return is None. "
    "Light does not echo comment content: the body and updated_at are absent "
    "from this response, so pass response=full to confirm an edit landed. "
    'Pass `response="full"` for the full comment (body, author).'
)

PR_RESPONSE_DESC = (
    "Response shape. Default `light`: the pull request carries exactly `id`, "
    "`url`, `status`, `merged`, `mergeable_state`, `head` (plus project_id). "
    "`head` is a dict with exactly `head.ref`, `head.sha`, "
    "`head.repo_full_name` - on GitLab `head.repo_full_name` is None for an "
    "unresolved cross-fork source. "
    "Name mappings only, not keys of this response and not input parameter "
    "names: `number` is `id`, `state` is `status`, `head_sha` is `head.sha`. "
    "Values come from the write response with no reload. Labels and the "
    "ai-generated/ai-modified markers are still applied - light only shrinks "
    "the response. `mergeable_state` is provider-specific and None where the "
    "provider does not report it (e.g. GitHub right after a merge, GitLab, "
    "Azure DevOps). "
    "Light does not echo edited content: the title, body and draft flag are "
    "absent from this response, so pass response=full or re-read with get_pr "
    "to confirm a change landed. "
    'Pass `response="full"` for the full pull request (body, reviews, '
    "comments data)."
)

RELATION_RESPONSE_DESC = (
    "Response shape. Default `light`: the relation carries exactly `kind`, "
    "`ticket_id` (plus project_id). `relation.ticket_id` echoes the input "
    "`target`, which is not a response key itself. Values come from the write "
    "response with no reload. The relation and any labels are still applied - "
    "light only shrinks the response. A field the provider does not return is "
    "None. Light does not echo the related ticket's title or state: they are "
    "absent from this response. "
    'Pass `response="full"` for the full relation (title, url, state).'
)

REVIEW_RESPONSE_DESC = (
    "Response shape. Default `light`: the review carries exactly `id`, `url`, "
    "`submitted_at` (plus project_id). Values come from the write response "
    "with no reload. The marker prefix and any labels are still applied - "
    "light only shrinks the response. A field the provider does not return is None. "
    "Light does not echo review content: the state and body are absent from "
    "this response, so pass response=full to confirm them. "
    'Pass `response="full"` for the full review (state, author, body, '
    "commit_sha)."
)

REVIEW_COMMENT_RESPONSE_DESC = (
    "Response shape. Default `light`: the review comment carries exactly "
    "`id`, `url`, `created_at`, `discussion_id` (plus project_id); pass "
    "the discussion_id back as in_reply_to to continue the thread. Values "
    "come from the write response with no reload. The marker prefix and any "
    "labels are still applied - light only shrinks the response. A field the provider does not "
    "return is None. "
    "Light does not echo anchor or content: the path, line, side, commit_sha "
    "and body are absent from this response, so pass response=full to confirm "
    "them. "
    'Pass `response="full"` for the full review comment.'
)


def pick_light(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Return ``{k: row[k]}`` for the keys present, in the declared order.

    ``None`` values are kept so per-provider null fields stay visible.
    """
    return {k: row[k] for k in keys if k in row}


__all__ = [
    "COMMENT_LIGHT_KEYS",
    "COMMENT_RESPONSE_DESC",
    "PR_LIGHT_KEYS",
    "PR_RESPONSE_DESC",
    "RELATION_LIGHT_KEYS",
    "RELATION_RESPONSE_DESC",
    "REVIEW_COMMENT_LIGHT_KEYS",
    "REVIEW_COMMENT_RESPONSE_DESC",
    "REVIEW_LIGHT_KEYS",
    "REVIEW_RESPONSE_DESC",
    "TICKET_LIGHT_KEYS",
    "TICKET_RESPONSE_DESC",
    "pick_light",
    "apply_body_knobs",
    "apply_omit_nulls",
    "apply_order",
    "filter_since",
]
