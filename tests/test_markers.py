from __future__ import annotations

from project_issues_plugin.markers import (
    AI_BODY_PREFIX,
    AI_COMMENT_PREFIX,
    AI_GENERATED_LABEL,
    AI_MODIFIED_LABEL,
    AI_NOT_PLANNED_LABEL,
    LABEL_COLORS,
    LABEL_DESCRIPTIONS,
    ensure_body_prefix,
    ensure_comment_prefix,
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


def test_body_prefix_tolerates_leading_whitespace():
    """`ensure_body_prefix` matches the `lstrip` semantics of the comment
    helper — a leading newline shouldn't trigger a duplicate prefix."""
    body = "\n#ai-generated\n\nAlready prefixed."
    assert ensure_body_prefix(body) == body


def test_body_prefix_treats_none_as_empty_body():
    """Callers may pass `None` for optional bodies; the helper must not
    crash and must still return a valid prefixed string."""
    result = ensure_body_prefix(None)
    assert result == AI_BODY_PREFIX


def test_body_prefix_handles_empty_string():
    result = ensure_body_prefix("")
    assert result == AI_BODY_PREFIX
