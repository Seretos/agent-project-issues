"""Project configuration loading.

Resolves the project list for the server's current working directory:

  1. Walk up from CWD looking for `.claude/project-issues.yml` (or
     `.yaml`). If found, parse it, load the referenced `.env`, and
     return the configured projects.
  2. Otherwise, walk up looking for `.git/config` and inspect the
     `origin` remote URL. For github.com / gitlab.com, synthesize a
     single read-only project with id="_auto".
  3. Otherwise, return an empty list.

YAML schema (v1):

    version: 1
    env_file: .env            # optional, relative to the config file

    projects:
      - id: acme-backend
        description: Acme main backend
        provider: github
        path: acme/backend     # owner/repo for GitHub, group/sub/repo
                               # for GitLab — replaces the old
                               # owner+repo / project_path triple.
        token_env: GITHUB_TOKEN_ACME
        permissions:
          issues:
            create: true
            modify: true
          pulls:
            create: true
            modify: false
            merge:  false

The schema is **strict**: unknown top-level / project / permissions keys
are rejected with a clear error.

The previously-shipped TOML format and the flat `permissions` form
(`{create, modify, pr_create, pr_modify}`) are no longer accepted —
this is a hard breaking change (see ticket #8). Migrating from the
old TOML schema is a literal field-by-field copy:

    [[projects]]                    projects:
    id = "x"                          - id: x
    provider = "github"                 provider: github
    owner = "acme"                      path: acme/backend
    repo = "backend"                    permissions:
                                          issues: {create: true, ...}
"""
from __future__ import annotations

import io
import logging
import os
import re
import warnings
from configparser import ConfigParser
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from ruamel.yaml import YAML, YAMLError

log = logging.getLogger("project-issues.config")

Provider = Literal["github", "gitlab"]
Source = Literal["config", "git-remote"]


class IssuesPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    create: bool = False
    modify: bool = False


class PullsPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    create: bool = False
    modify: bool = False
    merge: bool = False


class Permissions(BaseModel):
    """Nested permissions namespace.

    The legacy flat form (`{create, modify, pr_create, pr_modify}`)
    was removed in v1 of the YAML schema — see ticket #8.
    """

    model_config = ConfigDict(extra="forbid")
    issues: IssuesPermissions = Field(default_factory=IssuesPermissions)
    pulls: PullsPermissions = Field(default_factory=PullsPermissions)


class ProjectConfig(BaseModel):
    """A single project entry.

    `path` is the provider-native repo identifier:
      - GitHub: `"owner/repo"` (e.g. `"Seretos/agent-project-issues"`)
      - GitLab: full namespace path (e.g. `"group/sub/project"`)

    The legacy split into `owner`/`repo`/`project_path` is gone from
    the YAML schema; for backward compatibility the internal code
    keeps accessing `project.owner` / `project.repo` / `project.project_path`
    via derived properties so the GitHub provider doesn't need a
    rewrite.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = ""
    provider: Provider
    path: str | None = None
    base_url: str | None = None
    token_env: str | None = None
    permissions: Permissions = Field(default_factory=Permissions)
    source: Source = "config"

    @model_validator(mode="after")
    def _check_provider_fields(self) -> "ProjectConfig":
        if not self.path:
            raise ValueError(
                f"project '{self.id}' is missing required field 'path' "
                f"(provider-native repo path, e.g. 'owner/repo' for github)"
            )
        if self.provider == "github":
            if "/" not in self.path or self.path.count("/") < 1:
                raise ValueError(
                    f"project '{self.id}': github 'path' must be "
                    f"'owner/repo', got {self.path!r}"
                )
        return self

    # --- Backward-compat derived properties ----------------------------------

    @property
    def owner(self) -> str | None:
        """GitHub owner derived from `path` (`"owner/repo"`)."""
        if self.provider != "github" or not self.path or "/" not in self.path:
            return None
        return self.path.split("/", 1)[0]

    @property
    def repo(self) -> str | None:
        """GitHub repo derived from `path`."""
        if self.provider != "github" or not self.path or "/" not in self.path:
            return None
        return self.path.split("/", 1)[1]

    @property
    def project_path(self) -> str | None:
        """GitLab project path — same as `path` for the gitlab provider."""
        return self.path if self.provider == "gitlab" else None

    @property
    def display_path(self) -> str:
        return self.path or ""

    @property
    def web_url(self) -> str | None:
        if self.provider == "github":
            return f"https://github.com/{self.path}"
        if self.provider == "gitlab":
            base = (self.base_url or "https://gitlab.com").rstrip("/")
            return f"{base}/{self.path}"
        return None


class ConfigDocument(BaseModel):
    """Top-level YAML document shape.

    `version` defaults to 1 when omitted — this preserves the simplest
    happy-path for tiny configs while still letting a future v2 break
    cleanly. Strict on unknown top-level keys (`extra="forbid"`).
    """

    model_config = ConfigDict(extra="forbid")
    version: int = 1
    env_file: str | None = None
    projects: list[dict[str, Any]] = Field(default_factory=list)


class ConfigError(Exception):
    pass


def _walk_up(start: Path, names: tuple[str, ...]) -> Path | None:
    """Walk up from `start` looking for the first existing path that ends
    with any of `names`. `names` are relative to each visited directory.
    """
    cur = start.resolve()
    while True:
        for name in names:
            candidate = cur / name
            if candidate.exists():
                return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def _load_env_file(path: Path) -> None:
    """Tiny .env parser — KEY=value lines, optional quotes, # comments.

    Does not overwrite entries already present in os.environ, so explicit
    process-env always wins over file-env. Tolerates a leading UTF-8 BOM.
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
    git_config_path = _walk_up(start, (".git/config",))
    if not git_config_path:
        return None
    cp = ConfigParser()
    try:
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
        if "/" not in path:
            return None
        return ProjectConfig(
            id="_auto",
            description=f"Auto-discovered from git remote ({url.strip()})",
            provider="github",
            path=path,
            token_env="GITHUB_TOKEN",
            source="git-remote",
        )
    if host == "gitlab.com":
        return ProjectConfig(
            id="_auto",
            description=f"Auto-discovered from git remote ({url.strip()})",
            provider="gitlab",
            path=path,
            token_env="GITLAB_TOKEN",
            source="git-remote",
        )
    log.info("auto-discovery skipped — host %s is not github.com or gitlab.com", host)
    return None


