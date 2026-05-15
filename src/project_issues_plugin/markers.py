"""AI-attribution markers added to tickets and comments by this server.

Provider implementations call these helpers; the agent never sets the
markers manually.
"""
from __future__ import annotations

AI_GENERATED_LABEL = "ai-generated"
AI_MODIFIED_LABEL = "ai-modified"
AI_NOT_PLANNED_LABEL = "ai-closed-not-planned"   # GitLab: stand-in for state_reason

AI_COMMENT_PREFIX = "#ai-generated\n\n"
# Same literal as AI_COMMENT_PREFIX, kept as a distinct name so callers
# express intent: ticket / PR bodies get an AI_BODY_PREFIX, comments get
# an AI_COMMENT_PREFIX. The body prefix is the source of truth for AI
# attribution; the `ai-generated` LABEL is best-effort decoration that
# can be silently dropped or refused depending on the caller's GitHub
# permissions on the target repo (see `providers/github.py`).
AI_BODY_PREFIX = "#ai-generated\n\n"

LABEL_COLORS = {
    AI_GENERATED_LABEL: "0e8a16",     # green
    AI_MODIFIED_LABEL: "fbca04",      # yellow
    AI_NOT_PLANNED_LABEL: "cccccc",   # grey
}

LABEL_DESCRIPTIONS = {
    AI_GENERATED_LABEL: "Created by the project-issues AI agent",
    AI_MODIFIED_LABEL: "Modified by the project-issues AI agent",
    AI_NOT_PLANNED_LABEL: "Closed as 'not planned' by the project-issues AI agent",
}


def ensure_comment_prefix(body: str) -> str:
    """Prepend the AI-comment marker unless the body already starts with it."""
    if body.lstrip().startswith("#ai-generated"):
        return body
    return AI_COMMENT_PREFIX + body


def ensure_body_prefix(body: str | None) -> str:
    """Prepend the AI body marker unless the body already starts with it.

    Mirrors `ensure_comment_prefix` but for ticket / PR bodies. The helper
    is idempotent — applying it twice produces the same string as applying
    it once. `None` is treated as an empty body so callers can pass through
    optional inputs without a `None`-check.
    """
    text = body or ""
    if text.lstrip().startswith("#ai-generated"):
        return text
    return AI_BODY_PREFIX + text
