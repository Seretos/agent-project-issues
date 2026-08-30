"""Driving tests for ticket #303 — surface `permissions.verified`/
`permissions.reason` (the two `@computed_field`s the lib already computes
on `Permissions`) in `list_projects`/`search_projects` output, drop the
redundant top-level `permissions_probe_error`, and stop the 401 recovery
text from naming `token_available`/`token_error` as the verification
truth.

Phase = tests. None of this behaviour exists yet on the current code:

  - `_project_to_dict` (projects.py:184-256) hand-builds the `permissions`
    dict and never reads `p.permissions.verified` / `p.permissions.reason`
    — there is no `verified`/`reason` key under `permissions` at all.
  - `permissions_probe_error` is still a live top-level per-project key.
  - `_GITHUB_AUTH_HINT` / `_GITLAB_AUTH_HINT` / `_AZUREDEVOPS_AUTH_HINT`
    (_providers.py:597-614) still tell the agent to "check this project's
    token_env / token_available / token_error fields" after a 401 — those
    fields only prove a token string is present in the environment, never
    that the provider accepted it.
  - The `list_projects` / `search_projects` / module docstrings don't
    mention `permissions.verified`/`permissions.reason` and still document
    the soon-to-be-removed `permissions_probe_error`.

Every test below is RED against that code and is expected to go GREEN
only after the `implement` phase's changes to
`src/project_issues_plugin/tools/projects.py` and
`src/project_issues_plugin/tools/_providers.py`.

R6 (`_runtime_block` debug-only docs) and R7 (README/SECURITY wording)
are declared `none` in the plan (no observable behaviour) and have no
tests here.
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects.models import IssuesPermissions, Permissions
from lib_python_projects.providers.azuredevops import AzureDevOpsError
from lib_python_projects.providers.base import TokenCapabilities
from lib_python_projects.providers.github import GitHubError
from lib_python_projects.providers.gitlab import GitLabError

from project_issues_plugin.tools import _providers as providers_mod
from project_issues_plugin.tools import pipelines as pipeline_tools
from project_issues_plugin.tools import projects as proj_tools
from project_issues_plugin.tools import tickets as ticket_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _make_stub_tools(monkeypatch: pytest.MonkeyPatch, fake_result: ProjectsLoadResult) -> dict:
    """Register projects.py's tools against a stub MCP, wiring the stub
    `load_projects`. Mirrors `test_projects_diagnostics.py`'s helper of
    the same name."""
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)
    stub = _StubMCP()
    proj_tools.register(stub)
    return stub.tools


@pytest.fixture
def _clean_probe_cache():
    """The probe cache is process-global; reset it before AND after each
    test so order-of-execution can't leak state (mirrors
    test_projects_diagnostics.py's fixture of the same name)."""
    proj_tools._probe_cache_clear()
    yield
    proj_tools._probe_cache_clear()


# ---------------------------------------------------------------------------
# R1 — config-sourced projects report permissions.verified/reason in both
# list_projects and search_projects.
# ---------------------------------------------------------------------------


def _config_project(project_id: str = "acme") -> ProjectConfig:
    """A plain `source="config"` project with the library's default,
    never-probed `Permissions()` (verified=False, reason="not_probed")."""
    return ProjectConfig(
        id=project_id,
        provider="github",
        path=f"{project_id}/backend",
        token_env=f"TOKEN_{project_id.upper()}",
    )


def test_R1_list_projects_config_project_reports_verified_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R1 (list_projects side). A source='config' project
    must carry `permissions.verified: false` / `permissions.reason:
    "not_probed"` in its response entry (`fields="full"`).

    RED today: `_project_to_dict` never emits a `verified`/`reason` key
    under `permissions` at all — the dict is hand-built from
    issues/pulls/board sub-dicts only."""
    project = _config_project()
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"](fields="full")
    p = out["projects"][0]

    assert "verified" in p["permissions"], p["permissions"]
    assert p["permissions"]["verified"] is False
    assert p["permissions"]["reason"] == "not_probed"


def test_R1_search_projects_config_project_reports_verified_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R1 (search_projects side) — same field, same tool
    call shape as list_projects since both route through
    `_project_to_dict`. RED for the same reason as the list_projects
    case above."""
    project = _config_project()
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["search_projects"](query="", fields="full")
    m = out["matches"][0]

    assert "verified" in m["permissions"], m["permissions"]
    assert m["permissions"]["verified"] is False
    assert m["permissions"]["reason"] == "not_probed"


