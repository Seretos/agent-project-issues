"""Tests for ticket #245: `trigger_pipeline`, `get_ref`, `list_releases`,
and the `workflow`/`event`/`since` filters on `list_pipeline_runs`.

Uses `httpx.MockTransport` to intercept GitHub API calls, mirroring
`tests/test_pipelines.py` / `tests/test_pulls.py`. Every wait-path test
uses a tiny `wait_timeout_seconds` and monkeypatches the lib's
`_trigger_sleep` to a no-op so nothing here ever sleeps for real (the
suite has a 60s per-test timeout — see #244).
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pipelines as pipeline_tools


# ---------- helpers ----------------------------------------------------------


def _project(*, pipelines_trigger: bool = False, pulls_merge: bool = False) -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={
            "issues": {"create": True, "modify": True},
            "pulls": {"create": True, "modify": True, "merge": pulls_merge},
            "pipelines": {"trigger": pipelines_trigger},
        },
    )


def _run_payload(
    run_id: int,
    *,
    name: str = "CI",
    branch: str = "feature/x",
    head_sha: str = "deadbeef",
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "workflow_dispatch",
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
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project],
            state="ok",
            search_root="/tmp",
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)

    stub = _StubMCP()
    pipeline_tools.register(stub)
    return stub.tools


# ---------- B1: trigger_pipeline permission gate ------------------------------


def test_trigger_pipeline_denied_without_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=False))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](
        project_id="acme", workflow="release.yml", ref="main",
    )
    assert "error" in result
    assert "pipelines.trigger" in result["error"]


def test_trigger_pipeline_denied_when_permissions_block_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lib default for `pipelines.trigger` is False — an omitted `permissions:`
    block (or a `permissions:` block that just doesn't mention `pipelines`)
    must still deny, independent of `pulls.merge`."""
    project = ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={"pulls": {"merge": True}},
    )
    tools = _register_tools_with(monkeypatch, project)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](project_id="acme", workflow="release.yml")
    assert "error" in result
    assert "pipelines.trigger" in result["error"]


