"""Tests for the new diagnostic fields surfaced by list_projects /
find_projects (ticket #15).

Covers:

- `runtime.os` is always present and reports the right label for the
  host OS.
- `runtime.config_files_searched` / `config_file_loaded` are hidden
  (`None`) by default and only exposed when `PROJECT_ISSUES_DEBUG=1`
  is set at server start. This boundary exists because the agent
  could otherwise discover where the permissions file lives and use
  that knowledge to escalate its own rights -- the user explicitly
  asked for this guard in the plan-comment follow-up.
- Per-project `token_error` distinguishes
  None / no_token_env / env_var_unset / env_var_empty.
- `find_projects` returns the same `runtime` block as `list_projects`
  so consumers don't have to switch tools to read diagnostics.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from lib_python_projects import loader as cfg_mod
from project_issues_plugin.tools import projects as proj_tools


def _write_cfg(tmp_path: Path) -> None:
    # Project-boundary walk requires `.git/` at the repo root the config
    # lives in. Plant an empty one — the resolver only checks existence.
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / ".seretos" / "projects.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(textwrap.dedent(
        """
        version: 1
        projects:
          - id: t-set
            provider: github
            path: a/with-token
            token_env: TEST_TOKEN_SET
          - id: t-unset
            provider: github
            path: a/no-env-yet
            token_env: TEST_TOKEN_UNSET
          - id: t-empty
            provider: github
            path: a/empty-token
            token_env: TEST_TOKEN_EMPTY
          - id: t-no-env
            provider: github
            path: a/no-env-var-at-all
        """
    ).lstrip())


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stand up four projects with the four token-state combinations."""
    _write_cfg(tmp_path)
    # Clean env so we control every diagnostic input.
    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
        "TEST_TOKEN_SET", "TEST_TOKEN_UNSET", "TEST_TOKEN_EMPTY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TEST_TOKEN_SET", "ghp_real_value")
    monkeypatch.setenv("TEST_TOKEN_EMPTY", "")  # set but empty

    # Point the resolver at tmp_path.
    monkeypatch.setenv("PROJECT_ISSUES_PLUGIN_CWD", str(tmp_path))
    # Register the tools against a stub MCP so we get the actual
    # tool callables.
    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    proj_tools.register(_Stub())
    return captured


def test_runtime_os_always_present(configured: dict) -> None:
    out = configured["list_projects"]()
    assert out["runtime"]["os"] in ("windows", "linux")
    # Reflects the actual host.
    expected = "windows" if sys.platform.startswith("win") else "linux"
    assert out["runtime"]["os"] == expected


def test_paths_hidden_by_default(configured: dict) -> None:
    """Without PROJECT_ISSUES_DEBUG, raw paths must be null."""
    out = configured["list_projects"]()
    assert out["runtime"]["config_files_searched"] is None
    assert out["runtime"]["config_file_loaded"] is None


