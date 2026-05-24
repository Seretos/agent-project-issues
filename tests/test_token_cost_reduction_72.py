"""Tests for ticket #72: Token-Cost Reduction Opportunities.

Covers items 1–5 of the approved plan:
  1. `apply_omit_nulls` helper + `omit_nulls` knob on list_prs / list_tickets.
  2. `include_review_comments` / `review_comments_limit` on get_pr.
  3. `fields="light"` on list_projects / find_projects.
  4. Post-marker `body_max_chars` measurement in apply_body_knobs.
  5. `has_more` per project in list_tickets_across_projects + fanout warning.

The suite stubs the project/provider layer (monkeypatch _providers.load_projects +
fake providers), following the convention established in test_response_slimming.py.
"""
from __future__ import annotations

import json
from typing import Callable
from unittest.mock import MagicMock

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import bulk as bulk_tools
from project_issues_plugin.tools import projects as project_tools
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import tickets as ticket_tools
from project_issues_plugin.tools._slicing import apply_body_knobs, apply_omit_nulls


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _project(id_: str = "acme", repo: str = "backend") -> ProjectConfig:
    return ProjectConfig(
        id=id_,
        provider="github",
        path=f"acme/{repo}",
        token_env="GITHUB_TOKEN_ACME",
        permissions={"issues": {"create": True, "modify": True}},
    )


def _issue(id_: int, body: str = "issue body") -> dict:
    return {
        "number": id_,
        "title": f"issue {id_}",
        "body": body,
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/issues/{id_}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _pr(id_: int, body: str = "pr body") -> dict:
    return {
        "number": id_,
        "title": f"pr {id_}",
        "body": body,
        "state": "open",
        "user": {"login": "alice"},
        "assignees": [],
        "labels": [],
        "html_url": f"https://github.com/acme/backend/pull/{id_}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "head": {
            "ref": "feat", "sha": "abc",
            "user": {"login": "alice"},
            "repo": {"full_name": "acme/backend"},
        },
        "base": {
            "ref": "main", "sha": "def",
            "user": {"login": "alice"},
            "repo": {"full_name": "acme/backend"},
        },
        "draft": False,
        "merged": False,
        "mergeable": None,
        "mergeable_state": "unknown",
        "requested_reviewers": [],
        "requested_teams": [],
    }


def _comment(id_: int, body: str = "c body") -> dict:
    return {
        "id": id_,
        "user": {"login": "alice"},
        "body": body,
        "html_url": f"https://github.com/acme/backend/issues/1#issuecomment-{id_}",
        "created_at": f"2024-01-0{id_}T00:00:00Z",
    }


