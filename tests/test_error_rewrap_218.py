"""Tests for ticket #218 finding 3 — three raw provider error bodies used to
reach the agent verbatim, leaking internal field names / status codes /
work-item ids:

  1. `update_label(new_name=<existing label>)` against GitHub surfaced
     `"GitHub 422: Validation Failed: Label.name (already_exists)"`.
  2. `add_relation(kind="parent", ...)` against Azure DevOps when the target
     already has a parent surfaced `"Azure DevOps 400: TF201036: ... work
     items 66 and 59 ..."`.
  3. `create_ticket(custom_fields=...)` / `update_ticket(custom_fields=...)`
     against Azure DevOps with an unrecognised field reference surfaced
     `"Azure DevOps 400: TF51535: Cannot find field Custom.X."`.

Each gets a new narrowly-gated `_rewrap_*` helper in `tools/_providers.py`,
following the exact #214/#217 recipe: module-level compiled regex, gate on
status + message pattern, build the replacement message solely from the
caller's own inputs, pass through unchanged on non-match.

Mirrors the fake-provider-raises pattern used in `test_error_rewrap_217.py`.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.github import GitHubError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import labels as label_tools
from project_issues_plugin.tools import relations as relation_tools
from project_issues_plugin.tools import tickets as ticket_tools
from project_issues_plugin.tools._providers import (
    _rewrap_404,
    _rewrap_422_assignee,
    _rewrap_azure_single_parent,
    _rewrap_azure_unknown_field,
    _rewrap_label_404,
    _rewrap_label_already_exists,
)


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _github_project() -> ProjectConfig:
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


def _azure_project() -> ProjectConfig:
    from lib_python_projects import IssuesPermissions, Permissions
    return ProjectConfig(
        id="acme",
        provider="azuredevops",
        path="myorg/myproject/myrepo",
        token_env="ADO_TOKEN_ACME",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
        ),
    )


def _register_with_provider(
    monkeypatch: pytest.MonkeyPatch,
    module,
    project: ProjectConfig,
    provider_instance,
    token_env: str,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv(token_env, "sometoken")
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)

    stub = _StubMCP()
    module.register(stub)
    return stub.tools


# ===========================================================================
# Finding 3a — update_label(new_name=<existing>) already_exists 422
# ===========================================================================


class _MockGitHubProviderLabelAlreadyExists:
    def update_label(self, project, token, name, *, new_name=None, color=None, description=None):
        raise GitHubError(422, "Validation Failed: Label.name (already_exists)")


def test_update_label_new_name_collision_is_not_raw_github_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_with_provider(
        monkeypatch, label_tools, _github_project(),
        _MockGitHubProviderLabelAlreadyExists(), "GITHUB_TOKEN_ACME",
    )

    out = tools["update_label"](project_id="acme", name="bug", new_name="enhancement")

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "enhancement" in message
    assert "already exists" in message
    assert "already_exists" not in message
    assert "Label.name" not in message


def test_rewrap_label_already_exists_names_the_new_name() -> None:
    exc = GitHubError(422, "Validation Failed: Label.name (already_exists)")
    out = _rewrap_label_already_exists(exc, new_name="enhancement")
    assert out is not exc
    assert "enhancement" in out.message
    assert "already exists" in out.message


def test_rewrap_label_already_exists_passes_through_non_matching_status() -> None:
    exc = GitHubError(404, "Validation Failed: Label.name (already_exists)")
    out = _rewrap_label_already_exists(exc, new_name="enhancement")
    assert out is exc


def test_rewrap_label_already_exists_passes_through_non_matching_message() -> None:
    exc = GitHubError(422, "Validation Failed: Label.color (invalid)")
    out = _rewrap_label_already_exists(exc, new_name="enhancement")
    assert out is exc


# ===========================================================================
# Finding 3b — add_relation(kind="parent", ...) Azure single-parent 400
# ===========================================================================


class _MockAzureProviderSingleParent:
    def add_relation(self, project, token, ticket_id, kind, target):
        raise AzureDevOpsError(
            400,
            "TF201036: You are trying to add a Parent link between work "
            "items 66 and 59, but work item 59 already has a Parent link.",
        )


def test_add_relation_azure_single_parent_is_not_raw_provider_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_with_provider(
        monkeypatch, relation_tools, _azure_project(),
        _MockAzureProviderSingleParent(), "ADO_TOKEN_ACME",
    )

    out = tools["add_relation"](
        project_id="acme", ticket_id="59", kind="parent", target="66",
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "one parent" in message
    assert "TF201036" not in message
    # The 59 / 66 that appear are our own ticket_id/target inputs (which
    # happen to match the provider's internal ids in this fixture), not
    # the raw TF201036 body — the raw phrasing ("Parent link between
    # work items") is gone.
    assert "59" in message
    assert "66" in message
    assert "Parent link between work items" not in message


def test_rewrap_azure_single_parent_builds_message_from_call_inputs() -> None:
    exc = AzureDevOpsError(
        400,
        "TF201036: work items 66 and 59 already has a Parent link.",
    )
    out = _rewrap_azure_single_parent(exc, ticket_id="59", target="66", kind="parent")
    assert out is not exc
    assert "59" in out.message
    assert "66" in out.message
    assert "parent" in out.message
    assert "one parent" in out.message


def test_rewrap_azure_single_parent_passes_through_non_matching_status() -> None:
    exc = AzureDevOpsError(404, "TF201036: whatever")
    out = _rewrap_azure_single_parent(exc, ticket_id="59", target="66", kind="parent")
    assert out is exc


def test_rewrap_azure_single_parent_passes_through_non_matching_message() -> None:
    exc = AzureDevOpsError(400, "TF401398: some other activation failure")
    out = _rewrap_azure_single_parent(exc, ticket_id="59", target="66", kind="parent")
    assert out is exc


# ===========================================================================
# Finding 3c — create_ticket / update_ticket unrecognised custom_fields 400
# ===========================================================================


class _MockAzureProviderUnknownField:
    def create_ticket(self, project, token, title, body, labels, assignees, *, status=None, custom_fields=None):
        raise AzureDevOpsError(
            400, "TF51535: Cannot find field Custom.ThisFieldDoesNotExist.",
        )

    def update_ticket(self, project, token, ticket_id, *, custom_fields=None, **kwargs):
        raise AzureDevOpsError(
            400, "TF51535: Cannot find field Custom.ThisFieldDoesNotExist.",
        )


def test_create_ticket_azure_unknown_field_is_not_raw_provider_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_with_provider(
        monkeypatch, ticket_tools, _azure_project(),
        _MockAzureProviderUnknownField(), "ADO_TOKEN_ACME",
    )

    out = tools["create_ticket"](
        project_id="acme", title="t",
        custom_fields={"Custom.ThisFieldDoesNotExist": "x"},
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "'Custom.ThisFieldDoesNotExist'" in message
    assert "'Custom.ThisFieldDoesNotExist.'" not in message
    assert "TF51535" not in message
    assert "list_custom_fields" in message


def test_update_ticket_azure_unknown_field_is_not_raw_provider_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _register_with_provider(
        monkeypatch, ticket_tools, _azure_project(),
        _MockAzureProviderUnknownField(), "ADO_TOKEN_ACME",
    )

    out = tools["update_ticket"](
        project_id="acme", ticket_id="5",
        custom_fields={"Custom.ThisFieldDoesNotExist": "x"},
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert "'Custom.ThisFieldDoesNotExist'" in message
    assert "'Custom.ThisFieldDoesNotExist.'" not in message
    assert "TF51535" not in message
    assert "list_custom_fields" in message


def test_rewrap_azure_unknown_field_names_the_parsed_field() -> None:
    exc = AzureDevOpsError(
        400, "TF51535: Cannot find field Custom.ThisFieldDoesNotExist.",
    )
    out = _rewrap_azure_unknown_field(exc, custom_fields={"Custom.ThisFieldDoesNotExist": "x"})
    assert out is not exc
    # Exact match on the quoted field name — catches a regex that greedily
    # swallows Azure's sentence-terminating period (e.g. yielding
    # "Custom.ThisFieldDoesNotExist." instead of the bare field name), which
    # a substring-containment check alone would miss.
    assert "'Custom.ThisFieldDoesNotExist'" in out.message
    assert "'Custom.ThisFieldDoesNotExist.'" not in out.message
    assert "TF51535" not in out.message
    assert "list_custom_fields" in out.message


def test_rewrap_azure_unknown_field_falls_back_to_caller_keys_when_unparseable() -> None:
    exc = AzureDevOpsError(400, "TF51535: Cannot find field.")
    out = _rewrap_azure_unknown_field(
        exc, custom_fields={"Custom.Foo": "1", "Custom.Bar": "2"},
    )
    assert out is not exc
    assert "Custom.Foo" in out.message
    assert "Custom.Bar" in out.message
    assert "list_custom_fields" in out.message


def test_rewrap_azure_unknown_field_passes_through_non_matching_status() -> None:
    exc = AzureDevOpsError(404, "TF51535: Cannot find field Custom.X.")
    out = _rewrap_azure_unknown_field(exc, custom_fields={"Custom.X": "1"})
    assert out is exc


def test_rewrap_azure_unknown_field_passes_through_non_matching_message() -> None:
    exc = AzureDevOpsError(400, "TF401398: unrelated activation failure")
    out = _rewrap_azure_unknown_field(exc, custom_fields={"Custom.X": "1"})
    assert out is exc


# ===========================================================================
# Co-existence — the new Azure-400 rewrap must not clobber update_ticket's
# existing 404 (bad ticket id / bad label) or 422 (bad assignee) rewraps.
# ===========================================================================


def test_rewrap_azure_unknown_field_does_not_disturb_existing_404_rewrap() -> None:
    exc = GitHubError(404, "Not Found")
    step1 = _rewrap_404(exc, project_id="acme", kind="ticket", ident="5")
    step2 = _rewrap_label_404(step1, labels_add=None)
    step3 = _rewrap_422_assignee(step2, assignees_add=None)
    final = _rewrap_azure_unknown_field(step3, custom_fields={"Custom.X": "1"})
    assert final is step3
    assert "acme#5" in final.message
    assert "not found" in final.message


def test_rewrap_azure_unknown_field_does_not_disturb_existing_422_assignee_rewrap() -> None:
    exc = GitHubError(422, "Validation Failed: Issue.assignees='baduser' (invalid)")
    step1 = _rewrap_404(exc, project_id="acme", kind="ticket", ident="5")
    step2 = _rewrap_label_404(step1, labels_add=None)
    step3 = _rewrap_422_assignee(step2, assignees_add=["baduser"])
    final = _rewrap_azure_unknown_field(step3, custom_fields={"Custom.X": "1"})
    assert final is step3
    assert "baduser" in final.message
