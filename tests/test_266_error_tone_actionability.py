"""Tests for ticket #266 — error-message tone/actionability consistency.

Three behavioural requirements:

  A. The seven `_require_*` permission gates in `tools/_providers.py`
     stop asking the agent to relay a message ("Tell the user the
     project is configured without ...") and instead state the fact and
     a direct, non-retryable instruction — matching `_resolve`'s
     unknown-project voice (`_providers.py:81-83`): name the exact
     `permissions.<ns>.<flag>` config key and say this cannot be worked
     around from the tools, so the agent stops retrying and reports it.

  B. A raw 401 from any of the three providers, surfaced through
     `_safe`, picks up a provider-specific scope hint (GitHub: `repo`
     scope; GitLab: `api` scope; Azure DevOps: Work Items scope, plus a
     Build mention) appended to the original message — applied
     centrally via `_with_auth_hint(exc, hint) -> str`, wired into
     `_safe`'s three provider `except` blocks. Non-401 errors and bare
     `ProviderError` are untouched.

  C. (attempt 2, the sole remaining gap) The Azure DevOps Build mention
     in `_AZUREDEVOPS_AUTH_HINT` must not be conditioned on "triggering
     pipelines" — pipeline READ operations (`list_pipeline_runs`,
     `get_pipeline_run`, `get_pipeline_step_log`) hit the same
     `/_apis/build/*` surface without ever going through the
     trigger-only gate `_require_pipelines_trigger`, so the old "if
     triggering pipelines" phrasing understated when Build is actually
     needed. The fix stays a single fixed module constant applied to
     every Azure DevOps call — no per-call-site hint selection.

Mirrors the fake-provider-raises pattern used in `test_error_rewrap_251.py`
(pipeline tools) and `test_257_error_surface_consistency.py` (ticket
tools) — a mock provider is registered directly into
`providers_mod._PROVIDERS` so no HTTP mocking is needed.

Phase = tests (attempt 2): requirements A and B's production code is
already implemented and review-approved (commit b7c4cf9) — the tests
for them stay green throughout as a no-regression baseline. Requirement
C is the only new RED here: `_AZUREDEVOPS_AUTH_HINT` still reads
"...also Build if triggering pipelines" on the current code, so the new
`test_C_*` driving test below (and the updated assertions in
`tests/test_list_custom_fields.py`) fail against that stale wording.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import (
    IssuesPermissions,
    Permissions,
    ProjectConfig,
    ProjectsLoadResult,
)
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.base import ProviderError
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError
from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools import tickets as ticket_tools
from project_issues_plugin.tools._providers import (
    _require_board_manage,
    _require_issues_create,
    _require_issues_modify,
    _require_pipelines_trigger,
    _require_pulls_create,
    _require_pulls_merge,
    _require_pulls_modify,
    _require_token,
)


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _denied_project(project_id: str = "acme", provider: str = "github") -> ProjectConfig:
    """A project whose `Permissions()` default to all-False, so every
    `_require_*` gate is denied."""
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=f"{project_id}/backend",
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(),
    )


# ---------------------------------------------------------------------------
# Requirement A — permission-gate voice
# ---------------------------------------------------------------------------


_GATES = [
    pytest.param(_require_issues_create, "issues", "create", id="issues_create"),
    pytest.param(_require_issues_modify, "issues", "modify", id="issues_modify"),
    pytest.param(_require_pulls_create, "pulls", "create", id="pulls_create"),
    pytest.param(_require_pulls_modify, "pulls", "modify", id="pulls_modify"),
    pytest.param(_require_pulls_merge, "pulls", "merge", id="pulls_merge"),
    pytest.param(_require_board_manage, "board", "manage", id="board_manage"),
    pytest.param(_require_pipelines_trigger, "pipelines", "trigger", id="pipelines_trigger"),
]


@pytest.mark.parametrize("gate, namespace, flag", _GATES)
def test_gate_messages_are_direct_and_name_the_config_key(gate, namespace, flag) -> None:
    """Driving test for requirement A. Every gate's message must name the
    exact `permissions.<ns>.<flag>` config key, say this cannot be worked
    around from the tools and must be reported (not retried), and must NOT
    ask the agent to relay a message ("Tell the user ..."). RED today:
    all seven gates currently say "Tell the user the project is
    configured without <ns>.<flag> permission." with no
    `permissions.<ns>.<flag> is false in projects.yml` phrasing and no
    "report it to the user" signal."""
    project = _denied_project()

    with pytest.raises(PermissionError) as excinfo:
        gate(project)

    message = str(excinfo.value)
    assert (
        f"permissions.{namespace}.{flag} is false in projects.yml "
        "(or projects.yaml)" in message
    ), (
        f"expected the exact config key naming both accepted config "
        f"filenames in the message; got: {message!r}"
    )
    assert "report it to the user" in message, (
        "expected an explicit not-retryable/report-it signal "
        f"('report it to the user'); got: {message!r}"
    )
    assert "Tell the user" not in message, (
        f"gate message still asks the agent to relay a message: {message!r}"
    )


def test_A2_create_ticket_surfaces_new_gate_text_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: `create_ticket` against a project with issues.create=False
    surfaces the new gate text through `_safe`, not just at the unit level."""
    project = ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="TOKEN_ACME",
        permissions=Permissions(issues=IssuesPermissions(create=False)),
    )

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv("TOKEN_ACME", "tok")

    stub = _StubMCP()
    ticket_tools.register(stub)

    out = stub.tools["create_ticket"](project_id="acme", title="x")

    assert "error" in out, f"expected error dict; got: {out}"
    assert (
        "permissions.issues.create is false in projects.yml (or projects.yaml)"
        in out["error"]
    )
    assert "report it to the user" in out["error"]
    assert "Tell the user" not in out["error"]


