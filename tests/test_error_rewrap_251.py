"""Tests for ticket #251 finding 3: `_rewrap_404` used to append an extra
`({provider} {status})` tag to the rebuilt message, e.g.
`"GitLab 404: pipeline run 'acme#1234' not found (GitLab 404)"` — doubling
the provider/status tag, because `GitLabError.__str__` / `AzureDevOpsError`
already bake a `"{Provider} {status}: "` prefix onto every message.
`GitHubError` happened to mask the bug by stripping a trailing
`(GitHub NNN)` suffix, so only GitLab and Azure DevOps actually doubled up.

Findings 1-2 of #251 are provider-layer gaps tracked separately as
lib-python-projects#208 and are out of scope here.

Mirrors the fake-provider-raises pattern used in `test_error_rewrap_217.py`
/ `test_error_rewrap_218.py` — a mock provider is registered directly into
`providers_mod._PROVIDERS` so no HTTP mocking is needed.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools._providers import _rewrap_404


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _gitlab_project() -> ProjectConfig:
    from lib_python_projects import IssuesPermissions, Permissions
    return ProjectConfig(
        id="acme",
        provider="gitlab",
        path="acme/backend",
        token_env="GITLAB_TOKEN_ACME",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
        ),
    )


def _register_pipeline_tools_with_provider(
    monkeypatch: pytest.MonkeyPatch, provider_instance,
) -> dict[str, Callable]:
    project = _gitlab_project()

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv("GITLAB_TOKEN_ACME", "glpat_token")
    monkeypatch.setitem(providers_mod._PROVIDERS, "gitlab", provider_instance)

    stub = _StubMCP()
    pipeline_tools.register(stub)
    return stub.tools


class _MockGitLabProvider404:
    """Fake GitLab provider whose get_run raises a raw 404."""

    def get_run(self, project, token, run_id, *, include_failure_excerpt=True):
        raise GitLabError(404, "404 Project Not Found")


# ---------------------------------------------------------------------------
# Direct unit tests of `_rewrap_404`
# ---------------------------------------------------------------------------


def test_rewrap_404_gitlab_message_has_no_doubled_provider_tag() -> None:
    exc = GitLabError(404, "Not Found")
    out = _rewrap_404(exc, project_id="acme", kind="pipeline run", ident="1234")
    assert str(out) == "GitLab 404: pipeline run 'acme#1234' not found"
    assert str(out).count("GitLab 404") == 1


def test_rewrap_404_azure_message_has_no_doubled_provider_tag() -> None:
    exc = AzureDevOpsError(404, "Not Found")
    out = _rewrap_404(exc, project_id="acme", kind="ticket", ident="5")
    assert str(out) == "Azure DevOps 404: ticket 'acme#5' not found"
    assert str(out).count("404") == 1


def test_rewrap_404_github_message_unchanged() -> None:
    """No-regression baseline: GitHub already rendered this correctly
    (`GitHubError` strips a trailing `(GitHub NNN)` suffix), so this
    must keep passing unchanged after the fix."""
    exc = GitHubError(404, "Not Found")
    out = _rewrap_404(exc, project_id="acme", kind="ticket", ident="5")
    assert str(out) == "GitHub 404: ticket 'acme#5' not found"


def test_rewrap_404_still_passes_through_non_404_and_label_404() -> None:
    """Pins the existing non-404 pass-through gate and the `_LABEL_404_RE`
    anti-clobber gate — both should already pass today."""
    non_404 = GitHubError(422, "Validation Failed")
    out_non_404 = _rewrap_404(non_404, project_id="acme", kind="ticket", ident="5")
    assert out_non_404 is non_404

    label_404 = GitHubError(404, "label 'x' does not exist in acme")
    out_label_404 = _rewrap_404(label_404, project_id="acme", kind="ticket", ident="5")
    assert out_label_404 is label_404


# ---------------------------------------------------------------------------
# End-to-end test through the tool surface
# ---------------------------------------------------------------------------


def test_get_pipeline_run_gitlab_404_message_not_doubled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_pipeline_tools_with_provider(
        monkeypatch, _MockGitLabProvider404(),
    )

    result = tools["get_pipeline_run"](project_id="acme", run_id="1234")

    assert "error" in result, f"expected error dict; got: {result}"
    message = result["error"]
    assert message.count("GitLab 404") == 1
    assert not message.endswith(")")
