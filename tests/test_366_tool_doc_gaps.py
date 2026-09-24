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


# Test-critic round-4 F1: shared negation guard. The round-2/3 checkers below
# tie a claim word ("only", "exact", "case-sensitive", "empty", ...) to the
# OTHER half of a claim (e.g. "only" close to "Azure DevOps") but never check
# that the claim word ITSELF isn't negated -- so a paraphrase that negates a
# DIFFERENT part of the same claim (e.g. "not only populated on Azure
# DevOps", "do not compare path exactly", "case-sensitive only on
# search_projects", "does not return an empty result") still satisfied every
# existing assertion. `_assert_not_negated` centralises the fix: it is used
# right before/around each claim-bearing word to reject a preceding negation
# word wrapping that SPECIFIC word, not just the phrase near it.
_NEGATION_RE = re.compile(
    r"\b(?:never|not|no|isn't|doesn't|do not|don't)\b", re.IGNORECASE,
)


def _assert_not_negated(text: str, claim_idx: int, *, window: int = 30, what: str = "") -> None:
    """Assert the text immediately before `claim_idx` (within `window`
    chars) contains no negation word -- guards the claim-bearing word/phrase
    starting at `claim_idx` against being wrapped by a negation that leaves
    every other proximity/order check satisfied."""
    prefix = text[max(0, claim_idx - window): claim_idx]
    assert not _NEGATION_RE.search(prefix), (
        f"{what or text[claim_idx: claim_idx + 20]!r} must not itself be "
        f"negated by a preceding negation word (never/not/no/isn't/"
        f"doesn't/do not/don't):\n{text[max(0, claim_idx - window): claim_idx + 40]}"
    )


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


def _check_ac_paragraph_documents_body_fallback(paragraph: str) -> None:
    """Shared checker for R1 (test-critic round-2 F1), extracted from the
    driving test so the exact same logic can be exercised against a
    plausible-but-wrong negative case
    (`test_ac_paragraph_check_rejects_separate_from_body_phrasing`) and not
    just against the real served paragraph. Raises AssertionError (via
    plain `assert`) on any violation.

    Requires: (a) 'Azure DevOps' tied to 'only' (sole populating provider),
    (b) 'GitHub/GitLab' tied to 'empty' (structurally empty field), and (c)
    an AFFIRMATIVE instruction -- carrying a 'read'/'consult'/'see' verb --
    to read AC from `body` there. Test-critic round-2 F2: (c) must reject
    phrasing that only notes AC sits apart from `body` ('separate from
    `body`' / 'distinct from `body`' / 'kept ... from `body`') without ever
    instructing an agent to read it there -- the literal substring 'from
    `body`' is necessary but not sufficient."""
    assert "Azure DevOps" in paragraph, (
        f"expected 'Azure DevOps' in the acceptance_criteria paragraph:\n{paragraph}"
    )
    assert "GitHub/GitLab" in paragraph, (
        f"expected 'GitHub/GitLab' in the acceptance_criteria paragraph:\n{paragraph}"
    )

    # Azure DevOps must be named as the SOLE populating provider -- "only"
    # (or equivalent) tied to the Azure DevOps mention, not floating free.
    azure_idx = paragraph.index("Azure DevOps")
    azure_window = paragraph[max(0, azure_idx - 60): azure_idx + 60]
    assert "only" in azure_window, (
        f"'Azure DevOps' must be tied to 'only populates ...' language "
        f"naming it the sole populating provider, not just mentioned in "
        f"passing:\n{azure_window}"
    )
    # Test-critic round-4 F1: "only" itself must not be negated (e.g. "not
    # only populated on Azure DevOps") -- the round-2 proximity check above
    # only confirmed "only" sits near "Azure DevOps", not that it actually
    # names Azure DevOps as the SOLE provider.
    _assert_not_negated(
        azure_window, azure_window.index("only"), what="'only' (Azure DevOps sole-provider claim)",
    )

    # GitHub/GitLab must be described as structurally empty, tied to the
    # GitHub/GitLab mention itself.
    ghgl_idx = paragraph.index("GitHub/GitLab")
    ghgl_window = paragraph[max(0, ghgl_idx - 60): ghgl_idx + 80]
    assert "empty" in ghgl_window, (
        f"'GitHub/GitLab' must be tied to 'empty' language describing the "
        f"structurally-empty field, not just mentioned in passing:"
        f"\n{ghgl_window}"
    )
    # Test-critic round-4 F1: "empty" itself must not be negated (e.g. "it
    # is never empty on GitHub/GitLab").
    _assert_not_negated(
        ghgl_window, ghgl_window.index("empty"), what="'empty' (GitHub/GitLab structurally-empty claim)",
    )

    # The 'from `body`' hint must be an affirmative instruction (not negated
    # by a preceding 'never'/'not'/'don't'/'avoid') and must sit close to
    # the GitHub/GitLab mention it applies to.
    body_match = re.search(r"from `body`", paragraph)
    assert body_match is not None, (
        f"expected a 'from `body`' hint telling agents where to read AC on "
        f"GitHub/GitLab in the acceptance_criteria paragraph:\n{paragraph}"
    )
    preceding = paragraph[max(0, body_match.start() - 20): body_match.start()].lower()
    assert not re.search(r"\b(never|not|don't|do not|avoid)\b", preceding), (
        f"the 'from `body`' hint must be an affirmative instruction, not "
        f"negated by a preceding 'never'/'not'/'don't'/'avoid':\n{paragraph}"
    )
    assert abs(body_match.start() - ghgl_idx) <= 150, (
        f"the 'from `body`' hint must be tied to the GitHub/GitLab mention "
        f"(within ~150 chars), not floating free of it in the paragraph:"
        f"\n{paragraph}"
    )

    # Test-critic round-2 F2 fix: "from `body`" must carry an actual
    # instruction verb near it, and phrasing that only notes AC's distance
    # from `body` ("separate"/"distinct"/"kept ... from `body`") must be
    # rejected even though it contains the literal substring.
    instruction_window = paragraph[
        max(0, body_match.start() - 40): body_match.start() + 40
    ]
    assert re.search(r"\b(read|consult|see)\b", instruction_window, re.IGNORECASE), (
        f"the 'from `body`' hint must carry an actual instruction verb "
        f"('read'/'consult'/'see') near it, not just note AC's relationship "
        f"to `body`:\n{instruction_window}"
    )
    apart_from_body = re.search(
        r"\b(separate|distinct|apart)\b[^.]{0,30}from `body`"
        r"|\bkept\b[^.]{0,30}from `body`",
        paragraph,
        re.IGNORECASE,
    )
    assert apart_from_body is None, (
        f"'separate from `body`' / 'distinct from `body`' / 'kept ... from "
        f"`body`' phrasing notes distance, not an instruction to read AC "
        f"from `body`, and must be rejected:\n{paragraph}"
    )


