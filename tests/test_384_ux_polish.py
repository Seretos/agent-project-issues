"""Driving tests for work package #384's three independent UX-polish
fixes:

  R1. `create_ticket`/`update_ticket`'s `template_unknown` refusal names
      the rejected template string in its `hint`, so it no longer reads
      byte-identical to the `template_required` hint.
  R2. A provider 5xx error surfaced through `_safe` (single-project
      tools) or `bulk._error_message` (the bulk loop) carries a "retrying
      may be worthwhile" hint, appended the same way the existing 401
      auth hint is.
  R3. `search_projects`'s docstring states that an empty query is still
      capped by `limit` (default 10) and reports `truncated`.

Harness helpers for R1 are reused from `tests.test_307_ticket_templates`
(Route A/B registration, `_project`, `_FakeTemplateProvider`) — the same
precedent `tests/test_325_refusal_payload_size.py` follows.

Phase = tests: only RED driving tests + compile-level scaffolding here.
No production code (`tools/tickets.py`, `tools/_providers.py`,
`tools/projects.py`) is touched in this file.
"""
from __future__ import annotations

import re

import httpx
import pytest

from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import bulk as bulk_tools
from project_issues_plugin.tools import projects as project_tools

from tests.test_307_ticket_templates import (
    _install_mock,
    _project,
    _register_tools_with,
    _template_content_response,
)


# ===========================================================================
# R1 — template_unknown hint names the rejected template
# ===========================================================================


def _template_handler(req: httpx.Request) -> httpx.Response:
    resp = _template_content_response("acme/backend", req)
    if resp is not None:
        return resp
    raise AssertionError(f"unexpected request: {req.method} {req.url}")


@pytest.mark.parametrize("tool_name", ["create_ticket", "update_ticket"])
@pytest.mark.parametrize("rejected_template", ["nope", "also-not-a-template"])
def test_template_unknown_hint_names_rejected_template(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, rejected_template: str,
) -> None:
    """Driving test for R1. RED today: the `template_unknown` hint is the
    generic 'no template was resolved; pick one from the templates list
    below (or call list_ticket_templates), then retry: ...' text, which
    does not contain the rejected template string anywhere.

    Parametrized over two different rejected values (not just 'nope') so a
    hint hard-coded to `template "nope" ...` instead of actually echoing
    the caller's argument cannot pass (test-critic round 1, tautology::F2)."""
    tools = _register_tools_with(monkeypatch, _project())
    _install_mock(monkeypatch, _template_handler)

    if tool_name == "create_ticket":
        result = tools["create_ticket"](
            project_id="acme", title="Bug", body="### Description\nx\n",
            template=rejected_template,
        )
    else:
        # update_ticket resolves the ticket only when it needs to infer
        # a template from labels; an explicit `template=` always skips
        # inference, so no GET is needed here — the handler only serves
        # the template-listing routes.
        result = tools["update_ticket"](
            project_id="acme", ticket_id="42", body="### Description\nx\n",
            template=rejected_template,
        )

    assert result["state"] == "template_unknown"
    assert f'template "{rejected_template}"' in result["hint"], (
        f"expected the rejected template name quoted in the hint; got: {result['hint']!r}"
    )
    assert "list_ticket_templates" in result["hint"]
    assert 'template="<name>"' in result["hint"], (
        "the re-call must still carry the <name> placeholder, never a "
        f"fabricated template name; got: {result['hint']!r}"
    )


