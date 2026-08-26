"""Tests for ticket #288 (bundled into work package #290): non-GitHub
provider errors in `list_tickets_across_projects` must be isolated per
project, not abort the whole batch — and a bulk 401 must carry the same
provider-specific scope hint a single-project tool gets via `_safe`.

Two behavioural requirements:

  R1. `bulk.py`'s except clause at line 164 only catches `GitHubError`
      among the concrete provider error types, so a `GitLabError` or
      `AzureDevOpsError` (or a bare `ProviderError`) raised by
      `provider.list_tickets(...)` propagates out of
      `list_tickets_across_projects` entirely, aborting every other
      project in the same call — exactly the reported #288 bug (an
      Azure 401 destroyed the whole batch). Widening the except tuple to
      the common `ProviderError` base fixes this for all three
      providers plus bare lib-raised `ProviderError`s.

  R2. Once R1 isolates the error, `bulk.py:165` still uses bare
      `str(exc)`, so a 401 in the bulk loop is missing the
      provider-specific auth-scope hint that `_safe` already attaches
      for single-project tools (`_providers.py:617-654`). A module-level
      `_error_message` helper (dispatching by concrete error type, since
      bare `ProviderError` has no `_PROVIDER_AUTH_HINTS` match and falls
      back to `str(exc)` — matching `_safe`'s own bare-`ProviderError`
      branch, which also does not add a hint) closes that gap.

Mirrors the fake-provider-raises pattern from
`tests/test_266_error_tone_actionability.py` (`monkeypatch.setitem(
providers_mod._PROVIDERS, <key>, <mock instance>)`) combined with
`tests/test_bulk.py`'s `load_projects` monkeypatch + HTTP-mock fixtures,
so a real HTTP-mocked GitHub project and a stubbed Azure/GitLab provider
can coexist in one bulk call.

Phase = tests: only RED driving tests + compile-level scaffolding here.
No production code (`bulk.py`) is touched in this file.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.base import ProviderError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import bulk as bulk_tools


# ---------- helpers (mirrors tests/test_bulk.py) ------------------------------


def _github_project(
    project_id: str,
    *,
    owner: str = "acme",
    repo: str | None = None,
    token_env: str | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        id=project_id,
        provider="github",
        path=f"{owner}/{repo or project_id}",
        token_env=token_env or f"GITHUB_TOKEN_{project_id.upper()}",
        permissions={"issues": {"create": True, "modify": True}},
    )


def _azure_project(project_id: str, *, token_env: str | None = None) -> ProjectConfig:
    # Azure DevOps validates `path` as 'organization/project/repository'
    # (3 segments) — see tests/test_266_error_tone_actionability.py's
    # `_project` helper, same requirement.
    return ProjectConfig(
        id=project_id,
        provider="azuredevops",
        path="myorg/myproject/myrepo",
        token_env=token_env or f"AZURE_TOKEN_{project_id.upper()}",
        permissions={"issues": {"create": True, "modify": True}},
    )


def _gitlab_project(project_id: str, *, token_env: str | None = None) -> ProjectConfig:
    return ProjectConfig(
        id=project_id,
        provider="gitlab",
        path=f"acme/{project_id}",
        token_env=token_env or f"GITLAB_TOKEN_{project_id.upper()}",
        permissions={"issues": {"create": True, "modify": True}},
    )


def _issue_payload(issue_id: int, title: str = "issue", **overrides) -> dict:
    base = {
        "number": issue_id,
        "title": title,
        "body": "",
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/repo/issues/{issue_id}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _json(payload, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_github_mock(
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


def _register_bulk_with(monkeypatch: pytest.MonkeyPatch, projects: list[ProjectConfig]):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=projects, state="ok", search_root="/tmp")

    # Bulk resolves `load_projects` off its own module (see bulk.py's
    # `_resolve_local`), not `_providers._resolve` — patch both names so
    # neither path can fall through to the real loader.
    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(bulk_tools, "load_projects", fake_load_projects)

    stub = _StubMCP()
    bulk_tools.register(stub)
    return stub.tools


# ---------- mock providers -----------------------------------------------------


class _MockAzure401Provider:
    def list_tickets(self, project, token, filters):
        raise AzureDevOpsError(401, "401 Unauthorized")


class _MockGitLab401Provider:
    def list_tickets(self, project, token, filters):
        raise GitLabError(401, "401 Unauthorized")


class _MockGitLab404Provider:
    def list_tickets(self, project, token, filters):
        raise GitLabError(404, "Project Not Found")


class _MockBareProviderErrorProvider:
    def list_tickets(self, project, token, filters):
        raise ProviderError(400, "bad since= value")


# ===========================================================================
# R1 — a non-GitHub provider error is isolated per project and does not
#      abort the batch (the #288 bug)
# ===========================================================================


def test_288_azure_error_does_not_abort_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test. Two projects, the failing Azure one listed FIRST in
    `project_ids`, a healthy HTTP-mocked GitHub one second.

    RED today: `AzureDevOpsError` is not in the except tuple at
    `bulk.py:164`, so it propagates out of
    `list_tickets_across_projects` — the raw exception aborts the test
    before any assertion runs, and (ordered first) demonstrably prevents
    the healthy GitHub project from ever being queried.

    GREEN once R1 lands: the exception is caught by the widened
    `ProviderError` clause, recorded per project, and the loop
    continues to the healthy project.
    """
    az = _azure_project("az")
    gh = _github_project("gh", repo="repo-gh")
    tools = _register_bulk_with(monkeypatch, [az, gh])
    monkeypatch.setitem(providers_mod._PROVIDERS, "azuredevops", _MockAzure401Provider())

    monkeypatch.setenv("GITHUB_TOKEN_GH", "tok-gh")
    monkeypatch.setenv("AZURE_TOKEN_AZ", "tok-az")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/repo-gh/issues":
            return _json([
                _issue_payload(1, title="first"),
                _issue_payload(2, title="second"),
            ])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["list_tickets_across_projects"](project_ids=["az", "gh"])

    # The healthy project after the failing one must still be queried.
    assert result["results"]["gh"]["error"] is None
    assert len(result["results"]["gh"]["tickets"]) == 2
    assert result["total_tickets"] == 2
    assert result["project_count"] == 2

    # The failing Azure project is isolated, not silently dropped.
    assert result["results"]["az"]["tickets"] == []
    assert result["results"]["az"]["error"] is not None