def test_A2_ensure_board_column_surfaces_new_gate_text_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: `ensure_board_column` against a project with
    board.manage=False surfaces the new gate text through `_safe`."""
    project = ProjectConfig(
        id="acme",
        provider="github",
        path="acme/backend",
        token_env="TOKEN_ACME",
        permissions=Permissions(),
    )

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv("TOKEN_ACME", "tok")

    stub = _StubMCP()
    ticket_tools.register(stub)

    out = stub.tools["ensure_board_column"](project_id="acme", column_name="Approved")

    assert "error" in out, f"expected error dict; got: {out}"
    assert (
        "permissions.board.manage is false in projects.yml (or projects.yaml)"
        in out["error"]
    )
    assert "report it to the user" in out["error"]
    assert "Tell the user" not in out["error"]


def test_A4_require_token_message_unchanged() -> None:
    """`_require_token` is explicitly out of scope for #266 (already
    direct voice, names the env var) — pinned here as a no-regression
    baseline. Already passes today."""
    project = _denied_project()
    with pytest.raises(PermissionError) as excinfo:
        _require_token(project)
    assert "no API token" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Requirement B — provider-specific 401 scope hint
# ---------------------------------------------------------------------------


def _project(provider: str, project_id: str = "acme") -> ProjectConfig:
    # Azure DevOps validates `path` as 'organization/project/repository'
    # (3 segments) — GitHub/GitLab accept the generic 2-segment form used
    # everywhere else in this file; only azuredevops needs the longer shape.
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(
            issues=IssuesPermissions(create=True, modify=True),
        ),
    )


def _register_tickets(monkeypatch: pytest.MonkeyPatch, provider_instance, provider_key: str):
    project = _project(provider_key)

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv(f"TOKEN_{project.id.upper()}", "tok")
    monkeypatch.setitem(providers_mod._PROVIDERS, provider_key, provider_instance)

    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


def _register_pipelines(monkeypatch: pytest.MonkeyPatch, provider_instance, provider_key: str):
    project = _project(provider_key)

    def fake_load_projects(*_args, **_kwargs):
        return ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")

    monkeypatch.setattr(providers_mod, "load_projects", fake_load_projects)
    monkeypatch.setenv(f"TOKEN_{project.id.upper()}", "tok")
    monkeypatch.setitem(providers_mod._PROVIDERS, provider_key, provider_instance)

    stub = _StubMCP()
    pipeline_tools.register(stub)
    return stub.tools


class _MockGitHubProvider401:
    def update_ticket(self, project, token, ticket_id, **kwargs):
        raise GitHubError(401, "Bad credentials")


class _MockGitLabProvider401:
    def get_run(self, project, token, run_id, *, include_failure_excerpt=True):
        raise GitLabError(401, "401 Unauthorized")


def test_B_github_401_gets_scope_hint_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for requirement B (GitHub). A raw GitHub 401 surfaced
    through `update_ticket` picks up a hint naming the real GitHub scope
    token 'repo'. RED today: `_safe`'s GitHubError branch returns
    `str(exc)` unchanged, so no hint is appended."""
    tools = _register_tickets(monkeypatch, _MockGitHubProvider401(), "github")

    out = tools["update_ticket"](project_id="acme", ticket_id="5", title="new title")

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert message.startswith("GitHub 401: Bad credentials")
    assert "repo" in message, f"expected the GitHub 'repo' scope hint; got: {message!r}"


