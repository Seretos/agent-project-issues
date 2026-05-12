from __future__ import annotations

import textwrap
from pathlib import Path

from project_issues_plugin.config import (
    LoadResult,
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
        permissions = { create = true, modify = false }
    """)
    result = load_projects(cwd=tmp_path)
    assert isinstance(result, LoadResult)
    assert result.state == "ok"
    assert len(result.projects) == 1
    p = result.projects[0]
    assert p.id == "acme"
    assert p.provider == "github"
    assert p.permissions.create is True
    assert p.permissions.modify is False


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
