"""Guard tests for ticket #283 — two documentation-only findings from the
E2E sweep of `add_relation` / `remove_relation` / `list_hierarchy` in
`project_issues_plugin/tools/relations.py`. No runtime behaviour changes;
these lock in docstring content and ordering only.

  (1) `add_relation`'s `duplicate_of` side-effect callout ("Duplicate of
      #N appends... AND closes the source, on GitHub and GitLab") lived
      near the very end of the docstring, inside "Provider-specific
      notes", after the direction/kind/target/cross-project/symmetry
      prose. It is repositioned to sit immediately after the `Returns:`
      section — the same "important info first" fix ticket #223 already
      applied to the `Returns:` block itself. #223's own pinned ordering
      guard (`Returns:` before `on GitHub and GitLab`, checked on the raw
      docstring) must still hold.

  (2) None of `add_relation`, `remove_relation`, `list_hierarchy`
      documented the deliberate `#N`-reference-form vs. bare-id
      convention used across their response shapes:
        - `add_relation`'s `relation.ticket_id` -> `#N` form.
        - `remove_relation`'s `target` echo -> bare form (unchanged,
          docs only).
        - `list_hierarchy`'s top-level `ticket_id` -> bare form, but its
          `parent`/`children` entries' `ticket_id` -> `#N` form.

Follows the `_StubMCP` + module-level `_register(relation_tools)` pattern
used by `tests/test_docstring_223_add_relation_returns_ordering.py`,
`tests/test_docstring_236_add_relation_kind_direction.py`, and
`tests/test_250_docstring_relation_review_side_effects.py`, and reuses
#250's `_normalize_ws` whitespace-insensitive-substring helper (CPython
3.13+ dedents `__doc__` at compile time; the CI-pinned 3.12 does not, so
wrapped docstring lines carry `\\n` + source indentation on 3.12 but not
on 3.13+ — normalising whitespace keeps assertions correct on both).

Ordering assertions (behaviour 1) run against the **raw** `__doc__`,
mirroring #223's own check. Content/attribution assertions (behaviours
2-4) run against the whitespace-normalised doc.
"""
from __future__ import annotations

import re
from typing import Callable

from project_issues_plugin.tools import relations as relation_tools


class _StubMCP:
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


def _normalize_ws(text: str) -> str:
    """Collapse all whitespace runs (including the newline + indentation
    between wrapped docstring lines) to a single space. See module
    docstring for why this is needed across the 3.12/3.13+ split.
    """
    return " ".join(text.split())


_tools = _register(relation_tools)


# ---------------------------------------------------------------------------
# Behaviour 1 — duplicate_of callout promoted above low-priority prose.
# ---------------------------------------------------------------------------


def test_add_relation_docstring_duplicate_of_callout_precedes_direction_matters():
    """Driving test: today the callout sits near the end (inside
    "Provider-specific notes"), well after "Direction matters" near the
    top — this must fail RED for that reason until the callout moves.

    Also pins that "Duplicate of #N" occurs exactly **twice** in the
    (whitespace-normalised) docstring — that is the genuine, unmoved
    content: once in the "appends a `Duplicate of #N` line" clause, once
    in the "strips the `Duplicate of #N` line" reversal clause later in
    the same bullet (confirmed against the current, pre-move docstring —
    a naive "exactly once" expectation is unreachable by any verbatim
    carry-over move and would make this driving test impossible to turn
    GREEN without deleting real content). A do-nothing edit that inserts
    an early forward-reference line (e.g. "See the Duplicate of #N note
    under Provider-specific notes below.") while leaving the real bullet
    in place would satisfy the ordering check alone, since it plants an
    earlier occurrence without actually moving the bullet — but it raises
    the count to three, still caught by the `== 2` guard.

    Also pins "closes" (the literal word the current docstring uses for
    the auto-close side effect, distinct from the unrelated `closed_by`
    kind name that appears later in the doc) to the same promoted
    position and exactly-once count (the genuine bullet uses "closes"
    only once, unlike "Duplicate of #N") — a split edit that moves only
    the "Duplicate of #N" phrase forward while leaving the auto-close
    sentence behind in "Provider-specific notes" would satisfy the
    assertion above alone but fail this one, since "closes" would still
    first occur after "Direction matters".
    """
    doc = _tools["add_relation"].__doc__ or ""
    assert doc.index("Duplicate of #N") < doc.index("Direction matters"), repr(doc)
    assert doc.index("closes") < doc.index("Direction matters"), repr(doc)
    normalized = _normalize_ws(doc)
    assert normalized.count("Duplicate of #N") == 2, repr(normalized)
    assert normalized.count("closes") == 1, repr(normalized)