def test_template_unknown_hint_differs_from_template_required_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R1: the two hints must no longer be byte-identical,
    so an agent can tell the two refusal states apart without reading the
    raw `state` field. RED today: both hints are the exact same generic
    'no template was resolved; ...' string."""
    tools = _register_tools_with(monkeypatch, _project())
    _install_mock(monkeypatch, _template_handler)

    required_result = tools["create_ticket"](
        project_id="acme", title="Bug", body="### Description\nx\n",
    )
    unknown_result = tools["create_ticket"](
        project_id="acme", title="Bug", body="### Description\nx\n",
        template="nope",
    )

    assert required_result["state"] == "template_required"
    assert unknown_result["state"] == "template_unknown"
    assert required_result["hint"] != unknown_result["hint"], (
        "template_required and template_unknown hints must be distinguishable "
        f"without inspecting `state`; both were: {required_result['hint']!r}"
    )


def test_template_required_hint_text_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional edge-case coverage: the `template_required` hint keeps
    its existing generic wording — R1 only changes `template_unknown`.
    Already passes today (existing test_307 assertions cover the same
    ground); pinned here alongside R1's new tests for locality."""
    tools = _register_tools_with(monkeypatch, _project())
    _install_mock(monkeypatch, _template_handler)

    result = tools["create_ticket"](
        project_id="acme", title="Bug", body="### Description\nx\n",
    )

    assert result["state"] == "template_required"
    assert result["hint"].startswith("no template was resolved; ")
    assert "list_ticket_templates" in result["hint"]


# ===========================================================================
# R2 — 5xx provider errors carry a retry hint
# ===========================================================================


_5XX_PROVIDER_ERRORS = [
    pytest.param(GitHubError, 500, id="github_500"),
    pytest.param(GitHubError, 503, id="github_503"),
    pytest.param(GitLabError, 500, id="gitlab_500"),
    pytest.param(GitLabError, 503, id="gitlab_503"),
    pytest.param(AzureDevOpsError, 500, id="azuredevops_500"),
    pytest.param(AzureDevOpsError, 503, id="azuredevops_503"),
]


@pytest.mark.parametrize("error_cls, status", _5XX_PROVIDER_ERRORS)
def test_safe_5xx_appends_retry_hint(error_cls, status: int) -> None:
    """Driving test for R2 (`_safe`, single-project choke point). RED
    today: `_with_auth_hint` only special-cases `status == 401`, so a 5xx
    returns `str(exc)` unchanged from `_safe` — 'retry' never appears."""
    from project_issues_plugin.tools._providers import _safe

    exc = error_cls(status, "boom")

    def go():
        raise exc

    out = _safe(go)

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert message.startswith(str(exc)), (
        f"expected the message to start with str(exc); got: {message!r}"
    )
    assert message.count("retry") == 1, (
        f"expected the word 'retry' to appear exactly once; got: {message!r}"
    )
    assert " — " in message


def test_update_comment_5xx_carries_retry_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for R2, reproducing the ticket's observed symptom
    verbatim: a fake provider's `update_comment` raises
    `GitHubError(500, "Internal Server Error")`, which reaches `_safe`
    through `_rewrap_404` (a no-op pass-through for non-404 statuses).
    RED today: the tool-level error is the bare 'GitHub 500: Internal
    Server Error' with no retry text."""
    from lib_python_projects import ProjectConfig, ProjectsLoadResult
    from project_issues_plugin.tools import comments as comment_tools

    class _StubMCP:
        def __init__(self) -> None:
            self.tools = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorator

    class _Mock500Provider:
        def update_comment(self, project, token, comment_id, body, *, ticket_id=None):
            raise GitHubError(500, "Internal Server Error")

    project = ProjectConfig(
        id="acme", provider="github", path="acme/backend", token_env="TOKEN_ACME",
        permissions={"issues": {"create": True, "modify": True}},
    )

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _Mock500Provider())
    monkeypatch.setenv("TOKEN_ACME", "tok")

    stub = _StubMCP()
    comment_tools.register(stub)

    out = stub.tools["update_comment"](
        project_id="acme", comment_id="1", body="new body",
    )

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert message.startswith("GitHub 500: Internal Server Error")
    assert "retry" in message, f"expected a retry hint; got: {message!r}"


