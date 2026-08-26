"""Driving tests for WP #291's documentation requirements (R5a, R5b, R5c,
R6, R9, R11, R12) plus R8's new README-parity test.

Doc requirements are exempt from a strict red->green TDD *loop* per test
(they aren't behavioural), but the plan still requires each to start RED
against the unmodified docstrings/README and turn GREEN after the
documented edit — so every assertion here is checked against today's
(unedited) source and is expected to fail for a "missing fragment"
reason, never an import/collection error.

Reuses the `_StubMCP` / `_register` / `_param_description` pattern from
`tests/test_tool_schema_descriptions.py` (schema-level Field descriptions)
and `tests/test_242_skill_full_tool_coverage.py` (`_repo_root`, README/SKILL
file reads).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from mcp.server.fastmcp.utilities.func_metadata import func_metadata

from project_issues_plugin.tools import pulls as pull_tools
from project_issues_plugin.tools import tickets as ticket_tools


class _StubMCP:
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


def _param_description(fn: Callable, param: str) -> str:
    """Return the Field description for a parameter, or '' if absent."""
    schema = func_metadata(fn).arg_model.model_json_schema()
    prop = schema.get("properties", {}).get(param, {})
    return prop.get("description", "")


def _norm(text: str) -> str:
    """Whitespace-normalise a docstring so wrapped multi-line prose can be
    matched with a single contiguous substring."""
    return " ".join(text.split())


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# R5a — `add_pr_review_comment`'s `line`/`path` Field descriptions (#287 f.2)
# ---------------------------------------------------------------------------


def test_add_pr_review_comment_line_field_documents_diff_membership():
    """Driving test for R5a. RED today: the `line` Field description
    contains neither "PR's diff" nor "list_pr_files"."""
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "line")
    assert "appears in the PR's diff" in desc, f"missing diff-membership clause: {desc!r}"
    assert "list_pr_files" in desc, f"missing list_pr_files pointer: {desc!r}"
    assert "start..end" in desc, f"missing start..end phrasing: {desc!r}"
    # The existing text must survive verbatim.
    assert (
        "absolute file line number (1-based), NOT a diff-hunk position" in desc
    ), f"existing text must be preserved verbatim: {desc!r}"


def test_add_pr_review_comment_path_field_names_list_pr_files():
    """Edge case for R5a: `path`'s Field description also names `list_pr_files`."""
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "path")
    assert "list_pr_files" in desc, f"missing list_pr_files pointer: {desc!r}"


# ---------------------------------------------------------------------------
# R5b — `list_pr_files`'s docstring: LEFT/RIGHT + tri-state
# ---------------------------------------------------------------------------


def test_list_pr_files_docstring_qualifies_sides_and_tri_state():
    """Driving test for R5b. RED today: `list_pr_files` doesn't exist yet
    (`KeyError: 'list_pr_files'`)."""
    tools = _register(pull_tools)
    doc = tools["list_pr_files"].__doc__ or ""
    norm = _norm(doc)
    for token in (
        "RIGHT", "LEFT", "pre-change", "inclusive", "add_pr_review_comment",
        "GitLab", 'side="RIGHT"', 'side="LEFT"',
        "binary", "cannot supply positions at all",
    ):
        assert token in norm, f"list_pr_files docstring missing {token!r}"
    # These three must appear verbatim (not whitespace-normalised) per the plan.
    for literal in ("line_ranges: []", "line_ranges: null", "patch: null"):
        assert literal in doc, f"list_pr_files docstring missing verbatim {literal!r}"
    assert "start+" not in norm, "must not use the incorrect 'start+' phrasing"


def test_list_pr_files_docstring_names_supports_flag_and_patch_knobs():
    """Edge case for R5b: names `supports_line_ranges`, Azure DevOps, and the patch knobs."""
    tools = _register(pull_tools)
    doc = tools["list_pr_files"].__doc__ or ""
    for token in ("supports_line_ranges", "Azure DevOps", "include_patch", "patch_max_chars"):
        assert token in doc, f"list_pr_files docstring missing {token!r}"


# ---------------------------------------------------------------------------
# R5c — comment-tool family cross-references, on explicitly named surfaces
# ---------------------------------------------------------------------------


def test_add_pr_comment_doc_already_names_add_pr_review_comment():
    """Already-passing guard: `add_pr_comment` already cross-references
    `add_pr_review_comment` today — this plan doesn't touch that clause."""
    tools = _register(pull_tools)
    doc = tools["add_pr_comment"].__doc__ or ""
    assert "add_pr_review_comment" in doc


