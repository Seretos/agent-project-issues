from __future__ import annotations

from project_issues_plugin.markers import (
    AI_COMMENT_PREFIX,
    AI_GENERATED_LABEL,
    AI_MODIFIED_LABEL,
    AI_NOT_PLANNED_LABEL,
    LABEL_COLORS,
    LABEL_DESCRIPTIONS,
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