def test_bulk_error_message_5xx_appends_retry_hint() -> None:
    """Driving test for R2 (`bulk._error_message`, the bulk choke point).
    RED today: `_error_message` only special-cases 401 via
    `_PROVIDER_AUTH_HINTS`/`_with_auth_hint`, so a 5xx falls through to
    `str(exc)` with no retry text."""
    exc = AzureDevOpsError(503, "Service Unavailable")

    message = bulk_tools._error_message(exc)

    assert message.startswith(str(exc))
    assert "retry" in message, f"expected a retry hint; got: {message!r}"


def test_401_still_gets_only_the_auth_hint_no_retry_text() -> None:
    """Additional edge-case coverage: a 401 must still get exactly the
    auth-scope hint, with no retry text mixed in (401 is not in the 5xx
    range). Already passes today (existing test_266 coverage); pinned
    here alongside R2's new tests for locality."""
    from project_issues_plugin.tools._providers import _safe

    def go():
        raise GitHubError(401, "Bad credentials")

    out = _safe(go)
    assert "retry" not in out["error"]
    assert "repo" in out["error"]


def test_429_and_403_stay_byte_identical_to_str_exc() -> None:
    """Additional edge-case coverage: 429/403 (not 5xx) get no hint at
    all — byte-identical to `str(exc)`. Already passes today (existing
    test_266 `test_B_rate_limit_status_gets_no_hint` coverage)."""
    from project_issues_plugin.tools._providers import _safe

    out_429 = _safe(lambda: (_ for _ in ()).throw(GitHubError(429, "API rate limit exceeded")))
    assert out_429 == {"error": "GitHub 429: API rate limit exceeded"}

    out_403 = _safe(lambda: (_ for _ in ()).throw(AzureDevOpsError(403, "TF400813: forbidden")))
    assert out_403 == {"error": "Azure DevOps 403: TF400813: forbidden"}


def test_499_and_statusless_exception_get_no_hint() -> None:
    """Additional edge-case coverage: a status just outside the 5xx range
    (499) and an exception with no `.status` attribute at all get no
    retry hint. Already passes today for the status-less case (matches
    `_with_auth_hint`'s existing no-status guard); the 499 case is new
    coverage for R2's upper-bound exclusion (5xx is 500-599, not 499)."""
    from project_issues_plugin.tools._providers import _with_auth_hint

    class _FakeExcWithStatus:
        def __init__(self, status: int, message: str) -> None:
            self.status = status
            self._message = message

        def __str__(self) -> str:
            return self._message

    out_499 = _with_auth_hint(_FakeExcWithStatus(499, "weird status"), "some hint")
    assert out_499 == "weird status", (
        "expected 499 (just outside 500-599) to get no hint appended at "
        f"all, i.e. the bare message; got: {out_499!r}"
    )

    out_no_status = _with_auth_hint(RuntimeError("boom"), "some hint")
    assert out_no_status == "boom"


# ===========================================================================
# R3 — search_projects docstring documents the limit on an empty query
# ===========================================================================


class _StubMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _search_projects_doc() -> str:
    stub = _StubMCP()
    project_tools.register(stub)
    return stub.tools["search_projects"].__doc__ or ""


def _empty_query_bullet(doc: str) -> str:
    start = doc.find("Empty or whitespace-only query")
    assert start != -1, "expected the 'Empty or whitespace-only query' bullet in the docstring"
    end = doc.find("Non-empty query", start)
    assert end != -1, "expected a 'Non-empty query' bullet following it"
    return doc[start:end]


