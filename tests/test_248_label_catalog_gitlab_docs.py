"""Regression test for #248: `create_ticket`'s and `update_ticket`'s "Label
catalog requirement" docstring paragraphs wrongly claimed GitLab, like
GitHub, enforces a pre-existing label catalog ("on GitHub (and presumably
GitLab)"). E2E testing confirmed GitLab actually auto-creates unknown
labels on the fly, like Azure DevOps' freeform tags. This is a
documentation-only fix (no functional/behavioral code change) that
corrects the wording introduced by #237/PR #238.

Follows the `_StubMCP` / module-level `register()` / `_normalize_ws()`
pattern used by `tests/test_237_label_catalog_docs.py`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from project_issues_plugin.tools import tickets as ticket_tools


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


def _normalize_ws(text: str) -> str:
    """Collapse all whitespace runs (including the newline + indentation
    between wrapped docstring lines) to a single space.

    CPython 3.13+ strips the common leading whitespace from multi-line
    docstrings at compile time (`__doc__` comes back dedented); older
    versions (e.g. the 3.12 CI pin) do not, so a wrapped docstring line
    is followed by `\\n` plus the source's indentation instead of a bare
    `\\n`. Comparing against whitespace-normalized text keeps these
    assertions correct on both.
    """
    return " ".join(text.split())


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _skill_text() -> str:
    path = _repo_root() / "skills" / "project-issues" / "SKILL.md"
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


_ticket_tools = _register(ticket_tools)

_TICKET_PATTERN = re.compile(r"ticket\s+#", re.IGNORECASE)
_GITLAB_ON_THE_FLY = re.compile(
    r"gitlab.{0,120}(created on the fly|create[sd]? on the fly|freeform)",
    re.IGNORECASE | re.DOTALL,
)


def test_create_ticket_docstring_does_not_claim_gitlab_enforces_catalog():
    doc = _normalize_ws(_ticket_tools["create_ticket"].__doc__ or "")

    # Retracted wording must be gone.
    assert "presumably" not in doc

    # GitHub's half of the claim must survive intact.
    assert "GitHub" in doc
    assert "create_label" in doc
    assert "catalog" in doc
    assert "404" in doc

    # GitLab must be positively described as NOT sharing the restriction.
    assert "GitLab" in doc
    assert _GITLAB_ON_THE_FLY.search(doc), (
        "docstring must describe GitLab as creating unknown labels on "
        "the fly, not enforcing a pre-existing catalog"
    )
    assert not _TICKET_PATTERN.search(doc)


def test_update_ticket_docstring_does_not_claim_gitlab_enforces_catalog():
    doc = _normalize_ws(_ticket_tools["update_ticket"].__doc__ or "")

    assert "presumably" not in doc

    assert "GitHub" in doc
    assert "create_label" in doc
    assert "catalog" in doc
    assert "labels_add" in doc
    assert "404" in doc

    assert "GitLab" in doc
    assert _GITLAB_ON_THE_FLY.search(doc), (
        "docstring must describe GitLab as creating unknown labels on "
        "the fly, not enforcing a pre-existing catalog"
    )
    assert not _TICKET_PATTERN.search(doc)


def test_skill_label_section_does_not_claim_gitlab_enforces_catalog():
    """Ties SKILL.md's wording to the same corrected claim as the two
    tool docstrings above, so the skill can't drift back to the
    retracted "presumably GitLab" phrasing independently of the code.
    """
    text = _normalize_ws(_skill_text())
    assert "presumably" not in text
    assert _GITLAB_ON_THE_FLY.search(text), (
        "SKILL.md must describe GitLab as creating unknown labels on "
        "the fly, not enforcing a pre-existing catalog"
    )


def test_create_ticket_still_returns_error_dict_for_unresolvable_project():
    """Docs-only change must not widen/alter behavior — an unresolvable
    project_id still surfaces as {"error": ...}, not a traceback.
    """
    fn = _ticket_tools["create_ticket"]
    result = fn(project_id="does-not-exist", title="x", labels=["new-label"])
    assert "error" in result


def test_update_ticket_still_returns_error_dict_for_unresolvable_project():
    fn = _ticket_tools["update_ticket"]
    result = fn(
        project_id="does-not-exist", ticket_id="1", labels_add=["new-label"],
    )
    assert "error" in result