def test_add_relation_docstring_duplicate_of_callout_precedes_symmetry_cross_project_and_provider_notes():
    """Also pins the exactly-twice-occurrence guard described on the
    sibling ordering test above, for the same reason, and the same
    "closes" whole-side-effect-clause ordering pin."""
    doc = _tools["add_relation"].__doc__ or ""
    assert doc.index("Duplicate of #N") < doc.index("Symmetry:"), repr(doc)
    assert doc.index("Duplicate of #N") < doc.index(
        "Cross-project references are rejected"
    ), repr(doc)
    assert doc.index("Duplicate of #N") < doc.index("Provider-specific notes"), repr(doc)
    assert doc.index("closes") < doc.index("Symmetry:"), repr(doc)
    assert doc.index("closes") < doc.index(
        "Cross-project references are rejected"
    ), repr(doc)
    assert doc.index("closes") < doc.index("Provider-specific notes"), repr(doc)
    normalized = _normalize_ws(doc)
    assert normalized.count("Duplicate of #N") == 2, repr(normalized)
    assert normalized.count("closes") == 1, repr(normalized)


def test_add_relation_docstring_duplicate_of_callout_still_follows_returns():
    """Already-passing guard (not RED): the callout must stay after
    `Returns:`, preserving #223's pinned ordering.

    The plan is explicit that the callout goes after the *complete*
    `Returns:` unit — the fenced shape block, the `relation.ticket_id`
    disambiguation paragraph, the "fully hydrated / same shape"
    paragraph, AND the `resolved` bullet list — not merely after the
    literal `Returns:` header. Pinning only `"Returns:" < "Duplicate of
    #N"` would let an edit wedge the callout into the *middle* of that
    unit (e.g. between the "fully hydrated" paragraph and the
    `resolved` bullets) and still pass, since the header alone precedes
    everything that follows it. `"liveness is unknown (provider did
    not indicate)."` is the literal last line of the `resolved` bullet
    list today — i.e. the end of the Returns unit — so requiring it to
    precede the callout too pins the callout to sit after the *whole*
    unit, not just after its opening header.
    """
    doc = _tools["add_relation"].__doc__ or ""
    assert doc.index("Returns:") < doc.index("Duplicate of #N"), repr(doc)
    assert doc.index(
        "liveness is unknown (provider did not indicate)."
    ) < doc.index("Duplicate of #N"), repr(doc)


def test_add_relation_docstring_preserves_duplicate_of_content_and_github_gitlab_phrase():
    """Already-passing guards: no information lost in the move, and
    #197's "GitHub and GitLab" (not "both providers") phrasing survives.
    """
    doc = _normalize_ws(_tools["add_relation"].__doc__ or "")
    assert "on GitHub and GitLab" in doc, repr(doc)
    assert "both providers" not in doc, repr(doc)
    for substring in ("Duplicate of #N", "remove_relation", "Azure DevOps"):
        assert substring in doc, repr(substring)
    assert "closes" in doc or "closed" in doc, repr(doc)


_TICKET_PATTERN = re.compile(r"ticket\s+#|#283", re.IGNORECASE)


def test_add_relation_docstring_has_no_internal_ticket_references():
    """Already-passing hygiene guard, scoped to this ticket's edits."""
    doc = _tools["add_relation"].__doc__ or ""
    assert not _TICKET_PATTERN.search(doc), repr(doc)


# ---------------------------------------------------------------------------
# Behaviour 2 — add_relation documents the #N vs. bare convention.
# ---------------------------------------------------------------------------


