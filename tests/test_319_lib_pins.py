"""Regression tests for package #319: pin BOTH libs to exact immutable tags.

`lib-python-config` moves from the floating `@release/0.x` branch to the exact
tag v0.1.2, and `lib-python-projects` is bumped v0.3.17 -> v0.3.19.

Floor-based convention (like #246/#254/.../#308): the *declared* tag must be an
exact `vX.Y.Z` tag that is >= this ticket's floor, so the tests survive future
bumps. The installed-vs-declared test (R2) is what proves the real environment
holds exactly the declared versions; together with the floors that pins the
acceptance criterion (config 0.1.2 / projects 0.3.19 at this ticket).
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_CONFIG_FLOOR = Version("0.1.2")
_PROJECTS_FLOOR = Version("0.3.19")
_TAG_RE = re.compile(r"v\d+\.\d+\.\d+")
_EXACT_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
# Wording that *describes* a lib as floating on a branch. Deliberately does not
# match the legitimate "not silently through a moving branch" rationale.
_FLOATING_RE = re.compile(
    r"\bfloat(?:s|ing|ed)?\b|release/[0-9Nx]|HEAD of release|branch HEAD",
    re.IGNORECASE,
)
_SYNC_LIBS_PATTERN = re.compile(r'(lib-python-[^"]+@[^"]+)')


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
        assert _SYNC_LIBS_PATTERN.search(f'"{entry}"'), entry
    captured = _SYNC_LIBS_PATTERN.findall(
        " ".join(f'"{e}"' for e in _dependencies() if "lib-python-" in e)
    )
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


def _norm(text: str) -> str:
    return " ".join(text.replace("#", " ").split()).lower()


def test_floating_regex_bites_on_floating_prose_only() -> None:
    """Guard the guard: the regex flags floating descriptions but not the
    legitimate rationale sentence."""
    legit = (
        "# Pinned to an exact immutable tag (v0.1.2). New lib versions arrive "
        "via an explicit chore ticket -- not silently through a moving branch."
    )
    assert not _FLOATING_RE.search(legit)
    for bad in (
        "Floats on the libs' branch",
        "floating libs",
        "pinned to release/0.x",
        "the HEAD of release/0.x",
        "re-fetch the branch HEAD",
        "bump to release/Nx",
    ):
        assert _FLOATING_RE.search(bad), bad


def test_config_comment_names_declared_tag_and_gives_chore_rationale() -> None:
    """Driving test (R3): the comment block above the config dependency line
    names the declared tag (and only it), states the same rationale as the
    projects comment (explicit chore ticket AND not via a moving branch), and
    no longer describes config as floating."""
    declared = _declared_tag("lib-python-config")
    lines = _pyproject_text().splitlines()
    dep_idx = next(
        i for i, line in enumerate(lines) if "lib-python-config @ git+" in line
    )
    start = dep_idx
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    block = " ".join(lines[start:dep_idx])

    found = _TAG_RE.findall(block)
    assert found, "comment above lib-python-config names no vX.Y.Z tag"
    assert all(t == declared for t in found), (
        f"comment tag mentions {found!r} do not match declared pin {declared!r}"
    )
    text = _norm(block)
    assert "chore ticket" in text, "comment lacks the explicit-chore-ticket rationale"
    assert "moving branch" in text and "not" in text.split("moving branch")[0], (
        "comment lacks the 'not silently through a moving branch' rationale"
    )
    assert "immutable" in text or "exact" in text, "comment does not say the pin is exact"
    contradiction = _FLOATING_RE.search(block)
    assert not contradiction, (
        f"config comment still describes floating: {contradiction.group(0)!r}"
    )


def test_no_floating_branch_prose_remains() -> None:
    """Driving test (R3): no floating-branch wording for the libs left in
    pyproject, sync-libs, test.ps1 or the test workflow."""
    offenders = {}
    for rel in (
        "pyproject.toml",
        "scripts/sync-libs.ps1",
        "scripts/test.ps1",
        ".github/workflows/test.yml",
    ):
        text = (_repo_root() / rel).read_text(encoding="utf-8")
        hits = sorted({m.group(0) for m in _FLOATING_RE.finditer(text)})
        if hits:
            offenders[rel] = hits
    assert not offenders, f"floating-branch prose still present: {offenders}"
