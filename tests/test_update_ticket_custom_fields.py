"""Tests for the `custom_fields` parameter on `update_ticket`.

Verifies:
- custom_fields is forwarded to providers whose update_ticket signature accepts it
  (e.g. Azure DevOps, and now GitHub — the lib added custom_fields support to
  GitHubProvider.update_ticket, detected dynamically via inspect.signature).
- custom_fields is NOT forwarded (and causes no error) to providers whose signature
  does not include it (e.g. GitLab, the one provider still lacking update-time
  custom_fields support).
- A non-empty custom_fields dict alone satisfies the actionable guard.
- An empty dict or None does NOT satisfy the actionable guard.
- The Field description for custom_fields mentions Azure.
- The update_ticket docstring mentions custom_fields.
"""
from __future__ import annotations

from typing import Callable

import pytest
from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import Ticket
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import tickets as ticket_tools


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_update_ticket_response_shape.py)
# ---------------------------------------------------------------------------


def _project(provider: str = "azuredevops") -> ProjectConfig:
    from lib_python_projects import IssuesPermissions, Permissions
    # Azure DevOps path must be 'organization/project/repository' (three segments).
    # GitHub/GitLab use 'owner/repo' (two segments).
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else "acme/backend"
    token_env = {
        "github": "GITHUB_TOKEN_ACME",
        "gitlab": "GITLAB_TOKEN_ACME",
    }.get(provider, "ADO_TOKEN_ACME")
    return ProjectConfig(
        id="acme",
        provider=provider,
        path=path,
        token_env=token_env,
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
        ),
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _full_ticket() -> Ticket:
    return Ticket(
        id="42",
        title="some title",
        body="some body",
        status="Active",
        author="alice",
        assignees=[],
        labels=[],
        url="https://example.test/workitems/42",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Provider factories — one that accepts custom_fields, one that does not
# ---------------------------------------------------------------------------


class _MockProviderWithCustomFields:
    """Simulates Azure DevOps provider: update_ticket accepts custom_fields."""

    def __init__(self) -> None:
        self.captured: dict = {}

    def update_ticket(self, project_, token, ticket_id, *, custom_fields=None, **kwargs):
        self.captured.update(kwargs)
        self.captured["_ticket_id"] = ticket_id
        self.captured["custom_fields"] = custom_fields
        return _full_ticket()


class _MockProviderWithoutCustomFields:
    """Simulates GitLab provider: update_ticket does NOT accept custom_fields."""

    def __init__(self) -> None:
        self.captured: dict = {}

    def update_ticket(self, project_, token, ticket_id, **kwargs):
        # custom_fields must NOT be in kwargs — if it is, a TypeError should
        # have already been raised by the caller (but our implementation avoids
        # that via the inspect.signature guard).
        self.captured.update(kwargs)
        self.captured["_ticket_id"] = ticket_id
        return _full_ticket()


class _MockGitHubProviderWithCustomFields:
    """Simulates the current GitHub provider: update_ticket accepts
    custom_fields (lib added this support, symmetric with create_ticket)."""

    def __init__(self) -> None:
        self.captured: dict = {}

    def update_ticket(self, project_, token, ticket_id, *, custom_fields=None, **kwargs):
        self.captured.update(kwargs)
        self.captured["_ticket_id"] = ticket_id
        self.captured["custom_fields"] = custom_fields
        return _full_ticket()


def _register_with_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider_instance,
    provider_key: str = "azuredevops",
    token_env: str = "ADO_TOKEN_ACME",
    token_value: str = "ado_token",
) -> tuple[dict[str, Callable], object]:
    project = _project(provider=provider_key)

    # Patch project resolution
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv(token_env, token_value)

    # Inject mock provider
    monkeypatch.setitem(providers_mod._PROVIDERS, provider_key, provider_instance)

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools, provider_instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_custom_fields_forwarded_to_ado_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields is passed through when the provider's update_ticket accepts it."""
    mock_provider = _MockProviderWithCustomFields()
    tools, provider = _register_with_provider(monkeypatch, mock_provider)

    out = tools["update_ticket"](
        project_id="acme",
        ticket_id="42",
        custom_fields={"Custom.ProcessState": "Approved"},
    )

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.captured["custom_fields"] == {"Custom.ProcessState": "Approved"}