def test_add_relation_docstring_documents_reference_form_convention():
    """Driving test: none of this vocabulary exists in the docstring
    today, so the windowed assertion fails RED for the expected reason
    (the convention is undocumented).

    Windowed on `relation.ticket_id`'s first mention, with an ordering
    check, rather than a bare substring-anywhere check — a docstring
    that states the split **backwards** (e.g. "`relation.ticket_id`
    comes back bare... unlike `remove_relation`'s `target`, which uses
    the reference form") would still satisfy a loose "these tokens
    appear somewhere nearby" check, since both "bare" and "reference
    form"/"#187" would land in the same window either way. Requiring the
    concrete `#187` example marker to appear *before* any "bare" mention
    in that window ties the #N-form claim specifically to
    `relation.ticket_id` and rejects the backwards telling (which puts
    "bare" first).

    `#187` (not just any of the three markers) is required, together
    with at least one of the explanatory phrases ("reference form" /
    "leading `#`") — a docstring that only says "reference form" without
    ever giving the `#187` example, or that only gives a bare `#187`
    without explaining what it means, would previously have slipped
    through the old any-of-three-markers disjunction; both are now
    mandatory.

    `remove_relation` and `list_hierarchy` must be cross-referenced
    *inside this same bounded window*, not just anywhere in the whole
    docstring — both tokens already occur elsewhere in the unmodified
    docstring today (`remove_relation` in the unrelated `duplicate_of`
    reversal bullet near the end; `list_hierarchy` in the unrelated
    "same shape as ... entries" sentence just past this window), so an
    anywhere-in-doc check would already be satisfied today and could
    never actually fail RED for the missing convention sentence.

    The window must also state the **contrast** with the bare form, not
    just the `#N` form in isolation — a docstring that only said
    "`relation.ticket_id` comes back in reference form (e.g. `#187`);
    see `remove_relation` and `list_hierarchy`" would satisfy every
    check above without ever saying those other tools use the *bare*
    form instead. Requiring `"bare"` inside the window (unconditionally,
    not gated behind an `if`) — and, per the backwards-telling guard
    above, requiring it to come *after* `#187` — pins that the contrast
    is actually spelled out here, matching the plan's own wording
    ("differs from the **bare** numeric form used by ...
    `remove_relation`'s `target` echo").
    """
    doc = _normalize_ws(_tools["add_relation"].__doc__ or "")
    idx = doc.index("relation.ticket_id")
    window = doc[idx : idx + 500]
    assert "#187" in window, repr(window)
    explanatory = [marker for marker in ("reference form", "leading `#`") if marker in window]
    assert explanatory, repr(window)
    assert "bare" in window, repr(window)
    ref_pos = window.index("#187")
    assert ref_pos < window.index("bare"), repr(window)
    assert "remove_relation" in window, repr(window)
    assert "list_hierarchy" in window, repr(window)


def test_add_relation_docstring_preserves_existing_content_around_convention():
    """Already-passing guards: the surrounding Returns-block prose this
    convention text will sit near must survive untouched.
    """
    doc = _normalize_ws(_tools["add_relation"].__doc__ or "")
    assert "target/other" in doc, repr(doc)
    assert "distinct from" in doc, repr(doc)
    assert "fully hydrated" in doc, repr(doc)
    assert "same shape" in doc, repr(doc)


def test_add_relation_docstring_relation_ticket_id_still_precedes_low_priority_prose():
    """Already-passing guard, mirroring #223's own check."""
    doc = _tools["add_relation"].__doc__ or ""
    assert doc.index("relation.ticket_id") < doc.index("on GitHub and GitLab"), repr(doc)


# ---------------------------------------------------------------------------
# Behaviour 3 — remove_relation documents the bare `target` echo.
# ---------------------------------------------------------------------------