def test_R1_fields_light_still_omits_permissions_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional edge-case coverage (plan's R1 note): `fields="light"`
    never had a `permissions` key at all (`_project_to_light` returns
    only `id`/`provider`) — this must keep holding after R1's change.
    Already passing today; not a RED driving test, just a baseline pin."""
    project = _config_project()
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"](fields="light")
    p = out["projects"][0]
    assert "permissions" not in p


# ---------------------------------------------------------------------------
# R2 — verified/reason track the branch that produced the flags.
# ---------------------------------------------------------------------------


def _token_discovery_project(
    path: str = "acme/frontend", *, confirmed: bool, reason: str | None,
) -> ProjectConfig:
    from lib_python_projects.models import PullsPermissions

    return ProjectConfig(
        id="_td",
        description="Discovered via token",
        provider="github",
        path=path,
        token_env="GITHUB_TOKEN",
        source="token-discovery",
        permissions=Permissions.from_probe(
            issues=IssuesPermissions(create=True, modify=True),
            pulls=PullsPermissions(create=True, modify=True, merge=False),
            confirmed=confirmed,
            reason=reason,
        ),
    )


def _autodiscovered_project(path: str = "acme/backend") -> ProjectConfig:
    return ProjectConfig(
        id="_auto",
        description="Auto-discovered from git remote",
        provider="github",
        path=path,
        token_env="GITHUB_TOKEN",
        source="git-remote",
    )


def test_R2_token_discovery_project_reports_verified_true_when_confirmed(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    """Token-discovery projects carry a lib-stamped `Permissions` already
    — R1's plan says pass it through verbatim. A `Permissions.from_probe
    (confirmed=True, reason=None)` project must report
    `permissions.verified: true` / `permissions.reason: None`.

    (Per the plan-critic note forwarded in the plan: this verbatim pass-
    through is accepted even for the token-discovery path's own
    partial-but-confirmed asymmetry with the git-remote path — not
    re-litigated here.)

    RED today: no `verified`/`reason` key exists under `permissions`."""
    td = _token_discovery_project(confirmed=True, reason=None)
    fake_result = ProjectsLoadResult(projects=[td], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()
    p = out["projects"][0]
    assert "verified" in p["permissions"], p["permissions"]
    assert p["permissions"]["verified"] is True
    assert p["permissions"]["reason"] is None


def test_R2_git_remote_clean_probe_reports_verified_true_reason_none(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    """Auto-discovered (git-remote) project + token + a clean probe
    (`caps.reason is None`, the existing branch that already adopts the
    probe's flags) must report `permissions.verified: true` /
    `permissions.reason: None`.

    RED today: no `verified`/`reason` key exists under `permissions`."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")
    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(projects=[auto], state="ok", search_root="/tmp")

    class _FakeProvider:
        def probe_token_capabilities(self, project, token):
            return TokenCapabilities(
                issues_create=True, issues_modify=True,
                pulls_create=True, pulls_modify=True, pulls_merge=False,
                reason=None,
            )

    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _FakeProvider())
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()
    p = out["projects"][0]
    assert p["permissions_source"] == "token-probe"
    assert "verified" in p["permissions"], p["permissions"]
    assert p["permissions"]["verified"] is True
    assert p["permissions"]["reason"] is None
    # Test-critic note: pin the "iff" direction of the invariant too — a
    # clean probe (verified=True) must actually have adopted the probe's
    # issues/pulls flags into the output, not just report verified=True
    # while leaving the all-False default in place.
    assert p["permissions"]["issues"]["create"] is True
    assert p["permissions"]["issues"]["modify"] is True
    assert p["permissions"]["pulls"]["create"] is True
    assert p["permissions"]["pulls"]["modify"] is True
    assert p["permissions"]["pulls"]["merge"] is False


@pytest.mark.parametrize(
    "probe_reason",
    [
        pytest.param("repo_invisible_to_token", id="full_failure"),
        pytest.param("work_items_unavailable", id="partial_azure_failure"),
    ],
)
def test_R2_git_remote_failed_or_partial_probe_reports_verified_false(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache, probe_reason: str,
) -> None:
    """Auto-discovered (git-remote) project + token, but the probe did NOT
    come back fully clean (`caps.reason is not None` — both a total
    failure like `repo_invisible_to_token` and a partial, surface-
    specific failure like Azure's `work_items_unavailable` take this same
    branch) must report `permissions.verified: false` /
    `permissions.reason == caps.reason`, with the issues/pulls flags left
    at today's unchanged all-False default (the plan explicitly does NOT
    switch the flag-adoption predicate to `caps.confirmed`).

    RED today: no `verified`/`reason` key exists under `permissions`."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")
    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(projects=[auto], state="ok", search_root="/tmp")

    class _FailingProvider:
        def probe_token_capabilities(self, project, token):
            return TokenCapabilities(reason=probe_reason)

    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _FailingProvider())
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()
    p = out["projects"][0]
    assert p["permissions_source"] == "default"
    assert "verified" in p["permissions"], p["permissions"]
    assert p["permissions"]["verified"] is False
    assert p["permissions"]["reason"] == probe_reason
    # Flags unchanged from today's all-False default (no widening).
    assert p["permissions"]["issues"]["create"] is False
    assert p["permissions"]["pulls"]["merge"] is False


def test_R2_git_remote_no_token_reports_verified_false_reason_not_probed(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    """Auto-discovered (git-remote) project WITHOUT a usable token must
    never probe, and reports `permissions.verified: false` /
    `permissions.reason: "not_probed"` — sourced from the project's own
    (never-probed) `p.permissions.reason`, not from a probe result.

    RED today: no `verified`/`reason` key exists under `permissions`."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(projects=[auto], state="ok", search_root="/tmp")

    class _ExplodingProvider:
        def probe_token_capabilities(self, project, token):
            raise AssertionError("probe must not run without a token")

    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _ExplodingProvider())
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()
    p = out["projects"][0]
    assert p["permissions_source"] == "default"
    assert "verified" in p["permissions"], p["permissions"]
    assert p["permissions"]["verified"] is False
    assert p["permissions"]["reason"] == "not_probed"


def test_R2_provider_unsupported_fallback_reports_verified_false(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    """Additional edge-case coverage: no provider implementation at all
    (`_probe_capabilities`'s bare-constructed `TokenCapabilities(reason=
    "provider_unsupported")` fallback) must also report
    `permissions.verified: false` / `permissions.reason:
    "provider_unsupported"` — same branch as the failed/partial-probe
    case above, just a different reason string.

    RED today: no `verified`/`reason` key exists under `permissions`."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")
    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(projects=[auto], state="ok", search_root="/tmp")
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", object())
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()
    p = out["projects"][0]
    assert "verified" in p["permissions"], p["permissions"]
    assert p["permissions"]["verified"] is False
    assert p["permissions"]["reason"] == "provider_unsupported"


# ---------------------------------------------------------------------------
# R3 — permissions_probe_error is gone and no longer duplicated.
# ---------------------------------------------------------------------------


def test_R3_permissions_probe_error_key_absent_after_failed_probe(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    """The top-level `permissions_probe_error` key must be entirely absent
    from a project's entry — its information now lives on
    `permissions.reason`. Checked on the failed-probe case, the only
    scenario where the key was ever non-null.

    RED today: the key is currently present with value
    "repo_invisible_to_token"."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")
    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(projects=[auto], state="ok", search_root="/tmp")

    class _FailingProvider:
        def probe_token_capabilities(self, project, token):
            return TokenCapabilities(reason="repo_invisible_to_token")

    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _FailingProvider())
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()
    p = out["projects"][0]
    assert "permissions_probe_error" not in p, p
    # The same information now lives on permissions.reason.
    assert p["permissions"]["reason"] == "repo_invisible_to_token"


def test_R3_permissions_probe_error_key_absent_in_search_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same absence check via search_projects, on a plain config project
    (where the key was always None but still present as a key today).

    RED today: the key is present (value None) in the match dict."""
    project = _config_project()
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["search_projects"](query="")
    m = out["matches"][0]
    assert "permissions_probe_error" not in m, m


# ---------------------------------------------------------------------------
# R4 — 401 recovery text no longer names token_available/token_error as
# the verification truth.
# ---------------------------------------------------------------------------


_OLD_TOKEN_FIELDS_TAIL = "token_env / token_available / token_error fields"


def _hint_project(provider: str, project_id: str = "acme") -> ProjectConfig:
    # Azure DevOps validates `path` as 'organization/project/repository'
    # (3 segments); GitHub/GitLab accept the generic 2-segment form.
    path = "myorg/myproject/myrepo" if provider == "azuredevops" else f"{project_id}/backend"
    return ProjectConfig(
        id=project_id,
        provider=provider,
        path=path,
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=Permissions(issues=IssuesPermissions(create=True, modify=True)),
    )


def _register_tickets(monkeypatch: pytest.MonkeyPatch, provider_instance, provider_key: str):
    project = _hint_project(provider_key)
    monkeypatch.setattr(
        providers_mod, "load_projects",
        lambda *a, **k: ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp"),
    )
    monkeypatch.setenv(f"TOKEN_{project.id.upper()}", "tok")
    monkeypatch.setitem(providers_mod._PROVIDERS, provider_key, provider_instance)
    stub = _StubMCP()
    ticket_tools.register(stub)
    return stub.tools


def _register_pipelines(monkeypatch: pytest.MonkeyPatch, provider_instance, provider_key: str):
    project = _hint_project(provider_key)
    monkeypatch.setattr(
        providers_mod, "load_projects",
        lambda *a, **k: ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp"),
    )
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


class _MockAzureDevOpsProvider401:
    def get_run(self, project, token, run_id, *, include_failure_excerpt=True):
        raise AzureDevOpsError(401, "401 Unauthorized")


# Test-critic note 1 (forwarded in the plan): don't just assert
# `permissions.verified` is present — also assert `permissions.reason` is
# named, that the message actually states token_* fields are
# unverified/only-presence (not just the absence of the old literal
# clause, which a differently-worded but equally-misleading rewrite could
# dodge), and that the message never instructs the agent to *use*
# `token_available`/`token_error` as the verification check.
def _assert_verified_hint_shape(message: str) -> None:
    assert "permissions.verified" in message, message
    assert "permissions.reason" in message, message
    assert _OLD_TOKEN_FIELDS_TAIL not in message, message
    # Semantic caveat, not just a literal-string absence check: the
    # message must actually say token_available/token_error only prove a
    # token string is present, never that the provider accepted it.
    assert "only report whether a token string is present" in message, message
    assert "never that the provider accepted it" in message, message
    # Must not instruct the agent to *check* token_available/token_error
    # as the verification step (the old, misleading advice) — checked
    # semantically, not just via the one literal old string above: the
    # clause introducing those two field names must be the "only report
    # presence" caveat, never a "check ..." imperative.
    if "token_available" in message:
        before = message.split("token_available", 1)[0]
        assert not before.rstrip().endswith("check"), message
        assert "check this project's" not in before[-40:], message
    assert message.count(" — ") == 1, message


def test_R4_github_401_hint_names_permissions_verified_not_token_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R4 (GitHub). A raw GitHub 401, surfaced through
    `update_ticket` -> `_safe` -> `_with_auth_hint`, must name
    `permissions.verified` / `permissions.reason`, state the token_*
    fields are unverified, and drop the old "check token_env /
    token_available / token_error fields" clause, while the GitHub-
    specific `'repo' scope` phrase (pinned by test_266 requirement C /
    test_list_custom_fields.py) survives untouched. Exactly one hint
    separator.

    RED today: the tail still reads "...call list_projects and check
    this project's token_env / token_available / token_error fields"."""
    tools = _register_tickets(monkeypatch, _MockGitHubProvider401(), "github")
    out = tools["update_ticket"](project_id="acme", ticket_id="5", title="new title")

    assert "error" in out, out
    message = out["error"]
    _assert_verified_hint_shape(message)
    assert "'repo' scope" in message, message


def test_R4_gitlab_401_hint_names_permissions_verified_not_token_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R4 (GitLab). Same shape as the GitHub case above,
    via `get_pipeline_run`; the GitLab-specific `'api' scope` phrase must
    survive. RED for the same reason as the GitHub case."""
    tools = _register_pipelines(monkeypatch, _MockGitLabProvider401(), "gitlab")
    out = tools["get_pipeline_run"](project_id="acme", run_id="1234")

    assert "error" in out, out
    message = out["error"]
    _assert_verified_hint_shape(message)
    assert "'api' scope" in message, message


def test_R4_azuredevops_401_hint_names_permissions_verified_not_token_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R4 (Azure DevOps), via a pipeline READ
    (`get_pipeline_run`, token-gated only — never routes through
    `_require_pipelines_trigger`) so the Build mention's "read or
    trigger" phrasing (pinned by test_266 requirement C) is exercised
    too, alongside the new `permissions.verified` clause. RED for the
    same reason as the GitHub/GitLab cases."""
    tools = _register_pipelines(monkeypatch, _MockAzureDevOpsProvider401(), "azuredevops")
    out = tools["get_pipeline_run"](project_id="acme", run_id="678")

    assert "error" in out, out
    message = out["error"]
    _assert_verified_hint_shape(message)
    assert "Build for any pipeline operation, read or trigger" in message, message


# ---------------------------------------------------------------------------
# R5 — list_projects/search_projects/module docstrings document the two
# new fields and drop the stale permissions_probe_error mention.
# ---------------------------------------------------------------------------


def test_R5_list_projects_docstring_documents_new_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R5 (list_projects docstring — the contract shipped
    to agents). Must name `permissions.verified`, `permissions.reason`,
    the `"not_probed"` default, and the token_available-is-unverified
    caveat; must no longer name `permissions_probe_error`.

    RED today: none of `permissions.verified` / `permissions.reason` /
    `not_probed` appear in the docstring, and `permissions_probe_error`
    still does (projects.py:467-470)."""
    fake_result = ProjectsLoadResult(projects=[], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)
    doc = tools["list_projects"].__doc__

    assert "permissions.verified" in doc, doc
    assert "permissions.reason" in doc, doc
    assert "not_probed" in doc, doc
    assert "unverified" in doc, doc
    assert "permissions_probe_error" not in doc, doc
    # Test-critic note 3: "unverified" must be tied to token_available/
    # token_error specifically, not floating anywhere in the docstring
    # unrelated to them (e.g. describing something else as unverified).
    token_idx = doc.index("token_available")
    unverified_idx = doc.index("unverified")
    assert abs(unverified_idx - token_idx) < 250, (
        f"'unverified' (at {unverified_idx}) is not near "
        f"'token_available' (at {token_idx}) in the docstring: {doc!r}"
    )
    assert "token_error" in doc[token_idx:unverified_idx + 20], doc


def test_R5_search_projects_docstring_same_diagnostic_fields_paragraph_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving test for R5 (search_projects docstring). The "same
    diagnostic fields as list_projects" paragraph (projects.py:612-616)
    must be extended to mention the new fields too.

    RED today: `permissions.verified` does not appear anywhere in the
    docstring."""
    fake_result = ProjectsLoadResult(projects=[], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)
    doc = tools["search_projects"].__doc__

    assert "permissions.verified" in doc, doc


def test_R5_module_docstring_documents_new_fields_and_drops_probe_error() -> None:
    """Driving test for R5 (module docstring, projects.py:1-65). Must gain
    `permissions.verified`/`permissions.reason` documentation (including
    the `"not_probed"` default) and drop the `permissions_probe_error`
    mention.

    RED today: none of `permissions.verified` / `permissions.reason` /
    `not_probed` appear in the module docstring, and
    `permissions_probe_error` still does (projects.py:57-59)."""
    doc = proj_tools.__doc__
    assert "permissions.verified" in doc, doc
    assert "permissions.reason" in doc, doc
    assert "not_probed" in doc, doc
    assert "permissions_probe_error" not in doc, doc