def test_B_gitlab_401_gets_scope_hint_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving test for requirement B (GitLab). A raw GitLab 401 surfaced
    through `get_pipeline_run` picks up a hint naming the real GitLab
    scope token 'api'. RED today: `_safe`'s GitLabError branch returns
    `str(exc)` unchanged."""
    tools = _register_pipelines(monkeypatch, _MockGitLabProvider401(), "gitlab")

    out = tools["get_pipeline_run"](project_id="acme", run_id="1234")

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert message.startswith("GitLab 401: 401 Unauthorized")
    assert "api" in message, f"expected the GitLab 'api' scope hint; got: {message!r}"


# B-azure lives in tests/test_list_custom_fields.py (extends the existing
# `test_list_custom_fields_non_404_error_passes_through_unchanged`, which
# already drives an AzureDevOpsError(401, ...) through `list_custom_fields`
# — see B2/B-azure notes there).


def test_B1_non_401_errors_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-regression pin: a non-401 provider error surfaces byte-identical
    to `str(exc)` — no hint appended. Already passes today (scoping to 401
    only is the whole point of the fix) and must keep passing."""

    class _MockGitHubProvider404:
        def update_ticket(self, project, token, ticket_id, **kwargs):
            raise GitHubError(404, "Not Found")

    tools = _register_tickets(monkeypatch, _MockGitHubProvider404(), "github")
    out = tools["update_ticket"](project_id="acme", ticket_id="5", title="new title")

    assert "error" in out, f"expected error dict; got: {out}"
    # `_rewrap_404` fires for update_ticket's bare 404 too, but the
    # rewritten message is still the exact rewrap text with no auth hint
    # tacked on — a non-401 status never picks up a scope hint at all.
    assert "GitHub 404" in out["error"]
    assert "repo" not in out["error"]


def test_B3_bare_provider_error_401_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-regression pin: a bare `ProviderError(401, ...)` (not one of the
    three concrete provider subclasses) is caught by `_safe`'s separate
    `except ProviderError` block, which #266's plan leaves untouched — no
    hint is appended there. Already passes today."""
    from project_issues_plugin.tools._providers import _safe

    def go():
        raise ProviderError(401, "generic unauthorized")

    out = _safe(go)
    assert out == {"error": "generic unauthorized"}


def test_B4a_with_auth_hint_no_status_attr_returns_plain_str() -> None:
    """Unit test for the new `_with_auth_hint` helper: an exception with no
    `.status` attribute (e.g. a plain `RuntimeError`) must return
    `str(exc)` unchanged, no `AttributeError`. RED today: `_with_auth_hint`
    does not exist yet (ImportError) — valid RED for a not-yet-written
    helper, per the plan's phase=tests instructions."""
    from project_issues_plugin.tools._providers import _with_auth_hint

    exc = RuntimeError("boom")
    assert _with_auth_hint(exc, "some hint") == "boom"