def test_custom_fields_only_on_gitlab_provider_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields alone on a provider that doesn't support it must return an error,
    not silently mutate labels via the standard-fields path. GitLab is the one
    provider still lacking update-time custom_fields support (unlike GitHub,
    which now forwards it — see test_custom_fields_forwarded_to_github_provider)."""
    mock_provider = _MockProviderWithoutCustomFields()
    tools, provider = _register_with_provider(
        monkeypatch, mock_provider,
        provider_key="gitlab",
        token_env="GITLAB_TOKEN_ACME",
        token_value="glpat_token",
    )

    out = tools["update_ticket"](
        project_id="acme",
        ticket_id="42",
        custom_fields={"Custom.ProcessState": "Approved"},
    )

    assert "error" in out, f"expected error for unsupported custom_fields; got: {out}"
    assert "not supported" in out["error"], (
        f"error must say 'not supported'; got: {out['error']!r}"
    )
    # Provider must NOT have been called at all.
    assert not provider.captured, (
        "provider must not be called when custom_fields is unsupported and alone"
    )


def test_custom_fields_with_standard_field_silently_dropped_on_gitlab_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When combined with a real standard field, custom_fields is silently dropped
    (not forwarded) and the call succeeds — no TypeError, no error result. GitLab
    remains the provider without update-time custom_fields support."""
    mock_provider = _MockProviderWithoutCustomFields()
    tools, provider = _register_with_provider(
        monkeypatch, mock_provider,
        provider_key="gitlab",
        token_env="GITLAB_TOKEN_ACME",
        token_value="glpat_token",
    )

    out = tools["update_ticket"](
        project_id="acme",
        ticket_id="42",
        status="closed:completed",
        custom_fields={"Custom.ProcessState": "Approved"},
    )

    assert "error" not in out, f"unexpected error: {out}"
    assert "custom_fields" not in provider.captured, (
        "custom_fields must NOT be forwarded to providers lacking the parameter"
    )


def test_custom_fields_forwarded_to_github_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: the lib added custom_fields support to GitHub's
    update_ticket (symmetric with create_ticket). custom_fields as the ONLY
    changed field must be forwarded, not rejected as unsupported."""
    mock_provider = _MockGitHubProviderWithCustomFields()
    tools, provider = _register_with_provider(
        monkeypatch, mock_provider,
        provider_key="github",
        token_env="GITHUB_TOKEN_ACME",
        token_value="ghp_token",
    )

    out = tools["update_ticket"](
        project_id="acme",
        ticket_id="42",
        custom_fields={"Status": "Done"},
    )

    assert "error" not in out, f"unexpected error: {out}"
    assert provider.captured["custom_fields"] == {"Status": "Done"}


def test_custom_fields_alone_satisfies_actionable_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty custom_fields dict is sufficient to pass the actionable guard."""
    mock_provider = _MockProviderWithCustomFields()
    tools, _ = _register_with_provider(monkeypatch, mock_provider)

    out = tools["update_ticket"](
        project_id="acme",
        ticket_id="42",
        custom_fields={"Custom.X": "v"},
    )

    assert "error" not in out, f"actionable guard incorrectly rejected custom_fields: {out}"


def test_empty_custom_fields_does_not_satisfy_actionable_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty dict for custom_fields is treated as 'no action' — guard fires."""
    mock_provider = _MockProviderWithCustomFields()
    tools, _ = _register_with_provider(monkeypatch, mock_provider)

    out = tools["update_ticket"](
        project_id="acme",
        ticket_id="42",
        custom_fields={},
    )

    assert "error" in out, "empty custom_fields should fail the actionable guard"
    assert "no update fields" in out["error"]


def test_none_custom_fields_rejected_when_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicitly passing custom_fields=None (with no other fields) triggers the guard."""
    mock_provider = _MockProviderWithCustomFields()
    tools, _ = _register_with_provider(monkeypatch, mock_provider)

    out = tools["update_ticket"](
        project_id="acme",
        ticket_id="42",
        custom_fields=None,
    )

    assert "error" in out, "custom_fields=None alone should fail the actionable guard"
    assert "no update fields" in out["error"]


def _param_description(fn: Callable, param: str) -> str:
    schema = func_metadata(fn).arg_model.model_json_schema()
    return schema.get("properties", {}).get(param, {}).get("description", "")


def test_custom_fields_param_description_mentions_azure() -> None:
    """The Field description for custom_fields must mention Azure."""
    stub = _StubMCP()
    ticket_tools.register(stub)
    desc = _param_description(stub.tools["update_ticket"], "custom_fields")
    assert "Azure" in desc, (
        f"custom_fields description must mention 'Azure'; got: {desc!r}"
    )


def test_update_ticket_docstring_mentions_custom_fields() -> None:
    """The update_ticket docstring must reference custom_fields."""
    stub = _StubMCP()
    ticket_tools.register(stub)
    doc = stub.tools["update_ticket"].__doc__ or ""
    assert "custom_fields" in doc, (
        f"update_ticket docstring must mention 'custom_fields'; got: {doc[:400]!r}"
    )
