"""Tests for ticket #284 (bundled into work package #290): four tool
docstrings have a load-bearing detail cut off by `ToolSearch`'s ~2000-char
truncation, `list_prs` factually claims `mergeable`/`mergeable_state` are
"always null in list results" (false — they're populated via the
Search-API back-fill and on GitLab/Azure DevOps list results), and
`list_tickets_across_projects`' error duplication (`results[pid]["error"]`
+ the top-level `errors` list carrying the same string) should be
documented as intentional rather than left implicit.

Three behavioural requirements:

  R3. `list_prs`'s docstring stops claiming `mergeable`/`mergeable_state`
      are "always null in list results" and instead states, in one
      place, the fast-path/back-fill/GitLab/Azure-DevOps split.

  R4. Each of the four tools' single most truncation-vulnerable detail
      (`create_ticket` -> `list_ticket_statuses` vocabulary pointer,
      `get_ticket` -> the `Relation kinds:` sentence,
      `list_custom_fields` -> the field-key digest ending in
      `always_required`, `list_prs` -> the routing caveat) must be
      front-loaded into the first `_BUDGET` (1200) chars, WITHOUT
      pushing any caveat that is within the first `_CLIFF` (2000) chars
      today past that cliff, and without deleting any caveat's content
      (only relocating/tightening its wording is allowed).

  R5. `list_tickets_across_projects`'s docstring states the
      `results`/`errors` duplication is intentional (by design) and
      tells the caller to deduplicate client-side; `bulk.py`'s module
      docstring states isolation covers every provider and that a 401
      entry carries the same scope hint a single-project tool returns.

Measurement note: this file's `_MAX_LEN` / census-phrase-offset /
no-deletion-phrase constants were generated from the CURRENT (pre-change)
`__doc__` values of `get_ticket`, `create_ticket`, `list_custom_fields`
(`tools/tickets.py`) and `list_prs` (`tools/pulls.py`), read directly
from the installed source — NOT from the plan's own measured-with-±5%
estimates, per the plan's own instruction that an implementer with
codebase access should re-measure exactly. All phrase-offset checks
below whitespace-normalise the docstring first (collapse all runs of
whitespace, including newlines, to a single space) before computing
offsets — several of the plan-named caveat phrases wrap across a source
line break, so raw (non-normalised) offsets would be unreliable; this is
a deliberate implementation choice for this file, applied consistently
to every phrase-offset assertion here (the `_BUDGET`/`_CLIFF` figures
are themselves offsets into the *normalised* text under this scheme).
`_MAX_LEN` (a plain length ceiling, not an offset) uses the RAW
`len(__doc__)` instead, matching the plan's Approach-step-3 table, which
measured "indentation included" (i.e. the raw string).

Phase = tests: only RED driving tests + compile-level scaffolding here.
No production code (`tools/tickets.py`, `tools/pulls.py`, `tools/bulk.py`)
is touched in this file.
"""
from __future__ import annotations

import re
from typing import Callable

import pytest

from project_issues_plugin.tools import bulk as bulk_tools
from project_issues_plugin.tools import pulls as pulls_tools
from project_issues_plugin.tools import tickets as ticket_tools


# ---------- tool registration (mirrors tests/test_180_docstring_behavior.py) --


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
_pull_tools = _register(pulls_tools)
_bulk_tools = _register(bulk_tools)

_TOOLS: dict[str, Callable] = {
    "get_ticket": _ticket_tools["get_ticket"],
    "create_ticket": _ticket_tools["create_ticket"],
    "list_custom_fields": _ticket_tools["list_custom_fields"],
    "list_prs": _pull_tools["list_prs"],
}


def _norm(s: str) -> str:
    """Collapse all whitespace runs (including newlines/indentation) to a
    single space. See the module docstring for why every phrase-offset
    check in this file uses the normalised text."""
    return re.sub(r"\s+", " ", s).strip()


# ===========================================================================
# R3 — list_prs no longer claims mergeable is always null, and states the
#      split per path *and* per provider
# ===========================================================================


