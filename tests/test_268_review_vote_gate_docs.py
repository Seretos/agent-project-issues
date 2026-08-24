"""Tests for ticket #268 — document that Azure DevOps review/vote submission
(`add_pr_review_comment`, `submit_pr_review`) is gated by the generic
`permissions.pulls.modify` flag.

Docs-only ticket. `add_pr_review_comment` and `submit_pr_review` are real
write operations that both route through `_require_pulls_modify`, yet every
user-facing description of the `pulls.modify` gate historically listed only
`update_pr` / `add_pr_comment`. This package makes the gate's true blast
radius visible in README.md, SECURITY.md, the `_require_pulls_modify` error
message, and both tool docstrings. No lib change, no new permission flag, no
behavior change — the design-review rationale (reviewing is intentionally
treated as modifying the PR; there is no separate review permission flag) is
closed by the documentation itself.

Five behavioural requirements (R1-R5), R5 split into three driving tests
(gate message + two docstrings) as directed by the plan:

  R1. README permissions table's `pulls.modify` row lists both review tools.
  R2. README `permissions.pulls.*` namespace bullet covers both review tools
      and the words "reviewer vote".
  R3. README "## PR tools" section gets two new bullets, each carrying the
      exact gate-rationale clause.
  R4. SECURITY.md's "## Permission gating" table gets rows for every write
      tool (not just tickets), including both review tools.
  R5. `_require_pulls_modify`'s message names "submitting reviews/votes";
      both tool docstrings state the gate rationale.

All literal-fragment assertions are CASE-SENSITIVE plain containment checks
(`fragment in text`) — never lowercase either side, never IGNORECASE, per
the plan's explicit instruction.

Follows the `_StubMCP` / module-level `register()` pattern used by
`tests/test_196_azure_docs.py` / `tests/test_194_board_docs.py`, the
`_normalize_ws` whitespace-collapse helper from `test_196_azure_docs.py`
(needed because CPython <3.13 does not dedent multi-line docstrings the way
3.13+ does, so a wrapped sentence can carry embedded newline+indentation),
and the fake-`ProjectConfig`/denied-project construction pattern from
`tests/test_266_error_tone_actionability.py`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import pytest

from lib_python_projects import Permissions, ProjectConfig, ProjectsLoadResult
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools._providers import _require_pulls_modify

_ROOT = Path(__file__).resolve().parent.parent
_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_SECURITY = (_ROOT / "SECURITY.md").read_text(encoding="utf-8")


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
    between wrapped docstring lines) to a single space. See module
    docstring / `test_196_azure_docs.py` for why this is needed."""
    return " ".join(text.split())


def _denied_project(project_id: str = "acme", provider: str = "github") -> ProjectConfig:
    """A project whose `Permissions()` default to all-False, so
    `_require_pulls_modify` is denied. Mirrors
    `test_266_error_tone_actionability.py::_denied_project`."""
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=f"{project_id}/backend",
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(),
    )


_pull_tools = _register(pull_tools)


# ---------------------------------------------------------------------------
# R1 — README permissions (schema reference) table's `pulls.modify` row
# ---------------------------------------------------------------------------


def test_readme_permissions_table_pulls_modify_lists_review_tools():
    """RED today: the `pulls.modify` row's Gated-tool(s) cell only lists
    `update_pr`, `add_pr_comment` — neither review tool appears."""
    rows = [
        line for line in _README.splitlines()
        if line.startswith("| `pulls.modify`")
    ]
    assert len(rows) == 1, (
        f"expected exactly one `pulls.modify` row; found {len(rows)}: {rows!r}"
    )
    row = rows[0]
    assert "add_pr_review_comment" in row, f"row missing add_pr_review_comment: {row!r}"
    assert "submit_pr_review" in row, f"row missing submit_pr_review: {row!r}"


