"""Tests for work package #272 (children #269, #267): `search_projects`
limit validation and two docstring corrections.

Covers:
  - #269: `limit < 1` is rejected up front with a structured
    `{"error": "limit must be a positive integer, got <limit>"}`, instead of
    being silently clamped to 1 by the old `cap = max(1, limit)` line. No
    project load, no scoring happens once the limit is invalid.
  - #267 item 1: the "does project X exist?" existence-check guidance names
    the `match_confidence == "weak"` filter explicitly.
  - #267 item 2: the docstring discloses that `search_projects` id matching
    is case-insensitive, while `project_id` elsewhere (resolved via
    `_resolve`) is exact/case-sensitive.
"""
from __future__ import annotations

import re
from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import projects as project_tools


def _project(
    id_: str = "acme",
    path: str = "acme/backend",
    description: str = "",
) -> ProjectConfig:
    return ProjectConfig(
        id=id_,
        provider="github",
        path=path,
        description=description,
    )


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _make_fake_load(projects, counter: dict | None = None):
    def fake_load_projects(*_args, **_kwargs):
        if counter is not None:
            counter["n"] += 1
        return ProjectsLoadResult(
            projects=projects, state="ok", search_root="/tmp",
        )
    return fake_load_projects


def _register(monkeypatch, projects, counter: dict | None = None) -> dict[str, Callable]:
    monkeypatch.setattr(
        project_tools, "load_projects", _make_fake_load(projects, counter),
    )
    stub = _StubMCP()
    project_tools.register(stub)
    return stub.tools


def _paragraphs(doc: str) -> list[str]:
    return re.split(r"\n\s*\n", doc)


FOUR_PROJECTS = [
    _project(id_="alpha", path="org/alpha"),
    _project(id_="beta", path="org/beta"),
    _project(id_="gamma", path="org/gamma"),
    _project(id_="delta", path="org/delta"),
]


# ===========================================================================
# Behavioural requirement 1 (#269): limit < 1 is rejected before any work
# ===========================================================================


def test_search_projects_rejects_zero_limit(monkeypatch):
    tools = _register(monkeypatch, FOUR_PROJECTS)
    result = tools["search_projects"](query="alpha", limit=0)
    assert result == {"error": "limit must be a positive integer, got 0"}


@pytest.mark.parametrize("limit", [-1, -100])
def test_search_projects_rejects_negative_limit(monkeypatch, limit):
    tools = _register(monkeypatch, FOUR_PROJECTS)
    result = tools["search_projects"](query="alpha", limit=limit)
    assert result == {"error": f"limit must be a positive integer, got {limit}"}


def test_search_projects_rejects_zero_limit_in_light_branch(monkeypatch):
    tools = _register(monkeypatch, FOUR_PROJECTS)
    result = tools["search_projects"](query="alpha", limit=0, fields="light")
    assert result == {"error": "limit must be a positive integer, got 0"}


def test_search_projects_rejects_zero_limit_on_empty_query(monkeypatch):
    tools = _register(monkeypatch, FOUR_PROJECTS)
    result = tools["search_projects"](query="", limit=0)
    assert result == {"error": "limit must be a positive integer, got 0"}


def test_invalid_limit_does_not_load_projects(monkeypatch):
    counter = {"n": 0}
    tools = _register(monkeypatch, FOUR_PROJECTS, counter=counter)
    tools["search_projects"](query="alpha", limit=0)
    assert counter["n"] == 0


def test_invalid_limit_does_not_score(monkeypatch):
    tools = _register(monkeypatch, FOUR_PROJECTS)

    def fail_score(*_args, **_kwargs):
        pytest.fail("_score_match must not be called when limit is invalid")

    monkeypatch.setattr(project_tools, "_score_match", fail_score)
    tools["search_projects"](query="alpha", limit=0)


