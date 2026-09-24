"""Regression test for work package #375: `lib-python-projects` v0.3.24
adds support for `board.columns` on a project with no `board.binding` (a
"label-mode" board -- the agent reasons about logical columns, but no
provider-native board is wired up). Before v0.3.24, `Board.binding` was a
required field, so a `board:` block with only `columns` failed pydantic
validation and the whole project entry was silently dropped into
`invalid_projects`, disappearing from `list_projects`.

Board/binding parsing logic lives entirely in `lib-python-projects`, per
AGENTS.md ("this repo only wires the lib's surface into MCP tools") -- so
this repo needs no production-code change. This test proves the *pinned,
installed* lib now accepts the binding-less board, through this repo's own
`list_projects` tool surface, using the real lib loader (no monkeypatched
`load_projects`, no fake provider) -- mirroring the `configured` fixture
pattern in `tests/test_projects_diagnostics.py` and the real-loader pattern
in `tests/test_179_config_example_board.py`.

R4 -- driving-test:
  - `test_label_board_project_listed`: RED on v0.3.23 -- `label-board` (only
    `board.columns`, no `binding`) is rejected by the lib and missing from
    `list_projects`; `plain-control` (no board at all) is present, ruling
    out a broken fixture. GREEN once v0.3.24 is installed: both ids listed.
  - `test_label_board_parsed_by_lib`: additional edge-case coverage --
    calls the lib's `load_projects` directly (same setup) and asserts
    `invalid_projects` is empty and the parsed `Board` carries
    `columns == ["Todo", "Doing", "Done"]` and `binding is None`.

Both test projects clear `GITHUB_TOKEN` / `GITLAB_TOKEN` / `AZURE_DEVOPS_TOKEN`
(plus the vars the `configured` fixture in test_projects_diagnostics.py
clears) so no token-discovery pass or network call can happen -- the two
configured projects are the only possible source of `list_projects`'
result.

Phase = tests: only the RED driving test + scaffolding here. No production
code is touched in this file (board/binding logic lives in the lib).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from lib_python_projects import load_projects
from project_issues_plugin.tools import projects as proj_tools


def _write_cfg(tmp_path: Path) -> None:
    # Project-boundary walk requires `.git/` at the repo root the config
    # lives in. Plant an empty one -- the resolver only checks existence.
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / ".seretos" / "projects.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(textwrap.dedent(
        """
        version: 1
        projects:
          - id: plain-control
            provider: github
            path: acme/plain-control
          - id: label-board
            provider: github
            path: acme/label-board
            board:
              columns:
                - Todo
                - Doing
                - Done
        """
    ).lstrip())


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "PROJECT_ISSUES_CONFIG", "PROJECT_ISSUES_PLUGIN_ROOT",
        "PROJECT_ISSUES_PLUGIN_CWD", "CLAUDE_PROJECT_DIR",
        "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE",
        "PROJECT_ISSUES_DEBUG",
        # No token-discovery / network call must be reachable -- the only
        # projects `list_projects` can return are the two defined below.
        "GITHUB_TOKEN", "GITLAB_TOKEN", "AZURE_DEVOPS_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stand up `plain-control` (no board) and `label-board` (board with
    columns, no binding), and register the real tools against a stub MCP --
    no monkeypatched `load_projects`, no fake provider."""
    _write_cfg(tmp_path)
    _clear_env(monkeypatch)
    monkeypatch.setenv("PROJECT_ISSUES_PLUGIN_CWD", str(tmp_path))

    captured: dict = {}

    class _Stub:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    proj_tools.register(_Stub())
    return captured


def test_label_board_project_listed(configured: dict) -> None:
    """Driving test. RED on v0.3.23: `label-board` is missing from the
    result (dropped into the lib's `invalid_projects`) while
    `plain-control` is present -- proving the fixture itself is sound and
    the drop is specific to the binding-less board. GREEN on v0.3.24: both
    ids are listed."""
    out = configured["list_projects"](fields="light")
    assert out["state"] == "ok"
    ids = {p["id"] for p in out["projects"]}
    assert "plain-control" in ids, ids
    assert "label-board" in ids, ids


def test_label_board_parsed_by_lib(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Additional edge-case coverage: the real lib loader (not the MCP tool
    surface) parses the binding-less board cleanly -- no entry lands in
    `invalid_projects`, and the parsed `Board` carries the declared columns
    with `binding is None`."""
    _write_cfg(tmp_path)
    _clear_env(monkeypatch)
    monkeypatch.setenv("PROJECT_ISSUES_PLUGIN_CWD", str(tmp_path))

    result = load_projects(
        config_filename="projects.yml", config_filename_alt="projects.yaml",
    )
    assert result.invalid_projects == [], result.invalid_projects
    by_id = {p.id: p for p in result.projects}
    assert "label-board" in by_id, by_id.keys()
    board = by_id["label-board"].board
    assert board is not None
    assert board.columns == ["Todo", "Doing", "Done"]
    assert board.binding is None
