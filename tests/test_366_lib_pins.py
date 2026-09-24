"""Regression tests for work package #366 (children #356/#374): bump BOTH
libs together, `lib-python-config` v0.1.2 -> v0.1.3 and `lib-python-projects`
v0.3.22 -> v0.3.23.

Floor-based convention (like #246/#254/.../#308/#319): the *declared* tag
must be an exact `vX.Y.Z` tag that is >= this ticket's floor (a floor, not
equality, so the tests survive future bumps). The pin-parsing helpers below
are copied from `tests/test_319_lib_pins.py` (simplifier::F1 from the round-1
plan critique flags this duplication as a minor, non-blocking note -- kept as
a straight copy here per the plan's own instruction to follow the per-bump
floor convention and not edit test_319's floors, which would erase that
ticket's record; sharing the helpers via a common module is a possible future
cleanup, not scoped to this ticket).

R4 (#356/#374) both pins bumped together -- driving-test:
  - `test_config_pin_meets_366_floor` / `test_projects_pin_meets_366_floor`:
    RED today -- declared `0.1.2 < 0.1.3` and `0.3.22 < 0.3.23`.
  - `test_installed_libs_match_declared_pins` (R2, copied from test_319):
    already exists in test_319 and re-asserted there against the new floors
    implicitly via the *declared* pin -- not duplicated here since it reads
    the declared pin dynamically, not a floor.
  - `test_installed_projects_requires_pinned_config_tag`: additional
    edge-case coverage guarding the "lib-python-projects v0.3.23 pins
    lib-python-config exactly v0.1.3" premise the plan's Approach calls out
    as only partially checked at plan time (network access was unavailable
    to the planner). Passes today (0.3.22 already requires exactly v0.1.2,
    matching today's pyproject pin) and must keep passing once both pins are
    bumped together.

R5 (build still succeeds with new pins) is `ci-evidence` -- the PyInstaller
`build` + `smoke` jobs of `.github/workflows/test.yml` on the PR run, not a
test in this file.

Phase = tests: only RED driving tests + compile-level scaffolding here. No
production code (`pyproject.toml`) is touched in this file.
"""
from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_CONFIG_FLOOR = Version("0.1.3")
_PROJECTS_FLOOR = Version("0.3.23")
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
    # Test-critic round-1 F4: pull the tag from the PARSED PEP 508 direct-
    # reference URL (`Requirement(entry).url`), not from a raw split of the
    # whole dependency-line text -- this survives reformatting of the
    # `name @ url` entry itself (extra whitespace, a future extras marker,
    # etc.) that a blind `entry.rsplit("@", 1)` would be sensitive to.
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
    """Test-critic round-4 F2: the floor tests below only parsed the
    DECLARED tag out of pyproject.toml -- a text-only pin edit with no
    re-sync of the installed libs passed all three, and was only caught by
    the separately-existing `test_installed_libs_match_declared_pins` /
    `test_installed_projects_requires_pinned_config_tag` further down in
    this file, a different test that could be skipped, deleted, or fail to
    run without the floor tests themselves noticing. This helper wires the
    floor tests directly to the installed environment's actual version via
    `importlib.metadata.version`, so a declared-but-not-installed bump is
    caught by the floor test itself."""
    try:
        return Version(importlib.metadata.version(name))
    except importlib.metadata.PackageNotFoundError:
        raise AssertionError(
            f"{name} is not installed; run `pwsh scripts/test.ps1` "
            f"(or scripts/sync-libs.ps1) to install the pinned tag"
        )


# ===========================================================================
# R4 -- both pins bumped together to meet the #366 floors
# ===========================================================================


def test_config_pin_meets_366_floor() -> None:
    """Driving test. RED today: pyproject.toml declares lib-python-config
    @v0.1.2, which is below this ticket's v0.1.3 floor.

    Behavioural grounding for test-critic round-3 F4: this test (and
    `test_projects_pin_meets_366_floor` / `test_pin_comment_names_366_
    floor_tag` below) used to only parse the DECLARED tag/comment strings
    out of `pyproject.toml` -- a `pyproject.toml` edited to the right tags
    but never reinstalled would still pass all three, leaving the
    installed-environment reality to be caught only by the separately-
    existing `test_installed_libs_match_declared_pins` /
    `test_installed_projects_requires_pinned_config_tag` further down in
    this file. Test-critic round-4 F2: this floor test now also asserts the
    INSTALLED version directly (`_installed_version`), so a declared-but-
    not-installed bump is caught by the floor test itself, not only by a
    separate test that happens to exist alongside it."""
    assert _exact_version("lib-python-config") >= _CONFIG_FLOOR
    assert _installed_version("lib-python-config") >= _CONFIG_FLOOR