def test_limit_one_is_the_valid_boundary(monkeypatch):
    """limit=1 is the smallest VALID value and must keep working exactly as
    before -- regression pin, not RED (already passes on unfixed code)."""
    tools = _register(monkeypatch, FOUR_PROJECTS)
    result = tools["search_projects"](query="", limit=1)
    assert "error" not in result
    assert len(result["matches"]) == 1
    assert result["total"] == 4
    assert result["truncated"] is True


def test_valid_limits_unchanged(monkeypatch):
    """limit=2 and the default limit=10 must produce identical match-count /
    total / truncated values before and after the fix, in both `fields`
    modes -- regression pin, not RED (already passes on unfixed code).

    Empty query -> all 4 projects, sorted alphabetically by id, scored 0.
    For limit=2: only 2 of the 4 are returned (total=4 > limit=2 -> True).
    For the default limit=10: all 4 are returned (total=4 > limit=10 ->
    False, nothing is truncated).
    """
    for fields in ("full", "light"):
        tools = _register(monkeypatch, FOUR_PROJECTS)

        result_2 = tools["search_projects"](query="", limit=2, fields=fields)
        assert "error" not in result_2
        assert len(result_2["matches"]) == 2, (fields, result_2)
        assert result_2["total"] == 4
        assert result_2["truncated"] is True

        result_default = tools["search_projects"](query="", limit=10, fields=fields)
        assert "error" not in result_default
        assert len(result_default["matches"]) == 4, (fields, result_default)
        assert result_default["total"] == 4
        assert result_default["truncated"] is False


# ===========================================================================
# Behavioural requirement 2 (#267 item 1): existence-check guidance names
# the weak-confidence filter
# ===========================================================================


def test_docstring_tells_existence_checks_to_filter_weak(monkeypatch):
    tools = _register(monkeypatch, [])
    doc = tools["search_projects"].__doc__ or ""
    paragraphs = _paragraphs(doc)
    target = next((p for p in paragraphs if "does project X exist" in p), None)
    assert target is not None, "existence-check paragraph not found in docstring"
    assert "filter out" in target
    assert 'match_confidence == "weak"' in target


def test_existing_docstring_contract_preserved(monkeypatch):
    """Regression pin: the pre-existing docstring contract (unrelated to
    #267/#269) survives the edit -- already passes on unfixed code."""
    tools = _register(monkeypatch, [])
    doc = tools["search_projects"].__doc__ or ""
    assert "list_projects" in doc
    assert "score" in doc
    assert "incidental" in doc or "sub-token" in doc
    for literal in ('"exact"', '"id"', '"path"', '"description"', '"weak"'):
        assert literal in doc, literal


# ===========================================================================
# Behavioural requirement 3 (#267 item 2): case-sensitivity asymmetry is
# disclosed
# ===========================================================================


def test_docstring_documents_case_sensitivity_asymmetry(monkeypatch):
    tools = _register(monkeypatch, [])
    doc = tools["search_projects"].__doc__ or ""
    assert "case-insensitive" in doc
    assert "case-sensitive" in doc
    assert "project_id" in doc


def test_resolve_remains_case_sensitive(monkeypatch):
    """Regression pin: `_resolve` (used by every other `project_id` tool)
    stays exact/case-sensitive -- already passes on unfixed code."""
    project = _project(id_="Acme", path="acme/backend")
    monkeypatch.setattr(
        providers_mod, "load_projects", _make_fake_load([project]),
    )
    with pytest.raises(LookupError):
        providers_mod._resolve("acme")
    resolved = providers_mod._resolve("Acme")
    assert resolved.id == "Acme"


def test_search_projects_remains_case_insensitive(monkeypatch):
    """Regression pin: `search_projects` id matching stays case-insensitive
    -- already passes on unfixed code."""
    tools = _register(monkeypatch, [_project(id_="Acme", path="acme/backend")])
    result = tools["search_projects"](query="ACME")
    assert result["matches"]
    assert result["matches"][0]["match_confidence"] == "exact"