def test_get_ticket_ac_paragraph_names_body_fallback() -> None:
    """Driving test (R1). RED today: the current paragraph
    (tools/tickets.py:524-529) names Azure DevOps and GitHub/GitLab, but
    only says acceptance_criteria sits "alongside `body`" -- it never tells
    an agent to read AC from `body` on GitHub/GitLab instead. GREEN once
    the paragraph gains that hint (plan #361 Approach).

    Tightened per test-critic round-1 F1 and round-2 F1/F2: the checking
    logic now lives in `_check_ac_paragraph_documents_body_fallback` so it
    can also be exercised against a plausible-but-wrong negative case
    (`test_ac_paragraph_check_rejects_separate_from_body_phrasing`), proving
    it discriminates correct phrasing from wrong phrasing rather than just
    checking token presence/position."""
    doc = _ticket_tools["get_ticket"].__doc__ or ""
    paragraph = _ac_paragraph(doc)
    _check_ac_paragraph_documents_body_fallback(paragraph)


def test_ac_paragraph_check_rejects_separate_from_body_phrasing() -> None:
    """Negative case for test-critic round-2 F1/F2: proves
    `_check_ac_paragraph_documents_body_fallback` actually discriminates
    correct from plausible-wrong phrasing, addressing the round-2 critique's
    concern that "any docstring containing the checked tokens in the
    checked positions, whatever it actually tells the reader" would satisfy
    a token/position-only check. Uses the exact plausible-wrong example
    quoted in the round-2 critique JSON (tautology::F2's `what` field): a
    paragraph naming Azure DevOps as sole populator ('only'), GitHub/GitLab
    as structurally 'empty', and containing the literal substring 'from
    `body`' -- so it would have satisfied every ROUND-1 assertion -- but
    which never instructs an agent to read AC from `body`; it only says AC
    is "kept separate from `body`". The tightened checker must still reject
    it."""
    plausible_wrong = (
        "populated only on Azure DevOps ..., kept separate from `body`; "
        "structurally empty on GitHub/GitLab"
    )
    with pytest.raises(AssertionError):
        _check_ac_paragraph_documents_body_fallback(plausible_wrong)


