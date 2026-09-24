"""Regression test for work package #385: bump `lib-python-projects`
v0.3.24 -> v0.3.25 (`lib-python-config` stays at whatever v0.3.25 itself
requires -- verified separately by `test_366_lib_pins.py::
test_installed_projects_requires_pinned_config_tag`, which reads the
installed lib's own `Requires-Dist` and needs no change here).

Floor-based convention (like #246/#254/.../#308/#319/#366/#375): the
*declared* tag must be an exact `vX.Y.Z` tag that is >= this ticket's floor
(a floor, not equality, so the tests survive future bumps -- the pin line
itself, an immutable exact tag, is what actually pins the exact version).
The pin-parsing helpers below are copied from `tests/test_375_lib_pins.py`
(itself copied from `tests/test_366_lib_pins.py`, from `tests/
test_319_lib_pins.py`), per the plan's instruction to follow the per-bump
floor convention and not edit an earlier ticket's floor test, which would
erase that ticket's record.

R1 (#385) projects pin bumped -- driving-test:
  - `test_projects_pin_meets_385_floor`: RED today -- declared and
    installed `0.3.24 < 0.3.25`.

R2 (config pin still matches what v0.3.25 requires) is `existing-suite` --
`test_366_lib_pins.py::test_installed_projects_requires_pinned_config_tag`
and `test_config_pin_meets_366_floor` already cover it dynamically.

R3 (pin comment names its own tag) is `existing-suite` --
`test_319_lib_pins.py::test_pin_comment_names_only_its_own_declared_tag`.

R4 (clean install/build/smoke stay green) is `ci-evidence` -- the
`.github/workflows/test.yml` PR run, not a test in this file.

Phase = tests: only the RED driving test + scaffolding here. No production
code (`pyproject.toml`) is touched in this file.
"""
from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_PROJECTS_FLOOR = Version("0.3.25")
_EXACT_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _pyproject_text() -> str:
    return (_repo_root() / "pyproject.toml").read_text(encoding="utf-8")


def _dependencies() -> list[str]:
    return tomllib.loads(_pyproject_text())["project"]["dependencies"]


def _entry(name: str) -> str:
    for entry in _dependencies():
        if Requirement(entry).name == name:
            return entry
    raise AssertionError(f"no {name!r} entry found in project.dependencies")


def _tag_from_url(entry: str) -> str:
    url = Requirement(entry).url or ""
    assert url, f"{entry!r} has no direct-reference URL to pull a tag from"
    return url.rsplit("@", 1)[-1].strip()


def _declared_tag(name: str) -> str:
    return _tag_from_url(_entry(name))


def _exact_version(name: str) -> Version:
    tag = _declared_tag(name)
    assert _EXACT_TAG_RE.match(tag), (
        f"{name} must be pinned to an exact vX.Y.Z tag, found ref {tag!r}"
    )
    return Version(tag.lstrip("v"))


def _installed_version(name: str) -> Version:
    """Wires the floor test directly to the installed environment's actual
    version (not just the declared pyproject.toml text), so a declared-but-
    not-installed bump is caught by the floor test itself -- see
    `test_366_lib_pins.py::_installed_version`'s docstring for the
    originating test-critic finding this mirrors."""
    try:
        return Version(importlib.metadata.version(name))
    except importlib.metadata.PackageNotFoundError:
        raise AssertionError(
            f"{name} is not installed; run `pwsh scripts/test.ps1` "
            f"(or scripts/sync-libs.ps1) to install the pinned tag"
        )


def test_projects_pin_meets_385_floor() -> None:
    """Driving test (R1). RED today: pyproject.toml declares
    lib-python-projects @v0.3.24, which is below this ticket's v0.3.25
    floor, and the installed version is 0.3.24 too."""
    assert _exact_version("lib-python-projects") >= _PROJECTS_FLOOR
    assert _installed_version("lib-python-projects") >= _PROJECTS_FLOOR
