"""Tests for the pipeline / CI-run tools.

Uses `httpx.MockTransport` to intercept GitHub API calls. The provider
is monkey-patched so `_client(token)` returns a client backed by the
mock transport. The log-fetch path (which uses a separate
`httpx.Client(follow_redirects=True)`) is handled by patching
`_fetch_job_log` directly — `MockTransport` doesn't auto-follow
redirects and the log fetch is the only call that needs it.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.providers import github as github_provider
from project_issues_plugin.tools import pipelines as pipeline_tools


# ---------- helpers ----------------------------------------------------------


def _project() -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


def _run_payload(
    run_id: int,
    *,
    name: str = "CI",
    branch: str = "feature/x",
    head_sha: str = "deadbeef",
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "push",
    created_at: str = "2024-01-01T00:00:00Z",
    updated_at: str = "2024-01-01T01:00:00Z",
    run_attempt: int = 1,
) -> dict:
    return {
        "id": run_id,
        "name": name,
        "head_branch": branch,
        "head_sha": head_sha,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/acme/backend/actions/runs/{run_id}",
        "created_at": created_at,
        "updated_at": updated_at,
        "run_attempt": run_attempt,
    }


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_mock(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "test-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    return seen


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools_with(monkeypatch: pytest.MonkeyPatch, project: ProjectConfig):
    from project_issues_plugin import config as cfg_mod

    def fake_load_projects(cwd=None):
        return cfg_mod.LoadResult(
            projects=[project],
            state="ok",
            search_root="/tmp",
        )

    monkeypatch.setattr(cfg_mod, "load_projects", fake_load_projects)

    stub = _StubMCP()
    pipeline_tools.register(stub)
    return stub.tools


# ---------- list_pipeline_runs: argument validation --------------------------


def test_list_pipeline_runs_zero_address_args_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](project_id="acme")
    assert "error" in result
    assert "exactly one" in result["error"]


def test_list_pipeline_runs_two_address_args_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", branch="main", commit_sha="abc",
    )
    assert "error" in result
    assert "exactly one" in result["error"]


# ---------- list_pipeline_runs: branch ---------------------------------------


def test_list_pipeline_runs_branch_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/actions/runs":
            captured["branch"] = req.url.params.get("branch", "")
            captured["per_page"] = req.url.params.get("per_page", "")
            return _json({
                "workflow_runs": [
                    _run_payload(1001, name="lint", branch="main"),
                    _run_payload(1002, name="test", branch="main"),
                ]
            })
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", branch="main", limit=5,
    )
    assert "error" not in result, result
    assert result["addressed_by"] == "branch"
    assert result["resolved_refs"] is None
    assert result["hint"] is None
    assert captured["branch"] == "main"
    assert captured["per_page"] == "5"
    assert [r["id"] for r in result["runs"]] == ["1001", "1002"]
    first = result["runs"][0]
    # Field mapping spot-checks.
    assert first["name"] == "lint"
    assert first["branch"] == "main"
    assert first["head_sha"] == "deadbeef"
    assert first["event"] == "push"
    assert first["status"] == "completed"
    assert first["conclusion"] == "success"
    assert first["run_attempt"] == 1
    assert first["url"].endswith("/actions/runs/1001")


# ---------- list_pipeline_runs: commit_sha -----------------------------------


def test_list_pipeline_runs_commit_sha_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/actions/runs":
            captured["head_sha"] = req.url.params.get("head_sha", "")
            return _json({
                "workflow_runs": [_run_payload(2001, head_sha="abc123")]
            })
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", commit_sha="abc123",
    )
    assert "error" not in result, result
    assert result["addressed_by"] == "commit"
    assert captured["head_sha"] == "abc123"
    assert [r["id"] for r in result["runs"]] == ["2001"]


# ---------- list_pipeline_runs: tag ------------------------------------------


def test_list_pipeline_runs_tag_resolves_to_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/git/refs/tags/v1.0":
            # Lightweight tag - object.type is "commit".
            return _json({
                "ref": "refs/tags/v1.0",
                "object": {"type": "commit", "sha": "tagsha999"},
            })
        if req.url.path == "/repos/acme/backend/actions/runs":
            captured["head_sha"] = req.url.params.get("head_sha", "")
            return _json({
                "workflow_runs": [_run_payload(3001, head_sha="tagsha999")]
            })
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", tag="v1.0",
    )
    assert "error" not in result, result
    assert result["addressed_by"] == "tag"
    assert result["resolved_refs"] == ["tagsha999"]
    assert captured["head_sha"] == "tagsha999"
    assert [r["id"] for r in result["runs"]] == ["3001"]


# ---------- list_pipeline_runs: ticket ---------------------------------------


def test_list_pipeline_runs_ticket_via_timeline_cross_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket mode: timeline cross-ref -> PR -> head.sha -> runs."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json({"number": 42, "body": "", "state": "open"})
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([
                {
                    "event": "cross-referenced",
                    "source": {
                        "type": "issue",
                        "issue": {
                            "number": 77,
                            "pull_request": {"url": "..."},
                            "repository": {"full_name": "acme/backend"},
                            "html_url": "https://github.com/acme/backend/pull/77",
                        },
                    },
                }
            ])
        if path == "/search/issues":
            # No additional PRs found via search.
            return _json({"items": []})
        if path == "/repos/acme/backend/pulls/77":
            return _json({
                "number": 77,
                "head": {"sha": "headsha77"},
            })
        if path == "/repos/acme/backend/actions/runs":
            assert req.url.params.get("head_sha") == "headsha77"
            return _json({
                "workflow_runs": [
                    _run_payload(4001, head_sha="headsha77", branch="feat/77")
                ]
            })
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", ticket_id="42",
    )
    assert "error" not in result, result
    assert result["addressed_by"] == "ticket"
    assert result["resolved_refs"] == ["headsha77"]
    assert result["hint"] is None
    assert [r["id"] for r in result["runs"]] == ["4001"]


def test_list_pipeline_runs_ticket_no_linked_refs_returns_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/issues/42":
            return _json({"number": 42, "body": "no hints here", "state": "open"})
        if path == "/repos/acme/backend/issues/42/timeline":
            return _json([])
        if path == "/search/issues":
            return _json({"items": []})
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", ticket_id="42",
    )
    assert "error" not in result, result
    assert result["addressed_by"] == "ticket"
    assert result["runs"] == []
    assert result["hint"] is not None
    assert "no linked PR/branch" in result["hint"]


# ---------- get_pipeline_run -------------------------------------------------


def test_get_pipeline_run_failed_populates_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed run + failure excerpt requested -> annotations + log_excerpt."""
    tools = _register_tools_with(monkeypatch, _project())

    annotations = [
        {
            "path": "src/foo.py",
            "start_line": 10,
            "annotation_level": "failure",
            "message": "NameError: 'x' is not defined",
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/runs/5001":
            return _json(_run_payload(
                5001,
                name="test",
                status="completed",
                conclusion="failure",
            ))
        if path == "/repos/acme/backend/actions/runs/5001/jobs":
            return _json({
                "jobs": [
                    {
                        "id": 7001,
                        "name": "pytest",
                        "html_url": "https://github.com/acme/backend/actions/runs/5001/job/7001",
                        "conclusion": "failure",
                        "check_run_url": "https://api.github.com/repos/acme/backend/check-runs/9999",
                        "steps": [
                            {"name": "checkout", "conclusion": "success"},
                            {"name": "run tests", "conclusion": "failure"},
                        ],
                    }
                ]
            })
        # Annotations endpoint — absolute URL on api.github.com lands here.
        if path == "/repos/acme/backend/check-runs/9999/annotations":
            return _json(annotations)
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    # Patch the log-fetch (separate httpx client w/ follow_redirects).
    def fake_log(token, log_url):
        return (
            "Setting up runner...\n"
            "Running tests...\n"
            "FAILED: test_foo - NameError: 'x' is not defined\n"
            "1 failed, 0 passed\n"
        )

    monkeypatch.setattr(github_provider, "_fetch_job_log", fake_log)

    result = tools["get_pipeline_run"](project_id="acme", run_id="5001")
    assert "error" not in result, result
    run = result["run"]
    assert run["id"] == "5001"
    assert run["conclusion"] == "failure"
    assert run["failure"] is not None
    failing = run["failure"]["failing_jobs"]
    assert len(failing) == 1
    job = failing[0]
    assert job["name"] == "pytest"
    assert job["failed_step"] == "run tests"
    assert job["annotations"] == annotations
    assert job["log_excerpt"] is not None
    assert "FAILED" in job["log_excerpt"]
    assert run["failure"]["note"] is None


def test_get_pipeline_run_excerpt_anchors_on_failing_step_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repro for `agent-project-issues#6`: a template `::error::` echo
    inside an unexecuted bash `if` in an early step must NOT hijack
    the excerpt. The excerpt must be clamped to the `##[group]Run
    <failed_step>` ... `##[endgroup]` block of the actually-failing
    step (`Dispatch to agent-marketplace`).
    """
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/runs/25764620196":
            return _json(_run_payload(
                25764620196, status="completed", conclusion="failure"
            ))
        if path == "/repos/acme/backend/actions/runs/25764620196/jobs":
            return _json({
                "jobs": [
                    {
                        "id": 999001,
                        "name": "release",
                        "html_url": "https://x/job/999001",
                        "conclusion": "failure",
                        "check_run_url": (
                            "https://api.github.com/repos/acme/backend/check-runs/77"
                        ),
                        "steps": [
                            {"name": "Validate version", "conclusion": "success"},
                            {"name": "Tag precondition", "conclusion": "success"},
                            {"name": "Set up job", "conclusion": "success"},
                            {"name": "Dispatch to agent-marketplace", "conclusion": "failure"},
                        ],
                    }
                ]
            })
        if path == "/repos/acme/backend/check-runs/77/annotations":
            return _json([
                {
                    "annotation_level": "failure",
                    "message": "Process completed with exit code 22.",
                    "start_line": 31,
                    "end_line": 31,
                }
            ])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    # Hand-rolled log fragment that mirrors the structure of the
    # production run: an early step holds a template `::error::` line
    # inside an `if` branch (never executed), but the substring still
    # appears in the raw log. The last group is the actual failing
    # `Dispatch to agent-marketplace` step.
    fake_log_text = "\n".join([
        "2025-05-13T22:00:00.0Z ##[group]Run echo Validate version",
        "2025-05-13T22:00:00.1Z Validate version",
        "2025-05-13T22:00:00.2Z if [ -z \"$V\" ]; then echo \"::error::Version '$V' is not valid semver (template)\"; fi",
        "2025-05-13T22:00:00.3Z Version OK",
        "2025-05-13T22:00:00.4Z ##[endgroup]",
        "2025-05-13T22:00:01.0Z ##[group]Run actions/checkout@v4",
        "2025-05-13T22:00:01.1Z Syncing repository: acme/backend",
        "2025-05-13T22:00:01.2Z Determining the checkout info",
        "2025-05-13T22:00:01.3Z Checking out the ref",
        "2025-05-13T22:00:01.4Z ##[endgroup]",
        "2025-05-13T22:00:02.0Z ##[group]Run Dispatch to agent-marketplace",
        "2025-05-13T22:00:02.1Z curl -X POST https://api.github.com/repos/acme/marketplace/dispatches",
        "2025-05-13T22:00:02.2Z HTTP/2 401",
        "2025-05-13T22:00:02.3Z curl: (22) The requested URL returned error: 401",
        "2025-05-13T22:00:02.4Z ##[error]Process completed with exit code 22.",
        "2025-05-13T22:00:02.5Z ##[endgroup]",
        "2025-05-13T22:00:02.6Z Cleaning up runner",
    ])

    monkeypatch.setattr(
        github_provider, "_fetch_job_log", lambda token, url: fake_log_text
    )

    result = tools["get_pipeline_run"](
        project_id="acme", run_id="25764620196"
    )
    assert "error" not in result, result
    job = result["run"]["failure"]["failing_jobs"][0]
    excerpt = job["log_excerpt"]
    assert excerpt is not None
    # Must include the real failing-step content...
    assert "Dispatch to agent-marketplace" in excerpt
    assert "exit code 22" in excerpt
    # ...and must NOT include the template echo from the version step
    # nor the checkout-step body, which is what the old substring scan
    # used to anchor on.
    assert "is not valid semver" not in excerpt
    assert "Syncing repository" not in excerpt


def test_extract_log_excerpt_substring_fallback_skips_setup_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When step-header matching cannot resolve (no matching group
    name) and there are no annotation lines, the substring scan must
    only run *after* the first group header — so template `::error::`
    lines emitted as plain stdout *before* the first group can never
    win.
    """
    log_text = "\n".join([
        "Setting up runner",
        "echo \"::error::Version 'x' is not valid semver\"",  # before any group
        "##[group]Run real step",
        "real-step starting",
        "##[error]boom",
        "##[endgroup]",
    ])
    out = github_provider._extract_log_excerpt(
        log_text, failed_step="step-not-in-log", annotations=[]
    )
    assert "is not valid semver" not in out
    assert "boom" in out


def test_get_pipeline_run_log_403_marks_logs_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/runs/5002":
            return _json(_run_payload(
                5002, status="completed", conclusion="failure"
            ))
        if path == "/repos/acme/backend/actions/runs/5002/jobs":
            return _json({
                "jobs": [
                    {
                        "id": 7002,
                        "name": "pytest",
                        "html_url": "https://x/job/7002",
                        "conclusion": "failure",
                        "check_run_url": (
                            "https://api.github.com/repos/acme/backend/check-runs/8888"
                        ),
                        "steps": [{"name": "run", "conclusion": "failure"}],
                    }
                ]
            })
        if path == "/repos/acme/backend/check-runs/8888/annotations":
            return _json([])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_mock(monkeypatch, handler)

    # Logs unavailable -> _fetch_job_log returns None on 403.
    monkeypatch.setattr(
        github_provider, "_fetch_job_log", lambda token, url: None
    )

    result = tools["get_pipeline_run"](project_id="acme", run_id="5002")
    assert "error" not in result, result
    failing = result["run"]["failure"]["failing_jobs"]
    assert len(failing) == 1
    assert failing[0]["log_excerpt"] is None
    assert result["run"]["failure"]["note"] == "logs unavailable"


def test_get_pipeline_run_in_progress_skips_failure_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/repos/acme/backend/actions/runs/5003":
            return _json(_run_payload(
                5003, status="in_progress", conclusion=None
            ))
        # Any /jobs or annotations call would mean we attempted the fetch.
        raise AssertionError(
            f"unexpected request for in-progress run: {req.url}"
        )

    _install_mock(monkeypatch, handler)
    # If the failure-fetch was ever called we'd hit MockTransport and fail.
    # Also stub _fetch_job_log to a marker so accidental calls show up.
    called: dict[str, bool] = {"x": False}

    def boom(token, url):
        called["x"] = True
        return ""

    monkeypatch.setattr(github_provider, "_fetch_job_log", boom)

    result = tools["get_pipeline_run"](project_id="acme", run_id="5003")
    assert "error" not in result, result
    assert result["run"]["status"] == "in_progress"
    assert result["run"]["conclusion"] is None
    assert result["run"]["failure"] is None
    assert called["x"] is False
