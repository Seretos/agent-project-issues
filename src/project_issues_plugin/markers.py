"""AI-attribution markers added to tickets and comments by this server.

Provider implementations call these helpers; the agent never sets the
markers manually.
"""
from __future__ import annotations

AI_GENERATED_LABEL = "ai-generated"
AI_MODIFIED_LABEL = "ai-modified"
AI_NOT_PLANNED_LABEL = "ai-closed-not-planned"   # GitLab: stand-in for state_reason

AI_COMMENT_PREFIX = "#ai-generated\n\n"

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
