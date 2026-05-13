# agent-project-issues

Let your AI coding agent read and write **GitHub/GitLab issues** safely. MCP tools for listing/creating/updating tickets and adding comments, with automatic safety markers (`ai-generated` / `ai-modified` labels, `#ai-generated` comment prefix). Per-project permissions and read-only auto-discovery from the local git remote when no config exists.

## Quick install

```
/plugin marketplace add Seretos/agent-marketplace
/plugin install agent-project-issues@agent-marketplace
```

Self-contained `.exe` — no Python, no `pip install`.

## Read-only setup (zero config)

Put your token once in `~/.claude/settings.json`:

```json
{
  "env": {
    "GITHUB_TOKEN": "ghp_…"
  }
}
```

In any repo with a `github.com` `origin` remote, the plugin auto-discovers a single read-only project (`id="_auto"`) using that token. Use `list_projects` / `list_tickets` / `get_ticket` right away — no further setup. GitLab works the same way with `GITLAB_TOKEN`.

## Write access (per-project)

Drop a `.claude/project-issues.toml` next to your repo:

```toml
env_file = ".env"   # optional

[[projects]]
id          = "acme-backend"
description = "Acme main backend"
provider    = "github"
owner       = "acme"
repo        = "backend"
token_env   = "GITHUB_TOKEN_ACME"

[projects.permissions.issues]
create = true
modify = true

[projects.permissions.pulls]
create = true
modify = false
merge  = false
```

Each `[[projects]]` table can reuse the global `GITHUB_TOKEN` or scope to a per-project token (`token_env = "GITHUB_TOKEN_ACME"`) — the env var name is just a pointer; the token value itself is read from the process environment.

Read access is always implicit (token-gated). Each write namespace has its own flags:

- `permissions.issues.create` / `permissions.issues.modify` — gate `create_ticket` / `update_ticket` / `add_comment` / `update_comment`.
- `permissions.pulls.create` / `permissions.pulls.modify` / `permissions.pulls.merge` — gate `create_pr` / `update_pr`+`add_pr_comment` / `merge_pr` respectively. `pulls.merge` defaults to false even when the other PR flags are true — opt in deliberately.

#### Legacy flat form (deprecated)

The previously-shipped flat shape is still accepted; it auto-migrates on load and emits a single `DeprecationWarning` to the server log:

```toml
permissions = { create = true, modify = true }
# extended flat form (also accepted):
permissions = { create = true, modify = true, pr_create = true, pr_modify = false }
```

There is no flat equivalent for `pulls.merge` — existing configs cannot merge PRs without an explicit nested opt-in.

## Alternative installs

### From GitHub Releases
Download `project-issues-plugin-<version>.zip` from [Releases](https://github.com/Seretos/agent-project-issues/releases), unpack, then `/plugin install <path>`.

### From the release branch
```
git clone --branch release --depth 1 https://github.com/Seretos/agent-project-issues.git
```

### Build from source
Requires Python 3.11+.

```powershell
git clone https://github.com/Seretos/agent-project-issues.git
cd agent-project-issues
py -3 -m pip install -e ".[build]"
.\scripts\build.ps1 -Clean -Package
```

## Notes
- GitLab support is stubbed today (`tools/tickets.py` only resolves `github`). Extending it means implementing a `GitLabProvider`.
- AI-marker labels (`ai-generated`, `ai-modified`, `ai-closed-not-planned`) are created lazily on first write to a repo.
- `get_ticket` returns typed `relations` (parent / child / closes / closed_by / duplicate_of / duplicated_by / mentions / mentioned_by) alongside the ticket and comments. Cross-repo refs are formatted as `owner/repo#N`. Pass `include_relations=false` to skip the two extra API calls when relation context isn't needed; `relations_truncated=true` signals that the timeline had more pages than were fetched.
- `list_tickets` accepts an extended filter set beyond the basics (`status`, `labels`, `assignee`, `search`, `limit`): `not_labels` (exclude), `author`, `created_after` / `created_before`, `updated_after` / `updated_before` (ISO dates), plus `sort_by` (`created` / `updated` / `comments`) and `sort_order` (`asc` / `desc`). When any of the exclusion / author / date filters is set the provider switches from the cheap `/repos/.../issues` endpoint to GitHub's Search API, which has its own rate-limit bucket (30 req/min); the default-fast path stays on the legacy endpoint.
- Comment tools mirror the ticket surface: `add_comment` (write, gated by `modify`), `list_comments` and `get_comment` (read-only), and `update_comment` (write, gated by `modify`). `update_comment` re-applies the `#ai-generated` prefix to the new body so any AI edit stays labelled.
- `list_tickets_across_projects` fans the standard list filters (`status`, `labels`, `not_labels`, `assignee`, `author`, `search`, `limit_per_project`) across multiple projects in one call. Pass `project_ids=None` (the default) to query every configured project, or a subset. The call is partial-failure tolerant: an error on one project (missing token, permission denied, API failure, unknown id) is recorded in `results[project_id].error` and in the top-level `errors` list without aborting the rest.

## PR tools

Pull-request surface mirrors the ticket surface and is gated by the `permissions.pulls.*` namespace:

- `list_prs(project_id, status, labels, assignee, head, base, search, limit)` — read-only. The default-fast path hits `/pulls`; setting `labels`, `assignee`, or `search` switches to GitHub's Search API (`/search/issues` with `is:pr`), which has its own 30 req/min rate-limit bucket.
- `get_pr(project_id, pr_id)` — returns the PR plus its issue-style discussion comments (inline review comments live on a separate endpoint and are not surfaced).
- `create_pr(project_id, title, body, head, base, draft, labels, assignees)` — gated by `pulls.create`. Applies the `ai-generated` label automatically.
- `update_pr(project_id, pr_id, title, body, status, base, labels_add, labels_remove, assignees_add, assignees_remove)` — gated by `pulls.modify`. `status` accepts only `"open"` / `"closed"`; merging is a separate tool. The `ai-modified` label is added when the PR wasn't originally AI-generated.
- `add_pr_comment(project_id, pr_id, body)` — gated by `pulls.modify`. The body is automatically prefixed with `#ai-generated\n\n`.
- `merge_pr(project_id, pr_id, merge_method, commit_title, commit_message)` — gated by `pulls.merge`. `merge_method` is `"merge"`, `"squash"`, or `"rebase"`. Existing configs cannot merge without an explicit `pulls.merge = true` opt-in (no flat-form equivalent).
