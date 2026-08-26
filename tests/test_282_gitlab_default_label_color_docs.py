"""Regression test for ticket #282 (Finding 2): documentation asymmetry in
`project_issues_plugin/tools/labels.py`. The GitHub bullet of the label
color docs states its default (``ededed``) both in the module docstring's
"Provider color format notes" and in `create_label`'s docstring, but the
GitLab bullet states no default — even though omitting `color` on GitLab
yields the same grey (``#ededed``, in GitLab's ``#RRGGBB`` form) *as this
plugin's own behavior*: its pinned lib-python-projects dependency fills in
``#ededed`` before the GitLab API call is ever made. The docstring wording
is scoped to that fact — this plugin's own default behavior — and is
deliberately not phrased as a claim about what GitLab's native API
documents or defaults to.

This is a documentation-only fix: no functional/behavioral code change.
Finding 1 (GitHub hex-color normalization) is out of scope for this
package — blocked on ticket #289's lib bump.

Follows the `_StubMCP` / module-level `register()` / `_normalize_ws()`
pattern used by `tests/test_256_docstring_self_review_and_label_catalog.py`,
and its `_azure_self_review_slice`-style windowed slicing so that a bullet
belonging to one provider cannot make an assertion pass via a neighboring
provider's text.
"""
from __future__ import annotations

import re
from typing import Callable

from project_issues_plugin.tools import labels as label_tools


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


_label_tools = _register(label_tools)


def _gitlab_bullet_slice(doc: str) -> str:
    """Slice the normalized docstring to just the GitLab bullet: from the
    `GitLab` marker up to the following `Azure DevOps` marker, so the
    neighboring GitHub bullet's `ededed`/"default" text cannot make the
    assertion pass without the GitLab bullet itself saying so.
    """
    start = doc.index("GitLab")
    end = doc.index("Azure DevOps", start)
    return doc[start:end]


# ---------------------------------------------------------------------------
# Requirement — this tool's GitLab default label color behavior is
# documented (the real gap). The assertions only check that the GitLab
# bullet mentions "#ededed" and "default" — they do not encode any claim
# about GitLab's own API; the docstring text itself is what scopes the
# claim to this plugin's behavior.
# ---------------------------------------------------------------------------


def test_create_label_docstring_documents_gitlab_default_color():
    doc = _normalize_ws(_label_tools["create_label"].__doc__ or "")
    window = _gitlab_bullet_slice(doc)
    assert "#ededed" in window
    assert "default" in window


def test_module_docstring_color_notes_document_gitlab_default():
    doc = _normalize_ws(label_tools.__doc__ or "")
    window = _gitlab_bullet_slice(doc)
    assert "#ededed" in window
    assert "default" in window


# ---------------------------------------------------------------------------
# Anti-regression guards (expected to already pass today).
# ---------------------------------------------------------------------------


def test_github_default_color_documentation_intact():
    """The neighboring GitHub bullets must survive the edit unclobbered."""
    create_doc = _normalize_ws(_label_tools["create_label"].__doc__ or "")
    assert "ededed" in create_doc
    assert "Omit to use the GitHub default" in create_doc

    module_doc = _normalize_ws(label_tools.__doc__ or "")
    assert "ededed" in module_doc
    assert "Defaults to" in module_doc


def test_update_label_docstring_still_delegates_to_create_label():
    doc = _normalize_ws(_label_tools["update_label"].__doc__ or "")
    assert "provider-specific" in doc
    assert "`create_label` docs" in doc


def test_gitlab_bare_hex_normalization_note_intact():
    create_doc = _normalize_ws(_label_tools["create_label"].__doc__ or "")
    window = _gitlab_bullet_slice(create_doc)
    assert "ff00ff" in window
    assert "normalized" in window

    module_doc = _normalize_ws(label_tools.__doc__ or "")
    window = _gitlab_bullet_slice(module_doc)
    assert "ff00ff" in window
    assert "normalized" in window


_TICKET_PATTERN = re.compile(r"ticket\s+#|#282|ticket\s+282", re.IGNORECASE)


def test_docstrings_have_no_internal_ticket_references():
    violations: list[str] = []
    if _TICKET_PATTERN.search(label_tools.__doc__ or ""):
        violations.append("<module>")
    for name in ("list_labels", "create_label", "update_label", "delete_label"):
        if _TICKET_PATTERN.search(_label_tools[name].__doc__ or ""):
            violations.append(name)
    assert not violations, (
        f"internal ticket references found in docstrings: {violations}"
    )


# ---------------------------------------------------------------------------
# Behaviour-unchanged guards (proves this is docs-only).
# ---------------------------------------------------------------------------


def test_create_label_still_returns_error_dict_for_unresolvable_project():
    result = _label_tools["create_label"](
        project_id="does-not-exist", name="x", color="#ff0000",
    )
    assert "error" in result


def test_update_label_still_returns_error_dict_for_unresolvable_project():
    result = _label_tools["update_label"](
        project_id="does-not-exist", name="x", color="ededed",
    )
    assert "error" in result