def test_remove_relation_docstring_documents_bare_target_convention():
    """Driving test: "bare" is not in the docstring today, so this fails
    RED for the expected reason (the convention is undocumented).

    Windowed on `target`'s mention in the `Returns:` section (the pinned
    `"target": str, "removed": true` shape text), with an ordering
    check, rather than a bare substring-anywhere check — a docstring
    stating the split backwards (e.g. "`target` is echoed in reference
    form (`#187`), unlike `add_relation`'s bare `relation.ticket_id`")
    would still satisfy a loose "these tokens appear somewhere nearby"
    check.

    The guard is unconditional on both sides: "bare" must appear in the
    window near `target`'s Returns mention (positive), and "reference
    form" is required to exist in the doc at all (positive, not gated
    behind an `if`) with its *first* occurrence — wherever in the whole
    doc it sits, not just inside the post-anchor window — required to
    come *after* `target`'s "bare" mention (negative/ordering, spanning
    the whole doc rather than only the text after the anchor). This
    rejects both the backwards telling that places "reference form"
    inside the window ahead of "bare", and one that dodges the window
    entirely by placing the backwards claim earlier in the docstring,
    before the `Returns:` anchor — a purely post-anchor-window ordering
    check would miss that second case since it never looks earlier than
    the anchor.

    Also rejects the "target is echoed with a `#`" decoy: nothing above
    stops a docstring from documenting the bare convention *and* still
    showing `target`'s echoed value with a leading `#` — e.g. "`target`
    is echoed as `#187` — the bare, normalised id in reference form,
    unlike `add_relation`'s `relation.ticket_id`." satisfies every check
    above ("bare" is in the window, "reference form" occurs later,
    "add_relation" is present) while literally claiming `target` comes
    back as `#187`, the false statement the plan says must be excluded.
    The plan-compliant wording only ever puts a `#`-prefixed id near
    `target` when describing the *accepted input* that was normalised
    away (e.g. "even when `#187` was passed"), never as target's
    *echoed/returned* value — so requiring "passed" to follow any
    `#<digits>` token shortly after in the window accepts the former and
    rejects the latter (verified against both the decoy sentence above
    and the plan's own compliant sentence with a throwaway scratch
    check).
    """
    doc = _normalize_ws(_tools["remove_relation"].__doc__ or "")
    anchor = '"target": str, "removed": true'
    idx = doc.index(anchor)
    window = doc[idx : idx + 350]
    assert "bare" in window, repr(window)
    assert "reference form" in doc, repr(doc)
    bare_doc_pos = idx + window.index("bare")
    ref_doc_pos = doc.index("reference form")
    assert bare_doc_pos < ref_doc_pos, repr(doc)
    assert "add_relation" in doc, repr(doc)
    for match in re.finditer(r"#\d+", window):
        tail = window[match.end() : match.end() + 30]
        assert "passed" in tail, (
            "a `#`-prefixed id near target's Returns example must only "
            "describe the accepted *input* form (followed by 'passed'), "
            f"never claim target is echoed with a leading '#': {window!r}"
        )


def test_remove_relation_docstring_preserves_returns_shape_text():
    """Already-passing guard: the existing Returns shape text (and the
    `target` echo value itself) must stay unchanged — docs only."""
    doc = _normalize_ws(_tools["remove_relation"].__doc__ or "")
    assert (
        '"project_id": str, "kind": str, "target": str, "removed": true' in doc
    ), repr(doc)


# ---------------------------------------------------------------------------
# Behaviour 4 — list_hierarchy documents both id forms, correctly
# attributed to the right field.
# ---------------------------------------------------------------------------


def test_list_hierarchy_docstring_top_level_ticket_id_documented_as_bare():
    """Driving test: "top-level" is not in the docstring today, so
    `doc.index` raises for the expected reason (the top-level `ticket_id`
    field's bare-form convention is undocumented).

    Windowed on "top-level" with an ordering check, not a wide
    substring-anywhere-in-window check — the prior 200-char window was
    wide enough that a single contrasting sentence with the split
    written backwards (e.g. "the top-level `ticket_id` uses the
    `#N` reference form, unlike each entry's bare id") would satisfy it,
    since both "bare" and "reference form"/"#N" land inside the same
    window regardless of which field they actually describe. Requiring
    "bare" to appear immediately after "top-level" (within a short
    sub-window) and, if a "reference form"/"#N" token appears at all in
    the wider window, requiring it to come *after* "bare" — rejects the
    backwards telling (which puts the #N-form marker first).
    """
    doc = _normalize_ws(_tools["list_hierarchy"].__doc__ or "")
    idx = doc.index("top-level")
    window = doc[idx : idx + 250]
    early = window[:100]
    assert "ticket_id" in window, repr(window)
    assert "bare" in early, repr(early)
    ref_positions = [
        window.index(marker) for marker in ("reference form", "#N") if marker in window
    ]
    if ref_positions:
        assert window.index("bare") < min(ref_positions), repr(window)