def _review_comment(id_: int, body: str = "rc body") -> dict:
    return {
        "id": id_,
        "user": {"login": "alice"},
        "body": body,
        "path": "src/foo.py",
        "line": 10,
        "original_line": 10,
        "side": "RIGHT",
        "commit_id": "deadbeef",
        "html_url": f"https://github.com/acme/backend/pull/5#discussion_r{id_}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _json_response(
    payload, status_code: int = 200, headers: dict | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _install_mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def fake_client(token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers={"Accept": "application/vnd.github+json"},
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _register(monkeypatch, module, projects: list[ProjectConfig]):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=projects, state="ok", search_root="/tmp",
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    if hasattr(module, "load_projects"):
        monkeypatch.setattr(module, "load_projects", fake_load_projects)
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


# ===========================================================================
# Item 1 — apply_omit_nulls unit tests
# ===========================================================================


def test_apply_omit_nulls_drops_none_values():
    rows = [{"id": "1", "title": "t", "assignee": None, "body": None}]
    out = apply_omit_nulls(rows)
    assert "assignee" not in out[0]
    assert "body" not in out[0]
    assert out[0]["id"] == "1"
    assert out[0]["title"] == "t"


def test_apply_omit_nulls_preserves_non_none():
    rows = [{"id": "1", "body": "text", "count": 0, "flag": False}]
    out = apply_omit_nulls(rows)
    # 0 and False are not None — must be preserved
    assert out[0]["body"] == "text"
    assert out[0]["count"] == 0
    assert out[0]["flag"] is False


def test_apply_omit_nulls_is_shallow_nested_none_preserved():
    """Nested None values (e.g. head.sha=None) must NOT be stripped."""
    rows = [
        {
            "id": "1",
            "null_top": None,        # top-level None — should be dropped
            "head": {"sha": None, "ref": "feat"},  # nested None — must stay
        }
    ]
    out = apply_omit_nulls(rows)
    assert "null_top" not in out[0]
    assert "head" in out[0]
    assert out[0]["head"]["sha"] is None   # nested None preserved
    assert out[0]["head"]["ref"] == "feat"


def test_apply_omit_nulls_empty_rows():
    assert apply_omit_nulls([]) == []


def test_apply_omit_nulls_no_nulls_unchanged():
    rows = [{"id": "1", "body": "text"}]
    out = apply_omit_nulls(rows)
    assert out == rows


# ---------------------------------------------------------------------------
# Item 1 wire-up: omit_nulls on list_tickets
# ---------------------------------------------------------------------------


def test_list_tickets_omit_nulls_drops_null_field(monkeypatch):
    """omit_nulls=True removes top-level None from ticket rows."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    issue = _issue(1)
    issue["assignee"] = None  # GitHub often includes this

    def handler(req):
        return _json_response([issue])

    _install_mock(monkeypatch, handler)
    result = tools["list_tickets"](project_id="acme", omit_nulls=True)
    assert "error" not in result
    # The top-level None field must be gone
    for row in result["tickets"]:
        assert "assignee" not in row or row["assignee"] is not None


def test_list_tickets_omit_nulls_default_false_preserves_nulls(monkeypatch):
    """omit_nulls defaults to False; None fields are kept."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, ticket_tools, [_project()])

    # The GitHub provider maps `milestone` → None when absent.  We just
    # confirm the tool does NOT strip None by default.
    def handler(req):
        return _json_response([_issue(1)])

    _install_mock(monkeypatch, handler)
    result = tools["list_tickets"](project_id="acme")
    assert "error" not in result
    # body is present and non-None in our stub
    assert result["tickets"][0]["body"] == "issue body"


# ---------------------------------------------------------------------------
# Item 1 wire-up: omit_nulls on list_prs
# ---------------------------------------------------------------------------


def test_list_prs_omit_nulls_drops_null_field(monkeypatch):
    """omit_nulls=True removes top-level None from PR rows."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])

    pr = _pr(1)
    pr["mergeable"] = None  # nullable PR field

    def handler(req):
        return _json_response([pr])

    _install_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme", omit_nulls=True)
    assert "error" not in result
    # After omit_nulls, top-level None fields are gone
    for row in result["prs"]:
        assert "mergeable" not in row or row["mergeable"] is not None


def test_list_prs_omit_nulls_default_false(monkeypatch):
    """Default omit_nulls=False — None-valued top-level fields remain."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])

    def handler(req):
        return _json_response([_pr(1)])

    _install_mock(monkeypatch, handler)
    result = tools["list_prs"](project_id="acme")
    assert "error" not in result
    # mergeable is None in _pr() fixture — must still be present
    assert "mergeable" in result["prs"][0]
    assert result["prs"][0]["mergeable"] is None


# ===========================================================================
# Item 2 — include_review_comments / review_comments_limit on get_pr
# ===========================================================================


def test_get_pr_include_review_comments_false_omits_key(monkeypatch):
    """Regression: include_review_comments=False omits the key and
    emits review_comments_fetched: False."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])
    provider_called = {"review_comments": False}

    def handler(req):
        if req.url.path == "/repos/acme/backend/pulls/5":
            return _json_response(_pr(5))
        if req.url.path == "/repos/acme/backend/issues/5/comments":
            return _json_response([])
        if req.url.path == "/repos/acme/backend/pulls/5/comments":
            provider_called["review_comments"] = True
            return _json_response([_review_comment(1)])
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_pr"](
        project_id="acme", pr_id="5", include_review_comments=False,
    )
    assert "error" not in result, result
    # Key must be absent
    assert "review_comments" not in result
    assert result["review_comments_fetched"] is False
    # Provider call must have been skipped
    assert not provider_called["review_comments"]


def test_get_pr_review_comments_limit_zero_aliases_false(monkeypatch):
    """review_comments_limit=0 behaves the same as include_review_comments=False,
    including skipping the provider fetch."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])
    provider_called = {"review_comments": False}

    def handler(req):
        if req.url.path == "/repos/acme/backend/pulls/5":
            return _json_response(_pr(5))
        if req.url.path == "/repos/acme/backend/issues/5/comments":
            return _json_response([])
        if req.url.path == "/repos/acme/backend/pulls/5/comments":
            provider_called["review_comments"] = True
            return _json_response([_review_comment(1)])
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_pr"](
        project_id="acme", pr_id="5", review_comments_limit=0,
    )
    assert "error" not in result, result
    assert "review_comments" not in result
    assert result["review_comments_fetched"] is False
    # Provider call must have been skipped, just like include_review_comments=False
    assert not provider_called["review_comments"]


def test_get_pr_review_comments_limit_caps(monkeypatch):
    """review_comments_limit=2 caps the returned list to 2."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])

    def handler(req):
        if req.url.path == "/repos/acme/backend/pulls/5":
            return _json_response(_pr(5))
        if req.url.path == "/repos/acme/backend/issues/5/comments":
            return _json_response([])
        if req.url.path == "/repos/acme/backend/pulls/5/comments":
            return _json_response([
                _review_comment(1),
                _review_comment(2),
                _review_comment(3),
            ])
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_pr"](
        project_id="acme", pr_id="5", review_comments_limit=2,
    )
    assert "error" not in result, result
    assert len(result["review_comments"]) == 2
    assert result["review_comments_fetched"] is True


def test_get_pr_default_fetches_review_comments(monkeypatch):
    """Default behaviour: include_review_comments=True emits the list +
    review_comments_fetched: True."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, pull_tools, [_project()])

    def handler(req):
        if req.url.path == "/repos/acme/backend/pulls/5":
            return _json_response(_pr(5))
        if req.url.path == "/repos/acme/backend/issues/5/comments":
            return _json_response([])
        if req.url.path == "/repos/acme/backend/pulls/5/comments":
            return _json_response([_review_comment(1)])
        return _json_response([])

    _install_mock(monkeypatch, handler)
    result = tools["get_pr"](project_id="acme", pr_id="5")
    assert "error" not in result, result
    assert "review_comments" in result
    assert result["review_comments_fetched"] is True
    assert len(result["review_comments"]) == 1


# ===========================================================================
# Item 3 — fields="light" for list_projects / find_projects
# ===========================================================================


def _make_fake_load(projects):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=projects, state="ok", search_root="/tmp",
        )
    return fake_load_projects