def test_readme_permissions_table_pulls_create_and_merge_rows_not_widened():
    """Edge case: only the `pulls.modify` row grows — `pulls.create` and
    `pulls.merge` stay untouched, and there is still exactly one row for
    each gate (no accidental duplication)."""
    create_rows = [
        line for line in _README.splitlines()
        if line.startswith("| `pulls.create`")
    ]
    merge_rows = [
        line for line in _README.splitlines()
        if line.startswith("| `pulls.merge`")
    ]
    assert len(create_rows) == 1
    assert len(merge_rows) == 1
    assert "add_pr_review_comment" not in create_rows[0]
    assert "submit_pr_review" not in create_rows[0]
    assert "add_pr_review_comment" not in merge_rows[0]
    assert "submit_pr_review" not in merge_rows[0]


# ---------------------------------------------------------------------------
# R2 — README `permissions.pulls.*` namespace bullet
# ---------------------------------------------------------------------------


def test_readme_pulls_namespace_bullet_covers_review_and_vote():
    """RED today: none of the three literal fragments are present on the
    `permissions.pulls.create` bullet line."""
    lines = [
        line for line in _README.splitlines()
        if "permissions.pulls.create" in line
    ]
    assert len(lines) == 1, (
        f"expected exactly one line naming permissions.pulls.create; "
        f"found {len(lines)}: {lines!r}"
    )
    line = lines[0]
    assert "add_pr_review_comment" in line, f"line missing add_pr_review_comment: {line!r}"
    assert "submit_pr_review" in line, f"line missing submit_pr_review: {line!r}"
    assert "reviewer vote" in line, f"line missing literal 'reviewer vote': {line!r}"


def test_readme_pulls_namespace_bullet_merge_opt_in_sentence_survives():
    """Edge case: the pre-existing `pulls.merge` default-false opt-in
    sentence must still be present on (or alongside) that same bullet."""
    lines = [
        line for line in _README.splitlines()
        if "permissions.pulls.create" in line
    ]
    line = lines[0]
    assert "defaults to false" in line
    assert "opt in deliberately" in line


# ---------------------------------------------------------------------------
# R3 — README "## PR tools" section bullets
# ---------------------------------------------------------------------------

_GATE_RATIONALE = (
    "gated by `pulls.modify` (reviewing is treated as modifying the PR; "
    "no separate review flag)"
)


def _pr_tools_section() -> str:
    start = _README.index("## PR tools")
    rest = _README[start + len("## PR tools"):]
    m = re.search(r"^## ", rest, flags=re.MULTILINE)
    end = m.start() if m else len(rest)
    return rest[:end]


def _bullet_line(section: str, tool_name: str) -> str:
    """Locate the bullet whose leading token is "- `<tool_name>("."""
    pattern = re.compile(rf"^- `{re.escape(tool_name)}\(.*$", flags=re.MULTILINE)
    m = pattern.search(section)
    assert m is not None, f"no bullet found for {tool_name!r} in PR tools section"
    return m.group(0)


def test_readme_pr_tools_section_lists_review_tools_with_gate_rationale():
    """RED today: the PR tools section ends at `merge_pr` — neither tool nor
    the gate-rationale phrase (case-sensitive, lowercase 'gated') is
    present."""
    section = _pr_tools_section()
    assert "add_pr_review_comment" in section
    assert "submit_pr_review" in section

    review_comment_bullet = _bullet_line(section, "add_pr_review_comment")
    assert _GATE_RATIONALE in review_comment_bullet, (
        f"add_pr_review_comment bullet missing gate rationale: "
        f"{review_comment_bullet!r}"
    )

    submit_review_bullet = _bullet_line(section, "submit_pr_review")
    assert _GATE_RATIONALE in submit_review_bullet, (
        f"submit_pr_review bullet missing gate rationale: "
        f"{submit_review_bullet!r}"
    )


def test_readme_pr_tools_section_new_bullets_ordered_between_comment_and_merge():
    """Edge case: the two new bullets sit between the `add_pr_comment` and
    `merge_pr` bullets, and pre-existing bullets are unchanged."""
    section = _pr_tools_section()
    add_comment_idx = section.index("- `add_pr_comment(")
    review_comment_idx = section.index("- `add_pr_review_comment(")
    submit_review_idx = section.index("- `submit_pr_review(")
    merge_idx = section.index("- `merge_pr(")

    assert add_comment_idx < review_comment_idx < submit_review_idx < merge_idx, (
        "expected order: add_pr_comment, add_pr_review_comment, "
        "submit_pr_review, merge_pr"
    )

    # Pre-existing bullets unchanged.
    assert "- `list_prs(project_id, status, labels, assignee, head, base, search, limit)` — read-only." in section
    assert "- `get_pr(project_id, pr_id)` — returns the PR plus its issue-style discussion comments" in section
    assert "- `create_pr(project_id, title, body, head, base, draft, labels, assignees)` — gated by `pulls.create`." in section


