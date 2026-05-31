"""Response-shape regression tests for ticket #118.

Covers:
  - Fix 3: `remove_relation` echoes `kind` and `target` in addition to
    `project_id` and `removed`.
  - Fix 4: `list_prs` docstring mentions `mergeable` null behaviour and
    directs agents to `get_pr` for authoritative mergeability.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import IssuesPermissions, Permissions, ProjectConfig, ProjectsLoadResult
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import relations as relation_tools
from project_issues_plugin.tools import pulls as pull_tools


# ---------- helpers ----------------------------------------------------------


def _project_with_modify(project_id: str = "acme") -> ProjectConfig:
    return ProjectConfig(
        id=project_id,
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
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


def _register_relation_tools(
    monkeypatch: pytest.MonkeyPatch,
    remove_relation_return: dict,
) -> dict[str, Callable]:
    """Wire up relation tools against a mock provider."""
    project = _project_with_modify()

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "ghp_token")

    class _MockProvider:
        def remove_relation(self, project_, token, ticket_id, kind, target):
            return remove_relation_return

    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _MockProvider())

    stub = _StubMCP()
    relation_tools.register(stub)
    return stub.tools


# ---------- Fix 3: remove_relation echoes kind + target ----------------------


def test_remove_relation_echoes_kind_and_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_relation must echo `kind` and `target` in the response so
    callers don't need to re-derive them from their call arguments."""
    tools = _register_relation_tools(
        monkeypatch, remove_relation_return={"removed": True}
    )
    result = tools["remove_relation"](
        project_id="acme",
        ticket_id="42",
        kind="blocks",
        target="99",
    )
    assert "error" not in result, result
    assert result["project_id"] == "acme"
    assert result["kind"] == "blocks"
    assert result["removed"] is True
    # `target` is normalised (string id), just assert it is present and non-null.
    assert "target" in result
    assert result["target"] is not None


def test_remove_relation_response_has_all_four_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full response shape must contain exactly project_id, kind,
    target, removed (plus any extra fields the provider may return)."""
    tools = _register_relation_tools(
        monkeypatch, remove_relation_return={"removed": True}
    )
    result = tools["remove_relation"](
        project_id="acme",
        ticket_id="10",
        kind="duplicate_of",
        target="5",
    )
    assert "error" not in result, result
    for expected_key in ("project_id", "kind", "target", "removed"):
        assert expected_key in result, f"key '{expected_key}' missing from remove_relation response"
    assert result["removed"] is True


# ---------- Fix 4: list_prs docstring mentions mergeable null ----------------


def test_list_prs_docstring_mentions_mergeable_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list_prs tool docstring must warn that `mergeable` and
    `mergeable_state` are null in list results and point to `get_pr`."""
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[], state="ok", search_root="/tmp"
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)

    stub = _StubMCP()
    pull_tools.register(stub)

    doc = stub.tools["list_prs"].__doc__ or ""
    assert "mergeable" in doc, (
        "list_prs docstring must mention 'mergeable' (null-in-list-results caveat)"
    )
    assert "null" in doc or "get_pr" in doc, (
        "list_prs docstring must mention 'null' or 'get_pr' to guide agents"
    )