def test_trigger_pipeline_gate_fires_before_token_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token set at all — the gate must still be the error, not the
    token-missing error, proving gate-before-token ordering."""
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=False))
    monkeypatch.delenv("GITHUB_TOKEN_ACME", raising=False)

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](project_id="acme", workflow="release.yml")
    assert "error" in result
    assert "pipelines.trigger" in result["error"]


# ---------- B2: trigger_pipeline dispatch + run resolution --------------------


def test_trigger_pipeline_dispatches_and_returns_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_provider, "_trigger_sleep", lambda seconds: None)
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    dispatch_requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/repos/acme/backend/actions/workflows/release.yml/dispatches":
            dispatch_requests.append(req)
            return httpx.Response(status_code=204)
        if req.method == "GET" and req.url.path == "/repos/acme/backend/actions/workflows/release.yml/runs":
            return _json({
                "workflow_runs": [
                    _run_payload(101, branch="main", created_at="2099-01-01T00:00:00Z"),
                ],
            })
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](
        project_id="acme", workflow="release.yml", ref="main",
        wait_for_run=True, wait_timeout_seconds=1,
    )
    assert "error" not in result, result
    assert result["triggered"] is True
    assert result["run"]["id"] == "101"
    assert result["hint"] is None

    # No AI-attribution marker leaks into the dispatch request body — this
    # tool never writes a ticket/PR body or comment, so none should apply.
    assert len(dispatch_requests) == 1
    body = json.loads(dispatch_requests[0].content)
    assert "#ai-generated" not in json.dumps(body)
    assert "ai-generated" not in json.dumps(body)


def test_trigger_pipeline_times_out_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_provider, "_trigger_sleep", lambda seconds: None)
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "dispatches" in req.url.path:
            return httpx.Response(status_code=204)
        if req.method == "GET" and req.url.path == "/repos/acme/backend/actions/workflows/release.yml/runs":
            # No run ever appears — the poll must time out, not hang or raise.
            return _json({"workflow_runs": []})
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](
        project_id="acme", workflow="release.yml", ref="main",
        wait_for_run=True, wait_timeout_seconds=0,
    )
    assert "error" not in result, result
    assert result["triggered"] is True
    assert result["run"] is None
    assert result["hint"] is not None
    assert "list_pipeline_runs" in result["hint"] or "get_pipeline_run" in result["hint"]


def test_trigger_pipeline_wait_for_run_false_skips_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "dispatches" in req.url.path:
            return httpx.Response(status_code=204)
        raise AssertionError(
            f"no polling expected with wait_for_run=False; got {req.method} {req.url}"
        )

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](
        project_id="acme", workflow="release.yml", ref="main",
        wait_for_run=False,
    )
    assert "error" not in result, result
    assert result["triggered"] is True
    assert result["run"] is None
    assert result["hint"] is not None
    assert "wait_for_run=False" in result["hint"]


def test_trigger_pipeline_wait_timeout_seconds_clamped_at_120(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_timeout: dict[str, float] = {}

    def spy_wait_for_run(self, project, token, **kwargs):
        captured_timeout["timeout"] = kwargs.get("timeout")
        return None

    monkeypatch.setattr(github_provider.GitHubProvider, "wait_for_run", spy_wait_for_run)
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "dispatches" in req.url.path:
            return httpx.Response(status_code=204)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](
        project_id="acme", workflow="release.yml", ref="main",
        wait_for_run=True, wait_timeout_seconds=99999,
    )
    assert "error" not in result, result
    assert captured_timeout["timeout"] == 120.0


def test_trigger_pipeline_404_rewrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "dispatches" in req.url.path:
            return _json({"message": "Not Found"}, status_code=404)
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](
        project_id="acme", workflow="nonexistent.yml", ref="main",
    )
    assert "error" in result


def test_trigger_pipeline_rejects_empty_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](project_id="acme", workflow="   ")
    assert "error" in result


def test_trigger_pipeline_rejects_non_dict_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=True))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["trigger_pipeline"](
        project_id="acme", workflow="release.yml", inputs="not-a-dict",
    )
    assert "error" in result


# ---------- B3: list_pipeline_runs workflow/event/since filters ---------------


def test_list_pipeline_runs_recent_forwards_event_and_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _json({
            "workflow_runs": [
                _run_payload(1, event="workflow_dispatch", created_at="2024-06-01T00:00:00Z"),
            ],
        })

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", recent=True,
        event="manual", since="2024-01-01T00:00:00Z",
    )
    assert "error" not in result, result
    assert len(captured) == 1
    query = captured[0].url.params
    # "manual" resolves to GitHub's native "workflow_dispatch" (EVENT_ALIASES).
    assert query.get("event") == "workflow_dispatch"
    assert query.get("created") == ">=2024-01-01T00:00:00Z"


def test_list_pipeline_runs_for_branch_forwards_workflow_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.url.path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "cafebabe"}})
        return _json({"workflow_runs": [_run_payload(2, branch="main")]})

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", branch="main", workflow="release.yml",
    )
    assert "error" not in result, result
    # A .yml workflow filename scopes the request to the per-workflow
    # endpoint (see _runs_path / _workflow_path_segment).
    run_requests = [r for r in captured if "workflows/release.yml/runs" in r.url.path]
    assert run_requests, [str(r.url) for r in captured]


def test_list_pipeline_runs_filters_unset_matches_baseline_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No workflow/event/since -> byte-identical request shape to before
    ticket #245 (no `event`/`created` query params, unscoped runs path)."""
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _json({"workflow_runs": [_run_payload(3)]})

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](project_id="acme", recent=True)
    assert "error" not in result, result
    assert len(captured) == 1
    assert captured[0].url.path == "/repos/acme/backend/actions/runs"
    query = captured[0].url.params
    assert "event" not in query
    assert "created" not in query


def test_list_pipeline_runs_hint_names_active_filter_on_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"workflow_runs": []})

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", recent=True, workflow="nightly.yml",
    )
    assert "error" not in result, result
    assert result["runs"] == []
    assert result["hint"] is not None
    assert "workflow filter 'nightly.yml'" in result["hint"]


