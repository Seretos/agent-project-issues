from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from project_issues_plugin import config as cfg_mod
from project_issues_plugin.config import (
    ConfigDocument,
    ConfigError,
    LoadResult,
    Permissions,
    ProjectConfig,
    _parse_remote_url,
    _resolve_config_path,
    load_projects,
)


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


def test_parse_remote_url_ssh():
    assert _parse_remote_url("git@github.com:acme/backend.git") == ("github.com", "acme/backend")


def test_parse_remote_url_https():
    assert _parse_remote_url("https://github.com/acme/backend") == ("github.com", "acme/backend")


def test_parse_remote_url_https_with_token():
    assert _parse_remote_url("https://x-access-token:abc@github.com/acme/backend.git") == ("github.com", "acme/backend")


def test_parse_remote_url_unknown():
    assert _parse_remote_url("ftp://example.com/foo") is None


# ---------- YAML loader: happy path -----------------------------------------


def test_load_yaml_returns_projects(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: acme
            provider: github
            path: acme/backend
            permissions:
              issues:
                create: true
                modify: false
    """)
    result = load_projects(cwd=tmp_path)
    assert isinstance(result, LoadResult)
    assert result.state == "ok"
    assert len(result.projects) == 1
    p = result.projects[0]
    assert p.id == "acme"
    assert p.provider == "github"
    assert p.path == "acme/backend"
    # Backward-compat derived properties still work for internal code.
    assert p.owner == "acme"
    assert p.repo == "backend"
    assert p.display_path == "acme/backend"
    assert p.web_url == "https://github.com/acme/backend"
    assert p.permissions.issues.create is True
    assert p.permissions.issues.modify is False
    # pulls namespace defaults to all-false
    assert p.permissions.pulls.create is False
    assert p.permissions.pulls.modify is False
    assert p.permissions.pulls.merge is False


def test_load_yaml_yaml_extension_also_works(tmp_path: Path):
    """The loader accepts `.yaml` in addition to `.yml`."""
    cfg = tmp_path / ".claude/project-issues.yaml"
    _write(cfg, """
        version: 1
        projects:
          - id: a
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "ok"
    assert result.projects[0].id == "a"


def test_load_yaml_omitted_version_defaults_to_one(tmp_path: Path):
    """`version` is optional and defaults to 1 (per plan-comment D2=B)."""
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        projects:
          - id: nv
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "ok"


def test_load_yaml_rejects_unknown_top_level_key(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        version: 1
        oops_extra: 42
        projects:
          - id: x
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "oops_extra" in (result.error or "")


def test_load_yaml_rejects_unknown_project_key(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: x
            provider: github
            path: a/b
            owner: legacy    # legacy v0 field — must be rejected
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "owner" in (result.error or "")


def test_load_yaml_rejects_future_schema_version(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        version: 99
        projects:
          - id: x
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "version" in (result.error or "").lower()


def test_load_yaml_rejects_github_path_without_slash(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: bad
            provider: github
            path: justname
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "owner/repo" in (result.error or "")


def test_load_yaml_rejects_reserved_id(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: _auto
            provider: github
            path: a/b
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "_auto" in (result.error or "")


def test_load_no_config_returns_no_config(tmp_path: Path):
    result = load_projects(cwd=tmp_path)
    assert result.state == "no_config"
    assert result.projects == []


def test_load_config_empty(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, "# empty file\n")
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_empty"
    assert result.projects == []


def test_load_yaml_gitlab_path_is_passthrough(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: gl
            provider: gitlab
            path: group/sub/project
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "ok"
    p = result.projects[0]
    assert p.path == "group/sub/project"
    assert p.project_path == "group/sub/project"
    assert p.owner is None
    assert p.repo is None
    assert p.web_url == "https://gitlab.com/group/sub/project"


# ---------- Permissions model: strict-only (no flat migration) --------------


def test_permissions_nested_form_loads_correctly():
    """The nested form is the canonical (and only) shape."""
    perms = Permissions.model_validate({
        "issues": {"create": True, "modify": True},
        "pulls": {"create": True, "modify": False, "merge": False},
    })
    assert perms.issues.create is True
    assert perms.issues.modify is True
    assert perms.pulls.create is True
    assert perms.pulls.modify is False
    assert perms.pulls.merge is False


def test_permissions_legacy_flat_form_is_rejected():
    """Flat `{create, modify}` is no longer accepted in v1 (D3=A)."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Permissions.model_validate({"create": True, "modify": True})


def test_permissions_empty_defaults_all_false():
    perms = Permissions.model_validate({})
    assert perms.issues.create is False
    assert perms.issues.modify is False
    assert perms.pulls.create is False
    assert perms.pulls.modify is False
    assert perms.pulls.merge is False


# ---------- ConfigDocument model directly -----------------------------------


def test_config_document_strict():
    """ConfigDocument forbids unknown top-level keys."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ConfigDocument.model_validate({"version": 1, "unknown": True})


# ---------- list_projects response shape (no schema-internal leakage) -------


def test_list_projects_response_keeps_path_key(tmp_path: Path):
    """Smoke-test: the externally-visible list_projects response shape
    (which is `path`, NOT `owner`/`repo`) must be unchanged by the
    schema migration — ticket #8 acceptance criterion."""
    cfg = tmp_path / ".claude/project-issues.yml"
    _write(cfg, """
        version: 1
        projects:
          - id: a
            provider: github
            path: acme/backend
    """)
    from project_issues_plugin.tools.projects import _project_to_dict
    result = load_projects(cwd=tmp_path)
    d = _project_to_dict(result.projects[0])
    assert d["path"] == "acme/backend"
    assert d["provider"] == "github"
    assert "owner" not in d
    assert "repo" not in d


# ---------- OS-aware config-path resolver (ticket #14) ----------------------


class TestConfigPathResolver:
    """Cover the resolver's precedence and per-OS default sets.

    All tests use `monkeypatch` to control `os.environ`, `Path.home`,
    and `sys.platform` — they never read the real user's home
    directory or rely on the real OS.
    """

    @staticmethod
    def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
        """Wipe every env var the resolver consults so test runs are
        deterministic. Without this a developer's locally-set
        `PROJECT_ISSUES_CONFIG` could leak in."""
        for var in (
            "PROJECT_ISSUES_CONFIG",
            "PROJECT_ISSUES_PLUGIN_ROOT",
            "PROJECT_ISSUES_PLUGIN_CWD",
            "CLAUDE_PROJECT_DIR",
            "XDG_CONFIG_HOME",
            "APPDATA",
            "USERPROFILE",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_env_override_wins_over_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`PROJECT_ISSUES_CONFIG` is checked first; even when a
        CWD-local config exists, the override wins."""
        self._clear_env(monkeypatch)
        override = tmp_path / "alt" / "explicit.yml"
        override.parent.mkdir(parents=True)
        override.write_text("version: 1\nprojects: []\n")
        # Also create a CWD-near config that would otherwise win.
        cwd_cfg = tmp_path / ".claude" / "project-issues.yml"
        cwd_cfg.parent.mkdir(parents=True)
        cwd_cfg.write_text("version: 1\nprojects: []\n")

        monkeypatch.setenv("PROJECT_ISSUES_CONFIG", str(override))
        winner, searched = _resolve_config_path(tmp_path)
        assert winner == override.resolve()
        # The override is the only entry inspected — the resolver
        # short-circuits after the explicit hit.
        assert searched == [override.resolve()]

    def test_env_override_missing_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """D2 = A: missing override path is a hard error, not a
        silent fall-through."""
        self._clear_env(monkeypatch)
        bogus = tmp_path / "does-not-exist.yml"
        monkeypatch.setenv("PROJECT_ISSUES_CONFIG", str(bogus))
        with pytest.raises(ConfigError, match="non-existent"):
            _resolve_config_path(tmp_path)

    def test_env_override_missing_propagates_to_load_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`load_projects` translates the resolver's `ConfigError`
        into a `config_error` LoadResult so MCP callers see the
        diagnostic instead of a stack trace."""
        self._clear_env(monkeypatch)
        bogus = tmp_path / "missing.yml"
        monkeypatch.setenv("PROJECT_ISSUES_CONFIG", str(bogus))
        result = load_projects(cwd=tmp_path)
        assert result.state == "config_error"
        assert "non-existent" in (result.error or "")
        # `searched_paths` always reflects what the resolver tried.
        assert any("missing.yml" in p for p in result.searched_paths)

    def test_plugin_root_beats_cwd_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`$PROJECT_ISSUES_PLUGIN_ROOT/project-issues.yml` outranks
        the `<cwd>/.claude/...` walk-up."""
        self._clear_env(monkeypatch)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        (plugin_root / "project-issues.yml").write_text("version: 1\nprojects: []\n")
        cwd_cfg = tmp_path / ".claude" / "project-issues.yml"
        cwd_cfg.parent.mkdir(parents=True)
        cwd_cfg.write_text("version: 1\nprojects: []\n")

        monkeypatch.setenv("PROJECT_ISSUES_PLUGIN_ROOT", str(plugin_root))
        winner, _ = _resolve_config_path(tmp_path)
        assert winner == (plugin_root / "project-issues.yml").resolve()

    def test_cwd_walk_beats_os_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A CWD-near config wins over the OS-default home locations."""
        self._clear_env(monkeypatch)
        cwd_cfg = tmp_path / ".claude" / "project-issues.yml"
        cwd_cfg.parent.mkdir(parents=True)
        cwd_cfg.write_text("version: 1\nprojects: []\n")
        # Stand up an `OS-default` config that would otherwise be
        # picked: point HOME / XDG at a sibling that also carries a
        # config.
        fake_home = tmp_path / "fake-home"
        (fake_home / ".config" / "project-issues.yml").parent.mkdir(parents=True)
        (fake_home / ".config" / "project-issues.yml").write_text(
            "version: 1\nprojects: []\n"
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
        monkeypatch.setattr(cfg_mod.Path, "home", lambda: fake_home)
        monkeypatch.setattr(cfg_mod, "_is_windows", lambda: False)

        winner, searched = _resolve_config_path(tmp_path)
        assert winner == cwd_cfg.resolve()
        # The XDG candidate must not appear in `searched` -- the
        # resolver short-circuited on the CWD hit, before reaching the
        # OS-default loop.
        xdg_str = str((fake_home / ".config" / "project-issues.yml").resolve())
        assert xdg_str not in {str(p) for p in searched}

    def test_cwd_walk_deepest_first_addresses_ticket_19(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Ticket #19 case: a sub-folder config must NOT shadow the
        CWD-near config. The walk-up is *upward*, so a sibling
        `<cwd>/<sub>/.claude/project-issues.yml` is never even
        considered when starting from `<cwd>`."""
        self._clear_env(monkeypatch)
        # CWD-near config (the one the user intended).
        cwd_cfg = tmp_path / ".claude" / "project-issues.yml"
        cwd_cfg.parent.mkdir(parents=True)
        cwd_cfg.write_text(
            "version: 1\nprojects:\n  - id: cwd\n    provider: github\n    path: a/b\n"
        )
        # A sibling sub-folder also carries a config — must NOT win
        # when CWD is `tmp_path`.
        sub_cfg = tmp_path / "dev-test" / ".claude" / "project-issues.yml"
        sub_cfg.parent.mkdir(parents=True)
        sub_cfg.write_text(
            "version: 1\nprojects:\n  - id: sub\n    provider: github\n    path: x/y\n"
        )
        monkeypatch.setattr(cfg_mod, "_is_windows", lambda: False)
        winner, _ = _resolve_config_path(tmp_path)
        assert winner == cwd_cfg.resolve()
        # And, as a regression guard, the sub-folder candidate must
        # not even be in `searched`.
        result = load_projects(cwd=tmp_path)
        assert result.config_file == str(cwd_cfg.resolve())
        assert {p.id for p in result.projects} == {"cwd"}

    def test_linux_default_xdg_then_dotclaude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Linux defaults: XDG candidate is tried before
        `~/.claude`. Whichever exists wins."""
        self._clear_env(monkeypatch)
        fake_home = tmp_path / "fake-home"
        # ONLY the ~/.claude file exists (no XDG).
        dot_claude = fake_home / ".claude" / "project-issues.yml"
        dot_claude.parent.mkdir(parents=True)
        dot_claude.write_text("version: 1\nprojects: []\n")
        monkeypatch.setattr(cfg_mod.Path, "home", lambda: fake_home)
        monkeypatch.setattr(cfg_mod, "_is_windows", lambda: False)

        # Use an empty CWD so the resolver falls through to OS defaults.
        empty_cwd = tmp_path / "empty-cwd"
        empty_cwd.mkdir()
        winner, searched = _resolve_config_path(empty_cwd)
        assert winner == dot_claude.resolve()
        # The XDG candidate was inspected first (and rejected).
        xdg_first = str((fake_home / ".config" / "project-issues.yml"))
        assert any(xdg_first in str(p) for p in searched)

    def test_windows_default_appdata_then_userprofile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Windows defaults: APPDATA candidate is tried before
        USERPROFILE\\.claude."""
        self._clear_env(monkeypatch)
        # ONLY the USERPROFILE candidate exists.
        userprofile = tmp_path / "User"
        userprofile.mkdir()
        up_cfg = userprofile / ".claude" / "project-issues.yml"
        up_cfg.parent.mkdir(parents=True)
        up_cfg.write_text("version: 1\nprojects: []\n")

        appdata = tmp_path / "AppData"
        appdata.mkdir()  # exists but empty -- no config inside
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("USERPROFILE", str(userprofile))
        monkeypatch.setattr(cfg_mod, "_is_windows", lambda: True)

        empty_cwd = tmp_path / "empty-cwd"
        empty_cwd.mkdir()
        winner, searched = _resolve_config_path(empty_cwd)
        assert winner == up_cfg.resolve()
        # APPDATA candidate was inspected (and missed) before
        # USERPROFILE.
        appdata_cand = appdata / "project-issues" / "project-issues.yml"
        assert any(str(appdata_cand) in str(p) for p in searched)

    def test_no_config_returns_searched_paths_for_diagnostics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When nothing is found `searched_paths` still reports the
        candidate set, so #15's `runtime.*` block can surface it."""
        self._clear_env(monkeypatch)
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(cfg_mod.Path, "home", lambda: fake_home)
        monkeypatch.setattr(cfg_mod, "_is_windows", lambda: False)
        empty_cwd = tmp_path / "empty-cwd"
        empty_cwd.mkdir()
        result = load_projects(cwd=empty_cwd)
        assert result.state == "no_config"
        # At least the OS-defaults + the empty-CWD walk were inspected.
        # Normalize separators so the assertion is OS-agnostic — Windows
        # paths use '\\', Linux paths use '/'.
        normed = [p.replace("\\", "/") for p in result.searched_paths]
        assert any(".config/project-issues.yml" in p for p in normed)
        assert any(".claude/project-issues.yml" in p for p in normed)
