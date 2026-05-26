"""Pipeline / CI-run tools exposed to the agent.

Single unified `list_pipeline_runs` with four addressing modes
(`branch` / `tag` / `commit_sha` / `ticket_id`) plus a `get_pipeline_run`
detail tool that can also surface failure context (annotations + log
excerpt) for failed runs.

All reads are token-gated only; there is no permission flag (mirrors
`list_tickets` / `get_ticket` / `list_prs`).
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from lib_python_projects import resolve_token
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools._providers import (
    _normalize_id,
    _provider_for,
    _resolve,
    _rewrap_404,
    _safe,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_pipeline_runs(
        project_id: str,
        branch: Annotated[str | None, Field(description="One-of addressing argument. Exactly one of branch/tag/commit_sha/ticket_id must be set. Filter runs by branch name (e.g. 'main').")] = None,
        tag: Annotated[str | None, Field(description="One-of addressing argument. Exactly one of branch/tag/commit_sha/ticket_id must be set. Resolves tag to commit SHA, then lists runs for that SHA.")] = None,
        commit_sha: Annotated[str | None, Field(description="One-of addressing argument. Exactly one of branch/tag/commit_sha/ticket_id must be set. Lists runs filtered by head_sha.")] = None,
        ticket_id: Annotated[str | None, Field(description="One-of addressing argument. Exactly one of branch/tag/commit_sha/ticket_id must be set. Walks the ticket's timeline to collect head_shas and aggregates runs.")] = None,
        status: Literal["queued", "in_progress", "completed", "all"] = "all",
        limit: int = 10,
    ) -> dict:
        """List CI/CD pipeline runs for a project.

        Exactly ONE addressing argument must be set:
          - `branch`: list runs filtered by branch name (e.g. `main`).
          - `tag`: resolves tag -> commit SHA, then lists runs for that SHA.
          - `commit_sha`: lists runs filtered by `head_sha`.
          - `ticket_id`: walks the ticket's timeline / referenced PRs to
            collect head_shas, then aggregates runs across them. When a
            ticket has no linked PR / branch, returns `runs=[]` and a
            `hint` asking the user for a branch or commit.

        `status` filters runs by run-status (`queued` / `in_progress` /
        `completed`); `"all"` (default) returns runs regardless of state.
        `limit` is capped at the provider's max page size (100).

        Return shape:
        ```
        {
          "project_id": str,
          "addressed_by": "branch"|"tag"|"commit"|"ticket",
          "resolved_refs": list | None,  # only set for tag/ticket modes
          "runs": [PipelineRun, ...],
          "hint": str | None,
        }
        ```

        Run details (`name`, `branch`, `head_sha`, `event`, `status`,
        `conclusion`, `url`, `created_at`, `updated_at`, `run_attempt`)
        match the GitHub Actions `workflow_run` shape. `conclusion` is
        `None` for runs still in progress.

        Read-only: no permission flag required (token-gated like
        `list_tickets` / `list_prs`).
        """
        addr_args = {
            "branch": branch,
            "tag": tag,
            "commit_sha": commit_sha,
            "ticket_id": ticket_id,
        }
        set_args = [k for k, v in addr_args.items() if v]
        if len(set_args) == 0:
            return {
                "error": (
                    "list_pipeline_runs requires exactly one of "
                    "branch/tag/commit_sha/ticket_id."
                )
            }
        if len(set_args) > 1:
            return {
                "error": (
                    f"list_pipeline_runs accepts exactly one addressing "
                    f"argument, but got: {', '.join(set_args)}."
                )
            }

        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            hint: str | None = None
            resolved_refs: list[str] | None = None

            if branch:
                runs, _ = provider.list_runs_for_branch(
                    project, token, branch, status=status, limit=limit
                )
                addressed_by = "branch"
            elif commit_sha:
                runs, _ = provider.list_runs_for_commit(
                    project, token, commit_sha, status=status, limit=limit
                )
                addressed_by = "commit"
            elif tag:
                runs, resolved_refs = provider.list_runs_for_tag(
                    project, token, tag, status=status, limit=limit
                )
                addressed_by = "tag"
                if not resolved_refs:
                    hint = (
                        f"could not resolve tag '{tag}' to a commit — "
                        "verify the tag exists in the project."
                    )
            else:
                # ticket mode
                normalized_ticket = _normalize_id(project, ticket_id)
                runs, resolved_refs = provider.list_runs_for_ticket(
                    project, token, normalized_ticket, status=status, limit=limit  # type: ignore[arg-type]
                )
                addressed_by = "ticket"
                if not resolved_refs:
                    hint = (
                        "no linked PR/branch found — ask user for a "
                        "branch/commit"
                    )

            return {
                "project_id": project.id,
                "addressed_by": addressed_by,
                "resolved_refs": resolved_refs,
                "runs": [asdict(r) for r in runs],
                "hint": hint,
            }

        return _safe(go)

    @mcp.tool()
    def get_pipeline_run(
        project_id: str,
        run_id: Annotated[str, Field(description="Numeric string identifying the pipeline run. GitHub: Actions workflow_run id (e.g. '9876543210'); GitLab: pipeline id (e.g. '12345'); Azure DevOps: build id (e.g. '678'). Obtain from list_pipeline_runs.")],
        include_failure_excerpt: bool = True,
    ) -> dict:
        """Get a single pipeline run's details.

        When `include_failure_excerpt=True` (default) AND the run
        concluded as `failure`, the response also carries a
        `run.failure` block:

        ```
        {
          "failing_jobs": [
            {
              "name": str,
              "url": str,
              "failed_step": str,
              "annotations": [ ... ],   # GitHub check-run annotations
              "log_excerpt": str | None  # ~30 lines clamped to the
                                         # failing step's ##[group] /
                                         # ##[endgroup] block (with
                                         # annotation-line + substring
                                         # fallbacks), or None if logs
                                         # unavailable
            },
            ...
          ],
          "note": str | None  # e.g. "logs unavailable"
        }
        ```

        In-progress runs (`conclusion=None`) never trigger the failure
        fetch. 403/404 on the log endpoint degrades to
        `log_excerpt=None` plus `note="logs unavailable"`.

        Read-only: no permission flag required.
        """
        if not run_id.strip().isdigit():
            return {
                "error": (
                    f"run_id must be a numeric string (got {run_id!r}). "
                    "Obtain from list_pipeline_runs."
                )
            }

        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            try:
                run = provider.get_run(
                    project, token, run_id,
                    include_failure_excerpt=include_failure_excerpt,
                )
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="pipeline run",
                    ident=run_id,
                )
            return {"project_id": project.id, "run": asdict(run)}
        return _safe(go)