def test_add_pr_comment_doc_names_add_comment_by_name():
    """Driving test for R5c. RED today: `add_pr_comment`'s docstring
    does not name `add_comment(` — supplied by 6c's aliasing paragraph."""
    tools = _register(pull_tools)
    doc = tools["add_pr_comment"].__doc__ or ""
    assert "add_comment(" in doc


def test_add_comment_doc_names_add_pr_comment_by_name():
    """Driving test for R5c. RED today: `add_comment`'s (tickets.py)
    docstring does not name `add_pr_comment` — supplied by 6c."""
    tools = _register(ticket_tools)
    doc = tools["add_comment"].__doc__ or ""
    assert "add_pr_comment" in doc


def test_add_pr_review_comment_line_field_names_list_pr_files():
    """Driving test for R5c (the Field-description leg, distinct from the
    docstring legs above): the `line` Field — NOT the docstring — names
    `list_pr_files`. Overlaps with test_add_pr_review_comment_line_field_
    documents_diff_membership in R5a; kept here too since R5c names it as
    its own numbered surface."""
    tools = _register(pull_tools)
    desc = _param_description(tools["add_pr_review_comment"], "line")
    assert "list_pr_files" in desc


def test_add_pr_review_comment_doc_already_names_add_pr_comment():
    """Already-passing guard: `add_pr_review_comment`'s docstring already
    names `add_pr_comment` today."""
    tools = _register(pull_tools)
    doc = tools["add_pr_review_comment"].__doc__ or ""
    assert "add_pr_comment" in doc


# ---------------------------------------------------------------------------
# R6 — `update_pr` documents atomicity and base-retarget staleness
# ---------------------------------------------------------------------------


def test_update_pr_docstring_documents_atomicity():
    """Driving test for R6 (atomicity leg), corrected per code review: the
    docstring must not claim a blanket "all-or-nothing" call across every
    provider (GitHub's own lib docstring literally says "Not atomic" — it
    applies title/body/base/status, labels, assignees, reviewers, and draft
    as separate sequential calls with no rollback once label validation
    passes). It must instead describe accurate per-provider behavior: a bad
    `labels_add` name fails before any write on GitHub, GitLab is close to
    atomic because it bundles fields into one request, and Azure DevOps has
    no pre-write label check at all — plus a recommendation to re-run
    `get_pr` after any failure."""
    tools = _register(pull_tools)
    doc = tools["update_pr"].__doc__ or ""
    norm = _norm(doc)
    # The old blanket claim must be gone.
    assert "all-or-nothing on every provider" in norm, (
        "update_pr docstring must open the atomicity paragraph by rejecting "
        "a blanket all-or-nothing claim"
    )
    assert "is all-or-nothing:" not in norm, (
        "update_pr docstring must not claim every provider is unconditionally "
        "all-or-nothing"
    )
    for token in (
        "labels_add", "title",
        "GitHub", "no rollback",
        "GitLab", "close to atomic",
        "Azure DevOps", "no equivalent",
        "get_pr",
    ):
        assert token in norm, f"update_pr docstring missing {token!r}"
    assert "`status` accepts" in norm
    assert norm.index("all-or-nothing on every provider") < norm.index("`status` accepts"), (
        "atomicity paragraph must be positioned before the status paragraph"
    )


def test_update_pr_docstring_documents_base_retarget_staleness():
    """Driving test for R6 (base-retarget leg). RED today: no `base` prose
    exists in `update_pr`'s docstring at all."""
    tools = _register(pull_tools)
    doc = tools["update_pr"].__doc__ or ""
    norm = _norm(doc)
    for token in ("base", "re-target", "stale", "list_pr_files"):
        assert token in norm, f"update_pr docstring missing {token!r}"
    idx = norm.find("stale")
    assert idx != -1
    window = norm[max(0, idx - 300): idx + 300]
    for token in ("path", "line", "commit_sha"):
        assert token in window, f"base-retarget sentence missing {token!r}: {window!r}"


def test_update_pr_docstring_preserves_existing_paragraphs():
    """Non-regression guard for R6: the pre-existing summary line and
    `draft` paragraph survive verbatim. Already passes today."""
    tools = _register(pull_tools)
    doc = tools["update_pr"].__doc__ or ""
    assert "Update an existing pull request. Only specified fields change." in doc
    assert "`draft` toggles the PR's draft state." in doc


