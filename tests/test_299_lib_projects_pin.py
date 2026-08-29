"""Regression test for ticket #299: bump the `lib-python-projects` exact-tag
pin in `pyproject.toml` from v0.3.14 to v0.3.15.

Modelled on tests/test_289_lib_projects_pin.py's tomllib + packaging.requirements
helpers -- reuses the same helper functions verbatim. Unlike #289's floor-based
pin assertion, this ticket names one exact tag (per plan-critic round 1
feedback), so the driving assertion for R1 pins equality to v0.3.15 rather
than only asserting a floor -- a floor alone would also pass for e.g. v0.4.0,
which isn't what's being shipped here. A floor-style constant is still kept
for consistency with the precedent files' comment/tag-matching helpers.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_EXACT_TAG = "v0.3.15"
_MIN_VERSION = Version("0.3.15")
_COMMENT_TAG_RE = re.compile(r"v\d+\.\d+\.\d+")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _dependencies() -> list[str]:
    pyproject = _repo_root() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["dependencies"]


def _lib_python_projects_entry() -> str:
    for entry in _dependencies():
        requirement = Requirement(entry)
        if requirement.name == "lib-python-projects":
            return entry
    raise AssertionError("no 'lib-python-projects' entry found in project.dependencies")


def _tag_from_url(entry: str) -> str:
    # entry looks like: "lib-python-projects @ git+https://.../lib-python-projects@v0.3.14"
    return entry.rsplit("@", 1)[-1].strip()


def _declared_tag() -> str:
    """The exact vX.Y.Z tag currently declared for lib-python-projects in
    pyproject.toml (e.g. "v0.3.15")."""
    entry = _lib_python_projects_entry()
    return _tag_from_url(entry)


def test_lib_python_projects_pin_is_exactly_v0_3_15() -> None:
    """Driving test (R1): the declared pin must equal v0.3.15 exactly -- not
    merely meet a floor, since this ticket names one exact tag (a floor alone
    would also pass for e.g. a later v0.4.0). RED against the unbumped
    pyproject.toml, which still declares v0.3.14."""
    tag = _declared_tag()
    assert tag == _EXACT_TAG, f"expected declared pin {_EXACT_TAG!r}, got {tag!r}"


def test_pin_comment_matches_declared_tag_and_floor() -> None:
    """Driving test (R2): the explanatory comment above the dependency line
    must name the current declared tag, and that tag must meet this ticket's
    floor. RED because the comment currently still says "(v0.3.14)", which
    fails the >= 0.3.15 floor."""
    declared = _declared_tag()

    pyproject = _repo_root() / "pyproject.toml"
    lines = pyproject.read_text(encoding="utf-8").splitlines()

    dep_line_idx = next(
        i for i, line in enumerate(lines) if "lib-python-projects @ git+" in line
    )
    comment_idx = dep_line_idx - 1
    while comment_idx >= 0 and lines[comment_idx].strip().startswith("#"):
        comment_idx -= 1
    comment_idx += 1

    # Scope the assertion to the comment lines only (exclude the dependency
    # line itself), so the comment's own parenthetical must name a tag --
    # not merely rely on the dependency line via _declared_tag().
    comment_block = "\n".join(lines[comment_idx:dep_line_idx])

    found_tags = _COMMENT_TAG_RE.findall(comment_block)
    assert found_tags, "expected at least one vX.Y.Z tag mention in the lib-python-projects comment block"
    assert all(tag == declared for tag in found_tags), (
        f"found tag mention(s) {found_tags!r} that don't match the declared "
        f"pin {declared!r} in the lib-python-projects comment block"
    )
    assert all(Version(tag.lstrip("v")) >= _MIN_VERSION for tag in found_tags), (
        f"found tag mention(s) {found_tags!r} in the comment block that don't "
        f"meet the v0.3.15 floor"
    )


def test_installed_lib_matches_declared_pin() -> None:
    """Driving test (R3): the environment this suite runs in must have
    EXACTLY the tag declared in pyproject.toml installed. This is the
    formulation the plan-critic asked for in place of the plan's original
    (unreachable) "pre-sync" RED condition: it fails whenever the installed
    lib-python-projects version doesn't match the declared pin -- whether
    because pyproject.toml was bumped but the environment wasn't re-synced,
    or (as observed for real, see the change report) because the environment
    is still on the old pin and pyproject.toml has not yet been bumped.
    Turns/stays GREEN only once `pwsh -File scripts/test.ps1` (or
    scripts/sync-libs.ps1) has synced the environment to match the bumped
    pyproject.toml declared here."""
    import importlib.metadata

    declared = _declared_tag().lstrip("v")

    try:
        installed = importlib.metadata.version("lib-python-projects")
    except importlib.metadata.PackageNotFoundError:
        raise AssertionError(
            "lib-python-projects is not installed in this environment -- "
            "run 'pwsh -File scripts/test.ps1' (or scripts/sync-libs.ps1) "
            "instead of a bare 'python -m pytest'"
        )

    assert installed == declared, (
        f"installed lib-python-projects=={installed} does not match the "
        f"declared pin ({declared}); run 'pwsh -File scripts/test.ps1' "
        "to sync the environment to pyproject.toml's pin (this catches a "
        "stale/local shadow in either direction -- older OR newer than the "
        "declared tag)"
    )