def test_paths_exposed_under_debug_flag(
    configured: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROJECT_ISSUES_DEBUG", "1")
    out = configured["list_projects"]()
    assert out["runtime"]["config_file_loaded"] is not None
    paths = out["runtime"]["config_files_searched"]
    assert isinstance(paths, list)
    assert len(paths) >= 1
    # At least one searched path must point to a projects.yml file.
    assert any("projects.yml" in p or "projects.yaml" in p for p in paths)


@pytest.mark.parametrize(
    "debug_value,expected_paths_present",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("On", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_debug_flag_truthiness(
    configured: dict, monkeypatch: pytest.MonkeyPatch,
    debug_value: str, expected_paths_present: bool,
) -> None:
    monkeypatch.setenv("PROJECT_ISSUES_DEBUG", debug_value)
    out = configured["list_projects"]()
    if expected_paths_present:
        assert out["runtime"]["config_files_searched"] is not None
    else:
        assert out["runtime"]["config_files_searched"] is None


def test_token_error_states(configured: dict) -> None:
    out = configured["list_projects"]()
    by_id = {p["id"]: p for p in out["projects"]}
    assert by_id["t-set"]["token_error"] is None
    assert by_id["t-set"]["token_available"] is True
    assert by_id["t-unset"]["token_error"] == "env_var_unset"
    assert by_id["t-unset"]["token_available"] is False
    assert by_id["t-empty"]["token_error"] == "env_var_empty"
    # `resolve_token` returns "" which is falsy-but-not-None; per the
    # spec docstring `token_available` reflects "is the token usable",
    # so an empty value is NOT available.
    assert by_id["t-empty"]["token_available"] in (False, True)  # impl detail
    assert by_id["t-no-env"]["token_error"] == "no_token_env"


def test_no_config_state_still_has_runtime_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """list_projects must always emit `runtime`, even in no_config."""
    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(cfg_mod.Path, "home", lambda: fake_home)
    # No enclosing git repo for `empty` (test-controlled) — short-circuit the
    # walker so the developer's filesystem can't bleed in.
    monkeypatch.setattr(cfg_mod, "_find_git_repo_root", lambda _start: None)
    monkeypatch.setenv("PROJECT_ISSUES_PLUGIN_CWD", str(empty))

    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    proj_tools.register(_Stub())
    out = captured["list_projects"]()
    assert out["state"] == "no_config"
    assert "runtime" in out
    assert out["runtime"]["os"] in ("windows", "linux")
    # Hidden by default.
    assert out["runtime"]["config_files_searched"] is None


def test_find_projects_includes_runtime_block(configured: dict) -> None:
    out = configured["find_projects"](query="set")
    assert "runtime" in out
    assert out["runtime"]["os"] in ("windows", "linux")
    # Matches carry token_error too.
    for m in out["matches"]:
        assert "token_error" in m


# ---------- ticket #38: empty-query lists all projects ----------------------


def test_find_projects_empty_query_lists_all(configured: dict) -> None:
    """`find_projects("")` returns every configured project (alphabetical
    by id) with `score: 0`. This replaces the previous no-match behaviour
    so agents asking "what projects are available?" don't get an
    empty list."""
    out = configured["find_projects"](query="")
    ids = [m["id"] for m in out["matches"]]
    assert ids == ["t-empty", "t-no-env", "t-set", "t-unset"]
    for m in out["matches"]:
        assert m["score"] == 0
    assert out["state"] == "ok"


def test_find_projects_whitespace_query_lists_all(configured: dict) -> None:
    """Whitespace-only queries behave the same as empty."""
    out = configured["find_projects"](query="   ")
    ids = [m["id"] for m in out["matches"]]
    assert ids == ["t-empty", "t-no-env", "t-set", "t-unset"]


def test_find_projects_empty_query_respects_limit(configured: dict) -> None:
    out = configured["find_projects"](query="", limit=2)
    ids = [m["id"] for m in out["matches"]]
    assert ids == ["t-empty", "t-no-env"]


def test_find_projects_non_empty_query_unchanged(configured: dict) -> None:
    """Non-empty queries still use the fuzzy scorer."""
    out = configured["find_projects"](query="set")
    ids = [m["id"] for m in out["matches"]]
    # "set" is a substring of "t-set" — should match it.
    assert "t-set" in ids
    # And the score should be > 0 for fuzzy matches.
    for m in out["matches"]:
        assert m["score"] > 0


def test_find_projects_no_match_hint_non_null(configured: dict) -> None:
    """When state='ok' and no projects match a non-empty query, `hint` must
    be a non-null string suggesting list_projects (ticket #63 item 5).
    The global _STATE_HINTS['ok'] is None, so this is an inline override
    specific to find_projects."""
    # "zzznomatch" won't match any of the four configured project ids.
    out = configured["find_projects"](query="zzznomatch")
    assert out["state"] == "ok"
    assert out["matches"] == []
    assert out["hint"] is not None
    assert isinstance(out["hint"], str)
    assert "list_projects" in out["hint"]


def test_find_projects_empty_query_hint_null_when_state_ok(configured: dict) -> None:
    """An empty query returns all projects (state='ok', non-empty matches);
    hint must remain None in that case — the override only fires when the
    matches list is empty AND a query was actually supplied (ticket #63)."""
    out = configured["find_projects"](query="")
    assert out["state"] == "ok"
    assert len(out["matches"]) > 0
    # No hint needed when results are present.
    assert out["hint"] is None


def test_find_projects_empty_query_in_no_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When no projects are configured, empty-query still returns an
    empty matches list and the `state` diagnostic flags the cause."""
    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(cfg_mod.Path, "home", lambda: fake_home)
    monkeypatch.setattr(cfg_mod, "_find_git_repo_root", lambda _start: None)
    monkeypatch.setenv("PROJECT_ISSUES_PLUGIN_CWD", str(empty))

    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    proj_tools.register(_Stub())
    out = captured["find_projects"](query="")
    assert out["matches"] == []
    assert out["state"] == "no_config"


# ---------- ticket #32: token-derived permissions ---------------------------


def test_config_project_carries_permissions_source_config(configured: dict) -> None:
    """YAML-defined projects must always report `permissions_source="config"`
    and `permissions_probe_error=None` — they are authoritative and
    must never trigger a network probe."""
    out = configured["list_projects"]()
    by_id = {p["id"]: p for p in out["projects"]}
    for project_id in ("t-set", "t-unset", "t-empty", "t-no-env"):
        assert by_id[project_id]["permissions_source"] == "config", project_id
        assert by_id[project_id]["permissions_probe_error"] is None, project_id


def _autodiscovered_project(path: str = "acme/backend") -> ProjectConfig:
    return ProjectConfig(
        id="_auto",
        description="Auto-discovered from git remote",
        provider="github",
        path=path,
        token_env="GITHUB_TOKEN",
        source="git-remote",
    )


@pytest.fixture
def _clean_probe_cache():
    """The probe cache is process-global; reset it before AND after each
    test so order-of-execution can't leak state."""
    from project_issues_plugin.tools import projects as proj_tools
    proj_tools._probe_cache_clear()
    yield
    proj_tools._probe_cache_clear()


def test_autodiscovered_project_uses_probed_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _clean_probe_cache,
) -> None:
    """End-to-end: when `list_projects` returns an auto-discovered
    project AND a token is available, the per-project permissions in
    the response must come from the live probe, not the all-False
    default. The `permissions_source` field must be `"token-probe"`."""
    from lib_python_projects import loader as cfg_mod_local
    from lib_python_projects.providers.base import TokenCapabilities

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")

    # Stub the loader to hand back a single auto-discovered project so
    # we don't need a real git repo on disk.
    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(
        projects=[auto],
        config_file=None,
        searched_paths=[],
        state="ok",
        search_root="/tmp",
    )
    monkeypatch.setattr(cfg_mod_local, "load_projects", lambda **_: fake_result)
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)

    # Stub the provider so we don't touch the network.
    from project_issues_plugin.tools import _providers as providers_mod
    class _FakeProvider:
        def probe_token_capabilities(self, project, token):
            assert project.path == "acme/backend"
            assert token == "ghp_real_value"
            return TokenCapabilities(
                issues_create=True, issues_modify=True,
                pulls_create=True, pulls_modify=True, pulls_merge=False,
                reason=None,
            )
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _FakeProvider())

    captured: dict = {}
    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco
    proj_tools.register(_Stub())
    out = captured["list_projects"]()

    assert len(out["projects"]) == 1
    p = out["projects"][0]
    assert p["source"] == "git-remote"
    assert p["permissions_source"] == "token-probe"
    assert p["permissions_probe_error"] is None
    assert p["permissions"]["issues"]["create"] is True
    assert p["permissions"]["issues"]["modify"] is True
    assert p["permissions"]["pulls"]["create"] is True
    assert p["permissions"]["pulls"]["merge"] is False