def test_ac_paragraph_check_rejects_negated_only_and_empty() -> None:
    """Negative case for test-critic round-4 F1: proves
    `_check_ac_paragraph_documents_body_fallback` rejects a paraphrase that
    negates the "only"/"empty" claim words THEMSELVES, using exactly the
    plausible-wrong example quoted in the round-4 critique JSON
    (tautology::F1's `surviving_implementation` field): "not only populated
    on Azure DevOps" (negating "only") and "never empty on GitHub/GitLab"
    (negating "empty") both sit within the round-2/3 proximity windows the
    pre-round-4 checker already guarded, so without a negation guard tied to
    the claim word itself this would have passed. This test validates the
    checker's discrimination, not the requirement directly -- the checker
    still needs the real docstring's GREEN run to ground the requirement."""
    plausible_wrong = (
        "acceptance_criteria is not only populated on Azure DevOps "
        "(Microsoft.VSTS.Common.AcceptanceCriteria); it is never empty on "
        "GitHub/GitLab, so read it from `body` there."
    )
    with pytest.raises(AssertionError):
        _check_ac_paragraph_documents_body_fallback(plausible_wrong)


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


def test_acceptance_criteria_is_empty_on_github_and_gitlab_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioural grounding for R1 (test-critic round-3 F1). Every other
    test in this R1 block checks literal DOCSTRING TEXT; none of them prove
    the underlying claim -- that `acceptance_criteria` really is "" on
    GitHub/GitLab -- is actually true at runtime. This test closes that gap
    by exercising the REAL `GitHubProvider.get_ticket` / `GitLabProvider.
    get_ticket` (not a hand-rolled fake provider) against a minimal mocked
    HTTP transport (same `httpx.MockTransport` pattern as the R3 not_labels
    tests further down) and asserting the returned `Ticket.
    acceptance_criteria` is "" for both -- grounded in the pinned lib's own
    code: `base.py`'s `Ticket.acceptance_criteria` field defaults to "", and
    neither `github.py`'s nor `gitlab.py`'s `_map_issue` ever sets it (only
    `azuredevops.py`'s `get_ticket` does, from
    `Microsoft.VSTS.Common.AcceptanceCriteria`).

    Related existing coverage: `tests/test_ticket_fields_167_168.py::
    test_get_ticket_response_acceptance_criteria_defaults_empty` already
    pins the GitHub-empty-default behaviour, but through the MCP tool layer
    with a hand-rolled `_MockGetTicketProvider` stand-in (not the real
    `GitHubProvider`), and it does not cover GitLab at all -- so on its own
    it does not ground the "GitHub/GitLab" half of the docstring's claim in
    the real provider code. This test does that directly, for both
    providers."""

    def _github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if "/issues/" in request.url.path:
            return httpx.Response(
                200, json={"number": 42, "title": "t", "state": "open"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def _fake_github_client(token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url=github_provider.API_BASE,
            transport=httpx.MockTransport(_github_handler),
        )

    monkeypatch.setattr(github_provider, "_client", _fake_github_client)
    gh_project = ProjectConfig(id="acme", provider="github", path="acme/backend")
    gh_ticket, _gh_comments, _gh_rel, _gh_trunc = GitHubProvider().get_ticket(
        gh_project, "tok", "42", include_relations=False,
    )
    assert gh_ticket.acceptance_criteria == "", gh_ticket

    def _gitlab_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/notes"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/issues/42"):
            return httpx.Response(
                200, json={"iid": 42, "title": "t", "state": "opened"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def _fake_gitlab_client(project: ProjectConfig, token: str | None) -> httpx.Client:
        return httpx.Client(
            base_url="https://gitlab.example.com/api/v4",
            transport=httpx.MockTransport(_gitlab_handler),
        )

    monkeypatch.setattr(gitlab_provider, "_client", _fake_gitlab_client)
    gl_project = ProjectConfig(id="acme", provider="gitlab", path="group/proj")
    gl_ticket, _gl_comments, _gl_rel, _gl_trunc = GitLabProvider().get_ticket(
        gl_project, "tok", "42", include_relations=False,
    )
    assert gl_ticket.acceptance_criteria == "", gl_ticket


# ===========================================================================
# R2 (#362) -- search_projects carries the full single-project-resolution
# recipe; list_projects carries the case rule + a cross-reference to it
# ===========================================================================


def _recipe_paragraph(doc: str) -> str:
    target = next(
        (p for p in _paragraphs(doc) if "no single-project lookup tool" in p), None,
    )
    assert target is not None, (
        f"no 'Resolving one project by repo path' recipe paragraph found in "
        f"search_projects's docstring:\n{doc}"
    )
    return target


def _check_recipe_paragraph(paragraph: str) -> None:
    """Shared checker for R2's search_projects half (test-critic round-2
    F1), extracted from the driving test so the exact same logic can be
    exercised against a plausible-but-wrong negative case
    (`test_recipe_paragraph_check_rejects_untied_exact`). Raises
    AssertionError on any violation.

    Tightened per test-critic round-1 F2/F6: the original version checked
    each substring ('no single-project lookup tool', the exact call text,
    'fields=\"full\"', 'exact') anywhere in the WHOLE docstring, disconnected
    from each other -- search_projects' pre-existing text already contains
    'exact' (several times, re: match_confidence/case-sensitivity, unrelated
    to this recipe) and 'fields=\"full\"' (in the pre-existing Token-cheap
    knob bullet), so those two assertions were pre-satisfied and the 'exact'
    check proved nothing about a `path` comparison. Now every assertion is
    scoped to the single new recipe paragraph, the steps must appear in
    order, and 'exact' must sit within ~40 chars of a `path` mention inside
    that paragraph specifically."""
    no_lookup_idx = paragraph.index("no single-project lookup tool")

    call_idx = paragraph.find('search_projects(query="<owner/repo>", limit=5)')
    assert call_idx != -1, (
        f"recipe paragraph must name the exact recommended call for "
        f"resolving one project by repo path:\n{paragraph}"
    )
    assert no_lookup_idx < call_idx, (
        f"'no single-project lookup tool' framing must come before the "
        f"recommended call in the recipe paragraph:\n{paragraph}"
    )

    fields_idx = paragraph.find('fields="full"')
    assert fields_idx != -1, (
        f'recipe paragraph must say to pass fields="full" for the '
        f"resolution recipe (the `light` shape has no `path`):\n{paragraph}"
    )
    assert fields_idx > call_idx, (
        f'fields="full" must follow the recommended call in the recipe '
        f"paragraph:\n{paragraph}"
    )

    # "exact" must be tied to a `path` comparison inside the recipe
    # paragraph, not merely present anywhere in the docstring.
    tail = paragraph[fields_idx:]
    path_matches = list(re.finditer(r"\bpath\b", tail))
    assert path_matches, (
        f"recipe paragraph must mention comparing the match's `path` after "
        f'fields="full":\n{paragraph}'
    )
    exact_matches = list(re.finditer(r"\bexact\w*\b", tail))
    assert exact_matches, (
        f"recipe paragraph must describe an EXACT path comparison:\n{paragraph}"
    )
    closest_em, closest_pm = min(
        ((em, pm) for em in exact_matches for pm in path_matches),
        key=lambda pair: abs(pair[0].start() - pair[1].start()),
    )
    closest_gap = abs(closest_em.start() - closest_pm.start())
    assert closest_gap <= 40, (
        f"'exact' must sit close to the `path` comparison it qualifies "
        f"(within ~40 chars), not float free in the recipe paragraph:"
        f"\n{paragraph}"
    )
    # Test-critic round-4 F1: "exact" itself must not be negated (e.g. "but
    # do not compare path exactly") -- the proximity check above only
    # confirmed "exact" sits near "path", not that it actually claims an
    # exact comparison.
    _assert_not_negated(
        tail, closest_em.start(), what="'exact' (path comparison claim)",
    )


def test_search_projects_documents_full_recipe() -> None:
    """Driving test (R2, search_projects half). RED today: none of these
    substrings exist yet in search_projects's docstring -- there is no
    "Resolving one project" recipe paragraph at all.

    Checking logic lives in `_check_recipe_paragraph` (test-critic round-2
    F1) so it can also be exercised against a plausible-but-wrong negative
    case (`test_recipe_paragraph_check_rejects_untied_exact`)."""
    doc = _project_tools["search_projects"].__doc__ or ""
    paragraph = _recipe_paragraph(doc)
    _check_recipe_paragraph(paragraph)


def test_recipe_paragraph_check_rejects_untied_exact() -> None:
    """Negative case for test-critic round-2 F1: proves
    `_check_recipe_paragraph` discriminates a paragraph that contains every
    required token, in the required order, from one where "exact" never
    actually qualifies the `path` comparison -- it qualifies matching the
    query text instead, with `path` mentioned only in a distant, unrelated
    aside about display. A naive presence/order-only check would pass this;
    the proximity check must still reject it."""
    plausible_wrong = (
        "there is no single-project lookup tool; call "
        'search_projects(query="<owner/repo>", limit=5) with fields="full" '
        "for richer output, then match the query text exactly against what "
        "you sent -- the fuzzy matcher can otherwise return more than one "
        "candidate. the returned path field is only shown for display "
        "purposes, not for comparison; pass that match's id verbatim."
    )
    with pytest.raises(AssertionError):
        _check_recipe_paragraph(plausible_wrong)


def test_recipe_paragraph_check_rejects_negated_exact() -> None:
    """Negative case for test-critic round-4 F1: proves `_check_recipe_
    paragraph` rejects a paraphrase that negates "exact" itself, using
    exactly the plausible-wrong example quoted in the round-4 critique JSON
    (tautology::F1's `surviving_implementation` field): "but do not compare
    path exactly" still puts "exact" within ~40 chars of "path" (the round-2
    proximity window), so without a negation guard tied to "exact" itself
    this would have passed. This test validates the checker's
    discrimination, not the requirement directly -- the checker still needs
    the real docstring's GREEN run to ground the requirement."""
    plausible_wrong = (
        "there is no single-project lookup tool; call "
        'search_projects(query="<owner/repo>", limit=5) with fields="full", '
        "but do not compare path exactly -- just pick the first match "
        "returned; pass that match's id verbatim."
    )
    with pytest.raises(AssertionError):
        _check_recipe_paragraph(plausible_wrong)


