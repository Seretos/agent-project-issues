"""Driving tests for work package #366 (children #361, #362, #363; #356/#374
are covered separately in `tests/test_366_lib_pins.py`): three tool-docstring
gaps in the served MCP tool descriptions.

- #361: `get_ticket`'s `acceptance_criteria` paragraph must name Azure DevOps
  as the only populating provider and tell agents to read AC from `body` on
  GitHub/GitLab.
- #362: `search_projects` must document the recommended way to resolve a
  single project by repo path (there is no single-project lookup tool);
  `list_projects` must state the id case-sensitivity rule and cross-reference
  that recipe rather than duplicate it.
- #363: `list_tickets` must document unknown-label behaviour for `not_labels`
  and `labels`, with GitHub's `not_labels` result marked verified live and
  GitLab/Azure DevOps marked not verified live (inferred from the lib's
  query-construction code only).

Every docstring test reads `__doc__` off the tool callable registered via a
minimal `_StubMCP`, matching `tests/test_284_docstring_front_loading.py` /
`tests/test_272_search_projects_limit_and_docstring.py`'s existing pattern
(FastMCP reads `__doc__` verbatim at decoration time, so this is equivalent
to reading the served description).

Round-1 plan-critique notes this file was written against (see
`.adev/366-1/plan-critic-1/critique-merged.json`) and how each was resolved:

  - misread::F3 / untestable::F1 (blocking-minor / note): the plan's
    Approach has `search_projects` carry the full single-project-resolution
    recipe (the exact call, `limit=5`, `fields="full"`, the exact-`path`
    comparison, "no single-project lookup tool") and has `list_projects`
    only CROSS-REFERENCE that recipe (case rule + a pointer to
    `search_projects`), not duplicate it -- but R2's own "Expected RED
    reason" line ambiguously implied those substrings were required in
    both. Resolved here by testing the two tools against DIFFERENT
    substring sets: `test_search_projects_documents_full_recipe` checks the
    full recipe only on `search_projects`; `test_list_projects_documents_
    case_rule_and_cross_reference` checks only the case rule + a
    `search_projects` pointer on `list_projects`.
  - untestable::F2 (note): the plan's R3 driving test as originally
    described only checked that `GitHub`, `GitLab`, `Azure DevOps` and
    `not verified live` all appear SOMEWHERE in the "Unknown labels"
    paragraph -- which would also pass if GitHub were (wrongly) marked
    unverified, or if only one provider were marked at all. Resolved here
    by splitting the paragraph into its `not_labels=` clause and its bare
    `labels=` clause and checking the verified/not-verified marking is tied
    to the right providers in the right clause (`test_list_tickets_
    documents_unknown_labels`).
  - misread::F1 (blocking-minor): the plan's draft prose opens with an
    unconditional "no provider validates label names, so an unknown label
    never raises" claim covering all three providers, while only GitHub's
    `not_labels` *filtering result* was verified live -- whether any
    provider's *server* actually raises on an unknown label name was never
    checked live for GitLab/Azure DevOps. This file does not assert that
    "never raises" (or any global no-error claim) appears anywhere in the
    docstring -- only the per-provider verified/not-verified marking of the
    FILTERING result, which is what #363 actually asks to be documented.
  - misread::F2 (blocking-minor): the plan's draft prose for #361 names an
    "Acceptance criteria" heading, but this repo's own issue templates
    (`.github/ISSUE_TEMPLATE/{bug,feature,task}.yml`) use the field label
    "Acceptance" (GitHub renders that as an `## Acceptance` body heading),
    not "Acceptance criteria". R1's driving test below does not assert on
    the heading name at all (the plan's own R1 driving-test definition
    already didn't) -- it only checks `Azure DevOps`, `GitHub/GitLab`, and
    "from `body`".

Phase = tests: only RED driving tests + compile-level scaffolding here. No
production code (`tools/tickets.py`, `tools/projects.py`) is touched in this
file.
"""
from __future__ import annotations