def test_284_list_prs_docstring_corrects_always_null_claim() -> None:
    """Driving test. On the whitespace-normalised `list_prs` docstring:

      1. The literal "always `null` in list results" claim is absent.
      2. A 600-char window anchored on the first `mergeable` mention
         contains all four limbs of the corrected story: a fast-path
         token, a back-fill token, "GitLab", and "Azure DevOps".

    RED today (both assertions fail, for two different reasons):
      1. `pulls.py:93-95` reads "mergeable` and `mergeable_state` are
         always `null` in list results" verbatim — assertion 1 fails.
      2. The 600-char window from the first `mergeable` mention today
         already happens to contain "GitLab" and "Azure DevOps" (the
         adjacent GitLab-approvals note mentions both), but contains
         NEITHER a fast-path token ("/repos/" or "fast path") NOR a
         back-fill token ("Search") — those only appear later, inside
         the routing-caveat paragraph, well past the 600-char window —
         so assertion 2 fails on the fast-path/back-fill limbs.

    GREEN once R3 lands: the corrected, front-loaded sentence from the
    plan's Approach step 4 replaces the false claim, with the routing
    caveat moved immediately after it (Approach step 3(a)), bringing all
    four limbs inside the 600-char window.
    """
    doc = _norm(_TOOLS["list_prs"].__doc__ or "")

    assert "always `null` in list results" not in doc, (
        "list_prs docstring must not claim mergeable is always null in "
        "list results — it is populated via the Search-API back-fill and "
        "on GitLab / Azure DevOps list results"
    )

    idx = doc.index("mergeable")
    window = doc[idx : idx + 600]
    fast_path_token = "/repos/" in window or "fast path" in window
    assert fast_path_token, (
        f"expected a fast-path token ('/repos/' or 'fast path') within "
        f"600 chars of the first 'mergeable' mention; window: {window!r}"
    )
    assert "Search" in window, (
        f"expected a back-fill token ('Search') within 600 chars of the "
        f"first 'mergeable' mention; window: {window!r}"
    )
    assert "GitLab" in window, (
        f"expected 'GitLab' within 600 chars of the first 'mergeable' "
        f"mention; window: {window!r}"
    )
    assert "Azure DevOps" in window, (
        f"expected 'Azure DevOps' within 600 chars of the first "
        f"'mergeable' mention; window: {window!r}"
    )


def test_284_list_prs_pre_existing_mergeable_pin_stays_green() -> None:
    """No-regression guard (already passing today): the ticket #118 pin
    (`tests/test_118_response_shape.py::test_list_prs_docstring_mentions_mergeable_null`)
    only requires 'mergeable' plus ('null' or 'get_pr') to be present —
    the corrected sentence keeps both tokens, so this stays green
    throughout. Repeated here as a same-file guard."""
    doc = _TOOLS["list_prs"].__doc__ or ""
    assert "mergeable" in doc
    assert "null" in doc or "get_pr" in doc


# ===========================================================================
# R4 — each truncation-prone detail is front-loaded without displacing an
#      existing caveat past the cliff
# ===========================================================================

_BUDGET = 1200
_CLIFF = 2000

_ANCHORS: dict[str, str] = {
    "create_ticket": "list_ticket_statuses",
    "get_ticket": "Relation kinds:",
    "list_custom_fields": "always_required",
    "list_prs": "Routing caveat",
}


@pytest.mark.parametrize("tool_name,anchor", sorted(_ANCHORS.items()))
def test_named_detail_is_front_loaded(tool_name: str, anchor: str) -> None:
    """Driving test (parametrised). Take `doc[:_BUDGET]` RAW (not the
    whole docstring), whitespace-normalise just that slice, then check
    the named detail's anchor substring is present.

    RED today — measured raw offsets of each anchor in the current
    (pre-change) docstrings, all well past `_BUDGET=1200`:
      create_ticket -> "list_ticket_statuses" @ ~2160
      get_ticket    -> "Relation kinds:"       @ ~1873
      list_custom_fields -> "always_required"  @ ~2339
      list_prs      -> "Routing caveat"        @ ~1952

    GREEN once R4's front-load + tightening pass (plan Approach step 3)
    lands: each anchor moves under 1200.

    Precedent: tests/test_docstring_119_schema_clarity.py:222-229 /
    :243-248 (`doc[:400]` / `doc[:300]` raw-slice-then-check pattern).
    """
    doc = _TOOLS[tool_name].__doc__ or ""
    window = _norm(doc[:_BUDGET])
    assert anchor in window, (
        f"{tool_name}: expected {anchor!r} within the first {_BUDGET} "
        f"(normalised) chars; got: {window!r}"
    )