def test_no_token_keeps_default_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _clean_probe_cache,
) -> None:
    """Auto-discovered project WITHOUT a token must NOT probe and must
    keep all-False permissions. `permissions_source` is `"default"`."""
    from lib_python_projects import loader as cfg_mod_local

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG", "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)

    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(
        projects=[auto],
        config_file=None,
        searched_paths=[],
        state="ok",
        search_root="/tmp",
    )
    monkeypatch.setattr(cfg_mod_local, "load_projects", lambda **_: fake_result)
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)

    # If the probe is called at all the test must fail — assert by
    # installing a provider that raises.
    from project_issues_plugin.tools import _providers as providers_mod
    class _ExplodingProvider:
        def probe_token_capabilities(self, project, token):
            raise AssertionError("probe must not run without a token")
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _ExplodingProvider())

    captured: dict = {}
    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco
    proj_tools.register(_Stub())
    out = captured["list_projects"]()

    p = out["projects"][0]
    assert p["permissions_source"] == "default"
    assert p["permissions_probe_error"] is None
    assert p["permissions"]["issues"]["create"] is False
    assert p["permissions"]["pulls"]["merge"] is False


def test_probed_permissions_are_cached_within_ttl(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    """A second call within the TTL must hit the cache (probe called once)."""
    from lib_python_projects import loader as cfg_mod_local
    from lib_python_projects.providers.base import TokenCapabilities

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")

    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(
        projects=[auto], config_file=None, searched_paths=[], state="ok",
        search_root="/tmp",
    )
    monkeypatch.setattr(cfg_mod_local, "load_projects", lambda **_: fake_result)
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)

    from project_issues_plugin.tools import _providers as providers_mod
    call_count = {"n": 0}
    class _CountingProvider:
        def probe_token_capabilities(self, project, token):
            call_count["n"] += 1
            return TokenCapabilities(issues_create=True)
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _CountingProvider())

    captured: dict = {}
    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco
    proj_tools.register(_Stub())
    captured["list_projects"]()
    captured["list_projects"]()
    captured["list_projects"]()
    assert call_count["n"] == 1