import re
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import azuredevops as azuredevops_provider
from lib_python_projects.providers import github as github_provider
from lib_python_projects.providers import gitlab as gitlab_provider
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
from lib_python_projects.providers.base import TicketFilters
from lib_python_projects.providers.github import GitHubProvider
from lib_python_projects.providers.gitlab import GitLabProvider

from project_issues_plugin.tools import projects as project_tools
from project_issues_plugin.tools import tickets as ticket_tools


# ---------- tool registration (mirrors test_284 / test_272's _StubMCP) -------


class _StubMCP:
    """Minimal FastMCP stub that records registered tool callables."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register(module) -> dict[str, Callable]:
    stub = _StubMCP()
    module.register(stub)
    return stub.tools


_ticket_tools = _register(ticket_tools)
_project_tools = _register(project_tools)


def _norm(s: str) -> str:
    """Collapse all whitespace runs (including newlines/indentation) to a
    single space -- matches test_284/test_359's normalisation."""
    return re.sub(r"\s+", " ", s).strip()


def _paragraphs(doc: str) -> list[str]:
    return re.split(r"\n\s*\n", doc)


# ===========================================================================
# R1 (#361) -- get_ticket's acceptance_criteria paragraph names Azure DevOps
# as the only populating provider and points to `body` on GitHub/GitLab
# ===========================================================================


def _ac_paragraph(doc: str) -> str:
    target = next(
        (p for p in _paragraphs(doc) if "acceptance_criteria" in p), None,
    )
    assert target is not None, (
        f"no paragraph mentioning 'acceptance_criteria' found in "
        f"get_ticket's docstring:\n{doc}"
    )
    return target


def test_get_ticket_ac_paragraph_names_body_fallback() -> None:
    """Driving test (R1). RED today: the current paragraph
    (tools/tickets.py:524-529) names Azure DevOps and GitHub/GitLab, but
    only says acceptance_criteria sits "alongside `body`" -- it never tells
    an agent to read AC from `body` on GitHub/GitLab instead. GREEN once
    the paragraph gains that hint (plan #361 Approach)."""
    doc = _ticket_tools["get_ticket"].__doc__ or ""
    paragraph = _ac_paragraph(doc)

    assert "from `body`" in paragraph, (
        f"expected a 'from `body`' hint telling agents where to read AC on "
        f"GitHub/GitLab in the acceptance_criteria paragraph:\n{paragraph}"
    )
    assert "Azure DevOps" in paragraph, (
        f"expected 'Azure DevOps' in the acceptance_criteria paragraph:\n{paragraph}"
    )
    assert "GitHub/GitLab" in paragraph, (
        f"expected 'GitHub/GitLab' in the acceptance_criteria paragraph:\n{paragraph}"
    )


def test_get_ticket_ac_paragraph_stays_within_284_length_cap() -> None:
    """Additional edge-case coverage (already enforced by
    tests/test_284_docstring_front_loading.py::test_docstring_stays_within_
    length_budget and test_named_detail_is_front_loaded / test_pre_cliff_
    caveats_stay_pre_cliff): the added words must be paid for by tightening
    the same paragraph, not by raising the raw length cap or pushing
    `Relation kinds:` / other pre-cliff caveats past the 2000-char cliff.
    Repeated here as a same-file guard; already passing today (the
    paragraph hasn't grown yet)."""
    doc = _ticket_tools["get_ticket"].__doc__ or ""
    assert len(doc) <= 5723, (
        f"get_ticket docstring grew to {len(doc)} chars, exceeding the "
        f"5723-char budget test_284 enforces"
    )


# ===========================================================================
# R2 (#362) -- search_projects carries the full single-project-resolution
# recipe; list_projects carries the case rule + a cross-reference to it
# ===========================================================================


