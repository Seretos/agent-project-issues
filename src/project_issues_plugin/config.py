"""Project configuration loading.

Resolves the project list for the server's current working directory:

  1. Walk up from CWD looking for `.claude/project-issues.toml`. If found,
     parse it, load the referenced `.env`, and return the configured
     projects.
  2. Otherwise, walk up looking for `.git/config` and inspect the `origin`
     remote URL. For github.com / gitlab.com, synthesize a single
     read-only project with id="_auto".
  3. Otherwise, return an empty list.

TOML schema:

    env_file = ".env"   # optional, relative to the config file

    [[projects]]
    id          = "acme-backend"
    description = "Acme main backend"
    provider    = "github"
    owner       = "acme"
    repo        = "backend"
    token_env   = "GITHUB_TOKEN_ACME"
    permissions = { create = true, modify = true }
"""
from __future__ import annotations

import logging
import os
import re
import tomllib
from configparser import ConfigParser
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

log = logging.getLogger("project-issues.config")

Provider = Literal["github", "gitlab"]
Source = Literal["config", "git-remote"]


class Permissions(BaseModel):
    create: bool = False
    modify: bool = False


class ProjectConfig(BaseModel):
    id: str = Field(min_length=1)
    description: str = ""
    provider: Provider
    owner: str | None = None
    repo: str | None = None
    project_path: str | None = None
    base_url: str | None = None
    token_env: str | None = None
    permissions: Permissions = Field(default_factory=Permissions)
    source: Source = "config"

    @model_validator(mode="after")
    def _check_provider_fields(self) -> "ProjectConfig":
        if self.provider == "github":
            if not self.owner or not self.repo:
                raise ValueError("github project requires 'owner' and 'repo'")
        elif self.provider == "gitlab":
            if not self.project_path:
                raise ValueError("gitlab project requires 'project_path'")
        return self

    @property
    def display_path(self) -> str:
        if self.provider == "github":
            return f"{self.owner}/{self.repo}"
        return self.project_path or ""

    @property
    def web_url(self) -> str | None:
        if self.provider == "github":
            return f"https://github.com/{self.owner}/{self.repo}"
        if self.provider == "gitlab":
            base = (self.base_url or "https://gitlab.com").rstrip("/")
            return f"{base}/{self.project_path}"
        return None


class ConfigError(Exception):
    pass


