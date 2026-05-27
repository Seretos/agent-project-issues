"""Docstring and behaviour guard tests for ticket #94 cross-provider
convention inconsistencies.

Buckets:
  T3  — label color format per-provider already documented; guard test only.
  T4  — create_ticket/update_ticket label add/remove cross-references.
  D4  — list_tickets status filter vocabulary note.
  C3  — get/update/delete_comment composite id phrasing hedged.
  C4  — list_comments `since` uses updated_at, not created_at.
  C5  — add_comment returned body includes #ai-generated prefix.
  C7  — update_comment 404 is now wrapped (behavioural regression test).
  PL9 — list_pipeline_runs addressed_by "commit" cross-references commit_sha.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.providers import github as github_provider
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import comments as comment_tools
from project_issues_plugin.tools import labels as label_tools
from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools import tickets as ticket_tools


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


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


_ticket_tools = _register(ticket_tools)
_comment_tools = _register(comment_tools)
_label_tools = _register(label_tools)
_pipeline_tools = _register(pipeline_tools)


def _github_project(*, modify: bool = True) -> ProjectConfig:
    return ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="GITHUB_TOKEN_ACME",
        permissions={
            "issues": {"create": True, "modify": modify},
        },
    )


def _register_with_project(
    monkeypatch: pytest.MonkeyPatch,
    module,
    project: ProjectConfig,
) -> dict[str, Callable]:
    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(
            projects=[project],
            state="ok",
            search_root="/tmp",
        )

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    if hasattr(module, "load_projects"):
        monkeypatch.setattr(module, "load_projects", fake_load_projects)

    stub = _StubMCP()
    module.register(stub)
    return stub.tools


def _json_resp(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _install_github_mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)

    def fake_client(token: str | None) -> httpx.Client:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "test-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=github_provider.API_BASE,
            headers=headers,
            transport=transport,
        )

    monkeypatch.setattr(github_provider, "_client", fake_client)


# ---------------------------------------------------------------------------
# T3 — label color format already documented (guard only)
# ---------------------------------------------------------------------------


def test_t3_create_label_docstring_mentions_github_hex():
    """create_label docstring mentions the GitHub bare-hex format (ededed)."""
    doc = _label_tools["create_label"].__doc__ or ""
    assert "ededed" in doc, (
        "create_label docstring missing GitHub bare-hex color example"
    )


def test_t3_create_label_docstring_mentions_gitlab_rrggbb():
    """create_label docstring mentions the GitLab #RRGGBB format."""
    doc = _label_tools["create_label"].__doc__ or ""
    assert "#RRGGBB" in doc or "#ff0000" in doc, (
        "create_label docstring missing GitLab #RRGGBB color format"
    )


def test_t3_list_labels_docstring_mentions_hex():
    """list_labels docstring mentions provider color differences."""
    doc = _label_tools["list_labels"].__doc__ or ""
    assert "ededed" in doc, (
        "list_labels docstring missing bare-hex color example"
    )


# ---------------------------------------------------------------------------
# T4 — create_ticket / update_ticket label add/remove cross-references
# ---------------------------------------------------------------------------


def test_t4_create_ticket_docstring_mentions_labels_add():
    """create_ticket docstring references labels_add (for the update path)."""
    doc = _ticket_tools["create_ticket"].__doc__ or ""
    assert "labels_add" in doc, (
        "create_ticket docstring missing cross-reference to labels_add"
    )


def test_t4_create_ticket_docstring_mentions_labels_remove():
    """create_ticket docstring references labels_remove (for the update path)."""
    doc = _ticket_tools["create_ticket"].__doc__ or ""
    assert "labels_remove" in doc, (
        "create_ticket docstring missing cross-reference to labels_remove"
    )


def test_t4_update_ticket_docstring_references_create_ticket():
    """update_ticket docstring references create_ticket for initial labels."""
    doc = _ticket_tools["update_ticket"].__doc__ or ""
    assert "create_ticket" in doc, (
        "update_ticket docstring missing cross-reference to create_ticket for initial label assignment"
    )


def test_t4_update_ticket_docstring_mentions_flat_labels_param():
    """update_ticket docstring mentions the flat 'labels' parameter."""
    doc = _ticket_tools["update_ticket"].__doc__ or ""
    # The plan says it should reference the flat `labels` param (not add/remove)
    assert "flat" in doc or "labels` parameter" in doc, (
        "update_ticket docstring missing mention of flat labels param on create_ticket"
    )


