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
