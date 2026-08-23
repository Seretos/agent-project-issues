"""Tests for ticket #258: `search_projects` structured `match_confidence`.

Every `search_projects` match gains a `match_confidence` field derived from
*which scoring rule fired* (never from the numeric `score` total), so an
agent can branch on match quality without internalizing the raw
`>= 300` / `>= 200` / `< 100` threshold prose.

Covers:
  - `_score_match()` unit-level behaviour for all five confidence levels
    plus the `None`-iff-`score==0` invariant.
  - Wiring into both `fields="full"` and `fields="light"` branches of
    `search_projects`.
  - The docstring documents the new field and its literal values.
  - Sort order (by `score` only) is unaffected by the extra tuple element.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
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


def _make_fake_load(projects):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=projects, state="ok", search_root="/tmp",
        )
    return fake_load_projects


def _register(monkeypatch, projects) -> dict[str, Callable]:
    monkeypatch.setattr(
        project_tools, "load_projects", _make_fake_load(projects),
    )
    stub = _StubMCP()
    project_tools.register(stub)
    return stub.tools


# ===========================================================================
# Unit-level: _score_match()
# ===========================================================================


def test_confidence_is_none_iff_score_is_zero():
    """Invariant: match_confidence is None iff score == 0.

    Covers two genuinely-zero-score cases: an empty query, and a query that
    shares no substring or sub-token overlap with id/path/description at
    all. The positive-score "weak" case (score > 0 with no base rule
    firing) is covered separately by `test_sub_token_only_match_is_weak`
    and `test_no_real_match_query_marks_all_matches_weak`.
    """
    # Empty query -> score 0, confidence None.
    p = _project(id_="agent-project-issues", path="org/agent-project-issues")
    score, conf = project_tools._score_match("", p)
    assert score == 0
    assert conf is None

    # A query that shares no substring or sub-token with id/path/description
    # at all -> genuinely zero match.
    no_match_p = _project(id_="widget-catalog", path="org/widget-catalog", description="")
    score, conf = project_tools._score_match("proj-iss", no_match_p)
    assert score == 0
    assert conf is None


def test_description_only_match_above_300_is_still_description():
    """A description-only match whose accumulated score exceeds 300 must
    still report 'description', not 'id' — confidence is derived from the
    branch that set the base score, not from the numeric total."""
    # "widget" appears many times in the description to accumulate bonus
    # points past the 300 id-substring floor, while never appearing in
    # id or path at all.
    p = _project(
        id_="acme-backend",
        path="acme/backend",
        description=" ".join(["widget"] * 20),
    )
    score, conf = project_tools._score_match("widget", p)
    assert score > 300, score
    assert conf == "description"


@pytest.mark.parametrize(
    "query",
    ["acme", "ACME", "  acme  "],  # exact id match, case/whitespace-insensitive
)
def test_exact_id_match_is_exact_confidence(query):
    p = _project(id_="acme", path="other/path", description="")
    score, conf = project_tools._score_match(query, p)
    assert score > 0
    assert conf == "exact"


@pytest.mark.parametrize(
    "query",
    ["acm", "me-back"],  # id prefix / id mid-substring
)
def test_id_prefix_or_substring_match_is_id_confidence(query):
    p = _project(id_="acme-backend", path="other/path", description="")
    score, conf = project_tools._score_match(query, p)
    assert score > 0
    assert conf == "id"


def test_path_only_match_is_path_confidence():
    p = _project(id_="acme", path="org/some-widget-repo", description="")
    score, conf = project_tools._score_match("widget-repo", p)
    assert score > 0
    assert conf == "path"


def test_description_only_match_is_description_confidence():
    p = _project(id_="acme", path="org/acme", description="a handy widget service")
    score, conf = project_tools._score_match("handy widget", p)
    assert score > 0
    assert conf == "description"


def test_sub_token_only_match_is_weak():
    """Query shares a sub-token with the id via the hyphen-split matcher,
    but is not itself a substring of id/path/description -> weak."""
    p = _project(id_="agent-project-issues", path="org/agent-project-issues", description="")
    score, conf = project_tools._score_match("proj-iss", p)
    assert score > 0
    assert conf == "weak"


def test_no_real_match_query_marks_all_matches_weak(monkeypatch):
    """A query with no id/path/description substring hit, but which still
    accumulates a positive score via incidental sub-token/word-token
    overlap, surfaces those matches labeled 'weak' rather than as a
    higher-confidence level."""
    projects = [
        _project(id_="agent-project-issues", path="org/agent-project-issues", description="misc"),
    ]
    tools = _register(monkeypatch, projects)
    result = tools["search_projects"](query="proj-iss", fields="full")
    assert result["matches"], "expected at least one incidental match"
    for m in result["matches"]:
        assert m["match_confidence"] == "weak"


# ===========================================================================
# Wiring: search_projects (full + light)
# ===========================================================================


def test_light_and_full_agree_on_match_confidence(monkeypatch):
    projects = [_project(id_="acme-backend", path="acme/backend", description="")]
    tools = _register(monkeypatch, projects)

    full_result = tools["search_projects"](query="acme", fields="full")
    light_result = tools["search_projects"](query="acme", fields="light")

    full_conf = full_result["matches"][0]["match_confidence"]
    light_conf = light_result["matches"][0]["match_confidence"]
    assert full_conf == light_conf == "id"

    light_match = light_result["matches"][0]
    assert set(light_match.keys()) == {"id", "provider", "score", "match_confidence"}


def test_empty_query_entries_have_null_match_confidence(monkeypatch):
    projects = [_project(id_="acme", path="acme/backend", description="")]
    tools = _register(monkeypatch, projects)

    for q in ("", "   "):
        for fields in ("full", "light"):
            result = tools["search_projects"](query=q, fields=fields)
            m = result["matches"][0]
            assert m["score"] == 0
            assert m["match_confidence"] is None


def test_sort_order_unchanged_on_score_ties(monkeypatch):
    """Tuple-widening to (score, confidence, project) must not change sort
    semantics on ties — sort key stays score-only, so tie order is stable
    (input order preserved by Python's stable sort).

    Deliberately uses two projects that tie on `score` (260) but land in
    *different* confidence buckets ("path" vs "description"). If sorting
    ever regressed to a `(score, confidence)` tuple key, "description" <
    "path" alphabetically would reorder these two — a plain same-confidence
    tie (as in an earlier version of this test) can't detect that, since a
    tuple-sort regression would only be observable on ties whose secondary
    key actually differs.

    The path project matches "cat dog" as a path substring (score 260); the
    description project matches the same query in its description, padded
    with decoy tokens that each add a small sub-token bonus so its total
    also lands on exactly 260 — see `_score_match`'s scoring rules for how
    the base (200 path / 100 description) plus per-token bonuses accumulate.
    """
    decoys = [f"catword{i}" for i in range(6)] + [f"dogword{i}" for i in range(5)]
    path_project = _project(id_="zzz-project", path="org/cat dog", description="")
    description_project = _project(
        id_="aaa-project", path="other/path", description="cat dog " + " ".join(decoys),
    )
    projects = [path_project, description_project]
    tools = _register(monkeypatch, projects)

    result = tools["search_projects"](query="cat dog", fields="full")
    scores = [m["score"] for m in result["matches"]]
    confidences = [m["match_confidence"] for m in result["matches"]]
    assert scores == [260, 260], scores
    assert confidences == ["path", "description"], confidences

    ids = [m["id"] for m in result["matches"]]
    # Stable sort must preserve the original `result.projects` order on
    # this tie, regardless of the (differing) confidence labels.
    assert ids == ["zzz-project", "aaa-project"]


# ===========================================================================
# Docstring
# ===========================================================================


def test_search_projects_docstring_documents_match_confidence(monkeypatch):
    tools = _register(monkeypatch, [])
    doc = tools["search_projects"].__doc__ or ""
    assert "match_confidence" in doc
    for literal in ('"exact"', '"id"', '"path"', '"description"', '"weak"'):
        assert literal in doc, literal
    assert "probably not a real match" in doc.lower()