def test_list_pipeline_runs_addressing_validation_unaffected_by_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero addressing args is still an error even when workflow/event/since
    are set — filters never substitute for an addressing argument."""
    tools = _register_tools_with(monkeypatch, _project())

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call expected; got {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", workflow="release.yml", event="push", since="2024-01-01T00:00:00Z",
    )
    assert "error" in result
    assert "exactly one" in result["error"]


def test_list_pipeline_runs_malformed_since_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        # since is validated before any filtering matters, but the actual
        # HTTP call may still legitimately happen before the client-side
        # apply_run_filters pass raises — either way this must not blow up
        # with a raw traceback.
        return _json({"workflow_runs": [_run_payload(4)]})

    _install_mock(monkeypatch, handler)
    result = tools["list_pipeline_runs"](
        project_id="acme", recent=True, since="not-a-real-timestamp",
    )
    assert "error" in result, result
    assert "since" in result["error"]


# ---------- B4: get_ref --------------------------------------------------------


def test_get_ref_returns_resolved_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "cafebabe"}})
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["get_ref"](project_id="acme", ref="main")
    assert "error" not in result, result
    assert result["ref"]["name"] == "main"
    assert result["ref"]["kind"] == "branch"
    assert result["ref"]["sha"] == "cafebabe"


def test_get_ref_404_when_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        # Branch, tag, and commit lookups all miss -> get_ref returns None.
        if req.url.path == "/repos/acme/backend/branches/ghost":
            return httpx.Response(status_code=404)
        if req.url.path == "/repos/acme/backend/git/refs/tags/ghost":
            return httpx.Response(status_code=404)
        if req.url.path == "/repos/acme/backend/commits/ghost":
            return httpx.Response(status_code=404)
        return httpx.Response(status_code=404)

    _install_mock(monkeypatch, handler)
    result = tools["get_ref"](project_id="acme", ref="ghost")
    assert "error" in result
    assert "acme" in result["error"]
    assert "ghost" in result["error"]


def test_get_ref_succeeds_on_all_permissions_false_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only: no gate, unlike trigger_pipeline."""
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=False))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/branches/main":
            return _json({"commit": {"sha": "cafebabe"}})
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["get_ref"](project_id="acme", ref="main")
    assert "error" not in result, result


# ---------- B5: list_releases --------------------------------------------------


def _release_payload(tag: str, **overrides) -> dict:
    base = {
        "tag_name": tag,
        "name": f"Release {tag}",
        "html_url": f"https://github.com/acme/backend/releases/tag/{tag}",
        "draft": False,
        "prerelease": False,
        "created_at": "2024-01-01T00:00:00Z",
        "published_at": "2024-01-02T00:00:00Z",
        "body": "notes",
    }
    base.update(overrides)
    return base


def test_list_releases_returns_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/backend/releases":
            return _json([_release_payload("v1.0.0")])
        if req.url.path == "/repos/acme/backend/git/refs/tags/v1.0.0":
            return _json({"object": {"type": "commit", "sha": "deadbeef"}})
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_mock(monkeypatch, handler)
    result = tools["list_releases"](project_id="acme")
    assert "error" not in result, result
    assert len(result["releases"]) == 1
    assert result["releases"][0]["tag"] == "v1.0.0"
    assert result["hint"] is None


def test_list_releases_empty_returns_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([])

    _install_mock(monkeypatch, handler)
    result = tools["list_releases"](project_id="acme")
    assert "error" not in result, result
    assert result["releases"] == []
    assert result["hint"] is not None
    assert "acme" in result["hint"]


def test_list_releases_limit_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _register_tools_with(monkeypatch, _project())
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.url.path == "/repos/acme/backend/releases":
            return _json([_release_payload(f"v{i}.0.0") for i in range(1, 4)])
        return _json({"object": {"type": "commit", "sha": "deadbeef"}})

    _install_mock(monkeypatch, handler)
    result = tools["list_releases"](project_id="acme", limit=3)
    assert "error" not in result, result
    release_list_requests = [r for r in captured if r.url.path == "/repos/acme/backend/releases"]
    assert release_list_requests
    assert release_list_requests[0].url.params.get("per_page") == "3"


def test_list_releases_succeeds_on_all_permissions_false_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_tools_with(monkeypatch, _project(pipelines_trigger=False))
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        return _json([])

    _install_mock(monkeypatch, handler)
    result = tools["list_releases"](project_id="acme")
    assert "error" not in result, result