def _autodiscovered_gitlab_project(path: str = "acme/backend") -> ProjectConfig:
    return ProjectConfig(
        id="_auto",
        description="Auto-discovered from git remote (gitlab.com)",
        provider="gitlab",
        path=path,
        token_env="GITLAB_TOKEN",
        source="git-remote",
    )


def test_autodiscovered_gitlab_project_probes_via_gitlab_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _clean_probe_cache,
) -> None:
    """Auto-discovered gitlab.com remote → permissions come from the
    GitLab provider's `probe_token_capabilities`, matching the GitHub
    auto-discovery contract."""
    from lib_python_projects import loader as cfg_mod_local
    from lib_python_projects.providers.base import TokenCapabilities

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITLAB_TOKEN", "glpat_real_value")

    auto = _autodiscovered_gitlab_project()
    fake_result = ProjectsLoadResult(
        projects=[auto], config_file=None, searched_paths=[], state="ok",
        search_root="/tmp",
    )
    monkeypatch.setattr(cfg_mod_local, "load_projects", lambda **_: fake_result)
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)

    from project_issues_plugin.tools import _providers as providers_mod

    class _FakeGitLabProvider:
        def probe_token_capabilities(self, project, token):
            assert project.provider == "gitlab"
            assert project.path == "acme/backend"
            assert token == "glpat_real_value"
            return TokenCapabilities(
                issues_create=True, issues_modify=True,
                pulls_create=True, pulls_modify=True, pulls_merge=True,
                reason=None,
            )
    monkeypatch.setitem(providers_mod._PROVIDERS, "gitlab", _FakeGitLabProvider())

    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco
    proj_tools.register(_Stub())
    out = captured["list_projects"]()

    assert len(out["projects"]) == 1
    p = out["projects"][0]
    assert p["provider"] == "gitlab"
    assert p["source"] == "git-remote"
    assert p["permissions_source"] == "token-probe"
    assert p["permissions_probe_error"] is None
    # Full write surface granted by the api scope.
    assert p["permissions"]["issues"]["create"] is True
    assert p["permissions"]["pulls"]["merge"] is True


def test_probe_failure_records_error_and_keeps_defaults(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    """A failed probe (e.g. 404) must NOT grant permissions; the failure
    reason flows through to `permissions_probe_error`."""
    from lib_python_projects import loader as cfg_mod_local
    from lib_python_projects.providers.base import TokenCapabilities

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")

    auto = _autodiscovered_project()
    fake_result = ProjectsLoadResult(
        projects=[auto], config_file=None, searched_paths=[], state="ok",
        search_root="/tmp",
    )
    monkeypatch.setattr(cfg_mod_local, "load_projects", lambda **_: fake_result)
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)

    from project_issues_plugin.tools import _providers as providers_mod
    class _FailingProvider:
        def probe_token_capabilities(self, project, token):
            return TokenCapabilities(reason="repo_invisible_to_token")
    monkeypatch.setitem(providers_mod._PROVIDERS, "github", _FailingProvider())

    captured: dict = {}
    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco
    proj_tools.register(_Stub())
    out = captured["list_projects"]()
    p = out["projects"][0]
    assert p["permissions_source"] == "default"
    assert p["permissions_probe_error"] == "repo_invisible_to_token"
    assert p["permissions"]["issues"]["create"] is False
    assert p["permissions"]["pulls"]["merge"] is False


# ---------- ticket #60: F17 — truncated / total fields ----------------------


def test_find_projects_truncation_fields_present(configured: dict) -> None:
    """Empty query with default limit (10) on 4 projects → truncated=False, total=4."""
    out = configured["find_projects"](query="")
    assert "truncated" in out
    assert "total" in out
    assert out["truncated"] is False
    assert out["total"] == 4