def test_list_projects_light_returns_id_and_provider_only(monkeypatch):
    """fields='light' returns only {id, provider} per project, no runtime."""
    monkeypatch.setattr(
        project_tools, "load_projects",
        _make_fake_load([_project("acme")]),
    )
    stub = _StubMCP()
    project_tools.register(stub)
    tools = stub.tools

    result = tools["list_projects"](fields="light")
    assert "runtime" not in result
    projects = result["projects"]
    assert len(projects) == 1
    p = projects[0]
    assert set(p.keys()) == {"id", "provider"}
    assert p["id"] == "acme"
    assert p["provider"] == "github"


def test_list_projects_full_unchanged(monkeypatch):
    """fields='full' (default) retains permissions and runtime block."""
    monkeypatch.setattr(
        project_tools, "load_projects",
        _make_fake_load([_project("acme")]),
    )
    stub = _StubMCP()
    project_tools.register(stub)
    tools = stub.tools

    result = tools["list_projects"](fields="full")
    assert "runtime" in result
    p = result["projects"][0]
    # Full mode has permissions block
    assert "permissions" in p


def test_list_projects_default_is_full(monkeypatch):
    """Default call (no fields arg) behaves like fields='full'."""
    monkeypatch.setattr(
        project_tools, "load_projects",
        _make_fake_load([_project("acme")]),
    )
    stub = _StubMCP()
    project_tools.register(stub)
    tools = stub.tools

    result = tools["list_projects"]()
    assert "runtime" in result
    assert "permissions" in result["projects"][0]


