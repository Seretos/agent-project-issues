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
