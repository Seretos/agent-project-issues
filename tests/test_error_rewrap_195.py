"""Tests for ticket #195: normalize raw provider errors leaking through
`update_ticket` (finding 1) and `create_pr` (finding 2).

Mirrors the fake-provider-raises pattern used for the `_rewrap_404` /
`_rewrap_work_item_type_404` tests in `test_list_custom_fields.py` — a
mock provider is registered directly into `providers_mod._PROVIDERS` so
no HTTP mocking is needed.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.base import PullRequest
from lib_python_projects.providers.github import GitHubError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import tickets as ticket_tools
from project_issues_plugin.tools._providers import (
    _rewrap_422_assignee,
    _rewrap_azure_bad_base,
)


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


# ---------------------------------------------------------------------------
# Finding 1: update_ticket / GitHub 422 invalid-assignee rewrap
# ---------------------------------------------------------------------------


def _ticket_project() -> ProjectConfig:
    from lib_python_projects import IssuesPermissions, Permissions
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
        ),
    )


def _register_ticket_tools_with_provider(
    monkeypatch: pytest.MonkeyPatch, provider_instance,
) -> dict[str, Callable]:
    project = _ticket_project()

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "ghp_token")
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", provider_instance)

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


class _MockGitHubProvider422:
    """Fake GitHub provider whose update_ticket raises a raw 422 for an
    invalid assignee, mirroring GitHub's real `Validation Failed:
    Issue.assignees='<login>' (invalid)` response body."""

    def __init__(self, message: str = "Validation Failed: Issue.assignees='baduser' (invalid)"):
        self._message = message

    def update_ticket(self, project, token, ticket_id, **kwargs):
        raise GitHubError(422, self._message)


class _MockGitHubProvider500:
    def update_ticket(self, project, token, ticket_id, **kwargs):
        raise GitHubError(500, "Internal Server Error")


class _MockGitHubProvider404:
    def update_ticket(self, project, token, ticket_id, **kwargs):
        raise GitHubError(404, "Not Found")


def test_update_ticket_invalid_assignee_hides_raw_provider_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`update_ticket(assignees_add=["baduser"])` against a non-collaborator
    must not leak GitHub's raw 'Validation Failed: Issue.assignees=...'
    body; the message names the offending login instead."""
    tools = _register_ticket_tools_with_provider(
        monkeypatch, _MockGitHubProvider422(),
    )

    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", assignees_add=["baduser"],
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "baduser" in message
    assert "not a valid GitHub user/collaborator" in message
    assert "Validation Failed" not in message
    assert "Issue.assignees=" not in message


def test_update_ticket_invalid_assignee_unparseable_falls_back_to_input_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the offending login can't be parsed out of GitHub's message,
    the fallback names the caller's own `assignees_add` input list."""
    tools = _register_ticket_tools_with_provider(
        monkeypatch,
        _MockGitHubProvider422(message="Validation Failed: assignees are invalid somehow"),
    )

    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", assignees_add=["baduser", "otheruser"],
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "baduser" in message
    assert "otheruser" in message
    assert "not valid GitHub users/collaborators" in message


def test_update_ticket_non_422_error_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-422 GitHubError (e.g. 500) is untouched by the assignee
    rewrap; it surfaces via the normal `_safe` translation."""
    tools = _register_ticket_tools_with_provider(
        monkeypatch, _MockGitHubProvider500(),
    )

    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", assignees_add=["baduser"],
    )

    assert "error" in out, f"expected error dict; got: {out}"
    assert "Internal Server Error" in out["error"]


def test_update_ticket_404_still_rewraps_alongside_422_rewrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `_rewrap_404` context rewrap and the new `_rewrap_422_assignee`
    coexist: a plain 404 (unrelated to assignees) is still rewrapped by
    `_rewrap_404` with project/id context, unaffected by the 422-only
    assignee gate."""
    tools = _register_ticket_tools_with_provider(
        monkeypatch, _MockGitHubProvider404(),
    )

    out = tools["update_ticket"](
        project_id="acme", ticket_id="5", assignees_add=["baduser"],
    )

    assert "error" in out, f"expected error dict; got: {out}"
    assert "acme#5" in out["error"]
    assert "not found" in out["error"]


def test_rewrap_422_assignee_passes_through_non_matching_422() -> None:
    """Direct unit test: a 422 that doesn't mention `assignees` at all
    (e.g. an invalid label) passes through unchanged."""
    exc = GitHubError(422, "Validation Failed: Label.name=['bogus'] (invalid)")
    out = _rewrap_422_assignee(exc, assignees_add=["baduser"])
    assert out is exc


def test_rewrap_422_assignee_passes_through_non_422() -> None:
    """Direct unit test: any non-422 status is untouched, even if the
    message happens to mention assignees."""
    exc = GitHubError(500, "assignees blew up")
    out = _rewrap_422_assignee(exc, assignees_add=["baduser"])
    assert out is exc


# ---------------------------------------------------------------------------
# Finding 2: create_pr / Azure DevOps 400 bad-base-branch rewrap
# ---------------------------------------------------------------------------


def _pr_project(provider: str = "azuredevops") -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else "acme/backend"
    token_env = "ADO_TOKEN_ACME" if provider == "azuredevops" else "GITHUB_TOKEN_ACME"
    return ProjectConfig(
        id="acme",
        provider=provider,
        path=path,
        token_env=token_env,
        permissions={
            "issues": {"create": True, "modify": True},
            "pulls": {"create": True, "modify": True, "merge": True},
        },
    )


def _register_pull_tools_with_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider_instance,
    *,
    provider_key: str = "azuredevops",
    token_env: str = "ADO_TOKEN_ACME",
) -> dict[str, Callable]:
    project = _pr_project(provider=provider_key)

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setattr(pull_tools, "load_projects", fake_load_projects)
    monkeypatch.setenv(token_env, "tok")
    monkeypatch.setitem(providers_mod._PROVIDERS, provider_key, provider_instance)

    stub = _StubMCP()
    pull_tools.register(stub)
    return stub.tools


class _MockAzureProviderBadBase:
    """Fake Azure DevOps provider whose create_pr raises a raw 400 for an
    unusable base branch, mirroring Azure's real TF401398 activation
    failure text."""

    def create_pr(self, project, token, title, body, head, base, **kwargs):
        raise AzureDevOpsError(
            400,
            "TF401398: The pull request cannot be activated because "
            "one or more reviewers rejected the changes, or the branch "
            "no longer exists.",
        )


class _MockAzureProviderOtherBadRequest:
    def create_pr(self, project, token, title, body, head, base, **kwargs):
        raise AzureDevOpsError(400, "TF401027: You need Git 'GenericContribute' permission.")


class _MockAzureProvider500:
    def create_pr(self, project, token, title, body, head, base, **kwargs):
        raise AzureDevOpsError(500, "Internal Server Error")


def test_create_pr_bad_base_branch_hides_raw_azure_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`create_pr` against a non-existent base branch must not leak
    Azure's raw 'TF401398: The pull request cannot be activated...' body
    verbatim; the message names the `base` branch instead."""
    tools = _register_pull_tools_with_provider(
        monkeypatch, _MockAzureProviderBadBase(),
    )

    out = tools["create_pr"](
        project_id="acme", title="t", body="b", head="feature/x", base="no-such-branch",
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "no-such-branch" in message
    assert "TF401398" not in message
    assert "cannot be activated" not in message


def test_create_pr_unrelated_400_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 unrelated to the base branch (e.g. a permissions error) is
    not touched by the bad-base rewrap and surfaces via the normal
    `_safe` translation."""
    tools = _register_pull_tools_with_provider(
        monkeypatch, _MockAzureProviderOtherBadRequest(),
    )

    out = tools["create_pr"](
        project_id="acme", title="t", body="b", head="feature/x", base="main",
    )

    assert "error" in out, f"expected error dict; got: {out}"
    assert "TF401027" in out["error"]


def test_create_pr_non_400_azure_error_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-400 AzureDevOpsError (e.g. 500) is untouched by the bad-base
    rewrap."""
    tools = _register_pull_tools_with_provider(
        monkeypatch, _MockAzureProvider500(),
    )

    out = tools["create_pr"](
        project_id="acme", title="t", body="b", head="feature/x", base="main",
    )

    assert "error" in out, f"expected error dict; got: {out}"
    assert "Internal Server Error" in out["error"]


def test_create_pr_github_happy_path_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new try/except around `provider.create_pr` must not regress the
    happy path on a provider that raises nothing."""

    class _MockGitHubProviderHappy:
        def create_pr(self, project, token, title, body, head, base, **kwargs):
            return PullRequest(
                id="42",
                number=42,
                title=title,
                body=body,
                status="open",
                draft=False,
                author="alice",
                assignees=[],
                reviewers=[],
                requested_reviewers=[],
                labels=[],
                head={"ref": head, "sha": "deadbeef", "repo_full_name": "acme/backend"},
                base={"ref": base, "sha": "cafebabe"},
                merged=False,
                mergeable=None,
                url="https://github.com/acme/backend/pull/42",
                created_at="2024-01-01T00:00:00Z",
                updated_at="2024-01-01T00:00:00Z",
            )

    tools = _register_pull_tools_with_provider(
        monkeypatch, _MockGitHubProviderHappy(),
        provider_key="github", token_env="GITHUB_TOKEN_ACME",
    )

    out = tools["create_pr"](
        project_id="acme", title="t", body="b", head="feature/x", base="main",
    )

    assert "error" not in out, f"unexpected error: {out}"
    assert out["pull_request"]["id"] == "42"


def test_rewrap_azure_bad_base_passes_through_non_matching_400() -> None:
    """Direct unit test: a 400 that doesn't signal a base/target-branch
    activation problem passes through unchanged."""
    exc = AzureDevOpsError(400, "TF401027: You need Git permission.")
    out = _rewrap_azure_bad_base(exc, base="main")
    assert out is exc


def test_rewrap_azure_bad_base_passes_through_non_400() -> None:
    """Direct unit test: any non-400 status is untouched, even if the
    message contains TF401398."""
    exc = AzureDevOpsError(500, "TF401398: whatever")
    out = _rewrap_azure_bad_base(exc, base="main")
    assert out is exc