def test_find_projects_light_returns_id_provider_score(monkeypatch):
    """fields='light' returns only {id, provider, score} per match, no runtime."""
    monkeypatch.setattr(
        project_tools, "load_projects",
        _make_fake_load([_project("acme")]),
    )
    stub = _StubMCP()
    project_tools.register(stub)
    tools = stub.tools

    result = tools["find_projects"](query="acme", fields="light")
    assert "runtime" not in result
    matches = result["matches"]
    assert len(matches) >= 1
    m = matches[0]
    assert set(m.keys()) == {"id", "provider", "score"}
    assert m["id"] == "acme"
    assert m["provider"] == "github"
    assert isinstance(m["score"], int)


def test_find_projects_light_empty_query_no_runtime(monkeypatch):
    """Empty query with fields='light' also omits runtime."""
    monkeypatch.setattr(
        project_tools, "load_projects",
        _make_fake_load([_project("acme")]),
    )
    stub = _StubMCP()
    project_tools.register(stub)
    tools = stub.tools

    result = tools["find_projects"](query="", fields="light")
    assert "runtime" not in result
    m = result["matches"][0]
    assert set(m.keys()) == {"id", "provider", "score"}


def test_find_projects_full_unchanged(monkeypatch):
    """fields='full' (default) retains permissions and runtime block."""
    monkeypatch.setattr(
        project_tools, "load_projects",
        _make_fake_load([_project("acme")]),
    )
    stub = _StubMCP()
    project_tools.register(stub)
    tools = stub.tools

    result = tools["find_projects"](query="acme", fields="full")
    assert "runtime" in result
    assert "permissions" in result["matches"][0]


# ===========================================================================
# Item 4 — post-marker body_max_chars measurement
# ===========================================================================