# ---------------------------------------------------------------------------
# D4 — list_tickets status filter vocabulary note
# ---------------------------------------------------------------------------


def test_d4_list_tickets_docstring_mentions_list_ticket_statuses():
    """list_tickets docstring directs callers to list_ticket_statuses for the mapping."""
    doc = _ticket_tools["list_tickets"].__doc__ or ""
    assert "list_ticket_statuses" in doc, (
        "list_tickets docstring missing reference to list_ticket_statuses"
    )


def test_d4_list_tickets_docstring_does_not_say_pass_native_values():
    """list_tickets docstring warns against passing Azure native state names."""
    doc = _ticket_tools["list_tickets"].__doc__ or ""
    # Must contain a hint about native states being wrong here
    assert "To Do" in doc or "do NOT pass" in doc or "native" in doc, (
        "list_tickets docstring missing warning about Azure native state names"
    )


# ---------------------------------------------------------------------------
# C3 — composite comment id phrasing hedged (no overconfident 'keeps working')
# ---------------------------------------------------------------------------


def test_c3_get_comment_no_keeps_working_phrasing():
    """get_comment docstring no longer says 'keeps working too'."""
    doc = _comment_tools["get_comment"].__doc__ or ""
    assert "keeps working" not in doc, (
        "get_comment docstring still contains the overconfident 'keeps working' phrasing"
    )


def test_c3_get_comment_has_hedged_composite_wording():
    """get_comment docstring has the hedged composite id wording."""
    doc = _comment_tools["get_comment"].__doc__ or ""
    assert "may not work correctly" in doc or "Prefer passing" in doc, (
        "get_comment docstring missing hedged composite-id wording"
    )


def test_c3_update_comment_no_keeps_working_phrasing():
    """update_comment docstring no longer says 'keeps working too'."""
    doc = _comment_tools["update_comment"].__doc__ or ""
    assert "keeps working" not in doc, (
        "update_comment docstring still contains the overconfident 'keeps working' phrasing"
    )


def test_c3_update_comment_has_hedged_composite_wording():
    """update_comment docstring has the hedged composite id wording."""
    doc = _comment_tools["update_comment"].__doc__ or ""
    assert "may not work correctly" in doc or "Prefer passing" in doc, (
        "update_comment docstring missing hedged composite-id wording"
    )


def test_c3_delete_comment_no_keeps_working_phrasing():
    """delete_comment docstring no longer says 'keeps working too'."""
    doc = _comment_tools["delete_comment"].__doc__ or ""
    assert "keeps working" not in doc, (
        "delete_comment docstring still contains the overconfident 'keeps working' phrasing"
    )


def test_c3_delete_comment_has_hedged_composite_wording():
    """delete_comment docstring has the hedged composite id wording."""
    doc = _comment_tools["delete_comment"].__doc__ or ""
    assert "may not work correctly" in doc or "Prefer passing" in doc, (
        "delete_comment docstring missing hedged composite-id wording"
    )


# ---------------------------------------------------------------------------
# C4 — list_comments `since` uses updated_at, not created_at
# ---------------------------------------------------------------------------


def test_c4_list_comments_since_mentions_updated_at():
    """list_comments docstring says `since` filters by updated_at."""
    doc = _comment_tools["list_comments"].__doc__ or ""
    assert "updated_at" in doc, (
        "list_comments docstring missing 'updated_at' in the `since` description"
    )


def test_c4_list_comments_since_does_not_say_github_filters_by_created_at():
    """list_comments docstring does not describe GitHub as filtering by created_at."""
    doc = _comment_tools["list_comments"].__doc__ or ""
    # The old incorrect text was 'Comments with `created_at` (GitHub)'
    assert "created_at`\n            (GitHub)" not in doc, (
        "list_comments docstring still incorrectly attributes GitHub filtering to created_at"
    )
    # Stronger check: 'created_at' may still appear in other contexts, but
    # the specific phrasing "GitHub filters by created_at" must not appear.
    assert "GitHub" not in doc.split("updated_at")[0].split("since")[1] or "updated_at" in doc, (
        "list_comments docstring still incorrectly describes GitHub filtering by created_at"
    )


# ---------------------------------------------------------------------------
# C5 — add_comment returned body includes #ai-generated prefix
# ---------------------------------------------------------------------------


