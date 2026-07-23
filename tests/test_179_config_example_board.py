"""Regression test for ticket #179: `config.example.yml` never documented
the `board:` block, even though board support (list_board_columns, the
`column` filter, custom_fields via board binding) is fully shipped. This
left users with no discoverable starting point for configuring boards.

Unlike `tests/test_board_columns_169_170.py` (which stubs the provider
layer to test tool wiring), this test loads `config.example.yml` through
the **real** lib config loader — no monkeypatched fakes — so the `board:`
blocks are validated against the actual `Board`/`GithubProjectsV2Binding`/
`AzureBoardsBinding` pydantic schema (extra="forbid", so a typo'd sub-key
would fail this test with a `config_error` state).

Before the fix, `config.example.yml` had no `board:` key on any project
entry, so `acme-backend.board` / `acme-ado-frontend.board` were `None` and
every assertion below would fail.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lib_python_projects import load_projects


@pytest.fixture
def loaded_example_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Copy the real `config.example.yml` into a tmp `.seretos/projects.yml`
    and load it through the real, non-monkeypatched lib loader — mirrors
    how `project_issues_plugin.tools._providers._load_projects` invokes
    `load_projects` with `config_filename="projects.yml"` /
    `config_filename_alt="projects.yaml"` (see AGENTS.md)."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "config.example.yml"
    assert example.is_file(), f"missing {example}"

    # Project-boundary walk requires a `.git/`-bearing ancestor.
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    seretos_dir = tmp_path / ".seretos"
    seretos_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(example, seretos_dir / "projects.yml")

    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PROJECT_ISSUES_PLUGIN_CWD", str(tmp_path))

    return load_projects(
        config_filename="projects.yml", config_filename_alt="projects.yaml",
    )


def _by_id(result, project_id: str):
    matches = [p for p in result.projects if p.id == project_id]
    assert matches, f"project {project_id!r} not found in {[p.id for p in result.projects]}"
    return matches[0]


def test_config_loads_without_error(loaded_example_config) -> None:
    """The whole file must still parse cleanly — board schema is
    unknown-key-strict (extra='forbid'), so a typo'd sub-key would
    surface here as `state == 'config_error'`."""
    assert loaded_example_config.state == "ok", loaded_example_config.error
    assert loaded_example_config.error is None
    assert not loaded_example_config.invalid_projects


def test_github_project_has_board_with_github_projects_v2_binding(
    loaded_example_config,
) -> None:
    project = _by_id(loaded_example_config, "acme-backend")
    assert project.board is not None
    assert project.board.binding.kind == "github-projects-v2"


def test_azuredevops_project_has_board_with_azure_boards_binding(
    loaded_example_config,
) -> None:
    project = _by_id(loaded_example_config, "acme-ado-frontend")
    assert project.board is not None
    assert project.board.binding.kind == "azure-boards"


def test_gitlab_project_has_no_board(loaded_example_config) -> None:
    """GitLab has no board concept — the example must not add one."""
    project = _by_id(loaded_example_config, "read-only-gitlab-example")
    assert project.board is None


def test_github_binding_exposes_owner_and_project_number(
    loaded_example_config,
) -> None:
    binding = _by_id(loaded_example_config, "acme-backend").board.binding
    assert binding.owner == "acme"
    assert binding.project_number == 7


def test_azure_binding_exposes_team_and_board(loaded_example_config) -> None:
    binding = _by_id(loaded_example_config, "acme-ado-frontend").board.binding
    assert binding.team == "Web Team"
    assert binding.board == "Stories"


def test_github_board_columns_match_readme_example(loaded_example_config) -> None:
    project = _by_id(loaded_example_config, "acme-backend")
    assert project.board.columns == ["Todo", "Approved", "Doing", "Done"]


def test_azure_board_columns_match_readme_example(loaded_example_config) -> None:
    project = _by_id(loaded_example_config, "acme-ado-frontend")
    assert project.board.columns == ["New", "Approved", "Doing", "Done"]


# ---------------------------------------------------------------------------
# ticket agent-project-issues#230: `board.manage` (gates `ensure_board_column`)
# must be documented, commented-out, in both board-carrying example entries —
# the example must still load with the safe manage=False default.
# ---------------------------------------------------------------------------


def test_github_project_board_manage_defaults_false(loaded_example_config) -> None:
    project = _by_id(loaded_example_config, "acme-backend")
    assert project.permissions.board.manage is False


def test_azuredevops_project_board_manage_defaults_false(loaded_example_config) -> None:
    project = _by_id(loaded_example_config, "acme-ado-frontend")
    assert project.permissions.board.manage is False


def test_config_example_documents_board_manage_flag() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "config.example.yml"
    text = example.read_text(encoding="utf-8")
    # Commented (not live) — the example must keep manage=False by default.
    assert "board:" in text
    assert "manage: false" in text
    assert text.count("manage: false") >= 2, (
        "expected a commented `manage: false` under both board-carrying "
        "permissions blocks (acme-backend, acme-ado-frontend)"
    )
