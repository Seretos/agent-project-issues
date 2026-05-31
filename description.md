Manage issues and pull requests across GitHub, GitLab, and Azure DevOps from any AI coding agent — with automatic AI-attribution and per-project access control.

- **Multi-provider support** — works with GitHub, GitLab, and Azure DevOps out of the box; switch providers per project without changing your agent workflow.
- **Issue management** — list, create, update, and comment on tickets; filters by status, label, milestone, and assignee.
- **Pull request management** — list, create, update, merge, and comment on PRs across all three providers.
- **Automatic AI-attribution** — every issue or PR your agent creates or updates is marked `#ai-generated` so humans can tell at a glance what the agent touched.
- **Per-project permissions** — a single `projects.yml` file controls which operations (read / create / modify / merge) each project allows; agents cannot exceed the declared scope.
- **Zero-config read-only access** — point the plugin at any repo via its git remote URL and it will infer the provider and offer read-only access with no extra setup.
- **Self-contained binary** — ships as a single executable for Linux and Windows; no Python toolchain or dependency install required on the end-user machine.
