from __future__ import annotations

import textwrap
import warnings
from pathlib import Path

import pytest

from project_issues_plugin.config import (
    LoadResult,
    Permissions,
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


def test_load_toml_returns_projects(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.toml"
    _write(cfg, """
        [[projects]]
        id = "acme"
        provider = "github"
        owner = "acme"
        repo = "backend"

        [projects.permissions.issues]
        create = true
        modify = false
    """)
    result = load_projects(cwd=tmp_path)
    assert isinstance(result, LoadResult)
    assert result.state == "ok"
    assert len(result.projects) == 1
    p = result.projects[0]
    assert p.id == "acme"
    assert p.provider == "github"
    assert p.permissions.issues.create is True
    assert p.permissions.issues.modify is False
    # pulls namespace defaults to all-false
    assert p.permissions.pulls.create is False
    assert p.permissions.pulls.modify is False
    assert p.permissions.pulls.merge is False


def test_load_toml_rejects_reserved_id(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.toml"
    _write(cfg, """
        [[projects]]
        id = "_auto"
        provider = "github"
        owner = "acme"
        repo = "backend"
    """)
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_error"
    assert "_auto" in (result.error or "")


def test_load_no_config_returns_no_config(tmp_path: Path):
    result = load_projects(cwd=tmp_path)
    assert result.state == "no_config"
    assert result.projects == []


def test_load_config_empty(tmp_path: Path):
    cfg = tmp_path / ".claude/project-issues.toml"
    _write(cfg, "# empty file\n")
    result = load_projects(cwd=tmp_path)
    assert result.state == "config_empty"
    assert result.projects == []


# ---------- Permissions model: nested form + flat-form auto-migration -------


def test_permissions_nested_form_loads_correctly():
    """The new nested form is the canonical shape and emits no warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning here would be a regression
        perms = Permissions.model_validate({
            "issues": {"create": True, "modify": True},
            "pulls": {"create": True, "modify": False, "merge": False},
        })
    assert perms.issues.create is True
    assert perms.issues.modify is True
    assert perms.pulls.create is True
    assert perms.pulls.modify is False
    assert perms.pulls.merge is False


def test_permissions_flat_form_migrates_and_warns():
    """Flat `{create, modify}` migrates to `issues.*` and warns once."""
    with pytest.warns(DeprecationWarning, match="Flat 'permissions' form"):
        perms = Permissions.model_validate({"create": True, "modify": True})
    assert perms.issues.create is True
    assert perms.issues.modify is True
    # pulls namespace is all-False — no flat equivalent for merge.
    assert perms.pulls.create is False
    assert perms.pulls.modify is False
    assert perms.pulls.merge is False


def test_permissions_flat_with_pr_keys_migrates():
    """Flat with `pr_create` / `pr_modify` migrates to `pulls.*`."""
    with pytest.warns(DeprecationWarning):
        perms = Permissions.model_validate({
            "create": True,
            "modify": True,
            "pr_create": True,
            "pr_modify": False,
        })
    assert perms.issues.create is True
    assert perms.issues.modify is True
    assert perms.pulls.create is True
    assert perms.pulls.modify is False
    # Merge is NEW — no flat equivalent — defaults to False even when the
    # other pulls flags are set.
    assert perms.pulls.merge is False


def test_permissions_empty_defaults_all_false():
    """An empty `permissions` dict defaults every flag to False and is silent."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        perms = Permissions.model_validate({})
    assert perms.issues.create is False
    assert perms.issues.modify is False
    assert perms.pulls.create is False
    assert perms.pulls.modify is False
    assert perms.pulls.merge is False


def test_load_toml_flat_form_migrates(tmp_path: Path):
    """A TOML file using the legacy flat shape still loads + emits a warning."""
    cfg = tmp_path / ".claude/project-issues.toml"
    _write(cfg, """
        [[projects]]
        id = "legacy"
        provider = "github"
        owner = "acme"
        repo = "backend"
        permissions = { create = true, modify = true }
    """)
    with pytest.warns(DeprecationWarning, match="Flat 'permissions' form"):
        result = load_projects(cwd=tmp_path)
    assert result.state == "ok"
    p = result.projects[0]
    assert p.permissions.issues.create is True
    assert p.permissions.issues.modify is True
    assert p.permissions.pulls.create is False
    assert p.permissions.pulls.merge is False


def test_load_toml_flat_with_pr_keys_migrates(tmp_path: Path):
    """A TOML file with the extended flat shape (`pr_create`/`pr_modify`) migrates."""
    cfg = tmp_path / ".claude/project-issues.toml"
    _write(cfg, """
        [[projects]]
        id = "legacy-ext"
        provider = "github"
        owner = "acme"
        repo = "backend"
        permissions = { create = true, modify = true, pr_create = true, pr_modify = false }
    """)
    with pytest.warns(DeprecationWarning):
        result = load_projects(cwd=tmp_path)
    p = result.projects[0]
    assert p.permissions.issues.create is True
    assert p.permissions.pulls.create is True
    assert p.permissions.pulls.modify is False
    assert p.permissions.pulls.merge is False  # no flat equivalent