def test_B4b_with_auth_hint_401_appends_hint_exactly_once() -> None:
    """Unit test for `_with_auth_hint`: status == 401 appends the hint
    exactly once. RED today: `_with_auth_hint` does not exist yet
    (ImportError) — valid RED for a not-yet-written helper."""
    from project_issues_plugin.tools._providers import _with_auth_hint

    exc = GitHubError(401, "Bad credentials")
    out = _with_auth_hint(exc, "check your token's repo scope")

    assert out == "GitHub 401: Bad credentials — check your token's repo scope"
    assert out.count("check your token's repo scope") == 1


def test_B4c_401_through_rewrap_then_safe_gets_exactly_one_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 guard: a 401 fed through a `_rewrap_*` helper (all of which are
    no-op pass-throughs for a non-matching status) and then through
    `_safe` still yields exactly one hint — the rewrap identity
    pass-through doesn't double up or drop the hint. This exercises
    existing rewrap identity behavior (may already hold on the rewrap
    side) but the end-to-end hint presence is still RED today until
    `_safe` is wired."""
    tools = _register_tickets(monkeypatch, _MockGitHubProvider401(), "github")

    out = tools["update_ticket"](project_id="acme", ticket_id="5", title="new title")

    assert "error" in out, f"expected error dict; got: {out}"
    # Exactly one occurrence of the scope-hint marker character sequence
    # ' — ' (em dash separator used by `_with_auth_hint`) confirms the
    # hint was appended exactly once, not doubled by chained rewraps.
    assert out["error"].count(" — ") == 1, (
        f"expected exactly one hint separator; got: {out['error']!r}"
    )


def test_B_rate_limit_status_gets_no_hint() -> None:
    """Edge-case pin (not new behaviour): a rate-limit-flavored non-401
    status (GitHub 429, Azure DevOps 403) never picks up the auth-scope
    hint — only a literal 401 does. Already passes today; kept here as
    documentation of the non-overlap requirement rather than as its own
    RED-driving test."""
    from project_issues_plugin.tools._providers import _safe

    def go_github():
        raise GitHubError(429, "API rate limit exceeded")

    out = _safe(go_github)
    assert out == {"error": "GitHub 429: API rate limit exceeded"}

    def go_azure():
        raise AzureDevOpsError(403, "TF400813: forbidden")

    out = _safe(go_azure)
    assert out == {"error": "Azure DevOps 403: TF400813: forbidden"}


# ---------------------------------------------------------------------------
# Requirement C — Azure DevOps Build hint must cover reads, not just trigger
# ---------------------------------------------------------------------------


class _MockAzureDevOpsProvider401:
    def get_run(self, project, token, run_id, *, include_failure_excerpt=True):
        raise AzureDevOpsError(401, "401 Unauthorized")


def test_C_azure_401_on_pipeline_read_hint_covers_reads_not_only_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for requirement C (attempt 2's sole remaining gap): an
    Azure DevOps 401 raised from a pipeline READ (`get_pipeline_run`,
    which is token-gated only and never routes through
    `_require_pipelines_trigger`) must still state that Build is needed
    for pipeline operations generally — not phrased as conditional on
    triggering. RED today: `_AZUREDEVOPS_AUTH_HINT` still reads "...also
    Build if triggering pipelines", so the positive assertion below
    fails (the operation-scoped phrase is absent) and the negative
    assertion fails too (the stale trigger-conditional phrase is still
    present)."""
    tools = _register_pipelines(
        monkeypatch, _MockAzureDevOpsProvider401(), "azuredevops",
    )

    out = tools["get_pipeline_run"](project_id="acme", run_id="678")

    assert "error" in out, f"expected error dict; got: {out}"
    message = out["error"]
    assert message.startswith("Azure DevOps 401: 401 Unauthorized")
    assert "Build for any pipeline operation, read or trigger" in message, (
        f"expected the operation-scoped Build hint; got: {message!r}"
    )
    assert "if triggering pipelines" not in message, (
        "Build mention must not be conditioned on triggering pipelines "
        f"any more; got: {message!r}"
    )