def test_readme_pr_tools_submit_pr_review_bullet_names_states_and_vote_mapping():
    """Edge case: the `submit_pr_review` bullet names all three `state`
    values and the +10/-10 Azure vote mapping."""
    section = _pr_tools_section()
    submit_review_bullet = _bullet_line(section, "submit_pr_review")
    assert "approve" in submit_review_bullet
    assert "request_changes" in submit_review_bullet
    assert "comment" in submit_review_bullet
    assert "+10" in submit_review_bullet
    assert "-10" in submit_review_bullet


# ---------------------------------------------------------------------------
# R4 — SECURITY.md "## Permission gating" table
# ---------------------------------------------------------------------------


def _permission_gating_section() -> str:
    start = _SECURITY.index("## Permission gating")
    rest = _SECURITY[start + len("## Permission gating"):]
    m = re.search(r"^## ", rest, flags=re.MULTILINE)
    end = m.start() if m else len(rest)
    return rest[:end]


def test_security_gate_table_lists_pr_review_gates():
    """RED today: no `pulls.*` rows exist at all in the gate table, so
    neither `add_pr_review_comment` nor `submit_pr_review` (nor
    `permissions.pulls.modify`) appears."""
    section = _permission_gating_section()
    assert "add_pr_review_comment" in section
    assert "submit_pr_review" in section
    assert "permissions.pulls.modify" in section

    # Each review tool's own row must name permissions.pulls.modify.
    for tool_name in ("add_pr_review_comment", "submit_pr_review"):
        rows = [
            line for line in section.splitlines()
            if line.strip().startswith(f"| `{tool_name}`")
        ]
        assert len(rows) == 1, f"expected exactly one row for {tool_name}: {rows!r}"
        assert "permissions.pulls.modify" in rows[0], (
            f"{tool_name} row does not name permissions.pulls.modify: {rows[0]!r}"
        )


def test_security_gate_table_covers_all_pr_write_tools():
    """Edge case: rows also exist for `create_pr`, `update_pr`,
    `add_pr_comment`, `merge_pr` naming their own `permissions.pulls.*`
    flag."""
    section = _permission_gating_section()
    expectations = {
        "create_pr": "permissions.pulls.create",
        "update_pr": "permissions.pulls.modify",
        "add_pr_comment": "permissions.pulls.modify",
        "merge_pr": "permissions.pulls.merge",
    }
    for tool_name, flag in expectations.items():
        rows = [
            line for line in section.splitlines()
            if line.strip().startswith(f"| `{tool_name}`")
        ]
        assert len(rows) == 1, f"expected exactly one row for {tool_name}: {rows!r}"
        assert flag in rows[0], f"{tool_name} row does not name {flag}: {rows[0]!r}"


def test_security_gate_table_intro_no_longer_scoped_to_tickets_py():
    """Edge case: intro sentence widens beyond `tools/tickets.py` to cover
    the ticket and pull-request write tools the table actually documents —
    without overclaiming coverage of every write tool in the codebase
    (comments/labels/relations/board/pipelines tools have no table rows)."""
    section = _permission_gating_section()
    intro = section.split("|", 1)[0]
    assert "tools/tickets.py" not in intro, (
        f"intro sentence still scopes gating to tools/tickets.py only: {intro!r}"
    )
    assert "ticket" in intro, (
        f"expected intro to mention ticket write tools; got: {intro!r}"
    )
    assert "pull-request" in intro or "pull request" in intro, (
        f"expected intro to mention pull-request write tools; got: {intro!r}"
    )
    assert "every write tool" not in intro, (
        f"intro should not claim blanket 'every write tool' coverage; got: {intro!r}"
    )


