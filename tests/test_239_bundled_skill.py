"""Regression test for #239: the shipped `agent-project-issues` plugin

(0.2.2) served no skill, even though `skills/project-issues/SKILL.md`
existed in the repo. Root cause: `.github/workflows/release.yml` stages
the plugin payload twice (the install ZIP step and the orphan `release`
branch step) and neither copy included `skills/`, so it could never
reach an installed plugin (the marketplace dispatch serves exactly the
orphan-branch tree). Consequence: generic operating knowledge for
driving this MCP correctly (label catalog precondition, parallel-write
semantics, board-column mechanics) had to be duplicated into each
consuming project's `CLAUDE.md` instead of living in the bundled skill.

This ticket fixes two things:
  1. packaging — both release.yml staging steps now copy `skills/`, and
     `.claude-plugin/plugin.json` declares `"skills": "./skills"`
     (mirroring `agent-worktree`);
  2. content — `SKILL.md` is expanded to actually carry the three
     operational facts the ticket names, on top of the existing
     "what/entry point/behavioural rules" sections.

Follows the repo's per-ticket test-file convention (one new test file
per ticket number, `tests/test_237_label_catalog_docs.py` as the
`_StubMCP` / `register()` template) and the plain-text repo-file
assertion style of `tests/test_179_config_example_board.py` (no YAML
parser — PyYAML is not a declared test dependency).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

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


def _release_yml_text() -> str:
    path = _repo_root() / ".github" / "workflows" / "release.yml"
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Requirement 1 — the release artifact ships skills/.
# --------------------------------------------------------------------------


def test_release_workflow_stages_skills_into_zip_and_release_branch():
    text = _release_yml_text()
    assert 'cp -a skills "$STAGE/"' in text, (
        "ZIP staging step (STAGE) must copy skills/ so the install ZIP "
        "ships the bundled skill"
    )
    assert 'cp -a skills "$STAGE_DIR/"' in text, (
        "orphan release-branch staging step (STAGE_DIR) must copy skills/ "
        "so the marketplace dispatch tag ships the bundled skill"
    )


def test_skills_dir_is_not_gitignored():
    gitignore = _repo_root() / ".gitignore"
    if not gitignore.is_file():
        return
    text = gitignore.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        assert stripped not in {"skills", "skills/", "/skills", "/skills/"}, (
            f".gitignore excludes skills/ via line {line!r}"
        )


def test_build_script_packages_skills():
    """Already passes today — pins the local -Package build path that
    made the CI/local divergence go unnoticed in the first place."""
    text = (_repo_root() / "scripts" / "build.ps1").read_text(encoding="utf-8")
    assert '"skills"' in text
    assert "Copy-Item -Recurse -Force \"skills\" $stage" in text


# --------------------------------------------------------------------------
# Requirement 2 — the Claude manifest declares the skill.
# --------------------------------------------------------------------------


def test_claude_plugin_manifest_declares_skills():
    path = _repo_root() / ".claude-plugin" / "plugin.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["skills"] == "./skills"
    # Guard against the manifest expansion breaking the release `jq` stamp.
    assert data["name"] == "agent-project-issues"
    assert "version" in data
    assert "mcpServers" in data


def test_codex_manifest_untouched_and_valid_json():
    path = _repo_root() / ".codex-plugin" / "plugin.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "skills" not in data, (
        "codex manifest schema has no known skills contract; this ticket "
        "does not add one"
    )


# --------------------------------------------------------------------------
# Requirement 3 — the skill carries the label catalog precondition (#237).
# --------------------------------------------------------------------------


def test_skill_documents_label_catalog_precondition():
    text = _skill_text()
    for token in ("create_label", "create_ticket", "labels_add", "catalog", "GitHub"):
        assert token in text, f"SKILL.md missing {token!r}"
    assert re.search(r"azure devops", text, re.IGNORECASE) and re.search(
        r"freeform", text, re.IGNORECASE
    ), "SKILL.md must mention Azure DevOps' freeform tags"


def test_skill_label_claim_matches_tool_docstrings():
    """Drift guard for #248: SKILL.md's label-catalog section and both
    tool docstrings must agree that the catalog requirement is
    GitHub-only — none of the three may still claim (via the retracted
    "presumably GitLab" wording) that GitLab enforces a pre-existing
    catalog, and the skill text must say GitLab creates labels on the
    fly instead.
    """
    tools = _register(ticket_tools)
    # Whitespace-normalized so a line-wrapped "created\non the fly" still
    # matches the same way as an unwrapped occurrence would.
    skill_text = " ".join(_skill_text().split())
    assert "presumably" not in skill_text
    assert re.search(r"gitlab.{0,80}created on the fly", skill_text, re.IGNORECASE) or \
        re.search(r"created on the fly.{0,80}gitlab", skill_text, re.IGNORECASE), (
        "SKILL.md must describe GitLab as creating unknown labels on the fly"
    )
    for name in ("create_ticket", "update_ticket"):
        doc = " ".join((tools[name].__doc__ or "").split())
        assert "create_label" in doc
        assert "catalog" in doc
        assert "presumably" not in doc
        assert "GitLab" in doc


# --------------------------------------------------------------------------
# Requirement 4 — board-column mechanics.
# --------------------------------------------------------------------------


def test_skill_documents_board_column_two_step():
    text = _skill_text()
    for token in (
        "list_board_columns", "custom_fields", "Status", "update_ticket",
        "board.manage",
    ):
        assert token in text, f"SKILL.md missing {token!r}"


def test_board_tools_exist_with_those_names():
    """Already passes — pins that the tool names the skill references
    are actually registered by the server."""
    tools = _register(
        project_tools, ticket_tools, comment_tools, bulk_tools, pull_tools,
        pipeline_tools, relation_tools, label_tools,
    )
    for name in ("list_board_columns", "ensure_board_column", "update_ticket"):
        assert name in tools


# --------------------------------------------------------------------------
# Requirement 5 — parallel-write / conflict semantics.
# --------------------------------------------------------------------------


def test_skill_documents_parallel_write_and_conflict_semantics():
    text = _skill_text()
    assert re.search(r"parallel|concurrent", text, re.IGNORECASE)
    assert "different tickets" in text
    assert "error" in text
    assert re.search(r"not a race", text, re.IGNORECASE)
    assert re.search(r"do not blind-retry|don't blind-retry", text, re.IGNORECASE)


def test_error_surface_is_dict_not_exception():
    """Already passes — mirrors test_237's error-dict assertions; pins
    that the skill's "inspect {"error": ...}" claim reflects real
    behaviour rather than an aspiration."""
    tools = _register(ticket_tools)
    result = tools["create_ticket"](project_id="does-not-exist", title="x")
    assert "error" in result


# --------------------------------------------------------------------------
# Requirement 6 — the skill stays generic and internally consistent.
# --------------------------------------------------------------------------

_BACKTICK_TOKEN = re.compile(r"`([a-z][a-z0-9_]*)`")

_NON_TOOL_WHITELIST = {
    "custom_fields", "labels_add", "labels_remove", "off_board",
    "board_warning", "column_name", "project_id", "ticket_id", "kind",
    "target", "status", "status_field", "native", "logical", "option_id",
    "states", "is_split", "column", "columns", "error", "labels",
    "config_error", "board", "manage", "board.manage", "projects.yml",
    "projects.yaml", "hints.default_open",
    # Added by ticket #242 (pipeline/label/relation/custom-field prose):
    "branch", "tag", "commit_sha", "recent", "hint", "run_id", "failure",
    "conclusion", "log_excerpt", "mode", "around_failure", "max_lines",
    "annotations", "name", "new_name", "color", "description",
    "read_only_kinds", "mentions", "closed_by", "provider_support",
    "parent", "children", "relations_truncated", "work_item_type",
    "reference_name", "allowed_values", "job_id", "fields",
    # Added by ticket #245 (pipeline workflow/event/since filters and the
    # trigger_pipeline dispatch-then-poll flow):
    "api", "commit", "event", "false", "manual", "pull_request", "push",
    "run", "schedule", "since", "triggered", "wait_timeout_seconds",
    "workflow",
    # Added by ticket #262 (get_pipeline_step_log character-based hard cap):
    "max_chars", "tail",
}


def test_skill_mentions_only_real_tool_names():
    text = _skill_text()
    tools = _register(
        project_tools, ticket_tools, comment_tools, bulk_tools, pull_tools,
        pipeline_tools, relation_tools, label_tools,
    )
    known_tools = set(tools.keys())
    for token in set(_BACKTICK_TOKEN.findall(text)):
        if token in known_tools or token in _NON_TOOL_WHITELIST:
            continue
        # Anything left is a snake_case token that isn't a registered
        # tool and isn't on the non-tool whitelist. It may legitimately
        # be prose (e.g. a parameter name) rather than a tool reference;
        # only flag names that look like a tool call — i.e. verb-shaped
        # and not a known parameter/response-field name.
        assert token in known_tools or token in _NON_TOOL_WHITELIST, (
            f"SKILL.md backtick-quotes {token!r}, which is neither a "
            "registered tool name nor whitelisted as a non-tool identifier"
        )


def test_skill_documents_relation_direction():
    text = _skill_text()
    for token in ("add_relation", "list_relation_kinds", "blocks", "blocked_by"):
        assert token in text, f"SKILL.md missing {token!r}"
    assert re.search(r"ticket_id.{0,40}from", text) or re.search(
        r"from.{0,40}ticket_id", text
    ), "SKILL.md must document that ticket_id is the 'from' end of a relation"


def test_skill_has_no_project_specific_content():
    text = _skill_text()
    assert "sothis" not in text.lower()
    assert "Seretos/" not in text
    # Hardcoded ticket references (e.g. "#237") don't belong in generic
    # operating guidance meant to ship to every consumer.
    assert not re.search(r"#\d+", text)


def test_skill_frontmatter_intact():
    text = _skill_text()
    assert text.startswith("---\n")
    assert "name: project-issues" in text
    assert "Projektboard" in text
    assert "leg ein Ticket an" in text
