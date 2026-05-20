from __future__ import annotations

from project_issues_plugin.markers import (
    AI_BODY_PREFIX,
    AI_COMMENT_PREFIX,
    AI_GENERATED_LABEL,
    AI_GENERATED_PREFIX,
    AI_MODIFIED_LABEL,
    AI_MODIFIED_PREFIX,
    AI_NOT_PLANNED_LABEL,
    LABEL_COLORS,
    LABEL_DESCRIPTIONS,
    apply_body_marker,
    ensure_body_prefix,
    ensure_comment_prefix,
    has_ai_generated_marker,
)


def test_labels_are_stable_strings():
    assert AI_GENERATED_LABEL == "ai-generated"
    assert AI_MODIFIED_LABEL == "ai-modified"
    assert AI_NOT_PLANNED_LABEL == "ai-closed-not-planned"


def test_comment_prefix_is_added_when_missing():
    body = "Hello, world."
    result = ensure_comment_prefix(body)
    assert result.startswith(AI_COMMENT_PREFIX)
    assert result.endswith(body)


def test_comment_prefix_is_not_duplicated():
    body = "#ai-generated\n\nAlready prefixed."
    assert ensure_comment_prefix(body) == body


def test_label_metadata_has_entries_for_every_label():
    for lbl in (AI_GENERATED_LABEL, AI_MODIFIED_LABEL, AI_NOT_PLANNED_LABEL):
        assert lbl in LABEL_COLORS
        assert lbl in LABEL_DESCRIPTIONS


# ---------- body-prefix marker (ticket #15) ----------------------------------


def test_body_prefix_string_is_stable():
    """`AI_BODY_PREFIX` carries the same literal as the comment prefix so a
    single grep finds both surfaces."""
    assert AI_BODY_PREFIX == "#ai-generated\n\n"
    assert AI_BODY_PREFIX == AI_COMMENT_PREFIX
    assert AI_GENERATED_PREFIX == AI_BODY_PREFIX
    assert AI_MODIFIED_PREFIX == "#ai-modified\n\n"


def test_body_prefix_added_when_missing():
    body = "Some ticket description."
    result = ensure_body_prefix(body)
    assert result.startswith(AI_BODY_PREFIX)
    assert result.endswith(body)


def test_body_prefix_is_idempotent():
    """Applying twice must yield the same string as applying once."""
    body = "Hello."
    once = ensure_body_prefix(body)
    twice = ensure_body_prefix(once)
    assert once == twice


def test_body_prefix_not_duplicated_when_already_present():
    body = "#ai-generated\n\nAlready prefixed."
    assert ensure_body_prefix(body) == body


def test_body_prefix_normalises_leading_whitespace():
    """Leading whitespace before the marker is stripped — the post-write
    body always starts cleanly with the marker line."""
    body = "\n#ai-generated\n\nAlready prefixed."
    assert ensure_body_prefix(body) == "#ai-generated\n\nAlready prefixed."


def test_body_prefix_treats_none_as_empty_body():
    """Callers may pass `None` for optional bodies; the helper must not
    crash and must still return a valid prefixed string."""
    result = ensure_body_prefix(None)
    assert result == AI_BODY_PREFIX


def test_body_prefix_handles_empty_string():
    result = ensure_body_prefix("")
    assert result == AI_BODY_PREFIX


# ---------- apply_body_marker (ticket #44) -----------------------------------


def test_apply_body_marker_generated_on_none():
    assert apply_body_marker(None, will_be_ai_generated=True) == AI_GENERATED_PREFIX


def test_apply_body_marker_modified_on_none():
    assert apply_body_marker(None, will_be_ai_generated=False) == AI_MODIFIED_PREFIX


def test_apply_body_marker_adds_modified_to_unmarked_body():
    body = "Plain human-written description."
    result = apply_body_marker(body, will_be_ai_generated=False)
    assert result == AI_MODIFIED_PREFIX + body


def test_apply_body_marker_strips_existing_generated_when_switching_to_modified():
    """Generated → modified transition replaces the marker, no stacking."""
    body = "#ai-generated\n\nFoo"
    result = apply_body_marker(body, will_be_ai_generated=False)
    assert result == "#ai-modified\n\nFoo"


def test_apply_body_marker_strips_existing_modified_when_switching_to_generated():
    """Modified → generated transition replaces the marker, no stacking."""
    body = "#ai-modified\n\nFoo"
    result = apply_body_marker(body, will_be_ai_generated=True)
    assert result == "#ai-generated\n\nFoo"


def test_apply_body_marker_no_stacking_when_marker_matches():
    body = "#ai-generated\n\nFoo"
    assert apply_body_marker(body, will_be_ai_generated=True) == body


def test_apply_body_marker_strips_only_one_marker_line():
    """If the body somehow already has two stacked marker lines (legacy),
    only the leading one is stripped — the second moves into prose, which
    is acceptable; we don't aggressively rewrite legacy content."""
    body = "#ai-generated\n\n#ai-modified\n\nFoo"
    result = apply_body_marker(body, will_be_ai_generated=True)
    assert result == "#ai-generated\n\n#ai-modified\n\nFoo"


def test_apply_body_marker_tolerates_leading_blank_lines():
    body = "\n\n#ai-generated\n\nFoo"
    result = apply_body_marker(body, will_be_ai_generated=False)
    assert result == "#ai-modified\n\nFoo"


def test_apply_body_marker_does_not_match_marker_mid_body():
    """Marker-line stripper is anchored at the start — a `#ai-...` line in
    the middle of the body is left alone."""
    body = "Real intro.\n\n#ai-generated\n\nFollowup."
    result = apply_body_marker(body, will_be_ai_generated=True)
    assert result == AI_GENERATED_PREFIX + body


# ---------- has_ai_generated_marker -----------------------------------------


def test_has_ai_generated_marker_true():
    assert has_ai_generated_marker("#ai-generated\n\nFoo") is True


def test_has_ai_generated_marker_true_with_leading_whitespace():
    assert has_ai_generated_marker("\n  #ai-generated\n\nFoo") is True


def test_has_ai_generated_marker_false_for_modified():
    assert has_ai_generated_marker("#ai-modified\n\nFoo") is False


def test_has_ai_generated_marker_false_for_plain():
    assert has_ai_generated_marker("Plain body.") is False


def test_has_ai_generated_marker_false_for_none_or_empty():
    assert has_ai_generated_marker(None) is False
    assert has_ai_generated_marker("") is False
