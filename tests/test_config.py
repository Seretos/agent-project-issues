from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from project_issues_plugin.config import (
    ConfigDocument,
    LoadResult,
    Permissions,
    ProjectConfig,
    _parse_remote_url,
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
