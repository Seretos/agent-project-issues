---
name: project-issues
description: >
  Manage tickets, issues, bugs, backlog items, pull requests, merge requests,
  and code review comments on GitHub, GitLab, or Azure DevOps. Use when the
  user mentions: ticket system, issue tracker, project board, PR, pull request,
  merge request, code review, GitHub, GitLab, Azure DevOps, or their German
  equivalents — "Ticket", "Issue", "Bug", "Backlog", "Projektboard",
  "Pull Request", "Merge Request", "Code-Review", "leg ein Ticket an",
  "erstelle ein Issue", "welche Projekte gibt es", "öffne ein Issue",
  "zeig mir offene Tickets", "was sind die offenen PRs".
---

# project-issues

## What this skill is for

Use this skill whenever the user references tickets, issues, bugs, PRs, merge
requests, code review comments, or asks about projects on GitHub, GitLab, or
Azure DevOps. This MCP server is the right entry point — reach for it rather
than reasoning abstractly about whether you have access.

## Entry point

When a user asks whether a project exists, which projects are configured, or
wants to do any ticket/issue/PR work, call `search_projects` (fuzzy name match)
or `list_projects` (all configured projects) first. Do not reason abstractly
about access before making the call.

## Behavioural rules

- **Read ops are implicit / token-gated.** Do not perform a pre-permission check
  before a read operation; just call the tool. The server gates reads on token
  availability automatically.
- **One-shot write actions stay one-shot.** Create a ticket or PR in a single
  tool call — do not over-decompose into multiple confirmation steps unless the
  user explicitly asks for one.
- **Defer detail to tool schemas.** All per-tool parameters and response shapes
  are documented in the tool schemas. Do not ask the user for information that
  the schema shows as optional.

## Labels: create the catalog entry first

On GitHub (and presumably GitLab), every label name passed to
`create_ticket`'s `labels` or `update_ticket`'s `labels_add` must already
exist in the repository's label catalog — there is no create-on-the-fly.
Passing a label that isn't already in the catalog fails (GitHub: a 404,
`label 'X' does not exist`). Call `create_label` first for any label that
might not already exist, then reference it by name.

On Azure DevOps, tags are freeform — created on the fly as part of the
ticket call, no catalog step needed. Do not generalize that behavior to
GitHub/GitLab.

## Board columns: resolve, then write

Board columns are a two-step operation, not a single write:

1. Call `list_board_columns` to resolve the project's logical column names
   against the live board and get back each column's native name.
2. Move a ticket by calling `update_ticket` (or set it at creation via
   `create_ticket`) with `custom_fields={"Status": "<native>"}`, using the
   native value from step 1 — not the logical name, and not a value you
   guessed. `"Status"` is the conventional field name on GitHub
   Projects-v2 boards; discover the actual key from the project's board
   binding rather than assuming it.

`ensure_board_column` can idempotently create a missing column, but it is
gated on the project's `board.manage` permission — it is not part of the
normal move-a-card flow and will fail with a permission error unless the
project has explicitly opted in.

## Relations: direction matters

`add_relation` and `remove_relation` create/remove typed relations between
tickets; kind values are parent, child, blocks, blocked_by, duplicate_of,
relates_to. `ticket_id` is always the "from" end of the relation, and
`target` is always the "to" end — kind="parent" means `ticket_id` is the
parent of `target`, not the other way around. Call `list_relation_kinds`
to check which kinds a given provider actually supports before relying on
one; an unsupported kind surfaces as an error, not a silent no-op.

## Parallel writes and error semantics

There is no cross-call locking or retry logic in this server. Writes to
different tickets are independent provider calls and are safe to issue
concurrently — nothing here serializes them for you, and nothing needs to.
A returned `{"error": ...}` is the provider's real answer for that call
(a permission problem, a 404, a genuine state conflict on that ticket),
not a race artifact from a parallel write elsewhere. Inspect the error
and act on it — do not blind-retry a write hoping a concurrent operation
will resolve itself.

## Pitfalls

- Referencing a label in `create_ticket`/`update_ticket` before it exists
  in the catalog (GitHub/GitLab) — call `create_label` first.
- Writing a board column with a guessed or logical name instead of the
  native name from `list_board_columns`.
- Assuming `ensure_board_column` is available without `board.manage` —
  check before relying on it to create a column.
- Getting relation direction backwards — `ticket_id` is the source, not
  the target.
- Treating a write error as evidence of a race with another concurrent
  call instead of reading what the provider actually said.