# ---------- companion guards (generated from the CURRENT __doc__ values, ----
# ---------- BEFORE any production edit — see module docstring) --------------


# Distinctive caveat phrases whose end offset in the pre-change,
# whitespace-normalised docstring is <= 1900 (a 100-char safety band below
# _CLIFF=2000). Offsets were measured directly against the installed
# source (see module docstring) rather than the plan's ±5% estimates.
_PRE_CLIFF_CENSUS: dict[str, list[str]] = {
    "get_ticket": [
        "System.CreatedBy",
        "acceptance_criteria",
        "two fixed keys",
        "Call `list_custom_fields",
    ],
    "create_ticket": [
        "label catalog",
        "#ai-generated",
        "labels_add",
    ],
    "list_custom_fields": [
        "shared Team Project",
        "work_item_type=None",
        '"fields": []',
    ],
    "list_prs": [
        "approvals_required",
        'no `"merged"` filter value',
        "omit_nulls",
    ],
}


@pytest.mark.parametrize("tool_name", sorted(_PRE_CLIFF_CENSUS))
def test_pre_cliff_caveats_stay_pre_cliff(tool_name: str) -> None:
    """Guard, not a driving test — passes today (the census was built
    from today's offsets) and must keep passing after R4's production
    edit: every caveat phrase that sits within the first `_CLIFF` chars
    today must still sit within the first `_CLIFF` chars afterwards.
    This is what stops front-loading from being paid for by pushing an
    existing caveat past the truncation cliff."""
    doc = _norm(_TOOLS[tool_name].__doc__ or "")
    for phrase in _PRE_CLIFF_CENSUS[tool_name]:
        phrase_n = _norm(phrase)
        idx = doc.index(phrase_n)
        end = idx + len(phrase_n)
        assert end <= _CLIFF, (
            f"{tool_name}: caveat phrase {phrase!r} ends at {end}, past "
            f"the {_CLIFF}-char cliff"
        )


# Pre-change measured lengths (RAW `len(__doc__)`, indentation included —
# matches the plan's Approach-step-3 table's measurement convention).
# get_ticket / create_ticket / list_custom_fields use their exact current
# raw length as the ceiling (front-loading must be paid for by tightening,
# not by growth). list_prs uses the plan's stated target of 2200 — a real
# reduction the current raw length (2243) does not yet meet.
_MAX_LEN: dict[str, int] = {
    "get_ticket": 5723,
    "create_ticket": 4796,
    "list_custom_fields": 2450,
    "list_prs": 2200,
}


@pytest.mark.parametrize("tool_name", sorted(_MAX_LEN))
def test_docstring_stays_within_length_budget(tool_name: str) -> None:
    """Guard: `len(doc) <= _MAX_LEN[tool]`.

    RED today for `list_prs` only: its current raw docstring length
    (2243) exceeds the 2200 ceiling the plan sets as "a real reduction"
    — this specific case is a genuine driving assertion, not just a
    guard, since it forces list_prs's tightening pass (Approach step
    3(a)) to net-shrink the docstring, not just reshuffle it.
    `get_ticket` / `create_ticket` / `list_custom_fields` already pass
    today (their ceiling is their own current length) and must stay
    within it — i.e. any front-loading there must be paid for by
    tightening elsewhere, not by growing the docstring.
    """
    doc = _TOOLS[tool_name].__doc__ or ""
    assert len(doc) <= _MAX_LEN[tool_name], (
        f"{tool_name}: docstring grew to {len(doc)} chars, exceeding the "
        f"{_MAX_LEN[tool_name]}-char budget"
    )


