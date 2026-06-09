"""Tests for the `list_custom_fields` MCP tool (ticket #143).

Covers:
- ADO fake provider returns a list of FieldSpec objects; tool serialises them
  correctly with all six keys.
- GitHub fake provider returns []; tool returns fields=[] with no error.
- work_item_type kwarg is forwarded to the provider.
- work_item_type defaults to None when omitted.
- Unknown project_id returns an error dict (not an exception).
- The custom_fields Field description on update_ticket references list_custom_fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pytest
from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import FieldSpec
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import tickets as ticket_tools


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_update_ticket_custom_fields.py)
# ---------------------------------------------------------------------------


def _project(provider: str = "azuredevops") -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else "acme/backend"
    token_env = "ADO_TOKEN_ACME" if provider == "azuredevops" else "GITHUB_TOKEN_ACME"
    return ProjectConfig(
        id="acme",
        provider=provider,
        path=path,
        token_env=token_env,
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_with_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider_instance,
    provider_key: str = "azuredevops",
    token_env: str = "ADO_TOKEN_ACME",
    token_value: str = "ado_token",
) -> tuple[dict[str, Callable], object]:
    project = _project(provider=provider_key)

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv(token_env, token_value)
    monkeypatch.setitem(providers_mod._PROVIDERS, provider_key, provider_instance)

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools, provider_instance


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class _MockADOProvider:
    """Fake ADO provider: list_fields returns two FieldSpec objects."""

    def __init__(self) -> None:
        self.captured_kwargs: dict = {}

    def list_fields(self, project, token, *, work_item_type=None):
        self.captured_kwargs["work_item_type"] = work_item_type
        return [
            FieldSpec(
                reference_name="System.State",
                display_name="State",
                type="picklistString",
                allowed_values=["A", "B"],
                read_only=False,
                always_required=True,
            ),
            FieldSpec(
                reference_name="Custom.Notes",
                display_name="Notes",
                type="string",
                allowed_values=None,
                read_only=False,
                always_required=False,
            ),
        ]


class _MockGitHubProvider:
    """Fake GitHub provider: list_fields returns empty list."""

    def __init__(self) -> None:
        self.captured_kwargs: dict = {}

    def list_fields(self, project, token, *, work_item_type=None):
        self.captured_kwargs["work_item_type"] = work_item_type
        return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_custom_fields_ado_returns_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO provider returns two FieldSpec objects; tool serialises all six keys."""
    mock = _MockADOProvider()
    tools, _ = _register_with_provider(monkeypatch, mock)

    out = tools["list_custom_fields"](project_id="acme")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["project_id"] == "acme"
    assert out["provider"] == "azuredevops"
    assert len(out["fields"]) == 2

    first = out["fields"][0]
    assert set(first.keys()) == {
        "reference_name", "display_name", "type",
        "allowed_values", "read_only", "always_required",
    }, f"field dict has unexpected keys: {first.keys()}"
    assert first["reference_name"] == "System.State"
    assert first["display_name"] == "State"
    assert first["type"] == "picklistString"
    assert first["allowed_values"] == ["A", "B"]
    assert first["read_only"] is False
    assert first["always_required"] is True

    second = out["fields"][1]
    assert second["allowed_values"] is None


def test_list_custom_fields_github_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub provider returns []; tool returns fields=[] with no error."""
    mock = _MockGitHubProvider()
    tools, _ = _register_with_provider(
        monkeypatch, mock,
        provider_key="github",
        token_env="GITHUB_TOKEN_ACME",
        token_value="ghp_token",
    )

    out = tools["list_custom_fields"](project_id="acme")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["fields"] == []
    assert out["provider"] == "github"


def test_list_custom_fields_work_item_type_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """work_item_type kwarg is forwarded to the provider."""
    mock = _MockADOProvider()
    tools, _ = _register_with_provider(monkeypatch, mock)

    tools["list_custom_fields"](project_id="acme", work_item_type="Bug")

    assert mock.captured_kwargs["work_item_type"] == "Bug"


def test_list_custom_fields_work_item_type_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting work_item_type passes None to the provider."""
    mock = _MockADOProvider()
    tools, _ = _register_with_provider(monkeypatch, mock)

    tools["list_custom_fields"](project_id="acme")

    assert mock.captured_kwargs["work_item_type"] is None


def test_list_custom_fields_unknown_project_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown project_id surfaces as an error dict, not an exception."""
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    stub = _StubMCP()
    ticket_tools.register(stub)

    out = stub.tools["list_custom_fields"](project_id="no-such-project")

    assert "error" in out, f"expected error key; got: {out}"


def _param_description(fn: Callable, param: str) -> str:
    schema = func_metadata(fn).arg_model.model_json_schema()
    return schema.get("properties", {}).get(param, {}).get("description", "")


def test_list_custom_fields_custom_fields_description_mentions_tool() -> None:
    """The custom_fields Field description on update_ticket must mention list_custom_fields."""
    stub = _StubMCP()
    ticket_tools.register(stub)
    desc = _param_description(stub.tools["update_ticket"], "custom_fields")
    assert "list_custom_fields" in desc, (
        f"custom_fields description must mention 'list_custom_fields'; got: {desc!r}"
    )
