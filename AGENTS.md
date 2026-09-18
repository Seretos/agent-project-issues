# agent-project-issues

MCP server exposing provider-agnostic issue/PR management (GitHub, GitLab, Azure DevOps) to AI
coding agents, with per-project permissions and automatic AI-attribution. Ships as a
self-contained binary (PyInstaller) so end users need no Python toolchain.

## Tool priority

Skills and MCP tools take priority over raw file tools — and this **explicitly overrides** the generic harness default that says "prefer the dedicated file/search tools (Glob/Grep/Read)". When a skill or MCP tool covers the task, reach for it first; fall back to raw Glob/Grep/Read only when none applies.

Concretely: any *"where is X defined / what does the code support / which Y exist / how does X work / find the callers of X"* question is a **code-understanding task → use the matching skill first** (e.g. the `serena-wrapper` symbol-aware tools), never raw Glob/Grep/Read.

## Where the code lives (read before grounding a change)

This repo is the **MCP server + tool wiring only**. The domain layer lives in the libs, declared
in `pyproject.toml`: both `lib-python-config` and `lib-python-projects` are pinned to the
exact immutable tags declared there; a bump is an explicit chore ticket.

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
- **Issue-template enforcement lives here, not in the lib.** `lib-python-projects` owns
  discovery and parsing only — `list_issue_templates`, `validate_ticket_body`,
  `render_skeleton` — and never decides whether a write should be blocked; deciding that,
  building the refusal payload, and gating `create_ticket`/`update_ticket` on it all live in
  `tools/tickets.py`. Keep that split: **the library reports, the plugin decides** — if a
  future change needs the gate to behave differently (a new refusal state, a different
  label/title-prefix policy), it belongs in this repo's tool layer, not as a new knob on the
  lib's parsing functions. Relatedly, `create_ticket`/`update_ticket` call
  `templates.validate_ticket_body` on the caller's raw body BEFORE the provider prepends the
  `#ai-generated`/`#ai-modified` marker — **validation runs before the #ai-generated marker**
  is added, so the marker's own text can never accidentally satisfy (or fail) a template
  field's content check.

## Gotchas

- `python -m pytest` runs the suite (config in `pyproject.toml`, `pythonpath=src`). Tests stub
  the project/provider layer (monkey-patching `_providers.load_projects` + fake providers), so a
  **green run ≠ verified against a live provider** — real HTTP is exercised in the lib / manually.
- Installing test deps (`pip install -e ".[test]"`) pulls both `lib-python-config` and
  `lib-python-projects` from GitHub at the pinned tags in `pyproject.toml`, so it needs network
  + git access.
- **Both libs are pinned to exact tags (see `pyproject.toml`) — no drift possible.** The tags are
  immutable, so a stale pip cache can't silently slide a dep forward. However a local
  `pip install -e <lib>` checkout still shadows the pinned release (and **never depend on a local
  lib branch for this repo**), so run `pwsh scripts/test.ps1` (force-reinstalls both libs from the
  declared tag refs, overriding any local editable shadow, then runs pytest) — or
  `pwsh scripts/sync-libs.ps1` before a bare `python -m pytest`. CI runs the same sync step.

## Contracts

- **Every release tag has a `src/<TAG>` sibling on `main`.** `release.yml`
  publishes each release as `<plugin>--vX.Y.Z` on the ancestor-less orphan
  `release` branch — that tag shares no history with any previous release,
  so `gh release create --generate-notes` against it always produced empty
  notes (ticket #298). To fix this, the `release` job also tags `main` at
  the run's frozen `github.sha` (the commit the release was actually built
  from, not `main`'s tip at publish time) as `src/<plugin>--vX.Y.Z`, and
  generates real notes by diffing `src/<PREV_TAG>...src/<TAG>` — real
  history that only exists on `main`.
- **These marker tags are immutable.** Never re-tag or force-push
  `src/<TAG>`; `stamp`'s pre-flight step fails the run if `src/<TAG>`
  already exists.
- **`release.yml` must be dispatched from the default branch.** The
  pre-flight step in `stamp` rejects any other `ref_name`, since `src/<TAG>`
  is only meaningful as a marker on `main`.
- **Bootstrapping a release that predates this ticket.** If `stamp`'s
  pre-flight reports that `src/<PREV_TAG>` is missing, read `head_sha` from
  the Actions run that produced `<PREV_TAG>`, then:
  ```
  git tag src/<PREV_TAG> <sha>
  git push origin refs/tags/src/<PREV_TAG>
  ```
- **Retry procedure.** If the marker push in the `release` job is rejected
  (e.g. `main` advanced with a workflow-file change since dispatch, tripping
  `GITHUB_TOKEN`'s workflow-scope restriction), nothing has been
  published yet — no tag, release, or marketplace dispatch — so simply
  re-run `release.yml` with the **same version**; the re-run re-dispatches
  at `main`'s new tip. Do **not** introduce a PAT to work around this.

- **Package layout is staged by one script.** `.github/scripts/stage-plugin-payload.sh <src> <dest>`
  is the single list of what ships (root `plugin.json` + `mcp.json`, `.claude-plugin/`, `hooks/`,
  `skills/`, `bin/`); `release.yml` calls it from both the ZIP and orphan-branch steps. The MCP
  server is declared in root `mcp.json` with a plugin-relative `./bin/project-issues` (no
  `${...}` placeholder - that broke Codex on Windows, #315). Add new shipped members in the script.

## More

Build (PyInstaller), the release pipeline, server-side env vars, and the marketplace contract
are documented in `README.md` / `SECURITY.md`. Provider internals (Azure work-item types, HTML
conversion, auth schemes, status discovery) live with the code in `lib-python-projects`.
