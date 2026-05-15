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

Drop a `.claude/project-issues.yml` next to your repo (a `.yaml` extension also works):

```yaml
version: 1
env_file: .env   # optional, resolved relative to this file

projects:
  - id: acme-backend
    description: Acme main backend
    provider: github
    path: acme/backend            # owner/repo for github, full namespace for gitlab
    token_env: GITHUB_TOKEN_ACME
    permissions:
      issues:
        create: true
        modify: true
      pulls:
        create: true
        modify: false
        merge: false
```

A complete example lives at [`config.example.yml`](./config.example.yml) in the repo root.

Each `projects[]` entry can reuse the global `GITHUB_TOKEN` or scope to a per-project token (`token_env: GITHUB_TOKEN_ACME`) — the env var name is just a pointer; the token value itself is read from the process environment.

Read access is always implicit (token-gated). Each write namespace has its own flags:

- `permissions.issues.create` / `permissions.issues.modify` — gate `create_ticket` / `update_ticket` / `add_comment` / `update_comment`.
- `permissions.pulls.create` / `permissions.pulls.modify` / `permissions.pulls.merge` — gate `create_pr` / `update_pr`+`add_pr_comment` / `merge_pr` respectively. `pulls.merge` defaults to false even when the other PR flags are true — opt in deliberately.

### Schema reference (v1)

Strict — unknown top-level / project / permissions keys are rejected with a clear error.

**Top-level:**

| Field      | Type   | Required | Default | Description |
|------------|--------|----------|---------|-------------|
| `version`  | int    | optional | `1`     | Schema version. Only `1` is accepted today. |
| `env_file` | string | optional | auto    | Path to a `.env` file relative to the config file. When omitted, the loader checks `<config-dir>/../.env` and then `<config-dir>/.env`. |
| `projects` | list   | required | `[]`    | List of project entries (see below). An empty list yields `state: "config_empty"`. |

**Per project (`projects[]`):**

| Field         | Type   | Required | Default       | Description |
|---------------|--------|----------|---------------|-------------|
| `id`          | string | required | —             | Stable, opaque identifier the agent uses to refer to the project. Reserved value `_auto` is rejected — it's only emitted by git-remote auto-discovery. |
| `description` | string | optional | `""`          | Human-readable description, shown by `list_projects`. |
| `provider`    | enum   | required | —             | `"github"` or `"gitlab"`. |
| `path`        | string | required | —             | Provider-native repo path. GitHub: `"owner/repo"`. GitLab: full namespace, e.g. `"group/sub/project"`. |
| `base_url`    | string | optional | provider host | Self-hosted GitLab base URL (e.g. `https://gitlab.example.com`). Ignored for GitHub. |
| `token_env`   | string | optional | provider default | Name of the env var holding the API token for this project (e.g. `"GITHUB_TOKEN_ACME"`). When omitted the loader falls back to `GITHUB_TOKEN` / `GITLAB_TOKEN`. |
| `permissions` | object | optional | all-false     | Permission flags (see below). Defaults make the project read-only. |

**`permissions`:**

| Field                  | Type  | Default | Gated tool(s) |
|------------------------|-------|---------|---------------|
| `issues.create`        | bool  | `false` | `create_ticket` |
| `issues.modify`        | bool  | `false` | `update_ticket`, `add_comment`, `update_comment` |
| `pulls.create`         | bool  | `false` | `create_pr` |
| `pulls.modify`         | bool  | `false` | `update_pr`, `add_pr_comment` |
| `pulls.merge`          | bool  | `false` | `merge_pr` (opt in deliberately) |

### Migrating from the previous `.toml` config

Before v1 the config lived in `.claude/project-issues.toml` and used a flat `permissions = { ... }` table plus split `owner` / `repo` fields. The format changed in a single breaking step — no auto-converter, no fallback. Migration is a literal field-by-field copy.

**Before — `.claude/project-issues.toml`:**

```toml
env_file = ".env"

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

**After — `.claude/project-issues.yml`:**

```yaml
version: 1
env_file: .env

projects:
  - id: acme-backend
    description: Acme main backend
    provider: github
    path: acme/backend
    token_env: GITHUB_TOKEN_ACME
    permissions:
      issues:
        create: true
        modify: true
      pulls:
        create: true
        modify: false
        merge: false
```

Key consolidation points:

- **`owner` + `repo` → `path`.** GitHub uses `"owner/repo"`; the old separate fields are no longer accepted (the strict schema rejects them).
- **`version: 1` is the new top-level field.** It defaults to `1` when omitted, but adding it explicitly future-proofs against later schema breaks.
- **`permissions` is a YAML mapping**, not a TOML inline-table. The legacy flat shape `permissions = { create = true, modify = true, pr_create = true, pr_modify = false }` is no longer accepted — split it into the nested `issues` / `pulls` sub-mappings shown above. There was never a flat equivalent for `pulls.merge`.
- **File extension `.yml` / `.yaml`** — the `.toml` filename is no longer recognised. Rename the file along with the migration.

Find-replace is enough for typical configs; the only structural change is the permissions / path collapse described above.

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

## Pipeline / CI tools

Read-only CI-run lookup against GitHub Actions. Both tools are token-gated only — no permission flag is required, mirroring `list_tickets` / `list_prs`. Works against `_auto`-discovered projects too.

- `list_pipeline_runs(project_id, branch?, tag?, commit_sha?, ticket_id?, status, limit)` — exactly one addressing argument must be set. `branch` filters Actions runs by branch name; `tag` resolves the tag ref to a commit SHA first; `commit_sha` filters by `head_sha`; `ticket_id` walks the ticket's timeline + body for linked PRs / branch hints, resolves them to head_shas, and aggregates runs (deduped by run id, sorted by `created_at` desc, capped to `limit`). When a ticket has no linked PR / branch, returns `runs=[]` plus a `hint` asking the user for a branch or commit. Each `PipelineRun` carries `id`, `name`, `branch`, `head_sha`, `event`, `status`, `conclusion`, `url`, `created_at`, `updated_at`, `run_attempt`.
- `get_pipeline_run(project_id, run_id, include_failure_excerpt=true)` — returns the run plus, for failed completed runs, a `failure` block listing each failing job (`name`, `url`, `failed_step`, GitHub check-run `annotations`, and a `log_excerpt` of ~30 lines around the first `Error` / `FAILED` / `##[error]` marker). 403/404 on the log endpoint degrades to `log_excerpt=null` and `note="logs unavailable"`. In-progress runs skip the failure fetch entirely.