# ---------------------------------------------------------------------------
# R9 — GitHub comment-API aliasing lands in BOTH docstrings (#281 finding 1)
# ---------------------------------------------------------------------------


def test_add_pr_comment_documents_github_id_space_aliasing():
    """Driving test for R9 (add_pr_comment leg). RED today: no aliasing
    paragraph exists yet."""
    tools = _register(pull_tools)
    doc = tools["add_pr_comment"].__doc__ or ""
    for token in (
        "GitHub", "/issues/{n}/comments", "silently", "NOT portable",
        "GitLab", "Azure DevOps",
    ):
        assert token in doc, f"add_pr_comment docstring missing {token!r}"
    assert ("id space" in doc) or ("id-space" in doc), (
        "add_pr_comment docstring missing 'id space'/'id-space' phrasing"
    )


def test_add_comment_documents_github_id_space_aliasing():
    """Driving test for R9 (add_comment leg, tickets.py). RED today: no
    aliasing paragraph exists yet."""
    tools = _register(ticket_tools)
    doc = tools["add_comment"].__doc__ or ""
    for token in (
        "GitHub", "/issues/{n}/comments", "silently", "NOT portable",
        "GitLab", "Azure DevOps",
    ):
        assert token in doc, f"add_comment docstring missing {token!r}"
    assert ("id space" in doc) or ("id-space" in doc), (
        "add_comment docstring missing 'id space'/'id-space' phrasing"
    )


def test_aliasing_paragraphs_do_not_leak_ticket_number():
    """Guard (docstring hygiene): neither aliasing paragraph should cite
    the internal ticket number. Passes today (no paragraph exists yet)
    and must keep passing after the fix."""
    pull_tools_reg = _register(pull_tools)
    ticket_tools_reg = _register(ticket_tools)
    for doc in (
        pull_tools_reg["add_pr_comment"].__doc__ or "",
        ticket_tools_reg["add_comment"].__doc__ or "",
    ):
        assert "#281" not in doc
        assert "ticket #281" not in doc.lower()


# ---------------------------------------------------------------------------
# R11 — `create_pr`'s "do not pre-inspect" scoped to create-time
# ---------------------------------------------------------------------------


def test_create_pr_docstring_scopes_no_preinspect_to_create_time():
    """Driving test for R11. RED today: the prohibition is unqualified —
    no `list_pr_files` / "Once the PR exists" clause exists."""
    tools = _register(pull_tools)
    doc = tools["create_pr"].__doc__ or ""
    norm = _norm(doc)
    assert "list_pr_files" in norm
    assert "Once the PR exists" in norm
    # The original prohibition must survive verbatim.
    assert "DO NOT pre-inspect the" in norm


# ---------------------------------------------------------------------------
# R12 — `create_pr` warns a GitLab zero-diff MR is unmergeable
# ---------------------------------------------------------------------------


def test_create_pr_docstring_documents_gitlab_zero_diff_unmergeable():
    """Driving test for R12. RED today: none of the four required
    fragments exist in `create_pr`'s docstring."""
    tools = _register(pull_tools)
    doc = tools["create_pr"].__doc__ or ""
    norm = _norm(doc)
    for token in ("cannot be merged", "real diff", "merge_pr", "commits_status"):
        assert token in norm, f"create_pr docstring missing {token!r}"
    # Pre-existing zero-diff tokens must survive verbatim, and the new
    # sentence must come after them.
    for token in ("422", "No commits between", "ahead of"):
        assert token in norm, f"create_pr docstring lost existing token {token!r}"
    assert norm.index("No commits between") < norm.index("cannot be merged")


# ---------------------------------------------------------------------------
# R8 (new test) — README documents list_pr_files as read-only
# ---------------------------------------------------------------------------


def test_readme_documents_list_pr_files():
    """Driving test for R8's README leg. RED today: README.md doesn't
    mention `list_pr_files` at all."""
    text = (_repo_root() / "README.md").read_text(encoding="utf-8")
    assert "list_pr_files" in text, "README.md must document list_pr_files"
    idx = text.index("list_pr_files")
    window = text[max(0, idx - 200): idx + 400]
    assert "read-only" in window, (
        "README.md's list_pr_files entry must note it needs no pulls.* permission"
    )
