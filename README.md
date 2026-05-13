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
permissions = { create = true, modify = true }
```

Each `[[projects]]` table can reuse the global `GITHUB_TOKEN` or scope to a per-project token (`token_env = "GITHUB_TOKEN_ACME"`) — the env var name is just a pointer; the token value itself is read from the process environment.

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