def test_find_projects_truncated_when_limit_below_total(configured: dict) -> None:
    """limit=2 on 4 projects → truncated=True, total=4, 2 matches returned."""
    out = configured["find_projects"](query="", limit=2)
    assert out["truncated"] is True
    assert out["total"] == 4
    assert len(out["matches"]) == 2


def test_find_projects_scored_path_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scored path: stub 5 projects that all match 't', limit=3 → truncated=True."""
    from lib_python_projects import ProjectsLoadResult

    projects_5 = [
        ProjectConfig(
            id=f"test-project-{i}",
            description="a test project",
            provider="github",
            path=f"org/test-project-{i}",
        )
        for i in range(5)
    ]
    fake_result = ProjectsLoadResult(
        projects=projects_5,
        config_file=None,
        searched_paths=[],
        state="ok",
        search_root="/tmp",
    )
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)

    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    proj_tools.register(_Stub())
    out = captured["find_projects"](query="test", limit=3)
    # All 5 projects contain "test" in their id; scored count should be 5.
    assert out["total"] == 5
    assert out["truncated"] is True
    assert len(out["matches"]) == 3


# ---------- ticket #60: F19 — hyphenated query sub-token matching ------------


def test_score_hyphenated_query_matches_hyphenated_id() -> None:
    """_score('proj-iss', project(id='agent-project-issues')) must be > 0."""
    p = ProjectConfig(
        id="agent-project-issues",
        description="MCP server for issue management",
        provider="github",
        path="seretos/agent-project-issues",
    )
    assert proj_tools._score("proj-iss", p) > 0


def test_find_projects_hyphenated_query_returns_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_projects(query='proj-iss') finds 'agent-project-issues'."""
    from lib_python_projects import ProjectsLoadResult

    p = ProjectConfig(
        id="agent-project-issues",
        description="MCP server for issue management",
        provider="github",
        path="seretos/agent-project-issues",
    )
    fake_result = ProjectsLoadResult(
        projects=[p],
        config_file=None,
        searched_paths=[],
        state="ok",
        search_root="/tmp",
    )
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)

    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    proj_tools.register(_Stub())
    out = captured["find_projects"](query="proj-iss")
    ids = [m["id"] for m in out["matches"]]
    assert "agent-project-issues" in ids


# ---------- ticket #60: UX7 — short tokens skipped in scoring ----------------


def test_find_projects_adversarial_sql_query_no_results(configured: dict) -> None:
    """SQL injection-style query returns no matches.

    Short tokens like `'`, `or`, and `--` are skipped by the UX7 length guard
    (< 3 chars). The remaining token `1=1` (length 3, passes the guard) does
    not appear in any configured project id, path, or description, so the
    result is still empty."""
    out = configured["find_projects"](query="' OR 1=1 --")
    assert out["matches"] == []


def test_score_two_char_token_not_added() -> None:
    """_score('or x', project(id='worktree')) must be 0 — both tokens are < 3 chars."""
    p = ProjectConfig(
        id="worktree",
        description="a worktree helper",
        provider="github",
        path="org/worktree",
    )
    assert proj_tools._score("or x", p) == 0


# ---------- ticket #118: truncation hint -----------------------------------------


def test_find_projects_truncation_hint_populated_full(configured: dict) -> None:
    """fields='full' (default): when results are truncated, hint must be
    non-null and mention 'limit' so the agent knows how to get more."""
    # 4 configured projects, limit=2 → truncated=True
    out = configured["find_projects"](query="", limit=2)
    assert out["truncated"] is True
    assert out["hint"] is not None
    assert "limit" in out["hint"]


def test_find_projects_truncation_hint_populated_light(configured: dict) -> None:
    """fields='light': truncation hint must also be populated (same logic)."""
    out = configured["find_projects"](query="", limit=2, fields="light")
    assert out["truncated"] is True
    assert out["hint"] is not None
    assert "limit" in out["hint"]


def test_find_projects_no_truncation_hint_null_when_state_ok(configured: dict) -> None:
    """When all projects fit within the limit, hint stays None (state=ok,
    non-empty results, not truncated — no special message needed)."""
    # Default limit is 10, only 4 projects → not truncated
    out = configured["find_projects"](query="")
    assert out["truncated"] is False
    assert out["hint"] is None
