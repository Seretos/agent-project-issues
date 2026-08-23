---
name: project-issues
description: >
  Manage tickets, issues, bugs, backlog items, pull requests, merge requests,
  code review comments, CI/CD pipeline and build results, label catalogs,
  ticket relations (blocking/dependency/sub-task hierarchy), and project
  board columns on GitHub, GitLab, or Azure DevOps. Use when the user
  mentions: ticket system, issue tracker, project board, PR, pull request,
  merge request, code review, pipeline, CI, build failure, job log, label,
  blocks, blocked_by, relation, dependency, board column, sub-task, epic,
  hierarchy, parent/child, GitHub, GitLab, Azure DevOps, or their German
  equivalents — "Ticket", "Issue", "Bug", "Backlog", "Projektboard",
  "Pull Request", "Merge Request", "Code-Review", "Pipeline", "CI kaputt",
  "warum ist der Build rot", "Label", "Label anlegen", "blockiert",
  "hängt ab von", "Board-Spalte", "Spalte verschieben", "Unteraufgabe",
  "Epic", "Teilaufgabe", "leg ein Ticket an", "erstelle ein Issue",
  "welche Projekte gibt es", "öffne ein Issue", "zeig mir offene Tickets",
  "was sind die offenen PRs".
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

## Tool map

Every registered tool, grouped by module — a discovery index. The sections
below go deep only on pipelines, labels, relations/hierarchy, and custom
fields/boards, since those are otherwise the easiest capabilities to miss.

- **projects** — `list_projects` list all configured projects;
  `search_projects` fuzzy-match a project by name.
- **tickets** — `list_tickets` list/filter a project's tickets;
  `get_ticket` fetch one ticket's full detail; `create_ticket` open a
  new ticket; `update_ticket` edit fields, labels, or board column;
  `list_ticket_statuses` discover valid status values;
  `list_custom_fields` discover the provider's field schema;
  `list_board_columns` resolve board columns to native names;
  `ensure_board_column` idempotently create a board column;
  `add_comment` post a comment on a ticket.
- **comments** — `list_comments` list a ticket's comments;
  `get_comment` fetch one comment; `update_comment` edit a comment's
  body; `delete_comment` remove a comment.
- **bulk** — `list_tickets_across_projects` list tickets spanning
  multiple projects at once.
- **pulls** — `list_prs` list/filter pull or merge requests;
  `get_pr` fetch one PR's full detail; `create_pr` open a new PR;
  `update_pr` edit a PR's fields; `add_pr_comment` post a PR-level
  comment; `add_pr_review_comment` post an inline code-review comment;
  `submit_pr_review` submit an approve/request-changes review;
  `merge_pr` merge an approved PR.
- **pipelines** — `list_pipeline_runs` list CI/CD runs by
  branch/tag/commit/ticket; `get_pipeline_run` fetch one run's detail
  and failures; `get_pipeline_step_log` fetch a failing job's bounded
  log; `trigger_pipeline` dispatch a new pipeline run;
  `get_ref` resolve a branch/tag/commit to its commit sha;
  `list_releases` list a project's published releases.
- **relations** — `add_relation` create a typed relation between
  tickets; `remove_relation` remove a typed relation;
  `list_relation_kinds` discover supported relation kinds;
  `list_hierarchy` read a ticket's parent/children in one call.
- **labels** — `list_labels` list a repo's label catalog;
  `create_label` add a new label; `update_label` rename or recolour a
  label; `delete_label` remove a label.

## Behavioural rules

- **Read ops are implicit / token-gated.** Do not perform a pre-permission check
  before a read operation; just call the tool. The server gates reads on token
  availability automatically.
- **One-shot write actions stay one-shot.** Create a ticket or PR in a single
  tool call — do not over-decompose into multiple confirmation steps unless the
  user explicitly asks for one.
- **Schemas carry parameters; this skill carries sequencing.** All per-tool
  parameters and response shapes are documented in the tool schemas — don't
  ask the user for information the schema marks optional. This skill instead
  carries the non-obvious operational facts and cross-tool sequencing schemas
  alone don't convey: the pipeline drill-down chain, label rename semantics,
  relation direction, and board write keys.