def test_security_gate_table_existing_ticket_and_readonly_rows_survive():
    """Edge case: pre-existing ticket rows and read-only rows are
    untouched."""
    section = _permission_gating_section()
    assert "| `create_ticket`  | `permissions.create == true` AND token available |" in section
    assert "| `update_ticket`  | `permissions.modify == true` AND token available |" in section
    assert "| `add_comment`    | `permissions.modify == true` AND token available |" in section
    assert "| `list_tickets`   | read-only — no gate                 |" in section
    assert "| `get_ticket`     | read-only — no gate                 |" in section


def test_security_gate_table_pulls_rows_carry_token_available_clause():
    """Edge case: the new `pulls.*` rows follow the existing convention of
    an 'AND token available' clause, mirroring the ticket rows."""
    section = _permission_gating_section()
    for tool_name in ("create_pr", "update_pr", "add_pr_comment", "add_pr_review_comment", "submit_pr_review", "merge_pr"):
        rows = [
            line for line in section.splitlines()
            if line.strip().startswith(f"| `{tool_name}`")
        ]
        assert len(rows) == 1, f"expected exactly one row for {tool_name}: {rows!r}"
        assert "AND token available" in rows[0], (
            f"{tool_name} row missing 'AND token available' clause: {rows[0]!r}"
        )


# ---------------------------------------------------------------------------
# R5.1 — `_require_pulls_modify` message names review/vote submission
# ---------------------------------------------------------------------------


def test_require_pulls_modify_message_names_review_submission():
    """RED today: 'submitting reviews/votes' is absent from the message."""
    project = _denied_project()

    with pytest.raises(PermissionError) as excinfo:
        _require_pulls_modify(project)

    message = str(excinfo.value)
    assert "modifying pull requests" in message
    assert "adding PR comments" in message
    assert "submitting reviews/votes" in message, (
        f"expected 'submitting reviews/votes' in message; got: {message!r}"
    )


# ---------------------------------------------------------------------------
# R5.2 / R5.3 — both tool docstrings state the gate rationale
# ---------------------------------------------------------------------------


def test_add_pr_review_comment_docstring_states_gate_rationale():
    """RED today: the docstring only says "Requires the project's
    `pulls.modify` permission." with no rationale."""
    doc = _normalize_ws(_pull_tools["add_pr_review_comment"].__doc__ or "")
    assert "reviewing is treated as modifying the PR" in doc
    assert "no separate review flag" in doc


def test_submit_pr_review_docstring_states_gate_rationale():
    """RED today: rationale phrases absent. The survival assertions
    (existing permission sentence + vote mapping) already pass."""
    doc = _normalize_ws(_pull_tools["submit_pr_review"].__doc__ or "")
    assert "reviewing is treated as modifying the PR" in doc
    assert "no separate review flag" in doc
    # Survival checks — already pass today.
    assert "Requires the project's `pulls.modify` permission" in doc
    assert "+10" in doc
    assert "-10" in doc


# ---------------------------------------------------------------------------
# R5 edge-case coverage — end-to-end denied-project behaviour (may already
# pass; documents the no-regression baseline this docs-only change relies
# on).
# ---------------------------------------------------------------------------


def _register_pulls(monkeypatch: pytest.MonkeyPatch, project: ProjectConfig):
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv(f"TOKEN_{project.id.upper()}", "tok")

    stub = _StubMCP()
    pull_tools.register(stub)
    return stub.tools


def test_add_pr_review_comment_denied_project_returns_error_not_traceback(
    monkeypatch: pytest.MonkeyPatch,
):
    project = _denied_project()
    tools = _register_pulls(monkeypatch, project)

    out = tools["add_pr_review_comment"](
        project_id="acme", pr_id="1", body="nice",
        path="src/foo.py", line=10, commit_sha="deadbeef",
    )
    assert "error" in out, f"expected error dict, not a raised exception: {out}"
    assert "submitting reviews/votes" in out["error"]


def test_submit_pr_review_denied_project_returns_error_not_traceback(
    monkeypatch: pytest.MonkeyPatch,
):
    project = _denied_project()
    tools = _register_pulls(monkeypatch, project)

    out = tools["submit_pr_review"](project_id="acme", pr_id="1", state="approve")
    assert "error" in out, f"expected error dict, not a raised exception: {out}"
    assert "submitting reviews/votes" in out["error"]
