"""Tests for tickets #166 / #167 / #168: wiring the new lib-python-projects
v0.2.3 ticket-field surface through to the MCP tools.

Covers:
- get_ticket: `ticket.acceptance_criteria` flows through unchanged (#168).
- get_ticket: `include_custom_fields` is forwarded to the provider and
  `ticket.custom_fields` flows through when populated (#167).
- create_ticket: `custom_fields` is forwarded to the provider (#167).
- list_custom_fields: populated `allowed_values` flows through unchanged
  (#166) — a focused regression check; broader coverage already lives in
  test_list_custom_fields.py.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import IssuesPermissions, Permissions, ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import FieldSpec, Ticket
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import tickets as ticket_tools


def _project(provider: str = "azuredevops") -> ProjectConfig:
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else "acme/backend"
    token_env = "ADO_TOKEN_ACME" if provider == "azuredevops" else "GITHUB_TOKEN_ACME"
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


def _full_ticket(**overrides) -> Ticket:
    base = dict(
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
    base.update(overrides)
    return Ticket(**base)


# ---------------------------------------------------------------------------
# #168 — acceptance_criteria passthrough on get_ticket
# ---------------------------------------------------------------------------


class _MockGetTicketProvider:
    def __init__(self, ticket: Ticket) -> None:
        self._ticket = ticket
        self.captured_kwargs: dict = {}

    def get_ticket(self, project_, token, ticket_id, *, include_relations=True, include_custom_fields=False):
        self.captured_kwargs["include_relations"] = include_relations
        self.captured_kwargs["include_custom_fields"] = include_custom_fields
        return self._ticket, [], [], False


def test_get_ticket_response_includes_acceptance_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`acceptance_criteria` from the provider's Ticket flows through unchanged."""
    ticket = _full_ticket(acceptance_criteria="Given/When/Then...")
    mock = _MockGetTicketProvider(ticket)
    tools, _ = _register_with_provider(monkeypatch, mock)

    out = tools["get_ticket"](project_id="acme", ticket_id="42")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["ticket"]["acceptance_criteria"] == "Given/When/Then..."


def test_get_ticket_response_acceptance_criteria_defaults_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providers with no acceptance-criteria concept (GitHub/GitLab) report ''."""
    ticket = _full_ticket()  # acceptance_criteria left at its default ""
    mock = _MockGetTicketProvider(ticket)
    tools, _ = _register_with_provider(
        monkeypatch, mock, provider_key="github", token_env="GITHUB_TOKEN_ACME",
        token_value="ghp_token",
    )

    out = tools["get_ticket"](project_id="acme", ticket_id="42")

    assert out["ticket"]["acceptance_criteria"] == ""


# ---------------------------------------------------------------------------
# #167 — include_custom_fields on get_ticket
# ---------------------------------------------------------------------------


def test_get_ticket_include_custom_fields_defaults_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting include_custom_fields forwards False to the provider."""
    mock = _MockGetTicketProvider(_full_ticket())
    tools, _ = _register_with_provider(monkeypatch, mock)

    tools["get_ticket"](project_id="acme", ticket_id="42")

    assert mock.captured_kwargs["include_custom_fields"] is False


def test_get_ticket_include_custom_fields_forwarded_when_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_custom_fields=True is forwarded to the provider and the
    populated custom_fields map flows through in the response."""
    ticket = _full_ticket(custom_fields={"System.AreaPath": "Proj\\Team"})
    mock = _MockGetTicketProvider(ticket)
    tools, _ = _register_with_provider(monkeypatch, mock)

    out = tools["get_ticket"](project_id="acme", ticket_id="42", include_custom_fields=True)

    assert mock.captured_kwargs["include_custom_fields"] is True
    assert out["ticket"]["custom_fields"] == {"System.AreaPath": "Proj\\Team"}


def test_get_ticket_custom_fields_none_when_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields stays None when the provider reports 'not applicable'
    (e.g. GitHub with no board binding configured) — not an error."""
    ticket = _full_ticket(custom_fields=None)
    mock = _MockGetTicketProvider(ticket)
    tools, _ = _register_with_provider(
        monkeypatch, mock, provider_key="github", token_env="GITHUB_TOKEN_ACME",
        token_value="ghp_token",
    )

    out = tools["get_ticket"](project_id="acme", ticket_id="42", include_custom_fields=True)

    assert "error" not in out, f"unexpected error: {out}"
    assert out["ticket"]["custom_fields"] is None


# ---------------------------------------------------------------------------
# #167 — custom_fields on create_ticket
# ---------------------------------------------------------------------------


class _MockCreateTicketProvider:
    def __init__(self, ticket: Ticket) -> None:
        self._ticket = ticket
        self.captured_kwargs: dict = {}

    def create_ticket(self, project_, token, title, body, labels, assignees, *, status=None, custom_fields=None):
        self.captured_kwargs.update(
            title=title, body=body, labels=labels, assignees=assignees,
            status=status, custom_fields=custom_fields,
        )
        return self._ticket


def test_create_ticket_custom_fields_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_fields is forwarded to the provider's create_ticket."""
    mock = _MockCreateTicketProvider(_full_ticket())
    tools, _ = _register_with_provider(monkeypatch, mock)

    out = tools["create_ticket"](
        project_id="acme", title="t",
        custom_fields={"Custom.ProcessState": "Approved"},
    )

    assert "error" not in out, f"unexpected error: {out}"
    assert mock.captured_kwargs["custom_fields"] == {"Custom.ProcessState": "Approved"}


def test_create_ticket_custom_fields_defaults_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting custom_fields forwards None to the provider (a no-op)."""
    mock = _MockCreateTicketProvider(_full_ticket())
    tools, _ = _register_with_provider(monkeypatch, mock)

    tools["create_ticket"](project_id="acme", title="t")

    assert mock.captured_kwargs["custom_fields"] is None


def test_create_ticket_custom_fields_error_surfaces_as_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError from the provider (e.g. missing board binding on GitHub)
    surfaces as {"error": ...}, not a traceback."""
    class _RaisingProvider:
        def create_ticket(self, *args, **kwargs):
            raise ValueError(
                "custom_fields was provided but project 'acme' has no "
                "'github-projects-v2' board configured"
            )

    tools, _ = _register_with_provider(
        monkeypatch, _RaisingProvider(), provider_key="github",
        token_env="GITHUB_TOKEN_ACME", token_value="ghp_token",
    )

    out = tools["create_ticket"](
        project_id="acme", title="t", custom_fields={"Status": "Done"},
    )

    assert "error" in out, f"expected error dict; got: {out}"
    assert "board" in out["error"]


# ---------------------------------------------------------------------------
# #166 — list_custom_fields allowed_values passthrough (focused regression)
# ---------------------------------------------------------------------------


def test_list_custom_fields_allowed_values_passthrough_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Populated allowed_values (e.g. from AzDO classification nodes) flow
    through list_custom_fields unchanged — no server-side stripping."""
    class _MockFieldsProvider:
        def list_fields(self, project_, token, *, work_item_type=None):
            return [
                FieldSpec(
                    reference_name="System.AreaPath",
                    display_name="Area Path",
                    type="treePath",
                    allowed_values=["Proj\\TeamA", "Proj\\TeamB"],
                    read_only=False,
                    always_required=False,
                ),
            ]

    tools, _ = _register_with_provider(monkeypatch, _MockFieldsProvider())

    out = tools["list_custom_fields"](project_id="acme")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["fields"][0]["allowed_values"] == ["Proj\\TeamA", "Proj\\TeamB"]