## Labels: create the catalog entry first (GitHub only)

On GitHub, every label name passed to `create_ticket`'s `labels` or
`update_ticket`'s `labels_add` must already exist in the repository's
label catalog — there is no create-on-the-fly. Passing a label that
isn't already in the catalog fails with a 404, `label 'X' does not
exist`. Call `create_label` first for any label that might not
already exist, then reference it by name.

GitLab does NOT share this restriction: an unknown label is created
on the fly as part of the `create_ticket` / `update_ticket` call. On
Azure DevOps, tags are likewise freeform — created on the fly, no
catalog step needed. Do not generalize the GitHub catalog
requirement to GitLab or Azure DevOps.

The full catalog CRUD surface is `list_labels` (read, token-optional —
works on public repos without a token), plus `create_label`,
`update_label`, and `delete_label` (the three writes require
`issues.modify`).

On `update_label`, `name` is required and looks the label up — it is a
lookup key only and is never itself mutated. To rename a label, pass the
new name as `new_name`; leave `new_name` unset to keep the current name
and only change `color`/`description`. At least one of `new_name`,
`color`, or `description` must be supplied, or the call errors before
making any HTTP request.

Color formats are provider-specific: GitHub wants a bare 6-digit hex
string with no leading hash, validated locally before the API call;
GitLab wants a leading-hash `#RRGGBB` form (a bare 6-hex string is also
accepted and normalized); Azure DevOps has no color concept at all.

`create_label`, `update_label`, and `delete_label` always return an
error containing "not supported" on Azure DevOps — tags there are
freeform, not a mutable catalog entry. Use the freeform-tag workaround
above instead of trying to manage an Azure label catalog.

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
project has explicitly opted in. However, on GitLab the call reports the provider as unsupported before any permission check, since GitLab has no board concept to manage in the first place.

## Custom fields and board write keys

`list_custom_fields` discovers a provider's structured field schema —
Azure DevOps is the primary use case (typed fields, picklist
constraints, optional `work_item_type` scoping). On GitHub and GitLab
it always returns `"fields": []` — a stable fact about those
providers, not an error or a reason to retry; they have no structured
field schema. Valid `work_item_type` names vary by Azure process
template, so call with `work_item_type` unset first to discover which
types exist before scoping to one. The returned `reference_name` and
`allowed_values` feed directly into `update_ticket`'s `custom_fields`
parameter — call `list_custom_fields` first to discover valid field
references before setting them.

This intersects with the board-column flow above: on GitHub the board
write key is not discoverable via `list_custom_fields` (it returns an
empty `fields` list there too) — it is the conventional `"Status"` key
with the native value from `list_board_columns`, as already described.
GitLab has no board concept at all (`list_board_columns` returns an
empty `columns` list); a missing or misconfigured `board` block on a
provider that does support boards (GitHub, Azure DevOps) raises a
descriptive error instead of silently returning empty — fix
`projects.yml` rather than treating it as transient.

## Relations: direction matters

`add_relation` and `remove_relation` create/remove typed relations between
tickets; kind values are parent, child, blocks, blocked_by, duplicate_of,
relates_to. `ticket_id` is always the "from" end of the relation, and
`target` is always the "to" end — kind="parent" means `ticket_id` is the
parent of `target`, not the other way around. Call `list_relation_kinds`
to check which kinds a given provider actually supports before relying on
one; an unsupported kind surfaces as an error, not a silent no-op.

`list_relation_kinds`' response also carries `read_only_kinds` — relation
kinds that appear in `get_ticket`'s output (for example `mentions` and
`closed_by`) but are derived automatically; never pass one of these to
`add_relation` or `remove_relation`. It also carries a `provider_support`
matrix — check that matrix instead of learning provider gaps from failed
calls.

`list_hierarchy` is a one-call projection of a ticket's parent/child
(epic) structure: it makes exactly the same single
`get_ticket(include_relations=True)` call and resolves nothing extra,
returning `parent` (the single parent relation, or null) and `children`
(the list of child relations, or an empty list). `relations_truncated`
mirrors `get_ticket`'s field of the same name — when true, `children`
may be incomplete because the underlying timeline had more pages than
were fetched.