def _check_case_rule_and_cross_reference(doc: str) -> None:
    """Shared checker for R2's list_projects half (test-critic round-2 F1),
    extracted from the driving test so the exact same logic can be
    exercised against a plausible-but-wrong negative case
    (`test_case_rule_check_rejects_recipe_pointing_elsewhere`). Raises
    AssertionError on any violation.

    `project_id` must be documented as case-sensitive (not negated) with
    'pass it verbatim' nearby, and the docstring must cross-reference the
    single-project resolution recipe by name (pointing at search_projects,
    not just any 'resolve a single ...' sentence). Deliberately does NOT
    require the full recipe substrings (`limit=5`, exact `path` comparison,
    "no single-project lookup tool") here -- per the plan's Approach,
    `list_projects` only cross-references `search_projects`'s recipe rather
    than duplicating it (misread::F3 / untestable::F1 from the round-1 plan
    critique).

    Behavioural grounding for test-critic round-3 F2: this checker (like
    the rest of R2) only inspects docstring TEXT -- it does not itself prove
    that `project_id` really is case-sensitive on tools other than
    `search_projects`. That is already proven behaviourally by
    `tests/test_272_search_projects_limit_and_docstring.py::
    test_resolve_remains_case_sensitive`, which calls the real `_resolve`
    helper (used by every `project_id`-taking write/read tool via
    `tools/_providers.py`) directly: `_resolve("acme")` raises `LookupError`
    while `_resolve("Acme")` succeeds against a project declared as
    `id="Acme"` -- i.e. the wrong-case id is rejected and the correct-case
    id is accepted, which is exactly the claim this docstring text makes.
    Not duplicated here to avoid re-testing the same lib behaviour twice."""
    assert "case-sensitive" in doc, (
        f"list_projects docstring must state that `project_id` is "
        f"case-sensitive on every tool taking it:\n{doc}"
    )
    idx = doc.index("case-sensitive")
    # Guard against a negated claim ("... is NOT case-sensitive ...")
    # satisfying the substring check (test-critic F2): the words
    # immediately before "case-sensitive" must not negate it. Test-critic
    # round-4 F1: widened from a literal "not " check to the full negation
    # word set (never/not/no/isn't/doesn't/do not/don't).
    _assert_not_negated(doc, idx, what="'case-sensitive'")

    # Test-critic round-4 F1: the rule must apply universally, not be
    # restricted to a single OTHER tool (e.g. "case-sensitive only on
    # search_projects" reverses which tool the rule applies to while still
    # containing every previously-checked token/position).
    scope_window = doc[max(0, idx - 40): idx + 100]
    assert "every tool" in scope_window, (
        f"the case-sensitivity rule must state it applies on EVERY tool "
        f"taking `project_id`, not restrict itself to a single other named "
        f"tool (e.g. 'case-sensitive only on search_projects' reverses "
        f"which tool the rule applies to):\n{scope_window}"
    )

    window = doc[max(0, idx - 200): idx + 200]
    assert "verbatim" in window, (
        f"expected 'pass it verbatim' near the case-sensitivity rule "
        f"(not the unrelated 'sourced from config verbatim' sentence "
        f"about board.manage elsewhere in the docstring):\n{window}"
    )
    # Test-critic round-4 F1: "verbatim" itself must not be negated (e.g.
    # "no need to pass it verbatim").
    _assert_not_negated(
        window, window.index("verbatim"), what="'verbatim' (pass-it-verbatim instruction)",
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


def test_list_projects_documents_case_rule_and_cross_reference() -> None:
    """Driving test (R2, list_projects half). RED today: `list_projects`
    never says `project_id` is case-sensitive (only `search_projects`'s
    pre-existing paragraph documents the case-insensitive/-sensitive
    asymmetry), and never cross-references the single-project resolution
    recipe.

    Checking logic lives in `_check_case_rule_and_cross_reference`
    (test-critic round-2 F1) so it can also be exercised against a
    plausible-but-wrong negative case
    (`test_case_rule_check_rejects_recipe_pointing_elsewhere`)."""
    doc = _project_tools["list_projects"].__doc__ or ""
    _check_case_rule_and_cross_reference(doc)


def test_case_rule_check_rejects_recipe_pointing_elsewhere() -> None:
    """Negative case for test-critic round-2 F1: proves
    `_check_case_rule_and_cross_reference` discriminates a docstring that
    states the case-sensitivity rule correctly but cross-references the
    resolution recipe to somewhere OTHER than search_projects -- a plausible
    documentation mistake (pointing agents at the config file directly,
    which duplicates rather than reuses the recipe) that contains every
    required token except the one that matters."""
    plausible_wrong = (
        "`project_id` is case-sensitive on every tool that takes it; pass "
        "it verbatim. To resolve a single project by repo path, open the "
        "project's configuration file directly and read its `path` field."
    )
    with pytest.raises(AssertionError):
        _check_case_rule_and_cross_reference(plausible_wrong)


def test_case_rule_check_rejects_scope_reversed_to_other_tool() -> None:
    """Negative case for test-critic round-4 F1: proves
    `_check_case_rule_and_cross_reference` rejects a paraphrase that
    reverses WHICH TOOL the case-sensitivity rule applies to, using exactly
    the plausible-wrong example quoted in the round-4 critique JSON
    (tautology::F1's `surviving_implementation` field): "case-sensitive
    only on search_projects" restricts the rule to a different tool instead
    of stating it applies on every tool, and "no need to pass it verbatim"
    negates the verbatim instruction -- both still satisfy every
    pre-round-4 assertion (the literal tokens are present, unnegated by a
    literal 'not ' immediately before "case-sensitive"). This test
    validates the checker's discrimination, not the requirement directly --
    the checker still needs the real docstring's GREEN run to ground the
    requirement."""
    plausible_wrong = (
        "`id` is case-sensitive only on search_projects; other tools "
        "accept any case, no need to pass it verbatim."
    )
    with pytest.raises(AssertionError):
        _check_case_rule_and_cross_reference(plausible_wrong)


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
    # Test-critic round-2 F3: the exact-match project's id is deliberately
    # mixed-case so an implementation that lowercases (or uppercases) ids
    # cannot pass the "pass that match's id verbatim" check below just
    # because the fixture id happened to already be lowercase.
    _project(id_="Acme-App", path="acme/app"),
    _project(id_="acme-app-legacy", path="acme/app-legacy"),
    # Test-critic round-3 F6: a third project whose path shares nothing
    # with the "acme/app" query. With only the two acme/app* projects above
    # and limit=5, a search_projects that ignored the query entirely and
    # returned every configured project would still satisfy every assertion
    # below (both acme/app* paths would be "in paths" and the exact-match
    # count would still be 1). This project's presence in `result["matches"]`
    # would prove exactly that failure mode, so its absence is asserted
    # below.
    _project(id_="unrelated-widget", path="widgets/gizmo"),
]