def test_list_hierarchy_docstring_entry_ticket_id_documented_as_reference_form():
    """Driving test: "entries" (plural, referring to `parent`/`children`
    entries) is not in the docstring today, so `doc.index` raises for the
    expected reason.

    Windowed on "entries" with an ordering check, symmetric to the
    top-level test above — the prior 250-char window was wide enough
    that a backwards-split sentence would satisfy it too. Requiring a
    "reference form"/"#N" token to appear immediately after "entries"
    (within a short sub-window) and, if "bare" appears at all in the
    wider window, requiring it to come *after* the #N-form marker —
    rejects the backwards telling (which puts "bare" first, attributing
    it to the entries instead of the top-level field).

    Anchored on the *last* "entries"/"entry's" mention, not the first:
    the docstring already has an unrelated, pre-existing "...
    `parent`/`child` entries." in its opening "one-call alternative"
    paragraph (describing `get_ticket`'s relations list, not this
    field's id form). A plain first-occurrence `doc.index` would lock
    onto that unrelated sentence forever and could never observe the
    new convention note wherever it lands — `rindex` over both spellings
    picks whichever mention is physically last, which is where new prose
    gets appended.
    """
    doc = _normalize_ws(_tools["list_hierarchy"].__doc__ or "")
    candidates = [doc.rindex(marker) for marker in ("entries", "entry's") if marker in doc]
    assert candidates, repr(doc)
    idx = max(candidates)
    window = doc[idx : idx + 250]
    early = window[:100]
    assert "ticket_id" in window, repr(window)
    ref_positions_early = [
        early.index(marker) for marker in ("reference form", "#N") if marker in early
    ]
    assert ref_positions_early, repr(early)
    ref_pos = min(
        window.index(marker) for marker in ("reference form", "#N") if marker in window
    )
    if "bare" in window:
        assert ref_pos < window.index("bare"), repr(window)


def test_list_hierarchy_docstring_cross_references_add_relation():
    """Driving test: `add_relation` does not appear anywhere in
    `list_hierarchy`'s docstring today (confirmed against the current
    source), so this fails RED for the expected reason — the
    cross-reference is missing.

    Windowed on the same last-"entries"/"entry's" anchor used by
    `test_list_hierarchy_docstring_entry_ticket_id_documented_as_reference_form`
    above, rather than an anywhere-in-doc check — an unwindowed
    `"add_relation" in doc` would also pass if a future edit mentioned
    `add_relation` somewhere unrelated (e.g. in the "one-call
    alternative to `get_ticket`" opening paragraph) without ever tying
    it to the entries' `ticket_id` reference-form note, which is what
    the plan actually asks for: the entries' id-form note should cite
    `add_relation` as the tool that produces the same `#N` form.
    """
    doc = _normalize_ws(_tools["list_hierarchy"].__doc__ or "")
    candidates = [doc.rindex(marker) for marker in ("entries", "entry's") if marker in doc]
    assert candidates, repr(doc)
    idx = max(candidates)
    window = doc[idx : idx + 250]
    assert "add_relation" in window, repr(window)


def test_list_hierarchy_docstring_preserves_existing_content():
    """Already-passing guards: existing prose this ticket must not
    disturb."""
    doc = _normalize_ws(_tools["list_hierarchy"].__doc__ or "")
    assert "relations_truncated" in doc, repr(doc)
    assert (
        "same shape as an item in `get_ticket`'s `relations` list" in doc
    ), repr(doc)
