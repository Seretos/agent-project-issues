# Security Policy

## Threat model

`project-issues-plugin` is a **local** MCP server. It runs as a process launched
by an MCP client (typically Claude Code) on the same machine as the user, with
the user's own privileges. It does not listen on a network socket and is not
designed to be exposed beyond the host. Its only outbound traffic is HTTPS
requests to `api.github.com` (and, when GitLab support lands, `gitlab.com` or
a self-hosted base URL).

The trust boundary is the MCP client: anything that can reach the server's
stdio already runs as the user. The tools exposed here are accordingly
authority-equivalent to "the user runs commands themselves" — within the
scope of the API tokens they configure.

## Token handling

- Tokens are read from environment variables only. The plugin does NOT read
  arbitrary secret files; the `env_file` directive in TOML loads a `.env` into
  the process environment but never echoes its contents.
- Tokens are **never** included in tool responses. `list_projects` exposes
  `token_env` (the *name* of the variable) and `token_available: true/false`
  (a boolean), nothing more.
- Tokens are not logged. The startup log records the env-var name only when
  auto-discovery is invoked, never the value.
- `Authorization: Bearer <token>` headers are set per-request by the httpx
  client and stay inside the process.

## Permission gating

Every write tool in `tools/tickets.py` enforces project permissions **before**
making any HTTP call:

| Tool             | Gate                                |
| ---------------- | ----------------------------------- |
| `create_ticket`  | `permissions.create == true` AND token available |
| `update_ticket`  | `permissions.modify == true` AND token available |
| `add_comment`    | `permissions.modify == true` AND token available |
| `list_tickets`   | read-only — no gate                 |
| `get_ticket`     | read-only — no gate                 |

The permission flags on any project determine what the agent may do. How those
flags are populated depends on the project's source:

- **`source="config"`** — flags come directly from the `.seretos/projects.yml`
  entry (`permissions_source="config"`). Write access requires an explicit
  YAML entry with the relevant flag set to `true`.
- **`source="git-remote"`** — the `_auto` project produced from the CWD's git
  remote. When a token is present the plugin probes the token's effective rights
  against the repo and uses the result (`permissions_source="token-probe"`). A
  token with push access will therefore surface with write flags — these projects
  are **not** forced read-only.
- **`source="token-discovery"`** — permissions are pre-populated by the lib from
  the token's effective rights during enumeration (`permissions_source="token-discovery"`).
  No additional probe is issued; a PAT with write access grants write access through
  this plugin. See [Token-discovery: broad-PAT exposure](#token-discovery-broad-pat-exposure)
  below for details.

## Config-file protection (managed-settings deny rule)

**Threat vector:** an agent operating in a project that has a `.seretos/projects.yml` config file is write-capable within that project tree. A sufficiently unconstrained agent can overwrite the config to grant itself broader permissions (additional projects, escalated write flags) before its next tool call — the permission gate reads the config on every call, so an overwrite takes effect immediately.

**Mitigation:** place a Claude Code managed-settings deny rule in an **admin-owned OS location** that the agent's file tools cannot reach. The rule blocks Claude Code's own `Edit` and `Write` tools from modifying `.seretos/projects.yml` (and its `.yaml` extension variant). The `Read` tool is intentionally not denied — the config contains no secrets (tokens are env vars, not inlined in the file) and reading it is a legitimate agent operation.

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

**Caveats:**

- `~/.claude/settings.json` is within the agent's write reach and is **not** a substitute.
- This rule protects **integrity** — it prevents the agent from overwriting the config to escalate its own permissions. It does not address confidentiality: `Read` is intentionally not denied.
- Deny rules bind Claude Code's own file tools (and recognized file commands in Bash). They do **not** sandbox arbitrary subprocesses — an agent can still invoke `cp`, `tar`, a shell redirect, or any subprocess that writes the file directly. OS-level sandboxing (e.g. `seccomp`, container restrictions) is required for a stronger guarantee.
- The `**/.seretos/` glob in the rule covers both per-project configs (`.seretos/` inside a repo) and the user-level fallback (`~/.seretos/projects.yml`) whenever the agent's working root encompasses the home directory.

See the [Security hardening](README.md#security-hardening) section in README.md for setup steps.

## Token-discovery: broad-PAT exposure

When no `.seretos/projects.yml` config file is found and a provider token is
set (`GITHUB_TOKEN`, `GITLAB_TOKEN`, or `AZURE_DEVOPS_TOKEN`), the plugin
automatically enumerates every repository/project accessible to that token
(up to a hard-coded lib constant) and exposes them all as managed projects
with `source="token-discovery"`.

**Permissions are NOT forced read-only.** The lib pre-populates permissions
from the token's effective rights during enumeration. A PAT with write access
to many repositories grants write access to all of them through this plugin —
the agent can create tickets, post comments, open and merge pull requests on
any repo the token can reach.

**Opt-out:** drop a `.seretos/projects.yml` with an explicit `projects:` list.
A config file wins entirely and disables token-discovery for that directory
(legs 2 and 3 of the resolution order are skipped). An empty list
(`projects: []`) is enough to suppress discovery while you fill in the real
entries.

**Scope-down:** use a narrowly-scoped token — repo-specific fine-grained PATs
(GitHub) or read-only tokens — to limit how many repositories token-discovery
can reach and what it may write.

**Azure DevOps note:** ADO token-discovery requires a determinable organization
context. Without an org hint available to the lib, ADO repositories are not
enumerated by token-discovery.

## AI-attribution markers

Tickets and comments authored by this plugin carry stable, user-visible
markers (`ai-generated` / `ai-modified` labels, `#ai-generated` comment
prefix). These are NOT a security control — a sufficiently determined user
could strip them server-side. They are a transparency feature so the human
maintainers of a repo can audit what an AI agent touched.

## Out of scope

- Compromise of the host machine where the plugin runs (the user already
  owns it).
- Token leakage via the user's own shell history, environment dumps, or
  third-party tooling that scrapes `os.environ`.
- API-side abuse — the underlying GitHub/GitLab tokens have whatever scope
  the user granted them; the plugin doesn't escalate beyond those scopes.

## Reporting a vulnerability

For any token leak in tool output, log lines, or error messages — or any
case where a permission gate can be bypassed — open a GitHub issue with the
label `security` (or a private security advisory if the repository supports
them).