def test_projects_pin_meets_366_floor() -> None:
    """Driving test. RED today: pyproject.toml declares lib-python-projects
    @v0.3.22, which is below this ticket's v0.3.23 floor.

    Test-critic round-4 F2: also asserts the INSTALLED version directly
    (see `test_config_pin_meets_366_floor`'s docstring for why)."""
    assert _exact_version("lib-python-projects") >= _PROJECTS_FLOOR
    assert _installed_version("lib-python-projects") >= _PROJECTS_FLOOR


# Test-critic round-5 F1 (critical, resolved by deletion): a
# `test_pin_comment_names_366_floor_tag` used to live here, asserting (a)
# the comment block above each dependency names only its own declared tag
# and meets this ticket's floor, and (b) the installed version meets the
# same floor. Comment text has zero runtime effect, so (a) is inherently a
# check of literal text in a config file -- and it duplicates coverage that
# already exists: `tests/test_319_lib_pins.py::
# test_pin_comment_names_only_its_own_declared_tag` already asserts,
# generically (not tied to any one ticket's floor), that the comment block
# above each dependency names ONLY that dependency's currently-declared
# tag -- it reads `_declared_tag(name)` dynamically off `pyproject.toml`,
# so once this ticket's pins are bumped, that existing test only passes if
# the comments are updated to say `(v0.1.3)` / `(v0.3.23)` too. That is
# exactly the "comment update" acceptance criterion from the plan's
# Approach (#356/#374), and the plan itself already names test_319 as its
# existing-suite coverage for it. Part (b), the installed-version floor
# check, was a verbatim repeat of `test_config_pin_meets_366_floor` /
# `test_projects_pin_meets_366_floor` below. With both halves already
# covered elsewhere, nothing here was left that wasn't either tautological
# (comment text) or duplicate (installed-version floor) -- so the test is
# deleted rather than further heuristic-patched. See R4's docstring note
# above and the plan's Mechanism-balance section, which already flags
# test_319 as the existing-suite owner of the comment-tag invariant.


# ---------- Additional edge-case coverage -------------------------------------


def test_both_entries_still_parse_and_declare_exact_tags() -> None:
    """Edge coverage: every dependency parses, both lib entries keep the
    git+https URL prefix and an exact vX.Y.Z tag. Already passes today
    (existing invariant, unrelated to the floor bump)."""
    for entry in _dependencies():
        Requirement(entry)
    for name in ("lib-python-config", "lib-python-projects"):
        assert _entry(name).startswith(f"{name} @ git+https://")
        _exact_version(name)  # raises if not an exact tag


def test_installed_libs_match_declared_pins() -> None:
    """Driving test (R4, copied from test_319's R2): the installed
    environment holds exactly the declared versions (acceptance criterion,
    measured via importlib.metadata). RED once pyproject.toml is bumped but
    `pwsh scripts/test.ps1` / `scripts/sync-libs.ps1` hasn't re-synced the
    environment yet -- this is the test that forces that sync step."""
    for name in ("lib-python-config", "lib-python-projects"):
        declared = _exact_version(name)
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


def test_installed_projects_requires_pinned_config_tag() -> None:
    """Additional edge-case coverage: guards the plan's premise
    ("lib-python-projects v0.3.23 pins lib-python-config exactly v0.1.3"),
    which the planner could only partially verify (no network access to
    inspect the not-yet-installed v0.3.23 METADATA). Reads the INSTALLED
    lib-python-projects package's own declared requirement on
    lib-python-config via importlib.metadata.requires and checks it names
    the exact tag pyproject.toml pins for lib-python-config -- catching a
    mismatch between the two libs' pins regardless of which one is stale.
    Passes today (installed 0.3.22 requires exactly v0.1.2, matching
    today's declared config pin); must keep passing once both pins are
    bumped together and re-synced."""
    requires = importlib.metadata.requires("lib-python-projects") or []
    config_reqs = [r for r in requires if Requirement(r).name == "lib-python-config"]
    assert config_reqs, (
        "installed lib-python-projects METADATA declares no requirement on "
        "lib-python-config at all"
    )
    assert len(config_reqs) == 1, config_reqs
    installed_projects_requires_tag = _tag_from_url(config_reqs[0])
    declared_config_tag = _declared_tag("lib-python-config")
    assert installed_projects_requires_tag == declared_config_tag, (
        f"installed lib-python-projects requires lib-python-config at "
        f"{installed_projects_requires_tag!r}, but pyproject.toml pins "
        f"lib-python-config at {declared_config_tag!r} -- the two libs' "
        f"pins must move together (run `pwsh scripts/test.ps1` to re-sync "
        f"if this is just a stale install)"
    )
