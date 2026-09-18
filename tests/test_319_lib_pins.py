"""Regression tests for package #319: pin BOTH libs to exact immutable tags.

`lib-python-config` moves from the floating `@release/0.x` branch to the exact
tag v0.1.2, and `lib-python-projects` is bumped v0.3.17 -> v0.3.19.

Floor-based convention (like #246/#254/.../#308): the *declared* tag must be an
exact `vX.Y.Z` tag that is >= this ticket's floor (a floor, not equality, so
the tests survive future bumps; a stray v0.1.3 would still pass R1 but the
acceptance values are pinned at this ticket by the floors plus R2). The installed-vs-declared test (R2) is what proves the real environment
holds exactly the declared versions; together with the floors that pins the
acceptance criterion (config 0.1.2 / projects 0.3.19 at this ticket).
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

_CONFIG_FLOOR = Version("0.1.2")
_PROJECTS_FLOOR = Version("0.3.19")
_TAG_RE = re.compile(r"v\d+\.\d+\.\d+")
_EXACT_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
# The moving-branch ref and its "floats on" description. A ref literal, not
# wording: no legitimate use of either remains once both libs are exact tags.
_FLOATING_RE = re.compile(r"release/0\.x|floats on", re.IGNORECASE)
# sync-libs.ps1's real regex, copied verbatim from the script.
_SYNC_LIBS_PATTERN = re.compile(r'"(lib-python-[^"]+@[^"]+)"')


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
    return entry.rsplit("@", 1)[-1].strip()


def _declared_tag(name: str) -> str:
    return _tag_from_url(_entry(name))


def _exact_version(name: str) -> Version:
    tag = _declared_tag(name)
    assert _EXACT_TAG_RE.match(tag), (
        f"{name} must be pinned to an exact vX.Y.Z tag, found ref {tag!r}"
    )
    return Version(tag.lstrip("v"))


def test_config_pin_is_exact_tag_meeting_floor() -> None:
    """Driving test (R1). RED: config currently ends in `@release/0.x`."""
    assert _exact_version("lib-python-config") >= _CONFIG_FLOOR


def test_projects_pin_meets_v0_3_19_floor() -> None:
    """Driving test (R1). RED: projects currently declares v0.3.17."""
    assert _exact_version("lib-python-projects") >= _PROJECTS_FLOOR


def test_both_entries_still_parse_and_match_sync_libs_pattern() -> None:
    """Edge coverage: every dependency parses, both lib entries keep the
    git+https URL prefix and match sync-libs.ps1's lib-python-* regex."""
    for entry in _dependencies():
        Requirement(entry)
    for name in ("lib-python-config", "lib-python-projects"):
        entry = _entry(name)
        assert f"git+https://github.com/Seretos/{name}@" in entry
    captured = _SYNC_LIBS_PATTERN.findall(_pyproject_text())
    assert len(captured) == 2, captured
    for cap in captured:
        assert _EXACT_TAG_RE.match(cap.rsplit("@", 1)[-1]), (
            f"sync-libs regex would capture a non-tag ref: {cap!r}"
        )


def test_installed_libs_match_declared_pins() -> None:
    """Driving test (R2): the installed environment holds exactly the declared
    versions (acceptance criterion, measured via importlib.metadata). Catches a
    local editable shadow in either direction."""
    for name in ("lib-python-config", "lib-python-projects"):
        declared = _exact_version(name)  # tag `v0.1.2` -> version 0.1.2 (v stripped)
        try:
            installed = Version(importlib.metadata.version(name))
        except importlib.metadata.PackageNotFoundError:
            raise AssertionError(
                f"{name} is not installed; run `pwsh scripts/test.ps1` "
                f"(or scripts/sync-libs.ps1) to install the pinned tag"
            )
        assert installed == declared, (
            f"installed {name} is {installed} but pyproject.toml pins "
            f"{declared}; run `pwsh scripts/test.ps1` to re-sync"
        )


def _comment_block(name: str) -> str:
    """The contiguous `#` comment lines directly above `name`'s dependency."""
    lines = _pyproject_text().splitlines()
    dep_idx = next(i for i, line in enumerate(lines) if f"{name} @ git+" in line)
    start = dep_idx
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    return " ".join(lines[start:dep_idx])


@pytest.mark.parametrize("name", ["lib-python-config", "lib-python-projects"])
def test_pin_comment_names_only_its_own_declared_tag(name: str) -> None:
    """Driving test (R4, symmetric for both libs): each dependency's comment
    block names ONLY its own declared vX.Y.Z tag (a stale tag such as v0.3.17
    on the projects block fails, as does a block naming no tag). The rationale
    wording of the comment is verified by review, not mechanically."""
    declared = _declared_tag(name)
    found = _TAG_RE.findall(_comment_block(name))
    assert found, f"comment above {name} names no vX.Y.Z tag"
    assert set(found) == {declared}, (
        f"comment above {name} mentions tags {found!r}, declared pin is {declared!r}"
    )


def test_release_branch_ref_absent_from_pin_artefacts() -> None:
    """Driving test (R3): the moving-branch ref `release/0.x` and the phrase
    `floats on` appear in none of the files that carried the old config scheme.
    RED: all five files still contain `release/0.x`."""
    offenders = {}
    for rel in (
        "pyproject.toml",
        "scripts/sync-libs.ps1",
        "scripts/test.ps1",
        ".github/workflows/test.yml",
        "AGENTS.md",
    ):
        text = (_repo_root() / rel).read_text(encoding="utf-8")
        hits = sorted({m.group(0).lower() for m in _FLOATING_RE.finditer(text)})
        if hits:
            offenders[rel] = hits
    assert not offenders, f"moving-branch ref still present: {offenders}"
