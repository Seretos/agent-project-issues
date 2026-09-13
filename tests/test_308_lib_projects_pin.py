"""Regression test for ticket #308: bump the `lib-python-projects` exact-tag
pin in `pyproject.toml` from v0.3.16 to v0.3.17.

Modelled on tests/test_304_lib_projects_pin.py's tomllib + packaging.requirements
helpers -- reuses the same helper functions verbatim. Uses the floor-based
convention of #246/#254/#259/#270/#289/#299/#304 (assert the declared tag is
>= this ticket's floor) rather than a one-off exact-equality assertion, so
this test survives future bumps instead of going red for the wrong reason.

Only the two tests that actually go RED at v0.3.16 are carried here. The
five generic edge-case tests and the installed-vs-declared check already
exist dynamically in the sibling pin files (test_246/254/259/270/289/299/304)
and cover v0.3.17 automatically once the pin is edited -- they are not
re-copied here per the plan.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_MIN_VERSION = Version("0.3.17")
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
    # entry looks like: "lib-python-projects @ git+https://.../lib-python-projects@v0.3.16"
    return entry.rsplit("@", 1)[-1].strip()


def _declared_tag() -> str:
    """The exact vX.Y.Z tag currently declared for lib-python-projects in
    pyproject.toml (e.g. "v0.3.17")."""
    entry = _lib_python_projects_entry()
    return _tag_from_url(entry)


def test_lib_python_projects_pin_meets_v0_3_17_floor() -> None:
    """Driving test (R1): the declared pin must be >= v0.3.17. RED against
    the unbumped pyproject.toml, which still declares v0.3.16."""
    tag = _declared_tag()
    assert Version(tag.lstrip("v")) >= _MIN_VERSION


def test_pin_comment_matches_declared_tag_and_floor() -> None:
    """Driving test (R1): the explanatory comment above the dependency line
    must name the current declared tag, and that tag must meet this ticket's
    floor. RED because the comment currently still says "(v0.3.16)", which
    fails the >= 0.3.17 floor."""
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
        f"meet the v0.3.17 floor"
    )