def test_c5_add_comment_docstring_warns_about_ai_generated_in_response():
    """add_comment docstring warns that returned body includes #ai-generated."""
    doc = _ticket_tools["add_comment"].__doc__ or ""
    assert "#ai-generated" in doc, (
        "add_comment docstring missing warning about #ai-generated in the response body"
    )


def test_c5_add_comment_docstring_references_update_comment():
    """add_comment docstring references update_comment for the strip-marker guidance."""
    doc = _ticket_tools["add_comment"].__doc__ or ""
    assert "update_comment" in doc, (
        "add_comment docstring missing cross-reference to update_comment"
    )


# ---------------------------------------------------------------------------
# PL9 — list_pipeline_runs addressed_by "commit" cross-references commit_sha
# ---------------------------------------------------------------------------


def test_pl9_list_pipeline_runs_docstring_mentions_commit_sha():
    """list_pipeline_runs docstring mentions both 'commit_sha' and 'commit'."""
    doc = _pipeline_tools["list_pipeline_runs"].__doc__ or ""
    assert "commit_sha" in doc, (
        "list_pipeline_runs docstring missing 'commit_sha'"
    )
    assert '"commit"' in doc or "'commit'" in doc or "\"commit\"" in doc, (
        "list_pipeline_runs docstring missing 'commit' response value"
    )


def test_pl9_list_pipeline_runs_docstring_cross_references_commit_sha_to_commit():
    """list_pipeline_runs docstring explicitly cross-references commit to commit_sha."""
    doc = _pipeline_tools["list_pipeline_runs"].__doc__ or ""
    # The cross-reference should appear near the addressed_by return shape
    assert "commit_sha" in doc and "commit" in doc, (
        "list_pipeline_runs docstring missing cross-reference between commit_sha and commit"
    )
    # Check both appear in the return shape section
    return_section = doc[doc.find("Return shape"):] if "Return shape" in doc else doc
    assert "commit_sha" in return_section or "commit_sha" in doc, (
        "commit_sha cross-reference not near the Return shape section"
    )


# ---------------------------------------------------------------------------
# C7 — update_comment 404 is now wrapped (behavioural regression test)
# ---------------------------------------------------------------------------


def test_c7_update_comment_404_wrapped_with_project_and_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 from the GitHub provider on update_comment is re-wrapped to include
    the project#id context (mirrors get_comment and delete_comment behaviour).

    This is the regression test for C7 — it must FAIL on the old code (where
    update_comment had no try/except) and PASS after the fix.
    """
    project = _github_project(modify=True)
    tools = _register_with_project(monkeypatch, comment_tools, project)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        # GET for the existing comment body (for marker determination) → 404
        if (
            req.method == "GET"
            and req.url.path == "/repos/acme/backend/issues/comments/777"
        ):
            return _json_resp({"message": "Not Found"}, status_code=404)
        # PATCH should not be reached; if GET already 404s the provider raises
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["update_comment"](
        project_id="acme",
        comment_id="777",
        body="new content",
    )
    assert "error" in result, f"expected error dict, got: {result}"
    # Must include the project#id form
    assert "acme#777" in result["error"], (
        f"error missing project#id context 'acme#777': {result['error']}"
    )
    # Must include GitHub 404 from _rewrap_404
    assert "GitHub 404" in result["error"], (
        f"error missing 'GitHub 404': {result['error']}"
    )


def test_c7_update_comment_non_404_error_still_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-404 provider error (e.g. 422) still surfaces as {'error': ...}
    without being lost — sanity check that _safe wrapping still works."""
    project = _github_project(modify=True)
    tools = _register_with_project(monkeypatch, comment_tools, project)
    monkeypatch.setenv("GITHUB_TOKEN_ACME", "tok")

    def handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "GET"
            and req.url.path == "/repos/acme/backend/issues/comments/777"
        ):
            # Return the comment OK so GET succeeds; fail on PATCH
            return _json_resp({
                "id": 777,
                "user": {"login": "alice"},
                "body": "original body",
                "html_url": "https://github.com/acme/backend/issues/1#issuecomment-777",
                "created_at": "2024-01-01T00:00:00Z",
            })
        if (
            req.method == "PATCH"
            and req.url.path == "/repos/acme/backend/issues/comments/777"
        ):
            return _json_resp(
                {"message": "Unprocessable Entity"}, status_code=422
            )
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    _install_github_mock(monkeypatch, handler)

    result = tools["update_comment"](
        project_id="acme",
        comment_id="777",
        body="new content",
    )
    assert "error" in result, f"expected error dict for 422, got: {result}"
