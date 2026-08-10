"""Regression test for #242: the shipped `project-issues` skill only

advertised and taught ticket/PR/comment work, even though the server
registers 35 tools across 8 modules — including CI/pipeline log
inspection, label-catalog CRUD, typed relations, sub-task hierarchy,
and board/custom-field discovery. Consequence: an agent would not
discover the skill for those tasks (the frontmatter `description:`
never named the trigger terms), and even after loading it the skill
explicitly punted ("Defer detail to tool schemas") instead of teaching
the non-obvious operational facts (drill-down chains, permission
gates, provider quirks) needed to use those tools correctly.

This ticket:
  1. extends the frontmatter `description:` with discovery triggers
     for the four previously-uncovered tool groups (pipeline/CI,
     label, relation/hierarchy, board column), bilingually;
  2. adds a "Tool map" index section naming every registered tool;
  3. adds deep operational sections for pipelines, labels, hierarchy,
     and custom fields, grounded in the real tool docstrings;
  4. rewords (does not delete) the "Defer detail to tool schemas"
     bullet.

Follows the repo's per-ticket test-file convention and copies the
`_StubMCP` / `_register` / `_repo_root` / `_skill_text` helpers from
`tests/test_239_bundled_skill.py` so this file is self-contained.

Also resolves the ticket's "duplicate SKILL.md" flag (see
`test_repo_has_exactly_one_skill_md` below): investigation found no
second `SKILL.md` and no `.claude/worktrees/` directory anywhere in
this worktree, and the path is not `.gitignore`-excluded — so it was
not tracked on `main` and the finding is not reproducible here. R8
pins that as a permanent guard rather than leaving it as an
unverified claim.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from lib_python_projects.providers.base import (
    READ_ONLY_RELATION_KINDS,
    WRITABLE_RELATION_KINDS,
)
from project_issues_plugin.tools import (
    bulk as bulk_tools,
    comments as comment_tools,
    labels as label_tools,
    pipelines as pipeline_tools,
    projects as project_tools,
    pulls as pull_tools,
    relations as relation_tools,
    tickets as ticket_tools,
)


class _StubMCP:
    """Minimal FastMCP stub that records registered tool callables."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register(*modules) -> dict[str, Callable]:
    stub = _StubMCP()
    for module in modules:
        module.register(stub)
    return stub.tools


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _skill_text() -> str:
    path = _repo_root() / "skills" / "project-issues" / "SKILL.md"
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _skill_path() -> Path:
    return _repo_root() / "skills" / "project-issues" / "SKILL.md"


# The same eight modules `server.main()` registers (see server.py).
_ALL_TOOL_MODULES = (
    project_tools, ticket_tools, comment_tools, bulk_tools, pull_tools,
    pipeline_tools, relation_tools, label_tools,
)

_BACKTICK_TOKEN = re.compile(r"`([a-z][a-z0-9_]*)`")


def _frontmatter_description() -> str:
    text = _skill_text()
    fences = [m.start() for m in re.finditer(r"^---\n", text, re.MULTILINE)]
    assert len(fences) >= 2, "SKILL.md must have a --- ... --- frontmatter block"
    return text[fences[0]:fences[1]]


# --------------------------------------------------------------------------
# R1 — frontmatter advertises the uncovered tool groups.
# --------------------------------------------------------------------------


def test_frontmatter_description_covers_all_tool_groups():
    description = _frontmatter_description().lower()
    groups = {
        "pipeline/CI": re.search(r"pipeline|\bci\b", description),
        "label": re.search(r"label", description),
        "relation": re.search(r"block(s|ed_by)|relation", description),
        "board column": re.search(r"board column|board-spalte", description),
        "sub-task/hierarchy": re.search(
            r"sub-task|unteraufgabe|hierarch", description
        ),
    }
    missing = [name for name, match in groups.items() if not match]
    assert not missing, f"frontmatter description missing trigger(s) for: {missing}"


