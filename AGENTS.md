# agent-project-issues

MCP server exposing provider-agnostic issue/PR management (GitHub, GitLab, Azure DevOps) to AI
coding agents, with per-project permissions and automatic AI-attribution. Ships as a
self-contained binary (PyInstaller) so end users need no Python toolchain.

## Where the code lives (read before grounding a change)

This repo is the **MCP server + tool wiring only**. The domain layer lives in the libs, pinned
via git `@release/0.x` in `pyproject.toml`:

- **`lib-python-projects`** — `ProjectConfig`, `load_projects`, `resolve_token`, and **all
  provider implementations** (`GitHubProvider` / `GitLabProvider` / `AzureDevOpsProvider`,
  `BaseProvider`, `TicketFilters` / `PRFilters`, the typed `*Error` classes, and the
  AI-attribution machinery).
- **`lib-python-config`** — lower-level config primitives the above builds on.

So a ticket about provider behaviour, the data model, config loading, or attribution markers is
almost always a change **in the lib**, not here. This repo only wires the lib's surface into MCP
tools: tool modules are registered in `src/project_issues_plugin/server.py`; shared helpers
(resolve, permission gates, error translation, id normalisation) live in `tools/_providers.py`.

## Invariants (don't break these)

- **Permission gates are mandatory.** Every write tool routes through the gates in
  `tools/_providers.py` — `_require_token`, `_require_issues_create` / `_require_issues_modify`,
  `_require_pulls_create` / `_require_pulls_modify` / `_require_pulls_merge`. A new write op
  without its gate is a bug.
- **Errors return as data, not tracebacks.** Wrap provider calls in `_safe` so failures surface
  as `{"error": "..."}`; never let a raw exception reach the agent.
- **Never emit attribution markers from tool arguments.** The layer auto-prepends the
  `#ai-generated` marker / applies the `ai-generated` label. Tools and callers must not pass them.
- **Config file is `projects.yml`.** The plugin passes this name (and `projects.yaml`)
  explicitly to `load_projects`, because the lib still defaults to the legacy
  `project-issues.yml`. Don't "simplify" that back to the lib default.

## Gotchas

- `python -m pytest` runs the suite (config in `pyproject.toml`, `pythonpath=src`). Tests stub
  the project/provider layer (monkey-patching `_providers.load_projects` + fake providers), so a
  **green run ≠ verified against a live provider** — real HTTP is exercised in the lib / manually.
- Installing test deps (`pip install -e ".[test]"`) pulls `lib-python-config` /
  `lib-python-projects` from GitHub (`@release/0.x`), so it needs network + git access.
- **The libs float on `@release/0.x` — keep local in sync, or local pytest lies.** The deps track
  a moving branch, but pip won't re-pull a branch dep whose version is unchanged ("already
  satisfied"), and a local `pip install -e <lib>` checkout *shadows* the released package
  entirely. Either way your local suite can pass against a stale/local lib while CI fails against
  the current `release/0.x`. **Never depend on a local lib branch for this repo.** Run
  `pwsh scripts/test.ps1` (force-refreshes both libs to `release/0.x` HEAD, then runs pytest) — or
  `pwsh scripts/sync-libs.ps1` before a bare `python -m pytest`. CI runs the same sync step so the
  pipeline can't be fooled by a cached wheel.

## More

Build (PyInstaller), the release pipeline, server-side env vars, and the marketplace contract
are documented in `README.md` / `SECURITY.md`. Provider internals (Azure work-item types, HTML
conversion, auth schemes, status discovery) live with the code in `lib-python-projects`.