def _walk_up(start: Path, name: str) -> Path | None:
    cur = start.resolve()
    while True:
        candidate = cur / name
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def _load_env_file(path: Path) -> None:
    """Tiny .env parser — KEY=value lines, optional quotes, # comments.

    Does not overwrite entries already present in os.environ, so explicit
    process-env always wins over file-env. Tolerates a leading UTF-8 BOM
    (Windows editors / `Set-Content -Encoding utf8` add one).
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        log.warning("could not read env_file %s: %s", path, exc)
        return
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            log.warning("%s:%d: skipping malformed line", path, lineno)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_SSH_RE = re.compile(r"^git@([^:]+):(.+?)(?:\.git)?$")
_HTTPS_RE = re.compile(r"^https?://(?:[^/@]+@)?([^/]+)/(.+?)(?:\.git)?/?$")


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    """Return (host, path) where `path` is owner/repo or group/sub/repo."""
    url = url.strip()
    m = _SSH_RE.match(url)
    if m:
        return m.group(1).lower(), m.group(2)
    m = _HTTPS_RE.match(url)
    if m:
        return m.group(1).lower(), m.group(2)
    return None


def _autodiscover_from_git(start: Path) -> ProjectConfig | None:
    git_config_path = _walk_up(start, ".git/config")
    if not git_config_path:
        return None
    cp = ConfigParser()
    try:
        # utf-8-sig: tolerate a leading BOM written by some Windows editors.
        cp.read(git_config_path, encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not parse %s: %s", git_config_path, exc)
        return None
    section = 'remote "origin"'
    if section not in cp.sections():
        log.info("no [remote \"origin\"] in %s — skipping auto-discovery", git_config_path)
        return None
    url = cp.get(section, "url", fallback=None)
    if not url:
        return None
    parsed = _parse_remote_url(url)
    if not parsed:
        log.info("origin remote URL not recognised: %s", url)
        return None
    host, path = parsed
    if host == "github.com":
        parts = path.split("/", 1)
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]
        return ProjectConfig(
            id="_auto",
            description=f"Auto-discovered from git remote ({url.strip()})",
            provider="github",
            owner=owner,
            repo=repo,
            token_env="GITHUB_TOKEN",
            source="git-remote",
        )
    if host == "gitlab.com":
        return ProjectConfig(
            id="_auto",
            description=f"Auto-discovered from git remote ({url.strip()})",
            provider="gitlab",
            project_path=path,
            token_env="GITLAB_TOKEN",
            source="git-remote",
        )
    log.info("auto-discovery skipped — host %s is not github.com or gitlab.com", host)
    return None


def _load_toml(toml_path: Path) -> list[ProjectConfig]:
    raw = toml_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        # tomllib rejects the BOM; strip it transparently so users who saved
        # the file with a BOM-emitting editor (Notepad, `Set-Content -Encoding utf8`)
        # don't get a cryptic "Invalid statement at line 1" error.
        raw = raw[3:]
    data = tomllib.loads(raw.decode("utf-8"))

    # Locate the .env file. If `env_file` is explicitly set, respect it
    # (resolved relative to the config file). Otherwise check the project
    # root first (the conventional .env location) and then the config
    # directory as a fallback.
    explicit_env = data.get("env_file")
    if explicit_env is not None:
        candidates = [(toml_path.parent / explicit_env).resolve()]
    else:
        candidates = [
            (toml_path.parent.parent / ".env").resolve(),
            (toml_path.parent / ".env").resolve(),
        ]
    for env_path in candidates:
        if env_path.exists():
            _load_env_file(env_path)
            break

    raw_projects = data.get("projects", [])
    if not isinstance(raw_projects, list):
        raise ConfigError(f"{toml_path}: 'projects' must be an array of tables")

    projects: list[ProjectConfig] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_projects):
        if not isinstance(item, dict):
            raise ConfigError(f"{toml_path}: project #{idx + 1} must be a table")
        try:
            project = ProjectConfig.model_validate({**item, "source": "config"})
        except ValidationError as exc:
            raise ConfigError(f"{toml_path}: project #{idx + 1}: {exc}") from exc
        if project.id == "_auto":
            raise ConfigError(f"{toml_path}: project id '_auto' is reserved for auto-discovery")
        if project.id in seen_ids:
            raise ConfigError(f"{toml_path}: duplicate project id '{project.id}'")
        seen_ids.add(project.id)
        projects.append(project)
    return projects


def resolve_search_root(explicit: Path | None) -> Path:
    """Where the loader should start walking up to find `.claude/project-issues.toml`.

    Precedence:
      1. The explicit argument (used by tests).
      2. `PROJECT_ISSUES_PLUGIN_CWD` env var — escape hatch when Claude Code spawns
         the server in an unexpected directory.
      3. `CLAUDE_PROJECT_DIR` env var, in case Claude Code (or the user)
         sets it.
      4. The process's current working directory.
    """
    if explicit:
        return explicit.resolve()
    for var in ("PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR"):
        candidate = os.environ.get(var)
        if candidate:
            return Path(candidate).resolve()
    return Path.cwd().resolve()


class LoadResult(BaseModel):
    """What `load_projects` resolved, including provenance.

    `state` lets callers distinguish the four empty/non-empty cases without
    guessing from `len(projects)`:
      - "ok":            projects were loaded (from config or auto-discovery).
      - "config_empty":  TOML exists but has no `[[projects]]` entries.
      - "no_config":     no TOML and no usable git remote at github.com / gitlab.com.
      - "config_error":  TOML exists but failed to parse/validate (see `error`).
    """
    projects: list[ProjectConfig]
    state: Literal["ok", "config_empty", "no_config", "config_error"]
    config_file: str | None = None
    git_config: str | None = None
    search_root: str
    error: str | None = None


def load_projects(cwd: Path | None = None) -> LoadResult:
    """Resolve the project list for the configured working directory."""
    cwd = resolve_search_root(cwd)
    toml_path = _walk_up(cwd, ".claude/project-issues.toml")
    git_path = _walk_up(cwd, ".git/config")
    if toml_path:
        try:
            projects = _load_toml(toml_path)
        except (ConfigError, tomllib.TOMLDecodeError, OSError) as exc:
            return LoadResult(
                projects=[],
                state="config_error",
                config_file=str(toml_path),
                git_config=str(git_path) if git_path else None,
                search_root=str(cwd),
                error=f"failed to load {toml_path}: {exc}",
            )
        log.info("loaded %d project(s) from %s", len(projects), toml_path)
        return LoadResult(
            projects=projects,
            state="ok" if projects else "config_empty",
            config_file=str(toml_path),
            git_config=str(git_path) if git_path else None,
            search_root=str(cwd),
        )
    auto = _autodiscover_from_git(cwd)
    if auto:
        log.info("auto-discovered read-only project (%s %s)", auto.provider, auto.display_path)
        return LoadResult(
            projects=[auto],
            state="ok",
            git_config=str(git_path) if git_path else None,
            search_root=str(cwd),
        )
    log.info("no config and no usable git remote in %s", cwd)
    return LoadResult(
        projects=[],
        state="no_config",
        git_config=str(git_path) if git_path else None,
        search_root=str(cwd),
    )


def resolve_token(project: ProjectConfig) -> str | None:
    if not project.token_env:
        return None
    return os.environ.get(project.token_env)
