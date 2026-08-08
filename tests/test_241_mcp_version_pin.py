"""Regression test for ticket #241: the `mcp` dependency must exclude 2.x.

CI's `tests` workflow failed at collection with 49
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'` because
`pyproject.toml` declared `"mcp>=1.2"` with no upper bound. `mcp` 2.0.0
removed the `mcp.server.fastmcp` package (apparently renamed to
`mcp.server.mcpserver`), and every module under
`src/project_issues_plugin/tools/` imports `FastMCP` from the old path.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _dependencies() -> list[str]:
    pyproject = _repo_root() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["dependencies"]


def _mcp_specifier() -> SpecifierSet:
    found: Requirement | None = None
    for entry in _dependencies():
        # The two lib-python-* entries are `name @ git+https://...` URL
        # requirements; Requirement() parses those fine too, so the loop
        # doesn't need to special-case them.
        requirement = Requirement(entry)
        if requirement.name == "mcp":
            found = requirement
            break

    assert found is not None, "no 'mcp' entry found in project.dependencies"
    return found.specifier


def test_mcp_requirement_excludes_2_x() -> None:
    spec = _mcp_specifier()

    # Lower bound preserved -- guards against accidentally over-tightening
    # (e.g. to `==1.27.1`) while adding the ceiling.
    assert "1.2" in spec
    assert "1.27.1" in spec

    # Upper bound excludes the 2.x line that removed mcp.server.fastmcp.
    assert "2.0.0" not in spec
    assert "2.1.0" not in spec

    # Exercise the ceiling against a 2.0.0 pre-release explicitly.
    # SpecifierSet.__contains__ / .contains(prereleases=None) resolution
    # around pre-releases is subtle and version-dependent; passing
    # prereleases=True forces a real check against the ceiling regardless
    # of that default, rather than risking a silent tautology.
    assert not spec.contains("2.0.0rc1", prereleases=True)


def test_mcp_requirement_lookup_tolerates_url_requirements() -> None:
    """The name-matching loop must not crash on the two `name @ git+...`
    URL requirements (lib-python-config, lib-python-projects), and the
    'mcp' entry must actually be found -- a lookup miss must fail loudly,
    not silently skip past an empty match."""
    names = {Requirement(entry).name for entry in _dependencies()}
    assert "lib-python-config" in names
    assert "lib-python-projects" in names
    assert "mcp" in names


def test_installed_mcp_satisfies_declared_requirement() -> None:
    """Coverage (already passes): the environment this suite runs in
    already satisfies the declared specifier."""
    import importlib.metadata

    installed = importlib.metadata.version("mcp")
    assert installed in _mcp_specifier()


def test_fastmcp_import_path_exists() -> None:
    """Coverage (already passes): the exact import path used by
    tools/tickets.py:17, tools/comments.py:23, tools/relations.py:20,
    tools/projects.py:80 must succeed and expose FastMCP. This module
    imports nothing from `src/`, so it collects cleanly even when every
    other test module explodes on collection -- it would have surfaced
    #241 as one crisp failure carrying a version number instead of 49
    opaque collection errors.
    """
    from mcp.server.fastmcp import FastMCP

    assert FastMCP is not None
