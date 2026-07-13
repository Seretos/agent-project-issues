"""Tests for ticket-hierarchy discoverability (#213 gap 1): the new
`list_hierarchy` tool.

Agents previously had to call `get_ticket` per candidate and hand-filter
its `relations` list for `parent`/`child` entries to reconstruct an
epic hierarchy. `list_hierarchy` makes the same single `get_ticket`
call (`include_relations=True`) and projects `parent` / `children`
directly, so this suite proves:
  - the projection is correct against a mixed relations list (parent,
    children, plus unrelated kinds that must NOT leak through);
  - the edge cases (no parent, no children, empty relations) behave;
  - `relations_truncated` passes through from the provider unchanged;
  - the provider is called with `include_relations=True` (no separate
    resolution path, no extra calls);
  - provider errors (404 and generic) surface as `{"error": ...}`
    rather than a traceback, matching `get_ticket`'s own contract.

Uses the same `_StubMCP` + monkeypatched `_providers.load_projects` +
fake-provider pattern as `test_board_columns_169_170.py` /
`test_ticket_fields_167_168.py`.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers.base import Relation
from lib_python_projects.providers.github import GitHubError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import relations as relation_tools


def _project(provider: str = "github") -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider=provider,
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register(
    monkeypatch: pytest.MonkeyPatch, provider_instance, project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, project.provider, provider_instance)

    stub = _StubMCP()
    relation_tools.register(stub)
    return stub.tools


def _rel(kind: str, ticket_id: str, **overrides) -> Relation:
    base = dict(
        kind=kind,
        ticket_id=ticket_id,
        title=f"Title {ticket_id}",
        url=f"https://github.com/acme/backend/issues/{ticket_id.lstrip('#')}",
        state="open",
        is_pull_request=False,
        resolved=True,
    )
    base.update(overrides)
    return Relation(**base)


class _MockHierarchyProvider:
    def __init__(
        self,
        relations: list[Relation] | None = None,
        truncated: bool = False,
        error: Exception | None = None,
    ) -> None:
        self._relations = relations or []
        self._truncated = truncated
        self._error = error
        self.captured_kwargs: dict = {}
        self.call_count = 0

    def get_ticket(self, project_, token, ticket_id, *, include_relations=True, include_custom_fields=False):
        self.call_count += 1
        self.captured_kwargs["include_relations"] = include_relations
        self.captured_kwargs["include_custom_fields"] = include_custom_fields
        if self._error is not None:
            raise self._error
        ticket = object()  # list_hierarchy must not touch the ticket itself
        return ticket, [], self._relations, self._truncated


# ---------------------------------------------------------------------------
# Core: mixed relations list
# ---------------------------------------------------------------------------


def test_list_hierarchy_mixed_relations_projects_parent_and_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relations = [
        _rel("parent", "#1"),
        _rel("child", "#2"),
        _rel("child", "#3"),
        _rel("blocks", "#4"),
        _rel("mentions", "#5"),
        _rel("duplicate_of", "#6"),
    ]
    provider = _MockHierarchyProvider(relations=relations)
    tools = _register(monkeypatch, provider, _project())

    out = tools["list_hierarchy"](project_id="acme", ticket_id="42")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["project_id"] == "acme"
    assert out["parent"]["ticket_id"] == "#1"
    assert out["parent"]["kind"] == "parent"
    assert [c["ticket_id"] for c in out["children"]] == ["#2", "#3"]
    assert all(c["kind"] == "child" for c in out["children"])


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_list_hierarchy_no_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    relations = [_rel("child", "#2"), _rel("child", "#3")]
    provider = _MockHierarchyProvider(relations=relations)
    tools = _register(monkeypatch, provider, _project())

    out = tools["list_hierarchy"](project_id="acme", ticket_id="42")

    assert out["parent"] is None
    assert len(out["children"]) == 2


def test_list_hierarchy_no_children_leaf(monkeypatch: pytest.MonkeyPatch) -> None:
    relations = [_rel("parent", "#1")]
    provider = _MockHierarchyProvider(relations=relations)
    tools = _register(monkeypatch, provider, _project())

    out = tools["list_hierarchy"](project_id="acme", ticket_id="42")

    assert out["parent"]["ticket_id"] == "#1"
    assert out["children"] == []


def test_list_hierarchy_empty_relations(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _MockHierarchyProvider(relations=[])
    tools = _register(monkeypatch, provider, _project())

    out = tools["list_hierarchy"](project_id="acme", ticket_id="42")

    assert "error" not in out, f"unexpected error: {out}"
    assert out["parent"] is None
    assert out["children"] == []


# ---------------------------------------------------------------------------
# relations_truncated passthrough
# ---------------------------------------------------------------------------


def test_list_hierarchy_relations_truncated_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockHierarchyProvider(relations=[], truncated=True)
    tools = _register(monkeypatch, provider, _project())

    out = tools["list_hierarchy"](project_id="acme", ticket_id="42")

    assert out["relations_truncated"] is True


# ---------------------------------------------------------------------------
# Forwarding — include_relations=True, single call
# ---------------------------------------------------------------------------


def test_list_hierarchy_forwards_include_relations_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockHierarchyProvider(relations=[])
    tools = _register(monkeypatch, provider, _project())

    tools["list_hierarchy"](project_id="acme", ticket_id="42")

    assert provider.captured_kwargs["include_relations"] is True
    assert provider.captured_kwargs["include_custom_fields"] is False
    assert provider.call_count == 1


# ---------------------------------------------------------------------------
# Error-as-data
# ---------------------------------------------------------------------------


def test_list_hierarchy_404_rewrapped_as_error_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockHierarchyProvider(error=GitHubError(404, "Not Found"))
    tools = _register(monkeypatch, provider, _project())

    out = tools["list_hierarchy"](project_id="acme", ticket_id="999")

    assert "error" in out, f"expected error dict; got: {out}"
    assert "acme" in out["error"]
    assert "999" in out["error"] or "#999" in out["error"]


def test_list_hierarchy_generic_error_surfaces_as_data_not_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _MockHierarchyProvider(error=ValueError("boom"))
    tools = _register(monkeypatch, provider, _project())

    out = tools["list_hierarchy"](project_id="acme", ticket_id="42")

    assert out == {"error": "boom"}