def test_frontmatter_existing_triggers_preserved():
    description = _frontmatter_description()
    for token in (
        "ticket system", "issue tracker", "project board", "PR",
        "pull request", "merge request", "code review", "GitHub", "GitLab",
        "Azure DevOps", "Ticket", "Issue", "Bug", "Backlog", "Projektboard",
        "Pull Request", "Merge Request", "Code-Review", "leg ein Ticket an",
        "erstelle ein Issue", "welche Projekte gibt es", "öffne ein Issue",
        "zeig mir offene Tickets", "was sind die offenen PRs",
    ):
        assert token in description, f"frontmatter description lost {token!r}"


# --------------------------------------------------------------------------
# R2 — pipeline drill-down chain.
# --------------------------------------------------------------------------


def test_skill_documents_pipeline_triage_chain():
    text = _skill_text()
    for token in (
        "list_pipeline_runs", "get_pipeline_run", "get_pipeline_step_log",
        "failing_jobs", "job_id", "around_failure", "max_lines",
    ):
        assert token in text, f"SKILL.md missing {token!r}"
    assert re.search(r"exactly one", text, re.IGNORECASE), (
        "SKILL.md must document the 'exactly one addressing argument' rule"
    )
    assert re.search(r"gitlab.{0,80}annotations|annotations.{0,80}gitlab", text, re.IGNORECASE | re.DOTALL), (
        "SKILL.md must document that GitLab annotations are always empty"
    )


def test_skill_pipeline_claims_match_tool_docstrings():
    """Guards the skill's claims against drifting from the real
    docstrings — already passes given the tool source verified while
    writing this ticket's SKILL.md content."""
    tools = _register(pipeline_tools)
    for name in ("list_pipeline_runs", "get_pipeline_run", "get_pipeline_step_log"):
        assert name in tools
    run_doc = tools["get_pipeline_run"].__doc__ or ""
    assert "failing_jobs" in run_doc
    log_doc = tools["get_pipeline_step_log"].__doc__ or ""
    assert "around_failure" in log_doc
    assert "max_lines" in log_doc


# --------------------------------------------------------------------------
# R3 — label CRUD surface.
# --------------------------------------------------------------------------


def test_skill_documents_label_crud_surface():
    text = _skill_text()
    for token in ("list_labels", "update_label", "delete_label", "new_name"):
        assert token in text, f"SKILL.md missing {token!r}"
    assert re.search(r"lookup key", text, re.IGNORECASE), (
        "SKILL.md must document that `name` on update_label is a lookup "
        "key only, not a rename"
    )
    assert re.search(r"ededed", text) or re.search(r"6-hex|6-digit hex", text, re.IGNORECASE), (
        "SKILL.md must document GitHub's bare-hex color format"
    )
    assert re.search(r"#RRGGBB|#ff0000", text), (
        "SKILL.md must document GitLab's #RRGGBB color format"
    )
    assert re.search(
        r"azure devops.{0,120}not supported|not supported.{0,120}azure devops",
        text, re.IGNORECASE | re.DOTALL,
    ), "SKILL.md must document that label writes always error on Azure DevOps"


def test_label_write_tools_are_permission_gated_as_documented():
    """Already passes — pins that the three label-write tools really do
    require issues.modify, matching the skill's claim."""
    import inspect

    tools = _register(label_tools)
    for name in ("create_label", "update_label", "delete_label"):
        source = inspect.getsource(tools[name])
        assert "_require_issues_modify" in source


# --------------------------------------------------------------------------
# R4 — hierarchy and relation vocabulary.
# --------------------------------------------------------------------------


def test_skill_documents_hierarchy_and_relation_matrix():
    text = _skill_text()
    for token in (
        "list_hierarchy", "children", "relations_truncated",
        "read_only_kinds", "provider_support",
    ):
        assert token in text, f"SKILL.md missing {token!r}"
    assert re.search(
        r"never pass|read-only kind.{0,60}add_relation|add_relation.{0,60}read-only",
        text, re.IGNORECASE | re.DOTALL,
    ), "SKILL.md must state that read-only kinds can never be passed to add_relation"


