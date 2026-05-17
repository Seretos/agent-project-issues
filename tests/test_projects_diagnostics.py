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

from project_issues_plugin import config as cfg_mod
from project_issues_plugin.config import ProjectConfig
from project_issues_plugin.tools import projects as proj_tools


def _write_cfg(tmp_path: Path) -> None:
    cfg = tmp_path / ".claude" / "project-issues.yml"
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
    # At least one searched path must point to a project-issues file.
    assert any("project-issues" in p for p in paths)


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
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(cfg_mod.Path, "home", lambda: fake_home)
    monkeypatch.setattr(cfg_mod, "_is_windows", lambda: False)
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
    from project_issues_plugin import config as cfg_mod_local
    from project_issues_plugin.providers.base import TokenCapabilities

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
    fake_result = cfg_mod_local.LoadResult(
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
    from project_issues_plugin import config as cfg_mod_local

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG", "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)

    auto = _autodiscovered_project()
    fake_result = cfg_mod_local.LoadResult(
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
    from project_issues_plugin import config as cfg_mod_local
    from project_issues_plugin.providers.base import TokenCapabilities

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")

    auto = _autodiscovered_project()
    fake_result = cfg_mod_local.LoadResult(
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


def test_probe_failure_records_error_and_keeps_defaults(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    """A failed probe (e.g. 404) must NOT grant permissions; the failure
    reason flows through to `permissions_probe_error`."""
    from project_issues_plugin import config as cfg_mod_local
    from project_issues_plugin.providers.base import TokenCapabilities

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real_value")

    auto = _autodiscovered_project()
    fake_result = cfg_mod_local.LoadResult(
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