def test_288_gitlab_error_does_not_abort_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage: GitLab variant of the driving test.
    RED today too (currently uncaught) — `GitLabError` is likewise
    absent from `bulk.py:164`'s except tuple."""
    gl = _gitlab_project("gl")
    gh = _github_project("gh", repo="repo-gh")
    tools = _register_bulk_with(monkeypatch, [gl, gh])
    monkeypatch.setitem(providers_mod._PROVIDERS, "gitlab", _MockGitLab401Provider())

    monkeypatch.setenv("GITHUB_TOKEN_GH", "tok-gh")
    monkeypatch.setenv("GITLAB_TOKEN_GL", "tok-gl")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/acme/repo-gh/issues":
            return _json([_issue_payload(1, title="only")])
        raise AssertionError(f"unexpected request: {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["list_tickets_across_projects"](project_ids=["gl", "gh"])

    assert result["results"]["gh"]["error"] is None
    assert len(result["results"]["gh"]["tickets"]) == 1
    assert result["results"]["gl"]["tickets"] == []
    assert result["results"]["gl"]["error"] is not None


def test_288_bare_provider_error_isolated_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage: a bare `ProviderError(400, ...)`
    (e.g. the lib's `since=`-validation error, raised directly on
    `base.ProviderError`, not on a concrete provider subclass) must be
    isolated the same way. RED today too — currently uncaught, since
    `bulk.py:164` doesn't even catch the concrete subclasses, let alone
    the base class."""
    bad = _gitlab_project("bad")
    tools = _register_bulk_with(monkeypatch, [bad])
    monkeypatch.setitem(providers_mod._PROVIDERS, "gitlab", _MockBareProviderErrorProvider())
    monkeypatch.setenv("GITLAB_TOKEN_BAD", "tok-bad")

    result = tools["list_tickets_across_projects"](project_ids=["bad"])

    assert result["results"]["bad"]["tickets"] == []
    assert result["results"]["bad"]["error"] is not None
    assert result["total_tickets"] == 0
    assert result["project_count"] == 1


def test_288_failed_entry_has_more_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage (already passing today): a failed
    entry's `has_more` is `False`. Uses an unknown-project-id failure —
    a type already caught pre-#288-fix (`LookupError`) — so this
    assertion is already true today; it stays true after R1 widens the
    except tuple to also cover provider errors."""
    known = _github_project("known", repo="repo-known")
    tools = _register_bulk_with(monkeypatch, [known])

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["list_tickets_across_projects"](project_ids=["nonexistent"])

    assert result["results"]["nonexistent"]["has_more"] is False


def test_288_every_project_failing_yields_zero_total_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage (already passing today): a batch
    where every project fails yields `total_tickets == 0` with all
    entries present. Uses unknown-project-id failures (already-caught
    `LookupError`) so this already passes pre-#288-fix; it stays true
    once provider errors are isolated too."""
    tools = _register_bulk_with(monkeypatch, [])

    result = tools["list_tickets_across_projects"](project_ids=["missing-a", "missing-b"])

    assert result["total_tickets"] == 0
    assert set(result["results"].keys()) == {"missing-a", "missing-b"}
    assert len(result["errors"]) == 2


# ===========================================================================
# R2 — a bulk per-project 401 carries the provider-specific scope hint
# ===========================================================================


def test_288_azure_401_in_bulk_gets_scope_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test. The message for a bulk-loop Azure 401 must match
    what `_safe` returns for the same error in a single-project tool:
    the raw message PLUS the Azure DevOps scope hint (which names
    'Work Items').

    RED today (pre-R1): `AzureDevOpsError` isn't even caught, so the
    exception propagates and this test errors before any assertion
    runs — the same RED as R1's driving test. Once R1's except-widening
    lands (R1 and R2 are implemented together per the plan), the RED
    reason shifts to the specific gap R2 targets: `bulk.py:165`'s bare
    `str(exc)` produces 'Azure DevOps 401: 401 Unauthorized' with no
    hint, so the 'Work Items' assertion fails on its own.

    GREEN once R2 lands: `_error_message` routes `AzureDevOpsError`
    through `_with_auth_hint(exc, _AZUREDEVOPS_AUTH_HINT)`.
    """
    az = _azure_project("az")
    tools = _register_bulk_with(monkeypatch, [az])
    monkeypatch.setitem(providers_mod._PROVIDERS, "azuredevops", _MockAzure401Provider())
    monkeypatch.setenv("AZURE_TOKEN_AZ", "tok-az")

    result = tools["list_tickets_across_projects"](project_ids=["az"])

    error_message = result["results"]["az"]["error"]
    assert error_message.startswith("Azure DevOps 401: 401 Unauthorized")
    assert "Work Items" in error_message

    # The identical string appears in the top-level `errors` entry too.
    matching = [e for e in result["errors"] if e["project_id"] == "az"]
    assert len(matching) == 1
    assert matching[0]["error"] == error_message


def test_288_github_401_in_bulk_gets_repo_scope_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage: GitHub 401 -> hint contains 'repo'.
    RED today (a clean assertion failure, not an uncaught exception):
    `GitHubError` IS already caught by `bulk.py:164`'s current except
    tuple, so this reaches line 165's bare `str(exc)` today, with no
    hint appended."""
    gh = _github_project("gh", repo="repo-gh")
    tools = _register_bulk_with(monkeypatch, [gh])

    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"message": "Bad credentials"}, status_code=401)

    _install_github_mock(monkeypatch, handler)
    monkeypatch.setenv("GITHUB_TOKEN_GH", "tok-gh")

    result = tools["list_tickets_across_projects"](project_ids=["gh"])

    error_message = result["results"]["gh"]["error"]
    assert error_message.startswith("GitHub 401:")
    assert "repo" in error_message


def test_288_gitlab_401_in_bulk_gets_api_scope_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage: GitLab 401 -> hint contains 'api'.
    RED today via the uncaught-exception path (pre-R1), same caveat as
    the Azure driving test above."""
    gl = _gitlab_project("gl")
    tools = _register_bulk_with(monkeypatch, [gl])
    monkeypatch.setitem(providers_mod._PROVIDERS, "gitlab", _MockGitLab401Provider())
    monkeypatch.setenv("GITLAB_TOKEN_GL", "tok-gl")

    result = tools["list_tickets_across_projects"](project_ids=["gl"])

    error_message = result["results"]["gl"]["error"]
    assert error_message.startswith("GitLab 401: 401 Unauthorized")
    assert "api" in error_message


def test_288_gitlab_non_401_message_is_byte_identical_to_str_exc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage: a non-401 (`GitLabError(404, ...)`)
    is byte-identical to `str(exc)` — no hint separator appended.
    RED today via the uncaught-exception path (pre-R1)."""
    gl = _gitlab_project("gl")
    tools = _register_bulk_with(monkeypatch, [gl])
    monkeypatch.setitem(providers_mod._PROVIDERS, "gitlab", _MockGitLab404Provider())
    monkeypatch.setenv("GITLAB_TOKEN_GL", "tok-gl")

    result = tools["list_tickets_across_projects"](project_ids=["gl"])

    error_message = result["results"]["gl"]["error"]
    assert error_message == str(GitLabError(404, "Project Not Found"))
    assert " — " not in error_message


def test_288_unknown_project_message_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin (must stay green, unmodified behavior): `LookupError('unknown
    project')` is unaffected by R1/R2 — already asserted by
    `tests/test_bulk.py::test_bulk_unknown_project_id_surfaces_error`;
    repeated here as a same-file regression guard for the exact string
    and shape R2's `_error_message` dispatch must not touch."""
    tools = _register_bulk_with(monkeypatch, [])

    result = tools["list_tickets_across_projects"](project_ids=["nonexistent"])

    entry = result["results"]["nonexistent"]
    assert entry["tickets"] == []
    assert entry["error"] == "unknown project"
    assert result["errors"] == [{"project_id": "nonexistent", "error": "unknown project"}]