def test_search_projects_documents_full_recipe() -> None:
    """Driving test (R2, search_projects half). RED today: none of these
    substrings exist yet in search_projects's docstring -- there is no
    "Resolving one project" recipe paragraph at all."""
    doc = _project_tools["search_projects"].__doc__ or ""

    assert "no single-project lookup tool" in doc, (
        f"search_projects docstring must say there is no single-project "
        f"lookup tool:\n{doc}"
    )
    assert 'search_projects(query="<owner/repo>", limit=5)' in doc, (
        f"search_projects docstring must name the exact recommended call "
        f"for resolving one project by repo path:\n{doc}"
    )
    assert 'fields="full"' in doc, (
        f'search_projects docstring must say to pass fields="full" for the '
        f"resolution recipe (the `light` shape has no `path`):\n{doc}"
    )
    assert "exact" in doc, (
        f"search_projects docstring must describe an exact `path` "
        f"comparison as part of the recipe:\n{doc}"
    )


def test_list_projects_documents_case_rule_and_cross_reference() -> None:
    """Driving test (R2, list_projects half). RED today: `list_projects`
    never says `project_id` is case-sensitive (only `search_projects`'s
    pre-existing paragraph documents the case-insensitive/-sensitive
    asymmetry), and never cross-references the single-project resolution
    recipe. Deliberately does NOT require the full recipe substrings
    (`limit=5`, exact `path` comparison, "no single-project lookup tool")
    here -- per the plan's Approach, `list_projects` only cross-references
    `search_projects`'s recipe rather than duplicating it (misread::F3 /
    untestable::F1 from the round-1 plan critique)."""
    doc = _project_tools["list_projects"].__doc__ or ""

    assert "case-sensitive" in doc, (
        f"list_projects docstring must state that `project_id` is "
        f"case-sensitive on every tool taking it:\n{doc}"
    )
    idx = doc.index("case-sensitive")
    window = doc[max(0, idx - 200): idx + 200]
    assert "verbatim" in window, (
        f"expected 'pass it verbatim' near the case-sensitivity rule "
        f"(not the unrelated 'sourced from config verbatim' sentence "
        f"about board.manage elsewhere in the docstring):\n{window}"
    )

    assert "resolve a single" in doc, (
        f"list_projects docstring must cross-reference the single-project "
        f"resolution recipe rather than staying silent about it:\n{doc}"
    )
    cross_ref_idx = doc.index("resolve a single")
    cross_ref_window = doc[cross_ref_idx: cross_ref_idx + 200]
    assert "search_projects" in cross_ref_window, (
        f"the 'resolve a single ...' sentence must point at search_projects:"
        f"\n{cross_ref_window}"
    )


# ---------- Additional edge-case coverage: the recipe actually disambiguates -


def _project(id_: str, path: str, description: str = "") -> ProjectConfig:
    return ProjectConfig(id=id_, provider="github", path=path, description=description)


def _make_fake_load(projects):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=projects, state="ok", search_root="/tmp")
    return fake_load_projects


def _register_projects(monkeypatch, projects) -> dict[str, Callable]:
    monkeypatch.setattr(project_tools, "load_projects", _make_fake_load(projects))
    stub = _StubMCP()
    project_tools.register(stub)
    return stub.tools


_PREFIX_COLLIDING_PROJECTS = [
    _project(id_="acme-app", path="acme/app"),
    _project(id_="acme-app-legacy", path="acme/app-legacy"),
]