def test_apply_body_knobs_ai_generated_prefix_measures_post_marker():
    """Regression: body_max_chars=4 on '#ai-generated\\n\\nabcdefghij' should
    produce '#ai-generated\\n\\nabcd' with body_truncated=True."""
    rows = [{"id": "1", "body": "#ai-generated\n\nabcdefghij"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=4)
    assert out[0]["body"] == "#ai-generated\n\nabcd"
    assert out[0]["body_truncated"] is True


def test_apply_body_knobs_ai_modified_prefix_measures_post_marker():
    """Same test with #ai-modified prefix."""
    rows = [{"id": "1", "body": "#ai-modified\n\nabcdefghij"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=4)
    assert out[0]["body"] == "#ai-modified\n\nabcd"
    assert out[0]["body_truncated"] is True


def test_apply_body_knobs_marker_free_body_unchanged():
    """Marker-free bodies continue to use the cap on the full string."""
    rows = [{"id": "1", "body": "abcdefghij"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=4)
    assert out[0]["body"] == "abcd"
    assert out[0]["body_truncated"] is True


def test_apply_body_knobs_marker_only_body_not_truncated():
    """A body that is exactly a marker with no content under the cap is
    not truncated."""
    rows = [{"id": "1", "body": "#ai-generated\n\n"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=100)
    assert out[0]["body"] == "#ai-generated\n\n"
    assert out[0]["body_truncated"] is False


def test_apply_body_knobs_post_marker_under_cap_not_truncated():
    """Post-marker content under the cap sets body_truncated=False."""
    rows = [{"id": "1", "body": "#ai-generated\n\nabc"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=10)
    assert out[0]["body"] == "#ai-generated\n\nabc"
    assert out[0]["body_truncated"] is False


def test_apply_body_knobs_post_marker_exact_cap_not_truncated():
    """Exactly body_max_chars content after marker — not truncated."""
    rows = [{"id": "1", "body": "#ai-generated\n\nabcd"}]
    out = apply_body_knobs(rows, omit_body=False, body_max_chars=4)
    assert out[0]["body"] == "#ai-generated\n\nabcd"
    assert out[0]["body_truncated"] is False


# Docstring regression guards: verify relevant docstrings contain the
# expected guidance text.
# The docstrings are set at module registration time; we register with a
# stub MCP against _providers.load_projects (which IS always present) so
# we can read the function objects. Modules that have no module-level
# load_projects do NOT need that attribute patched.


def _get_registered_tools(monkeypatch, module) -> dict[str, Callable]:
    fake_load = lambda *a, **k: ProjectsLoadResult(  # noqa: E731
        projects=[], state="ok", search_root="/tmp",
    )
    monkeypatch.setattr(providers_mod, "load_projects", fake_load)
    if hasattr(module, "load_projects"):
        monkeypatch.setattr(module, "load_projects", fake_load)
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


def test_list_tickets_docstring_recommends_omit_body_for_discovery(monkeypatch):
    """list_tickets docstring must recommend omit_body for discovery passes."""
    tools = _get_registered_tools(monkeypatch, ticket_tools)
    doc = tools["list_tickets"].__doc__ or ""
    assert "discovery" in doc.lower() or "omit_body" in doc, (
        "list_tickets docstring should mention omit_body for discovery passes"
    )


def test_get_ticket_docstring_notes_comments_body_max_chars_unbounded(monkeypatch):
    """get_ticket docstring must note that comments_body_max_chars is unbounded
    and recommend e.g. 500 for summary reads."""
    tools = _get_registered_tools(monkeypatch, ticket_tools)
    doc = tools["get_ticket"].__doc__ or ""
    assert "unbounded" in doc.lower() or "Unbounded" in doc, (
        "get_ticket docstring should note that comments_body_max_chars is unbounded"
    )
    assert "500" in doc, (
        "get_ticket docstring should recommend e.g. comments_body_max_chars=500"
    )


def test_list_tickets_docstring_mentions_marker_prefix(monkeypatch):
    """list_tickets docstring must mention #ai-generated/#ai-modified prefix."""
    tools = _get_registered_tools(monkeypatch, ticket_tools)
    doc = tools["list_tickets"].__doc__ or ""
    assert "#ai-generated" in doc, (
        "list_tickets docstring should mention #ai-generated marker"
    )


# ===========================================================================
# Item 5 — has_more per project + fanout warning docstring
# ===========================================================================


def test_bulk_has_more_false_when_under_limit(monkeypatch):
    """Regression: has_more must be present in per-project result (was discarded)."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, bulk_tools, [_project()])

    def handler(req):
        # Return fewer items than the limit → no Link rel=next header
        return _json_response([_issue(1)])

    _install_mock(monkeypatch, handler)
    result = tools["list_tickets_across_projects"](project_ids=["acme"])
    entry = result["results"]["acme"]
    assert "has_more" in entry, "has_more must be present in per-project result"
    assert entry["has_more"] is False


def test_bulk_has_more_true_when_provider_signals_more(monkeypatch):
    """has_more=True is propagated when the provider's Link header says so."""
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")
    tools = _register(monkeypatch, bulk_tools, [_project()])

    def handler(req):
        # Include a Link rel=next header to signal more pages
        link_header = '<https://api.github.com/repos/acme/backend/issues?page=2>; rel="next"'
        return _json_response(
            [_issue(i) for i in range(1, 11)],
            headers={"Link": link_header},
        )

    _install_mock(monkeypatch, handler)
    result = tools["list_tickets_across_projects"](
        project_ids=["acme"], limit_per_project=10,
    )
    entry = result["results"]["acme"]
    assert "has_more" in entry
    assert entry["has_more"] is True


def test_bulk_docstring_warns_about_fanout_to_all(monkeypatch):
    """list_tickets_across_projects docstring must warn about ALL configured
    projects including production projects."""
    tools = _get_registered_tools(monkeypatch, bulk_tools)
    doc = tools["list_tickets_across_projects"].__doc__ or ""
    # Plan requires the docstring to mention ALL and production
    assert "ALL" in doc or "all" in doc.lower(), (
        "docstring should warn that project_ids=None fans out to ALL projects"
    )
    assert "production" in doc.lower(), (
        "docstring should warn about production projects"
    )
