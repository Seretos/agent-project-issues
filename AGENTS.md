# agent-project-issues

MCP server that lets AI coding agents read and write GitHub/GitLab issues with per-project permissions and automatic AI-attribution markers. Ships as a self-contained Windows `.exe` (PyInstaller-frozen Python + mcp + httpx + pydantic) so end users don't need a Python toolchain.

## Layout

```
src/project_issues_plugin/      # Python source (src-layout)
  server.py                       # FastMCP entry point, wires the tools
  config.py                       # TOML loader + git-remote autodiscovery + LoadResult
  markers.py                      # AI-attribution constants (labels, comment prefix)
  providers/
    base.py                       # provider-agnostic Ticket/Comment dataclasses
    github.py                     # REST v3 implementation
  tools/
    projects.py                   # list_projects / find_projects
    tickets.py                    # list/get/create/update_ticket, add_comment

tests/                          # pytest, runs on every push (test.yml)
scripts/build.ps1               # PyInstaller wrapper + smoke test + optional packaging
project-issues.spec             # PyInstaller config
pyproject.toml                  # setuptools (package-dir = src/) + pytest config
.claude-plugin/plugin.json      # plugin manifest, points at bin/project-issues.exe

.github/workflows/
  test.yml                      # pytest on every push and PR
  release.yml                   # manual-dispatch full release flow
  dispatch.yml                  # manual recovery: re-send marketplace dispatch
```

## Branches

- `main` — source of truth. All edits go here.
- `release` — orphan branch, force-pushed by `release.yml`. Contains only install-ready files: `.claude-plugin/plugin.json`, `bin/project-issues.exe`, `README.md`. Clients clone at the version tag (e.g. `v0.0.1`), which lives on a release-branch commit.

The release branch shares no history with main. Don't try to merge between them.

## Release flow

Triggered manually:

```
Actions → release → Run workflow → version=X.Y.Z
```

or `gh workflow run release.yml -f version=X.Y.Z`.

The workflow:
1. Validates `X.Y.Z` is semver.
2. Fails if tag `vX.Y.Z` already exists.
3. Stamps the version into `pyproject.toml` and `.claude-plugin/plugin.json` (CI checkout only — never pushed back to main).
4. Runs `scripts/build.ps1 -Clean -Package` (PyInstaller → smoke test → ZIP).
5. Stashes the ZIP outside the working tree (step 6 wipes it).
6. Force-pushes the orphan `release` branch from the staged install-ready tree.
7. Creates the `vX.Y.Z` tag on that commit and a GitHub Release with the ZIP attached.
8. POSTs to `Seretos/agent-marketplace/dispatches` with the plugin metadata. (Direct POST because tags created via `GITHUB_TOKEN` don't trigger downstream workflows — Actions blocks it to prevent loops.)

## Environment variables (server-side)

| Variable | Effect |
|---|---|
| `PROJECT_ISSUES_PLUGIN_ROOT` | Set by Claude Code to the plugin's install dir. Logged on startup. |
| `PROJECT_ISSUES_PLUGIN_CWD` | Override the search root for `.claude/project-issues.toml` and `.git/config`. Highest priority. |
| `CLAUDE_PROJECT_DIR` | Fallback search root if the plugin var is unset. |
| `PROJECT_ISSUES_PLUGIN_LOG` | Logging level (`DEBUG`/`INFO`/…). Default `INFO`. Goes to stderr. |
| `GITHUB_TOKEN` / `GITLAB_TOKEN` | Default tokens for `_auto` projects discovered from the git remote. |
| `<token_env>` | Per-project token if `token_env` is set in TOML. The token value itself never leaves the process. |

## AI-marker conventions

`markers.py` defines stable constants that are visible to end users in their own repositories:

- `AI_GENERATED_LABEL = "ai-generated"`
- `AI_MODIFIED_LABEL  = "ai-modified"`
- `AI_NOT_PLANNED_LABEL = "ai-closed-not-planned"`
- `AI_COMMENT_PREFIX = "#ai-generated\n\n"` (single `#` — `##` would render as h2)

**These strings MUST remain stable across versions.** Changing them retroactively orphans existing labels/comments in user repos. If you ever need new variants, add new constants instead of mutating these.

The provider applies markers automatically; the agent must not pass them in arguments.

## Build conventions (`scripts/build.ps1`)

- Compatible with **Windows PowerShell 5.1** and PowerShell 7. CI uses 7.
- No global `$ErrorActionPreference = 'Stop'` — httpx/pyinstaller log to stderr.
- Python discovery prefers `py.exe -3` locally, `python.exe` in `$env:CI`.
- The smoke test runs an MCP `initialize` handshake against the freshly built `.exe`.

## PyInstaller / src-layout notes

- The Python package is `project_issues_plugin` under `src/`. `pyproject.toml` declares `package-dir = { "" = "src" }` and `pythonpath = ["src"]`.
- `project-issues.spec` references `src/project_issues_plugin/__main__.py` as the entry and `pathex=[ROOT / "src"]`.
- `httpx` / `httpcore` / `certifi` are pulled in via `collect_all(...)` because httpx loads transports lazily.

## Conventions

- Tests live in `tests/` and use pytest. They cover deterministic logic (markers, config parsing). HTTP paths are tested manually against real providers.
- GitLab is stubbed — `_PROVIDERS` in `tools/tickets.py` only registers GitHub. Auto-discovery emits gitlab projects but write paths fail with `NotImplementedError`.
- Permission gating lives in `tools/tickets.py` (`_require_create`, `_require_modify`, `_require_token`). New write operations MUST go through these helpers.
- The `dispatch.yml` workflow is a manual recovery tool only.

## What lives where (for cross-repo reasoning)

- The marketplace contract (payload format) is in `agent-marketplace/AGENTS.md`. The "Dispatch to agent-marketplace" step in `release.yml` here must match it.
- `MARKETPLACE_DISPATCH_TOKEN` is a fine-grained PAT with `contents: write` + `pull-requests: write` on `Seretos/agent-marketplace`, stored as a repo secret here.
