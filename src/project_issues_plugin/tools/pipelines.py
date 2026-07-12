"""Pipeline / CI-run tools exposed to the agent.

Single unified `list_pipeline_runs` with five addressing modes
(`branch` / `tag` / `commit_sha` / `ticket_id` / `recent`) plus a
`get_pipeline_run` detail tool that can also surface failure context
(annotations + log excerpt) for failed runs.

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
from project_issues_plugin.tools._log_slicing import slice_log
from project_issues_plugin.tools._providers import (
    _normalize_id,
    _provider_for,
    _resolve,
    _rewrap_404,
    _safe,
)


def _serialize_annotation(annotation) -> dict:
    """Map a single `FailureAnnotation` (ticket #152/#200) into the wire
    shape: `step`, `message`, `file`, `line`, `severity`, `title`."""
    return {
        "step": annotation.step,
        "message": annotation.message,
        "file": annotation.file,
        "line": annotation.line,
        "severity": annotation.severity,
        "title": annotation.title,
    }


def _serialize_failing_job(job, *, include_annotations: bool) -> dict:
    """Serialize a single `FailingJob`.

    Full (`include_annotations=True`): carries the normalized
    `annotations` list (see `_serialize_annotation`) alongside
    `log_excerpt`.

    Compact (`include_annotations=False`): drops the `annotations`
    list in favor of `annotation_count` + `annotations_fetched: False`
    (mirroring the `comments_fetched`/`relations_fetched` sentinel
    convention used elsewhere in this plugin), while still keeping
    `name`, `url`, `failed_step`, and `log_excerpt`.

    `job_id` (ticket #199) is emitted in both forms — it's the handle
    an agent needs to follow up with `get_pipeline_step_log` for the
    job's full log, so it must survive the compact/full split.
    """
    out: dict = {
        "name": job.name,
        "url": job.url,
        "failed_step": job.failed_step,
        "job_id": job.job_id,
    }
    if include_annotations:
        out["annotations"] = [_serialize_annotation(a) for a in job.annotations]
    else:
        out["annotation_count"] = len(job.annotations)
        out["annotations_fetched"] = False
    # `log_excerpt` is a structural fallback for empty/absent structured
    # annotations (e.g. GitLab today) — always emitted alongside the
    # annotation section regardless of include_annotations, and is
    # itself already gated by `include_failure_excerpt` at the point
    # this function is only called when a `failure` block exists.
    out["log_excerpt"] = job.log_excerpt
    return out


def _serialize_failure(failure, *, include_annotations: bool) -> dict:
    """Hand-built serializer for `PipelineFailure` (ticket #200) — does
    NOT rely on `dataclasses.asdict` recursion across the nested
    `FailingJob` / `FailureAnnotation` dataclasses, so this repo
    controls the exact wire shape (and can apply the
    `include_annotations` progressive-disclosure gate) independent of
    however the lib's dataclasses are structured. Per-job grouping
    only — deliberately does not add a top-level flattened annotations
    list (the lib's `PipelineFailure.failures` convenience property is
    intentionally not surfaced here).
    """
    return {
        "failing_jobs": [
            _serialize_failing_job(job, include_annotations=include_annotations)
            for job in failure.failing_jobs
        ],
        "note": failure.note,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_pipeline_runs(
        project_id: str,
        branch: Annotated[str | None, Field(description="One-of addressing argument. Exactly one of branch/tag/commit_sha/ticket_id/recent must be set. Filter runs by branch name (e.g. 'main').")] = None,
        tag: Annotated[str | None, Field(description="One-of addressing argument. Exactly one of branch/tag/commit_sha/ticket_id/recent must be set. Resolves tag to commit SHA, then lists runs for that SHA.")] = None,
        commit_sha: Annotated[str | None, Field(description="One-of addressing argument. Exactly one of branch/tag/commit_sha/ticket_id/recent must be set. Lists runs filtered by head_sha.")] = None,
        ticket_id: Annotated[str | None, Field(description="One-of addressing argument. Exactly one of branch/tag/commit_sha/ticket_id/recent must be set. Walks the ticket's timeline to collect head_shas and aggregates runs.")] = None,
        recent: Annotated[bool, Field(description="One-of addressing argument. When True (and no other addressing arg is set), lists the most recent runs across the project with no ref filter. Exactly one of branch/tag/commit_sha/ticket_id/recent must be set.")] = False,
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
          - `recent`: when `True`, lists the most recent runs across the
            project with no ref filter. Use this when you have no branch,
            tag, commit, or ticket to anchor on and just need to see the
            latest CI activity.

        `status` filters runs by run-status (`queued` / `in_progress` /
        `completed`); `"all"` (default) returns runs regardless of state.
        `limit` is capped at the provider's max page size (100).

        Return shape:
        ```
        {
          "project_id": str,
          "addressed_by": "branch"|"tag"|"commit"|"ticket"|"recent",
            # "commit" corresponds to the commit_sha input param
          "resolved_refs": list | None,  # only set for tag/ticket modes
          "runs": [PipelineRun, ...],
          "hint": str | None,
        }
        ```

        `hint` is populated whenever `runs` is empty, regardless of
        addressing mode — not just for `recent`/`ticket`. The more
        specific tag/ticket resolution-failure hints take precedence;
        otherwise a generic "no runs found" hint names the addressing
        mode and, when a non-`all` `status` filter is active, notes
        that the filter may be excluding matching runs.

        Run details (`name`, `branch`, `head_sha`, `event`, `status`,
        `conclusion`, `url`, `created_at`, `updated_at`, `run_attempt`)
        match the GitHub Actions `workflow_run` shape. `conclusion` is
        `None` for runs still in progress.

        For a single run's full detail — notably the `run.failure` block
        (failing jobs, annotations, log excerpt) that only
        `get_pipeline_run` returns and only for failed runs — pass a
        row's `id` to `get_pipeline_run`. These two tools document
        complementary halves of the run shape: the per-run fields here,
        the failure block there.

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
        if recent:
            set_args.append("recent")
        # Ticket #195 finding 3: both branches share one "exactly one
        # addressing argument" template (only the `got:` tail differs) so
        # the wording no longer diverges between the 0-arg and 2+-arg
        # cases. The "exactly one" substring is preserved for existing
        # tests.
        if len(set_args) == 0:
            return {
                "error": (
                    "list_pipeline_runs accepts exactly one addressing "
                    "argument (branch/tag/commit_sha/ticket_id/recent), "
                    "but got: none."
                )
            }
        if len(set_args) > 1:
            return {
                "error": (
                    "list_pipeline_runs accepts exactly one addressing "
                    "argument (branch/tag/commit_sha/ticket_id/recent), "
                    f"but got: {', '.join(set_args)}."
                )
            }

        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            hint: str | None = None
            resolved_refs: list[str] | None = None

            if recent:
                runs, _ = provider.list_runs_recent(
                    project, token, status=status, limit=limit
                )
                addressed_by = "recent"
                addressed_desc = "recent pipeline/CI runs"
            elif branch:
                runs, _ = provider.list_runs_for_branch(
                    project, token, branch, status=status, limit=limit
                )
                addressed_by = "branch"
                addressed_desc = f"pipeline/CI runs for branch '{branch}'"
            elif commit_sha:
                runs, _ = provider.list_runs_for_commit(
                    project, token, commit_sha, status=status, limit=limit
                )
                addressed_by = "commit"
                addressed_desc = f"pipeline/CI runs for commit '{commit_sha}'"
            elif tag:
                runs, resolved_refs = provider.list_runs_for_tag(
                    project, token, tag, status=status, limit=limit
                )
                addressed_by = "tag"
                addressed_desc = f"pipeline/CI runs for tag '{tag}'"
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
                addressed_desc = "pipeline/CI runs for this ticket"
                if not resolved_refs:
                    hint = (
                        "no linked PR/branch found — ask user for a "
                        "branch/commit"
                    )

            # Ticket #195 finding 4: emit an equivalent "no runs found"
            # hint regardless of addressing mode — previously only
            # `recent` (and the tag/ticket resolution-failure paths
            # above) populated `hint` on an empty result; `branch` and
            # `commit_sha` silently returned `hint: null`. The
            # more-specific hints set above (tag/ticket resolution
            # failure) take precedence and are left untouched.
            if not runs and hint is None:
                hint = (
                    f"no {addressed_desc} found — the project may have no "
                    "CI/CD pipeline configured, or this ref/filter simply "
                    "has no runs yet"
                )
                if status != "all":
                    hint += (
                        f" (status filter '{status}' is active and may be "
                        "excluding matching runs)"
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
        run_id: Annotated[str, Field(description="Numeric string identifying the pipeline run. GitHub: Actions workflow_run id (e.g. '9876543210'); GitLab: pipeline id (e.g. '12345'); Azure DevOps: build id (e.g. '678'). Obtain from list_pipeline_runs. The value is numeric but the type is string — always pass it quoted (\"9876543210\"), never as a bare integer.")],
        include_failure_excerpt: bool = True,
        include_annotations: Annotated[bool, Field(description="When True (default), each failing job carries its full normalized `annotations` list. When False, the annotations list is dropped in favor of a compact `annotation_count` + `annotations_fetched: False` pair (name/url/failed_step/log_excerpt are unaffected). Combine with include_failure_excerpt=False for a fully compact summary.")] = True,
    ) -> dict:
        """Get a single pipeline run's details.

        `run_id` is a numeric identifier but is typed as a string —
        always pass it quoted (e.g. `"9876543210"`), never as a bare
        integer. The base `run` fields (`name`, `branch`, `head_sha`,
        `status`, `conclusion`, ...) are documented on
        `list_pipeline_runs`; this tool adds the `run.failure` block on
        top.

        When `include_failure_excerpt=True` (default) AND the run
        concluded as `failure`, the response also carries a
        `run.failure` block, grouped per failing job:

        ```
        {
          "failing_jobs": [
            {
              "name": str,
              "url": str,
              "failed_step": str,
              # Full form (include_annotations=True, default):
              "annotations": [
                {
                  "step": str,           # job/step the annotation belongs to
                  "message": str,        # human-readable annotation text ("" if omitted)
                  "file": str | None,    # source file the provider anchored to
                  "line": int | None,    # source line the provider anchored to
                  "severity": str | None,# provider-native level, e.g. "failure"/"warning"/"notice"
                  "title": str | None,   # short summary, when distinct from message
                },
                ...
              ],
              # Compact form (include_annotations=False) instead carries:
              #   "annotation_count": int,
              #   "annotations_fetched": False,
              "log_excerpt": str | None  # ~30 lines clamped to the
                                         # failing step's ##[group] /
                                         # ##[endgroup] block (with
                                         # annotation-line + substring
                                         # fallbacks), or None if logs
                                         # unavailable. Always present
                                         # (fallback context) regardless
                                         # of include_annotations —
                                         # structured annotations may be
                                         # empty (e.g. GitLab today) even
                                         # when a log excerpt exists.
            },
            ...
          ],
          "note": str | None  # e.g. "logs unavailable"
        }
        ```

        `annotations` are normalized across providers (GitHub Check-Run
        annotations, Azure Pipelines timeline-record issues both map
        into this shape); GitLab currently has no structured surface to
        map from, so its `annotations` is always `[]` — rely on
        `log_excerpt` for context in that case.

        Each failing job also carries a `job_id` (ticket #199). When
        `log_excerpt`'s ~30 lines aren't enough, pass that `job_id`
        (with this same `run_id`) to `get_pipeline_step_log` for the
        job's full log, sliced down to a bounded window instead of the
        whole unbounded text.

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
            run_dict = asdict(run)
            if run.failure is not None:
                run_dict["failure"] = _serialize_failure(
                    run.failure, include_annotations=include_annotations,
                )
            return {"project_id": project.id, "run": run_dict}
        return _safe(go)

    @mcp.tool()
    def get_pipeline_step_log(
        project_id: str,
        run_id: Annotated[str, Field(description="Numeric string identifying the pipeline run — the same run_id you'd pass to get_pipeline_run. Obtain from list_pipeline_runs.")],
        job_id: Annotated[str, Field(description="Numeric string identifying the failing job within the run. Obtain from get_pipeline_run's run.failure.failing_jobs[].job_id — do not guess or construct it.")],
        mode: Literal["tail", "around_failure", "errors_only"] = "around_failure",
        max_lines: int = 200,
    ) -> dict:
        """Fetch one failing job's full log, bounded to a small slice.

        This is the explicit, bounded follow-up for a single failing
        job named by `get_pipeline_run` — call it when that tool's
        compact `log_excerpt` (~30 lines clamped around the failing
        step) isn't enough context. Unlike a raw log fetch, this tool
        ALWAYS bounds its output to at most `max_lines` (hard-capped at
        1000 regardless of what's requested) — it never returns the
        full, unbounded log text into the conversation.

        `run_id` / `job_id` are the same identifiers surfaced on a
        failing job from `get_pipeline_run`
        (`run.failure.failing_jobs[].job_id`, alongside the `run_id`
        you already used to call it) — obtain them there first, don't
        guess or construct them.

        `mode` controls how the raw log is sliced down to `max_lines`:
          - `"around_failure"` (default): finds the first line that
            looks like an error (case-insensitive substring match
            against a small fixed pattern set: `##[error]`, `error`,
            `failed`, `failure`, `traceback`, `exception`, `fatal`,
            `panic`) and returns a window of `max_lines` lines centered
            on it. If no such line is found, this falls back to
            `"tail"` behavior and the response's `mode` comes back as
            `"around_failure->tail"` so you can tell it degraded.
          - `"tail"`: the last `max_lines` lines of the log.
          - `"errors_only"`: only the lines matching the error-pattern
            set above, in original order, capped at `max_lines`
            matching lines.

        Return shape:
        ```
        {
          "project_id": str,
          "run_id": str,
          "job_id": str,
          "lines": str,            # the sliced log text
          "mode": str,             # echoes the effective mode (see around_failure->tail above)
          "truncated": bool,       # more content existed than was returned
          "total_lines": int,      # total lines in the raw log
          "returned_lines": int,   # lines actually returned in `lines`
          "more_available": bool,  # a different mode/max_lines would surface more
        }
        ```

        For `"tail"`/`"around_failure"`, `truncated`/`more_available`
        reflect the whole raw log vs. what was returned. For
        `"errors_only"`, they instead reflect whether there were more
        *matching* lines than `max_lines` could hold.

        404s (log unavailable, non-numeric run_id/job_id at the
        provider) are rewrapped with project/run/job context, same as
        `get_pipeline_run`.

        Read-only: no permission flag required.
        """
        if not run_id.strip().isdigit():
            return {
                "error": (
                    f"run_id must be a numeric string (got {run_id!r}). "
                    "Obtain from list_pipeline_runs / get_pipeline_run."
                )
            }
        if not job_id.strip().isdigit():
            return {
                "error": (
                    f"job_id must be a numeric string (got {job_id!r}). "
                    "Obtain from get_pipeline_run's "
                    "run.failure.failing_jobs[].job_id."
                )
            }
        if mode not in ("tail", "around_failure", "errors_only"):
            return {
                "error": (
                    f"mode={mode!r} is not supported. "
                    "Use one of: tail, around_failure, errors_only."
                )
            }
        if not isinstance(max_lines, int) or max_lines <= 0:
            return {
                "error": f"max_lines must be a positive int (got {max_lines!r})."
            }

        def go() -> dict:
            project = _resolve(project_id)
            provider = _provider_for(project)
            token = resolve_token(project)
            try:
                log_text = provider.get_step_log(project, token, run_id, job_id)
            except (GitHubError, GitLabError, AzureDevOpsError) as exc:
                raise _rewrap_404(
                    exc, project_id=project.id, kind="pipeline job log",
                    ident=f"{run_id}/{job_id}",
                )
            sliced = slice_log(log_text, mode=mode, max_lines=max_lines)
            return {
                "project_id": project.id,
                "run_id": run_id,
                "job_id": job_id,
                **sliced,
            }
        return _safe(go)
