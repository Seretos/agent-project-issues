<img src="assets/icon.png" width="96" alt="agent-project-issues icon">

# agent-project-issues

Let your AI coding agent read and write **GitHub / GitLab / Azure DevOps issues** safely. MCP tools for listing/creating/updating tickets and adding comments, with automatic safety markers (`ai-generated` / `ai-modified` labels, `#ai-generated` comment prefix). Per-project permissions and automatic project discovery from provider tokens and the local git remote when no config exists.

## Quick install

```
/plugin marketplace add Seretos/agent-marketplace
/plugin install agent-project-issues@agent-marketplace
```

Self-contained `.exe` — no Python, no `pip install`.

## Read-only setup (zero config)

Put your token once in `~/.claude/settings.json` (the Claude Code host file — that path is fixed by Claude Code itself and unrelated to the plugin's own `.seretos/` directory):

```json
{
  "env": {
    "GITHUB_TOKEN": "ghp_…"
  }
}
```

In any repo with a `github.com` `origin` remote, the plugin auto-discovers a project from the CWD's git remote. Use `list_projects` / `list_tickets` / `get_ticket` right away — no further setup. GitLab works the same way with `GITLAB_TOKEN`.

If no `.seretos/projects.yml` config file is found (neither walking up from the working directory nor at `~/.seretos/`) but a provider token is set, **token-discovery** kicks in: the plugin enumerates every repository accessible to that token and surfaces all of them as managed projects. A broad PAT will surface all repos it can reach — see [Token-discovery: broad-PAT exposure](SECURITY.md#token-discovery-broad-pat-exposure) for the implications.

## Write access (per-project)

Drop a `.seretos/projects.yml` at the root of your repo (a `.yaml` extension also works):

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

#### GitLab — self-hosted and gitlab.com

```yaml
version: 1

projects:
  - id: acme-gitlab
    description: Acme backend on self-hosted GitLab
    provider: gitlab
    path: acme/backend                  # full namespace; supports groups
    base_url: https://gitlab.example.com  # omit for gitlab.com
    token_env: GITLAB_TOKEN_ACME
    permissions:
      issues:
        create: true
        modify: true
      pulls:                            # GitLab calls them "merge requests"
        create: true
        modify: true                    # merging is a separate flag below
        merge: false
```

GitLab differences worth knowing:

- **PAT scopes** — the provider asks GitLab to introspect the token's scopes via `/personal_access_tokens/self`. A token with the `api` scope grants the full write surface (`issues.*`, `pulls.*`, `merge_pr`). Anything else (`read_api`, `read_repository`, etc.) is treated as read-only, with `permissions_probe_error: "insufficient_scope"` on the project record.
- **Status state-space** — GitLab issues only have `open` ↔ `closed`. The `closed:completed` / `closed:not_planned` distinction from GitHub collapses to `closed`; the `ai-closed-not-planned` label is the agent-side convention for "won't do" semantics. `update_ticket(status="closed:not_planned")` still works — it maps to `state_event="close"` and the caller should add the marker label via `labels_add` for the same effect.
- **Merge strategies** — `merge_pr(merge_method="merge")` and `merge_pr(merge_method="squash")` are native. `merge_method="rebase"` is rejected with a clear error because GitLab's rebase is a separate flow (a `PUT .../rebase` endpoint that doesn't merge); callers wanting rebase-first-merge should call the rebase endpoint and then `merge_pr(merge_method="merge")`.
- **Comment ids** — GitLab notes are scoped per issue/MR, unlike GitHub's repo-wide comment ids. `get_comment` / `update_comment` accept the composite form `"<issue_iid>/<note_id>"` (e.g. `"5/99"`); a bare note id surfaces a clear error.
- **Pipelines** — `list_pipeline_runs(ticket_id=...)` walks the issue's related MRs and aggregates their pipelines. `get_pipeline_run(include_failure_context=true)` fetches the failing jobs' traces (last ~4KB tail) but does not surface GitHub-style structured annotations — GitLab has no equivalent surface, so `annotations: []` on every failing job.

#### Azure DevOps — work items, PRs, threads

```yaml
version: 1

projects:
  - id: acme-ado-web
    description: Frontend repo in the acme ADO project
    provider: azuredevops
    # organization/project/repository — three segments, all required.
    # Work items live at organization/project; PRs at the full path.
    path: acme/frontend-stack/web
    # base_url is optional; defaults to https://dev.azure.com.
    # Set for Azure DevOps Server (on-prem) installations.
    # base_url: https://devops.acme.internal
    token_env: AZURE_DEVOPS_TOKEN_ACME
    # Optional: which work-item type does `create_ticket` create?
    # default_work_item_type: Bug
    permissions:
      issues:
        create: true
        modify: true
      pulls:
        create: true
        modify: true
        merge: false
```

Azure DevOps differences worth knowing:

- **Path scope** — work items are scoped to `organization/project`, pull requests to the full `organization/project/repository`. Two YAML entries that share an `organization/project` prefix see the **same** work-item backlog and differ only in which repository their PR operations target.
- **Auth** — PAT via HTTP Basic with empty username (`":{PAT}"`). The provider can't enumerate PAT scopes through the REST API, so `permissions:` in YAML is the source of truth for what's allowed.
- **Status state-space** — each Azure DevOps project picks a *process template* (Basic, Agile, Scrum, CMMI, or a custom one) that defines its own states (`To Do`/`Doing`/`Done` for Basic, `New`/`Active`/`Resolved`/`Closed`/`Removed` for Agile, etc.). `list_ticket_statuses` discovers the live state-space for the default work-item type — call it before `update_ticket(status=...)` if you're not sure which strings are valid.
- **Work-item type for `create_ticket`** — set `default_work_item_type` in YAML to pin (`Bug`, `Issue`, `User Story`, …). When unset the provider auto-picks the first match from `Issue → Bug → User Story → Product Backlog Item → Requirement` against the project's actual types.
- **Bodies + comments are HTML on the wire** — the provider converts to/from markdown automatically so the agent sees the same shape as on GitHub/GitLab. The `#ai-generated` marker line is preserved across the round-trip.
- **PR comments** — Azure DevOps has no flat "issue comments" vs "review comments" split. Everything is a *thread* hanging off the PR. Threads without `threadContext` surface as `Comment`s (top-level discussion); threads with `threadContext` surface as `ReviewComment`s (diff-anchored). Replies post into the existing thread's id (`in_reply_to`).
- **Relations** — `parent` / `child` map to `Hierarchy-Reverse` / `Hierarchy-Forward`, `blocks` / `blocked_by` to `Dependency-Forward` / `-Reverse`, `duplicate_of` to `Duplicate-Forward`, `relates_to` to `Related`. Cross-project relation writes are rejected (parity with GitHub/GitLab).
- **Pipelines** — `list_pipeline_runs` reads from the classic Build REST surface (`/_apis/build/builds`). `list_runs_for_ticket` walks the work-item's `ArtifactLink` relations to find associated builds. Failure context is the last ~120 lines of the failing job's log; like GitLab, no structured Check-Run annotations.

### Where the loader looks for the config

Resolution happens in three legs, in order. **The first leg that produces results wins entirely — subsequent legs are skipped.**

**Leg 1 — Config file (explicit or discovered):**

1. `$PROJECT_ISSUES_CONFIG` (explicit override; missing path = hard error).
2. `<enclosing-git-repo>/.seretos/projects.{yml,yaml}`. The walk finds the nearest `.git`-bearing ancestor, checks its `.seretos/`, then jumps *out* of that repo (next iteration starts above its root). Repeats project-by-project until no enclosing repo exists.
3. `~/.seretos/projects.{yml,yaml}` (user-level fallback).

Configs are **not merged** — the first match wins entirely. Higher/outer configs are ignored. If a project is not in the winning config, the agent has no access to it. Finding a config file also **disables all further discovery** (legs 2 and 3 are not run).

**Leg 2 — Token-discovery:**

If no config file is found but a provider token is present (`GITHUB_TOKEN`, `GITLAB_TOKEN`, or `AZURE_DEVOPS_TOKEN`), the lib enumerates every repository/project accessible to that token and emits them all as managed projects with `source="token-discovery"`. Permissions on each discovered project are pre-populated by the lib from the token's effective rights — **no additional probe is issued**. The result list is capped by a hard-coded lib constant (not user-configurable); when the cap is hit, `list_projects`/`search_projects` surface a `hint` field describing the truncation. Token-discovery bypasses leg 3: if the enumeration returns anything (even a partial list), no git-remote `_auto` project is added.

**Leg 3 — Git-remote auto-discovery:**

If no config file was found and token-discovery yielded nothing, the CWD's git remote is used to produce one project entry (`id="_auto"`, `source="git-remote"`). Permissions are derived from the token's effective rights via a live probe, not assumed — with a working token the project can surface with write flags if the token permits them.

**State semantics:**

- `state="ok"` — projects are available. This no longer implies a config file was found: token-discovery can produce `state="ok"` with no config file on disk.
- `state="no_config"` — no config file found **and** no token-discovery results **and** no git remote yielded a project.
- `state="config_empty"` — a config file was found but it declares no projects.
- `state="config_error"` — a config file was found but failed to parse.

### Known limitation: GitHub Copilot CLI

In Claude Code CLI the host passes the user's working directory to the MCP, so the project-boundary walk lands on the repo's `.seretos/`. **GitHub Copilot CLI** does not pass a usable CWD — it spawns the MCP from the plugin install dir. Two workarounds:

- **Recommended:** put your config in `~/.seretos/projects.yml`. The user-level fallback is the dedicated escape hatch.
- **Per-project:** export `PROJECT_ISSUES_PLUGIN_CWD=$(pwd)` before launching `copilot`. The plugin reads it as the search root.

Each `projects[]` entry can reuse the global `GITHUB_TOKEN` or scope to a per-project token (`token_env: GITHUB_TOKEN_ACME`) — the env var name is just a pointer; the token value itself is read from the process environment.

## Security hardening

`.seretos/projects.yml` grants an agent write permissions over your repositories. A dotfile path is **not** a security boundary against a write-capable agent — an agent with file-write access to the working tree can overwrite the config to broaden its own permissions.

The robust mitigation is a Claude Code **managed-settings deny rule** placed in an admin-owned OS location that the agent's file tools cannot reach:

```json
{
  "permissions": {
    "deny": [
      "Edit(**/.seretos/projects.yml)",
      "Write(**/.seretos/projects.yml)",
      "Edit(**/.seretos/projects.yaml)",
      "Write(**/.seretos/projects.yaml)"
    ]
  }
}
```

Place this in the OS-managed settings file (filename `managed-settings.json`):

| Platform | Path |
|---|---|
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux / WSL | `/etc/claude-code/managed-settings.json` |

**Important caveats:**

- `~/.claude/settings.json` is within the agent's write reach and is **not** a substitute — deny rules placed there can be overwritten by the agent.
- This rule protects **integrity** — it prevents the agent from overwriting the config to escalate its own permissions. `Read` is intentionally not denied: the config contains no secrets, and the agent can already see permission flags via `list_projects`.
- The `**/.seretos/` glob covers both per-project configs and the user-level fallback (`~/.seretos/projects.yml`) when the agent's working root encompasses the home directory.
- Deny rules bind Claude Code's own file tools (and recognized file commands in Bash). They do **not** sandbox arbitrary subprocesses (e.g. a Python script that opens the file directly) — OS-level sandboxing (e.g. `seccomp`, container restrictions) is required for that.

This plugin ships a `UserPromptSubmit` hook (`hooks/security_hint.py`) that periodically reminds the agent to surface these guidelines whenever a `.seretos/` directory is present in the project tree. The hint hook requires Python on PATH (the core plugin binary does not).

See [SECURITY.md](SECURITY.md) for the threat model.

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
| `provider`    | enum   | required | —             | `"github"`, `"gitlab"`, or `"azuredevops"`. |
| `path`        | string | required | —             | Provider-native repo path. GitHub: `"owner/repo"`. GitLab: full namespace, e.g. `"group/sub/project"`. Azure DevOps: `"organization/project/repository"`. |
| `base_url`    | string | optional | provider host | Self-hosted base URL — GitLab (`https://gitlab.example.com`) or Azure DevOps Server (`https://devops.example.com`). Ignored for GitHub. |
| `token_env`   | string | optional | provider default | Name of the env var holding the API token for this project (e.g. `"GITHUB_TOKEN_ACME"`). When omitted the loader falls back to `GITHUB_TOKEN` / `GITLAB_TOKEN` / `AZURE_DEVOPS_TOKEN`. |
| `default_work_item_type` | string | optional | discovered | Azure DevOps only. Which work-item type `create_ticket` creates (e.g. `"Bug"`). When omitted the provider auto-picks the first match from `Issue → Bug → User Story → Product Backlog Item → Requirement`. |
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

Before v1 the config lived in `.seretos/project-issues.toml` (originally `.claude/project-issues.toml` — both folder names have since been retired) and used a flat `permissions = { ... }` table plus split `owner` / `repo` fields. The format changed in a single breaking step — no auto-converter, no fallback. Migration is a literal field-by-field copy. The new filename is `projects.yml` (the old `project-issues.yml` name is no longer recognised by the plugin).

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

**After — `.seretos/projects.yml`:**

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
- **File extension `.yml` / `.yaml`, filename `projects.yml`** — the `.toml` filename and the old `project-issues.yml` name are no longer recognised. Rename the file along with the migration.

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

## AI-attribution markers

Every AI-authored write is tagged so a human can audit, filter, or auto-close it. The convention is **two layers, body prefix is canonical**:

1. **Body prefix `#ai-generated\n\n`** (source of truth) — prepended to issue / PR bodies and to comments. Always lands regardless of the caller's GitHub permissions because the body is just text the issue-creator controls. Idempotent: re-applying the prefix is a no-op.
2. **`ai-generated` / `ai-modified` labels** (best-effort decoration) — applied when the caller has enough permission on the target repo (`push` to create the label, `triage` to apply it). If GitHub refuses (403) the label is dropped from the request and the operation proceeds; the body-prefix marker remains the durable record. A silent label-drop on the `POST /issues` response (GitHub returns 201 but strips the label for callers without `triage`) is detected after the fact and logged at WARNING.

Downstream tooling should treat the body prefix as authoritative for AI-attribution and the label as a convenience indicator that may be missing for external contributors. See `Seretos/agent-marketplace#15` for the history.

## Notes
- GitLab is supported alongside GitHub. Issues, merge requests (mapped to the common PR surface), notes, and pipelines all flow through the same tool calls — `provider: gitlab` plus a `path` (e.g. `group/sub/project`) is the only config-side difference. For self-hosted instances add `base_url: https://gitlab.example.com`. Status hints collapse `terminal_completed` and `terminal_declined` to `"closed"` because GitLab has no `state_reason`; agents wanting "not planned" semantics apply the `ai-closed-not-planned` label. See the GitLab notes block under "Per-project setup" below.
- AI-marker labels (`ai-generated`, `ai-modified`, `ai-closed-not-planned`) are created lazily on first write to a repo. Label create / apply failures are non-fatal — the body-prefix marker is the canonical source of truth.
- `get_ticket` returns typed `relations` (parent / child / closes / closed_by / duplicate_of / duplicated_by / mentions / mentioned_by) alongside the ticket and comments. Cross-repo refs are formatted as `owner/repo#N`. Pass `include_relations=false` to skip the two extra API calls when relation context isn't needed; in that case the response carries `relations_fetched=false` and omits the `relations` / `relations_truncated` keys (so an agent can tell "skipped" from "fetched but empty"). When relations are fetched, `relations_fetched=true` accompanies them and `relations_truncated=true` signals that the timeline had more pages than were fetched. Comment slicing mirrors this: `include_comments=false` (or `comments_limit=0`) yields `comments_fetched=false` with the `comments` key omitted.
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
