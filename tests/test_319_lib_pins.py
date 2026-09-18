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

import pytest
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
# Extra floating-semantics vocabulary, applied only to comment lines / step
# names; runner names like `ubuntu-latest` are exempt via the lookbehind.
_FLOATING_WORDS_RE = re.compile(
    r"\bmoving\b|\bkeeps\b|(?<![-\w])latest\b|\btracks?\b|\bfollows?\b|"
    r"\badvances?\b|\btip\b|\bdrift\b",
    re.IGNORECASE,
)
# The one legitimate use of "moving": the rationale sentence, matched as ONE
# contiguous normalised phrase.
_RATIONALE = "not silently through a moving branch"
_CHORE = "via an explicit chore ticket"
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


def _comment_block(name: str) -> str:
    """The contiguous `#` comment lines directly above `name`'s dependency."""
    lines = _pyproject_text().splitlines()
    dep_idx = next(i for i, line in enumerate(lines) if f"{name} @ git+" in line)
    start = dep_idx
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    return " ".join(lines[start:dep_idx])


@pytest.mark.parametrize("name", ["lib-python-config", "lib-python-projects"])
def test_pin_comment_states_same_rationale_and_own_tag(name: str) -> None:
    """Driving test (R3, symmetric for both libs): each dependency's comment
    block names ONLY its own declared tag (a stale tag such as v0.3.17 on the
    projects block fails), and carries the same rationale as contiguous
    normalised phrases: `via an explicit chore ticket` and
    `not silently through a moving branch`, and calls the pin an exact
    immutable tag. The same required-phrase set applies to both blocks."""
    declared = _declared_tag(name)
    block = _comment_block(name)
    found = _TAG_RE.findall(block)
    assert found, f"comment above {name} names no vX.Y.Z tag"
    assert set(found) == {declared}, (
        f"comment above {name} mentions tags {found!r}, declared pin is {declared!r}"
    )
    text = _norm(block)
    for phrase in ("exact immutable tag", _CHORE, _RATIONALE):
        assert phrase in text, f"comment above {name} lacks phrase {phrase!r}"
    assert _FLOATING_RE.search(block) is None, (
        f"comment above {name} still describes floating"
    )


def _scan_text_for_floating(text: str, *, comments_only: bool) -> list[str]:
    hits = {m.group(0) for m in _FLOATING_RE.finditer(text)}
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        is_comment = stripped.startswith("#") or "#" in stripped
        is_name = stripped.lstrip("- ").startswith("name:")
        if comments_only and not (is_comment or is_name):
            continue
        lines.append(_norm(line).replace(_RATIONALE, ""))
    hits |= {m.group(0) for m in _FLOATING_WORDS_RE.finditer(" | ".join(lines))}
    return sorted(hits)


_SELF_CHECKS = [
    ("# Pinned to an exact immutable tag (v0.1.2). New versions arrive via an "
     "explicit chore ticket -- not silently through a moving branch.", False),
    ("Floats on the libs' branch", True),
    ("pinned to release/0.x", True),
    ("re-fetch the branch HEAD", True),
    ("# the upstream 0.x line keeps moving and the cache lags it", True),
    ("# re-fetch in case the tip advanced", True),
    ("- name: Sync libs to latest", True),
    ("      # follows the newest release", True),
]


def test_no_floating_prose_remains_in_pin_artefacts() -> None:
    """Driving test (R3): no floating-semantics wording remains in pyproject,
    sync-libs.ps1, test.ps1 or the test workflow (comment lines / step names;
    the contiguous rationale sentence is exempt). The detector is first
    self-checked on known-good/known-bad samples, then run over the real
    files. Residual semantic rewordings that no vocabulary catches are
    verified by code review, not mechanically."""
    for sample, should_hit in _SELF_CHECKS:
        assert bool(_scan_text_for_floating(sample, comments_only=True)) is should_hit, sample

    offenders = {}
    for rel in (
        "pyproject.toml",
        "scripts/sync-libs.ps1",
        "scripts/test.ps1",
        ".github/workflows/test.yml",
    ):
        text = (_repo_root() / rel).read_text(encoding="utf-8")
        hits = _scan_text_for_floating(text, comments_only=False)
        comment_hits = _scan_text_for_floating(text, comments_only=True)
        hits = sorted(set(hits) | set(comment_hits))
        if hits:
            offenders[rel] = hits
    assert not offenders, f"floating-branch prose still present: {offenders}"