def test_relation_kind_vocabulary_matches_lib():
    """Already passes — pins that the skill's writable/read-only kind
    claims match the lib's actual vocabulary."""
    text = _skill_text()
    for kind in WRITABLE_RELATION_KINDS:
        assert kind in text, f"SKILL.md missing writable relation kind {kind!r}"
    # At least one read-only kind example should be named (mirrors the
    # tool docstring's "e.g. mentions, closed_by" style).
    assert any(kind in text for kind in READ_ONLY_RELATION_KINDS), (
        "SKILL.md should name at least one read-only relation kind example"
    )


# --------------------------------------------------------------------------
# R5 — custom fields and board interaction.
# --------------------------------------------------------------------------


def test_skill_documents_custom_fields_and_board_interaction():
    text = _skill_text()
    for token in ("list_custom_fields", "reference_name", "work_item_type"):
        assert token in text, f"SKILL.md missing {token!r}"
    assert re.search(
        r"fields.{0,10}\[\].{0,150}(fact|not an error)|"
        r"(fact|not an error).{0,150}fields.{0,10}\[\]",
        text, re.IGNORECASE | re.DOTALL,
    ), "SKILL.md must document that empty fields=[] on GitHub/GitLab is a fact, not an error"
    assert "list_board_columns" in text and "Status" in text


# --------------------------------------------------------------------------
# R6 — coverage cannot drift again.
# --------------------------------------------------------------------------


def test_skill_tool_map_covers_every_registered_tool():
    tools = _register(*_ALL_TOOL_MODULES)
    expected = set(tools)
    documented = set(_BACKTICK_TOKEN.findall(_skill_text()))
    missing = sorted(expected - documented)
    assert not missing, (
        f"SKILL.md's Tool map section is missing these registered tools: "
        f"{missing} — every tool server.main() registers must be named "
        "(backtick-quoted) somewhere in SKILL.md."
    )


def test_tool_map_module_list_matches_server_registration():
    server_text = (
        _repo_root() / "src" / "project_issues_plugin" / "server.py"
    ).read_text(encoding="utf-8")
    module_names = {
        "projects": "project_tools", "tickets": "ticket_tools",
        "comments": "comment_tools", "bulk": "bulk_tools",
        "pulls": "pull_tools", "pipelines": "pipeline_tools",
        "relations": "relation_tools", "labels": "label_tools",
    }
    for alias in module_names.values():
        assert f"{alias}.register(mcp)" in server_text, (
            f"server.py no longer registers {alias} — _ALL_TOOL_MODULES "
            "in this test file must be kept in sync"
        )


# --------------------------------------------------------------------------
# R7 — LF line endings.
# --------------------------------------------------------------------------


def test_skill_uses_lf_line_endings():
    assert b"\r\n" not in _skill_path().read_bytes(), (
        "SKILL.md must use LF-only line endings — CRLF causes Claude Code "
        "to silently ignore the file"
    )


# --------------------------------------------------------------------------
# R8 — exactly one canonical SKILL.md.
# --------------------------------------------------------------------------


def test_repo_has_exactly_one_skill_md():
    """Ticket #242 flagged a duplicate copy at
    `.claude/worktrees/bump-lib-v0.2.3/skills/project-issues/SKILL.md`.
    Investigated and NOT reproducible: this worktree has no
    `.claude/worktrees/` directory at all (`.claude/` contains only
    `settings.json`), and the path is not `.gitignore`-excluded, so if
    it were tracked on `main` it would be materialized here. This test
    is an expected-green guard recording that finding, not a fix for a
    reproduced bug — a future regression (a real second SKILL.md
    landing on `main`) will turn it red.
    """
    matches = sorted(
        p for p in _repo_root().rglob("SKILL.md") if ".git" not in p.parts
    )
    assert len(matches) == 1, f"expected exactly one SKILL.md, found: {matches}"
    assert matches[0] == _repo_root() / "skills" / "project-issues" / "SKILL.md", (
        f"the sole SKILL.md moved to an unexpected location: {matches[0]}"
    )