## Pipelines: drill down, don't guess

CI/pipeline triage is a three-tool chain, each step narrowing scope:

1. `list_pipeline_runs` lists CI/CD runs for a project. Exactly one
   addressing argument must be set — `branch`, `tag`, `commit_sha`,
   `ticket_id`, or `recent` — passing zero or more than one is an
   error. `hint` is populated whenever the run list comes back empty,
   regardless of which addressing argument was used.
2. `get_pipeline_run` fetches one run's detail — pass the run id from
   step 1 as `run_id`. `run_id` is numeric but typed as a string:
   always pass it quoted (e.g. "9876543210"), never as a bare integer.
   Only this tool returns the `failure` block on the run (failing
   jobs, annotations, log excerpt), and only when the run's
   `conclusion` is "failure".
3. `get_pipeline_step_log` fetches one failing job's full log, bounded
   to a small slice, when step 2's `log_excerpt` (~30 lines) isn't
   enough. `job_id` must come from `run.failure.failing_jobs[].job_id`
   — never construct or guess it. `mode` defaults to `around_failure`
   (a window centred on the first error-looking line); when no such
   line is found it degrades to tail behaviour and reports back mode
   "around_failure->tail" so the degradation is visible. Output is
   always bounded by `max_lines` (hard-capped at 1000) — this tool
   never returns the full unbounded log.

GitLab has no structured CI annotations — `annotations` on a GitLab
failing job is always empty. `log_excerpt` is the only failure context
available there; don't wait for annotations that will never appear.

`list_pipeline_runs` also accepts `workflow` / `event` / `since`
filters that combine with any addressing argument — they are filters,
not a sixth addressing mode. `workflow` matches by name (a bare name
and a `.yml`/`.yaml` filename are equivalent); `event` accepts the
canonical vocabulary (`manual`, `push`, `schedule`, `pull_request`,
`api`) resolved to each provider's native string; `since` is an
ISO-8601 timestamp and returns a structured error rather than silently
filtering everything out when malformed.

**Triggering a pipeline run** is a dispatch-then-poll flow, gated by
`pipelines.trigger` (defaults to `false` on every existing config — a
new namespace with no flat-form equivalent, opt in deliberately):

1. `trigger_pipeline(project_id, workflow, ref, inputs)` dispatches the
   run. `wait_for_run=True` (default) polls for the resulting run for
   up to `wait_timeout_seconds` (hard-capped at 120s) and returns it
   under `run`.
2. If the poll times out, the dispatch has still succeeded —
   `trigger_pipeline` degrades instead of raising: `run` comes back
   `None` and `hint` says to poll `list_pipeline_runs` /
   `get_pipeline_run` afterwards rather than re-triggering. The same
   degraded `{run: None, hint}` shape applies when `wait_for_run=False`
   is passed explicitly (no polling attempted at all). `triggered`
   stays `True` in both cases — it reflects that the dispatch request
   itself succeeded, independent of whether the run could be resolved.
3. Once you have a `run_id` (either from `trigger_pipeline`'s `run` or
   from a follow-up `list_pipeline_runs`/`get_pipeline_run` poll), the
   normal drill-down chain above applies.

`get_ref(project_id, ref)` resolves a branch, tag, or commit sha to its
peeled commit sha and reports which kind it resolved as (`branch` /
`tag` / `commit`; resolution order is branch -> tag -> commit).
`list_releases(project_id, limit)` lists published releases, most
recent first. Both are read-only (token-gated only, no permission
flag) — unlike `trigger_pipeline`, they work even on a project with
every permission flag set to `false`.

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
- Constructing or guessing a `job_id` instead of reading it from a
  failing job's entry in `get_pipeline_run`'s failure block.
- Passing `run_id` as a bare integer instead of a quoted string.
- Treating GitHub/GitLab's empty `list_custom_fields` result as an
  error or a reason to retry — it's a stable fact about those
  providers.
- Calling `update_label` with a new value in `name`, expecting it to
  rename the label — `name` is a lookup key only; use `new_name`.
- Expecting `create_label`/`update_label`/`delete_label` to work on
  Azure DevOps — they always error there; use the freeform-tag
  workaround instead.