# Distinctive phrases from each paragraph the plan's Approach step 3 says
# will be tightened or relocated (not the sentence being moved/rewritten
# itself — that's covered by the anchor/R3 tests above — but the REST of
# that paragraph, to guard against the whole paragraph being dropped
# rather than merely reworded). Chosen for content specificity (field
# names, concrete facts) rather than exact prose, since tightening is
# expected to reword surrounding text.
_NO_DELETION_PHRASES: dict[str, list[str]] = {
    "get_ticket": [
        # custom_fields GitHub bullet (tickets.py:309-313, tightened)
        "project.board.binding",
        # relations paragraph after the moved "Relation kinds:" sentence
        # (tickets.py:333-335) — the rest of the paragraph must survive
        "outgoing relations parsed from the queried ticket's own body",
    ],
    "create_ticket": [
        # body-newlines paragraph (tickets.py:518-522, tightened)
        "escape-sequence normalisation",
        # labels-vs-update_ticket paragraph (tickets.py:528-531, tightened)
        "labels_remove` parameters instead",
        # status paragraph's surviving tail after the vocabulary sentence
        # is moved out (tickets.py:551-554)
        "already-resolved tickets",
    ],
    "list_custom_fields": [
        # board-write-key note (tickets.py:937-941, tightened)
        "not discoverable here",
    ],
    "list_prs": [
        # status bullet (pulls.py, tightened 340->200)
        "merged: true",
        # omit_nulls bullet (pulls.py, tightened 280->170)
        "shallow strip",
        # body_max_chars marker-prefix note (pulls.py, tightened 145->100)
        "~15 chars longer",
        # GitLab approvals paragraph (pulls.py, tightened 390->270)
        "both paths compute these fields the same way",
    ],
}


@pytest.mark.parametrize("tool_name", sorted(_NO_DELETION_PHRASES))
def test_no_caveat_was_deleted(tool_name: str) -> None:
    """Guard: each relocated/tightened paragraph's distinctive phrase is
    still present SOMEWHERE in the docstring (no offset requirement —
    this only protects against outright deletion, not rewording).
    Passes today by construction; must keep passing after R4's
    production edit."""
    doc = _norm(_TOOLS[tool_name].__doc__ or "")
    for phrase in _NO_DELETION_PHRASES[tool_name]:
        phrase_n = _norm(phrase)
        assert phrase_n in doc, (
            f"{tool_name}: expected phrase {phrase!r} to survive "
            f"tightening/relocation somewhere in the docstring"
        )


# ===========================================================================
# R5 — the bulk docs state that isolation is provider-wide, that 401s
#      carry the scope hint, and that the error duplication is intentional
# ===========================================================================


def test_284_bulk_docstring_marks_error_duplication_intentional() -> None:
    """Driving test 1. The `list_tickets_across_projects` tool docstring
    must state the `results`/`errors` duplication is intentional and
    tell the caller to deduplicate client-side.

    RED today: `bulk.py:98-104` describes the duplication mechanically
    (`results[project_id]["error"]` AND the top-level `errors` list) but
    never calls it intentional/by-design and never mentions
    deduplicating.
    """
    doc = _norm(_bulk_tools["list_tickets_across_projects"].__doc__ or "")

    assert 'results[project_id]["error"]' in doc
    assert "errors" in doc
    assert "intentional" in doc or "by design" in doc, (
        "list_tickets_across_projects docstring must call the "
        "results/errors duplication intentional/by design"
    )
    assert "deduplicate" in doc, (
        "list_tickets_across_projects docstring must tell the caller to "
        "deduplicate client-side if iterating both results and errors"
    )


def test_284_bulk_module_docstring_states_provider_wide_isolation() -> None:
    """Driving test 2. `bulk.py`'s MODULE docstring must state isolation
    covers every provider's errors, and that a 401 entry carries the
    same scope hint a single-project tool returns.

    RED today: the current module docstring (`bulk.py:3-6`) says only
    "Errors on one project never abort the call" — it names no
    provider scope and never mentions a scope hint.
    """
    mod_doc = _norm(bulk_tools.__doc__ or "")

    assert "every provider" in mod_doc or "any provider" in mod_doc, (
        "bulk.py module docstring must state isolation covers every "
        "provider's errors, not just GitHub's"
    )
    assert "scope hint" in mod_doc, (
        "bulk.py module docstring must mention that a 401 entry carries "
        "the same scope hint a single-project tool returns"
    )


def test_284_bulk_summary_still_mentions_column_filtering() -> None:
    """No-regression guard (already passing today):
    `tests/test_194_board_docs.py::test_list_tickets_across_projects_summary_mentions_column_filtering`
    requires "column" in the summary region — the plan explicitly says
    R5's edits must not touch that region. Repeated here as a same-file
    guard."""
    doc = _bulk_tools["list_tickets_across_projects"].__doc__ or ""
    assert "column" in doc.split("\n\n")[0].lower() or "column" in doc[:200].lower()