def test_recipe_disambiguates_prefix_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage for R2 (behavioural, not doc-text):
    `search_projects(query="acme/app", limit=5, fields="full")` returns both
    `acme/app` and `acme/app-legacy` (the fuzzy matcher substring-matches
    per `_score_match`), but exactly one match's `path` equals `acme/app`
    exactly -- proving the recipe's "compare `path` exactly" step actually
    disambiguates. Also checked case-insensitively (`"ACME/App"` still
    matches). May already pass today -- this is existing scoring behaviour,
    not new production code."""
    tools = _register_projects(monkeypatch, _PREFIX_COLLIDING_PROJECTS)

    result = tools["search_projects"](query="acme/app", limit=5, fields="full")
    assert "error" not in result, result
    paths = [m["path"] for m in result["matches"]]
    assert "acme/app" in paths, paths
    assert "acme/app-legacy" in paths, paths
    exact_matches = [m for m in result["matches"] if m["path"] == "acme/app"]
    assert len(exact_matches) == 1, (
        f"expected exactly one exact path match for 'acme/app': {result['matches']}"
    )
    assert exact_matches[0]["id"] == "acme-app"

    result_diff_case = tools["search_projects"](
        query="ACME/App", limit=5, fields="full",
    )
    assert "error" not in result_diff_case, result_diff_case
    diff_case_exact = [
        m for m in result_diff_case["matches"] if m["path"] == "acme/app"
    ]
    assert len(diff_case_exact) == 1, (
        f"case-insensitive query 'ACME/App' should still surface the exact "
        f"'acme/app' path match: {result_diff_case['matches']}"
    )


# ===========================================================================
# R3 (#363) -- list_tickets documents unknown-label behaviour for
# not_labels/labels, GitHub verified live, GitLab/Azure DevOps not verified
# ===========================================================================

_LIST_TICKETS_SECTION_END = "Token-cheap knobs:"
_BARE_LABELS_RE = re.compile(r"(?<!not_)\blabels=")


def _unknown_labels_section(doc: str) -> str:
    norm = _norm(doc)
    assert "Unknown labels" in norm, (
        f"no 'Unknown labels' section found in list_tickets's docstring:\n{norm}"
    )
    start = norm.index("Unknown labels")
    end = norm.find(_LIST_TICKETS_SECTION_END, start)
    if end == -1:
        end = len(norm)
    return norm[start:end]


def test_list_tickets_documents_unknown_labels() -> None:
    """Driving test (R3). RED today: no 'Unknown labels' section exists at
    all in list_tickets's docstring. GREEN once the section exists AND ties
    the verified/not-verified marking to the right providers in the right
    clause (see the module docstring's untestable::F2 note for why this
    doesn't just check the four tokens appear somewhere)."""
    doc = _ticket_tools["list_tickets"].__doc__ or ""
    section = _unknown_labels_section(doc)

    assert "not_labels" in section, (
        f"Unknown labels section must mention not_labels:\n{section}"
    )
    labels_match = _BARE_LABELS_RE.search(section)
    assert labels_match is not None, (
        f"Unknown labels section must have a bare 'labels=...' clause "
        f"distinct from the 'not_labels=...' clause:\n{section}"
    )
    assert "list_labels" in section, (
        f"Unknown labels section must point agents at list_labels to check "
        f"spelling:\n{section}"
    )

    not_labels_idx = section.index("not_labels")
    labels_idx = labels_match.start()
    not_labels_clause = section[not_labels_idx:labels_idx]
    labels_clause = section[labels_idx:]

    # -- not_labels clause: GitHub verified live, GitLab/Azure DevOps not --
    for token in ("GitHub", "GitLab", "Azure DevOps"):
        assert token in not_labels_clause, (
            f"{token} missing from the not_labels clause:\n{not_labels_clause}"
        )
    gh_idx = not_labels_clause.index("GitHub")
    verified_match = re.search(r"verified live", not_labels_clause)
    assert verified_match is not None, (
        f"expected 'verified live' in the not_labels clause:\n{not_labels_clause}"
    )
    assert abs(verified_match.start() - gh_idx) <= 60, (
        f"'verified live' must be tied to GitHub (within 60 chars) in the "
        f"not_labels clause, not floating free of it:\n{not_labels_clause}"
    )
    not_verified_match = re.search(r"not verified live", not_labels_clause)
    assert not_verified_match is not None, (
        f"expected GitLab/Azure DevOps to be marked 'not verified live' in "
        f"the not_labels clause:\n{not_labels_clause}"
    )
    gitlab_idx = not_labels_clause.index("GitLab")
    azure_idx = not_labels_clause.index("Azure DevOps")
    assert gitlab_idx < not_verified_match.start(), (
        f"'not verified live' must come after the GitLab mention it "
        f"qualifies:\n{not_labels_clause}"
    )
    assert azure_idx < not_verified_match.start(), (
        f"'not verified live' must come after the Azure DevOps mention it "
        f"qualifies:\n{not_labels_clause}"
    )

    # -- labels clause: inferred/not verified live on all three providers --
    assert "not verified" in labels_clause or "inferred" in labels_clause, (
        f"labels clause must mark the unknown-label-matches-nothing result "
        f"as inferred/not verified live on all three providers (labels-side "
        f"behaviour was never verified live even on GitHub, per the plan's "
        f"#363 Approach):\n{labels_clause}"
    )


# ---------- Additional edge-case coverage: the lib's query-construction ------
# ---------- forwards an unknown label without validating it, per provider ----


def _ticket_filters_any(**overrides) -> TicketFilters:
    return TicketFilters(status="any", not_labels=["no-such-label"], **overrides)


def test_github_not_labels_sends_dash_label_qualifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage for R3: grounds the "verified live on GitHub /
    inferred for GitLab+Azure DevOps" claim in the pinned lib's actual
    query-construction code (github.py's `_list_via_search`, which forwards
    `-label:<name>` into GitHub's Search API `q=` without ever checking the
    label exists). Proves the client-side request shape only -- nothing
    about live GitHub server semantics."""
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/search/issues":
            captured["request"] = request
            return httpx.Response(200, json={"items": []})
        if request.url.path.endswith("/labels"):
            pytest.fail(f"list_tickets must not hit a labels endpoint: {request.url}")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def fake_client(token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=github_provider.API_BASE,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)
    project = ProjectConfig(id="acme", provider="github", path="acme/backend")

    tickets, _has_more = GitHubProvider().list_tickets(project, "tok", _ticket_filters_any())

    assert tickets == []
    query = captured["request"].url.params["q"]
    assert "-label:no-such-label" in query, query


def test_gitlab_not_labels_sends_not_labels_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage for R3: grounds the GitLab half in the pinned
    lib's `not[labels]` query param (gitlab.py's list_tickets), inferred
    (not verified live) per the plan's #363 Approach."""
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/issues"):
            captured["request"] = request
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/labels"):
            pytest.fail(f"list_tickets must not hit a labels endpoint: {request.url}")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def fake_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url="https://gitlab.example.com/api/v4",
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(gitlab_provider, "_client", fake_client)
    project = ProjectConfig(id="acme", provider="gitlab", path="group/proj")

    tickets, _has_more = GitLabProvider().list_tickets(project, "tok", _ticket_filters_any())

    assert tickets == []
    assert captured["request"].url.params["not[labels]"] == "no-such-label"


def test_azure_not_labels_sends_not_contains_wiql_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional coverage for R3: grounds the Azure DevOps half in the
    pinned lib's WIQL `NOT CONTAINS` clause (azuredevops.py's `_build_wiql`),
    inferred (not verified live) per the plan's #363 Approach. Uses
    `status="any"` so `_build_wiql` skips its state-discovery branch
    entirely -- no extra HTTP calls beyond the single WIQL POST."""
    import json as json_mod

    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/_apis/wit/wiql"):
            captured["body"] = json_mod.loads(request.content)
            return httpx.Response(200, json={"workItems": []})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def fake_client(project, token, *, base_url=None):
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=base_url or "https://dev.azure.com",
        )

    monkeypatch.setattr(azuredevops_provider, "_client", fake_client)
    project = ProjectConfig(id="acme", provider="azuredevops", path="org/project/repo")

    tickets, has_more = AzureDevOpsProvider().list_tickets(project, "tok", _ticket_filters_any())

    assert tickets == []
    assert has_more is False
    wiql = captured["body"]["query"]
    assert "[System.Tags] NOT CONTAINS 'no-such-label'" in wiql, wiql
