"""Tests for ticket agent-project-issues#252: echo `board.manage` in the
per-project `permissions` payload returned by `list_projects` /
`search_projects`.

Ticket #252's remaining in-repo scope (per the ticket's follow-up comment)
is narrowed to exactly this one item: `ensure_board_column` already gates
writes on `project.permissions.board.manage` via `_require_board_manage`
(see `tests/test_230_ensure_board_column.py`), but an agent had no way to
*discover* that gate result ahead of time — `permissions` in `list_projects`
/ `search_projects` only ever exposed `read` / `issues` / `pulls`. This
module proves the new `board` namespace is present, sourced verbatim from
config (never derived by the token-probe branch, which has no board
concept), and that the deliberately out-of-scope `pipelines` namespace is
NOT added.

Mirrors the stubbing patterns of `tests/test_230_ensure_board_column.py`
(hand-built `ProjectConfig` + permissions dict) and
`tests/test_projects_diagnostics.py` (stub-MCP registration against
`project_issues_plugin.tools.projects`, and the token-probe fixture set).
"""
from __future__ import annotations

from typing import Callable

import pytest

from lib_python_projects import ProjectConfig, ProjectsLoadResult
from project_issues_plugin.tools import projects as proj_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _make_stub_tools(monkeypatch: pytest.MonkeyPatch, fake_result: ProjectsLoadResult) -> dict:
    monkeypatch.setattr(proj_tools, "load_projects", lambda **_: fake_result)
    stub = _StubMCP()
    proj_tools.register(stub)
    return stub.tools


def _config_project(
    project_id: str = "acme",
    *,
    board_manage: bool | None = True,
    no_board_block: bool = False,
) -> ProjectConfig:
    permissions: dict = {
        "issues": {"create": True, "modify": True},
        "pulls": {"create": True, "modify": True, "merge": True},
    }
    if not no_board_block:
        permissions["board"] = {"manage": bool(board_manage)}
    return ProjectConfig(
        id=project_id,
        provider="github",
        path=f"{project_id}/backend",
        token_env=f"TOKEN_{project_id.upper()}",
        permissions=permissions,
    )


@pytest.fixture
def _clean_probe_cache():
    proj_tools._probe_cache_clear()
    yield
    proj_tools._probe_cache_clear()


# ---------------------------------------------------------------------------
# Behaviour 1 — list_projects echoes board.manage from config, both truthy
# and falsy, including when no `board:` block is configured at all.
# ---------------------------------------------------------------------------


def test_list_projects_config_project_board_manage_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _config_project(board_manage=True)
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()

    p = out["projects"][0]
    assert p["permissions"]["board"]["manage"] is True


def test_list_projects_config_project_board_manage_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _config_project(board_manage=False)
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()

    p = out["projects"][0]
    assert p["permissions"]["board"]["manage"] is False


def test_list_projects_no_board_block_defaults_to_false_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project with no `board:` block configured at all must still yield
    `manage is False` (the lib's own default) — never missing or None."""
    project = _config_project(no_board_block=True)
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()

    p = out["projects"][0]
    assert "board" in p["permissions"]
    assert p["permissions"]["board"]["manage"] is False


# ---------------------------------------------------------------------------
# Behaviour 2 — search_projects exposes the same field on both the
# empty-query "list all" path and the scored-match path.
# ---------------------------------------------------------------------------


def test_search_projects_empty_query_path_exposes_board_manage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _config_project(board_manage=True)
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["search_projects"](query="")

    assert len(out["matches"]) == 1
    assert out["matches"][0]["permissions"]["board"]["manage"] is True


def test_search_projects_scored_match_path_exposes_board_manage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _config_project(project_id="acme", board_manage=True)
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["search_projects"](query="acme")

    assert len(out["matches"]) == 1
    assert out["matches"][0]["permissions"]["board"]["manage"] is True


# ---------------------------------------------------------------------------
# Behaviour 3 — token-probe / auto-discovered project path: board.manage is
# present and False (the probe has no board concept), while issues/pulls/
# permissions_source probe behavior is unchanged.
# ---------------------------------------------------------------------------


def _autodiscovered_project(path: str = "acme/backend") -> ProjectConfig:
    return ProjectConfig(
        id="_auto",
        description="Auto-discovered from git remote",
        provider="github",
        path=path,
        token_env="GITHUB_TOKEN",
        source="git-remote",
    )


def test_token_probe_project_board_manage_false_issues_pulls_unaffected(
    monkeypatch: pytest.MonkeyPatch, _clean_probe_cache,
) -> None:
    from lib_python_projects.providers.base import TokenCapabilities
    from project_issues_plugin.tools import _providers as providers_mod

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
    # Board has no probe concept — must stay False, never derived from caps.
    assert p["permissions"]["board"]["manage"] is False
    # Existing probe behavior for issues/pulls/permissions_source unchanged.
    assert p["permissions_source"] == "token-probe"
    assert "permissions_probe_error" not in p
    assert p["permissions"]["verified"] is True
    assert p["permissions"]["reason"] is None
    assert p["permissions"]["issues"]["create"] is True
    assert p["permissions"]["pulls"]["merge"] is False


# ---------------------------------------------------------------------------
# Behaviour 4 — docstrings mention `board` in a permissions context.
# ---------------------------------------------------------------------------


def test_list_projects_and_search_projects_docstrings_mention_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_result = ProjectsLoadResult(projects=[], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)
    assert "board" in tools["list_projects"].__doc__
    assert "board" in tools["search_projects"].__doc__


# ---------------------------------------------------------------------------
# Behaviour 5 — scope guard: `pipelines` is deliberately NOT added.
# ---------------------------------------------------------------------------


def test_list_projects_scope_guard_no_pipelines_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _config_project()
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"]()

    p = out["projects"][0]
    assert "pipelines" not in p["permissions"]


# ---------------------------------------------------------------------------
# Behaviour 6 — regression guard: fields="light" output is unchanged (no
# `permissions` key at all) for both tools.
# ---------------------------------------------------------------------------


def test_list_projects_light_fields_still_has_no_permissions_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _config_project()
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["list_projects"](fields="light")

    p = out["projects"][0]
    assert "permissions" not in p


def test_search_projects_light_fields_still_has_no_permissions_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _config_project()
    fake_result = ProjectsLoadResult(projects=[project], state="ok", search_root="/tmp")
    tools = _make_stub_tools(monkeypatch, fake_result)

    out = tools["search_projects"](query="", fields="light")

    m = out["matches"][0]
    assert "permissions" not in m
