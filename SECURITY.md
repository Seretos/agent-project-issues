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

`_auto`-discovered projects (no TOML) have `create=false, modify=false` and
are therefore read-only by construction. Write access requires an explicit
`.claude/project-issues.toml` entry.

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