def test_recipe_disambiguates_prefix_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage for R2 (behavioural, not doc-text):
    `search_projects(query="acme/app", limit=5, fields="full")` returns both
    `acme/app` and `acme/app-legacy` (the fuzzy matcher substring-matches
    per `_score_match`), but exactly one match's `path` equals `acme/app`
    exactly -- proving the recipe's "compare `path` exactly" step actually
    disambiguates. Also checked case-insensitively (`"ACME/App"` still
    matches). May already pass today -- this is existing scoring behaviour,
    not new production code.

    Test-critic round-2 F3: the exact match's id ("Acme-App") is
    mixed-case, and the assertion below pins it down exactly -- an
    implementation that lowercases or uppercases ids would now fail
    here, whereas the original all-lowercase fixture id could not tell the
    difference. (Test-critic round-3 F5: dropped the two `!=` assertions
    that used to follow the `== "Acme-App"` check -- once a string is
    asserted equal to "Acme-App", asserting it `!= "acme-app"` / `!=
    "ACME-APP"` can never come out false, so they were redundant with the
    equality check, not independent coverage.)

    Test-critic round-3 F6: `unrelated-widget` (path `widgets/gizmo`) is
    asserted absent from the result, closing the gap where a
    search_projects that ignored the query and limit and just returned
    every configured project would otherwise satisfy every other
    assertion here."""
    tools = _register_projects(monkeypatch, _PREFIX_COLLIDING_PROJECTS)

    result = tools["search_projects"](query="acme/app", limit=5, fields="full")
    assert "error" not in result, result
    paths = [m["path"] for m in result["matches"]]
    assert "acme/app" in paths, paths
    assert "acme/app-legacy" in paths, paths
    assert "widgets/gizmo" not in paths, (
        f"search_projects must not return a project unrelated to the "
        f"query 'acme/app': {result['matches']}"
    )
    exact_matches = [m for m in result["matches"] if m["path"] == "acme/app"]
    assert len(exact_matches) == 1, (
        f"expected exactly one exact path match for 'acme/app': {result['matches']}"
    )
    assert exact_matches[0]["id"] == "Acme-App", (
        f"expected the exact match's id to preserve its declared mixed-case "
        f"casing verbatim: {exact_matches[0]}"
    )

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
    assert diff_case_exact[0]["id"] == "Acme-App", (
        f"the exact match's id must still preserve its declared mixed-case "
        f"casing verbatim under a case-insensitive query: {diff_case_exact[0]}"
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


def _check_unknown_labels_section(section: str) -> None:
    """Shared checker for R3 (test-critic round-2 F1), extracted from the
    driving test so the exact same logic can be exercised against a
    plausible-but-wrong negative case
    (`test_unknown_labels_check_rejects_swapped_verification_marking`).
    Raises AssertionError on any violation.

    Tightened per test-critic round-1 F3/F5:
      - F5: `re.search(r"verified live", ...)` also matches inside "not
        verified live" -- a clause marking GitHub itself "not verified
        live" would satisfy the old "within 60 chars of GitHub" check. The
        'verified live' search below now uses a negative lookbehind so it
        can only match a BARE 'verified live', never the tail of 'not
        verified live'.
      - F3: the old assertions only checked token order/proximity, never
        the actual claimed BEHAVIOUR -- a docstring saying "not_labels with
        an unknown label raises an error" (instead of "excludes nothing")
        would have passed every assertion. Now the not_labels clause must
        state the excludes-nothing/unfiltered behaviour and must NOT claim
        the call raises/errors; the labels clause must state its
        matches-nothing/empty-result claim explicitly."""
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
    # F5 fix: negative lookbehind so this can only match a BARE "verified
    # live", never the tail of "not verified live" (which the old bare
    # `r"verified live"` pattern would happily match).
    verified_match = re.search(r"(?<!not )verified live", not_labels_clause)
    assert verified_match is not None, (
        f"expected a bare 'verified live' (distinct from 'not verified "
        f"live') in the not_labels clause:\n{not_labels_clause}"
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

    # F3 fix: tie the actual claimed BEHAVIOUR (not just the verification
    # marker) to the not_labels clause -- a clause saying "raises an error"
    # instead of "excludes nothing" must not pass.
    assert "excludes nothing" in not_labels_clause or "unfiltered" in not_labels_clause, (
        f"not_labels clause must state the behavioural claim itself -- an "
        f"unknown label excludes nothing / leaves the list unfiltered -- "
        f"not just mark providers verified/not-verified without saying "
        f"what the actual result is:\n{not_labels_clause}"
    )
    assert not re.search(r"\b(raises?|error)\b", not_labels_clause, re.IGNORECASE), (
        f"not_labels clause must not claim an unknown label raises/errors "
        f"-- the documented behaviour is silent no-op filtering:"
        f"\n{not_labels_clause}"
    )

    # -- labels clause: inferred/not verified live on all three providers,
    # AND the actual matches-nothing/empty-result claim (F3) --
    assert "not verified" in labels_clause or "inferred" in labels_clause, (
        f"labels clause must mark the unknown-label-matches-nothing result "
        f"as inferred/not verified live on all three providers (labels-side "
        f"behaviour was never verified live even on GitHub, per the plan's "
        f"#363 Approach):\n{labels_clause}"
    )
    claim_token = next(
        (t for t in ("matches no ticket", "empty") if t in labels_clause), None,
    )
    assert claim_token is not None, (
        f"labels clause must state the actual behavioural claim -- an "
        f"unknown label matches no ticket / returns an empty result -- not "
        f"just the inferred/not-verified marker on its own:\n{labels_clause}"
    )
    # Test-critic round-4 F1: the claim word/phrase itself must not be
    # negated (e.g. "does not return an empty result") -- the presence
    # check above only confirmed the token appears somewhere in the clause.
    _assert_not_negated(
        labels_clause,
        labels_clause.index(claim_token),
        what=f"{claim_token!r} (labels empty-result claim)",
    )

    # Behavioural grounding for test-critic round-3 F3: everything above is
    # still a check of DOCSTRING TEXT -- it does not itself prove the
    # "verified live" GitHub claim is true. That runtime-truth backing is
    # `test_github_not_labels_sends_dash_label_qualifier` below in this same
    # file: it drives the pinned lib's real `GitHubProvider.list_tickets`
    # with `not_labels=["no-such-label"]` against a mocked GitHub Search API
    # transport and asserts the outgoing request actually carries
    # `-label:no-such-label` in `q=` -- i.e. the client-side request shape
    # the "excludes nothing / unfiltered" claim rests on is real, not just
    # documented. (`test_gitlab_not_labels_sends_not_labels_param` /
    # `test_azure_not_labels_sends_not_contains_wiql_clause` are the same
    # kind of grounding for the GitLab/Azure DevOps halves, which the
    # docstring marks "not verified live" -- those two tests prove the
    # lib's *query construction*, not live server semantics, matching that
    # weaker marking.)


def test_list_tickets_documents_unknown_labels() -> None:
    """Driving test (R3). RED today: no 'Unknown labels' section exists at
    all in list_tickets's docstring. GREEN once the section exists AND ties
    the verified/not-verified marking to the right providers in the right
    clause (see the module docstring's untestable::F2 note for why this
    doesn't just check the four tokens appear somewhere).

    Checking logic lives in `_check_unknown_labels_section` (test-critic
    round-2 F1) so it can also be exercised against a plausible-but-wrong
    negative case
    (`test_unknown_labels_check_rejects_swapped_verification_marking`)."""
    doc = _ticket_tools["list_tickets"].__doc__ or ""
    section = _unknown_labels_section(doc)
    _check_unknown_labels_section(section)


def test_unknown_labels_check_rejects_swapped_verification_marking() -> None:
    """Negative case for test-critic round-2 F1: proves
    `_check_unknown_labels_section` discriminates correct verified/
    not-verified markings from a SWAPPED marking that contains every
    required token in a plausible arrangement -- GitHub marked 'not
    verified live' and GitLab/Azure DevOps marked (bare) 'verified live',
    the reverse of the true claim (checked live only on GitHub) -- plus the
    right behavioural claims and a list_labels pointer, so a naive
    presence-only check would pass it."""
    plausible_wrong = (
        "Unknown labels: not_labels=[<unknown>] excludes nothing (the list "
        "stays unfiltered) on GitHub, not verified live there; on GitLab "
        "and Azure DevOps this is verified live. labels=[<unknown>] "
        "matches no ticket -> empty result on all three, inferred, not "
        "verified live. Check spelling with list_labels(project_id)."
    )
    with pytest.raises(AssertionError):
        _check_unknown_labels_section(plausible_wrong)


def test_unknown_labels_check_rejects_negated_empty_claim() -> None:
    """Negative case for test-critic round-4 F1: proves
    `_check_unknown_labels_section` rejects a paraphrase that negates the
    labels=[<unknown>] empty-result claim itself, using exactly the
    plausible-wrong example quoted in the round-4 critique JSON
    (tautology::F1's `surviving_implementation` field): "labels=[<unknown>]
    does not return an empty result (inferred)" still contains the literal
    substring "empty" the pre-round-4 check looked for anywhere in the
    labels clause, so without a negation guard tied to "empty" itself this
    would have passed. This test validates the checker's discrimination,
    not the requirement directly -- the checker still needs the real
    docstring's GREEN run to ground the requirement."""
    plausible_wrong = (
        "Unknown labels: not_labels=[<unknown>] excludes nothing (the list "
        "stays unfiltered) on GitHub, verified live there; on GitLab and "
        "Azure DevOps this is not verified live, inferred from the query "
        "the lib builds. labels=[<unknown>] does not return an empty "
        "result, inferred, not verified live. Check spelling with "
        "list_labels(project_id)."
    )
    with pytest.raises(AssertionError):
        _check_unknown_labels_section(plausible_wrong)


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

    _tickets, _has_more = GitHubProvider().list_tickets(project, "tok", _ticket_filters_any())

    # F7 fix: the real evidence is what was SENT to the provider, not the
    # mocked-empty response echoed back -- a provider that short-circuits
    # without ever calling out would trivially satisfy a bare `== []` check.
    assert "request" in captured, (
        "list_tickets must actually issue the /search/issues request "
        "carrying the unknown label, not short-circuit before sending it"
    )
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

    _tickets, _has_more = GitLabProvider().list_tickets(project, "tok", _ticket_filters_any())

    # F7 fix: assert on what was SENT, not the mocked-empty response.
    assert "request" in captured, (
        "list_tickets must actually issue the /issues request carrying the "
        "unknown label, not short-circuit before sending it"
    )
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

    _tickets, _has_more = AzureDevOpsProvider().list_tickets(
        project, "tok", _ticket_filters_any(),
    )

    # F7 fix: assert on what was SENT (the WIQL body), not the mocked-empty
    # response.
    assert "body" in captured, (
        "list_tickets must actually issue the WIQL POST carrying the "
        "unknown label, not short-circuit before sending it"
    )
    wiql = captured["body"]["query"]
    assert "[System.Tags] NOT CONTAINS 'no-such-label'" in wiql, wiql