def test_search_projects_doc_empty_query_mentions_limit() -> None:
    """Driving test for R3. RED today: the empty-query bullet mentions
    `limit` only as a reason to prefer `search_projects` over
    `list_projects` ('a bounded `limit` over a large set', 'no `limit`') —
    never as a cap tied to the empty-query enumeration itself.

    A bare substring check for 'limit'/'10'/'truncated' would already pass
    today, since 'limit' is already present for an unrelated reason
    (test-critic round 1, tautology::F1) — and '10'/'truncated' could
    equally be satisfied by an unrelated or even contradictory sentence
    (e.g. 'results are never truncated'). So this asserts the specific
    combination the plan requires instead: the cap explicitly tied to a
    default of 10 ('capped by `limit` ... default 10'), plus the
    `truncated: true` result marker documented for the over-limit case —
    neither of which appears anywhere in the bullet today."""
    doc = _search_projects_doc()
    bullet = _empty_query_bullet(doc)

    assert re.search(r"capped by `?limit`?[^.]*default\s*(?:of\s*)?10", bullet, re.IGNORECASE), (
        "expected the empty-query enumeration's cap explicitly tied to "
        f"`limit` with a default of 10; got bullet: {bullet!r}"
    )
    assert "truncated: true" in bullet, (
        "expected the `truncated: true` result marker documented for an "
        f"over-limit empty query; got bullet: {bullet!r}"
    )


def test_search_projects_doc_no_longer_claims_returns_all_projects() -> None:
    """Additional edge-case coverage: the plan explicitly fixes the
    contradictory 'returns **all** projects' phrasing (it does not, once
    capped by `limit`). RED today: the bullet contains this exact phrase.

    Normalizes markdown emphasis markers and matches a family of synonymous
    phrasings ('returns/lists/enumerates all/every project(s)') rather than
    one exact spelling, so dropping just the `**` asterisks or rewording to
    e.g. 'lists every project' cannot silently pass (test-critic round 2,
    tautology::F2)."""
    doc = _search_projects_doc()
    bullet = _empty_query_bullet(doc)
    normalized = re.sub(r"[*`]", "", bullet).lower()

    assert not re.search(r"\b(returns|lists?|enumerates)\s+all\s+projects\b", normalized), (
        f"expected the contradictory 'returns all projects' phrasing to be "
        f"gone (even without markdown emphasis); got bullet: {bullet!r}"
    )
    assert not re.search(r"\b(returns|lists?|enumerates)\s+every\s+projects?\b", normalized), (
        f"expected no 'returns/lists every project(s)' synonym for the "
        f"contradictory claim either; got bullet: {bullet!r}"
    )


def test_search_projects_empty_query_actually_caps_at_default_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioural grounding for R3 (test-critic round 2, tautology::F1).
    Every other R3 test checks literal DOCSTRING TEXT; none of them prove
    the claim the docstring makes -- that an empty query really is capped
    at the default `limit` of 10 and reports `truncated: true` -- is
    actually true at runtime. This test closes that gap the way #366 did
    for its own docstring-vs-runtime gap (see
    tests/test_366_tool_doc_gaps.py::
    test_acceptance_criteria_is_empty_on_github_and_gitlab_providers): it
    exercises the real `search_projects` tool (not a docstring read) with
    12 fake projects -- more than the default `limit` -- and an empty
    query, and asserts the real returned values.

    Per the ticket, the runtime capping/truncation behaviour is already
    correct today; only the docstring was wrong. So unlike the driving
    test above, this one is expected to already be GREEN against the
    unfixed code -- it grounds the docstring-text assertions in runtime
    truth rather than adding a second RED requirement. See the change
    report for confirmation this passes pre-fix."""
    from lib_python_projects import ProjectConfig, ProjectsLoadResult

    projects = [
        ProjectConfig(id=f"proj{i:02d}", provider="github", path=f"org/proj{i:02d}")
        for i in range(12)
    ]

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=projects, state="ok", search_root="/tmp")

    monkeypatch.setattr(project_tools, "load_projects", fake_load_projects)
    stub = _StubMCP()
    project_tools.register(stub)

    result = stub.tools["search_projects"](query="")

    assert "error" not in result, result
    assert len(result["matches"]) == 10, (
        f"expected the default limit (10) to cap the empty-query "
        f"enumeration; got {len(result['matches'])} matches"
    )
    assert result["total"] == 12
    assert result["truncated"] is True
    assert "list_projects" in (result["hint"] or ""), (
        f"expected a hint pointing at list_projects; got: {result['hint']!r}"
    )