# Single, reusable YAML parser (safe by default — no arbitrary tag
# instantiation). Configured for line-/column-preserving round-trip so
# error messages can reference the exact source location.
_yaml = YAML(typ="safe", pure=True)


def _load_yaml(yaml_path: Path) -> list[ProjectConfig]:
    raw = yaml_path.read_bytes()
    # Strip a UTF-8 BOM if present (Notepad / `Set-Content -Encoding utf8`).
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = _yaml.load(io.BytesIO(raw))
    except YAMLError as exc:
        raise ConfigError(f"{yaml_path}: YAML parse error: {exc}") from exc

    # Empty file -> empty document.
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{yaml_path}: top-level must be a mapping, got {type(data).__name__}"
        )

    try:
        doc = ConfigDocument.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{yaml_path}: schema error: {exc}") from exc

    if doc.version != 1:
        raise ConfigError(
            f"{yaml_path}: unsupported config schema version "
            f"{doc.version} (this server understands v1)"
        )

    # Locate the .env file. If `env_file` is explicitly set, respect it
    # (resolved relative to the config file). Otherwise check the project
    # root first (the conventional .env location) and then the config
    # directory as a fallback.
    if doc.env_file is not None:
        candidates = [(yaml_path.parent / doc.env_file).resolve()]
    else:
        candidates = [
            (yaml_path.parent.parent / ".env").resolve(),
            (yaml_path.parent / ".env").resolve(),
        ]
    for env_path in candidates:
        if env_path.exists():
            _load_env_file(env_path)
            break

    projects: list[ProjectConfig] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(doc.projects):
        if not isinstance(item, dict):
            raise ConfigError(
                f"{yaml_path}: projects[{idx}]: must be a mapping"
            )
        try:
            project = ProjectConfig.model_validate({**item, "source": "config"})
        except ValidationError as exc:
            raise ConfigError(
                f"{yaml_path}: projects[{idx}]: {exc}"
            ) from exc
        if project.id == "_auto":
            raise ConfigError(
                f"{yaml_path}: project id '_auto' is reserved for "
                "auto-discovery"
            )
        if project.id in seen_ids:
            raise ConfigError(
                f"{yaml_path}: duplicate project id '{project.id}'"
            )
        seen_ids.add(project.id)
        projects.append(project)
    return projects


# Backwards-compat alias (a few tests imported the old name directly).
_load_toml = _load_yaml  # pragma: no cover — alias, will be dropped post-migration


def resolve_search_root(explicit: Path | None) -> Path:
    """Where the loader should start walking up to find the config.

    Precedence:
      1. The explicit argument (used by tests).
      2. `PROJECT_ISSUES_PLUGIN_CWD` env var — escape hatch when Claude
         Code spawns the server in an unexpected directory.
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

    `state` lets callers distinguish the four empty/non-empty cases:
      - "ok":            projects were loaded (from config or auto-discovery).
      - "config_empty":  config exists but has no `projects:` entries.
      - "no_config":     no config and no usable git remote.
      - "config_error":  config exists but failed to parse/validate.
    """
    projects: list[ProjectConfig]
    state: Literal["ok", "config_empty", "no_config", "config_error"]
    config_file: str | None = None
    git_config: str | None = None
    search_root: str
    error: str | None = None


# Config-file candidate names, in priority order. The YAML extension is
# preferred — `.yml` first because that's the Worktree-Plugin convention.
_CONFIG_CANDIDATES = (
    ".claude/project-issues.yml",
    ".claude/project-issues.yaml",
)


def load_projects(cwd: Path | None = None) -> LoadResult:
    """Resolve the project list for the configured working directory."""
    cwd = resolve_search_root(cwd)
    config_path = _walk_up(cwd, _CONFIG_CANDIDATES)
    git_path = _walk_up(cwd, (".git/config",))
    if config_path:
        try:
            projects = _load_yaml(config_path)
        except (ConfigError, OSError) as exc:
            return LoadResult(
                projects=[],
                state="config_error",
                config_file=str(config_path),
                git_config=str(git_path) if git_path else None,
                search_root=str(cwd),
                error=f"failed to load {config_path}: {exc}",
            )
        log.info("loaded %d project(s) from %s", len(projects), config_path)
        return LoadResult(
            projects=projects,
            state="ok" if projects else "config_empty",
            config_file=str(config_path),
            git_config=str(git_path) if git_path else None,
            search_root=str(cwd),
        )
    auto = _autodiscover_from_git(cwd)
    if auto:
        log.info(
            "auto-discovered read-only project (%s %s)",
            auto.provider, auto.display_path,
        )
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


# `warnings` is no longer used by this module (flat-permissions migration
# was removed in v1 of the YAML schema). Keep the import alive for
# downstream consumers that re-export it; ruff would flag unused otherwise.
_ = warnings  # noqa: F401
