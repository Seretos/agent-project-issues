"""Regression tests for ticket #196: four Azure DevOps documentation/
discoverability gaps in this MCP server's tool surface.

None of these are wrapper bugs — the underlying provider behavior is
correct/native — but the tool docstrings didn't warn agents about them,
so an agent without Azure DevOps domain knowledge could misinterpret
provider responses or not know a workaround exists. This is a
docstring/Field-description-only fix; no behavior changed.

  (1) `submit_pr_review`'s `state` param only accepts `approve` /
      `request_changes` / `comment`, normalizing away 2 of Azure's 5
      native reviewer votes (`approve_with_suggestions`/+5 and
      `wait_for_author`/-5). Documented on `submit_pr_review` (prose +
      `Field` description).
  (2) `create_label`/`update_label`/`delete_label` correctly reject on
      Azure, but the freeform-tag workaround (`create_ticket(labels=[...])`
      / `update_ticket(labels_add=[...])`) was not mentioned anywhere.
      Documented on all three.
  (3) When multiple configured projects share one underlying ADO Team
      Project, `list_custom_fields` returns identical area/iteration
      `allowed_values` and `area_path` filtering against the second
      project can 404. Documented on `list_custom_fields`.
  (4) On Azure, `mergeable_state` stays permanently `null` (no
      async-resolve pattern like GitHub's). Documented on `get_pr`.

Follows the `_StubMCP` / module-level `register()` / `_normalize_ws()`
pattern used by `tests/test_181_docstring_pr_cross_provider.py`.
"""
from __future__ import annotations

import re
from typing import Callable

from project_issues_plugin.tools import labels as label_tools
from project_issues_plugin.tools import pulls as pull_tools
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


_pull_tools = _register(pull_tools)
_label_tools = _register(label_tools)
_ticket_tools = _register(ticket_tools)


# ---------------------------------------------------------------------------
# Item (4) — Azure DevOps mergeable_state stays permanently null, on get_pr.
# ---------------------------------------------------------------------------


def test_get_pr_docstring_documents_ado_mergeable_state_permanently_null():
    doc = _normalize_ws(_pull_tools["get_pr"].__doc__ or "")
    assert "mergeable_state" in doc
    assert "Azure DevOps" in doc
    assert "permanently" in doc and "null" in doc
    assert "re-fetch" in doc.lower() or "re-fetching" in doc.lower()


# ---------------------------------------------------------------------------
# Item (1) — submit_pr_review's 5-vote Azure model normalized to 3, on
# submit_pr_review (prose + Field schema description).
# ---------------------------------------------------------------------------


def test_submit_pr_review_docstring_documents_azure_vote_mapping():
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "approve_with_suggestions" in doc
    assert "wait_for_author" in doc
    assert "Azure DevOps" in doc
    # 5 native votes, only 3 exposed.
    assert "5" in doc and "3" in doc


def test_submit_pr_review_state_field_schema_mentions_azure_limitation():
    """The `state` Field(description=...) feeds the generated JSON schema,
    so the limitation should be visible there too, not just in prose.

    `pulls.py` has `from __future__ import annotations`, so
    `inspect.signature(...).annotation` comes back as an unevaluated
    string — `get_type_hints(..., include_extras=True)` is required to
    resolve it to the real `Annotated[str, Field(...)]` object and keep
    the `Field` metadata (`include_extras=True` is what preserves it;
    plain `get_type_hints` strips `Annotated` wrappers).
    """
    from typing import get_type_hints

    hints = get_type_hints(
        _pull_tools["submit_pr_review"], include_extras=True,
    )
    state_hint = hints["state"]
    metadata = getattr(state_hint, "__metadata__", ())
    descriptions = [
        m.description for m in metadata if hasattr(m, "description")
    ]
    assert descriptions, "expected a Field(description=...) on `state`"
    combined = " ".join(descriptions)
    assert "approve_with_suggestions" in combined or "5" in combined


def test_submit_pr_review_still_rejects_unsupported_azure_votes():
    """Docs-only change must not widen the accepted `state` set — Azure's
    un-exposed votes remain rejected by the validation branch.
    """
    fn = _pull_tools["submit_pr_review"]
    result = fn(
        project_id="whatever",
        pr_id="1",
        state="approve_with_suggestions",
    )
    assert "error" in result

    result2 = fn(
        project_id="whatever",
        pr_id="1",
        state="wait_for_author",
    )
    assert "error" in result2


# ---------------------------------------------------------------------------
# Item (2) — Azure freeform-tag workaround, on create_label / update_label /
# delete_label.
# ---------------------------------------------------------------------------


def test_create_label_docstring_documents_freeform_tag_workaround():
    doc = _normalize_ws(_label_tools["create_label"].__doc__ or "")
    assert "create_ticket" in doc
    assert "update_ticket" in doc
    assert "freeform" in doc.lower()


def test_update_label_docstring_cross_references_workaround():
    doc = _normalize_ws(_label_tools["update_label"].__doc__ or "")
    assert "create_label" in doc
    assert "freeform" in doc.lower() or "workaround" in doc.lower()


def test_delete_label_docstring_cross_references_workaround():
    doc = _normalize_ws(_label_tools["delete_label"].__doc__ or "")
    assert "create_label" in doc
    assert "freeform" in doc.lower() or "workaround" in doc.lower()


# ---------------------------------------------------------------------------
# Item (3) — shared ADO Team Project area/iteration allowed_values collision
# and area_path 404, on list_custom_fields.
# ---------------------------------------------------------------------------


def test_list_custom_fields_docstring_documents_shared_team_project_caveat():
    doc = _normalize_ws(_ticket_tools["list_custom_fields"].__doc__ or "")
    assert "Team Project" in doc
    assert "allowed_values" in doc
    assert "area_path" in doc
    assert "404" in doc


# ---------------------------------------------------------------------------
# Hygiene guard — mirrors test_tool_docstring_hygiene.py's pattern: none of
# the new prose may contain the literal string "ticket #" (case-insensitive).
# ---------------------------------------------------------------------------

_TICKET_PATTERN = re.compile(r"ticket\s+#", re.IGNORECASE)


def test_new_docstrings_contain_no_internal_ticket_references():
    violations: list[str] = []
    checks = [
        ("pulls.get_pr", _pull_tools["get_pr"]),
        ("pulls.submit_pr_review", _pull_tools["submit_pr_review"]),
        ("labels.create_label", _label_tools["create_label"]),
        ("labels.update_label", _label_tools["update_label"]),
        ("labels.delete_label", _label_tools["delete_label"]),
        ("tickets.list_custom_fields", _ticket_tools["list_custom_fields"]),
    ]
    for name, fn in checks:
        doc = fn.__doc__ or ""
        if _TICKET_PATTERN.search(doc):
            violations.append(name)
    assert not violations, (
        f"internal ticket references found in docstrings: {violations}"
    )
